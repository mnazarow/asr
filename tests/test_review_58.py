"""Заход 58: проверка слоя ИИ-аналитики.

Найдено и исправлено:

- модель получала разговор без подписи «Сотрудник», если метка оператора
  не задана настройкой (по умолчанию она пуста), хотя разбор содержания
  оператора уже определил — вопросы скоркарты о поведении сотрудника модель
  решала угадыванием;
- собеседник оператора оставался «SPEAKER_01», а не «Клиент»;
- реплика длиннее предела куска резалась посреди слова.
"""

from __future__ import annotations

import time
from pathlib import Path

from asrhub.llm import tasks
from asrhub.llm.client import LLMClient
from asrhub.llm.worker import LLMWorker

КОРЕНЬ = Path(__file__).resolve().parents[1]


class _Настройки:
    ПО_УМОЛЧАНИЮ = {
        "llm_enabled": True, "llm_backend": "stub", "llm_model": "stub",
        "llm_url": "", "llm_api_key": "", "llm_timeout_s": 30,
        "llm_temperature": 0.0, "llm_max_tokens": 800,
        "llm_context_chars": 12000, "llm_min_free_vram_gb": 0, "llm_auto": True,
        "llm_backfill": False, "llm_yield_to_queue": True,
        "llm_tasks": ["summary"], "llm_reasons": ["другое"],
        "llm_outcomes": ["неясно"], "llm_trackers": [], "llm_scorecard": [],
        "content_script": [], "content_agent_speaker": "",
    }

    def __init__(self, **значения):
        self.значения = {**self.ПО_УМОЛЧАНИЮ, **значения}

    def get(self, ключ, по_умолчанию=None):
        return self.значения.get(ключ, по_умолчанию)


# ---------------------------------------------------------------------------
# Подписи сторон в расшифровке для модели
# ---------------------------------------------------------------------------


def test_the_only_other_speaker_is_labelled_as_the_client():
    разговор = [
        {"speaker": "SPEAKER_01", "text": "Алло, это служба доставки?"},
        {"speaker": "SPEAKER_00", "text": "Да, слушаю вас."},
        {"speaker": "SPEAKER_01", "text": "Заказ не пришёл."},
    ]
    текст = tasks.transcript_of(разговор, "", agent_speaker="SPEAKER_00")
    assert текст.split("\n") == ["Клиент: Алло, это служба доставки?",
                                 "Сотрудник: Да, слушаю вас.",
                                 "Клиент: Заказ не пришёл."]
    # Оператор не назван — подписи как есть: угадывать здесь нечего.
    assert tasks.transcript_of(разговор, "").startswith("SPEAKER_01: Алло")


def test_a_conference_keeps_the_other_labels():
    """Трое и больше (перевод, конференция) — «Клиентом» никого не называем."""
    разговор = [
        {"speaker": "SPEAKER_00", "text": "Соединяю с отделом."},
        {"speaker": "SPEAKER_01", "text": "Жду."},
        {"speaker": "SPEAKER_02", "text": "Отдел доставки, слушаю."},
    ]
    строки = tasks.transcript_of(разговор, "", agent_speaker="SPEAKER_00").split("\n")
    assert строки == ["Сотрудник: Соединяю с отделом.", "SPEAKER_01: Жду.",
                      "SPEAKER_02: Отдел доставки, слушаю."]


def test_a_single_voice_is_not_labelled_as_the_employee():
    """Диаризации не было — всё «SPEAKER_00»: ролей нет, подписи как были."""
    разговор = [{"speaker": "SPEAKER_00", "text": "Здравствуйте."},
                {"speaker": "SPEAKER_00", "text": "Хочу вернуть товар."}]
    текст = tasks.transcript_of(разговор, "", agent_speaker="SPEAKER_00")
    assert "Сотрудник" not in текст and текст.startswith("SPEAKER_00: ")


# ---------------------------------------------------------------------------
# Нарезка длинной реплики
# ---------------------------------------------------------------------------


def test_a_long_reply_is_cut_between_words():
    слова = [f"слово{i}" for i in range(1000)]
    строка = "Клиент: " + " ".join(слова)
    куски = tasks.chunks_of(строка, 600)          # предел не меньше 500 знаков
    assert len(куски) > 5 and all(len(к) <= 600 for к in куски)
    # Ни одно слово не разрезано: склейка по пробелу даёт исходные слова.
    assert " ".join(куски).split() == строка.split()
    assert all(not к.startswith(" ") and not к.endswith(" ") for к in куски)


def test_a_reply_without_spaces_is_still_cut_by_the_limit():
    """Пробела в первой половине куска нет — режется по пределу, а не теряется."""
    строка = "Клиент: " + "а" * 3000
    куски = tasks.chunks_of(строка, 600)
    assert len(куски) > 4 and all(len(к) <= 600 for к in куски)
    assert "".join(куски).replace(" ", "") == строка.replace(" ", "")


# ---------------------------------------------------------------------------
# Кто оператор для языковой модели
# ---------------------------------------------------------------------------


def _архив(tmp_path, *, source: str = "", params: dict | None = None,
           реплики: list[dict] | None = None):
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    поля = {"id": "j1", "filename": "1.wav", "owner": "анна", "model": "demo",
            "engine": "demo", "media_duration_s": 30.0}
    if source:
        поля["source"] = source
    if params is not None:
        поля["params"] = params
    db.create_job(поля)
    db.update_job("j1", status="completed", finished_at=time.time(),
                  text="Здравствуйте. Заказ не пришёл.")
    db.save_segments("j1", реплики or [
        {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00", "text": "Здравствуйте, слушаю."},
        {"start": 4.0, "end": 8.0, "speaker": "SPEAKER_01", "text": "Заказ не пришёл."}])
    return db


def _поток(db, **настройки):
    н = _Настройки(**настройки)
    return LLMWorker(db, н, LLMClient(н, db))


def _подписи(поток, monkeypatch) -> list[str]:
    """Какую метку оператора worker отдал в разбор."""
    отданные: list[str] = []
    исходный = tasks.analyze

    def ловушка(*a, **k):
        отданные.append(k.get("agent_speaker"))
        return исходный(*a, **k)

    monkeypatch.setattr("asrhub.llm.worker.tasks.analyze", ловушка)
    поток.analyze_job("j1", force=True)
    return отданные


def test_the_model_gets_the_agent_found_by_content_analysis(tmp_path, monkeypatch):
    """Настройка пуста, разбор содержания решил, что оператор — SPEAKER_01
    (например, клиент заговорил первым): модель получает ту же метку."""
    db = _архив(tmp_path)
    db.save_content("j1", {"version": 1, "agent_speaker": "SPEAKER_01"})
    assert _подписи(_поток(db), monkeypatch) == ["SPEAKER_01"]


def test_without_content_analysis_the_first_speaker_is_the_agent(tmp_path, monkeypatch):
    db = _архив(tmp_path)
    assert db.get_content("j1") is None
    assert _подписи(_поток(db), monkeypatch) == ["SPEAKER_00"]


def test_the_setting_still_wins_for_calls(tmp_path, monkeypatch):
    db = _архив(tmp_path)
    db.save_content("j1", {"version": 1, "agent_speaker": "SPEAKER_00"})
    поток = _поток(db, content_agent_speaker="SPEAKER_01")
    assert _подписи(поток, monkeypatch) == ["SPEAKER_01"]


def test_a_chat_takes_the_side_named_at_intake(tmp_path, monkeypatch):
    """У переписки настройка с «SPEAKER_00» ни при чём: оператор — сторона,
    названная при приёме, как у разбора содержания."""
    реплики = [{"start": 0.0, "end": 0.0, "speaker": "Клиент", "text": "Где мой заказ?"},
               {"start": 0.0, "end": 0.0, "speaker": "Мария", "text": "Сейчас проверю."}]
    db = _архив(tmp_path, source="text", params={"agent": "Мария"}, реплики=реплики)
    поток = _поток(db, content_agent_speaker="SPEAKER_00")
    assert _подписи(поток, monkeypatch) == ["Мария"]
    # И в расшифровке для модели стороны подписаны ролями.
    assert tasks.transcript_of(реплики, "", agent_speaker="Мария").split("\n") == [
        "Клиент: Где мой заказ?", "Сотрудник: Сейчас проверю."]


# ---------------------------------------------------------------------------
# Обезличивание перед моделью (llm_mask_pii)
# ---------------------------------------------------------------------------


class _Запоминающий:
    """Клиент модели, который запоминает подсказки и отвечает резюме."""

    model = "проба"

    def __init__(self):
        self.подсказки: list[str] = []

    def chat(self, система, подсказка, *, kind, validate=None, **_):
        self.подсказки.append(подсказка)
        return '{"summary": "Клиент продиктовал номер.", "resolved": null}'


_С_НОМЕРАМИ = [
    {"speaker": "SPEAKER_00", "text": "Слушаю вас."},
    {"speaker": "SPEAKER_01",
     "text": "Мой номер +7 912 345-67-89, карта 4111 1111 1111 1111."},
]


def test_the_model_does_not_see_card_and_phone_numbers():
    клиент = _Запоминающий()
    tasks.analyze(клиент, text="", segments=_С_НОМЕРАМИ,
                  settings=_Настройки(), agent_speaker="SPEAKER_00")
    (подсказка,) = клиент.подсказки
    assert "4111" not in подсказка and "912" not in подсказка
    assert "[карта]" in подсказка and "[телефон]" in подсказка
    assert "Клиент: Мой номер [телефон]" in подсказка


def test_masking_for_the_model_can_be_turned_off():
    клиент = _Запоминающий()
    tasks.analyze(клиент, text="", segments=_С_НОМЕРАМИ,
                  settings=_Настройки(llm_mask_pii=False), agent_speaker="SPEAKER_00")
    assert "4111 1111 1111 1111" in клиент.подсказки[0]


def test_the_option_is_in_the_catalog_and_on_by_default():
    from asrhub.catalog.params import PARAMS

    параметр = next(п for п in PARAMS if п.key == "llm_mask_pii")
    assert параметр.default is True and параметр.group == "llm"
    assert "llm_mask_pii" in (КОРЕНЬ / "docs" / "04-parameters.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Глава 35
# ---------------------------------------------------------------------------


def test_the_ai_analytics_chapter_is_in_the_manual():
    глава = (КОРЕНЬ / "docs" / "35-ai-analytics.md").read_text(encoding="utf-8")
    assert глава.startswith("# 35. ")
    for раздел in ("## Что уже есть", "## Что нашла проверка", "## Что можно сделать"):
        assert раздел in глава, раздел
    assert "35-ai-analytics.md" in (КОРЕНЬ / "docs" / "00-README.md").read_text(encoding="utf-8")
    assert "35-ai-analytics.md" in (КОРЕНЬ / "docs" / "assemble.py").read_text(encoding="utf-8")
