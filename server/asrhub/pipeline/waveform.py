"""Огибающая громкости записи — по одному замеру на интервал.

Повторяет расчёт из phone_asr (`app/src/routes/asr.py:generate_waveform` и
`misc/main.py:generate_audio_waveform`): запись режется на интервалы по
секунде, и для каждого берётся **средний модуль отсчёта**. Это не пик и не
децибелы: величина безразмерная, в диапазоне 0…1, где тишина даёт около
0.001, а обычная речь — 0.15…0.25.

Средний модуль выбран там не случайно. Пик реагирует на один щелчок и делает
полосу нечитаемой, RMS завышает шипящие, а среднее по модулю даёт ровно то,
что глаз ждёт от «полоски громкости»: где говорят — выше, где молчат —
низко. Мы считаем так же, чтобы числа совпадали с теми, что уже рисует
принимающая сторона.

Разбивка тоже повторяет исходную логику:

* стереозапись — отдельная кривая на канал (у телефонии в разных каналах
  оператор и абонент);
* монозапись с диаризацией — отдельная кривая на говорящего;
* всё остальное — одна общая кривая с меткой «all».
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("waveform")

#: Метка кривой, когда разделить запись не на что.
ALL = "all"


def _envelope(samples: Any, sample_rate: int, interval_s: float) -> list[dict[str, float]]:
    """Средний модуль отсчёта по интервалам.

    Округления те же, что в phone_asr: время до трёх знаков, амплитуда до
    шести. Совпадение здесь важно — принимающая сторона сравнивает значения
    между собой и рисует по ним полосу.
    """
    step = max(1, int(round(sample_rate * interval_s)))
    points: list[dict[str, float]] = []

    try:
        import numpy as np  # type: ignore

        data = np.asarray(samples, dtype="float32")
        total = int(np.ceil(len(data) / step)) if len(data) else 0
        for index in range(total):
            segment = data[index * step:(index + 1) * step]
            if segment.size == 0:
                continue
            points.append({
                "time": round(float(index * interval_s), 3),
                "amplitude": round(float(np.mean(np.abs(segment))), 6),
            })
        return points
    except ModuleNotFoundError:
        pass

    # Запасной путь без numpy: медленнее, но результат тот же.
    values = list(samples)
    for start in range(0, len(values), step):
        segment = values[start:start + step]
        if not segment:
            continue
        points.append({
            "time": round(start / sample_rate, 3),
            "amplitude": round(sum(abs(v) for v in segment) / len(segment), 6),
        })
    return points


def _field(segment: Any, name: str, default: Any = None) -> Any:
    """Поле сегмента независимо от того, объект это или словарь.

    До постобработки сегменты — объекты Segment, после неё — словари.
    Полоса строится после постобработки (чтобы подписи совпадали с именами
    говорящих в расшифровке), но вызывать её с объектами тоже должно быть
    можно: обращение через getattr к словарю молча давало None, и кривые
    по говорящим переставали строиться вовсе.
    """
    if isinstance(segment, dict):
        return segment.get(name, default)
    return getattr(segment, name, default)


def _by_speakers(samples: Any, sample_rate: int, interval_s: float,
                 segments: list[Any]) -> dict[str, list[dict[str, float]]]:
    """Отдельная кривая на каждого говорящего.

    Вне своих реплик говорящий молчит, поэтому в его кривой там нули — так
    несколько полос, наложенных друг на друга, читаются как диалог: видно,
    кто когда говорил.
    """
    speakers: list[str] = []
    for segment in segments:
        name = _field(segment, "speaker")
        if name and name not in speakers:
            speakers.append(name)
    if len(speakers) < 2:
        return {}

    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError:
        # Без numpy раскладка по говорящим обошлась бы слишком дорого:
        # отдаём общую кривую, о чём сообщаем в журнал.
        log.debug("numpy недоступен — огибающая по говорящим не строится")
        return {}

    data = np.asarray(samples, dtype="float32")
    curves: dict[str, list[dict[str, float]]] = {}
    for speaker in speakers:
        masked = np.zeros_like(data)
        for segment in segments:
            if _field(segment, "speaker") != speaker:
                continue
            start = max(0, int(float(_field(segment, "start", 0.0)) * sample_rate))
            end = min(len(data), int(float(_field(segment, "end", 0.0)) * sample_rate))
            if end > start:
                masked[start:end] = data[start:end]
        curves[speaker] = _envelope(masked, sample_rate, interval_s)
    return curves


def build(channels: list[tuple[str, Path]], segments: list[Any],
          settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Строит огибающие для записи. Возвращает список кривых.

    Каждая кривая — словарь с полями `audio_waveform`, `sample_rate` и
    `speaker`: тот же набор, что отдаёт phone_asr, чтобы принимающая сторона
    работала без правок.
    """
    from . import audio as audio_mod

    interval = float(settings.get("waveform_interval_s") or 1.0)
    interval = min(60.0, max(0.05, interval))
    curves: list[dict[str, Any]] = []

    # Стереозапись, разложенная по каналам: кривая на канал, как в phone_asr,
    # где в левом канале оператор, а в правом абонент.
    if len(channels) > 1:
        for index, (label, path) in enumerate(channels):
            samples, rate = audio_mod.load_samples(path)
            curves.append({
                "audio_waveform": _envelope(samples, rate, interval),
                "sample_rate": rate,
                "speaker": index,
                "label": label or f"Канал {index + 1}",
            })
        return curves

    if not channels:
        return curves

    label, path = channels[0]
    samples, rate = audio_mod.load_samples(path)

    # Монозапись: если диаризация нашла нескольких говорящих, кривая на
    # каждого. Это то же намерение, что у phone_asr, где монозапись перед
    # расчётом принудительно разделяли диаризацией на два канала.
    per_speaker = _by_speakers(samples, rate, interval, segments)
    if per_speaker:
        for index, (speaker, points) in enumerate(per_speaker.items()):
            curves.append({
                "audio_waveform": points,
                "sample_rate": rate,
                "speaker": index,
                "label": speaker,
            })
        return curves

    curves.append({
        "audio_waveform": _envelope(samples, rate, interval),
        "sample_rate": rate,
        "speaker": ALL,
        "label": label or "Вся запись",
    })
    return curves


def to_phone_asr(curves: list[dict[str, Any]]) -> list[str]:
    """Кривые в том виде, в каком их отдаёт phone_asr: массив JSON-строк.

    В схеме phone_asr поле объявлено как `waveforms: list[str]`, а внутрь
    кладётся `json.dumps(...)` — то есть массив строк, а не объектов.
    Принимающая сторона разбирает каждый элемент отдельно, поэтому мы
    повторяем именно этот вид, включая порядок и набор полей.

    Поле `label` в совместимый вид не идёт: у phone_asr его нет, а лишний
    ключ ломает строгие разборщики.
    """
    return [
        json.dumps({
            "audio_waveform": curve["audio_waveform"],
            "sample_rate": curve["sample_rate"],
            "speaker": curve["speaker"],
        }, ensure_ascii=False)
        for curve in curves
    ]
