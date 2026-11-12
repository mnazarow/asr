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

import contextlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from .. import resources as ресурсы
from ..db import new_id
from ..errors import ASRHubError, ConfigError, FileTooLarge, ForbiddenError, StorageError
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

#: Звонки, которые принимаются прямо сейчас («станция:идентификатор»).
#: Повтор сценария, пока первый запрос ещё идёт, получает 409 и повторит
#: позже, а не ставит второе задание. Замок всей станции на каждый приём
#: больше не берётся: под ним шли сохранение файла, ffprobe и замеры роста
#: записи, и массовый отбой (конец смены) выстраивал все обращения станции
#: в одну очередь в общем пуле потоков сервера.
_ПРИНИМАЮТСЯ: set[str] = set()
_ЗАМОК_ПРИЁМА = threading.Lock()
#: Чей ключ принимает звонки станции: kv «dialplan:key:<станция>».
КЛЮЧ_СТАНЦИИ = "dialplan:key:"


class ПриёмНедоступен(ASRHubError):
    """Забор или станция выключены — временно: повторить позже.

    Был 400, и сценарий отправки складывал звонки за время обслуживания в
    `failed/`, откуда их дошлёт только ручной `--resend-failed`. С 503 и
    `Retry-After` они остаются в `pending/` и дошлются сами.
    """

    code = "intake_disabled"
    http_status = 503
    retryable = True


def _недоступен(сообщение: str, подсказка: str) -> ПриёмНедоступен:
    ошибка = ПриёмНедоступен(сообщение, hint=подсказка)
    ошибка.details = {**(ошибка.details or {}), "retry_after_s": 300}
    return ошибка


def _проверить_квоту_станции(state: Any, principal: Principal, станция: Any) -> None:
    """Суточная квота ключа — по звонкам его станции.

    Задания звонков записываются не на ключ, а на владельца из карты
    станции (отдел, сотрудник), поэтому обычный расход ключа их не видит.
    Станция же закреплена за ключом (см. `_проверить_ключ`): её звонки за
    сутки и есть расход ключа. Раньше квота здесь не проверялась вовсе.
    """
    if principal.is_admin:
        return
    заданий = float(principal.quota_jobs_per_day or 0)
    часов = float(principal.quota_audio_hours_per_day or 0)
    if not (заданий or часов):
        return
    from ..errors import QuotaExceeded  # noqa: PLC0415

    расход = state.db.query_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(j.media_duration_s), 0) AS s "
        "FROM calls c JOIN jobs j ON j.id = c.job_id "
        "WHERE c.station=? AND j.created_at>=? AND j.status NOT IN ('cancelled', 'failed')",
        (станция.id, time.time() - 86400))
    n, s = int(расход["n"] or 0), float(расход["s"] or 0)
    if заданий and n + 1 > заданий:
        raise QuotaExceeded("jobs", n + 1, заданий)
    if часов and s / 3600 >= часов:
        raise QuotaExceeded("audio_hours", round(s / 3600, 3), часов)


def _проверить_ключ(state: Any, principal: Principal, станция: Any) -> None:
    """Звонки станции принимаются от одного ключа (и от администратора).

    Раньше станция выбиралась полем запроса, и любой ключ с правом записи
    заводил звонки — и задания — от имени чужого подразделения. Теперь
    станция закрепляется за ключом, который первым прислал ей звонок; ключи
    той же группы считаются своими. Ключ отозвали — станция переходит к
    пришедшему. Отвязать станцию может администратор в разделе «Asterisk:
    диалплан».
    """
    if principal.is_admin:
        return
    ключ = КЛЮЧ_СТАНЦИИ + станция.id
    владелец = str(state.db.get_kv(ключ) or "")
    if not владелец:
        state.db.set_kv(ключ, principal.name)
        return
    if владелец == principal.name or владелец in (principal.peers or ()):
        return
    from .routes_agent import _владелец_жив  # noqa: PLC0415

    if not _владелец_жив(state, владелец):
        log.warning("Диалплан «%s»: ключ «%s» отозван, станция переходит к «%s»",
                    станция.name, владелец, principal.name)
        state.db.set_kv(ключ, principal.name)
        return
    raise ForbiddenError(
        f"Станция «{станция.name}» принимает звонки от другого ключа доступа.",
        hint="Каждая станция шлёт звонки своим ключом. Отвязать станцию от ключа "
             "может администратор в разделе «Asterisk: диалплан».")

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
        дорожки: dict[str, UploadFile] = {}
        for имя, значение in форма.multi_items():
            if isinstance(значение, UploadFile):
                if not (значение.filename or "").strip():
                    continue
                # Раздельные дорожки MixMonitor r()/t(): клиент и оператор.
                if имя in ("file_in", "file_out"):
                    дорожки[имя] = значение
                elif файл is None:
                    файл = значение
                continue
            if имя != "api_key":
                поля[имя] = значение
        if len(дорожки) == 2:
            # Обе стороны — в одном запросе: сводятся в стерео при приёме
            # (`_сохранить_дорожки`), смешанный файл тогда не нужен.
            поля["_дорожки"] = дорожки
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


def _сохранить_дорожки(state: Any, дорожки: dict[str, UploadFile]) -> tuple[Path, int]:
    """Две дорожки MixMonitor r()/t() → одна стереозапись в каталоге загрузок.

    Левый канал — клиент (r(), что пришло в канал), правый — оператор (t(),
    что ушло из канала). Так «кто говорит» известен точно, без разделения
    по голосам: разговор разбирается по каналам, как стерео от станции.
    """
    from ..phone_compat import _merge_to_stereo  # noqa: PLC0415

    части: list[Path] = []
    try:
        for имя in ("file_in", "file_out"):
            загрузка = дорожки[имя]
            расширение = Д.проверить_расширение(Path(str(загрузка.filename)).name)
            путь, _ = _сохранить(state, загрузка, расширение)
            части.append(путь)
        цель = Path(state.settings.paths.uploads) / f"{new_id('up')}.wav"
        _merge_to_stereo(части[0], части[1], цель)
        return цель, цель.stat().st_size
    finally:
        for часть in части:
            часть.unlink(missing_ok=True)


#: Сколько ждать сервер записей по ссылке.
СРОК_СКАЧИВАНИЯ_С = 120


def _скачать(state: Any, адрес: str) -> tuple[Path, int]:
    """Запись по ссылке (`recording_url`) — в каталог загрузок.

    Только http и https; адрес и каждое перенаправление проверяются, как у
    обратного вызова (`check_outbound_url`): внутренние адреса — лишь при
    `recording_url_allow_internal`. Предел — `max_upload_mb`; расширение —
    по пути ссылки или по Content-Type.
    """
    from ..job_queue import check_outbound_url, открыватель_наружу  # noqa: PLC0415

    внутрь = bool(state.settings.get("recording_url_allow_internal", False))
    адрес = str(адрес or "").strip()
    check_outbound_url(адрес, allow_internal=внутрь)
    предел = int(float(state.settings.get("max_upload_mb") or 2048) * 1024 * 1024)
    import urllib.error  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415
    from urllib.parse import urlparse  # noqa: PLC0415

    запрос = urllib.request.Request(адрес, headers={"User-Agent": "ASR-Hub"})
    try:
        ответ = открыватель_наружу(allow_internal=внутрь).open(
            запрос, timeout=СРОК_СКАЧИВАНИЯ_С)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise _недоступен(f"Запись по ссылке не скачалась: {exc}",
                          "Проверьте, что ссылка открывается с сервера ASR Hub. "
                          "Запрос будет повторён.") from exc
    with ответ:
        if getattr(ответ, "status", 200) >= 400:
            raise _недоступен(f"Сервер записей ответил {ответ.status}.",
                              "Проверьте ссылку на запись.")
        расширение = Path(urlparse(адрес).path).suffix.lower()
        if расширение not in Д.ЗВУК:
            вид = str(ответ.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            расширение = {"audio/wav": ".wav", "audio/x-wav": ".wav", "audio/wave": ".wav",
                          "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/ogg": ".ogg",
                          "audio/flac": ".flac", "audio/x-flac": ".flac",
                          "audio/mp4": ".m4a"}.get(вид, "")
        Д.проверить_расширение(f"запись{расширение}" if расширение else адрес[-60:])
        цель = state.settings.paths.uploads / f"{new_id('pbx')}{расширение}"
        размер = 0
        try:
            with цель.open("wb") as выход:
                while True:
                    кусок = ответ.read(1 << 20)
                    if not кусок:
                        break
                    размер += len(кусок)
                    if размер > предел:
                        raise FileTooLarge(размер / 1048576, предел / 1048576)
                    выход.write(кусок)
        except BaseException:
            цель.unlink(missing_ok=True)
            raise
    return цель, размер


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
    поля, лишние = Д.разобрать_поля({к: з for к, з in сырые.items() if к != "_дорожки"})
    проба = Д.да(поля.get("dry_run"))
    отметка: dict[str, Any] = {
        "ip": адрес, "key": principal.name, "station": "",
        "uniqueid": поля.get("uniqueid", "")[:64], "src": поля.get("src", ""),
        "dst": поля.get("dst", ""), "agent": поля.get("agent", ""),
        "direction": поля.get("direction", ""), "bytes": 0,
        # Способ — сразу, по самому запросу: и отказ, и повтор показываются в
        # журнале с тем, как станция пыталась передать запись.
        "mode": ("upload" if загрузка is not None else
                 "path" if поля.get("recording") else
                 "url" if поля.get("recording_url") else "none")}
    сохранённый: Path | None = None
    занят_ключ = ""
    try:
        станция = Д.выбрать_станцию(state.settings, поля.get("station", ""))
        отметка["station"] = станция.id
        if not state.settings.get("telephony_enabled", False):
            raise _недоступен(
                "Забор записей с АТС выключен.",
                "Включите параметр «Забирать записи с АТС» (telephony_enabled) "
                "в разделе «Настройки» или кнопкой в разделе «Asterisk: диалплан». "
                "Сценарий отправки держит звонки у себя и дошлёт их сам.")
        if not станция.enabled:
            raise _недоступен(f"Станция «{станция.name}» выключена.",
                              "Включите её в разделе «АТС». Сценарий отправки держит "
                              "звонки у себя и дошлёт их сам.")
        _проверить_ключ(state, principal, станция)
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

        from .routes_agent import ЗвонокПринимается  # noqa: PLC0415

        with _ЗАМОК_ПРИЁМА:
            if ключ in _ПРИНИМАЮТСЯ:
                raise ЗвонокПринимается(
                    f"Звонок {звонок.uniqueid} уже принимается другим запросом.",
                    hint="Сценарий отправки повторит его позже сам.")
            _ПРИНИМАЮТСЯ.add(ключ)
            занят_ключ = ключ
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
        дорожки = сырые.get("_дорожки") if isinstance(сырые.get("_дорожки"), dict) else None
        не_записывать = str(поля.get("record") or "").strip() in ("0", "no", "нет", "false")
        if не_записывать:
            # «Не записывать этот звонок» — сервисный код в разговоре. Звонок
            # остаётся в журнале, а запись не принимается и не хранится.
            загрузка, дорожки = None, None
            поля.pop("recording", None)
        if дорожки and not проба:
            сохранённый, размер = _сохранить_дорожки(state, дорожки)
            путь = сохранённый
            готов = True
            отметка["bytes"] = размер
            отметка["mode"] = "tracks"
            тело["bytes"] = размер
            тело["tracks"] = True
        elif загрузка is not None:
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
        elif поля.get("recording_url"):
            # Ссылка на запись: сервер скачивает её сам — с той же проверкой
            # адреса, что у обратных вызовов, и тем же пределом размера.
            готов = True
            if not проба:
                сохранённый, размер = _скачать(state, поля["recording_url"])
                путь = сохранённый
                отметка["bytes"] = размер
                тело["bytes"] = размер
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
            # Есть ли файл в каталоге записей — только администратору: иначе
            # проба с путём превращалась в способ узнавать, какие файлы лежат
            # на общем диске.
            if путь is not None and principal.is_admin:
                тело["recording_exists"] = путь.is_file()
            Д.журнал.отметить(**отметка, result="dry_run",
                              reason=(f"был бы пропущен: {тело['reason']}" if тело.get("reason")
                                      else "был бы поставлен"))
            return 200, тело

        _проверить_квоту_станции(state, principal, станция)
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

        # Замок станции — только когда он нужен: поток станции может прямо
        # сейчас дозабирать этот же отложенный звонок, а у станции с журналом
        # или AMI — импортировать тот же звонок из своего источника. Без
        # замка одна запись ушла бы двумя заданиями. Новый звонок станции
        # «вызов из диалплана» её поток не видит, пока тот не отложен, и от
        # повтора того же звонка хватает занятого ключа.
        from ..telephony.importer import ОчередьОтказала  # noqa: PLC0415

        нужен_замок = отложен or станция.source != Д.ИСТОЧНИК
        with импортёр._заход if нужен_замок else contextlib.nullcontext():
            try:
                почему = импортёр._взять(звонок, отложенный=отложен, готов=готов,
                                         разметка=False)
            except ОчередьОтказала:
                # Очередь полна или кончился диск: звонок уже отложен без
                # срока, и поток станции поставит его, когда место появится.
                # Для станции это не отказ — повторять ей нечего.
                почему = "очередь отказала"
        строка = _строка(state, ключ) or {}
        if почему is not None and не_записывать and not строка.get("job_id"):
            почему = "не записывать: попросили в разговоре"
            state.db.save_call(ключ, skipped=почему, recording="")
        elif почему is not None and поля.get("recording_rejected") \
                and not строка.get("job_id") and путь is None:
            # Сценарий отправки шлёт звонок без записи, которую сервер не
            # принял (413 или 415), а копию с записью держит в failed/:
            # пометка говорит, где искать, и позволяет дослать запись.
            код = re.sub(r"[^0-9]", "", str(поля.get("recording_rejected")))[:3] or "?"
            почему = f"сервер не принял запись (код {код}): копия звонка — в failed/ станции"
            state.db.save_call(ключ, skipped=почему)
        метка = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_.:+-]", "", str(поля.get("tag") or ""))[:64]
        if метка and строка.get("job_id"):
            задание = state.db.get_job(str(строка["job_id"])) or {}
            метки = [м for м in str(задание.get("tags") or "").split(",") if м]
            if метка not in метки:
                state.db.update_job(str(строка["job_id"]), tags=",".join([*метки, метка]))
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
    finally:
        if занят_ключ:
            with _ЗАМОК_ПРИЁМА:
                _ПРИНИМАЮТСЯ.discard(занят_ключ)


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
    "tag": {"type": "string", "description": "метка звонка — в метки задания"},
    "record": {"type": "string", "description": "0 — не записывать этот звонок"},
    "recording_rejected": {"type": "string",
                           "description": "код, с которым сервер не принял запись"},
    "file_in": {"type": "string", "format": "binary",
                "description": "дорожка клиента (MixMonitor r())"},
    "file_out": {"type": "string", "format": "binary",
                 "description": "дорожка оператора (MixMonitor t())"},
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


#: Ключ пульса сценария отправки в таблице kv.
КЛЮЧ_ПУЛЬСА = "dialplan:pulse:"


@router.post("/pulse", summary="Пульс сценария отправки: жив, сколько ждёт в очереди")
async def pulse(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сценарий asrhub-dialplan присылает его при каждой досылке (cron, раз
    в пять минут): сколько звонков ждёт в очереди повтора, сколько отвергнуто,
    возраст самого старого. По нему раздел и тревоги отличают «станция
    молчит» от «звонков нет»."""
    require_write(principal)
    state = get_state(request)
    поля, _файл, форма = await _прочитать(request)
    try:
        станция = Д.выбрать_станцию(state.settings, str(поля.get("station") or ""))
    except ASRHubError as exc:
        raise error_response(exc) from exc
    finally:
        if форма is not None:
            await форма.close()
    try:
        await run_in_threadpool(_проверить_ключ, state, principal, станция)
    except ASRHubError as exc:
        raise error_response(exc) from exc

    def число(имя: str) -> int:
        try:
            return max(0, min(int(float(str(поля.get(имя) or 0))), 10**9))
        except ValueError:
            return 0

    запись = {"at": time.time(), "pending": число("pending"), "failed": число("failed"),
              "oldest_s": число("oldest_s"), "sent": число("sent"),
              "version": re.sub(r"[^0-9A-Za-z.+-]", "", str(поля.get("version") or ""))[:32],
              "ip": _адрес_клиента(request), "key": principal.name}
    await run_in_threadpool(state.db.set_kv, КЛЮЧ_ПУЛЬСА + станция.id, запись)
    return {"ok": True, "station": станция.id}


def пульс(state: Any, станция_id: str) -> dict[str, Any]:
    """Последний пульс сценария станции; {} — не было ни одного."""
    данные = state.db.get_kv(КЛЮЧ_ПУЛЬСА + станция_id) or {}
    return dict(данные) if isinstance(данные, dict) else {}


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
        данные["intake_days"] = Д.журнал.по_дням(с.id, дней=7)
        данные["pulse"] = пульс(state, с.id)
        данные["calls"] = state.db.call_counts(station=с.id)
        # Ключ, за которым закреплена станция (см. `_проверить_ключ`).
        данные["intake_key"] = str(state.db.get_kv(КЛЮЧ_СТАНЦИИ + с.id) or "")
        станции.append(данные)
    return {
        "enabled": bool(state.settings.get("telephony_enabled", False)),
        "url": _адрес_сервера(request, state),
        "endpoint": К.ПРИЁМ,
        "stations": станции,
        "other_stations": [{"id": с.id, "name": с.name, "source": с.source}
                           for с in С.список(state.settings) if с.source != Д.ИСТОЧНИК],
        "summary": Д.журнал.свод(station) if station else Д.журнал.свод(),
        "days": Д.журнал.по_дням(station, дней=14),
        "recent": Д.журнал.последние(station=station, limit=limit),
        "rate_limit_per_minute": int(state.settings.get("rate_limit_per_minute") or 0),
        "version": str(state.version),
    }


@router.delete("/key/{station_id}", summary="Отвязать станцию от ключа доступа")
def unbind_key(station_id: str, request: Request,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Станция примет звонки от следующего ключа, который их пришлёт."""
    require_admin(principal)
    state = get_state(request)
    было = str(state.db.get_kv(КЛЮЧ_СТАНЦИИ + station_id) or "")
    state.db.set_kv(КЛЮЧ_СТАНЦИИ + station_id, "")
    if было:
        state.db.add_event(None, "settings_changed",
                           f"Диалплан: станция {station_id} отвязана от ключа «{было}»")
    return {"ok": True, "station": station_id, "was": было}


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
           # Строкой, а не bool: страница шлёт «tracks=» пустым, когда флажок
           # снят, и bool-параметр отвечал на это 422 — раздел не собирал файлы.
           tracks: str = Query("", max_length=8, description="Писать стороны раздельно: "
                                                             "MixMonitor r()/t() (1/yes/true)"),
           norec_code: str = Query("", max_length=4,
                                   description="Код «не записывать» в разговоре, например *7"),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_write(principal)
    state = get_state(request)
    try:
        п = К.параметры(driver=driver, mode=mode, station=station,
                        url=url or _адрес_сервера(request, state), base=base,
                        format=format, key_in=key_in, trunk=trunk,
                        internal_len=internal_len, tracks=tracks, norec_code=norec_code)
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
    путь = СЦЕНАРИЙ if СЦЕНАРИЙ.is_file() else (
        ресурсы.файл("agent", СЦЕНАРИЙ.name, переменная="ASRHUB_AGENT_DIR") or СЦЕНАРИЙ)
    try:
        текст = путь.read_text(encoding="utf-8")
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
