"""Маршруты раздела «Резервные копии»: снять, посмотреть, вернуть, убрать.

Всё здесь — только администратору, и не из осторожности вообще, а по делу:
в копии настроек лежат пароли к АТС, ключи доступа и токены, а
восстановление данных подменяет базу целиком. Скачивание копии — это
выдача наружу всех секретов сервера одним файлом, поэтому оно тоже здесь.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile
from fastapi.responses import FileResponse

from .. import backup
from ..errors import ASRHubError, ConfigError
from .deps import Principal, authenticate, error_response, get_state, require_admin, require_write

router = APIRouter(prefix="/api/backup", tags=["Резервные копии"])

ВИД = Query(default="", pattern="^(|full|settings)$")


@router.get("", summary="Копии и состояние резервного копирования")
def список_копий(request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что снято, когда, чем занято место и что говорит расписание."""
    require_admin(principal)
    state = get_state(request)
    try:
        return backup.свод(state.settings)
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.post("", summary="Снять копию прямо сейчас")
def снять(request: Request, kind: str = ВИД,
          данные: dict[str, Any] | None = Body(default=None),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Снимает копию выбранного вида, не дожидаясь расписания.

    Вид берётся из запроса, а не из настройки: «снять копию настроек перед
    тем, как что-то менять» — самая частая причина нажать эту кнопку, и
    заставлять ради неё переключать расписание было бы издевательством.
    """
    require_admin(require_write(principal))
    state = get_state(request)
    тело = данные or {}
    вид = kind or str(тело.get("kind") or state.settings.get("backup_kind") or "full")
    try:
        return backup.создать(state.db, state.settings, kind=вид,
                              comment=str(тело.get("comment") or "вручную"),
                              include_results=тело.get("include_results"))
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.post("/restore", summary="Восстановить из копии")
def восстановить(request: Request, данные: dict[str, Any] = Body(...),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Возвращает настройки, а по требованию — и данные.

    `what`: `settings` — только параметры, применяются сразу; `full` —
    ещё и база, с подменой файла и требованием перезапуска. Прежняя база
    не удаляется, а остаётся рядом под именем `.before-restore-…`:
    восстановление не из той копии — обычная ошибка, и она обязана быть
    обратимой.
    """
    require_admin(require_write(principal))
    state = get_state(request)
    имя = str(данные.get("name") or "").strip()
    что = str(данные.get("what") or "settings")
    if not имя:
        raise error_response(ConfigError(
            "Не указано, из какой копии восстанавливать.",
            hint="Передайте name из списка копий."))
    try:
        итог = backup.восстановить(
            state.db, state.settings, имя, what=что,
            apply_settings=bool(данные.get("apply_settings", True)))
    except ASRHubError as exc:
        raise error_response(exc) from exc
    if итог.get("applied"):
        # Настройки применены на ходу — часть из них требует, чтобы их
        # разнесли по уже работающим узлам, иначе очередь и кэш моделей
        # останутся с прежними числами до перезапуска.
        _разнести(state)
    return итог


@router.delete("/{name}", summary="Убрать копию")
def убрать(request: Request, name: str,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(require_write(principal))
    state = get_state(request)
    try:
        return backup.удалить(state.settings, name)
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/{name}/file", summary="Скачать копию")
def скачать(request: Request, name: str,
            principal: Principal = Depends(authenticate)) -> FileResponse:
    """Отдаёт файл копии. Внутри секреты сервера — храните как пароль."""
    require_admin(principal)
    state = get_state(request)
    try:
        путь = backup._путь_копии(state.settings, name)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return FileResponse(путь, filename=путь.name,
                        media_type="application/gzip")


@router.post("/upload", summary="Загрузить копию с другой машины")
async def загрузить(request: Request, file: UploadFile = File(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Кладёт присланный архив в каталог копий — перенос на новый сервер."""
    require_admin(require_write(principal))
    state = get_state(request)
    try:
        return backup.принять(state.settings, file.filename or "", file.file)
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.post("/cleanup", summary="Убрать копии сверх срока хранения")
def подчистить(request: Request,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(require_write(principal))
    state = get_state(request)
    убрано = backup.подчистить(state.settings)
    return {"removed": убрано, "count": len(убрано)}


def _число(settings: Any, ключ: str, по_умолчанию: int) -> int:
    """Число из настройки, у которого ноль — значение, а не «не задано».

    У `model_idle_unload_s` ноль означает «не выгружать модели никогда», и
    `int(значение or 900)` включал автовыгрузку через пятнадцать минут
    после каждого восстановления настроек — при том что в интерфейсе
    по-прежнему стоял ноль.
    """
    значение = settings.get(ключ)
    if значение is None or значение == "":
        return по_умолчанию
    try:
        return max(0, int(значение))
    except (TypeError, ValueError):
        return по_умолчанию


def _разнести(state: Any) -> None:
    """Доводит применённые настройки до тех узлов, что держат их у себя."""
    try:
        state.queue.set_concurrency(max(1, _число(state.settings, "max_concurrent_jobs", 2)))
        state.registry.configure(max(1, _число(state.settings, "model_cache_size", 2)),
                                 _число(state.settings, "model_idle_unload_s", 900))
    except Exception:                                        # noqa: BLE001
        # Узла может не быть (сервер поднят урезанным) — настройки от этого
        # не перестают быть восстановленными.
        pass


__all__ = ["router"]
