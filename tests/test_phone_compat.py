"""Приём заданий по схеме phone_asr.

Смысл проверок — не «маршрут отвечает», а «приложение, написанное под
phone_asr, работает без правок». Поэтому здесь сверяются имена полей, вид
значений и текст ответа: расхождение в одном ключе делает совместимость
бесполезной, а заметить его по глазам нельзя.
"""
from __future__ import annotations

import json
import math
import os
import struct
import threading
import wave
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

import pytest
from asrhub import phone_compat

# ---------------------------------------------------------------------------
# Разбор адресов
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("example.com", "https://example.com"),
    ("https://example.com/", "https://example.com"),
    ("http://192.168.0.10/asr/", "http://192.168.0.10/asr"),
    ("  example.com  ", "https://example.com"),
])
def test_base_url_matches_phone_asr(given, expected):
    """Схема дописывается, косая черта снимается — как в валидаторе phone_asr.

    Расхождение здесь означало бы разные адреса обратного вызова у двух
    серверов при одном и том же запросе.
    """
    assert phone_compat.normalise_base_url(given) == expected


def test_empty_base_url_is_refused():
    from asrhub.errors import ASRHubError

    with pytest.raises(ASRHubError):
        phone_compat.normalise_base_url("   ")


@pytest.mark.parametrize("given,expected", [
    ("http://h/оператор.wav",
     "http://h/%D0%BE%D0%BF%D0%B5%D1%80%D0%B0%D1%82%D0%BE%D1%80.wav"),
    ("http://h/a%20b.wav", "http://h/a%20b.wav"),          # уже закодировано
    ("http://h/a b.wav", "http://h/a%20b.wav"),
    ("https://хост.рф/f.mp3", "https://xn--n1agdj.xn--p1ai/f.mp3"),
])
def test_urls_are_encoded_for_urllib(given, expected):
    """Кириллица в имени записи валила запрос, не дойдя до сети.

    phone_asr этого не замечал: aiohttp кодирует путь сам. У нас urllib, и
    он бросает «'ascii' codec can't encode characters» — а имена записей на
    АТС кириллические сплошь и рядом. Уже закодированный адрес повторно не
    кодируется: иначе %20 превратился бы в %2520.
    """
    assert phone_compat.encode_url(given) == expected


def test_callback_address_is_built_like_phone_asr():
    call = phone_compat.PhoneRequest(call_id="1", files=[], base_url="https://h")
    assert call.target_url() == "https://h/callback-endpoint.php"
    assert call.uuid == "1_1"


def test_base_path_is_a_json_string_of_file_names():
    """В схеме phone_asr base_path объявлен строкой, а не списком."""
    call = phone_compat.PhoneRequest(
        call_id="1", base_url="https://h",
        files=["https://h/rec/оператор.wav", "https://h/rec/абонент.wav"])
    value = call.base_path()
    assert isinstance(value, str)
    assert json.loads(value) == ["оператор.wav", "абонент.wav"]


# ---------------------------------------------------------------------------
# Тело обратного вызова
# ---------------------------------------------------------------------------


def test_callback_body_has_exactly_the_phone_asr_fields():
    """Набор полей сверяется целиком: лишнее и недостающее одинаково плохо."""
    call = phone_compat.PhoneRequest(call_id="c1", files=["https://h/a.wav"],
                                     base_url="https://h", part=2, total_parts=3)
    job = {"status": "completed", "media_duration_s": 12.5, "waveform": [
        {"audio_waveform": [{"time": 0.0, "amplitude": 0.1}],
         "sample_rate": 16000, "speaker": 0, "label": "Оператор"}]}
    segments = [{"speaker": "SPEAKER_00", "text": "Добрый день", "start": 0.0, "end": 1.5},
                {"speaker": "SPEAKER_01", "text": "  ", "start": 2.0, "end": 3.0}]

    body = phone_compat.callback_body(call, job, segments)
    assert sorted(body) == sorted([
        "call_id", "base_path", "part", "total_parts", "true_duration",
        "sentiment", "waveforms", "formatted_dialogue", "transcription",
        "status", "error_message"])
    assert body["status"] == "success"
    assert body["sentiment"] == "neutral", "поле есть в схеме, значение всегда это"
    assert body["part"] == 2 and body["total_parts"] == 3
    assert body["true_duration"] == 12.5
    # Пустая реплика в диалог не идёт, иначе принимающая сторона рисует пустую строку.
    assert len(body["formatted_dialogue"]) == 1
    item = body["formatted_dialogue"][0]
    assert sorted(item) == ["dialogue", "speaker", "time", "time_end"]
    assert item["speaker"] == "SPEAKER_00"
    assert body["transcription"] == "Добрый день"
    # waveforms — массив СТРОК, и внутри ровно два ключа phone_asr.
    assert isinstance(body["waveforms"][0], str)
    assert sorted(json.loads(body["waveforms"][0])) == ["audio_waveform", "speaker"]


def test_time_end_is_never_zero():
    """В схеме phone_asr time_end объявлен PositiveFloat — ноль её не пройдёт."""
    call = phone_compat.PhoneRequest(call_id="c", files=[], base_url="https://h")
    body = phone_compat.callback_body(
        call, {"status": "completed", "media_duration_s": 0},
        [{"speaker": "SPEAKER_00", "text": "щелчок", "start": 1.0, "end": 1.0}])
    assert body["formatted_dialogue"][0]["time_end"] > 0
    assert body["true_duration"] > 0


def test_failed_job_becomes_failed_callback():
    call = phone_compat.PhoneRequest(call_id="c", files=[], base_url="https://h")
    body = phone_compat.callback_body(
        call, {"status": "failed", "error_message": "нет места на диске"}, [])
    assert body["status"] == "failed"
    assert body["error_message"] == "нет места на диске"


# ---------------------------------------------------------------------------
# Полный обмен
# ---------------------------------------------------------------------------


def _mono_wav(path: Path, freq: float = 320.0, seconds: float = 4.0) -> Path:
    rate, frames = 16000, bytearray()
    for index in range(int(rate * seconds)):
        second = index / rate
        amplitude = 0.0 if 1.5 <= second < 2.2 else 0.4
        frames += struct.pack("<h", int(amplitude * 32767
                                        * math.sin(2 * math.pi * freq * second)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


class _Receiver:
    """Файловый сервер и приёмник обратных вызовов в одном лице."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.received: list[dict] = []
        outer = self

        class Handler(SimpleHTTPRequestHandler):
            def translate_path(self, path):                      # noqa: N802
                name = os.path.basename(unquote(path.split("?")[0]))
                return str(outer.directory / name)

            def do_POST(self):                                   # noqa: N802
                size = int(self.headers.get("Content-Length", 0))
                outer.received.append(json.loads(self.rfile.read(size)))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

            def log_message(self, *args):                        # noqa: A003
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture()
def receiver(tmp_path: Path):
    box = _Receiver(tmp_path)
    try:
        yield box
    finally:
        box.close()


@pytest.fixture()
def phone_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_WEBHOOK_ALLOW_INTERNAL", "true")
    app = create_app(load(), start_queue=True)
    with TestClient(app) as client:
        client.headers.update({
            "X-API-Key": (tmp_path / "data" / "api-key.txt").read_text().strip()})
        yield client


@pytest.mark.slow
def test_two_channels_go_all_the_way_to_the_callback(phone_client, receiver,
                                                     tmp_path: Path):
    """Полный обмен: запрос, скачивание, задание, обратный вызов.

    Проверяется то, ради чего всё делалось: приложение отдаёт ссылки и
    получает результат в своей схеме, ничего у себя не меняя.
    """
    import time

    _mono_wav(tmp_path / "оператор.wav", freq=320.0)
    _mono_wav(tmp_path / "абонент.wav", freq=480.0)

    response = phone_client.post("/process-call", json={
        "call_id": "звонок-4821", "base_url": receiver.base,
        "files": [f"{receiver.base}/оператор.wav", f"{receiver.base}/абонент.wav"],
    })
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "accepted"
    assert "звонок-4821" in body["message"]
    assert body["message"].endswith("/callback-endpoint.php.")

    for _ in range(180):
        if receiver.received:
            break
        time.sleep(1)
    assert receiver.received, "обратный вызов не пришёл"

    call = receiver.received[0]
    assert call["status"] == "success", call.get("error_message")
    assert call["call_id"] == "звонок-4821"
    assert json.loads(call["base_path"]) == ["оператор.wav", "абонент.wav"]
    assert call["true_duration"] == pytest.approx(4.0, abs=0.2)
    speakers = {item["speaker"] for item in call["formatted_dialogue"]}
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}, \
        f"стороны разговора не различены: {speakers}"
    assert len(call["waveforms"]) == 2
    assert sorted(json.loads(call["waveforms"][0])) == ["audio_waveform", "speaker"]

    statuses = phone_client.get("/statuses").json()
    assert statuses == [{"call_id": "звонок-4821", "part": 1, "status": "success"}]


def test_unreachable_file_still_answers_the_caller(phone_client, receiver):
    """Молчание — худший исход: на 202 ждут результат, которого не будет."""
    import time

    response = phone_client.post("/process-call", json={
        "call_id": "нет-файла", "base_url": receiver.base,
        "files": [f"{receiver.base}/такого-нет.wav"],
    })
    assert response.status_code == 202
    for _ in range(60):
        if receiver.received:
            break
        time.sleep(0.5)
    assert receiver.received, "об отказе не сообщили"
    call = receiver.received[0]
    assert call["status"] == "failed"
    assert call["error_message"], "отказ без причины"
    assert call["formatted_dialogue"] == [] and call["transcription"] == ""


def test_readonly_key_cannot_send_calls(phone_client):
    """Расшифровка — работа, а не чтение."""
    key = phone_client.post("/api/keys",
                            json={"name": "чтение", "role": "readonly"}).json()["key"]
    response = phone_client.post("/process-call", json={
        "call_id": "c", "base_url": "https://example.com",
        "files": ["https://example.com/a.wav"]}, headers={"X-API-Key": key})
    assert response.status_code == 403


def test_internal_callback_is_refused_by_default(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch):
    """Адрес приходит из запроса и уходит в urlopen — без проверки это дыра.

    Отказываем сразу при приёме: взять разговор и через минуту обнаружить,
    что результат некуда деть, — хуже, чем честный отказ.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as client:
        client.headers.update({
            "X-API-Key": (tmp_path / "data" / "api-key.txt").read_text().strip()})
        response = client.post("/process-call", json={
            "call_id": "c", "base_url": "http://169.254.169.254",
            "files": ["http://169.254.169.254/a.wav"]})
    assert response.status_code == 400
    assert "внутренней сети" in response.text


def test_the_switch_that_the_hint_names_actually_exists():
    """Подсказка звала включить параметр, которого не было в каталоге.

    То есть предлагала сделать невозможное: config.yaml с этим ключом
    сервер отвергал как «неизвестные параметры», и приёмник в локальной
    сети — обычный случай для телефонии — оставался недостижим.
    """
    from asrhub.catalog import PARAMS_BY_KEY

    assert "webhook_allow_internal" in PARAMS_BY_KEY
    assert "phone_compat_enabled" in PARAMS_BY_KEY
    assert "phone_compat_callback_suffix" in PARAMS_BY_KEY


def test_call_details_survive_the_settings_filter():
    """`merged` пропускает только ключи каталога и всё прочее выбрасывает.

    Приписка о разговоре уходила внутрь merged и исчезала: задание считалось,
    а результат уходил обычным телом ASR Hub вместо схемы phone_asr — то
    есть ровно то, ради чего всё делалось, тихо не работало.
    """
    from asrhub.api.routes_phone import _settings_for
    from asrhub.config import load

    class _State:
        settings = load()

    call = phone_compat.PhoneRequest(call_id="c9", files=["https://h/a.wav"],
                                     base_url="https://h", part=2, total_parts=5)
    fetched = phone_compat.Fetched(path=Path("/tmp/x.wav"), channels=2, filename="a.wav")
    settings = _settings_for(_State(), call, fetched)
    assert settings["_phone"]["call_id"] == "c9"
    assert settings["_phone"]["part"] == 2
    assert settings["diarization_backend"] == "channels"
    assert settings["speaker_names"] == "SPEAKER_00,SPEAKER_01"


def test_swap_sides_swaps_the_labels():
    from asrhub.api.routes_phone import _settings_for
    from asrhub.config import load

    class _State:
        settings = load()

    call = phone_compat.PhoneRequest(call_id="c", files=[], base_url="https://h",
                                     swap_sides=True)
    fetched = phone_compat.Fetched(path=Path("/tmp/x.wav"), channels=2, filename="a.wav")
    settings = _settings_for(_State(), call, fetched)
    assert settings["speaker_names"] == "SPEAKER_01,SPEAKER_00"


def test_more_than_two_files_are_refused(tmp_path: Path):
    from asrhub.errors import ASRHubError

    with pytest.raises(ASRHubError) as info:
        phone_compat.download(["a", "b", "c"], tmp_path, limit_bytes=0)
    assert "один файл или два" in info.value.message


# ---------------------------------------------------------------------------
# Разделение по каналам
# ---------------------------------------------------------------------------


def test_channel_beats_the_engine_guess(tmp_path: Path, data_dir: Path):
    """Стереозапись звонка приходила с одним говорящим на обе стороны.

    Стояло `segment.speaker or label`, то есть метка канала применялась
    только к сегментам без говорящего, — а движки, которые расставляют
    говорящих сами, заполняют это поле всегда. Разделение по каналам, про
    которое в справочнике сказано «безошибочно для стереозаписей», молча не
    работало именно там, где оно и нужно.
    """
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        pytest.skip("нужен ffmpeg")
    from asrhub import processor
    from asrhub.config import load
    from asrhub.engines import EngineRegistry

    left = _mono_wav(tmp_path / "left.wav", freq=320.0)
    right = _mono_wav(tmp_path / "right.wav", freq=480.0)
    stereo = tmp_path / "stereo.wav"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", str(left), "-i", str(right), "-filter_complex",
                    "[0:a][1:a]amerge=inputs=2[o]", "-map", "[o]", "-ac", "2",
                    str(stereo)], check=True, timeout=120)

    settings = load().merged({
        "model": "demo-simulator", "engine": "demo", "vad_backend": "energy",
        "audio_channels": "split", "diarization_enabled": True,
        "diarization_backend": "channels",
        "speaker_names": "SPEAKER_00,SPEAKER_01"})
    workdir, outdir = tmp_path / "w", tmp_path / "o"
    workdir.mkdir()
    outdir.mkdir()
    outcome = processor.process_job(stereo, settings, EngineRegistry(),
                                    workdir=workdir, outdir=outdir, basename="звонок")
    speakers = {segment["speaker"] for segment in outcome.segments}
    assert speakers == {"SPEAKER_00", "SPEAKER_01"}, \
        f"стороны разговора слились в одну: {speakers}"


def test_api_key_is_accepted_in_the_request_body(data_dir, monkeypatch):
    """phone_asr присылает ключ полем api_key в теле, а не заголовком.

    Заголовка X-API-Key в этом запросе нет вовсе, и без разбора тела
    совместимый клиент получал 401 при формально верном ключе — то есть
    совместимость, ради которой всё делалось, не работала на первом же шаге.
    """
    from pathlib import Path

    from asrhub.api.app import create_app
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    with TestClient(create_app(start_queue=False)) as client:
        key = (Path(data_dir) / "api-key.txt").read_text(encoding="utf-8").strip()
        body = {
            "call_id": "12345",
            "base_url": "http://127.0.0.1:9/проект",
            "api_key": key,
            "files": ["http://127.0.0.1:9/запись.wav"],
        }
        # Внутренние адреса по умолчанию запрещены — проверяем то, что нужно:
        # ключ принят (иначе был бы 401, а не отказ по адресу).
        accepted = client.post("/api/process-call", json=body)
        assert accepted.status_code != 401, accepted.text

        refused = client.post("/api/process-call", json=dict(body, api_key="ah_чужой"))
        assert refused.status_code == 401
        assert refused.json()["code"] == "auth_error"

        # Тот же адрес без префикса /api — как у phone_asr.
        assert client.post("/process-call", json=body).status_code != 401

        # Ключ не должен попасть в задание: он не параметр распознавания.
        assert "api_key" not in str(body.get("settings", {}))


def test_callback_url_with_cyrillic_is_delivered(tmp_path, monkeypatch):
    """Кириллица в адресе обратного вызова ломала доставку молча.

    urllib требует уже закодованный путь и бросает «'ascii' codec can't
    encode characters», не дойдя до сети. Пять попыток подряд заканчивались
    пометкой «failed», а вызывающая сторона не получала ничего — при том что
    имя проекта в пути обратного вызова обычно как раз русское.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):                      # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            received.append((self.path, self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):           # тишина в выводе тестов
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from asrhub.db import Database
        from asrhub.job_queue import JobQueue

        queue = JobQueue.__new__(JobQueue)      # без запуска рабочих потоков
        queue.db = Database(tmp_path / "asrhub.db")
        queue.settings = _FakeSettings()
        job = {"id": "job_1", "webhook_url": f"http://127.0.0.1:{port}/проект/callback.php"}
        queue.db.execute(
            "INSERT INTO jobs (id, status, created_at, updated_at) "
            "VALUES (?, 'completed', 0, 0)", ["job_1"])
        queue._deliver_webhook(job, json.dumps({"ok": True}).encode("utf-8"))
    finally:
        server.shutdown()

    assert received, "уведомление не доставлено"
    path, payload = received[0]
    assert path == "/%D0%BF%D1%80%D0%BE%D0%B5%D0%BA%D1%82/callback.php"
    assert json.loads(payload) == {"ok": True}


class _FakeSettings:
    """Минимум, который нужен доставке уведомления."""

    def get(self, key, default=None):
        return {"webhook_secret": "", "webhook_workers": 1}.get(key, default)
