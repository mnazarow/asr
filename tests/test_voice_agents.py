"""Заход 52: добавочные номера в «Голосовой аналитике».

Раздел показывал причины, исходы и обязательства без ответа на первый
вопрос к строке — «чей это разговор». Добавочный оператора лежит в журнале
звонков (с АТС, из `internalPhone` интеграции, из полей звонка при загрузке),
а имя — в справочнике сотрудников. Теперь:

* свод отдаёт разрез по добавочным (`agents`): разговоров, разобрано, доля
  решённых, частые причины и исходы — и он сходится со списками раскрытия;
* построчные разборы, обязательства и строки списков раскрытия приходят с
  добавочным и именем;
* раздел сужается до одного добавочного (`agent=`) — все его числа тоже.
"""
from __future__ import annotations

import time

import pytest
from drill_seed import Архив, всего
from test_drill_voice import _досеять

СВОИ = ("201", "202", "203")


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("voice-agents") / "data")
    _досеять(хранилище)
    db = хранилище.db
    # Разобранным записям без звонка — звонки с тремя добавочными.
    свободные = [р["id"] for р in db.query(
        "SELECT j.id FROM jobs j JOIN llm_results l ON l.job_id = j.id "
        "WHERE j.status = 'completed' AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.job_id = j.id) "
        "ORDER BY j.created_at DESC")]
    assert len(свободные) >= 12
    for n, ид in enumerate(свободные):
        db.save_call(f"api:{ид}", job_id=ид, src=f"+7900{n:07d}", dst=СВОИ[n % 3],
                     agent=СВОИ[n % 3], started_at=time.time() - n * 600, answered=True,
                     station="api", direction="входящий")
    db.employee_save({"last_name": "Петрова", "first_name": "Мария", "middle_name": "Ивановна",
                      "department": "Отдел продаж", "phone_ext": "201",
                      "source": "тест", "external_id": "201"})
    db.employee_save({"last_name": "Сидоров", "first_name": "Пётр",
                      "department": "Отдел продаж", "phone_ext": "202",
                      "source": "тест", "external_id": "202"})
    db.employee_save({"last_name": "Кузнецова", "first_name": "Анна",
                      "department": "Сервис", "phone_ext": "203",
                      "source": "тест", "external_id": "203"})
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    from fastapi.testclient import TestClient

    with TestClient(архив.app) as клиент:
        yield клиент


def _свод(c, **параметры):
    ответ = c.get("/api/content/llm", params={"period": "all", "part": "fast", **параметры})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def test_разрез_по_добавочным_сходится_со_списками(c):
    свод = _свод(c)
    по_номеру = {с["agent"]: с for с in свод["agents"]}
    assert set(СВОИ) <= set(по_номеру)
    for номер in СВОИ:
        строка = по_номеру[номер]
        assert строка["records"] == всего(c, свод["window"], [
            "status:completed", "no_control", f"agent:{номер}"])
        assert строка["analyzed"] == всего(c, свод["window"], [
            "status:completed", "no_control", "llm_ok", f"agent:{номер}"], base="llm")
        assert строка["records"] >= строка["analyzed"] > 0
    assert по_номеру["201"]["agent_name"] == "Петрова Мария Ивановна"
    assert по_номеру["201"]["agent_department"] == "Отдел продаж"
    assert по_номеру["202"]["agent_name"] == "Сидоров Пётр"
    # Самый занятый — первым.
    записей = [с["records"] for с in свод["agents"]]
    assert записей == sorted(записей, reverse=True)


def test_строки_разборов_подписаны(c):
    свод = _свод(c)
    свои = [р for р in свод["rows"] if р.get("agent") in СВОИ]
    assert свои, "ни одной строки с добавочным"
    for строка in свои:
        if строка["agent"] == "201":
            assert строка["agent_name"] == "Петрова Мария Ивановна"
    # У разбора без звонка добавочного нет — но поле есть, пустое.
    assert all("agent" in р for р in свод["rows"])


def test_раздел_сужается_до_добавочного(c):
    весь = _свод(c)
    строка = next(с for с in весь["agents"] if с["agent"] == "202")
    свой = _свод(c, agent="202")
    assert свой["agent"] == "202"
    assert свой["records"] == строка["records"]
    assert свой["analyzed"] == строка["analyzed"]
    assert sum(п["records"] for п in свой["reasons"]) <= строка["analyzed"]
    assert {р["agent"] for р in свой["rows"]} == {"202"}
    # Разрез по добавочным от отбора не зависит: из него выбирают.
    assert len(свой["agents"]) == len(весь["agents"])
    # Вторая часть — по тому же отбору.
    тяжёлая = c.get("/api/content/llm", params={"period": "all", "part": "heavy",
                                               "agent": "202"}).json()
    assert {д["agent"] for д in тяжёлая["action_items"]} <= {"202"}


def test_список_раскрытия_приносит_добавочный_и_имя(c):
    ответ = c.get("/api/drill", params={"base": "llm", "f": ["agent:201"], "limit": 50}).json()
    assert ответ["items"]
    for строка in ответ["items"]:
        assert строка["agent"] == "201" and строка["agent_name"] == "Петрова Мария Ивановна"
        assert строка["agent_department"] == "Отдел продаж"
    звонки = c.get("/api/drill", params={"base": "calls", "f": ["agent:203"]}).json()
    assert звонки["items"] and all(з["agent_name"] == "Кузнецова Анна" for з in звонки["items"])


def test_отбор_по_отделу_в_списке(c):
    свод = _свод(c)
    продажи = sum(с["records"] for с in свод["agents"] if с["agent"] in ("201", "202"))
    assert всего(c, свод["window"], ["status:completed", "no_control",
                                     "department:Отдел продаж"]) == продажи
    assert всего(c, None, ["department:Отдел продаж"], base="calls") == sum(
        1 for р in c.app.state.hub.db.query(
            "SELECT 1 FROM calls WHERE agent IN ('201','202')"))


def test_без_связи_по_номеру_имён_нет(архив, c):
    настройки = архив.app.state.hub.settings
    настройки.set("employees_match_ext", False)
    try:
        свод = _свод(c)
        assert all("agent_name" not in с for с in свод["agents"])
        ответ = c.get("/api/drill", params={"base": "llm", "f": ["agent:201"]}).json()
        assert ответ["items"] and all("agent_name" not in с for с in ответ["items"])
    finally:
        настройки.set("employees_match_ext", True)


def test_интерфейс_раскрывает_с_добавочным():
    from pathlib import Path

    код = (Path(__file__).resolve().parents[1] / "server/asrhub/web/app.js").read_text("utf-8")
    голос = код.split("RENDERERS.voice = {", 1)[1].split("\n};\n", 1)[0]
    # Отбор раздела уходит и в свод, и во вторую часть, и в каждый список.
    assert голос.count("&agent=${encodeURIComponent(this.добавочный)}") == 2
    assert "const свой = [...this.ЗАПИСИ, `agent:${ч.agent}`];" in голос
    assert "столбцы: ['when', 'record', 'agent', 'duration', 'outcome']" in голос
    assert "${добавочный(с)}" in голос and "${добавочный(а)}" in голос
