"""Проверки принудительного выравнивания.

Сам MFA в тестовой среде не установлен, поэтому проверяется то, что от него не
зависит: раскладка выровненных слов по сегментам, сохранение исходного текста,
склейка и поведение при недоступном выравнивателе.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from asrhub.engines.base import Segment
from asrhub.errors import DependencyMissing
from asrhub.pipeline import alignment


class Settings(dict):
    """Минимальная замена настроек: в конвейере используется только .get."""

    def get(self, key, default=None):          # type: ignore[override]
        return super().get(key, default)


def make_segments():
    return [
        Segment(start=0.0, end=2.0, text="Здравствуйте, коллеги."),
        Segment(start=2.0, end=5.0, text="Начнём совещание сейчас."),
    ]


WORDS = [
    {"word": "здравствуйте", "start": 0.12, "end": 0.94},
    {"word": "коллеги", "start": 1.02, "end": 1.71},
    {"word": "начнём", "start": 2.30, "end": 2.85},
    {"word": "совещание", "start": 2.88, "end": 3.61},
    {"word": "сейчас", "start": 3.70, "end": 4.22},
]


def test_backend_none_returns_input_unchanged():
    segments = make_segments()
    assert alignment.align_segments(Path("нет.wav"), segments,
                                    Settings(alignment_backend="none")) is segments


def test_missing_backend_raises_dependency_error():
    with pytest.raises(DependencyMissing) as info:
        alignment.align_segments(Path("нет.wav"), make_segments(),
                                 Settings(alignment_backend="mfa"))
    assert "mfa" in info.value.hint.lower()


def test_unknown_backend_reports_clearly():
    with pytest.raises(DependencyMissing):
        alignment.align_segments(Path("нет.wav"), make_segments(),
                                 Settings(alignment_backend="телепатия"))


def test_words_are_distributed_by_segment_word_count():
    segments = alignment._redistribute(make_segments(), WORDS, Settings())
    assert len(segments) == 2
    assert [w["word"] for w in segments[0].words] == ["Здравствуйте,", "коллеги."]
    assert [w["word"] for w in segments[1].words] == ["Начнём", "совещание", "сейчас."]


def test_segment_bounds_follow_aligned_words():
    segments = alignment._redistribute(make_segments(), WORDS, Settings())
    assert segments[0].start == 0.12 and segments[0].end == 1.71
    assert segments[1].start == 2.3 and segments[1].end == 4.22


def test_original_text_is_preserved_by_default():
    """Выравниватель отдаёт текст без пунктуации — она не должна теряться."""
    segments = alignment._redistribute(make_segments(), WORDS, Settings())
    joined = " ".join(w["word"] for w in segments[0].words)
    assert joined == "Здравствуйте, коллеги."


def test_debug_mode_shows_aligner_words():
    segments = alignment._redistribute(make_segments(), WORDS,
                                       Settings(alignment_keep_text=False))
    assert [w["word"] for w in segments[0].words] == ["здравствуйте", "коллеги"]


def test_missing_words_leave_segment_untouched():
    """Если выравниватель вернул меньше слов, лишние сегменты не ломаются."""
    segments = alignment._redistribute(make_segments(), WORDS[:2], Settings())
    assert segments[0].words and not segments[1].words
    assert segments[1].start == 2.0 and segments[1].end == 5.0


def test_merge_close_segments():
    segments = alignment._redistribute(make_segments(), WORDS,
                                       Settings(alignment_max_gap_s=1.0))
    assert len(segments) == 1
    assert segments[0].text == "Здравствуйте, коллеги. Начнём совещание сейчас."
    assert len(segments[0].words) == 5


def test_merge_respects_speaker_boundaries():
    segments = make_segments()
    segments[0].speaker = "SPEAKER_00"
    segments[1].speaker = "SPEAKER_01"
    result = alignment._redistribute(segments, WORDS, Settings(alignment_max_gap_s=5.0))
    assert len(result) == 2, "реплики разных говорящих склеивать нельзя"


def test_merge_disabled_by_default():
    assert len(alignment._redistribute(make_segments(), WORDS, Settings())) == 2


def test_clean_for_mfa_strips_punctuation_and_case():
    assert alignment._clean_for_mfa("Здравствуйте, «коллеги» — начнём!") == \
        "здравствуйте коллеги начнём"


def test_alignment_params_are_in_catalog():
    from asrhub.catalog.params import PARAMS

    keys = {p.key for p in PARAMS}
    for key in ("alignment_backend", "alignment_dictionary", "alignment_acoustic_model",
                "alignment_max_gap_s", "alignment_keep_text"):
        assert key in keys, f"{key} отсутствует в каталоге параметров"
