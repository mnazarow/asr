"""Заход 70 в браузере: «Настройки» и «АТС» — шаблоном html`…`.

Станции в настройках телефонии, проверка запроса в CRM, разбор забора
(диагностика), карточки и строки станций, разговоры с переводом и события
очередей собирались строками с esc() у каждой подстановки. Теперь их
собирает шаблон, который экранирует сам. Здесь в каждое место подаётся имя
с разметкой: на странице оно — текст, и ничего не исполняется.
"""
from __future__ import annotations

import json

import pytest
from drill_seed import Архив, браузер, поднять

КЛЮЧ = "ah_templates_70_0123456789abcdef"
ЗЛОЕ = '<img src=x onerror="window.__взлом=1">'


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("tpl70") / "data", auth=True, заданий=2,
                      ключи={КЛЮЧ: {"name": "шаблоны-70", "role": "admin"}})
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


def _ответ(маршрут, тело: dict) -> None:
    маршрут.fulfill(status=200, content_type="application/json",
                    body=json.dumps(тело, ensure_ascii=False))


def test_настройки_и_атс_показывают_разметку_текстом(стенд):
    from playwright.sync_api import sync_playwright

    адрес = стенд
    станция = {"id": "s1", "name": ЗЛОЕ, "source": "ami", "enabled": True,
               "host": ЗЛОЕ, "last_error": ЗЛОЕ, "running": False,
               "calls": {"total": 3, "skipped": 1, "deferred": 1}}
    with sync_playwright() as игра:
        движок = игра.chromium.launch(executable_path=браузер())
        try:
            страница = движок.new_page(viewport={"width": 1400, "height": 900})
            страница.add_init_script(
                f"try {{ localStorage.setItem('asrhub_key', '{КЛЮЧ}'); }} catch (e) {{}}")
            ошибки: list[str] = []
            страница.on("pageerror", lambda e: ошибки.append(str(e)))
            страница.route("**/api/telephony/stations*",
                           lambda м: _ответ(м, {"items": [станция]}))
            страница.route("**/api/crm/test*", lambda м: _ответ(м, {
                "kind": ЗЛОЕ, "url": ЗЛОЕ, "entity_id": ЗЛОЕ, "sent": True,
                "requests": [{"method": "POST", "url": ЗЛОЕ, "body": ЗЛОЕ, "purpose": ЗЛОЕ}],
                "response": {"status": 200, "fields_error": ЗЛОЕ}}))
            страница.route("**/api/telephony/conversations*", lambda м: _ответ(м, {
                "calls": 4, "conversations": 2, "transferred": 1,
                "items": [{"started_at": 1_700_000_000, "agents": [ЗЛОЕ, "102"], "legs": 2,
                           "transfers": 1, "talk_s": 75, "job_id": ЗЛОЕ}]}))
            страница.route("**/api/telephony/queues*", lambda м: _ответ(м, {
                "events": 10, "threshold_s": 20,
                "queues": [{"queue": ЗЛОЕ, "entered": 5, "answered": 4, "lost": 1,
                            "lost_share": 20.0, "abandoned": 1, "avg_wait_s": 12,
                            "avg_lost_wait_s": 30, "service_level": 80.0,
                            "ringnoanswer": 2}],
                "agents": [{"agent": ЗЛОЕ, "ringnoanswer": 2}]}))

            # «Настройки» → «Телефония»: врезка со станциями.
            страница.goto(f"{адрес}/#settings", wait_until="networkidle")
            страница.click('#group-nav button[data-group="telephony"]', no_wait_after=True)
            страница.wait_for_selector("#set-stations table")
            assert ЗЛОЕ in страница.locator("#set-stations").inner_text()
            assert страница.locator("#set-stations [data-station='s1']").count() == 1

            # «Настройки» → «CRM»: проверка запроса.
            страница.click('#group-nav button[data-group="crm"]', no_wait_after=True)
            страница.click("#crm-dry", no_wait_after=True)
            страница.wait_for_function(
                "() => document.querySelector('#crm-out').textContent.includes('CRM ответила')")
            вывод = страница.locator("#crm-out").inner_text()
            assert вывод.count(ЗЛОЕ) >= 5, вывод

            # «АТС»: разбор забора, карточка и строки станции, переводы, очереди.
            итог = страница.evaluate("""([злое, станция]) => {
                const П = window.__asrhub.RENDERERS.pbx;
                const место = document.createElement('div');
                место.id = 'pbx-diag';
                document.body.append(место);
                П.drawDiagResult({stations: [{name: злое, station: 's1', source: 'ami',
                    state: 'fail', failed: 1, verdict: злое,
                    checks: [{state: 'fail', title: злое, value: злое, hint: злое}]}]});
                const д = {stations: [станция], enabled: true,
                    by_station: [{station: 's1', total: 3, answered: 2, queued: 1,
                                  talk_s: 90, skipped: 1}]};
                const карточка = document.createElement('div');
                карточка.innerHTML = String(П.stationCard(станция, д));
                const таблица = document.createElement('table');
                таблица.innerHTML = '<tbody>' + П.stationRows(д) + '</tbody>';
                document.body.append(карточка, таблица);
                return {разбор: место.textContent, карточка: карточка.textContent,
                        строки: таблица.textContent,
                        кнопок: карточка.querySelectorAll('[data-act]').length};
            }""", [ЗЛОЕ, станция])
            assert итог["разбор"].count(ЗЛОЕ) >= 4, итог["разбор"]
            assert итог["карточка"].count(ЗЛОЕ) >= 2, итог["карточка"]
            assert ЗЛОЕ in итог["строки"], итог["строки"]
            assert итог["кнопок"] == 6, "у администратора на карточке шесть действий"

            страница.evaluate("""() => {
                for (const ид of ['pbx-conversations', 'pbx-queue-log']) {
                    const место = document.createElement('div');
                    место.id = ид;
                    document.body.append(место);
                }
                const П = window.__asrhub.RENDERERS.pbx;
                П.drawConversations();
                П.drawQueueLog();
            }""")
            страница.wait_for_function(
                "() => document.querySelector('#pbx-conversations table')"
                " && document.querySelector('#pbx-queue-log table')")
            переводы = страница.locator("#pbx-conversations").inner_text()
            очереди = страница.locator("#pbx-queue-log").inner_text()
            assert f"{ЗЛОЕ} → 102" in переводы, переводы
            assert очереди.count(ЗЛОЕ) == 2, очереди
            assert страница.locator(
                "#pbx-conversations [data-open-job]").get_attribute("data-open-job") == ЗЛОЕ

            assert страница.evaluate("() => window.__взлом") is None
            assert страница.locator("img[src='x']").count() == 0
            assert not ошибки, ошибки
        finally:
            движок.close()
