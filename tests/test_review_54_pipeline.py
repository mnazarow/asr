"""Заход 54: конвейер обработки звука — то, что осталось от ревизии 3.1.20.

Проверки поведения, а не текста исходника: каждая падает на коде до захода
и проходит после. Внешние программы, которых в среде нет (deep-filter,
модели пунктуации), заменены подделками ровно в том объёме, который нужен
серверу.
"""
from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
from pathlib import Path

import pytest

НУЖЕН_FFMPEG = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")


def _тон(секунд: float, частота: float, rate: int, громкость: float = 0.3) -> list[float]:
    return [громкость * math.sin(2 * math.pi * частота * i / rate)
            for i in range(int(секунд * rate))]


def _wav(путь: Path, каналы: list[list[float]], rate: int = 16000) -> Path:
    """WAV 16 бит из списков отсчётов по каналам (одинаковой длины)."""
    путь.parent.mkdir(parents=True, exist_ok=True)
    длина = max(len(к) for к in каналы)
    каналы = [к + [0.0] * (длина - len(к)) for к in каналы]
    кадры = bytearray()
    for i in range(длина):
        for канал in каналы:
            кадры += struct.pack("<h", int(max(-1.0, min(1.0, канал[i])) * 32767))
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(len(каналы))
        файл.setsampwidth(2)
        файл.setframerate(rate)
        файл.writeframes(bytes(кадры))
    return путь


def _сильнейшая_частота(путь: Path) -> float:
    np = pytest.importorskip("numpy")
    with wave.open(str(путь), "rb") as файл:
        rate = файл.getframerate()
        данные = np.frombuffer(файл.readframes(файл.getnframes()), dtype="<i2").astype(float)
    спектр = np.abs(np.fft.rfft(данные))
    return float(np.fft.rfftfreq(len(данные), 1 / rate)[спектр.argmax()])


# ---------------------------------------------------------------------------
# 06-pipeline №3: DeepFilterNet и раздельные каналы
# ---------------------------------------------------------------------------

@pytest.fixture()
def поддельный_deepfilter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`deep-filter -o КАТАЛОГ ФАЙЛ…` — копирует каждый вход в каталог как есть."""
    бинарь = tmp_path / "bin" / "deep-filter"
    бинарь.parent.mkdir(parents=True)
    бинарь.write_text('#!/bin/sh\n'
                      'out=""\n'
                      'while [ $# -gt 0 ]; do\n'
                      '  case "$1" in -o) out="$2"; shift 2;;\n'
                      '  *) cp "$1" "$out/$(basename "$1")"; shift;; esac\n'
                      'done\n', encoding="utf-8")
    бинарь.chmod(0o755)
    monkeypatch.setenv("ASRHUB_DEEPFILTER", str(бинарь))
    return бинарь


@НУЖЕН_FFMPEG
def test_deepfilternet_не_смешивает_собеседников_стереозаписи(tmp_path, поддельный_deepfilter):
    """Слева 300 Гц, справа 1200 Гц: после очистки каналы обязаны остаться своими.

    Прежде вход модели сводился в моно, и «Канал 1» получал обоих, а
    «Канал 2» — тишину.
    """
    from asrhub.pipeline import audio

    исходник = _wav(tmp_path / "звонок.wav", [_тон(3, 300, 16000), _тон(3, 1200, 16000)])
    готово = audio.prepare(исходник, tmp_path / "работа",
                           {"audio_channels": "split", "audio_denoise": "deepfilternet",
                            "audio_sample_rate": 16000})
    assert [метка for метка, _ in готово.channels] == ["Канал 1", "Канал 2"], готово
    assert готово.silent == [], готово.silent
    левый, правый = (путь for _, путь in готово.channels)
    assert abs(_сильнейшая_частота(левый) - 300) < 20
    assert abs(_сильнейшая_частота(правый) - 1200) < 20


@НУЖЕН_FFMPEG
def test_deepfilternet_и_правый_канал(tmp_path, поддельный_deepfilter):
    """«Взять правый канал» после очистки брал правый канал моно-файла — тишину."""
    from asrhub.pipeline import audio

    исходник = _wav(tmp_path / "звонок.wav", [_тон(3, 300, 16000), _тон(3, 1200, 16000)])
    готово = audio.prepare(исходник, tmp_path / "работа",
                           {"audio_channels": "right", "audio_denoise": "deepfilternet"})
    assert abs(_сильнейшая_частота(готово.channels[0][1]) - 1200) < 20


# ---------------------------------------------------------------------------
# 06-pipeline №6: запись без длительности в заголовке
# ---------------------------------------------------------------------------

@НУЖЕН_FFMPEG
def test_webm_без_длительности_в_заголовке_принимается(tmp_path: Path):
    """Запись потоком (как пишет MediaRecorder) не несёт длины — её меряют декодером."""
    from asrhub.pipeline import audio

    путь = tmp_path / "поток.webm"
    with путь.open("wb") as выход:
        готово = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=6", "-c:a", "libopus", "-f", "webm",
             "pipe:1"], stdout=выход, check=False)
    if готово.returncode != 0:
        pytest.skip("ffmpeg собран без libopus")
    сведения = audio.probe(путь)
    assert 5.5 < сведения.duration_s < 6.5, сведения.duration_s
    подготовлено = audio.prepare(путь, tmp_path / "работа", {"audio_trim_silence": True})
    assert подготовлено.channels


# ---------------------------------------------------------------------------
# 06-pipeline №17: тишина в конце MP3
# ---------------------------------------------------------------------------

@НУЖЕН_FFMPEG
def test_тишина_в_конце_mp3_обрезается(tmp_path: Path):
    from asrhub.pipeline import audio

    rate = 8000
    исходник = _wav(tmp_path / "звонок.wav",
                    [[0.0] * (3 * rate) + _тон(5, 400, rate) + [0.0] * (2 * rate)], rate=rate)
    mp3 = tmp_path / "звонок.mp3"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(исходник),
                    str(mp3)], check=True)
    начало, конец = audio.silence_bounds(mp3, {"audio_trim_silence": True},
                                         audio.probe(mp3).duration_s)
    assert 2.8 < начало < 3.2, начало
    assert конец is not None and 7.8 < конец < 8.3, конец


# ---------------------------------------------------------------------------
# 06-pipeline №18: молчащий выбранный канал — «речи нет», а не сбой фильтров
# ---------------------------------------------------------------------------

@НУЖЕН_FFMPEG
def test_молчащий_правый_канал_это_нет_речи(tmp_path: Path):
    from asrhub.errors import NoSpeechDetected
    from asrhub.pipeline import audio

    исходник = _wav(tmp_path / "звонок.wav", [_тон(4, 300, 16000), [0.0] * 64000])
    with pytest.raises(NoSpeechDetected):
        audio.prepare(исходник, tmp_path / "работа",
                      {"audio_channels": "right", "audio_trim_silence": True})


# ---------------------------------------------------------------------------
# 06-pipeline №19: путь к модели RNNoise в цепочке фильтров
# ---------------------------------------------------------------------------

@НУЖЕН_FFMPEG
@pytest.mark.parametrize("каталог", ["mod:els", "my models, v2", "it's", "a[1];b"])
def test_путь_к_модели_rnnoise_доходит_до_ffmpeg_целым(tmp_path, monkeypatch, каталог):
    """ffmpeg должен получить ровно этот путь — тогда он и назовёт его в отказе.

    Файл модели нарочно негодный: важно не шумоподавление, а то, что путь
    разобран верно (прежде ffmpeg отвечал «Unable to parse option value» или
    «No such filter»).
    """
    from asrhub.pipeline import audio

    модель = tmp_path / каталог / "std.rnnn"
    модель.parent.mkdir(parents=True)
    модель.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ASRHUB_ARNNDN_MODEL", str(модель))
    цепочка = audio.build_filter_chain({"audio_denoise": "arnndn"})
    модель.unlink()                      # пусть ffmpeg скажет, какой файл не открылся
    готово = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=0.2",
         "-af", цепочка, "-f", "null", "-"], capture_output=True, text=True, check=False)
    assert f"Failed to open model file: {модель}" in готово.stderr, готово.stderr


# ---------------------------------------------------------------------------
# 06-pipeline №12: замеры не декодируют видео; недоделка: замер SNR в каталоге задания
# ---------------------------------------------------------------------------

def test_замеры_не_трогают_видеодорожку(monkeypatch, tmp_path: Path):
    """Команды замеров — с -vn: видеодорожка в них не декодируется."""
    from asrhub.pipeline import audio

    команды: list[list[str]] = []

    def запуск(cmd, *a, **k):
        команды.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "time=00:00:01.00")

    monkeypatch.setattr(audio.subprocess, "run", запуск)
    monkeypatch.setattr(audio, "_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(audio.shutil, "which", lambda name: name)
    файл = tmp_path / "совещание.mp4"
    файл.write_bytes(b"\0" * 10)
    audio.analyze_levels(файл)
    audio.silence_bounds(файл, {"audio_trim_silence": True}, 1.0)
    audio.peak_db(файл)
    assert len(команды) == 3
    for команда in команды:
        assert "-vn" in команда and "-nostdin" in команда, команда


@НУЖЕН_FFMPEG
def test_замер_snr_не_мусорит_в_каталоге_загрузок(tmp_path: Path, monkeypatch):
    """Временный файл замера — в рабочем каталоге задания, а не рядом с загрузкой.

    Смотрим в каталог загрузок в момент замера: файл после замера удаляется,
    но при обрыве процесса он там и оставался.
    """
    from asrhub.pipeline import audio, audio_profile

    загрузки = tmp_path / "uploads"
    исходник = _wav(загрузки / "up_1.wav", [_тон(2, 300, 16000)])
    увидено: list[list[str]] = []
    настоящий = audio_profile.profile_file

    def замер(путь, **kw):
        увидено.append(sorted(п.name for п in загрузки.iterdir()))
        return настоящий(путь, **kw)

    monkeypatch.setattr(audio_profile, "profile_file", замер)
    audio.prepare(исходник, tmp_path / "работа",
                  {"audio_denoise": "afftdn", "audio_denoise_below_snr_db": 5.0})
    assert увидено == [["up_1.wav"]], увидено


# ---------------------------------------------------------------------------
# 06-pipeline №4: заглушение номера после нормализации чисел
# ---------------------------------------------------------------------------

def _карта() -> str:
    from asrhub.content import masking

    for цифра in "0123456789":
        if masking.luhn("427638001234567" + цифра):
            return "427638001234567" + цифра
    raise AssertionError("нет номера с верной суммой")


def _реплика_с_номером(после: list[str], текст_после: str) -> tuple[dict, float, float]:
    """Движок отдал номер словами, нормализация сделала из него цифры."""
    номер = _карта()
    группы = " ".join(номер[i:i + 4] for i in range(0, 16, 4))
    до = ["здравствуйте", "номер", "карты"]
    число = ["четыре", "тысячи", "двести", "семьдесят", "шесть", "три", "тысячи", "восемьсот", "тысяча", "двести", "тридцать", "четыре", "пять", "тысяч", "шестьсот", "семьдесят"]
    слова = до + число + после
    шаг = 30.0 / len(слова)
    words = [{"word": с, "start": round(i * шаг, 2), "end": round((i + 1) * шаг - 0.05, 2)}
             for i, с in enumerate(слова)]
    текст = "Здравствуйте, номер карты " + группы + текст_после
    первое, последнее = words[len(до)], words[len(до) + len(число) - 1]
    return ({"start": 0.0, "end": 30.0, "text": текст, "words": words},
            первое["start"], последнее["end"])


def test_номер_словами_заглушается_целиком():
    from asrhub.pipeline import redact

    реплика, начало, конец = _реплика_с_номером(
        ["спасибо", "большое", "до", "свидания"], ", спасибо большое, до свидания.")
    интервалы = [и for и in redact.найти_интервалы([реплика]) if "card" in и.kind]
    assert интервалы, "номер не заглушён"
    assert any(и.start <= начало and и.end >= конец for и in интервалы), (
        начало, конец, [и.to_dict() for и in интервалы])


def test_слово_номера_не_цепляется_за_такое_же_слово_дальше():
    """«три» из «три тысячи» совпадает с «три» в конце реплики — граница не та."""
    from asrhub.pipeline import redact

    реплика, начало, конец = _реплика_с_номером(
        ["я", "вас", "спрошу", "три", "вопроса", "смотрите"], ". Я вас спрошу три вопроса, смотрите.")
    интервалы = [и for и in redact.найти_интервалы([реплика]) if "card" in и.kind]
    assert any(и.start <= начало and и.end >= конец for и in интервалы), (
        начало, конец, [и.to_dict() for и in интервалы])


# ---------------------------------------------------------------------------
# 06-pipeline №7: встроенная нормализация чисел
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("сказано, ждём", [
    ("три пятых акций", "три пятых акций"),
    ("две третьих голосов", "две третьих голосов"),
    ("пять первых мест", "пять первых мест"),
    ("десять первых минут", "десять первых минут"),
    ("одна вторая", "одна вторая"),
    ("встреча в восемь ноль ноль", "встреча в восемь ноль ноль"),
    ("код ноль пять", "код ноль пять"),
    ("добавочный ноль ноль семь", "добавочный ноль ноль семь"),
    ("ни один из них", "ни один из них"),
    ("тысяча тысяч", "тысяча тысяч"),
    ("сто тысяч миллионов", "сто тысяч миллионов"),
    ("двадцать одно очко", "21 очко"),
])
def test_встроенная_нормализация_не_портит_дроби_и_нули(сказано, ждём):
    from asrhub.pipeline import postprocess

    assert postprocess._builtin_itn_ru(сказано) == ждём


@pytest.mark.parametrize("сказано, ждём", [
    ("в две тысячи двадцать четвёртом году", "в 2024-м году"),
    ("пять тысяч первый", "5001-й"),
    ("в тысяча девятьсот девяностых годах", "в 1990-х годах"),
    ("сто двадцать три рубля", "123 ₽"),
    ("ноль", "0"),
    # Одиночное «один» — слово, но в перечислении с числами — число.
    ("Один, два, три, поехали!", "1, 2, 3, поехали!"),
    ("Три, два, один, пуск!", "3, 2, 1, пуск!"),
    ("Я один дома.", "Я один дома."),
])
def test_нормализация_прежних_случаев_не_сломалась(сказано, ждём):
    """Страховка к исправлению выше: то, что работало, работает."""
    from asrhub.pipeline import postprocess

    assert postprocess._builtin_itn_ru(сказано) == ждём


# ---------------------------------------------------------------------------
# Недоделка 06: встроенный список слов-паразитов — только междометия
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("текст", ["Что это значит?", "Ты знаешь ответ.", "В общем зале тихо."])
def test_значимые_слова_не_считаются_паразитами(текст):
    from asrhub.pipeline import postprocess

    assert postprocess.remove_fillers(текст) == текст


# ---------------------------------------------------------------------------
# 06-pipeline №1: выражения словаря замен
# ---------------------------------------------------------------------------

ОПАСНОЕ = {"re:(\\w+\\s?)+$": "x"}


def test_опасное_выражение_не_проходит_проверку():
    from asrhub import catalog

    ok, причина = catalog.validate_value("glossary", ОПАСНОЕ)
    assert not ok and "повтор" in причина, причина


@pytest.mark.parametrize("словарь", [{"а": 5}, ["сбер"], {"re:(a": "x"}, {"re:(\\w+)": "\\2"}])
def test_негодный_словарь_замен_отвергается(словарь):
    from asrhub import catalog

    assert not catalog.validate_value("glossary", словарь)[0]


def test_задание_не_задаёт_своих_выражений(data_dir):
    """Выражение — программа, а словарь задания задаёт любой ключ с правом записи."""
    from asrhub.config import load
    from asrhub.errors import ConfigError

    настройки = load()
    with pytest.raises(ConfigError, match="только в настройках сервера"):
        настройки.merged({"glossary": {"re:\\d+": "#"}})
    # простые замены — пожалуйста
    assert настройки.merged({"glossary": {"сбер": "Сбер"}})["glossary"] == {"сбер": "Сбер"}
    # выражения сервера в снимке задания — тоже
    настройки.set("glossary", {"re:\\d+": "#"})
    assert настройки.merged({"glossary": {"re:\\d+": "#", "мир": "Мир"}})["glossary"]


def test_выражение_с_откатом_не_замораживает_сервер():
    """Выражения выполняются отдельным процессом с пределом времени.

    Это выражение на тридцати словах считалось бы часами и держало бы GIL —
    предел срабатывает, простые замены применяются, задание узнаёт об этом.
    """
    from asrhub.pipeline import postprocess

    тексты, замен, беда = postprocess.применить_словарь(
        ["да " * 30 + "!"], {**ОПАСНОЕ, "да": "нет"}, предел_с=1.0)
    assert "пропущены" in беда, беда
    assert тексты[0].startswith("нет нет") and замен == 30


def test_выражения_сервера_применяются():
    from asrhub.pipeline import postprocess

    тексты, замен, беда = postprocess.применить_словарь(
        ["версия 3 точка 2", "без чисел"], {"re:версия (\\d+) точка (\\d+)": "версия \\1.\\2"})
    assert (тексты, замен, беда) == (["версия 3.2", "без чисел"], 1, "")


# ---------------------------------------------------------------------------
# 06-pipeline №10, №11: пунктуация — отказ виден, отказ не вечен, окно и замок
# ---------------------------------------------------------------------------

@pytest.fixture()
def чистый_кеш_пунктуации():
    from asrhub.pipeline import postprocess

    postprocess.unload_text_models()
    getattr(postprocess, "_ОТКАЗЫ", {}).clear()
    yield postprocess
    postprocess.unload_text_models()
    getattr(postprocess, "_ОТКАЗЫ", {}).clear()


def _без_transformers(monkeypatch):
    import builtins

    настоящий = builtins.__import__

    def импорт(имя, *a, **k):
        if имя.split(".")[0] in ("transformers", "onnxruntime", "sbert_punc_case_ru",
                                 "deepmultilingualpunctuation"):
            raise ModuleNotFoundError(имя)
        return настоящий(имя, *a, **k)

    monkeypatch.setattr(builtins, "__import__", импорт)


def _поддельный_transformers(monkeypatch, труба_для):
    import sys
    import types

    модуль = types.ModuleType("transformers")
    модуль.pipeline = lambda *a, **k: труба_для()
    monkeypatch.setitem(sys.modules, "transformers", модуль)


def test_запасная_пунктуация_видна_в_задании(monkeypatch, чистый_кеш_пунктуации):
    pp = чистый_кеш_пунктуации
    _без_transformers(monkeypatch)
    _, сводка = pp.process([{"start": 0, "end": 1, "text": "мы пошли в"}],
                           {"language": "ru", "punctuation_model": "rupunct",
                            "itn_enabled": False}, engine="gigaam")
    assert сводка["punctuation_applied"] is False
    assert any("Пунктуация не расставлена" in з for з in сводка.get("warnings", [])), сводка


def _труба_точка():
    """Труба «ner», которая ставит точку после каждого слова."""
    def труба(text):
        return [{"word": с, "entity_group": "LOWER_PERIOD"} for с in text.split()]
    return труба


def test_отказ_модели_пунктуации_не_вечен(monkeypatch, чистый_кеш_пунктуации):
    """Нет сети в момент первого задания — это не «без пунктуации до перезапуска»."""
    pp = чистый_кеш_пунктуации
    monkeypatch.setattr(pp, "ПАМЯТЬ_ОТКАЗА_С", 0.0, raising=False)
    with monkeypatch.context() as m:
        _без_transformers(m)
        assert pp.restore_punctuation("мы пошли", "rupunct") == "Мы пошли."
    _поддельный_transformers(monkeypatch, _труба_точка)
    assert pp.restore_punctuation("мы пошли", "rupunct") == "Мы. пошли."


def test_длинная_реплика_не_теряет_хвост(monkeypatch, чистый_кеш_пунктуации):
    """Труба режет вход по 512 токенам; хвост реплики не должен пропадать."""
    pp = чистый_кеш_пунктуации

    def труба_с_пределом():
        def труба(text):
            return [{"word": с, "entity_group": "LOWER_O"} for с in text.split()[:200]]
        return труба

    _поддельный_transformers(monkeypatch, труба_с_пределом)
    слова = [f"слово{i}" for i in range(450)]
    итог = pp.restore_punctuation(" ".join(слова), "rupunct")
    assert len(итог.split()) == 450 and итог.split()[-1] == "слово449"


def test_одна_труба_не_вызывается_из_двух_заданий_разом(monkeypatch, чистый_кеш_пунктуации):
    import threading
    import time

    pp = чистый_кеш_пунктуации
    внутри = []
    сбои = []

    def труба_одиночка():
        def труба(text):
            if внутри:
                сбои.append("Already borrowed")
                raise RuntimeError("Already borrowed")
            внутри.append(1)
            time.sleep(0.2)
            внутри.pop()
            return [{"word": с, "entity_group": "LOWER_PERIOD"} for с in text.split()]
        return труба

    _поддельный_transformers(monkeypatch, труба_одиночка)
    pp.restore_punctuation("разогрев", "rupunct")
    итоги: list[str] = []
    потоки = [threading.Thread(target=lambda: итоги.append(pp.restore_punctuation(
        "раз два", "rupunct"))) for _ in range(2)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join()
    assert not сбои and итоги == ["Раз. два.", "Раз. два."], (сбои, итоги)


# ---------------------------------------------------------------------------
# 06-pipeline №8: слово без границ у выравнивателя и пословная диаризация
# ---------------------------------------------------------------------------

def _выровнено(*слова):
    return [{"word": w, "start": a, "end": b} for w, a, b in слова]


def test_слово_без_границ_не_сдвигает_остальные():
    """WhisperX не даёт границ «2024» и «5%» — соседи сохраняют свои метки."""
    from asrhub.engines.base import Segment
    from asrhub.pipeline import alignment

    реплика = Segment(0.0, 5.0, "В 2024 году выручка выросла на 5% и это хорошо")
    слова = _выровнено(("в", 0.10, 0.20), ("году", 0.90, 1.20), ("выручка", 1.30, 1.80),
                       ("выросла", 1.90, 2.40), ("на", 2.50, 2.60), ("и", 3.40, 3.50),
                       ("это", 3.60, 3.80), ("хорошо", 3.90, 4.40))
    итог = alignment._redistribute([реплика], слова, {"alignment_keep_text": True})[0]
    по_слову = {w["word"]: (w["start"], w["end"]) for w in итог.words}
    assert по_слову["году"] == (0.90, 1.20)
    assert по_слову["хорошо"] == (3.90, 4.40)
    assert [w["word"] for w in итог.words] == итог.text.split()


def test_пословная_диаризация_не_теряет_текст_реплики():
    """Часть реплики собирается из её текста, а не склейкой слов с метками."""
    from asrhub.engines.base import Segment
    from asrhub.pipeline import diarization

    реплика = Segment(0.0, 5.0, "В 2024 году выручка выросла на 5% и это хорошо", words=[
        {"word": "в", "start": 0.1, "end": 0.2, "speaker": "A"},
        {"word": "году", "start": 0.9, "end": 1.2, "speaker": "A"},
        {"word": "выручка", "start": 1.3, "end": 1.8, "speaker": "A"},
        {"word": "выросла", "start": 1.9, "end": 2.4, "speaker": "A"},
        {"word": "на", "start": 2.5, "end": 2.6, "speaker": "B"},
        {"word": "и", "start": 3.4, "end": 3.5, "speaker": "B"},
        {"word": "это", "start": 3.6, "end": 3.8, "speaker": "B"},
        {"word": "хорошо", "start": 3.9, "end": 4.4, "speaker": "B"}])
    части = diarization.разделить_по_словам([реплика])
    assert [ч.text for ч in части] == ["В 2024 году выручка выросла", "на 5% и это хорошо"]


# ---------------------------------------------------------------------------
# Сквозные проверки конвейера — с подставным движком
# ---------------------------------------------------------------------------

class _Движок:
    """Движок, который отдаёт заранее заданный текст и запоминает, что ему подали."""

    режет_сам = False

    def __init__(self, ответ=None, *, режет_сам=False):
        self.ответ = ответ or (lambda путь, длина: [(0.0, длина, "добрый день")])
        self.режет_сам = режет_сам
        self.поданные: list[float] = []

    def transcribe(self, путь, settings, progress=None):
        from asrhub.engines.base import Segment, TranscriptionResult

        with wave.open(str(путь), "rb") as файл:
            длина = файл.getnframes() / файл.getframerate()
        self.поданные.append(round(длина, 2))
        реплики = [Segment(a, b, т) for a, b, т in self.ответ(путь, длина)]
        return TranscriptionResult(segments=реплики, language="ru", duration=длина,
                                   meta={"engine": "подставной"})


class _Реестр:
    def __init__(self, движок):
        self.движок = движок

    def lease(self, settings, **_):
        import contextlib

        @contextlib.contextmanager
        def аренда():
            yield self.движок
        return аренда()

    def resolve(self, settings):
        from asrhub.catalog import get_model
        return get_model("gigaam-v3-rnnt"), "gigaam"


def _прогон(tmp_path, исходник, движок, *, reference_text: str = "", **настройки):
    from asrhub import processor
    from asrhub.config import load

    s = load(apply_hardware=False).merged({"vad_backend": "energy", "model": "gigaam-v3-rnnt",
                                           "waveform_enabled": False, **настройки})
    # Эталон очередь кладёт в настройки после слияния — так же и здесь.
    s["reference_text"] = reference_text
    return processor.process_job(Path(исходник), s, _Реестр(движок),
                                 workdir=tmp_path / "работа", outdir=tmp_path / "итог",
                                 basename="итог")


def _речь_с_паузами(путь: Path, rate: int = 16000) -> Path:
    """2 с тишины, 3 с «речи», 6 с тишины, 3 с «речи», 2 с тишины."""
    шум = [0.0005 * math.sin(i) for i in range(2 * rate)]
    речь = [0.3 * math.sin(2 * math.pi * 220 * i / rate) * (0.6 + 0.4 * math.sin(i / 400))
            for i in range(3 * rate)]
    return _wav(путь, [шум + речь + [0.0005] * (6 * rate) + речь + шум], rate)


@НУЖЕН_FFMPEG
def test_точность_по_тексту_движка_а_не_постобработки(tmp_path, data_dir):
    """Безошибочно распознанная фраза с числами — WER ноль, а не 0,6."""
    сказано = "в две тысячи двадцать четвёртом году выручка выросла на двадцать пять процентов"
    движок = _Движок(lambda путь, длина: [(0.0, длина, сказано)], режет_сам=True)
    итог = _прогон(tmp_path, _речь_с_паузами(tmp_path / "запись.wav"), движок,
                   reference_text=сказано, punctuation_enabled=False)
    assert итог.stats["accuracy"]["wer"] == 0.0, итог.stats["accuracy"]


@НУЖЕН_FFMPEG
def test_движку_без_своей_нарезки_подаются_только_куски_речи(tmp_path, data_dir):
    """openai-whisper, Vosk, T-one и прочие получали запись целиком, с тишиной."""
    движок = _Движок(lambda путь, длина: [(0.0, длина, "слово")])
    итог = _прогон(tmp_path, _речь_с_паузами(tmp_path / "запись.wav"), движок)
    assert len(движок.поданные) == 2, движок.поданные
    assert all(длина < 5.0 for длина in движок.поданные), движок.поданные
    # таймкоды — в координатах записи
    начала = sorted(с["start"] for с in итог.segments)
    assert начала[0] > 1.0 and начала[-1] > 7.0, итог.segments


@НУЖЕН_FFMPEG
def test_движок_со_своей_нарезкой_получает_запись_целиком(tmp_path, data_dir):
    движок = _Движок(режет_сам=True)
    _прогон(tmp_path, _речь_с_паузами(tmp_path / "запись.wav"), движок)
    assert len(движок.поданные) == 1 and движок.поданные[0] > 11.0, движок.поданные


@НУЖЕН_FFMPEG
def test_доля_речи_по_всем_каналам(tmp_path, data_dir):
    """Молчащий второй канал звонка не делает «речи 0 %»."""
    rate = 16000
    речь = [0.3 * math.sin(2 * math.pi * 220 * i / rate) for i in range(4 * rate)]
    исходник = _wav(tmp_path / "звонок.wav", [[0.0] * rate + речь + [0.0] * rate,
                                              [0.0] * (6 * rate)])
    итог = _прогон(tmp_path, исходник, _Движок(режет_сам=True), audio_channels="split",
                   audio_trim_silence=False)
    assert итог.stats["speech_ratio"] > 0.4, итог.stats


@НУЖЕН_FFMPEG
def test_подмена_детектора_речи_видна_в_задании(tmp_path, data_dir):
    """Silero нет — энергетический порог; задание об этом говорит."""
    итог = _прогон(tmp_path, _речь_с_паузами(tmp_path / "запись.wav"),
                   _Движок(режет_сам=True), vad_backend="silero")
    assert any("детектор «silero» недоступен" in з for з in итог.warnings), итог.warnings


@НУЖЕН_FFMPEG
def test_несостоявшийся_формат_выгрузки_виден_в_задании(tmp_path, data_dir, monkeypatch):
    from asrhub.pipeline import export

    def сбой(*a, **k):
        raise OSError("File name too long")

    monkeypatch.setattr(export, "to_srt", сбой)
    итог = _прогон(tmp_path, _речь_с_паузами(tmp_path / "запись.wav"),
                   _Движок(режет_сам=True), output_formats=["txt", "srt"])
    assert any("srt не записан" in з for з in итог.warnings), итог.warnings
    assert "txt" in итог.files


def test_длинное_имя_файла_результата_укорачивается():
    from asrhub.pipeline import export

    имя = export.safe_basename("Совещание " * 20)
    assert len(имя.encode("utf-8")) <= 200 and имя.startswith("Совещание")


# ---------------------------------------------------------------------------
# 06-pipeline №13: тихий собеседник и запасной детектор
# ---------------------------------------------------------------------------

def test_энергетический_детектор_слышит_тихого_собеседника(tmp_path):
    """Дальний собеседник на 15 дБ тише оператора — его речь не теряется."""
    np = pytest.importorskip("numpy")
    from asrhub.pipeline import audio, vad

    rate = 16000
    генератор = np.random.default_rng(0)

    def речь(секунд, громкость):
        t = np.arange(int(секунд * rate)) / rate
        огибающая = 0.5 * (1 + np.sin(2 * np.pi * 4 * t))
        return громкость * огибающая * (0.7 * np.sin(2 * np.pi * 200 * t)
                                        + 0.3 * генератор.standard_normal(len(t)))

    def тишина(секунд):
        return 0.001 * генератор.standard_normal(int(секунд * rate))

    тихо = 0.3 * 10 ** (-15 / 20)
    звук = np.concatenate([тишина(2), речь(8, 0.3), тишина(2), речь(8, тихо), тишина(2),
                           речь(8, 0.3), тишина(2), речь(8, тихо), тишина(2)])
    путь = audio.write_wav(tmp_path / "звонок.wav", звук, rate)
    участки = vad._detect_energy(путь, {"vad_threshold": 0.5})
    assert len(участки) == 4, [(round(у.start, 1), round(у.end, 1)) for у in участки]


def test_silero_получает_16_килогерц(monkeypatch, tmp_path):
    """22,05 и 44,1 кГц Silero не принимает — запись передискретизируется."""
    pytest.importorskip("numpy")
    import sys
    import types

    from asrhub.pipeline import vad

    увидено = []

    def поиск(тензор, модель, sampling_rate, **_):
        увидено.append(sampling_rate)
        return [{"start": 0.1, "end": 0.5}]

    torch = types.ModuleType("torch")
    torch.float32 = "float32"
    torch.as_tensor = lambda данные, dtype=None: данные
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setattr(vad, "_silero_model", lambda: (object(), {"get_speech_timestamps": поиск}))
    путь = _wav(tmp_path / "запись.wav", [_тон(1, 300, 22050)], rate=22050)
    assert vad._detect_silero(путь, {}) is not None
    assert увидено == [16000]


# ---------------------------------------------------------------------------
# 06-pipeline №14: WER длинных записей — точно
# ---------------------------------------------------------------------------

def test_wer_длинной_записи_точный():
    pytest.importorskip("numpy")
    import random

    from asrhub.pipeline import metrics

    random.seed(2)
    словарь = [f"с{i}" for i in range(400)]
    веса = [1 / (i + 1) for i in range(400)]
    эталон = random.choices(словарь, веса, k=2200)
    гипотеза = []
    for слово in эталон:
        r = random.random()
        if r < 0.12:
            continue
        if r < 0.24:
            гипотеза.append(random.choices(словарь, веса)[0])
            continue
        гипотеза.append(слово)
        if random.random() < 0.12:
            гипотеза.append(random.choices(словарь, веса)[0])
    точно = metrics._levenshtein(эталон, гипотеза).error_rate
    assert metrics._levenshtein_chunked(эталон, гипотеза).error_rate == pytest.approx(точно)


def test_счётчики_ошибок_сходятся_с_точным_расчётом():
    pytest.importorskip("numpy")
    import random

    from asrhub.pipeline import metrics

    for номер in range(200):
        random.seed(номер)
        слова = [str(i) for i in range(random.randint(1, 5))]
        a = random.choices(слова, k=random.randint(0, 25))
        b = random.choices(слова, k=random.randint(0, 25))
        точно = metrics._levenshtein(a, b)
        быстро = metrics._levenshtein_numpy(a, b)
        assert быстро.error_rate == pytest.approx(точно.error_rate), (a, b)
        assert быстро.hits + быстро.substitutions + быстро.deletions == len(a)
        assert быстро.hits + быстро.substitutions + быстро.insertions == len(b)


# ---------------------------------------------------------------------------
# 06-pipeline №15: полоса громкости и поиск речи не читают запись целиком
# ---------------------------------------------------------------------------

def _десять_минут(путь: Path) -> Path:
    np = pytest.importorskip("numpy")
    генератор = np.random.default_rng(0)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        for _ in range(10):
            файл.writeframes((генератор.standard_normal(16000 * 60) * 3000)
                             .astype("<i2").tobytes())
    return путь


def _пик_памяти(действие) -> float:
    import tracemalloc

    tracemalloc.start()
    try:
        действие()
        return tracemalloc.get_traced_memory()[1] / 1024 / 1024
    finally:
        tracemalloc.stop()


def test_полоса_громкости_по_говорящим_без_записи_в_памяти(tmp_path):
    """Десять минут — 19 МБ отсчётов; прежде в памяти были они и копия на говорящего."""
    from asrhub.pipeline import waveform

    путь = _десять_минут(tmp_path / "запись.wav")
    реплики = [{"start": i * 10.0, "end": i * 10.0 + 9, "speaker": f"Г{i % 2}", "text": "а"}
               for i in range(60)]
    пик = _пик_памяти(lambda: waveform.build([("", путь)], реплики, {"waveform_interval_s": 1.0}))
    assert пик < 10, f"пик {пик:.0f} МБ"


def test_энергетический_поиск_речи_без_записи_в_памяти(tmp_path):
    from asrhub.pipeline import vad

    путь = _десять_минут(tmp_path / "запись.wav")
    пик = _пик_памяти(lambda: vad._detect_energy(путь, {}))
    assert пик < 15, f"пик {пик:.0f} МБ"


# ---------------------------------------------------------------------------
# 06-pipeline №16: CSV для русского Excel
# ---------------------------------------------------------------------------

def test_csv_открывается_в_русском_excel_и_без_формул():
    from asrhub.pipeline import export

    итог = {"segments": [{"start": 1.0, "end": 2.5, "speaker": "=cmd|' /C calc'!A0",
                          "text": "+7 916 123-45-67 мой номер", "confidence": 0.9}]}
    текст = export.to_table(итог, {"include_confidence": True}, ",")
    assert текст.startswith("﻿")
    строки = текст.lstrip("﻿").splitlines()
    assert строки[0].count(";") >= 5
    клетки = строки[1].split(";")
    assert клетки[1] == "1,000" and клетки[2] == "2,500"
    assert клетки[4].startswith("'=") and клетки[5].startswith("'+7")
    # TSV — машинный, без поправок
    assert export.to_table(итог, {}, "\t").splitlines()[1].split("\t")[1] == "1.000"


# ---------------------------------------------------------------------------
# 06-pipeline №20: механическая нарезка и сшивка
# ---------------------------------------------------------------------------

def test_механическая_нарезка_без_лишнего_хвоста():
    from asrhub.pipeline import vad

    план = vad.chunk_plan(21.5, {"vad_max_speech_s": 22.0, "chunk_overlap_s": 1.0})
    assert [(к.start, к.end) for к in план] == [(0.0, 21.5)]
    план = vad.chunk_plan(43.0, {"vad_max_speech_s": 22.0, "chunk_overlap_s": 1.0})
    assert [(к.start, к.end) for к in план] == [(0.0, 22.0), (21.0, 43.0)]


def test_механическая_нарезка_не_длиннее_предела_модели():
    """VAD выключен, chunk_length_s 30 — у GigaAM кусок всё равно не длиннее 22 с."""
    from asrhub.pipeline import vad

    план = vad.chunk_plan(60.0, {"chunk_length_s": 30.0, "chunk_overlap_s": 1.0},
                          предел_модели=22.0)
    assert max(к.end - к.start for к in план) <= 22.0


def test_сшивка_видит_все_реплики_правого_фрагмента():
    from asrhub.engines.base import Segment
    from asrhub.pipeline import stitch

    a = Segment(15.0, 22.0, "договор подпишем завтра утром в офисе", words=_выровнено(
        ("договор", 15.0, 15.6), ("подпишем", 15.7, 16.4), ("завтра", 20.1, 20.6),
        ("утром", 20.7, 21.1), ("в", 21.2, 21.3), ("офисе", 21.4, 21.9)))
    b1 = Segment(20.0, 20.65, "завтра", words=_выровнено(("завтра", 20.1, 20.6)))
    b2 = Segment(20.7, 24.0, "утром в офисе после обеда", words=_выровнено(
        ("утром", 20.7, 21.1), ("в", 21.2, 21.3), ("офисе", 21.4, 21.9),
        ("после", 22.5, 22.9), ("обеда", 23.0, 23.6)))
    итог = " ".join(с.text for с in stitch.убрать_повторы([a, b1, b2], 2.0)).split()
    assert итог == ["договор", "подпишем", "завтра", "утром", "в", "офисе", "после", "обеда"], итог


# ---------------------------------------------------------------------------
# Недоделки 06: «плохой звук», профиль звука, вспомогательные модели, WAV без ffmpeg
# ---------------------------------------------------------------------------

def test_snr_по_перцентилям_не_делает_звук_плохим():
    from asrhub.pipeline import audio_profile

    assert not audio_profile.is_bad({"snr_db": 0.0, "clipping_share": 0.0,
                                     "method": "percentile"})
    assert audio_profile.is_bad({"snr_db": 3.0, "clipping_share": 0.0, "method": "vad"})


def test_профиль_звука_с_разметкой_не_зависит_от_порядка_и_вложенности():
    """Страховка к проходу двумя указателями: участки вразнобой и вложенные — тот же итог."""
    from asrhub.pipeline import audio_profile

    отсчёты = [0.2 * math.sin(i / 3) if (i // 8000) % 2 else 0.001 * math.sin(i)
               for i in range(16000 * 6)]
    ровно = [(0.5, 1.0), (1.5, 2.0), (2.5, 3.0), (3.5, 4.0), (4.5, 5.0), (5.5, 6.0)]
    вразнобой = [(5.5, 6.0), (0.5, 1.0), (0.6, 0.9), (2.5, 3.0), (1.5, 2.0), (3.5, 4.0),
                 (4.5, 5.0), (4.6, 4.7)]
    первый = audio_profile.profile(отсчёты, 16000, speech_spans=ровно)
    второй = audio_profile.profile(отсчёты, 16000, speech_spans=вразнобой)
    assert первый["method"] == "vad" and первый["snr_db"] > 20, первый
    assert первый["snr_db"] == второй["snr_db"]


def test_загрузка_одной_вспомогательной_модели_не_держит_другие():
    import threading

    from asrhub.pipeline import model_cache

    пошла = threading.Event()
    отпустить = threading.Event()

    def медленно():
        пошла.set()
        отпустить.wait(30)
        return "медленная"

    поток = threading.Thread(target=lambda: model_cache.взять(("тест-медленная",), медленно))
    поток.start()
    взято: list[str] = []
    try:
        assert пошла.wait(10)
        # Пока первая грузится, вторая берётся и выгрузка по простою работает.
        сосед = threading.Thread(target=lambda: (
            взято.append(model_cache.взять(("тест-быстрая",), lambda: "быстрая").модель),
            model_cache.выгрузить(0.0)))
        сосед.start()
        сосед.join(10)
        assert not сосед.is_alive() and взято == ["быстрая"], "сосед ждал чужую загрузку"
    finally:
        отпустить.set()
        поток.join(30)
    model_cache.выгрузить(None)


def test_wav_готовится_и_без_ffmpeg(tmp_path, monkeypatch):
    """Шапка модуля обещала: без ffmpeg несжатый WAV читается. prepare падал BinaryMissing."""
    from asrhub.pipeline import audio

    monkeypatch.setattr(audio.shutil, "which", lambda name: None)
    исходник = _wav(tmp_path / "звонок.wav", [_тон(2, 300, 8000), _тон(2, 900, 8000)], rate=8000)
    готово = audio.prepare(исходник, tmp_path / "работа",
                           {"audio_channels": "right", "audio_trim_silence": True,
                            "audio_sample_rate": 16000})
    путь = готово.channels[0][1]
    with wave.open(str(путь), "rb") as файл:
        assert файл.getframerate() == 16000 and файл.getnchannels() == 1
    assert any("ffmpeg не найден" in з for з in готово.warnings), готово.warnings


# ---------------------------------------------------------------------------
# Диктовка: хвост декодера
# ---------------------------------------------------------------------------

def test_хвост_декодера_доходит_до_движка(tmp_path):
    """ffmpeg отдаёт звук с задержкой — последние секунды в хвосте при закрытии."""
    from asrhub import streaming

    class Поток:
        def __init__(self):
            self.байт = 0

        def accept(self, pcm):
            self.байт += len(pcm)
            return None

        def finish(self):
            return "конец фразы"

    class Декодер:
        def feed(self, chunk):
            return chunk

        def close(self):
            return b"\0\1" * 16000             # секунда звука в хвосте

    сессия = streaming.StreamSession(None, {}, workdir=tmp_path)
    сессия.decoder = Декодер()
    сессия._native = поток = Поток()
    сессия.feed(b"\0\1" * 8000)
    события = сессия.finish()
    assert поток.байт == 2 * 8000 + 2 * 16000, поток.байт
    assert any(с.type == "final" for с in события)
