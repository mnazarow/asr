"""Заход 54: движки распознавания — то, что осталось от ревизии 3.1.20.

Настоящих библиотек движков (faster-whisper, NeMo, WhisperX, transformers,
qwen-asr, vosk, whisper.cpp) в среде проверок нет. Подделки ниже повторяют их
API ровно в той части, которую зовёт адаптер, — по документации и исходникам
библиотек; проверяется поведение адаптера при этих ответах. Сами адаптеры
против живых библиотек не запускались.
"""
from __future__ import annotations

import math
import struct
import sys
import types
import wave
from pathlib import Path
from typing import Any

import pytest


def _wav(путь: Path, секунд: float = 2.0, rate: int = 16000) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    кадры = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / rate)))
                     for i in range(int(секунд * rate)))
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(rate)
        файл.writeframes(кадры)
    return путь


def _модуль(monkeypatch, имя: str, **поля) -> types.ModuleType:
    модуль = types.ModuleType(имя)
    for ключ, значение in поля.items():
        setattr(модуль, ключ, значение)
    monkeypatch.setitem(sys.modules, имя, модуль)
    return модуль


def _без_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", None)


def _спец(модель: str):
    from asrhub.catalog import get_model

    return get_model(модель)


# ---------------------------------------------------------------------------
# 07-engines №1: имена моделей из задания не доходят до загрузчика весов
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ключ", ["diarization_model", "align_model",
                                  "alignment_acoustic_model", "alignment_dictionary"])
def test_имя_модели_из_задания_не_принимается(data_dir, ключ):
    from asrhub.config import load

    настройки = load()
    итог = настройки.merged({ключ: "attacker/evil-repo"})
    assert итог[ключ] == настройки.get(ключ), итог[ключ]


def test_pyannote_получает_модель_сервера(data_dir, monkeypatch, tmp_path):
    from asrhub.config import load
    from asrhub.engines.base import Segment
    from asrhub.pipeline import diarization, model_cache

    model_cache.выгрузить(None)
    просили: list[str] = []

    class Конвейер:
        @classmethod
        def from_pretrained(cls, имя, token=None):
            просили.append(имя)
            return None                       # «нет доступа» — дальше не нужно

    _модуль(monkeypatch, "pyannote", __path__=[])
    _модуль(monkeypatch, "pyannote.audio", Pipeline=Конвейер)
    настройки = load().merged({"diarization_enabled": True, "diarization_backend": "pyannote",
                               "diarization_model": "attacker/evil-pipeline",
                               "hf_token": "hf_x"})
    настройки["hf_token"] = "hf_" + "x" * 20
    предупреждения: list[str] = []
    from asrhub.errors import GatedModelError

    with pytest.raises(GatedModelError):              # подделка отвечает «нет доступа»
        diarization.diarize_segments(_wav(tmp_path / "a.wav"), [Segment(0, 1, "да")],
                                     настройки, предупреждения)
    model_cache.выгрузить(None)
    assert просили == ["pyannote/speaker-diarization-community-1"], просили


def test_переменная_nemo_не_остаётся_на_весь_процесс(monkeypatch):
    """TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD — на время загрузки, а не навсегда."""
    from asrhub.engines.nemo_engine import NeMoEngine

    monkeypatch.delenv("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD", raising=False)
    при_загрузке: list[str | None] = []

    class Модель:
        def cpu(self):
            return self

        def eval(self):
            return self

    class ASRModel:
        @staticmethod
        def from_pretrained(model_name):
            import os
            при_загрузке.append(os.environ.get("TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"))
            return Модель()

    nemo = _модуль(monkeypatch, "nemo", __path__=[])
    коллекции = _модуль(monkeypatch, "nemo.collections", __path__=[])
    asr = _модуль(monkeypatch, "nemo.collections.asr",
                  models=types.SimpleNamespace(ASRModel=ASRModel))
    nemo.collections = коллекции
    коллекции.asr = asr
    _без_torch(monkeypatch)
    NeMoEngine(_спец("parakeet-tdt-0.6b-v3"), {})._load({"device": "cpu"})
    import os
    assert при_загрузке == ["1"]
    assert "TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD" not in os.environ


# ---------------------------------------------------------------------------
# 07-engines №2 (08-params №9): пакетный конвейер faster-whisper
# ---------------------------------------------------------------------------

class _Модель:
    def __init__(self):
        self.вызовы: list[str] = []

    def transcribe(self, путь, **options):
        self.вызовы.append("модель")
        return iter([]), types.SimpleNamespace(duration=1.0, language="ru",
                                              language_probability=0.9)


@pytest.fixture()
def faster_whisper(monkeypatch):
    пакетом: list[dict[str, Any]] = []

    class Пакет:
        def __init__(self, model):
            self.model = model

        def transcribe(self, путь, **options):
            пакетом.append(options)
            return iter([]), types.SimpleNamespace(duration=1.0, language="ru",
                                                  language_probability=0.9)

    _модуль(monkeypatch, "faster_whisper", BatchedInferencePipeline=Пакет)
    return пакетом


def _прогон_fw(настройки: dict[str, Any], tmp_path) -> tuple[_Модель, Any]:
    from asrhub.engines.faster_whisper_engine import FasterWhisperEngine

    движок = FasterWhisperEngine(_спец("faster-whisper-large-v3"), настройки)
    движок._model = _Модель()
    итог = движок._transcribe(_wav(tmp_path / "a.wav"), настройки, None)
    return движок._model, итог


def test_по_умолчанию_каскад_температур_исполняется(faster_whisper, tmp_path):
    """Пакетный конвейер берёт одну температуру — с каскадом идём обычным путём."""
    модель, итог = _прогон_fw({"batch_size": 8}, tmp_path)
    assert модель.вызовы == ["модель"] and faster_whisper == []
    assert "каскад температур" in итог.meta.get("batched_skipped", [])


def test_без_поиска_речи_пакетом_не_идём(faster_whisper, tmp_path):
    """BatchedInferencePipeline без VAD на записи от 30 с бросает RuntimeError."""
    модель, _ = _прогон_fw({"batch_size": 8, "vad_enabled": False,
                            "temperature_fallback": False,
                            "hallucination_silence_threshold": 0.0}, tmp_path)
    assert модель.вызовы == ["модель"] and faster_whisper == []


def test_пакетом_когда_он_ничего_не_теряет(faster_whisper, tmp_path):
    """Страж: ограничения выше не отключают пакетный путь там, где он честен."""
    модель, _ = _прогон_fw({"batch_size": 8, "temperature_fallback": False,
                            "hallucination_silence_threshold": 0.0}, tmp_path)
    assert модель.вызовы == [] and len(faster_whisper) == 1
    assert faster_whisper[0]["batch_size"] == 8


# ---------------------------------------------------------------------------
# 07-engines №3: движок Transformers
# ---------------------------------------------------------------------------

def _transformers(monkeypatch, *, тип: str = "ctc", первый_отказ: Exception | None = None):
    вызовы: list[dict[str, Any]] = []
    кадры: list[dict[str, Any]] = []

    class Труба:
        type = тип
        model = types.SimpleNamespace(config=types.SimpleNamespace(model_type="wav2vec2"))

        def __call__(self, путь, **kwargs):
            кадры.append(kwargs)
            if kwargs.get("return_timestamps") not in (None, "word", "char") and тип == "ctc":
                raise ValueError("CTC can only predict word level timestamps")
            return {"text": "привет мир",
                    "chunks": [{"text": "привет", "timestamp": (0.1, 0.5)},
                               {"text": "мир", "timestamp": (0.6, 0.9)}]}

    def pipeline(задача, **kwargs):
        вызовы.append(kwargs)
        if первый_отказ is not None and len(вызовы) == 1:
            raise первый_отказ
        if "trust_remote_code" in kwargs.get("model_kwargs", {}) and "trust_remote_code" in kwargs:
            raise TypeError("got multiple values for keyword argument 'trust_remote_code'")
        return Труба()

    torch = _модуль(monkeypatch, "torch", float16="f16", bfloat16="bf16", float32="f32")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    _модуль(monkeypatch, "transformers", pipeline=pipeline)
    return вызовы, кадры


def test_ctc_модель_получает_пословные_таймкоды(monkeypatch, tmp_path):
    from asrhub.engines.hf_transformers_engine import TransformersEngine

    вызовы, кадры = _transformers(monkeypatch)
    движок = TransformersEngine(_спец("w2v2-xlsr53-ru"), {"device": "cpu"})
    итог = движок.transcribe(_wav(tmp_path / "a.wav"), {"device": "cpu"})
    assert кадры[0]["return_timestamps"] == "word"
    assert итог.segments[0].text == "привет мир"
    assert [w["word"] for w in итог.segments[0].words] == ["привет", "мир"]
    assert вызовы[0]["device"] == "cpu"


def test_доверенный_код_передаётся_аргументом_конвейера(monkeypatch):
    from asrhub.engines.hf_transformers_engine import TransformersEngine

    вызовы, _ = _transformers(monkeypatch, первый_отказ=ValueError("нужен trust_remote_code"))
    TransformersEngine(_спец("moss-transcribe-diarize"), {})._load({"device": "cpu"})
    assert вызовы[1].get("trust_remote_code") is True
    assert "trust_remote_code" not in (вызовы[1].get("model_kwargs") or {})


@pytest.mark.parametrize("метки, ждём", [
    (("SPK1", "SPK2"), ["Говорящий 1", "Говорящий 2"]),
    (("SPEAKER_00", "SPEAKER_01"), ["Говорящий 1", "Говорящий 2"]),
    (("S01", "S02"), ["Говорящий 1", "Говорящий 2"]),
])
def test_метки_говорящих_в_тексте_модели(метки, ждём):
    from asrhub.engines.hf_transformers_engine import _говорящий

    assert [_говорящий(м) for м in метки] == ждём


# ---------------------------------------------------------------------------
# 07-engines №4: Qwen3-ASR
# ---------------------------------------------------------------------------

def test_qwen3_по_api_пакета(monkeypatch, tmp_path):
    from asrhub.engines.qwen3_engine import Qwen3ASREngine

    создано: list[tuple[str, dict[str, Any]]] = []
    спрошено: list[Any] = []

    class Qwen3ASRModel:
        @classmethod
        def from_pretrained(cls, источник, **kwargs):
            создано.append((источник, kwargs))
            return cls()

        def transcribe(self, audio, language=None):
            спрошено.append(language)
            return [types.SimpleNamespace(text="Добрый день.", language="Russian")]

    _модуль(monkeypatch, "qwen_asr", Qwen3ASRModel=Qwen3ASRModel)
    _без_torch(monkeypatch)
    движок = Qwen3ASREngine(_спец("qwen3-asr-1.7b"), {})
    assert Qwen3ASREngine.check_available()[0]
    итог = движок.transcribe(_wav(tmp_path / "a.wav"), {"device": "cpu", "language": "ru"})
    assert создано[0][0] == "Qwen/Qwen3-ASR-1.7B" and спрошено == ["Russian"]
    assert итог.text == "Добрый день." and итог.language == "ru"
    итог = движок.transcribe(_wav(tmp_path / "a.wav"), {"device": "cpu", "language": "auto"})
    assert спрошено[-1] is None and итог.language == "ru"


def test_qwen3_без_пакета_не_установлен(monkeypatch):
    from asrhub.engines.qwen3_engine import Qwen3ASREngine

    monkeypatch.setitem(sys.modules, "qwen_asr", None)
    _модуль(monkeypatch, "transformers")
    assert Qwen3ASREngine.check_available()[0] is False


def test_подсказка_установки_называет_настоящий_файл():
    from asrhub.errors import DependencyMissing

    assert "requirements/engines/qwen3-asr.txt" in DependencyMissing("qwen3_asr").hint
    assert "requirements/engines/faster-whisper.txt" in DependencyMissing("faster_whisper").hint


# ---------------------------------------------------------------------------
# 07-engines №5: веса openai-whisper
# ---------------------------------------------------------------------------

def test_веса_openai_whisper_не_путаются_с_faster_whisper(tmp_path):
    from asrhub import model_files

    модели = tmp_path / "models"
    (модели / "models--Systran--faster-whisper-large-v3").mkdir(parents=True)
    whisper, faster = _спец("whisper-large-v3"), _спец("faster-whisper-large-v3")
    assert model_files.find_local(модели, whisper.source, whisper.revision or "") is None
    assert model_files.find_local(модели, faster.source, faster.revision or "") is not None
    (модели / "large-v3.pt").write_bytes(b"pt")
    найдено = model_files.find_local(модели, whisper.source, whisper.revision or "")
    assert найдено == модели / "large-v3.pt"


def test_скачать_whisper_кладёт_файл_pt(client, monkeypatch, tmp_path):
    """Загрузка — загрузчиком openai-whisper, а не снапшотом faster-whisper."""
    import time

    скачано: list[tuple[str, str]] = []

    def _download(адрес, корень, в_памяти):
        скачано.append((адрес, корень))
        путь = Path(корень) / адрес.rsplit("/", 1)[-1]
        путь.write_bytes(b"pt")
        return str(путь)

    _модуль(monkeypatch, "whisper", _download=_download,
            _MODELS={"medium": "https://example.invalid/abc/medium.pt"})
    ответ = client.post("/api/models/whisper-medium/download")
    assert ответ.status_code == 200, ответ.text
    for _ in range(100):
        if client.get("/api/models/whisper-medium/status").json().get("downloaded"):
            break
        time.sleep(0.1)
    assert скачано and скачано[0][0].endswith("medium.pt")
    assert client.get("/api/models/whisper-medium/status").json().get("downloaded") is True


# ---------------------------------------------------------------------------
# 07-engines №6 (05-queue №19): «Выгрузить модели» не трогает занятые
# ---------------------------------------------------------------------------

def test_выгрузка_моделей_не_роняет_идущее_задание(data_dir, tmp_path):
    from asrhub.config import load
    from asrhub.engines import EngineRegistry

    реестр = EngineRegistry()
    настройки = load().merged({"model": "demo-simulator", "engine": "demo",
                               "vad_backend": "energy"})
    with реестр.lease(настройки) as движок:
        движок.ensure_loaded(настройки)
        итог = реестр.unload_all()
        assert итог == {"freed": 0, "busy": 1}
        assert движок.is_loaded
        assert движок.transcribe(_wav(tmp_path / "a.wav"), настройки).segments is not None
    assert реестр.unload_all() == {"freed": 1, "busy": 0}


# ---------------------------------------------------------------------------
# 07-engines №7: вспомогательные и неисполняемые модели в задание не берутся
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("модель", ["gigaam-emo", "sortformer-diar-4spk-v2",
                                    "kyutai-stt-1b-en-fr", "canary-qwen-2.5b"])
def test_неисполняемая_модель_отвергается(data_dir, модель):
    from asrhub import catalog
    from asrhub.config import load
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError, match="не берётся"):
        load().merged({"model": модель})
    assert not catalog.validate_server_value("model", модель)[0]


def test_omnilingual_карточка_и_предел_прохода():
    from asrhub.engines.misc_engines import _карточка_omnilingual

    спец = _спец("omnilingual-ctc-1b")
    assert _карточка_omnilingual(спец.source) == "omniASR_CTC_1B"
    assert спец.max_audio_s == 40


# ---------------------------------------------------------------------------
# 07-engines №8: Vosk не подменяет версию модели
# ---------------------------------------------------------------------------

def test_vosk_054_не_распознаёт_моделью_042(tmp_path):
    from asrhub.engines.vosk_engine import VoskEngine
    from asrhub.errors import ModelNotDownloaded

    (tmp_path / "vosk" / "vosk-model-ru-0.42").mkdir(parents=True)
    движок = VoskEngine(_спец("vosk-ru-0.54"), {})
    with pytest.raises(ModelNotDownloaded):
        движок._model_dir({"models_dir": str(tmp_path)})
    assert VoskEngine(_спец("vosk-ru-0.42"), {})._model_dir(
        {"models_dir": str(tmp_path)}).name == "vosk-model-ru-0.42"


# ---------------------------------------------------------------------------
# 07-engines №9: номер карты доходит до движка
# ---------------------------------------------------------------------------

def test_nemo_садится_на_заданную_карту(monkeypatch):
    from asrhub.engines.nemo_engine import NeMoEngine

    куда: list[str] = []

    class Модель:
        def to(self, устройство):
            куда.append(устройство)
            return self

        def cuda(self):
            куда.append("cuda")
            return self

        def eval(self):
            return self

    class ASRModel:
        @staticmethod
        def from_pretrained(model_name):
            return Модель()

    nemo = _модуль(monkeypatch, "nemo", __path__=[])
    коллекции = _модуль(monkeypatch, "nemo.collections", __path__=[])
    asr = _модуль(monkeypatch, "nemo.collections.asr",
                  models=types.SimpleNamespace(ASRModel=ASRModel))
    nemo.collections = коллекции
    коллекции.asr = asr
    _без_torch(monkeypatch)
    NeMoEngine(_спец("parakeet-tdt-0.6b-v3"), {})._load({"device": "cuda:1"})
    assert куда == ["cuda:1"]


@pytest.fixture()
def whisper_cpp(monkeypatch, tmp_path):
    from asrhub.engines import whisper_cpp_engine as модуль

    команды: list[list[str]] = []

    настоящий = модуль.subprocess.run

    def запуск(cmd, **kwargs):
        if "-of" not in cmd:                          # ffprobe и прочие — как есть
            return настоящий(cmd, **kwargs)
        команды.append(list(cmd))
        выход = Path(cmd[cmd.index("-of") + 1]).with_suffix(".json")
        выход.write_text('{"result": {"language": "ru"}, "transcription": []}',
                         encoding="utf-8")
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(модуль.subprocess, "run", запуск)
    _без_torch(monkeypatch)
    return команды


def _прогон_cpp(device: str, tmp_path) -> list[str]:
    from asrhub.engines.whisper_cpp_engine import WhisperCppEngine

    движок = WhisperCppEngine(_спец("whispercpp-large-v3-turbo"), {})
    движок._model = {"binary": "whisper-cli", "model": "ggml.bin"}
    настройки = {"device": device, "vad_enabled": False, "temp_dir": str(tmp_path)}
    движок._transcribe(_wav(tmp_path / "a.wav"), настройки, None)
    return []


def test_whisper_cpp_при_auto_не_запрещает_карту(whisper_cpp, tmp_path):
    """Без torch «auto» уходило в -ng, хотя сборка whisper.cpp карту видит."""
    _прогон_cpp("auto", tmp_path)
    assert "-ng" not in whisper_cpp[-1]
    _прогон_cpp("cpu", tmp_path)
    assert "-ng" in whisper_cpp[-1]


def test_whisper_cpp_получает_номер_карты(whisper_cpp, tmp_path):
    _прогон_cpp("cuda:1", tmp_path)
    команда = whisper_cpp[-1]
    assert команда[команда.index("-dev") + 1] == "1"


def test_whisper_cpp_не_проверяет_карту_через_torch(monkeypatch):
    from asrhub.engines.whisper_cpp_engine import WhisperCppEngine

    _без_torch(monkeypatch)
    assert WhisperCppEngine(_спец("whispercpp-base"), {}).проверить_устройство("cuda") == (True, "")


# ---------------------------------------------------------------------------
# 07-engines №10: WhisperX
# ---------------------------------------------------------------------------

def test_whisperx_язык_не_прилипает_к_модели(monkeypatch):
    from asrhub.engines.whisperx_engine import WhisperXEngine

    загрузки: list[dict[str, Any]] = []
    _модуль(monkeypatch, "whisperx", load_model=lambda *a, **k: загрузки.append(k) or "м")
    _без_torch(monkeypatch)
    WhisperXEngine(_спец("whisperx-large-v3"), {})._load({"device": "cpu", "language": "en"})
    assert "language" not in загрузки[0]


def test_whisperx_слово_без_выравнивания_между_соседями():
    from asrhub.engines.whisperx_engine import _слова_реплики

    слова = _слова_реплики({"start": 0.0, "end": 5.0, "words": [
        {"word": "в", "start": 0.1, "end": 0.2, "score": 0.9},
        {"word": "2024"},
        {"word": "году", "start": 0.9, "end": 1.2, "score": 0.8}]}, пословно=False)
    assert (слова[1]["start"], слова[1]["end"], слова[1]["confidence"]) == (0.2, 0.9, None)


def test_whisperx_номер_карты(monkeypatch):
    from asrhub.engines.whisperx_engine import WhisperXEngine

    загрузки: list[tuple[Any, ...]] = []
    _модуль(monkeypatch, "whisperx",
            load_model=lambda *a, **k: загрузки.append((a, k)) or "м")
    _без_torch(monkeypatch)
    WhisperXEngine(_спец("whisperx-large-v3"), {})._load({"device": "cuda:1"})
    (имя, устройство), kwargs = загрузки[0]
    assert устройство == "cuda" and kwargs["device_index"] == 1


# ---------------------------------------------------------------------------
# 07-engines №11: без пословных таймкодов реплики не нулевой длины
# ---------------------------------------------------------------------------

def test_nemo_без_таймкодов_реплика_до_конца_фрагмента():
    from asrhub.engines.nemo_engine import _convert

    итог = _convert(types.SimpleNamespace(text="добрый день", timestamp=None), 40.0, "ru",
                    конец=62.0)
    assert (итог[0].start, итог[0].end) == (40.0, 62.0)


def test_nemo_реплика_без_конца_не_сдвигается_дважды():
    from asrhub.engines.nemo_engine import _convert

    вывод = types.SimpleNamespace(text="да", timestamp={"segment": [
        {"start": 1.0, "segment": "да"}], "word": []})
    итог = _convert(вывод, 40.0, "ru", конец=60.0)
    assert (итог[0].start, итог[0].end) == (41.0, 41.0)


def test_vosk_без_слов_всё_равно_знает_границы(monkeypatch, tmp_path):
    from asrhub.engines.vosk_engine import VoskEngine

    флаги: list[bool] = []

    class Распознаватель:
        def __init__(self, модель, rate):
            self.выдано = False

        def SetWords(self, флаг):
            флаги.append(флаг)

        def SetPartialWords(self, флаг):
            pass

        def AcceptWaveform(self, данные):
            return False

        def FinalResult(self):
            import json
            return json.dumps({"text": "добрый день", "result": [
                {"word": "добрый", "start": 0.3, "end": 0.7, "conf": 1.0},
                {"word": "день", "start": 0.8, "end": 1.2, "conf": 1.0}]})

    _модуль(monkeypatch, "vosk", KaldiRecognizer=Распознаватель)
    движок = VoskEngine(_спец("vosk-ru-0.42"), {})
    движок._model = object()
    итог = движок._transcribe(_wav(tmp_path / "a.wav"), {"word_timestamps": False}, None)
    assert флаги == [True]
    assert (итог.segments[0].start, итог.segments[0].end) == (0.3, 1.2)
    assert итог.segments[0].words == []


# ---------------------------------------------------------------------------
# 07-engines №12: длинная запись в NeMo — кусками; Canary без языка
# ---------------------------------------------------------------------------

def test_nemo_режет_запись_длиннее_безопасного_прохода(monkeypatch, tmp_path):
    from asrhub.engines import nemo_engine
    from asrhub.engines.nemo_engine import NeMoEngine

    поданные: list[int] = []

    class Модель:
        def transcribe(self, пути, **kwargs):
            поданные.append(len(пути))
            return [types.SimpleNamespace(text="да", timestamp=None) for _ in пути]

    monkeypatch.setattr(nemo_engine, "probe",
                        lambda путь: types.SimpleNamespace(duration_s=1200.0))
    monkeypatch.setattr(nemo_engine, "slice_wav", lambda *a, **k: None)
    движок = NeMoEngine(_спец("parakeet-tdt-0.6b-v3"), {})
    движок._model = Модель()
    движок._transcribe(tmp_path / "a.wav", {"vad_enabled": False, "temp_dir": str(tmp_path),
                                            "chunk_overlap_s": 1.0}, None)
    assert поданные and поданные[0] >= 4, поданные


def test_canary_без_языка_отказывает_понятно(tmp_path):
    from asrhub.engines.nemo_engine import NeMoEngine
    from asrhub.errors import EngineError

    движок = NeMoEngine(_спец("canary-1b-v2"), {})
    движок._model = object()
    with pytest.raises(EngineError, match="не определяет язык"):
        движок._transcribe(_wav(tmp_path / "a.wav"), {"language": "auto"}, None)


# ---------------------------------------------------------------------------
# 07-engines №13: Moonshine и общая нарезка
# ---------------------------------------------------------------------------

def test_moonshine_советует_выполнимое(monkeypatch, tmp_path):
    from asrhub.engines import misc_engines
    from asrhub.errors import EngineError

    monkeypatch.setattr(misc_engines, "probe", lambda путь: types.SimpleNamespace(duration_s=90.0))
    движок = misc_engines.MoonshineEngine(_спец("moonshine-base"), {})
    движок._model = object()
    with pytest.raises(EngineError) as отказ:
        движок._transcribe(tmp_path / "a.wav", {}, None)
    assert "vad_enabled" in (отказ.value.hint or "")


def test_движки_без_своей_нарезки_помечены():
    from asrhub.engines import ENGINE_CLASSES

    сами = {имя for имя, cls in ENGINE_CLASSES.items() if cls.режет_сам}
    assert {"gigaam", "faster_whisper", "whisper_cpp", "voxtral", "nemo", "whisperx",
            "demo"} <= сами
    assert not сами & {"whisper", "transformers", "vosk", "tone", "qwen3_asr", "funasr",
                       "moonshine", "omnilingual"}


# ---------------------------------------------------------------------------
# 07-engines №14: устройство движков без torch
# ---------------------------------------------------------------------------

def test_faster_whisper_видит_карту_через_ctranslate2(monkeypatch):
    from asrhub.engines.faster_whisper_engine import FasterWhisperEngine

    _без_torch(monkeypatch)
    _модуль(monkeypatch, "ctranslate2", get_cuda_device_count=lambda: 1)
    движок = FasterWhisperEngine(_спец("faster-whisper-small"), {})
    assert движок.resolve_device({"device": "auto"}) == "cuda"
    assert движок.проверить_устройство("cuda") == (True, "")
    годно, причина = движок.проверить_устройство("cuda:1")
    assert not годно and "1" in причина


# ---------------------------------------------------------------------------
# 07-engines №15: одна модель — один экземпляр в кеше
# ---------------------------------------------------------------------------

def test_auto_и_явное_устройство_одна_модель(data_dir, monkeypatch):
    from asrhub.engines import EngineRegistry

    _без_torch(monkeypatch)
    реестр = EngineRegistry(max_size=4)
    база = {"model": "demo-simulator", "engine": "demo"}
    первый = реестр.get({**база, "device": "auto", "compute_type": "auto"})
    второй = реестр.get({**база, "device": "cpu", "compute_type": "auto"})
    третий = реестр.get({**база, "device": "auto", "compute_type": "int8"})
    assert первый is второй is третий
    assert len(реестр.loaded()) == 1


# ---------------------------------------------------------------------------
# 07-engines №16, №19: GigaAM — ответ объектом, нет слов, предел куска
# ---------------------------------------------------------------------------

def _gigaam(ответ, tmp_path, **настройки):
    from asrhub.engines.gigaam_engine import GigaAMEngine

    class Модель:
        def transcribe(self, путь, **kwargs):
            if callable(ответ):
                return ответ(путь, **kwargs)
            return ответ

    движок = GigaAMEngine(_спец("gigaam-v3-rnnt"), {})
    движок._model = Модель()
    return движок._transcribe(_wav(tmp_path / "a.wav", 3.0),
                              {"vad_enabled": False, "temp_dir": str(tmp_path), **настройки},
                              None)


def test_gigaam_ответ_объектом_не_уходит_в_текст_repr(tmp_path):
    ответ = types.SimpleNamespace(text="добрый день", words=[])
    итог = _gigaam(ответ, tmp_path)
    assert итог.segments[0].text == "добрый день"


def test_gigaam_без_слов_говорит_об_этом(tmp_path):
    итог = _gigaam(lambda путь, **k: "добрый день", tmp_path)
    assert any("пословные таймкоды" in з for з in итог.meta.get("warnings", []))


def test_gigaam_отказ_по_длине_узнаётся(tmp_path):
    from asrhub.errors import EngineError

    def отказ(путь, **kwargs):
        raise ValueError("Too long wav file, use 'transcribe_longform' method.")

    with pytest.raises(EngineError, match="длиннее 25 секунд"):
        _gigaam(отказ, tmp_path)


def test_gigaam_при_нехватке_памяти_не_идёт_в_сеть(monkeypatch):
    from asrhub.engines.gigaam_engine import GigaAMEngine
    from asrhub.errors import ModelLoadError

    def load_model(*a, **k):
        raise RuntimeError("CUDA out of memory. Tried to allocate 2.00 GiB")

    через_transformers: list[Any] = []

    class AutoModel:
        @staticmethod
        def from_pretrained(*a, **k):
            через_transformers.append(a)
            raise RuntimeError("сеть")

    _модуль(monkeypatch, "gigaam", load_model=load_model)
    _модуль(monkeypatch, "transformers", AutoModel=AutoModel)
    with pytest.raises(ModelLoadError):
        GigaAMEngine(_спец("gigaam-v3-rnnt"), {})._load({"device": "cpu"})
    assert через_transformers == []


# ---------------------------------------------------------------------------
# 07-engines №17: версии драйвера NVIDIA
# ---------------------------------------------------------------------------

def test_расхождение_в_третьем_числе_версии():
    from asrhub.hardware import _версии_сходятся

    assert _версии_сходятся("535.183.01", "535.183.06") is False
    assert _версии_сходятся("595.91.07", "595.91") is True


def test_жалоба_nvidia_smi_это_расхождение(monkeypatch, tmp_path):
    from asrhub import hardware

    proc = tmp_path / "version"
    proc.write_text("NVRM version: NVIDIA UNIX x86_64 Kernel Module  535.183.01  Sun\n")
    monkeypatch.setattr(hardware, "NVIDIA_PROC", str(proc))
    monkeypatch.setattr(hardware, "_библиотеки", lambda: ("535.183", True))
    monkeypatch.setattr(hardware.shutil, "which", lambda имя: None)
    monkeypatch.setattr(hardware, "модули_по_ядрам", lambda корень: {})
    assert hardware.проверить_драйвер()["state"] != "ok"


# ---------------------------------------------------------------------------
# 07-engines №18, №20: самопроверка и подбор под оборудование
# ---------------------------------------------------------------------------

def test_самопроверка_при_auto_говорит_куда_пойдут_движки(monkeypatch):
    from asrhub import selfcheck

    _без_torch(monkeypatch)
    состояние = types.SimpleNamespace(settings={"device": "auto"})
    проверки: list[dict[str, Any]] = []
    selfcheck._устройство_проверка(состояние, types.SimpleNamespace(accelerator="cuda"),
                                   проверки)
    assert проверки[0]["state"] == "warn" and "cpu" in проверки[0]["value"]


def test_карта_на_8_гб_получает_уровень_8_гб():
    from asrhub import hardware

    карта = types.SimpleNamespace(memory_total_mb=8188, compute_capability="8.9",
                                  name="RTX 4060", vendor="nvidia")
    сведения = types.SimpleNamespace(accelerator="cuda", best_gpu=карта, gpus=[карта, карта],
                                     cpu_cores_available=8)
    итог = hardware.recommended_settings(сведения)
    assert (итог["model"], итог["batch_size"]) == ("gigaam-v3-rnnt", 8)
    assert итог["max_concurrent_jobs"] == 1


def test_только_демо_это_ни_одного_движка(monkeypatch):
    from asrhub import selfcheck

    список = [{"id": "demo", "available": True, "name": "Демо"},
              {"id": "gigaam", "available": False, "name": "GigaAM"}]
    monkeypatch.setattr("asrhub.engines.engine_status", lambda: список)
    состояние = types.SimpleNamespace(settings={"engine": "demo", "model": "demo-simulator"})
    раздел = selfcheck._движки(состояние, False) if hasattr(selfcheck, "_движки") else None
    if раздел is None:
        pytest.skip("раздел движков самопроверки называется иначе")
    пункты = {п["id"]: п for п in раздел["checks"]}
    assert пункты["installed"]["state"] == "fail"


def test_voxtral_без_mistral_common_не_установлен(monkeypatch):
    from asrhub.engines.misc_engines import VoxtralEngine

    _модуль(monkeypatch, "transformers")
    monkeypatch.setitem(sys.modules, "mistral_common", None)
    assert VoxtralEngine.check_available()[0] is False


# ---------------------------------------------------------------------------
# 08-params №20б: штраф длины у openai-whisper
# ---------------------------------------------------------------------------

def test_openai_whisper_получает_штраф_длины_в_допустимых_пределах(tmp_path):
    from asrhub.engines.whisper_engine import WhisperEngine

    передано: list[dict[str, Any]] = []

    class Модель:
        def transcribe(self, путь, **options):
            передано.append(options)
            return {"segments": [], "language": "ru"}

    движок = WhisperEngine(_спец("whisper-small"), {})
    движок._model = Модель()
    движок._transcribe(_wav(tmp_path / "a.wav"), {"device": "cpu", "length_penalty": 1.5}, None)
    assert передано[0]["length_penalty"] == 1.0


def _раздел_оборудования(client, monkeypatch, *, памяти_гб: float, замер_назад_с: float | None,
                         запущен_назад_с: float) -> dict[str, dict[str, Any]]:
    import dataclasses
    import time

    from asrhub import hardware, selfcheck

    настоящее = hardware.detect

    def detect(*a, **k):
        return dataclasses.replace(настоящее(*a, **k), ram_total_gb=памяти_гб)

    monkeypatch.setattr(hardware, "detect", detect)
    состояние = client.app.state.hub
    monkeypatch.setattr(состояние, "started_at", time.time() - запущен_назад_с)
    if замер_назад_с is not None:
        замер = {"ts": time.time() - замер_назад_с, "cpu_percent": 5, "ram_used_mb": 1,
                 "ram_total_mb": 2, "queue_depth": 0}
        monkeypatch.setattr(состояние.db, "system_samples", lambda *a, **k: [замер])
    раздел = selfcheck._оборудование(состояние, False)
    return {п["id"]: п for п in раздел["checks"]}


def test_память_16_гб_по_паспорту_не_жёлтая(client, monkeypatch):
    """MemTotal машины «на 16 ГБ» — 15,6 ГБ: это не повод для предупреждения."""
    пункты = _раздел_оборудования(client, monkeypatch, памяти_гб=15.6, замер_назад_с=None,
                                  запущен_назад_с=3600)
    assert пункты["ram"]["state"] == "ok"
    пункты = _раздел_оборудования(client, monkeypatch, памяти_гб=12.0, замер_назад_с=None,
                                  запущен_назад_с=3600)
    assert пункты["ram"]["state"] == "warn"


def test_старый_замер_сразу_после_запуска_не_тревога(client, monkeypatch):
    пункты = _раздел_оборудования(client, monkeypatch, памяти_гб=32.0, замер_назад_с=900,
                                  запущен_назад_с=10)
    assert пункты["sample"]["state"] == "ok"
    пункты = _раздел_оборудования(client, monkeypatch, памяти_гб=32.0, замер_назад_с=900,
                                  запущен_назад_с=3600)
    assert пункты["sample"]["state"] == "warn"
