"""Заход 54: хранение — резервные копии, удаление по требованию, миграции.

По находкам ревизии 3.1.20 (отчёт «04-storage»): файлы в архиве копии по
два-три раза, неудавшееся восстановление «full» с применёнными чужими
настройками, предел копий без различия вида, отложенное восстановление, о
котором никто не знает, удаление по требованию сверх пятисот совпадений,
физическое стирание по умолчанию сборки SQLite, скачивание копии мимо
журнала доступа, звонки, забытые раньше своих заданий, уникальный
указатель, роняющий миграцию.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Any

import pytest
from asrhub import backup
from asrhub.db import SCHEMA_VERSION, Database
from asrhub.errors import ConfigError, StorageError


class Настройки(dict):
    """Настройки сервера в объёме, который нужен копии и уборке."""

    def __init__(self, каталог: Path, **поверх: Any):
        super().__init__({
            "backup_keep_days": 10, "backup_keep": 0, "backup_enabled": True,
            "backup_time": "00:01", "backup_interval_hours": 0,
            "backup_kind": "full", "backup_include_results": False,
            "backup_dir": str(каталог / "backups"),
            "beam_size": 5, "max_concurrent_jobs": 2,
            "review_enabled": False, "qa_enabled": False, "digest_url": "",
            "control_model": "", "employees_sync_enabled": False,
            **поверх})

        class Пути:                                          # noqa: N801
            data = str(каталог)
            results = str(каталог / "results")
            db = каталог / "asrhub.db"
        self.paths = Пути()
        self.config_file = None
        self.sources: dict[str, str] = {}

    def get(self, ключ, по_умолчанию=None):                  # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def set(self, ключ, значение, source="runtime"):         # noqa: A003
        self[ключ] = значение

    def save(self, path=None):
        return path


@pytest.fixture()
def стенд(tmp_path: Path):
    (tmp_path / "results").mkdir()
    db = Database(tmp_path / "asrhub.db")
    for н in range(3):
        db.create_job({"id": f"job_{н}", "filename": f"f{н}.wav", "status": "completed",
                       "text": "привет"})
    yield db, Настройки(tmp_path), tmp_path
    db.close()


def _архив(путь: Path) -> list[tarfile.TarInfo]:
    with tarfile.open(путь, "r:gz") as архив:
        return архив.getmembers()


# ---------------------------------------------------------------------------
# 04-storage №1, №7: состав архива, опись первой, повторы при распаковке
# ---------------------------------------------------------------------------


def test_каждый_файл_ложится_в_архив_один_раз_и_опись_первой(стенд):
    """Цикл по rglob звал рекурсивный `add`: база дважды, результаты трижды."""
    db, настройки, каталог = стенд
    for н in range(3):
        (каталог / "results" / f"job_{н}").mkdir()
        (каталог / "results" / f"job_{н}" / "result.json").write_bytes(os.urandom(50_000))
    копия = backup.создать(db, настройки, kind="full", include_results=True)
    записи = _архив(Path(настройки["backup_dir"]) / копия["name"])
    имена = [з.name for з in записи]
    assert len(имена) == len(set(имена)), f"повторы в архиве: {имена}"
    assert имена[0] == backup.ОПИСЬ, имена
    assert not [и for и in имена if и.endswith(("-wal", "-shm", "-journal"))], имена
    assert "db/asrhub.db" in имена and "results/job_2/result.json" in имена


def test_старая_копия_с_повторами_не_упирается_в_предел(стенд, monkeypatch):
    """Сумма с повторами отвергала собственную копию как «слишком велика»."""
    db, настройки, каталог = стенд
    снимок = каталог / "снимок"
    (снимок / "db").mkdir(parents=True)
    backup._снять_базу(db, снимок / "db" / "asrhub.db")
    (снимок / "settings.json").write_text("{}", encoding="utf-8")
    (снимок / backup.ОПИСЬ).write_text(json.dumps({"format": 1, "kind": "full"}),
                                        encoding="utf-8")
    место = Path(настройки["backup_dir"])
    место.mkdir()
    файл = место / f"asrhub-20260101-000000-full-old{backup.РАСШИРЕНИЕ}"
    with tarfile.open(файл, "w:gz") as архив:          # как собирала прежняя версия
        for путь in sorted(снимок.rglob("*")):
            архив.add(путь, arcname=str(путь.relative_to(снимок)))
    размер_базы = (снимок / "db" / "asrhub.db").stat().st_size
    monkeypatch.setattr(backup, "ПРЕДЕЛ_РАСПАКОВКИ", int(размер_базы * 1.5))
    итог = backup.восстановить(db, настройки, файл.name, what="full", apply_settings=False)
    assert итог["database"] is True


def test_опись_читается_без_прогона_всего_архива(tmp_path: Path):
    """`getmember` прогонял через gzip весь архив ради описи в сотни байт."""
    опись = tmp_path / backup.ОПИСЬ
    опись.write_text(json.dumps({"format": 1, "kind": "full", "jobs": 7}), encoding="utf-8")
    большой = tmp_path / "большой.bin"
    большой.write_bytes(os.urandom(2_000_000))
    файл = tmp_path / f"asrhub-x{backup.РАСШИРЕНИЕ}"
    with tarfile.open(файл, "w:gz") as архив:
        архив.add(опись, arcname=backup.ОПИСЬ)
        архив.add(большой, arcname="db/asrhub.db")
    данные = файл.read_bytes()
    файл.write_bytes(данные[:len(данные) // 2])        # обрыв посреди базы
    assert backup._прочитать_опись(файл)["jobs"] == 7


def test_настройки_из_полной_копии_не_распаковывают_базу(стенд, monkeypatch):
    """«Вернуть только настройки» распаковывало всю базу во временный каталог."""
    db, настройки, _ = стенд
    копия = backup.создать(db, настройки, kind="full")
    распаковано: list[str] = []
    настоящее = tarfile.TarFile.extractall

    def записать(self, path=".", members=None, **к):
        распаковано.extend(m.name for m in (members if members is not None else self))
        return настоящее(self, path, members=members, **к)

    monkeypatch.setattr(tarfile.TarFile, "extractall", записать)
    backup.восстановить(db, настройки, копия["name"], what="settings")
    assert распаковано and set(распаковано) <= {backup.ОПИСЬ, "settings.json"}, распаковано


# ---------------------------------------------------------------------------
# 04-storage №3: неудавшееся восстановление «full»
# ---------------------------------------------------------------------------


def _копия_с_битой_базой(db: Database, настройки: Настройки, каталог: Path) -> str:
    """Полная копия с чужими настройками и базой, испорченной при переносе."""
    чужие = Настройки(каталог, max_concurrent_jobs=7)
    копия = backup.создать(db, чужие, kind="full")
    путь = Path(настройки["backup_dir"]) / копия["name"]
    распаковка = каталог / "распаковка"
    with tarfile.open(путь, "r:gz") as архив:
        архив.extractall(распаковка, filter="data")
    база = распаковка / "db" / "asrhub.db"
    данные = bytearray(база.read_bytes())
    данные[100:4096] = b"\x00" * (4096 - 100)            # заголовок цел, схема — нет
    база.write_bytes(bytes(данные))
    путь.unlink()
    with tarfile.open(путь, "w:gz") as архив:
        for имя in (backup.ОПИСЬ, "settings.json", "db", "db/asrhub.db"):
            архив.add(распаковка / имя, arcname=имя, recursive=False)
    return копия["name"]


def test_битая_база_в_копии_не_оставляет_применёнными_чужие_настройки(стенд):
    db, настройки, каталог = стенд
    имя = _копия_с_битой_базой(db, настройки, каталог)
    with pytest.raises(ConfigError):
        backup.восстановить(db, настройки, имя, what="full")
    assert настройки["max_concurrent_jobs"] == 2, "чужие настройки применены"
    assert not (каталог / ("asrhub.db" + backup.ОЖИДАЕТ_ЗАПУСКА)).exists()


def test_отказ_после_подготовки_базы_её_подготовку_снимает(стенд, monkeypatch):
    """Сбой на применении настроек не должен оставлять базу ждать запуска."""
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")

    def упасть(*_a, **_k):
        raise StorageError("диск полон")

    # Проверка-страж нового порядка: настройки применяются последними.
    monkeypatch.setattr(backup, "_применить_настройки", упасть, raising=False)
    with pytest.raises(StorageError):
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert not (каталог / ("asrhub.db" + backup.ОЖИДАЕТ_ЗАПУСКА)).exists()


def test_база_новее_сервера_не_ставится(стенд, tmp_path):
    """Опись хранила версию схемы, а сверялся только формат архива."""
    _, настройки, _ = стенд
    файл = tmp_path / "новая.db"
    соединение = sqlite3.connect(файл)
    соединение.execute("CREATE TABLE jobs (id TEXT)")
    соединение.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    соединение.commit()
    соединение.close()
    with pytest.raises(ConfigError, match="новее"):
        backup._вернуть_базу(файл, настройки)


# ---------------------------------------------------------------------------
# 04-storage №10: отложенное восстановление
# ---------------------------------------------------------------------------


def test_ожидающее_восстановление_видно_и_отменяется(client):
    копия = client.post("/api/backup?kind=full").json()
    ответ = client.post("/api/backup/restore", json={"name": копия["name"], "what": "full",
                                                      "apply_settings": False})
    assert ответ.status_code == 200, ответ.text
    свод = client.get("/api/backup").json()
    assert свод["pending_restore"] and свод["pending_restore"]["name"].endswith(
        backup.ОЖИДАЕТ_ЗАПУСКА), свод.get("pending_restore")
    assert "pending_restore" in _проверки_самопроверки(client)
    assert client.delete("/api/backup/pending").json() == {"cancelled": True}
    assert client.get("/api/backup").json()["pending_restore"] is None
    assert "pending_restore" not in _проверки_самопроверки(client)


def _проверки_самопроверки(client) -> set[str]:
    """Номера всех проверок самопроверки, на любой глубине ответа."""
    найдено: set[str] = set()

    def обойти(узел: Any) -> None:
        if isinstance(узел, dict):
            if isinstance(узел.get("id"), str) and "state" in узел:
                найдено.add(узел["id"])
            for значение in узел.values():
                обойти(значение)
        elif isinstance(узел, list):
            for значение in узел:
                обойти(значение)

    from asrhub import selfcheck  # noqa: PLC0415

    selfcheck._КЕШ_КОПИЙ.clear()        # свод копий помнится 5 с — не про то
    ответ = client.get("/api/system/selfcheck")
    assert ответ.status_code == 200, ответ.text
    обойти(ответ.json())
    return найдено


def _база_с_заданием(путь: Path, номер: str) -> None:
    db = Database(путь)
    db.create_job({"id": номер, "filename": "a.wav", "status": "completed"})
    db.close()


def test_давно_подготовленная_база_не_ставится_без_подтверждения(tmp_path: Path):
    """Через две недели плановый перезапуск молча ставил двухнедельную копию."""
    рабочая = tmp_path / "asrhub.db"
    _база_с_заданием(рабочая, "рабочее")
    ожидает = tmp_path / ("asrhub.db" + backup.ОЖИДАЕТ_ЗАПУСКА)
    _база_с_заданием(ожидает, "из_копии")
    давно = time.time() - 10 * 86400
    os.utime(ожидает, (давно, давно))

    assert backup.применить_отложенное(рабочая) == ""
    db = Database(рабочая)
    assert db.get_job("рабочее") is not None, "подменили без подтверждения"
    db.close()
    assert "подтвердите" in backup.причина_отсрочки(рабочая).get("reason", "")

    настройки = Настройки(tmp_path)
    assert backup.ожидающее_восстановление(настройки)["expired"] is True
    backup.подтвердить_ожидающее(настройки)
    assert backup.применить_отложенное(рабочая)
    db = Database(рабочая)
    assert db.get_job("из_копии") is not None
    db.close()
    assert not backup.причина_отсрочки(рабочая)


def test_отсрочка_из_за_соседей_оставляет_причину(tmp_path: Path):
    """Отказ из-за соседей был только строкой журнала — и администратор
    считал, что восстановил."""
    рабочая = tmp_path / "asrhub.db"
    db = Database(рабочая)
    db.set_kv("instance:соседняя-машина:123", {"host": "соседняя-машина"})
    db.close()
    _база_с_заданием(tmp_path / ("asrhub.db" + backup.ОЖИДАЕТ_ЗАПУСКА), "из_копии")
    assert backup.применить_отложенное(рабочая) == ""
    причина = backup.причина_отсрочки(рабочая)
    assert причина.get("kind") == "neighbours" and "соседняя-машина" in причина["reason"]
    свод = backup.ожидающее_восстановление(Настройки(tmp_path))
    assert свод and "соседняя-машина" in свод["deferred"]


# ---------------------------------------------------------------------------
# 04-storage №11: предел копий по видам
# ---------------------------------------------------------------------------


def test_копии_настроек_не_вытесняют_копии_данных(стенд):
    db, настройки, _ = стенд
    настройки["backup_keep"] = 3
    for _ in range(3):
        backup.создать(db, настройки, kind="full")
    for _ in range(3):
        backup.создать(db, настройки, kind="settings")
    виды = sorted(к["kind"] for к in backup.список(настройки))
    assert виды == ["full"] * 3 + ["settings"] * 3, виды


# ---------------------------------------------------------------------------
# 04-storage №14, №15: опись не того вида, путь в URI
# ---------------------------------------------------------------------------


def _архив_с_описью(путь: Path, опись: dict[str, Any]) -> bytes:
    буфер = io.BytesIO()
    with tarfile.open(fileobj=буфер, mode="w:gz") as архив:
        for имя, данные in ((backup.ОПИСЬ, json.dumps(опись).encode()),
                            ("settings.json", b"{}")):
            запись = tarfile.TarInfo(имя)
            запись.size = len(данные)
            архив.addfile(запись, io.BytesIO(данные))
    путь.write_bytes(буфер.getvalue())
    return буфер.getvalue()


def test_опись_не_того_вида_не_роняет_раздел_и_не_принимается(стенд, tmp_path):
    _, настройки, _ = стенд
    место = Path(настройки["backup_dir"])
    место.mkdir()
    плохая = {"format": 1, "kind": "settings", "schema_version": "v31"}
    _архив_с_описью(место / f"asrhub-20260101-000000-settings-x{backup.РАСШИРЕНИЕ}", плохая)
    копии = backup.список(настройки)
    assert len(копии) == 1 and "опись не читается" in копии[0]["error"]
    данные = _архив_с_описью(tmp_path / "чужая.tar.gz", плохая)
    with pytest.raises(ConfigError):
        backup.принять(настройки, f"со-стороны{backup.РАСШИРЕНИЕ}", io.BytesIO(данные))
    assert [к["name"] for к in backup.список(настройки)] == [копии[0]["name"]]


@pytest.mark.parametrize("имя", ["копии 100%25", "копии #2", "копии ?x"])
def test_знаки_uri_в_пути_каталога_копий(стенд, tmp_path, имя):
    """«#» и «?» обрывали путь, «%25» раскодировался — проверка снимка шла не по нему."""
    db, настройки, _ = стенд
    место = tmp_path / имя
    настройки["backup_dir"] = str(место)
    копия = backup.создать(db, настройки, kind="full")
    assert (место / копия["name"]).is_file()
    пустые = [п.name for п in tmp_path.iterdir() if п.is_file() and п.stat().st_size == 0]
    assert not пустые, f"рядом появились пустые базы: {пустые}"


# ---------------------------------------------------------------------------
# 04-storage №9: снимок в режиме журнала отката
# ---------------------------------------------------------------------------


def test_снимок_в_режиме_отката_идёт_шагами_под_замком_записи(tmp_path, monkeypatch):
    """Одношаговый снимок держал разделяемую блокировку всю копию: писатели
    через 30 с получали «database is locked». Под замком свои ждут, а не
    падают; шаги оставляют окно писателям других серверов."""
    db = Database(tmp_path / "asrhub.db", journal_mode="delete")
    assert db.journal_mode == "delete"
    for н in range(50):
        db.create_job({"id": f"j{н}", "filename": "a.wav", "text": "слово " * 2000})
    настоящее = Database.conn
    увидено: list[tuple[bool, Any]] = []

    class Обёртка:
        def __init__(self, соединение):
            self._с = соединение

        def __getattr__(self, имя):
            return getattr(self._с, имя)

        def backup(self, target, **к):
            увидено.append((db._write_lock._is_owned(), к.get("pages")))
            return self._с.backup(target, **к)

    monkeypatch.setattr(Database, "conn", property(lambda self: Обёртка(настоящее.fget(self))))
    backup._снять_базу(db, tmp_path / "снимок.db")
    monkeypatch.undo()
    assert увидено and увидено[0][0], "снимок шёл без замка записи"
    assert увидено[0][1] and увидено[0][1] > 0, "снимок одним шагом"
    копия = Database(tmp_path / "снимок.db")
    assert копия.count_jobs() == 50
    копия.close()
    db.close()


def test_копия_service_sh_при_пределе_один_не_удаляет_прежнюю_до_снятия(tmp_path, monkeypatch):
    """При `backup_keep: 1` единственная копия удалялась до попытки снять новую."""
    from asrhub import maintenance

    db = Database(tmp_path / "asrhub.db")
    настройки = Настройки(tmp_path, backup_keep=1)
    место = Path(настройки["backup_dir"])
    место.mkdir()
    прежняя = место / "asrhub-20260101-000000-x.db"
    sqlite3.connect(прежняя).close()
    настоящее = sqlite3.connect

    def отказ(путь, *a, **к):
        if "backups" in str(путь) and str(путь).endswith(".db"):
            raise sqlite3.OperationalError("disk I/O error")
        return настоящее(путь, *a, **к)

    monkeypatch.setattr(sqlite3, "connect", отказ)
    assert maintenance.make_backup(db, настройки) is None
    monkeypatch.undo()
    assert прежняя.exists(), "единственную копию удалили, а новая не снялась"
    db.close()


def test_копия_по_расписанию_перечитывает_отметку_под_арендой(tmp_path, monkeypatch):
    """«Пора» проверялось до аренды: два сервера снимали две копии подряд."""
    from asrhub import maintenance

    db = Database(tmp_path / "asrhub.db")
    настройки = Настройки(tmp_path, backup_interval_hours=24)
    настоящая = db.lease_take

    def сосед_только_что_снял(*a, **к):
        db.set_kv(maintenance.KV_BACKUP, time.time())
        return настоящая(*a, **к)

    monkeypatch.setattr(db, "lease_take", сосед_только_что_снял)
    monkeypatch.setattr(backup, "создать", lambda *a, **к: pytest.fail("вторая копия"))
    сделано = maintenance.run_scheduled(db, настройки, analytics=None)
    assert "backup" not in сделано
    db.close()


def test_пропущенный_вчерашний_заход_догоняется(стенд, monkeypatch):
    """Сервер стоял с назначенного часа до полуночи — копии за сутки не было."""
    db, настройки, _ = стенд
    настройки["backup_time"] = "23:00"
    местное = time.localtime()
    полночь_с_минутой = time.mktime((местное.tm_year, местное.tm_mon, местное.tm_mday,
                                     0, 30, 0, 0, 0, -1))
    monkeypatch.setattr(backup.time, "time", lambda: полночь_с_минутой)
    db.set_kv("backup_last_at", полночь_с_минутой - 2 * 86400)
    assert backup.пора(db, настройки) is True
    db.set_kv("backup_last_at", полночь_с_минутой - 3600)         # вчера в 23:30
    assert backup.пора(db, настройки) is False


def test_прежние_базы_видны_и_старые_убираются(стенд):
    db, настройки, каталог = стенд
    старая = каталог / "asrhub.db.before-restore-20260101-000000"
    свежая = каталог / "asrhub.db.before-restore-20260901-000000"
    самая_старая = каталог / "asrhub.db.before-restore-20251201-000000"
    for путь, дней in ((самая_старая, 40), (старая, 30), (свежая, 20)):
        путь.write_bytes(b"x" * 10)
        момент = time.time() - дней * 86400
        os.utime(путь, (момент, момент))
    свод = backup.свод(настройки)
    assert [з["name"] for з in свод["previous_bases"]] == [свежая.name, старая.name,
                                                          самая_старая.name]
    backup.подчистить(настройки)
    assert свежая.exists(), "самую свежую прежнюю базу удалять нельзя"
    assert not старая.exists() and not самая_старая.exists()


def test_загрузка_копии_не_упирается_в_предел_для_звука(data_dir, monkeypatch):
    """Перенос архивом больше max_upload_mb получал 413 «разбейте файл на части»."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MAX_UPLOAD_MB", "1")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as клиент:
        state = app.state.hub
        каталог = Path(state.settings.paths.results) / "job_x"
        каталог.mkdir(parents=True)
        (каталог / "r.bin").write_bytes(os.urandom(3 * 1024 * 1024))
        копия = backup.создать(state.db, state.settings, kind="full", include_results=True)
        файл = backup.каталог(state.settings) / копия["name"]
        данные = файл.read_bytes()
        assert len(данные) > 2 * 1024 * 1024
        файл.unlink()
        ответ = клиент.post("/api/backup/upload",
                            files={"file": (копия["name"], данные, "application/gzip")})
        assert ответ.status_code == 200, ответ.text[:300]


# ---------------------------------------------------------------------------
# 04-storage №5, №8, №6: удаление по требованию и журнал доступа
# ---------------------------------------------------------------------------


def _записи_номера(state: Any, сколько: int) -> None:
    for н in range(сколько):
        state.db.create_job({"id": f"job_{н}", "filename": f"{н}.wav", "status": "completed",
                             "text": "разговор"})
        state.db.save_call(f"pbx:{н}", job_id=f"job_{н}", src="+7 (916) 123-45-67",
                           station="pbx")


def test_удаление_по_требованию_сверх_предела_доходит_до_конца(client, monkeypatch):
    """Удалялось не больше BULK_LIMIT, а обезличивались все звонки — остаток
    по номеру было уже не найти."""
    from asrhub.api import routes_jobs

    monkeypatch.setattr(routes_jobs, "BULK_LIMIT", 3)
    state = client.app.state.hub
    _записи_номера(state, 5)
    ответ = client.post("/api/maintenance/erase",
                        json={"query": "89161234567", "dry_run": False}).json()
    assert ответ["deleted"] == 5 and ответ["more"] is False, ответ
    assert state.db.count_jobs() == 0
    проба = client.post("/api/maintenance/erase", json={"query": "89161234567"}).json()
    assert проба["matched"] == 0 and проба["calls"] == 0, проба


def test_остаток_сверх_прохода_остаётся_находимым(client, monkeypatch):
    from asrhub.api import routes_jobs, routes_system

    monkeypatch.setattr(routes_jobs, "BULK_LIMIT", 2)
    monkeypatch.setattr(routes_system, "ERASE_PASSES", 1)
    state = client.app.state.hub
    _записи_номера(state, 5)
    ответ = client.post("/api/maintenance/erase",
                        json={"query": "89161234567", "dry_run": False}).json()
    assert ответ["deleted"] == 2 and ответ["more"] is True, ответ
    проба = client.post("/api/maintenance/erase", json={"query": "89161234567"}).json()
    assert проба["matched"] == 2, "остаток уже не находится по номеру"


def test_освобождённые_страницы_затираются_при_любой_сборке(tmp_path, monkeypatch):
    """Без `secure_delete` фамилия и номер оставались в свободных страницах."""
    настоящее = sqlite3.connect

    def без_затирания(*a, **к):
        соединение = настоящее(*a, **к)
        соединение.execute("PRAGMA secure_delete=OFF")
        return соединение

    import asrhub.db as модуль

    monkeypatch.setattr(модуль.sqlite3, "connect", без_затирания)
    db = Database(tmp_path / "asrhub.db")
    try:
        assert db.conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    finally:
        db.close()


def test_скачивание_копии_всегда_в_журнале_доступа(client):
    копия = client.post("/api/backup?kind=settings").json()
    assert client.get(f"/api/backup/{копия['name']}/file").status_code == 200
    журнал = client.get("/api/audit").json()["items"]
    assert any(з["path"].endswith(f"/{копия['name']}/file") and з["method"] == "GET"
               for з in журнал), [з["path"] for з in журнал]


def test_образцы_чувствительного_чтения_совпадают_с_маршрутами(client):
    """Четыре из семи образцов не соответствовали ни одному маршруту."""
    import re

    from asrhub import audit

    пути = [re.sub(r"\{[^}]+\}", "x", п) for п, действия in
            client.app.openapi()["paths"].items() if "get" in действия]
    for образец in audit.ЧУВСТВИТЕЛЬНОЕ_ЧТЕНИЕ:
        assert any(образец.search(п) for п in пути), f"ни одного маршрута: {образец.pattern}"
    assert audit.чувствительное_чтение("/api/jobs/x/redacted")
    assert audit.чувствительное_чтение("/api/jobs/x")
    assert not audit.чувствительное_чтение("/api/jobs/models")
    assert audit.описать("POST", "/api/maintenance/erase") == "изменил: удаление по требованию"
    assert audit.записывать("POST", "/process-call", reads=False)


# ---------------------------------------------------------------------------
# 04-storage №13: звонки и срок хранения
# ---------------------------------------------------------------------------


def test_звонок_живого_задания_переживает_уборку(tmp_path):
    """Срок звонка считался от импорта, срок задания — от окончания."""
    db = Database(tmp_path / "asrhub.db")
    день, сейчас = 86400, time.time()
    db.create_job({"id": "job_1", "filename": "a.wav", "status": "completed",
                   "created_at": сейчас - 31 * день, "finished_at": сейчас - 29 * день})
    db.save_call("pbx:file:2026/09/a.wav", job_id="job_1", src="79161234567", station="pbx")
    db.save_call("pbx:file:2026/09/b.wav", station="pbx")             # без задания
    db.save_call("pbx:1700000000.1", station="pbx")                   # без задания, не файл
    db.execute("UPDATE calls SET imported_at=?", (сейчас - 40 * день,))
    db.cleanup(results_days=30, calls_file_days=60)
    assert db.call_for_job("job_1") is not None, "звонок ушёл раньше своего задания"
    assert db.call_exists("pbx:file:2026/09/b.wav"), "файл в глубине просмотра забыт"
    assert not db.call_exists("pbx:1700000000.1")
    db.cleanup(results_days=30, calls_file_days=0)
    assert not db.call_exists("pbx:file:2026/09/b.wav")
    db.close()


def test_эталон_и_открытая_проверка_не_уходят_по_сроку(tmp_path):
    db = Database(tmp_path / "asrhub.db")
    давно = time.time() - 40 * 86400
    for номер in ("эталон", "проверка", "обычное"):
        db.create_job({"id": номер, "filename": "a.wav", "status": "completed",
                       "created_at": давно, "finished_at": давно})
    db.set_mark("эталон", "reference", "yes")
    db.qa_assign("проверка", assigned_to="мария")
    db.cleanup(results_days=30)
    assert db.get_job("эталон") is not None and db.get_job("проверка") is not None
    assert db.get_job("обычное") is None
    db.close()


# ---------------------------------------------------------------------------
# 04-storage №16: уникальный указатель на старых данных
# ---------------------------------------------------------------------------


class _Записи(logging.Handler):
    """Записи журнала сервера — своим обработчиком: у `asrhub` после
    `logging_setup.setup()` распространение выключено, и caplog их не видит."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.записи: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.записи.append(record)


def test_дубли_под_уникальным_указателем_не_роняют_миграцию(tmp_path):
    путь = tmp_path / "asrhub.db"
    Database(путь).close()
    соединение = sqlite3.connect(путь)
    соединение.execute("DROP INDEX idx_users_username")
    соединение.execute("INSERT INTO users (id, username, password_hash) VALUES ('u1','Admin','x')")
    соединение.execute("INSERT INTO users (id, username, password_hash) VALUES ('u2','admin','x')")
    соединение.execute(f"PRAGMA user_version={SCHEMA_VERSION - 1}")
    соединение.commit()
    соединение.close()
    журнал = _Записи()
    logging.getLogger("asrhub.db").addHandler(журнал)
    try:
        db = Database(путь)
    finally:
        logging.getLogger("asrhub.db").removeHandler(журнал)
    assert int(db.query_one("PRAGMA user_version")[0]) == SCHEMA_VERSION
    db.close()
    assert any("Дубли" in з.getMessage() and "admin" in з.getMessage().lower()
               for з in журнал.записи), [з.getMessage() for з in журнал.записи]


# ---------------------------------------------------------------------------
# Сопутствующее: опись первой не прячет оборванный архив
# ---------------------------------------------------------------------------


def test_оборванный_архив_по_прежнему_виден_в_списке(стенд):
    """Опись теперь первая и читается и у оборванного архива — обрыв
    ловится полной проверкой, которая запоминается по размеру и времени."""
    db, настройки, _ = стенд
    целая = backup.создать(db, настройки, kind="full")
    место = Path(настройки["backup_dir"])
    данные = (место / целая["name"]).read_bytes()
    обрыв = место / f"asrhub-20260101-000000-full-обрыв{backup.РАСШИРЕНИЕ}"
    обрыв.write_bytes(данные[:len(данные) // 3])
    по_имени = {к["name"]: к for к in backup.список(настройки)}
    assert "опись не читается" in по_имени[обрыв.name].get("error", "")
    assert not по_имени[целая["name"]].get("error")
    # Повторный список итог берёт из памяти проверки, а не прогоном архива.
    прогонов: list[str] = []
    настоящее = backup.tarfile.open

    def считать(*a, **к):
        прогонов.append(str(a[0]) if a else "")
        return настоящее(*a, **к)

    import unittest.mock

    with unittest.mock.patch.object(backup.tarfile, "open", считать):
        backup.список(настройки)
    assert len(прогонов) == 2, прогонов          # по одному чтению описи на копию
