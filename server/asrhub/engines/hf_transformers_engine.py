"""Универсальный адаптер моделей Hugging Face Transformers.

Обслуживает wav2vec2, MOSS Transcribe, Cohere Transcribe, Granite Speech,
OWSM, Phi-4 и Vikhr Borealis — то есть всё, что публикуется как обычный
чекпоинт Transformers.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import DependencyMissing, classify_exception
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_SPEAKER_RE = re.compile(r"\[(S\d+|SPK\d+|SPEAKER_\d+)\]\s*")


class TransformersEngine(Engine):
    id = "transformers"
    supports_word_timestamps = False
    supports_batching = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import transformers  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен пакет transformers: pip install transformers torch"

    @property
    def supports_diarization(self) -> bool:      # type: ignore[override]
        return self.spec.diarization

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import torch  # type: ignore
            from transformers import pipeline  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("transformers", "transformers", cause=exc) from exc

        device = self.resolve_device(settings)
        compute = self.resolve_compute_type(settings, device)
        dtype = torch.float16 if compute in ("float16", "int8_float16") else (
            torch.bfloat16 if compute == "bfloat16" else torch.float32)
        if device == "cpu":
            dtype = torch.float32

        kwargs: dict[str, Any] = {
            "model": self.spec.source,
            "device_map": "auto" if device.startswith("cuda") else None,
            "torch_dtype": dtype,
        }
        if settings.get("models_dir"):
            kwargs["model_kwargs"] = {"cache_dir": str(settings["models_dir"])}
        try:
            return pipeline("automatic-speech-recognition",
                            **{k: v for k, v in kwargs.items() if v is not None})
        except Exception as exc:
            # Часть моделей требует доверенного кода из репозитория
            try:
                kwargs.setdefault("model_kwargs", {})["trust_remote_code"] = True
                return pipeline("automatic-speech-recognition",
                                **{k: v for k, v in kwargs.items() if v is not None})
            except Exception:
                raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        chunk = float(settings.get("chunk_length_s") or 0) or 30.0
        overlap = S.num(settings, "chunk_overlap_s", 1.0)

        kwargs: dict[str, Any] = {
            "chunk_length_s": chunk,
            "stride_length_s": max(0.0, min(overlap, chunk / 6)),
            "batch_size": int(settings.get("batch_size") or 8),
            "return_timestamps": True,
        }
        language = self.language_for(settings)
        generate: dict[str, Any] = {}
        if language:
            generate["language"] = language
        if str(settings.get("task")) == "translate":
            generate["task"] = "translate"
        if generate:
            kwargs["generate_kwargs"] = generate

        self.report(progress, 0.1, "распознавание")
        try:
            raw = self._model(str(audio_path), **kwargs)
        except TypeError:
            kwargs.pop("generate_kwargs", None)
            raw = self._model(str(audio_path), **kwargs)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        segments: list[Segment] = []
        chunks = raw.get("chunks") if isinstance(raw, dict) else None
        if chunks:
            for item in chunks:
                stamp = item.get("timestamp") or (None, None)
                start = float(stamp[0]) if stamp[0] is not None else 0.0
                end = float(stamp[1]) if stamp[1] is not None else start
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                speaker = None
                match = _SPEAKER_RE.match(text)
                if match:
                    speaker = f"Говорящий {match.group(1)[-2:].lstrip('0') or '1'}"
                    text = _SPEAKER_RE.sub("", text, count=1).strip()
                segments.append(Segment(start=start, end=end, text=text,
                                        speaker=speaker, language=language))
        else:
            text = str(raw.get("text", "") if isinstance(raw, dict) else raw).strip()
            if text:
                segments.append(Segment(start=0.0, end=info.duration_s, text=text,
                                        language=language))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments, language=language or "",
            duration=info.duration_s,
            meta={"pipeline": "transformers", "chunk_length_s": chunk},
        )
