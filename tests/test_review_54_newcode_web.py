"""Заход 54 в настоящем браузере: число и список в голосовой аналитике.

Серверная сторона тех же исправлений — в `test_review_54_newcode.py`.
Здесь — что интерфейс спрашивает список и вторую часть свода тем же окном,
каким посчитано число:

* «Разговоров» у добавочного в «Голосовой аналитике» — все готовые записи,
  и разобранные моделью, и ещё нет; список под числом раскрывался по
  ответам модели, и десять разговоров давали шесть строк;
* вторая часть свода «Голосовой аналитики» и отчёта за период, а также
  сводка проблем рядом с отчётом спрашиваются на момент первой части;
* день календаря раскрывается границами дня сервера, а не полуночью
  браузера: у сервера в UTC и человека в Токио это разные сутки.
"""
from __future__ import annotations

import datetime as dt
from urllib.parse import parse_qs, urlparse

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("review-54-web") / "data")
    db = хранилище.db
    # Запись в прошлом квартале в 23:30 по часам сервера: в поясе, где
    # сутки начинаются раньше, она уже «завтра».
    from asrhub import voice_period as В

    квартал = В.сдвинуть("quarter", В.начало("quarter", dt.date.today()), -1)
    день = квартал + dt.timedelta(days=20)
    поздно = dt.datetime(день.year, день.month, день.day, 23, 30).timestamp()
    db.create_job({"id": "cal-late", "filename": "поздний.wav", "source": "phone",
                   "media_duration_s": 60})
    db.execute("UPDATE jobs SET created_at=? WHERE id='cal-late'", (поздно,))
    db.update_job("cal-late", status="completed", finished_at=поздно + 5)
    адрес, остановить = поднять(хранилище.app)
    yield адрес, хранилище, день
    остановить()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер[0])
    try:
        yield окно
    finally:
        окно.закрыть()


def _параметры(адрес: str) -> dict[str, str]:
    return {к: з[0] for к, з in parse_qs(urlparse(адрес).query).items()}


def test_разговоры_добавочного_раскрываются_всеми_записями(вкладка):
    с = вкладка.открыть("voice")
    с.wait_for_selector("#vo-agents table")
    люди = с.evaluate("() => window.__asrhub.state.voice.agents")
    # Добавочный, у которого разобрана моделью только часть разговоров.
    номер, ч = next((н, ч) for н, ч in enumerate(люди) if ч["records"] > ч["analyzed"])
    кнопка = с.locator("#vo-agents tbody tr").nth(номер).locator("button.drill")
    кнопка.click()
    с.wait_for_selector("#vo-agents .drill-panel tbody tr")
    assert с.locator("#vo-agents .drill-panel tbody tr").count() == min(ч["records"], 50)
    вкладка.чисто()


def test_вторая_часть_спрашивается_окном_первой(вкладка):
    с = вкладка.открыть("voice")
    с.wait_for_function("() => (window.__asrhub.state.voice || {}).lists === 'ready'")
    окно = с.evaluate("() => window.__asrhub.state.voice.window")
    вторая = [_параметры(а) for а in вкладка.запросы
              if "/api/content/llm" in а and "part=heavy" in а]
    assert вторая and float(вторая[-1]["until"]) == pytest.approx(окно["until"])
    # Отчёт за период: вторая часть и сводка проблем — на момент первой.
    с = вкладка.открыть("voice_month")
    с.wait_for_selector("#vp-lists .card")
    с.wait_for_function("() => window.__asrhub.RENDERERS.voice_month.проблемы")
    момент = с.evaluate("() => window.__asrhub.RENDERERS.voice_month.данные.generated_at")
    период = [_параметры(а) for а in вкладка.запросы
              if "/api/voice/period" in а and "part=heavy" in а]
    проблемы = [_параметры(а) for а in вкладка.запросы
                if "/api/voice/problems" in а and "scale=month" in а]
    assert период and float(период[-1]["now"]) == pytest.approx(момент)
    assert проблемы and float(проблемы[-1]["now"]) == pytest.approx(момент)
    вкладка.чисто()


def test_день_календаря_раскрывается_сутками_сервера(сервер):
    """Браузер в Токио, сервер — в своём поясе: клетка дня раскрывается
    границами, которые прислал сервер, и в ней та же одна запись."""
    from drill_seed import общий_браузер

    адрес, _, день = сервер
    # Общий браузер файла (его же берут вкладки): второй Playwright в том же
    # потоке не запускается. Пояс — у своего контекста.
    контекст = общий_браузер().new_context(viewport={"width": 1600, "height": 1000},
                                           color_scheme="dark", timezone_id="Asia/Tokyo")
    try:
        с = контекст.new_page()
        запросы: list[str] = []
        с.on("request", lambda r: запросы.append(r.url))
        с.goto(f"{адрес}/#voice_quarter", wait_until="networkidle")
        с.wait_for_selector("#vp-cal svg rect")
        с.click("#vp-prev")
        с.wait_for_function("(д) => (window.__asrhub.RENDERERS.voice_quarter.данные.calendar || [])"
                            ".some((с) => с.date === д)", arg=день.isoformat())
        сутки = с.evaluate("(д) => window.__asrhub.RENDERERS.voice_quarter.данные.calendar"
                           ".find((с) => с.date === д)", день.isoformat())
        assert сутки["completed"] >= 1
        # Раскрываемые клетки идут по дням с разговорами, по порядку дат.
        дни = sorted(д["date"] for д in с.evaluate(
            "() => window.__asrhub.RENDERERS.voice_quarter.данные.calendar") if д["completed"])
        до = len(запросы)
        с.locator("#vp-cal svg rect.pick").nth(дни.index(день.isoformat())).click()
        с.wait_for_selector(".drill-panel tbody tr")
        раскрытие = [_параметры(а) for а in запросы[до:] if "/api/drill?" in а][-1]
        assert float(раскрытие["since"]) == pytest.approx(сутки["since"])
        assert float(раскрытие["until"]) == pytest.approx(сутки["until"])
        assert с.locator(".drill-panel tbody tr").count() == сутки["completed"]
    finally:
        контекст.close()
