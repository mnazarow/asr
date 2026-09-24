"""Заход 45: оборудование, проверка окружения, самопроверка, аналитика.

`detect()` кешировал на весь процесс и свободное место, и доступную
память, и свободную видеопамять — установщик модели проверял место по
состоянию на момент запуска. Рекомендации брали ядра и память машины, а не
контейнера. `--check` проверял каталоги по умолчанию вместо `models_dir` и
`temp_dir`. Самопроверка при каждом опросе панели запускала nvidia-smi,
dkms и modinfo, а свежую установку объявляла неисправной из-за копии,
которой ещё не могло быть.
"""
from __future__ import annotations

import time
from collections import namedtuple
from pathlib import Path
from typing import Any

import pytest
from asrhub import hardware
from asrhub.hardware import GPUInfo, HardwareInfo
from fastapi.testclient import TestClient

Место = namedtuple("Место", "total used free")
ГИБ = 1024 ** 3


def _железо(**поля: Any) -> HardwareInfo:
    основа = {"os_name": "Linux", "os_version": "6", "arch": "x86_64", "cpu_model": "cpu",
              "cpu_cores_physical": 8, "cpu_cores_logical": 16, "ram_total_gb": 16.0,
              "ram_available_gb": 10.0, "disk_free_gb": 0.0, "ffmpeg": True}
    return HardwareInfo(**{**основа, **поля})


def test_изменчивое_меряется_заново_а_постоянное_один_раз(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    hardware.detect.cache_clear()
    вызовов: list[int] = []
    monkeypatch.setattr(hardware, "_cpu_model", lambda: вызовов.append(1) or "Процессор")
    места = iter([Место(500 * ГИБ, 400 * ГИБ, 100 * ГИБ), Место(500 * ГИБ, 490 * ГИБ, 10 * ГИБ)])
    monkeypatch.setattr(hardware.shutil, "disk_usage", lambda путь: next(места))
    памяти = iter([(16.0, 12.0), (16.0, 12.0), (16.0, 3.0)])
    monkeypatch.setattr(hardware, "_memory_gb", lambda: next(памяти))
    try:
        первое = hardware.detect(str(tmp_path))
        второе = hardware.detect(str(tmp_path))
    finally:
        hardware.detect.cache_clear()
    assert (первое.disk_free_gb, второе.disk_free_gb) == (100.0, 10.0)
    assert (первое.ram_available_gb, второе.ram_available_gb) == (12.0, 3.0)
    assert len(вызовов) == 1, "постоянное определялось заново"
    assert первое is not второе, "общий объект: правка одного меняла бы кеш"
    assert any("10.0 ГБ" in п for п in второе.warnings), второе.warnings
    assert not any("ГБ. Полный" in п for п in первое.warnings)


class Часы:
    def __init__(self) -> None:
        self.t = 1_000.0

    def time(self) -> float:
        return self.t


def test_свободная_видеопамять_свежая_но_не_на_каждый_вызов(monkeypatch: pytest.MonkeyPatch):
    основа = _железо(accelerator="cuda",
                     gpus=[GPUInfo(index=0, name="RTX", memory_total_mb=24000,
                                   memory_free_mb=20000)])
    monkeypatch.setattr(hardware, "_постоянное", lambda data_dir=".": основа)
    monkeypatch.setattr(hardware.shutil, "which", lambda имя: "/usr/bin/" + имя)
    ответы = iter(["0, 5000", "0, 3000"])
    запросов: list[list[str]] = []

    def выполнить(команда: list[str], **_: Any) -> str:
        запросов.append(команда)
        return next(ответы)

    monkeypatch.setattr(hardware, "_run", выполнить)
    часы = Часы()
    monkeypatch.setattr(hardware, "time", часы)
    monkeypatch.setitem(hardware._видеопамять, "at", 0.0)
    первое = hardware.detect(".")
    часы.t += 5
    второе = hardware.detect(".")
    часы.t += hardware.ВИДЕОПАМЯТЬ_С
    третье = hardware.detect(".")
    assert [п.gpus[0].memory_free_mb for п in (первое, второе, третье)] == [5000, 5000, 3000]
    assert len(запросов) == 2
    assert основа.gpus[0].memory_free_mb == 20000, "кеш постоянного испорчен"


def test_пределы_контейнера_cgroup_v2_и_v1(tmp_path: Path):
    v2 = tmp_path / "v2"
    v2.mkdir()
    (v2 / "cpu.max").write_text("200000 100000\n")
    (v2 / "memory.max").write_text(f"{8 * ГИБ}\n")
    (v2 / "memory.current").write_text(f"{ГИБ}\n")
    assert hardware.пределы_контейнера(str(v2)) == (2.0, 8.0 * ГИБ, 1.0 * ГИБ)

    v1 = tmp_path / "v1"
    (v1 / "cpu").mkdir(parents=True)
    (v1 / "memory").mkdir()
    (v1 / "cpu" / "cpu.cfs_quota_us").write_text("150000\n")
    (v1 / "cpu" / "cpu.cfs_period_us").write_text("100000\n")
    (v1 / "memory" / "memory.limit_in_bytes").write_text(f"{4 * ГИБ}\n")
    (v1 / "memory" / "memory.usage_in_bytes").write_text(f"{ГИБ}\n")
    assert hardware.пределы_контейнера(str(v1)) == (1.5, 4.0 * ГИБ, 1.0 * ГИБ)

    без = tmp_path / "без"
    (без / "memory").mkdir(parents=True)
    (без / "cpu.max").write_text("max 100000\n")
    (без / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    assert hardware.пределы_контейнера(str(без)) == (0.0, 0.0, 0.0)


def test_рекомендации_по_квоте_контейнера():
    """Контейнер с --cpus=4 на тридцати двух ядрах: потоков — по квоте."""
    железо = _железо(cpu_cores_physical=32, cpu_cores_logical=64, cpu_limit=4.0,
                     accelerator="cpu")
    assert железо.cpu_cores_available == 4
    совет = hardware.recommended_settings(железо)
    assert совет["cpu_threads"] == 3
    assert совет["max_concurrent_jobs"] == 1
    assert hardware.recommended_settings(_железо(cpu_cores_physical=32,
                                                 accelerator="cpu"))["cpu_threads"] == 31


def test_доступная_память_не_больше_предела_контейнера(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(hardware, "_постоянное",
                        lambda data_dir=".": _железо(ram_total_gb=8.0, memory_limit_gb=8.0,
                                                     ram_host_gb=128.0))
    monkeypatch.setattr(hardware, "_memory_gb", lambda: (128.0, 100.0))
    monkeypatch.setattr(hardware, "пределы_контейнера",
                        lambda корень="": (0.0, 8.0 * ГИБ, 6.0 * ГИБ))
    железо = hardware.detect(".")
    assert железо.ram_available_gb == 2.0, "в контейнере видна память всей машины"


# ---------------------------------------------------------------------------
# python -m asrhub --check
# ---------------------------------------------------------------------------


def test_проверка_окружения_смотрит_в_models_dir_и_temp_dir(data_dir: Path, tmp_path: Path,
                                                           monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture):
    from asrhub import doctor
    from asrhub.config import load

    модели = tmp_path / "нет" / "models"
    временные = tmp_path / "нет" / "tmp"
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MODELS_DIR", str(модели))
    monkeypatch.setenv("ASRHUB_TEMP_DIR", str(временные))
    настройки = load()
    assert doctor.run_checks(настройки) is False
    первый = capsys.readouterr().out
    assert str(модели) in первый and str(временные) in первый
    doctor.run_checks(настройки)
    второй = capsys.readouterr().out

    def итог(текст: str) -> str:
        return текст.rsplit("пройдено:", 1)[1].split("\n", 1)[0]

    assert итог(первый) == итог(второй), "счётчики второго прогона сложились с первым"


# ---------------------------------------------------------------------------
# Самопроверка
# ---------------------------------------------------------------------------


def test_приговор_драйвера_не_на_каждый_опрос(monkeypatch: pytest.MonkeyPatch):
    from asrhub import selfcheck

    вызовов: list[int] = []
    monkeypatch.setattr(hardware, "проверить_драйвер",
                        lambda **_: вызовов.append(1) or {"state": "unknown"})
    monkeypatch.setitem(selfcheck._ДРАЙВЕР, "приговор", None)
    for _ in range(3):
        selfcheck._драйвер_проверка([], глубоко=False)
    assert len(вызовов) == 1, "nvidia-smi, dkms и modinfo на каждый опрос панели"
    selfcheck._драйвер_проверка([], глубоко=True)
    assert len(вызовов) == 2, "глубокая проверка обязана спросить заново"


def test_свежая_установка_без_копий_не_авария(data_dir: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    from asrhub import selfcheck
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app):
        hub = app.state.hub

        def пункт() -> dict[str, Any]:
            selfcheck._КЕШ_КОПИЙ.clear()
            свод = selfcheck.состояние(hub)
            раздел = next(к for к in свод["components"] if к["id"] == "backup")
            return next(п for п in раздел["checks"] if п["id"] == "last")

        assert пункт()["state"] == "info"
        hub.db.execute("INSERT INTO events (job_id, ts, kind, message) VALUES (?,?,?,?)",
                       (None, time.time() - 10 * 86400, "старое", "давно работает"))
        assert пункт()["state"] == "fail"


# ---------------------------------------------------------------------------
# Аналитика: разбивка по стадиям
# ---------------------------------------------------------------------------


def test_разбивка_по_стадиям_показывает_разделение_по_говорящим(client):
    db = client.app.state.hub.db
    сейчас = time.time()
    db.create_job({"id": "s1", "filename": "a.wav", "status": "completed",
                   "created_at": сейчас, "finished_at": сейчас, "inference_s": 10.0,
                   "diarization_s": 30.0, "vad_s": 1.0})
    стадии = client.get("/api/analytics/overview?period=day").json()["stages"]
    доли = dict(zip(стадии["labels"], стадии["seconds"], strict=True))
    assert доли["Разделение по говорящим"] == 30.0
    assert доли["Поиск речи"] == 1.0
    assert "Выравнивание" not in доли, "стадия, которой не было, в легенде «0 с»"
