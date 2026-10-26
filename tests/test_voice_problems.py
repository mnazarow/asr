"""Заход 53: проблемы и сложности разговоров за период — сводка по всем разговорам.

`GET /api/voice/problems` (модуль `voice_problems`) сводит признаки,
посчитанные по каждому разговору, — разбор содержания, ответ языковой
модели, журнал звонков, проверку звука — в один перечень проблем за
период. Здесь проверяется:

* каждое число перечня раскрывается в список ровно из стольких разговоров
  (число = список) — и сама проблема, и её частые причины и сотрудники,
  и разрезы «у кого» и «с какой причиной»;
* доля считается от тех, у кого признак мог быть;
* окно и база: календарный период (с базой «на ту же дату») и скользящее
  окно (база — такое же окно перед ним);
* отбор по добавочному сужает сводку;
* сводку языковой моделью можно составить, она хранится и помечается
  устаревшей, когда разговоров стало больше; без модели — понятный отказ.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from asrhub import voice_problems as П
from drill_seed import Архив, всего


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    а = Архив(tmp_path_factory.mktemp("voice-problems") / "data", заданий=96, с_моделью=30)
    yield а
    а.закрыть()


@pytest.fixture(scope="module")
def клиент(архив):
    from fastapi.testclient import TestClient

    with TestClient(архив.app) as c:
        yield c


def _сводка(клиент, **параметры: Any) -> dict[str, Any]:
    ответ = клиент.get("/api/voice/problems", params=параметры)
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _раскрыть(сводка: dict[str, Any], строка: dict[str, Any], *ещё: str) -> dict[str, Any]:
    """Отборы списка под строкой — так же, как их собирает интерфейс."""
    что = строка["drill"]
    звонки = что["base"] == "calls"
    раздел = [о for о in сводка["drill"] if not (звонки and о == "no_control")]
    готовые = [] if звонки or что.get("status") else ["status:completed"]
    return {"base": что["base"], "f": [*готовые, *раздел, *что["f"], *ещё]}


def test_перечень_раскрывается_существующими_отборами():
    from asrhub.drill import describe

    перечень = describe()
    задания = {о["name"] for о in перечень["filters"]}
    звонки = {о["name"] for о in перечень["call_filters"]}
    for п in П.ПРОБЛЕМЫ:
        for отбор in п.отборы:
            assert отбор.split(":")[0] in задания, (п.id, отбор)
    assert {"abandoned", "answered", "wait_over"} <= звонки
    # Имена проблем уникальны, у каждой своя группа из перечня групп.
    assert len({п.id for п in П.ПРОБЛЕМЫ}) == len(П.ПРОБЛЕМЫ)
    assert {п.группа for п in П.ПРОБЛЕМЫ} <= {г for г, _ in П.ГРУППЫ}


def test_число_проблемы_равно_списку(клиент):
    сводка = _сводка(клиент, period="week")
    assert сводка["conversations"] > 20 and сводка["with_problems"] > 0
    проверено = 0
    for строка in сводка["items"]:
        if not строка["records"]:
            continue
        раскрытие = _раскрыть(сводка, строка)
        assert всего(клиент, сводка["window"], раскрытие["f"], base=раскрытие["base"]) \
            == строка["records"], строка["id"]
        проверено += 1
        # Частые причины и сотрудники строки — тоже списками той же длины.
        for причина in строка["reasons"]:
            р = _раскрыть(сводка, строка, f"reason:{причина['reason']}")
            assert всего(клиент, сводка["window"], р["f"], base=р["base"]) \
                == причина["records"], (строка["id"], причина)
        for человек in строка["agents"]:
            р = _раскрыть(сводка, строка, f"agent:{человек['agent']}")
            assert всего(клиент, сводка["window"], р["f"], base=р["base"]) \
                == человек["records"], (строка["id"], человек)
    assert проверено >= 6


def test_доля_от_тех_у_кого_признак_мог_быть(клиент):
    сводка = _сводка(клиент, period="week")
    по_имени = {с["id"]: с for с in сводка["items"]}
    # «Вопрос не решён» — от разговоров, где модель ответила, решён ли он.
    нерешён = по_имени["unresolved"]
    знает = всего(клиент, сводка["window"],
                  ["status:completed", *сводка["drill"], "llm_ok", "resolved_known"], base="llm")
    assert нерешён["base"] == знает < сводка["conversations"]
    assert нерешён["share"] == pytest.approx(нерешён["records"] / знает, abs=1e-4)
    # Технические — от всех записей окна, вместе с без речи и короткими.
    без_речи = по_имени["no_speech"]
    assert без_речи["group"] == "record"
    assert без_речи["base"] == сводка["records"]["jobs"] > сводка["conversations"]


def test_разговор_с_проблемой_и_разрезы(клиент):
    сводка = _сводка(клиент, period="week")
    # «С проблемами» — не больше разговоров и не меньше самой частой проблемы.
    разговорные = [с for с in сводка["items"] if с["group"] in П.ГРУППЫ_РАЗГОВОРА
                   and not с.get("calls")]
    assert max(с["records"] for с in разговорные) <= сводка["with_problems"] \
        <= сводка["conversations"]
    assert сводка["problems_total"] == sum(с["records"] for с in разговорные)
    # Разрез по сотрудникам: число разговоров — список записей добавочного.
    for человек in сводка["by_agent"]:
        assert человек["conversations"] >= П.МИНИМУМ_У_СОТРУДНИКА
        assert всего(клиент, сводка["window"],
                     ["status:completed", *сводка["drill"], f"agent:{человек['agent']}"],
                     base="jobs") == человек["conversations"]
    # Разрез по причинам — то же через причину из ответа модели.
    for причина in сводка["by_reason"]:
        assert всего(клиент, сводка["window"],
                     ["status:completed", *сводка["drill"], "llm_ok",
                      f"reason:{причина['reason']}"], base="llm") == причина["conversations"]
    assert сводка["summary"] and сводка["summary"][0].startswith("Проблемы есть в")


def test_окно_и_база(клиент, архив):
    сейчас = архив.данные["now"]
    сводка = _сводка(клиент, period="day", since=сейчас - 86400, until=сейчас)
    assert сводка["base"]["since"] == pytest.approx(сейчас - 2 * 86400)
    assert сводка["base"]["until"] == pytest.approx(сейчас - 86400)
    assert сводка["base"]["label"] == "предыдущие сутки"
    # Сдвиг доли — против базы, и значимость считается тем же критерием.
    for строка in сводка["items"]:
        if строка["base"] and строка["prev_base"]:
            assert строка["delta_pp"] == pytest.approx(
                (строка["records"] / строка["base"]
                 - строка["prev_records"] / строка["prev_base"]) * 100, abs=0.02)
    # За всё время сравнивать не с чем.
    assert _сводка(клиент, period="all")["base"] is None
    # Календарный месяц — с базой «прошлый месяц»; идущий — на ту же дату.
    месяц = _сводка(клиент, scale="month")
    assert месяц["period_id"].startswith("month:") and месяц["base"]["to_date"] is True
    assert месяц["window"]["until"] == pytest.approx(time.time(), abs=60)


def test_отбор_по_добавочному_сужает_сводку(клиент):
    все = _сводка(клиент, period="week")
    свои = _сводка(клиент, period="week", agent="101")
    assert 0 < свои["conversations"] < все["conversations"]
    assert "agent:101" in свои["drill"]
    assert свои["conversations"] == всего(
        клиент, свои["window"], ["status:completed", *свои["drill"]], base="jobs")


def test_неверный_масштаб_отвергается(клиент):
    assert клиент.get("/api/voice/problems", params={"scale": "week"}).status_code == 422
    assert клиент.get("/api/voice/problems", params={"period": "decade"}).status_code == 422
    assert клиент.get("/api/voice/problems",
                      params={"scale": "month", "date": "вчера"}).status_code == 400


def test_сводка_моделью_хранится_и_устаревает(клиент, архив):
    db = архив.db
    сводка = _сводка(клиент, period="week")
    assert сводка["llm_enabled"] is True and сводка["digest"] is None
    ответ = клиент.post("/api/voice/problems/digest", params={"period": "week"})
    assert ответ.status_code == 200, ответ.text
    составлено = ответ.json()
    результат = составлено["result"]
    assert результат["overview"] and результат["problems"]
    assert составлено["conversations"] == сводка["conversations"]
    assert 0 < len(составлено["sample"]) <= П.ПЕРЕСКАЗОВ
    # Хранится: GET отдаёт её полем `digest`, пока разговоров столько же.
    снова = _сводка(клиент, period="week")
    assert снова["digest"]["result"] == результат and снова["digest"]["stale"] is False
    # У другого добавочного своя сводка.
    assert _сводка(клиент, period="week", agent="101")["digest"] is None
    # Разговор прибавился — сводка устарела.
    номер = db.create_job({"id": "probnew", "filename": "новый.wav", "owner": "анна"})
    db.update_job(номер, status="completed", text="Добрый день. Жалоба на доставку.",
                  finished_at=time.time())
    assert _сводка(клиент, period="week")["digest"]["stale"] is True


def test_без_модели_сводку_не_составить(клиент, архив):
    настройки = архив.app.state.hub.settings
    прежний = настройки.get("llm_backend")
    настройки.set("llm_backend", "off")
    try:
        ответ = клиент.post("/api/voice/problems/digest", params={"period": "week"})
        assert ответ.status_code == 400
        assert ответ.json()["code"] == "config_error" and "выключена" in ответ.json()["message"]
        # Перечень считается и без модели.
        assert _сводка(клиент, period="week")["llm_enabled"] is False
    finally:
        настройки.set("llm_backend", прежний)


def test_подсказка_модели_несёт_числа_и_пересказы(архив):
    from asrhub import voice_period as В

    сейчас = архив.данные["now"]
    окна = П.Окна(since=сейчас - 7 * 86400, until=сейчас, label="неделя")
    отбор = В.Отбор()
    сводка = П.посчитать(архив.db, окна, отбор)
    пересказы = П.выборка(архив.db, окна, отбор)
    assert пересказы and all(р["summary"] and р["flags"] for р in пересказы)
    текст = П.подсказка(сводка, пересказы)
    частая = max((с for с in сводка["items"] if с["records"]), key=lambda с: с["share"] or 0)
    assert частая["label"] in текст and f"{частая['records']} из {частая['base']}" in текст
    assert пересказы[0]["summary"][:40] in текст
    # Ответ модели разбирается терпимо: кривые поля отбрасываются.
    разобрано = П.разобрать_сводку(
        '{"overview": "Главное.", "problems": [{"title": "Долгое ожидание", '
        '"details": "у трети", "examples": ["ждал 5 минут", ""]}, "мусор", {"details": "без '
        'названия"}], "recommendations": ["добавить смену"]}')
    assert разобрано["problems"] == [{"title": "Долгое ожидание", "details": "у трети",
                                      "examples": ["ждал 5 минут"]}]
    assert разобрано["recommendations"] == ["добавить смену"] and разобрано["causes"] == []
