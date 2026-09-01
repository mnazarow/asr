"""Проверки очереди, классификации ошибок и конфигурации."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from asrhub import errors
from asrhub.config import generate_example_config, load
from asrhub.db import Database
from asrhub.engines import EngineRegistry, engine_status

# --- классификация ошибок ---------------------------------------------------

def test_oom_is_retryable():
    err = errors.classify_exception(RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"))
    assert err.code == "out_of_memory"
    assert err.retryable is True
    assert "batch_size" in err.hint


def test_cudnn_mismatch_gives_version_table():
    err = errors.classify_exception(
        OSError("Could not load library libcudnn_ops_infer.so.8"), engine="faster_whisper")
    assert err.code == "dependency_missing"
    assert "ctranslate2==4.4.0" in err.hint


def test_missing_module_maps_to_dependency():
    err = errors.classify_exception(ModuleNotFoundError("No module named 'gigaam'"), engine="gigaam")
    assert err.code == "dependency_missing"


def test_gated_model_error():
    err = errors.classify_exception(
        Exception("401 Client Error: gated repo"), model="pyannote/x")
    assert err.code == "gated_model"
    assert "huggingface" in err.hint.lower()


def test_network_error_is_retryable():
    err = errors.classify_exception(OSError("Max retries exceeded with url: connection refused"))
    assert err.retryable is True


def test_disk_full():
    exc = OSError("No space left on device")
    exc.errno = 28
    assert errors.classify_exception(exc).code == "storage_error"


def test_error_serialisation():
    err = errors.FileTooLarge(3000.0, 2048)
    payload = err.to_dict()
    assert payload["code"] == "file_too_large"
    assert "413" not in payload["message"]
    assert payload["hint"]


def test_every_error_has_hint():
    classes = [value for value in vars(errors).values()
               if isinstance(value, type) and issubclass(value, errors.ASRHubError)]
    assert len(classes) > 15
    for cls in classes:
        assert cls.hint, f"{cls.__name__}: нет подсказки по устранению"


# --- конфигурация ------------------------------------------------------------

def test_config_defaults_load(data_dir: Path):
    settings = load()
    assert settings.get("beam_size") == 5
    assert settings.paths.data.exists()


def test_env_override(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_BEAM_SIZE", "9")
    settings = load()
    assert settings["beam_size"] == 9
    assert settings.sources["beam_size"].startswith("env:")


def test_env_validation(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_BEAM_SIZE", "1000")
    with pytest.raises(errors.ConfigError):
        load()


def test_example_config_is_complete():
    text = generate_example_config()
    from asrhub import catalog

    for param in catalog.PARAMS:
        assert f"{param.key}:" in text, f"{param.key} отсутствует в примере конфигурации"
    assert text.count("Рекомендация:") > 80


def test_settings_roundtrip(data_dir: Path, tmp_path: Path):
    settings = load()
    settings.set("beam_size", 3)
    target = settings.save(tmp_path / "config.yaml")
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "beam_size: 3" in text


def test_merged_rejects_bad_values(data_dir: Path):
    settings = load()
    with pytest.raises(errors.ConfigError):
        settings.merged({"vad_threshold": 5.0})


# --- база данных --------------------------------------------------------------

def test_database_lifecycle(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    job_id = db.create_job({"filename": "a.wav", "model": "m", "engine": "e",
                            "media_duration_s": 60.0, "params": {"beam_size": 5}})
    assert db.get_job(job_id)["status"] == "queued"

    db.update_job(job_id, status="completed", rtf=0.2, words_count=100)
    assert db.get_job(job_id)["rtf"] == 0.2

    db.save_segments(job_id, [{"start": 0, "end": 1, "text": "тест", "confidence": 0.9}])
    assert db.get_segments(job_id)[0]["text"] == "тест"

    db.bump_model_stats("m", "e", ok=True, audio_s=60, processing_s=12, rtf=0.2)
    stats = db.model_stats()[0]
    assert stats["jobs_ok"] == 1 and stats["rtf_avg"] == 0.2

    assert db.count_jobs(status="completed") == 1
    db.delete_job(job_id)
    assert db.get_job(job_id) is None


def test_database_cleanup(tmp_path: Path):
    db = Database(tmp_path / "cleanup.db")
    old = time.time() - 60 * 86400
    job_id = db.create_job({"filename": "старое.wav", "params": {}})
    db.update_job(job_id, status="completed", finished_at=old)
    removed = db.cleanup(results_days=30)
    assert removed["jobs"] == 1


# --- движки -------------------------------------------------------------------

def test_engine_status_lists_all():
    items = engine_status()
    assert len(items) >= 15
    demo = next(i for i in items if i["id"] == "demo")
    assert demo["available"]


def test_registry_resolves_model():
    registry = EngineRegistry(2, 900)
    spec, engine = registry.resolve({"model": "gigaam-v3-rnnt", "engine": "auto"})
    assert spec.id == "gigaam-v3-rnnt"
    assert engine == "gigaam"


def test_registry_unknown_model_suggests():
    registry = EngineRegistry(2, 900)
    with pytest.raises(errors.ModelNotFound) as info:
        registry.resolve({"model": "gigaam-v99"})
    assert info.value.details["suggestions"], "должны предлагаться похожие модели"


def test_registry_missing_engine_is_reported():
    registry = EngineRegistry(2, 900)
    with pytest.raises(errors.DependencyMissing):
        registry.get({"model": "gigaam-v3-rnnt", "engine": "gigaam"})


def test_registry_evicts_by_cache_size():
    registry = EngineRegistry(1, 900)
    first = registry.get({"model": "demo-simulator", "engine": "demo"})
    assert first is not None
    assert len(registry.loaded()) == 1
