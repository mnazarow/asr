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
    путь = каталог(settings)
    try:
        путь.mkdir(parents=True, exist_ok=True)
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
    """Согласованный снимок базы через «.backup», без остановки сервера."""
    приёмник = sqlite3.connect(str(куда))
    try:
        with приёмник:
            db.conn.backup(приёмник)
    finally:
        приёмник.close()


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
    подчистить(settings, место_под_ещё_одну=True)
    класть_результаты = (bool(settings.get("backup_include_results", False))
                         if include_results is None else bool(include_results))

    начало = time.time()
    состав = ["settings.json"]
    временный = Path(tempfile.mkdtemp(prefix="asrhub-backup-", dir=str(место)))
    цель = место / _имя(kind)
    # Черновик — внутри временного каталога, а не рядом с целью: имя у него
    # тогда уникально по построению, и брошенный при сбое он уходит вместе
    # с каталогом, а не остаётся в списке копий мусором.
    черновик = временный / "архив.part"
    try:
        (временный / "settings.json").write_text(
            json.dumps(_настройки_наружу(settings), ensure_ascii=False, indent=2),
            encoding="utf-8")
        файл_настроек = getattr(settings, "config_path", None)
        if файл_настроек and Path(файл_настроек).is_file():
            shutil.copy2(файл_настроек, временный / "asrhub.yaml")
            состав.append("asrhub.yaml")
        if kind == "full":
            (временный / "db").mkdir()
            _снять_базу(db, временный / "db" / "asrhub.db")
            состав.append("db/asrhub.db")
            if класть_результаты:
                каталог_результатов = Path(settings.paths.results)
                if каталог_результатов.is_dir():
                    shutil.copytree(каталог_результатов, временный / "results",
                                    dirs_exist_ok=True)
                    состав.append("results/")
        опись = _опись(db, settings, kind, comment, состав)
        (временный / ОПИСЬ).write_text(
            json.dumps(опись, ensure_ascii=False, indent=2), encoding="utf-8")

        with tarfile.open(черновик, "w:gz") as архив:
            for путь in sorted(временный.rglob("*")):
                if путь == черновик:
                    continue
                архив.add(путь, arcname=str(путь.relative_to(временный)))
        # Имя выбирается в последний момент и под замком: с точностью до
        # секунды два захода выбрали бы одно.
        with _ЗАМОК:
            цель = место / _имя(kind)
            счётчик = 2
            while цель.exists():
                цель = место / _имя(kind).replace(РАСШИРЕНИЕ, f"-{счётчик}{РАСШИРЕНИЕ}")
                счётчик += 1
            черновик.replace(цель)
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
    return _описание(цель).to_dict()


# ---------------------------------------------------------------------------
# Что лежит в каталоге
# ---------------------------------------------------------------------------

def _прочитать_опись(путь: Path) -> dict[str, Any]:
    with tarfile.open(путь, "r:gz") as архив:
        try:
            запись = архив.getmember(ОПИСЬ)
        except KeyError:
            return {}
        поток = архив.extractfile(запись)
        if поток is None:
            return {}
        return json.loads(поток.read().decode("utf-8"))


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
        опись = _прочитать_опись(путь)
    except Exception as exc:                                 # noqa: BLE001
        # Ловим всё: gzip на оборванном файле кидает EOFError, на порче в
        # середине — zlib.error, и ни то, ни другое не наследуется от
        # TarError. Один недописанный файл (падение в момент копии,
        # оборванный перенос на сетевое хранилище) делал весь раздел
        # неоткрываемым, а убрать битую копию через интерфейс было нельзя:
        # имя берётся из списка, а списка нет.
        копия.error = f"опись не читается: {exc}"
        return копия
    копия.kind = str(опись.get("kind") or "full")
    копия.created_at = float(опись.get("created_at") or копия.created_at)
    копия.version = str(опись.get("version") or "")
    копия.schema_version = int(опись.get("schema_version") or 0)
    копия.instance = str(опись.get("instance") or "")
    копия.comment = str(опись.get("comment") or "")
    копия.contents = list(опись.get("contents") or [])
    копия.calls = int(опись.get("calls") or 0)
    копия.jobs = int(опись.get("jobs") or 0)
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
        "kinds": [{"key": к, "title": з} for к, з in ВИДЫ.items()],
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


def подчистить(settings: Any, *, место_под_ещё_одну: bool = False) -> list[str]:
    """Убирает копии старше срока и сверх числа. Возвращает, что убрала.

    Подчистка идёт ДО снятия новой копии, а не после: каталог копий обычно
    лежит на том же диске, что и база, и если места хватает ровно на
    предельное число копий, очередная не помещается — а удаляет старые
    строка, до которой дело уже не доходит. С этого мгновения свежих копий
    не появляется вообще никогда, и молча.
    """
    дней = _целое(settings, "backup_keep_days", 10)
    штук = _целое(settings, "backup_keep", 0)
    место = каталог(settings)
    if not место.is_dir():
        return []
    копии: list[Path] = []
    for образец in (f"*{РАСШИРЕНИЕ}", "asrhub-*.db"):
        копии.extend(место.glob(образец))
    try:
        копии.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []

    убрать: set[Path] = set()
    if дней > 0:
        граница = time.time() - дней * 86400
        убрать.update(п for п in копии if п.stat().st_mtime < граница)
    if штук > 0:
        предел = max(0, штук - 1) if место_под_ещё_одну else штук
        убрать.update(копии[предел:])
    # Последнюю копию не трогаем никогда, каким бы ни был срок: каталог,
    # в котором по правилам не должно остаться ни одной копии, — это не
    # «чисто», а «резервных копий нет».
    if копии and копии[0] in убрать and len(убрать) >= len(копии):
        убрать.discard(копии[0])

    убранные = []
    for путь in sorted(убрать):
        try:
            путь.unlink()
            убранные.append(путь.name)
            log.info("Удалена старая копия: %s", путь.name)
        except OSError as exc:
            log.warning("Копию %s удалить не удалось: %s", путь.name, exc)
    return убранные


# ---------------------------------------------------------------------------
# Восстановление
# ---------------------------------------------------------------------------

def _безопасно_распаковать(архив: tarfile.TarFile, куда: Path) -> None:
    """Распаковка с проверкой каждого имени и общего размера.

    Архив резервной копии приходит извне ровно так же, как любой другой
    файл: его могли принести с чужого сервера или подменить. Имя внутри
    архива вида «../../etc/cron.d/x» кладёт файл мимо каталога — это
    известная дыра всех распаковщиков; сжатые нули, разворачивающиеся в
    терабайт, забивают диск за секунды.
    """
    корень = куда.resolve()
    всего = 0
    for запись in архив.getmembers():
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
        всего += max(0, int(запись.size or 0))
        if всего > ПРЕДЕЛ_РАСПАКОВКИ:
            raise ConfigError(
                "Копия слишком велика — распаковка отменена.",
                hint=f"Предел {ПРЕДЕЛ_РАСПАКОВКИ // 1024 ** 3} ГБ; "
                     "проверьте, что это действительно резервная копия.")
    # filter="data" — штатная защита Python: снимает права, владельцев и
    # устройства. Проверка выше от неё не избавляет: она про содержимое, эта
    # про имена, и падать нужно до того, как на диск ляжет первый файл.
    архив.extractall(куда, filter="data")


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
        _записать_событие(db, f"Восстановлена база из {путь.name}",
                          цель=Path(settings.paths.data) / "asrhub.db")
        return итог

    временный = Path(tempfile.mkdtemp(prefix="asrhub-restore-"))
    try:
        try:
            with tarfile.open(путь, "r:gz") as архив:
                _безопасно_распаковать(архив, временный)
        except tarfile.TarError as exc:
            raise ConfigError(
                f"Копия «{путь.name}» не читается: {exc}",
                hint="Файл повреждён или это не резервная копия ASR Hub.") from exc

        опись_файл = временный / ОПИСЬ
        опись = json.loads(опись_файл.read_text(encoding="utf-8")) if опись_файл.is_file() else {}
        if int(опись.get("format") or ФОРМАТ) > ФОРМАТ:
            raise ConfigError(
                f"Копия снята более новой версией сервера (формат {опись.get('format')}).",
                hint=f"Этот сервер понимает формат {ФОРМАТ}. Обновите ASR Hub.")
        итог["from_version"] = str(опись.get("version") or "")
        итог["created_at"] = float(опись.get("created_at") or 0)

        # Проверяем всё, что может отказать, ДО первой записи. Иначе
        # «восстановление не удалось» оставляло настройки уже подменёнными
        # и записанными на диск: администратор видел ошибку и был уверен,
        # что ничего не произошло.
        файл_базы = временный / "db" / "asrhub.db"
        if what == "full" and not файл_базы.is_file():
            raise ConfigError(
                f"В копии «{путь.name}» нет базы: это копия только настроек.",
                hint="Данные можно вернуть из копии вида «настройки и данные».")
        if apply_settings:
            итог["applied"] = _вернуть_настройки(временный / "settings.json", settings)
        if what == "full":
            итог.update(_вернуть_базу(файл_базы, settings))
            результаты = временный / "results"
            if результаты.is_dir():
                shutil.copytree(результаты, Path(settings.paths.results),
                                dirs_exist_ok=True)
                итог["results"] = True
    finally:
        shutil.rmtree(временный, ignore_errors=True)

    что = ("настройки" if what == "settings" else "настройки и данные")
    _записать_событие(db, f"Восстановлено ({что}) из {путь.name}: "
                          f"параметров {итог['applied']}",
                      цель=(Path(settings.paths.data) / "asrhub.db"
                            if итог.get("database") else None))
    log.info("Восстановление из %s: %s, параметров %d, база %s",
             путь.name, что, итог["applied"], "да" if итог["database"] else "нет")
    return итог


def _вернуть_настройки(файл: Path, settings: Any) -> int:
    """Применяет значения из копии. Возвращает, сколько параметров легло.

    Параметры, которых нет в каталоге этой версии, пропускаются молча: в
    копии со старого сервера они есть, а смысла у них здесь нет, и падать
    из-за них — значит сделать копию бесполезной после каждого обновления.
    """
    from .catalog import PARAMS_BY_KEY, validate_all  # noqa: PLC0415

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
    свои = {к: з for к, з in значения.items() if к in PARAMS_BY_KEY}
    # Проверяем ДО применения и целиком: наполовину применённая копия
    # оставляет сервер в состоянии, которого не было ни до, ни после.
    ошибки = validate_all(свои)
    if ошибки:
        raise ConfigError(
            "Настройки в копии не проходят проверку: " + "; ".join(ошибки[:5]),
            hint="Копия снята другой версией сервера или изменена вручную.")
    for ключ, значение in свои.items():
        settings.set(ключ, значение, source="restore")
    try:
        settings.save()
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Настройки применены, но в файл не записаны: %s", exc)
    return len(свои)


def _вернуть_базу(файл: Path, settings: Any) -> dict[str, Any]:
    """Подменяет файл базы. Прежний сохраняется рядом.

    Целостность проверяется ДО того, как трогать рабочую базу:
    восстановление из битого файла, обнаруженное после подмены, оставляет
    человека вообще без базы — с двумя нерабочими файлами вместо одного.
    """
    try:
        с_копией = sqlite3.connect(f"file:{файл}?mode=ro", uri=True)
        try:
            итог = с_копией.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            с_копией.close()
    except sqlite3.DatabaseError as exc:
        raise ConfigError(
            f"Файл в копии не похож на базу ASR Hub: {exc}",
            hint="Возьмите другую копию.") from exc
    if итог != "ok":
        raise ConfigError(f"База в копии повреждена: {итог}",
                          hint="Восстановление отменено, рабочая база не тронута.")

    цель = Path(settings.paths.data) / "asrhub.db"
    метка = time.strftime("%Y%m%d-%H%M%S")
    сохранённая = ""
    try:
        if цель.exists():
            запасная = цель.with_name(f"{цель.name}.before-restore-{метка}")
            цель.replace(запасная)
            сохранённая = запасная.name
        for хвост in ("-wal", "-shm"):
            спутник = Path(str(цель) + хвост)
            if спутник.exists():
                # Спутники прежней базы обязаны уйти вместе с ней: SQLite
                # достроит по ним состояние, которого в копии нет.
                спутник.replace(Path(f"{спутник}.before-restore-{метка}"))
        shutil.copy2(файл, цель)
    except OSError as exc:
        # Рабочая база уже отодвинута, а новая не легла: на её месте либо
        # ничего, либо обрезок. Молчать об этом нельзя — человек обязан
        # знать, как называется его настоящая база, иначе после перезапуска
        # он получит пустую и решит, что потерял всё.
        цель.unlink(missing_ok=True)
        возврат = ""
        if сохранённая:
            try:
                (цель.with_name(сохранённая)).replace(цель)
                возврат = " Рабочая база возвращена на место."
            except OSError:
                возврат = (f" Рабочая база лежит рядом под именем «{сохранённая}» — "
                           "верните её этим именем вручную.")
        raise StorageError(
            f"База не восстановлена: {exc}.{возврат}",
            hint="Проверьте свободное место и права на каталог данных.") from exc
    return {"database": True, "restart_required": True, "previous": сохранённая}


def принять(settings: Any, имя_файла: str, поток: Any) -> dict[str, Any]:
    """Кладёт присланный архив в каталог копий — восстановление с другой машины.

    Файл сначала пишется во временный, потом проверяется опись и только
    потом переименовывается: архив, который не является копией, не должен
    попадать в список — иначе его однажды выберут для восстановления.
    """
    место = _готовый_каталог(settings)
    короткое = os.path.basename(str(имя_файла or "").strip())
    if not короткое.endswith(РАСШИРЕНИЕ) and not короткое.endswith(".db"):
        raise ConfigError(
            f"«{короткое}» не похоже на резервную копию ASR Hub.",
            hint=f"Принимаются файлы *{РАСШИРЕНИЕ} и копии базы *.db.")
    цель = место / короткое
    if цель.exists():
        основа = короткое[:-len(РАСШИРЕНИЕ)] if короткое.endswith(РАСШИРЕНИЕ) else короткое[:-3]
        хвост = РАСШИРЕНИЕ if короткое.endswith(РАСШИРЕНИЕ) else ".db"
        цель = место / f"{основа}-{time.strftime('%Y%m%d-%H%M%S')}{хвост}"
    черновик = цель.with_suffix(".part")
    записано = 0
    try:
        with черновик.open("wb") as файл:
            while True:
                кусок = поток.read(1024 * 1024)
                if not кусок:
                    break
                записано += len(кусок)
                if записано > ПРЕДЕЛ_РАСПАКОВКИ:
                    raise ConfigError("Файл слишком велик.",
                                      hint="Это точно резервная копия?")
                файл.write(кусок)
        if цель.name.endswith(РАСШИРЕНИЕ):
            опись = _прочитать_опись(черновик)
            if not опись.get("kind"):
                raise ConfigError(
                    "В архиве нет описи — это не резервная копия ASR Hub.",
                    hint="Копии сервер собирает сам; чужие архивы не подойдут.")
        черновик.replace(цель)
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
        return False
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


__all__ = ["ВИДЫ", "Копия", "восстановить", "каталог", "подчистить", "пора",
           "принять", "разобрать_время", "свод", "создать", "список", "удалить"]
