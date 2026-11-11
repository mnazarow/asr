"""Заход 54: приём звонков из диалплана Asterisk (`/api/telephony/dialplan`).

Станция сама сообщает о каждом разговоре — из обработчика отбоя или по
окончании MixMonitor — и присылает запись файлом либо путь к ней на общем
диске. Здесь проверяется то, что стоит дорого, если сломается:

* звонок с полями диалплана встаёт в архив станции с ЕГО добавочным,
  направлением и очередью — сервер их не перевычисляет;
* повтор того же звонка не заводит второго задания и не оставляет файла;
* пороги станции (короткий, без ответа) работают так же, как для журнала,
  а присланный файл отсеянного звонка не копится на диске;
* путь к записи принимается только внутри каталога станции — иначе приём
  стал бы «прочитай любой файл сервера»;
* запись на общем диске, которая ещё пишется, откладывается и дозабирается
  потоком станции;
* каждый исход, включая отказ ключа, виден в журнале приёма;
* готовые файлы настройки для chan_sip и chan_pjsip не пропускают значения
  звонящего в командную строку без FILTER();
* сценарий отправки доставляет звонок, отбрасывает опасные значения,
  копит очередь повтора и досылает её.
"""
from __future__ import annotations

import io
import os
import re
import subprocess
import time
import wave
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from asrhub.telephony import dialplan as Д
from asrhub.telephony import dialplan_config as К
from fastapi.testclient import TestClient

АДМИН = {"X-API-Key": "ah_admin"}
СТАНЦИЯ = {"X-API-Key": "ah_pbx"}
АДРЕС = "/api/telephony/dialplan/call"
КОРЕНЬ = Path(__file__).resolve().parents[1]
СЦЕНАРИЙ = КОРЕНЬ / "agent" / "asrhub-dialplan"


def wav(секунд: float = 6.0) -> bytes:
    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(8000)
        файл.writeframes(b"\x01\x00" * int(8000 * секунд))
    return буфер.getvalue()


def станция(**поверх):
    return {"id": "main", "name": "Головной офис", "source": "dialplan",
            "enabled": True, "min_duration_s": 5, "skip_unanswered": True,
            "settle_s": 30, "poll_s": 3600, "owner": "sales",
            "owner_map": {"102": "support"}, "internal_digits": 3, **поверх}


def приложение(data_dir: Path, monkeypatch: pytest.MonkeyPatch, станции, *,
               включено: bool = True):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin": {"name": "админ", "role": "admin", "enabled": True},
        "ah_pbx": {"name": "станция", "role": "user", "enabled": True},
        "ah_ro": {"name": "чтение", "role": "readonly", "enabled": True},
    })
    настройки.set("telephony_enabled", включено)
    настройки.set("telephony_stations", станции)
    Д.журнал.сбросить()
    return create_app(настройки, start_queue=False)


@pytest.fixture()
def каталог(tmp_path: Path) -> Path:
    путь = tmp_path / "monitor"
    (путь / "asrhub" / "2026" / "10" / "27").mkdir(parents=True)
    return путь


@pytest.fixture()
def клиент(data_dir: Path, monkeypatch: pytest.MonkeyPatch, каталог: Path):
    app = приложение(data_dir, monkeypatch, [станция(recordings_dir=str(каталог))])
    with TestClient(app) as c:
        c.hub = app.state.hub
        yield c


def прислать(клиент, uniqueid: str = "1793000000.11", *, файл=("rec.wav", None),
             headers=СТАНЦИЯ, **поля):
    данные = {"uniqueid": uniqueid, "src": "79161234567", "dst": "74950000000",
              "agent": "102", "direction": "in", "queue": "sales",
              "billsec": "30", "duration": "41", "disposition": "ANSWERED",
              "started_at": str(int(time.time()) - 60), **поля}
    данные = {к: з for к, з in данные.items() if з is not None}
    файлы = None
    if файл is not None:
        имя, содержимое = файл
        файлы = {"file": (имя, содержимое if содержимое is not None else wav(), "audio/wav")}
    return клиент.post(АДРЕС, headers=headers, data=данные, files=файлы)


def строка(клиент, ключ: str) -> dict | None:
    r = клиент.hub.db.query_one("SELECT * FROM calls WHERE uniqueid=?", (ключ,))
    return dict(r) if r else None


def загрузки(клиент) -> set[str]:
    return {п.name for п in клиент.hub.settings.paths.uploads.glob("pbx*")}


# ---------------------------------------------------------------------------
# Приём файлом
# ---------------------------------------------------------------------------

def test_файлом_звонок_встаёт_с_полями_диалплана(клиент):
    ответ = прислать(клиент)
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["result"] == "queued" and тело["job_id"]
    assert тело["key"] == "main:1793000000.11" and тело["station"] == "main"
    # Добавочный — тот, кого назвал диалплан, а не набранный городской номер:
    # без этого входящий в очередь доставался бы номеру линии.
    з = строка(клиент, "main:1793000000.11")
    assert з["agent"] == "102" and з["direction"] == "входящий" and з["queue"] == "sales"
    assert з["pbx_uid"] == "1793000000.11" and з["station"] == "main"
    assert з["billsec"] == 30 and з["answered"] == 1 and з["disposition"] == "ANSWERED"
    # Владелец — по карте станции для этого добавочного.
    assert з["owner"] == "support" and тело["owner"] == "support"
    задание = клиент.hub.db.get_job(тело["job_id"])
    assert задание["source"] == "asterisk" and задание["owner"] == "support"
    assert Path(з["recording"]).is_file()


def test_повтор_не_заводит_второго_задания_и_файла(клиент):
    первый = прислать(клиент).json()
    было = загрузки(клиент)
    второй = прислать(клиент)
    assert второй.status_code == 200
    assert второй.json()["result"] == "duplicate"
    assert второй.json()["job_id"] == первый["job_id"]
    assert загрузки(клиент) == было
    assert клиент.hub.db.query_one("SELECT COUNT(*) AS n FROM jobs")["n"] == 1


def test_пороги_станции_и_файл_отсеянного_не_копится(клиент):
    было = загрузки(клиент)
    короткий = прислать(клиент, "1793000000.12", billsec="2", duration="4").json()
    assert короткий["result"] == "skipped" and короткий["skipped"] == "короткий"
    # «NO ANSWER», пропущенный в диалплане через FILTER(A-Z), — «NOANSWER».
    без_ответа = прислать(клиент, "1793000000.13", disposition="NOANSWER").json()
    assert без_ответа["skipped"] == "без ответа"
    assert загрузки(клиент) == было
    з = строка(клиент, "main:1793000000.13")
    assert з["disposition"] == "NO ANSWER" and з["answered"] == 0 and з["recording"] == ""
    assert строка(клиент, "main:1793000000.12")["skipped"] == "короткий"
    # DIALSTATUS тоже понимается: CANCEL — звонящий положил трубку до ответа.
    отмена = прислать(клиент, "1793000000.14", disposition=None, dialstatus="CANCEL").json()
    assert отмена["skipped"] == "без ответа"


def test_длительность_без_полей_меряется_по_записи(клиент):
    ответ = прислать(клиент, "1793000000.15", billsec=None, duration=None,
                     disposition=None, started_at=None, файл=("r.wav", wav(7))).json()
    assert ответ["result"] == "queued"
    з = строка(клиент, "main:1793000000.15")
    assert з["billsec"] == 7 and з["duration"] == 7 and з["answered"] == 1
    # Начало — «сейчас минус длительность»: обращение приходит в конце разговора.
    assert time.time() - 60 < з["started_at"] <= time.time()


def test_направление_и_добавочный_если_не_назвали(клиент):
    ответ = прислать(клиент, "1793000000.16", direction=None, agent=None, queue=None,
                     src="101", dst="79161234567").json()
    з = строка(клиент, "main:1793000000.16")
    # Внутренний номер в три цифры звонит наружу — исходящий, добавочный — звонивший.
    assert ответ["direction"] == "исходящий" == з["direction"] and з["agent"] == "101"
    синоним = прислать(клиент, "1793000000.17", direction="internal", src="101",
                       dst="102").json()
    assert синоним["direction"] == "внутренний"
    непонятное = прислать(клиент, "1793000000.18", direction="туда").json()
    assert any("направление" in б for б in непонятное["warnings"])


def test_не_звук_и_негодный_идентификатор(клиент):
    было = загрузки(клиент)
    ответ = прислать(клиент, "1793000000.19", файл=("rec.txt", b"hello"))
    assert ответ.status_code == 415 and ответ.json()["code"] == "unsupported_format"
    ответ = прислать(клиент, "../../etc/x")
    assert ответ.status_code == 400 and "идентификатор" in ответ.json()["message"]
    ответ = прислать(клиент, "")
    assert ответ.status_code == 400
    assert загрузки(клиент) == было
    assert клиент.hub.db.query_one("SELECT COUNT(*) AS n FROM calls")["n"] == 0


# ---------------------------------------------------------------------------
# Приём путём на общем диске
# ---------------------------------------------------------------------------

def test_путь_только_внутри_каталога_станции(клиент, каталог, tmp_path):
    чужой = tmp_path / "secret.wav"
    чужой.write_bytes(wav())
    ссылка = каталог / "asrhub" / "link.wav"
    ссылка.symlink_to(чужой)
    for путь in (str(чужой), "../secret.wav", "asrhub/link.wav"):
        ответ = прислать(клиент, "1793000000.20", файл=None, recording=путь, ready="1")
        assert ответ.status_code == 403, (путь, ответ.text)
        assert ответ.json()["code"] == "forbidden"
    ответ = прислать(клиент, "1793000000.20", файл=None, recording="asrhub/x.txt")
    assert ответ.status_code == 415
    assert строка(клиент, "main:1793000000.20") is None


def test_путь_готовой_записи_ставится_сразу(клиент, каталог):
    запись = каталог / "asrhub" / "2026" / "10" / "27" / "1793000000.21.wav"
    запись.write_bytes(wav())
    ответ = прислать(клиент, "1793000000.21", файл=None,
                     recording="asrhub/2026/10/27/1793000000.21.wav", ready="1")
    assert ответ.status_code == 200 and ответ.json()["result"] == "queued", ответ.text
    assert строка(клиент, "main:1793000000.21")["recording"] == str(запись.resolve())


def test_путь_дописываемой_записи_откладывается_и_дозабирается(клиент, каталог):
    запись = каталог / "asrhub" / "2026" / "10" / "27" / "1793000000.22.wav"
    запись.write_bytes(wav())
    сейчас = time.time()
    ответ = прислать(клиент, "1793000000.22", файл=None,
                     recording=str(запись), started_at=str(int(сейчас - 41)))
    assert ответ.status_code == 202, ответ.text
    assert ответ.json()["result"] == "deferred" and ответ.json()["deferred"] == "ещё пишется"
    з = строка(клиент, "main:1793000000.22")
    assert з["skipped"].startswith(клиент.hub.db.ОТЛОЖЕН) and з["agent"] == "102"
    # Прошло время: файл закрыт давно — поток станции дозабирает звонок с
    # теми же полями диалплана (добавочный не перевычисляется).
    клиент.hub.db.execute("UPDATE calls SET started_at=? WHERE uniqueid=?",
                          (сейчас - 7200, "main:1793000000.22"))
    os.utime(запись, (сейчас - 3600, сейчас - 3600))
    итог = клиент.hub.telephony.импортёр("main").scan()
    assert итог["imported"] == 1 and итог["deferred"] == 1
    з = строка(клиент, "main:1793000000.22")
    assert з["job_id"] and з["skipped"] == "" and з["agent"] == "102"
    assert з["direction"] == "входящий" and з["queue"] == "sales"


def test_без_записи_и_без_каталога_звонок_в_журнале(data_dir, monkeypatch):
    app = приложение(data_dir, monkeypatch, [станция()])
    with TestClient(app) as c:
        c.hub = app.state.hub
        ответ = прислать(c, "1793000000.23", файл=None).json()
        assert ответ["result"] == "skipped" and ответ["skipped"] == "нет записи"
        assert строка(c, "main:1793000000.23")["agent"] == "102"
        # Запись можно дослать: сценарий после 413 шлёт звонок без файла, и
        # раньше повтор с файлом получал «уже импортирован» навсегда.
        ответ = прислать(c, "1793000000.23").json()
        assert ответ["result"] == "queued" and ответ["job_id"]
        assert строка(c, "main:1793000000.23")["job_id"] == ответ["job_id"]
        assert прислать(c, "1793000000.23").json()["result"] == "duplicate"
        # Путь без каталога у станции — отказ с объяснением, а не поиск по диску.
        ответ = прислать(c, "1793000000.24", файл=None, recording="/tmp/x.wav")
        assert ответ.status_code == 400 and "только файлом" in ответ.json()["message"]


# ---------------------------------------------------------------------------
# Виды запроса, станция, ключ
# ---------------------------------------------------------------------------

def test_форма_без_content_type_и_поля_в_адресе(клиент, каталог):
    """Так присылает CURL() диалплана, когда в CURLOPT задан свой заголовок."""
    запись = каталог / "asrhub" / "1793000000.25.wav"
    запись.write_bytes(wav())
    тело = ("uniqueid=1793000000.25&src=%2B79161234567&dst=101&direction=internal"
            "&billsec=30&disposition=NO%20ANSWER&recording=asrhub%2F1793000000.25.wav")
    ответ = клиент.post(f"{АДРЕС}?station=main", content=тело.encode(),
                        headers={**СТАНЦИЯ})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["skipped"] == "без ответа"
    з = строка(клиент, "main:1793000000.25")
    assert з["src"] == "+79161234567" and з["disposition"] == "NO ANSWER"
    # JSON и ключ в адресе — для своих интеграций и старого Asterisk.
    ответ = клиент.post(f"{АДРЕС}?api_key=ah_pbx", json={
        "uniqueid": "1793000000.26", "callerid": "79160000000", "exten": "101",
        "billsec": 20, "dry_run": True})
    assert ответ.status_code == 200 and ответ.json()["result"] == "dry_run"
    assert ответ.json()["direction"] == "входящий"


def test_выбор_станции(data_dir, monkeypatch):
    app = приложение(data_dir, monkeypatch, [
        станция(), станция(id="filial", name="Филиал"),
        {"id": "old", "name": "Журнал", "source": "cdr_csv", "cdr_file": "/nonexistent"}])
    with TestClient(app) as c:
        ответ = прислать(c, файл=None)
        assert ответ.status_code == 400
        assert "main" in ответ.json()["hint"] and "filial" in ответ.json()["hint"]
        assert прислать(c, файл=None, station="Филиал").json()["station"] == "filial"
        assert прислать(c, файл=None, station="нет-такой").status_code == 400
        # Станцию с журналом можно назвать явно — ключ архива тот же, что у
        # её импортёра, и звонок не задвоится.
        assert прислать(c, "1793000000.27", файл=None,
                        station="old").json()["key"] == "old:1793000000.27"
    app = приложение(data_dir, monkeypatch, [])
    with TestClient(app) as c:
        ответ = прислать(c, файл=None)
        assert ответ.status_code == 400 and "Нет станции" in ответ.json()["message"]


def test_выключенная_телефония_и_станция(data_dir, monkeypatch):
    app = приложение(data_dir, monkeypatch, [станция()], включено=False)
    with TestClient(app) as c:
        ответ = прислать(c, файл=None)
        # Временно, а не навсегда: 503 и Retry-After — сценарий держит звонок
        # в pending/ и дошлёт его сам, а не складывает в failed/.
        assert ответ.status_code == 503 and "выключен" in ответ.json()["message"]
        assert ответ.headers.get("Retry-After") == "300"
    app = приложение(data_dir, monkeypatch, [станция(enabled=False)])
    with TestClient(app) as c:
        ответ = прислать(c, файл=None)
        assert ответ.status_code == 503 and "выключена" in ответ.json()["message"]


def test_проба_ничего_не_пишет(клиент):
    было = загрузки(клиент)
    ответ = прислать(клиент, "1793000000.28", dry_run="1").json()
    assert ответ["result"] == "dry_run" and ответ["would"] == "queue"
    assert ответ["owner"] == "support" and ответ["agent"] == "102"
    assert прислать(клиент, "1793000000.29", dry_run="1",
                    billsec="1").json()["reason"] == "короткий"
    assert строка(клиент, "main:1793000000.28") is None and загрузки(клиент) == было


def test_ключ_и_журнал_приёма(клиент):
    assert прислать(клиент, headers={}).status_code == 401
    assert прислать(клиент, headers={"X-API-Key": "ah_ro"}).status_code == 403
    прислать(клиент, "1793000000.30")
    прислать(клиент, "1793000000.30")
    ответ = клиент.get("/api/telephony/dialplan/status", headers=АДМИН)
    assert ответ.status_code == 200
    данные = ответ.json()
    итоги = [з["result"] for з in данные["recent"]]
    # Свежие первыми; отказы ключа видны вместе с удачными приёмами.
    assert итоги[:4] == ["duplicate", "queued", "rejected", "rejected"]
    assert {з["code"] for з in данные["recent"][2:4]} == {"auth_error", "forbidden"}
    станция_ = данные["stations"][0]
    assert станция_["id"] == "main" and станция_["intake"]["queued"] == 1
    assert данные["enabled"] is True and данные["endpoint"] == АДРЕС
    # Журнал обращений — с номерами телефонов: только администратору.
    assert клиент.get("/api/telephony/dialplan/status", headers=СТАНЦИЯ).status_code == 403
    assert клиент.delete("/api/telephony/dialplan/log", headers=АДМИН).status_code == 200
    assert клиент.get("/api/telephony/dialplan/status",
                      headers=АДМИН).json()["recent"] == []


def test_проверка_связи_и_разбор_станции_диалплана(клиент):
    прислать(клиент, "1793000000.31")
    связь = клиент.post("/api/telephony/test", headers=АДМИН,
                        json=станция(recordings_dir=""))
    assert связь.status_code == 200, связь.text
    assert связь.json()["source"] == "dialplan"
    разбор = клиент.post("/api/telephony/diagnose", headers=АДМИН,
                         params={"station": "main"})
    assert разбор.status_code == 200, разбор.text
    свод = разбор.json()["stations"][0]
    assert свод["state"] == "ok" and "станция присылает" in свод["verdict"]
    проверки = {п["id"]: п for п in свод["checks"]}
    assert проверки["dialplan"]["state"] == "ok"
    assert "обращений 1" in проверки["dialplan"]["value"]
    # Журнала у такой станции нет — и разбор не жалуется на его отсутствие.
    assert "source" not in проверки and "rec_match" not in проверки


# ---------------------------------------------------------------------------
# Готовые файлы настройки
# ---------------------------------------------------------------------------

def _подставляемое(строка_: str) -> list[str]:
    """Верхнеуровневые ${…} строки — с учётом вложенных скобок."""
    итог, i = [], 0
    while (i := строка_.find("${", i)) >= 0:
        глубина, j = 0, i + 1
        while j < len(строка_):
            if строка_[j] == "{":
                глубина += 1
            elif строка_[j] == "}":
                глубина -= 1
                if not глубина:
                    break
            j += 1
        итог.append(строка_[i:j + 1])
        i = j + 1
    return итог


@pytest.mark.parametrize("драйвер", ["pjsip", "sip"])
def test_система_получает_только_отфильтрованное(драйвер):
    файлы = {ф["id"]: ф for ф in К.собрать(К.параметры(driver=драйвер, station="main"))["files"]}
    текст = файлы["dialplan"]["text"]
    команда = next(с for с in текст.splitlines() if "System(" in с)
    внутри = команда.split("System(", 1)[1].rsplit(")", 1)[0]
    свои = {"${ASRHUB_SEND}", "${ASRHUB_BASE}", "${ASRHUB_REL}"}
    for вставка in _подставляемое(внутри):
        assert вставка in свои or вставка.startswith("${FILTER("), вставка
    # Значения звонящего вне FILTER() в командную строку не попадают нигде.
    assert "CALLERID(num)" in внутри and "CALLERID(name)" not in текст
    # Фильтры не пропускают ни пробела, ни символов оболочки.
    for набор in re.findall(r"FILTER\(([^,]+),", внутри):
        assert not set(набор) & set(" ;&|$`'\"<>()*?[]\\!#~"), набор


@pytest.mark.parametrize("драйвер,свой,набор,транк", [
    ("pjsip", "${CHANNEL(endpoint)}", "Dial(PJSIP/${EXTEN},30,tT)",
     "Dial(PJSIP/${EXTEN}@provider,60,tT)"),
    ("sip", "${CHANNEL(peername)}", "Dial(SIP/${EXTEN},30,tT)",
     "Dial(SIP/provider/${EXTEN},60,tT)"),
])
def test_синтаксис_драйвера(драйвер, свой, набор, транк):
    собрано = К.собрать(К.параметры(driver=драйвер, station="main"))
    файлы = {ф["id"]: ф for ф in собрано["files"]}
    assert свой in файлы["dialplan"]["text"]
    assert набор in файлы["contexts"]["text"] and транк in файлы["contexts"]["text"]
    чужой = "${CHANNEL(peername)}" if драйвер == "pjsip" else "${CHANNEL(endpoint)}"
    assert чужой not in файлы["dialplan"]["text"]
    assert "setinterfacevar = yes" in файлы["queues"]["text"]
    assert ("member => PJSIP/101" if драйвер == "pjsip" else "member => SIP/101") \
        in файлы["queues"]["text"]
    assert {"install", "script_conf", "freepbx", "check"} <= set(файлы)
    assert len(собрано["differences"]) >= 8
    if драйвер == "sip":
        assert "удалён в 21" in собрано["tips"][0]


def test_curl_путём_без_оболочки():
    собрано = К.собрать(К.параметры(mode="path", station="main", url="https://asr.local"))
    текст = {ф["id"]: ф for ф in собрано["files"]}["dialplan"]["text"]
    assert "System(" not in текст and "ASRHUB_SEND" not in текст
    вызов = next(с for с in текст.splitlines() if "${CURL(" in с)
    # Значения снаружи — только через URIENCODE(): запятая разломала бы CURL().
    assert "URIENCODE(${CALLERID(num)})" in вызов
    assert вызов.count("${CALLERID(num)}") == вызов.count("URIENCODE(${CALLERID(num)})")
    assert "CURLOPT(httpheader)=X-API-Key: ${ASRHUB_KEY}" in текст
    assert "ASRHUB_URL=https://asr.local/api/telephony/dialplan/call?station=main" in текст
    по_адресу = {ф["id"]: ф for ф in К.собрать(К.параметры(
        mode="path", station="main", key_in="query"))["files"]}["dialplan"]["text"]
    assert "CURLOPT(httpheader)" not in по_адресу and "&api_key=" in по_адресу


def test_негодные_параметры_настройки(клиент):
    for плохое in ({"url": "http://a\n[globals]"}, {"station": "a b"},
                   {"base": "/var/../etc"}, {"driver": "iax"}, {"mode": "ftp"}):
        ответ = клиент.get("/api/telephony/dialplan/config", headers=СТАНЦИЯ, params=плохое)
        assert ответ.status_code == 400, плохое
    ответ = клиент.get("/api/telephony/dialplan/config", headers=СТАНЦИЯ,
                       params={"driver": "sip", "station": "main"})
    assert ответ.status_code == 200 and ответ.json()["driver"] == "sip"
    # Адрес по умолчанию — тот, по которому пришёл запрос.
    assert ответ.json()["endpoint"] == f"http://testserver{АДРЕС}"


def test_сценарий_отдаётся_с_подстановками(клиент):
    ответ = клиент.get("/api/telephony/dialplan/asrhub-dialplan", headers=СТАНЦИЯ,
                       params={"station": "main"})
    assert ответ.status_code == 200
    assert 'ASRHUB_URL="http://testserver"' in ответ.text
    assert 'ASRHUB_STATION="main"' in ответ.text
    assert not re.search(r"@@(SERVER_URL|STATION|VERSION)@@", ответ.text)
    assert клиент.get("/api/telephony/dialplan/asrhub-dialplan", headers=СТАНЦИЯ,
                      params={"station": "a;b"}).status_code == 400
    assert клиент.get("/api/telephony/dialplan/asrhub-dialplan").status_code == 401


# ---------------------------------------------------------------------------
# Сценарий отправки — настоящий sh против настоящего сервера
# ---------------------------------------------------------------------------

@pytest.fixture()
def латиница():
    """Каталог с путём только из латиницы: сценарий отбрасывает значения с
    другими символами (так безопаснее для командной строки), а в имени
    каталога pytest — русское имя проверки."""
    import shutil
    import tempfile

    путь = Path(tempfile.mkdtemp(prefix="asrhub-dp-"))
    (путь / "monitor" / "asrhub").mkdir(parents=True)
    yield путь
    shutil.rmtree(путь, ignore_errors=True)


@pytest.fixture()
def живой(data_dir, monkeypatch, латиница):
    from drill_seed import поднять

    app = приложение(data_dir, monkeypatch,
                     [станция(recordings_dir=str(латиница / "monitor"))])
    адрес, остановить = поднять(app)
    yield адрес, app.state.hub, латиница
    остановить()


def запустить(tmp_path: Path, адрес: str, *аргументы: str, ключ: str = "ah_pbx",
              фон: bool = False, **окружение) -> subprocess.CompletedProcess:
    настройки = tmp_path / "dialplan.conf"
    настройки.write_text(
        f"ASRHUB_URL={адрес}\nASRHUB_KEY={ключ}\nASRHUB_STATION=main\n"
        f"ASRHUB_SPOOL={tmp_path / 'spool'}\nASRHUB_LOG={tmp_path / 'send.log'}\n"
        f"ASRHUB_RECORDINGS={tmp_path / 'monitor'}\n"
        "ASRHUB_WAIT_S=5\nASRHUB_RETRIES=1\n", encoding="utf-8")
    env = {**os.environ, "ASRHUB_CONF": str(настройки),
           "ASRHUB_FOREGROUND": "0" if фон else "1", **окружение}
    return subprocess.run(["sh", str(СЦЕНАРИЙ), *аргументы], env=env, capture_output=True,
                          text=True, timeout=60)


def test_сценарий_доставляет_и_отбрасывает_опасное(живой):
    адрес, hub, tmp_path = живой
    каталог = tmp_path / "monitor"
    запись = каталог / "asrhub" / "1793000000.40.wav"
    запись.write_bytes(wav())
    след = tmp_path / "pwned"
    итог = запустить(tmp_path, адрес, "uniqueid=1793000000.40", "direction=in",
                     f"src=1;touch {след}", "dst=74950000000", "agent=102",
                     "billsec=30", "disposition=ANSWERED", f"file={запись}",
                     "src2=@/etc/passwd")
    assert итог.returncode == 0, итог.stderr
    з = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.40'")
    assert з is not None and з["job_id"] and з["agent"] == "102"
    # Значение с «;» и пробелом отброшено целиком, команда не выполнена.
    assert з["src"] == "" and not след.exists()
    журнал = (tmp_path / "send.log").read_text(encoding="utf-8")
    assert "недопустимые символы" in журнал and "принят" in журнал
    assert not list((tmp_path / "spool").rglob("*.call"))


def test_сценарий_проба_и_имя_файла_freepbx(живой):
    адрес, hub, tmp_path = живой
    каталог = tmp_path / "monitor"
    итог = запустить(tmp_path, адрес, "--test")
    assert итог.returncode == 0 and "проверка пройдена" in итог.stdout, итог.stdout + итог.stderr
    assert hub.db.query_one("SELECT COUNT(*) AS n FROM calls")["n"] == 0
    # FreePBX: путь относительный, направление — по началу имени файла.
    имя = "q-7000-79161234567-20261027-101500-1793000000.41.wav"
    (каталог / имя).write_bytes(wav())
    итог = запустить(tmp_path, адрес, "ready=1", "uniqueid=1793000000.41",
                     "src=79161234567", f"file={имя}")
    assert итог.returncode == 0, итог.stderr
    з = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.41'")
    assert з["direction"] == "входящий" and з["job_id"] and з["billsec"] == 6


def test_сценарий_копит_очередь_повтора_и_досылает(живой):
    адрес, hub, tmp_path = живой
    каталог = tmp_path / "monitor"
    запись = каталог / "asrhub" / "1793000000.42.wav"
    запись.write_bytes(wav())
    # Сервер недоступен: звонок остаётся в очереди повтора.
    итог = запустить(tmp_path, "http://127.0.0.1:9", "ready=1",
                     "uniqueid=1793000000.42", "billsec=30", f"file={запись}")
    assert итог.returncode != 0
    assert len(list((tmp_path / "spool" / "pending").glob("*.call"))) == 1
    # Неверный ключ — отказ окончательный: в failed/ вместе с ответом сервера.
    итог = запустить(tmp_path, адрес, "ready=1", "uniqueid=1793000000.43",
                     "billsec=30", f"file={запись}", ключ="ah_wrong")
    assert len(list((tmp_path / "spool" / "failed").glob("*.call"))) == 1
    assert list((tmp_path / "spool" / "failed").glob("*.answer"))
    # Сервер вернулся — --resend досылает очередь, --resend-failed — отвергнутое.
    итог = запустить(tmp_path, адрес, "--resend")
    assert "дослано 1, осталось 0" in итог.stdout, итог.stdout + итог.stderr
    итог = запустить(tmp_path, адрес, "--resend-failed")
    assert "дослано 1, осталось 0" in итог.stdout
    for uid in ("1793000000.42", "1793000000.43"):
        assert hub.db.query_one("SELECT job_id FROM calls WHERE uniqueid=?",
                                (f"main:{uid}",))["job_id"]
    assert not list((tmp_path / "spool").rglob("*.call"))


def test_сценарий_уходит_в_фон_и_ждёт_закрытия_файла(живой):
    адрес, hub, tmp_path = живой
    каталог = tmp_path / "monitor"
    запись = каталог / "asrhub" / "1793000000.44.wav"
    запись.write_bytes(wav())
    начало = time.monotonic()
    итог = запустить(tmp_path, адрес, "uniqueid=1793000000.44", "billsec=30",
                     f"file={запись}", фон=True)
    # Asterisk ждёт только сам вызов — он возвращается сразу, а отправка
    # идёт в фоне после того, как размер файла перестал меняться.
    assert итог.returncode == 0 and time.monotonic() - начало < 2
    for _ in range(100):
        з = hub.db.query_one("SELECT job_id FROM calls WHERE uniqueid='main:1793000000.44'")
        if з and з["job_id"]:
            break
        time.sleep(0.2)
    assert з and з["job_id"]


def test_глава_31_показывает_то_же_что_раздел():
    """Файлы настройки в документации — ровно те, что собирает раздел.

    Расхождение между инструкцией и тем, что показывает сервер, — это
    ровно та ошибка, из-за которой у человека «по инструкции не работает».
    Блоки главы пересобирает `python3 docs/generate.py`.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("docs_generate", КОРЕНЬ / "docs" / "generate.py")
    модуль = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(модуль)
    глава = (КОРЕНЬ / "docs" / "31-asterisk-dialplan.md").read_text(encoding="utf-8")
    assert len(модуль.ДИАЛПЛАН_ПОМЕТКА.findall(глава)) >= 10
    assert модуль.dialplan_blocks(глава) == глава, \
        "блоки главы 31 устарели: выполните python3 docs/generate.py"
    # Таблица различий драйверов в главе — та же, что в разделе.
    без_кода = глава.replace("`", "")
    for что, _sip, _pjsip in К.РАЗЛИЧИЯ:
        assert что in без_кода, что


def test_предел_загрузки_и_проба_известного_звонка(клиент):
    """Запись больше предела — 413 с понятным текстом, и файла на диске не
    остаётся. Проба уже принятого звонка — проба, а не «повтор» в
    счётчиках приёма."""
    было = загрузки(клиент)
    клиент.hub.settings.set("max_upload_mb", 1)
    try:
        большой = прислать(клиент, "1793000000.51", файл=("r.wav", wav(80)))
        assert большой.status_code == 413 and "1 МБ" in большой.json()["message"]
        assert загрузки(клиент) == было
        assert прислать(клиент, "1793000000.50", файл=("r.wav", wav(6))).json()["result"] \
            == "queued"
    finally:
        клиент.hub.settings.set("max_upload_mb", 2048)
    проба = прислать(клиент, "1793000000.50", dry_run="1").json()
    assert проба["result"] == "dry_run" and проба["reason"] == "уже импортирован"
    assert Д.журнал.свод("main").get("duplicate", 0) == 0
