"""Заход 49: раскрытие числа отчёта в список записей — `GET /api/drill`.

Разделы аналитики показывали числа, а «какие именно записи» приходилось
добывать в «Результатах» отбором руками — если такой отбор там был. Теперь
любое число раскрывается списком со ссылкой на каждую запись. Здесь —
серверная часть: что список совпадает с числом, не выдаёт чужого, листается
без пропусков и повторов и не может повесить сервер.
"""
from __future__ import annotations

import pytest
from asrhub import drill
from asrhub.db import QueryTimeout
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill") / "data")
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _значение(отбор: drill.Отбор, имя: str) -> str:
    return {"признак": имя, "текст": f"{имя}:x", "целое": f"{имя}:{отбор.границы[0]}",
            "диапазон": f"{имя}:0..10"}[отбор.вид]


# ---------------------------------------------------------------------------
# Перечень отборов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("имя", sorted(drill.ОТБОРЫ))
def test_каждый_отбор_заданий_исполняется(c, имя):
    """Выражения собраны руками, и опечатка в одном из сотни — это 500 на
    том единственном числе, которое им раскрывается."""
    ответ = c.get("/api/drill", params={"f": [_значение(drill.ОТБОРЫ[имя], имя)], "limit": 3})
    assert ответ.status_code == 200, ответ.text


@pytest.mark.parametrize("имя", sorted(drill.ОТБОРЫ_ЗВОНКОВ))
def test_каждый_отбор_звонков_исполняется(c, имя):
    ответ = c.get("/api/drill", params={
        "base": "calls", "f": [_значение(drill.ОТБОРЫ_ЗВОНКОВ[имя], имя)], "limit": 3})
    assert ответ.status_code == 200, ответ.text


@pytest.mark.parametrize("имя", sorted(drill.ЧИСЛА))
def test_каждая_величина_годится_в_отбор_и_в_порядок(c, имя):
    for отбор in (f"has:{имя}", f"nz:{имя}", f"{имя}:0..1000000"):
        for порядок in (f"{имя}:asc", f"{имя}:desc"):
            ответ = c.get("/api/drill", params={"f": [отбор], "sort": порядок, "limit": 3})
            assert ответ.status_code == 200, (отбор, порядок, ответ.text)


def test_перечень_отдаётся_и_сходится_с_кодом(c):
    перечень = c.get("/api/drill/filters").json()
    assert {о["name"] for о in перечень["filters"]} == set(drill.ОТБОРЫ)
    assert {о["name"] for о in перечень["call_filters"]} == set(drill.ОТБОРЫ_ЗВОНКОВ)
    assert перечень["values"] == sorted(drill.ЧИСЛА)
    assert all(о["description"] for о in перечень["filters"] + перечень["call_filters"])


@pytest.mark.parametrize("параметры", [
    {"f": ["нет_такого"]}, {"f": ["hour:24"]}, {"f": ["hour:x"]}, {"f": ["duration:.."]},
    {"f": ["status:готово"]}, {"f": ["has:нет"]}, {"sort": "нет"}, {"sort": "duration:вбок"},
    {"cursor": "мусор"}, {"cursor": "W10"}, {"base": "calls", "f": ["model:x"]},
    {"base": "calls", "sort": "rtf"}, {"f": ["x" * 400]},
])
def test_неверный_запрос_получает_объяснение_а_не_500(c, параметры):
    ответ = c.get("/api/drill", params=параметры)
    assert ответ.status_code == 400, ответ.text
    assert ответ.json()["code"] == "config_error"
    assert ответ.json()["hint"]


def test_имя_отбора_не_попадает_в_запрос(c):
    """Выражение берётся из перечня, значение уходит параметром: ни то, ни
    другое не склеивается с текстом запроса."""
    до = всего(c)
    ответ = c.get("/api/drill", params={"f": ["model:x' OR 1=1 --"]})
    assert ответ.status_code == 200 and ответ.json()["total"] == 0
    assert c.get("/api/drill", params={"f": ["model' OR 1=1 --:x"]}).status_code == 400
    assert всего(c) == до


# ---------------------------------------------------------------------------
# Страницы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("порядок", ["", "time:asc", "duration:desc", "duration:asc",
                                     "confidence:desc", "retries:desc"])
def test_страницы_идут_без_пропусков_и_повторов(c, архив, порядок):
    """У величины много одинаковых значений (повторов 0 у большинства):
    продолжение «после такой-то записи» обязано различать их по номеру."""
    по_одной = все_строки(c, sort=порядок, limit=7)
    разом = все_строки(c, sort=порядок, limit=200)
    assert [с["id"] for с in по_одной] == [с["id"] for с in разом]
    assert len({с["id"] for с in по_одной}) == len(по_одной)
    if порядок.split(":")[0] in ("", "time"):
        assert len(по_одной) == len(архив.данные["jobs"])
        времена = [с["created_at"] for с in по_одной]
        assert времена == sorted(времена, reverse=not порядок.endswith("asc"))
    else:
        значения = [с["value"] for с in по_одной]
        assert None not in значения
        assert значения == sorted(значения, reverse=порядок.endswith("desc"))


def test_звонки_листаются_так_же(c, архив):
    по_одной = все_строки(c, base="calls", limit=7)
    assert len(по_одной) == len(архив.данные["calls"])
    assert len({с["uniqueid"] for с in по_одной}) == len(по_одной)
    по_длине = все_строки(c, base="calls", sort="billsec:desc", limit=9)
    assert [с["billsec"] for с in по_длине] == sorted(
        (с["billsec"] for с in по_длине), reverse=True)
    assert isinstance(по_одной[0]["answered"], bool)


def test_счёт_только_на_первой_странице_и_с_потолком(c, архив, monkeypatch):
    первая = c.get("/api/drill", params={"limit": 10}).json()
    assert первая["total"] == len(архив.данные["jobs"]) and not первая["total_capped"]
    вторая = c.get("/api/drill", params={"limit": 10, "cursor": первая["next"]}).json()
    assert вторая["total"] is None
    # Точное «всего» на миллионах стоит полного прохода; дальше потолка не считаем.
    monkeypatch.setattr(drill, "ПРЕДЕЛ_СЧЁТА", 25)
    срез = c.get("/api/drill", params={"limit": 10}).json()
    assert срез["total"] == 25 and срез["total_capped"] is True


def test_окно_нижняя_граница_входит_верхняя_нет(c, архив):
    строки = все_строки(c, limit=200)
    времена = sorted(с["created_at"] for с in строки)
    от, до = времена[10], времена[20]
    assert всего(c, {"since": от, "until": до}) == 10
    assert всего(c, {"since": от}) == len(времена) - 10
    assert всего(c, {"until": до}) == 20
    # Ноль — законная граница: «с начала времён» не значит «без границы».
    assert всего(c, {"since": 0, "until": 0}) == 0


def test_расшифровка_в_список_не_попадает(c):
    """Список — лёгкие поля. Текст разговора в нём не нужен и на тысяче
    строк весит мегабайты."""
    строка = c.get("/api/drill", params={"base": "llm", "f": ["scored"], "limit": 1}).json()
    assert строка["items"], строка
    поля = set(строка["items"][0])
    assert {"id", "filename", "created_at", "status", "sentiment", "reason"} <= поля
    assert not поля & {"text", "segments", "words", "params", "reference_text"}


def test_запрос_со_сроком_прерывается(архив):
    тяжёлый = ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r "
               "WHERE n < 50000000) SELECT COUNT(*) AS n FROM r")
    with pytest.raises(QueryTimeout):
        архив.db.query_bounded(тяжёлый, seconds=0.05)
    # Соединение после прерывания живо, и срок с него снят.
    assert архив.db.query_bounded("SELECT 1 AS n")[0]["n"] == 1
    assert архив.db.query_one("SELECT COUNT(*) AS n FROM jobs")["n"] > 0


def test_не_уложился_в_срок_значит_совет_а_не_ошибка(c, архив, monkeypatch):
    def медленно(*args, **kwargs):
        raise QueryTimeout("долго")

    monkeypatch.setattr(архив.db, "query_bounded", медленно)
    ответ = c.get("/api/drill", params={"f": ["negative"]})
    assert ответ.status_code == 200
    assert ответ.json()["timed_out"] is True and ответ.json()["items"] == []


def test_счёт_не_уложился_а_страница_есть(c, архив, monkeypatch):
    настоящий = архив.db.query_bounded

    def только_страница(sql, params=(), **kwargs):
        if sql.startswith("SELECT COUNT"):
            raise QueryTimeout("долго")
        return настоящий(sql, params, **kwargs)

    monkeypatch.setattr(архив.db, "query_bounded", только_страница)
    ответ = c.get("/api/drill", params={"limit": 5}).json()
    assert len(ответ["items"]) == 5 and ответ["total"] is None and not ответ["timed_out"]


# ---------------------------------------------------------------------------
# Число отчёта = длина списка
# ---------------------------------------------------------------------------


def test_список_сходится_с_числами_аналитики(c):
    отчёт = c.get("/api/analytics", params={"period": "week"}).json()
    окно = отчёт["window"]
    assert окно["since"] and окно["until"] > окно["since"]
    задания = отчёт["overview"]["jobs"]
    assert всего(c, окно) == задания["total"]
    assert всего(c, окно, ["status:completed"]) == задания["completed"]
    assert всего(c, окно, ["status:failed"]) == задания["failed"]
    assert всего(c, окно, ["status:active"]) == задания["in_progress"]
    assert всего(c, окно, ["status:cancelled"]) == задания["cancelled"]
    assert задания["failed"] and задания["cancelled"] and задания["in_progress"]
    for модель in отчёт["models"]:
        имя = "" if модель["model"] == "—" else модель["model"]
        assert всего(c, окно, [f"model:{имя}"]) == модель["jobs"], модель["model"]


def test_окно_всего_времени_без_нижней_границы(c, архив):
    отчёт = c.get("/api/analytics", params={"period": "all"}).json()
    assert отчёт["window"]["since"] is None
    assert всего(c, отчёт["window"]) == len(архив.данные["jobs"])


# ---------------------------------------------------------------------------
# Чужого не показывать
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def закрытый(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-auth") / "data", auth=True, ключи={
        "ah_admin": {"name": "админ", "role": "admin"},
        "ah_anna": {"name": "анна", "role": "user"},
        "ah_boris": {"name": "борис", "role": "user", "group": "продажи"},
        "ah_vera": {"name": "вера", "role": "user", "group": "продажи"},
        "ah_mask": {"name": "анна", "role": "user", "mask_pii": True},
    })
    with TestClient(хранилище.app) as клиент:
        yield клиент, хранилище
    хранилище.закрыть()


def _владельцы(клиент, ключ, base="jobs"):
    строки = []
    указатель = ""
    while True:
        тело = клиент.get("/api/drill", params={"base": base, "limit": 200, "cursor": указатель},
                          headers={"X-API-Key": ключ}).json()
        строки += тело["items"]
        указатель = тело["next"] or ""
        if not указатель:
            return строки


def test_без_ключа_список_закрыт(закрытый):
    клиент, _ = закрытый
    assert клиент.get("/api/drill", headers={"X-API-Key": ""}).status_code == 401
    assert клиент.get("/api/drill/filters", headers={"X-API-Key": ""}).status_code == 401


def test_ключ_видит_только_своё_а_группа_своё_общее(закрытый):
    """Число из своего отчёта не должно раскрываться в чужие разговоры."""
    клиент, хранилище = закрытый
    все = _владельцы(клиент, "ah_admin")
    assert len(все) == len(хранилище.данные["jobs"])
    assert {с["owner"] for с in все} >= {"анна", "борис", "вера"}
    assert {с["owner"] for с in _владельцы(клиент, "ah_anna")} == {"анна"}
    assert {с["owner"] for с in _владельцы(клиент, "ah_boris")} == {"борис", "вера"}
    # Отбор по чужому владельцу своего не расширяет: условия складываются.
    чужое = клиент.get("/api/drill", params={"f": ["owner:борис"]},
                       headers={"X-API-Key": "ah_anna"}).json()
    assert чужое["total"] == 0 and чужое["items"] == []


def test_звонки_тоже_режутся_по_владельцу(закрытый):
    клиент, хранилище = закрытый
    assert len(_владельцы(клиент, "ah_admin", "calls")) == len(хранилище.данные["calls"])
    свои = _владельцы(клиент, "ah_anna", "calls")
    чужие = {р["uniqueid"] for р in хранилище.db.query(
        "SELECT uniqueid FROM calls WHERE COALESCE(owner,'') <> 'анна'")}
    assert свои and not {с["uniqueid"] for с in свои} & чужие


def test_ключу_без_персональных_данных_номера_не_отдаются(закрытый):
    клиент, хранилище = закрытый
    хранилище.db.execute("UPDATE jobs SET filename='+7 916 555-12-34.wav' WHERE id='dr0000'")
    строки = клиент.get("/api/drill", params={"limit": 200},
                        headers={"X-API-Key": "ah_mask"}).json()["items"]
    имена = " ".join(с["filename"] for с in строки)
    assert "555-12-34" not in имена and "dr0000" in {с["id"] for с in строки}
    звонки = клиент.get("/api/drill", params={"base": "calls", "limit": 200},
                        headers={"X-API-Key": "ah_mask"}).json()["items"]
    assert звонки and not any("+7916555" in с["src"] for с in звонки)
    # Обычному ключу того же владельца номера видны: маскирование — свойство ключа.
    открыто = клиент.get("/api/drill", params={"base": "calls", "limit": 200},
                         headers={"X-API-Key": "ah_anna"}).json()["items"]
    assert any("+7916555" in с["src"] for с in открыто)
