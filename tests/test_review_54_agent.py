"""Заход 54: агент на станции — находки ревизии 3.1.20 (отчёт 14-agent).

Агента проверяем настоящим — против поддельной станции из `test_agent` и
против настоящего сервера (`TestClient`): звонки, потерянные на отказе
«агент выключен»; пробный запуск, сдвигавший позицию; один звонок,
останавливавший отправку навсегда; прокси, тихо лишавший архив записей.
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_agent import ФАЙЛ_АГЕНТА, Станция, wav, агент, состояние, стенд, строка_cdr
from test_agent_api import АДМИН, звонки, здравствуй, прислать
from test_agent_api import сервер as сервер  # noqa: F401  (фикстура)


@pytest.fixture(autouse=True)
def _быстрые_замеры(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(агент, "ПАУЗА_ЗАМЕРА", 0.01)


def запись_wav(секунд: int = 1) -> bytes:
    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(8000)
        файл.writeframes(b"\x01\x02" * (8000 * секунд))
    return буфер.getvalue()


def журнал(tmp_path: Path, *строки: str) -> None:
    (tmp_path / "Master.csv").write_text("".join(строки), encoding="utf-8")


ДАВНО = time.time() - 3600


# ---------------------------------------------------------------------------
# №1. Отказ «агент выключен» не теряет звонки
# ---------------------------------------------------------------------------

def test_агент_выключен_на_сервере_звонки_ждут_а_не_теряются(tmp_path: Path):
    """Сервер отвечает на «выключен» 400 — раньше это «ошибка звонка».

    Заход шёл дальше, позиция журнала уходила в конец, а звонки не
    приходили больше никогда — и после включения тоже.
    """
    журнал_ = "".join(строка_cdr(f"a1.{н}", начало=ДАВНО) for н in range(4))
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        исходная = а.клиент.отправить

        def выключен(поля, путь=None):
            raise агент.ОтказСервера(400, "agent_disabled",
                                     "Агент выключен на сервере.")

        а.клиент.отправить = выключен
        а.поздороваться()
        with pytest.raises(агент.ОтказСервера):
            а.проход("new")
        assert станция.принятые == []
        assert set(а.состояние.данные["отложенные"]) == {f"a1.{н}" for н in range(4)}

        а.клиент.отправить = исходная          # агента включили
        свод = а.проход("new")
        а.закрыть()
    assert свод["отправлено"] == 4
    assert {з["meta"]["uniqueid"] for з in станция.принятые} == {f"a1.{н}" for н in range(4)}


def test_сервер_различает_выключенного_и_неизвестного_агента(сервер: TestClient):
    agent_id = здравствуй(сервер)["agent_id"]
    неизвестный = прислать(сервер, "no-such-agent", "100.1")
    assert неизвестный.json()["detail"]["code"] == "agent_unknown"
    сервер.post(f"/api/telephony/agents/{agent_id}/enabled", headers=АДМИН,
                json={"enabled": False})
    выключен = прислать(сервер, agent_id, "100.2")
    assert выключен.json()["detail"]["code"] == "agent_disabled"
    # И на вопросе о звонках — то же: агент не идёт слать то, что не примут.
    вопрос = сервер.post("/api/telephony/agent/ask", headers=АДМИН,
                         json={"agent_id": agent_id, "ids": ["100.3"]})
    assert вопрос.status_code == 400 and вопрос.json()["detail"]["code"] == "agent_disabled"


# ---------------------------------------------------------------------------
# №2. --dry-run ничего не меняет
# ---------------------------------------------------------------------------

def test_пробный_запуск_не_двигает_позицию_и_не_снимает_задание(tmp_path: Path):
    приветствия = []
    журнал_ = строка_cdr("b2.1", начало=ДАВНО) + строка_cdr("b2.2", начало=ДАВНО)
    with Станция(команда="collect:all") as станция:
        пробный = стенд(tmp_path, станция.адрес, журнал_, "--dry-run")
        исходное = пробный.клиент.hello

        def запомнить(сведения):
            приветствия.append(dict(сведения))
            return исходное(сведения)

        пробный.клиент.hello = запомнить
        пробный.один_заход("new")
        пробный.закрыть()
        assert станция.принятые == []
        assert приветствия and приветствия[0].get("peek") is True
        assert not (tmp_path / "state" / "state.json").exists(), "пробный запуск записал состояние"

        настоящий = стенд(tmp_path, станция.адрес, журнал_)
        настоящий.поздороваться()
        свод = настоящий.проход("new")
        настоящий.закрыть()
    assert свод["отправлено"] == 2, "показанные пробно звонки потом не уехали"


# ---------------------------------------------------------------------------
# №3. «Ядовитый» звонок не останавливает отправку
# ---------------------------------------------------------------------------

def test_звонок_с_устойчивой_пятисоткой_не_держит_остальные(tmp_path: Path):
    журнал_ = "".join(строка_cdr(f"c3.{н}", начало=ДАВНО) for н in range(4))
    for н in range(4):
        wav(tmp_path / "monitor" / f"c3.{н}.wav")
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        исходная = а.клиент.отправить

        def плохой(поля, путь=None):
            if поля["uniqueid"] == "c3.0" and путь:
                raise агент.ОтказСервера(502, "", "плохой шлюз")
            return исходная(поля, путь)

        а.клиент.отправить = плохой
        а.поздороваться()
        первый = а.проход("new")
        assert первый["отправлено"] == 3, "один звонок остановил отправку остальных"
        # Попытка засчитывается, только когда в том же заходе уходят другие
        # звонки: иначе дело не в звонке, а в сервере. Новые звонки идут.
        for номер in range(агент.ПОПЫТОК_НА_ЗВОНОК):
            with (tmp_path / "Master.csv").open("a", encoding="utf-8") as файл:
                файл.write(строка_cdr(f"c3.новый{номер}", начало=ДАВНО))
            а.проход("new")
        а.закрыть()
    принятые = {з["meta"]["uniqueid"]: з for з in станция.принятые}
    assert {"c3.0", "c3.1", "c3.2", "c3.3"} <= set(принятые)
    assert принятые["c3.0"]["имя_файла"] is None
    assert принятые["c3.0"]["meta"]["skipped"].startswith("сервер не принял запись")


def test_полная_очередь_сервера_не_засчитывается_звонку(tmp_path: Path):
    """503 «очередь полна» — дело не в звонке: заход прерывается сразу, а
    звонок не уезжает без записи, сколько бы заходов очередь ни была полна."""
    журнал_ = "".join(строка_cdr(f"c3q.{н}", начало=ДАВНО) for н in range(3))
    for н in range(3):
        wav(tmp_path / "monitor" / f"c3q.{н}.wav")
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        исходная = а.клиент.отправить

        def занят(поля, путь=None):
            if поля["uniqueid"] == "c3q.1":
                raise агент.ОтказСервера(503, "queue_full", "очередь полна")
            return исходная(поля, путь)

        а.клиент.отправить = занят
        а.поздороваться()
        for _ in range(агент.ПОПЫТОК_НА_ЗВОНОК + 2):
            with pytest.raises(агент.ОтказСервера):
                а.проход("new")
        assert а.состояние.попыток("c3q.1") == 0
        assert а.состояние.причина("c3q.1").startswith("сервер занят")
        а.клиент.отправить = исходная
        а.проход("new")
        а.закрыть()
    принятые = {з["meta"]["uniqueid"]: з for з in станция.принятые}
    assert set(принятые) == {"c3q.0", "c3q.1", "c3q.2"}
    assert принятые["c3q.1"]["имя_файла"], "звонок уехал без записи из-за полной очереди"


def test_ошибка_чтения_записи_не_выглядит_как_недоступный_сервер(tmp_path: Path,
                                                                monkeypatch):
    """EIO при чтении записи urllib заворачивал в «сервер недоступен»."""
    wav(tmp_path / "monitor" / "c3b.1.wav")
    настоящий_open = open

    class Сбойный:
        def __init__(self, путь, *а, **к):
            self._файл = настоящий_open(путь, *а, **к)

        def read(self, *а):
            raise OSError(5, "Input/output error")

        def seek(self, *а):
            return self._файл.seek(*а)

        def __enter__(self):
            return self

        def __exit__(self, *а):
            self._файл.close()

    def открыть(путь, режим="r", *а, **к):
        if str(путь).endswith(".wav") and "b" in режим:
            return Сбойный(путь, режим, *а, **к)
        return настоящий_open(путь, режим, *а, **к)

    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, строка_cdr("c3b.1", начало=ДАВНО))
        а.поздороваться()
        monkeypatch.setattr(агент, "open", открыть, raising=False)
        свод = а.проход("new")
        а.закрыть()
    assert свод["отправлено"] == 1 and свод["ошибок"] == 0, свод
    # Поддельная станция успевает записать и оборванный запрос (у настоящего
    # сервера неполное тело не разбирается) — смотрим последний.
    принятый = станция.принятые[-1]
    assert принятый["имя_файла"] is None
    assert принятый["meta"]["skipped"].startswith("запись не читается")


# ---------------------------------------------------------------------------
# №4. Срок отложенных — от первого откладывания, истёкшие едут
# ---------------------------------------------------------------------------

def test_срок_отложенного_от_первого_раза_а_истёкший_едет_без_записи(tmp_path: Path):
    хранилище = состояние(tmp_path)
    звонок = агент.разобрать_строку_cdr(строка_cdr("d4.1"))
    хранилище.отложить(звонок, "ещё пишется")
    первый_срок = хранилище.данные["отложенные"]["d4.1"]["до"]
    хранилище.отложить(звонок, "ещё пишется")
    assert хранилище.данные["отложенные"]["d4.1"]["до"] == первый_срок, \
        "повторное откладывание продлило срок"
    # Отложенные по связи и остановке — без срока.
    хранилище.отложить(агент.разобрать_строку_cdr(строка_cdr("d4.2")),
                       "связь с сервером прервалась")
    assert хранилище.данные["отложенные"]["d4.2"]["до"] == 0

    хранилище.данные["отложенные"]["d4.1"]["до"] = time.time() - 1
    просроченные = хранилище.просроченные()
    assert [з.uniqueid for з in просроченные] == ["d4.1"]
    assert просроченные[0].последний_шанс == "ещё пишется"


def test_истёкший_отложенный_отправляется_а_не_исчезает(tmp_path: Path):
    """Агент сутки не делал заходов — отложенные удалялись молча."""
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, "")
        а.состояние.отложить(агент.разобрать_строку_cdr(строка_cdr("d4.3", начало=ДАВНО)),
                             "ещё пишется")
        а.состояние.данные["отложенные"]["d4.3"]["до"] = time.time() - 1
        а.поздороваться()
        а.проход("new")
        а.закрыть()
    [принятый] = станция.принятые
    assert принятый["meta"]["uniqueid"] == "d4.3"
    assert принятый["meta"]["skipped"] in ("нет записи", "не дождались записи: ещё пишется")


def test_пустой_старый_файл_это_пустая_запись_а_не_вечное_ожидание(tmp_path: Path):
    путь = tmp_path / "monitor" / "d4.4.wav"
    путь.parent.mkdir(parents=True)
    путь.write_bytes(b"")
    os.utime(путь, (ДАВНО, ДАВНО))
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, строка_cdr("d4.4", начало=ДАВНО - 600))
        а.поздороваться()
        свод = а.проход("new")
        а.закрыть()
    assert свод["отправлено"] == 1
    assert станция.принятые[0]["meta"]["skipped"] == "пустая запись"


# ---------------------------------------------------------------------------
# №5. 413 от прокси — громко и обратимо
# ---------------------------------------------------------------------------

def test_413_от_прокси_громко_и_запись_потом_дошлётся(tmp_path: Path, caplog):
    with Станция() as станция:
        станция.сбои["/api/telephony/agent/call"] = [413]
        wav(tmp_path / "monitor" / "e5.1.wav")
        а = стенд(tmp_path, станция.адрес, строка_cdr("e5.1", начало=ДАВНО))
        а.поздороваться()
        with caplog.at_level(logging.ERROR, logger="asrhub-agent"):
            свод = а.проход("new")
        а.закрыть()
    assert "прокси" in caplog.text, "о прокси, режущем записи, не сказано"
    assert свод["причины"].get("прокси не пропустил запись") == 1
    принятый = станция.принятые[-1]
    assert принятый["meta"]["skipped"].startswith("сервер не принял запись")


def test_сервер_даёт_дослать_запись_непринятую_раньше(сервер: TestClient):
    agent_id = здравствуй(сервер)["agent_id"]
    без_записи = прислать(сервер, agent_id, "200.1", файл=None,
                          skipped="сервер не принял запись: прокси (HTTP 413)")
    assert без_записи.status_code == 200, без_записи.text
    вопрос = сервер.post("/api/telephony/agent/ask", headers=АДМИН,
                         json={"agent_id": agent_id, "ids": ["200.1"]}).json()
    assert вопрос["wanted"] == ["200.1"], "«уже в архиве» — записи не дослать"
    с_записью = прислать(сервер, agent_id, "200.1", файл=("200.1.wav", запись_wav()))
    assert с_записью.status_code == 200, с_записью.text
    assert с_записью.json().get("job_id")
    [звонок] = звонки(сервер, f"agent:{agent_id}")
    assert звонок["job_id"] and звонок["skipped"] == ""


# ---------------------------------------------------------------------------
# №6. Запись по номерам — по концу звонка и не дважды
# ---------------------------------------------------------------------------

def test_длинному_звонку_достаётся_своя_запись_а_не_короткого(tmp_path: Path):
    каталог = tmp_path / "monitor"
    начало = ДАВНО - 7200
    wav(каталог / "in-79161234567-101-100000.wav")
    os.utime(каталог / "in-79161234567-101-100000.wav", (начало + 60, начало + 60))
    wav(каталог / "in-79161234567-101-100200.wav")
    os.utime(каталог / "in-79161234567-101-100200.wav",
             (начало + 120 + 2280, начало + 120 + 2280))
    записи = агент.Записи(str(каталог), окно_минут=120)
    длинный = агент.Звонок(uniqueid="x-2", src="79161234567", dst="101",
                           started_at=начало + 120, duration=2280)
    assert Path(записи.найти(длинный)).name == "in-79161234567-101-100200.wav"
    # Отданный файл второму звонку не достаётся.
    записи.выдан(записи.найти(длинный), "x-2")
    третий = агент.Звонок(uniqueid="x-3", src="79161234567", dst="101",
                          started_at=начало + 120, duration=2280)
    assert Path(записи.найти(третий)).name == "in-79161234567-101-100000.wav"


def test_звёздочка_из_номера_не_становится_маской(tmp_path: Path):
    каталог = tmp_path / "monitor"
    wav(каталог / "79990001122-101.wav")
    записи = агент.Записи(str(каталог), шаблон="{src}-{dst}*.wav", окно_минут=0)
    чужой = агент.Звонок(uniqueid="f6.1", src="*", dst="101", started_at=time.time())
    assert записи.найти(чужой) is None


# ---------------------------------------------------------------------------
# №7. Несколько строк одного uniqueid
# ---------------------------------------------------------------------------

def test_строки_одного_звонка_уезжают_одним_звонком_с_главной_ногой(tmp_path: Path):
    журнал_ = (строка_cdr("g7.1", dst="101", disposition="NO ANSWER", duration=20,
                          billsec=0, начало=ДАВНО)
               + строка_cdr("g7.1", dst="102", начало=ДАВНО + 20))
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        а.поздороваться()
        а.проход("new")
        а.закрыть()
    [принятый] = станция.принятые
    assert принятый["meta"]["dst"] == "102" and принятый["meta"]["billsec"] == 290


def test_сервер_берёт_отвеченную_ногу_пришедшую_позже(сервер: TestClient):
    agent_id = здравствуй(сервер)["agent_id"]
    первая = прислать(сервер, agent_id, "300.1", файл=None, dst="101", answered=False,
                      billsec=0, disposition="NO ANSWER", skipped="без ответа")
    assert первая.status_code == 200
    вторая = прислать(сервер, agent_id, "300.1", файл=("300.1.wav", запись_wav()),
                      dst="102", billsec=290, disposition="ANSWERED")
    assert вторая.json().get("job_id"), вторая.text
    [звонок] = звонки(сервер, f"agent:{agent_id}")
    assert звонок["dst"] == "102" and звонок["job_id"]


# ---------------------------------------------------------------------------
# №8, №9. Квоты и одновременный приём
# ---------------------------------------------------------------------------

def test_суточная_квота_действует_и_на_звонки_агента(сервер: TestClient):
    настройки = сервер.app.state.hub.settings
    настройки.api_keys["ah_quota"] = {"name": "квота", "role": "user", "enabled": True,
                                      "quota_jobs_per_day": 1}
    ключ = {"X-API-Key": "ah_quota"}
    agent_id = сервер.post("/api/telephony/agent/hello", headers=ключ,
                           json={"host": "pbx-q", "name": "Q"}).json()["agent_id"]
    коды = []
    for номер in range(3):
        ответ = сервер.post(
            "/api/telephony/agent/call", headers=ключ,
            data={"meta": json.dumps({"agent_id": agent_id, "uniqueid": f"400.{номер}",
                                      "answered": True, "billsec": 30})},
            files={"file": (f"400.{номер}.wav", запись_wav(), "audio/wav")})
        коды.append(ответ.status_code)
    assert коды[0] == 200 and 429 in коды[1:], коды
    uploads = Path(настройки.paths.uploads)
    assert len(list(uploads.glob("pbx_*"))) == 1, "отвергнутая запись осталась на диске"


def test_звонок_уже_принимаемый_другим_запросом_получает_409(сервер: TestClient):
    from asrhub.api import routes_agent

    agent_id = здравствуй(сервер)["agent_id"]
    ключ = f"agent:{agent_id}:500.1"
    routes_agent._ПРИНИМАЮТСЯ.add(ключ)
    try:
        ответ = прислать(сервер, agent_id, "500.1", файл=("500.1.wav", запись_wav()))
    finally:
        routes_agent._ПРИНИМАЮТСЯ.discard(ключ)
    assert ответ.status_code == 409
    assert ответ.json()["detail"]["code"] == "call_in_progress"
    assert len(сервер.app.state.hub.db.list_jobs(limit=10)) == 0


# ---------------------------------------------------------------------------
# №10. Указатель каталога и замер роста
# ---------------------------------------------------------------------------

def test_пустой_указатель_не_пересобирается_на_каждый_звонок(tmp_path: Path, monkeypatch):
    каталог = tmp_path / "monitor"
    wav(каталог / "без-идентификатора.wav")
    обходов = []
    настоящий = агент.os.walk

    def считать(*а, **к):
        обходов.append(1)
        return настоящий(*а, **к)

    monkeypatch.setattr(агент.os, "walk", считать)
    записи = агент.Записи(str(каталог))
    for номер in range(10):
        записи.найти(агент.Звонок(uniqueid=f"1757{номер:06d}.1", started_at=ДАВНО))
    assert len(обходов) <= 2, f"обходов каталога: {len(обходов)}"


def test_давний_файл_не_меряется_на_рост(tmp_path: Path, monkeypatch):
    путь = wav(tmp_path / "monitor" / "старый.wav")
    os.utime(путь, (ДАВНО - 86400, ДАВНО - 86400))
    сна = []
    monkeypatch.setattr(агент.time, "sleep", lambda с: сна.append(с))
    assert агент.Записи(str(tmp_path / "monitor")).готовность(str(путь)) == ""
    assert сна == [], "замер роста у файла суточной давности"


# ---------------------------------------------------------------------------
# №11. Предел захода, страницы MySQL, предел скорости
# ---------------------------------------------------------------------------

def test_обычный_заход_берёт_не_больше_предела(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(агент, "ПРЕДЕЛ_ЗАХОДА", 5)
    журнал_ = "".join(строка_cdr(f"h11.{н}", начало=ДАВНО) for н in range(12))
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        а.поздороваться()
        свод = а.проход("new")
        assert свод["отправлено"] == 5 and свод["ещё"] is True
        while а.проход("new")["ещё"]:
            pass
        а.закрыть()
    assert len(станция.принятые) == 12


def test_выборка_mysql_идёт_страницами(tmp_path: Path, monkeypatch):
    from test_agent import источник_mysql

    monkeypatch.setattr(агент.ИсточникMySQL, "СТРАНИЦА", 2)
    источник = источник_mysql(tmp_path, "[global]\ndbname=cdrdb\n")
    запросы = []

    def строка(н):
        return "\t".join(["", "79161234567", "101", "from-trunk", "К", "SIP/tr", "SIP/101",
                          "Dial", "SIP/101", f"2026-09-10 14:0{н}:00", "60", "50",
                          "ANSWERED", f"1757500001.{н}", ""])

    def выполнить(запрос):
        if запрос.startswith("SHOW COLUMNS"):         # проверка колонок linkedid
            return ""
        запросы.append(запрос)
        if len(запросы) == 1:
            return строка(1) + "\n" + строка(2) + "\n"
        if len(запросы) == 2:
            return строка(3) + "\n"
        return ""

    источник._выполнить = выполнить
    звонки_ = list(источник.звонки(весь_архив=True))
    assert [з.uniqueid for з in звонки_] == ["1757500001.1", "1757500001.2", "1757500001.3"]
    assert "LIMIT 2" in запросы[0] and "uniqueid > '1757500001.2'" in запросы[1]


def test_предел_скорости_отправки_записи():
    паузы = []
    тело = агент.ТелоЗапроса(b"", io.BytesIO(b"\x00" * 65536), b"", кбит_с=64,
                             ждать=паузы.append)
    while тело.read(8192):
        pass
    # 64 КБ при 64 кбит/с — восемь секунд; ждали «по часам» не меньше семи.
    assert sum(паузы) >= 7.0, sum(паузы)


# ---------------------------------------------------------------------------
# №12, №13. Ключ доступа и одинаковые имена хостов
# ---------------------------------------------------------------------------

def test_агента_можно_передать_новому_ключу(сервер: TestClient):
    настройки = сервер.app.state.hub.settings
    настройки.api_keys["ah_old"] = {"name": "старый", "role": "user", "enabled": True}
    настройки.api_keys["ah_new"] = {"name": "новый", "role": "user", "enabled": True}
    agent_id = сервер.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_old"},
                           json={"host": "pbx-k", "name": "K"}).json()["agent_id"]
    чужой = сервер.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_new"},
                        json={"agent_id": agent_id, "host": "pbx-k"})
    assert чужой.status_code == 403
    assert "/owner" in чужой.json()["detail"]["hint"]
    передача = сервер.post(f"/api/telephony/agents/{agent_id}/owner", headers=АДМИН,
                           json={"owner": "новый"})
    assert передача.status_code == 200, передача.text
    свой = сервер.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_new"},
                       json={"agent_id": agent_id, "host": "pbx-k"})
    assert свой.status_code == 200, свой.text


def test_отозванный_ключ_не_запирает_агента(сервер: TestClient):
    """Отозвали ключ, завели новый, перезапустили install.sh — раньше 403."""
    настройки = сервер.app.state.hub.settings
    настройки.api_keys["ah_old2"] = {"name": "старый2", "role": "user", "enabled": True}
    настройки.api_keys["ah_new2"] = {"name": "новый2", "role": "user", "enabled": True}
    agent_id = сервер.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_old2"},
                           json={"host": "pbx-r", "name": "R"}).json()["agent_id"]
    настройки.api_keys.pop("ah_old2")
    ответ = сервер.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_new2"},
                        json={"agent_id": agent_id, "host": "pbx-r"})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["agent_id"] == agent_id


def test_две_станции_с_одним_именем_хоста_не_сливаются(сервер: TestClient):
    первая = здравствуй(сервер, host="freepbx.sangoma.local", name="", machine="aaaa")
    вторая = здравствуй(сервер, host="freepbx.sangoma.local", name="", machine="bbbb")
    assert первая["agent_id"] != вторая["agent_id"]
    # А та же машина после потери состояния — узнаётся.
    снова = здравствуй(сервер, host="freepbx.sangoma.local", name="", machine="aaaa")
    assert снова["agent_id"] == первая["agent_id"]


# ---------------------------------------------------------------------------
# №14. Байты не из UTF-8
# ---------------------------------------------------------------------------

@pytest.mark.skipif(sys.platform != "linux", reason="имена в байтах — Linux")
def test_имя_записи_в_cp1251_уезжает_и_состояние_сохраняется(tmp_path: Path):
    каталог = tmp_path / "monitor"
    каталог.mkdir(parents=True)
    имя = "звонок-1757500009.1.wav".encode("cp1251")
    with open(os.path.join(os.fsencode(str(каталог)), имя), "wb") as файл:
        файл.write(запись_wav())
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, строка_cdr("1757500009.1", начало=ДАВНО))
        а.поздороваться()
        свод = а.проход("new")
        а.состояние.отложить(агент.Звонок(uniqueid="x", recording=os.fsdecode(
            os.path.join(os.fsencode(str(каталог)), имя))), "ещё пишется")
        assert а.состояние.сохранить(принудительно=True) is True
        а.закрыть()
    assert свод["ошибок"] == 0 and свод["с записью"] == 1, свод
    assert not list((tmp_path / "state").glob("state.json.tmp*"))


def test_вывод_mysql_в_cp1251_не_роняет_заход(tmp_path: Path):
    программа = tmp_path / "bin" / "mysql"
    программа.parent.mkdir()
    программа.write_text("#!/bin/sh\nprintf '\\310\\342\\340\\355\\356\\342\\n'\n",
                         encoding="ascii")
    программа.chmod(0o755)
    from test_agent import источник_mysql

    источник = источник_mysql(tmp_path, "[global]\ndbname=cdrdb\n")
    старый_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{программа.parent}{os.pathsep}{старый_path}"
    try:
        вывод = источник._выполнить("SELECT 1")
    finally:
        os.environ["PATH"] = старый_path
    assert вывод.strip(), "вывод потерян"


# ---------------------------------------------------------------------------
# №15. Локаль C на станции
# ---------------------------------------------------------------------------

def test_агент_в_локали_c_не_падает_на_кириллице(tmp_path: Path):
    окружение = {к: з for к, з in os.environ.items()
                 if not к.startswith(("LC_", "LANG", "PYTHONIO", "PYTHONUTF8"))}
    окружение.update({"LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
    итог = subprocess.run([sys.executable, str(ФАЙЛ_АГЕНТА), "--help"],
                          capture_output=True, env=окружение, timeout=60)
    assert итог.returncode == 0, итог.stderr.decode("utf-8", "replace")
    assert "Агент ASR Hub" in итог.stdout.decode("utf-8")


# ---------------------------------------------------------------------------
# №16. Ротация журнала без прав на каталог
# ---------------------------------------------------------------------------

def test_журнал_пишется_дальше_когда_ротация_невозможна(tmp_path: Path, monkeypatch):
    файл = tmp_path / "agent.log"
    агент.настроить_журнал(str(файл), предел_мб=1, копий=2)
    обработчик = next(о for о in агент.log.handlers if isinstance(о, logging.FileHandler))

    def нельзя(*_а):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "rename", нельзя)
    monkeypatch.setattr(os, "replace", нельзя)
    for номер in range(9000):
        агент.log.info("строка %d %s", номер, "x" * 100)
    агент.log.info("ПОСЛЕ ПРЕДЕЛА")
    обработчик.flush()
    текст = файл.read_text(encoding="utf-8", errors="replace")
    for о in list(агент.log.handlers):
        агент.log.removeHandler(о)
        о.close()
    assert "ПОСЛЕ ПРЕДЕЛА" in текст, "журнал перестал писаться после предела"


# ---------------------------------------------------------------------------
# №17. Ручной запуск от root
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="нужен root")
def test_запуск_от_root_работает_от_владельца_каталога_состояния(tmp_path: Path):
    каталог = tmp_path / "state"
    каталог.mkdir()
    os.chown(каталог, 65534, 65534)
    tmp_path.chmod(0o755)
    os.chmod(каталог, 0o700)
    for предок in каталог.parents:
        # Каталог состояния службы лежит в /var/lib — проходимом для всех.
        if str(предок) in ("/", "/tmp"):
            break
        os.chmod(предок, os.stat(предок).st_mode | 0o011)
    код = (
        "import importlib.util, os, sys\n"
        f"сп = importlib.util.spec_from_file_location('a', {str(ФАЙЛ_АГЕНТА)!r})\n"
        "м = importlib.util.module_from_spec(сп); сп.loader.exec_module(м)\n"
        f"м.сбросить_права({str(каталог)!r})\n"
        f"open(os.path.join({str(каталог)!r}, 'state.json'), 'w').write('{{}}')\n"
        "print(os.geteuid())\n")
    итог = subprocess.run([sys.executable, "-c", код], capture_output=True, text=True,
                          timeout=60)
    assert итог.returncode == 0, итог.stderr
    assert итог.stdout.strip() == "65534"
    assert (каталог / "state.json").stat().st_uid == 65534


# ---------------------------------------------------------------------------
# №18. Остановка посреди чтения журнала
# ---------------------------------------------------------------------------

def test_сигнал_посреди_чтения_журнала_не_теряет_звонок(tmp_path: Path):
    журнал_ = "".join(строка_cdr(f"k18.{н}", начало=ДАВНО) for н in range(4))
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, журнал_)
        а.поздороваться()
        настоящий = а.источник.звонки

        def с_сигналом(**параметры_):
            for номер, звонок in enumerate(настоящий(**параметры_)):
                if номер == 2:
                    а.остановить(15)
                yield звонок

        а.источник.звонки = с_сигналом
        with pytest.raises(агент.Остановлен):
            а.проход("new")
        а.закрыть()
    отложенные = set(а.состояние.данные["отложенные"])
    assert отложенные == {"k18.0", "k18.1", "k18.2"}, отложенные


def test_пауза_повтора_прерывается_сигналом(tmp_path: Path):
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, "")
        а._остановлен = True
        with pytest.raises(агент.Остановлен):
            а._ждать_повтора(30)
        а.закрыть()


# ---------------------------------------------------------------------------
# №19, №20. «%» в настройках, синонимы источника
# ---------------------------------------------------------------------------

def test_процент_в_настройках_не_роняет_агента(tmp_path: Path):
    файл = tmp_path / "asrhub-agent.conf"
    файл.write_text("[agent]\nurl = http://x\nkey = ah_50%скидка\n", encoding="utf-8")
    assert агент.прочитать_настройки(str(файл))["key"] == "ah_50%скидка"
    from test_agent import источник_mysql

    источник = источник_mysql(tmp_path, "[global]\ndbname=cdrdb\npassword=Pa%ss\n")
    assert источник.настройки["password"] == "Pa%ss"


def test_источник_csv_и_dir_как_в_документации():
    разбор = агент.собрать_аргументы()
    assert разбор.parse_args(["--source", "csv"]).source == "csv"
    assert разбор.parse_args(["--source", "dir"]).source == "dir"


# ---------------------------------------------------------------------------
# Недоделки отчёта
# ---------------------------------------------------------------------------

def test_ключ_не_попадает_в_трассировку_и_исключение(tmp_path: Path):
    файл = tmp_path / "agent.log"
    ключ = "ah_secret1234567890"
    агент.настроить_журнал(str(файл), ключ=ключ)
    try:
        raise RuntimeError(f"запрос с ключом {ключ} не прошёл")
    except RuntimeError as сбой:
        агент.log.exception("сбой: %s", сбой)
    for о in list(агент.log.handlers):
        о.flush()
        агент.log.removeHandler(о)
        о.close()
    текст = файл.read_text(encoding="utf-8")
    assert ключ not in текст and "ah_secr…" in текст


def test_перенаправление_не_уводит_ключ_на_другой_узел(tmp_path: Path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    пришло = []

    class Перенаправляет(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):                                   # noqa: N802
            пришло.append(self.path)
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:9/api/telephony/agent/hello")
            self.send_header("Content-Length", "0")
            self.end_headers()

    сервер_ = ThreadingHTTPServer(("127.0.0.1", 0), Перенаправляет)
    threading.Thread(target=сервер_.serve_forever, daemon=True).start()
    try:
        клиент = агент.Клиент(f"http://127.0.0.1:{сервер_.server_address[1]}",
                              "ah_test1234567890", повторов=0, ждать=lambda _: None)
        with pytest.raises(агент.ОтказСервера) as отказ:
            клиент.hello({"name": "x"})
    finally:
        сервер_.shutdown()
        сервер_.server_close()
    assert отказ.value.статус == 302 and "перенаправляет" in отказ.value.сообщение
    assert пришло == ["/api/telephony/agent/hello"]


def test_ссылка_наружу_из_каталога_записей_не_становится_звонком(tmp_path: Path):
    каталог = tmp_path / "monitor"
    каталог.mkdir()
    секрет = wav(tmp_path / "секрет.wav")
    os.symlink(секрет, каталог / "ссылка.wav")
    wav(каталог / "свой.wav")
    источник = агент.ИсточникКаталога(str(каталог), состояние(tmp_path))
    звонки_ = list(источник.звонки())
    assert [Path(з.recording).name for з in звонки_] == ["свой.wav"]
    # И начало звонка — время изменения минус длительность (конец записи).
    сведения = os.stat(каталог / "свой.wav")
    assert звонки_[0].started_at == pytest.approx(сведения.st_mtime - 1, abs=0.01)


def test_состояние_без_изменений_не_переписывается(tmp_path: Path):
    хранилище = состояние(tmp_path)
    хранилище.отметить("s.1")
    assert хранилище.сохранить(принудительно=True)
    путь = Path(хранилище.путь)
    os.utime(путь, (1, 1))
    assert хранилище.сохранить(принудительно=True)
    assert путь.stat().st_mtime == 1, "состояние переписано без единого изменения"
    хранилище.отметить("s.2")
    хранилище.сохранить(принудительно=True)
    assert путь.stat().st_mtime > 1


def test_полный_сбор_читает_копии_dateext(tmp_path: Path):
    журнал(tmp_path, строка_cdr("m.3"))
    (tmp_path / "Master.csv-20260901").write_text(строка_cdr("m.1"), encoding="utf-8")
    (tmp_path / "Master.csv-20260902").write_text(строка_cdr("m.2"), encoding="utf-8")
    os.utime(tmp_path / "Master.csv-20260901", (ДАВНО - 200, ДАВНО - 200))
    os.utime(tmp_path / "Master.csv-20260902", (ДАВНО - 100, ДАВНО - 100))
    источник = агент.ИсточникCSV([str(tmp_path / "Master.csv")], состояние(tmp_path))
    assert [з.uniqueid for з in источник.звонки(весь_архив=True)] == ["m.1", "m.2", "m.3"]


def test_свежий_звонок_без_записи_ждёт_а_потом_едет_с_ней(tmp_path: Path):
    сейчас = time.time()
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес,
                  строка_cdr("n.1", начало=сейчас - 40, duration=30, billsec=20))
        а.поздороваться()
        assert а.проход("new")["причины"].get("записи ещё нет") == 1
        wav(tmp_path / "monitor" / "n.1.wav")
        os.utime(tmp_path / "monitor" / "n.1.wav", (сейчас - 20, сейчас - 20))
        а.записи._пересобран = -1e9
        а.проход("new")
        а.закрыть()
    [принятый] = станция.принятые
    assert принятый["имя_файла"] == "n.1.wav"


def test_остатки_убитого_процесса_убираются_при_старте(tmp_path: Path):
    каталог = tmp_path / "state"
    каталог.mkdir()
    (каталог / "state.json.tmp99999").write_text("{", encoding="utf-8")
    (каталог / "mysql-99999.cnf").write_text("[client]\npassword=секрет\n", encoding="utf-8")
    with Станция() as станция:
        а = стенд(tmp_path, станция.адрес, "")
        а.закрыть()
    assert not (каталог / "state.json.tmp99999").exists()
    assert not (каталог / "mysql-99999.cnf").exists()


def test_штатная_остановка_службы_код_успеха(tmp_path: Path):
    with Станция() as станция:
        станция.сбои["/api/telephony/agent/hello"] = [400]
        а = стенд(tmp_path, станция.адрес, "")
        исходный = а._поспать

        def и_стоп(секунд):
            а._остановлен = True
            исходный(0)

        а._поспать = и_стоп
        код = а.следить(5)
        а.закрыть()
    assert а.ошибок == 1 and код == агент.КОД_УСПЕХ


def test_сервер_отвергает_точки_бесконечность_и_длинный_uniqueid(сервер: TestClient):
    точки = сервер.post("/api/telephony/agent/hello", headers=АДМИН,
                        json={"agent_id": "..", "host": "x"})
    assert точки.status_code == 400
    agent_id = здравствуй(сервер)["agent_id"]
    assert прислать(сервер, agent_id, "9" * 200, файл=None).status_code == 400
    ответ = прислать(сервер, agent_id, "600.1", файл=None, duration="Infinity")
    assert ответ.status_code == 200, ответ.text


def test_контрольная_сумма_записи_сверяется(сервер: TestClient):
    agent_id = здравствуй(сервер)["agent_id"]
    плохая = сервер.post(
        "/api/telephony/agent/call", headers=АДМИН,
        data={"meta": json.dumps({"agent_id": agent_id, "uniqueid": "700.1"}),
              "sha256": "0" * 64},
        files={"file": ("700.1.wav", запись_wav(), "audio/wav")})
    assert плохая.status_code == 400 and "контрольная сумма" in плохая.json()["detail"]["message"]
    assert звонки(сервер, f"agent:{agent_id}") == []


def test_агент_шлёт_сумму_записи_полем_после_файла(tmp_path: Path):
    import hashlib

    wav(tmp_path / "monitor" / "p.1.wav")
    with Станция() as станция:
        тела = []
        а = стенд(tmp_path, станция.адрес, строка_cdr("p.1", начало=ДАВНО))
        исходный = а.клиент._запрос

        def запомнить(метод, путь, тело=None, *ар, **кв):
            if путь.endswith("/call") and hasattr(тело, "read"):
                тело.перемотать()
                тела.append(тело.read())
                тело.перемотать()
            return исходный(метод, путь, тело, *ар, **кв)

        а.клиент._запрос = запомнить
        а.поздороваться()
        а.проход("new")
        а.закрыть()
    сумма = hashlib.sha256((tmp_path / "monitor" / "p.1.wav").read_bytes()).hexdigest()
    assert тела and f'name="sha256"\r\n\r\n{сумма}'.encode() in тела[0]


def test_ключ_пользователя_не_заводит_агентов_без_счёта(сервер: TestClient, monkeypatch):
    from asrhub.api import routes_agent

    monkeypatch.setattr(routes_agent, "АГЕНТОВ_НА_КЛЮЧ", 2)
    ключ = {"X-API-Key": "ah_agent"}
    коды = [сервер.post("/api/telephony/agent/hello", headers=ключ,
                        json={"host": f"pbx-{н}", "name": f"P{н}"}).status_code
            for н in range(3)]
    assert коды == [200, 200, 400], коды
    # Уже заведённый агент того же ключа здоровается как прежде.
    агенты = [а for а in сервер.app.state.hub.db.agent_list() if а["owner"] == "агент"]
    повтор = сервер.post("/api/telephony/agent/hello", headers=ключ,
                         json={"agent_id": агенты[0]["id"], "host": агенты[0]["host"]})
    assert повтор.status_code == 200, повтор.text
