"""Маршруты в том виде, в каком их принимает phone_asr.

Смысл раздела — не новый способ работы, а возможность не переписывать то,
что уже работает. Приложение, которое сегодня шлёт разговоры в phone_asr,
переключается на ASR Hub сменой адреса: путь, имена полей, код ответа и
обратный вызов совпадают дословно.

Внутри это обычное задание очереди — с владельцем, квотами, приоритетом,
кешем и карточкой в интерфейсе. Совместимость касается только краёв.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .. import employees, phone_compat
from ..errors import ASRHubError, ConfigError, ForbiddenError
from ..logging_setup import get_logger
from .deps import (
    Principal,
    authenticate_body_key,
    error_response,
    get_state,
    require_write,
)

log = get_logger("api.phone")

#: Маршруты живут в корне, как у phone_asr, и продублированы под /api —
#: чтобы новые клиенты не выглядели исключением среди прочих маршрутов.
router = APIRouter(tags=["Совместимость с phone_asr"])


#: Сколько знаков номера хранить. Номер — подпись, а не данные: длиннее
#: этого приезжает только мусор, и ради него отказывать в разговоре нельзя.
ДЛИНА_НОМЕРА = 64


class ProcessCallBody(BaseModel):
    """Тело запроса. Поля и умолчания повторяют схему phone_asr.

    Сверх неё — номера разговора: кто звонил (`phone`) и кто говорил
    (`internalPhone`, добавочный сотрудника). Их шлёт интеграция поверх
    phone_asr, а phone_asr их не читал — номера терялись, и разговор,
    приехавший этим путём, не попадал ни в журнал звонков, ни в разрез по
    сотрудникам. Поля необязательные: без них всё как раньше.
    """

    #: Имя поля в схеме — `internalPhone`, как его пишет интеграция; в
    #: Python оно `internal_phone`, и по этому имени тоже принимается.
    model_config = ConfigDict(populate_by_name=True)

    call_id: str
    files: list[str]
    base_url: str
    part: int = Field(default=1, ge=1)
    total_parts: int = Field(default=1, ge=1)
    swap_sides: bool = False
    #: Ключ доступа прямо в теле — так его шлёт phone_asr. Заголовок и
    #: параметр адреса тоже принимаются; в задание и в журнал он не попадает.
    api_key: str = ""
    #: Кто звонил — номер абонента.
    phone: str = Field(default="", description="Кто звонил: номер абонента")
    #: Кто говорил — добавочный (внутренний) номер сотрудника.
    internal_phone: str = Field(default="", alias="internalPhone",
                                description="Кто говорил: добавочный номер сотрудника")
    #: Направление звонка, если интеграция его знает: входящий, исходящий,
    #: внутренний (или in, out, internal).
    direction: str = Field(default="", description="входящий, исходящий или внутренний")

    @field_validator("base_url", mode="before")
    @classmethod
    def _scheme(cls, value: str) -> str:
        return phone_compat.normalise_base_url(str(value or ""))

    @field_validator("phone", "internal_phone", "direction", mode="before")
    @classmethod
    def _подпись(cls, value: Any) -> str:
        """Номер строкой — даже если его прислали числом.

        PHP кладёт номер в JSON тем типом, каким он был в переменной, и
        `'internalPhone' => $to` приезжает то строкой «101», то числом 101.
        Строгая проверка типа отвечала бы на число отказом 422 — и разговор
        не принимался бы вовсе из-за необязательной подписи. Задание важнее
        метки: число становится строкой, пустое — пустой строкой, лишняя
        длина отрезается.
        """
        if value is None or isinstance(value, bool):
            return ""
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, (str, int, float)):
            return ""
        return str(value).strip()[:ДЛИНА_НОМЕРА]


@router.post("/process-call", status_code=status.HTTP_202_ACCEPTED,
             summary="Принять разговор на расшифровку (совместимо с phone_asr)")
def process_call(request: Request, body: ProcessCallBody,
                 principal: Principal = Depends(authenticate_body_key)) -> dict[str, str]:
    """Принимает разговор и отвечает сразу, не дожидаясь расшифровки.

    Ответ 202 означает «взято в работу», а не «готово»: результат придёт
    отдельным обращением на ваш адрес. Так устроен phone_asr, и так и должно
    быть для телефонии — разговор длится минуты, держать соединение всё это
    время незачем.
    """
    state = get_state(request)
    if not state.settings.get("phone_compat_enabled", True):
        raise error_response(ForbiddenError(
            "Приём разговоров по схеме phone_asr выключен.",
            hint="Включить: «Настройки», параметр phone_compat_enabled, или "
                 "строкой phone_compat_enabled: true в config.yaml."))
    require_write(principal)

    call = phone_compat.PhoneRequest(
        call_id=body.call_id, files=list(body.files), base_url=body.base_url,
        part=body.part, total_parts=body.total_parts, swap_sides=body.swap_sides)
    suffix = str(state.settings.get("phone_compat_callback_suffix")
                 or phone_compat.DEFAULT_CALLBACK_SUFFIX)
    target = call.target_url(suffix)

    # Проверяем адрес обратного вызова сразу: отказать в приёме честнее, чем
    # взять разговор и через минуту обнаружить, что результат некуда деть.
    try:
        state.queue._check_webhook_url(target)          # noqa: SLF001
    except ASRHubError as exc:
        raise error_response(exc) from exc
    # Суточная квота по числу заданий — тоже сразу, как у обычной загрузки.
    # Объём и длительность известны только после скачивания и проверяются
    # там же (`_accept`). Раньше этот путь квоту не проверял вовсе.
    from .routes_jobs import check_quota  # noqa: PLC0415

    check_quota(request, principal)

    # Скачивание идёт в отдельном потоке: файл лежит на чужом сервере, и ждать
    # его, держа обработчик запроса, значит отвечать 202 через полминуты.
    номера = {"phone": body.phone, "internal_phone": body.internal_phone,
              "direction": body.direction}
    threading.Thread(target=_accept, name=f"phone-{call.uuid}",
                     args=(state, call, target, principal, номера), daemon=True).start()

    return {
        "status": "accepted",
        "message": (f"Обработка звонка с ID {call.call_id} добавлена в очередь."
                    f" Результаты будут отправлены на {target}."),
    }


@router.get("/statuses", summary="Состояние принятых разговоров")
def statuses(request: Request,
             principal: Principal = Depends(authenticate_body_key)) -> list[dict[str, Any]]:
    """Те же три поля, что отдаёт phone_asr: call_id, part и состояние.

    Состояния переведены в его словарь: у нас их больше, у него четыре.
    """
    state = get_state(request)
    from .deps import scope_owner

    owner = scope_owner(principal)
    jobs = state.db.list_jobs(owner=owner, limit=500, light=False)
    result: list[dict[str, Any]] = []
    for job in jobs:
        call = (job.get("params") or {}).get("_phone")
        if not call:
            continue
        result.append({
            "call_id": call.get("call_id"),
            "part": call.get("part", 1),
            "status": _phone_status(str(job.get("status") or "")),
        })
    return result


def _phone_status(status_name: str) -> str:
    """Состояния ASR Hub в словаре phone_asr."""
    return {
        "queued": "pending",
        "retry": "pending",
        "paused": "pending",
        "running": "in_progress",
        "completed": "success",
        "failed": "failed",
        "cancelled": "failed",
    }.get(status_name, "pending")


def _accept(state: Any, call: phone_compat.PhoneRequest, target: str,
            principal: Principal, номера: dict[str, str] | None = None) -> None:
    """Скачивает запись и ставит её в обычную очередь."""
    # path_key, а не uuid: call_id приходит от клиента, и в исходном виде он
    # уводил и запись файла, и рекурсивную уборку за пределы своих каталогов.
    ключ = call.path_key
    workdir = Path(state.settings.get("temp_dir") or state.settings.paths.tmp) / \
        f"phone-{ключ}"
    try:
        limit_mb = int(state.settings.get("max_upload_mb") or 0)
        fetched = phone_compat.download(
            call.files, workdir, limit_bytes=limit_mb * 1024 * 1024,
            check_url=state.queue._check_webhook_url)      # noqa: SLF001

        uploads = Path(state.settings.paths.uploads)
        uploads.mkdir(parents=True, exist_ok=True)
        stored = uploads / f"{ключ}-{fetched.path.name}"
        # Проверка после очистки, а не вместо неё. Очистка отвечает за то,
        # что сюда не приедет «..», а эта строка — за то, что путь не увёл
        # наружу вообще никак: расширение задаёт адрес источника, и цена
        # ошибки здесь — свой файл в каталоге, откуда сервер что-то отдаёт.
        if uploads.resolve() not in stored.resolve().parents:
            raise ConfigError(
                "Имя записи уводит за пределы каталога загрузок.",
                hint="Проверьте значение call_id: в нём не должно быть "
                     "разделителей пути.")
        shutil.move(str(fetched.path), stored)
        try:
            from .routes_jobs import _probe_duration, проверить_квоту  # noqa: PLC0415

            проверить_квоту(state, principal, incoming_bytes=stored.stat().st_size,
                            incoming_audio_s=_probe_duration(stored))
            settings = _settings_for(state, call, fetched)
            job = state.queue.submit(
                file_path=stored, filename=fetched.filename, settings=settings,
                owner=principal.name, api_key_name=principal.name,
                group_id=call.call_id, source="phone", webhook_url=target)
        except BaseException:
            # Файл уже в uploads, а задания, которое на него ссылается, нет —
            # уборка по таблице заданий его не найдёт никогда. Три отказа
            # очереди «переполнена» оставляли три записи на диске навсегда.
            stored.unlink(missing_ok=True)
            raise
        log.info("Разговор %s принят как задание %s", call.uuid, job["id"],
                 extra={"job_id": job["id"]})
        _записать_звонок(state, job, call, principal, номера or {})
    except ASRHubError as exc:
        log.warning("Разговор %s не принят: %s", call.uuid, exc.message)
        _send_failure(state, call, target, exc.message)
    except Exception as exc:                              # noqa: BLE001
        log.exception("Разговор %s не принят: %s", call.uuid, exc)
        _send_failure(state, call, target, str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _записать_звонок(state: Any, job: dict[str, Any], call: phone_compat.PhoneRequest,
                     principal: Principal, номера: dict[str, str]) -> None:
    """Номера разговора — строкой в журнале звонков, связанной с заданием.

    Тот же путь, что у полей звонка в `POST /api/jobs` (`_данные_звонка`,
    `_сохранить_звонок`): строка в `calls` с `job_id`, и разговор виден в
    разделе «Телефония», в разрезе «Аналитики по сотрудникам» по оператору,
    в карточке записи — с именем сотрудника, если его добавочный есть в
    справочнике.

    Кто звонил (`phone`) — это `src`, номер звонящего. Кто говорил
    (`internalPhone`) — добавочный сотрудника: он и `agent` (по нему идут
    разрез по оператору и сверка со справочником), и `dst` (журнал
    показывает «кто → кому»). В `agent` добавочный хранится одними цифрами,
    как его пишет АТС и как его хранит справочник: «доб. 101» и «101» —
    один и тот же стол, и сравниваются они строкой.

    Номеров не прислали — строки нет, всё как раньше. Не записалось —
    задание всё равно распознаётся: это подпись, а не условие; причина
    уходит в журнал и в события задания.
    """
    from .routes_jobs import СТАНЦИЯ_API, _данные_звонка, _сохранить_звонок  # noqa: PLC0415

    звонящий = str(номера.get("phone") or "").strip()
    добавочный = str(номера.get("internal_phone") or "").strip()
    направление = str(номера.get("direction") or "").strip()
    if not (звонящий or добавочный or направление):
        return
    данные, беды = _данные_звонка({
        "caller": звонящий, "callee": добавочный,
        "agent": employees.внутренний(добавочный) or добавочный,
        "direction": направление, "call_id": call.call_id, "station": СТАНЦИЯ_API})
    # Разговор частями — несколько заданий одного звонка: ключ строки свой у
    # каждой части, а идентификатор звонка общий (как в пакетной загрузке).
    итог = _сохранить_звонок(state, job, principal, данные, беды,
                             ключ_по_заданию=call.total_parts > 1)
    сказано = ", ".join(ч for ч in (
        f"звонил {звонящий}" if звонящий else "",
        f"добавочный {данные.get('agent')}" if данные.get("agent") else "") if ч)
    if итог.get("saved"):
        state.db.add_event(job["id"], "call", f"Звонок записан в журнал: {сказано}",
                           {"uniqueid": итог.get("uniqueid"), "warnings": итог.get("warnings")})
    else:
        log.warning("Номера разговора %s не записаны: %s", call.uuid,
                    "; ".join(итог.get("warnings") or []))
        state.db.add_event(job["id"], "call_failed", "Номера разговора не записаны: "
                           + "; ".join(итог.get("warnings") or []))


def _settings_for(state: Any, call: phone_compat.PhoneRequest,
                  fetched: phone_compat.Fetched) -> dict[str, Any]:
    """Настройки задания под телефонный разговор — как их понимает phone_asr.

    Две дорожки или стерео — стороны разговора известны точно, поэтому
    разделение идёт по каналам, а не догадками. Одна моно-дорожка — сторон в
    записи не различить, включается диаризация: ровно так поступает и
    phone_asr, предупреждая, что точность будет ниже.
    """
    left, right = phone_compat.SPEAKER_LEFT, phone_compat.SPEAKER_RIGHT
    if call.swap_sides:
        left, right = right, left
    overrides: dict[str, Any] = {}
    if fetched.channels >= 2:
        overrides.update({
            "audio_channels": "split",
            "diarization_enabled": True,
            "diarization_backend": "channels",
            "speaker_names": f"{left},{right}",
        })
    else:
        overrides.update({
            "audio_channels": "mono",
            "diarization_enabled": True,
            "speaker_names": f"{left},{right}",
        })
    settings = state.settings.merged(overrides)
    # Приписку о разговоре добавляем ПОСЛЕ merged: тот пропускает только
    # ключи каталога и всё постороннее молча выбрасывает. Раньше блок уходил
    # внутрь merged и исчезал — задание считалось, а результат уходил
    # обычным телом ASR Hub вместо схемы phone_asr, то есть ровно то, ради
    # чего всё делалось, тихо не работало.
    settings["_phone"] = {
        "call_id": call.call_id, "part": call.part,
        "total_parts": call.total_parts, "base_path": call.base_path(),
        "swap_sides": call.swap_sides,
    }
    return settings


def _send_failure(state: Any, call: phone_compat.PhoneRequest, target: str,
                  message: str) -> None:
    """Сообщает об отказе тем же обратным вызовом, что и об успехе.

    Молчание здесь — худший исход: вызывающая сторона ответила 202 и ждёт
    результат, которого не будет. phone_asr в этом месте тоже шлёт callback
    со status=failed.
    """
    body = {
        "call_id": call.call_id, "base_path": call.base_path(),
        "part": call.part, "total_parts": call.total_parts,
        "true_duration": 0.001, "sentiment": "neutral", "waveforms": [],
        "formatted_dialogue": [], "transcription": "",
        "status": "failed", "error_message": message,
    }
    import urllib.error
    import urllib.request

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        target, data=payload, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "ASRHub/3.0"})
    try:
        with urllib.request.urlopen(request, timeout=15):
            pass
    except (urllib.error.URLError, OSError) as exc:
        log.warning("Отказ по разговору %s не доставлен на %s: %s",
                    call.uuid, target, exc)
