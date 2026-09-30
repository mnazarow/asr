#!/usr/bin/env python3
"""Снять метрики и сверить их с порогами из справочника.

Полезно, когда своей системы наблюдения ещё нет, а понять «всё ли хорошо»
надо уже сейчас. Заодно показывает, чем /api/monitoring/metrics.json удобнее
обычного формата Prometheus: к каждому числу приложены описание, обычное
значение и порог.

    python3 examples/monitoring_pull.py
    python3 examples/monitoring_pull.py --group queue --all
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}


def human(value: float, unit: str) -> str:
    """Байты и секунды в читаемом виде: каталог хранит их в базовых единицах."""
    if unit in ("Б", "B"):
        rest, step = float(value), 0
        names = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
        while rest >= 1024 and step < len(names) - 1:
            rest, step = rest / 1024, step + 1
        return f"{rest:.1f} {names[step]}"
    if unit in ("с", "s") and abs(value) >= 60:
        return f"{value / 60:.1f} мин"
    return f"{value:g}{(' ' + unit) if unit else ''}"


def level_of(value: float, threshold: dict) -> str:
    """«авария», «внимание» или пусто — по порогу из справочника метрики.

    Порог бывает включительным (`inclusive`): у метрик, где он стоит на краю
    шкалы, — например у уровня дрейфа (0, 1, 2) критический порог 2, и
    строгое «больше двух» не сработало бы никогда.
    """
    direction = threshold.get("direction")
    inclusive = bool(threshold.get("inclusive"))

    def past(limit) -> bool:
        if limit is None:
            return False
        if direction == "above":
            return value >= limit if inclusive else value > limit
        return value <= limit if inclusive else value < limit

    if not direction:
        return ""
    if past(threshold.get("critical")):
        return "авария"
    if past(threshold.get("warning")):
        return "внимание"
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Снимок метрик с разбором порогов")
    parser.add_argument("--group", default="", help="только одна группа метрик")
    parser.add_argument("--all", action="store_true", help="показать и те, что в норме")
    args = parser.parse_args()

    params = {"group": args.group} if args.group else {}
    response = requests.get(f"{SERVER}/api/monitoring/metrics.json",
                            headers=HEADERS, params=params, timeout=60)
    if not response.ok:
        print(f"Не удалось снять метрики: HTTP {response.status_code}", file=sys.stderr)
        if response.status_code == 401:
            print("  Метрики закрыты ключом: задайте ASRHUB_KEY.", file=sys.stderr)
        return 1

    payload = response.json()
    health = requests.get(f"{SERVER}/api/monitoring/health", headers=HEADERS, timeout=30)
    state = health.json().get("status", "?") if health.ok else "?"
    print(f"Состояние сервиса: {state}")
    print(f"Метрик в снимке: {len(payload.get('metrics', []))}\n")

    warned = 0
    for metric in payload.get("metrics", []):
        # У гистограммы порога нет: её ведра сравнивать не с чем.
        if metric.get("type") == "histogram":
            continue
        threshold = metric.get("threshold") or {}
        unit = metric.get("unit") or ""
        # У метрики не одно значение, а по значению на каждый набор меток:
        # по модели, по диску, по очереди.
        for sample in metric.get("values") or []:
            value = sample.get("value")
            if value is None:
                continue
            level = level_of(float(value), threshold)
            if not level and not args.all:
                continue
            if level:
                warned += 1
            labels = sample.get("labels") or {}
            name = metric.get("label") or metric["name"]
            if labels:
                name += " (" + ", ".join(f"{k}={v}" for k, v in labels.items()) + ")"
            mark = {"авария": "✕", "внимание": "!"}.get(level, " ")
            print(f"{mark} {name}: {human(float(value), unit)}")
            if level:
                limit = threshold.get("critical") if level == "авария" else threshold.get("warning")
                sign = (">" if threshold.get("direction") == "above" else "<") + \
                    ("=" if threshold.get("inclusive") else "")
                print(f"    порог: {sign} {human(float(limit), unit)}")
                if metric.get("recommendation"):
                    print(f"    что делать: {metric['recommendation']}")

    if not warned:
        print("Отклонений от порогов нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
