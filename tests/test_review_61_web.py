"""Заход 61 в браузере: проход «роль × раздел × тема × ширина».

Каждый раздел меню открывается под администратором, пользователем и
ключом «только чтение», в светлой и тёмной теме, на широком экране и на
телефоне. Проверяется то, что ломается незаметно: исключение в скрипте,
ошибка сервера 5xx, пустой раздел и вылезающая за экран телефона
страница. Отказы 401 и 403 у не-администратора — ожидаемые и не считаются.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, браузер, поднять

КЛЮЧИ = {
    "admin": "ah_matrix_admin_0123456789abcdef",
    "user": "ah_matrix_user_0123456789abcdefgh",
    "readonly": "ah_matrix_readonly_0123456789abcd",
}
ПРОХОДЫ = [
    ("admin", "light", 1600),
    ("admin", "dark", 390),
    ("user", "dark", 1600),
    ("readonly", "light", 390),
]


@pytest.fixture(scope="module")
def стенд(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("matrix") / "data", auth=True, заданий=24,
                      ключи={ключ: {"name": f"матрица-{роль}", "role": роль}
                             for роль, ключ in КЛЮЧИ.items()})
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


@pytest.mark.parametrize(("роль", "тема", "ширина"), ПРОХОДЫ,
                         ids=[f"{р}-{т}-{ш}" for р, т, ш in ПРОХОДЫ])
def test_every_section_renders_for_role_theme_and_width(стенд, роль, тема, ширина):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as игра:
        движок = игра.chromium.launch(executable_path=браузер())
        try:
            # Политика содержимого — настоящая (проверки обходят её по
            # умолчанию): нарушение в любом разделе — ошибка консоли.
            страница = движок.new_page(viewport={"width": ширина, "height": 900},
                                       color_scheme=тема, bypass_csp=False)
            страница.add_init_script(
                f"try {{ localStorage.setItem('asrhub_key', '{КЛЮЧИ[роль]}');"
                f" localStorage.setItem('asrhub_theme', '{тема}'); }} catch (e) {{}}")
            ошибки: list[str] = []
            страница.on("pageerror", lambda e: ошибки.append(f"исключение: {e}"))
            страница.on("console", lambda m: ошибки.append(f"консоль: {m.text}")
                        if m.type == "error" and "Failed to load resource" not in m.text
                        else None)
            страница.on("response", lambda r: ошибки.append(f"ответ {r.status}: {r.url}")
                        if r.status >= 500 else None)
            страница.goto(f"{стенд}/#dashboard", wait_until="networkidle")
            страница.wait_for_timeout(1000)
            разделы = страница.evaluate(
                "() => [...new Set([...document.querySelectorAll('.nav-item[data-view]')]"
                ".map((к) => к.dataset.view))]")
            assert len(разделы) >= 20, разделы
            вылезли, пустые = [], []
            for раздел in разделы:
                ошибки_до = len(ошибки)
                страница.goto(f"{стенд}/#{раздел}", wait_until="networkidle")
                страница.wait_for_timeout(700)
                if len(ошибки) > ошибки_до:
                    ошибки[ошибки_до:] = [f"{раздел}: {о}" for о in ошибки[ошибки_до:]]
                if not страница.inner_text("#content").strip():
                    пустые.append(раздел)
                if ширина < 600 and страница.evaluate(
                        "() => document.documentElement.scrollWidth > innerWidth + 2"):
                    вылезли.append(раздел)
            assert not ошибки, "\n".join(ошибки[:20])
            assert not пустые, f"пустые разделы: {пустые}"
            assert not вылезли, f"на ширине {ширина} страница шире экрана: {вылезли}"
        finally:
            движок.close()
