"""События заданий между серверами над общей базой.

Рассылка по веб-сокету жила в памяти процесса: задание, которое считал
соседний сервер, в интерфейсе этого сервера не двигалось — ни «начато»,
ни прогресс, ни «готово», пока страницу не обновят. Теперь каждый сервер
раз в секунду читает из общей базы события, записанные соседями (у
события есть отметка экземпляра), и ход заданий, которые соседи считают
прямо сейчас, и рассылает их своим подписчикам с тем же адресом
владельца, что и свои.

Пока соседей нет (отметки живых экземпляров), опроса нет вовсе.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from .logging_setup import get_logger

log = get_logger("events")

#: Вид события в таблице → тип события веб-сокета.
ВИДЫ = {
    "created": "job.queued", "requeued": "job.queued", "started": "job.started",
    "completed": "job.completed", "failed": "job.failed", "cancelled": "job.cancelled",
    "cached": "job.cached", "retry": "job.retry", "retry_scheduled": "job.retry",
}
#: Как часто опрашивать базу, пока соседи есть.
ШАГ_С = 1.0
#: Как часто перепроверять, есть ли соседи.
СОСЕДИ_С = 15.0


class МостСобытий:
    def __init__(self, db: Any, опубликовать: Callable[[str, dict[str, Any]], None],
                 *, шаг_с: float = ШАГ_С) -> None:
        self.db = db
        self.опубликовать = опубликовать
        self.шаг_с = шаг_с
        self._стоп = threading.Event()
        self._поток: threading.Thread | None = None
        self._после: int | None = None
        self._ход: dict[str, tuple[float, str]] = {}
        self._соседи_до = 0.0
        self._соседи = False

    def start(self) -> None:
        if self._поток is not None:
            return
        self._поток = threading.Thread(target=self._цикл, name="asrhub-events-bridge",
                                       daemon=True)
        self._поток.start()

    def stop(self) -> None:
        self._стоп.set()
        if self._поток is not None:
            self._поток.join(timeout=3)

    def _есть_соседи(self, сейчас: float) -> bool:
        if сейчас >= self._соседи_до:
            self._соседи_до = сейчас + СОСЕДИ_С
            try:
                self._соседи = bool(self.db.other_instances())
            except Exception:                               # noqa: BLE001
                self._соседи = False
        return self._соседи

    def _цикл(self) -> None:
        import time  # noqa: PLC0415

        while not self._стоп.wait(self.шаг_с):
            try:
                if self._есть_соседи(time.monotonic()):
                    self.шаг()
                else:
                    self._после = None
                    self._ход.clear()
            except Exception as exc:                        # noqa: BLE001
                log.debug("События соседей не прочитаны: %s", exc)

    def шаг(self) -> int:
        """Один проход: разослать новые события и ход соседей. Сколько разослано."""
        if self._после is None:
            # Начинаем с текущего конца: старое уже показано при загрузке.
            self._после = self.db.последнее_событие()
            return 0
        разослано = 0
        for с in self.db.события_соседей(self._после):
            self._после = max(self._после, int(с["id"]))
            тип = ВИДЫ.get(str(с["kind"]))
            if тип is None or с.get("own") or not с.get("job_id") or с.get("owner") is None:
                continue
            данные: dict[str, Any] = {"id": с["job_id"], "_owner": с.get("owner") or "",
                                      "status": с.get("status"), "message": с.get("message") or "",
                                      "filename": с.get("filename") or "", "remote": True}
            self.опубликовать(тип, данные)
            разослано += 1
            if тип in ("job.completed", "job.failed", "job.cancelled"):
                self._ход.pop(str(с["job_id"]), None)
        идут = set()
        for з in self.db.ход_соседей():
            ид = str(з["id"])
            идут.add(ид)
            ход = (round(float(з.get("progress") or 0.0), 4), str(з.get("stage") or ""))
            if self._ход.get(ид) == ход:
                continue
            self._ход[ид] = ход
            self.опубликовать("job.progress", {"id": ид, "_owner": з.get("owner") or "",
                                               "progress": ход[0], "stage": ход[1],
                                               "remote": True})
            разослано += 1
        for ид in set(self._ход) - идут:
            self._ход.pop(ид, None)
        return разослано
