#!/usr/bin/env python3
"""Распознать один файл и забрать текст.

Самый частый сценарий: поставить запись в очередь, дождаться результата,
сохранить расшифровку. Показано и ожидание опросом — оно проще всего, когда
файл один и подписываться на события незачем.

    python3 examples/transcribe.py запись.mp3
    python3 examples/transcribe.py запись.mp3 --model gigaam-v3-e2e-rnnt
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}


def fail(response: requests.Response) -> None:
    """Печатает ошибку так, как её описывает сервер, и выходит.

    Формат ответа об ошибке одинаков у всех маршрутов: code для машины,
    message и hint — для человека. Разбирать его стоит именно так, а не
    показывать пользователю голый код состояния.
    """
    try:
        detail = response.json().get("detail", {})
    except ValueError:
        detail = {}
    print(f"Ошибка {response.status_code}: {detail.get('message') or response.text[:200]}",
          file=sys.stderr)
    if detail.get("hint"):
        print(f"  {detail['hint']}", file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Распознавание одного файла")
    parser.add_argument("file", type=Path)
    parser.add_argument("--model", default="", help="модель; по умолчанию серверная")
    parser.add_argument("--language", default="", help="язык; auto — определять")
    parser.add_argument("--diarization", action="store_true", help="разделить по говорящим")
    parser.add_argument("--out", type=Path, help="куда сохранить текст")
    args = parser.parse_args()

    if not args.file.exists():
        print(f"Файл не найден: {args.file}", file=sys.stderr)
        return 1

    # Параметры задания можно передать отдельными полями формы: сервер
    # принимает любой ключ из GET /api/params и отвечает внятной ошибкой на
    # опечатку. Второй способ — поле settings с объектом JSON внутри.
    data = {}
    if args.model:
        data["model"] = args.model
    if args.language:
        data["language"] = args.language
    if args.diarization:
        data["diarization_enabled"] = "true"

    with args.file.open("rb") as handle:
        response = requests.post(f"{SERVER}/api/jobs", headers=HEADERS,
                                 files={"file": (args.file.name, handle)},
                                 data=data, timeout=300)
    if not response.ok:
        fail(response)

    job = response.json()
    job_id = job["id"]
    print(f"Задание {job_id}: {job['filename']}")

    # Опрос раз в две секунды. Для одного файла этого достаточно; когда
    # заданий много, дешевле подписаться на /ws — см. live_events.py.
    while True:
        response = requests.get(f"{SERVER}/api/jobs/{job_id}", headers=HEADERS, timeout=30)
        if not response.ok:
            fail(response)
        job = response.json()
        status = job["status"]
        if status == "completed":
            break
        if status in ("failed", "cancelled"):
            print(f"\n{job.get('error_message') or status}", file=sys.stderr)
            if job.get("error_hint"):
                print(f"  {job['error_hint']}", file=sys.stderr)
            return 1
        progress = float(job.get("progress") or 0) * 100
        print(f"\r  {status} {progress:3.0f}%  {job.get('stage', '')}", end="", flush=True)
        time.sleep(2)

    print(f"\r  готово: RTF {job.get('rtf')}, слов {job.get('words_count')}"
          f"{' ' * 20}")

    # Формат выгрузки выбирается параметром fmt. json содержит всё: сегменты,
    # слова, уверенность, говорящих и метаданные задания.
    response = requests.get(f"{SERVER}/api/jobs/{job_id}/download",
                            headers=HEADERS, params={"fmt": "txt"}, timeout=60)
    if not response.ok:
        fail(response)

    target = args.out or args.file.with_suffix(".txt")
    target.write_text(response.text, encoding="utf-8")
    print(f"Расшифровка: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
