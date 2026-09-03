"""Проверки конвейера: аудио, VAD, постобработка, экспорт, метрики."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from asrhub.pipeline import audio, export, metrics, postprocess, vad

# --- аудио -----------------------------------------------------------------

def test_probe_reads_wav(sample_wav: Path):
    info = audio.probe(sample_wav)
    assert info.duration_s > 4.0
    assert info.sample_rate == 16000
    assert info.channels == 1


def test_probe_rejects_missing_file(tmp_path: Path):
    from asrhub.errors import AudioError

    with pytest.raises(AudioError):
        audio.probe(tmp_path / "нет-такого.wav")


def test_prepare_converts(sample_wav: Path, tmp_path: Path):
    outputs = audio.prepare(sample_wav, tmp_path, {
        "audio_sample_rate": 16000, "audio_channels": "mono",
        "audio_normalize": False, "audio_trim_silence": False,
    })
    assert len(outputs.channels) == 1
    label, path = outputs.channels[0]
    assert path.exists() and path.stat().st_size > 1000
    assert not outputs.shifted, "без обрезки и смены темпа сдвига быть не должно"


def test_file_hash_stable(sample_wav: Path):
    assert audio.file_hash(sample_wav) == audio.file_hash(sample_wav)


def test_slice_wav(sample_wav: Path, tmp_path: Path):
    target = tmp_path / "piece.wav"
    audio.slice_wav(sample_wav, target, 0.5, 1.5)
    assert 0.9 < audio.probe(target).duration_s < 1.1


# --- VAD -------------------------------------------------------------------

def test_vad_finds_two_segments(sample_wav: Path):
    options = {"vad_backend": "energy", "vad_threshold": 0.5,
               "vad_min_speech_ms": 250, "vad_min_silence_ms": 400,
               "vad_speech_pad_ms": 100, "vad_max_speech_s": 22.0}
    segments = vad.detect(sample_wav, options)
    assert len(segments) >= 2, "детектор должен разделить запись по паузе"
    assert all(s.duration > 0.2 for s in segments)


def test_vad_respects_max_length(sample_wav: Path):
    options = {"vad_backend": "energy", "vad_threshold": 0.4,
               "vad_min_speech_ms": 100, "vad_min_silence_ms": 300,
               "vad_speech_pad_ms": 50, "vad_max_speech_s": 1.0}
    segments = vad.detect(sample_wav, options)
    assert all(s.duration <= 1.05 for s in segments)


def test_chunk_plan_without_vad():
    plan = vad.chunk_plan(100.0, {"vad_max_speech_s": 20.0, "chunk_overlap_s": 2.0}, None)
    assert len(plan) >= 5
    assert plan[0].start == 0.0


# --- постобработка ----------------------------------------------------------

def test_hallucination_filter():
    assert postprocess.is_hallucination("Спасибо за просмотр")
    assert postprocess.is_hallucination("Субтитры сделал DimaTorzok")
    assert postprocess.is_hallucination("да да да да да да")
    assert not postprocess.is_hallucination("Коллеги, начнём совещание")


def test_filler_removal():
    result = postprocess.remove_fillers("ну в общем это самое мы решили")
    assert "ну" not in result.lower().split()
    assert "решили" in result


def test_itn_russian():
    assert "25" in postprocess.apply_itn("двадцать пять процентов", "auto", "ru")
    assert "100" in postprocess.apply_itn("сто рублей", "auto", "ru")


def test_glossary_replacement():
    text, count = postprocess.apply_glossary("используем кубернетес", {"кубернетес": "Kubernetes"})
    assert text == "используем Kubernetes" and count == 1


def test_glossary_regex():
    text, count = postprocess.apply_glossary(
        "версия 3 точка 2", {r"re:версия (\d+) точка (\d+)": r"версия \1.\2"})
    assert "3.2" in text and count == 1


def test_profanity_masking():
    text, hits = postprocess.filter_profanity("это полная хуйня", "mask")
    assert hits == 1 and "*" in text


def test_merge_short_segments():
    segments = [
        {"start": 0.0, "end": 0.8, "text": "Да.", "speaker": "S1"},
        {"start": 0.9, "end": 1.4, "text": "Согласен.", "speaker": "S1"},
        {"start": 5.0, "end": 9.0, "text": "Другая мысль.", "speaker": "S2"},
    ]
    merged = postprocess.merge_segments(segments, min_duration=1.5)
    assert len(merged) == 2


def test_full_postprocess_pipeline():
    segments = [
        {"start": 0, "end": 3, "text": "ну у нас двадцать пять процентов роста", "speaker": "S1"},
        {"start": 3, "end": 4, "text": "Спасибо за просмотр", "speaker": "S1"},
    ]
    result, stats = postprocess.process(segments, {
        "language": "ru", "hallucination_filter": True, "remove_filler_words": True,
        "punctuation_enabled": False, "itn_enabled": True, "glossary": {},
        "merge_short_segments": False,
    })
    assert stats["hallucinations_removed"] == 1
    assert len(result) == 1
    assert "25" in result[0]["text"]


# --- метрики ----------------------------------------------------------------

def test_wer_identical():
    assert metrics.wer("привет мир", "привет мир") == 0.0


def test_wer_counts_errors():
    value = metrics.wer("один два три четыре", "один два три")
    assert abs(value - 0.25) < 1e-9


def test_detailed_diff():
    detail = metrics.detailed("кот сидел на окне", "кот сидит на окне")
    assert detail["words"]["substitutions"] == 1
    assert any(item["op"] == "sub" for item in detail["diff"])


def test_normalization_ignores_case_and_punctuation():
    assert metrics.wer("Привет, мир!", "привет мир") == 0.0


def test_percentiles():
    summary = metrics.summarize([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert summary["p50"] == 5.5
    assert summary["max"] == 10


# --- экспорт ----------------------------------------------------------------

@pytest.fixture()
def result_payload():
    return {
        "meta": {"filename": "тест.mp3", "model": "gigaam-v3-rnnt",
                 "language": "ru", "duration_s": 12.0},
        "segments": [
            {"start": 0.0, "end": 4.0, "text": "Первая реплика.", "speaker": "Иванов",
             "confidence": 0.96},
            {"start": 4.5, "end": 9.0, "text": "Вторая реплика немного длиннее.",
             "speaker": "Петрова", "confidence": 0.88},
        ],
        "metrics": {"rtf": 0.12, "segments": 2, "words": 7},
    }


def test_export_all_formats(result_payload, tmp_path: Path):
    settings = {"output_formats": ["txt", "json", "srt", "vtt", "ass", "csv", "tsv", "md"],
                "include_speaker_labels": True, "include_confidence": True,
                "word_timestamps": True, "paragraph_mode": "speaker",
                "subtitle_max_line_width": 42, "subtitle_max_lines": 2,
                "subtitle_min_duration_s": 1.0}
    written = export.write_all(result_payload, settings, tmp_path, "тест")
    assert set(written) == set(settings["output_formats"])
    for path in written.values():
        assert Path(path).stat().st_size > 10


def test_srt_timestamps(result_payload):
    text = export.to_srt(result_payload, {"subtitle_max_line_width": 42,
                                          "subtitle_max_lines": 2,
                                          "subtitle_min_duration_s": 1.0,
                                          "include_speaker_labels": False})
    assert "00:00:00,000 --> 00:00:04,000" in text


def test_subtitles_do_not_overlap(result_payload):
    payload = dict(result_payload)
    payload["segments"] = [
        {"start": 0.0, "end": 0.2, "text": "Да."},
        {"start": 0.3, "end": 0.5, "text": "Нет."},
    ]
    prepared = export._prepare_subtitles(payload, 1.0)
    assert prepared[0]["end"] <= prepared[1]["start"]


def test_json_export_is_valid(result_payload):
    payload = json.loads(export.to_json(result_payload, {"include_confidence": True,
                                                         "word_timestamps": True}))
    assert payload["segments"][0]["text"] == "Первая реплика."


def test_subtitle_wrapping():
    """Разбивка на строки не теряет текст.

    Раньше лишние строки просто отрезались вместе со словами: из реплики в
    пятнадцать слов до субтитра доходило четырнадцать. Теперь при нехватке
    места строка становится длиннее заданной ширины — это заметно, но
    поправимо, в отличие от бесследно пропавшего текста.
    """
    text = export.wrap_subtitle("а" * 100, 42, 2)
    lines = text.split("\n")
    assert len(lines) <= 2
    assert sum(len(line) for line in lines) >= 100, "часть текста потерялась"

    phrase = ("Мы обсудили условия поставки и договорились перенести отгрузку "
              "на следующий понедельник, потому что склад закрыт")
    wrapped = export.wrap_subtitle(phrase, 42, 2)
    assert wrapped.replace("\n", " ").split() == phrase.split()
    assert len(wrapped.split("\n")) == 2


# ---------------------------------------------------------------------------
# Регрессии ревизии: показатели в выгрузке и детектор речи
# ---------------------------------------------------------------------------

def test_exported_metrics_are_not_zero(tmp_path: Path, sample_wav: Path):
    """В выгруженные файлы должны попадать настоящие показатели.

    to_result читает self.timings, а замеры проставлялись после записи
    файлов: в каждый выгруженный json уходили «rtf: 0.0» и
    «processing_time_s: 0», тогда как в интерфейсе по тому же заданию стояли
    настоящие значения. Расхождение выглядело как порча данных при выгрузке.
    """
    import json

    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.processor import process_job

    settings = load().merged({"model": "demo-simulator", "engine": "demo",
                              "vad_backend": "energy",
                              "output_formats": ["json", "txt"]})
    outcome = process_job(sample_wav, settings, EngineRegistry(),
                          workdir=tmp_path / "wd", outdir=tmp_path / "out",
                          basename="проба")

    written = json.loads((tmp_path / "out" / "проба.json").read_text(encoding="utf-8"))
    metrics = written["metrics"]
    assert metrics["rtf"] > 0, "в файл записан нулевой RTF"
    assert metrics["processing_time_s"] > 0, "в файл записано нулевое время"
    assert any(k.startswith("stage_") for k in metrics), "нет разбивки по стадиям"
    # И то, что уходит в базу, тоже заполнено.
    assert outcome.timings, "замеры не проставлены в результат"


def test_energy_vad_does_not_call_silence_speech(tmp_path: Path):
    """Детектор не должен объявлять речью почти тихую запись.

    Пик брался как 95-я перцентиль энергии. Если речи меньше пяти процентов,
    в перцентиль попадал шум: контраст схлопывался, порог вставал ниже
    шумовой полки, и час тишины уходил в движок целиком — со счётом за
    вычисления и галлюцинациями Whisper на тишине.
    """
    import math
    import random
    import struct
    import wave

    from asrhub.pipeline import vad

    path = tmp_path / "почти-тишина.wav"
    rate, seconds = 16000, 60
    random.seed(7)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = []
        for i in range(seconds * rate):
            t = i / rate
            value = random.gauss(0, 30)
            if 29.0 <= t < 31.0:                       # две секунды речи
                value += 9000 * math.sin(2 * math.pi * 220 * t)
            frames.append(struct.pack("<h", max(-32768, min(32767, int(value)))))
        w.writeframes(b"".join(frames))

    segments = vad.detect(path, {"vad_backend": "energy", "vad_threshold": 0.5})
    found = sum(s.end - s.start for s in segments)
    assert found < seconds * 0.25, \
        f"детектор объявил речью {found:.0f} с из {seconds} при двух секундах речи"
    assert found > 0.5, "речь потеряна совсем"


def test_alignment_survives_word_count_mismatch():
    """Границы не должны разъезжаться, когда счёт слов не совпадает.

    _redistribute резал плоский список выровненных слов по числу слов в
    тексте сегмента. Совпадение не гарантировано: MFA чистит текст и «из-за»
    приходит двумя словами, WhisperX выбрасывает слова без таймкодов.
    Расхождение в одно слово сдвигало границы всех последующих сегментов, и
    сдвиг накапливался до конца записи.
    """
    from asrhub.engines.base import Segment
    from asrhub.pipeline.alignment import _redistribute

    def word(text: str, start: float, end: float) -> dict:
        return {"word": text, "start": start, "end": end, "score": 0.9}

    # MFA разбил «Из-за» и «чего-то» — восемь слов вместо шести по счёту.
    aligned = [word("из", 0.0, 0.4), word("за", 0.4, 0.8),
               word("чего", 0.8, 1.2), word("то", 1.2, 1.5),
               word("встреча", 1.5, 2.4), word("сдвинулась", 2.4, 3.9),
               word("обсудим", 4.0, 4.8), word("бюджет", 4.8, 5.9)]
    segments = [Segment(start=0, end=0, text="Из-за чего-то встреча сдвинулась."),
                Segment(start=0, end=0, text="Обсудим бюджет.")]
    out = _redistribute(segments, aligned, {"alignment_keep_text": True})
    assert round(out[0].end, 2) == 3.9, "границу первого сегмента сдвинуло"
    assert round(out[1].start, 2) == 4.0, "второй сегмент забрал чужие слова"

    # WhisperX потерял слово «дивный».
    aligned = [word("привет", 0.0, 0.5), word("мир", 0.5, 1.0),
               word("как", 2.0, 2.3), word("дела", 2.6, 3.0)]
    segments = [Segment(start=0, end=0, text="Привет, дивный мир."),
                Segment(start=0, end=0, text="Как дела?")]
    out = _redistribute(segments, aligned, {"alignment_keep_text": True})
    assert round(out[0].end, 2) == 1.0, "потерянное слово утащило чужую границу"
    assert round(out[1].start, 2) == 2.0

    # Текст остаётся исходным — со знаками препинания и заглавными.
    assert out[0].text.startswith("Привет")


def test_diarization_fallback_is_reported():
    """Подмена диаризации разбивкой по паузам не должна быть молчаливой.

    Если явно выбранный механизм падал не на отсутствии зависимости, ошибка
    гасилась и говорящие расставлялись по паузам. В протоколе совещания это
    неотличимо от настоящей диаризации — ни в интерфейсе, ни в выгрузке.
    """
    import wave

    from asrhub.engines.base import Segment
    from asrhub.pipeline import diarization

    tmp = Path(__file__).resolve().parent / "_diar.wav"
    with wave.open(str(tmp), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0" * 32000)

    original = diarization._pyannote
    diarization._pyannote = lambda *a, **kw: (_ for _ in ()).throw(
        RuntimeError("CUDA out of memory"))
    try:
        segments = [Segment(start=0, end=3, text="Первая"),
                    Segment(start=3.1, end=6, text="Вторая"),
                    Segment(start=9, end=12, text="Третья")]
        warnings: list[str] = []
        result = diarization.diarize_segments(
            tmp, segments, {"diarization_backend": "pyannote"}, warnings)
    finally:
        diarization._pyannote = original
        tmp.unlink(missing_ok=True)

    assert all(s.speaker for s in result), "говорящие всё же должны быть расставлены"
    assert warnings, "подмена прошла молча"
    assert "по паузам" in warnings[0], warnings[0]


def test_postprocess_cache_does_not_grow_per_language():
    """Кеш моделей постобработки не должен расти по числу языков.

    Модель пунктуации от языка не зависит, а ключ кеша его содержал: при
    `language: auto` и разноязычном потоке каждый новый язык добавлял ещё
    одну полную копию того же трансформера.
    """
    from asrhub.pipeline import postprocess

    postprocess._PUNCT_CACHE.clear()
    postprocess._ITN_CACHE.clear()
    for language in ("ru", "en", "de", "fr", "es", "it", "pt", "nl"):
        postprocess._load_punctuator("multilingual", language)
        postprocess._load_itn("nemo", language)

    assert len(postprocess._PUNCT_CACHE) == 1, \
        f"на восемь языков заведено {len(postprocess._PUNCT_CACHE)} моделей пунктуации"
    assert len(postprocess._ITN_CACHE) <= postprocess._ITN_CACHE_LIMIT, \
        f"кеш нормализаторов вырос до {len(postprocess._ITN_CACHE)}"

    # «Выгрузить всё» должно освобождать и этот кеш.
    postprocess._PUNCT_CACHE["x"] = None
    assert postprocess.unload_text_models() >= 1
    assert not postprocess._PUNCT_CACHE and not postprocess._ITN_CACHE
