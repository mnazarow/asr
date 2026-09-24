"""Заход 42: телефония — пояс станции, отложенные, записи, журнал, AMI.

Каждая проверка воспроизводит поломку так, как её видел человек: звонок,
сдвинутый на три часа; запись соседнего звонка; хвост журнала, потерянный
при ротации; одна запись, поставленная в распознавание дважды.
"""
from __future__ import annotations

import os
import socket
import threading
import time
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.telephony import asterisk
from asrhub.telephony.asterisk import (
    Звонок,
    УказательЗаписей,
    _время,
    найти_запись,
    подставить,
    разобрать_cdr_строку,
    читать_журнал,
)
from asrhub.telephony.importer import Импортёр, Телефония
from asrhub.telephony.stations import _станция_из, проверить_набор, разобрать_пояс


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def wav(путь: Path, *, когда: float | None = None, секунд: int = 1) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(8000)
        файл.writeframes(b"\x00\x00" * (8000 * секунд))
    if когда is not None:
        os.utime(путь, (когда, когда))
    return путь


def wav_пишется(путь: Path) -> Path:
    """WAV, который MixMonitor ещё не закрыл: в заголовке нулевые размеры."""
    путь.parent.mkdir(parents=True, exist_ok=True)
    заголовок = (b"RIFF" + (0).to_bytes(4, "little") + b"WAVEfmt "
                 + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
                 + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
                 + (16000).to_bytes(4, "little") + (2).to_bytes(2, "little")
                 + (16).to_bytes(2, "little") + b"data" + (0).to_bytes(4, "little"))
    путь.write_bytes(заголовок + b"\x01\x02" * 8000)
    return путь


def строка_cdr(uid: str, src: str, dst: str, *, начало: float, duration: int = 300,
               пояс: timezone | None = None, context: str = "from-trunk") -> str:
    """Строка Master.csv, записанная по часам станции (`пояс`)."""
    def часы(момент: float) -> str:
        if пояс is None:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(момент))
        return datetime.fromtimestamp(момент, tz=пояс).strftime("%Y-%m-%d %H:%M:%S")
    н, к = часы(начало), часы(начало + duration)
    return (f'"","{src}","{dst}","{context}","<{src}>","SIP/tr","SIP/{dst}",'
            f'"Dial","SIP/{dst}","{н}","{н}","{к}",{duration},{duration - 10},'
            f'"ANSWERED","DOCUMENTATION","{uid}",""\n')


class Очередь:
    def __init__(self, пауза: float = 0.0):
        self.вызовы: list[dict] = []
        self.пауза = пауза
        self._замок = threading.Lock()

    def submit(self, **kwargs):
        if self.пауза:
            time.sleep(self.пауза)
        with self._замок:
            self.вызовы.append(kwargs)
            return {"id": f"job_{len(self.вызовы)}"}


class Настройки(dict):
    def get(self, ключ, по_умолчанию=None):                  # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def merged(self, поверх):
        значения = dict(self)
        значения.update(поверх or {})
        return значения


def поля_станции(каталог: Path, имя: str = "АТС", **поверх) -> dict:
    поля = {
        "id": "pbx", "name": имя, "enabled": True, "source": "cdr_csv",
        "cdr_file": str(каталог / "Master.csv"),
        "recordings_dir": str(каталог / "monitor"),
        "settle_s": 0, "min_duration_s": 0, "skip_unanswered": False,
        "internal_digits": "3", "poll_s": 60, "lookback_days": 3650,
        "contexts": {"from-trunk": "входящий"},
    }
    поля.update(поверх)
    return поля


def импортёр(db: Database, каталог: Path, очередь: Очередь | None = None,
             **поверх) -> Импортёр:
    return Импортёр(db, _станция_из(поля_станции(каталог, **поверх), 0),
                    очередь or Очередь(), Настройки())


def чужой_пояс() -> timezone:
    """Пояс, далёкий от пояса машины, на которой идут проверки."""
    местный = datetime.now().astimezone().utcoffset() or timedelta(0)
    сдвиг = местный + timedelta(hours=5)
    if сдвиг > timedelta(hours=12):
        сдвиг = местный - timedelta(hours=5)
    return timezone(сдвиг)


def запись_пояса(пояс: timezone) -> str:
    сдвиг = int((пояс.utcoffset(None) or timedelta(0)).total_seconds())
    знак = "+" if сдвиг >= 0 else "-"
    часы, остаток = divmod(abs(сдвиг), 3600)
    return f"{знак}{часы:02d}:{остаток // 60:02d}"


# ---------------------------------------------------------------------------
# Часовой пояс станции
# ---------------------------------------------------------------------------

def test_время_журнала_читается_в_поясе_станции():
    """Asterisk пишет местное время без пояса — читать его надо по часам станции."""
    москва = timezone(timedelta(hours=3))
    момент = _время("2026-09-10 12:00:00", москва)
    assert момент == datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc).timestamp()
    звонок = разобрать_cdr_строку(
        строка_cdr("1757500001.1", "79160000001", "101",
                   начало=1_757_500_000.0, пояс=москва), пояс=москва)
    assert звонок is not None and звонок.started_at == 1_757_500_000.0


def test_пояс_станции_разбирается_и_проверяется_при_сохранении():
    assert разобрать_пояс("") is None
    assert разобрать_пояс("+03:00").utcoffset(None) == timedelta(hours=3)
    assert разобрать_пояс("UTC-05:30").utcoffset(None) == -timedelta(hours=5, minutes=30)
    assert разобрать_пояс("Europe/Moscow") is not None
    with pytest.raises(ValueError):
        разобрать_пояс("Марс/Олимп")
    ошибки = проверить_набор([{"name": "Филиал", "source": "cdr_csv",
                               "cdr_file": "/x.csv", "timezone": "Марс/Олимп"}])
    assert any("Марс/Олимп" in о for о in ошибки)
    assert not проверить_набор([{"name": "Филиал", "source": "cdr_csv",
                                 "cdr_file": "/x.csv", "timezone": "Europe/Moscow"}])
    станция = _станция_из({"name": "Юг", "timezone": "+05:00"}, 0)
    assert станция.to_dict()["timezone"] == "+05:00"
    assert станция.пояс.utcoffset(None) == timedelta(hours=5)


def test_свежий_звонок_станции_в_другом_поясе_не_откладывается(tmp_path: Path):
    """Сервер в контейнере живёт по всемирному времени, станция — по местному.

    Без пояса станции время звонка сдвигалось на разницу поясов: закончившийся
    десять минут назад звонок выглядел закончившимся через пять часов, и
    выдержка откладывала его как «ещё пишется» на все эти часы.
    """
    пояс = чужой_пояс()
    каталог = tmp_path
    начало = time.time() - 900
    (каталог / "Master.csv").write_text(
        строка_cdr("1757600001.1", "79160000001", "101", начало=начало,
                   duration=300, пояс=пояс), encoding="utf-8")
    wav(каталог / "monitor" / "1757600001.1.wav", когда=time.time() - 400)
    очередь = Очередь()
    db = база(tmp_path)
    свод = импортёр(db, каталог, очередь, settle_s=60,
                    timezone=запись_пояса(пояс)).scan()
    assert свод["imported"] == 1, свод
    строка = db.query_one("SELECT started_at FROM calls WHERE uniqueid='pbx:1757600001.1'")
    assert abs(float(строка["started_at"]) - начало) < 2


def test_шаблон_имени_понимает_короткие_даты_и_пояс_станции():
    """Форма станции советовала ${YYYY}/${MM}/${DD} — сервер их не знал."""
    пояс = timezone(timedelta(hours=3))
    звонок = Звонок(uniqueid="1757500001.1", src="101", dst="202",
                    started_at=datetime(2026, 9, 10, 22, 30, tzinfo=timezone.utc).timestamp())
    имя = подставить("${YYYY}/${MM}/${DD}/${HH}-${UNIQUEID}.wav", звонок, пояс=пояс)
    # 22:30 по всемирному — это уже 01:30 следующего дня по Москве.
    assert имя == "2026/09/11/01-1757500001.1.wav"
    assert подставить("{год}-{месяц}-{день}", звонок, пояс=пояс) == "2026-09-11"


def test_шаблон_со_звёздочкой_находит_ближайшую_запись(tmp_path: Path):
    """Пример из справки «out-${DST}-${SRC}-${YEAR}${MONTH}${DAY}-*.wav» не работал."""
    каталог = tmp_path / "monitor"
    начало = time.time() - 3600
    день = datetime.fromtimestamp(начало).strftime("%Y%m%d")
    # В именах нет ни идентификатора, ни номеров — найти файл может только
    # шаблон, и только если он понимает звёздочку.
    далёкая = wav(каталог / f"rec-{день}-080000.wav", когда=начало - 3000)
    своя = wav(каталог / f"rec-{день}-090000.wav", когда=начало + 120)
    звонок = Звонок(uniqueid="1757500009.4", src="101", dst="202", started_at=начало)
    найдено = найти_запись(звонок, каталог, шаблон="rec-${YEAR}${MONTH}${DAY}-*.wav",
                           окно_минут=0)
    assert найдено is not None and найдено.name == своя.name
    assert далёкая.exists()


def test_идентификатор_звонка_не_находит_запись_соседнего(tmp_path: Path):
    """«1757500001.1» — не «1757500001.12»: две записи в одну секунду бывают."""
    каталог = tmp_path / "monitor"
    wav(каталог / "1757500001.12.wav")
    звонок = Звонок(uniqueid="1757500001.1", started_at=time.time())
    assert найти_запись(звонок, каталог) is None
    своя = wav(каталог / "out-1757500001.1-101.wav")
    найдено = найти_запись(звонок, каталог)
    assert найдено is not None and найдено.name == своя.name
    # Идентификатор не «секунды.порядковый» — ищется перебором, с той же границей.
    wav(каталог / "rec-abc12.wav")
    чужой = Звонок(uniqueid="abc1", started_at=time.time())
    assert найти_запись(чужой, каталог) is None


def test_каталог_записей_обходится_один_раз_за_заход(tmp_path: Path, monkeypatch):
    """Обход каталога шёл на КАЖДЫЙ звонок: сотни тысяч файлов × порция."""
    каталог = tmp_path
    строки = ""
    for н in range(6):
        uid = f"17576000{н:02d}.1"
        строки += строка_cdr(uid, "7916000000" + str(н), "101",
                             начало=time.time() - 3600 + н)
        wav(каталог / "monitor" / f"{uid}.wav", когда=time.time() - 1800)
    (каталог / "Master.csv").write_text(строки, encoding="utf-8")
    обходы: list[Path] = []
    исходный = УказательЗаписей.собрать

    def считать(self):
        обходы.append(self.каталог)
        return исходный(self)

    monkeypatch.setattr(УказательЗаписей, "собрать", считать)
    свод = импортёр(база(tmp_path), каталог).scan()
    assert свод["imported"] == 6
    assert len(обходы) == 1, обходы


# ---------------------------------------------------------------------------
# Выдержка по времени файла, замер роста, пустые записи
# ---------------------------------------------------------------------------

def _папка(tmp_path: Path, **поверх) -> tuple[Database, Импортёр, Очередь]:
    db = база(tmp_path)
    очередь = Очередь()
    поля = поля_станции(tmp_path, source="folder", **поверх)
    return db, Импортёр(db, _станция_из(поля, 0), очередь, Настройки()), очередь


def test_дописываемый_wav_из_каталога_выдерживается(tmp_path: Path):
    """У WAV, который ещё пишется, в заголовке нули — длительность ноль.

    Проверка выдержки была написана как «если длительность известна» и не
    выполнялась вовсе: файл уходил в распознавание обрезанным.
    """
    wav_пишется(tmp_path / "monitor" / "in-79160000001-101.wav")
    db, имп, очередь = _папка(tmp_path, settle_s=30)
    свод = имп.scan()
    assert свод["imported"] == 0, свод
    assert not очередь.вызовы
    строка = db.query_one("SELECT skipped FROM calls")
    assert строка["skipped"].startswith(db.ОТЛОЖЕН) and "пишется" in строка["skipped"]


def test_давний_файл_берётся_без_замера_роста(tmp_path: Path, monkeypatch):
    """Полсекунды замера на КАЖДЫЙ файл — это часы на сборе архива."""
    замеры: list[Path] = []
    monkeypatch.setattr(Импортёр, "_дописан", lambda self, путь: замеры.append(путь) or True)
    wav(tmp_path / "monitor" / "a-79160000001-101.wav", когда=time.time() - 3600)
    _, имп, очередь = _папка(tmp_path, settle_s=30)
    assert имп.scan()["imported"] == 1
    assert замеры == []
    # Свежий (старше выдержки, но моложе десяти минут) — с замером.
    wav(tmp_path / "monitor" / "b-79160000002-102.wav", когда=time.time() - 120)
    assert имп.scan()["imported"] == 1
    assert len(замеры) == 1


def test_пустая_запись_не_висит_в_отложенных(tmp_path: Path):
    """Файл нулевой длины через десять минут — это запись, которая не велась."""
    каталог = tmp_path / "monitor"
    каталог.mkdir(parents=True)
    старый = каталог / "old-79160000001-101.wav"
    старый.write_bytes(b"")
    os.utime(старый, (time.time() - 3600, time.time() - 3600))
    свежий = каталог / "new-79160000002-102.wav"
    свежий.write_bytes(b"")
    db, имп, очередь = _папка(tmp_path, settle_s=0)
    свод = имп.scan()
    assert свод["imported"] == 0 and not очередь.вызовы
    причины = {r["uniqueid"].split("file:", 1)[1]: r["skipped"]
               for r in db.query("SELECT uniqueid, skipped FROM calls")}
    assert причины["old-79160000001-101.wav"] == "пустая запись"
    assert причины["new-79160000002-102.wav"].startswith(db.ОТЛОЖЕН)


# ---------------------------------------------------------------------------
# Отложенные: срок от первого откладывания, окончательный пропуск
# ---------------------------------------------------------------------------

def test_отложенный_стареет_от_первого_откладывания(tmp_path: Path, monkeypatch):
    """Каждое повторное откладывание продлевало жизнь: безнадёжный звонок вечен."""
    import asrhub.db as модуль_бд

    db = база(tmp_path)
    t0 = time.time() - 30 * 3600
    часы = {"сейчас": t0}
    monkeypatch.setattr(модуль_бд, "now", lambda: часы["сейчас"])
    for сдвиг in (0, 10 * 3600, 20 * 3600, 29 * 3600):
        часы["сейчас"] = t0 + сдвиг
        db.save_call("pbx:вечный", job_id=None, skipped=f"{db.ОТЛОЖЕН}файл ещё растёт",
                     owner="telephony", station="pbx", pbx_uid="вечный",
                     src="1", dst="2", started_at=t0, duration=100)
    строка = db.query_one("SELECT deferred_at, imported_at FROM calls")
    assert строка["deferred_at"] == t0
    порог = t0 + 29 * 3600 - 24 * 3600
    assert db.calls_deferred(station="pbx", older_than=порог) == []
    assert db.calls_expire_deferred(station="pbx", older_than=порог) == 1
    итог = db.query_one("SELECT skipped, deferred_at FROM calls")
    assert итог["skipped"] == f"{db.НЕ_ДОЖДАЛИСЬ}файл ещё растёт"
    assert итог["deferred_at"] is None
    # Взятый звонок забывает время откладывания.
    db.save_call("pbx:вечный", job_id="job_1", skipped="", station="pbx")
    assert db.query_one("SELECT deferred_at FROM calls")["deferred_at"] is None


def test_заход_переводит_просроченные_в_пропущенные(tmp_path: Path):
    db = база(tmp_path)
    давно = time.time() - 3 * 86400
    db.save_call("pbx:старый", job_id=None, skipped=f"{db.ОТЛОЖЕН}не задан каталог записей",
                 owner="telephony", station="pbx", pbx_uid="старый", started_at=давно)
    db.execute("UPDATE calls SET deferred_at=?, imported_at=?", [давно, давно])
    (tmp_path / "Master.csv").write_text("", encoding="utf-8")
    свод = импортёр(db, tmp_path).scan()
    assert свод["deferred"] == 0
    assert db.query_one("SELECT skipped FROM calls")["skipped"].startswith(db.НЕ_ДОЖДАЛИСЬ)


# ---------------------------------------------------------------------------
# Одна станция — один заход: замок процесса и аренда в базе
# ---------------------------------------------------------------------------

def test_два_импортёра_одной_станции_не_задваивают_отложенные(tmp_path: Path):
    """Правка полей станции заводит новый импортёр, а старый ещё доделывает заход.

    У каждого был свой замок, отложенные звонки проходят мимо `call_exists` —
    и одна запись уезжала в распознавание двумя заданиями.
    """
    db = база(tmp_path)
    for н in range(4):
        uid = f"17577000{н:02d}.1"
        wav(tmp_path / "monitor" / f"{uid}.wav", когда=time.time() - 3600)
        db.save_call(f"pbx:{uid}", job_id=None, skipped=f"{db.ОТЛОЖЕН}очередь отказала",
                     owner="telephony", station="pbx", pbx_uid=uid, src="1", dst="2",
                     started_at=time.time() - 3600, duration=60)
    (tmp_path / "Master.csv").write_text("", encoding="utf-8")
    очередь = Очередь(пауза=0.05)
    старый, новый = импортёр(db, tmp_path, очередь), импортёр(db, tmp_path, очередь)
    потоки = [threading.Thread(target=и.scan) for и in (старый, новый)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=30)
    файлы = [в["file_path"].name for в in очередь.вызовы]
    assert sorted(файлы) == sorted(set(файлы)), файлы
    assert len(файлы) == 4


def test_станцию_соседнего_сервера_не_трогаем(tmp_path: Path):
    """Два сервера над одной базой не должны вести одну станцию вдвоём."""
    db = база(tmp_path)
    (tmp_path / "Master.csv").write_text(
        строка_cdr("1757800001.1", "79160000001", "101", начало=time.time() - 3600),
        encoding="utf-8")
    wav(tmp_path / "monitor" / "1757800001.1.wav", когда=time.time() - 3000)
    assert db.lease_take("telephony_lease:pbx", "другая-машина:4242", 600) is None
    очередь = Очередь()
    свод = импортёр(db, tmp_path, очередь).scan()
    assert свод["held_by"] == "другая-машина:4242"
    assert not очередь.вызовы
    # Сосед умер — аренда истекла, станцию подхватываем.
    db.set_kv("telephony_lease:pbx", {"holder": "другая-машина:4242",
                                      "until": time.time() - 1})
    assert импортёр(db, tmp_path, очередь).scan()["imported"] == 1


def test_аренда_мёртвого_процесса_этой_машины_свободна(tmp_path: Path):
    from asrhub.instance import HOSTNAME

    db = база(tmp_path)
    мёртвый = f"{HOSTNAME}:999999"
    db.set_kv("telephony_lease:pbx", {"holder": мёртвый, "until": time.time() + 600})
    assert db.lease_take("telephony_lease:pbx", "я:1", 600) is None
    assert db.lease_release("telephony_lease:pbx", "кто-то-ещё") is False
    assert db.lease_release("telephony_lease:pbx", "я:1") is True


# ---------------------------------------------------------------------------
# Ротация журнала
# ---------------------------------------------------------------------------

def test_хвост_повёрнутого_журнала_дочитывается(tmp_path: Path):
    """Звонки между последним заходом и ротацией терялись: новый файл — с начала."""
    журнал = tmp_path / "Master.csv"
    начало = time.time() - 3600
    журнал.write_text(строка_cdr("1757900001.1", "1", "101", начало=начало), encoding="utf-8")
    звонки, позиция, inode = читать_журнал(журнал, offset=0, inode=0, limit=50)
    assert [з.uniqueid for з in звонки] == ["1757900001.1"]
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write(строка_cdr("1757900002.1", "2", "102", начало=начало + 60))
    журнал.rename(tmp_path / "Master.csv.1")
    журнал.write_text(строка_cdr("1757900003.1", "3", "103", начало=начало + 120),
                      encoding="utf-8")
    хвост, позиция, inode_хвоста = читать_журнал(журнал, offset=позиция, inode=inode)
    assert [з.uniqueid for з in хвост] == ["1757900002.1"]
    assert inode_хвоста == inode and хвост[0].inode_журнала == inode
    новые, позиция, inode_новый = читать_журнал(журнал, offset=позиция, inode=inode_хвоста)
    assert [з.uniqueid for з in новые] == ["1757900003.1"]
    assert inode_новый == журнал.stat().st_ino != inode


def test_хвост_после_copytruncate_берётся_из_копии(tmp_path: Path):
    журнал = tmp_path / "Master.csv"
    начало = time.time() - 3600
    журнал.write_text(строка_cdr("1758000001.1", "1", "101", начало=начало), encoding="utf-8")
    _, позиция, inode = читать_журнал(журнал)
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write(строка_cdr("1758000002.1", "2", "102", начало=начало + 60))
    (tmp_path / "Master.csv.1").write_bytes(журнал.read_bytes())
    with журнал.open("w", encoding="utf-8") as файл:          # обрезан на месте
        файл.write(строка_cdr("1758000003.1", "3", "103", начало=начало + 120)[:40])
    assert журнал.stat().st_ino == inode
    хвост, позиция, inode_копии = читать_журнал(журнал, offset=позиция, inode=inode)
    assert [з.uniqueid for з in хвост] == ["1758000002.1"]
    assert inode_копии == (tmp_path / "Master.csv.1").stat().st_ino


def test_импорт_не_теряет_звонки_при_ротации(tmp_path: Path):
    db = база(tmp_path)
    журнал = tmp_path / "Master.csv"
    начало = time.time() - 3600
    журнал.write_text(строка_cdr("1758100001.1", "1", "101", начало=начало), encoding="utf-8")
    очередь = Очередь()
    имп = импортёр(db, tmp_path, очередь)
    for uid in ("1758100001.1", "1758100002.1", "1758100003.1"):
        wav(tmp_path / "monitor" / f"{uid}.wav", когда=time.time() - 1800)
    assert имп.scan()["imported"] == 1
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write(строка_cdr("1758100002.1", "2", "102", начало=начало + 60))
    журнал.rename(tmp_path / "Master.csv.1")
    журнал.write_text(строка_cdr("1758100003.1", "3", "103", начало=начало + 120),
                      encoding="utf-8")
    for _n in range(3):
        имп.scan()
    взятые = sorted(в["file_path"].name for в in очередь.вызовы)
    assert взятые == ["1758100001.1.wav", "1758100002.1.wav", "1758100003.1.wav"]


# ---------------------------------------------------------------------------
# AMI: события при входе и сохранённый пароль в проверке
# ---------------------------------------------------------------------------

class ПоддельныйAMI:
    """Слушает порт, запоминает пакеты входа и отвечает «Success»."""

    def __init__(self) -> None:
        self.сокет = socket.socket()
        self.сокет.bind(("127.0.0.1", 0))
        self.сокет.listen(5)
        self.порт = self.сокет.getsockname()[1]
        self.входы: list[dict[str, str]] = []
        self._стоп = False
        threading.Thread(target=self._служить, daemon=True).start()

    def _служить(self) -> None:
        while not self._стоп:
            try:
                связь, _ = self.сокет.accept()
            except OSError:
                return
            threading.Thread(target=self._разговор, args=(связь,), daemon=True).start()

    def _разговор(self, связь: socket.socket) -> None:
        связь.settimeout(5)
        связь.sendall(b"Asterisk Call Manager/5.0.1\r\n")
        буфер = b""
        try:
            while True:
                кусок = связь.recv(4096)
                if not кусок:
                    return
                буфер += кусок
                while b"\r\n\r\n" in буфер:
                    пакет, буфер = буфер.split(b"\r\n\r\n", 1)
                    поля = dict(с.split(": ", 1) for с in пакет.decode().split("\r\n")
                                if ": " in с)
                    if поля.get("Action", "").lower() == "login":
                        self.входы.append(поля)
                    связь.sendall(b"Response: Success\r\nMessage: ok\r\n\r\n")
        except OSError:
            return
        finally:
            связь.close()

    def закрыть(self) -> None:
        self._стоп = True
        self.сокет.close()


@pytest.fixture()
def ами():
    сервер = ПоддельныйAMI()
    yield сервер
    сервер.закрыть()


def test_забор_входит_в_ami_только_за_событиями_cdr(ами: ПоддельныйAMI):
    """События всех классов забивали буфер, который читается пять секунд в минуту."""
    клиент = asterisk.AMIClient("127.0.0.1", ами.порт, "asrhub", "пароль", timeout=3)
    клиент.connect()
    клиент.close()
    asterisk.проверить("127.0.0.1", ами.порт, "asrhub", "пароль", timeout=3)
    assert [в["Events"] for в in ами.входы] == ["cdr", "off"]


def test_проверка_станции_берёт_сохранённый_пароль(ами: ПоддельныйAMI, tmp_path: Path):
    """Форма правки не присылает нетронутый пароль — проверка шла с пустым."""
    настройки = Настройки(telephony_stations=[{
        "id": "office", "name": "Офис", "source": "ami", "host": "127.0.0.1",
        "port": ами.порт, "username": "asrhub", "secret": "s3cret"}])
    телефония = Телефония(база(tmp_path), настройки, Очередь())
    телефония.проверить_набросок({"id": "office", "name": "Офис", "source": "ami",
                                  "host": "127.0.0.1", "port": ами.порт,
                                  "username": "asrhub", "secret": ""})
    assert ами.входы[-1]["Secret"] == "s3cret"
    # Другой узел или учётная запись — сохранённый пароль туда не уходит.
    телефония.проверить_набросок({"id": "office", "name": "Офис", "source": "ami",
                                  "host": "127.0.0.1", "port": ами.порт,
                                  "username": "другой", "secret": "***"})
    assert ами.входы[-1].get("Secret", "") in ("", "***")


# ---------------------------------------------------------------------------
# Разбор забора: пояс журнала
# ---------------------------------------------------------------------------

def test_разбор_забора_называет_пояс_станции(tmp_path: Path):
    """Время последней строки журнала и время записи файла — одно мгновение."""
    пояс = чужой_пояс()
    журнал = tmp_path / "Master.csv"
    конец = time.time() - 60
    журнал.write_text(строка_cdr("1758200001.1", "1", "101", начало=конец - 300,
                                 duration=300, пояс=пояс), encoding="utf-8")
    os.utime(журнал, (конец, конец))
    (tmp_path / "monitor").mkdir()
    db = база(tmp_path)
    отчёт = импортёр(db, tmp_path).диагностика()
    проверка = next(п for п in отчёт["checks"] if п["id"] == "cdr_timezone")
    assert проверка["state"] == "warn", проверка
    assert проверка["details"]["suggested"] == f"UTC{запись_пояса(пояс)}"
    исправлено = импортёр(db, tmp_path, timezone=запись_пояса(пояс)).диагностика()
    проверка = next(п for п in исправлено["checks"] if п["id"] == "cdr_timezone")
    assert проверка["state"] == "ok", проверка
