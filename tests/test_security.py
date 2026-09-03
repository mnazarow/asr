"""Проверки разграничения доступа и утечек.

Каждый тест здесь закрывает дефект, найденный ревизией и воспроизведённый
запросом: не «так задумано», а «так было, и вот запрос, который это делал».
"""
from __future__ import annotations

import io
import math
import struct
import wave
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i / 8)))
                               for i in range(16000)))
    return buf.getvalue()


@pytest.fixture()
def keys_app(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Приложение с включённой аутентификацией и тремя ролями."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    settings.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    settings.api_keys["ah_bob"] = {"name": "Боб", "role": "user", "enabled": True}
    settings.api_keys["ah_ro"] = {"name": "чтение", "role": "readonly", "enabled": True}
    return settings


def test_job_list_is_scoped_to_owner(keys_app):
    """Список заданий не должен показывать чужие.

    Карточка задания была закрыта require_owner, а список — нет: GET
    /api/jobs отдавал чужие задания вместе с именем файла, путём на диске и
    полной расшифровкой. Проверка стояла на одном пути и отсутствовала на
    соседнем.
    """
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        created = c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
                         files={"file": ("секрет.wav", _wav(), "audio/wav")})
        assert created.status_code == 200, created.text
        job_id = created.json()["id"]

        mine = c.get("/api/jobs?limit=50", headers={"X-API-Key": "ah_alice"}).json()
        assert [j["id"] for j in mine["items"]] == [job_id]

        theirs = c.get("/api/jobs?limit=50", headers={"X-API-Key": "ah_bob"}).json()
        assert theirs["items"] == [], "чужое задание видно в списке"
        assert theirs["total"] == 0, "чужое задание учтено в счётчике"

        # Прямое обращение и раньше было закрыто — проверяем, что осталось.
        assert c.get(f"/api/jobs/{job_id}", headers={"X-API-Key": "ah_bob"}).status_code == 403

        # Администратор видит всё.
        everything = c.get("/api/jobs?limit=50", headers={"X-API-Key": "ah_admin"}).json()
        assert [j["id"] for j in everything["items"]] == [job_id]


def test_owner_filter_cannot_be_widened_by_query(keys_app):
    """Параметр owner не должен открывать чужие задания."""
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
               files={"file": ("а.wav", _wav(), "audio/wav")})
        peek = c.get("/api/jobs?owner=Алиса&limit=50",
                     headers={"X-API-Key": "ah_bob"}).json()
        assert peek["items"] == [], "чужой владелец подставился в фильтр"


def test_analytics_is_scoped_to_owner(keys_app):
    """Аналитика ключа без прав администратора считается по его заданиям."""
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
               files={"file": ("а.wav", _wav(), "audio/wav")})
        bob = c.get("/api/analytics?period=month", headers={"X-API-Key": "ah_bob"}).json()
        assert bob["overview"]["jobs"]["total"] == 0
        assert bob["owners"] == [], "перечень владельцев виден чужому ключу"

        admin = c.get("/api/analytics?period=month", headers={"X-API-Key": "ah_admin"}).json()
        assert admin["overview"]["jobs"]["total"] >= 1


def test_server_log_requires_admin(keys_app):
    """Журнал сервера несёт чужие имена файлов и трассировки."""
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        assert c.get("/api/logs", headers={"X-API-Key": "ah_alice"}).status_code == 403
        assert c.get("/api/logs", headers={"X-API-Key": "ah_admin"}).status_code == 200


def test_ssrf_guard_covers_settings_field(keys_app):
    """Адрес уведомления проверяется и внутри settings.

    Запрет на внутреннюю сеть обходился одной строкой в JSON: поле формы
    webhook_url отвергалось, а то же значение внутри settings проходило — и
    сервер сам ходил по адресу от своего имени.
    """
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        for payload in ('{"webhook_url":"http://169.254.169.254/latest/meta-data/"}',
                        '{"webhook_url":"file:///etc/passwd"}',
                        '{"webhook_url":"http://127.0.0.1:8080/api/keys"}'):
            r = c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
                       files={"file": ("а.wav", _wav(), "audio/wav")},
                       data={"settings": payload})
            assert r.status_code == 400, f"адрес прошёл: {payload} -> {r.status_code}"

        good = c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
                      files={"file": ("б.wav", _wav(), "audio/wav")},
                      data={"settings": '{"webhook_url":"https://example.org/hook"}'})
        assert good.status_code == 200, good.text


def test_metrics_endpoint_respects_monitoring_public(data_dir, monkeypatch):
    """У /api/metrics тот же порядок допуска, что и у /api/monitoring/metrics.

    Администратор закрывал метрики настройкой monitoring_public: false,
    /api/monitoring/metrics честно отвечал 401, а /api/metrics отдавал тот
    же снимок кому угодно.
    """
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.values["monitoring_public"] = False
    settings.api_keys["ah_key"] = {"name": "к", "role": "admin", "enabled": True}
    with TestClient(create_app(settings, start_queue=False)) as c:
        assert c.get("/api/monitoring/metrics").status_code == 401
        assert c.get("/api/metrics").status_code == 401, "второй адрес остался открытым"
        assert c.get("/api/metrics", headers={"X-API-Key": "ah_key"}).status_code == 200

    settings.values["monitoring_public"] = True
    with TestClient(create_app(settings, start_queue=False)) as c:
        assert c.get("/api/metrics").status_code == 200, "при monitoring_public должен быть открыт"


def test_webhook_secret_is_not_disclosed(keys_app):
    """Секрет подписи уведомлений не должен уходить в настройках."""
    keys_app.values["webhook_secret"] = "секрет-подписи"
    app = create_app(keys_app, start_queue=False)
    with TestClient(app) as c:
        values = c.get("/api/settings", headers={"X-API-Key": "ah_ro"}).json()["values"]
        assert values["webhook_secret"] == "***", "секрет отдан открытым текстом"
        assert "секрет-подписи" not in c.get(
            "/api/settings", headers={"X-API-Key": "ah_ro"}).text


def test_rejected_upload_leaves_no_file(data_dir, monkeypatch):
    """Отклонённая загрузка не должна оставлять файл на диске.

    Файл записывался до разбора полей формы, а разбор стоял вне защиты:
    каждая опечатка в имени параметра оставляла копию записи в uploads, на
    которую не ссылается ни одно задание — уборщик её никогда не найдёт.
    """
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    with TestClient(create_app(load(), start_queue=False)) as c:
        uploads = Path(data_dir) / "uploads"
        for payload in ({"опечатка": "1"}, {"settings": "{битый json"},
                        {"languagee": "ru"}):
            r = c.post("/api/jobs", files={"file": ("а.wav", _wav(), "audio/wav")},
                       data=payload)
            assert r.status_code == 400, payload
        left = list(uploads.glob("*")) if uploads.exists() else []
        assert left == [], f"на диске осталось {len(left)} файлов без задания"
        assert c.get("/api/jobs").json()["total"] == 0


def test_loading_settings_does_not_rotate_the_key(data_dir, monkeypatch):
    """Повторная загрузка настроек не должна выпускать новый ключ.

    Ключ читался только из config.yaml, а установки без него — а таких
    большинство — получали новый ключ при каждой загрузке, и файл
    api-key.txt переписывался. Достаточно было выполнить
    `python3 -m asrhub --check` при работающем сервере: в файле оказывался
    ключ, которого сервер не знает, и веб-интерфейс переставал пускать — с
    файлом, выглядящим совершенно правильным.
    """
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")

    first = load()
    keyfile = Path(data_dir) / "api-key.txt"
    assert keyfile.exists(), "ключ не создан при первом запуске"
    saved = keyfile.read_text(encoding="utf-8").strip()
    assert saved in first.api_keys

    for _ in range(3):
        again = load()
        assert keyfile.read_text(encoding="utf-8").strip() == saved, \
            "файл ключа переписан при повторной загрузке"
        assert saved in again.api_keys, "сервер не принял бы ключ из собственного файла"

    assert oct(keyfile.stat().st_mode)[-3:] == "600", "ключ доступен на чтение всем"


def test_saving_settings_keeps_the_file_private(data_dir: Path,
                                                monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path):
    """Сохранение настроек не должно раздавать права на файл с секретами.

    Запись шла через временный файл: `tmp.write_text(...)` создаёт его по
    umask (обычно 0644), а `tmp.replace(target)` отдаёт цели права ЭТОГО
    файла. Установщик ставил на config.yaml 0640, а первый же запуск
    сервера — тот самый, который дописывает туда автоматически созданный
    ключ доступа, — делал файл с ключами и токеном Hugging Face читаемым
    всем в системе.
    """
    from asrhub.config import load

    config = tmp_path / "config.yaml"
    config.write_text(f'data_dir: {data_dir}\nhf_token: "hf_токен1234567890абв"\n'
                      'server:\n  server_port: 8080\n',
                      encoding="utf-8")
    config.chmod(0o640)

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    settings = load(config)

    assert oct(config.stat().st_mode)[-3:] == "640", \
        "файл с ключами доступа стал читаемым всем"
    assert settings.api_keys, "ключ при первом запуске не создан — проверка ничего не стоит"
    assert not list(tmp_path.glob("*.tmp")), "временный файл остался на диске"


def test_saving_settings_keeps_the_hugging_face_token(data_dir: Path,
                                                      monkeypatch: pytest.MonkeyPatch,
                                                      tmp_path: Path):
    """Токен обязан пережить сохранение настроек.

    hf_token не относится ни к одной группе каталога, поэтому save() его не
    писал — и первое же сохранение (сервер делает его сам при первом
    запуске, когда выпускает ключ доступа) стирало из config.yaml токен,
    записанный установщиком. Диаризация после этого падала с «нужен токен»
    на ровном месте, а пользователь помнил, что вводил его в мастере.
    """
    from asrhub.config import load

    config = tmp_path / "config.yaml"
    config.write_text(f'data_dir: {data_dir}\nhf_token: "hf_токен1234567890абв"\n'
                      'server:\n  server_port: 8080\n',
                      encoding="utf-8")

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    first = load(config)
    assert first.hf_token == "hf_токен1234567890абв"

    body = config.read_text(encoding="utf-8")
    assert "hf_token" in body, "сохранение стёрло токен из файла"
    assert load(config).hf_token == "hf_токен1234567890абв"
    assert first.get("server_port") == 8080, "остальные параметры не должны пострадать"
