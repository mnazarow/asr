"""Общие зависимости API: состояние приложения, аутентификация, ограничение частоты."""
from __future__ import annotations

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


@dataclass
class AppState:
    settings: Settings
    db: Database
    registry: EngineRegistry
    queue: JobQueue
    analytics: Analytics
    started_at: float = field(default_factory=time.time)
    subscribers: set[Any] = field(default_factory=set)
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


def require_write(principal: Principal) -> Principal:
    if not principal.can_write:
        raise ForbiddenError("Ключ доступа работает только на чтение.")
    return principal


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise ForbiddenError("Требуется ключ с ролью администратора.")
    return principal


def error_response(exc: ASRHubError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_dict())
