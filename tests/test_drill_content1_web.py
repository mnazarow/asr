"""Заход 49: раскрытие чисел «Аналитики записей» в браузере — вкладки «Свод»,
«Эмоции», «Понятность», «NPS» и «Категории».

Серверная сверка «число = список» — в `test_drill_content1.py`. Здесь то же
глазами человека: по одному блоку каждого вида — плитка, полоса «сколько из
скольких», строка таблицы, столбец, точка ряда, доля кольца и полосы — и на
каждом проверяется, что в панели столько строк, сколько обещает число, что
запрос ушёл с окном того ответа, из которого число взято, и что до щелчка
страница за списками не ходит вовсе.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_content1 import досеять

ВКЛАДКИ = {"summary": "#tone-bar svg", "emotion": "#emo-line svg",
           "clarity": "#clr-line svg", "nps": "#nps-bar svg", "categories": "#cat-table"}


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("drill-content1-ui") / "data", заданий=140)
    досеять(хранилище)
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


class _Окно(Вкладка):
    """Вкладка, которая помнит ответы сводов: по ним сверяются окна списков."""

    def __init__(self, адрес: str, **размер) -> None:
        super().__init__(адрес, **размер)
        self.своды: dict[str, object] = {}
        self.страница.on("response", lambda о: self.своды.__setitem__(
            urlparse(о.url).path, о) if "/api/content/" in о.url else None)

    def свод(self, путь: str) -> dict:
        return self.своды[путь].json()

    def список(self) -> dict[str, list[str]]:
        """Параметры последнего запроса за списком."""
        адрес = [а for а in self.запросы if "/api/drill?" in а][-1]
        return parse_qs(urlparse(адрес).query, keep_blank_values=True)

    def на(self, имя: str):
        с = self.открыть("content")
        if имя != "summary":
            с.click(f'#content-tabs button[data-tab="{имя}"]')
        с.wait_for_selector(ВКЛАДКИ[имя], timeout=15000)
        с.wait_for_timeout(900)
        return с


@pytest.fixture()
def вкладка(сервер):
    окно = _Окно(сервер)
    try:
        yield окно
    finally:
        окно.закрыть()


def _число(текст: str) -> int:
    """Целое из подписи кнопки: «1 284», «44»."""
    return int(re.sub(r"[^\d]", "", текст.split(",")[0]))


def _панель(с, сколько: int | None = None) -> str:
    """Ждёт список и сверяет его с обещанным числом; отдаёт заголовок."""
    с.wait_for_selector(".drill-panel tbody tr")
    if сколько is not None:
        assert с.locator(".drill-panel tbody tr").count() == min(50, сколько)
        счёт = с.locator(".drill-count").inner_text().replace(" ", " ")
        assert счёт.endswith(f"из {сколько}"), счёт
    return с.locator(".drill-title").inner_text()


def _окно_запроса(вкладка: _Окно, окно: dict) -> None:
    """Список ушёл ровно с теми границами, что прислал свод."""
    запрос = вкладка.список()
    assert float(запрос["since"][0]) == окно["since"]
    assert float(запрос["until"][0]) == окно["until"]


# ---------------------------------------------------------------------------


def test_пока_ничего_не_раскрыто_вкладки_за_списками_не_ходят(вкладка):
    """На пяти вкладках полторы сотни чисел-кнопок. Запрос на каждое при
    открытии — это и было бы «зависает на большом количестве звонков»."""
    кнопок = 0
    for имя in ВКЛАДКИ:
        с = вкладка.на(имя) if имя == "summary" else вкладка.страница
        if имя != "summary":
            с.click(f'#content-tabs button[data-tab="{имя}"]')
            с.wait_for_selector(ВКЛАДКИ[имя], timeout=15000)
            с.wait_for_timeout(900)
        на_вкладке = с.locator("#content-body button.drill").count()
        assert на_вкладке >= 5, (имя, на_вкладке)
        кнопок += на_вкладке
        if имя != "categories":
            assert с.locator("#content-body .pick").count() >= 3, имя
        assert с.locator(".drill-panel").count() == 0
    assert кнопок >= 100
    assert вкладка.раскрытий() == 0
    # Ноль кнопкой не становится: за ним нет ни одной записи.
    assert вкладка.страница.evaluate("""() => Array.from(document.querySelectorAll(
        '#cat-table tbody tr')).filter((с) => с.children[3].textContent.trim() === '0')
        .every((с) => !с.children[3].querySelector('button.drill'))""")
    вкладка.чисто()


def test_плитка_свода_раскрывается_под_рядом_и_ведёт_к_записи(вкладка):
    с = вкладка.на("summary")
    плитка = с.locator(".kpi button.drill").first
    сколько = _число(плитка.inner_text())
    свод = вкладка.свод("/api/content/summary")
    assert сколько == свод["current"]["records"]
    плитка.click()
    assert _панель(с, сколько) == "Разобранные записи за период"
    assert плитка.get_attribute("aria-expanded") == "true"
    _окно_запроса(вкладка, свод["window"])
    assert вкладка.список()["base"] == ["content"]
    # Панель — под всем рядом плиток, во всю ширину.
    assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
        return п.previousElementSibling.classList.contains('grid')
          && п.getBoundingClientRect().width > 900; }""")
    # Ссылка открывает карточку записи на вкладке разбора; список остаётся.
    ссылка = с.locator(".drill-panel a.drill-open").first
    номер = ссылка.get_attribute("data-open")
    assert ссылка.get_attribute("href") == f"#/jobs/{номер}"
    assert ссылка.get_attribute("data-tab") == "analysis"
    ссылка.click()
    с.wait_for_selector(".modal-backdrop")
    assert с.evaluate("document.querySelector('.modal-backdrop').dataset.jobId") == номер
    assert с.locator(".drill-panel tbody tr").count() == min(50, сколько)
    assert вкладка.раскрытий() == 1
    вкладка.чисто()


def test_доля_раскрывается_в_записи_числителя_по_величине(вкладка):
    """«Отрицательных 43 %» — это отрицательные записи, сверху самые тяжёлые."""
    с = вкладка.на("summary")
    сейчас = вкладка.свод("/api/content/summary")["current"]
    плитка = с.locator('.kpi:has(.kpi-label:text-is("Отрицательных")) .kpi-value button.drill')
    assert "%" in плитка.inner_text()
    плитка.click()
    assert _панель(с, сейчас["negative"]).startswith("Отрицательные разговоры")
    assert вкладка.список()["f"] == ["negative"]
    assert вкладка.список()["sort"] == ["sentiment:asc"]
    # Последний столбец — величина, по которой список упорядочен.
    assert с.locator(".drill-panel thead th").last.text_content() == "Тональность"
    значения = с.evaluate("""() => Array.from(document.querySelectorAll(
        '.drill-panel tbody tr td:last-child')).map((я) =>
          Number(я.textContent.replace(',', '.').replace('−', '-')))""")
    assert значения == sorted(значения) and значения[-1] < -0.15
    # Знаменатель той же плитки — оценённые записи: другой список, то же место.
    с.locator('.kpi:has(.kpi-label:text-is("Отрицательных")) .kpi-sub button.drill').click()
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.startsWith('Записи с оценкой')")
    _панель(с, сейчас["scored"])
    assert с.locator(".drill-panel").count() == 1
    вкладка.чисто()


def test_прошлый_период_раскрывается_своим_окном(вкладка):
    с = вкладка.на("summary")
    с.click('#content-period button[data-period="day"]')
    с.wait_for_selector("#tone-bar svg", timeout=15000)
    с.wait_for_timeout(900)
    свод = вкладка.свод("/api/content/summary")
    было = с.locator(".kpi .kpi-trend button.drill").first
    assert _число(было.inner_text()) == свод["previous"]["records"] > 0
    было.click()
    assert _панель(с, свод["previous"]["records"]) == "Разобранные записи за прошлый период"
    _окно_запроса(вкладка, свод["window_previous"])
    вкладка.чисто()


def test_строка_таблицы_речь_и_разговор_раскрывается_под_собой(вкладка):
    с = вкладка.на("summary")
    сейчас = вкладка.свод("/api/content/summary")["current"]
    строка = с.locator('section.card:has(h3:text-is("Речь и разговор")) tbody tr',
                       has_text="Напряжение").first
    строка.locator("td").nth(1).locator("button.drill").click()
    assert _панель(с, сейчас["stress_checked"]).startswith("Напряжение")
    assert вкладка.список()["f"] == ["has:stress"]
    assert вкладка.список()["sort"] == ["stress:desc"]
    # Список — строкой сразу под своей, и столбец величины назван показателем.
    assert с.evaluate("""() => { const р = document.querySelector('tr.drill-row');
        return р.previousElementSibling.contains(document.querySelector('button.drill-on')); }""")
    значения = с.evaluate("""() => Array.from(document.querySelectorAll(
        '.drill-panel tbody tr td:last-child')).map((я) => Number(я.textContent))""")
    assert значения == sorted(значения, reverse=True) and значения[0] >= 55
    # Соседние ячейки таблицы на месте: их читает проверка сравнения периодов.
    assert строка.locator("td").count() == 6
    вкладка.чисто()


def test_столбец_и_точка_ряда_раскрываются_границами_своей_корзины(вкладка):
    с = вкладка.на("summary")
    корзины = [т for т in вкладка.свод("/api/content/timeline")["buckets"] if т["records"]]
    assert len(корзины) >= 2
    столбцы = с.locator("#chart-vol-time .pick")
    assert столбцы.count() == len(корзины)
    столбцы.last.click()
    заголовок = _панель(с, корзины[-1]["records"])
    assert заголовок.startswith("Разобранные записи за ")
    _окно_запроса(вкладка, корзины[-1])
    # Панель — под рядом из трёх графиков; тот же столбец ещё раз — свернуть.
    assert с.evaluate("""() => document.querySelector('.drill-panel')
        .previousElementSibling.classList.contains('grid')""")
    столбцы.last.click()
    assert с.locator(".drill-panel").count() == 0
    # Точка линейного графика — та же корзина, но уже отрицательные записи.
    поле = с.locator("#chart-neg-time rect.pick")
    рамка = поле.bounding_box()
    поле.click(position={"x": рамка["width"] - 2, "y": рамка["height"] / 2})
    assert _панель(с).startswith("Отрицательные разговоры за ")
    _окно_запроса(вкладка, корзины[-1])
    assert вкладка.список()["f"] == ["negative"]
    вкладка.чисто()


def test_полоса_тональности_и_числа_под_ней_делят_одно_место(вкладка):
    с = вкладка.на("summary")
    сейчас = вкладка.свод("/api/content/summary")["current"]
    с.locator("#tone-bar .chart-legend .pick").nth(1).click()        # нейтральные
    assert _панель(с, сейчас["neutral"]) == "Нейтральные разговоры"
    карточка = с.locator('section.card:has(#tone-bar)')
    assert карточка.locator(".drill-panel").count() == 1
    карточка.locator("button.drill", has_text=str(сейчас["positive"])).first.click()
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.startsWith('Положительные')")
    _панель(с, сейчас["positive"])
    assert карточка.locator(".drill-panel").count() == 1
    вкладка.чисто()


def test_точка_контрольной_карты_раскрывается_в_записи_суток(вкладка):
    с = вкладка.на("summary")
    карты = {к["key"]: к for к in вкладка.свод("/api/content/control")["charts"]}
    точки = карты["agent_score"]["points"]
    assert точки[-1]["records"] > 0
    поле = с.locator("#spc-agent_score rect.pick")
    рамка = поле.bounding_box()
    поле.click(position={"x": рамка["width"] - 2, "y": рамка["height"] / 2})
    assert _панель(с).startswith("Балл оператора: записи за ")
    _окно_запроса(вкладка, точки[-1])
    assert вкладка.список()["f"] == ["has:agent_score"]
    вкладка.чисто()


def test_свод_модели_раскрывается_в_записи_с_ответом(вкладка):
    с = вкладка.на("summary")
    с.wait_for_selector("#llm-summary .card", timeout=20000)
    с.wait_for_timeout(500)
    д = вкладка.свод("/api/content/llm")
    с.locator('#llm-summary .kpi:has(.kpi-label:text-is("Покрытие")) button.drill').click()
    assert _панель(с, д["analyzed"]) == "Записи с ответом модели"
    запрос = вкладка.список()
    assert запрос["base"] == ["llm"]
    assert запрос["f"] == ["status:completed", "no_control", "llm_ok"]
    _окно_запроса(вкладка, д["window"])
    assert "Причина" in с.locator(".drill-panel thead").text_content()
    # Доля кольца «Исходы» — по строке легенды.
    с.locator("#llm-outcomes .row.pick").first.click()
    с.wait_for_function(
        "document.querySelectorAll('.drill-title')[1] && "
        "document.querySelectorAll('.drill-title')[1].textContent.startsWith('Исход: ')")
    assert вкладка.список()["f"][-1] == f"outcome:{д['outcomes'][0]['key']}"
    # Ячейка таблицы трекеров — строкой под своей.
    ячейка = с.locator('#llm-summary table:has(th:text-is("Умный трекер"))').locator(
        "tbody tr").first.locator("button.drill").nth(1)
    сработал = _число(ячейка.inner_text())
    ячейка.click()
    с.wait_for_selector("#llm-summary tr.drill-row .drill-panel tbody tr")
    assert с.locator("#llm-summary tr.drill-row .drill-panel tbody tr").count() == сработал
    assert вкладка.список()["f"][-1].startswith("tracker:")
    вкладка.чисто()


def test_эмоции_полоса_кольцо_столбец_и_точка(вкладка):
    с = вкладка.на("emotion")
    свод = вкладка.свод("/api/content/summary")
    сейчас = свод["current"]
    # Полоса «сколько из скольких»: числитель и знаменатель — разные списки.
    полоса = с.locator("#content-body div.row.small", has_text="С высоким напряжением")
    полоса.locator("button.drill").first.click()
    assert _панель(с, сейчас["stress_high"]) == "Разговоры с напряжением 55 и выше"
    _окно_запроса(вкладка, свод["window"])
    полоса.locator("button.drill").nth(1).click()
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.startsWith('Записи, где')")
    _панель(с, сейчас["stress_checked"])
    # Доля кольца: «ровно» — остаток, включая записи без измеренного сдвига.
    с.locator("#emo-shift .row.pick").nth(2).click()
    ровно = сейчас["records"] - сейчас["recovered"] - сейчас["worsened"]
    кольцо = с.locator(".drill-panel", has_text="Разговор закончился ровно")
    кольцо.locator("tbody tr").first.wait_for()
    assert кольцо.locator(".drill-count").inner_text().endswith(f"из {ровно}")
    assert вкладка.список()["f"] == ["mood_flat"]
    # Столбец разреза: окно — своего ответа, отбор — группа и «измерено».
    разрез = вкладка.свод("/api/content/breakdown/agent")
    с.locator("#emo-breakdown rect.pick").first.click()
    с.wait_for_function(
        "Array.from(document.querySelectorAll('.drill-title')).some("
        "(з) => з.textContent.startsWith('Напряжение: '))")
    первый = next(г for г in разрез["items"] if г["stress"] is not None)
    запрос = вкладка.список()
    assert запрос["f"] == [f"agent:{'' if первый['key'] == '—' else первый['key']}",
                           "has:stress"]
    _окно_запроса(вкладка, разрез["window"])
    assert разрез["window"]["since"] != свод["window"]["since"]
    панель = с.locator(".drill-panel", has_text="Напряжение: ")
    панель.locator("tbody tr").first.wait_for()
    assert панель.locator(".drill-count").inner_text().endswith(f"из {первый['stress_checked']}")
    # Сменили разрез — список под прежним столбцом свёрнут, а не висит.
    с.select_option("#emo-dim", "owner")
    с.wait_for_function(
        "!Array.from(document.querySelectorAll('.drill-title')).some("
        "(з) => з.textContent.startsWith('Напряжение: '))")
    assert с.locator("#emo-breakdown rect.pick").count() >= 2
    # Точка ряда — корзина со своими границами.
    корзины = вкладка.свод("/api/content/timeline")["buckets"]
    поле = с.locator("#emo-line rect.pick")
    рамка = поле.bounding_box()
    поле.click(position={"x": рамка["width"] - 2, "y": рамка["height"] / 2})
    с.wait_for_function(
        "Array.from(document.querySelectorAll('.drill-title')).some("
        "(з) => з.textContent.startsWith('Разговоры за '))")
    _окно_запроса(вкладка, корзины[-1])
    вкладка.чисто()


def test_понятность_плитка_среднего_и_столбик_по_человеку(вкладка):
    с = вкладка.на("clarity")
    сейчас = вкладка.свод("/api/content/summary")["current"]
    с.locator('.kpi:has(.kpi-label:text-is("Понятность речи")) button.drill').click()
    assert _панель(с, сейчас["clarity_checked"]).startswith("Понятность речи")
    assert вкладка.список()["sort"] == ["clarity:asc"]         # сверху самые тяжёлые
    люди = вкладка.свод("/api/content/employees")
    с.locator("#clr-fillers rect.pick").first.click()
    с.wait_for_function(
        "Array.from(document.querySelectorAll('.drill-title')).some("
        "(з) => з.textContent.startsWith('Слова-паразиты: '))")
    запрос = вкладка.список()
    assert запрос["f"][0].startswith(f"{люди['by']}:") and запрос["f"][1] == "has:filler_rate"
    _окно_запроса(вкладка, люди["window"])
    вкладка.чисто()


def test_nps_плитки_полоса_и_столбец(вкладка):
    с = вкладка.на("nps")
    сейчас = вкладка.свод("/api/content/summary")["current"]
    плитка = с.locator('.kpi:has(.kpi-label:text-is("Промоутеры"))')
    плитка.locator(".kpi-value button.drill").click()
    assert _панель(с, сейчас["promoters"]).startswith("Промоутеры")
    assert вкладка.список()["f"] == ["nps_group:промоутер"]
    # Панель — внутри карточки, между полосой и пояснением про опрос.
    assert с.evaluate("""() => { const п = document.querySelector('.drill-panel');
        const пояснение = document.querySelector('#content-body .banner');
        return п.parentElement.hasAttribute('data-drill-host')
          && Boolean(п.compareDocumentPosition(пояснение) & Node.DOCUMENT_POSITION_FOLLOWING); }""")
    с.locator("#nps-bar .chart-legend .pick").nth(2).click()         # критики
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.startsWith('Критики')")
    _панель(с, сейчас["detractors"])
    assert с.locator(".drill-panel").count() == 1
    # Названный балл — свой список, не предсказанный.
    с.locator('.kpi:has(.kpi-label:text-is("Названный балл")) button.drill').click()
    с.wait_for_function(
        "document.querySelector('.drill-title').textContent.includes('назвал')")
    _панель(с, сейчас["nps_stated_count"])
    assert вкладка.список()["f"] == ["has:nps_said"]
    разрез = вкладка.свод("/api/content/breakdown/agent")
    с.locator("#nps-breakdown rect.pick").first.click()
    с.wait_for_function(
        "Array.from(document.querySelectorAll('.drill-title')).some("
        "(з) => з.textContent.startsWith('NPS: '))")
    assert вкладка.список()["f"][1] == "has:nps"
    _окно_запроса(вкладка, разрез["window"])
    вкладка.чисто()


def test_категории_плитка_строка_таблицы_и_драйвер(вкладка):
    с = вкладка.на("categories")
    свод = вкладка.свод("/api/content/categories")
    без = свод["uncategorized"]["records"]
    с.locator('.kpi:has(.kpi-label:text-is("Без категории")) button.drill').click()
    assert _панель(с, без) == "Записи без категории обращения"
    _окно_запроса(вкладка, свод["window"])
    # Строка таблицы: список — под строкой.
    сроки = next(к for к in свод["items"] if к["id"] == "deadline")
    строка = с.locator("#cat-table tbody tr", has=с.locator('button[data-category="deadline"]'))
    строка.locator("td").nth(3).locator("button.drill").click()
    с.wait_for_selector("#cat-table tr.drill-row .drill-panel tbody tr")
    assert с.locator("#cat-table tr.drill-row .drill-panel tbody tr").count() == min(
        50, сроки["records"])
    assert вкладка.список()["f"] == ["category:deadline"]
    assert с.locator("#cat-table tr.drill-row .drill-count").inner_text().endswith(
        f"из {сроки['records']}")
    # Кнопка «Записи» той же строки осталась и по-прежнему ведёт в «Результаты».
    assert строка.locator('button[data-category="deadline"]').inner_text() == "Записи"
    # Драйвер негатива: «записей» — оценённые записи категории.
    драйверы = вкладка.свод("/api/content/drivers")
    assert драйверы["down"], "в архиве нет драйверов негатива"
    первый = драйверы["down"][0]
    карточка = с.locator('section.card:has(h3:text-is("Что тянет вниз"))')
    карточка.locator("tbody tr").first.locator("button.drill").first.click()
    карточка.locator("tr.drill-row .drill-panel tbody tr").first.wait_for()
    assert вкладка.список()["f"] == [f"category:{первый['id']}", "scored"]
    _окно_запроса(вкладка, драйверы["window"])
    assert карточка.locator("tr.drill-row .drill-count").inner_text().endswith(
        f"из {первый['records']}")
    вкладка.чисто()


@pytest.mark.parametrize("имя,кнопка", [
    ("summary", ".kpi button.drill"),
    ("emotion", "#content-body div.row.small button.drill"),
    ("nps", ".kpi button.drill"),
    ("categories", "#cat-table td button.drill"),
])
def test_на_узком_экране_список_не_раздвигает_страницу(сервер, имя, кнопка):
    окно = _Окно(сервер, ширина=390, высота=800)
    try:
        с = окно.на(имя)
        до = с.evaluate("document.documentElement.scrollWidth")
        assert до <= 391
        с.locator(кнопка).first.click()
        с.wait_for_selector(".drill-panel tbody tr")
        assert с.evaluate("document.documentElement.scrollWidth") <= до
        окно.чисто()
    finally:
        окно.закрыть()
