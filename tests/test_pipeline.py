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
    assert len(outputs) == 1
    label, path = outputs[0]
    assert path.exists() and path.stat().st_size > 1000


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
    text = export.wrap_subtitle("а" * 100, 42, 2)
    assert all(len(line) <= 55 for line in text.split("\n"))
