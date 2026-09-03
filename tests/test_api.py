"""Сквозные проверки программного интерфейса."""
from __future__ import annotations

import json
import time
from pathlib import Path

from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


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


# ---------------------------------------------------------------------------
# Одноразовые билеты вместо ключа в адресе
# ---------------------------------------------------------------------------

def _ws_rejected(test_client, path: str) -> bool:
    """Правда, если сервер закрыл соединение вместо приветствия."""
    try:
        with test_client.websocket_connect(path) as ws:
            ws.receive_text()
    except WebSocketDisconnect:
        return True
    except Exception:                                       # noqa: BLE001
        # Отказ на этапе рукопожатия приходит как WebSocketDisconnect,
        # но версия starlette может обернуть его иначе.
        return True
    return False


def _auth_client(monkeypatch):
    """Клиент с включённой аутентификацией и одним известным ключом."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.api_keys["ah_ticket_key"] = {"name": "тест", "role": "admin", "enabled": True}
    return create_app(settings, start_queue=False)


def test_ticket_requires_key(data_dir, monkeypatch):
    with TestClient(_auth_client(monkeypatch)) as c:
        assert c.post("/api/auth/ticket").status_code == 401


def test_ticket_is_single_use(data_dir, monkeypatch):
    """Билет открывает WebSocket ровно один раз и живёт минуту."""
    with TestClient(_auth_client(monkeypatch)) as c:
        issued = c.post("/api/auth/ticket", headers={"X-API-Key": "ah_ticket_key"}).json()
        assert issued["ticket"] and issued["expires_in"] > 0

        with c.websocket_connect(f"/ws?ticket={issued['ticket']}") as ws:
            assert json.loads(ws.receive_text())["type"] == "hello"

        # Тот же билет второй раз не проходит.
        assert _ws_rejected(c, f"/ws?ticket={issued['ticket']}")


def test_ticket_forged_is_rejected(data_dir, monkeypatch):
    with TestClient(_auth_client(monkeypatch)) as c:
        assert _ws_rejected(c, "/ws?ticket=подделка")
        assert _ws_rejected(c, "/ws")


def test_websocket_still_accepts_api_key(data_dir, monkeypatch):
    """Сторонние клиенты продолжают подключаться по ключу."""
    with TestClient(_auth_client(monkeypatch)) as c, \
            c.websocket_connect("/ws?api_key=ah_ticket_key") as ws:
        assert json.loads(ws.receive_text())["type"] == "hello"


def test_ticket_store_expires(monkeypatch):
    """Просроченный билет не принимается."""
    from asrhub.api.deps import TicketStore

    store = TicketStore(ttl_s=0.01)
    ticket, _ = store.issue("ключ")
    time.sleep(0.05)
    assert store.redeem(ticket) == ""


def test_metrics_endpoint_matches_monitoring(client):
    """Оба адреса Prometheus отдают одни и те же имена метрик.

    Раньше /api/metrics обслуживался отдельным ручным экспортом на полтора
    десятка метрик со старыми именами, и правила тревог, собранные по
    каталогу, на нём не срабатывали.
    """
    def names(text: str) -> set[str]:
        return {line.split("{")[0].split(" ")[0]
                for line in text.splitlines() if line and not line.startswith("#")}

    legacy = names(client.get("/api/metrics").text)
    modern = names(client.get("/api/monitoring/metrics").text)
    assert legacy == modern, "адреса Prometheus разошлись по именам метрик"
    assert len(legacy) > 40, "экспорт снова сузился до старого ручного набора"


# ---------------------------------------------------------------------------
# Документация не должна расходиться с кодом
# ---------------------------------------------------------------------------

def _emitted_event_names() -> set[str]:
    """Имена событий, которые сервер действительно шлёт в WebSocket."""
    import re

    root = Path(__file__).resolve().parent.parent / "server" / "asrhub"
    names: set[str] = set()
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        names |= set(re.findall(r'_emit\(\s*"([a-z]+\.[a-z]+)"', text))
    return names


def test_documented_events_exist_in_code():
    """Каждое событие из главы про API сервер обязан уметь отправлять.

    В главе значились job.created, queue.changed, system.sample и
    server.ready — ни одного из них сервер не шлёт. Клиент, написанный по
    такой документации, ждал бы события, которых нет.
    """
    import re

    chapter = (Path(__file__).resolve().parent.parent / "docs" / "08-api.md")
    text = chapter.read_text(encoding="utf-8")
    # Берём имена из таблицы событий: «| `job.progress` | …»
    documented = set(re.findall(r"^\| `([a-z]+\.[a-z]+)` \|", text, flags=re.M))
    assert documented, "таблица событий в главе не найдена"

    emitted = _emitted_event_names()
    unknown = documented - emitted
    assert not unknown, f"документация обещает несуществующие события: {sorted(unknown)}"


def test_all_emitted_events_are_documented():
    """И наоборот: новое событие не должно остаться незадокументированным."""
    import re

    chapter = (Path(__file__).resolve().parent.parent / "docs" / "08-api.md")
    text = chapter.read_text(encoding="utf-8")
    documented = set(re.findall(r"^\| `([a-z]+\.[a-z]+)` \|", text, flags=re.M))
    missing = _emitted_event_names() - documented
    assert not missing, f"события есть в коде, но не описаны: {sorted(missing)}"


def test_light_listing_drops_heavy_fields(client, sample_wav: Path):
    """light=true не должен тащить текст и сегменты.

    Параметр был реализован в базе, но наружу не выведен — а документация
    его обещала.
    """
    with sample_wav.open("rb") as fh:
        client.post("/api/jobs", files={"file": ("проба.wav", fh, "audio/wav")})
    for _ in range(60):
        items = client.get("/api/jobs?limit=1").json()["items"]
        if items and items[0]["status"] in ("completed", "failed"):
            break
        time.sleep(0.5)

    full = client.get("/api/jobs?limit=1").json()["items"]
    light = client.get("/api/jobs?limit=1&light=true").json()["items"]
    assert full and light
    assert "text" in full[0], "полный список обязан содержать расшифровку"
    assert "text" not in light[0], "облегчённый список не должен нести текст"
    assert light[0]["id"] == full[0]["id"]


def test_form_fields_override_settings(client, sample_wav: Path):
    """Параметр, переданный полем формы, должен применяться.

    Раньше принималось только поле settings с JSON внутри, а `-F model=…`
    молча пропадало: задание уходило на модель по умолчанию.
    """
    with sample_wav.open("rb") as fh:
        response = client.post("/api/jobs",
                               files={"file": ("проба.wav", fh, "audio/wav")},
                               data={"language": "en"})
    assert response.status_code == 200, response.text
    assert response.json()["params"]["language"] == "en"


def test_unknown_form_field_is_reported(client, sample_wav: Path):
    """Опечатка в имени параметра не должна проходить молча."""
    with sample_wav.open("rb") as fh:
        response = client.post("/api/jobs",
                               files={"file": ("проба.wav", fh, "audio/wav")},
                               data={"languagee": "en"})
    assert response.status_code == 400, response.text
    assert "languagee" in response.json()["detail"]["message"]


def test_form_field_examples_name_real_parameters():
    """Каждый параметр из примеров `-F ключ=…` должен существовать в каталоге.

    В справочнике стоял пример `-F 'diarization=true'`, а такого параметра
    нет — есть diarization_enabled. Пользователь, скопировавший строку из
    документации, получал 400 «Неизвестные поля формы».
    """
    import re

    from asrhub import catalog

    root = Path(__file__).resolve().parent.parent
    reserved = {"file", "files", "settings", "priority", "group_id", "tags",
                "reference_text", "webhook_url"}
    pattern = re.compile(r"-F ['\"]?([a-z_0-9]+)=")

    bad: list[str] = []
    # Глава ревизии намеренно цитирует то, что было неверным, — это её
    # содержание, а не инструкция к применению.
    skip = {"18-review.md"}
    for path in [*(root / "docs").glob("*.md"), *(root / "docs").glob("*.py"),
                 *(root / "examples").glob("*.py")]:
        if path.name in skip:
            continue
        for key in pattern.findall(path.read_text(encoding="utf-8")):
            if key in reserved or key in catalog.PARAMS_BY_KEY:
                continue
            bad.append(f"{path.name}: {key}")
    assert not bad, "в примерах названы несуществующие параметры: " + "; ".join(sorted(set(bad)))


def test_form_fields_accept_declared_types(client, sample_wav: Path):
    """Поле формы приходит строкой, а проверка ждёт объявленный тип.

    `-F diarization_enabled=true` отвергалось с «ожидается да/нет», хотя
    именно так этот способ описан в справочнике.
    """
    for value in ("true", "false", "да", "нет", "1", "0"):
        with sample_wav.open("rb") as fh:
            response = client.post("/api/jobs",
                                   files={"file": ("проба.wav", fh, "audio/wav")},
                                   data={"diarization_enabled": value})
        assert response.status_code == 200, f"{value}: {response.text[:160]}"

    with sample_wav.open("rb") as fh:
        response = client.post("/api/jobs", files={"file": ("проба.wav", fh, "audio/wav")},
                               data={"beam_size": "8"})
    assert response.status_code == 200
    assert response.json()["params"]["beam_size"] == 8, "число осталось строкой"

    with sample_wav.open("rb") as fh:
        response = client.post("/api/jobs", files={"file": ("проба.wav", fh, "audio/wav")},
                               data={"diarization_enabled": "мусор"})
    assert response.status_code == 400
    assert "diarization_enabled" in response.json()["detail"]["message"]


def test_health_answers_on_the_conventional_path(client):
    """Проверка живёт и по /health, а не только под префиксом /api.

    Балансировщики, docker HEALTHCHECK, uptime-мониторы и kubelet стучатся в
    /health по умолчанию. Настраивать каждому свой путь — лишний шаг, на
    котором проверку чаще всего просто не заводят.
    """
    short = client.get("/health")
    full = client.get("/api/health")
    assert short.status_code == 200
    assert short.json()["status"] == full.json()["status"] == "ok"
    assert short.json()["version"] == full.json()["version"]
    # Ключ не спрашивается: проверка, требующая ключа, однажды покажет
    # «сервер лёг» из-за отозванного ключа, и разбираться будут не с ключом.
    assert "checks" in short.json()


def test_health_says_degraded_when_the_database_is_gone(data_dir, monkeypatch):
    """Проверка обязана отвечать отказом, когда работать нельзя.

    Иначе балансировщик держит в строю сервер, который принимает запросы и
    падает на каждом: 200 при мёртвой базе — это не «здоров», а «врёт».
    """
    from fastapi.testclient import TestClient

    from asrhub.api.app import create_app

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    app = create_app(start_queue=False)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200

        state = app.state.hub
        working = state.db

        class Broken:
            def query_one(self, *args, **kwargs):
                raise RuntimeError("файл базы недоступен")

            def __getattr__(self, name):
                return getattr(working, name)

        state.db = Broken()
        try:
            broken = client.get("/health")
            assert broken.status_code == 503, "сервер объявил себя здоровым без базы"
            assert broken.json()["status"] == "degraded"
            assert "недоступна" in broken.json()["checks"]["database"]
            # Тот же ответ и под префиксом: два адреса не должны расходиться.
            assert client.get("/api/health").status_code == 503
        finally:
            state.db = working
        assert client.get("/health").status_code == 200


def test_paused_queue_is_not_an_unhealthy_server(client):
    """Пауза очереди — это «занят по решению человека», а не поломка.

    Снимать такой сервер с раздачи нельзя: он отвечает, принимает задания и
    отдаёт готовые результаты, просто не берёт новые в работу.
    """
    client.post("/api/queue/pause")
    try:
        paused = client.get("/health")
        assert paused.status_code == 200
        assert paused.json()["status"] == "ok"
        assert paused.json()["queue_paused"] is True
        assert paused.json()["checks"]["queue"] == "приостановлена"
    finally:
        client.post("/api/queue/resume")
