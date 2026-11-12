"""Приём звонков от агента, поставленного на саму станцию.

Забор «изнутри» работает, только когда сервер распознавания видит файлы
АТС: смонтированный каталог, общий диск, одна машина. В жизни станция чаще
стоит отдельно — за NAT, в другом городе, под чужим присмотром, — и тогда
не помогает ни журнал CDR, ни AMI: события приходят, а записей не достать.

Агент решает это с другой стороны: он живёт НА станции, читает её журнал и
её же каталог записей и сам присылает звонок вместе с файлом. Наружу
станции при этом не нужно открывать ничего — только исходящее соединение к
этому серверу.

Порядок разговора агента с сервером:

1. ``POST /hello`` — «я такой-то, вот моя станция и версия». В ответ —
   идентификатор агента, правила приёма (предел размера, нужен ли звук) и
   задание, если его поставили из раздела «АТС».
2. ``POST /ask`` — список идентификаторов звонков. Сервер отвечает, какие
   из них ему нужны: заливать гигабайты записей, уже лежащих в архиве,
   незачем.
3. ``POST /call`` — звонок и его запись одним многочастным запросом.

Все три требуют обычного ключа доступа с правом записи: агент — это
клиент, а не особая сущность с собственной аутентификацией.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Request,
    Response,
    UploadFile,
)

from .. import resources as ресурсы
from ..db import new_id
from ..errors import (
    ASRHubError,
    ConfigError,
    FileTooLarge,
    ForbiddenError,
    RequestTooLarge,
    StorageError,
    UnsupportedFormat,
)
from ..logging_setup import get_logger
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_write,
)

log = get_logger("telephony")

router = APIRouter(prefix="/api/telephony/agent", tags=["Телефония"])

#: Где лежат файлы агента в дереве программы. Отдаём их с сервера, чтобы
#: установка на станции была одной строкой и не требовала ни git, ни
#: копирования руками.
КОРЕНЬ_АГЕНТА = Path(__file__).resolve().parents[3] / "agent"

#: Идентификатор агента приходит от него самого — значит, это ввод.
#: Пропускаем только то, из чего нельзя собрать путь: он же служит частью
#: имени станции в архиве. Из одних точек — нельзя: «.» и «..» в адресе
#: управления агентом браузер нормализует, и ручка уходила не туда.
ПОХОЖ_НА_ID = re.compile(r"^(?!\.+$)[A-Za-z0-9._\-]{1,64}$")

#: Предел длины идентификатора звонка: у Asterisk это «эпоха.счётчик» с
#: приставкой системы, и длиннее этого приезжает только мусор, ложащийся
#: в первичный ключ архива.
ДЛИНА_UNIQUEID = 128

#: Сколько агентов может завести один ключ, не администратор. Каждый —
#: станция в архиве и строка в разделе «АТС»; ключ роли user заводил их без
#: счёта, по одному на каждое приветствие без идентификатора.
АГЕНТОВ_НА_КЛЮЧ = 100

#: Ключи звонков, запись которых принимается прямо сейчас. Проверка «есть ли
#: звонок» и запись строки разделены приёмом файла: повтор агента после 504
#: от прокси, пока первый запрос ещё идёт, или задвоение запроса сетью
#: создавали два задания на один звонок и два файла в uploads.
_ПРИНИМАЮТСЯ: set[str] = set()
_ЗАМОК_ПРИЁМА = threading.Lock()


class АгентНеизвестен(ConfigError):
    """Агента нет на сервере (убрали в разделе «АТС» или не здоровался).

    Код ответа прежний, 400, — так его понимают уже поставленные агенты, — а
    свой код ошибки нужен агенту: по нему он прерывает заход и откладывает
    звонки, а не теряет их как «ошибку одного звонка».
    """

    code = "agent_unknown"


class АгентВыключен(ConfigError):
    """Агент выключен в разделе «АТС» — звонков от него сервер не принимает."""

    code = "agent_disabled"

#: Расширения записей, которые примем от агента.
ЗВУК = {".wav", ".mp3", ".ogg", ".gsm", ".alaw", ".ulaw", ".sln", ".g722", ".opus"}

#: Поля звонка, которые агент вправе прислать. Список закрытый: всё
#: остальное в архив не попадёт, даже если приедет.
ПОЛЯ_ЗВОНКА = (
    "src", "dst", "clid", "channel", "dstchannel", "context", "disposition",
    "direction", "queue", "agent", "duration", "billsec", "answered",
    "started_at", "userfield", "accountcode", "linkedid", "sequence", "did",
)


def _агентская_станция(agent_id: str) -> str:
    """Имя станции для звонков этого агента.

    Агент — это отдельный источник, и мешать его звонки со станцией,
    которую сервер читает сам, нельзя: у них разные идентификаторы звонков
    и разные каталоги записей.
    """
    return f"agent:{agent_id}"


def _проверить_id(agent_id: str) -> str:
    agent_id = str(agent_id or "").strip()
    if not agent_id:
        raise error_response(ConfigError(
            "Не указан идентификатор агента.",
            hint="Сначала вызовите POST /api/telephony/agent/hello."))
    if not ПОХОЖ_НА_ID.match(agent_id):
        raise error_response(ConfigError(
            f"Недопустимый идентификатор агента: «{agent_id}».",
            hint="Разрешены латиница, цифры, точка, дефис и подчёркивание."))
    return agent_id


def _свой_агент(state: Any, agent_id: str, principal: Principal) -> dict[str, Any]:
    """Агент, которым этот ключ вправе распоряжаться.

    Агент принадлежит ключу, которым он впервые поздоровался. Раньше
    личность агента задавалась одним телом запроса: посторонний ключ с
    правом записи называл чужой agent_id (или хост с именем) — и забирал
    задание администратора («собрать архив»), которого настоящий агент
    после этого уже не видел, а через /ask и /call заранее вписывал
    идентификаторы звонков чужой станции, чтобы настоящие пропускались как
    «уже импортированные». Агент без владельца (заведён до этой проверки)
    закрепляется за первым, кто к нему обратится.
    """
    запись = state.db.agent_get(agent_id)
    if запись is None:
        raise error_response(АгентНеизвестен(
            f"Агент «{agent_id}» серверу неизвестен.",
            hint="Вызовите POST /api/telephony/agent/hello."))
    владелец = str(запись.get("owner") or "")
    if not владелец:
        state.db.agent_save(agent_id, {"owner": principal.name}, seen=False)
        запись["owner"] = principal.name
    elif владелец != principal.name and not principal.is_admin:
        if not _владелец_жив(state, владелец):
            # Ключа, под которым агент работал, больше нет (его отозвали),
            # а пришедший — с правом записи: это и есть «завели новый ключ и
            # перезапустили install.sh». Раньше агент запирался навсегда:
            # «работает под другим ключом — переустановите с нужным», хотя
            # именно это и было сделано. Перехват живого чужого агента
            # по-прежнему невозможен: его ключ на месте.
            log.warning("Агент «%s» переходит от ключа «%s» (его больше нет) к «%s»",
                        agent_id, владелец, principal.name)
            state.db.agent_save(agent_id, {"owner": principal.name}, seen=False)
            state.db.add_event(None, "agent_owner",
                               f"Агент {agent_id}: ключ «{владелец}» отозван, "
                               f"теперь «{principal.name}»")
            запись["owner"] = principal.name
            return запись
        raise error_response(ForbiddenError(
            f"Агент «{agent_id}» работает под другим ключом доступа.",
            hint="Каждый агент ходит своим ключом — тем, с которым он был "
                 "установлен. Передать агента другому ключу может "
                 "администратор: POST /api/telephony/agents/<id>/owner."))
    return запись


def _владелец_жив(state: Any, имя: str) -> bool:
    """Есть ли ещё ключ (или учётная запись) с этим именем."""
    for сведения in (getattr(state.settings, "api_keys", None) or {}).values():
        if str((сведения or {}).get("name") or "ключ") == имя \
                and (сведения or {}).get("enabled") is not False:
            return True
    учётки = getattr(state, "accounts", None)
    if учётки is not None:
        try:
            return any(str(у.username) == имя for у in учётки.list())
        except Exception:                                    # noqa: BLE001
            return True
    return False


def _число(значение: Any, по_умолчанию: float = 0.0) -> float:
    """Число из того, что прислал агент. Мусор — это `по_умолчанию`.

    Ноль здесь — законное значение: `or` вместо явной проверки превращал бы
    длительность 0 в умолчание, а это разные вещи.
    """
    if значение is None or значение == "":
        return по_умолчанию
    try:
        число = float(значение)
    except (TypeError, ValueError):
        return по_умолчанию
    # «Infinity» и «NaN» — тоже числа для float, но не для длительности:
    # int(inf) бросал OverflowError, и звонок получал ответ 500.
    return число if math.isfinite(число) else по_умолчанию


@router.post("/hello", summary="Агент сообщает о себе и получает задание")
def hello(request: Request, данные: dict[str, Any] | None = None,
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    данные = данные or {}
    agent_id = str(данные.get("agent_id") or "").strip()
    if not agent_id:
        # Агент пришёл без идентификатора: либо он новый, либо у него
        # потерялся файл состояния. Второе — обычное дело после переустановки
        # станции, и выдавать в этом случае НОВЫЙ идентификатор нельзя:
        # архив получил бы вторую станцию «agent:…» с теми же разговорами, и
        # весь архив заехал бы по сети заново. Поэтому сначала ищем среди
        # известных агента с тем же хостом и именем.
        #
        # Узнаём только своих: агента того же ключа (или ещё ничейного).
        # Иначе посторонний ключ, назвав хост и имя станции, получал в ответ
        # чужой идентификатор — а с ним и чужое задание.
        #
        # И только ту же машину, если агент её называет (`machine` — отпечаток
        # /etc/machine-id): две станции с именем хоста по умолчанию
        # («freepbx.sangoma.local»), поставленные одним ключом без --name,
        # сливались в одного агента, и звонки второй с совпавшим uniqueid
        # отбрасывались как «уже импортирован».
        хост = str(данные.get("host") or "").strip()
        имя = str(данные.get("name") or "").strip()
        машина = str(данные.get("machine") or "").strip()[:64]
        for прежний in state.db.agent_list():
            чей = str(прежний.get("owner") or "")
            if чей and чей != principal.name:
                continue
            if хост and str(прежний.get("host") or "") == хост \
                    and (not имя or str(прежний.get("name") or "") == имя):
                была = _машина_агента(прежний)
                if машина and была and была != машина:
                    continue
                agent_id = str(прежний["id"])
                log.info("Агент с хоста «%s» узнан по прежней записи: %s", хост, agent_id)
                break
    if not agent_id:
        основа = re.sub(r"[^A-Za-z0-9._\-]+", "-",
                        str(данные.get("host") or "pbx")).strip("-") or "pbx"
        agent_id = f"{основа[:32]}-{new_id('a')[2:8]}"
    agent_id = _проверить_id(agent_id)
    новый = state.db.agent_get(agent_id) is None
    if not новый:
        _свой_агент(state, agent_id, principal)
    elif not principal.is_admin and sum(
            1 for а in state.db.agent_list()
            if str(а.get("owner") or "") == principal.name) >= АГЕНТОВ_НА_КЛЮЧ:
        raise error_response(ConfigError(
            f"У ключа «{principal.name}» уже {АГЕНТОВ_НА_КЛЮЧ} агентов.",
            hint="Уберите лишних в разделе «АТС» или поставьте агента с "
                 "сохранённым идентификатором (state.json)."))
    # «Только посмотреть»: самопроверка агента (`--selftest`) здоровается,
    # чтобы проверить связь и часы, — и раньше забирала при этом задание
    # сервера («собрать всё»), которого сама не выполняет: нажатое в
    # разделе «АТС» задание пропадало, если на станции в это время
    # запускали самопроверку (а её запускает и повторный install.sh). Её же
    # состояние {"selftest": true} затирало настоящее состояние агента в
    # разделе. Прежние агенты шлют только state.selftest — узнаём и их.
    состояние_агента = данные.get("state") if isinstance(данные.get("state"), dict) else {}
    посмотреть = bool(данные.get("peek")) or bool((состояние_агента or {}).get("selftest"))

    состояние_сохранить = dict(данные.get("state") or {}) \
        if isinstance(данные.get("state"), dict) else {}
    if данные.get("machine"):
        # Отпечаток машины — в состоянии агента: отдельной колонки под него
        # нет, а читать его нужно только при узнавании (см. выше).
        состояние_сохранить["machine"] = str(данные.get("machine"))[:64]
    запись = state.db.agent_save(agent_id, {
        # Владелец ставится один раз — при первом приветствии. Иначе
        # администратор, поздоровавшийся за агента (проверка руками),
        # перехватывал его, и настоящий агент получал отказ.
        **({"owner": principal.name} if новый else {}),
        "name": str(данные.get("name") or данные.get("host") or agent_id)[:120],
        "host": str(данные.get("host") or "")[:120],
        "os": str(данные.get("os") or "")[:120],
        "asterisk": str(данные.get("asterisk") or "")[:120],
        "version": str(данные.get("version") or "")[:40],
        "source": str(данные.get("source") or "")[:40],
        "station": _агентская_станция(agent_id),
        **({} if посмотреть else
           {"state": json.dumps(состояние_сохранить, ensure_ascii=False)[:4000]}),
    })
    # При «только посмотреть» задание остаётся на сервере: его покажут,
    # но не снимут — выполнит его настоящий заход агента.
    команда = (str(запись.get("command") or "") if посмотреть
               else state.db.agent_command_take(agent_id))
    # Часы станции и сервера расходятся чаще, чем кажется, а по времени
    # звонка потом строится вся аналитика. Пусть агент знает наше время и
    # скажет человеку, если разошлись.
    return {
        "agent_id": agent_id,
        "station": _агентская_станция(agent_id),
        "server_time": time.time(),
        "max_file_mb": int(state.settings.get("max_upload_mb") or 2048),
        "want_audio": True,
        "command": команда,
        "peek": посмотреть,
        "enabled": bool(запись.get("enabled", 1)),
        "min_duration_s": int(state.settings.get("telephony_min_duration_s") or 0),
        "skip_unanswered": bool(state.settings.get("telephony_skip_unanswered", True)),
        # Версия агента на сервере и её отпечаток: агент старее — сам
        # скажет об этом в журнале, а с auto_update скачает и сверит.
        "agent_version": str(state.version),
        "agent_outdated": устарел(str(данные.get("version") or ""), str(state.version)),
        "agent_sha256": _отпечаток_агента(),
    }


def _версия(текст: str) -> tuple[int, ...]:
    части = re.findall(r"\d+", str(текст or ""))[:4]
    return tuple(int(ч) for ч in части)


def устарел(версия_агента: str, версия_сервера: str) -> bool:
    """Агент старее сервера (сравнение по числам версии; пустая — не знаем)."""
    у_агента, у_сервера = _версия(версия_агента), _версия(версия_сервера)
    return bool(у_агента) and bool(у_сервера) and у_агента < у_сервера


def _отпечаток_агента() -> str:
    """sha256 исходного текста агента, который отдаёт этот сервер."""
    import hashlib  # noqa: PLC0415

    try:
        return hashlib.sha256(_файл_агента("asrhub-agent.py").encode("utf-8")).hexdigest()
    except Exception:                                        # noqa: BLE001
        return ""


def _с_отпечатком(текст: str, вид: str) -> Response:
    """Код для станции — с sha256 в заголовке: установщик и агент сверяют.

    Код ставится на станцию и работает там от asterisk или root: подмена
    по дороге (прокси, http без TLS) — это выполнение чужого кода на АТС.
    """
    import hashlib  # noqa: PLC0415

    данные = текст.encode("utf-8")
    return Response(данные, media_type=вид,
                    headers={"X-ASRHub-SHA256": hashlib.sha256(данные).hexdigest()})


def _машина_агента(запись: dict[str, Any]) -> str:
    """Отпечаток машины из сохранённого состояния агента; пусто — не знаем."""
    try:
        состояние = json.loads(str(запись.get("state") or "{}"))
    except ValueError:
        return ""
    return str(состояние.get("machine") or "") if isinstance(состояние, dict) else ""


def _включён(state: Any, agent_id: str, запись: dict[str, Any]) -> None:
    """Отказ «агент выключен» — тем же видом на /ask и на /call.

    Вопрос о звонках у выключенного агента раньше отвечал как обычно, и
    агент шёл слать то, что сервер всё равно не примет.
    """
    включён = запись.get("enabled")
    if not int(включён if включён is not None else 1):
        raise error_response(АгентВыключен(
            f"Агент «{agent_id}» выключен на сервере.",
            hint="Включите его в разделе «АТС»."))


#: Пометки звонка без задания, к которому агент вправе дослать запись:
#: сервер не принял её раньше (413 от прокси, перебой) — а запись на
#: станции есть. Без этого звонок с такой пометкой оставался без записи
#: навсегда: /ask отвечал «уже в архиве», /call — «уже импортирован».
ДОСЛАТЬ_МОЖНО = ("сервер не принял запись", "запись не читается", "не дождались записи")


def _дослать_можно(строка: dict[str, Any]) -> bool:
    if строка.get("job_id"):
        return False
    пометка = str(строка.get("skipped") or "")
    return any(пометка.startswith(п) for п in ДОСЛАТЬ_МОЖНО)


@router.post("/queue_log", summary="Строки журнала очередей со станции агента")
def queue_log(request: Request, данные: dict[str, Any],
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Строки `queue_log` станции: `{"agent_id": …, "lines": ["…|…", …]}`.

    Агент (или любой отправщик — `tail -F queue_log | …`) присылает строки
    как есть; сервер разбирает события очередей и складывает их по станции
    агента. Повтор тех же строк не задваивает событий.
    """
    require_write(principal)
    state = get_state(request)
    agent_id = _проверить_id(str(данные.get("agent_id") or ""))
    _свой_агент(state, agent_id, principal)
    строки = данные.get("lines") or []
    if not isinstance(строки, list) or len(строки) > 20000:
        raise error_response(ConfigError(
            "Ожидается список строк lines, не больше 20 000 за запрос.",
            hint="Присылайте журнал очередей порциями."))
    from ..telephony import queue_log as журнал_очередей  # noqa: PLC0415

    события = журнал_очередей.разобрать("\n".join(str(с)[:2000] for с in строки))
    добавлено = state.db.queue_events_add(_агентская_станция(agent_id), события)
    return {"received": len(строки), "events": len(события), "added": добавлено}


@router.post("/cel", summary="Строки журнала CEL со станции агента: события перевода")
def cel(request: Request, данные: dict[str, Any],
        principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Строки журнала CEL станции (`{"agent_id": …, "lines": ["…", …]}`).

    Формат — отображение `asrhub-cel.csv` из раздела «Asterisk: диалплан»
    (`telephony.transfers.ОТОБРАЖЕНИЕ_CEL`). Из строк берутся события
    перевода — граница отрезков участников в разговоре с переводом; повтор
    тех же строк не задваивает событий.
    """
    import csv  # noqa: PLC0415

    require_write(principal)
    state = get_state(request)
    agent_id = _проверить_id(str(данные.get("agent_id") or ""))
    _свой_агент(state, agent_id, principal)
    строки = данные.get("lines") or []
    if not isinstance(строки, list) or len(строки) > 20000:
        raise error_response(ConfigError(
            "Ожидается список строк lines, не больше 20 000 за запрос.",
            hint="Присылайте журнал CEL порциями."))
    from ..telephony import transfers as события_перевода  # noqa: PLC0415

    переводы = [п for п in (события_перевода.из_строки_cel(поля) for поля in csv.reader(
        str(с)[:4000] for с in строки)) if п is not None]
    добавлено = state.db.call_transfers_add(_агентская_станция(agent_id), переводы)
    return {"received": len(строки), "transfers": len(переводы), "added": добавлено}


#: Предел одной части записи: части по 8 МБ проходят любой прокси.
ЧАСТЬ_ДО_МБ = 16
#: Сколько хранить недокачанную запись: агент дошлёт её следующим заходом.
ЧАСТИ_ХРАНИТЬ_С = 2 * 86400
_ИД_ЗАГРУЗКИ = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_ЗАМОК_ЧАСТЕЙ = threading.Lock()


class ЧастьНеСМеста(ASRHubError):
    """Часть записи не с того байта: агент спросит принятое и продолжит."""

    code = "upload_offset_mismatch"
    http_status = 409


def _каталог_частей(state: Any) -> Path:
    каталог = Path(state.settings.paths.uploads) / "parts"
    каталог.mkdir(parents=True, exist_ok=True)
    return каталог


def _часть(state: Any, agent_id: str, upload_id: str) -> tuple[Path, Path]:
    """Файлы недокачанной записи — свои у каждого агента."""
    if not _ИД_ЗАГРУЗКИ.match(upload_id or ""):
        raise error_response(ConfigError("Недопустимый идентификатор загрузки.",
                                         hint="Латиница, цифры, «._-», 8–128 знаков."))
    каталог = _каталог_частей(state)
    имя = hashlib.sha256(f"{agent_id}\0{upload_id}".encode()).hexdigest()[:40]
    return каталог / f"{имя}.part", каталог / f"{имя}.json"


def _подмести_части(каталог: Path) -> None:
    срок = time.time() - ЧАСТИ_ХРАНИТЬ_С
    for путь in каталог.glob("*"):
        try:
            if путь.stat().st_mtime < срок:
                путь.unlink(missing_ok=True)
        except OSError:
            continue


@router.get("/chunk", summary="Сколько байт записи уже принято (докачка частями)")
def chunk_status(request: Request, upload_id: str = Query(...), agent_id: str = Query(...),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_write(principal)
    state = get_state(request)
    agent_id = _проверить_id(agent_id)
    _свой_агент(state, agent_id, principal)
    часть, сведения = _часть(state, agent_id, upload_id)
    принято = часть.stat().st_size if часть.is_file() else 0
    return {"upload_id": upload_id, "received": принято,
            "total": (json.loads(сведения.read_text()) if сведения.is_file() else {}).get("total")}


@router.post("/chunk", summary="Часть записи от агента (докачка с места обрыва)")
async def chunk(request: Request, upload_id: str = Query(...), agent_id: str = Query(...),
                offset: int = Query(..., ge=0), total: int = Query(..., ge=1),
                sha256: str = Query(..., pattern="^[0-9a-fA-F]{64}$"),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Часть записи телом запроса: `offset` — с какого байта, `total` — весь размер.

    Записи больше предела прокси перед сервером (nginx `client_max_body_size`)
    раньше не доезжали вовсе: звонок уходил без записи. Теперь агент шлёт
    большую запись частями, а после обрыва спрашивает, сколько принято
    (`GET /chunk`), и продолжает с того места. Часть не с того места — 409
    с принятым размером. Звонок потом ссылается на загрузку полем
    `upload_id` в `meta` вместо файла.
    """
    require_write(principal)
    state = get_state(request)
    agent_id = _проверить_id(agent_id)
    await asyncio.to_thread(_свой_агент, state, agent_id, principal)
    предел = int(float(state.settings.get("max_upload_mb") or 2048) * 1024 * 1024)
    if total > предел:
        ошибка = FileTooLarge(total / 1048576, предел // 1048576)
        ошибка.message = f"Запись больше предела в {предел // 1048576} МБ."
        raise error_response(ошибка)
    часть, сведения = _часть(state, agent_id, upload_id)
    куски: list[bytes] = []
    длина = 0
    async for кусок in request.stream():
        длина += len(кусок)
        if длина > ЧАСТЬ_ДО_МБ * 1024 * 1024:
            raise error_response(RequestTooLarge(длина / 1048576, ЧАСТЬ_ДО_МБ))
        куски.append(кусок)
    данные = b"".join(куски)
    сумма = sha256.lower()

    def записать() -> int:
        with _ЗАМОК_ЧАСТЕЙ:
            return _дописать(часть, сведения, данные, offset, total, сумма,
                             agent_id, principal.name)

    принято = await asyncio.to_thread(записать)
    return {"upload_id": upload_id, "received": принято, "total": total,
            "complete": принято == total}


def _дописать(часть: Path, сведения: Path, данные: bytes, offset: int, total: int,
              сумма: str, agent_id: str, владелец: str) -> int:
    """Часть — в конец недокачанной записи; итог — сколько принято."""
    if offset == 0 and not часть.exists():
        _подмести_части(часть.parent)
    if сведения.is_file():
        try:
            прежние = json.loads(сведения.read_text())
        except ValueError:
            прежние = {}
        if (прежние.get("total"), прежние.get("sha256")) != (total, сумма):
            # Под тем же идентификатором — другая запись (файл на станции
            # переписан): старое начало ей не годится.
            часть.unlink(missing_ok=True)
    elif часть.exists():
        часть.unlink(missing_ok=True)
    принято = часть.stat().st_size if часть.is_file() else 0
    if offset != принято:
        ошибка = ЧастьНеСМеста(
            f"Часть с байта {offset}, а принято {принято}.",
            hint="Агент спросит принятое (GET /chunk) и продолжит с него.")
        ошибка.details = {"received": принято}
        raise error_response(ошибка)
    if принято + len(данные) > total:
        raise error_response(ConfigError("Части длиннее объявленного размера записи."))
    with часть.open("ab") as выход:
        выход.write(данные)
    сведения.write_text(json.dumps({"total": total, "sha256": сумма,
                                    "agent_id": agent_id, "owner": владелец}))
    return принято + len(данные)


def _убрать_загрузку(state: Any, agent_id: str, upload_id: str) -> None:
    """Докачанная запись не понадобилась (звонок уже в архиве) — убрать."""
    if not upload_id:
        return
    try:
        for путь in _часть(state, agent_id, upload_id):
            путь.unlink(missing_ok=True)
    except Exception:                                       # noqa: BLE001
        pass


def _собрать_загрузку(state: Any, upload_id: str, agent_id: str,
                      расширение: str) -> tuple[Path, int, str]:
    """Докачанная запись → файл загрузки: (путь, размер, sha256)."""
    часть, сведения = _часть(state, agent_id, upload_id)
    размер = часть.stat().st_size if часть.is_file() else 0
    try:
        о_загрузке = json.loads(сведения.read_text()) if сведения.is_file() else {}
    except ValueError:
        о_загрузке = {}
    if not размер or размер != int(о_загрузке.get("total") or -1):
        # 409 того же вида, что у части не с того места: агент спросит
        # принятое и дошлёт остаток (или всю запись, если её подмели).
        ошибка = ЧастьНеСМеста(
            f"Запись {upload_id} докачана не целиком: {размер} из "
            f"{о_загрузке.get('total') or '?'} байт.",
            hint="Агент дошлёт остаток и пришлёт звонок ещё раз.")
        ошибка.details = {"received": размер}
        raise error_response(ошибка)
    сумма = hashlib.sha256()
    with часть.open("rb") as вход:
        for кусок in iter(lambda: вход.read(1 << 20), b""):
            сумма.update(кусок)
    цель = Path(state.settings.paths.uploads) / f"{new_id('pbx')}{расширение}"
    часть.replace(цель)
    сведения.unlink(missing_ok=True)
    return цель, размер, сумма.hexdigest()


@router.get("/command", summary="Задание для агента, если оно есть")
def command(request: Request, agent_id: str = Query(...),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    agent_id = _проверить_id(agent_id)
    _свой_агент(state, agent_id, principal)
    return {"command": state.db.agent_command_take(agent_id),
            "server_time": time.time()}


@router.post("/ask", summary="Какие из этих звонков серверу ещё нужны")
def ask(request: Request, данные: dict[str, Any],
        principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Отсев уже известного — до того, как агент начнёт заливать записи.

    Без этого повторный сбор архива тащил бы по сети всё заново: на
    сорока тысячах разговоров это десятки гигабайт впустую.
    """
    state = get_state(request)
    require_write(principal)
    agent_id = _проверить_id(str(данные.get("agent_id") or ""))
    _включён(state, agent_id, _свой_агент(state, agent_id, principal))
    станция = _агентская_станция(agent_id)
    ids = [str(и) for и in (данные.get("ids") or []) if str(и).strip()]
    if len(ids) > 5000:
        raise error_response(ConfigError(
            "За один раз можно спросить не более 5000 звонков.",
            hint="Разбейте список на части."))
    нужны = []
    for и in ids:
        строка = state.db.call_get(f"{станция}:{и}")
        if строка is None or _дослать_можно(строка):
            нужны.append(и)
    return {"wanted": нужны, "known": len(ids) - len(нужны)}


class ЗвонокПринимается(ASRHubError):
    """Тот же звонок уже принимается другим запросом — повторить позже."""

    code = "call_in_progress"
    http_status = 409


@router.post("/call", summary="Звонок с записью от агента")
async def call(request: Request,
               meta: str = Form(..., description="JSON с полями звонка"),
               file: UploadFile | None = File(default=None),
               sha256: str = Form(default="", description="SHA-256 записи — поле "
                                  "после файла: агент считает её по ходу отправки"),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Принимает звонок (и запись) от агента на станции.

    Обработчик асинхронный — ради потокового приёма файла, — а всё, что
    ходит в базу и запускает ffprobe, уходит в поток. Раньше agent_get,
    call_exists, save_call и queue.submit (хеш файла, ffprobe до минуты,
    запись в базу) шли прямо в цикле событий, и на каждую запись от агента
    сервер переставал отвечать всем — /health ждал полторы-три секунды.

    Суточная квота ключа действует и здесь: прежде через агента ключ с
    квотой «два задания в сутки» ставил их сколько угодно.
    """
    state = get_state(request)
    require_write(principal)
    try:
        # Разбор полей и проверка агента ходят в базу — в потоке.
        итог = await asyncio.to_thread(_звонок_до_записи, state, principal, meta)
    except BaseException:
        if file is not None:
            await file.close()
        raise
    поля, agent_id, запись_агента = итог["поля"], итог["agent_id"], итог["агент"]
    uniqueid, станция, ключ = итог["uniqueid"], итог["станция"], итог["ключ"]
    if sha256:
        поля["sha256"] = sha256
    with _ЗАМОК_ПРИЁМА:
        if ключ in _ПРИНИМАЮТСЯ:
            if file is not None:
                await file.close()
            raise error_response(ЗвонокПринимается(
                f"Звонок {uniqueid} уже принимается другим запросом.",
                hint="Агент повторит его позже сам."))
        _ПРИНИМАЮТСЯ.add(ключ)
    try:
        return await _принять(request, state, principal, поля, file, agent_id,
                              запись_агента, uniqueid, станция, ключ)
    finally:
        with _ЗАМОК_ПРИЁМА:
            _ПРИНИМАЮТСЯ.discard(ключ)


def _звонок_до_записи(state: Any, principal: Principal, meta: str) -> dict[str, Any]:
    """Всё, что о звонке решается до приёма записи, — в потоке (база)."""
    try:
        поля = json.loads(meta)
    except ValueError as exc:
        raise error_response(ConfigError(
            f"Поля звонка не читаются как JSON: {exc}")) from exc
    if not isinstance(поля, dict):
        raise error_response(ConfigError("Поля звонка должны быть объектом JSON."))

    agent_id = _проверить_id(str(поля.get("agent_id") or ""))
    запись_агента = _свой_агент(state, agent_id, principal)
    _включён(state, agent_id, запись_агента)

    uniqueid = str(поля.get("uniqueid") or "").strip()
    if not uniqueid:
        raise error_response(ConfigError(
            "У звонка нет идентификатора (uniqueid)."))
    if len(uniqueid) > ДЛИНА_UNIQUEID:
        raise error_response(ConfigError(
            f"Идентификатор звонка длиннее {ДЛИНА_UNIQUEID} знаков.",
            hint="У Asterisk это «эпоха.счётчик»; проверьте разбор журнала на станции."))
    станция = _агентская_станция(agent_id)
    return {"поля": поля, "agent_id": agent_id, "агент": запись_агента,
            "uniqueid": uniqueid, "станция": станция, "ключ": f"{станция}:{uniqueid}"}


async def _принять_поток(state: Any, file: UploadFile, расширение: str,
                         agent_id: str) -> tuple[Path, int, str]:
    """Запись из тела запроса — в файл загрузки: (путь, размер, sha256)."""
    предел = int(state.settings.get("max_upload_mb") or 2048)
    цель = state.settings.paths.uploads / f"{new_id('pbx')}{расширение}"
    размер = 0
    сумма = hashlib.sha256()
    try:
        with цель.open("wb") as выход:
            while True:
                кусок = await file.read(1 << 20)
                if not кусок:
                    break
                размер += len(кусок)
                if размер > предел * 1024 * 1024:
                    выход.close()
                    цель.unlink(missing_ok=True)
                    # 413, а не 400: агент по нему шлёт звонок без записи.
                    ошибка = FileTooLarge(размер / 1048576, предел)
                    ошибка.message = f"Запись больше предела в {предел} МБ."
                    ошибка.hint = ("Поднимите «Предел размера загрузки» или сжимайте "
                                   "записи на станции.")
                    raise error_response(ошибка)
                сумма.update(кусок)
                выход.write(кусок)
    except OSError as exc:
        цель.unlink(missing_ok=True)
        await asyncio.to_thread(state.db.agent_count, agent_id, errors=1, error=str(exc))
        raise error_response(StorageError(f"Не удалось сохранить запись: {exc}")) from exc
    finally:
        await file.close()

    return цель, размер, сумма.hexdigest()


async def _принять(request: Request, state: Any, principal: Principal,
                   поля: dict[str, Any], file: UploadFile | None, agent_id: str,
                   запись_агента: dict[str, Any], uniqueid: str, станция: str,
                   ключ: str) -> dict[str, Any]:
    """Сам приём звонка — под занятым ключом (см. `_ПРИНИМАЮТСЯ`).

    Всё, что ходит в базу и запускает ffprobe, — в потоке (см. `call`).
    """
    from ..telephony.asterisk import нога_лучше  # noqa: PLC0415
    from .routes_jobs import _probe_duration, check_quota, под_квотой  # noqa: PLC0415

    звонок = {к: поля.get(к) for к in ПОЛЯ_ЗВОНКА if к in поля}
    звонок["duration"] = int(_число(звонок.get("duration")))
    звонок["billsec"] = int(_число(звонок.get("billsec")))
    звонок["started_at"] = _число(звонок.get("started_at"))
    if "linkedid" in звонок:
        связь = str(звонок.get("linkedid") or "").strip()[:128]
        звонок["linkedid"] = связь if re.fullmatch(r"[\w.\-]*\d+\.\d+", связь) else ""
    if "sequence" in звонок:
        звонок["sequence"] = int(_число(звонок.get("sequence")))
    if "did" in звонок:
        звонок["did"] = re.sub(r"[^0-9+]", "", str(звонок.get("did") or ""))[:32]
    звонок["answered"] = 1 if звонок.get("answered") else 0
    звонок["pbx_uid"] = uniqueid
    загрузка = str(поля.get("upload_id") or "").strip()
    есть_файл = (file is not None and bool((file.filename or "").strip())) or bool(загрузка)

    прежний = await asyncio.to_thread(state.db.call_get, ключ)
    if прежний is not None:
        # Звонок уже в архиве. Asterisk 12+ пишет на один звонок несколько
        # строк CDR, и пришедшая позже бывает той самой — отвеченной. Раньше
        # она отбрасывалась как «уже импортирован», приняв тело целиком.
        лучше = нога_лучше(звонок, прежний)
        if прежний.get("job_id") or not (есть_файл and (лучше or _дослать_можно(прежний))):
            if лучше:
                пометка = str(прежний.get("skipped") or "") if прежний.get("job_id") \
                    else str(поля.get("skipped") or прежний.get("skipped") or "")
                await asyncio.to_thread(functools.partial(
                    state.db.save_call, ключ, job_id=None, skipped=пометка, owner=None,
                    station=станция, **звонок))
            if file is not None:
                await file.close()
            await asyncio.to_thread(_убрать_загрузку, state, agent_id, загрузка)
            return {"ok": True, "skipped": "уже импортирован", "uniqueid": uniqueid,
                    **({"updated": True} if лучше else {})}

    if not есть_файл:
        # Звонок без записи — тоже звонок: он нужен в журнале, в счётчиках
        # и в разрезах. Просто распознавать нечего.
        def без_записи() -> None:
            state.db.save_call(ключ, job_id=None,
                               skipped=str(поля.get("skipped") or "нет записи"),
                               owner=principal.name, station=станция, **звонок)
            state.db.agent_count(agent_id, calls=1)

        await asyncio.to_thread(без_записи)
        if file is not None:
            await file.close()
        return {"ok": True, "skipped": "нет записи", "uniqueid": uniqueid}
    имя = Path(str(file.filename if file is not None and not загрузка
                   else поля.get("upload_name") or "запись.wav")).name
    расширение = Path(имя).suffix.lower()
    if расширение not in ЗВУК:
        # 415, а не 400: агент по этому коду понимает, что дело в ЗАПИСИ, и
        # шлёт звонок ещё раз без неё — звонок остаётся в журнале.
        if file is not None:
            await file.close()
        await asyncio.to_thread(_убрать_загрузку, state, agent_id, загрузка)
        ошибка = UnsupportedFormat(имя)
        ошибка.message = f"«{имя}»: такие записи сервер не принимает."
        ошибка.hint = "Ожидаются " + ", ".join(sorted(ЗВУК)) + "."
        raise error_response(ошибка)
    # Суточная квота по числу заданий — до приёма файла, как у обычной
    # загрузки и у /process-call: незачем принимать сотню мегабайт, чтобы
    # затем отказать. Отказ — 429 с Retry-After: агент повторит позже.
    # Раньше этот путь квоту не проверял вовсе: ключ с quota_jobs_per_day=2
    # сам заводил агента через /hello и ставил задания без счёта.
    try:
        await asyncio.to_thread(check_quota, request, principal)
    except BaseException:
        if file is not None:
            await file.close()
        raise
    if загрузка:
        # Запись докачана частями (`POST /chunk`): собрать и сверить сумму.
        if file is not None:
            await file.close()
        цель, размер, получено = await asyncio.to_thread(
            _собрать_загрузку, state, загрузка, agent_id, расширение)
    else:
        assert file is not None
        цель, размер, получено = await _принять_поток(state, file, расширение, agent_id)

    ожидалось = str(поля.get("sha256") or "").strip().lower()
    if ожидалось and ожидалось != получено:
        # Запись доехала не той, что ушла: обрыв, который прокси выдал за
        # конец тела, или порча по дороге. Повтор агента пришлёт её заново.
        цель.unlink(missing_ok=True)
        await asyncio.to_thread(functools.partial(
            state.db.agent_count, agent_id, errors=1,
            error="контрольная сумма записи не сошлась"))
        raise error_response(ConfigError(
            f"Запись звонка {uniqueid} повреждена в пути: контрольная сумма не сошлась.",
            hint="Агент отправит её ещё раз; если повторяется — проверьте прокси "
                 "между станцией и сервером."))

    def поставить() -> dict[str, Any]:
        # Хеш файла, ffprobe и запись в базу — в потоке, а не в цикле событий:
        # на записи в сотни мегабайт они занимали его секундами, и сервер не
        # отвечал даже на /api/health. Последняя проверка квоты и постановка
        # — под одним замком владельца (см. `под_квотой`): иначе
        # одновременные звонки обходили квоту.
        try:
            метки = ",".join(м for м in ("АТС", "агент", str(запись_агента.get("name") or ""),
                                         str(звонок.get("direction") or "")) if м)
            задание = под_квотой(
                state, principal, functools.partial(
                    state.queue.submit,
                    file_path=цель, filename=имя,
                    settings=state.settings.merged({}),
                    owner=principal.name, api_key_name=principal.name,
                    priority=int(state.settings.get("telephony_priority") or 40),
                    source="asterisk", tags=метки),
                incoming_bytes=размер, incoming_audio_s=_probe_duration(цель))
        except BaseException as exc:
            цель.unlink(missing_ok=True)
            state.db.agent_count(agent_id, errors=1, error=str(getattr(exc, "message", exc)))
            raise
        звонок["recording"] = str(цель)
        state.db.save_call(ключ, job_id=задание.get("id"), skipped="",
                           owner=principal.name, station=станция, **звонок)
        state.db.agent_count(agent_id, calls=1, files=1, size=размер)
        return задание

    try:
        задание = await asyncio.to_thread(поставить)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {"ok": True, "uniqueid": uniqueid, "job_id": задание.get("id"),
            "bytes": размер}


# ---------------------------------------------------------------------------
# Раздача самого агента и установщика
# ---------------------------------------------------------------------------

def _файл_агента(имя: str) -> str:
    # Сначала — дерево программы (клон, установщик, образ), потом —
    # share/asrhub/agent, куда агента кладёт колесо (см. `asrhub.resources`).
    путь = КОРЕНЬ_АГЕНТА / имя
    if not путь.is_file():
        путь = ресурсы.файл("agent", имя, переменная="ASRHUB_AGENT_DIR") or путь
    try:
        return путь.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_response(ASRHubError(
            f"Файл агента «{имя}» не найден в установке сервера.",
            hint="Похоже, сервер обновляли частично: каталог agent/ отсутствует. "
                 "Укажите его переменной ASRHUB_AGENT_DIR.")) from exc


@router.get("/asrhub-agent.py", summary="Исходный текст агента")
def исходник(request: Request,
             principal: Principal = Depends(authenticate)) -> Response:
    require_write(principal)
    return _с_отпечатком(_файл_агента("asrhub-agent.py"), "text/x-python; charset=utf-8")


@router.get("/install.sh", summary="Установщик агента для станции")
def установщик(request: Request,
               principal: Principal = Depends(authenticate)) -> Response:
    """Скрипт установки, уже знающий адрес этого сервера.

    Отдаётся с подставленным адресом: человеку остаётся вставить ключ, а
    не сверять три параметра руками.
    """
    require_write(principal)
    state = get_state(request)
    текст = _файл_агента("install.sh")
    адрес = str(request.base_url).rstrip("/")
    # Адрес по умолчанию — тот, по которому пришёл этот запрос: именно он
    # заведомо доступен тому, кто устанавливает.
    текст = текст.replace("@@SERVER_URL@@", адрес)
    текст = текст.replace("@@VERSION@@", str(state.version))
    return _с_отпечатком(текст, "text/x-shellscript; charset=utf-8")


# ---------------------------------------------------------------------------
# Управление агентами из раздела «АТС»
# ---------------------------------------------------------------------------

управление = APIRouter(prefix="/api/telephony", tags=["Телефония"])


@управление.get("/agents", summary="Агенты, поставленные на станции")
def список_агентов(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    # Верх окна для раскрытия «прислал N звонков» в список: берётся до счёта.
    верх = state.db.call_horizon()
    агенты = state.db.agent_list()
    for агент in агенты:
        станция = str(агент.get("station") or "")
        агент["calls"] = state.db.call_counts(station=станция) if станция else {}
        # Состояние агент присылает строкой JSON — разворачиваем, чтобы
        # раздел не занимался разбором.
        try:
            агент["state"] = json.loads(агент.get("state") or "{}")
        except ValueError:
            агент["state"] = {}
        агент["outdated"] = устарел(str(агент.get("version") or ""), str(state.version))
    return {"agents": агенты, "at": time.time(), "server_version": str(state.version),
            "agent_sha256": _отпечаток_агента(),
            "window": {"since": None, "until": верх}}


@управление.post("/agents/{agent_id}/command", summary="Задание агенту")
def задание(request: Request, agent_id: str, данные: dict[str, Any],
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сбор всего архива или за период — агент заберёт задание сам.

    Ходить к агенту напрямую сервер не может: станция за NAT, и связь
    всегда начинает агент. Поэтому задание кладётся рядом с его записью и
    ждёт следующего обращения.
    """
    state = get_state(request)
    require_admin(principal)
    agent_id = _проверить_id(agent_id)
    режим = str(данные.get("mode") or "all")
    if режим not in ("all", "period", "new", "stop"):
        raise error_response(ConfigError(
            f"Неизвестный режим сбора: «{режим}».",
            hint="Ожидается all, period, new или stop."))
    if режим == "period":
        начало, конец = _число(данные.get("since")), _число(данные.get("until"))
        if not начало or not конец or конец <= начало:
            raise error_response(ConfigError(
                "Для сбора за период нужны начало и конец промежутка."))
        команда = f"collect:period:{int(начало)}:{int(конец)}"
    else:
        команда = f"collect:{режим}"
    if not state.db.agent_command_set(agent_id, команда):
        raise error_response(ConfigError(f"Агент «{agent_id}» серверу неизвестен."))
    state.db.add_event(None, "agent_command", f"Агенту {agent_id}: {команда}")
    return {"ok": True, "agent_id": agent_id, "command": команда}


@управление.post("/agents/{agent_id}/enabled", summary="Включить или выключить агента")
def включить(request: Request, agent_id: str, данные: dict[str, Any],
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    agent_id = _проверить_id(agent_id)
    if state.db.agent_get(agent_id) is None:
        raise error_response(ConfigError(f"Агент «{agent_id}» серверу неизвестен."))
    state.db.agent_save(agent_id, {"enabled": bool(данные.get("enabled", True))},
                        seen=False)
    return {"ok": True, "agent_id": agent_id,
            "enabled": bool(данные.get("enabled", True))}


@управление.post("/agents/{agent_id}/owner", summary="Передать агента другому ключу")
def передать(request: Request, agent_id: str, данные: dict[str, Any],
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Агент принадлежит ключу, которым поздоровался впервые.

    Сменить ключ на станции значило переустановить агента — и получить 403
    «работает под другим ключом», хотя именно это и сделано; удаление
    состояния давало новый идентификатор и вторую станцию в архиве с теми же
    звонками. Здесь администратор передаёт агента ключу по его имени.
    """
    state = get_state(request)
    require_admin(principal)
    agent_id = _проверить_id(agent_id)
    кому = str(данные.get("owner") or "").strip()
    if not кому:
        raise error_response(ConfigError("Не указано, какому ключу передать агента.",
                                         hint="Тело: {\"owner\": \"<имя ключа>\"}."))
    if not _владелец_жив(state, кому):
        raise error_response(ConfigError(
            f"Ключа или учётной записи «{кому}» нет.",
            hint="Имя ключа — в разделе «Доступ»."))
    прежний = state.db.agent_get(agent_id)
    if прежний is None:
        raise error_response(АгентНеизвестен(f"Агент «{agent_id}» серверу неизвестен."))
    state.db.agent_save(agent_id, {"owner": кому}, seen=False)
    state.db.add_event(None, "agent_owner",
                       f"Агент {agent_id}: «{прежний.get('owner') or '—'}» → «{кому}»")
    return {"ok": True, "agent_id": agent_id, "owner": кому}


@управление.delete("/agents/{agent_id}", summary="Убрать агента")
def убрать(request: Request, agent_id: str,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Забывает агента. Звонки, которые он прислал, остаются в архиве."""
    state = get_state(request)
    require_admin(principal)
    agent_id = _проверить_id(agent_id)
    if not state.db.agent_delete(agent_id):
        raise error_response(ConfigError(f"Агент «{agent_id}» серверу неизвестен."))
    state.db.add_event(None, "agent_removed", f"Агент {agent_id} убран")
    return {"ok": True, "agent_id": agent_id}
