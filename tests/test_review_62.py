"""Заход 62: остаток главы 33 после 3.1.33.

Карта по свободной памяти, порядок «Результатов» по столбцу на сервере,
звонок со ссылкой на запись, опрос по входящему звонку.
"""
from __future__ import annotations

import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(КОРЕНЬ / "tests"))


# ---------------------------------------------------------------------------
# Карта по свободной памяти
# ---------------------------------------------------------------------------

def test_gpu_pick_by_free_memory_keeps_the_own_card_while_close(monkeypatch):
    from asrhub import job_queue as jq

    очередь = jq.JobQueue.__new__(jq.JobQueue)
    значения = {"gpu_devices": "cuda:0, cuda:1", "gpu_pick": "free_memory"}
    очередь.settings = type("Н", (), {"get": lambda s, k, d=None: значения.get(k, d)})()
    память = {"cuda:0": 2 << 30, "cuda:1": 20 << 30}
    monkeypatch.setattr("asrhub.job_queue_worker.свободная_память", lambda к: память.get(к))
    assert очередь._карта_воркера(0, {"device": "auto"}) == "cuda:1"
    память["cuda:0"] = 19 << 30            # своя почти как лучшая — остаётся своя
    assert очередь._карта_воркера(0, {"device": "auto"}) == "cuda:0"
    monkeypatch.setattr("asrhub.job_queue_worker.свободная_память", lambda к: None)
    assert очередь._карта_воркера(1, {"device": "auto"}) == "cuda:1", "без сведений — по номеру"
    значения["gpu_pick"] = "worker"
    monkeypatch.setattr("asrhub.job_queue_worker.свободная_память", lambda к: память.get(к))
    assert очередь._карта_воркера(0, {"device": "auto"}) == "cuda:0"


# ---------------------------------------------------------------------------
# «Результаты»: порядок всего списка по столбцу
# ---------------------------------------------------------------------------

def test_results_columns_sort_on_the_server(tmp_path):
    from asrhub.db import Database

    база = Database(tmp_path / "asrhub.db")
    try:
        for н, (имя, слов) in enumerate([("б.wav", 10), ("а.wav", 30), ("в.wav", 20)]):
            ид = база.create_job({"id": f"j{н}", "filename": имя, "model": "demo",
                                  "engine": "demo"})
            база.update_job(ид, status="completed", words_count=слов)
        def порядок(order):
            return [з["filename"] for з in база.list_jobs(status="completed", order=order)]
        assert порядок("words_count DESC") == ["а.wav", "в.wav", "б.wav"]
        assert порядок("filename ASC") == ["а.wav", "б.wav", "в.wav"]
        # Чужой порядок — как по умолчанию, а не текст в SQL.
        assert len(порядок("filename; DROP TABLE jobs")) == 3
    finally:
        база.close()
    app = (КОРЕНЬ / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert "th[data-order]" in app and "['words_count', 'Слов', 'num']" in app


# ---------------------------------------------------------------------------
# Звонок со ссылкой на запись
# ---------------------------------------------------------------------------

class _Записи(BaseHTTPRequestHandler):
    тело = b""

    def do_GET(self) -> None:                               # noqa: N802
        if self.path.startswith("/rec/"):
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(self.тело)))
            self.end_headers()
            self.wfile.write(self.тело)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_a) -> None:
        return


@pytest.fixture()
def сервер_записей():
    from test_dialplan import wav

    _Записи.тело = wav(4.0)
    сервер = ThreadingHTTPServer(("127.0.0.1", 0), _Записи)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    yield f"http://127.0.0.1:{сервер.server_address[1]}"
    сервер.shutdown()


def test_call_with_recording_url_downloads_the_recording(data_dir, monkeypatch, tmp_path,
                                                         сервер_записей):
    from fastapi.testclient import TestClient
    from test_dialplan import приложение, прислать, станция, строка

    app = приложение(data_dir, monkeypatch, [станция(recordings_dir=str(tmp_path))])
    with TestClient(app) as клиент:
        клиент.hub = app.state.hub
        # Внутренний адрес без разрешения — отказ проверки адреса.
        отказ = прислать(клиент, "1793000000.51", файл=None,
                         recording_url=f"{сервер_записей}/rec/1.wav")
        assert отказ.status_code == 400, отказ.text
        app.state.hub.settings.set("recording_url_allow_internal", True)
        ответ = прислать(клиент, "1793000000.52", файл=None,
                         recording_url=f"{сервер_записей}/rec/2.wav")
        assert ответ.status_code == 200, ответ.text
        тело = ответ.json()
        assert тело["result"] == "queued" and тело["bytes"] > 1000, тело
        assert строка(клиент, "main:1793000000.52")["job_id"]
        # Ссылка, по которой ничего нет, — повторимый отказ (станция повторит).
        нет = прислать(клиент, "1793000000.53", файл=None,
                       recording_url=f"{сервер_записей}/nope")
        assert нет.status_code in (503, 415), нет.text
        # Чужая схема — сразу отказ.
        файл = прислать(клиент, "1793000000.54", файл=None, recording_url="file:///etc/passwd")
        assert файл.status_code == 400


# ---------------------------------------------------------------------------
# Опрос по входящему звонку
# ---------------------------------------------------------------------------

def test_phone_answers_an_incoming_call_and_hands_it_over():
    import socket
    import uuid

    from asrhub.qc import sip

    станция = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    станция.bind(("127.0.0.1", 0))
    станция.settimeout(3)
    порт = станция.getsockname()[1]
    телефон = sip.Телефон("127.0.0.1", порт, пользователь="900", пароль="x",
                          местный="127.0.0.1")
    получено: list = []
    готово = threading.Event()

    def обработчик(разговор, запрос):
        получено.append((разговор, запрос.взять("x-asrhub-ext")))
        готово.set()

    try:
        телефон.входящие = обработчик
        адрес_телефона = ("127.0.0.1", телефон._сокет.getsockname()[1])
        тело = (b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                b"m=audio 40000 RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\n"
                b"a=rtpmap:101 telephone-event/8000\r\n")
        звонок = f"{uuid.uuid4().hex}@pbx"
        invite = sip.собрать("INVITE sip:900@127.0.0.1 SIP/2.0", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{порт};branch=z9hG4bK{uuid.uuid4().hex[:8]}"),
            ("From", "<sip:79991234567@127.0.0.1>;tag=pbx1"), ("To", "<sip:900@127.0.0.1>"),
            ("Call-ID", звонок), ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:pbx@127.0.0.1:{порт}>"), ("X-ASRHub-Ext", "101"),
            ("Content-Type", "application/sdp")], тело)
        станция.sendto(invite, адрес_телефона)
        ответы = []
        конец = time.time() + 3
        while time.time() < конец and not any(о.код == 200 for о in ответы):
            данные, _ = станция.recvfrom(65535)
            сообщение = sip.разобрать(данные)
            if сообщение is not None and сообщение.ответ:
                ответы.append(сообщение)
        двести = next(о for о in ответы if о.код == 200)
        строка_m = next(с for с in двести.тело.decode().splitlines() if с.startswith("m=audio"))
        assert строка_m.split()[3:] == ["0", "101"], строка_m   # только PCMU — как предложено
        assert готово.wait(3)
        разговор, добавочный = получено[0]
        assert добавочный == "101" and разговор.кодек == "PCMU"
        # Трубку кладёт сервер — станция получает BYE в том же диалоге.
        threading.Thread(target=разговор.положить, daemon=True).start()
        конец = time.time() + 3
        bye = None
        while time.time() < конец and bye is None:
            данные, откуда = станция.recvfrom(65535)
            сообщение = sip.разобрать(данные)
            if сообщение is not None and not сообщение.ответ and сообщение.метод == "BYE":
                bye = сообщение
        assert bye is not None and bye.взять("call-id") == звонок
        assert "tag=pbx1" in bye.взять("to")
        станция.sendto(sip.собрать("SIP/2.0 200 OK", [
            ("Via", bye.взять("via")), ("From", bye.взять("from")), ("To", bye.взять("to")),
            ("Call-ID", звонок), ("CSeq", bye.взять("cseq"))]), откуда)
    finally:
        телефон.закрыть()
        станция.close()


def test_inbound_survey_is_recorded_with_operator(data_dir, monkeypatch):
    from asrhub.qc import survey
    from test_qc import приложение
    from test_review_61 import Трубка

    class Линия(Трубка):
        def положить(self) -> None:
            self.жив = False

        def запись_стерео(self) -> bytes:
            return b""

    class Запрос:
        заголовки = {"from": "\"Клиент\" <sip:79990001122@pbx>;tag=a", "x-asrhub-ext": "101",
                     "x-asrhub-call": ""}

        def взять(self, имя, запасное=""):
            return self.заголовки.get(имя, запасное)

    _, hub = приложение(data_dir, monkeypatch, qc_inbound=True)
    hub.qc._входящий(Линия(["4", "1"]), Запрос())
    строка = hub.db.query_one("SELECT * FROM qc_surveys WHERE kind='входящий'")
    assert строка is not None
    опрос = survey.опрос_наружу(строка)
    assert опрос["status"] == survey.ПРОЙДЕН and опрос["ext"] == "101", опрос
    assert опрос["number"] == "79990001122" and [о["digit"] for о in опрос["answers"]] == [4, 1]
