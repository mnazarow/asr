"""Тридцатый заход: несколько АТС, показатели разбора, аналитика по сотрудникам.

Здесь же — проверки на две ошибки, о которых сообщил владелец сервера:
установщик языковой модели, падавший на sudo в контейнере с «no new
privileges», и раздел «Сервер», рушившийся на сведениях, которых не
получает неадминистратор.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.errors import ConfigError
from asrhub.telephony.stations import _станция_из


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


# ---------------------------------------------------------------------------
# Установщик языковой модели без root
# ---------------------------------------------------------------------------

def test_sudo_c_флагом_no_new_privileges_считается_недоступным(monkeypatch):
    """Флаг запрещает процессу получить больше прав — это не поломка настройки.

    Раньше мы просто звали `sudo -n` и показывали человеку его обломки:
    «The "no new privileges" flag is set» — сообщение, по которому непонятно
    ни что случилось, ни что делать.
    """
    import subprocess

    from asrhub.llm import provision

    monkeypatch.setattr(provision.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: "/usr/bin/sudo")

    def отказ(*_а, **_к):
        return subprocess.CompletedProcess(
            [], 1, "", 'sudo: The "no new privileges" flag is set, which prevents '
                       "sudo from running as root.")

    monkeypatch.setattr(provision.subprocess, "run", отказ)
    можно, почему = provision.sudo_работает()
    assert можно is False
    assert "no new privileges" in почему, почему
    assert "sudo" not in почему.split("«")[0].lower() or "флаг" in почему


def test_нет_sudo_вовсе_тоже_не_повод_сдаваться(monkeypatch):
    from asrhub.llm import provision

    monkeypatch.setattr(provision.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: None)
    можно, почему = provision.sudo_работает()
    assert можно is False and "sudo" in почему


def test_от_root_sudo_не_нужен(monkeypatch):
    from asrhub.llm import provision

    monkeypatch.setattr(provision.os, "geteuid", lambda: 0, raising=False)
    assert provision.sudo_работает() == (True, "уже root")


def test_без_root_установка_идёт_в_каталог_данных(monkeypatch, tmp_path: Path):
    """Ollama ставится и без root — распаковкой официального архива.

    Раньше на этом месте был отказ с подсказкой «запустите от root», а
    запустить от root в контейнере нельзя по устройству среды.
    """
    import types

    from asrhub.llm import provision

    monkeypatch.setattr(provision, "sudo_работает",
                        lambda: (False, "флаг «no new privileges»"))
    monkeypatch.setattr(provision.shutil, "which",
                        lambda имя: None if имя == "ollama" else f"/usr/bin/{имя}")

    записи: list = []
    установщик = provision.Установщик.__new__(provision.Установщик)
    установщик.settings = types.SimpleNamespace(
        paths=types.SimpleNamespace(data=str(tmp_path)))
    установщик._записать = записи.append
    установщик._шаг = lambda *а, **к: записи.append(("шаг", *а))
    выбрано: list = []
    установщик._в_свой_каталог = lambda причина="": выбрано.append(причина)

    установщик._установка(True)
    assert выбрано == [""], "не ушли на установку без root"
    assert any("no new privileges" in str(з) for з in записи), записи


def test_архив_есть_для_обеих_архитектур():
    from asrhub.llm.provision import АРХИВЫ

    for машина in ("x86_64", "amd64", "aarch64", "arm64"):
        assert машина in АРХИВЫ
        assert АРХИВЫ[машина].startswith("https://")


# ---------------------------------------------------------------------------
# Раздел «Сервер»
# ---------------------------------------------------------------------------

def test_раздел_сервер_не_падает_без_сведений_о_базе():
    """`/api/system` отдаёт `database` и `paths` только администратору.

    Раздел читал их без проверки и падал целиком с «can't access property
    size_mb, sys.database is undefined»: неадминистратор видел вместо
    страницы пустоту, а в консоли ошибку.
    """
    источник = (Path(__file__).resolve().parent.parent
                / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    начало = источник.index("RENDERERS.system = {")
    конец = источник.index("RENDERERS.", начало + 10)
    раздел = источник[начало:конец]
    assert "sys.database." not in раздел, "прямое обращение к sys.database вернулось"
    assert "const база = sys.database;" in раздел
    for обращение in ("база.jobs", "база.segments", "база.size_mb"):
        assert обращение in раздел, обращение


# ---------------------------------------------------------------------------
# Несколько АТС
# ---------------------------------------------------------------------------

def _строка(uid: str, src: str, dst: str, *, context: str = "from-trunk",
            начало: float | None = None, duration: int = 300) -> str:
    import time as _t
    начало = _t.time() - 600 if начало is None else начало
    н = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(начало))
    к = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(начало + duration))
    return (f'"","{src}","{dst}","{context}","<{src}>","SIP/tr","SIP/{dst}",'
            f'"Dial","SIP/{dst}","{н}","{н}","{к}",{duration},{duration - 10},'
            f'"ANSWERED","DOCUMENTATION","{uid}",""\n')


class _Очередь:
    def __init__(self):
        self.вызовы: list[dict] = []

    def submit(self, **kwargs):
        self.вызовы.append(kwargs)
        return {"id": f"job_{len(self.вызовы)}"}


class _Настройки(dict):
    def get(self, ключ, по_умолчанию=None):                  # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def merged(self, поверх):
        значения = dict(self)
        значения.update(поверх or {})
        return значения


def _поля(каталог: Path, имя: str, **поверх) -> dict:
    поля = {
        "name": имя, "enabled": True, "source": "cdr_csv",
        "cdr_file": str(каталог / "Master.csv"),
        "recordings_dir": str(каталог / "monitor"),
        "settle_s": 0, "min_duration_s": 10, "skip_unanswered": True,
        "internal_digits": "3, 4, 6", "poll_s": 60, "lookback_days": 7,
        "contexts": {"from-trunk": "входящий", "from-internal": "исходящий"},
    }
    поля.update(поверх)
    return поля


def _wav(путь: Path, секунд: int = 1) -> Path:
    import wave
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"\x00\x00" * (16000 * секунд))
    return путь


def _две_станции(tmp_path: Path) -> tuple[Path, Path]:
    """Две настоящие станции: у каждой свой журнал и свой каталог записей."""
    пути = []
    for имя in ("golovnoy", "filial"):
        каталог = tmp_path / имя
        (каталог / "monitor").mkdir(parents=True, exist_ok=True)
        пути.append(каталог)
    return пути[0], пути[1]


def _записи(каталог: Path, *идентификаторы: str) -> None:
    for uid in идентификаторы:
        _wav(каталог / "monitor" / f"{uid}.wav")


def test_у_каждой_станции_своя_позиция_чтения(tmp_path):
    """Общая позиция означала бы, что вторая станция дочитывает за первой.

    Ключ позиции раньше был один на весь сервер (`telephony_cdr_offset`).
    С двумя станциями это теряет звонки молча: первая станция сдвигает
    позицию на 3 строки, вторая начинает читать свой журнал с четвёртой —
    и первые три её звонка не попадают в архив никогда.
    """
    from asrhub.telephony import Импортёр

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(
        _строка("1.1", "79160000001", "101") + _строка("1.2", "79160000002", "102"),
        encoding="utf-8")
    (вторая / "Master.csv").write_text(
        _строка("2.1", "79160000003", "201"), encoding="utf-8")
    _записи(первая, "1.1", "1.2")
    _записи(вторая, "2.1")

    db, очередь = база(tmp_path), _Очередь()
    станции = [_станция_из(_поля(первая, "Головной офис"), 0),
               _станция_из(_поля(вторая, "Филиал"), 1)]
    имп = [Импортёр(db, с, очередь, _Настройки({})) for с in станции]

    assert имп[0]._ключ_позиции != имп[1]._ключ_позиции
    assert имп[0].scan()["imported"] == 2
    assert имп[1].scan()["imported"] == 1, "вторая станция дочитала за первой"
    assert {з["pbx_uid"] for з in db.list_calls(limit=10)["calls"]} == {
        "1.1", "1.2", "2.1"}


def test_звонок_помнит_станцию_и_владельца(tmp_path):
    """Звонки двух станций лежат рядом — различать их должно поле, а не догадки."""
    from asrhub.telephony import Импортёр

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    (вторая / "Master.csv").write_text(_строка("2.1", "79160000003", "201"),
                                       encoding="utf-8")
    _записи(первая, "1.1")
    _записи(вторая, "2.1")
    db, очередь = база(tmp_path), _Очередь()
    Импортёр(db, _станция_из(_поля(первая, "Головной офис", owner="главный",
                                   tags="центр"), 0), очередь, _Настройки({})).scan()
    Импортёр(db, _станция_из(_поля(вторая, "Филиал", owner="филиал",
                                   tags="регион"), 1), очередь, _Настройки({})).scan()

    станции = {з["pbx_uid"]: з["station"] for з in db.list_calls(limit=10)["calls"]}
    assert станции == {"1.1": "golovnoy-ofis", "2.1": "filial"}
    # Ключ архива — станция плюс идентификатор станции: у Asterisk он
    # уникален только внутри одной АТС, и общий ключ терял бы второй звонок.
    ключи = {з["uniqueid"] for з in db.list_calls(limit=10)["calls"]}
    assert ключи == {"golovnoy-ofis:1.1", "filial:2.1"}
    свои = db.list_calls(limit=10, station="filial")["calls"]
    assert [з["pbx_uid"] for з in свои] == ["2.1"]
    владельцы = {в["owner"] for в in очередь.вызовы}
    assert владельцы == {"главный", "филиал"}


def test_разрез_по_станциям_виден_в_измерениях(tmp_path):
    """Раздел «АТС» строит разрезы по станциям — значит, база их и отдаёт."""
    from asrhub.telephony import Импортёр

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    (вторая / "Master.csv").write_text(_строка("2.1", "79160000003", "201"),
                                       encoding="utf-8")
    _записи(первая, "1.1")
    _записи(вторая, "2.1")
    db, очередь = база(tmp_path), _Очередь()
    for номер, (каталог, имя) in enumerate(((первая, "Головной офис"),
                                            (вторая, "Филиал"))):
        Импортёр(db, _станция_из(_поля(каталог, имя), номер), очередь,
                 _Настройки({})).scan()

    assert sorted(db.call_dimensions()["stations"]) == ["filial", "golovnoy-ofis"]
    assert db.call_counts(station="filial")["total"] == 1


def test_переименование_станции_не_отвязывает_архив(tmp_path):
    """Идентификатор вычисляется один раз и живёт в настройке.

    Если бы он пересчитывался из имени, переименование «Головной офис» в
    «Центральный офис» сделало бы весь прежний архив станции ничьим:
    разрезы по АТС показали бы ноль звонков у живой станции и гору у
    станции, которой нет.
    """
    первая = _станция_из(_поля(tmp_path, "Головной офис"), 0)
    assert первая.id == "golovnoy-ofis"
    второе_имя = _станция_из({**_поля(tmp_path, "Центральный офис"),
                              "id": первая.id}, 0)
    assert второе_имя.id == "golovnoy-ofis"
    assert второе_имя.name == "Центральный офис"


def test_одинаковые_имена_станций_разводятся(tmp_path):
    """Скопировали станцию и забыли поправить имя — архивы не должны слипнуться."""
    from asrhub.telephony import stations

    настройки = _Настройки({"telephony_stations": [
        _поля(tmp_path, "Филиал"), _поля(tmp_path, "Филиал")]})
    ид = [с.id for с in stations.список(настройки)]
    assert len(set(ид)) == 2, ид
    assert ид[1].startswith(ид[0])


def test_старые_настройки_одной_атс_продолжают_работать(tmp_path):
    """Обновление сервера не должно требовать переписывать настройку руками."""
    from asrhub.telephony import stations

    настройки = _Настройки({
        "telephony_source": "cdr_csv",
        "telephony_cdr_file": str(tmp_path / "Master.csv"),
        "telephony_internal_digits": 4,
        "telephony_owner": "старый",
    })
    # Ключи именно заданы, а не достались из умолчаний: на свежем сервере
    # у `telephony_source` тоже есть значение, и станция из него — призрак.
    настройки.sources = {"telephony_source": "config:asrhub.yaml"}
    станции = stations.список(настройки)
    assert len(станции) == 1
    assert станции[0].owner == "старый" and станции[0].длины == {4}
    # Как только список появился — старые ключи больше не в счёт, иначе
    # одна и та же станция читалась бы дважды.
    настройки["telephony_stations"] = [_поля(tmp_path, "Новая")]
    assert [с.name for с in stations.список(настройки)] == ["Новая"]


# ---------------------------------------------------------------------------
# Сведение набора станций на ходу
# ---------------------------------------------------------------------------

def test_новая_станция_подхватывается_без_перезапуска_сервера(tmp_path):
    from asrhub.telephony import Телефония

    первая, вторая = _две_станции(tmp_path)
    настройки = _Настройки({"telephony_enabled": True,
                            "telephony_stations": [_поля(первая, "Головной офис")]})
    телефония = Телефония(база(tmp_path), настройки, _Очередь())
    assert [с["id"] for с in телефония.status()["stations"]] == ["golovnoy-ofis"]

    настройки["telephony_stations"] = [_поля(первая, "Головной офис"),
                                       _поля(вторая, "Филиал")]
    assert [с["id"] for с in телефония.status()["stations"]] == [
        "golovnoy-ofis", "filial"]

    настройки["telephony_stations"] = [_поля(вторая, "Филиал")]
    assert [с["id"] for с in телефония.status()["stations"]] == ["filial"]
    телефония.stop(timeout=1.0)


def test_перестановка_станций_местами_ничего_не_перезаводит(tmp_path):
    """Сверка идёт по идентификатору: порядок в списке — дело вкуса.

    Сверяйся мы по порядку, перетаскивание станции в интерфейсе выше
    соседней перезапускало бы обе и обнуляло их счётчики.
    """
    from asrhub.telephony import Телефония

    первая, вторая = _две_станции(tmp_path)
    настройки = _Настройки({"telephony_enabled": True, "telephony_stations": [
        _поля(первая, "Головной офис"), _поля(вторая, "Филиал")]})
    телефония = Телефония(база(tmp_path), настройки, _Очередь())
    телефония.status()
    были = dict(телефония._станции)
    настройки["telephony_stations"] = [_поля(вторая, "Филиал"),
                                       _поля(первая, "Головной офис")]
    телефония.status()
    assert телефония._станции["filial"] is были["filial"]
    assert телефония._станции["golovnoy-ofis"] is были["golovnoy-ofis"]
    телефония.stop(timeout=1.0)


def test_смена_настроек_станции_перезапускает_её_но_не_теряет_счётчики(tmp_path):
    """Счётчики — про архив станции, а не про её поток.

    Поправили длительность отсечки — поток надо поднять заново (у станции
    может смениться источник), а «взято 40 звонков» при этом не должно
    превратиться в ноль: человек прочтёт это как потерю архива.
    """
    from asrhub.telephony import Телефония

    первая, _ = _две_станции(tmp_path)
    настройки = _Настройки({"telephony_enabled": True,
                            "telephony_stations": [_поля(первая, "Головной офис")]})
    телефония = Телефония(база(tmp_path), настройки, _Очередь())
    телефония.status()
    старый = телефония._станции["golovnoy-ofis"]
    старый.imported, старый.skipped = 40, 7

    настройки["telephony_stations"] = [_поля(первая, "Головной офис",
                                             min_duration_s=30)]
    телефония.status()
    новый = телефония._станции["golovnoy-ofis"]
    assert новый is not старый, "станция не перезапущена при смене настроек"
    assert новый.станция.min_duration_s == 30
    assert (новый.imported, новый.skipped) == (40, 7)
    телефония.stop(timeout=1.0)


def test_выключенная_станция_не_ходит_на_атс(tmp_path):
    from asrhub.telephony import Телефония

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    (вторая / "Master.csv").write_text(_строка("2.1", "79160000003", "201"),
                                       encoding="utf-8")
    _записи(первая, "1.1")
    _записи(вторая, "2.1")
    db = база(tmp_path)
    настройки = _Настройки({"telephony_enabled": True, "telephony_stations": [
        _поля(первая, "Головной офис"),
        _поля(вторая, "Филиал", enabled=False)]})
    свод = Телефония(db, настройки, _Очередь()).scan()
    assert свод["imported"] == 1
    assert [з["station"] for з in db.list_calls(limit=10)["calls"]] == ["golovnoy-ofis"]


def test_заход_по_одной_станции_не_трогает_остальные(tmp_path):
    from asrhub.telephony import Телефония

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    (вторая / "Master.csv").write_text(_строка("2.1", "79160000003", "201"),
                                       encoding="utf-8")
    _записи(первая, "1.1")
    _записи(вторая, "2.1")
    db = база(tmp_path)
    телефония = Телефония(db, _Настройки({"telephony_enabled": True,
                                          "telephony_stations": [
                                              _поля(первая, "Головной офис"),
                                              _поля(вторая, "Филиал")]}),
                          _Очередь())
    свод = телефония.scan(station_id="filial")
    assert свод["imported"] == 1
    assert [з["pbx_uid"] for з in db.list_calls(limit=10)["calls"]] == ["2.1"]


def test_сбой_одной_станции_не_отменяет_заход_остальных(tmp_path):
    """Филиал отвалился — головной офис всё равно должен забрать свои звонки.

    Раньше заход был один на всю телефонию: исключение на первой же
    станции прекращало обход, и записи живых станций ждали следующего
    круга — а при устойчивом сбое не приезжали никогда.
    """
    from asrhub.telephony import Телефония

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    _записи(первая, "1.1")
    db = база(tmp_path)
    настройки = _Настройки({"telephony_enabled": True, "telephony_stations": [
        _поля(вторая, "Филиал", cdr_file=str(вторая / "нет-такого.csv")),
        _поля(первая, "Головной офис")]})
    свод = Телефония(db, настройки, _Очередь()).scan()
    assert свод["imported"] == 1, свод
    assert [о["station"] for о in свод["errors"]] == ["filial"], свод["errors"]


def test_проверка_связи_до_сохранения_настроек(tmp_path):
    """«Проверить подключение» при добавлении АТС — по наброску, а не по записи.

    Иначе проверка требовала бы сперва сохранить станцию: закрытая
    на полпути вкладка оставляла бы в настройках полупустую АТС, которая
    молча пытается ходить неизвестно куда.
    """
    from asrhub.telephony import Телефония

    первая, _ = _две_станции(tmp_path)
    (первая / "Master.csv").write_text(_строка("1.1", "79160000001", "101"),
                                       encoding="utf-8")
    телефония = Телефония(база(tmp_path), _Настройки({"telephony_stations": []}),
                          _Очередь())
    итог = телефония.проверить_набросок(_поля(первая, "Ещё не сохранённая"))
    assert итог["ok"] is True, итог
    assert телефония.status()["configured"] == 0, "набросок осел в настройках"

    # Недоступный источник — это неверная настройка, а не поломка сервера:
    # ошибка приходит с именем станции и подсказкой, что именно чинить.
    with pytest.raises(ConfigError) as сбой:
        телефония.проверить_набросок(
            _поля(первая, "Кривая", cdr_file=str(первая / "нет-такого.csv")))
    assert "Кривая" in сбой.value.message
    assert сбой.value.hint, "не сказано, что делать"


# ---------------------------------------------------------------------------
# Несколько длин внутреннего номера и несколько контекстов
# ---------------------------------------------------------------------------

def test_несколько_длин_внутреннего_номера():
    """В одной сети живут 101, 1010 и 500101 — все они внутренние.

    Одна длина заставляла выбирать, чьи звонки считать правильно: при
    `internal_digits = 3` четырёхзначные номера становились «внешними»,
    и все разговоры между отделами уезжали в статистику как входящие с
    улицы.
    """
    from asrhub.telephony.asterisk import длины_внутренних

    assert длины_внутренних("3, 4, 6") == {3, 4, 6}
    assert длины_внутренних([3, "4", 6.0]) == {3, 4, 6}
    assert длины_внутренних(5) == {5}
    assert длины_внутренних("") == set()
    # Мусор молча не превращается в длину: ни ноль, ни слово, ни минус.
    assert длины_внутренних("0, abc, 4") == {4}
    assert длины_внутренних([0, -2, 99, 4]) == {4}


def test_направление_с_несколькими_длинами():
    from asrhub.telephony.asterisk import Звонок, направление

    длины = "3, 4, 6"
    внутри = Звонок(uniqueid="1", src="101", dst="4021", context="from-internal")
    assert направление(внутри, внутренние_знаков=длины) == "внутренний"
    входящий = Звонок(uniqueid="2", src="79161234567", dst="500101",
                      context="from-trunk")
    assert направление(входящий, внутренние_знаков=длины) == "входящий"
    исходящий = Звонок(uniqueid="3", src="4021", dst="79161234567",
                       context="from-internal")
    assert направление(исходящий, внутренние_знаков=длины) == "исходящий"
    # Пятизначного в списке нет — значит, это не внутренний номер.
    чужой = Звонок(uniqueid="4", src="79161234567", dst="12345",
                   context="from-trunk")
    assert направление(чужой, внутренние_знаков=длины) == "входящий"


def test_порядок_правил_по_контекстам_сохраняется():
    """Правила читаются сверху вниз, и первое подходящее выигрывает.

    Контексты у Asterisk вложены по смыслу: `from-trunk-vip` — частный
    случай `from-trunk`. Если правила перемешать (скажем, сложить в
    множество), звонок VIP-линии будет то одним направлением, то другим —
    в зависимости от того, как в этот раз легли ключи.
    """
    from asrhub.telephony.asterisk import правила_контекстов

    правила = правила_контекстов("from-internal-out=исходящий, from-internal=внутренний")
    assert правила == [("from-internal-out", "исходящий"),
                       ("from-internal", "внутренний")]
    assert правила_контекстов([{"context": "a", "direction": "входящий"},
                               {"context": "b", "direction": "исходящий"}]) == [
        ("a", "входящий"), ("b", "исходящий")]


def test_частный_контекст_побеждает_общий():
    from asrhub.telephony.asterisk import Звонок, направление

    правила = "from-internal-out=исходящий, from-internal=внутренний"
    звонок = Звонок(uniqueid="1", src="101", dst="79161234567",
                    context="from-internal-out")
    assert направление(звонок, внутренние_знаков="3, 4", контексты=правила) == "исходящий"
    обычный = Звонок(uniqueid="2", src="101", dst="4021", context="from-internal")
    assert направление(обычный, внутренние_знаков="3, 4",
                       контексты=правила) == "внутренний"


def test_настройка_станций_проверяется_до_сохранения(tmp_path):
    """Настройку легче поправить в форме, чем ловить её последствия в журнале."""
    from asrhub.telephony.stations import проверить_набор

    assert проверить_набор([_поля(tmp_path, "Головной офис")]) == []
    ошибки = проверить_набор([
        {"name": "", "source": "cdr_csv"},
        {"name": "Филиал", "source": "ami"},
        {"name": "Филиал", "source": "cdr_csv", "cdr_file": "/x"},
        {"name": "Странная", "source": "телепатия"},
        {"name": "Кривые правила", "source": "cdr_csv", "cdr_file": "/x",
         "contexts": "from-trunk=наружу"},
    ])
    свод = " | ".join(ошибки)
    assert "не задано имя" in свод
    assert "нужна учётная запись" in свод
    assert "уже занято" in свод
    assert "неизвестный источник" in свод
    assert "не бывает" in свод


# ---------------------------------------------------------------------------
# Разрезы для раздела «АТС»
# ---------------------------------------------------------------------------

def _архив(tmp_path: Path, сколько: int = 48) -> Database:
    """Сутки звонков двух станций: по звонку в час, через час — вторая."""
    import time as _t
    db = база(tmp_path)
    начало = _t.time() - сколько * 3600
    for н in range(сколько):
        станция = "golovnoy" if н % 2 else "filial"
        db.save_call(
            uniqueid=f"u{н}", job_id=f"job{н}" if н % 3 else None,
            src="79160000001" if н % 2 else "101",
            dst="101" if н % 2 else "79160000001",
            direction="входящий" if н % 2 else "исходящий",
            queue="продажи" if н % 3 else "поддержка", agent=f"оператор {н % 4}",
            duration=70 + н * 10, billsec=60 + н * 10, answered=1 if н % 5 else 0,
            started_at=начало + н * 3600, station=станция,
            skipped="" if н % 4 else "нет записи", owner="telephony")
    return db


def test_лента_по_корзинам_складывает_каждый_звонок_ровно_один_раз(tmp_path):
    db = _архив(tmp_path)
    лента = db.call_timeline(bucket="hour")
    assert sum(т["total"] for т in лента) == 48
    assert all(т["total"] == 1 for т in лента), "в часовую корзину попало больше часа"
    по_суткам = db.call_timeline(bucket="day")
    assert sum(т["total"] for т in по_суткам) == 48
    assert len(по_суткам) < len(лента), "суточная корзина не крупнее часовой"
    # Корзины идут по возрастанию времени: линия, нарисованная в обратном
    # порядке, показывает рост там, где было падение.
    assert [т["t"] for т in лента] == sorted(т["t"] for т in лента)


def test_разрезы_считают_только_свои_звонки(tmp_path):
    """Разрез по владельцу — самая частая забытая строчка в этом проекте."""
    db = _архив(tmp_path)
    db.save_call(uniqueid="чужой", src="1", dst="2", started_at=time.time() - 60,
                 billsec=100, answered=1, direction="входящий", queue="чужая",
                 agent="чужой оператор", station="чужая-атс", owner="другой-отдел")
    for вызов in (
        lambda **к: db.call_timeline(**к),
        lambda **к: db.call_by_station(**к),
        lambda **к: db.call_load_heatmap(**к),
        lambda **к: db.call_duration_histogram(**к),
        lambda **к: db.call_tops("queue", **к),
        lambda **к: db.call_timeline_by_station(**к),
    ):
        свой = json.dumps(вызов(owner="telephony"), ensure_ascii=False)
        assert "чужая" not in свой and "другой-отдел" not in свой, свой


def test_карта_нагрузки_раскладывает_по_местному_времени(tmp_path):
    """Расписание смен читают в своём часовом поясе, а не в UTC."""
    import time as _t
    db = база(tmp_path)
    когда = _t.mktime((2026, 9, 9, 14, 30, 0, 0, 0, -1))     # среда, 14:30 местных
    db.save_call(uniqueid="1", src="79160000001", dst="101", started_at=когда,
                 billsec=120, answered=1, direction="входящий", station="pbx",
                 owner="telephony")
    карта = db.call_load_heatmap()
    assert карта["days"][0] == "Пн", "неделя должна начинаться с понедельника"
    assert карта["calls"][2][14] == 1, карта["calls"]
    assert sum(sum(с) for с in карта["calls"]) == 1
    assert карта["talk_s"][2][14] == 120


def test_гистограмма_длительностей_не_теряет_разговоры(tmp_path):
    db = база(tmp_path)
    for н, сколько in enumerate((5, 45, 90, 200, 400, 900, 1500, 4000)):
        db.save_call(uniqueid=f"u{н}", src="1", dst="2", billsec=сколько,
                     duration=сколько + 10, answered=1, started_at=time.time() - 60,
                     station="pbx", owner="telephony")
    # Неотвеченный в гистограмму не идёт: у него нет разговора, а ноль в
    # корзине «до 30 секунд» выглядел бы как сотня коротких звонков.
    db.save_call(uniqueid="без-ответа", src="1", dst="2", billsec=0, duration=30,
                 answered=0, started_at=time.time() - 60, station="pbx",
                 owner="telephony")
    корзины = db.call_duration_histogram()
    assert sum(к["count"] for к in корзины) == 8
    assert корзины[0]["count"] == 1 and корзины[-1]["count"] == 1
    assert [к["label"] for к in корзины][0].startswith("0")


def test_разрез_по_неизвестному_полю_отвергается(tmp_path):
    """Имя колонки нельзя передать параметром — значит, список закрытый.

    Иначе `field` из запроса попадал бы в SQL как есть, и `queue` можно
    было бы заменить на что угодно: имя колонки в SQL не параметризуется,
    и подстановка строки — это внедрение.
    """
    db = база(tmp_path)
    with pytest.raises(ValueError):
        db.call_tops("owner, (SELECT secret FROM kv)")
    with pytest.raises(ValueError):
        db.call_tops("recording")
    assert db.call_tops("queue") == []


# ---------------------------------------------------------------------------
# Ручки раздела «АТС»
# ---------------------------------------------------------------------------

def _сервер(tmp_path: Path, monkeypatch):
    """Поднятый сервер без очереди и без моделей — только маршруты."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    return TestClient(create_app(load(), start_queue=False))


def test_станцию_заводят_правят_выключают_и_убирают_через_ручки(tmp_path, monkeypatch):
    """Весь путь станции — из интерфейса, без правки файла настроек руками."""
    (tmp_path / "pbx").mkdir()
    журнал = tmp_path / "pbx" / "Master.csv"
    журнал.write_text("", encoding="utf-8")
    with _сервер(tmp_path, monkeypatch) as client:
        заведена = client.post("/api/telephony/stations", json={
            "name": "Головной офис", "source": "cdr_csv", "cdr_file": str(журнал),
            "recordings_dir": str(tmp_path / "pbx"), "internal_digits": "3, 4"})
        assert заведена.status_code == 200, заведена.text
        ид = заведена.json()["station"]["id"]
        assert ид == "golovnoy-ofis"

        # Правка по идентификатору, а не по месту в списке.
        правка = client.post("/api/telephony/stations",
                             json={"id": ид, "min_duration_s": 25})
        assert правка.status_code == 200
        assert правка.json()["station"]["min_duration_s"] == 25
        assert правка.json()["station"]["name"] == "Головной офис", "правка стёрла поля"

        выкл = client.post(f"/api/telephony/stations/{ид}/enabled?enabled=false")
        assert выкл.status_code == 200 and выкл.json()["enabled"] is False
        станции = client.get("/api/telephony/stations").json()["items"]
        assert [с["enabled"] for с in станции] == [False]

        убрана = client.delete(f"/api/telephony/stations/{ид}")
        assert убрана.status_code == 200
        assert client.get("/api/telephony/stations").json()["items"] == []


def test_вторая_станция_не_отменяет_первую(tmp_path, monkeypatch):
    """Настройка пишется списком целиком — легко потерять соседей."""
    (tmp_path / "pbx").mkdir()
    (tmp_path / "pbx" / "Master.csv").write_text("", encoding="utf-8")
    поля = {"source": "cdr_csv", "cdr_file": str(tmp_path / "pbx" / "Master.csv"),
            "recordings_dir": str(tmp_path / "pbx")}
    with _сервер(tmp_path, monkeypatch) as client:
        client.post("/api/telephony/stations", json={**поля, "name": "Первая"})
        client.post("/api/telephony/stations", json={**поля, "name": "Вторая"})
        имена = [с["name"] for с in client.get("/api/telephony/stations").json()["items"]]
        assert имена == ["Первая", "Вторая"], имена


def test_станция_с_занятым_именем_не_сохраняется(tmp_path, monkeypatch):
    (tmp_path / "pbx").mkdir()
    (tmp_path / "pbx" / "Master.csv").write_text("", encoding="utf-8")
    поля = {"name": "Филиал", "source": "cdr_csv",
            "cdr_file": str(tmp_path / "pbx" / "Master.csv")}
    with _сервер(tmp_path, monkeypatch) as client:
        assert client.post("/api/telephony/stations", json=поля).status_code == 200
        ответ = client.post("/api/telephony/stations", json=поля)
        assert ответ.status_code == 400
        assert "занято" in ответ.text


def test_обзор_отдаёт_всё_нужное_разделу_одним_ответом(tmp_path, monkeypatch):
    with _сервер(tmp_path, monkeypatch) as client:
        db = client.app.state.hub.db
        сейчас = time.time()
        for н in range(12):
            db.save_call(uniqueid=f"o{н}", src="79160000001", dst="101",
                         direction="входящий", queue="продажи", agent="Иванов А.",
                         duration=130, billsec=120, answered=1,
                         started_at=сейчас - н * 3600, station="pbx",
                         owner="telephony", job_id=f"j{н}" if н % 2 else None)
        свод = client.get("/api/telephony/overview?period=week").json()
        for ключ in ("period", "bucket", "calls", "period_calls", "by_station",
                     "timeline", "station_timelines", "heatmap", "durations", "tops"):
            assert ключ in свод, ключ
        assert свод["period_calls"]["total"] == 12
        assert свод["tops"]["queue"][0]["value"] == "продажи"
        assert sum(т["total"] for т in свод["timeline"]) == 12
        # Шаг линии подбирается под период: за неделю по часам, за год по
        # неделям. Иначе «за год» — это 8760 точек ради картинки в 900.
        assert свод["bucket"] == "hour"
        assert client.get("/api/telephony/overview?period=year").json()["bucket"] == "week"


def test_проверка_наброска_не_оставляет_станцию_в_настройках(tmp_path, monkeypatch):
    """«Проверить при добавлении» — до сохранения, иначе форма сорит станциями."""
    (tmp_path / "pbx").mkdir()
    (tmp_path / "pbx" / "Master.csv").write_text("", encoding="utf-8")
    with _сервер(tmp_path, monkeypatch) as client:
        ответ = client.post("/api/telephony/test", json={
            "name": "Ещё не сохранённая", "source": "cdr_csv",
            "cdr_file": str(tmp_path / "pbx" / "Master.csv")})
        assert ответ.status_code == 200 and ответ.json()["ok"] is True
        assert client.get("/api/telephony/stations").json()["items"] == []


def test_свежий_сервер_не_показывает_призрачную_атс(tmp_path, monkeypatch):
    """У `telephony_source` есть значение по умолчанию — и это ловушка.

    Проверка «старый ключ не пустой» срабатывала на сервере, где телефонию
    не настраивали вовсе: в разделе «АТС» появлялась станция «АТС», которая
    ходила по путям из умолчаний, ничего не находила и портила все разрезы
    пустой строкой в списке станций.
    """
    with _сервер(tmp_path, monkeypatch) as client:
        assert client.get("/api/telephony/stations").json()["items"] == []
        assert client.get("/api/telephony/status").json()["configured"] == 0


# ---------------------------------------------------------------------------
# Дефекты, найденные ревизией тридцатого захода
# ---------------------------------------------------------------------------

def test_отложенные_звонки_чужой_станции_не_забираются(tmp_path):
    """Импортёр берёт из очереди отложенных ТОЛЬКО свои звонки.

    Без отбора по станции головной офис забирал отложенные звонки филиала,
    обрабатывал их своими правилами и переписывал им владельца и станцию:
    разговоры филиала уезжали в чужой отчёт и в чужую очередь, а запись
    искалась в чужом каталоге и не находилась никогда.
    """
    from asrhub.telephony import Импортёр

    первая, вторая = _две_станции(tmp_path)
    (первая / "Master.csv").write_text("", encoding="utf-8")
    (вторая / "Master.csv").write_text("", encoding="utf-8")
    db, очередь = база(tmp_path), _Очередь()
    свой = _станция_из(_поля(первая, "Головной офис", owner="главный"), 0)
    чужой = _станция_из(_поля(вторая, "Филиал", owner="филиал"), 1)

    # Филиал отложил звонок: запись ещё пишется.
    db.save_call("filial:2.1", job_id=None, skipped=f"{db.ОТЛОЖЕН}ещё пишется",
                 owner="филиал", station=чужой.id, pbx_uid="2.1",
                 src="79160000003", dst="201", started_at=time.time() - 120,
                 duration=100, billsec=90, answered=1)

    свои = db.calls_deferred(station=свой.id)
    assert свои == [], "головной офис видит отложенные филиала"
    имп = Импортёр(db, свой, очередь, _Настройки({}))
    имп.scan()
    строка = db.list_calls(limit=5)["calls"][0]
    assert строка["owner"] == "филиал" and строка["station"] == "filial"
    assert db.calls_deferred(station=чужой.id), "филиал потерял свой отложенный"


def test_безнадёжные_отложенные_не_забивают_очередь(tmp_path):
    """Запись, не появившаяся за сутки, не появится уже никогда.

    Такие звонки навечно занимали место в очереди отложенных, и после
    одного сбоя записи импорт по станции вставал целиком: каждый заход
    перебирал одних и тех же безнадёжных и до новых не доходил.
    """
    старьё = time.time() - 3 * 86400
    db = база(tmp_path)
    for н in range(30):
        db.save_call(f"pbx:z{н}", job_id=None, skipped=f"{db.ОТЛОЖЕН}файл ещё растёт",
                     owner="telephony", station="pbx", pbx_uid=f"z{н}",
                     src="1", dst="2", started_at=старьё, duration=100, billsec=90)
    db.save_call("pbx:свежий", job_id=None, skipped=f"{db.ОТЛОЖЕН}ещё пишется",
                 owner="telephony", station="pbx", pbx_uid="свежий",
                 src="1", dst="2", started_at=time.time() - 300, duration=100, billsec=90)
    порог = time.time() - 24 * 3600
    очередь = db.calls_deferred(limit=25, station="pbx", older_than=порог)
    assert [з["pbx_uid"] for з in очередь] == ["свежий"]


def test_одинаковый_идентификатор_у_двух_атс_не_теряет_звонок(tmp_path):
    """У Asterisk `uniqueid` уникален только внутри одной станции.

    Это «эпоха.счётчик», счётчик локален станции и сбрасывается при её
    перезапуске. Две станции в одну секунду дают одинаковый идентификатор,
    и второй звонок молча выбрасывался как «уже импортирован».
    """
    from asrhub.telephony import Импортёр

    первая, вторая = _две_станции(tmp_path)
    один_и_тот_же = "1789145000.1"
    (первая / "Master.csv").write_text(_строка(один_и_тот_же, "79160000001", "101"),
                                       encoding="utf-8")
    (вторая / "Master.csv").write_text(_строка(один_и_тот_же, "79160000002", "201"),
                                       encoding="utf-8")
    _записи(первая, один_и_тот_же)
    _записи(вторая, один_и_тот_же)
    db, очередь = база(tmp_path), _Очередь()
    for номер, (каталог, имя) in enumerate(((первая, "Головной офис"), (вторая, "Филиал"))):
        итог = Импортёр(db, _станция_из(_поля(каталог, имя), номер), очередь,
                        _Настройки({})).scan()
        assert итог["imported"] == 1, (имя, итог)
    звонки = db.list_calls(limit=5)["calls"]
    assert len(звонки) == 2
    # Настоящий идентификатор станции сохранён: по нему ищется запись и по
    # нему звонок узнают на самой АТС.
    assert {з["pbx_uid"] for з in звонки} == {один_и_тот_же}
    assert {з["uniqueid"] for з in звонки} == {
        f"golovnoy-ofis:{один_и_тот_же}", f"filial:{один_и_тот_же}"}


def test_одинаковые_имена_файлов_в_разных_папках_не_слипаются(tmp_path):
    """Asterisk раскладывает записи по годам и месяцам.

    «out-101-….wav» из марта и из апреля — два разных разговора с одним
    именем, и ключ по имени файла терял второй как «уже импортирован».
    """
    from asrhub.telephony import Импортёр

    каталог = tmp_path / "архив"
    for месяц in ("03", "04"):
        _wav(каталог / "monitor" / "2026" / месяц / "out-101-79160000001.wav")
    db, очередь = база(tmp_path), _Очередь()
    станция = _станция_из({**_поля(каталог, "Архив"), "source": "folder",
                           "recordings_dir": str(каталог / "monitor"),
                           "min_duration_s": 0, "lookback_days": 3650}, 0)
    итог = Импортёр(db, станция, очередь, _Настройки({})).scan()
    assert итог["imported"] == 2, итог


def test_сведение_читает_настройки_под_замком(tmp_path):
    """Два одновременных запроса не должны воскрешать убранную станцию.

    Тот, кто прочитал список до замка, под замком заводил станцию, которую
    сосед только что убрал: удалённая АТС продолжала ходить и заводить
    задания, и само это не проходило — `_свести` зовут только ручки.
    """
    import inspect

    from asrhub.telephony import Телефония

    исходник = inspect.getsource(Телефония._свести)
    без_комментариев = "\n".join(
        с for с in исходник.splitlines() if not с.strip().startswith("#"))
    замок = без_комментариев.index("with self._lock:")
    чтение = без_комментариев.index("self.станции()")
    assert чтение > замок, "настройки читаются до замка"


def test_станцией_из_настроек_без_идентификатора_можно_управлять(tmp_path, monkeypatch):
    """В настройке, написанной руками, поля `id` нет.

    Раздел показывал такую станцию (идентификатор считается на лету), а
    правка и удаление отвечали «станция не найдена»: станция есть, сделать
    с ней нельзя ничего.
    """
    (tmp_path / "pbx").mkdir()
    (tmp_path / "pbx" / "Master.csv").write_text("", encoding="utf-8")
    with _сервер(tmp_path, monkeypatch) as client:
        client.app.state.hub.settings.set("telephony_stations", [
            {"name": "Головной офис", "source": "cdr_csv",
             "cdr_file": str(tmp_path / "pbx" / "Master.csv")},
            {"name": "Филиал", "source": "cdr_csv",
             "cdr_file": str(tmp_path / "pbx" / "Master.csv")},
        ], source="api")
        станции = client.get("/api/telephony/stations").json()["items"]
        ид = станции[0]["id"]
        assert client.post(f"/api/telephony/stations/{ид}/enabled?enabled=false"
                           ).status_code == 200
        правка = client.post("/api/telephony/stations",
                             json={"id": ид, "min_duration_s": 33})
        assert правка.status_code == 200, правка.text
        assert правка.json()["station"]["min_duration_s"] == 33
        # Удаление убирает ровно одну станцию.
        assert client.delete(f"/api/telephony/stations/{ид}").status_code == 200
        осталось = client.get("/api/telephony/stations").json()["items"]
        assert [с["name"] for с in осталось] == ["Филиал"]


def test_суточные_корзины_режутся_по_местному_времени(tmp_path, monkeypatch):
    """Сервер живёт в UTC, а сутки считает человек по своим часам.

    Без поправки сутки начинались в 03:00, ночные звонки попадали во
    вчера, и линия нагрузки противоречила соседней тепловой карте в одном
    и том же ответе.
    """
    # Пояс задаём явно: контейнер живёт в UTC, и на нём поправка неотличима
    # от её отсутствия — проверка проходила бы и без исправления.
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    db = база(tmp_path)
    день = (2026, 9, 10)
    for н, час in enumerate((0, 12, 23)):
        когда = time.mktime((*день, час, 30, 0, 0, 0, -1))
        db.save_call(uniqueid=f"u{н}", src="1", dst="2", started_at=когда,
                     billsec=100, duration=110, answered=1, station="pbx",
                     owner="telephony")
    лента = db.call_timeline(bucket="day")
    assert len(лента) == 1, [time.strftime("%d.%m %H:%M", time.localtime(т["t"]))
                             for т in лента]
    assert лента[0]["total"] == 3
    начало = time.localtime(лента[0]["t"])
    assert (начало.tm_hour, начало.tm_min) == (0, 0)
    time.tzset()


def test_средний_разговор_считается_по_отвеченным(tmp_path):
    """Недозвон длится ноль секунд и занижает «средний разговор».

    Ровно на свою долю: у очереди с половиной недозвонов средняя выходила
    вдвое меньше настоящей.
    """
    db = база(tmp_path)
    сейчас = time.time()
    for н in range(5):
        db.save_call(uniqueid=f"о{н}", src="1", dst="2", queue="продажи",
                     started_at=сейчас - н * 60, billsec=200, duration=210,
                     answered=1, station="pbx", owner="telephony")
        db.save_call(uniqueid=f"н{н}", src="1", dst="2", queue="продажи",
                     started_at=сейчас - н * 60, billsec=0, duration=25,
                     answered=0, station="pbx", owner="telephony")
    строка = db.call_tops("queue")[0]
    assert строка["count"] == 10 and строка["answered"] == 5
    assert строка["avg_s"] == 200.0, строка
