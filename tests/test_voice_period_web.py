"""Заход 52 в настоящем браузере: разделы за период, добавочные, импорт, итоги без речи.

Серверные числа сверены в `test_voice_period.py`, `test_voice_agents.py`,
`test_employees_import_file.py` и `test_empty_outcomes.py`. Здесь — что
интерфейс их показывает: разделы за день, месяц, квартал и год рисуются без
ошибок, число раскрывается в список, стрелки листают период; «Голосовая
аналитика» сужается до добавочного; импорт сотрудников показывает
предпросмотр до записи; запись без речи — не ошибка и в карточке.
"""
from __future__ import annotations

import time

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять
from test_drill_voice import _досеять
from test_employees_import_file import ЛЮДИ, _книга_xml


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("voice-period-ui") / "data")
    _досеять(хранилище)
    db = хранилище.db
    свободные = [р["id"] for р in db.query(
        "SELECT j.id FROM jobs j WHERE j.status = 'completed' "
        "AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.job_id = j.id) ORDER BY j.created_at DESC")]
    for n, ид in enumerate(свободные):
        db.save_call(f"ui:{ид}", job_id=ид, agent=("301", "302")[n % 2], dst=("301", "302")[n % 2],
                     src="+79000000000", started_at=time.time() - n * 900, answered=True,
                     station="ui", direction="входящий")
    db.employee_save({"last_name": "Орлова", "first_name": "Вера", "department": "Продажи",
                      "phone_ext": "301", "source": "ui", "external_id": "301"})
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


@pytest.mark.parametrize(("вид", "корзин"), [
    ("voice_day", 24), ("voice_month", None), ("voice_quarter", None), ("voice_year", 12)])
def test_раздел_за_период_рисуется_и_раскрывается(вкладка, вид, корзин):
    с = вкладка.открыть(вид)
    с.wait_for_selector("#vp-body .vp-kpis .kpi", timeout=30000)
    с.wait_for_selector("#vp-lists .card")
    подпись = с.inner_text("#vp-label")
    assert подпись and подпись != "…"
    assert с.locator(".vp-summary li").count() >= 2
    assert с.locator(".vp-kpis .kpi").count() == 12
    for блок in ("#vp-load svg", "#vp-mix svg", "#vp-quality svg", "#vp-stack svg",
                 "#vp-pareto svg", "#vp-agents table"):
        assert с.locator(блок).count() >= 1, блок
    if вид in ("voice_quarter", "voice_year"):
        assert с.locator("#vp-cal svg rect").count() > 80
    else:
        assert с.locator("#vp-cal").count() == 0
    данные = с.evaluate(f"() => window.__asrhub.RENDERERS.{вид}.данные")
    if корзин:
        assert len(данные["series"]["labels"]) == корзин
    # Плитка «Разговоров с речью» раскрывается в столько же строк.
    плитка = с.locator(".vp-kpis .kpi").first.locator("button.drill")
    до = вкладка.раскрытий()
    плитка.click()
    с.wait_for_selector(".drill-panel tbody tr")
    assert вкладка.раскрытий() == до + 1
    итог = данные["totals"]["current"]["completed"]
    assert с.locator(".drill-panel tbody tr").count() == min(итог, 50)
    # Столбец нагрузки — тоже список.
    с.locator("#vp-load rect.pick").first.click()
    с.wait_for_timeout(600)
    assert с.locator(".drill-panel").count() >= 2
    # Стрелка назад листает период, вперёд из текущего — нельзя.
    assert с.locator("#vp-next").is_disabled()
    с.click("#vp-prev")
    с.wait_for_function("(было) => document.querySelector('#vp-label').textContent !== было",
                        arg=подпись)
    assert not с.locator("#vp-next").is_disabled()
    вкладка.чисто()


def test_отбор_по_добавочному_в_разделе_за_месяц(вкладка):
    с = вкладка.открыть("voice_month")
    с.wait_for_selector("#vp-agents table")
    с.wait_for_function("() => document.querySelectorAll('#vp-agent option').length > 1")
    assert "Орлова Вера" in с.inner_text("#vp-agent")
    всего = с.evaluate("() => window.__asrhub.RENDERERS.voice_month.данные.totals.current.completed")
    с.select_option("#vp-agent", "301")
    с.wait_for_function("() => (window.__asrhub.RENDERERS.voice_month.данные.filter || {}).agent === '301'")
    свой = с.evaluate("() => window.__asrhub.RENDERERS.voice_month.данные.totals.current.completed")
    assert 0 < свой < всего
    вкладка.чисто()


def test_голосовая_аналитика_показывает_добавочные(вкладка):
    с = вкладка.открыть("voice")
    с.select_option("#vo-period", "all")
    с.wait_for_selector("#vo-agents table")
    таблица = с.inner_text("#vo-agents")
    assert "301" in таблица and "Орлова В." in таблица
    # Строка причины раскрывается со столбцом «Добавочный».
    с.locator("#vo-s1 tr.drill-item").first.click()
    с.wait_for_selector("#vo-s1 .drill-panel tbody tr")
    assert "ДОБАВОЧНЫЙ" in с.inner_text("#vo-s1 .drill-panel thead").upper()
    # Номер в разрезе сужает раздел.
    с.locator('#vo-agents a[data-agent="302"]').click()
    с.wait_for_function("() => window.__asrhub.RENDERERS.voice.добавочный === '302'")
    с.wait_for_function("() => (window.__asrhub.state.voice || {}).agent === '302'")
    assert с.input_value("#vo-agent") == "302"
    вкладка.чисто()


def test_импорт_сотрудников_с_предпросмотром(вкладка, сервер, tmp_path):
    файл = tmp_path / "справочник.xml"
    файл.write_bytes(_книга_xml(ЛЮДИ))
    с = вкладка.открыть("staff")
    с.click("#st-file-import")
    с.set_input_files("#st-file", str(файл))
    с.wait_for_selector("#st-preview .kpi")
    текст = с.inner_text("#st-preview")
    assert "Как легли колонки" in текст and "тел. внутренний" in текст
    assert "Отдел продаж (МСК)" in текст and "<span>" not in текст
    # Предпросмотр ничего не записал.
    assert сервер[1].db.query_one(
        "SELECT COUNT(*) AS n FROM employees WHERE last_name='Сидоров'")["n"] == 0
    с.click("#st-apply")
    с.wait_for_selector(".modal-backdrop", state="detached")
    строка = сервер[1].db.query_one(
        "SELECT phone_ext, department FROM employees WHERE last_name='Сидоров'")
    assert строка["phone_ext"] == "149"
    assert строка["department"] == "Отдел ИТ поддержки Help desk (МСК)"
    вкладка.чисто()


def test_запись_без_речи_не_ошибка_в_карточке(вкладка, сервер):
    db = сервер[1].db
    ид = db.query_one("SELECT id FROM jobs WHERE status='no_speech' LIMIT 1")["id"]
    с = вкладка.открыть("queue")
    с.select_option("#q-filter", "no_speech")
    с.wait_for_selector(f'tr[data-job-id="{ид}"]')
    assert "без речи" in с.inner_text(f'tr[data-job-id="{ид}"]')
    с.evaluate(f"window.__asrhub.openJob('{ид}')")
    с.wait_for_selector(".empty-outcome")
    карточка = с.inner_text(".modal")
    assert "Без речи" in карточка and "не ошибка" in карточка
    assert с.locator('.modal [data-action="retry"]').inner_text().strip() == "Распознать заново"
    assert с.locator('.modal .card[style*="--err"]').count() == 0
