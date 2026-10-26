"""Голосовая аналитика за период: день, месяц, квартал, год.

Один маршрут на четыре раздела интерфейса — «за день», «за месяц», «за
квартал», «за год»: отчёт тот же, меняются масштаб и шаг по времени (см.
`voice_period`). Разрез по владельцу — как у остальных сводов: обычный ключ
видит свои записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .. import employees as справочник
from .. import voice_period as период
from ..errors import ASRHubError
from .deps import Principal, authenticate, error_response, get_state, scope_owner

router = APIRouter(prefix="/api/voice", tags=["Аналитика"])

#: Сколько добавочных предлагает отбор раздела.
ДОБАВОЧНЫХ_В_ОТБОРЕ = 500


def _отделы(сотрудники: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Свод по отделам — из строк сотрудников, отдел которых знает справочник."""
    отделы: dict[str, dict[str, Any]] = {}
    for с in сотрудники:
        имя = str(с.get("agent_department") or "")
        if not имя:
            continue
        о = отделы.setdefault(имя, {
            "department": имя, "agents": 0, "jobs": 0, "completed": 0, "empty": 0,
            "failed": 0, "talk_hours": 0.0, "known": 0, "yes": 0, "scored": 0,
            "negative": 0, "score_sum": 0.0, "score_n": 0, "prev_completed": 0})
        о["agents"] += 1
        for поле in ("jobs", "completed", "empty", "failed", "scored", "negative",
                     "score_n", "prev_completed"):
            о[поле] += int(с.get(поле) or 0)
        о["talk_hours"] += float(с.get("talk_hours") or 0)
        о["known"] += int(с.get("resolved_known") or 0)
        о["yes"] += int(с.get("resolved_yes") or 0)
        о["score_sum"] += float(с.get("score_sum") or 0)
    строки = []
    for о in отделы.values():
        строки.append({
            "department": о["department"], "agents": о["agents"], "jobs": о["jobs"],
            "completed": о["completed"], "empty": о["empty"], "failed": о["failed"],
            "talk_hours": round(о["talk_hours"], 2),
            "resolved_share": round(о["yes"] / о["known"], 4) if о["known"] else None,
            "negative_share": round(о["negative"] / о["scored"], 4) if о["scored"] else None,
            "agent_score": round(о["score_sum"] / о["score_n"], 1) if о["score_n"] else None,
            "prev_completed": о["prev_completed"],
            "change": (round((о["completed"] - о["prev_completed"]) / о["prev_completed"], 4)
                       if о["prev_completed"] else None),
        })
    строки.sort(key=lambda с: (-с["completed"], с["department"]))
    return строки


def _варианты(state: Any, отчёт: период.Отчёт, owner: Any,
              имена: bool) -> dict[str, Any]:
    """Из чего выбирать в отборах раздела: добавочные периода и отделы."""
    без_отбора = период.Отбор(owner=owner)
    где, арг = без_отбора.где_звонков(state.db, отчёт.окно.since, отчёт.окно.until)
    добавочные = [{"agent": str(r["agent"]), "calls": int(r["n"] or 0)} for r in state.db.query(
        f"SELECT cl.agent AS agent, COUNT(*) AS n FROM calls cl WHERE {где} "
        "AND COALESCE(cl.agent,'') <> '' GROUP BY cl.agent ORDER BY n DESC, cl.agent LIMIT ?",
        [*арг, ДОБАВОЧНЫХ_В_ОТБОРЕ])]
    справочник.подписать_добавочные(state.db, добавочные, имена=имена)
    отделы = [str(r.get("department") or "") for r in state.db.employee_departments()]
    return {"agents": добавочные, "departments": [о for о in отделы if о]}


@router.get("/period", summary="Голосовая аналитика за день, месяц, квартал или год")
def voice_period(
    request: Request,
    scale: str = Query(default="month", pattern="^(day|month|quarter|year)$",
                       description="day, month, quarter или year"),
    date: str | None = Query(default=None, max_length=10,
                             description="Любой день периода, ГГГГ-ММ-ДД; пусто — сегодня"),
    agent: str = Query(default="", max_length=64, description="Только этот добавочный"),
    department: str = Query(default="", max_length=200,
                            description="Только отдел (по справочнику сотрудников)"),
    part: str = Query(default="", pattern="^(|fast|heavy)$",
                      description="fast — без трекеров и скоркарты, heavy — только они"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    """Сводный отчёт за календарный период со сравнением.

    Период — день, месяц, квартал или год, в который попадает `date`, по
    местному времени сервера. Базы сравнения: у дня — вчера и тот же день
    неделей раньше, у месяца и квартала — прошлый период и тот же период год
    назад, у года — прошлый год. Пока период идёт, базы обрезаются тем же
    числом прошедших секунд (`baselines[].to_date`).

    Отчёт приходит двумя частями, как и раздел «Голосовая аналитика»:
    `part=fast` — счётная (объём, состав, ряды по корзинам, тональность,
    причины и исходы с изменением долей, сотрудники, отделы, звонки,
    «коротко о периоде»), `part=heavy` — трекеры, скоркарта и обязательства:
    ради них читается каждый ответ модели окна. Без `part` — обе.

    Без речи и короткие входят в объём, но не в доли: тональность, решённые,
    причины и исходы считаются по разговорам с речью.
    """
    state = get_state(request)
    владелец = scope_owner(principal)
    имена = bool(state.settings.get("employees_match_ext", True))
    try:
        отчёт = период.Отчёт(
            state.db, scale, период.разобрать_дату(date),
            период.Отбор(owner=владелец, agent=agent.strip(), department=department.strip()))
    except ASRHubError as exc:
        raise error_response(exc) from exc
    if part == "heavy":
        return {**отчёт._шапка(), "part": "heavy", "lists": период.списки(state.db, отчёт)}
    ответ = отчёт.посчитать()
    if not ответ.get("future"):
        справочник.подписать_добавочные(state.db, ответ["agents"], имена=имена)
        ответ["departments"] = _отделы(ответ["agents"])
        ответ["summary"] = период.коротко(ответ)
    ответ["options"] = _варианты(state, отчёт, владелец, имена)
    if part == "fast":
        ответ["part"] = "fast"
    elif not ответ.get("future"):
        ответ["lists"] = период.списки(state.db, отчёт)
    return ответ


__all__ = ["router"]
