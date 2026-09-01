"""Сбор всех параметров работы сервиса в одну плоскую таблицу измерений.

Собранный снимок — это список `Sample`: имя метрики, набор меток и значение.
Из него получаются все форматы выгрузки, поэтому логика сбора написана один
раз, а не по разу на каждую систему мониторинга.

Сбор рассчитан на то, что его дёргают часто (Prometheus по умолчанию раз в
15 секунд), поэтому:
* дорогие замеры — размер каталогов, обход базы — кешируются на заданное время;
* любой сбойный источник пропускается, а не роняет весь снимок: неработающий
  nvidia-smi не должен лишать вас метрик очереди;
* счётчики живут в памяти процесса и обнуляются при перезапуске — так принято
  и правильно обрабатывается функцией rate() на стороне Prometheus.
"""
from __future__ import annotations

import os
import platform
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..db import SCHEMA_VERSION
from ..pipeline import metrics as M
from .catalog import METRICS_BY_NAME

# Границы гистограмм. Подобраны под реальные величины: задания от секунд до
# часов, запросы к API — от миллисекунд до минуты загрузки файла.
JOB_DURATION_BUCKETS = (1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600, 7200)
MEDIA_DURATION_BUCKETS = (10, 30, 60, 120, 300, 600, 1800, 3600, 7200, 14400)
HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60)


@dataclass
class Sample:
    """Одно измерение: имя, метки, значение."""

    name: str
    value: float
    labels: dict[str, str] = field(default_factory=dict)


@dataclass
class Histogram:
    """Гистограмма: границы, счётчики по корзинам, сумма и общее число."""

    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: int = 0
    sum: float = 0.0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        """Учитывает измерение. Счётчики хранятся по корзинам, не накопленные."""
        self.total += 1
        self.sum += value
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1
                break                       # значение попадает ровно в одну корзину

    def cumulative(self) -> list[tuple[float, int]]:
        """Накопленные счётчики: Prometheus ждёт «не больше le», а не «в корзине»."""
        running = 0
        result = []
        for edge, count in zip(self.buckets, self.counts, strict=True):
            running += count
            result.append((edge, running))
        return result


class Runtime:
    """Счётчики и гистограммы, которые копятся в памяти процесса.

    Всё, что нельзя посчитать запросом к базе: запросы к API, загрузки моделей,
    отказы аутентификации. Класс потокобезопасен — его дёргают из обработчиков
    запросов и из воркеров очереди одновременно.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.counters: Counter[tuple[str, tuple[tuple[str, str], ...]]] = Counter()
        self.histograms: dict[tuple[str, tuple[tuple[str, str], ...]], Histogram] = {}
        self.gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self.in_flight = 0
        self.websocket_clients = 0
        self.last_error_ts = 0.0

    @staticmethod
    def _key(name: str, labels: dict[str, str] | None) -> tuple[str, tuple[tuple[str, str], ...]]:
        return name, tuple(sorted((labels or {}).items()))

    def inc(self, name: str, labels: dict[str, str] | None = None, value: float = 1) -> None:
        with self._lock:
            self.counters[self._key(name, labels)] += value

    def set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            self.gauges[self._key(name, labels)] = value

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None,
                buckets: tuple[float, ...] = HTTP_BUCKETS) -> None:
        key = self._key(name, labels)
        with self._lock:
            hist = self.histograms.get(key)
            if hist is None:
                hist = self.histograms[key] = Histogram(buckets)
            hist.observe(value)

    def request_started(self) -> None:
        with self._lock:
            self.in_flight += 1

    def request_finished(self) -> None:
        with self._lock:
            self.in_flight = max(0, self.in_flight - 1)

    def note_error(self, code: str, retryable: bool) -> None:
        self.inc("asrhub_errors_total", {"code": code, "retryable": str(retryable).lower()})
        with self._lock:
            self.last_error_ts = time.time()

    def snapshot(self) -> tuple[dict, dict, dict]:
        with self._lock:
            return dict(self.counters), dict(self.gauges), dict(self.histograms)


RUNTIME = Runtime()



def _file_size(path: Any) -> int:
    """Размер файла в байтах; ноль, если файла нет или он недоступен."""
    if not path:
        return 0
    try:
        return os.path.getsize(str(path))
    except OSError:
        return 0

class Collector:
    """Собирает снимок всех параметров работы сервиса."""

    def __init__(self, state: Any, runtime: Runtime | None = None,
                 expensive_interval_s: float = 300.0) -> None:
        self.state = state
        self.runtime = runtime or RUNTIME
        self.expensive_interval_s = expensive_interval_s
        self._expensive_cache: list[Sample] = []
        self._expensive_at = 0.0
        self._lock = threading.Lock()
        self._collect_id = 0
        self._by_model_at = -1
        self._by_model_cache: list[dict[str, Any]] = []

    def _by_model(self) -> list[dict[str, Any]]:
        """Разрез по моделям, посчитанный один раз за сбор.

        Вызывается из двух мест — производительности и моделей; без кеша это
        были бы две полные выборки заданий за сутки на каждый опрос метрик.
        """
        if self._by_model_at != self._collect_id:
            self._by_model_cache = self.state.analytics.by_model("day")
            self._by_model_at = self._collect_id
        return self._by_model_cache

    # -- вспомогательное -----------------------------------------------------

    @staticmethod
    def _safe(source: str, fn: Any, out: list[Sample], errors: list[str]) -> None:
        """Выполняет часть сбора, не давая ей уронить весь снимок."""
        try:
            fn(out)
        except Exception as exc:                        # noqa: BLE001
            errors.append(f"{source}: {type(exc).__name__}: {exc}")

    def collect(self) -> tuple[list[Sample], list[str]]:
        """Возвращает снимок и список источников, которые не удалось опросить."""
        out: list[Sample] = []
        errors: list[str] = []
        self._collect_id += 1
        for source, fn in (
            ("service", self._service),
            ("queue", self._queue),
            ("jobs", self._jobs),
            ("performance", self._performance),
            ("quality", self._quality),
            ("models", self._models),
            ("resources", self._resources),
            ("storage", self._storage),
            ("api", self._api),
            ("errors", self._errors),
            ("runtime", self._runtime_series),
        ):
            self._safe(source, fn, out, errors)
        self._safe("storage_size", lambda acc: acc.extend(self._expensive()), out, errors)
        out.extend(self._deprecated_aliases(out))
        return out, errors

    @staticmethod
    def _deprecated_aliases(samples: list[Sample]) -> list[Sample]:
        """Дублирует переименованные метрики под старыми именами.

        Метрики переведены в базовые единицы (байты, секунды), как требует
        соглашение Prometheus. Старые имена отдаются рядом, чтобы панели и
        правила, написанные до переименования, продолжали работать: убрать
        их можно будет, когда все потребители перейдут на новые.
        """
        from .catalog import RENAMED_FROM

        aliases = []
        for sample in samples:
            entry = RENAMED_FROM.get(sample.name)
            if entry is None:
                continue
            old_name, factor = entry
            aliases.append(Sample(old_name, round(sample.value / factor, 4), sample.labels))
        return aliases

    # -- служба --------------------------------------------------------------

    def _service(self, out: list[Sample]) -> None:
        from .. import catalog as model_catalog

        settings = self.state.settings
        out.append(Sample("asrhub_up", 1))
        out.append(Sample("asrhub_uptime_seconds",
                          round(time.time() - self.state.started_at, 1)))
        out.append(Sample("asrhub_build_info", 1, {
            "version": str(getattr(self.state, "version", "3.0.0")),
            "schema_version": str(SCHEMA_VERSION),
            "python": platform.python_version(),
            "catalog_date": str(model_catalog.CATALOG_DATE),
            "platform": f"{platform.system()}-{platform.machine()}".lower(),
        }))
        out.append(Sample("asrhub_queue_paused", 1 if self.state.queue.is_paused else 0))
        out.append(Sample("asrhub_scheduling_policy_info", 1,
                          {"policy": str(settings.get("scheduling_policy") or "priority_fifo")}))

    # -- очередь -------------------------------------------------------------

    def _queue(self, out: list[Sample]) -> None:
        status = self.state.queue.status()
        counts = status.get("counts") or {}
        for name, value in counts.items():
            out.append(Sample("asrhub_jobs_by_status", float(value), {"status": name}))

        out.append(Sample("asrhub_queue_depth", float(status.get("queue_depth", 0))))
        out.append(Sample("asrhub_active_jobs", float(status.get("active", 0))))
        out.append(Sample("asrhub_workers", float(status.get("worker_count", 0))))
        out.append(Sample("asrhub_queue_pending_audio_seconds",
                          float(status.get("pending_audio_s", 0))))
        out.append(Sample("asrhub_queue_eta_seconds", float(status.get("eta_s", 0))))
        out.append(Sample("asrhub_queue_capacity",
                          float(status.get("max_queue_size", 0) or 0)))

        oldest = self.state.db.query_one(
            "SELECT MIN(queued_at) t FROM jobs WHERE status IN ('queued','retry')")
        stamp = dict(oldest).get("t") if oldest else None
        out.append(Sample("asrhub_queue_oldest_seconds",
                          round(max(0.0, time.time() - float(stamp)), 1) if stamp else 0.0))

        waits = [float(r["queue_time_s"]) for r in self.state.db.query(
            "SELECT queue_time_s FROM jobs WHERE queue_time_s IS NOT NULL "
            "AND finished_at>=? LIMIT 5000", (time.time() - 86400,))]
        for stat, value in self._quantiles(waits).items():
            out.append(Sample("asrhub_queue_wait_seconds", value, {"stat": stat}))

    # -- задания -------------------------------------------------------------

    def _jobs(self, out: list[Sample]) -> None:
        since = time.time() - 86400
        rows = self.state.db.query(
            "SELECT model, engine, source, status, media_duration_s, processing_time_s "
            "FROM jobs WHERE created_at>=? LIMIT 100000", (since,))

        by_model: Counter[tuple[str, str]] = Counter()
        by_source: Counter[str] = Counter()
        durations = Histogram(JOB_DURATION_BUCKETS)
        media = Histogram(MEDIA_DURATION_BUCKETS)

        for row in rows:
            by_model[(row["model"] or "", row["engine"] or "")] += 1
            by_source[row["source"] or "unknown"] += 1
            if row["processing_time_s"]:
                durations.observe(float(row["processing_time_s"]))
            if row["media_duration_s"]:
                media.observe(float(row["media_duration_s"]))

        for (model, engine), count in by_model.most_common(40):
            out.append(Sample("asrhub_jobs_by_model", float(count),
                              {"model": model, "engine": engine}))
        for source, count in by_source.items():
            out.append(Sample("asrhub_jobs_by_source", float(count), {"source": source}))

        self._emit_histogram(out, "asrhub_job_duration_seconds", durations, {})
        self._emit_histogram(out, "asrhub_media_duration_seconds", media, {})

    # -- производительность --------------------------------------------------

    def _performance(self, out: list[Sample]) -> None:
        overview = self.state.analytics.overview("day")
        performance = overview.get("performance") or {}

        rtf = performance.get("rtf") or {}
        for key in ("avg", "p50", "p90", "p95", "p99"):
            if rtf.get(key) is not None:
                out.append(Sample("asrhub_rtf", float(rtf[key]), {"stat": key}))

        for row in self._by_model()[:40]:
            if row.get("rtf_avg") is not None:
                out.append(Sample("asrhub_rtf_by_model", float(row["rtf_avg"]),
                                  {"model": str(row.get("model") or "")}))

        row = self.state.db.query_one(
            "SELECT AVG(audio_prep_s) prep, AVG(model_load_s) load, AVG(inference_s) inf, "
            "AVG(postprocess_s) post FROM jobs WHERE status='completed' AND finished_at>=?",
            (time.time() - 86400,))
        stages = dict(row) if row else {}
        for stage, column in (("audio_prep", "prep"), ("model_load", "load"),
                              ("inference", "inf"), ("postprocess", "post")):
            if stages.get(column) is not None:
                out.append(Sample("asrhub_stage_seconds", round(float(stages[column]), 4),
                                  {"stage": stage}))

        efficiency = self.state.analytics.efficiency("day")
        if efficiency.get("model_load_share") is not None:
            out.append(Sample("asrhub_model_load_share", float(efficiency["model_load_share"])))
        audio_hours = float(efficiency.get("audio_hours") or 0)
        out.append(Sample("asrhub_throughput_audio_hours", round(audio_hours / 24, 3)))

    # -- качество ------------------------------------------------------------

    def _quality(self, out: list[Sample]) -> None:
        since = time.time() - 86400
        confidences = [float(r["avg_confidence"]) for r in self.state.db.query(
            "SELECT avg_confidence FROM jobs WHERE avg_confidence IS NOT NULL "
            "AND finished_at>=? LIMIT 20000", (since,))]
        for stat, value in self._quantiles(confidences).items():
            out.append(Sample("asrhub_confidence", round(value, 4), {"stat": stat}))
        if confidences:
            low = sum(1 for value in confidences if value < 0.7) / len(confidences)
            out.append(Sample("asrhub_low_confidence_share", round(low, 4)))

        for row in self.state.db.query(
                "SELECT model, AVG(wer) w FROM jobs WHERE wer IS NOT NULL AND finished_at>=? "
                "GROUP BY model", (since,)):
            out.append(Sample("asrhub_wer", round(float(row["w"]), 4),
                              {"model": str(row["model"] or "")}))

    # -- модели и движки -----------------------------------------------------

    def _models(self, out: list[Sample]) -> None:
        from ..engines import engine_status

        registry = self.state.registry
        out.append(Sample("asrhub_models_loaded", float(len(registry.loaded()))))

        available = 0
        for info in engine_status():
            ok = 1.0 if info.get("available") else 0.0
            available += int(ok)
            out.append(Sample("asrhub_engine_available", ok,
                              {"engine": str(info.get("id") or "")}))
        out.append(Sample("asrhub_engines_available", float(available)))

        for row in self._by_model()[:40]:
            if row.get("success_rate") is not None:
                out.append(Sample("asrhub_model_success_rate", float(row["success_rate"]),
                                  {"model": str(row.get("model") or "")}))

    # -- оборудование --------------------------------------------------------

    def _resources(self, out: list[Sample]) -> None:
        samples = self.state.db.system_samples(time.time() - 600, limit=1)
        latest = samples[-1] if samples else {}

        MB = 1024 ** 2
        if latest.get("cpu_percent") is not None:
            out.append(Sample("asrhub_cpu_percent", float(latest["cpu_percent"])))
        for column, metric in (("ram_used_mb", "asrhub_ram_used_bytes"),
                               ("ram_total_mb", "asrhub_ram_total_bytes")):
            if latest.get(column) is not None:
                out.append(Sample(metric, float(latest[column]) * MB))

        for column, metric in (("gpu_percent", "asrhub_gpu_percent"),
                               ("gpu_mem_mb", "asrhub_gpu_memory_used_bytes"),
                               ("gpu_mem_total", "asrhub_gpu_memory_total_bytes")):
            if latest.get(column) is not None:
                value = float(latest[column])
                out.append(Sample(metric, value * (1 if metric.endswith("percent") else MB),
                                  {"gpu": "0"}))

        self._gpu_extra(out)
        self._process(out)

    def _gpu_extra(self, out: list[Sample]) -> None:
        """Температура и потребление: их нет в общем замере системы."""
        from ..hardware import _run

        result = _run(["nvidia-smi",
                       "--query-gpu=index,temperature.gpu,power.draw",
                       "--format=csv,noheader,nounits"])
        if not result:
            return
        for line in result.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 3:
                continue
            index = parts[0]
            for value, metric in ((parts[1], "asrhub_gpu_temperature_celsius"),
                                  (parts[2], "asrhub_gpu_power_watts")):
                try:
                    out.append(Sample(metric, float(value), {"gpu": index}))
                except ValueError:
                    continue

    def _process(self, out: list[Sample]) -> None:
        out.append(Sample("asrhub_process_threads", float(threading.active_count())))
        try:
            import psutil  # type: ignore

            process = psutil.Process(os.getpid())
            # Имя метрики не должно зависеть от того, установлен ли psutil.
            # Раньше эта ветка отдавала asrhub_process_memory_mb, а запасная —
            # asrhub_process_memory_bytes: один и тот же сервис на двух
            # машинах публиковал метрику под разными именами, и правило
            # тревоги срабатывало ровно на половине установок.
            out.append(Sample("asrhub_process_memory_bytes",
                              float(process.memory_info().rss)))
        except Exception:                                   # noqa: BLE001
            try:
                with open(f"/proc/{os.getpid()}/statm", encoding="utf-8") as fh:
                    pages = int(fh.read().split()[1])
                out.append(Sample("asrhub_process_memory_bytes",
                                  float(pages * os.sysconf("SC_PAGE_SIZE"))))
            except (OSError, ValueError, IndexError):
                pass

    # -- хранилище -----------------------------------------------------------


    def _storage(self, out: list[Sample]) -> None:
        import shutil

        usage = shutil.disk_usage(str(self.state.settings.paths.data))
        out.append(Sample("asrhub_disk_free_bytes", float(usage.free)))
        out.append(Sample("asrhub_disk_used_percent",
                          round(usage.used / usage.total * 100, 1) if usage.total else 0.0))

        stats = self.state.db.stats()
        # Размер берётся у файла напрямую. Через stats()["size_mb"] он шёл
        # округлённым до сотых мегабайта: пустая база (несколько килобайт)
        # показывала ровный ноль, а большая — обратно домноженное округление
        # вместо настоящего числа байт.
        out.append(Sample("asrhub_database_size_bytes", float(_file_size(stats.get("path")))))
        for table in ("jobs", "segments", "events", "metrics"):
            if stats.get(table) is not None:
                out.append(Sample("asrhub_database_rows", float(stats[table]), {"table": table}))

    # -- программный интерфейс и ошибки --------------------------------------

    def _api(self, out: list[Sample]) -> None:
        out.append(Sample("asrhub_http_in_flight", float(self.runtime.in_flight)))
        out.append(Sample("asrhub_websocket_clients", float(self.runtime.websocket_clients)))

    def _errors(self, out: list[Sample]) -> None:
        out.append(Sample("asrhub_last_error_timestamp_seconds",
                          float(self.runtime.last_error_ts)))

    def _runtime_series(self, out: list[Sample]) -> None:
        """Переносит в снимок счётчики и гистограммы, накопленные в памяти."""
        counters, gauges, histograms = self.runtime.snapshot()
        for (name, labels), value in counters.items():
            out.append(Sample(name, float(value), dict(labels)))
        for (name, labels), value in gauges.items():
            out.append(Sample(name, float(value), dict(labels)))
        for (name, labels), hist in histograms.items():
            self._emit_histogram(out, name, hist, dict(labels))

    # -- дорогие замеры ------------------------------------------------------

    def _expensive(self) -> list[Sample]:
        """Размеры каталогов: обход каталога моделей стоит дорого, поэтому кеш."""
        with self._lock:
            if time.time() - self._expensive_at < self.expensive_interval_s:
                return list(self._expensive_cache)

        result: list[Sample] = []
        paths = self.state.settings.paths
        for kind in ("uploads", "results", "models", "logs"):
            directory = getattr(paths, kind, None)
            if directory is None:
                continue
            try:
                total = sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
            except OSError:
                continue
            result.append(Sample("asrhub_storage_bytes", float(total), {"kind": kind}))

        with self._lock:
            self._expensive_cache = result
            self._expensive_at = time.time()
        return list(result)

    # -- общее ---------------------------------------------------------------

    @staticmethod
    def _emit_histogram(out: list[Sample], name: str, hist: Histogram,
                        labels: dict[str, str]) -> None:
        for edge, count in hist.cumulative():
            out.append(Sample(f"{name}_bucket", float(count), {**labels, "le": str(edge)}))
        out.append(Sample(f"{name}_bucket", float(hist.total), {**labels, "le": "+Inf"}))
        out.append(Sample(f"{name}_sum", round(hist.sum, 4), dict(labels)))
        out.append(Sample(f"{name}_count", float(hist.total), dict(labels)))

    @staticmethod
    def _quantiles(values: list[float]) -> dict[str, float]:
        if not values:
            return {}
        summary = M.summarize(values)
        return {key: float(summary[key]) for key in ("avg", "p50", "p90", "p95", "p99")
                if summary.get(key) is not None}


def describe(samples: list[Sample]) -> list[dict[str, Any]]:
    """Дополняет снимок описаниями из каталога — для выгрузки в JSON."""
    grouped: dict[str, list[Sample]] = defaultdict(list)
    for sample in samples:
        base = sample.name
        for suffix in ("_bucket", "_sum", "_count"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        grouped[base].append(sample)

    result = []
    for name, items in sorted(grouped.items()):
        spec = METRICS_BY_NAME.get(name)
        entry: dict[str, Any] = {
            "name": name,
            "values": [{"labels": s.labels, "value": s.value} for s in items],
        }
        if spec:
            entry.update({
                "type": spec.type, "group": spec.group, "label": spec.label,
                "unit": spec.unit, "description": spec.description,
                "recommendation": spec.recommendation, "normal": spec.normal,
                "threshold": spec.threshold.to_dict() if spec.threshold else None,
            })
        result.append(entry)
    return result
