"""Заход 48: ссылка на запись снаружи — проверка настоящим браузером.

Примечание в CRM несёт ссылку «<public_url>/#/jobs/<id>», а интерфейс такого
адреса не понимал: раздела с таким именем нет, и ссылка открывала главную
страницу вместо расшифровки, ради которой по ней и шли.
"""
from __future__ import annotations

from test_review_46_web import _без_исключений, _вкладка, браузер, стенд  # noqa: F401


def test_ссылка_на_запись_открывает_её_карточку(стенд, браузер):  # noqa: F811
    задание = next(з for з in стенд["состояние"].db.list_jobs(status="completed", limit=50)
                   if з.get("filename"))
    вкладка = _вкладка(браузер, стенд["адрес"])
    вкладка.goto(f"{стенд['адрес']}/#/jobs/{задание['id']}", wait_until="networkidle")
    окно = вкладка.locator(".modal .modal-head b").first
    окно.wait_for(timeout=10000)
    assert окно.inner_text() == задание["filename"]
    assert вкладка.evaluate("location.hash") == "#results"
    _без_исключений(вкладка)
    вкладка.context.close()
