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
from .errors import ASRHubError, JobCancelled, NoSpeechDetected
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
    #: Огибающая громкости: список кривых с полями audio_waveform,
    #: sample_rate, speaker и label.
    waveform: list[dict[str, Any]] = field(default_factory=list)

    #: Стадии, которые входят в RTF. Загрузка весов исключена намеренно:
    #: она случается раз на несколько заданий и к скорости распознавания
    #: отношения не имеет, а её включение завышало показатель настолько,
    #: что сравнивать его с цифрами производителей было бессмысленно.
    RTF_STAGES = ("audio_prep", "vad", "inference", "alignment",
                  "diarization", "postprocess")

    @property
    def rtf(self) -> float:
        """Отношение времени обработки к длительности записи.

        Считается по стадиям конвейера без загрузки модели и без выгрузки
        результатов: первое — разовая трата, второе к распознаванию не
        относится. Полное время задания хранится отдельно, в
        `processing_time_s`.
        """
        total = sum(value for stage, value in self.timings.items()
                    if stage in self.RTF_STAGES)
        return round(total / self.duration_s, 4) if self.duration_s > 0 else 0.0

    @property
    def rtf_total(self) -> float:
        """RTF с учётом всех стадий, включая загрузку модели и выгрузку."""
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
                "rtf_total": self.rtf_total,
                "processing_time_s": round(sum(self.timings.values()), 3),
                "segments": len(self.segments),
                "words": sum(len(s.get("text", "").split()) for s in self.segments),
                "avg_confidence": self.stats.get("avg_confidence"),
                "speech_ratio": self.stats.get("speech_ratio"),
                **{f"stage_{k}_s": round(v, 3) for k, v in self.timings.items()},
            },
            "stats": self.stats,
            "warnings": self.warnings,
            "waveform": self.waveform,
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

    def snapshot(self) -> dict[str, float]:
        """Замеры на текущий момент, вместе с ещё идущей стадией.

        Нужен там, где показатели надо положить в файл до того, как этап
        закончится: сам файл и есть результат этого этапа.
        """
        values = dict(self.values)
        if self._label:
            values[self._label] = values.get(self._label, 0.0) + (
                time.perf_counter() - self._start)
        return values


def process_job(source: Path, settings: dict[str, Any], registry: EngineRegistry,
                *, workdir: Path, outdir: Path, basename: str,
                progress: ProgressFn | None = None,
                cancelled: Callable[[], bool] | None = None) -> ProcessOutcome:
    """Полный цикл обработки одного файла."""

    def report(value: float, stage: str) -> None:
        # Проверка отмены живёт здесь, потому что этот обработчик движки
        # вызывают между фрагментами: так отмена доходит внутрь распознавания,
        # а не ждёт его конца.
        check_cancel()
        if progress is not None:
            try:
                progress(max(0.0, min(1.0, value)), stage)
            except Exception:
                pass

    def check_cancel() -> None:
        if cancelled is not None and cancelled():
            raise JobCancelled(
                "Задание отменено пользователем.",
                hint="Повторить можно кнопкой «Повторить» в карточке задания.")

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
    prepared_audio = audio_mod.prepare(source, workdir, settings)
    channels = prepared_audio.channels
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
    silent_channels: list[str] = []

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
            outcome.stats.setdefault("speech", {})[label or "моно"] = speech_stats
            outcome.stats["speech_ratio"] = speech_stats.get("speech_ratio")
            if not spans and info.duration_s > 1.0:
                # Раньше здесь стоял raise, и на стереозаписи молчащий канал
                # уносил с собой уже распознанный первый: в записи звонка,
                # где клиент не сказал ни слова, задание падало целиком.
                # Тишина в одном канале — это не отказ, а факт о записи.
                silent_channels.append(label or "моно")
                continue

        # ---- 3. Распознавание -------------------------------------------
        check_cancel()

        def make_progress(index: int, total: int):
            # Индекс канала связываем явно, чтобы замыкание не смотрело
            # на переменную цикла после её изменения.
            def engine_progress(value: float, stage: str) -> None:
                base = 0.18 + 0.64 * (index / max(1, total))
                width = 0.64 / max(1, total)
                report(base + width * value, stage)
            return engine_progress

        # lease() держит модель занятой: пока идёт распознавание, её не
        # выгрузит ни сборщик простоя, ни вытеснение из кеша.
        with registry.lease(settings) as engine:
            timer.start("inference")
            result = engine.transcribe(prepared, settings,
                                       make_progress(channel_index, len(channels)))
            timer.stop()
        # Загрузка весов происходит внутри transcribe(), поэтому вычитаем её
        # из времени распознавания: иначе она считалась бы дважды и завышала RTF.
        timer.values["inference"] = max(
            0.0, timer.values.get("inference", 0.0) - result.model_load_s)
        timer.values["model_load"] = timer.values.get("model_load", 0.0) + result.model_load_s

        engine_meta = dict(result.meta)
        if result.language:
            languages.append(result.language)
        for segment in result.segments:
            if label:
                segment.speaker = segment.speaker or label
            all_segments.append(segment)

    # Речи нет ни в одном канале — вот это уже отказ. Если молчал только
    # один, отмечаем это предупреждением и работаем с остальными.
    if silent_channels and not all_segments:
        raise NoSpeechDetected(
            f"В файле «{source.name}» не обнаружено речи "
            f"(длительность {info.duration_s:.1f} с).")
    if silent_channels:
        outcome.warnings.append(
            "Речь не обнаружена в каналах: " + ", ".join(silent_channels))

    all_segments.sort(key=lambda s: s.start)

    # ---- 4. Принудительное выравнивание ----------------------------------
    # Идёт до диаризации: та опирается на границы сегментов, и чем они точнее,
    # тем меньше реплик достаётся не тому говорящему.
    if str(settings.get("alignment_backend") or "none") != "none" and all_segments:
        check_cancel()
        timer.start("alignment")
        report(0.80, "уточнение границ слов")
        try:
            from .pipeline.alignment import align_segments

            all_segments = align_segments(channels[0][1], all_segments, settings)
        except ASRHubError as exc:
            # Выравнивание — улучшение, а не обязательный этап: при неудаче
            # остаются модельные таймкоды, а задание доводится до конца.
            outcome.warnings.append(f"Выравнивание не выполнено: {exc.message}")
        except Exception as exc:
            outcome.warnings.append(f"Выравнивание не выполнено: {exc}")
        timer.stop()

    # ---- 5. Диаризация ---------------------------------------------------
    if settings.get("diarization_enabled") and not any(s.speaker for s in all_segments):
        check_cancel()
        timer.start("diarization")
        report(0.84, "разделение по говорящим")
        try:
            from .pipeline.diarization import diarize_segments

            # Предупреждения складываем прямо в результат: подмена
            # диаризации разбивкой по паузам должна быть видна в выгрузке,
            # а не только в журнале сервера.
            all_segments = diarize_segments(channels[0][1], all_segments, settings,
                                            outcome.warnings)
        except ASRHubError as exc:
            outcome.warnings.append(f"Диаризация не выполнена: {exc.message}")
        except Exception as exc:
            outcome.warnings.append(f"Диаризация не выполнена: {exc}")
        timer.stop()

    # Подготовка сместила систему координат: обрезка начальной тишины
    # сдвинула всё на offset_s, изменение темпа сжало в speed раз. Движок
    # работал уже в новых координатах, и без возврата назад субтитры
    # разъезжаются с исходной записью ровно на длину обрезанной тишины.
    #
    # Возврат стоит здесь, а не сразу после распознавания: выравнивание и
    # диаризация выше получают тот же подготовленный файл и должны видеть
    # метки в его координатах. Раньше возврат шёл до них — и обе работали по
    # меткам исходной записи, глядя в обрезанный файл: при обрезке в пять
    # секунд выравнивание искало слова за концом звука.
    if prepared_audio.shifted:
        for segment in all_segments:
            segment.start = round(prepared_audio.to_source_time(segment.start), 3)
            segment.end = round(prepared_audio.to_source_time(segment.end), 3)
            for word in segment.words or []:
                for key in ("start", "end"):
                    if isinstance(word.get(key), (int, float)):
                        word[key] = round(prepared_audio.to_source_time(float(word[key])), 3)
        log.debug("Таймкоды возвращены в координаты исходной записи: "
                  "сдвиг %.3f с, темп %.3f", prepared_audio.offset_s, prepared_audio.speed)

    # ---- 6. Постобработка -------------------------------------------------
    check_cancel()
    timer.start("postprocess")
    report(0.90, "постобработка текста")
    raw_segments = [s.to_dict() for s in all_segments]
    model_punct = bool(engine_meta.get("punctuation_from_model")) or (
        spec is not None and spec.punctuation)
    try:
        processed, pp_stats = postprocess.process(raw_segments, settings,
                                                  model_has_punctuation=model_punct)
    except Exception as exc:                                # noqa: BLE001
        # Единственный шаг после распознавания, который не имел защиты, — а
        # ронять его умеет и опечатка в пользовательском словаре замен.
        # Расшифровка к этому моменту уже получена, и терять её из-за
        # пунктуации или замен нельзя: отдаём сырые сегменты.
        log.warning("Постобработка не выполнена: %s", exc)
        outcome.warnings.append(f"Постобработка не выполнена: {exc}")
        processed, pp_stats = raw_segments, {}
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

    # ---- 6а. Огибающая громкости -------------------------------------------
    # После постобработки, а не до неё: на монозаписи кривые подписываются
    # именами говорящих, а переименование по speaker_names происходит именно
    # в постобработке. Раньше полоса шла раньше и оставалась с подписями
    # «Говорящий 1», тогда как в расшифровке стояло «Оператор» — сопоставить
    # одно с другим было нельзя.
    if settings.get("waveform_enabled", True):
        check_cancel()
        timer.start("waveform")
        report(0.86, "полоса громкости")
        try:
            from .pipeline.waveform import build as build_waveform

            outcome.waveform = build_waveform(channels, outcome.segments, settings)
            # Полоса считается по подготовленному звуку, а расшифровка уже
            # вернулась в координаты исходной записи. Без перевода оси
            # карточка задания рисовала их рядом на разных шкалах: щелчок по
            # полосе открывал не ту реплику, а при обрезке тишины полоса
            # начиналась там, где в записи ещё тишина.
            if prepared_audio.shifted:
                for track in outcome.waveform or []:
                    for points in track.values():
                        if not isinstance(points, list):
                            continue
                        for point in points:
                            if isinstance(point, dict) and "time" in point:
                                point["time"] = round(
                                    prepared_audio.to_source_time(float(point["time"])), 3)
        except Exception as exc:                            # noqa: BLE001
            # Полоса громкости — вспомогательные данные: её отсутствие не
            # повод терять уже посчитанную расшифровку.
            log.warning("Огибающая не построена: %s", exc)
            outcome.warnings.append(f"Полоса громкости не построена: {exc}")
        timer.stop()

    reference = str(settings.get("reference_text") or "").strip()
    if reference:
        # Точность считаем по тексту без меток говорящих: в эталоне их нет,
        # и каждая реплика добавляла две лишние вставки, завышая WER.
        plain = "\n\n".join(postprocess.build_paragraphs(
            processed, str(settings.get("paragraph_mode") or "speaker"),
            speaker_labels=False))
        try:
            detail = metrics.detailed(reference, plain)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Точность не рассчитана: %s", exc)
            outcome.warnings.append(f"Точность не рассчитана: {exc}")
            detail = {}
        outcome.stats["accuracy"] = detail

    # ---- 7. Выгрузка ------------------------------------------------------
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
    # Замеры кладём в outcome ДО формирования полезной нагрузки: to_result
    # читает self.timings, и раньше в каждый выгруженный файл уходили нули —
    # «rtf: 0.0», «processing_time_s: 0» и ни одной стадии, тогда как в
    # интерфейсе по тому же заданию стояли настоящие значения. Берём снимок
    # с ещё идущей стадией выгрузки: полностью её закрыть нельзя, сама
    # запись файлов ещё впереди.
    outcome.timings = {k: round(v, 4) for k, v in timer.snapshot().items()}

    result_payload = outcome.to_result(meta)
    outcome.files = export.write_all(result_payload, settings, outdir, basename)

    # Окончательные замеры — уже со временем записи файлов; они уходят в базу
    # и в метрики, где точность важнее совпадения с содержимым файла.
    timer.stop()
    outcome.timings = {k: round(v, 4) for k, v in timer.values.items()}
    report(1.0, "готово")
    return outcome


def settings_digest(settings: dict[str, Any], *, weights: str = "") -> str:
    """Короткий отпечаток настроек — входит в ключ кеша результатов.

    weights — отпечаток файлов модели. Без него кеш опирался только на имя
    модели, и обновление весов под тем же именем возвращало старый
    результат как свежий.
    """
    import hashlib

    from .catalog import PARAMS_BY_KEY

    relevant = {k: v for k, v in sorted(settings.items())
                if k in PARAMS_BY_KEY and k not in (
                    "priority", "max_retries", "job_timeout_s", "webhook_url",
                    "result_retention_days", "output_formats")}
    blob = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    if weights:
        blob += f"\nweights={weights}"
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
