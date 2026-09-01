"""Реестр движков распознавания с кешем загруженных моделей."""
from __future__ import annotations

import threading
import time
from typing import Any

from ..catalog import ModelSpec, get_engine, get_model
from ..errors import DependencyMissing, ModelNotFound, UnsupportedFeature
from ..logging_setup import get_logger
from .base import Engine, Segment, TranscriptionResult
from .demo import DemoEngine
from .faster_whisper_engine import FasterWhisperEngine
from .gigaam_engine import GigaAMEngine
from .hf_transformers_engine import TransformersEngine
from .misc_engines import (
    FunASREngine,
    KyutaiEngine,
    MoonshineEngine,
    OmnilingualEngine,
    SherpaOnnxEngine,
    VoxtralEngine,
)
from .nemo_engine import NeMoEngine
from .qwen3_engine import Qwen3ASREngine
from .tone_engine import ToneEngine
from .vosk_engine import VoskEngine
from .whisper_cpp_engine import WhisperCppEngine
from .whisper_engine import WhisperEngine
from .whisperx_engine import WhisperXEngine

log = get_logger("engines")

ENGINE_CLASSES: dict[str, type[Engine]] = {
    "gigaam": GigaAMEngine,
    "faster_whisper": FasterWhisperEngine,
    "whisper": WhisperEngine,
    "whisper_cpp": WhisperCppEngine,
    "nemo": NeMoEngine,
    "transformers": TransformersEngine,
    "qwen3_asr": Qwen3ASREngine,
    "vosk": VoskEngine,
    "tone": ToneEngine,
    "whisperx": WhisperXEngine,
    "funasr": FunASREngine,
    "moonshine": MoonshineEngine,
    "omnilingual": OmnilingualEngine,
    "voxtral": VoxtralEngine,
    "kyutai": KyutaiEngine,
    "sherpa_onnx": SherpaOnnxEngine,
    "demo": DemoEngine,
}

__all__ = ["Engine", "Segment", "TranscriptionResult", "ENGINE_CLASSES",
           "EngineRegistry", "engine_status", "available_engines"]


def engine_status() -> list[dict[str, Any]]:
    """Состояние всех движков: установлен ли, что мешает, что умеет."""
    out: list[dict[str, Any]] = []
    for engine_id, cls in ENGINE_CLASSES.items():
        spec = get_engine(engine_id)
        try:
            available, reason = cls.check_available()
        except Exception as exc:      # проверка не должна ронять список
            available, reason = False, f"Ошибка проверки: {exc}"
        out.append({
            "id": engine_id,
            "name": spec.name if spec else engine_id,
            "description": spec.description if spec else "",
            "available": available,
            "reason": reason,
            "license": spec.license if spec else "",
            "homepage": spec.homepage if spec else "",
            "requirements_file": spec.requirements_file if spec else "",
            "install_notes": spec.install_notes if spec else "",
            "known_issues": spec.known_issues if spec else [],
            "supports": {
                "gpu": spec.supports_gpu if spec else True,
                "cpu": spec.supports_cpu if spec else True,
                "mps": spec.supports_mps if spec else False,
                "streaming": spec.supports_streaming if spec else False,
                "batching": spec.supports_batching if spec else False,
            },
            "weight": spec.weight if spec else 100,
        })
    out.sort(key=lambda item: (not item["available"], item["weight"]))
    return out


def available_engines() -> set[str]:
    return {item["id"] for item in engine_status() if item["available"]}


class EngineRegistry:
    """Кеш загруженных движков с вытеснением по давности использования.

    Загрузка модели занимает от 5 до 60 секунд, поэтому кеш критичен для
    очереди со смешанными заданиями. Размер кеша задаётся параметром
    model_cache_size и должен подбираться по объёму видеопамяти.
    """

    def __init__(self, max_size: int = 2, idle_unload_s: int = 900):
        self.max_size = max(1, int(max_size))
        self.idle_unload_s = int(idle_unload_s)
        self._cache: dict[str, Engine] = {}
        self._lock = threading.RLock()

    def configure(self, max_size: int, idle_unload_s: int) -> None:
        with self._lock:
            self.max_size = max(1, int(max_size))
            self.idle_unload_s = int(idle_unload_s)
            self._evict_if_needed()

    def resolve(self, settings: dict[str, Any]) -> tuple[ModelSpec, str]:
        """Определяет модель и движок по настройкам задания."""
        model_id = str(settings.get("model") or "")
        spec = get_model(model_id)
        if spec is None:
            from ..catalog import suggest_models

            raise ModelNotFound(model_id, suggest_models(model_id))
        engine_id = str(settings.get("engine") or "auto")
        if engine_id in ("", "auto"):
            engine_id = spec.engine
        if engine_id not in ENGINE_CLASSES:
            raise UnsupportedFeature(f"движок «{engine_id}»", model=spec.id)
        return spec, engine_id

    def get(self, settings: dict[str, Any]) -> Engine:
        spec, engine_id = self.resolve(settings)
        cls = ENGINE_CLASSES[engine_id]
        available, reason = cls.check_available()
        if not available:
            raise DependencyMissing(engine_id).with_hint(reason)

        key = f"{engine_id}::{spec.id}::{settings.get('device', 'auto')}::" \
              f"{settings.get('compute_type', 'auto')}"
        with self._lock:
            engine = self._cache.get(key)
            if engine is None:
                engine = cls(spec, settings)
                self._cache[key] = engine
                self._evict_if_needed(protect=key)
            engine.last_used = time.time()
            return engine

    def _evict_if_needed(self, protect: str = "") -> None:
        while len(self._cache) > self.max_size:
            candidates = [(k, e) for k, e in self._cache.items() if k != protect]
            if not candidates:
                break
            oldest_key, oldest = min(candidates, key=lambda item: item[1].last_used)
            log.info("Выгрузка модели «%s» — превышен размер кеша", oldest.spec.id)
            try:
                oldest.unload()
            except Exception as exc:
                log.warning("Ошибка при выгрузке: %s", exc)
            self._cache.pop(oldest_key, None)

    def collect_idle(self) -> int:
        """Выгружает модели, простаивающие дольше заданного времени."""
        if self.idle_unload_s <= 0:
            return 0
        cutoff = time.time() - self.idle_unload_s
        removed = 0
        with self._lock:
            for key, engine in list(self._cache.items()):
                if engine.last_used < cutoff and engine.is_loaded:
                    log.info("Выгрузка модели «%s» после простоя", engine.spec.id)
                    try:
                        engine.unload()
                    except Exception:
                        pass
                    self._cache.pop(key, None)
                    removed += 1
        return removed

    def unload_all(self) -> None:
        with self._lock:
            for engine in self._cache.values():
                try:
                    engine.unload()
                except Exception:
                    pass
            self._cache.clear()

    def loaded(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{
                "key": key,
                "model": engine.spec.id,
                "engine": engine.id,
                "loaded": engine.is_loaded,
                "idle_s": round(time.time() - engine.last_used, 1),
            } for key, engine in self._cache.items()]
