"""Общие зависимости API: состояние приложения, аутентификация, ограничение частоты."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request

from ..analytics import Analytics
from ..config import Settings
from ..db import Database
from ..engines import EngineRegistry
from ..errors import ASRHubError, AuthError, ForbiddenError, RateLimited
from ..job_queue import JobQueue
from ..logging_setup import get_logger

log = get_logger("api")


class TicketStore:
    """Одноразовые короткоживущие билеты вместо ключа в адресе.

    Браузерный WebSocket не позволяет задать заголовок, поэтому ключ раньше
    уходил в строку запроса — а она попадает в историю браузера, в журнал
    обратного прокси и в поле Referer. Билет живёт минуту, тратится один раз
    и ничего не открывает повторно.
    """

    __slots__ = ("_ttl", "_items", "_lock")

    def __init__(self, ttl_s: float = 60.0) -> None:
        self._ttl = ttl_s
        self._items: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def issue(self, token: str) -> tuple[str, int]:
        ticket = secrets.token_urlsafe(24)
        with self._lock:
            self._prune()
            # Ограничение сверху: билеты выдаются по запросу, и без потолка
            # цикл переподключения мог бы наращивать словарь без предела.
            if len(self._items) > 512:
                self._items.clear()
            self._items[ticket] = (time.time() + self._ttl, token)
        return ticket, int(self._ttl)

    def redeem(self, ticket: str) -> str:
        """Возвращает ключ, к которому привязан билет, и гасит билет."""
        if not ticket:
            return ""
        with self._lock:
            self._prune()
            item = self._items.pop(ticket, None)
        if not item or item[0] < time.time():
            return ""
        return item[1]

    def _prune(self) -> None:
        now = time.time()
        for key in [k for k, (expires, _) in self._items.items() if expires < now]:
            self._items.pop(key, None)


@dataclass
class AppState:
    settings: Settings
    db: Database
    registry: EngineRegistry
    queue: JobQueue
    analytics: Analytics
    started_at: float = field(default_factory=time.time)
    subscribers: set[Any] = field(default_factory=set)
    monitoring: Any = None
    version: str = "3.0.0"
    tickets: TicketStore = field(default_factory=TicketStore)
    _rate: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))

    def check_rate(self, key: str, limit: int) -> None:
        if limit <= 0:
            return
        window = self._rate[key]
        now = time.time()
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit:
            raise RateLimited(limit, retry_after_s=int(60 - (now - window[0])) + 1)
        window.append(now)


def get_state(request: Request) -> AppState:
    state = getattr(request.app.state, "hub", None)
    if state is None:
        raise HTTPException(status_code=503, detail={"code": "not_ready",
                                                     "message": "Сервер ещё запускается."})
    return state


@dataclass
class Principal:
    key: str = ""
    name: str = "аноним"
    role: str = "admin"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "user")


def authenticate(request: Request,
                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                 authorization: str | None = Header(default=None)) -> Principal:
    """Проверяет ключ доступа и лимит частоты запросов."""
    state = get_state(request)
    if not state.settings.get("auth_enabled", True):
        return Principal(name="без аутентификации", role="admin")

    token = x_api_key or ""
    if not token and authorization:
        parts = authorization.split(" ", 1)
        token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" \
            else authorization.strip()
    if not token:
        token = request.query_params.get("api_key", "")

    info = state.settings.api_keys.get(token)
    if not info:
        raise AuthError("Ключ доступа отсутствует или недействителен.")
    if info.get("enabled") is False:
        raise ForbiddenError("Ключ доступа отключён.")

    limit = int(info.get("rate_limit") or state.settings.get("rate_limit_per_minute") or 0)
    state.check_rate(token, limit)
    return Principal(key=token, name=str(info.get("name") or "ключ"),
                     role=str(info.get("role") or "user"))


def scope_owner(principal: Principal, requested: str | None = None) -> str | None:
    """Владелец, которым надо ограничить выборку.

    Карточка задания давно закрыта require_owner, а список — нет: ключ с
    ролью user получал по GET /api/jobs чужие задания целиком, вместе с
    именем файла, путём на диске и полной расшифровкой. Проверка стояла на
    одном пути и отсутствовала на соседнем.

    Администратор видит всё и может отобрать по любому владельцу; остальным
    выборка сужается до собственных заданий, что бы они ни просили.
    """
    if principal.is_admin:
        return requested or None
    return principal.name


def require_write(principal: Principal) -> Principal:
    if not principal.can_write:
        raise ForbiddenError("Ключ доступа работает только на чтение.")
    return principal


def require_owner(principal: Principal, job: dict[str, Any]) -> None:
    """Проверяет, что ключ вправе распоряжаться этим заданием.

    Поле owner заполнялось, но нигде не сверялось: ключ с ролью «user» мог
    удалить или переставить чужое задание. Администратор по-прежнему видит
    и меняет всё.
    """
    if principal.is_admin:
        return
    owner = str(job.get("owner") or "")
    # Пустой владелец раньше означал «проверять нечего», и любое такое
    # задание — созданное клиентом командной строки, ключом без имени или
    # ещё до появления поля — читал и удалял кто угодно. Задание без
    # владельца считаем чужим для всех, кроме администратора.
    if owner != principal.name:
        raise ForbiddenError(
            f"Задание принадлежит другому ключу («{owner}»).",
            hint="Распоряжаться чужими заданиями может только ключ с ролью admin.")


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise ForbiddenError("Требуется ключ с ролью администратора.")
    return principal


def error_response(exc: ASRHubError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_dict())
