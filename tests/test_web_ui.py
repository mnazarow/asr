"""Проверки интерфейса настоящим браузером.

Зачем это заведено. До сих пор интерфейс проверялся только глазами, и класс
ошибок, который так ловится, трижды прошёл бы в поставку в одном заходе:
легенда графика показывала «undefined undefined», столбчатый график падал
на записи без подписей и уносил за собой весь раздел, а медленная вкладка
рисовала таблицу поверх той, что человек уже выбрал. Ни одна проверка
текста файла этого не видит: код синтаксически верен, ruff молчит, тесты
сервера зелены — а раздел не работает.

Проверяется не вёрстка и не оформление: их сравнение со снимком ломается от
любой правки отступа и через месяц отключается целиком. Проверяется то, что
ломается по-настоящему: чиста ли консоль, нарисовалось ли то, за чем
пришли, и не остались ли на экране следы предыдущего раздела.

Браузер берётся из окружения: `PLAYWRIGHT_CHROMIUM` или предустановленный
Chromium сборки Playwright. Если его нет — проверки пропускаются, а не
падают: набор тестов должен проходить и там, где браузера не бывает.
"""
from __future__ import annotations

import os
import socket
import threading
import time
from pathlib import Path

import pytest

#: Где искать браузер. Первый существующий и берётся.
БРАУЗЕРЫ = (
    os.environ.get("PLAYWRIGHT_CHROMIUM", ""),
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def _браузер() -> str | None:
    for путь in БРАУЗЕРЫ:
        if путь and Path(путь).exists():
            return путь
    return None


def _свободный_порт() -> int:
    with socket.socket() as гнездо:
        гнездо.bind(("127.0.0.1", 0))
        return int(гнездо.getsockname()[1])


@pytest.fixture(scope="module")
def сервер(tmp_path_factory, request):
    """Настоящий сервер с наполненной базой на свободном порту.

    Именно сервер, а не заглушка: половина проверяемого — это разговор
    интерфейса с ручками, и подменять их значило бы проверять сам себя.
    """
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if _браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")

    import uvicorn

    каталог = tmp_path_factory.mktemp("ui") / "data"
    каталог.mkdir(parents=True)
    прежние = {к: os.environ.get(к) for к in
               ("ASRHUB_DATA_DIR", "ASRHUB_AUTH_ENABLED", "ASRHUB_MODEL",
                "ASRHUB_ENGINE")}
    os.environ.update(ASRHUB_DATA_DIR=str(каталог), ASRHUB_AUTH_ENABLED="false",
                      ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo")

    from asrhub.api.app import create_app

    приложение = create_app(start_queue=False)
    _наполнить(приложение)

    порт = _свободный_порт()
    настройка = uvicorn.Config(приложение, host="127.0.0.1", port=порт,
                               log_level="warning")
    служба = uvicorn.Server(настройка)
    поток = threading.Thread(target=служба.run, daemon=True)
    поток.start()
    for _ in range(200):
        if служба.started:
            break
        time.sleep(0.05)
    assert служба.started, "сервер не поднялся"

    yield f"http://127.0.0.1:{порт}"

    служба.should_exit = True
    поток.join(timeout=10)
    for к, v in прежние.items():
        if v is None:
            os.environ.pop(к, None)
        else:
            os.environ[к] = v


def _наполнить(приложение) -> None:
    """Записи с разбором: на пустом разделе проверять нечего."""
    from asrhub.content_index import ContentIndex

    состояние = приложение.state.hub
    db = состояние.db
    тексты = [
        ("Здравствуйте, компания Ромашка, меня зовут Анна. Договор готов, "
         "отправлю сегодня до восемнадцати. Спасибо, всего доброго!", "хорошо"),
        ("Срок поставки сорван второй раз, я крайне недоволен. Отвратительное "
         "качество. Буду жаловаться в суд.", "плохо"),
        ("Добрый день, подскажите статус заявки на двести тысяч рублей. "
         "Ну, э-э, короче, надо посмотреть. Перезвоню завтра.", "средне"),
    ]
    сейчас = time.time()
    for n in range(30):
        текст, метка = тексты[n % 3]
        когда = сейчас - n * 3600
        job_id = db.create_job({
            "id": f"ui{n:03d}", "filename": f"разговор-{n}.wav",
            "owner": "анна" if n % 2 else "борис", "engine": "demo",
            "model": "demo-simulator", "language": "ru", "source": "web",
            "tags": метка, "media_duration_s": 45.0})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, job_id))
        db.update_job(job_id, status="completed", text=текст,
                      finished_at=когда + 30, words_count=len(текст.split()),
                      segments_count=2, rtf=0.12, processing_time_s=5.0,
                      avg_confidence=0.93, speakers_count=2)
        куски = текст.split(". ")
        db.save_segments(job_id, [
            {"start": i * 12.0, "end": i * 12.0 + 10.0, "text": к,
             "speaker": f"SPEAKER_{i % 2:02d}"}
            for i, к in enumerate(куски)])
    ContentIndex(db, состояние.settings).backfill_once(limit=100)


@pytest.fixture()
def страница(сервер):
    """Вкладка браузера, которая копит ошибки консоли и отказы запросов."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        браузер = playwright.chromium.launch(executable_path=_браузер())
        вкладка = браузер.new_page(viewport={"width": 1600, "height": 1000})
        ошибки: list[str] = []
        вкладка.on("console", lambda m: ошибки.append(f"консоль: {m.text}")
                   if m.type == "error" else None)
        вкладка.on("pageerror", lambda e: ошибки.append(f"исключение: {e}"))
        вкладка.on("response", lambda r: ошибки.append(
            f"ответ {r.status}: {r.url}") if r.status >= 500 else None)
        вкладка.ошибки = ошибки
        вкладка.сервер = сервер
        try:
            yield вкладка
        finally:
            браузер.close()


def _открыть(страница, раздел: str) -> None:
    страница.goto(f"{страница.сервер}/#{раздел}", wait_until="networkidle")
    страница.wait_for_timeout(1500)


def _чисто(страница) -> None:
    """Ошибок в консоли быть не должно ни одной.

    Именно ни одной, а не «немного»: список исключений начинается с одной
    записи, а через полгода в нём десяток, и проверка перестаёт что-либо
    значить. Запись без пути к файлу (её даёт проигрыватель на записях без
    исходника) — единственное послабление, и оно названо явно.
    """
    настоящие = [о for о in страница.ошибки
                 if "audio" not in о and "400" not in о]
    assert not настоящие, "\n".join(настоящие)


# ---------------------------------------------------------------------------
# Аналитика записей
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("вкладка,что_ждём", [
    ("summary", "#tone-bar svg"),
    ("categories", "#cat-table"),
    ("agents", "#agents-table"),
    ("groups", "#chart-by-owner svg"),
    ("topics", "#chart-topics svg"),
    ("links", ".card"),
    ("records", "#content-records table"),
    ("script", "#script-list .script-item"),
])
def test_every_tab_of_the_content_section_draws_itself(страница, вкладка, что_ждём):
    """Каждая вкладка рисует то, за чем на неё заходят, и молчит в консоли."""
    _открыть(страница, "content")
    страница.click(f'#content-tabs button[data-tab="{вкладка}"]')
    страница.wait_for_selector(что_ждём, timeout=15000)
    страница.wait_for_timeout(700)
    _чисто(страница)
    # Пустой раздел — тоже отрисованный раздел, но здесь база наполнена, и
    # «загрузка…» на экране означает, что запрос не вернулся.
    тело = страница.inner_text("#content-body")
    assert "Загрузка" not in тело, тело[:300]
    assert "Не удалось" not in тело, тело[:300]


def test_a_chart_legend_never_says_undefined(страница):
    """Ряд подписывался полем label, а легенда читает name."""
    _открыть(страница, "content")
    страница.wait_for_selector("#chart-neg-time svg", timeout=15000)
    подписи = страница.inner_text("#content-body")
    assert "undefined" not in подписи.lower(), подписи[:400]
    assert "NaN" not in подписи, подписи[:400]


#: Задержка ответов на разрезы — внутри страницы, а не в обработчике route.
#
#: Так вышло не из вкусовых соображений. Обработчик `page.route` в
#: синхронном Playwright исполняется в том же потоке, что и команды теста:
#: `time.sleep` внутри него останавливает не только запрос, но и всё
#: общение с браузером. Клик по «Темам» тогда доезжает уже после того, как
#: разрезы дорисовались, порядок событий выходит правильный сам собой — и
#: проверка проходит даже на сломанном коде. Проверено прямо: со снятой
#: защитой в showTab тест с route-задержкой был зелёным.
#:
#: Подмена fetch живёт в самой странице и драйвер не держит: запрос
#: действительно висит, пока тест кликает дальше. Сигнал отмены передаётся
#: в родной fetch как есть, так что отменяемость запросов не меняется.
ЗАДЕРЖКА_РАЗРЕЗОВ = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/content/breakdown/')) {
        return new Promise((готово, отказ) => setTimeout(
          () => родной(вход, настройки).then(готово, отказ), 1200));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_switching_tabs_quickly_shows_the_one_that_was_chosen(страница):
    """Медленная вкладка дорисовывалась поверх уже выбранной.

    Проверка «вкладку успели сменить» стояла только в обработчике ошибки:
    подсвечены «Темы», показаны «Разрезы», и заметить это можно было
    только по содержимому таблицы.

    Разрезы задерживаются нарочно. На тридцати записях они отвечают за
    десяток миллисекунд, и гонки не случается ни разу из ста — то есть
    проверка без задержки не проверяла бы ничего. На архиве в сто тысяч
    записей эта задержка и есть настоящее время ответа.
    """
    страница.add_init_script(ЗАДЕРЖКА_РАЗРЕЗОВ)
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="groups"]')
    страница.wait_for_timeout(150)
    страница.click('#content-tabs button[data-tab="topics"]')
    страница.wait_for_selector("#chart-topics svg", timeout=20000)
    страница.wait_for_timeout(2500)

    подсвечена = страница.get_attribute(
        '#content-tabs button[data-tab="topics"]', "class")
    assert "active" in (подсвечена or ""), подсвечена
    тело = страница.inner_text("#content-body")
    assert "О чём говорят" in тело, тело[:300]
    assert "все показатели рядом" not in тело, (
        "разрезы дорисовались поверх выбранных тем:\n" + тело[:300])
    # Отдельно по разметке: текст можно однажды переписать, а вот график
    # «Тональность по владельцам» есть только у разрезов.
    assert страница.locator("#chart-by-owner").count() == 0, (
        "на экране остался график разрезов")
    _чисто(страница)


def test_the_period_can_be_changed_without_breaking_the_section(страница):
    """Смена периода перерисовывает раздел — и гасила таймер полосы разбора."""
    _открыть(страница, "content")
    for период in ("week", "day", "all", "month"):
        страница.click(f'#content-period button[data-period="{период}"]')
        страница.wait_for_timeout(1200)
    страница.wait_for_selector("#tone-bar svg", timeout=15000)
    _чисто(страница)
    assert страница.evaluate("() => window.__asrhub.state.contentPeriod") == "month"


def test_the_record_card_shows_the_analysis_and_seeks_by_click(страница):
    """Разбор без перехода по времени — справка, а не оглавление разговора."""
    _открыть(страница, "results")
    страница.wait_for_selector("#results-table button", timeout=15000)
    страница.click('#results-table button:has-text("Открыть")')
    страница.wait_for_selector("#job-tabs", timeout=10000)
    страница.click('#job-tabs button[data-tab="analysis"]')
    страница.wait_for_selector("#analysis-traj svg", timeout=15000)
    страница.wait_for_timeout(500)

    тело = страница.inner_text("#job-analysis")
    for заголовок in ("Ход тональности", "О чём говорили", "Что прозвучало"):
        assert заголовок in тело, тело[:400]
    # Строки разбора кликабельны: у них есть секунда записи.
    сколько = страница.evaluate(
        "() => document.querySelectorAll('.analysis-line[data-start]').length")
    assert сколько > 0, "строки разбора не привязаны ко времени"
    _чисто(страница)


def test_the_script_editor_checks_a_marker_on_a_real_record(страница):
    """Понять по списку слов, годится ли примета, нельзя — только на записи."""
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="script"]')
    страница.wait_for_selector("#script-check table", timeout=15000)

    строки = страница.inner_text("#script-check")
    assert "Выполнено пунктов" in строки, строки[:300]
    # Проверка идёт на настоящей записи, значит хоть один пункт где-то нашёлся.
    assert "есть" in строки or "нет" in строки, строки[:300]

    # Правка приметы перепроверяет пункт, не сохраняя настройку.
    поле = страница.query_selector(".script-item .script-any")
    поле.fill("здравствуйте, добрый день")
    страница.wait_for_timeout(1500)
    # «Добавить пункт» действительно добавляет: раньше вкладка перерисовывалась
    # и заново читала скрипт из настроек, теряя новый пункт.
    было = страница.evaluate("() => document.querySelectorAll('#script-list .script-item').length")
    страница.click("#script-add")
    страница.wait_for_timeout(800)
    assert страница.evaluate(
        "() => document.querySelectorAll('#script-list .script-item').length") == было + 1
    страница.click("#script-add-name")
    страница.wait_for_timeout(1500)
    assert "Проверяется по факту" in страница.inner_text("#script-list")
    assert "Обратился по имени" in страница.inner_text("#script-check")
    _чисто(страница)


def test_the_categories_tab_counts_edits_and_checks_a_rule(страница):
    """Категории: счёт за период по сохранённому набору, черновик правила
    проверяется на настоящей записи, ошибка правила показывается на месте,
    а строка таблицы ведёт в «Результаты» с отбором по категории."""
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="categories"]')
    страница.wait_for_selector("#cat-table", timeout=15000)
    страница.wait_for_selector("#cat-check table", timeout=15000)

    таблица = страница.inner_text("#cat-table")
    # В наполнении «сорван срок» и «суд» есть в каждой третьей записи:
    # готовые «Сроки» и «Эскалация» обязаны их собрать.
    assert "Сроки" in таблица and "Эскалация" in таблица, таблица[:400]
    assert "нет в наборе" not in таблица
    проверка = страница.inner_text("#cat-check")
    assert "Сработало категорий" in проверка, проверка[:300]
    # Драйверы негатива: плохие записи — про сроки и суд, и «Сроки» обязаны
    # тянуть вниз с подъёмом больше единицы.
    тело = страница.inner_text("#content-body")
    assert "Что тянет вниз" in тело and "×" in тело, тело[:800]
    # Флаг «сообщать» — часть черновика: его смена включает кнопку «Сохранить».
    страница.check(".script-item .cat-notify")
    assert not страница.is_disabled("#cat-save")

    # Правка правила перепроверяет черновик; сломанное правило — с ошибкой
    # под самим правилом, не сохраняя ничего.
    поле = страница.query_selector(".script-item .cat-rule")
    поле.fill("срок ИЛИ (")
    страница.wait_for_timeout(1500)
    assert "оборвано" in страница.inner_text("#cat-list .script-item .script-hit")
    поле.fill("срок ИЛИ поставка")
    страница.wait_for_timeout(1500)
    подсказка = страница.inner_text("#cat-list .script-item .script-hit")
    assert "совп." in подсказка or подсказка == "", подсказка
    assert not страница.is_disabled("#cat-save")

    # Переход к записям категории приносит отбор в «Результаты».
    страница.click('#cat-table button[data-category="deadline"]')
    страница.wait_for_selector("#results-table table", timeout=15000)
    страница.wait_for_timeout(1500)
    assert страница.input_value("#r-content") == "category:deadline"
    строк = страница.evaluate(
        "() => document.querySelectorAll('#results-table tbody tr').length")
    assert строк == 10, строк
    _чисто(страница)


def test_the_operators_tab_opens_a_card_and_marks_a_record_reviewed(страница):
    """Операторы: строка ведёт в карточку против команды и обратно; кнопка
    «Разобрано» убирает запись из очереди коучинга без перезагрузки."""
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="agents"]')
    страница.wait_for_selector("#agents-table", timeout=15000)
    таблица = страница.inner_text("#agents-table")
    assert "SPEAKER_00" in таблица, таблица[:300]
    было = страница.evaluate(
        "() => [...document.querySelectorAll('#content-body .card h3, #content-body .card .card-title, #content-body .card')]"
        ".map((c) => c.textContent).find((t) => t.includes('Очередь коучинга'))")
    assert было and "Очередь коучинга (" in было, было
    страница.click('#agents-table tr.clickable[data-agent="SPEAKER_00"]')
    страница.wait_for_selector("#agent-score-chart", timeout=15000)
    страница.wait_for_timeout(800)
    карточка = страница.inner_text("#content-body")
    assert "Против команды" in карточка and "Балл по неделям" in карточка, карточка[:500]
    страница.click("#agent-back")
    страница.wait_for_selector("#agents-table", timeout=15000)
    # Отметка «разобрано»: очередь становится короче на одну запись.
    до = страница.evaluate(
        "() => document.querySelectorAll('button[data-mark=\"coaching\"][data-status=\"done\"]').length")
    assert до > 0
    страница.click('button[data-mark="coaching"][data-status="done"]')
    страница.wait_for_timeout(1500)
    после = страница.evaluate(
        "() => document.querySelectorAll('button[data-mark=\"coaching\"][data-status=\"done\"]').length")
    assert после == до - 1, (до, после)
    _чисто(страница)


def test_the_record_card_lists_its_categories(страница):
    """Карточка записи показывает категории с примерами реплик."""
    _открыть(страница, "results")
    страница.wait_for_selector("#results-table table", timeout=15000)
    # ui001 — «плохая» запись: сорванный срок и суд.
    страница.evaluate("() => __asrhub.openJob('ui001')")
    страница.wait_for_selector('#job-tabs button[data-tab="analysis"]', timeout=15000)
    страница.click('#job-tabs button[data-tab="analysis"]')
    страница.wait_for_selector("#job-tab-body .analysis-line", timeout=15000)
    текст = страница.inner_text("#job-tab-body")
    assert "Категории обращения" in текст and "Сроки" in текст, текст[:600]
    _чисто(страница)


def test_the_results_list_can_be_filtered_by_content(страница):
    """Отбор по содержанию складывается с поиском — ради этого он и заведён."""
    _открыть(страница, "results")
    страница.wait_for_selector("#results-table table", timeout=15000)
    всего = страница.evaluate(
        "() => document.querySelectorAll('#results-table tbody tr').length")
    страница.select_option("#r-content", "alerts")
    страница.wait_for_timeout(1500)
    после = страница.evaluate(
        "() => document.querySelectorAll('#results-table tbody tr').length")
    assert 0 < после < всего, (всего, после)
    _чисто(страница)


def test_the_analytics_section_still_works(страница):
    """Соседний раздел проверяется тем же способом: его графики я тоже правил."""
    _открыть(страница, "analytics")
    страница.wait_for_selector("#chart-flow svg", timeout=20000)
    страница.wait_for_timeout(1000)
    тело = страница.inner_text("#analytics-body")
    assert "undefined" not in тело.lower(), тело[:400]
    assert "Не удалось" not in тело, тело[:400]
    _чисто(страница)
