"""Кеш вспомогательных моделей конвейера: диаризация и выравнивание.

Модели распознавания живут в реестре движков и грузятся один раз, а
диаризация (pyannote, Sortformer) и выравнивание (wav2vec2 у WhisperX)
грузились заново на каждое задание и на каждый канал: секунды загрузки и
гигабайт видеопамяти, выделяемый и освобождаемый по кругу. На очереди из
сотни звонков это минуты чистой загрузки весов.

Здесь они хранятся так же, как движки: по ключу, с отметкой последнего
использования и собственным замком. Замок нужен не только загрузке — сами
конвейеры pyannote и wav2vec2 не рассчитаны на одновременный вызов из двух
потоков, а воркеров по умолчанию два. Выгружает простаивающие реестр движков
вместе со своими (`EngineRegistry.collect_idle` и `unload_all`).
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging_setup import get_logger

log = get_logger("model_cache")


@dataclass
class Запись:
    модель: Any
    использована: float
    замок: threading.Lock = field(default_factory=threading.Lock)


_записи: dict[tuple[Any, ...], Запись] = {}
_замок = threading.Lock()


def взять(ключ: tuple[Any, ...], загрузить: Callable[[], Any]) -> Запись:
    """Модель из кеша; при первом обращении — загрузка.

    Загрузка идёт под общим замком: два задания, одновременно дошедшие до
    диаризации, иначе грузили бы две копии одной модели.
    """
    with _замок:
        запись = _записи.get(ключ)
        if запись is None:
            запись = Запись(загрузить(), time.time())
            _записи[ключ] = запись
            log.info("Загружена вспомогательная модель: %s", ключ[0])
        запись.использована = time.time()
        return запись


def выгрузить(простой_с: float | None = None) -> int:
    """Выгружает модели, простаивающие дольше `простой_с`; None — все.

    Занятую (её замок взят) не трогает: выгрузка посреди диаризации часовой
    записи оставила бы задание с моделью, которой уже нет.
    """
    сейчас = time.time()
    with _замок:
        лишние = [ключ for ключ, запись in _записи.items()
                  if not запись.замок.locked()
                  and (простой_с is None or сейчас - запись.использована > простой_с)]
        for ключ in лишние:
            _записи.pop(ключ, None)
    if лишние:
        try:
            import gc  # noqa: PLC0415

            gc.collect()
            import torch  # type: ignore  # noqa: PLC0415

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:                                   # noqa: BLE001
            pass
        log.info("Выгружено вспомогательных моделей: %d", len(лишние))
    return len(лишние)


def загружено() -> list[str]:
    """Что сейчас в кеше — для состояния сервера."""
    with _замок:
        return [str(ключ[0]) for ключ in _записи]
