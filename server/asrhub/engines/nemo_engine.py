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

from ..errors import DependencyMissing, classify_exception
from ..pipeline import vad
from ..pipeline.audio import probe, slice_wav
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


class NeMoEngine(Engine):
    id = "nemo"
    supports_word_timestamps = True
    supports_batching = True
    outputs_punctuation = True

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

        # Совместимость со старыми чекпоинтами при PyTorch 2.6+
        os.environ.setdefault("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", "1")
        models_dir = settings.get("models_dir")
        if models_dir:
            os.environ.setdefault("HF_HOME", str(models_dir))
            os.environ.setdefault("NEMO_CACHE_DIR", str(models_dir))

        try:
            model = nemo_asr.models.ASRModel.from_pretrained(model_name=self.spec.source)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        device = self.resolve_device(settings)
        try:
            if device.startswith("cuda"):
                model = model.cuda()
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
        max_chunk = float(settings.get("vad_max_speech_s") or 0) or float(
            self.spec.max_audio_s or 300)

        # Модели с коротким окном (Canary — 40 с) режем по речи.
        need_chunking = bool(self.spec.max_audio_s) and duration > (self.spec.max_audio_s or 1e9)
        pieces: list[tuple[float, Path]] = []
        tmpdir: tempfile.TemporaryDirectory[str] | None = None

        if need_chunking:
            opts = dict(settings)
            opts["vad_max_speech_s"] = min(max_chunk, float(self.spec.max_audio_s or max_chunk))
            spans = vad.detect(audio_path, opts) if settings.get("vad_enabled", True) else []
            plan = vad.chunk_plan(duration, opts, spans)
            tmpdir = tempfile.TemporaryDirectory(prefix="nemo-",
                                                 dir=settings.get("temp_dir") or None)
            for idx, span in enumerate(plan):
                piece = Path(tmpdir.name) / f"chunk{idx:05d}.wav"
                slice_wav(audio_path, piece, span.start, span.end)
                pieces.append((span.start, piece))
        else:
            pieces.append((0.0, audio_path))

        batch = int(settings.get("batch_size") or 8)
        segments: list[Segment] = []
        try:
            self.report(progress, 0.1, "распознавание")
            paths = [str(p) for _, p in pieces]
            kwargs: dict[str, Any] = {"batch_size": max(1, batch)}
            if settings.get("word_timestamps", True):
                kwargs["timestamps"] = True
            language = self.language_for(settings)
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
            for (offset, _), out in zip(pieces, flat, strict=False):
                segments.extend(_convert(out, offset, language or ""))
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

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


def _convert(out: Any, offset: float, language: str) -> list[Segment]:
    """Приводит вывод NeMo к общей структуре сегментов."""
    text = getattr(out, "text", None)
    if text is None:
        text = str(out)
    timestamp = getattr(out, "timestamp", None) or {}

    seg_items = timestamp.get("segment") if isinstance(timestamp, dict) else None
    word_items = timestamp.get("word") if isinstance(timestamp, dict) else None

    words = [{
        "word": str(w.get("word", w.get("segment", ""))).strip(),
        "start": round(float(w.get("start", 0.0)) + offset, 3),
        "end": round(float(w.get("end", 0.0)) + offset, 3),
    } for w in (word_items or []) if isinstance(w, dict)]

    if seg_items:
        result: list[Segment] = []
        for item in seg_items:
            if not isinstance(item, dict):
                continue
            start = float(item.get("start", 0.0)) + offset
            end = float(item.get("end", start)) + offset
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
    end = words[-1]["end"] if words else offset
    return [Segment(start=offset, end=end, text=str(text).strip(),
                    language=language or None, words=words)]
