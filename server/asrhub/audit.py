"""Журнал доступа: кто, когда и что сделал.

Зачем он нужен отдельно от журнала сервера. В журнале сервера записано, что
пришёл POST на `/api/jobs/8f2…/delete` и вернул 200. Ответить по нему на
вопрос «кто удалил запись разговора с клиентом» нельзя: там нет ни имени
человека, ни того, что за объект скрывался за идентификатором. А спрашивают
именно это — и спрашивают обычно те, кто проводит проверку, а не те, кто
чинит сервер.

Поэтому здесь ведётся второй журнал, устроенный от человека: строка на
действие, с именем учётной записи, ролью, адресом, откуда пришли, и
названием действия по-русски. Он не заменяет журнал сервера и ничего о нём
не знает.

Чего в нём нет умышленно:

* **Содержания запроса.** Тело POST может нести расшифровку разговора,
  пароль или персональные данные; журнал, который их сохраняет, сам
  становится тем, что нужно охранять. Записывается, ЧТО сделали, а не с
  какими словами.
* **Чтений подряд.** Раздел аналитики делает десятки GET-запросов на один
  взгляд человека, и запись каждого превращает журнал в поток, в котором
  ничего не найти. По умолчанию пишутся изменения; чтения — только те, что
  выдают наружу саму запись разговора или выгрузку, и только если это
  включено настройкой.
"""
from __future__ import annotations

import functools
import ipaddress
import re
from typing import Any

#: Что считается чтением, за которым стоит следить: выдача самой записи,
#: расшифровки или выгрузки. Обычный просмотр списков и графиков сюда не
#: входит — он ничего не выносит за пределы сервера.
ЧУВСТВИТЕЛЬНОЕ_ЧТЕНИЕ = (
    re.compile(r"^/api/jobs/[^/]+/(media|download|audio)"),
    re.compile(r"^/api/jobs/[^/]+/segments"),
    re.compile(r"^/api/jobs/[^/]+/text"),
    re.compile(r"^/api/(analytics|content|trends|employees)/export"),
    re.compile(r"^/api/export"),
    re.compile(r"^/api/backup/[^/]+/download"),
    re.compile(r"^/api/telephony/calls/[^/]+/recording"),
)

#: Адрес → название объекта по-русски. Порядок важен: первое совпадение и
#: выигрывает, поэтому частное стоит выше общего.
ОБЪЕКТЫ: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/api/auth/login"), "вход"),
    (re.compile(r"^/api/auth/logout"), "выход"),
    (re.compile(r"^/api/auth/password"), "свой пароль"),
    (re.compile(r"^/api/users"), "учётные записи"),
    (re.compile(r"^/api/keys"), "ключи доступа"),
    (re.compile(r"^/api/settings"), "настройки"),
    (re.compile(r"^/api/jobs/[^/]+/redact"), "редакция записи"),
    (re.compile(r"^/api/jobs/[^/]+/erase"), "удаление по требованию"),
    (re.compile(r"^/api/jobs"), "записи"),
    (re.compile(r"^/api/models"), "модели"),
    (re.compile(r"^/api/telephony/stations"), "станции АТС"),
    (re.compile(r"^/api/telephony"), "телефония"),
    (re.compile(r"^/api/employees"), "сотрудники"),
    (re.compile(r"^/api/content"), "аналитика записей"),
    (re.compile(r"^/api/analytics"), "аналитика"),
    (re.compile(r"^/api/trends"), "тренды"),
    (re.compile(r"^/api/review"), "ручная проверка"),
    (re.compile(r"^/api/llm"), "языковая модель"),
    (re.compile(r"^/api/backup"), "резервные копии"),
    (re.compile(r"^/api/monitoring"), "мониторинг"),
    (re.compile(r"^/api/agent"), "агент на станции"),
    (re.compile(r"^/api/system"), "система"),
    (re.compile(r"^/api/audit"), "журнал доступа"),
)

#: Метод → глагол. Для чтения глагол зависит от адреса, поэтому его здесь нет.
ГЛАГОЛЫ = {"POST": "изменил", "PUT": "изменил", "PATCH": "изменил",
           "DELETE": "удалил"}


def чувствительное_чтение(path: str) -> bool:
    """Выносит ли этот GET за пределы сервера саму запись или выгрузку."""
    return any(образец.search(path) for образец in ЧУВСТВИТЕЛЬНОЕ_ЧТЕНИЕ)


def объект(path: str) -> str:
    """Название раздела по-русски — то, что человек ищет глазами."""
    for образец, имя in ОБЪЕКТЫ:
        if образец.search(path):
            return имя
    return path


def описать(method: str, path: str) -> str:
    """Короткое название действия: «удалил записи», «скачал выгрузку»."""
    метод = (method or "").upper()
    что = объект(path)
    if метод == "GET":
        # Отдельные названия у того, за чем чаще всего и приходят проверять.
        if re.search(r"/(media|download|audio|recording)", path):
            return f"скачал: {что}"
        if "/export" in path:
            return f"выгрузил: {что}"
        return f"смотрел: {что}"
    if метод == "POST" and path.endswith("/auth/login"):
        return "вход"
    if метод == "POST" and path.endswith("/auth/logout"):
        return "выход"
    return f"{ГЛАГОЛЫ.get(метод, метод.lower() or 'обратился')}: {что}"


def записывать(method: str, path: str, *, reads: bool) -> bool:
    """Стоит ли вообще заносить этот запрос в журнал.

    Изменения — всегда. Чтения — только чувствительные, и только когда это
    включено: иначе один взгляд на раздел аналитики даёт полсотни строк, и
    журнал перестают открывать.
    """
    if not path.startswith("/api/"):
        return False
    if (method or "").upper() != "GET":
        return True
    return bool(reads) and чувствительное_чтение(path)


#: Кому верить без настройки — то же, что умолчание `trusted_proxies`.
ДОВЕРЕННЫЕ_ПО_УМОЛЧАНИЮ = ("127.0.0.0/8, ::1, 10.0.0.0/8, 172.16.0.0/12, "
                           "192.168.0.0/16, fc00::/7")


@functools.lru_cache(maxsize=32)
def _сети(запись: str) -> tuple[Any, ...]:
    сети = []
    for часть in str(запись or "").split(","):
        часть = часть.strip()
        if not часть:
            continue
        try:
            сети.append(ipaddress.ip_network(часть, strict=False))
        except ValueError:
            continue
    return tuple(сети)


def _в_сетях(адрес_узла: str, сети: tuple[Any, ...]) -> bool:
    try:
        узел = ipaddress.ip_address(str(адрес_узла).strip().split("%", 1)[0])
    except ValueError:
        return False
    сопоставленный = getattr(узел, "ipv4_mapped", None)
    if сопоставленный is not None:
        узел = сопоставленный
    return any(узел.version == сеть.version and узел in сеть for сеть in сети)


def адрес(headers: Any, client: Any, доверенные: str | None = None) -> str:
    """Откуда пришли: заголовок обратного прокси или сам сокет.

    За nginx у каждого запроса адрес самого nginx, и журнал, записавший
    «127.0.0.1» напротив каждой строки, бесполезен ровно там, где он нужнее
    всего. Но и верить заголовку от кого угодно нельзя: при прямом доступе
    к серверу `X-Forwarded-For: 6.6.6.6` подписывал запрос чужим адресом.

    Поэтому заголовок читается, только если соединение пришло от
    доверенного прокси (`trusted_proxies`), а из цепочки берётся ближайший
    справа недоверенный адрес — тот, от кого прокси запрос и получил;
    дописанное клиентом левее ничего не решает.
    """
    сокет = str(getattr(client, "host", "") or "")
    сети = _сети(ДОВЕРЕННЫЕ_ПО_УМОЛЧАНИЮ if доверенные is None else str(доверенные))
    if not сети or not _в_сетях(сокет, сети):
        return сокет[:64]
    try:
        цепочка = [часть.strip() for часть in
                   str(headers.get("x-forwarded-for") or "").split(",") if часть.strip()]
    except Exception:                                       # noqa: BLE001
        цепочка = []
    for звено in reversed(цепочка):
        if not _в_сетях(звено, сети):
            return звено[:64]
    if цепочка:
        # Вся цепочка из доверенных — клиент внутри своей же сети.
        return цепочка[0][:64]
    try:
        реальный = str(headers.get("x-real-ip") or "").strip()
    except Exception:                                       # noqa: BLE001
        реальный = ""
    return (реальный or сокет)[:64]

