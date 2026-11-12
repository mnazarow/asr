"""Заход 54, участок «безопасность и вход»: цикл событий, остановка, фоновые
потоки, исходящие запросы и мелочи API.

Отчёты 02-concurrency и 03-api-core ревизии 3.1.20. Проверки поведения:
не «в коде стоит to_thread», а «работа идёт не в потоке цикла событий»;
не «очередь в списке первой», а «очередь остановилась первой».
"""
from __future__ import annotations

import asyncio
import http.server
import io
import json
import math
import shutil
import signal
import sqlite3
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"

АДМИН = "ah_admin_54r_000000000"
АЛИСА = "ah_alice_54r_000000000"
ПРОДАЖИ = "ah_sales_54r_000000000"
КВОТА = "ah_quota_54r_000000000"


def H(ключ: str) -> dict[str, str]:
    return {"X-API-Key": ключ}


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
def настройки(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    н = load()
    н.api_keys.update({
        АДМИН: {"name": "админ", "role": "admin", "enabled": True},
        АЛИСА: {"name": "alice", "role": "user", "enabled": True},
        ПРОДАЖИ: {"name": "продажи", "role": "user", "enabled": True, "group": "sales"},
        КВОТА: {"name": "квота", "role": "user", "enabled": True, "quota_jobs_per_day": 1},
    })
    н.values["rate_limit_per_minute"] = 0
    н.values["login_rate_limit"] = 0
    return н


@pytest.fixture()
def сервер(настройки):
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        yield клиент


def _hub(клиент: TestClient) -> Any:
    return клиент.app.state.hub


def _в_цикле_событий() -> bool:
    """Вызвано ли из потока, где крутится цикл событий."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# ---------------------------------------------------------------------------
# Цикл событий не ждёт базу
# ---------------------------------------------------------------------------


def test_журнал_доступа_не_держит_цикл_событий(сервер):
    """02-concurrency №3, 03-api-core №4, 04-storage №2.

    Журнал писался синхронно из прослойки: пока замок записи держал VACUUM
    или копия, первый же не-GET запрос останавливал весь сервер.
    """
    db = _hub(сервер).db
    держит, отпустить = threading.Event(), threading.Event()

    def держать() -> None:
        with db._write_lock:                                   # noqa: SLF001
            держит.set()
            отпустить.wait(30)

    замок = threading.Thread(target=держать, daemon=True)
    замок.start()
    assert держит.wait(5)
    ответ: dict[str, Any] = {}
    запрос = threading.Thread(
        target=lambda: ответ.update(код=сервер.post("/api/auth/logout").status_code),
        daemon=True)
    запрос.start()
    запрос.join(timeout=10)
    висел = запрос.is_alive()
    отпустить.set()
    запрос.join(timeout=30)
    замок.join(timeout=5)
    assert not висел, "запрос ждал замок записи базы"
    assert ответ.get("код") == 200
    # И строка журнала всё равно записана — фоновым писателем.
    журнал = сервер.get("/api/audit", headers=H(АДМИН)).json()
    assert any(с["action"] == "выход" for с in журнал["items"]), журнал["items"][:3]


def test_анонимные_отказы_сводятся(сервер):
    """03-api-core №4: каждый анонимный 401 — строка журнала на год, без предела."""
    for _ in range(45):
        сервер.post("/api/jobs/bulk", json={"action": "retry", "ids": ["x"]})
    журнал = сервер.get("/api/audit", headers=H(АДМИН), params={"limit": 500}).json()
    анонимных = [с for с in журнал["items"] if с.get("kind") == "anon"]
    assert 0 < len(анонимных) <= 31, len(анонимных)


def test_приём_звонка_агента_не_в_цикле_событий(сервер, monkeypatch):
    """02-concurrency №2: хеш, ffprobe и запись в базу — прямо в цикле событий."""
    hub = _hub(сервер)
    agent_id = сервер.post("/api/telephony/agent/hello", headers=H(АДМИН),
                           json={"host": "pbx-r.local", "name": "АТС"}).json()["agent_id"]
    где: list[bool] = []
    прежний = hub.queue.submit

    def submit(*args, **kwargs):
        где.append(_в_цикле_событий())
        return прежний(*args, **kwargs)

    monkeypatch.setattr(hub.queue, "submit", submit)
    ответ = сервер.post("/api/telephony/agent/call", headers=H(АДМИН), data={
        "meta": json.dumps({"agent_id": agent_id, "uniqueid": "u-loop", "src": "79161234567",
                            "dst": "101", "duration": 3, "billsec": 2, "answered": True,
                            "started_at": time.time() - 60})},
        files={"file": ("запись.wav", _wav(1.0), "audio/wav")})
    assert ответ.status_code == 200, ответ.text
    assert где == [False], "постановка шла в потоке цикла событий"


def test_импорт_справочника_не_в_цикле_событий(сервер, monkeypatch):
    """02-concurrency №12, 10-analytics №1: скачивание выгрузки — в цикле событий."""
    from asrhub import employees

    где: list[bool] = []

    def импорт(*args, **kwargs):
        где.append(_в_цикле_событий())
        return {"added": 0, "updated": 0, "deactivated": 0, "dry_run": True}

    monkeypatch.setattr(employees, "импорт", импорт)
    ответ = сервер.post("/api/employees/import?dry_run=true", headers=H(АДМИН),
                        json={"url": "https://hr.example.com/выгрузка.csv"})
    assert ответ.status_code == 200, ответ.text
    assert где == [False]


def test_лента_спрашивает_очередь_не_в_цикле_событий(сервер, monkeypatch):
    """02-concurrency №3: queue.status() — десяток запросов к базе из /ws."""
    hub = _hub(сервер)
    где: list[bool] = []
    прежний = hub.queue.status

    def status():
        где.append(_в_цикле_событий())
        return прежний()

    monkeypatch.setattr(hub.queue, "status", status)
    with сервер.websocket_connect(f"/ws?api_key={АЛИСА}") as сокет:
        сокет.receive_text()
        сокет.send_text("status")
        сокет.receive_text()
    assert где and not any(где), где


def test_метод_вне_перечня_не_плодит_серий(сервер):
    """03-api-core №16, 02-concurrency №20б, 12-monitoring №3."""
    from asrhub.monitoring import RUNTIME

    for номер in range(5):
        сервер.request(f"M{номер:04d}", "/api/нет-такого")
    методы = {dict(метки).get("method") for (_, метки) in RUNTIME.snapshot()[0]}
    assert not {м for м in методы if м and м.startswith("M0")}, методы
    assert "other" in методы


# ---------------------------------------------------------------------------
# Остановка
# ---------------------------------------------------------------------------


def test_остановка_начинается_с_очереди_и_переживает_сбой(настройки):
    """02-concurrency №8, 05-queue №2: очередь стояла последней, без try."""
    app = create_app(настройки, start_queue=False)
    hub = app.state.hub
    порядок: list[str] = []

    def запомнить(имя: str, *, сбой: bool = False):
        def шаг(*args, **kwargs):
            порядок.append(имя)
            if сбой:
                raise RuntimeError("сбой при остановке")
        return шаг

    службы = [hub.monitoring, hub.content, hub.llm_worker, hub.telephony]
    прежние = [с.stop for с in службы]
    try:
        with TestClient(app):
            hub.queue.stop = запомнить("очередь")
            hub.monitoring.stop = запомнить("мониторинг", сбой=True)
            hub.content.stop = запомнить("разбор")
            hub.llm_worker.stop = запомнить("модель")
            hub.telephony.stop = запомнить("телефония")
            прежняя = hub.db.instance_forget
            hub.db.instance_forget = lambda: (порядок.append("отметка"), прежняя())
    finally:
        # Подменённые остановки ничего не останавливали — их потоки иначе
        # пережили бы тест и попали в счёт потоков соседних проверок.
        for остановить in прежние:
            остановить()
    assert порядок[0] == "очередь", порядок
    assert "отметка" in порядок and "телефония" in порядок, порядок


def test_сервер_ограничивает_ожидание_запросов_и_переживает_sighup(data_dir, monkeypatch):
    """02-concurrency №8–9: без срока остановки и смерть от SIGHUP."""
    import tempfile

    import uvicorn
    from asrhub import __main__ as точка_входа

    захвачено: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: захвачено.update(kw))
    monkeypatch.setattr(tempfile, "tempdir", None)
    if hasattr(signal, "SIGHUP"):
        прежний = signal.getsignal(signal.SIGHUP)
        monkeypatch.setattr(signal, "signal", _сигнал_без_последствий(прежний))
    assert точка_входа.main(["--port", "8199"]) == 0
    assert захвачено.get("timeout_graceful_shutdown"), захвачено
    if hasattr(signal, "SIGHUP"):
        assert _сигнал_без_последствий.поставлен, "обработчика SIGHUP нет"
    # Тело загрузки — во временный каталог сервера, а не в системный /tmp.
    assert tempfile.tempdir and Path(tempfile.tempdir).resolve() == \
        (data_dir / "tmp").resolve(), tempfile.tempdir


class _сигнал_без_последствий:
    """signal.signal, который запоминает обработчик SIGHUP, но не ставит его."""

    поставлен: Any = None

    def __init__(self, прежний: Any) -> None:
        self.прежний = прежний
        type(self).поставлен = None

    def __call__(self, номер: int, обработчик: Any) -> Any:
        if номер == getattr(signal, "SIGHUP", None):
            type(self).поставлен = обработчик
            return self.прежний
        return signal.SIG_DFL  # другие сигналы точка входа не трогает


def test_юнит_systemd_без_reload_через_kill(tmp_path: Path):
    """02-concurrency №9: ExecReload=kill -HUP убивал сервер, служба не поднималась."""
    итог = subprocess.run(
        [BASH, str(ROOT / "scripts" / "service.sh"), "install", "--dry-run",
         "--prefix", "/opt/asrhub", "--data", "/var/lib/asrhub", "--user", "asrhub"],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT))
    юнит = итог.stdout
    assert "ExecStart=" in юнит, итог.stderr[-500:]
    assert not [с for с in юнит.splitlines() if с.strip().startswith("ExecReload")], юнит


def test_compose_даёт_время_на_возврат_заданий():
    """02-concurrency №8: у Docker на остановку 10 с по умолчанию."""
    yaml = pytest.importorskip("yaml")
    описание = yaml.safe_load((ROOT / "docker" / "docker-compose.yml").read_text(encoding="utf-8"))
    срок = str(описание["services"]["asrhub"].get("stop_grace_period") or "")
    assert срок.endswith("s") and int(срок[:-1]) >= 30, срок


# ---------------------------------------------------------------------------
# Фоновые потоки и пулы
# ---------------------------------------------------------------------------


def test_поток_разбора_переживает_сбой_базы(сервер, monkeypatch):
    """02-concurrency №5, 11-llm №4: один StorageError убивал поток навсегда."""
    from asrhub.errors import StorageError

    hub = _hub(сервер)
    hub.settings.values["llm_backend"] = "stub"
    hub.settings.values["llm_queue_paused"] = False
    поток = hub.llm_worker
    поток.stop()
    взято = threading.Event()

    def _next():
        взято.set()
        raise StorageError("database is locked")

    monkeypatch.setattr(поток, "_next", _next)
    monkeypatch.setattr(поток, "_queue_busy", lambda: False)
    поток.start()
    try:
        assert взято.wait(15), "поток не дошёл до очереди"
        поток._thread.join(timeout=2)                          # noqa: SLF001
        assert поток._thread.is_alive(), "поток разбора умер от ошибки базы"  # noqa: SLF001
        assert "недоступна" in str(поток.last_error)
    finally:
        поток.stop()


def test_ручной_разбор_со_сбоем_закрывает_строку(сервер, monkeypatch):
    """02-concurrency №16, 11-llm №9: не LLMError оставлял «идёт» навсегда."""
    from asrhub.llm import tasks

    hub = _hub(сервер)
    hub.settings.values["llm_backend"] = "stub"
    job_id = hub.db.create_job({"filename": "разбор.wav", "status": "completed",
                                "owner": "alice", "text": "клиент: добрый день"})
    hub.db.save_segments(job_id, [{"start": 0.0, "end": 1.0, "text": "добрый день"}])

    def сбой(*args, **kwargs):
        raise RuntimeError("неожиданный ответ модели")

    monkeypatch.setattr(tasks, "analyze", сбой)
    from asrhub.llm import LLMError

    # Заход 54 (слияние с content-analytics): ручной разбор отдаёт любой сбой
    # как LLMError — обработчик кнопки показывает его текст.
    with pytest.raises(LLMError, match="неожиданный ответ модели"):
        hub.llm_worker.analyze_job(job_id, force=True)
    строка = hub.db.query_one("SELECT state FROM llm_queue WHERE job_id=?", [job_id])
    assert строка is None or строка["state"] != "идёт", dict(строка)


def test_живость_видит_остановившиеся_потоки():
    """02-concurrency №5: проба смотрела только флаг «запущена»."""
    from types import SimpleNamespace

    from asrhub.monitoring import probes

    мёртвый = threading.Thread(target=lambda: None, name="asrhub-heartbeat")
    мёртвый.start()
    мёртвый.join()
    рабочий = threading.Thread(target=lambda: None, name="asrhub-worker-0")
    рабочий.start()
    рабочий.join()
    очередь = SimpleNamespace(_started=True, _workers=[мёртвый, рабочий])
    state = SimpleNamespace(started_at=time.time(), queue=очередь, queue_enabled=True,
                            llm_worker=SimpleNamespace(_thread=мёртвый,
                                                       _stop=threading.Event()))
    итог = probes.liveness(state)
    assert итог["status"] == "fail", итог


def test_шлюз_модели_работает_в_своём_пуле(client, monkeypatch):
    """02-concurrency №6, 11-llm №3: ожидание слота занимало общий пул."""
    from asrhub.api import routes_llm_proxy

    monkeypatch.setattr(routes_llm_proxy, "_одним_ответом",
                        lambda *a, **k: {"поток": threading.current_thread().name})
    hub = _hub(client)
    hub.settings.values.update({"llm_backend": "stub", "llm_model": "stub",
                                "llm_network_enabled": True})
    ответ = client.post("/api/llm/v1/chat/completions", json={
        "model": "stub", "messages": [{"role": "user", "content": "привет"}]})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["поток"].startswith("asrhub-llm-gw"), ответ.json()


def test_шлюз_модели_не_копит_ждущих_без_предела(client, monkeypatch):
    """02-concurrency №6: сверх предела ждущих — отказ сразу, а не ещё один поток."""
    from asrhub.api import routes_llm_proxy

    monkeypatch.setattr(routes_llm_proxy, "ЖДУЩИХ_ШЛЮЗА", 0, raising=False)
    monkeypatch.setattr(routes_llm_proxy, "_одним_ответом", lambda *a, **k: {"ok": True})
    hub = _hub(client)
    hub.settings.values.update({"llm_backend": "stub", "llm_model": "stub",
                                "llm_network_enabled": True})
    ответ = client.post("/api/llm/v1/chat/completions", json={
        "model": "stub", "messages": [{"role": "user", "content": "привет"}]})
    assert ответ.status_code == 503, ответ.text
    assert ответ.headers.get("retry-after") or ответ.json().get("retryable"), ответ.text


def test_диктовка_работает_в_своём_пуле(client, monkeypatch):
    """02-concurrency №6, 05-queue №16: сессии занимали пул приёма файлов."""
    from asrhub import streaming

    где: list[str] = []

    def start(self):
        где.append(threading.current_thread().name)
        return streaming.StreamEvent("ready", extra={"mode": "window"})

    monkeypatch.setattr(streaming.StreamSession, "start", start)
    with client.websocket_connect("/api/stream") as сокет:
        сокет.send_text(json.dumps({"format": "pcm_s16le"}))
        assert json.loads(сокет.receive_text())["type"] == "ready"
    assert где and где[0].startswith("asrhub-stream"), где


def _реестр_с_демо() -> tuple[Any, dict[str, Any]]:
    from asrhub.engines import EngineRegistry

    return EngineRegistry(2, 0), {"model": "demo-simulator", "engine": "demo",
                                  "device": "cpu", "compute_type": "int8"}


def test_второй_экземпляр_модели_ждёт_с_пределом():
    """02-concurrency №6: поток висел на замке второго экземпляра без срока."""
    from asrhub.errors import EngineBusy

    реестр, настройки = _реестр_с_демо()
    держать, взяли = threading.Event(), threading.Barrier(3)

    def занять(отдельный: bool) -> None:
        with реестр.lease(настройки, wait_s=0.1, отдельный_если_занят=отдельный):
            взяли.wait(10)
            держать.wait(30)

    первый = threading.Thread(target=занять, args=(False,), daemon=True)
    первый.start()
    time.sleep(0.3)
    второй = threading.Thread(target=занять, args=(True,), daemon=True)
    второй.start()
    взяли.wait(10)
    итог: dict[str, Any] = {}

    def третий() -> None:
        try:
            with реестр.lease(настройки, wait_s=0.2, отдельный_если_занят=True):
                итог["взял"] = True
        except EngineBusy:
            итог["занят"] = True

    поток = threading.Thread(target=третий, daemon=True)
    поток.start()
    поток.join(timeout=10)
    висел = поток.is_alive()
    держать.set()
    for п in (первый, второй, поток):
        п.join(timeout=10)
    assert not висел, "ожидание второго экземпляра без срока"
    assert итог.get("занят"), итог


def test_выгрузка_моделей_не_трогает_занятую(сервер):
    """02-concurrency №13, 07-engines №6: «Выгрузить модели» роняла идущее задание."""
    hub = _hub(сервер)
    настройки = hub.settings.merged({})
    держать, взяли = threading.Event(), threading.Event()

    def занять() -> None:
        with hub.registry.lease(настройки):
            взяли.set()
            держать.wait(30)

    поток = threading.Thread(target=занять, daemon=True)
    поток.start()
    assert взяли.wait(30)
    try:
        ответ = сервер.post("/api/maintenance/unload-models", headers=H(АДМИН))
        assert ответ.status_code == 200
        assert ответ.json().get("busy_kept") == 1, ответ.json()
        assert hub.registry.loaded(), "занятую модель выгрузили"
    finally:
        держать.set()
        поток.join(timeout=10)


def test_повторный_запуск_отправки_метрик_не_плодит_потоков(monkeypatch):
    """02-concurrency №20а: недождавшийся поток терял ссылку — и рядом второй."""
    from asrhub.monitoring import pushers

    менеджер = pushers.PushManager(lambda: [], [])
    в_сборе, отпустить = threading.Event(), threading.Event()

    def tick(*args, **kwargs):
        в_сборе.set()
        отпустить.wait(30)

    monkeypatch.setattr(менеджер, "tick", tick)
    # Потоки отправки, оставшиеся от других проверок в том же процессе, не в
    # счёт: считаем только заведённые этим менеджером.
    прежние = {п for п in threading.enumerate() if п.name == "asrhub-push"}
    менеджер.start()
    try:
        assert в_сборе.wait(20)
        менеджер.stop(timeout=0.2)
        менеджер.start()
        живых = [п for п in threading.enumerate()
                 if п.name == "asrhub-push" and п.is_alive() and п not in прежние]
        assert len(живых) == 1, живых
    finally:
        отпустить.set()
        менеджер.stop(timeout=10)


def test_повторный_запуск_тревог_не_плодит_потоков(сервер, monkeypatch):
    """02-concurrency №20а: то же у фонового расчёта тревог."""
    from asrhub.monitoring import service

    мониторинг = _hub(сервер).monitoring
    мониторинг.stop()
    в_сборе, отпустить = threading.Event(), threading.Event()

    def samples(*args, **kwargs):
        в_сборе.set()
        отпустить.wait(60)
        return [], []

    monkeypatch.setattr(service, "ОЦЕНКА_ТРЕВОГ_С", 0.05)
    monkeypatch.setattr(мониторинг, "samples", samples)
    мониторинг.start()
    try:
        assert в_сборе.wait(20)
        мониторинг.stop()                 # поток стоит в сборе и не дождётся

        def живые() -> set[threading.Thread]:
            return {п for п in threading.enumerate()
                    if п.name == "asrhub-alerts" and п.is_alive()}

        до = живые()
        мониторинг.start()
        assert живые() == до, "рядом с недождавшимся потоком поднят второй"
    finally:
        отпустить.set()
        мониторинг.stop()


def _сессия_диктовки(tmp_path: Path, **свои: Any) -> Any:
    from asrhub.engines import EngineRegistry
    from asrhub.streaming import StreamSession

    н = load()
    н.set("model", "demo-simulator")
    н.set("engine", "demo")
    сессия = StreamSession(EngineRegistry(), н.merged(свои), workdir=tmp_path)
    сессия.start()
    return сессия


def test_сессия_диктовки_не_обрывается_переводом_часов(data_dir, tmp_path, monkeypatch):
    """02-concurrency №14: часы вперёд на час обрывали сессию «длиннее часа»."""
    from asrhub import streaming

    сессия = _сессия_диктовки(tmp_path, stream_window_s=30)
    try:
        настоящее = time.time
        monkeypatch.setattr(streaming.time, "time", lambda: настоящее() + 7200)
        события = сессия.feed(streaming.tone(0.2))
        assert "error" not in [с.type for с in события], [с.to_dict() for с in события]
    finally:
        сессия.close()


def test_сессия_диктовки_ограничена_количеством_звука(data_dir, tmp_path, monkeypatch):
    """02-concurrency №14: звук быстрее реального времени обходил предел сессии."""
    from asrhub import streaming

    monkeypatch.setattr(streaming, "MAX_SESSION_S", 3.0)
    сессия = _сессия_диктовки(tmp_path, stream_window_s=30)
    try:
        события: list[Any] = []
        for _ in range(16):             # восемь секунд звука разом
            события += сессия.feed(streaming.tone(0.5))
        assert "error" in [с.type for с in события]
    finally:
        сессия.close()


# ---------------------------------------------------------------------------
# Исходящие запросы
# ---------------------------------------------------------------------------


def test_общий_адрес_провайдера_внутренний():
    """05-queue №6: 100.64.0.0/10 (там метаданные Alibaba Cloud) считался внешним."""
    from asrhub.errors import ConfigError
    from asrhub.job_queue import check_outbound_url

    for адрес in ("http://100.100.100.200/latest/meta-data/", "http://100.64.0.1/",
                  "http://192.0.2.10/"):
        with pytest.raises(ConfigError):
            check_outbound_url(адрес)
    assert check_outbound_url("https://93.184.216.34/hook")


class _Приёмник(http.server.BaseHTTPRequestHandler):
    запросы: list[str] = []

    def do_POST(self):  # noqa: N802
        type(self).запросы.append(self.path)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        return


def test_соединение_идёт_на_проверенный_адрес(monkeypatch):
    """05-queue №6: проверка и соединение узнавали имя каждый своё (DNS-подмена)."""
    import socket
    import urllib.request

    from asrhub.errors import ConfigError
    from asrhub.job_queue import check_outbound_url, открыватель_наружу

    for имя in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
                "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(имя, raising=False)
    сервер = http.server.HTTPServer(("127.0.0.1", 0), _Приёмник)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    порт = сервер.server_port
    настоящий = socket.getaddrinfo
    ответов = {"n": 0}

    def подмена(хост, *args, **kwargs):
        if хост == "rebind.example":
            ответов["n"] += 1
            адрес = "93.184.216.34" if ответов["n"] == 1 else "127.0.0.1"
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (адрес, args[0] if args else 0))]
        return настоящий(хост, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", подмена)
    _Приёмник.запросы = []
    адрес = f"http://rebind.example:{порт}/internal-admin"
    try:
        assert check_outbound_url(адрес)           # первый ответ DNS — внешний
        запрос = urllib.request.Request(адрес, data=b"{}", method="POST")
        with pytest.raises(ConfigError):
            открыватель_наружу(False).open(запрос, timeout=5)
    finally:
        сервер.shutdown()
    assert not _Приёмник.запросы, "запрос ушёл на внутренний адрес"


def test_номер_сделки_только_число(сервер):
    """11-llm №13: «../contacts/5» уходил в путь amoCRM с токеном интеграции."""
    from asrhub import crm
    from asrhub.errors import ASRHubError

    ответ = сервер.post("/api/jobs/text", headers=H(АЛИСА), json={
        "text": "клиент: добрый день\nоператор: здравствуйте",
        "crm_entity_id": "../contacts/5"})
    assert ответ.status_code == 400, ответ.text
    настройки = crm.Настройки(enabled=True, kind="amocrm", url="https://фирма.amocrm.ru",
                              token="AMO-TOKEN", fields={})
    with pytest.raises(ASRHubError):
        crm.запросы({"summary": "итог"}, настройки, entity_id="1/../../../private/api/v2/hooks?x=")
    assert crm.запросы({"summary": "итог"}, настройки, entity_id="123456")


# ---------------------------------------------------------------------------
# Мелочи API
# ---------------------------------------------------------------------------


def test_переписки_разом_не_обходят_квоту(сервер, monkeypatch):
    """02-concurrency №10: проверка квоты и создание переписки шли без замка."""
    hub = _hub(сервер)
    прежний = hub.db.create_job

    def create_job(*args, **kwargs):
        time.sleep(0.3)              # окно между проверкой и записью
        return прежний(*args, **kwargs)

    monkeypatch.setattr(hub.db, "create_job", create_job)
    старт = threading.Barrier(4)
    коды: list[int] = []

    def отправить() -> None:
        старт.wait(30)
        коды.append(сервер.post("/api/jobs/text", headers=H(КВОТА), json={
            "text": "клиент: добрый день\nоператор: здравствуйте"}).status_code)

    потоки = [threading.Thread(target=отправить, daemon=True) for _ in range(4)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=60)
    assert sorted(коды) == [200, 429, 429, 429], коды


def test_выгрузка_с_управляющим_знаком_не_пятисотка(сервер):
    """01-security №4: NUL в периоде ронял openpyxl — 500 вместо ответа."""
    pytest.importorskip("openpyxl")
    ответ = сервер.get("/api/analytics/export", params={"period": "a\x00b", "fmt": "xlsx"},
                       headers=H(АДМИН))
    assert ответ.status_code != 500, ответ.text[:300]


def test_повторное_распознавание_проверяет_всё_до_постановки(сервер):
    """03-api-core №17: «priority: срочно» ставило задание и отвечало отказом."""
    hub = _hub(сервер)
    исходник = hub.settings.paths.uploads / "up_54_rescan.wav"
    исходник.write_bytes(_wav(0.5))
    job_id = hub.db.create_job({"filename": "повтор.wav", "status": "completed",
                                "owner": "alice", "file_path": str(исходник)})
    ответ = сервер.post("/api/jobs/rescan", headers=H(АЛИСА),
                        json={"ids": [job_id], "priority": "срочно"})
    assert ответ.status_code == 400, ответ.text
    assert hub.db.get_job(job_id)["status"] == "completed", "задание поставлено при отказе"
    кривой = сервер.post("/api/jobs/rescan", headers=H(АЛИСА), json={"filter": "completed"})
    assert кривой.status_code == 400, кривой.text


def test_повторное_распознавание_с_пределом_и_понятным_отказом(сервер):
    """03-api-core №17: «403: {'code': …}» в отказе и список ids без предела."""
    hub = _hub(сервер)
    чужое = hub.db.create_job({"filename": "чужое.wav", "status": "completed",
                               "owner": "админ"})
    ответ = сервер.post("/api/jobs/rescan", headers=H(АЛИСА), json={"ids": [чужое]})
    assert ответ.status_code == 200, ответ.text
    отказ = ответ.json()["failed"][0]["error"]
    assert not отказ.startswith(("403", "404")) and "code" not in отказ, отказ
    много = сервер.post("/api/jobs/rescan", headers=H(АЛИСА),
                        json={"ids": [f"job_{н:06d}" for н in range(5001)]})
    assert много.status_code == 400, много.text[:300]


def test_сброс_настроек_доходит_до_реестра_моделей(сервер):
    """03-api-core №18: сброс менял только значения, реестр жил по-старому."""
    hub = _hub(сервер)
    hub.registry.configure(5, 0)
    assert сервер.post("/api/settings/reset", headers=H(АДМИН)).status_code == 200
    # Заход 54 (слияние с params): сброс не трогает параметры самого сервера
    # (`Settings.сбросить`) — реестр возвращается к НЫНЕШНИМ настройкам
    # сервера, а не к умолчаниям каталога.
    assert hub.registry.max_size == max(1, int(hub.settings.get("model_cache_size") or 2))
    assert hub.registry.idle_unload_s == int(hub.settings.get("model_idle_unload_s"))


def test_удаление_по_требованию_говорит_об_остатке(сервер, monkeypatch):
    """03-api-core, недоделки: erase молча ограничен порцией — «удалено всё»."""
    from asrhub.api import routes_jobs

    hub = _hub(сервер)
    monkeypatch.setattr(routes_jobs, "BULK_LIMIT", 2)
    for номер in range(3):
        hub.db.create_job({"filename": f"79990001122-{номер}.wav", "status": "completed",
                           "owner": "alice", "text": "перезвоните на 79990001122"})
    ответ = сервер.post("/api/maintenance/erase", headers=H(АДМИН),
                        json={"query": "79990001122"}).json()
    assert ответ["matched"] == 2 and ответ.get("more") is True, ответ
    # Заход 54 (слияние со storage): удаление идёт порциями до конца, а
    # `more` остаётся для остатка сверх ERASE_PASSES проходов
    # (test_review_54_storage.test_остаток_сверх_прохода_остаётся_находимым).
    удалено = сервер.post("/api/maintenance/erase", headers=H(АДМИН),
                          json={"query": "79990001122", "dry_run": False}).json()
    assert удалено["deleted"] == 3 and удалено.get("more") is False, удалено
    остаток = сервер.post("/api/maintenance/erase", headers=H(АДМИН),
                          json={"query": "79990001122"}).json()
    assert остаток["matched"] == 0 and остаток.get("more") is False, остаток


def test_сервер_без_очереди_не_поднимает_воркеров_правкой_настроек(сервер):
    """05-queue №1: «--no-queue» начинал разбирать очередь после правки числа воркеров."""
    hub = _hub(сервер)
    assert hub.queue._worker_count == 0                       # noqa: SLF001
    ответ = сервер.put("/api/settings", headers=H(АДМИН), json={"max_concurrent_jobs": 3})
    assert ответ.status_code == 200, ответ.text
    assert сервер.post("/api/settings/reset", headers=H(АДМИН)).status_code == 200
    assert hub.queue._worker_count == 0                       # noqa: SLF001


def test_набор_настроек_доходит_до_очереди(client):
    """03-api-core №18: и применение набора меняло значения, но не очередь."""
    hub = _hub(client)
    assert hub.queue._worker_count == 2                       # noqa: SLF001
    ответ = client.post("/api/presets/ru-speed/apply")
    assert ответ.status_code == 200 and ответ.json()["applied"].get("max_concurrent_jobs") == 1
    assert hub.queue._worker_count == 1                       # noqa: SLF001


def test_время_звонка_бесконечность_не_записывается(сервер):
    """03-api-core, недоделки: «inf» и «1e30» ложились в архив звонков."""
    for значение in ("inf", "nan", "1e30"):
        ответ = сервер.post("/api/jobs", headers=H(АЛИСА),
                            files={"file": ("звонок.wav", _wav(0.5), "audio/wav")},
                            data={"caller": "79161234567", "call_started_at": значение})
        assert ответ.status_code == 200, (значение, ответ.text[:300])
        звонок = ответ.json()["call"]
        assert звонок["warnings"], звонок
        assert math.isfinite(float(звонок["started_at"])) and \
            float(звонок["started_at"]) <= time.time() + 60


def test_ответы_несут_заголовки_безопасности(сервер):
    """01-security, недоделки: ни nosniff, ни запрета рамки."""
    ответ = сервер.get("/api/health")
    assert ответ.headers.get("x-content-type-options") == "nosniff"
    assert ответ.headers.get("x-frame-options") == "SAMEORIGIN"


def test_лента_событий_по_охвату_подразделения(сервер):
    """03-api-core, недоделки: /api/events брал задания по одному имени."""
    hub = _hub(сервер)
    hub.accounts.create("petrova", "пароль-петровой", group="sales",
                        must_change_password=False)
    job_id = hub.db.create_job({"filename": "коллега.wav", "status": "completed",
                                "owner": "petrova"})
    hub.db.add_event(job_id, "completed", "Готово")
    события = сервер.get("/api/events", headers=H(ПРОДАЖИ)).json()["items"]
    assert job_id in [с.get("job_id") for с in события]


def test_окна_частоты_не_растут_без_предела(monkeypatch):
    """03-api-core, недоделки: словарь окон рос произвольными ключами навсегда."""
    from asrhub.api import deps

    состояние = deps.AppState(settings=None, db=None, registry=None, queue=None,
                              analytics=None)
    сейчас = {"t": 1000.0}
    monkeypatch.setattr(deps.time, "monotonic", lambda: сейчас["t"])
    for номер in range(1500):
        состояние.check_rate(f"login:6.6.6.{номер}", 10)
    сейчас["t"] += 120
    # Чистка — не на каждом запросе, а когда словарь вырос вдвое против
    # живых окон; после неё минутой раньше закрытых окон не остаётся.
    for номер in range(1500, 2100):
        состояние.check_rate(f"login:6.6.6.{номер}", 10)
    assert "login:6.6.6.0" not in состояние._rate                # noqa: SLF001
    assert len(состояние._rate) <= 600, len(состояние._rate)   # noqa: SLF001


def test_билет_живёт_по_монотонным_часам(monkeypatch):
    """02-concurrency №14: перевод часов вперёд гасил билеты, назад — продлевал."""
    from asrhub.api import deps

    билеты = deps.TicketStore(ttl_s=60)
    билет, _ = билеты.issue("ah_key")
    настоящее = time.time
    monkeypatch.setattr(deps.time, "time", lambda: настоящее() + 3600)
    assert билеты.redeem(билет) == "ah_key"


def test_рассылка_не_ждёт_зависшего_подписчика(monkeypatch):
    """03-api-core, недоделки: зависший клиент задерживал рассылку всем."""
    from asrhub.api import event_hub as модуль

    monkeypatch.setattr(модуль, "ОТПРАВКА_СОБЫТИЯ_С", 0.2)

    class Зависший:
        async def send_text(self, текст):
            await asyncio.sleep(3600)

        async def close(self, code=1000, reason=""):
            return None

    class Обычный:
        def __init__(self):
            self.получено: list[str] = []

        async def send_text(self, текст):
            self.получено.append(текст)

        async def close(self, code=1000, reason=""):
            return None

    лента = модуль.EventHub()
    зависший, обычный = Зависший(), Обычный()
    подписчик = getattr(модуль, "Подписчик", None)
    for клиент in (зависший, обычный):
        лента._clients[клиент] = (подписчик(is_admin=True) if подписчик   # noqa: SLF001
                                  else ("", True))

    async def разослать():
        await asyncio.wait_for(лента._broadcast({"type": "x"}), timeout=10)  # noqa: SLF001

    asyncio.run(разослать())
    assert обычный.получено, "обычный подписчик ничего не получил"
    assert зависший not in лента._clients                                # noqa: SLF001


def test_миграция_перечитывает_версию_под_замком(tmp_path: Path):
    """02-concurrency №20в: второй сервер повторял починки поверх обновлённой базы."""
    import inspect

    from asrhub import db as модуль_базы

    путь = tmp_path / "гонка.db"
    модуль_базы.Database(путь).close()
    with sqlite3.connect(путь) as соединение:
        соединение.execute(f"PRAGMA user_version={модуль_базы.SCHEMA_VERSION - 1}")
    починки: list[int] = []

    class Обёртка:
        def __init__(self, замок):
            self.замок = замок

        def __enter__(self):
            if inspect.stack()[1].function == "_migrate":
                # Пока мы ждали замок, соседний сервер уже обновил схему.
                with sqlite3.connect(путь) as другой:
                    другой.execute(f"PRAGMA user_version={модуль_базы.SCHEMA_VERSION}")
            return self.замок.__enter__()

        def __exit__(self, *exc):
            return self.замок.__exit__(*exc)

    class Гонка(модуль_базы.Database):
        @property
        def _write_lock(self):
            return self._обёртка

        @_write_lock.setter
        def _write_lock(self, замок):
            self._обёртка = Обёртка(замок)

        def _итоги_без_расшифровки(self, conn):
            починки.append(1)
            return super()._итоги_без_расшифровки(conn)

    Гонка(путь).close()
    assert not починки, "починки данных повторены поверх обновлённой схемы"
