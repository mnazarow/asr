"""Заход 49: раскрытие чисел «Аналитики по сотрудникам» — глазами человека.

Числа таблицы и карточки сверены со списками на сервере
(`test_drill_employees.py`); здесь — то, что сервером не проверить: число в
строке раскрывает список и не открывает карточку, щелчок мимо числа карточку
по-прежнему открывает, звонки раскрываются звонками со ссылкой на запись,
панель встаёт рядом с числом, а пока ничего не раскрыто — за списками никто
не ходит.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_employees import _досеять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-emp-ui") / "data")
    _досеять(хранилище)
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


def _число(текст: str) -> int:
    return int("".join(знак for знак in текст if знак.isdigit()))


def _раздел(вкладка, период: str = ""):
    с = вкладка.открыть("employees")
    с.wait_for_selector("#emp-body table")
    if период:
        с.click(f'#emp-period button[data-period="{период}"]')
        с.wait_for_timeout(700)
    return с


def _карточка(с, ключ: str = "102"):
    с.locator(f'#emp-body tbody tr[data-key="{ключ}"] [data-emp-open]').click()
    с.wait_for_selector("#emp-card table")
    с.wait_for_timeout(500)


def test_пока_число_не_раскрыли_за_списком_не_ходят(вкладка):
    """В таблице и карточке под сотню чисел-кнопок; запросов за списками — ноль."""
    с = _раздел(вкладка)
    assert с.locator("#emp-top .kpi button.drill").count() >= 4
    assert с.locator("#emp-body button.drill").count() >= 8
    _карточка(с)
    assert с.locator("#emp-card button.drill").count() >= 30
    assert вкладка.раскрытий() == 0
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_плитка_команды_раскрывается_под_рядом_плиток(вкладка):
    с = _раздел(вкладка)
    число = с.locator("#emp-top .kpi button.drill").first
    всего = _число(число.inner_text())
    число.click()
    с.wait_for_selector("#emp-top .drill-panel tbody tr")
    assert с.locator("#emp-top .drill-panel tbody tr").count() == min(50, всего)
    assert f"из {всего}" in с.locator(".drill-count").inner_text()
    assert с.evaluate("""() => { const п = document.querySelector('#emp-top .drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    # Плитка со средним раскрывается по величине: худшие сверху.
    с.locator("#emp-top .kpi button.drill").nth(1).click()
    с.wait_for_function("document.querySelector('#emp-top .drill-sort').value === 'agent_score:asc'")
    с.wait_for_selector("#emp-top .drill-panel tbody tr")
    баллы = с.evaluate("""() => Array.from(document.querySelectorAll(
        '#emp-top .drill-panel tbody tr td:last-child')).map((я) => Number(я.innerText))""")
    assert len(баллы) > 5 and баллы == sorted(баллы)
    assert вкладка.раскрытий() == 2
    вкладка.чисто()


def test_число_в_строке_раскрывает_список_а_строка_открывает_карточку(вкладка):
    с = _раздел(вкладка)
    строка = с.locator('#emp-body tbody tr[data-key="102"]')
    кнопка = строка.locator("td").nth(1).locator("button.drill")
    записей = _число(кнопка.inner_text())
    кнопка.click()
    с.wait_for_selector("#emp-body tr.drill-row .drill-panel tbody tr")
    assert с.locator("#emp-body tr.drill-row .drill-panel tbody tr").count() == min(50, записей)
    assert "102" in с.locator("#emp-body .drill-title").inner_text()
    # Щелчок по числу карточку не открыл.
    assert с.locator("#emp-card table").count() == 0
    assert с.evaluate("""() => document.querySelector('#emp-body tr.drill-row')
        .previousElementSibling.dataset.key""") == "102"
    # Щелчок мимо числа — по-прежнему карточка; список в таблице остаётся.
    строка.locator("td").nth(5).click()
    с.wait_for_selector("#emp-card table")
    assert с.locator("#emp-body tr.drill-row .drill-panel tbody tr").count() == min(50, записей)
    вкладка.чисто()


def test_звонки_сотрудника_раскрываются_журналом_со_ссылкой_на_запись(вкладка):
    с = _раздел(вкладка)
    строка = с.locator('#emp-body tbody tr[data-key="102"]')
    записей = _число(строка.locator("td").nth(1).inner_text())
    кнопка = строка.locator("td").nth(2).locator("button.drill")
    звонков = _число(кнопка.inner_text())
    assert звонков != записей
    assert кнопка.get_attribute("title") == "Показать звонки"
    кнопка.click()
    с.wait_for_selector("#emp-body tr.drill-row .drill-panel tbody tr")
    assert с.locator("#emp-body tr.drill-row .drill-panel tbody tr").count() == min(50, звонков)
    шапка = с.locator("#emp-body .drill-panel thead").inner_text().lower()
    assert "направление" in шапка and "оператор" in шапка
    операторы = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#emp-body .drill-panel tbody tr td:nth-child(5)')).map((я) => я.innerText.trim())"""))
    assert операторы == {"102"}
    # У звонка с записью — ссылка, у недозвона — причина, почему записи нет.
    ссылки = с.locator("#emp-body .drill-panel a.drill-open")
    assert 0 < ссылки.count() < min(50, звонков)
    номер = ссылки.first.get_attribute("data-open")
    ссылки.first.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert с.evaluate("location.hash") == "#employees"
    вкладка.чисто()


def test_наговорено_раскрывается_теми_же_звонками_по_длительности(вкладка):
    с = _раздел(вкладка)
    строка = с.locator('#emp-body tbody tr[data-key="103"]')
    звонков = _число(строка.locator("td").nth(2).inner_text())
    строка.locator("td").nth(3).locator("button.drill").click()
    с.wait_for_selector("#emp-body tr.drill-row .drill-panel tbody tr")
    assert с.locator("#emp-body .drill-sort").input_value() == "billsec:desc"
    длительности = с.evaluate("""() => Array.from(document.querySelectorAll(
        '#emp-body .drill-panel tbody tr td:nth-child(6)')).map((я) => {
          const [м, сек] = я.innerText.trim().split(':').map(Number); return м * 60 + сек; })""")
    assert len(длительности) == min(50, звонков)
    assert длительности == sorted(длительности, reverse=True) and длительности[0] > 0
    вкладка.чисто()


def test_карточка_шапка_и_таблица_сравнения(вкладка):
    с = _раздел(вкладка)
    _карточка(с)
    шапка = с.locator("#emp-card .card-head button.drill")
    разговоров = _число(шапка.first.inner_text())
    шапка.first.click()
    с.wait_for_selector("#emp-card .drill-panel tbody tr")
    assert с.locator("#emp-card .drill-panel tbody tr").count() == min(50, разговоров)
    # Список встаёт под шапкой карточки, а не в её конце под всеми таблицами.
    assert с.evaluate("""() => { const п = document.querySelector('#emp-card .drill-panel');
        return п.previousElementSibling.classList.contains('card-head'); }""")
    шапка.first.click()
    assert с.locator("#emp-card .drill-panel").count() == 0
    # Звонки в шапке — журналом.
    шапка.nth(1).click()
    с.wait_for_selector("#emp-card .drill-panel tbody tr")
    assert "направление" in с.locator("#emp-card .drill-panel thead").inner_text().lower()
    шапка.nth(1).click()
    # Строка сравнения: «Сотрудник» — свои записи по величине, «Команда» — все.
    балл = с.locator("#emp-card tbody tr", has_text="Балл оператора").first
    кнопки = балл.locator("button.drill")
    assert кнопки.count() == 2          # за месяц прошлого периода в данных нет
    кнопки.first.click()
    с.wait_for_selector("#emp-card tr.drill-row .drill-panel tbody tr")
    assert "балл" in с.locator("#emp-card tr.drill-row thead").inner_text().lower()
    своих = с.locator("#emp-card tr.drill-row .drill-panel tbody tr").count()
    assert 0 < своих <= разговоров
    кнопки.nth(1).click()
    с.wait_for_function("""() => { const з = document.querySelector('#emp-card tr.drill-row .drill-title');
        return з && з.innerText.includes('вся команда'); }""")
    с.wait_for_selector("#emp-card tr.drill-row .drill-panel tbody tr")
    assert с.locator("#emp-card tr.drill-row").count() == 1
    assert с.locator("#emp-card tr.drill-row .drill-panel tbody tr").count() > своих
    # Доля раскрывается числителем.
    доля = с.locator("#emp-card tbody tr", has_text="С нарушениями, %").first
    доля.locator("button.drill").first.click()
    с.wait_for_function("""() => Array.from(document.querySelectorAll('#emp-card tr.drill-row .drill-title'))
        .some((з) => з.innerText.includes('Разговоры с нарушениями'))""")
    вкладка.чисто()


def test_прошлый_период_раскрывается_своим_окном(вкладка):
    с = _раздел(вкладка, "day")
    _карточка(с)
    кнопка = с.locator("#emp-card tbody tr td:nth-child(4) button.drill").first
    кнопка.click()
    с.wait_for_selector("#emp-card tr.drill-row .drill-panel tbody tr")
    assert "прошлый период" in с.locator("#emp-card tr.drill-row .drill-title").inner_text()
    # Записи прошлых суток: все старше начала нынешнего окна.
    окно = с.evaluate("() => JSON.parse(document.querySelector('#emp-card tbody tr "
                      "td:nth-child(4) button.drill').dataset.drill)")
    assert окно["until"] - окно["since"] == pytest.approx(86400, abs=1)
    нынешнее = с.evaluate("() => JSON.parse(document.querySelector('#emp-card tbody tr "
                          "td:nth-child(2) button.drill').dataset.drill)")
    assert нынешнее["since"] == окно["until"]
    вкладка.чисто()


def test_нарушения_и_точка_хода_по_неделям(вкладка):
    с = _раздел(вкладка)
    _карточка(с)
    чип = с.locator("#emp-card .chip.err button.drill").first
    сколько = _число(чип.inner_text())
    чип.click()
    с.wait_for_selector("#emp-card [data-drill-host] .drill-panel tbody tr")
    assert с.locator("#emp-card .drill-panel tbody tr").count() == сколько
    assert "Нарушение" in с.locator("#emp-card .drill-title").inner_text()
    с.locator("#emp-card .drill-close").click()
    # Точка графика — неделя сотрудника. Щёлкаем у правого края: там последняя.
    ход = с.evaluate("""async () => (await window.__asrhub.API.call(
        '/api/content/agents/102?by=agent&period=month')).timeline""")
    # Место щелчка — относительно самого слоя: карточка после открытия ещё
    # доезжает до верха плавной прокруткой, и координаты экрана устаревают.
    слой = с.locator("#emp-line svg .pick").first
    рамка = слой.bounding_box()
    слой.click(position={"x": рамка["width"] - 3, "y": рамка["height"] / 2})
    с.wait_for_selector("#emp-card .drill-panel tbody tr")
    assert с.locator("#emp-card .drill-title").inner_text().startswith("Разговоры ")
    assert ход[-1]["records"] > 0
    assert с.locator("#emp-card .drill-panel tbody tr").count() == min(50, ход[-1]["records"])
    # Список под графиками во всю ширину карточки, а не в её половине.
    assert с.evaluate("""() => { const п = document.querySelector('#emp-card .drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    # Пустая неделя (левый край) списка не открывает и прежний не сворачивает.
    было = вкладка.раскрытий()
    слой.click(position={"x": 3, "y": рамка["height"] / 2})
    с.wait_for_timeout(300)
    assert ход[0]["records"] == 0 and вкладка.раскрытий() == было
    вкладка.чисто()


def test_на_узком_экране_список_не_раздвигает_страницу(сервер):
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = окно.открыть("employees")
        с.wait_for_selector("#emp-body table")
        до = с.evaluate("document.documentElement.scrollWidth")
        с.locator("#emp-body button.drill").first.click()
        с.wait_for_selector(".drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= max(до, 391)
        окно.чисто()
    finally:
        окно.закрыть()
