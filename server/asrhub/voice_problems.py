"""Проблемы и сложности разговоров за период — сводка по всем разговорам.

Разделы голосовой аналитики отвечают, о чём звонили и чем закончилось.
Следующий вопрос руководителя — «что пошло не так»: где клиент ушёл с
нерешённым вопросом, где разговор испортился, где оператор нарушил скрипт,
где в трубке была тишина или шум. Всё это уже посчитано по каждому
разговору — разбором содержания (`content`), ответом языковой модели
(`llm_results`), журналом звонков и проверкой звука, — но лежит в десятке
разных разделов. Здесь оно сводится в один перечень проблем за период:
сколько разговоров задело каждое, какая это доля, как изменилось к базе,
с какой причиной обращения и у каких сотрудников встречается чаще.

Что важно знать заранее:

* **Перечень закрытый и описан здесь же** (`ПРОБЛЕМЫ`): условие на
  строку разговора, основа доли и отборы списка раскрытия. Условия — те
  же выражения, что у отборов «что послушать» (`Database.CONTENT_FILTERS`)
  и у списков раскрытия (`drill`), поэтому число в сводке и список под
  ним сходятся.
* **Доля — от тех, у кого признак вообще мог быть.** «Вопрос не решён» —
  от разговоров, где модель ответила, решён ли он; «скрипт не соблюдён» —
  от разговоров, где скрипт проверялся. Иначе разговоры без разбора тянули
  бы доли вниз.
* **Один проход по окну.** Все признаки считаются одним запросом с
  группировкой по добавочному и причине обращения; из групп питоном
  складываются итоги, разрезы по сотрудникам и по причинам. Базе
  сравнения хватает итогов — второй запрос без группировки.
* **«Разговор с проблемой»** — разговор, у которого есть хотя бы одна
  проблема групп «клиент», «оператор», «ход разговора». Технические
  (звук, расшифровка, сбой разбора) показываются, но в это число не
  входят: они говорят о записи, а не о разговоре.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from .logging_setup import get_logger
from .voice_period import (
    ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ,
    МАЛО_ДЛЯ_ВЫВОДА,
    Отбор,
    _доля,
    _пул,
    _сравнить_доли,
)

log = get_logger("asrhub.voice_problems")

#: Группы проблем — в порядке показа.
ГРУППЫ: tuple[tuple[str, str], ...] = (
    ("client", "Клиент и его вопрос"),
    ("agent", "Работа оператора"),
    ("process", "Ход разговора"),
    ("record", "Запись и разбор"),
)
#: Какие группы делают разговор «разговором с проблемой».
ГРУППЫ_РАЗГОВОРА = ("client", "agent", "process")

#: Основы долей: разговор с разбором содержания. Ответ модели соединяется
#: вместе со сбойными (`l`), и условия на него сами требуют ответа без
#: сбоя: проверка сбоя подзапросом на каждую строку стоила на годовом окне
#: две секунды из трёх.
С_РАЗБОРОМ = "c.job_id IS NOT NULL"
ОТВЕТ_БЕЗ_СБОЯ = "COALESCE(l.error,'') = ''"


@dataclass(frozen=True)
class Проблема:
    id: str
    группа: str
    название: str
    подсказка: str
    #: Условие на строку разговора (`j` — задание, `c` — разбор, `l` —
    #: ответ модели, в том числе сбойный).
    условие: str
    #: Основа доли: у каких разговоров признак мог быть. Пусто — у всех.
    основа: str
    #: Отборы списка раскрытия (`GET /api/drill`) и его база.
    отборы: tuple[str, ...]
    база: str = "content"


ПРОБЛЕМЫ: tuple[Проблема, ...] = (
    # --- клиент -----------------------------------------------------------
    Проблема("unresolved", "client", "Вопрос не решён",
             "по ответу языковой модели: клиент ушёл без решения",
             f"{ОТВЕТ_БЕЗ_СБОЯ} AND l.resolved = 0", f"{ОТВЕТ_БЕЗ_СБОЯ} AND l.resolved IS NOT NULL",
             ("llm_ok", "resolved:0"), "llm"),
    Проблема("negative", "client", "Негативная тональность",
             "общая тональность разговора ниже −0,15",
             "c.sentiment < -0.15", "c.sentiment IS NOT NULL", ("negative",)),
    Проблема("downturn", "client", "Разговор испортился к концу",
             "тональность к концу разговора упала заметно сильнее, чем была в начале",
             "c.sentiment_shift < -0.2", "c.sentiment_shift IS NOT NULL", ("downturn",)),
    Проблема("frustrated", "client", "Раздражение клиента",
             "клиент раздражён: «сколько можно», «третий раз звоню», повышенный тон",
             "COALESCE(c.frustration,0) > 0", С_РАЗБОРОМ, ("frustrated",)),
    Проблема("repeat", "client", "Повторное обращение",
             "клиент говорит, что звонит не впервые по тому же вопросу",
             "COALESCE(c.repeat_contact,0) > 0", С_РАЗБОРОМ, ("repeat",)),
    Проблема("alerts", "client", "Тревожные упоминания",
             "суд, жалоба, претензия, расторжение, Роспотребнадзор и подобное",
             "COALESCE(c.alerts,0) > 0", С_РАЗБОРОМ, ("alerts",)),
    Проблема("effort", "client", "Клиенту пришлось пробиваться",
             "переспросы, повторы, «я же уже говорил» — высокое усилие клиента",
             "c.effort <= -2", "c.effort IS NOT NULL", ("effort_high",)),
    Проблема("profanity", "client", "Мат в разговоре",
             "нецензурная лексика у любой из сторон",
             "COALESCE(c.profanity,0) > 0", С_РАЗБОРОМ, ("profanity",)),
    # --- оператор ---------------------------------------------------------
    Проблема("violation", "agent", "Нарушения оператора",
             "запрещённые фразы и нарушения из каталога",
             "COALESCE(c.violations,0) > 0", С_РАЗБОРОМ, ("violation",)),
    Проблема("profanity_agent", "agent", "Мат оператора",
             "нецензурная лексика в репликах оператора",
             "COALESCE(c.profanity_agent,0) > 0", С_РАЗБОРОМ, ("profanity_agent",)),
    Проблема("impolite", "agent", "Невежливость",
             "в речи оператора больше грубого, чем вежливого",
             "c.empathy IS NOT NULL AND c.empathy < 0", "c.empathy IS NOT NULL",
             ("impolite",)),
    Проблема("script_failed", "agent", "Скрипт не соблюдён",
             "выполнено меньше половины пунктов скрипта",
             "c.compliance IS NOT NULL AND c.compliance < 0.5", "c.compliance IS NOT NULL",
             ("script_failed",)),
    Проблема("low_score", "agent", "Низкий балл оператора",
             "балл работы оператора ниже 60 из 100",
             "c.agent_score IS NOT NULL AND c.agent_score < 60", "c.agent_score IS NOT NULL",
             ("low_score",)),
    Проблема("objection_unhandled", "agent", "Возражение не отработано",
             "клиент возразил, оператор не ответил по существу",
             "COALESCE(c.objections_unhandled,0) > 0", С_РАЗБОРОМ, ("objection_unhandled",)),
    Проблема("interruptions", "agent", "Перебивания",
             "больше двух перебиваний за разговор",
             "COALESCE(c.interruptions,0) > 2", С_РАЗБОРОМ, ("interruptions",)),
    Проблема("open_commitments", "agent", "Обещание без срока",
             "оператор что-то пообещал, но не назвал, когда",
             "c.commitments > COALESCE(c.commitments_dated,0)", С_РАЗБОРОМ,
             ("open_commitments",)),
    Проблема("monologue", "agent", "Долгий монолог",
             "кто-то говорит без перерыва две с половиной минуты и дольше",
             "c.monologue_s >= 150", С_РАЗБОРОМ, ("monologue",)),
    # --- ход разговора ----------------------------------------------------
    Проблема("dead_air", "process", "Мёртвый эфир",
             "в сумме полминуты и больше тишины, когда ждут друг друга",
             "c.dead_air_s >= 30", С_РАЗБОРОМ, ("dead_air",)),
    Проблема("silence", "process", "Много молчания",
             "молчание занимает больше трети разговора",
             "c.silence_share > 0.3", С_РАЗБОРОМ, ("silence",)),
    Проблема("stress", "process", "Высокое напряжение",
             "напряжение в голосе и в словах — 55 и выше из 100",
             "c.stress >= 55", "c.stress IS NOT NULL", ("stress_high",)),
    Проблема("clarity", "process", "Речь тяжело слушать",
             "разборчивость речи ниже 35 из 100",
             "c.clarity < 35", "c.clarity IS NOT NULL", ("clarity_low",)),
    # --- запись и разбор ----------------------------------------------------
    Проблема("bad_audio", "record", "Плохой звук",
             "шум (SNR ниже 10 дБ) или клиппинг от 1 % отсчётов",
             "(j.snr_db < 10 OR j.clipping_share >= 0.01)",
             "(j.snr_db IS NOT NULL OR j.clipping_share IS NOT NULL)", ("bad_audio",), "jobs"),
    Проблема("suspect", "record", "Подозрительная расшифровка",
             "признаки выдуманного текста, повторов или неверно разделённых говорящих",
             "COALESCE(j.quality_flags,'') <> ''", "j.quality_flags IS NOT NULL",
             ("flagged",), "jobs"),
    Проблема("llm_failed", "record", "Модель не разобрала",
             "разбор языковой моделью кончился сбоем",
             "COALESCE(l.error,'') <> ''", "", ("llm_failed",), "llm"),
)

ПО_ИМЕНИ = {п.id: п for п in ПРОБЛЕМЫ}

#: Ожидание ответа, после которого оно считается долгим, секунды.
ДОЛГОЕ_ОЖИДАНИЕ_С = 60
#: Сколько сотрудников и причин отдают разрезы.
СОТРУДНИКОВ = 15
ПРИЧИН = 15
#: Меньше скольких разговоров у сотрудника — в разрез «у кого больше всего
#: проблем» он не попадает: на трёх звонках «67 %» — это два звонка.
МИНИМУМ_У_СОТРУДНИКА = 5


def _условие_разговора() -> str:
    return " OR ".join(f"({п.условие})" for п in ПРОБЛЕМЫ if п.группа in ГРУППЫ_РАЗГОВОРА)


def _столбцы(группы: bool) -> str:
    части = ["COUNT(*) AS n",
             f"SUM(CASE WHEN {_условие_разговора()} THEN 1 ELSE 0 END) AS any_problem"]
    for п in ПРОБЛЕМЫ:
        части.append(f"SUM(CASE WHEN {п.условие} THEN 1 ELSE 0 END) AS p_{п.id}")
        основа = п.основа or "1"
        части.append(f"SUM(CASE WHEN {основа} THEN 1 ELSE 0 END) AS b_{п.id}")
    if группы:
        части.insert(0, f"{ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ} AS a, "
                        f"CASE WHEN {ОТВЕТ_БЕЗ_СБОЯ} THEN COALESCE(l.reason,'') ELSE '' END AS r")
    return ", ".join(части)


_СОЕДИНЕНИЯ = ("FROM jobs j LEFT JOIN content c ON c.job_id = j.id "
               "LEFT JOIN llm_results l ON l.job_id = j.id")


def _группы(db: Any, отбор: Отбор, since: float, until: float) -> list[dict[str, Any]]:
    где, арг = отбор.где(db, since, until, готовые=True)
    return [dict(r) for r in db.query(
        f"SELECT {_столбцы(True)} {_СОЕДИНЕНИЯ} WHERE {где} GROUP BY a, r", арг)]


def _итоги(db: Any, отбор: Отбор, since: float, until: float) -> dict[str, Any]:
    где, арг = отбор.где(db, since, until, готовые=True)
    строка = db.query_one(f"SELECT {_столбцы(False)} {_СОЕДИНЕНИЯ} WHERE {где}", арг)
    return dict(строка) if строка else {}


def _состояния(db: Any, отбор: Отбор, since: float, until: float) -> dict[str, int]:
    """Записи окна по состояниям: без речи, короткие и ошибки — тоже сложности."""
    где, арг = отбор.где(db, since, until)
    счёт = {str(r["s"]): int(r["n"] or 0) for r in db.query(
        f"SELECT j.status AS s, COUNT(*) AS n FROM jobs j WHERE {где} GROUP BY j.status", арг)}
    return {"jobs": sum(счёт.values()), "completed": счёт.get("completed", 0),
            "no_speech": счёт.get("no_speech", 0), "short": счёт.get("short", 0),
            "failed": счёт.get("failed", 0)}


def _звонки(db: Any, отбор: Отбор, since: float, until: float) -> dict[str, int]:
    где, арг = отбор.где_звонков(db, since, until)
    r = db.query_one(
        "SELECT COUNT(*) AS calls, "
        "SUM(CASE WHEN cl.direction = 'входящий' THEN 1 ELSE 0 END) AS inbound, "
        "SUM(CASE WHEN cl.direction = 'входящий' AND COALESCE(cl.answered,0) <> 1 "
        "    THEN 1 ELSE 0 END) AS abandoned, "
        "SUM(CASE WHEN cl.answered = 1 THEN 1 ELSE 0 END) AS answered, "
        "SUM(CASE WHEN cl.answered = 1 AND MAX(0, COALESCE(cl.duration,0) - "
        f"    COALESCE(cl.billsec,0)) > {ДОЛГОЕ_ОЖИДАНИЕ_С} THEN 1 ELSE 0 END) AS long_wait "
        f"FROM calls cl WHERE {где}", арг)
    return {к: int((r[к] if r else 0) or 0)
            for к in ("calls", "inbound", "abandoned", "answered", "long_wait")}


@dataclass
class Окна:
    """Окно сводки и база сравнения — границы, подписи."""

    since: float
    until: float
    label: str
    base_since: float | None = None
    base_until: float | None = None
    base_label: str = ""
    to_date: bool = False


def посчитать(db: Any, окна: Окна, отбор: Отбор, *, имена: bool = True) -> dict[str, Any]:
    """Сводка проблем за окно — со сравнением с базой, если она есть."""
    from . import employees as справочник  # noqa: PLC0415

    пул = _пул()
    есть_база = окна.base_since is not None and окна.base_until is not None
    ж_группы = пул.submit(_группы, db, отбор, окна.since, окна.until)
    ж_база = (пул.submit(_итоги, db, отбор, окна.base_since, окна.base_until)
              if есть_база else None)
    ж_состояния = пул.submit(_состояния, db, отбор, окна.since, окна.until)
    ж_состояния_было = (пул.submit(_состояния, db, отбор, окна.base_since, окна.base_until)
                        if есть_база else None)
    ж_звонки = пул.submit(_звонки, db, отбор, окна.since, окна.until)
    ж_звонки_было = (пул.submit(_звонки, db, отбор, окна.base_since, окна.base_until)
                     if есть_база else None)

    группы = ж_группы.result()
    база = ж_база.result() if ж_база is not None else {}
    состояния = ж_состояния.result()
    состояния_было = ж_состояния_было.result() if ж_состояния_было is not None else {}
    звонки = ж_звонки.result()
    звонки_было = ж_звонки_было.result() if ж_звонки_было is not None else {}

    поля = ["n", "any_problem", *(f"p_{п.id}" for п in ПРОБЛЕМЫ),
            *(f"b_{п.id}" for п in ПРОБЛЕМЫ)]
    итог = dict.fromkeys(поля, 0)
    по_людям: dict[str, dict[str, int]] = {}
    по_причинам: dict[str, dict[str, int]] = {}
    for г in группы:
        for поле in поля:
            значение = int(г.get(поле) or 0)
            итог[поле] += значение
            по_людям.setdefault(str(г.get("a") or ""), dict.fromkeys(поля, 0))[поле] += значение
            по_причинам.setdefault(str(г.get("r") or ""), dict.fromkeys(поля, 0))[поле] += значение

    разговоров = итог["n"]
    было_разговоров = int(база.get("n") or 0)
    строки: list[dict[str, Any]] = []
    for п in ПРОБЛЕМЫ:
        сколько, из = итог[f"p_{п.id}"], итог[f"b_{п.id}"]
        было, было_из = int(база.get(f"p_{п.id}") or 0), int(база.get(f"b_{п.id}") or 0)
        где_чаще = sorted(
            ((причина, с[f"p_{п.id}"], с[f"b_{п.id}"]) for причина, с in по_причинам.items()
             if причина and с[f"p_{п.id}"]),
            key=lambda т: (-т[1], т[0]))[:3]
        у_кого = sorted(
            ((номер, с[f"p_{п.id}"], с[f"b_{п.id}"]) for номер, с in по_людям.items()
             if номер and с[f"p_{п.id}"]),
            key=lambda т: (-т[1], т[0]))[:3]
        строки.append({
            "id": п.id, "group": п.группа, "label": п.название, "hint": п.подсказка,
            "records": сколько, "base": из, "share": _доля(сколько, из),
            "prev_records": было, "prev_base": было_из, "prev_share": _доля(было, было_из),
            **(_сравнить_доли(сколько, из, было, было_из) if есть_база
               else {"delta_pp": None, "z": None, "significant": False}),
            "drill": {"base": п.база, "f": list(п.отборы)},
            "reasons": [{"reason": р, "records": n, "share": _доля(n, b)} for р, n, b in где_чаще],
            "agents": [{"agent": а, "records": n, "share": _доля(n, b)} for а, n, b in у_кого],
        })

    # Технические сложности окна: записи без речи, короткие, ошибки. Доля —
    # от всех записей окна, а не от разговоров: у них разговора и не было.
    всего, всего_было = состояния.get("jobs", 0), состояния_было.get("jobs", 0)
    for ключ, название, подсказка in (
            ("no_speech", "Записи без речи",
             "тишина, гудки, музыка ожидания — речи в записи нет"),
            ("short", "Короткие записи",
             "короче порога «короткой записи» или пустой файл — сброс, неотвеченный звонок"),
            ("failed", "Ошибки распознавания", "запись не удалось распознать")):
        сколько, было = состояния.get(ключ, 0), состояния_было.get(ключ, 0)
        строки.append({
            "id": ключ, "group": "record", "label": название, "hint": подсказка,
            "records": сколько, "base": всего, "share": _доля(сколько, всего),
            "prev_records": было, "prev_base": всего_было, "prev_share": _доля(было, всего_было),
            **(_сравнить_доли(сколько, всего, было, всего_было) if есть_база
               else {"delta_pp": None, "z": None, "significant": False}),
            "drill": {"base": "jobs", "f": [f"status:{ключ}"], "status": True},
            "reasons": [], "agents": [],
        })

    # Телефония: пропущенные входящие и долгое ожидание — сложности хода
    # обращения, которых в записи разговора нет вовсе.
    if звонки.get("calls"):
        for ключ, название, подсказка, из_поля, отборы in (
                ("abandoned", "Пропущенные входящие",
                 "входящий звонок, на который не ответили", "inbound",
                 ["abandoned"]),
                ("long_wait", f"Долгое ожидание ответа (> {ДОЛГОЕ_ОЖИДАНИЕ_С} с)",
                 "клиент ждал ответа дольше минуты", "answered",
                 ["answered", f"wait_over:{ДОЛГОЕ_ОЖИДАНИЕ_С}"])):
            сколько, из = звонки.get(ключ, 0), звонки.get(из_поля, 0)
            было, было_из = звонки_было.get(ключ, 0), звонки_было.get(из_поля, 0)
            строки.append({
                "id": ключ, "group": "process", "label": название, "hint": подсказка,
                "records": сколько, "base": из, "share": _доля(сколько, из),
                "prev_records": было, "prev_base": было_из, "prev_share": _доля(было, было_из),
                **(_сравнить_доли(сколько, из, было, было_из) if есть_база
                   else {"delta_pp": None, "z": None, "significant": False}),
                "drill": {"base": "calls", "f": отборы}, "calls": True,
                "reasons": [], "agents": [],
            })

    проблем = sum(итог[f"p_{п.id}"] for п in ПРОБЛЕМЫ if п.группа in ГРУППЫ_РАЗГОВОРА)
    люди = []
    for номер, с in по_людям.items():
        if not номер or с["n"] < МИНИМУМ_У_СОТРУДНИКА:
            continue
        главная = max((п for п in ПРОБЛЕМЫ if п.группа in ГРУППЫ_РАЗГОВОРА),
                      key=lambda п, с=с: (с[f"p_{п.id}"], -ПРОБЛЕМЫ.index(п)))
        люди.append({"agent": номер, "conversations": с["n"], "with_problems": с["any_problem"],
                     "share": _доля(с["any_problem"], с["n"]),
                     "unresolved": с["p_unresolved"], "unresolved_base": с["b_unresolved"],
                     "unresolved_share": _доля(с["p_unresolved"], с["b_unresolved"]),
                     "top": ({"id": главная.id, "label": главная.название,
                              "records": с[f"p_{главная.id}"]}
                             if с[f"p_{главная.id}"] else None)})
    люди.sort(key=lambda ч: (-(ч["share"] or 0), -ч["conversations"], ч["agent"]))
    люди = люди[:СОТРУДНИКОВ]
    справочник.подписать_добавочные(db, люди, имена=имена)

    причины = []
    for причина, с in по_причинам.items():
        if not причина:
            continue
        главная = max((п for п in ПРОБЛЕМЫ if п.группа in ГРУППЫ_РАЗГОВОРА),
                      key=lambda п, с=с: (с[f"p_{п.id}"], -ПРОБЛЕМЫ.index(п)))
        причины.append({"reason": причина, "conversations": с["n"],
                        "with_problems": с["any_problem"],
                        "share": _доля(с["any_problem"], с["n"]),
                        "unresolved": с["p_unresolved"],
                        "unresolved_share": _доля(с["p_unresolved"], с["b_unresolved"]),
                        "negative_share": _доля(с["p_negative"], с["b_negative"]),
                        "top": ({"id": главная.id, "label": главная.название,
                                 "records": с[f"p_{главная.id}"]}
                                if с[f"p_{главная.id}"] else None)})
    причины.sort(key=lambda п: (-п["with_problems"], п["reason"]))
    причины = причины[:ПРИЧИН]

    ответ = {
        "window": {"since": окна.since or None, "until": окна.until},
        "base": ({"since": окна.base_since, "until": окна.base_until,
                  "label": окна.base_label, "to_date": окна.to_date} if есть_база else None),
        "label": окна.label,
        "conversations": разговоров,
        "with_problems": итог["any_problem"],
        "share": _доля(итог["any_problem"], разговоров),
        "problems_total": проблем,
        "per_conversation": round(проблем / разговоров, 2) if разговоров else None,
        "prev_conversations": было_разговоров,
        "prev_with_problems": int(база.get("any_problem") or 0),
        "prev_share": _доля(int(база.get("any_problem") or 0), было_разговоров),
        **({"change": _сравнить_доли(итог["any_problem"], разговоров,
                                     int(база.get("any_problem") or 0), было_разговоров)}
           if есть_база else {}),
        "records": состояния,
        "telephony": звонки if звонки.get("calls") else None,
        "groups": [{"id": г, "label": п} for г, п in ГРУППЫ],
        "items": строки,
        "by_agent": люди,
        "by_reason": причины,
        "drill": отбор.drill(),
        "filter": {"agent": отбор.agent, "department": отбор.department},
    }
    ответ["summary"] = коротко(ответ)
    return ответ


# ---------------------------------------------------------------------------
# Коротко о проблемах
# ---------------------------------------------------------------------------

def _п(доля: float | None) -> str:
    if доля is None:
        return "—"
    return f"{доля * 100:.0f} %".replace(".", ",")


def _пп(дельта: float | None) -> str:
    if дельта is None:
        return ""
    знак = "+" if дельта > 0 else "−" if дельта < 0 else "±"
    return f"{знак}{abs(дельта):.1f} п. п.".replace(".", ",", 1)


def _число(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def коротко(сводка: dict[str, Any]) -> list[str]:
    """Главное из сводки — фразами, из тех же чисел, что ниже. Без модели."""
    фразы: list[str] = []
    разговоров = int(сводка.get("conversations") or 0)
    if not разговоров:
        фразы.append("За период нет разговоров с речью — проблемам не в чем найтись.")
    else:
        изм = (сводка.get("change") or {}).get("delta_pp")
        фразы.append(
            f"Проблемы есть в {_число(сводка['with_problems'])} разговорах из "
            f"{_число(разговоров)} ({_п(сводка.get('share'))}"
            + (f", {_пп(изм)} к базе" if изм is not None else "") + "); "
            f"в среднем {str(сводка.get('per_conversation') or 0).replace('.', ',')} "
            "проблемы на разговор.")
        разговорные = [с for с in сводка["items"]
                       if с["group"] in ГРУППЫ_РАЗГОВОРА and not с.get("calls")
                       and с["records"] and с["base"] >= МАЛО_ДЛЯ_ВЫВОДА // 4]
        частые = sorted(разговорные, key=lambda с: (-(с["share"] or 0), с["label"]))[:3]
        if частые:
            фразы.append("Чаще всего: " + ", ".join(
                f"{с['label'].lower()} — {_п(с['share'])}" for с in частые) + ".")
        выросли = sorted((с for с in сводка["items"] if с.get("significant")
                          and (с.get("delta_pp") or 0) > 0),
                         key=lambda с: -(с["delta_pp"] or 0))[:3]
        if выросли:
            фразы.append("Заметно выросли: " + ", ".join(
                f"{с['label'].lower()} ({_пп(с['delta_pp'])})" for с in выросли) + ".")
        снизились = sorted((с for с in сводка["items"] if с.get("significant")
                            and (с.get("delta_pp") or 0) < 0),
                           key=lambda с: с["delta_pp"] or 0)[:2]
        if снизились:
            фразы.append("Заметно снизились: " + ", ".join(
                f"{с['label'].lower()} ({_пп(с['delta_pp'])})" for с in снизились) + ".")
        трудные = [п for п in сводка.get("by_reason") or []
                   if п["conversations"] >= МИНИМУМ_У_СОТРУДНИКА and п.get("share") is not None]
        if трудные:
            т = max(трудные, key=lambda п: (п["share"], п["conversations"]))
            фразы.append(
                f"Сложнее всего с причиной «{т['reason']}»: проблемы в {_п(т['share'])} "
                f"разговоров" + (f", не решён вопрос в {_п(т['unresolved_share'])}"
                                 if т.get("unresolved_share") is not None else "") + ".")
        люди = сводка.get("by_agent") or []
        if люди and люди[0].get("share") is not None:
            ч = люди[0]
            кто = f"добавочного {ч['agent']}" + (f" ({ч['agent_name']})" if ч.get("agent_name")
                                                 else "")
            фразы.append(f"Больше всего проблемных разговоров у {кто}: {_п(ч['share'])} из "
                         f"{_число(ч['conversations'])}"
                         + (f", чаще всего — {ч['top']['label'].lower()}" if ч.get("top") else "")
                         + ".")
    з = сводка.get("records") or {}
    технические = [f"{подпись} {_число(з.get(ключ, 0))}" for ключ, подпись in (
        ("no_speech", "без речи"), ("short", "коротких"), ("failed", "с ошибкой"))
        if з.get(ключ)]
    плохой_звук = next((с for с in сводка["items"] if с["id"] == "bad_audio"), None)
    if плохой_звук and плохой_звук["records"]:
        технические.append(f"с плохим звуком {_число(плохой_звук['records'])}")
    if технические:
        фразы.append("Записи: " + ", ".join(технические) + ".")
    тел = сводка.get("telephony") or {}
    if тел.get("inbound"):
        фразы.append(f"Пропущено {_п(_доля(тел['abandoned'], тел['inbound']))} входящих "
                     f"({_число(тел['abandoned'])}), ответа ждали дольше "
                     f"{ДОЛГОЕ_ОЖИДАНИЕ_С} с — {_число(тел['long_wait'])} раз.")
    return фразы


# ---------------------------------------------------------------------------
# Сводка языковой моделью
# ---------------------------------------------------------------------------

#: Сколько пересказов проблемных разговоров идёт в подсказку модели.
ПЕРЕСКАЗОВ = 40
#: Предел одного пересказа, знаков.
ПЕРЕСКАЗ_ЗНАКОВ = 420

_СИСТЕМА = (
    "Ты аналитик контакт-центра. Тебе дают сводку проблем за период по всем "
    "разговорам и пересказы самых проблемных разговоров. Опирайся только на "
    "эти данные, ничего не выдумывай, числа бери из сводки. Отвечай на русском "
    "языке, строго одним объектом JSON без пояснений."
)

_ЗАПРОС = """Период: {label}.
Разговоров с речью: {conversations}, из них с проблемами: {with_problems} ({share}).

Проблемы (доля, изменение к базе):
{problems}

Причины обращений, с которыми больше всего проблем:
{reasons}

Сотрудники с наибольшей долей проблемных разговоров:
{agents}

Пересказы проблемных разговоров (дата, добавочный, причина, исход, признаки):
{conversations_list}

Верни JSON:
{{"overview": "три-пять предложений: что главное за период",
  "problems": [{{"title": "проблема или сложность", "details": "в чём она, насколько массовая, где и у кого встречается", "examples": ["короткий пример из пересказов"]}}],
  "causes": ["вероятная причина проблем — только если она видна из данных"],
  "recommendations": ["что сделать руководителю"]}}
"""


def _строка_проблемы(с: dict[str, Any]) -> str:
    изм = f", {_пп(с['delta_pp'])}" if с.get("delta_pp") is not None else ""
    значимо = " (значимо)" if с.get("significant") else ""
    return f"- {с['label']}: {с['records']} из {с['base']} ({_п(с['share'])}{изм}){значимо}"


def выборка(db: Any, окна: Окна, отбор: Отбор, *, предел: int = ПЕРЕСКАЗОВ
            ) -> list[dict[str, Any]]:
    """Пересказы самых проблемных разговоров окна — для подсказки модели.

    Сначала — разговоры, у которых больше всего проблем; при равенстве —
    свежие. Берутся только те, где модель оставила пересказ. Отбор идёт в
    два шага: номера по тяжести (по узким колонкам), затем пересказы
    только отобранных — иначе сортировка тащила бы через себя пересказ
    каждого проблемного разговора года.
    """
    разговорные = [п for п in ПРОБЛЕМЫ if п.группа in ГРУППЫ_РАЗГОВОРА]
    тяжесть = " + ".join(f"(CASE WHEN {п.условие} THEN 1 ELSE 0 END)" for п in разговорные)
    признаки = ", ".join(f"(CASE WHEN {п.условие} THEN 1 ELSE 0 END) AS p_{п.id}"
                         for п in разговорные)
    где, арг = отбор.где(db, окна.since, окна.until, готовые=True)
    кандидаты = db.query(
        f"SELECT j.id AS job_id, j.created_at AS created_at, "
        f"{ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ} AS agent, ({тяжесть}) AS weight, {признаки} {_СОЕДИНЕНИЯ} "
        f"WHERE {где} AND {ОТВЕТ_БЕЗ_СБОЯ} AND l.job_id IS NOT NULL "
        f"AND ({_условие_разговора()}) "
        "ORDER BY weight DESC, j.created_at DESC LIMIT ?", [*арг, int(предел) * 3])
    if not кандидаты:
        return []
    места = ",".join("?" for _ in кандидаты)
    тексты = {str(r["job_id"]): r for r in db.query(
        f"SELECT job_id, summary, reason, outcome FROM llm_results WHERE job_id IN ({места})",
        [r["job_id"] for r in кандидаты])}
    итог = []
    for r in кандидаты:
        т = тексты.get(str(r["job_id"]))
        if т is None or not str(т["summary"] or "").strip():
            continue
        итог.append({"job_id": r["job_id"], "created_at": r["created_at"],
                     "agent": r["agent"] or "", "reason": т["reason"] or "",
                     "outcome": т["outcome"] or "",
                     "flags": [п.название.lower() for п in разговорные if r[f"p_{п.id}"]],
                     "summary": " ".join(str(т["summary"]).split())[:ПЕРЕСКАЗ_ЗНАКОВ]})
        if len(итог) >= предел:
            break
    return итог


def подсказка(сводка: dict[str, Any], пересказы: list[dict[str, Any]]) -> str:
    проблемы = [с for с in сводка["items"] if с["records"]]
    проблемы.sort(key=lambda с: (-(с["share"] or 0), с["label"]))
    причины = "\n".join(
        f"- {п['reason']}: проблемы в {п['with_problems']} из {п['conversations']} "
        f"({_п(п['share'])}), не решён — {_п(п.get('unresolved_share'))}"
        for п in (сводка.get("by_reason") or [])[:10]) or "- нет данных"
    люди = "\n".join(
        f"- {ч['agent']}" + (f" {ч['agent_name']}" if ч.get("agent_name") else "")
        + f": {ч['with_problems']} из {ч['conversations']} ({_п(ч['share'])})"
        + (f", чаще всего — {ч['top']['label'].lower()}" if ч.get("top") else "")
        for ч in (сводка.get("by_agent") or [])[:8]) or "- нет данных"
    разговоры = "\n".join(
        f"{i}. {time.strftime('%d.%m %H:%M', time.localtime(р['created_at'] or 0))}"
        f" · {р['agent'] or 'без добавочного'} · {р['reason'] or '—'} → {р['outcome'] or '—'}"
        f" · {', '.join(р['flags'])}\n   {р['summary']}"
        for i, р in enumerate(пересказы, 1)) or "нет пересказов"
    return _ЗАПРОС.format(
        label=сводка.get("label") or "", conversations=сводка.get("conversations") or 0,
        with_problems=сводка.get("with_problems") or 0, share=_п(сводка.get("share")),
        problems="\n".join(_строка_проблемы(с) for с in проблемы) or "- проблем не найдено",
        reasons=причины, agents=люди, conversations_list=разговоры)


def _строки(значение: Any, предел: int = 12) -> list[str]:
    if not isinstance(значение, list):
        return []
    return [" ".join(str(з).split())[:600] for з in значение if str(з).strip()][:предел]


def разобрать_сводку(ответ: str) -> dict[str, Any]:
    """Ответ модели → поля сводки; кривые поля отбрасываются, а не роняют её."""
    from .llm.tasks import parse_json  # noqa: PLC0415

    данные = parse_json(ответ)
    проблемы = []
    for п in данные.get("problems") or []:
        if not isinstance(п, dict) or not str(п.get("title") or "").strip():
            continue
        проблемы.append({"title": " ".join(str(п["title"]).split())[:200],
                         "details": " ".join(str(п.get("details") or "").split())[:1200],
                         "examples": _строки(п.get("examples"), 4)})
    return {"overview": " ".join(str(данные.get("overview") or "").split())[:2000],
            "problems": проблемы[:12],
            "causes": _строки(данные.get("causes"), 8),
            "recommendations": _строки(данные.get("recommendations"), 10)}


def ключ_сводки(период: str, отбор: Отбор) -> str:
    """Ключ хранения сводки моделью: период, отбор и чьи записи.

    Период — имя, а не границы: у идущего периода конец — «сейчас», и по
    границам сводка, составленная утром, днём уже не нашлась бы. Хранится
    последняя сводка периода; её окно и время составления — в ней самой.
    """
    владелец = отбор.owner if isinstance(отбор.owner, (str, type(None))) else sorted(отбор.owner)
    отпечаток = hashlib.sha256(json.dumps(
        [период, отбор.agent, отбор.department, владелец],
        ensure_ascii=False).encode("utf-8")).hexdigest()[:32]
    return f"voice_digest:{отпечаток}"


__all__ = ["ГРУППЫ", "ПРОБЛЕМЫ", "Окна", "Проблема", "выборка", "ключ_сводки", "коротко",
           "подсказка", "посчитать", "разобрать_сводку"]
