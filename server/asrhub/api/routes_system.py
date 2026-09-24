"""Маршруты сервера: состояние, настройки, очередь, аналитика, журнал, ключи."""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
)

from .. import catalog, selfcheck
from .. import settings_access as S
from ..errors import ASRHubError, AuthError, ConfigError, ForbiddenError, KeyNotFound
from ..hardware import detect, recommended_settings
from ..logging_setup import counts as log_counts
from ..logging_setup import get_logger
from ..logging_setup import recent as log_recent
from ..maintenance import retention_days
from ..monitoring.collector import RUNTIME
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    scope_owner,
    token_of,
)

log = get_logger("api.settings")

router = APIRouter(prefix="/api", tags=["Сервер"])


def _health_body(state: Any) -> tuple[dict[str, Any], int]:
    """Состояние сервера и код ответа для проверки.

    Проверка обязана отвечать «не в порядке», когда работать нельзя, — иначе
    балансировщик держит в строю сервер, который принимает запросы и на
    каждом падает. Смотрим на то, без чего распознавание невозможно: базу.
    Всё остальное (нет весов модели, занята очередь) — это «занят», а не
    «сломан», и снимать такой сервер с раздачи не нужно.

    Проверка базы — «SELECT 1»: пробы ходят раз в несколько секунд, и
    дорогой запрос здесь превратился бы в постоянную нагрузку.
    """
    checks: dict[str, Any] = {}
    healthy = True
    try:
        state.db.query_one("SELECT 1 AS ok")
        checks["database"] = "ok"
    except Exception as exc:                                   # noqa: BLE001
        checks["database"] = f"недоступна: {exc}"
        healthy = False

    # Очередь может быть остановлена намеренно (пауза) — это не поломка, и
    # различать эти два случая важнее, чем свести всё к одному признаку.
    checks["queue"] = "приостановлена" if state.queue.is_paused else "работает"

    body = {
        "status": "ok" if healthy else "degraded",
        "version": state.version,
        "uptime_s": round(time.time() - state.started_at, 1),
        "queue_paused": state.queue.is_paused,
        "catalog_date": catalog.CATALOG_DATE,
        "checks": checks,
    }
    return body, 200 if healthy else 503


@router.get("/health", summary="Проверка доступности")
def health(request: Request) -> JSONResponse:
    state = get_state(request)
    body, status_code = _health_body(state)
    return JSONResponse(status_code=status_code, content=body)


#: Тот же ответ по адресу, куда пробы стучатся по умолчанию. Балансировщики,
#: docker HEALTHCHECK, uptime-мониторы и kubelet спрашивают /health, а не
#: /api/health, и настраивать это каждому — лишний шаг, на котором проверку
#: чаще всего просто не заводят.
health_router = APIRouter(tags=["Сервер"])


@health_router.get("/health", summary="Проверка доступности (без префикса)")
def health_root(request: Request) -> JSONResponse:
    """То же самое, что GET /api/health, по общепринятому адресу.

    Ключ не спрашиваем намеренно: ответ не рассказывает ничего, кроме того,
    что сервер отвечает, а проверка, требующая ключа, однажды покажет
    «сервер лёг» из-за отозванного ключа — и разбираться будут не с ключом.
    """
    state = get_state(request)
    body, status_code = _health_body(state)
    return JSONResponse(status_code=status_code, content=body)


@router.post("/auth/ticket", summary="Одноразовый билет для WebSocket")
def auth_ticket(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Выдаёт короткоживущий одноразовый билет вместо ключа в адресе.

    Браузерный WebSocket не умеет отправлять заголовки, поэтому ключ раньше
    приходилось писать в строку запроса — а она видна в истории браузера, в
    журналах обратного прокси и в поле Referer. Билет действует минуту,
    гасится при первом же использовании и не даёт доступа к HTTP-методам.
    """
    state = get_state(request)
    if not state.settings.get("auth_enabled", True):
        return {"ticket": "", "expires_in": 0, "auth_enabled": False}
    ticket, ttl = state.tickets.issue(principal.key)
    return {"ticket": ticket, "expires_in": ttl, "auth_enabled": True}


@router.get("/system", summary="Сведения о сервере и оборудовании")
def system(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    hardware = detect(str(state.settings.paths.data))
    data: dict[str, Any] = {
        "version": state.version,
        "uptime_s": round(time.time() - state.started_at, 1),
        "hardware": hardware.to_dict(),
        "recommended": recommended_settings(hardware),
        "log_counts": log_counts(),
        "catalog": catalog.catalog_summary(),
        "params_stats": catalog.params_stats(),
    }
    # Раскладка файловой системы и путь к базе — это разведка перед атакой,
    # а не сведения, нужные обычному пользователю для работы. Интерфейс
    # показывает их в разделе «Сервер», который и так открыт только админу.
    if principal.is_admin:
        data.update({
            "database": state.db.stats(),
            "paths": {k: str(v) for k, v in vars(state.settings.paths).items()},
            "config_file": (str(state.settings.config_file)
                            if state.settings.config_file else None),
        })
    return data


@router.get("/queue", summary="Состояние очереди")
def queue_status(request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    status = state.queue.status()
    # Тот же принцип, что и в списке заданий: всё, кроме администратора,
    # видит только своё. Прошлый заход закрыл GET /api/jobs, а этот маршрут
    # остался открытым — и отдавал имена чужих файлов, пути на диске, готовые
    # расшифровки и адреса уведомлений вместе с токенами в них.
    # light=True здесь ещё и по делу: интерфейс рисует таблицу, текст ему
    # не нужен, а на сотне часовых записей это мегабайты на каждый опрос.
    status["items"] = state.db.list_jobs(
        status=["queued", "running", "retry", "paused"], limit=200,
        order="priority DESC", owner=scope_owner(principal), light=True)
    return status


@router.post("/queue/pause", summary="Приостановить очередь")
def queue_pause(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    state.queue.pause()
    return state.queue.status()


@router.post("/queue/resume", summary="Возобновить очередь")
def queue_resume(request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    state.queue.resume()
    return state.queue.status()


@router.post("/queue/clear", summary="Отменить все ожидающие задания")
def queue_clear(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    return {"cancelled": state.queue.cancel_all()}


@router.post("/queue/retry-failed", summary="Повторить все неудавшиеся задания")
def queue_retry_failed(request: Request,
                       limit: int = Query(default=100, ge=1, le=10000),
                       principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    return {"requeued": state.queue.retry_failed(limit)}


@router.post("/queue/concurrency", summary="Изменить число одновременных заданий")
def queue_concurrency(request: Request, workers: int = Body(embed=True, ge=1, le=64),
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    state.queue.set_concurrency(workers)
    return state.queue.status()


@router.get("/settings", summary="Текущие настройки сервера")
def get_settings(request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Значения параметров и откуда каждое взялось.

    Администратору отдаётся всё, включая раскладку каталогов и адреса
    обратных вызовов; остальным — только значения, и секреты в них
    замаскированы. Ключ «только чтение» получал полный ответ, а в нём
    входящий адрес чата с токеном внутри и путь к базе.
    """
    state = get_state(request)
    return state.settings.to_dict(for_admin=principal.is_admin)


@router.put("/settings", summary="Изменить настройки сервера")
def update_settings(request: Request, values: dict[str, Any] = Body(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    # Приведение до проверки: интерфейс и внешние клиенты присылают то, что
    # ввёл человек, — «5» там, где каталог ждёт число, и 5 там, где строку.
    # Заглушки «***» вместо секретов означают «не менял» и значением не
    # становятся — см. Settings.без_заглушек.
    свои = catalog.coerce_all(state.settings.без_заглушек(
        {k: v for k, v in values.items() if k in catalog.PARAMS_BY_KEY}))
    errors = catalog.validate_all(свои)
    if errors:
        raise error_response(ConfigError("; ".join(errors)))
    applied = {}
    for key, value in свои.items():
        state.settings.set(key, value, source="api")
        applied[key] = value
    if "max_concurrent_jobs" in applied:
        state.queue.set_concurrency(int(applied["max_concurrent_jobs"]))
    if "model_cache_size" in applied or "model_idle_unload_s" in applied:
        # Ноль у `model_idle_unload_s` — «не выгружать никогда», и `or 900`
        # его терял: сохранение настроек включало автовыгрузку обратно.
        state.registry.configure(
            max(1, S.integer(state.settings, "model_cache_size", 2)),
            S.integer(state.settings, "model_idle_unload_s", 900))
    _применить_мониторинг(state, applied)
    state.db.add_event(None, "settings_changed", f"Изменено параметров: {len(applied)}")
    RUNTIME.inc("asrhub_config_reloads_total")
    return {"applied": applied}


def _применить_мониторинг(state: Any, изменено: dict[str, Any] | None) -> None:
    """Приёмники, пороги и кеш мониторинга — сразу, а не с перезапуска.

    Мониторинг читал свои настройки один раз, при запуске: приёмник,
    добавленный в «Настройках», не начинал работать, выключенная отправка
    продолжала слать, а свой порог тревоги не действовал — при том что
    страница отвечала «Применено».
    """
    from ..monitoring.service import КЛЮЧИ_НАСТРОЕК  # noqa: PLC0415

    служба = getattr(state, "monitoring", None)
    if служба is None or (изменено is not None and not КЛЮЧИ_НАСТРОЕК & set(изменено)):
        return
    try:
        служба.apply_settings(state.settings)
    except Exception as exc:                                  # noqa: BLE001
        log.warning("Настройки мониторинга не применились на ходу: %s", exc)


@router.get("/settings/hf-token", summary="Задан ли токен Hugging Face")
def hf_token_state(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Отдаёт признак и начало токена, но никогда его целиком.

    Показать токен в интерфейсе — значит отдать его каждому, кто заглянет
    через плечо, и положить в кеш браузера. Для «задан ли и тот ли» хватает
    шести знаков и длины.
    """
    state = get_state(request)
    require_admin(principal)
    token = str(state.settings.hf_token or "")
    return {
        "configured": bool(token),
        "preview": (token[:6] + "…") if token else "",
        "length": len(token),
        # Куда ляжет изменение — путь честнее слова «конфигурация»: файла
        # может не быть вовсе, и тогда он создастся при первой записи.
        "config_file": str(state.settings.config_file
                           or (state.settings.paths.data / "config.yaml")),
    }


@router.put("/settings/hf-token", summary="Задать токен Hugging Face")
def set_hf_token(request: Request, token: str = Body(default="", embed=True),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записывает токен и сохраняет его в файл конфигурации.

    Токен — не параметр каталога: у него нет ни диапазона, ни значения по
    умолчанию, и через PUT /api/settings он не проходит. Поэтому отдельный
    маршрут, а заодно и отдельное право: раздавать доступ к чужим весам
    может только администратор.

    Пустая строка — это «убрать»: иногда токен надо именно снять, например
    при передаче сервера другому владельцу.
    """
    state = get_state(request)
    require_admin(principal)
    token = (token or "").strip()
    if token and not re.match(r"^hf_[A-Za-z0-9_-]{16,}$", token):
        raise error_response(ConfigError(
            "Токен Hugging Face выглядит так: hf_ и ещё не меньше шестнадцати знаков.",
            hint="Взять его: https://huggingface.co/settings/tokens — прав «read» достаточно."))
    state.settings.hf_token = token
    target = state.settings.config_file or (state.settings.paths.data / "config.yaml")
    try:
        state.settings.save(target)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    state.db.add_event(None, "settings_changed",
                       "Токен Hugging Face " + ("задан" if token else "убран"))
    log.info("Токен Hugging Face %s", "задан" if token else "убран")
    return {"configured": bool(token),
            "preview": (token[:6] + "…") if token else "",
            "length": len(token)}


@router.post("/settings/save", summary="Сохранить настройки в файл конфигурации")
def save_settings(request: Request,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    target = state.settings.config_file or (state.settings.paths.data / "config.yaml")
    path = state.settings.save(target)
    return {"saved": str(path)}


@router.post("/settings/reset", summary="Сбросить настройки к значениям по умолчанию")
def reset_settings(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    for key, value in catalog.defaults().items():
        state.settings.set(key, value, source="default")
    _применить_мониторинг(state, None)
    return {"reset": True}


@router.get("/analytics", summary="Сводная аналитика")
def analytics(request: Request, period: str = Query(default="week"),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    # Ключу без прав администратора аналитика считается только по его
    # заданиям: иначе разделы «ошибки» и «самые медленные» показывали чужие
    # имена файлов, а «по владельцам» — весь список тех, кто пользуется
    # сервером.
    return state.analytics.full_report(period, owner=scope_owner(principal))


@router.get("/analytics/export", summary="Выгрузка аналитики в таблицу")
def analytics_export(request: Request, period: str = Query(default="month"),
                     fmt: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
                     principal: Principal = Depends(authenticate)) -> Any:
    """Тот же отчёт, что на экране, — книгой Excel или архивом CSV.

    Отчёт можно было только смотреть: чтобы отдать месячные числа
    руководителю, их переписывали руками — и переписывали с округлённых
    значений на экране, а не с тех, что посчитал сервер.

    Разрез по владельцу здесь тот же, что и у самого отчёта: обычный ключ
    выгружает только свои задания.
    """
    from ..analytics_export import to_csv_zip, to_xlsx
    from .routes_jobs import content_disposition

    state = get_state(request)
    отчёт = state.analytics.full_report(period, owner=scope_owner(principal))
    # Выгрузка идёт мимо прослойки маскирования: та смотрит только на
    # ответы JSON, а здесь книга Excel. Значит, маскируем сами — иначе
    # ключ, заведённый «без персональных данных», получал их именем файла
    # записи, а в колл-центре имя файла — это номер клиента.
    if bool(state.settings.get("export_mask_pii")) or principal.mask_pii:
        from ..content import masking  # noqa: PLC0415

        отчёт = masking.mask_payload(отчёт)
    метка = time.strftime("%Y-%m-%d")
    if fmt == "csv":
        тело = to_csv_zip(отчёт, period)
        имя, тип = f"asrhub-аналитика-{period}-{метка}.zip", "application/zip"
    else:
        try:
            тело = to_xlsx(отчёт, period)
        except ASRHubError as exc:
            raise error_response(exc) from exc
        имя = f"asrhub-аналитика-{period}-{метка}.xlsx"
        тип = ("application/vnd.openxmlformats-officedocument."
               "spreadsheetml.sheet")
    return Response(content=тело, media_type=тип, headers={
        "Content-Disposition": content_disposition(имя),
        # Отчёт считается на момент запроса: закешированная выгрузка —
        # это вчерашние числа под сегодняшним именем.
        "Cache-Control": "no-store",
    })


@router.get("/analytics/{section}", summary="Отдельный раздел аналитики")
def analytics_section(request: Request, section: str, period: str = "week",
                      principal: Principal = Depends(authenticate)) -> Any:
    """Один разрез сводного отчёта — когда весь отчёт не нужен.

    Разделы: overview, timeseries, models, languages, owners, engines,
    sources, errors, durations, slowest, profile, efficiency, weekly, cache,
    reliability, audio, resources, quality, suspicious, drift, control,
    accuracy, calibration, latency, agreement, tags, queue. Неизвестный раздел
    отвечает 400 с перечнем доступных.
    """
    state = get_state(request)
    handlers = {
        "overview": state.analytics.overview,
        "timeseries": state.analytics.timeseries,
        "models": state.analytics.by_model,
        "languages": state.analytics.by_language,
        "owners": state.analytics.by_owner,
        "engines": state.analytics.by_engine,
        "sources": state.analytics.by_source,
        "errors": state.analytics.errors,
        "durations": state.analytics.duration_histogram,
        "slowest": state.analytics.slowest,
        "profile": state.analytics.hourly_profile,
        "efficiency": state.analytics.efficiency,
        "weekly": state.analytics.weekly_heatmap,
        "cache": state.analytics.cache_savings,
        "reliability": state.analytics.reliability,
        "audio": state.analytics.audio_profile,
        "resources": state.analytics.resources,
        "quality": state.analytics.quality_trend,
        "suspicious": state.analytics.suspicious,
        "drift": state.analytics.drift,
        "control": state.analytics.control,
        "accuracy": state.analytics.accuracy,
        "calibration": state.analytics.calibration,
        "latency": state.analytics.latency,
        "agreement": state.analytics.agreement,
        "tags": state.analytics.by_tag,
        "queue": state.analytics.queue_latency,
    }
    handler = handlers.get(section)
    if handler is None:
        raise error_response(ConfigError(
            f"Неизвестный раздел аналитики «{section}».",
            hint="Доступные разделы: " + ", ".join(sorted(handlers))))
    return handler(period, owner=scope_owner(principal))


@router.get("/logs", summary="Журнал сервера")
def logs(request: Request, limit: int = Query(default=200, ge=1, le=5000),
         level: str = "", search: str = "",
         job_id: str = "", principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Журнал сервера целиком.

    Требует ключа администратора: записи несут имена чужих файлов, тексты
    ошибок и трассировки, а разделить журнал по владельцам нечем — строка
    пишется до того, как становится известен ключ.
    """
    require_admin(principal)
    return {"items": log_recent(limit=limit, level=level, search=search, job_id=job_id),
            "counts": log_counts()}


@router.get("/events", summary="Лента событий")
def events(request: Request,
           # Предел обязателен и снизу, и сверху: голым `int` сюда проходил
           # ноль и отрицательное, а «LIMIT -1» в SQLite снимает предел
           # вовсе — любой ключ поднимал в память всю таблицу событий за
           # девяносто дней, да ещё с разбором JSON в каждой строке.
           limit: int = Query(default=100, ge=1, le=1000),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Лента событий сервера.

    Событие ссылается на задание и несёт имя файла, поэтому лента целиком
    доступна только администратору. Ключ пользователя видит события своих
    заданий — по ним же строится живое обновление интерфейса.
    """
    state = get_state(request)
    items = state.db.get_events(limit=limit if principal.is_admin else limit * 4)
    if principal.is_admin:
        return {"items": items}

    own = {j["id"] for j in state.db.list_jobs(owner=principal.name, limit=2000, light=True)}
    # События без job_id — это ровно административные: создание и отзыв
    # ключей, изменение настроек, загрузка моделей. Условие «нет job_id —
    # значит можно» пропускало их всем, включая readonly, вместе с именами
    # ключей и их ролями. Из общесистемных оставляем то, что и так видно
    # по состоянию очереди.
    public_kinds = {"queue_paused", "queue_resumed", "queue_cleared"}
    mine = [e for e in items
            if e.get("job_id") in own
            or (not e.get("job_id") and e.get("kind") in public_kinds)]
    return {"items": mine[:limit]}


@router.get("/usage", summary="Расход и квоты ключа")
def usage(request: Request,
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сколько израсходовано за сутки и сколько всего можно.

    Без этого маршрута о квоте узнавали только в момент отказа — уже после
    того, как файл загружен.
    """
    state = get_state(request)
    scope = scope_owner(principal) or principal.name
    used = state.db.owner_usage(scope, time.time() - 86400)
    limits = {
        "jobs": principal.quota_jobs_per_day,
        "audio_hours": principal.quota_audio_hours_per_day,
        "storage_gb": principal.quota_storage_gb,
    }
    remaining = {k: (None if not limits[k] else round(max(0.0, limits[k] - used[k]), 4))
                 for k in limits}
    return {
        "owner": principal.name,
        "group": principal.group,
        "scope": [scope] if isinstance(scope, str) else scope,
        "window": "последние сутки",
        "used": used,
        "limits": {k: (v or None) for k, v in limits.items()},
        "remaining": remaining,
    }


@router.get("/keys", summary="Ключи доступа")
def list_keys(request: Request,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    items = []
    for key, info in state.settings.api_keys.items():
        items.append({
            # Отдельно от превью: отзыв требует не меньше двенадцати символов,
            # а из «ah_8AD…anCu» столько не выкроить. Интерфейс раньше слал
            # первые шесть символов превью и получал 400 на любом ключе —
            # отозвать ключ через интерфейс было невозможно в принципе.
            "key_id": key[:16] if len(key) > 16 else key,
            "key_preview": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***",
            "name": info.get("name"),
            "role": info.get("role", "user"),
            "enabled": info.get("enabled", True),
            "rate_limit": info.get("rate_limit", 0),
            "group": info.get("group", ""),
            "quota_jobs_per_day": info.get("quota_jobs_per_day", 0),
            "quota_audio_hours_per_day": info.get("quota_audio_hours_per_day", 0),
            "quota_storage_gb": info.get("quota_storage_gb", 0),
            "mask_pii": bool(info.get("mask_pii")),
        })
    return {"items": items}


@router.post("/keys", summary="Создать ключ доступа")
def create_key(request: Request, name: str = Body(embed=True),
               role: str = Body(default="user", embed=True),
               rate_limit: int = Body(default=0, embed=True),
               group: str = Body(default="", embed=True),
               quota_jobs_per_day: int = Body(default=0, embed=True),
               quota_audio_hours_per_day: float = Body(default=0, embed=True),
               quota_storage_gb: float = Body(default=0, embed=True),
               mask_pii: bool = Body(default=False, embed=True),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    import secrets

    state = get_state(request)
    require_admin(principal)
    if role not in ("admin", "user", "readonly"):
        raise error_response(ConfigError(
            f"Недопустимая роль «{role}».",
            hint="Допустимые роли: admin — полный доступ, user — отправка заданий, "
                 "readonly — только чтение."))
    # Имя ключа — это и есть его область: по нему ключ видит свои задания.
    # Два ключа без имени получали одно имя «ключ» и видели задания друг
    # друга — ровно то, от чего область и заводилась.
    name = str(name or "").strip()
    if not name:
        raise error_response(ConfigError(
            "Укажите имя ключа.",
            hint="По имени ключ видит свои задания: два ключа с одним именем "
                 "делят одну область. Назовите ключ по системе или человеку, "
                 "которому он выдаётся."))
    if len(name) > 100:
        raise error_response(ConfigError("Имя ключа длиннее ста знаков."))
    тёзки = [info for info in state.settings.api_keys.values()
             if str(info.get("name") or "ключ") == name]
    учётка = None
    try:
        учётка = state.accounts.by_username(name) if getattr(state, "accounts", None) else None
    except Exception:                                    # noqa: BLE001
        учётка = None
    key = "ah_" + secrets.token_urlsafe(24)
    # group объединяет ключи в подразделение: они видят задания друг друга.
    # Квоты нулевые означают «без ограничения» и считаются за скользящие сутки.
    state.settings.api_keys[key] = {
        "name": name, "role": role, "rate_limit": rate_limit, "enabled": True,
        "group": group,
        "quota_jobs_per_day": max(0, quota_jobs_per_day),
        "quota_audio_hours_per_day": max(0.0, quota_audio_hours_per_day),
        "quota_storage_gb": max(0.0, quota_storage_gb),
        # Ключ интеграции или аналитика, которому нужен текст, но не
        # персональные данные: все его ответы и выгрузки обезличиваются.
        "mask_pii": bool(mask_pii),
    }
    # Без записи на диск ключ жил бы только до перезапуска, тогда как
    # интерфейс обещает пользователю обратное.
    saved = state.settings.persist_api_keys()
    state.db.add_event(None, "key_created", f"Создан ключ «{name}» с ролью {role}")
    ответ: dict[str, Any] = {
        "key": key, "name": name, "role": role, "group": group,
        "mask_pii": bool(mask_pii),
        "persisted": saved,
        "warning": "Ключ показывается один раз — сохраните его."
                   if saved else
                   "Ключ показывается один раз. Внимание: файл конфигурации "
                   "недоступен, поэтому ключ будет действовать только до перезапуска."}
    # Одно имя — одна область. Для смены ключа так и задумано (новый ключ
    # видит задания прежнего), но по ошибке это открывает чужие задания,
    # поэтому говорим об этом прямо.
    if тёзки or учётка is not None:
        ответ["note"] = (
            f"Имя «{name}» уже носит "
            + ("другой ключ" if тёзки else "учётная запись")
            + ": они видят задания друг друга. Если это смена ключа — так и "
              "задумано, прежний можно отозвать; если нет — отзовите этот и "
              "создайте с другим именем.")
    return ответ


@router.delete("/keys/{preview}", summary="Отозвать ключ доступа")
def revoke_key(request: Request, preview: str,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    if len(preview) < 12:
        raise error_response(ConfigError(
            "Слишком короткий идентификатор ключа.",
            hint="Передайте не менее двенадцати первых символов ключа — "
                 "иначе под совпадение попадёт чужой ключ."))

    matches = [key for key in state.settings.api_keys if key.startswith(preview)]
    if not matches:
        raise error_response(KeyNotFound(preview))
    if len(matches) > 1:
        raise error_response(ConfigError(
            f"Под «{preview}» подходит несколько ключей ({len(matches)}).",
            hint="Передайте больше символов, чтобы совпадение было однозначным."))

    name = str(state.settings.api_keys.get(matches[0], {}).get("name") or "")
    state.settings.api_keys.pop(matches[0], None)
    state.settings.persist_api_keys()
    state.db.add_event(None, "key_revoked", f"Отозван ключ доступа «{name}»")
    return {"revoked": True, "name": name}


def _guard_metrics(request: Request, state: Any) -> None:
    """Допуск к метрикам: свободно при monitoring_public, иначе по ключу."""
    if not state.settings.get("auth_enabled", True):
        return
    if state.settings.get("monitoring_public", True):
        return
    token = token_of(request)
    info = state.settings.api_keys.get(token)
    if not info:
        raise error_response(AuthError("Ключ доступа отсутствует или недействителен."))
    if info.get("enabled") is False:
        raise error_response(ForbiddenError("Ключ доступа отключён."))


@router.get("/metrics", summary="Метрики Prometheus", response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    """Полный снимок метрик в формате Prometheus.

    Тот же вывод, что и у /api/monitoring/metrics. Раньше здесь работал
    отдельный, написанный вручную экспорт на полтора десятка метрик со
    старыми именами (asrhub_ram_used_mb, asrhub_disk_free_gb): два адреса
    отдавали разные имена для одних и тех же величин, и правила тревог,
    собранные по каталогу, на этом адресе не срабатывали ни разу.

    Старые имена никуда не делись — подсистема мониторинга отдаёт их рядом
    с новыми как устаревшие псевдонимы, — поэтому уже настроенный сбор
    продолжает работать.
    """
    state = get_state(request)
    if not state.settings.get("metrics_enabled", True):
        return PlainTextResponse("# экспорт метрик отключён\n", status_code=404)
    # Тот же порядок допуска, что и у /api/monitoring/metrics. Раньше этот
    # адрес не проверял ничего: администратор закрывал метрики настройкой
    # monitoring_public: false, /api/monitoring/metrics честно отвечал 401,
    # а здесь тот же снимок — глубина очереди, счётчики ошибок, свободное
    # место, версии — отдавался кому угодно.
    _guard_metrics(request, state)
    service = getattr(state, "monitoring", None)
    if service is None:
        # Подсистема мониторинга не поднялась — отдаём хотя бы прежний срез,
        # чтобы сбор метрик не остался совсем без данных.
        # Свод по содержанию передаём и сюда: этот путь работает, когда
        # подсистема мониторинга не поднялась, и оставлять его без метрик
        # разговоров значило бы, что при её сбое пропадает ровно та часть,
        # ради которой на метрики и смотрят.
        свод = None
        if getattr(state, "content", None) is not None:
            from ..insights import Insights  # noqa: PLC0415

            свод = Insights(state.db, state.content)
        return PlainTextResponse(
            state.analytics.prometheus(свод),
            media_type="text/plain; version=0.0.4; charset=utf-8")
    body, content_type = service.render("prometheus")
    return PlainTextResponse(body, media_type=content_type)


@router.get("/reference", summary="Автономный справочник API (без интернета)",
            response_class=HTMLResponse)
def api_reference(request: Request) -> HTMLResponse:
    """Справочник, собранный из схемы OpenAPI прямо на сервере.

    Штатные страницы /api/docs и /api/redoc подгружают скрипты из интернета
    и не работают в закрытом контуре. Эта страница полностью автономна.
    """
    schema = request.app.openapi()
    rows: list[str] = []
    for path, methods in sorted(schema.get("paths", {}).items()):
        for method, spec in methods.items():
            if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
                continue
            params = spec.get("parameters", []) or []
            body = spec.get("requestBody", {})
            param_html = "".join(
                f"<li><code>{p.get('name')}</code> "
                f"<span class=q>{p.get('in')}</span>"
                + (" <b>обязательный</b>" if p.get("required") else "")
                + (f" — {p.get('description')}" if p.get("description") else "")
                + "</li>"
                for p in params)
            rows.append(
                f"<tr><td><span class=m data-m='{method.upper()}'>{method.upper()}</span></td>"
                f"<td><code>{path}</code></td>"
                f"<td>{spec.get('summary', '')}"
                + (f"<ul>{param_html}</ul>" if param_html else "")
                + ("<div class=q>принимает тело запроса</div>" if body else "")
                + "</td></tr>")
    html = f"""<!DOCTYPE html><html lang=ru><head><meta charset=utf-8>
<title>ASR Hub — справочник API</title>
<style>
body{{font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
 background:#0e1116;color:#e6edf3;margin:0;padding:28px 32px}}
h1{{font-size:20px;margin:0 0 4px}} .sub{{color:#9aa7b6;margin-bottom:20px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{text-align:left;color:#6b7889;font-size:11px;text-transform:uppercase;
 padding:8px;border-bottom:1px solid #262e3a}}
td{{padding:9px 8px;border-bottom:1px solid #1e2530;vertical-align:top}}
code{{font-family:ui-monospace,Menlo,monospace;font-size:12.5px;color:#4c8dff}}
.m{{font-family:ui-monospace,monospace;font-size:11px;padding:2px 7px;border-radius:4px;
 background:#1b212a}}
.m[data-m=GET]{{color:#3fb950}} .m[data-m=POST]{{color:#4c8dff}}
.m[data-m=PUT]{{color:#d29922}} .m[data-m=DELETE]{{color:#f85149}}
ul{{margin:6px 0 0;padding-left:18px;color:#9aa7b6;font-size:12px}}
.q{{color:#6b7889;font-size:11.5px}}
a{{color:#4c8dff}}
</style></head><body>
<h1>ASR Hub — справочник программного интерфейса</h1>
<div class=sub>Версия {schema.get('info', {}).get('version', '')} ·
 {len(rows)} операций · страница собрана на сервере и не требует интернета ·
 <a href="/api/openapi.json">схема OpenAPI</a> ·
 <a href="/api/docs">интерактивный Swagger (нужен интернет)</a></div>
<table><thead><tr><th style="width:70px">Метод</th><th style="width:280px">Путь</th>
<th>Описание и параметры</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>"""
    return HTMLResponse(html)


@router.post("/maintenance/cleanup", summary="Очистка старых данных")
def cleanup(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    removed = state.db.cleanup(
        results_days=retention_days(state.settings),
        audit_days=S.integer(state.settings, "audit_days", 365))
    # Очистка по кнопке — вся: и строки без задания со следами удалённых
    # разговоров, которые служебный цикл убирает раз в сутки.
    уборка = state.db.sweep_orphans()
    removed["orphans"] = уборка.get("rows", 0)
    сжатие = state.db.vacuum()
    return {"removed": removed, "sweep": уборка, "vacuum": сжатие}


#: Самый короткий запрос на удаление по требованию. Три буквы нашли бы
#: половину архива, а удаление необратимо.
ERASE_MIN = 4


@router.post("/maintenance/erase", summary="Удалить записи по требованию")
def erase(request: Request,
          query: str = Body(embed=True),
          dry_run: bool = Body(default=True, embed=True),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Находит и удаляет всё, где встречается запрос: по номеру телефона,
    имени файла, фамилии — тем же поиском, что в «Результатах», и по
    журналу звонков: номер в любом поле звонка (сравниваются цифры, от
    семи) и имя звонящего.

    Нужно для 152-ФЗ: при отзыве согласия записи уничтожаются в срок до
    тридцати дней, и искать их по одной в архиве на сто тысяч записей —
    занятие на день. По умолчанию — пробный запуск: показывает, что нашлось,
    и ничего не удаляет; удаление — только с `dry_run: false` и только
    администратору. Сам запрос в журнал попадает усечённым: номер телефона
    в журнале событий — это ещё одно место, откуда его придётся удалять.
    """
    from .routes_jobs import BULK_LIMIT, _delete_one  # noqa: PLC0415

    state = get_state(request)
    require_admin(principal)
    запрос = (query or "").strip()
    if len(запрос) < ERASE_MIN:
        raise error_response(ConfigError(
            f"Запрос короче {ERASE_MIN} символов найдёт слишком многое.",
            hint="Укажите номер телефона, имя файла или фамилию целиком."))
    найдено = state.db.list_jobs(search=запрос, limit=BULK_LIMIT)
    номера = [str(j["id"]) for j in найдено]
    # И звонки по номеру и имени звонящего: запись, названная
    # `${UNIQUEID}.wav`, по номеру клиента не находилась ничем, если номер
    # не произнесли вслух, — субъекту отвечали «удалено», а разговор
    # оставался.
    for номер in state.db.erase_call_job_ids(запрос, limit=BULK_LIMIT):
        if номер not in номера and len(номера) < BULK_LIMIT:
            задание = state.db.get_job(номер)
            if задание is not None:
                найдено.append(задание)
                номера.append(номер)
    звонков = state.db.erase_calls_count(запрос)
    if dry_run:
        return {"dry_run": True, "matched": len(номера), "ids": номера,
                "calls": звонков, "limit": BULK_LIMIT}
    for job in найдено:
        _delete_one(state, job, principal)
    # Звонки без задания (пропущенные — короткий, без записи, не отвечен)
    # хранят тот же номер: обезличиваются и они, ключ остаётся. Звонки
    # удалённых записей обезличило уже само удаление — в ответе все, что
    # нашлись по запросу.
    state.db.erase_calls(запрос)
    обезличено = звонков
    # Ответы модели, положенные в кеш до того, как он узнал свои записи.
    из_кеша = state.db.llm_cache_forget_matching(запрос)
    # Следы в указателе поиска и словаре форм — сразу, а не суточной
    # уборкой: требование субъекта исполняется в срок, а не «к утру».
    уборка = state.db.sweep_orphans()
    усечённый = запрос[:3] + "…" if len(запрос) > 3 else "…"
    state.db.add_event(None, "erase",
                       f"Удалено по требованию: {len(номера)} записей, звонков "
                       f"обезличено {обезличено} по запросу «{усечённый}» "
                       f"({principal.name})",
                       {"count": len(номера), "calls": обезличено, "by": principal.name})
    return {"dry_run": False, "deleted": len(номера), "ids": номера,
            "calls": обезличено, "llm_cache": из_кеша,
            "index_purged": bool(уборка.get("fts")), "limit": BULK_LIMIT}


@router.get("/maintenance/consent", summary="Записи без отметки о согласии")
def consent(request: Request,
            days: int | None = Query(default=None, ge=0, le=3650),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Завершённые записи старше N дней, у которых нет метки согласия.

    Метка задаётся настройкой `consent_tag`; пустая — проверка выключена.
    Это не юридическая гарантия, а список того, на что стоит посмотреть:
    сервер не знает, есть ли согласие, он знает лишь, поставили ли метку.
    """
    state = get_state(request)
    require_admin(principal)
    метка = str(state.settings.get("consent_tag") or "").strip()
    значение = state.settings.get("consent_days")
    срок = days if days is not None else int(
        значение if значение not in (None, "") else 30)
    if not метка:
        return {"enabled": False, "tag": "", "days": срок, "count": 0, "ids": []}
    номера = state.db.jobs_without_tag(метка, older_than=time.time() - срок * 86400)
    return {"enabled": True, "tag": метка, "days": срок,
            "count": len(номера), "ids": номера[:200]}


@router.post("/maintenance/unload-models", summary="Выгрузить модели из памяти")
def unload_models(request: Request,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    state.registry.unload_all()
    return {"unloaded": True}


# ---------------------------------------------------------------------------
# Автодиагностика
# ---------------------------------------------------------------------------

@router.get("/system/selfcheck", summary="Автодиагностика всей системы")
def system_selfcheck(request: Request,
                     deep: bool = Query(default=False),
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Состояние всех подсистем сразу: что работает, что сломано, что делать.

    `deep=1` добавляет дорогие проверки: целостность базы, разбор станций
    АТС, пробу сервера языковой модели, обход каталогов и попытку
    достучаться до внешних адресов. Это секунды против миллисекунд,
    поэтому панель опрашивает обычный вариант, а «проверить всё» —
    глубокий.

    Ключу без прав администратора ответ выдаётся без раскладки каталогов и
    без адресов: соседние GET /api/system и GET /api/settings прячут
    `paths` ровно по этой причине, и диагностика, отдающая путь к базе и
    адрес приёмника метрик любому ключу, сводила бы ту защиту на нет.
    Сами неисправности при этом видны всем — скрывать от оператора, что
    сервер нездоров, незачем.
    """
    state = get_state(request)
    свод = selfcheck.состояние(state, глубоко=bool(deep))
    # Запись в журнал — только по изменениям и только от администратора.
    # Иначе опрос панели обычным ключом наполнял бы ленту событий теми же
    # строками, а мы бы ещё и писали в базу на каждый GET.
    if principal.is_admin:
        try:
            свод["journal"] = selfcheck.записать_проблемы(state.db, свод)
        except Exception as exc:                               # noqa: BLE001
            # Диагностика не обязана падать из-за того, что не смогла
            # записать о себе в журнал: свод уже собран, и он нужен.
            log.warning("Проблемы не записаны в журнал: %s", exc)
    else:
        свод = selfcheck.спрятать_пути(свод, state.settings)
    return свод


#: Виды событий, которые считаются неисправностями, и их уровень. Уровня у
#: события в базе нет — есть вид; перечень явный, потому что «всё, кроме
#: хорошего» затащило бы в список проблем создание заданий и смену
#: приоритета, а в них ничего неисправного нет.
PROBLEM_KINDS = {
    "failed": "error",
    "warning": "warning",
    "retry_scheduled": "warning",
    "alert_firing": "error",
    "employees_sync_failed": "warning",
    "selfcheck": "warning",
    "agent_removed": "warning",
}


def _problem_row(at: Any, level: str, source: str, what: str,
                 hint: str = "", **extra: Any) -> dict[str, Any]:
    """Одна строка журнала проблем — одинаковая для всех источников."""
    return {"at": at, "level": level, "source": source,
            "what": str(what or "")[:1000], "hint": str(hint or "")[:1000], **extra}


@router.get("/system/problems", summary="Журнал проблем и неисправностей")
def system_problems(request: Request,
                    since: float = Query(default=0.0, ge=0),
                    limit: int = Query(default=200, ge=1, le=2000),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Всё плохое в одном списке: и то, что происходит сейчас, и то, что было.

    До этого маршрута ответ на вопрос «что у нас ломалось на прошлой
    неделе» собирался из четырёх мест: неудавшиеся задания — в разделе
    заданий, тревоги — в мониторинге, сбои станций — в «АТС», ошибки
    разбора — в смысловом слое. Пока их четыре, никто не смотрит ни в
    одно.

    `since` — момент времени (секунды эпохи); по умолчанию сутки назад.
    Ключ без прав администратора видит только свои задания и события своих
    заданий — ровно как в GET /api/jobs и GET /api/events.
    """
    state = get_state(request)
    начало = float(since) if since else time.time() - 86400
    записи: list[dict[str, Any]] = []

    # 1. То, что не так прямо сейчас. Без глубоких проверок: этот маршрут
    #    вызывают из ленты, а не по кнопке «проверить всё».
    свод: dict[str, Any] = {}
    try:
        свод = selfcheck.состояние(state, глубоко=False)
        for проблема in свод.get("problems", []):
            записи.append(_problem_row(
                проблема["at"],
                "error" if проблема["state"] == "fail" else "warning",
                f"диагностика · {проблема.get('component_title')}",
                f"{проблема['title']}: {проблема.get('value', '')}",
                проблема.get("hint", ""),
                component=проблема.get("component"), id=проблема.get("id"),
                current=True))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Свод диагностики для журнала проблем не собран: %s", exc)
        записи.append(_problem_row(
            time.time(), "error", "диагностика",
            f"Автодиагностика не отработала: {exc}",
            "Смотрите журнал сервера: не работает сама проверка."))

    # 2. Неудавшиеся задания за период. Разрез по владельцу тот же, что у
    #    списка заданий: иначе в «журнале проблем» показывались бы чужие
    #    имена файлов — а в колл-центре имя файла это номер клиента.
    try:
        for job in state.db.list_jobs(status="failed", since=начало,
                                      owner=scope_owner(principal),
                                      limit=min(limit, 500), light=True):
            записи.append(_problem_row(
                job.get("finished_at") or job.get("created_at"), "error",
                "задание",
                f"{job.get('filename') or job.get('id')}: "
                f"{job.get('error_message') or 'ошибка без описания'}",
                job.get("error_hint") or "",
                job_id=job.get("id"), code=job.get("error_code")))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Неудавшиеся задания не прочитаны: %s", exc)

    # 3. События сервера. Своими руками их не отфильтровать по виду в SQL —
    #    берём с запасом и отбираем здесь; лента событий и так подрезана по
    #    сроку хранения.
    try:
        свои: set[str] = set()
        if not principal.is_admin:
            свои = {j["id"] for j in state.db.list_jobs(
                owner=scope_owner(principal), limit=2000, light=True)}
        for событие in state.db.get_events(limit=min(limit * 5, 5000)):
            уровень = PROBLEM_KINDS.get(str(событие.get("kind") or ""))
            if уровень is None or float(событие.get("ts") or 0) < начало:
                continue
            # События без задания — общесистемные: тревоги, диагностика,
            # сбои синхронизации. Их видит только администратор: в них
            # имена ключей, адреса приёмников и пути.
            job_id = событие.get("job_id")
            if not principal.is_admin and (not job_id or job_id not in свои):
                continue
            данные = событие.get("data") if isinstance(событие.get("data"), dict) else {}
            записи.append(_problem_row(
                событие.get("ts"), уровень,
                f"событие · {событие.get('kind')}",
                событие.get("message") or str(событие.get("kind")),
                str(данные.get("hint") or ""),
                job_id=job_id))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Лента событий для журнала проблем не прочитана: %s", exc)

    # 4. Телефония: у каждой станции своя последняя ошибка, и в общую
    #    ленту она не попадает — поток забора пишет её только себе.
    try:
        телефония = getattr(state, "telephony", None)
        if телефония is not None:
            состояние_атс = телефония.status(for_admin=principal.is_admin)
            for станция in состояние_атс.get("stations", []):
                if станция.get("last_error"):
                    записи.append(_problem_row(
                        станция.get("last_run"), "error",
                        f"АТС · {станция.get('name')}",
                        str(станция["last_error"]),
                        "Подробный разбор: GET /api/system/selfcheck?deep=1 — "
                        "он называет причину, по которой звонки не приезжают.",
                        component="telephony", id=станция.get("id")))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Состояние станций для журнала проблем не прочитано: %s", exc)

    # 5. Смысловой слой: ошибки вызовов модели и очередь разбора.
    try:
        клиент = getattr(state, "llm", None)
        if клиент is not None and клиент.enabled and клиент.last_error:
            записи.append(_problem_row(
                getattr(клиент, "last_call_at", None), "warning",
                "языковая модель", str(клиент.last_error),
                "Разбор пропускает записи, на которых модель ответила "
                "ошибкой, и возвращается к ним позже.",
                component="llm"))
        сводка = state.db.llmq_stats(начало)
        неудач = int(float(сводка.get("failed") or 0))
        if неудач:
            записи.append(_problem_row(
                time.time(), "warning", "языковая модель",
                f"Разбор не удался у {неудач} записей за период",
                "Повторить: POST /api/llm/queue/retry-failed.",
                component="llm"))
    except Exception as exc:                                   # noqa: BLE001
        log.warning("Состояние смыслового слоя для журнала проблем не прочитано: %s", exc)

    # Свежее сверху: журнал читают с начала, и первым должно стоять то,
    # что случилось только что. Записи без времени (их даёт станция, у
    # которой ещё не было ни одного захода) не должны при этом уезжать в
    # непредсказуемое место — считаем их самыми свежими.
    записи.sort(key=lambda з: float(з.get("at") or time.time()), reverse=True)
    ответ = {
        "since": начало,
        "at": time.time(),
        "total": len(записи),
        "items": записи[:limit],
        "state": свод.get("state", "unknown"),
        "summary": свод.get("summary", {}),
    }
    if not principal.is_admin:
        ответ = selfcheck.спрятать_пути(ответ, state.settings)
    return ответ


@router.post("/crm/test", summary="Проверить обратную запись в CRM")
def crm_test(request: Request, job_id: str = Query(default="", max_length=64),
             entity_id: str = Query(default="", max_length=64),
             dry_run: bool = Query(default=True),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Показывает, что именно уйдёт в CRM, и (по желанию) отправляет это.

    По умолчанию ничего не отправляется: сначала человек должен увидеть адрес
    и тело запроса. Комментарий, ушедший не в ту карточку, убирается руками, а
    в ленте Bitrix24 он остаётся навсегда — в отличие от неудачной попытки.
    """
    from .. import crm as crm_mod  # noqa: PLC0415

    state = get_state(request)
    require_admin(principal)
    настройки = crm_mod.Настройки.из_настроек(state.settings)
    беда = настройки.проблема()
    if беда and not настройки.enabled:
        беда = "Обратная запись в CRM выключена настройкой «crm_enabled»."
    if беда:
        raise error_response(ConfigError(беда, hint="Раздел «Настройки» → «CRM»."))

    задание = state.db.get_job(job_id) if job_id else None
    звонок = state.db.call_for_job(job_id) if job_id else None
    if job_id and not задание:
        raise error_response(ConfigError(f"Задание {job_id} не найдено."))
    if задание is None:
        # Без записи собираем показательный пример: человеку нужно увидеть
        # форму запроса, а не дожидаться подходящего разговора.
        задание = {"id": "пример", "text": "Здравствуйте, я по поводу счёта.",
                   "media_duration_s": 128}
        разбор: dict[str, Any] = {
            "sentiment": {"label": "нейтральная", "turn": {"shift": 0.1}},
            "compliance": {"score": 0.9}, "scorecard": {"score": 82},
            "categories": {"topics": [{"name": "оплата"}]},
            "llm": {"summary": "Клиент просит выставить счёт на оплату.",
                    "reason": "счёт", "outcome": "решено",
                    "actions": [{"text": "выставить счёт"}]}}
    else:
        разбор = dict((state.db.get_content(job_id) or {}).get("detail") or {})
        модель = state.db.llm_get(job_id)
        if модель:
            разбор["llm"] = dict(модель)

    сделка = entity_id or crm_mod.сущность_из(задание, звонок)
    данные = crm_mod.собрать(задание, разбор, звонок,
                             base_url=str(state.settings.get("public_url") or ""),
                             mask=настройки.mask_pii)
    # Без сделки запрос к amoCRM и Bitrix24 показать можно только с номером
    # для примера — «1». Но ОТПРАВЛЯТЬ с ним нельзя: раньше «Отправить» без
    # сделки писал показательное примечание в сделку №1 — карточку
    # настоящего клиента, — а интерфейс при этом показывал «Сделка: не
    # определена». У своего адреса («custom») номер сделки необязателен.
    нужна_сделка = настройки.kind != "custom"
    пример = нужна_сделка and not сделка
    if пример and not dry_run:
        raise error_response(ConfigError(
            "Не указано, в какую сделку отправлять: примечание ушло бы в "
            "сделку №1 — карточку чужого клиента.",
            hint="Впишите номер сделки в поле проверки или выберите запись, "
                 "у которой он есть (поле звонка userfield или accountcode, "
                 "параметр задания crm_entity_id)."))
    try:
        список = crm_mod.запросы(данные, настройки,
                                 entity_id=сделка or ("1" if пример else ""))
    except ASRHubError as exc:
        raise error_response(exc) from exc

    # Токен в показанном теле и заголовках не нужен: человек и так его знает,
    # а ответ API уходит в журналы и на экран.
    def безопасные(заголовки: dict[str, str]) -> dict[str, str]:
        return {к: ("…" if к.lower() == "authorization" else з)
                for к, з in заголовки.items()}

    первый = список[0]
    итог: dict[str, Any] = {
        "kind": настройки.kind, "entity_id": сделка,
        # Номер в показанном запросе — для примера, а не найденная сделка.
        "entity_example": пример,
        "url": первый.url, "headers": безопасные(первый.headers),
        "body": первый.body.decode("utf-8", "replace"),
        # Все запросы по порядку: свои поля уходят вторым — правкой сущности.
        "requests": [{"method": з.method, "url": з.url, "purpose": з.назначение,
                      "headers": безопасные(з.headers),
                      "body": з.body.decode("utf-8", "replace")} for з in список],
        "note": crm_mod.примечание(данные, transcript=настройки.send_transcript),
        "sent": False,
    }
    if not dry_run:
        try:
            ответ = crm_mod.отправить(
                данные, настройки, entity_id=сделка,
                allow_internal=bool(state.settings.get("webhook_allow_internal", False)))
        except ASRHubError as exc:
            raise error_response(exc) from exc
        итог["sent"] = True
        итог["response"] = ответ
        if job_id and задание.get("id") == job_id:
            # Отправили руками — автоматическая отправка по этой записи
            # второго примечания уже не сделает.
            state.db.crm_mark(job_id, state.db.CRM_ОТПРАВЛЕНО)
    return итог
