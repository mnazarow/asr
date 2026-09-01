"""Адаптер Qwen3-ASR (Alibaba): 30 языков, лицензия Apache-2.0."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, classify_exception
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


class Qwen3ASREngine(Engine):
    id = "qwen3_asr"
    supports_batching = True
    supports_streaming = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import qwen_asr  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            try:
                import transformers  # type: ignore  # noqa: F401
                return True, ""
            except ModuleNotFoundError:
                return False, "Не установлен qwen-asr: pip install -U qwen-asr"

    def _load(self, settings: dict[str, Any]) -> Any:
        device = self.resolve_device(settings)
        try:
            from qwen_asr import QwenASR  # type: ignore

            return {"kind": "qwen_asr",
                    "model": QwenASR(self.spec.source, device=device)}
        except Exception:
            pass
        try:
            from transformers import pipeline  # type: ignore

            return {"kind": "transformers",
                    "model": pipeline("automatic-speech-recognition",
                                      model=self.spec.source,
                                      device_map="auto" if device.startswith("cuda") else None,
                                      trust_remote_code=True)}
        except ModuleNotFoundError as exc:
            raise DependencyMissing("qwen3_asr", "qwen-asr", cause=exc) from exc
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        language = self.language_for(settings)
        self.report(progress, 0.1, "распознавание")
        kind = self._model["kind"]
        model = self._model["model"]
        try:
            if kind == "qwen_asr":
                out = model.transcribe(str(audio_path), language=language)
                text = out.get("text") if isinstance(out, dict) else str(out)
                chunks = out.get("segments") if isinstance(out, dict) else None
            else:
                out = model(str(audio_path), chunk_length_s=30,
                            batch_size=int(settings.get("batch_size") or 8),
                            return_timestamps=True)
                text = out.get("text", "")
                chunks = out.get("chunks")
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        segments: list[Segment] = []
        if chunks:
            for item in chunks:
                stamp = item.get("timestamp") or (0.0, 0.0)
                piece = str(item.get("text", "")).strip()
                if piece:
                    segments.append(Segment(
                        start=float(stamp[0] or 0.0),
                        end=float(stamp[1] or stamp[0] or 0.0),
                        text=piece, language=language))
        elif str(text).strip():
            segments.append(Segment(start=0.0, end=info.duration_s,
                                    text=str(text).strip(), language=language))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(segments=segments, language=language or "",
                                   duration=info.duration_s, meta={"backend": kind})
