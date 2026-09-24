"""Заход 41: остаток программного интерфейса и базы.

Задание переопределяло любые параметры сервера — каталог моделей, адрес
языковой модели, тайм-аут, — а карточка задания показывала адреса и пути
инфраструктуры. Адрес в журнале доступа подделывался одним заголовком, а
предел попыток входа за nginx был общим на всех. Два безымянных ключа
делили одну область. Значение не того вида возвращалось как «ошибка базы»
с куском SQL.
"""
from __future__ import annotations

import io
import math
import struct
import threading
import wave
from pathlib import Path

import pytest
from asrhub.db import Database
from fastapi.testclient import TestClient


def _wav(секунд: float = 1.0) -> bytes:
    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(и / 8)))
                                  for и in range(int(16000 * секунд))))
    return буфер.getvalue()


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    настройки.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        yield клиент


# ---------------------------------------------------------------------------
# Параметры сервера — не параметры задания
# ---------------------------------------------------------------------------


def test_задание_не_переопределяет_параметры_сервера(tmp_path: Path):
    from asrhub.config import load

    настройки = load(apply_hardware=False)
    настройки.set("job_timeout_s", 600)
    настройки.set("max_retries", 2)
    итог = настройки.merged({"models_dir": str(tmp_path / "чужие"),
                             "llm_url": "http://внутренний:11434",
                             "webhook_allow_internal": True,
                             "job_timeout_s": 0, "max_retries": 10,
                             "beam_size": 3, "stream_window_s": 2.0})
    assert итог["models_dir"] == настройки.get("models_dir")
    assert итог["llm_url"] == настройки.get("llm_url")
    assert итог["webhook_allow_internal"] == настройки.get("webhook_allow_internal")
    assert итог["job_timeout_s"] == 600, "задание сняло предел времени"
    assert итог["max_retries"] == 2, "задание подняло число повторов"
    assert итог["beam_size"] == 3 and итог["stream_window_s"] == 2.0
    assert настройки.merged({"job_timeout_s": 60})["job_timeout_s"] == 60


def test_карточка_задания_без_адресов_и_путей_сервера(сервер):
    ответ = сервер.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
                        files={"file": ("звонок.wav", _wav(), "audio/wav")},
                        data={"settings": '{"models_dir": "/tmp/чужие", "beam_size": 3}'})
    assert ответ.status_code == 200, ответ.text
    карточка = сервер.get(f"/api/jobs/{ответ.json()['id']}",
                          headers={"X-API-Key": "ah_alice"}).json()
    параметры = карточка["params"]
    for ключ in ("models_dir", "llm_url", "telephony_host", "auth_ldap_url",
                 "backup_dir", "temp_dir", "max_queue_size"):
        assert ключ not in параметры, ключ
    assert параметры["beam_size"] == 3


# ---------------------------------------------------------------------------
# Адрес клиента за прокси
# ---------------------------------------------------------------------------


def test_адрес_из_заголовка_только_от_доверенного_прокси():
    from asrhub import audit

    прокси = type("К", (), {"host": "127.0.0.1"})()
    снаружи = type("К", (), {"host": "198.51.100.4"})()
    assert audit.адрес({"x-forwarded-for": "6.6.6.6"}, снаружи) == "198.51.100.4", \
        "клиент без прокси подписался чужим адресом"
    # Ближайший справа недоверенный: левее мог дописать сам клиент.
    assert audit.адрес({"x-forwarded-for": "6.6.6.6, 203.0.113.9"}, прокси) == "203.0.113.9"
    assert audit.адрес({"x-forwarded-for": "6.6.6.6"}, прокси, "") == "127.0.0.1"
    assert audit.адрес({"x-forwarded-for": "203.0.113.9"},
                       type("К", (), {"host": "10.0.0.5"})(), "10.0.0.5") == "203.0.113.9"


def test_предел_входа_считается_по_клиенту_за_прокси(сервер, monkeypatch):
    """За nginx у всех один адрес: чужой подбор пароля запирал вход всем."""
    state = сервер.app.state.hub
    ключи: list[str] = []
    monkeypatch.setattr(state, "check_rate", lambda ключ, предел: ключи.append(ключ))
    # Соединение приходит от nginx на той же машине, клиент — в заголовке.
    with TestClient(сервер.app, client=("127.0.0.1", 50000)) as за_прокси:
        за_прокси.post("/api/auth/login",
                       json={"username": "кто-то", "password": "не-тот"},
                       headers={"X-API-Key": "", "X-Forwarded-For": "203.0.113.9"})
    assert "login:203.0.113.9" in ключи, ключи


def test_доверенные_прокси_проверяются_при_сохранении():
    from asrhub.catalog.params import validate_value

    годно, _ = validate_value("trusted_proxies", "10.0.0.0/8, ::1, 192.168.1.10")
    assert годно
    годно, сообщение = validate_value("trusted_proxies", "10.0.0.0/8, мусор")
    assert not годно and "мусор" in сообщение


# ---------------------------------------------------------------------------
# Ключи и учётные записи
# ---------------------------------------------------------------------------


def test_ключ_без_имени_не_создаётся(сервер):
    """Два безымянных ключа получали имя «ключ» и видели задания друг друга."""
    for имя in ("", "   "):
        ответ = сервер.post("/api/keys", headers={"X-API-Key": "ah_admin"}, json={"name": имя})
        assert ответ.status_code == 400, ответ.text


def test_тёзка_ключа_предупреждён(сервер):
    ответ = сервер.post("/api/keys", headers={"X-API-Key": "ah_admin"},
                        json={"name": "Алиса"})
    assert ответ.status_code == 200, ответ.text
    assert "видят задания друг друга" in ответ.json().get("note", "")
    свой = сервер.post("/api/keys", headers={"X-API-Key": "ah_admin"},
                       json={"name": "Интеграция CRM"})
    assert "note" not in свой.json()


def test_поле_учётки_не_того_вида_это_400(сервер):
    """Словарь в имени доходил до SQLite: 507 и внутренности базы в ответе."""
    новый = сервер.post("/api/users", headers={"X-API-Key": "ah_admin"},
                        json={"username": "оператор", "password": "Пароль-оператора-1"})
    assert новый.status_code == 200, новый.text
    ид = новый.json()["id"]
    ответ = сервер.patch(f"/api/users/{ид}", headers={"X-API-Key": "ah_admin"},
                         json={"display_name": {"a": 1}})
    assert ответ.status_code == 400, ответ.text
    assert "binding" not in ответ.text
    assert "display_name" in ответ.text, "отказ не называет поле"


def test_строка_false_отключает_учётку_но_не_последнего_админа(сервер):
    """`1 if "false" else 0` — это единица: «отключить» не отключало."""
    новый = сервер.post("/api/users", headers={"X-API-Key": "ah_admin"},
                        json={"username": "стажёр", "password": "Пароль-стажёра-1"})
    ид = новый.json()["id"]
    ответ = сервер.patch(f"/api/users/{ид}", headers={"X-API-Key": "ah_admin"},
                         json={"enabled": "false"})
    assert ответ.status_code == 200 and ответ.json()["enabled"] is False, ответ.text
    админы = [у for у in сервер.get("/api/users", headers={"X-API-Key": "ah_admin"})
              .json()["users"] if у["role"] == "admin"]
    assert len(админы) == 1
    ответ = сервер.patch(f"/api/users/{админы[0]['id']}", headers={"X-API-Key": "ah_admin"},
                         json={"enabled": "false"})
    assert ответ.status_code == 403, "строкой «false» отключился последний администратор"
    ответ = сервер.patch(f"/api/users/{ид}", headers={"X-API-Key": "ah_admin"},
                         json={"enabled": "может быть"})
    assert ответ.status_code == 400


# ---------------------------------------------------------------------------
# База
# ---------------------------------------------------------------------------


def test_ошибка_чтения_не_выносит_sql_наружу(tmp_path: Path):
    from asrhub.errors import StorageError

    база = Database(tmp_path / "asrhub.db")
    try:
        with pytest.raises(StorageError) as отказ:
            база.query("SELECT * FROM нет_такой_таблицы")
    finally:
        база.close()
    assert "sql" not in (отказ.value.details or {})


def test_значение_не_того_вида_в_запись_это_отказ_ввода(tmp_path: Path):
    from asrhub.errors import ConfigError

    база = Database(tmp_path / "asrhub.db")
    try:
        база.create_job({"id": "j", "filename": "a.wav"})
        with pytest.raises(ConfigError):
            база.execute("UPDATE jobs SET tags=? WHERE id=?", ({"a": 1}, "j"))
    finally:
        база.close()


def test_контрольный_прогон_не_съедает_квоту_владельца(tmp_path: Path):
    база = Database(tmp_path / "asrhub.db")
    try:
        база.create_job({"id": "своё", "filename": "a.wav", "owner": "Алиса",
                         "status": "completed", "file_size": 100})
        база.create_job({"id": "контроль", "filename": "a.wav", "owner": "Алиса",
                         "status": "completed", "file_size": 100, "source": "control"})
        расход = база.owner_usage("Алиса", 0)
    finally:
        база.close()
    assert расход["jobs"] == 1


def test_признак_усечённого_поиска_у_каждого_потока_свой(tmp_path: Path):
    """Параллельный поиск другого пользователя перезаписывал признак."""
    база = Database(tmp_path / "asrhub.db")
    try:
        база.last_search_truncated = True
        увиденное: list[bool] = []
        поток = threading.Thread(target=lambda: увиденное.append(база.last_search_truncated))
        поток.start()
        поток.join()
        assert увиденное == [False]
        assert база.last_search_truncated is True
    finally:
        база.close()


def test_подчёркивание_и_процент_в_поиске_это_буквы(tmp_path: Path):
    """В журнале доступа и справочнике «_» в запросе означал «любой знак»."""
    база = Database(tmp_path / "asrhub.db")
    try:
        for путь in ("/api/jobs", "/api/some_thing"):
            база.audit_add(action="смотрел", actor="кто", actor_id="", kind="key", role="",
                           method="GET", path=путь, status=200, ip="10.0.0.1")
        журнал = база.audit_list(query="_")
        for номер, почта in enumerate(("ivan_petrov@example.com", "ivanXpetrov@example.com")):
            база.employee_save({"last_name": "Иванов", "first_name": "Пётр", "email": почта,
                                "external_id": f"e{номер}"})
        справочник = база.employee_list(query="ivan_")
    finally:
        база.close()
    assert [с["path"] for с in журнал["items"]] == ["/api/some_thing"], журнал
    строки = справочник.get("items") or []
    assert [с["email"] for с in строки] == ["ivan_petrov@example.com"], строки
