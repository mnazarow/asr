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

import json
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile

from ..db import new_id
from ..errors import ASRHubError, ConfigError, ForbiddenError, StorageError
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
#: имени станции в архиве.
ПОХОЖ_НА_ID = re.compile(r"^[A-Za-z0-9._\-]{1,64}$")

#: Расширения записей, которые примем от агента.
ЗВУК = {".wav", ".mp3", ".ogg", ".gsm", ".alaw", ".ulaw", ".sln", ".g722", ".opus"}

#: Поля звонка, которые агент вправе прислать. Список закрытый: всё
#: остальное в архив не попадёт, даже если приедет.
ПОЛЯ_ЗВОНКА = (
    "src", "dst", "clid", "channel", "dstchannel", "context", "disposition",
    "direction", "queue", "agent", "duration", "billsec", "answered",
    "started_at", "userfield", "accountcode",
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
        raise error_response(ConfigError(
            f"Агент «{agent_id}» серверу неизвестен.",
            hint="Вызовите POST /api/telephony/agent/hello."))
    владелец = str(запись.get("owner") or "")
    if not владелец:
        state.db.agent_save(agent_id, {"owner": principal.name}, seen=False)
        запись["owner"] = principal.name
    elif владелец != principal.name and not principal.is_admin:
        raise error_response(ForbiddenError(
            f"Агент «{agent_id}» работает под другим ключом доступа.",
            hint="Каждый агент ходит своим ключом — тем, с которым он был "
                 "установлен. Переустановите агента с нужным ключом."))
    return запись


def _число(значение: Any, по_умолчанию: float = 0.0) -> float:
    """Число из того, что прислал агент. Мусор — это `по_умолчанию`.

    Ноль здесь — законное значение: `or` вместо явной проверки превращал бы
    длительность 0 в умолчание, а это разные вещи.
    """
    if значение is None or значение == "":
        return по_умолчанию
    try:
        return float(значение)
    except (TypeError, ValueError):
        return по_умолчанию


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
        хост = str(данные.get("host") or "").strip()
        имя = str(данные.get("name") or "").strip()
        for прежний in state.db.agent_list():
            чей = str(прежний.get("owner") or "")
            if чей and чей != principal.name:
                continue
            if хост and str(прежний.get("host") or "") == хост \
                    and (not имя or str(прежний.get("name") or "") == имя):
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
        "state": json.dumps(данные.get("state") or {}, ensure_ascii=False)[:4000],
    })
    команда = state.db.agent_command_take(agent_id)
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
        "enabled": bool(запись.get("enabled", 1)),
        "min_duration_s": int(state.settings.get("telephony_min_duration_s") or 0),
        "skip_unanswered": bool(state.settings.get("telephony_skip_unanswered", True)),
    }


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
    _свой_агент(state, agent_id, principal)
    станция = _агентская_станция(agent_id)
    ids = [str(и) for и in (данные.get("ids") or []) if str(и).strip()]
    if len(ids) > 5000:
        raise error_response(ConfigError(
            "За один раз можно спросить не более 5000 звонков.",
            hint="Разбейте список на части."))
    нужны = [и for и in ids if not state.db.call_exists(f"{станция}:{и}")]
    return {"wanted": нужны, "known": len(ids) - len(нужны)}


@router.post("/call", summary="Звонок с записью от агента")
async def call(request: Request,
               meta: str = Form(..., description="JSON с полями звонка"),
               file: UploadFile | None = File(default=None),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_write(principal)
    try:
        поля = json.loads(meta)
    except ValueError as exc:
        raise error_response(ConfigError(
            f"Поля звонка не читаются как JSON: {exc}")) from exc
    if not isinstance(поля, dict):
        raise error_response(ConfigError("Поля звонка должны быть объектом JSON."))

    agent_id = _проверить_id(str(поля.get("agent_id") or ""))
    запись_агента = _свой_агент(state, agent_id, principal)
    if not int(запись_агента.get("enabled") or 0):
        raise error_response(ConfigError(
            f"Агент «{agent_id}» выключен на сервере.",
            hint="Включите его в разделе «АТС»."))

    uniqueid = str(поля.get("uniqueid") or "").strip()
    if not uniqueid:
        raise error_response(ConfigError(
            "У звонка нет идентификатора (uniqueid)."))
    станция = _агентская_станция(agent_id)
    ключ = f"{станция}:{uniqueid}"
    if state.db.call_exists(ключ):
        return {"ok": True, "skipped": "уже импортирован", "uniqueid": uniqueid}

    звонок = {к: поля.get(к) for к in ПОЛЯ_ЗВОНКА if к in поля}
    звонок["duration"] = int(_число(звонок.get("duration")))
    звонок["billsec"] = int(_число(звонок.get("billsec")))
    звонок["started_at"] = _число(звонок.get("started_at"))
    звонок["answered"] = 1 if звонок.get("answered") else 0
    звонок["pbx_uid"] = uniqueid

    if file is None or not (file.filename or "").strip():
        # Звонок без записи — тоже звонок: он нужен в журнале, в счётчиках
        # и в разрезах. Просто распознавать нечего.
        state.db.save_call(ключ, job_id=None, skipped=str(поля.get("skipped") or "нет записи"),
                           owner=principal.name, station=станция, **звонок)
        state.db.agent_count(agent_id, calls=1)
        return {"ok": True, "skipped": "нет записи", "uniqueid": uniqueid}

    имя = Path(str(file.filename)).name
    расширение = Path(имя).suffix.lower()
    if расширение not in ЗВУК:
        raise error_response(ConfigError(
            f"«{имя}»: такие записи сервер не принимает.",
            hint="Ожидаются " + ", ".join(sorted(ЗВУК)) + "."))
    предел = int(state.settings.get("max_upload_mb") or 2048)
    цель = state.settings.paths.uploads / f"{new_id('pbx')}{расширение}"
    размер = 0
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
                    raise error_response(ConfigError(
                        f"Запись больше предела в {предел} МБ.",
                        hint="Поднимите «Предел размера загрузки» или сжимайте записи на станции."))
                выход.write(кусок)
    except OSError as exc:
        цель.unlink(missing_ok=True)
        state.db.agent_count(agent_id, errors=1, error=str(exc))
        raise error_response(StorageError(f"Не удалось сохранить запись: {exc}")) from exc
    finally:
        await file.close()

    try:
        метки = ",".join(м for м in ("АТС", "агент", str(запись_агента.get("name") or ""),
                                     str(звонок.get("direction") or "")) if м)
        задание = state.queue.submit(
            file_path=цель, filename=имя,
            settings=state.settings.merged({}),
            owner=principal.name, api_key_name=principal.name,
            priority=int(state.settings.get("telephony_priority") or 40),
            source="asterisk", tags=метки)
    except Exception as exc:                                 # noqa: BLE001
        цель.unlink(missing_ok=True)
        state.db.agent_count(agent_id, errors=1, error=str(exc))
        raise
    звонок["recording"] = str(цель)
    state.db.save_call(ключ, job_id=задание.get("id"), skipped="",
                       owner=principal.name, station=станция, **звонок)
    state.db.agent_count(agent_id, calls=1, files=1, size=размер)
    return {"ok": True, "uniqueid": uniqueid, "job_id": задание.get("id"),
            "bytes": размер}


# ---------------------------------------------------------------------------
# Раздача самого агента и установщика
# ---------------------------------------------------------------------------

def _файл_агента(имя: str) -> str:
    путь = КОРЕНЬ_АГЕНТА / имя
    try:
        return путь.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_response(ASRHubError(
            f"Файл агента «{имя}» не найден в установке сервера.",
            hint="Похоже, сервер обновляли частично: каталог agent/ отсутствует.")) from exc


@router.get("/asrhub-agent.py", summary="Исходный текст агента")
def исходник(request: Request,
             principal: Principal = Depends(authenticate)) -> Response:
    require_write(principal)
    return Response(_файл_агента("asrhub-agent.py"),
                    media_type="text/x-python; charset=utf-8")


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
    return Response(текст, media_type="text/x-shellscript; charset=utf-8")


# ---------------------------------------------------------------------------
# Управление агентами из раздела «АТС»
# ---------------------------------------------------------------------------

управление = APIRouter(prefix="/api/telephony", tags=["Телефония"])


@управление.get("/agents", summary="Агенты, поставленные на станции")
def список_агентов(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
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
    return {"agents": агенты, "at": time.time()}


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
