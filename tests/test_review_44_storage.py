"""Заход 44: база — журнал, соседи, сжатие, указатели и порядок.

SQLite в режиме WAL держится на общей памяти одной машины, и два сервера на
разных машинах над каталогом на NFS портили базу, а документация обещала,
что «это работает». Простаивающий сосед был невидим. VACUUM строил копию
базы в памяти процесса, запирал соседей и оставлял журнал размером с базу.
Полоса разбора раз в пятнадцать секунд читала все расшифровки целиком.
Корзина «неделя» начиналась в четверг, «месяц» был тридцатью сутками.
Проверка оператора и её оценка заводились и затирались вдвоём.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from asrhub import fsinfo
from asrhub.db import Database, _порядок_заданий, fts_query
from asrhub.errors import ConfigError

ЧУЖАЯ_МАШИНА = "другая-машина"


def _отметить_соседа(db: Database, ид: str = f"{ЧУЖАЯ_МАШИНА}:4242", *,
                     журнал: str = "wal", давность: float = 0.0) -> None:
    """Отметка жизни соседнего сервера — так, как её ставит он сам."""
    db.execute("INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
               (f"instance:{ид}", json.dumps({"host": ид.rsplit(":", 1)[0],
                                             "pid": 4242, "journal": журнал}),
                time.time() - давность))


# ---------------------------------------------------------------------------
# На какой файловой системе лежит база
# ---------------------------------------------------------------------------

MOUNTINFO = """28 1 254:0 / / rw,relatime - ext4 /dev/vda rw
40 28 0:50 / /mnt/общие\\040данные rw,relatime shared:5 - nfs4 nas:/export rw,vers=4.2,local_lock=none,addr=10.0.0.2
41 28 0:51 / /mnt/old rw,relatime - nfs nas:/old rw,vers=3,nolock,addr=10.0.0.2
42 28 0:52 / /mnt/c rw - 9p drvfs rw,aname=drvfs
"""


def test_сетевая_фс_узнаётся_по_mountinfo():
    фс = fsinfo.файловая_система("/mnt/общие данные/asrhub/asrhub.db", mountinfo=MOUNTINFO)
    assert (фс.тип, фс.сетевая, фс.локальные_блокировки) == ("nfs4", True, False)
    assert фс.точка == "/mnt/общие данные", "восьмеричный пробел в точке не раскодирован"
    старое = fsinfo.файловая_система("/mnt/old/asrhub.db", mountinfo=MOUNTINFO)
    assert старое.сетевая and старое.локальные_блокировки, "nolock — блокировки только свои"


def test_точка_монтирования_самая_длинная_и_целая():
    """«/mnt/oldx» не лежит в «/mnt/old», хотя начинается так же."""
    assert fsinfo.файловая_система("/mnt/oldx/a.db", mountinfo=MOUNTINFO).тип == "ext4"
    assert fsinfo.файловая_система("/var/lib/a.db", mountinfo=MOUNTINFO).тип == "ext4"


def test_папка_виртуальной_машины_и_macos():
    вм = fsinfo.файловая_система("/mnt/c/data/asrhub.db", mountinfo=MOUNTINFO)
    assert вм.папка_вм and not вм.сетевая
    mount = ("/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)\n"
             "//user@nas/share on /Volumes/share (smbfs, nodev, nosuid, mounted by user)\n")
    сеть = fsinfo.файловая_система("/Volumes/share/asrhub", mount_macos=mount)
    assert сеть.сетевая and сеть.тип == "smbfs"
    assert fsinfo.файловая_система("/Users/a", mount_macos=mount).тип == "apfs"


# ---------------------------------------------------------------------------
# Режим журнала
# ---------------------------------------------------------------------------


def _режим_в_потоке(db: Database) -> tuple[str, int]:
    """Режим и synchronous у соединения другого потока."""
    итог: dict[str, Any] = {}

    def спросить() -> None:
        итог["режим"] = db.query_one("PRAGMA journal_mode")[0]
        итог["синхронно"] = int(db.query_one("PRAGMA synchronous")[0])

    поток = threading.Thread(target=спросить)
    поток.start()
    поток.join(10)
    return итог["режим"], итог["синхронно"]


def test_новая_база_в_wal_а_журнал_отката_по_настройке(tmp_path: Path):
    wal = Database(tmp_path / "wal.db")
    assert wal.journal_mode == "wal"
    assert _режим_в_потоке(wal) == ("wal", 1), "у WAL synchronous=NORMAL"
    откат = Database(tmp_path / "откат.db", journal_mode="delete")
    assert откат.journal_mode == "delete"
    # Каждое соединение каждого потока — в том же режиме; у журнала отката
    # NORMAL при сбое питания может испортить базу, поэтому FULL.
    assert _режим_в_потоке(откат) == ("delete", 2)


def test_без_просьбы_режим_файла_не_трогается(tmp_path: Path):
    """Сценарии командной строки открывают базу под работающим сервером.

    Прежде каждое соединение просило WAL: `service.sh backup` над базой
    сервера в режиме отката переключал бы её под ним.
    """
    путь = tmp_path / "a.db"
    Database(путь, journal_mode="delete").close()
    assert Database(путь).journal_mode == "delete"
    assert Database(путь, journal_mode="wal").journal_mode == "wal"
    assert Database(путь).journal_mode == "wal"


def test_режим_не_меняется_при_живом_соседе(tmp_path: Path, caplog):
    """Сосед со старым режимом и мы с новым видят базу по-разному."""
    путь = tmp_path / "a.db"
    db = Database(путь)
    _отметить_соседа(db)
    db.close()
    with caplog.at_level("ERROR", logger="asrhub.db"):
        попытка = Database(путь, journal_mode="delete")
    assert попытка.journal_mode == "wal"
    попытка.close()
    assert "не сменён" in caplog.text and ЧУЖАЯ_МАШИНА in caplog.text
    # Отметка умершего соседа гаснет — и режим меняется.
    db = Database(путь)
    _отметить_соседа(db, давность=3600)
    db.close()
    assert Database(путь, journal_mode="delete").journal_mode == "delete"


def test_неизвестный_режим_журнала_отвергается(tmp_path: Path):
    with pytest.raises(ConfigError):
        Database(tmp_path / "a.db", journal_mode="memory")


def test_запуск_предупреждает_о_wal_с_соседом_с_другой_машины(data_dir: Path):
    from asrhub.api import create_app
    from asrhub.config import load

    db = Database(data_dir / "asrhub.db")
    _отметить_соседа(db)
    db.close()
    приложение = create_app(load(), start_queue=False)
    база = приложение.state.hub.db
    события = [e for e in база.get_events() if e["kind"] == "storage_warning"]
    assert события and ЧУЖАЯ_МАШИНА in события[0]["message"]
    assert "db_journal_mode: delete" in события[0]["message"]
    свои = [э for э in база.instances() if э["self"]]
    assert свои and свои[0]["journal"] == "wal" and свои[0].get("version")


def test_запуск_видит_разные_режимы_у_соседей(data_dir: Path):
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.instance import HOSTNAME

    db = Database(data_dir / "asrhub.db")
    # Сосед на этой же машине — живой процесс (наш родитель), режим другой.
    import os
    _отметить_соседа(db, f"{HOSTNAME}:{os.getppid()}", журнал="delete")
    db.close()
    база = create_app(load(), start_queue=False).state.hub.db
    assert any("другой режим журнала" in e["message"] for e in база.get_events()
               if e["kind"] == "storage_warning")


# ---------------------------------------------------------------------------
# Отметки жизни экземпляров
# ---------------------------------------------------------------------------


def test_отметки_экземпляров(tmp_path: Path):
    from asrhub.instance import HOSTNAME, INSTANCE_ID

    db = Database(tmp_path / "a.db")
    db.instance_beat(version="проба")
    свои = db.instances()
    assert [с["instance"] for с in свои] == [INSTANCE_ID] and свои[0]["self"]
    assert свои[0]["version"] == "проба" and свои[0]["journal"] == "wal"
    # Своя машина, процесса нет — отметка не считается, даже свежая.
    _отметить_соседа(db, f"{HOSTNAME}:999999999")
    _отметить_соседа(db)
    _отметить_соседа(db, "третья-машина:7", давность=600)
    соседи = db.other_instances()
    assert [с["instance"] for с in соседи] == [f"{ЧУЖАЯ_МАШИНА}:4242"]
    _отметить_соседа(db, "древняя-машина:1", давность=30 * 86400)
    assert db.instances_prune() == 1
    assert db.get_kv("instance:древняя-машина:1") is None


def test_сервер_отмечается_и_снимает_отметку_при_остановке(data_dir: Path, monkeypatch):
    from asrhub import job_queue
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.instance import INSTANCE_ID
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setattr(job_queue, "HEARTBEAT_S", 0.2)
    приложение = create_app(load(), start_queue=True)
    база = приложение.state.hub.db
    ключ = f"instance:{INSTANCE_ID}"

    def когда() -> float:
        строка = база.query_one("SELECT ts FROM kv WHERE key=?", (ключ,))
        return float(строка["ts"]) if строка else 0.0

    with TestClient(приложение):
        первая = когда()
        assert первая, "сервер не отметился при запуске"
        # Отметка обновляется служебным потоком очереди, а не только при
        # старте: иначе через пять минут живой сервер считался бы умершим.
        конец = time.time() + 10
        while когда() <= первая and time.time() < конец:
            time.sleep(0.1)
        assert когда() > первая, "отметка жизни не обновляется"
    проверка = Database(data_dir / "asrhub.db")
    assert проверка.get_kv(f"instance:{INSTANCE_ID}") is None, \
        "после остановки соседи ещё пять минут считали бы сервер живым"


# ---------------------------------------------------------------------------
# VACUUM
# ---------------------------------------------------------------------------


def _база_с_мусором(путь: Path, строк: int = 400) -> Database:
    db = Database(путь)
    for и in range(строк):
        db.create_job({"id": f"j{и}", "filename": "a.wav", "status": "completed",
                       "text": "слово " * 400})
    for и in range(строк):
        db.delete_job(f"j{и}")
    return db


def test_vacuum_пропускается_при_соседе(tmp_path: Path):
    db = _база_с_мусором(tmp_path / "a.db", 20)
    _отметить_соседа(db)
    итог = db.vacuum()
    assert итог["done"] is False and "другие серверы" in итог["reason"]


def test_vacuum_пропускается_без_места(tmp_path: Path, monkeypatch):
    import shutil

    from asrhub import db as модуль

    db = _база_с_мусором(tmp_path / "a.db", 20)
    настоящее = shutil.disk_usage
    monkeypatch.setattr(модуль.shutil, "disk_usage",
                        lambda путь: настоящее(путь)._replace(free=1024))
    итог = db.vacuum()
    assert итог["done"] is False and "не хватит места" in итог["reason"]


def test_vacuum_строит_копию_на_диске_и_усекает_журнал(tmp_path: Path):
    db = _база_с_мусором(tmp_path / "a.db")
    выполнено: list[str] = []
    db.conn.set_trace_callback(выполнено.append)
    итог = db.vacuum()
    db.conn.set_trace_callback(None)
    assert итог["done"] is True, итог
    сжатие = выполнено.index("VACUUM")
    assert "PRAGMA temp_store=FILE" in выполнено[:сжатие], \
        "копия базы строилась в памяти процесса, где лежат модели"
    assert int(db.query_one("PRAGMA temp_store")[0]) == 2, "память не вернули"
    журнал = Path(str(tmp_path / "a.db") + "-wal")
    assert not журнал.exists() or журнал.stat().st_size == 0, \
        "рядом с базой остался журнал размером с перестроенное"
    assert итог["after_bytes"] < итог["before_bytes"]


def test_очистка_по_кнопке_отвечает_итогом_сжатия(client):
    ответ = client.post("/api/maintenance/cleanup")
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["vacuum"]["done"] is True and "sweep" in тело


# ---------------------------------------------------------------------------
# Указатели: полоса разбора не читает расшифровки
# ---------------------------------------------------------------------------


def _планы(db: Database, вызов) -> list[str]:
    """Планы запросов, которые выполнил вызов."""
    запросы: list[str] = []
    db.conn.set_trace_callback(запросы.append)
    try:
        вызов()
    finally:
        db.conn.set_trace_callback(None)
    планы = []
    for sql in запросы:
        if sql.lstrip().upper().startswith("SELECT"):
            планы.append(" | ".join(str(r[3]) for r in db.conn.execute(
                "EXPLAIN QUERY PLAN " + sql)))
    return планы


def _читает_текст(db: Database, вызов) -> list[str]:
    """Запросы вызова, в байт-коде которых есть чтение `jobs.text`.

    План («USING INDEX») этого не показывает: указатель с условием слабее
    запроса тоже годится, и тогда `text != ''` проверяется чтением строки —
    то есть всей расшифровки. Байт-код показывает: `Column` курсора таблицы
    заданий с номером колонки текста.
    """
    запросы: list[str] = []
    db.conn.set_trace_callback(запросы.append)
    try:
        вызов()
    finally:
        db.conn.set_trace_callback(None)
    корень = int(db.query_one("SELECT rootpage FROM sqlite_master WHERE name='jobs'")[0])
    номер = [int(r[0]) for r in db.query("PRAGMA table_info(jobs)") if r[1] == "text"][0]
    читают = []
    for sql in запросы:
        if not sql.lstrip().upper().startswith("SELECT"):
            continue
        код = db.conn.execute("EXPLAIN " + sql).fetchall()
        курсоры = {r[2] for r in код if r[1] == "OpenRead" and r[3] == корень}
        if any(r[1] == "Column" and r[2] in курсоры and r[3] == номер for r in код):
            читают.append(sql)
    return читают


def test_полоса_разбора_идёт_по_частичному_указателю(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    for и in range(30):
        db.create_job({"id": f"j{и}", "filename": "a.wav", "status": "completed",
                       "text": "слово " * 50, "owner": "отдел"})
    for план in _планы(db, lambda: (db.content_pending(8), db.content_stats(8),
                                     db.content_stats(8, owner=["отдел", "другой"]))):
        assert "idx_jobs_text_ready" in план, план
    # Счётчики полосы — вообще без чтения расшифровок.
    assert _читает_текст(db, lambda: (db.content_stats(8),
                                      db.content_stats(8, owner=["отдел"]))) == []
    # И ответ по существу: пустой текст и контрольный прогон не в счёт.
    db.create_job({"id": "пустое", "filename": "a.wav", "status": "completed", "text": ""})
    db.create_job({"id": "контроль", "filename": "a.wav", "status": "completed",
                   "text": "слово", "source": "control"})
    assert db.content_stats(8)["total"] == 30
    assert {з["id"] for з in db.content_pending(8, limit=100)} == {f"j{и}" for и in range(30)}


def test_указатели_уборки_на_месте(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    есть = {r[0] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {"idx_jobs_finished", "idx_calls_imported", "idx_metrics_ts",
            "idx_llm_cache_created", "idx_llm_cache_job", "idx_jobs_text_ready"} <= есть
    план = " | ".join(str(r[3]) for r in db.query(
        "EXPLAIN QUERY PLAN DELETE FROM llm_cache WHERE created_at<?", (1.0,)))
    assert "idx_llm_cache_created" in план, план


# ---------------------------------------------------------------------------
# Колонки — из самой схемы
# ---------------------------------------------------------------------------


def test_пропавшая_колонка_дописывается_в_любой_таблице(tmp_path: Path):
    """Набор колонок был написан руками, и сотрудников в нём не было."""
    from asrhub.db import _EXPECTED_COLUMNS

    assert {"employees", "agents", "llm_queue"} <= set(_EXPECTED_COLUMNS)
    assert _EXPECTED_COLUMNS["llm_cache"]["job_id"] == "TEXT"
    путь = tmp_path / "a.db"
    Database(путь).close()
    with sqlite3.connect(путь) as соединение:
        соединение.execute("ALTER TABLE employees DROP COLUMN note")
        соединение.execute("ALTER TABLE llm_queue DROP COLUMN heartbeat_at")
    db = Database(путь)
    assert "note" in {r[1] for r in db.query("PRAGMA table_info(employees)")}
    assert "heartbeat_at" in {r[1] for r in db.query("PRAGMA table_info(llm_queue)")}


# ---------------------------------------------------------------------------
# Миграция ключей звонков: задание прежней строки главнее
# ---------------------------------------------------------------------------


def test_миграция_ключей_звонков_переносит_задание_прежней_строки(tmp_path: Path, caplog):
    путь = tmp_path / "a.db"
    db = Database(путь)
    for ид in ("прежнее", "дубль"):
        db.create_job({"id": ид, "filename": "1789.1.wav", "status": "completed"})
    db.execute("INSERT INTO calls (uniqueid, job_id, station, pbx_uid, src) "
               "VALUES ('1789.1', 'прежнее', 'pbx', '1789.1', '79161234567')")
    db.execute("INSERT INTO calls (uniqueid, job_id, station, pbx_uid, src) "
               "VALUES ('pbx:1789.1', 'дубль', 'pbx', '1789.1', '79161234567')")
    db.execute("PRAGMA user_version=23")
    db.close()
    with caplog.at_level("WARNING", logger="asrhub.db"):
        db = Database(путь)
    звонок = db.call_for_job("прежнее")
    assert звонок is not None and звонок["uniqueid"] == "pbx:1789.1", \
        "у исходной расшифровки потерялась связь со звонком"
    assert db.call_for_job("дубль") is None
    assert int(db.query_one("SELECT COUNT(*) FROM calls")[0]) == 1
    assert "дубль" in caplog.text, "повторное задание не названо в журнале"


# ---------------------------------------------------------------------------
# Корзины звонков: неделя с понедельника, месяц календарный
# ---------------------------------------------------------------------------


@pytest.fixture()
def москва(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _звонки(db: Database, *моменты: tuple[int, int, int, int, int]) -> None:
    import datetime

    for номер, (г, м, д, ч, мин) in enumerate(моменты):
        db.save_call(f"pbx:{номер}", station="pbx",
                     started_at=datetime.datetime(г, м, д, ч, мин).timestamp())


def test_неделя_начинается_в_понедельник(tmp_path: Path, москва):
    db = Database(tmp_path / "a.db")
    # Четверг, воскресенье, понедельник следующей недели.
    _звонки(db, (2026, 9, 10, 12, 0), (2026, 9, 13, 23, 30), (2026, 9, 14, 0, 30))
    корзины = [(time.strftime("%a %d.%m %H:%M", time.localtime(к["t"])), к["total"])
               for к in db.call_timeline(bucket="week")]
    assert корзины == [("Mon 07.09 00:00", 2), ("Mon 14.09 00:00", 1)], корзины
    ленты = db.call_timeline_by_station(bucket="week", points=3)
    assert ленты == {"pbx": [0, 2, 1]}, ленты


def test_месяц_календарный(tmp_path: Path, москва):
    db = Database(tmp_path / "a.db")
    _звонки(db, (2026, 8, 31, 23, 30), (2026, 9, 1, 0, 30), (2026, 9, 30, 23, 59),
            (2026, 10, 1, 0, 1))
    корзины = [(time.strftime("%d.%m %H:%M", time.localtime(к["t"])), к["total"])
               for к in db.call_timeline(bucket="month")]
    assert корзины == [("01.08 00:00", 1), ("01.09 00:00", 2), ("01.10 00:00", 1)], корзины
    assert db.call_timeline_by_station(bucket="month", points=4) == {"pbx": [0, 1, 2, 1]}


# ---------------------------------------------------------------------------
# Проверки оператора: вдвоём не заводятся и не затираются
# ---------------------------------------------------------------------------


def _вдвоём(db: Database, monkeypatch, действие, потоков: int = 4) -> list[Any]:
    """Потоки проходят чтение вместе и только потом пишут.

    Прежняя проверка «уже стоит?» и «ещё открыта?» шла отдельным чтением до
    записи. Барьер после такого чтения — это два сервера над общей базой,
    прочитавшие одно и то же в одну секунду. Новый код такого чтения не
    делает, и барьер не срабатывает вовсе.
    """
    барьер = threading.Barrier(потоков)
    настоящее = db.query_one

    def с_барьером(sql: str, params=()):
        ответ = настоящее(sql, params)
        if "qa_reviews" in sql:
            барьер.wait(timeout=10)
        return ответ

    monkeypatch.setattr(db, "query_one", с_барьером)
    итоги: list[Any] = [None] * потоков

    def работа(номер: int) -> None:
        итоги[номер] = действие(номер)

    потоки = [threading.Thread(target=работа, args=(н,)) for н in range(потоков)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(30)
    return итоги


def test_проверка_записи_заводится_один_раз(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "a.db")
    db.create_job({"id": "j1", "filename": "a.wav", "status": "completed"})
    итоги = _вдвоём(db, monkeypatch, lambda н: db.qa_assign("j1", assigned_to=f"п{н}"))
    assert sum(1 for и in итоги if и) == 1, итоги
    assert int(db.query("SELECT COUNT(*) FROM qa_reviews WHERE job_id='j1'")[0][0]) == 1


def test_вторая_оценка_не_затирает_первую(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "a.db")
    db.create_job({"id": "j1", "filename": "a.wav", "status": "completed"})
    ид = db.qa_assign("j1")
    итоги = _вдвоём(db, monkeypatch,
                    lambda н: db.qa_submit(ид, reviewer=f"п{н}", score=10.0 * (н + 1)))
    assert итоги.count(True) == 1, итоги
    победитель = итоги.index(True)
    строка = db.query("SELECT reviewer, score FROM qa_reviews WHERE id=?", (ид,))[0]
    assert (строка[0], строка[1]) == (f"п{победитель}", 10.0 * (победитель + 1))


# ---------------------------------------------------------------------------
# Поиск: одна буква — слово, указатель — один раз на запрос
# ---------------------------------------------------------------------------


def test_продолжение_слова_от_двух_букв():
    assert fts_query("д") == '"д"'
    assert fts_query("до") == '"до"*'
    assert fts_query("договор д") == '"договор" "д"'


def _задание_с_репликой(db: Database, ид: str, текст: str) -> None:
    db.create_job({"id": ид, "filename": f"{ид}.wav", "status": "completed", "text": текст})
    db.save_segments(ид, [{"start": 0.0, "end": 2.0, "text": текст}])


def test_список_и_счётчик_спрашивают_указатель_один_раз(client):
    db = client.app.state.hub.db
    for и in range(3):
        _задание_с_репликой(db, f"j{и}", f"обсуждали договор поставки номер {и}")
    обращений = []
    настоящее = db.query

    def считать(sql: str, params=()):
        if "GROUP BY s.job_id" in sql:
            обращений.append(sql)
        return настоящее(sql, params)

    db.query = считать
    try:
        ответ = client.get("/api/jobs", params={"search": "договор"})
    finally:
        db.query = настоящее
    assert ответ.status_code == 200 and ответ.json()["total"] == 3
    assert len(обращений) == 1, f"указатель спрошен {len(обращений)} раз(а)"


def test_память_поиска_гаснет_при_записи_реплик(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    _задание_с_репликой(db, "первое", "согласовали договор")
    assert db.jobs_matching("договор") == ["первое"]
    _задание_с_репликой(db, "второе", "договор подписан")
    assert set(db.jobs_matching("договор")) == {"первое", "второе"}
    db.delete_job("первое")
    assert db.jobs_matching("договор") == ["второе"]


# ---------------------------------------------------------------------------
# Порядок листалок — с номером последним ключом
# ---------------------------------------------------------------------------


def test_порядок_заданий_с_номером():
    assert _порядок_заданий("created_at DESC", False) == "created_at DESC, id DESC"
    assert _порядок_заданий("rtf ASC", True) == "jobs.rtf ASC, jobs.id ASC"
    assert _порядок_заданий("deadline ASC", False).endswith("id ASC")


def test_задания_одной_секунды_листаются_без_повторов(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    номера = ["j5", "j1", "j9", "j3", "j7", "j2", "j8", "j4", "j6"]
    for ид in номера:
        db.create_job({"id": ид, "filename": "a.wav", "status": "completed"})
    db.execute("UPDATE jobs SET created_at=1000")
    страницы = [[j["id"] for j in db.list_jobs(limit=3, offset=с * 3, light=True)]
                for с in range(3)]
    assert sum(страницы, []) == sorted(номера, reverse=True), страницы


def test_справочник_и_проверки_листаются_стабильно(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    for ид in ("e3", "e1", "e2"):
        db.execute("INSERT INTO employees (id, external_id, last_name, first_name, active) "
                   "VALUES (?, ?, 'Иванов', 'Иван', 1)", (ид, ид))
    assert [с["id"] for с in db.employee_list()["items"]] == ["e1", "e2", "e3"]
    for номер, ид in enumerate(("j3", "j1", "j2")):
        db.create_job({"id": ид, "filename": "a.wav", "status": "completed",
                       "owner": "отдел"})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (1000 + номер, ид))
        db.review_add(ид, "manual")
    db.execute("UPDATE review_queue SET picked_at=1000")
    assert [с["job_id"] for с in db.review_list()] == ["j1", "j2", "j3"]
    # С отбором по отделу SQLite идёт от заданий — и без номера в порядке
    # отдавал их по времени создания.
    assert [с["job_id"] for с in db.review_list(status=None, owner=["отдел"])] == \
        ["j1", "j2", "j3"]
    for ид in ("j3", "j1", "j2"):
        db.qa_assign(ид)
    db.execute("UPDATE qa_reviews SET assigned_at=1000")
    порядок = [с["id"] for с in db.qa_list()]
    assert порядок == sorted(порядок, reverse=True)


# ---------------------------------------------------------------------------
# Файл настроек: сохранения по одному, каждое со своим временным файлом
# ---------------------------------------------------------------------------


def _настройки_с_файлом(data_dir: Path):
    from asrhub.config import load

    data_dir.mkdir(parents=True, exist_ok=True)
    файл = data_dir / "asrhub.yaml"
    файл.write_text(f"data_dir: {data_dir}\n", encoding="utf-8")
    return load(файл), файл


def test_брошенный_временный_файл_не_мешает_сохранению(data_dir: Path):
    настройки, файл = _настройки_с_файлом(data_dir)
    # Прежний общий «.tmp» — каталогом: так выглядит и чужой брошенный файл
    # без прав, и половина от упавшего сохранения.
    Path(str(файл) + ".tmp").mkdir()
    настройки.set("result_retention_days", 45)
    настройки.save()
    assert "45" in файл.read_text(encoding="utf-8")


def test_второе_сохранение_не_затирает_первое(data_dir: Path, monkeypatch):
    """Сохранение, снявшее значения раньше, но записавшее позже, их теряло.

    Первое сохранение задерживается на записи файла; второе за это время
    меняет своё значение и сохраняется. Без замка первое, закончив,
    записывало свой прежний снимок поверх — и значение второго пропадало.
    С замком второе ждёт первое и снимает значения уже после него.
    """
    from asrhub import config as модуль

    настройки, файл = _настройки_с_файлом(data_dir)
    первое_пишет = threading.Event()
    второе_готово = threading.Event()
    настоящее = модуль._dump_yaml

    def медленно(данные):
        if threading.current_thread().name == "первое":
            первое_пишет.set()
            второе_готово.wait(timeout=1.0)
        return настоящее(данные)

    monkeypatch.setattr(модуль, "_dump_yaml", медленно)

    def первое() -> None:
        настройки.set("result_retention_days", 41)
        настройки.save()

    def второе() -> None:
        первое_пишет.wait(timeout=5)
        настройки.set("audit_days", 777)
        настройки.save()
        второе_готово.set()

    потоки = [threading.Thread(target=первое, name="первое"),
              threading.Thread(target=второе, name="второе")]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(30)
    текст = файл.read_text(encoding="utf-8")
    assert "777" in текст and "41" in текст, текст


def test_сохранения_разом_не_теряют_ключей(data_dir: Path):
    import yaml

    настройки, файл = _настройки_с_файлом(data_dir)
    барьер = threading.Barrier(6)

    def завести(номер: int) -> None:
        барьер.wait(timeout=10)
        настройки.api_keys[f"k{номер}"] = {"name": f"ключ-{номер}", "role": "user"}
        настройки.save()

    потоки = [threading.Thread(target=завести, args=(н,)) for н in range(6)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(30)
    записано = yaml.safe_load(файл.read_text(encoding="utf-8"))
    имена = {к.get("name") for к in (записано.get("api_keys") or {}).values()}
    assert {f"ключ-{н}" for н in range(6)} <= имена, имена
    assert not [п for п in файл.parent.iterdir() if п.name.endswith(".tmp") and п.is_file()]


# ---------------------------------------------------------------------------
# Обход каталога записей: неполное множество доспрашивается у базы
# ---------------------------------------------------------------------------


def test_обход_каталога_не_топчется_на_известном(tmp_path: Path):
    """Множество известных — только свежие звонки, остальное — у базы.

    На архиве больше предела старые известные записи проходили как новые и
    занимали порцию: полный сбор топтался на уже импортированном и до новых
    записей не доходил.
    """
    from test_review_42_telephony import _папка, wav  # noqa: PLC0415

    db, импортёр, _ = _папка(tmp_path)
    for номер, имя in enumerate(("a", "b", "c", "d")):
        wav(tmp_path / "monitor" / f"{имя}-7916000000{номер}-101.wav", когда=time.time() - 3600)
        ключ = f"{импортёр.id}:file:{имя}-7916000000{номер}-101.wav"
        db.save_call(ключ, station=импортёр.id, skipped="короткий")
        db.execute("UPDATE calls SET imported_at=? WHERE uniqueid=?", (1000 + номер, ключ))
    wav(tmp_path / "monitor" / "e-79160000009-101.wav", когда=time.time() - 3600)
    db.ПРЕДЕЛ_ИЗВЕСТНЫХ_ЗВОНКОВ = 2
    assert len(db.known_call_ids(station=импортёр.id)) == 2
    новые = импортёр._из_папки(2, всё=True)                           # noqa: SLF001
    assert [з.uniqueid for з in новые] == ["file:e-79160000009-101.wav"], \
        [з.uniqueid for з in новые]


# ---------------------------------------------------------------------------
# Самопроверка и сценарии знают о соседях
# ---------------------------------------------------------------------------


@pytest.fixture()
def состояние(data_dir: Path, monkeypatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    return create_app(load(), start_queue=False).state.hub


def test_самопроверка_называет_wal_при_соседе_с_другой_машины(состояние):
    from asrhub import selfcheck
    from test_selfcheck import проверка  # noqa: PLC0415

    assert проверка(selfcheck.состояние(состояние), "db", "journal")["state"] == "ok"
    _отметить_соседа(состояние.db)
    журнал = проверка(selfcheck.состояние(состояние), "db", "journal")
    assert журнал["state"] == "fail", журнал
    assert ЧУЖАЯ_МАШИНА in журнал["hint"] and "db_journal_mode: delete" in журнал["hint"]


def test_самопроверка_видит_разные_режимы_и_несовпадение_с_настройкой(состояние):
    from asrhub import selfcheck
    from asrhub.instance import HOSTNAME
    from test_selfcheck import проверка  # noqa: PLC0415

    состояние.settings.set("db_journal_mode", "delete")
    журнал = проверка(selfcheck.состояние(состояние), "db", "journal")
    assert журнал["state"] == "warn" and "в настройке delete" in журнал["value"], журнал
    состояние.settings.set("db_journal_mode", "wal")
    import os
    _отметить_соседа(состояние.db, f"{HOSTNAME}:{os.getppid()}", журнал="delete")
    журнал = проверка(selfcheck.состояние(состояние), "db", "journal")
    assert журнал["state"] == "fail" and "другой режим" in журнал["hint"], журнал


def test_самопроверка_называет_хранилище(состояние, monkeypatch):
    from asrhub import selfcheck
    from test_selfcheck import проверка  # noqa: PLC0415

    monkeypatch.setattr(fsinfo, "файловая_система", lambda путь, **_: fsinfo.ФС(
        тип="9p", точка="/mnt/c", источник="drvfs", известна=True, папка_вм=True))
    хранилище = проверка(selfcheck.состояние(состояние), "db", "storage")
    assert хранилище["state"] == "warn" and "виртуальной машины" in хранилище["hint"]
    monkeypatch.setattr(fsinfo, "файловая_система", lambda путь, **_: fsinfo.ФС(
        тип="nfs", точка="/mnt/общие", источник="nas:/export", параметры="nolock",
        известна=True, сетевая=True, локальные_блокировки=True))
    assert проверка(selfcheck.состояние(состояние), "db", "storage")["state"] == "ok", \
        "одному серверу nolock не мешает"
    _отметить_соседа(состояние.db)
    свод = selfcheck.состояние(состояние)
    хранилище = проверка(свод, "db", "storage")
    assert хранилище["state"] == "fail" and "nolock" in хранилище["hint"]
    # Раскладка хранилища и имена соседей — не для всех.
    скрыто = проверка(selfcheck.спрятать_пути(свод, состояние.settings), "db", "storage")
    assert not {"mount", "source", "options"} & set(скрыто["metrics"])
    журнал = проверка(selfcheck.спрятать_пути(свод, состояние.settings), "db", "journal")
    assert "other_hosts" not in журнал["metrics"] and "instances" not in журнал["metrics"]


def test_сценарии_видят_простаивающего_соседа(repo_root: Path, tmp_path: Path):
    """Сосед без идущих заданий был невидим для `uninstall --purge` и `update`."""
    from test_install_scripts import BASH, run_bash  # noqa: PLC0415

    if BASH is None:
        pytest.skip("нужен bash")
    данные = tmp_path / "данные"
    данные.mkdir()
    db = Database(данные / "asrhub.db")
    _отметить_соседа(db)
    _отметить_соседа(db, "умерший-сервер:1", давность=3600)
    db.close()
    общее = repo_root / "scripts" / "lib" / "common.sh"
    найдено = run_bash(f'source "{общее}"; other_instances "{данные}"').stdout.strip()
    assert найдено == f"{ЧУЖАЯ_МАШИНА}:4242", найдено


def test_контейнеры_одной_машины_не_соседи_по_сети(data_dir: Path):
    """У контейнеров одной машины имена разные, а ядро и память — общие.

    Два контейнера над общим томом WAL выдерживают; по имени машины они
    выглядели бы разными машинами, и запуск ругался бы на ровном месте.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.instance import KERNEL_ID

    if not KERNEL_ID:
        pytest.skip("отпечаток ядра есть только в Linux")
    db = Database(data_dir / "asrhub.db")
    db.execute("INSERT INTO kv (key, value, ts) VALUES (?,?,?)",
               ("instance:контейнер-2:7", json.dumps({"host": "контейнер-2", "pid": 7,
                                                       "kernel": KERNEL_ID,
                                                       "journal": "wal"}), time.time()))
    db.execute("INSERT INTO kv (key, value, ts) VALUES (?,?,?)",
               ("instance:чужой-хост:7", json.dumps({"host": "чужой-хост", "pid": 7,
                                                      "kernel": "другое-ядро",
                                                      "journal": "wal"}), time.time()))
    db.close()
    база = create_app(load(), start_queue=False).state.hub.db
    предупреждения = [e["message"] for e in база.get_events() if e["kind"] == "storage_warning"]
    assert len(предупреждения) == 1, предупреждения
    assert "чужой-хост" in предупреждения[0] and "контейнер-2" not in предупреждения[0]
