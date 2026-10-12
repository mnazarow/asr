"""Заход 49: раскрытие чисел «Голосовой аналитики» и «Очереди LLM» в браузере.

Сами числа сверены со списками на сервере (`test_drill_voice.py`). Здесь —
что раздел раскрывает их так, как обещано: плитка, строка графика, сектор
кольца, клетка таблицы; ссылка ведёт к записи; а «Очередь LLM», которая
перерисовывается сама раз в две с половиной секунды, раскрытый список при
этом не сносит.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_voice import _досеять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-voice-ui") / "data")
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


def _голос(вкладка, раздел: str = ""):
    с = вкладка.открыть("voice")
    с.select_option("#vo-period", "all")
    с.wait_for_selector("#vo-s1 tr.drill-item")
    # Свод приходит двумя частями; проверкам нужны обе.
    с.wait_for_function("() => (window.__asrhub.state.voice || {}).lists === 'ready'")
    с.wait_for_timeout(300)
    if раздел:
        с.click(f'#vo-tabs button[data-tab="{раздел}"]')
        с.wait_for_selector("#vo-body .card")
    return с, с.evaluate("() => window.__asrhub.state.voice")


def _строк(с, где: str = "") -> int:
    return с.locator(f"{где} .drill-panel tbody tr").count()


def _колонка(с, номер: int) -> set[str]:
    return set(с.evaluate("""(n) => Array.from(document.querySelectorAll(
        `.drill-panel tbody tr td:nth-child(${n})`)).map((я) => я.innerText.trim())""", номер))


# ---------------------------------------------------------------------------
# Голосовая аналитика
# ---------------------------------------------------------------------------


def test_пока_число_не_раскрыли_за_списком_не_ходят(вкладка):
    с, свод = _голос(вкладка)
    assert с.locator("#vo-head button.drill").count() == 4
    # Число-кнопка не меняет того, что читает человек и соседний тест.
    шапка = с.inner_text("#vo-head").lower()
    assert f"разговоров за период\n{свод['records']}\n" in шапка, шапка
    assert f"разобрано моделью\n{свод['analyzed']}\n" in шапка, шапка
    for раздел in ("reasons", "trackers", "scorecard", "records", "actions"):
        с.click(f'#vo-tabs button[data-tab="{раздел}"]')
        с.wait_for_selector("#vo-body .card")
    assert вкладка.раскрытий() == 0 and с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_плитки_шапки(вкладка):
    с, свод = _голос(вкладка)
    плитки = с.locator("#vo-head button.drill")
    # «Разговоров за период» — задания, без столбцов ответа модели.
    плитки.nth(0).click()
    с.wait_for_selector("#vo-head .drill-panel tbody tr")
    assert _строк(с) == min(50, свод["records"])
    assert "причина" not in с.locator(".drill-panel thead").inner_text().lower()
    assert с.evaluate("""() => document.querySelector('#vo-head .drill-panel')
        .previousElementSibling.classList.contains('grid')""")
    # «Разобрано моделью» — записи с ответом: причина и исход столбцами.
    плитки.nth(1).click()
    с.wait_for_function(
        "document.querySelector('.drill-title').innerText === 'Разобрано моделью'")
    с.wait_for_selector("#vo-head .drill-panel tbody tr")
    assert с.locator("#vo-head .drill-panel").count() == 1
    assert f"из {свод['analyzed']}" in с.locator(".drill-count").inner_text()
    assert "причина" in с.locator(".drill-panel thead").inner_text().lower()
    # Доля раскрывается числителем, «в N разговорах» — этими N разговорами.
    плитки.nth(3).click()
    с.wait_for_function(
        "document.querySelector('.drill-title').innerText === 'Разговоры с обязательствами'")
    с.wait_for_selector("#vo-head .drill-panel tbody tr")
    assert _число(плитки.nth(3).inner_text()) == свод["records_with_actions"]
    assert f"из {свод['records_with_actions']}" in с.locator(".drill-count").inner_text() \
        or _строк(с) == свод["records_with_actions"]
    плитки.nth(2).click()
    с.wait_for_function(
        "document.querySelector('.drill-title').innerText === 'Вопрос решён при обращении'")
    с.wait_for_selector("#vo-head .drill-panel tbody tr")
    assert 0 < _строк(с) < свод["analyzed"]
    вкладка.чисто()


def _заголовки(с, где: str) -> list[str]:
    return [т.strip().lower() for т in с.locator(f"{где} .drill-panel thead th").all_inner_texts()]


def test_причина_раскрывается_строкой_в_своём_блоке(вкладка):
    """Блок «Причины обращений» — перечень: строка с уголком и подчёркнутым
    числом, под ней по щелчку — её разговоры. Раньше здесь был график, по
    столбцу которого можно было щёлкнуть, но ничто на экране этого не
    говорило."""
    с, свод = _голос(вкладка)
    строки = с.locator("#vo-s1 tr.drill-item")
    assert строки.count() == len(свод["reasons"]) >= 2
    assert вкладка.раскрытий() == 0                      # до щелчка — ни запроса
    assert "раскрывает" in с.locator('section.card:has(#vo-s1) .hint').inner_text()
    первая, вторая = свод["reasons"][0], свод["reasons"][1]
    assert строки.first.get_attribute("aria-expanded") == "false"
    assert _число(строки.first.locator(".drill-num").inner_text()) == первая["records"]
    строки.first.click()
    с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
    assert строки.first.get_attribute("aria-expanded") == "true"
    assert с.locator("#vo-s1 .drill-title").inner_text() == f"Причина: {первая['key']}"
    assert _строк(с, "#vo-s1") == min(50, первая["records"])
    # Список — под своей строкой, внутри блока, со столбцами под его ширину:
    # причина у всех строк одна, вместо неё — исход.
    assert с.evaluate("""() => { const р = document.querySelector('#vo-s1 tr.drill-row');
        return р.previousElementSibling.classList.contains('drill-on')
          && р.getBoundingClientRect().width
             <= document.querySelector('#vo-s1').getBoundingClientRect().width + 1; }""")
    assert _заголовки(с, "#vo-s1") == ["когда", "запись", "длительность", "исход"]
    исходы = {и["key"] for и in свод["outcomes"]} | {"—"}
    assert _колонка(с, 4) <= исходы and _колонка(с, 4) - {"—"}
    assert вкладка.раскрытий() == 1
    # Вторая строка: раскрыта в блоке одна — первая сворачивается сама.
    строки.nth(1).click()
    с.wait_for_function("""(название) => { const т = document.querySelectorAll('#vo-s1 .drill-title');
        return т.length === 1 && т[0].innerText === название; }""",
                        arg=f"Причина: {вторая['key']}")
    с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
    assert _строк(с, "#vo-s1") == min(50, вторая["records"])
    assert строки.first.get_attribute("aria-expanded") == "false"
    # Повторный щелчок сворачивает; с клавиатуры строка раскрывается так же.
    строки.nth(1).click()
    assert с.locator("#vo-s1 .drill-panel").count() == 0
    строки.first.focus()
    с.keyboard.press("Enter")
    с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
    с.keyboard.press("Enter")
    assert с.locator("#vo-s1 .drill-panel").count() == 0
    вкладка.чисто()


def test_исход_раскрывается_строкой_и_сектором_кольца(вкладка):
    с, свод = _голос(вкладка)
    строки = с.locator("#vo-s2-list tr.drill-item")
    assert строки.count() == len(свод["outcomes"]) >= 2
    исход = свод["outcomes"][0]
    строки.first.click()
    с.wait_for_selector("#vo-s2-list .drill-panel tbody tr")
    assert с.locator("#vo-s2-list .drill-title").inner_text() == f"Исход: {исход['key']}"
    assert _строк(с, "#vo-s2-list") == min(50, исход["records"])
    assert _заголовки(с, "#vo-s2-list") == ["когда", "запись", "длительность", "причина"]
    причины = {п["key"] for п in свод["reasons"]} | {"—"}
    assert _колонка(с, 4) <= причины
    assert f"из {исход['records']}" in с.locator("#vo-s2-list .drill-count").inner_text() \
        or _строк(с, "#vo-s2-list") == исход["records"]
    # Ссылка открывает запись на вкладке разбора; список остаётся.
    ссылка = с.locator("#vo-s2-list .drill-panel a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    assert ссылка.get_attribute("href") == f"#/jobs/{номер}"
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert с.evaluate("location.hash") == "#voice"
    assert _строк(с, "#vo-s2-list") == min(50, исход["records"])
    с.keyboard.press("Escape")
    с.wait_for_selector(".modal-backdrop", state="detached")
    # Сектор кольца раскрывает ту же строку: второй сектор — вторую.
    второй = свод["outcomes"][1]
    с.locator("#vo-s2 svg path.pick").nth(1).dispatch_event("click")
    с.wait_for_function("""(название) => { const т = document.querySelectorAll('#vo-s2-list .drill-title');
        return т.length === 1 && т[0].innerText === название; }""",
                        arg=f"Исход: {второй['key']}")
    assert строки.nth(1).get_attribute("aria-expanded") == "true"
    # Причины при этом не трогаются: у каждого блока свой раскрытый список.
    с.locator("#vo-s1 tr.drill-item").first.click()
    с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
    assert с.locator(".drill-panel").count() == 2
    вкладка.чисто()


def test_раздел_рисуется_не_дожидаясь_второй_части_свода(сервер):
    """Обязательства, трекеры и скоркарту сервер считает, читая каждый
    ответ модели, — на большом архиве это десятки секунд. Причины и исходы
    их не ждут: раздел рисуется по первой части, вторая дорисовывает своё."""
    окно = Вкладка(сервер)
    try:
        окно.страница.add_init_script("""
            const настоящий = window.fetch;
            window.fetch = (адрес, ...прочее) => (String(адрес).includes('part=heavy')
              ? new Promise((готово) => setTimeout(готово, 2500)).then(() => настоящий(адрес, ...прочее))
              : настоящий(адрес, ...прочее));""")
        с = окно.открыть("voice")
        с.wait_for_selector("#vo-s1 tr.drill-item")
        assert с.evaluate("window.__asrhub.state.voice.lists") is None
        шапка = с.inner_text("#vo-head").lower()
        assert "обязательств\n…\n" in шапка and "разобрано моделью" in шапка, шапка
        # Строка раскрывается, пока вторая часть ещё считается.
        с.locator("#vo-s1 tr.drill-item").first.click()
        с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
        с.locator("#vo-head button.drill").first.click()
        с.wait_for_selector("#vo-head .drill-panel tbody tr")
        # Вкладка, которой нужна вторая часть, честно говорит, что считает.
        с.click('#vo-tabs button[data-tab="trackers"]')
        assert "считаем по всем разборам" in с.inner_text("#vo-body").lower()
        с.click('#vo-tabs button[data-tab="summary"]')
        с.wait_for_function("() => window.__asrhub.state.voice.lists === 'ready'")
        с.wait_for_function(
            "() => !document.querySelector('#vo-head').innerText.includes('…')")
        # Список, раскрытый под плитками, перерисовку шапки пережил.
        assert с.locator("#vo-head .drill-panel tbody tr").count() > 0
        с.click('#vo-tabs button[data-tab="trackers"]')
        с.wait_for_selector("#vo-t1 svg")
        адреса = [а for а in окно.запросы if "/api/content/llm" in а]
        assert len(адреса) == 2 and "part=fast" in адреса[0] and "part=heavy" in адреса[1]
        assert "since=" in адреса[1]
        окно.чисто()
    finally:
        окно.закрыть()


def test_клетка_причина_исход(вкладка):
    с, свод = _голос(вкладка, "reasons")
    кнопка = с.locator("#vo-body table").first.locator("button.drill").first
    сколько = _число(кнопка.inner_text())
    кнопка.click()
    с.wait_for_selector("#vo-body tr.drill-row .drill-panel tbody tr")
    assert _строк(с) == сколько
    причина, исход = с.locator(".drill-title").inner_text().split(" → ")
    assert _колонка(с, 7) == {причина} and _колонка(с, 8) == {исход}
    assert сколько == next(п["records"] for п in свод["pairs"]
                           if п["reason"] == причина and п["outcome"] == исход)
    # Столбец «всего» — сумма по показанным исходам: не раскрывается.
    assert с.locator("#vo-body table").first.locator(
        "tbody tr:not(.drill-row) td:last-child button.drill").count() == 0
    вкладка.чисто()


def test_трекер_и_пункт_скоркарты(вкладка):
    с, свод = _голос(вкладка, "trackers")
    с.wait_for_selector("#vo-t1 svg")
    трекер = свод["trackers"][0]
    assert трекер["fired"] > 0
    с.locator("#vo-t1 svg rect.pick").first.click()
    с.wait_for_selector(".drill-panel tbody tr")
    assert с.locator(".drill-title").inner_text() == f"Сработал трекер: {трекер['label']}"
    assert _строк(с) == min(50, трекер["fired"])
    с.click('#vo-tabs button[data-tab="scorecard"]')
    с.wait_for_selector("#vo-body table")
    assert с.locator(".drill-panel").count() == 0
    строка = с.locator("#vo-body tbody tr").first
    for столбец, начало in ((1, "Выполнено: "), (2, "Не выполнено: "), (3, "Неприменимо: ")):
        кнопка = строка.locator("td").nth(столбец).locator("button.drill")
        сколько = _число(кнопка.inner_text())
        кнопка.click()
        с.wait_for_function("(начало) => { const з = document.querySelector('.drill-title');"
                            " return з && з.innerText.startsWith(начало); }", arg=начало)
        с.wait_for_selector("#vo-body tr.drill-row .drill-panel tbody tr")
        assert _строк(с) == min(50, сколько), начало
        assert с.locator("#vo-body tr.drill-row").count() == 1
    вкладка.чисто()


def test_записи_и_сбои_разбора(вкладка):
    с, свод = _голос(вкладка, "records")
    кнопки = с.locator("#vo-body .card-head button.drill")
    assert _число(кнопки.nth(0).inner_text()) == свод["rows_total"]
    assert _число(кнопки.nth(1).inner_text()) == свод["errors"] > 0
    кнопки.nth(1).click()
    с.wait_for_selector("#vo-body .drill-panel tbody tr")
    assert с.locator(".drill-title").inner_text() == "Сбои разбора"
    assert _строк(с) == свод["errors"]
    # Список — сразу под шапкой карточки, над таблицей в триста строк.
    assert с.evaluate("""() => document.querySelector('#vo-body .drill-panel')
        .previousElementSibling.classList.contains('card-head')""")
    кнопки.nth(0).click()
    с.wait_for_function(
        "document.querySelector('.drill-title').innerText === 'Записи с ответом модели'")
    с.wait_for_selector("#vo-body .drill-panel tbody tr")
    assert с.locator("#vo-body .drill-panel").count() == 1
    assert f"из {свод['rows_total']}" in с.locator(".drill-count").inner_text()
    вкладка.чисто()


def test_на_узком_экране_список_не_раздвигает_страницу(сервер):
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = окно.открыть("voice")
        с.select_option("#vo-period", "all")
        с.wait_for_selector("#vo-s1 tr.drill-item")
        до = с.evaluate("document.documentElement.scrollWidth")
        с.locator("#vo-head button.drill").nth(1).click()
        с.wait_for_selector(".drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= max(до, 391)
        # И список под строкой перечня — тоже: он листается внутри себя.
        с.locator("#vo-s1 tr.drill-item").first.click()
        с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= max(до, 391)
        окно.чисто()
    finally:
        окно.закрыть()


# ---------------------------------------------------------------------------
# Очередь LLM
# ---------------------------------------------------------------------------


def _очередь(вкладка):
    с = вкладка.открыть("llmqueue")
    с.wait_for_selector("#lq-top .kpi")
    с.wait_for_selector("#lq-c1 svg")
    return с


def test_очередь_сама_за_списками_не_ходит(вкладка):
    """Раздел опрашивает сервер каждые две с половиной секунды; ни один
    опрос не должен тянуть за собой списки."""
    с = _очередь(вкладка)
    с.wait_for_timeout(5500)
    assert с.locator("#lq-top button.drill").count() == 3
    assert вкладка.раскрытий() == 0
    # Состояние очереди, скорость и настройки — не записи: не раскрываются.
    подписи = с.evaluate("""() => Array.from(document.querySelectorAll('#lq-top .kpi'))
        .filter((п) => п.querySelector('button.drill'))
        .map((п) => п.querySelector('.kpi-label').innerText.toLowerCase())""")
    assert подписи == ["разобрано за окно", "сбоев"]
    вкладка.чисто()


def test_раскрытый_список_переживает_перерисовку_очереди(вкладка):
    с = _очередь(вкладка)
    сбоев = с.locator("#lq-top .kpi", has_text="Сбоев").locator("button.drill")
    сколько = _число(сбоев.inner_text())
    сбоев.click()
    с.wait_for_selector("#lq-top .drill-panel tbody tr")
    assert _строк(с, "#lq-top") == min(50, сколько)
    метка = с.evaluate("""() => { const п = document.querySelector('#lq-top .drill-panel');
        п.dataset.mark = 'та же'; return п.dataset.mark; }""")
    опросов = sum(1 for адрес in вкладка.запросы if "/api/llm/queue?" in адрес)
    с.wait_for_timeout(6000)
    # Опрос шёл, очередь под плитками перерисовывалась, а список — тот же узел.
    assert sum(1 for адрес in вкладка.запросы if "/api/llm/queue?" in адрес) >= опросов + 2
    assert с.evaluate("document.querySelector('#lq-top .drill-panel').dataset.mark") == метка
    assert _строк(с, "#lq-top") == min(50, сколько)
    assert _число(сбоев.inner_text()) == сколько
    assert с.locator("#lq-top .lq-held").count() == 1
    assert вкладка.раскрытий() == 1
    # Свернули — плитки снова живые: пометка «числа стоят» уходит с опросом.
    с.locator("#lq-top .drill-close").click()
    с.wait_for_function("!document.querySelector('#lq-top .lq-held')", timeout=8000)
    # «Разобрано за окно» = успешные + сбои, и раскрывается этим же набором.
    всего = с.locator("#lq-top .kpi", has_text="Разобрано за окно").locator("button.drill")
    разобрано, успешно = _число(всего.nth(0).inner_text()), _число(всего.nth(1).inner_text())
    assert разобрано == успешно + сколько
    всего.nth(0).click()
    с.wait_for_selector("#lq-top .drill-panel tbody tr")
    assert f"из {разобрано}" in с.locator("#lq-top .drill-count").inner_text() \
        or _строк(с, "#lq-top") == разобрано
    # «Обновить» — просьба пересчитать всё: список закрывается.
    с.click("#lq-refresh")
    с.wait_for_function("!document.querySelector('#lq-top .drill-panel')", timeout=8000)
    вкладка.чисто()


def test_столбец_графика_очереди_раскрывается_своей_корзиной(вкладка):
    с = _очередь(вкладка)
    # Опрос останавливаем: корзины ползут вместе с окном, и число под
    # столбцом иначе пришлось бы ловить между двумя перерисовками.
    с.evaluate("() => { window.__asrhub.RENDERERS.llmqueue.load = async () => {}; }")
    с.wait_for_timeout(300)
    ряд = с.evaluate("() => window.__asrhub.state.llmQueue.series")
    кусок = с.locator("#lq-c1 svg path.pick").first
    кусок.click()
    с.wait_for_selector("#lq-chart .drill-panel tbody tr")
    описание = с.evaluate(
        "() => JSON.parse(document.querySelector('#lq-chart .drill-panel').dataset.key)")
    вид, промежуток = описание["f"][0].split(":", 1)
    от, до = (float(граница) for граница in промежуток.split(".."))
    номер = round((от - ряд["since"]) / ряд["bucket_seconds"])
    assert до - от == pytest.approx(ряд["bucket_seconds"])
    корзина = next(к for к in ряд["rows"] if к["bucket"] == номер)
    ожидается = корзина["ok"] if вид == "llmq_ok" else корзина["failed"]
    assert ожидается > 0 and _строк(с, "#lq-chart") == ожидается
    assert "since" not in описание and "until" not in описание
    # Под рядом из двух графиков, во всю ширину.
    assert с.evaluate("""() => { const п = document.querySelector('#lq-chart .drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    ссылка = с.locator("#lq-chart .drill-panel a.drill-open").first
    номер_записи = ссылка.get_attribute("data-open")
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер_записи
    вкладка.чисто()
