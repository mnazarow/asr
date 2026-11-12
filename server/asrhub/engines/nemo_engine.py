"""Адаптер NVIDIA NeMo: Parakeet, Canary, Nemotron.

Рекордная пропускная способность на GPU. Стек NeMo конфликтен по
зависимостям, поэтому установщик ASR Hub предлагает вынести его
в отдельное виртуальное окружение.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, EngineError, classify_exception
from ..pipeline import stitch, vad
from ..pipeline.audio import probe, slice_wav
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

#: Длиннее этого запись в модель одним проходом не идёт. Предел из карточки
#: Parakeet (1440 с) относится к A100 на 80 ГБ: внимание растёт квадратично, и
#: совещание на 15–24 минуты на карте в 8–24 ГБ кончалось нехваткой памяти, а
#: деление пакета при повторе не помогало — файл один. Пять минут проходят
#: и на карте в 8 ГБ.
БЕЗОПАСНЫЙ_ПРОХОД_С = 300.0


class NeMoEngine(Engine):
    id = "nemo"
    supports_word_timestamps = True
    supports_batching = True
    outputs_punctuation = True
    режет_сам = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import nemo.collections.asr  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Не установлен NVIDIA NeMo: pip install nemo-toolkit-asr. "
                           "Рекомендуется отдельное окружение — стек конфликтен по зависимостям.")
        except Exception as exc:
            return False, f"NeMo установлен, но не импортируется: {exc}"

    @property
    def supports_streaming(self) -> bool:      # type: ignore[override]
        return self.spec.streaming

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import nemo.collections.asr as nemo_asr  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("nemo", "nemo-toolkit-asr", cause=exc) from exc

        models_dir = settings.get("models_dir")
        if models_dir:
            os.environ.setdefault("HF_HOME", str(models_dir))
            os.environ.setdefault("NEMO_CACHE_DIR", str(models_dir))

        # Совместимость со старыми чекпоинтами при PyTorch 2.6+ — только на
        # время загрузки. Прежде переменная ставилась на весь процесс и
        # оставалась: после первой модели NeMo каждый torch.load сервера
        # (pyannote, Sortformer, чужие веса) снова исполнял pickle без
        # ограничений. Заданную администратором явно не трогаем.
        свой_флаг = "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD" not in os.environ
        if свой_флаг:
            os.environ["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"
        try:
            model = nemo_asr.models.ASRModel.from_pretrained(model_name=self.spec.source)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        finally:
            if свой_флаг:
                os.environ.pop("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", None)

        device = self.resolve_device(settings)
        try:
            if device.startswith("cuda"):
                # С номером карты: `model.cuda()` всегда шёл на карту 0, и
                # «device: cuda:1» (карта 0 занята языковой моделью) кончался
                # нехваткой памяти на чужой карте при отчёте «cuda:1».
                model = model.to(device if ":" in device else "cuda")
            elif device == "cpu":
                model = model.cpu()
            model.eval()
        except Exception as exc:
            self.log.warning("Не удалось перенести модель на %s: %s", device, exc)
        return model

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        duration = info.duration_s
        language = self.language_for(settings)
        if "canary" in self.spec.id and not language:
            # Canary язык сама не определяет: без source_lang она считает
            # запись английской — и русская запись выходила английским текстом.
            raise EngineError(
                f"Модель «{self.spec.id}» не определяет язык сама.",
                hint="Укажите язык записи (параметр «Язык»), например ru.")
        # Предел одного прохода: окно модели (Canary — 40 с), но не больше
        # безопасного (см. `БЕЗОПАСНЫЙ_ПРОХОД_С`).
        предел = min(float(self.spec.max_audio_s or БЕЗОПАСНЫЙ_ПРОХОД_С), БЕЗОПАСНЫЙ_ПРОХОД_С)
        max_chunk = min(float(settings.get("vad_max_speech_s") or 0) or предел, предел)

        need_chunking = duration > предел
        pieces: list[tuple[float, Path, float]] = []
        tmpdir: tempfile.TemporaryDirectory[str] | None = None

        if need_chunking:
            opts = dict(settings)
            opts["vad_max_speech_s"] = max_chunk
            spans = vad.detect(audio_path, opts) if settings.get("vad_enabled", True) else []
            plan = vad.chunk_plan(duration, opts, spans, предел_модели=предел)
            tmpdir = tempfile.TemporaryDirectory(prefix="nemo-",
                                                 dir=settings.get("temp_dir") or None)
            for idx, span in enumerate(plan):
                piece = Path(tmpdir.name) / f"chunk{idx:05d}.wav"
                slice_wav(audio_path, piece, span.start, span.end)
                pieces.append((span.start, piece, span.end))
        else:
            pieces.append((0.0, audio_path, duration))

        batch = int(settings.get("batch_size") or 8)
        segments: list[Segment] = []
        try:
            self.report(progress, 0.1, "распознавание")
            paths = [str(p) for _, p, _ in pieces]
            # Таймкоды просятся всегда, а слова отбрасываются на выходе, если
            # их не просили. Без таймкодов NeMo не даёт и границ реплик, и
            # запись выходила одной репликой 0.0–0.0: субтитры с нулевыми
            # интервалами, диаризации не с чем пересекаться.
            kwargs: dict[str, Any] = {"batch_size": max(1, batch), "timestamps": True}
            if language and "canary" in self.spec.id:
                kwargs["source_lang"] = language
                kwargs["target_lang"] = language if str(
                    settings.get("task")) != "translate" else "en"
                kwargs["task"] = "asr" if str(settings.get("task")) != "translate" else "ast"
            try:
                outputs = self._model.transcribe(paths, **kwargs)
            except TypeError:
                kwargs.pop("timestamps", None)
                outputs = self._model.transcribe(paths, **kwargs)

            flat = _flatten(outputs)
            if len(flat) != len(pieces):
                self.log.warning(
                    "NeMo вернула %d результатов на %d фрагментов — часть будет потеряна",
                    len(flat), len(pieces))
            for (offset, _, конец), out in zip(pieces, flat, strict=False):
                segments.extend(_convert(out, offset, language or "", конец=конец))
            if not settings.get("word_timestamps", True):
                for segment in segments:
                    segment.words = []
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

        # Перекрытие фрагментов существует ради слова на разрезе, но речь
        # в зоне перекрытия распозналась дважды, и обе копии попадали в
        # расшифровку. Сшивка убирает повтор.
        if need_chunking:
            segments = stitch.убрать_повторы(
                segments, float(settings.get("chunk_overlap_s") or 0.0))

        if not segments:
            self.log.warning("NeMo не вернула сегментов для %s", audio_path.name)

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language=self.language_for(settings) or "",
            duration=duration,
            meta={"chunks": len(pieces), "batch_size": batch},
        )


def _flatten(outputs: Any) -> list[Any]:
    if outputs is None:
        return []
    if isinstance(outputs, (list, tuple)):
        if outputs and isinstance(outputs[0], (list, tuple)):
            return list(outputs[0])
        return list(outputs)
    return [outputs]


def _convert(out: Any, offset: float, language: str, *,
             конец: float | None = None) -> list[Segment]:
    """Приводит вывод NeMo к общей структуре сегментов.

    `конец` — где кончается фрагмент в координатах записи: граница реплики,
    когда таймкодов у модели нет.
    """
    text = getattr(out, "text", None)
    if text is None:
        text = str(out)
    timestamp = getattr(out, "timestamp", None) or {}

    seg_items = timestamp.get("segment") if isinstance(timestamp, dict) else None
    word_items = timestamp.get("word") if isinstance(timestamp, dict) else None

    words: list[dict[str, Any]] = [{
        "word": str(w.get("word", w.get("segment", ""))).strip(),
        "start": round(float(w.get("start", 0.0)) + offset, 3),
        "end": round(float(w.get("end", 0.0)) + offset, 3),
    } for w in (word_items or []) if isinstance(w, dict)]

    if seg_items:
        result: list[Segment] = []
        for item in seg_items:
            if not isinstance(item, dict):
                continue
            # Конец без своего значения — начало реплики. Прежде запасным
            # значением шло уже сдвинутое начало, и сдвиг прибавлялся второй
            # раз: (41.0, 81.0) при фрагменте с 40 секунд.
            сырое_начало = float(item.get("start", 0.0))
            start = сырое_начало + offset
            end = float(item.get("end", сырое_начало)) + offset
            piece = str(item.get("segment", item.get("text", ""))).strip()
            if not piece:
                continue
            result.append(Segment(
                start=start, end=end, text=piece, language=language or None,
                words=[w for w in words if start <= w["start"] < end],
            ))
        if result:
            return result

    if not str(text).strip():
        return []
    end = words[-1]["end"] if words else (конец if конец is not None else offset)
    start = words[0]["start"] if words else offset
    return [Segment(start=start, end=end, text=str(text).strip(),
                    language=language or None, words=words)]
