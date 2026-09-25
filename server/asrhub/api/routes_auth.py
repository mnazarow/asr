"""Вход по логину и паролю и управление учётными записями.

Ключи доступа этими маршрутами не затрагиваются: они как были, так и
остаются — для программ. Здесь всё про людей, открывающих веб-интерфейс.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from .. import audit
from ..accounts import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    AccountError,
    AccountNotFound,
    поле_да_нет,
)
from ..errors import AuthError, ForbiddenError
from ..logging_setup import get_logger
from .deps import (
    SESSION_COOKIE,
    Principal,
    authenticate,
    get_state,
    require_admin,
)

log = get_logger("api.auth")

router = APIRouter(prefix="/api/auth", tags=["Вход"])


def _accounts(request: Request):
    state = get_state(request)
    if state.accounts is None:
        raise AuthError("Вход по логину и паролю недоступен.",
                        hint="Учётные записи появляются после обновления базы.")
    return state.accounts


def _set_cookie(request: Request, response: Response, token: str, expires: float) -> None:
    """Ставит куку сессии.

    secure выставляем по фактической схеме запроса, а не жёстко: сервер часто
    стоит за прокси на http внутри сети, и кука с secure туда просто не
    доедет — человек вошёл бы и тут же оказался разлогинен.
    """
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    https = (forwarded or request.url.scheme) == "https"
    response.set_cookie(
        SESSION_COOKIE, token,
        # `request.scope["_now"]` не заполняет никто, поэтому раньше сюда
        # уходила не длительность, а абсолютная метка времени: кука
        # получала срок годности 2083 год, а Starlette из целого `expires`
        # делает «столько секунд от сейчас». Серверная сессия при этом
        # честно истекала — но браузер хранил мёртвый токен десятилетиями
        # и слал его при каждом запросе.
        max_age=int(max(0, expires - time.time())) or None,
        expires=int(expires),
        httponly=True,          # javascript до куки не дотянется
        samesite="lax",         # чужая страница не отправит её POST-запросом
        secure=https,
        path="/",
    )


@router.post("/login", summary="Вход по логину и паролю")
def login(request: Request, response: Response,
          payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Проверяет пару и заводит сессию.

    Ответ одинаков для несуществующего логина и неверного пароля: иначе
    список существующих логинов собирается простым перебором.
    """
    state = get_state(request)
    # Предел частоты — до scrypt, а не после. Это единственный маршрут без
    # ключа доступа, и единственный, где сервер считает хеш пароля: 32 МБ
    # и сотня миллисекунд на попытку. Без предела шестьдесят запросов с
    # одного адреса за семь секунд поднимали потребление памяти с 89 МБ до
    # 1,1 ГБ, а /api/health отвечал четыре секунды вместо десятой доли.
    # Блокировка учётной записи здесь не помощник: она привязана к
    # существующему логину, а наплыв идёт по выдуманным.
    # Адрес клиента — за доверенным прокси из его заголовка: за nginx у
    # всех один адрес, и предел считался общим на всех — чужой подбор
    # пароля запирал вход каждому (см. `trusted_proxies`).
    адрес = (audit.адрес(request.headers, request.client,
                         state.settings.get("trusted_proxies")) or "неизвестно")
    state.check_rate(f"login:{адрес}",
                     int(state.settings.get("login_rate_limit") or 0))
    accounts = _accounts(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise AuthError("Введите логин и пароль.")
    # Имя для журнала доступа — до проверки пароля, а не после. Неудачный вход
    # и есть то, ради чего журнал читают: череда отказов под одним логином —
    # это подбор пароля, а «аноним не смог войти» об этом не говорит ничего.
    # Само имя при отказе ничего не подтверждает: логин мог быть выдуман.
    request.state.audit_actor = username
    account = _войти(state, accounts, username, password)
    request.state.audit_actor = account.username
    token, expires = accounts.open_session(
        account.id,
        user_agent=request.headers.get("user-agent", ""),
        address=адрес if адрес != "неизвестно" else "")
    _set_cookie(request, response, token, expires)
    log.info("Вход: %s", account.username)
    ответ: dict[str, Any] = {
        "user": account.to_dict(),
        "expires_at": expires,
        "must_change_password": account.must_change_password,
    }
    if account.must_change_password:
        # Почему смена обязательна — так форма скажет правду (см. /me).
        ответ["password_reason"] = (
            "default" if account.username.lower() == DEFAULT_USERNAME
            and password == DEFAULT_PASSWORD else "assigned")
    return ответ


def _войти(state, accounts, username: str, password: str):
    """Проверяет пару: своей записью или через каталог предприятия.

    Порядок именно такой, и он не случаен:

    1. Запись, пришедшая из каталога, проверяется ТОЛЬКО каталогом. Пробовать
       для неё местный пароль нельзя: пароля у неё нет, каждая попытка была бы
       неудачей, и после нескольких входов подряд счётчик неудач запер бы
       человека, который всё делал правильно.
    2. Своя запись проверяется своим паролем. Администратор, заведённый при
       установке, обязан входить и тогда, когда каталог недоступен, — иначе
       сервер запирается вместе с упавшим контроллером домена.
    3. Логин, которого здесь нет вовсе, идёт в каталог: так входят первый раз.

    Ответ при отказе один и тот же во всех трёх случаях — иначе по разнице в
    сообщениях собирается список тех, кто в каталоге есть.
    """
    from ..ldap_auth import Настройки, войти  # noqa: PLC0415

    каталог = Настройки.из_настроек(state.settings)
    запись = accounts.by_username(username)

    if запись is not None and not запись.from_directory:
        return accounts.authenticate(username, password)

    if not каталог.enabled:
        # Записи из каталога остались, а каталог выключили: входить по ним
        # нечем, и делать вид, что дело в пароле, нечестно.
        if запись is not None and запись.from_directory:
            raise AuthError(
                "Эта учётная запись входит через каталог предприятия, "
                "а он выключен.",
                hint="Включите «Вход через каталог» в настройках или задайте "
                     "этому человеку обычную учётную запись.")
        return accounts.authenticate(username, password)

    человек = войти(каталог, username, password)
    if человек is None:
        if запись is not None and запись.from_directory:
            raise AuthError("Неверный логин или пароль.")
        # Логина нет ни здесь, ни в каталоге — обычная проверка даст тот же
        # отказ и тем же текстом, заодно посчитав неудачу.
        return accounts.authenticate(username, password)

    учётная = accounts.ensure_directory(
        человек.username, role=человек.role, display_name=человек.display_name)
    if not учётная.enabled:
        raise ForbiddenError("Учётная запись отключена на сервере.")
    accounts.note_login(учётная.id)
    log.info("Вход через каталог: %s (роль %s)", учётная.username, учётная.role)
    return учётная


@router.post("/logout", summary="Выход")
def logout(request: Request, response: Response) -> dict[str, Any]:
    accounts = _accounts(request)
    токен = request.cookies.get(SESSION_COOKIE, "")
    ушедший = accounts.session_account(токен) if токен else None
    if ушедший is not None:
        request.state.audit_actor = ушедший.username
    accounts.close_session(токен)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me", summary="Кто я")
def me(request: Request,
       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сведения о текущем входе — их спрашивает интерфейс при загрузке."""
    state = get_state(request)
    вход_включён = bool(state.settings.get("auth_enabled", True))
    data: dict[str, Any] = {
        "name": principal.name,
        "role": principal.role,
        "group": principal.group,
        "kind": "user" if principal.user_id else "key",
        "must_change_password": principal.must_change_password,
        "auth_enabled": вход_включён,
    }
    if principal.must_change_password and state.accounts is not None:
        # Почему нужна смена: форма говорила «пароль, заданный при первом
        # запуске, известен всем» и тому, кому администратор выдал
        # временный пароль лично, — а это неправда и пугает зря.
        учётка = state.accounts.get(principal.user_id) if principal.user_id else None
        data["password_reason"] = (
            "default" if учётка is not None
            and учётка.username.lower() == DEFAULT_USERNAME
            and state.accounts.uses_default_password() else "assigned")
    if principal.is_admin and state.accounts is not None and вход_включён:
        # Предупреждение про пароль по умолчанию видит только администратор:
        # остальным оно ничего не даёт, а подсказывает лишнее. И только
        # когда вход включён: без него пароль ничего не защищает, а
        # предупреждение всплывало на каждой загрузке страницы.
        data["default_password_in_use"] = state.accounts.uses_default_password()
    return data


@router.post("/password", summary="Смена своего пароля")
def change_password(request: Request,
                    payload: dict[str, Any] = Body(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Меняет пароль текущей учётной записи.

    Старый пароль спрашиваем всегда, даже когда смена обязательная: чужая
    открытая вкладка не должна давать возможность сменить пароль и забрать
    учётную запись себе.
    """
    accounts = _accounts(request)
    if not principal.user_id:
        raise ForbiddenError("Пароль есть только у учётной записи.",
                             hint="Вы вошли по ключу доступа — у него пароля нет.")
    current = str(payload.get("current_password") or "")
    new = str(payload.get("new_password") or "")
    account = accounts.get(principal.user_id)
    if account is None:
        raise AccountNotFound("Учётная запись не найдена.")
    accounts.authenticate(account.username, current)
    if new == current:
        raise AccountError("Новый пароль совпадает со старым.")
    if new == DEFAULT_PASSWORD:
        raise AccountError("Это пароль по умолчанию — придумайте другой.")
    accounts.set_password(account.id, new,
                          keep_sessions=request.cookies.get(SESSION_COOKIE, ""))
    log.info("Пароль изменён: %s", account.username)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Управление учётными записями — только администратору
# ---------------------------------------------------------------------------

users_router = APIRouter(prefix="/api/users", tags=["Учётные записи"])


@users_router.get("", summary="Список учётных записей")
def list_users(request: Request,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    accounts = _accounts(request)
    return {"users": [a.to_dict() for a in accounts.list()],
            "default_username": DEFAULT_USERNAME,
            "default_password_in_use": accounts.uses_default_password()}


@users_router.post("", summary="Завести учётную запись")
def create_user(request: Request, payload: dict[str, Any] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    accounts = _accounts(request)
    account = accounts.create(
        str(payload.get("username") or "").strip(),
        str(payload.get("password") or ""),
        role=str(payload.get("role") or "user"),
        # Как пришло — проверяет `Accounts.create`: `str(...)` превращал
        # словарь в его запись, а `bool("false")` — в «да».
        display_name=payload.get("display_name") or "",
        group=payload.get("group") or "",
        must_change_password=payload.get("must_change_password", True))
    return account.to_dict()


@users_router.patch("/{user_id}", summary="Изменить учётную запись")
def update_user(user_id: str, request: Request, payload: dict[str, Any] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    accounts = _accounts(request)
    account = accounts.get(user_id)
    if account is None:
        raise AccountNotFound(f"Учётная запись {user_id} не найдена.")

    # Последнего действующего администратора нельзя ни разжаловать, ни
    # отключить: иначе управлять сервером станет некому, и восстанавливать
    # доступ придётся из консоли.
    # «Отключить» разбирается тем же правилом, что и при записи: строка
    # «false» здесь проходила мимо (`is False`), а запись её честно
    # выключала — и последний администратор отключался.
    выключают = "enabled" in payload and not поле_да_нет("enabled", payload["enabled"])
    losing_admin = (account.role == "admin"
                    and (payload.get("role") not in (None, "admin") or выключают))
    if losing_admin and accounts.admin_count() <= 1:
        raise ForbiddenError(
            "Это последний администратор — сервером станет некому управлять.",
            hint="Сначала заведите второго администратора.")

    fields: dict[str, Any] = {}
    for name in ("display_name", "role", "group", "enabled", "must_change_password"):
        if name in payload:
            fields[name] = payload[name]
    updated = accounts.update(user_id, **fields)

    # Пароль меняет администратор без знания старого — это сброс, а не смена.
    if payload.get("password"):
        accounts.set_password(user_id, str(payload["password"]),
                              must_change=поле_да_нет("must_change_password",
                                                      payload.get("must_change_password", True)))
        log.info("Пароль сброшен администратором: %s", updated.username)
        updated = accounts.get(user_id) or updated
    return updated.to_dict()


@users_router.delete("/{user_id}", summary="Удалить учётную запись")
def delete_user(user_id: str, request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    accounts = _accounts(request)
    account = accounts.get(user_id)
    if account is None:
        raise AccountNotFound(f"Учётная запись {user_id} не найдена.")
    if account.id == principal.user_id:
        raise ForbiddenError("Нельзя удалить учётную запись, под которой вы вошли.")
    if account.role == "admin" and accounts.admin_count() <= 1:
        raise ForbiddenError("Это последний администратор.",
                             hint="Сначала заведите второго администратора.")
    accounts.delete(user_id)
    log.info("Удалена учётная запись «%s»", account.username)
    return {"status": "ok", "username": account.username}


audit_router = APIRouter(prefix="/api/audit", tags=["Журнал доступа"])


@audit_router.get("", summary="Журнал доступа: кто, когда и что сделал")
def read_audit(request: Request,
               limit: int = Query(default=200, ge=1, le=2000),
               offset: int = Query(default=0, ge=0),
               since: float = Query(default=0.0, ge=0),
               until: float = Query(default=0.0, ge=0),
               actor: str = Query(default="", max_length=128),
               query: str = Query(default="", max_length=200),
               failed_only: bool = Query(default=False),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Строки журнала доступа с отбором, общим числом и списком участников.

    Только администратору. Журнал — это свидетельство о действиях людей, и
    открывать его тому, о ком он ведётся, значит подсказывать, что именно
    записано и чего в записи нет.
    """
    require_admin(principal)
    state = get_state(request)
    итог = state.db.audit_list(limit=limit, offset=offset, since=since,
                               until=until, actor=actor, query=query,
                               failed_only=failed_only)
    итог["actors"] = state.db.audit_actors()
    итог["enabled"] = bool(state.settings.get("audit_enabled", True))
    итог["reads"] = bool(state.settings.get("audit_reads", False))
    итог["limit"] = limit
    итог["offset"] = offset
    return итог
