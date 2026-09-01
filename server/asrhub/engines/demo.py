"""Демонстрационный движок — работает без каких-либо зависимостей.

Нужен, чтобы проверить весь путь задания: очередь, конвейер, аналитику,
экспорт и интерфейс — до того, как на сервер загружены веса моделей.
Генерирует правдоподобный русский текст, привязанный к реальным
границам речи, найденным детектором.
"""
from __future__ import annotations

import hashlib
import random
import time
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..pipeline import vad
from ..pipeline.audio import probe
from .base import Engine, ProgressCallback, Segment, TranscriptionResult

_PHRASES = [
    "Коллеги, добрый день, начнём с итогов прошлой недели",
    "По плану релиза мы идём с опережением на два дня",
    "Нагрузочное тестирование показало запас по производительности",
    "Предлагаю вынести этот вопрос на отдельную встречу",
    "Отчёт будет готов к пятнице, я пришлю его в общий канал",
    "Здесь важно согласовать формулировки с юридическим отделом",
    "Давайте зафиксируем это решение в протоколе",
    "Бюджет остаётся в прежних рамках, дополнительных заявок нет",
    "Нужно проверить, как это поведёт себя на реальных данных",
    "Спасибо всем за подготовку материалов к встрече",
    "Второй вариант выглядит надёжнее с точки зрения поддержки",
    "Уточню детали у смежной команды и вернусь с ответом",
]
_SPEAKERS = ["Говорящий 1", "Говорящий 2", "Говорящий 3"]


class DemoEngine(Engine):
    id = "demo"
    supports_word_timestamps = True
    supports_batching = True
    supports_streaming = True
    outputs_punctuation = True

    @classmethod
    def check_available(cls) -> tuple[bool, str]:
        return True, ""

    def _load(self, settings: dict[str, Any]) -> Any:
        time.sleep(0.05)      # имитация загрузки весов
        return {"ready": True}

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: ProgressCallback | None) -> TranscriptionResult:
        info = probe(audio_path)
        duration = info.duration_s

        # Детерминированность: один и тот же файл даёт один и тот же текст.
        seed = int(hashlib.blake2b(str(audio_path).encode(), digest_size=8).hexdigest(), 16)
        rnd = random.Random(seed)

        opts = dict(settings)
        opts.setdefault("vad_max_speech_s", 12.0)
        speech = vad.detect(audio_path, opts) if settings.get("vad_enabled", True) else []
        if not speech:
            step = 6.0
            speech = [vad.SpeechSegment(t, min(duration, t + step))
                      for t in _frange(0.0, duration, step)]

        rtf = float(settings.get("simulated_rtf") or self.spec.default_params.get(
            "simulated_rtf", 0.15))
        segments: list[Segment] = []
        speaker_count = max(1, min(3, S.integer(settings, "diarization_num_speakers", 2)))

        for idx, span in enumerate(speech):
            self.report(progress, (idx + 1) / max(1, len(speech)), "распознавание")
            time.sleep(min(0.05, span.duration * rtf * 0.02))
            text = _PHRASES[rnd.randrange(len(_PHRASES))]
            words = text.split()
            step = span.duration / max(1, len(words))
            word_items = [
                {"word": w,
                 "start": round(span.start + i * step, 3),
                 "end": round(span.start + (i + 1) * step, 3),
                 "confidence": round(rnd.uniform(0.82, 0.995), 4)}
                for i, w in enumerate(words)
            ]
            segments.append(Segment(
                start=span.start,
                end=span.end,
                text=text + ".",
                speaker=(_SPEAKERS[idx % speaker_count]
                         if settings.get("diarization_enabled") else None),
                confidence=round(sum(w["confidence"] for w in word_items) / len(word_items), 4),
                no_speech_prob=round(rnd.uniform(0.001, 0.08), 4),
                compression_ratio=round(rnd.uniform(1.3, 2.1), 3),
                temperature=0.0,
                language="ru",
                words=word_items if settings.get("word_timestamps", True) else [],
            ))

        return TranscriptionResult(
            segments=segments,
            language="ru",
            language_probability=0.99,
            duration=duration,
            meta={"simulated": True,
                  "note": "Результат сгенерирован демонстрационным движком, "
                          "реальное распознавание не выполнялось"},
        )


def _frange(start: float, stop: float, step: float):
    value = start
    while value < stop:
        yield value
        value += step
