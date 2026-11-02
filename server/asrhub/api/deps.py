"""Общие зависимости API: состояние приложения, аутентификация, ограничение частоты."""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from fastapi import Header, HTTPException, Request

from .. import __version__
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
            # Срок — по монотонным часам: билет живёт в памяти процесса, и
            # перевод системных часов назад продлевал бы его на час, а вперёд
            # — гасил все разом.
            self._items[ticket] = (time.monotonic() + self._ttl, token)
        return ticket, int(self._ttl)

    def redeem(self, ticket: str) -> str:
        """Возвращает ключ, к которому привязан билет, и гасит билет."""
        if not ticket:
            return ""
        with self._lock:
            self._prune()
            item = self._items.pop(ticket, None)
        if not item or item[0] < time.monotonic():
            return ""
        return item[1]

    def _prune(self) -> None:
        now = time.monotonic()
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
    #: Обрабатывает ли этот процесс очередь. `--no-queue` — «только
    #: интерфейс»: задания принимает, а считает их соседний сервер над
    #: той же базой. Пробам это нужно знать, иначе такой сервер вечно
    #: «мёртв» и оркестратор перезапускает его по кругу.
    queue_enabled: bool = True
    monitoring: Any = None
    #: Разбор содержания записей: признаки, свод по корпусу, пересчёт архива.
    content: Any = None
    #: Смысловой слой: клиент языковой модели и фоновый поток разбора.
    llm: Any = None
    llm_worker: Any = None
    #: Версия сервера. Одна на всех: раньше число было вписано в десяток
    #: мест по отдельности, и после обновления интерфейс показывал одну
    #: версию, `/api/health` — другую, а файл VERSION — третью.
    version: str = __version__
    tickets: TicketStore = field(default_factory=TicketStore)
    #: Учётные записи и сессии веб-интерфейса. Ключи доступа живут отдельно и
    #: работают как раньше: они для программ, учётные записи — для людей.
    accounts: Accounts | None = None
    #: Фоновая запись журнала доступа (`audit.Писатель`).
    audit_writer: Any = None
    #: Лента событий веб-сокетов (`app.EventHub`): обработчикам отзыва и
    #: выхода нужно закрывать сокеты.
    events: Any = None
    #: Пул потоков диктовки (`app._в_потоке_диктовки`).
    stream_pool: Any = None
    _rate: dict[str, deque[float]] = field(default_factory=lambda: defaultdict(deque))
    #: Замок к окнам частоты. Проверка ключа — обычная (не async)
    #: зависимость, а такие FastAPI выполняет в пуле потоков: два запроса
    #: с одним ключом входили сюда одновременно, оба видели просроченную
    #: запись, и второй `popleft()` падал `IndexError` на пустой очереди —
    #: пятисоткой на совершенно исправном запросе.
    _rate_lock: threading.Lock = field(default_factory=threading.Lock)
    #: С какого размера словаря окон пора его чистить (см. `_вычистить_окна`).
    _rate_чистка: int = 1024

    def check_rate(self, key: str, limit: int) -> None:
        if limit <= 0:
            return
        # Монотонные часы: окно живёт в памяти процесса, и перевод системных
        # часов на час назад запирал ключ с исчерпанным окном на час.
        now = time.monotonic()
        with self._rate_lock:
            self._вычистить_окна(now)
            window = self._rate[key]
            while window and now - window[0] > 60:
                window.popleft()
            if len(window) >= limit:
                raise RateLimited(limit, retry_after_s=int(60 - (now - window[0])) + 1)
            window.append(now)

    def _вычистить_окна(self, now: float) -> None:
        """Убирает окна, в которых за минуту ничего не было.

        Словарь рос без предела: ключ «login:<адрес>» оставался навсегда, а
        за доверенным прокси клиент наращивал его произвольными строками в
        X-Forwarded-For. Чистим не на каждом запросе, а когда словарь
        заметно вырос.
        """
        if len(self._rate) <= self._rate_чистка:
            return
        for ключ in [к for к, окно in self._rate.items()
                     if not окно or now - окно[-1] > 60]:
            self._rate.pop(ключ, None)
        self._rate_чистка = max(1024, 2 * len(self._rate))


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
    #: Ключ получает тексты с замаскированными персональными данными:
    #: телефоны, почта, карты, паспорт, СНИЛС, ИНН, даты рождения.
    mask_pii: bool = False

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
    if getattr(account, "продлена_до", 0):
        # Сессию продлили на сервере — продлеваем и куку (это сделает
        # прослойка после ответа): иначе браузер выбрасывал её через неделю
        # от входа при любой работе.
        request.state.session_refresh = (token, float(account.продлена_до))

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

    principal = участник_записи(state, account)
    if principal.must_change_password \
            and request.url.path not in _PASSWORD_CHANGE_ALLOWED:
        raise PasswordChangeRequired()
    return principal


def коллеги(state: AppState, group: str) -> tuple[str, ...]:
    """Имена всех участников подразделения: и ключей, и учётных записей.

    Одна сборка на оба пути входа. Раньше у сессии в коллеги попадали и
    учётные записи группы, и её ключи, а у ключа — только ключи: учётная
    запись видела задания ключа своей группы, а ключ задания учётной записи
    той же группы — нет, и карточка отвечала ему 403.
    """
    if not group:
        return ()
    names = {str(info.get("name") or "ключ")
             for info in state.settings.api_keys.values()
             if str(info.get("group") or "") == group}
    accounts = state.accounts
    if accounts is not None:
        try:
            names.update(other.username for other in accounts.list()
                         if other.group == group)
        except Exception as exc:                               # noqa: BLE001
            # Таблицы учётных записей может не быть (база до миграции): ключи
            # обязаны работать и тогда — с коллегами-ключами.
            log.debug("Коллеги по группе «%s» без учётных записей: %s", group, exc)
    return tuple(sorted(names))


def участник_ключа(state: AppState, token: str, info: dict[str, Any]) -> Principal:
    """Участник по ключу доступа — одинаково для HTTP и веб-сокетов."""
    group = str(info.get("group") or "")
    return Principal(key=token, name=str(info.get("name") or "ключ"),
                     group=group, peers=коллеги(state, group),
                     quota_jobs_per_day=int(info.get("quota_jobs_per_day") or 0),
                     quota_audio_hours_per_day=float(
                         info.get("quota_audio_hours_per_day") or 0),
                     quota_storage_gb=float(info.get("quota_storage_gb") or 0),
                     mask_pii=bool(info.get("mask_pii")),
                     role=str(info.get("role") or "user"))


def участник_записи(state: AppState, account: Any) -> Principal:
    """Участник по учётной записи — одинаково для HTTP и веб-сокетов."""
    return Principal(name=account.username, role=account.role,
                     group=account.group, peers=коллеги(state, account.group),
                     user_id=account.id,
                     must_change_password=account.must_change_password)


def ключ_действует(state: AppState, token: str) -> bool:
    """Жив ли ключ: не отозван и не отключён — проверка без базы."""
    info = state.settings.api_keys.get(token) if token else None
    return bool(info) and info.get("enabled") is not False


def token_from(x_api_key: str | None, authorization: str | None,
               query_key: str | None = None) -> str:
    """Ключ доступа из того, чем его прислали, — одним местом на всех.

    Разбор заголовка повторялся в шести местах: в проверке ключа, в двух
    маршрутах мониторинга, в двух веб-сокетах и в прослойке маскирования.
    Пять из них принимали `Authorization: <ключ>` без схемы, а шестая —
    только `Bearer <ключ>`. Этой шестой была прослойка маскирования: ключ,
    заведённый администратором именно затем, чтобы не видеть персональных
    данных, получал их целиком, стоит убрать из заголовка слово «Bearer».

    Разъезжаться такому разбору нельзя вообще: он решает, кто есть кто, и
    любое расхождение между «кем тебя считает проверка» и «кем тебя
    считает защита» — это дыра, а не неудобство.
    """
    token = (x_api_key or "").strip()
    if not token and authorization:
        части = authorization.split(" ", 1)
        token = (части[1].strip()
                 if len(части) == 2 and части[0].lower() == "bearer"
                 else authorization.strip())
    if not token:
        token = (query_key or "").strip()
    return token


def token_of(request: Request) -> str:
    """То же самое для запроса или веб-сокета, у которого нет Depends."""
    query = getattr(request, "query_params", None)
    return token_from(request.headers.get("x-api-key"),
                      request.headers.get("authorization"),
                      query.get("api_key") if query is not None else None)


def authenticate(request: Request,
                 x_api_key: str | None = Header(default=None, alias="X-API-Key"),
                 authorization: str | None = Header(default=None)) -> Principal:
    """Проверяет ключ доступа или сессию входа и лимит частоты запросов.

    Найденного участника кладёт в `request.state.principal`: журналу доступа
    и middleware нужно знать, КТО пришёл, а разбирать заголовки второй раз
    нельзя. Одна такая вторая копия разбора уже стоила дыры — она принимала
    только «Bearer», и ключ с обезличиванием получал полные телефоны, стоило
    передать ключ заголовком X-API-Key. Точка входа должна остаться одна.
    """
    участник = _проверить(request, x_api_key, authorization)
    request.state.principal = участник
    return участник


def _проверить(request: Request, x_api_key: str | None,
               authorization: str | None) -> Principal:
    state = get_state(request)
    if not state.settings.get("auth_enabled", True):
        return Principal(name="без аутентификации", role="admin")

    token = token_from(x_api_key, authorization,
                       request.query_params.get("api_key"))
    # Одноразовый билет вместо ключа — для скачивания обычной ссылкой.
    # Копия базы бывает в гигабайты, и тянуть её в память вкладки, чтобы
    # приложить заголовок, нельзя; а обычная ссылка заголовка не несёт, и
    # у вошедшего по ключу скачивание срывалось с 401. Билет живёт минуту,
    # тратится один раз и принимается только на чтение.
    по_билету = False
    if not token and request.method in ("GET", "HEAD"):
        билет = request.query_params.get("ticket", "")
        if билет:
            по_билету = True
            token = state.tickets.redeem(билет)

    info = state.settings.api_keys.get(token)
    if not info:
        # Ключа нет — пробуем сессию веб-интерфейса. Порядок важен: программа
        # с ключом работает от ключа, даже если в браузере открыт интерфейс.
        session = _session_principal(request, state)
        if session is not None:
            return session
        if по_билету and not token:
            raise AuthError("Ссылка для скачивания устарела или уже использована.",
                            hint="Нажмите кнопку скачивания ещё раз: ссылка "
                                 "действует минуту и открывается один раз.")
        # «Отсутствует или недействителен» — это два разных случая с разным
        # лечением, а звучали они одинаково: тот, кто вовсе не передал ключ,
        # шёл проверять его правильность, а тот, у кого ключ отозван, —
        # искать, куда его вписать. Различать их безопасно: ответ не
        # рассказывает, какие ключи существуют.
        if not token:
            raise AuthError("Ключ доступа не передан.")
        raise AuthError("Ключ доступа недействителен.",
                        hint="Проверьте, что ключ скопирован целиком (он начинается "
                             "с «ah_») и не отозван. Список ключей — в разделе "
                             "«Доступ» веб-интерфейса.")
    if info.get("enabled") is False:
        raise ForbiddenError("Ключ доступа отключён.")

    limit = int(info.get("rate_limit") or state.settings.get("rate_limit_per_minute") or 0)
    state.check_rate(token, limit)
    return участник_ключа(state, token, info)


async def authenticate_body_key(request: Request) -> Principal:
    """Как authenticate, но ключ принимается ещё и полем api_key в теле.

    Так его присылает phone_asr: в теле JSON рядом с call_id, base_url и
    files. Заголовка X-API-Key в этом запросе нет вовсе, и без разбора тела
    совместимый клиент получал 401 — при формально верном ключе.

    Тело читается здесь и кешируется Starlette, поэтому обработчик получает
    его как обычно и вторым чтением ничего не ломает.
    """
    token = ""
    field_present = False
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip() == "application/json":
        try:
            payload = await request.json()
        except Exception:                       # тело не JSON — не наша забота
            payload = None
        if isinstance(payload, dict):
            field_present = "api_key" in payload
            token = str(payload.get("api_key") or "")
    try:
        return authenticate(request,
                            x_api_key=token or request.headers.get("X-API-Key"),
                            authorization=request.headers.get("Authorization"))
    except AuthError as exc:
        # Подсказка по умолчанию зовёт в заголовки, а этот адрес читает ключ
        # ещё и из тела — именно так его и присылают. Пустое поле api_key
        # при этом называется прямо: догадаться по «ключ не передан», что
        # поле есть, но в нём ничего нет, нельзя.
        if not token and field_present:
            raise AuthError(
                "Поле api_key в теле запроса пустое.",
                hint="Впишите в него ключ доступа (начинается с «ah_») — или "
                     "передайте ключ заголовком X-API-Key.") from None
        if not token:
            raise AuthError(
                exc.message,
                hint="Передайте ключ полем api_key в теле запроса, заголовком "
                     "X-API-Key или Authorization: Bearer <ключ>.") from None
        raise


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


def допуск_к_метрикам(request: Request) -> None:
    """Допуск к метрикам и пробам: свободно при `monitoring_public`, иначе — как везде.

    Своя проверка у метрик знала только ключи: при `monitoring_public: false`
    вошедший логином и паролем получал 401 в разделе «Мониторинг», а при
    выключенной аутентификации сервер требовал ключ, которого у него не
    было. Теперь разбор общий с остальными маршрутами: ключ, билет, сессия.
    """
    state = get_state(request)
    if state.settings.get("monitoring_public", True):
        return
    try:
        request.state.principal = _проверить(request, request.headers.get("x-api-key"),
                                             request.headers.get("authorization"))
    except ASRHubError as exc:
        raise error_response(exc) from exc


def закрыть_сокеты(request: Any, условие: Any, *, reason: str) -> None:
    """Закрывает веб-сокеты по условию — для обработчиков, у которых есть запрос.

    Ошибка здесь не должна отменять то, ради чего обработчик звали: ключ
    уже отозван, запись уже отключена — сокет закроет перепроверка.
    """
    hub = getattr(getattr(request, "app", None), "state", None)
    hub = getattr(hub, "events", None)
    if hub is None:
        return
    try:
        hub.close_where(условие, reason=reason)
    except Exception as exc:                                # noqa: BLE001
        log.debug("Сокеты не закрыты: %s", exc)


def error_response(exc: ASRHubError) -> HTTPException:
    return HTTPException(status_code=exc.http_status, detail=exc.to_dict())
