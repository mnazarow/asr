"""Заход 49: раскрытие чисел «Операторов», «Разрезов», «Тем», «Связей» и
«Что послушать» — как это видит человек в браузере.

Серверная сверка (`test_drill_content2.py`) отвечает за то, что отбор
сходится с числом. Здесь — что интерфейс этот отбор и просит: под числом
раскрывается столько строк, сколько оно обещает, окном того ответа, из
которого оно взято; ссылка открывает запись; а пока ничего не раскрыто,
вкладка за списками не ходит вовсе.
"""
from __future__ import annotations

from typing import Any

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_content2 import досеять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-content2-ui") / "data")
    досеять(хранилище)
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


def _на_вкладку(окно: Вкладка, имя: str, ждать: str, период: str = "") -> Any:
    с = окно.открыть("content")
    if период:
        с.click(f'#content-period button[data-period="{период}"]')
        с.wait_for_timeout(600)
    с.click(f'#content-tabs button[data-tab="{имя}"]')
    с.wait_for_selector(ждать, timeout=20000)
    с.wait_for_timeout(500)
    return с


def _число(текст: str) -> int:
    return int("".join(знак for знак in текст if знак.isdigit()))


def _раскрыто(с: Any, место: str = "") -> tuple[int, str, str]:
    """Сколько строк в раскрытом списке, его подпись «показано…» и заголовок."""
    панель = f"{место} .drill-panel".strip()
    с.wait_for_function(
        """(п) => { const у = document.querySelector(п);
            return у && (у.querySelector('.drill-table > tbody > tr')
              || /нет/.test(у.querySelector('.drill-foot').innerText)); }""",
        arg=панель, timeout=15000)
    return (с.locator(f"{панель} tbody tr").count(),
            с.locator(f"{панель} .drill-count").first.inner_text().replace(" ", " "),
            с.locator(f"{панель} .drill-title").first.inner_text())


def _свернуть_всё(с: Any) -> None:
    for _ in range(20):
        if not с.locator(".drill-panel").count():
            return
        с.locator(".drill-panel .drill-close").first.click()
    raise AssertionError("списки не сворачиваются")


def _окно_списка(окно: Вкладка) -> tuple[float, float]:
    """Границы, с которыми страница сходила за последним списком."""
    from urllib.parse import parse_qs, urlparse

    запрос = parse_qs(urlparse([а for а in окно.запросы if "/api/drill?" in а][-1]).query)
    return float(запрос["since"][0]), float(запрос["until"][0])


def _ответ(с: Any, путь: str) -> dict[str, Any]:
    """Свод тем же запросом, что делает вкладка, — за числами, которых на
    экране нет (у доли виден процент, а раскрывается числитель)."""
    return с.evaluate("(п) => fetch(п).then((о) => о.json())", путь)


# ---------------------------------------------------------------------------
# Пока не раскрыли — ни одного запроса
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("имя,ждать,кнопок", [
    ("agents", "#agents-table", 20), ("groups", "#chart-by-owner svg", 150),
    ("topics", "#chart-topics svg", 40), ("links", "#content-body table", 2),
    ("records", "#content-records table", 1),
])
def test_вкладка_с_числами_кнопками_за_списками_не_ходит(вкладка, имя, ждать, кнопок):
    """На «Разрезах» чисел-кнопок две с половиной сотни. Запрос на каждое
    при открытии вкладки — это и было бы «тормозит на большом архиве»."""
    с = _на_вкладку(вкладка, имя, ждать)
    assert с.locator("#content-body button.drill").count() >= кнопок
    assert с.locator(".drill-panel").count() == 0
    assert вкладка.раскрытий() == 0
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Операторы
# ---------------------------------------------------------------------------


def test_число_в_строке_оператора_раскрывает_список_а_строка_карточку(вкладка):
    """У строки свой щелчок — карточка оператора. Число его не запускает,
    а щелчок мимо числа, по самой строке, по-прежнему открывает карточку."""
    с = _на_вкладку(вкладка, "agents", "#agents-table")
    строка = '#agents-table tr.clickable[data-agent="SPEAKER_00"]'
    кнопка = с.locator(f"{строка} td:nth-child(2) button.drill")
    записей = _число(кнопка.inner_text())
    assert 5 <= записей <= 50
    кнопка.click()
    строк, подпись, заголовок = _раскрыто(с, "#agents-table tr.drill-row")
    assert строк == записей and f"показано {записей}" in подпись
    assert "SPEAKER_00" in заголовок
    assert вкладка.раскрытий() == 1
    # Карточка не открылась: таблица на месте, список — строкой под своей.
    assert с.locator("#agent-back").count() == 0
    assert с.evaluate("""(с) => document.querySelector(с).nextElementSibling
        .classList.contains('drill-row')""", строка)
    # Доля раскрывается в числитель: «отрицательных 25,9 %» — это столько записей.
    свод = _ответ(с, "/api/content/agents?period=month&by=speaker")
    отрицательных = next(г["negative"] for г in свод["items"] if г["key"] == "SPEAKER_00")
    assert отрицательных > 0
    с.locator(f"{строка} td:nth-child(5) button.drill").click()
    с.wait_for_function("""() => /отрицательные/.test(
        document.querySelector('#agents-table tr.drill-row .drill-title').innerText)""")
    строк, _, _ = _раскрыто(с, "#agents-table tr.drill-row")
    assert строк == отрицательных
    # Среднее — записи, по которым оно посчитано, худшими вперёд: столбец
    # величины растёт сверху вниз.
    с.locator(f"{строка} td:nth-child(3) button.drill").click()
    с.wait_for_function("""() => /по баллу/.test(
        document.querySelector('#agents-table tr.drill-row .drill-title').innerText)""")
    _раскрыто(с, "#agents-table tr.drill-row")
    баллы = с.evaluate("""() => Array.from(document.querySelectorAll(
        '#agents-table tr.drill-row tbody tr td:last-child')).map((я) => Number(я.innerText))""")
    assert len(баллы) >= 5 and баллы == sorted(баллы)
    # Ссылка открывает запись поверх раздела; список остаётся.
    ссылка = с.locator("#agents-table tr.drill-row a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    с.keyboard.press("Escape")
    с.wait_for_selector(".modal-backdrop", state="detached")
    assert с.locator("#agents-table tr.drill-row").count() == 1
    # Щелчок по строке — её середина приходится на поле между колонками —
    # открывает карточку.
    под_курсором = с.evaluate("""(с) => { const р = document.querySelector(с)
        .getBoundingClientRect();
      const у = document.elementFromPoint(р.left + р.width / 2, р.top + р.height / 2);
      return у.closest('button') ? 'кнопка' : у.tagName; }""", строка)
    assert под_курсором == "TD"
    с.click(строка)
    с.wait_for_selector("#agent-score-chart svg", timeout=15000)
    assert с.locator("#agent-back").count() == 1
    вкладка.чисто()


def test_счётчик_очереди_коучинга_раскрывает_всю_очередь(вкладка):
    """В таблице очереди — первые тридцать строк; под счётчиком — вся."""
    с = _на_вкладку(вкладка, "agents", "#agents-table")
    кнопка = с.locator("#content-body .card h3 button.drill")
    всего = _число(кнопка.inner_text())
    assert всего > 30
    кнопка.click()
    строк, подпись, заголовок = _раскрыто(с)
    assert строк == всего and "Очередь коучинга" in заголовок
    # Разобранных в списке нет — как и в самой очереди.
    assert вкладка.раскрытий() == 1 and "coaching_open" in [
        а for а in вкладка.запросы if "/api/drill?" in а][0]
    # Список встаёт сразу под шапкой карточки: под рядом карточек он
    # оказался бы за тридцатью строками очереди, далеко за краем экрана.
    assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
        const шапка = п.previousElementSibling;
        return шапка.classList.contains('card-head') && шапка.contains(
          document.querySelector('#content-body .card h3 button.drill-on'))
          && п.getBoundingClientRect().top < window.innerHeight; }""")
    # С разобранными очередь длиннее — и список вместе с ней.
    _свернуть_всё(с)
    с.check("#coaching-done")
    с.wait_for_function(
        "(было) => { const к = document.querySelector('#content-body .card h3 button.drill');"
        " return к && Number(к.innerText.replace(/\\D/g, '')) > было; }", arg=всего)
    кнопка = с.locator("#content-body .card h3 button.drill")
    с_разобранными = _число(кнопка.inner_text())
    кнопка.click()
    строк, _, заголовок = _раскрыто(с)
    assert строк == с_разобранными > всего and "с разобранными" in заголовок
    вкладка.чисто()


def test_карточка_оператора_раскрывает_шапку_плитки_сравнение_и_графики(вкладка):
    с = _на_вкладку(вкладка, "agents", "#agents-table")
    с.click('#agents-table tr.clickable[data-agent="SPEAKER_00"] button[data-agent]')
    с.wait_for_selector("#agent-score-chart svg", timeout=15000)
    с.wait_for_timeout(400)
    assert вкладка.раскрытий() == 0
    карточка = _ответ(с, "/api/content/agents/SPEAKER_00?period=month&by=speaker")
    своих, у_команды = карточка["summary"]["records"], карточка["team"]["records"]
    # Шапка: «записей за период: X из Y у команды» — оба числа, список под ней.
    шапка = с.locator("#content-body .settings-toolbar button.drill")
    assert [_число(т) for т in шапка.all_inner_texts()] == [своих, у_команды]
    шапка.nth(0).click()
    строк, подпись, _ = _раскрыто(с, "#agent-head-drill")
    assert строк == своих
    шапка.nth(1).click()
    с.wait_for_function("""() => /Команда/.test(document.querySelector(
        '#agent-head-drill .drill-title').innerText)""")
    строк, подпись, _ = _раскрыто(с, "#agent-head-drill")
    assert строк == min(50, у_команды) and f"из {у_команды}" in подпись
    # То же число ещё раз — свернуть.
    шапка.nth(1).click()
    assert с.locator(".drill-panel").count() == 0
    # Панель липкая: по числу щёлкают и пролистав карточку до конца. Список
    # встаёт под исходным местом панели — и страница к нему подъезжает.
    с.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    с.mouse.wheel(0, 5000)
    с.wait_for_timeout(300)
    assert шапка.nth(0).is_visible()
    шапка.nth(0).click()
    _раскрыто(с, "#agent-head-drill")
    с.wait_for_function("""() => { const р = document.querySelector(
        '#agent-head-drill .drill-panel').getBoundingClientRect();
      const низ = document.querySelector('.settings-toolbar').getBoundingClientRect().bottom;
      return р.top >= низ - 1 && р.top < window.innerHeight - 100; }""", timeout=5000)
    _свернуть_всё(с)
    с.evaluate("window.scrollTo(0, 0)")
    с.mouse.wheel(0, -9000)
    с.wait_for_timeout(300)
    # Плитка «С нарушениями» — под рядом плиток.
    плитка = с.locator("#content-body .kpi").nth(3).locator("button.drill")
    нарушений = _число(плитка.inner_text())
    assert нарушений == карточка["summary"]["violation_records"] > 0
    плитка.click()
    строк, _, заголовок = _раскрыто(с)
    assert строк == нарушений and "с нарушениями" in заголовок
    assert с.evaluate("""() => document.querySelector('.drill-panel')
        .previousElementSibling.classList.contains('grid')""")
    _свернуть_всё(с)
    # «Против команды»: столбец «Прошлый период» раскрывается прошлым окном.
    прошлое = карточка["previous_window"]
    assert карточка["previous"]["records"] > 0
    строка = с.locator("#content-body table tr", has_text="Балл оператора")
    assert строка.locator("button.drill").count() == 3
    строка.locator("td:nth-child(5) button.drill").click()
    строк, _, заголовок = _раскрыто(с, "#content-body tr.drill-row")
    assert "прошлый период" in заголовок and 0 < строк <= карточка["previous"]["records"]
    # Вкладка спрашивала свод на мгновение раньше нас: границы те же с
    # точностью до этого мгновения, а ширина окна — ровно месяц.
    от, до = _окно_списка(вкладка)
    assert до == pytest.approx(прошлое["until"], abs=30) and до - от == pytest.approx(30 * 86400)
    # Составные показатели набором записей не являются — и остаются числами.
    for подпись_строки in ("Возражений без отработки", "Индекс NPS"):
        assert с.locator("#content-body table tr", has_text=подпись_строки).locator(
            "button.drill").count() == 0
    _свернуть_всё(с)
    # График по неделям: точка — записи оператора за эту неделю.
    последняя = карточка["timeline"][-1]
    assert последняя["records"] > 0
    слой = с.locator("#agent-score-chart svg rect.pick")
    рамка = слой.bounding_box()
    слой.click(position={"x": рамка["width"] - 2, "y": рамка["height"] / 2})
    строк, подпись, заголовок = _раскрыто(с)
    assert "неделя с" in заголовок and "по баллу" in заголовок
    от, до = _окно_списка(вкладка)
    assert от == pytest.approx(последняя["since"], abs=30)
    assert до - от == pytest.approx(последняя["until"] - последняя["since"], abs=1)
    assert 0 < строк <= min(50, последняя["records"])
    # Пустая неделя не раскрывается: списка под ней нет.
    assert карточка["timeline"][0]["records"] == 0
    до = вкладка.раскрытий()
    слой.click(position={"x": 2, "y": рамка["height"] / 2})
    с.wait_for_timeout(300)
    assert вкладка.раскрытий() == до
    _свернуть_всё(с)
    # Нарушения по категориям и счётчик очереди оператора.
    фишка = с.locator("#content-body .chips .chip.err button.drill").first
    записей = _число(фишка.inner_text())
    фишка.click()
    строк, _, заголовок = _раскрыто(с)
    assert строк == записей and "нарушение" in заголовок
    _свернуть_всё(с)
    очередь = с.locator("#content-body .card h3 button.drill")
    assert _число(очередь.inner_text()) == карточка["coaching_total"]
    очередь.click()
    # Список встаёт сразу под шапкой карточки, а не под таблицей из двадцати строк.
    строк, _, _ = _раскрыто(с, "#content-body .card [data-drill-host]")
    assert строк == карточка["coaching_total"]
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Разрезы
# ---------------------------------------------------------------------------


def test_разрезы_раскрываются_из_таблицы_и_из_графиков(вкладка):
    с = _на_вкладку(вкладка, "groups", "#chart-by-owner svg")
    владельцы = _ответ(с, "/api/content/breakdown/owner?period=month")["items"]
    # Таблица «По владельцам»: «Записей» первой строки.
    таблица = с.locator("#content-body section.card", has_text="все показатели рядом")
    кнопка = таблица.locator("tbody tr").first.locator("td:nth-child(2) button.drill")
    записей = _число(кнопка.inner_text())
    assert записей == владельцы[0]["records"]
    кнопка.click()
    строк, _, заголовок = _раскрыто(с, "#content-body tr.drill-row")
    assert строк == записей and f"Владелец «{владельцы[0]['label']}»" in заголовок
    хозяева = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#content-body tr.drill-row tbody tr td:nth-child(5)')).map((я) => я.innerText.trim())"""))
    assert хозяева == {владельцы[0]["key"]}, хозяева
    _свернуть_всё(с)
    # Столбец графика «Тональность по владельцам» — та же группа, по тональности.
    с.locator("#chart-by-owner svg rect.pick").first.click()
    строк, _, заголовок = _раскрыто(с)
    assert "по тональности" in заголовок and строк > 0
    assert с.evaluate("""() => document.querySelector('.drill-panel')
        .previousElementSibling.classList.contains('grid')""")
    # Тот же столбец ещё раз — свернуть.
    с.locator("#chart-by-owner svg rect.pick").first.click()
    assert с.locator(".drill-panel").count() == 0
    # «По дням недели»: столбец — число записей дня.
    дни = _ответ(с, "/api/content/breakdown/weekday?period=month")["items"]
    с.locator("#chart-by-weekday svg rect.pick").first.click()
    строк, подпись, заголовок = _раскрыто(с)
    assert "День недели" in заголовок
    assert строк == min(50, дни[0]["records"]) and str(дни[0]["records"]) in подпись
    # У списка — окно ответа про дни недели, а не про владельцев: вкладка
    # собрана из восьми запросов, и у каждого своё.
    запрос = [а for а in вкладка.запросы if "/api/drill?" in а][-1]
    assert "weekday%3A" in запрос and "since=" in запрос and "until=" in запрос
    _свернуть_всё(с)
    # «По часам суток»: точка линии.
    слой = с.locator("#chart-by-hour svg rect.pick")
    рамка = слой.bounding_box()
    слой.click(position={"x": рамка["width"] / 2, "y": рамка["height"] / 2})
    строк, _, заголовок = _раскрыто(с)
    assert "Час" in заголовок and строк > 0
    _свернуть_всё(с)
    # Метки и категории — своими отборами.
    for подпись_карточки, отбор in (("метка задания", "tag%3A"),
                                    ("входит в обе группы", "category%3A")):
        карточка = с.locator("#content-body section.card", has_text=подпись_карточки)
        кнопка = карточка.locator("tbody tr").nth(1).locator("td:nth-child(2) button.drill")
        записей = _число(кнопка.inner_text())
        кнопка.click()
        строк, _, _ = _раскрыто(с, "#content-body tr.drill-row")
        assert строк == записей > 0
        assert отбор in [а for а in вкладка.запросы if "/api/drill?" in а][-1]
        _свернуть_всё(с)
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Темы, связи, что послушать
# ---------------------------------------------------------------------------


def test_темы_раскрываются_из_графика_таблицы_хода_и_новых_слов(вкладка):
    с = _на_вкладку(вкладка, "topics", "#chart-topics svg")
    темы = _ответ(с, "/api/content/topics?period=month")
    # «по N разобранным записям периода».
    корпус = с.locator("#content-body .card-head .hint button.drill").first
    assert _число(корпус.inner_text()) == темы["corpus"]
    корпус.click()
    строк, подпись, _ = _раскрыто(с)
    assert строк == min(50, темы["corpus"]) and f"из {темы['corpus']}" in подпись
    _свернуть_всё(с)
    # Строка графика «О чём говорят».
    с.locator("#chart-topics svg rect.pick").first.click()
    строк, _, заголовок = _раскрыто(с)
    assert строк == темы["items"][0]["records"]
    assert f"Тема «{темы['items'][0]['word']}»" in заголовок
    _свернуть_всё(с)
    # «Все темы периода»: число записей последней темы.
    таблица = с.locator("#content-body section.card", has_text="Все темы периода")
    кнопка = таблица.locator("tbody tr").last.locator("td:nth-child(2) button.drill")
    записей = _число(кнопка.inner_text())
    кнопка.click()
    строк, _, _ = _раскрыто(с, "#content-body tr.drill-row")
    assert строк == записей == темы["items"][-1]["records"]
    _свернуть_всё(с)
    # Ход тем: «было» раскрывается прошлым окном, «сейчас» — текущим.
    ушедшая = next(т for т in темы["trend"] if т["stem"] == "старин")
    assert ушедшая["now"] == 0 and ушедшая["before"] > 0
    строка = с.locator("#content-body table tr", has_text="старина").first
    assert строка.locator("button.drill").count() == 1          # «сейчас 0 %» — не кнопка
    строка.locator("button.drill").click()
    строк, _, заголовок = _раскрыто(с, "#content-body tr.drill-row")
    assert строк == ушедшая["before"] and "в прошлом периоде" in заголовок
    ход = темы["trend_window"]
    от, до = _окно_списка(вкладка)
    assert до == pytest.approx(ход["since"], abs=30) and до - от == pytest.approx(30 * 86400)
    _свернуть_всё(с)
    # Новые слова: число на фишке раскрывает список, а не уводит в поиск.
    новое = темы["new"][0]
    фишка = с.locator("#content-body .chips .chip[data-search] button.drill").first
    assert _число(фишка.inner_text()) == новое["now"]
    фишка.click()
    строк, _, _ = _раскрыто(с)
    assert строк == новое["now"]
    assert с.evaluate("location.hash") == "#content"
    вкладка.чисто()


def test_тема_за_всё_время_раскрывается_вместе_с_записью_на_повторе(вкладка):
    """За всё время свод считает тему по таблице основ, не заглядывая в
    задания: запись, отправленная на повтор, в числе есть — и в списке тоже."""
    с = _на_вкладку(вкладка, "topics", "#chart-topics svg", период="all")
    темы = _ответ(с, "/api/content/topics?period=all")
    assert темы["completed_only"] is False
    новинка = next(т for т in темы["items"] if т["stem"] == "новинк")
    строка = с.locator("#content-body section.card", has_text="Все темы периода").locator(
        "tbody tr", has_text="новинка")
    строка.locator("td:nth-child(2) button.drill").click()
    строк, _, _ = _раскрыто(с, "#content-body tr.drill-row")
    assert строк == новинка["records"] == 5
    статусы = с.evaluate("""() => Array.from(document.querySelectorAll(
        '#content-body tr.drill-row tbody tr td:nth-child(6)')).map((я) => я.innerText.trim())""")
    assert len(set(статусы)) == 2, статусы                 # готовые и одна в очереди
    # А ход тем при «всё время» — за месяц: у его чисел окно своё.
    ход = темы["trend_window"]
    assert ход["period"] == "month" and темы["window"]["since"] is None
    _свернуть_всё(с)
    с.locator("#content-body .chips .chip[data-search] button.drill").first.click()
    _раскрыто(с)
    от, до = _окно_списка(вкладка)
    assert от == pytest.approx(ход["since"], abs=30) and до - от == pytest.approx(30 * 86400, abs=5)
    вкладка.чисто()


def test_связь_раскрывается_в_записи_с_обоими_признаками(вкладка):
    с = _на_вкладку(вкладка, "links", "#content-body table")
    связи = _ответ(с, "/api/content/correlations?period=month")
    кнопки = с.locator("#content-body table tbody td:last-child button.drill")
    assert кнопки.count() == len(связи["items"]) > 0
    первая = связи["items"][0]
    assert _число(кнопки.first.inner_text()) == первая["n"] < связи["sampled"]
    кнопки.first.click()
    строк, подпись, заголовок = _раскрыто(с, "#content-body tr.drill-row")
    assert строк == min(50, первая["n"]) and f"из {первая['n']}" in подпись
    assert первая["x_title"] in заголовок and первая["y_title"] in заголовок
    # Коэффициент — не набор записей.
    assert с.locator("#content-body table tbody td:nth-child(2) button.drill").count() == 0
    вкладка.чисто()


def test_что_послушать_раскрывает_весь_отбор_а_не_первые_полсотни(вкладка):
    с = _на_вкладку(вкладка, "records", "#content-records table")
    # Отбор длиннее таблицы: «первые 50» и кнопка «весь список».
    assert "первые 50" in с.locator("#content-records .card-head .hint").inner_text()
    кнопка = с.locator("#content-records .card-head button.drill")
    assert кнопка.inner_text().strip() == "весь список"
    кнопка.click()
    строк, подпись, заголовок = _раскрыто(с, "#content-records [data-drill-host]")
    всего = _число(подпись.split("из")[1])
    assert строк == 50 < всего and "все записи периода" in заголовок
    # Порядок тот же, что в таблице раздела: сверху самая тяжёлая запись
    # (или равная ей по тональности — второй ключ порядка у раздела свой).
    в_списке = с.locator(
        "#content-records [data-drill-host] .drill-table > tbody > tr").first.locator(
        "td").last.inner_text()
    в_таблице = с.locator(
        "#content-records > section > .table-wrap tbody tr").first.locator("td").nth(3).inner_text()
    тон = [float(т.strip().split()[-1].replace(",", ".").replace("−", "-"))
           for т in (в_списке, в_таблице)]
    assert тон[0] == pytest.approx(тон[1], abs=0.006) and тон[0] < 0
    # Кнопка перехода в «Результаты» осталась как была.
    assert с.locator("#content-all").count() == 1
    # Короткий отбор уместился в таблицу целиком: раскрывается его число.
    с.click('#content-kind button[data-kind="mixed"]')
    с.wait_for_selector("#content-records .card-head button.drill")
    кнопка = с.locator("#content-records .card-head button.drill")
    записей = _число(кнопка.inner_text())
    assert записей == с.locator("#content-records > section > .table-wrap tbody tr").count() > 0
    кнопка.click()
    строк, _, _ = _раскрыто(с, "#content-records [data-drill-host]")
    assert строк == записей
    вкладка.чисто()


def test_кучка_тем_без_правил_раскрывается_в_свои_примеры(вкладка):
    """Кучка собирается по похожести, условия в базе у неё нет: под числом
    — записи, которые отчёт отдал сам, и их столько, сколько написано."""
    с = _на_вкладку(вкладка, "categories", "#topics-go")
    с.click("#topics-go")
    с.wait_for_selector("#topics-body table", timeout=30000)
    assert вкладка.раскрытий() == 0
    кнопка = с.locator("#topics-body table button.drill").first
    примеров = _число(кнопка.inner_text())
    размер = _число(кнопка.locator("xpath=ancestor::td").inner_text().split("·")[0])
    assert 0 < примеров == min(10, размер)
    кнопка.click()
    строк, _, заголовок = _раскрыто(с, "#topics-body tr.drill-row")
    assert строк == примеров and "Тема «" in заголовок
    if размер > примеров:
        assert f"первые {примеров} из {размер}" in заголовок
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Узкий экран
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("имя,ждать", [("agents", "#agents-table"),
                                       ("groups", "#chart-by-owner svg"),
                                       ("topics", "#chart-topics svg"),
                                       ("records", "#content-records table")])
def test_на_узком_экране_список_не_раздвигает_страницу(сервер, имя, ждать):
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = _на_вкладку(окно, имя, ждать)
        assert с.evaluate("document.documentElement.scrollWidth") <= 391
        # На узком экране кнопку перекрывает то липкая шапка, то панель
        # отборов: щелчок — событием, проверяем здесь не попадание мышью.
        с.locator("#content-body button.drill").first.dispatch_event("click")
        _раскрыто(с)
        assert с.evaluate("document.documentElement.scrollWidth") <= 391
        assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
            const р = п.closest('.table-wrap') || п;
            return р.getBoundingClientRect().right <= window.innerWidth + 1; }""")
        окно.чисто()
    finally:
        окно.закрыть()
