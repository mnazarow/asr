"""Сквозные проверки программного интерфейса."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient


@pytest.fixture()
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "2")
    settings = load()
    app = create_app(settings, start_queue=True)
    with TestClient(app) as test_client:
        yield test_client


def test_health(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_catalog_endpoint(client):
    data = client.get("/api/catalog").json()
    assert len(data["models"]) > 50
    assert len(data["params"]) > 90
    assert data["summary"]["total"] == len(data["models"])


def test_models_filtering(client):
    russian = client.get("/api/models?language=ru").json()
    assert russian["total"] > 20
    streaming = client.get("/api/models?streaming=true").json()
    assert all(m["streaming"] for m in streaming["items"])


def test_model_card(client):
    data = client.get("/api/models/gigaam-v3-rnnt").json()
    assert data["family"] == "GigaAM"
    assert data["license"] == "MIT"
    assert data["benchmarks"]


def test_unknown_model_gives_helpful_error(client):
    response = client.get("/api/models/gigaam-v9")
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["code"] == "model_not_found"
    assert detail["hint"]


def test_params_have_examples(client):
    data = client.get("/api/params").json()
    assert data["stats"]["examples_total"] > 200
    assert all(item["description"] for item in data["items"])


def test_presets(client):
    data = client.get("/api/presets").json()
    ids = {p["id"] for p in data["items"]}
    assert {"ru-accuracy", "callcenter", "subtitles"} <= ids


def test_settings_update_and_validation(client):
    response = client.put("/api/settings", json={"beam_size": 7})
    assert response.status_code == 200
    assert response.json()["applied"]["beam_size"] == 7

    bad = client.put("/api/settings", json={"beam_size": 999})
    assert bad.status_code == 400
    assert "максимум" in bad.json()["detail"]["message"]


def test_queue_status(client):
    data = client.get("/api/queue").json()
    assert "workers" in data and "counts" in data


def test_full_job_cycle(client, sample_wav: Path):
    with sample_wav.open("rb") as handle:
        response = client.post("/api/jobs", files={"file": ("тест.wav", handle, "audio/wav")},
                               data={"settings": json.dumps({
                                   "model": "demo-simulator", "engine": "demo",
                                   "vad_backend": "energy",
                                   "output_formats": ["txt", "json", "srt"]})})
    assert response.status_code == 200, response.text
    job = response.json()
    job_id = job["id"]

    for _ in range(80):
        current = client.get(f"/api/jobs/{job_id}").json()
        if current["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)

    assert current["status"] == "completed", current.get("error_message")
    assert current["words_count"] > 0
    assert current["rtf"] is not None

    segments = client.get(f"/api/jobs/{job_id}/segments").json()["items"]
    assert segments and segments[0]["text"]

    for fmt in ("txt", "srt", "json", "vtt", "csv"):
        download = client.get(f"/api/jobs/{job_id}/download?fmt={fmt}")
        assert download.status_code == 200, fmt
        assert len(download.content) > 10

    analytics = client.get("/api/analytics/overview?period=day").json()
    assert analytics["jobs"]["completed"] >= 1


def test_reference_text_computes_wer(client, sample_wav: Path):
    with sample_wav.open("rb") as handle:
        job = client.post("/api/jobs", files={"file": ("эталон.wav", handle, "audio/wav")},
                          data={"settings": json.dumps({"model": "demo-simulator",
                                                        "engine": "demo",
                                                        "vad_backend": "energy"})}).json()
    for _ in range(80):
        current = client.get(f"/api/jobs/{job['id']}").json()
        if current["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    result = client.post(f"/api/jobs/{job['id']}/reference",
                         json={"text": "Совершенно другой эталонный текст"}).json()
    # WER не ограничен сверху: при большом числе вставок он превышает единицу.
    assert result["wer"] >= 0.0
    assert result["words"]["insertions"] >= 0
    assert "diff" in result


def test_cancel_job(client, sample_wav: Path):
    client.post("/api/queue/pause")
    try:
        with sample_wav.open("rb") as handle:
            job = client.post("/api/jobs", files={"file": ("отмена.wav", handle, "audio/wav")},
                              data={"settings": json.dumps({"model": "demo-simulator",
                                                            "engine": "demo"})}).json()
        cancelled = client.post(f"/api/jobs/{job['id']}/cancel").json()
        assert cancelled["status"] == "cancelled"
    finally:
        client.post("/api/queue/resume")


def test_priority_change(client, sample_wav: Path):
    client.post("/api/queue/pause")
    try:
        with sample_wav.open("rb") as handle:
            job = client.post("/api/jobs", files={"file": ("приоритет.wav", handle, "audio/wav")},
                              data={"settings": json.dumps({"model": "demo-simulator"})}).json()
        updated = client.post(f"/api/jobs/{job['id']}/top").json()
        assert updated["priority"] == 100
    finally:
        client.post("/api/queue/resume")


def test_unsupported_format_rejected(client, tmp_path: Path):
    bad = tmp_path / "документ.pdf"
    bad.write_bytes(b"%PDF-1.4 fake")
    with bad.open("rb") as handle:
        response = client.post("/api/jobs", files={"file": ("документ.pdf", handle, "application/pdf")})
    assert response.status_code == 415
    assert response.json()["detail"]["code"] == "unsupported_format"


def test_metrics_endpoint(client):
    response = client.get("/api/metrics")
    assert response.status_code == 200
    assert "asrhub_jobs_total" in response.text


def test_api_reference_is_offline(client):
    response = client.get("/api/reference")
    assert response.status_code == 200
    assert "справочник" in response.text.lower()
    assert "cdn" not in response.text.lower(), "страница не должна тянуть внешние ресурсы"


def test_engines_listing(client):
    data = client.get("/api/engines").json()
    demo = next(item for item in data["items"] if item["id"] == "demo")
    assert demo["available"] is True
    for item in data["items"]:
        if not item["available"]:
            assert item["reason"], f"{item['id']}: нет объяснения недоступности"


def test_web_interface_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "ASR Hub" in response.text
    for asset in ("/static/app.js", "/static/charts.js", "/static/styles.css"):
        assert client.get(asset).status_code == 200
