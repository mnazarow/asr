"""Вход по логину и паролю и управление учётными записями.

Ключи доступа этими маршрутами не затрагиваются: они как были, так и
остаются — для программ. Здесь всё про людей, открывающих веб-интерфейс.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Request, Response

from ..accounts import DEFAULT_PASSWORD, DEFAULT_USERNAME, AccountError, AccountNotFound
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
        max_age=int(max(0, expires - request.scope.get("_now", 0)) or 0) or None,
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
    accounts = _accounts(request)
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username or not password:
        raise AuthError("Введите логин и пароль.")
    account = accounts.authenticate(username, password)
    token, expires = accounts.open_session(
        account.id,
        user_agent=request.headers.get("user-agent", ""),
        address=(request.client.host if request.client else ""))
    _set_cookie(request, response, token, expires)
    log.info("Вход: %s", account.username)
    return {
        "user": account.to_dict(),
        "expires_at": expires,
        "must_change_password": account.must_change_password,
    }


@router.post("/logout", summary="Выход")
def logout(request: Request, response: Response) -> dict[str, Any]:
    accounts = _accounts(request)
    accounts.close_session(request.cookies.get(SESSION_COOKIE, ""))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "ok"}


@router.get("/me", summary="Кто я")
def me(request: Request,
       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сведения о текущем входе — их спрашивает интерфейс при загрузке."""
    state = get_state(request)
    data: dict[str, Any] = {
        "name": principal.name,
        "role": principal.role,
        "group": principal.group,
        "kind": "user" if principal.user_id else "key",
        "must_change_password": principal.must_change_password,
    }
    if principal.is_admin and state.accounts is not None:
        # Предупреждение про пароль по умолчанию видит только администратор:
        # остальным оно ничего не даёт, а подсказывает лишнее.
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
        display_name=str(payload.get("display_name") or ""),
        group=str(payload.get("group") or ""),
        must_change_password=bool(payload.get("must_change_password", True)))
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
    losing_admin = (account.role == "admin"
                    and (payload.get("role") not in (None, "admin")
                         or payload.get("enabled") is False))
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
                              must_change=bool(payload.get("must_change_password", True)))
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
