"""Заход 54: очередь заданий, уведомления, планировщик, поток, остановка.

По находкам ревизии 3.1.20 (отчёт «05-queue»): «--no-queue», начинающий
считать; уведомления без досылки; «оживший» экземпляр, стирающий чужое;
задание навсегда «выполняется»; поздняя отмена повтора без отката; политики
deadline и fair_share; «Повторить все неудавшиеся», обрывающийся на первом;
temp_dir и брошенный tmp/; классификация ошибок и утечка путей; выгрузка
моделей посреди задания; клон из кеша; остановка сервера; поток диктовки.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import logging
import math
import socket
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from asrhub import job_queue as jq


def wav(путь: Path, секунд: float = 1.0) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(и / 8))) for и in range(int(16000 * секунд))))
    return путь


def ждать(условие, срок: float = 30.0) -> Any:
    конец = time.monotonic() + срок
    while time.monotonic() < конец:
        значение = условие()
        if значение:
            return значение
        time.sleep(0.05)
    return None


def подмена(*, hook=None, fail=None, steps: int = 3, сегменты=None):
    """Быстрая подмена `process_job`: доклады прогресса, проверки отмены."""
    from asrhub.errors import JobCancelled
    from asrhub.processor import ProcessOutcome

    вызовы: list[dict[str, Any]] = []

    def process_job(source, settings, registry, *, workdir, outdir, basename, filename="",
                    progress=None, cancelled=None, deadline=None, timeout_s=0,
                    measure_memory=False, reset_memory=False):
        вызов = {"source": str(source), "workdir": Path(workdir), "outdir": Path(outdir),
                 "batch_size": settings.get("batch_size"), "owner": settings.get("_owner")}
        вызовы.append(вызов)
        (Path(workdir) / "prepared.wav").write_bytes(b"x" * 10)
        for и in range(steps):
            if cancelled is not None and cancelled():
                raise JobCancelled("Задание отменено пользователем.")
            if progress is not None:
                progress((и + 1) / (steps + 1), "распознавание")
        if fail is not None:
            исключение = fail(вызов) if callable(fail) else fail
            if исключение is not None:
                raise исключение
        Path(outdir).mkdir(parents=True, exist_ok=True)
        (Path(outdir) / f"{basename}.txt").write_text("привет мир", encoding="utf-8")
        if hook is not None:
            hook(вызов)
        return ProcessOutcome(
            text="привет мир", language="ru", duration_s=1.0,
            segments=list(сегменты or [{"start": 0.0, "end": 1.0, "text": "привет"},
                                       {"start": 1.0, "end": 2.0, "text": "мир"}]),
            timings={"inference": 0.1}, stats={"words": 2, "chars": 9})

    process_job.calls = вызовы
    return process_job


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер без запущенной очереди: её запускает сама проверка."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "1")
    monkeypatch.setenv("ASRHUB_WEBHOOK_ALLOW_INTERNAL", "true")
    for имя in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY",
                "all_proxy"):
        monkeypatch.delenv(имя, raising=False)
    app = create_app(load(), start_queue=False)
    with TestClient(app) as клиент:
        yield клиент
        app.state.hub.queue.stop(timeout=5)


def _загрузка(state: Any, имя: str = "a.wav", секунд: float = 1.0) -> Path:
    return wav(Path(state.settings.paths.uploads) / f"up_{имя}", секунд)


def _поставить(state: Any, путь: Path, **к: Any) -> dict[str, Any]:
    настройки = state.settings.merged(к.pop("overrides", None) or {})
    return state.queue.submit(file_path=путь, filename=к.pop("filename", путь.name),
                              settings=настройки, **к)


class Приёмник:
    """Приёмник уведомлений на 127.0.0.1, со своим ответом на каждый путь."""

    def __init__(self, ответы: dict[str, Any] | None = None):
        self.запросы: list[tuple[str, str, bytes]] = []
        ответы = ответы or {}
        снаружи = self

        class Обработчик(http.server.BaseHTTPRequestHandler):
            def _ответить(self):
                длина = int(self.headers.get("content-length") or 0)
                тело = self.rfile.read(длина) if длина else b""
                снаружи.запросы.append((self.command, self.path, тело))
                ответ = ответы.get(self.path, 200)
                if isinstance(ответ, tuple):
                    self.send_response(ответ[0])
                    self.send_header("Location", ответ[1])
                else:
                    self.send_response(ответ)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = _ответить               # noqa: N815
            do_GET = _ответить                # noqa: N815

            def log_message(self, *a):         # noqa: D401
                return

        self.сервер = http.server.HTTPServer(("127.0.0.1", 0), Обработчик)
        self.порт = self.сервер.server_address[1]
        threading.Thread(target=self.сервер.serve_forever, daemon=True).start()

    def адрес(self, путь: str = "/cb") -> str:
        return f"http://127.0.0.1:{self.порт}{путь}"

    def закрыть(self) -> None:
        self.сервер.shutdown()
        self.сервер.server_close()


@pytest.fixture()
def приёмник():
    п = Приёмник()
    yield п
    п.закрыть()


# ---------------------------------------------------------------------------
# 05-queue №1: «--no-queue» не начинает считать
# ---------------------------------------------------------------------------


def test_правка_числа_воркеров_не_поднимает_очередь_только_интерфейса(сервер):
    ответ = сервер.put("/api/settings", json={"max_concurrent_jobs": 3})
    assert ответ.status_code == 200
    воркеры = [t.name for t in threading.enumerate() if t.name.startswith("asrhub-worker")]
    assert not воркеры, воркеры
    assert сервер.app.state.hub.settings.get("max_concurrent_jobs") == 3


# ---------------------------------------------------------------------------
# 05-queue №4: уведомление — состояние, досылка служебным циклом
# ---------------------------------------------------------------------------


def test_уведомление_из_окна_остановки_досылается_после_запуска(сервер, приёмник, monkeypatch):
    state = сервер.app.state.hub
    очередь = state.queue
    monkeypatch.setattr(jq, "process_job", подмена(hook=lambda _в: очередь._stop.set()))
    задание = _поставить(state, _загрузка(state), webhook_url=приёмник.адрес())
    очередь.start()
    assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "completed")
    очередь.stop(timeout=5)
    строка = state.db.get_job(задание["id"])
    assert строка["webhook_status"] == "pending", строка["webhook_status"]
    assert not приёмник.запросы
    # «Новый процесс» на той же базе.
    вторая = jq.JobQueue(state.db, state.settings, state.registry)
    вторая.start()
    try:
        assert ждать(lambda: приёмник.запросы), "уведомление так и не пришло"
        assert ждать(lambda: str(state.db.get_job(задание["id"])["webhook_status"])
                     .startswith("ok:"))
    finally:
        вторая.stop(timeout=5)


def test_снятое_с_instance_lost_задание_уведомляет(сервер, приёмник):
    state = сервер.app.state.hub
    задание = _поставить(state, _загрузка(state), webhook_url=приёмник.адрес())
    давно = time.time() - 2000
    state.db.update_job(задание["id"], status="running", instance_id="машина-9:1", retries=2,
                        heartbeat_at=давно, started_at=давно)
    очередь = state.queue
    очередь._stop.clear()
    очередь._reclaim_stale_jobs()
    assert state.db.get_job(задание["id"])["error_code"] == "instance_lost"
    assert ждать(lambda: приёмник.запросы), "при instance_lost уведомления нет"
    тело = json.loads(приёмник.запросы[0][2])
    assert тело["id"] == задание["id"] and тело["status"] == "failed"


def test_недоступный_приёмник_получает_уведомление_позже(сервер, monkeypatch):
    state = сервер.app.state.hub
    очередь = state.queue
    monkeypatch.setattr(jq, "ПОПЫТОК_ЗА_ЗАХОД", 1)
    с = socket.socket()
    с.bind(("127.0.0.1", 0))
    порт = с.getsockname()[1]
    с.close()                                   # на этом порту никто не слушает
    задание = _поставить(state, _загрузка(state), webhook_url=f"http://127.0.0.1:{порт}/cb")
    state.db.update_job(задание["id"], status="completed")
    очередь._stop.clear()
    очередь._send_webhook(задание["id"])
    assert ждать(lambda: int(state.db.get_job(задание["id"])["webhook_attempts"] or 0) == 1)
    строка = state.db.get_job(задание["id"])
    assert строка["webhook_status"] == "pending"
    assert float(строка["webhook_next_at"]) > time.time() + 30, "досылка без паузы"
    # Приёмник поднялся на том же адресе, пауза прошла — служебный цикл досылает.
    п = Приёмник()
    try:
        state.db.update_job(задание["id"], webhook_url=п.адрес())
        state.db.update_job(задание["id"], webhook_next_at=time.time() - 1)
        assert очередь._дослать_уведомления() == 1
        assert ждать(lambda: п.запросы)
        assert ждать(lambda: str(state.db.get_job(задание["id"])["webhook_status"])
                     .startswith("ok:"))
    finally:
        п.закрыть()


# ---------------------------------------------------------------------------
# 05-queue №5: перенаправление уведомления
# ---------------------------------------------------------------------------


def test_перенаправление_не_теряет_тело_уведомления(сервер):
    п = Приёмник({"/hook": (302, "/hook/")})
    try:
        state = сервер.app.state.hub
        задание = _поставить(state, _загрузка(state), webhook_url=п.адрес("/hook"))
        state.db.update_job(задание["id"], status="completed")
        очередь = state.queue
        очередь._stop.clear()
        очередь._deliver_webhook(state.db.get_job(задание["id"]), b'{"id": "x"}')
        assert [(м, путь) for м, путь, _ in п.запросы] == [("POST", "/hook"), ("POST", "/hook/")]
        assert п.запросы[1][2] == b'{"id": "x"}'
        assert state.db.get_job(задание["id"])["webhook_status"] == "ok:200"
    finally:
        п.закрыть()


# ---------------------------------------------------------------------------
# 05-queue №7, №8: «оживший» экземпляр, поздняя отмена и instance_lost повтора
# ---------------------------------------------------------------------------


def test_оживший_экземпляр_не_стирает_реплики_соседа(сервер, monkeypatch):
    state = сервер.app.state.hub
    задание = _поставить(state, _загрузка(state))

    def сосед_досчитал(_в):
        state.db.save_segments(задание["id"], [{"start": 0, "end": 1, "text": "сосед"},
                                                {"start": 1, "end": 2, "text": "досчитал"},
                                                {"start": 2, "end": 3, "text": "раньше"}])
        state.db.update_job(задание["id"], status="completed", instance_id=None,
                            segments_count=3)

    monkeypatch.setattr(jq, "process_job", подмена(hook=сосед_досчитал))
    state.queue.start()
    assert ждать(lambda: (state.db.get_job(задание["id"]) or {}).get("status") == "completed")
    assert ждать(lambda: not state.queue._running)
    реплики = [с["text"] for с in state.db.get_segments(задание["id"])]
    assert реплики == ["сосед", "досчитал", "раньше"], реплики


def _готовое_с_результатом(state: Any) -> dict[str, Any]:
    задание = _поставить(state, _загрузка(state))
    каталог = Path(state.settings.paths.results) / задание["id"]
    каталог.mkdir(parents=True)
    (каталог / "a.txt").write_text("прежний текст", encoding="utf-8")
    state.db.update_job(задание["id"], status="completed", result_path=str(каталог),
                        text="прежний текст", segments_count=1)
    state.db.save_segments(задание["id"], [{"start": 0, "end": 1, "text": "прежний"}])
    return state.db.get_job(задание["id"])


def test_поздняя_отмена_повтора_возвращает_прежний_результат(сервер, monkeypatch):
    state = сервер.app.state.hub
    задание = _готовое_с_результатом(state)

    def поздняя_отмена(_в):
        state.db.update_job(задание["id"], status="cancelled")

    monkeypatch.setattr(jq, "process_job", подмена(hook=поздняя_отмена))
    state.queue.retry(задание["id"])
    state.queue.start()
    assert ждать(lambda: any(с["kind"] == "rescan_reverted"
                             for с in state.db.get_events(задание["id"])))
    строка = state.db.get_job(задание["id"])
    assert строка["status"] == "completed", строка["status"]
    assert Path(строка["result_path"]).is_dir()
    assert [с["text"] for с in state.db.get_segments(задание["id"])] == ["прежний"]


def test_instance_lost_повтора_возвращает_прежний_результат(сервер):
    state = сервер.app.state.hub
    задание = _готовое_с_результатом(state)
    state.queue.retry(задание["id"])
    state.db.update_job(задание["id"], status="running", instance_id=jq.INSTANCE_ID,
                        retries=5, heartbeat_at=time.time())
    state.queue.recover()
    строка = state.db.get_job(задание["id"])
    assert строка["status"] == "completed", (строка["status"], строка["error_code"])
    assert Path(строка["result_path"]).is_dir()


# ---------------------------------------------------------------------------
# 05-queue №18: своё задание «выполняется» без исполнителя
# ---------------------------------------------------------------------------


def test_своё_задание_без_исполнителя_возвращается_без_траты_попытки(сервер):
    state = сервер.app.state.hub
    задание = _поставить(state, _загрузка(state))
    state.db.update_job(задание["id"], status="running", instance_id=jq.INSTANCE_ID,
                        retries=1, heartbeat_at=time.time() - 600, stage="готово",
                        progress=1.0)
    assert state.queue._сверить_свои() == 1
    строка = state.db.get_job(задание["id"])
    assert строка["status"] == "queued" and строка["retries"] == 1


def test_остановка_возвращает_и_задание_без_исполнителя(сервер, monkeypatch):
    state = сервер.app.state.hub
    monkeypatch.setattr(jq, "process_job", подмена())
    очередь = state.queue
    state.queue.pause()                         # чтобы воркер не взял его сам
    задание = _поставить(state, _загрузка(state))
    очередь.start()
    state.db.update_job(задание["id"], status="running", instance_id=jq.INSTANCE_ID,
                        heartbeat_at=time.time())
    очередь.stop(timeout=5)
    assert state.db.get_job(задание["id"])["status"] == "queued"
    state.queue.resume()


def test_умерший_воркер_поднимается(сервер, monkeypatch):
    state = сервер.app.state.hub
    очередь = state.queue
    очередь.start()
    поток = очередь._потоки[0]
    очередь._потоки[0] = threading.Thread(target=lambda: None)      # «умер»
    очередь._потоки[0].start()
    очередь._потоки[0].join()
    assert очередь._поднять_умерших() == 1
    assert очередь._потоки[0].is_alive() and очередь._потоки[0] is not поток


# ---------------------------------------------------------------------------
# 05-queue №9, №10: политики deadline и fair_share
# ---------------------------------------------------------------------------


def test_срок_выполнения_разбирается_как_его_напишут():
    сейчас = 1_800_000_000.0
    assert jq.разобрать_срок("+30", сейчас=сейчас) == сейчас + 1800
    assert jq.разобрать_срок("2h", сейчас=сейчас) == сейчас + 7200
    assert jq.разобрать_срок("1d", сейчас=сейчас) == сейчас + 86400
    assert jq.разобрать_срок("1767225600", сейчас=сейчас) == 1767225600.0
    assert jq.разобрать_срок("2026-11-01T18:00:00+03:00") == 1793545200.0
    assert jq.разобрать_срок("") is None
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError):
        jq.разобрать_срок("послезавтра")


def test_срок_принимается_полем_формы_и_меняется_у_ждущего(сервер, sample_wav):
    ответ = сервер.post("/api/jobs", data={"deadline": "+30"},
                        files={"file": ("a.wav", sample_wav.read_bytes(), "audio/wav")})
    assert ответ.status_code == 200, ответ.text
    номер = ответ.json()["id"]
    срок = float(сервер.app.state.hub.db.get_job(номер)["deadline"])
    assert time.time() + 25 * 60 < срок < time.time() + 35 * 60
    ответ = сервер.post(f"/api/jobs/{номер}/deadline", json={"deadline": "2h"})
    assert ответ.status_code == 200, ответ.text
    assert float(ответ.json()["deadline"]) > time.time() + 100 * 60
    ответ = сервер.post(f"/api/jobs/{номер}/deadline", json={"deadline": None})
    assert ответ.json()["deadline"] is None


def test_справедливое_разделение_при_одном_воркере_чередует_владельцев(сервер, monkeypatch):
    state = сервер.app.state.hub
    state.settings.set("scheduling_policy", "fair_share")
    порядок: list[str] = []
    monkeypatch.setattr(jq, "process_job", подмена(
        hook=lambda в: порядок.append(Path(в["source"]).name)))
    for н in range(5):
        _поставить(state, _загрузка(state, f"alice{н}.wav"), owner="alice")
    _поставить(state, _загрузка(state, "bob.wav"), owner="bob")
    state.queue.start()
    assert ждать(lambda: len(порядок) == 6, 60)
    чей = ["bob" if "bob" in имя else "alice" for имя in порядок]
    assert чей.index("bob") <= 1, чей


# ---------------------------------------------------------------------------
# 05-queue №11: «Повторить все неудавшиеся»
# ---------------------------------------------------------------------------


def test_повтор_всех_неудавшихся_не_обрывается_на_первом_отказе(сервер):
    state = сервер.app.state.hub
    номера = []
    for н in range(3):
        задание = _поставить(state, _загрузка(state, f"f{н}.wav"))
        state.db.update_job(задание["id"], status="failed")
        номера.append(задание["id"])
    Path(state.db.get_job(номера[1])["file_path"]).unlink()
    ответ = сервер.post("/api/queue/retry-failed").json()
    assert ответ["requeued"] == 2 and [о["id"] for о in ответ["failed"]] == [номера[1]], ответ
    assert [state.db.get_job(н)["status"] for н in номера] == ["queued", "failed", "queued"]


# ---------------------------------------------------------------------------
# 05-queue №12: temp_dir и брошенный tmp/
# ---------------------------------------------------------------------------


def test_рабочий_каталог_задания_в_temp_dir_и_со_своим_экземпляром(сервер, monkeypatch,
                                                                    tmp_path):
    state = сервер.app.state.hub
    быстрый = tmp_path / "быстрый"
    state.settings.set("temp_dir", str(быстрый))
    подменённая = подмена()
    monkeypatch.setattr(jq, "process_job", подменённая)
    задание = _поставить(state, _загрузка(state))
    state.queue.start()
    assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "completed")
    рабочий = подменённая.calls[0]["workdir"]
    assert рабочий.parent == быстрый, рабочий
    assert рабочий.name != задание["id"] and рабочий.name.startswith(задание["id"] + ".")


def test_запуск_убирает_рабочие_каталоги_умерших_процессов(сервер):
    from asrhub.instance import HOSTNAME
    from asrhub.phone_compat import safe_path_key

    state = сервер.app.state.hub
    tmp = Path(state.settings.get("temp_dir") or state.settings.paths.tmp)
    идёт = _поставить(state, _загрузка(state))
    state.db.update_job(идёт["id"], status="running", instance_id="соседняя:1",
                        heartbeat_at=time.time())
    мёртвый = tmp / f"job_мёртвый.{safe_path_key(HOSTNAME)}_99999999"
    живой_сосед = tmp / f"{идёт['id']}.соседняя_1"
    чужой = tmp / "чужой-файл.txt"
    for путь in (мёртвый, живой_сосед):
        путь.mkdir(parents=True)
    чужой.write_text("не моё", encoding="utf-8")
    state.queue._подмести_временные(при_запуске=True)
    assert not мёртвый.exists(), "каталог умершего процесса остался"
    assert живой_сосед.exists() and чужой.exists()


# ---------------------------------------------------------------------------
# 05-queue №13, №14: ошибки — причина, трассировка, пути
# ---------------------------------------------------------------------------


def test_понятная_ошибка_параметров_не_затирается_внутренней(сервер):
    state = сервер.app.state.hub
    задание = _поставить(state, _загрузка(state))
    параметры = dict(state.db.get_job(задание["id"])["params"])
    параметры["beam_size"] = 999
    state.db.update_job(задание["id"], params=параметры)
    state.queue.start()
    assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "failed")
    строка = state.db.get_job(задание["id"])
    assert строка["error_code"] == "config_error", строка["error_message"]
    assert "Внутренняя ошибка" not in строка["error_message"]


class _Записи(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.записи: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.записи.append(record)


def test_причина_отказа_в_журнале_с_трассировкой_а_владельцу_без_путей(сервер, monkeypatch):
    state = сервер.app.state.hub
    события: list[tuple[str, dict]] = []
    state.queue.on_event = lambda вид, данные: события.append((вид, данные))
    путь = "/opt/asrhub/data/models/hub/v3_ctc.ckpt"
    monkeypatch.setattr(jq, "process_job", подмена(
        fail=FileNotFoundError(2, "No such file or directory", путь)))
    журнал = _Записи()
    logging.getLogger("asrhub.queue").addHandler(журнал)
    try:
        задание = _поставить(state, _загрузка(state))
        state.queue.start()
        assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "failed")
        assert ждать(lambda: any(в == "job.failed" for в, _ in события))
    finally:
        logging.getLogger("asrhub.queue").removeHandler(журнал)
    провал = [з for з in журнал.записи if "провалено" in з.getMessage()]
    assert провал and провал[0].exc_info is not None, "в журнале нет трассировки"
    отказ = next(д for в, д in события if в == "job.failed")
    assert "cause" not in отказ["error"] and путь not in json.dumps(отказ, ensure_ascii=False)
    в_карточке = [с for с in state.db.get_events(задание["id"]) if с["kind"] == "failed"]
    assert путь not in json.dumps(в_карточке, ensure_ascii=False)


def test_классификация_нехватки_памяти_и_сетевых_признаков():
    from asrhub.errors import classify_exception, без_путей

    for исключение in (MemoryError(), RuntimeError("DefaultCPUAllocator: can't allocate memory"),
                       RuntimeError("std::bad_alloc"),
                       RuntimeError("onnxruntime: Failed to allocate memory for requested buffer")):
        assert classify_exception(исключение).code == "out_of_memory", исключение
    for исключение in (ValueError("Unsupported lossless codec"), KeyError("connection_id")):
        ошибка = classify_exception(исключение)
        assert "Сетевая" not in ошибка.message, (исключение, ошибка.message)
    сеть = classify_exception(ConnectionRefusedError(111, "Connection refused"))
    assert "Сетевая" in сеть.message and "refused" in сеть.message
    assert без_путей("Cannot reach https://huggingface.co/pyannote/x-3.1 (see)") == \
        "Cannot reach https://huggingface.co/pyannote/x-3.1 (see)"
    assert без_путей("обработано 5/10, частота 44100/48000") == "обработано 5/10, частота 44100/48000"
    assert без_путей("нет файла /opt/asrhub/models/v3.ckpt") == "нет файла v3.ckpt"


def test_нет_файла_весов_при_загрузке_модели_это_не_битый_звук():
    from asrhub.catalog import get_model
    from asrhub.engines.demo import DemoEngine
    from asrhub.errors import ModelLoadError

    class БезВесов(DemoEngine):
        def _load(self, settings):
            raise FileNotFoundError(2, "No such file", "/opt/asrhub/models/v3_ctc.ckpt")

    движок = БезВесов(get_model("demo-simulator"), {"device": "cpu"})
    with pytest.raises(ModelLoadError) as отказ:
        движок.ensure_loaded({"device": "cpu", "model": "demo-simulator"})
    assert отказ.value.retryable and "/opt/" not in отказ.value.message


# ---------------------------------------------------------------------------
# 05-queue №19: кеш моделей
# ---------------------------------------------------------------------------


def test_вытеснение_после_освобождения_соблюдает_размер_кеша():
    from asrhub.engines import EngineRegistry

    а = {"model": "demo-simulator", "engine": "demo", "device": "cpu", "compute_type": "A"}
    б = {"model": "demo-simulator", "engine": "demo", "device": "cpu", "compute_type": "B"}
    реестр = EngineRegistry(max_size=1, idle_unload_s=900)
    внутри, отпустить = threading.Barrier(3), threading.Event()

    def держать(настройки):
        with реестр.lease(настройки) as движок:
            движок.ensure_loaded(настройки)
            внутри.wait(10)
            отпустить.wait(10)

    потоки = [threading.Thread(target=держать, args=(н,)) for н in (а, б)]
    for поток in потоки:
        поток.start()
    внутри.wait(10)
    отпустить.set()
    for поток in потоки:
        поток.join(10)
    assert len(реестр.loaded()) == 1, реестр.loaded()


# ---------------------------------------------------------------------------
# 05-queue №20: клон из кеша
# ---------------------------------------------------------------------------


def test_клон_из_кеша_несёт_поля_своей_загрузки(сервер, monkeypatch):
    state = сервер.app.state.hub
    monkeypatch.setattr(jq, "process_job", подмена())
    первое = _поставить(state, _загрузка(state, "a.wav"), owner="alice")
    state.queue.start()
    assert ждать(lambda: state.db.get_job(первое["id"])["status"] == "completed")
    state.db.update_job(первое["id"], waveform=[{"audio_waveform": [1, 2], "sample_rate": 1}])
    клон = _поставить(state, _загрузка(state, "b.wav"), owner="alice", api_key_name="ключ",
                      source="telephony", tags="vip,согласие", priority=80, deadline="+30")
    assert клон["cached_from"] == первое["id"]
    assert (клон["api_key_name"], клон["source"], клон["tags"], клон["priority"]) == \
        ("ключ", "telephony", "vip,согласие", 80)
    assert клон["deadline"] and state.db.get_job(клон["id"])["waveform"]


# ---------------------------------------------------------------------------
# Недоделки: пауза в общей базе, прогресс без записи на каждый кусок
# ---------------------------------------------------------------------------


def test_пауза_очереди_общая_и_переживает_перезапуск(сервер):
    state = сервер.app.state.hub
    state.queue.pause()
    вторая = jq.JobQueue(state.db, state.settings, state.registry)
    assert вторая.is_paused
    _поставить(state, _загрузка(state))
    вторая._пауза_из_базы(сразу=True)
    assert вторая._next_job(0) is None
    state.queue.resume()
    assert not вторая._пауза_из_базы(сразу=True)


def test_доклады_прогресса_пишутся_в_базу_не_на_каждый_кусок(сервер, monkeypatch):
    state = сервер.app.state.hub
    monkeypatch.setattr(jq, "process_job", подмена(steps=60))
    записей = {"n": 0}
    настоящее = state.db.update_job_if_status

    def считать(job_id, expected, **поля):
        if "progress" in поля and поля.get("stage") == "распознавание":
            записей["n"] += 1
        return настоящее(job_id, expected, **поля)

    monkeypatch.setattr(state.db, "update_job_if_status", считать)
    задание = _поставить(state, _загрузка(state))
    state.queue.start()
    assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "completed")
    assert записей["n"] < 20, записей


def test_приостановка_задания_не_пишет_поверх_захваченного(сервер, monkeypatch):
    state = сервер.app.state.hub
    задание = _поставить(state, _загрузка(state))
    state.db.update_job(задание["id"], status="running", instance_id="кто-то:1")
    monkeypatch.setattr(state.queue, "get", lambda _id: {**задание, "status": "queued"})
    state.queue.pause_job(задание["id"])
    assert state.db.get_job(задание["id"])["status"] == "running"


# ---------------------------------------------------------------------------
# 05-queue №3: срок задания не считает ожидание модели
# ---------------------------------------------------------------------------


def test_ожидание_замка_модели_не_съедает_срок_задания(data_dir, monkeypatch, sample_wav):
    from asrhub import processor
    from asrhub.config import load
    from asrhub.engines import EngineRegistry

    настройки = load()
    merged = настройки.merged({"model": "demo-simulator", "engine": "demo",
                               "vad_backend": "energy"})

    class Часы:
        """Время процессора со сдвигом: ожидание замка «длится» сто секунд."""

        def __init__(self) -> None:
            self.сдвиг = 0.0

        def time(self) -> float:
            return time.time() + self.сдвиг

        def monotonic(self) -> float:
            return time.monotonic() + self.сдвиг

        def __getattr__(self, имя):
            return getattr(time, имя)

    часы = Часы()
    monkeypatch.setattr(processor, "time", часы)
    реестр = EngineRegistry()
    настоящая = реестр.lease

    @contextlib.contextmanager
    def долго_ждать(*a, **к):
        часы.сдвиг += 100.0                    # соседний воркер держал модель
        with настоящая(*a, **к) as движок:
            yield движок

    monkeypatch.setattr(реестр, "lease", долго_ждать)
    итог = processor.process_job(
        sample_wav, merged, реестр, workdir=data_dir / "w", outdir=data_dir / "o",
        basename="a", deadline=time.time() + 60, timeout_s=60)
    assert итог.text


# ---------------------------------------------------------------------------
# 04-storage №4: жив ли процесс — в Windows не сигналом
# ---------------------------------------------------------------------------


def test_в_windows_жизнь_процесса_не_проверяется_сигналом(monkeypatch):
    from asrhub import instance

    monkeypatch.setattr(instance.sys, "platform", "win32")
    monkeypatch.setattr(instance, "_жив_в_windows", lambda pid: pid != 4242)

    def сигнал(*_a):
        raise AssertionError("os.kill в Windows шлёт Ctrl+C")

    monkeypatch.setattr(instance.os, "kill", сигнал)
    assert instance.process_alive(f"{instance.HOSTNAME}:4242") is False
    assert instance.process_alive(f"{instance.HOSTNAME}:4243") is True


# ---------------------------------------------------------------------------
# 05-queue №17: поток диктовки при отказах движка
# ---------------------------------------------------------------------------


def _сессия_с_отказами(tmp_path: Path):
    from asrhub.errors import OutOfMemoryError
    from asrhub.streaming import StreamSession

    class Реестр:
        def __init__(self) -> None:
            self.попыток = 0

        def get(self, _settings):
            return object()

        @contextlib.contextmanager
        def lease(self, _settings, **_к):
            self.попыток += 1
            raise OutOfMemoryError("GPU")
            yield                                          # pragma: no cover

    реестр = Реестр()
    сессия = StreamSession(реестр, {"model": "x", "stream_window_s": 4.0}, workdir=tmp_path)
    сессия.start()
    return сессия, реестр


def test_устойчивый_отказ_движка_закрывает_поток(tmp_path):
    from asrhub.streaming import SAMPLE_RATE, tone

    сессия, реестр = _сессия_с_отказами(tmp_path)
    секунда = tone(1.0)
    события = []
    for _ in range(180):
        if сессия._closed:
            break
        события.extend(сессия.feed(секунда))
    assert сессия._closed, "сессия не закрылась"
    assert any(с.type == "done" for с in события)
    assert реестр.попыток <= 10, реестр.попыток
    assert len(сессия._pcm) / (SAMPLE_RATE * 2) <= 70


def test_на_пределе_сессии_остаток_распознаётся_и_приходит_done(tmp_path):
    from asrhub import streaming
    from asrhub.engines.base import Segment, TranscriptionResult
    from asrhub.streaming import StreamSession, tone

    class Реестр:
        def get(self, _settings):
            return object()

        @contextlib.contextmanager
        def lease(self, _settings, **_к):
            class Движок:
                def transcribe(self, _путь, _настройки, _прогресс):
                    return TranscriptionResult(segments=[Segment(0, 1, "слово")])
            yield Движок()

    сессия = StreamSession(Реестр(), {"model": "x", "stream_window_s": 4.0,
                                      "streaming_partial_results": False}, workdir=tmp_path)
    сессия.start()
    for _ in range(10):
        сессия.feed(tone(1.0))
    сессия._started -= streaming.MAX_SESSION_S + 1
    события = сессия.feed(tone(1.0))
    assert [с.type for с in события][0] == "error"
    assert any(с.type == "done" for с in события), [с.type for с in события]
    assert any(с.type == "final" and с.text for с in события)
