"""Заход 44: копии и восстановление — то, что оставалось после сорокового.

Перенесённая на другой каталог данных база держала пути прежнего, и
удаление записи (в том числе по требованию субъекта) не находило её
файлов. Восстановление из командной строки откладывало журнал прежней базы
под именем, по которому SQLite его не найдёт, и не принимало архивы,
которые снимает расписание. Присланная «prod.db» получала «принято» и
исчезала из списка. Снимок с порчей становился копией. Упавшая копия по
расписанию повторялась через сутки. Восстановление подменяло базу под
соседним сервером.
"""
from __future__ import annotations

import inspect
import io
import json
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from asrhub import backup, maintenance
from asrhub.db import Database
from asrhub.errors import ConfigError, StorageError
from test_backup import Настройки  # noqa: PLC0415
from test_review_44_storage import _отметить_соседа  # noqa: PLC0415


def _сервер(каталог: Path, заданий: int = 3) -> tuple[Database, Настройки]:
    """Сервер с заданиями, файлы которых лежат в его каталоге данных."""
    (каталог / "uploads").mkdir(parents=True, exist_ok=True)
    (каталог / "results").mkdir(parents=True, exist_ok=True)
    db = Database(каталог / "asrhub.db")
    for номер in range(заданий):
        ид = f"j{номер}"
        файл = каталог / "uploads" / f"up_{номер}.wav"
        файл.write_bytes(b"RIFF")
        (каталог / "results" / ид).mkdir(exist_ok=True)
        db.create_job({"id": ид, "filename": f"{номер}.wav", "status": "completed",
                       "file_path": str(файл), "result_path": str(каталог / "results" / ид)})
    # Запись из архива станции — вне каталога данных, её путь не трогаем.
    db.create_job({"id": "pbx", "filename": "out.wav", "status": "completed",
                   "file_path": "/var/spool/asterisk/monitor/out-1.wav"})
    return db, Настройки(каталог)


def _пути(база: Path) -> dict[str, tuple[str, str]]:
    with sqlite3.connect(база) as соединение:
        return {r[0]: (r[1] or "", r[2] or "") for r in соединение.execute(
            "SELECT id, file_path, result_path FROM jobs")}


# ---------------------------------------------------------------------------
# Перенос на другой каталог данных
# ---------------------------------------------------------------------------


def test_перенос_копии_переписывает_пути_заданий(tmp_path: Path):
    db, настройки_а = _сервер(tmp_path / "а")
    копия = backup.создать(db, настройки_а, kind="full")
    db_б, настройки_б = _сервер(tmp_path / "б", заданий=0)
    настройки_б["backup_dir"] = настройки_а["backup_dir"]
    итог = backup.восстановить(db_б, настройки_б, копия["name"], what="full")
    assert итог["paths_rewritten"] == 3 and итог["from_data_dir"] == str(tmp_path / "а")
    db_б.close()
    backup.применить_отложенное(tmp_path / "б" / "asrhub.db")
    пути = _пути(tmp_path / "б" / "asrhub.db")
    assert пути["j1"] == (str(tmp_path / "б" / "uploads" / "up_1.wav"),
                          str(tmp_path / "б" / "results" / "j1"))
    assert пути["pbx"][0] == "/var/spool/asterisk/monitor/out-1.wav"


def test_опись_главнее_догадки(tmp_path: Path):
    """Каталог из описи переписывает и пути, по которым его не угадать."""
    каталог = tmp_path / "а"
    (каталог / "uploads" / "папка").mkdir(parents=True)
    db = Database(каталог / "asrhub.db")
    файл = каталог / "uploads" / "папка" / "x.wav"
    файл.write_bytes(b"RIFF")
    db.create_job({"id": "j", "filename": "x.wav", "status": "completed",
                   "file_path": str(файл)})
    настройки_а = Настройки(каталог)
    копия = backup.создать(db, настройки_а, kind="full")
    db_б, настройки_б = _сервер(tmp_path / "б", заданий=0)
    настройки_б["backup_dir"] = настройки_а["backup_dir"]
    backup.восстановить(db_б, настройки_б, копия["name"], what="full")
    db_б.close()
    backup.применить_отложенное(tmp_path / "б" / "asrhub.db")
    assert _пути(tmp_path / "б" / "asrhub.db")["j"][0] == \
        str(tmp_path / "б" / "uploads" / "папка" / "x.wav")


def test_копия_прежнего_образца_угадывает_прежний_каталог(tmp_path: Path):
    """У файла базы нет описи — каталог узнаётся по путям самих заданий."""
    db, настройки_а = _сервер(tmp_path / "а")
    файл = maintenance.make_backup(db, настройки_а)
    assert файл is not None
    _, настройки_б = _сервер(tmp_path / "б", заданий=0)
    итог = backup._вернуть_базу(файл, настройки_б)                    # noqa: SLF001
    assert итог["from_data_dir"] == str(tmp_path / "а")
    backup.применить_отложенное(tmp_path / "б" / "asrhub.db")
    assert _пути(tmp_path / "б" / "asrhub.db")["j0"][0].startswith(str(tmp_path / "б"))


def test_похожее_начало_пути_не_переписывается(tmp_path: Path):
    """«/data/asrhub2/…» начинается с «/data/asrhub», но лежит не в нём."""
    база = tmp_path / "a.db"
    db = Database(база)
    db.create_job({"id": "свой", "filename": "a", "file_path": "/data/asrhub/uploads/a.wav"})
    db.create_job({"id": "сосед", "filename": "b", "file_path": "/data/asrhub2/uploads/b.wav"})
    db.create_job({"id": "windows", "filename": "c",
                   "file_path": "C:\\ASRHub\\data\\uploads\\c.wav"})
    db.close()
    assert backup._переписать_пути(база, "/data/asrhub", "/srv/new") == 1   # noqa: SLF001
    assert backup._переписать_пути(база, "C:\\ASRHub\\data", "/srv/new") == 1  # noqa: SLF001
    пути = _пути(база)
    assert пути["свой"][0] == "/srv/new/uploads/a.wav"
    assert пути["сосед"][0] == "/data/asrhub2/uploads/b.wav"
    assert пути["windows"][0] == "/srv/new/uploads/c.wav"


# ---------------------------------------------------------------------------
# Восстановление из командной строки
# ---------------------------------------------------------------------------


def _рабочая_с_хвостом_в_журнале(база: Path) -> sqlite3.Connection:
    """Рабочая база, у которой последние транзакции ещё в журнале WAL.

    Соединение остаётся открытым: пока оно живо, журнал не влит — ровно как
    после сбоя или на остановленном «kill -9» сервере.
    """
    Database(база).close()
    соединение = sqlite3.connect(база, isolation_level=None)
    соединение.execute("PRAGMA wal_autocheckpoint=0")
    for номер in range(5):
        соединение.execute("INSERT INTO kv (key, value, ts) VALUES (?, '1', 0)", (f"до-{номер}",))
    соединение.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for номер in range(3):
        соединение.execute("INSERT INTO kv (key, value, ts) VALUES (?, '1', 0)",
                           (f"после-{номер}",))
    return соединение


def test_командная_строка_сохраняет_прежнюю_базу_целиком(tmp_path: Path):
    """Журнал откладывался как «asrhub.db-wal.before-restore-…» — SQLite его
    не находил, и из восьми записей в возвращённой базе оставалось пять."""
    db, настройки = _сервер(tmp_path / "копия")
    файл = maintenance.make_backup(db, настройки)
    рабочая = tmp_path / "data" / "asrhub.db"
    рабочая.parent.mkdir()
    соединение = _рабочая_с_хвостом_в_журнале(рабочая)
    try:
        прежняя = maintenance.restore(файл, рабочая)
    finally:
        соединение.close()
    assert прежняя.startswith("asrhub.db.before-restore-")
    with sqlite3.connect(рабочая.with_name(прежняя)) as старая:
        ключи = {r[0] for r in старая.execute("SELECT key FROM kv WHERE key LIKE 'до-%' "
                                             "OR key LIKE 'после-%'")}
    assert len(ключи) == 8, f"в прежней базе {len(ключи)} из 8 записей"
    assert Database(рабочая).count_jobs() == 4


def test_командная_строка_принимает_архив_расписания(tmp_path: Path):
    db, настройки = _сервер(tmp_path / "а")
    копия = backup.создать(db, настройки, kind="full")
    архив = Path(настройки["backup_dir"]) / копия["name"]
    рабочая = tmp_path / "б" / "asrhub.db"
    рабочая.parent.mkdir()
    Database(рабочая).close()
    maintenance.restore(архив, рабочая)
    восстановленная = Database(рабочая)
    assert восстановленная.count_jobs() == 4
    assert восстановленная.get_job("j2")["file_path"].startswith(str(tmp_path / "б"))


def test_архив_только_настроек_объясняет_отказ(tmp_path: Path):
    db, настройки = _сервер(tmp_path / "а")
    копия = backup.создать(db, настройки, kind="settings")
    рабочая = tmp_path / "б" / "asrhub.db"
    рабочая.parent.mkdir()
    Database(рабочая).close()
    with pytest.raises(ValueError, match="только настроек"):
        maintenance.restore(Path(настройки["backup_dir"]) / копия["name"], рабочая)
    assert not list(рабочая.parent.glob("*.before-restore-*")), "рабочую базу тронули"


def test_командная_строка_не_восстанавливает_под_соседом(tmp_path: Path):
    db, настройки = _сервер(tmp_path / "а")
    файл = maintenance.make_backup(db, настройки)
    рабочая = tmp_path / "б" / "asrhub.db"
    рабочая.parent.mkdir()
    сосед = Database(рабочая)
    _отметить_соседа(сосед)
    сосед.close()
    with pytest.raises(ValueError, match="работают серверы"):
        maintenance.restore(файл, рабочая)


# ---------------------------------------------------------------------------
# Восстановление из интерфейса при соседях
# ---------------------------------------------------------------------------


def test_восстановление_данных_отказывает_при_живом_соседе(tmp_path: Path):
    db, настройки = _сервер(tmp_path)
    копия = backup.создать(db, настройки, kind="full")
    _отметить_соседа(db)
    with pytest.raises(ConfigError, match="другие серверы"):
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert not list(tmp_path.glob("*.restore-pending"))
    # Настройки — можно: они применяются на ходу и базу не подменяют.
    assert backup.восстановить(db, настройки, копия["name"], what="settings")["applied"]


def test_отложенная_подмена_ждёт_ухода_соседа(tmp_path: Path, caplog):
    db, настройки = _сервер(tmp_path)
    копия = backup.создать(db, настройки, kind="full")
    backup.восстановить(db, настройки, копия["name"], what="full")
    ожидает = tmp_path / "asrhub.db.restore-pending"
    assert ожидает.is_file()
    _отметить_соседа(db)
    db.close()
    with caplog.at_level("ERROR", logger="asrhub.backup"):
        assert backup.применить_отложенное(tmp_path / "asrhub.db") == ""
    assert ожидает.is_file() and "отложено" in caplog.text
    db = Database(tmp_path / "asrhub.db")
    _отметить_соседа(db, давность=3600)
    db.close()
    assert backup.применить_отложенное(tmp_path / "asrhub.db")
    assert not ожидает.exists()


# ---------------------------------------------------------------------------
# Присланная копия
# ---------------------------------------------------------------------------


def test_присланная_база_видна_в_списке_под_своим_именем(tmp_path: Path):
    _, настройки = _сервер(tmp_path / "а")
    другая = tmp_path / "prod-2026-09-01.db"
    Database(другая).close()
    принято = backup.принять(настройки, другая.name, io.BytesIO(другая.read_bytes()))
    assert принято["name"] == "asrhub-prod-2026-09-01.db"
    assert принято["name"] in {к["name"] for к in backup.список(настройки)}
    assert backup.удалить(настройки, принято["name"])["removed"] == принято["name"]


def test_не_база_не_принимается_и_не_оставляет_черновика(tmp_path: Path):
    _, настройки = _сервер(tmp_path / "а")
    with pytest.raises(ConfigError, match="не похож на базу"):
        backup.принять(настройки, "prod.db", io.BytesIO("это не база".encode() * 100))
    чужая = tmp_path / "чужая.db"
    with sqlite3.connect(чужая) as соединение:
        соединение.execute("CREATE TABLE t (x)")
    with pytest.raises(ConfigError, match="нет таблицы заданий"):
        backup.принять(настройки, "чужая.db", io.BytesIO(чужая.read_bytes()))
    assert [п.name for п in Path(настройки["backup_dir"]).iterdir()] == []


def test_загрузка_копии_не_держит_цикл_событий():
    """Запись гигабайтной копии шла в цикле событий и останавливала сервер."""
    from asrhub.api import routes_backup

    assert not inspect.iscoroutinefunction(routes_backup.загрузить)


# ---------------------------------------------------------------------------
# Снятие копии
# ---------------------------------------------------------------------------


def test_снимок_с_порчей_не_становится_копией(tmp_path: Path, monkeypatch):
    db, настройки = _сервер(tmp_path)

    def испортить(db_, куда: Path) -> None:
        backup_снять(db_, куда)
        # Страницы со второй по шестую — корни таблиц и указателей.
        with open(куда, "r+b") as файл:
            файл.seek(4096)
            файл.write(b"\xff" * 4096 * 5)

    backup_снять = backup._снять_базу                                  # noqa: SLF001
    monkeypatch.setattr(backup, "_снять_базу", испортить)
    with pytest.raises(StorageError, match="проверку целостности|проверка не прошла"):
        backup.создать(db, настройки, kind="full")
    assert not list(Path(настройки["backup_dir"]).glob("*.asrhub.tar.gz"))


def test_копия_распаковывается_во_временный_каталог_сервера(tmp_path: Path, monkeypatch):
    db, настройки = _сервер(tmp_path)
    копия = backup.создать(db, настройки, kind="settings")
    настройки["temp_dir"] = str(tmp_path / "свой-tmp")
    куда: list[str] = []
    настоящее = backup.tempfile.mkdtemp

    def запомнить(*a, **k):
        куда.append(str(k.get("dir")))
        return настоящее(*a, **k)

    monkeypatch.setattr(backup.tempfile, "mkdtemp", запомнить)
    backup.восстановить(db, настройки, копия["name"], what="settings")
    assert куда == [str(tmp_path / "свой-tmp")], куда


# ---------------------------------------------------------------------------
# Копия по расписанию: отметка после успеха, повтор через час, один сервер
# ---------------------------------------------------------------------------


class _Расписание(Настройки):
    def __init__(self, каталог: Path):
        super().__init__(каталог, review_enabled=False, qa_enabled=False)


def test_упавшая_копия_повторяется_через_час(tmp_path: Path, monkeypatch):
    db, _ = _сервер(tmp_path)
    настройки = _Расписание(tmp_path)
    monkeypatch.setattr(backup, "пора", lambda *a, **k: True)
    попытки: list[str] = []

    def упасть(*a, **k):
        попытки.append("x")
        raise StorageError("диск полон")

    monkeypatch.setattr(backup, "создать", упасть)
    сделано = maintenance.run_scheduled(db, настройки, analytics=None)
    assert сделано["backup_error"] and db.get_kv(maintenance.KV_BACKUP) is None, \
        "отметка «снята» поставлена упавшей копии — повтор через сутки"
    maintenance.run_scheduled(db, настройки, analytics=None)
    assert len(попытки) == 1, "повтор каждые двадцать секунд — полным чтением базы"
    db.set_kv(maintenance.KV_BACKUP_FAILED, time.time() - 2 * 3600)
    monkeypatch.setattr(backup, "создать", lambda *a, **k: {"name": "готово"})
    assert maintenance.run_scheduled(db, настройки, analytics=None)["backup"] == "готово"
    assert db.get_kv(maintenance.KV_BACKUP)


def test_копию_снимает_один_сервер(tmp_path: Path, monkeypatch):
    db, _ = _сервер(tmp_path)
    настройки = _Расписание(tmp_path)
    monkeypatch.setattr(backup, "пора", lambda *a, **k: True)
    monkeypatch.setattr(backup, "создать", lambda *a, **k: pytest.fail("копия вдвоём"))
    assert db.lease_take(maintenance.АРЕНДА_КОПИИ, "сосед:1", 3600) is None
    сделано = maintenance.run_scheduled(db, настройки, analytics=None)
    assert "backup" not in сделано


def test_журнал_restore_pending_содержит_след(tmp_path: Path):
    """След восстановления из копии прежнего образца — в ту базу, что станет рабочей."""
    db, настройки = _сервер(tmp_path / "а")
    файл = maintenance.make_backup(db, настройки)
    backup.восстановить(db, настройки, файл.name, what="full")
    db.close()
    backup.применить_отложенное(tmp_path / "а" / "asrhub.db")
    события = [e["message"] for e in Database(tmp_path / "а" / "asrhub.db").get_events()
               if e["kind"] == "backup_restored"]
    assert any(файл.name in с for с in события), события


def test_опись_описывает_каталог_данных(tmp_path: Path):
    db, настройки = _сервер(tmp_path)
    копия = backup.создать(db, настройки, kind="full")
    with tarfile.open(Path(настройки["backup_dir"]) / копия["name"]) as архив:
        опись = json.load(архив.extractfile("manifest.json"))
    assert опись["data_dir"] == str(tmp_path)
