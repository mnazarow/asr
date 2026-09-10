"""Телефония: разбор журнала АТС, поиск записи, импорт, разрез по владельцу.

Каждая проверка написана так, чтобы падать на конкретной ошибке, которая
уже делалась или напрашивается: сдвиг полей от запятой в имени звонящего,
задание, заведённое дважды, запись, взятая раньше, чем её дописали, и
разрез по владельцу, который забыли применить.
"""
from __future__ import annotations

import socket
import threading
import time
import wave
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.errors import ConfigError, StorageError
from asrhub.telephony import Импортёр
from asrhub.telephony.asterisk import (
    AMIClient,
    AMIError,
    Звонок,
    найти_запись,
    направление,
    подставить,
    проверить,
    разобрать_cdr_строку,
    читать_csv,
)

# ---------------------------------------------------------------------------
# Опоры
# ---------------------------------------------------------------------------

СЕЙЧАС = time.time() - 600


def строка_cdr(uid: str, src: str, dst: str, disposition: str = "ANSWERED",
               duration: int = 300, billsec: int = 290, *,
               context: str = "from-trunk", clid: str | None = None,
               lastapp: str = "Queue", lastdata: str = "sales,t",
               начало: float = СЕЙЧАС) -> str:
    """Строка Master.csv в том виде, в каком её пишет Asterisk."""
    н = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало))
    к = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(начало + duration))
    имя = clid if clid is not None else f'"""Клиент, отдел"" <{src}>"'
    return (f'"","{src}","{dst}","{context}",{имя},"SIP/tr-01","SIP/{dst}",'
            f'"{lastapp}","{lastdata}","{н}","{н}","{к}",{duration},{billsec},'
            f'"{disposition}","DOCUMENTATION","{uid}",""\n')


def wav(путь: Path, секунд: int = 1) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"\x00\x00" * (16000 * секунд))
    return путь


class Настройки(dict):
    """Минимальные настройки: только то, что читает импортёр."""

    def get(self, ключ, по_умолчанию=None):        # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def merged(self, поверх):
        значения = dict(self)
        значения.update(поверх or {})
        return значения


class Очередь:
    """Очередь-пустышка: запоминает, с чем её позвали."""

    def __init__(self, падать: bool = False):
        self.вызовы: list[dict] = []
        self.падать = падать

    def submit(self, **kwargs):
        if self.падать:
            raise RuntimeError("очередь переполнена")
        self.вызовы.append(kwargs)
        return {"id": f"job_{len(self.вызовы)}"}


def настройки(tmp_path: Path, **поверх) -> Настройки:
    значения = {
        "telephony_enabled": True,
        "telephony_source": "cdr_csv",
        "telephony_cdr_file": str(tmp_path / "Master.csv"),
        "telephony_recordings_dir": str(tmp_path / "monitor"),
        "telephony_settle_s": 0,
        "telephony_min_duration_s": 10,
        "telephony_skip_unanswered": True,
        "telephony_internal_digits": 5,
        "telephony_contexts": {"from-trunk": "входящий", "from-internal": "исходящий"},
        "telephony_owner": "telephony",
        "telephony_owner_map": {},
        "telephony_priority": 40,
        "telephony_lookback_days": 7,
        "telephony_filename": "",
    }
    значения.update(поверх)
    return Настройки(значения)


def стенд(tmp_path: Path, строки: str, **поверх):
    (tmp_path / "Master.csv").write_text(строки, encoding="utf-8")
    (tmp_path / "monitor").mkdir(exist_ok=True)
    db = Database(tmp_path / "asrhub.db")
    очередь = Очередь()
    return db, очередь, Импортёр(db, настройки(tmp_path, **поверх), очередь)


# ---------------------------------------------------------------------------
# Разбор журнала
# ---------------------------------------------------------------------------

def test_запятая_в_имени_звонящего_не_сдвигает_поля():
    """«Иванов, отдел продаж» — обычное имя, и делить строку по запятой нельзя.

    Деление по запятой сдвигало бы всё правее имени: номер, длительность и
    идентификатор звонка уезжали бы на поле, и звонок попадал бы в базу с
    чужими числами.
    """
    звонок = разобрать_cdr_строку(строка_cdr("1757500001.1", "79161234567", "101"))
    assert звонок is not None
    assert звонок.uniqueid == "1757500001.1"
    assert звонок.src == "79161234567"
    assert звонок.dst == "101"
    assert звонок.billsec == 290
    assert "Клиент, отдел" in звонок.clid


def test_обратная_косая_как_экранирование_тоже_разбирается():
    """Часть надстроек над АТС экранирует кавычку косой, а не удвоением."""
    обычная = строка_cdr("1757500002.2", "79990001122", "102",
                         clid='"\\"Иванов, отдел\\" <79990001122>"')
    звонок = разобрать_cdr_строку(обычная)
    assert звонок is not None
    assert звонок.uniqueid == "1757500002.2", "поля сдвинулись при разборе"
    assert звонок.src == "79990001122"


@pytest.mark.parametrize("строка", ["", "   ", "совсем не csv", '"a","b","c"'])
def test_мусорная_строка_не_роняет_разбор(строка):
    assert разобрать_cdr_строку(строка) is None


def test_журнал_читается_с_запомненной_позиции(tmp_path: Path):
    """Второй заход не должен перечитывать то, что уже разобрано."""
    журнал = tmp_path / "Master.csv"
    журнал.write_text(строка_cdr("a.1", "79161234567", "101"), encoding="utf-8")
    звонки, позиция = читать_csv(журнал, offset=0, limit=100)
    assert len(звонки) == 1
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write(строка_cdr("a.2", "79161234567", "102"))
    ещё, позиция2 = читать_csv(журнал, offset=позиция, limit=100)
    assert [з.uniqueid for з in ещё] == ["a.2"]
    assert позиция2 > позиция


def test_недописанная_строка_остаётся_следующему_заходу(tmp_path: Path):
    """Строку без перевода строки АТС ещё дописывает — брать её рано."""
    журнал = tmp_path / "Master.csv"
    журнал.write_text(строка_cdr("b.1", "79161234567", "101")
                      + '"","79161234567","102","from-trunk"', encoding="utf-8")
    звонки, позиция = читать_csv(журнал, offset=0, limit=100)
    assert [з.uniqueid for з in звонки] == ["b.1"]
    # Дописали хвост — и он разобрался следующим заходом целиком.
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write(строка_cdr("b.2", "79161234567", "102")[len(
            '"","79161234567","102","from-trunk"'):])
    ещё, _ = читать_csv(журнал, offset=позиция, limit=100)
    assert [з.uniqueid for з in ещё] == ["b.2"]


def test_ротация_журнала_не_теряет_звонки(tmp_path: Path):
    """Журнал повернули — файл стал короче, и читать надо с начала."""
    журнал = tmp_path / "Master.csv"
    журнал.write_text(строка_cdr("c.1", "79161234567", "101") * 3, encoding="utf-8")
    _, позиция = читать_csv(журнал, offset=0, limit=100)
    журнал.write_text(строка_cdr("c.9", "79161234567", "109"), encoding="utf-8")
    звонки, _ = читать_csv(журнал, offset=позиция, limit=100)
    assert [з.uniqueid for з in звонки] == ["c.9"]


def test_предел_порции_соблюдается(tmp_path: Path):
    журнал = tmp_path / "Master.csv"
    журнал.write_text("".join(строка_cdr(f"d.{i}", "79161234567", "101")
                              for i in range(50)), encoding="utf-8")
    звонки, позиция = читать_csv(журнал, offset=0, limit=10)
    assert len(звонки) == 10
    assert 0 < позиция < журнал.stat().st_size, "позиция должна встать на 10-й строке"


# ---------------------------------------------------------------------------
# Направление и поиск файла
# ---------------------------------------------------------------------------

def test_направление_берётся_из_контекста_а_не_из_длины():
    """Контекст диалплана знает точно; длина номера только догадывается."""
    звонок = Звонок(uniqueid="1.1", src="79161234567", dst="79990001122",
                    context="from-internal")
    assert направление(звонок, контексты={"from-internal": "исходящий"}) == "исходящий"
    # Без контекста оба номера длинные — догадка честно скажет «внешний».
    assert направление(звонок, контексты={}) != "исходящий"


@pytest.mark.parametrize("src,dst,ожидание", [
    ("79161234567", "101", "входящий"),
    ("101", "79161234567", "исходящий"),
    ("101", "102", "внутренний"),
])
def test_направление_по_длине_номера(src, dst, ожидание):
    звонок = Звонок(uniqueid="1.1", src=src, dst=dst)
    assert направление(звонок, внутренние_знаков=5, контексты={}) == ожидание


def test_запись_находится_по_идентификатору(tmp_path: Path):
    каталог = tmp_path / "monitor"
    wav(каталог / "2026" / "09" / "out-101-1757500001.1-20260910.wav")
    звонок = Звонок(uniqueid="1757500001.1", started_at=time.time())
    найдено = найти_запись(звонок, каталог, окно_дней=7)
    assert найдено is not None and "1757500001.1" in найдено.name


def test_шаблон_имени_имеет_приоритет_над_поиском(tmp_path: Path):
    каталог = tmp_path / "monitor"
    wav(каталог / "точный.wav")
    wav(каталог / "1757500001.1-запасной.wav")
    звонок = Звонок(uniqueid="1757500001.1", started_at=time.time())
    найдено = найти_запись(звонок, каталог, шаблон="точный.wav", окно_дней=7)
    assert найдено is not None and найдено.name == "точный.wav"


def test_подстановки_шаблона():
    звонок = Звонок(uniqueid="1757500001.1", src="79161234567", dst="101",
                    started_at=time.mktime((2026, 9, 10, 14, 3, 11, 0, 0, -1)),
                    direction="входящий")
    имя = подставить("${YEAR}/${MONTH}/${DAY}/${UNIQUEID}-${SRC}-${DST}.wav", звонок)
    assert имя == "2026/09/10/1757500001.1-79161234567-101.wav"


def test_шаблон_из_справки_работает_как_написано(tmp_path: Path):
    """Пример «${YEAR}/${MONTH}/${DAY}/${UNIQUEID}.wav» обещан в справке параметра.

    Подстановка дат делалась только в написании «{year}», поэтому путь
    получался с фигурными скобками как есть, и запись не находилась
    никогда — при том, что настройка выглядела правильной.
    """
    момент = time.mktime((2026, 9, 10, 14, 3, 11, 0, 0, -1))
    звонок = Звонок(uniqueid="1757500001.1", src="79161234567", dst="101",
                    started_at=момент)
    assert подставить("${YEAR}/${MONTH}/${DAY}/${UNIQUEID}.wav", звонок) == \
        "2026/09/10/1757500001.1.wav"
    # И тот же шаблон должен выигрывать у переборного поиска: рядом лежит
    # файл с тем же идентификатором в имени, который перебор нашёл бы
    # первым, — а шаблон обязан привести именно к своему.
    каталог = tmp_path / "monitor"
    wav(каталог / "2026" / "09" / "10" / "1757500001.1.wav")
    wav(каталог / "мусор-1757500001.1-копия.wav")
    найдено = найти_запись(звонок, каталог,
                           шаблон="${YEAR}/${MONTH}/${DAY}/${UNIQUEID}.wav",
                           окно_дней=7)
    assert найдено is not None
    assert найдено.parent.name == "10", "шаблон не сработал, нашлось перебором"


def test_номер_звонящего_не_уводит_поиск_из_каталога(tmp_path: Path):
    """Caller ID задаёт тот, кто звонит, а он попадает в путь для чтения.

    Без очистки «../» в номере шаблон вида «${SRC}.wav» открывал бы любой
    файл, доступный пользователю сервера, — и расшифровка показала бы его
    содержимое в интерфейсе.
    """
    каталог = tmp_path / "monitor"
    каталог.mkdir(parents=True, exist_ok=True)
    (tmp_path / "секрет.wav").write_bytes(b"RIFF....WAVE")
    звонок = Звонок(uniqueid="1757500001.1", src="../секрет", dst="101",
                    started_at=time.time())
    assert подставить("${SRC}.wav", звонок) == "__секрет.wav"
    assert найти_запись(звонок, каталог, шаблон="${SRC}.wav", окно_дней=7) is None


def test_ссылка_из_каталога_наружу_не_открывается(tmp_path: Path):
    """Очистка полей не спасает от символической ссылки внутри каталога.

    Ссылку кладёт не звонящий, а тот, у кого есть доступ к каталогу
    записей, — и шаблон, честно составленный из безопасных знаков, всё
    равно приводит наружу. Поэтому найденный путь проверяется на то, что
    он остался внутри каталога, уже после разрешения ссылок.
    """
    каталог = tmp_path / "monitor"
    каталог.mkdir(parents=True, exist_ok=True)
    снаружи = wav(tmp_path / "чужое" / "секрет.wav")
    ссылка = каталог / "1757500001.1.wav"
    try:
        ссылка.symlink_to(снаружи)
    except (OSError, NotImplementedError):
        pytest.skip("файловая система без символических ссылок")
    звонок = Звонок(uniqueid="1757500001.1", started_at=time.time())
    assert найти_запись(звонок, каталог, шаблон="${UNIQUEID}.wav",
                        окно_дней=7) is None


def test_чужой_файл_за_окном_не_подбирается(tmp_path: Path):
    """Файл недельной давности не может быть записью сегодняшнего звонка."""
    каталог = tmp_path / "monitor"
    старый = wav(каталог / "1757500001.1.wav")
    import os
    давно = time.time() - 30 * 86400
    os.utime(старый, (давно, давно))
    звонок = Звонок(uniqueid="совсем-другой", started_at=time.time())
    assert найти_запись(звонок, каталог, окно_дней=2) is None


# ---------------------------------------------------------------------------
# Импорт
# ---------------------------------------------------------------------------

def test_импорт_ставит_задание_и_запоминает_звонок(tmp_path: Path):
    db, очередь, имп = стенд(tmp_path, строка_cdr("e.1", "79161234567", "101"))
    wav(tmp_path / "monitor" / "e.1.wav")
    итог = имп.scan()
    assert итог["imported"] == 1
    assert len(очередь.вызовы) == 1
    вызов = очередь.вызовы[0]
    assert вызов["source"] == "asterisk"
    assert "АТС" in вызов["tags"] and "входящий" in вызов["tags"]
    звонок = db.call_for_job("job_1")
    assert звонок is not None
    assert звонок["direction"] == "входящий"
    assert звонок["queue"] == "sales", "очередь берётся из lastdata приложения Queue"


def test_повторный_заход_не_заводит_задание_дважды(tmp_path: Path):
    """Главный предохранитель: даже потерянная позиция не даёт дубля."""
    db, очередь, имп = стенд(tmp_path, строка_cdr("f.1", "79161234567", "101"))
    wav(tmp_path / "monitor" / "f.1.wav")
    assert имп.scan()["imported"] == 1
    db.set_kv("telephony_cdr_offset", 0)          # как будто позицию потеряли
    итог = имп.scan()
    assert итог["imported"] == 0
    assert итог["reasons"] == {"уже импортирован": 1}
    assert len(очередь.вызовы) == 1


def test_короткие_и_неотвеченные_не_распознаются_но_учитываются(tmp_path: Path):
    журнал = (строка_cdr("g.1", "79161234567", "101", "NO ANSWER", 12, 0)
              + строка_cdr("g.2", "79990001122", "102", "ANSWERED", 8, 5))
    db, очередь, имп = стенд(tmp_path, журнал)
    wav(tmp_path / "monitor" / "g.1.wav")
    wav(tmp_path / "monitor" / "g.2.wav")
    итог = имп.scan()
    assert итог["imported"] == 0
    assert очередь.вызовы == []
    assert set(итог["reasons"]) == {"без ответа", "короткий"}
    свод = db.call_counts()
    assert свод["total"] == 2 and свод["skipped"] == 2, "учёт ведётся всё равно"


def test_выдержка_не_даёт_взять_недописанную_запись(tmp_path: Path):
    """Разговор кончился секунду назад — MixMonitor ещё закрывает файл."""
    журнал = строка_cdr("h.1", "79161234567", "101", duration=60, billsec=55,
                        начало=time.time() - 55)
    db, очередь, имп = стенд(tmp_path, журнал, telephony_settle_s=300)
    wav(tmp_path / "monitor" / "h.1.wav")
    итог = имп.scan()
    assert итог["imported"] == 0
    assert итог["reasons"] == {"ещё пишется": 1}
    assert not db.call_exists("h.1"), "недовыдержанный звонок не помечается разобранным"


def test_звонок_без_записи_помечается_и_не_ищется_вечно(tmp_path: Path):
    db, очередь, имп = стенд(tmp_path, строка_cdr("i.1", "79161234567", "101"))
    итог = имп.scan()
    assert итог["reasons"] == {"нет записи": 1}
    assert db.call_exists("i.1"), "иначе файл искался бы на каждом заходе"


def test_владелец_берётся_по_номеру_оператора(tmp_path: Path):
    журнал = (строка_cdr("j.1", "79161234567", "101")
              + строка_cdr("j.2", "79161234567", "201"))
    db, очередь, имп = стенд(tmp_path, журнал,
                             telephony_owner_map={"101": "sales", "201": "support"})
    wav(tmp_path / "monitor" / "j.1.wav")
    wav(tmp_path / "monitor" / "j.2.wav")
    имп.scan()
    владельцы = {в["owner"] for в in очередь.вызовы}
    assert владельцы == {"sales", "support"}
    assert db.list_calls(owner="sales")["total"] == 1
    assert db.list_calls(owner="support")["total"] == 1


def test_неизвестный_номер_достаётся_владельцу_по_умолчанию(tmp_path: Path):
    db, очередь, имп = стенд(tmp_path, строка_cdr("k.1", "79161234567", "999"),
                             telephony_owner_map={"101": "sales"},
                             telephony_owner="общий")
    wav(tmp_path / "monitor" / "k.1.wav")
    имп.scan()
    assert очередь.вызовы[0]["owner"] == "общий"


def test_отказ_очереди_не_помечает_звонок_разобранным(tmp_path: Path):
    """Иначе звонок пропал бы навсегда: очередь отказала — и больше не придёт."""
    db = Database(tmp_path / "asrhub.db")
    (tmp_path / "Master.csv").write_text(строка_cdr("l.1", "79161234567", "101"),
                                         encoding="utf-8")
    (tmp_path / "monitor").mkdir()
    wav(tmp_path / "monitor" / "l.1.wav")
    имп = Импортёр(db, настройки(tmp_path), Очередь(падать=True))
    итог = имп.scan()
    assert итог["reasons"] == {"очередь отказала": 1}
    assert not db.call_exists("l.1")


def test_один_плохой_звонок_не_уносит_всю_порцию(tmp_path: Path):
    """Позиция чтения уже сдвинута — остаток порции не придёт никогда.

    Значит, исключение на одном звонке означало бы тихую потерю всех
    следующих за ним разговоров, и заметить это можно было бы только по
    дыре в архиве через неделю.
    """
    журнал = "".join(строка_cdr(f"s.{i}", "79161234567", f"10{i}") for i in range(4))
    db, очередь, имп = стенд(tmp_path, журнал)
    for i in range(4):
        wav(tmp_path / "monitor" / f"s.{i}.wav")

    настоящий = имп._взять
    def падать_на_втором(звонок):
        if звонок.uniqueid == "s.1":
            raise RuntimeError("что-то пошло не так")
        return настоящий(звонок)
    имп._взять = падать_на_втором

    итог = имп.scan()
    assert итог["seen"] == 4
    assert итог["imported"] == 3, "три оставшихся звонка должны дойти до очереди"
    assert итог["reasons"].get("сбой разбора") == 1
    assert {з["uniqueid"] for з in db.list_calls()["calls"]} == {"s.0", "s.2", "s.3"}


def test_два_захода_разом_не_читают_журнал_дважды(tmp_path: Path):
    """Кнопка «Забрать сейчас» и фоновый поток не должны идти вместе."""
    журнал = "".join(строка_cdr(f"t.{i}", "79161234567", "101") for i in range(3))
    db, очередь, имп = стенд(tmp_path, журнал)
    for i in range(3):
        wav(tmp_path / "monitor" / f"t.{i}.wav")

    прочитано: list[int] = []
    исходный = имп._из_csv
    def медленно(limit):
        # Пауза ДО чтения, а не после: она и есть то окно, в которое второй
        # заход успевает войти и прочитать журнал с той же позиции. После
        # чтения позиция уже сдвинута, и окна не остаётся — первая редакция
        # этой проверки именно поэтому проходила и на коде без замка.
        time.sleep(0.35)
        звонки = исходный(limit)
        прочитано.append(len(звонки))
        return звонки
    имп._из_csv = медленно

    итоги: list[dict] = []
    потоки = [threading.Thread(target=lambda: итоги.append(имп.scan()))
              for _ in range(2)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=10)

    assert sum(прочитано) == 3, f"журнал прочитан дважды: {прочитано}"
    assert sum(и["imported"] for и in итоги) == 3
    assert len(очередь.вызовы) == 3


def test_источник_папка_берёт_каждый_файл_ровно_раз(tmp_path: Path):
    каталог = tmp_path / "monitor"
    wav(каталог / "in-79161234567-101-20260910.wav")
    wav(каталог / "in-79990001122-102-20260910.wav")
    db = Database(tmp_path / "asrhub.db")
    очередь = Очередь()
    имп = Импортёр(db, настройки(tmp_path, telephony_source="folder"), очередь)
    assert имп.scan()["imported"] == 2
    assert имп.scan()["imported"] == 0, "второй заход не должен задваивать"
    assert len(очередь.вызовы) == 2


def test_приоритет_ниже_ручной_загрузки(tmp_path: Path):
    """Поток звонков не должен заставлять человека ждать своей загрузки."""
    db, очередь, имп = стенд(tmp_path, строка_cdr("m.1", "79161234567", "101"))
    wav(tmp_path / "monitor" / "m.1.wav")
    имп.scan()
    assert очередь.вызовы[0]["priority"] == 40


def test_выключенная_телефония_не_ходит_за_звонками(tmp_path: Path):
    db, очередь, имп = стенд(tmp_path, строка_cdr("n.1", "79161234567", "101"),
                             telephony_enabled=False)
    assert имп.enabled is False
    имп.start()
    time.sleep(0.2)
    имп.stop(timeout=2.0)
    assert очередь.вызовы == [], "фоновый поток не должен работать при выключенной настройке"


# ---------------------------------------------------------------------------
# Хранение
# ---------------------------------------------------------------------------

def test_пометка_не_отвязывает_звонок_от_задания(tmp_path: Path):
    """Второй вызов save_call без job_id не должен стирать связь и владельца."""
    db = Database(tmp_path / "asrhub.db")
    db.save_call("o.1", job_id="job_9", owner="sales", src="79161234567", dst="101")
    db.save_call("o.1", skipped="")
    звонок = db.call_for_job("job_9")
    assert звонок is not None
    assert звонок["owner"] == "sales"
    assert звонок["src"] == "79161234567"


def test_разрез_по_владельцу_в_журнале_и_счётчиках(tmp_path: Path):
    db = Database(tmp_path / "asrhub.db")
    db.save_call("p.1", owner="sales", src="79161234567", dst="101",
                 direction="входящий", billsec=100, started_at=time.time())
    db.save_call("p.2", owner="support", src="79990001122", dst="201",
                 direction="входящий", billsec=200, started_at=time.time())
    assert db.list_calls(owner="sales")["total"] == 1
    assert db.call_counts(owner="sales")["talk_s"] == 100
    assert db.call_counts(owner="support")["talk_s"] == 200
    assert db.call_counts()["talk_s"] == 300
    assert db.call_dimensions(owner="sales")["agents"] in ([], ["101"])


def test_поиск_по_номеру_не_ломается_на_подчёркивании(tmp_path: Path):
    """Подчёркивание — шаблон LIKE; без экранирования оно нашло бы лишнее."""
    db = Database(tmp_path / "asrhub.db")
    db.save_call("q.1", src="7916_1234", dst="101", started_at=time.time())
    db.save_call("q.2", src="7916X1234", dst="102", started_at=time.time())
    assert db.list_calls(search="7916_1234")["total"] == 1


def test_старые_звонки_убираются_вместе_с_заданиями(tmp_path: Path):
    """Номер клиента не должен пережить удаление самого разговора."""
    db = Database(tmp_path / "asrhub.db")
    db.save_call("u.old", src="79161234567", dst="101", started_at=time.time())
    db.execute("UPDATE calls SET imported_at=? WHERE uniqueid=?",
               (time.time() - 400 * 86400, "u.old"))
    db.save_call("u.new", src="79990001122", dst="102", started_at=time.time())
    убрано = db.cleanup(results_days=30)
    assert убрано["calls"] == 1
    assert not db.call_exists("u.old")
    assert db.call_exists("u.new")


def test_бессрочное_хранение_не_трогает_звонки(tmp_path: Path):
    """Ноль означает «хранить всегда» — и для звонков тоже."""
    db = Database(tmp_path / "asrhub.db")
    db.save_call("u.old", src="79161234567", dst="101", started_at=time.time())
    db.execute("UPDATE calls SET imported_at=? WHERE uniqueid=?",
               (time.time() - 4000 * 86400, "u.old"))
    assert db.cleanup(results_days=0)["calls"] == 0
    assert db.call_exists("u.old")


def test_звонок_без_идентификатора_не_сохраняется(tmp_path: Path):
    db = Database(tmp_path / "asrhub.db")
    with pytest.raises(StorageError):
        db.save_call("", src="79161234567")


def test_ненайденный_журнал_это_настройка_а_не_сбой_связи(tmp_path: Path):
    """Код и подсказка должны указывать на то, что чинить.

    Раньше отсутствующий файл приходил как AMIError: код 502 «станция не
    отвечает» и подсказка «проверьте manager.conf» — при источнике «журнал
    CDR», где учётной записи AMI нет вовсе и чинить по этой подсказке
    нечего.
    """
    db = Database(tmp_path / "asrhub.db")
    имп = Импортёр(db, настройки(tmp_path, telephony_cdr_file=str(tmp_path / "нет.csv")),
                   Очередь())
    with pytest.raises(ConfigError) as ошибка:
        имп.scan()
    assert ошибка.value.http_status == 400
    assert "manager.conf" not in (ошибка.value.hint or "")
    assert "чтение" in (ошибка.value.hint or "")


def test_недоступная_станция_остаётся_сбоем_связи():
    """А вот это как раз AMIError: код 502 и подсказка про manager.conf."""
    сокет = socket.socket()
    сокет.bind(("127.0.0.1", 0))
    порт = сокет.getsockname()[1]
    сокет.close()
    with pytest.raises(AMIError) as ошибка:
        проверить("127.0.0.1", порт, "asrhub", "секрет", timeout=2.0)
    assert ошибка.value.http_status == 502


# ---------------------------------------------------------------------------
# AMI
# ---------------------------------------------------------------------------

class ПоддельнаяАТС:
    """Крошечный сервер, говорящий на языке AMI: вход, Ping, CoreSettings, Cdr."""

    def __init__(self, *, пускать: bool = True, события: list[dict] | None = None):
        self.пускать = пускать
        self.события = события or []
        self.принято: list[str] = []
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.поток = threading.Thread(target=self._работать, daemon=True)
        self.поток.start()

    def _работать(self) -> None:
        try:
            клиент, _ = self.sock.accept()
        except OSError:
            return
        with клиент:
            клиент.sendall(b"Asterisk Call Manager/7.0.3\r\n")
            буфер = b""
            while True:
                try:
                    кусок = клиент.recv(4096)
                except OSError:
                    return
                if not кусок:
                    return
                буфер += кусок
                while b"\r\n\r\n" in буфер:
                    пакет, _, буфер = буфер.partition(b"\r\n\r\n")
                    поля = {с.split(":", 1)[0].strip(): с.split(":", 1)[1].strip()
                            for с in пакет.decode().splitlines() if ":" in с}
                    действие = поля.get("Action", "").lower()
                    self.принято.append(действие)
                    if действие == "login":
                        if not self.пускать:
                            клиент.sendall(b"Response: Error\r\n"
                                           b"Message: Authentication failed\r\n\r\n")
                            return
                        клиент.sendall(b"Response: Success\r\n"
                                       b"Message: Authentication accepted\r\n\r\n")
                        for событие in self.события:
                            тело = "".join(f"{к}: {з}\r\n" for к, з in событие.items())
                            клиент.sendall(тело.encode() + b"\r\n")
                    elif действие == "ping":
                        клиент.sendall(b"Response: Success\r\nPing: Pong\r\n\r\n")
                    elif действие == "coresettings":
                        клиент.sendall(b"Response: Success\r\n"
                                       b"AsteriskVersion: 20.5.0\r\n\r\n")
                    elif действие == "logoff":
                        return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def test_проверка_связи_возвращает_версию_атс():
    атс = ПоддельнаяАТС()
    try:
        итог = проверить("127.0.0.1", атс.port, "asrhub", "секрет", timeout=5.0)
    finally:
        атс.close()
    assert итог["ok"] is True
    assert итог["version"] == "20.5.0"
    assert "Asterisk Call Manager" in итог["banner"]


def test_отказ_входа_объясняется_а_не_молчит():
    атс = ПоддельнаяАТС(пускать=False)
    try:
        with pytest.raises(AMIError) as ошибка:
            проверить("127.0.0.1", атс.port, "asrhub", "не тот", timeout=5.0)
    finally:
        атс.close()
    assert "manager.conf" in (ошибка.value.hint or "")


def test_клиент_не_умеет_командовать_станцией():
    """Скомпрометированный сервер распознавания не должен уметь звонить."""
    атс = ПоддельнаяАТС()
    try:
        with AMIClient("127.0.0.1", атс.port, "asrhub", "секрет", timeout=5.0) as клиент:
            for опасное in ("Originate", "Redirect", "Command", "Hangup", "DBPut"):
                with pytest.raises(AMIError):
                    клиент.action(опасное, Channel="SIP/101")
    finally:
        атс.close()
    assert "originate" not in атс.принято


def test_недоступная_атс_сообщает_адрес():
    сокет = socket.socket()
    сокет.bind(("127.0.0.1", 0))
    порт = сокет.getsockname()[1]
    сокет.close()
    with pytest.raises(AMIError) as ошибка:
        проверить("127.0.0.1", порт, "asrhub", "секрет", timeout=2.0)
    assert str(порт) in str(ошибка.value)


def test_события_cdr_превращаются_в_звонки(tmp_path: Path):
    событие = {
        "Event": "Cdr", "AccountCode": "", "Source": "79161234567",
        "Destination": "101", "DestinationContext": "from-trunk",
        "CallerID": '"Клиент" <79161234567>', "Channel": "SIP/tr-01",
        "DestinationChannel": "SIP/101", "LastApplication": "Queue",
        "LastData": "sales,t",
        "StartTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(СЕЙЧАС)),
        "AnswerTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(СЕЙЧАС)),
        "EndTime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(СЕЙЧАС + 300)),
        "Duration": "300", "BillableSeconds": "290", "Disposition": "ANSWERED",
        "AMAFlags": "DOCUMENTATION", "UniqueID": "r.1", "UserField": "",
    }
    атс = ПоддельнаяАТС(события=[событие])
    (tmp_path / "monitor").mkdir()
    wav(tmp_path / "monitor" / "r.1.wav")
    db = Database(tmp_path / "asrhub.db")
    очередь = Очередь()
    имп = Импортёр(db, настройки(tmp_path, telephony_source="ami",
                                 telephony_host="127.0.0.1",
                                 telephony_port=атс.port,
                                 telephony_username="asrhub",
                                 telephony_secret="секрет"), очередь)
    try:
        итог = имп.scan(limit=5)
    finally:
        имп.stop(timeout=2.0)
        атс.close()
    assert итог["imported"] == 1
    assert db.call_exists("r.1")
    assert очередь.вызовы[0]["source"] == "asterisk"


# ---------------------------------------------------------------------------
# Каталог настроек
# ---------------------------------------------------------------------------

def test_пароль_атс_считается_секретом():
    from asrhub.config import Settings
    assert "telephony_secret" in Settings.SECRET_KEYS


def test_все_параметры_телефонии_описаны():
    from asrhub.catalog.params import GROUPS_BY_ID, PARAMS
    assert "telephony" in GROUPS_BY_ID
    свои = [п for п in PARAMS if п.group == "telephony"]
    assert len(свои) >= 15
    for параметр in свои:
        assert параметр.description, параметр.key
        assert параметр.recommendation, параметр.key
        assert параметр.examples, параметр.key
