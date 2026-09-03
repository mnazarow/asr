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
from pydantic import BaseModel, Field, field_validator

from .. import phone_compat
from ..errors import ASRHubError, ForbiddenError
from ..logging_setup import get_logger
from .deps import Principal, authenticate, error_response, get_state, require_write

log = get_logger("api.phone")

#: Маршруты живут в корне, как у phone_asr, и продублированы под /api —
#: чтобы новые клиенты не выглядели исключением среди прочих маршрутов.
router = APIRouter(tags=["Совместимость с phone_asr"])


class ProcessCallBody(BaseModel):
    """Тело запроса. Поля и умолчания повторяют схему phone_asr."""

    call_id: str
    files: list[str]
    base_url: str
    part: int = Field(default=1, ge=1)
    total_parts: int = Field(default=1, ge=1)
    swap_sides: bool = False

    @field_validator("base_url", mode="before")
    @classmethod
    def _scheme(cls, value: str) -> str:
        return phone_compat.normalise_base_url(str(value or ""))


@router.post("/process-call", status_code=status.HTTP_202_ACCEPTED,
             summary="Принять разговор на расшифровку (совместимо с phone_asr)")
def process_call(request: Request, body: ProcessCallBody,
                 principal: Principal = Depends(authenticate)) -> dict[str, str]:
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
            hint="Включить: asrctl config set phone_compat_enabled true"))
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

    # Скачивание идёт в отдельном потоке: файл лежит на чужом сервере, и ждать
    # его, держа обработчик запроса, значит отвечать 202 через полминуты.
    threading.Thread(target=_accept, name=f"phone-{call.uuid}",
                     args=(state, call, target, principal), daemon=True).start()

    return {
        "status": "accepted",
        "message": (f"Обработка звонка с ID {call.call_id} добавлена в очередь."
                    f" Результаты будут отправлены на {target}."),
    }


@router.get("/statuses", summary="Состояние принятых разговоров")
def statuses(request: Request,
             principal: Principal = Depends(authenticate)) -> list[dict[str, Any]]:
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
            principal: Principal) -> None:
    """Скачивает запись и ставит её в обычную очередь."""
    workdir = Path(state.settings.get("temp_dir") or state.settings.paths.tmp) / \
        f"phone-{call.uuid}"
    try:
        limit_mb = int(state.settings.get("max_upload_mb") or 0)
        fetched = phone_compat.download(
            call.files, workdir, limit_bytes=limit_mb * 1024 * 1024,
            check_url=state.queue._check_webhook_url)      # noqa: SLF001

        uploads = Path(state.settings.paths.uploads)
        uploads.mkdir(parents=True, exist_ok=True)
        stored = uploads / f"{call.uuid}-{fetched.path.name}"
        shutil.move(str(fetched.path), stored)

        settings = _settings_for(state, call, fetched)
        job = state.queue.submit(
            file_path=stored, filename=fetched.filename, settings=settings,
            owner=principal.name, api_key_name=principal.name,
            group_id=call.call_id, source="phone", webhook_url=target)
        log.info("Разговор %s принят как задание %s", call.uuid, job["id"],
                 extra={"job_id": job["id"]})
    except ASRHubError as exc:
        log.warning("Разговор %s не принят: %s", call.uuid, exc.message)
        _send_failure(state, call, target, exc.message)
    except Exception as exc:                              # noqa: BLE001
        log.exception("Разговор %s не принят: %s", call.uuid, exc)
        _send_failure(state, call, target, str(exc))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


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
