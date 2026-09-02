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


# ---------------------------------------------------------------------------
# Регрессии ревизии: слот, кеш, планировщик
# ---------------------------------------------------------------------------

def test_failed_pick_releases_the_slot(data_dir, monkeypatch):
    """Сорвавшийся выбор задания обязан вернуть занятый слот.

    Слот резервировался до двух обращений к базе, а освобождение жило в
    цикле воркера — и срабатывало только для заданий, которые тот успел
    получить. Одна ошибка «база заблокирована» навсегда съедала слот; при
    достижении предела очередь переставала брать работу до перезапуска.
    """
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    from asrhub.config import load
    from asrhub.db import Database
    from asrhub.engines import EngineRegistry
    from asrhub.errors import StorageError
    from asrhub.job_queue import JobQueue

    settings = load()
    db = Database(settings.paths.db)
    queue = JobQueue(db, settings, EngineRegistry())

    def add(name: str, ts: float) -> str:
        return db.create_job({"status": "queued", "filename": name,
                              "model": "demo-simulator", "priority": 50,
                              "created_at": ts, "queued_at": ts,
                              "file_path": "/tmp/a.wav"})

    add("a.wav", 1.0)
    original = db.update_job
    db.update_job = lambda *a, **kw: (_ for _ in ()).throw(StorageError("база заблокирована"))
    with pytest.raises(StorageError):
        queue._next_job(0)
    db.update_job = original
    assert queue._running == {}, "слот не освобождён после ошибки записи"

    # Второй путь: задание исчезло между выборкой и чтением.
    add("b.wav", 2.0)
    original_get = db.get_job
    db.get_job = lambda _: None
    assert queue._next_job(0) is None
    db.get_job = original_get
    assert queue._running == {}, "слот не освобождён, когда задание исчезло"

    # Очередь по-прежнему берёт работу.
    add("c.wav", 3.0)
    assert queue._next_job(0) is not None, "очередь встала после сорвавшихся попыток"


def test_light_listing_carries_what_consumers_need():
    """Облегчённый список обязан нести поля, которыми пользуются потребители.

    Без deadline не работала политика планирования по сроку, без
    error_message и filename разбор ошибок в аналитике показывал пустые
    столбцы у всех строк — и то и другое молча.
    """
    from asrhub.db import Database

    columns = {c.strip() for c in Database.LIGHT_COLUMNS.split(",")}
    for needed in ("deadline", "error_code", "error_message", "error_hint",
                   "filename", "rtf", "media_duration_s"):
        assert needed in columns, f"{needed} пропал из облегчённого набора"
    for heavy in ("text", "file_path", "params"):
        assert heavy not in columns, f"{heavy} не должен ехать в облегчённом списке"


def test_client_script_has_no_known_defects():
    """Статические проверки клиента: то, что уже ломалось.

    Все три места воспроизводились запуском против живого сервера, поэтому
    закрываем их проверкой исходника — поднимать ради этого сервер в тестах
    незачем.
    """
    from pathlib import Path

    client = (Path(__file__).resolve().parent.parent
              / "scripts" / "client" / "asrctl").read_text(encoding="utf-8")

    # Ключ не должен уходить аргументом curl: аргументы видны в ps.
    assert '-H "X-API-Key: ${API_KEY}"' not in client, "ключ снова в аргументах curl"
    assert "--config -" in client, "ключ не передаётся через стандартный ввод"

    # Профиль — умолчание, а не приказ.
    assert "SERVER_EXPLICIT" in client and "KEY_EXPLICIT" in client, \
        "профиль снова затирает --server и --key"

    # Имя файла в кавычках: запятая и точка с запятой в имени ломали отправку.
    assert '-F "file=@\\"${file}\\""' in client, "имя файла без кавычек"

    # Настройки собираются и без python3.
    assert "build_settings" in client, "сборка настроек снова зависит от python3"

    # У ожидания есть общий срок.
    assert "WAIT_LIMIT" in client, "ожидание снова без предела"


def test_install_scripts_have_no_known_defects():
    """Статические проверки сценариев установки."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "scripts"
    install = (root / "install.sh").read_text(encoding="utf-8")
    update = (root / "update.sh").read_text(encoding="utf-8")
    common = (root / "lib" / "common.sh").read_text(encoding="utf-8")
    uninstall_ps = (root / "uninstall.ps1").read_text(encoding="utf-8")

    # readarray — bash 4+, а macOS поставляет 3.2. Ищем вызов, не упоминание.
    calls = [line for line in install.split("\n")
             if "readarray" in line and not line.lstrip().startswith("#")]
    assert not calls, f"readarray ломает установку на macOS: {calls}"

    # Массив читается в docker-режиме, а заполняется только в нативном.
    assert install.index("FAILED_ENGINES=()") < install.index("${#FAILED_ENGINES[@]}"), \
        "FAILED_ENGINES читается раньше объявления"

    # Обновление из самой установки не должно удалять источник.
    assert "SRC_REAL" in update and "DST_REAL" in update, \
        "update.sh снова удалит каталог, из которого копирует"

    # Без терминала берётся умолчание, а не «да».
    assert "[[ ! -t 0 ]] && return 0" not in common, \
        "confirm снова соглашается без терминала"

    # Пробный запуск не должен трогать службу в дочернем процессе.
    assert "export ASRHUB_DRY_RUN" in common, "флаг пробного запуска не передаётся дальше"

    # На Windows каталог данных лежит внутри каталога программы.
    assert "dataInsidePrefix" in uninstall_ps, \
        "удаление на Windows снова снесёт данные вместе с программой"
