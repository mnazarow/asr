"""Мелочи, общие для частей базы (`db.py` и её примесей)."""
from __future__ import annotations

import time


def now() -> float:
    return time.time()


def _экранировать_like(значение: str) -> str:
    """Обезвреживает образцы LIKE: обратная косая, процент, подчёркивание."""
    return (str(значение or "").replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))
