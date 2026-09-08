"""Аналитика: сводные показатели, ряды по времени, сравнение моделей, разбор ошибок.

Все расчёты выполняются на стороне базы либо на выборках ограниченного
размера, чтобы страница аналитики оставалась быстрой даже при сотнях
тысяч заданий.
"""
from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
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
        #: Кеш выборок заданий на время одного сводного отчёта. Вне отчёта —
        #: None, то есть каждый разрез читает базу сам, как и раньше.
        self._выборки: dict[tuple[Any, ...], list[dict[str, Any]]] | None = None
        #: Границы окон, замороженные на время отчёта (см. `_since`).
        self._окна: dict[str, float] | None = None

    def _since(self, period: str) -> float:
        """Начало окна разреза; внутри отчёта — общее для всех разрезов.

        Каждый разрез считал границу сам, от текущего времени. За те
        секунды, что собирается отчёт, «месяц» у последнего разреза
        начинался позже, чем у первого: разрезы одного отчёта расходились
        между собой, а общая выборка заданий не попадала в кеш ни разу —
        ключ у каждого получался свой.
        """
        if self._окна is None:
            return _since(period)
        if period not in self._окна:
            self._окна[period] = _since(period)
        return self._окна[period]

    def _jobs(self, *, since: float | None = None,
              status: str | None = None, owner: str | None = None,
              limit: int = 100000) -> list[dict[str, Any]]:
        """Выборка заданий для разреза — с общим кешом внутри отчёта.

        Сводный отчёт собирает два десятка разрезов, и каждый читал базу
        сам: на архиве в сто тысяч заданий это два десятка полных проходов
        по одному и тому же окну ради одних и тех же строк. Ключ кеша —
        сам запрос, так что разрезы по завершённым и по упавшим остаются
        отдельными выборками, а не сводятся к одной «на все случаи».

        Строки только читают: ни один разрез не пишет в них, поэтому общий
        список можно отдавать всем сразу.
        """
        ключ = (since, status, owner, limit)
        готовое = self._выборки.get(ключ) if self._выборки is not None else None
        if готовое is not None:
            return готовое
        строки = self.db.list_jobs(since=since, status=status, limit=limit,
                                   owner=owner, light=True)
        if self._выборки is not None:
            self._выборки[ключ] = строки
        return строки

    @contextmanager
    def _общая_выборка(self) -> Iterator[None]:
        """Включает кеш на время сборки одного отчёта и гасит его после.

        Вложенность допустима: внутренний вызов не сбрасывает чужой кеш.
        Держать его дольше отчёта нельзя — аналитика живёт в приложении
        всё время его работы, и кеш стал бы просто устаревшими данными.
        """
        свой = self._выборки is None
        if свой:
            self._выборки = {}
            self._окна = {}
        try:
            yield
        finally:
            if свой:
                self._выборки = None
                self._окна = None

    # --- сводка ---------------------------------------------------------

    def overview(self, period: str = "day", owner: str | None = None) -> dict[str, Any]:
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
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
        since = self._since(period) or (time.time() - PERIODS["week"])
        span = max(time.time() - since, 60.0)
        width = span / buckets
        jobs = self._jobs(since=since, limit=100000, owner=owner)

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
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
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

    # Владелец передаётся во все срезы без исключения. В двух он терялся, и
    # обычный ключ видел в разделах «языки» и «движки» сводку по всему
    # серверу: и чужие числа, и чужие названия моделей.
    def by_language(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "language", "Язык не определён", owner)

    def by_owner(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "owner", "аноним", owner)

    def by_engine(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "engine", "—", owner)

    def by_source(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        return self._group(period, "source", "api", owner)

    def _group(self, period: str, field: str, fallback: str,
               owner: str | None = None) -> list[dict[str, Any]]:
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
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
        since = self._since(period)
        jobs = self._jobs(status="failed", since=since or None, limit=10000, owner=owner)
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
        # Числитель и знаменатель обязаны считаться по одному множеству.
        # Раньше отказы брались по владельцу, а всего заданий — по всему
        # серверу, и доля выходила заниженной в разы. Плюс len(jobs) упирался
        # в limit выборки, поэтому на большом периоде отказы переставали
        # расти после десяти тысяч.
        total_jobs = self.db.count_jobs(since=since or None, owner=owner)
        total_failed = self.db.count_jobs(status="failed", since=since or None, owner=owner)
        return {
            "total_failed": total_failed,
            "total_jobs": total_jobs,
            "failure_rate": round(total_failed / total_jobs, 4) if total_jobs else 0.0,
            "by_code": rows,
        }

    def duration_histogram(self, period: str = "month", bins: int = 10,
                          owner: str | None = None) -> dict[str, Any]:
        since = self._since(period)
        jobs = [j for j in self._jobs(status="completed", since=since or None, limit=100000, owner=owner)
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
        since = self._since(period)
        jobs = self._jobs(status="completed", since=since or None, limit=100000, owner=owner)
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
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
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

    @staticmethod
    def _measured_rtf(jobs: list[dict[str, Any]]) -> float:
        """Во сколько раз обработка медленнее реального времени.

        Считается по заданиям, которые сервер действительно считал сам:
        у взятого из кеша собственного времени обработки нет, и включать
        его в среднее — значит занижать цену работы тем сильнее, чем чаще
        срабатывает кеш.

        Раньше экономию от кеша в одном разрезе считали по этой измеренной
        величине, а в другом — по вбитой в код 0.2, и на одной странице
        стояли два числа про одно и то же, различавшиеся вдвое.
        """
        свои = [j for j in jobs
                if j.get("status") == "completed" and not j.get("cached_from")]
        звук = sum(float(j.get("media_duration_s") or 0) for j in свои)
        время = sum(float(j.get("processing_time_s") or 0) for j in свои)
        return (время / звук) if звук else 0.0

    def efficiency(self, period: str = "month", owner: str | None = None) -> dict[str, Any]:
        """Оценка эффективности: сколько ресурсов уходит на час аудио."""
        since = self._since(period)
        jobs = self._jobs(status="completed", since=since or None, limit=100000, owner=owner)
        audio = sum(float(j.get("media_duration_s") or 0) for j in jobs)
        proc = sum(float(j.get("processing_time_s") or 0) for j in jobs)
        load = sum(float(j.get("model_load_s") or 0) for j in jobs)
        из_кеша = [j for j in jobs if j.get("cached_from")]
        rtf = self._measured_rtf(jobs)
        return {
            "audio_hours": round(audio / 3600, 2),
            "compute_hours": round(proc / 3600, 2),
            # Здесь кеш учитывается: это фактическая цена часа выданного
            # звука, и снижать её — ровно то, ради чего кеш существует.
            "compute_per_audio_hour": round(proc / audio, 3) if audio else None,
            "model_load_share": round(load / proc, 4) if proc else None,
            "model_load_seconds": round(load, 1),
            "cache_hits": len(из_кеша),
            "cache_hit_rate": round(len(из_кеша) / len(jobs), 4) if jobs else 0.0,
            # А здесь — нет: экономия считается по цене работы, которую
            # пришлось бы выполнить, то есть по скорости своих заданий.
            "assumed_rtf": round(rtf, 4) if rtf else None,
            "saved_compute_hours": round(
                sum(float(j.get("media_duration_s") or 0) for j in из_кеша)
                * rtf / 3600, 3),
        }

    # --- сводный отчёт -------------------------------------------------------

    # --- нагрузка по дням недели ------------------------------------------

    def weekly_heatmap(self, period: str = "month",
                       owner: str | None = None) -> dict[str, Any]:
        """Карта нагрузки «день недели × час».

        Суточный профиль усредняет будни с выходными и потому отвечает не на
        тот вопрос. Планировать обслуживание, окна обновления и запас
        мощности приходится по неделе: у телефонии понедельник в десять утра
        и воскресенье в десять вечера — это разные миры.
        """
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
        счёт = [[0] * 24 for _ in range(7)]
        часы = [[0.0] * 24 for _ in range(7)]
        for job in jobs:
            ts = float(job.get("created_at") or 0)
            if not ts:
                continue
            стамп = time.localtime(ts)
            день = стамп.tm_wday
            час = стамп.tm_hour
            счёт[день][час] += 1
            часы[день][час] += float(job.get("media_duration_s") or 0) / 3600

        плоско = [v for строка in счёт for v in строка]
        пик = max(плоско) if плоско else 0
        индекс = плоско.index(пик) if пик else -1
        return {
            "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "hours": list(range(24)),
            "jobs": счёт,
            "audio_hours": [[round(v, 3) for v in строка] for строка in часы],
            "peak": {
                "jobs": пик,
                "day": индекс // 24 if индекс >= 0 else None,
                "hour": индекс % 24 if индекс >= 0 else None,
            },
            "total": sum(плоско),
        }

    # --- экономия на повторах ---------------------------------------------

    def cache_savings(self, period: str = "month",
                      owner: str | None = None) -> dict[str, Any]:
        """Сколько работы сняли повторы одного и того же файла.

        Сервер узнаёт уже виденный файл по хешу и отдаёт готовый результат.
        Пока это видно только счётчиком «из кеша», а вопрос у владельца
        сервера другой: сколько часов звука и машинного времени это
        сэкономило — то есть стоило ли оно того.
        """
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
        из_кеша = [j for j in jobs if j.get("cached_from")]
        свои = [j for j in jobs if not j.get("cached_from") and j["status"] == "completed"]

        # Стоимость повтора считаем по средней скорости своих заданий: у
        # взятого из кеша задания собственного времени обработки нет.
        звук_кеш = sum(float(j.get("media_duration_s") or 0) for j in из_кеша)
        rtf = self._measured_rtf(jobs)

        # Самые частые повторы: по ним видно, не шлёт ли клиент одно и то же
        # по кругу из-за ошибки в своей очереди.
        повторы: dict[str, dict[str, Any]] = {}
        for job in из_кеша:
            ключ = str(job.get("file_hash") or job.get("cached_from") or "")
            if not ключ:
                continue
            запись = повторы.setdefault(ключ, {"hits": 0, "filename": job.get("filename") or "",
                                               "audio_hours": 0.0})
            запись["hits"] += 1
            запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
        топ = sorted(повторы.values(), key=lambda r: r["hits"], reverse=True)[:10]
        for запись in топ:
            запись["audio_hours"] = round(запись["audio_hours"], 3)

        return {
            "period": period,
            "hits": len(из_кеша),
            "misses": len(свои),
            "hit_rate": round(len(из_кеша) / len(jobs), 4) if jobs else None,
            "audio_hours_saved": round(звук_кеш / 3600, 3),
            "processing_seconds_saved": round(звук_кеш * rtf, 1),
            "assumed_rtf": round(rtf, 4) if rtf else None,
            "repeats": топ,
        }

    # --- надёжность --------------------------------------------------------

    def reliability(self, period: str = "month",
                    owner: str | None = None) -> dict[str, Any]:
        """Что происходит с заданием между приёмом и выдачей результата.

        Доля успеха отвечает «сколько дошло», но не «какой ценой». Задание,
        прошедшее с третьей попытки, считается успешным наравне с тем, что
        прошло с первой, — а это разные состояния сервера.
        """
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
        готово = [j for j in jobs if j["status"] == "completed"]
        с_повтором = [j for j in готово if int(j.get("retries") or 0) > 0]
        отменены = [j for j in jobs if j["status"] == "cancelled"]

        кем: dict[str, int] = {}
        for job in отменены:
            кем[str(job.get("cancelled_by") or "неизвестно")] = \
                кем.get(str(job.get("cancelled_by") or "неизвестно"), 0) + 1

        вебхуки: dict[str, int] = {}
        for job in jobs:
            статус = str(job.get("webhook_status") or "")
            if статус:
                вебхуки[статус] = вебхуки.get(статус, 0) + 1

        распределение: dict[int, int] = {}
        for job in jobs:
            n = int(job.get("retries") or 0)
            распределение[n] = распределение.get(n, 0) + 1

        return {
            "period": period,
            "total": len(jobs),
            "first_attempt_success": len(готово) - len(с_повтором),
            "first_attempt_rate": round((len(готово) - len(с_повтором)) / len(jobs), 4)
                                  if jobs else None,
            # Названия здесь важнее обычного: «повторялось: 0» рядом с
            # «повторов всего: 7» читается как противоречие, хотя это разные
            # вопросы — сколько заданий дошло со второй попытки и сколько
            # повторов было вообще (включая те, что так и не дошли).
            "completed_after_retry": len(с_повтором),
            "jobs_with_retries": sum(1 for j in jobs if int(j.get("retries") or 0) > 0),
            "retry_total": sum(int(j.get("retries") or 0) for j in jobs),
            "retry_distribution": [{"retries": k, "jobs": v}
                                   for k, v in sorted(распределение.items())],
            "cancelled": len(отменены),
            "cancelled_by": [{"who": k, "jobs": v}
                             for k, v in sorted(кем.items(), key=lambda kv: -kv[1])],
            "webhooks": [{"status": k, "jobs": v}
                         for k, v in sorted(вебхуки.items(), key=lambda kv: -kv[1])],
        }

    # --- каким бывает звук --------------------------------------------------

    def audio_profile(self, period: str = "month",
                      owner: str | None = None) -> dict[str, Any]:
        """Свойства самих записей: формат, битрейт, темп речи, говорящие.

        Это разрез не про сервер, а про материал. Темп речи и доля тишины
        объясняют, почему одна и та же модель на одном потоке работает
        вдвое медленнее, чем на другом, — и стоит ли менять модель или
        источник записи.
        """
        since = self._since(period)
        jobs = [j for j in self._jobs(since=since or None, limit=100000,
                                   owner=owner)
                if j["status"] == "completed"]

        форматы: dict[str, dict[str, float]] = {}
        темпы: list[float] = []
        битрейты: list[float] = []
        говорящие: dict[int, int] = {}
        плотность: list[float] = []

        for job in jobs:
            имя = str(job.get("filename") or "")
            расш = имя.rsplit(".", 1)[-1].lower() if "." in имя else "без расширения"
            запись = форматы.setdefault(расш, {"jobs": 0, "audio_hours": 0.0, "bytes": 0.0})
            запись["jobs"] += 1
            запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
            запись["bytes"] += float(job.get("file_size") or 0)

            длительность = float(job.get("media_duration_s") or 0)
            слова = int(job.get("words_count") or 0)
            if длительность > 1 and слова:
                темпы.append(слова / (длительность / 60))
            размер = float(job.get("file_size") or 0)
            if длительность > 1 and размер:
                битрейты.append(размер * 8 / длительность / 1000)
            n = int(job.get("speakers_count") or 0)
            if n:
                говорящие[n] = говорящие.get(n, 0) + 1
            # Сегментов на минуту записи: по этому числу видно, что за
            # материал приносят. Диктовка даёт единицы, живой диалог с
            # перебиваниями — десятки. Это не доля времени под речью:
            # длину сегментов сервер в разрезе не хранит.
            сегменты = float(job.get("segments_count") or 0)
            if длительность > 1 and сегменты:
                плотность.append(сегменты / (длительность / 60))

        строки = []
        for имя, данные in sorted(форматы.items(), key=lambda kv: -kv[1]["jobs"]):
            строки.append({
                "format": имя,
                "jobs": int(данные["jobs"]),
                "audio_hours": round(данные["audio_hours"], 2),
                "avg_mb": round(данные["bytes"] / данные["jobs"] / 1024 / 1024, 2)
                          if данные["jobs"] else 0,
            })

        return {
            "period": period,
            "formats": строки,
            "speech_rate_wpm": M.summarize(темпы),
            "bitrate_kbps": M.summarize(битрейты),
            "segments_per_minute": M.summarize(плотность),
            "speakers": [{"speakers": k, "jobs": v} for k, v in sorted(говорящие.items())],
        }

    # --- расход ресурсов ----------------------------------------------------

    def resources(self, period: str = "month",
                  owner: str | None = None) -> dict[str, Any]:
        """Память и устройства в разрезе моделей.

        Отвечает на вопрос, который задают перед покупкой второй карты:
        какая модель сколько памяти просит на пике и сколько заданий вообще
        уехало на процессор вместо видеокарты.
        """
        since = self._since(period)
        jobs = [j for j in self._jobs(since=since or None, limit=100000,
                                   owner=owner)
                if j["status"] == "completed"]

        по_модели: dict[str, list[float]] = {}
        устройства: dict[str, dict[str, float]] = {}
        for job in jobs:
            память = float(job.get("peak_memory_mb") or 0)
            if память:
                по_модели.setdefault(str(job.get("model") or "—"), []).append(память)
            dev = str(job.get("device") or "неизвестно")
            запись = устройства.setdefault(dev, {"jobs": 0, "audio_hours": 0.0,
                                                 "processing_s": 0.0})
            запись["jobs"] += 1
            запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
            запись["processing_s"] += float(job.get("processing_time_s") or 0)

        модели = []
        for имя, значения in sorted(по_модели.items(),
                                    key=lambda kv: -max(kv[1])):
            сводка = M.summarize(значения)
            модели.append({"model": имя, "jobs": len(значения),
                           "peak_mb": сводка["max"], "avg_mb": сводка["avg"],
                           "p95_mb": сводка["p95"]})

        разрез = []
        for имя, данные in sorted(устройства.items(), key=lambda kv: -kv[1]["jobs"]):
            звук = данные["audio_hours"]
            разрез.append({
                "device": имя,
                "jobs": int(данные["jobs"]),
                "audio_hours": round(звук, 2),
                "rtf": round(данные["processing_s"] / (звук * 3600), 4) if звук else None,
            })

        return {"period": period, "models": модели, "devices": разрез}

    # --- качество во времени ------------------------------------------------

    def quality_trend(self, period: str = "month", buckets: int = 24,
                      owner: str | None = None) -> dict[str, Any]:
        """Уверенность и WER по времени.

        Средняя уверенность за месяц — число, которое ничего не сообщает.
        Полезен ход: провал в среду означает, что в среду что-то поменялось
        — источник записей, модель или параметры.
        """
        since = self._since(period)
        jobs = [j for j in self._jobs(since=since or None, limit=100000,
                                   owner=owner)
                if j["status"] == "completed"]
        if not jobs:
            return {"period": period, "buckets": [], "bucket_seconds": 0,
                    "confidence": [], "wer": [], "jobs": [],
                    "low_confidence_share": []}

        начало = since or min(float(j["created_at"]) for j in jobs)
        конец = time.time()
        шаг = max(1.0, (конец - начало) / buckets)

        корзины: list[dict[str, list[float]]] = [
            {"conf": [], "wer": [], "low": []} for _ in range(buckets)]
        for job in jobs:
            i = int((float(job["created_at"]) - начало) / шаг)
            i = min(max(i, 0), buckets - 1)
            c = job.get("avg_confidence")
            if c is not None:
                корзины[i]["conf"].append(float(c))
                корзины[i]["low"].append(1.0 if float(c) < 0.75 else 0.0)
            w = job.get("wer")
            if w is not None:
                корзины[i]["wer"].append(float(w))

        среднее = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
        return {
            "period": period,
            "buckets": [round(начало + i * шаг) for i in range(buckets)],
            # Ширина корзины — чтобы подписать ось. За час она в минутах, за
            # год — в датах; без неё все подписи печатались как «день.месяц»
            # и на часовом окне читались как одна и та же дата двадцать
            # четыре раза подряд.
            "bucket_seconds": round(шаг),
            "confidence": [среднее(k["conf"]) for k in корзины],
            "wer": [среднее(k["wer"]) for k in корзины],
            "low_confidence_share": [среднее(k["low"]) for k in корзины],
            "jobs": [len(k["conf"]) for k in корзины],
        }

    # --- разрез по меткам ---------------------------------------------------

    def by_tag(self, period: str = "month", owner: str | None = None) -> list[dict[str, Any]]:
        """Метки — единственный разрез, который задаёт сам пользователь.

        Всё остальное сервер знает про себя: модель, движок, источник. А
        метка отвечает на вопрос «сколько ушло на этот проект» — и потому
        для отчётности она важнее прочих разрезов.
        """
        since = self._since(period)
        jobs = self._jobs(since=since or None, limit=100000, owner=owner)
        собрано: dict[str, dict[str, Any]] = {}
        for job in jobs:
            метки = [t.strip() for t in str(job.get("tags") or "").split(",") if t.strip()]
            for метка in метки or ["без метки"]:
                запись = собрано.setdefault(метка, {
                    "tag": метка, "jobs": 0, "completed": 0, "failed": 0,
                    "audio_hours": 0.0, "processing_s": 0.0, "words": 0})
                запись["jobs"] += 1
                if job["status"] == "completed":
                    запись["completed"] += 1
                    запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
                    запись["processing_s"] += float(job.get("processing_time_s") or 0)
                    запись["words"] += int(job.get("words_count") or 0)
                elif job["status"] == "failed":
                    запись["failed"] += 1

        строки = []
        for запись in собрано.values():
            звук = запись["audio_hours"]
            строки.append({
                **запись,
                "audio_hours": round(звук, 2),
                "processing_s": round(запись["processing_s"], 1),
                "rtf": round(запись["processing_s"] / (звук * 3600), 4) if звук else None,
            })
        return sorted(строки, key=lambda r: -r["jobs"])

    # --- ожидание в очереди -------------------------------------------------

    def queue_latency(self, period: str = "month",
                      owner: str | None = None) -> dict[str, Any]:
        """Сколько задание ждало и от чего это зависело.

        Среднее ожидание скрывает именно то, на что жалуются: хвост. Ждали
        три секунды девятьсот заданий и сорок минут — десять, и среднее
        покажет «всё хорошо».
        """
        since = self._since(period)
        jobs = [j for j in self._jobs(since=since or None, limit=100000,
                                   owner=owner)
                if j.get("queue_time_s") is not None]

        по_приоритету: dict[str, list[float]] = {}
        по_источнику: dict[str, list[float]] = {}
        for job in jobs:
            ожидание = float(job.get("queue_time_s") or 0)
            приоритет = int(job.get("priority") or 50)
            группа = ("высокий (>50)" if приоритет > 50 else
                      "обычный (50)" if приоритет == 50 else "низкий (<50)")
            по_приоритету.setdefault(группа, []).append(ожидание)
            по_источнику.setdefault(str(job.get("source") or "api"), []).append(ожидание)

        разрез = lambda d: [  # noqa: E731
            {"name": k, "jobs": len(v), **M.summarize(v)}
            for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))]

        все = [float(j["queue_time_s"]) for j in jobs]
        return {
            "period": period,
            "overall": M.summarize(все),
            "by_priority": разрез(по_приоритету),
            "by_source": разрез(по_источнику),
            "waited_over_minute": sum(1 for v in все if v > 60),
            "waited_over_10_minutes": sum(1 for v in все if v > 600),
        }

    def full_report(self, period: str = "week", owner: str | None = None) -> dict[str, Any]:
        """Все разрезы сразу — по одной выборке заданий на каждый запрос.

        Отдельные разрезы забирают по своим адресам и читают базу сами;
        здесь их два десятка подряд, и общий кеш убирает два десятка
        одинаковых проходов по архиву.
        """
        with self._общая_выборка():
            return self._full_report(period, owner)

    def _full_report(self, period: str, owner: str | None) -> dict[str, Any]:
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
            "weekly": self.weekly_heatmap(period, owner),
            "cache": self.cache_savings(period, owner),
            "reliability": self.reliability(period, owner),
            "audio": self.audio_profile(period, owner),
            "resources": self.resources(period, owner),
            "quality_trend": self.quality_trend(period, owner=owner),
            "tags": self.by_tag(period, owner),
            "queue": self.queue_latency(period, owner),
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
