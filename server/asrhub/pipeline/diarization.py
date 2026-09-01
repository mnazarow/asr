"""Разделение записи по говорящим.

Порядок выбора: встроенная в модель разметка → pyannote → NVIDIA Sortformer →
простая кластеризация по паузам. Последний вариант работает без зависимостей
и даёт грубое, но полезное приближение для диалогов двух человек.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..engines.base import Segment
from ..errors import DependencyMissing, GatedModelError
from ..logging_setup import get_logger

log = get_logger("diarization")


def diarize_segments(audio_path: Path, segments: list[Segment],
                     settings: dict[str, Any]) -> list[Segment]:
    backend = str(settings.get("diarization_backend") or "auto")
    order = {
        "auto": ["pyannote", "sortformer", "pauses"],
        "pyannote": ["pyannote", "pauses"],
        "sortformer": ["sortformer", "pauses"],
        "channels": ["pauses"],
        "builtin": ["pauses"],
    }.get(backend, ["pauses"])

    for name in order:
        try:
            if name == "pyannote":
                turns = _pyannote(audio_path, settings)
            elif name == "sortformer":
                turns = _sortformer(audio_path, settings)
            else:
                return _by_pauses(segments, settings)
        except (DependencyMissing, GatedModelError):
            if backend not in ("auto",):
                raise
            continue
        except Exception as exc:
            log.warning("Диаризация «%s» не удалась: %s", name, exc)
            continue
        if turns:
            return _assign(segments, turns)
    return _by_pauses(segments, settings)


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
    pipeline = Pipeline.from_pretrained(model_name, use_auth_token=token)

    device = str(settings.get("device") or "auto")
    if device.startswith("cuda"):
        try:
            import torch  # type: ignore

            pipeline.to(torch.device(device))
        except Exception:
            pass

    kwargs: dict[str, Any] = {}
    num = int(settings.get("diarization_num_speakers") or 0)
    if num:
        kwargs["num_speakers"] = num
    else:
        kwargs["min_speakers"] = int(settings.get("diarization_min_speakers") or 1)
        kwargs["max_speakers"] = int(settings.get("diarization_max_speakers") or 8)

    annotation = pipeline(str(audio_path), **kwargs)
    return [(float(turn.start), float(turn.end), str(speaker))
            for turn, _, speaker in annotation.itertracks(yield_label=True)]


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
    speakers = max(2, int(settings.get("diarization_num_speakers") or 2))
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
