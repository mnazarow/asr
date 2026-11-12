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

#: Откуда сервер узнаёт о звонках станции. Первые три он читает сам;
#: `dialplan` — наоборот, приём: звонки присылает диалплан станции
#: (см. telephony/dialplan.py), а поток станции только дозабирает отложенные.
ИСТОЧНИКИ = ("cdr_csv", "ami", "folder", "dialplan")

#: Поля станции, которые нельзя показывать не администратору: по ним
#: строится вход в телефонию организации, а не понимание своей работы.
ЗАКРЫТЫЕ = ("host", "port", "username", "secret", "cdr_file",
            "recordings_dir", "filename", "tls", "ca_file", "auth", "queue_log",
            "transfers", "cel_file")

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
    #: AMI по TLS (порт 5039, `tlsenable` в manager.conf) и центр, которым
    #: подписан сертификат станции; вход по MD5 через Challenge.
    tls: bool = False
    ca_file: str = ""
    auth: str = "plain"
    #: Журнал очередей Asterisk (`/var/log/asterisk/queue_log`), если сервер
    #: его видит: ожидание, брошенные в очереди, пропуски операторов.
    queue_log: str = ""
    #: События перевода из AMI (`BlindTransfer`, `AttendedTransfer`, класс
    #: `call`): точная граница отрезков участников в разговоре с переводом.
    transfers: bool = True
    #: Журнал CEL с событиями перевода (`cel_custom.conf`, отображение
    #: `transfers.ОТОБРАЖЕНИЕ_CEL`) — для станций без AMI.
    cel_file: str = ""

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
                "tls": self.tls, "ca_file": self.ca_file, "auth": self.auth,
                "queue_log": self.queue_log, "transfers": self.transfers,
                "cel_file": self.cel_file,
                "cdr_file": self.cdr_file, "recordings_dir": self.recordings_dir,
                "filename": self.filename, "owner_map": dict(self.owner_map),
            })
        return данные


def _признак(значение: Any, по_умолчанию: bool) -> bool:
    """Да или нет из того, что записано в настройке.

    `bool("false")` — это True: станция, выключенная строкой в config.yaml
    или в JSON из чужого скрипта, продолжала работать.
    """
    if значение is None or значение == "":
        return по_умолчанию
    if isinstance(значение, str):
        текст = значение.strip().lower()
        if текст in ("0", "false", "no", "off", "нет", "выкл", "выключено"):
            return False
        if текст in ("1", "true", "yes", "on", "да", "вкл", "включено"):
            return True
        return по_умолчанию
    return bool(значение)


def карта_владельцев(значение: Any) -> dict[str, str]:
    """«Номер → владелец» в любом из принятых написаний.

    Объект, строка «101=sales, 102=support» (так же, как контексты) и список
    пар. Всё прочее — пустая карта: раньше `dict("101=sales")` бросал
    ValueError, раздел «АТС» отвечал 500, а после перезапуска сервер не
    поднимался вовсе. Сказать человеку, что запись не годится, — дело
    проверки при сохранении (`проверить_набор`).
    """
    if not значение:
        return {}
    пары: list[tuple[Any, Any]] = []
    if isinstance(значение, dict):
        пары = list(значение.items())
    elif isinstance(значение, str):
        for кусок in re.split(r"[;,\n]+", значение):
            номер, знак, владелец = кусок.partition("=")
            if not знак:
                номер, знак, владелец = кусок.partition(":")
            if знак:
                пары.append((номер, владелец))
    elif isinstance(значение, (list, tuple)):
        for элемент in значение:
            if isinstance(элемент, dict):
                пары.append((элемент.get("number") or элемент.get("номер") or "",
                             элемент.get("owner") or элемент.get("владелец") or ""))
            elif isinstance(элемент, (list, tuple)) and len(элемент) == 2:
                пары.append((элемент[0], элемент[1]))
    итог: dict[str, str] = {}
    for номер, владелец in пары:
        if isinstance(владелец, (dict, list, tuple)):
            continue
        номер, владелец = str(номер or "").strip(), str(владелец or "").strip()
        if номер and владелец:
            итог[номер] = владелец
    return итог


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
        enabled=_признак(данные.get("enabled"), True),
        source=источник,
        host=str(данные.get("host") or "127.0.0.1").strip(),
        port=число("port", 5039 if _признак(данные.get("tls"), False) else 5038),
        username=str(данные.get("username") or "").strip(),
        secret=str(данные.get("secret") or ""),
        tls=_признак(данные.get("tls"), False),
        ca_file=str(данные.get("ca_file") or "").strip(),
        auth="md5" if str(данные.get("auth") or "").strip().lower() == "md5" else "plain",
        queue_log=str(данные.get("queue_log") or "").strip(),
        transfers=_признак(данные.get("transfers"), True),
        cel_file=str(данные.get("cel_file") or "").strip(),
        cdr_file=str(данные.get("cdr_file") or "").strip(),
        recordings_dir=str(данные.get("recordings_dir") or "").strip(),
        filename=str(данные.get("filename") or "").strip(),
        internal_digits=данные.get("internal_digits", 5),
        contexts=данные.get("contexts") or {},
        min_duration_s=число("min_duration_s", 10),
        skip_unanswered=_признак(данные.get("skip_unanswered"), True),
        settle_s=число("settle_s", 30),
        lookback_days=число("lookback_days", 7),
        match_window=число("match_window", 120),
        owner=str(данные.get("owner") or "telephony").strip(),
        owner_map=карта_владельцев(данные.get("owner_map")),
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
    #
    # И только ключи самой станции: общий выключатель `telephony_enabled`
    # станцию не описывает. Раньше галочка «Забирать записи с АТС»,
    # поставленная до заведения станций, порождала рабочую станцию «АТС»
    # с путями по умолчанию, а первое добавление станции записывало в список
    # обе — и каждый звонок ложился в архив дважды.
    откуда = getattr(settings, "sources", None) or {}
    задано = any(str(откуда.get(ключ) or "default") != "default"
                 for ключ in ("telephony_source", "telephony_cdr_file",
                              "telephony_recordings_dir", "telephony_host",
                              "telephony_username"))
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


def список_задан(settings: Any) -> bool:
    """Задан ли список станций явно — хотя бы пустым.

    Пустой список, записанный разделом «АТС» после удаления последней
    станции, — это «станций нет», а не «настройка прежней версии»: иначе
    удалённая станция из старых ключей воскресала, и удаление отвечало 200,
    оставляя её на месте.
    """
    откуда = getattr(settings, "sources", None) or {}
    return str(откуда.get("telephony_stations") or "default") != "default"


def список(settings: Any, *, только_включённые: bool = False) -> list[Станция]:
    """Станции из настроек. Пустой список — телефония не настроена.

    Станция, которую не удалось разобрать, пропускается с записью в журнал,
    а не роняет всё: одна испорченная запись в настройке (написанная руками
    или пришедшая до проверки типов) делала весь раздел «АТС» ответом 500, а
    сервер — неподнимаемым.
    """
    сырые = settings.get("telephony_stations") or []
    if isinstance(сырые, dict):                  # {"id": {...}} — тоже примем
        сырые = [{**значение, "id": ключ} for ключ, значение in сырые.items()
                 if isinstance(значение, dict)]
    if not isinstance(сырые, list) or not сырые:
        сырые = [] if isinstance(сырые, list) and список_задан(settings) \
            else _из_старых_ключей(settings)

    станции: list[Станция] = []
    занятые: set[str] = set()
    for номер, данные in enumerate(сырые):
        if not isinstance(данные, dict):
            continue
        try:
            станция = _станция_из(данные, номер)
        except Exception as exc:                             # noqa: BLE001
            log.error("Станция №%d в настройке не разобрана (%s) — пропущена",
                      номер + 1, exc)
            continue
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


#: Поля-числа станции и их допустимые границы.
_ЧИСЛА = {"port": (1, 65535), "poll_s": (5, 86400), "settle_s": (0, 86400),
          "min_duration_s": (0, 86400), "lookback_days": (0, 3650),
          "match_window": (0, 10080), "priority": (0, 100)}


def проверить_набор(сырые: Any) -> list[str]:
    """Что не так со списком станций — для проверки настройки при сохранении.

    Проверяются и типы полей, а не только их наличие: `owner_map` строкой,
    которая не разбирается в пары, или порт словом раньше проходили, а
    падали уже при разборе станции — после того как настройка применена и
    записана в config.yaml (раздел «АТС» отвечал 500, перезапуск не
    поднимал сервер).
    """
    ошибки: list[str] = []
    if сырые in (None, ""):
        return ошибки
    if isinstance(сырые, dict):                  # {"id": {...}} — как в `список`
        сырые = [{**значение, "id": ключ} if isinstance(значение, dict) else значение
                 for ключ, значение in сырые.items()]
    if not isinstance(сырые, list):
        return ["ожидается список станций"]
    имена: set[str] = set()
    журналы: dict[str, str] = {}
    каталоги: dict[str, str] = {}
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
        кто = f"«{имя or номер}»"
        источник = str(данные.get("source") or "cdr_csv")
        if источник not in ИСТОЧНИКИ:
            ошибки.append(f"{кто}: неизвестный источник «{источник}»")
        if источник == "ami" and not str(данные.get("username") or "").strip():
            ошибки.append(f"{кто}: для AMI нужна учётная запись")
        if источник == "cdr_csv" and not str(данные.get("cdr_file") or "").strip():
            ошибки.append(f"{кто}: не задан журнал звонков")
        if источник == "folder" and not str(данные.get("recordings_dir") or "").strip():
            ошибки.append(f"{кто}: не задан каталог записей")
        способ = str(данные.get("auth") or "plain").strip().lower()
        if способ not in ("plain", "md5"):
            ошибки.append(f"{кто}: способ входа в AMI — plain или md5, а не «{способ}»")
        файл_центра = str(данные.get("ca_file") or "").strip()
        if файл_центра and not Path(файл_центра).is_absolute():
            ошибки.append(f"{кто}: центр сертификации AMI — абсолютный путь к файлу")
        for поле in ("host", "username", "secret"):
            # Перевод строки в учётных данных AMI дописывал к входу второе
            # действие — в обход списка разрешённых (см. `AMIClient.action`).
            if any(знак in str(данные.get(поле) or "") for знак in "\r\n"):
                ошибки.append(f"{кто}: в поле {поле} перевод строки")
        for поле, (низ, верх) in _ЧИСЛА.items():
            значение = данные.get(поле)
            if значение is None or значение == "":
                continue
            try:
                if isinstance(значение, bool):
                    raise ValueError
                число = int(float(str(значение).strip()))
            except (TypeError, ValueError):
                ошибки.append(f"{кто}: {поле} — ожидается число, получено «{значение}»")
                continue
            if not низ <= число <= верх:
                ошибки.append(f"{кто}: {поле} — от {низ} до {верх}, получено {число}")
        ошибки.extend(_ошибки_типов(данные, кто))
        карта = данные.get("owner_map")
        if isinstance(карта, dict) and any(
                isinstance(з, (dict, list, tuple)) for з in карта.values()):
            ошибки.append(f"{кто}: owner_map — владелец задаётся строкой")
        for правило in правила_контекстов(данные.get("contexts")):
            if правило[1] not in ("входящий", "исходящий", "внутренний"):
                ошибки.append(f"{кто}: направление «{правило[1]}» "
                              "не бывает — только входящий, исходящий, внутренний")
        пояс = str(данные.get("timezone") or данные.get("tz") or "").strip()
        if пояс:
            try:
                разобрать_пояс(пояс)
            except ValueError as exc:
                ошибки.append(f"{кто}: {exc}")
        # Две включённые станции на одном журнале или одном каталоге — один и
        # тот же звонок в архиве дважды (под двумя станциями) и два задания на
        # одну запись. Выключенная — архивная копия, ей можно.
        if not _признак(данные.get("enabled"), True):
            continue
        if источник == "cdr_csv":
            журнал = str(данные.get("cdr_file") or "").strip()
            if журнал:
                ключ = _нормальный_путь(журнал)
                if ключ in журналы:
                    ошибки.append(f"{кто}: журнал {журнал} уже читает станция "
                                  f"«{журналы[ключ]}»")
                else:
                    журналы[ключ] = имя or str(номер)
        if источник == "folder":
            каталог = str(данные.get("recordings_dir") or "").strip()
            if каталог:
                ключ = _нормальный_путь(каталог)
                if ключ in каталоги:
                    ошибки.append(f"{кто}: каталог {каталог} уже обходит станция "
                                  f"«{каталоги[ключ]}»")
                else:
                    каталоги[ключ] = имя or str(номер)
    return ошибки


def _ошибки_типов(данные: dict[str, Any], кто: str) -> list[str]:
    """Поля, которые не годятся ни в каком виде: разбор их раньше падал.

    `owner_map`, который не разбирается в пары, и `contexts` числом роняли
    раздел «АТС» ответом 500, а запуск сервера — исключением.
    """
    ошибки = []
    карта = данные.get("owner_map")
    if карта not in (None, "", {}, []):
        if not isinstance(карта, (dict, str, list, tuple)) or not карта_владельцев(карта):
            ошибки.append(
                f"{кто}: owner_map — ожидается объект «номер → владелец» "
                "или строка «101=sales, 102=support»")
    контексты = данные.get("contexts")
    if контексты not in (None, "", {}, []) and not isinstance(
            контексты, (dict, str, list, tuple)):
        ошибки.append(f"{кто}: contexts — ожидается объект или строка "
                      "«from-trunk=входящий, from-internal=исходящий»")
    return ошибки


def проверить_типы(сырые: Any) -> list[str]:
    """Только то, что в станции не годится ни в каком виде (`_ошибки_типов`).

    Эту проверку зовёт каталог параметров — и при правке через PUT
    /api/settings, и при чтении config.yaml на запуске, где негодное
    значение заменяется умолчанием, то есть пустым списком станций.
    Поэтому сюда не входит то, что прежние версии принимали (одинаковый
    журнал у двух станций, число вне границ, пояс, направление словом):
    иначе одна такая станция после обновления стёрла бы весь список. Это
    проверяет раздел «АТС» при сохранении (`проверить_набор`).
    """
    if isinstance(сырые, dict):
        сырые = list(сырые.values())
    if not isinstance(сырые, list):
        return []
    ошибки: list[str] = []
    for номер, данные in enumerate(сырые, start=1):
        if isinstance(данные, dict):
            ошибки.extend(_ошибки_типов(
                данные, f"«{str(данные.get('name') or '').strip() or номер}»"))
    return ошибки


def _нормальный_путь(путь: str) -> str:
    """Путь для сравнения: «/var/log/x/» и «/var/log//x» — один и тот же."""
    import os  # noqa: PLC0415

    return os.path.normpath(путь.strip())


__all__ = ["ЗАКРЫТЫЕ", "ИСТОЧНИКИ", "СЕКРЕТНЫЕ", "Станция", "карта_владельцев", "найти",
           "проверить_набор", "проверить_типы", "разобрать_пояс", "список", "список_задан"]
