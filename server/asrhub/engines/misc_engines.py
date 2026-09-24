"""Компактные адаптеры: FunASR, Moonshine, Omnilingual, Voxtral, Kyutai, sherpa-onnx.

Все следуют одному шаблону: ленивый импорт, единый формат сегментов,
преобразование исключений в типизированные ошибки ASR Hub.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..errors import DependencyMissing, EngineError, classify_exception
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult


def _single(text: str, duration: float, language: str) -> list[Segment]:
    text = str(text).strip()
    return [Segment(start=0.0, end=duration, text=text, language=language or None)] if text else []


class FunASREngine(Engine):
    """SenseVoice и Paraformer: транскрипт, язык, эмоция и аудиособытия за один проход."""

    id = "funasr"
    supports_batching = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import funasr  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен FunASR: pip install -U funasr modelscope"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from funasr import AutoModel  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("funasr", "funasr", cause=exc) from exc
        device = self.resolve_device(settings)
        return AutoModel(model=self.spec.source,
                         device="cuda:0" if device.startswith("cuda") else "cpu",
                         disable_update=True,
                         cache_dir=str(settings.get("models_dir") or "") or None)

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        language = self.language_for(settings) or "auto"
        self.report(progress, 0.15, "распознавание")
        try:
            out = self._model.generate(
                input=str(audio_path), language=language, use_itn=bool(settings.get("itn_enabled", True)),
                batch_size_s=int(settings.get("batch_size") or 8) * 30)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

        segments: list[Segment] = []
        emotions: list[str] = []
        for item in out or []:
            text = str(item.get("text", "")) if isinstance(item, dict) else str(item)
            # SenseVoice помечает язык, эмоцию и события тегами вида <|HAPPY|>
            tags = [t for t in text.split("<|") if "|>" in t]
            for tag in tags:
                name = tag.split("|>")[0]
                if name.isupper() and name not in ("ZH", "EN", "JA", "KO", "YUE", "NOSPEECH"):
                    emotions.append(name)
            clean = text
            for tag in tags:
                clean = clean.replace(f"<|{tag.split('|>')[0]}|>", "")
            segments.extend(_single(clean, info.duration_s, language))

        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(
            segments=segments, language=language, duration=info.duration_s,
            meta={"emotions": sorted(set(emotions))} if emotions else {})


class MoonshineEngine(Engine):
    """Сверхлёгкий движок для edge-устройств. Только английский."""

    id = "moonshine"

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import moonshine  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Не установлен Moonshine: pip install "
                           "useful-moonshine@git+https://github.com/usefulsensors/moonshine.git")

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import moonshine  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("moonshine", "useful-moonshine", cause=exc) from exc
        return moonshine

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        if info.duration_s > 64:
            raise EngineError(
                "Moonshine рассчитана на короткие фрагменты (до минуты).",
                hint="Включите VAD или выберите другую модель для длинных записей.")
        self.report(progress, 0.2, "распознавание")
        name = self.spec.source.rsplit("/", 1)[-1]
        out = self._model.transcribe(str(audio_path), f"moonshine/{name.replace('moonshine-', '')}")
        text = out[0] if isinstance(out, (list, tuple)) and out else str(out)
        return TranscriptionResult(segments=_single(text, info.duration_s, "en"),
                                   language="en", duration=info.duration_s)


class OmnilingualEngine(Engine):
    """Meta Omnilingual ASR: 1600+ языков, Apache-2.0 на код и веса."""

    id = "omnilingual"
    supports_batching = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import omnilingual_asr  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен Omnilingual ASR: pip install omnilingual-asr"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from omnilingual_asr.models.inference.pipeline import (
                ASRInferencePipeline,  # type: ignore
            )
        except ModuleNotFoundError as exc:
            raise DependencyMissing("omnilingual", "omnilingual-asr", cause=exc) from exc
        return ASRInferencePipeline(model_card=self.spec.source.rsplit("/", 1)[-1])

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        lang = str(settings.get("lang_code") or "")
        if not lang:
            base = self.language_for(settings) or "ru"
            lang = {"ru": "rus_Cyrl", "en": "eng_Latn", "uk": "ukr_Cyrl",
                    "kk": "kaz_Cyrl"}.get(base, "rus_Cyrl")
        self.report(progress, 0.2, "распознавание")
        try:
            out = self._model.transcribe([str(audio_path)], lang=[lang],
                                         batch_size=int(settings.get("batch_size") or 4))
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        text = out[0] if isinstance(out, (list, tuple)) and out else str(out)
        return TranscriptionResult(segments=_single(text, info.duration_s, lang[:3]),
                                   language=lang, duration=info.duration_s,
                                   meta={"lang_code": lang})


class VoxtralEngine(Engine):
    """Mistral Voxtral: аудио-LLM с транскрипцией и потоковым режимом."""

    id = "voxtral"
    supports_streaming = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import transformers  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, ("Для Voxtral нужен transformers>=4.54 и mistral-common[audio]; "
                           "рекомендуется запуск через vLLM.")

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import torch  # type: ignore
            from transformers import AutoProcessor, VoxtralForConditionalGeneration  # type: ignore
        except ImportError as exc:
            raise DependencyMissing("voxtral", "transformers", cause=exc) from exc
        device = self.resolve_device(settings)
        processor = AutoProcessor.from_pretrained(self.spec.source)
        model = VoxtralForConditionalGeneration.from_pretrained(
            self.spec.source,
            torch_dtype=torch.bfloat16 if device.startswith("cuda") else torch.float32,
            device_map="auto" if device.startswith("cuda") else None)
        return {"processor": processor, "model": model, "device": device}

    #: Сколько звука отдаётся модели за раз. Окно Voxtral — 32 тысячи
    #: токенов, и полчаса звука (заявленный предел) съедают из них больше
    #: двадцати: на текст остаётся мало, и длинная запись обрывалась молча.
    #: Десять минут — с запасом и для звука, и для текста.
    КУСОК_С = 600.0

    #: Токенов текста на секунду звука — с запасом на быструю речь: около
    #: трёх слов в секунду и полтора-два токена на слово.
    ТОКЕНОВ_НА_СЕКУНДУ = 6

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        """Распознаёт запись кусками по речи, не длиннее `КУСОК_С`.

        Прежде здесь было три беды разом. Метод назывался
        `apply_transcrition_request` — с опечаткой, которой в transformers
        нет, — и движок падал на первом же задании. Запись отдавалась
        целиком, хотя модель принимает не больше получаса. А ответ
        обрезался на 2048 токенах — это минут десять речи, остальное молча
        пропадало. Язык по умолчанию стоял «ru», которого Voxtral не знает;
        теперь, если язык не задан, модель определяет его сама.
        """
        import tempfile  # noqa: PLC0415

        from ..pipeline.audio import slice_wav  # noqa: PLC0415

        info = probe(audio_path)
        processor = self._model["processor"]
        model = self._model["model"]
        language = self.language_for(settings) or None
        предел = min(float(self.spec.max_audio_s or self.КУСОК_С), self.КУСОК_С)
        куски = _куски_по_речи(audio_path, info.duration_s, settings, предел)
        segments: list[Segment] = []
        with tempfile.TemporaryDirectory(prefix="voxtral-",
                                         dir=settings.get("temp_dir") or None) as tmp:
            for номер, (начало, конец) in enumerate(куски):
                self.report(progress, 0.1 + 0.85 * номер / max(1, len(куски)),
                            "распознавание")
                путь = audio_path
                if len(куски) > 1:
                    путь = slice_wav(audio_path, Path(tmp) / f"part{номер:04d}.wav",
                                     начало, конец)
                text = self._кусок(processor, model, путь, language, конец - начало)
                if text.strip():
                    segments.append(Segment(start=round(начало, 3), end=round(конец, 3),
                                            text=text.strip(), language=language))
        return TranscriptionResult(segments=segments, language=language or "",
                                   duration=info.duration_s)

    def _кусок(self, processor: Any, model: Any, path: Path, language: str | None,
               seconds: float) -> str:
        try:
            inputs = processor.apply_transcription_request(
                language=language, audio=str(path), model_id=self.spec.source)
            inputs = inputs.to(model.device, dtype=model.dtype)
            предел = int(min(8192, max(256, seconds * self.ТОКЕНОВ_НА_СЕКУНДУ)))
            outputs = model.generate(**inputs, max_new_tokens=предел)
            decoded = processor.batch_decode(
                outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)
            return str(decoded[0]) if decoded else ""
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc


def _куски_по_речи(path: Path, duration: float, settings: dict[str, Any],
                   предел: float) -> list[tuple[float, float]]:
    """Нарезка записи на куски не длиннее `предел` — с разрезами в паузах.

    Участки речи складываются подряд, пока кусок помещается в предел;
    разрез проходит между участками, то есть в тишине, а не посреди слова.
    Без поиска речи (выключен или ничего не нашёл) — ровными долями.
    """
    import math  # noqa: PLC0415

    from ..pipeline import vad  # noqa: PLC0415

    if duration <= предел:
        return [(0.0, duration)]
    спаны = []
    if settings.get("vad_enabled", True):
        спаны = vad.detect(path, {**settings, "vad_max_speech_s": min(предел, 30.0)})
    if not спаны:
        частей = int(math.ceil(duration / предел))
        шаг = duration / частей
        return [(i * шаг, min(duration, (i + 1) * шаг)) for i in range(частей)]
    куски: list[tuple[float, float]] = []
    начало, конец = спаны[0].start, спаны[0].end
    for спан in спаны[1:]:
        if спан.end - начало <= предел:
            конец = спан.end
        else:
            куски.append((начало, конец))
            начало, конец = спан.start, спан.end
    куски.append((начало, конец))
    return куски


class KyutaiEngine(Engine):
    """Kyutai STT: потоковое распознавание с задержкой 0.5 с (английский и французский)."""

    id = "kyutai"
    supports_streaming = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import moshi  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен Kyutai STT: pip install moshi"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from moshi.models import loaders  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("kyutai", "moshi", cause=exc) from exc
        return loaders.CheckpointInfo.from_hf_repo(self.spec.source)

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        raise EngineError(
            "Kyutai STT в ASR Hub доступен только в потоковом режиме.",
            hint="Используйте вкладку «Реальное время» или другой движок для файлов.")


class SherpaOnnxEngine(Engine):
    """sherpa-onnx: лёгкий рантайм ONNX без PyTorch."""

    id = "sherpa_onnx"
    supports_word_timestamps = True
    supports_streaming = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        try:
            import sherpa_onnx  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен sherpa-onnx: pip install sherpa-onnx"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            import sherpa_onnx  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("sherpa_onnx", "sherpa-onnx", cause=exc) from exc
        base = Path(settings.get("models_dir") or ".") / "sherpa-onnx" / self.spec.id
        if not base.exists():
            raise EngineError(
                f"Каталог модели sherpa-onnx не найден: {base}",
                hint="Скачайте модель: asrctl models download " + self.spec.id)
        model_file = next(iter(base.glob("model*.onnx")), None)
        tokens = base / "tokens.txt"
        if model_file is None or not tokens.exists():
            raise EngineError("В каталоге модели нет файлов model.onnx и tokens.txt.")
        return sherpa_onnx.OfflineRecognizer.from_nemo_ctc(
            model=str(model_file), tokens=str(tokens),
            num_threads=S.integer(settings, "cpu_threads", 0) or (os.cpu_count() or 4))

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        from ..pipeline.audio import load_samples

        info = probe(audio_path)
        samples, rate = load_samples(audio_path)
        self.report(progress, 0.2, "распознавание")
        stream = self._model.create_stream()
        stream.accept_waveform(rate, samples)
        self._model.decode_stream(stream)
        text = stream.result.text
        return TranscriptionResult(segments=_single(text, info.duration_s,
                                                    self.language_for(settings) or "ru"),
                                   language=self.language_for(settings) or "ru",
                                   duration=info.duration_s)
