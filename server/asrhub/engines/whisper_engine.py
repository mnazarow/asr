"""Адаптер openai-whisper — эталонная реализация.

Держим её ради сверки результатов: она медленнее faster-whisper в 2–4 раза,
но воспроизводит поведение, описанное в оригинальной статье. Важная деталь:
в Python API значения beam_size и best_of по умолчанию равны None
(жадный поиск), тогда как в CLI они равны 5. ASR Hub всегда передаёт
их явно, чтобы результаты были воспроизводимы.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, classify_exception
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


class WhisperEngine(Engine):
    id = "whisper"
    supports_word_timestamps = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import whisper  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен пакет openai-whisper: pip install openai-whisper"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import whisper  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("whisper", "openai-whisper", cause=exc) from exc
        device = self.resolve_device(settings)
        if device.startswith("cuda") and ":" in device:
            device = device.replace("cuda:", "cuda:")
        return whisper.load_model(
            self.spec.source, device=device,
            download_root=str(settings.get("models_dir") or "") or None)

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        device = self.resolve_device(settings)
        temperature: Any
        if settings.get("temperature_fallback", True):
            temperature = (float(settings.get("temperature") or 0.0), 0.2, 0.4, 0.6, 0.8, 1.0)
        else:
            temperature = float(settings.get("temperature") or 0.0)

        options: dict[str, Any] = {
            "language": self.language_for(settings),
            "task": str(settings.get("task") or "transcribe"),
            "temperature": temperature,
            "beam_size": int(settings.get("beam_size") or 5),
            "best_of": int(settings.get("best_of") or 5),
            "compression_ratio_threshold": float(
                settings.get("compression_ratio_threshold") or 2.4),
            "logprob_threshold": float(settings.get("logprob_threshold") or -1.0),
            "no_speech_threshold": float(settings.get("no_speech_threshold") or 0.6),
            "condition_on_previous_text": bool(
                settings.get("condition_on_previous_text", False)),
            "word_timestamps": bool(settings.get("word_timestamps", True)),
            "fp16": device.startswith("cuda"),
            "verbose": None,
        }
        patience = settings.get("patience")
        if patience:
            options["patience"] = float(patience)
        length_penalty = settings.get("length_penalty")
        if length_penalty and abs(float(length_penalty) - 1.0) > 1e-6:
            options["length_penalty"] = float(length_penalty)
        prompt = str(settings.get("initial_prompt") or "").strip()
        if prompt:
            options["initial_prompt"] = prompt
            if settings.get("carry_initial_prompt"):
                options["carry_initial_prompt"] = True
        threshold = float(settings.get("hallucination_silence_threshold") or 0.0)
        if threshold > 0 and options["word_timestamps"]:
            options["hallucination_silence_threshold"] = threshold

        self.report(progress, 0.1, "распознавание")
        try:
            raw = self._model.transcribe(str(audio_path), **options)
        except TypeError:
            # Старые версии не знают часть параметров — убираем необязательные.
            for key in ("carry_initial_prompt", "hallucination_silence_threshold",
                        "patience", "length_penalty"):
                options.pop(key, None)
            raw = self._model.transcribe(str(audio_path), **options)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        segments: list[Segment] = []
        for seg in raw.get("segments", []):
            words = [{
                "word": str(w.get("word", "")).strip(),
                "start": round(float(w.get("start", 0.0)), 3),
                "end": round(float(w.get("end", 0.0)), 3),
                "confidence": round(float(w.get("probability", 0.0)), 4),
            } for w in (seg.get("words") or [])]
            logprob = seg.get("avg_logprob")
            confidence = round(min(1.0, max(0.0, 2.718281828 ** float(logprob))), 4) \
                if logprob is not None else None
            segments.append(Segment(
                start=float(seg.get("start", 0.0)),
                end=float(seg.get("end", 0.0)),
                text=str(seg.get("text", "")).strip(),
                confidence=confidence,
                no_speech_prob=seg.get("no_speech_prob"),
                compression_ratio=seg.get("compression_ratio"),
                temperature=seg.get("temperature"),
                language=raw.get("language"),
                words=words,
            ))
        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments,
            language=str(raw.get("language") or ""),
            duration=segments[-1].end if segments else 0.0,
            meta={"reference_implementation": True},
        )
