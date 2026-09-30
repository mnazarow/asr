"""Заход 48: движки, которые молча не делали обещанного.

whisper.cpp — рекомендуемый движок для Mac — не работал ни с одной моделью
large-v3: ключ `-dtw large-v3-turbo` whisper-cli отвергает («unknown DTW
preset», код 3), а слова включены по умолчанию. Короткий JSON `-oj` не несёт
токенов, поэтому слов и уверенности не было и у остальных моделей. Скачать
веса было нечем: общая загрузка принимала «large-v3-turbo» за модель
faster-whisper. Сборка под Mac включала Core ML без права отступить, а
энкодера Core ML не скачивает никто, — whisper-cli не запускался вовсе.

`diarization_backend: channels` ничего не включал: каналы сводились в моно,
а говорящие молча расставлялись по паузам. Раздельная обработка каналов
брала только два первых — совещание «по микрофону на человека» теряло
остальных.

Фальшивый whisper-cli ниже повторяет то, что проверено на настоящем
(сборка из исходников ggml-org/whisper.cpp): отказ на незнакомом наборе DTW
и токены только в полном JSON.
"""
from __future__ import annotations

import http.server
import json
import math
import os
import shutil
import struct
import subprocess
import sys
import threading
import wave
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
НУЖЕН_FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")


# ---------------------------------------------------------------------------
# Разбор вывода whisper-cli
# ---------------------------------------------------------------------------

#: Вывод `whisper-cli -ojf` в том виде, в каком его пишет настоящая программа:
#: токены — куски слов с пробелом впереди у начала слова, служебные метки
#: «[_…]», смещения в миллисекундах.
ВЫВОД = {
    "result": {"language": "ru"},
    "transcription": [
        {"offsets": {"from": 0, "to": 2400}, "text": " Привет, мир!",
         "tokens": [
             {"text": "[_BEG_]", "offsets": {"from": 0, "to": 0}, "p": 0.9},
             {"text": " При", "offsets": {"from": 0, "to": 400}, "p": 0.9},
             {"text": "вет", "offsets": {"from": 400, "to": 800}, "p": 0.8},
             {"text": ",", "offsets": {"from": 800, "to": 850}, "p": 1.0},
             {"text": " мир", "offsets": {"from": 900, "to": 1500}, "p": 0.7},
             {"text": "!", "offsets": {"from": 1500, "to": 1550}, "p": 0.6},
             {"text": "[_TT_120]", "offsets": {"from": 2400, "to": 2400}, "p": 0.01},
         ]},
        {"offsets": {"from": 29000, "to": 31000}, "text": " «Как дела?» (пауза)",
         "tokens": [
             {"text": " «", "offsets": {"from": 29000, "to": 29100}, "p": 0.5},
             {"text": "Как", "offsets": {"from": 29100, "to": 29400}, "p": 0.5},
             {"text": " дела", "offsets": {"from": 29500, "to": 30400}, "p": 0.5},
             {"text": "?", "offsets": {"from": 30400, "to": 30500}, "p": 0.5},
             # Закрывающая кавычка с пробелом впереди — всё равно к слову
             # перед ней, открывающая скобка — к слову после.
             {"text": " »", "offsets": {"from": 30500, "to": 30600}, "p": 0.5},
             {"text": " (", "offsets": {"from": 30600, "to": 30650}, "p": 0.5},
             {"text": " пауза", "offsets": {"from": 30650, "to": 30900}, "p": 0.5},
             {"text": ")", "offsets": {"from": 30900, "to": 31000}, "p": 0.5},
         ]},
    ],
}


def test_токены_whisper_cpp_складываются_в_слова():
    """Раньше каждый токен становился «словом»: «При», «вет», «,»."""
    from asrhub.engines.whisper_cpp_engine import разобрать_вывод

    реплики = разобрать_вывод(ВЫВОД, пословно=True, длительность=None)
    assert [р.text for р in реплики] == ["Привет, мир!", "«Как дела?» (пауза)"]
    assert [с["word"] for с in реплики[0].words] == ["Привет,", "мир!"]
    assert [с["word"] for с in реплики[1].words] == ["«Как", "дела?»", "(пауза)"]
    привет = реплики[0].words[0]
    assert (привет["start"], привет["end"]) == (0.0, 0.85)
    assert привет["confidence"] == pytest.approx((0.9 + 0.8 + 1.0) / 3, abs=1e-4)
    # Уверенность реплики — без служебных меток с их копеечной вероятностью.
    assert реплики[0].confidence == pytest.approx((0.9 + 0.8 + 1.0 + 0.7 + 0.6) / 5, abs=1e-4)
    assert реплики[0].language == "ru"


def test_таймкоды_whisper_cpp_не_выходят_за_конец_записи():
    from asrhub.engines.whisper_cpp_engine import разобрать_вывод

    реплики = разобрать_вывод(ВЫВОД, пословно=True, длительность=30.0)
    assert реплики[1].end == 30.0 and реплики[1].start == 29.0
    assert all(с["end"] <= 30.0 and с["start"] <= с["end"] for с in реплики[1].words)
    assert разобрать_вывод(ВЫВОД, пословно=False)[0].words == []


# ---------------------------------------------------------------------------
# Вызов whisper-cli: фальшивая программа с поведением настоящей
# ---------------------------------------------------------------------------

ФАЛЬШИВЫЙ_CLI = r'''#!{python}
import json, sys
арг = sys.argv[1:]
with open({журнал!r}, "a", encoding="utf-8") as ж:
    ж.write(json.dumps(арг) + "\n")
if "-dtw" in арг:
    набор = арг[арг.index("-dtw") + 1]
    if набор not in ("tiny", "tiny.en", "base", "base.en", "small", "small.en", "medium",
                     "medium.en", "large.v1", "large.v2", "large.v3", "large.v3.turbo"):
        print(f"error: unknown DTW preset '{{набор}}'", file=sys.stderr)
        sys.exit(3)
вывод = json.loads({вывод!r})
if "-ojf" not in арг:
    for пункт in вывод["transcription"]:
        пункт.pop("tokens", None)
with open(арг[арг.index("-of") + 1] + ".json", "w", encoding="utf-8") as ф:
    json.dump(вывод, ф, ensure_ascii=False)
'''


def _тон(path: Path, seconds: float = 3.0, channels: int = 1,
         частоты: tuple[float, ...] = (320.0, 480.0, 640.0, 800.0)) -> Path:
    rate, frames = 16000, bytearray()
    for index in range(int(rate * seconds)):
        second = index / rate
        amplitude = 0.0 if 1.2 <= second < 1.8 else 0.4
        for канал in range(channels):
            frames += struct.pack("<h", int(amplitude * 32767 * math.sin(
                2 * math.pi * частоты[канал % len(частоты)] * second)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


@pytest.fixture()
def whisper_cli(tmp_path: Path, monkeypatch, data_dir):
    """Фальшивый whisper-cli, каталог весов с моделью и журнал вызовов."""
    журнал = tmp_path / "вызовы.jsonl"
    программа = tmp_path / "whisper-cli"
    вывод = json.dumps({**ВЫВОД, "transcription": ВЫВОД["transcription"][:1]},
                       ensure_ascii=False)
    программа.write_text(ФАЛЬШИВЫЙ_CLI.format(python=sys.executable, журнал=str(журнал),
                                               вывод=вывод), encoding="utf-8")
    программа.chmod(0o755)
    веса = tmp_path / "модели"
    (веса / "whisper.cpp").mkdir(parents=True)
    (веса / "whisper.cpp" / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"lmgg" + b"\0" * 64)
    monkeypatch.setenv("ASRHUB_WHISPER_CPP", str(программа))
    monkeypatch.setenv("ASRHUB_MODELS_DIR", str(веса))
    monkeypatch.delenv("ASRHUB_WHISPER_CPP_VAD", raising=False)
    monkeypatch.delenv("ASRHUB_WHISPER_CPP_MODELS", raising=False)
    return {"журнал": журнал, "веса": веса}


def _распознать(tmp_path: Path, **настройки):
    from asrhub import processor
    from asrhub.config import load
    from asrhub.engines import EngineRegistry

    settings = load(apply_hardware=False).merged({
        "model": "whispercpp-large-v3-turbo-q5_0", "engine": "auto", "language": "ru",
        "vad_backend": "energy", "punctuation_enabled": False, "itn_enabled": False,
        **настройки})
    workdir, outdir = tmp_path / "w", tmp_path / "o"
    workdir.mkdir(exist_ok=True)
    outdir.mkdir(exist_ok=True)
    return processor.process_job(_тон(tmp_path / "запись.wav"), settings, EngineRegistry(),
                                 workdir=workdir, outdir=outdir, basename="запись")


def _вызовы(журнал: Path) -> list[list[str]]:
    return [json.loads(строка) for строка in журнал.read_text(encoding="utf-8").splitlines()]


def test_large_v3_turbo_распознаётся_и_отдаёт_слова(tmp_path: Path, whisper_cli):
    """Раньше: «-dtw large-v3-turbo» → «unknown DTW preset», задание падало."""
    итог = _распознать(tmp_path, word_timestamps=True, vad_enabled=False)
    вызов = _вызовы(whisper_cli["журнал"])[-1]
    assert "-dtw" not in вызов
    assert "-ojf" in вызов
    слова = [с["word"] for р in итог.segments for с in р.get("words") or []]
    assert слова[:2] == ["Привет,", "мир!"], итог.segments
    assert итог.segments[0].get("confidence") is not None


def test_поиск_речи_whisper_cpp_берёт_модель_из_каталога_весов(tmp_path: Path, whisper_cli):
    """Модель для --vad бралась только из переменной, которую нигде не
    задавали: «Поиск речи» был включён и не делал ничего."""
    итог = _распознать(tmp_path, vad_enabled=True)
    assert "--vad" not in _вызовы(whisper_cli["журнал"])[-1]
    assert any("Поиск речи у whisper.cpp не работает" in п for п in итог.warnings), итог.warnings

    модель = whisper_cli["веса"] / "whisper.cpp" / "ggml-silero-v6.2.0.bin"
    модель.write_bytes(b"lmgg")
    итог = _распознать(tmp_path, vad_enabled=True)
    вызов = _вызовы(whisper_cli["журнал"])[-1]
    assert вызов[вызов.index("--vad") + 2] == str(модель)
    assert not any("Поиск речи у whisper.cpp" in п for п in итог.warnings)


# ---------------------------------------------------------------------------
# Загрузка весов whisper.cpp
# ---------------------------------------------------------------------------


class _Раздача:
    """Локальная раздача файлов вместо Hugging Face."""

    def __init__(self, файлы: dict[str, tuple[int, bytes, dict]]):
        self.файлы = файлы
        self.запросы: list[str] = []
        раздача = self

        class Обработчик(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                раздача.запросы.append(self.path)
                код, тело, заголовки = раздача.файлы.get(self.path, (404, b"", {}))
                self.send_response(код)
                self.send_header("Content-Length", str(заголовки.get("length", len(тело))))
                self.end_headers()
                self.wfile.write(тело)

            def log_message(self, *args):
                pass

        self.сервер = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Обработчик)
        threading.Thread(target=self.сервер.serve_forever, daemon=True).start()
        self.адрес = f"http://127.0.0.1:{self.сервер.server_address[1]}/"

    def закрыть(self):
        self.сервер.shutdown()
        self.сервер.server_close()


@pytest.fixture()
def раздача(monkeypatch):
    from asrhub.engines import whisper_cpp_engine as w

    сервер = _Раздача({})
    monkeypatch.setattr(w, "АДРЕС_ВЕСОВ", сервер.адрес + "w/")
    monkeypatch.setattr(w, "АДРЕС_VAD", сервер.адрес + "v/")
    monkeypatch.delenv("ASRHUB_WHISPER_CPP_VAD", raising=False)
    monkeypatch.delenv("ASRHUB_WHISPER_CPP_MODELS", raising=False)
    yield сервер
    сервер.закрыть()


def test_веса_whisper_cpp_качаются_файлом_ggml_туда_где_их_ищет_движок(tmp_path, раздача):
    from asrhub import catalog, model_files
    from asrhub.engines import whisper_cpp_engine as w

    spec = catalog.get_model("whispercpp-large-v3-turbo-q5_0")
    веса = b"lmgg" + os.urandom(3000)
    раздача.файлы["/w/" + spec.source] = (200, веса, {})
    раздача.файлы["/v/" + w.ФАЙЛ_VAD] = (200, b"lmgg" + b"\1" * 10, {})

    доли: list[float] = []
    путь = w.download_weights(spec.source, tmp_path, progress=доли.append)
    assert путь == tmp_path / "whisper.cpp" / "ggml-large-v3-turbo-q5_0.bin"
    assert путь.read_bytes() == веса and доли and доли[-1] == 1.0
    assert (tmp_path / "whisper.cpp" / w.ФАЙЛ_VAD).exists()
    assert not list(tmp_path.rglob("*.part"))
    # Сервер видит модель скачанной, и отпечаток весов у неё есть.
    assert model_files.find_local(tmp_path, spec.source) == путь
    assert model_files.fingerprint(tmp_path, spec.source)
    # Повтор без --force не качает заново.
    запросов = len(раздача.запросы)
    w.download_weights(spec.source, tmp_path)
    assert len(раздача.запросы) == запросов


@pytest.mark.parametrize("ответ, ожидание", [
    ((404, b"", {}), "нет файла"),
    ((200, b"<html>login</html>", {}), "пришло что-то другое"),
    ((200, b"lmgg" + b"\0" * 10, {"length": 5000}), "оборвалась"),
])
def test_неудачная_загрузка_не_оставляет_обрубка_весов(tmp_path, раздача, ответ, ожидание):
    from asrhub.engines import whisper_cpp_engine as w
    from asrhub.errors import EngineError

    раздача.файлы["/w/ggml-tiny.bin"] = ответ
    with pytest.raises(EngineError) as отказ:
        w.download_weights("ggml-tiny.bin", tmp_path)
    assert ожидание in отказ.value.message, отказ.value.message
    assert not (tmp_path / "whisper.cpp" / "ggml-tiny.bin").exists()
    assert not list(tmp_path.rglob("*.part"))


def test_веса_whisper_cpp_не_путаются_с_faster_whisper(tmp_path):
    """Источник «tiny» у whisper.cpp совпадал с моделью faster-whisper: её
    каталог считался весами whisper.cpp, и наоборот."""
    from asrhub import catalog, model_files

    (tmp_path / "models--Systran--faster-whisper-tiny").mkdir()
    spec = catalog.get_model("whispercpp-tiny")
    assert model_files.find_local(tmp_path, spec.source) is None
    assert model_files.find_local(tmp_path, catalog.get_model("faster-whisper-tiny").source) \
        is not None


def test_раздел_модели_качает_whisper_cpp_своей_загрузкой(client, monkeypatch):
    from asrhub.engines import whisper_cpp_engine as w

    вызовы = []
    monkeypatch.setattr(w, "download_weights",
                        lambda source, models_dir, **kw: вызовы.append(source))
    ответ = client.post("/api/models/whispercpp-large-v3-q5_0/download")
    assert ответ.status_code == 200, ответ.text
    for _ in range(200):
        состояние = client.get("/api/models/whispercpp-large-v3-q5_0/status").json().get("download")
        if (состояние or {}).get("status") != "running":
            break
        threading.Event().wait(0.05)
    assert вызовы == ["ggml-large-v3-q5_0.bin"], состояние
    assert состояние["status"] == "completed", состояние


def test_удаление_весов_одним_файлом_удаляет_файл(client, data_dir):
    """rmtree на файле молча не делал ничего, а ответ говорил «удалено»."""
    from asrhub import catalog, model_files

    состояние = client.app.state.hub
    каталог = Path(состояние.settings.get("models_dir") or состояние.settings.paths.models)
    spec = catalog.get_model("whispercpp-tiny")
    файл = каталог / "whisper.cpp" / spec.source
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_bytes(b"lmgg" + b"\0" * 100)
    ответ = client.delete("/api/models/whispercpp-tiny")
    assert ответ.status_code == 200 and ответ.json()["removed"] is True, ответ.text
    assert not файл.exists()
    assert model_files.find_local(каталог, spec.source) is None


def _models_sh(tmp_path: Path, *аргументы: str, путь: str | None = None):
    окружение = {**os.environ, "ASRHUB_DATA_DIR": str(tmp_path / "данные"),
                 "ASRHUB_ASSUME_YES": "1", "NO_COLOR": "1"}
    окружение.pop("ASRHUB_WHISPER_CPP_VAD", None)
    if путь is not None:
        окружение["PATH"] = путь
    return subprocess.run(["bash", str(КОРЕНЬ / "scripts" / "models.sh"), *аргументы],
                          capture_output=True, text=True, timeout=300, env=окружение)


def test_models_sh_download_берёт_веса_whisper_cpp_своей_загрузкой(tmp_path: Path):
    """Раньше `models.sh download whispercpp-tiny` звал загрузку Hugging Face
    для репозитория faster-whisper."""
    каталог = tmp_path / "данные" / "models" / "whisper.cpp"
    каталог.mkdir(parents=True)
    (каталог / "ggml-tiny.bin").write_bytes(b"lmgg")
    (каталог / "ggml-silero-v6.2.0.bin").write_bytes(b"lmgg")
    итог = _models_sh(tmp_path, "download", "whispercpp-tiny")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert f"Готово: {каталог / 'ggml-tiny.bin'}" in итог.stdout, итог.stdout
    assert not list((tmp_path / "данные" / "models").glob("models--*"))


def test_models_sh_install_engine_whisper_cpp_собирает_программу(tmp_path: Path):
    """`install-engine whisper-cpp` ставил пустой список пакетов Python и
    отвечал «установлен», а whisper-cli так и не появлялся."""
    заглушки = tmp_path / "bin"
    заглушки.mkdir()
    for имя in ("git", "cmake"):
        (заглушки / имя).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (заглушки / имя).chmod(0o755)
    установка = tmp_path / "установка"
    (установка / "whisper.cpp" / ".git").mkdir(parents=True)
    итог = _models_sh(tmp_path, "install-engine", "whisper_cpp", "--prefix", str(установка),
                      путь=f"{заглушки}{os.pathsep}{os.environ['PATH']}")
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 1, вывод
    assert "whisper.cpp собрать не удалось" in вывод, вывод
    assert "установлен" not in вывод, вывод


def test_сборка_whisper_cpp_под_mac_разрешает_отступить_с_core_ml():
    """Без WHISPER_COREML_ALLOW_FALLBACK whisper-cli без энкодера Core ML
    не запускался — а энкодер не скачивает никто."""
    текст = (КОРЕНЬ / "scripts" / "lib" / "whispercpp.sh").read_text(encoding="utf-8")
    строка = next(с for с in текст.splitlines() if с.strip().startswith("mps)"))
    assert "-DWHISPER_COREML_ALLOW_FALLBACK=1" in строка, строка


# ---------------------------------------------------------------------------
# Разделение по каналам
# ---------------------------------------------------------------------------


def _многоканальная(tmp_path: Path, каналов: int) -> Path:
    return _тон(tmp_path / f"каналы{каналов}.wav", seconds=3.0, channels=каналов)


def _обработать(tmp_path: Path, запись: Path, **настройки):
    from asrhub import processor
    from asrhub.config import load
    from asrhub.engines import EngineRegistry

    settings = load(apply_hardware=False).merged({
        "model": "demo-simulator", "engine": "demo", "vad_backend": "energy", **настройки})
    workdir, outdir = tmp_path / "w", tmp_path / "o"
    workdir.mkdir(exist_ok=True)
    outdir.mkdir(exist_ok=True)
    return processor.process_job(запись, settings, EngineRegistry(),
                                 workdir=workdir, outdir=outdir, basename="звонок")


@НУЖЕН_FFMPEG
def test_диаризация_по_каналам_сама_разделяет_стереозапись(tmp_path: Path, data_dir):
    """Выбор «по каналам» без `audio_channels: split` сводил запись в моно."""
    итог = _обработать(tmp_path, _многоканальная(tmp_path, 2),
                       diarization_enabled=True, diarization_backend="channels")
    assert {с["speaker"] for с in итог.segments} == {"Канал 1", "Канал 2"}, итог.segments


@НУЖЕН_FFMPEG
def test_раздельная_обработка_берёт_все_каналы(tmp_path: Path, data_dir):
    """Четыре канала — четыре говорящих, а не два первых."""
    итог = _обработать(tmp_path, _многоканальная(tmp_path, 4), audio_channels="split",
                       speaker_names="Анна,Борис,Вера,Глеб")
    assert {с["speaker"] for с in итог.segments} == {"Анна", "Борис", "Вера", "Глеб"}


@НУЖЕН_FFMPEG
def test_раздельная_обработка_одноканальной_записи_предупреждает(tmp_path: Path, data_dir):
    итог = _обработать(tmp_path, _многоканальная(tmp_path, 1), audio_channels="split")
    assert any("одноканальная" in п for п in итог.warnings), итог.warnings


@pytest.mark.parametrize("способ, причина", [
    ("channels", "каналов для разделения нет"),
    ("builtin", "не расставляет говорящих сама"),
])
def test_разбивка_по_паузам_вместо_выбранного_не_молчит(tmp_path: Path, способ, причина):
    """На одноканальной записи «по каналам» молча становилось «по паузам»."""
    from asrhub.engines.base import Segment
    from asrhub.pipeline.diarization import diarize_segments

    реплики = [Segment(start=0.0, end=1.0, text="раз"), Segment(start=3.0, end=4.0, text="два")]
    предупреждения: list[str] = []
    итог = diarize_segments(tmp_path / "нет.wav", реплики,
                            {"diarization_backend": способ, "model": "demo-simulator"},
                            предупреждения)
    assert all(р.speaker for р in итог)
    assert len(предупреждения) == 1 and причина in предупреждения[0], предупреждения


@НУЖЕН_FFMPEG
def test_arnndn_без_модели_предупреждает_в_задании(tmp_path: Path, monkeypatch):
    """Шумоподавление arnndn без файла модели пропускалось с одной строкой
    в журнале сервера: в задании стояло «arnndn», как будто оно было."""
    from asrhub.pipeline import audio

    monkeypatch.delenv("ASRHUB_ARNNDN_MODEL", raising=False)
    запись = _тон(tmp_path / "шум.wav")
    итог = audio.prepare(запись, tmp_path / "w", {"audio_denoise": "arnndn",
                                                  "audio_denoise_below_snr_db": 0})
    assert any("ASRHUB_ARNNDN_MODEL" in з for з in итог.warnings), итог.warnings
    итог = audio.prepare(запись, tmp_path / "w2", {"audio_denoise": "afftdn",
                                                   "audio_denoise_below_snr_db": 0})
    assert not any("arnndn" in з for з in итог.warnings), итог.warnings


def test_whisper_cli_находится_в_каталоге_установки(tmp_path: Path, monkeypatch):
    """Установщик собирает whisper.cpp в `<установка>/whisper.cpp`, а сервер
    искал его в /opt/whisper.cpp и в PATH — находил только через переменную
    из env.sh, которую сервер вне службы не видит."""
    from asrhub.engines import whisper_cpp_engine as w

    установка = tmp_path / "asrhub"
    программа = установка / "whisper.cpp" / "build" / "bin" / "whisper-cli"
    программа.parent.mkdir(parents=True)
    программа.write_text("#!/bin/sh\n", encoding="utf-8")
    программа.chmod(0o755)
    (установка / "server" / "asrhub" / "engines").mkdir(parents=True)
    monkeypatch.setattr(w, "__file__",
                        str(установка / "server" / "asrhub" / "engines" / "whisper_cpp_engine.py"))
    monkeypatch.delenv("ASRHUB_WHISPER_CPP", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "пусто"))
    monkeypatch.setenv("HOME", str(tmp_path / "дом"))
    assert w.WhisperCppEngine._find_binary() == str(программа)


def test_models_sh_на_незнакомую_команду_печатает_справку_а_не_код(tmp_path: Path):
    итог = _models_sh(tmp_path, "нечто")
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 2, вывод
    assert "Неизвестная команда: нечто" in вывод and "models.sh download" in вывод, вывод
    assert "SCRIPT_DIR=" not in вывод and "source " not in вывод, вывод


def test_неудачная_загрузка_исходников_whisper_cpp_останавливает_сборку(tmp_path: Path):
    """Без `|| return 1` провал git clone не останавливал сборку: дальше
    дважды падал cmake на несуществующем каталоге."""
    заглушки = tmp_path / "bin"
    заглушки.mkdir()
    for имя in ("git", "cmake"):
        (заглушки / имя).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        (заглушки / имя).chmod(0o755)
    (заглушки / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (заглушки / "sleep").chmod(0o755)
    итог = _models_sh(tmp_path, "install-engine", "whisper_cpp", "--prefix",
                      str(tmp_path / "установка"),
                      путь=f"{заглушки}{os.pathsep}{os.environ['PATH']}")
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 1, вывод
    assert "whisper.cpp собрать не удалось" in вывод, вывод
    assert "Сборка (занимает" not in вывод, вывод


@НУЖЕН_FFMPEG
def test_каналы_многоканальной_записи_берутся_каждый_свой(tmp_path: Path):
    """Каждый канал — свой звук, а не одна и та же смесь четырёх."""
    from asrhub.pipeline import audio

    готово = audio.prepare(_многоканальная(tmp_path, 4), tmp_path / "w",
                           {"audio_channels": "split", "audio_normalize": False})
    assert [метка for метка, _ in готово.channels] == [f"Канал {i}" for i in range(1, 5)]

    def переходов(путь: Path) -> int:
        with wave.open(str(путь), "rb") as w:
            кадры = w.readframes(w.getnframes())
        отсчёты = struct.unpack(f"<{len(кадры) // 2}h", кадры)
        return sum(1 for a, b in zip(отсчёты[:-1], отсчёты[1:], strict=True)
                   if (a < 0) != (b < 0))

    частоты = [переходов(путь) for _, путь in готово.channels]
    # 320, 480, 640 и 800 Гц — число переходов через ноль растёт с частотой.
    assert частоты == sorted(частоты) and len(set(частоты)) == 4, частоты
