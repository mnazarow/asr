"""Адаптер GigaAM (SaluteDevices).

Особенности, учтённые в реализации:
* один проход модели ограничен 25 секундами — длинное аудио режется по VAD;
* пакет gigaam на PyPI устарел, ставится из git;
* transcribe_longform требует pyannote и токен Hugging Face, поэтому
  ASR Hub по умолчанию делает нарезку сам и не зависит от gated-моделей;
* варианты e2e возвращают текст с пунктуацией и числами — постобработка
  для них отключается автоматически.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, EngineError, ModelLoadError
from ..pipeline import vad
from ..pipeline.audio import probe, slice_wav
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_MAX_CHUNK_S = 22.0          # запас к жёсткому пределу модели в 25 секунд


#: По каким словам в ошибке узнаётся причина. Порядок важен: доступ к весам
#: выглядит как «файл не найден», и общее правило перехватило бы его первым.
#:
#: Слова должны быть такими, чтобы их нельзя было встретить в постороннем
#: тексте. Это не педантизм: короткое «ssl» однажды совпало с именем модели
#: «ssl» в перечне доступных вариантов, и отказ из-за нехватки прав на запись
#: был выдан за отсутствие интернета. Человек пошёл проверять сеть, а дело
#: было в каталоге. По той же причине здесь нет «token» — оно живёт внутри
#: «tokenizer», а токенизатор GigaAM качает при каждой загрузке e2e-модели.
_LOAD_REASONS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("401", "403", "gated", "authorization", "unauthorized",
      "invalid token", "token is required", "hf_token"),
     "к весам нужен доступ по токену Hugging Face",
     "Токен задаётся в разделе «Доступ» веб-интерфейса или ключом hf_token "
     "в config.yaml. Модель ai-sage/GigaAM-v3 требует принятия условий на "
     "странице модели."),
    (("connectionerror", "connection refused", "connection reset",
      "connection aborted", "name resolution", "getaddrinfo", "timed out",
      "timeout", "temporary failure", "unreachable", "proxyerror",
      "sslerror", "ssl:", "ssl certificate", "certificate verify",
      "errno -3", "errno -2"),
     "сервер не смог обратиться к хранилищу весов",
     "Проверьте доступ в интернет с сервера или загрузите веса заранее: "
     "bash scripts/models.sh download <модель>"),
    (("no space left", "errno 28"),
     "не хватило места на диске",
     "Освободите место в каталоге моделей и повторите."),
    (("out of memory", "cuda error", "cublas", "cudnn"),
     "не хватило памяти видеокарты или сломан её драйвер",
     "Попробуйте device=cpu или модель поменьше; проверьте nvidia-smi."),
    (("read-only file system", "errno 30"),
     "каталог, куда пишется библиотека, доступен только для чтения",
     "Служба работает с ProtectHome=read-only, поэтому домашний каталог для "
     "неё закрыт, а библиотеки по умолчанию складывают кеш именно туда. "
     "Кеш нужно увести в каталог данных."),
    (("permission denied", "errno 13"),
     "нет прав на каталог",
     "Каталог должен принадлежать пользователю службы."),
    (("modulenotfound", "no module named", "importerror", "undefined symbol"),
     "окружение движка неполное",
     "Переустановите движок: bash scripts/models.sh install-engine gigaam"),
    (("no such file", "does not exist", "checkpoint", "no file named"),
     "веса не найдены на диске",
     "Загрузите их: bash scripts/models.sh download <модель>"),
)


#: Имя варианта, которым GigaAM называет свои веса. Библиотека принимает
#: только эти короткие имена (или путь к .ckpt) — идентификатор репозитория
#: Hugging Face она не понимает вовсе.
_VARIANTS: dict[str, dict[str, str]] = {
    "ai-sage/GigaAM-v3": {"ctc": "v3_ctc", "rnnt": "v3_rnnt",
                          "e2e_ctc": "v3_e2e_ctc", "e2e_rnnt": "v3_e2e_rnnt",
                          "ssl": "v3_ssl"},
    "ai-sage/GigaAM-v2": {"ctc": "v2_ctc", "rnnt": "v2_rnnt", "ssl": "v2_ssl"},
    # Голые «ctc»/«rnnt» тут были ошибкой: библиотека сама разворачивает
    # короткое имя в v3_*, и репозиторий первой версии молча отдавал третью.
    "ai-sage/GigaAM": {"ctc": "v1_ctc", "rnnt": "v1_rnnt",
                       "ssl": "v1_ssl", "emo": "emo"},
    "ai-sage/GigaAM-Multilingual": {"ctc": "multilingual_ctc",
                                    "large_ctc": "multilingual_large_ctc",
                                    "ssl": "multilingual_ssl"},
}


def variant_name(source: str, revision: str) -> str:
    """Короткое имя варианта GigaAM по паре «источник + ревизия».

    Вынесено из движка наружу, потому что тем же соответствием пользуется
    предварительная загрузка весов: скачивать надо ровно тот файл, который
    потом станет искать загрузчик, иначе на диске лежит одно, а движок ждёт
    другого — ровно так и вышло, когда веса качались снапшотом с Hugging
    Face, а библиотека брала .ckpt с CDN.
    """
    return _VARIANTS.get(source, {}).get(revision, revision)


def weights_file(source: str, revision: str) -> str:
    """Имя файла весов, которое библиотека кладёт в download_root."""
    name = variant_name(source, revision)
    # Короткие имена библиотека сама разворачивает в v3_*.
    if name in ("ctc", "rnnt", "e2e_ctc", "e2e_rnnt", "ssl"):
        name = f"v3_{name}"
    return f"{name}.ckpt"


def _failing_path(text: str) -> str:
    """Достаёт путь, на котором споткнулась библиотека.

    В сообщении вида «[Errno 30] Read-only file system: \'/home/asrhub\'» этот
    путь — самое полезное, что есть: он один отвечает на вопрос «а куда,
    собственно, она писала».
    """
    import re
    match = re.search(r"[\'\"](/[^\'\"]{2,200})[\'\"]", text)
    return match.group(1) if match else ""


def _load_failure(model_id: str, errors: list[str], device: str,
                  models_dir: str) -> ModelLoadError:
    """Собирает отказ, который называет причину, а не только факт.

    «Не удалось загрузить GigaAM» — это факт, известный и без нас. Причина же
    лежит в тексте попыток, и раньше она уезжала в подсказку, которую
    обратный вызов телефонии не передаёт вовсе: у принимающей стороны
    оставалась одна строка без единой зацепки.
    """
    # Разбираем сначала первую попытку, и только потом остальные. Попытки
    # идут по убыванию осмысленности: первая — штатный путь загрузки, и её
    # отказ и есть причина. Дальше идут запасные, которые падают по своим
    # поводам, и их текст может увести разбор в сторону — так и вышло, когда
    # перечень доступных моделей из второй попытки перебил настоящую причину
    # из первой.
    reason = hint = ""
    for scope in ([errors[0]] if errors else [], errors):
        haystack = " | ".join(scope).lower()
        for needles, text, advice in _LOAD_REASONS:
            if any(needle in haystack for needle in needles):
                reason, hint = text, advice
                break
        if reason:
            break

    message = f"Не удалось загрузить GigaAM «{model_id}»"
    message += f": {reason}." if reason else "."
    parts = [hint] if hint else [
        "Причина в тексте попыток ниже; проверьте установку движка и наличие весов.",
    ]
    # Путь из ошибки — самое полезное, что в ней есть: он отвечает на вопрос
    # «куда именно не удалось записать», на который иначе нет ответа вовсе.
    путь = _failing_path(errors[0]) if errors else ""
    if путь:
        parts.append(f"Путь, на котором споткнулась загрузка: {путь}")
    if models_dir:
        parts.append(f"Каталог моделей: {models_dir}")
    parts.append(f"Устройство: {device}")
    # Попытки — целиком: их две-три, и каждая объясняет свой способ загрузки.
    parts.append("Попытки: " + " | ".join(errors))
    return ModelLoadError(message, hint="\n".join(parts))


class GigaAMEngine(Engine):
    id = "gigaam"
    supports_word_timestamps = True
    supports_batching = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import gigaam  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, (
                "Не установлен пакет gigaam. Внимание: версия с PyPI устарела, "
                "ставьте из репозитория: "
                "pip install git+https://github.com/salute-developers/GigaAM.git")

    @property
    def outputs_punctuation(self) -> bool:      # type: ignore[override]
        return "e2e" in (self.spec.revision or "")

    def cache_key(self, settings: dict[str, Any]) -> str:
        return f"{self.spec.source}|{self.spec.revision}|{self.resolve_device(settings)}"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import gigaam  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("gigaam", "gigaam", cause=exc) from exc

        device = self.resolve_device(settings)
        revision = self.spec.revision or "rnnt"
        models_dir = settings.get("models_dir") or ""
        if models_dir:
            # GIGAAM_MODEL_DIR не читает никто: сама библиотека берёт каталог
            # только из аргумента download_root, а без него — из
            # os.path.expanduser("~/.cache/gigaam"). Служба работает с
            # ProtectHome=read-only, и запись туда кончалась OSError [Errno 30]
            # на «/home/asrhub». Переменную оставляем на случай, если её
            # когда-нибудь начнут читать, но полагаемся на аргумент.
            os.environ.setdefault("GIGAAM_MODEL_DIR", str(models_dir))
            os.environ.setdefault("HF_HOME", str(models_dir))

        # Официальный путь загрузки: gigaam.load_model с именем варианта.
        name = variant_name(self.spec.source, revision)

        errors: list[str] = []
        # Список без повторов: раньше первым и вторым шло одно и то же имя, и
        # в отчёт об ошибке попадали две одинаковые строки — вытесняя ту, что
        # объясняла настоящую причину.
        #
        # Идентификатор репозитория (ai-sage/GigaAM-v3) в этот список больше не
        # входит. Он не мог сработать никогда: load_model принимает либо
        # короткое имя варианта из своего перечня, либо путь к файлу .ckpt, а
        # на всё прочее отвечает «Model not found. Available model names: […]».
        # Попытка была не просто бесполезной — её ответ с перечнем имён и
        # сбивал разбор причины.
        attempts = list(dict.fromkeys(
            [name] + ([self.spec.source] if os.path.isfile(
                os.path.expanduser(self.spec.source or "")) else [])))
        # download_root — единственный способ увести загрузку из домашнего
        # каталога; иначе библиотека пишет в ~/.cache/gigaam.
        root = str(models_dir) if models_dir else None
        if root:
            os.makedirs(root, exist_ok=True)
        for attempt in attempts:
            try:
                model = gigaam.load_model(attempt, device=device,
                                          download_root=root)
                self.log.info("GigaAM: загружен вариант «%s» на %s", attempt, device)
                return model
            except Exception as exc:      # пробуем следующий способ
                errors.append(f"{attempt}: {type(exc).__name__}: {exc}")

        # Запасной путь — через transformers с trust_remote_code
        try:
            from transformers import AutoModel  # type: ignore

            model = AutoModel.from_pretrained(
                self.spec.source, revision=revision, trust_remote_code=True)
            if device != "cpu":
                model = model.to(device)
            self.log.info("GigaAM: загружен через transformers, ревизия «%s»", revision)
            return model
        except Exception as exc:
            errors.append(f"transformers: {type(exc).__name__}: {exc}")

        raise _load_failure(self.spec.id, errors, device, str(models_dir))

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        duration = info.duration_s
        want_words = bool(settings.get("word_timestamps", True))

        opts = dict(settings)
        opts["vad_max_speech_s"] = min(float(opts.get("vad_max_speech_s") or _MAX_CHUNK_S),
                                       _MAX_CHUNK_S)

        if settings.get("vad_enabled", True):
            self.report(progress, 0.05, "поиск речи")
            spans = vad.detect(audio_path, opts)
        else:
            spans = []
        plan = vad.chunk_plan(duration, opts, spans)
        if not plan:
            plan = [vad.SpeechSegment(0.0, min(duration, _MAX_CHUNK_S))]

        segments: list[Segment] = []
        with tempfile.TemporaryDirectory(prefix="gigaam-",
                                         dir=settings.get("temp_dir") or None) as tmp:
            tmpdir = Path(tmp)
            for idx, span in enumerate(plan):
                self.report(progress, 0.05 + 0.9 * (idx / max(1, len(plan))), "распознавание")
                if span.duration < 0.15:
                    continue
                piece = tmpdir / f"chunk{idx:05d}.wav"
                try:
                    slice_wav(audio_path, piece, span.start, span.end)
                except Exception as exc:
                    self.log.warning("Не удалось вырезать фрагмент %.2f–%.2f: %s",
                                     span.start, span.end, exc)
                    continue
                text, words = self._run_chunk(piece, want_words)
                if not text.strip():
                    continue
                shifted = [
                    {"word": w.get("word", w.get("text", "")),
                     "start": round(float(w.get("start", 0.0)) + span.start, 3),
                     "end": round(float(w.get("end", 0.0)) + span.start, 3),
                     "confidence": w.get("confidence")}
                    for w in words
                ] if words else []
                segments.append(Segment(
                    start=span.start,
                    end=span.end,
                    text=text.strip(),
                    language="ru",
                    words=shifted,
                ))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language="ru",
            language_probability=1.0,
            duration=duration,
            meta={"chunks": len(plan), "revision": self.spec.revision,
                  "punctuation_from_model": self.outputs_punctuation},
        )

    def _run_chunk(self, path: Path, want_words: bool) -> tuple[str, list[dict[str, Any]]]:
        model = self._model
        try:
            if want_words:
                try:
                    out = model.transcribe(str(path), word_timestamps=True)
                except TypeError:
                    out = model.transcribe(str(path))
            else:
                out = model.transcribe(str(path))
        except Exception as exc:
            message = str(exc).lower()
            if "25" in message and ("second" in message or "длин" in message):
                raise EngineError(
                    "GigaAM отказалась обрабатывать фрагмент длиннее 25 секунд.",
                    hint="Уменьшите vad_max_speech_s до 22 секунд или включите VAD.",
                ) from exc
            raise

        if isinstance(out, dict):
            text = str(out.get("transcription") or out.get("text") or "")
            words = out.get("words") or out.get("word_timestamps") or []
        elif isinstance(out, (list, tuple)) and out:
            first = out[0]
            if isinstance(first, dict):
                text = str(first.get("transcription") or first.get("text") or "")
                words = first.get("words") or []
            else:
                text, words = str(first), []
        else:
            text, words = str(out), []
        return text, list(words) if isinstance(words, (list, tuple)) else []
