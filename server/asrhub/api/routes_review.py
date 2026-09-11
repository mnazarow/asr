"""Маршруты здоровья распознавания: очередь ручной проверки и контрольные
прогоны второй моделью — /api/review/* и /api/control/*.

Отдельно от аналитики, потому что здесь есть действия, а не только
чтение: человек закрывает строку очереди, администратор запускает отбор
или контрольный прогон вручную, не дожидаясь суточного захода.

Разрез по владельцу тот же, что у списка заданий: обычный ключ видит и
меняет только свои записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from .. import review
from ..errors import ASRHubError, ConfigError, JobNotFound
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

router = APIRouter(prefix="/api", tags=["Здоровье распознавания"])

ПЕРИОД = Query(default="week", pattern="^(hour|day|week|month|quarter|year|all)$")


def _owned(request: Request, job_id: str, principal: Principal) -> dict[str, Any]:
    state = get_state(request)
    try:
        job = state.queue.get(job_id)
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return job


@router.get("/review", summary="Очередь ручной проверки")
def review_list(request: Request, status: str = Query(default="pending",
                                                     pattern="^(pending|done|skipped|all)$"),
                limit: int = Query(default=100, ge=1, le=1000),
                offset: int = Query(default=0, ge=0),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записи, отобранные на прослушивание, с исходом; счётчики — за неделю.

    Строка закрывается сама, когда по записи задан эталон (вкладка «Эталон»
    карточки или POST /api/jobs/{id}/reference); пропустить можно вручную.
    """
    state = get_state(request)
    owner = scope_owner(principal)
    items = state.db.review_list(status=None if status == "all" else status,
                                 owner=owner, limit=limit, offset=offset)
    return {
        "items": items,
        "counts": state.db.review_counts(since=time.time() - 7 * 86400,
                                        owner=owner),
        "enabled": bool(state.settings.get("review_enabled", True)),
        "last_sampled_at": state.db.get_kv("review_sampled_at"),
        "reasons": {"random": "случайная выборка",
                    "low_confidence": "нижняя четверть по уверенности",
                    "manual": "добавлена вручную", "bad_audio": "плохой звук"},
    }


@router.post("/review/sample", summary="Пополнить очередь проверки сейчас")
def review_sample(request: Request,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    итог = review.sample_review(state.db, state.settings)
    state.db.set_kv("review_sampled_at", time.time())
    return итог


@router.post("/review/{job_id}", summary="Добавить запись в очередь проверки")
def review_add(request: Request, job_id: str,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    _owned(request, job_id, principal)
    return {"job_id": job_id, "added": state.db.review_add(job_id, "manual")}


@router.put("/review/{job_id}", summary="Исход проверки записи")
def review_update(request: Request, job_id: str,
                  status: str = Body(embed=True),
                  note: str = Body(default="", embed=True),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    _owned(request, job_id, principal)
    if status not in state.db.REVIEW_STATUSES:
        raise error_response(ConfigError(
            f"Недопустимый исход «{status}».",
            hint="Допустимо: done — проверено, skipped — пропущено, pending — вернуть."))
    if not state.db.review_update(job_id, status, reviewer=principal.name, note=note):
        raise error_response(JobNotFound(f"Записи «{job_id}» нет в очереди проверки."))
    return {"job_id": job_id, "status": status}


@router.get("/control", summary="Согласие моделей по контрольным прогонам")
def control_report(request: Request, period: str = ПЕРИОД,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    return state.analytics.agreement(period, owner=scope_owner(principal))


@router.post("/control/run", summary="Поставить контрольные прогоны сейчас")
def control_run(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    if not str(state.settings.get("control_model") or "").strip():
        raise error_response(ConfigError(
            "Контрольная модель не задана.",
            hint="Укажите control_model в настройках — модель другого семейства."))
    итог = review.sample_control(state.db, state.settings, state.queue)
    state.db.set_kv("control_sampled_at", time.time())
    return итог
