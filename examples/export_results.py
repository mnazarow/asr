#!/usr/bin/env python3
"""Пакетная выгрузка результатов: все готовые записи за период — в каталог.

Список заданий отдаётся страницами (GET /api/jobs, до 500 за раз), файл
результата — по одному (GET /api/jobs/{id}/download?format=…). Пример
проходит все страницы и складывает файлы в каталог; уже выгруженное при
повторном запуске не качается заново — так его удобно ставить в cron.

    python3 examples/export_results.py выгрузка/ --since-hours 24 --format txt
    python3 examples/export_results.py выгрузка/ --format json --content qc:low
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}


def готовые(since: float | None, content: str):
    """Все завершённые задания по отбору — страница за страницей."""
    смещение, страница = 0, 500
    while True:
        параметры = {"status": "completed", "light": "true", "limit": страница,
                     "offset": смещение, "order": "created_at ASC"}
        if since:
            параметры["since"] = since
        if content:
            параметры["content"] = content
        ответ = requests.get(f"{SERVER}/api/jobs", headers=HEADERS, params=параметры,
                             timeout=60)
        ответ.raise_for_status()
        items = ответ.json().get("items") or []
        yield from items
        if len(items) < страница:
            return
        смещение += страница


def имя_файла(задание: dict, формат: str) -> str:
    основа = Path(str(задание.get("filename") or задание["id"])).stem
    основа = re.sub(r"[^\w.\-]+", "_", основа)[:80] or "запись"
    return f"{основа}.{задание['id']}.{формат}"


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("каталог")
    разбор.add_argument("--format", default="txt", choices=("txt", "srt", "vtt", "json", "docx"))
    разбор.add_argument("--since-hours", type=float, default=0.0,
                        help="только поставленные за последние N часов")
    разбор.add_argument("--content", default="", help="отбор: negative, qc:low, …")
    аргументы = разбор.parse_args()

    куда = Path(аргументы.каталог)
    куда.mkdir(parents=True, exist_ok=True)
    since = time.time() - аргументы.since_hours * 3600 if аргументы.since_hours else None
    скачано = пропущено = 0
    for задание in готовые(since, аргументы.content):
        файл = куда / имя_файла(задание, аргументы.format)
        if файл.exists():
            пропущено += 1
            continue
        ответ = requests.get(f"{SERVER}/api/jobs/{задание['id']}/download",
                             headers=HEADERS, params={"fmt": аргументы.format}, timeout=120)
        if ответ.status_code != 200:
            print(f"{задание['id']}: ответ {ответ.status_code}", file=sys.stderr)
            continue
        # Через временный файл: прерванная загрузка не выглядит готовой.
        временный = файл.with_suffix(файл.suffix + ".part")
        временный.write_bytes(ответ.content)
        временный.replace(файл)
        скачано += 1
    print(f"скачано {скачано}, уже было {пропущено}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
