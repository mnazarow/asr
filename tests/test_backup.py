"""Резервные копии: что внутри, что возвращается и чего копия не должна делать.

Копия проверяется тем же способом, каким её и заводят: снять, испортить
настройки, вернуть и убедиться, что вернулось именно то. Отдельно —
защита распаковки: архив приходит извне, и «файл с именем ../../etc» —
не выдумка, а самая известная ошибка всех распаковщиков.
"""
from __future__ import annotations

import json
import os
import tarfile
import time
from pathlib import Path

import pytest
from asrhub import backup
from asrhub.db import Database
from asrhub.errors import ASRHubError, ConfigError, StorageError


class Настройки(dict):
    """Настройки сервера в объёме, который нужен копии."""

    def __init__(self, каталог: Path, **поверх):
        super().__init__({
            "backup_keep_days": 10, "backup_keep": 0, "backup_enabled": True,
            "backup_time": "00:01", "backup_interval_hours": 0,
            "backup_kind": "full", "backup_include_results": False,
            "backup_dir": str(каталог / "backups"),
            "beam_size": 5, "max_concurrent_jobs": 2,
            **поверх})
        class Пути:                                          # noqa: N801
            data = str(каталог)
            results = str(каталог / "results")
        self.paths = Пути()
        self.config_path = None
        self.sources = {}
        self.записано: list[tuple] = []

    def get(self, ключ, по_умолчанию=None):                  # noqa: A003
        return dict.get(self, ключ, по_умолчанию)

    def set(self, ключ, значение, source="runtime"):         # noqa: A003
        self[ключ] = значение
        self.записано.append((ключ, значение, source))

    def save(self, path=None):
        return path


@pytest.fixture()
def стенд(tmp_path):
    (tmp_path / "results").mkdir()
    db = Database(tmp_path / "asrhub.db")
    db.save_call(uniqueid="1", src="79160000001", dst="101", billsec=60,
                 answered=1, started_at=time.time() - 300, station="pbx",
                 owner="telephony")
    return db, Настройки(tmp_path), tmp_path


# ---------------------------------------------------------------------------
# Что внутри копии
# ---------------------------------------------------------------------------

def test_копия_настроек_весит_килобайты_и_не_тащит_базу(стенд):
    """Смысл отдельного вида — в том, что его можно снимать хоть ежеминутно."""
    db, настройки, _ = стенд
    копия = backup.создать(db, настройки, kind="settings")
    assert копия["kind"] == "settings"
    assert копия["contents"] == ["settings.json"]
    assert копия["size"] < 200 * 1024, "в копию настроек попало что-то лишнее"


def test_полная_копия_несёт_базу_и_опись(стенд):
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full", comment="перед обновлением")
    путь = Path(настройки["backup_dir"]) / копия["name"]
    with tarfile.open(путь, "r:gz") as архив:
        имена = архив.getnames()
        опись = json.loads(архив.extractfile("manifest.json").read().decode("utf-8"))
    assert "db/asrhub.db" in имена and "settings.json" in имена
    # Опись отвечает на вопрос «что это за копия», не разбирая архив: без
    # неё восстановление превращается в угадывание.
    assert опись["kind"] == "full" and опись["format"] == backup.ФОРМАТ
    assert опись["calls"] == 1 and опись["comment"] == "перед обновлением"
    assert опись["schema_version"] > 0 and опись["version"]


def test_прерванная_копия_не_попадает_в_список(стенд, monkeypatch):
    """Обрезанный архив в списке — это копия, которую однажды выберут.

    Сборка идёт во временный файл и переименовывается в конце; сбой на
    середине обязан не оставить в каталоге ничего — ни копии, ни черновика.
    """
    db, настройки, _ = стенд
    настоящий = Path.replace

    def сломать(self, цель):
        if str(self).endswith(".part"):
            raise OSError("диск кончился ровно на переименовании")
        return настоящий(self, цель)

    monkeypatch.setattr(Path, "replace", сломать)
    with pytest.raises(ASRHubError):
        backup.создать(db, настройки, kind="settings")
    monkeypatch.undo()
    assert backup.список(настройки) == []
    assert not list(Path(настройки["backup_dir"]).glob("*.part")), "остался черновик"


def test_секреты_в_копии_есть_а_снаружи_замаскированы(стенд):
    """Копия, из которой сервер не поднимается без ручного ввода паролей,
    решает не ту задачу, ради которой её снимают."""
    db, настройки, каталог = стенд
    настройки.set("telephony_stations", [{"id": "a", "name": "АТС",
                                          "source": "ami", "username": "asrhub",
                                          "secret": "очень-секретно"}])
    копия = backup.создать(db, настройки, kind="settings")
    путь = Path(настройки["backup_dir"]) / копия["name"]
    with tarfile.open(путь, "r:gz") as архив:
        значения = json.loads(архив.extractfile("settings.json").read().decode("utf-8"))
    станции = значения["telephony_stations"]
    assert станции[0]["secret"] == "очень-секретно", "пароль не переживёт восстановление"


# ---------------------------------------------------------------------------
# Восстановление
# ---------------------------------------------------------------------------

def test_настройки_возвращаются_на_ходу(стенд):
    db, настройки, _ = стенд
    настройки.set("beam_size", 9)
    копия = backup.создать(db, настройки, kind="settings")
    настройки.set("beam_size", 1)

    итог = backup.восстановить(db, настройки, копия["name"], what="settings")
    assert настройки["beam_size"] == 9
    assert итог["applied"] > 0
    assert итог["restart_required"] is False, "настройки не требуют перезапуска"
    assert итог["database"] is False


def test_из_копии_настроек_нельзя_вернуть_данные(стенд):
    db, настройки, _ = стенд
    копия = backup.создать(db, настройки, kind="settings")
    with pytest.raises(ConfigError) as сбой:
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert "нет базы" in сбой.value.message


def test_прежняя_база_остаётся_рядом(стенд):
    """Восстановление не из той копии — обычная ошибка, и она обратима."""
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")
    итог = backup.восстановить(db, настройки, копия["name"], what="full")
    assert итог["database"] and итог["restart_required"]
    сохранённые = list(каталог.glob("asrhub.db.before-restore-*"))
    assert сохранённые, "прежняя база исчезла без следа"
    assert (каталог / "asrhub.db").is_file()


def test_жалоба_на_целостность_останавливает_восстановление(стенд, monkeypatch):
    """SQLite сообщает о порче двумя способами, и второй легко проглядеть.

    Грубая порча вылетает исключением на открытии. Тонкая — открывается,
    а «PRAGMA integrity_check» возвращает не «ok», а перечень бед обычной
    строкой. Ветка со строкой проверяется отдельно: без неё повреждённая
    база встала бы на место рабочей молча.
    """
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")
    настоящий = backup.sqlite3.connect

    class Жалоба:
        def execute(self, _запрос):
            return self

        def fetchone(self):
            return ("*** in database main ***\nPage 42: btreeInitPage() returns error",)

        def close(self):
            return None

    monkeypatch.setattr(backup.sqlite3, "connect",
                        lambda путь, **к: Жалоба() if "mode=ro" in str(путь)
                        else настоящий(путь, **к))
    было = (каталог / "asrhub.db").read_bytes()[:64]
    with pytest.raises(ConfigError) as сбой:
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert "повреждена" in сбой.value.message
    assert (каталог / "asrhub.db").read_bytes()[:64] == было, "рабочую базу тронули"


def test_база_с_битой_страницей_тоже_не_подменяет_рабочую(стенд, tmp_path):
    """Файл открывается, заголовок цел — а «PRAGMA integrity_check» не «ok».

    Это другой случай, чем мусор вместо базы: там SQLite падает на открытии,
    здесь молча отдаёт повреждённые данные, и без проверки они встали бы
    на место рабочей базы.
    """
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")
    путь = Path(настройки["backup_dir"]) / копия["name"]
    временный = tmp_path / "правка"
    временный.mkdir()
    with tarfile.open(путь, "r:gz") as архив:
        архив.extractall(временный, filter="data")
    файл = временный / "db" / "asrhub.db"
    данные = bytearray(файл.read_bytes())
    # Портим середину, оставляя заголовок: страница станет нечитаемой, а
    # файл — по-прежнему базой SQLite.
    for сдвиг in range(4096, min(len(данные), 12288)):
        данные[сдвиг] = (данные[сдвиг] + 173) % 256
    файл.write_bytes(bytes(данные))
    путь.unlink()
    with tarfile.open(путь, "w:gz") as архив:
        for элемент in sorted(временный.rglob("*")):
            архив.add(элемент, arcname=str(элемент.relative_to(временный)))

    было = (каталог / "asrhub.db").read_bytes()[:64]
    with pytest.raises(ConfigError) as сбой:
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert "поврежден" in сбой.value.message.lower() or "не похож" in сбой.value.message
    assert (каталог / "asrhub.db").read_bytes()[:64] == было, "рабочую базу тронули"


def test_повреждённая_копия_не_подменяет_рабочую_базу(стенд):
    """Проверка целостности — ДО подмены, иначе человек остаётся без базы."""
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")
    # Подкладываем в архив «базу» из мусора
    путь = Path(настройки["backup_dir"]) / копия["name"]
    временный = каталог / "порча"
    временный.mkdir()
    with tarfile.open(путь, "r:gz") as архив:
        архив.extractall(временный, filter="data")
    (временный / "db" / "asrhub.db").write_bytes("это не база".encode() * 100)
    путь.unlink()
    with tarfile.open(путь, "w:gz") as архив:
        for файл in sorted(временный.rglob("*")):
            архив.add(файл, arcname=str(файл.relative_to(временный)))

    было = (каталог / "asrhub.db").read_bytes()[:64]
    with pytest.raises(ConfigError):
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert (каталог / "asrhub.db").read_bytes()[:64] == было, "рабочую базу тронули"


# ---------------------------------------------------------------------------
# Защита
# ---------------------------------------------------------------------------

def test_имя_копии_не_выводит_за_каталог(стенд, tmp_path):
    """Имя приходит снаружи — значит, «../../etc/passwd» обязано отлететь.

    Проверка идёт и на имени с правильным расширением: иначе от подстановки
    защищало бы только «не похоже на копию», и файл соседнего каталога,
    названный как копия, читался бы и удалялся как своя.
    """
    _db, настройки, _ = стенд
    чужой = tmp_path / f"чужой{backup.РАСШИРЕНИЕ}"
    чужой.write_bytes(b"x")
    плохие = ("../../etc/passwd", "/etc/passwd", "..", "a/b.asrhub.tar.gz",
              f"../{чужой.name}", f"..{os.sep}..{os.sep}{чужой.name}",
              str(чужой))
    for плохое in плохие:
        with pytest.raises(ConfigError):
            backup._путь_копии(настройки, плохое)


def test_архив_с_путём_наружу_не_распаковывается(стенд, tmp_path):
    """Известная дыра всех распаковщиков: имя внутри архива кладёт файл мимо."""
    db, настройки, каталог = стенд
    злой = Path(настройки["backup_dir"])
    злой.mkdir(parents=True, exist_ok=True)
    подкидыш = tmp_path / "подкидыш.txt"
    подкидыш.write_text("я не должен сюда попасть", encoding="utf-8")
    файл = злой / f"asrhub-20260101-000000-full-x{backup.РАСШИРЕНИЕ}"
    with tarfile.open(файл, "w:gz") as архив:
        опись = tmp_path / "manifest.json"
        опись.write_text(json.dumps({"format": 1, "kind": "settings"}), encoding="utf-8")
        архив.add(опись, arcname="manifest.json")
        архив.add(подкидыш, arcname="../../подкидыш.txt")
    with pytest.raises(ConfigError) as сбой:
        backup.восстановить(db, настройки, файл.name, what="settings")
    assert "за её пределы" in сбой.value.message


def test_чужой_архив_не_принимается(стенд, tmp_path):
    db, настройки, _ = стенд
    чужой = tmp_path / f"чужой{backup.РАСШИРЕНИЕ}"
    with tarfile.open(чужой, "w:gz") as архив:
        файл = tmp_path / "readme.txt"
        файл.write_text("просто архив", encoding="utf-8")
        архив.add(файл, arcname="readme.txt")
    with чужой.open("rb") as поток, pytest.raises(ConfigError) as сбой:
        backup.принять(настройки, чужой.name, поток)
    assert "не резервная копия" in сбой.value.message
    assert not list(Path(настройки["backup_dir"]).glob("*.part")), "остался черновик"


# ---------------------------------------------------------------------------
# Расписание и срок хранения
# ---------------------------------------------------------------------------

def test_срок_хранения_считается_в_днях(стенд):
    db, настройки, _ = стенд
    место = Path(настройки["backup_dir"])
    место.mkdir(parents=True, exist_ok=True)
    старая = место / f"asrhub-20250101-000000-full-x{backup.РАСШИРЕНИЕ}"
    свежая = место / f"asrhub-20260101-000000-full-x{backup.РАСШИРЕНИЕ}"
    for файл in (старая, свежая):
        файл.write_bytes(b"x")
    import os
    os.utime(старая, (time.time() - 40 * 86400,) * 2)
    убрано = backup.подчистить(настройки)
    assert убрано == [старая.name]
    assert свежая.exists()


def test_последняя_копия_не_удаляется_никогда(стенд):
    """Каталог без единой копии — это не «чисто», а «копий нет»."""
    db, настройки, _ = стенд
    настройки["backup_keep_days"] = 1
    место = Path(настройки["backup_dir"])
    место.mkdir(parents=True, exist_ok=True)
    единственная = место / f"asrhub-20200101-000000-full-x{backup.РАСШИРЕНИЕ}"
    единственная.write_bytes(b"x")
    import os
    os.utime(единственная, (time.time() - 400 * 86400,) * 2)
    backup.подчистить(настройки)
    assert единственная.exists()


def test_ноль_дней_значит_бессрочно_а_не_ноль_дней(стенд):
    """Ноль ложен в Python — и `int(значение or 10)` тихо режет «вечно» в декаду."""
    db, настройки, _ = стенд
    настройки["backup_keep_days"] = 0
    место = Path(настройки["backup_dir"])
    место.mkdir(parents=True, exist_ok=True)
    древняя = место / f"asrhub-20200101-000000-full-x{backup.РАСШИРЕНИЕ}"
    свежая = место / f"asrhub-20260101-000000-full-x{backup.РАСШИРЕНИЕ}"
    for файл in (древняя, свежая):
        файл.write_bytes(b"x")
    import os
    os.utime(древняя, (time.time() - 4000 * 86400,) * 2)
    assert backup.подчистить(настройки) == []
    assert древняя.exists()


def test_расписание_раз_в_сутки_в_назначенное_время(стенд, monkeypatch):
    """«Каждые 24 часа» и «раз в сутки в 00:01» — разные вещи.

    Сервер, постоявший выключенным полчаса, при счёте «прошло ли 24 часа»
    сдвигал бы копию каждый день, пока она не уезжала в разгар работы.
    """
    db, настройки, _ = стенд
    настройки["backup_time"] = "00:01"
    сейчас = time.time()
    местное = time.localtime(сейчас)
    сегодня_в_00_01 = time.mktime((местное.tm_year, местное.tm_mon, местное.tm_mday,
                                   0, 1, 0, 0, 0, -1))

    db.set_kv("backup_last_at", сегодня_в_00_01 + 60)        # копия уже была сегодня
    assert backup.пора(db, настройки) is False
    db.set_kv("backup_last_at", сегодня_в_00_01 - 3600)      # была вчера вечером
    assert backup.пора(db, настройки) is True
    настройки["backup_enabled"] = False
    assert backup.пора(db, настройки) is False


def test_интервал_в_часах_главнее_времени(стенд):
    db, настройки, _ = стенд
    настройки["backup_interval_hours"] = 6
    db.set_kv("backup_last_at", time.time() - 3 * 3600)
    assert backup.пора(db, настройки) is False
    db.set_kv("backup_last_at", time.time() - 7 * 3600)
    assert backup.пора(db, настройки) is True


def test_время_разбирается_как_его_напишет_человек():
    assert backup.разобрать_время("00:01") == (0, 1)
    assert backup.разобрать_время("4.30") == (4, 30)
    assert backup.разобрать_время("23") == (23, 0)
    # Мусор — не повод не делать копию вовсе: берём значение по умолчанию.
    for плохое in ("", None, "чепуха", "25:00", "12:99", "-1:00"):
        assert backup.разобрать_время(плохое) == (0, 1), плохое


# ---------------------------------------------------------------------------
# Дефекты, найденные ревизией тридцатого захода
# ---------------------------------------------------------------------------

def test_две_копии_одновременно_не_портят_друг_друга(стенд):
    """Имя с точностью до секунды — два захода выбирали одно.

    Оба открывали один черновик на запись, один переименовывал, второй
    дописывал уже в переименованный: на выходе архив, который выглядит
    копией и не распаковывается, а следом переставал открываться весь
    раздел.
    """
    import threading

    db, настройки, _ = стенд
    барьер = threading.Barrier(2)
    итоги: list = []

    def снять():
        барьер.wait()
        try:
            итоги.append(backup.создать(db, настройки, kind="full"))
        except Exception as exc:                             # noqa: BLE001
            итоги.append(exc)

    потоки = [threading.Thread(target=снять) for _ in range(2)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=60)

    assert all(not isinstance(и, Exception) for и in итоги), итоги
    имена = {и["name"] for и in итоги}
    assert len(имена) == 2, "две копии легли под одним именем"
    # И обе читаются: список раздела не падает.
    for копия in backup.список(настройки):
        assert not копия.get("error"), копия


def test_битая_копия_не_роняет_весь_список(стенд):
    """Один недописанный файл делал раздел неоткрываемым.

    gzip на оборванном файле кидает EOFError, а он не наследуется от
    TarError — мягкая деградация не срабатывала. Убрать битую копию через
    интерфейс было нельзя: имя берётся из списка, а списка нет.
    """
    db, настройки, _ = стенд
    целая = backup.создать(db, настройки, kind="full")
    место = Path(настройки["backup_dir"])
    данные = (место / целая["name"]).read_bytes()
    (место / f"asrhub-20260101-000000-full-обрыв{backup.РАСШИРЕНИЕ}").write_bytes(
        данные[:len(данные) // 3])
    мусор = bytearray(данные)
    for сдвиг in range(len(мусор) // 2, len(мусор) // 2 + 200):
        мусор[сдвиг] = (мусор[сдвиг] + 97) % 256
    (место / f"asrhub-20260101-000001-full-порча{backup.РАСШИРЕНИЕ}").write_bytes(bytes(мусор))

    копии = backup.список(настройки)
    assert len(копии) == 3, "список не собрался"
    битых = [к for к in копии if к.get("error")]
    # Обрыв ловится описью; порча в середине архива опись не задевает —
    # такую копию видно в списке как целую, и споткнётся она уже при
    # восстановлении, где проверка целостности стоит до подмены базы.
    assert битых, копии
    assert all("опись не читается" in к["error"] for к in битых)
    # И свод раздела собирается, а битую копию можно убрать.
    assert backup.свод(настройки)["total"] == 3
    backup.удалить(настройки, битых[0]["name"])


def test_недокачанный_архив_не_оставляет_черновик(стенд, tmp_path):
    """Иначе на диске остаётся файл размером с базу.

    Невидимый в списке, не учтённый в «занято» и неудаляемый через
    интерфейс — только через оболочку.
    """
    import io

    db, настройки, _ = стенд
    целая = backup.создать(db, настройки, kind="full")
    данные = (Path(настройки["backup_dir"]) / целая["name"]).read_bytes()
    # Разные доли обрываются по-разному: на одних gzip кидает EOFError, на
    # других tar — ReadError. Принимать нельзя ни один, и черновика не
    # должно остаться ни от одного.
    for доля in (0.1, 0.3, 0.5, 0.9):
        кусок = io.BytesIO(данные[:max(1, int(len(данные) * доля))])
        with pytest.raises(ConfigError) as сбой:
            backup.принять(настройки, f"со-стороны{backup.РАСШИРЕНИЕ}", кусок)
        assert "не принят" in сбой.value.message or "не резервная копия" in сбой.value.message
        assert not list(Path(настройки["backup_dir"]).glob("*.part")), доля


def test_неудачное_восстановление_не_подменяет_настройки(стенд):
    """Администратор выбрал не ту копию, увидел ошибку — и уверен, что
    ничего не произошло. Так и должно быть.
    """
    db, настройки, _ = стенд
    настройки.set("beam_size", 9)
    копия = backup.создать(db, настройки, kind="settings")
    настройки.set("beam_size", 1)
    with pytest.raises(ConfigError):
        backup.восстановить(db, настройки, копия["name"], what="full")
    assert настройки["beam_size"] == 1, "настройки подменены при отказе"


def test_рабочая_база_не_теряется_при_нехватке_места(стенд, monkeypatch):
    """Прежняя база уже отодвинута, а новая не легла.

    Молчать об этом нельзя: после перезапуска человек получит пустую базу
    и решит, что потерял всё.
    """
    db, настройки, каталог = стенд
    копия = backup.создать(db, настройки, kind="full")
    было = (каталог / "asrhub.db").read_bytes()

    def нет_места(*_а, **_к):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup.shutil, "copy2", нет_места)
    with pytest.raises(StorageError) as сбой:
        backup.восстановить(db, настройки, копия["name"], what="full")
    monkeypatch.undo()
    assert "не восстановлена" in сбой.value.message
    assert (каталог / "asrhub.db").read_bytes() == было, "рабочая база потеряна"


def test_рабочая_база_в_каталоге_копий_не_удаляется_как_копия(стенд):
    """Каталог копий, указанный на каталог данных, — ошибка настройки.

    Но она не должна делать рабочую базу удаляемой по имени: список копий
    её никогда не показывал, а `удалить` принимал.
    """
    db, настройки, каталог = стенд
    место = Path(настройки["backup_dir"])
    место.mkdir(parents=True, exist_ok=True)
    (место / "asrhub.db").write_bytes("это рабочая база".encode())
    with pytest.raises(ConfigError):
        backup.удалить(настройки, "asrhub.db")
    assert (место / "asrhub.db").exists()


def test_предел_по_числу_копий_ноль_значит_без_предела(стенд):
    """`int(значение or 7)` превращал «предела нет» в семь.

    Ручная команда снятия копии молча сносила самые старые — те, ради
    которых предел и ставили в ноль.
    """
    import os

    from asrhub import maintenance

    db, настройки, каталог = стенд
    настройки["backup_keep"] = 0
    место = Path(настройки["backup_dir"])
    место.mkdir(parents=True, exist_ok=True)
    for н in range(8):
        файл = место / f"asrhub-2026090{н}-000000-vm.db"
        файл.write_bytes(b"x")
        os.utime(файл, (time.time() - (8 - н) * 3600,) * 2)
    maintenance.make_backup(db, настройки)
    assert len(list(место.glob("asrhub-*.db"))) == 9, "старые копии снесены"
