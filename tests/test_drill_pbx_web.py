"""Заход 49: раскрытие чисел «АТС» и «Телефонии» — как это видит человек.

Серверная сверка (`test_drill_pbx.py`) доказывает, что список под числом
тот же, что само число. Здесь — что до списка можно добраться: плитка,
ячейка таблицы, столбец, точка линии, клетка карты и доля полосы
раскрываются щелчком, в списке столько строк, сколько обещано, запись
открывается по ссылке, а пока ничего не раскрыто, раздел за списками не
ходит вовсе.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_pbx import досеять

ДАННЫЕ = "window.__asrhub.state.pbxData"


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    каталог = tmp_path_factory.mktemp("drill-pbx-ui")
    хранилище = Архив(каталог / "data", заданий=48, с_моделью=0)
    досеять(хранилище, каталог)
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
    return int("".join(з for з in текст if з.isdigit()))


def _на_вкладку(с: Any, имя: str) -> None:
    с.click(f'#pbx-tabs button[data-tab="{имя}"]')
    с.wait_for_timeout(500)


def _строк(с: Any, где: str = "") -> int:
    return с.locator(f"{где} .drill-panel tbody tr".strip()).count()


def _сколько_обещано(с: Any, где: str = "") -> int:
    """Число из подписи панели «показано N из M» (или просто N)."""
    подпись = с.locator(f"{где} .drill-count".strip()).first.inner_text()
    числа = [_число(ч) for ч in re.findall(r"\d[\d\s  ]*", подпись)]
    return числа[-1]


def _раскрыть_метку(с: Any, метка: Any, где: str = "") -> None:
    """Щелчок по метке графика. Прозрачная цель щелчка лежит поверх
    столбца, и «видимой» её Playwright не считает — щелчок шлём сами."""
    метка.dispatch_event("click")
    с.wait_for_selector(f"{где} .drill-panel tbody tr".strip())


# ---------------------------------------------------------------------------
# Ни одного запроса, пока не раскрыли
# ---------------------------------------------------------------------------


def test_пока_число_не_раскрыли_за_списком_не_ходят(вкладка):
    """В разделе «АТС» раскрывается под сотню чисел и две сотни меток на
    графиках. Сходи страница за каждым — это и было бы «тормозит на большом
    количестве звонков»."""
    с = вкладка.открыть("pbx")
    assert с.locator("#pbx-top button.drill").count() >= 10
    кнопок = с.locator("#pbx-body button.drill").count()
    меток = 0
    for имя in ("overview", "load", "people", "numbers", "intake", "agents"):
        _на_вкладку(с, имя)
        кнопок += с.locator("#pbx-body button.drill").count()
        меток += с.locator("#pbx-body .pick").count()
    assert кнопок >= 40 and меток >= 100, (кнопок, меток)
    с = вкладка.открыть("telephony")
    assert с.locator("#tel-status button.drill").count() >= 6
    assert вкладка.раскрытий() == 0
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


# ---------------------------------------------------------------------------
# «АТС»
# ---------------------------------------------------------------------------


def test_плитка_шапки_раскрывается_списком_звонков(вкладка):
    с = вкладка.открыть("pbx")
    число = с.locator("#pbx-top .kpi button.drill").first
    всего = _число(число.inner_text())
    assert всего == с.evaluate(f"{ДАННЫЕ}.period_calls.total") > 50
    число.click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    assert _строк(с, "#pbx-top") == 50
    assert _сколько_обещано(с) == всего
    assert с.locator(".drill-title").inner_text() == "Звонки за период"
    # Панель — внутри карточки «Станции», под рядом плиток, во всю ширину.
    assert с.evaluate("""() => { const п = document.querySelector('#pbx-top .drill-panel');
        return п.parentElement.classList.contains('card')
          && п.getBoundingClientRect().width > 900; }""")
    assert вкладка.раскрытий() == 1
    # Смена вкладки шапку не перерисовывает: список остаётся.
    _на_вкладку(с, "people")
    assert _строк(с, "#pbx-top") == 50
    число.click()
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_звонок_с_записью_открывается_а_у_пропущенного_причина(вкладка):
    с = вкладка.открыть("pbx")
    # «Распознано»: у каждого звонка есть задание — и ссылка на его карточку.
    с.locator("#pbx-top .kpi", has_text="Распознано").locator("button.drill").click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    assert с.locator(".drill-title").inner_text() == "Звонки, поставленные на распознавание"
    строк = _строк(с, "#pbx-top")
    assert строк == с.evaluate(f"{ДАННЫЕ}.period_calls.queued") > 5
    ссылки = с.locator("#pbx-top .drill-panel a.drill-open")
    # Одно задание удалено: звонок в списке есть, ссылки у него нет.
    assert ссылки.count() == строк - 1
    номер = ссылки.first.get_attribute("data-open")
    assert ссылки.first.get_attribute("href") == f"#/jobs/{номер}"
    ссылки.first.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert с.evaluate("location.hash") == "#pbx"
    assert _строк(с, "#pbx-top") == строк, "карточка поверх раздела список не закрывает"
    с.keyboard.press("Escape")
    # «Пропущено»: ссылок нет, вместо них причина.
    с.wait_for_selector(".modal-backdrop", state="detached")
    с.locator("#pbx-top .kpi", has_text="Пропущено").locator("button.drill").click()
    с.wait_for_function("""() => document.querySelector('#pbx-top .drill-title')
        .innerText.startsWith('Пропущенные')""")
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    assert с.locator("#pbx-top .drill-panel a.drill-open").count() == 0
    причины = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#pbx-top .drill-panel tbody tr td:last-child')).map((я) => я.innerText.trim())"""))
    assert причины == {"короче 5 с", "без ответа", "нет записи",
                       "нет записи: каталог пуст"}, причины
    вкладка.чисто()


def test_ожидание_показано_столбцом_и_упорядочено(вкладка):
    """Список «сначала ждавшие дольше» без самого ожидания был бы упорядочен
    по числу, которого не видно."""
    с = вкладка.открыть("pbx")
    с.locator("#pbx-top .kpi", has_text="Ожидание").locator("button.drill").first.click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    шапка = [я.upper() for я in с.locator("#pbx-top .drill-panel thead th").all_inner_texts()]
    assert "ОЖИДАНИЕ" in шапка and "РАЗГОВОР" in шапка
    столбец = шапка.index("ОЖИДАНИЕ") + 1
    значения = с.evaluate(f"""() => Array.from(document.querySelectorAll(
        '#pbx-top .drill-panel tbody tr td:nth-child({столбец})')).map((я) => я.innerText.trim())""")
    секунды = [int(з.split(":")[0]) * 60 + int(з.split(":")[1]) for з in значения]
    assert секунды == sorted(секунды, reverse=True) and секунды[0] > секунды[-1]
    assert с.locator("#pbx-top .drill-sort").input_value() == "wait:desc"
    вкладка.чисто()


def test_показатель_раскрывается_в_свой_числитель(вкладка):
    с = вкладка.открыть("pbx")
    k = с.evaluate(f"{ДАННЫЕ}.kpi")
    assert k["abandoned"] == 5 and k["repeated"] == 2
    карточка = с.locator("#pbx-top section.card").nth(1)
    карточка.locator(".kpi", has_text="Потерянные вызовы").locator("button.drill").click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    assert _строк(с, "#pbx-top") == k["abandoned"]
    итоги = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#pbx-top .drill-panel tbody tr')).map((р) => р.children[1].innerText.trim()
          + '|' + р.children[6].innerText.trim())"""))
    assert all(и.startswith("входящий|") and "отвечен" not in и for и in итоги), итоги
    # «С повторным — 2»: те обращения, после которых номер перезвонил.
    карточка.locator(".kpi", has_text="Решение с первого раза").locator(
        ".kpi-sub button.drill").click()
    с.wait_for_function("""() => document.querySelectorAll(
        '#pbx-top .drill-panel tbody tr').length === 2""")
    номера = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#pbx-top .drill-panel tbody tr')).map((р) => р.children[2].innerText.trim())"""))
    assert len(номера) == 1 and "+79161112233" in next(iter(номера))
    вкладка.чисто()


def test_карточка_и_строка_станции(вкладка):
    с = вкладка.открыть("pbx")
    карточка = с.locator('#pbx-cards .pbx-card[data-station="ats2"]')
    число = карточка.locator(".kpi-value button.drill").first
    всего = _число(число.inner_text())
    число.click()
    с.wait_for_selector("#pbx-body .drill-panel tbody tr")
    # Карточки стоят в ряд: список — под всем рядом, а не внутри карточки.
    assert с.evaluate("""() => { const п = document.querySelector('#pbx-body .drill-panel');
        return п.previousElementSibling.id === 'pbx-cards'; }""")
    assert _сколько_обещано(с) == всего
    assert с.locator(".drill-title").inner_text() == "Вторая: звонки за период"
    # Кнопки карточки не задеты: «Звонки» по-прежнему ведут в журнал.
    assert карточка.locator('button[data-act="calls"]').count() == 1
    число.click()
    assert с.locator(".drill-panel").count() == 0
    # Таблица «Станции рядом»: список встаёт строкой под своей.
    таблица = с.locator("#pbx-body table.table").last
    строка = таблица.locator("tbody tr", has_text="без станции")
    кнопка = строка.locator("button.drill").first
    сколько = _число(кнопка.inner_text())
    assert сколько == 3
    кнопка.click()
    с.wait_for_selector("#pbx-body tr.drill-row .drill-panel tbody tr")
    assert _строк(с, "#pbx-body tr.drill-row") == сколько
    # Архив станции — за всё время: у первой станции есть звонки старше
    # недели, и в архиве их больше, чем «за период» на той же карточке.
    первая = с.locator('#pbx-cards .pbx-card[data-station="ats1"]')
    за_неделю = _число(первая.locator(".kpi-value button.drill").first.inner_text())
    архив = первая.locator(".small button.drill").first
    за_всё_время = _число(архив.inner_text())
    архив.click()
    с.wait_for_function("""() => { const п = document.querySelector('#pbx-cards + .drill-panel');
        return п && п.querySelectorAll('tbody tr').length > 0; }""")
    assert с.locator("#pbx-cards + .drill-panel .drill-title").inner_text() == "Первая: весь архив"
    assert _сколько_обещано(с, "#pbx-cards + .drill-panel") == за_всё_время > за_неделю
    вкладка.чисто()


def test_столбцы_и_доли_обзора(вкладка):
    с = вкладка.открыть("pbx")
    по_станциям = с.evaluate(f"{ДАННЫЕ}.by_station")
    _раскрыть_метку(с, с.locator("#pbx-by-station rect.pick").first)
    первая = max(по_станциям, key=lambda строка: строка["total"])
    assert _сколько_обещано(с) == первая["total"]
    assert с.locator(".drill-title").inner_text().startswith("Звонки за период:")
    # Тот же столбец ещё раз — свернуть.
    с.locator("#pbx-by-station rect.pick").first.dispatch_event("click")
    assert с.locator(".drill-panel").count() == 0
    # Кольцо направлений: щёлкаем по строке легенды «без направления».
    за = с.evaluate(f"{ДАННЫЕ}.period_calls")
    прочие = за["total"] - за["inbound"] - за["outbound"] - за["internal"]
    assert прочие > 0
    с.locator("#pbx-directions .row.pick").last.click()
    с.wait_for_selector("#pbx-body .drill-panel tbody tr")
    assert _сколько_обещано(с) == прочие
    направления = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#pbx-body .drill-panel tbody tr td:nth-child(2)')).map((я) => я.innerText.trim())"""))
    assert направления == {"—"}, направления
    вкладка.чисто()


def test_нагрузка_точка_клетка_и_столбец(вкладка):
    с = вкладка.открыть("pbx")
    _на_вкладку(с, "load")
    # Точка линии: щелчок по правому краю — последняя корзина.
    лента = с.evaluate(f"{ДАННЫЕ}.timeline")
    слой = с.locator("#pbx-timeline rect.pick")
    рамка = слой.bounding_box()
    слой.click(position={"x": рамка["width"] - 2, "y": рамка["height"] / 2})
    с.wait_for_selector("#pbx-body .drill-panel tbody tr")
    assert _строк(с, "#pbx-body") == min(50, лента[-1]["total"])
    assert с.locator(".drill-title").inner_text().startswith("Звонки: ")
    # Переключатель линии перерисовывает только линию: список под ней цел.
    с.check("#pbx-load-talk")
    с.wait_for_timeout(300)
    assert _строк(с, "#pbx-body") == min(50, лента[-1]["total"])
    с.locator("#pbx-body .drill-close").click()
    # Клетка карты: самая нагруженная.
    карта = с.evaluate(f"{ДАННЫЕ}.heatmap.calls")
    сколько, день, час = max((v, д, ч) for д, ряд in enumerate(карта) for ч, v in enumerate(ряд))
    assert сколько >= 3
    _раскрыть_метку(с, с.locator("#pbx-heat svg rect").nth(день * 24 + час))
    assert _сколько_обещано(с) == сколько
    assert _строк(с, "#pbx-body") == min(50, сколько)
    # Панель встаёт под рядом из двух карточек, и линию она не трогает.
    assert с.evaluate("""() => document.querySelector('#pbx-body .drill-panel')
        .previousElementSibling.classList.contains('grid')""")
    # Пустая клетка не раскрывается: запроса нет, панель остаётся прежней.
    пустая = next((д * 24 + ч for д, ряд in enumerate(карта) for ч, v in enumerate(ряд) if not v))
    до = вкладка.раскрытий()
    клетка = с.locator("#pbx-heat svg rect").nth(пустая)
    # Она и не кнопка: в подсказке у неё нет строки «щелчок — список звонков».
    assert "pick" not in (клетка.get_attribute("class") or "")
    клетка.dispatch_event("click")
    с.wait_for_timeout(300)
    assert вкладка.раскрытий() == до
    # Столбец длительностей: первая непустая корзина.
    корзины = [к for к in с.evaluate(f"{ДАННЫЕ}.durations") if к["count"]]
    _раскрыть_метку(с, с.locator("#pbx-durations rect.pick").first)
    с.wait_for_function("""() => document.querySelector('#pbx-body .drill-title')
        .innerText.startsWith('Отвеченные звонки длительностью')""")
    с.wait_for_selector("#pbx-body .drill-panel tbody tr")
    assert _сколько_обещано(с) == корзины[0]["count"]
    # Часы суток: первый непустой столбец — сумма столбца карты.
    по_часам = [sum(ряд[ч] for ряд in карта) for ч in range(24)]
    первый = next(ч for ч, v in enumerate(по_часам) if v)
    _раскрыть_метку(с, с.locator("#pbx-hours rect.pick").first)
    с.wait_for_function("""() => Array.from(document.querySelectorAll('#pbx-body .drill-title'))
        .some((з) => з.innerText.includes('все дни периода'))""")
    панель = с.locator("#pbx-body .drill-panel", has_text="все дни периода")
    панель.locator("tbody tr").first.wait_for()
    подпись = панель.locator(".drill-count").inner_text()
    assert _число(re.findall(r"\d[\d\s  ]*", подпись)[-1]) == по_часам[первый]
    вкладка.чисто()


def test_очереди_номера_и_забор(вкладка):
    с = вкладка.открыть("pbx")
    топы = с.evaluate(f"{ДАННЫЕ}.tops")
    # Очереди и операторы: столбик, средний разговор и строка таблицы.
    _на_вкладку(с, "people")
    _раскрыть_метку(с, с.locator("#pbx-queues rect.pick").first)
    assert _сколько_обещано(с) == топы["queue"][0]["count"]
    по_среднему = max(топы["agent"], key=lambda строка: строка["avg_s"])
    _раскрыть_метку(с, с.locator("#pbx-agent-avg rect.pick").first)
    с.wait_for_function("""() => Array.from(document.querySelectorAll('#pbx-body .drill-title'))
        .some((з) => з.innerText.includes('отвеченные, сначала самые долгие'))""")
    панель = с.locator("#pbx-body .drill-panel", has_text="отвеченные, сначала самые долгие")
    панель.locator("tbody tr").first.wait_for()
    assert панель.locator(".drill-sort").input_value() == "billsec:desc"
    assert _число(re.findall(r"\d[\d\s  ]*",
                             панель.locator(".drill-count").inner_text())[-1]) \
        == по_среднему["answered"]
    таблица = с.locator("#pbx-body table.table").first
    строка = таблица.locator("tbody tr").first
    кнопка = строка.locator("button.drill").first
    сколько = _число(кнопка.inner_text())
    assert сколько == топы["agent"][0]["count"]
    кнопка.click()
    с.wait_for_selector("#pbx-body tr.drill-row .drill-panel tbody tr")
    assert _сколько_обещано(с, "#pbx-body tr.drill-row") == сколько
    операторы = set(с.evaluate("""() => Array.from(document.querySelectorAll(
        '#pbx-body tr.drill-row .drill-panel tbody tr td:nth-child(5)'))
        .map((я) => я.innerText.trim())"""))
    assert операторы == {топы["agent"][0]["value"]}
    # Номера и направления: итог звонка подписан по-русски, а отбор — как в базе.
    _на_вкладку(с, "numbers")
    _раскрыть_метку(с, с.locator("#pbx-disposition rect.pick").first)
    assert _сколько_обещано(с) == топы["disposition"][0]["count"]
    assert с.locator(".drill-title").inner_text() == "Звонки за период: ответили"
    # Забор записей: причина пропуска, доля полосы и счётчик станции.
    _на_вкладку(с, "intake")
    _раскрыть_метку(с, с.locator("#pbx-reasons rect.pick").first)
    assert _сколько_обещано(с) == топы["skipped"][0]["count"]
    с.locator("#pbx-body .drill-close").click()
    за = с.evaluate(f"{ДАННЫЕ}.period_calls")
    _раскрыть_метку(с, с.locator("#pbx-funnel .chart-legend .pick").last)
    assert с.locator(".drill-title").inner_text() == "Звонки за период: остальное"
    assert _сколько_обещано(с) == за["total"] - за["queued"] - за["skipped"]
    кнопка = с.locator("#pbx-body table.table tbody tr").first.locator("button.drill").first
    взято = _число(кнопка.inner_text())
    кнопка.click()
    с.wait_for_selector("#pbx-body tr.drill-row .drill-panel tbody tr")
    assert _сколько_обещано(с, "#pbx-body tr.drill-row") == взято > 0
    # Агенты на станциях: «прислал N звонков».
    _на_вкладку(с, "agents")
    с.wait_for_selector("#pbx-body table button.drill")
    кнопка = с.locator("#pbx-body table button.drill").first
    прислал = _число(кнопка.inner_text())
    кнопка.click()
    с.wait_for_selector("#pbx-body tr.drill-row .drill-panel tbody tr")
    assert _сколько_обещано(с, "#pbx-body tr.drill-row") == прислал > 0
    вкладка.чисто()


def test_раскрытый_список_сам_не_пропадает(вкладка):
    """Раздел «АТС» по таймеру и по событиям не обновляется: общий такт
    (4 с) перерисовывает только «Очередь» и «Транскрибацию». Проверяем это
    делом: два такта подряд список стоит, а свод заново не запрашивается."""
    с = вкладка.открыть("pbx")
    с.locator("#pbx-top .kpi button.drill").first.click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    _на_вкладку(с, "load")
    _раскрыть_метку(с, с.locator("#pbx-days rect.pick").last, "#pbx-body")
    до = sum(1 for адрес in вкладка.запросы if "/api/telephony/overview" in адрес)
    с.wait_for_timeout(9000)
    assert _строк(с, "#pbx-top") == 50
    assert _строк(с, "#pbx-body") > 0
    assert sum(1 for адрес in вкладка.запросы if "/api/telephony/overview" in адрес) == до
    вкладка.чисто()


def test_заход_за_звонками_не_сносит_раскрытый_список(вкладка):
    """«Забрать со всех» по окончании перерисовывает раздел целиком. Заход
    длится долго, и список, раскрытый за это время, пропал бы из-под рук.
    При раскрытом списке раздел остаётся как есть, без списка — обновляется."""
    с = вкладка.открыть("pbx")

    def сводов() -> int:
        return sum(1 for адрес in вкладка.запросы if "/api/telephony/overview" in адрес)

    def заходов() -> int:
        return sum(1 for адрес in вкладка.запросы if "/api/telephony/scan" in адрес)

    число = с.locator("#pbx-top .kpi button.drill").first
    число.click()
    с.wait_for_selector("#pbx-top .drill-panel tbody tr")
    до = сводов()
    с.click("#pbx-scan-all")
    с.wait_for_selector("#toasts .toast")
    с.wait_for_function("() => !document.querySelector('#pbx-scan-all').disabled")
    assert заходов() == 1 and сводов() == до, "раздел перечитан поверх раскрытого списка"
    assert _строк(с, "#pbx-top") == 50
    assert "нажмите «Обновить»" in с.locator("#toasts").inner_text()
    # Список свернули — следующий заход раздел обновляет.
    с.locator("#pbx-top .drill-close").click()
    с.click("#pbx-scan-all")
    с.wait_for_function("() => !document.querySelector('#pbx-scan-all').disabled")
    с.wait_for_timeout(500)
    assert заходов() == 2 and сводов() == до + 1
    вкладка.чисто()


def test_на_узком_экране_список_не_раздвигает_раздел(сервер):
    окно = Вкладка(сервер, ширина=390, высота=800)
    try:
        с = окно.открыть("pbx")
        до = с.evaluate("document.documentElement.scrollWidth")
        с.locator("#pbx-top .kpi", has_text="Ожидание").locator("button.drill").first.click()
        с.wait_for_selector("#pbx-top .drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= до <= 391
        assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
            return п.getBoundingClientRect().right <= window.innerWidth + 1; }""")
        окно.чисто()
    finally:
        окно.закрыть()


# ---------------------------------------------------------------------------
# «Телефония»
# ---------------------------------------------------------------------------


def test_плитки_над_журналом_раскрываются_а_журнал_цел(вкладка):
    с = вкладка.открыть("telephony")
    с.wait_for_selector("#tel-calls table")
    журнал = с.locator("#tel-calls tbody tr").count()
    assert журнал == 50
    число = с.locator("#tel-status .kpi button.drill").first
    всего = _число(число.inner_text())
    число.click()
    с.wait_for_selector("#tel-status .drill-panel tbody tr")
    assert _сколько_обещано(с) == всего > 90
    assert с.locator(".drill-title").inner_text() == "Все звонки архива"
    # Шапка — за всё время: в списке есть и звонок без времени начала.
    с.select_option("#tel-status .drill-sort", "time:asc")
    с.wait_for_function("""() => { const я = document.querySelector(
        '#tel-status .drill-panel tbody tr td'); return я && я.innerText.trim() === '—'; }""")
    # Смена периода перечитывает журнал, а шапку и список под ней не трогает.
    с.click('#tel-period button[data-period="day"]')
    с.wait_for_timeout(700)
    assert _строк(с, "#tel-status") == 50
    assert с.locator("#tel-calls tbody tr").count() <= журнал
    # Причина пропуска — чип с числом.
    чип = с.locator("#tel-status .chip", has_text="без ответа").locator("button.drill")
    сколько = _число(чип.inner_text())
    чип.click()
    с.wait_for_function("""() => document.querySelector('#tel-status .drill-title')
        .innerText === 'Пропущено: без ответа'""")
    с.wait_for_selector("#tel-status .drill-panel tbody tr")
    assert _строк(с, "#tel-status") == сколько > 0
    # Чип станции остался отбором журнала, а не раскрытием.
    с.locator("#tel-status [data-station-chip]").first.click()
    с.wait_for_timeout(700)
    assert с.evaluate("window.__asrhub.state.telStation") == "ats1"
    assert _строк(с, "#tel-status") == сколько
    # «Забрать сейчас»: шапка под раскрытым списком не перерисовывается,
    # журнал — перечитывается.
    журналов = sum(1 for адрес in вкладка.запросы if "/api/telephony/calls" in адрес)
    с.click("#tel-scan")
    с.wait_for_selector("#toasts .toast")
    с.wait_for_function("() => !document.querySelector('#tel-scan').disabled")
    с.wait_for_timeout(500)
    assert _строк(с, "#tel-status") == сколько
    assert sum(1 for адрес in вкладка.запросы if "/api/telephony/calls" in адрес) == журналов + 1
    вкладка.чисто()
