"""Заход 54: то, что прежние заходы оставили «как есть».

Заход 49 оставил кнопки «Записи» у категории и «Все такие записи» в «Что
послушать», ведущие в «Результаты» без периода: в отчёте за неделю строка
говорила «12 записей», а «Результаты» показывали записи категории за всё
время. Теперь список заданий принимает окно `since`/`until` — то же
полуоткрытое, что у списков раскрытия, — и обе кнопки его передают.
"""
from __future__ import annotations

import time

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    а = Архив(tmp_path_factory.mktemp("leftovers") / "data", заданий=40)
    yield а
    а.закрыть()


@pytest.fixture(scope="module")
def клиент(архив):
    from fastapi.testclient import TestClient

    with TestClient(архив.app) as c:
        yield c


def test_список_заданий_режется_окном_отчёта(клиент, архив):
    все = клиент.get("/api/jobs", params={"status": "completed", "limit": 500,
                                          "light": "true"}).json()
    времена = sorted(з["created_at"] for з in все["items"])
    assert len(времена) > 10
    since, until = времена[3], времена[-3]
    окно = клиент.get("/api/jobs", params={"status": "completed", "limit": 500,
                                           "light": "true", "since": since,
                                           "until": until}).json()
    ожидается = [т for т in времена if since <= т < until]
    assert окно["total"] == len(ожидается) == len(окно["items"])
    assert all(since <= з["created_at"] < until for з in окно["items"])
    # Сходится со списком раскрытия за то же окно.
    раскрытие = клиент.get("/api/drill", params={"base": "jobs", "since": since,
                                                 "until": until, "limit": 1,
                                                 "f": "status:completed"}).json()
    assert раскрытие["total"] == окно["total"]
    # since_hours и since вместе — пересечение, а не что-то одно.
    сутки = клиент.get("/api/jobs", params={"status": "completed", "since": since,
                                            "since_hours": 1, "limit": 1}).json()
    assert сутки["total"] == sum(1 for т in времена
                                 if т >= max(since, time.time() - 3600))


def test_результаты_показывают_окно_и_снимают_его(архив):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден")
    адрес, остановить = поднять(архив.app)
    вкладка = Вкладка(адрес)
    try:
        с = вкладка.открыть("dashboard")
        сейчас = time.time()
        с.evaluate("""(о) => { const a = window.__asrhub;
            a.state.resultsWindow = {since: о.since, until: о.until, label: 'с 1 по 2'};
            a.go('results'); }""", {"since": сейчас - 86400 * 3, "until": сейчас})
        с.wait_for_selector("#results-window .banner")
        assert "с 1 по 2" in с.inner_text("#results-window")
        запросы = [з for з in вкладка.запросы if "/api/jobs?" in з]
        assert any("since=" in з and "until=" in з for з in запросы)
        с.click("#r-window-off")
        с.wait_for_function("() => !document.querySelector('#results-window .banner')")
        # Окно — на один заход: из меню «Результаты» снова за всё время.
        с = вкладка.открыть("dashboard")
        с = вкладка.открыть("results")
        assert с.locator("#results-window .banner").count() == 0
        вкладка.чисто()
    finally:
        вкладка.закрыть()
        остановить()
