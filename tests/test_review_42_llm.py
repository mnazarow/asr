"""Заход 42: языковая модель, её очередь, шлюз и обратная запись в CRM.

Очередь модели на нескольких серверах, попытки при лежащем сервере модели,
разбор ответа с пояснением, проба модели, слот шлюза — и примечание в CRM,
которое уходит один раз и с пересказом.
"""
from __future__ import annotations

import http.server
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from asrhub import crm
from asrhub.content_index import ContentIndex
from asrhub.db import Database
from asrhub.instance import HOSTNAME, INSTANCE_ID
from asrhub.llm import tasks
from asrhub.llm.client import LLMClient, LLMError, та_же_модель
from asrhub.llm.worker import LLMWorker


class Настройки:
    def __init__(self, **значения: Any):
        self.значения = {
            "llm_backend": "stub", "llm_model": "test", "llm_auto": True,
            "llm_backfill": False, "llm_yield_to_queue": False,
            "llm_queue_paused": False, "llm_max_concurrent": 1,
            "llm_queue_batch": 5, "llm_queue_idle_s": 1,
            "llm_queue_cooldown_s": 1800, "llm_tasks": ["summary", "outcome"],
            "content_analysis": True,
            **значения,
        }

    def get(self, ключ: str, по_умолчанию: Any = None) -> Any:
        return self.значения.get(ключ, по_умолчанию)


def база(tmp_path: Path, записей: int = 2, **параметры: Any) -> Database:
    db = Database(tmp_path / "asrhub.db")
    for i in range(записей):
        db.create_job({"id": f"job{i}", "filename": f"запись-{i}.wav", "owner": "анна",
                       "model": "demo", "engine": "demo", "media_duration_s": 30.0,
                       "params": dict(параметры)})
        db.update_job(f"job{i}", status="completed", finished_at=time.time(),
                      text="Здравствуйте, у меня вопрос по заказу. Перезвоню вам завтра.")
        db.save_segments(f"job{i}", [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
             "text": "Здравствуйте, у меня вопрос по заказу."},
            {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
             "text": "Сейчас посмотрю и перезвоню вам завтра."}])
    return db


def закрытый_порт() -> int:
    с = socket.socket()
    с.bind(("127.0.0.1", 0))
    порт = с.getsockname()[1]
    с.close()
    return порт


class HTTPСервер:
    """Маленький сервер: отвечает заданным кодом и телом, запоминает запросы."""

    def __init__(self, код: int = 200, тело: Any = None, пауза: float = 0.0):
        сервер = self
        self.код, self.тело, self.пауза = код, тело if тело is not None else {"ok": True}, пауза
        self.запросы: list[dict[str, Any]] = []

        class Обработчик(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def _ответить(self):
                длина = int(self.headers.get("Content-Length") or 0)
                данные = self.rfile.read(длина) if длина else b""
                сервер.запросы.append({"method": self.command, "path": self.path,
                                       "body": данные.decode("utf-8", "replace")})
                if сервер.пауза:
                    time.sleep(сервер.пауза)
                тело = json.dumps(сервер.тело, ensure_ascii=False).encode()
                self.send_response(сервер.код)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(тело)))
                self.end_headers()
                self.wfile.write(тело)

            do_GET = do_POST = do_PATCH = _ответить                # noqa: N815

        self._сервер = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Обработчик)
        threading.Thread(target=self._сервер.serve_forever, daemon=True).start()

    @property
    def адрес(self) -> str:
        return f"http://127.0.0.1:{self._сервер.server_address[1]}"

    def закрыть(self) -> None:
        self._сервер.shutdown()
        self._сервер.server_close()


@pytest.fixture()
def crm_сервер():
    сервер = HTTPСервер(200, {"id": 1})
    yield сервер
    сервер.закрыть()


# ---------------------------------------------------------------------------
# Очередь модели на нескольких серверах
# ---------------------------------------------------------------------------

def test_старт_не_забирает_идущий_разбор_соседа(tmp_path: Path):
    """Второй сервер при старте возвращал в очередь ВСЕ идущие строки.

    Запись, которую прямо сейчас разбирает сосед по общей базе, уходила в
    «ждёт» — и разбиралась второй раз.
    """
    db = база(tmp_path, записей=4)
    for i in range(4):
        db.llmq_put(f"job{i}")
    db.llmq_take(instance="сосед-машина:4242")                     # живой сосед
    db.llmq_take()                                                  # своё
    db.llmq_take(instance=f"{HOSTNAME}:999999")                     # мёртвый на этой машине
    db.llmq_take(instance="сосед-машина:7777")                      # сосед без отметки
    db.execute("UPDATE llm_queue SET heartbeat_at=? WHERE instance='сосед-машина:7777'",
               [time.time() - 3600])
    вернулось = db.llmq_reset_running(stale_s=600)
    assert вернулось == 3
    идут = db.query("SELECT job_id, instance FROM llm_queue WHERE state=?", (db.LLMQ_ИДЁТ,))
    assert [(r["job_id"], r["instance"]) for r in идут] == [("job0", "сосед-машина:4242")]


def test_обход_на_ходу_не_трогает_свой_идущий_разбор(tmp_path: Path):
    db = база(tmp_path, записей=2)
    db.llmq_put("job0")
    db.llmq_put("job1")
    db.llmq_take()
    db.llmq_take(instance="сосед:1")
    db.execute("UPDATE llm_queue SET heartbeat_at=? WHERE instance='сосед:1'",
               [time.time() - 3600])
    assert db.llmq_reset_running(stale_s=600, свои=False) == 1
    assert db.query_one("SELECT state, instance FROM llm_queue WHERE job_id='job0'")[
        "instance"] == INSTANCE_ID
    assert db.llmq_heartbeat() == 1


def test_постановка_в_очередь_одним_выражением(tmp_path: Path, monkeypatch):
    """Чтение и запись двумя шагами: две постановки разом давали ошибку базы.

    Между «прочитать» и «вставить» второй поток успевал сделать то же самое,
    и вторая вставка падала на UNIQUE — «ошибка записи в базу» в ответ на
    нажатие кнопки. Здесь окно между шагами растянуто нарочно.
    """
    db = база(tmp_path, записей=1)
    исходный = Database.query_one

    def медленно(self, sql, *а, **к):
        итог = исходный(self, sql, *а, **к)
        if "FROM llm_queue WHERE job_id" in sql:
            time.sleep(0.2)
        return итог

    monkeypatch.setattr(Database, "query_one", медленно)
    сбои: list[BaseException] = []

    def поставить():
        try:
            db.llmq_put("job0", kind="по просьбе", priority=70)
        except BaseException as exc:                          # noqa: BLE001
            сбои.append(exc)

    потоки = [threading.Thread(target=поставить) for _ in range(2)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join()
    assert сбои == []
    assert db.llmq_counts()[db.LLMQ_ЖДЁТ] == 1
    db.llmq_take()
    assert db.llmq_put("job0") is False, "идущую запись вернули в «ждёт»"


def test_повтор_из_ошибки_получает_новые_попытки(tmp_path: Path):
    db = база(tmp_path, записей=1)
    db.llmq_put("job0")
    for _n in range(3):
        db.llmq_take()
        db.llmq_release("job0", error="сбой", attempts_max=3)
    assert db.query_one("SELECT state FROM llm_queue")["state"] == db.LLMQ_ОШИБКА
    db.llmq_retry_failed()
    строка = db.query_one("SELECT state, attempts, priority FROM llm_queue")
    assert строка["state"] == db.LLMQ_ЖДЁТ and строка["attempts"] == 0
    # «Повторить упавшие» — с важностью архива, а не выше свежих записей.
    assert строка["priority"] == 50


def test_повтор_упавших_ставится_с_важностью_архива(tmp_path: Path):
    """«Повторить упавшие» — сотни записей разом; раньше они шли впереди свежих."""
    db = база(tmp_path, записей=1)
    db.llmq_put("job0", priority=10)
    db.llmq_take()
    db.llmq_finish("job0", error="сбой")
    db.llmq_retry_failed()
    assert db.query_one("SELECT priority FROM llm_queue")["priority"] == 30


def test_повтор_упавших_из_раздела_берёт_важность_архива_из_настроек(data_dir: Path):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    настройки = load()
    настройки.set("llm_backend", "stub", source="api")
    настройки.set("llm_queue_priority_backfill", 25, source="api")
    with TestClient(create_app(настройки, start_queue=False)) as клиент:
        db = клиент.app.state.hub.db
        db.create_job({"id": "jobX", "filename": "x.wav", "owner": "anonymous"})
        db.llmq_put("jobX", priority=90)
        db.llmq_take()
        db.llmq_finish("jobX", error="сбой")
        ответ = клиент.post("/api/llm/queue/add", json={"scope": "failed"})
        assert ответ.status_code == 200, ответ.text
        assert db.query_one("SELECT priority, state FROM llm_queue")["state"] == db.LLMQ_ЖДЁТ
        assert db.query_one("SELECT priority FROM llm_queue")["priority"] == 90
        db.execute("UPDATE llm_queue SET priority=10, state=?", [db.LLMQ_ОШИБКА])
        клиент.post("/api/llm/queue/add", json={"scope": "failed"})
        assert db.query_one("SELECT priority FROM llm_queue")["priority"] == 25


# ---------------------------------------------------------------------------
# Лежащий сервер модели не тратит попытки
# ---------------------------------------------------------------------------

def test_недоступный_сервер_модели_не_уводит_записи_в_ошибку(tmp_path: Path):
    """Минутный перезапуск Ollama уводил головные записи в «ошибку» за полминуты."""
    db = база(tmp_path, записей=1)
    настройки = Настройки(llm_backend="ollama", llm_url=f"http://127.0.0.1:{закрытый_порт()}",
                          llm_queue_attempts=2, llm_queue_idle_s=1)
    db.llmq_put("job0")
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток.start()
    try:
        time.sleep(4.5)
    finally:
        поток.stop()
    строка = db.query_one("SELECT state, attempts FROM llm_queue")
    assert поток.failed >= 2, "поток не дошёл до повторов"
    assert строка["state"] == db.LLMQ_ЖДЁТ, строка["state"]
    assert строка["attempts"] == 0


def test_временные_и_постоянные_сбои_различаются(tmp_path: Path):
    клиент = LLMClient(Настройки(llm_backend="ollama",
                                 llm_url=f"http://127.0.0.1:{закрытый_порт()}"))
    with pytest.raises(LLMError) as отказ:
        клиент._http("GET", f"{клиент.url}/api/tags", None, timeout=2)
    assert отказ.value.временная is True
    for код, временная in ((503, True), (502, True), (429, True), (404, False), (400, False)):
        сервер = HTTPСервер(код, {"error": "x"})
        try:
            with pytest.raises(LLMError) as отказ:
                клиент._http("GET", f"{сервер.адрес}/api/tags", None, timeout=2)
            assert отказ.value.временная is временная, код
        finally:
            сервер.закрыть()
    медленный = HTTPСервер(200, {}, пауза=2.0)
    try:
        with pytest.raises(LLMError) as отказ:
            клиент._http("GET", f"{медленный.адрес}/x", None, timeout=0.5)
        assert отказ.value.временная is False, "тайм-аут — попытка: иначе вечный круг"
    finally:
        медленный.закрыть()


# ---------------------------------------------------------------------------
# Разбор ответа и проба модели
# ---------------------------------------------------------------------------

def test_ответ_с_пояснением_в_фигурных_скобках_разбирается():
    """Вырезка от первой «{» до последней «}» захватывала пояснение модели."""
    ответ = '{"summary": "Клиент спросил о заказе"}\nПримечание: поле {reason} пустое.'
    assert tasks.parse_json(ответ) == {"summary": "Клиент спросил о заказе"}
    ответ = 'Ответ {в формате JSON}: {"outcome": "решено", "детали": {"а": 1}}'
    assert tasks.parse_json(ответ) == {"outcome": "решено", "детали": {"а": 1}}
    with pytest.raises(LLMError):
        tasks.parse_json("никакого JSON {здесь} нет")


def test_проба_признаёт_только_ту_же_модель():
    """Скачана qwen3.5:9b, задана qwen3.5:27b — проба говорила «модель есть»."""
    assert not та_же_модель("qwen3.5:9b", "qwen3.5:27b")
    assert та_же_модель("qwen3:latest", "qwen3")
    assert та_же_модель("qwen3", "qwen3:latest")
    assert не_равны("hf.co/org/model:Q4", "hf.co/org/model:Q8")
    сервер = HTTPСервер(200, {"models": [{"name": "qwen3.5:9b"}]})
    try:
        клиент = LLMClient(Настройки(llm_backend="ollama", llm_url=сервер.адрес,
                                     llm_model="qwen3.5:27b"))
        assert клиент.probe(fresh=True)["model_known"] is False
    finally:
        сервер.закрыть()


def не_равны(а: str, б: str) -> bool:
    return not та_же_модель(а, б)


# ---------------------------------------------------------------------------
# Слот шлюза модели
# ---------------------------------------------------------------------------

class _Поймать:
    """Вместо StreamingResponse: отдаёт генератор тела как есть."""

    def __init__(self, content, **_к):
        self.content = content


def _шлюз(tmp_path: Path, monkeypatch):
    from asrhub.api import routes_llm_proxy as шлюз

    monkeypatch.setattr(шлюз, "StreamingResponse", _Поймать)
    db = Database(tmp_path / "gw.db")
    клиент = LLMClient(Настройки(llm_backend="stub", llm_max_concurrent=1), db)
    состояние = SimpleNamespace(db=db)
    запрос = {"model": "stub", "stream": True,
              "messages": [{"role": "user", "content": "Здравствуйте, вопрос по заказу."}]}
    return шлюз, клиент, состояние, запрос


def test_слот_шлюза_освобождается_если_ответ_не_начался(tmp_path: Path, monkeypatch):
    """Клиент ушёл до первого куска — генератор не стартовал, слот висел вечно."""
    шлюз, клиент, состояние, запрос = _шлюз(tmp_path, monkeypatch)
    monkeypatch.setattr(шлюз, "НЕ_НАЧАЛСЯ_С", 0.3)
    шлюз._потоком(состояние, клиент, шлюз._Учёт(), запрос)
    семафор = клиент._семафор()
    assert not семафор.acquire(blocking=False), "слот не занят на время ответа"
    time.sleep(0.9)
    assert семафор.acquire(timeout=1), "слот так и остался занятым"
    семафор.release()


def test_медленный_клиент_не_держит_слот_дольше_предела(tmp_path: Path, monkeypatch):
    """Клиент, который не читает ответ, держал единственный слот сколько угодно."""
    шлюз, клиент, состояние, запрос = _шлюз(tmp_path, monkeypatch)
    monkeypatch.setattr(шлюз, "_предел_потока", lambda _к: 0.3)
    ответ = шлюз._потоком(состояние, клиент, шлюз._Учёт(), запрос)
    первый = next(ответ.content)
    assert "data:" in первый
    time.sleep(0.9)
    семафор = клиент._семафор()
    assert семафор.acquire(timeout=1), "слот не освобождён по предельному сроку"
    семафор.release()
    остаток = "".join(ответ.content)
    assert "llm_slot_timeout" in остаток and остаток.rstrip().endswith("[DONE]")
    # Генератор, закончив, не отпускает слот второй раз (BoundedSemaphore упал бы).
    assert семафор.acquire(timeout=1)
    семафор.release()


# ---------------------------------------------------------------------------
# CRM: примечание один раз и с пересказом
# ---------------------------------------------------------------------------

def _настройки_crm(сервер: HTTPСервер, **поверх: Any) -> Настройки:
    return Настройки(crm_enabled=True, crm_kind="custom", crm_url=f"{сервер.адрес}/hook",
                     webhook_allow_internal=True, llm_tasks=["summary", "outcome", "actions"],
                     **поверх)


def test_примечание_ждёт_пересказа_и_уходит_один_раз(tmp_path: Path, crm_сервер: HTTPСервер):
    """Примечание уходило сразу после разбора содержания — с пустым пересказом.

    А каждый пересчёт разбора и каждое открытие карточки записи слали в
    карточку сделки новое примечание.
    """
    db = база(tmp_path, записей=1, crm_entity_id="77")
    настройки = _настройки_crm(crm_сервер)
    индекс = ContentIndex(db, настройки)
    задание = db.get_job("job0")
    индекс.on_job_completed("job0", задание, db.get_segments("job0"))
    assert crm_сервер.запросы == [], "примечание ушло до смыслового разбора"
    assert db.get_job("job0")["crm_status"] == db.CRM_ЖДЁТ_МОДЕЛЬ
    LLMWorker(db, настройки, LLMClient(настройки, db)).analyze_job("job0", force=True)
    assert len(crm_сервер.запросы) == 1
    тело = json.loads(crm_сервер.запросы[0]["body"])
    assert тело["summary"], "примечание ушло без пересказа"
    assert тело["entity_id"] == "77"
    assert db.get_job("job0")["crm_status"] == db.CRM_ОТПРАВЛЕНО
    # Пересчёты и повторный разбор моделью — без новых примечаний.
    индекс.analyze_job("job0")
    индекс.on_job_completed("job0", задание, db.get_segments("job0"))
    LLMWorker(db, настройки, LLMClient(настройки, db)).analyze_job("job0", force=True)
    assert len(crm_сервер.запросы) == 1


def test_без_модели_примечание_уходит_сразу(tmp_path: Path, crm_сервер: HTTPСервер):
    db = база(tmp_path, записей=1, crm_entity_id="78")
    настройки = _настройки_crm(crm_сервер, llm_backend="off")
    ContentIndex(db, настройки).on_job_completed("job0", db.get_job("job0"),
                                                 db.get_segments("job0"))
    assert len(crm_сервер.запросы) == 1


def test_досылка_не_ждёт_снятого_разбора(tmp_path: Path, crm_сервер: HTTPСервер):
    """Разбор сняли с очереди или он упал — примечание не должно ждать вечно."""
    db = база(tmp_path, записей=3, crm_entity_id="79")
    настройки = _настройки_crm(crm_сервер)
    for job_id in ("job0", "job1", "job2"):
        db.crm_mark(job_id, db.CRM_ЖДЁТ_МОДЕЛЬ)
        db.llmq_put(job_id)
    db.llmq_cancel("job0")                                   # сняли
    db.execute("UPDATE jobs SET crm_at=? WHERE id='job2'",  # ждёт слишком долго
               [time.time() - crm.ЖДАТЬ_МОДЕЛЬ_С - 60])
    assert crm.досылка(db, настройки) == 2
    отправлены = {json.loads(з["body"])["link"] for з in crm_сервер.запросы}
    assert отправлены == {"job0", "job2"}
    assert db.get_job("job1")["crm_status"] == db.CRM_ЖДЁТ_МОДЕЛЬ


def test_проверка_crm_не_шлёт_в_сделку_номер_один(data_dir: Path, crm_сервер: HTTPСервер):
    """Пустое поле сделки в проверке отправляло примечание в сделку №1."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    настройки = load()
    настройки.set("crm_enabled", True, source="api")
    настройки.set("crm_kind", "amocrm", source="api")
    настройки.set("crm_url", crm_сервер.адрес, source="api")
    настройки.set("crm_token", "t0ken", source="api")
    настройки.set("webhook_allow_internal", True, source="api")
    with TestClient(create_app(настройки, start_queue=False)) as клиент:
        отказ = клиент.post("/api/crm/test?dry_run=false")
        assert отказ.status_code == 400, отказ.text
        assert crm_сервер.запросы == []
        показ = клиент.post("/api/crm/test?dry_run=true").json()
        assert показ["entity_example"] is True and "/leads/1/notes" in показ["url"]
        отправлено = клиент.post("/api/crm/test?dry_run=false&entity_id=4242").json()
        assert отправлено["sent"] is True
        assert crm_сервер.запросы[-1]["path"] == "/api/v4/leads/4242/notes"


def test_свои_поля_amocrm_уходят_правкой_сделки():
    """Объект вместо массива на адрес примечаний ломал запись в amoCRM целиком."""
    данные = crm.собрать({"id": "j1", "text": "т"}, {"llm": {"summary": "Итог", "outcome": "решено"}})
    настройки = crm.Настройки(enabled=True, kind="amocrm", url="https://x.amocrm.ru",
                              token="t", fields={"summary": "123", "outcome": "OUTCOME"})
    запросы = crm.запросы(данные, настройки, entity_id="55")
    assert [(з.method, з.url.rsplit("/api/v4", 1)[1]) for з in запросы] == [
        ("POST", "/leads/55/notes"), ("PATCH", "/leads/55")]
    примечания = json.loads(запросы[0].body)
    assert isinstance(примечания, list) and примечания[0]["note_type"] == "common"
    поля = json.loads(запросы[1].body)["custom_fields_values"]
    assert {"field_id": 123, "values": [{"value": "Итог"}]} in поля
    assert {"field_code": "OUTCOME", "values": [{"value": "решено"}]} in поля


def test_отказ_своих_полей_не_отменяет_примечания(crm_сервер: HTTPСервер, monkeypatch):
    вызовы: list[str] = []

    def послать(запрос, настройки, allow_internal):
        вызовы.append(запрос.method)
        if запрос.method == "PATCH":
            raise crm.CRMError("CRM ответила 400: нет такого поля")
        return {"status": 200, "body": "{}"}

    monkeypatch.setattr(crm, "_послать", послать)
    настройки = crm.Настройки(enabled=True, kind="amocrm", url="https://x.amocrm.ru",
                              token="t", fields={"summary": "123"})
    ответ = crm.отправить({"summary": "Итог"}, настройки, entity_id="55")
    assert вызовы == ["POST", "PATCH"]
    assert ответ["status"] == 200 and "нет такого поля" in ответ["fields_error"]


def test_примечание_по_записи_уходит_один_раз(tmp_path: Path, crm_сервер: HTTPСервер):
    """Отметка «отправлено» по заданию держит и от повторного вызова, и от гонки."""
    db = база(tmp_path, записей=1, crm_entity_id="80")
    настройки = _настройки_crm(crm_сервер)
    assert crm.отправить_разбор(db, настройки, "job0") is not None
    assert crm.отправить_разбор(db, настройки, "job0") is None
    assert len(crm_сервер.запросы) == 1


def test_служба_отмечает_жизнь_подбирает_брошенное_и_досылает(tmp_path: Path, monkeypatch):
    """Разбор длинной записи — минуты; без отметки сосед счёл бы его брошенным."""
    from asrhub.llm import worker as модуль

    monkeypatch.setattr(модуль, "ОТМЕТКА_С", 0.1)
    monkeypatch.setattr(модуль, "ОБХОД_С", 0.2)
    досылки: list[int] = []
    monkeypatch.setattr(crm, "досылка", lambda db, settings: досылки.append(1) or 0)
    db = база(tmp_path, записей=2)
    настройки = Настройки(llm_queue_paused=True)
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток.start()
    try:
        db.llmq_put("job0")
        db.llmq_put("job1")
        db.llmq_take()                                       # своё, как бы идёт
        db.llmq_take(instance="сосед:1")
        давно = time.time() - 3600
        db.execute("UPDATE llm_queue SET heartbeat_at=?", [давно])
        time.sleep(1.0)
    finally:
        поток.stop()
    своё = db.query_one("SELECT state, heartbeat_at FROM llm_queue WHERE job_id='job0'")
    assert своё["state"] == db.LLMQ_ИДЁТ and своё["heartbeat_at"] > давно + 3000
    assert db.query_one("SELECT state FROM llm_queue WHERE job_id='job1'")["state"] == \
        db.LLMQ_ЖДЁТ
    assert досылки, "досылка примечаний CRM не запускалась"
