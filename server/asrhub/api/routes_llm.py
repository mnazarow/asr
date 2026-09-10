"""Маршруты смыслового слоя: /api/llm/* и /api/content/jobs/{id}/llm.

Состояние слоя и проба сервера модели, разбор одной записи по требованию,
запуск разбора архива и свод ответов за период: причины и исходы, действия
к исполнению, срабатывания умных трекеров, ответы скоркарты.

Разрез по владельцу тот же, что у аналитики записей: обычный ключ видит
свои записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..analytics import PERIODS
from ..errors import ASRHubError, ConfigError
from ..llm import LLMError, provision, tasks
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_owner,
    require_write,
    scope_owner,
)

router = APIRouter(prefix="/api", tags=["Языковая модель"])

ПЕРИОД = Query(default="week", pattern="^(hour|day|week|month|quarter|year|all)$")


def _установщик(request: Request) -> Any:
    """Установщик модели из состояния приложения."""
    state = get_state(request)
    if getattr(state, "llm_setup", None) is None:
        raise error_response(ASRHubError(
            "Установщик модели не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return state.llm_setup


def _slot(request: Request) -> tuple[Any, Any, Any]:
    state = get_state(request)
    if getattr(state, "llm", None) is None or getattr(state, "llm_worker", None) is None:
        raise error_response(ASRHubError(
            "Смысловой слой не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return state, state.llm, state.llm_worker


@router.get("/llm/status", summary="Состояние языковой модели")
def llm_status(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Как подключено, доступен ли сервер модели, учёт вызовов, очередь
    разбора и покрытие: сколько записей за сутки уже разобрано."""
    state, клиент, поток = _slot(request)
    since = time.time() - 86400
    свод = state.db.llm_stats(tasks.VERSION, since, owner=scope_owner(principal))
    разобрано = [р for р in свод["rows"] if not р.get("error") and р.get("version") == tasks.VERSION]
    return {
        **клиент.status(),
        "worker": поток.status(),
        "version": tasks.VERSION,
        "coverage": {"total": свод["total"], "analyzed": len(разобрано),
                     "errors": sum(1 for р in свод["rows"] if р.get("error")),
                     "share": round(len(разобрано) / свод["total"], 4) if свод["total"] else None},
    }


@router.post("/llm/test", summary="Проверить сервер модели")
def llm_test(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Короткий вызов мимо кеша: доступен ли сервер, знает ли модель,
    сколько ждать ответа. Для настройки — до включения разбора."""
    _, клиент, _ = _slot(request)
    require_admin(principal)
    проба = клиент.probe(fresh=True)
    if not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена.", hint="Задайте llm_backend, llm_url и llm_model."))
    начало = time.perf_counter()
    try:
        ответ = клиент.chat(
            "Отвечай одним объектом JSON.",
            'Ответь ровно так: {"ok": true, "lang": "ru"}',
            kind="test", use_cache=False)
    except LLMError as exc:
        return {**проба, "ok": False, "error": str(exc),
                "ms": round((time.perf_counter() - начало) * 1000, 1)}
    return {**проба, "ok": True, "answer": ответ[:200],
            "ms": round((time.perf_counter() - начало) * 1000, 1), "model": клиент.model}


@router.get("/content/jobs/{job_id}/llm", summary="Смысловой разбор записи")
def llm_job(request: Request, job_id: str,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, клиент, _ = _slot(request)
    try:
        job = state.queue.get(job_id)
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    результат = state.db.llm_get(job_id)
    return {"job_id": job_id, "result": результат, "enabled": клиент.enabled,
            "stale": bool(результат and результат.get("version") != tasks.VERSION),
            "version": tasks.VERSION}


@router.post("/content/jobs/{job_id}/llm", summary="Разобрать запись моделью сейчас")
def llm_job_run(request: Request, job_id: str,
                force: bool = Query(default=True),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Синхронно, в обработчике: ответ приходит вместе с результатом.
    Длинная запись — несколько вызовов; ждать столько, сколько задано
    llm_timeout_s на каждый."""
    state, клиент, поток = _slot(request)
    require_write(principal)
    try:
        job = state.queue.get(job_id)
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    if not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена.", hint="Задайте llm_backend, llm_url и llm_model."))
    if job.get("status") != "completed":
        raise error_response(ConfigError("Запись ещё не распознана."))
    try:
        результат = поток.analyze_job(job_id, force=force)
    except LLMError as exc:
        raise error_response(ASRHubError(
            f"Модель не ответила: {exc}",
            hint="Проверьте сервер модели: GET /api/llm/status и POST /api/llm/test.")) from exc
    return {"job_id": job_id, "result": результат, "version": tasks.VERSION}


@router.post("/llm/backfill", summary="Разобрать архив моделью")
def llm_backfill(request: Request, limit: int = Body(default=100, embed=True),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ставит в очередь разбора записи без ответа модели — новые первыми.
    Идут в фоне, по одной, уступая распознаванию."""
    state, клиент, поток = _slot(request)
    require_admin(principal)
    if not клиент.enabled:
        raise error_response(ConfigError("Языковая модель выключена."))
    ожидают = state.db.llm_pending(tasks.VERSION, limit=max(1, min(int(limit), 10000)))
    поставлено = поток.enqueue_many(з["id"] for з in ожидают)
    return {"queued": поставлено, "worker": поток.status()}


@router.get("/content/llm", summary="Свод ответов модели за период")
def llm_report(request: Request, period: str = ПЕРИОД,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Причины и исходы по закрытым спискам, доля решённых, действия к
    исполнению, срабатывания умных трекеров, ответы скоркарты — по
    записям периода с ответом модели."""
    state, клиент, поток = _slot(request)
    секунд = PERIODS.get(period, 7 * 86400)
    since = (time.time() - секунд) if секунд else 0.0
    свод = state.db.llm_stats(tasks.VERSION, since, owner=scope_owner(principal))
    строки = [р for р in свод["rows"] if not р.get("error")]
    распределения = tasks.summarize_for_digest(строки)
    трекеры: dict[str, dict[str, Any]] = {}
    for р in строки:
        for т in р.get("trackers") or []:
            запись = трекеры.setdefault(str(т.get("id")), {"id": str(т.get("id")),
                                                            "label": т.get("label"),
                                                            "checked": 0, "fired": 0})
            запись["checked"] += 1
            запись["fired"] += 1 if т.get("fired") else 0
    скоркарта: dict[str, dict[str, Any]] = {}
    for р in строки:
        for о in р.get("scorecard") or []:
            запись = скоркарта.setdefault(str(о.get("id")), {"id": str(о.get("id")),
                                                             "question": о.get("question"),
                                                             "да": 0, "нет": 0, "н/п": 0})
            запись[о.get("answer") if о.get("answer") in ("да", "нет", "н/п") else "н/п"] += 1
    действия = []
    for р in sorted(строки, key=lambda x: -float(x.get("created_at") or 0)):
        for д in р.get("actions") or []:
            действия.append({**д, "job_id": р["job_id"], "filename": р.get("filename"),
                             "created_at": р.get("created_at")})
            if len(действия) >= 50:
                break
        if len(действия) >= 50:
            break
    return {
        "period": period, "enabled": клиент.enabled, "model": клиент.model,
        "records": свод["total"], "analyzed": len(строки),
        "errors": sum(1 for р in свод["rows"] if р.get("error")),
        "stale": sum(1 for р in строки if р.get("version") != tasks.VERSION),
        "coverage": round(len(строки) / свод["total"], 4) if свод["total"] else None,
        "avg_latency_ms": round(sum(float(р.get("latency_ms") or 0) for р in строки)
                                / len(строки), 1) if строки else None,
        "off_list": sum(1 for р in строки
                        if any("вне списка" in str(з) for з in (р.get("warnings") or []))),
        **распределения,
        "trackers": sorted(трекеры.values(), key=lambda т: -т["fired"]),
        "scorecard": list(скоркарта.values()),
        "action_items": действия,
        "worker": поток.status(),
    }


# --- установка модели ---------------------------------------------------
#
# Смысловой слой без модели — это выключенный слой, а поставить модель
# руками значит зайти на сервер по ssh и выполнить полдюжины команд.
# Поэтому те же полдюжины команд собраны здесь: каталог с подбором под
# железо, установка, скачивание с процентами и запись настроек.


@router.get("/llm/models", summary="Каталог моделей и подбор под оборудование")
def llm_models(request: Request, refresh: bool = Query(default=False),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что можно поставить, что уже стоит и что поместится в память.

    Пометки считаются по свободной видеопамяти за вычетом запаса под
    распознавание: смысловой слой делит карту с главной работой сервера.
    При `refresh=true` размеры уточняются по реестру Ollama — это
    несколько секунд, поэтому по умолчанию берутся из каталога.
    """
    state = get_state(request)
    require_admin(principal)
    адрес = str(state.settings.get("llm_url") or provision.АДРЕС)
    скачанные = provision.установленные(адрес)
    свод = provision.подобрать(settings=state.settings,
                               installed=[м["name"] for м in скачанные])
    if refresh:
        for строка in свод["models"]:
            размер, ошибка = provision.размер_в_реестре(str(строка["name"]))
            строка["size_gb"] = размер if размер is not None else строка["size_gb"]
            строка["registry"] = ошибка or "ok"
    return {**свод, "installed": скачанные, "service": provision.служба(адрес),
            "backend": str(state.settings.get("llm_backend") or "off"),
            "active": str(state.settings.get("llm_model") or ""),
            "setup": _установщик(request).status()}


@router.post("/llm/setup", summary="Поставить и настроить модель")
def llm_setup(request: Request,
              models: list[str] = Body(default=[], embed=True),
              activate: str = Body(default="", embed=True),
              install_server: bool = Body(default=True, embed=True),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ставит Ollama (если её нет), скачивает модели, прогревает и
    включает выбранную. Идёт в фоне: ход установки — GET
    /api/llm/setup/status.

    Шаги, которые уже сделаны, пропускаются, поэтому повторный запуск на
    настроенном сервере просто докачивает ещё одну модель.
    """
    state = get_state(request)
    require_admin(principal)
    установщик = _установщик(request)
    выбор = [str(м) for м in (models or []) if str(м).strip()]
    if not выбор:
        # Ничего не выбрали — ставим то, что сами и советуем.
        свод = provision.подобрать(settings=state.settings)
        if not свод.get("recommended"):
            raise error_response(ConfigError(
                "Ни одна модель каталога не помещается в память этого сервера.",
                hint="Освободите видеопамять или выберите модель вручную."))
        выбор = [str(свод["recommended"])]
    try:
        return установщик.start(выбор, activate=(activate or None) if activate else выбор[0],
                                install_server=bool(install_server),
                                url=str(state.settings.get("llm_url") or provision.АДРЕС))
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/llm/setup/status", summary="Ход установки модели")
def llm_setup_status(request: Request,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Шаги, проценты и журнал последней установки."""
    require_admin(principal)
    return _установщик(request).status()


@router.post("/llm/setup/cancel", summary="Отменить установку модели")
def llm_setup_cancel(request: Request,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Останавливает установку на ближайшем шаге. Скачанное остаётся:
    Ollama продолжит с места обрыва при следующем запуске."""
    require_admin(principal)
    return _установщик(request).cancel()


@router.post("/llm/models/delete", summary="Удалить скачанную модель")
def llm_model_delete(request: Request, model: str = Body(..., embed=True),
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Убирает веса с диска. Включённую модель удалить нельзя — сначала
    выберите другую, иначе разбор останется без модели."""
    state = get_state(request)
    require_admin(principal)
    имя = str(model or "").strip()
    if имя and имя == str(state.settings.get("llm_model") or ""):
        raise error_response(ConfigError(
            f"Модель «{имя}» сейчас выбрана для разбора.",
            hint="Сначала выберите другую модель в настройках."))
    адрес = str(state.settings.get("llm_url") or provision.АДРЕС)
    try:
        provision.удалить(имя, адрес)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {"deleted": имя, "installed": provision.установленные(адрес)}
