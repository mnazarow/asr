"""Заход 49, раздел «Аналитика»: раскрытие чисел — как его видит человек.

Серверная сверка (`test_drill_analytics.py`) проверяет отборы. Здесь — что
сам раздел их так и шлёт: плитка, строка таблицы, точка ряда, столбец,
клетка карты раскрываются в список той длины, что обещает число; список
встаёт туда, где его видно; пока ничего не раскрыто, страница за списками
не ходит; а на экране телефона ничего не уезжает вбок.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_analytics import досеять

СТРОКИ = ".drill-panel .drill-table > tbody > tr"


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-analytics-ui") / "data",
                      заданий=140, с_моделью=0)
    досеять(хранилище.db, хранилище.данные["now"])
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


def _открыть(окно: Вкладка) -> Any:
    с = окно.открыть("analytics")
    с.wait_for_selector("#chart-flow svg", timeout=20000)
    с.wait_for_selector("#table-tags table", timeout=20000)
    return с


def _свод(с: Any, путь: str) -> Any:
    """Число из последнего ответа свода — того, что раздел и нарисовал."""
    return с.evaluate(f"() => window.__asrhub.state.analytics.{путь}")


def _панель(с: Any, было: int = 0) -> dict[str, Any]:
    """Ждёт список и отдаёт, что в нём: строки, счёт, заголовок, место."""
    с.wait_for_function(
        """(было) => { const п = document.querySelectorAll('.drill-panel')[было];
            return п && (п.querySelector('.drill-table > tbody > tr')
              || (п.querySelector('.drill-foot').textContent || '').includes('нет')); }""",
        arg=было, timeout=15000)
    return с.evaluate(
        """(было) => { const п = document.querySelectorAll('.drill-panel')[было];
            const счёт = п.querySelector('.drill-count').textContent;
            const всего = /из ([\\d\\s]+)/.exec(счёт);
            return { rows: п.querySelectorAll('.drill-table > tbody > tr').length,
                     total: всего ? Number(всего[1].replace(/\\s/g, '')) : null,
                     title: п.querySelector('.drill-title').textContent,
                     head: Array.from(п.querySelectorAll('.drill-table th')).map((я) => я.textContent),
                     sort: п.querySelector('.drill-sort').value,
                     inRow: Boolean(п.closest('tr.drill-row')),
                     width: Math.round(п.getBoundingClientRect().width),
                     visible: п.getBoundingClientRect().top < window.innerHeight }; }""",
        было)


def _число(текст: str) -> int:
    return int(re.sub(r"\D", "", текст))


def _отборы(окно: Вкладка) -> dict[str, list[str]]:
    """Что ушло на сервер последним запросом списка."""
    адреса = [а for а in окно.запросы if "/api/drill?" in а]
    return parse_qs(urlsplit(адреса[-1]).query)


def _свернуть(с: Any) -> None:
    с.locator(".drill-close").first.click()
    с.wait_for_function("document.querySelectorAll('.drill-panel').length === 0")


# ---------------------------------------------------------------------------
# Пока не раскрыли — ни одного запроса
# ---------------------------------------------------------------------------


def test_раздел_рисуется_без_единого_запроса_за_списком(вкладка):
    """Чисел-кнопок в разделе под две сотни, графиков с выбором — полтора
    десятка. Всё это — разметка и описание отбора в атрибуте; списки не
    вложены в ответ свода и не запрашиваются, пока по числу не щёлкнули."""
    с = _открыть(вкладка)
    assert с.locator("#analytics-body button.drill").count() > 120
    for график in ("#chart-flow", "#chart-rtf", "#chart-status", "#chart-stages", "#chart-conf",
                   "#chart-dur", "#chart-hours", "#chart-lang", "#chart-owner", "#chart-weekly",
                   "#chart-quality", "#chart-quality2", "#chart-suspect-flags",
                   "#spc-a-confidence"):
        assert с.locator(f"{график} .pick").count() >= 1, график
    # Замеры железа и очередь по замерам — не задания: раскрывать нечего.
    assert с.locator("#chart-system .pick, #chart-queue .pick").count() == 0
    assert вкладка.раскрытий() == 0
    assert с.locator(".drill-panel").count() == 0
    тело = с.inner_text("#analytics-body")
    assert "undefined" not in тело and "NaN" not in тело
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Число = список
# ---------------------------------------------------------------------------

#: Число-кнопка (её текст — счёт заданий) и где обязан встать список:
#: `строка` — строкой под своей в таблице во всю ширину; `ряд` — под рядом
#: карточек или под блоком во всю ширину (таблица в узкой карточке и плитки).
ЧИСЛА = [
    ("из кеша", "#analytics-body > .grid.cols-6 .kpi:nth-child(2) .kpi-sub button.drill", "ряд"),
    ("низких", "#analytics-body > .grid.cols-6 .kpi:nth-child(6) .kpi-sub button.drill", "ряд"),
    ("неудач", "#errors-body > .row .chip.err button.drill", "ряд"),
    ("ошибка по коду", "#errors-body .card.tight .row button.drill", "ряд"),
    ("ошибка на модели", "#errors-body .card.tight .faint button.drill", "ряд"),
    ("попаданий в кеш", "#efficiency-body tr:nth-child(5) button.drill", "ряд"),
    ("пик недели", "#chart-weekly button.drill", "ряд"),
    ("очередь: дольше минуты", "#queue-body .kpi-sub button.drill", "ряд"),
    ("очередь: приоритет", "#queue-body tbody tr:nth-child(1) button.drill", "ряд"),
    ("очередь: источник", "#queue-body tbody tr:nth-child(4) button.drill", "ряд"),
    ("устройство", "#resources-body table:nth-of-type(1) tbody button.drill", "ряд"),
    ("заданий разом", "#resources-body table:nth-of-type(3) tbody button.drill", "ряд"),
    ("с признаками", "#suspicious-body .kpi:nth-child(1) .kpi-value button.drill", "ряд"),
    ("не проверены", "#suspicious-body .kpi:nth-child(3) .kpi-sub button.drill", "ряд"),
    ("подозрительные по модели", "#suspicious-body table tbody tr:nth-child(1) button.drill", "ряд"),
    ("дрейф: период", "#drift-body tbody tr:nth-child(1) td:nth-child(3) button.drill", "ряд"),
    ("эталон: всего", "#accuracy-body .kpi-sub button.drill", "ряд"),
    ("эталон: модель", "#accuracy-body table tbody button.drill", "ряд"),
    ("калибровка: записей", "#calibration-body p button.drill", "ряд"),
    ("латентность: модель", "#latency-body table tbody button.drill", "ряд"),
    ("с первой попытки", "#reliability-body tr:nth-child(2) button.drill", "ряд"),
    ("кто отменял", "#reliability-body > .small button.drill", "ряд"),
    ("повторы файла", "#cache-body table tbody button.drill", "ряд"),
    ("формат", "#audio-body table tbody button.drill", "строка"),
    ("говорящих", "#audio-body > [data-drill-host] > .small button.drill", "ряд"),
    ("плохой звук по источнику", "#audio-body div > [data-drill-host] > .small button.drill", "ряд"),
    ("шумных", "#audio-body .grid.cols-4 .kpi:nth-child(2) .kpi-sub button.drill", "ряд"),
    ("метка: заданий", "#table-tags tbody tr:nth-child(1) td:nth-child(2) button.drill", "строка"),
    ("метка: ошибок", "#table-tags tbody tr:nth-child(1) td:nth-child(4) button.drill", "строка"),
    ("модель: готово", "#table-models tbody tr:nth-child(1) td:nth-child(3) button.drill", "строка"),
]


def test_каждое_число_раскрывается_в_список_своей_длины(вкладка):
    """По одному числу каждого блока: в списке столько строк, сколько оно
    обещает (или первая страница), и «из N» под заголовком — то же N."""
    с = _открыть(вкладка)
    for раз, (имя, отбор, место) in enumerate(ЧИСЛА, start=1):
        кнопка = с.locator(отбор).first
        assert кнопка.count() == 1, f"нет числа: {имя}"
        if имя == "модель: готово":
            # У доли раскрывается её числитель: готовые задания модели.
            обещано = _свод(с, "models[0].completed")
        else:
            обещано = _число(кнопка.inner_text())
        assert обещано > 0, имя
        кнопка.click()
        панель = _панель(с)
        assert панель["rows"] == min(50, обещано), (имя, панель)
        assert панель["total"] == обещано, (имя, панель)
        assert вкладка.раскрытий() == раз, имя
        assert кнопка.get_attribute("aria-expanded") == "true"
        # Список виден без прокрутки руками и стоит там, где ему хватает места.
        assert панель["visible"], (имя, панель)
        assert панель["inRow"] == (место == "строка"), (имя, панель)
        assert панель["width"] > 900, (имя, панель)
        кнопка.click()
        assert с.locator(".drill-panel").count() == 0, имя
        assert кнопка.get_attribute("aria-expanded") == "false"
    вкладка.чисто()


def test_среднее_раскрывается_списком_по_своей_величине(вкладка):
    """«Средний RTF 0,18» — это не счёт. Раскрывается в задания, по которым
    оно посчитано, по убыванию RTF: сверху те, кто его делает."""
    с = _открыть(вкладка)
    с.locator("#analytics-body > .grid.cols-6 .kpi:nth-child(4) .kpi-value button.drill").click()
    панель = _панель(с)
    assert панель["total"] == _свод(с, "overview.performance.rtf.count")
    assert панель["head"][-1] == "RTF" and панель["sort"] == "rtf:desc"
    значения = с.evaluate(
        f"""() => Array.from(document.querySelectorAll('{СТРОКИ} td:last-child'))
              .map((я) => Number(я.textContent.replace(',', '.')))""")
    assert значения == sorted(значения, reverse=True) and значения[0] > значения[-1]
    запрос = _отборы(вкладка)
    assert запрос["f"] == ["status:completed", "nz:rtf"] and запрос["sort"] == ["rtf:desc"]
    # Окно — из ответа свода, а не «неделя до сейчас», посчитанная заново.
    assert float(запрос["since"][0]) == _свод(с, "window.since")
    assert float(запрос["until"][0]) == _свод(с, "window.until")
    _свернуть(с)

    # Уверенность: плитка отбрасывает нули (`nz:`), список — снизу вверх.
    с.locator("#analytics-body > .grid.cols-6 .kpi:nth-child(6) .kpi-value button.drill").click()
    assert _панель(с)["total"] == _свод(с, "overview.quality.confidence.count")
    запрос = _отборы(вкладка)
    assert запрос["f"] == ["status:completed", "nz:confidence"]
    assert запрос["sort"] == ["confidence:asc"]
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Графики
# ---------------------------------------------------------------------------


def _щёлкнуть_точку(с: Any, график: str, номер: int, всего: int) -> None:
    """Щелчок по вертикали точки линейного графика."""
    слой = с.locator(f"{график} svg rect.pick")
    слой.scroll_into_view_if_needed()
    рамка = слой.bounding_box()
    x = рамка["width"] * номер / max(1, всего - 1)
    слой.click(position={"x": min(max(x, 2), рамка["width"] - 2), "y": рамка["height"] / 2})


def test_точка_ряда_раскрывается_в_задания_своей_корзины(вкладка):
    с = _открыть(вкладка)
    ряд = _свод(с, "timeseries")
    поток = [а + б for а, б in zip(ряд["completed"], ряд["failed"], strict=True)]
    номер = max(range(len(поток)), key=lambda i: поток[i])
    _щёлкнуть_точку(с, "#chart-flow", номер, len(поток))
    панель = _панель(с)
    assert панель["total"] == поток[номер] and панель["rows"] == min(50, поток[номер])
    assert панель["title"].startswith("Готовые и упавшие задания")
    assert not панель["inRow"] and панель["width"] > 900
    запрос = _отборы(вкладка)
    # Границы корзины — те, что отдал свод, до последнего знака.
    assert float(запрос["since"][0]) == ряд["edges"][номер]
    assert float(запрос["until"][0]) == ряд["edges"][номер + 1]
    assert запрос["f"] == ["status:completed,failed"]
    # Та же точка ещё раз — свернуть.
    _щёлкнуть_точку(с, "#chart-flow", номер, len(поток))
    assert с.locator(".drill-panel").count() == 0

    # Пустая точка не открывает ничего и никуда не ходит.
    пустая = поток.index(0)
    было = вкладка.раскрытий()
    _щёлкнуть_точку(с, "#chart-flow", пустая, len(поток))
    с.wait_for_timeout(300)
    assert с.locator(".drill-panel").count() == 0 and вкладка.раскрытий() == было
    вкладка.чисто()


def test_у_линии_с_двумя_рядами_ряд_выбирается_под_графиком(вкладка):
    """Средний RTF и среднее ожидание сложены из разных заданий: под одной
    точкой — два разных списка, и какой открыть, выбирает переключатель."""
    с = _открыть(вкладка)
    ряд = _свод(с, "timeseries")
    номер = max(i for i, v in enumerate(ряд["rtf"]) if v is not None)
    _щёлкнуть_точку(с, "#chart-rtf", номер, len(ряд["rtf"]))
    панель = _панель(с)
    assert панель["head"][-1] == "RTF" and "по RTF" in панель["title"]
    assert _отборы(вкладка)["f"] == ["status:completed", "nz:rtf"]
    с.locator("#chart-rtf + .row button[data-series='1']").click()
    assert "active" in с.locator("#chart-rtf + .row button[data-series='1']").get_attribute("class")
    _щёлкнуть_точку(с, "#chart-rtf", номер, len(ряд["rtf"]))
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.includes('ожиданию')")
    панель = _панель(с)
    assert панель["head"][-1] == "Ожидание, с" and панель["sort"] == "wait:desc"
    # Ожидание свод берёт «где записано», включая ноль, — не `nz:`.
    assert _отборы(вкладка)["f"] == ["status:completed", "has:wait"]
    assert с.locator(".drill-panel").count() == 1
    вкладка.чисто()


def test_столбцы_клетки_и_строки_графиков(вкладка):
    """По одному графику каждого вида: число под указателем = длина списка."""
    с = _открыть(вкладка)

    def раскрыть(цель: Any, обещано: int, что: str) -> dict[str, Any]:
        цель.scroll_into_view_if_needed()
        цель.click(force=True)
        панель = _панель(с)
        assert панель["total"] == обещано and панель["rows"] == min(50, обещано), (что, панель)
        assert not панель["inRow"] and панель["width"] > 900, (что, панель)
        цель.click(force=True)                               # тот же ещё раз — свернуть
        с.wait_for_function("document.querySelectorAll('.drill-panel').length === 0")
        return панель

    # Столбцы: распределение уверенности и длительность записей.
    корзины = [к for к in _свод(с, "overview.quality.confidence_distribution") if к["count"]]
    раскрыть(с.locator("#chart-conf svg rect.pick").last, корзины[-1]["count"], "уверенность")
    # Верх последней корзины входит в неё: диапазон сверху открыт.
    assert _отборы(вкладка)["f"][-1] == f"confidence:{корзины[-1]['from']}.."
    длины = _свод(с, "durations")
    непустые = [i for i, сколько in enumerate(длины["counts"]) if сколько]
    раскрыть(с.locator("#chart-dur svg rect.pick").first, длины["counts"][непустые[0]], "длина")
    assert _отборы(вкладка)["f"][-1] \
        == f"duration:{длины['edges'][непустые[0]]}..{длины['edges'][непустые[0] + 1]}"

    # Тепловая полоса: час суток.
    часы = _свод(с, "profile.hours")
    занятые = [ч for ч, сколько in enumerate(часы) if сколько]
    раскрыть(с.locator("#chart-hours svg rect.pick").nth(1), часы[занятые[1]], "час")
    assert _отборы(вкладка)["f"] == [f"hour:{занятые[1]}"]

    # Карта недели: клетка пика; клетка с нулём не раскрывается.
    пик = _свод(с, "weekly.peak")
    клетки = с.locator("#chart-weekly svg rect")
    раскрыть(клетки.nth(пик["day"] * 24 + пик["hour"]), пик["jobs"], "клетка")
    assert _отборы(вкладка)["f"] == [f"weekday:{пик['day']}", f"hour:{пик['hour']}"]
    неделя = _свод(с, "weekly.jobs")
    пусто = next((д, ч) for д in range(7) for ч in range(24) if not неделя[д][ч])
    было = вкладка.раскрытий()
    пустая = клетки.nth(пусто[0] * 24 + пусто[1])
    # Она и не кнопка: ни курсора, ни строки «щелчок — список» в подсказке.
    assert "pick" not in (пустая.get_attribute("class") or "")
    пустая.click(force=True)
    с.wait_for_timeout(300)
    assert с.locator(".drill-panel").count() == 0 and вкладка.раскрытий() == было

    # Строки: языки (пустой язык — «Язык не определён») и признаки.
    языки = _свод(с, "languages")
    номер = next(i for i, я in enumerate(языки) if я["key"] == "Язык не определён")
    раскрыть(с.locator("#chart-lang svg rect.pick").nth(номер), языки[номер]["jobs"], "язык")
    assert _отборы(вкладка)["f"] == ["language:"]
    признаки = _свод(с, "suspicious.by_flag")
    раскрыть(с.locator("#chart-suspect-flags svg rect.pick").first, признаки[0]["jobs"], "признак")
    assert _отборы(вкладка)["f"] == ["status:completed", "nz:segments", f"flag:{признаки[0]['key']}"]

    # Доля полосы этапов — через строку легенды: список по секундам этапа.
    этапы = _свод(с, "overview.stages")
    номер = этапы["keys"].index("inference_s")
    с.locator("#chart-stages .chart-legend .pick").nth(
        sum(1 for секунд in этапы["seconds"][:номер] if секунд)).click()
    панель = _панель(с)
    assert панель["sort"] == "inference:desc" and "Распознавание" in панель["title"]
    assert _отборы(вкладка)["f"] == ["status:completed", "nz:inference"]
    вкладка.чисто()


def test_ход_качества_и_контрольная_карта(вкладка):
    с = _открыть(вкладка)
    ход = _свод(с, "quality_trend")
    номер = max(range(len(ход["jobs"])), key=lambda i: ход["jobs"][i])
    _щёлкнуть_точку(с, "#chart-quality", номер, len(ход["jobs"]))
    панель = _панель(с)
    assert панель["total"] == ход["jobs"][номер] and панель["sort"] == "confidence:asc"
    запрос = _отборы(вкладка)
    assert запрос["f"] == ["status:completed", "has:confidence"]
    assert float(запрос["since"][0]) == ход["edges"][номер]
    _свернуть(с)

    карты = _свод(с, "control")
    точки = next(к["points"] for к in карты["charts"] if к["key"] == "low_confidence_share")
    номер = max(i for i, т in enumerate(точки) if т["value"])
    _щёлкнуть_точку(с, "#spc-a-low_confidence_share", номер, len(точки))
    панель = _панель(с)
    assert панель["total"] and панель["title"].startswith("Задания с уверенностью ниже 0,75")
    запрос = _отборы(вкладка)
    assert запрос["f"] == ["status:completed", "has:confidence", "low_confidence"]
    assert float(запрос["since"][0]) == карты["edges"][номер]
    assert float(запрос["until"][0]) == карты["edges"][номер + 1]
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Ссылки на записи
# ---------------------------------------------------------------------------


def test_из_списка_и_из_таблицы_медленных_открывается_карточка(вкладка):
    с = _открыть(вкладка)
    # «Самые медленные» — готовый список: имя файла теперь ссылка на запись.
    ссылка = с.locator("#table-slow a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    assert ссылка.get_attribute("href") == f"#/jobs/{номер}"
    assert номер == _свод(с, "slowest[0].id")
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert вкладка.раскрытий() == 0
    с.keyboard.press("Escape")
    с.wait_for_function("!document.querySelector('.modal-backdrop')")

    # А десятку продолжает список под таблицей — и из него открывается карточка.
    с.locator("#table-slow button.drill").click()
    assert _панель(с)["total"] == _свод(с, "overview.performance.rtf.count")
    # Верх списка — тот же RTF, что у первой строки таблицы (при равных RTF
    # порядок между ними у таблицы и у списка свой).
    первая = с.locator(f"{СТРОКИ} td:last-child").first.inner_text()
    assert float(первая.replace(",", ".")) == pytest.approx(_свод(с, "slowest[0].rtf"), abs=5e-4)
    запись = с.locator(".drill-panel a.drill-open").first
    открываем = запись.get_attribute("data-open")
    запись.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == открываем
    assert с.evaluate("location.hash") == "#analytics"
    assert с.locator(СТРОКИ).count() == 50
    вкладка.чисто()


# ---------------------------------------------------------------------------
# Узкий экран
# ---------------------------------------------------------------------------


def test_на_телефоне_раздел_не_прокручивается_вбок(сервер):
    """Таблица про звук лежала без обёртки и глубже, чем достаёт правило для
    таблиц без неё: шесть числовых столбцов раздвигали страницу до 510 px."""
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = _открыть(окно)
        assert с.evaluate("document.documentElement.scrollWidth") <= 391
        assert с.locator("#audio-body table").count() == 2
        assert с.evaluate("""() => Array.from(document.querySelectorAll('#audio-body table'))
            .every((т) => т.closest('.table-wrap'))""")
        # И с раскрытым списком тоже: плитка, строка широкой таблицы, строка
        # таблицы в карточке, точка графика.
        for отбор in ("#audio-body .kpi button.drill", "#audio-body table button.drill",
                      "#efficiency-body button.drill", "#queue-body table button.drill",
                      "#table-tags button.drill"):
            кнопка = с.locator(отбор).first
            кнопка.scroll_into_view_if_needed()
            кнопка.click()
            панель = _панель(с)
            assert панель["rows"] >= 1, отбор
            assert с.evaluate("document.documentElement.scrollWidth") <= 391, отбор
            кнопка.click()
            assert с.locator(".drill-panel").count() == 0, отбор
        окно.чисто()
    finally:
        окно.закрыть()


def test_на_среднем_экране_список_в_таблице_не_раздвигает_страницу(сервер):
    """Между 900 и 980 px карточки идут в один столбец, а таблицы ещё не
    прокручиваются сами: список шире таблицы растягивал ячейку, за ней
    карточку и документ. Теперь он занимает ширину таблицы."""
    окно = Вкладка(сервер, ширина=940, высота=900)
    try:
        с = _открыть(окно)
        до = с.evaluate("document.documentElement.scrollWidth")
        for отбор in ("#efficiency-body button.drill", "#queue-body table button.drill",
                      "#drift-body button.drill", "#latency-body table button.drill"):
            кнопка = с.locator(отбор).first
            кнопка.scroll_into_view_if_needed()
            кнопка.click()
            панель = _панель(с)
            # Карточка во всю ширину — список остаётся строкой под своей.
            assert панель["inRow"] and панель["rows"] >= 1, отбор
            assert с.evaluate("document.documentElement.scrollWidth") <= до, отбор
            кнопка.click()
        окно.чисто()
    finally:
        окно.закрыть()
