"""Заход 61: остаток главы 33 после 3.1.32.

Сервер (дочерний процесс на воркер, пул видеокарт, пробуждение одного
воркера, события между серверами), контроль качества, телефония,
веб-интерфейс и документация.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parents[1]


@pytest.fixture()
def database(tmp_path: Path):
    from asrhub.db import Database

    база = Database(tmp_path / "asrhub.db")
    yield база
    база.close()


# ---------------------------------------------------------------------------
# Дочерний процесс на воркер
# ---------------------------------------------------------------------------

def _жив(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_process_isolation_runs_jobs_in_a_child_and_kills_a_hung_one(
        data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сторож снимал зависшее задание, но поток и память жили до перезапуска."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient
    from test_review_54_queue import _загрузка, _поставить, ждать

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "1")
    monkeypatch.delenv("ASRHUB_DEMO_HANG_S", raising=False)
    with TestClient(create_app(load(), start_queue=False)) as клиент:
        state = клиент.app.state.hub
        state.settings.set("worker_isolation", "process")
        queue = state.queue
        queue.start()
        try:
            первое = _поставить(state, _загрузка(state, "раз.wav", 3.0))
            assert ждать(lambda: state.db.get_job(первое["id"])["status"] == "completed", 120), \
                state.db.get_job(первое["id"])
            дочерний = queue._дочерние[0]
            pid = дочерний.pid
            assert pid and pid != os.getpid() and _жив(pid)
            assert (state.db.get_job(первое["id"])["text"] or "").strip()

            # Движок зависает внутри вызова и молчит.
            monkeypatch.setenv("ASRHUB_DEMO_HANG_S", "300")
            дочерний.завершить("проверка")       # следующий процесс — с зависанием
            прежний_поток = queue._потоки[0]
            висит = _поставить(state, _загрузка(state, "висит.wav", 3.2))
            assert ждать(lambda: queue._states[0].job_id == висит["id"]
                         and дочерний.жив(), 60)
            time.sleep(1.0)
            состояние = queue._states[0]
            состояние.срок_с = 1.0
            состояние.started_at -= 1000
            состояние.доклад -= 1000
            зависший = дочерний.pid
            assert queue._сторож_зависших() == 1
            assert ждать(lambda: state.db.get_job(висит["id"])["status"] == "failed", 30)
            строка = state.db.get_job(висит["id"])
            assert строка["error_code"] == "engine_hung", строка
            # Процесс завершён — память ушла вместе с ним, поток не менялся.
            assert ждать(lambda: not _жив(зависший), 15)
            assert queue._потоки[0] is прежний_поток and прежний_поток.is_alive()

            # Следом — новый процесс, задание проходит.
            monkeypatch.delenv("ASRHUB_DEMO_HANG_S")
            следом = _поставить(state, _загрузка(state, "следом.wav", 3.4))
            assert ждать(lambda: state.db.get_job(следом["id"])["status"] == "completed", 120)
            assert дочерний.pid not in (None, зависший)
        finally:
            queue.stop()
        assert not дочерний.жив(), "процесс воркера пережил остановку очереди"


def test_child_errors_come_back_as_asrhub_errors():
    from asrhub import errors as E
    from asrhub.worker_process import _ошибка_из, _сведения_об

    исходная = E.EngineHung(120, миг_доклада=60)
    вернулась = _ошибка_из(_сведения_об(исходная))
    assert isinstance(вернулась, E.EngineHung) and вернулась.code == "engine_hung"
    assert вернулась.message == исходная.message and вернулась.retryable is False
    чужая = _ошибка_из(_сведения_об(MemoryError("CUDA out of memory")))
    assert "CUDA out of memory" in str(чужая)
    assert E.classify_exception(чужая).code in ("out_of_memory", "gpu_out_of_memory")


def test_worker_process_died_is_retryable_and_documented():
    from asrhub.errors import WorkerProcessDied

    assert WorkerProcessDied("x").retryable is True
    assert "`worker_process_died`" in (КОРЕНЬ / "docs" / "08-api.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Пул видеокарт
# ---------------------------------------------------------------------------

def test_gpu_pool_gives_each_worker_its_card():
    from asrhub.job_queue import JobQueue

    очередь = JobQueue.__new__(JobQueue)
    очередь.settings = type("Н", (), {"get": lambda s, k, d=None: {
        "gpu_devices": "cuda:0, cuda:1"}.get(k, d)})()
    assert очередь._карта_воркера(0, {"device": "auto"}) == "cuda:0"
    assert очередь._карта_воркера(1, {"device": "cuda"}) == "cuda:1"
    assert очередь._карта_воркера(2, {}) == "cuda:0"
    # Явная карта и процессор — как просили.
    assert очередь._карта_воркера(1, {"device": "cuda:0"}) == ""
    assert очередь._карта_воркера(1, {"device": "cpu"}) == ""
    очередь.settings = type("Н", (), {"get": lambda s, k, d=None: d})()
    assert очередь._карта_воркера(1, {"device": "auto"}) == ""


def test_gpu_pool_keeps_one_model_instance_per_card():
    from asrhub.engines import EngineRegistry

    реестр = EngineRegistry(4, 0)
    общие = {"model": "demo-simulator", "engine": "demo"}
    первый = реестр.get({**общие, "device": "cuda:0"})
    второй = реестр.get({**общие, "device": "cuda:1"})
    assert первый is not второй
    assert реестр.get({**общие, "device": "cuda:1"}) is второй


# ---------------------------------------------------------------------------
# Пробуждение одного воркера и события между серверами
# ---------------------------------------------------------------------------

def test_alarm_wakes_one_worker_and_stop_wakes_all():
    import threading

    from asrhub.job_queue import Будильник

    б = Будильник()
    проснулись: list[int] = []

    def ждать(номер: int) -> None:
        if б.wait(5):
            проснулись.append(номер)

    потоки = [threading.Thread(target=ждать, args=(н,)) for н in range(4)]
    for п in потоки:
        п.start()
    time.sleep(0.3)
    б.set()
    time.sleep(0.5)
    assert len(проснулись) == 1, проснулись
    б.set_all()
    for п in потоки:
        п.join(5)
    assert len(проснулись) == 4
    # Пачка сигналов копится, но с пределом.
    б2 = Будильник()
    for _ in range(100):
        б2.set()
    assert б2._сигналов == Будильник.ПРЕДЕЛ


def test_events_of_a_neighbour_server_reach_local_subscribers(database, monkeypatch):
    from asrhub import db as db_module
    from asrhub.events_bridge import МостСобытий

    разослано: list[tuple[str, dict]] = []
    мост = МостСобытий(database, lambda тип, данные: разослано.append((тип, данные)))
    database.create_job({"id": "j1", "filename": "a.wav", "owner": "анна", "model": "demo",
                         "engine": "demo"})
    assert мост.шаг() == 0          # первый проход только запоминает конец
    # Своё событие не пересылается: его уже разослала своя очередь.
    database.add_event("j1", "started", "своё")
    assert мост.шаг() == 0
    # Событие соседа — пересылается с адресом владельца.
    monkeypatch.setattr(db_module, "INSTANCE_ID", "сосед:1-abc")
    database.add_event("j1", "started", "начато у соседа")
    database.update_job("j1", status="running", instance_id="сосед:1-abc", progress=0.4,
                        stage="распознавание")
    monkeypatch.setattr(db_module, "INSTANCE_ID", "этот:2-def")
    assert мост.шаг() == 2, разослано
    типы = dict(разослано)
    assert типы["job.started"]["_owner"] == "анна" and типы["job.started"]["remote"]
    assert типы["job.progress"]["progress"] == 0.4
    # Тот же ход второй раз не рассылается.
    assert мост.шаг() == 0


def test_events_table_has_instance_column(database):
    колонки = {с["name"] for с in database.query("PRAGMA table_info(events)")}
    assert "instance" in колонки


# ---------------------------------------------------------------------------
# Контроль качества: ветвления, комментарии, голосовые ответы, автоответчик
# ---------------------------------------------------------------------------

def _громко(секунд: float) -> bytes:
    import math
    import struct

    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * н / 8000)))
                    for н in range(int(секунд * 8000)))


class _Ф:
    def звук(self, кодек: str) -> bytes:
        return b"\xd5" * 160


class Трубка:
    """Разговор без сети: кнопки и речь клиента по сценарию, время — по звуку."""

    кодек = "PCMA"

    def __init__(self, ответы: list, лента: bytes = b"") -> None:
        self.ответы = list(ответы)
        self.лента = bytearray(лента)
        self.звук = bytearray()
        self.жив = True
        self.фраз = 0

    def _идёт(self, с: float) -> None:
        n = int(с * 8000) * 2
        кусок = bytes(self.лента[:n])
        del self.лента[:n]
        self.звук += кусок + bytes(n - len(кусок))

    def начать_запись(self) -> None:
        pass

    def сейчас_с(self) -> float:
        return len(self.звук) / 16000

    def звук_клиента(self, *, с: float = 0.0) -> bytes:
        return bytes(self.звук[int(с * 8000) * 2:])

    def молчать(self, с: float) -> str:
        self._идёт(с)
        return ""

    def сбросить_кнопки(self) -> None:
        pass

    def проиграть(self, звук: bytes, *, прерывать: bool = True) -> str:
        self.фраз += 1
        self._идёт(0.5)
        return ""

    def ждать_кнопку(self, с: float) -> str:
        if с == 0:
            return ""
        if self.лента or not self.ответы:
            self._идёт(min(с, 0.2) if self.лента else с)
            return ""
        ответ = self.ответы.pop(0)
        if isinstance(ответ, tuple):            # («голос», секунд) — клиент говорит
            self.лента += _громко(ответ[1]) + bytes(int(1.1 * 8000) * 2)
            self._идёт(0.2)
            return ""
        self._идёт(0.2)
        return ответ


def _группа(**поверх):
    from asrhub.qc import survey

    return survey.разобрать_группы([{"name": "Г", "extensions": "101", **поверх}])[0]


def test_branching_asks_follow_up_only_after_a_low_score():
    from asrhub.qc import survey

    группа = _группа(questions=[
        {"text": "Оценка?", "min": 1, "max": 5},
        {"text": "Что не так?", "min": 1, "max": 3, "when": {"question": 1, "below": 3}}])
    звуки = {т: _Ф() for т in группа.фразы()}
    хорошо = survey.провести(Трубка(["5", "2"]), группа, звуки, ждать_кнопку_с=1)
    assert [о["digit"] for о in хорошо["answers"]] == [5, None]
    assert хорошо["answers"][1]["skipped"] is True
    плохо = survey.провести(Трубка(["2", "3"]), группа, звуки, ждать_кнопку_с=1)
    assert [о["digit"] for о in плохо["answers"]] == [2, 3]
    # Условие ссылается только на прежний вопрос с оценкой.
    with pytest.raises(ValueError, match="when"):
        survey.разобрать_группы([{"name": "Г", "questions": [
            {"text": "a", "when": {"question": 2, "below": 3}}, {"text": "b"}]}])
    with pytest.raises(ValueError, match="одно из"):
        survey.разобрать_группы([{"name": "Г", "questions": [
            {"text": "a"}, {"text": "b", "when": {"question": 1}}]}])


def test_voice_answers_and_comment_are_recognised():
    from asrhub.qc import survey

    группа = _группа(voice_answers=True, questions=[
        {"text": "Оценка?", "min": 1, "max": 5},
        {"text": "Решён?", "min": 1, "max": 2, "words": {"да": 1, "нет": 2}},
        {"text": "Что улучшить?", "kind": "comment", "seconds": 5}])
    звуки = {т: _Ф() for т in группа.фразы()}
    сказано = iter(["Ну, пятёрка", "да, решён", "быстрее отвечать"])
    итог = survey.провести(Трубка([("голос", 0.8), ("голос", 0.6), ("голос", 1.0)]), группа,
                           звуки, ждать_кнопку_с=3, распознать=lambda pcm: next(сказано))
    ответы = итог["answers"]
    assert [о["digit"] for о in ответы[:2]] == [5, 1], ответы
    assert ответы[0]["voice"] == "Ну, пятёрка"
    assert ответы[2]["comment"] == "быстрее отвечать" and ответы[2]["kind"] == "comment"
    # Оценка — по шкалам; комментарий её не трогает.
    assert survey.оценка(ответы)[2] == 2
    # Без распознавателя комментарий пропускается, а не ломает опрос.
    без = survey.провести(Трубка(["4", "1"]), группа, звуки, ждать_кнопку_с=1)
    assert без["answers"][2].get("skipped") is True


def test_numbers_in_speech_map_to_the_scale():
    from asrhub.qc import survey

    вопрос = survey.Вопрос("x", 1, 5)
    assert вопрос.цифра_из_речи("наверное четыре") == 4
    assert вопрос.цифра_из_речи("на 5") == 5
    assert вопрос.цифра_из_речи("девять") is None        # вне шкалы
    assert вопрос.цифра_из_речи("не знаю") is None


def test_answering_machine_is_not_surveyed():
    from asrhub.qc import survey

    группа = _группа(questions=[{"text": "Оценка?", "min": 1, "max": 5}])
    звуки = {т: _Ф() for т in группа.фразы()}
    машина = Трубка(["5"], лента=_громко(4.5))
    итог = survey.провести(машина, группа, звуки, ждать_кнопку_с=1, автоответчик=True)
    assert итог.get("machine") is True and машина.фраз == 0
    человек = Трубка(["5"], лента=bytes(int(0.6 * 8000) * 2) + _громко(0.5))
    итог = survey.провести(человек, группа, звуки, ждать_кнопку_с=1, автоответчик=True)
    assert not итог.get("machine") and итог["answers"][0]["digit"] == 5
    тишина = Трубка(["4"])
    assert survey.определить_автоответчик(тишина) == "тишина"


def test_group_voice_sample_must_be_an_uploaded_record():
    from asrhub.qc import survey

    assert _группа(voice_sample="sales.wav").voice_sample == "sales.wav"
    with pytest.raises(ValueError, match="voice_sample"):
        survey.разобрать_группы([{"name": "Г", "voice_sample": "../etc/passwd"}])


def test_reinvite_answer_keeps_codec_200_repeats_until_ack_and_ack_has_auth(
        data_dir, monkeypatch):
    import sys

    sys.path.insert(0, str(КОРЕНЬ / "tests"))
    from asrhub.qc import sip, survey
    from qc_pbx import МиниАТС
    from test_qc import приложение

    станция = МиниАТС(кнопки=["5", "1"], перепригласить=True)
    try:
        _, hub = приложение(data_dir, monkeypatch, станция.порт, qc_record=True)
        итог = hub.qc.пробный_звонок("101", "Продажи")
        номер = итог["id"]
        конец = time.time() + 40
        while time.time() < конец:
            строка = hub.db.query_one("SELECT * FROM qc_surveys WHERE id=?", (номер,))
            if строка["status"] in survey.ИТОГОВЫЕ:
                break
            time.sleep(0.2)
        assert строка["status"] in survey.ИТОГОВЫЕ, dict(строка)
        # 200 на повторное приглашение повторялся, пока не пришёл ACK.
        assert len(станция.ответы_на_повтор) >= 3
        описание = станция.ответы_на_повтор[0].тело.decode()
        строка_m = next(с for с in описание.splitlines() if с.startswith("m=audio"))
        assert строка_m.split()[3:] == [str(sip.НОМЕР_КОДЕКА["PCMA"]), "101"], строка_m
        # ACK на 200 к INVITE — с теми же учётными данными (первый ACK — на
        # запрос пароля 401, он без них).
        assert станция.ack_с_паролем[-1] is True, станция.ack_с_паролем
        # Запись опроса: стерео WAV, путь наружу не уходит.
        assert строка["recording"] and Path(строка["recording"]).is_file()
        наружу = survey.опрос_наружу(строка)
        assert наружу["has_recording"] is True and "recording" not in наружу
        import wave

        with wave.open(строка["recording"]) as файл:
            assert файл.getnchannels() == 2 and файл.getframerate() == 8000
            assert файл.getnframes() > 8000
    finally:
        станция.закрыть()


def test_survey_audio_endpoint_respects_scope(data_dir, monkeypatch):
    import sys

    sys.path.insert(0, str(КОРЕНЬ / "tests"))
    from fastapi.testclient import TestClient
    from test_qc import АДМИН, приложение

    app, hub = приложение(data_dir, monkeypatch)
    каталог = hub.qc.каталог_записей_опросов()
    каталог.mkdir(parents=True, exist_ok=True)
    файл = каталог / "s1.wav"
    файл.write_bytes(b"RIFF0000WAVE")
    hub.db.execute("INSERT INTO qc_surveys (id, call_key, number, owner, status, created_at, "
                   "recording) VALUES ('s1','k1','79990000000','sales','пройден',?,?)",
                   (time.time(), str(файл)))
    with TestClient(app) as клиент:
        assert клиент.get("/api/qc/surveys/s1/audio", headers=АДМИН).status_code == 200
        assert клиент.get("/api/qc/surveys/s1/audio",
                          headers={"X-API-Key": "ah_user"}).status_code == 404
    # Удаление по требованию убирает и запись.
    hub.db.erase_qc("79990000000")
    assert not файл.exists()


def test_busy_on_our_second_line_is_a_station_limit_not_the_client():
    import threading

    from asrhub.qc import survey
    from asrhub.qc.sip import Занято

    кк = survey.КонтрольКачества.__new__(survey.КонтрольКачества)
    кк.settings = type("Н", (), {"get": lambda s, k, d=None: {"qc_parallel": 3}.get(k, d)})()
    кк._замок_линий = threading.Lock()
    кк._линий = (0, 0.0)
    живой = threading.Thread(target=time.sleep, args=(2,))
    живой.start()
    кк._идут = {"a": живой}
    assert not кк._упёрлись_в_линии(Занято("занято", 486)), "один свой звонок — это клиент"
    кк._идут = {"a": живой, "b": живой}
    assert кк._упёрлись_в_линии(Занято("занято", 486))
    assert кк._предел_линий() == 1
    живой.join()


# ---------------------------------------------------------------------------
# Телефония: события очередей из AMI, код «не записывать», AudioSocket
# ---------------------------------------------------------------------------

def test_ami_queue_events_map_to_queue_log_rows():
    from asrhub.telephony import queue_log as Q

    вход = Q.из_события_ami({"Event": "QueueCallerJoin", "Queue": "sales", "Uniqueid": "1.1",
                             "Timestamp": "1700000000.5"})
    assert вход["event"] == "ENTERQUEUE" and вход["ts"] == 1700000000.5 and вход["agent"] == ""
    бросил = Q.из_события_ami({"Event": "QueueCallerAbandon", "Queue": "sales",
                               "Uniqueid": "1.2", "HoldTime": "42"})
    assert бросил["event"] == "ABANDON" and бросил["wait_s"] == 42
    ответил = Q.из_события_ami({"Event": "AgentConnect", "Queue": "sales", "Uniqueid": "1.1",
                                "Interface": "PJSIP/101", "HoldTime": "12", "RingTime": "4"})
    assert ответил["event"] == "CONNECT" and ответил["agent"] == "101"
    assert ответил["wait_s"] == 12 and ответил["ring_s"] == 4
    не_взял = Q.из_события_ami({"Event": "AgentRingNoAnswer", "Queue": "sales",
                                "Interface": "Local/102@from-queue/n", "RingTime": "15000"})
    assert не_взял["event"] == "RINGNOANSWER" and не_взял["ring_s"] == 15.0
    assert не_взял["agent"] == "102"
    конец = Q.из_события_ami({"Event": "AgentComplete", "Queue": "sales", "Uniqueid": "1.1",
                              "Interface": "PJSIP/101", "HoldTime": "12", "TalkTime": "300",
                              "Reason": "caller"})
    assert конец["event"] == "COMPLETECALLER" and конец["talk_s"] == 300
    assert Q.из_события_ami({"Event": "Newchannel"}) is None
    свод = Q.свод([вход, бросил, ответил, не_взял, конец])
    очередь = свод["queues"][0]
    assert очередь["answered"] == 1 and очередь["abandoned"] == 1 and очередь["ringnoanswer"] == 1


def test_ami_asks_for_queue_events_only_without_a_visible_queue_log(tmp_path):
    from types import SimpleNamespace

    from asrhub.telephony.importer import Импортёр

    журнал = tmp_path / "queue_log"
    импорт = Импортёр.__new__(Импортёр)
    импорт.станция = SimpleNamespace(queue_log="", путь=lambda поле: None)
    assert импорт._классы_событий_ami() == "cdr,agent"
    журнал.write_text("")
    импорт.станция = SimpleNamespace(queue_log=str(журнал), путь=lambda поле: журнал)
    assert импорт._классы_событий_ami() == "cdr"


def test_in_call_norec_code_goes_to_features_and_dialplan():
    from asrhub.telephony import dialplan_config as К

    без = К.собрать(К.параметры(station="main"))
    assert "features" not in {ф["id"] for ф in без["files"]}
    п = К.параметры(station="main", norec_code="*7")
    файлы = {ф["id"]: ф["text"] for ф in К.собрать(п)["files"]}
    assert "asrhub_norec_in => *7,peer/callee,Gosub(asrhub-norec,s,1)" in файлы["features"]
    assert "DYNAMIC_FEATURES" in файлы["dialplan"]
    with pytest.raises(ValueError, match="четырёх"):
        К.параметры(station="main", norec_code="*7;rm")


def test_audiosocket_call_is_recognised_live(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Живой звук разговора по AudioSocket — фразы на лету и итог в ленте."""
    import socket
    import uuid

    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.streaming import tone
    from asrhub.telephony.audiosocket import ВИД_ЗВУК, ВИД_ИД, ВИД_КОНЕЦ, в_16к, кадр
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    with socket.socket() as пробный:
        пробный.bind(("127.0.0.1", 0))
        порт = пробный.getsockname()[1]
    настройки = load()
    настройки.set("audiosocket_port", порт)
    настройки.set("audiosocket_host", "127.0.0.1")
    app = create_app(настройки, start_queue=False)
    события: list[tuple[str, dict]] = []
    hub = app.state.hub
    with TestClient(app) as клиент:
        исходная = клиент.app.state.hub.audiosocket.опубликовать
        клиент.app.state.hub.audiosocket.опубликовать = \
            lambda тип, данные: (события.append((тип, данные)), исходная(тип, данные))
        ид = uuid.uuid4()
        звук16 = tone(4.0)
        звук8 = звук16[::2][: len(звук16) // 2]
        with socket.create_connection(("127.0.0.1", порт), timeout=10) as сокет:
            сокет.sendall(кадр(ВИД_ИД, ид.bytes))
            for н in range(0, len(звук8) - 319, 320):
                сокет.sendall(кадр(ВИД_ЗВУК, звук8[н:н + 320]))
            сокет.sendall(кадр(ВИД_КОНЕЦ))
        конец = time.time() + 30
        while time.time() < конец and not any(д.get("type") == "done" for _, д in события):
            time.sleep(0.1)
        типы = [д.get("type") for _, д in события]
        assert типы[0] == "ready" and "done" in типы, типы
        assert all(тип == "call.live" and д["uuid"] == str(ид) for тип, д in события)
        # Адресат — только администраторы (владелец не задан).
        assert all(д["_owner"] == "" for _, д in события)
        итог = [е for е in hub.db.get_events(limit=20) if е["kind"] == "audiosocket"]
        assert итог and итог[0]["data"]["uuid"] == str(ид)
        живые = клиент.get("/api/telephony/live")        # проверки без входа — администратор
        assert живые.status_code == 200 and живые.json()["enabled"] is True
    assert len(в_16к(b"\x00\x01" * 160)) == 640


# ---------------------------------------------------------------------------
# Веб: политика содержимого, проверки JS
# ---------------------------------------------------------------------------

def test_page_csp_has_no_unsafe_inline_scripts_and_markup_has_no_inline_handlers():
    import re

    from asrhub.api.app import _ПОЛИТИКА_СТРАНИЦЫ

    скрипты = next(ч for ч in _ПОЛИТИКА_СТРАНИЦЫ.split(";") if "script-src" in ч)
    assert "unsafe-inline" not in скрипты and "unsafe-eval" not in скрипты
    for имя in ("app.js", "charts.js", "index.html"):
        текст = (КОРЕНЬ / "server" / "asrhub" / "web" / имя).read_text(encoding="utf-8")
        # Встроенный обработчик в разметке (кроме примера в комментарии к jsArg).
        найдено = [м.group(0) for м in re.finditer(r'\son(click|keydown|change|input|load|error)="(?!…|f\()', текст)]
        assert not найдено, (имя, найдено[:3])
    index = (КОРЕНЬ / "server" / "asrhub" / "web" / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", index), "встроенный <script> в index.html"


def test_js_helpers_pass_node_test():
    import shutil
    import subprocess

    узел = shutil.which("node")
    if узел is None:
        pytest.skip("нет node")
    итог = subprocess.run([узел, "--test", str(КОРЕНЬ / "tests" / "js" / "helpers.test.mjs")],
                          capture_output=True, text=True, timeout=120, cwd=str(КОРЕНЬ))
    assert итог.returncode == 0, итог.stdout[-2000:] + итог.stderr[-2000:]


def test_eslint_config_is_clean_when_eslint_is_available():
    import os
    import shutil
    import subprocess

    eslint = os.environ.get("ASRHUB_ESLINT") or shutil.which("eslint")
    if not eslint:
        pytest.skip("ESLint не установлен (npm i -D eslint@9 или ASRHUB_ESLINT)")
    итог = subprocess.run([eslint, "--config", str(КОРЕНЬ / "eslint.config.mjs"),
                           "server/asrhub/web"], capture_output=True, text=True, timeout=300,
                          cwd=str(КОРЕНЬ))
    assert итог.returncode == 0, итог.stdout[-3000:] + итог.stderr[-1000:]
