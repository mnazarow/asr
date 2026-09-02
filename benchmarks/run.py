#!/usr/bin/env python3
"""Прогон набора записей через несколько моделей и сравнение результатов.

Числа из публикаций авторов моделей получены на других наборах и другом
оборудовании. Этот сценарий отвечает на другой вопрос: какая модель лучше
именно на ваших записях и вашей машине.

    python3 benchmarks/run.py ./эталон --models gigaam-v3-e2e-rnnt,faster-whisper-large-v3
    python3 benchmarks/run.py ./эталон --reference ./эталон/тексты

Эталонные тексты — файлы .txt с тем же именем, что и запись.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}
HERE = Path(__file__).resolve().parent

AUDIO = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".mp4", ".mkv", ".mov"}


def wait_for(job_id: str, limit_s: float = 3600) -> dict:
    """Ждёт завершения задания. Возвращает карточку целиком."""
    deadline = time.time() + limit_s
    while time.time() < deadline:
        response = requests.get(f"{SERVER}/api/jobs/{job_id}", headers=HEADERS, timeout=30)
        response.raise_for_status()
        job = response.json()
        if job["status"] in ("completed", "failed", "cancelled"):
            return job
        time.sleep(2)
    raise TimeoutError(f"Задание {job_id} не завершилось за {limit_s:.0f} с")


def run_model(model: str, files: list[Path], references: dict[str, str],
              warmup: bool) -> dict:
    """Прогоняет все записи одной моделью."""
    results: list[dict] = []
    print(f"\n=== {model}")

    order = files
    if warmup and files:
        # Первое задание включает загрузку весов — на маленьком наборе она
        # одна перевешивает всё остальное. Прогоняем и выбрасываем.
        print("  прогрев…", end="", flush=True)
        order = [files[0], *files]

    for index, path in enumerate(order):
        skipped = warmup and index == 0
        data = {"model": model, "deduplicate_jobs": "false"}
        reference = references.get(path.stem)
        if reference and not skipped:
            data["reference_text"] = reference
        with path.open("rb") as handle:
            response = requests.post(f"{SERVER}/api/jobs", headers=HEADERS,
                                     files={"file": (path.name, handle)},
                                     data=data, timeout=600)
        if not response.ok:
            print(f"\n  ! {path.name}: HTTP {response.status_code} {response.text[:120]}")
            continue
        job = wait_for(response.json()["id"])
        if skipped:
            print(" готов")
            continue
        if job["status"] != "completed":
            print(f"  ✕ {path.name}: {job.get('error_message')}")
            continue
        results.append({
            "file": path.name,
            "duration_s": job.get("media_duration_s"),
            "rtf": job.get("rtf"),
            "processing_time_s": job.get("processing_time_s"),
            "words": job.get("words_count"),
            "confidence": job.get("avg_confidence"),
            "wer": job.get("wer"),
        })
        wer = f", WER {job['wer']:.3f}" if job.get("wer") is not None else ""
        print(f"  ✓ {path.name}: RTF {job.get('rtf')}{wer}")

    def average(field: str) -> float | None:
        values = [r[field] for r in results if r.get(field) is not None]
        return round(statistics.mean(values), 4) if values else None

    return {
        "model": model,
        "files": len(results),
        "rtf_avg": average("rtf"),
        "rtf_median": (round(statistics.median([r["rtf"] for r in results
                                                if r.get("rtf") is not None]), 4)
                       if any(r.get("rtf") is not None for r in results) else None),
        "confidence_avg": average("confidence"),
        "wer_avg": average("wer"),
        "audio_seconds": round(sum(r["duration_s"] or 0 for r in results), 1),
        "compute_seconds": round(sum(r["processing_time_s"] or 0 for r in results), 1),
        "runs": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Сравнительный прогон моделей")
    parser.add_argument("folder", type=Path, help="каталог с записями")
    parser.add_argument("--models", default="", help="через запятую; пусто — все доступные")
    parser.add_argument("--reference", type=Path, help="каталог с эталонными текстами")
    parser.add_argument("--no-warmup", action="store_true", help="не отбрасывать первый прогон")
    args = parser.parse_args()

    files = sorted(p for p in args.folder.glob("*")
                   if p.is_file() and p.suffix.lower() in AUDIO)
    if not files:
        print("В каталоге нет записей.", file=sys.stderr)
        return 1
    if len(files) < 20:
        print(f"! Записей всего {len(files)}. На малом наборе разброс между "
              "прогонами больше разницы между моделями — считайте результат "
              "ориентировочным.\n", file=sys.stderr)

    references: dict[str, str] = {}
    if args.reference:
        for text_file in args.reference.glob("*.txt"):
            references[text_file.stem] = text_file.read_text(encoding="utf-8").strip()
        print(f"Эталонных текстов: {len(references)}")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        response = requests.get(f"{SERVER}/api/models", headers=HEADERS,
                                params={"installed": "true"}, timeout=60)
        response.raise_for_status()
        models = [m["id"] for m in response.json().get("items", [])]
        print(f"Доступные модели: {', '.join(models) or '—'}")
    if not models:
        print("Нет установленных моделей.", file=sys.stderr)
        return 1

    summary = [run_model(model, files, references, not args.no_warmup)
               for model in models]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": f"{platform.system()} {platform.machine()}",
        "files": len(files),
        "results": summary,
    }
    out_json = HERE / f"{stamp}-{socket.gethostname()}.json"
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = ["| Модель | Записей | RTF ср. | RTF медиана | Уверенность | WER |",
            "|---|---|---|---|---|---|"]
    for item in summary:
        rows.append(
            f"| {item['model']} | {item['files']} | {item['rtf_avg'] or '—'} | "
            f"{item['rtf_median'] or '—'} | {item['confidence_avg'] or '—'} | "
            f"{item['wer_avg'] if item['wer_avg'] is not None else '—'} |")
    out_md = HERE / f"{stamp}-{socket.gethostname()}.md"
    out_md.write_text("\n".join(rows) + "\n", encoding="utf-8")

    print("\n" + "\n".join(rows))
    print(f"\nСохранено: {out_json.name}, {out_md.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
