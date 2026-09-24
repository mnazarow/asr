"""Разделение записи по говорящим.

Порядок выбора: встроенная в модель разметка → pyannote → NVIDIA Sortformer →
простая кластеризация по паузам. Последний вариант работает без зависимостей
и даёт грубое, но полезное приближение для диалогов двух человек.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..engines.base import Segment, device_for
from ..errors import DependencyMissing, GatedModelError
from ..logging_setup import get_logger
from ..monitoring.collector import RUNTIME

log = get_logger("diarization")


def diarize_segments(audio_path: Path, segments: list[Segment],
                     settings: dict[str, Any],
                     warnings: list[str] | None = None) -> list[Segment]:
    """Расставляет говорящих. Возвращает те же сегменты с полем speaker.

    Если выбранный механизм не сработал, применяется разбивка по паузам —
    но об этом обязательно сообщается: в журнал и в список предупреждений
    задания. Раньше подмена была молчаливой, и в протоколе совещания реплики,
    расставленные по паузе в 1,2 секунды, выглядели ровно так же, как
    настоящая диаризация, — отличить их было нельзя ни в интерфейсе, ни в
    выгрузке.
    """
    backend = str(settings.get("diarization_backend") or "auto")
    order = {
        "auto": ["pyannote", "sortformer", "pauses"],
        "pyannote": ["pyannote", "pauses"],
        "sortformer": ["sortformer", "pauses"],
        "channels": ["pauses"],
        "builtin": ["pauses"],
    }.get(backend, ["pauses"])

    def fallback(reason: str) -> list[Segment]:
        if reason:
            note = (f"Диаризация «{backend}» не выполнена ({reason}). "
                    "Говорящие расставлены по паузам — это грубая оценка, "
                    "а не разделение по голосам.")
            log.warning("%s", note)
            if warnings is not None:
                warnings.append(note)
            RUNTIME.inc("asrhub_errors_total", labels={"code": "diarization_fallback",
                                                       "retryable": "true"})
        return _by_pauses(segments, settings)

    failure = ""
    for name in order:
        try:
            if name == "pyannote":
                turns = _pyannote(audio_path, settings)
            elif name == "sortformer":
                turns = _sortformer(audio_path, settings)
            else:
                # До разбивки по паузам дошли штатно — она и была выбрана.
                return fallback(failure)
        except (DependencyMissing, GatedModelError):
            if backend not in ("auto",):
                raise
            continue
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            log.warning("Диаризация «%s» не удалась: %s", name, exc)
            continue
        if turns:
            return _assign(segments, turns)
        failure = failure or "механизм не нашёл ни одного говорящего"
    return fallback(failure)


def _pyannote(audio_path: Path, settings: dict[str, Any]) -> list[tuple[float, float, str]]:
    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyMissing("pyannote", "pyannote.audio", cause=exc) from exc

    token = str(settings.get("hf_token") or os.environ.get("HF_TOKEN", "")
                or os.environ.get("HUGGING_FACE_HUB_TOKEN", ""))
    if not token:
        raise GatedModelError("pyannote/speaker-diarization-community-1",
                              "https://huggingface.co/pyannote/speaker-diarization-community-1")

    model_name = str(settings.get("diarization_model")
                     or "pyannote/speaker-diarization-community-1")
    pipeline = _из_хаба(Pipeline, model_name, token)
    if pipeline is None:
        raise GatedModelError(model_name, f"https://huggingface.co/{model_name}")

    # «auto» — это видеокарта, если она есть. Раньше на карту переносили
    # только при явном «cuda», а умолчание «auto» оставляло pyannote считать
    # на процессоре — в разы дольше самого распознавания.
    device = device_for(settings)
    if device.startswith("cuda"):
        try:
            import torch  # type: ignore

            pipeline.to(torch.device(device))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Диаризация остаётся на процессоре: %s", exc)

    kwargs: dict[str, Any] = {}
    num = S.integer(settings, "diarization_num_speakers", 0)
    if num:
        kwargs["num_speakers"] = num
    else:
        kwargs["min_speakers"] = int(settings.get("diarization_min_speakers") or 1)
        kwargs["max_speakers"] = int(settings.get("diarization_max_speakers") or 8)

    итог = pipeline(str(audio_path), **kwargs)
    # pyannote.audio 4 отдаёт не разметку, а набор разметок. Для расшифровки
    # нужна «исключающая» — без наложенных реплик: каждому слову один
    # говорящий. В 3.x ответ — сама разметка.
    annotation = (getattr(итог, "exclusive_speaker_diarization", None)
                  or getattr(итог, "speaker_diarization", None) or итог)
    return [(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)]


def _из_хаба(Pipeline: Any, model_name: str, token: str) -> Any:
    """`Pipeline.from_pretrained` с токеном — под обе ветки pyannote.audio.

    В 4.x параметр называется `token`, а `use_auth_token` убран: вызов
    падал с TypeError, и диаризация через pyannote не работала никогда —
    конвейер уходил к Sortformer или к разбивке по паузам. Ставится же по
    требованию «pyannote.audio>=3.1» именно 4.x, а модель по умолчанию
    speaker-diarization-community-1 без неё и не грузится. В 3.x — наоборот.
    """
    import inspect  # noqa: PLC0415

    try:
        параметры = inspect.signature(Pipeline.from_pretrained).parameters
    except (TypeError, ValueError):
        параметры = {}
    if "token" in параметры or not параметры:
        try:
            return Pipeline.from_pretrained(model_name, token=token)
        except TypeError as exc:
            if "token" not in str(exc):
                raise
    return Pipeline.from_pretrained(model_name, use_auth_token=token)


def _sortformer(audio_path: Path, settings: dict[str, Any]) -> list[tuple[float, float, str]]:
    try:
        from nemo.collections.asr.models import SortformerEncLabelModel  # type: ignore
    except ModuleNotFoundError as exc:
        raise DependencyMissing("nemo", "nemo-toolkit-asr", cause=exc) from exc

    model = SortformerEncLabelModel.from_pretrained(
        str(settings.get("diarization_model") or "nvidia/diar_streaming_sortformer_4spk-v2"))
    model.eval()
    predictions = model.diarize(audio=str(audio_path), batch_size=1)
    turns: list[tuple[float, float, str]] = []
    for item in predictions or []:
        for entry in (item if isinstance(item, (list, tuple)) else [item]):
            parts = str(entry).split()
            if len(parts) >= 3:
                try:
                    turns.append((float(parts[0]), float(parts[1]), parts[2]))
                except ValueError:
                    continue
    return turns


def _assign(segments: list[Segment], turns: list[tuple[float, float, str]]) -> list[Segment]:
    """Присваивает сегменту говорящего с максимальным перекрытием по времени."""
    mapping: dict[str, str] = {}
    for segment in segments:
        best_label, best_overlap = None, 0.0
        for start, end, label in turns:
            overlap = min(segment.end, end) - max(segment.start, start)
            if overlap > best_overlap:
                best_overlap, best_label = overlap, label
        if best_label is not None:
            if best_label not in mapping:
                mapping[best_label] = f"Говорящий {len(mapping) + 1}"
            segment.speaker = mapping[best_label]
        for word in segment.words:
            wstart = float(word.get("start", segment.start))
            for start, end, label in turns:
                if start <= wstart < end:
                    word["speaker"] = mapping.get(label, label)
                    break
    return segments


def _by_pauses(segments: list[Segment], settings: dict[str, Any]) -> list[Segment]:
    """Грубое разделение по длинным паузам между репликами.

    Не заменяет настоящую диаризацию, но на поочерёдном диалоге двух человек
    даёт разумный результат и не требует ни одной зависимости.
    """
    speakers = max(2, S.integer(settings, "diarization_num_speakers", 2))
    gap_threshold = float(settings.get("diarization_pause_s") or 1.2)
    current = 0
    previous_end = None
    for segment in segments:
        if previous_end is not None and segment.start - previous_end >= gap_threshold:
            current = (current + 1) % speakers
        segment.speaker = f"Говорящий {current + 1}"
        previous_end = segment.end
    log.info("Использовано приближённое разделение по паузам (%d говорящих)", speakers)
    return segments
