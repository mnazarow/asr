"""Приём звонков из диалплана Asterisk и всё для его настройки.

`POST /call` — сам приём: станция в конце разговора сообщает о звонке и
присылает запись файлом (или путь к ней на общем диске). Разбор полей и
решение — в `telephony/dialplan.py` и `Импортёр._взять`; здесь — чтение
запроса в любом из трёх видов, в которых его умеет отправить Asterisk:

* `multipart/form-data` — curl из сценария отправки, с файлом записи;
* `application/x-www-form-urlencoded` — функция CURL() диалплана; если в
  CURLOPT задан свой заголовок, Content-Type по умолчанию пропадает, и тело
  приходит вовсе без него — такое тоже разбирается как форма;
* `application/json` — для своих интеграций.

Поля можно передать и в адресе (`?station=…&uniqueid=…`): так короче
строка CURL(). Значение из тела важнее значения из адреса.

Остальные ручки — для раздела «Asterisk: диалплан»: состояние приёма и
журнал обращений, готовые файлы настройки для chan_sip и chan_pjsip и
сценарий отправки для станции.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from ..db import new_id
from ..errors import ASRHubError, ConfigError, FileTooLarge, StorageError
from ..logging_setup import get_logger
from ..telephony import dialplan as Д
from ..telephony import dialplan_config as К
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_write,
)

log = get_logger("telephony")

#: Пометки звонка без задания, к которому станция вправе дослать запись.
ДОСЛАТЬ_МОЖНО = ("нет записи", "сервер не принял запись", "запись не читается",
                 "не дождались записи")

router = APIRouter(prefix="/api/telephony/dialplan", tags=["Телефония"])

#: Сценарий отправки лежит рядом с агентом и раздаётся так же: установка на
#: станции — одна команда, без git и без копирования руками.
СЦЕНАРИЙ = Path(__file__).resolve().parents[3] / "agent" / "asrhub-dialplan"

#: Идентификатор звонка становится частью ключа архива и образцом при поиске
#: файла записи в каталоге. Пропускаем только то, из чего не собрать ни
#: путь, ни шаблон поиска.
_ПОХОЖ_НА_UID = re.compile(r"^[A-Za-z0-9._:@\-]{1,128}$")


def _адрес_клиента(request: Request) -> str:
    return request.client.host if request.client else ""


async def _прочитать(request: Request) -> tuple[dict[str, Any], UploadFile | None, Any]:
    """Поля и файл из запроса — в каком бы виде их ни прислали.

    Третье значение — разобранная форма: её надо закрыть после обработки,
    иначе временный файл загрузки живёт до сборки мусора.
    """
    поля: dict[str, Any] = {к: з for к, з in request.query_params.multi_items()
                            if к != "api_key"}
    вид = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    файл: UploadFile | None = None
    форма = None
    if вид in ("multipart/form-data", "application/x-www-form-urlencoded"):
        форма = await request.form(max_files=2, max_fields=200)
        for имя, значение in форма.multi_items():
            if isinstance(значение, UploadFile):
                if файл is None and (значение.filename or "").strip():
                    файл = значение
                continue
            if имя != "api_key":
                поля[имя] = значение
    elif вид == "application/json":
        try:
            данные = json.loads((await request.body()) or b"{}")
        except ValueError as exc:
            raise ConfigError(f"Тело запроса не читается как JSON: {exc}") from exc
        if not isinstance(данные, dict):
            raise ConfigError("Тело запроса должно быть объектом JSON.")
        поля.update({к: з for к, з in данные.items() if к != "api_key"})
    else:
        # Без Content-Type: так приходит CURL() диалплана, когда в CURLOPT
        # задан хоть один свой заголовок. Тело — та же форма.
        тело = await request.body()
        if тело:
            try:
                текст = тело.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ConfigError("Тело запроса — не текст формы.",
                                  hint="Пришлите поля формой или JSON.") from exc
            поля.update({к: з for к, з in parse_qsl(текст, keep_blank_values=False)
                         if к != "api_key"})
    return поля, файл, форма


def _сохранить(state: Any, файл: UploadFile, расширение: str) -> tuple[Path, int]:
    """Запись из запроса — в каталог загрузок, с пределом размера.

    Тело уже принято (Starlette разбирает форму до обработчика), а предел
    тела в целом держит прослойка приложения. Здесь — предел на сам файл:
    без него запись в полтора предела проходила бы, если поля формы малы.
    """
    # Через float: предел бывает дробным (0.5 МБ на пробном стенде), и int()
    # превращал его в ноль — любая запись была бы «больше предела».
    предел = int(float(state.settings.get("max_upload_mb") or 2048) * 1024 * 1024)
    цель = state.settings.paths.uploads / f"{new_id('pbx')}{расширение}"
    размер = 0
    try:
        файл.file.seek(0)
        with цель.open("wb") as выход:
            while True:
                кусок = файл.file.read(1 << 20)
                if not кусок:
                    break
                размер += len(кусок)
                if размер > предел:
                    выход.close()
                    цель.unlink(missing_ok=True)
                    ошибка = FileTooLarge(размер / 1048576, предел / 1048576)
                    ошибка.message = f"Запись больше предела в {предел / 1048576:g} МБ."
                    ошибка.hint = ("Поднимите «Предел размера загрузки» или пишите "
                                   "записи в сжатом формате.")
                    raise ошибка
                выход.write(кусок)
    except OSError as exc:
        цель.unlink(missing_ok=True)
        raise StorageError(f"Не удалось сохранить запись: {exc}") from exc
    return цель, размер


def _строка(state: Any, ключ: str) -> dict[str, Any] | None:
    строка = state.db.query_one(
        "SELECT job_id, skipped, recording FROM calls WHERE uniqueid=?", (ключ,))
    return dict(строка) if строка else None


def _принять(state: Any, principal: Principal, сырые: dict[str, Any],
             загрузка: UploadFile | None, адрес: str) -> tuple[int, dict[str, Any]]:
    """Весь приём одного звонка; (код ответа, тело). Работает в потоке.

    Каждый исход — и удачный, и отказ — попадает в журнал приёма: при
    наладке диалплана это единственное место, где видно, что станция
    прислала и что ей ответили.
    """
    поля, лишние = Д.разобрать_поля(сырые)
    проба = Д.да(поля.get("dry_run"))
    отметка: dict[str, Any] = {
        "ip": адрес, "key": principal.name, "station": "",
        "uniqueid": поля.get("uniqueid", "")[:64], "src": поля.get("src", ""),
        "dst": поля.get("dst", ""), "agent": поля.get("agent", ""),
        "direction": поля.get("direction", ""), "bytes": 0,
        # Способ — сразу, по самому запросу: и отказ, и повтор показываются в
        # журнале с тем, как станция пыталась передать запись.
        "mode": ("upload" if загрузка is not None else
                 "path" if поля.get("recording") else "none")}
    сохранённый: Path | None = None
    try:
        станция = Д.выбрать_станцию(state.settings, поля.get("station", ""))
        отметка["station"] = станция.id
        if not state.settings.get("telephony_enabled", False):
            raise ConfigError(
                "Забор записей с АТС выключен.",
                hint="Включите параметр «Забирать записи с АТС» (telephony_enabled) "
                     "в разделе «Настройки» или кнопкой в разделе «Asterisk: диалплан».")
        if not станция.enabled:
            raise ConfigError(f"Станция «{станция.name}» выключена.",
                              hint="Включите её в разделе «АТС».")
        uid = поля.get("uniqueid", "")
        if not uid:
            raise ConfigError("У звонка нет идентификатора (uniqueid).",
                              hint="Передайте ${UNIQUEID} полем uniqueid.")
        if not _ПОХОЖ_НА_UID.match(uid):
            raise ConfigError(
                f"Недопустимый идентификатор звонка «{uid[:64]}».",
                hint="Разрешены латиница, цифры, точка, двоеточие, @, дефис и "
                     "подчёркивание — как в ${UNIQUEID}.")
        есть_файл = загрузка is not None
        звонок, беды = Д.собрать_звонок(поля, станция, есть_запись=есть_файл)
        ключ = f"{станция.id}:{звонок.uniqueid}"
        импортёр = state.telephony.импортёр(станция.id)
        if импортёр is None:                                  # станцию только что убрали
            raise ConfigError(f"Станция «{станция.name}» не найдена.")
        владелец = импортёр._владелец(звонок)
        отметка.update({"direction": звонок.direction, "agent": звонок.agent,
                        "src": звонок.src, "dst": звонок.dst})
        тело: dict[str, Any] = {
            "ok": True, "uniqueid": звонок.uniqueid, "key": ключ, "station": станция.id,
            "direction": звонок.direction, "agent": звонок.agent, "owner": владелец,
            "warnings": беды, "ignored": лишние}

        прежняя = _строка(state, ключ)
        отложен = bool(прежняя) and str(прежняя.get("skipped") or "").startswith(
            state.db.ОТЛОЖЕН)
        # Запись можно дослать к звонку, который лёг без неё: сценарий на 413
        # и 415 шлёт звонок без файла, и без этого правила запись потом не
        # принималась уже ничем — звонок навсегда оставался «нет записи».
        if (прежняя and not отложен and not прежняя.get("job_id")
                and (загрузка is not None or поля.get("recording"))
                and str(прежняя.get("skipped") or "").startswith(ДОСЛАТЬ_МОЖНО)):
            отложен = True
        if прежняя and not отложен and проба:
            тело.update({"result": "dry_run", "dry_run": True, "would": "skip",
                         "reason": "уже импортирован", "job_id": прежняя.get("job_id")})
            Д.журнал.отметить(**отметка, result="dry_run",
                              reason="был бы пропущен: уже импортирован")
            return 200, тело
        if прежняя and not отложен:
            тело.update({"result": "duplicate", "skipped": "уже импортирован",
                         "job_id": прежняя.get("job_id")})
            Д.журнал.отметить(**отметка, result="duplicate", reason="уже импортирован",
                              job_id=прежняя.get("job_id"))
            return 200, тело

        путь: Path | None = None
        готов = False
        if загрузка is not None:
            расширение = Д.проверить_расширение(Path(str(загрузка.filename)).name)
            готов = True
            if not проба:
                сохранённый, размер = _сохранить(state, загрузка, расширение)
                путь = сохранённый
                отметка["bytes"] = размер
                тело["bytes"] = размер
        elif поля.get("recording"):
            путь = Д.путь_записи(станция, поля["recording"])
            готов = Д.да(поля.get("ready"))
        else:
            # Ни файла, ни пути. С каталогом записей сервер поищет запись по
            # идентификатору — после выдержки, как для журнала. Без каталога
            # искать негде, и ждать нечего: звонок сразу встаёт в журнал.
            готов = станция.путь("recordings_dir") is None

        if проба:
            тело.update({"result": "dry_run", "dry_run": True,
                         **Д.решение_пробы(звонок, станция,
                                           есть_запись=путь is not None or есть_файл,
                                           владелец=владелец)})
            if путь is not None:
                тело["recording_exists"] = путь.is_file()
            Д.журнал.отметить(**отметка, result="dry_run",
                              reason=(f"был бы пропущен: {тело['reason']}" if тело.get("reason")
                                      else "был бы поставлен"))
            return 200, тело

        if путь is not None:
            звонок.recording = str(путь)
            if готов and not (звонок.billsec or звонок.duration) and путь.is_file():
                # Длительность не назвали (команда MixMonitor её не знает) —
                # меряем саму запись: по ней работают порог «короткий» и
                # сводки часов разговора.
                from ..telephony.importer import _длительность_файла  # noqa: PLC0415

                звонок.duration = звонок.billsec = _длительность_файла(путь)
        if not звонок.started_at:
            # Обращение приходит в конце разговора: начало — «сейчас минус
            # длительность». Ноль оставлять нельзя: звонок не попал бы ни в
            # один период раздела «Телефония».
            звонок.started_at = time.time() - float(звонок.duration or звонок.billsec or 0)

        # Под замком станции: её поток прямо сейчас может дозабирать этот же
        # отложенный звонок, и без замка одна запись ушла бы двумя заданиями.
        from ..telephony.importer import ОчередьОтказала  # noqa: PLC0415

        with импортёр._заход:
            try:
                почему = импортёр._взять(звонок, отложенный=отложен, готов=готов,
                                         разметка=False)
            except ОчередьОтказала:
                # Очередь полна или кончился диск: звонок уже отложен без
                # срока, и поток станции поставит его, когда место появится.
                # Для станции это не отказ — повторять ей нечего.
                почему = "очередь отказала"
        строка = _строка(state, ключ) or {}
        if почему is None:
            тело.update({"result": "queued", "job_id": строка.get("job_id")})
            Д.журнал.отметить(**отметка, result="queued", job_id=строка.get("job_id"))
            log.info("Диалплан «%s»: звонок %s поставлен (%s)", станция.name,
                     звонок.uniqueid, строка.get("job_id"))
            return 200, тело
        if str(строка.get("skipped") or "").startswith(state.db.ОТЛОЖЕН):
            тело.update({"result": "deferred", "deferred": почему})
            Д.журнал.отметить(**отметка, result="deferred", reason=почему)
            return 202, тело
        # Окончательный пропуск: присланный файл больше не нужен никому.
        if сохранённый is not None:
            сохранённый.unlink(missing_ok=True)
            if строка.get("recording") == str(сохранённый):
                state.db.save_call(ключ, skipped=str(строка.get("skipped") or почему),
                                   recording="")
            сохранённый = None
        тело.update({"result": "duplicate" if почему == "уже импортирован" else "skipped",
                     "skipped": почему})
        Д.журнал.отметить(**отметка, result=тело["result"], reason=почему)
        return 200, тело
    except ASRHubError as exc:
        if сохранённый is not None:
            сохранённый.unlink(missing_ok=True)
        Д.журнал.отметить(**отметка, result="rejected", reason=exc.message,
                          code=exc.code, status=exc.http_status)
        raise
    except Exception as exc:
        if сохранённый is not None:
            сохранённый.unlink(missing_ok=True)
        Д.журнал.отметить(**отметка, result="rejected", reason=f"сбой сервера: {exc}",
                          code="internal_error", status=500)
        raise


#: Тело запроса для описания API: поля читаются из запроса вручную (форма,
#: форма без Content-Type, JSON, адрес), и FastAPI сам их не увидит.
_ПОЛЯ_ОПИСАНИЯ = {
    "uniqueid": {"type": "string", "description": "${UNIQUEID} — обязательно"},
    "station": {"type": "string", "description": "идентификатор или имя станции приёма"},
    "file": {"type": "string", "format": "binary", "description": "файл записи"},
    "recording": {"type": "string", "description": "путь к записи на общем диске"},
    "ready": {"type": "string", "description": "1 — запись уже закрыта"},
    "src": {"type": "string", "description": "номер звонящего"},
    "dst": {"type": "string", "description": "набранный номер"},
    "agent": {"type": "string", "description": "добавочный сотрудника"},
    "direction": {"type": "string", "description": "in, out, internal"},
    "queue": {"type": "string", "description": "имя очереди"},
    "disposition": {"type": "string", "description": "ANSWERED, NO ANSWER, … или DIALSTATUS"},
    "answered": {"type": "string", "description": "1 или 0"},
    "billsec": {"type": "integer", "description": "длительность разговора, с"},
    "duration": {"type": "integer", "description": "длительность звонка, с"},
    "started_at": {"type": "string", "description": "секунды эпохи или ГГГГ-ММ-ДД ЧЧ:ММ:СС"},
    "userfield": {"type": "string", "description": "метка для поиска"},
    "dry_run": {"type": "string", "description": "1 — проба без записи в архив"},
}


@router.post("/call", summary="Звонок из диалплана Asterisk", openapi_extra={
    "requestBody": {"content": {
        "multipart/form-data": {"schema": {"type": "object", "required": ["uniqueid"],
                                           "properties": _ПОЛЯ_ОПИСАНИЯ}},
        "application/x-www-form-urlencoded": {"schema": {
            "type": "object", "required": ["uniqueid"],
            "properties": {к: з for к, з in _ПОЛЯ_ОПИСАНИЯ.items() if к != "file"}}},
        "application/json": {"schema": {
            "type": "object", "required": ["uniqueid"],
            "properties": {к: з for к, з in _ПОЛЯ_ОПИСАНИЯ.items() if к != "file"}}},
    }}})
async def call(request: Request,
               station: str | None = Query(None, description="Станция приёма — можно и в адресе: "
                                                             "так короче строка CURL()"),
               dry_run: str | None = Query(None, description="1 — проба без записи в архив"),
               ) -> JSONResponse:
    """Принимает звонок от станции: поля и запись (файлом или путём).

    Ответы: 200 — поставлен в очередь, пропущен по порогам станции или уже
    был; 202 — принят, но запись на общем диске ещё пишется (её дозаберёт
    поток станции). Ошибки — обычные ошибки API: 400 (настройка), 401 и 403
    (ключ), 413 и 415 (запись), 429 (предел частоты). Сценарий отправки
    повторяет запрос только на 408, 409, 425, 429 и 5xx.

    `station` и `dry_run` объявлены здесь только для описания API: все поля
    из адреса читаются вместе с остальными в `_прочитать`.
    """
    state = get_state(request)
    адрес = _адрес_клиента(request)
    try:
        principal = authenticate(request, x_api_key=request.headers.get("X-API-Key"),
                                 authorization=request.headers.get("Authorization"))
        require_write(principal)
    except ASRHubError as exc:
        Д.журнал.отметить(ip=адрес, key="", station="", result="rejected",
                          uniqueid=str(request.query_params.get("uniqueid") or "")[:64],
                          reason=exc.message, code=exc.code, status=exc.http_status)
        raise error_response(exc) from exc
    форма = None
    try:
        try:
            сырые, загрузка, форма = await _прочитать(request)
        except ASRHubError as exc:
            Д.журнал.отметить(ip=адрес, key=principal.name, station="", result="rejected",
                              reason=exc.message, code=exc.code, status=exc.http_status)
            raise
        код, тело = await run_in_threadpool(_принять, state, principal, сырые, загрузка,
                                            адрес)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    finally:
        if форма is not None:
            await форма.close()
    return JSONResponse(тело, status_code=код)


# ---------------------------------------------------------------------------
# Раздел «Asterisk: диалплан»
# ---------------------------------------------------------------------------

def _адрес_сервера(request: Request, state: Any) -> str:
    """Адрес, по которому станция найдёт сервер: общий адрес или этот запрос."""
    общий = str(state.settings.get("public_url") or "").strip().rstrip("/")
    return общий or str(request.base_url).rstrip("/")


@router.get("/status", summary="Приём из диалплана: станции, счётчики, журнал")
def status(request: Request,
           station: str = Query("", description="Только эта станция"),
           limit: int = Query(50, ge=1, le=200, description="Сколько обращений журнала"),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Всё для раздела одним ответом. Только администратору: в журнале
    обращений — номера телефонов и адреса станций."""
    require_admin(principal)
    state = get_state(request)
    from ..telephony import stations as С  # noqa: PLC0415

    свои = Д.станции_приёма(state.settings)
    станции = []
    for с in свои:
        данные = с.to_dict(for_admin=True)
        данные["intake"] = Д.журнал.свод(с.id)
        данные["calls"] = state.db.call_counts(station=с.id)
        станции.append(данные)
    return {
        "enabled": bool(state.settings.get("telephony_enabled", False)),
        "url": _адрес_сервера(request, state),
        "endpoint": К.ПРИЁМ,
        "stations": станции,
        "other_stations": [{"id": с.id, "name": с.name, "source": с.source}
                           for с in С.список(state.settings) if с.source != Д.ИСТОЧНИК],
        "summary": Д.журнал.свод(station) if station else Д.журнал.свод(),
        "recent": Д.журнал.последние(station=station, limit=limit),
        "rate_limit_per_minute": int(state.settings.get("rate_limit_per_minute") or 0),
        "version": str(state.version),
    }


@router.delete("/log", summary="Очистить журнал обращений из диалплана")
def clear_log(request: Request,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    Д.журнал.сбросить()
    return {"ok": True}


@router.get("/config", summary="Готовые файлы настройки Asterisk для chan_sip и chan_pjsip")
def config(request: Request,
           driver: str = Query("pjsip", description="pjsip или sip"),
           mode: str = Query("upload", description="upload — файлом, path — путём"),
           station: str = Query("", description="Идентификатор станции приёма"),
           url: str = Query("", description="Адрес сервера; пусто — адрес этого запроса"),
           base: str = Query("/var/spool/asterisk/monitor",
                             description="Каталог MixMonitor на станции"),
           format: str = Query("wav", description="wav, WAV или gsm"),  # noqa: A002
           key_in: str = Query("header", description="Ключ в заголовке или в адресе"),
           trunk: str = Query("provider", description="Имя транка в примерах"),
           internal_len: int = Query(3, description="Длина внутреннего номера в примерах"),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_write(principal)
    state = get_state(request)
    try:
        п = К.параметры(driver=driver, mode=mode, station=station,
                        url=url or _адрес_сервера(request, state), base=base,
                        format=format, key_in=key_in, trunk=trunk,
                        internal_len=internal_len)
    except ValueError as exc:
        raise error_response(ConfigError(f"Параметры настройки: {exc}.")) from exc
    return К.собрать(п, версия=str(state.version))


@router.get("/asrhub-dialplan", summary="Сценарий отправки звонков для станции")
def script(request: Request,
           station: str = Query("", description="Станция, которую подставить в сценарий"),
           principal: Principal = Depends(authenticate)) -> Response:
    """Сценарий с подставленными адресом сервера, станцией и версией."""
    require_write(principal)
    state = get_state(request)
    if station and not К._СТАНЦИЯ.match(station):
        raise error_response(ConfigError("Недопустимый идентификатор станции."))
    try:
        текст = СЦЕНАРИЙ.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_response(ASRHubError(
            "Сценарий отправки не найден в установке сервера.",
            hint="Похоже, сервер обновляли частично: нет файла agent/asrhub-dialplan.")) from exc
    адрес = _адрес_сервера(request, state)
    текст = (текст.replace("@@SERVER_URL@@", адрес)
             .replace("@@STATION@@", station)
             .replace("@@VERSION@@", str(state.version)))
    return Response(текст, media_type="text/x-shellscript; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="asrhub-dialplan"'})
