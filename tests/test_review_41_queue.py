"""Заход 41: конвейер и очередь.

Перезапуск посреди задания, на котором процесс падает сам, возвращал его в
очередь без счёта попыток — по кругу, бесконечно; плановая остановка,
наоборот, тратила попытку у каждого идущего задания. Отмена на соседнем
сервере не останавливала задание, а отметка жизни ставилась только вместе с
прогрессом. Тайм-аут выбрасывал уже готовый текст; диктовка висела, пока
модель занята файлом; кеш результатов был общим на всех владельцев.
"""
from __future__ import annotations

import contextlib
import json
import math
import struct
import threading
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from asrhub.db import Database


def wav(путь: Path, секунд: float = 1.0, *, частота: int = 16000) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(частота)
        файл.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(и / 8)))
            for и in range(int(частота * секунд))))
    return путь


def ждать(условие, срок: float = 20.0) -> bool:
    конец = time.time() + срок
    while time.time() < конец:
        if условие():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер без запуска очереди: задания стоят там, куда их поставили."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as клиент:
        yield клиент


def _загрузка(state: Any, имя: str = "up_x.wav", секунд: float = 1.0) -> Path:
    return wav(Path(state.settings.paths.uploads) / имя, секунд)


# ---------------------------------------------------------------------------
# Предел очереди, попытки, ложные нули
# ---------------------------------------------------------------------------


def test_предел_очереди_действует_без_перезапуска(сервер):
    """Предел читался один раз при запуске: 2 в настройках — и 4 принятых."""
    from asrhub.errors import QueueFull

    state = сервер.app.state.hub
    state.queue.submit(file_path=_загрузка(state, "up_1.wav"), filename="1.wav",
                       settings=state.settings.merged({}))
    state.settings.set("max_queue_size", 1)
    with pytest.raises(QueueFull):
        state.queue.submit(file_path=_загрузка(state, "up_2.wav"), filename="2.wav",
                           settings=state.settings.merged({}))
    assert state.queue.status()["max_queue_size"] == 1


def test_свои_повторы_задания_с_честным_нулём_и_не_больше_серверных(сервер):
    очередь = сервер.app.state.hub.queue
    очередь.settings.set("max_retries", 2)
    assert очередь._предел_повторов({"params": {"max_retries": 0}}) == 0
    assert очередь._предел_повторов({"params": {}}) == 2
    assert очередь._предел_повторов({"params": {"max_retries": 9}}) == 2


def test_нулевая_пауза_между_повторами_это_ноль(tmp_path: Path):
    """`retry_backoff_s: 0` превращался в десять секунд."""
    from asrhub import job_queue as jq
    from asrhub.errors import EngineError

    база = Database(tmp_path / "asrhub.db")
    try:
        очередь = jq.JobQueue.__new__(jq.JobQueue)
        очередь.db = база
        очередь._lock = threading.RLock()
        очередь._cancelled = set()
        очередь._stop = threading.Event()
        очередь.on_event = None
        база.create_job({"id": "j", "filename": "a.wav", "status": "running"})
        база.update_job("j", instance_id=jq.INSTANCE_ID)
        до = time.time()
        очередь._handle_failure(база.get_job("j"), EngineError("сбой движка"),
                                {"retry_backoff_s": 0, "max_retries": 2})
        строка = база.get_job("j")
    finally:
        база.close()
    assert строка["status"] == "retry", строка
    assert float(строка["queued_at"]) <= до + 1.0, "пауза не ноль"


def test_перезапуск_посреди_задания_считает_попытку(tmp_path: Path):
    """Запись, на которой процесс падает сам, возвращалась в очередь по кругу."""
    from asrhub import job_queue as jq
    from asrhub.instance import HOSTNAME

    class _Настройки(dict):
        def get(self, к, з=None):
            return dict.get(self, к, з)

    база = Database(tmp_path / "asrhub.db")
    try:
        очередь = jq.JobQueue.__new__(jq.JobQueue)
        очередь.db = база
        очередь.settings = _Настройки(max_retries=2)
        мёртвый = f"{HOSTNAME}:999999"
        база.create_job({"id": "j", "filename": "a.wav", "status": "running"})
        база.update_job("j", instance_id=мёртвый, retries=0)
        очередь.recover()
        первый = база.get_job("j")
        база.update_job("j", status="running", instance_id=мёртвый, retries=2)
        очередь.recover()
        второй = база.get_job("j")
        события = [с["kind"] for с in база.query("SELECT kind FROM events WHERE job_id='j'")]
    finally:
        база.close()
    assert первый["status"] == "queued" and первый["retries"] == 1, первый
    assert второй["status"] == "failed" and второй["error_code"] == "instance_lost", второй
    assert "recovered" in события and "failed" in события


def test_штатная_остановка_не_тратит_попытку(tmp_path: Path):
    from asrhub import job_queue as jq
    from asrhub.errors import JobCancelled

    база = Database(tmp_path / "asrhub.db")
    try:
        очередь = jq.JobQueue.__new__(jq.JobQueue)
        очередь.db = база
        очередь._lock = threading.RLock()
        очередь._cancelled = set()
        очередь._stop = threading.Event()
        очередь._идут = set()
        очередь.on_event = None
        for ид in ("прервано", "висит"):
            база.create_job({"id": ид, "filename": "a.wav", "status": "running"})
            база.update_job(ид, instance_id=jq.INSTANCE_ID, retries=1)
        очередь._stop.set()
        # Прервано на проверке отмены — встаёт в очередь само.
        очередь._handle_failure(база.get_job("прервано"),
                                JobCancelled("остановка"), {"max_retries": 2})
        # Сидит в долгом вызове движка — его возвращает остановка.
        очередь._идут.add("висит")
        очередь._вернуть_свои_при_остановке()
        строки = {ид: база.get_job(ид) for ид in ("прервано", "висит")}
        события = [с["kind"] for с in база.query("SELECT kind FROM events")]
    finally:
        база.close()
    for ид, строка in строки.items():
        assert строка["status"] == "queued", (ид, строка)
        assert строка["retries"] == 1, f"{ид}: остановка потратила попытку"
    assert события.count("requeued") == 2


# ---------------------------------------------------------------------------
# Живая очередь: остановка, отмена с соседа, отметка жизни
# ---------------------------------------------------------------------------


@pytest.fixture()
def живая(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с очередью и управляемым «распознаванием» вместо конвейера."""
    from asrhub import job_queue
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.errors import JobCancelled
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setattr(job_queue, "HEARTBEAT_S", 0.1)
    управление = {"начал": threading.Event(), "прогресс": True}

    def process_job(source, merged, registry, *, progress=None, cancelled=None, **_):
        управление["начал"].set()
        time.sleep(управление.get("застрять", 0.0))
        for _n in range(300):
            if cancelled():
                raise JobCancelled("прервано")
            if управление["прогресс"]:
                progress(0.3, "распознавание")
            time.sleep(0.03)
        raise JobCancelled("слишком долго")

    monkeypatch.setattr(job_queue, "process_job", process_job)
    app = create_app(load(), start_queue=True)
    with TestClient(app) as клиент:
        yield клиент, управление


def _поставить(клиент) -> str:
    state = клиент.app.state.hub
    задание = state.queue.submit(file_path=_загрузка(state, f"up_{time.time_ns()}.wav"),
                                 filename="звонок.wav", settings=state.settings.merged({}))
    return задание["id"]


def test_остановка_сервера_возвращает_задание_без_траты_попытки(живая):
    клиент, управление = живая
    state = клиент.app.state.hub
    ид = _поставить(клиент)
    assert управление["начал"].wait(10)
    начало = time.time()
    state.queue.stop()
    # Идущее задание замечает остановку на проверке отмены, а не держит её
    # до истечения ожидания.
    assert time.time() - начало < 2.0, "остановка ждала задание до конца срока"
    строка = state.db.get_job(ид)
    события = [с["kind"] for с in state.db.query(
        "SELECT kind FROM events WHERE job_id=?", (ид,))]
    assert строка["status"] == "queued", строка
    assert int(строка["retries"] or 0) == 0
    assert "requeued" in события


def test_остановка_возвращает_и_застрявшее_в_движке_задание(живая):
    """Задание внутри долгого вызова движка проверку отмены не проходит —
    его возвращает сама остановка, иначе следующий запуск счёл бы его
    потерянным и потратил попытку."""
    клиент, управление = живая
    управление["застрять"] = 6.0
    state = клиент.app.state.hub
    ид = _поставить(клиент)
    assert управление["начал"].wait(10)
    state.queue.stop()
    строка = state.db.get_job(ид)
    assert строка["status"] == "queued" and int(строка["retries"] or 0) == 0, строка


def test_отмена_на_соседнем_сервере_останавливает_задание(живая):
    """Отмена жила в памяти того процесса, куда пришла: этот досчитывал до
    конца и переписывал «отменено» своим «распознавание, 76 %»."""
    клиент, управление = живая
    state = клиент.app.state.hub
    ид = _поставить(клиент)
    assert управление["начал"].wait(10)
    # Так отменяет сосед: запись в общую базу, в память этого процесса — ничего.
    state.db.update_job(ид, status="cancelled", stage="отменено", progress=0.3)
    assert ждать(lambda: ид not in state.queue._идут, 3), "задание так и считается"
    строка = state.db.get_job(ид)
    assert строка["status"] == "cancelled" and строка["stage"] == "отменено", строка


def test_отметка_жизни_замечает_отобранное_задание(живая):
    """Задание отдали другому экземпляру — считать его здесь незачем."""
    клиент, управление = живая
    управление["прогресс"] = False
    state = клиент.app.state.hub
    ид = _поставить(клиент)
    assert управление["начал"].wait(10)
    state.db.update_job(ид, instance_id="сосед:1")
    assert ждать(lambda: ид not in state.queue._идут, 3), "досчитывает чужое задание"


def test_отметка_жизни_ставится_и_без_прогресса(живая):
    """Шаг без прогресса дольше пяти минут — и сосед забирал живое задание."""
    клиент, управление = живая
    управление["прогресс"] = False
    state = клиент.app.state.hub
    ид = _поставить(клиент)
    assert управление["начал"].wait(10)
    state.db.update_job(ид, heartbeat_at=1.0)
    assert ждать(lambda: float(state.db.get_job(ид)["heartbeat_at"] or 0) > time.time() - 5,
                 5), "отметка жизни не обновилась"


# ---------------------------------------------------------------------------
# Конвейер: тайм-аут, прогресс, имя записи
# ---------------------------------------------------------------------------


class _Движок:
    """Как настоящие движки: работа, потом доклад «сборка результата» (0,98).

    `доклад_посреди` — доклад о прогрессе ещё посреди распознавания.
    """

    def __init__(self, *, пауза: float = 0.0, доклад_посреди: bool = False):
        self.пауза = пауза
        self.доклад_посреди = доклад_посреди

    def transcribe(self, path, settings, progress):
        from asrhub.engines.base import Segment, TranscriptionResult

        time.sleep(self.пауза)
        if progress is not None:
            progress(0.5 if self.доклад_посреди else 0.98,
                     "распознавание" if self.доклад_посреди else "сборка результата")
        return TranscriptionResult(segments=[Segment(0.0, 0.9, "привет мир")],
                                   language="ru", duration=1.0)


class _Реестр:
    def __init__(self, движок):
        self.движок = движок

    def resolve(self, settings):
        return None, "demo"

    def lease(self, settings, **_):
        return contextlib.nullcontext(self.движок)


def _настройки(**поверх: Any) -> dict[str, Any]:
    from asrhub import catalog

    return {**catalog.defaults(), "model": "demo-simulator", "engine": "demo",
            "vad_backend": "energy", "punctuation_enabled": False, "itn_enabled": False,
            "diarization_enabled": False, "alignment_backend": "none",
            "output_formats": ["json"], **поверх}


def test_тайм_аут_не_выбрасывает_готовый_текст(tmp_path: Path, sample_wav: Path):
    """Движок одним вызовом досчитал — первый же доклад поднимал тайм-аут."""
    from asrhub.processor import process_job

    итог = process_job(sample_wav, _настройки(), _Реестр(_Движок(пауза=2.5)),
                       workdir=tmp_path / "w", outdir=tmp_path / "o", basename="a",
                       deadline=time.time() + 1.5, timeout_s=1)
    assert "привет мир" in итог.text


def test_тайм_аут_прерывает_само_распознавание(tmp_path: Path, sample_wav: Path):
    from asrhub.errors import JobTimeout
    from asrhub.processor import process_job

    with pytest.raises(JobTimeout):
        process_job(sample_wav, _настройки(), _Реестр(_Движок(пауза=2.5, доклад_посреди=True)),
                    workdir=tmp_path / "w", outdir=tmp_path / "o", basename="a",
                    deadline=time.time() + 1.5, timeout_s=1)


def test_тайм_аут_не_повторяется():
    from asrhub.errors import JobTimeout

    assert JobTimeout(60).retryable is False


def test_полоса_прогресса_только_вперёд(tmp_path: Path, sample_wav: Path):
    """Выравнивание докладывало 0,80 после 0,82, полоса громкости — 0,86 после 0,90."""
    from asrhub.processor import process_job

    доли: list[float] = []
    process_job(sample_wav, _настройки(alignment_backend="mfa", waveform_enabled=True),
                _Реестр(_Движок()), workdir=tmp_path / "w", outdir=tmp_path / "o",
                basename="a", progress=lambda доля, этап: доли.append(доля))
    assert доли == sorted(доли), доли


def test_в_выгрузке_имя_записи_а_не_служебное(tmp_path: Path, sample_wav: Path):
    """В meta.filename и заголовок DOCX уходило «up_….wav»."""
    import shutil

    from asrhub.processor import process_job

    служебное = tmp_path / "up_3f9a.wav"
    shutil.copy(sample_wav, служебное)
    process_job(служебное, _настройки(), _Реестр(_Движок()), workdir=tmp_path / "w",
                outdir=tmp_path / "o", basename="звонок клиенту",
                filename="звонок клиенту.wav")
    данные = json.loads((tmp_path / "o" / "звонок клиенту.json").read_text(encoding="utf-8"))
    assert данные["meta"]["filename"] == "звонок клиенту.wav"


# ---------------------------------------------------------------------------
# Кеш результатов и отпечатки
# ---------------------------------------------------------------------------


def _готово(state: Any, ид: str, имя: str) -> None:
    """Задание завершено: как если бы его посчитала очередь."""
    from asrhub.pipeline.export import safe_basename

    каталог = Path(state.settings.paths.results) / ид
    каталог.mkdir(parents=True, exist_ok=True)
    # Имя — как его пишет выгрузка: «+» в имени файла становится «_».
    (каталог / f"{safe_basename(Path(имя).stem)}.txt").write_text("текст", encoding="utf-8")
    state.db.update_job(ид, status="completed", result_path=str(каталог), text="текст")


def test_кеш_результатов_только_среди_своих(сервер):
    """Боб узнавал, что такую же запись уже присылала Алиса, и получал её
    файлы под её именем."""
    state = сервер.app.state.hub
    настройки = state.settings.merged({})
    первое = state.queue.submit(file_path=_загрузка(state, "up_a.wav"),
                                filename="+79161234567 Иванов.wav", settings=настройки,
                                owner="алиса")
    _готово(state, первое["id"], "+79161234567 Иванов.wav")

    чужое = state.queue.submit(file_path=_загрузка(state, "up_b.wav"), filename="my.wav",
                               settings=настройки, owner="боб")
    assert чужое["status"] == "queued" and not чужое.get("cached_from"), чужое

    своё = state.queue.submit(file_path=_загрузка(state, "up_c.wav"), filename="мой.wav",
                              settings={**настройки, "delete_source_after": True},
                              owner="алиса")
    assert своё["status"] == "completed" and своё["cached_from"] == первое["id"]
    файлы = sorted(п.name for п in Path(своё["result_path"]).iterdir())
    assert файлы == ["мой.txt"], файлы
    assert not (Path(state.settings.paths.uploads) / "up_c.wav").exists(), \
        "«удалять исходник» не сработал у задания из кеша"


def test_повтор_даёт_тот_же_отпечаток_что_и_новая_загрузка(сервер):
    """Отпечаток при постановке брал секреты и параметры сервера, при
    повторе — нет: кеш по повторённому заданию не срабатывал никогда."""
    state = сервер.app.state.hub
    настройки = state.settings.merged({})
    первое = state.queue.submit(file_path=_загрузка(state, "up_d.wav"), filename="d.wav",
                                settings=настройки)
    второе = state.queue.submit(file_path=_загрузка(state, "up_e.wav"), filename="e.wav",
                                settings=настройки)
    _готово(state, первое["id"], "d.wav")
    state.queue.retry(первое["id"])
    повторённое = state.db.get_job(первое["id"])
    assert повторённое["params"]["_hash"] == второе["params"]["_hash"]


def test_повтор_без_исходной_записи_отказывает_сразу(сервер):
    from asrhub.errors import ConfigError

    state = сервер.app.state.hub
    путь = _загрузка(state, "up_f.wav")
    задание = state.queue.submit(file_path=путь, filename="f.wav",
                                 settings=state.settings.merged({}))
    _готово(state, задание["id"], "f.wav")
    путь.unlink()
    with pytest.raises(ConfigError, match="больше нет"):
        state.queue.retry(задание["id"])
    строка = state.db.get_job(задание["id"])
    assert строка["status"] == "completed"
    assert (Path(state.settings.paths.results) / задание["id"]).is_dir(), \
        "отказ тронул готовый результат"


def test_переписку_повторить_нельзя(сервер):
    from asrhub.errors import ConfigError

    state = сервер.app.state.hub
    state.db.create_job({"id": "чат", "filename": "чат", "status": "completed",
                         "source": "text"})
    with pytest.raises(ConfigError, match="переписка"):
        state.queue.retry("чат")


def test_отпечаток_весов_различает_варианты_gigaam(tmp_path: Path):
    """Без ревизии брался первый по алфавиту `v3_ctc.ckpt`, и обновление
    весов модели по умолчанию (`v3_rnnt`) отпечаток не меняло."""
    from asrhub import model_files

    каталог = tmp_path / "models"
    каталог.mkdir()
    (каталог / "v3_ctc.ckpt").write_bytes(b"ctc")
    (каталог / "v3_rnnt.ckpt").write_bytes(b"rnnt")
    model_files.forget()
    было = model_files.fingerprint(каталог, "ai-sage/GigaAM-v3", "rnnt")
    (каталог / "v3_rnnt.ckpt").write_bytes(b"rnnt-new-weights")
    model_files.forget()
    стало = model_files.fingerprint(каталог, "ai-sage/GigaAM-v3", "rnnt")
    (каталог / "v3_ctc.ckpt").write_bytes(b"ctc-new-weights")
    model_files.forget()
    чужое = model_files.fingerprint(каталог, "ai-sage/GigaAM-v3", "rnnt")
    assert было and было != стало, "обновление весов rnnt не изменило отпечаток"
    assert стало == чужое, "отпечаток rnnt зависит от весов ctc"


def test_хеш_файла_видит_середину(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Прежний хеш — края и размер. Две минутные записи звонков одной длины с
    одним приветствием автоответчика получали один ключ кеша."""
    from asrhub.pipeline import audio

    начало = b"\x01" * (1 << 20)
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    a.write_bytes(начало + b"\x02" * (1 << 19))
    b.write_bytes(начало + b"\x03" * (1 << 19))
    assert audio.file_hash(a) != audio.file_hash(b), "полтора мегабайта хешировались по первому"

    monkeypatch.setattr(audio, "_ХЕШ_ЦЕЛИКОМ", 1 << 20)
    края = b"\x07" * (1 << 20)
    c, d = tmp_path / "c.bin", tmp_path / "d.bin"
    c.write_bytes(края + b"\x04" * (3 << 20) + края)
    d.write_bytes(края + b"\x05" * (3 << 20) + края)
    assert audio.file_hash(c) != audio.file_hash(d), "середина большого файла не участвует"


# ---------------------------------------------------------------------------
# Поиск речи, профиль звука
# ---------------------------------------------------------------------------


def test_поиск_речи_в_том_же_файле_не_повторяется(tmp_path: Path, monkeypatch):
    """Конвейер и GigaAM искали речь в одном файле дважды."""
    from asrhub.pipeline import vad

    путь = wav(tmp_path / "a.wav", 2.0)
    вызовы: list[float] = []
    настоящий = vad._detect

    def считать(path, opts):
        вызовы.append(float(opts.get("vad_max_speech_s") or 0))
        return настоящий(path, opts)

    monkeypatch.setattr(vad, "_detect", считать)
    настройки = {"vad_backend": "energy", "vad_max_speech_s": 22.0}
    первый = vad.detect(путь, настройки)
    второй = vad.detect(путь, dict(настройки))
    vad.detect(путь, {**настройки, "vad_max_speech_s": 5.0})
    assert [(s.start, s.end) for s in первый] == [(s.start, s.end) for s in второй]
    assert вызовы == [22.0, 5.0], вызовы


def test_silero_не_вызывается_из_двух_потоков_разом(tmp_path: Path, monkeypatch):
    """Silero держит состояние между окнами, а экземпляр один на процесс."""
    import sys
    import types

    from asrhub.pipeline import vad

    одновременно = {"сейчас": 0, "наибольшее": 0}
    замок = threading.Lock()

    def разметка(tensor, model, **kwargs):
        with замок:
            одновременно["сейчас"] += 1
            одновременно["наибольшее"] = max(одновременно["наибольшее"], одновременно["сейчас"])
        time.sleep(0.2)
        with замок:
            одновременно["сейчас"] -= 1
        return [{"start": 0.0, "end": 0.5}]

    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        as_tensor=lambda данные, dtype=None: данные, float32="float32"))
    monkeypatch.setattr(vad, "_silero_model",
                        lambda: (object(), {"get_speech_timestamps": разметка}))
    путь = wav(tmp_path / "a.wav", 1.0)
    потоки = [threading.Thread(target=vad._detect_silero, args=(путь, {})) for _ in range(2)]
    for поток in потоки:
        поток.start()
    for поток in потоки:
        поток.join()
    assert одновременно["наибольшее"] == 1


def test_длинный_участок_режется_в_тишине(tmp_path: Path):
    """Равные доли резали участок посреди слова."""
    from asrhub.pipeline.vad import SpeechSegment, _enforce_max_length

    громкость = [1.0] * 100
    громкость[35] = 0.01                     # вдох на 3,5 секунды
    куски = _enforce_max_length([SpeechSegment(0.0, 10.0)], 4.0,
                                energies=громкость, step_s=0.1)
    assert abs(куски[0].end - 3.55) < 1e-6, куски
    assert all(к.duration <= 4.0 + 1e-9 for к in куски)
    assert куски[-1].end == 10.0


def test_профиль_читает_только_начало_записи(tmp_path: Path):
    from asrhub.pipeline.audio import load_samples

    путь = wav(tmp_path / "a.wav", 3.0)
    отсчёты, частота = load_samples(путь, max_seconds=1.0)
    assert len(отсчёты) == частота


# ---------------------------------------------------------------------------
# Постобработка и выгрузка
# ---------------------------------------------------------------------------


def test_нормализация_чисел_не_стирает_ё(monkeypatch):
    """Стоило поменять одно число — и все «ё» реплики пропадали."""
    from asrhub.pipeline import postprocess

    monkeypatch.setattr(postprocess, "_load_itn",
                        lambda backend, language: lambda текст: текст.replace(
                            "двадцать пять", "25"))
    итог = postprocess.apply_itn("Всё будет через двадцать пять минут, ещё раз", "nemo", "ru")
    assert итог == "Всё будет через 25 минут, ещё раз"


def test_фильтр_мата_маскирует_и_пословные_метки():
    from asrhub.pipeline import postprocess

    реплики = [{"start": 0.0, "end": 1.0, "text": "ну это блядство какое-то",
                "words": [{"word": "ну", "start": 0.0, "end": 0.1},
                          {"word": "это", "start": 0.1, "end": 0.2},
                          {"word": "блядство", "start": 0.2, "end": 0.6},
                          {"word": "какое-то", "start": 0.6, "end": 1.0}]}]
    итог, _ = postprocess.process(реплики, {"profanity_filter": "mask",
                                            "punctuation_enabled": False,
                                            "itn_enabled": False,
                                            "merge_short_segments": False})
    слова = [с["word"] for с in итог[0]["words"]]
    assert "блядство" not in слова and слова[2].startswith("б*"), слова


def test_субтитры_не_режут_реплику_чужим_угу():
    """«угу» клиента урезало десятисекундную реплику оператора до полсекунды."""
    from asrhub.pipeline import export

    результат = {"segments": [
        {"start": 0.0, "end": 10.0, "text": "длинное объяснение условий", "speaker": "оператор"},
        {"start": 0.5, "end": 0.9, "text": "угу", "speaker": "клиент"},
        {"start": 11.0, "end": 12.0, "text": "понятно", "speaker": "клиент"}]}
    подготовлено = export._prepare_subtitles(результат, 1.0)
    assert подготовлено[0]["end"] == 10.0, подготовлено[0]


def test_субтитры_одного_говорящего_не_наезжают():
    from asrhub.pipeline import export

    результат = {"segments": [
        {"start": 0.0, "end": 5.0, "text": "раз", "speaker": "А"},
        {"start": 4.0, "end": 6.0, "text": "два", "speaker": "А"}]}
    подготовлено = export._prepare_subtitles(результат, 1.0)
    assert подготовлено[0]["end"] <= 4.0


def test_webvtt_и_ass_не_ломаются_на_тексте_и_имени():
    from asrhub.pipeline import export

    результат = {"segments": [{"start": 0.0, "end": 2.0, "text": "А --> Б <тег> & всё",
                               "speaker": "Иванов, менеджер"}]}
    vtt = export.to_vtt(результат, {"include_speaker_labels": False})
    assert "А --&gt; Б &lt;тег&gt; &amp; всё" in vtt, vtt
    ass = export.to_ass(результат, {})
    строка = next(с for с in ass.splitlines() if с.startswith("Dialogue:"))
    поля = строка[len("Dialogue: "):].split(",", 9)
    assert поля[4] == "Иванов  менеджер" and поля[9].startswith("А --> Б"), поля


# ---------------------------------------------------------------------------
# Поток: занятая модель, чётность отсчётов, время кусков
# ---------------------------------------------------------------------------


def _держать_модель(реестр, настройки):
    занята, отпустить = threading.Event(), threading.Event()
    движок = реестр.get(настройки)

    def держать():
        with движок.lock:
            занята.set()
            отпустить.wait(20)

    поток = threading.Thread(target=держать, daemon=True)
    поток.start()
    assert занята.wait(5)
    return отпустить, поток


def _настройки_потока(**поверх: Any) -> dict[str, Any]:
    from asrhub import catalog

    return {**catalog.defaults(), "model": "demo-simulator", "engine": "demo",
            "vad_backend": "energy", "stream_window_s": 1.0, **поверх}


def test_диктовка_не_висит_пока_модель_занята_файлом(tmp_path: Path):
    """Окно потока ждало блокировку модели до конца файла из очереди."""
    from asrhub.engines import EngineRegistry
    from asrhub.streaming import StreamSession, tone

    реестр = EngineRegistry()
    настройки = _настройки_потока(stream_separate_engine=False)
    отпустить, поток = _держать_модель(реестр, настройки)
    сессия = StreamSession(реестр, настройки, workdir=tmp_path)
    сессия.start()
    try:
        начало = time.time()
        события = сессия.feed(tone(1.2))
        assert time.time() - начало < 6, "окно ждало модель, занятую файлом"
        assert [с.type for с in события] == ["busy"], события
        assert события[0].extra["busy"] is True
        отпустить.set()
        поток.join(5)
        события = сессия.feed(tone(1.2))
        виды = [с.type for с in события]
        assert "busy" in виды and "partial" in виды, виды
    finally:
        отпустить.set()
        сессия.close()


def test_диктовка_берёт_второй_экземпляр_модели(tmp_path: Path):
    from asrhub.engines import EngineRegistry
    from asrhub.streaming import StreamSession, tone

    реестр = EngineRegistry()
    настройки = _настройки_потока(stream_separate_engine=True)
    отпустить, _поток = _держать_модель(реестр, настройки)
    сессия = StreamSession(реестр, настройки, workdir=tmp_path)
    сессия.start()
    try:
        события = сессия.feed(tone(1.2))
        assert [с.type for с in события] == ["partial"], события
        assert any(ключ.endswith("::поток") for ключ in реестр._cache)
    finally:
        отпустить.set()
        сессия.close()


def test_аренда_не_отдаёт_выгруженную_модель():
    """Между `get()` и пометкой «занят» успевал вклиниться сборщик простоя."""
    from asrhub.engines import EngineRegistry

    реестр = EngineRegistry(idle_unload_s=1)
    настройки = _настройки_потока()
    движок = реестр.get(настройки)
    движок._model = {"ready": True}
    настоящий_get = реестр.get

    def get_со_сборщиком(settings, **kwargs):
        найденный = настоящий_get(settings, **kwargs)
        найденный.last_used = time.time() - 100
        сборщик = threading.Thread(target=реестр.collect_idle)
        сборщик.start()
        сборщик.join(0.3)
        return найденный

    реестр.get = get_со_сборщиком
    with реестр.lease(настройки) as взятый:
        assert взятый in реестр._cache.values(), "модель выгружена прямо в руках задания"


def test_нечётный_кусок_не_сдвигает_поток():
    from asrhub.streaming import _Decoder

    декодер = _Decoder("pcm_s16le")
    assert декодер.feed(b"\x01") == b""
    assert декодер.feed(b"\x02\x03") == b"\x01\x02"
    assert декодер.feed(b"\x04") == b"\x03\x04"


def test_настоящий_поток_даёт_время_кусков(tmp_path: Path):
    """У каждого `final` в настоящем потоке стояло start=0.0."""
    from asrhub.streaming import SAMPLE_RATE, StreamSession

    class _Родной:
        def __init__(self):
            self.ответы = [("final", "раз"), ("final", "два")]

        def accept(self, pcm):
            return self.ответы.pop(0) if self.ответы else None

    сессия = StreamSession.__new__(StreamSession)
    сессия._native = _Родной()
    сессия._native_bytes = 0
    сессия._committed_s = 0.0
    сессия._final_text = ""
    сессия._last_partial = ""
    сессия._first_text_at = None
    сессия.settings = {}
    секунда = b"\x00\x00" * SAMPLE_RATE
    сессия._native_bytes += len(секунда)
    первое = сессия._feed_native(секунда)[0]
    сессия._native_bytes += len(секунда)
    второе = сессия._feed_native(секунда)[0]
    assert (первое.start, первое.end) == (0.0, 1.0)
    assert (второе.start, второе.end) == (1.0, 2.0), второе


# ---------------------------------------------------------------------------
# Voxtral
# ---------------------------------------------------------------------------


class _Входы(dict):
    input_ids = type("И", (), {"shape": (1, 3)})()

    def to(self, *a, **k):
        return self


class _Процессор:
    def __init__(self):
        self.вызовы: list[tuple[Any, str]] = []

    def apply_transcription_request(self, *, language, audio, model_id):
        self.вызовы.append((language, audio))
        return _Входы()

    def batch_decode(self, tokens, skip_special_tokens=True):
        return ["кусок"]


class _Модель:
    device = "cpu"
    dtype = None

    def __init__(self):
        self.пределы: list[int] = []

    def generate(self, **kwargs):
        self.пределы.append(kwargs["max_new_tokens"])
        return type("В", (), {"__getitem__": lambda self, key: self})()


def test_voxtral_режет_длинную_запись_и_не_обрезает_ответ(tmp_path: Path, monkeypatch):
    """Метод с опечаткой, запись целиком и 2048 токенов на весь ответ."""
    from asrhub import catalog
    from asrhub.engines.misc_engines import VoxtralEngine

    движок = VoxtralEngine.__new__(VoxtralEngine)
    движок.spec = catalog.get_model("voxtral-mini-3b")
    процессор, модель = _Процессор(), _Модель()
    движок._model = {"processor": процессор, "model": модель, "device": "cpu"}
    monkeypatch.setattr(VoxtralEngine, "КУСОК_С", 2.0)
    путь = wav(tmp_path / "a.wav", 5.0)
    итог = движок._transcribe(путь, {"vad_backend": "energy", "temp_dir": str(tmp_path)}, None)
    assert len(процессор.вызовы) >= 3, процессор.вызовы
    assert all(язык is None for язык, _ in процессор.вызовы), "язык по умолчанию «ru»"
    assert [с.text for с in итог.segments] == ["кусок"] * len(процессор.вызовы)
    assert all(с.end - с.start <= 2.0 + 1e-6 for с in итог.segments)
    assert движок._кусок(процессор, модель, путь, None, 300.0) == "кусок"
    assert модель.пределы[-1] == 1800, "ответ обрезался на 2048 токенах"


def test_интерфейс_диктовки_понимает_занятую_модель():
    """Событие `busy` — не ошибка: интерфейс говорит «текст догонит»."""
    app_js = (Path(__file__).resolve().parent.parent / "server" / "asrhub" / "web"
              / "app.js").read_text(encoding="utf-8")
    обработчик = app_js[app_js.index("socket.onmessage"):]
    обработчик = обработчик[:обработчик.index("\n    };")]
    assert "case 'busy':" in обработчик
    assert "текст догонит" in обработчик


def test_внутри_приоритета_первым_идёт_раньше_поставленное(tmp_path: Path):
    """Окно выборки планировщика брало сначала «queued», потом «retry»: задание,
    чьё время повтора давно наступило, ждало, пока очередь не станет меньше окна."""
    база = Database(tmp_path / "asrhub.db")
    try:
        база.create_job({"id": "повтор", "filename": "a.wav", "status": "retry",
                         "priority": 50})
        база.update_job("повтор", created_at=1.0, queued_at=1.0)
        for номер in range(5):
            база.create_job({"id": f"новое-{номер}", "filename": "b.wav",
                             "status": "queued", "priority": 50})
        окно = база.list_jobs(status=["queued", "retry"], order="priority DESC",
                              limit=3, light=True, ready_before=time.time())
    finally:
        база.close()
    assert окно[0]["id"] == "повтор", [з["id"] for з in окно]


def test_диаризация_грузится_один_раз(tmp_path: Path, monkeypatch):
    """Конвейер pyannote грузился заново на каждое задание и каждый канал."""
    import sys
    import types

    from asrhub.pipeline import diarization, model_cache

    загрузок = []

    class _Разметка:
        def itertracks(self, yield_label=True):
            отрезок = types.SimpleNamespace(start=0.0, end=1.0)
            return iter([(отрезок, None, "SPEAKER_00")])

    class Pipeline:
        @classmethod
        def from_pretrained(cls, name, token=None):
            загрузок.append(name)
            return cls()

        def __call__(self, path, **kwargs):
            return _Разметка()

    пакет = types.ModuleType("pyannote")
    модуль = types.ModuleType("pyannote.audio")
    модуль.Pipeline = Pipeline
    пакет.audio = модуль
    monkeypatch.setitem(sys.modules, "pyannote", пакет)
    monkeypatch.setitem(sys.modules, "pyannote.audio", модуль)
    model_cache.выгрузить(None)
    путь = wav(tmp_path / "a.wav", 1.0)
    настройки = {"hf_token": "hf_x", "device": "cpu"}
    try:
        первая = diarization._pyannote(путь, настройки)
        вторая = diarization._pyannote(путь, настройки)
    finally:
        model_cache.выгрузить(None)
    assert первая == вторая == [(0.0, 1.0, "SPEAKER_00")]
    assert len(загрузок) == 1, загрузок


def test_вспомогательные_модели_выгружаются_вместе_с_простоем():
    from asrhub.engines import EngineRegistry
    from asrhub.pipeline import model_cache

    model_cache.выгрузить(None)
    запись = model_cache.взять(("проба",), lambda: object())
    запись.использована = time.time() - 100
    EngineRegistry(idle_unload_s=10).collect_idle()
    assert "проба" not in model_cache.загружено()
