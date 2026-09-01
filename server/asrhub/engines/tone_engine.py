"""Адаптер T-one (Т-Банк) — потоковое распознавание русской телефонии."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, classify_exception
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


class ToneEngine(Engine):
    id = "tone"
    supports_streaming = True
    outputs_punctuation = False

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import tone  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Не установлен пакет T-one. Установка: "
                           "git clone https://github.com/voicekit-team/T-one && "
                           "cd T-one && poetry install. "
                           "Под Windows KenLM собирается только в WSL или Docker.")

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from tone import StreamingCTCPipeline  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("tone", "tone", cause=exc) from exc
        except ImportError as exc:
            raise DependencyMissing("tone", "tone", cause=exc) from exc
        try:
            return StreamingCTCPipeline.from_hugging_face()
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        try:
            from tone import read_audio  # type: ignore
        except ImportError:
            read_audio = None  # type: ignore

        info = probe(audio_path)
        self.report(progress, 0.1, "распознавание")
        try:
            if read_audio is not None:
                audio = read_audio(str(audio_path))
                outputs = self._model.forward_offline(audio)
            else:
                outputs = self._model.forward_offline(str(audio_path))
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        segments: list[Segment] = []
        for item in outputs or []:
            text = str(getattr(item, "text", item)).strip()
            if not text:
                continue
            start = float(getattr(item, "start_time", 0.0) or 0.0)
            end = float(getattr(item, "end_time", start) or start)
            segments.append(Segment(start=start, end=end, text=text, language="ru"))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments, language="ru", duration=info.duration_s,
            meta={"telephony_optimized": True})
