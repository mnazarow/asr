"""Заход 49: раскрытие в «Трендах» — как это видит человек в браузере.

Точка графика, число в своде, плитка показателя и клетка карты часов
недели раскрываются списком записей. Здесь проверяется то, чего не видно
из ответа сервера: что список встаёт под своим блоком и в нём столько
строк, сколько обещает число; что интерфейс спрашивает ровно ту корзину,
которую отдал свод; что список переживает галочку в соседней строке и не
переживает смену периода; что до щелчка страница за списками не ходит.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-trends-ui") / "data", заданий=140)
    # Эталоны — только у записей двух-трёхдневной давности: у «WER по
    # эталонам» последняя непустая корзина тогда не последняя по времени.
    сейчас = хранилище.данные["now"]
    хранилище.db.execute(
        "UPDATE jobs SET ref_words=40, sub_words=4, del_words=1, ins_words=1, wer=0.15, "
        "mer=0.14 WHERE status='completed' AND created_at >= ? AND created_at < ?",
        (сейчас - 3 * 86400, сейчас - 2.2 * 86400))
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


class _Тренды(Вкладка):
    """Вкладка, которая запоминает ответы сводов раздела — чтобы сверять
    список с тем самым ответом, по которому нарисована страница."""

    def __init__(self, адрес: str, **прочее: Any) -> None:
        super().__init__(адрес, **прочее)
        self.своды: dict[str, dict[str, Any]] = {}
        self.страница.on("response", self._запомнить)

    def _запомнить(self, ответ: Any) -> None:
        путь = urlsplit(ответ.url).path
        if путь in ("/api/trends", "/api/trends/heatmap") and ответ.status == 200:
            self.своды[путь] = ответ.json()

    def тренды(self) -> Any:
        с = self.открыть("trends")
        с.wait_for_selector("#tr-heat svg rect")
        с.wait_for_selector("#tr-table tr")
        return с

    def за_списком(self) -> list[dict[str, list[str]]]:
        """Запросы списков: с какими границами и отборами ходила страница."""
        return [parse_qs(urlsplit(адрес).query) for адрес in self.запросы
                if "/api/drill?" in адрес]


@pytest.fixture()
def вкладка(сервер):
    окно = _Тренды(сервер)
    try:
        yield окно
    finally:
        окно.закрыть()


def _число(текст: str) -> int:
    return int("".join(знак for знак in текст if знак.isdigit()))


def _строк(с: Any, где: str = "") -> int:
    return с.locator(f"{где} .drill-panel tbody tr".strip()).count()


def _точка(с: Any, доля: float) -> None:
    """Щелчок по главному графику: `доля` — место по ширине, 1 — правый край."""
    слой = с.locator("#tr-main svg rect.pick").bounding_box()
    с.mouse.click(слой["x"] + 2 + (слой["width"] - 4) * доля, слой["y"] + слой["height"] / 2)


def test_до_щелчка_раздел_за_списками_не_ходит(вкладка):
    """На странице полсотни показателей, у каждого плитка и строка свода."""
    с = вкладка.тренды()
    assert с.locator("#tr-table button.drill").count() >= 30
    assert с.locator("#tr-groups .kpi button.drill").count() >= 20
    assert с.locator("#tr-main svg rect.pick").count() == 1
    assert с.locator("#tr-heat svg rect.pick").count() >= 24
    assert вкладка.раскрытий() == 0
    assert с.locator(".drill-panel").count() == 0
    вкладка.чисто()


def test_число_свода_раскрывается_в_записи_всего_окна(вкладка):
    с = вкладка.тренды()
    свод = вкладка.своды["/api/trends"]
    итог = next(строка for строка in свод["summary"] if строка["id"] == "jobs")
    кнопка = с.locator("#tr-table tr:has(input[data-metric='jobs']) button.drill")
    assert _число(кнопка.inner_text()) == итог["n"]
    кнопка.click()
    с.wait_for_selector("#tr-table tr.drill-row .drill-panel tbody tr")
    assert _строк(с, "#tr-table tr.drill-row") == min(50, итог["n"])
    assert f"из {итог['n']}" in с.locator(".drill-count").inner_text().replace(" ", " ")
    assert с.locator(".drill-title").inner_text() == "Задания · за период"
    # Панель — строкой под своей строкой, окно — то, что отдал свод.
    assert с.evaluate("""() => document.querySelector('#tr-table tr.drill-row')
        .previousElementSibling.contains(document.querySelector('#tr-table button.drill-on'))""")
    запрос = вкладка.за_списком()[-1]
    assert float(запрос["since"][0]) == свод["window"]["since"]
    assert float(запрос["until"][0]) == свод["window"]["until"]
    assert "f" not in запрос and запрос["base"] == ["jobs"]
    # Ссылка открывает карточку записи, список остаётся под ней.
    ссылка = с.locator("#tr-table .drill-panel a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert с.evaluate("location.hash") == "#trends"
    вкладка.чисто()


def test_среднее_раскрывается_списком_по_самой_величине(вкладка):
    """За «RTF 0,17» — записи, у которых он измерен, худшие сверху, RTF столбцом."""
    с = вкладка.тренды()
    свод = вкладка.своды["/api/trends"]
    итог = next(строка for строка in свод["summary"] if строка["id"] == "rtf")
    assert 0 < итог["n"] < next(х["n"] for х in свод["summary"] if х["id"] == "jobs")
    кнопка = с.locator("#tr-table tr:has(input[data-metric='rtf']) button.drill")
    assert _число(кнопка.inner_text()) == итог["n"]
    кнопка.click()
    с.wait_for_selector("#tr-table tr.drill-row .drill-panel tbody tr")
    assert с.locator("#tr-table .drill-panel thead th").last.inner_text().upper() == "RTF"
    assert с.locator("#tr-table .drill-sort").input_value() == "rtf:desc"
    величины = [float(т.replace(",", ".")) for т in с.locator(
        "#tr-table .drill-panel tbody tr td:last-child").all_inner_texts()]
    assert len(величины) == min(50, итог["n"]) and величины == sorted(величины, reverse=True)
    запрос = вкладка.за_списком()[-1]
    assert запрос["f"] == ["has:rtf"] and запрос["sort"] == ["rtf:desc"]
    # По времени — те же записи: порядок не меняет состав списка.
    с.select_option("#tr-table .drill-sort", "time:desc")
    с.wait_for_function("""(n) => document.querySelector('#tr-table .drill-count')
        .textContent.replace(/\\s/g, '').endsWith('из' + n)""", arg=итог["n"])
    вкладка.чисто()


def test_точка_графика_раскрывается_в_записи_своей_корзины(вкладка):
    с = вкладка.тренды()
    свод = вкладка.своды["/api/trends"]
    последняя = len(свод["buckets"]) - 1
    _точка(с, 1.0)
    с.wait_for_selector("#tr-pick button.drill")
    с.wait_for_selector("#trends-body > .card .drill-panel tbody tr")
    # Под графиком — по числу на каждый выбранный показатель, и каждое равно
    # тому, что свод насчитал в этой корзине.
    ряды = {ряд["id"]: ряд for ряд in свод["series"]}
    числа = [_число(т) for т in с.locator("#tr-pick button.drill").all_inner_texts()]
    assert числа == [ряды[и]["n"][последняя] for и in ("jobs", "rtf", "confidence")]
    assert числа[0] > числа[1] > 0, "за «Заданий» и за «RTF» стоят разные записи"
    # Первый список открыт сразу и стоит под графиком, а не под картой.
    открытая = с.locator("#tr-pick button.drill-on")
    assert открытая.count() == 1 and _число(открытая.inner_text()) == числа[0]
    assert _строк(с, "#trends-body > .card") == min(50, числа[0])
    assert с.evaluate("""() => document.querySelector('.drill-panel').parentElement
        .contains(document.querySelector('#tr-main'))""")
    # Границы — корзины из ответа свода; последняя обрезана концом окна.
    запрос = вкладка.за_списком()[-1]
    assert float(запрос["since"][0]) == свод["buckets"][последняя]
    assert float(запрос["until"][0]) == свод["window"]["until"]
    assert вкладка.раскрытий() == 1
    # Соседний показатель той же точки — свой список вместо первого.
    с.locator("#tr-pick button.drill").nth(1).click()
    с.wait_for_function("""() => { const т = document.querySelector('.drill-title');
        return т && т.textContent.startsWith('Записи с измеренным RTF'); }""")
    с.wait_for_selector(".drill-panel tbody tr")
    assert с.locator(".drill-panel").count() == 1 and _строк(с) == min(50, числа[1])
    assert вкладка.за_списком()[-1]["f"] == ["has:rtf"]
    # Та же точка ещё раз — строка и список убираются.
    _точка(с, 1.0)
    assert с.locator(".drill-panel").count() == 0
    assert с.locator("#tr-pick").inner_text().strip() == ""
    # Корзина в середине окна — полная: конец ровно через шаг.
    _точка(с, (последняя - 2) / последняя)
    с.wait_for_selector(".drill-panel tbody tr")
    запрос = вкладка.за_списком()[-1]
    assert float(запрос["since"][0]) == свод["buckets"][последняя - 2]
    assert float(запрос["until"][0]) == свод["buckets"][последняя - 1]
    assert _строк(с) == min(50, ряды["jobs"]["n"][последняя - 2])
    вкладка.чисто()


def test_плитка_раскрывается_в_записи_последней_корзины(вкладка):
    с = вкладка.тренды()
    свод = вкладка.своды["/api/trends"]
    итог = next(строка for строка in свод["summary"] if строка["id"] == "failed")
    ряд = next(ряд for ряд in свод["series"] if ряд["id"] == "failed")
    в_корзине = ряд["n"][итог["last_at"]]
    плитка = с.locator("#tr-groups .kpi", has_text="С ошибкой").first
    кнопка = плитка.locator("button.drill")
    assert _число(кнопка.inner_text()) == в_корзине == итог["last"] > 0
    кнопка.click()
    с.wait_for_selector("#tr-groups .drill-panel tbody tr")
    assert _строк(с, "#tr-groups") == в_корзине
    assert с.locator("#tr-groups .drill-title").inner_text().startswith("Задания с ошибкой · ")
    assert set(с.locator("#tr-groups .drill-panel tbody tr td:nth-child(6)")
               .all_inner_texts()) == {"ошибка"}
    запрос = вкладка.за_списком()[-1]
    assert float(запрос["since"][0]) == свод["buckets"][итог["last_at"]]
    assert запрос["f"] == ["status:failed"]
    # «Последнее» — последняя НЕПУСТАЯ корзина. У WER она позавчерашняя:
    # под числом плитки — записи именно её, а не самой свежей корзины.
    итог = next(строка for строка in свод["summary"] if строка["id"] == "wer")
    ряд = next(ряд for ряд in свод["series"] if ряд["id"] == "wer")
    assert итог["last_at"] < len(свод["buckets"]) - 1
    assert ряд["n"][итог["last_at"]] > 0 and ряд["n"][-1] == 0
    с.locator("#tr-groups .kpi", has_text="WER по эталонам").first.locator("button.drill").click()
    с.wait_for_function("""() => Array.from(document.querySelectorAll('#tr-groups .drill-title'))
        .some((т) => т.textContent.startsWith('Записи с эталоном'))""")
    запрос = вкладка.за_списком()[-1]
    assert float(запрос["since"][0]) == свод["buckets"][итог["last_at"]]
    assert float(запрос["until"][0]) == свод["buckets"][итог["last_at"] + 1]
    assert запрос["f"] == ["has_reference"] and запрос["sort"] == ["wer:desc"]
    качество = с.locator("#tr-groups .card", has_text="WER по эталонам")
    качество.locator(".drill-panel tbody tr").first.wait_for()
    assert качество.locator(".drill-panel tbody tr").count() == ряд["n"][итог["last_at"]]
    # Щелчок по числу ряд на график не вынес: это действие плитки, а не числа.
    assert "на графике" not in плитка.inner_text()
    плитка.locator(".kpi-label").click()
    с.wait_for_function("""() => Array.from(document.querySelectorAll('#tr-groups .kpi'))
        .some((п) => п.textContent.includes('С ошибкой') && п.textContent.includes('на графике'))""")
    вкладка.чисто()


def test_клетка_карты_раскрывается_в_записи_своего_часа_недели(вкладка):
    с = вкладка.тренды()
    карта = вкладка.своды["/api/trends/heatmap"]
    assert карта["metric"] == "jobs" and карта["weeks"] > 1
    # Час — не из первых семи: перепутай интерфейс день с часом, отбор
    # `weekday:9` сервер отвергнет, а не молча покажет другую клетку.
    день, час = next((д, ч) for д in range(1, 7) for ч in range(8, 24)
                     if карта["n"][д][ч] > 0)
    # Клетки идут по строкам: семь дней по двадцать четыре часа.
    клетка = с.locator("#tr-heat svg rect").nth(день * 24 + час)
    клетка.click()
    с.wait_for_selector(".drill-panel tbody tr")
    assert _строк(с) == карта["n"][день][час]
    assert с.locator(".drill-title").inner_text() == \
        f"Задания · {карта['days'][день]}, {час:02d}:00–{час:02d}:59, за период"
    # Панель — под рядом из двух карточек, во всю ширину.
    assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    запрос = вкладка.за_списком()[-1]
    assert запрос["f"] == [f"weekday:{день}", f"hour:{час}"]
    assert float(запрос["since"][0]) == карта["window"]["since"]
    assert float(запрос["until"][0]) == карта["window"]["until"]
    # Та же клетка ещё раз — свернуть.
    клетка.click()
    assert с.locator(".drill-panel").count() == 0
    # Показатель без записей за часом: клетки не раскрываются вовсе.
    с.select_option("#tr-heat-metric", "cpu")
    с.wait_for_function("() => !document.querySelector('#tr-heat svg rect.pick')")
    вкладка.чисто()


def test_список_переживает_перерисовку_а_смену_периода_нет(вкладка):
    """Галочка показателя пересобирает раздел целиком — список остаётся."""
    с = вкладка.тренды()
    _точка(с, 1.0)
    с.wait_for_selector("#trends-body > .card .drill-panel tbody tr")
    с.locator("#tr-table tr:has(input[data-metric='completed']) button.drill").click()
    с.wait_for_selector("#tr-table tr.drill-row .drill-panel tbody tr")
    с.locator("#tr-heat svg rect.pick").first.click()
    с.wait_for_function("document.querySelectorAll('.drill-panel').length === 3")
    с.wait_for_function("""() => Array.from(document.querySelectorAll('.drill-panel'))
        .every((п) => п.querySelector('tbody tr'))""")
    # Долистали список свода и прокрутили его — это тоже должно остаться.
    с.locator("#tr-table .drill-more").click()
    с.wait_for_function(
        "document.querySelectorAll('#tr-table .drill-panel tbody tr').length > 50")
    с.evaluate("document.querySelector('#tr-table .drill-body').scrollTop = 300")
    до = {"запросов": вкладка.раскрытий(), "строк": _строк(с, "#tr-table"),
          "заголовки": с.locator(".drill-title").all_inner_texts()}
    assert до["запросов"] == 4
    метка = с.evaluate("""() => { const т = document.querySelector('#tr-table');
        т.dataset.prev = '1'; return true; }""")
    assert метка
    с.locator("#tr-table input[data-metric='failed']").check()
    с.wait_for_function("() => !document.querySelector('#tr-table').dataset.prev")
    с.wait_for_selector("#tr-heat svg rect")
    assert с.locator(".drill-title").all_inner_texts() == до["заголовки"]
    assert _строк(с, "#tr-table") == до["строк"]
    assert с.evaluate("document.querySelector('#tr-table .drill-body').scrollTop") == 300
    assert вкладка.раскрытий() == до["запросов"], "перерисовка не должна ходить за списками"
    # Числа снова знают свои панели: щелчок сворачивает, а не открывает вторую.
    assert с.locator("button.drill-on").count() == 2
    assert "С ошибкой" in с.locator("#tr-pick").inner_text()
    с.locator("#tr-table button.drill-on").click()
    assert с.locator("#tr-table tr.drill-row").count() == 0
    # Показатель убрали с графика — его числа под графиком больше нет, и списка тоже.
    с.locator("#tr-table input[data-metric='jobs']").uncheck()
    с.wait_for_function("""() => !document.querySelector('#tr-pick').textContent
        .includes('Заданий')""")
    assert с.locator("#trends-body > .card .drill-panel").count() == 0
    assert с.locator(".drill-panel").count() == 1, "список под картой на месте"
    # Новый период — новое окно: прежние списки были про другое.
    с.select_option("#tr-period", "week")
    с.wait_for_function("""() => document.querySelector('#trends-body .hint')
        .textContent.includes('по часам')""")
    assert с.locator(".drill-panel").count() == 0
    assert с.locator("#tr-pick").inner_text().strip() == ""
    assert вкладка.раскрытий() == до["запросов"]
    вкладка.чисто()


def test_на_узком_экране_раздел_со_списком_не_уезжает_вбок(сервер):
    окно = _Тренды(сервер, ширина=390, высота=800)
    try:
        с = окно.тренды()
        до = с.evaluate("document.documentElement.scrollWidth")
        assert до <= 391
        кнопка = с.locator("#tr-groups .kpi button.drill").first
        кнопка.scroll_into_view_if_needed()
        кнопка.click()
        с.wait_for_selector("#tr-groups .drill-panel tbody tr")
        с.locator("#tr-table button.drill").first.click()
        с.wait_for_selector("#tr-table tr.drill-row .drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= до
        окно.чисто()
    finally:
        окно.закрыть()
