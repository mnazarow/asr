"""Аналитика: сводные показатели, ряды по времени, сравнение моделей, разбор ошибок.

Все расчёты выполняются на стороне базы либо на выборках ограниченного
размера, чтобы страница аналитики оставалась быстрой даже при сотнях
тысяч заданий.
"""
from __future__ import annotations

import time
from typing import Any

from .catalog import get_model
from .db import Database
from .logging_setup import get_logger
from .pipeline import metrics as M

log = get_logger("analytics")

PERIODS = {
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    "quarter": 7776000,
    "year": 31536000,
    "all": 0,
}


def _since(period: str) -> float:
    seconds = PERIODS.get(period, 86400)
    return 0.0 if seconds == 0 else time.time() - seconds


def _escape_label(value: Any) -> str:
    """Экранирует значение метки по правилам формата Prometheus."""
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Analytics:
    def __init__(self, db: Database):
        self.db = db

    # --- сводка ---------------------------------------------------------

    def overview(self, period: str = "day", owner: str | None = None) -> dict[str, Any]:
        since = _since(period)
        jobs = self.db.list_jobs(since=since or None, limit=100000, owner=owner, light=True)
        done = [j for j in jobs if j["status"] == "completed"]
        failed = [j for j in jobs if j["status"] == "failed"]
        cancelled = [j for j in jobs if j["status"] == "cancelled"]

        audio_s = sum(float(j.get("media_duration_s") or 0) for j in done)
        proc_s = sum(float(j.get("processing_time_s") or 0) for j in done)
        words = sum(int(j.get("words_count") or 0) for j in done)
        rtf_values = [float(j["rtf"]) for j in done if j.get("rtf")]
        queue_values = [float(j["queue_time_s"]) for j in done if j.get("queue_time_s")]
        conf_values = [float(j["avg_confidence"]) for j in done if j.get("avg_confidence")]
        wer_values = [float(j["wer"]) for j in done if j.get("wer") is not None]

        span_h = ((time.time() - since) / 3600) if since else max(
            1.0, (time.time() - min((float(j["created_at"]) for j in jobs), default=time.time()))
            / 3600)

        return {
            "period": period,
            "generated_at": time.time(),
            "jobs": {
                "total": len(jobs),
                "completed": len(done),
                "failed": len(failed),
                "cancelled": len(cancelled),
                "in_progress": sum(1 for j in jobs if j["status"] in ("queued", "running", "retry")),
                "cached": sum(1 for j in jobs if j.get("cached_from")),
                "success_rate": round(len(done) / len(jobs), 4) if jobs else None,
            },
            "volume": {
                "audio_seconds": round(audio_s, 1),
                "audio_hours": round(audio_s / 3600, 2),
                "processing_seconds": round(proc_s, 1),
                "words": words,
                "characters": sum(int(j.get("chars_count") or 0) for j in done),
                "segments": sum(int(j.get("segments_count") or 0) for j in done),
                "files_per_hour": round(len(done) / span_h, 2) if span_h else 0,
                "audio_hours_per_hour": round(audio_s / 3600 / span_h, 2) if span_h else 0,
            },
            "performance": {
                "rtf": M.summarize(rtf_values),
                "processing_time_s": M.summarize(
                    [float(j["processing_time_s"]) for j in done if j.get("processing_time_s")]),
                "queue_time_s": M.summarize(queue_values),
                "speedup": round(audio_s / proc_s, 2) if proc_s else None,
            },
            "quality": {
                "confidence": M.summarize(conf_values),
                "wer": M.summarize(wer_values) if wer_values else None,
                "low_confidence_jobs": sum(1 for c in conf_values if c < 0.75),
                "confidence_distribution": M.confidence_buckets(conf_values) if conf_values else [],
            },
            "stages": self._stage_breakdown(done),
        }

    def _stage_breakdown(self, jobs: list[dict[str, Any]]) -> dict[str, Any]:
        keys = {
            "audio_prep_s": "Подготовка аудио",
            "model_load_s": "Загрузка модели",
            "inference_s": "Распознавание",
            "postprocess_s": "Постобработка",
        }
        totals: dict[str, float] = {}
        for key in keys:
            totals[key] = sum(float(j.get(key) or 0) for j in jobs)
        grand = sum(totals.values()) or 1.0
        return {
            "labels": [keys[k] for k in keys],
            "seconds": [round(totals[k], 2) for k in keys],
            "share": [round(totals[k] / grand, 4) for k in keys],
            "total_seconds": round(grand, 2),
        }

    # --- ряды по времени -------------------------------------------------

    def timeseries(self, period: str = "day", buckets: int = 48,
                   owner: str | None = None) -> dict[str, Any]:
        since = _since(period) or (time.time() - PERIODS["week"])
        span = max(time.time() - since, 60.0)
        width = span / buckets
        jobs = self.db.list_jobs(since=since, limit=100000, owner=owner, light=True)

        series = {
            "labels": [], "completed": [], "failed": [], "audio_minutes": [],
            "rtf": [], "queue_time": [], "words": [],
        }
        grid: dict[int, dict[str, list[float] | int]] = {}
        for job in jobs:
            slot = int((float(job["created_at"]) - since) / width)
            slot = max(0, min(buckets - 1, slot))
            cell = grid.setdefault(slot, {"completed": 0, "failed": 0, "audio": 0.0,
                                          "rtf": [], "queue": [], "words": 0})
            if job["status"] == "completed":
                cell["completed"] = int(cell["completed"]) + 1        # type: ignore[assignment]
                cell["audio"] = float(cell["audio"]) + float(job.get("media_duration_s") or 0)
                cell["words"] = int(cell["words"]) + int(job.get("words_count") or 0)
                if job.get("rtf"):
                    cell["rtf"].append(float(job["rtf"]))             # type: ignore[union-attr]
                if job.get("queue_time_s"):
                    cell["queue"].append(float(job["queue_time_s"]))  # type: ignore[union-attr]
            elif job["status"] == "failed":
                cell["failed"] = int(cell["failed"]) + 1              # type: ignore[assignment]

        for slot in range(buckets):
            cell = grid.get(slot, {"completed": 0, "failed": 0, "audio": 0.0,
                                   "rtf": [], "queue": [], "words": 0})
            stamp = since + slot * width
            series["labels"].append(stamp)
            series["completed"].append(cell["completed"])
            series["failed"].append(cell["failed"])
            series["audio_minutes"].append(round(float(cell["audio"]) / 60, 2))
            rtf_list = cell["rtf"]                                     # type: ignore[assignment]
            queue_list = cell["queue"]                                 # type: ignore[assignment]
            series["rtf"].append(round(sum(rtf_list) / len(rtf_list), 4) if rtf_list else None)
            series["queue_time"].append(
                round(sum(queue_list) / len(queue_list), 2) if queue_list else None)
            series["words"].append(cell["words"])

        samples = self.db.system_samples(since, limit=2000)
        series["system"] = {
            "ts": [s["ts"] for s in samples],
            "cpu": [s.get("cpu_percent") for s in samples],
            "ram_used_mb": [s.get("ram_used_mb") for s in samples],
            "gpu": [s.get("gpu_percent") for s in samples],
            "gpu_mem_mb": [s.get("gpu_mem_mb") for s in samples],
            "queue_depth": [s.get("queue_depth") for s in samples],
            "active_jobs": [s.get("active_jobs") for s in samples],
        }
        series["bucket_seconds"] = round(width, 1)
        series["since"] = since
        return series

    # --- сравнение моделей -------------------------------------------------

    def by_model(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        since = _since(period)
        jobs = self.db.list_jobs(since=since or None, limit=100000, owner=owner, light=True)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            grouped.setdefault(str(job.get("model") or "—"), []).append(job)

        rows: list[dict[str, Any]] = []
        for model, items in grouped.items():
            done = [j for j in items if j["status"] == "completed"]
            rtf = [float(j["rtf"]) for j in done if j.get("rtf")]
            conf = [float(j["avg_confidence"]) for j in done if j.get("avg_confidence")]
            wer = [float(j["wer"]) for j in done if j.get("wer") is not None]
            audio = sum(float(j.get("media_duration_s") or 0) for j in done)
            proc = sum(float(j.get("processing_time_s") or 0) for j in done)
            spec = get_model(model)
            rows.append({
                "model": model,
                "name": spec.name if spec else model,
                "family": spec.family if spec else "",
                "engine": items[0].get("engine") if items else "",
                "license": spec.license if spec else "",
                "jobs": len(items),
                "completed": len(done),
                "failed": sum(1 for j in items if j["status"] == "failed"),
                "success_rate": round(len(done) / len(items), 4) if items else None,
                "audio_hours": round(audio / 3600, 3),
                "processing_hours": round(proc / 3600, 3),
                "speedup": round(audio / proc, 2) if proc else None,
                "rtf_avg": round(sum(rtf) / len(rtf), 4) if rtf else None,
                "rtf_p90": round(M.percentile(rtf, 0.9), 4) if rtf else None,
                "confidence_avg": round(sum(conf) / len(conf), 4) if conf else None,
                "wer_avg": round(sum(wer) / len(wer), 4) if wer else None,
                "words": sum(int(j.get("words_count") or 0) for j in done),
                "avg_duration_s": round(audio / len(done), 1) if done else None,
                "catalog_ru_wer": spec.best_ru_wer if spec else None,
                "catalog_rtfx": spec.rtfx if spec else None,
            })
        rows.sort(key=lambda r: r["jobs"], reverse=True)
        return rows

    # --- прочие срезы -------------------------------------------------------

    def by_language(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "language", "Язык не определён")

    def by_owner(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "owner", "аноним", owner)

    def by_engine(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "engine", "—")

    def by_source(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "source", "api", owner)

    def _group(self, period: str, field: str, fallback: str,
               owner: str | None = None) -> list[dict[str, Any]]:
        since = _since(period)
        jobs = self.db.list_jobs(since=since or None, limit=100000, owner=owner, light=True)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            grouped.setdefault(str(job.get(field) or fallback), []).append(job)
        rows = []
        for key, items in grouped.items():
            done = [j for j in items if j["status"] == "completed"]
            rtf = [float(j["rtf"]) for j in done if j.get("rtf")]
            rows.append({
                "key": key,
                "jobs": len(items),
                "completed": len(done),
                "failed": sum(1 for j in items if j["status"] == "failed"),
                "audio_hours": round(
                    sum(float(j.get("media_duration_s") or 0) for j in done) / 3600, 3),
                "words": sum(int(j.get("words_count") or 0) for j in done),
                "rtf_avg": round(sum(rtf) / len(rtf), 4) if rtf else None,
            })
        rows.sort(key=lambda r: r["jobs"], reverse=True)
        return rows

    def errors(self, period: str = "month", owner: str | None = None) -> dict[str, Any]:
        since = _since(period)
        jobs = self.db.list_jobs(status="failed", since=since or None, limit=10000, owner=owner, light=True)
        by_code: dict[str, dict[str, Any]] = {}
        for job in jobs:
            code = str(job.get("error_code") or "unknown")
            entry = by_code.setdefault(code, {
                "code": code, "count": 0, "message": job.get("error_message") or "",
                "hint": job.get("error_hint") or "", "models": {}, "examples": []})
            entry["count"] += 1
            model = str(job.get("model") or "—")
            entry["models"][model] = entry["models"].get(model, 0) + 1
            if len(entry["examples"]) < 5:
                entry["examples"].append({
                    "id": job["id"], "filename": job.get("filename"),
                    "created_at": job.get("created_at")})
        rows = sorted(by_code.values(), key=lambda r: r["count"], reverse=True)
        total_jobs = self.db.count_jobs(since=since or None)
        return {
            "total_failed": len(jobs),
            "total_jobs": total_jobs,
            "failure_rate": round(len(jobs) / total_jobs, 4) if total_jobs else 0.0,
            "by_code": rows,
        }

    def duration_histogram(self, period: str = "month", bins: int = 10,
                          owner: str | None = None) -> dict[str, Any]:
        since = _since(period)
        jobs = [j for j in self.db.list_jobs(status="completed", since=since or None, limit=100000, owner=owner, light=True)
                if j.get("media_duration_s")]
        durations = [float(j["media_duration_s"]) for j in jobs]
        if not durations:
            return {"edges": [], "counts": [], "labels": []}
        edges = [0, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 1e9]
        labels = ["<30 с", "30–60 с", "1–2 мин", "2–5 мин", "5–10 мин", "10–20 мин",
                  "20–30 мин", "30–60 мин", "1–2 ч", ">2 ч"]
        counts = [0] * len(labels)
        for value in durations:
            for idx in range(len(labels)):
                if edges[idx] <= value < edges[idx + 1]:
                    counts[idx] += 1
                    break
        return {"labels": labels, "counts": counts,
                "total": len(durations), "summary": M.summarize(durations)}

    def slowest(self, period: str = "month", limit: int = 15,
                owner: str | None = None) -> list[dict[str, Any]]:
        since = _since(period)
        jobs = self.db.list_jobs(status="completed", since=since or None, limit=100000, owner=owner, light=True)
        jobs = [j for j in jobs if j.get("rtf")]
        jobs.sort(key=lambda j: float(j["rtf"]), reverse=True)
        return [{
            "id": j["id"], "filename": j.get("filename"), "model": j.get("model"),
            "rtf": j.get("rtf"), "duration_s": j.get("media_duration_s"),
            "processing_time_s": j.get("processing_time_s"),
            "created_at": j.get("created_at"),
        } for j in jobs[:limit]]

    def hourly_profile(self, period: str = "month", owner: str | None = None) -> dict[str, Any]:
        """Распределение нагрузки по часам суток и дням недели."""
        since = _since(period)
        jobs = self.db.list_jobs(since=since or None, limit=100000, owner=owner, light=True)
        hours = [0] * 24
        weekdays = [0] * 7
        for job in jobs:
            local = time.localtime(float(job["created_at"]))
            hours[local.tm_hour] += 1
            weekdays[local.tm_wday] += 1
        return {
            "hours": hours,
            "hour_labels": [f"{h:02d}" for h in range(24)],
            "weekdays": weekdays,
            "weekday_labels": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "peak_hour": max(range(24), key=lambda h: hours[h]) if any(hours) else None,
        }

    def efficiency(self, period: str = "month", owner: str | None = None) -> dict[str, Any]:
        """Оценка эффективности: сколько ресурсов уходит на час аудио."""
        since = _since(period)
        jobs = self.db.list_jobs(status="completed", since=since or None, limit=100000, owner=owner, light=True)
        audio = sum(float(j.get("media_duration_s") or 0) for j in jobs)
        proc = sum(float(j.get("processing_time_s") or 0) for j in jobs)
        load = sum(float(j.get("model_load_s") or 0) for j in jobs)
        cached = sum(1 for j in jobs if j.get("cached_from"))
        return {
            "audio_hours": round(audio / 3600, 2),
            "compute_hours": round(proc / 3600, 2),
            "compute_per_audio_hour": round(proc / audio, 3) if audio else None,
            "model_load_share": round(load / proc, 4) if proc else None,
            "model_load_seconds": round(load, 1),
            "cache_hits": cached,
            "cache_hit_rate": round(cached / len(jobs), 4) if jobs else 0.0,
            "saved_compute_hours": round(
                sum(float(j.get("media_duration_s") or 0) for j in jobs if j.get("cached_from"))
                * 0.2 / 3600, 3),
        }

    # --- сводный отчёт -------------------------------------------------------

    def full_report(self, period: str = "week", owner: str | None = None) -> dict[str, Any]:
        return {
            "overview": self.overview(period, owner),
            "timeseries": self.timeseries(period, owner=owner),
            "models": self.by_model(period, owner),
            "languages": self.by_language(period, owner),
            "owners": self.by_owner(period, owner),
            "engines": self.by_engine(period, owner),
            "errors": self.errors(period, owner),
            "durations": self.duration_histogram(period, owner=owner),
            "slowest": self.slowest(period, owner=owner),
            "profile": self.hourly_profile(period, owner),
            "efficiency": self.efficiency(period, owner),
        }

    # --- экспорт метрик Prometheus --------------------------------------------

    def prometheus(self) -> str:
        lines: list[str] = []

        def add(name: str, value: Any, labels: str = "", help_text: str = "",
                kind: str = "gauge") -> None:
            if value is None:
                return
            if help_text:
                lines.append(f"# HELP asrhub_{name} {help_text}")
                lines.append(f"# TYPE asrhub_{name} {kind}")
            suffix = "{" + labels + "}" if labels else ""
            lines.append(f"asrhub_{name}{suffix} {value}")

        counts = {status: self.db.count_jobs(status=status)
                  for status in ("queued", "running", "completed", "failed",
                                 "cancelled", "retry")}
        for status, value in counts.items():
            add("jobs_total", value, f'status="{status}"',
                "Число заданий по статусам" if status == "queued" else "", "gauge")

        overview = self.overview("day")
        add("audio_seconds_total", overview["volume"]["audio_seconds"], "",
            "Обработано аудио за сутки, секунд", "counter")
        add("words_total", overview["volume"]["words"], "",
            "Распознано слов за сутки", "counter")
        add("rtf_avg", overview["performance"]["rtf"]["avg"], "",
            "Средний коэффициент реального времени")
        add("rtf_p95", overview["performance"]["rtf"]["p95"])
        add("queue_time_p95", overview["performance"]["queue_time_s"]["p95"], "",
            "95-й перцентиль ожидания в очереди, секунд")
        add("confidence_avg", overview["quality"]["confidence"]["avg"], "",
            "Средняя уверенность распознавания")
        add("success_rate", overview["jobs"]["success_rate"], "",
            "Доля успешно завершённых заданий")

        for row in self.by_model("day"):
            # Значение метки экранируется: имя модели приходит из запроса и
            # без этого позволяло бы вписать в вывод поддельные метрики.
            label = 'model="{}"'.format(_escape_label(row["model"]))
            add("model_jobs", row["jobs"], label)
            add("model_rtf_avg", row["rtf_avg"], label)
            add("model_success_rate", row["success_rate"], label)

        samples = self.db.system_samples(time.time() - 300, limit=200)
        if samples:
            sample = samples[-1]
            add("cpu_percent", sample.get("cpu_percent"))
            add("ram_used_mb", sample.get("ram_used_mb"))
            add("gpu_percent", sample.get("gpu_percent"))
            add("gpu_memory_mb", sample.get("gpu_mem_mb"))
            add("disk_free_gb", sample.get("disk_free_gb"))
        return "\n".join(lines) + "\n"
