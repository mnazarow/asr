#!/usr/bin/env python3
"""Снять метрики и сверить их с порогами из справочника.

Полезно, когда своей системы наблюдения ещё нет, а понять «всё ли хорошо»
надо уже сейчас. Заодно показывает, чем /api/monitoring/metrics.json удобнее
обычного формата Prometheus: к каждому числу приложены описание, обычное
значение и порог.

Вердикты — от самого сервера (GET /api/monitoring/alerts), а не сравнением
сырого числа с порогом: у части метрик порог относится к выражению — доле
ответов 5xx, доле отказов, проценту занятой памяти, — и пример, сравнивавший
сам, рисовал «аварии» на здоровом сервере («Заданий обработано: 4 — порог
> 0.2»). Сервер считает эти выражения так же, как правила Prometheus.

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
    parser.add_argument("--all", action="store_true", help="показать и все метрики (справочно)")
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

    # Тревоги — от встроенного движка сервера: он знает, к какому выражению
    # относится порог (доля, прирост за окно, процент), и держит for_seconds.
    response = requests.get(f"{SERVER}/api/monitoring/alerts", headers=HEADERS, timeout=30)
    if not response.ok:
        print(f"Не удалось получить тревоги: HTTP {response.status_code}", file=sys.stderr)
        return 1
    alerts = response.json().get("alerts") or []
    warned = 0
    for alert in alerts:
        if alert.get("state") not in ("firing", "pending"):
            continue
        firing = alert.get("state") == "firing"
        warned += 1
        level = "авария" if alert.get("severity") == "critical" else "внимание"
        mark = {"авария": "✕", "внимание": "!"}[level] if firing else "…"
        name = alert.get("label") or alert.get("summary") or alert.get("metric")
        labels = alert.get("labels") or {}
        if labels:
            name += " (" + ", ".join(f"{k}={v}" for k, v in labels.items()) + ")"
        unit = alert.get("unit") or ""
        value = alert.get("value")
        shown = human(float(value), unit) if isinstance(value, (int, float)) else "—"
        print(f"{mark} {name}: {shown}{'' if firing else ' (ещё не дольше срока — ждём)'}")
        sign = (">" if alert.get("direction") == "above" else "<") + \
            ("=" if alert.get("inclusive") else "")
        threshold = alert.get("threshold")
        if isinstance(threshold, (int, float)):
            print(f"    порог: {sign} {human(float(threshold), unit)}")
        if alert.get("hint"):
            print(f"    что делать: {alert['hint']}")

    if args.all:
        # Сами числа — справочно, с порогом из справочника: к сырому
        # значению он относится не всегда (см. note у метрики).
        print("\nВсе метрики:")
        for metric in payload.get("metrics", []):
            if metric.get("type") == "histogram":
                continue
            unit = metric.get("unit") or ""
            for sample in metric.get("values") or []:
                value = sample.get("value")
                if value is None:
                    continue
                labels = sample.get("labels") or {}
                name = metric.get("label") or metric["name"]
                if labels:
                    name += " (" + ", ".join(f"{k}={v}" for k, v in labels.items()) + ")"
                print(f"  {name}: {human(float(value), unit)}")

    if not warned:
        print("Тревог нет.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
