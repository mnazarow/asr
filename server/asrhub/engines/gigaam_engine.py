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
            os.environ.setdefault("GIGAAM_MODEL_DIR", str(models_dir))
            os.environ.setdefault("HF_HOME", str(models_dir))

        # Официальный путь загрузки: gigaam.load_model с именем варианта.
        name_map = {
            "ai-sage/GigaAM-v3": {"ctc": "v3_ctc", "rnnt": "v3_rnnt",
                                  "e2e_ctc": "v3_e2e_ctc", "e2e_rnnt": "v3_e2e_rnnt",
                                  "ssl": "v3_ssl"},
            "ai-sage/GigaAM-v2": {"ctc": "v2_ctc", "rnnt": "v2_rnnt", "ssl": "v2_ssl"},
            "ai-sage/GigaAM": {"ctc": "ctc", "rnnt": "rnnt", "emo": "emo"},
            "ai-sage/GigaAM-Multilingual": {"ctc": "multilingual_ctc",
                                            "large_ctc": "multilingual_large_ctc",
                                            "ssl": "multilingual_ssl"},
        }
        name = name_map.get(self.spec.source, {}).get(revision, revision)

        errors: list[str] = []
        for attempt in (name, f"{name}", self.spec.source):
            try:
                model = gigaam.load_model(attempt, device=device)
                self.log.info("GigaAM: загружен вариант «%s» на %s", attempt, device)
                return model
            except Exception as exc:      # пробуем следующий способ
                errors.append(f"{attempt}: {exc}")

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
            errors.append(f"transformers: {exc}")

        raise ModelLoadError(
            f"Не удалось загрузить GigaAM «{self.spec.id}».",
            hint=("Проверьте установку: pip install "
                  "git+https://github.com/salute-developers/GigaAM.git\n"
                  "Попытки: " + " | ".join(errors[-3:])))

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
