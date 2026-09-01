"""Компактные адаптеры: FunASR, Moonshine, Omnilingual, Voxtral, Kyutai, sherpa-onnx.

Все следуют одному шаблону: ленивый импорт, единый формат сегментов,
преобразование исключений в типизированные ошибки ASR Hub.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

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

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        processor = self._model["processor"]
        model = self._model["model"]
        language = self.language_for(settings) or "ru"
        self.report(progress, 0.2, "распознавание")
        try:
            inputs = processor.apply_transcrition_request(
                language=language, audio=str(audio_path), model_id=self.spec.source)
            inputs = inputs.to(model.device, dtype=model.dtype)
            outputs = model.generate(**inputs, max_new_tokens=2048)
            decoded = processor.batch_decode(
                outputs[:, inputs.input_ids.shape[1]:], skip_special_tokens=True)
            text = decoded[0] if decoded else ""
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        return TranscriptionResult(segments=_single(text, info.duration_s, language),
                                   language=language, duration=info.duration_s)


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
            num_threads=int(settings.get("cpu_threads") or 0) or (os.cpu_count() or 4))

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
