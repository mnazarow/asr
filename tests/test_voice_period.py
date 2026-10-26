"""Заход 52: «Голосовая аналитика за день, месяц, квартал и год».

Один отчёт за календарный период с базами сравнения (`GET /api/voice/period`,
модуль `voice_period`). Здесь проверяется:

* календарь: границы периодов, базы сравнения, корзины по времени;
* незакрытый период сравнивается с тем же отрезком от начала базы;
* итоги, ряды, причины, сотрудники и отделы сходятся друг с другом и со
  списками раскрытия (число = список) — при тех же окне и отборе;
* без речи и короткие — в объёме, но не в долях;
* изменение доли называется значимым, только когда оно больше случайного;
* отборы по добавочному и отделу сужают весь отчёт.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from asrhub import voice_period as В
from asrhub.llm import tasks

# ---------------------------------------------------------------------------
# Календарь
# ---------------------------------------------------------------------------


def test_начало_и_сдвиг_периода():
    день = dt.date(2026, 10, 26)
    assert В.начало("day", день) == день
    assert В.начало("month", день) == dt.date(2026, 10, 1)
    assert В.начало("quarter", день) == dt.date(2026, 10, 1)
    assert В.начало("quarter", dt.date(2026, 8, 31)) == dt.date(2026, 7, 1)
    assert В.начало("year", день) == dt.date(2026, 1, 1)
    assert В.сдвинуть("month", dt.date(2026, 1, 1), -1) == dt.date(2025, 12, 1)
    assert В.сдвинуть("quarter", dt.date(2026, 1, 1), -1) == dt.date(2025, 10, 1)
    assert В.сдвинуть("day", dt.date(2026, 3, 1), -1) == dt.date(2026, 2, 28)
    assert В.подпись("day", день) == "26 октября 2026, понедельник"
    assert В.подпись("month", dt.date(2026, 10, 1)) == "октябрь 2026"
    assert В.подпись("quarter", dt.date(2026, 10, 1)) == "IV квартал 2026"
    assert В.подпись("year", dt.date(2026, 1, 1)) == "2026 год"


def test_базы_сравнения_по_масштабу():
    день = В.базы("day", dt.date(2026, 10, 26))
    assert [б.id for б in день] == ["week", "prev"]
    assert [б.first for б in день] == [dt.date(2026, 10, 19), dt.date(2026, 10, 25)]
    месяц = В.базы("month", dt.date(2026, 10, 1))
    assert [(б.id, б.first) for б in месяц] == [("prev", dt.date(2026, 9, 1)),
                                                 ("yoy", dt.date(2025, 10, 1))]
    квартал = В.базы("quarter", dt.date(2026, 10, 1))
    assert [б.first for б in квартал] == [dt.date(2026, 7, 1), dt.date(2025, 10, 1)]
    год = В.базы("year", dt.date(2026, 1, 1))
    assert [(б.id, б.first) for б in год] == [("prev", dt.date(2025, 1, 1))]


def test_корзины_по_масштабу():
    assert len(В.корзины("day", dt.date(2026, 10, 26))) == 24
    assert len(В.корзины("month", dt.date(2026, 2, 1))) == 28
    assert len(В.корзины("month", dt.date(2026, 10, 1))) == 31
    квартал = В.корзины("quarter", dt.date(2026, 10, 1))
    # 1 октября 2026 — четверг: первая неделя неполная, дальше с понедельников.
    assert квартал.labels[0] == "1 окт" and квартал.labels[1] == "5 окт"
    assert квартал.номер("2026-10-04") == 0 and квартал.номер("2026-10-05") == 1
    assert квартал.номер("2026-12-31") == len(квартал) - 1
    год = В.корзины("year", dt.date(2026, 1, 1))
    assert len(год) == 12 and год.номер("2026-07-15") == 6
    # Края идут подряд: конец корзины — начало следующей, последняя — конец периода.
    for к in (квартал, год):
        assert к.edges == sorted(к.edges) and len(к.edges) == len(к) + 1
    assert год.edges[-1] == dt.datetime(2027, 1, 1).timestamp()


def test_значимость_изменения_доли():
    # 30 % против 10 % на сотнях — значимо; на десятке — нет.
    assert В._сравнить_доли(30, 100, 10, 100)["significant"] is True
    assert В._сравнить_доли(3, 10, 1, 10)["significant"] is False
    почти = В._сравнить_доли(52, 100, 48, 100)
    assert почти["delta_pp"] == 4.0 and почти["significant"] is False


# ---------------------------------------------------------------------------
# Отчёт на своих данных
# ---------------------------------------------------------------------------

СЕЙЧАС = dt.datetime(2026, 10, 20, 15, 0).timestamp()        # вторник, 20 октября 2026


def _задание(db: Any, ид: str, когда: dt.datetime, статус: str = "completed", *,
             агент: str = "", причина: str = "", исход: str = "", решено: bool | None = None,
             тон: float | None = None, длит: float = 120.0) -> None:
    db.create_job({"id": ид, "filename": f"{ид}.wav", "source": "phone",
                   "media_duration_s": длит, "model": "demo-simulator"})
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда.timestamp(), ид))
    db.update_job(ид, status=статус, finished_at=когда.timestamp() + 5)
    if агент:
        db.save_call(f"t:{ид}", job_id=ид, agent=агент, dst=агент, src="+79000000000",
                     started_at=когда.timestamp() - 10, answered=True, station="t",
                     direction="входящий", duration=int(длит) + 15, billsec=int(длит))
    if статус != "completed":
        return
    if тон is not None:
        db.execute("INSERT INTO content (job_id, version, computed_at, sentiment, agent_score) "
                   "VALUES (?,?,?,?,?)", (ид, 1, когда.timestamp(), тон, 70.0))
    if причина:
        db.llm_save(ид, tasks.VERSION, model="t", reason=причина, outcome=исход or "решено",
                    resolved=решено, summary="")


@pytest.fixture(scope="module")
def отчёт_база(tmp_path_factory):
    from asrhub.api import create_app
    from asrhub.config import load

    каталог = tmp_path_factory.mktemp("voice-period") / "data"
    import os

    прежние = {к: os.environ.get(к) for к in ("ASRHUB_DATA_DIR", "ASRHUB_AUTH_ENABLED",
                                             "ASRHUB_MODEL")}
    os.environ.update({"ASRHUB_DATA_DIR": str(каталог), "ASRHUB_AUTH_ENABLED": "false",
                       "ASRHUB_MODEL": "demo-simulator"})
    app = create_app(load(), start_queue=False)
    db = app.state.hub.db
    db.employee_save({"last_name": "Петрова", "first_name": "Мария", "department": "Продажи",
                      "phone_ext": "101", "source": "т", "external_id": "101"})
    db.employee_save({"last_name": "Сидоров", "first_name": "Пётр", "department": "Сервис",
                      "phone_ext": "102", "source": "т", "external_id": "102"})
    n = 0

    def добавить(день: dt.date, час: int, статус: str = "completed", **поля: Any) -> None:
        nonlocal n
        n += 1
        _задание(db, f"p{n:04d}", dt.datetime(день.year, день.month, день.day, час, 10),
                 статус, **поля)

    # Октябрь 2026 до 20-го (15:00): 40 разговоров, 4 без речи, 2 коротких, 1 ошибка.
    for k in range(40):
        день = dt.date(2026, 10, 1 + k % 19)
        добавить(день, 9 + k % 6, агент=("101", "102")[k % 2],
                 причина=("доставка", "оплата", "возврат")[k % 3] if k % 5 else "доставка",
                 исход=("решено", "перезвонить")[k % 2], решено=k % 2 == 0,
                 тон=-0.5 if k % 4 == 0 else 0.3)
    for k in range(4):
        добавить(dt.date(2026, 10, 2 + k), 10, "no_speech", агент="101")
    for k in range(2):
        добавить(dt.date(2026, 10, 3 + k), 11, "short", агент="102")
    добавить(dt.date(2026, 10, 5), 12, "failed")
    # После «сейчас» в этом же месяце — не входит ни во что.
    добавить(dt.date(2026, 10, 20), 18, агент="101", причина="доставка")
    # Сентябрь: 30 разговоров в первые 19 дней и 10 — после 20-го.
    for k in range(40):
        добавить(dt.date(2026, 9, 1 + (k % 19 if k < 30 else 20 + k % 10)), 10,
                 агент="101", причина="оплата" if k % 2 else "доставка",
                 исход="решено", решено=True, тон=0.2)
    # Октябрь 2025 — год назад.
    for k in range(10):
        добавить(dt.date(2025, 10, 1 + k), 10, причина="возврат", исход="решено", решено=False)
    yield app, db
    for к, v in прежние.items():
        if v is None:
            os.environ.pop(к, None)
        else:
            os.environ[к] = v


def _отчёт(db, масштаб="month", день=dt.date(2026, 10, 20), **отбор) -> dict[str, Any]:
    return В.Отчёт(db, масштаб, день, В.Отбор(**отбор), сейчас=СЕЙЧАС).посчитать()


def test_итоги_и_состав_месяца(отчёт_база):
    _, db = отчёт_база
    о = _отчёт(db)
    т = о["totals"]["current"]
    assert (т["completed"], т["no_speech"], т["short"], т["failed"]) == (40, 4, 2, 1)
    assert т["jobs"] == 47 and т["empty"] == 6
    assert т["talk_hours"] == round(40 * 120 / 3600, 2)
    assert т["median_duration_s"] == 120.0
    # Без речи и короткие — в объёме, но не в долях: тональность по 40.
    assert т["content"]["scored"] == 40 and т["content"]["negative"] == 10
    assert т["content"]["negative_share"] == 0.25
    assert т["llm"]["analyzed"] == 40 and т["llm"]["coverage"] == 1.0
    assert о["complete"] is False and 0 < о["elapsed"] < 1


def test_база_сравнения_на_ту_же_дату(отчёт_база):
    _, db = отчёт_база
    о = _отчёт(db)
    прошлый = о["totals"]["prev"]
    # Сентябрь обрезан теми же 19 днями с хвостиком: 30, а не 40.
    assert прошлый["completed"] == 30
    база = о["baselines"][0]
    assert база["to_date"] is True and база["until"] < база["end"]
    assert о["totals"]["yoy"]["completed"] == 10
    # Закрытый период — полностью и без обрезки.
    сентябрь = _отчёт(db, день=dt.date(2026, 9, 10))
    assert сентябрь["complete"] is True
    assert сентябрь["totals"]["current"]["completed"] == 40
    assert сентябрь["baselines"][0]["to_date"] is False


def test_ряды_складываются_в_итоги(отчёт_база):
    _, db = отчёт_база
    о = _отчёт(db)
    ряд = о["series"]
    т = о["totals"]["current"]
    assert len(ряд["labels"]) == 31 and len(ряд["edges"]) == 32
    assert sum(ряд["completed"]) == т["completed"]
    assert sum(ряд["empty"]) == т["empty"] and sum(ряд["failed"]) == т["failed"]
    assert sum(ряд["analyzed"]) == т["llm"]["analyzed"]
    assert sum(ряд["baselines"]["prev"]) == о["totals"]["prev"]["completed"]
    assert sum(sum(r) for r in о["week_map"]) == т["completed"]


def test_причины_с_изменением_и_накоплением(отчёт_база):
    _, db = отчёт_база
    о = _отчёт(db)
    по = {п["key"]: п for п in о["reasons"]}
    assert sum(п["records"] for п in о["reasons"]) == 40
    assert о["reasons"][-1]["cumulative_share"] == 1.0
    доставка = по["доставка"]
    assert доставка["prev_records"] == 15 and доставка["prev_share"] == 0.5
    assert доставка["delta_pp"] == round((доставка["share"] - 0.5) * 100, 2)
    # «возврат» в сентябре не звучал — доля была нулём, а не «не было базы».
    assert по["возврат"]["prev_records"] == 0 and по["возврат"]["prev_share"] == 0.0


def test_сотрудники_и_отделы(отчёт_база):
    app, db = отчёт_база
    from asrhub.api.routes_voice import _отделы
    from asrhub.employees import подписать_добавочные

    о = _отчёт(db)
    люди = {с["agent"]: с for с in о["agents"]}
    assert люди["101"]["completed"] == 20 and люди["101"]["empty"] == 4
    assert люди["102"]["completed"] == 20 and люди["102"]["empty"] == 2
    assert люди["101"]["prev_completed"] == 30
    assert люди["101"]["resolved_share"] == 1.0 and люди["102"]["resolved_share"] == 0.0
    assert о["without_agent"]["jobs"] == 1                       # ошибка без звонка
    подписать_добавочные(db, о["agents"])
    отделы = {о_["department"]: о_ for о_ in _отделы(о["agents"])}
    assert отделы["Продажи"]["completed"] == 20 and отделы["Сервис"]["completed"] == 20
    assert отделы["Продажи"]["resolved_share"] == 1.0


def test_отборы_сужают_весь_отчёт(отчёт_база):
    _, db = отчёт_база
    свой = _отчёт(db, agent="102")
    т = свой["totals"]["current"]
    assert (т["completed"], т["short"], т["no_speech"]) == (20, 2, 0)
    assert свой["drill"] == ["no_control", "agent:102"]
    отдел = _отчёт(db, department="Продажи")
    assert отдел["totals"]["current"]["completed"] == 20
    assert отдел["drill"] == ["no_control", "department:Продажи"]


def test_масштабы_день_квартал_год(отчёт_база):
    _, db = отчёт_база
    день = _отчёт(db, "day", dt.date(2026, 10, 5))
    assert len(день["series"]["labels"]) == 24 and "week_map" not in день
    assert [б["id"] for б in день["baselines"]] == ["week", "prev"]
    квартал = _отчёт(db, "quarter")
    assert квартал["totals"]["current"]["completed"] == 40
    # Прошлый квартал — тоже «на ту же дату»: 1–20 июля, а сентябрь в него не входит.
    assert квартал["totals"]["prev"]["completed"] == 0
    assert квартал["baselines"][0]["date"] == "2026-07-01" and квартал["baselines"][0]["to_date"]
    assert sum(д["completed"] for д in квартал["calendar"]) == 40
    год = _отчёт(db, "year")
    assert len(год["series"]["labels"]) == 12
    assert год["series"]["completed"][9] == 40 and год["series"]["completed"][8] == 40
    assert год["totals"]["prev"]["completed"] == 10


def test_коротко_о_периоде(отчёт_база):
    _, db = отчёт_база
    о = _отчёт(db)
    фразы = В.коротко(о)
    assert фразы[0].startswith("Разговоров с речью — 40 из 47 записей (без речи 4, коротких 2, "
                               "с ошибкой 1)")
    assert "к периоду «сентябрь 2026»" in фразы[0]
    assert any("Период ещё идёт" in ф for ф in фразы)
    assert all("п, п," not in ф for ф in фразы)


def test_будущий_период(отчёт_база):
    _, db = отчёт_база
    о = В.Отчёт(db, "month", dt.date(2026, 12, 5), В.Отбор(), сейчас=СЕЙЧАС).посчитать()
    assert о["future"] is True and "totals" not in о


# ---------------------------------------------------------------------------
# Маршрут и число = список
# ---------------------------------------------------------------------------


@pytest.fixture()
def клиент(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    app = create_app(load(), start_queue=False)
    db = app.state.hub.db
    db.employee_save({"last_name": "Петрова", "first_name": "Мария", "department": "Продажи",
                      "phone_ext": "101", "source": "т", "external_id": "101"})
    # Прошлый месяц — закрытый: числа не зависят от того, в какой час идёт проверка.
    сегодня = dt.date.today()
    прошлый = (сегодня.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    for k in range(24):
        когда = dt.datetime(прошлый.year, прошлый.month, 1 + k, 8 + k % 10, 15)
        статус = ("completed", "completed", "completed", "no_speech", "short", "failed")[k % 6]
        _задание(db, f"r{k:03d}", когда, статус, агент=("101", "102")[k % 2],
                 причина=("доставка", "оплата")[k % 2], исход="решено", решено=k % 3 == 0,
                 тон=-0.4 if k % 4 == 0 else 0.4)
    with TestClient(app) as c:
        yield c


def _список(клиент, окно, f, base="jobs") -> int:
    ответ = клиент.get("/api/drill", params={"base": base, "f": f, "limit": 1,
                                             "since": окно["since"], "until": окно["until"]})
    assert ответ.status_code == 200, ответ.text
    return int(ответ.json()["total"])


def test_числа_раскрываются_в_свои_записи(клиент):
    сегодня = dt.date.today()
    прошлый = (сегодня.replace(day=1) - dt.timedelta(days=1)).isoformat()
    ответ = клиент.get("/api/voice/period", params={"scale": "month", "date": прошлый})
    assert ответ.status_code == 200, ответ.text
    о = ответ.json()
    assert о["complete"] is True
    т, окно, отбор = о["totals"]["current"], о["window"], о["drill"]
    assert т["completed"] > 0
    assert _список(клиент, окно, [*отбор, "status:completed"]) == т["completed"]
    assert _список(клиент, окно, [*отбор, "status:no_speech,short"]) == т["empty"]
    assert _список(клиент, окно, [*отбор, "status:failed"]) == т["failed"]
    assert _список(клиент, окно, [*отбор, "status:completed", "resolved:1"],
                   base="llm") == т["llm"]["resolved_yes"]
    assert _список(клиент, окно, [*отбор, "status:completed", "negative"],
                   base="content") == т["content"]["negative"]
    for с in о["agents"]:
        assert _список(клиент, окно, [*отбор, "status:completed", f"agent:{с['agent']}"]) \
            == с["completed"]
    for д in о["departments"]:
        assert _список(клиент, окно, [*отбор, "status:completed",
                                      f"department:{д['department']}"]) == д["completed"]
    # Корзина ряда раскрывается в записи своей корзины.
    ряд = о["series"]
    for номер, n in enumerate(ряд["completed"]):
        if n:
            assert _список(клиент, {"since": ряд["edges"][номер], "until": ряд["edges"][номер + 1]},
                           [*отбор, "status:completed"]) == n
    for п in о["reasons"]:
        assert _список(клиент, окно, [*отбор, "status:completed", "llm_ok", f"reason:{п['key']}"],
                       base="llm") == п["records"]
    # Имена и сводка — только в маршруте.
    assert next(с for с in о["agents"] if с["agent"] == "101")["agent_name"] == "Петрова Мария"
    assert о["summary"] and о["options"]["departments"] == ["Продажи"]
    assert {ч["agent"] for ч in о["options"]["agents"]} == {"101", "102"}


def test_вторая_часть_и_проверка_параметров(клиент):
    тяжёлая = клиент.get("/api/voice/period", params={"scale": "year", "part": "heavy"})
    assert тяжёлая.status_code == 200 and "lists" in тяжёлая.json()
    assert "totals" not in тяжёлая.json()
    assert клиент.get("/api/voice/period", params={"scale": "week"}).status_code == 422
    плохая = клиент.get("/api/voice/period", params={"scale": "day", "date": "вчера"})
    assert плохая.status_code == 400 and "ГГГГ-ММ-ДД" in плохая.text
    for масштаб in В.МАСШТАБЫ:
        assert клиент.get("/api/voice/period", params={"scale": масштаб}).status_code == 200


def test_разделы_в_интерфейсе():
    корень = Path(__file__).resolve().parents[1] / "server/asrhub/web"
    код = (корень / "app.js").read_text("utf-8")
    меню = (корень / "index.html").read_text("utf-8")
    for вид, название in (("voice_day", "за день"), ("voice_month", "за месяц"),
                          ("voice_quarter", "за квартал"), ("voice_year", "за год")):
        assert f"RENDERERS.{вид} = периодГолоса(" in код
        assert f"Голосовая аналитика {название}" in код
        assert f'data-view="{вид}"' in меню
    # Порядок горячих клавиш — порядок меню.
    порядок = ["voice", "voice_day", "voice_month", "voice_quarter", "voice_year", "llmqueue"]
    позиции = [меню.index(f'data-view="{в}"') for в in порядок]
    assert позиции == sorted(позиции)
    assert "'voice', 'voice_day', 'voice_month', 'voice_quarter', 'voice_year'," in код.replace(
        "\n                      ", " ")
    for график in ("combo", "pareto", "calendar", "scatter"):
        assert f"function {график}(host, config)" in (корень / "charts.js").read_text("utf-8")
