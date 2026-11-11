"""Заход 56 в браузере: вставка разметки, телефон, контраст, окна вместо prompt().

* «Ядовитые» строки во всех текстовых полях базы и в именах станции,
  сотрудника, группы контроля качества: ни в одном разделе они не
  становятся разметкой. Раньше так прошло имя станции в плитке раздела
  «Asterisk: диалплан» — теперь это постоянная проверка.
* Карточка записи на экране телефона не шире экрана, крестик на месте.
* Основная кнопка и «бледный» текст — не ниже 4,5:1 в обеих темах.
* Новый ключ доступа — в окне, которое не закрывается случайным Esc.
* Заголовок вкладки браузера называет раздел.
"""
from __future__ import annotations

import json
import re
import urllib.request

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять

ЯД = """x"'><img src=x onerror=window.__x=(window.__x||0)+1>"""
ПРОВЕРКА = ("() => ({x: window.__x || 0, imgs: [...document.querySelectorAll('img[src=x]')]"
            ".slice(0, 3).map((i) => ((i.parentElement.closest('[id]') || {}).id || '') + ' | '"
            " + i.parentElement.outerHTML.slice(0, 160))})")


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api", reason="нужен пакет playwright")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    архив = Архив(tmp_path_factory.mktemp("z56") / "data", заданий=40)
    адрес, остановить = поднять(архив.app)
    yield адрес, архив
    остановить()
    архив.закрыть()


def _api(адрес: str, метод: str, путь: str, тело: dict) -> None:
    запрос = urllib.request.Request(адрес + путь, data=json.dumps(тело).encode(), method=метод,
                                    headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(запрос).read()
    except Exception:  # noqa: BLE001 — часть полей может не подойти, это не важно
        pass


def _отравить(адрес: str, архив: Архив) -> list[str]:
    for добавочный in ("101", "102"):
        _api(адрес, "POST", "/api/employees", {
            "last_name": "Иванов" + ЯД, "first_name": "Пётр" + ЯД, "phone_ext": добавочный,
            "department": "Отдел " + ЯД, "position": "Должность " + ЯД, "note": ЯД})
    настройки = архив.app.state.hub.settings
    настройки.set("telephony_stations", [{"id": "main", "name": "Станция " + ЯД,
                                          "source": "dialplan", "enabled": True}])
    настройки.set("qc_groups", [{"name": "Группа " + ЯД, "extensions": "101",
                                 "greeting": ЯД, "questions": [{"text": ЯД, "min": 1, "max": 5}]}])
    db = архив.db
    пропустить = re.compile(r"(^id$|_id$|^status$|^key$|^kind$|^role$|hash|^state$|json|"
                            r"^code$|error_code|^base$|^type$|^uniqueid$|^number$)")
    изменено = []
    for таблица in [с["name"] for с in db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite%' "
            "AND name NOT LIKE '%fts%'")]:
        if таблица in ("kv", "sessions", "users", "api_keys", "settings", "audit", "schema_version"):
            continue
        for столбец in db.query(f'PRAGMA table_info("{таблица}")'):
            имя = столбец["name"]
            if "TEXT" not in (столбец["type"] or "").upper() or столбец["pk"] or пропустить.search(имя):
                continue
            try:
                строка = db.query_one(f'SELECT "{имя}" AS v FROM "{таблица}" '  # noqa: S608
                                      f'WHERE "{имя}" IS NOT NULL AND "{имя}" <> \'\' LIMIT 1')
                if not строка or str(строка["v"])[:1] in "{[":
                    continue
                db.execute(f'UPDATE "{таблица}" SET "{имя}" = "{имя}" || ? '  # noqa: S608
                           f'WHERE "{имя}" IS NOT NULL AND "{имя}" <> \'\'', (ЯД,))
                изменено.append(f"{таблица}.{имя}")
            except Exception:  # noqa: BLE001
                continue
    return изменено


def test_ядовитые_строки_не_становятся_разметкой_ни_в_одном_разделе(сервер):
    адрес, архив = сервер
    изменено = _отравить(адрес, архив)
    assert "jobs.filename" in изменено and len(изменено) > 20
    from pathlib import Path

    меню = (Path(__file__).resolve().parents[1] / "server" / "asrhub" / "web"
            / "index.html").read_text(encoding="utf-8")
    # Все разделы меню, кроме диктовки (она просит микрофон).
    разделы = [р for р in re.findall(r'data-view="([a-z_]+)"', меню) if р != "dictation"]
    assert "manual" in разделы and "qc" in разделы
    вкладка = Вкладка(адрес)
    найдено: dict[str, list[str]] = {}
    try:
        с = вкладка.страница
        for раздел in разделы:
            вкладка.открыть(раздел)
            итог = с.evaluate(ПРОВЕРКА)
            if итог["imgs"]:
                найдено[раздел] = итог["imgs"]
            вкладок = с.evaluate("() => document.querySelectorAll("
                                 "'#content [data-tab], #content .tabs button').length")
            for н in range(min(вкладок, 8)):
                с.evaluate("(н) => { const к = [...document.querySelectorAll("
                           "'#content [data-tab], #content .tabs button')][н]; if (к) к.click(); }", н)
                с.wait_for_timeout(500)
                итог = с.evaluate(ПРОВЕРКА)
                if итог["imgs"]:
                    найдено[f"{раздел}/{н}"] = итог["imgs"]
        assert not найдено, json.dumps(найдено, ensure_ascii=False, indent=1)
        assert с.evaluate("window.__x || 0") == 0
    finally:
        вкладка.закрыть()


def test_карточка_записи_на_телефоне_не_шире_экрана(сервер):
    адрес, _ = сервер
    вкладка = Вкладка(адрес, ширина=375, высота=800)
    try:
        с = вкладка.открыть("results")
        с.locator("#content tbody tr button:has-text('Открыть')").first.click()
        с.wait_for_selector(".modal")
        с.wait_for_timeout(800)
        ширина = с.evaluate("document.querySelector('.modal').getBoundingClientRect().width")
        assert ширина <= 376, ширина
        крестик = с.evaluate("(() => { const к = [...document.querySelectorAll('.modal-head button')]"
                             ".pop(); const r = к.getBoundingClientRect(); return r.right; })()")
        assert крестик <= 376, крестик
    finally:
        вкладка.закрыть()


КОНТРАСТ = """(выбор) => {
  const цвет = (с) => { const m = с.match(/\\d+(\\.\\d+)?/g).map(Number); return m.slice(0, 3); };
  const яркость = ([r, g, b]) => { const f = (c) => { c /= 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
  const фон = (у) => { while (у) { const с = getComputedStyle(у).backgroundColor;
    if (с && !с.startsWith('rgba(0, 0, 0, 0)') && с !== 'transparent') return цвет(с);
    у = у.parentElement; } return [255, 255, 255]; };
  const у = document.querySelector(выбор);
  if (!у) return null;
  const a = яркость(цвет(getComputedStyle(у).color)), b = яркость(фон(у));
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}"""


@pytest.mark.parametrize("тема", ["dark", "light"])
def test_контраст_кнопки_и_бледного_текста(сервер, тема):
    адрес, _ = сервер
    вкладка = Вкладка(адрес)
    try:
        с = вкладка.открыть("results")
        if тема == "light":
            с.evaluate("document.body.classList.add('light')")
        с.evaluate("""() => { const п = document.createElement('div');
            п.className = 'card'; п.innerHTML = '<button class="primary" id="z-btn">Кнопка</button>'
            + '<span class="faint" id="z-faint">бледный</span>';
            document.querySelector('#content').prepend(п); }""")
        assert с.evaluate(КОНТРАСТ, "#z-btn") >= 4.5
        assert с.evaluate(КОНТРАСТ, "#z-faint") >= 4.5
    finally:
        вкладка.закрыть()


def test_новый_ключ_в_окне_и_заголовок_вкладки(сервер):
    адрес, _ = сервер
    вкладка = Вкладка(адрес)
    try:
        с = вкладка.открыть("system")
        assert с.evaluate("document.title").startswith("Сервер")
        if not с.locator("#key-name").count():
            pytest.skip("раздел ключей не показан на этом сервере")
        диалоги: list[str] = []
        с.on("dialog", lambda д: (диалоги.append(д.message), д.dismiss()))
        с.fill("#key-name", "проба окна")
        с.click("#key-create")
        с.wait_for_selector("[data-secret]")
        ключ = с.input_value("[data-secret]")
        assert ключ.startswith("ah_") and not диалоги
        с.keyboard.press("Escape")
        с.wait_for_timeout(300)
        assert с.locator("[data-secret]").count() == 1, "Esc не закрывает окно с ключом"
        с.click("[data-secret-done]")
        assert с.locator("[data-secret]").count() == 0
    finally:
        вкладка.закрыть()
