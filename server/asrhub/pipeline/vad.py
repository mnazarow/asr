"""Детектор речевой активности и нарезка длинного аудио.

Порядок выбора реализации: silero (лучшее качество, MIT) → ten → webrtc →
встроенный энергетический детектор. Последний работает без каких-либо
зависимостей и гарантирует, что нарезка возможна всегда.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import NoSpeechDetected
from ..logging_setup import get_logger
from .audio import load_samples

log = get_logger("vad")


@dataclass(slots=True)
class SpeechSegment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict[str, float]:
        return {"start": round(self.start, 3), "end": round(self.end, 3),
                "duration": round(self.duration, 3)}


_SILERO_CACHE: dict[str, Any] = {}


def _silero_model():
    if "model" in _SILERO_CACHE:
        return _SILERO_CACHE["model"], _SILERO_CACHE["utils"]
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad  # type: ignore

        model = load_silero_vad()
        _SILERO_CACHE["model"] = model
        _SILERO_CACHE["utils"] = {"get_speech_timestamps": get_speech_timestamps}
        return model, _SILERO_CACHE["utils"]
    except ModuleNotFoundError:
        pass
    try:  # запасной путь через torch.hub
        import torch  # type: ignore

        model, utils = torch.hub.load(
            repo_or_dir="snakers4/silero-vad", model="silero_vad",
            force_reload=False, onnx=False, trust_repo=True)
        helpers = {"get_speech_timestamps": utils[0]}
        _SILERO_CACHE["model"] = model
        _SILERO_CACHE["utils"] = helpers
        return model, helpers
    except Exception as exc:
        log.debug("Silero VAD недоступен: %s", exc)
        return None, None


def _detect_silero(path: Path, opts: dict[str, Any]) -> list[SpeechSegment] | None:
    model, utils = _silero_model()
    if model is None:
        return None
    try:
        import torch  # type: ignore

        samples, rate = load_samples(path)
        tensor = torch.as_tensor(samples, dtype=torch.float32)
        stamps = utils["get_speech_timestamps"](
            tensor, model,
            sampling_rate=rate,
            threshold=float(opts.get("vad_threshold", 0.5)),
            neg_threshold=float(opts.get("vad_neg_threshold", 0.35)),
            min_speech_duration_ms=int(opts.get("vad_min_speech_ms", 250)),
            max_speech_duration_s=float(opts.get("vad_max_speech_s", 22.0)),
            min_silence_duration_ms=int(opts.get("vad_min_silence_ms", 500)),
            speech_pad_ms=int(opts.get("vad_speech_pad_ms", 200)),
            return_seconds=True,
        )
        return [SpeechSegment(float(s["start"]), float(s["end"])) for s in stamps]
    except Exception as exc:
        log.warning("Silero VAD дал сбой (%s), используется запасной детектор", exc)
        return None


def _detect_webrtc(path: Path, opts: dict[str, Any]) -> list[SpeechSegment] | None:
    try:
        import webrtcvad  # type: ignore
    except ModuleNotFoundError:
        return None
    try:
        import wave

        aggressiveness = min(3, max(0, int(float(opts.get("vad_threshold", 0.5)) * 4)))
        vad = webrtcvad.Vad(aggressiveness)
        with wave.open(str(path), "rb") as wf:
            rate = wf.getframerate()
            if rate not in (8000, 16000, 32000, 48000):
                return None
            frames = wf.readframes(wf.getnframes())
        step_ms = 30
        step = int(rate * step_ms / 1000) * 2
        flags: list[bool] = []
        for offset in range(0, len(frames) - step, step):
            chunk = frames[offset:offset + step]
            try:
                flags.append(vad.is_speech(chunk, rate))
            except Exception:
                flags.append(False)
        return _flags_to_segments(flags, step_ms / 1000.0, opts)
    except Exception as exc:
        log.debug("WebRTC VAD недоступен: %s", exc)
        return None


def _detect_energy(path: Path, opts: dict[str, Any]) -> list[SpeechSegment]:
    """Энергетический детектор без внешних зависимостей.

    Порог берётся адаптивно от медианной энергии кадров, поэтому детектор
    сносно работает и на тихих, и на громких записях.
    """
    samples, rate = load_samples(path)
    frame_ms = 30
    frame = max(1, int(rate * frame_ms / 1000))
    energies: list[float] = []
    try:
        import numpy as np  # type: ignore

        arr = np.asarray(samples, dtype="float32")
        usable = (len(arr) // frame) * frame
        if usable == 0:
            return []
        blocks = arr[:usable].reshape(-1, frame)
        energies = np.sqrt((blocks ** 2).mean(axis=1)).tolist()
    except ModuleNotFoundError:
        for offset in range(0, len(samples) - frame, frame):
            block = samples[offset:offset + frame]
            energies.append(math.sqrt(sum(v * v for v in block) / len(block)))
    if not energies:
        return []

    ordered = sorted(energies)
    n = len(ordered)
    floor = ordered[max(0, int(n * 0.10))]          # уровень шума
    peak = ordered[min(n - 1, int(n * 0.95))]       # уровень речи
    sensitivity = float(opts.get("vad_threshold", 0.5))

    # Порог ставим между шумом и пиком. Доля зависит от чувствительности:
    # чем выше vad_threshold, тем строже детектор.
    span = peak - floor
    if span > peak * 0.15:
        # Есть выраженный контраст шум/речь — обычная запись.
        threshold = floor + span * (0.15 + 0.45 * sensitivity)
    else:
        # Сигнал почти равномерный (сплошная речь, тон, шум).
        # Медианный порог здесь отбросил бы всё, поэтому опираемся на пик.
        threshold = peak * 0.25
    threshold = max(threshold, 1e-4)

    flags = [e >= threshold for e in energies]
    if not any(flags) and peak > 3e-3:
        # Предохранитель: в записи есть сигнал, но порог его не пропустил.
        log.debug("Энергетический VAD не нашёл речи при пике %.4f — снижаем порог", peak)
        flags = [e >= peak * 0.1 for e in energies]
    return _flags_to_segments(flags, frame_ms / 1000.0, opts)


def _flags_to_segments(flags: Sequence[bool], step_s: float,
                       opts: dict[str, Any]) -> list[SpeechSegment]:
    min_speech = float(opts.get("vad_min_speech_ms", 250)) / 1000.0
    min_silence = float(opts.get("vad_min_silence_ms", 500)) / 1000.0
    pad = float(opts.get("vad_speech_pad_ms", 200)) / 1000.0
    max_speech = float(opts.get("vad_max_speech_s", 22.0))

    segments: list[SpeechSegment] = []
    start: float | None = None
    silence_run = 0.0
    for idx, is_speech in enumerate(flags):
        t = idx * step_s
        if is_speech:
            if start is None:
                start = t
            silence_run = 0.0
        else:
            if start is not None:
                silence_run += step_s
                if silence_run >= min_silence:
                    end = t - silence_run + step_s
                    if end - start >= min_speech:
                        segments.append(SpeechSegment(start, end))
                    start = None
                    silence_run = 0.0
    if start is not None:
        end = len(flags) * step_s
        if end - start >= min_speech:
            segments.append(SpeechSegment(start, end))

    total = len(flags) * step_s
    padded: list[SpeechSegment] = []
    for seg in segments:
        padded.append(SpeechSegment(max(0.0, seg.start - pad), min(total, seg.end + pad)))

    merged: list[SpeechSegment] = []
    for seg in padded:
        if merged and seg.start <= merged[-1].end:
            merged[-1] = SpeechSegment(merged[-1].start, max(merged[-1].end, seg.end))
        else:
            merged.append(seg)

    return _enforce_max_length(merged, max_speech)


def _enforce_max_length(segments: list[SpeechSegment], max_len: float) -> list[SpeechSegment]:
    if max_len <= 0:
        return segments
    out: list[SpeechSegment] = []
    for seg in segments:
        if seg.duration <= max_len:
            out.append(seg)
            continue
        parts = int(math.ceil(seg.duration / max_len))
        step = seg.duration / parts
        for i in range(parts):
            out.append(SpeechSegment(seg.start + i * step,
                                     min(seg.end, seg.start + (i + 1) * step)))
    return out


def detect(path: Path, opts: dict[str, Any]) -> list[SpeechSegment]:
    """Находит участки речи выбранным или доступным детектором."""
    backend = str(opts.get("vad_backend") or "auto")
    order: list[str]
    if backend == "auto" or backend == "silero":
        order = ["silero", "webrtc", "energy"]
    elif backend == "webrtc":
        order = ["webrtc", "energy"]
    elif backend == "ten":
        order = ["ten", "silero", "webrtc", "energy"]
    else:
        order = ["energy"]

    for name in order:
        if name == "silero":
            result = _detect_silero(path, opts)
        elif name == "webrtc":
            result = _detect_webrtc(path, opts)
        elif name == "ten":
            result = _detect_ten(path, opts)
        else:
            result = _detect_energy(path, opts)
        if result is not None:
            if name != backend and backend not in ("auto", ""):
                log.info("Детектор «%s» недоступен, использован «%s»", backend, name)
            return result
    return []


def _detect_ten(path: Path, opts: dict[str, Any]) -> list[SpeechSegment] | None:
    try:
        from ten_vad import TenVad  # type: ignore
    except ModuleNotFoundError:
        return None
    try:
        samples, rate = load_samples(path)
        hop = 256
        vad = TenVad(hop_size=hop, threshold=float(opts.get("vad_threshold", 0.5)))
        flags: list[bool] = []
        try:
            import numpy as np  # type: ignore

            arr = (np.asarray(samples, dtype="float32") * 32767).astype("int16")
        except ModuleNotFoundError:
            arr = [int(v * 32767) for v in samples]
        for offset in range(0, len(arr) - hop, hop):
            _, flag = vad.process(arr[offset:offset + hop])
            flags.append(bool(flag))
        return _flags_to_segments(flags, hop / rate, opts)
    except Exception as exc:
        log.debug("TEN VAD недоступен: %s", exc)
        return None


def chunk_plan(duration_s: float, opts: dict[str, Any],
               segments: list[SpeechSegment] | None = None) -> list[SpeechSegment]:
    """Итоговый план нарезки: по речи, если VAD дал результат, иначе механически."""
    max_len = float(opts.get("vad_max_speech_s") or 22.0)
    overlap = float(opts.get("chunk_overlap_s") or 0.0)

    if segments:
        merged = _merge_adjacent(segments, max_len, gap=0.35)
        return merged

    chunk = float(opts.get("chunk_length_s") or 0) or max_len
    step = max(1.0, chunk - overlap)
    plan: list[SpeechSegment] = []
    position = 0.0
    while position < duration_s:
        plan.append(SpeechSegment(position, min(duration_s, position + chunk)))
        position += step
    return plan


def _merge_adjacent(segments: list[SpeechSegment], max_len: float,
                    gap: float = 0.35) -> list[SpeechSegment]:
    """Склеивает соседние короткие участки, не превышая предел длины.

    Это заметно ускоряет распознавание: одно обращение к модели на 20 секунд
    дешевле, чем двадцать обращений по секунде.
    """
    if not segments:
        return []
    out = [SpeechSegment(segments[0].start, segments[0].end)]
    for seg in segments[1:]:
        last = out[-1]
        if (seg.start - last.end <= gap) and (seg.end - last.start <= max_len):
            out[-1] = SpeechSegment(last.start, seg.end)
        else:
            out.append(SpeechSegment(seg.start, seg.end))
    return out


def speech_statistics(segments: list[SpeechSegment], total_duration: float) -> dict[str, float]:
    speech = sum(s.duration for s in segments)
    return {
        "segments": len(segments),
        "speech_seconds": round(speech, 2),
        "silence_seconds": round(max(0.0, total_duration - speech), 2),
        "speech_ratio": round(speech / total_duration, 4) if total_duration else 0.0,
        "avg_segment_s": round(speech / len(segments), 2) if segments else 0.0,
        "longest_segment_s": round(max((s.duration for s in segments), default=0.0), 2),
    }


def ensure_speech(segments: list[SpeechSegment], duration: float) -> None:
    if not segments:
        raise NoSpeechDetected(
            f"В записи длительностью {duration:.1f} с не найдено речи.")
