"""Заход 54: телефония — находки ревизии 3.1.20 (отчёт 13-telephony).

Каждая проверка воспроизводит поломку так, как её видел человек: отвеченная
нога звонка, выброшенная как «уже импортирован»; архив, списанный за сутки
полной очереди; события AMI, потерянные на обрыве; станция, после которой
сервер не поднимается; чужая запись у звонка.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import types
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.errors import ConfigError, QueueFull, StorageError
from asrhub.telephony import asterisk
from asrhub.telephony.asterisk import AMIClient, AMIError, Звонок, найти_запись
from asrhub.telephony.importer import Импортёр, Телефония
from asrhub.telephony.stations import _станция_из, карта_владельцев, проверить_набор, список

# ---------------------------------------------------------------------------
# Опоры
# ---------------------------------------------------------------------------


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def wav(путь: Path, *, когда: float | None = None, секунд: int = 1,
        каналов: int = 1) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(каналов)
        файл.setsampwidth(2)
        файл.setframerate(8000)
        файл.writeframes(b"\x01\x02" * каналов * (8000 * секунд))
    if когда is not None:
        os.utime(путь, (когда, когда))
    return путь


def часы(момент: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(момент))


def cdr(uid: str, src: str = "79161234567", dst: str = "101", *, начало: float,
        duration: int = 300, billsec: int | None = None,
        disposition: str = "ANSWERED", context: str = "from-trunk",
        channel: str = "SIP/tr-0001", dstchannel: str | None = None,
        lastapp: str = "Dial", lastdata: str | None = None, clid: str | None = None) -> str:
    н, к = часы(начало), часы(начало + duration)
    billsec = duration - 10 if billsec is None else billsec
    dstchannel = f"SIP/{dst}-0002" if dstchannel is None else dstchannel
    lastdata = f"SIP/{dst}" if lastdata is None else lastdata
    clid = f'""{src}"" <{src}>' if clid is None else clid
    ответ = н if disposition == "ANSWERED" else ""
    return (f'"","{src}","{dst}","{context}","{clid}","{channel}","{dstchannel}",'
            f'"{lastapp}","{lastdata}","{н}","{ответ}","{к}",{duration},{billsec},'
            f'"{disposition}","DOCUMENTATION","{uid}",""\n')


class Очередь:
    """Очередь-пустышка: запоминает вызовы; `отказ` — сколько раз отказать."""

    def __init__(self, *, предел: int | None = None):
        self.вызовы: list[dict] = []
        self.предел = предел
        self.отмены: list[str] = []
        self._замок = threading.Lock()

    def submit(self, **kwargs):
        with self._замок:
            if self.предел is not None and len(self.вызовы) >= self.предел:
                raise QueueFull(f"В очереди уже {len(self.вызовы)} заданий.")
            self.вызовы.append(kwargs)
            return {"id": f"job_{len(self.вызовы)}"}

    def cancel(self, job_id: str, by: str = "user"):
        self.отмены.append(job_id)
        return {}


class Настройки(dict):
    def get(self, ключ, по_умолчанию=None):                  # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def merged(self, поверх):
        значения = dict(self)
        значения.update(поверх or {})
        return значения


def поля_станции(кат: Path, **поверх) -> dict:
    поля = {
        "id": "pbx", "name": "АТС", "enabled": True, "source": "cdr_csv",
        "cdr_file": str(кат / "Master.csv"),
        "recordings_dir": str(кат / "monitor"),
        "settle_s": 0, "min_duration_s": 0, "skip_unanswered": False,
        "internal_digits": "3", "poll_s": 60, "lookback_days": 3650,
        "contexts": {"from-trunk": "входящий"},
    }
    поля.update(поверх)
    return поля


def импортёр(db: Database, кат: Path, очередь: Очередь | None = None,
             настройки: Настройки | None = None, **поверх) -> Импортёр:
    return Импортёр(db, _станция_из(поля_станции(кат, **поверх), 0),
                    очередь or Очередь(), настройки if настройки is not None else Настройки())


def звонки(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM calls ORDER BY rowid")]


def журнал(кат: Path, *строки: str) -> Path:
    путь = кат / "Master.csv"
    путь.write_text("".join(строки), encoding="utf-8")
    (кат / "monitor").mkdir(exist_ok=True)
    return путь


def ждать(условие, срок: float = 30.0) -> bool:
    """Ждать, пока условие станет истинным; срок — с большим запасом."""
    конец = time.monotonic() + срок
    while time.monotonic() < конец:
        if условие():
            return True
        time.sleep(0.05)
    return условие()


ДАВНО = time.time() - 3600


# ---------------------------------------------------------------------------
# №1. Две строки CDR с одним uniqueid
# ---------------------------------------------------------------------------

def test_отвеченная_нога_звонка_не_выбрасывается_как_уже_импортированная(tmp_path: Path):
    """101 не взял трубку, перевод на 102, пять минут разговора — один звонок.

    Раньше первая строка ложилась «без ответа», вторая — «уже импортирован»,
    и разговор не распознавался никогда.
    """
    db = база(tmp_path)
    журнал(tmp_path,
           cdr("1757500001.1", dst="101", начало=ДАВНО, duration=20, billsec=0,
               disposition="NO ANSWER"),
           cdr("1757500001.1", dst="102", начало=ДАВНО + 20, duration=300, billsec=290))
    wav(tmp_path / "monitor" / "in-1757500001.1.wav", когда=ДАВНО + 330)
    очередь = Очередь()
    свод = импортёр(db, tmp_path, очередь, skip_unanswered=True).scan()

    assert свод["imported"] == 1, свод
    assert "уже импортирован" not in свод["reasons"]
    [звонок] = звонки(db)
    assert звонок["dst"] == "102" and звонок["disposition"] == "ANSWERED"
    assert звонок["billsec"] == 290 and звонок["job_id"] == "job_1"
    # Время — от всего звонка: двадцать секунд звонил 101, потом разговор.
    assert звонок["duration"] == 320
    assert len(очередь.вызовы) == 1
    # Тот же журнал, прочитанный снова («забыть позицию»), — повтор, а не
    # новая нога: второго задания нет.
    имп = импортёр(db, tmp_path, очередь, skip_unanswered=True)
    имп.забыть_позицию()
    assert имп.scan()["reasons"] == {"уже импортирован": 1}
    assert len(очередь.вызовы) == 1


def test_нога_пришедшая_следующим_заходом_ставит_звонок(tmp_path: Path):
    """Строки одного звонка разошлись по двум заходам (предел порции, AMI)."""
    db = база(tmp_path)
    путь = журнал(tmp_path, cdr("1757500002.1", dst="101", начало=ДАВНО, duration=20,
                                billsec=0, disposition="NO ANSWER"))
    wav(tmp_path / "monitor" / "in-1757500002.1.wav", когда=ДАВНО + 330)
    очередь = Очередь()
    имп = импортёр(db, tmp_path, очередь, skip_unanswered=True)
    assert имп.scan()["reasons"] == {"без ответа": 1}

    with путь.open("a", encoding="utf-8") as файл:
        файл.write(cdr("1757500002.1", dst="102", начало=ДАВНО + 20, duration=300,
                       billsec=290))
    свод = имп.scan()
    assert свод["imported"] == 1, свод
    [звонок] = звонки(db)
    assert звонок["dst"] == "102" and звонок["skipped"] == "" and звонок["job_id"]


def test_новая_нога_у_звонка_с_заданием_обновляет_поля_но_не_ставит_второе(tmp_path: Path):
    """skip_unanswered выключен: первая нога ушла в распознавание с полями 101."""
    db = база(tmp_path)
    путь = журнал(tmp_path, cdr("1757500004.1", dst="101", начало=ДАВНО, duration=20,
                                billsec=0, disposition="NO ANSWER"))
    wav(tmp_path / "monitor" / "in-1757500004.1.wav", когда=ДАВНО + 330)
    очередь = Очередь()
    имп = импортёр(db, tmp_path, очередь)
    assert имп.scan()["imported"] == 1
    with путь.open("a", encoding="utf-8") as файл:
        файл.write(cdr("1757500004.1", dst="102", начало=ДАВНО + 20, duration=300,
                       billsec=290))
    свод = имп.scan()
    assert свод["reasons"] == {"ещё нога звонка": 1}
    [звонок] = звонки(db)
    assert (звонок["dst"], звонок["disposition"], звонок["billsec"]) == ("102", "ANSWERED", 290)
    assert звонок["job_id"] == "job_1" and len(очередь.вызовы) == 1


# ---------------------------------------------------------------------------
# №2. Полная очередь: обратное давление и бессрочные отложенные
# ---------------------------------------------------------------------------

def test_отказ_очереди_останавливает_заход_и_не_списывает_звонки(tmp_path: Path):
    """Очередь на два места, журнал в шесть звонков.

    Раньше журнал читался дальше, всё уходило в отложенные «очередь отказала»,
    а через сутки списывалось навсегда; «забыть позицию» давало «уже
    импортирован».
    """
    db = база(tmp_path)
    журнал(tmp_path, *(cdr(f"1757600001.{н}", начало=ДАВНО + н) for н in range(6)))
    for н in range(6):
        wav(tmp_path / "monitor" / f"1757600001.{н}.wav", когда=ДАВНО + 300 + н)
    очередь = Очередь(предел=2)
    имп = импортёр(db, tmp_path, очередь)
    свод = имп.scan()
    assert свод["imported"] == 2 and свод["reasons"] == {"очередь отказала": 1}, свод
    # Остаток журнала не тронут: ни строки в архиве, позиция перед ним.
    assert len(звонки(db)) == 3

    # Сутки простоя очереди: отложенный из-за неё не списывается.
    db.execute("UPDATE calls SET deferred_at=?, imported_at=? WHERE skipped LIKE 'отложен:%'",
               (time.time() - 3 * 86400, time.time() - 3 * 86400))
    очередь.предел = None
    for _ in range(3):
        имп.scan()
    итог = звонки(db)
    assert len(итог) == 6
    assert all(з["job_id"] for з in итог), [(з["pbx_uid"], з["skipped"]) for з in итог]
    assert len(очередь.вызовы) == 6


def test_полная_очередь_не_читается_журнал(tmp_path: Path, monkeypatch):
    """Свободного места в очереди нет — журнал не читается вовсе."""
    db = база(tmp_path)
    журнал(tmp_path, cdr("1757600002.1", начало=ДАВНО))
    wav(tmp_path / "monitor" / "1757600002.1.wav", когда=ДАВНО + 300)
    очередь = Очередь()
    очередь._max_queue = 3
    monkeypatch.setattr(db, "count_jobs", lambda **_: 3)
    имп = импортёр(db, tmp_path, очередь)
    свод = имп.scan()
    assert свод["backpressure"] is True and свод["seen"] == 0
    assert int(db.get_kv("telephony_cdr_offset:pbx", 0) or 0) == 0
    monkeypatch.setattr(db, "count_jobs", lambda **_: 0)
    assert имп.scan()["imported"] == 1


# ---------------------------------------------------------------------------
# №3. /process-call: уникальное имя записи
# ---------------------------------------------------------------------------

def test_process_call_с_одним_call_id_не_перезаписывает_чужую_запись(tmp_path: Path,
                                                                      monkeypatch):
    """Две CRM с разными ключами нумеруют звонки с 1001.

    Раньше второй запрос перезаписывал файл, на который ссылалось ждущее
    задание первого: одна CRM получала расшифровку чужого разговора.
    """
    from asrhub import phone_compat
    from asrhub.api import routes_phone
    from asrhub.api.deps import Principal

    поставлено: list[dict] = []
    uploads = tmp_path / "uploads"
    tmp = tmp_path / "tmp"

    class Пути:
        pass

    пути = Пути()
    пути.uploads, пути.tmp = uploads, tmp
    настройки = Настройки({"max_upload_mb": 100, "phone_compat_enabled": True})
    настройки.paths = пути
    state = types.SimpleNamespace(
        settings=настройки, db=None,
        queue=types.SimpleNamespace(
            _check_webhook_url=lambda адрес: адрес,
            submit=lambda **kw: поставлено.append(kw) or {"id": f"job_{len(поставлено)}"}))

    def скачать(urls, workdir, *, limit_bytes, check_url=None):
        workdir.mkdir(parents=True, exist_ok=True)
        путь = workdir / "часть-0.wav"
        wav(путь, секунд=int(urls[0].rsplit("/", 1)[-1].split(".")[0]))
        return phone_compat.Fetched(path=путь, channels=1, filename="запись.wav",
                                    sources=list(urls))

    monkeypatch.setattr(phone_compat, "download", скачать)
    кто = Principal(name="админ", role="admin")
    for секунд in (1, 3):
        звонок = phone_compat.PhoneRequest(call_id="1001", files=[f"https://x/{секунд}.wav"],
                                           base_url="https://crm.example")
        routes_phone._accept(state, звонок, "https://crm.example/cb.php", кто)
    assert len(поставлено) == 2
    первый, второй = (Path(п["file_path"]) for п in поставлено)
    assert первый != второй, "две записи под одним именем"
    assert первый.stat().st_size < второй.stat().st_size, "запись первого задания подменена"


# ---------------------------------------------------------------------------
# Поддельная AMI
# ---------------------------------------------------------------------------

class ПоддельнаяAMI:
    """AMI на 127.0.0.1: вход принимается, дальше — сценарий (conn, self)."""

    def __init__(self, сценарий):
        self.сценарий = сценарий
        self.получено = b""
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._слушать, daemon=True).start()

    def _слушать(self):
        while not self._stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._обслужить, args=(conn,), daemon=True).start()

    def читать_действие(self, conn) -> bytes:
        данные = b""
        conn.settimeout(10)
        while b"\r\n\r\n" not in данные:
            кусок = conn.recv(4096)
            if not кусок:
                break
            данные += кусок
        self.получено += данные
        return данные

    def _обслужить(self, conn):
        try:
            conn.sendall(b"Asterisk Call Manager/5.0.2\r\n")
            self.читать_действие(conn)
            conn.sendall(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
            self.сценарий(conn, self)
        except OSError:
            pass

    def close(self):
        self._stop = True
        try:
            self.sock.close()
        except OSError:
            pass


def событие_cdr(uid: str, *, начало: float, src: str = "79161234567",
                dst: str = "101", duration: int = 60, billsec: int = 50) -> bytes:
    поля = [
        "Event: Cdr", "Privilege: cdr,all", "AccountCode: ", f"Source: {src}",
        f"Destination: {dst}", "DestinationContext: from-trunk",
        f'CallerID: "{src}" <{src}>', "Channel: SIP/tr-0001",
        f"DestinationChannel: SIP/{dst}-0002", "LastApplication: Dial",
        f"LastData: SIP/{dst}", f"StartTime: {часы(начало)}",
        f"AnswerTime: {часы(начало)}", f"EndTime: {часы(начало + duration)}",
        f"Duration: {duration}", f"BillableSeconds: {billsec}",
        "Disposition: ANSWERED", "AMAFlags: DOCUMENTATION", f"UniqueID: {uid}",
        "UserField: ",
    ]
    return ("\r\n".join(поля) + "\r\n\r\n").encode()


# ---------------------------------------------------------------------------
# №4, №14, №15, №19. AMI
# ---------------------------------------------------------------------------

def test_события_прочитанные_до_обрыва_ami_не_теряются(tmp_path: Path):
    """manager reload: три события Cdr и FIN в сокете — три звонка в архиве."""
    def сценарий(conn, _srv):
        for н in range(3):
            conn.sendall(событие_cdr(f"1757700001.{н}", начало=ДАВНО + н))
        time.sleep(0.3)
        conn.close()

    сервер = ПоддельнаяAMI(сценарий)
    try:
        db = база(tmp_path)
        (tmp_path / "monitor").mkdir()
        for н in range(3):
            wav(tmp_path / "monitor" / f"in-1757700001.{н}.wav", когда=ДАВНО + 100)
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        ждать(lambda: False, срок=0.8)   # станция успела прислать события и закрыть
        свод = имп.scan()
        assert свод["imported"] == 3, свод
        assert имп.last_error and "закрыла соединение" in имп.last_error
        assert имп._ami is None, "оборванное соединение не закрыто"
    finally:
        сервер.close()


def test_событие_ami_разрезанное_паузой_собирается_целиком(tmp_path: Path):
    """Повторная передача TCP посреди события Cdr — пауза дольше секунды."""
    def сценарий(conn, _srv):
        пакет = событие_cdr("1757800001.5", начало=ДАВНО)
        разрез = пакет.index(b"Disposition")
        conn.sendall(пакет[:разрез])
        time.sleep(1.6)
        conn.sendall(пакет[разрез:])
        time.sleep(5)

    сервер = ПоддельнаяAMI(сценарий)
    try:
        db = база(tmp_path)
        (tmp_path / "monitor").mkdir()
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        собрано = []
        for _ in range(4):
            собрано += имп._из_ami(50)
            if собрано:
                break
        имп._закрыть_ami()
        assert [(з.uniqueid, з.disposition, з.billsec) for з in собрано] == \
            [("1757800001.5", "ANSWERED", 50)]
    finally:
        сервер.close()


def test_тишина_ami_проверяется_ping_а_события_во_время_ping_не_теряются(
        tmp_path: Path, monkeypatch):
    """Мёртвое соединение неотличимо от тишины — а живое на Ping отвечает."""
    from asrhub.telephony import importer as модуль

    def сценарий(conn, srv):
        действие = srv.читать_действие(conn)
        if b"Action: Ping" in действие:
            # Событие проскакивает раньше ответа на Ping — его нельзя съесть.
            conn.sendall(событие_cdr("1757900001.1", начало=ДАВНО))
            conn.sendall(b"Response: Success\r\nPing: Pong\r\n\r\n")
        time.sleep(3)

    monkeypatch.setattr(модуль, "ПИНГ_ПОСЛЕ_С", 0.0)
    сервер = ПоддельнаяAMI(сценарий)
    try:
        db = база(tmp_path)
        (tmp_path / "monitor").mkdir()
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        звонки_ami = имп._из_ami(1)
        assert b"Action: Ping" in сервер.получено
        assert [з.uniqueid for з in звонки_ami] == ["1757900001.1"]
        сокет = имп._ami._sock
        assert сокет.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) == 1
        имп._закрыть_ami()
    finally:
        сервер.close()


def test_перевод_строки_в_пароле_ami_не_дописывает_действие():
    """«x⏎⏎Action: Originate…» в пароле — второе действие в обход списка."""
    сервер = ПоддельнаяAMI(lambda conn, srv: time.sleep(1))
    try:
        клиент = AMIClient("127.0.0.1", сервер.port, "asrhub",
                           "x\r\n\r\nAction: Originate\r\nChannel: SIP/trunk/79001234567",
                           timeout=2)
        with pytest.raises(AMIError):
            клиент.connect()
        клиент.close()
        assert b"Originate" not in сервер.получено
    finally:
        сервер.close()
    ошибки = проверить_набор([{"name": "А", "source": "ami", "username": "asrhub",
                                "secret": "x\nAction: Originate"}])
    assert any("перевод строки" in о for о in ошибки), ошибки


# ---------------------------------------------------------------------------
# №5. owner_map не-объект
# ---------------------------------------------------------------------------

def test_испорченная_станция_не_роняет_ни_список_ни_проверку():
    """owner_map числом или строкой без пар — раньше 500 и неподнимаемый сервер."""
    assert карта_владельцев("101=sales, 102 = support") == {"101": "sales", "102": "support"}
    assert карта_владельцев(5) == {}
    станция = _станция_из({"name": "А", "owner_map": "101=sales", "contexts": 7,
                           "enabled": "false"}, 0)
    assert станция.owner_map == {"101": "sales"}
    assert станция.enabled is False, "строка «false» — это выключено"
    станция.to_dict(for_admin=True)          # не падает на contexts=7
    ошибки = проверить_набор([{"name": "А", "cdr_file": "/x", "owner_map": 5,
                                "port": "пять"}])
    assert any("owner_map" in о for о in ошибки)
    assert any("port" in о for о in ошибки)
    настройки = Настройки({"telephony_stations": [
        {"name": "Хорошая", "cdr_file": "/a"}, "не объект", {"name": "Тоже", "cdr_file": "/b",
                                                             "owner_map": [1, 2, 3]}]})
    assert [с.name for с in список(настройки)] == ["Хорошая", "Тоже"]


def test_прежний_список_станций_переживает_запуск(data_dir: Path, monkeypatch):
    """Проверка каталога действует и на чтение config.yaml — там негодное
    значение заменяется пустым списком. То, что прежние версии принимали
    (один журнал у двух станций), не должно стереть все станции."""
    from asrhub.config import load

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(
        "telephony:\n  telephony_stations:\n"
        "    - {name: Офис, source: cdr_csv, cdr_file: /var/log/Master.csv}\n"
        "    - {name: Копия, source: cdr_csv, cdr_file: /var/log/Master.csv}\n",
        encoding="utf-8")
    monkeypatch.setenv("ASRHUB_CONFIG", str(data_dir / "config.yaml"))
    настройки = load()
    assert [с["name"] for с in настройки.get("telephony_stations")] == ["Офис", "Копия"]
    assert not настройки.problems, настройки.problems
    # А станция, которую нельзя разобрать никак, — громко, но запуск идёт.
    (data_dir / "config.yaml").write_text(
        "telephony:\n  telephony_stations:\n"
        "    - {name: Офис, source: cdr_csv, cdr_file: /a, contexts: 7}\n",
        encoding="utf-8")
    assert any("contexts" in п for п in load().problems)


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin": {"name": "админ", "role": "admin", "enabled": True},
        "ah_user": {"name": "отдел", "role": "user", "enabled": True},
    })
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        yield клиент


АДМИН = {"X-API-Key": "ah_admin"}


def test_станция_с_owner_map_строкой_принимается_без_500(сервер, tmp_path: Path):
    ответ = сервер.post("/api/telephony/stations", headers=АДМИН, json={
        "name": "Офис", "source": "cdr_csv", "cdr_file": str(tmp_path / "Master.csv"),
        "owner_map": "101=sales"})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["station"]["owner_map"] == {"101": "sales"}
    плохая = сервер.post("/api/telephony/stations", headers=АДМИН, json={
        "name": "Филиал", "source": "cdr_csv", "cdr_file": str(tmp_path / "F.csv"),
        "owner_map": 5})
    assert плохая.status_code == 400, плохая.text
    assert сервер.get("/api/telephony/status", headers=АДМИН).status_code == 200
    # Общий PUT /api/settings проверяет станции той же проверкой.
    через_настройки = сервер.put("/api/settings", headers=АДМИН, json={
        "telephony_stations": [{"name": "Х", "source": "cdr_csv", "cdr_file": "/x",
                                "owner_map": 5}]})
    assert через_настройки.status_code == 400, через_настройки.text
    assert сервер.get("/api/telephony/status", headers=АДМИН).status_code == 200


def test_заглушка_пароля_не_сохраняется_паролем_ami(сервер):
    """Клиент API прочитал станцию (secret: «***») и отправил её обратно."""
    создано = сервер.post("/api/telephony/stations", headers=АДМИН, json={
        "name": "AMI", "source": "ami", "host": "10.0.0.5", "username": "asrhub",
        "secret": "настоящий"}).json()["station"]
    assert создано["secret"] == "***"
    ответ = сервер.post("/api/telephony/stations", headers=АДМИН,
                        json={**создано, "poll_s": 120})
    assert ответ.status_code == 200, ответ.text
    станции = сервер.app.state.hub.settings.get("telephony_stations")
    assert [с.get("secret") for с in станции] == ["настоящий"]


def test_обычный_ключ_не_видит_сводов_захода_станции(сервер):
    сервер.post("/api/telephony/stations", headers=АДМИН, json={
        "name": "Офис", "source": "cdr_csv", "cdr_file": "/var/log/x.csv"})
    станция = сервер.get("/api/telephony/status",
                         headers={"X-API-Key": "ah_user"}).json()["stations"][0]
    assert "imported" not in станция and "failed" not in станция
    assert set(станция["last_scan"]) <= {"at"}
    assert "cdr_file" not in станция


# ---------------------------------------------------------------------------
# №6. Сбор архива: предел по новым и конец по дочитанному журналу
# ---------------------------------------------------------------------------

def _дождаться_сбора(имп: Импортёр) -> dict:
    assert ждать(lambda: not имп.сбор_состояние()["running"], срок=120)
    return имп.сбор_состояние()


def test_сбор_архива_продолжает_с_места_остановки_на_пределе(tmp_path: Path, monkeypatch):
    """Раньше каждый запуск обнулял позицию и вставал на том же пределе."""
    from asrhub.telephony import importer as модуль

    monkeypatch.setattr(модуль, "ПОРЦИЯ_СБОРА", 10)
    db = база(tmp_path)
    журнал(tmp_path, *(cdr(f"1758000000.{н}", начало=ДАВНО + н) for н in range(70)))
    имп = импортёр(db, tmp_path)
    сколько = []
    for _ in range(3):
        имп.собрать(предел=30)
        итог = _дождаться_сбора(имп)
        сколько.append(len(звонки(db)))
    assert сколько == [30, 60, 70], сколько
    assert итог["error"] == "", итог


def test_сбор_архива_кончается_при_отложенных_а_не_крутится(tmp_path: Path):
    """Отложенные входят в «увидено», и сбор не замечал, что журнал кончился."""
    db = база(tmp_path)
    сейчас = time.time()
    журнал(tmp_path, *(cdr(f"1758100000.{н}", начало=сейчас - 60, duration=30)
                       for н in range(5)))
    имп = импортёр(db, tmp_path, settle_s=86400)
    имп.собрать()
    итог = _дождаться_сбора(имп)
    assert итог["passes"] <= 3, итог
    assert итог["seen"] <= 20, итог


def test_сбор_по_выключенной_станции_отвечает_отказом(tmp_path: Path):
    db = база(tmp_path)
    настройки = Настройки({"telephony_enabled": True, "telephony_stations": [
        поля_станции(tmp_path, enabled=False)]})
    телефония = Телефония(db, настройки, Очередь())
    with pytest.raises(ConfigError, match="выключена"):
        телефония.собрать(station_id="pbx")
    настройки["telephony_enabled"] = False
    with pytest.raises(ConfigError, match="выключен"):
        телефония.собрать()


# ---------------------------------------------------------------------------
# №7, №8. Очередь, оператор и владелец
# ---------------------------------------------------------------------------

def test_очередь_звонка_через_отложенные_не_теряется(tmp_path: Path):
    """settle_s>0 — у источника AMI откладывается каждый звонок."""
    db = база(tmp_path)
    журнал(tmp_path, cdr("1758200001.7", dst="500", начало=time.time() - 120, duration=100,
                         lastapp="Queue", lastdata="500,t,,,300", context="ext-queues",
                         dstchannel="PJSIP/101-0000002a"))
    wav(tmp_path / "monitor" / "q-500-1758200001.7.wav", когда=time.time() - 3600)
    имп = импортёр(db, tmp_path, settle_s=86400, contexts={"ext-queues": "входящий"})
    assert имп.scan()["reasons"] == {"ещё пишется": 1}
    очередь = Очередь()
    второй = импортёр(db, tmp_path, очередь, settle_s=0, contexts={"ext-queues": "входящий"})
    assert второй.scan()["imported"] == 1
    [звонок] = звонки(db)
    assert звонок["queue"] == "500"
    assert "очередь 500" in очередь.вызовы[0]["tags"]


def test_оператор_и_владелец_из_канала_а_не_из_номера(tmp_path: Path):
    """Очередь 500, снял 101: оператор — 101, владелец — отдел 101."""
    db = база(tmp_path)
    журнал(tmp_path,
           cdr("1758300001.1", dst="500", начало=ДАВНО, lastapp="Queue", lastdata="500,t",
               context="ext-queues", dstchannel="PJSIP/101-0000002a"),
           cdr("1758300002.1", src="79161234567", dst="74951234567", начало=ДАВНО + 60,
               context="from-pstn", dstchannel="SIP/102-00000031"))
    wav(tmp_path / "monitor" / "1758300001.1.wav", когда=ДАВНО + 300)
    wav(tmp_path / "monitor" / "1758300002.1.wav", когда=ДАВНО + 360)
    очередь = Очередь()
    имп = импортёр(db, tmp_path, очередь, owner="common",
                   owner_map={"101": "sales", "102": "support"},
                   contexts={"ext-queues": "входящий", "from-pstn": "входящий"})
    имп.scan()
    по_ид = {з["pbx_uid"]: з for з in звонки(db)}
    assert по_ид["1758300001.1"]["agent"] == "101"
    assert по_ид["1758300001.1"]["owner"] == "sales"
    assert по_ид["1758300002.1"]["agent"] == "102"
    assert по_ид["1758300002.1"]["owner"] == "support"
    # Направление не определилось — оператора нет, а не номер клиента.
    неизвестный = Звонок(uniqueid="x", src="79161234567", dst="74951234567")
    asterisk.разметить(неизвестный, внутренние_знаков="3", контексты={})
    assert неизвестный.direction == "" and неизвестный.agent == ""


def test_пропущенный_без_ответа_звонок_несёт_направление(tmp_path: Path):
    """Без направления «потерянные входящие» в показателях не считались вовсе."""
    db = база(tmp_path)
    журнал(tmp_path, cdr("1758400001.1", начало=ДАВНО, duration=20, billsec=0,
                         disposition="NO ANSWER"))
    импортёр(db, tmp_path, skip_unanswered=True).scan()
    [звонок] = звонки(db)
    assert звонок["skipped"] == "без ответа" and звонок["direction"] == "входящий"


# ---------------------------------------------------------------------------
# №9. Подбор записи по номерам и маске
# ---------------------------------------------------------------------------

def test_по_номерам_берётся_запись_своего_разговора_а_не_предыдущего(tmp_path: Path):
    """Разговор А — полчаса, через 30 с клиент перезванивает на две минуты."""
    каталог = tmp_path / "monitor"
    начало_а = ДАВНО - 3 * 3600
    конец_а = начало_а + 1800
    начало_б = конец_а + 30
    конец_б = начало_б + 120
    wav(каталог / "in-79161234567-101-a.wav", когда=конец_а)
    wav(каталог / "in-79161234567-101-b.wav", когда=конец_б)
    звонок_б = Звонок(uniqueid="без-id-б", src="79161234567", dst="101",
                      started_at=начало_б, duration=120)
    найдено = найти_запись(звонок_б, каталог, окно_минут=120, окно_дней=30)
    assert найдено is not None and найдено.name == "in-79161234567-101-b.wav"


def test_одна_запись_не_достаётся_двум_звонкам(tmp_path: Path):
    db = база(tmp_path)
    журнал(tmp_path,
           cdr("aa-1", начало=ДАВНО, duration=300),
           cdr("aa-2", начало=ДАВНО + 400, duration=300))
    wav(tmp_path / "monitor" / "in-79161234567-101.wav", когда=ДАВНО + 300)
    очередь = Очередь()
    свод = импортёр(db, tmp_path, очередь).scan()
    assert свод["imported"] == 1 and свод["reasons"] == {"нет записи": 1}, свод


def test_звёздочка_в_номере_не_превращает_имя_в_маску(tmp_path: Path):
    каталог = tmp_path / "monitor"
    wav(каталог / "79990001122-101.wav")
    звонок = Звонок(uniqueid="1758500001.1", src="*", dst="101", started_at=time.time())
    assert найти_запись(звонок, каталог, шаблон="{src}-{dst}.wav", окно_минут=0) is None
    # А шаблон-маска по-прежнему маска — и номер в ней экранирован.
    свой = Звонок(uniqueid="1758500002.1", src="79990001122", dst="101",
                  started_at=time.time())
    assert найти_запись(свой, каталог, шаблон="{src}-*.wav", окно_минут=0) is not None
    чужой = Звонок(uniqueid="1758500003.1", src="*", dst="101", started_at=time.time())
    assert найти_запись(чужой, каталог, шаблон="{src}-{dst}*.wav", окно_минут=0) is None


# ---------------------------------------------------------------------------
# №10. Призрачная станция
# ---------------------------------------------------------------------------

def test_галочка_забора_не_заводит_станцию_и_удалённая_не_воскресает(data_dir: Path,
                                                                    monkeypatch):
    from asrhub.config import load

    настройки = load()
    настройки.set("telephony_enabled", True, source="api")
    assert список(настройки) == [], "станция «АТС» из одной галочки"
    # Сервер прежней версии: станция в старых ключах — работает, как работала.
    настройки.set("telephony_source", "cdr_csv", source="api")
    настройки.set("telephony_cdr_file", "/var/log/asterisk/cdr-csv/Master.csv", source="api")
    assert [с.id for с in список(настройки)] == ["pbx"]
    # Удалили последнюю станцию: пустой список — это «станций нет».
    настройки.set("telephony_stations", [], source="api")
    assert список(настройки) == []


def test_две_станции_на_одном_журнале_не_сохраняются():
    ошибки = проверить_набор([
        {"name": "АТС", "source": "cdr_csv", "cdr_file": "/var/log/asterisk/cdr-csv/Master.csv"},
        {"name": "Офис", "source": "cdr_csv", "cdr_file": "/var/log/asterisk//cdr-csv/Master.csv"},
    ])
    assert any("уже читает" in о for о in ошибки), ошибки
    assert not проверить_набор([
        {"name": "АТС", "cdr_file": "/a.csv"},
        {"name": "Архив", "cdr_file": "/a.csv", "enabled": False}])


# ---------------------------------------------------------------------------
# №11. PUT /api/settings доходит до потоков
# ---------------------------------------------------------------------------

def test_выключатель_забора_доходит_до_потоков_без_раздела_атс(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(Телефония, "ПЕРИОД_СВЕРКИ", 0.2)
    db = база(tmp_path)
    журнал(tmp_path)
    настройки = Настройки({"telephony_enabled": False,
                           "telephony_stations": [поля_станции(tmp_path, poll_s=3600)]})
    телефония = Телефония(db, настройки, Очередь())
    телефония.start()
    try:
        def живой() -> bool:
            импортёр_ = телефония._станции.get("pbx")
            return bool(импортёр_ and импортёр_._thread and импортёр_._thread.is_alive())

        assert not живой()
        настройки["telephony_enabled"] = True          # как PUT /api/settings
        assert ждать(живой), "включённый забор не завёл поток"
        настройки["telephony_enabled"] = False
        assert ждать(lambda: not живой()), "выключенный забор продолжает ходить"
    finally:
        телефония.stop(timeout=2)


# ---------------------------------------------------------------------------
# №12. Глубина просмотра и длинный журнал
# ---------------------------------------------------------------------------

def test_старые_строки_журнала_не_занимают_порцию(tmp_path: Path):
    """Журнал за год: обычный заход сразу доходит до свежих звонков."""
    db = база(tmp_path)
    старое = time.time() - 400 * 86400
    журнал(tmp_path, *(cdr(f"1758600000.{н}", начало=старое + н) for н in range(120)),
           cdr("1758600999.1", начало=ДАВНО))
    wav(tmp_path / "monitor" / "1758600999.1.wav", когда=ДАВНО + 300)
    имп = импортёр(db, tmp_path, lookback_days=7)
    свод = имп.scan(limit=50)
    assert свод["imported"] == 1, свод
    assert свод["reasons"]["старше глубины просмотра"] == 120
    assert len(звонки(db)) == 1, "старые строки не должны помечаться в архиве"
    # «Собрать весь архив» глубины не знает — старое поднимается им.
    имп.собрать()
    _дождаться_сбора(имп)
    assert len(звонки(db)) == 121


def test_полная_порция_значит_есть_ещё(tmp_path: Path):
    db = база(tmp_path)
    журнал(tmp_path, *(cdr(f"1758700000.{н}", начало=ДАВНО + н) for н in range(5)))
    имп = импортёр(db, tmp_path)
    assert имп.scan(limit=3)["full"] is True
    assert имп.scan(limit=3)["full"] is False


# ---------------------------------------------------------------------------
# №13. Сбой базы не списывает порцию
# ---------------------------------------------------------------------------

def test_сбой_базы_прерывает_заход_и_не_сдвигает_позицию(tmp_path: Path, monkeypatch):
    db = база(tmp_path)
    журнал(tmp_path, *(cdr(f"1758800000.{н}", начало=ДАВНО + н) for н in range(5)))
    for н in range(5):
        wav(tmp_path / "monitor" / f"1758800000.{н}.wav", когда=ДАВНО + 300 + н)
    очередь = Очередь()
    имп = импортёр(db, tmp_path, очередь)

    def занята(*_a, **_k):
        raise StorageError("database is locked")

    настоящие = db.save_call, db.set_kv
    monkeypatch.setattr(db, "save_call", занята)
    monkeypatch.setattr(db, "set_kv", занята)
    свод = имп.scan()
    assert свод["imported"] == 0 and "сбой хранения" in свод["interrupted"]
    assert int(db.get_kv("telephony_cdr_offset:pbx", 0) or 0) == 0
    assert очередь.вызовы == [], "задание без строки в архиве — сирота"

    monkeypatch.setattr(db, "save_call", настоящие[0])
    monkeypatch.setattr(db, "set_kv", настоящие[1])
    assert имп.scan()["imported"] == 5
    assert len(звонки(db)) == 5 and len(очередь.вызовы) == 5

    # А звонок, о который база спотыкается заход за заходом (при живой базе),
    # перед остальным журналом вечно не стоит.
    путь = tmp_path / "Master.csv"
    with путь.open("a", encoding="utf-8") as файл:
        for н in range(3):
            файл.write(cdr(f"1758900000.{н}", начало=ДАВНО + 10 + н))
    for н in range(3):
        wav(tmp_path / "monitor" / f"1758900000.{н}.wav", когда=ДАВНО + 320 + н)

    def на_первом(ключ, **поля):
        if ключ.endswith("1758900000.0"):
            raise StorageError("disk I/O error")
        return настоящие[0](ключ, **поля)

    monkeypatch.setattr(db, "save_call", на_первом)
    for _ in range(4):
        имп.scan()
    новые = {з["pbx_uid"] for з in звонки(db) if з["pbx_uid"].startswith("1758900000")}
    assert новые == {"1758900000.1", "1758900000.2"}


# ---------------------------------------------------------------------------
# №16. Указатель каталога живёт между заходами
# ---------------------------------------------------------------------------

def test_каталог_записей_не_обходится_на_каждом_заходе(tmp_path: Path, monkeypatch):
    from asrhub.telephony.asterisk import УказательЗаписей

    monkeypatch.setattr(УказательЗаписей, "СРОК_ЖИЗНИ_С", 3600.0)
    db = база(tmp_path)
    путь = журнал(tmp_path, cdr("1759000001.1", начало=ДАВНО))
    wav(tmp_path / "monitor" / "1759000001.1.wav", когда=ДАВНО + 300)
    имп = импортёр(db, tmp_path)
    имп.scan()
    with путь.open("a", encoding="utf-8") as файл:
        файл.write(cdr("1759000002.1", начало=ДАВНО + 10))
    wav(tmp_path / "monitor" / "1759000002.1.wav", когда=ДАВНО + 310)
    имп._указатель._собран = time.time() - 1     # «старым» он ещё не стал
    имп.scan()
    assert имп._указатель.обходов == 1


# ---------------------------------------------------------------------------
# №17. Отказ /process-call — общей доставкой
# ---------------------------------------------------------------------------

def test_отказ_process_call_уходит_подписанной_доставкой(tmp_path: Path):
    """Подпись X-ASRHub-Signature, кодирование адреса с кириллицей, повторы."""
    from asrhub import phone_compat
    from asrhub.api import routes_phone
    from asrhub.job_queue import JobQueue

    получено: list[tuple[str, dict]] = []

    class Приёмник(BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_POST(self):                                   # noqa: N802
            тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            получено.append((self.path, {"sig": self.headers.get("X-ASRHub-Signature"),
                                         "body": json.loads(тело)}))
            self.send_response(200)
            self.end_headers()

    сервер = ThreadingHTTPServer(("127.0.0.1", 0), Приёмник)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    try:
        очередь = types.SimpleNamespace(
            settings=Настройки({"webhook_secret": "секрет", "webhook_allow_internal": True}),
            _stop=threading.Event(), db=types.SimpleNamespace(update_job=lambda *a, **k: None))
        очередь._deliver_webhook = types.MethodType(JobQueue._deliver_webhook, очередь)
        state = types.SimpleNamespace(queue=очередь)
        адрес = f"http://127.0.0.1:{сервер.server_address[1]}/проект/cb.php"
        звонок = phone_compat.PhoneRequest(call_id="1001", files=["https://x/a.wav"],
                                           base_url="https://x")
        routes_phone._send_failure(state, звонок, адрес, "файл недоступен")
    finally:
        сервер.shutdown()
        сервер.server_close()
    assert len(получено) == 1
    путь, данные = получено[0]
    assert "%D0%BF" in путь, "адрес с кириллицей не закодирован"
    assert данные["sig"], "отказ ушёл без подписи"
    assert данные["body"]["status"] == "failed" and данные["body"]["call_id"] == "1001"


# ---------------------------------------------------------------------------
# №20. Записи без заголовка, две дорожки, каталог записей
# ---------------------------------------------------------------------------

@pytest.mark.skipif(subprocess.run(["which", "ffmpeg"], capture_output=True).returncode,
                    reason="нужен ffmpeg")
def test_ulaw_перекодируется_а_пара_in_out_сводится_в_стерео(tmp_path: Path):
    from asrhub.pipeline.audio import probe

    db = база(tmp_path)
    журнал(tmp_path, cdr("1759100001.1", начало=ДАВНО),
           cdr("1759100002.1", начало=ДАВНО + 10))
    каталог = tmp_path / "monitor"
    исходник = wav(tmp_path / "звук.wav", секунд=2)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i",
                    str(исходник), "-f", "mulaw", "-ar", "8000", "-ac", "1",
                    str(каталог / "1759100001.1.ulaw")], check=True)
    os.utime(каталог / "1759100001.1.ulaw", (ДАВНО + 300, ДАВНО + 300))
    wav(каталог / "1759100002.1-in.wav", когда=ДАВНО + 310, секунд=2)
    wav(каталог / "1759100002.1-out.wav", когда=ДАВНО + 310, секунд=2)

    class Пути:
        uploads = tmp_path / "uploads"

    настройки = Настройки()
    настройки.paths = Пути()
    очередь = Очередь()
    свод = импортёр(db, tmp_path, очередь, настройки).scan()
    assert свод["imported"] == 2, свод
    файлы = {Path(в["filename"]).stem: Path(в["file_path"]) for в in очередь.вызовы}
    ulaw = файлы["1759100001.1"]
    assert ulaw.suffix == ".wav" and ulaw.parent == tmp_path / "uploads"
    assert probe(ulaw).duration_s == pytest.approx(2.0, abs=0.2)
    пара = next(п for имя, п in файлы.items() if имя.startswith("1759100002.1"))
    assert probe(пара).channels == 2, "сторона разговора потеряна"


def test_каталог_записей_как_источник_берёт_gsm_и_пару_одним_звонком(tmp_path: Path):
    db = база(tmp_path)
    каталог = tmp_path / "monitor"
    (каталог / "a.gsm").parent.mkdir(parents=True)
    (каталог / "a.gsm").write_bytes(b"\xd8" * 330)
    wav(каталог / "b-in.wav")
    wav(каталог / "b-out.wav")
    имп = импортёр(db, tmp_path, source="folder", lookback_days=0)
    имена = sorted(Path(з.recording).name for з in имп._из_папки(50, всё=True))
    assert имена == ["a.gsm", "b-in.wav"]


# ---------------------------------------------------------------------------
# №21. Разбор забора
# ---------------------------------------------------------------------------

def test_разбор_считает_звонки_с_записями_по_поставленным(tmp_path: Path):
    from asrhub.telephony.diagnose import диагноз

    db = база(tmp_path)
    журнал(tmp_path, cdr("1759200001.1", начало=ДАВНО))
    wav(tmp_path / "monitor" / "1759200001.1.wav", когда=ДАВНО + 300)
    имп = импортёр(db, tmp_path)
    имп.scan()
    архив = next(п for п in диагноз(имп)["checks"] if п["id"] == "archive")
    assert "с записями 1" in архив["value"]


def test_разбор_без_uniqueid_в_журнале_называет_loguniqueid(tmp_path: Path):
    from asrhub.telephony.diagnose import диагноз

    db = база(tmp_path)
    строка = cdr("1759300001.1", начало=ДАВНО)
    шестнадцать = ",".join(строка.strip().split(",")[:16]) + "\n"
    журнал(tmp_path, шестнадцать, шестнадцать)
    разбор = next(п for п in диагноз(импортёр(db, tmp_path))["checks"]
                  if п["id"] == "cdr_parse")
    assert разбор["state"] == "fail" and "loguniqueid" in разбор["hint"]


def test_разбор_ami_проверяет_записи_и_пороги_по_событиям(tmp_path: Path, monkeypatch):
    from asrhub.telephony import diagnose

    monkeypatch.setattr(diagnose, "СЛУШАТЬ", 1.5)

    def сценарий(conn, _srv):
        conn.sendall(событие_cdr("1759400001.1", начало=ДАВНО))
        time.sleep(3)

    сервер = ПоддельнаяAMI(сценарий)
    try:
        db = база(tmp_path)
        (tmp_path / "monitor").mkdir()
        wav(tmp_path / "monitor" / "1759400001.1.wav", когда=ДАВНО + 60)
        имп = импортёр(db, tmp_path, source="ami", host="127.0.0.1", port=сервер.port,
                       username="asrhub", secret="s3cret")
        проверки = {п["id"]: п for п in diagnose.диагноз(имп)["checks"]}
    finally:
        сервер.close()
    assert проверки["rec_match"]["state"] == "ok", проверки.get("rec_match")
    assert "filters" in проверки


# ---------------------------------------------------------------------------
# Недоделки: поздняя запись, CP1251, 17 колонок, перепроверка «нет записи»
# ---------------------------------------------------------------------------

def test_запись_приехавшая_позже_выдержки_подбирается(tmp_path: Path, monkeypatch):
    from asrhub.telephony.asterisk import УказательЗаписей

    # Между заходами здесь доли секунды, а не минута: указатель каталога
    # пересобирается по промаху не чаще раза в полминуты.
    monkeypatch.setattr(УказательЗаписей, "ПЕРЕСОБИРАТЬ_НЕ_ЧАЩЕ_С", 0.0)
    db = база(tmp_path)
    журнал(tmp_path, cdr("1759500001.1", начало=time.time() - 200, duration=100))
    имп = импортёр(db, tmp_path, settle_s=30)
    assert имп.scan()["reasons"] == {"записи ещё нет": 1}
    wav(tmp_path / "monitor" / "1759500001.1.wav", когда=time.time() - 100)
    assert имп.scan()["imported"] == 1


def test_имя_звонящего_в_cp1251_читается(tmp_path: Path):
    db = база(tmp_path)
    строка = cdr("1759600001.1", начало=ДАВНО, clid='"Иванов" <79161234567>')
    (tmp_path / "Master.csv").write_bytes(строка.encode("cp1251"))
    (tmp_path / "monitor").mkdir()
    импортёр(db, tmp_path).scan()
    [звонок] = звонки(db)
    assert "Иванов" in звонок["clid"]


def test_семнадцать_колонок_без_uniqueid_не_дают_ключ_из_userfield():
    строка = cdr("1759700001.1", начало=ДАВНО).strip().split(",")
    без_id = строка[:16] + ['"метка"']
    assert asterisk.разобрать_cdr_строку(",".join(без_id)) is None


def test_сбор_архива_перепроверяет_нет_записи(tmp_path: Path):
    db = база(tmp_path)
    журнал(tmp_path, cdr("1759800001.1", начало=ДАВНО))
    имп = импортёр(db, tmp_path)
    assert имп.scan()["reasons"] == {"нет записи": 1}
    wav(tmp_path / "monitor" / "1759800001.1.wav", когда=ДАВНО + 300)
    имп.собрать()
    _дождаться_сбора(имп)
    [звонок] = звонки(db)
    assert звонок["job_id"] and звонок["skipped"] == ""


def test_copytruncate_с_переросшим_файлом_не_читается_с_середины(tmp_path: Path):
    """Сервер стоял дольше периода ротации: файл обрезали и он уже длиннее.

    По одному размеру ротацию не видно, и чтение шло с середины новой порции:
    хвост старой и начало новой пропадали молча.
    """
    db = база(tmp_path)
    путь = журнал(tmp_path, *(cdr(f"1760000001.{н}", начало=ДАВНО + н) for н in range(3)))
    имп = импортёр(db, tmp_path)
    имп.scan()
    # Станция дописала ещё звонок, потом logrotate скопировал файл и обрезал.
    with путь.open("a", encoding="utf-8") as файл:
        файл.write(cdr("1760000001.9", начало=ДАВНО + 9))
    (tmp_path / "Master.csv.1").write_bytes(путь.read_bytes())
    путь.write_text("".join(cdr(f"1760000002.{н}", src="79990001122", начало=ДАВНО + 100 + н)
                            for н in range(8)), encoding="utf-8")
    for _ in range(3):
        имп.scan()
    ид = {з["pbx_uid"] for з in звонки(db)}
    assert "1760000001.9" in ид, "хвост старого файла потерян"
    assert {f"1760000002.{н}" for н in range(8)} <= ид, "начало нового файла потеряно"
