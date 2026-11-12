"""Резервные копии: настройки отдельно, настройки вместе с данными — вместе.

Две разные копии отвечают на два разных вопроса.

«Только настройки» — это несколько килобайт, которые снимаются за
мгновение и переносятся куда угодно: на запасной сервер, в систему учёта
изменений, в письмо коллеге. В них вся работа, вложенная в подбор моделей,
порогов, словарей и станций АТС, — то, что восстановить по памяти
невозможно, а потерять при переустановке проще всего.

«Настройки и данные» — это ещё и база: задания, результаты, звонки,
показатели. Она весит столько же, сколько весит база, и нужна там, где
теряется не настройка, а история.

Копия — архив с описью внутри. Опись (`manifest.json`) отвечает, откуда
копия, какой в ней вид, какая версия схемы базы и что лежит внутри: без
неё восстановление превращается в угадывание, а восстановление не из той
копии — самая дорогая ошибка из возможных.

База копируется командой SQLite «.backup», а не `cp`: в режиме WAL рядом с
базой лежат `-wal` и `-shm`, и обычная копия получается несогласованной —
то есть выглядит как копия и ею не является.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .errors import ConfigError, StorageError
from .instance import INSTANCE_ID

log = logging.getLogger("asrhub.backup")

#: Замок на снятие копии. Два запроса в одну секунду выбирали одно имя
#: черновика и писали в него каждый со своего дескриптора: на выходе
#: получался архив, который выглядит копией и не распаковывается, а
#: следом переставал открываться весь раздел.
_ЗАМОК = threading.Lock()

#: Виды копий и что каждая в себя включает.
ВИДЫ = {
    "settings": "только настройки",
    "full": "настройки и данные",
}

#: Имя описи внутри архива и версия её формата. Версия нужна, чтобы копия,
#: снятая будущей версией сервера, отказывалась восстанавливаться явно, а
#: не разбиралась наполовину.
ОПИСЬ = "manifest.json"
ФОРМАТ = 1

#: Расширение архива. Один файл переносится и хранится проще каталога, а
#: gzip на настройках даёт десятикратное сжатие почти бесплатно.
РАСШИРЕНИЕ = ".asrhub.tar.gz"

#: Предел на распаковку. Архив резервной копии приходит извне ровно так же,
#: как любой другой файл, и «архивная бомба» — сжатые нули, разворачивающиеся
#: в терабайт — забивает диск сервера до отказа за секунды.
ПРЕДЕЛ_РАСПАКОВКИ = 64 * 1024 * 1024 * 1024

#: Уровень сжатия архива. Девятый (умолчание `tarfile`) на базе в гигабайты
#: стоил вдвое больше процессорного времени, чем шестой, а выигрывал
#: проценты: расшифровки сжимаются хорошо на любом уровне, а звук и веса в
#: копию не входят.
УРОВЕНЬ_СЖАТИЯ = 6

#: Спутники файла базы SQLite. В архив они не кладутся: снимок проверяется
#: открытием «только чтение», и рядом с ним оставались пустые `-wal` и
#: `-shm` — лишние записи в копии, которые при распаковке ложились рядом с
#: базой и выдавали себя за её журнал.
СПУТНИКИ_БАЗЫ = ("-wal", "-shm", "-journal")

#: Сколько ждёт подготовленная к восстановлению база, прежде чем запуск
#: перестанет ставить её молча. «Вернуть данные» без перезапуска — обычное
#: дело (перезапуск отложили до вечера), но через две недели плановый
#: перезапуск подменял базу двухнедельной копией, а файлы новых заданий
#: уходили в карантин. Старше срока копия не ставится, пока её не
#: подтвердят (раздел «Копии», `POST /api/backup/pending/confirm`) или не
#: восстановят заново.
СРОК_ОЖИДАНИЯ_С = 72 * 3600.0


def _uri_только_чтение(путь: Path | str) -> str:
    """Адрес файла базы для SQLite в режиме «только чтение».

    Путь подставлялся в URI как есть: `f"file:{путь}?mode=ro"`. В URI знаки
    «#» и «?» обрывают путь, а «%XX» раскодируется: каталог копий
    «backup #2» давал проверку снимка по новой пустой базе «backup » (и
    ответ «ok»), а каталог с «%25» — отказ «Рабочая база повреждена». Путь,
    закодированный как положено (`as_uri`), SQLite раскодирует обратно в тот
    же самый.
    """
    return Path(путь).resolve().as_uri() + "?mode=ro"


@dataclass
class Копия:
    """Одна резервная копия — то, что о ней известно, не разбирая архив."""

    name: str = ""
    kind: str = "full"
    created_at: float = 0.0
    size: int = 0
    version: str = ""
    schema_version: int = 0
    instance: str = ""
    comment: str = ""
    contents: list[str] = field(default_factory=list)
    calls: int = 0
    jobs: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        данные = {
            "name": self.name, "kind": self.kind, "kind_title": ВИДЫ.get(self.kind, self.kind),
            "created_at": self.created_at, "size": self.size, "version": self.version,
            "schema_version": self.schema_version, "instance": self.instance,
            "comment": self.comment, "contents": self.contents,
            "calls": self.calls, "jobs": self.jobs,
        }
        if self.error:
            данные["error"] = self.error
        return данные


# ---------------------------------------------------------------------------
# Где лежат копии
# ---------------------------------------------------------------------------

def каталог(settings: Any) -> Path:
    """Куда складывать копии: настройка или «backups» в каталоге данных."""
    задан = str(settings.get("backup_dir") or "").strip()
    путь = Path(задан) if задан else Path(settings.paths.data) / "backups"
    return путь


def _готовый_каталог(settings: Any) -> Path:
    """Каталог копий, созданный при необходимости — закрытым для чужих.

    В копии лежат все секреты сервера (ключи, токены, пароли станций) и вся
    база разговоров, а каталог создавался с правами по umask — 0755, файлы
    — 0644. Документация советует выносить копии за пределы каталога данных
    (например, в /mnt/backup), и там их читал любой пользователь машины.
    Каталог, который создаём мы, — 0700; права уже существующего не
    трогаем: их выбрал администратор. Сами копии — всегда 0600.
    """
    путь = каталог(settings)
    try:
        новый = not путь.exists()
        путь.mkdir(parents=True, exist_ok=True)
        if новый:
            os.chmod(путь, 0o700)
    except OSError as exc:
        raise StorageError(
            f"Каталог для резервных копий недоступен: {путь}",
            hint="Проверьте путь в настройке «Каталог для копий» и права "
                 "пользователя, от которого работает сервер.") from exc
    return путь


def _кто() -> str:
    """Короткая метка экземпляра — чтобы копии двух серверов не слипались."""
    from .phone_compat import safe_path_key  # noqa: PLC0415

    return safe_path_key(INSTANCE_ID)[:24] or "экземпляр"


def _имя(kind: str) -> str:
    return f"asrhub-{time.strftime('%Y%m%d-%H%M%S')}-{kind}-{_кто()}{РАСШИРЕНИЕ}"


def _путь_копии(settings: Any, name: str) -> Path:
    """Путь к копии по имени. Имя приходит снаружи — значит, проверяется.

    Без проверки `name` вида «../../etc/passwd» превращал бы удаление копии
    в удаление чужого файла, а скачивание — в выдачу наружу чего угодно с
    диска сервера.
    """
    короткое = os.path.basename(str(name or "").strip())
    if not короткое or короткое != str(name).strip():
        raise ConfigError(f"Недопустимое имя копии: {name!r}",
                          hint="Имя берётся из списка копий как есть.")
    # Только то, что мы сами и показываем в списке. Без приставки «asrhub-»
    # любой *.db в каталоге считался копией, а если каталог копий указан на
    # каталог данных — рабочая база становилась удаляемой по имени.
    свой = короткое.endswith(РАСШИРЕНИЕ) or (
        короткое.startswith("asrhub-") and короткое.endswith(".db"))
    if not свой:
        raise ConfigError(
            f"«{короткое}» не похоже на резервную копию.",
            hint=f"Копии называются *{РАСШИРЕНИЕ}; принимается и старая копия "
                 "базы вида asrhub-<дата>.db.")
    путь = _готовый_каталог(settings) / короткое
    if not путь.is_file():
        raise ConfigError(f"Копия «{короткое}» не найдена.",
                          hint="Список копий: GET /api/backup.")
    return путь


# ---------------------------------------------------------------------------
# Что кладём внутрь
# ---------------------------------------------------------------------------

def _настройки_наружу(settings: Any) -> dict[str, Any]:
    """Значения всех параметров каталога — то, что и есть «настройки».

    Именно значения, а не файл настроек: часть их приходит из переменных
    окружения и из подбора по железу, и файл без них — половина картины.
    Секреты входят в копию как есть: копия настроек, из которой сервер не
    поднимается без ручного ввода паролей, решает не ту задачу, ради
    которой её снимают. Поэтому архив и надо хранить как пароль.
    """
    from .catalog import PARAMS_BY_KEY  # noqa: PLC0415

    значения = {}
    for ключ in PARAMS_BY_KEY:
        значение = settings.get(ключ)
        if значение is not None:
            значения[ключ] = значение
    return значения


def _опись(db: Any, settings: Any, kind: str, comment: str,
           состав: list[str]) -> dict[str, Any]:
    from .db import SCHEMA_VERSION  # noqa: PLC0415

    звонков, заданий = 0, 0
    try:
        звонков = int((db.query_one("SELECT COUNT(*) AS n FROM calls") or {})["n"] or 0)
        заданий = int((db.query_one("SELECT COUNT(*) AS n FROM jobs") or {})["n"] or 0)
    except Exception:                                        # noqa: BLE001
        pass
    return {
        "format": ФОРМАТ, "kind": kind, "created_at": time.time(),
        "version": __version__, "schema_version": SCHEMA_VERSION,
        "instance": INSTANCE_ID, "comment": str(comment or "")[:500],
        "contents": состав, "calls": звонков, "jobs": заданий,
        "data_dir": str(settings.paths.data),
    }


def _снять_базу(db: Any, куда: Path) -> None:
    """Согласованный снимок базы через «.backup», без остановки сервера.

    Как именно копировать, решает сама база (`Database.snapshot`): в режиме
    журнала отката копия одним шагом запирала запись всем на всё время
    снимка. Подставные базы проверок такого метода не знают — им хватает
    обычного шага.
    """
    приёмник = sqlite3.connect(str(куда))
    try:
        with приёмник:
            снимок = getattr(db, "snapshot", None)
            if callable(снимок):
                снимок(приёмник)
            else:
                db.conn.backup(приёмник)
    finally:
        приёмник.close()
    _влить_спутники(куда)


def _влить_спутники(путь: Path) -> None:
    """Вливает журнал снимка в сам файл и убирает его спутников.

    Снимок наследует у рабочей базы отметку WAL, и любое открытие оставляет
    рядом `-wal` и `-shm`. В архиве им не место (см. `СПУТНИКИ_БАЗЫ`), а
    выбрасывать непустой журнал нельзя: в нём часть снимка.
    """
    журнал = Path(str(путь) + "-wal")
    try:
        if журнал.is_file() and журнал.stat().st_size > 0:
            соединение = sqlite3.connect(str(путь))
            try:
                соединение.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                соединение.close()
    except sqlite3.Error as exc:
        log.warning("Журнал снимка %s не влит: %s", путь.name, exc)
        return
    for хвост in СПУТНИКИ_БАЗЫ:
        спутник = Path(str(путь) + хвост)
        try:
            if хвост == "-shm" or (спутник.is_file() and спутник.stat().st_size == 0):
                спутник.unlink(missing_ok=True)
        except OSError:
            pass


def _проверить_снимок(путь: Path) -> None:
    """Быстрая проверка целостности снимка — до того, как он станет копией.

    «.backup» копирует страницы как есть: база, испорченная на диске, даёт
    такой же испорченный снимок, и узнают об этом в день восстановления.
    Проверка читает снимок целиком — секунды на гигабайт, — зато копия,
    лёгшая в каталог, заведомо восстанавливается, а порча рабочей базы
    видна сразу: копия не снимается, и в журнале — почему.
    """
    try:
        соединение = sqlite3.connect(_uri_только_чтение(путь), uri=True)
        try:
            ответ = str(соединение.execute("PRAGMA quick_check").fetchone()[0])
        finally:
            соединение.close()
    except sqlite3.Error as exc:
        ответ = f"проверка не прошла: {exc}"
    _влить_спутники(путь)
    if ответ.lower() != "ok":
        raise StorageError(
            f"Копия не снята: снимок базы не прошёл проверку целостности ({ответ[:200]}).",
            hint="Рабочая база повреждена. Остановите сервер, проверьте её "
                 "(sqlite3 asrhub.db \"PRAGMA integrity_check\") и восстановите "
                 "из последней исправной копии.")


def _временный_каталог(settings: Any) -> Path:
    """Где распаковывать копию: каталог временных файлов сервера.

    Не системный /tmp: на многих системах это tmpfs, то есть память, и
    полная копия с базой в несколько гигабайт ложилась в неё целиком —
    рядом с моделями.
    """
    задан = str(settings.get("temp_dir") or "").strip()
    путь = Path(задан) if задан else Path(
        getattr(settings.paths, "tmp", None) or Path(settings.paths.data) / "tmp")
    try:
        путь.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StorageError(f"Каталог временных файлов недоступен: {путь}: {exc}",
                           hint="Проверьте настройку temp_dir и права на каталог.") from exc
    return путь


def создать(db: Any, settings: Any, *, kind: str = "full", comment: str = "",
            include_results: bool | None = None) -> dict[str, Any]:
    """Снимает копию и возвращает её описание.

    Архив собирается во временном файле рядом и переименовывается в конце.
    Иначе прерванная на середине копия остаётся в каталоге как обычная: её
    видно в списке, её можно выбрать для восстановления, и узнать о том,
    что она обрезана, можно только в самый неподходящий момент.
    """
    if kind not in ВИДЫ:
        raise ConfigError(f"Неизвестный вид копии: {kind!r}",
                          hint=f"Бывают: {', '.join(ВИДЫ)}.")
    место = _готовый_каталог(settings)
    подчистить(settings, место_под_ещё_одну=True, вид=kind)
    класть_результаты = (bool(settings.get("backup_include_results", False))
                         if include_results is None else bool(include_results))
    # Исходные записи (`uploads`) по умолчанию в копию не входят: это самая
    # большая часть данных. Флажок `backup_include_uploads` их добавляет —
    # без него восстановленный сервер показывает расшифровки, но не играет
    # звук и не может перераспознать.
    класть_записи = bool(settings.get("backup_include_uploads", False))
    if kind == "full":
        _хватит_ли_места(db, settings, место, класть_результаты, класть_записи)

    начало = time.time()
    состав = ["settings.json"]
    # Каталог может оказаться чужим (его создал `service.sh backup` от root):
    # mkdtemp вне обработки давал 500 вместо объяснения.
    try:
        временный = Path(tempfile.mkdtemp(prefix="asrhub-backup-", dir=str(место)))
    except OSError as exc:
        raise StorageError(
            f"Резервная копия не снята: каталог копий «{место}» недоступен на запись: {exc}",
            hint="Проверьте владельца каталога копий: он должен принадлежать "
                 "пользователю, от которого работает сервер.") from exc
    цель = место / _имя(kind)
    # Черновик — внутри временного каталога, а не рядом с целью: имя у него
    # тогда уникально по построению, и брошенный при сбое он уходит вместе
    # с каталогом, а не остаётся в списке копий мусором.
    черновик = временный / "архив.part"
    try:
        (временный / "settings.json").write_text(
            json.dumps(_настройки_наружу(settings), ensure_ascii=False, indent=2),
            encoding="utf-8")
        # Поле называется `config_file`. Здесь стояло `config_path`, и
        # `getattr` честно возвращал None: копия НИКОГДА не содержала файла
        # настроек. Вместе с ним не копировались ключи доступа и токен
        # Hugging Face — то есть восстановление из полной копии поднимало
        # сервер, в который нельзя войти. Подставные настройки в проверке
        # были объявлены под тем же неверным именем, поэтому она это и не
        # ловила.
        файл_настроек = getattr(settings, "config_file", None)
        if файл_настроек and Path(файл_настроек).is_file():
            shutil.copy2(файл_настроек, временный / "asrhub.yaml")
            состав.append("asrhub.yaml")
        if kind == "full":
            (временный / "db").mkdir()
            _снять_базу(db, временный / "db" / "asrhub.db")
            _проверить_снимок(временный / "db" / "asrhub.db")
            состав.append("db/asrhub.db")
            if класть_результаты:
                каталог_результатов = Path(settings.paths.results)
                if каталог_результатов.is_dir():
                    shutil.copytree(каталог_результатов, временный / "results",
                                    dirs_exist_ok=True)
                    состав.append("results/")
            if класть_записи:
                каталог_записей = Path(settings.paths.uploads)
                if каталог_записей.is_dir():
                    shutil.copytree(каталог_записей, временный / "uploads",
                                    dirs_exist_ok=True)
                    состав.append("uploads/")
        опись = _опись(db, settings, kind, comment, состав)
        (временный / ОПИСЬ).write_text(
            json.dumps(опись, ensure_ascii=False, indent=2), encoding="utf-8")

        # Каждая запись — ровно один раз и без рекурсии. Цикл шёл по
        # `rglob("*")` и звал `add` для каждого пути, а `tarfile.add` у
        # каталога рекурсивен: база ложилась в архив дважды (через «db/» и
        # сама), файлы результатов — трижды. Копия весила вдвое-втрое больше
        # нужного, а распаковка, складывавшая размеры всех записей, отвергала
        # собственную свежую копию базы от 32 ГБ как «слишком велика».
        with tarfile.open(черновик, "w:gz", compresslevel=УРОВЕНЬ_СЖАТИЯ) as архив:
            for путь in _состав_архива(временный, исключить=черновик):
                архив.add(путь, arcname=путь.relative_to(временный).as_posix(),
                          recursive=False)
        # Права — до переименования, пока черновик лежит во временном
        # каталоге (mkdtemp создаёт его закрытым): копия ни мгновения не
        # лежит в каталоге копий открытой.
        os.chmod(черновик, 0o600)
        # Имя выбирается в последний момент и под замком: с точностью до
        # секунды два захода выбрали бы одно.
        with _ЗАМОК:
            цель = место / _имя(kind)
            счётчик = 2
            while цель.exists():
                цель = место / _имя(kind).replace(РАСШИРЕНИЕ, f"-{счётчик}{РАСШИРЕНИЕ}")
                счётчик += 1
            черновик.replace(цель)
        # Архив записан нами целиком и закрыт — перечитывать его ради списка
        # незачем (см. `_проверка_архива`).
        _запомнить_проверку(цель, "")
    except (OSError, sqlite3.Error) as exc:
        raise StorageError(
            f"Резервная копия не снята: {exc}",
            hint="Обычно это нехватка места или права на каталог копий. "
                 "Свободное место и путь видны в разделе «Сервер».") from exc
    finally:
        shutil.rmtree(временный, ignore_errors=True)

    размер = цель.stat().st_size
    log.info("Резервная копия (%s): %s, %.2f МБ, за %.1f с",
             ВИДЫ[kind], цель.name, размер / 1048576, time.time() - начало)
    try:
        db.add_event(None, "backup_created",
                     f"Копия «{ВИДЫ[kind]}»: {цель.name}, "
                     f"{размер / 1048576:.1f} МБ")
    except Exception:                                        # noqa: BLE001
        pass
    подчистить(settings)
    вынести(цель, settings, db)
    return _описание(цель).to_dict()


# ---------------------------------------------------------------------------
# Шифрование и вынос копии с сервера
# ---------------------------------------------------------------------------

#: Расширение зашифрованной копии (openssl enc, AES-256-CBC, PBKDF2).
ЗАШИФРОВАНО = ".enc"
#: Метка начала файла `openssl enc` с солью.
_МЕТКА_OPENSSL = b"Salted__"
KV_ВЫНОС = "backup:export"


def зашифрована(путь: Path) -> bool:
    """Зашифрован ли файл копии (`openssl enc` с солью)."""
    try:
        with open(путь, "rb") as файл:
            return файл.read(8) == _МЕТКА_OPENSSL
    except OSError:
        return False


def _openssl(аргументы: list[str], пароль: str, *, срок_с: float = 3600.0) -> None:
    import subprocess  # noqa: PLC0415

    окружение = {**os.environ, "ASRHUB_BACKUP_PASS": пароль}
    try:
        subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                        "-salt", *аргументы, "-pass", "env:ASRHUB_BACKUP_PASS"],
                       env=окружение, check=True, capture_output=True, timeout=срок_с)
    except FileNotFoundError as exc:
        raise ConfigError("Для шифрования копий нужна программа openssl.",
                          hint="Установите пакет openssl.") from exc
    except subprocess.CalledProcessError as exc:
        ошибка = (exc.stderr or b"").decode("utf-8", "replace").strip()[:300]
        raise ConfigError(
            f"openssl завершился с ошибкой: {ошибка or exc.returncode}",
            hint="Если это расшифровка — проверьте пароль копий (backup_password).") from exc


def зашифровать(путь: Path, пароль: str) -> Path:
    """Зашифрованная копия рядом: `<имя>.enc`, права 0600."""
    цель = путь.with_name(путь.name + ЗАШИФРОВАНО)
    _openssl(["-in", str(путь), "-out", str(цель)], пароль)
    os.chmod(цель, 0o600)
    return цель


def расшифровать(путь: Path, пароль: str, куда: Path) -> Path:
    """Расшифровывает копию в `куда` (права 0600)."""
    if not пароль:
        raise ConfigError("Копия зашифрована, а пароль копий не задан.",
                          hint="Укажите backup_password — тот, с которым копия снималась.")
    _openssl(["-d", "-in", str(путь), "-out", str(куда)], пароль)
    os.chmod(куда, 0o600)
    return куда


def вынести(цель: Path, settings: Any, db: Any = None, *, ждать: bool = False) -> bool:
    """Вынос копии с сервера: команда «после копии» в фоне.

    Команда (`backup_post_command`) задаётся только в файле настроек или
    переменной окружения — не из интерфейса: это запуск программы на
    сервере. `{file}` в ней заменяется путём копии; если задан пароль
    копий (`backup_password`), это путь зашифрованного `.enc`, который
    удаляется после команды, — наружу уходит только шифрованное.
    Возвращает, запущен ли вынос.
    """
    команда = str(settings.get("backup_post_command") or "").strip()
    if not команда:
        return False
    поток = threading.Thread(target=_вынести, args=(цель, settings, db, команда),
                             name="asrhub-backup-export", daemon=True)
    поток.start()
    if ждать:
        поток.join()
    return True


def _вынести(цель: Path, settings: Any, db: Any, команда: str) -> None:
    import shlex  # noqa: PLC0415
    import subprocess  # noqa: PLC0415

    пароль = str(settings.get("backup_password") or "")
    срок = float(settings.get("backup_post_timeout_s") or 1800)
    файл = цель
    итог: dict[str, Any] = {"ts": time.time(), "copy": цель.name, "ok": False,
                            "encrypted": bool(пароль)}
    try:
        if пароль:
            файл = зашифровать(цель, пароль)
        части = [ч.replace("{file}", str(файл)) for ч in shlex.split(команда)]
        if not any("{file}" in ч for ч in shlex.split(команда)):
            части.append(str(файл))
        ответ = subprocess.run(части, capture_output=True, timeout=срок, check=False,
                               env={**os.environ, "ASRHUB_BACKUP_FILE": str(файл)})
        итог["code"] = ответ.returncode
        итог["ok"] = ответ.returncode == 0
        if not итог["ok"]:
            итог["error"] = (ответ.stderr or ответ.stdout or b"").decode(
                "utf-8", "replace").strip()[-500:]
    except subprocess.TimeoutExpired:
        итог["error"] = f"команда не завершилась за {срок:.0f} с"
    except (OSError, ValueError, ConfigError) as exc:
        итог["error"] = getattr(exc, "message", None) or str(exc)
    finally:
        if файл != цель:
            файл.unlink(missing_ok=True)
    if итог["ok"]:
        log.info("Копия %s вынесена командой «после копии»", цель.name)
    else:
        log.warning("Копия %s не вынесена: %s", цель.name, итог.get("error"))
    if db is not None:
        try:
            db.set_kv(KV_ВЫНОС, итог)
            db.add_event(None, "backup_exported" if итог["ok"] else "backup_export_failed",
                         f"Вынос копии {цель.name}: "
                         + ("готово" if итог["ok"] else str(итог.get("error") or "сбой")))
        except Exception:                                    # noqa: BLE001
            pass


def _состав_архива(корень: Path, *, исключить: Path | None = None) -> list[Path]:
    """Что и в каком порядке ложится в архив: опись первой, дальше по алфавиту.

    Опись первой — ради списка копий: она читается перебором до первого
    совпадения (см. `_прочитать_опись`), и лежавшая за базой опись означала
    прогнать через gzip всю базу ради нескольких сотен байт. Спутники файла
    базы не входят (см. `СПУТНИКИ_БАЗЫ`).
    """
    состав: list[Path] = []
    опись = корень / ОПИСЬ
    if опись.is_file():
        состав.append(опись)
    for путь in sorted(корень.rglob("*")):
        if путь in (опись, исключить):
            continue
        if any(путь.name.endswith(хвост) for хвост in СПУТНИКИ_БАЗЫ) and \
                путь.with_name(путь.name.rsplit("-", 1)[0]).suffix == ".db":
            continue
        состав.append(путь)
    return состав


def _хватит_ли_места(db: Any, settings: Any, место: Path, с_результатами: bool,
                     с_записями: bool = False) -> None:
    """Отказ заранее, если на снимок базы не хватит места.

    Полная копия собирается во временном каталоге рядом с архивом: снимок
    базы, копия результатов и сам архив лежат там одновременно. На почти
    полном диске снимок доходил до конца места и падал, по дороге отняв его
    у рабочей базы, — а у той как раз в это время шли записи заданий.
    Оценка грубая (снимок и архив — каждый не больше базы), но порядок
    величины верный.
    """
    try:
        база = Path(getattr(db, "path", "") or _путь_базы(settings))
        нужно = 2 * база.stat().st_size if база.is_file() else 0
    except (OSError, TypeError, ValueError):
        return
    for нужен, вид in ((с_результатами, "results"), (с_записями, "uploads")):
        if not нужен:
            continue
        try:
            каталог_данных = Path(getattr(settings.paths, вид))
            нужно += 2 * sum(ф.stat().st_size for ф in каталог_данных.rglob("*")
                             if ф.is_file())
        except (OSError, AttributeError):
            pass
    try:
        свободно = shutil.disk_usage(место).free
    except OSError:
        return
    if нужно and свободно < нужно:
        raise StorageError(
            f"Резервная копия не снята: на её сборку нужно около {нужно / 1048576:.0f} МБ, "
            f"а в каталоге копий свободно {свободно / 1048576:.0f} МБ.",
            hint="Освободите место, уменьшите срок хранения копий (backup_keep_days) "
                 "или укажите каталог копий на другом диске (backup_dir).")


# ---------------------------------------------------------------------------
# Что лежит в каталоге
# ---------------------------------------------------------------------------

def _прочитать_опись(путь: Path) -> dict[str, Any]:
    """Опись копии — перебором записей до первого совпадения.

    `getmember` загружает заголовки ВСЕХ записей, то есть прогоняет через
    gzip весь архив: на десятке суточных копий базы в несколько гигабайт
    раздел «Копии» открывался минутами. Свои копии кладут опись первой (см.
    `_состав_архива`), и она читается сразу; у прежних она лежит за базой, и
    там перебор по-прежнему долгий — но только один раз на копию.
    """
    with tarfile.open(путь, "r:gz") as архив:
        for запись in архив:
            if запись.name.lstrip("./") != ОПИСЬ or not запись.isfile():
                continue
            поток = архив.extractfile(запись)
            if поток is None:
                return {}
            данные = json.loads(поток.read().decode("utf-8"))
            return данные if isinstance(данные, dict) else {}
        return {}


#: Где лежат итоги полной проверки архивов (см. `_проверка_архива`).
ПРОВЕРЕНО = ".asrhub-checked.json"
_ЗАМОК_ПРОВЕРОК = threading.Lock()


def _отпечаток_файла(путь: Path) -> list[int]:
    сведения = путь.stat()
    return [int(сведения.st_size), int(сведения.st_mtime_ns)]


def _проверки(место: Path) -> dict[str, Any]:
    try:
        данные = json.loads((место / ПРОВЕРЕНО).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return данные if isinstance(данные, dict) else {}


def _запомнить_проверку(путь: Path, ошибка: str) -> None:
    """Итог проверки архива — рядом с копиями, по размеру и времени файла."""
    with _ЗАМОК_ПРОВЕРОК:
        проверки = _проверки(путь.parent)
        try:
            проверки[путь.name] = [*_отпечаток_файла(путь), ошибка]
        except OSError:
            return
        # Записи о копиях, которых уже нет, — прочь, иначе файл растёт.
        проверки = {имя: з for имя, з in проверки.items() if (путь.parent / имя).exists()}
        файл = путь.parent / ПРОВЕРЕНО
        try:
            черновик = файл.with_name(файл.name + ".part")
            черновик.write_text(json.dumps(проверки, ensure_ascii=False), encoding="utf-8")
            os.chmod(черновик, 0o600)
            черновик.replace(файл)
        except OSError as exc:
            log.debug("Итог проверки копии не записан: %s", exc)


def _проверка_архива(путь: Path) -> str:
    """Читается ли архив целиком. "" — да, иначе — почему нет.

    Опись теперь лежит первой и читается сразу, а оборванный архив
    (оборванный перенос на сетевое хранилище) так не отличить от целого:
    обрыв прежде ловился описью, лежавшей в конце. Полная проверка
    прогоняет через gzip весь архив — поэтому она делается один раз на файл
    и запоминается по его размеру и времени (`ПРОВЕРЕНО`). Свою только что
    снятую и принятую со стороны копию сервер проверяет сам при записи, и
    список их не перечитывает.
    """
    try:
        отпечаток = _отпечаток_файла(путь)
    except OSError as exc:
        return f"файл недоступен: {exc}"
    запись = _проверки(путь.parent).get(путь.name)
    if isinstance(запись, list) and len(запись) == 3 and запись[:2] == отпечаток:
        return str(запись[2] or "")
    try:
        with tarfile.open(путь, "r:gz") as архив:
            for _ in архив:
                pass
        ошибка = ""
    except Exception as exc:                                 # noqa: BLE001
        ошибка = f"архив оборван или повреждён ({exc})"
    _запомнить_проверку(путь, ошибка)
    return ошибка


def _поля_описи(опись: dict[str, Any]) -> dict[str, Any]:
    """Поля описи в нужном виде. `ValueError`, если вид не тот.

    Приведение стояло вне всякой обработки: копия с `"schema_version":
    "v31"` (правка руками, чужая версия) давала 500 на загрузке, файл
    оставался в каталоге, и раздел «Копии» отвечал 500 постоянно — а убрать
    файл через интерфейс было нельзя: имя берётся из списка.
    """
    try:
        состав = опись.get("contents") or []
        if not isinstance(состав, list):
            raise TypeError("contents — не список")
        return {
            "kind": str(опись.get("kind") or "full"),
            "created_at": float(опись.get("created_at") or 0),
            "version": str(опись.get("version") or ""),
            "schema_version": int(опись.get("schema_version") or 0),
            "instance": str(опись.get("instance") or ""),
            "comment": str(опись.get("comment") or ""),
            "contents": [str(часть) for часть in состав],
            "calls": int(опись.get("calls") or 0),
            "jobs": int(опись.get("jobs") or 0),
        }
    except (TypeError, ValueError) as exc:
        raise ValueError(f"поле описи не того вида: {exc}") from exc


def _описание(путь: Path) -> Копия:
    """Копия из файла: опись изнутри, а при её отсутствии — что видно снаружи."""
    статистика = путь.stat()
    копия = Копия(name=путь.name, size=статистика.st_size,
                  created_at=статистика.st_mtime)
    if путь.name.endswith(".db"):
        # Копия прежнего образца — просто файл базы. Восстанавливать её
        # умеем, поэтому и показываем: иначе человек видит в каталоге файлы,
        # которых нет в списке, и не понимает, какому из двух мест верить.
        копия.kind = "database"
        копия.contents = ["asrhub.db"]
        return копия
    try:
        поля = _поля_описи(_прочитать_опись(путь))
        ошибка = _проверка_архива(путь)
        if ошибка:
            raise ValueError(ошибка)
    except Exception as exc:                                 # noqa: BLE001
        # Ловим всё: gzip на оборванном файле кидает EOFError, на порче в
        # середине — zlib.error, и ни то, ни другое не наследуется от
        # TarError. Один недописанный файл (падение в момент копии,
        # оборванный перенос на сетевое хранилище) делал весь раздел
        # неоткрываемым, а убрать битую копию через интерфейс было нельзя:
        # имя берётся из списка, а списка нет. Опись с полями не того вида
        # — туда же (см. `_поля_описи`).
        копия.error = f"опись не читается: {exc}"
        return копия
    создана = поля.pop("created_at") or копия.created_at
    for ключ, значение in поля.items():
        setattr(копия, ключ, значение)
    копия.created_at = создана
    return копия


def список(settings: Any) -> list[dict[str, Any]]:
    """Все копии в каталоге, свежие первыми."""
    место = каталог(settings)
    if not место.is_dir():
        return []
    файлы: list[Path] = []
    for образец in (f"*{РАСШИРЕНИЕ}", "asrhub-*.db"):
        файлы.extend(место.glob(образец))
    копии = []
    for путь in файлы:
        try:
            копии.append(_описание(путь))
        except OSError as exc:
            log.warning("Копия %s не прочиталась: %s", путь.name, exc)
    копии.sort(key=lambda к: к.created_at, reverse=True)
    return [к.to_dict() for к in копии]


def свод(settings: Any) -> dict[str, Any]:
    """Состояние резервного копирования — шапка раздела."""
    копии = список(settings)
    место = каталог(settings)
    свободно = 0
    try:
        свободно = shutil.disk_usage(место if место.is_dir()
                                     else место.parent).free
    except OSError:
        pass
    настроек = [к for к in копии if к["kind"] == "settings"]
    полных = [к for к in копии if к["kind"] in ("full", "database")]
    return {
        "dir": str(место), "items": копии, "total": len(копии),
        "bytes": sum(int(к["size"]) for к in копии), "free_bytes": свободно,
        "last": копии[0] if копии else None,
        "last_settings": настроек[0] if настроек else None,
        "last_full": полных[0] if полных else None,
        "enabled": bool(settings.get("backup_enabled", True)),
        "time": str(settings.get("backup_time") or "00:01"),
        "interval_hours": _целое(settings, "backup_interval_hours", 0),
        "keep_days": _целое(settings, "backup_keep_days", 10),
        "keep_count": _целое(settings, "backup_keep", 0),
        "kind": str(settings.get("backup_kind") or "full"),
        "include_results": bool(settings.get("backup_include_results", False)),
        "include_uploads": bool(settings.get("backup_include_uploads", False)),
        "encrypted_export": bool(str(settings.get("backup_password") or "")),
        "export_command": bool(str(settings.get("backup_post_command") or "").strip()),
        "kinds": [{"key": к, "title": з} for к, з in ВИДЫ.items()],
        # База, подготовленная «Вернуть данные» и ждущая перезапуска. Её не
        # было видно нигде: нажали, не перезапустили — и через две недели
        # плановый перезапуск молча ставил двухнедельную копию.
        "pending_restore": ожидающее_восстановление(settings),
        # Прежние базы, сохранённые восстановлением, — каждая размером с базу.
        "previous_bases": прежние_базы(settings),
    }


def удалить(settings: Any, name: str) -> dict[str, Any]:
    путь = _путь_копии(settings, name)
    размер = путь.stat().st_size
    путь.unlink()
    log.info("Копия удалена: %s", путь.name)
    return {"removed": путь.name, "freed": размер}


# ---------------------------------------------------------------------------
# Сколько держать
# ---------------------------------------------------------------------------

def _целое(settings: Any, ключ: str, по_умолчанию: int) -> int:
    """Число из настройки, у которого ноль — значение, а не «не задано».

    В Python ноль ложен, и `int(значение or 10)` превращает «хранить
    бессрочно» в десять суток — ровно наоборот тому, что просили.
    """
    значение = settings.get(ключ)
    if значение is None or значение == "":
        return по_умолчанию
    try:
        return max(0, int(значение))
    except (TypeError, ValueError):
        return по_умолчанию


def _группа(путь: Path) -> str:
    """К какой группе относится копия: «settings» или «data».

    Предел числа и защита «последнюю не трогаем» считаются по группам: копия
    настроек не заменяет копию данных. Полная копия и копия базы прежнего
    образца — одна группа: обе возвращают данные. Вид берётся из имени,
    которое даёт сервер, а у присланных копий с другим именем — из описи.
    """
    имя = путь.name
    if имя.endswith(".db"):
        return "data"
    if "-settings-" in имя:
        return "settings"
    if "-full-" in имя:
        return "data"
    try:
        вид = str(_прочитать_опись(путь).get("kind") or "full")
    except Exception:                                        # noqa: BLE001
        вид = "full"
    return "settings" if вид == "settings" else "data"


def подчистить(settings: Any, *, место_под_ещё_одну: bool = False,
               вид: str | None = None) -> list[str]:
    """Убирает копии старше срока и сверх числа. Возвращает, что убрала.

    Подчистка идёт ДО снятия новой копии, а не после: каталог копий обычно
    лежит на том же диске, что и база, и если места хватает ровно на
    предельное число копий, очередная не помещается — а удаляет старые
    строка, до которой дело уже не доходит. С этого мгновения свежих копий
    не появляется вообще никогда, и молча.

    Срок, предел числа и защита последней копии считаются по группам (см.
    `_группа`). Прежде `backup_keep` считал все файлы подряд: при пределе 3
    и трёх ночных полных копиях три «копии настроек перед правкой» удаляли
    все копии данных, а свежая копия настроек «прикрывала» удаление
    последней исправной полной по сроку. `вид` — какую копию сейчас снимут:
    место освобождается только в её группе.
    """
    дней = _целое(settings, "backup_keep_days", 10)
    штук = _целое(settings, "backup_keep", 0)
    место = каталог(settings)
    if not место.is_dir():
        return _подчистить_прежние_базы(settings, дней)
    копии: list[Path] = []
    for образец in (f"*{РАСШИРЕНИЕ}", "asrhub-*.db"):
        копии.extend(место.glob(образец))
    try:
        копии.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    своя_группа = None
    if вид is not None:
        своя_группа = "settings" if вид == "settings" else "data"

    группы: dict[str, list[Path]] = {}
    for путь in копии:
        группы.setdefault(_группа(путь), []).append(путь)

    убрать: set[Path] = set()
    for группа, свои in группы.items():
        лишние: set[Path] = set()
        if дней > 0:
            граница = time.time() - дней * 86400
            for путь in свои:
                try:
                    if путь.stat().st_mtime < граница:
                        лишние.add(путь)
                except OSError:
                    continue
        if штук > 0:
            предел = штук
            if место_под_ещё_одну and (своя_группа is None or своя_группа == группа):
                предел = max(0, штук - 1)
            лишние.update(свои[предел:])
        # Последнюю копию группы не трогаем никогда, каким бы ни был срок:
        # каталог, в котором по правилам не должно остаться ни одной копии
        # данных, — это не «чисто», а «резервных копий нет».
        if свои and свои[0] in лишние and len(лишние) >= len(свои):
            лишние.discard(свои[0])
        убрать |= лишние

    убранные = []
    for путь in sorted(убрать):
        try:
            путь.unlink()
            убранные.append(путь.name)
            log.info("Удалена старая копия: %s", путь.name)
        except OSError as exc:
            log.warning("Копию %s удалить не удалось: %s", путь.name, exc)
    убранные.extend(_подчистить_прежние_базы(settings, дней))
    return убранные


def прежние_базы(settings: Any) -> list[dict[str, Any]]:
    """Базы, сохранённые восстановлением рядом с рабочей (`….before-restore-…`)."""
    try:
        база = _путь_базы(settings)
        файлы = sorted(база.parent.glob(f"{база.name}.before-restore-*"))
    except (OSError, AttributeError, TypeError):
        return []
    итог = []
    for путь in файлы:
        if any(путь.name.endswith(хвост) for хвост in СПУТНИКИ_БАЗЫ):
            continue
        try:
            сведения = путь.stat()
        except OSError:
            continue
        итог.append({"name": путь.name, "size": сведения.st_size,
                     "created_at": сведения.st_mtime})
    итог.sort(key=lambda з: float(з["created_at"]), reverse=True)  # type: ignore[arg-type]
    return итог


def _подчистить_прежние_базы(settings: Any, дней: int) -> list[str]:
    """Прежние базы старше срока хранения копий — кроме самой свежей.

    Каждая размером с базу, и копились они бессрочно: ни списка, ни уборки.
    Самую свежую оставляем всегда — это отмена последнего восстановления.
    """
    if дней <= 0:
        return []
    граница = time.time() - дней * 86400
    база = _путь_базы(settings)
    убранные = []
    for запись in прежние_базы(settings)[1:]:
        if запись["created_at"] >= граница:
            continue
        путь = база.with_name(запись["name"])
        try:
            путь.unlink()
            for хвост in СПУТНИКИ_БАЗЫ:
                Path(str(путь) + хвост).unlink(missing_ok=True)
            убранные.append(путь.name)
            log.info("Удалена прежняя база после восстановления: %s", путь.name)
        except OSError as exc:
            log.warning("Прежнюю базу %s удалить не удалось: %s", путь.name, exc)
    return убранные


# ---------------------------------------------------------------------------
# Восстановление
# ---------------------------------------------------------------------------

def _безопасно_распаковать(архив: tarfile.TarFile, куда: Path, *,
                           только: set[str] | None = None) -> None:
    """Распаковка с проверкой каждого имени и общего размера.

    Архив резервной копии приходит извне ровно так же, как любой другой
    файл: его могли принести с чужого сервера или подменить. Имя внутри
    архива вида «../../etc/cron.d/x» кладёт файл мимо каталога — это
    известная дыра всех распаковщиков; сжатые нули, разворачивающиеся в
    терабайт, забивают диск за секунды.

    Размер считается по разным именам, а не по всем записям: копии прежних версий
    несли каждый файл по два-три раза (см. `_состав_архива`), и сумма с
    повторами отвергала собственную исправную копию как «слишком велика».

    `только` — какие записи нужны. «Вернуть только настройки» из полной копии
    распаковывало всю базу во временный каталог ради файла в несколько
    килобайт; теперь перебор идёт до тех пор, пока не найдено нужное.
    """
    корень = куда.resolve()
    размеры: dict[str, int] = {}
    всего = 0
    выбранные: list[tarfile.TarInfo] = []
    осталось = set(только) if только is not None else None
    for запись in архив:
        имя = запись.name.lstrip("./") if только is not None else запись.name
        # Проверка имён — у каждой встреченной записи, и у ненужных тоже:
        # архив со ссылкой или путём за пределы — не копия ASR Hub, даже если
        # именно эту запись мы распаковывать не собирались.
        нужна = осталось is None or имя in осталось
        if запись.issym() or запись.islnk():
            raise ConfigError(
                f"В копии есть ссылка «{запись.name}» — такие архивы не принимаем.",
                hint="Скорее всего, это не копия ASR Hub.")
        if not (запись.isfile() or запись.isdir()):
            continue
        цель = (корень / запись.name).resolve()
        if not str(цель).startswith(str(корень) + os.sep) and цель != корень:
            raise ConfigError(
                f"В копии есть путь за её пределы: «{запись.name}».",
                hint="Файл повреждён или собран не ASR Hub.")
        if not нужна:
            continue
        размер = max(0, int(запись.size or 0))
        всего += размер - размеры.get(запись.name, 0)
        размеры[запись.name] = размер
        if всего > ПРЕДЕЛ_РАСПАКОВКИ:
            raise ConfigError(
                "Копия слишком велика — распаковка отменена.",
                hint=f"Предел {ПРЕДЕЛ_РАСПАКОВКИ // 1024 ** 3} ГБ; "
                     "проверьте, что это действительно резервная копия.")
        выбранные.append(запись)
        if осталось is not None:
            осталось.discard(имя)
            if not осталось:
                break
    # filter="data" — штатная защита Python: снимает права, владельцев и
    # устройства. Проверка выше от неё не избавляет: она про содержимое, эта
    # про имена, и падать нужно до того, как на диск ляжет первый файл.
    архив.extractall(куда, members=выбранные, filter="data")


def восстановить(db: Any, settings: Any, name: str, *, what: str = "settings",
                 apply_settings: bool = True) -> dict[str, Any]:
    """Возвращает настройки, а при `what="full"` — и базу.

    Настройки применяются на ходу: их для того и снимают, чтобы поднять
    сервер как был, не перезапуская. База так не умеет — она открыта
    прямо сейчас, — поэтому файл подменяется, а сервер просит перезапуска
    и говорит об этом прямо. Прежняя база не удаляется, а переименовывается:
    восстановление не из той копии — обычная ошибка, и она обязана быть
    обратимой.
    """
    if what not in ("settings", "full"):
        raise ConfigError(f"Неизвестный вид восстановления: {what!r}",
                          hint="Бывают: settings, full.")
    путь = _путь_копии(settings, name)
    if what == "full":
        _нет_соседей(db)
    итог: dict[str, Any] = {"name": путь.name, "what": what, "applied": 0,
                            "restart_required": False, "database": False}

    if путь.name.endswith(".db"):
        # Копия прежнего образца — только база, настроек в ней нет.
        if what == "settings":
            raise ConfigError(
                f"В копии «{путь.name}» нет настроек: это копия базы прежнего образца.",
                hint="Восстановите из неё данные или возьмите копию вида "
                     "«только настройки».")
        итог.update(_вернуть_базу(путь, settings))
        # След — в копию, ждущую запуска: она станет рабочей базой.
        _записать_событие(db, f"Восстановлена база из {путь.name}",
                          цель=_путь_базы(settings).with_name(
                              _путь_базы(settings).name + ОЖИДАЕТ_ЗАПУСКА))
        return итог

    временный = Path(tempfile.mkdtemp(prefix="asrhub-restore-",
                                      dir=str(_временный_каталог(settings))))
    try:
        try:
            with tarfile.open(путь, "r:gz") as архив:
                # Настройки из полной копии — без её базы: две записи, а не
                # гигабайты во временном каталоге.
                _безопасно_распаковать(
                    архив, временный,
                    только=None if what == "full" else {ОПИСЬ, "settings.json"})
        except (tarfile.TarError, EOFError) as exc:
            raise ConfigError(
                f"Копия «{путь.name}» не читается: {exc}",
                hint="Файл повреждён или это не резервная копия ASR Hub.") from exc

        опись_файл = временный / ОПИСЬ
        try:
            опись = (json.loads(опись_файл.read_text(encoding="utf-8"))
                     if опись_файл.is_file() else {})
            if not isinstance(опись, dict):
                raise ValueError("опись — не объект")
            формат = int(опись.get("format") or ФОРМАТ)
        except (OSError, TypeError, ValueError) as exc:
            raise ConfigError(f"Опись копии «{путь.name}» не читается: {exc}",
                              hint="Файл повреждён или изменён вручную.") from exc
        if формат > ФОРМАТ:
            raise ConfigError(
                f"Копия снята более новой версией сервера (формат {опись.get('format')}).",
                hint=f"Этот сервер понимает формат {ФОРМАТ}. Обновите ASR Hub.")
        итог["from_version"] = str(опись.get("version") or "")
        try:
            итог["created_at"] = float(опись.get("created_at") or 0)
        except (TypeError, ValueError):
            итог["created_at"] = 0.0

        # Проверяем всё, что может отказать, ДО первой записи — и базу тоже.
        # Комментарий это обещал давно, а проверялось только наличие файла
        # базы: настройки применялись раньше `_вернуть_базу`, где и случаются
        # настоящие отказы (порча, «нет таблицы заданий», нехватка места).
        # Копия с другого сервера с повреждённой при переносе базой отвечала
        # ошибкой, а чужие настройки — вместе с `result_retention_days` —
        # оставались применёнными и записанными, и ближайшая уборка стирала
        # архив по чужому сроку. Теперь порядок: настройки прочитаны и
        # проверены → база проверена и подготовлена → результаты на месте →
        # и только тогда настройки применяются. Отказ после подготовки базы
        # её подготовку снимает.
        файл_базы = временный / "db" / "asrhub.db"
        if what == "full" and not файл_базы.is_file():
            raise ConfigError(
                f"В копии «{путь.name}» нет базы: это копия только настроек.",
                hint="Данные можно вернуть из копии вида «настройки и данные».")
        значения = (_прочитать_настройки(временный / "settings.json")
                    if apply_settings else None)
        if what == "full":
            итог.update(_вернуть_базу(файл_базы, settings,
                                      старый_каталог=str(опись.get("data_dir") or "")))
        try:
            if what == "full":
                результаты = временный / "results"
                if результаты.is_dir():
                    try:
                        shutil.copytree(результаты, Path(settings.paths.results),
                                        dirs_exist_ok=True)
                    except OSError as exc:
                        raise StorageError(
                            f"Файлы результатов из копии не скопированы: {exc}. "
                            "Восстановление отменено, рабочая база не тронута.",
                            hint="Проверьте свободное место в каталоге данных.") from exc
                    итог["results"] = True
                записи = временный / "uploads"
                if записи.is_dir():
                    try:
                        shutil.copytree(записи, Path(settings.paths.uploads),
                                        dirs_exist_ok=True)
                    except OSError as exc:
                        raise StorageError(
                            f"Исходные записи из копии не скопированы: {exc}. "
                            "Восстановление отменено, рабочая база не тронута.",
                            hint="Проверьте свободное место в каталоге данных.") from exc
                    итог["uploads"] = True
            if значения is not None:
                итог["applied"] = _применить_настройки(значения, settings)
        except BaseException:
            if итог.get("database"):
                отменить_ожидающее(settings, причина="восстановление не завершилось")
                итог["database"] = False
            raise
    finally:
        shutil.rmtree(временный, ignore_errors=True)

    что = ("настройки" if what == "settings" else "настройки и данные")
    # След — в ту базу, которая станет рабочей после перезапуска: в копию,
    # ждущую запуска. Нынешняя рабочая после подмены станет «прежней».
    ожидающая = None
    if итог.get("database"):
        база = _путь_базы(settings)
        ожидающая = база.with_name(база.name + ОЖИДАЕТ_ЗАПУСКА)
    _записать_событие(db, f"Восстановлено ({что}) из {путь.name}: "
                          f"параметров {итог['applied']}", цель=ожидающая)
    log.info("Восстановление из %s: %s, параметров %d, база %s",
             путь.name, что, итог["applied"], "да" if итог["database"] else "нет")
    return итог


def _вернуть_настройки(файл: Path, settings: Any) -> int:
    """Применяет значения из копии. Возвращает, сколько параметров легло."""
    return _применить_настройки(_прочитать_настройки(файл), settings)


def _применить_настройки(свои: dict[str, Any], settings: Any) -> int:
    """Кладёт проверенные значения в настройки и записывает их в файл."""
    for ключ, значение in свои.items():
        settings.set(ключ, значение, source="restore")
    try:
        settings.save()
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Настройки применены, но в файл не записаны: %s", exc)
    return len(свои)


def _прочитать_настройки(файл: Path) -> dict[str, Any]:
    """Значения из копии — прочитанные, приведённые и проверенные, но не применённые.

    Параметры, которых нет в каталоге этой версии, пропускаются молча: в
    копии со старого сервера они есть, а смысла у них здесь нет, и падать
    из-за них — значит сделать копию бесполезной после каждого обновления.
    """
    from .catalog import PARAMS_BY_KEY, coerce_all, validate_all  # noqa: PLC0415

    if not файл.is_file():
        raise ConfigError("В копии нет настроек.",
                          hint="Похоже, архив собран не ASR Hub.")
    try:
        значения = json.loads(файл.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ConfigError(f"Настройки в копии не читаются: {exc}",
                          hint="Файл повреждён.") from exc
    if not isinstance(значения, dict):
        raise ConfigError("Настройки в копии имеют неожиданный вид.",
                          hint="Ожидается объект «ключ: значение».")
    # Копия снята прежней версией — значит, часть значений записана в той
    # форме, которая была тогда. Приводим, иначе копия становится
    # бесполезной ровно в тот момент, когда она нужнее всего: после
    # обновления, которое пошло не так.
    свои = coerce_all({к: з for к, з in значения.items() if к in PARAMS_BY_KEY})
    # Проверяем ДО применения и целиком: наполовину применённая копия
    # оставляет сервер в состоянии, которого не было ни до, ни после.
    ошибки = validate_all(свои)
    if ошибки:
        raise ConfigError(
            "Настройки в копии не проходят проверку: " + "; ".join(ошибки[:5]),
            hint="Копия снята другой версией сервера или изменена вручную.")
    return свои


#: Суффикс базы из копии, которая ждёт перезапуска сервера (см. `_вернуть_базу`).
ОЖИДАЕТ_ЗАПУСКА = ".restore-pending"

#: Рядом с ожидающей базой — почему запуск её не поставил (см.
#: `применить_отложенное`): соседи над базой или истёкший срок ожидания.
#: Отдельным файлом, потому что пишется до того, как база открыта, а
#: показывается — разделом «Копии» и самопроверкой.
ПРИЧИНА_ОТСРОЧКИ = ".restore-pending.json"


def _путь_базы(settings: Any) -> Path:
    return Path(getattr(settings.paths, "db", "") or Path(settings.paths.data) / "asrhub.db")


def _ожидающая(путь_базы: Path | str) -> Path:
    цель = Path(путь_базы)
    return цель.with_name(цель.name + ОЖИДАЕТ_ЗАПУСКА)


def _файл_причины(путь_базы: Path | str) -> Path:
    цель = Path(путь_базы)
    return цель.with_name(цель.name + ПРИЧИНА_ОТСРОЧКИ)


def причина_отсрочки(путь_базы: Path | str) -> dict[str, Any]:
    """Почему подготовленная база не поставлена при запуске ({} — не откладывали)."""
    try:
        данные = json.loads(_файл_причины(путь_базы).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return данные if isinstance(данные, dict) else {}


def _записать_причину(путь_базы: Path, **поля: Any) -> None:
    try:
        файл = _файл_причины(путь_базы)
        файл.write_text(json.dumps({"at": time.time(), **поля}, ensure_ascii=False),
                        encoding="utf-8")
        os.chmod(файл, 0o600)
    except OSError as exc:
        log.debug("Причина отсрочки восстановления не записана: %s", exc)


def ожидающее_восстановление(settings: Any) -> dict[str, Any] | None:
    """Подготовленная к восстановлению база, если она есть, — для свода и проверки."""
    try:
        база = _путь_базы(settings)
        путь = _ожидающая(база)
        сведения = путь.stat()
    except (OSError, AttributeError, TypeError):
        return None
    возраст = max(0.0, time.time() - сведения.st_mtime)
    причина = причина_отсрочки(база)
    return {"name": путь.name, "size": сведения.st_size,
            "prepared_at": сведения.st_mtime, "age_s": round(возраст, 1),
            "expired": возраст > СРОК_ОЖИДАНИЯ_С, "max_age_s": СРОК_ОЖИДАНИЯ_С,
            "deferred": причина.get("reason") or "",
            "deferred_at": причина.get("at")}


def отменить_ожидающее(settings: Any, *, причина: str = "отменено") -> bool:
    """Снимает подготовленную к восстановлению базу. True — было что снимать."""
    база = _путь_базы(settings)
    путь = _ожидающая(база)
    было = путь.is_file()
    for лишний in (путь, Path(str(путь) + ".part"), _файл_причины(база),
                   *(Path(str(путь) + хвост) for хвост in СПУТНИКИ_БАЗЫ)):
        try:
            лишний.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Не удалось убрать %s: %s", лишний.name, exc)
    if было:
        log.warning("Подготовленное восстановление базы снято (%s): %s", причина, путь.name)
    return было


def подтвердить_ожидающее(settings: Any) -> dict[str, Any]:
    """Продлевает ожидание подготовленной базы: «да, поставить при перезапуске».

    Нужна для копии старше `СРОК_ОЖИДАНИЯ_С`: запуск её больше не ставит
    молча. Подтверждение обновляет время подготовки и снимает причину
    прошлой отсрочки.
    """
    база = _путь_базы(settings)
    путь = _ожидающая(база)
    if not путь.is_file():
        raise ConfigError("Подготовленного восстановления нет.",
                          hint="Восстановление данных: POST /api/backup/restore с what=full.")
    os.utime(путь, None)
    _файл_причины(база).unlink(missing_ok=True)
    log.warning("Подготовленное восстановление базы подтверждено: %s", путь.name)
    return ожидающее_восстановление(settings) or {}


def _похожа_на_базу(путь: Path) -> bool:
    """Заголовок SQLite в начале файла — прежде чем открывать его на запись.

    Открытая на запись испорченная база со своим журналом рядом — это
    журнал, который SQLite сочтёт мусором и удалит при закрытии. Файл,
    который базой не выглядит, не открываем вовсе: пусть лежит как есть.
    """
    try:
        with open(путь, "rb") as файл:
            return файл.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _соседи_по_файлу(путь_базы: Path) -> list[dict[str, Any]]:
    """Живые экземпляры над базой — прямо по файлу, без открытой `Database`."""
    from .db import РЕЕСТР, СВЕЖЕСТЬ_ОТМЕТКИ_С, _живые_отметки  # noqa: PLC0415

    if not путь_базы.is_file() or not _похожа_на_базу(путь_базы):
        return []
    try:
        соединение = sqlite3.connect(_uri_только_чтение(путь_базы), uri=True, timeout=5.0)
        try:
            строки = соединение.execute(
                "SELECT key, value, ts FROM kv WHERE substr(key, 1, ?) = ?",
                (len(РЕЕСТР), РЕЕСТР)).fetchall()
        finally:
            соединение.close()
    except sqlite3.Error as exc:
        log.debug("Отметки экземпляров не прочитаны: %s", exc)
        return []
    return [о for о in _живые_отметки(строки, свежее=time.time() - СВЕЖЕСТЬ_ОТМЕТКИ_С)
            if not о["self"]]


def _нет_соседей(db: Any) -> None:
    """Отказ, если над базой работает ещё кто-то, кроме этого сервера.

    Восстановление данных подменяет файл базы при следующем запуске ЭТОГО
    сервера. Соседний сервер над общей базой держит её открытой и после
    подмены продолжает писать в прежний файл — то самое, от чего
    восстановление под работающим сервером и было переделано.
    """
    try:
        соседи = db.other_instances()
    except Exception as exc:                                 # noqa: BLE001
        log.debug("Соседи не проверены: %s", exc)
        return
    if соседи:
        raise ConfigError(
            "Над базой работают другие серверы: "
            + ", ".join(с["instance"] for с in соседи[:5])
            + ". Восстановление данных подменит базу и для них.",
            hint="Остановите остальные серверы над этим каталогом данных и "
                 "повторите; отметка остановленного сервера гаснет за пять минут.")


def _старый_каталог(соединение: sqlite3.Connection) -> str:
    """Каталог данных, в котором жила база, — по путям самих заданий.

    Для копий прежнего образца (файл базы без описи): результаты лежат в
    `<данные>/results/<номер задания>`, загрузки — в `<данные>/uploads/…`.
    Такой путь однозначен — номер задания в конце совпадает с номером.
    """
    from collections import Counter  # noqa: PLC0415
    from pathlib import PurePosixPath, PureWindowsPath  # noqa: PLC0415

    счёт: Counter[str] = Counter()
    try:
        строки = соединение.execute(
            "SELECT id, result_path, file_path FROM jobs "
            "WHERE COALESCE(result_path,'') <> '' OR COALESCE(file_path,'') <> '' "
            "ORDER BY created_at DESC LIMIT 200").fetchall()
    except sqlite3.Error:
        return ""
    for номер, результат, файл in строки:
        for путь, каталог in ((результат, "results"), (файл, "uploads")):
            путь = str(путь or "")
            if not путь:
                continue
            вид = PureWindowsPath(путь) if ("\\" in путь and "/" not in путь) else PurePosixPath(путь)
            if вид.parent.name != каталог:
                continue
            if каталог == "results" and вид.name != str(номер):
                continue
            счёт[str(вид.parent.parent)] += 1
    return счёт.most_common(1)[0][0] if счёт else ""


def _переписать_пути(файл: Path, старый: str, новый: str) -> int:
    """Пути заданий из прежнего каталога данных — в нынешний. Возвращает, сколько.

    Копию переносят на сервер с другим каталогом данных, а в базе лежат
    абсолютные пути прежнего: удаление задания (и по требованию субъекта)
    и уборка по сроку файлов не находили, и расшифровка оставалась на
    диске без задания, прослушивание отвечало «файл не найден». Меняется
    только начало пути, совпадающее с прежним каталогом: записи из архива
    станции лежат вне каталога данных и остаются как есть. Разделитель
    хвоста приводится к здешнему — копия с Windows приезжает с «\\».
    """
    старый = str(старый or "").rstrip("/\\")
    новый = str(новый or "").rstrip("/\\")
    if not старый or not новый or старый == новый:
        return 0
    разделитель = "\\" if ("\\" in новый and "/" not in новый) else "/"
    изменено = 0
    соединение = sqlite3.connect(str(файл))
    try:
        строки = соединение.execute(
            "SELECT id, file_path, result_path FROM jobs "
            "WHERE substr(COALESCE(file_path,''), 1, ?) = ? "
            "   OR substr(COALESCE(result_path,''), 1, ?) = ?",
            (len(старый), старый, len(старый), старый)).fetchall()
        правки = []
        for номер, файл_задания, результат in строки:
            пути = []
            for путь in (файл_задания, результат):
                путь = str(путь or "")
                хвост = путь[len(старый):] if путь.startswith(старый) else None
                # «/var/lib/asrhub2/…» начинается с «/var/lib/asrhub», но
                # лежит не в нём: за прежним каталогом обязан идти разделитель.
                if хвост is not None and хвост[:1] in ("/", "\\"):
                    путь = новый + разделитель + хвост[1:].replace(
                        "\\" if разделитель == "/" else "/", разделитель)
                пути.append(путь or None)
            if (пути[0], пути[1]) != (файл_задания or None, результат or None):
                правки.append((пути[0], пути[1], номер))
        with соединение:
            соединение.executemany(
                "UPDATE jobs SET file_path=?, result_path=? WHERE id=?", правки)
        изменено = len(правки)
    finally:
        соединение.close()
    if изменено:
        log.info("Пути %d заданий переписаны: %s → %s", изменено, старый, новый)
    return изменено


def _вернуть_базу(файл: Path, settings: Any, *, старый_каталог: str = "") -> dict[str, Any]:
    """Готовит базу из копии к подмене — сама подмена идёт при запуске.

    Целостность проверяется ДО того, как что-то трогать: восстановление из
    битого файла, обнаруженное после подмены, оставляет человека вообще без
    базы.

    Подменять файл под работающим сервером нельзя, а раньше так и делалось:
    у каждого потока своё соединение с базой, и долгоживущие потоки —
    воркеры очереди, служебный цикл, разбор, импорт с АТС — продолжали
    писать в переименованный прежний файл, а новые потоки открывали
    восстановленный. Один и тот же запрос отвечал по-разному в зависимости
    от потока, загрузка после «Вернуть данные» висела «в очереди» до
    перезапуска, а результаты заданий, шедших в тот момент, после
    перезапуска пропадали. Теперь копия кладётся рядом с базой и ждёт
    запуска: подменяет её `применить_отложенное` — до того, как база
    откроется хоть одним соединением.

    `старый_каталог` — каталог данных сервера, снявшего копию (из описи);
    для копии прежнего образца он угадывается по путям заданий. Пути
    заданий в готовящейся копии переписываются на нынешний каталог.
    """
    from .db import SCHEMA_VERSION  # noqa: PLC0415

    try:
        с_копией = sqlite3.connect(_uri_только_чтение(файл), uri=True)
        схема, есть_задания = 0, None
        try:
            итог = с_копией.execute("PRAGMA integrity_check").fetchone()[0]
            # У повреждённой базы дальше спрашивать нечего: ответ на любой
            # запрос — та же жалоба.
            if итог == "ok":
                схема = int(с_копией.execute("PRAGMA user_version").fetchone()[0] or 0)
                есть_задания = с_копией.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
                ).fetchone()
                if not старый_каталог and есть_задания:
                    старый_каталог = _старый_каталог(с_копией)
        finally:
            с_копией.close()
        _влить_спутники(файл)
    except sqlite3.DatabaseError as exc:
        raise ConfigError(
            f"Файл в копии не похож на базу ASR Hub: {exc}",
            hint="Возьмите другую копию.") from exc
    if итог != "ok":
        raise ConfigError(f"База в копии повреждена: {итог}",
                          hint="Восстановление отменено, рабочая база не тронута.")
    if not есть_задания:
        raise ConfigError("В файле нет таблицы заданий — это не база ASR Hub.",
                          hint="Возьмите копию, снятую сервером ASR Hub.")
    # База новее сервера молча вставала под старый код: миграция при
    # `current > SCHEMA_VERSION` лишь дописывает колонки, а то, что новая
    # версия переложила иначе, старая читала бы неверно — или писала
    # поверх. Опись хранит версию схемы, но сверялся только формат архива.
    if схема > SCHEMA_VERSION:
        raise ConfigError(
            f"База в копии новее этого сервера: схема {схема}, сервер понимает {SCHEMA_VERSION}.",
            hint="Обновите ASR Hub до версии, снявшей копию, и восстановите заново.")

    цель = _путь_базы(settings)
    ожидает = цель.with_name(цель.name + ОЖИДАЕТ_ЗАПУСКА)
    черновик = ожидает.with_name(ожидает.name + ".part")
    переписано = 0
    try:
        shutil.copyfile(файл, черновик)
        if цель.exists():
            shutil.copymode(цель, черновик)
        переписано = _переписать_пути(черновик, старый_каталог, str(settings.paths.data))
        for хвост in СПУТНИКИ_БАЗЫ:
            Path(str(черновик) + хвост).unlink(missing_ok=True)
        черновик.replace(ожидает)
        # Новая подготовка — новый отсчёт: причина прежней отсрочки к ней
        # не относится.
        _файл_причины(цель).unlink(missing_ok=True)
    except (OSError, sqlite3.Error) as exc:
        черновик.unlink(missing_ok=True)
        raise StorageError(
            f"База не подготовлена к восстановлению: {exc}. Рабочая база не тронута.",
            hint="Проверьте свободное место и права на каталог данных.") from exc
    return {"database": True, "restart_required": True, "pending": ожидает.name,
            "previous": "", "paths_rewritten": переписано,
            "from_data_dir": старый_каталог or ""}


def применить_отложенное(путь_базы: Path | str, *, срок_с: float | None = None) -> str:
    """Подменяет базу копией, подготовленной `_вернуть_базу`. Зовётся при запуске.

    Прежняя база сохраняется рядом под именем «….before-restore-ДАТА» — и
    сохраняется ЦЕЛИКОМ: сначала её журнал WAL вливается в сам файл. Раньше
    `-wal` откладывался под именем «asrhub.db-wal.before-restore-…», а SQLite
    ищет журнал по имени «<файл>-wal», и последние транзакции прежней базы
    при её возвращении не подхватывались никогда: из восьми заданий в
    возвращённой базе оставалось пять.

    Если над базой работают другие серверы (их отметки свежие), подмена
    откладывается до следующего запуска: соседи держат прежний файл
    открытым и продолжили бы писать в него. Копия старше `срок_с` (по
    умолчанию `СРОК_ОЖИДАНИЯ_С`) молча не ставится тоже: её подготовили
    давно, и за это время в рабочей базе накопилась работа. Причина отсрочки
    ложится рядом (см. `причина_отсрочки`) — её показывают раздел «Копии» и
    самопроверка, а запуск пишет событием. Раньше было только `log.error`, и
    администратор, поднявший сервер на старой базе, считал, что восстановил.

    Возвращает имя сохранённой прежней базы ("" — подменять было нечего).
    """
    цель = Path(путь_базы)
    ожидает = цель.with_name(цель.name + ОЖИДАЕТ_ЗАПУСКА)
    if not ожидает.is_file():
        return ""
    срок = СРОК_ОЖИДАНИЯ_С if срок_с is None else float(срок_с)
    try:
        возраст = time.time() - ожидает.stat().st_mtime
    except OSError:
        возраст = 0.0
    if срок > 0 and возраст > срок:
        причина = (f"копия подготовлена {возраст / 3600:.0f} ч назад — дольше "
                   f"{срок / 3600:.0f} ч; подтвердите её в разделе «Копии» или "
                   "восстановите заново")
        log.error("Восстановление базы из копии не поставлено: %s. Копия ждёт в %s.",
                  причина, ожидает.name)
        _записать_причину(цель, reason=причина, kind="expired")
        return ""
    соседи = _соседи_по_файлу(цель)
    if соседи:
        имена = [str(с["instance"]) for с in соседи[:5]]
        log.error(
            "Восстановление базы из копии отложено: над ней работают другие серверы "
            "(%s). Остановите их и перезапустите этот; копия ждёт в %s.",
            ", ".join(имена), ожидает.name)
        _записать_причину(цель, reason="над базой работают другие серверы: "
                                       + ", ".join(имена),
                          kind="neighbours", instances=имена)
        return ""
    метка = time.strftime("%Y%m%d-%H%M%S")
    запасная = цель.with_name(f"{цель.name}.before-restore-{метка}")
    счётчик = 2
    while запасная.exists():
        запасная = цель.with_name(f"{цель.name}.before-restore-{метка}-{счётчик}")
        счётчик += 1
    if цель.exists():
        # Сначала переезд — файл вместе со спутниками, под именами, по
        # которым SQLite найдёт их у отложенной базы; потом вливание журнала
        # уже под новым именем. В обратном порядке испорченная база (ради
        # таких восстановление и затевают), открытая на запись, теряла свой
        # журнал: SQLite считал его мусором и удалял при закрытии.
        цель.replace(запасная)
        for хвост in ("-wal", "-shm", "-journal"):
            спутник = Path(str(цель) + хвост)
            if спутник.exists():
                спутник.replace(Path(str(запасная) + хвост))
        if _похожа_на_базу(запасная):
            try:
                соединение = sqlite3.connect(str(запасная))
                try:
                    соединение.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    соединение.close()
            except sqlite3.Error as exc:
                log.warning("Журнал прежней базы не влит: %s — он лежит рядом с ней", exc)
    # У копии, ждущей запуска, могли остаться свои спутники (в неё писали
    # след о восстановлении): вливаем их, чтобы файл переехал целиком.
    try:
        соединение = sqlite3.connect(str(ожидает))
        try:
            соединение.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            соединение.close()
    except sqlite3.Error as exc:
        log.warning("Журнал восстанавливаемой базы не влит: %s", exc)
    for хвост in ("-wal", "-shm"):
        Path(str(ожидает) + хвост).unlink(missing_ok=True)
    ожидает.replace(цель)
    _файл_причины(цель).unlink(missing_ok=True)
    log.warning("База восстановлена из копии при запуске; прежняя сохранена как %s",
                запасная.name if запасная.exists() else "—")
    return запасная.name if запасная.exists() else ""


def восстановить_из_файла(путь: Path | str, settings: Any) -> dict[str, Any]:
    """Восстановление базы из командной строки — при остановленном сервере.

    Принимает и файл базы (копия прежнего образца, `service.sh backup`), и
    архив `*.asrhub.tar.gz`, который снимает расписание: раньше архив
    отвергался как «не похож на базу», хотя других копий сервер по
    расписанию не делает. Из архива берётся только база; настройки из него
    возвращаются в разделе «Копии» — на ходу, без остановки.

    Путь тот же, что у восстановления из интерфейса: копия проверяется,
    пути заданий переписываются под этот каталог данных, копия ложится
    ждать запуска — и тут же ставится (сервер остановлен). Прежняя база
    остаётся рядом со своим журналом.
    """
    источник = Path(путь)
    if not источник.is_file():
        raise ConfigError(f"Копия не найдена: {источник}")
    база = _путь_базы(settings)
    соседи = _соседи_по_файлу(база)
    if соседи:
        raise ConfigError(
            "Над базой работают серверы: " + ", ".join(с["instance"] for с in соседи[:5])
            + " — восстановление подменило бы базу под ними.",
            hint="Остановите все серверы над этим каталогом данных; отметка "
                 "остановленного сервера гаснет за пять минут.")
    итог: dict[str, Any] = {"source": str(источник)}
    if зашифрована(источник):
        открытый = Path(tempfile.mkdtemp(prefix="asrhub-decrypt-",
                                         dir=str(_временный_каталог(settings))))
        имя = источник.name[:-len(ЗАШИФРОВАНО)] if источник.name.endswith(ЗАШИФРОВАНО) \
            else источник.name
        try:
            расшифрованный = расшифровать(источник, str(settings.get("backup_password") or ""),
                                          открытый / имя)
            ответ = восстановить_из_файла(расшифрованный, settings)
        finally:
            shutil.rmtree(открытый, ignore_errors=True)
        ответ["source"] = str(источник)
        ответ["decrypted"] = True
        return ответ
    if источник.name.endswith(".db"):
        итог.update(_вернуть_базу(источник, settings))
    else:
        временный = Path(tempfile.mkdtemp(prefix="asrhub-restore-",
                                          dir=str(_временный_каталог(settings))))
        try:
            try:
                with tarfile.open(источник, "r:gz") as архив:
                    _безопасно_распаковать(архив, временный)
            except (tarfile.TarError, EOFError, OSError) as exc:
                raise ConfigError(
                    f"«{источник.name}» не читается как копия: {exc}",
                    hint="Ожидается файл базы *.db или архив *.asrhub.tar.gz.") from exc
            опись_файл = временный / ОПИСЬ
            try:
                опись = (json.loads(опись_файл.read_text(encoding="utf-8"))
                         if опись_файл.is_file() else {})
            except ValueError:
                опись = {}
            if not isinstance(опись, dict):
                опись = {}
            файл_базы = временный / "db" / "asrhub.db"
            if not файл_базы.is_file():
                raise ConfigError(
                    f"В копии «{источник.name}» нет базы: это копия только настроек.",
                    hint="Настройки возвращаются в разделе «Копии»; данные — из "
                         "копии вида «настройки и данные».")
            итог.update(_вернуть_базу(файл_базы, settings,
                                      старый_каталог=str(опись.get("data_dir") or "")))
            итог["from_version"] = str(опись.get("version") or "")
        finally:
            shutil.rmtree(временный, ignore_errors=True)
    итог["previous"] = применить_отложенное(база)
    итог["restart_required"] = False
    return итог


def принять(settings: Any, имя_файла: str, поток: Any) -> dict[str, Any]:
    """Кладёт присланный архив в каталог копий — восстановление с другой машины.

    Файл сначала пишется во временный, потом проверяется и только потом
    переименовывается: файл, который не является копией, не должен попадать
    в список — иначе его однажды выберут для восстановления.

    Файл базы (`*.db`) принимается под именем `asrhub-…db`: список,
    восстановление, удаление и подчистка видят только такие, и присланная
    «prod-2026-09-01.db» получала «принято», а в списке не появлялась —
    ни восстановить, ни удалить, и лежала в каталоге копий вечно. Заодно он
    проверяется: база ли это вообще и есть ли в ней задания.
    """
    место = _готовый_каталог(settings)
    короткое = os.path.basename(str(имя_файла or "").strip())
    # Зашифрованная копия (вынесенная с сервера с паролем копий) принимается
    # и расшифровывается здесь же: в каталоге копий лежат открытые архивы.
    шифр = короткое.endswith(РАСШИРЕНИЕ + ЗАШИФРОВАНО)
    if шифр:
        короткое = короткое[:-len(ЗАШИФРОВАНО)]
    архив = короткое.endswith(РАСШИРЕНИЕ)
    if not архив and not короткое.endswith(".db"):
        raise ConfigError(
            f"«{короткое}» не похоже на резервную копию ASR Hub.",
            hint=f"Принимаются файлы *{РАСШИРЕНИЕ} и копии базы *.db.")
    if not архив and not короткое.startswith("asrhub-"):
        короткое = f"asrhub-{короткое}"
    цель = место / короткое
    if цель.exists():
        основа = короткое[:-len(РАСШИРЕНИЕ)] if архив else короткое[:-3]
        хвост = РАСШИРЕНИЕ if архив else ".db"
        цель = место / f"{основа}-{time.strftime('%Y%m%d-%H%M%S')}{хвост}"
        счётчик = 2
        while цель.exists():
            цель = место / f"{основа}-{time.strftime('%Y%m%d-%H%M%S')}-{счётчик}{хвост}"
            счётчик += 1
    # Черновик — со своим именем: две загрузки одного файла в одну секунду
    # писали в один «.part» каждая со своего дескриптора. Имя не похоже на
    # копию, и в список черновик не попадает.
    дескриптор, имя_черновика = tempfile.mkstemp(prefix=".upload-", suffix=".part",
                                                 dir=str(место))
    черновик = Path(имя_черновика)
    записано = 0
    try:
        # Присланная копия — такая же, как своя: 0600 с самого начала.
        os.chmod(черновик, 0o600)
        with os.fdopen(дескриптор, "wb") as файл:
            while True:
                кусок = поток.read(1024 * 1024)
                if not кусок:
                    break
                записано += len(кусок)
                if записано > ПРЕДЕЛ_РАСПАКОВКИ:
                    raise ConfigError("Файл слишком велик.",
                                      hint="Это точно резервная копия?")
                файл.write(кусок)
        if шифр or зашифрована(черновик):
            открытый = черновик.with_name(черновик.name + ".plain")
            try:
                расшифровать(черновик, str(settings.get("backup_password") or ""), открытый)
                открытый.replace(черновик)
            finally:
                открытый.unlink(missing_ok=True)
        if архив:
            опись = _прочитать_опись(черновик)
            if not опись.get("kind"):
                raise ConfigError(
                    "В архиве нет описи — это не резервная копия ASR Hub.",
                    hint="Копии сервер собирает сам; чужие архивы не подойдут.")
            # Вид полей — до того, как файл ляжет в каталог: опись с
            # «schema_version: v31» прежде проходила, и раздел «Копии»
            # отвечал 500 при каждом открытии.
            try:
                _поля_описи(опись)
            except ValueError as exc:
                raise ConfigError(f"Опись копии повреждена: {exc}",
                                  hint="Архив изменён вручную или собран другой "
                                       "программой.") from exc
            # Целиком — до того, как файл ляжет в список: опись лежит первой и
            # читается и у оборванного архива (см. `_проверка_архива`).
            with tarfile.open(черновик, "r:gz") as проверяемый:
                for _ in проверяемый:
                    pass
        else:
            _проверить_базу(черновик)
        черновик.replace(цель)
        if архив:
            _запомнить_проверку(цель, "")
    except ConfigError:
        черновик.unlink(missing_ok=True)
        raise
    except Exception as exc:                                 # noqa: BLE001
        # Недокачанный архив кидает EOFError, а не TarError: без этого
        # пользователь получал 500 вместо объяснения, а на диске оставался
        # черновик размером с базу — невидимый в списке и неудаляемый.
        черновик.unlink(missing_ok=True)
        raise ConfigError(f"Файл не принят: {exc}",
                          hint="Похоже, копия повреждена при передаче.") from exc
    log.info("Принята копия со стороны: %s (%.2f МБ)", цель.name, записано / 1048576)
    return _описание(цель).to_dict()


def _проверить_базу(путь: Path) -> None:
    """Присланный файл базы: читается ли, цел ли и есть ли в нём задания."""
    try:
        соединение = sqlite3.connect(_uri_только_чтение(путь), uri=True)
        try:
            ответ = str(соединение.execute("PRAGMA quick_check").fetchone()[0])
            задания = соединение.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()
        finally:
            соединение.close()
        _влить_спутники(путь)
    except sqlite3.DatabaseError as exc:
        raise ConfigError(f"Файл не похож на базу ASR Hub: {exc}",
                          hint="Принимаются копии базы, снятые сервером.") from exc
    if ответ.lower() != "ok":
        raise ConfigError(f"База в присланном файле повреждена: {ответ[:200]}",
                          hint="Возьмите другую копию.")
    if not задания:
        raise ConfigError("В присланном файле нет таблицы заданий — это не база ASR Hub.",
                          hint="Принимаются копии базы, снятые сервером.")


def _записать_событие(db: Any, текст: str, *, цель: Path | None = None) -> None:
    """Событие в журнал. После подмены базы — прямо в новый файл.

    Открытый дескриптор после восстановления указывает на переименованную
    прежнюю базу, и запись уходила в файл, который никто больше не откроет:
    в журнале восстановленного сервера не оставалось следа того, что базу
    подменяли.
    """
    if цель is not None:
        try:
            прямо = sqlite3.connect(str(цель))
            try:
                with прямо:
                    прямо.execute(
                        "INSERT INTO events (ts, job_id, kind, message) VALUES (?,?,?,?)",
                        (time.time(), None, "backup_restored", текст))
            finally:
                прямо.close()
            return
        except sqlite3.Error as exc:
            log.warning("След о восстановлении не записан: %s", exc)
    try:
        db.add_event(None, "backup_restored", текст)
    except Exception:                                        # noqa: BLE001
        pass


def пора(db: Any, settings: Any, *, ключ: str = "backup_last_at") -> bool:
    """Пришло ли время очередной копии.

    Два способа задать расписание, и они не спорят: заданный интервал в
    часах главнее, потому что он строже; иначе — раз в сутки в назначенный
    час. «Раз в сутки в 00:01» проверяется по местному времени и по тому,
    была ли сегодня копия, а не по «прошло ли 24 часа»: сервер, который
    полчаса постоял выключенным, иначе сдвигал бы время копии каждый день,
    пока оно не уезжало в разгар рабочего дня.
    """
    if not settings.get("backup_enabled", True):
        return False
    часов = _целое(settings, "backup_interval_hours", 0)
    было = 0.0
    try:
        было = float(db.get_kv(ключ, 0) or 0)
    except (TypeError, ValueError):
        было = 0.0
    сейчас = time.time()
    if часов > 0:
        return сейчас - было >= часов * 3600
    час, минута = разобрать_время(settings.get("backup_time"))
    местное = time.localtime(сейчас)
    назначено = time.mktime((местное.tm_year, местное.tm_mon, местное.tm_mday,
                             час, минута, 0, 0, 0, -1))
    if сейчас < назначено:
        # Сегодняшний час ещё не настал — но вчерашний мог быть пропущен.
        # Прежде здесь стоял `return False`, и сервер, простоявший с
        # назначенного часа до полуночи (обновление, отключение питания),
        # оставался без копии за целые сутки: вчерашний заход уже не
        # догонялся, сегодняшний ещё не наступил.
        вчера = time.localtime(сейчас - 86400)
        назначено = time.mktime((вчера.tm_year, вчера.tm_mon, вчера.tm_mday,
                                 час, минута, 0, 0, 0, -1))
    return было < назначено


def разобрать_время(значение: Any, по_умолчанию: tuple[int, int] = (0, 1)) -> tuple[int, int]:
    """«00:01» → (0, 1). Мусор — не повод не делать копию вовсе."""
    текст = str(значение or "").strip()
    if not текст:
        return по_умолчанию
    части = текст.replace(".", ":").split(":")
    try:
        час = int(части[0])
        минута = int(части[1]) if len(части) > 1 else 0
    except (TypeError, ValueError):
        return по_умолчанию
    if not (0 <= час <= 23 and 0 <= минута <= 59):
        return по_умолчанию
    return час, минута


__all__ = ["ВИДЫ", "Копия", "вынести", "зашифровать", "расшифровать",
           "восстановить", "восстановить_из_файла", "каталог",
           "подчистить", "пора", "принять", "разобрать_время", "свод", "создать",
           "список", "удалить"]
