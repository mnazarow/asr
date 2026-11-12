"""Адаптер WhisperX: faster-whisper + форсированное выравнивание + диаризация."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import DependencyMissing, GatedModelError, classify_exception
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_ALIGN_MODELS = {
    "ru": "jonatasgrosman/wav2vec2-large-xlsr-53-russian",
    "uk": "Yehor/wav2vec2-xls-r-300m-uk-with-small-lm",
    "kk": "aismlv/wav2vec2-large-xlsr-kazakh",
}


def имя_говорящего(метка: Any) -> str:
    """«SPEAKER_00» pyannote — «Говорящий 1», «SPEAKER_01» — «Говорящий 2».

    Раньше номер получался срезанием ведущих нулей: у «SPEAKER_00» —
    пусто, и подставлялась единица, у «SPEAKER_01» — тоже «1». Первые два
    собеседника становились одним «Говорящий 1», и диалог двух человек
    выходил у WhisperX монологом.
    """
    цифры = "".join(с for с in str(метка).rsplit("_", 1)[-1] if с.isdigit())
    if not цифры:
        return str(метка)
    return f"Говорящий {int(цифры) + 1}"


def _слова_реплики(item: dict[str, Any], пословно: bool) -> list[dict[str, Any]]:
    """Слова реплики WhisperX; слово без выравнивания — между соседями.

    У слов, которым выравнивание не нашло границ (числа, символы вне алфавита
    wav2vec2), нет ни start, ни end, ни score. Прежде им ставились границы
    всей реплики и уверенность 0,0: «2024» занимал всю реплику — ломались
    пословная диаризация и субтитры, — и слово подсвечивалось как самое
    сомнительное. Теперь время берётся между концом предыдущего выровненного
    слова и началом следующего, а уверенности нет (None): её и не было.
    """
    сырые = [w for w in item.get("words") or [] if w.get("word")]
    начало_реплики = float(item.get("start", 0.0) or 0.0)
    конец_реплики = float(item.get("end", начало_реплики) or начало_реплики)
    слова: list[dict[str, Any]] = []
    for номер, w in enumerate(сырые):
        if w.get("start") is not None and w.get("end") is not None:
            начало, конец = float(w["start"]), float(w["end"])
        else:
            слева = next((float(п["end"]) for п in reversed(сырые[:номер])
                          if п.get("end") is not None), начало_реплики)
            справа = next((float(п["start"]) for п in сырые[номер + 1:]
                           if п.get("start") is not None), конец_реплики)
            начало, конец = слева, max(слева, справа)
        оценка = w.get("score")
        слово = {
            "word": str(w.get("word", "")).strip(),
            "start": round(начало, 3),
            "end": round(конец, 3),
            "confidence": round(float(оценка), 4) if оценка is not None else None,
        }
        if пословно and w.get("speaker"):
            слово["speaker"] = имя_говорящего(w["speaker"])
        слова.append(слово)
    return слова


class WhisperXEngine(Engine):
    id = "whisperx"
    supports_word_timestamps = True
    supports_batching = True
    supports_diarization = True
    outputs_punctuation = True
    режет_сам = True              # свой VAD внутри whisperx.load_model

    def _устройство(self, settings: dict[str, Any]) -> tuple[str, int]:
        """Устройство и номер карты в том виде, в каком их ждёт WhisperX.

        Прежде «cuda:1» сводилось к «cuda» без номера, и модель садилась на
        карту 0 — на ту, что занята, ради чего и задавали первую.
        """
        device = self.resolve_device(settings)
        if not device.startswith("cuda"):
            return "cpu", 0
        _, _, номер = device.partition(":")
        return "cuda", int(номер) if номер.strip().isdigit() else 0

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import whisperx  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Не установлен WhisperX: pip install whisperx. "
                           "Требуется отдельное окружение — пакет жёстко фиксирует torch~=2.8.")
        except Exception as exc:
            return False, f"WhisperX установлен, но не импортируется: {exc}"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import whisperx  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("whisperx", "whisperx", cause=exc) from exc

        device, номер = self._устройство(settings)
        compute = self.resolve_compute_type(settings, device)
        if device == "cpu" and compute in ("float16", "int8_float16"):
            compute = "int8"
        # Язык в load_model не передаётся: модель с заданным языком берёт его
        # из своего токенизатора и при `language: auto` определение не
        # запускает, а в ключ кеша язык не входит — язык первого задания
        # «прилипал», и русская запись после английской шла английской.
        # Язык задаётся каждому вызову transcribe.
        try:
            return whisperx.load_model(
                self.spec.source, device, device_index=номер, compute_type=compute,
                download_root=str(settings.get("models_dir") or "") or None)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        import whisperx  # type: ignore

        device, номер = self._устройство(settings)
        if device == "cuda":
            device = f"cuda:{номер}"
        audio = whisperx.load_audio(str(audio_path))
        batch = int(settings.get("batch_size") or 16)

        self.report(progress, 0.15, "распознавание")
        raw = self._model.transcribe(audio, batch_size=batch,
                                     language=self.language_for(settings))
        language = raw.get("language") or self.language_for(settings) or "ru"

        if settings.get("word_timestamps", True):
            self.report(progress, 0.5, "выравнивание таймкодов")
            align_name = str(settings.get("align_model") or "") or _ALIGN_MODELS.get(language, "")
            try:
                from ..pipeline import model_cache  # noqa: PLC0415

                # Один раз на язык и устройство, а не на каждое задание.
                запись = model_cache.взять(
                    ("whisperx_align", id(whisperx), language, device, align_name),
                    lambda: whisperx.load_align_model(
                        language_code=language, device=device,
                        model_name=align_name or None))
                align_model, metadata = запись.модель
                with запись.замок:
                    raw = whisperx.align(raw["segments"], align_model, metadata, audio,
                                         device, return_char_alignments=False)
            except Exception as exc:
                self.log.warning("Выравнивание не выполнено (%s), используются исходные таймкоды", exc)

        if settings.get("diarization_enabled") and str(
                settings.get("diarization_backend", "auto")) in ("auto", "pyannote"):
            self.report(progress, 0.75, "разделение по говорящим")
            token = str(settings.get("hf_token") or os.environ.get("HF_TOKEN", ""))
            if not token:
                raise GatedModelError(
                    "pyannote/speaker-diarization-community-1",
                    "https://huggingface.co/pyannote/speaker-diarization-community-1")
            try:
                # whisperx 3.8 (он тянет pyannote.audio 4) принимает `token`,
                # прежние — `use_auth_token`. Вызов по старому имени падал с
                # TypeError, и диаризация WhisperX не работала вовсе — лишь
                # строка ошибки в журнале сервера.
                import inspect  # noqa: PLC0415

                from ..pipeline import model_cache  # noqa: PLC0415

                конвейер = whisperx.diarize.DiarizationPipeline
                try:
                    параметры = inspect.signature(конвейер).parameters
                except (TypeError, ValueError):
                    параметры = {}  # type: ignore[assignment]

                def загрузить() -> Any:
                    if "use_auth_token" in параметры and "token" not in параметры:
                        return конвейер(use_auth_token=token, device=device)
                    return конвейер(token=token, device=device)

                # Конвейер диаризации — один на устройство, а не новый на
                # каждое задание: загрузка pyannote стоит секунды и гигабайт.
                запись_диаризации = model_cache.взять(
                    ("whisperx_diarize", id(конвейер), device), загрузить)
                diarize = запись_диаризации.модель
                kwargs: dict[str, Any] = {}
                num = S.integer(settings, "diarization_num_speakers", 0)
                if num:
                    kwargs["num_speakers"] = num
                else:
                    kwargs["min_speakers"] = int(settings.get("diarization_min_speakers") or 1)
                    kwargs["max_speakers"] = int(settings.get("diarization_max_speakers") or 8)
                with запись_диаризации.замок:
                    diarized = diarize(audio, **kwargs)
                raw = whisperx.assign_word_speakers(diarized, raw)
            except GatedModelError:
                raise
            except Exception as exc:
                self.log.error("Диаризация не выполнена: %s", exc)

        пословно = bool(settings.get("diarization_assign_words", True))
        segments: list[Segment] = []
        for item in raw.get("segments", []):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            words = _слова_реплики(item, пословно)
            speaker = item.get("speaker")
            scores = [w["confidence"] for w in words if w.get("confidence")]
            segments.append(Segment(
                start=float(item.get("start", 0.0)),
                end=float(item.get("end", 0.0)),
                text=text, speaker=имя_говорящего(speaker) if speaker else None,
                confidence=round(sum(scores) / len(scores), 4) if scores else None,
                language=language,
                words=words,
            ))
        if пословно:
            # Реплика, внутри которой сменился говорящий, — на части, как и
            # у остальных движков (см. diarization_assign_words).
            from ..pipeline.diarization import разделить_по_словам  # noqa: PLC0415

            segments = разделить_по_словам(segments)

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments, language=language,
            duration=segments[-1].end if segments else 0.0,
            meta={"aligned": True, "diarized": bool(settings.get("diarization_enabled"))})
