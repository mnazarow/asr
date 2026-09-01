"""Каталог ASR Hub: модели, движки, параметры и пресеты."""
from __future__ import annotations

from .engines import ENGINES, ENGINES_BY_ID, get_engine
from .models import (
    CATALOG_DATE,
    EXCLUDED_MODELS,
    MODELS,
    MODELS_BY_ID,
    SOURCES,
    catalog_summary,
    get_model,
    mean_ru_wer,
    models_for_engine,
    models_for_language,
    recommended_ru,
    suggest_models,
)
from .params import (
    GROUPS,
    GROUPS_BY_ID,
    PARAMS,
    PARAMS_BY_KEY,
    defaults,
    get_param,
    params_for_engine,
    params_for_group,
    translate_key,
    validate_all,
    validate_value,
)
from .params import stats as params_stats
from .presets import PRESETS, PRESETS_BY_ID, get_preset
from .schema import (
    Benchmark,
    EngineSpec,
    Maturity,
    ModelSpec,
    ParamExample,
    ParamSpec,
    PresetSpec,
    Quality,
    Timestamps,
)

__all__ = [
    "CATALOG_DATE", "EXCLUDED_MODELS", "MODELS", "MODELS_BY_ID", "SOURCES",
    "ENGINES", "ENGINES_BY_ID", "PARAMS", "PARAMS_BY_KEY", "GROUPS", "GROUPS_BY_ID",
    "PRESETS", "PRESETS_BY_ID",
    "Benchmark", "EngineSpec", "Maturity", "ModelSpec", "ParamExample", "ParamSpec",
    "PresetSpec", "Quality", "Timestamps",
    "catalog_summary", "get_model", "get_engine", "get_param", "get_preset", "suggest_models",
    "mean_ru_wer", "models_for_engine", "suggest_models", "models_for_language", "recommended_ru",
    "defaults", "params_for_engine", "params_for_group", "translate_key",
    "validate_all", "validate_value", "params_stats",
]


def full_catalog() -> dict[str, object]:
    """Полный каталог в виде обычных структур — для API и генератора документации."""
    return {
        "date": CATALOG_DATE,
        "models": [m.to_dict() for m in MODELS],
        "engines": [e.to_dict() for e in ENGINES],
        "params": [p.to_dict() for p in PARAMS],
        "groups": GROUPS,
        "presets": [p.to_dict() for p in PRESETS],
        "excluded": EXCLUDED_MODELS,
        "sources": SOURCES,
        "summary": catalog_summary(),
        "params_stats": params_stats(),
    }
