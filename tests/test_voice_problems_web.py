"""Заход 53 в настоящем браузере: проблемы и сложности за период.

Серверные числа сверены в `test_voice_problems.py`. Здесь — что интерфейс
их показывает: блок «Проблемы и сложности за период» в разделе за месяц и
во вкладке «Проблемы» «Голосовой аналитики» (с короткой сводкой на
«Сводке»), число раскрывается в список разговоров той же длины, а сводка
языковой моделью составляется по кнопке и потом показывается сама.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("voice-problems-ui") / "data", с_моделью=20)
    адрес, остановить = поднять(хранилище.app)
    yield адрес, хранилище
    остановить()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер[0])
    try:
        yield окно
    finally:
        окно.закрыть()


def _первая_строка(с, место: str) -> tuple[str, int]:
    """Первая строка перечня проблем: название и сколько разговоров."""
    return с.evaluate("""(место) => {
      const строка = document.querySelector(`${место} .vprob-table tbody tr:not(.vprob-group):not(.vprob-zero)`);
      const кнопка = строка.querySelector('td:nth-child(2) button.drill');
      return [строка.querySelector('td').innerText.trim(),
              Number(кнопка.innerText.replace(/[^0-9]/g, ''))];
    }""", место)


def test_проблемы_в_разделе_за_месяц(вкладка):
    с = вкладка.открыть("voice_month")
    с.wait_for_selector("#vp-problems .vprob-table tbody tr")
    данные = с.evaluate("() => window.__asrhub.RENDERERS.voice_month.проблемы")
    assert данные["conversations"] > 0 and данные["period_id"].startswith("month:")
    текст = с.inner_text("#vp-problems")
    assert "Проблемы есть в" in текст and "Клиент и его вопрос" in текст
    assert "Работа оператора" in текст and "Запись и разбор" in текст
    название, разговоров = _первая_строка(с, "#vp-problems")
    строка = next(и for и in данные["items"] if и["label"] == название)
    assert строка["records"] == разговоров > 0
    до = вкладка.раскрытий()
    с.locator("#vp-problems .vprob-table tbody tr:not(.vprob-group):not(.vprob-zero)").first \
        .locator("td:nth-child(2) button.drill").click()
    с.wait_for_selector("#vp-problems .drill-panel tbody tr")
    assert вкладка.раскрытий() == до + 1
    assert с.locator("#vp-problems .drill-panel tbody tr").count() == min(50, разговоров)
    assert название in с.inner_text("#vp-problems .drill-panel")
    # Трекеры модели дорисовываются второй частью отчёта, раскрытый список
    # при этом остаётся на месте.
    с.wait_for_function("() => window.__asrhub.RENDERERS.voice_month.списки !== null")
    с.wait_for_timeout(300)
    assert с.locator("#vp-problems .drill-panel").count() == 1
    вкладка.чисто()


def test_проблемы_в_голосовой_аналитике_и_сводка_моделью(вкладка):
    с = вкладка.открыть("voice")
    с.select_option("#vo-period", "all")
    # Сводку «за неделю» сменяет сводка «за всё время»: ждём её, а не первую.
    с.wait_for_function("() => ((window.__asrhub.state.voiceProblems || {}).period_id"
                        " === 'period:all')")
    с.wait_for_selector("#vo-problems-short .kpi")
    assert "Проблемы есть в" in с.inner_text("#vo-problems-short")
    с.click("#vo-problems-all")
    с.wait_for_selector("#vo-problems .vprob-table tbody tr")
    assert с.locator("#vo-tabs button.active").inner_text() == "Проблемы"
    данные = с.evaluate("() => window.__asrhub.state.voiceProblems")
    assert данные["base"] is None                     # «за всё время» сравнивать не с чем
    название, разговоров = _первая_строка(с, "#vo-problems")
    assert next(и for и in данные["items"] if и["label"] == название)["records"] == разговоров
    # Сводка моделью — по кнопке; на заглушке она пересказывает перечень.
    с.click("#vo-problems [data-vprob-digest]")
    с.wait_for_selector("#vo-problems .vprob-list li", timeout=30000)
    assert "заглушкой" in с.inner_text("#vo-problems .vprob-digest")
    # Составленная сводка хранится: после перехода она видна без кнопки.
    с = вкладка.открыть("dashboard")
    с = вкладка.открыть("voice")
    с.select_option("#vo-period", "all")
    с.wait_for_function("() => ((window.__asrhub.state.voiceProblems || {}).period_id"
                        " === 'period:all')")
    с.click('#vo-tabs button[data-tab="problems"]')
    с.wait_for_selector("#vo-problems .vprob-list li", timeout=30000)
    assert "Составлено" in с.inner_text("#vo-problems .vprob-digest")
    вкладка.чисто()
