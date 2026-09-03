"""Маршруты работы с заданиями: загрузка, очередь, результаты, управление."""
from __future__ import annotations

import json
import mimetypes
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from .. import catalog
from ..config import parse_scalar
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

    ascii_name = filename.encode("ascii", "replace").decode("ascii").replace("?", "_")
    quoted = quote(filename, safe="")
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


#: Поля формы, которые обрабатываются отдельно и параметрами не являются.
_RESERVED_FORM_FIELDS = frozenset({
    "file", "files", "settings", "priority", "group_id", "tags",
    "reference_text", "webhook_url",
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
                 "reference_text, webhook_url."))
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

    state = get_state(request)
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
            raise error_response(QuotaExceeded(kind, round(pending[kind], 3), limit))


@router.post("", summary="Поставить файл в очередь")
async def create_job(
    request: Request,
    file: UploadFile = File(..., description="Аудио- или видеофайл"),
    settings: str | None = Form(default=None, description="JSON с параметрами задания"),
    priority: int | None = Form(default=None),
    group_id: str | None = Form(default=None),
    tags: str = Form(default=""),
    reference_text: str = Form(default=""),
    webhook_url: str = Form(default=""),
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
        check_quota(request, principal, incoming_bytes=size,
                    incoming_audio_s=_probe_duration(target))
        # Значения из settings перекрывают одноимённые поля формы: явный JSON
        # выражает намерение точнее, чем разрозненные поля.
        overrides = {**await _form_overrides(request), **_parse_settings(settings)}
        merged = state.settings.merged(overrides)
        job = state.queue.submit(
            file_path=target, filename=filename, settings=merged,
            owner=principal.name, api_key_name=principal.name,
            priority=priority, group_id=group_id, source="web",
            tags=tags, reference_text=reference_text, webhook_url=webhook_url)
    except Exception:
        # Ловим всё: ASRHubError, HTTPException от разбора полей и любую
        # неожиданную ошибку — файл не должен пережить неудачный запрос.
        target.unlink(missing_ok=True)
        raise
    return job


@router.post("/batch", summary="Поставить несколько файлов одной группой")
async def create_batch(
    request: Request,
    files: list[UploadFile] = File(...),
    settings: str | None = Form(default=None),
    priority: int | None = Form(default=None),
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
                        incoming_audio_s=_probe_duration(target))
            job = state.queue.submit(
                file_path=target, filename=filename, settings=merged,
                owner=principal.name, api_key_name=principal.name,
                priority=priority, group_id=group, source="web-batch")
            created.append(job)
        except ASRHubError as exc:
            target.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": exc.message})
        except OSError as exc:
            target.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": str(exc)})
        finally:
            await item.close()

    return {"group_id": group, "created": len(created), "jobs": created, "errors": errors}


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
    jobs = state.db.list_jobs(status=statuses, owner=scope, model=model, group_id=group_id,
                              search=search, since=since, limit=limit, offset=offset,
                              order=order, light=light)
    return {
        "items": jobs,
        "total": state.db.count_jobs(status=statuses, owner=scope),
        "limit": limit,
        "offset": offset,
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
    return job


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

    if result_dir is not None and result_dir.is_dir():
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
    merged = state.settings.merged(job.get("params") or {})
    name = Path(job.get("filename") or job_id).stem
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


@router.post("/{job_id}/retry", summary="Повторить задание")
def retry(request: Request, job_id: str,
          overrides: dict[str, Any] | None = Body(default=None),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    return state.queue.retry(job_id, overrides)


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
    job = state.queue.get(job_id)
    if job["status"] in ACTIVE_STATUSES:
        state.queue.cancel(job_id, by=principal.name)
    result_dir = job.get("result_path")
    if result_dir:
        shutil.rmtree(result_dir, ignore_errors=True)
    if job.get("file_path"):
        Path(job["file_path"]).unlink(missing_ok=True)
    state.db.delete_job(job_id)
    return {"deleted": job_id}


@router.post("/{job_id}/reference", summary="Задать эталонный текст и пересчитать WER")
def set_reference(request: Request, job_id: str,
                  text: str = Body(embed=True),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    from ..pipeline import metrics as M

    state = get_state(request)
    _owned_job(request, job_id, principal)
    require_write(principal)
    job = state.queue.get(job_id)
    detail = M.detailed(text, job.get("text") or "")
    state.db.update_job(job_id, reference_text=text, wer=detail["wer"], cer=detail["cer"])
    return {"job_id": job_id, **detail}
