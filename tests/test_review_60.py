"""Заход 60: остаток главы 33 — сервер.

Отпечаток экземпляра со случайным хвостом, прежние ключи — в хеш, ключ
идемпотентности для одновременных запросов, близнецы в очереди, прореженный
прогресс и история событий под замком, лента событий отбором в SQL, пауза
мёртвого приёмника уведомлений, `PRAGMA optimize`, длительность без второго
ffprobe, суточная уборка от файлов к базе, ключ «только метрики» и белый
список сетей, цепочка хешей журнала доступа и копия в syslog, копии с
записями, шифрование и вынос копии.
"""
from __future__ import annotations

import json
import socket
import time
from pathlib import Path
from typing import Any

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]


@pytest.fixture()
def database(tmp_path: Path):
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def keys_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as client:
        admin = (tmp_path / "data" / "api-key.txt").read_text(encoding="utf-8").strip()
        client.headers.update({"X-API-Key": admin})
        yield client


# ---------------------------------------------------------------------------
# Экземпляр и ключи
# ---------------------------------------------------------------------------

def test_instance_id_differs_between_containers_with_pid_one():
    """Два контейнера с одним именем узла и pid 1 давали одно имя экземпляра."""
    from asrhub import instance

    assert instance.INSTANCE_ID.startswith(f"{instance.HOSTNAME}:")
    хвост = instance.INSTANCE_ID.rsplit(":", 1)[1]
    pid, _, случайное = хвост.partition("-")
    assert pid.isdigit() and len(случайное) == 6, instance.INSTANCE_ID
    # Свой экземпляр узнаётся по-прежнему — по pid в имени.
    assert instance.process_alive(instance.INSTANCE_ID)


def test_plaintext_keys_are_hashed_on_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Ключ до 3.1.29 лежал в файле настроек открытым текстом — и продолжал."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    данные = tmp_path / "data"
    данные.mkdir()
    ключ = "ah_legacy0123456789abcdef0123456789"
    конфиг = tmp_path / "asrhub.yaml"
    конфиг.write_text(
        "api_keys:\n"
        f"  {ключ}:\n"
        "    role: admin\n"
        "    name: прежний\n", encoding="utf-8")
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(данные))
    monkeypatch.setenv("ASRHUB_CONFIG", str(конфиг))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as client:
        ответ = client.get("/api/keys", headers={"X-API-Key": ключ})
        assert ответ.status_code == 200, ответ.text
    текст = конфиг.read_text(encoding="utf-8")
    assert ключ not in текст, "прежний ключ так и остался в файле открытым текстом"
    assert "sha256:" in текст


def test_idempotency_claim_marks_a_key_in_progress(database):
    """Два одновременных запроса с одним ключом: второй видит «принимается»."""
    assert database.kv_claim("idem:x", {"job": None}, stale_s=600) is None
    занят = database.kv_claim("idem:x", {"job": None}, stale_s=600)
    assert занят is not None, "второй запрос не увидел, что ключ уже принимается"
    # Пометка со сроком: брошенная (процесс упал) не держит ключ вечно.
    database.execute("UPDATE kv SET ts=? WHERE key=?", (time.time() - 700, "idem:x"))
    assert database.kv_claim("idem:x", {"job": None}, stale_s=600) is None
    database.del_kv("idem:x")
    assert database.get_kv("idem:x") is None


def test_in_progress_error_is_documented():
    from asrhub.errors import InProgress

    assert InProgress.http_status == 409 and InProgress.code == "in_progress"
    assert "`in_progress`" in (КОРЕНЬ / "docs" / "08-api.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Близнецы в очереди
# ---------------------------------------------------------------------------

def _задание(db: Any, **поля: Any) -> str:
    return db.create_job({"filename": "звонок.wav", "owner": "отдел", "model": "demo-simulator",
                          "file_hash": "один-и-тот-же", "params": {"_hash": "настройки"},
                          **поля})


def test_a_queued_twin_waits_for_the_running_one(database):
    """Две одинаковые записи подряд распознавались обе: кеш видит только готовое."""
    from asrhub.job_queue import STATUS_QUEUED, STATUS_RUNNING

    идёт = _задание(database, status=STATUS_RUNNING)
    ждёт = _задание(database, status=STATUS_QUEUED)
    чужое = _задание(database, status=STATUS_QUEUED, owner="другой")
    другие_настройки = _задание(database, status=STATUS_QUEUED,
                                params={"_hash": "иные"})
    без_дедупликации = _задание(database, status=STATUS_QUEUED,
                                params={"_hash": "настройки", "deduplicate_jobs": False})
    ждут = database.ждут_близнеца([ждёт, чужое, другие_настройки, без_дедупликации], [])
    assert ждут == {ждёт}
    # Только что взятое этим процессом (в базе ещё «в очереди») — тоже близнец.
    database.update_job(идёт, status=STATUS_QUEUED)
    assert database.ждут_близнеца([ждёт], [идёт]) == {ждёт}
    assert database.ждут_близнеца([ждёт], []) == set()


def test_a_waiting_twin_takes_the_finished_result(database, tmp_path: Path):
    """Дождавшийся близнеца получает копию результата вместо прогона модели."""
    from asrhub import job_queue as jq

    готово = _задание(database, status=jq.STATUS_COMPLETED, text="готовая расшифровка",
                      words_count=2, finished_at=time.time())
    database.save_segments(готово, [{"start": 0.0, "end": 1.0, "text": "готовая расшифровка"}])
    ждал = _задание(database, status=jq.STATUS_RUNNING, filename="второй.wav")

    queue = jq.JobQueue.__new__(jq.JobQueue)
    queue.db = database
    queue.content_index = None
    queue.llm_worker = None
    queue.on_event = None
    queue._copy_results = lambda *a, **k: None
    queue._уведомление_ждёт = lambda job, **k: {}
    взят = queue._взять_у_близнеца(database.get_job(ждал), {"deduplicate_jobs": True})
    assert взят
    строка = database.get_job(ждал)
    assert строка["status"] == jq.STATUS_COMPLETED
    assert строка["cached_from"] == готово
    assert строка["text"] == "готовая расшифровка"
    assert database.get_segments(ждал), "реплики близнеца не перенесены"
    # Без дедупликации — обычный прогон.
    третий = _задание(database, status=jq.STATUS_RUNNING)
    assert not queue._взять_у_близнеца(database.get_job(третий), {"deduplicate_jobs": False})


def test_pick_skips_a_job_whose_twin_is_running(database):
    """Выборка очереди не отдаёт воркеру задание, у которого идёт близнец."""
    from asrhub import job_queue as jq

    class Настройки(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    queue = jq.JobQueue.__new__(jq.JobQueue)
    queue.db = database
    queue.settings = Настройки(max_concurrent_jobs=4, scheduling_policy="priority_fifo")
    import threading

    queue._lock = threading.RLock()
    queue._running, queue._concurrency, queue._model_counts = {}, {}, {}
    queue._пауза_из_базы = lambda: False
    queue._старение_с = lambda: 0
    queue._note_concurrency = lambda: None
    идёт = _задание(database, status=jq.STATUS_RUNNING)
    ждёт = _задание(database, status=jq.STATUS_QUEUED, priority=90)
    другое = _задание(database, status=jq.STATUS_QUEUED, file_hash="иная запись", priority=10)
    взято = queue._next_job(0)
    assert взято is not None and взято["id"] == другое, (идёт, ждёт, взято)


# ---------------------------------------------------------------------------
# События
# ---------------------------------------------------------------------------

def test_progress_is_thinned_and_last_value_wins(monkeypatch: pytest.MonkeyPatch):
    """Движок докладывает долю на каждый кусок звука — подписчикам это не нужно."""
    from asrhub.api import event_hub as модуль

    monkeypatch.setattr(модуль, "ПРОГРЕСС_НЕ_ЧАЩЕ_С", 0.15)
    hub = модуль.EventHub()
    for шаг in range(50):
        hub.publish("job.progress", {"id": "j1", "progress": шаг / 50})
    сразу = [m for m in hub.history if m["type"] == "job.progress"]
    assert len(сразу) == 1, f"прогресс не прорежен: {len(сразу)} событий"
    time.sleep(0.4)
    прогресс = [m["progress"] for m in hub.history if m["type"] == "job.progress"]
    assert прогресс[-1] == pytest.approx(49 / 50), "последнее значение потерялось"
    # «Готово» отменяет придержанный прогресс: «95 %» после «готово» — это откат.
    hub.publish("job.progress", {"id": "j2", "progress": 0.1})
    hub.publish("job.progress", {"id": "j2", "progress": 0.95})
    hub.publish("job.completed", {"id": "j2"})
    time.sleep(0.4)
    после = [m["type"] for m in hub.history if m.get("id") == "j2"]
    assert после == ["job.progress", "job.completed"], после


def test_history_is_a_bounded_deque():
    from asrhub.api import event_hub as модуль

    hub = модуль.EventHub()
    for номер in range(модуль.ИСТОРИЯ_СОБЫТИЙ + 50):
        hub.publish("queue_paused", {"n": номер})
    assert len(hub.history) == модуль.ИСТОРИЯ_СОБЫТИЙ
    assert hub.history[-1]["n"] == модуль.ИСТОРИЯ_СОБЫТИЙ + 49


def test_user_event_feed_is_selected_in_sql(database):
    """Лента обычного ключа поднимала две тысячи его заданий на каждый опрос."""
    своё = database.create_job({"filename": "а.wav", "owner": "алиса"})
    чужое = database.create_job({"filename": "б.wav", "owner": "боб"})
    database.add_event(своё, "started", "своё")
    database.add_event(чужое, "started", "чужое")
    database.add_event(None, "queue_paused", "пауза")
    database.add_event(None, "key_created", "ключ")
    лента = {e["message"] for e in database.get_events_for(
        "алиса", public_kinds=("queue_paused",))}
    assert {"своё", "пауза"} <= лента
    assert not лента & {"чужое", "ключ", "Задание создано: б.wav"}, лента
    группа = {e["message"] for e in database.get_events_for(["алиса", "боб"])}
    assert {"своё", "чужое"} <= группа and not группа & {"пауза", "ключ"}


def test_events_route_uses_the_owner_filter(keys_client):
    ключ = keys_client.post("/api/keys", json={"name": "оператор", "role": "user"}).json()["key"]
    ответ = keys_client.get("/api/events", headers={"X-API-Key": ключ})
    assert ответ.status_code == 200
    assert all(e.get("kind") in ("queue_paused", "queue_resumed", "queue_cleared")
               or e.get("job_id") for e in ответ.json()["items"])


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

def test_dead_webhook_host_is_paused_whole():
    """Один мёртвый приёмник занимал весь пул доставки."""
    from asrhub import job_queue as jq

    queue = jq.JobQueue.__new__(jq.JobQueue)
    узел = "мёртвый.example:8080"
    for _ in range(jq.JobQueue.ОТКАЗОВ_ДО_ПАУЗЫ_УЗЛА - 1):
        queue._узел_ответил(узел, False)
    assert not queue._узел_на_паузе(узел)
    queue._узел_ответил(узел, False)
    assert queue._узел_на_паузе(узел) > time.time()
    assert not queue._узел_на_паузе("живой.example")
    queue._узел_ответил(узел, True)
    assert not queue._узел_на_паузе(узел)


def test_paused_host_postpones_without_spending_an_attempt(database):
    from asrhub import job_queue as jq

    class Настройки(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    queue = jq.JobQueue.__new__(jq.JobQueue)
    queue.db = database
    queue.settings = Настройки(webhook_allow_internal=True)
    import threading

    queue._stop = threading.Event()
    задание = database.create_job({"filename": "а.wav", "webhook_url": "http://10.1.1.1/hook",
                                   "webhook_status": "sending", "webhook_attempts": 1})
    for _ in range(jq.JobQueue.ОТКАЗОВ_ДО_ПАУЗЫ_УЗЛА):
        queue._узел_ответил("10.1.1.1", False)
    queue._deliver_webhook(database.get_job(задание), b"{}")
    строка = database.get_job(задание)
    assert строка["webhook_status"] == "pending"
    assert int(строка["webhook_attempts"]) == 1, "пауза узла потратила попытку"
    assert float(строка["webhook_next_at"]) > time.time() + 30


def test_webhook_due_oldest_first(database):
    поздно = database.create_job({"filename": "а.wav", "webhook_status": "pending",
                                  "webhook_next_at": time.time() - 10})
    рано = database.create_job({"filename": "б.wav", "webhook_status": "pending",
                                "webhook_next_at": time.time() - 1000})
    assert database.webhook_due(limit=5)[:2] == [рано, поздно]


# ---------------------------------------------------------------------------
# Обслуживание
# ---------------------------------------------------------------------------

def test_optimize_keeps_the_crm_index(database):
    """Полный ANALYZE на перекошенных данных уводил `crm_waiting` в полный проход."""
    with database.write() as conn:
        conn.executemany(
            "INSERT INTO jobs (id, filename, status, created_at, updated_at, crm_status) "
            "VALUES (?,?,?,?,?,?)",
            [(f"j{н}", "а.wav", "completed", time.time(), time.time(),
              None if н % 500 else "ждёт модель") for н in range(5000)])
    assert database.optimize()
    план = " ".join(str(tuple(с)) for с in database.query(
        "EXPLAIN QUERY PLAN SELECT j.id FROM jobs j LEFT JOIN llm_queue q ON q.job_id=j.id "
        "WHERE j.crm_status=? ORDER BY j.crm_at LIMIT 50", ("ждёт модель",)))
    assert "idx_jobs_crm" in план, план


def test_daily_maintenance_runs_optimize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub import maintenance
    from asrhub.config import load
    from asrhub.db import Database

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_BACKUP_ENABLED", "false")
    monkeypatch.setenv("ASRHUB_REVIEW_ENABLED", "false")
    настройки = load()
    база = Database(tmp_path / "data" / "asrhub.db")
    вызвано = []
    monkeypatch.setattr(база, "optimize", lambda: вызвано.append(1) or True)
    # Первый заход только ставит отметку — суточная уборка «была» сутки назад.
    база.set_kv(maintenance.KV_SWEEP, time.time() - 2 * 86400)
    try:
        сделано = maintenance.run_scheduled(база, настройки, analytics=None)
    finally:
        база.close()
    assert вызвано and сделано.get("optimize") is True, сделано


def test_submit_does_not_probe_twice(data_dir: Path, sample_wav: Path,
                                    monkeypatch: pytest.MonkeyPatch):
    """Загрузка меряла длительность для квоты, а постановка — ещё раз."""
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.pipeline import audio
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")

    вызовы = []
    настоящая = audio.probe

    def считать(path, *a, **k):
        вызовы.append(str(path))
        return настоящая(path, *a, **k)

    monkeypatch.setattr(audio, "probe", считать)
    # Очередь не запущена: обработка звука сама меряет файл, и это не тот случай.
    with TestClient(create_app(load(), start_queue=False)) as client, \
            sample_wav.open("rb") as файл:
        ответ = client.post("/api/jobs", files={"file": ("один.wav", файл, "audio/wav")})
    assert ответ.status_code in (200, 201, 202), ответ.text
    assert len(вызовы) == 1, f"ffprobe на одну загрузку: {len(вызовы)}"
    assert ответ.json().get("media_duration_s", 0) > 0


def test_orphan_sweep_walks_files_not_jobs(tmp_path: Path, database):
    """Уборка читала все задания и делала resolve() на каждый путь."""
    import os

    from asrhub import maintenance

    загрузки = tmp_path / "uploads"
    результаты = tmp_path / "results"
    загрузки.mkdir()
    результаты.mkdir()
    своё = загрузки / "своё_1.wav"
    своё.write_bytes(b"x")
    через_ссылку = tmp_path / "ссылка"
    через_ссылку.symlink_to(загрузки)
    ещё = загрузки / "ещё.wav"
    ещё.write_bytes(b"x")
    сирота = загрузки / "сирота.wav"
    сирота.write_bytes(b"x")
    database.create_job({"filename": "а.wav", "file_path": str(своё)})
    # Путь в базе записан через ссылку — файл всё равно свой.
    database.create_job({"filename": "б.wav", "file_path": str(через_ссылку / "ещё.wav")})
    готовое = database.create_job({"filename": "в.wav"})
    (результаты / готовое).mkdir()
    (результаты / "чужой-каталог").mkdir()
    давно = time.time() - 3 * 86400
    for путь in (своё, ещё, сирота, результаты / готовое, результаты / "чужой-каталог"):
        os.utime(путь, (давно, давно))

    class Настройки:
        paths = type("P", (), {"uploads": загрузки, "results": результаты,
                               "data": tmp_path})()

    итог = maintenance.подмести_файлы(database, Настройки())
    assert итог["moved"] == 2, итог
    assert своё.exists() and ещё.exists() and (результаты / готовое).exists()
    assert not сирота.exists() and not (результаты / "чужой-каталог").exists()


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

def test_metrics_key_reads_metrics_and_nothing_else(keys_client):
    """Закрыть метрики значило выдать сборщику readonly — а он читает расшифровки."""
    ответ = keys_client.post("/api/keys", json={"name": "prometheus", "role": "metrics"})
    assert ответ.status_code == 200, ответ.text
    ключ = ответ.json()["key"]
    keys_client.put("/api/settings", json={"monitoring_public": False})
    чужой = {"X-API-Key": ключ}
    assert keys_client.get("/api/metrics", headers=чужой).status_code == 200
    assert keys_client.get("/api/monitoring/metrics", headers=чужой).status_code == 200
    assert keys_client.get("/api/jobs", headers=чужой).status_code == 403
    assert keys_client.get("/api/settings", headers=чужой).status_code == 403
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect), \
            keys_client.websocket_connect(f"/ws?api_key={ключ}") as сокет:
        сокет.receive_text()


def test_metrics_network_whitelist(keys_client):
    keys_client.put("/api/settings", json={"monitoring_public": True,
                                           "metrics_allowed_networks": "10.99.0.0/16"})
    # TestClient приходит с адреса «testclient» — не из списка.
    assert keys_client.get("/api/metrics").status_code == 403
    assert keys_client.get("/api/monitoring/metrics").status_code == 403
    keys_client.put("/api/settings", json={"metrics_allowed_networks": ""})
    assert keys_client.get("/api/metrics").status_code == 200
    ответ = keys_client.put("/api/settings", json={"metrics_allowed_networks": "не сеть"})
    assert ответ.status_code == 400


def test_whitelist_matches_by_network():
    from asrhub.api import deps

    class Запрос:
        def __init__(self, адрес):
            self.headers = {}
            self.client = type("C", (), {"host": адрес})()
            self.app = type("A", (), {"state": type("S", (), {})()})()

    class Состояние:
        settings = {"metrics_allowed_networks": "10.99.0.0/16, 127.0.0.1",
                    "trusted_proxies": ""}

    import asrhub.api.deps as модуль

    исходный = модуль.get_state
    модуль.get_state = lambda request: Состояние()
    try:
        assert deps.сеть_метрик_разрешена(Запрос("10.99.4.5"))
        assert deps.сеть_метрик_разрешена(Запрос("127.0.0.1"))
        assert not deps.сеть_метрик_разрешена(Запрос("10.98.0.1"))
    finally:
        модуль.get_state = исходный


# ---------------------------------------------------------------------------
# Журнал доступа
# ---------------------------------------------------------------------------

def test_audit_hash_chain_detects_edits_and_cuts(database):
    for номер in range(5):
        database.audit_add(action=f"действие {номер}", actor="админ", status=200)
    итог = database.audit_verify()
    assert итог["ok"] and итог["checked"] == 5, итог
    строки = [dict(с) for с in database.query("SELECT id FROM audit ORDER BY id")]
    database.execute("UPDATE audit SET action='подменено' WHERE id=?", (строки[2]["id"],))
    итог = database.audit_verify()
    assert not итог["ok"] and итог["broken_id"] == строки[2]["id"], итог


def test_audit_cut_in_the_middle_is_detected(database):
    for номер in range(5):
        database.audit_add(action=f"действие {номер}", actor="админ", status=200)
    строки = [dict(с) for с in database.query("SELECT id FROM audit ORDER BY id")]
    database.execute("DELETE FROM audit WHERE id=?", (строки[2]["id"],))
    итог = database.audit_verify()
    assert not итог["ok"] and итог["broken_id"] == строки[3]["id"], итог
    # Уборка по сроку отрезает начало — это не разрыв: первая строка — опора.
    database.execute("DELETE FROM audit WHERE id<=?", (строки[2]["id"],))
    assert database.audit_verify()["ok"]


def test_audit_verify_route(keys_client):
    keys_client.post("/api/keys", json={"name": "для журнала", "role": "user"})
    ответ = keys_client.get("/api/audit/verify")
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["ok"] is True and ответ.json()["checked"] >= 1


def test_audit_rows_are_copied_to_syslog(database):
    from asrhub import audit

    приём = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    приём.bind(("127.0.0.1", 0))
    приём.settimeout(5)
    порт = приём.getsockname()[1]
    database.audit_sink = audit.ПриёмникSyslog({"audit_syslog": f"udp://127.0.0.1:{порт}"})
    database.audit_add(action="удалил запись", actor="админ", status=200, ip="10.0.0.7")
    данные = приём.recv(65535).decode("utf-8")
    приём.close()
    assert "asrhub-audit" in данные and "удалил запись" in данные
    тело = json.loads(данные.split("asrhub-audit: ", 1)[1].rstrip("\x00"))
    assert тело["hash"] and тело["ip"] == "10.0.0.7"


def test_syslog_address_is_validated():
    from asrhub.catalog import validate_all

    assert validate_all({"audit_syslog": "udp://10.0.0.1:514"}, server=True) == []
    assert validate_all({"audit_syslog": "/dev/log"}, server=True) == []
    assert validate_all({"audit_syslog": "куда-нибудь"}, server=True)


# ---------------------------------------------------------------------------
# Копии
# ---------------------------------------------------------------------------

def _настройки_копий(tmp_path: Path, **ещё: Any) -> Any:
    class Настройки(dict):
        config_file = None
        paths = type("P", (), {"uploads": tmp_path / "uploads",
                               "results": tmp_path / "results",
                               "data": tmp_path, "db": tmp_path / "asrhub.db"})()

        def get(self, key, default=None):
            return super().get(key, default)

    return Настройки(backup_dir=str(tmp_path / "копии"), **ещё)


def test_backup_can_carry_the_recordings(tmp_path: Path, database):
    import tarfile

    from asrhub import backup

    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "звонок.wav").write_bytes(b"RIFF" + b"0" * 100)
    без = backup.создать(database, _настройки_копий(tmp_path), kind="full")
    с = backup.создать(database, _настройки_копий(tmp_path, backup_include_uploads=True),
                       kind="full")
    место = tmp_path / "копии"
    with tarfile.open(место / без["name"]) as архив:
        assert not any(и.startswith("uploads/") for и in архив.getnames())
    with tarfile.open(место / с["name"]) as архив:
        assert "uploads/звонок.wav" in архив.getnames()
    assert backup.свод(_настройки_копий(tmp_path))["include_uploads"] is False


def test_backup_encrypt_roundtrip(tmp_path: Path):
    from asrhub import backup

    исходный = tmp_path / "копия.asrhub.tar.gz"
    исходный.write_bytes(b"\x1f\x8b" + "данные".encode() * 100)
    шифр = backup.зашифровать(исходный, "длинный-пароль")
    assert backup.зашифрована(шифр) and not backup.зашифрована(исходный)
    assert b"\xd0\xb4" not in шифр.read_bytes()[16:200]
    обратно = backup.расшифровать(шифр, "длинный-пароль", tmp_path / "обратно")
    assert обратно.read_bytes() == исходный.read_bytes()
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError):
        backup.расшифровать(шифр, "не тот", tmp_path / "мимо")


def test_post_backup_command_exports_encrypted_copy(tmp_path: Path, database):
    from asrhub import backup

    наружу = tmp_path / "наружу"
    наружу.mkdir()
    настройки = _настройки_копий(
        tmp_path, backup_password="длинный-пароль",
        backup_post_command=f"cp {{file}} {наружу}/")
    копия = backup.создать(database, настройки, kind="settings")
    # Вынос идёт в фоне — дожидаемся его отметки.
    for _ in range(100):
        if database.get_kv(backup.KV_ВЫНОС):
            break
        time.sleep(0.05)
    итог = database.get_kv(backup.KV_ВЫНОС)
    assert итог and итог["ok"], итог
    вынесено = list(наружу.iterdir())
    assert len(вынесено) == 1 and вынесено[0].name == копия["name"] + ".enc"
    assert backup.зашифрована(вынесено[0])
    # Зашифрованный файл рядом с копией после выноса не остаётся.
    assert not (tmp_path / "копии" / (копия["name"] + ".enc")).exists()
    # И принимается обратно — расшифровывается тем же паролем.
    with вынесено[0].open("rb") as поток:
        принято = backup.принять(настройки, вынесено[0].name, поток)
    assert принято["kind"] == "settings"


def test_post_backup_command_is_not_settable_from_the_ui(keys_client):
    ответ = keys_client.put("/api/settings", json={"backup_post_command": "rm -rf /"})
    assert ответ.status_code == 400
    assert "файле настроек" in ответ.json()["message"]
    # Пароль копий — секрет: в настройках не отдаётся и администратору.
    keys_client.put("/api/settings", json={"backup_password": "длинный-пароль"})
    значение = keys_client.get("/api/settings").json()
    плоско = json.dumps(значение, ensure_ascii=False)
    assert "длинный-пароль" not in плоско


# ---------------------------------------------------------------------------
# Сторож зависшего движка
# ---------------------------------------------------------------------------

def test_hung_engine_is_taken_off_and_the_worker_replaced(data_dir: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    """Движок, зависший внутри библиотеки, держал слот и задание вечно."""
    import threading

    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient
    from test_review_54_queue import _загрузка, _поставить, ждать, подмена

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "1")
    отпустить = threading.Event()
    обычная = подмена()

    def зависающая(source, settings, registry, **k):
        if "висит" in str(source):
            отпустить.wait(30)
        return обычная(source, settings, registry, **k)

    monkeypatch.setattr("asrhub.job_queue_worker.process_job", зависающая)
    with TestClient(create_app(load(), start_queue=False)) as клиент:
        state = клиент.app.state.hub
        queue = state.queue
        queue.start()
        висит = _поставить(state, _загрузка(state, "висит.wav"))
        assert ждать(lambda: any(с.job_id == висит["id"] for с in queue._states.values()), 20)
        прежний = queue._потоки[0]
        состояние = queue._states[0]
        # Вышел предел задания, и движок давно молчит.
        состояние.срок_с = 1.0
        состояние.started_at -= 1000
        состояние.доклад -= 1000
        assert queue._сторож_зависших() == 1
        строка = state.db.get_job(висит["id"])
        assert строка["status"] == "failed" and строка["error_code"] == "engine_hung"
        assert queue._потоки[0] is not прежний, "поток воркера не заменён"
        # Очередь работает дальше, не дожидаясь зависшего.
        следом = _поставить(state, _загрузка(state, "следом.wav", 1.3))
        assert ждать(lambda: state.db.get_job(следом["id"])["status"] == "completed", 30)
        # Зависший поток вернулся — он молча уходит и ничего не портит.
        отпустить.set()
        assert ждать(lambda: not прежний.is_alive(), 10)
        assert state.db.get_job(висит["id"])["status"] == "failed"
        assert queue._потоки[0].is_alive()


def test_engine_hung_is_documented():
    from asrhub.errors import EngineHung

    assert EngineHung(60).code == "engine_hung"
    assert "`engine_hung`" in (КОРЕНЬ / "docs" / "08-api.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Диалплан: предохранитель, отвергнутая запись, дорожки, метка, «не записывать»
# ---------------------------------------------------------------------------

from test_dialplan import wav, живой, запустить, латиница  # noqa: E402,F401


def test_script_circuit_breaker_parks_calls_while_server_is_down(tmp_path: Path):
    """Каждый отбой при лежащем сервере запускал sh на минуты повторов."""
    with socket.socket() as занятый:
        занятый.bind(("127.0.0.1", 0))
        порт = занятый.getsockname()[1]
    адрес = f"http://127.0.0.1:{порт}"
    первый = запустить(tmp_path, адрес, "uniqueid=1793000000.70", "billsec=30")
    assert первый.returncode != 0
    assert (tmp_path / "spool" / ".down_until").is_file(), "предохранитель не взведён"
    второй = запустить(tmp_path, адрес, "uniqueid=1793000000.71", "billsec=30")
    assert второй.returncode == 0
    журнал = (tmp_path / "send.log").read_text(encoding="utf-8")
    assert "предохранитель" in журнал
    assert (tmp_path / "spool" / "pending" ).glob("1793000000.71.*.call")
    assert len(list((tmp_path / "spool" / "pending").glob("*.call"))) == 2


def test_script_resend_lifts_the_breaker(живой, tmp_path: Path):  # noqa: F811
    адрес, hub, каталог = живой
    (каталог / "spool" / "pending").mkdir(parents=True)
    (каталог / "spool" / ".down_until").write_text(str(int(time.time()) + 600))
    (каталог / "spool" / "pending" / "1793000000.72.1.call").write_text(
        "uniqueid=1793000000.72\nbillsec=30\ndisposition=ANSWERED\n", encoding="utf-8")
    итог = запустить(каталог, адрес, "--resend")
    assert итог.returncode == 0, итог.stderr
    assert not (каталог / "spool" / ".down_until").exists()
    assert hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.72'")


def test_rejected_recording_is_marked_and_kept_in_failed(живой):  # noqa: F811
    """413 на запись: звонок уходит без неё с пометкой, копия — в failed/."""
    адрес, hub, каталог = живой
    запись = каталог / "monitor" / "asrhub" / "1793000000.73.wav"
    запись.write_bytes(wav(80))
    hub.settings.set("max_upload_mb", 1)
    итог = запустить(каталог, адрес, "uniqueid=1793000000.73", "billsec=80",
                     "disposition=ANSWERED", f"file={запись}")
    assert итог.returncode == 0, итог.stderr
    звонок = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.73'")
    assert звонок is not None and not звонок["job_id"]
    assert str(звонок["skipped"]).startswith("сервер не принял запись (код 413)"), dict(звонок)
    копии = list((каталог / "spool" / "failed").glob("*.call"))
    assert len(копии) == 1 and "rejected=" not in копии[0].read_text(encoding="utf-8")
    # Предел подняли — --resend-failed досылает звонок с записью.
    hub.settings.set("max_upload_mb", 2048)
    итог = запустить(каталог, адрес, "--resend-failed")
    assert итог.returncode == 0, итог.stderr
    звонок = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.73'")
    assert звонок["job_id"], dict(звонок)


def test_two_tracks_become_one_stereo_recording(живой):  # noqa: F811
    """MixMonitor r()/t(): клиент и оператор отдельно — сервер сводит в стерео."""
    from asrhub.pipeline.audio import probe

    адрес, hub, каталог = живой
    клиент = каталог / "monitor" / "asrhub" / "1793000000.74-in.wav"
    оператор = каталог / "monitor" / "asrhub" / "1793000000.74-out.wav"
    клиент.write_bytes(wav(6))
    оператор.write_bytes(wav(7))
    итог = запустить(каталог, адрес, "uniqueid=1793000000.74", "billsec=7",
                     "disposition=ANSWERED", f"file_in={клиент}", f"file_out={оператор}",
                     "tag=CRM-42")
    assert итог.returncode == 0, итог.stderr
    звонок = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.74'")
    assert звонок["job_id"], (звонок["skipped"], (каталог / "send.log").read_text("utf-8")[-600:])
    задание = hub.db.get_job(звонок["job_id"])
    assert probe(Path(задание["file_path"])).channels == 2
    assert "CRM-42" in str(задание["tags"])


def test_record_zero_keeps_the_call_without_recording(живой):  # noqa: F811
    адрес, hub, каталог = живой
    запись = каталог / "monitor" / "asrhub" / "1793000000.75.wav"
    запись.write_bytes(wav(6))
    итог = запустить(каталог, адрес, "uniqueid=1793000000.75", "billsec=30",
                     "disposition=ANSWERED", "record=0", f"file={запись}")
    assert итог.returncode == 0, итог.stderr
    звонок = hub.db.query_one("SELECT * FROM calls WHERE uniqueid='main:1793000000.75'")
    assert звонок is not None and not звонок["job_id"]
    assert str(звонок["skipped"]).startswith("не записывать"), dict(звонок)
    assert not звонок["recording"]


# ---------------------------------------------------------------------------
# AMI: вход по MD5, TLS, ActionID, фоновое чтение
# ---------------------------------------------------------------------------

class _ПоддельнаяAMI:
    """Подменная станция: Challenge/MD5, эхо ActionID, события по команде."""

    def __init__(self, *, пароль: str = "секрет", tls_контекст: Any = None,
                 запоздалый: bool = False):
        import threading

        self.пароль = пароль
        self.слушатель = socket.socket()
        self.слушатель.bind(("127.0.0.1", 0))
        self.слушатель.listen(1)
        self.порт = self.слушатель.getsockname()[1]
        self.tls = tls_контекст
        self.запоздалый = запоздалый
        self.входы: list[dict[str, str]] = []
        self.соединение: Any = None
        self.готов = threading.Event()
        threading.Thread(target=self._обслужить, daemon=True).start()

    def отправить(self, текст: str) -> None:
        self.соединение.sendall(текст.encode("utf-8"))

    def _обслужить(self) -> None:
        import hashlib

        сокет, _ = self.слушатель.accept()
        if self.tls is not None:
            try:
                сокет = self.tls.wrap_socket(сокет, server_side=True)
            except OSError:
                сокет.close()                 # клиент не принял сертификат
                return
        self.соединение = сокет
        self.готов.set()
        сокет.sendall(b"Asterisk Call Manager/7.0.3\r\n")
        буфер = b""
        вызов = "1234567890"
        while True:
            try:
                кусок = сокет.recv(4096)
            except OSError:
                return
            if not кусок:
                return
            буфер += кусок
            while b"\r\n\r\n" in буфер:
                пакет, буфер = буфер.split(b"\r\n\r\n", 1)
                поля = dict(строка.split(": ", 1) for строка in
                            пакет.decode("utf-8").split("\r\n") if ": " in строка)
                номер = поля.get("ActionID", "")
                действие = поля.get("Action", "").lower()
                if действие == "challenge":
                    сокет.sendall(f"Response: Success\r\nActionID: {номер}\r\n"
                                  f"Challenge: {вызов}\r\n\r\n".encode())
                elif действие == "login":
                    self.входы.append(поля)
                    верно = (поля.get("Key") == hashlib.md5(
                        (вызов + self.пароль).encode()).hexdigest()
                        if поля.get("AuthType") == "MD5" else поля.get("Secret") == self.пароль)
                    ответ = "Success" if верно else "Error"
                    сокет.sendall(f"Response: {ответ}\r\nActionID: {номер}\r\n"
                                  "Message: Authentication\r\n\r\n".encode())
                elif действие == "ping":
                    if self.запоздалый:
                        # Сначала запоздалый ответ на чужое действие.
                        сокет.sendall(b"Response: Success\r\nActionID: old-1\r\nPing: Pong\r\n\r\n")
                    сокет.sendall(f"Response: Success\r\nActionID: {номер}\r\nPing: Pong\r\n"
                                  f"Timestamp: 2\r\n\r\n".encode())
                elif действие == "logoff":
                    return


def test_ami_md5_login_sends_no_password():
    from asrhub.telephony.asterisk import AMIClient

    станция = _ПоддельнаяAMI()
    клиент = AMIClient("127.0.0.1", станция.порт, "asrhub", "секрет", auth="md5",
                       события="off", timeout=5)
    клиент.connect()
    клиент.close()
    assert станция.входы and станция.входы[0].get("AuthType") == "MD5"
    assert "Secret" not in станция.входы[0], "пароль ушёл по сети открытым текстом"


def test_ami_ignores_a_late_answer_to_another_action():
    from asrhub.telephony.asterisk import AMIClient

    станция = _ПоддельнаяAMI(запоздалый=True)
    клиент = AMIClient("127.0.0.1", станция.порт, "asrhub", "секрет", события="off", timeout=5)
    клиент.connect()
    ответ = клиент.ping()
    клиент.close()
    assert ответ.get("Timestamp") == "2", f"принят чужой ответ: {ответ}"


def test_ami_reader_thread_keeps_the_socket_drained():
    from asrhub.telephony.asterisk import AMIClient

    станция = _ПоддельнаяAMI()
    клиент = AMIClient("127.0.0.1", станция.порт, "asrhub", "секрет", timeout=5)
    клиент.connect()
    клиент.читать_в_фоне()
    for номер in range(50):
        станция.отправить(f"Event: Cdr\r\nUniqueID: 1793000000.{номер}\r\n\r\n")
    assert клиент.ping().get("Ping") == "Pong"
    события = []
    while True:
        событие = клиент.пакет(timeout=0.5)
        if not событие:
            break
        события.append(событие["UniqueID"])
    клиент.close()
    assert len(события) == 50 and события[0] == "1793000000.0"


def test_ami_over_tls_with_own_ca(tmp_path: Path):
    import ssl
    import subprocess

    from asrhub.telephony.asterisk import AMIClient

    ключ, сертификат = tmp_path / "pbx.key", tmp_path / "pbx.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1",
                    "-keyout", str(ключ), "-out", str(сертификат)],
                   check=True, capture_output=True, timeout=60)
    контекст = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    контекст.load_cert_chain(str(сертификат), str(ключ))
    станция = _ПоддельнаяAMI(tls_контекст=контекст)
    клиент = AMIClient("127.0.0.1", станция.порт, "asrhub", "секрет", tls=True,
                       ca_file=str(сертификат), события="off", timeout=5)
    assert "Asterisk" in клиент.connect()
    клиент.close()
    # Без своего центра сертификат станции не принимается.
    from asrhub.telephony.asterisk import AMIError

    чужая = _ПоддельнаяAMI(tls_контекст=контекст)
    with pytest.raises(AMIError):
        AMIClient("127.0.0.1", чужая.порт, "asrhub", "секрет", tls=True,
                  события="off", timeout=5).connect()


def test_station_tls_fields_are_validated():
    from asrhub.telephony.stations import проверить_набор

    assert проверить_набор([{"name": "А", "source": "ami", "username": "u",
                             "auth": "md5", "tls": True, "ca_file": "/etc/ca.pem"}]) == []
    ошибки = проверить_набор([{"name": "А", "source": "ami", "username": "u",
                               "auth": "ntlm", "ca_file": "ca.pem"}])
    assert len(ошибки) == 2, ошибки


# ---------------------------------------------------------------------------
# Агент: версия, «устарел», отпечаток кода, самообновление со сверкой
# ---------------------------------------------------------------------------

from test_agent_api import здравствуй  # noqa: E402
from test_agent_api import сервер as агентский_сервер  # noqa: E402,F401


def test_hello_tells_an_old_agent_it_is_outdated(агентский_сервер):  # noqa: F811
    import hashlib

    ответ = здравствуй(агентский_сервер, version="3.0.1")
    assert ответ["agent_outdated"] is True
    код = агентский_сервер.get("/api/telephony/agent/asrhub-agent.py",
                               headers={"X-API-Key": "ah_agent"})
    assert ответ["agent_sha256"] == hashlib.sha256(код.content).hexdigest()
    assert код.headers["X-ASRHub-SHA256"] == ответ["agent_sha256"]
    свежий = здравствуй(агентский_сервер, version=ответ["agent_version"], host="pbx2")
    assert свежий["agent_outdated"] is False
    список = агентский_сервер.get("/api/telephony/agents",
                                  headers={"X-API-Key": "ah_admin"}).json()
    assert {а["host"]: а["outdated"] for а in список["agents"]} == {"pbx1.local": True,
                                                                     "pbx2": False}
    установщик = агентский_сервер.get("/api/telephony/agent/install.sh",
                                      headers={"X-API-Key": "ah_agent"})
    assert установщик.headers["X-ASRHub-SHA256"] == hashlib.sha256(
        установщик.content).hexdigest()


def test_agent_updates_itself_only_with_a_matching_sha256(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    import hashlib

    from test_agent import агент

    свой = tmp_path / "asrhub-agent.py"
    свой.write_text("# прежний агент\n", encoding="utf-8")
    новый = (КОРЕНЬ / "agent" / "asrhub-agent.py").read_bytes()
    отпечаток = hashlib.sha256(новый).hexdigest()

    class Клиент:
        def __init__(self, тело, заголовок):
            self.тело, self.заголовок = тело, заголовок

        def скачать(self, путь, таймаут=None):
            return self.тело, {"X-ASRHub-SHA256": self.заголовок}

    class Перезапуск(Exception):
        pass

    def execv(*_):
        raise Перезапуск

    monkeypatch.setattr(агент.sys, "argv", [str(свой)])
    monkeypatch.setattr(агент.os, "execv", execv)
    # Подменённый по дороге файл не ставится.
    with pytest.raises(RuntimeError):
        агент.обновить_себя(Клиент(новый + b"\n#", отпечаток), отпечаток)
    assert свой.read_text(encoding="utf-8") == "# прежний агент\n"
    # Без отпечатка от сервера — тоже нет.
    with pytest.raises(RuntimeError):
        агент.обновить_себя(Клиент(новый, ""), "")
    # Совпало — файл заменён, процесс перезапускается.
    with pytest.raises(Перезапуск):
        агент.обновить_себя(Клиент(новый, отпечаток), отпечаток, "9.9.9")
    assert свой.read_bytes() == новый


def test_installer_checks_the_sha256_header():
    текст = (КОРЕНЬ / "agent" / "install.sh").read_text(encoding="utf-8")
    assert "verify_sha256" in текст and "X-ASRHub-SHA256".lower() in текст.lower()
    assert "-D \"${dest}.headers\"" in текст


# ---------------------------------------------------------------------------
# Установка рядом с Asterisk
# ---------------------------------------------------------------------------

def test_service_unit_gets_the_asterisk_group(tmp_path: Path):
    import shutil
    import subprocess

    итог = subprocess.run(
        [shutil.which("bash") or "bash", str(КОРЕНЬ / "scripts" / "service.sh"), "install",
         "--dry-run", "--prefix", str(tmp_path / "opt"), "--data", str(tmp_path / "data"),
         "--user", "asrhub", "--group", "root"],
        capture_output=True, text=True, timeout=60, cwd=str(КОРЕНЬ))
    assert "SupplementaryGroups=root" in итог.stdout, итог.stdout[-800:] + итог.stderr[-800:]
    без = subprocess.run(
        [shutil.which("bash") or "bash", str(КОРЕНЬ / "scripts" / "service.sh"), "install",
         "--dry-run", "--prefix", str(tmp_path / "opt"), "--data", str(tmp_path / "data"),
         "--user", "asrhub"],
        capture_output=True, text=True, timeout=60, cwd=str(КОРЕНЬ))
    assert "SupplementaryGroups" not in без.stdout


def test_doctor_checks_station_paths():
    текст = (КОРЕНЬ / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert "readable_by_service" in текст and "telephony_stations" in текст
    assert "install --group" in текст
    # Журнал очередей читается тем же пользователем службы, что и CDR.
    assert '("cdr_file", "recordings_dir", "queue_log")' in текст


def test_qc_page_gives_manager_report_only():
    """Сервер отдаёт руководителю отчёт своего подразделения, а страница
    раздела не-администратору показывала только «смотрит администратор»."""
    app = (КОРЕНЬ / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    начало = app.index("RENDERERS.qc = {")
    кусок = app[начало:начало + 4000]
    assert "this._толькоОтчёт" in кусок and "настраивает и смотрит администратор" not in кусок
    отчёт = app[app.index("  async loadReport() {", начало):][:1200]
    # Список «не звонить» — только администратору: руководителю его не запрашиваем.
    assert "this._толькоОтчёт ? Promise.resolve(null) : API.get('/api/qc/stoplist')" in отчёт


def test_recordings_walk_skips_old_leaf_directories(tmp_path: Path):
    """Каталог записей обходился целиком на каждом заходе."""
    import os

    from asrhub.telephony.importer import обойти_свежее

    давно = time.time() - 30 * 86400
    старый = tmp_path / "2026" / "10" / "01"
    свежий = tmp_path / "2026" / "11" / "12"
    for каталог, имя in ((старый, "a.wav"), (свежий, "b.wav")):
        каталог.mkdir(parents=True)
        (каталог / имя).write_bytes(b"x")
    os.utime(старый / "a.wav", (давно, давно))
    os.utime(старый, (давно, давно))
    найдено = [п.name for п in обойти_свежее(tmp_path, time.time() - 7 * 86400)]
    assert найдено == ["b.wav"], найдено
    # Полный сбор (окно 0) видит всё.
    assert sorted(п.name for п in обойти_свежее(tmp_path, 0)) == ["a.wav", "b.wav"]


# ---------------------------------------------------------------------------
# Суточная сверка «станция ↔ архив»
# ---------------------------------------------------------------------------

def test_reconcile_finds_calls_lost_on_the_way(tmp_path: Path, database):
    import datetime as dt

    from asrhub.telephony import reconcile
    from asrhub.telephony.stations import Станция

    сейчас = time.time()
    начало, конец, день = reconcile.окно_вчера(сейчас)
    журнал = tmp_path / "Master.csv"
    строки = []
    for номер in range(10):
        момент = dt.datetime.fromtimestamp(начало + 3600 + номер * 60)
        старт = момент.strftime("%Y-%m-%d %H:%M:%S")
        строки.append(
            f'"","7916000000{номер}","101","from-trunk","","PJSIP/t-0001","PJSIP/101-0002",'
            f'"Dial","PJSIP/101","{старт}","{старт}","{старт}",30,25,"ANSWERED",'
            f'"DOCUMENTATION","1793000000.{номер}",""')
    журнал.write_text("\n".join(строки) + "\n", encoding="utf-8")
    станция = Станция(id="main", name="Головная", source="cdr_csv", cdr_file=str(журнал))
    # В архиве — только три из десяти.
    for номер in range(3):
        database.save_call(f"main:1793000000.{номер}", station="main",
                           started_at=начало + 3600 + номер * 60, answered=1)
    итог = reconcile.сверить_станцию(database, станция, начало, конец, день)
    assert итог["station_total"] == 10 and итог["archive_total"] == 3
    assert итог["missing"] == 7 and итог["ok"] is False and "потеряны" in итог["summary"]
    # Всё на месте — сошлось.
    for номер in range(3, 10):
        database.save_call(f"main:1793000000.{номер}", station="main",
                           started_at=начало + 3600 + номер * 60, answered=1)
    assert reconcile.сверить_станцию(database, станция, начало, конец, день)["ok"] is True


def test_reconcile_counts_recording_files_for_dialplan(tmp_path: Path, database):
    import os

    from asrhub.telephony import reconcile
    from asrhub.telephony.stations import Станция

    начало, конец, день = reconcile.окно_вчера()
    каталог = tmp_path / "monitor"
    каталог.mkdir()
    for номер in range(6):
        файл = каталог / f"1793000000.{номер}.wav"
        файл.write_bytes(b"RIFF")
        os.utime(файл, (начало + 600, начало + 600))
    database.save_call("dp:1793000000.0", station="dp", started_at=начало + 500,
                       recording="x.wav")
    станция = Станция(id="dp", name="Диалплан", source="dialplan",
                      recordings_dir=str(каталог))
    итог = reconcile.сверить_станцию(database, станция, начало, конец, день)
    assert итог["station_files"] == 6 and итог["missing"] == 5 and итог["ok"] is False


# ---------------------------------------------------------------------------
# События очередей (queue_log)
# ---------------------------------------------------------------------------

ЖУРНАЛ_ОЧЕРЕДЕЙ = """\
1793000000|1793000000.1|sales|NONE|ENTERQUEUE||79160000001|1
1793000005|1793000000.1|sales|PJSIP/101|RINGNOANSWER|15000
1793000012|1793000000.1|sales|PJSIP/102|CONNECT|12|1793000000.3|4
1793000100|1793000000.1|sales|PJSIP/102|COMPLETECALLER|12|88|1
1793000200|1793000000.5|sales|NONE|ENTERQUEUE||79160000002|1
1793000245|1793000000.5|sales|NONE|ABANDON|1|1|45
1793000300|1793000000.6|sales|NONE|ENTERQUEUE||79160000003|1
1793000305|1793000000.6|sales|PJSIP/101|CONNECT|5|1793000000.7|2
1793000306|1793000000.9|sales|NONE|CONFIGRELOAD|
"""


def test_queue_log_is_parsed_into_waits_and_losses():
    from asrhub.telephony import queue_log

    события = queue_log.разобрать(ЖУРНАЛ_ОЧЕРЕДЕЙ)
    assert len(события) == 8, "CONFIGRELOAD — не событие ожидания"
    свод = queue_log.свод(события, порог_с=10)
    очередь = свод["queues"][0]
    assert очередь["entered"] == 3 and очередь["answered"] == 2 and очередь["lost"] == 1
    assert очередь["abandoned"] == 1 and очередь["avg_wait_s"] == 8.5
    assert очередь["avg_lost_wait_s"] == 45.0
    assert очередь["service_level"] == pytest.approx(33.3, abs=0.1)  # 5 с из (12, 5, ушёл)
    оператор = {а["agent"]: а for а in свод["agents"]}
    assert оператор["101"]["ringnoanswer"] == 1 and оператор["102"]["answered"] == 1


def test_station_queue_log_is_read_incrementally(tmp_path: Path, database):
    from asrhub.telephony import queue_log

    журнал = tmp_path / "queue_log"
    журнал.write_text(ЖУРНАЛ_ОЧЕРЕДЕЙ[:200], encoding="utf-8")
    события, позиция, inode = queue_log.прочитать_приращение(журнал, 0, 0)
    assert позиция <= 200 and all(с["ts"] for с in события)
    database.queue_events_add("main", события)
    журнал.write_text(ЖУРНАЛ_ОЧЕРЕДЕЙ, encoding="utf-8")
    ещё, позиция2, _ = queue_log.прочитать_приращение(журнал, позиция, inode)
    database.queue_events_add("main", ещё)
    # Повтор тех же строк не задваивает.
    database.queue_events_add("main", queue_log.разобрать(ЖУРНАЛ_ОЧЕРЕДЕЙ))
    assert len(database.queue_events(station="main")) == 8
    assert позиция2 == len(ЖУРНАЛ_ОЧЕРЕДЕЙ.encode())


def test_agent_ships_queue_log_and_admin_sees_the_summary(агентский_сервер):  # noqa: F811
    ответ = здравствуй(агентский_сервер)
    агент = {"X-API-Key": "ah_admin"}
    итог = агентский_сервер.post("/api/telephony/agent/queue_log", headers=агент, json={
        "agent_id": ответ["agent_id"], "lines": ЖУРНАЛ_ОЧЕРЕДЕЙ.splitlines()})
    assert итог.status_code == 200 and итог.json()["added"] == 8, итог.text
    свод = агентский_сервер.get("/api/telephony/queues", headers=агент,
                                params={"period": "year"})
    # События 2026-10 при «год» — в окне; при неделе их может не быть.
    assert свод.status_code == 200
    assert агентский_сервер.get("/api/telephony/queues",
                                headers={"X-API-Key": "ah_agent"}).status_code == 403


# ---------------------------------------------------------------------------
# Справочник: заголовок авторизации портала и каталог LDAP
# ---------------------------------------------------------------------------

def test_directory_download_sends_the_auth_header_but_not_on_redirect():
    import http.server
    import threading

    from asrhub import employees

    увидено: list[tuple[str, str]] = []

    class Портал(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_):
            return

        def do_GET(self):                                     # noqa: N802
            увидено.append((self.path, self.headers.get("Authorization") or ""))
            if self.path == "/go":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/file")
                self.end_headers()
                return
            тело = "Фамилия;Имя;Внутренний номер\nИванов;Иван;101\n".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

    сервер = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Портал)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    try:
        адрес = f"http://127.0.0.1:{сервер.server_port}"
        employees.загрузить(адрес + "/file", заголовок="Authorization: Bearer portal-secret")
        employees.загрузить(адрес + "/go", заголовок="Authorization: Bearer portal-secret")
    finally:
        сервер.shutdown()
    assert увидено[0] == ("/file", "Bearer portal-secret")
    assert увидено[1] == ("/go", "Bearer portal-secret")
    assert увидено[2] == ("/file", ""), "заголовок ушёл вслед за перенаправлением"


def test_directory_from_ldap_becomes_cards(database):
    from asrhub import employees

    class Значение:
        def __init__(self, значение):
            self.values = [значение] if значение else []

    class Запись:
        def __init__(self, **поля):
            for имя, значение in поля.items():
                setattr(self, имя, Значение(значение))

    class Связь:
        entries = [
            Запись(sn="Иванов", givenName="Иван", title="Менеджер", department="Продажи",
                   ipPhone="101", mail="ivanov@example.ru", userAccountControl="512"),
            Запись(displayName="Петров Пётр", ipPhone="102", userAccountControl="514"),
        ]

        def search(self, база, отбор, attributes=None):
            self.запрос = (база, отбор, attributes)

    настройки = {"auth_ldap_url": "ldaps://dc.example.ru", "auth_ldap_base_dn": "DC=example,DC=ru"}
    связь = Связь()
    таблица = employees.из_каталога(настройки, соединение=связь)
    assert "ipPhone" in связь.запрос[2] and связь.запрос[0] == "DC=example,DC=ru"
    итог = employees.импорт(database, данные=таблица, тип="csv", source="каталог")
    assert итог["added"] == 2, итог
    список = database.employee_list(active=None) if "active" in \
        database.employee_list.__code__.co_varnames else database.employee_list()
    строки = список["items"] if isinstance(список, dict) else список
    карточки = {str(к.get("phone_ext")): к for к in строки}
    assert карточки["101"]["department"] == "Продажи"
    assert not карточки["102"]["active"], "отключённая учётная запись AD — не работает"


def test_agent_reads_freepbx_columns_and_finds_the_recording_by_name(tmp_path: Path):
    from test_agent import агент, источник_mysql

    источник = источник_mysql(tmp_path, "[global]\ndbname=asteriskcdrdb\n")
    запросы: list[str] = []

    def выполнить(запрос):
        запросы.append(запрос)
        if запрос.startswith("SHOW COLUMNS"):
            for колонка in ("recordingfile", "did", "cnum"):
                if f"LIKE '{колонка}'" in запрос:
                    return f"{колонка}\tvarchar(255)\tNO\t\t\t\n"
            return ""
        поля = ["", "", "101", "from-did-direct", "Клиент", "PJSIP/tr-01", "PJSIP/101-0001",
                "Dial", "PJSIP/101", "2026-11-10 14:03:11", "60", "50", "ANSWERED",
                "1793000000.1", "", "in-74950000000-79161234567-20261110-140311-1793000000.1.wav",
                "74950000000", "79161234567"]
        return "\t".join(поля) + "\n"

    источник._выполнить = выполнить
    звонки = list(источник.звонки())
    выборка = [з for з in запросы if з.startswith("SELECT")][0]
    assert "recordingfile" in выборка and " did" in выборка and "cnum" in выборка
    звонок = звонки[0]
    assert звонок.recordingfile.startswith("in-74950000000") and звонок.did == "74950000000"
    assert звонок.src == "79161234567", "cnum не подставился вместо пустого src"
    assert звонок.поля_для_сервера("a1")["did"] == "74950000000"
    # Запись находится по имени из CDR, а не подбором по номерам.
    каталог = tmp_path / "monitor" / "2026" / "11" / "10"
    каталог.mkdir(parents=True)
    файл = каталог / звонок.recordingfile
    файл.write_bytes(b"RIFF")
    записи = агент.Записи(str(tmp_path / "monitor"))
    assert записи.найти(звонок) == str(файл)


# ---------------------------------------------------------------------------
# Разговор как единица (linkedid, этап 2)
# ---------------------------------------------------------------------------

def test_legs_of_one_linkedid_are_one_conversation(database):
    сейчас = time.time() - 3600
    # Клиент → очередь → 101 → перевод на 205: три ноги одного разговора.
    for номер, (кто, начало, говорили) in enumerate((("", 0, 0), ("101", 5, 60),
                                                     ("205", 70, 120))):
        database.save_call(f"main:1793000000.{номер}", station="main",
                           pbx_uid=f"1793000000.{номер}", linkedid="1793000000.0",
                           sequence=номер, agent=кто, started_at=сейчас + начало,
                           duration=говорили + 5, billsec=говорили, answered=1 if кто else 0,
                           direction="входящий")
    # И отдельный звонок без перевода.
    database.save_call("main:1793000100.1", station="main", pbx_uid="1793000100.1",
                       agent="102", started_at=сейчас + 300, billsec=30, duration=35, answered=1)
    итог = database.conversations(station="main", since=сейчас - 10)
    assert итог["calls"] == 4 and итог["conversations"] == 2 and итог["transferred"] == 1
    разговор = next(р for р in итог["items"] if р["legs"] == 3)
    assert разговор["agents"] == ["101", "205"] and разговор["transfers"] == 1
    assert разговор["talk_s"] == 180 and разговор["answered"]
    только = database.conversations(station="main", since=сейчас - 10, only_transferred=True)
    assert [р["legs"] for р in только["items"]] == [3]


# ---------------------------------------------------------------------------
# Оценка клиента: «Результаты», аналитика, выгрузка, права руководителя
# ---------------------------------------------------------------------------

def _опрос(db: Any, job_id: str, *, owner: str, pct: float | None, ext: str = "101") -> None:
    db.execute(
        "INSERT INTO qc_surveys (id, call_key, job_id, owner, ext, group_name, status, "
        "created_at, score, score_pct) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"qc-{job_id}", f"main:{job_id}", job_id, owner, ext, "Продажи",
         "пройден" if pct is not None else "без ответа", time.time(),
         None if pct is None else pct / 20, pct))


def test_results_show_and_filter_the_client_score(keys_client):
    db = keys_client.app.state.hub.db
    хорошо = db.create_job({"filename": "a.wav", "status": "completed", "owner": "админ"})
    плохо = db.create_job({"filename": "b.wav", "status": "completed", "owner": "админ"})
    _опрос(db, хорошо, owner="админ", pct=100)
    _опрос(db, плохо, owner="админ", pct=40)
    низкие = keys_client.get("/api/jobs", params={"content": "qc:low", "light": "true"}).json()
    assert [з["id"] for з in низкие["items"]] == [плохо]
    assert низкие["items"][0]["qc"]["score_pct"] == 40
    assert keys_client.get("/api/jobs", params={"content": "qc:nope"}).status_code in (200, 400)


def test_qc_surveys_export_to_xlsx_and_manager_scope(keys_client):
    import io

    from openpyxl import load_workbook

    db = keys_client.app.state.hub.db
    своё = db.create_job({"filename": "a.wav", "status": "completed", "owner": "отдел-1"})
    чужое = db.create_job({"filename": "b.wav", "status": "completed", "owner": "отдел-2"})
    _опрос(db, своё, owner="отдел-1", pct=80)
    _опрос(db, чужое, owner="отдел-2", pct=20)
    книга = keys_client.get("/api/qc/surveys/export", params={"fmt": "xlsx"})
    assert книга.status_code == 200, книга.text[:300]
    листы = load_workbook(io.BytesIO(книга.content)).sheetnames
    assert листы == ["Опросы", "Отчёт"]
    ключ = keys_client.post("/api/keys", json={"name": "отдел-1", "role": "user"}).json()["key"]
    руководитель = {"X-API-Key": ключ}
    журнал = keys_client.get("/api/qc/surveys", headers=руководитель)
    assert журнал.status_code == 200
    assert {з["owner"] for з in журнал.json()["items"]} == {"отдел-1"}
    свод = keys_client.get("/api/qc/summary", headers=руководитель)
    assert свод.status_code == 200
    # Телефон, группы и «не звонить» — по-прежнему только администратору.
    assert keys_client.get("/api/qc/stoplist", headers=руководитель).status_code == 403


def test_employees_analytics_carries_the_client_score(database):
    задание = database.create_job({"filename": "a.wav", "status": "completed"})
    _опрос(database, задание, owner="o", pct=60, ext="205")
    _опрос(database, database.create_job({"filename": "b.wav"}), owner="o", pct=100, ext="205")
    свод = database.qc_by("agent")
    assert свод["205"] == {"surveys": 2, "avg_pct": 80.0, "low": 0}


# ---------------------------------------------------------------------------
# Примеры клиентов: выгрузка результатов, повтор упавших, приёмник уведомлений
# ---------------------------------------------------------------------------

def test_examples_export_retry_and_receiver(tmp_path: Path, data_dir: Path, sample_wav: Path,
                                           monkeypatch: pytest.MonkeyPatch):
    import hashlib
    import hmac
    import importlib.util
    import subprocess
    import sys

    from asrhub.api import create_app
    from asrhub.config import load
    from drill_seed import поднять

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    app = create_app(load(), start_queue=True)
    адрес, остановить = поднять(app)
    try:
        import requests

        with sample_wav.open("rb") as файл:
            задание = requests.post(f"{адрес}/api/jobs", files={"file": ("one.wav", файл)},
                                    timeout=60).json()
        for _ in range(200):
            if requests.get(f"{адрес}/api/jobs/{задание['id']}", timeout=10).json()[
                    "status"] == "completed":
                break
            time.sleep(0.1)
        hub = app.state.hub
        упавшее = hub.db.create_job({"filename": "x.wav", "status": "failed",
                                     "error_code": "audio_error"})
        env = {**__import__("os").environ, "ASRHUB_SERVER": адрес}
        выгрузка = tmp_path / "out"
        итог = subprocess.run([sys.executable, str(КОРЕНЬ / "examples" / "export_results.py"),
                               str(выгрузка), "--format", "txt"], env=env,
                              capture_output=True, text=True, timeout=120)
        assert итог.returncode == 0 and "скачано 1" in итог.stdout, итог.stdout + итог.stderr
        повтор = subprocess.run([sys.executable, str(КОРЕНЬ / "examples" / "export_results.py"),
                                 str(выгрузка)], env=env, capture_output=True, text=True,
                                timeout=120)
        assert "скачано 0, уже было 1" in повтор.stdout
        причины = subprocess.run([sys.executable, str(КОРЕНЬ / "examples" / "retry_failed.py")],
                                 env=env, capture_output=True, text=True, timeout=120)
        assert "audio_error: 1 (не повторяется)" in причины.stdout, причины.stdout + причины.stderr
        assert hub.db.get_job(упавшее)["status"] == "failed"
    finally:
        остановить()

    spec = importlib.util.spec_from_file_location("приёмник", КОРЕНЬ / "examples" /
                                                  "webhook_receiver.py")
    приёмник = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(приёмник)
    тело, момент = b'{"id":"j1"}', str(int(time.time()))
    подпись = "sha256=" + hmac.new(b"s", момент.encode() + b"." + тело, hashlib.sha256).hexdigest()
    assert приёмник.подпись_верна(тело, момент, подпись, b"s")
    assert not приёмник.подпись_верна(тело + b" ", момент, подпись, b"s")
    assert not приёмник.подпись_верна(тело, str(int(time.time()) - 3600), подпись, b"s")
