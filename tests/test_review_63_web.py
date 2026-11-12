"""Заход 63 в браузере: мягкое обновление строк «Очереди».

Таблица заданий обновляется каждые четыре секунды. Раньше она собиралась
заново целиком: фокус клавиатуры возвращался «примерно туда же», выделение
текста пропадало, строки мигали. Теперь неизменившаяся строка остаётся
тем же элементом, изменившаяся заменяется на месте, а фокус в ней
возвращается на ту же кнопку. Строки собирает шаблон html`…`, который
экранирует подстановки сам: имя файла с разметкой остаётся текстом.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, браузер, поднять

КЛЮЧ = "ah_soft_rows_63_0123456789abcdef"


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("soft") / "data", auth=True, заданий=12,
                      ключи={КЛЮЧ: {"name": "мягкие-строки", "role": "admin"}})
    адрес, остановить = поднять(хранилище.app)
    yield адрес, хранилище
    остановить()
    хранилище.закрыть()


def test_queue_rows_update_in_place_and_keep_focus(стенд):
    from playwright.sync_api import sync_playwright

    адрес, хранилище = стенд
    db = хранилище.db
    задания = [з["id"] for з in db.list_jobs(limit=50)]
    assert len(задания) >= 6
    # Имя с разметкой: шаблон обязан показать его текстом.
    db.update_job(задания[0], filename='<img src=x onerror="window.__взлом=1">.wav')

    with sync_playwright() as игра:
        движок = игра.chromium.launch(executable_path=браузер())
        try:
            страница = движок.new_page(viewport={"width": 1500, "height": 900},
                                       bypass_csp=False)
            страница.add_init_script(
                f"try {{ localStorage.setItem('asrhub_key', '{КЛЮЧ}'); }} catch (e) {{}}")
            ошибки: list[str] = []
            страница.on("pageerror", lambda e: ошибки.append(str(e)))
            страница.goto(f"{адрес}/#queue", wait_until="networkidle")
            страница.select_option("#q-filter", "")
            страница.wait_for_function(
                "() => document.querySelectorAll('#queue-table tbody tr').length >= 6")
            assert страница.evaluate("() => window.__взлом") is None
            assert страница.locator("#queue-table").inner_text().count("<img src=x") == 1

            # Пометить строки и поставить фокус на кнопку третьей.
            страница.evaluate("""() => {
                const строки = [...document.querySelectorAll('#queue-table tbody tr')];
                строки.forEach((с, н) => { с.__метка = н; });
                строки[2].querySelector('[data-action="open"]').focus();
                window.__ключи = строки.map((с) => с.dataset.key);
            }""")
            третья = страница.evaluate("() => window.__ключи[2]")
            вторая = страница.evaluate("() => window.__ключи[1]")
            # Сервер изменил одну строку (вторую), остальные — те же.
            db.update_job(вторая, priority=77)
            # Тот же отбор ещё раз — то же, что такт опроса: таблица заново с сервера.
            страница.dispatch_event("#q-filter", "change")
            страница.wait_for_function(
                "(к) => [...document.querySelectorAll('#queue-table tbody tr')]"
                ".some((с) => с.dataset.key === к && с.__метка === undefined)", arg=вторая)
            итог = страница.evaluate("""() => {
                const строки = [...document.querySelectorAll('#queue-table tbody tr')];
                const ф = document.activeElement;
                return {
                    помеченных: строки.filter((с) => с.__метка !== undefined).length,
                    всего: строки.length,
                    фокусВ: ф && ф.closest('tr') ? ф.closest('tr').dataset.key : '',
                    действие: ф && ф.dataset ? ф.dataset.action : '',
                    той_же: ф && ф.closest('tr') ? ф.closest('tr').__метка : null,
                };
            }""")
            assert итог["всего"] == len(страница.evaluate("() => window.__ключи"))
            assert итог["помеченных"] == итог["всего"] - 1, итог
            assert итог["фокусВ"] == третья and итог["действие"] == "open", итог
            assert итог["той_же"] == 2, "строка с фокусом не должна пересоздаваться"

            # Строка, на которой фокус, изменилась — фокус на той же кнопке новой строки.
            db.update_job(третья, priority=78)
            # Тот же отбор ещё раз — то же, что такт опроса: таблица заново с сервера.
            страница.dispatch_event("#q-filter", "change")
            страница.wait_for_function(
                "(к) => { const ф = document.activeElement;"
                " return ф && ф.closest('tr') && ф.closest('tr').dataset.key === к"
                " && ф.closest('tr').__метка === undefined; }", arg=третья)
            assert страница.evaluate("() => document.activeElement.dataset.action") == "open"

            # Другой отбор — таблица собирается заново (без мягкого пути).
            страница.select_option("#q-filter", "completed")
            страница.wait_for_timeout(600)
            assert not ошибки, ошибки
        finally:
            движок.close()
