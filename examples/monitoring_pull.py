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
        threshold = metric.get("threshold") or {}
        value = metric.get("value")
        if value is None:
            continue
        direction = threshold.get("direction")
        critical = threshold.get("critical")
        warning = threshold.get("warning")

        level = ""
        if direction and critical is not None:
            above = direction == "above"
            if (value > critical) if above else (value < critical):
                level = "авария"
            elif warning is not None and ((value > warning) if above else (value < warning)):
                level = "внимание"

        if not level and not args.all:
            continue
        if level:
            warned += 1
        mark = {"авария": "✕", "внимание": "!"}.get(level, " ")
        unit = metric.get("unit") or ""
        print(f"{mark} {metric['label']}: {human(float(value), unit)}")
        if level:
            print(f"    порог: {'выше' if direction == 'above' else 'ниже'} {critical}")
            if metric.get("troubleshooting"):
                print(f"    что делать: {metric['troubleshooting']}")

    if not warned:
        print("Отклонений от порогов нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
