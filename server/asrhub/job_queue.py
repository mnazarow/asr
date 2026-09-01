"""Очередь заданий: планирование, воркеры, повторы, ограничения.

Возможности:
* четыре политики планирования (приоритет, короткие вперёд, справедливое
  разделение между пользователями, по сроку);
* приостановка и возобновление очереди целиком и отдельных заданий;
* отмена в любой момент, включая уже выполняющееся задание;
* автоматические повторы с экспоненциальной задержкой и уменьшением
  размера пакета при нехватке видеопамяти;
* ограничение параллельности глобально и по каждой модели;
* кеш результатов по содержимому файла и отпечатку настроек;
* восстановление после перезапуска: задания, застрявшие в состоянии
  «выполняется», возвращаются в очередь.
"""
from __future__ import annotations

import random
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .catalog import get_model
from .db import Database, new_id, now
from .engines import EngineRegistry
from .errors import (
    ASRHubError,
    ConfigError,
    JobNotFound,
    JobTimeout,
    OutOfMemoryError,
    QueueFull,
    StorageError,
    classify_exception,
)
from .logging_setup import get_logger
from .monitoring.collector import (
    JOB_DURATION_BUCKETS,
    MEDIA_DURATION_BUCKETS,
    RUNTIME,
)
from .processor import cleanup_workdir, process_job, safe_workdir, settings_digest

log = get_logger("queue")

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
STATUS_RETRY = "retry"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY, STATUS_PAUSED)


@dataclass
class WorkerState:
    index: int
    job_id: str | None = None
    model: str = ""
    started_at: float = 0.0
    stage: str = ""
    progress: float = 0.0
    busy: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "index": self.index,
            "busy": self.busy,
            "job_id": self.job_id,
            "model": self.model,
            "stage": self.stage,
            "progress": round(self.progress, 3),
        }
        if self.busy and self.started_at:
            data["elapsed_s"] = round(time.time() - self.started_at, 1)
        return data


class JobQueue:
    """Менеджер очереди с пулом рабочих потоков."""

    def __init__(self, db: Database, settings: Any, registry: EngineRegistry,
                 *, on_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.db = db
        self.settings = settings
        self.registry = registry
        self.on_event = on_event

        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._paused = False
        self._workers: list[threading.Thread] = []
        self._states: list[WorkerState] = []
        self._cancelled: set[str] = set()
        # Индексы воркеров, помеченных на выход при уменьшении их числа.
        self._retiring: set[int] = set()
        self._webhooks: Any = None
        self._running: dict[str, float] = {}
        self._model_counts: dict[str, int] = {}
        self._max_queue = int(getattr(settings, "get", lambda *_: 1000)("max_queue_size", 1000) or 1000)
        self._started = False

    # --- запуск и остановка ---------------------------------------------

    def start(self, workers: int | None = None) -> None:
        if self._started:
            return
        count = int(workers or self.settings.get("max_concurrent_jobs") or 2)
        self.recover()
        self._states = [WorkerState(index=i) for i in range(count)]
        for index in range(count):
            thread = threading.Thread(target=self._worker_loop, args=(index,),
                                      name=f"asrhub-worker-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)
        janitor = threading.Thread(target=self._janitor_loop, name="asrhub-janitor", daemon=True)
        janitor.start()
        self._workers.append(janitor)
        self._started = True
        log.info("Очередь запущена: воркеров %d", count)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        with self._lock:
            pool, self._webhooks = self._webhooks, None
        if pool is not None:
            pool.shutdown(wait=False)
        self._wake.set()
        for thread in self._workers:
            thread.join(timeout=timeout / max(1, len(self._workers)))
        self._started = False
        log.info("Очередь остановлена")

    def recover(self) -> int:
        """Возвращает в очередь задания, оставшиеся в состоянии «выполняется».

        Обрабатываются все такие задания, а не первая тысяча: остаток иначе
        зависал бы навсегда — планировщик его не выберет, а очистка не удалит.
        """
        total = 0
        while True:
            stuck = self.db.list_jobs(status=STATUS_RUNNING, limit=1000, light=True)
            if not stuck:
                break
            for job in stuck:
                self.db.update_job(job["id"], status=STATUS_QUEUED, stage="",
                                   progress=0.0, started_at=None)
                self.db.add_event(job["id"], "recovered",
                                  "Задание возвращено в очередь после перезапуска сервера")
            total += len(stuck)
            if len(stuck) < 1000:
                break
        if total:
            log.warning("Возвращено в очередь после перезапуска: %d заданий", total)
        return total

    # --- добавление -----------------------------------------------------

    def submit(self, *, file_path: Path, filename: str, settings: dict[str, Any],
               owner: str = "anonymous", api_key_name: str = "", priority: int | None = None,
               group_id: str | None = None, source: str = "api",
               tags: str = "", deadline: float | None = None,
               reference_text: str = "", webhook_url: str = "") -> dict[str, Any]:
        """Ставит файл в очередь. Возвращает описание задания."""
        # Адрес уведомления приходит двумя путями: отдельным аргументом и
        # полем webhook_url внутри настроек задания. Проверялся только
        # первый, поэтому запрет на внутреннюю сеть обходился одной строкой
        # в JSON: settings={"webhook_url":"http://169.254.169.254/..."} — и
        # сервер сам ходил по этому адресу, а поле webhook_status работало
        # оракулом для обхода внутренней сети. Проверяем оба.
        target = webhook_url or str(settings.get("webhook_url") or "")
        if target:
            target = self._check_webhook_url(target)
        webhook_url = target
        depth = self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY])
        if depth >= self._max_queue:
            raise QueueFull(f"В очереди уже {depth} заданий (предел {self._max_queue}).")

        self._check_disk_space()

        path = Path(file_path)
        from .pipeline.audio import file_hash, probe

        digest = file_hash(path)
        params_digest = settings_digest(settings)
        spec = get_model(str(settings.get("model") or ""))

        duration = 0.0
        try:
            duration = probe(path).duration_s
        except ASRHubError as exc:
            log.info("Не удалось определить длительность «%s»: %s", filename, exc.message)

        # Кеш результатов по содержимому и настройкам
        if settings.get("deduplicate_jobs", True):
            cached = self._find_cached(digest, params_digest)
            if cached is not None:
                job_id = self._clone_cached(cached, filename, str(path), owner,
                                            group_id, settings, webhook_url)
                RUNTIME.inc("asrhub_cached_jobs_total")
                self._emit("job.cached", {"id": job_id, "source": cached["id"]})
                # Задание завершено мгновенно, но для отправителя оно
                # завершено — значит уведомление обязано уйти. Раньше на
                # этом пути его не отправляли вовсе, и внешняя система
                # ждала колбэка до собственного тайм-аута.
                if webhook_url:
                    self._send_webhook(job_id)
                return self.get(job_id)

        payload = dict(settings)
        payload["_hash"] = params_digest

        job_id = self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": str(path),
            "file_size": path.stat().st_size if path.exists() else 0,
            "file_hash": digest,
            "media_duration_s": duration,
            "engine": str(settings.get("engine") or (spec.engine if spec else "")),
            "model": str(settings.get("model") or ""),
            "language": str(settings.get("language") or ""),
            "params": payload,
            "priority": int(priority if priority is not None
                            else settings.get("priority", 50)),
            "owner": owner,
            "api_key_name": api_key_name,
            "source": source,
            "tags": tags,
            "deadline": deadline,
            "reference_text": reference_text or None,
            "webhook_url": webhook_url or None,
        })
        self._emit("job.queued", {"id": job_id, "filename": filename})
        self._wake.set()
        return self.get(job_id)

    def _find_cached(self, file_digest: str, params_digest: str) -> dict[str, Any] | None:
        """Ищет готовый результат для той же записи с теми же настройками.

        Поиск идёт запросом по индексу, а не перебором последних заданий:
        при потоке больше полусотни файлов перебор просто не находил
        совпадений, и дедупликация тихо переставала работать.
        """
        cached = self.db.find_cached(file_digest, params_digest)
        if cached is None:
            return None
        # Результаты могли быть удалены очисткой, а запись остаться:
        # отдавать ссылку на несуществующий каталог нельзя.
        result_path = cached.get("result_path")
        if result_path and not Path(result_path).is_dir():
            log.debug("Кеш пропущен: каталог результатов %s исчез", result_path)
            return None
        return cached

    def _clone_cached(self, cached: dict[str, Any], filename: str, path: str,
                      owner: str, group_id: str | None,
                      settings: dict[str, Any], webhook_url: str = "") -> str:
        job_id = self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": path,
            # Адрес уведомления переносим в клон: без него задание
            # завершалось молча и повторить отправку было нечем.
            "webhook_url": webhook_url or None,
            "file_hash": cached.get("file_hash"),
            "media_duration_s": cached.get("media_duration_s"),
            "engine": cached.get("engine"),
            "model": cached.get("model"),
            "language": cached.get("language"),
            "params": {**settings, "_hash": (cached.get("params") or {}).get("_hash")},
            "owner": owner,
            "status": STATUS_COMPLETED,
            "cached_from": cached["id"],
        })
        # Копируем каталог результатов, а не ссылаемся на чужой: иначе
        # удаление любого из двух заданий уничтожало результаты второго.
        result_path = self._copy_results(cached.get("result_path"), job_id)
        self.db.update_job(
            job_id,
            status=STATUS_COMPLETED,
            started_at=now(), finished_at=now(),
            text=cached.get("text"), result_path=result_path,
            segments_count=cached.get("segments_count"),
            words_count=cached.get("words_count"),
            avg_confidence=cached.get("avg_confidence"),
            rtf=cached.get("rtf"), processing_time_s=0.0, queue_time_s=0.0,
            progress=1.0, stage="из кеша")
        segments = self.db.get_segments(cached["id"])
        if segments:
            self.db.save_segments(job_id, segments)
        self.db.add_event(job_id, "cached",
                          f"Результат взят из кеша задания {cached['id']}")
        log.info("Задание %s: результат взят из кеша (%s)", job_id, cached["id"])
        return job_id

    # --- управление -----------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job is None:
            raise JobNotFound(f"Задание «{job_id}» не найдено.")
        return job

    def _copy_results(self, source: str | None, job_id: str) -> str | None:
        """Копирует каталог результатов для задания, отданного из кеша."""
        if not source:
            return None
        origin = Path(source)
        if not origin.is_dir():
            return None
        target = self.settings.paths.results / job_id
        try:
            shutil.copytree(origin, target, dirs_exist_ok=True)
        except OSError as exc:
            log.warning("Не удалось скопировать результаты из кеша: %s", exc)
            return str(origin)          # хуже, чем копия, но лучше, чем ничего
        return str(target)

    def cancel(self, job_id: str, by: str = "user") -> dict[str, Any]:
        """Отменяет задание. Уже завершённое не трогает.

        Смена статуса идёт условным запросом: между проверкой и записью
        воркер мог успеть завершить задание, и безусловная запись пометила
        бы готовый результат отменённым.
        """
        job = self.get(job_id)
        if job["status"] in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
            return job
        with self._lock:
            self._cancelled.add(job_id)

        changed = self.db.update_job_if_status(
            job_id,
            expected=[STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY, STATUS_PAUSED],
            status=STATUS_CANCELLED, cancelled_by=by,
            finished_at=now(), stage="отменено")
        if changed:
            self.db.add_event(job_id, "cancelled", f"Отменено ({by})")
            self._emit("job.cancelled", {"id": job_id})
        else:
            # Задание завершилось прямо во время отмены — это не ошибка.
            with self._lock:
                self._cancelled.discard(job_id)
            log.info("Отмена %s не применена: задание уже завершилось", job_id)
        return self.get(job_id)

    def cancel_group(self, group_id: str) -> int:
        jobs = self.db.list_jobs(group_id=group_id, status=list(ACTIVE_STATUSES), limit=10000)
        for job in jobs:
            self.cancel(job["id"], by="group")
        return len(jobs)

    def cancel_all(self) -> int:
        jobs = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY, STATUS_PAUSED], limit=10000)
        for job in jobs:
            self.cancel(job["id"], by="all")
        return len(jobs)

    def retry(self, job_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        job = self.get(job_id)
        params = dict(job.get("params") or {})
        if overrides:
            params.update(overrides)
        params.pop("_hash", None)
        params["_hash"] = settings_digest(params)
        self.db.update_job(job_id, status=STATUS_QUEUED, error_code=None,
                           error_message=None, error_hint=None, progress=0.0,
                           stage="", started_at=None, finished_at=None,
                           params=params, queued_at=now())
        self.db.add_event(job_id, "retry", "Задание поставлено в очередь повторно")
        with self._lock:
            self._cancelled.discard(job_id)
        self._wake.set()
        self._emit("job.queued", {"id": job_id})
        return self.get(job_id)

    def retry_failed(self, limit: int = 100) -> int:
        jobs = self.db.list_jobs(status=STATUS_FAILED, limit=limit)
        for job in jobs:
            self.retry(job["id"])
        return len(jobs)

    def set_priority(self, job_id: str, priority: int) -> dict[str, Any]:
        priority = max(0, min(100, int(priority)))
        self.db.update_job(job_id, priority=priority)
        self.db.add_event(job_id, "priority", f"Приоритет изменён на {priority}")
        self._wake.set()
        return self.get(job_id)

    def move_to_top(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 100)

    def move_to_bottom(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 0)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == STATUS_QUEUED:
            self.db.update_job(job_id, status=STATUS_PAUSED, stage="приостановлено")
            self.db.add_event(job_id, "paused", "Задание приостановлено")
        return self.get(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == STATUS_PAUSED:
            self.db.update_job(job_id, status=STATUS_QUEUED, stage="")
            self.db.add_event(job_id, "resumed", "Задание возобновлено")
            self._wake.set()
        return self.get(job_id)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        self.db.add_event(None, "queue_paused", "Очередь приостановлена")
        self._emit("queue.paused", {})

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._wake.set()
        self.db.add_event(None, "queue_resumed", "Очередь возобновлена")
        self._emit("queue.resumed", {})

    @property
    def is_paused(self) -> bool:
        return self._paused

    def set_concurrency(self, workers: int) -> None:
        """Меняет число воркеров на ходу.

        Новые потоки запускаются сразу; лишние помечаются на выход и
        завершаются, доработав текущее задание. Без этой пометки потоки
        оставались жить, а последовательность 2 → 8 → 2 оставляла восемь
        живых воркеров при заявленных двух.
        """
        workers = max(1, min(64, int(workers)))
        with self._lock:
            current = len(self._states)
            if workers > current:
                # Вернувшиеся к работе индексы снимаем с выхода.
                self._retiring -= set(range(workers))
                for index in range(current, workers):
                    state = WorkerState(index=index)
                    self._states.append(state)
                    thread = threading.Thread(target=self._worker_loop, args=(index,),
                                              name=f"asrhub-worker-{index}", daemon=True)
                    thread.start()
                    self._workers.append(thread)
            elif workers < current:
                self._retiring.update(range(workers, current))
            self._limit = workers
        self.settings.set("max_concurrent_jobs", workers)
        self._wake.set()

    # --- выбор следующего задания ----------------------------------------

    def _next_job(self, worker_index: int) -> dict[str, Any] | None:
        with self._lock:
            if self._paused or worker_index >= int(
                    self.settings.get("max_concurrent_jobs") or 2):
                return None
            active = len(self._running)
            if active >= int(self.settings.get("max_concurrent_jobs") or 2):
                return None
            per_model_limit = int(self.settings.get("max_concurrent_per_model") or 0)

            policy = str(self.settings.get("scheduling_policy") or "priority_fifo")
            # Порядок предварительной выборки должен соответствовать политике,
            # иначе она работает на случайной верхушке очереди: срочное
            # задание с низким приоритетом не будет выбрано, пока не
            # разгребётся всё, что стоит выше.
            preselect = {
                "shortest_first": "media_duration_s ASC",
                "deadline": "created_at ASC",
                "fair_share": "created_at ASC",
            }.get(policy, "priority DESC")
            window = int(self.settings.get("scheduling_window") or 500)
            candidates = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY],
                                           limit=window, order=preselect, light=True)
            candidates = [c for c in candidates if c["id"] not in self._running]
            if per_model_limit > 0:
                candidates = [c for c in candidates
                              if self._model_counts.get(c.get("model") or "", 0) < per_model_limit]
            candidates = [c for c in candidates
                          if not c.get("queued_at") or c["queued_at"] <= now()]
            if not candidates:
                return None

            chosen = self._apply_policy(candidates, policy)
            if chosen is None:
                return None

            self._running[chosen["id"]] = time.time()
            model = chosen.get("model") or ""
            self._model_counts[model] = self._model_counts.get(model, 0) + 1

        # Слот занят. Дальше два обращения к базе, и любой их срыв обязан
        # слот вернуть: освобождение живёт в _worker_loop, но только для
        # заданий, которые тот успел получить. Без отката одна ошибка «база
        # заблокирована» навсегда съедала слот, а при достижении предела
        # очередь переставала брать работу до перезапуска.
        try:
            self.db.update_job(chosen["id"], status=STATUS_RUNNING, started_at=now(),
                               stage="запуск", progress=0.0,
                               queue_time_s=max(0.0, now() - float(
                                   chosen.get("queued_at") or chosen.get("created_at") or now())))
            job = self.db.get_job(chosen["id"])
        except Exception:
            self._release_slot(chosen["id"], model)
            raise
        if job is None:
            # Задание удалили между выборкой и чтением.
            self._release_slot(chosen["id"], model)
            return None
        return job

    def _release_slot(self, job_id: str, model: str) -> None:
        """Возвращает занятый слот и счётчик модели."""
        with self._lock:
            self._running.pop(job_id, None)
            if model in self._model_counts:
                self._model_counts[model] = max(0, self._model_counts[model] - 1)

    def _check_webhook_url(self, url: str) -> str:
        """Проверяет адрес уведомления.

        Адрес приходит из запроса и уходит в urlopen, поэтому без проверки
        сервер становится инструментом обращения к внутренней сети от своего
        имени: file:// читает диск, а http://169.254.169.254 достаёт учётные
        данные облака. Пропускаем только http и https и запрещаем адреса,
        которые заведомо указывают внутрь.
        """
        import ipaddress
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ConfigError(
                f"Адрес уведомления должен начинаться с http:// или https:// "
                f"(получено «{parsed.scheme or url[:20]}»).",
                hint="Другие схемы запрещены: через них сервер читал бы "
                     "собственные файлы вместо отправки уведомления.")
        host = (parsed.hostname or "").strip()
        if not host:
            raise ConfigError("В адресе уведомления не указан узел.")

        if self.settings.get("webhook_allow_internal", False):
            return url

        lowered = host.lower()
        if lowered in ("localhost", "localhost.localdomain") or lowered.endswith(".localhost"):
            raise ConfigError(
                "Адрес уведомления указывает на сам сервер.",
                hint="Если это намеренно, включите webhook_allow_internal.")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return url                      # доменное имя — проверять некому
        if (address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast):
            raise ConfigError(
                f"Адрес уведомления {host} находится во внутренней сети.",
                hint="Если это намеренно, включите webhook_allow_internal.")
        return url

    def _check_disk_space(self) -> None:
        """Отказывает в приёме, пока на диске меньше порога свободного места.

        Заполнившийся диск повреждает и базу, и уже посчитанные результаты,
        поэтому честный отказ на входе дешевле, чем авария в середине ночи.
        """
        limit_gb = float(self.settings.get("disk_min_free_gb") or 0.0)
        if limit_gb <= 0:
            return
        import shutil as shutil_mod
        try:
            free_gb = shutil_mod.disk_usage(str(self.settings.paths.data)).free / 1024 ** 3
        except OSError:
            return          # не смогли измерить — не мешаем работать
        if free_gb < limit_gb:
            raise StorageError(
                f"На диске осталось {free_gb:.1f} ГБ при пороге {limit_gb:.1f} ГБ — "
                f"новые задания не принимаются.",
                hint="Освободите место или уменьшите disk_min_free_gb. "
                     "Старые задания и файлы удаляет POST /api/maintenance/cleanup.")

    def _apply_policy(self, candidates: list[dict[str, Any]],
                      policy: str) -> dict[str, Any] | None:
        if not candidates:
            return None
        if policy == "shortest_first":
            return min(candidates,
                       key=lambda c: (float(c.get("media_duration_s") or 1e9),
                                      -int(c.get("priority") or 0)))
        if policy == "deadline":
            with_deadline = [c for c in candidates if c.get("deadline")]
            if with_deadline:
                return min(with_deadline, key=lambda c: float(c["deadline"]))
            return max(candidates, key=lambda c: (int(c.get("priority") or 0),
                                                  -float(c.get("created_at") or 0)))
        if policy == "fair_share":
            # Круговое обслуживание владельцев: берём задание того, у кого
            # сейчас меньше всего выполняющихся заданий.
            load: dict[str, int] = {}
            for job_id in self._running:
                job = self.db.get_job(job_id)
                if job:
                    owner = job.get("owner") or "anonymous"
                    load[owner] = load.get(owner, 0) + 1
            return min(candidates,
                       key=lambda c: (load.get(c.get("owner") or "anonymous", 0),
                                      -int(c.get("priority") or 0),
                                      float(c.get("created_at") or 0)))
        # priority_fifo
        return max(candidates, key=lambda c: (int(c.get("priority") or 0),
                                              -float(c.get("created_at") or 0)))

    # --- рабочий цикл -----------------------------------------------------

    def _worker_loop(self, index: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                if index in self._retiring:
                    self._retiring.discard(index)
                    if index < len(self._states):
                        del self._states[index:]
                    log.info("Воркер %d завершён: число воркеров уменьшено", index)
                    return
            job = None
            try:
                job = self._next_job(index)
            except Exception as exc:
                log.error("Ошибка выбора задания: %s", exc)
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            state = self._states[index] if index < len(self._states) else WorkerState(index)
            state.busy = True
            state.job_id = job["id"]
            state.model = job.get("model") or ""
            state.started_at = time.time()
            state.progress = 0.0
            try:
                self._execute(job, state)
            except Exception as exc:
                log.exception("Непредвиденная ошибка воркера: %s", exc)
            finally:
                with self._lock:
                    self._running.pop(job["id"], None)
                    model = job.get("model") or ""
                    if model in self._model_counts:
                        self._model_counts[model] = max(0, self._model_counts[model] - 1)
                state.busy = False
                state.job_id = None
                state.stage = ""
                state.progress = 0.0
                self._wake.set()

    def _execute(self, job: dict[str, Any], state: WorkerState) -> None:
        job_id = job["id"]
        params = dict(job.get("params") or {})
        merged = self.settings.merged(params)
        merged["reference_text"] = job.get("reference_text") or ""
        merged.setdefault("hf_token", self.settings.hf_token)

        paths = self.settings.paths
        workdir = safe_workdir(paths.tmp, job_id)
        outdir = paths.results / job_id
        timeout = int(merged.get("job_timeout_s") or 0)
        started = time.time()

        def progress(value: float, stage: str) -> None:
            state.progress = value
            state.stage = stage
            self.db.update_job(job_id, progress=round(value, 4), stage=stage)
            self._emit("job.progress", {"id": job_id, "progress": round(value, 4),
                                        "stage": stage})

        def cancelled() -> bool:
            if timeout and (time.time() - started) > timeout:
                raise JobTimeout(timeout)
            with self._lock:
                return job_id in self._cancelled

        self.db.add_event(job_id, "started", f"Обработка начата: {job.get('filename')}")
        self._emit("job.started", {"id": job_id, "filename": job.get("filename")})
        log.info("Задание %s: старт (%s)", job_id, job.get("model"),
                 extra={"job_id": job_id, "model": job.get("model")})

        try:
            outcome = process_job(
                Path(job["file_path"]), merged, self.registry,
                workdir=workdir, outdir=outdir,
                basename=Path(job.get("filename") or job_id).stem,
                progress=progress, cancelled=cancelled)
        except ASRHubError as exc:
            self._handle_failure(job, exc, merged)
            return
        except Exception as exc:
            self._handle_failure(job, classify_exception(
                exc, engine=str(job.get("engine")), model=str(job.get("model"))), merged)
            return
        finally:
            cleanup_workdir(workdir)

        elapsed = time.time() - started
        accuracy = outcome.stats.get("accuracy") or {}
        self.db.update_job(
            job_id,
            status=STATUS_COMPLETED, finished_at=now(), progress=1.0, stage="готово",
            text=outcome.text,
            result_path=str(outdir),
            segments_count=len(outcome.segments),
            words_count=int(outcome.stats.get("words") or 0),
            chars_count=int(outcome.stats.get("chars") or 0),
            speakers_count=len(outcome.speakers),
            avg_confidence=outcome.stats.get("avg_confidence"),
            rtf=outcome.rtf,
            processing_time_s=round(elapsed, 3),
            audio_prep_s=outcome.timings.get("audio_prep"),
            model_load_s=outcome.timings.get("model_load"),
            inference_s=outcome.timings.get("inference"),
            postprocess_s=outcome.timings.get("postprocess"),
            language=outcome.language,
            device=str(merged.get("device")),
            wer=accuracy.get("wer"), cer=accuracy.get("cer"),
        )
        self.db.save_segments(job_id, outcome.segments)

        RUNTIME.inc("asrhub_jobs_total", {"status": "completed"})
        RUNTIME.inc("asrhub_audio_seconds_total",
                    value=float(job.get("media_duration_s") or 0))
        RUNTIME.inc("asrhub_words_total", value=float(outcome.stats.get("words") or 0))
        RUNTIME.observe("asrhub_job_duration_seconds", elapsed, buckets=JOB_DURATION_BUCKETS)
        if job.get("media_duration_s"):
            RUNTIME.observe("asrhub_media_duration_seconds",
                            float(job["media_duration_s"]), buckets=MEDIA_DURATION_BUCKETS)

        self.db.bump_model_stats(
            str(job.get("model") or ""), str(job.get("engine") or ""), ok=True,
            audio_s=float(job.get("media_duration_s") or 0.0),
            processing_s=elapsed, words=int(outcome.stats.get("words") or 0),
            rtf=outcome.rtf, confidence=outcome.stats.get("avg_confidence"),
            wer=accuracy.get("wer"))
        for name, value in (("rtf", outcome.rtf),
                            ("processing_time_s", elapsed),
                            ("queue_time_s", float(job.get("queue_time_s") or 0.0)),
                            ("confidence", outcome.stats.get("avg_confidence") or 0.0)):
            self.db.add_metric(name, value, job_id=job_id, model=str(job.get("model")),
                               engine=str(job.get("engine")))
        for warning in outcome.warnings:
            self.db.add_event(job_id, "warning", warning)

        log.info("Задание %s: готово за %.1f с, RTF %.3f", job_id, elapsed, outcome.rtf,
                 extra={"job_id": job_id, "model": job.get("model")})
        self.db.add_event(job_id, "completed",
                          f"Готово за {elapsed:.1f} с, RTF {outcome.rtf:.3f}")
        self._emit("job.completed", {"id": job_id, "rtf": outcome.rtf,
                                     "duration_s": elapsed})
        self._send_webhook(job_id)

        if merged.get("delete_source_after"):
            try:
                Path(job["file_path"]).unlink(missing_ok=True)
            except OSError:
                pass

    def _handle_failure(self, job: dict[str, Any], error: ASRHubError,
                        merged: dict[str, Any]) -> None:
        job_id = job["id"]
        with self._lock:
            was_cancelled = job_id in self._cancelled
            self._cancelled.discard(job_id)
        if was_cancelled:
            self.db.update_job(job_id, status=STATUS_CANCELLED, finished_at=now(),
                               stage="отменено")
            RUNTIME.inc("asrhub_jobs_total", {"status": "cancelled"})
            return

        retries = int(job.get("retries") or 0)
        max_retries = int(merged.get("max_retries") or 0)

        if error.retryable and retries < max_retries:
            delay = float(merged.get("retry_backoff_s") or 10.0) * (2 ** retries)
            delay *= 0.75 + random.random() * 0.5      # разброс, чтобы повторы не совпали
            params = dict(job.get("params") or {})
            if isinstance(error, OutOfMemoryError):
                divisor = max(1, int(merged.get("oom_retry_batch_divisor") or 2))
                old_batch = int(params.get("batch_size", merged.get("batch_size", 8)))
                params["batch_size"] = max(1, old_batch // divisor)
                self.db.add_event(
                    job_id, "retry_adjust",
                    f"Размер пакета уменьшен с {old_batch} до {params['batch_size']} "
                    f"из-за нехватки памяти")
            self.db.update_job(
                job_id, status=STATUS_RETRY, retries=retries + 1,
                error_code=error.code, error_message=error.message, error_hint=error.hint,
                stage=f"повтор через {int(delay)} с", progress=0.0,
                queued_at=now() + delay, params=params)
            self.db.add_event(job_id, "retry_scheduled",
                              f"Повтор {retries + 1} из {max_retries} через {int(delay)} с: "
                              f"{error.message}")
            log.warning("Задание %s: %s — повтор через %.0f с", job_id, error.message, delay,
                        extra={"job_id": job_id, "error_code": error.code})
            RUNTIME.inc("asrhub_retries_total")
            self._emit("job.retry", {"id": job_id, "attempt": retries + 1,
                                     "delay_s": round(delay, 1), "error": error.message})
            return

        self.db.update_job(
            job_id, status=STATUS_FAILED, finished_at=now(), progress=0.0,
            stage="ошибка", error_code=error.code, error_message=error.message,
            error_hint=error.hint)
        self.db.bump_model_stats(str(job.get("model") or ""), str(job.get("engine") or ""),
                                 ok=False)
        RUNTIME.inc("asrhub_jobs_total", {"status": "failed"})
        RUNTIME.note_error(error.code, error.retryable)
        if error.code == "no_speech":
            RUNTIME.inc("asrhub_no_speech_total")
        self.db.add_event(job_id, "failed", error.message, error.to_dict())
        log.error("Задание %s провалено: %s", job_id, error.message,
                  extra={"job_id": job_id, "error_code": error.code})
        self._emit("job.failed", {"id": job_id, "error": error.to_dict()})
        self._send_webhook(job_id)

    # --- уведомления -----------------------------------------------------

    def _send_webhook(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job or not job.get("webhook_url"):
            return
        self._webhook_pool().submit(self._webhook_worker, job)

    def _webhook_pool(self) -> Any:
        """Ограниченный пул для доставки уведомлений.

        Создаётся при первой доставке, чтобы не держать потоки на серверах,
        где уведомления не используются.
        """
        with self._lock:
            if self._webhooks is None:
                from concurrent.futures import ThreadPoolExecutor

                size = max(1, int(self.settings.get("webhook_workers") or 4))
                self._webhooks = ThreadPoolExecutor(
                    max_workers=size, thread_name_prefix="asrhub-webhook")
                log.debug("Пул доставки уведомлений: %d потоков", size)
            return self._webhooks

    def _webhook_worker(self, job: dict[str, Any]) -> None:
        import hashlib
        import hmac
        import json as json_mod
        import urllib.error
        import urllib.request

        payload = json_mod.dumps({
            "id": job["id"], "status": job["status"], "filename": job.get("filename"),
            "model": job.get("model"), "rtf": job.get("rtf"),
            "duration_s": job.get("media_duration_s"),
            "words": job.get("words_count"),
            "error": job.get("error_message"),
        }, ensure_ascii=False).encode("utf-8")

        secret = str(self.settings.get("webhook_secret") or "").encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "User-Agent": "ASRHub/3.0"}
        if secret:
            headers["X-ASRHub-Signature"] = hmac.new(secret, payload, hashlib.sha256).hexdigest()

        attempts = 5
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(job["webhook_url"], data=payload,
                                                 headers=headers, method="POST")
                with urllib.request.urlopen(request, timeout=15) as response:
                    if 200 <= response.status < 300:
                        self.db.update_job(job["id"], webhook_status=f"ok:{response.status}")
                        RUNTIME.inc("asrhub_webhooks_total", {"result": "ok"})
                        return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.info("Уведомление для %s не доставлено (попытка %d из %d): %s",
                         job["id"], attempt + 1, attempts, exc)
            if attempt < attempts - 1:      # после последней попытки ждать незачем
                time.sleep(min(60, 2 ** attempt))
        self.db.update_job(job["id"], webhook_status="failed")
        RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.debug("Обработчик события «%s» дал сбой: %s", kind, exc)

    # --- фоновое обслуживание ---------------------------------------------

    def _janitor_loop(self) -> None:
        last_cleanup = 0.0
        failures = 0
        while not self._stop.wait(timeout=20.0):
            try:
                self.registry.collect_idle()
                self._sample_system()
                if time.time() - last_cleanup > 3600:
                    retention = int(self.settings.get("result_retention_days") or 30)
                    removed = self.db.cleanup(results_days=retention)
                    if any(removed.values()):
                        log.info("Очистка хранилища: %s", removed)
                    last_cleanup = time.time()
            except Exception as exc:                    # noqa: BLE001
                # Раньше здесь стоял debug, и постоянно падающая очистка диска
                # не давала ни одной записи при штатном уровне INFO. Первые
                # сбои показываем, дальше переходим на debug, чтобы не залить
                # журнал одной и той же строкой раз в двадцать секунд.
                failures += 1
                if failures <= 3 or failures % 180 == 0:
                    log.warning("Служебный цикл дал сбой (%d-й раз): %s", failures, exc)
                else:
                    log.debug("Служебный цикл: %s", exc)
            else:
                if failures:
                    log.info("Служебный цикл восстановился после %d сбоев", failures)
                failures = 0

    def _sample_system(self) -> None:
        sample: dict[str, Any] = {
            "queue_depth": self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY]),
            "active_jobs": len(self._running),
        }
        try:
            import psutil  # type: ignore

            sample["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sample["ram_used_mb"] = round((vm.total - vm.available) / 1024 / 1024)
            sample["ram_total_mb"] = round(vm.total / 1024 / 1024)
        except Exception:
            try:
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    info = {}
                    for line in fh:
                        key, _, rest = line.partition(":")
                        parts = rest.strip().split()
                        if parts and parts[0].isdigit():
                            info[key] = int(parts[0])
                total = info.get("MemTotal", 0) / 1024
                avail = info.get("MemAvailable", 0) / 1024
                sample["ram_total_mb"] = round(total)
                sample["ram_used_mb"] = round(total - avail)
            except OSError:
                pass
        try:
            from .hardware import _run

            out = _run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                        "--format=csv,noheader,nounits"])
            if out:
                parts = [p.strip() for p in out.splitlines()[0].split(",")]
                if len(parts) >= 3:
                    sample["gpu_percent"] = float(parts[0])
                    sample["gpu_mem_mb"] = float(parts[1])
                    sample["gpu_mem_total"] = float(parts[2])
        except Exception:
            pass
        try:
            import shutil as shutil_mod

            usage = shutil_mod.disk_usage(str(self.settings.paths.data))
            sample["disk_free_gb"] = round(usage.free / 1024 ** 3, 2)
        except Exception:
            pass
        self.db.add_system_sample(sample)

    # --- состояние --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        counts = {status: self.db.count_jobs(status=status)
                  for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY,
                                 STATUS_PAUSED, STATUS_COMPLETED, STATUS_FAILED,
                                 STATUS_CANCELLED)}
        queued = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY], limit=500,
                                   light=True)
        pending_audio = sum(float(j.get("media_duration_s") or 0) for j in queued)
        stats = self.db.model_stats()
        rtf_values = [s["rtf_avg"] for s in stats if s.get("rtf_avg")]
        avg_rtf = sum(rtf_values) / len(rtf_values) if rtf_values else 0.25
        workers = int(self.settings.get("max_concurrent_jobs") or 2)
        eta = (pending_audio * avg_rtf / max(1, workers)) if pending_audio else 0.0
        return {
            "paused": self._paused,
            "workers": [s.to_dict() for s in self._states],
            "worker_count": workers,
            "counts": counts,
            "queue_depth": counts[STATUS_QUEUED] + counts[STATUS_RETRY],
            "active": len(self._running),
            "pending_audio_s": round(pending_audio, 1),
            "eta_s": round(eta, 1),
            "policy": self.settings.get("scheduling_policy"),
            "max_queue_size": self._max_queue,
            "loaded_models": self.registry.loaded(),
        }
