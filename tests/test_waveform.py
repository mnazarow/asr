"""Полоса громкости: совместимость с phone_asr и разбивка по каналам.

Смысл этих проверок — не «код не падает», а «числа те же». Огибающую
рисует сторона, которая уже получает её от phone_asr, и любое расхождение
в округлении или в наборе полей выглядит там как поехавшая полоса.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import time
import wave
from pathlib import Path

import pytest
from asrhub.engines.base import Segment
from asrhub.pipeline import waveform as wf

# ---------------------------------------------------------------------------
# Эталон: функция phone_asr дословно (app/src/routes/asr.py)
# ---------------------------------------------------------------------------

def phone_asr_generate_waveform(audio, sample_rate=16000, interval_s=1):
    """Дословная копия расчёта из phone_asr — источник истины для сравнения."""
    import numpy as np

    samples_per_interval = int(sample_rate * interval_s)
    num_intervals = int(np.ceil(len(audio) / samples_per_interval))
    waveform = []
    for i in range(num_intervals):
        start = i * samples_per_interval
        end = start + samples_per_interval
        segment = audio[start:end]
        if len(segment) == 0:
            continue
        amplitude = float(np.mean(np.abs(segment)))
        waveform.append({"time": round(i * interval_s, 3),
                         "amplitude": round(amplitude, 6)})
    return waveform


def _signal(seconds: float, rate: int = 16000, freq: float = 320.0,
            silence: tuple[float, float] | None = None):
    import numpy as np

    t = np.arange(int(seconds * rate), dtype="float32") / rate
    data = (0.4 * np.sin(2 * math.pi * freq * t)).astype("float32")
    if silence:
        data[int(silence[0] * rate):int(silence[1] * rate)] = 0.0
    return data


def _wav(path: Path, samples, rate: int = 16000) -> Path:
    pcm = b"".join(struct.pack("<h", int(max(-1.0, min(1.0, float(v))) * 32767))
                   for v in samples)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)
    return path


# ---------------------------------------------------------------------------
# Расчёт
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,silence", [
    (6.0, (3.0, 4.2)),      # речь, пауза, речь
    (2.4, None),            # неполный хвост: последний интервал короче секунды
    (0.3, None),            # запись короче интервала
])
def test_envelope_matches_phone_asr(seconds, silence):
    """Числа обязаны совпадать с phone_asr до последнего знака."""
    pytest.importorskip("numpy")
    data = _signal(seconds, silence=silence)
    assert wf._envelope(data, 16000, 1.0) == phone_asr_generate_waveform(data, 16000, 1)


def test_envelope_empty_input():
    assert wf._envelope([], 16000, 1.0) == []


def test_envelope_fallback_matches_numpy(monkeypatch):
    """Путь без numpy обязан давать те же числа, а не «примерно те же».

    numpy прячется через sys.modules: подмена __import__ незаметно
    проваливается, если модуль уже в кеше, и проверка тогда сравнивает
    numpy сам с собой.
    """
    import builtins
    import sys

    pytest.importorskip("numpy")
    data = _signal(3.7, silence=(1.0, 2.0))
    expected = wf._envelope(data, 16000, 1.0)

    real_import = builtins.__import__

    def without_numpy(name, *args, **kwargs):
        if name == "numpy" or name.startswith("numpy."):
            raise ModuleNotFoundError("numpy отключён для проверки")
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, "numpy", raising=False)
    monkeypatch.setattr(builtins, "__import__", without_numpy)
    with pytest.raises(ModuleNotFoundError):        # заглушка и правда работает
        import numpy  # noqa: F401
    assert wf._envelope([float(v) for v in data], 16000, 1.0) == expected


def test_envelope_amplitude_range():
    """Тишина у самого нуля, речь заметно выше — иначе полосу не прочитать."""
    pytest.importorskip("numpy")
    points = wf._envelope(_signal(4.0, silence=(1.0, 3.0)), 16000, 1.0)
    assert points[1]["amplitude"] == 0.0
    assert points[0]["amplitude"] > 0.2


# ---------------------------------------------------------------------------
# Разбивка
# ---------------------------------------------------------------------------

def test_build_stereo_gives_curve_per_channel(tmp_path):
    pytest.importorskip("numpy")
    left = _wav(tmp_path / "l.wav", _signal(3.0))
    right = _wav(tmp_path / "r.wav", _signal(3.0, freq=180.0))
    curves = wf.build([("Левый", left), ("Правый", right)], [], {})
    assert [c["speaker"] for c in curves] == [0, 1]
    assert [c["label"] for c in curves] == ["Левый", "Правый"]
    assert all(len(c["audio_waveform"]) == 3 for c in curves)


def test_build_mono_with_speakers(tmp_path):
    """Монозапись с диаризацией: кривая на говорящего, вне реплик — нули."""
    pytest.importorskip("numpy")
    path = _wav(tmp_path / "m.wav", _signal(4.0))
    segments = [Segment(0.0, 2.0, "раз", speaker="Говорящий 1"),
                Segment(2.0, 4.0, "два", speaker="Говорящий 2")]
    curves = wf.build([("Вся запись", path)], segments, {})
    assert len(curves) == 2
    assert [c["speaker"] for c in curves] == [0, 1]
    assert [c["label"] for c in curves] == ["Говорящий 1", "Говорящий 2"]
    first, second = (c["audio_waveform"] for c in curves)
    assert first[0]["amplitude"] > 0.2 and first[3]["amplitude"] == 0.0
    assert second[0]["amplitude"] == 0.0 and second[3]["amplitude"] > 0.2


def test_build_mono_single_speaker_is_one_curve(tmp_path):
    path = _wav(tmp_path / "m.wav", _signal(2.0))
    segments = [Segment(0.0, 2.0, "раз", speaker="Говорящий 1")]
    curves = wf.build([("Вся запись", path)], segments, {})
    assert len(curves) == 1 and curves[0]["speaker"] == wf.ALL


def test_build_no_channels():
    assert wf.build([], [], {}) == []


@pytest.mark.parametrize("value,points", [(0.5, 4), (2.0, 1), (1000.0, 1), (0.0, 2)])
def test_interval_setting_and_clamping(tmp_path, value, points):
    """Шаг берётся из настроек и зажимается в разумные границы."""
    path = _wav(tmp_path / "m.wav", _signal(2.0))
    curves = wf.build([("Вся запись", path)], [], {"waveform_interval_s": value})
    assert len(curves[0]["audio_waveform"]) == points


# ---------------------------------------------------------------------------
# Совместимый формат
# ---------------------------------------------------------------------------

def test_to_phone_asr_shape():
    """Массив JSON-строк с ровно тремя полями — как в схеме phone_asr."""
    curves = [{"audio_waveform": [{"time": 0.0, "amplitude": 0.5}],
               "sample_rate": 16000, "speaker": 0, "label": "Канал 1"}]
    result = wf.to_phone_asr(curves)
    assert isinstance(result, list) and isinstance(result[0], str)
    parsed = json.loads(result[0])
    assert sorted(parsed) == ["audio_waveform", "sample_rate", "speaker"]
    assert "label" not in parsed


def test_to_phone_asr_empty():
    assert wf.to_phone_asr([]) == []


# ---------------------------------------------------------------------------
# Хранение и программный интерфейс
# ---------------------------------------------------------------------------

def test_migration_adds_column(tmp_path):
    """База прошлой версии дополняется колонкой, задания в ней не теряются."""
    from asrhub.db import Database

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE jobs (id TEXT PRIMARY KEY, created_at REAL, updated_at REAL,
                           status TEXT, params TEXT);
        PRAGMA user_version=3;
    """)
    conn.execute("INSERT INTO jobs VALUES ('job_old', 1.0, 1.0, 'completed', '{}')")
    conn.commit()
    conn.close()

    db = Database(path)
    assert db.get_job("job_old")["waveform"] == []
    db.update_job("job_old", waveform=[{"audio_waveform": [], "sample_rate": 16000,
                                        "speaker": "all"}])
    assert db.get_job("job_old")["waveform"][0]["sample_rate"] == 16000


def _completed_job(client, sample_wav: Path, settings: dict | None = None) -> str:
    payload = {"model": "demo-simulator", "engine": "demo", "vad_backend": "energy",
               "output_formats": ["json"]}
    payload.update(settings or {})
    with sample_wav.open("rb") as handle:
        response = client.post("/api/jobs",
                               files={"file": ("тест.wav", handle, "audio/wav")},
                               data={"settings": json.dumps(payload)})
    job_id = response.json()["id"]
    for _ in range(80):
        card = client.get(f"/api/jobs/{job_id}").json()
        if card["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert card["status"] == "completed", card.get("error_message")
    return job_id


def test_api_card_and_endpoint(client, sample_wav: Path):
    job_id = _completed_job(client, sample_wav)

    card = client.get(f"/api/jobs/{job_id}").json()
    assert card["waveform"] and card["waveform"][0]["audio_waveform"]
    assert card["waveforms"] == wf.to_phone_asr(card["waveform"])

    points = client.get(f"/api/jobs/{job_id}/waveform").json()
    assert points["interval_s"] == 1.0
    assert points["curves"] == card["waveform"]

    compat = client.get(f"/api/jobs/{job_id}/waveform?fmt=phone_asr").json()
    assert sorted(json.loads(compat["waveforms"][0])) == [
        "audio_waveform", "sample_rate", "speaker"]

    assert client.get(f"/api/jobs/{job_id}/waveform?fmt=нет").status_code == 422


def test_api_omits_waveform_where_it_is_dead_weight(client, sample_wav: Path):
    """В списке и при with_waveform=false огибающей быть не должно."""
    job_id = _completed_job(client, sample_wav)

    lean = client.get(f"/api/jobs/{job_id}?with_waveform=false").json()
    assert "waveform" not in lean and "waveforms" not in lean

    items = client.get("/api/jobs").json()["items"]
    assert items and all("waveform" not in item for item in items)


def test_waveform_can_be_switched_off(client, sample_wav: Path):
    job_id = _completed_job(client, sample_wav, {"waveform_enabled": False})
    card = client.get(f"/api/jobs/{job_id}").json()
    assert "waveform" not in card
    assert client.get(f"/api/jobs/{job_id}/waveform").json()["curves"] == []


def test_json_export_carries_waveform(client, sample_wav: Path):
    job_id = _completed_job(client, sample_wav)
    exported = json.loads(client.get(f"/api/jobs/{job_id}/download?fmt=json").text)
    assert exported["waveform"][0]["audio_waveform"]


# ---------------------------------------------------------------------------
# Документация и интерфейс
# ---------------------------------------------------------------------------

def test_docs_name_real_things(repo_root: Path):
    """В документации названы параметры и маршрут, которые есть в коде."""
    from asrhub import catalog

    keys = {p.key for p in catalog.PARAMS}
    assert {"waveform_enabled", "waveform_interval_s", "webhook_waveform"} <= keys

    api = (repo_root / "docs" / "08-api.md").read_text(encoding="utf-8")
    assert "GET /api/jobs/{id}/waveform" in api
    for key in ("waveform_interval_s", "waveform_enabled", "webhook_waveform"):
        assert key in api, key


def test_web_assets_wired(repo_root: Path):
    """Полоса нарисована тем же кодом, что и остальные графики."""
    charts = (repo_root / "server" / "asrhub" / "web" / "charts.js").read_text(encoding="utf-8")
    app = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert "function waveform(host, config)" in charts
    assert "spark, heat, waveform," in charts
    assert "Charts.waveform(host" in app
    # Обработчики на window снимаются при закрытии карточки, иначе они копятся.
    assert "asrhub:closed" in app and "removeEventListener('resize', redraw)" in app
    assert "asrhub:theme" in app
