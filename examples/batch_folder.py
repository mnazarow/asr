#!/usr/bin/env python3
"""Отправить каталог записей и дождаться всех.

Отличие от transcribe.py — в двух вещах, из-за которых пакетная обработка
обычно и ломается: отправка ограничена по числу одновременных запросов, а
отказ по одному файлу не останавливает остальные.

    python3 examples/batch_folder.py ./архив --model gigaam-v3-e2e-rnnt
    python3 examples/batch_folder.py ./архив --workers 2 --formats txt,srt

Результат ложится рядом с записью: запись.txt, запись.srt. Файл, который уже
есть (эталонный текст, прежняя расшифровка), не перезаписывается — результат
получает имя запись.asrhub.txt; перезаписать — ключ --overwrite.
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
        # Поля ошибки — на верхнем уровне ответа; detail есть не у каждой.
        try:
            body = response.json()
        except ValueError:
            body = {}
        message = body.get("message") if isinstance(body, dict) else ""
        if not message and isinstance(body, dict):
            message = str(body.get("detail") or "")
        return path, None, message or f"HTTP {response.status_code}"
    return path, response.json()["id"], ""


def free_target(path: Path, fmt: str, overwrite: bool) -> Path:
    """Куда положить результат, не затирая чужой файл.

    Раньше результат писался в <имя>.txt без проверки, и эталонный текст
    запись.txt, лежащий рядом с записью, молча затирался.
    """
    target = path.with_suffix(f".{fmt}")
    if overwrite or not target.exists():
        return target
    target = path.with_suffix(f".asrhub.{fmt}")
    number = 1
    while target.exists():
        number += 1
        target = path.with_suffix(f".asrhub-{number}.{fmt}")
    return target


def poll(started: float, pending: set[str]) -> tuple[dict[str, dict], bool]:
    """Состояние заданий пачки: листаем список страницами.

    Прежде был один запрос limit=500 без offset: сервер больше 500 за раз
    не отдаёт, порядок — от новых, и задания за пределами первой страницы
    не попадали в «готово» никогда — скрипт крутился вечно на пачке из 600
    файлов или на сервере с потоком телефонии. Теперь — только задания,
    созданные с начала пачки, и все страницы, пока не найдены свои.
    """
    found: dict[str, dict] = {}
    since_hours = (time.time() - started) / 3600 + 0.1
    offset, page = 0, 500
    while pending - found.keys():
        response = requests.get(f"{SERVER}/api/jobs", headers=HEADERS,
                                params={"limit": page, "offset": offset, "light": "true",
                                        "since_hours": f"{since_hours:.3f}"},
                                timeout=60)
        if not response.ok:
            return found, False
        items = response.json().get("items") or []
        for job in items:
            if job.get("id") in pending:
                found[job["id"]] = job
        if len(items) < page:
            break
        offset += page
    return found, True


def main() -> int:
    parser = argparse.ArgumentParser(description="Пакетная обработка каталога")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--model", default="")
    parser.add_argument("--formats", default="txt", help="через запятую")
    parser.add_argument("--workers", type=int, default=4,
                        help="сколько отправок одновременно (не воркеров сервера)")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--timeout", type=float, default=6 * 3600,
                        help="общий срок ожидания, с (по умолчанию шесть часов)")
    parser.add_argument("--overwrite", action="store_true",
                        help="перезаписывать файлы результата, которые уже есть")
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
    started = time.time()
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
    deadline = started + args.timeout
    while len(done) < len(jobs):
        if time.time() > deadline:
            print(f"\nСрок ожидания ({args.timeout:g} с) вышел: готово {len(done)} из {len(jobs)}.",
                  file=sys.stderr)
            break
        time.sleep(3)
        # Список вместо запроса на задание: облегчённый список не тащит
        # расшифровки и на сотне записей отличается по объёму в десятки раз.
        states, ok = poll(started, set(jobs) - set(done))
        if not ok:
            print("Не удалось получить состояние очереди.", file=sys.stderr)
            return 1
        for job in states.values():
            # no_speech и short — итоги без расшифровки, а не ошибки.
            if job["status"] in ("completed", "failed", "cancelled", "no_speech", "short"):
                if job["id"] not in done:
                    done[job["id"]] = job["status"]
                    mark = {"completed": "✓", "no_speech": "–", "short": "–"}.get(
                        job["status"], "✕")
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
                target = free_target(jobs[job_id], fmt, args.overwrite)
                target.write_bytes(response.content)
    print(f"Выгружено: {len(ok_ids)} записей в форматах {args.formats}")
    без_речи = sum(1 for status in done.values() if status in ("no_speech", "short"))
    if без_речи:
        print(f"Без речи и коротких (расшифровки нет): {без_речи}")
    return 0 if len(ok_ids) + без_речи == len(jobs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
