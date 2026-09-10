"""Фоновый поток смыслового разбора.

Модель отвечает секунды, а не миллисекунды, и звать её из потока
распознавания значило бы задерживать выдачу результата. Поэтому свой
поток: свежие записи встают в очередь при завершении задания, архив — по
одной, от новых к старым, когда включён `llm_backfill`.

Уступает распознаванию: пока в очереди ждут задания, поток спит
(`llm_yield_to_queue`) — расшифровка важнее пересказа. Сбой одной записи
не останавливает остальные: ответ с ошибкой кладётся в таблицу, чтобы
запись не возвращалась в очередь бесконечно, а в карточке была видна
причина.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any

from ..logging_setup import get_logger
from . import tasks
from .client import LLMClient, LLMError

log = get_logger("llm")

#: Пауза, когда делать нечего или очередь распознавания занята.
ПАУЗА_ПРОСТОЯ = 15.0

#: Сколько записей архива брать за один заход.
ПОРЦИЯ = 5


class LLMWorker:
    def __init__(self, db: Any, settings: Any, client: LLMClient, *,
                 queue_state: Any = None, content_index: Any = None):
        self.db = db
        self.settings = settings
        self.client = client
        self._queue_state = queue_state
        self._content = content_index
        self._pending: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.done = 0
        self.failed = 0
        self.last_error: str | None = None
        self.current: str | None = None

    # --- снаружи ----------------------------------------------------------

    def enqueue(self, job_id: str) -> None:
        """Свежая запись — в очередь разбора, если слой включён и разбор новых."""
        if self.client.enabled and self.settings.get("llm_auto", True):
            self._pending.put(job_id)

    def analyze_job(self, job_id: str, *, force: bool = False) -> dict[str, Any]:
        """Разбор одной записи сейчас, в вызывающем потоке; ответ — в базу."""
        if not force:
            готовое = self.db.llm_get(job_id)
            if готовое and готовое.get("version") == tasks.VERSION and not готовое.get("error"):
                return готовое
        job = self.db.get_job(job_id)
        if job is None:
            raise LLMError(f"Задание «{job_id}» не найдено.")
        сегменты = self.db.get_segments(job_id)
        скрипт = list(self.settings.get("content_script") or [])
        оператор = str(self.settings.get("content_agent_speaker") or "")
        try:
            итог = tasks.analyze(self.client, text=str(job.get("text") or ""),
                                 segments=сегменты, settings=self.settings,
                                 agent_speaker=оператор, script=скрипт)
        except LLMError as exc:
            self.db.llm_save(job_id, tasks.VERSION, model=self.client.model, error=str(exc),
                             calls=0, latency_ms=None)
            raise
        self.db.llm_save(
            job_id, tasks.VERSION, model=self.client.model,
            summary=итог.get("summary"), reason=итог.get("reason"),
            outcome=итог.get("outcome"), resolved=итог.get("resolved"),
            actions=итог.get("actions"), trackers=итог.get("trackers"),
            scorecard=итог.get("scorecard"), chunks=итог.get("chunks") or 1,
            calls=итог.get("calls") or 0, latency_ms=итог.get("latency_ms"),
            error=("; ".join(итог.get("warnings") or []) or None)
            if not итог.get("summary") and not итог.get("outcome") else None)
        return self.db.llm_get(job_id) or итог

    def status(self) -> dict[str, Any]:
        return {"running": bool(self._thread and self._thread.is_alive()),
                "queued": self._pending.qsize(), "current": self.current,
                "done": self.done, "failed": self.failed, "last_error": self.last_error,
                "auto": bool(self.settings.get("llm_auto", True)),
                "backfill": bool(self.settings.get("llm_backfill", False))}

    # --- поток ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="asrhub-llm", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        поток, self._thread = self._thread, None
        if поток and поток.is_alive():
            поток.join(timeout=timeout)

    def _queue_busy(self) -> bool:
        """Ждут ли задания распознавания — тогда модель подождёт."""
        if not self.settings.get("llm_yield_to_queue", True) or self._queue_state is None:
            return False
        try:
            return int(self._queue_state()) > 0
        except Exception:                                    # noqa: BLE001
            return False

    def _next(self) -> str | None:
        try:
            return self._pending.get_nowait()
        except queue.Empty:
            pass
        if not self.settings.get("llm_backfill", False):
            return None
        ожидают = self.db.llm_pending(tasks.VERSION, limit=ПОРЦИЯ)
        return str(ожидают[0]["id"]) if ожидают else None

    def _loop(self) -> None:
        пауза = 1.0
        while not self._stop.wait(timeout=пауза):
            if not self.client.enabled or self._queue_busy():
                пауза = ПАУЗА_ПРОСТОЯ
                continue
            job_id = self._next()
            if job_id is None:
                пауза = ПАУЗА_ПРОСТОЯ
                continue
            self.current = job_id
            try:
                self.analyze_job(job_id, force=True)
                self.done += 1
                пауза = 0.5
            except LLMError as exc:
                self.failed += 1
                self.last_error = str(exc)
                log.warning("Смысловой разбор %s не удался: %s", job_id, exc)
                # Сервер модели лежит — не долбить его каждой записью.
                пауза = ПАУЗА_ПРОСТОЯ
            except Exception as exc:                         # noqa: BLE001
                self.failed += 1
                self.last_error = str(exc)
                log.warning("Смысловой разбор %s дал сбой: %s", job_id, exc)
                try:
                    self.db.llm_save(job_id, tasks.VERSION, model=self.client.model,
                                     error=str(exc), calls=0, latency_ms=None)
                except Exception:                            # noqa: BLE001
                    pass
                пауза = 2.0
            finally:
                self.current = None
        return None


def wait_idle(worker: LLMWorker, timeout: float = 30.0) -> bool:
    """Ждёт, пока очередь разбора опустеет, — для проверок."""
    крайний = time.time() + timeout
    while time.time() < крайний:
        if worker._pending.empty() and worker.current is None:
            return True
        time.sleep(0.05)
    return False
