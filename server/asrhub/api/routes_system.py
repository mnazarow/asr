"""Маршруты сервера: состояние, настройки, очередь, аналитика, журнал, ключи."""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from .. import catalog
from ..errors import ConfigError, KeyNotFound
from ..hardware import detect, recommended_settings
from ..logging_setup import counts as log_counts
from ..logging_setup import recent as log_recent
from ..monitoring.collector import RUNTIME
from .deps import Principal, authenticate, error_response, get_state, require_admin

router = APIRouter(prefix="/api", tags=["Сервер"])


@router.get("/health", summary="Проверка доступности")
def health(request: Request) -> dict[str, Any]:
    state = get_state(request)
    return {
        "status": "ok",
        "version": "3.0.0",
        "uptime_s": round(time.time() - state.started_at, 1),
        "queue_paused": state.queue.is_paused,
        "catalog_date": catalog.CATALOG_DATE,
    }


@router.get("/system", summary="Сведения о сервере и оборудовании")
def system(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    hardware = detect(str(state.settings.paths.data))
    return {
        "version": "3.0.0",
        "uptime_s": round(time.time() - state.started_at, 1),
        "hardware": hardware.to_dict(),
        "recommended": recommended_settings(hardware),
        "database": state.db.stats(),
        "paths": {k: str(v) for k, v in vars(state.settings.paths).items()},
        "config_file": str(state.settings.config_file) if state.settings.config_file else None,
        "log_counts": log_counts(),
        "catalog": catalog.catalog_summary(),
        "params_stats": catalog.params_stats(),
    }


@router.get("/queue", summary="Состояние очереди")
def queue_status(request: Request,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    status = state.queue.status()
    status["items"] = state.db.list_jobs(
        status=["queued", "running", "retry", "paused"], limit=200, order="priority DESC")
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
def queue_retry_failed(request: Request, limit: int = 100,
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
    state = get_state(request)
    return state.settings.to_dict(include_secrets=False)


@router.put("/settings", summary="Изменить настройки сервера")
def update_settings(request: Request, values: dict[str, Any] = Body(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    errors = catalog.validate_all({k: v for k, v in values.items()
                                   if k in catalog.PARAMS_BY_KEY})
    if errors:
        raise error_response(ConfigError("; ".join(errors)))
    applied = {}
    for key, value in values.items():
        if key in catalog.PARAMS_BY_KEY:
            state.settings.set(key, value, source="api")
            applied[key] = value
    if "max_concurrent_jobs" in applied:
        state.queue.set_concurrency(int(applied["max_concurrent_jobs"]))
    if "model_cache_size" in applied or "model_idle_unload_s" in applied:
        state.registry.configure(int(state.settings.get("model_cache_size") or 2),
                                 int(state.settings.get("model_idle_unload_s") or 900))
    state.db.add_event(None, "settings_changed", f"Изменено параметров: {len(applied)}")
    RUNTIME.inc("asrhub_config_reloads_total")
    return {"applied": applied}


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
    return {"reset": True}


@router.get("/analytics", summary="Сводная аналитика")
def analytics(request: Request, period: str = Query(default="week"),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    return state.analytics.full_report(period)


@router.get("/analytics/{section}", summary="Отдельный раздел аналитики")
def analytics_section(request: Request, section: str, period: str = "week",
                      principal: Principal = Depends(authenticate)) -> Any:
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
    }
    handler = handlers.get(section)
    if handler is None:
        raise error_response(ConfigError(
            f"Неизвестный раздел аналитики «{section}».",
            hint="Доступные разделы: " + ", ".join(sorted(handlers))))
    return handler(period)


@router.get("/logs", summary="Журнал сервера")
def logs(request: Request, limit: int = 200, level: str = "", search: str = "",
         job_id: str = "", principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return {"items": log_recent(limit=limit, level=level, search=search, job_id=job_id),
            "counts": log_counts()}


@router.get("/events", summary="Лента событий")
def events(request: Request, limit: int = 100,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    return {"items": state.db.get_events(limit=limit)}


@router.get("/keys", summary="Ключи доступа")
def list_keys(request: Request,
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    items = []
    for key, info in state.settings.api_keys.items():
        items.append({
            "key_preview": f"{key[:6]}…{key[-4:]}" if len(key) > 12 else "***",
            "name": info.get("name"),
            "role": info.get("role", "user"),
            "enabled": info.get("enabled", True),
            "rate_limit": info.get("rate_limit", 0),
        })
    return {"items": items}


@router.post("/keys", summary="Создать ключ доступа")
def create_key(request: Request, name: str = Body(embed=True),
               role: str = Body(default="user", embed=True),
               rate_limit: int = Body(default=0, embed=True),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    import secrets

    state = get_state(request)
    require_admin(principal)
    if role not in ("admin", "user", "readonly"):
        raise error_response(ConfigError(
            f"Недопустимая роль «{role}».",
            hint="Допустимые роли: admin — полный доступ, user — отправка заданий, "
                 "readonly — только чтение."))
    key = "ah_" + secrets.token_urlsafe(24)
    state.settings.api_keys[key] = {"name": name, "role": role,
                                    "rate_limit": rate_limit, "enabled": True}
    # Без записи на диск ключ жил бы только до перезапуска, тогда как
    # интерфейс обещает пользователю обратное.
    saved = state.settings.persist_api_keys()
    state.db.add_event(None, "key_created", f"Создан ключ «{name}» с ролью {role}")
    return {"key": key, "name": name, "role": role, "persisted": saved,
            "warning": "Ключ показывается один раз — сохраните его."
                       if saved else
                       "Ключ показывается один раз. Внимание: файл конфигурации "
                       "недоступен, поэтому ключ будет действовать только до перезапуска."}


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


@router.get("/metrics", summary="Метрики Prometheus", response_class=PlainTextResponse)
def metrics(request: Request) -> PlainTextResponse:
    state = get_state(request)
    if not state.settings.get("metrics_enabled", True):
        return PlainTextResponse("# экспорт метрик отключён\n", status_code=404)
    return PlainTextResponse(state.analytics.prometheus(),
                             media_type="text/plain; version=0.0.4; charset=utf-8")


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
        results_days=int(state.settings.get("result_retention_days") or 30))
    state.db.vacuum()
    return {"removed": removed}


@router.post("/maintenance/unload-models", summary="Выгрузить модели из памяти")
def unload_models(request: Request,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    state.registry.unload_all()
    return {"unloaded": True}
