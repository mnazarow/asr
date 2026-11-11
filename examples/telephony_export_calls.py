#!/usr/bin/env python3
"""Выгрузить журнал звонков с АТС в CSV — для сверки со своей статистикой.

Журнал GET /api/telephony/calls отдаёт звонки страницами (до 500 за раз)
с отбором по периоду, станции, направлению, очереди и оператору. Пример
проходит все страницы и пишет CSV, который открывается в Excel: разделитель
«;», кодировка UTF-8 с BOM.

Пометка `skipped` объясняет, почему у звонка нет задания: «короткий», «без
ответа», «нет записи», «отложен: …». Пустая пометка и `job_id` — звонок
распознаётся или распознан.

    python3 examples/telephony_export_calls.py звонки.csv
    python3 examples/telephony_export_calls.py звонки.csv --period month --station main
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}

КОЛОНКИ = ("started_at", "station", "pbx_uid", "linkedid", "direction", "src", "dst",
           "agent", "queue", "disposition", "duration", "billsec", "job_id", "skipped")


def звонки(**отбор: str):
    """Все звонки по отбору — страница за страницей."""
    смещение, страница = 0, 500
    while True:
        ответ = requests.get(f"{SERVER}/api/telephony/calls", headers=HEADERS, timeout=60,
                             params={**отбор, "limit": страница, "offset": смещение})
        ответ.raise_for_status()
        данные = ответ.json()
        строки = данные.get("items") or данные.get("calls") or []
        yield from строки
        if len(строки) < страница:
            return
        смещение += страница


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    разбор.add_argument("out", help="куда записать CSV")
    разбор.add_argument("--period", default="week",
                        choices=["day", "week", "month", "quarter", "year", "all"])
    разбор.add_argument("--station", default="")
    разбор.add_argument("--direction", default="")
    разбор.add_argument("--queue", default="")
    разбор.add_argument("--agent", default="")
    аргументы = разбор.parse_args()
    отбор = {к: з for к, з in {"period": аргументы.period, "station": аргументы.station,
                               "direction": аргументы.direction, "queue": аргументы.queue,
                               "agent": аргументы.agent}.items() if з}
    всего = 0
    with open(аргументы.out, "w", encoding="utf-8-sig", newline="") as файл:
        запись = csv.writer(файл, delimiter=";")
        запись.writerow(КОЛОНКИ)
        for звонок in звонки(**отбор):
            начало = float(звонок.get("started_at") or 0)
            звонок["started_at"] = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало))
                                    if начало else "")
            запись.writerow([звонок.get(к, "") for к in КОЛОНКИ])
            всего += 1
    print(f"звонков: {всего} → {аргументы.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
