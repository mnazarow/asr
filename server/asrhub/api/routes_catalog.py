"""Маршруты каталога: модели, движки, параметры, пресеты, менеджер моделей."""
from __future__ import annotations

import shutil
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from .. import catalog, model_files
from ..engines import ENGINE_CLASSES, engine_status
from ..errors import ASRHubError, ModelNotFound, PresetNotFound
from ..logging_setup import get_logger
from .deps import Principal, authenticate, error_response, get_state, require_admin

log = get_logger("api.catalog")
router = APIRouter(prefix="/api", tags=["Каталог"])

_downloads: dict[str, dict[str, Any]] = {}


@router.get("/catalog", summary="Полный каталог: модели, движки, параметры, пресеты")
def full_catalog() -> dict[str, Any]:
    return catalog.full_catalog()


@router.get("/models", summary="Список моделей")
def list_models(
    request: Request,
    language: str | None = None,
    engine: str | None = None,
    family: str | None = None,
    streaming: bool | None = None,
    diarization: bool | None = None,
    commercial_only: bool = False,
    #: Только те модели, чьи веса лежат на диске. Клиент командной строки
    #: принимал такой ключ и молча его игнорировал: `asrctl models
    #: --installed` печатал весь каталог из семидесяти с лишним моделей.
    installed: bool | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    items = catalog.MODELS
    if language:
        items = [m for m in items
                 if language in m.languages or any(x.startswith("multi") for x in m.languages)]
    if engine:
        items = [m for m in items if m.engine == engine]
    if family:
        items = [m for m in items if m.family == family]
    if streaming is not None:
        items = [m for m in items if m.streaming == streaming]
    if diarization is not None:
        items = [m for m in items if m.diarization == diarization]
    if commercial_only:
        items = [m for m in items if m.commercial_use]
    if installed is not None:
        state = get_state(request)
        models_dir = Path(state.settings.get("models_dir") or state.settings.paths.models)
        items = [m for m in items
                 if bool(model_files.find_local(models_dir, m.source)) is installed]
    if search:
        needle = search.lower()
        items = [m for m in items
                 if needle in m.id.lower() or needle in m.name.lower()
                 or needle in m.family.lower() or any(needle in t for t in m.tags)]
    return {
        "items": [m.to_dict() for m in items],
        "total": len(items),
        "summary": catalog.catalog_summary(),
        "excluded": catalog.EXCLUDED_MODELS,
    }


@router.get("/models/recommended", summary="Рекомендуемые модели для русского языка")
def recommended(limit: int = 8) -> dict[str, Any]:
    items = catalog.recommended_ru(limit)
    return {"items": [{**m.to_dict(), "mean_ru_wer": catalog.mean_ru_wer(m)} for m in items],
            "note": ("Значения WER измерены на разных наборах данных и разными авторами. "
                     "Прямое сравнение чисел между семействами моделей некорректно — "
                     "используйте таблицу как ориентир, а не как рейтинг.")}


@router.get("/models/{model_id}", summary="Карточка модели")
def get_model(model_id: str) -> dict[str, Any]:
    spec = catalog.get_model(model_id)
    if spec is None:
        raise error_response(ModelNotFound(model_id, catalog.suggest_models(model_id)))
    data = spec.to_dict()
    data["mean_ru_wer"] = catalog.mean_ru_wer(spec)
    engine_spec = catalog.get_engine(spec.engine)
    data["engine_info"] = engine_spec.to_dict() if engine_spec else None
    data["params"] = [p.to_dict() for p in catalog.params_for_engine(spec.engine)]
    return data


@router.get("/models/{model_id}/status", summary="Загружены ли веса модели")
def model_status(request: Request, model_id: str,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    spec = catalog.get_model(model_id)
    if spec is None:
        raise error_response(ModelNotFound(model_id, catalog.suggest_models(model_id)))
    models_dir = Path(state.settings.get("models_dir") or state.settings.paths.models)
    found = model_files.find_local(models_dir, spec.source)
    cls = ENGINE_CLASSES.get(spec.engine)
    available, reason = cls.check_available() if cls else (False, "движок неизвестен")
    return {
        "model": model_id,
        "downloaded": bool(found),
        "path": str(found) if found else None,
        "size_mb": round(model_files.directory_size(found) / 1024 / 1024, 1) if found else None,
        "engine_available": available,
        "engine_reason": reason,
        "download": _downloads.get(model_id),
    }


@router.post("/models/{model_id}/download", summary="Загрузить веса модели")
def download_model(request: Request, model_id: str,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    spec = catalog.get_model(model_id)
    if spec is None:
        raise error_response(ModelNotFound(model_id))
    if _downloads.get(model_id, {}).get("status") == "running":
        return _downloads[model_id]

    entry = {"status": "running", "model": model_id, "progress": 0.0, "message": "запуск"}
    _downloads[model_id] = entry

    def worker() -> None:
        try:
            models_dir = Path(state.settings.get("models_dir") or state.settings.paths.models)
            models_dir.mkdir(parents=True, exist_ok=True)
            if spec.source.startswith("http"):
                from ..engines.vosk_engine import VoskEngine

                engine = VoskEngine(spec, {})
                entry["message"] = "загрузка архива"
                engine.download({"models_dir": str(models_dir)})
            else:
                from huggingface_hub import snapshot_download  # type: ignore

                entry["message"] = "загрузка с Hugging Face"
                snapshot_download(
                    repo_id=spec.source, revision=spec.revision or None,
                    cache_dir=str(models_dir),
                    token=state.settings.hf_token or None)
            entry.update(status="completed", progress=1.0, message="готово")
            # Веса сменились — запомненный отпечаток больше не годится,
            # иначе кеш до минуты отдавал бы результаты прошлой версии.
            model_files.forget(models_dir)
            state.db.add_event(None, "model_downloaded", f"Загружена модель {model_id}")
        except Exception as exc:
            entry.update(status="failed", message=str(exc)[:500])
            log.error("Загрузка модели %s не удалась: %s", model_id, exc)

    threading.Thread(target=worker, name=f"download-{model_id}", daemon=True).start()
    return entry


@router.delete("/models/{model_id}", summary="Удалить веса модели с диска")
def remove_model(request: Request, model_id: str,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    spec = catalog.get_model(model_id)
    if spec is None:
        raise error_response(ModelNotFound(model_id, catalog.suggest_models(model_id)))
    models_dir = Path(state.settings.get("models_dir") or state.settings.paths.models)
    found = model_files.find_local(models_dir, spec.source)
    if not found:
        return {"model": model_id, "removed": False, "message": "веса не найдены на диске"}
    freed = model_files.directory_size(found)
    shutil.rmtree(found, ignore_errors=True)
    model_files.forget(models_dir)
    state.db.add_event(None, "model_removed", f"Удалены веса модели {model_id}")
    return {"model": model_id, "removed": True, "freed_mb": round(freed / 1024 / 1024, 1)}


@router.get("/engines", summary="Состояние движков")
def list_engines() -> dict[str, Any]:
    return {"items": engine_status()}


@router.get("/params", summary="Все параметры с описаниями и примерами")
def list_params(engine: str | None = None, group: str | None = None,
                advanced: bool | None = None) -> dict[str, Any]:
    items = catalog.PARAMS
    if engine:
        items = catalog.params_for_engine(engine)
    if group:
        items = [p for p in items if p.group == group]
    if advanced is not None:
        items = [p for p in items if p.advanced == advanced]
    return {
        "groups": catalog.GROUPS,
        "items": [p.to_dict() for p in items],
        "defaults": catalog.defaults(),
        "stats": catalog.params_stats(),
    }


@router.get("/presets", summary="Готовые наборы настроек")
def list_presets() -> dict[str, Any]:
    return {"items": [p.to_dict() for p in catalog.PRESETS]}


@router.post("/presets/{preset_id}/apply", summary="Применить пресет к серверным настройкам")
def apply_preset(request: Request, preset_id: str,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state = get_state(request)
    require_admin(principal)
    preset = catalog.get_preset(preset_id)
    if preset is None:
        raise error_response(PresetNotFound(preset_id, [p.id for p in catalog.PRESETS]))
    applied: dict[str, Any] = {}
    for key, value in preset.values.items():
        try:
            state.settings.set(key, value, source=f"preset:{preset_id}")
            applied[key] = value
        except ASRHubError as exc:
            log.warning("Пресет %s: параметр %s не применён (%s)", preset_id, key, exc.message)
    state.db.add_event(None, "preset_applied", f"Применён пресет «{preset.name}»")
    return {"preset": preset_id, "applied": applied}
