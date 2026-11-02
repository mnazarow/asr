"""Адаптер Qwen3-ASR (Alibaba): 30 языков, лицензия Apache-2.0.

Пакет qwen-asr (README QwenLM/Qwen3-ASR): модель создаётся через
`Qwen3ASRModel.from_pretrained(источник, dtype=…, device_map=…)`, а
`transcribe(audio=…, language=…)` возвращает список объектов с полями
`text` и `language`; язык передаётся названием («Russian»), None — модель
определяет его сама. Прежде адаптер импортировал несуществующий `QwenASR`,
глотал ошибку и уходил в конвейер transformers, который модель типа
qwen3_asr не знает, — и исправный движок отвечал «не установлен пакет».
Проверено по документации пакета, не запуском: библиотеки в среде нет.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..errors import DependencyMissing, classify_exception
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

#: Код языка из настроек → название, которым язык задаётся Qwen3-ASR.
ЯЗЫКИ_QWEN = {
    "zh": "Chinese", "en": "English", "yue": "Cantonese", "ar": "Arabic",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese",
    "id": "Indonesian", "it": "Italian", "ko": "Korean", "ru": "Russian",
    "th": "Thai", "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish",
    "hi": "Hindi", "ms": "Malay", "nl": "Dutch", "sv": "Swedish", "da": "Danish",
    "fi": "Finnish", "pl": "Polish", "cs": "Czech", "fil": "Filipino",
    "fa": "Persian", "el": "Greek", "ro": "Romanian", "hu": "Hungarian",
    "mk": "Macedonian",
}
_КОДЫ = {название.lower(): код for код, название in ЯЗЫКИ_QWEN.items()}


class Qwen3ASREngine(Engine):
    id = "qwen3_asr"
    supports_batching = True
    supports_streaming = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        # Только сам пакет: прежде движок считался установленным по одному
        # наличию transformers, а без qwen-asr модель не грузится вовсе.
        try:
            import qwen_asr  # type: ignore  # noqa: F401
            return True, ""
        except ModuleNotFoundError:
            return False, "Не установлен qwen-asr: pip install -U qwen-asr"

    def _load(self, settings: dict[str, Any]) -> Any:
        try:
            from qwen_asr import Qwen3ASRModel  # type: ignore
        except ModuleNotFoundError as exc:
            raise DependencyMissing("qwen3_asr", "qwen-asr", cause=exc) from exc
        device = self.resolve_device(settings)
        kwargs: dict[str, Any] = {}
        try:
            import torch  # type: ignore

            kwargs["dtype"] = torch.bfloat16 if device.startswith("cuda") else torch.float32
        except ModuleNotFoundError:
            pass
        if device.startswith("cuda"):
            kwargs["device_map"] = device if ":" in device else "cuda:0"
        elif device == "cpu":
            kwargs["device_map"] = "cpu"
        if settings.get("models_dir"):
            # Тот же каталог, куда веса кладёт раздел «Модели».
            kwargs["cache_dir"] = str(settings["models_dir"])
        try:
            return Qwen3ASRModel.from_pretrained(
                self.spec.source,
                max_inference_batch_size=max(1, int(settings.get("batch_size") or 8)),
                **kwargs)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        код = self.language_for(settings)
        язык = ЯЗЫКИ_QWEN.get(код or "")
        self.report(progress, 0.1, "распознавание")
        try:
            ответы = self._model.transcribe(audio=str(audio_path), language=язык)
        except Exception as exc:
            raise classify_exception(exc, engine=self.id, model=self.spec.id) from exc
        первый = ответы[0] if isinstance(ответы, (list, tuple)) and ответы else ответы
        text = str(getattr(первый, "text", "") or "").strip()
        определён = str(getattr(первый, "language", "") or "")
        итог_языка = код or _КОДЫ.get(определён.lower(), определён.lower())
        segments: list[Segment] = []
        if text:
            segments.append(Segment(start=0.0, end=info.duration_s, text=text,
                                    language=итог_языка or None))
        self.report(progress, 0.98, "сборка результата")
        return TranscriptionResult(segments=segments, language=итог_языка or "",
                                   duration=info.duration_s, meta={"backend": "qwen_asr"})
