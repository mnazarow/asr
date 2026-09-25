"""Заход 46: сервер под веб-интерфейсом — сохранение, скачивание, вход.

Интерфейс обещал больше, чем делал сервер. «Станция добавлена», «Скрипт
сохранён», «Сохранено» в очереди модели — всё это жило в памяти процесса и
пропадало при перезапуске. Создание ключа и установка языковой модели
писали в файл вообще всё — вместе с пробными значениями со страницы
настроек. Скачивание обычной ссылкой срывалось у вошедшего по ключу,
метрики при закрытом доступе не пускали вошедшего паролем, а форма смены
пароля говорила «пароль известен всем» тому, кому его выдал администратор.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

КЛЮЧ = "ah_" + "a" * 30
КЛЮЧ_ПОЛЬЗОВАТЕЛЯ = "ah_" + "u" * 30


def _приложение(data_dir: Path, monkeypatch: pytest.MonkeyPatch, *,
                config: dict[str, Any] | None = None, auth: bool = False,
                ключи: bool = False):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true" if auth else "false")
    if config is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    settings = load()
    if ключи:
        settings.api_keys[КЛЮЧ] = {"name": "админ", "role": "admin", "enabled": True}
        settings.api_keys[КЛЮЧ_ПОЛЬЗОВАТЕЛЯ] = {"name": "пользователь", "role": "user",
                                                 "enabled": True}
    return create_app(settings, start_queue=False)


def _файл(data_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((data_dir / "config.yaml").read_text(encoding="utf-8")) or {}


def _плоско(данные: dict[str, Any]) -> dict[str, Any]:
    """Значения файла без групп — чтобы проверять, не зная раскладки."""
    итог: dict[str, Any] = {}
    for ключ, значение in данные.items():
        if isinstance(значение, dict) and ключ not in ("api_keys",):
            итог.update(значение)
        else:
            итог[ключ] = значение
    return итог


# ---------------------------------------------------------------------------
# Разделы записывают своё в файл — и только своё
# ---------------------------------------------------------------------------


def test_параметры_раздела_записываются_только_с_persist(data_dir, monkeypatch):
    """Без `persist` — до перезапуска, с ним — в файл, и ничего чужого."""
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    with TestClient(app) as c:
        # Пробное значение со страницы настроек — не записывается никогда.
        assert c.put("/api/settings", json={"model_cache_size": 3}).status_code == 200
        ответ = c.put("/api/settings?persist=true", json={"backup_keep": 5}).json()
        assert ответ["persisted"] is True and ответ["applied"] == {"backup_keep": 5}
        файл = _плоско(_файл(data_dir))
        assert файл["backup_keep"] == 5
        assert "model_cache_size" not in файл, "пробное значение ушло в файл"
        assert файл["max_concurrent_jobs"] == 1, "прежнее содержимое потеряно"
        # Без persist файл не трогается и ответ о записи молчит.
        ответ = c.put("/api/settings", json={"backup_keep": 7}).json()
        assert "persisted" not in ответ
        assert _плоско(_файл(data_dir))["backup_keep"] == 5


def test_без_файла_конфигурации_говорится_прямо(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as c:
        ответ = c.put("/api/settings?persist=true", json={"backup_keep": 4}).json()
        assert ответ["persisted"] is False
        assert "без файла конфигурации" in ответ["reason"]
        assert "до перезапуска" not in ответ["reason"], "повтор фразы в подсказке"


def test_станции_переживают_перезапуск(data_dir, monkeypatch):
    """«Станция добавлена» — а после обновления станции не было."""
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    станция = {"name": "Филиал Юг", "source": "cdr_csv", "cdr_file": str(data_dir / "Master.csv"),
               "recordings_dir": str(data_dir)}
    with TestClient(app) as c:
        c.put("/api/settings", json={"model_cache_size": 3})     # проба — не в файл
        ответ = c.post("/api/telephony/stations", json=станция)
        assert ответ.status_code == 200, ответ.text
        итог = ответ.json()
        assert итог["persisted"] is True
        ид = итог["station"]["id"]
        сохранено = _плоско(_файл(data_dir))["telephony_stations"]
        assert [с["id"] for с in сохранено] == [ид]
        assert "model_cache_size" not in _плоско(_файл(data_dir))
        ответ = c.post(f"/api/telephony/stations/{ид}/enabled?enabled=false").json()
        assert ответ["persisted"] is True
        assert _плоско(_файл(data_dir))["telephony_stations"][0]["enabled"] is False
    # Перезапуск: станция на месте.
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as c:
        станции = c.get("/api/telephony/stations").json()["items"]
        assert [с["id"] for с in станции] == [ид]
        ответ = c.delete(f"/api/telephony/stations/{ид}").json()
        assert ответ["persisted"] is True
    assert _плоско(_файл(data_dir))["telephony_stations"] == []


def test_пауза_очереди_модели_переживает_перезапуск(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    with TestClient(app) as c:
        ответ = c.post("/api/llm/queue/pause", json={"paused": True})
        if ответ.status_code != 200:
            pytest.skip(f"очередь модели недоступна: {ответ.text[:200]}")
        assert ответ.json()["persisted"] is True
    assert _плоско(_файл(data_dir))["llm_queue_paused"] is True


def test_очередь_модели_называет_ширину_корзины(data_dir, monkeypatch):
    """Ширину корзины интерфейс считал от часов браузера — сервер её не называл."""
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    with TestClient(app) as c:
        ответ = c.get("/api/llm/queue?hours=6&limit=1")
        if ответ.status_code != 200:
            pytest.skip(f"очередь модели недоступна: {ответ.text[:200]}")
        ряд = ответ.json()["series"]
    assert ряд["buckets"] > 0
    assert ряд["bucket_seconds"] == pytest.approx(6 * 3600 / ряд["buckets"])


def test_ключ_доступа_пишет_в_файл_только_ключи(data_dir, monkeypatch):
    """Создание ключа уносило в файл и пробные значения со страницы настроек."""
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    with TestClient(app) as c:
        c.put("/api/settings", json={"model_cache_size": 3})
        ответ = c.post("/api/keys", json={"name": "интеграция", "role": "user"}).json()
        assert ответ["persisted"] is True
    файл = _файл(data_dir)
    assert ответ["key"] in файл["api_keys"]
    плоско = _плоско(файл)
    assert "model_cache_size" not in плоско, "пробное значение ушло в файл вместе с ключом"
    # Подобранное под оборудование тоже не записывается: на другой машине
    # его подберут заново.
    assert "compute_type" not in плоско and "cpu_threads" not in плоско


def test_токен_hugging_face_пишет_в_файл_только_токен(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch, config={"queue": {"max_concurrent_jobs": 1}})
    with TestClient(app) as c:
        c.put("/api/settings", json={"model_cache_size": 3})
        assert c.put("/api/settings/hf-token",
                     json={"token": "hf_" + "t" * 20}).status_code == 200
    файл = _файл(data_dir)
    assert файл["hf_token"] == "hf_" + "t" * 20
    assert "model_cache_size" not in _плоско(файл)


def test_установка_языковой_модели_пишет_три_параметра(data_dir, monkeypatch):
    """Установщик сохранял в файл всё — вместе с тем, что применили на пробу."""
    from asrhub.config import load
    from asrhub.llm import provision

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text("queue:\n  max_concurrent_jobs: 1\n",
                                          encoding="utf-8")
    settings = load()
    settings.set("model_cache_size", 3, source="api")               # проба
    установщик = provision.Установщик(settings)
    установщик._настройка("qwen3.5:9b", "http://127.0.0.1:11434")
    плоско = _плоско(_файл(data_dir))
    assert плоско["llm_model"] == "qwen3.5:9b" and плоско["llm_backend"] == "ollama"
    assert "model_cache_size" not in плоско
    assert плоско["max_concurrent_jobs"] == 1


# ---------------------------------------------------------------------------
# Скачивание по одноразовому билету
# ---------------------------------------------------------------------------


def test_скачивание_по_билету_работает_один_раз(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch, auth=True, ключи=True)
    заголовок = {"X-API-Key": КЛЮЧ}
    with TestClient(app) as c:
        копия = c.post("/api/backup?kind=settings", headers=заголовок,
                       json={"comment": "проверка"}).json()
        адрес = f"/api/backup/{копия['name']}/file"
        assert c.get(адрес).status_code == 401
        билет = c.post("/api/auth/ticket", headers=заголовок).json()["ticket"]
        ответ = c.get(f"{адрес}?ticket={билет}")
        assert ответ.status_code == 200 and ответ.content
        повтор = c.get(f"{адрес}?ticket={билет}")
        assert повтор.status_code == 401
        assert "устарела или уже использована" in повтор.json()["message"]
        # Билет — только на чтение.
        билет = c.post("/api/auth/ticket", headers=заголовок).json()["ticket"]
        assert c.post(f"/api/backup?kind=settings&ticket={билет}",
                      json={}).status_code == 401
        # Билет несёт права своего ключа, а не больше.
        билет = c.post("/api/auth/ticket",
                       headers={"X-API-Key": КЛЮЧ_ПОЛЬЗОВАТЕЛЯ}).json()["ticket"]
        assert c.get(f"{адрес}?ticket={билет}").status_code == 403


# ---------------------------------------------------------------------------
# Метрики при закрытом доступе — тем же разбором, что везде
# ---------------------------------------------------------------------------


def test_закрытые_метрики_пускают_вошедшего_паролем(data_dir, monkeypatch):
    """Своя проверка знала только ключи: сессия получала 401 в «Мониторинге»."""
    app = _приложение(data_dir, monkeypatch, auth=True, ключи=True,
                      config={"monitoring": {"monitoring_public": False}})
    состояние = app.state.hub
    with TestClient(app) as c:
        состояние.accounts.create("дежурный", "Дежурный-1-пароль", role="admin")
        assert c.get("/api/monitoring/health").status_code == 401
        assert c.post("/api/auth/login", json={"username": "дежурный",
                                               "password": "Дежурный-1-пароль"}).status_code == 200
        for путь in ("/api/monitoring/health", "/api/monitoring/metrics",
                     "/api/monitoring/config/zabbix", "/api/metrics"):
            assert c.get(путь).status_code == 200, путь


def test_без_входа_метрики_не_требуют_ключа(data_dir, monkeypatch):
    """С выключенной аутентификацией сервер требовал ключ, которого у него нет."""
    app = _приложение(data_dir, monkeypatch,
                      config={"monitoring": {"monitoring_public": False}})
    with TestClient(app) as c:
        assert c.get("/api/monitoring/metrics").status_code == 200
        assert c.get("/api/metrics").status_code == 200


# ---------------------------------------------------------------------------
# Кто я: причина смены пароля и предупреждение без входа
# ---------------------------------------------------------------------------


def test_причина_обязательной_смены_пароля(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch, auth=True)
    состояние = app.state.hub
    with TestClient(app) as c:
        состояние.accounts.create("новичок", "Временный-1-пароль", must_change_password=True)
        вход = c.post("/api/auth/login", json={"username": "новичок",
                                               "password": "Временный-1-пароль"}).json()
        assert вход["password_reason"] == "assigned"
        assert c.get("/api/auth/me").json()["password_reason"] == "assigned"
    with TestClient(app) as c:
        вход = c.post("/api/auth/login", json={"username": "admin",
                                               "password": "admin123"}).json()
        assert вход["must_change_password"] is True
        assert вход["password_reason"] == "default"
        assert c.get("/api/auth/me").json()["password_reason"] == "default"


def test_без_входа_нет_предупреждения_о_пароле(data_dir, monkeypatch):
    """Пароль по умолчанию ничего не защищает, когда вход выключен."""
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as c:
        я = c.get("/api/auth/me").json()
        assert я["auth_enabled"] is False
        assert "default_password_in_use" not in я


# ---------------------------------------------------------------------------
# Данные для разделов
# ---------------------------------------------------------------------------


def _задание(db, ид: str, *, owner: str, model: str, текст: str = "") -> None:
    db.create_job({"id": ид, "filename": f"{ид}.wav", "owner": owner, "engine": "demo",
                   "model": model, "language": "ru", "source": "web",
                   "media_duration_s": 10.0})
    db.update_job(ид, status="completed", text=текст or f"текст {ид}",
                  finished_at=time.time())


def test_модели_архива_для_отбора(data_dir, monkeypatch):
    """Отбор предлагал весь каталог — семьдесят моделей при двух в архиве."""
    app = _приложение(data_dir, monkeypatch, auth=True, ключи=True)
    db = app.state.hub.db
    for n in range(3):
        _задание(db, f"a{n}", owner="админ", model="demo-simulator")
    _задание(db, "u0", owner="пользователь", model="gigaam-v3-e2e-rnnt")
    with TestClient(app) as c:
        все = c.get("/api/jobs/models", headers={"X-API-Key": КЛЮЧ}).json()["items"]
        assert [(м["model"], м["jobs"]) for м in все] == [
            ("demo-simulator", 3), ("gigaam-v3-e2e-rnnt", 1)]
        assert все[1]["name"].startswith("GigaAM")
        свои = c.get("/api/jobs/models",
                     headers={"X-API-Key": КЛЮЧ_ПОЛЬЗОВАТЕЛЯ}).json()["items"]
        assert [м["model"] for м in свои] == ["gigaam-v3-e2e-rnnt"], "чужие модели видны"


def test_облегчённый_список_несёт_начало_расшифровки(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch)
    _задание(app.state.hub.db, "t1", owner="x", model="demo-simulator", текст="слово " * 100)
    with TestClient(app) as c:
        строка = c.get("/api/jobs?light=true&status=completed").json()["items"][0]
    assert "text" not in строка
    assert строка["text_preview"] == ("слово " * 100)[:200]


def test_карточка_задания_знает_о_проверке_качества(data_dir, monkeypatch):
    app = _приложение(data_dir, monkeypatch)
    db = app.state.hub.db
    _задание(db, "q1", owner="x", model="demo-simulator")
    with TestClient(app) as c:
        assert "qa" not in c.get("/api/jobs/q1").json()
        ид = db.qa_assign("q1", assigned_to="", assigned_by="проверка",
                          due_at=time.time() + 3600, agent="Анна", auto_score=80.0,
                          reason="проверка")
        проверка = c.get("/api/jobs/q1").json()["qa"]
        assert (проверка["id"], проверка["status"], проверка["auto_score"]) == (ид, "pending", 80.0)
        assert c.put(f"/api/qa/{ид}", json={"score": 75}).status_code == 200
        проверка = c.get("/api/jobs/q1").json()["qa"]
        assert (проверка["status"], проверка["score"], проверка["agree"]) == ("done", 75.0, True)


def test_тревога_несёт_знак_и_метки(data_dir, monkeypatch):
    """Таблица писала «> 1» для включительного порога и не различала модели."""
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as c:
        # Снимок уже собран и лежит в кеше: новые правила считаются сразу по
        # нему, а не после истечения кеша — раньше до того «Тревог нет».
        c.get("/api/monitoring/alerts")
        # Источник «очередь» отвечает (1), порог «не больше 1» — нарушен.
        c.put("/api/monitoring/alerts/rules", json=[
            {"metric": "asrhub_collector_source_up", "direction": "below", "threshold": 1,
             "severity": "critical", "inclusive": True, "labels": {"source": "queue"}}])
        тревоги = c.get("/api/monitoring/alerts").json()["alerts"]
    assert len(тревоги) == 1, тревоги
    assert тревоги[0]["inclusive"] is True
    assert тревоги[0]["labels"] == {"source": "queue"}
    assert тревоги[0]["state"] != "ok"


def test_справочник_метрик_показывает_действующий_порог(data_dir, monkeypatch):
    """Порог места — от disk_min_free_gb, а справочник показывал каталожный."""
    app = _приложение(data_dir, monkeypatch, config={"runtime": {"disk_min_free_gb": 50}})
    with TestClient(app) as c:
        метрика = c.get("/api/monitoring/catalog/asrhub_disk_free_bytes").json()
        снимок = c.get("/api/monitoring/metrics.json").json()
    порог = метрика["threshold"]
    assert порог["critical"] == 50 * 1024 ** 3 and порог["warning"] == 100 * 1024 ** 3, порог
    из_снимка = next(м for м in снимок["metrics"] if м["name"] == "asrhub_disk_free_bytes")
    assert из_снимка["threshold"] == порог


# ---------------------------------------------------------------------------
# Находки сверки интерфейса с сервером
# ---------------------------------------------------------------------------


def test_подпись_карты_по_часам_не_округляется_в_ноль(tmp_path):
    """Клетки по 0,23, а подпись «от 0 до 0, среднее 0»."""
    from asrhub.db import Database
    from asrhub.trends import Trends

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n in range(5):
        db.create_job({"id": f"h{n}", "filename": "x.wav", "owner": "анна",
                       "model": "demo", "engine": "demo", "media_duration_s": 60.0})
        когда = сейчас - n * 7 * 86400 - 3600
        db.update_job(f"h{n}", status="completed", finished_at=когда + 10, text="да")
        db.execute("UPDATE jobs SET created_at=?, queued_at=?, finished_at=? WHERE id=?",
                   (когда, когда, когда + 10, f"h{n}"))
    карта = Trends(db).heatmap("jobs", "quarter", is_admin=True)
    клетки = [з for строка in карта["grid"] for з in строка if з is not None]
    assert max(клетки) < 1, "проверка задумана на долях единицы"
    assert карта["max"] == max(клетки) and карта["max"] > 0, карта["max"]
    assert карта["avg"] > 0


def test_откат_повтора_возвращает_прежние_параметры(client, sample_wav):
    """Текст и модель — прежние, а в параметрах — модель, которой не считали."""
    with sample_wav.open("rb") as fh:
        ид = client.post("/api/jobs", files={"file": ("a.wav", fh, "audio/wav")}).json()["id"]
    for _ in range(300):
        if client.get(f"/api/jobs/{ид}").json()["status"] == "completed":
            break
        time.sleep(0.1)
    было = client.get(f"/api/jobs/{ид}").json()
    assert было["status"] == "completed"
    # Модель на движке без установленного пакета — повтор не удастся.
    ответ = client.post("/api/jobs/rescan", json={"ids": [ид], "overrides": {
        "model": "gigaam-v3-e2e-rnnt", "engine": "gigaam"}})
    assert ответ.status_code == 200, ответ.text
    for _ in range(300):
        события = [с["kind"] for с in client.get(f"/api/jobs/{ид}").json()["events"]]
        if "rescan_reverted" in события:
            break
        time.sleep(0.1)
    стало = client.get(f"/api/jobs/{ид}").json()
    if "rescan_reverted" not in [с["kind"] for с in стало["events"]]:
        pytest.skip("повтор на этой машине удался — откатывать нечего")
    assert стало["status"] == "completed"
    assert стало["params"]["model"] == было["params"]["model"], стало["params"]["model"]
    assert стало["params"].get("engine") == было["params"].get("engine")
    assert стало["text"] == было["text"]


def test_подсказки_о_повторе_разбора_ведут_на_настоящий_адрес():
    """«POST /api/llm/queue/retry-failed» отвечал 405 — такого маршрута нет."""
    import asrhub.api.routes_system as система
    import asrhub.selfcheck as самопроверка

    for модуль in (самопроверка, система):
        текст = Path(модуль.__file__).read_text(encoding="utf-8")
        assert "llm/queue/retry-failed" not in текст, модуль.__name__
    from asrhub.api.routes_llm import router

    пути = {(р.path, tuple(sorted(р.methods))) for р in router.routes}
    assert ("/api/llm/queue/add", ("POST",)) in пути


def test_название_темы_не_принимается_из_служебного_ответа():
    """Модель, ответившая «{}», давала темы с названием «{}»."""
    from asrhub import topics

    тема = topics.Тема(stems=["срок", "постав"], jobs=["a"], title="срок, постав")
    прежнее = тема.title
    topics.назвать([тема], lambda _: "{}", {"a": "текст"})
    assert тема.title == прежнее
    topics.назвать([тема], lambda _: "Сроки поставки", {"a": "текст"})
    assert тема.title == "Сроки поставки"
