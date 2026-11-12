"""Заход 59 в браузере: новые блоки ИИ-аналитики открываются без ошибок."""
from __future__ import annotations

import pytest

from tests.drill_seed import Архив, Вкладка, браузер, поднять

pytestmark = pytest.mark.skipif(браузер() is None, reason="нет Chromium")


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    архив = Архив(tmp_path_factory.mktemp("z59web"), заданий=40)
    hub = архив.app.state.hub
    for ключ, значение in {
        "llm_backend": "stub", "llm_model": "stub", "llm_auto": False,
        "llm_tasks": ["summary", "outcome", "actions", "trackers", "scorecard"],
        "llm_trackers": [{"id": "court", "label": "Угроза судом",
                          "description": "Клиент грозит судом или жалобой"}],
        "llm_scorecard": [{"id": "greet", "question": "Сотрудник поздоровался?",
                           "weight": 2}],
        "llm_embeddings": True, "qa_enabled": True,
    }.items():
        hub.settings.set(ключ, значение, source="api")
    готовые = [з["id"] for з in hub.db.query(
        "SELECT id FROM jobs WHERE status='completed' ORDER BY created_at DESC LIMIT 12")]
    for ид in готовые:
        hub.llm_worker.analyze_job(ид, force=True)
    hub.db.golden_put(готовые[0], {"reason": "другое", "resolved": True})
    адрес, остановить = поднять(архив.app)
    вкладка = Вкладка(адрес)
    yield вкладка, hub, готовые
    вкладка.закрыть()
    остановить()
    архив.закрыть()


def test_voice_tabs_with_commitments_risks_and_ask(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("voice")
    с.click("#vo-tabs button[data-tab='actions']")
    с.wait_for_selector("text=Контроль договорённостей", timeout=15000)
    с.click("#vo-tabs button[data-tab='trackers']")
    с.wait_for_selector("text=Риск ухода и эскалации", timeout=15000)
    с.click("#vo-tabs button[data-tab='ask']")
    с.fill("#vo-ask-q", "почему клиенты недовольны доставкой?")
    с.click("#vo-ask-go")
    с.wait_for_selector("#vo-ask-a >> text=записям", timeout=15000)
    с.fill("#vo-sem-q", "доставка не пришла")
    с.click("#vo-sem-go")
    с.wait_for_selector("#vo-sem-a table, #vo-sem-a .empty", timeout=15000)
    вкладка.чисто()


def test_golden_set_block_in_llm_queue(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("llmqueue")
    с.wait_for_selector("#lq-golden >> text=Золотой набор", timeout=15000)
    assert с.locator("#lq-golden >> text=Записей в наборе").count() == 1
    вкладка.чисто()


def test_trainer_view_runs_a_session(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("trainer")
    с.wait_for_selector("#tr-start")
    с.click("#tr-start")
    с.wait_for_selector("#tr-reply", timeout=15000)
    с.fill("#tr-reply", "Здравствуйте, меня зовут Анна, сейчас проверю ваш заказ.")
    с.click("#tr-send")
    с.wait_for_function("document.querySelectorAll('#tr-lines > div').length >= 3", timeout=15000)
    с.click("#tr-finish")
    с.wait_for_selector("text=Оценка", timeout=15000)
    вкладка.чисто()


def test_record_card_shows_why_and_buttons(стенд):
    вкладка, _, готовые = стенд
    с = вкладка.открыть("results")
    с.evaluate(f"window.__asrhub.openJob('{готовые[0]}')")
    с.click("#job-tabs button[data-tab='analysis']", timeout=15000)
    с.wait_for_selector("#job-llm-golden", timeout=15000)
    с.click("#job-llm-golden")
    с.wait_for_selector("text=Эталон человека: золотой набор")
    с.click("[data-g-save]")
    с.wait_for_selector("text=Эталон сохранён", timeout=10000)
    вкладка.чисто()


def test_employees_have_model_columns_and_card_block(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("employees")
    с.wait_for_timeout(500)
    assert с.locator("text=По ответам модели").count() >= 1
    вкладка.чисто()


def test_operator_card_has_model_block_and_plan(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("employees")
    с.wait_for_selector("#emp-body tr[data-key]", timeout=15000)
    с.locator("#emp-body tr[data-key]").first.click()
    с.wait_for_selector("#emp-card >> text=По ответам модели", timeout=15000)
    вкладка.чисто()


def test_qc_shows_survey_insights(стенд):
    вкладка, _, _ = стенд
    с = вкладка.открыть("qc")
    с.wait_for_selector("text=Оценка клиента и прогноз", timeout=20000)
    вкладка.чисто()


def test_trends_with_model_series_draw_markers(стенд):
    вкладка, hub, _ = стенд
    assert hub.db.llm_versions_list()
    с = вкладка.открыть("trends")
    с.wait_for_timeout(500)
    вкладка.чисто()
