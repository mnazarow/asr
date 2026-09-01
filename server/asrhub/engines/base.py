"""Базовый интерфейс движка распознавания.

Каждый адаптер обязан:
* сообщать о своей доступности, не падая при отсутствии зависимостей;
* загружать модель лениво, при первом обращении;
* приводить результат к единой структуре сегментов;
* переводить исключения библиотеки в типизированные ошибки ASR Hub.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..catalog import ModelSpec
from ..errors import ASRHubError, EngineError, classify_exception
from ..logging_setup import get_logger

ProgressCallback = Callable[[float, str], None]


@dataclass(slots=True)
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None
    confidence: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None
    temperature: float | None = None
    language: str | None = None
    words: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "start": round(float(self.start), 3),
            "end": round(float(self.end), 3),
            "text": self.text,
        }
        if self.speaker:
            data["speaker"] = self.speaker
        if self.confidence is not None:
            data["confidence"] = round(float(self.confidence), 4)
        if self.no_speech_prob is not None:
            data["no_speech_prob"] = round(float(self.no_speech_prob), 4)
        if self.compression_ratio is not None:
            data["compression_ratio"] = round(float(self.compression_ratio), 3)
        if self.temperature is not None:
            data["temperature"] = float(self.temperature)
        if self.language:
            data["language"] = self.language
        if self.words:
            data["words"] = self.words
        return data


@dataclass(slots=True)
class TranscriptionResult:
    segments: list[Segment] = field(default_factory=list)
    language: str = ""
    language_probability: float | None = None
    duration: float = 0.0
    model_load_s: float = 0.0
    inference_s: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return " ".join(s.text.strip() for s in self.segments if s.text.strip()).strip()

    @property
    def word_count(self) -> int:
        return sum(len(s.text.split()) for s in self.segments)

    @property
    def avg_confidence(self) -> float | None:
        values = [s.confidence for s in self.segments if s.confidence is not None]
        return sum(values) / len(values) if values else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "text": self.text,
            "language": self.language,
            "language_probability": self.language_probability,
            "duration": round(self.duration, 3),
            "meta": self.meta,
        }


class Engine(ABC):
    """Общий интерфейс всех движков."""

    id: str = "base"
    supports_word_timestamps: bool = False
    supports_batching: bool = False
    supports_streaming: bool = False
    supports_diarization: bool = False
    outputs_punctuation: bool = False

    def __init__(self, spec: ModelSpec, settings: dict[str, Any]):
        self.spec = spec
        self.settings = settings
        self.log = get_logger(f"engine.{self.id}")
        self._model: Any = None
        self._loaded_key: str = ""
        self.last_used: float = time.time()

    # --- проверка доступности -------------------------------------------

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        """Возвращает (доступен, причина недоступности)."""
        return True, ""

    @property
    def is_loaded(self) -> bool:
        return self._model is not None

    # --- жизненный цикл модели -------------------------------------------

    def cache_key(self, settings: dict[str, Any]) -> str:
        """Ключ, при изменении которого модель нужно перезагрузить."""
        return "|".join(str(settings.get(k, "")) for k in
                        ("model", "device", "compute_type", "cpu_threads"))

    @abstractmethod
    def _load(self, settings: dict[str, Any]) -> Any:
        """Загружает модель. Вызывается один раз для набора настроек."""

    def ensure_loaded(self, settings: dict[str, Any]) -> float:
        """Гарантирует, что модель загружена. Возвращает время загрузки в секундах."""
        key = self.cache_key(settings)
        if self._model is not None and key == self._loaded_key:
            self.last_used = time.time()
            return 0.0
        if self._model is not None:
            self.unload()
        started = time.perf_counter()
        try:
            self._model = self._load(settings)
        except ASRHubError:
            raise
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        self._loaded_key = key
        elapsed = time.perf_counter() - started
        self.last_used = time.time()
        self.log.info("Модель «%s» загружена за %.1f с", self.spec.id, elapsed,
                      extra={"model": self.spec.id, "engine": self.id})
        return elapsed

    def unload(self) -> None:
        """Выгружает модель и освобождает память устройства."""
        self._model = None
        self._loaded_key = ""
        try:
            import gc

            gc.collect()
            import torch  # type: ignore

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass

    # --- распознавание ---------------------------------------------------

    @abstractmethod
    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        """Реализация распознавания конкретным движком."""

    def transcribe(self, audio_path: Path, settings: dict[str, Any],
                   progress: ProgressCallback | None = None) -> TranscriptionResult:
        """Публичная точка входа: загрузка, распознавание, замер времени."""
        load_time = self.ensure_loaded(settings)
        started = time.perf_counter()
        try:
            result = self._transcribe(Path(audio_path), settings, progress)
        except ASRHubError:
            raise
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        result.inference_s = time.perf_counter() - started
        result.model_load_s = load_time
        result.meta.setdefault("engine", self.id)
        result.meta.setdefault("model", self.spec.id)
        self.last_used = time.time()
        return result

    # --- вспомогательное -------------------------------------------------

    def resolve_device(self, settings: dict[str, Any]) -> str:
        device = str(settings.get("device") or "auto")
        if device != "auto":
            return device
        try:
            import torch  # type: ignore

            if torch.cuda.is_available():
                return "cuda"
            if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
                return "mps"
        except Exception:
            pass
        return "cpu"

    def resolve_compute_type(self, settings: dict[str, Any], device: str) -> str:
        value = str(settings.get("compute_type") or "auto")
        if value != "auto":
            return value
        if device.startswith("cuda"):
            return "float16"
        if device == "mps":
            return "float16"
        return "int8"

    def language_for(self, settings: dict[str, Any]) -> str | None:
        lang = str(settings.get("language") or "auto")
        return None if lang in ("auto", "") else lang

    def report(self, progress: ProgressCallback | None, value: float, stage: str) -> None:
        if progress is not None:
            try:
                progress(max(0.0, min(1.0, value)), stage)
            except Exception:
                pass

    def describe(self) -> dict[str, Any]:
        available, reason = self.check_available()
        return {
            "engine": self.id,
            "model": self.spec.id,
            "loaded": self.is_loaded,
            "available": available,
            "reason": reason,
            "supports": {
                "word_timestamps": self.supports_word_timestamps,
                "batching": self.supports_batching,
                "streaming": self.supports_streaming,
                "diarization": self.supports_diarization,
                "punctuation": self.outputs_punctuation,
            },
        }


class NotImplementedEngine(Engine):
    """Заглушка для движка, который не удалось создать."""

    def __init__(self, spec: ModelSpec, settings: dict[str, Any], reason: str):
        super().__init__(spec, settings)
        self.reason = reason

    def _load(self, settings: dict[str, Any]) -> Any:
        raise EngineError(self.reason)

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        raise EngineError(self.reason)
