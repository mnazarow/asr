"""Проверки каталога моделей, движков и параметров."""
from __future__ import annotations

import json

from asrhub import catalog


def test_catalog_loads():
    assert len(catalog.MODELS) > 50
    assert len(catalog.ENGINES) > 10
    assert len(catalog.PARAMS) > 90
    assert len(catalog.PRESETS) >= 10


def test_model_ids_unique():
    ids = [m.id for m in catalog.MODELS]
    assert len(ids) == len(set(ids)), "идентификаторы моделей должны быть уникальны"


def test_param_keys_unique():
    keys = [p.key for p in catalog.PARAMS]
    assert len(keys) == len(set(keys)), "ключи параметров должны быть уникальны"


def test_every_model_has_known_engine():
    engines = {e.id for e in catalog.ENGINES}
    for model in catalog.MODELS:
        assert model.engine in engines, f"{model.id}: неизвестный движок {model.engine}"


def test_every_param_has_description_and_examples():
    for param in catalog.PARAMS:
        assert param.description.strip(), f"{param.key}: нет описания"
        assert param.label.strip(), f"{param.key}: нет названия"
        assert param.examples, f"{param.key}: нет примеров настройки"
        assert param.group in {g["id"] for g in catalog.GROUPS}, f"{param.key}: чужая группа"


def test_param_defaults_validate():
    errors = catalog.validate_all(catalog.defaults())
    assert not errors, f"значения по умолчанию не проходят проверку: {errors}"


def test_enum_defaults_are_allowed_values():
    for param in catalog.PARAMS:
        if param.type == "enum" and param.options:
            allowed = {o["value"] for o in param.options}
            assert param.default in allowed, \
                f"{param.key}: значение по умолчанию {param.default!r} вне списка"


def test_presets_reference_known_params_and_models():
    keys = set(catalog.PARAMS_BY_KEY)
    model_ids = set(catalog.MODELS_BY_ID)
    for preset in catalog.PRESETS:
        for key, value in preset.values.items():
            assert key in keys, f"пресет {preset.id}: неизвестный параметр {key}"
            ok, message = catalog.validate_value(key, value)
            assert ok, f"пресет {preset.id}: {message}"
        model = preset.values.get("model")
        if model:
            assert model in model_ids, f"пресет {preset.id}: неизвестная модель {model}"


def test_benchmarks_have_sources():
    for model in catalog.MODELS:
        for bench in model.benchmarks:
            assert bench.source.strip(), f"{model.id}: измерение без источника"
            assert 0 <= bench.value <= 100, f"{model.id}: подозрительное значение {bench.value}"


def test_validation_rejects_out_of_range():
    ok, message = catalog.validate_value("beam_size", 999)
    assert not ok and "максимум" in message


def test_validation_rejects_unknown_enum():
    ok, _ = catalog.validate_value("vad_backend", "не-существует")
    assert not ok


def test_catalog_serialises_to_json():
    payload = catalog.full_catalog()
    text = json.dumps(payload, ensure_ascii=False)
    assert len(text) > 100_000
    assert "gigaam-v3-rnnt" in text


def test_russian_models_present():
    russian = [m for m in catalog.MODELS if m.ru_quality != catalog.Quality.NONE]
    assert len(russian) > 20
    best = catalog.recommended_ru(3)
    assert best, "должны быть рекомендации для русского языка"


def test_excluded_models_documented():
    for item in catalog.EXCLUDED_MODELS:
        assert item["name"] and item["license"] and item["reason"]


def test_param_impact_is_well_formed():
    """`impact` — словарь с фиксированными ключами, а не свободный текст.

    Строка в этом поле не мешает серверу и молча доходит до генератора
    документации, где сборка падает на `.items()`. Проверка ловит это на
    месте объявления параметра.
    """
    allowed_keys = {"quality", "speed", "memory"}
    allowed_values = {"up", "down", "neutral"}
    for spec in catalog.PARAMS:
        assert isinstance(spec.impact, dict), f"{spec.key}: impact должен быть словарём"
        assert set(spec.impact) <= allowed_keys, f"{spec.key}: лишние ключи в impact"
        assert set(spec.impact.values()) <= allowed_values, \
            f"{spec.key}: недопустимое значение в impact"
