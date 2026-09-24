"""Заход 40: секреты, чужие данные и обращения сервера наружу.

Каждая проверка закрывает находку сороковой ревизии и до исправления
падала. Общее у них то же, что и в прошлых заходах: защита стояла на одном
пути и отсутствовала на соседнем, который делает то же самое другими
словами — полем в теле вместо поля формы, другой записью адреса, другим
маршрутом к той же записи.
"""
from __future__ import annotations

import io
import json
import math
import sqlite3
import struct
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest
from asrhub.api import create_app
from asrhub.config import Settings, load
from asrhub.errors import ConfigError
from fastapi.testclient import TestClient

СЕКРЕТЫ = {
    "llm_api_key": "sk-модель-7731",
    "webhook_secret": "подпись-9001",
    "crm_token": "crm-токен-4242",
    "crm_url": "https://crm.example.com/rest/1/вебхук-секрет/",
    "tracker_url": "https://chat.example.com/hooks/трекер-секрет",
    "employees_url": "https://hr.example.com/выгрузка?token=кадры-секрет",
    "telephony_secret": "агент-секрет-555",
}


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
def ключи(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    настройки = load()
    настройки.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    настройки.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    настройки.api_keys["ah_bob"] = {"name": "Боб", "role": "user", "enabled": True}
    return настройки


def _поставить(клиент: TestClient, ключ: str, **поля: Any) -> dict[str, Any]:
    ответ = клиент.post("/api/jobs", headers={"X-API-Key": ключ},
                        files={"file": ("звонок.wav", _wav(), "audio/wav")},
                        data={k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict) else v)
                              for k, v in поля.items()})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


# ---------------------------------------------------------------------------
# Снимок настроек в параметрах задания
# ---------------------------------------------------------------------------


def test_секреты_сервера_не_ложатся_в_задание(ключи):
    """В params каждого задания ложился полный снимок настроек сервера.

    Вместе с ним — ключ модели, секрет подписи уведомлений, токен CRM, адрес
    вебхука Bitrix24, — и всё это отдавалось владельцу задания в карточке
    и выгрузке. Пользователь без права на настройки читал секреты
    администратора, просто поставив файл.
    """
    for ключ, значение in СЕКРЕТЫ.items():
        ключи.set(ключ, значение, source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        задание = _поставить(клиент, "ah_alice")
        карточка = клиент.get(f"/api/jobs/{задание['id']}",
                              headers={"X-API-Key": "ah_alice"}).text
        сырое = app.state.hub.db.query_one("SELECT params FROM jobs WHERE id=?",
                                           (задание["id"],))["params"]
    for значение in СЕКРЕТЫ.values():
        assert значение not in карточка, f"секрет «{значение}» виден владельцу задания"
        assert значение not in сырое, f"секрет «{значение}» лежит в базе в параметрах"


def test_старые_задания_вычищаются_миграцией(tmp_path: Path):
    """Задания, созданные до исправления, хранят секреты в самой базе.

    Выдача чистит параметры сама, но копии базы уходят за пределы сервера:
    вычищать надо и на диске. Строки без секретов миграция не трогает.
    """
    from asrhub.db import Database

    путь = tmp_path / "asrhub.db"
    Database(путь).close()
    with sqlite3.connect(путь) as связь:
        связь.execute(
            "INSERT INTO jobs (id, filename, status, created_at, updated_at, params) "
            "VALUES (?,?,?,?,?,?)",
            ("j-секрет", "a.wav", "completed", time.time(), time.time(),
             json.dumps({"model": "demo-simulator", "llm_api_key": "sk-старый",
                         "crm_token": "crm-старый", "telephony_stations": [{"secret": "x"}],
                         "webhook_url": "https://ok.example.com/hook"})))
        связь.execute(
            "INSERT INTO jobs (id, filename, status, created_at, updated_at, params) "
            "VALUES (?,?,?,?,?,?)",
            ("j-чистый", "b.wav", "completed", time.time(), time.time(),
             json.dumps({"model": "demo-simulator", "llm_api_key": ""})))
        связь.execute("PRAGMA user_version=25")
    база = Database(путь)
    try:
        параметры = json.loads(база.query_one(
            "SELECT params FROM jobs WHERE id='j-секрет'")["params"])
        чистые = база.query_one("SELECT params FROM jobs WHERE id='j-чистый'")["params"]
    finally:
        база.close()
    assert "llm_api_key" not in параметры and "crm_token" not in параметры
    assert "telephony_stations" not in параметры
    assert параметры["model"] == "demo-simulator", "остальные параметры должны остаться"
    # Адрес уведомления у задания свой — это не секрет сервера.
    assert "webhook_url" not in Settings.SECRET_KEYS or параметры.get("webhook_url") is None
    assert json.loads(чистые) == {"model": "demo-simulator", "llm_api_key": ""}, \
        "строку без секретов миграция переписывать не должна"


def test_выдача_чистит_параметры_даже_без_миграции(tmp_path: Path):
    from asrhub.db import Database

    база = Database(tmp_path / "asrhub.db")
    try:
        база.create_job({"id": "j1", "filename": "a.wav", "status": "completed",
                         "params": {"model": "m", "crm_token": "crm-в-базе",
                                    "hf_token": "hf_в_базе"}})
        # Имитация строки, записанной старой версией в обход create_job.
        база.execute("UPDATE jobs SET params=? WHERE id='j1'",
                     (json.dumps({"model": "m", "crm_token": "crm-в-базе"}),))
        задание = база.get_job("j1")
    finally:
        база.close()
    assert "crm_token" not in задание["params"]
    assert задание["params"]["model"] == "m"


def test_задание_не_переопределяет_секрет_сервера():
    """Поле в JSON задания подменяло ключ модели или токен CRM сервера."""
    from asrhub import catalog

    настройки = Settings(values={**catalog.defaults(), "llm_api_key": "настоящий"})
    итог = настройки.merged({"llm_api_key": "подставной", "crm_token": "подставной",
                             "telephony_stations": [{"id": "x", "name": "x"}]})
    assert итог["llm_api_key"] == "настоящий"
    assert итог.get("crm_token") != "подставной"
    assert итог.get("telephony_stations") != [{"id": "x", "name": "x"}]


def test_заглушка_адреса_уведомления_не_ломает_загрузку(ключи):
    """Неадминистратор получает webhook_url как «***» и отправлял его обратно.

    Загрузка из интерфейса отвечала 400 «Адрес уведомления должен начинаться
    с http://» — у любого пользователя, кроме администратора, как только
    администратор задавал адрес уведомления.
    """
    ключи.set("webhook_url", "https://hooks.example.com/asr", source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        настройки = клиент.get("/api/settings", headers={"X-API-Key": "ah_alice"}).json()
        assert настройки["values"]["webhook_url"] == "***"
        задание = _поставить(клиент, "ah_alice", settings=настройки["values"])
    assert задание["status"] in ("queued", "completed")


def test_своё_адресное_уведомление_у_задания_остаётся():
    """Адрес уведомления у задания может быть свой — это не секрет сервера."""
    from asrhub import catalog

    настройки = Settings(values={**catalog.defaults(),
                                 "webhook_url": "https://server.example.org/hook"})
    итог = настройки.merged({"webhook_url": "https://my.example.org/done"})
    assert итог["webhook_url"] == "https://my.example.org/done"
    # А заглушка — не значение: остаётся адрес сервера.
    assert настройки.merged({"webhook_url": "***"})["webhook_url"] == \
        "https://server.example.org/hook"


def test_заглушка_не_затирает_секрет_при_сохранении_настроек(ключи):
    """«Применить» отправлял «***» обратно, и оно ложилось поверх настоящего."""
    ключи.set("crm_token", "настоящий-токен", source="test")
    ключи.set("telephony_stations", [{
        "id": "главная", "name": "Офис", "source": "ami", "host": "10.0.0.5",
        "port": 5038, "username": "asrhub", "secret": "пароль-АТС",
        "recordings_dir": "/var/spool/asterisk/monitor"}], source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        снимок = клиент.get("/api/settings", headers={"X-API-Key": "ah_admin"}).json()["values"]
        # Пароль станции прячется даже от администратора — он и вернётся
        # заглушкой вместе со всем списком станций.
        assert снимок["telephony_stations"][0]["secret"] == "***"
        ответ = клиент.put("/api/settings", headers={"X-API-Key": "ah_admin"},
                           json={"crm_token": "***",
                                 "telephony_stations": снимок["telephony_stations"]})
        assert ответ.status_code == 200, ответ.text
        итог = app.state.hub.settings
        assert итог.get("crm_token") == "настоящий-токен"
        assert итог.get("telephony_stations")[0]["secret"] == "пароль-АТС"


def test_приоритет_задания_зажат_в_пределы(ключи):
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        высокий = _поставить(клиент, "ah_alice", priority="100000")
        низкий = _поставить(клиент, "ah_alice", priority="-50")
    assert высокий["priority"] == 100
    assert низкий["priority"] == 0


# ---------------------------------------------------------------------------
# Обращения сервера наружу
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("адрес", [
    "http://127.1/", "http://2130706433/", "http://0x7f000001/",
    "http://017700000001/", "http://2852039166/latest/meta-data/",
    "http://[::ffff:127.0.0.1]/", "http://0.0.0.0:8080/", "http://10.1/",
])
def test_внутренние_адреса_в_любой_записи_запрещены(адрес):
    """`ipaddress` не понимает «127.1» и «2130706433», а сокет понимает.

    Проверка считала такие записи доменными именами «проверять некому», и
    сервер ходил во внутреннюю сеть и к метаданным облака от своего имени.
    """
    from asrhub.job_queue import check_outbound_url

    with pytest.raises(ConfigError):
        check_outbound_url(адрес)


def test_старая_запись_адреса_понимается_и_без_распознавателя(monkeypatch):
    """«127.1» переводит в адрес сам сокет — даже там, где имена не узнаются."""
    import socket

    from asrhub.job_queue import check_outbound_url

    def нет_распознавателя(*a, **k):
        raise OSError("распознаватель недоступен")

    monkeypatch.setattr(socket, "getaddrinfo", нет_распознавателя)
    for адрес in ("http://127.1/", "http://2130706433/", "http://0x7f000001/"):
        with pytest.raises(ConfigError):
            check_outbound_url(адрес)


def test_имя_указывающее_внутрь_запрещено(monkeypatch):
    """Имя, которое превращается в 10.0.0.5, ничем не лучше самого 10.0.0.5."""
    import socket

    from asrhub.job_queue import check_outbound_url

    def распознать(хост, *a, **k):
        адрес = "10.0.0.5" if хост == "crm.внутри.example" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (адрес, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", распознать)
    with pytest.raises(ConfigError):
        check_outbound_url("https://crm.внутри.example/hook")
    assert check_outbound_url("https://crm.снаружи.example/hook")


def test_внешний_адрес_проходит():
    from asrhub.job_queue import check_outbound_url

    assert check_outbound_url("https://93.184.216.34/hook") == "https://93.184.216.34/hook"


class _Перенаправитель(BaseHTTPRequestHandler):
    куда = "http://127.0.0.1:1/"
    запросы: list[str] = []

    def do_POST(self):  # noqa: N802
        type(self).запросы.append(self.path)
        self.send_response(302)
        self.send_header("Location", type(self).куда)
        self.end_headers()

    do_GET = do_POST  # noqa: N815

    def log_message(self, *args):  # noqa: D401
        return


def test_перенаправление_во_внутреннюю_сеть_не_выполняется():
    """urlopen шёл по 302 куда скажут: внешний приёмник уводил сервер внутрь."""
    from asrhub.job_queue import открыватель_наружу

    сервер = HTTPServer(("127.0.0.1", 0), _Перенаправитель)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        _Перенаправитель.куда = "http://169.254.169.254/latest/meta-data/"
        адрес = f"http://127.0.0.1:{сервер.server_port}/hook"
        # Сам приёмник на 127.0.0.1 разрешён явно — проверяется только переход.
        открыватель = открыватель_наружу(
            проверка=lambda а: None if а == адрес else __import__(
                "asrhub.job_queue", fromlist=["x"]).check_outbound_url(а))
        with pytest.raises(ConfigError):
            открыватель.open(адрес, data=b"{}", timeout=5)
    finally:
        сервер.shutdown()


def test_уведомление_на_внутренний_адрес_при_отправке_блокируется(ключи, monkeypatch):
    """Адрес проверялся при приёме, а имя могло начать указывать внутрь позже.

    При отправке адрес проверяется ещё раз; отказ — это «blocked», а не
    «failed» с текстом ответа внутреннего сервиса.
    """
    import urllib.error
    import urllib.request

    class _БезОжидания(threading.Event):
        """Паузы между попытками — мгновенные: на старом коде проверка иначе
        ждала бы минуту, пока пять попыток не кончатся."""

        def wait(self, timeout=None):
            return self.is_set()

    вызовы: list[str] = []

    def запрос(self, *a, **k):
        вызовы.append("сеть")
        raise urllib.error.URLError("нет сети")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", запрос)
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: запрос(None))
    app = create_app(ключи, start_queue=False)
    with TestClient(app):
        очередь = app.state.hub.queue
        monkeypatch.setattr(очередь, "_stop", _БезОжидания())
        задание = {"id": "j-hook", "webhook_url": "http://127.0.0.1:9/hook"}
        app.state.hub.db.create_job({"id": "j-hook", "filename": "a.wav",
                                     "status": "completed",
                                     "webhook_url": "http://127.0.0.1:9/hook"})
        очередь._deliver_webhook(задание, b"{}")
        статус = app.state.hub.db.get_job("j-hook")["webhook_status"]
    assert статус == "blocked"
    assert not вызовы


# ---------------------------------------------------------------------------
# Чужие записи: агенты, контроль качества, очередь модели
# ---------------------------------------------------------------------------


def test_чужого_агента_не_перехватить(ключи):
    """Личность агента задавалась одним телом запроса.

    Посторонний ключ с правом записи называл чужой agent_id — и забирал
    задание администратора для чужой станции, а через /ask заранее вписывал
    идентификаторы её звонков, чтобы настоящие пропускались.
    """
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        привет = клиент.post("/api/telephony/agent/hello", headers={"X-API-Key": "ah_alice"},
                             json={"agent_id": "pbx-alice", "host": "pbx1", "version": "1"})
        assert привет.status_code == 200, привет.text
        перехват = клиент.post("/api/telephony/agent/hello",
                               headers={"X-API-Key": "ah_bob"},
                               json={"agent_id": "pbx-alice", "host": "evil", "version": "1"})
        команда = клиент.get("/api/telephony/agent/command?agent_id=pbx-alice",
                             headers={"X-API-Key": "ah_bob"})
        # По хосту и имени чужой идентификатор тоже не выдаётся.
        по_хосту = клиент.post("/api/telephony/agent/hello",
                               headers={"X-API-Key": "ah_bob"},
                               json={"host": "pbx1", "version": "1"}).json()
        своя = клиент.get("/api/telephony/agent/command?agent_id=pbx-alice",
                          headers={"X-API-Key": "ah_alice"})
        запись = app.state.hub.db.agent_get("pbx-alice")
    assert перехват.status_code == 403, перехват.text
    assert команда.status_code == 403, команда.text
    assert по_хосту["agent_id"] != "pbx-alice"
    assert своя.status_code == 200, своя.text
    assert запись["owner"] == "Алиса" and запись["host"] == "pbx1"


def _проверка_качества(app, владелец: str) -> int:
    db = app.state.hub.db
    ид = f"j-{владелец}"
    db.create_job({"id": ид, "filename": f"{владелец}.wav", "status": "completed",
                   "owner": владелец})
    номер = db.qa_assign(ид, assigned_to="", assigned_by="админ",
                         due_at=time.time() + 3600, agent="101", auto_score=70.0,
                         reason="проба")
    assert номер is not None
    return int(номер)


def test_проверки_качества_видны_и_закрываются_только_свои(ключи):
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        чужая = _проверка_качества(app, "Боб")
        своя = _проверка_качества(app, "Алиса")
        список = клиент.get("/api/qa", headers={"X-API-Key": "ah_alice"}).json()
        видно = {int(з["id"]) for з in список["items"]}
        assert своя in видно and чужая not in видно, "чужие проверки видны в очереди"
        закрыть = клиент.put(f"/api/qa/{чужая}", headers={"X-API-Key": "ah_alice"},
                             json={"score": 10, "reviewer": "админ"})
        assert закрыть.status_code == 403, закрыть.text
        своё = клиент.put(f"/api/qa/{своя}", headers={"X-API-Key": "ah_alice"},
                          json={"score": 90, "reviewer": "админ"})
        assert своё.status_code == 200, своё.text
        строка = app.state.hub.db.qa_get(своя)
    # Подписаться чужим именем нельзя: калибровка проверяющих держится на этом.
    assert строка["reviewer"] == "Алиса"


def test_доля_согласия_считается_по_оценённым(tmp_path: Path):
    """Проверка без балла согласия не выражает, а шла в знаменатель.

    Руководитель закрыл две проверки: одну с баллом рядом с автоматом,
    другую без балла (разговор не по теме). Доля согласия выходила 50 %,
    хотя в единственной оценённой проверке он с автоматом согласен.
    """
    from asrhub.db import Database

    база = Database(tmp_path / "asrhub.db")
    try:
        номера = []
        for ид in ("a", "b"):
            база.create_job({"id": ид, "filename": ид, "status": "completed"})
            номера.append(база.qa_assign(ид, assigned_to="", assigned_by="x",
                                         due_at=time.time() + 60, agent="1",
                                         auto_score=70.0, reason=""))
        база.qa_submit(int(номера[0]), reviewer="р", score=75.0)
        база.qa_submit(int(номера[1]), reviewer="р", score=None)
        свод = база.qa_stats()
    finally:
        база.close()
    assert свод["done"] == 2
    assert свод["agree_share"] == 1.0


def test_счётчики_очереди_модели_по_своим_записям(ключи):
    """Список чужих записей скрыт, а счётчики «ждут четыре» говорили о чужой работе."""
    ключи.set("llm_backend", "stub", source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        db = app.state.hub.db
        for номер, владелец in enumerate(("Алиса", "Боб", "Боб")):
            ид = f"j-{номер}"
            db.create_job({"id": ид, "filename": "x.wav", "status": "completed",
                           "owner": владелец, "text": "текст"})
            db.llmq_put(ид, kind="разбор", priority=5)
        свои = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_alice"}).json()
        все = клиент.get("/api/llm/queue", headers={"X-API-Key": "ah_admin"}).json()
    счёт_алисы = sum(int(v) for v in (свои.get("counts") or {}).values())
    счёт_всех = sum(int(v) for v in (все.get("counts") or {}).values())
    assert счёт_алисы == 1, свои.get("counts")
    assert счёт_всех == 3, все.get("counts")


# ---------------------------------------------------------------------------
# Кривые числа от клиента — отказ, а не поломка
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("тело", [
    {"mode": "period", "since": "вчера", "until": 1},
    {"mode": "period", "since": 1, "until": "завтра"},
])
def test_кривая_граница_сбора_архива_это_400(ключи, тело):
    """«Вчера» вместо числа давало внутреннюю ошибку сервера вместо отказа."""
    app = create_app(ключи, start_queue=False)
    with TestClient(app, raise_server_exceptions=False) as клиент:
        ответ = клиент.post("/api/telephony/collect", headers={"X-API-Key": "ah_admin"},
                            json=тело)
    assert ответ.status_code == 400, ответ.text


@pytest.mark.parametrize("поле,значение", [
    ("temperature", "горячо"), ("max_tokens", "много"), ("top_p", [1, 2]),
])
def test_кривое_число_в_шлюзе_модели_это_400(ключи, поле, значение):
    """`float("горячо")` доходил до общего обработчика: 500 и трасса в журнале."""
    ключи.set("llm_backend", "stub", source="test")
    ключи.set("llm_network_enabled", True, source="test")
    app = create_app(ключи, start_queue=False)
    with TestClient(app, raise_server_exceptions=False) as клиент:
        ответ = клиент.post("/api/llm/v1/chat/completions",
                            headers={"X-API-Key": "ah_admin"},
                            json={"messages": [{"role": "user", "content": "привет"}],
                                  поле: значение})
    assert ответ.status_code == 400, ответ.text


# ---------------------------------------------------------------------------
# Приёмники метрик: адреса с секретами и перенаправления
# ---------------------------------------------------------------------------


def test_секрет_в_адресе_приёмника_прячется():
    from asrhub.monitoring.pushers import скрыть_адрес

    скрыто = скрыть_адрес("http://user:пароль@influx:8086/write?db=a&u=admin&p=S3cret&token=T0k")
    assert "пароль" not in скрыто and "S3cret" not in скрыто and "T0k" not in скрыто
    assert "influx:8086" in скрыто


def test_проба_приёмника_не_ставит_его_в_работу():
    """«Проверить приёмник» ставил несохранённый приёмник в рассылку."""
    from asrhub.monitoring.pushers import PushManager, Target

    отправитель = PushManager(lambda: [])
    проба = Target.from_dict({"kind": "webhook", "url": "http://127.0.0.1:9/x",
                              "name": "проба", "timeout_s": 1})
    итог = отправитель.push_once(проба, [], проба=True)
    assert "ok" in итог
    assert "проба" not in [t["name"] for t in отправитель.targets()]


def test_сбой_протокола_приёмника_не_валит_поток():
    """InvalidURL и BadStatusLine не ловились: 500 в проверке и трасса в потоке."""
    from asrhub.monitoring.pushers import PushManager, Target

    отправитель = PushManager(lambda: [])
    кривой = Target.from_dict({"kind": "webhook", "url": "http://influx:8086x/write",
                               "timeout_s": 1})
    итог = отправитель.push_once(кривой, [], проба=True)
    assert итог["ok"] is False and итог.get("error")


# ---------------------------------------------------------------------------
# Размер тела и прочее
# ---------------------------------------------------------------------------


def test_тело_без_длины_режется_по_пределу(ключи):
    """Предел загрузки проверялся по Content-Length, а потоковое тело его не несёт."""
    ключи.set("max_upload_mb", 1, source="test")
    app = create_app(ключи, start_queue=False)

    def куски():
        for _ in range(40):
            yield b"x" * (256 * 1024)

    with TestClient(app, raise_server_exceptions=False) as клиент:
        ответ = клиент.post("/api/llm/queue/add", headers={"X-API-Key": "ah_admin",
                                                           "Content-Type": "application/json"},
                            content=куски())
    assert ответ.status_code == 413, ответ.text


def test_заглушённая_копия_не_называет_номер_в_имени(ключи):
    """Тело копии заглушено, а в заголовке скачивания стоял номер клиента.

    Ключу с обезличиванием /audio и /download имя давно прячут, а у копии с
    заглушёнными данными — той, ради которой обезличивание и заводят, —
    имя уходило как есть.
    """
    ключи.api_keys["ah_mask"] = {"name": "Алиса", "role": "user", "enabled": True,
                                 "mask_pii": True}
    app = create_app(ключи, start_queue=False)
    with TestClient(app) as клиент:
        загрузки = Path(app.state.hub.settings.paths.uploads)
        исходник = загрузки / "up_1.wav"
        исходник.write_bytes(_wav())
        (загрузки / "up_1.redacted.wav").write_bytes(_wav())
        app.state.hub.db.create_job({"id": "j-имя", "filename": "79161234567-звонок.wav",
                                     "status": "completed", "owner": "Алиса",
                                     "file_path": str(исходник)})
        ответ = клиент.get("/api/jobs/j-имя/redacted", headers={"X-API-Key": "ah_mask"})
        открытый = клиент.get("/api/jobs/j-имя/redacted", headers={"X-API-Key": "ah_alice"})
    assert ответ.status_code == 200, ответ.text
    assert "79161234567" not in ответ.headers.get("content-disposition", "")
    # Без обезличивания имя остаётся человеческим.
    assert "79161234567" in открытый.headers.get("content-disposition", "") or \
        "%37%39" in открытый.headers.get("content-disposition", "").lower() or \
        "79161234567" in __import__("urllib.parse", fromlist=["x"]).unquote(
            открытый.headers.get("content-disposition", ""))
