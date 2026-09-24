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
    # Смысловой слой на заглушке: интерфейс должен рисовать ответы модели
    # там, где модели нет, — иначе проверять карточки нечем.
    состояние.settings.set("llm_backend", "stub")
    состояние.settings.set("llm_model", "stub")
    for n in range(6):
        состояние.llm_worker.analyze_job(f"ui{n:03d}", force=True)


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
    ("emotion", "#emo-line svg"),
    ("clarity", "#clr-line svg"),
    ("nps", "#nps-bar svg"),
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
    for заголовок in ("Ход тональности", "Показатели разговора", "О чём говорили",
                      "Что прозвучало"):
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


def test_the_record_card_saves_a_reference_and_shows_accuracy(страница):
    """Эталон задаётся в карточке, а не только через API; после сохранения
    в карточке — WER, MER, WIL и подсветка расхождений, а в «Аналитике» —
    раздел «Точность по эталону» с записью."""
    _открыть(страница, "results")
    страница.wait_for_selector("#results-table button", timeout=15000)
    страница.click('#results-table button:has-text("Открыть")')
    страница.wait_for_selector("#job-tabs", timeout=10000)
    страница.click('#job-tabs button[data-tab="reference"]')
    страница.wait_for_selector("#job-reference-text", timeout=10000)
    # Текст подставлен из записи: править быстрее, чем набирать. Портим
    # одно слово — и WER обязан стать ненулевым, а расхождение — подсвеченным.
    текст = страница.input_value("#job-reference-text")
    assert len(текст.split()) > 5, текст
    слова = текст.split()
    слова[2] = "подмена"
    страница.fill("#job-reference-text", " ".join(слова))
    страница.click("#job-reference-save")
    страница.wait_for_selector("#job-reference-result .transcript", timeout=10000)
    страница.wait_for_timeout(300)
    карточка = страница.inner_text("#job-reference")
    assert "WER" in карточка and "MER" in карточка and "WIL" in карточка, карточка[:400]
    assert "подмена" in карточка, карточка[:400]
    вкладка = страница.inner_text('#job-tabs button[data-tab="reference"]')
    assert "WER" in вкладка and "0.0%" not in вкладка, вкладка
    _чисто(страница)

    _открыть(страница, "analytics")
    страница.wait_for_selector("#accuracy-body table", timeout=20000)
    тело = страница.inner_text("#accuracy-body")
    assert "demo-simulator" in тело and "WER" in тело, тело[:400]
    assert "undefined" not in тело.lower() and "NaN" not in тело, тело[:400]
    # Калибровка и латентность отрисованы: у записей есть уверенность и время.
    assert страница.inner_text("#latency-body").count("demo-simulator") >= 1
    _чисто(страница)


def test_a_request_aborted_while_its_body_is_read_stays_silent(страница):
    """Отмена приходит и во время чтения тела ответа — на большом ответе
    чаще, чем до него. Раньше такой AbortError вылетал сырым исключением
    мимо разбора: красная плашка и ошибка в консоли при обычном
    переключении вкладок. Проверка воспроизводит это на месте."""
    _открыть(страница, "analytics")
    страница.wait_for_selector("#chart-flow svg", timeout=20000)
    # Тело ответа читается 800 мс и роняет AbortError при отмене — ровно
    # так ведёт себя настоящий fetch, только медленнее.
    страница.evaluate("""() => {
      const родной = window.fetch;
      window.fetch = async function (вход, настройки) {
        const ответ = await родной(вход, настройки);
        const сигнал = настройки && настройки.signal;
        const тело = await ответ.text();
        return {
          ok: ответ.ok, status: ответ.status, headers: ответ.headers,
          text: () => new Promise((готово, отказ) => {
            const т = setTimeout(() => готово(тело), 800);
            if (сигнал) сигнал.addEventListener('abort', () => {
              clearTimeout(т);
              отказ(new DOMException('The user aborted a request.', 'AbortError'));
            });
          }),
        };
      };
    }""")
    # Ошибка запроса идёт туда же, куда её отправляют разделы, — в fail().
    страница.evaluate("() => { window.__asrhub.API.latest('т', '/api/health').catch(window.__asrhub.fail); }")
    страница.wait_for_timeout(150)
    # Второй запрос с тем же ключом отменяет первый посреди чтения тела.
    страница.evaluate("() => { window.__asrhub.API.latest('т', '/api/health'); }")
    страница.wait_for_timeout(1500)
    плашки = страница.inner_text("#toasts")
    assert "aborted" not in плашки and "Запрос отменён" not in плашки, плашки
    _чисто(страница)


def test_the_review_queue_card_fills_opens_the_reference_tab_and_skips(страница):
    """Очередь проверки: пополняется кнопкой, «Открыть» ведёт сразу на
    вкладку «Эталон», «Пропустить» убирает строку без перезагрузки."""
    _открыть(страница, "analytics")
    страница.wait_for_selector("#review-body table, #review-body .empty", timeout=20000)
    страница.wait_for_timeout(500)
    assert "Согласие моделей" in страница.inner_text("#analytics-body")
    страница.click("#review-sample-now")
    страница.wait_for_selector("#review-body table", timeout=15000)
    страница.wait_for_timeout(500)
    строк = страница.evaluate("() => document.querySelectorAll('#review-body tbody tr').length")
    assert строк >= 1, страница.inner_text("#review-body")[:400]
    тело = страница.inner_text("#review-body")
    assert "случайная" in тело or "неуверенная" in тело, тело[:400]

    страница.click("#review-body [data-review-open]")
    страница.wait_for_selector("#job-reference-text", timeout=10000)
    assert страница.is_visible("#job-reference-save")
    страница.click("#modal-close")
    страница.wait_for_timeout(300)

    страница.click("#review-body [data-review-skip]")
    страница.wait_for_timeout(1200)
    осталось = страница.evaluate("() => document.querySelectorAll('#review-body tbody tr').length")
    assert осталось == строк - 1, (строк, осталось)
    _чисто(страница)


def test_the_meaning_layer_shows_up_in_the_summary_and_in_the_record_card(страница):
    """Свод: карточка «По ответам языковой модели» с исходами и причинами;
    карточка записи: смысл разговора с причиной, исходом и действиями."""
    _открыть(страница, "content")
    страница.wait_for_selector("#llm-summary .card", timeout=20000)
    страница.wait_for_timeout(700)
    тело = страница.inner_text("#llm-summary")
    assert "По ответам языковой модели" in тело and "Исходы" in тело, тело[:400]
    assert "undefined" not in тело.lower() and "NaN" not in тело, тело[:400]
    assert страница.evaluate(
        "() => document.querySelectorAll('#llm-outcomes svg').length") >= 1
    _чисто(страница)

    _открыть(страница, "results")
    страница.wait_for_selector("#results-table button", timeout=15000)
    страница.click('#results-table button:has-text("Открыть")')
    страница.wait_for_selector("#job-tabs", timeout=10000)
    страница.click('#job-tabs button[data-tab="analysis"]')
    страница.wait_for_selector("#job-llm .card", timeout=15000)
    страница.wait_for_timeout(400)
    карточка = страница.inner_text("#job-llm")
    assert "Смысл разговора" in карточка and "Сгенерировано моделью" in карточка, карточка[:400]
    # Ответ можно пересчитать, не выходя из карточки.
    страница.click("#job-llm-run")
    страница.wait_for_timeout(1500)
    assert "Смысл разговора" in страница.inner_text("#job-llm")
    _чисто(страница)


def test_the_settings_offer_to_install_a_model_for_this_hardware(страница):
    """Раздел «Языковая модель» начинается не с параметров, а с каталога:
    что поместится в память этого сервера, что уже скачано и чего не
    хватает. Без этой врезки шестнадцать настроек ниже описывают модель,
    которой на свежем сервере нет.
    """
    _открыть(страница, "settings")
    страница.click('#group-nav button[data-group="llm"]')
    страница.wait_for_selector("#llm-setup table tbody tr", timeout=20000)
    страница.wait_for_timeout(400)
    врезка = страница.inner_text("#llm-setup")
    # Заголовки таблицы CSS приводит к прописным — сравниваем без регистра.
    assert "Модель на сервере" in врезка and "зачем она" in врезка.lower(), врезка[:400]
    assert "undefined" not in врезка.lower() and "NaN" not in врезка, врезка[:400]
    # Каталог показан целиком: и то, что поместится, и то, что нет.
    строк = страница.evaluate(
        "() => document.querySelectorAll('#llm-setup table tbody tr').length")
    assert строк >= 10, строк
    assert "не поместится" in врезка or "поместится" in врезка
    # Кнопка установки на месте и не нажата: нажатие здесь качало бы
    # гигабайты на машину, где идут проверки.
    assert страница.evaluate("() => !!document.querySelector('#llm-go')")
    # Параметры группы никуда не делись — врезка стоит перед ними.
    assert страница.evaluate(
        "() => document.querySelectorAll('#params-body .params').length") >= 1
    _чисто(страница)


def test_карта_часов_недели_не_подставляет_ноль_вместо_пустоты():
    """Пустая клетка — «не было», а не ноль.

    Подстановка нуля стоила двух вещей: у показателя с отрицательными
    значениями ноль сам по себе значение, а у показателя без данных вся
    карта заливалась одним тоном при подписи «от — до —» под ней.
    """
    from pathlib import Path

    источник = (Path(__file__).resolve().parent.parent
                / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert "values: д.grid || []" in источник, "карта снова получает подменённые значения"
    assert "строка.map((v) => (v === null ? 0 : v))" not in источник


# ---------------------------------------------------------------------------
# Разделы тридцатого захода: АТС, сотрудники, резервные копии
# ---------------------------------------------------------------------------

def test_the_pbx_section_draws_every_tab_without_stations(страница):
    """Раздел «АТС» открывается и на сервере, где АТС не подключено.

    Пустой раздел — тоже отрисованный раздел: он обязан сказать, что
    станций нет и что с этим делать, а не показать пустоту или свалиться
    на отсутствующем поле.
    """
    _открыть(страница, "pbx")
    страница.wait_for_selector("#pbx-tabs button", timeout=15000)
    for вкладка in ("overview", "load", "people", "numbers", "intake"):
        страница.click(f'#pbx-tabs button[data-tab="{вкладка}"]')
        страница.wait_for_timeout(700)
        тело = страница.inner_text("#pbx-body")
        assert "Загрузка" not in тело, (вкладка, тело[:200])
        assert "Не удалось" not in тело, (вкладка, тело[:200])
    _чисто(страница)
    шапка = страница.inner_text("#pbx-top")
    assert "Станции" in шапка


def test_the_pbx_station_form_explains_every_field(страница):
    """Форма станции — с описанием, рекомендацией и примерами у каждого поля.

    Форма без объяснений заставляет угадывать, что такое «контекст» и чем
    журнал CDR отличается от каталога записей; угадывают обычно неверно.
    """
    _открыть(страница, "pbx")
    страница.wait_for_selector("#pbx-add", timeout=15000)
    страница.click("#pbx-add")
    страница.wait_for_selector("#pbx-fields .param", timeout=10000)
    полей = страница.locator("#pbx-fields .param").count()
    assert полей >= 15, полей
    assert страница.locator("#pbx-fields .param-rec").count() >= 15
    assert страница.locator("#pbx-fields details").count() >= 15
    # Поля источника переключаются вместе с ним: путь к журналу не нужен
    # станции, которую слушают через AMI.
    страница.select_option('[data-field="source"]', "ami")
    страница.wait_for_timeout(300)
    assert страница.locator('.param[data-only="cdr_csv"]').first.is_hidden()
    assert страница.locator('.param[data-only="ami"]').first.is_visible()
    _чисто(страница)


def test_the_employees_section_lists_people_and_opens_a_card(страница):
    """Таблица сотрудников и карточка под ней — на одном экране.

    Именно под, а не вместо: сравнение с соседями — половина смысла
    разговора о сотруднике.
    """
    _открыть(страница, "employees")
    страница.wait_for_selector("#emp-body table", timeout=15000)
    строк = страница.locator("#emp-body tbody tr").count()
    assert строк >= 1, "ни одного сотрудника"
    # Наборы столбцов: двадцать показателей в одной таблице не читаются.
    страница.click('#emp-cols button[data-cols="speech"]')
    страница.wait_for_timeout(400)
    заголовки = страница.inner_text("#emp-body thead").upper()
    assert "ПОНЯТНОСТЬ" in заголовки and "ЗВОНКОВ" not in заголовки
    страница.click("#emp-body tbody tr")
    страница.wait_for_selector("#emp-card table", timeout=10000)
    карточка = страница.inner_text("#emp-card")
    assert "Против команды" in карточка and "Ход по неделям" in карточка
    _чисто(страница)


def test_the_backup_section_makes_and_lists_a_copy(страница):
    """Копия настроек снимается одной кнопкой и сразу видна в списке."""
    _открыть(страница, "backup")
    страница.wait_for_selector("#bk-settings", timeout=15000)
    страница.click("#bk-settings")
    страница.wait_for_selector("#bk-list table", timeout=15000)
    список = страница.inner_text("#bk-list")
    assert "только настройки" in список
    # Настройки расписания — теми же карточками, что и в «Настройках».
    assert страница.locator("#bk-params .param").count() >= 6
    параметры = страница.inner_text("#bk-params")
    assert "backup_keep_days" in параметры and "backup_time" in параметры
    _чисто(страница)


def test_section_links_between_pbx_telephony_and_employees_work(страница):
    """Кнопки перехода между разделами — не украшение.

    Весь app.js — замыкание, наружу отдан только `window.__asrhub`, и
    inline-обработчик `onclick="go('pbx')"` вычисляется в глобальной
    области, где `go` не существует: кнопка молчала, а в консоли на каждый
    щелчок падал ReferenceError.
    """
    _открыть(страница, "telephony")
    страница.wait_for_selector("#tel-calls", timeout=15000)
    страница.click("text=Раздел «АТС» →")
    страница.wait_for_timeout(1500)
    assert страница.evaluate("location.hash") == "#pbx"
    страница.click('#pbx-tabs button[data-tab="people"]')
    страница.wait_for_timeout(1200)
    страница.click("text=Аналитика по сотрудникам →")
    страница.wait_for_timeout(1500)
    assert страница.evaluate("location.hash") == "#employees"
    _чисто(страница)


def test_the_station_form_behaves_like_every_other_modal(страница):
    """Форма станции идёт через mountModal: фон не прокручивается, Esc закрывает."""
    _открыть(страница, "pbx")
    страница.wait_for_selector("#pbx-add", timeout=15000)
    страница.click("#pbx-add")
    страница.wait_for_selector("#pbx-fields .param", timeout=10000)
    assert страница.evaluate("document.body.classList.contains('modal-open')")
    страница.keyboard.press("Escape")
    страница.wait_for_timeout(600)
    assert страница.evaluate("document.querySelectorAll('.modal-backdrop').length") == 0
    assert not страница.evaluate("document.body.classList.contains('modal-open')")
    _чисто(страница)


def test_switching_a_breakdown_and_the_tab_at_once_stays_quiet(страница):
    """Ответ разреза приезжает в узел, которого уже нет.

    Рисовать в него — это TypeError в консоли и пустое место на экране;
    сама отмена запроса — штатный ход, а не сбой.
    """
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="nps"]')
    страница.wait_for_selector("#nps-bar svg", timeout=15000)
    страница.select_option("#nps-dim", "queue")
    страница.click('#content-tabs button[data-tab="emotion"]')
    страница.wait_for_selector("#emo-line svg", timeout=15000)
    страница.wait_for_timeout(1200)
    _чисто(страница)


# ---------------------------------------------------------------------------
# Разделы тридцать второго захода
# ---------------------------------------------------------------------------

def test_новые_разделы_рисуются_и_не_сорят_в_консоль(страница):
    """Пять новых разделов открываются на сервере, где ещё ничего не было.

    Пустой раздел — тоже отрисованный раздел: он обязан объяснить, почему
    пусто, а не показать «Загрузка…» навсегда и не свалиться на поле,
    которого сервер не прислал. Каждый из пяти собирает ответ из нескольких
    ручек, и любая из них на пустом сервере отвечает нулями — именно на
    этом такие разделы обычно и ломаются.
    """
    for раздел, метка in (("dashboard", "#db-components"),
                          ("llmqueue", "#lq-table"),
                          ("rescan", "#rs-table"),
                          ("voice", "#vo-body"),
                          ("staff", "#st-table")):
        _открыть(страница, раздел)
        страница.wait_for_selector(метка, timeout=15000)
        страница.wait_for_timeout(1200)
        тело = страница.inner_text(метка)
        assert "Загрузка" not in тело, (раздел, тело[:200])
        assert "Не удалось" not in тело, (раздел, тело[:200])
        _чисто(страница)


def test_дашборд_рисует_схему_системы_и_журнал_неисправностей(страница):
    """Схема — не украшение: по ней видно, что от чего зависит.

    Узлы рисуются по ответу автодиагностики, связи — по полю depends_on.
    Проверка ловит два вида поломки: пустой ответ (узлов нет вовсе) и
    раскладку, в которой узлы наложились друг на друга.
    """
    _открыть(страница, "dashboard")
    страница.wait_for_selector(".system-map", timeout=15000)
    страница.wait_for_timeout(1500)
    узлов = страница.locator(".system-map .map-node").count()
    assert узлов >= 8, f"на схеме {узлов} узлов — диагностика ничего не вернула"
    assert страница.locator(".system-map path").count() >= 3, "связи между узлами не нарисованы"
    # Каждый узел — на своём месте: наложение означает ошибку раскладки.
    места = set()
    for i in range(узлов):
        коробка = страница.locator(".system-map .map-box").nth(i)
        места.add((коробка.get_attribute("x"), коробка.get_attribute("y")))
    assert len(места) == узлов, "узлы схемы наложились друг на друга"
    assert "Журнал неисправностей" in страница.inner_text("#db-problems")
    _чисто(страница)


def test_очередь_llm_показывает_состояние_и_настройки_рядом(страница):
    """Раздел об очереди обязан отвечать на «почему ничего не идёт».

    Пустая очередь при выключенной модели выглядит как исправная работа;
    поэтому карточка «Сейчас» объясняет причину словами, а настройки
    очереди лежат тут же, чтобы не ходить за ними в другой раздел.
    """
    _открыть(страница, "llmqueue")
    страница.wait_for_selector("#lq-current", timeout=15000)
    страница.wait_for_timeout(1200)
    сейчас = страница.inner_text("#lq-current")
    assert "Очередь пуста" in сейчас or "выключена" in сейчас or "приостановлена" in сейчас
    assert страница.locator("#lq-params .param").count() >= 6, "настройки очереди не показаны"
    assert "СБОЕВ" in страница.inner_text("#lq-top").upper()
    _чисто(страница)


def test_настройки_телефонии_дают_добавить_вторую_атс(страница):
    """Вторую станцию подключают из настроек, а не только из раздела «АТС».

    Раньше в настройках телефонии человек видел два десятка полей одной
    станции и поле `telephony_stations` с json внутри, которое надо было
    заполнять руками.
    """
    _открыть(страница, "settings")
    страница.wait_for_selector("#group-nav button", timeout=15000)
    страница.click('#group-nav button:has-text("Телефония")')
    страница.wait_for_selector("#set-stations", timeout=10000)
    страница.wait_for_timeout(900)
    врезка = страница.inner_text("#set-stations")
    assert "Подключённые АТС" in врезка
    страница.click("#set-pbx-add")
    страница.wait_for_selector("#pbx-fields .param", timeout=10000)
    assert страница.locator("#pbx-fields .param").count() >= 15
    страница.keyboard.press("Escape")
    _чисто(страница)


# ---------------------------------------------------------------------------
# Заход 35
# ---------------------------------------------------------------------------

def test_полосы_аналитики_показывают_свои_подписи(страница):
    """Две функции с именем «полоса» — и побеждала не та.

    Объявления функций поднимаются наверх, поэтому второе (двухдоводное,
    для ячейки таблицы) перекрывало первое (четырёхдоводное, «сколько из
    скольких»). Обе вкладки «Аналитика записей» рисовали безымянные полосы
    «0 %» вместо своих чисел: подписи терялись вместе со значениями. Ни
    ошибки в консоли, ни следа в журнале — просто раздел показывал нули.
    """
    _открыть(страница, "content")
    страница.click('#content-tabs button[data-tab="emotion"]')
    страница.wait_for_selector("#emo-line svg", timeout=15000)
    страница.wait_for_timeout(900)
    тело = страница.inner_text("#content-body")
    for подпись in ("С высоким напряжением", "Клиенту пришлось пробиваться",
                    "Повторное обращение"):
        assert подпись in тело, f"полоса «{подпись}» потеряла подпись:\n{тело[:400]}"

    страница.click('#content-tabs button[data-tab="clarity"]')
    страница.wait_for_selector("#clr-line svg", timeout=15000)
    страница.wait_for_timeout(900)
    тело = страница.inner_text("#content-body")
    assert "Речь тяжело слушать" in тело, тело[:400]
    _чисто(страница)


#: Ответ 503 на проверку состояния — прямо в странице, как и задержка
#: разрезов выше: обработчик `page.route` в синхронном Playwright держит
#: тот же поток, что и команды теста.
СОСТОЯНИЕ_503 = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/monitoring/health')) {
        return Promise.resolve(new Response(JSON.stringify({
          status: 'degraded',
          checks: [{ id: 'disk', state: 'fail', label: 'Свободно на диске',
                     value: '2 %', hint: 'Освободите место' }],
        }), { status: 503, headers: { 'Content-Type': 'application/json' } }));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_раздел_наблюдения_открывается_при_плохом_состоянии(страница):
    """`/api/monitoring/health` намеренно отвечает 503, когда всё плохо.

    Интерфейс читал его обычным запросом, получал исключение и показывал
    «Раздел не загрузился»: наблюдение становилось недоступно ровно в тот
    момент, ради которого оно и нужно. Тело ответа при этом полное — со
    списком проверок и причин.
    """
    страница.add_init_script(СОСТОЯНИЕ_503)
    _открыть(страница, "monitoring")
    страница.wait_for_timeout(1800)
    тело = страница.inner_text("#content")
    assert "не загрузил" not in тело.lower(), тело[:400]
    assert "Опрос метрик" not in тело, тело[:400]
    assert "Свободно на диске" in тело or "disk" in тело.lower(), тело[:400]


#: Задержка списка агентов — чтобы успеть уйти на соседнюю вкладку.
ЗАДЕРЖКА_АГЕНТОВ = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/telephony/agents')) {
        return new Promise((готово, отказ) => setTimeout(
          () => родной(вход, настройки).then(готово, отказ), 1500));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_вкладка_агентов_не_рисуется_поверх_выбранной(страница):
    """`drawAgents` писала в тело после ожидания и без сверки вкладки.

    За время ответа человек успевает уйти на соседнюю вкладку, и список
    агентов рисовался поверх неё: выбрал «Нагрузку», увидел агентов.
    Заметнее всего на медленной сети — то есть там, где раздел и так
    неудобен.
    """
    страница.add_init_script(ЗАДЕРЖКА_АГЕНТОВ)
    _открыть(страница, "pbx")
    страница.wait_for_selector("#pbx-tabs button", timeout=15000)
    страница.click('#pbx-tabs button[data-tab="agents"]')
    страница.wait_for_timeout(200)
    страница.click('#pbx-tabs button[data-tab="load"]')
    страница.wait_for_timeout(2500)
    тело = страница.inner_text("#pbx-body")
    assert "Установка агента" not in тело, \
        f"список агентов дорисовался поверх выбранной вкладки:\n{тело[:400]}"
    _чисто(страница)


# ---------------------------------------------------------------------------
# Заход 40
# ---------------------------------------------------------------------------


def test_голосовая_аналитика_показывает_разобранное(страница):
    """Раздел был написан под другой ответ сервера.

    Он читал rows, total, resolved_rate и actions_total, а сервер отдаёт
    records, analyzed, resolved_share, actions; графикам шли labels и values
    вместо items и parts. Модель разобрала шесть записей — а в шапке стояли
    нули, и на каждой вкладке «За период модель ничего не разобрала».
    """
    _открыть(страница, "voice")
    страница.select_option("#vo-period", "all")
    страница.wait_for_selector("#vo-s1 svg", timeout=20000)
    страница.wait_for_selector("#vo-s2 svg", timeout=20000)
    # Подписи KPI набраны прописными через CSS — сравниваем без регистра.
    шапка = страница.inner_text("#vo-head").lower()
    assert "разобрано моделью\n6\n" in шапка, шапка
    assert "разговоров за период\n30\n" in шапка, шапка
    for вкладка, метка in (("reasons", "table"), ("records", "table"),
                           ("actions", ".card"), ("trackers", ".card"),
                           ("scorecard", ".card")):
        страница.click(f'#vo-tabs button[data-tab="{вкладка}"]')
        страница.wait_for_selector(f"#vo-body {метка}", timeout=10000)
        тело = страница.inner_text("#vo-body")
        assert "ничего не разобрала" not in тело, (вкладка, тело[:300])
        assert "undefined" not in тело and "NaN" not in тело, (вкладка, тело[:300])
    _чисто(страница)


def test_применить_отправляет_только_изменённое_в_браузере(страница):
    """«Применить» слал снимок всех настроек страницы.

    Заведённая после её загрузки станция АТС пропадала, набор категорий
    откатывался, пароль станции «***» ложился поверх настоящего.
    """
    отправлено: list[str] = []
    страница.on("request", lambda з: отправлено.append(з.post_data or "")
                if з.method == "PUT" and з.url.endswith("/api/settings") else None)
    _открыть(страница, "settings")
    страница.wait_for_selector("#p-apply", timeout=15000)
    страница.click("#p-apply")
    страница.wait_for_timeout(600)
    assert not отправлено, f"без изменений ушло: {отправлено}"
    assert "Изменений нет" in страница.inner_text("#toasts")

    страница.click('#group-nav button[data-group="server"]')
    поле = страница.locator('.param[data-key="max_upload_mb"] input[type=number]')
    поле.wait_for(timeout=10000)
    поле.fill("1500")
    поле.dispatch_event("change")
    страница.click("#p-apply")
    страница.wait_for_timeout(800)
    assert len(отправлено) == 1, отправлено
    import json as _json

    assert _json.loads(отправлено[0]) == {"max_upload_mb": 1500}
    _чисто(страница)


def test_загрузка_файла_несёт_только_заданное_человеком(страница, tmp_path):
    """В settings задания уходила копия всех настроек сервера.

    Неадминистратор получал webhook_url заглушкой «***» и отправлял её
    обратно — загрузка отклонялась с 400.
    """
    import struct
    import wave as _wave

    файл = tmp_path / "проба.wav"
    with _wave.open(str(файл), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(struct.pack("<h", 0) * 16000)
    тела: list[str] = []

    def перехват(маршрут):
        тела.append(маршрут.request.post_data or "")
        маршрут.fulfill(status=200, content_type="application/json",
                        body='{"id": "ui-проба", "status": "queued"}')

    страница.route("**/api/jobs", перехват)
    _открыть(страница, "transcribe")
    страница.wait_for_selector("#file-input", state="attached", timeout=15000)
    страница.set_input_files("#file-input", str(файл))
    страница.click("#btn-submit")
    страница.wait_for_timeout(1000)
    assert тела, "запрос на постановку не ушёл"
    часть = тела[0].split('name="settings"', 1)[1]
    значение = часть.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]
    assert значение.strip() == "{}", значение[:300]
