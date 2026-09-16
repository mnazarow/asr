"""Автодиагностика: свод по всем подсистемам, журнал проблем и маршруты.

Проверки написаны против конкретных способов сломаться, а не против
формы ответа. Диагностика — последняя надежда человека понять, что
происходит с сервером, и у неё два способа подвести:

* замолчать — упасть вместе с тем, что она проверяет, и отдать пятисотую
  вместо списка неисправностей;
* соврать — показать «всё хорошо» там, где выбранный движок не установлен,
  станция читает несуществующий журнал или настройки не применились.

Здесь закрыты оба, а заодно — разрастание ленты событий (одна и та же
проблема, записанная на каждый опрос панели) и утечка раскладки каталогов
ключу без прав администратора.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from asrhub import selfcheck
from asrhub.api import create_app
from asrhub.config import load
from asrhub.db import Database
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Опоры
# ---------------------------------------------------------------------------


@pytest.fixture()
def настройки(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Настройки с демонстрационным движком — без настоящих моделей."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    return load()


def поднять(настройки):
    """Приложение и его состояние. Очередь не запускаем: диагностика
    читает состояние, а не ждёт обработки заданий."""
    app = create_app(настройки, start_queue=False)
    return app


@pytest.fixture()
def сервер(настройки):
    """Пара «клиент, состояние приложения» — как в tests/test_api.py."""
    app = поднять(настройки)
    with TestClient(app) as клиент:
        yield клиент, app.state.hub


def проверка(свод: dict, раздел: str, ид: str) -> dict:
    """Одна проверка из раздела по идентификатору — или понятный отказ."""
    разделы = {к["id"]: к for к in свод["components"]}
    assert раздел in разделы, f"нет раздела «{раздел}»: {sorted(разделы)}"
    пункты = {п["id"]: п for п in разделы[раздел]["checks"]}
    assert ид in пункты, f"в разделе «{раздел}» нет проверки «{ид}»: {sorted(пункты)}"
    return пункты[ид]


def раздел(свод: dict, ид: str) -> dict:
    разделы = {к["id"]: к for к in свод["components"]}
    assert ид in разделы, f"нет раздела «{ид}»: {sorted(разделы)}"
    return разделы[ид]


# ---------------------------------------------------------------------------
# Свод целиком
# ---------------------------------------------------------------------------


def test_свод_собирается_на_пустом_сервере(сервер):
    """На только что поднятом сервере свод обязан собраться целиком.

    Пустой сервер — это состояние, в котором диагностику открывают чаще
    всего: сразу после установки, когда «ничего не работает и непонятно
    почему». Ни одна проверка не должна при этом требовать заданий,
    звонков или замеров, которых ещё нет.
    """
    _, state = сервер
    свод = selfcheck.состояние(state)

    assert свод["state"] in ("ok", "warn", "fail")
    assert set(свод["summary"]) == {"ok", "warn", "fail", "off"}
    assert sum(свод["summary"].values()) == len(свод["components"])

    ожидаемые = {"server", "disk", "db", "hardware", "queue", "engines",
                 "models", "telephony", "llm", "content", "monitoring",
                 "backup", "maintenance", "network"}
    assert {к["id"] for к in свод["components"]} == ожидаемые

    for компонент in свод["components"]:
        assert компонент["title"], компонент["id"]
        assert компонент["group"], компонент["id"]
        assert компонент["state"] in ("ok", "warn", "fail", "off"), компонент
        assert isinstance(компонент["checks"], list)
        for пункт in компонент["checks"]:
            assert пункт["id"] and пункт["title"], пункт
            assert пункт["state"] in ("ok", "warn", "fail", "off", "info"), пункт
            # Плохое состояние без подсказки — это «сломано, разбирайтесь
            # сами»: ровно то, ради чего диагностику и заводили.
            if пункт["state"] == "fail":
                assert пункт["hint"], f"{компонент['id']}/{пункт['id']} без подсказки"
            # И наоборот: подсказка у исправной проверки не покажется
            # нигде — список проблем собирается только из плохих, — а
            # значит, она написана впустую и вводит в заблуждение автора
            # следующей правки.
            if пункт["state"] in ("ok", "off"):
                assert not пункт["hint"], \
                    f"{компонент['id']}/{пункт['id']}: подсказка без повода"

    # Свод уходит в браузер как JSON: несериализуемое значение (Path,
    # sqlite3.Row, множество) обрушило бы весь раздел, а не одну строку.
    json.dumps(свод, ensure_ascii=False)


def test_связи_разделов_ведут_на_существующие_узлы(сервер):
    """По `depends_on` рисуется схема системы — висячих рёбер быть не должно."""
    _, state = сервер
    свод = selfcheck.состояние(state)
    известные = {к["id"] for к in свод["components"]}
    for компонент in свод["components"]:
        for опора in компонент["depends_on"]:
            assert опора in известные, f"{компонент['id']} зависит от «{опора}»"
            assert опора != компонент["id"], "раздел зависит сам от себя"


def test_проблемы_собраны_плоским_списком_и_отсортированы(сервер):
    """Список проблем читают сверху вниз: сначала поломки, потом остальное."""
    _, state = сервер
    свод = selfcheck.состояние(state)
    состояния = [п["state"] for п in свод["problems"]]
    assert set(состояния) <= {"fail", "warn"}
    assert состояния == sorted(состояния, key=lambda с: 0 if с == "fail" else 1)

    известные = {к["id"] for к in свод["components"]}
    for проблема in свод["problems"]:
        # Без `component` интерфейс не подсветит узел схемы, а именно ради
        # этого проблемы и вынесены отдельным списком.
        assert проблема["component"] in известные
        assert проблема["title"] and проблема["at"]

    # Каждая плохая проверка обязана попасть в список — иначе «проблем
    # нет» и «проблемы есть, но их не собрали» выглядят одинаково.
    ожидаемых = sum(1 for к in свод["components"] for п in к["checks"]
                    if п["state"] in ("warn", "fail"))
    assert len(свод["problems"]) == ожидаемых


def test_сбой_одной_проверки_не_роняет_остальные(сервер, monkeypatch):
    """Споткнувшийся раздел помечается fail, соседние считаются как обычно.

    Диагностика, падающая целиком из-за одной подсистемы, бесполезна
    ровно тогда, когда нужна: сломанная подсистема — это и есть повод её
    открыть.
    """
    _, state = сервер

    def падать(*_args, **_kwargs):
        raise RuntimeError("очередь не отвечает")

    monkeypatch.setattr(state.queue, "status", падать)
    свод = selfcheck.состояние(state)

    очередь = раздел(свод, "queue")
    assert очередь["state"] == "fail"
    assert "очередь не отвечает" in очередь["value"]
    авария = проверка(свод, "queue", "selfcheck")
    assert авария["state"] == "fail"
    assert "RuntimeError" in авария["value"]

    # Соседи посчитаны: у них есть проверки, а не пустой список-заглушка.
    for ид in ("server", "db", "disk", "engines"):
        assert раздел(свод, ид)["checks"], f"раздел «{ид}» остался без проверок"

    # И поломка видна в общем списке — иначе о ней узнают, только раскрыв
    # именно этот раздел.
    assert any(п["component"] == "queue" and п["state"] == "fail"
               for п in свод["problems"])


def test_свод_отвечает_на_нечитаемой_базе(сервер, monkeypatch):
    """База не отвечает — свод обязан собраться и сказать именно это.

    «Строк не прочитали» и «строк нет» — разные ответы: пустой архив и
    повреждённая база выглядели бы одинаково («заданий 0»), а лечатся
    совершенно по-разному.
    """
    _, state = сервер

    def падать(*_args, **_kwargs):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(state.db, "query_one", падать)
    свод = selfcheck.состояние(state)

    assert len(свод["components"]) == 14
    файл = проверка(свод, "db", "size")
    assert файл["state"] == "fail"
    assert "заданий ?" in файл["value"], файл["value"]
    assert файл["hint"]
    # Разделы, не зависящие от базы, посчитаны как обычно.
    assert раздел(свод, "disk")["checks"]
    assert раздел(свод, "engines")["state"] in ("ok", "fail")


def test_проблемы_конфигурации_видны(настройки):
    """Негодная строка в config.yaml не мешает серверу подняться — и потому
    видна только здесь.

    Сервер берёт умолчание и работает дальше: «сервер работает» и
    «настройки применились» — разные утверждения, и разницу между ними,
    кроме диагностики, показать некому. Раньше человек менял модель в
    файле, получал рабочий сервер и прежнюю модель в расшифровках.
    """
    настройки.problems.append("Параметр beam_size: ожидается целое, получено «пять»")
    app = поднять(настройки)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)

    пункт = проверка(свод, "server", "config")
    assert пункт["state"] == "fail"
    assert "beam_size" in пункт["value"]
    assert пункт["hint"]
    assert раздел(свод, "server")["state"] == "fail"
    assert any(п["component"] == "server" and п["id"] == "config"
               for п in свод["problems"])


def test_выбранный_движок_и_модель_разобраны(сервер):
    """Выбранный движок и модель по умолчанию должны быть названы поимённо."""
    _, state = сервер
    свод = selfcheck.состояние(state)
    движок = проверка(свод, "engines", "selected")
    assert движок["state"] == "ok", движок
    assert движок["metrics"]["engine"] == "demo"

    # У встроенного движка весов нет вовсе, и «веса не скачаны» на нём —
    # ложная тревога на исправном сервере.
    веса = проверка(свод, "models", "weights")
    assert веса["state"] == "ok"
    assert веса["metrics"]["downloaded"] is True


def test_несуществующий_движок_называет_команду_установки(настройки):
    """Выбран движок, которого нет, — каждое задание будет падать."""
    настройки.values["engine"] = "vosk"
    app = поднять(настройки)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)
    пункт = проверка(свод, "engines", "selected")
    if пункт["state"] == "ok":               # окружение, где vosk установлен
        pytest.skip("движок vosk установлен в этом окружении")
    assert пункт["state"] == "fail"
    assert "install-engine vosk" in пункт["hint"]


# ---------------------------------------------------------------------------
# Телефония
# ---------------------------------------------------------------------------


def test_станция_без_журнала_даёт_fail(настройки, tmp_path: Path):
    """Станция читает журнал, которого нет, — и молчит об этом.

    «Заход прошёл, новых звонков ноль» выглядит как исправная работа:
    забор честно отчитывается нулём, в журнале сервера ни строчки. Пока
    это не назовут поломкой, записи не приезжают неделями, и замечают это
    по отсутствию разговоров в отчёте.
    """
    журнал = tmp_path / "Master.csv"          # намеренно не создаём
    настройки.values["telephony_enabled"] = True
    настройки.values["telephony_stations"] = [{
        "id": "pbx", "name": "АТС филиала", "enabled": True,
        "source": "cdr_csv", "cdr_file": str(журнал),
        "recordings_dir": str(tmp_path / "monitor"), "poll_s": 3600,
    }]
    app = поднять(настройки)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)

    пункт = проверка(свод, "telephony", "station:pbx")
    assert пункт["state"] == "fail", пункт
    assert "источника нет" in пункт["value"]
    assert пункт["hint"]
    assert раздел(свод, "telephony")["state"] == "fail"
    assert any(п["component"] == "telephony" and п["state"] == "fail"
               for п in свод["problems"])


def test_станция_с_журналом_не_ругается(настройки, tmp_path: Path):
    """Тот же случай, но файл на месте: поломки быть не должно.

    Проверка-близнец к предыдущей: без неё «fail всегда» прошло бы за
    работающую диагностику.
    """
    журнал = tmp_path / "Master.csv"
    журнал.write_text("", encoding="utf-8")
    (tmp_path / "monitor").mkdir()
    настройки.values["telephony_enabled"] = True
    настройки.values["telephony_stations"] = [{
        "id": "pbx", "name": "АТС", "enabled": True, "source": "cdr_csv",
        "cdr_file": str(журнал), "recordings_dir": str(tmp_path / "monitor"),
        "poll_s": 3600,
    }]
    app = поднять(настройки)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)
    пункт = проверка(свод, "telephony", "station:pbx")
    assert пункт["state"] != "fail", пункт


def test_выключенная_телефония_не_беспокоит(сервер):
    """Выключенное намеренно — не поломка и в список проблем не идёт."""
    _, state = сервер
    свод = selfcheck.состояние(state)
    assert раздел(свод, "telephony")["state"] == "off"
    assert not any(п["component"] == "telephony" for п in свод["problems"])


def test_молчащий_агент_замечен(сервер):
    """Агент не выходил на связь больше суток — записи не приезжают.

    Связь всегда начинает агент: сервер до станции за NAT не дотянется.
    Молчание агента поэтому неотличимо от «на линии тихо» — и увидеть его
    больше негде.
    """
    _, state = сервер
    state.db.agent_save("pbx-1", {"name": "Агент филиала", "station": "pbx"})
    state.db.execute("UPDATE agents SET last_seen=? WHERE id=?",
                     (time.time() - 3 * 86400, "pbx-1"))
    свод = selfcheck.состояние(state)
    пункт = проверка(свод, "telephony", "agents")
    assert пункт["state"] == "warn"
    assert "pbx-1" in пункт["value"]
    assert пункт["metrics"]["silent"] == 1


# ---------------------------------------------------------------------------
# Журнал проблем
# ---------------------------------------------------------------------------


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "selfcheck.db")


def свод_из(*проблемы) -> dict:
    """Свод с заданными проблемами — без поднятия сервера."""
    return {"at": time.time(), "state": "warn", "problems": [
        {"id": ид, "component": раздел_, "component_title": раздел_.title(),
         "title": название, "state": состояние, "value": "так вышло",
         "hint": "почините", "at": time.time()}
        for ид, раздел_, название, состояние in проблемы]}


def события(db: Database) -> list[str]:
    return [с["message"] for с in db.get_events(limit=500)
            if с["kind"] == "selfcheck"]


def test_записать_проблемы_не_пишет_одно_и_то_же_дважды(tmp_path: Path):
    """Панель опрашивает свод постоянно — запись каждой проблемы на каждый
    опрос превратила бы ленту событий в один и тот же текст десять тысяч
    раз за сутки, а таблицу событий — в самую большую в базе.
    """
    db = база(tmp_path)
    свод = свод_из(("free", "disk", "Свободное место", "warn"),
                   ("selected", "engines", "Выбранный движок", "fail"))

    первый = selfcheck.записать_проблемы(db, свод)
    assert len(первый["appeared"]) == 2
    assert первый["resolved"] == []
    assert len(события(db)) == 2

    for _ in range(5):
        повтор = selfcheck.записать_проблемы(db, свод)
        assert повтор["appeared"] == []
        assert повтор["resolved"] == []
    assert len(события(db)) == 2, "проблема записана повторно"


def test_записать_проблемы_отмечает_исчезновение(tmp_path: Path):
    """Ушедшую проблему нужно назвать: без этого «когда это кончилось»
    ответа не имеет, а лента проблем копит вечные записи."""
    db = база(tmp_path)
    было = свод_из(("free", "disk", "Свободное место", "warn"),
                   ("selected", "engines", "Выбранный движок", "fail"))
    selfcheck.записать_проблемы(db, было)

    стало = свод_из(("selected", "engines", "Выбранный движок", "fail"))
    итог = selfcheck.записать_проблемы(db, стало)
    assert итог["appeared"] == []
    assert len(итог["resolved"]) == 1
    тексты = события(db)
    assert any(т.startswith("Устранено:") and "Свободное место" in т for т in тексты)
    assert len(тексты) == 3

    # И больше об этом ни слова: устранённая проблема ушла из отпечатка.
    ещё = selfcheck.записать_проблемы(db, стало)
    assert ещё["resolved"] == []
    assert len(события(db)) == 3


def test_переход_предупреждения_в_поломку_это_новость(tmp_path: Path):
    """warn → fail — отдельное событие, а не «та же самая проблема».

    Состояние входит в отпечаток намеренно: место, которого «мало», и
    место, которого «нет», лечатся с разной срочностью, и молчать о
    переходе нельзя.
    """
    db = база(tmp_path)
    selfcheck.записать_проблемы(db, свод_из(
        ("free", "disk", "Свободное место", "warn")))
    итог = selfcheck.записать_проблемы(db, свод_из(
        ("free", "disk", "Свободное место", "fail")))
    assert len(итог["appeared"]) == 1
    assert len(итог["resolved"]) == 1
    тексты = события(db)
    assert any(т.startswith("Неисправность:") for т in тексты)
    assert any(т.startswith("Предупреждение:") for т in тексты)


def test_отпечаток_не_разрастается(tmp_path: Path):
    """В `kv` лежит ровно текущий набор проблем, а не история.

    Отпечаток, копящий всё, что когда-либо случалось, однажды станет
    строкой на мегабайты, которую читают на каждый опрос панели.
    """
    db = база(tmp_path)
    for шаг in range(30):
        selfcheck.записать_проблемы(db, свод_из(
            (f"пункт-{шаг}", "disk", f"Проверка {шаг}", "warn")))
    отпечаток = db.get_kv(selfcheck.КЛЮЧ_ОТПЕЧАТКА)
    assert isinstance(отпечаток, dict)
    assert len(отпечаток) == 1, f"в отпечатке лишнее: {sorted(отпечаток)}"


def test_запись_проблем_переживает_сломанную_базу(tmp_path: Path):
    """Диагностика не имеет права падать из-за того, что не смогла
    записать о себе в журнал."""
    class Немая:
        def get_kv(self, *_args, **_kwargs):
            raise RuntimeError("база не отвечает")

        def set_kv(self, *_args, **_kwargs):
            raise RuntimeError("база не отвечает")

        def add_event(self, *_args, **_kwargs):
            raise RuntimeError("база не отвечает")

    итог = selfcheck.записать_проблемы(Немая(), свод_из(
        ("free", "disk", "Свободное место", "warn")))
    assert len(итог["appeared"]) == 1


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------


@pytest.fixture()
def с_ключами(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Приложение с включённой аутентификацией — как в tests/test_security.py."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    settings.api_keys["ah_user"] = {"name": "Алиса", "role": "user", "enabled": True}
    return settings


def test_маршрут_свода_отвечает_и_прячет_пути(с_ключами):
    """Раскладка каталогов — разведка перед атакой, а не сведения для работы.

    Соседние GET /api/system и GET /api/settings прячут `paths` от
    неадминистратора; диагностика, отдающая путь к базе, каталог копий и
    адрес приёмника метрик любому ключу, сводила бы ту защиту на нет —
    достаточно было бы спросить её вместо них.
    """
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        каталог = str(с_ключами.paths.data)

        админ = c.get("/api/system/selfcheck", headers={"X-API-Key": "ah_admin"})
        assert админ.status_code == 200, админ.text
        свод = админ.json()
        assert len(свод["components"]) == 14
        assert каталог in админ.text, "администратору пути нужны"

        свой = c.get("/api/system/selfcheck", headers={"X-API-Key": "ah_user"})
        assert свой.status_code == 200, свой.text
        assert каталог not in свой.text, "путь каталога данных ушёл обычному ключу"
        # Сами неисправности при этом видны: скрывать от оператора, что
        # сервер нездоров, незачем.
        assert свой.json()["components"], "неадминистратору отдали пустой свод"
        assert len(свой.json()["components"]) == len(свод["components"])
        for компонент in свой.json()["components"]:
            assert "path" not in компонент["metrics"]
            for пункт in компонент["checks"]:
                assert "path" not in пункт["metrics"]
                assert "url" not in пункт["metrics"]

        без_ключа = c.get("/api/system/selfcheck")
        assert без_ключа.status_code == 401


def test_маршрут_свода_пишет_в_журнал_только_изменения(с_ключами):
    """Опрос панели не должен наполнять ленту событий одним и тем же."""
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        первый = c.get("/api/system/selfcheck",
                       headers={"X-API-Key": "ah_admin"}).json()
        assert первый["journal"]["appeared"], "первый заход ничего не записал"
        второй = c.get("/api/system/selfcheck",
                       headers={"X-API-Key": "ah_admin"}).json()
        assert второй["journal"]["appeared"] == []
        assert второй["journal"]["resolved"] == []


def test_маршрут_проблем_собирает_всё_в_один_список(с_ключами):
    """Журнал проблем: и то, что не так сейчас, и то, что было."""
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        state = app.state.hub
        state.db.add_event(None, "alert_firing", "Свободного места меньше 5 ГБ")
        state.db.add_event(None, "created", "Задание создано: запись.wav")

        ответ = c.get("/api/system/problems", headers={"X-API-Key": "ah_admin"})
        assert ответ.status_code == 200, ответ.text
        данные = ответ.json()
        assert данные["items"], "журнал проблем пуст на нездоровом сервере"
        for запись in данные["items"]:
            assert запись["level"] in ("error", "warning")
            assert запись["source"] and запись["what"]
            assert "at" in запись and "hint" in запись

        тексты = [з["what"] for з in данные["items"]]
        assert any("меньше 5 ГБ" in т for т in тексты), "тревога не попала в журнал"
        assert not any("Задание создано" in т for т in тексты), \
            "в журнал проблем затесалось обычное событие"

        # Свежее сверху: журнал читают с начала.
        времена = [float(з["at"] or 0) for з in данные["items"]]
        assert времена == sorted(времена, reverse=True)


def test_маршрут_проблем_прячет_чужое(с_ключами):
    """Обычный ключ видит свои задания и события своих заданий — как везде.

    Иначе «журнал проблем» стал бы обходным путём к чужим именам файлов, а
    в колл-центре имя файла — это номер клиента.
    """
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        state = app.state.hub
        state.db.add_event(None, "warning", "Секретное системное предупреждение")

        свой = c.get("/api/system/problems", headers={"X-API-Key": "ah_user"})
        assert свой.status_code == 200, свой.text
        assert "Секретное системное" not in свой.text
        assert str(с_ключами.paths.data) not in свой.text

        админ = c.get("/api/system/problems", headers={"X-API-Key": "ah_admin"})
        assert "Секретное системное" in админ.text


def test_маршрут_проблем_учитывает_период(с_ключами):
    """`since` отрезает старое: иначе журнал за девяносто дней приходит
    целиком на каждый запрос."""
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        state = app.state.hub
        state.db.add_event(None, "alert_firing", "Давняя тревога")
        state.db.execute("UPDATE events SET ts=? WHERE message=?",
                         (time.time() - 30 * 86400, "Давняя тревога"))

        недавно = c.get("/api/system/problems?since=0",
                        headers={"X-API-Key": "ah_admin"})
        assert "Давняя тревога" not in недавно.text

        давно = c.get(f"/api/system/problems?since={time.time() - 60 * 86400:.0f}",
                      headers={"X-API-Key": "ah_admin"})
        assert "Давняя тревога" in давно.text


# ---------------------------------------------------------------------------
# Глубокая проверка
# ---------------------------------------------------------------------------


def test_глубокая_проверка_не_падает_без_сети(настройки):
    """`deep=1` ходит в сеть — и обязан пережить её отсутствие.

    Адреса уведомлений, сводки и сервера языковой модели на закрытом
    контуре недоступны по определению. Проверка, падающая на этом,
    отменяет разом все остальные — включая те, ради которых её и звали.
    """
    настройки.values["webhook_url"] = "http://127.0.0.1:9/hook"
    настройки.values["digest_url"] = "https://127.0.0.1:9/digest"
    настройки.values["llm_backend"] = "ollama"
    настройки.values["llm_url"] = "http://127.0.0.1:9"
    настройки.values["llm_model"] = "qwen2.5:7b"

    app = поднять(настройки)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub, глубоко=True)

    assert свод["deep"] is True
    assert len(свод["components"]) == 14
    json.dumps(свод, ensure_ascii=False)

    вебхук = проверка(свод, "network", "webhook_url")
    assert вебхук["state"] == "warn"
    assert вебхук["metrics"]["reachable"] is False

    сервер_модели = проверка(свод, "llm", "server")
    assert сервер_модели["state"] == "fail"
    assert сервер_модели["hint"]

    # Целостность базы проверяется только в глубоком заходе — иначе
    # полное чтение базы шло бы на каждый опрос панели.
    целостность = проверка(свод, "db", "integrity")
    assert целостность["state"] == "ok"


def test_обычный_заход_не_ходит_в_сеть(настройки, monkeypatch):
    """Обычный опрос не должен трогать ни сеть, ни целостность базы.

    Панель спрашивает свод каждые несколько секунд. Один сетевой вызов на
    такой частоте — это постоянный стук в чужой приёмник, а полное чтение
    базы — постоянная нагрузка на диск.
    """
    настройки.values["webhook_url"] = "http://127.0.0.1:9/hook"
    настройки.values["llm_backend"] = "ollama"
    настройки.values["llm_url"] = "http://127.0.0.1:9"

    app = поднять(настройки)
    with TestClient(app):
        state = app.state.hub

        def нельзя(*_args, **_kwargs):
            raise AssertionError("обычный заход полез в сеть")

        monkeypatch.setattr(selfcheck, "_достучаться", нельзя)
        monkeypatch.setattr(state.llm, "probe", нельзя)
        свод = selfcheck.состояние(state)

    assert проверка(свод, "network", "webhook_url")["state"] == "ok"
    разделы = {к["id"]: к for к in свод["components"]}
    assert "integrity" not in {п["id"] for п in разделы["db"]["checks"]}
    assert "server" not in {п["id"] for п in разделы["llm"]["checks"]}


def test_маршрут_глубокой_проверки(с_ключами):
    """`deep=1` доходит до маршрута и не роняет его."""
    app = create_app(с_ключами, start_queue=False)
    with TestClient(app) as c:
        ответ = c.get("/api/system/selfcheck?deep=1",
                      headers={"X-API-Key": "ah_admin"})
        assert ответ.status_code == 200, ответ.text
        assert ответ.json()["deep"] is True
        # Обход каталогов — только в глубоком заходе.
        разделы = {к["id"]: к for к in ответ.json()["components"]}
        assert "usage" in {п["id"] for п in разделы["disk"]["checks"]}
