"""Маршруты ИИ-аналитики поверх ответов модели (заход 59).

* `GET /api/llm/commitments` — договорённости и их судьба по журналу звонков;
* `GET /api/llm/first-contact` — решён ли вопрос с первого обращения;
* `GET /api/llm/risks` — разговоры с риском ухода или эскалации;
* `GET /api/qc/insights` — сверка предсказанного NPS с опросом клиента и
  что снижает его оценку;
* `GET /api/llm/versions` — смены настройки модели (отметки на трендах);
* золотой набор: `GET/PUT/DELETE /api/llm/golden…`, прогон
  `POST /api/llm/golden/run`, прогоны `GET /api/llm/golden/runs`;
* `POST /api/llm/ask` — вопрос к архиву;
* `GET /api/llm/search` — поиск по смыслу, `POST /api/llm/search/index` —
  векторы для разобранных записей;
* `GET/POST /api/content/agents/{key}/plan` — план обучения оператора;
* тренажёр оператора: `/api/llm/trainer…`.

Разрез по владельцу тот же, что у остальной аналитики: обычный ключ видит
свои записи, администратор — все.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..analytics import PERIODS
from ..errors import ASRHubError, ConfigError, ForbiddenError
from ..llm import LLMError, insight, semantic
from ..llm import ask as спрос
from ..llm import golden as золото
from ..llm import trainer as тренажёр
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_owner,
    require_write,
    same_scope,
    scope_owner,
)

router = APIRouter(prefix="/api", tags=["ИИ-аналитика"])

ПЕРИОД = Query(default="week", pattern="^(hour|day|week|month|quarter|year|all)$")
ПО_КОМУ = Query(default="agent", pattern="^(speaker|owner|agent|queue|station)$")


def _окно(period: str, since: float | None, until: float | None) -> tuple[float, float]:
    сейчас = time.time()
    конец = сейчас if until is None else min(float(until), сейчас)
    секунд = PERIODS.get(period, 7 * 86400)
    начало = float(since) if since is not None else ((конец - секунд) if секунд else 0.0)
    return начало, конец


def _клиент(request: Request, *, нужен: bool = True) -> tuple[Any, Any]:
    state = get_state(request)
    клиент = getattr(state, "llm", None)
    if нужен and (клиент is None or not клиент.enabled):
        raise error_response(ConfigError(
            "Языковая модель выключена.",
            hint="Задайте llm_backend, llm_url и llm_model в настройках."))
    return state, клиент


def _модель_ответила(exc: LLMError) -> Any:
    return error_response(ASRHubError(
        f"Модель не ответила: {exc}",
        hint="Проверьте сервер модели: GET /api/llm/status и POST /api/llm/test."))


# ---------------------------------------------------------------------------
# Своды поверх ответов модели
# ---------------------------------------------------------------------------

@router.get("/llm/commitments", summary="Договорённости и их судьба")
def commitments(request: Request, period: str = ПЕРИОД,
                since: float | None = Query(default=None, ge=0),
                until: float | None = Query(default=None, ge=0),
                agent: str = Query(default="", max_length=64),
                limit: int = Query(default=200, ge=1, le=1000),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Договорённости сотрудника из ответов модели — выполнено, ждёт, просрочено.

    Проверяется по журналу звонков обещание перезвонить: был ли после
    разговора звонок с номером клиента до срока с запасом
    `llm_commitment_grace_hours`. Остальное — «не проверяется по журналу».
    """
    state = get_state(request)
    начало, конец = _окно(period, since, until)
    from .. import employees as справочник  # noqa: PLC0415

    ответ = insight.договорённости(state.db, state.settings, since=начало, until=конец,
                                   owner=scope_owner(principal), agent=agent.strip(),
                                   limit=limit)
    справочник.подписать_добавочные(
        state.db, ответ["items"],
        имена=bool(state.settings.get("employees_match_ext", True)))
    return {**ответ, "period": period, "window": {"since": начало or None, "until": конец}}


@router.get("/llm/first-contact", summary="Решение с первого обращения")
def first_contact(request: Request, period: str = ПЕРИОД,
                  since: float | None = Query(default=None, ge=0),
                  until: float | None = Query(default=None, ge=0),
                  agent: str = Query(default="", max_length=64),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Модель сказала «решён» — а клиент позвонил снова с той же причиной?

    Повтор — входящий звонок с того же номера в следующие `llm_fcr_days`
    дней, причина по ответу модели та же. `fcr_share` — доля решённых с
    первого раза по факту, `model_share` — по словам модели.
    """
    state = get_state(request)
    начало, конец = _окно(period, since, until)
    ответ = insight.первое_обращение(state.db, state.settings, since=начало, until=конец,
                                     owner=scope_owner(principal), agent=agent.strip())
    return {**ответ, "period": period, "window": {"since": начало or None, "until": конец}}


@router.get("/llm/risks", summary="Риск ухода и эскалации")
def risks(request: Request, period: str = ПЕРИОД,
          since: float | None = Query(default=None, ge=0),
          until: float | None = Query(default=None, ge=0),
          agent: str = Query(default="", max_length=64),
          limit: int = Query(default=50, ge=1, le=500),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Разговоры с высоким и средним риском — балл 0–100 и его причины.

    Балл складывается из признаков, которые уже посчитаны: вопрос не решён,
    трекер угрозы (суд, жалоба, уход), недовольство к концу, повторное
    обращение (по словам или по номеру), напряжение, раздражение. Высокий
    — от `llm_risk_threshold`.
    """
    state = get_state(request)
    начало, конец = _окно(period, since, until)
    ответ = insight.риски(state.db, state.settings, since=начало, until=конец,
                          owner=scope_owner(principal), agent=agent.strip(), limit=limit)
    return {**ответ, "period": period, "window": {"since": начало or None, "until": конец}}


@router.get("/qc/insights", summary="Опрос клиента: сверка прогноза и драйверы оценки")
def qc_insights(request: Request, period: str = Query(default="quarter",
                                                      pattern="^(week|month|quarter|year|all)$"),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Предсказанный NPS против оценки клиента из обратного звонка — и что
    сопутствует низкой оценке: причины, исходы, трекеры, ответы скоркарты.

    `calibration.suggested_shift` — поправка к предсказанию в баллах шкалы
    NPS (`content_nps_shift`), если опросов достаточно; прибавляется к уже
    действующей.
    """
    state = get_state(request)
    начало, конец = _окно(period, None, None)
    return {**insight.опросы_клиентов(state.db, state.settings, since=начало, until=конец,
                                      owner=scope_owner(principal)),
            "period": period}


@router.get("/llm/versions", summary="Смены настройки модели")
def versions(request: Request, since: float = Query(default=0.0, ge=0),
             until: float | None = Query(default=None, ge=0),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Когда и что менялось в настройке модели — для отметок на трендах."""
    state = get_state(request)
    return {"items": state.db.llm_versions_list(since, until)}


# ---------------------------------------------------------------------------
# Золотой набор
# ---------------------------------------------------------------------------

def _своя_запись(state: Any, principal: Principal, job_id: str) -> dict[str, Any]:
    задание = state.db.get_job(job_id)
    if задание is None:
        raise error_response(ConfigError(f"Запись {job_id} не найдена."))
    try:
        require_owner(principal, задание)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return задание


@router.get("/llm/golden", summary="Золотой набор: эталоны и точность")
def golden_list(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записи с ответами человека и точность текущих ответов модели по ним."""
    state = get_state(request)
    владелец = scope_owner(principal)
    записи = state.db.golden_list(owner=владелец)
    for з in записи:
        ответ = state.db.llm_get(з["job_id"]) or {}
        if ответ and not ответ.get("error"):
            сравнение = золото.сравнить(з["answers"], ответ)
            з["model"] = {к: ответ.get(к) for к in ("reason", "outcome", "resolved")}
            з["ok"] = sum(с["ok"] for с in сравнение.values())
            з["n"] = sum(с["n"] for с in сравнение.values())
    return {"items": записи, "current": золото.точность_текущих(state.db, owner=владелец),
            "runs": state.db.golden_runs(10), "overridable": list(золото.ПОДМЕНА)}


@router.put("/llm/golden/{job_id}", summary="Эталон человека по записи")
def golden_put(request: Request, job_id: str, данные: dict[str, Any] = Body(...),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ответы человека: `reason`, `outcome`, `resolved`, `trackers`
    ({id: true|false}), `scorecard` ({id: «да|нет|н/п»}), `fields` ({id: значение})."""
    state = get_state(request)
    require_write(principal)
    _своя_запись(state, principal, job_id)
    эталон = золото.эталон(данные)
    if not эталон:
        raise error_response(ConfigError(
            "В эталоне нет ни одного ответа.",
            hint="Задайте хотя бы причину, исход или «решён ли вопрос»."))
    state.db.golden_put(job_id, эталон, note=str(данные.get("note") or ""), by=principal.name)
    return {"job_id": job_id, "answers": эталон}


@router.delete("/llm/golden/{job_id}", summary="Убрать запись из золотого набора")
def golden_delete(request: Request, job_id: str,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    _своя_запись(state, principal, job_id)
    return {"deleted": state.db.golden_delete(job_id)}


@router.post("/llm/golden/run", summary="Прогнать золотой набор")
def golden_run(request: Request, данные: dict[str, Any] = Body(default={}),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Разбор записей набора заново — с текущими настройками или с подменой.

    `overrides` — что подменить на время прогона (модель, списки, трекеры,
    скоркарту, добавку к подсказке…); настройки сервера не меняются.
    Прогон идёт в фоне; ход — `GET /api/llm/golden/runs`.
    """
    state, _ = _клиент(request)
    require_admin(principal)
    подмена = данные.get("overrides") or {}
    if not isinstance(подмена, dict):
        raise error_response(ConfigError("overrides — объект {параметр: значение}."))
    лишние = sorted(set(подмена) - set(золото.ПОДМЕНА))
    if лишние:
        raise error_response(ConfigError(
            f"Подменить на время прогона нельзя: {', '.join(лишние)}.",
            hint="Можно: " + ", ".join(золото.ПОДМЕНА) + "."))
    if not state.db.golden_list(limit=1):
        raise error_response(ConfigError(
            "Золотой набор пуст.",
            hint="Отметьте ответы человека хотя бы у нескольких записей (PUT /api/llm/golden/{job_id})."))
    номер = золото.запустить(state, overrides=подмена,
                             label=str(данные.get("label") or "")[:120], by=principal.name)
    return {"id": номер, "status": "идёт"}


@router.get("/llm/golden/runs", summary="Прогоны золотого набора")
def golden_runs(request: Request, limit: int = Query(default=20, ge=1, le=100),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    return {"items": state.db.golden_runs(limit)}


# ---------------------------------------------------------------------------
# Вопрос к архиву и поиск по смыслу
# ---------------------------------------------------------------------------

@router.post("/llm/ask", summary="Вопрос к архиву")
def ask(request: Request, данные: dict[str, Any] = Body(...),
        principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Вопрос обычными словами по записям отбора — ответ со ссылками на записи.

    Тело: `question`, `period` (по умолчанию неделя) или `since`/`until`,
    `agent`, `reason`, `outcome`, `limit` (до 300 записей). Отвечает
    модель по готовым пересказам, а не по расшифровкам.
    """
    state, клиент = _клиент(request)
    require_write(principal)
    period = str(данные.get("period") or "week")
    if period not in PERIODS and period != "all":
        raise error_response(ConfigError(f"Неизвестный период «{period}»."))
    try:
        начало, конец = _окно(period, данные.get("since"), данные.get("until"))
        return спрос.спросить(
            state.db, клиент, state.settings, str(данные.get("question") or ""),
            since=начало, until=конец, owner=scope_owner(principal),
            agent=str(данные.get("agent") or "").strip()[:64],
            reason=str(данные.get("reason") or "")[:200],
            outcome=str(данные.get("outcome") or "")[:200],
            limit=int(данные.get("limit") or спрос.ЗАПИСЕЙ))
    except (TypeError, ValueError) as exc:
        raise error_response(ConfigError(f"Негодное значение в запросе: {exc}")) from exc
    except LLMError as exc:
        raise _модель_ответила(exc) from exc


@router.get("/llm/search", summary="Поиск по смыслу")
def search(request: Request, q: str = Query(..., min_length=2, max_length=300),
           period: str = Query(default="all", pattern="^(day|week|month|quarter|year|all)$"),
           limit: int = Query(default=20, ge=1, le=100),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записи, близкие к запросу по смыслу, а не по словам (`llm_embeddings`)."""
    state, клиент = _клиент(request)
    if not state.settings.get("llm_embeddings", False):
        raise error_response(ConfigError(
            "Поиск по смыслу выключен.",
            hint="Включите llm_embeddings и задайте модель векторов llm_embed_model "
                 "(у Ollama — nomic-embed-text или bge-m3)."))
    начало, конец = _окно(period, None, None)
    try:
        ответ = semantic.найти(state.db, клиент, q, owner=scope_owner(principal),
                               since=начало or None, until=конец, limit=limit)
    except LLMError as exc:
        raise _модель_ответила(exc) from exc
    ответ["index"] = state.db.vectors_stats()
    return ответ


_ИНДЕКС_ЗАМОК = threading.Lock()


@router.post("/llm/search/index", summary="Посчитать векторы смысла для архива")
def search_index(request: Request, limit: int = Query(default=500, ge=1, le=20000),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Векторы для записей, разобранных моделью, у которых их нет. В фоне."""
    state, клиент = _клиент(request)
    require_admin(principal)
    if not state.settings.get("llm_embeddings", False):
        raise error_response(ConfigError("Поиск по смыслу выключен (llm_embeddings)."))
    if not _ИНДЕКС_ЗАМОК.acquire(blocking=False):
        return {"started": False, "reason": "векторы уже считаются",
                "index": state.db.vectors_stats()}

    def считать() -> None:
        try:
            осталось = limit
            while осталось > 0:
                итог = semantic.дополнить(state.db, клиент, state.settings,
                                          limit=min(50, осталось))
                осталось -= итог["indexed"] + итог["errors"]
                if not итог["indexed"]:
                    break
        finally:
            _ИНДЕКС_ЗАМОК.release()

    threading.Thread(target=считать, name="asrhub-llm-index", daemon=True).start()
    return {"started": True, "limit": limit, "index": state.db.vectors_stats()}


# ---------------------------------------------------------------------------
# План обучения оператора
# ---------------------------------------------------------------------------

@router.get("/content/agents/{key}/plan", summary="План обучения оператора")
def plan_get(request: Request, key: str, by: str = ПО_КОМУ,
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Последний составленный план; `plan: null` — ещё не составляли."""
    state = get_state(request)
    план = спрос.сохранённый_план(state.db, by=by, key=key)
    if план and not principal.is_admin and scope_owner(principal):
        # План составлен по записям того, кто его просил; чужой ключ видит
        # только факт, что план есть.
        if not same_scope(principal, str(план.get("owner") or "")):
            план = {"by": by, "key": key, "created_at": план.get("created_at"), "hidden": True}
    return {"plan": план}


@router.post("/content/agents/{key}/plan", summary="Составить план обучения оператора")
def plan_make(request: Request, key: str, by: str = ПО_КОМУ, period: str = ПЕРИОД,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сильные стороны, слабые места и что потренировать — по разговорам
    оператора за период, со ссылками на записи. Составляет модель."""
    state, клиент = _клиент(request)
    require_write(principal)
    начало, конец = _окно(period, None, None)
    try:
        план = спрос.план_обучения(state.db, клиент, by=by, key=key, since=начало,
                                   until=конец, owner=scope_owner(principal),
                                   автор=principal.name)
    except LLMError as exc:
        raise _модель_ответила(exc) from exc
    return {"plan": план}


# ---------------------------------------------------------------------------
# Тренажёр оператора
# ---------------------------------------------------------------------------

def _сессия(state: Any, principal: Principal, ид: str) -> dict[str, Any]:
    сессия = тренажёр.получить(state.db, ид)
    if сессия is None:
        raise error_response(ConfigError(
            "Тренировка не найдена или закончилась больше суток назад."))
    if not principal.is_admin and str(сессия.get("owner") or "") != principal.name:
        raise error_response(ForbiddenError("Это чужая тренировка."))
    return сессия


def _наружу(сессия: dict[str, Any]) -> dict[str, Any]:
    return {к: сессия.get(к) for к in ("id", "job_id", "reason", "history", "done",
                                       "client_done", "started_at", "finished_at", "result")}


@router.get("/llm/trainer/scenarios", summary="Сценарии тренажёра")
def trainer_scenarios(request: Request,
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Разговоры архива, годные в сценарий (разобраны моделью, причина известна),
    и причины обращения из настроек — для сценария без записи."""
    state = get_state(request)
    return {"items": тренажёр.сценарии(state.db, owner=scope_owner(principal)),
            "reasons": [str(x) for x in (state.settings.get("llm_reasons") or [])]}


@router.post("/llm/trainer", summary="Начать тренировку")
def trainer_start(request: Request, данные: dict[str, Any] = Body(default={}),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сценарий — запись архива (`job_id`) или причина обращения (`reason`)."""
    state, клиент = _клиент(request)
    require_write(principal)
    ид = str(данные.get("job_id") or "").strip()
    if ид:
        _своя_запись(state, principal, ид)
    try:
        сессия = тренажёр.начать(state.db, клиент, job_id=ид,
                                 reason=str(данные.get("reason") or "")[:200],
                                 owner=principal.name)
    except LLMError as exc:
        raise _модель_ответила(exc) from exc
    return _наружу(сессия)


@router.post("/llm/trainer/{session_id}/reply", summary="Реплика оператора в тренировке")
def trainer_reply(request: Request, session_id: str, данные: dict[str, Any] = Body(...),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, клиент = _клиент(request)
    require_write(principal)
    сессия = _сессия(state, principal, session_id)
    try:
        сессия = тренажёр.ответить(state.db, клиент, сессия, str(данные.get("text") or ""))
    except LLMError as exc:
        raise error_response(ConfigError(str(exc))) from exc
    return _наружу(сессия)


@router.post("/llm/trainer/{session_id}/finish", summary="Закончить тренировку и оценить")
def trainer_finish(request: Request, session_id: str,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, клиент = _клиент(request)
    require_write(principal)
    сессия = _сессия(state, principal, session_id)
    try:
        сессия = тренажёр.закончить(state.db, клиент, state.settings, сессия)
    except LLMError as exc:
        raise error_response(ConfigError(str(exc))) from exc
    return _наружу(сессия)
