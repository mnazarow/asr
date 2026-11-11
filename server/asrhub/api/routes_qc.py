"""Контроль качества: группы, очередь обзвона, оценки и пробный звонок.

Всё здесь — только администратору: настройки содержат учётные данные
телефона, а журнал — номера клиентов и то, что они ответили.

Сама работа — в `qc/`: телефон SIP, голос и служба опросов. Маршруты лишь
читают её состояние и передают ручные действия: сохранить группы,
прослушать фразу, позвонить на пробу, отменить опрос, внести номер в
список «не звонить».
"""
from __future__ import annotations

import asyncio
import time
import unicodedata
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from ..errors import ASRHubError, ConfigError, JobNotFound
from ..logging_setup import get_logger
from ..qc import survey as опросы
from ..qc import tts
from .deps import Principal, authenticate, error_response, get_state, require_admin

log = get_logger("qc")

router = APIRouter(prefix="/api/qc", tags=["Контроль качества"])


def _служба(request: Request, principal: Principal) -> Any:
    state = get_state(request)
    require_admin(principal)
    служба = getattr(state, "qc", None)
    if служба is None:
        raise error_response(ConfigError("Служба контроля качества не запущена."))
    return state, служба


@router.get("/status", summary="Состояние контроля качества")
async def status(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Включён ли обзвон, зарегистрирован ли телефон, чем синтезируется речь,
    сколько опросов ждёт и когда ближайший."""
    _, служба = _служба(request, principal)
    return await asyncio.to_thread(служба.состояние)


@router.get("/groups", summary="Группы контроля качества")
async def groups(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, _ = _служба(request, principal)
    try:
        группы = опросы.разобрать_группы(state.settings.get("qc_groups"))
    except ValueError as exc:
        # Испорченную настройку надо уметь открыть и поправить.
        return {"items": [], "error": str(exc), "raw": state.settings.get("qc_groups")}
    return {"items": [г.to_dict() for г in группы], "error": "",
            "defaults": {"greeting": опросы.ПРИВЕТСТВИЕ, "goodbye": опросы.ПРОЩАНИЕ}}


@router.put("/groups", summary="Сохранить группы контроля качества")
async def save_groups(request: Request, groups: list[dict[str, Any]] = Body(..., embed=True),
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Заменяет весь список групп и записывает его в `config.yaml`.

    Тело: `{"groups": [{"name": "Продажи", "extensions": "101, 110-119",
    "questions": [{"text": "…", "min": 1, "max": 5}]}]}`.
    """
    state, служба = _служба(request, principal)
    try:
        разобрано = опросы.разобрать_группы(groups)
    except ValueError as exc:
        raise error_response(ConfigError(
            f"Группы не сохранены: {exc}.",
            hint="Поправьте группу и сохраните ещё раз.")) from exc
    чистые = [г.to_dict() for г in разобрано]
    def записать() -> dict[str, Any]:
        state.settings.set("qc_groups", чистые, source="api")
        сохранено = state.settings.записать_ключи(["qc_groups"])
        state.db.add_event(None, "settings_changed",
                           f"Контроль качества: групп в настройке — {len(чистые)}")
        return сохранено

    try:
        сохранено = await asyncio.to_thread(записать)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {"items": чистые, "warnings": опросы.проверить_группы(чистые), **сохранено}


@router.post("/tts", summary="Прослушать фразу голосом опроса")
async def tts_preview(request: Request, text: str = Body(..., embed=True),
                      principal: Principal = Depends(authenticate)) -> Response:
    """Синтезирует фразу тем же путём, что перед звонком, и отдаёт WAV.

    Клиент услышит ровно это: тот же синтезатор, тот же голос, та же
    телефонная полоса 8 кГц.
    """
    _, служба = _служба(request, principal)
    try:
        фраза = await asyncio.to_thread(
            tts.приготовить, text, служба.settings, служба.каталог_звука())
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return Response(content=tts.в_wav(фраза), media_type="audio/wav",
                    headers={"Cache-Control": "no-store"})


def _имя_записи(имя: str) -> str:
    """Имя файла от пользователя — в безопасное имя в каталоге записей."""
    имя = unicodedata.normalize("NFC", Path(str(имя or "").replace("\\", "/")).name)
    имя = "".join(з if з.isalnum() or з in "-_. ()" else "_" for з in имя).strip(" .")
    return имя[-120:]


def _записи(служба: Any, группы: list[Any]) -> list[dict[str, Any]]:
    каталог = tts.каталог_записей(служба.каталог_звука())
    итог = []
    for файл in sorted(каталог.glob("*")) if каталог.is_dir() else []:
        if not файл.is_file() or not tts.имя_годится(файл.name):
            continue
        сведения = файл.stat()
        try:
            секунд = round(tts.приготовить(tts.ЗАГРУЖЕННАЯ + файл.name, служба.settings,
                                           служба.каталог_звука()).секунд, 1)
        except ASRHubError:
            секунд = None
        итог.append({"name": файл.name, "bytes": сведения.st_size,
                     "uploaded_at": сведения.st_mtime, "seconds": секунд,
                     "phrase": tts.ЗАГРУЖЕННАЯ + файл.name,
                     "groups": [г.name for г in группы if файл.name in [
                         т[len(tts.ЗАГРУЖЕННАЯ):] for т in г.фразы()
                         if т.startswith(tts.ЗАГРУЖЕННАЯ)]]})
    return итог


def _группы_тихо(state: Any) -> list[Any]:
    try:
        return опросы.разобрать_группы(state.settings.get("qc_groups"))
    except ValueError:
        return []


@router.get("/audio", summary="Загруженные аудиозаписи")
async def audio_list(request: Request,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Готовые записи диктора: любая фраза опроса вида `audio:имя`."""
    state, служба = _служба(request, principal)
    записи = await asyncio.to_thread(_записи, служба, _группы_тихо(state))
    return {"items": записи, "max_mb": tts.ПРЕДЕЛ_ЗАПИСИ_МБ, "max_seconds": tts.ПРЕДЕЛ_ЗАПИСИ_С}


@router.post("/audio", summary="Загрузить аудиозапись")
async def audio_upload(request: Request, file: UploadFile = File(...),
                       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Принимает звуковой файл (WAV, MP3, OGG, FLAC, M4A и другие), проверяет,
    что он читается, и готовит к телефону. Запись с тем же именем заменяется."""
    state, служба = _служба(request, principal)
    имя = _имя_записи(file.filename or "")
    if not tts.имя_годится(имя):
        raise error_response(ConfigError(
            f"«{file.filename}» — не звуковой файл.",
            hint="Подходят: " + ", ".join(р.lstrip(".") for р in tts.ЗВУКОВЫЕ) + "."))
    предел = tts.ПРЕДЕЛ_ЗАПИСИ_МБ * 1024 * 1024
    каталог = tts.каталог_записей(служба.каталог_звука())
    каталог.mkdir(parents=True, exist_ok=True)
    временный = каталог / f".{имя}.part"
    размер = 0
    try:
        with временный.open("wb") as выход:
            while True:
                кусок = await file.read(1024 * 1024)
                if not кусок:
                    break
                размер += len(кусок)
                if размер > предел:
                    raise ConfigError(
                        f"Файл больше {tts.ПРЕДЕЛ_ЗАПИСИ_МБ} МБ.",
                        hint="Сожмите запись (MP3, моно) или разделите её на части.")
                выход.write(кусок)
        if not размер:
            raise ConfigError("Файл пуст.")

        def принять() -> float:
            # Проверка — на временном имени: негодный файл не должен затереть
            # прежнюю запись с тем же именем.
            проба = каталог / f"проверка-{time.time_ns()}{Path(имя).suffix.lower()}"
            временный.replace(проба)
            try:
                фраза = tts.приготовить(tts.ЗАГРУЖЕННАЯ + проба.name, служба.settings,
                                        служба.каталог_звука())
                if фраза.секунд > tts.ПРЕДЕЛ_ЗАПИСИ_С:
                    raise ConfigError(
                        f"Запись длиннее {tts.ПРЕДЕЛ_ЗАПИСИ_С // 60} минут "
                        f"({фраза.секунд / 60:.1f} мин).",
                        hint="По телефону такую не дослушают: сократите запись.")
                if фраза.секунд < 0.2:
                    raise ConfigError("В записи нет звука.")
                проба.replace(каталог / имя)
                return фраза.секунд
            finally:
                проба.unlink(missing_ok=True)

        секунд = await asyncio.to_thread(принять)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    finally:
        временный.unlink(missing_ok=True)
    state.db.add_event(None, "settings_changed",
                       f"Контроль качества: загружена аудиозапись «{имя}»")
    log.info("Контроль качества: %s загрузил аудиозапись «%s» (%.0f с)",
             principal.name, имя, секунд)
    return {"name": имя, "bytes": размер, "seconds": round(секунд, 1),
            "phrase": tts.ЗАГРУЖЕННАЯ + имя}


@router.get("/audio/{name}", summary="Скачать аудиозапись")
async def audio_get(name: str, request: Request,
                    principal: Principal = Depends(authenticate)) -> FileResponse:
    _, служба = _служба(request, principal)
    файл = tts.каталог_записей(служба.каталог_звука()) / name
    if not tts.имя_годится(name) or not файл.is_file():
        ошибка = JobNotFound(name)
        ошибка.message = "Такой аудиозаписи нет."
        ошибка.hint = "Обновите список записей."
        raise error_response(ошибка)
    return FileResponse(файл, filename=name, headers={"Cache-Control": "no-store"})


@router.delete("/audio/{name}", summary="Удалить аудиозапись")
async def audio_delete(name: str, request: Request,
                       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Запись, выбранную в группе, удалить нельзя: сначала уберите её из группы."""
    state, служба = _служба(request, principal)
    файл = tts.каталог_записей(служба.каталог_звука()) / name
    if not tts.имя_годится(name) or not файл.is_file():
        ошибка = JobNotFound(name)
        ошибка.message = "Такой аудиозаписи нет."
        ошибка.hint = "Обновите список записей."
        raise error_response(ошибка)
    заняты = [з for з in _записи(служба, _группы_тихо(state)) if з["name"] == name and з["groups"]]
    if заняты:
        raise error_response(ConfigError(
            f"Запись «{name}» используется в группе «{заняты[0]['groups'][0]}».",
            hint="Уберите запись из группы, сохраните группы и удалите её."))
    файл.unlink()
    state.db.add_event(None, "settings_changed",
                       f"Контроль качества: удалена аудиозапись «{name}»")
    return {"deleted": True, "name": name}


def _образец(служба: Any) -> dict[str, Any]:
    кеш = служба.каталог_звука()
    return {**tts.сведения_образца(кеш), "engine": tts.движок(служба.settings, tts.образец(кеш)),
            "min_seconds": tts.ОБРАЗЕЦ_ОТ_С, "max_seconds": tts.ОБРАЗЕЦ_ДО_С,
            "max_mb": tts.ПРЕДЕЛ_ЗАПИСИ_МБ}


@router.get("/voice-sample", summary="Образец голоса для синтезатора")
async def voice_sample(request: Request,
                       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Загружен ли образец голоса и каким синтезатором он используется."""
    _, служба = _служба(request, principal)
    return await asyncio.to_thread(_образец, служба)


@router.post("/voice-sample", summary="Загрузить образец голоса")
async def voice_sample_upload(request: Request, file: UploadFile = File(...),
                              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Принимает запись голоса (10–30 секунд чистой речи одного человека).

    Синтезатор, умеющий говорить по образцу (Coqui XTTS или своя команда с
    `{sample}`), будет проговаривать тексты опросов этим голосом. Прежний
    образец заменяется.
    """
    state, служба = _служба(request, principal)
    имя = _имя_записи(file.filename or "")
    if not tts.имя_годится(имя):
        raise error_response(ConfigError(
            f"«{file.filename}» — не звуковой файл.",
            hint="Подходят: " + ", ".join(р.lstrip(".") for р in tts.ЗВУКОВЫЕ) + "."))
    предел = tts.ПРЕДЕЛ_ЗАПИСИ_МБ * 1024 * 1024
    каталог = tts.каталог_образца(служба.каталог_звука())
    каталог.mkdir(parents=True, exist_ok=True)
    временный = каталог / f"upload-{time.time_ns()}{Path(имя).suffix.lower()}"
    размер = 0
    try:
        with временный.open("wb") as выход:
            while True:
                кусок = await file.read(1024 * 1024)
                if not кусок:
                    break
                размер += len(кусок)
                if размер > предел:
                    raise ConfigError(f"Файл больше {tts.ПРЕДЕЛ_ЗАПИСИ_МБ} МБ.",
                                      hint="Образцу хватит 10–30 секунд речи.")
                выход.write(кусок)
        if not размер:
            raise ConfigError("Файл пуст.")
        await asyncio.to_thread(tts.принять_образец, временный, служба.каталог_звука(), имя)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    finally:
        временный.unlink(missing_ok=True)
    state.db.add_event(None, "settings_changed",
                       f"Контроль качества: загружен образец голоса «{имя}»")
    log.info("Контроль качества: %s загрузил образец голоса «%s»", principal.name, имя)
    return await asyncio.to_thread(_образец, служба)


@router.get("/voice-sample/audio", summary="Прослушать образец голоса")
async def voice_sample_audio(request: Request,
                             principal: Principal = Depends(authenticate)) -> FileResponse:
    _, служба = _служба(request, principal)
    файл = tts.образец(служба.каталог_звука())
    if файл is None:
        ошибка = JobNotFound("voice-sample")
        ошибка.message = "Образец голоса не загружен."
        ошибка.hint = "Загрузите запись голоса в разделе «Контроль качества»."
        raise error_response(ошибка)
    return FileResponse(файл, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.delete("/voice-sample", summary="Убрать образец голоса")
async def voice_sample_delete(request: Request,
                              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """После этого тексты проговаривает обычный голос синтезатора."""
    state, служба = _служба(request, principal)
    был = await asyncio.to_thread(tts.убрать_образец, служба.каталог_звука())
    if был:
        state.db.add_event(None, "settings_changed", "Контроль качества: образец голоса убран")
    return {"deleted": был, **await asyncio.to_thread(_образец, служба)}


@router.post("/check", summary="Проверить телефон: регистрация на станции")
async def check(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Регистрирует телефон на станции прямо сейчас и говорит, чем кончилось."""
    _, служба = _служба(request, principal)
    чего_нет = служба.настроен()
    if чего_нет:
        return {"ok": False, "error": f"Телефон не настроен: {чего_нет}."}
    return await asyncio.to_thread(служба.проверить_телефон)


@router.post("/test-call", summary="Пробный звонок с опросом")
async def test_call(request: Request, number: str = Body(..., embed=True),
                    group: str = Body(..., embed=True),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Звонит на номер сейчас и проводит опрос группы.

    Мимо часов обзвона, паузы между опросами и списка «не звонить»; в отчёт
    не попадает. Ход и итог — в журнале опросов, строка с пометкой «проба».
    """
    _, служба = _служба(request, principal)
    try:
        return await asyncio.to_thread(служба.пробный_звонок, number, group,
                                       owner=principal.name)
    except ASRHubError as exc:
        raise error_response(exc) from exc


def _отбор_опросов(status: str, group: str, ext: str, search: str,
                   since_days: int) -> tuple[str, list[Any]]:
    условия = ["s.created_at>=?"]
    параметры: list[Any] = [time.time() - since_days * 86400]
    for поле, значение in (("s.status", status), ("s.group_name", group), ("s.ext", ext)):
        if значение:
            условия.append(f"{поле}=?")
            параметры.append(значение)
    цифры = опросы.чистый_номер(search)
    if цифры:
        условия.append("instr(s.number, ?) > 0")
        параметры.append(цифры)
    return " AND ".join(условия), параметры


@router.get("/surveys", summary="Журнал опросов")
async def surveys(request: Request, principal: Principal = Depends(authenticate),
                  status: str = Query("", description="Состояние: ждёт, пройден, частично…"),
                  group: str = Query("", description="Имя группы"),
                  ext: str = Query("", description="Добавочный номер"),
                  search: str = Query("", description="Часть номера клиента"),
                  since_days: int = Query(30, ge=1, le=3650),
                  limit: int = Query(100, ge=1, le=500),
                  offset: int = Query(0, ge=0)) -> dict[str, Any]:
    state, _ = _служба(request, principal)
    где, параметры = _отбор_опросов(status, group, ext, search, since_days)

    def прочитать() -> dict[str, Any]:
        всего = state.db.query_one(
            f"SELECT COUNT(*) AS n FROM qc_surveys s WHERE {где}", параметры)  # noqa: S608
        # Задание исходного разговора берётся из архива звонков на момент
        # чтения: опрос заводится раньше, чем запись доходит до распознавания.
        строки = state.db.query(
            f"SELECT s.*, COALESCE(c.job_id, s.job_id) AS call_job_id "  # noqa: S608
            f"FROM qc_surveys s LEFT JOIN calls c ON c.uniqueid = s.call_key "
            f"WHERE {где} ORDER BY s.created_at DESC LIMIT ? OFFSET ?",
            (*параметры, limit, offset))
        записи = [опросы.опрос_наружу(с) for с in строки]
        люди = state.db.employees_by_ext([з["ext"] for з in записи if з.get("ext")])
        for запись in записи:
            человек = люди.get(запись.get("ext") or "") or {}
            запись["ext_name"] = " ".join(
                ч for ч in (человек.get("last_name"), человек.get("first_name")) if ч)
            запись["job_id"] = запись.pop("call_job_id", None)
        return {"items": записи, "total": int((всего or {"n": 0})["n"]),
                "limit": limit, "offset": offset}

    return await asyncio.to_thread(прочитать)


#: Колонки выгрузки опросов: (заголовок, поле).
_КОЛОНКИ_ВЫГРУЗКИ = (("Создан", "created_at"), ("Разговор", "call_at"),
                     ("Завершён", "finished_at"), ("Группа", "group_name"),
                     ("Добавочный", "ext"), ("Сотрудник", "ext_name"),
                     ("Номер клиента", "number"), ("Итог", "status"),
                     ("Попыток", "attempts"), ("Оценка", "score"), ("Оценка, %", "score_pct"),
                     ("Код SIP", "sip_code"), ("Ошибка", "error"),
                     ("Задание разговора", "job_id"))


@router.get("/surveys/export", summary="Выгрузка опросов в CSV")
async def surveys_export(request: Request, principal: Principal = Depends(authenticate),
                         status: str = Query("", description="Состояние"),
                         group: str = Query("", description="Имя группы"),
                         ext: str = Query("", description="Добавочный номер"),
                         search: str = Query("", description="Часть номера клиента"),
                         since_days: int = Query(30, ge=1, le=3650)) -> Response:
    """Тот же отбор, что у журнала, — все строки, по колонке на каждый
    вопрос группы. CSV для Excel: разделитель «;», UTF-8 с BOM."""
    state, _ = _служба(request, principal)
    где, параметры = _отбор_опросов(status, group, ext, search, since_days)

    def собрать() -> str:
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415

        строки = state.db.query(
            f"SELECT s.*, COALESCE(c.job_id, s.job_id) AS call_job_id "  # noqa: S608
            f"FROM qc_surveys s LEFT JOIN calls c ON c.uniqueid = s.call_key "
            f"WHERE {где} ORDER BY s.created_at DESC LIMIT 100000", параметры)
        записи = [опросы.опрос_наружу(с) for с in строки]
        люди = state.db.employees_by_ext([з["ext"] for з in записи if з.get("ext")])
        вопросов = max((len(з.get("answers") or []) for з in записи), default=0)
        буфер = io.StringIO()
        письмо = csv.writer(буфер, delimiter=";")
        письмо.writerow([з for з, _ in _КОЛОНКИ_ВЫГРУЗКИ]
                        + [f"Вопрос {н + 1}" for н in range(вопросов)])
        for з in записи:
            человек = люди.get(з.get("ext") or "") or {}
            з["ext_name"] = " ".join(ч for ч in (человек.get("last_name"),
                                                 человек.get("first_name")) if ч)
            з["job_id"] = з.pop("call_job_id", None) or ""
            for поле in ("created_at", "call_at", "finished_at"):
                з[поле] = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(з[поле])))
                           if з.get(поле) else "")
            ответы = [("" if о.get("digit") is None else о.get("digit"))
                      for о in (з.get("answers") or [])]
            письмо.writerow([_ячейка(з.get(поле)) for _, поле in _КОЛОНКИ_ВЫГРУЗКИ]
                            + ответы + [""] * (вопросов - len(ответы)))
        return "\ufeff" + буфер.getvalue()

    текст = await asyncio.to_thread(собрать)
    имя = time.strftime("qc-surveys-%Y%m%d.csv")
    return Response(текст, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{имя}"'})


def _ячейка(значение: Any) -> Any:
    """Значение для CSV: текст, начинающийся с «=+-@», Excel не исполняет."""
    if значение is None:
        return ""
    if isinstance(значение, str) and значение[:1] in ("=", "+", "-", "@"):
        return "'" + значение
    return значение


@router.post("/surveys/{survey_id}/cancel", summary="Отменить ждущий опрос")
async def cancel(survey_id: str, request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    _, служба = _служба(request, principal)
    if not await asyncio.to_thread(служба.отменить, survey_id):
        ошибка = JobNotFound(survey_id)
        ошибка.message = "Опрос не найден или уже не ждёт: отменить можно только ждущий."
        ошибка.hint = "Обновите журнал опросов."
        raise error_response(ошибка)
    return {"cancelled": True, "id": survey_id}


@router.get("/summary", summary="Оценки клиентов за период")
async def summary(request: Request, principal: Principal = Depends(authenticate),
                  since_days: int = Query(30, ge=1, le=3650),
                  group: str = Query("", description="Имя группы")) -> dict[str, Any]:
    """Сколько опросов, какая доля дозвона и ответа, средняя оценка — всего,
    по группам, по добавочным и по вопросам с распределением нажатий."""
    state, _ = _служба(request, principal)
    return await asyncio.to_thread(
        опросы.сводка, state.db, since=time.time() - since_days * 86400, группа=group)


@router.get("/stoplist", summary="Список «не звонить»")
async def stoplist(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, _ = _служба(request, principal)
    строки = await asyncio.to_thread(
        state.db.query, "SELECT * FROM qc_stoplist ORDER BY added_at DESC LIMIT 1000")
    return {"items": [dict(с) for с in строки]}


@router.post("/stoplist", summary="Внести номер в список «не звонить»")
async def stoplist_add(request: Request, number: str = Body(..., embed=True),
                       reason: str = Body("", embed=True),
                       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    _, служба = _служба(request, principal)
    try:
        await asyncio.to_thread(служба.в_стоп_лист, number,
                                reason or f"внёс {principal.name}")
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {"added": True, "number": опросы.чистый_номер(number)}


@router.delete("/stoplist/{number}", summary="Убрать номер из списка «не звонить»")
async def stoplist_remove(number: str, request: Request,
                          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    _, служба = _служба(request, principal)
    if not await asyncio.to_thread(служба.из_стоп_листа, number):
        ошибка = JobNotFound(number)
        ошибка.message = "Такого номера в списке «не звонить» нет."
        ошибка.hint = "Обновите список."
        raise error_response(ошибка)
    return {"removed": True, "number": опросы.чистый_номер(number)}
