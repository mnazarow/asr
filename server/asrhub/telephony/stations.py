"""Список телефонных станций: одна настройка на все АТС сразу.

Организация редко живёт на одной станции. Головной офис на Asterisk,
филиал на своей, колл-центр на третьей, а в архиве ещё папка записей от
станции, которую давно отключили, но разговоры из неё нужны. Пока
настройка была одна на сервер, второй станции просто не существовало.

Здесь — описание одной станции (`Станция`) и разбор списка из настройки
(`список`). Всё, что раньше лежало в отдельных ключах `telephony_*`,
теперь поле станции: источник, учётные данные, каталог записей, набор
длин внутренних номеров, правила контекстов, владелец, приоритет.

Старая настройка из одного набора ключей продолжает работать: если списка
станций нет, а `telephony_source` задан, из него собирается станция с
именем «АТС». Сервер, обновившийся с прошлой версии, не должен замечать
подмены.
"""
from __future__ import annotations

import functools
import hashlib
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .asterisk import длины_внутренних, правила_контекстов

log = get_logger("telephony")

#: Смещение от всемирного времени, записанное числом: «+03:00», «UTC+3»,
#: «GMT-05:30». Нужно там, где базы часовых поясов нет (Windows без пакета
#: tzdata, урезанный образ контейнера), и тем, кто привык так писать.
_СМЕЩЕНИЕ = re.compile(r"^(?:UTC|GMT)?\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


@functools.lru_cache(maxsize=64)
def разобрать_пояс(имя: str) -> tzinfo | None:
    """Часовой пояс станции по его записи; None — пояс самого сервера.

    Asterisk пишет время звонка в журнал и в событие AMI по местным часам
    станции, без пояса. Сервер раньше читал его по своим часам — и сервер в
    контейнере (там всемирное время) сдвигал каждый звонок московской
    станции на три часа: выдержка откладывала каждый звонок «ещё пишется»
    на три часа, запись по номерам не находилась в окне поиска, а отчёты и
    тепловые карты съезжали. То же с филиалом в другом поясе.

    Понимает имя из базы поясов («Europe/Moscow», «Asia/Yekaterinburg»),
    «UTC» и смещение числом («+03:00», «UTC+5»). Неизвестное — ValueError
    с объяснением: молча читать время по часам сервера значит вернуть ровно
    ту ошибку, от которой настройка и заведена.
    """
    запись = str(имя or "").strip()
    if not запись:
        return None
    if запись.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    смещение = _СМЕЩЕНИЕ.match(запись)
    if смещение:
        знак, часы, минуты = смещение.groups()
        сдвиг = timedelta(hours=int(часы), minutes=int(минуты or 0))
        if сдвиг > timedelta(hours=14):
            raise ValueError(f"смещение «{запись}» больше четырнадцати часов")
        return timezone(-сдвиг if знак == "-" else сдвиг)
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415
    except ImportError as exc:                              # pragma: no cover
        raise ValueError("в этой сборке Python нет часовых поясов — укажите "
                         "смещение числом, например +03:00") from exc
    try:
        return ZoneInfo(запись)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"часовой пояс «{запись}» не найден — нужно имя из базы поясов "
            "(Europe/Moscow, Asia/Novosibirsk) или смещение числом (+03:00); "
            "если не находится ни одно имя, на сервере нет базы поясов: "
            "поставьте пакет tzdata") from exc

#: Источники, которые умеет читать сервер.
ИСТОЧНИКИ = ("cdr_csv", "ami", "folder")

#: Поля станции, которые нельзя показывать не администратору: по ним
#: строится вход в телефонию организации, а не понимание своей работы.
ЗАКРЫТЫЕ = ("host", "port", "username", "secret", "cdr_file",
            "recordings_dir", "filename")

#: Поля-секреты: не показываются никому и в ответах заменяются пометкой.
СЕКРЕТНЫЕ = ("secret",)


#: Кириллица в латиницу — чтобы идентификатор станции читался человеком.
#: «Головной офис» должен стать `golovnoy-ofis`, а не отпечатком: этот
#: идентификатор попадает в метки заданий и в адреса ручек, и по нему
#: человек должен узнавать свою станцию.
_ТРАНСЛИТ = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _идентификатор(имя: str, запасной: str) -> str:
    """Устойчивый идентификатор станции из её имени.

    Устойчивый — потому что к нему привязаны звонки в архиве. Переименовать
    станцию можно, но идентификатор при этом не меняется: иначе весь её
    архив в одно мгновение стал бы «ничей». Поэтому он вычисляется один раз
    при заведении и дальше хранится в настройке как есть.
    """
    переведено = "".join(_ТРАНСЛИТ.get(з, з) for з in (имя or "").lower())
    основа = re.sub(r"[^0-9a-z]+", "-", переведено).strip("-")
    if основа:
        return основа[:32]
    # Имя целиком из знаков, которым в идентификаторе не место (скажем,
    # иероглифы или одни цифры со скобками) — берём отпечаток: он короткий
    # и не меняется.
    return "pbx-" + hashlib.sha256((имя or запасной).encode("utf-8")).hexdigest()[:8]


@dataclass(eq=True)
class Станция:
    """Одна телефонная станция со всем, что нужно, чтобы забрать с неё записи."""

    id: str = ""
    name: str = "АТС"
    enabled: bool = True
    source: str = "cdr_csv"

    # Интерфейс управления
    host: str = "127.0.0.1"
    port: int = 5038
    username: str = ""
    secret: str = ""

    # Файлы
    cdr_file: str = ""
    recordings_dir: str = ""
    filename: str = ""

    # Как читать звонок
    internal_digits: Any = 5
    contexts: Any = field(default_factory=dict)
    min_duration_s: int = 10
    skip_unanswered: bool = True
    settle_s: int = 30
    lookback_days: int = 7
    match_window: int = 120

    # Куда класть
    owner: str = "telephony"
    owner_map: dict[str, str] = field(default_factory=dict)
    priority: int = 40
    tags: str = ""

    # Как часто ходить
    poll_s: int = 60

    #: Часовой пояс, в котором станция пишет время звонков. Пусто — пояс
    #: сервера (так было всегда, и для АТС на той же машине это верно).
    timezone: str = ""

    @property
    def пояс(self) -> tzinfo | None:
        """Пояс станции для разбора времени; None — пояс сервера.

        Негодная запись здесь не роняет забор: её ловит проверка при
        сохранении, а на случай настройки, написанной руками, — журнал и
        пояс сервера, то есть прежнее поведение.
        """
        try:
            return разобрать_пояс(self.timezone)
        except ValueError as exc:
            log.warning("Станция «%s»: %s — время читается по часам сервера",
                        self.name, exc)
            return None

    @property
    def длины(self) -> set[int]:
        return длины_внутренних(self.internal_digits) or {5}

    @property
    def правила(self) -> list[tuple[str, str]]:
        return правила_контекстов(self.contexts)

    def путь(self, поле: str) -> Path | None:
        значение = str(getattr(self, поле, "") or "").strip()
        return Path(значение) if значение else None

    def to_dict(self, *, for_admin: bool = False) -> dict[str, Any]:
        """Станция наружу. Не администратору — без адресов и путей."""
        данные = {
            "id": self.id, "name": self.name, "enabled": self.enabled,
            "source": self.source, "owner": self.owner, "priority": self.priority,
            "internal_digits": sorted(self.длины),
            "contexts": [{"context": к, "direction": з} for к, з in self.правила],
            "min_duration_s": self.min_duration_s,
            "skip_unanswered": self.skip_unanswered,
            "settle_s": self.settle_s, "lookback_days": self.lookback_days,
            "match_window": self.match_window,
            "poll_s": self.poll_s, "tags": self.tags,
            "timezone": self.timezone,
        }
        if for_admin:
            данные.update({
                "host": self.host, "port": self.port, "username": self.username,
                "secret": "***" if self.secret else "",
                "cdr_file": self.cdr_file, "recordings_dir": self.recordings_dir,
                "filename": self.filename, "owner_map": dict(self.owner_map),
            })
        return данные


def _станция_из(данные: dict[str, Any], номер: int) -> Станция:
    имя = str(данные.get("name") or данные.get("имя") or f"АТС {номер + 1}").strip()
    источник = str(данные.get("source") or "cdr_csv").strip()
    if источник not in ИСТОЧНИКИ:
        источник = "cdr_csv"

    def число(ключ: str, по_умолчанию: int) -> int:
        значение = данные.get(ключ)
        if значение is None or значение == "":
            return по_умолчанию
        try:
            return int(значение)
        except (TypeError, ValueError):
            return по_умолчанию

    станция = Станция(
        id=str(данные.get("id") or "").strip() or _идентификатор(имя, str(номер)),
        name=имя or f"АТС {номер + 1}",
        enabled=bool(данные.get("enabled", True)),
        source=источник,
        host=str(данные.get("host") or "127.0.0.1").strip(),
        port=число("port", 5038),
        username=str(данные.get("username") or "").strip(),
        secret=str(данные.get("secret") or ""),
        cdr_file=str(данные.get("cdr_file") or "").strip(),
        recordings_dir=str(данные.get("recordings_dir") or "").strip(),
        filename=str(данные.get("filename") or "").strip(),
        internal_digits=данные.get("internal_digits", 5),
        contexts=данные.get("contexts") or {},
        min_duration_s=число("min_duration_s", 10),
        skip_unanswered=bool(данные.get("skip_unanswered", True)),
        settle_s=число("settle_s", 30),
        lookback_days=число("lookback_days", 7),
        match_window=число("match_window", 120),
        owner=str(данные.get("owner") or "telephony").strip(),
        owner_map=dict(данные.get("owner_map") or {}),
        priority=число("priority", 40),
        tags=str(данные.get("tags") or "").strip(),
        poll_s=число("poll_s", 60),
        timezone=str(данные.get("timezone") or данные.get("tz") or "").strip(),
    )
    return станция


def _из_старых_ключей(settings: Any) -> list[dict[str, Any]]:
    """Одна станция из настроек прежней версии — чтобы обновление не заметили.

    Сервер, обновившийся с версии, где АТС была одна, продолжает работать
    ровно как работал: старые ключи `telephony_*` собираются в станцию с
    именем «АТС». Как только в списке появится хоть одна станция, старые
    ключи перестают учитываться — иначе одна и та же станция читалась бы
    дважды.
    """
    # Важно: старые ключи учитываются, только если их кто-то ЗАДАЛ. У
    # `telephony_source` есть значение по умолчанию, и проверка «оно не
    # пустое» срабатывала на свежем сервере, где телефонию не настраивали
    # вовсе: в разделе «АТС» появлялась несуществующая станция «АТС»,
    # которая ходила по пути из умолчаний и молча ничего не находила.
    откуда = getattr(settings, "sources", None) or {}
    задано = any(str(откуда.get(ключ) or "default") != "default"
                 for ключ in ("telephony_source", "telephony_cdr_file",
                              "telephony_recordings_dir", "telephony_host",
                              "telephony_username", "telephony_enabled"))
    if not задано or not str(settings.get("telephony_source") or "").strip():
        return []
    return [{
        "id": "pbx", "name": "АТС", "enabled": True,
        "source": settings.get("telephony_source"),
        "host": settings.get("telephony_host"),
        "port": settings.get("telephony_port"),
        "username": settings.get("telephony_username"),
        "secret": settings.get("telephony_secret"),
        "cdr_file": settings.get("telephony_cdr_file"),
        "recordings_dir": settings.get("telephony_recordings_dir"),
        "filename": settings.get("telephony_filename"),
        "internal_digits": settings.get("telephony_internal_digits"),
        "contexts": settings.get("telephony_contexts"),
        "min_duration_s": settings.get("telephony_min_duration_s"),
        "skip_unanswered": settings.get("telephony_skip_unanswered", True),
        "settle_s": settings.get("telephony_settle_s"),
        "lookback_days": settings.get("telephony_lookback_days"),
        "match_window": settings.get("telephony_match_window"),
        "owner": settings.get("telephony_owner"),
        "owner_map": settings.get("telephony_owner_map"),
        "priority": settings.get("telephony_priority"),
        "poll_s": settings.get("telephony_poll_s"),
    }]


def список(settings: Any, *, только_включённые: bool = False) -> list[Станция]:
    """Станции из настроек. Пустой список — телефония не настроена."""
    сырые = settings.get("telephony_stations") or []
    if isinstance(сырые, dict):                  # {"id": {...}} — тоже примем
        сырые = [{**значение, "id": ключ} for ключ, значение in сырые.items()]
    if not isinstance(сырые, list) or not сырые:
        сырые = _из_старых_ключей(settings)

    станции: list[Станция] = []
    занятые: set[str] = set()
    for номер, данные in enumerate(сырые):
        if not isinstance(данные, dict):
            continue
        станция = _станция_из(данные, номер)
        # Одинаковый идентификатор — это перепутанные архивы двух станций.
        # Разводим, но говорим об этом в журнал: скорее всего, человек
        # скопировал станцию и забыл поправить имя.
        основа = станция.id
        счётчик = 2
        while станция.id in занятые:
            станция.id = f"{основа}-{счётчик}"
            счётчик += 1
        if станция.id != основа:
            log.warning("Две станции с одним идентификатором «%s»; вторая стала «%s»",
                        основа, станция.id)
        занятые.add(станция.id)
        станции.append(станция)
    if только_включённые:
        return [с for с in станции if с.enabled]
    return станции


def найти(settings: Any, station_id: str) -> Станция | None:
    """Станция по идентификатору."""
    нужен = str(station_id or "").strip()
    return next((с for с in список(settings) if с.id == нужен), None)


def проверить_набор(сырые: Any) -> list[str]:
    """Что не так со списком станций — для проверки настройки при сохранении."""
    ошибки: list[str] = []
    if сырые in (None, ""):
        return ошибки
    if not isinstance(сырые, list):
        return ["ожидается список станций"]
    имена: set[str] = set()
    for номер, данные in enumerate(сырые, start=1):
        if not isinstance(данные, dict):
            ошибки.append(f"станция {номер}: ожидается объект")
            continue
        имя = str(данные.get("name") or "").strip()
        if not имя:
            ошибки.append(f"станция {номер}: не задано имя")
        elif имя.lower() in имена:
            ошибки.append(f"станция {номер}: имя «{имя}» уже занято")
        else:
            имена.add(имя.lower())
        источник = str(данные.get("source") or "cdr_csv")
        if источник not in ИСТОЧНИКИ:
            ошибки.append(f"«{имя or номер}»: неизвестный источник «{источник}»")
        if источник == "ami" and not str(данные.get("username") or "").strip():
            ошибки.append(f"«{имя or номер}»: для AMI нужна учётная запись")
        if источник == "cdr_csv" and not str(данные.get("cdr_file") or "").strip():
            ошибки.append(f"«{имя or номер}»: не задан журнал звонков")
        if источник == "folder" and not str(данные.get("recordings_dir") or "").strip():
            ошибки.append(f"«{имя or номер}»: не задан каталог записей")
        for правило in правила_контекстов(данные.get("contexts")):
            if правило[1] not in ("входящий", "исходящий", "внутренний"):
                ошибки.append(f"«{имя or номер}»: направление «{правило[1]}» "
                              "не бывает — только входящий, исходящий, внутренний")
        пояс = str(данные.get("timezone") or данные.get("tz") or "").strip()
        if пояс:
            try:
                разобрать_пояс(пояс)
            except ValueError as exc:
                ошибки.append(f"«{имя or номер}»: {exc}")
    return ошибки


__all__ = ["ЗАКРЫТЫЕ", "ИСТОЧНИКИ", "СЕКРЕТНЫЕ", "Станция", "найти",
           "проверить_набор", "разобрать_пояс", "список"]
