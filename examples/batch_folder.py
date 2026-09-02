#!/usr/bin/env python3
"""Отправить каталог записей и дождаться всех.

Отличие от transcribe.py — в двух вещах, из-за которых пакетная обработка
обычно и ломается: отправка ограничена по числу одновременных запросов, а
отказ по одному файлу не останавливает остальные.

    python3 examples/batch_folder.py ./архив --model gigaam-v3-e2e-rnnt
    python3 examples/batch_folder.py ./архив --workers 2 --formats txt,srt
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}

AUDIO = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus",
         ".mp4", ".mkv", ".mov", ".aac", ".wma"}


def submit(path: Path, data: dict[str, str]) -> tuple[Path, str | None, str]:
    """Ставит один файл в очередь. Возвращает (файл, идентификатор, ошибка)."""
    try:
        with path.open("rb") as handle:
            response = requests.post(f"{SERVER}/api/jobs", headers=HEADERS,
                                     files={"file": (path.name, handle)},
                                     data=data, timeout=600)
    except requests.RequestException as exc:
        return path, None, str(exc)
    if not response.ok:
        try:
            detail = response.json().get("detail", {})
        except ValueError:
            detail = {}
        return path, None, detail.get("message") or f"HTTP {response.status_code}"
    return path, response.json()["id"], ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Пакетная обработка каталога")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--formats", default="txt", help="через запятую")
    parser.add_argument("--workers", type=int, default=4,
                        help="сколько отправок одновременно (не воркеров сервера)")
    parser.add_argument("--recursive", action="store_true")
    args = parser.parse_args()

    pattern = "**/*" if args.recursive else "*"
    files = sorted(p for p in args.folder.glob(pattern)
                   if p.is_file() and p.suffix.lower() in AUDIO)
    if not files:
        print("В каталоге нет поддерживаемых записей.", file=sys.stderr)
        return 1
    print(f"Найдено файлов: {len(files)}")

    data: dict[str, str] = {}
    if args.model:
        data["model"] = args.model

    # Ограничиваем именно отправку. Сколько заданий сервер считает
    # одновременно — его дело: это max_concurrent_jobs, и лезть в него
    # отсюда не нужно.
    jobs: dict[str, Path] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for path, job_id, error in pool.map(lambda p: submit(p, data), files):
            if job_id:
                jobs[job_id] = path
                print(f"  + {path.name}")
            else:
                print(f"  ! {path.name}: {error}", file=sys.stderr)

    if not jobs:
        return 1
    print(f"Поставлено: {len(jobs)} из {len(files)}")

    done: dict[str, str] = {}
    while len(done) < len(jobs):
        time.sleep(3)
        # Один запрос на всю пачку вместо запроса на задание: облегчённый
        # список не тащит расшифровки и на сотне записей отличается по
        # объёму в десятки раз.
        response = requests.get(f"{SERVER}/api/jobs", headers=HEADERS,
                                params={"limit": 500, "light": "true"}, timeout=60)
        if not response.ok:
            print("Не удалось получить состояние очереди.", file=sys.stderr)
            return 1
        for job in response.json()["items"]:
            if job["id"] in jobs and job["status"] in ("completed", "failed", "cancelled"):
                if job["id"] not in done:
                    done[job["id"]] = job["status"]
                    mark = "✓" if job["status"] == "completed" else "✕"
                    print(f"  {mark} {jobs[job['id']].name}"
                          f"{'' if job['status'] == 'completed' else ': ' + str(job.get('error_message'))}")
        print(f"\r  готово {len(done)} из {len(jobs)}", end="", flush=True)
    print()

    ok_ids = [i for i, status in done.items() if status == "completed"]
    for fmt in (f.strip() for f in args.formats.split(",") if f.strip()):
        for job_id in ok_ids:
            response = requests.get(f"{SERVER}/api/jobs/{job_id}/download",
                                    headers=HEADERS, params={"fmt": fmt}, timeout=120)
            if response.ok:
                target = jobs[job_id].with_suffix(f".{fmt}")
                target.write_bytes(response.content)
    print(f"Выгружено: {len(ok_ids)} записей в форматах {args.formats}")
    return 0 if len(ok_ids) == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
