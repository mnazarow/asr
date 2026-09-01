"""Адаптер faster-whisper (CTranslate2) — основной движок семейства Whisper.

Учтено:
* transcribe() возвращает ленивый генератор — ошибки всплывают при итерации,
  поэтому обход завёрнут в обработчик;
* имя параметра log_prob_threshold отличается от logprob_threshold в openai-whisper;
* у BatchedInferencePipeline другие значения по умолчанию;
* рассогласование версий cuDNN — самая частая ошибка установки, для неё
  выдаётся подробная подсказка.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, classify_exception
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_CT2_NAMES = {
    "large-v3": "large-v3", "large-v2": "large-v2", "large-v1": "large-v1",
    "large-v3-turbo": "deepdml/faster-whisper-large-v3-turbo-ct2",
    "distil-large-v3": "distil-whisper/distil-large-v3-ct2",
    "distil-whisper/distil-large-v3.5-ct2": "distil-whisper/distil-large-v3.5-ct2",
}


class FasterWhisperEngine(Engine):
    id = "faster_whisper"
    supports_word_timestamps = True
    supports_batching = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import faster_whisper  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен пакет faster-whisper: pip install faster-whisper"

    def cache_key(self, settings: dict[str, Any]) -> str:
        return "|".join([
            self.spec.source, self.resolve_device(settings),
            self.resolve_compute_type(settings, self.resolve_device(settings)),
            str(settings.get("cpu_threads") or 0), str(settings.get("num_workers") or 1),
        ])

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("faster_whisper", "faster-whisper", cause=exc) from exc

        device = self.resolve_device(settings)
        if device == "mps":
            self.log.info("faster-whisper не поддерживает MPS — используется CPU с int8")
            device = "cpu"
        ct_device = "cuda" if device.startswith("cuda") else ("cpu" if device != "rocm" else "cuda")
        device_index = 0
        if ":" in device:
            try:
                device_index = int(device.split(":", 1)[1])
            except ValueError:
                device_index = 0

        compute_type = self.resolve_compute_type(settings, device)
        if ct_device == "cpu" and compute_type in ("float16", "int8_float16", "bfloat16"):
            compute_type = "int8"

        name = _CT2_NAMES.get(self.spec.source, self.spec.source)
        try:
            return WhisperModel(
                name,
                device=ct_device,
                device_index=device_index,
                compute_type=compute_type,
                cpu_threads=int(settings.get("cpu_threads") or 0),
                num_workers=int(settings.get("num_workers") or 1),
                download_root=str(settings.get("models_dir") or "") or None,
            )
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _build_options(self, settings: dict[str, Any]) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "language": self.language_for(settings),
            "task": str(settings.get("task") or "transcribe"),
            "beam_size": int(settings.get("beam_size") or 5),
            "best_of": int(settings.get("best_of") or 5),
            "patience": float(settings.get("patience") or 1.0),
            "length_penalty": float(settings.get("length_penalty") or 1.0),
            "repetition_penalty": float(settings.get("repetition_penalty") or 1.0),
            "no_repeat_ngram_size": int(settings.get("no_repeat_ngram_size") or 0),
            "compression_ratio_threshold": float(
                settings.get("compression_ratio_threshold") or 2.4),
            "log_prob_threshold": float(settings.get("logprob_threshold") or -1.0),
            "no_speech_threshold": float(settings.get("no_speech_threshold") or 0.6),
            "condition_on_previous_text": bool(settings.get("condition_on_previous_text", False)),
            "prompt_reset_on_temperature": float(
                settings.get("prompt_reset_on_temperature") or 0.5),
            "suppress_blank": bool(settings.get("suppress_blank", True)),
            "word_timestamps": bool(settings.get("word_timestamps", True)),
            "multilingual": bool(settings.get("multilingual_detection", False)),
        }

        if settings.get("temperature_fallback", True):
            base = float(settings.get("temperature") or 0.0)
            opts["temperature"] = [base, 0.2, 0.4, 0.6, 0.8, 1.0]
        else:
            opts["temperature"] = [float(settings.get("temperature") or 0.0)]

        prompt = str(settings.get("initial_prompt") or "").strip()
        if prompt:
            opts["initial_prompt"] = prompt
        hotwords = str(settings.get("hotwords") or "").strip()
        if hotwords:
            opts["hotwords"] = hotwords

        threshold = float(settings.get("hallucination_silence_threshold") or 0.0)
        if threshold > 0 and opts["word_timestamps"]:
            opts["hallucination_silence_threshold"] = threshold

        max_new = int(settings.get("max_new_tokens") or 0)
        if max_new > 0:
            opts["max_new_tokens"] = max_new

        raw_suppress = str(settings.get("suppress_tokens") or "-1").strip()
        if raw_suppress:
            try:
                opts["suppress_tokens"] = [int(x) for x in raw_suppress.split(",") if x.strip()]
            except ValueError:
                opts["suppress_tokens"] = [-1]

        if self.language_for(settings) is None:
            opts["language_detection_threshold"] = float(
                settings.get("language_detection_threshold") or 0.5)
            opts["language_detection_segments"] = int(
                settings.get("language_detection_segments") or 1)

        if settings.get("vad_enabled", True):
            opts["vad_filter"] = True
            opts["vad_parameters"] = {
                "threshold": float(settings.get("vad_threshold") or 0.5),
                "min_speech_duration_ms": int(settings.get("vad_min_speech_ms") or 250),
                "max_speech_duration_s": float(settings.get("vad_max_speech_s") or 30.0),
                "min_silence_duration_ms": int(settings.get("vad_min_silence_ms") or 500),
                "speech_pad_ms": int(settings.get("vad_speech_pad_ms") or 200),
            }
            neg = settings.get("vad_neg_threshold")
            if neg is not None:
                opts["vad_parameters"]["neg_threshold"] = float(neg)
        else:
            opts["vad_filter"] = False
        return opts

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        options = self._build_options(settings)
        batch_size = int(settings.get("batch_size") or 1)

        model = self._model
        runner = model
        if batch_size > 1 and self.supports_batching:
            try:
                from faster_whisper import BatchedInferencePipeline  # type: ignore

                runner = BatchedInferencePipeline(model=model)
                options["batch_size"] = batch_size
                # У пакетного конвейера свои умолчания — восстанавливаем ожидаемые.
                options.setdefault("without_timestamps", not options.get("word_timestamps", True))
            except Exception as exc:
                self.log.info("Пакетный режим недоступен (%s), обработка по одному", exc)
                runner = model
                options.pop("batch_size", None)

        self.report(progress, 0.05, "запуск распознавания")
        try:
            iterator, info = runner.transcribe(str(audio_path), **options)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        duration = float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[Segment] = []
        try:
            # Генератор ленивый: настоящая работа и настоящие ошибки — здесь.
            for seg in iterator:
                words = []
                for word in (getattr(seg, "words", None) or []):
                    words.append({
                        "word": getattr(word, "word", "").strip(),
                        "start": round(float(getattr(word, "start", 0.0)), 3),
                        "end": round(float(getattr(word, "end", 0.0)), 3),
                        "confidence": round(float(getattr(word, "probability", 0.0)), 4),
                    })
                logprob = getattr(seg, "avg_logprob", None)
                confidence = None
                if logprob is not None:
                    confidence = round(min(1.0, max(0.0, 2.718281828 ** float(logprob))), 4)
                segments.append(Segment(
                    start=float(getattr(seg, "start", 0.0)),
                    end=float(getattr(seg, "end", 0.0)),
                    text=str(getattr(seg, "text", "")).strip(),
                    confidence=confidence,
                    no_speech_prob=getattr(seg, "no_speech_prob", None),
                    compression_ratio=getattr(seg, "compression_ratio", None),
                    temperature=getattr(seg, "temperature", None),
                    language=getattr(info, "language", None),
                    words=words,
                ))
                if duration > 0:
                    self.report(progress, min(0.97, 0.05 + 0.92 * (segments[-1].end / duration)),
                                "распознавание")
        except Exception as exc:
            if segments:
                self.log.error("Распознавание прервано после %d сегментов: %s", len(segments), exc)
                raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        return TranscriptionResult(
            segments=segments,
            language=str(getattr(info, "language", "") or ""),
            language_probability=getattr(info, "language_probability", None),
            duration=duration,
            meta={
                "batch_size": options.get("batch_size", 1),
                "vad_filter": options.get("vad_filter"),
                "compute_type": self.resolve_compute_type(
                    settings, self.resolve_device(settings)),
            },
        )
