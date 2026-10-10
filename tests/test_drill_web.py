"""Заход 49: раскрытие числа в список — как это видит человек в браузере.

Проверяется сам механизм на разделе «Аналитика»: число раскрывается в
список, список листается, запись открывается по ссылке, а пока ничего не
раскрыто — страница за списками не ходит вовсе.
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
    хранилище = Архив(tmp_path_factory.mktemp("drill-ui") / "data", заданий=140)
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер)
    try:
        yield окно
    finally:
        окно.закрыть()


def test_пока_число_не_раскрыли_за_списком_не_ходят(вкладка):
    """Чисел на странице сотня. Сто запросов при открытии раздела — это и
    есть «тормозит на большом количестве звонков»."""
    с = вкладка.открыть("analytics")
    assert с.locator("button.drill").count() >= 5
    assert вкладка.раскрытий() == 0
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_число_раскрывается_списком_и_сворачивается(вкладка):
    с = вкладка.открыть("analytics")
    с.select_option("#an-period", "month") if с.locator("#an-period").count() else None
    с.wait_for_timeout(600)
    число = с.locator(".kpi button.drill").first
    всего = int("".join(ч for ч in число.inner_text() if ч.isdigit()))
    число.click()
    с.wait_for_selector(".drill-panel tbody tr")
    assert число.get_attribute("aria-expanded") == "true"
    assert с.locator(".drill-panel tbody tr").count() == min(50, всего)
    assert f"из {всего}" in с.locator(".drill-count").inner_text().replace(" ", " ")
    assert вкладка.раскрытий() == 1
    # Панель стоит под рядом плиток во всю ширину, а не внутри плитки.
    assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    число.click()
    assert с.locator(".drill-panel").count() == 0
    assert число.get_attribute("aria-expanded") == "false"
    вкладка.чисто()


def test_список_листается_и_не_растёт_без_предела(вкладка):
    с = вкладка.открыть("analytics")
    с.locator(".kpi button.drill").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    до = с.locator(".drill-panel tbody tr").count()
    assert до == 50
    с.locator(".drill-more").click()
    с.wait_for_function("document.querySelectorAll('.drill-panel tbody tr').length > 50")
    после = с.locator(".drill-panel tbody tr").count()
    номера = с.evaluate("""() => Array.from(document.querySelectorAll(
        '.drill-panel a.drill-open')).map((а) => а.dataset.open)""")
    assert len(номера) == после == len(set(номера))
    # Высота панели ограничена: тысяча строк не уводит раздел вниз.
    assert с.evaluate("document.querySelector('.drill-body').clientHeight") <= 460
    # Страниц две или три: пока кнопка докручивается под щелчок, низ списка
    # попадает в поле зрения, и следующую страницу могла взять сама прокрутка.
    assert вкладка.раскрытий() in (2, 3)
    assert после == 50 * вкладка.раскрытий()
    вкладка.чисто()


def test_прокрутка_до_низа_подгружает_сама(вкладка):
    с = вкладка.открыть("analytics")
    с.locator(".kpi button.drill").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    с.evaluate("""() => { const т = document.querySelector('.drill-body');
        т.scrollTop = т.scrollHeight; }""")
    с.wait_for_function("document.querySelectorAll('.drill-panel tbody tr').length > 50")
    вкладка.чисто()


def test_ссылка_открывает_запись_а_список_остаётся(вкладка):
    с = вкладка.открыть("analytics")
    с.locator(".kpi button.drill").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    ссылка = с.locator(".drill-panel a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    assert ссылка.get_attribute("href") == f"#/jobs/{номер}"
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    # Раздел не сменился, список на месте: закрыл карточку — иди к следующей.
    assert с.evaluate("location.hash") == "#analytics"
    assert с.locator(".drill-panel tbody tr").count() == 50
    вкладка.чисто()


def test_ссылка_из_новой_вкладки_ведёт_к_записи(вкладка):
    с = вкладка.открыть("/jobs/dr0000")
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == "dr0000"


def test_число_в_таблице_раскрывается_строкой_под_своей(вкладка):
    с = вкладка.открыть("analytics")
    кнопка = с.locator("#table-models button.drill").first
    сколько = int(кнопка.inner_text().strip().split()[0])
    кнопка.click()
    с.wait_for_selector("#table-models tr.drill-row .drill-panel tbody tr")
    assert с.locator("#table-models tr.drill-row .drill-panel tbody tr").count() == min(50, сколько)
    assert с.evaluate("""() => { const р = document.querySelector('#table-models tr.drill-row');
        return р.previousElementSibling.contains(
          document.querySelector('#table-models button.drill-on')); }""")
    # Вторая строка той же таблицы открывает свой список, первый остаётся.
    # Столбец назван явно: чисел-кнопок в строке теперь несколько («Успешно»,
    # «RTF ср.»…), и «вторая кнопка таблицы» — это соседняя ячейка той же
    # строки, а она список своей строки заменяет, а не добавляет второй.
    кнопки = с.locator("#table-models td:nth-child(2) button.drill")
    if кнопки.count() > 1:
        кнопки.nth(1).click()
        с.wait_for_function(
            "document.querySelectorAll('#table-models tr.drill-row').length === 2")
    вкладка.чисто()


def test_сектор_кольца_раскрывается_под_рядом_карточек(вкладка):
    с = вкладка.открыть("analytics")
    # Щёлкаем по строке легенды: у сектора на всё кольцо середина — дырка.
    с.locator("#chart-status .row.pick").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    заголовок = с.locator(".drill-title").inner_text()
    assert заголовок.startswith("Задания:")
    статусы = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '.drill-panel tbody tr td:nth-child(6)')).map((я) => я.innerText.trim())"""))
    assert len(статусы) == 1, статусы
    # Тот же сектор ещё раз — свернуть.
    с.locator("#chart-status .row.pick").first.click()
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_порядок_меняется_без_перезагрузки_раздела(вкладка):
    с = вкладка.открыть("analytics")
    с.locator(".kpi button.drill").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    первая = с.locator(".drill-panel a.drill-open").first.get_attribute("data-open")
    с.select_option(".drill-sort", "time:asc")
    с.wait_for_function(
        """(было) => { const а = document.querySelector('.drill-panel a.drill-open');
            return а && а.dataset.open !== было; }""", arg=первая)
    assert с.locator(".drill-panel tbody tr").count() == 50
    вкладка.чисто()


def test_на_узком_экране_панель_не_раздвигает_страницу(сервер):
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = окно.открыть("analytics")
        до = с.evaluate("document.documentElement.scrollWidth")
        с.locator(".kpi button.drill").first.click()
        с.wait_for_selector(".drill-panel tbody tr")
        # Таблица списка шире экрана — и листается внутри своей рамки.
        assert с.evaluate("document.documentElement.scrollWidth") <= до
        assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
            return п.getBoundingClientRect().right <= window.innerWidth + 1; }""")
        окно.чисто()
    finally:
        окно.закрыть()
