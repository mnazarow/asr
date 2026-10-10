"""Заход 49: раскрытие чисел «Аналитики записей» — вкладки «Свод», «Эмоции»,
«Понятность», «NPS» и «Категории».

Каждое число этих вкладок раскрывается в список записей, и главное
требование одно: список обязан быть тем самым набором, по которому число
посчитано. Здесь сверяется серверная сторона — число свода против `total`
ручки `/api/drill` с тем отбором и тем окном, которые интерфейс берёт из
ответа: циклом по плиткам, строкам таблиц, корзинам рядов и группам
разрезов.

Общий архив (`drill_seed`) для этого раздела бедноват: напряжение,
понятность, названный балл, нарушения и мат в нём не встречаются вовсе, и
сверка «ноль равен нулю» ничего бы не ловила. Поэтому разбор досеян здесь —
пёстро и с краями порогов (ровно 55, ровно 35, ровно ±0,15), на которых
«больше» и «не меньше» дают разные ответы.
"""
from __future__ import annotations

import json
import math

import pytest
from asrhub import drill
from asrhub.insights import ПРИЗНАКИ, _границы_корзин
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

ПЕРИОДЫ = ("day", "week", "month", "all")


def досеять(архив: Архив) -> None:
    """Разнообразит разбор общего архива: все показатели раздела — не нули.

    Значения ставятся прямо в колонки разбора: сверяется не анализатор, а
    то, что свод и список одинаково читают одну и ту же таблицу.
    """
    db = архив.db
    номера = [р["id"] for р in db.query(
        "SELECT j.id FROM jobs j JOIN content c ON c.job_id = j.id "
        "WHERE j.status = 'completed' ORDER BY j.created_at DESC")]
    assert len(номера) >= 60, "в общем архиве слишком мало разобранных записей"
    про_доставку = {р["job_id"] for р in db.query(
        "SELECT job_id FROM content_hits WHERE category = 'delivery'")}
    for i, job_id in enumerate(номера):
        балл = None if i % 5 == 1 else i % 11
        тон = None if i % 13 == 4 else round(((i * 37) % 21 - 10) / 10, 2)
        # Драйверы негатива: про доставку говорят плохо, возражения кончаются
        # хорошо — иначе таблицы «что тянет вниз» и «что держит наверху» пусты.
        if job_id in про_доставку and тон is not None:
            тон = -0.7
        elif i % 4 == 1 and тон is not None:
            тон = 0.6
        поля = {
            "sentiment": {7: -0.15, 9: 0.15}.get(i, тон),          # края порога
            "sentiment_label": ("смешанная" if i % 9 == 2 else "нейтральная"),
            "sentiment_shift": None if i % 7 == 0 else ((i * 5) % 9 - 4) / 10,
            "speakers": 1 if i % 6 == 1 else 2,
            "agent_speaker": ("", "SPEAKER_00", "SPEAKER_01")[i % 3],
            "mood": None if i % 5 == 0 else ((i * 3) % 11 - 5) / 5,
            "mood_shift": {5: 0.15, 6: -0.15}.get(
                i, None if i % 4 == 0 else ((i * 7) % 13 - 6) / 10),
            "intensity": 1 + i % 5,
            "stress": {3: 55, 4: 54}.get(i, None if i % 6 == 0 else (i * 17) % 101),
            "effort": {2: -2.0}.get(i, None if i % 8 == 0 else float((i * 3) % 11 - 5)),
            "fatigue": None if i % 3 == 0 else (i * 7) % 100,
            "clarity": {10: 35, 11: 34}.get(i, None if i % 7 == 3 else (i * 13) % 100),
            "accuracy": None if i % 9 == 0 else (i * 19) % 100,
            "politeness": None if i % 10 == 0 else (i * 23) % 100,
            "personalization": None if i % 11 == 0 else (i * 29) % 100,
            "rhythm": None if i % 4 == 2 else (i * 31) % 100,
            "filler_rate": (i % 10) / 100,
            "diminutives": None if i % 9 == 1 else (2 if i % 4 == 0 else 0),
            "diminutive_rate": None if i % 9 == 1 else (i % 4 == 0) * 0.01,
            "nps": балл,
            "nps_group": (None if балл is None else "промоутер" if балл >= 9
                          else "нейтрал" if балл >= 7 else "критик"),
            "nps_said": (i // 6) % 11 if i % 6 == 0 else None,
            "csat_said": 1 + (i // 10) % 5 if i % 10 == 0 else None,
            "frustration": None if i % 7 == 1 else (2 if i % 7 == 0 else 0),
            "repeat_contact": None if i % 5 == 2 else (1 if i % 5 == 0 else 0),
            "profanity": None if i % 4 == 3 else (1 if i % 8 == 0 else 0),
            "profanity_agent": 1 if i % 16 == 0 else 0,
            "objections": i % 4,
            "objections_unhandled": None if i % 9 == 0 else min(i % 4, i % 3),
            "violations": 2 if i % 10 == 3 else 0,
            "name_uses": None if i % 12 == 0 else (2 if i % 3 == 0 else 0),
            "commitments": i % 3,
            "commitments_dated": None if i % 6 == 2 else (i % 3) // 2,
            "questions": i % 4,
            "alerts": 3 if i % 5 == 0 else 0,
            "monologue_s": {12: 150.0, 13: 149.9}.get(i, float((i * 11) % 200)),
            "interruptions": i % 4,
            "pauses": i % 5,
            "longest_pause_s": float(i % 7),
            "reply_delay_s": None if i % 5 == 3 else (i % 9) / 4,
            "overlap_s": None if i % 6 == 4 else float(i % 5),
            "dead_air_s": None if i % 8 == 5 else float(i % 40),
            "switches": i % 9,
            "tempo_ratio": None if i % 7 == 6 else 0.5 + (i % 8) / 4,
            "talk_share": None if i % 10 == 9 else (i % 10) / 10,
            "customer_story_s": float((i * 3) % 90),
            "agent_score": None if i % 6 == 5 else float((i * 9) % 101),
            "empathy": None if i % 4 == 1 else float((i * 23) % 201 - 100),
            "compliance": None if i % 8 == 7 else ((i * 3) % 11) / 10,
            "wpm": 90 + i,
            "silence_share": (i % 10) / 20,
        }
        db.execute("UPDATE content SET " + ", ".join(f"{к} = ?" for к in поля)
                   + " WHERE job_id = ?", [*поля.values(), job_id])
        # У трёх записей длина неизвестна: средняя длительность свода их не
        # считает, и список «по кому посчитано» обязан быть короче корпуса.
        if i % 25 == 3:
            db.execute("UPDATE jobs SET media_duration_s = NULL WHERE id = ?", (job_id,))
        if i % 10 == 3:
            db.execute("INSERT OR REPLACE INTO content_hits (job_id, category, kind, count) "
                       "VALUES (?, 'stop_words', 'violation', 2)", (job_id,))
        if i % 4 == 1:
            db.execute("INSERT OR REPLACE INTO content_hits (job_id, category, kind, count) "
                       "VALUES (?, 'objection', 'objection', 1)", (job_id,))
    # Категория с совпадениями двух видов: вид «Сроков» сменили, а архив не
    # пересчитали — у части записей совпадение осталось прежнего вида.
    сроки = [р["job_id"] for р in db.query(
        "SELECT h.job_id FROM content_hits h JOIN jobs j ON j.id = h.job_id "
        "WHERE h.category = 'deadline' ORDER BY j.created_at DESC")]
    for job_id in сроки[::4]:
        db.execute("UPDATE content_hits SET kind = 'violation' "
                   "WHERE job_id = ? AND category = 'deadline'", (job_id,))
    # Ответы модели: трекеры, скоркарта, ответ мимо списка и один сбой.
    ответы = [р["job_id"] for р in db.query(
        "SELECT l.job_id FROM llm_results l JOIN jobs j ON j.id = l.job_id "
        "ORDER BY j.created_at DESC")]
    assert len(ответы) >= 5, "в общем архиве мало ответов модели"
    for i, job_id in enumerate(ответы):
        трекеры = [{"id": "upsell", "label": "Допродажа", "fired": i % 2 == 0},
                   {"id": "churn", "label": "Угроза ухода", "fired": i == 1}]
        скоркарта = [{"id": "greet", "question": "Поздоровался?",
                      "answer": ("да", "нет", "н/п", "не понял")[i % 4]}]
        db.execute(
            "UPDATE llm_results SET trackers = ?, scorecard = ?, warnings = ?, error = ? "
            "WHERE job_id = ?",
            (json.dumps(трекеры if i != 2 else трекеры[:1], ensure_ascii=False),
             json.dumps(скоркарта, ensure_ascii=False),
             json.dumps(["причина вне списка"] if i == 3 else [], ensure_ascii=False),
             "модель не ответила" if i == len(ответы) - 1 else None, job_id))


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-content1") / "data")
    досеять(хранилище)
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _свод(c, period: str) -> dict:
    ответ = c.get("/api/content/summary", params={"period": period})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _разбор(c, окно, f=(), **прочее) -> int:
    return всего(c, окно, list(f), base="content", **прочее)


def _значения(c, окно, f, sort: str, base: str = "content") -> list[float]:
    """Столбец величины списка целиком — то, из чего сложено число свода."""
    параметры = {"base": base, "f": list(f), "sort": sort, "limit": 200}
    for граница in ("since", "until"):
        if (окно or {}).get(граница) is not None:
            параметры[граница] = окно[граница]
    return [с["value"] for с in все_строки(c, **параметры)]


# ---------------------------------------------------------------------------
# Свод: счётчики записей
# ---------------------------------------------------------------------------

#: Ключ свода → отбор списка, как их связывает интерфейс.
СЧЁТЧИКИ: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("records", ()), ("scored", ("scored",)),
    ("negative", ("negative",)), ("positive", ("positive",)), ("neutral", ("neutral",)),
    ("mixed", ("mixed",)), ("alert_records", ("alerts",)), ("mono", ("mono",)),
    ("violation_records", ("violation",)), ("frustrated", ("frustrated",)),
    ("repeat", ("repeat",)), ("long_monologues", ("monologue",)),
    ("profanity_agent_records", ("profanity_agent",)),
    ("profanity_records", ("profanity",)), ("profanity_checked", ("has:profanity",)),
    ("scored_agents", ("has:agent_score",)), ("named", ("named",)),
    ("stress_checked", ("has:stress",)), ("stress_high", ("stress_high",)),
    ("effort_high", ("effort_high",)),
    ("recovered", ("mood_up",)), ("worsened", ("mood_down",)),
    ("clarity_checked", ("has:clarity",)), ("clarity_low", ("clarity_low",)),
    ("diminutive_records", ("diminutives",)),
    ("nps_checked", ("has:nps",)), ("nps_stated_count", ("has:nps_said",)),
    ("promoters", ("nps_group:промоутер",)), ("passives", ("nps_group:нейтрал",)),
    ("detractors", ("nps_group:критик",)),
    ("promoters_stated", ("stated_promoter",)), ("passives_stated", ("stated_passive",)),
    ("detractors_stated", ("stated_detractor",)), ("csat_count", ("has:csat_said",)),
)


@pytest.mark.parametrize("period", ПЕРИОДЫ)
def test_каждый_счётчик_свода_равен_своему_списку(c, period):
    """Плитки и полосы «Свода», «Эмоций», «Понятности» и «NPS»."""
    свод = _свод(c, period)
    сейчас, окно = свод["current"], свод["window"]
    for ключ, отбор in СЧЁТЧИКИ:
        assert сейчас[ключ] == _разбор(c, окно, отбор), (period, ключ)
        if period == "month":
            # Сверка нулей ничего не ловит: досев обязан дать каждому числу записи.
            assert сейчас[ключ] > 0, f"в архиве нет записей для «{ключ}»"
    # «Ровно» в кольце — остаток, в него входят и записи без измеренного сдвига.
    ровно = сейчас["records"] - сейчас["recovered"] - сейчас["worsened"]
    assert ровно == _разбор(c, окно, ["mood_flat"]) > 0


def test_пороги_свода_и_списка_стоят_на_одной_стороне(c):
    """Ровно 55, ровно 35, ровно ±0,15: «не меньше» и «больше» — разные списки.

    Досев ставит по записи на каждый край; счётчик и список сверены выше,
    здесь — что край вообще попал куда надо, а не выпал из обоих разом.
    """
    окно = _свод(c, "month")["window"]
    assert _разбор(c, окно, ["stress:55..56"]) >= 1
    assert _разбор(c, окно, ["stress_high", "stress:55..56"]) == _разбор(c, окно, ["stress:55..56"])
    assert _разбор(c, окно, ["stress_high", "stress:54..55"]) == 0
    assert _разбор(c, окно, ["clarity_low", "clarity:35..36"]) == 0
    assert _разбор(c, окно, ["clarity_low", "clarity:34..35"]) >= 1
    assert _разбор(c, окно, ["effort_high", "effort:-2..-1.5"]) >= 1
    # Ровно на пороге тональность — нейтральная, сдвиг настроения — «ровно».
    assert _разбор(c, окно, ["neutral", "sentiment:-0.15..-0.1499"]) >= 1
    assert _разбор(c, окно, ["negative", "sentiment:-0.15..-0.1499"]) == 0
    assert _разбор(c, окно, ["neutral", "sentiment:0.15..0.1501"]) >= 1
    assert _разбор(c, окно, ["positive", "sentiment:0.15..0.1501"]) == 0
    assert _разбор(c, окно, ["mood_flat", "mood_shift:0.15..0.1501"]) >= 1
    assert _разбор(c, окно, ["mood_up", "mood_shift:0.15..0.1501"]) == 0
    assert _разбор(c, окно, ["monologue", "monologue_s:150..150.01"]) >= 1
    assert _разбор(c, окно, ["monologue", "monologue_s:149..150"]) == 0


def test_числа_прошлого_периода_раскрываются_прошлым_окном(c):
    """«Было N» и колонка «Прошлый период» — по окну от начала прошлого
    периода до начала нынешнего; граница общая с окном свода."""
    свод = _свод(c, "day")
    прошлое, окно = свод["window_previous"], свод["window"]
    assert прошлое["until"] == окно["since"]
    assert прошлое["until"] - прошлое["since"] == pytest.approx(86400)
    было = свод["previous"]
    assert было["records"] > 0, "в прошлых сутках архива нет записей"
    for ключ, отбор in СЧЁТЧИКИ:
        assert было[ключ] == _разбор(c, прошлое, отбор), ключ
    # Вместе два окна не теряют и не удваивают запись на стыке.
    assert (было["records"] + свод["current"]["records"]
            == _разбор(c, {"since": прошлое["since"], "until": окно["until"]}))
    # У «всего времени» прошлого периода нет — и раскрывать его нечем.
    assert _свод(c, "all")["window_previous"] is None


# ---------------------------------------------------------------------------
# Свод: средние и суммы
# ---------------------------------------------------------------------------


def test_каждый_показатель_знает_чем_раскрыться(c):
    """Отбор и порядок показателю называет сервер: копии в интерфейсе нет,
    и новый показатель без величины остаётся без кнопки, а не с ошибкой."""
    признаки = {п["key"]: п for п in _свод(c, "month")["features"]}
    assert set(признаки) == {п["key"] for п in ПРИЗНАКИ}
    без_кнопки = sorted(к for к, п in признаки.items() if "drill" not in п)
    assert без_кнопки == ["long_pauses"], без_кнопки      # его в своде нет вовсе
    for ключ, признак in признаки.items():
        if "drill" not in признак:
            continue
        чем = признак["drill"]
        ответ = c.get("/api/drill", params={
            "base": "content", "f": чем["f"], "sort": чем["sort"], "limit": 1})
        assert ответ.status_code == 200, (ключ, ответ.text)


@pytest.mark.parametrize("period", ["week", "all"])
def test_среднее_свода_посчитано_по_записям_своего_списка(c, period):
    """«Речь и разговор», плитки средних: список — записи с измеренной
    величиной, и среднее по его столбцу — то самое число свода."""
    свод = _свод(c, period)
    сейчас, окно = свод["current"], свод["window"]
    проверено = 0
    for признак in свод["features"]:
        чем = признак.get("drill")
        if not чем or чем["f"] != [f"has:{признак['key']}"]:
            continue                                   # суммы и доли — ниже
        значения = _значения(c, окно, чем["f"], чем["sort"])
        assert значения, признак["key"]
        assert sum(значения) / len(значения) == pytest.approx(
            сейчас[признак["key"]], abs=10 ** -(признак["digits"] + 1)), признак["key"]
        # Порядок — тот, что обещан: сверху те, кто тянет показатель вниз.
        assert значения == sorted(значения, reverse=чем["sort"].endswith(":desc")), признак["key"]
        проверено += 1
    assert проверено >= 30, проверено
    # Средняя длительность — без записей с неизвестной длиной.
    assert _разбор(c, окно, ["has:duration_s"]) < сейчас["records"]


def test_суммы_свода_сложены_из_столбца_своего_списка(c):
    """Обещания и возражения на плитках — не записи, а суммы по ним. Список
    под плиткой короче числа, и сходиться обязана сумма его столбца."""
    свод = _свод(c, "month")
    сейчас, окно = свод["current"], свод["window"]
    суммы = (
        ("commitments_open", ("open_commitments",), "commitments_open:desc"),
        ("commitments", ("nz:commitments",), "commitments:desc"),
        ("objections_unhandled", ("objection_unhandled",), "objections_unhandled:desc"),
        ("objections", ("objection",), "objections:desc"),
        ("questions", ("nz:questions",), "questions:desc"),
    )
    for ключ, отбор, порядок in суммы:
        значения = _значения(c, окно, отбор, порядок)
        assert sum(значения) == сейчас[ключ] > 0, ключ
        assert len(значения) == _разбор(c, окно, отбор) < сейчас[ключ], ключ
    # Доли таблицы «Речь и разговор» раскрываются в записи числителя.
    доли = {"named_share": "named", "recovered_share": "recovered",
            "stress_high_share": "stress_high"}
    признаки = {п["key"]: п for п in свод["features"]}
    for ключ, числитель in доли.items():
        чем = признаки[ключ]["drill"]
        assert чем["zero_is_empty"] is True
        assert _разбор(c, окно, чем["f"]) == сейчас[числитель] > 0, ключ
        assert len(_значения(c, окно, чем["f"], чем["sort"])) == сейчас[числитель], ключ
    for ключ, знаменатель in (("nps_index", "nps_checked"),
                              ("nps_stated_avg", "nps_stated_count"),
                              ("csat_avg", "csat_count")):
        assert _разбор(c, окно, признаки[ключ]["drill"]["f"]) == сейчас[знаменатель] > 0, ключ


# ---------------------------------------------------------------------------
# Ряд по времени и контрольные карты
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ПЕРИОДЫ)
def test_точка_ряда_раскрывается_в_записи_своей_корзины(c, period):
    """Графики «во времени» на четырёх вкладках: точка — корзина, и границы
    корзины приходят с сервера, а не считаются заново от подписи оси."""
    ряд = c.get("/api/content/timeline", params={"period": period}).json()
    корзины = ряд["buckets"]
    assert sum(1 for т in корзины if т["records"]) >= 2
    for n, т in enumerate(корзины):
        окно = {"since": т["since"], "until": т["until"]}
        assert _разбор(c, окно) == т["records"], (period, n)
        if not т["records"]:
            continue
        оценено = _разбор(c, окно, ["scored"])
        доля = round(100.0 * _разбор(c, окно, ["negative"]) / оценено, 1) if оценено else None
        assert доля == т["negative_share"], (period, n)
        assert _разбор(c, окно, ["has:nps_said"]) == т["nps_said_count"], (period, n)
        assert (_разбор(c, окно, ["has:stress"]) > 0) == (т["stress"] is not None)
        assert (_разбор(c, окно, ["has:nps"]) > 0) == (т["nps"] is not None)
    # Корзины стоят встык и покрывают окно ряда: ни одна запись не выпала.
    for прошлая, следующая in zip(корзины, корзины[1:], strict=False):
        assert прошлая["until"] == следующая["since"]
    assert корзины[0]["since"] == pytest.approx(ряд["from"], abs=0.06)
    целиком = {"since": корзины[0]["since"], "until": корзины[-1]["until"]}
    assert sum(т["records"] for т in корзины) == _разбор(c, целиком)


def test_граница_корзины_та_же_что_у_базы_до_последнего_бита(архив, c):
    """Запись ровно на стыке корзин обязана попасть в одну и ту же корзину
    и у ряда, и у списка.

    База считает номер корзины делением, а «начало + номер × шаг» из-за
    округления встаёт на соседнее число. Записи ставятся на сами стыки и
    на соседние с ними числа — там, где наивная граница ошибается.
    """
    db = архив.db
    начало, конец, корзин = 978_300_000.123457, 978_300_000.123457 + 86400 * 7 / 3, 17
    корзин, шаг, _ = db.content_series(since=начало, until=конец, buckets=корзин)
    края = _границы_корзин(начало, шаг, корзин, конец)
    времена: list[float] = []
    for k in range(1, корзин):
        наивная = начало + k * шаг
        for t in (наивная, math.nextafter(наивная, 0), math.nextafter(наивная, math.inf),
                  края[k][0], math.nextafter(края[k][0], 0)):
            времена.append(t)
    времена += [начало, math.nextafter(конец, 0)]
    for n, t in enumerate(времена):
        job_id = db.create_job({"id": f"edge{n:03d}", "filename": f"стык-{n}.wav",
                                "owner": "анна", "engine": "demo", "model": "demo-simulator"})
        db.update_job(job_id, status="completed", text="стык", finished_at=t + 1)
        db.execute("UPDATE jobs SET created_at = ? WHERE id = ?", (t, job_id))
        db.save_content(job_id, {"version": 1, "sentiment": -0.5})
    _, _, строки = db.content_series(since=начало, until=конец, buckets=корзин)
    по_базе = {int(с["bucket"]): int(с["records"]) for с in строки}
    assert sum(по_базе.values()) == len(времена)
    наивно_разошлось = 0
    for k in range(корзин):
        окно = {"since": края[k][0], "until": края[k][1]}
        assert _разбор(c, окно) == по_базе.get(k, 0), k
        наивное = {"since": начало + k * шаг, "until": начало + (k + 1) * шаг}
        наивно_разошлось += _разбор(c, наивное) != по_базе.get(k, 0)
    # Проверка не пустая: посчитанная заново граница на этих данных ошибается.
    assert наивно_разошлось > 0


@pytest.mark.parametrize("period", ["week", "month", "all"])
def test_точка_контрольной_карты_раскрывается_в_записи_своих_суток(c, period):
    карты = c.get("/api/content/control", params={"period": period}).json()["charts"]
    assert {к["key"] for к in карты} == {"negative_share", "agent_score", "sentiment",
                                         "compliance"}
    чем = {"agent_score": "has:agent_score", "sentiment": "has:sentiment",
           "compliance": "has:compliance"}
    непустых = 0
    for карта in карты:
        for точка in карта["points"]:
            окно = {"since": точка["since"], "until": точка["until"]}
            assert _разбор(c, окно) == точка["records"], (карта["key"], точка["ts"])
            if not точка["records"]:
                continue
            непустых += 1
            if карта["key"] == "negative_share":
                оценено = _разбор(c, окно, ["scored"])
                доля = (round(100.0 * _разбор(c, окно, ["negative"]) / оценено, 1)
                        if оценено else None)
                assert доля == точка["value"]
            else:
                значения = _значения(c, окно, [чем[карта["key"]]],
                                     чем[карта["key"]].split(":")[1] + ":asc")
                среднее = sum(значения) / len(значения) if значения else None
                assert среднее == pytest.approx(точка["value"]), (карта["key"], точка["ts"])
    assert непустых >= 8


# ---------------------------------------------------------------------------
# Разрезы: «Напряжение по разрезам», «NPS по разрезам», столбики по людям
# ---------------------------------------------------------------------------


def _отбор_группы(разрез: str, ключ: str) -> list[str]:
    """То же соответствие, что `отборГруппыРазбора` в интерфейсе."""
    if разрез in ("agent", "queue", "station", "direction", "owner", "speaker"):
        return [f"{разрез}:{'' if ключ == '—' else ключ}"]
    if разрез == "category":
        return [f"category:{ключ}"]
    if разрез == "hour":
        return [f"hour:{int(ключ)}"]
    assert разрез == "weekday"
    return [f"weekday:{(int(ключ) + 6) % 7}"]      # свод считает от воскресенья


@pytest.mark.parametrize("разрез", ["agent", "queue", "station", "category", "direction",
                                    "hour", "weekday", "owner"])
def test_столбец_разреза_раскрывается_в_записи_своей_группы(c, разрез):
    ответ = c.get(f"/api/content/breakdown/{разрез}", params={"period": "month"}).json()
    окно = ответ["window"]
    assert len(ответ["items"]) >= 2, разрез
    for группа in ответ["items"]:
        отбор = _отбор_группы(разрез, группа["key"])
        assert _разбор(c, окно, отбор) == группа["records"] > 0, (разрез, группа["key"])
        # Столбцы показывают среднее напряжение и индекс NPS: раскрываются
        # они в записи группы, где величина измерена.
        assert _разбор(c, окно, [*отбор, "has:stress"]) == группа["stress_checked"]
        assert _разбор(c, окно, [*отбор, "has:nps"]) == группа["nps_checked"]
    assert sum(г["stress_checked"] for г in ответ["items"]) > 0
    assert sum(г["nps_checked"] for г in ответ["items"]) > 0
    if разрез != "category":
        # Группы разреза не пересекаются (у категорий запись входит в
        # несколько), а мелкие скрыты: в сумме — не больше корпуса.
        assert sum(г["records"] for г in ответ["items"]) <= _разбор(c, окно)


@pytest.mark.parametrize("по_кому", ["agent", "speaker", "owner", "queue", "station"])
def test_столбик_по_человеку_раскрывается_в_его_записи(c, по_кому):
    """«Слова-паразиты», «Уменьшительные», «Ритмичность» на «Понятности»."""
    ответ = c.get("/api/content/employees", params={"period": "month", "by": по_кому}).json()
    assert ответ["by"] == по_кому and ответ["window"]["since"]
    люди = [ч for ч in ответ["items"] if ч["records"]]
    assert len(люди) >= 2, по_кому
    for человек in люди:
        отбор = _отбор_группы(по_кому, человек["key"])
        assert _разбор(c, ответ["window"], отбор) == человек["records"], человек["key"]
        # Свод округляет среднее: доли — до шестого знака, ритмичность — до второго.
        for поле, точность in (("filler_rate", 1e-6), ("diminutive_rate", 1e-6),
                               ("rhythm", 0.006)):
            значения = _значения(c, ответ["window"], [*отбор, f"has:{поле}"], f"{поле}:desc")
            if человек[поле] is None:
                assert not значения
            else:
                assert sum(значения) / len(значения) == pytest.approx(
                    человек[поле], abs=точность), (человек["key"], поле)
    assert sum(ч["records"] for ч in люди) == _разбор(c, ответ["window"])


# ---------------------------------------------------------------------------
# Категории и драйверы негатива
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ПЕРИОДЫ)
def test_счёт_по_категориям_равен_спискам(c, period):
    свод = c.get("/api/content/categories", params={"period": period}).json()
    окно = свод["window"]
    assert свод["corpus"] == _разбор(c, окно) > 0
    без = свод["uncategorized"]["records"]
    assert без == _разбор(c, окно, ["uncategorized"]) > 0
    assert свод["corpus"] - без == _разбор(c, окно, ["categorized"]) > 0
    с_записями = [к for к in свод["items"] if к["records"]]
    assert len(с_записями) >= 3
    for категория in свод["items"]:
        отбор = [f"category:{категория['id']}"]
        assert _разбор(c, окно, отбор) == категория["records"], категория["id"]
    assert {к["kind"] for к in с_записями} >= {"topic", "violation", "objection"}


def test_категория_с_совпадениями_двух_видов_считается_целиком(c, архив):
    """Вид категории сменили без пересчёта архива: совпадения в нём двух
    видов. Свод брал одну из двух групп — меньшую — и показывал «5 записей»
    при девятнадцати в архиве. Теперь группы складываются: запись в
    категории одна, какого бы вида ни было её совпадение."""
    свод = c.get("/api/content/categories", params={"period": "month"}).json()
    сроки = next(к for к in свод["items"] if к["id"] == "deadline")
    по_видам = {str(р["kind"]): int(р["n"]) for р in архив.db.query(
        "SELECT h.kind, COUNT(*) AS n FROM content_hits h JOIN jobs j ON j.id = h.job_id "
        "WHERE h.category = 'deadline' AND j.status = 'completed' AND j.created_at >= ? "
        "GROUP BY h.kind", (свод["window"]["since"],))}
    assert len(по_видам) == 2 and min(по_видам.values()) > 0, "досев не дал двух видов"
    assert сроки["records"] == sum(по_видам.values())
    assert сроки["records"] == _разбор(c, свод["window"], ["category:deadline"])
    assert сроки["mentions"] >= сроки["records"]


def test_колонка_было_раскрывается_прошлым_окном(c):
    свод = c.get("/api/content/categories", params={"period": "day"}).json()
    прошлое = свод["window_previous"]
    assert прошлое["until"] == свод["window"]["since"]
    assert свод["corpus_previous"] == _разбор(c, прошлое) > 0
    были = [к for к in свод["items"] if к["previous"]]
    assert len(были) >= 2
    for категория in были:
        отбор = [f"category:{категория['id']}"]
        assert _разбор(c, прошлое, отбор) == категория["previous"], категория["id"]
    assert c.get("/api/content/categories",
                 params={"period": "all"}).json()["window_previous"] is None


@pytest.mark.parametrize("period", ["week", "month", "all"])
def test_драйверы_считают_оценённые_записи_категории(c, period):
    """«Записей» у драйвера — не все записи категории, а с оценкой
    тональности: отбор обязан это повторить."""
    драйверы = c.get("/api/content/drivers", params={"period": period}).json()
    окно = драйверы["window"]
    assert драйверы["items"], "за период нет категорий с десятью оценёнными записями"
    for строка in драйверы["items"]:
        отбор = [f"category:{строка['id']}"]
        assert _разбор(c, окно, [*отбор, "scored"]) == строка["records"] > 0, строка["id"]
        assert _разбор(c, окно, [*отбор, "negative"]) == строка["negative"], строка["id"]
    # Категория — одна строка драйверов, даже когда совпадения у неё двух видов.
    assert len({с["id"] for с in драйверы["items"]}) == len(драйверы["items"])
    assert any(_разбор(c, окно, [f"category:{с['id']}"]) > с["records"]
               for с in драйверы["items"]), "у всех категорий оценены все записи"
    assert sum(с["negative"] for с in драйверы["items"]) > 0


# ---------------------------------------------------------------------------
# Свод: блок «По ответам языковой модели»
# ---------------------------------------------------------------------------

РАЗОБРАНО = ["status:completed", "no_control", "llm_ok"]


def test_свод_модели_равен_спискам(c):
    """Основа другая — ответы модели, и условия свои: готовые задания без
    контрольных прогонов, ответ без сбоя."""
    д = c.get("/api/content/llm", params={"period": "month"}).json()
    окно = д["window"]
    assert окно["since"] and окно["until"] > окно["since"]

    def ответов(*отбор: str) -> int:
        return всего(c, окно, [*РАЗОБРАНО, *отбор], base="llm")

    assert д["analyzed"] == ответов() > 0
    assert д["records"] == всего(c, окно, ["status:completed", "no_control"])
    assert д["errors"] == всего(c, окно, ["status:completed", "no_control", "llm_failed"],
                                base="llm") == 1
    assert д["records_with_actions"] == ответов("with_actions") > 0
    assert д["off_list"] == ответов("off_list") == 1
    решено = sum(1 for р in д["rows"] if р["resolved"] is True and not р["error"])
    известно = sum(1 for р in д["rows"] if р["resolved"] is not None and not р["error"])
    assert решено == ответов("resolved:1") > 0
    assert известно == ответов("resolved_known") > решено
    assert д["resolved_share"] == round(100.0 * решено / известно, 1)
    for исход in д["outcomes"]:
        assert исход["records"] == ответов(f"outcome:{исход['key']}") > 0
    for причина in д["reasons"]:
        assert причина["records"] == ответов(f"reason:{причина['key']}") > 0
    assert len(д["trackers"]) == 2 and len(д["scorecard"]) == 1
    for трекер in д["trackers"]:
        assert трекер["checked"] == ответов(f"tracker_checked:{трекер['id']}") > 0
        assert трекер["fired"] == ответов(f"tracker:{трекер['id']}") > 0
    assert д["trackers"][0]["checked"] != д["trackers"][1]["checked"]
    for вопрос in д["scorecard"]:
        assert вопрос["да"] == ответов(f"score_yes:{вопрос['id']}") > 0
        assert вопрос["нет"] == ответов(f"score_no:{вопрос['id']}") > 0
        assert вопрос["н/п"] == ответов(f"score_na:{вопрос['id']}") > 0
    # «Всё время»: нижней границы нет — и в окне её нет, а не ноль.
    assert c.get("/api/content/llm", params={"period": "all"}).json()["window"]["since"] is None


# ---------------------------------------------------------------------------
# Окна и величины раздела
# ---------------------------------------------------------------------------


def test_у_каждого_ответа_вкладки_своё_окно(c):
    """Вкладка собирается из нескольких запросов, и каждый замораживает своё
    «сейчас»: число раскрывается окном того ответа, из которого взято."""
    адреса = ("/api/content/summary", "/api/content/timeline", "/api/content/categories",
              "/api/content/drivers", "/api/content/breakdown/owner",
              "/api/content/employees", "/api/content/control", "/api/content/llm")
    окна = [c.get(адрес, params={"period": "week"}).json()["window"] for адрес in адреса]
    for окно in окна:
        assert окно["until"] - окно["since"] == pytest.approx(7 * 86400, abs=5)
    assert len({окно["since"] for окно in окна}) == len(окна)


def test_величины_раздела_есть_в_перечне(c):
    assert всего(c, None, ["category:x' OR 1=1 --"], base="content") == 0
    for величина in ("pauses", "switches", "overlap_s", "tempo_ratio",
                     "objections_unhandled", "content_speakers", "duration_s",
                     "commitments_open"):
        assert величина in drill.ЧИСЛА
    assert drill.раскрытие_показателя("нет_такого") is None
    assert drill.раскрытие_показателя("sentiment", 1)["sort"] == "sentiment:asc"
    assert drill.раскрытие_показателя("stress", -1)["sort"] == "stress:desc"
