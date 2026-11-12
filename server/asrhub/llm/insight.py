"""Своды поверх ответов модели: то, ради чего ответы собирали.

Ответ модели по одной записи — пересказ и отметки. Ценность появляется,
когда их складывают с тем, что сервер и так знает: кто оператор, какой
номер у клиента, звонил ли он снова, что сказал в опросе после разговора.
Здесь шесть таких сводов:

* **оценка оператора по ответам модели** — доля решённых, итог скоркарты с
  весами, ответы по каждому вопросу, договорённости без срока;
* **контроль договорённостей** — обещали перезвонить к сроку: был ли
  звонок этому номеру (журнал звонков);
* **решение с первого обращения** — модель сказала «решён», а клиент через
  день позвонил снова с той же причиной: значит, не решён;
* **сверка предсказанного NPS** с оценкой клиента из обратного звонка и
  поправка к предсказанию по своему архиву;
* **что снижает оценку клиента** — причины, исходы, трекеры и пункты
  скоркарты против оценки из опроса;
* **риск ухода и эскалации** — балл разговора из признаков, которые уже
  посчитаны, с перечнем причин.

Всё считается по требованию, по окну периода; модель здесь не
вызывается.
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import deadlines

#: Номер клиента считается по последним десяти цифрам — как в удалении по
#: требованию: «+7 916…», «8916…» и «916…» — один номер. Короче семи цифр
#: — внутренний номер, не клиент.
ЦИФР_НОМЕРА = 10
МИН_ЦИФР = 7


def номер(значение: Any) -> str:
    цифры = re.sub(r"\D", "", str(значение or ""))
    return цифры[-ЦИФР_НОМЕРА:] if len(цифры) >= МИН_ЦИФР else ""


def номер_клиента(строка: dict[str, Any]) -> str:
    """Номер клиента в звонке: у входящего — кто звонил, у исходящего — кому."""
    направление = str(строка.get("direction") or "")
    if направление == "входящий":
        return номер(строка.get("src"))
    if направление == "исходящий":
        return номер(строка.get("dst"))
    if направление == "внутренний":
        return ""
    # Направление неизвестно: клиент — та сторона, чей номер похож на городской.
    for поле in ("src", "dst"):
        н = номер(строка.get(поле))
        if len(н) >= ЦИФР_НОМЕРА:
            return н
    return ""


def _время(строка: dict[str, Any]) -> float:
    return float(строка.get("call_at") or строка.get("started_at")
                 or строка.get("created_at") or 0.0)


def _доля(часть: float, всего: float) -> float | None:
    return round(100.0 * часть / всего, 1) if всего else None


# ---------------------------------------------------------------------------
# Оценка оператора по ответам модели
# ---------------------------------------------------------------------------

def _пустой_свод() -> dict[str, Any]:
    return {"analyzed": 0, "known": 0, "yes": 0, "scores": [], "critical": 0,
            "questions": {}, "actions": 0, "undated": 0}


def _добавить(свод: dict[str, Any], р: dict[str, Any]) -> None:
    свод["analyzed"] += 1
    if р.get("resolved") is not None:
        свод["known"] += 1
        свод["yes"] += 1 if р["resolved"] else 0
    if р.get("score") is not None:
        свод["scores"].append(float(р["score"]))
    if р.get("critical"):
        свод["critical"] += 1
    for о in р.get("scorecard") or []:
        if not isinstance(о, dict):
            continue
        в = свод["questions"].setdefault(str(о.get("id")), {
            "id": str(о.get("id")), "question": о.get("question"), "да": 0, "нет": 0, "н/п": 0})
        ответ = о.get("answer") if о.get("answer") in ("да", "нет") else "н/п"
        в[ответ] += 1
    for д in р.get("actions") or []:
        if not isinstance(д, dict):
            continue
        if str(д.get("who") or "сотрудник").lower().startswith("клиент"):
            continue
        свод["actions"] += 1
        if not д.get("when"):
            свод["undated"] += 1


def _итог(свод: dict[str, Any]) -> dict[str, Any]:
    вопросы = []
    for в in свод["questions"].values():
        отвечено = в["да"] + в["нет"]
        вопросы.append({**в, "yes_share": _доля(в["да"], отвечено)})
    return {
        "analyzed": свод["analyzed"],
        "resolved_share": _доля(свод["yes"], свод["known"]),
        "score": (round(sum(свод["scores"]) / len(свод["scores"]), 1)
                  if свод["scores"] else None),
        "scored": len(свод["scores"]),
        "critical_failed": свод["critical"],
        "scorecard": вопросы,
        "actions": свод["actions"], "actions_undated": свод["undated"],
        "undated_share": _доля(свод["undated"], свод["actions"]),
    }


def оценка_операторов(db: Any, *, since: float, until: float | None = None,
                      owner: Any = None, by: str = "agent",
                      key: str | None = None) -> dict[str, Any]:
    """Своды ответов модели по операторам разреза `by` (и по команде)."""
    строки = db.llm_window_rows(since or 0.0, until=until, owner=owner, by=by)
    по_людям: dict[str, dict[str, Any]] = {}
    команда = _пустой_свод()
    for р in строки:
        ключ = str(р.get("group_key") or "—")
        if key is not None and ключ != str(key):
            _добавить(команда, р)
            continue
        _добавить(по_людям.setdefault(ключ, _пустой_свод()), р)
        _добавить(команда, р)
    return {"by": by, "items": {к: _итог(с) for к, с in по_людям.items()},
            "team": _итог(команда)}


# ---------------------------------------------------------------------------
# Звонки по номерам
# ---------------------------------------------------------------------------

def _звонки_по_номерам(db: Any, since: float, until: float) -> dict[str, list[dict[str, Any]]]:
    по_номерам: dict[str, list[dict[str, Any]]] = {}
    for з in db.calls_by_numbers(since, until):
        н = номер_клиента(з)
        if н:
            по_номерам.setdefault(н, []).append(з)
    return по_номерам


# ---------------------------------------------------------------------------
# Контроль договорённостей
# ---------------------------------------------------------------------------

СТАТУСЫ = {
    "overdue": "просрочено",
    "done": "выполнено",
    "waiting": "ждёт срока",
    "undated": "без срока",
    "unchecked": "не проверяется по журналу",
    "no_number": "нет номера клиента",
}


def договорённости(db: Any, settings: Any, *, since: float, until: float | None = None,
                   owner: Any = None, agent: str = "", now: float | None = None,
                   limit: int = 200) -> dict[str, Any]:
    """Обещания сотрудника с их судьбой: выполнено, ждёт срока, просрочено.

    Проверяется по журналу звонков то, что по нему видно, — обещание
    перезвонить: был ли после разговора звонок с этим номером (в любую
    сторону) до срока с запасом `llm_commitment_grace_hours`. Обещание без
    срока считается выполненным, если звонок был в пределах
    `llm_fcr_days`, и просроченным, если эти дни прошли без звонка.
    Остальное («отправить письмо») — «не проверяется», но со сроком.
    """
    сейчас = float(now if now is not None else time.time())
    try:
        запас = max(0.0, float(settings.get("llm_commitment_grace_hours", 12) or 0)) * 3600.0
    except (TypeError, ValueError):
        запас = 12 * 3600.0
    дней = _дни(settings)
    строки = db.llm_window_rows(since or 0.0, until=until, owner=owner, agent=agent)
    горизонт = min(сейчас, (until or сейчас) + 31 * 86400.0)
    звонки = _звонки_по_номерам(db, since or 0.0, горизонт + 1.0)
    пункты: list[dict[str, Any]] = []
    счёт = dict.fromkeys(СТАТУСЫ, 0)
    for р in строки:
        когда_разговор = _время(р)
        клиент = номер_клиента(р)
        for д in р.get("actions") or []:
            if not isinstance(д, dict) or not д.get("what"):
                continue
            if str(д.get("who") or "сотрудник").lower().startswith("клиент"):
                continue
            срок = deadlines.срок(д.get("when"), когда_разговор) if д.get("when") else None
            статус, звонок = _судьба(д, срок, клиент, когда_разговор, звонки, запас, дней,
                                     сейчас)
            счёт[статус] += 1
            пункты.append({
                "job_id": р["job_id"], "filename": р.get("filename"),
                "created_at": р.get("created_at"), "agent": р.get("agent") or "",
                "what": д.get("what"), "when": д.get("when"), "due": срок,
                "status": статус, "status_title": СТАТУСЫ[статус],
                "callback_at": (звонок or {}).get("started_at"),
                "callback_job_id": (звонок or {}).get("job_id"),
            })
    порядок = {"overdue": 0, "waiting": 1, "undated": 2, "no_number": 3, "unchecked": 4,
               "done": 5}
    пункты.sort(key=lambda п: (порядок[п["status"]], п.get("due") or 9e18,
                               -(п.get("created_at") or 0)))
    проверяемых = счёт["overdue"] + счёт["done"]
    return {"counts": счёт, "titles": СТАТУСЫ, "total": len(пункты),
            "kept_share": _доля(счёт["done"], проверяемых),
            "grace_hours": round(запас / 3600.0, 1), "days": дней,
            "items": пункты[:max(1, int(limit))]}


def _судьба(д: dict[str, Any], срок: float | None, клиент: str, когда: float,
            звонки: dict[str, list[dict[str, Any]]], запас: float, дней: int,
            сейчас: float) -> tuple[str, dict[str, Any] | None]:
    if not deadlines.про_звонок(str(д.get("what") or "") + " " + str(д.get("quote") or "")):
        return "unchecked", None
    if not клиент:
        return "no_number", None
    предел = (срок + запас) if срок is not None else (когда + дней * 86400.0)
    for з in звонки.get(клиент, []):
        начало = float(з.get("started_at") or 0)
        if когда + 60.0 < начало <= предел:
            return "done", з
    if сейчас > предел:
        return "overdue", None
    return ("waiting" if срок is not None else "undated"), None


# ---------------------------------------------------------------------------
# Решение с первого обращения
# ---------------------------------------------------------------------------

def _дни(settings: Any) -> int:
    try:
        return max(1, min(90, int(settings.get("llm_fcr_days", 7) or 7)))
    except (TypeError, ValueError):
        return 7


def первое_обращение(db: Any, settings: Any, *, since: float, until: float | None = None,
                     owner: Any = None, agent: str = "",
                     now: float | None = None) -> dict[str, Any]:
    """«Решён» по факту: клиент не позвонил снова с той же причиной за N дней.

    Модель видит один разговор и говорит «решён», если клиента устроил
    ответ. Решён ли вопрос на самом деле, видно по тому, позвонит ли он
    снова. Повтор — входящий звонок с того же номера в следующие
    `llm_fcr_days` дней с той же причиной по ответу модели; повтор, который
    модель ещё не разобрала, — «возможно».
    """
    сейчас = float(now if now is not None else time.time())
    дней = _дни(settings)
    строки = db.llm_window_rows(since or 0.0, until=until, owner=owner, agent=agent)
    конец = min(сейчас, (until or сейчас) + дней * 86400.0)
    звонки = _звонки_по_номерам(db, since or 0.0, конец + 1.0)
    причины = {р["job_id"]: р.get("reason")
               for р in db.llm_window_rows(since or 0.0, until=конец + 1.0, owner=owner)}
    известно = решено = вернулись = возможно = 0
    примеры: list[dict[str, Any]] = []
    for р in строки:
        if р.get("resolved") is None:
            continue
        известно += 1
        if not р["resolved"]:
            continue
        решено += 1
        клиент = номер_клиента(р)
        if not клиент:
            continue
        когда = _время(р)
        for з in звонки.get(клиент, []):
            начало = float(з.get("started_at") or 0)
            if not (когда + 60.0 < начало <= когда + дней * 86400.0):
                continue
            if str(з.get("direction") or "") == "исходящий" or з.get("job_id") == р["job_id"]:
                continue
            причина = причины.get(str(з.get("job_id") or ""))
            if причина is None:
                возможно += 1
                break
            if причина == р.get("reason"):
                вернулись += 1
                if len(примеры) < 50:
                    примеры.append({"job_id": р["job_id"], "filename": р.get("filename"),
                                    "reason": р.get("reason"), "created_at": р.get("created_at"),
                                    "repeat_job_id": з.get("job_id"),
                                    "repeat_at": начало})
                break
    return {
        "days": дней, "known": известно, "resolved_by_model": решено,
        "reopened": вернулись, "possible": возможно,
        "model_share": _доля(решено, известно),
        "fcr_share": _доля(решено - вернулись, известно),
        "items": примеры,
    }


# ---------------------------------------------------------------------------
# Опросы клиента: сверка предсказанного NPS и драйверы оценки
# ---------------------------------------------------------------------------

def _опросы(db: Any, since: float, until: float | None, owner: Any) -> list[dict[str, Any]]:
    своё, своё_args = db._owner_clause(owner, "j")  # noqa: SLF001
    где = ["q.kind='опрос'", "q.finished_at IS NOT NULL", "q.score_pct IS NOT NULL",
           "j.created_at >= ?"]
    args: list[Any] = [float(since or 0.0)]
    if until is not None:
        где.append("j.created_at < ?")
        args.append(float(until))
    if своё:
        где.append(своё)
        args.extend(своё_args)
    строки = db.query(
        "SELECT q.job_id, q.score, q.score_pct, q.finished_at, q.ext, "
        "       c.nps, c.nps_group, l.reason, l.outcome, l.resolved, l.trackers, l.scorecard "
        "FROM qc_surveys q JOIN jobs j ON j.id = q.job_id "
        "LEFT JOIN content c ON c.job_id = q.job_id "
        "LEFT JOIN llm_results l ON l.job_id = q.job_id AND COALESCE(l.error,'') = '' "
        f"WHERE {' AND '.join(где)}", args)
    import json  # noqa: PLC0415

    готово = []
    for r in строки:
        з = dict(r)
        for к in ("trackers", "scorecard"):
            try:
                з[к] = json.loads(з[к]) if isinstance(з.get(к), str) else (з.get(к) or [])
            except (TypeError, ValueError):
                з[к] = []
        готово.append(з)
    return готово


def _корреляция(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    return round(sxy / (sxx * syy) ** 0.5, 3)


#: Меньше стольких пар сверка не делает выводов: поправка по пяти опросам —
#: подгонка под пять клиентов.
МИН_ПАР = 10
#: Признак попадает в драйверы оценки, если встретился хотя бы столько раз.
МИН_ВСТРЕЧ = 3


def опросы_клиентов(db: Any, settings: Any, *, since: float, until: float | None = None,
                    owner: Any = None) -> dict[str, Any]:
    """Сверка предсказанного NPS с опросом и драйверы оценки клиента."""
    строки = _опросы(db, since, until, owner)
    пары = [(float(р["nps"]) * 10.0, float(р["score_pct"])) for р in строки
            if р.get("nps") is not None and р.get("score_pct") is not None]
    try:
        поправка_сейчас = float(settings.get("content_nps_shift", 0) or 0)
    except (TypeError, ValueError):
        поправка_сейчас = 0.0
    сверка: dict[str, Any] = {"pairs": len(пары), "enough": len(пары) >= МИН_ПАР,
                              "current_shift": поправка_сейчас}
    if пары:
        разницы = [факт - прогноз for прогноз, факт in пары]
        сдвиг = sum(разницы) / len(разницы)
        группы = {"критики (0–6)": (0, 6), "нейтральные (7–8)": (7, 8),
                  "промоутеры (9–10)": (9, 10)}
        сверка.update({
            "mae": round(sum(abs(р) for р in разницы) / len(разницы), 1),
            "bias": round(сдвиг, 1),
            "correlation": _корреляция([п for п, _ in пары], [ф for _, ф in пары]),
            # Поправка — в баллах шкалы NPS (0–10): средняя разница факта и
            # прогноза в процентах делённая на десять, не больше трёх баллов
            # в любую сторону. Прибавляется к уже действующей.
            "suggested_shift": (round(max(-3.0, min(3.0, поправка_сейчас + сдвиг / 10.0)), 1)
                                if len(пары) >= МИН_ПАР else None),
            "groups": [
                {"group": имя, "n": len(свои),
                 "actual": round(sum(свои) / len(свои), 1) if свои else None}
                for имя, (от, до) in группы.items()
                for свои in [[ф for п, ф in пары if от <= п / 10.0 <= до]]],
        })
    всего = [float(р["score_pct"]) for р in строки]
    средняя = sum(всего) / len(всего) if всего else None
    признаки: dict[str, list[float]] = {}

    def отметить(название: str, значение: float) -> None:
        признаки.setdefault(название, []).append(значение)

    for р in строки:
        оценка = float(р["score_pct"])
        if р.get("reason"):
            отметить(f"причина: {р['reason']}", оценка)
        if р.get("outcome"):
            отметить(f"исход: {р['outcome']}", оценка)
        if р.get("resolved") is not None:
            отметить("вопрос решён" if р["resolved"] else "вопрос не решён", оценка)
        for т in р.get("trackers") or []:
            if isinstance(т, dict) and т.get("fired"):
                отметить(f"трекер: {т.get('label') or т.get('id')}", оценка)
        for о in р.get("scorecard") or []:
            if isinstance(о, dict) and о.get("answer") == "нет":
                отметить(f"скоркарта «нет»: {о.get('question') or о.get('id')}", оценка)
    драйверы = []
    for название, оценки in признаки.items():
        if len(оценки) < МИН_ВСТРЕЧ or средняя is None:
            continue
        осталось = len(всего) - len(оценки)
        среднее_без = ((sum(всего) - sum(оценки)) / осталось) if осталось > 0 else None
        драйверы.append({
            "feature": название, "n": len(оценки),
            "score": round(sum(оценки) / len(оценки), 1),
            "without": round(среднее_без, 1) if среднее_без is not None else None,
            "delta": (round(sum(оценки) / len(оценки) - среднее_без, 1)
                      if среднее_без is not None else None),
        })
    драйверы.sort(key=lambda д: (д["delta"] if д["delta"] is not None else 0.0))  # type: ignore[arg-type,return-value]
    return {"surveys": len(строки), "average": round(средняя, 1) if средняя is not None else None,
            "calibration": сверка, "drivers": драйверы[:30],
            "note": ("Связь — не причина: признак, рядом с которым оценка ниже, "
                     "стоит послушать, а не наказывать за него.")}


# ---------------------------------------------------------------------------
# Риск ухода и эскалации
# ---------------------------------------------------------------------------

#: Слова в трекерах и тревогах, которые говорят об уходе или эскалации.
_УГРОЗА = re.compile(r"суд|жалоб|надзор|прокурат|роспотреб|уйд|уход|отказ|расторг|"
                     r"конкурент|верн[уи] деньги|претенз|сми|отзыв", re.IGNORECASE)


def риск(р: dict[str, Any], повтор: bool = False) -> tuple[int, list[str]]:
    """Балл риска 0–100 и его причины — по уже посчитанным признакам."""
    балл = 0
    причины: list[str] = []
    if р.get("resolved") is False:
        балл += 30
        причины.append("вопрос не решён")
    угрозы = [str(т.get("label") or т.get("id")) for т in (р.get("trackers") or [])
              if isinstance(т, dict) and т.get("fired")
              and _УГРОЗА.search(f"{т.get('id')} {т.get('label')}")]
    if угрозы:
        балл += 25
        причины.append("трекер: " + ", ".join(угрозы[:3]))
    elif int(р.get("alerts") or 0) > 0:
        балл += 15
        причины.append("тревожные упоминания")
    сдвиг = р.get("mood_shift")
    тон = р.get("sentiment")
    if (сдвиг is not None and float(сдвиг) <= -0.2) or (тон is not None and float(тон) <= -0.3):
        балл += 15
        причины.append("к концу разговора клиент недоволен")
    if повтор or int(р.get("repeat_contact") or 0) > 0:
        балл += 15
        причины.append("повторное обращение")
    if р.get("stress") is not None and float(р["stress"]) >= 60:
        балл += 10
        причины.append("высокое напряжение")
    if int(р.get("frustration") or 0) > 0:
        балл += 5
        причины.append("раздражение")
    return min(100, балл), причины


def порог_риска(settings: Any) -> int:
    try:
        return max(1, min(100, int(settings.get("llm_risk_threshold", 70) or 70)))
    except (TypeError, ValueError):
        return 70


def риски(db: Any, settings: Any, *, since: float, until: float | None = None,
          owner: Any = None, agent: str = "", limit: int = 50,
          now: float | None = None) -> dict[str, Any]:
    """Разговоры с высоким риском ухода или эскалации — с причинами."""
    сейчас = float(now if now is not None else time.time())
    дней = _дни(settings)
    строки = db.llm_window_rows(since or 0.0, until=until, owner=owner, agent=agent)
    звонки = _звонки_по_номерам(db, max(0.0, (since or 0.0) - дней * 86400.0),
                                min(сейчас, until or сейчас) + 1.0)
    порог = порог_риска(settings)
    пункты = []
    по_баллам = {"high": 0, "medium": 0, "low": 0}
    for р in строки:
        клиент = номер_клиента(р)
        когда = _время(р)
        повтор = any(когда - дней * 86400.0 <= float(з.get("started_at") or 0) < когда - 60.0
                     and з.get("job_id") != р["job_id"]
                     and str(з.get("direction") or "") != "исходящий"
                     for з in звонки.get(клиент, [])) if клиент else False
        балл, причины = риск(р, повтор)
        уровень = "high" if балл >= порог else "medium" if балл >= порог / 2 else "low"
        по_баллам[уровень] += 1
        if уровень != "low":
            пункты.append({"job_id": р["job_id"], "filename": р.get("filename"),
                           "created_at": р.get("created_at"), "agent": р.get("agent") or "",
                           "reason": р.get("reason"), "outcome": р.get("outcome"),
                           "score": балл, "level": уровень, "why": причины})
    пункты.sort(key=lambda п: (-п["score"], -(п.get("created_at") or 0)))
    return {"threshold": порог, "counts": по_баллам, "analyzed": len(строки),
            "items": пункты[:max(1, int(limit))]}


__all__ = ["договорённости", "номер", "номер_клиента", "оценка_операторов",
           "опросы_клиентов", "первое_обращение", "порог_риска", "риск", "риски"]
