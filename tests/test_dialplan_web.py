"""Заход 54 в настоящем браузере: раздел «Asterisk: диалплан».

Серверная часть приёма проверена в `test_dialplan.py`. Здесь — что
человек, настраивающий станцию, может пройти раздел от начала до конца:
включить забор, завести станцию приёма прямо отсюда, получить файлы для
своего драйвера (chan_pjsip или chan_sip) и способа передачи, отправить
пробный звонок и увидеть его в журнале обращений.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("dialplan-ui") / "data", заданий=4)
    хранилище.app.state.hub.settings.set("telephony_enabled", False)
    адрес, остановить = поднять(хранилище.app)
    yield адрес, хранилище
    остановить()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер[0])
    try:
        yield окно
    finally:
        окно.закрыть()


def _текст(с, файл: str) -> str:
    return с.inner_text(f'pre[data-dp-file="{файл}"]')


def test_от_включения_до_пробного_звонка(вкладка, сервер):
    настройки = сервер[1].app.state.hub.settings
    с = вкладка.открыть("dialplan")
    с.wait_for_selector('pre[data-dp-file="dialplan"]')
    assert "Как это работает" in с.inner_text("#content")
    assert "Вызов из диалплана" in с.inner_text("#content")
    состояние = с.inner_text("#dp-state")
    assert "Забор записей с АТС выключен" in состояние and "Нет станции для приёма" in состояние
    # Включить забор — одной кнопкой, с записью в конфигурацию.
    с.click("#dp-enable")
    с.wait_for_function("() => !document.querySelector('#dp-enable')")
    assert настройки.get("telephony_enabled") is True
    # Завести станцию: форма открывается с заготовкой «вызов из диалплана»,
    # и поля чужих источников в ней скрыты.
    с.click("#dp-add2")
    с.wait_for_selector(".modal [data-field=source]")
    assert с.eval_on_selector(".modal [data-field=source]", "e => e.value") == "dialplan"
    видно = с.evaluate("""() => Object.fromEntries([...document.querySelectorAll(
        '.modal .param')].map(п => [п.querySelector('[data-field]').dataset.field,
        п.style.display !== 'none']))""")
    assert видно["recordings_dir"] and видно["min_duration_s"] and видно["owner"]
    assert not видно["cdr_file"] and not видно["host"] and not видно["lookback_days"]
    с.fill(".modal [data-field=name]", "Головной офис")
    с.click("#pbx-save")
    с.wait_for_selector("[data-dp-edit]")
    станция = next(с for с in настройки.get("telephony_stations")
                   if с["name"] == "Головной офис")
    assert станция["source"] == "dialplan"
    ид = станция["id"]
    # Станция попала в форму параметров и в адрес приёма собранных файлов.
    с.wait_for_function(f"""() => (document.querySelector('pre[data-dp-file="check"]') || {{}})
        .innerText?.includes('station={ид}')""")
    # Проба: тот же запрос, что пришлёт станция, — с ответом и строкой журнала.
    с.click("#dp-try")
    с.wait_for_selector("#dp-try-out .banner")
    assert "был бы поставлен" in с.inner_text("#dp-try-out")
    assert ид in с.inner_text("#dp-try-out")
    с.wait_for_selector("#dp-log-table tbody tr")
    assert "проба" in с.inner_text("#dp-log-table tbody tr")
    assert сервер[1].db.query_one("SELECT COUNT(*) AS n FROM calls WHERE station=?",
                                  (ид,))["n"] == 0
    вкладка.чисто()


def test_драйвер_и_способ_меняют_файлы(вкладка):
    с = вкладка.открыть("dialplan")
    с.wait_for_selector('pre[data-dp-file="dialplan"]')
    assert "${CHANNEL(endpoint)}" in _текст(с, "dialplan")
    assert "Dial(PJSIP/${EXTEN}" in _текст(с, "contexts")
    с.click('#dp-driver button[data-driver="sip"]')
    с.wait_for_function("""() => document.querySelector('pre[data-dp-file="dialplan"]')
        .innerText.includes('CHANNEL(peername)')""")
    assert "Dial(SIP/provider/${EXTEN}" in _текст(с, "contexts")
    assert "dp-ours" in с.get_attribute("#dp-diff-table th:nth-child(2)", "class")
    assert "удалён в 21" in с.inner_text("#dp-tips")
    # Путём на общем диске: CURL() вместо сценария, сценарий и его настройки не нужны.
    с.click('#dp-mode button[data-mode="path"]')
    с.wait_for_function("""() => document.querySelector('pre[data-dp-file="dialplan"]')
        .innerText.includes('${CURL(')""")
    assert с.locator('pre[data-dp-file="install"]').count() == 0
    assert с.locator("#dp-script").count() == 0
    с.select_option('[data-dp="key_in"]', "query")
    с.wait_for_function("""() => document.querySelector('pre[data-dp-file="dialplan"]')
        .innerText.includes('api_key=')""")
    # Адрес сервера правится в форме — файлы собираются заново.
    с.fill('[data-dp="url"]', "https://asr.example.ru")
    с.wait_for_function("""() => document.querySelector('pre[data-dp-file="dialplan"]')
        .innerText.includes('ASRHUB_URL=https://asr.example.ru/api/telephony/dialplan/call')""")
    # Негодный адрес — понятный отказ, а не файл с мусором.
    с.fill('[data-dp="url"]', "ftp://x")
    с.wait_for_selector("#dp-files .banner.err")
    assert "адрес сервера" in с.inner_text("#dp-files")
    вкладка.чисто()
