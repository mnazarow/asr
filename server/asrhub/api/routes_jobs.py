"""Маршруты работы с заданиями: загрузка, очередь, результаты, управление."""
from __future__ import annotations

import asyncio
import functools
import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, PlainTextResponse

from .. import catalog
from .. import settings_access as S
from ..config import Settings, parse_scalar
from ..db import new_id
from ..errors import (
    ASRHubError,
    ConfigError,
    FileTooLarge,
    JobNotFound,
    QuotaExceeded,
    StorageError,
    UnsupportedFormat,
)
from ..job_queue import ACTIVE_STATUSES
from ..logging_setup import get_logger
from ..pipeline import export as export_mod
from ..pipeline import waveform as waveform_mod
from ..pipeline.audio import SUPPORTED_EXTENSIONS
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_owner,
    require_write,
    scope_owner,
)

log = get_logger("api.jobs")
router = APIRouter(prefix="/api/jobs", tags=["Задания"])


def content_disposition(filename: str) -> str:
    """Заголовок скачивания с поддержкой кириллицы.

    Заголовки HTTP передаются в latin-1, поэтому имя файла с кириллицей
    нужно кодировать по RFC 5987. Дополнительно оставляем ASCII-запасной
    вариант для старых клиентов.
    """
    from urllib.parse import quote

    # Управляющие знаки — прочь, и первым делом перевод строки. Имя файла
    # приходит снаружи (в том числе из адреса в /process-call, где хвост
    # адреса берётся как имя), а значение заголовка, в котором есть \r\n, —
    # это либо отказ сервера («Invalid HTTP header value», пустой ответ и
    # оборванное соединение при каждой попытке скачать задание), либо, на
    # ASGI-сервере попроще, расщепление ответа. Кавычка в ASCII-варианте
    # закрывала бы заголовок раньше времени.
    чистое = "".join(" " if знак < " " or знак == "\x7f" else знак
                     for знак in str(filename or "")).strip() or "результат"
    чистое = чистое.replace('"', "'")
    ascii_name = чистое.encode("ascii", "replace").decode("ascii").replace("?", "_")
    quoted = quote(чистое, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quoted}"


def _parse_settings(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise error_response(ConfigError(
            f"Не удалось разобрать параметры задания: {exc}",
            hint="Поле settings должно быть корректным JSON.")) from exc
    if not isinstance(data, dict):
        raise error_response(ConfigError(
            "Параметры задания должны быть объектом JSON.",
            hint='Пример: settings={"model":"gigaam-v3-rnnt","language":"ru"}'))
    return data


#: Необязательные поля формы с данными звонка: кто кому звонил и при каких
#: обстоятельствах. Перечислены отдельно, потому что их два применения —
#: приём в обработчике и исключение из разбора параметров задания.
ПОЛЯ_ЗВОНКА = ("caller", "callee", "call_id", "direction", "agent", "queue",
               "call_started_at", "disposition", "station")

#: Поля формы, которые обрабатываются отдельно и параметрами не являются.
#: Поля звонка обязаны быть здесь: без этого `_form_overrides` принимает их
#: за опечатку в имени параметра и отвечает «Неизвестные поля формы» —
#: то есть задание с номерами абонентов не создавалось бы вовсе.
_RESERVED_FORM_FIELDS = frozenset({
    "file", "files", "settings", "priority", "group_id", "tags",
    "reference_text", "webhook_url", *ПОЛЯ_ЗВОНКА,
})


async def _form_overrides(request: Request) -> dict[str, Any]:
    """Параметры, переданные отдельными полями формы.

    Раньше принималось только поле settings с объектом JSON внутри. Первое,
    что делает человек с curl, — пишет `-F model=gigaam-v3-e2e-rnnt`, и это
    поле молча пропадало: задание уходило на модель по умолчанию, а в ответе
    стояла не та модель, которую просили. Теперь принимаются оба способа,
    поле settings имеет больший вес.
    """
    try:
        form = await request.form()
    except Exception:                                   # noqa: BLE001
        return {}

    values: dict[str, Any] = {}
    unknown: list[str] = []
    bad: list[str] = []
    for name in form:
        if name in _RESERVED_FORM_FIELDS:
            continue
        raw = form[name]
        if not isinstance(raw, str):
            continue                                    # это файл, а не параметр
        spec = catalog.PARAMS_BY_KEY.get(name)
        if spec is None:
            unknown.append(name)
            continue
        # Поле формы всегда строка, а проверка значений ждёт настоящий тип:
        # `-F diarization=true` отвергалось с «ожидается да/нет», хотя именно
        # так этот способ и описан в справочнике. Приводим к типу параметра
        # тем же разбором, что и для переменных окружения.
        try:
            values[name] = parse_scalar(raw, spec.type)
        except (ConfigError, ValueError, json.JSONDecodeError) as exc:
            bad.append(f"{name}: {exc}")

    if bad:
        raise error_response(ConfigError(
            "Не удалось разобрать поля формы: " + "; ".join(bad),
            hint="Логические значения: true/false, да/нет, 1/0. "
                 "Списки — через запятую."))

    if unknown:
        # Молчать нельзя: опечатка в имени параметра иначе выглядит как
        # «сервер меня проигнорировал».
        known = ", ".join(sorted(unknown))
        raise error_response(ConfigError(
            f"Неизвестные поля формы: {known}.",
            hint="Список допустимых параметров: GET /api/params. "
                 "Служебные поля: file, settings, priority, group_id, tags, "
                 "reference_text, webhook_url. Данные звонка: "
                 + ", ".join(ПОЛЯ_ЗВОНКА) + "."))
    return values


def _probe_duration(path: Path) -> float:
    """Длительность записи, ноль — если определить не удалось."""
    from ..pipeline.audio import probe

    try:
        return float(probe(path).duration_s)
    except (ASRHubError, OSError):
        return 0.0


def check_quota(request: Request, principal: Principal, *,
                incoming_bytes: int = 0, incoming_audio_s: float = 0.0) -> None:
    """Проверяет суточные квоты ключа перед приёмом задания (см. `проверить_квоту`)."""
    try:
        проверить_квоту(get_state(request), principal, incoming_bytes=incoming_bytes,
                        incoming_audio_s=incoming_audio_s)
    except QuotaExceeded as exc:
        raise error_response(exc) from exc


def проверить_квоту(state: Any, principal: Principal, *,
                    incoming_bytes: int = 0, incoming_audio_s: float = 0.0) -> None:
    """Проверяет суточные квоты ключа перед приёмом задания.

    Три роли давали ответ на вопрос «что можно делать», но не на вопрос
    «сколько». Один ключ мог занять всю очередь и весь диск, и остановить
    это можно было только отключив ключ целиком.

    Квота считается за скользящие сутки и восстанавливается сама.
    Администратор не ограничен: иначе обслуживание сервера упиралось бы в
    ту же стену, что и злоупотребление.
    """
    if principal.is_admin:
        return
    limits = {
        "jobs": float(principal.quota_jobs_per_day),
        "audio_hours": float(principal.quota_audio_hours_per_day),
        "storage_gb": float(principal.quota_storage_gb),
    }
    if not any(limits.values()):
        return

    scope = scope_owner(principal) or principal.name
    used = state.db.owner_usage(scope, time.time() - 86400)
    # Учитываем и то, что принимаем прямо сейчас: иначе одна загрузка на
    # сто часов проходила бы поверх любой квоты.
    pending = {
        "jobs": used["jobs"] + 1,
        "audio_hours": used["audio_hours"] + incoming_audio_s / 3600,
        "storage_gb": used["storage_gb"] + incoming_bytes / 1024 ** 3,
    }
    for kind, limit in limits.items():
        if limit and pending[kind] > limit:
            raise QuotaExceeded(kind, round(pending[kind], 3), limit)


# ---------------------------------------------------------------------------
# Данные звонка при постановке задания
# ---------------------------------------------------------------------------
#
# Запись разговора приезжает не только от импортёра АТС: её присылает
# интеграция, которая уже знает всё про звонок — номера, направление,
# оператора, очередь. Раньше это знание пропадало: задание создавалось, а
# разговор не попадал ни в раздел «Телефония», ни в разрезы по оператору и
# очереди, ни в отчёт по направлениям — там живут только строки таблицы
# `calls`. Теперь та же строка появляется и здесь.
#
# ВАЖНО про поведение при кривом значении. Поля необязательные, и ни одно
# из них не меняет того, что сервер делает с записью: это метки. Отвечать
# отказом на «direction=incomming» значит потерять распознавание из-за
# опечатки в необязательной подписи — обмен, на который никто в здравом уме
# не согласится. Отказать ДО приёма файла тоже нельзя: FastAPI разбирает
# multipart целиком до вызова обработчика, и к моменту, когда мы видим
# поля, файл уже принят и лежит во временном файле. Поэтому выбран мягкий
# путь: непонятое значение не записывается, а объяснение возвращается в
# ответе (`call.warnings`) — отправитель видит, что именно не понято, и
# исправляет, ничего не потеряв. Задание важнее метки.

#: Направления звонка — ровно те, которыми их называет импортёр АТС
#: (`telephony.asterisk.направление`). Свой набор здесь завести нельзя:
#: отбор в разделе «Телефония» идёт точным совпадением строки, и звонок с
#: направлением «Входящий» с большой буквы не нашёлся бы ни по одному
#: фильтру — он просто выпал бы из всех разрезов.
НАПРАВЛЕНИЯ = ("входящий", "исходящий", "внутренний")

#: Как то же самое называют в чужих системах. Интеграции пишут на
#: латинице, и требовать от них русского слова — значит требовать
#: переделки там, где достаточно словаря на десять строк.
СИНОНИМЫ_НАПРАВЛЕНИЯ = {
    "in": "входящий", "inbound": "входящий", "incoming": "входящий",
    "вх": "входящий", "вход": "входящий", "входящая": "входящий",
    "out": "исходящий", "outbound": "исходящий", "outgoing": "исходящий",
    "исх": "исходящий", "исход": "исходящий", "исходящая": "исходящий",
    "internal": "внутренний", "local": "внутренний", "internal_call": "внутренний",
    "вну": "внутренний", "внутр": "внутренний", "внутренняя": "внутренний",
}

#: Имя источника для звонков, приехавших через API без своего имени.
#:
#: Пустым его оставлять нельзя: ключ архива собирается как
#: «станция:идентификатор» (см. telephony/importer.py::_ключ), и при пустой
#: станции ключ выродился бы в голый идентификатор звонка — а он приходит
#: снаружи и вполне может совпасть с идентификатором настоящей АТС.
#: Совпадение означает не ошибку, а тихую подмену: `save_call` обновил бы
#: чужую строку, и разговор филиала стал бы разговором интеграции.
#: Постоянное «api» и отделяет такие записи от станций, и даёт разрез
#: «сколько разговоров приехало через API» — на графиках раздела
#: «Телефония» он виден наравне с настоящими станциями.
СТАНЦИЯ_API = "api"

#: Отметки времени раньше этой даты считаем ошибкой ввода, а не историей.
#: 2000-01-01; всё, что меньше, — это почти всегда миллисекунды, принятые
#: за секунды, ноль или мусор.
НЕ_РАНЬШЕ = 946684800.0


def _разобрать_время(значение: str) -> tuple[float, str]:
    """Время начала звонка: секунды эпохи или ISO-8601. Возвращает (время, беда).

    Принимаются три вида, потому что все три встречаются в жизни: секунды
    эпохи (АТС и биллинг), миллисекунды эпохи (всё, что писали на
    JavaScript) и ISO-8601 (всё остальное). Миллисекунды отличаем по
    порядку величины: 1e11 секунд — это 5138 год, дат оттуда не бывает, а
    вот миллисекунды с 1973 года выглядят именно так. Без этой проверки
    «1757500001000» дало бы звонок в далёком будущем — и он не нашёлся бы
    ни в одном периоде раздела «Телефония», потому что отбор идёт по
    `started_at` в границах периода.
    """
    текст = str(значение or "").strip()
    if not текст:
        return 0.0, ""
    try:
        число = float(текст)
    except ValueError:
        число = None
    if число is not None:
        if число >= 1e11:                       # похоже на миллисекунды
            число /= 1000.0
        if число < НЕ_РАНЬШЕ:
            return 0.0, (f"время начала звонка «{текст}» не похоже на дату "
                         f"(раньше 2000 года) — не записано")
        return число, ""
    # ISO-8601. `fromisoformat` до Python 3.11 не понимает «Z» на конце —
    # а именно так время пишет половина систем, включая браузеры.
    подготовленное = текст.replace("Z", "+00:00").replace("z", "+00:00")
    from datetime import datetime  # noqa: PLC0415

    for разбор in (datetime.fromisoformat,
                   lambda s: datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S"),
                   lambda s: datetime.strptime(s[:16], "%Y-%m-%d %H:%M")):
        try:
            когда = разбор(подготовленное)
        except (ValueError, TypeError):
            continue
        отметка = когда.timestamp()
        if отметка < НЕ_РАНЬШЕ:
            return 0.0, f"время начала звонка «{текст}» раньше 2000 года — не записано"
        return отметка, ""
    return 0.0, (f"время начала звонка «{текст}» не разобрано — ожидаются секунды "
                 f"эпохи (1757500001) или ISO-8601 (2026-09-10T14:03:11) — не записано")


def _разобрать_направление(значение: str) -> tuple[str, str]:
    """Направление звонка из известного набора. Возвращает (значение, беда)."""
    текст = str(значение or "").strip().lower()
    if not текст:
        return "", ""
    if текст in НАПРАВЛЕНИЯ:
        return текст, ""
    замена = СИНОНИМЫ_НАПРАВЛЕНИЯ.get(текст)
    if замена:
        return замена, ""
    return "", (f"направление «{значение}» не из известного набора "
                f"({', '.join(НАПРАВЛЕНИЯ)}) — не записано")


def _данные_звонка(поля: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Поля формы → колонки таблицы `calls` плюс список непонятого.

    Пустой словарь на выходе означает «про звонок ничего не передали»:
    задание в этом случае ведёт себя ровно так, как вело раньше, и лишней
    строки в архиве телефонии не появляется.
    """
    если_есть = {имя: str(поля.get(имя) or "").strip() for имя in ПОЛЯ_ЗВОНКА}
    if not any(если_есть.values()):
        return {}, []

    беды: list[str] = []
    направление, беда = _разобрать_направление(если_есть["direction"])
    if беда:
        беды.append(беда)
    начало, беда = _разобрать_время(если_есть["call_started_at"])
    if беда:
        беды.append(беда)

    # `answered` решает, попадёт ли разговор в долю отвеченных у оператора.
    # Если исход назвали — верим ему; если нет — считаем отвеченным: нам
    # прислали запись разговора, значит разговор состоялся. Обратное
    # умолчание (ноль) обнулило бы «отвечено» у всех заданий из API и
    # испортило бы разрез по операторам ровно там, где он и нужен.
    исход = если_есть["disposition"]
    отвечен = True
    if исход:
        отвечен = исход.strip().upper() in ("ANSWERED", "ANSWER", "ОТВЕЧЕН", "ОТВЕЧЕНО")

    данные: dict[str, Any] = {
        "src": если_есть["caller"],
        "dst": если_есть["callee"],
        "direction": направление,
        "agent": если_есть["agent"],
        "queue": если_есть["queue"],
        "disposition": исход,
        "started_at": начало,
        "answered": отвечен,
        # Станция — либо названная, либо постоянная «api»: см. СТАНЦИЯ_API.
        "station": если_есть["station"] or СТАНЦИЯ_API,
        # Номер звонящего в поле определителя: раздел «Телефония» ищет и по
        # нему тоже, и без него поиск по номеру находил бы не все звонки.
        "clid": если_есть["caller"],
    }
    данные["call_id"] = если_есть["call_id"]
    return данные, беды


def _сохранить_звонок(state: Any, job: dict[str, Any], principal: Principal,
                      данные: dict[str, Any], беды: list[str], *,
                      ключ_по_заданию: bool = False) -> dict[str, Any]:
    """Пишет строку в `calls` для уже созданного задания.

    Порядок именно такой — сначала задание, потом звонок: строка звонка
    ссылается на задание полем `job_id`, и без него разговор виден в
    архиве, но не связан с расшифровкой.

    Ключ архива собирается как «станция:идентификатор» — так же, как у
    импортёра АТС (telephony/importer.py::_ключ). Идентификатор берётся из
    `call_id`, а если его не прислали — это идентификатор задания.

    Почему именно идентификатор задания, а не случайный UUID и не отпечаток
    полей: он уже уникален на весь сервер, он уже напечатан в ответе на
    этот самый запрос, и по нему строка звонка и задание находятся друг из
    друга в обе стороны без единого лишнего поля. Случайный UUID был бы так
    же уникален, но не сказал бы ничего: найдя строку в архиве, человек не
    смог бы дойти до записи. Отпечаток из номеров и времени выглядит
    заманчиво (одна и та же запись не задвоилась бы), но два настоящих
    звонка между теми же номерами в ту же секунду слились бы в одну строку,
    а «в ту же секунду» — это ровно то, что делает исходящий обзвон.

    `ключ_по_заданию` нужен пакетной загрузке: там один `call_id` на
    несколько файлов, и если бы ключ собирался из него, второй файл пакета
    перезаписал бы строку первого (`save_call` обновляет запись с тем же
    ключом). Поэтому в пакете ключ всегда по заданию, а сам `call_id` едет
    в `pbx_uid` — в жизни одному звонку и правда соответствует несколько
    записей, по одной на плечо.
    """
    беды = list(беды)
    название = str(данные.get("call_id") or "")
    станция = str(данные.get("station") or СТАНЦИЯ_API)
    идентификатор = str(job.get("id") or "") if (ключ_по_заданию or not название) else название
    ключ = f"{станция}:{идентификатор}" if станция else идентификатор

    # Время начала: если его не назвали, берём время постановки задания.
    # Ноль оставлять нельзя — отбор в разделе «Телефония» идёт по
    # `started_at` внутри периода, и звонок с нулём не попал бы ни в один
    # период, кроме «за всё время»: строка есть, а найти её нечем.
    начало = float(данные.get("started_at") or 0.0)
    if начало <= 0:
        начало = float(job.get("created_at") or 0.0) or time.time()

    # Длительность разговора — длительность записи: другого источника у нас
    # нет, а без неё все сводки по времени разговора (talk_s) считали бы
    # ноль. Она известна уже сейчас: очередь измерила файл при постановке.
    длительность = int(float(job.get("media_duration_s") or 0.0))

    # Чужую строку архива трогать нельзя. Ключ собирается из полей формы,
    # а `save_call` обновляет строку с таким ключом молча — достаточно было
    # назвать настоящее имя станции (оно видно в GET /api/telephony/stations)
    # и угадать `uniqueid` вида «эпоха.счётчик», чтобы разговор филиала сменил
    # владельца, потерял номера и оператора и пропал из его отчётов.
    # Задвоения при этом не было: строка одна, прежней больше не существует.
    чей = state.db.call_owner(ключ)
    if чей is not None and not principal.is_admin and чей != principal.name:
        беды.append("данные звонка не сохранены: строка архива принадлежит "
                    "другому ключу доступа")
        return {"saved": False, "warnings": беды}

    поля = {к: з for к, з in данные.items() if к != "call_id"}
    поля.update({"pbx_uid": название or идентификатор, "started_at": начало,
                 "duration": длительность, "billsec": длительность})
    try:
        state.db.save_call(ключ, job_id=str(job.get("id") or ""),
                           owner=principal.name, **поля)
    except Exception as exc:                            # noqa: BLE001
        # Сюда попадает сбой базы. Задание уже создано и уже поставлено в
        # очередь: ронять ответ ошибкой значит заставить отправителя
        # повторить загрузку файла и создать второе такое же задание.
        # Поэтому — предупреждение в ответе и строка в журнале.
        log.warning("Звонок для задания %s не сохранён: %s", job.get("id"), exc)
        беды.append(f"данные звонка не сохранены: {exc}")
        return {"saved": False, "warnings": беды}
    return {"saved": True, "uniqueid": ключ, "pbx_uid": название or идентификатор,
            "station": станция, "direction": str(поля.get("direction") or ""),
            "started_at": начало, "warnings": беды}


def _число(значение: Any, умолчание: float) -> float:
    """Дробное из тела запроса — или понятный отказ вместо пятисотки.

    Тело здесь — свободный словарь, схемой не описанный, и `float("вчера")`
    доходил до общего обработчика: клиент получал «внутреннюю ошибку
    сервера» и трассировку в журнале, хотя виноват был он сам. Соседние
    маршруты того же файла на такую же ошибку отвечают 400 с объяснением.
    """
    if значение is None or значение == "":
        return умолчание
    try:
        return float(значение)
    except (TypeError, ValueError):
        raise error_response(ConfigError(
            f"Ожидается число, получено «{значение}».")) from None


#: Описание полей звонка для OpenAPI — одно на оба маршрута, чтобы они не
#: разъехались между собой в генерируемой документации.
ОПИСАНИЕ_ЗВОНКА = """
**Данные звонка (все поля необязательны).** Если задано хотя бы одно, после
постановки задания в архив телефонии добавляется строка о разговоре, и
запись попадает в раздел «Телефония», в разрезы по оператору, очереди и
направлению и в отчёты по номерам. Ничего не задано — поведение прежнее.

* `caller` — номер звонящего;
* `callee` — номер вызываемого;
* `call_id` — идентификатор звонка на стороне АТС; не задан — берётся
  идентификатор задания;
* `direction` — `входящий`, `исходящий` или `внутренний` (принимаются и
  `inbound`/`outbound`/`internal`);
* `agent` — внутренний номер оператора;
* `queue` — очередь;
* `call_started_at` — время начала: секунды эпохи (`1757500001`),
  миллисекунды или ISO-8601 (`2026-09-10T14:03:11`); не задано — время
  постановки задания;
* `disposition` — исход по версии АТС (`ANSWERED`, `NO ANSWER`, `BUSY`…);
* `station` — имя источника; не задано — `api`.

Непонятое значение одного поля не отменяет постановку задания: оно не
записывается, а объяснение возвращается в ответе, в `call.warnings`.
"""


@router.post("", summary="Поставить файл в очередь",
             description="Принимает аудио- или видеофайл и ставит его в очередь "
                         "распознавания. Параметры задания — полем `settings` "
                         "с объектом JSON или отдельными полями формы.\n"
                         + ОПИСАНИЕ_ЗВОНКА)
async def create_job(
    request: Request,
    file: UploadFile = File(..., description="Аудио- или видеофайл"),
    settings: str | None = Form(default=None, description="JSON с параметрами задания"),
    priority: int | None = Form(default=None),
    group_id: str | None = Form(default=None),
    tags: str = Form(default=""),
    reference_text: str = Form(default=""),
    webhook_url: str = Form(default=""),
    caller: str = Form(default="", description="Номер звонящего"),
    callee: str = Form(default="", description="Номер вызываемого"),
    call_id: str = Form(default="", description="Идентификатор звонка на стороне АТС"),
    direction: str = Form(default="",
                          description="входящий, исходящий или внутренний"),
    agent: str = Form(default="", description="Внутренний номер оператора"),
    queue: str = Form(default="", description="Очередь"),
    call_started_at: str = Form(default="",
                                description="Время начала звонка: секунды эпохи или ISO-8601"),
    disposition: str = Form(default="", description="Исход по версии АТС: ANSWERED, NO ANSWER…"),
    station: str = Form(default="", description="Имя источника; не задано — «api»"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)

    filename = Path(file.filename or "upload.wav").name
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in SUPPORTED_EXTENSIONS:
        raise error_response(UnsupportedFormat(filename, suffix))

    # Квоту на число заданий проверяем до приёма файла: смысла принимать
    # сотню мегабайт, чтобы затем отказать, нет.
    check_quota(request, principal)

    limit_mb = int(state.settings.get("max_upload_mb") or 2048)
    target = state.settings.paths.uploads / f"{new_id('up')}{suffix or '.bin'}"
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = await file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit_mb * 1024 * 1024:
                    out.close()
                    target.unlink(missing_ok=True)
                    raise error_response(FileTooLarge(size / 1024 / 1024, limit_mb))
                out.write(chunk)
    except OSError as exc:
        target.unlink(missing_ok=True)
        raise error_response(StorageError(f"Не удалось сохранить файл: {exc}")) from exc
    finally:
        await file.close()

    # Всё, что идёт после записи файла, обязано убирать его за собой: файл
    # уже лежит в uploads, а задания, которое на него ссылается, ещё нет —
    # значит уборщик его никогда не найдёт. Раньше разбор полей формы стоял
    # вне защиты, и каждая опечатка в имени параметра оставляла на диске
    # копию загруженной записи.
    try:
        # Объём и длительность известны только теперь, когда файл на диске.
        #
        # ffprobe — внешний процесс с тайм-аутом в минуту, и обработчик
        # здесь `async`: прямой вызов останавливал бы весь сервер на время
        # разбора. В поток уходит и постановка в очередь — она делает тот
        # же разбор второй раз и пишет в базу.
        check_quota(request, principal, incoming_bytes=size,
                    incoming_audio_s=await asyncio.to_thread(_probe_duration, target))
        # Значения из settings перекрывают одноимённые поля формы: явный JSON
        # выражает намерение точнее, чем разрозненные поля.
        overrides = {**await _form_overrides(request), **_parse_settings(settings)}
        merged = state.settings.merged(overrides)
        job = await asyncio.to_thread(functools.partial(
            state.queue.submit,
            file_path=target, filename=filename, settings=merged,
            owner=principal.name, api_key_name=principal.name,
            priority=priority, group_id=group_id, source="web",
            tags=tags, reference_text=reference_text, webhook_url=webhook_url))
    except Exception:
        # Ловим всё: ASRHubError, HTTPException от разбора полей и любую
        # неожиданную ошибку — файл не должен пережить неудачный запрос.
        target.unlink(missing_ok=True)
        raise

    # Данные звонка — ПОСЛЕ создания задания и вне защиты с уборкой файла.
    # Вне намеренно: задание уже существует и уже в очереди, и удалять под
    # ним файл из-за неудачной записи подписи значило бы сломать
    # распознавание ради метки. Ошибки здесь не поднимаются вовсе — см.
    # `_сохранить_звонок`.
    данные_звонка, беды = _данные_звонка({
        "caller": caller, "callee": callee, "call_id": call_id,
        "direction": direction, "agent": agent, "queue": queue,
        "call_started_at": call_started_at, "disposition": disposition,
        "station": station})
    if данные_звонка:
        job["call"] = await asyncio.to_thread(
            _сохранить_звонок, state, job, principal, данные_звонка, беды)
    return job


@router.post("/batch", summary="Поставить несколько файлов одной группой",
             description="Принимает несколько файлов одним запросом и ставит их "
                         "одной группой. Ответ — список созданных заданий и "
                         "список файлов, которые принять не удалось.\n"
                         + ОПИСАНИЕ_ЗВОНКА
                         + "\nВ пакете данные звонка применяются к каждому файлу: "
                           "так везут несколько записей одного разговора (по "
                           "одной на плечо). Ключ строки в архиве при этом свой "
                           "у каждого файла, а `call_id` — общий.")
async def create_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    settings: str | None = Form(default=None),
    priority: int | None = Form(default=None),
    caller: str = Form(default="", description="Номер звонящего"),
    callee: str = Form(default="", description="Номер вызываемого"),
    call_id: str = Form(default="", description="Идентификатор звонка на стороне АТС"),
    direction: str = Form(default="",
                          description="входящий, исходящий или внутренний"),
    agent: str = Form(default="", description="Внутренний номер оператора"),
    queue: str = Form(default="", description="Очередь"),
    call_started_at: str = Form(default="",
                                description="Время начала звонка: секунды эпохи или ISO-8601"),
    disposition: str = Form(default="", description="Исход по версии АТС: ANSWERED, NO ANSWER…"),
    station: str = Form(default="", description="Имя источника; не задано — «api»"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    limit_mb = int(state.settings.get("max_upload_mb") or 2048)
    max_files = int(state.settings.get("max_batch_files") or 200)
    if len(files) > max_files:
        raise error_response(ConfigError(
            f"В одном пакете {len(files)} файлов при пределе {max_files}.",
            hint="Разбейте отправку на несколько пакетов или поднимите "
                 "max_batch_files в настройках."))

    group = new_id("grp")
    created: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    overrides = {**await _form_overrides(request), **_parse_settings(settings)}
    merged = state.settings.merged(overrides)
    # Данные звонка разбираются один раз на весь пакет: поля общие, и
    # повторять разбор (а с ним и предупреждения) на каждый файл незачем.
    данные_звонка, беды_звонка = _данные_звонка({
        "caller": caller, "callee": callee, "call_id": call_id,
        "direction": direction, "agent": agent, "queue": queue,
        "call_started_at": call_started_at, "disposition": disposition,
        "station": station})

    for item in files:
        filename = Path(item.filename or "upload.wav").name
        suffix = Path(filename).suffix.lower()
        if suffix and suffix not in SUPPORTED_EXTENSIONS:
            errors.append({"filename": filename, "error": "неподдерживаемый формат"})
            await item.close()
            continue
        target = state.settings.paths.uploads / f"{new_id('up')}{suffix or '.bin'}"
        try:
            # Тот же предел, что и в одиночной загрузке: без него ключ с ролью
            # «user» забивал диск пакетом любого размера.
            size = 0
            with target.open("wb") as out:
                while True:
                    chunk = await item.read(1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit_mb * 1024 * 1024:
                        raise FileTooLarge(size / 1024 / 1024, limit_mb)
                    out.write(chunk)
            # Квота проверяется на каждый файл пакета: иначе один запрос из
            # двухсот файлов обходил бы её целиком.
            check_quota(request, principal, incoming_bytes=size,
                        incoming_audio_s=await asyncio.to_thread(_probe_duration, target))
            job = await asyncio.to_thread(functools.partial(
                state.queue.submit,
                file_path=target, filename=filename, settings=merged,
                owner=principal.name, api_key_name=principal.name,
                priority=priority, group_id=group, source="web-batch"))
            if данные_звонка:
                # Ключ архива — по заданию: иначе второй файл пакета
                # перезаписал бы строку первого, и в архиве осталась бы
                # одна запись вместо всех плеч разговора.
                job["call"] = await asyncio.to_thread(
                    _сохранить_звонок, state, job, principal, данные_звонка,
                    беды_звонка, ключ_по_заданию=True)
            created.append(job)
        except ASRHubError as exc:
            target.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": exc.message})
        except OSError as exc:
            target.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": str(exc)})
        except BaseException:
            # Всё остальное — тоже с уборкой файла. `check_quota` поднимает
            # HTTPException, а не ASRHubError: он пролетал мимо обеих веток
            # выше, и уже записанный файл оставался на диске без задания.
            # Найти его потом не может никто: и уборка по сроку, и удаление
            # ходят по таблице заданий. Ключ с квотой навсегда съедал по
            # max_upload_mb за каждую попытку повторить пакет.
            target.unlink(missing_ok=True)
            raise
        finally:
            await item.close()

    return {"group_id": group, "created": len(created), "jobs": created, "errors": errors}


#: Колонки задания, которые не нужны никому снаружи: раскладка хранилища.
#: `/api/system` прячет её за правами администратора с прямым объяснением —
#: «разведка перед атакой», — а в карточке задания та же информация уходила
#: любому ключу с ролью `user`, да ещё и в каждой строке списка. Интерфейс
#: их не читает, облегчённый список не отдаёт.
ПУТИ_НА_ДИСКЕ = ("file_path", "result_path")


def _без_раскладки(job: dict[str, Any], principal: Principal) -> dict[str, Any]:
    """Убирает пути на диске у всех, кроме администратора."""
    if principal.is_admin:
        return job
    for ключ in ПУТИ_НА_ДИСКЕ:
        job.pop(ключ, None)
    return job


@router.get("", summary="Список заданий")
def list_jobs(
    request: Request,
    status: str | None = Query(default=None, description="queued, running, completed, failed…"),
    owner: str | None = None,
    model: str | None = None,
    group_id: str | None = None,
    search: str | None = None,
    since_hours: float | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    order: str = "created_at DESC",
    content: str | None = Query(
        default=None,
        description="Отбор по содержанию разговора (negative, positive, "
                    "downturn, recovered, alerts, open_commitments, "
                    "interruptions, silence, script_failed, money, monologue, "
                    "mixed, dead_air, frustrated, repeat, profanity, "
                    "profanity_agent), по здоровью распознавания (suspect, "
                    "hallucination, speakers_mismatch), по звуку на входе "
                    "(bad_audio, noisy, clipped) или по ответу языковой модели "
                    "(llm_unresolved, llm_actions, llm_missing, llm_failed, "
                    "llm_done, outcome:<исход>, reason:<причина>)"),
    light: bool = Query(default=False,
                        description="Только поля для таблицы, без текста и сегментов"),
    principal: Principal = Depends(authenticate),
) -> dict[str, Any]:
    state = get_state(request)
    statuses: list[str] | str | None = status
    if status == "active":
        statuses = list(ACTIVE_STATUSES)
    elif status and "," in status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
    since = time.time() - since_hours * 3600 if since_hours else None
    # Выборка сужается до собственных заданий для всех, кроме администратора.
    # Карточка задания давно закрыта require_owner, а список — нет: чужие
    # имена файлов, пути на диске и расшифровки уходили любому ключу.
    scope = scope_owner(principal, owner)
    # Облегчённый список пропускает текст расшифровки и сегменты. На сотне
    # часовых записей ответ со всем текстом — единицы мегабайт, и таблица в
    # интерфейсе ждала их только чтобы выбросить.
    отборы = {**state.db.CONTENT_FILTERS, **state.db.JOB_FILTERS}
    if (content and content not in отборы
            and not content.startswith(state.db.PREFIX_FILTERS)):
        raise error_response(ConfigError(
            f"Неизвестный отбор по содержанию «{content}».",
            hint="Доступные: " + ", ".join(sorted(отборы)) + ", "
                 + ", ".join(f"{п}<значение>" for п in state.db.PREFIX_FILTERS)))
    jobs = state.db.list_jobs(status=statuses, owner=scope, model=model, group_id=group_id,
                              search=search, since=since, limit=limit, offset=offset,
                              order=order, light=light, content=content)
    if not light:
        jobs = [_без_раскладки(j, principal) for j in jobs]
    # При поиске к каждой строке добавляется сама найденная фраза с
    # обрамлением и её время. Без этого список отвечал «нашлось в этом
    # разговоре» и замолкал: дальше человек открывал карточку и искал
    # глазами — при том что сервер уже знал и фразу, и секунду.
    if search:
        находки = state.db.best_snippets(search, [str(j["id"]) for j in jobs])
        for job in jobs:
            найдено = находки.get(str(job["id"]))
            if найдено:
                job["match"] = {
                    "snippet": найдено.get("snippet") or "",
                    "start_s": найдено.get("start_s"),
                    "speaker": найдено.get("speaker"),
                }
    return {
        "items": jobs,
        # Счётчик получает те же условия, что и список: иначе «показано 30,
        # всего 4000» и листалка, ведущая на пустые страницы.
        "total": state.db.count_jobs(status=statuses, owner=scope, model=model,
                                     search=search, group_id=group_id,
                                     since=since, content=content),
        # Поиск по расшифровкам берёт не больше `SEARCH_LIMIT` совпадений,
        # и об этом надо сказать. Молчаливая обрезка читается как «старых
        # разговоров на эту тему нет», а это разные утверждения.
        "search_truncated": bool(search) and state.db.last_search_truncated,
        "search_limit": state.db.SEARCH_LIMIT if search else None,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{job_id}/search", summary="Поиск по репликам одного задания")
def search_in_job(request: Request, job_id: str,
                  q: str = Query(min_length=1, description="Что искать в расшифровке"),
                  limit: int = Query(default=100, ge=1, le=500),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Найденные реплики одного разговора: фраза, её время и говорящий.

    Часовой разговор — это сотни реплик, и «найти, где обсуждали сроки»
    поиском по странице означает пролистать их все. Здесь то же самое
    делает указатель, а щелчок по находке переводит проигрыватель на её
    начало.
    """
    state = get_state(request)
    _owned_job(request, job_id, principal)
    реплики = state.db.search_segments(q, job_id=job_id, limit=limit)
    return {
        "query": q,
        "indexed": state.db.fts_ready,
        "items": реплики,
    }


@router.get("/{job_id}", summary="Карточка задания")
def get_job(request: Request, job_id: str,
            with_segments: bool = False,
            with_waveform: bool = Query(
                default=True,
                description="Отдавать полосу громкости. Отключите при частом "
                            "опросе карточки: на длинной записи это сотни килобайт"),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    try:
        job = state.queue.get(job_id)
    except JobNotFound as exc:
        raise error_response(exc) from exc
    if with_segments:
        job["segments"] = state.db.get_segments(job_id)
    curves = job.get("waveform") or []
    if with_waveform and curves:
        # `waveform` — разобранные точки для интерфейса, `waveforms` — тот же
        # набор в виде массива JSON-строк: так его отдаёт phone_asr, и
        # приёмники, написанные под него, читают карточку без переделок.
        job["waveforms"] = waveform_mod.to_phone_asr(curves)
    else:
        job.pop("waveform", None)
    job["events"] = state.db.get_events(job_id, limit=100)
    # Если задание приехало с телефонной станции — поля звонка. Без них
    # карточка показывает «1757500001.1.wav» и больше ничего: ни кто
    # звонил, ни куда попал, ни сколько ждал.
    if str(job.get("source") or "") == "asterisk":
        звонок = state.db.call_for_job(job_id)
        if звонок:
            # `recording` — путь на станции; наружу он не нужен и попадает
            # под то же правило, что и раскладка хранилища.
            if not principal.is_admin:
                звонок.pop("recording", None)
            job["call"] = звонок
    return _без_раскладки(job, principal)


@router.get("/{job_id}/waveform", summary="Полоса громкости записи")
def get_waveform(request: Request, job_id: str,
                 fmt: str = Query(default="points", pattern="^(points|phone_asr)$",
                                  description="points — разобранные точки, "
                                              "phone_asr — массив JSON-строк"),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Огибающая громкости: один замер на интервал, отдельно по каналам
    или говорящим.

    Значение `amplitude` — средний модуль отсчёта на интервале, безразмерное
    число от 0 до 1: тишина даёт около 0.001, обычная речь — 0.15…0.25.
    """
    state = get_state(request)
    _owned_job(request, job_id, principal)
    try:
        job = state.queue.get(job_id)
    except JobNotFound as exc:
        raise error_response(exc) from exc
    curves = job.get("waveform") or []
    if fmt == "phone_asr":
        return {"id": job_id, "waveforms": waveform_mod.to_phone_asr(curves)}
    return {
        "id": job_id,
        "interval_s": (job.get("params") or {}).get("waveform_interval_s"),
        "duration_s": job.get("media_duration_s"),
        "curves": curves,
    }


#: Что отдавать браузеру для расширений, которые mimetypes не знает. Пустой
#: тип заставил бы <audio> отказаться от файла молча.
#: Тип содержимого для прослушивания исходной записи.
#:
#: Покрывает каждое расширение, которое сервер принимает на вход
#: (`SUPPORTED_EXTENSIONS`), — за этим следит проверка. Раньше в таблице
#: было двенадцать позиций из двадцати девяти, а остальные уходили в
#: `mimetypes.guess_type`, который про «.caf», «.w64» и «.m2ts» ничего не
#: знает: браузер получал «application/octet-stream» и даже не пробовал
#: открыть файл. Ответ, который браузер не станет играть, — это тот же
#: отказ, только без объяснения.
_AUDIO_TYPES = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".mp2": "audio/mpeg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".wma": "audio/x-ms-wma", ".aiff": "audio/aiff", ".aif": "audio/aiff",
    ".amr": "audio/amr", ".ac3": "audio/ac3", ".caf": "audio/x-caf",
    ".w64": "audio/x-w64",
    # Видео отдаётся своим типом, а не «audio/…»: браузер сам решит, что
    # умеет открыть. У части контейнеров звук он достаёт, у части — нет,
    # но врать ему про содержимое файла в любом случае незачем.
    ".mp4": "video/mp4", ".m4v": "video/x-m4v", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo",
    ".mov": "video/quicktime", ".flv": "video/x-flv",
    ".wmv": "video/x-ms-wmv", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".ts": "video/mp2t", ".m2ts": "video/mp2t", ".3gp": "video/3gpp",
}


def _source_audio(request: Request, job: dict[str, Any]) -> Path:
    """Путь к исходной записи задания — с проверкой, что он не увёл наружу.

    Все три места, где заводится задание, кладут файл в каталог загрузок под
    именем, которое придумал сервер. Но проверка здесь всё равно нужна: этот
    обработчик отдаёт файл наружу по значению из базы, и если однажды
    появится четвёртый путь, забывший про это правило, ошибка превратится в
    чтение произвольного файла с сервера. Дешевле не полагаться на обещание.
    """
    state = get_state(request)
    raw = str(job.get("file_path") or "").strip()
    if not raw:
        raise error_response(ConfigError(
            "У задания не сохранён путь к записи.",
            hint="Так бывает у заданий, заведённых до появления этой возможности."))

    uploads = Path(state.settings.paths.uploads)
    try:
        base = uploads.resolve(strict=True)
        real = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise error_response(ConfigError(
            "Запись не найдена на диске.",
            hint="Файл удалён: либо сработал параметр delete_source_after, либо "
                 "запись убрала очистка хранилища по сроку хранения.")) from exc

    if not real.is_file() or (base != real.parent and base not in real.parents):
        raise error_response(ConfigError(
            "Запись лежит вне каталога загрузок — отдавать её нельзя.",
            hint=f"Каталог загрузок: {base}"))
    return real


def _отредактированный_путь(real: Path) -> Path:
    """Куда кладётся отредактированная копия — рядом с исходником.

    Отдельным именем, а не поверх оригинала: редакция — это производная,
    и переписать ею запись значило бы уничтожить исходные данные по нажатию
    кнопки, без возможности передумать.
    """
    return real.with_name(f"{real.stem}.redacted{real.suffix}")


@router.post("/{job_id}/redact", summary="Заглушить персональные данные в записи")
def redact_audio(request: Request, job_id: str,
                 mode: str = Query(default="", pattern="^(|beep|silence)$"),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Делает копию записи, в которой персональные данные заглушены.

    Ищутся они там же, где и при обезличивании текста, — `masking.find` по
    расшифровке, — а время берётся у слов, если выравнивание их проставило.
    Без таймкодов границы оцениваются по месту знаков в реплике: это помечено
    в ответе полем `estimated`, и запас по краям для таких кусков больше.
    """
    from ..pipeline import redact as redact_mod  # noqa: PLC0415

    state = get_state(request)
    require_write(principal)
    job = _owned_job(request, job_id, principal)
    real = _source_audio(request, job)
    сегменты = state.db.get_segments(job_id)
    if not сегменты:
        raise error_response(ConfigError(
            "У записи нет расшифровки — искать в ней нечего.",
            hint="Дождитесь распознавания или запустите его заново."))
    виды = state.settings.get("redact_kinds") or ""
    выбранные = tuple(в.strip() for в in str(виды).replace(";", ",").split(",")
                      if в.strip()) or None
    итог = redact_mod.заглушить(
        real, _отредактированный_путь(real), сегменты,
        mode=mode or str(state.settings.get("redact_mode") or "beep"),
        kinds=выбранные,
        padding_ms=S.integer(state.settings, "redact_padding_ms",
                             redact_mod.ЗАПАС_МС),
        ffmpeg=str(state.settings.get("ffmpeg_path") or "ffmpeg"))
    ответ = итог.to_dict()
    ответ["found"] = bool(итог.path)
    ответ["url"] = f"/api/jobs/{job_id}/redacted" if итог.path else None
    return ответ


@router.get("/{job_id}/redacted", summary="Отредактированная копия записи")
def get_redacted(request: Request, job_id: str,
                 principal: Principal = Depends(authenticate)):
    """Отдаёт копию, в которой персональные данные заглушены."""
    job = _owned_job(request, job_id, principal)
    real = _source_audio(request, job)
    копия = _отредактированный_путь(real)
    if not копия.is_file():
        raise error_response(ConfigError(
            "Отредактированной копии нет.",
            hint="Сначала нажмите «Заглушить персональные данные» — или "
                 "вызовите POST /api/jobs/{id}/redact."))
    media = _AUDIO_TYPES.get(копия.suffix.lower())
    if media is None:
        media, _ = mimetypes.guess_type(копия.name)
    имя = str(job.get("filename") or "").strip() or копия.name
    # Как у /audio и /download: ключу с mask_pii имя обезличивается. Тело
    # копии заглушено, а в заголовке скачивания стоял номер клиента.
    if getattr(principal, "mask_pii", False):
        from ..content import masking  # noqa: PLC0415

        имя = masking.mask_text(имя)
    основа = Path(имя).stem
    return FileResponse(str(копия), media_type=media or "application/octet-stream",
                        filename=f"{основа}.отредактировано{копия.suffix}",
                        content_disposition_type="inline")


@router.get("/{job_id}/audio", summary="Исходная запись задания")
def get_audio(request: Request, job_id: str,
              principal: Principal = Depends(authenticate)):
    """Отдаёт исходный файл записи — для прослушивания и скачивания.

    Отдаётся через FileResponse, а он умеет отвечать на заголовок Range. Это
    не мелочь: без частичных ответов встроенный проигрыватель браузера не
    может перемотать запись, он способен только слушать её с начала.
    """
    job = _owned_job(request, job_id, principal)
    real = _source_audio(request, job)
    media = _AUDIO_TYPES.get(real.suffix.lower())
    if media is None:
        media, _ = mimetypes.guess_type(real.name)
    # Имя для скачивания берём человеческое, а не служебное «up-xxxx.wav»,
    # но ключу с mask_pii — обезличенное: в колл-центре имя файла это номер
    # клиента, и заголовок скачивания не должен обходить то, что делает тело.
    name = str(job.get("filename") or "").strip() or real.name
    if getattr(principal, "mask_pii", False):
        from ..content import masking  # noqa: PLC0415

        name = masking.mask_text(name)
    if not Path(name).suffix:
        name = f"{name}{real.suffix}"
    return FileResponse(str(real), media_type=media or "application/octet-stream",
                        filename=name,
                        content_disposition_type="inline")


@router.get("/{job_id}/segments", summary="Сегменты задания")
def get_segments(request: Request, job_id: str,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    state.queue.get(job_id)
    return {"items": state.db.get_segments(job_id)}


@router.get("/{job_id}/download", summary="Скачать результат в выбранном формате")
def download(request: Request, job_id: str, fmt: str = Query(default="txt"),
             principal: Principal = Depends(authenticate)):
    state = get_state(request)
    _owned_job(request, job_id, principal)
    job = state.queue.get(job_id)
    if job["status"] != "completed":
        raise error_response(ConfigError(
            f"Задание в состоянии «{job['status']}» — результата пока нет.",
            hint="Дождитесь завершения обработки."))

    # Path("") — это Path("."), то есть рабочий каталог сервера. Пустой
    # result_path (так бывает, когда каталог-источник исчез между проверкой
    # кеша и копированием) превращал поиск файла результата в обход текущего
    # каталога процесса: первый попавшийся файл с нужным расширением уходил
    # клиенту. Поэтому пустое значение — это «каталога нет», а не «корень».
    raw_path = str(job.get("result_path") or "").strip()
    result_dir = Path(raw_path) if raw_path else None

    if fmt not in export_mod.FORMATS:
        raise error_response(ConfigError(
            f"Неизвестный формат «{fmt}».",
            hint="Доступные форматы: " + ", ".join(export_mod.FORMATS)))

    # Обезличенная выгрузка — настройкой для всех или флагом у ключа:
    # готовые файлы результата содержат полный текст, поэтому такая
    # выгрузка всегда строится заново из сегментов, с масками.
    маскировать = bool(state.settings.get("export_mask_pii")) or principal.mask_pii

    if result_dir is not None and result_dir.is_dir() and not маскировать:
        try:
            base = result_dir.resolve(strict=True)
        except OSError:
            base = None
        if base is not None:
            for candidate in sorted(base.glob(f"*.{fmt}")):
                # Симлинк внутри каталога результатов не должен уводить наружу.
                try:
                    real = candidate.resolve(strict=True)
                except OSError:
                    continue
                if not real.is_file() or base not in real.parents:
                    continue
                media, _ = mimetypes.guess_type(real.name)
                return FileResponse(str(real), filename=real.name,
                                    media_type=media or "application/octet-stream")

    # Формат не сохранялся при обработке — строим на лету из сегментов.
    segments = state.db.get_segments(job_id)
    if not segments:
        raise error_response(ConfigError(
            "Сегменты задания недоступны.",
            hint="Сегменты появляются после успешного распознавания."))
    payload = {
        "meta": {"filename": job.get("filename"), "model": job.get("model"),
                 "language": job.get("language"), "duration_s": job.get("media_duration_s"),
                 "created_at": time.strftime("%d.%m.%Y %H:%M")},
        "segments": segments,
        "text": job.get("text") or "",
        "metrics": {"rtf": job.get("rtf"),
                    "processing_time_s": job.get("processing_time_s"),
                    "segments": len(segments),
                    "words": job.get("words_count"),
                    "avg_confidence": job.get("avg_confidence")},
    }
    if маскировать:
        from ..content import masking  # noqa: PLC0415

        payload = masking.mask_payload(payload)
    merged = state.settings.merged(job.get("params") or {})
    # Имя для заголовка скачивания берётся из ТОГО ЖЕ, что и тело: из
    # маскированной карточки. Раньше тело обезличивалось, а заголовок брал
    # имя из исходной строки задания — и обезличенная выгрузка приезжала
    # файлом «+79161234567 Иванов.json». В колл-центре имя файла это номер
    # клиента, ради чего маскирование имени и делалось.
    имя_файла = job.get("filename") or job_id
    if маскировать:
        from ..content import masking  # noqa: PLC0415

        имя_файла = masking.mask_text(str(имя_файла))
    name = Path(имя_файла).stem
    if fmt == "docx":
        out = state.settings.paths.tmp / f"{job_id}.docx"
        export_mod.to_docx(payload, merged, out)
        return FileResponse(str(out), filename=f"{name}.docx")
    body = {
        "txt": export_mod.to_txt, "json": export_mod.to_json, "srt": export_mod.to_srt,
        "vtt": export_mod.to_vtt, "ass": export_mod.to_ass, "md": export_mod.to_markdown,
    }.get(fmt)
    if body is None:
        text = export_mod.to_table(payload, merged, "," if fmt == "csv" else "\t")
    else:
        text = body(payload, merged)
    media = {"json": "application/json", "srt": "application/x-subrip",
             "vtt": "text/vtt", "csv": "text/csv"}.get(fmt, "text/plain")
    return PlainTextResponse(text, media_type=f"{media}; charset=utf-8", headers={
        "Content-Disposition": content_disposition(f"{name}.{fmt}")})


def _owned_job(request: Request, job_id: str, principal: Principal) -> dict[str, Any]:
    """Находит задание и проверяет права на него."""
    state = get_state(request)
    try:
        job = state.queue.get(job_id)
    except JobNotFound as exc:
        raise error_response(exc) from exc
    try:
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return job


@router.post("/{job_id}/cancel", summary="Отменить задание")
def cancel(request: Request, job_id: str,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.cancel(job_id, by=principal.name)


def _проверенные_переопределения(сырые: Any, *, разрешённые: tuple[str, ...] | None = None,
                                 строго: bool = True) -> dict[str, Any]:
    """Параметры повтора: только из каталога, приведённые и проверенные.

    /retry клал в задание всё, что пришло в теле, как есть. Соседний /rescan
    проверял и приводил значения, а /retry — нет: `{"beam_size": "мусор"}`
    принималось с ответом 200 и роняло задание уже в очереди, а служебный
    ключ `control_of` с чужим идентификатором записывал «контрольный
    прогон» в журнал ЧУЖОГО задания. Теперь на обоих путях одно и то же:
    ключи каталога, без секретов сервера, приведение, проверка.
    """
    if not сырые:
        return {}
    if not isinstance(сырые, dict):
        raise error_response(ConfigError(
            "Переопределения передаются объектом JSON: {\"model\": \"…\"}."))
    if строго:
        чужие = sorted(str(к) for к in сырые if к not in catalog.PARAMS_BY_KEY)
        if чужие:
            raise error_response(ConfigError(
                f"Неизвестные параметры: {', '.join(чужие)}.",
                hint="Список допустимых параметров: GET /api/params."))
    свои = {к: з for к, з in сырые.items()
            if к in catalog.PARAMS_BY_KEY and з not in (None, "")
            and (разрешённые is None or к in разрешённые)
            and not Settings.чужое_заданию(к, з)}
    # Приведение ПЕРЕД проверкой — как в `Settings.merged` и `Settings.set`.
    свои = catalog.coerce_all(свои)
    ошибки = catalog.validate_all(свои)
    if ошибки:
        raise error_response(ConfigError("; ".join(ошибки)))
    return свои


@router.post("/{job_id}/retry", summary="Повторить задание")
def retry(request: Request, job_id: str,
          overrides: dict[str, Any] | None = Body(default=None),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.retry(job_id, _проверенные_переопределения(overrides) or None)


@router.post("/{job_id}/priority", summary="Изменить приоритет")
def set_priority(request: Request, job_id: str,
                 priority: int = Body(embed=True, ge=0, le=100),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.set_priority(job_id, priority)


@router.post("/{job_id}/top", summary="Поднять в начало очереди")
def to_top(request: Request, job_id: str,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.move_to_top(job_id)


@router.post("/{job_id}/bottom", summary="Опустить в конец очереди")
def to_bottom(request: Request, job_id: str,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.move_to_bottom(job_id)


@router.post("/{job_id}/pause", summary="Приостановить задание")
def pause_job(request: Request, job_id: str,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.pause_job(job_id)


@router.post("/{job_id}/resume", summary="Возобновить задание")
def resume_job(request: Request, job_id: str,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.resume_job(job_id)


@router.delete("/{job_id}", summary="Удалить задание и результаты")
def delete_job(request: Request, job_id: str,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    _delete_one(state, state.queue.get(job_id), principal)
    return {"deleted": job_id}


#: Что умеет делать сразу над многими заданиями.
BULK_ACTIONS = ("retry", "cancel", "delete", "tag", "priority")

#: Предел на одну команду. Не из осторожности: каждое действие — это запись
#: в базу и работа с файлами, и пакет в десятки тысяч заданий занял бы
#: единственную блокировку записи на минуты, остановив всю очередь. Больше
#: предела — это несколько команд подряд, и о ходе видно по ответу каждой.
BULK_LIMIT = 500


#: Что разрешено менять при повторном распознавании. Список закрытый:
#: сюда приходит ввод из интерфейса, а `retry` кладёт всё это прямо в
#: параметры задания.
ПОЛЯ_ПЕРЕРАСПОЗНАВАНИЯ = (
    "engine", "model", "language", "task", "beam_size", "temperature",
    "vad_enabled", "diarization_enabled", "punctuation", "compute_type",
    "device", "initial_prompt", "num_speakers", "align_words",
)


@router.post("/rescan", summary="Распознать записи заново")
def rescan(request: Request, данные: dict[str, Any] = Body(default={}),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Повторное распознавание — отобранных записей или всего архива.

    Зачем отдельно от «Повторить»: причина повтора почти всегда не в сбое,
    а в том, что изменилось вокруг. Вышла модель лучше, поправили словарь
    замен, включили разделение по говорящим, сменили язык — и весь прежний
    архив распознан по-старому. Перебирать его по одной записи руками
    нельзя: их тысячи.

    Отбор делается на сервере: «перераспознать всё за квартал» — это
    десятки тысяч заданий, и присылать их списком значит гонять мегабайт
    идентификаторов ради одной кнопки.

    Записи ставятся в очередь с ПОНИЖЕННОЙ важностью: свежая загрузка
    человека не должна ждать, пока переварится архив.
    """
    state = get_state(request)
    require_write(principal)

    # Принимаем только то, что есть в каталоге: белого списка мало —
    # `punctuation`, `num_speakers` и `align_words` в нём числятся, а
    # параметрами каталога не являются, и проверять их было нечем. Такой
    # ключ уезжал в параметры задания с любым значением.
    # Приведение ПЕРЕД проверкой — как в `Settings.merged` и `Settings.set`.
    # Наоборот было ошибкой: интерфейс присылает переключатели строками, и
    # «да» для детектора речи сервер принимал при загрузке записи и отвергал
    # при её перераспознавании — одно и то же значение на двух путях.
    переопределения = _проверенные_переопределения(
        данные.get("overrides") or {}, разрешённые=ПОЛЯ_ПЕРЕРАСПОЗНАВАНИЯ, строго=False)

    ids = [str(и) for и in (данные.get("ids") or []) if str(и).strip()]
    предел = max(1, min(int(_число(данные.get("limit"), 500.0)), 5000))
    if not ids:
        отбор = dict(данные.get("filter") or {})
        задания = state.db.list_jobs(
            status=отбор.get("status") or ["completed", "failed"],
            model=отбор.get("model") or None,
            search=отбор.get("search") or None,
            since=_число(отбор.get("since"), 0.0) or None,
            owner=scope_owner(principal), limit=предел, light=True)
        до = _число(отбор.get("until"), 0.0) or None
        ids = [str(з["id"]) for з in задания
               if до is None or float(з.get("created_at") or 0) <= до]
    if not ids:
        raise error_response(ConfigError(
            "По этому отбору нечего распознавать заново.",
            hint="Проверьте период и состояние записей."))

    важность = данные.get("priority")
    поставлено, отказы = [], []
    for job_id in dict.fromkeys(ids):
        try:
            _owned_job(request, job_id, principal)
            задание = state.queue.retry(job_id, переопределения or None)
            if важность is not None:
                state.queue.set_priority(job_id, int(важность))
            поставлено.append(задание.get("id") or job_id)
        except ASRHubError as exc:
            отказы.append({"id": job_id, "error": exc.message})
        except Exception as exc:                             # noqa: BLE001
            отказы.append({"id": job_id, "error": str(exc)})
    state.db.add_event(None, "rescan",
                       f"Повторное распознавание: поставлено {len(поставлено)}"
                       + (f", отказов {len(отказы)}" if отказы else ""))
    return {"queued": len(поставлено), "ids": поставлено[:200],
            "failed": отказы[:50], "overrides": переопределения}


@router.post("/bulk", summary="Действие сразу над несколькими заданиями")
def bulk(request: Request,
         action: str = Body(embed=True),
         ids: list[str] = Body(embed=True),
         tags: str = Body(default="", embed=True),
         priority: int = Body(default=50, embed=True, ge=0, le=100),
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Повторить, отменить, удалить, пометить или сменить приоритет — пачкой.

    Все действия были поштучными, и после обновления модели пятьсот
    разговоров переобрабатывались по одному, вручную.

    Отказ на одном задании не отменяет остальных: в выборку почти всегда
    попадает что-то, к чему действие неприменимо — уже завершённое среди
    отменяемых, чужое среди своих. Прерывать всю команду из-за одной такой
    строки означало бы, что пакетное действие работает только на идеально
    подобранной выборке, то есть почти никогда. Поэтому каждое задание
    обрабатывается отдельно, а в ответе стоит, что получилось и что нет.
    """
    require_write(principal)
    if action not in BULK_ACTIONS:
        raise error_response(ConfigError(
            f"Неизвестное действие «{action}».",
            hint="Возможные: " + ", ".join(BULK_ACTIONS)))
    if not ids:
        raise error_response(ConfigError(
            "Не указано ни одного задания.",
            hint="Отметьте строки в списке и повторите."))
    if len(ids) > BULK_LIMIT:
        raise error_response(ConfigError(
            f"За один раз можно обработать не больше {BULK_LIMIT} заданий, "
            f"а указано {len(ids)}.",
            hint="Разбейте выборку на части: каждое действие — это запись в "
                 "базу, и слишком большой пакет остановит очередь."))

    state = get_state(request)
    сделано: list[str] = []
    отказы: list[dict[str, str]] = []

    for job_id in dict.fromkeys(ids):          # без повторов, порядок сохранён
        try:
            job = _owned_job(request, job_id, principal)
            if action == "retry":
                state.queue.retry(job_id, None)
            elif action == "cancel":
                state.queue.cancel(job_id, by=principal.name)
            elif action == "delete":
                _delete_one(state, job, principal)
            elif action == "tag":
                state.db.update_job(job_id, tags=tags)
            elif action == "priority":
                state.queue.set_priority(job_id, priority)
            сделано.append(job_id)
        except HTTPException as exc:
            # `error_response` кладёт разбор ошибки в detail; человеку нужна
            # одна строка причины, а не весь словарь в кавычках.
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            отказы.append({"id": job_id,
                           "error": str(detail.get("message") or exc.detail)})
        except ASRHubError as exc:
            отказы.append({"id": job_id, "error": exc.message})
        except Exception as exc:               # noqa: BLE001
            # Наружу — только род ошибки, как в общем обработчике сервера:
            # текст неожиданного исключения несёт то абсолютный путь к
            # файлу на сервере, то кусок SQL. Подробности — в журнал.
            log.warning("Пакетное действие %s не удалось на %s: %s",
                        action, job_id, exc, extra={"job_id": job_id})
            отказы.append({"id": job_id,
                           "error": f"Внутренняя ошибка сервера: {type(exc).__name__}"})

    log.info("Пакетное действие «%s»: получилось %d, отказов %d",
             action, len(сделано), len(отказы))
    return {"action": action, "done": сделано, "failed": отказы,
            "requested": len(dict.fromkeys(ids))}


def _inside(base: Path, target: str) -> Path | None:
    """Путь, если он лежит внутри базового каталога, иначе None.

    Удаление шло по значению из базы без всякой проверки: путь туда кладёт
    сервер, но проверка нужна ровно по той же причине, что и при выдаче
    записи наружу (см. `_source_audio`) — стоит появиться пути, попавшему в
    базу иначе, и удаление задания превращается в удаление любого файла,
    до которого дотягивается служба. Дешевле не полагаться на обещание.
    """
    if not target:
        return None
    try:
        корень = base.resolve(strict=True)
        путь = Path(target).resolve(strict=True)
    except OSError:
        return None
    if корень != путь.parent and корень not in путь.parents:
        log.warning("Отказ удалять «%s»: путь вне каталога «%s»", путь, корень)
        return None
    return путь


def _delete_one(state: Any, job: dict[str, Any], principal: Principal) -> None:
    """Удаление одного задания вместе с его файлами."""
    job_id = str(job["id"])
    if job["status"] in ACTIVE_STATUSES:
        state.queue.cancel(job_id, by=principal.name)
    каталог = _inside(Path(state.settings.paths.results),
                      str(job.get("result_path") or ""))
    if каталог is not None:
        shutil.rmtree(каталог, ignore_errors=True)
        # И отложенный на время повтора прежний результат.
        shutil.rmtree(каталог.with_name(каталог.name + ".prev"), ignore_errors=True)
    файл = _inside(Path(state.settings.paths.uploads),
                   str(job.get("file_path") or ""))
    # Запись бывает общей: контрольный прогон второй моделью идёт по файлу
    # исходного задания, и удаление контрольного уносило звук исходного.
    if файл is not None and not state.db.file_used_elsewhere(
            str(job.get("file_path") or ""), job_id):
        файл.unlink(missing_ok=True)
    state.db.delete_job(job_id)


@router.post("/{job_id}/reference", summary="Задать эталонный текст и пересчитать WER")
def set_reference(request: Request, job_id: str,
                  text: str = Body(embed=True),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    from ..pipeline import calibration
    from ..pipeline import metrics as M

    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    job = state.queue.get(job_id)
    # Метки говорящих в эталоне не пишут, а в тексте задания они есть у
    # каждой реплики: сравнение по сегментам, как в конвейере, иначе
    # каждая реплика приносила бы две лишние вставки.
    сегменты = state.db.get_segments(job_id)
    гипотеза = " ".join(str(с.get("text") or "") for с in сегменты) if сегменты \
        else (job.get("text") or "")
    detail = M.detailed(text, гипотеза)
    state.db.update_job(job_id, reference_text=text,
                        calibration=calibration.per_job(сегменты, text) if сегменты else None,
                        **M.job_fields(detail))
    # Эталон и есть итог ручной проверки: строка очереди закрывается сама,
    # с именем того, кто проверял.
    state.db.review_update(job_id, "done", reviewer=principal.name)
    return {"job_id": job_id, **detail}


@router.post("/text", summary="Разобрать переписку (чат, почта) без звука")
def create_text_job(request: Request, данные: dict[str, Any] = Body(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Принимает переписку и разбирает её тем же слоем, что и разговоры.

    Распознавания здесь нет — оно переписке и не нужно, — поэтому задание
    сразу создаётся завершённым и разбирается на месте: очередь ждать незачем.

    Единственное, с чем нельзя смошенничать, — время. Между репликами в чате
    проходит минута или час, и это не пауза в разговоре. Всё, что меряется
    секундами, у таких заданий пустое: ноль пауз в переписке читался бы как
    «отвечали мгновенно», хотя мерить там нечего.
    """
    from .. import textchat  # noqa: PLC0415

    состояние = get_state(request)
    require_write(principal)
    try:
        разобрано = textchat.задание(данные)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    # Переписка — такое же задание, как и разговор, и в суточную квоту
    # входит так же: без проверки ключ с квотой в одно задание в сутки
    # создавал их сколько угодно, лишь бы текстом.
    check_quota(request, principal,
                incoming_bytes=len(str(разобрано["text"] or "").encode("utf-8")))

    job_id = состояние.db.create_job({
        "filename": разобрано["filename"],
        "status": "completed",
        "source": "text",
        "owner": principal.name,
        "text": разобрано["text"],
        "language": str(данные.get("language") or "ru"),
        "model": f"переписка:{разобрано['channel']}",
        "engine": "text",
        "finished_at": time.time(),
        "params": {"channel": разобрано["channel"],
                   "external_id": str(данные.get("external_id") or ""),
                   # Кто здесь оператор — чтобы пересчёт и проверка скрипта
                   # взяли ту же сторону, что и разбор при приёме.
                   **({"agent": разобрано["agent"]} if разобрано["agent"] else {}),
                   **({"crm_entity_id": str(данные["crm_entity_id"])}
                      if данные.get("crm_entity_id") else {})},
    })
    состояние.db.save_segments(job_id, разобрано["segments"])
    разбор = None
    if состояние.content is not None:
        разбор = состояние.content.analyze_job(
            job_id, segments=разобрано["segments"],
            agent_speaker=разобрано["agent"])
        # Переписка — свежая запись, как и звонок: примечание в CRM уходит
        # по ней один раз (после смыслового разбора, если он будет).
        состояние.content.в_crm(job_id, разбор)
    # И в очередь смыслового разбора — как свежий разговор: переписка ради
    # того и приходит, чтобы её разобрали теми же задачами модели.
    поток = getattr(состояние, "llm_worker", None)
    if поток is not None:
        try:
            поток.enqueue(job_id)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Переписка %s не поставлена на смысловой разбор: %s", job_id, exc)
    return {
        "id": job_id,
        "channel": разобрано["channel"],
        "known_channel": разобрано["known_channel"],
        "messages": len(разобрано["segments"]),
        "agent": разобрано["agent"],
        "content": разбор,
    }
