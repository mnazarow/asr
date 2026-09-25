"""Заход 46: веб-интерфейс — проверки настоящим браузером.

Каждая проверка здесь воспроизводит то, что человек видел на экране:
карточка «Контроль качества» с вечным «Загрузка…», отбор «за сутки»,
показывавший весь архив, глубокая проверка, которую снимал опрос, ссылки
скачивания, отвечавшие 401 вошедшему по ключу, и так далее. Проверяется
поведение, а не разметка: число запросов, содержимое таблиц, открытые окна,
скачанные файлы.

Стендов два. Первый — без входа, с наполненной базой, звонками, журналом
доступа и файлом конфигурации: на нём проверяется, что разделы работают и
что «Сохранено» значит «записано в config.yaml». Второй — со входом: сессии,
ключи, обязательная смена пароля и закрытые метрики.

Браузер берётся так же, как в `test_web_ui.py`; нет браузера — проверки
пропускаются.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from test_web_ui import _браузер, _наполнить, _свободный_порт

#: Пароль администратора на стенде со входом — не по умолчанию.
ПАРОЛЬ = "Проверка-46-пароль"


def _поднять(приложение) -> tuple[str, object, threading.Thread]:
    import uvicorn

    порт = _свободный_порт()
    служба = uvicorn.Server(uvicorn.Config(приложение, host="127.0.0.1", port=порт,
                                           log_level="warning"))
    поток = threading.Thread(target=служба.run, daemon=True)
    поток.start()
    for _ in range(200):
        if служба.started:
            break
        time.sleep(0.05)
    assert служба.started, "сервер не поднялся"
    return f"http://127.0.0.1:{порт}", служба, поток


class _Окружение:
    """Временные переменные окружения — с возвратом прежних."""

    КЛЮЧИ = ("ASRHUB_DATA_DIR", "ASRHUB_AUTH_ENABLED", "ASRHUB_MODEL", "ASRHUB_ENGINE",
             "ASRHUB_CONFIG")

    def __init__(self, **значения: str) -> None:
        self.прежние = {к: os.environ.get(к) for к in self.КЛЮЧИ}
        self.значения = значения

    def __enter__(self):
        for к in self.КЛЮЧИ:
            os.environ.pop(к, None)
        os.environ.update(self.значения)
        return self

    def __exit__(self, *_):
        for к, v in self.прежние.items():
            if v is None:
                os.environ.pop(к, None)
            else:
                os.environ[к] = v


def _проверить_среду() -> None:
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if _браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    """Сервер без входа: база из `test_web_ui`, звонки, журнал, файл конфигурации."""
    _проверить_среду()
    каталог = tmp_path_factory.mktemp("ui46") / "data"
    каталог.mkdir(parents=True)
    конфигурация = каталог / "config.yaml"
    конфигурация.write_text("queue:\n  max_concurrent_jobs: 1\n", encoding="utf-8")
    with _Окружение(ASRHUB_DATA_DIR=str(каталог), ASRHUB_AUTH_ENABLED="false",
                    ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo",
                    ASRHUB_CONFIG=str(конфигурация)):
        from asrhub.api.app import create_app

        приложение = create_app(start_queue=False)
        _наполнить(приложение)
        состояние = приложение.state.hub
        db = состояние.db
        сейчас = time.time()
        # Ещё девяносто заданий — чтобы у «Результатов» была вторая страница.
        for n in range(90):
            ид = db.create_job({"id": f"extra{n:03d}", "filename": f"старое-{n}.wav",
                                "owner": "борис", "engine": "demo",
                                "model": "demo-simulator", "language": "ru",
                                "source": "web", "media_duration_s": 30.0})
            когда = сейчас - (40 + n) * 86400
            db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, ид))
            db.update_job(ид, status="completed", text=f"давний разговор номер {n}",
                          finished_at=когда + 20, words_count=4, segments_count=1,
                          rtf=0.1, processing_time_s=3.0, avg_confidence=0.9)
        # Звонки: три давних (для «Год») и два свежих (для «Сутки»).
        for n, (давность, номер) in enumerate([(200, "111"), (210, "112"), (220, "113"),
                                               (0.1, "221"), (0.2, "222")]):
            db.save_call(f"call-{n}", owner="борис", src=f"+7900{номер}0000",
                         dst="100", direction="входящий", answered=True,
                         duration=60, billsec=50, started_at=сейчас - давность * 86400)
        # Журнал доступа на три страницы.
        for n in range(130):
            db.audit_add(action=f"действие-{n:03d}", actor="проверка", method="GET",
                         path=f"/api/test/{n}", status=200, ip="127.0.0.1")
        адрес, служба, поток = _поднять(приложение)
        yield {"адрес": адрес, "состояние": состояние, "конфигурация": конфигурация}
        служба.should_exit = True
        поток.join(timeout=10)


@pytest.fixture(scope="module")
def стенд_со_входом(tmp_path_factory):
    """Сервер со входом: администратор, пользователь с временным паролем, ключ."""
    _проверить_среду()
    каталог = tmp_path_factory.mktemp("ui46auth") / "data"
    каталог.mkdir(parents=True)
    конфигурация = каталог / "config.yaml"
    конфигурация.write_text("monitoring:\n  monitoring_public: false\n", encoding="utf-8")
    with _Окружение(ASRHUB_DATA_DIR=str(каталог), ASRHUB_AUTH_ENABLED="true",
                    ASRHUB_MODEL="demo-simulator", ASRHUB_ENGINE="demo",
                    ASRHUB_CONFIG=str(конфигурация)):
        from asrhub.api.app import create_app

        приложение = create_app(start_queue=False)
        состояние = приложение.state.hub
        учётки = состояние.accounts
        админ = учётки.by_username("admin")
        учётки.update(админ.id, must_change_password=False)
        учётки.set_password(админ.id, ПАРОЛЬ)
        учётки.create("новичок", "Временный-1-пароль", role="user",
                      must_change_password=True)
        учётки.create("аналитик", "Аналитик-1-пароль", role="admin")
        ключ = "ah_" + "k" * 30
        состояние.settings.api_keys[ключ] = {"name": "ключ-админа", "role": "admin",
                                             "enabled": True}
        состояние.db.create_job({"id": "auth001", "filename": "запись.wav",
                                 "owner": "ключ-админа", "engine": "demo",
                                 "model": "demo-simulator", "language": "ru",
                                 "source": "web", "media_duration_s": 5.0})
        адрес, служба, поток = _поднять(приложение)
        yield {"адрес": адрес, "состояние": состояние, "ключ": ключ,
               "конфигурация": конфигурация}
        служба.should_exit = True
        поток.join(timeout=10)


@pytest.fixture(scope="module")
def браузер():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        б = playwright.chromium.launch(executable_path=_браузер())
        try:
            yield б
        finally:
            б.close()


def _вкладка(браузер, адрес: str, *, ключ: str = "", ширина: int = 1600,
             скрипт: str = ""):
    """Новая вкладка в своём контексте — со счётчиком ошибок консоли и запросов."""
    контекст = браузер.new_context(viewport={"width": ширина, "height": 1000},
                                   accept_downloads=True)
    if ключ:
        контекст.add_init_script(f"localStorage.setItem('asrhub_key', {json.dumps(ключ)});")
    if скрипт:
        контекст.add_init_script(скрипт)
    вкладка = контекст.new_page()
    вкладка.ошибки = []
    вкладка.запросы = []
    вкладка.on("pageerror", lambda e: вкладка.ошибки.append(f"исключение: {e}"))
    вкладка.on("console", lambda m: вкладка.ошибки.append(f"консоль: {m.text}")
               if m.type == "error" else None)
    вкладка.on("request", lambda r: вкладка.запросы.append(r.url))
    вкладка.адрес = адрес
    return вкладка


def _открыть(вкладка, раздел: str, пауза: int = 1500) -> None:
    вкладка.goto(f"{вкладка.адрес}/#{раздел}", wait_until="networkidle")
    вкладка.wait_for_timeout(пауза)


def _без_исключений(вкладка) -> None:
    """Необработанных исключений на странице быть не должно ни одного."""
    исключения = [о for о in вкладка.ошибки if о.startswith("исключение")]
    assert not исключения, "\n".join(исключения)


# ---------------------------------------------------------------------------
# Разделы, которые показывали не то
# ---------------------------------------------------------------------------


def test_карточка_контроля_качества_рисуется_вместе_с_аналитикой(стенд, браузер):
    """Вызов стоял внутри «Пропустить» у соседней очереди — «Загрузка…» навсегда."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "analytics", 3000)
    вкладка.wait_for_selector("#qa-body .kpi", timeout=15000)
    тело = вкладка.inner_text("#qa-body")
    assert "Загрузка" not in тело, тело[:200]
    assert "Ждут проверки" in тело or "ЖДУТ ПРОВЕРКИ" in тело, тело[:200]
    _без_исключений(вкладка)


def test_отбор_за_сутки_в_распознавании_записей_работает(стенд, браузер):
    """Список понимает since_hours, а слался since: «за сутки» показывало весь архив."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "rescan")
    вкладка.select_option("#rs-period", "1")
    вкладка.wait_for_timeout(1500)
    сверху = вкладка.inner_text("#rs-top")
    # За сутки — двадцать четыре задания из ста двадцати: по одному в час.
    assert "24" in сверху.split("\n")[1], сверху[:200]
    assert any("since_hours=24" in з for з in вкладка.запросы), вкладка.запросы[-5:]
    assert not any("since=" in з and "/api/jobs?" in з for з in вкладка.запросы)
    # Модели для отбора — из архива, с числом заданий, а не весь каталог.
    модели = вкладка.eval_on_selector_all("#rs-model option", "о => о.map(x => x.value)")
    assert модели == ["", "demo-simulator"], модели
    _без_исключений(вкладка)


#: Годовой ответ журнала звонков задерживается внутри страницы — как в
#: `test_web_ui`: задержка в обработчике `route` останавливала бы и сам тест.
ЗАДЕРЖКА_ГОДА = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/telephony/calls') && адрес.includes('period=year')) {
        return new Promise((готово, отказ) => setTimeout(
          () => родной(вход, настройки).then(готово, отказ), 1500));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_журнал_звонков_показывает_последний_выбранный_период(стенд, браузер):
    """«Год», сразу «Сутки» — и медленный годовой ответ рисовался поверх."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ЗАДЕРЖКА_ГОДА)
    _открыть(вкладка, "telephony", 2000)
    вкладка.click('#tel-period button[data-period="year"]')
    вкладка.wait_for_timeout(200)
    вкладка.click('#tel-period button[data-period="day"]')
    вкладка.wait_for_timeout(3000)
    таблица = вкладка.inner_text("#tel-calls")
    assert "+79002210000" in таблица and "+79002220000" in таблица, таблица[:300]
    assert "+79001110000" not in таблица, "годовой ответ пришёл позже и перерисовал таблицу"
    _без_исключений(вкладка)


#: Глубокая проверка — три секунды; счётчик её запросов — в самой странице.
ЗАДЕРЖКА_ГЛУБОКОЙ = """
  (() => {
    window.__глубоких = 0;
    window.__идёт = 0;
    window.__быстрыхВоВремя = 0;
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/system/selfcheck') && адрес.includes('deep=1')) {
        window.__глубоких += 1;
        window.__идёт += 1;
        return new Promise((готово, отказ) => setTimeout(
          () => родной(вход, настройки).then(готово, отказ)
            .finally(() => { window.__идёт -= 1; }), 3000));
      }
      if (адрес.includes('/api/system/selfcheck') && window.__идёт) {
        window.__быстрыхВоВремя += 1;
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_опрос_дашборда_не_снимает_глубокую_проверку(стенд, браузер):
    """Опрос шёл тем же ключом отмены и сам запускал глубокие проверки по кругу."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ЗАДЕРЖКА_ГЛУБОКОЙ)
    _открыть(вкладка, "dashboard", 2000)
    вкладка.click("#db-deep")
    вкладка.wait_for_timeout(300)
    # Опрос раз в десять секунд — здесь вызываем его сами, дважды.
    вкладка.evaluate("() => { const д = window.__asrhub.RENDERERS.dashboard;"
                     " д.load(true); д.load(true); }")
    # «Обновить» во время глубокой проверки тоже её не снимает: быстрая идёт
    # своим ключом отмены, а её ответ не рисуется поверх глубокой.
    вкладка.click("#db-refresh")
    вкладка.wait_for_timeout(1000)
    assert "выполнена" not in вкладка.inner_text("#toasts"), "сообщение раньше результата"
    вкладка.wait_for_timeout(3500)
    assert "Глубокая проверка выполнена" in вкладка.inner_text("#toasts")
    assert "глубокая проверка" in вкладка.inner_text("#db-top")
    # Опрос после неё результат не затирает и глубоких проверок не запускает.
    вкладка.evaluate("() => window.__asrhub.RENDERERS.dashboard.load(true)")
    вкладка.wait_for_timeout(1500)
    assert "глубокая проверка" in вкладка.inner_text("#db-top")
    assert вкладка.evaluate("() => window.__глубоких") == 1
    # Пока глубокая идёт, опрос на сервер не ходит вовсе.
    # Опрос во время глубокой на сервер не ходит; «Обновить» — один раз.
    assert вкладка.evaluate("() => window.__быстрыхВоВремя") == 1
    _без_исключений(вкладка)


def test_разобрано_в_очереди_модели_рисуется_столбцами(стенд, браузер):
    """Ряды уходили в полосу долей, которая их не читает: «Пока нет данных»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "llmqueue", 2500)
    вкладка.wait_for_selector("#lq-c1 svg", timeout=15000)
    легенда = вкладка.inner_text("#lq-c1")
    assert "успешно" in легенда and "сбои" in легенда, легенда
    столбцов = вкладка.evaluate("() => document.querySelectorAll('#lq-c1 svg path').length")
    assert столбцов >= 1
    _без_исключений(вкладка)


def test_деления_оси_у_мелких_значений_различаются(стенд, браузер):
    """Шаг 0,0002 при трёх знаках давал «0,000» у двух делений подряд."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    оси, одно = вкладка.evaluate("""() => {
      const оси = [];
      for (const значения of [[0.0002, 0.0004, 0.0006], [0.5, 1.5, 2]]) {
        const место = document.createElement('div');
        место.style.width = '600px';
        document.body.appendChild(место);
        window.Charts.line(место, { labels: значения.map((_, i) => String(i)),
                                    series: [{ name: 'ряд', values: значения }] });
        оси.push([...место.querySelectorAll('svg text[text-anchor="end"]')]
          .map((т) => т.textContent));
        место.remove();
      }
      return [оси, window.Charts.fmtNum(0.0004)];
    }""")
    мелкие, половинки = оси
    assert мелкие == ["0", "0,0002", "0,0004", "0,0006"], мелкие
    # Точность у всех делений одна: не «0,500», «1», «1,50».
    assert половинки == ["0", "0,5", "1,0", "1,5", "2,0"], половинки
    # Ненулевое значение не выглядит нулём.
    assert одно == "0,00040", одно
    _без_исключений(вкладка)


#: Часы браузера на десять часов впереди сервера.
ЧАСЫ_ВПЕРЕДИ = """
(() => {
  const настоящее = Date.now.bind(Date);
  Date.now = () => настоящее() + 10 * 3600 * 1000;
})();
"""


def test_подписи_графика_очереди_модели_не_зависят_от_часов_браузера(стенд, браузер):
    """Ширину корзины интерфейс считал от своих часов: при расхождении с
    сервером подписи уезжали от корзин, к которым относятся."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ЧАСЫ_ВПЕРЕДИ)
    _открыть(вкладка, "llmqueue", 2500)
    вкладка.wait_for_selector("#lq-c2 svg", timeout=15000)
    ждём, последняя = вкладка.evaluate("""async () => {
      const р = (await (await fetch('/api/llm/queue?hours=24&limit=1')).json()).series;
      const ждём = new Date((р.since + (р.buckets - 1) * р.bucket_seconds) * 1000)
        .toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
      const подписи = [...document.querySelectorAll('#lq-c2 svg text[text-anchor="middle"]')]
        .map((т) => т.textContent);
      return [ждём, подписи[подписи.length - 1]];
    }""")

    def минуты(подпись: str) -> int:
        часы, мин = подпись.split(":")
        return int(часы) * 60 + int(мин)

    # Запрос проверки идёт на пару секунд позже запроса раздела — минута
    # на границе допустима. Часы браузера давали бы расхождение в часы.
    разница = abs(минуты(ждём) - минуты(последняя)) % (24 * 60)
    assert min(разница, 24 * 60 - разница) <= 1, (ждём, последняя)
    _без_исключений(вкладка)


def test_сравнение_с_прошлым_периодом_в_плитке(стенд, браузер):
    """Разметку сравнения передавали объектом — вместо неё «kpi-trend undefined»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    разметка = вкладка.evaluate(
        "() => { const у = window.__asrhub.util;"
        " return у.kpi('Балл оператора', '72', 'из 100', у.delta(72, 60, { good: 1, digits: 0 })); }")
    assert "undefined" not in разметка, разметка
    assert "+12 к прошлому периоду" in разметка and "kpi-trend up" in разметка, разметка
    # Прежний вид — объект — продолжает работать.
    старый = вкладка.evaluate(
        "() => window.__asrhub.util.kpi('Записей', '5', '', { dir: 'down', text: 'было 7' })")
    assert "kpi-trend down" in старый and "было 7" in старый


def test_причина_отказа_проигрывателя_берётся_из_ответа(стенд, браузер):
    """Читали `body.error`, которого нет: «сервер ответил 400» вместо причины."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    вкладка.evaluate("() => window.__asrhub.openJob('ui003')")
    вкладка.wait_for_selector("#job-player", timeout=10000)
    вкладка.wait_for_timeout(2500)
    текст = вкладка.inner_text("#job-player")
    assert "ответил 400" not in текст, текст
    assert "запис" in текст.lower(), текст


def test_показать_ещё_в_журнале_доступа_не_задваивает_строки(стенд, браузер):
    """Двойной щелчок — «показано 150 из 130» и повторы строк."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "settings")
    вкладка.evaluate("() => { window.__asrhub.state.paramGroup = '_access';"
                     " window.__asrhub.renderView(); }")
    вкладка.wait_for_selector("#audit-rows table", timeout=15000)
    вкладка.evaluate("() => { const к = document.querySelector('#audit-more');"
                     " к.click(); к.click(); к.click(); }")
    вкладка.wait_for_timeout(2000)
    действия = вкладка.eval_on_selector_all(
        "#audit-rows tbody tr td:nth-child(3)", "т => т.map(x => x.textContent)")
    assert len(действия) == len(set(действия)), "строки задвоены"
    счётчик = вкладка.inner_text("#audit-count")
    показано, всего = [int(ч) for ч in счётчик.replace("показано", "").split("·")[0]
                       .split("из")]
    assert показано == len(действия) <= всего, счётчик
    _без_исключений(вкладка)


def test_esc_в_поиске_по_разговору_очищает_поиск_а_не_закрывает_карточку(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    вкладка.evaluate("() => window.__asrhub.openJob('ui000', { tab: 'segments' })")
    вкладка.wait_for_selector("#job-find-input", timeout=10000)
    вкладка.fill("#job-find-input", "договор")
    вкладка.wait_for_timeout(600)
    вкладка.press("#job-find-input", "Escape")
    вкладка.wait_for_timeout(300)
    assert вкладка.locator(".modal-backdrop").count() == 1, "Esc закрыл карточку"
    assert вкладка.input_value("#job-find-input") == ""
    # Второй Esc — с пустым полем — закрывает карточку, как везде.
    вкладка.press("#job-find-input", "Escape")
    вкладка.wait_for_timeout(300)
    assert вкладка.locator(".modal-backdrop").count() == 0


def test_выбранный_пресет_не_стирается_перерисовкой(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.select_option("#preset-select", "ru-accuracy")
    вкладка.wait_for_timeout(600)
    assert вкладка.input_value("#preset-select") == "ru-accuracy"
    assert "Сценарий" in вкладка.inner_text("#preset-desc")


#: Отказ сервера на «↑» в очереди модели — подменой ответа внутри страницы.
ОТКАЗ_ПОДНЯТЬ = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/llm/queue/') && адрес.endsWith('/top')) {
        return Promise.resolve(new Response(JSON.stringify(
          { code: 'llm_disabled', message: 'Языковая модель выключена',
            hint: 'Включите её в настройках' }),
          { status: 403, headers: { 'Content-Type': 'application/json' } }));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_отказ_в_очереди_модели_показывается(стенд, браузер):
    """У «↑» и «↻» не было обработки ошибок: тишина и отклонение в консоли."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=ОТКАЗ_ПОДНЯТЬ)
    _открыть(вкладка, "llmqueue", 2500)
    # Кнопки «↑» есть у ждущих записей; ставим запись в очередь сами.
    вкладка.evaluate("async () => { await window.__asrhub.API.post('/api/llm/queue/add',"
                     " { job_ids: ['ui020'], kind: 'повтор' });"
                     " await window.__asrhub.RENDERERS.llmqueue.load(); }")
    вкладка.wait_for_timeout(1500)
    кнопка = вкладка.locator("[data-top]").first
    if кнопка.count() == 0:
        pytest.skip("очередь модели разобрала запись раньше, чем её подняли")
    кнопка.click()
    вкладка.wait_for_timeout(800)
    assert "Языковая модель выключена" in вкладка.inner_text("#toasts")
    _без_исключений(вкладка)


# ---------------------------------------------------------------------------
# Узкий экран и клавиатура
# ---------------------------------------------------------------------------


def test_ни_один_раздел_не_уезжает_вбок_на_телефоне(стенд, браузер):
    """На 390 px «Аналитика записей» раздвигала страницу до 943 px."""
    вкладка = _вкладка(браузер, стенд["адрес"], ширина=390)
    _открыть(вкладка, "dashboard")
    разделы = вкладка.evaluate("() => Object.keys(window.__asrhub.RENDERERS)")
    широкие = []
    for раздел in разделы:
        вкладка.evaluate(f"() => window.__asrhub.go({json.dumps(раздел)})")
        вкладка.wait_for_timeout(1200)
        ширина = вкладка.evaluate("() => document.documentElement.scrollWidth")
        if ширина > 391:
            широкие.append(f"{раздел}: {ширина}")
    вкладка.evaluate("() => window.__asrhub.go('content')")
    вкладка.wait_for_timeout(1500)
    for кнопка in вкладка.eval_on_selector_all("#content-tabs button",
                                               "к => к.map(x => x.dataset.tab)"):
        вкладка.click(f'#content-tabs button[data-tab="{кнопка}"]')
        вкладка.wait_for_timeout(1000)
        ширина = вкладка.evaluate("() => document.documentElement.scrollWidth")
        if ширина > 391:
            широкие.append(f"content/{кнопка}: {ширина}")
    assert not широкие, широкие


def test_сотрудник_открывается_с_клавиатуры(стенд, браузер):
    """Строки «Аналитики по сотрудникам» открывались только мышью."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "employees", 2500)
    вкладка.wait_for_selector("#emp-body [data-emp-open]", timeout=15000)
    кнопка = вкладка.locator("#emp-body [data-emp-open]").first
    ключ = вкладка.evaluate(
        "() => document.querySelector('#emp-body [data-emp-open]').closest('tr').dataset.key")
    кнопка.focus()
    вкладка.keyboard.press("Enter")
    вкладка.wait_for_timeout(1500)
    assert вкладка.evaluate("() => window.__asrhub.state.employeeKey") == ключ
    # И сортировка по столбцу — тоже с клавиатуры.
    порядок = "() => [window.__asrhub.state.employeeSort, window.__asrhub.state.employeeSortDesc]"
    было = вкладка.evaluate(порядок)
    вкладка.locator("#emp-body th[data-sort] button").first.focus()
    вкладка.keyboard.press("Enter")
    вкладка.wait_for_timeout(500)
    assert вкладка.evaluate(порядок) != было, "сортировка с клавиатуры не сработала"


def test_окно_станции_прячет_страницу_от_диктора(стенд, браузер):
    """Форма добавлялась в документ до mountModal и находила там сама себя."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "pbx", 2000)
    вкладка.evaluate("() => window.__asrhub.RENDERERS.pbx.edit(null)")
    вкладка.wait_for_selector(".modal-backdrop", timeout=10000)
    assert вкладка.get_attribute(".app", "aria-hidden") == "true"


def test_переход_по_разделам_закрывает_окно(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    вкладка.evaluate("() => window.__asrhub.openJob('ui001')")
    вкладка.wait_for_selector(".modal-backdrop", timeout=10000)
    вкладка.evaluate("() => window.__asrhub.go('queue')")
    вкладка.wait_for_timeout(500)
    assert вкладка.locator(".modal-backdrop").count() == 0


def test_двойной_щелчок_открыть_даёт_одну_карточку(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results")
    вкладка.evaluate("() => { window.__asrhub.openJob('ui002');"
                     " window.__asrhub.openJob('ui002'); }")
    вкладка.wait_for_timeout(2000)
    assert вкладка.locator(".modal-backdrop").count() == 1


def test_кнопка_отправки_занята_и_после_перерисовки(стенд, браузер):
    """Уйти и вернуться во время загрузки — и новая кнопка отправляла файлы второй раз."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    вкладка.evaluate("() => { const s = window.__asrhub.state;"
                     " s.files = [new File(['x'], 'a.wav')]; s.отправка = true;"
                     " window.__asrhub.go('queue'); window.__asrhub.go('transcribe'); }")
    вкладка.wait_for_timeout(500)
    assert вкладка.is_disabled("#btn-submit")
    assert "Отправка" in вкладка.inner_text("#btn-submit")


# ---------------------------------------------------------------------------
# Списки, листалка, проверка качества
# ---------------------------------------------------------------------------


def test_очередь_и_результаты_берут_облегчённый_список(стенд, браузер):
    """Каждые четыре секунды приезжали полные расшифровки ради имени файла."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue", 2500)
    вкладка.evaluate("() => window.__asrhub.go('results')")
    вкладка.wait_for_timeout(1500)
    вкладка.evaluate("() => window.__asrhub.go('transcribe')")
    вкладка.wait_for_timeout(1500)
    списки = [з for з in вкладка.запросы if "/api/jobs?" in з]
    assert списки, "списков не запрашивали"
    тяжёлые = [з for з in списки if "light=true" not in з]
    assert not тяжёлые, тяжёлые
    # Начало расшифровки под именем файла осталось.
    вкладка.evaluate("() => window.__asrhub.go('results')")
    вкладка.wait_for_selector("#results-table tbody tr", timeout=10000)
    assert "Здравствуйте" in вкладка.inner_text("#results-table") or \
        "Срок поставки" in вкладка.inner_text("#results-table")


def test_результаты_листаются_и_говорят_из_скольких(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "results", 2000)
    assert "показаны 1–100 из 120" in вкладка.inner_text("#results-pager")
    assert вкладка.is_disabled("#r-prev")
    вкладка.click("#r-next")
    вкладка.wait_for_timeout(1500)
    assert "показаны 101–120 из 120" in вкладка.inner_text("#results-pager")
    assert вкладка.is_disabled("#r-next")
    строк = вкладка.locator("#results-table tbody tr").count()
    assert строк == 20, строк


def test_проверка_качества_закрывается_в_карточке_записи(стенд, браузер):
    """Очередь вела в карточку, а оценить там было нечем."""
    состояние = стенд["состояние"]
    ид = состояние.db.qa_assign("ui004", assigned_to="", assigned_by="проверка",
                                due_at=time.time() + 86400, agent="Анна",
                                auto_score=70.0, reason="проверка")
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "analytics", 2500)
    вкладка.wait_for_selector("[data-qa-open]", timeout=15000)
    вкладка.locator('[data-qa-open="ui004"]').first.click()
    вкладка.wait_for_selector("#qa-score", timeout=10000)
    вкладка_проверки = вкладка.locator('#job-tabs button[data-tab="qa"]')
    assert вкладка_проверки.count() == 1 and "ждёт" in вкладка_проверки.inner_text()
    assert "active" in (вкладка_проверки.get_attribute("class") or "")
    вкладка.fill("#qa-score", "150")
    вкладка.click("#qa-submit")
    assert "от 0 до 100" in вкладка.inner_text("#qa-error")
    вкладка.fill("#qa-score", "64")
    вкладка.fill("#qa-comment", "не представился")
    вкладка.click("#qa-submit")
    вкладка.wait_for_timeout(1200)
    assert "согласен" in вкладка.inner_text("#job-tab-body")
    проверка = состояние.db.qa_get(ид)
    assert проверка["status"] == "done" and проверка["score"] == 64.0
    assert проверка["comment"] == "не представился" and проверка["agree"] == 1


# ---------------------------------------------------------------------------
# «Сохранено» значит «записано в файл»
# ---------------------------------------------------------------------------


def test_скрипт_из_раздела_записывается_в_конфигурацию(стенд, браузер):
    """PUT без записи в файл: «Скрипт сохранён» — и перезапуск его стирал."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    вкладка.evaluate("""async () => {
      window.confirm = () => false;
      await window.__asrhub.RENDERERS.content.saveScript(
        [{ id: 'hello', label: 'Поздоровался', any: ['здравствуйте'] }]);
    }""")
    вкладка.wait_for_timeout(500)
    assert "Скрипт сохранён" in вкладка.inner_text("#toasts")
    текст = стенд["конфигурация"].read_text(encoding="utf-8")
    assert "Поздоровался" in текст, текст[:400]
    # Прежнее содержимое файла на месте — записан только скрипт.
    assert "max_concurrent_jobs: 1" in текст


# ---------------------------------------------------------------------------
# Скрытая вкладка, отказы, вспомогательное
# ---------------------------------------------------------------------------


#: Подмена `document.hidden` — чтобы проверить опрос на скрытой вкладке.
СКРЫТАЯ = """
  (() => {
    window.__скрыта = false;
    Object.defineProperty(document, 'hidden', { get: () => window.__скрыта });
  })();
"""


def test_скрытая_вкладка_не_опрашивает_сервер(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=СКРЫТАЯ)
    _открыть(вкладка, "queue", 2000)
    вкладка.evaluate("() => { window.__скрыта = true; }")
    вкладка.wait_for_timeout(500)
    было = len(вкладка.запросы)
    вкладка.wait_for_timeout(9000)
    assert len(вкладка.запросы) == было, вкладка.запросы[было:]
    вкладка.evaluate("() => { window.__скрыта = false;"
                     " document.dispatchEvent(new Event('visibilitychange')); }")
    вкладка.wait_for_timeout(1500)
    assert any("/api/queue" in з for з in вкладка.запросы[было:])


def test_отказ_422_читается_по_русски(стенд, браузер):
    """FastAPI отдаёт список в detail — интерфейс писал «Ошибка 422»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    отказ = вкладка.evaluate("""() => window.__asrhub.util.причинаОтказа({ detail: [{ type: 'string_too_long',
        loc: ['query', 'search'], msg: 'String should have at most 64 characters',
        ctx: { max_length: 64 } }] }, 422)""")
    assert отказ["message"] == "Параметр «search»: не длиннее 64 знаков.", отказ
    отказ = вкладка.evaluate("() => window.__asrhub.util.причинаОтказа({ code: 'x', message: 'Причина',"
                             " hint: 'Совет', detail: {} }, 400)")
    assert (отказ["message"], отказ["hint"]) == ("Причина", "Совет")
    # Поле поиска звонков не даёт набрать больше, чем примет сервер.
    _открыть(вкладка, "telephony")
    assert вкладка.get_attribute("#tel-search", "maxlength") == "64"


def test_аргумент_обработчика_не_ломается_апострофом(стенд, браузер):
    """Сущность &#39; раскодируется до разбора JS — апостроф закрывал строку."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    получено = вкладка.evaluate("""() => {
      window.__получено = null;
      window.__asrhub.проверка = (x) => { window.__получено = x; };
      const значение = "a');window.__взлом=1;//\\\\\\"";
      const узел = document.createElement('div');
      узел.innerHTML = `<button id="jsarg" onclick="__asrhub.проверка(${window.__asrhub.util.jsArg(значение)})">x</button>`;
      document.body.appendChild(узел);
      document.querySelector('#jsarg').click();
      return [window.__получено === значение, window.__взлом === undefined];
    }""")
    assert получено == [True, True], получено


def test_числа_пишутся_с_десятичной_запятой(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "transcribe")
    итог = вкладка.evaluate("() => { const у = window.__asrhub.util;"
                            " return [у.pct(0.125, 1), у.num(0.93, 2), у.fmtBytes(4096),"
                            " у.metricValue(1, ''), у.metricValue(0.5, ''), у.num(12345),"
                            " у.metricValue(0.25, 'с')]; }")
    assert итог == ["12,5 %", "0,93", "4,00 КБ", "1", "0,5", "12,3 тыс", "250 мс"], итог


def test_справка_не_показывает_ключ_целиком(стенд_со_входом, браузер):
    ключ = стенд_со_входом["ключ"]
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=ключ)
    _открыть(вкладка, "help", 2000)
    текст = вкладка.inner_text("#content")
    assert ключ not in текст, "ключ виден целиком"
    assert "$ASRHUB_KEY" in текст and "скопировать с ключом" in текст


# ---------------------------------------------------------------------------
# Вход, сессии и ключи
# ---------------------------------------------------------------------------


def _войти(вкладка, логин: str, пароль: str) -> None:
    ответ = вкладка.request.post(f"{вкладка.адрес}/api/auth/login",
                                 data={"username": логин, "password": пароль})
    assert ответ.ok, ответ.text()


def test_временный_пароль_ведёт_к_форме_и_после_f5(стенд_со_входом, браузер):
    """Отказ `/api/settings` глотался: после F5 — обычный интерфейс с плашками."""
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"])
    вкладка.goto(стенд_со_входом["адрес"])
    _войти(вкладка, "новичок", "Временный-1-пароль")
    _открыть(вкладка, "results", 1500)
    вкладка.reload(wait_until="networkidle")
    вкладка.wait_for_timeout(1500)
    assert вкладка.locator("#pw-new").count() == 1, "формы смены пароля нет"
    объяснение = вкладка.inner_text("#pw-reason")
    assert "выдан администратором" in объяснение, объяснение
    assert "известен всем" not in объяснение
    assert "Смените пароль" not in вкладка.inner_text("#toasts")


def test_кончившаяся_сессия_ведёт_к_форме_входа(стенд_со_входом, браузер):
    """Вместо стопки плашек «Ключ доступа не передан» — форма входа, один раз."""
    состояние = стенд_со_входом["состояние"]
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"])
    вкладка.goto(стенд_со_входом["адрес"])
    # Под формой входа меню разделы не рисует: раньше щелчок по нему давал
    # раздел без каталога и с отказом в каждом запросе. Переход — после
    # того как форма появилась: переход раньше неё проверял бы гонку, а не
    # запрет (форма пришла бы позже и закрыла раздел сама).
    вкладка.wait_for_selector("#login-user", timeout=15000)
    вкладка.evaluate("() => window.__asrhub.go('queue')")
    вкладка.wait_for_timeout(500)
    assert вкладка.locator("#login-user").count() == 1
    _войти(вкладка, "аналитик", "Аналитик-1-пароль")
    вкладка.reload(wait_until="networkidle")
    _открыть(вкладка, "queue", 2000)
    учётка = состояние.accounts.by_username("аналитик")
    состояние.accounts.drop_sessions(учётка.id)
    вкладка.click("#btn-refresh")
    вкладка.wait_for_timeout(2000)
    assert вкладка.locator("#login-user").count() == 1, "формы входа нет"
    assert "Сессия закончилась" in вкладка.inner_text("#login-reason")
    # Переход по меню форму не затирает.
    вкладка.evaluate("() => window.__asrhub.go('results')")
    вкладка.wait_for_timeout(800)
    assert вкладка.locator("#login-user").count() == 1
    assert вкладка.inner_text("#toasts").count("Ключ доступа") <= 1


def test_готовые_настройки_мониторинга_скачиваются_с_ключом(стенд_со_входом, браузер):
    """При закрытых метриках обычная ссылка отвечала 401 вошедшему по ключу."""
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=стенд_со_входом["ключ"])
    _открыть(вкладка, "monitoring", 3000)
    вкладка.wait_for_selector('[data-mon-file*="zabbix"]', timeout=15000)
    with вкладка.expect_download() as ожидание:
        вкладка.click('[data-mon-file*="zabbix"]')
    файл = ожидание.value
    содержимое = Path(файл.path()).read_text(encoding="utf-8")
    assert "zabbix_export" in содержимое, содержимое[:200]


def test_копия_скачивается_ссылкой_с_одноразовым_билетом(стенд_со_входом, браузер):
    """Гигабайтная копия идёт обычной ссылкой — билет вместо ключа в адресе."""
    ключ = стенд_со_входом["ключ"]
    вкладка = _вкладка(браузер, стенд_со_входом["адрес"], ключ=ключ)
    _открыть(вкладка, "backup", 2000)
    копия = вкладка.evaluate("""async () => (await window.__asrhub.API.post(
        '/api/backup?kind=settings', { comment: 'проверка' }))""")
    имя = копия.get("name") or Path(копия.get("path", "")).name
    with вкладка.expect_download() as ожидание:
        вкладка.evaluate(f"() => window.__asrhub.RENDERERS.backup.act('download', {json.dumps(имя)})")
    файл = ожидание.value
    assert Path(файл.path()).stat().st_size > 0
    адрес = файл.url
    assert "ticket=" in адрес and ключ not in адрес, адрес
    # Билет одноразовый: второй раз по той же ссылке — отказ с объяснением.
    повтор = вкладка.request.get(адрес)
    assert повтор.status == 401
    assert "устарела или уже использована" in повтор.text()


# ---------------------------------------------------------------------------
# Сверка полей интерфейса с ответами сервера
# ---------------------------------------------------------------------------


#: Сервер модели «лежит» — подменой ответов о состоянии внутри страницы.
МОДЕЛЬ_ЛЕЖИТ = """
  (() => {
    const родной = window.fetch;
    const причина = 'Сервер модели недоступен по адресу http://127.0.0.1:9';
    window.fetch = async function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      const ответ = await родной(вход, настройки);
      if (!ответ.ok) return ответ;
      if (адрес.includes('/api/llm/status') || адрес.includes('/api/llm/queue?')
          || адрес.endsWith('/api/llm/queue')) {
        const данные = await ответ.clone().json();
        const клиент = адрес.includes('/status') ? данные : (данные.client || {});
        клиент.available = false;
        клиент.reason = причина;
        if (!адрес.includes('/status')) {
          данные.client = клиент;
          данные.current = null;
          данные.counts = Object.assign({}, данные.counts, { 'ждёт': 3 });
        }
        return new Response(JSON.stringify(данные),
          { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return ответ;
    };
  })();
"""


def test_недоступная_модель_не_выдаётся_за_связь(стенд, браузер):
    """Читали `reachable`, которого нет: при лежащей модели — «на связи»."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=МОДЕЛЬ_ЛЕЖИТ)
    _открыть(вкладка, "voice", 2500)
    шапка = вкладка.inner_text("#vo-head")
    assert "сервер не отвечает" in шапка and "на связи" not in шапка, шапка
    _открыть(вкладка, "llmqueue", 2500)
    сейчас = вкладка.inner_text("#lq-current")
    assert "Сервер модели не отвечает" in сейчас, сейчас
    assert "уступает распознаванию" not in сейчас, "причина простоя — не та"
    assert "Сервер модели не отвечает" in вкладка.inner_text("#lq-top")


#: Разбор идёт сто секунд при среднем ответе модели в пять.
РАЗБОР_ЗАТЯНУЛСЯ = """
  (() => {
    const родной = window.fetch;
    window.fetch = async function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      const ответ = await родной(вход, настройки);
      if (!ответ.ok || !адрес.includes('/api/llm/queue?')) return ответ;
      const данные = await ответ.clone().json();
      данные.current = { job_id: 'j1', filename: 'долгий.wav', duration_s: 60,
                         since: Date.now() / 1000 - 100 };
      данные.stats = Object.assign({}, данные.stats, { avg_ms: 5000 });
      return new Response(JSON.stringify(данные),
        { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
  })();
"""


def test_затянувшийся_разбор_желтит_полосу(стенд, браузер):
    """Класс «затянулось» ставился на полосу, а цвет задаёт рамка:
    `.progress.warn > span`. Полоса не желтела никогда."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=РАЗБОР_ЗАТЯНУЛСЯ)
    _открыть(вкладка, "llmqueue", 2500)
    вкладка.wait_for_selector("#lq-progress", timeout=15000)
    цвет, жёлтый, синий = вкладка.evaluate("""() => {
      const образец = (класс) => {
        const рамка = document.createElement('div');
        рамка.className = 'progress ' + класс;
        рамка.innerHTML = '<span></span>';
        document.body.appendChild(рамка);
        const цвет = getComputedStyle(рамка.firstChild).backgroundColor;
        рамка.remove();
        return цвет;
      };
      return [getComputedStyle(document.querySelector('#lq-progress')).backgroundColor,
              образец('warn'), образец('')];
    }""")
    assert жёлтый != синий
    assert цвет == жёлтый, (цвет, жёлтый, синий)
    _без_исключений(вкладка)


def test_ячейки_с_кнопками_не_выпадают_из_строки(стенд, браузер):
    """`<td class="row">` делал ячейку гибким блоком: она выпадала из
    строки таблицы, и черта под кнопками шла вразнобой с соседними."""
    import urllib.request

    запрос = urllib.request.Request(f"{стенд['адрес']}/api/backup?kind=settings",
                                    data=json.dumps({"comment": "проверка"}).encode(),
                                    headers={"Content-Type": "application/json"},
                                    method="POST")
    with urllib.request.urlopen(запрос, timeout=60) as ответ:
        assert ответ.status == 200
    вкладка = _вкладка(браузер, стенд["адрес"])
    выпавшие = {}
    for раздел, таблица in (("llmqueue", "#lq-table"), ("backup", "#bk-list"),
                            ("system", "#users-body")):
        _открыть(вкладка, раздел, 2500)
        вкладка.wait_for_selector(f"{таблица} table tbody tr", timeout=15000)
        выпавшие[раздел] = вкладка.evaluate("""(таблица) =>
          [...document.querySelectorAll(таблица + ' td, ' + таблица + ' th')]
            .filter((я) => getComputedStyle(я).display !== 'table-cell')
            .map((я) => я.outerHTML.slice(0, 80))""", таблица)
    assert not any(выпавшие.values()), выпавшие
    _без_исключений(вкладка)


def test_откат_повтора_не_выдаётся_за_успех(стенд, браузер):
    """На откат всплывало зелёное «Задание готово (RTF —)»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "queue")
    вкладка.evaluate("() => window.__asrhub.util.handleEvent({ type: 'job.completed',"
                     " id: 'ui001', reverted: true, reason: 'Повторное распознавание не удалось' })")
    вкладка.wait_for_timeout(300)
    сообщения = вкладка.inner_text("#toasts")
    assert "Повтор не удался — оставлен прежний результат" in сообщения, сообщения
    assert "Задание готово" not in сообщения


def test_справочник_сотрудников_отбирает_работающих(стенд, браузер):
    """Подпись «работают» стояла, а запрос уходил без отбора."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "staff", 2000)
    запросы = [з for з in вкладка.запросы if "/api/employees?" in з]
    assert запросы and "active=true" in запросы[-1], запросы[-3:]
    вкладка.select_option("#st-active", "false")
    вкладка.wait_for_timeout(800)
    вкладка.evaluate("() => window.__asrhub.go('queue')")
    вкладка.wait_for_timeout(500)
    вкладка.evaluate("() => window.__asrhub.go('staff')")
    вкладка.wait_for_timeout(1500)
    assert вкладка.input_value("#st-active") == "false", "список показывает не тот отбор"
    assert "active=false" in [з for з in вкладка.запросы if "/api/employees?" in з][-1]


def test_новый_ключ_с_занятым_именем_предупреждает(стенд, браузер):
    """Ответ сервера о совпадении имён терялся — было только окно с ключом."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    вкладка.on("dialog", lambda окно: окно.accept())
    _открыть(вкладка, "settings")
    вкладка.evaluate("() => { window.__asrhub.state.paramGroup = '_access';"
                     " window.__asrhub.renderView(); }")
    вкладка.wait_for_selector("#access-new-name", timeout=10000)
    for _ in range(2):
        вкладка.fill("#access-new-name", "проба-имени")
        вкладка.click("#access-new")
        вкладка.wait_for_timeout(800)
    assert "Общая видимость заданий" in вкладка.inner_text("#toasts")


def test_рейтинг_называет_порог_при_переходе_в_результаты(стенд, браузер):
    """«Все такие записи» с тридцати строк вели на пустой список."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    вкладка.click('#content-tabs button[data-tab="records"]')
    вкладка.wait_for_selector("#content-kind", timeout=15000)
    кнопка = вкладка.locator('#content-kind button[data-kind="monologue"]')
    if кнопка.count() == 0:
        pytest.skip("отбора «монологи» на стенде нет")
    кнопка.click()
    вкладка.wait_for_selector("#content-all", timeout=10000)
    assert вкладка.inner_text("#content-all") == "Все с монологом от 2,5 минуты"


def test_смена_отбора_сразу_убирает_прежний_список(стенд, браузер):
    """Пока грузился новый отбор, под подсвеченной кнопкой стояли таблица и
    «Все …» прежнего — щелчок по ней вёл в «Результаты» с чужим отбором."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    вкладка.click('#content-tabs button[data-tab="records"]')
    вкладка.wait_for_selector("#content-kind", timeout=15000)
    вкладка.wait_for_function("""() => { const м = document.querySelector('#content-records');
      return м && !м.textContent.includes('Загрузка'); }""", timeout=15000)
    сразу = вкладка.evaluate("""() => {
      const другая = [...document.querySelectorAll('#content-kind button')]
        .find((к) => !к.classList.contains('active'));
      другая.click();
      const место = document.querySelector('#content-records');
      return [!!document.querySelector('#content-all'),
              место.querySelectorAll('tbody tr').length, место.textContent.trim()];
    }""")
    assert сразу[0] is False and сразу[1] == 0, сразу
    assert "Загрузка" in сразу[2], сразу
    _без_исключений(вкладка)


def test_подсказка_карты_по_часам_называет_час(стенд, браузер):
    """Подписи столбцов были пустыми у двух третей часов — «Ср, : 0,230»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "trends", 3000)
    вкладка.wait_for_selector("#tr-heat svg rect", timeout=15000)
    подсказки = вкладка.evaluate("""() => {
      const итог = [];
      const клетки = Array.from(document.querySelectorAll('#tr-heat svg rect'));
      for (const клетка of клетки.slice(0, 60)) {
        const р = клетка.getBoundingClientRect();
        клетка.dispatchEvent(new MouseEvent('mousemove', { bubbles: true,
          clientX: р.x + 2, clientY: р.y + 2 }));
        const tip = document.querySelector('#chart-tip');
        if (tip && tip.textContent) итог.push(tip.textContent);
      }
      return итог;
    }""")
    assert подсказки, "подсказок нет"
    assert not [п for п in подсказки if ", :" in п], подсказки[:5]


#: Сбой журнала и событий — ответ 500 внутри страницы.
СБОЙ_ЖУРНАЛА = """
  (() => {
    const родной = window.fetch;
    window.fetch = function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      if (адрес.includes('/api/logs?') || адрес.includes('/api/events?')) {
        return Promise.resolve(new Response(JSON.stringify(
          { code: 'internal_error', message: 'Внутренняя ошибка сервера: KeyError' }),
          { status: 500, headers: { 'Content-Type': 'application/json' } }));
      }
      return родной(вход, настройки);
    };
  })();
"""


def test_сбой_журнала_не_выдаётся_за_нехватку_прав(стенд, браузер):
    """Любой сбой писал администратору «доступен только администратору»."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=СБОЙ_ЖУРНАЛА)
    _открыть(вкладка, "logs", 2500)
    журнал = вкладка.inner_text("#log-table")
    assert "Журнал не получен" in журнал and "KeyError" in журнал, журнал
    assert "только ключу" not in журнал
    assert "События не получены" in вкладка.inner_text("#event-table")


def test_сбор_без_включённых_станций_не_выдаётся_за_запуск(стенд, браузер):
    вкладка = _вкладка(браузер, стенд["адрес"])
    вкладка.on("dialog", lambda окно: окно.accept())
    _открыть(вкладка, "pbx", 2000)
    вкладка.evaluate("() => window.__asrhub.RENDERERS.pbx.collect('all', null)")
    вкладка.wait_for_timeout(1500)
    сообщения = вкладка.inner_text("#toasts")
    assert "Сбор не запущен" in сообщения and "Сбор запущен" not in сообщения, сообщения


#: Тревога критическая, состояние сервера — «внимание».
КРИТИЧЕСКАЯ_ТРЕВОГА = """
  (() => {
    const родной = window.fetch;
    window.fetch = async function (вход, настройки) {
      const адрес = typeof вход === 'string' ? вход : (вход && вход.url) || '';
      const ответ = await родной(вход, настройки);
      if (адрес.includes('/api/monitoring/alerts')) {
        const данные = await ответ.clone().json();
        данные.summary = Object.assign({}, данные.summary, { worst: 'critical', firing: 1 });
        return new Response(JSON.stringify(данные),
          { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      if (адрес.includes('/api/monitoring/health')) {
        const данные = await ответ.clone().json();
        данные.status = 'warning';
        return new Response(JSON.stringify(данные),
          { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      return ответ;
    };
  })();
"""


def test_плашка_состояния_мониторинга_говорит_то_же_что_цвет(стенд, браузер):
    """Красная плашка со словом «внимание» при горящей критической тревоге."""
    вкладка = _вкладка(браузер, стенд["адрес"], скрипт=КРИТИЧЕСКАЯ_ТРЕВОГА)
    _открыть(вкладка, "monitoring", 3000)
    плашка = вкладка.locator(".kpi .chip").first
    assert плашка.inner_text() == "критическая тревога", плашка.inner_text()
    assert "err" in (плашка.get_attribute("class") or "")


def test_таблица_тревог_показывает_включительный_знак_и_метки(стенд, браузер):
    состояние = стенд["состояние"]
    вкладка = _вкладка(браузер, стенд["адрес"])
    ответ = вкладка.request.put(f"{стенд['адрес']}/api/monitoring/alerts/rules", data=[
        {"metric": "asrhub_collector_source_up", "direction": "below", "threshold": 1,
         "severity": "critical", "inclusive": True, "labels": {"source": "queue"}}])
    assert ответ.ok, ответ.text()
    try:
        _открыть(вкладка, "monitoring", 3000)
        таблица = вкладка.inner_text("#content")
        assert "≤ 1" in таблица, таблица[:600]
        assert "source=queue" in таблица
    finally:
        состояние.monitoring.save_rules(None)


def test_справочник_метрик_не_пишет_критично_без_уровня(стенд, браузер):
    """У «модель доступна» только предупреждение — выходило «— критично»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "monitoring", 3000)
    вкладка.wait_for_selector("#mon-catalog details", timeout=15000)
    текст = вкладка.evaluate("""() => {
      const карточка = Array.from(document.querySelectorAll('#mon-catalog details'))
        .find((д) => д.textContent.includes('asrhub_llm_available'));
      return карточка ? карточка.textContent : '';
    }""")
    assert текст, "карточки asrhub_llm_available нет"
    assert "— — критично" not in текст and "критично" not in текст, текст[:400]
    assert "не больше" in текст or "ниже" in текст


def test_коучинг_показывает_разобранные_по_переключателю(стенд, браузер):
    """Без переключателя кнопка «Вернуть» не появлялась никогда."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content")
    вкладка.click('#content-tabs button[data-tab="agents"]')
    вкладка.wait_for_selector("#coaching-done", timeout=15000)
    вкладка.check("#coaching-done")
    вкладка.wait_for_timeout(1500)
    assert any("/api/content/coaching" in з and "done=true" in з for з in вкладка.запросы)
    assert вкладка.is_checked("#coaching-done")


def test_сравнение_с_пустым_прошлым_периодом_не_показывается(стенд, браузер):
    """Прошлый период без записей давал «10 | 0 | +10» вместо «не с чем»."""
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "content", 2000)
    # Неделя: все разобранные записи стенда — за последние сутки, а прошлая
    # неделя пуста.
    вкладка.click('#content-period button[data-period="week"]')
    вкладка.wait_for_timeout(2500)
    изменения = вкладка.evaluate("""() => {
      const карточка = Array.from(document.querySelectorAll('#content-body section.card'))
        .find((к) => (к.querySelector('h3') || {}).textContent === 'Речь и разговор');
      if (!карточка) return null;
      return Array.from(карточка.querySelectorAll('tbody tr'))
        .map((с) => [с.children[2].textContent.trim(), с.children[3].textContent.trim()]);
    }""")
    if изменения is None:
        pytest.skip("карточки «Речь и разговор» нет")
    прошлый = стенд["состояние"].db.query_one(
        "SELECT COUNT(*) AS n FROM jobs WHERE created_at < ? AND created_at >= ?",
        (time.time() - 7 * 86400, time.time() - 14 * 86400))["n"]
    if прошлый:
        pytest.skip("у прошлой недели на стенде есть записи")
    assert изменения and all(было == "—" and стало == "—" for было, стало in изменения), \
        изменения[:5]


def test_импорт_справочника_называет_пропущенные_строки(стенд, браузер, tmp_path):
    файл = tmp_path / "штат.csv"
    файл.write_text("Фамилия;Имя;Отдел;Внутренний номер;Табельный\n"
                    "Зайцев;Пётр;Продажи;106;77\n;;Склад;107;78\n", encoding="utf-8")
    вкладка = _вкладка(браузер, стенд["адрес"])
    _открыть(вкладка, "staff", 2000)
    вкладка.set_input_files("#st-file", str(файл))
    вкладка.wait_for_timeout(2500)
    assert "пропущено 1" in вкладка.inner_text("#toasts")
    assert "нет фамилии и имени" in вкладка.inner_text(".modal-backdrop .modal-body")
