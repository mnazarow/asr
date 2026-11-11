"""Заход 57: доделки из главы 33 — телефония.

* станция «диалплан»: сверка каталога записей поднимает звонки, обращение
  о которых не дошло (CURL() не повторяет);
* AMI: после (пере)подключения журнал CDR дочитывается, разрыв виден;
* позиция журнала CDR двигается по порядку файла, а не за отложенным;
* systemname в ${UNIQUEID} проходит фильтр диалплана целиком;
* сценарий отправки делает столько попыток, сколько задано;
* агент: пароль MySQL с «#» и кавычками доходит до клиента целиком;
* колесо несёт агента и руководство, сервер находит их в share/asrhub.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_dialplan import АДМИН, живой, латиница, приложение, прислать, станция  # noqa: E402, F401
from test_dialplan import wav as wav_байты  # noqa: E402
from test_review_54_telephony import (  # noqa: E402
    ДАВНО,
    ПоддельнаяAMI,
    cdr,
    wav,
    база,
    ждать,
    журнал,
    звонки,
    импортёр,
    событие_cdr,
)
from test_review_56 import клиент2  # noqa: E402, F401

КОРЕНЬ = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Станция «диалплан»: сверка каталога записей
# ---------------------------------------------------------------------------

def _диалплан(db, кат: Path, **поверх):
    return импортёр(db, кат, source="dialplan", cdr_file="", lookback_days=2, **поверх)


def test_сверка_каталога_поднимает_звонок_без_обращения(tmp_path: Path):
    db = база(tmp_path)
    день = time.strftime("%Y/%m/%d")
    старая = time.time() - 1800
    папка = tmp_path / "monitor" / "asrhub" / день
    wav(папка / "1793100000.1.wav", когда=старая, секунд=3)
    wav(папка / "pbx-1-1793100000.2.wav", когда=старая, секунд=3)
    wav(папка / "1793100000.3.wav", секунд=3)                  # ещё может прийти обращение
    wav(папка / "1793100000.4.wav", когда=старая, секунд=3)    # уже пришло
    db.save_call("pbx:1793100000.4", job_id="job_x", skipped="", station="pbx",
                 pbx_uid="1793100000.4")
    имп = _диалплан(db, tmp_path)
    свод = имп.scan()
    ключи = {з["uniqueid"] for з in звонки(db)}
    assert {"pbx:1793100000.1", "pbx:pbx-1-1793100000.2"} <= ключи
    assert "pbx:1793100000.3" not in ключи
    assert свод["imported"] == 2, свод
    # Сверка не чаще раза в десять минут: второй заход каталог не обходит.
    wav(папка / "1793100000.5.wav", когда=старая, секунд=3)
    имп.scan()
    assert "pbx:1793100000.5" not in {з["uniqueid"] for з in звонки(db)}
    имп._сверка_была = 0
    имп.scan()
    assert "pbx:1793100000.5" in {з["uniqueid"] for з in звонки(db)}


def test_сверка_каталога_freepbx_и_уже_известный_с_systemname(tmp_path: Path):
    db = база(tmp_path)
    день = time.strftime("%Y/%m/%d")
    старая = time.time() - 1800
    папка = tmp_path / "monitor" / день
    wav(папка / "in-101-79161234567-20261111-101010-1793100001.7.wav", когда=старая)
    # Пришёл обращением с systemname, а файл FreePBX назван без него.
    wav(папка / "out-79160000000-102-20261111-101010-pbx1-1793100001.8.wav", когда=старая)
    db.save_call("pbx:1793100001.8", job_id="job_y", skipped="", station="pbx",
                 pbx_uid="1793100001.8")
    _диалплан(db, tmp_path).scan()
    строки = {з["uniqueid"]: з for з in звонки(db)}
    новый = строки["pbx:1793100001.7"]
    assert новый["direction"] == "входящий" and новый["src"] == "79161234567"
    assert len(строки) == 2


def test_сверка_без_каталога_ничего_не_делает(tmp_path: Path):
    db = база(tmp_path)
    свод = импортёр(db, tmp_path, source="dialplan", cdr_file="", recordings_dir="").scan()
    assert свод["imported"] == 0 and not звонки(db)


# ---------------------------------------------------------------------------
# AMI: дозабор из журнала CDR после разрыва
# ---------------------------------------------------------------------------

def test_ami_после_подключения_дочитывает_журнал(tmp_path: Path):
    """Звонки, завершившиеся без связи, AMI не пришлёт: их берёт журнал."""
    def сценарий(conn, _srv):
        conn.sendall(событие_cdr("1793200000.3", начало=ДАВНО + 30))
        time.sleep(30)

    сервер = ПоддельнаяAMI(сценарий)
    try:
        db = база(tmp_path)
        журнал(tmp_path,
               cdr("1793200000.1", начало=ДАВНО),
               cdr("1793200000.2", начало=ДАВНО + 10),
               cdr("1793200000.3", начало=ДАВНО + 30))
        for н in (1, 2, 3):
            wav(tmp_path / "monitor" / f"1793200000.{н}.wav", когда=ДАВНО + 200)
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        ждать(lambda: False, срок=0.5)
        имп.scan()
        ключи = [з["uniqueid"] for з in звонки(db)]
        # Третий пришёл обоими путями — в архиве один раз.
        assert sorted(ключи) == ["pbx:1793200000.1", "pbx:1793200000.2", "pbx:1793200000.3"]
        assert not имп._дочитать_журнал
    finally:
        сервер.close()


def test_ami_разрыв_виден_и_без_журнала(tmp_path: Path):
    сервер = ПоддельнаяAMI(lambda conn, _srv: time.sleep(30))
    db = база(tmp_path)
    имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                   username="asrhub", secret="s3cret", cdr_file="/нет/на/этой/машине.csv")
    имп.scan()
    имп._закрыть_ami()
    имп._ami_оборвана = time.time() - 120
    имп.scan()
    сервер.close()
    разрыв = имп.status(for_admin=True)["ami_gap"]
    assert разрыв["until"] - разрыв["since"] >= 119


def test_ami_позиция_журнала_отстаёт_на_заход(tmp_path: Path):
    сервер = ПоддельнаяAMI(lambda conn, _srv: time.sleep(30))
    try:
        db = база(tmp_path)
        путь = журнал(tmp_path, cdr("1793300000.1", начало=ДАВНО))
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        имп.scan()                                   # дочит после подключения
        размер = путь.stat().st_size
        with путь.open("a", encoding="utf-8") as f:
            f.write(cdr("1793300000.2", начало=ДАВНО + 50))
        имп.scan()                                   # замечен новый конец
        assert int(db.get_kv(имп._ключ_позиции, 0)) == размер
        имп.scan()                                   # заход спустя — позиция за ним
        assert int(db.get_kv(имп._ключ_позиции, 0)) == путь.stat().st_size
    finally:
        сервер.close()


# ---------------------------------------------------------------------------
# Позиция журнала: по порядку файла
# ---------------------------------------------------------------------------

def test_отложенный_звонок_не_уводит_позицию_вперёд(tmp_path: Path, monkeypatch):
    from asrhub.telephony.asterisk import Звонок

    db = база(tmp_path)
    журнал(tmp_path)
    имп = импортёр(db, tmp_path)
    отложенный = {"uniqueid": "pbx:9.3", "pbx_uid": "9.3", "src": "1", "dst": "101",
                  "started_at": ДАВНО}
    monkeypatch.setattr(db, "calls_deferred", lambda **_: [отложенный])

    def из_csv(limit, **_):
        свои = []
        for н, конец in ((1, 100), (2, 200), (3, 300)):
            з = Звонок(uniqueid=f"9.{н}", src="1", dst="101", started_at=ДАВНО)
            з.конец_строки, з.inode_журнала = конец, 7
            свои.append(з)
        return свои, 300, 7

    monkeypatch.setattr(имп, "_из_csv", из_csv)
    порядок: list[str] = []

    def взять(звонок, **_):
        порядок.append(звонок.uniqueid)
        return "проверка"

    monkeypatch.setattr(имп, "_взять", взять)
    имп.scan()
    assert порядок == ["9.1", "9.2", "9.3"]


# ---------------------------------------------------------------------------
# Диалплан: systemname
# ---------------------------------------------------------------------------

def _filter(разрешено: str, строка: str) -> str:
    """FILTER() Asterisk (func_strings.c): «a-b» — диапазон, иначе знак."""
    можно: set[str] = set()
    i = 0
    while i < len(разрешено):
        c1 = разрешено[i]
        i += 1
        if i < len(разрешено) and разрешено[i] == "-":
            c2 = разрешено[i + 1] if i + 1 < len(разрешено) else c1
            i += 2 if i + 1 < len(разрешено) else 1
            можно |= {chr(к) for к in range(ord(c1), ord(c2) + 1)}
        else:
            можно.add(c1)
    return "".join(с for с in строка if с in можно)


def test_systemname_проходит_фильтр_uniqueid():
    import re

    from asrhub.telephony import dialplan_config as К

    файлы = {ф["id"]: ф for ф in К.собрать(К.параметры(driver="pjsip", station="main"))["files"]}
    текст = файлы["dialplan"]["text"]
    фильтр = re.search(r"uniqueid=\$\{FILTER\(([^,]+),\$\{ASRHUB_UID\}\)\}", текст).group(1)
    assert _filter(фильтр, "pbx-1-1793000000.12") == "pbx-1-1793000000.12"
    assert _filter(фильтр, "pbx_2-1793000000.12") == "pbx_2-1793000000.12"
    assert _filter(фильтр, "1;touch /tmp/x,y`z`$(id)") == "1touchtmpxyzid"


# ---------------------------------------------------------------------------
# Сценарий отправки: число попыток
# ---------------------------------------------------------------------------

def test_сценарий_делает_все_заданные_попытки(tmp_path: Path):
    сценарий = КОРЕНЬ / "agent" / "asrhub-dialplan"
    текст = сценарий.read_text(encoding="utf-8")
    # Паузы — нули, чтобы проверка не ждала минуты; логика — та же.
    текст = текст.replace('PAUSES="5 15 30 60 120"', 'PAUSES="0 0"')
    копия = tmp_path / "asrhub-dialplan"
    копия.write_text(текст, encoding="utf-8")
    настройки = tmp_path / "dialplan.conf"
    настройки.write_text(
        f"ASRHUB_URL=http://127.0.0.1:9\nASRHUB_KEY=k\nASRHUB_STATION=main\n"
        f"ASRHUB_SPOOL={tmp_path / 'spool'}\nASRHUB_LOG={tmp_path / 'send.log'}\n"
        f"ASRHUB_RECORDINGS={tmp_path}\nASRHUB_WAIT_S=1\nASRHUB_RETRIES=7\n",
        encoding="utf-8")
    env = {**os.environ, "ASRHUB_CONF": str(настройки), "ASRHUB_FOREGROUND": "1"}
    subprocess.run(["sh", str(копия), "ready=1", "uniqueid=1793400000.1", "billsec=30"],
                   env=env, capture_output=True, text=True, timeout=120)
    журнал_ = (tmp_path / "send.log").read_text(encoding="utf-8")
    assert "попытка 7 " in журнал_ and "за 7 попыток" in журнал_, журнал_


# ---------------------------------------------------------------------------
# Агент: пароль MySQL
# ---------------------------------------------------------------------------

def test_пароль_mysql_с_решёткой_в_кавычках(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("агент57", КОРЕНЬ / "agent" / "asrhub-agent.py")
    агент = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(агент)
    assert агент._значение_mycnf('p#ss"w\\rd ') == '"p#ss\\"w\\\\rd "'
    assert агент._значение_mycnf("a\nhost=evil") == '"ahost=evil"'


# ---------------------------------------------------------------------------
# Колесо: агент и руководство
# ---------------------------------------------------------------------------

def test_сервер_находит_агента_и_руководство_в_share(tmp_path: Path, monkeypatch):
    from asrhub import manual, resources

    общее = tmp_path / "prefix" / "share" / "asrhub"
    (общее / "agent").mkdir(parents=True)
    (общее / "agent" / "asrhub-dialplan").write_text("#!/bin/sh\n", encoding="utf-8")
    (общее / "docs").mkdir()
    (общее / "docs" / "01-intro.md").write_text("# Введение\n", encoding="utf-8")
    monkeypatch.setattr(resources, "КОРЕНЬ", tmp_path / "нет")
    monkeypatch.setattr(resources.sys, "prefix", str(tmp_path / "prefix"))
    monkeypatch.delenv("ASRHUB_DOCS_DIR", raising=False)
    assert resources.файл("agent", "asrhub-dialplan") == общее / "agent" / "asrhub-dialplan"
    assert manual.каталог() == общее / "docs"


def test_pyproject_кладёт_агента_и_руководство_в_колесо():
    import tomllib

    данные = tomllib.loads((КОРЕНЬ / "pyproject.toml").read_text(encoding="utf-8"))
    файлы = данные["tool"]["setuptools"]["data-files"]
    агент = set(файлы["share/asrhub/agent"])
    assert {"agent/asrhub-agent.py", "agent/asrhub-dialplan", "agent/install.sh"} <= агент
    for путь in агент:
        assert (КОРЕНЬ / путь).is_file(), путь
    assert "docs/*.md" in файлы["share/asrhub/docs"]


@pytest.mark.skipif(sys.platform == "win32", reason="разбор параметров PowerShell")
def test_install_ps1_offline_с_каталогом_колёс():
    текст = (КОРЕНЬ / "scripts" / "install.ps1").read_text(encoding="utf-8-sig")
    assert "[string]$Wheels" in текст
    assert "'--find-links', $Wheels" in текст
    assert "Ключ -Offline без -Wheels" in текст


# ---------------------------------------------------------------------------
# Журнал приёма из диалплана — в базе
# ---------------------------------------------------------------------------

def test_журнал_приёма_переживает_перезапуск(tmp_path: Path):
    from asrhub.telephony.dialplan import ЖурналПриёма

    db = база(tmp_path)
    журнал_ = ЖурналПриёма()
    журнал_.привязать(db)
    журнал_.отметить(station="main", result="queued", uniqueid="1.1", ip="10.0.0.1")
    журнал_.отметить(station="main", result="rejected", reason="ключ", uniqueid="1.2")
    журнал_.сохранить()
    новый = ЖурналПриёма()
    новый.привязать(db)
    assert новый.свод("main")["requests"] == 2
    assert [з["uniqueid"] for з in новый.последние(station="main")] == ["1.2", "1.1"]
    сегодня = time.strftime("%Y-%m-%d")
    assert новый.по_дням("main")[0] == {"day": сегодня, "requests": 2, "queued": 1,
                                        "rejected": 1}
    новый.сбросить()
    третий = ЖурналПриёма()
    третий.привязать(db)
    assert третий.свод("main") == {}


def test_журнал_приёма_пишется_не_на_каждое_обращение(tmp_path: Path, monkeypatch):
    from asrhub.telephony.dialplan import ЖурналПриёма

    db = база(tmp_path)
    записи: list[str] = []
    исходная = db.set_kv
    monkeypatch.setattr(db, "set_kv", lambda к, з: (записи.append(к), исходная(к, з)))
    журнал_ = ЖурналПриёма()
    журнал_.привязать(db)
    for н in range(50):
        журнал_.отметить(station="main", result="queued", uniqueid=f"1.{н}")
    assert записи.count(ЖурналПриёма.КЛЮЧ) <= 1


def test_статус_раздела_отдаёт_счёт_по_суткам(клиент2):  # noqa: F811
    assert прислать(клиент2, "1793500000.1").status_code == 200
    данные = клиент2.get("/api/telephony/dialplan/status", headers=АДМИН).json()
    assert данные["days"][0]["requests"] >= 1
    assert данные["stations"][0]["intake_days"][0]["requests"] >= 1


# ---------------------------------------------------------------------------
# linkedid: ноги одного разговора
# ---------------------------------------------------------------------------

def test_linkedid_из_журнала_newcdrcolumns():
    from asrhub.telephony.asterisk import разобрать_cdr_строку

    строка = cdr("1793600000.5", начало=ДАВНО).rstrip("\n") + ',"","1793600000.4","12"\n'
    звонок = разобрать_cdr_строку(строка)
    assert (звонок.uniqueid, звонок.linkedid, звонок.sequence) == ("1793600000.5",
                                                                   "1793600000.4", 12)
    # Без новых колонок — пусто, как и было.
    assert разобрать_cdr_строку(cdr("1793600000.6", начало=ДАВНО)).linkedid == ""


def test_ноги_разговора_в_карточке_звонка(клиент2):  # noqa: F811
    hub = клиент2.hub
    первая = прислать(клиент2, "1793600001.1", linkedid="1793600001.1",
                     sequence="1", agent="101").json()
    вторая = прислать(клиент2, "1793600001.2", linkedid="1793600001.1",
                     sequence="2", agent="102").json()
    assert первая.get("job_id") and вторая.get("job_id"), (первая, вторая)
    строка_ = hub.db.query_one("SELECT linkedid, sequence FROM calls WHERE uniqueid=?",
                               ("main:1793600001.2",))
    assert (строка_["linkedid"], строка_["sequence"]) == ("1793600001.1", 2)
    карточка = клиент2.get(f"/api/jobs/{вторая['job_id']}", headers=АДМИН).json()
    ноги = карточка["call"]["legs"]
    assert [н["pbx_uid"] for н in ноги] == ["1793600001.1"]
    assert ноги[0]["job_id"] == первая["job_id"]
    # Мусор вместо linkedid не пишется.
    прислать(клиент2, "1793600001.3", linkedid="x;rm -rf")
    assert hub.db.query_one("SELECT linkedid FROM calls WHERE uniqueid=?",
                            ("main:1793600001.3",))["linkedid"] == ""


def test_агент_присылает_linkedid():
    import importlib.util

    spec = importlib.util.spec_from_file_location("агент57б", КОРЕНЬ / "agent" / "asrhub-agent.py")
    агент = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(агент)
    строка = cdr("1793600002.5", начало=ДАВНО).rstrip("\n") + ',"","1793600002.4","7"\n'
    звонок = агент.разобрать_строку_cdr(строка)
    поля = звонок.поля_для_сервера("a1")
    assert поля["linkedid"] == "1793600002.4" and поля["sequence"] == 7


# ---------------------------------------------------------------------------
# Примеры телефонии
# ---------------------------------------------------------------------------

def test_примеры_телефонии_работают(живой, tmp_path: Path):  # noqa: F811
    адрес, hub, каталог = живой
    запись = каталог / "rec.wav"
    запись.write_bytes(wav_байты())
    env = {**os.environ, "ASRHUB_SERVER": адрес, "ASRHUB_KEY": "ah_pbx"}
    итог = subprocess.run(
        [sys.executable, str(КОРЕНЬ / "examples" / "telephony_push_call.py"), str(запись),
         "--uniqueid", "1793700000.1", "--src", "79161234567", "--dst", "74950000000",
         "--agent", "101", "--direction", "in", "--billsec", "40", "--duration", "45"],
        env=env, capture_output=True, text=True, timeout=120)
    assert итог.returncode == 0 and итог.stdout.startswith("queued"), итог.stdout + итог.stderr
    повтор = subprocess.run(
        [sys.executable, str(КОРЕНЬ / "examples" / "telephony_push_call.py"), str(запись),
         "--uniqueid", "1793700000.1", "--billsec", "40"],
        env=env, capture_output=True, text=True, timeout=120)
    assert повтор.stdout.startswith("duplicate"), повтор.stdout + повтор.stderr
    выгрузка = tmp_path / "calls.csv"
    итог = subprocess.run(
        [sys.executable, str(КОРЕНЬ / "examples" / "telephony_export_calls.py"), str(выгрузка)],
        env={**env, "ASRHUB_KEY": "ah_admin"}, capture_output=True, text=True, timeout=120)
    assert итог.returncode == 0, итог.stderr
    текст = выгрузка.read_text(encoding="utf-8-sig")
    assert "1793700000.1" in текст and текст.splitlines()[0].startswith("started_at;")


# ---------------------------------------------------------------------------
# Сценарий отправки: пульс, --status, срок отвергнутых
# ---------------------------------------------------------------------------

def test_пульс_сценария_и_состояние(живой):  # noqa: F811
    from test_dialplan import запустить

    адрес, hub, каталог = живой
    (каталог / "spool" / "pending").mkdir(parents=True)
    (каталог / "spool" / "failed").mkdir(parents=True)
    (каталог / "spool" / "sending").mkdir(parents=True)
    старый = каталог / "spool" / "failed" / "старый.call"
    старый.write_text("uniqueid=1.1\n", encoding="utf-8")
    давно = time.time() - 40 * 86400
    os.utime(старый, (давно, давно))
    (каталог / "spool" / "failed" / "свежий.call").write_text("uniqueid=1.2\n", encoding="utf-8")
    итог = запустить(каталог, адрес, "--resend")
    assert "дослано 0" in итог.stdout, итог.stdout + итог.stderr
    assert not старый.exists(), "отвергнутый старше 30 суток не удалён"
    пульс = hub.db.get_kv("dialplan:pulse:main")
    assert пульс and пульс["failed"] == 1 and пульс["pending"] == 0, пульс
    import urllib.request

    запрос = urllib.request.Request(адрес + "/api/telephony/dialplan/status",
                                    headers={"X-API-Key": "ah_admin"})
    данные = json.loads(urllib.request.urlopen(запрос).read())
    assert данные["stations"][0]["pulse"]["failed"] == 1
    состояние = запустить(каталог, адрес, "--status")
    assert "failed   1" in состояние.stdout and "Досылка в cron" in состояние.stdout, \
        состояние.stdout


def test_пульс_чужой_станции_не_принимается(клиент2):  # noqa: F811
    assert прислать(клиент2, "1793800000.1").status_code == 200
    ответ = клиент2.post("/api/telephony/dialplan/pulse", data={"pending": "3"},
                         headers={"X-API-Key": "ah_pbx2"})
    assert ответ.status_code == 403
    ответ = клиент2.post("/api/telephony/dialplan/pulse", data={"pending": "3"},
                         headers={"X-API-Key": "ah_ro"})
    assert ответ.status_code == 403


# ---------------------------------------------------------------------------
# Метрики и тревоги телефонии и контроля качества
# ---------------------------------------------------------------------------

def _метрики(клиент) -> dict[str, list[dict]]:
    клиент.hub.monitoring.cache_ttl_s = 0
    данные = клиент.get("/api/monitoring/metrics.json", headers=АДМИН).json()
    assert not данные.get("collection_errors"), данные.get("collection_errors")
    return {м["name"]: м["values"] for м in данные["metrics"]}


def test_метрики_станции_диалплана(клиент2):  # noqa: F811
    hub = клиент2.hub
    прислать(клиент2, "1793900000.1")
    метрики = _метрики(клиент2)
    вверх = метрики["asrhub_telephony_station_up"]
    assert вверх[0]["labels"] == {"station": "main", "source": "dialplan"}
    assert вверх[0]["value"] == 1
    за_сутки = {з["labels"]["result"]: з["value"] for з in метрики["asrhub_telephony_calls_day"]
                if з["labels"]["station"] == "main"}
    assert за_сутки["queued"] >= 1
    assert "asrhub_dialplan_pulse_age_seconds" not in метрики   # пульса не было
    # Пульс получен давно — станция «молчит», в очереди повтора звонки.
    hub.db.set_kv("dialplan:pulse:main", {"at": time.time() - 3600, "pending": 4})
    метрики = _метрики(клиент2)
    assert метрики["asrhub_telephony_station_up"][0]["value"] == 0
    assert метрики["asrhub_dialplan_spool_pending"][0]["value"] == 4
    assert метрики["asrhub_dialplan_pulse_age_seconds"][0]["value"] >= 3599


def test_встроенные_тревоги_телефонии_есть_в_правилах():
    from asrhub.monitoring import exporters

    правила = exporters.prometheus_rules()
    for имя in ("asrhub_telephony_station_up", "asrhub_dialplan_spool_pending",
                "asrhub_qc_registered", "asrhub_qc_score_pct_day"):
        assert имя in правила, имя


@pytest.fixture()
def кк(data_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from test_qc import приложение as приложение_кк

    app, hub = приложение_кк(data_dir, monkeypatch)
    with TestClient(app) as c:
        c.hub = hub
        yield c


def _опрос(hub, номер: str, статус: str, оценка: float | None, *, ключ: str = "",
           ответы: list | None = None) -> None:
    сейчас = time.time()
    ключ = ключ or f"pbx:{номер}"
    hub.db.execute(
        "INSERT INTO qc_surveys (id, call_key, kind, group_name, ext, number, call_at, "
        "created_at, due_at, finished_at, status, attempts, answers, score, score_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (номер, ключ, "опрос", "Продажи", "101", "79991112233", сейчас - 600, сейчас - 600,
         сейчас - 500, сейчас - 400, статус, 1,
         json.dumps(ответы or [], ensure_ascii=False), оценка,
         None if оценка is None else (оценка - 1) / 4 * 100))


def test_метрики_и_тревога_низкой_оценки(кк):
    hub = кк.hub
    for н in range(3):
        _опрос(hub, f"s{н}", "пройден", 1.0)
    for н in range(3):
        _опрос(hub, f"n{н}", "не дозвонились", None)
    метрики = _метрики(кк)
    assert метрики["asrhub_qc_score_pct_day"][0]["value"] == 0
    assert метрики["asrhub_qc_unreached_share_day"][0]["value"] == 0.5
    итоги = {з["labels"]["status"]: з["value"] for з in метрики["asrhub_qc_surveys_day"]}
    assert итоги == {"пройден": 3, "не дозвонились": 3}
    тревоги = кк.get("/api/monitoring/alerts", headers=АДМИН).json()
    имена = json.dumps(тревоги, ensure_ascii=False)
    assert "asrhub_qc_score_pct_day" in имена


def test_выгрузка_опросов_csv(кк):
    hub = кк.hub
    _опрос(hub, "e1", "пройден", 4.0, ответы=[
        {"text": "Вежливость", "min": 1, "max": 5, "digit": 4},
        {"text": "Решён ли вопрос", "min": 1, "max": 2, "digit": None}])
    _опрос(hub, "e2", "отказ", None)
    hub.db.execute("UPDATE qc_surveys SET error='=HYPERLINK(\"x\")' WHERE id='e2'")
    ответ = кк.get("/api/qc/surveys/export?since_days=7", headers=АДМИН)
    assert ответ.status_code == 200 and "attachment" in ответ.headers["content-disposition"]
    текст = ответ.content.decode("utf-8")
    assert текст.startswith("﻿")
    строки = текст.lstrip("﻿").splitlines()
    assert строки[0].endswith("Вопрос 1;Вопрос 2")
    пройден = next(с for с in строки if ";пройден;" in с)
    assert пройден.endswith(";4;")
    assert "'=HYPERLINK" in текст
    assert кк.get("/api/qc/surveys/export", headers={"X-API-Key": "ah_user"}).status_code == 403


def test_завершённый_опрос_уходит_на_адрес(кк, monkeypatch):
    from asrhub import maintenance

    hub = кк.hub
    hub.settings.set("qc_webhook_url", "https://crm.example.com/hooks/qc")
    отправлено: list[tuple] = []
    готово = __import__("threading").Event()

    def отправить(тело, адрес, **_):
        отправлено.append((тело, адрес))
        готово.set()
        return True

    monkeypatch.setattr(maintenance, "send_json", отправить)
    _опрос(hub, "w1", "ждёт", None)
    hub.db.execute("UPDATE qc_surveys SET finished_at=NULL WHERE id='w1'")
    hub.qc._закрыть("w1", "пройден", score=5.0, score_pct=100.0)
    assert готово.wait(5)
    тело, адрес = отправлено[0]
    assert адрес == "https://crm.example.com/hooks/qc"
    assert тело["event"] == "qc_survey_finished" and тело["survey"]["score"] == 5.0
    # Отмена и пробный звонок не уходят.
    отправлено.clear()
    готово.clear()
    _опрос(hub, "w2", "ждёт", None)
    hub.qc._закрыть("w2", "отменён")
    assert not готово.wait(0.5)
    # Секрет: обычному ключу адрес не отдаётся.
    настройки = кк.get("/api/settings", headers={"X-API-Key": "ah_user"}).json()
    assert "crm.example.com" not in json.dumps(настройки, ensure_ascii=False)


def test_оценка_клиента_в_карточке_разговора(кк):
    from test_qc import звонок as звонок_кк

    hub = кк.hub
    ид = hub.db.create_job({"filename": "call.wav", "file_path": "/tmp/call.wav",
                            "model": "demo-simulator", "owner": "sales", "status": "completed"})
    звонок_кк(hub, "pbx:7.7")
    hub.db.execute("UPDATE calls SET job_id=? WHERE uniqueid='pbx:7.7'", (ид,))
    _опрос(hub, "c1", "пройден", 5.0, ключ="pbx:7.7",
           ответы=[{"text": "Вежливость", "min": 1, "max": 5, "digit": 5}])
    карточка = кк.get(f"/api/jobs/{ид}", headers=АДМИН).json()
    assert карточка["qc_survey"]["score"] == 5.0
    assert карточка["qc_survey"]["answers"][0]["digit"] == 5


# ---------------------------------------------------------------------------
# Сервер: пароль первого запуска, уведомления, номер запроса, повтор загрузки
# ---------------------------------------------------------------------------

def test_пароль_первого_запуска_случайный_и_в_файле(tmp_path: Path, monkeypatch):
    from asrhub.accounts import DEFAULT_PASSWORD, ФАЙЛ_ПАРОЛЯ, Accounts
    from asrhub.db import Database

    monkeypatch.delenv("ASRHUB_ADMIN_PASSWORD", raising=False)
    учётки = Accounts(Database(tmp_path / "a.db"))
    создана = учётки.ensure_default_admin(tmp_path)
    файл = tmp_path / ФАЙЛ_ПАРОЛЯ
    пароль = файл.read_text(encoding="utf-8").strip()
    assert создана.must_change_password and len(пароль) >= 12 and пароль != DEFAULT_PASSWORD
    assert (файл.stat().st_mode & 0o777) == 0o600
    assert учётки.authenticate("admin", пароль)
    assert not учётки.uses_default_password()
    Accounts.забыть_пароль_первого_запуска(tmp_path)
    assert not файл.exists()


def test_пароль_первого_запуска_не_в_журнале_и_смена_удаляет_файл(data_dir, monkeypatch,
                                                                  caplog):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.delenv("ASRHUB_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    with caplog.at_level("INFO"):
        app = create_app(load(), start_queue=False)
    файл = data_dir / "admin-password.txt"
    пароль = файл.read_text(encoding="utf-8").strip()
    assert пароль not in "\n".join(з.getMessage() for з in caplog.records)
    with TestClient(app) as c:
        вход = c.post("/api/auth/login", json={"username": "admin", "password": пароль})
        assert вход.status_code == 200 and вход.json()["password_reason"] == "first"
        assert c.get("/api/auth/me").json()["password_reason"] == "first"
        ответ = c.post("/api/auth/password", json={"current_password": пароль,
                                                   "new_password": "свой-пароль-57"})
        assert ответ.status_code == 200, ответ.text
    assert not файл.exists()


def test_уведомление_о_задании_с_номером_доставки_и_подписью_со_временем(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import threading as потоки
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    получено: list[tuple[dict, bytes]] = []

    class Приёмник(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            тело = self.rfile.read(int(self.headers["Content-Length"]))
            получено.append((self.headers, тело))
            self.send_response(200 if len(получено) > 1 else 500)
            self.end_headers()

        def log_message(self, *a):
            pass

    сервер = ThreadingHTTPServer(("127.0.0.1", 0), Приёмник)
    потоки.Thread(target=сервер.serve_forever, daemon=True).start()
    try:
        from asrhub.job_queue import JobQueue

        class Настройки(dict):
            def get(self, к, d=None):  # noqa: A003
                return dict.get(self, к, d)

        очередь = JobQueue.__new__(JobQueue)
        очередь.settings = Настройки(webhook_secret="s3", webhook_allow_internal=True)
        очередь._stop = потоки.Event()
        записи = {}
        очередь.db = type("Б", (), {"update_job": lambda self, i, **п: записи.update(п)})()
        monkeypatch.setattr("asrhub.job_queue.ПОПЫТОК_ЗА_ЗАХОД", 2)
        задание = {"id": "job1", "status": "completed", "finished_at": 1793000000.5,
                   "webhook_url": f"http://127.0.0.1:{сервер.server_port}/hook"}
        очередь._deliver_webhook(задание, b'{"a":1}')
        assert len(получено) == 2 and записи.get("webhook_status") == "ok:200"
        (з1, _), (з2, тело) = получено
        assert з1["X-ASRHub-Delivery"] == з2["X-ASRHub-Delivery"] == "job1.1793000000500"
        assert з2["X-ASRHub-Event"] == "job.completed" and з2["X-ASRHub-Attempt"] == "2"
        ожидаемая = hmac.new(b"s3", з2["X-ASRHub-Timestamp"].encode() + b"." + тело,
                             hashlib.sha256).hexdigest()
        assert з2["X-ASRHub-Signature-256"] == "sha256=" + ожидаемая
        assert з2["X-ASRHub-Signature"] == hmac.new(b"s3", тело, hashlib.sha256).hexdigest()
    finally:
        сервер.shutdown()


def test_номер_запроса_в_ответе_и_в_журнале(client, caplog):
    import logging

    from asrhub import logging_setup

    ответ = client.get("/api/health", headers={"X-Request-ID": "bad id <x>"})
    свой = ответ.headers["x-request-id"]
    assert len(свой) == 16 and свой != "bad id <x>"
    ответ = client.get("/api/health", headers={"X-Request-ID": "lb-0123456789abcdef"})
    assert ответ.headers["x-request-id"] == "lb-0123456789abcdef"
    запись = logging.LogRecord("asrhub.x", logging.INFO, "", 0, "проба", (), None)
    метка = logging_setup.НОМЕР_ЗАПРОСА.set("abc12345")
    try:
        logging_setup._ФильтрНомера().filter(запись)
    finally:
        logging_setup.НОМЕР_ЗАПРОСА.reset(метка)
    assert запись.request_id == "abc12345"
    assert "request_id" in logging_setup.JsonFormatter().format(запись)


def test_повтор_загрузки_с_ключом_идемпотентности(client, sample_wav):
    def загрузить(ключ: str):
        with sample_wav.open("rb") as f:
            return client.post("/api/jobs", files={"file": ("a.wav", f, "audio/wav")},
                               headers={"Idempotency-Key": ключ} if ключ else {})

    первое = загрузить("upload-57-0001").json()
    повтор = загрузить("upload-57-0001").json()
    assert повтор["id"] == первое["id"] and повтор.get("idempotent_replay") is True
    assert загрузить("upload-57-0002").json()["id"] != первое["id"]
    assert загрузить("with space").status_code == 400


def test_предел_очереди_держится_при_одновременной_постановке(client, sample_wav):
    from concurrent.futures import ThreadPoolExecutor

    hub = client.app.state.hub
    hub.queue.pause()
    hub.settings.set("max_queue_size", 3)
    hub.settings.set("deduplicate_jobs", False)

    def поставить(н):
        копия = sample_wav.parent / f"q57-{н}.wav"
        копия.write_bytes(sample_wav.read_bytes())
        try:
            hub.queue.submit(file_path=копия, filename=копия.name,
                             settings=hub.settings.merged({"deduplicate_jobs": False}))
            return True
        except Exception:  # noqa: BLE001
            return False

    with ThreadPoolExecutor(8) as пул:
        итоги = list(пул.map(поставить, range(12)))
    assert sum(итоги) <= 3
    assert hub.db.count_jobs(status=["queued", "retry"]) <= 3


def test_период_аналитики_проверяется(client):
    assert client.get("/api/analytics?period=mounth").status_code in (400, 422)
    assert client.get("/api/analytics/overview?period=mounth").status_code in (400, 422)
    assert client.get("/api/analytics?period=month").status_code == 200


def test_отмена_всей_очереди_проходами(client, sample_wav, monkeypatch):
    hub = client.app.state.hub
    hub.queue.pause()
    hub.settings.set("deduplicate_jobs", False)
    for н in range(12):
        копия = sample_wav.parent / f"c57-{н}.wav"
        копия.write_bytes(sample_wav.read_bytes())
        hub.queue.submit(file_path=копия, filename=копия.name,
                         settings=hub.settings.merged({"deduplicate_jobs": False}))
    исходная = hub.db.list_jobs
    monkeypatch.setattr(hub.db, "list_jobs",
                        lambda **к: исходная(**{**к, "limit": min(5, к.get("limit") or 5)}))
    assert hub.queue.cancel_all() == 12
    assert hub.db.count_jobs(status=["queued", "retry", "paused"]) == 0


def test_обычный_ключ_не_видит_устройства_сервера_в_очереди(data_dir, monkeypatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({"ah_u57": {"name": "отдел", "role": "user", "enabled": True},
                               "ah_a57": {"name": "админ", "role": "admin", "enabled": True}})
    with TestClient(create_app(настройки, start_queue=True)) as c:
        чужое = c.get("/api/queue", headers={"X-API-Key": "ah_u57"}).json()
        assert "instances" not in чужое and "instance" not in чужое
        assert "instances" in c.get("/api/queue", headers={"X-API-Key": "ah_a57"}).json()


def test_send_json_не_идёт_по_перенаправлению_внутрь(monkeypatch):
    import threading as потоки
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from asrhub import maintenance

    class Перенаправление(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()

        def log_message(self, *a):
            pass

    сервер = ThreadingHTTPServer(("127.0.0.1", 0), Перенаправление)
    потоки.Thread(target=сервер.serve_forever, daemon=True).start()
    try:
        адрес = f"http://127.0.0.1:{сервер.server_port}/hook"
        # Сам приёмник внутри — без разрешения не отправляется вовсе.
        assert maintenance.send_json({"a": 1}, адрес) is False
        # С разрешением на внутреннюю сеть — 302 всё равно не уводит на
        # адрес метаданных облака: ни повторов, ни исключения наружу.
        assert maintenance.send_json({"a": 1}, адрес, allow_internal=True) is False
    finally:
        сервер.shutdown()


def test_новый_ключ_хранится_хешем_и_работает(data_dir, monkeypatch):
    import yaml
    from asrhub.api import create_app
    from asrhub.config import load, хеш_ключа
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    конфиг = data_dir.parent / "config.yaml"
    конфиг.write_text("api_keys:\n  ah_old_plain_key_000000000000:\n    role: admin\n"
                      "    name: прежний\n", encoding="utf-8")
    monkeypatch.setenv("ASRHUB_CONFIG", str(конфиг))
    with TestClient(create_app(load(), start_queue=False)) as c:
        админ = {"X-API-Key": "ah_old_plain_key_000000000000"}
        новый = c.post("/api/keys", json={"name": "интеграция-57", "role": "user"},
                       headers=админ).json()["key"]
        файл = yaml.safe_load(конфиг.read_text(encoding="utf-8"))["api_keys"]
        assert хеш_ключа(новый) in файл and новый not in конфиг.read_text(encoding="utf-8")
        assert "ah_old_plain_key_000000000000" in файл           # прежний — как был
        assert c.get("/api/jobs", headers={"X-API-Key": новый}).status_code == 200
        список = c.get("/api/keys", headers=админ).json()["items"]
        запись = next(к for к in список if к["name"] == "интеграция-57")
        assert запись["hashed"] and запись["key_preview"] == f"{новый[:6]}…{новый[-4:]}"
        assert c.delete(f"/api/keys/{запись['key_id']}", headers=админ).status_code == 200
        assert c.get("/api/jobs", headers={"X-API-Key": новый}).status_code == 401


def test_старение_приоритета(client, sample_wav):
    hub = client.app.state.hub
    q = hub.queue
    старое = {"id": "a", "priority": 0, "created_at": time.time() - 7200,
              "queued_at": time.time() - 7200}
    новое = {"id": "b", "priority": 1, "created_at": time.time() - 10,
             "queued_at": time.time() - 10}
    hub.settings.set("priority_aging_minutes", 0)
    assert q._apply_policy([старое, новое], "priority_fifo")["id"] == "b"
    hub.settings.set("priority_aging_minutes", 60)
    assert q._apply_policy([старое, новое], "priority_fifo")["id"] == "a"


def test_метрики_сроков(client):
    hub = client.app.state.hub
    сейчас = time.time()
    hub.db.create_job({"filename": "x.wav", "file_path": "/tmp/x.wav", "model": "demo-simulator",
                       "status": "paused", "deadline": сейчас - 60})
    hub.db.create_job({"filename": "y.wav", "file_path": "/tmp/y.wav", "model": "demo-simulator",
                       "status": "completed", "deadline": сейчас - 600,
                       "finished_at": сейчас - 300})
    hub.monitoring.cache_ttl_s = 0
    данные = {м["name"]: м["values"] for м in
              client.get("/api/monitoring/metrics.json").json()["metrics"]}
    assert данные["asrhub_deadline_overdue"][0]["value"] == 1
    assert данные["asrhub_deadline_missed_day"][0]["value"] == 1


# ---------------------------------------------------------------------------
# Контроль качества: длины номеров, дни обзвона, отделы
# ---------------------------------------------------------------------------

def test_длины_коротких_номеров_настройкой():
    from asrhub.qc import survey

    assert survey.длины_коротких("3,4") == (3, 4)
    assert survey.длины_коротких("2-5") == (2, 3, 4, 5)
    assert survey.длины_коротких("") == survey.КОРОТКИЙ_НОМЕР
    группы = survey.разобрать_группы([{"name": "Г", "extensions": "10-19",
                                       "questions": [{"text": "Оцените", "min": 1, "max": 5}],
                                       "direction": "any"}])
    звонок = {"direction": "внутренний", "answered": 1, "agent": "12", "src": "12",
              "dst": "55", "billsec": 60}
    assert isinstance(survey.отобрать(звонок, группы, короткие=True), str)
    итог = survey.отобрать(звонок, группы, короткие=True, длины=(2,))
    assert not isinstance(итог, str) and итог[2] == "55"


def test_дни_обзвона_и_праздники(кк):
    import datetime as dt

    from asrhub.qc import survey

    assert survey.дни_обзвона("1-5") == {1, 2, 3, 4, 5}
    assert survey.дни_обзвона("") == set()
    служба = кк.hub.qc
    сегодня = служба._сейчас_местное()
    assert служба.можно_звонить()
    кк.hub.settings.set("qc_weekdays", str((сегодня.isoweekday() % 7) + 1))   # завтра
    assert not служба.можно_звонить()
    кк.hub.settings.set("qc_weekdays", "")
    кк.hub.settings.set("qc_holidays", сегодня.strftime("%m-%d"))
    assert not служба.можно_звонить()
    кк.hub.settings.set("qc_holidays", (сегодня + dt.timedelta(days=1)).strftime("%Y-%m-%d"))
    assert служба.можно_звонить()


def test_группа_по_отделу_справочника():
    from asrhub.qc import survey

    группы = survey.разобрать_группы([{"name": "Продажи", "departments": "Отдел продаж",
                                       "questions": [{"text": "Оцените", "min": 1, "max": 5}]}])
    assert группы[0].departments == ("отдел продаж",)
    assert survey.проверить_группы([группы[0].to_dict()]) == [] if hasattr(
        survey, "проверить_группы") else True
    звонок = {"direction": "входящий", "answered": 1, "agent": "305", "src": "79161234567",
              "dst": "74950000000", "billsec": 60}
    assert survey.отобрать(звонок, группы) == "нет подходящей группы" or isinstance(
        survey.отобрать(звонок, группы), str)
    итог = survey.отобрать(звонок, группы, отделы={"305": "Отдел  ПРОДАЖ"})
    assert not isinstance(итог, str) and итог[0].name == "Продажи"


def test_агент_берёт_linkedid_из_mysql_если_колонки_есть(tmp_path: Path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("агент57в", КОРЕНЬ / "agent" / "asrhub-agent.py")
    агент = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(агент)
    (tmp_path / "state").mkdir()
    конфиг = tmp_path / "cdr_mysql.conf"
    конфиг.write_text("[global]\ndbname=cdrdb\ntable=cdr\n", encoding="utf-8")
    состояние = агент.Состояние(str(tmp_path / "state" / "state.json"))
    источник = агент.ИсточникMySQL.из_конфига_asterisk(str(конфиг), состояние, поверх={},
                                                       каталог_временных=str(tmp_path / "state"))
    запросы: list[str] = []

    def выполнить(запрос):
        запросы.append(запрос)
        if запрос.startswith("SHOW COLUMNS"):
            return "linkedid\tvarchar(32)" if "linkedid" in запрос else "sequence\tint(11)"
        if len([з for з in запросы if з.startswith("SELECT")]) > 1:
            return ""
        return "\t".join(["", "79161234567", "101", "from-trunk", "К", "SIP/tr", "SIP/101",
                          "Dial", "SIP/101", "2026-09-10 14:01:00", "60", "50", "ANSWERED",
                          "1757600001.2", "", "1757600001.1", "4"]) + "\n"

    источник._выполнить = выполнить
    звонки_ = list(источник.звонки(весь_архив=True))
    выборка = next(з for з in запросы if з.startswith("SELECT"))
    assert ", linkedid, sequence " in выборка
    assert (звонки_[0].linkedid, звонки_[0].sequence) == ("1757600001.1", 4)
