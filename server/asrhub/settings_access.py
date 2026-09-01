"""Чтение числовых параметров задания без потери легального нуля.

Повсюду в движках и конвейере значение бралось так:

    float(settings.get("no_speech_threshold") or 0.6)

Приём короткий, но `or` срабатывает на любом ложном значении, а для чисел
ложным является ноль. Между тем ноль у многих параметров — не «не задано», а
осмысленная величина на краю диапазона: `no_speech_threshold: 0` отключает
отбраковку тишины, `logprob_threshold: 0` — отбраковку по правдоподобию,
`job_timeout_s: 0` снимает ограничение времени, `vad_speech_pad_ms: 0`
убирает поля вокруг речи.

Получалось так: пользователь выставлял ноль, интерфейс значение принимал,
API его сохранял, отпечаток настроек менялся, задание пересчитывалось — и
возвращался ровно прежний результат, потому что движок молча подставлял своё
значение по умолчанию. Понять это по журналу было нельзя.

Здесь запасное значение берётся только когда параметра действительно нет.
"""
from __future__ import annotations

from typing import Any


def _raw(settings: dict[str, Any], key: str) -> Any:
    value = settings.get(key)
    return None if value is None or value == "" else value


def num(settings: dict[str, Any], key: str, default: float) -> float:
    """Дробное значение параметра; ноль остаётся нулём."""
    value = _raw(settings, key)
    if value is None:
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def integer(settings: dict[str, Any], key: str, default: int) -> int:
    """Целое значение параметра; ноль остаётся нулём."""
    value = _raw(settings, key)
    if value is None:
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
