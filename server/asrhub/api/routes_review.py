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


# ---------------------------------------------------------------------------
# Контроль качества работы операторов
# ---------------------------------------------------------------------------

@router.get("/qa", summary="Проверки качества: очередь и калибровка")
def qa_list(request: Request,
            status: str = Query(default="", pattern="^(|pending|done)$"),
            assigned_to: str = Query(default="", max_length=64),
            agent: str = Query(default="", max_length=128),
            limit: int = Query(default=100, ge=1, le=500),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Список проверок и сводка по проверяющим.

    Сводка здесь же, а не отдельным адресом: открывая очередь, руководитель
    видит и то, сколько он сам расходится с автоматом. Это единственный
    способ заметить собственный сдвиг — по чужому списку его не увидеть.
    """
    from .. import qa as qa_mod  # noqa: PLC0415

    состояние = get_state(request)
    свои = qa_mod.настройки(состояние.settings)
    return {
        "items": состояние.db.qa_list(status=status, assigned_to=assigned_to,
                                      agent=agent, limit=limit),
        "stats": состояние.db.qa_stats(),
        "overdue": len(qa_mod.просроченные(состояние.db)),
        "enabled": свои["enabled"],
        "daily": свои["daily"],
    }


@router.post("/qa", summary="Поставить запись на проверку")
def qa_assign(request: Request, job_id: str = Query(..., max_length=64),
              assigned_to: str = Query(default="", max_length=64),
              due_hours: float = Query(default=0.0, ge=0, le=720),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Назначает проверку вручную — поверх автоматической выборки."""
    from .. import qa as qa_mod  # noqa: PLC0415

    состояние = get_state(request)
    require_write(principal)
    задание = состояние.db.get_job(job_id)
    if not задание:
        raise error_response(ConfigError(f"Запись {job_id} не найдена."))
    разбор = состояние.db.get_content(job_id) or {}
    звонок = состояние.db.call_for_job(job_id) or {}
    свои = qa_mod.настройки(состояние.settings)
    срок = time.time() + (due_hours or свои["due_hours"]) * 3600.0
    ид = состояние.db.qa_assign(
        job_id, assigned_to=assigned_to or свои["assign_to"],
        assigned_by=principal.name, due_at=срок,
        agent=str(звонок.get("agent") or разбор.get("agent_speaker") or ""),
        auto_score=разбор.get("agent_score"), reason="назначено вручную")
    if ид is None:
        raise error_response(ConfigError(
            "Эта запись уже стоит на проверке.",
            hint="Две проверки одного разговора дают два балла, и дальше "
                 "начинается спор о том, какой из них настоящий."))
    return {"id": ид, "job_id": job_id, "due_at": срок}


@router.post("/qa/sample", summary="Набрать проверки сейчас")
def qa_sample(request: Request,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Набирает порцию записей на проверку, не дожидаясь суточного захода."""
    from .. import qa as qa_mod  # noqa: PLC0415

    состояние = get_state(request)
    require_admin(principal)
    назначено = qa_mod.набрать(состояние.db, состояние.settings,
                               assigned_by=principal.name)
    return {"assigned": len(назначено), "items": назначено}


@router.put("/qa/{review_id}", summary="Исход проверки качества")
def qa_submit(review_id: int, request: Request,
              данные: dict[str, Any] = Body(...),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записывает оценку человека рядом с оценкой автомата.

    Согласие считается само, когда о нём не сказали: расхождение больше
    десяти баллов из ста — это уже другая оценка, а не округление.
    """
    состояние = get_state(request)
    require_write(principal)
    балл = данные.get("score")
    if балл is not None:
        try:
            балл = float(балл)
        except (TypeError, ValueError):
            raise error_response(ConfigError("Балл должен быть числом.")) from None
        if not 0.0 <= балл <= 100.0:
            raise error_response(ConfigError("Балл — от нуля до ста."))
    записано = состояние.db.qa_submit(
        int(review_id), reviewer=str(данные.get("reviewer") or principal.name),
        score=балл, agree=данные.get("agree"), items=данные.get("items"),
        comment=str(данные.get("comment") or ""))
    if not записано:
        raise error_response(ConfigError(
            f"Проверка {review_id} не найдена или уже закрыта."))
    return {"status": "ok", "id": int(review_id)}
