#!/usr/bin/env python3
"""Повтор упавших заданий: разобрать причины и повторить то, что имеет смысл.

POST /api/queue/retry-failed повторяет все упавшие разом (администратору).
Пример делает то же по одному — и не трогает задания, чей отказ повтором не
лечится: «файл повреждён», «квота» и превышение предела времени повторятся
так же. Без --apply только показывает, что сделал бы.

    python3 examples/retry_failed.py                    показать
    python3 examples/retry_failed.py --apply            повторить годные
    python3 examples/retry_failed.py --apply --model gigaam-v3-ctc
"""
from __future__ import annotations

import argparse
import collections
import os
import sys

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}

#: Отказы, которые повтором не лечатся (коды ошибок — docs/08-api.md).
НЕ_ПОВТОРЯТЬ = {"audio_error", "file_too_large", "unsupported_format", "quota_exceeded",
                "job_timeout", "engine_hung", "job_cancelled"}


def упавшие():
    смещение = 0
    while True:
        ответ = requests.get(f"{SERVER}/api/jobs", headers=HEADERS, timeout=60,
                             params={"status": "failed", "light": "true", "limit": 500,
                                     "offset": смещение})
        ответ.raise_for_status()
        items = ответ.json().get("items") or []
        yield from items
        if len(items) < 500:
            return
        смещение += 500


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--apply", action="store_true", help="повторить, а не только показать")
    разбор.add_argument("--model", default="", help="повторить на другой модели")
    аргументы = разбор.parse_args()

    причины: collections.Counter[str] = collections.Counter()
    повторено = отказано = 0
    for задание in упавшие():
        код = str(задание.get("error_code") or "неизвестно")
        причины[код] += 1
        if код in НЕ_ПОВТОРЯТЬ:
            continue
        if not аргументы.apply:
            continue
        тело = {"model": аргументы.model} if аргументы.model else None
        ответ = requests.post(f"{SERVER}/api/jobs/{задание['id']}/retry", headers=HEADERS,
                              json=тело, timeout=60)
        if ответ.status_code == 200:
            повторено += 1
        else:
            отказано += 1
            print(f"{задание['id']}: {ответ.status_code} {ответ.text[:200]}", file=sys.stderr)
    for код, сколько in причины.most_common():
        пометка = " (не повторяется)" if код in НЕ_ПОВТОРЯТЬ else ""
        print(f"{код}: {сколько}{пометка}")
    if аргументы.apply:
        print(f"повторено {повторено}, отказано {отказано}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
