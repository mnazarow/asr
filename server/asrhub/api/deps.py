"""Общие зависимости API: состояние приложения, аутентификация, ограничение частоты."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request

from ..accounts import Accounts
from ..analytics import Analytics
from ..config import Settings
from ..db import Database
from ..engines import EngineRegistry
from ..errors import ASRHubError, AuthError, ForbiddenError, PasswordChangeRequired, RateLimited
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
    #: Учётные записи и сессии веб-интерфейса. Ключи доступа живут отдельно и
    #: работают как раньше: они для программ, учётные записи — для людей.
    accounts: Accounts | None = None
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


#: Имя куки с сессией веб-интерфейса.
SESSION_COOKIE = "asrhub_session"

#: Что разрешено, пока пароль не сменён. Всё остальное отвечает отказом с
#: кодом password_change_required — интерфейс по нему показывает форму смены.
_PASSWORD_CHANGE_ALLOWED = (
    "/api/auth/me", "/api/auth/password", "/api/auth/logout", "/api/health",
)


@dataclass
class Principal:
    key: str = ""
    name: str = "аноним"
    role: str = "admin"
    #: Подразделение. Ключи одной группы видят задания друг друга — это то
    #: разделение, которого не хватало трём ролям: у бухгалтерии свои
    #: записи, у отдела продаж свои, а «своё/чужое» по одному ключу не
    #: позволяло даже двум сотрудникам работать с общим набором заданий.
    group: str = ""
    #: Квоты на сутки; ноль означает «без ограничения».
    quota_jobs_per_day: int = 0
    quota_audio_hours_per_day: float = 0.0
    quota_storage_gb: float = 0.0
    #: Имена всех ключей той же группы, включая себя. Заполняется при
    #: аутентификации: только там доступны настройки с перечнем ключей.
    peers: tuple[str, ...] = ()
    #: Учётная запись, если вход был по логину и паролю. Пусто для ключей.
    user_id: str = ""
    #: Пароль требует смены — до неё разрешены только сама смена и выход.
    must_change_password: bool = False

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "user")


def _session_principal(request: Request, state: AppState) -> Principal | None:
    """Пускает по куке сессии, если она есть и жива.

    Ключ доступа проверяется первым: программа, приславшая ключ, должна
    работать от него, даже если в том же браузере открыт интерфейс.
    """
    accounts = state.accounts
    if accounts is None:
        return None
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    account = accounts.session_account(token)
    if account is None:
        return None

    # Проверка происхождения запроса. Кука уходит на сервер сама, поэтому
    # чужая страница могла бы отправить запрос от имени вошедшего человека.
    # SameSite=Lax отсекает это в браузере, а здесь — второй рубеж на случай
    # старого браузера и на случай, когда куку принесли не из браузера.
    origin = request.headers.get("origin") or ""
    if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
        host = request.headers.get("host") or ""
        if host and origin.split("://", 1)[-1] != host:
            raise ForbiddenError(
                "Запрос пришёл с чужой страницы.",
                hint="Так браузер защищает вход по паролю. Откройте интерфейс сервера напрямую.")

    limit = int(state.settings.get("rate_limit_per_minute") or 0)
    state.check_rate(f"user:{account.id}", limit)

    peers: tuple[str, ...] = ()
    if account.group:
        names = {other.username for other in accounts.list()
                 if other.group == account.group}
        names.update(str(info.get("name") or "ключ")
                     for info in state.settings.api_keys.values()
                     if str(info.get("group") or "") == account.group)
        peers = tuple(sorted(names))

    principal = Principal(name=account.username, role=account.role,
                          group=account.group, peers=peers,
                          user_id=account.id,
                          must_change_password=account.must_change_password)
    if principal.must_change_password \
            and request.url.path not in _PASSWORD_CHANGE_ALLOWED:
        raise PasswordChangeRequired()
    return principal


def authenticate(request: Request,
                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                 authorization: str | None = Header(default=None)) -> Principal:
    """Проверяет ключ доступа или сессию входа и лимит частоты запросов."""
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
        # Ключа нет — пробуем сессию веб-интерфейса. Порядок важен: программа
        # с ключом работает от ключа, даже если в браузере открыт интерфейс.
        session = _session_principal(request, state)
        if session is not None:
            return session
        raise AuthError("Ключ доступа отсутствует или недействителен.")
    if info.get("enabled") is False:
        raise ForbiddenError("Ключ доступа отключён.")

    limit = int(info.get("rate_limit") or state.settings.get("rate_limit_per_minute") or 0)
    state.check_rate(token, limit)
    group = str(info.get("group") or "")
    peers: tuple[str, ...] = ()
    if group:
        peers = tuple(sorted({
            str(other.get("name") or "ключ")
            for other in state.settings.api_keys.values()
            if str(other.get("group") or "") == group}))
    return Principal(key=token, name=str(info.get("name") or "ключ"),
                     group=group, peers=peers,
                     quota_jobs_per_day=int(info.get("quota_jobs_per_day") or 0),
                     quota_audio_hours_per_day=float(
                         info.get("quota_audio_hours_per_day") or 0),
                     quota_storage_gb=float(info.get("quota_storage_gb") or 0),
                     role=str(info.get("role") or "user"))


async def authenticate_body_key(request: Request) -> Principal:
    """Как authenticate, но ключ принимается ещё и полем api_key в теле.

    Так его присылает phone_asr: в теле JSON рядом с call_id, base_url и
    files. Заголовка X-API-Key в этом запросе нет вовсе, и без разбора тела
    совместимый клиент получал 401 — при формально верном ключе.

    Тело читается здесь и кешируется Starlette, поэтому обработчик получает
    его как обычно и вторым чтением ничего не ломает.
    """
    token = ""
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip() == "application/json":
        try:
            payload = await request.json()
        except Exception:                       # тело не JSON — не наша забота
            payload = None
        if isinstance(payload, dict):
            token = str(payload.get("api_key") or "")
    return authenticate(request,
                        x_api_key=token or request.headers.get("X-API-Key"),
                        authorization=request.headers.get("Authorization"))


def scope_owner(principal: Principal,
                requested: str | None = None) -> str | list[str] | None:
    """Владелец или их список, которым надо ограничить выборку.

    Карточка задания давно закрыта require_owner, а список — нет: ключ с
    ролью user получал по GET /api/jobs чужие задания целиком, вместе с
    именем файла, путём на диске и полной расшифровкой. Проверка стояла на
    одном пути и отсутствовала на соседнем.

    Администратор видит всё и может отобрать по любому владельцу. Ключ в
    группе видит задания своей группы — так работают подразделения. Ключ без
    группы видит только своё.
    """
    if principal.is_admin:
        return requested or None
    if principal.group and principal.peers:
        # Запрошенный владелец учитывается, но только если он в той же группе.
        if requested:
            return requested if requested in principal.peers else principal.name
        return list(principal.peers)
    return principal.name


def same_scope(principal: Principal, owner: str) -> bool:
    """Вправе ли ключ распоряжаться заданием этого владельца."""
    if principal.is_admin:
        return True
    if owner == principal.name:
        return True
    return bool(principal.group) and owner in principal.peers


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
    if not same_scope(principal, owner):
        raise ForbiddenError(
            f"Задание принадлежит другому ключу («{owner}»).",
            hint="Распоряжаться чужими заданиями может только ключ с ролью admin.")


def require_admin(principal: Principal) -> Principal:
    if not principal.is_admin:
        raise ForbiddenError("Требуется ключ с ролью администратора.")
    return principal


def error_response(exc: ASRHubError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_dict())
