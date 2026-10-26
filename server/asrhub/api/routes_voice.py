"""Голосовая аналитика за период: день, месяц, квартал, год.

Один маршрут на четыре раздела интерфейса — «за день», «за месяц», «за
квартал», «за год»: отчёт тот же, меняются масштаб и шаг по времени (см.
`voice_period`). Разрез по владельцу — как у остальных сводов: обычный ключ
видит свои записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .. import employees as справочник
from .. import voice_period as период
from .. import voice_problems as проблемы
from ..analytics import PERIODS
from ..errors import ASRHubError, ConfigError
from ..logging_setup import get_logger
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_write,
    scope_owner,
)

log = get_logger("asrhub.voice")

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


# ---------------------------------------------------------------------------
# Проблемы и сложности за период
# ---------------------------------------------------------------------------

#: Подписи скользящих окон раздела «Голосовая аналитика»: окно и база.
СКОЛЬЗЯЩИЕ = {
    "hour": ("последний час", "предыдущий час"),
    "day": ("последние сутки", "предыдущие сутки"),
    "week": ("последние 7 дней", "предыдущие 7 дней"),
    "month": ("последние 30 дней", "предыдущие 30 дней"),
    "quarter": ("последние 90 дней", "предыдущие 90 дней"),
    "year": ("последние 365 дней", "предыдущие 365 дней"),
    "all": ("всё время", ""),
}

def _п(имя: str) -> Any:
    """Описание параметра окна — свежим объектом на каждую ручку.

    Один и тот же `Query` в двух ручках делил бы состояние описания поля.
    """
    return {
        "scale": lambda: Query(default="", pattern="^(|day|month|quarter|year)$",
                               description="календарный период, как у разделов "
                                           "«за день» … «за год»"),
        "date": lambda: Query(default=None, max_length=10,
                              description="любой день периода, ГГГГ-ММ-ДД"),
        "period": lambda: Query(default="week",
                                pattern="^(hour|day|week|month|quarter|year|all)$",
                                description="скользящее окно до «сейчас», как у "
                                            "«Голосовой аналитики»"),
        "since": lambda: Query(default=None, ge=0,
                               description="начало скользящего окна, секунды эпохи"),
        "until": lambda: Query(default=None, ge=0,
                               description="конец скользящего окна, секунды эпохи"),
        "agent": lambda: Query(default="", max_length=64, description="только этот добавочный"),
        "department": lambda: Query(default="", max_length=200, description="только отдел"),
    }[имя]()


def _окна_проблем(db: Any, отбор: период.Отбор, *, scale: str, date: str | None,
                  period: str, since: float | None, until: float | None
                  ) -> tuple[проблемы.Окна, str]:
    """Окно сводки и база сравнения — календарные или скользящие.

    Календарный период — те же границы и та же база, что у отчёта за период
    (`voice_period`): пока период идёт, база обрезана «на ту же дату».
    Скользящее окно — как у «Голосовой аналитики»: N секунд до «сейчас»,
    база — такое же окно перед ним.
    """
    if scale:
        отчёт = период.Отчёт(db, scale, период.разобрать_дату(date), отбор)
        б = отчёт.базы[0] if отчёт.базы else None
        окна = проблемы.Окна(
            since=отчёт.окно.since, until=отчёт.окно.until, label=отчёт.окно.label,
            base_since=б.since if б else None, base_until=б.until if б else None,
            base_label=б.label if б else "", to_date=bool(б and б.to_date))
        return окна, f"{scale}:{отчёт.окно.first.isoformat()}"
    секунд = PERIODS.get(period, 7 * 86400)
    до = float(until) if until else time.time()
    от = float(since) if since is not None else ((до - секунд) if секунд else 0.0)
    подпись, подпись_базы = СКОЛЬЗЯЩИЕ.get(period, ("", ""))
    длина = до - от
    окна = проблемы.Окна(since=от, until=до, label=подпись)
    if секунд and от > 0 and длина > 0:
        окна.base_since, окна.base_until, окна.base_label = от - длина, от, подпись_базы
    return окна, f"period:{period}"


def _сохранённая(state: Any, ключ: str, сводка: dict[str, Any]) -> dict[str, Any] | None:
    """Сводка моделью, составленная раньше, — с пометкой, устарела ли она."""
    запись = state.db.get_kv(ключ)
    if not isinstance(запись, dict):
        return None
    устарела = (int(запись.get("conversations") or 0) != int(сводка.get("conversations") or 0)
                or int(запись.get("with_problems") or 0) != int(сводка.get("with_problems") or 0))
    return {**запись, "stale": устарела}


@router.get("/problems", summary="Проблемы и сложности разговоров за период")
def voice_problems(
    request: Request,
    scale: str = _п("scale"), date: str | None = _п("date"),
    period: str = _п("period"), since: float | None = _п("since"),
    until: float | None = _п("until"), agent: str = _п("agent"),
    department: str = _п("department"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    """Сводка по всем разговорам периода: какие проблемы и сколько.

    Окно — календарный период (`scale` и `date`, как у разделов «за день» …
    «за год») или скользящее (`period`, как у «Голосовой аналитики»).
    Каждая проблема — сколько разговоров задела, доля от тех, у кого она
    могла быть, изменение к базе с проверкой значимости, частые причины
    обращения и сотрудники; плюс разрезы «у кого больше всего проблемных
    разговоров» и «с какой причиной сложнее всего», технические сложности
    (без речи, короткие, ошибки, звук) и телефония. `summary` — главное
    фразами; `digest` — последняя сводка языковой моделью, если её
    составляли.
    """
    state = get_state(request)
    владелец = scope_owner(principal)
    отбор = период.Отбор(owner=владелец, agent=agent.strip(), department=department.strip())
    try:
        окна, ид = _окна_проблем(state.db, отбор, scale=scale, date=date, period=period,
                                 since=since, until=until)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    сводка = проблемы.посчитать(state.db, окна, отбор,
                                имена=bool(state.settings.get("employees_match_ext", True)))
    клиент = getattr(state, "llm", None)
    сводка["period_id"] = ид
    сводка["llm_enabled"] = bool(клиент is not None and клиент.enabled)
    сводка["digest"] = _сохранённая(state, проблемы.ключ_сводки(ид, отбор), сводка)
    return сводка


@router.post("/problems/digest", summary="Сводка проблем за период языковой моделью")
def voice_problems_digest(
    request: Request,
    scale: str = _п("scale"), date: str | None = _п("date"),
    period: str = _п("period"), since: float | None = _п("since"),
    until: float | None = _п("until"), agent: str = _п("agent"),
    department: str = _п("department"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    """Связный текст о проблемах периода: главное, проблемы с примерами,
    вероятные причины и что сделать.

    Модели дают перечень проблем с долями и изменениями, разрезы по
    причинам и сотрудникам и пересказы до сорока самых проблемных
    разговоров — а не расшифровки: так подсказка влезает в окно модели и
    на годе. Ответ хранится, и `GET /api/voice/problems` отдаёт его полем
    `digest` с пометкой `stale`, если с тех пор разговоров стало больше.
    """
    from ..llm.client import LLMError  # noqa: PLC0415
    from ..llm.tasks import МЕТКА, parse_json  # noqa: PLC0415

    state = get_state(request)
    require_write(principal)
    клиент = getattr(state, "llm", None)
    if клиент is None or not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена — сводку составить нечем.",
            hint="Задайте llm_backend, llm_url и llm_model в настройках "
                 "(раздел «Языковая модель»). Перечень проблем считается и без неё."))
    владелец = scope_owner(principal)
    отбор = период.Отбор(owner=владелец, agent=agent.strip(), department=department.strip())
    try:
        окна, ид = _окна_проблем(state.db, отбор, scale=scale, date=date, period=period,
                                 since=since, until=until)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    имена = bool(state.settings.get("employees_match_ext", True))
    сводка = проблемы.посчитать(state.db, окна, отбор, имена=имена)
    пересказы = проблемы.выборка(state.db, окна, отбор)
    if not сводка["with_problems"]:
        результат = {"overview": "За период нет разговоров с проблемами — сводить нечего.",
                     "problems": [], "causes": [], "recommendations": []}
    else:
        try:
            текст = клиент.chat(МЕТКА.format(kind="digest") + проблемы._СИСТЕМА,
                                проблемы.подсказка(сводка, пересказы),
                                kind="digest", validate=parse_json)
        except LLMError as exc:
            raise error_response(ASRHubError(
                f"Модель не ответила: {exc}",
                hint="Проверьте сервер модели: GET /api/llm/status и POST /api/llm/test.")
            ) from exc
        результат = проблемы.разобрать_сводку(текст)
    запись = {
        "period_id": ид, "label": окна.label,
        "window": {"since": окна.since or None, "until": окна.until},
        "filter": {"agent": отбор.agent, "department": отбор.department},
        "generated_at": time.time(), "model": клиент.model, "backend": клиент.backend,
        "conversations": сводка["conversations"], "with_problems": сводка["with_problems"],
        "sample": [р["job_id"] for р in пересказы], "result": результат,
    }
    state.db.set_kv(проблемы.ключ_сводки(ид, отбор), запись)
    state.db.add_event(None, "voice_digest",
                       f"Сводка проблем моделью: {окна.label or ид}",
                       {"period": ид, "conversations": сводка["conversations"]})
    return {**запись, "stale": False}


__all__ = ["router"]
