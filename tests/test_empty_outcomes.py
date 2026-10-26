"""Заход 52: короткие записи и записи без речи — отдельные категории, не ошибки.

Телефония присылает всё подряд: сброс на первой секунде, гудок, пустой файл
неотвеченного звонка, тишину. До захода 52 такие записи кончались
состоянием `failed` и шли в ошибки, в долю отказов, в «требуют внимания» и в
кнопку «Повторить неудавшиеся» — настоящие сбои среди них терялись. Теперь у
них свои конечные состояния: `short` («короткая запись») и `no_speech»
(«без речи»).

Здесь проверяется, что категории получаются из настоящих записей, что они
не ошибки ни в одном счёте, что их видно в API, аналитике, трендах и
списках раскрытия, что вебхук и phone_asr не называют их отказом и что
прежние «ошибки» такого рода в архиве переезжают в свои категории сами.
"""
from __future__ import annotations

import json
import math
import shutil
import struct
import time
import wave
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")


def _тон(путь: Path, секунд: float, частота: int = 16000) -> Path:
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(частота)
        файл.writeframes(b"".join(
            struct.pack("<h", int(12000 * math.sin(2 * math.pi * 440 * i / частота)))
            for i in range(int(частота * секунд))))
    return путь


def _тишина(путь: Path, секунд: float, частота: int = 8000) -> Path:
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(частота)
        файл.writeframes(b"\x00\x00" * int(частота * секунд))
    return путь


КОНЕЧНЫЕ = ("completed", "failed", "cancelled", "no_speech", "short")


def _ждать(клиент, ид: str, срок: float = 60.0) -> dict[str, Any]:
    конец = time.time() + срок
    задание: dict[str, Any] = {}
    while time.time() < конец:
        задание = клиент.get(f"/api/jobs/{ид}").json()
        if задание["status"] in КОНЕЧНЫЕ:
            return задание
        time.sleep(0.1)
    raise AssertionError(f"задание {ид} не завершилось: {задание}")


def _отправить(клиент, запись: Path, **настройки: Any) -> str:
    with open(запись, "rb") as файл:
        ответ = клиент.post("/api/jobs", files={"file": (запись.name, файл, "audio/wav")},
                            data={"settings": json.dumps(настройки)} if настройки else None)
    assert ответ.status_code == 200, ответ.text
    return ответ.json()["id"]


@pytest.fixture()
def очередь(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_RETRY_BACKOFF_S", "0")
    app = create_app(load(), start_queue=True)
    with TestClient(app) as клиент:
        yield клиент


def test_короткая_запись_не_распознаётся_и_не_ошибка(очередь, tmp_path):
    ид = _отправить(очередь, _тон(tmp_path / "сброс.wav", 1.0))
    задание = _ждать(очередь, ид)
    assert задание["status"] == "short"
    assert задание["error_code"] == "audio_too_short"
    assert "короче порога 2" in задание["error_message"]
    assert задание["stage"] == "короткая запись"
    события = {с["kind"] for с in задание["events"]}
    assert "short" in события and "failed" not in события
    # Не ошибка ни в одном счёте.
    очередь_ = очередь.get("/api/queue").json()
    assert очередь_["counts"]["short"] == 1 and очередь_["counts"]["failed"] == 0
    ошибки = очередь.get("/api/analytics", params={"period": "day"}).json()["errors"]
    assert ошибки["total_failed"] == 0 and ошибки["failure_rate"] == 0
    assert ошибки["short"] == 1
    # «Повторить неудавшиеся» её не трогает.
    assert очередь.post("/api/queue/retry-failed").json()["requeued"] == 0


def test_тишина_это_без_речи(очередь, tmp_path):
    ид = _отправить(очередь, _тишина(tmp_path / "тишина.wav", 6.0))
    задание = _ждать(очередь, ид)
    assert задание["status"] == "no_speech", задание.get("error_message")
    assert задание["error_code"] == "no_speech"
    assert задание["progress"] == 1.0
    assert очередь.get("/api/jobs", params={"status": "failed"}).json()["total"] == 0
    assert [з["id"] for з in очередь.get(
        "/api/jobs", params={"status": "no_speech"}).json()["items"]] == [ид]
    # «empty» — обе категории разом.
    assert очередь.get("/api/jobs", params={"status": "empty"}).json()["total"] == 1
    # Расшифровки нет — и это сказано, а не «пока нет».
    скачать = очередь.get(f"/api/jobs/{ид}/download", params={"fmt": "txt"})
    assert скачать.status_code == 400 and "Расшифровки нет" in скачать.text


def test_порог_ноль_оставляет_короткими_только_пустые(очередь, tmp_path):
    ид = _отправить(очередь, _тон(tmp_path / "алло.wav", 1.0), audio_min_duration_s=0)
    assert _ждать(очередь, ид)["status"] == "completed"


def test_повтор_с_другим_порогом_переделывает_короткую(очередь, tmp_path):
    ид = _отправить(очередь, _тон(tmp_path / "алло.wav", 1.5))
    assert _ждать(очередь, ид)["status"] == "short"
    # Завершённое не отменяется…
    assert очередь.post(f"/api/jobs/{ид}/cancel").json()["status"] == "short"
    # …а повторяется: например, после того как порог понизили.
    ответ = очередь.post(f"/api/jobs/{ид}/retry", json={"audio_min_duration_s": 0})
    assert ответ.status_code == 200, ответ.text
    задание = _ждать(очередь, ид)
    assert задание["status"] == "completed" and задание["error_code"] is None


def test_пустой_файл_короткий_при_любом_пороге(tmp_path):
    from asrhub.errors import AudioTooShort
    from asrhub.processor import process_job

    пустой = tmp_path / "пусто.wav"
    пустой.write_bytes(b"")
    with pytest.raises(AudioTooShort) as отказ:
        process_job(пустой, {"model": "demo-simulator", "audio_min_duration_s": 0}, None,
                    workdir=tmp_path / "w", outdir=tmp_path / "o", basename="пусто")
    assert отказ.value.code == "audio_too_short"
    assert "нет звука" in отказ.value.message
    with wave.open(str(tmp_path / "заголовок.wav"), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(8000)
        файл.writeframes(b"")
    with pytest.raises(AudioTooShort):
        process_job(tmp_path / "заголовок.wav", {"model": "demo-simulator",
                                                  "audio_min_duration_s": 0}, None,
                    workdir=tmp_path / "w2", outdir=tmp_path / "o2", basename="заголовок")


def test_модель_без_слов_это_без_речи(tmp_path, monkeypatch):
    """Детектор речи что-то услышал (или выключен), а модель не вернула ни слова."""
    from contextlib import contextmanager

    from asrhub.engines.base import TranscriptionResult
    from asrhub.errors import NoSpeechDetected
    from asrhub.processor import process_job

    class Молчун:
        def transcribe(self, path, settings, progress):
            return TranscriptionResult(segments=[], language="ru", duration=3.0)

    class Реестр:
        @contextmanager
        def lease(self, settings):
            yield Молчун()

    запись = _тон(tmp_path / "гудки.wav", 3.0)
    with pytest.raises(NoSpeechDetected) as отказ:
        process_job(запись, {"model": "demo-simulator", "vad_enabled": False}, Реестр(),
                    workdir=tmp_path / "w", outdir=tmp_path / "o", basename="гудки")
    assert отказ.value.details.get("empty_text") is True


def test_вебхук_и_phone_asr_не_называют_это_отказом(очередь, tmp_path, monkeypatch):
    from asrhub.phone_compat import PhoneRequest, callback_body

    очередь_ = очередь.app.state.hub.queue
    тела: list[dict] = []
    monkeypatch.setattr(очередь_, "_deliver_webhook",
                        lambda задание, тело: тела.append(json.loads(тело)))
    ид = _отправить(очередь, _тон(tmp_path / "сброс.wav", 0.8))
    _ждать(очередь, ид)
    задание = очередь_.db.get_job(ид)
    очередь_._webhook_worker({**задание, "webhook_url": "http://example.invalid/"})
    assert тела and тела[-1]["status"] == "short"
    assert тела[-1]["error"] is None and "короче порога" in тела[-1]["reason"]
    # phone_asr: успех с пустым диалогом — так и он отвечал бы на тишину.
    запрос = PhoneRequest(call_id="c1", files=[], base_url="", part=1, total_parts=1)
    тело = callback_body(запрос, {**задание, "status": "no_speech"}, [])
    assert тело["status"] == "success" and тело["error_message"] is None
    assert тело["transcription"] == ""
    from asrhub.api.routes_phone import _phone_status

    assert _phone_status("no_speech") == _phone_status("short") == "success"


def test_аналитика_и_тренды_видят_категории(очередь, tmp_path):
    _ждать(очередь, _отправить(очередь, _тон(tmp_path / "а.wav", 1.0)))
    _ждать(очередь, _отправить(очередь, _тишина(tmp_path / "б.wav", 6.0)))
    _ждать(очередь, _отправить(очередь, _тон(tmp_path / "в.wav", 3.0)))
    свод = очередь.get("/api/analytics", params={"period": "day"}).json()
    задания = свод["overview"]["jobs"]
    assert (задания["completed"], задания["no_speech"], задания["short"],
            задания["failed"]) == (1, 1, 1, 0)
    # Доля успешных — завершённые без ошибки: все три.
    assert задания["success_rate"] == 1.0
    assert sum(свод["timeseries"]["empty"]) == 2
    тренды = очередь.get("/api/trends", params={"period": "day"}).json()
    итог = {п["id"]: п for п in тренды["summary"]}
    assert итог["no_speech"]["n"] == 1 and итог["short"]["n"] == 1
    assert итог["failed"]["n"] == 0
    # Список раскрытия: `status:empty` — обе категории.
    список = очередь.get("/api/drill", params={"base": "jobs", "f": ["status:empty"]}).json()
    assert {з["status"] for з in список["items"]} == {"no_speech", "short"}


def test_архив_переезжает_в_категории(data_dir: Path, monkeypatch):
    """Прежние «ошибки» без речи и на пустом файле — в свои состояния при обновлении."""
    from asrhub.db import SCHEMA_VERSION, Database

    путь = data_dir / "asrhub.db"
    data_dir.mkdir(parents=True, exist_ok=True)
    база = Database(путь)
    for ид, код, сообщение, длит in (
            ("a1", "no_speech", "В файле «x» не обнаружено речи (длительность 9.0 с).", 9.0),
            ("a2", "no_speech", "В записи длительностью 1.2 с не найдено речи.", 1.2),
            ("a3", "audio_error", "Длительность файла «y.wav» близка к нулю.", 0.0),
            ("a4", "audio_error", "В записи «z.wav» нет ни одного отсчёта звука.", 0.0),
            ("a5", "audio_error", "Файл пуст: q.wav", 0.0),
            ("a6", "audio_error", "ffmpeg не смог прочитать файл", 30.0),
            ("a7", "model_load_failed", "сбой", 30.0)):
        база.create_job({"id": ид, "filename": f"{ид}.wav", "media_duration_s": длит})
        база.update_job(ид, status="failed", error_code=код, error_message=сообщение)
    база.execute("PRAGMA user_version=32")
    del база
    повторно = Database(путь)
    assert повторно.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION >= 33
    статусы = {r["id"]: (r["status"], r["error_code"]) for r in повторно.query(
        "SELECT id, status, error_code FROM jobs")}
    assert статусы["a1"] == ("no_speech", "no_speech")
    assert статусы["a2"] == ("short", "audio_too_short")
    for ид in ("a3", "a4", "a5"):
        assert статусы[ид] == ("short", "audio_too_short"), ид
    # Настоящие ошибки остаются ошибками.
    assert статусы["a6"][0] == статусы["a7"][0] == "failed"
    assert повторно.has_index("idx_calls_job_agent")


def test_параметр_порога_в_каталоге():
    from asrhub.catalog import get_param

    п = get_param("audio_min_duration_s")
    assert п is not None and п.default == 2.0 and п.group == "audio"
