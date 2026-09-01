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


class WhisperXEngine(Engine):
    id = "whisperx"
    supports_word_timestamps = True
    supports_batching = True
    supports_diarization = True
    outputs_punctuation = True

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

        device = self.resolve_device(settings)
        device = "cuda" if device.startswith("cuda") else "cpu"
        compute = self.resolve_compute_type(settings, device)
        if device == "cpu" and compute in ("float16", "int8_float16"):
            compute = "int8"
        try:
            return whisperx.load_model(
                self.spec.source, device, compute_type=compute,
                language=self.language_for(settings),
                download_root=str(settings.get("models_dir") or "") or None)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        import whisperx  # type: ignore

        device = "cuda" if self.resolve_device(settings).startswith("cuda") else "cpu"
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
                align_model, metadata = whisperx.load_align_model(
                    language_code=language, device=device,
                    model_name=align_name or None)
                raw = whisperx.align(raw["segments"], align_model, metadata, audio, device,
                                     return_char_alignments=False)
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
                diarize = whisperx.diarize.DiarizationPipeline(
                    use_auth_token=token, device=device)
                kwargs: dict[str, Any] = {}
                num = S.integer(settings, "diarization_num_speakers", 0)
                if num:
                    kwargs["num_speakers"] = num
                else:
                    kwargs["min_speakers"] = int(settings.get("diarization_min_speakers") or 1)
                    kwargs["max_speakers"] = int(settings.get("diarization_max_speakers") or 8)
                diarized = diarize(audio, **kwargs)
                raw = whisperx.assign_word_speakers(diarized, raw)
            except GatedModelError:
                raise
            except Exception as exc:
                self.log.error("Диаризация не выполнена: %s", exc)

        segments: list[Segment] = []
        for item in raw.get("segments", []):
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            words = [{
                "word": str(w.get("word", "")).strip(),
                "start": round(float(w.get("start", item.get("start", 0.0))), 3),
                "end": round(float(w.get("end", item.get("end", 0.0))), 3),
                "confidence": round(float(w.get("score", 0.0)), 4),
                "speaker": w.get("speaker"),
            } for w in (item.get("words") or []) if w.get("word")]
            speaker = item.get("speaker")
            if speaker:
                speaker = f"Говорящий {str(speaker).split('_')[-1].lstrip('0') or '1'}"
            scores = [w["confidence"] for w in words if w["confidence"]]
            segments.append(Segment(
                start=float(item.get("start", 0.0)),
                end=float(item.get("end", 0.0)),
                text=text, speaker=speaker,
                confidence=round(sum(scores) / len(scores), 4) if scores else None,
                language=language,
                words=words,
            ))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments, language=language,
            duration=segments[-1].end if segments else 0.0,
            meta={"aligned": True, "diarized": bool(settings.get("diarization_enabled"))})
