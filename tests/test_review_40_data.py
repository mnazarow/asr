"""Заход 40: потеря данных и то, что ломало работу прямо сейчас.

Повтор, который стирал готовую расшифровку; исходник, который удалялся из
архива станции; позиция журнала, уезжавшая за необработанные звонки;
восстановление базы под работающим сервером; фильтр галлюцинаций, который
вырезал настоящие реплики у модели по умолчанию; обрезка тишины, съедавшая
десятки гигабайт памяти на длинной записи.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from asrhub.db import Database

КОРЕНЬ = Path(__file__).resolve().parent.parent


def wav(путь: Path, секунд: float = 1.0, *, тон: bool = True) -> Path:
    import math
    import struct

    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(и / 8)) if тон else 0)
            for и in range(int(16000 * секунд))))
    return путь


def ждать(условие, срок: float = 30.0) -> bool:
    конец = time.time() + срок
    while time.time() < конец:
        if условие():
            return True
        time.sleep(0.1)
    return False


# ---------------------------------------------------------------------------
# Повтор не стирает готовый результат
# ---------------------------------------------------------------------------


def _готовое_задание(client, sample_wav: Path) -> str:
    with sample_wav.open("rb") as файл:
        ответ = client.post("/api/jobs", files={"file": ("a.wav", файл, "audio/wav")})
    assert ответ.status_code == 200, ответ.text
    ид = ответ.json()["id"]
    assert ждать(lambda: client.get(f"/api/jobs/{ид}").json()["status"] == "completed"), \
        client.get(f"/api/jobs/{ид}").json()
    return ид


def test_неудачный_повтор_возвращает_прежнюю_расшифровку(client, sample_wav, monkeypatch):
    """Повтор переделывал задание на месте и сносил каталог результата заранее.

    Движка нет на сервере, запись удалила АТС — и задание, вчера давшее
    расшифровку, уходило в «ошибку», а выгрузка отвечала 400.
    """
    from asrhub import job_queue
    from asrhub.errors import DependencyMissing

    ид = _готовое_задание(client, sample_wav)
    до = client.get(f"/api/jobs/{ид}/download?fmt=txt")
    assert до.status_code == 200, до.text

    def движка_нет(*a, **k):
        raise DependencyMissing("gigaam")

    monkeypatch.setattr(job_queue, "process_job", движка_нет)
    повтор = client.post(f"/api/jobs/{ид}/retry", json={"model": "gigaam-v3-ctc"})
    assert повтор.status_code == 200, повтор.text

    def закончилось():
        return client.get(f"/api/jobs/{ид}").json()["status"] in ("completed", "failed")
    assert ждать(закончилось)
    карточка = client.get(f"/api/jobs/{ид}").json()
    после = client.get(f"/api/jobs/{ид}/download?fmt=txt")
    виды = [с["kind"] for с in client.app.state.hub.db.query(
        "SELECT kind FROM events WHERE job_id=?", (ид,))]
    assert карточка["status"] == "completed", карточка
    assert после.status_code == 200 and после.text == до.text
    assert "rescan_reverted" in виды


def test_удачный_повтор_не_оставляет_отложенный_каталог(client, sample_wav):
    ид = _готовое_задание(client, sample_wav)
    повтор = client.post(f"/api/jobs/{ид}/retry", json={"beam_size": 3})
    assert повтор.status_code == 200, повтор.text
    time.sleep(0.2)
    assert ждать(lambda: client.get(f"/api/jobs/{ид}").json()["status"] == "completed")
    результаты = Path(client.app.state.hub.settings.paths.results)
    assert (результаты / ид).is_dir()
    assert not (результаты / f"{ид}.prev").exists()


def test_повтор_проверяет_переопределения(client, sample_wav):
    """/retry клал в задание всё, что пришло в теле, как есть.

    `{"beam_size": "мусор"}` принималось с ответом 200 и роняло задание уже в
    очереди, а служебный ключ с чужим идентификатором писал в журнал чужого
    задания.
    """
    ид = _готовое_задание(client, sample_wav)
    мусор = client.post(f"/api/jobs/{ид}/retry", json={"beam_size": "мусор"})
    чужое = client.post(f"/api/jobs/{ид}/retry", json={"control_of": "j-чужой"})
    assert мусор.status_code == 400, мусор.text
    assert чужое.status_code == 400, чужое.text


def test_идущее_задание_повторять_нельзя(tmp_path: Path):
    from asrhub.errors import ConfigError
    from asrhub.job_queue import JobQueue

    class _Настройки(dict):
        paths = type("П", (), {"results": str(tmp_path / "r"), "uploads": str(tmp_path / "u")})

        def get(self, к, з=None):
            return dict.get(self, к, з)

    база = Database(tmp_path / "asrhub.db")
    очередь = JobQueue.__new__(JobQueue)
    очередь.db = база
    очередь.settings = _Настройки()
    база.create_job({"id": "j", "filename": "a.wav", "status": "running"})
    try:
        with pytest.raises(ConfigError):
            очередь.retry("j")
        assert база.get_job("j")["status"] == "running"
    finally:
        база.close()


# ---------------------------------------------------------------------------
# Исходник удаляется только свой и только из загрузок
# ---------------------------------------------------------------------------


def test_удаление_исходника_не_трогает_архив_станции(client, tmp_path):
    """delete_source_after удалял любой путь задания.

    Импорт с АТС ставит в очередь оригинал записи прямо в архиве станции, и
    с включённой настройкой каждое распознанное задание стирало разговор из
    архива Asterisk.
    """
    state = client.app.state.hub
    архив = wav(tmp_path / "monitor" / "звонок.wav", 2)
    загрузка = wav(Path(state.settings.paths.uploads) / "up_x.wav", 2)
    общий = wav(Path(state.settings.paths.uploads) / "up_общий.wav", 2)
    for ид, путь in (("j-архив", архив), ("j-загрузка", загрузка),
                     ("j-общий-1", общий), ("j-общий-2", общий)):
        state.db.create_job({"id": ид, "filename": путь.name, "status": "completed",
                             "file_path": str(путь)})
    очередь = state.queue
    for ид in ("j-архив", "j-загрузка", "j-общий-1"):
        очередь._удалить_исходник(state.db.get_job(ид))
    assert архив.exists(), "запись в архиве станции удалена"
    assert not загрузка.exists(), "загруженный файл должен удаляться"
    assert общий.exists(), "файл, нужный другому заданию, удалён"


def test_общий_файл_знает_о_соседях(tmp_path: Path):
    """Контрольный прогон второй моделью идёт по записи исходного задания."""
    база = Database(tmp_path / "asrhub.db")
    try:
        общий = wav(tmp_path / "uploads" / "общий.wav")
        for ид in ("a", "b"):
            база.create_job({"id": ид, "filename": "x", "status": "completed",
                             "file_path": str(общий)})
        assert база.file_used_elsewhere(str(общий), "a")
        база.delete_job("b")
        assert not база.file_used_elsewhere(str(общий), "a")
    finally:
        база.close()


def test_удаление_задания_через_api_не_трогает_общий_файл(client, tmp_path):
    state = client.app.state.hub
    общий = wav(Path(state.settings.paths.uploads) / "up_общий.wav", 2)
    for ид in ("j-основное", "j-контроль"):
        state.db.create_job({"id": ид, "filename": "x.wav", "status": "completed",
                             "file_path": str(общий)})
    ответ = client.delete("/api/jobs/j-контроль")
    assert ответ.status_code == 200, ответ.text
    assert общий.exists(), "удаление контрольного прогона снесло запись основного задания"
    client.delete("/api/jobs/j-основное")
    assert not общий.exists(), "последнее задание уносит файл с собой"


# ---------------------------------------------------------------------------
# Восстановление базы — при запуске, а не под работающим сервером
# ---------------------------------------------------------------------------


def test_восстановление_базы_применяется_при_следующем_запуске(data_dir, monkeypatch):
    """База подменялась, пока процесс держал её открытой.

    Сервер продолжал писать в удалённый файл, а новые задания пропадали
    после перезапуска; при неудаче подмены рабочей базы не оставалось вовсе.
    Теперь восстановление готовит копию рядом, а подменяет её запуск.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as клиент:
        db = app.state.hub.db
        db.create_job({"id": "j-до-копии", "filename": "a.wav", "status": "completed"})
        копия = клиент.post("/api/backup?kind=full").json()
        db.create_job({"id": "j-после-копии", "filename": "b.wav", "status": "completed"})
        итог = клиент.post("/api/backup/restore",
                           json={"name": копия["name"], "what": "full"}).json()
        assert итог["restart_required"] is True
        # Под работающим сервером база не подменилась: он её держит.
        assert db.get_job("j-после-копии") is not None
    app2 = create_app(load(), start_queue=False)
    with TestClient(app2):
        db2 = app2.state.hub.db
        assert db2.get_job("j-до-копии") is not None
        assert db2.get_job("j-после-копии") is None, "база не восстановилась при запуске"
        события = db2.query("SELECT kind FROM events WHERE kind='restore_applied'")
    assert события, "запуск не сказал, что поставил восстановленную базу"
    assert list(Path(data_dir).glob("asrhub.db.before-restore-*")), "прежняя база исчезла"


def test_копии_закрыты_от_посторонних(tmp_path: Path):
    """В копии — вся база разговоров, ключи и токен: 0600, каталог 0700."""
    from asrhub import backup

    class _Настройки(dict):
        def __init__(self):
            super().__init__({"backup_dir": str(tmp_path / "backups"), "backup_keep": 0,
                              "backup_keep_days": 0, "backup_include_results": False})
            self.paths = type("П", (), {"data": str(tmp_path),
                                        "results": str(tmp_path / "results")})
            self.config_file = None
            self.sources = {}

        def get(self, к, з=None):
            return dict.get(self, к, з)

    база = Database(tmp_path / "asrhub.db")
    try:
        копия = backup.создать(база, _Настройки(), kind="full")
    finally:
        база.close()
    каталог = tmp_path / "backups"
    assert каталог.stat().st_mode & 0o777 == 0o700
    assert (каталог / копия["name"]).stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="нужны права root: проверяется смена владельца")
def test_восстановление_от_root_отдаёт_базу_службе(tmp_path: Path):
    """`sudo service.sh restore` оставлял базу root:root.

    Служба от своего пользователя её только читала: «attempt to write a
    readonly database» на первом же задании, а /health при этом отвечал.
    """
    from asrhub import maintenance

    данные = tmp_path / "data"
    данные.mkdir()
    os.chown(данные, 65534, 65534)
    исходная = Database(tmp_path / "копия.db")
    исходная.close()
    maintenance.restore(tmp_path / "копия.db", данные / "asrhub.db")
    assert (данные / "asrhub.db").stat().st_uid == 65534


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0,
                    reason="нужны права root: проверяется смена владельца")
def test_копия_от_root_не_закрывает_каталог_копий(tmp_path: Path):
    """Первый `sudo service.sh backup` создавал каталог копий от root.

    Все следующие копии по расписанию падали на правах каталога.
    """
    from asrhub import maintenance

    данные = tmp_path / "data"
    данные.mkdir()
    os.chown(данные, 65534, 65534)

    class _Настройки(dict):
        def __init__(self):
            super().__init__({"backup_keep": 0})
            self.paths = type("П", (), {"data": str(данные)})

        def get(self, к, з=None):
            return dict.get(self, к, з)

    база = Database(данные / "asrhub.db")
    try:
        копия = maintenance.make_backup(база, _Настройки())
    finally:
        база.close()
    assert копия is not None
    assert (данные / "backups").stat().st_uid == 65534
    assert копия.stat().st_uid == 65534


# ---------------------------------------------------------------------------
# Телефония: позиция журнала и сбор за период
# ---------------------------------------------------------------------------


def _строка_cdr(uid: str, начало: float, src: str = "79161234567", dst: str = "101") -> str:
    н = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало))
    к = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало + 300))
    return (f'"","{src}","{dst}","from-trunk","""Клиент"" <{src}>","SIP/tr-01","SIP/{dst}",'
            f'"Queue","sales,t","{н}","{н}","{к}",300,290,"ANSWERED","DOCUMENTATION",'
            f'"{uid}",""\n')


class _Очередь:
    def __init__(self):
        self.вызовы: list[dict] = []

    def submit(self, **kwargs):
        self.вызовы.append(kwargs)
        return {"id": f"job_{len(self.вызовы)}"}


class _Настройки(dict):
    def get(self, ключ, по_умолчанию=None):        # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def merged(self, поверх):
        return {**self, **(поверх or {})}


def _импортёр(tmp_path: Path, db: Database, очередь: _Очередь):
    from asrhub.telephony import Импортёр
    from asrhub.telephony.stations import _станция_из

    станция = _станция_из({
        "id": "pbx", "name": "АТС", "enabled": True, "source": "cdr_csv",
        "cdr_file": str(tmp_path / "Master.csv"),
        "recordings_dir": str(tmp_path / "monitor"),
        "settle_s": 0, "min_duration_s": 10, "skip_unanswered": True,
        "internal_digits": 5, "contexts": {"from-trunk": "входящий"},
        "owner": "telephony", "owner_map": {}, "priority": 40,
        "lookback_days": 7, "filename": "", "poll_s": 60}, 0)
    return Импортёр(db, станция, очередь, _Настройки({}))


def test_перезапуск_посреди_захода_не_теряет_звонки(tmp_path: Path):
    """Позиция журнала сдвигалась на всю порцию ДО её обработки.

    На звонок уходит 0,4 с проверки роста файла, порция из пятидесяти —
    двадцать секунд, а остановка ждёт поток пять. Перезапуск посреди захода
    терял необработанный остаток навсегда: из журнала он больше не придёт.
    """
    сейчас = time.time() - 900
    (tmp_path / "Master.csv").write_text(
        "".join(_строка_cdr(f"c.{i}", сейчас + i) for i in range(3)), encoding="utf-8")
    for i in range(3):
        wav(tmp_path / "monitor" / f"c.{i}.wav")
    db = Database(tmp_path / "asrhub.db")
    очередь = _Очередь()
    try:
        имп = _импортёр(tmp_path, db, очередь)
        настоящий = имп._взять

        def прервать_на_втором(звонок, **kwargs):
            if звонок.uniqueid == "c.1":
                raise KeyboardInterrupt("служба остановлена")
            return настоящий(звонок, **kwargs)

        имп._взять = прервать_на_втором
        with pytest.raises(KeyboardInterrupt):
            имп.scan()
        # Новый процесс — новый импортёр над той же базой.
        _импортёр(tmp_path, db, очередь).scan()
        принятые = {з["pbx_uid"] for з in db.list_calls()["calls"]}
    finally:
        db.close()
    assert принятые == {"c.0", "c.1", "c.2"}


def test_сбор_за_период_не_сдвигает_позицию_обычного_захода(tmp_path: Path):
    """Сбор за период обнулял общую позицию и дочитывал журнал до конца.

    Звонки вне окна он пропускал без пометки — и всё, до чего обычный заход
    ещё не дошёл, обычный заход больше не видел никогда.
    """
    сейчас = time.time() - 900
    (tmp_path / "Master.csv").write_text(
        _строка_cdr("давний", сейчас - 5 * 86400)
        + _строка_cdr("свежий-1", сейчас) + _строка_cdr("свежий-2", сейчас + 1),
        encoding="utf-8")
    for имя in ("давний", "свежий-1", "свежий-2"):
        wav(tmp_path / "monitor" / f"{имя}.wav")
    db = Database(tmp_path / "asrhub.db")
    очередь = _Очередь()
    try:
        имп = _импортёр(tmp_path, db, очередь)
        окно = (сейчас - 6 * 86400, сейчас - 4 * 86400)
        итог = имп.scan(limit=500, всё=True, окно=окно)
        assert итог["imported"] == 1 and итог["outside"] == 2
        обычный = имп.scan()
        принятые = {з["pbx_uid"] for з in db.list_calls()["calls"]}
    finally:
        db.close()
    assert обычный["imported"] == 2, обычный
    assert принятые == {"давний", "свежий-1", "свежий-2"}


# ---------------------------------------------------------------------------
# Агент на станции
# ---------------------------------------------------------------------------


def _агент():
    описание = importlib.util.spec_from_file_location(
        "asrhub_agent_r40", str(КОРЕНЬ / "agent" / "asrhub-agent.py"))
    модуль = importlib.util.module_from_spec(описание)
    описание.loader.exec_module(модуль)
    return модуль


def test_mysql_видит_длинный_звонок_начатый_до_позиции(tmp_path: Path):
    """calldate — время НАЧАЛА, а строку Asterisk пишет по ОКОНЧАНИИ.

    Получасовой разговор, начатый в 10:00, появляется в таблице в 10:30,
    когда позиция уже ушла дальше за короткими звонками. Отбор «calldate >=
    позиции» не видел его никогда — терялись как раз самые длинные разговоры.
    """
    агент = _агент()
    (tmp_path / "state").mkdir()
    конф = tmp_path / "cdr_mysql.conf"
    конф.write_text("[global]\ndbname=cdrdb\ntable=cdr\n", encoding="utf-8")
    состояние = агент.Состояние(str(tmp_path / "state" / "state.json"))
    источник = агент.ИсточникMySQL.из_конфига_asterisk(
        str(конф), состояние, поверх={}, каталог_временных=str(tmp_path / "state"))
    состояние.запомнить_mysql("2026-09-10 14:00:00", "позиция")
    состояние.отметить("уже-отправлен")
    запросы: list[str] = []
    поля = ["", "79161234567", "101", "from-trunk", "Клиент", "SIP/tr", "SIP/101",
            "Queue", "sales", "{начало}", "1800", "1790", "ANSWERED", "{uid}", ""]

    def строка(начало: str, uid: str) -> str:
        return "\t".join(п.format(начало=начало, uid=uid) for п in поля)

    def выполнить(запрос):
        запросы.append(запрос)
        return "\n".join([строка("2026-09-10 13:40:00", "уже-отправлен"),
                          строка("2026-09-10 13:45:00", "длинный")])

    источник._выполнить = выполнить
    звонки = [з.uniqueid for з in источник.звонки()]
    assert "calldate >= '2026-09-10 08:00:00'" in запросы[0], запросы[0]
    assert звонки == ["длинный"], "отправленный в окне запаса пришёл повторно"


def test_временный_отказ_сервера_не_теряет_звонок(tmp_path: Path):
    """Очередь сервера полна (429) или он перезапускается (503).

    Повторы клиента исчерпаны — и звонок считался «ошибкой», заход шёл
    дальше. Позиция журнала уже сдвинута, в отложенные он не попадал: звонок
    не приезжал больше никогда.
    """
    агент = _агент()
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    принятые: list[str] = []

    class Сервер(BaseHTTPRequestHandler):
        def log_message(self, *а):
            return

        def _ответ(self, код, данные):
            тело = json.dumps(данные).encode()
            self.send_response(код)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

        def do_GET(self):  # noqa: N802
            self._ответ(200, {"status": "ok", "command": "", "server_time": time.time()})

        def do_POST(self):  # noqa: N802
            длина = int(self.headers.get("Content-Length") or 0)
            тело = self.rfile.read(длина) if длина else b""
            if self.path.startswith("/api/telephony/agent/hello"):
                self._ответ(200, {"agent_id": "pbx-t", "station": "agent:pbx-t",
                                  "server_time": time.time(), "max_file_mb": 100,
                                  "want_audio": True, "command": "", "enabled": True,
                                  "min_duration_s": 0, "skip_unanswered": False})
            elif self.path.startswith("/api/telephony/agent/ask"):
                ids = json.loads(тело or b"{}").get("ids") or []
                self._ответ(200, {"wanted": ids, "known": 0})
            else:
                текст = тело.decode("utf-8", "replace")
                for uid in ("p.1", "p.2", "p.3"):
                    if f'"uniqueid": "{uid}"' in текст or f'"uniqueid":"{uid}"' in текст:
                        принятые.append(uid)
                        break
                self._ответ(200, {"ok": True, "job_id": "j"})

    сервер = ThreadingHTTPServer(("127.0.0.1", 0), Сервер)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        начало = time.time() - 900
        строки = ""
        for i, uid in enumerate(("p.1", "p.2", "p.3")):
            н = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало + i))
            к = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало + i + 300))
            строки += (f'"","79161234567","101","from-trunk","Клиент","SIP/tr","SIP/101",'
                       f'"Queue","sales","{н}","{н}","{к}",300,290,"ANSWERED",'
                       f'"DOCUMENTATION","{uid}",""\n')
        (tmp_path / "Master.csv").write_text(строки, encoding="utf-8")
        (tmp_path / "monitor").mkdir()
        разбор = агент.собрать_аргументы()
        аргументы = разбор.parse_args([
            "--url", f"http://127.0.0.1:{сервер.server_port}", "--key", "ah_test1234567890",
            "--state-dir", str(tmp_path / "state"), "--log-file", str(tmp_path / "a.log"),
            "--cdr-file", str(tmp_path / "Master.csv"),
            "--recordings-dir", str(tmp_path / "monitor"),
            "--source", "cdr_csv", "--retries", "1"])
        станция = агент.Агент(агент.собрать_параметры(аргументы, {}))
        станция.подготовиться()
        исходная = станция.клиент.отправить
        отказано: list[int] = []

        def отказ_на_втором(поля, путь=None):
            if поля.get("uniqueid") == "p.2" and not отказано:
                отказано.append(1)
                raise агент.ОтказСервера(503, "queue_full", "очередь полна")
            return исходная(поля, путь)

        станция.клиент.отправить = отказ_на_втором
        try:
            станция.поздороваться()
            with pytest.raises(агент.ОтказСервера):
                станция.проход("new")
            станция.проход("new")
        finally:
            станция.закрыть()
    finally:
        сервер.shutdown()
    assert sorted(принятые) == ["p.1", "p.2", "p.3"], принятые


# ---------------------------------------------------------------------------
# Две дорожки телефонного разговора
# ---------------------------------------------------------------------------


def test_две_дорожки_разной_длины_дают_полную_стереозапись(tmp_path: Path):
    """amerge кончается вместе с КОРОТКОЙ дорожкой.

    Оператор говорит десять минут, клиент — три: распознавались три, а
    остальные семь молча терялись вместе с длительностью в обратном вызове.
    """
    from asrhub import phone_compat
    from asrhub.pipeline.audio import probe

    длинная = wav(tmp_path / "оператор.wav", 10)
    короткая = wav(tmp_path / "клиент.wav", 3)
    итог = phone_compat._merge_to_stereo(длинная, короткая, tmp_path / "стерео.wav")
    сведения = probe(итог)
    assert сведения.channels == 2
    assert сведения.duration_s == pytest.approx(10.0, abs=0.1)


# ---------------------------------------------------------------------------
# Постобработка: фильтр галлюцинаций и имена сторон
# ---------------------------------------------------------------------------


РЕПЛИКИ = ["алло алло алло", "да да да", "всем пока", "до новых встреч",
           "продолжение следует завтра"]


def _реплики() -> list[dict[str, Any]]:
    return [{"start": float(i), "end": float(i) + 0.9, "text": т, "speaker": "S0"}
            for i, т in enumerate(РЕПЛИКИ)]


def test_фильтр_галлюцинаций_не_режет_речь_у_gigaam():
    """На модели по умолчанию из звонка пропадали настоящие реплики.

    Фильтр объявлен в каталоге только для Whisper — для других движков
    интерфейс его не показывает и выключить его нельзя, — а применялся ко
    всем. Из пяти реплик звонка удалялись три.
    """
    from asrhub.pipeline import postprocess

    настройки = {"hallucination_filter": True, "punctuation_enabled": False,
                 "itn_enabled": False, "merge_short_segments": False}
    итог, свод = postprocess.process(_реплики(), настройки, engine="gigaam")
    тексты = [с["text"].lower().strip(" .") for с in итог]
    assert свод["hallucinations_removed"] == 0
    assert "алло алло алло" in тексты and "всем пока" in тексты
    assert "да да да" in тексты, "повтор в речи — это повтор в речи"


def test_у_whisper_фильтр_по_прежнему_работает():
    from asrhub.pipeline import postprocess

    настройки = {"hallucination_filter": True, "punctuation_enabled": False,
                 "itn_enabled": False, "merge_short_segments": False}
    итог, свод = postprocess.process(_реплики(), настройки, engine="faster_whisper")
    assert свод["hallucinations_removed"] >= 3


def test_имена_сторон_по_каналу_а_не_по_первой_реплике():
    """Контракт phone_asr: первый канал — всегда SPEAKER_00.

    Имена шли по порядку первой реплики, и в исходящем звонке, где первым
    звучит «Алло» абонента из второго канала, речь клиента уходила в
    обратный вызов под меткой оператора. `swap_sides` менял местами не
    каналы, а «кто заговорил первым».
    """
    from asrhub.pipeline import postprocess

    сегменты = [
        {"start": 0.5, "end": 1.9, "text": "алло", "speaker": "Канал 2"},
        {"start": 2.0, "end": 4.0, "text": "здравствуйте компания", "speaker": "Канал 1"},
    ]
    настройки = {"speaker_names": "SPEAKER_00,SPEAKER_01", "punctuation_enabled": False,
                 "itn_enabled": False, "merge_short_segments": False,
                 "hallucination_filter": False}
    итог, _ = postprocess.process(сегменты, настройки, engine="gigaam")
    по_тексту = {с["text"]: с["speaker"] for с in итог}
    assert по_тексту["алло"] == "SPEAKER_01"
    assert по_тексту["здравствуйте компания"] == "SPEAKER_00"


def test_метки_диаризации_по_прежнему_по_первой_реплике():
    from asrhub.pipeline.postprocess import _speaker_mapping

    сегменты = [{"speaker": "SPEAKER_07"}, {"speaker": "SPEAKER_02"}]
    assert _speaker_mapping(сегменты, ["Ведущий", "Гость"]) == \
        {"SPEAKER_07": "Ведущий", "SPEAKER_02": "Гость"}


# ---------------------------------------------------------------------------
# Диаризация pyannote 4 и устройство
# ---------------------------------------------------------------------------


class _Отрезок:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Разметка:
    def __init__(self, дорожки):
        self._дорожки = дорожки

    def itertracks(self, yield_label=False):
        for (начало, конец), кто in self._дорожки:
            yield _Отрезок(начало, конец), None, кто


class _Итог4:
    """Ответ pyannote.audio 4: набор разметок, а не разметка."""

    def __init__(self):
        self.speaker_diarization = _Разметка([((0.0, 2.0), "A"), ((1.5, 3.0), "B")])
        self.exclusive_speaker_diarization = _Разметка([((0.0, 1.5), "A"), ((1.5, 3.0), "B")])


def _конвейер(версия: int):
    class Конвейер:
        вызовы: list[dict] = []

        if версия == 4:
            @classmethod
            def from_pretrained(cls, checkpoint, revision=None, hparams_file=None,
                                subfolder=None, token=None, cache_dir=None):
                cls.вызовы.append({"token": token})
                return cls()

            def __call__(self, путь, **kwargs):
                return _Итог4()
        else:
            @classmethod
            def from_pretrained(cls, checkpoint_path, hparams_file=None,
                                use_auth_token=None, cache_dir=None):
                cls.вызовы.append({"use_auth_token": use_auth_token})
                return cls()

            def __call__(self, путь, **kwargs):
                return _Разметка([((0.0, 1.0), "A")])

        def to(self, устройство):
            return self

    return Конвейер


@pytest.mark.parametrize("версия", [3, 4])
def test_pyannote_обеих_веток_грузится_с_токеном(monkeypatch, tmp_path, версия):
    """В pyannote.audio 4 нет use_auth_token — только token.

    Вызов падал с TypeError, и диаризация через pyannote не работала никогда:
    конвейер уходил к Sortformer или к разбивке по паузам. А ответ 4.x — не
    разметка, и itertracks у него нет вовсе.
    """
    import sys
    import types

    from asrhub.pipeline import diarization

    модуль = types.ModuleType("pyannote.audio")
    модуль.Pipeline = _конвейер(версия)
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", модуль)
    дорожки = diarization._pyannote(tmp_path / "a.wav",
                                    {"hf_token": "hf_проба", "device": "cpu"})
    assert модуль.Pipeline.вызовы, "конвейер не загружен"
    assert "hf_проба" in модуль.Pipeline.вызовы[0].values()
    if версия == 4:
        # Для расшифровки — исключающая разметка: у слова один говорящий.
        assert дорожки == [(0.0, 1.5, "A"), (1.5, 3.0, "B")]
    else:
        assert дорожки == [(0.0, 1.0, "A")]


@pytest.mark.parametrize("задано,ждём", [
    ("rocm", "cuda"), ("hip", "cuda"), ("rocm:1", "cuda:1"), ("ROCm", "cuda"),
    ("cuda:0", "cuda:0"), ("cpu", "cpu"),
])
def test_rocm_для_torch_это_cuda(задано, ждём):
    """ROCm-сборка PyTorch называет карту AMD «cuda», а «rocm» не принимает.

    Установщик пишет `device: rocm` на машине с AMD, и каждое задание GigaAM,
    Whisper и NeMo падало при загрузке модели.
    """
    from asrhub.engines.base import device_for

    assert device_for({"device": задано}) == ждём


# ---------------------------------------------------------------------------
# Тишина по краям — без areverse
# ---------------------------------------------------------------------------


def _тишина_тон_тишина(путь: Path) -> Path:
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=2",
        "-f", "lavfi", "-i", "sine=f=440:d=3:sample_rate=16000",
        "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono:d=2.5",
        "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", str(путь)],
        check=True, capture_output=True, timeout=60)
    return путь


def test_обрезка_тишины_не_держит_запись_в_памяти():
    """`areverse` держит в памяти всю запись: 25 ГБ на двухчасовое совещание."""
    from asrhub.pipeline.audio import build_filter_chain

    цепочка = build_filter_chain({"audio_trim_silence": True, "audio_normalize": True})
    assert "areverse" not in цепочка


def test_тишина_по_краям_срезается_по_замеру(tmp_path: Path):
    from asrhub.pipeline import audio

    исходник = _тишина_тон_тишина(tmp_path / "t.wav")
    начало, конец = audio.silence_bounds(исходник, {"audio_trim_silence": True})
    assert начало == pytest.approx(2.0, abs=0.05)
    assert конец == pytest.approx(5.0, abs=0.05)
    готово = audio.prepare(исходник, tmp_path, {"audio_trim_silence": True,
                                                "audio_sample_rate": 16000})
    assert готово.offset_s == pytest.approx(2.0, abs=0.05)
    assert audio.probe(готово.channels[0][1]).duration_s == pytest.approx(3.0, abs=0.1)


def test_без_обрезки_запись_целиком(tmp_path: Path):
    from asrhub.pipeline import audio

    исходник = _тишина_тон_тишина(tmp_path / "t.wav")
    assert audio.silence_bounds(исходник, {"audio_trim_silence": False}) == (0.0, None)
    готово = audio.prepare(исходник, tmp_path, {"audio_sample_rate": 16000})
    assert audio.probe(готово.channels[0][1]).duration_s == pytest.approx(7.5, abs=0.1)


# ---------------------------------------------------------------------------
# Ответы языковой модели
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ответ,ждём", [
    ("Вопрос решен", "вопрос решён"), ("вопрос решён.", "вопрос решён"),
    ("«Вопрос решён»", "вопрос решён"), ("  ВОПРОС   РЕШЕН!  ", "вопрос решён"),
    ("Вопрос, решён", "вопрос решён"), ("вопрос: решен", "вопрос решён"),
])
def test_ё_и_знаки_не_выводят_ответ_из_списка(ответ, ждём):
    """«Вопрос решен» на пункт «вопрос решён» становился «неясно» с пометкой.

    Доля решённых в отчётах занижалась ровно на такие ответы.
    """
    from asrhub.llm.tasks import _из_списка

    assert _из_списка(ответ, ["вопрос решён", "перезвонить", "неясно"],
                      ("неясн",)) == (ждём, True)


@pytest.mark.parametrize("значение,ждём", [
    (True, True), (False, False), ("false", False), ("Да", True), ("нет", False),
    ("true", True), (1, True), (0, False), ("может быть", None), (None, None),
])
def test_да_нет_из_ответа_модели(значение, ждём):
    """`bool("false")` истинно: трекер со строкой «false» считался сработавшим."""
    from asrhub.llm.tasks import _да_нет

    assert _да_нет(значение) is ждём


class _КлиентМодели:
    """Модель, отвечающая строками вместо булевых значений — так бывает."""

    model = "проба"
    enabled = True

    def chat(self, system, prompt, *, kind="main", validate=None):
        if kind == "trackers":
            return json.dumps({"trackers": [
                {"id": "угроза", "fired": "false", "quote": ""},
                {"id": "жалоба", "fired": "Да", "quote": "буду жаловаться"}]},
                ensure_ascii=False)
        return json.dumps({"summary": ["клиент спросил про оплату", "оператор объяснил"],
                           "resolved": "true", "reason": "оплата", "outcome": "вопрос решен"},
                          ensure_ascii=False)


def test_трекер_со_строкой_false_не_срабатывает():
    """`bool("false")` истинно: в своде копились ложные срабатывания."""
    from asrhub.llm import tasks

    class _Настройки(dict):
        def get(self, к, з=None):
            return dict.get(self, к, з)

    итог = tasks.analyze(_КлиентМодели(), text="разговор", segments=[
        {"start": 0, "end": 5, "text": "буду жаловаться", "speaker": "Клиент"}],
        settings=_Настройки({"llm_tasks": ["summary", "outcome", "trackers"],
                             "llm_outcomes": ["вопрос решён", "неясно"],
                             "llm_reasons": ["оплата", "другое"],
                             "llm_trackers": [{"id": "угроза", "label": "угроза"},
                                              {"id": "жалоба", "label": "жалоба"}]}))
    сработали = {т["id"]: т["fired"] for т in итог["trackers"]}
    assert сработали == {"угроза": False, "жалоба": True}
    assert итог["resolved"] is True
    assert итог["outcome"] == "вопрос решён"
    assert итог["summary"] == "клиент спросил про оплату; оператор объяснил"


def test_резюме_списком_склеивается_а_не_превращается_в_repr():
    from asrhub.llm.tasks import _строка

    assert _строка(["клиент спросил про оплату", "оператор объяснил"]) == \
        "клиент спросил про оплату; оператор объяснил"


def test_только_трекеры_на_длинной_записи_не_ошибка(tmp_path: Path, monkeypatch):
    """Пустым считался любой разбор без резюме и исхода.

    Сервер, настроенный на одни трекеры, получал на каждой длинной записи
    «ошибку» из замечания «разбор по пересказам N частей», и свод такую
    запись выбрасывал.
    """
    from asrhub.llm import tasks
    from asrhub.llm.worker import LLMWorker

    база = Database(tmp_path / "asrhub.db")
    база.create_job({"id": "j", "filename": "a.wav", "status": "completed",
                     "text": "длинный разговор"})

    def разбор(*a, **k):
        return {"summary": None, "reason": None, "outcome": None, "resolved": None,
                "actions": None, "scorecard": None, "chunks": 2, "calls": 3,
                "trackers": [{"id": "t", "label": "угроза", "fired": False, "quote": ""}],
                "warnings": ["разбор по пересказам 2 частей"], "latency_ms": 10.0}

    monkeypatch.setattr(tasks, "analyze", разбор)

    class _Клиент:
        model = "проба"
        enabled = True

    class _Настройки(dict):
        def get(self, к, з=None):
            return dict.get(self, к, з)

    работник = LLMWorker.__new__(LLMWorker)
    работник.db = база
    работник.client = _Клиент()
    работник.settings = _Настройки({"llm_tasks": ["trackers"]})
    работник._сбои = {}
    try:
        итог = работник.analyze_job("j") if hasattr(работник, "analyze_job") else None
        if итог is None:
            pytest.skip("имя метода разбора изменилось")
        строка = база.llm_get("j")
    finally:
        база.close()
    assert not строка.get("error"), строка
    assert строка.get("warnings"), "замечание должно остаться замечанием"


# ---------------------------------------------------------------------------
# Свод «Голосовой аналитики»
# ---------------------------------------------------------------------------


def test_свод_модели_отдаёт_построчный_список_и_пары(client):
    """Раздел рисовал таблицы по полю rows, которого в ответе не было вовсе."""
    from asrhub.llm import tasks

    db = client.app.state.hub.db
    for номер, (причина, исход) in enumerate([("оплата", "решён"), ("оплата", "решён"),
                                              ("доставка", "перезвонить")]):
        ид = f"j-{номер}"
        db.create_job({"id": ид, "filename": f"{ид}.wav", "status": "completed",
                       "text": "текст", "owner": ""})
        db.llm_save(ид, tasks.VERSION, model="проба", summary=f"резюме {номер}",
                    reason=причина, reason_quote="цитата", outcome=исход,
                    outcome_quote="", resolved=исход == "решён",
                    actions=[{"what": "перезвонить", "who": "сотрудник", "when": "завтра"}],
                    trackers=[{"id": "t", "label": "угроза", "fired": номер == 2}],
                    scorecard=[{"id": "q", "question": "поздоровался?", "answer": "да"}],
                    chunks=1, calls=1, latency_ms=5.0)
    свод = client.get("/api/content/llm?period=all").json()
    assert свод["analyzed"] == 3
    assert len(свод["rows"]) == 3 and свод["rows"][0]["summary"].startswith("резюме")
    assert {(п["reason"], п["outcome"], п["records"]) for п in свод["pairs"]} == \
        {("оплата", "решён", 2), ("доставка", "перезвонить", 1)}
    assert свод["actions"] == 3 and len(свод["action_items"]) == 3
