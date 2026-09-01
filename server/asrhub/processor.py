"""Сквозная обработка одного задания.

Порядок этапов и доля прогресса, которую занимает каждый:
  подготовка аудио      0 → 12 %
  поиск речи (VAD)     12 → 18 %
  распознавание        18 → 82 %
  диаризация           82 → 90 %
  постобработка        90 → 96 %
  выгрузка форматов    96 → 100 %

Каждый этап замеряется отдельно: разбивка времени видна в аналитике и
помогает понять, где именно теряется производительность.
"""
from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .catalog import get_model
from .engines import EngineRegistry, Segment
from .errors import ASRHubError, NoSpeechDetected
from .logging_setup import get_logger
from .pipeline import audio as audio_mod
from .pipeline import export, metrics, postprocess, vad

log = get_logger("processor")

ProgressFn = Callable[[float, str], None]


@dataclass
class ProcessOutcome:
    text: str = ""
    segments: list[dict[str, Any]] = field(default_factory=list)
    language: str = ""
    duration_s: float = 0.0
    files: dict[str, str] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    speakers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rtf(self) -> float:
        total = sum(self.timings.values())
        return round(total / self.duration_s, 4) if self.duration_s > 0 else 0.0

    def to_result(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {
            "meta": meta,
            "text": self.text,
            "segments": self.segments,
            "language": self.language,
            "speakers": self.speakers,
            "metrics": {
                "rtf": self.rtf,
                "processing_time_s": round(sum(self.timings.values()), 3),
                "segments": len(self.segments),
                "words": sum(len(s.get("text", "").split()) for s in self.segments),
                "avg_confidence": self.stats.get("avg_confidence"),
                "speech_ratio": self.stats.get("speech_ratio"),
                **{f"stage_{k}_s": round(v, 3) for k, v in self.timings.items()},
            },
            "stats": self.stats,
            "warnings": self.warnings,
        }


class Timer:
    """Замер длительности этапов."""

    def __init__(self) -> None:
        self.values: dict[str, float] = {}
        self._start = time.perf_counter()
        self._label = ""

    def start(self, label: str) -> None:
        self.stop()
        self._label = label
        self._start = time.perf_counter()

    def stop(self) -> None:
        if self._label:
            self.values[self._label] = self.values.get(self._label, 0.0) + (
                time.perf_counter() - self._start)
            self._label = ""


def process_job(source: Path, settings: dict[str, Any], registry: EngineRegistry,
                *, workdir: Path, outdir: Path, basename: str,
                progress: ProgressFn | None = None,
                cancelled: Callable[[], bool] | None = None) -> ProcessOutcome:
    """Полный цикл обработки одного файла."""

    def report(value: float, stage: str) -> None:
        if progress is not None:
            try:
                progress(max(0.0, min(1.0, value)), stage)
            except Exception:
                pass

    def check_cancel() -> None:
        if cancelled is not None and cancelled():
            raise ASRHubError("Задание отменено пользователем.", hint="")

    timer = Timer()
    outcome = ProcessOutcome()
    spec = get_model(str(settings.get("model") or ""))

    # ---- 1. Подготовка аудио -------------------------------------------
    report(0.01, "анализ файла")
    timer.start("audio_prep")
    info = audio_mod.probe(source)
    outcome.duration_s = info.duration_s
    outcome.stats["source"] = info.to_dict()

    if not audio_mod.has_ffmpeg() and source.suffix.lower() != ".wav":
        outcome.warnings.append(
            "ffmpeg не найден: сжатые форматы и видео обрабатываться не будут.")

    check_cancel()
    report(0.04, "подготовка аудио")
    channels = audio_mod.prepare(source, workdir, settings)
    levels = audio_mod.analyze_levels(source)
    if levels:
        outcome.stats["levels"] = levels
        peak = levels.get("peak_db")
        if peak is not None and peak < -30:
            outcome.warnings.append(
                f"Очень тихая запись (пик {peak:.1f} дБ). Включите нормализацию громкости.")
        if peak is not None and peak > -0.5:
            outcome.warnings.append(
                "Запись на пределе громкости — возможны искажения и ошибки распознавания.")
    timer.stop()
    report(0.12, "подготовка завершена")

    all_segments: list[Segment] = []
    engine_meta: dict[str, Any] = {}
    languages: list[str] = []

    for channel_index, (label, prepared) in enumerate(channels):
        check_cancel()

        # ---- 2. Поиск речи ---------------------------------------------
        speech_stats: dict[str, Any] = {}
        if settings.get("vad_enabled", True):
            timer.start("vad")
            report(0.14, "поиск речи")
            spans = vad.detect(prepared, settings)
            speech_stats = vad.speech_statistics(spans, info.duration_s)
            timer.stop()
            if not spans and info.duration_s > 1.0:
                raise NoSpeechDetected(
                    f"В файле «{source.name}» не обнаружено речи "
                    f"(длительность {info.duration_s:.1f} с).")
            outcome.stats.setdefault("speech", {})[label or "моно"] = speech_stats
            outcome.stats["speech_ratio"] = speech_stats.get("speech_ratio")

        # ---- 3. Распознавание -------------------------------------------
        check_cancel()
        timer.start("inference")
        engine = registry.get(settings)

        def make_progress(index: int, total: int):
            # Индекс канала связываем явно, чтобы замыкание не смотрело
            # на переменную цикла после её изменения.
            def engine_progress(value: float, stage: str) -> None:
                base = 0.18 + 0.64 * (index / max(1, total))
                width = 0.64 / max(1, total)
                report(base + width * value, stage)
            return engine_progress

        result = engine.transcribe(prepared, settings,
                                   make_progress(channel_index, len(channels)))
        timer.stop()
        timer.values["model_load"] = timer.values.get("model_load", 0.0) + result.model_load_s

        engine_meta = dict(result.meta)
        if result.language:
            languages.append(result.language)
        for segment in result.segments:
            if label:
                segment.speaker = segment.speaker or label
            all_segments.append(segment)

    all_segments.sort(key=lambda s: s.start)

    # ---- 4. Диаризация ---------------------------------------------------
    if settings.get("diarization_enabled") and not any(s.speaker for s in all_segments):
        check_cancel()
        timer.start("diarization")
        report(0.84, "разделение по говорящим")
        try:
            from .pipeline.diarization import diarize_segments

            all_segments = diarize_segments(channels[0][1], all_segments, settings)
        except ASRHubError as exc:
            outcome.warnings.append(f"Диаризация не выполнена: {exc.message}")
        except Exception as exc:
            outcome.warnings.append(f"Диаризация не выполнена: {exc}")
        timer.stop()

    # ---- 5. Постобработка -------------------------------------------------
    check_cancel()
    timer.start("postprocess")
    report(0.90, "постобработка текста")
    raw_segments = [s.to_dict() for s in all_segments]
    model_punct = bool(engine_meta.get("punctuation_from_model")) or (
        spec is not None and spec.punctuation)
    processed, pp_stats = postprocess.process(raw_segments, settings,
                                              model_has_punctuation=model_punct)
    timer.stop()

    outcome.segments = processed
    outcome.text = "\n\n".join(
        postprocess.build_paragraphs(processed, str(settings.get("paragraph_mode") or "speaker")))
    outcome.language = languages[0] if languages else str(settings.get("language") or "")
    outcome.speakers = sorted({s["speaker"] for s in processed if s.get("speaker")})
    outcome.stats.update(pp_stats)

    confidences = [s["confidence"] for s in processed if s.get("confidence") is not None]
    if confidences:
        outcome.stats["avg_confidence"] = round(sum(confidences) / len(confidences), 4)
        outcome.stats["confidence_distribution"] = metrics.confidence_buckets(confidences)
        outcome.stats["low_confidence_segments"] = sum(1 for c in confidences if c < 0.7)
    words_total = sum(len(s.get("text", "").split()) for s in processed)
    outcome.stats["words"] = words_total
    outcome.stats["chars"] = sum(len(s.get("text", "")) for s in processed)
    outcome.stats["speaking_rate_wpm"] = metrics.speaking_rate(words_total, info.duration_s)

    reference = str(settings.get("reference_text") or "").strip()
    if reference:
        detail = metrics.detailed(reference, outcome.text)
        outcome.stats["accuracy"] = detail

    # ---- 6. Выгрузка ------------------------------------------------------
    check_cancel()
    timer.start("export")
    report(0.96, "сохранение результатов")
    meta = {
        "filename": source.name,
        "model": settings.get("model"),
        "engine": engine_meta.get("engine") or settings.get("engine"),
        "language": outcome.language,
        "duration_s": round(info.duration_s, 2),
        "created_at": time.strftime("%d.%m.%Y %H:%M"),
        "settings_digest": settings_digest(settings),
    }
    result_payload = outcome.to_result(meta)
    outcome.files = export.write_all(result_payload, settings, outdir, basename)
    timer.stop()

    outcome.timings = {k: round(v, 4) for k, v in timer.values.items()}
    report(1.0, "готово")
    return outcome


def settings_digest(settings: dict[str, Any]) -> str:
    """Короткий отпечаток настроек — входит в ключ кеша результатов."""
    import hashlib

    from .catalog import PARAMS_BY_KEY

    relevant = {k: v for k, v in sorted(settings.items())
                if k in PARAMS_BY_KEY and k not in (
                    "priority", "max_retries", "job_timeout_s", "webhook_url",
                    "result_retention_days", "output_formats")}
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.blake2b(blob.encode("utf-8"), digest_size=8).hexdigest()


def safe_workdir(base: Path, job_id: str) -> Path:
    path = base / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def cleanup_workdir(path: Path) -> None:
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
