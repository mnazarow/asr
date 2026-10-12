"""Заход 51: добавочный в карточке записи — проверка настоящим браузером.

Номера разговора из интеграции ложатся в журнал звонков, а карточка записи
спрашивала журнал только у записей с АТС: у разговора, приехавшего через
`POST /process-call`, блока звонка не было вовсе. Теперь он есть у любого
задания со строкой в журнале — с добавочным и именем сотрудника из
справочника.
"""
from __future__ import annotations

import time

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("phone-ext-ui") / "data", заданий=8,
                      с_моделью=0)
    db = хранилище.db
    # Журнал звонков общих данных здесь не нужен: проверяется ровно то, что
    # записали ниже.
    db.execute("DELETE FROM calls")
    # Разговор, приехавший через phone_asr: источник «phone», строка журнала
    # с номерами — так её пишет `routes_phone._записать_звонок`.
    db.execute("UPDATE jobs SET source='phone' WHERE id='dr0001'")
    db.save_call("api:ext-ui-1", job_id="dr0001", owner="",
                 src="+79165551234", dst="101", clid="+79165551234", agent="101",
                 started_at=time.time() - 3600, duration=57, billsec=57,
                 answered=True, station="api", pbx_uid="ext-ui-1")
    db.save_call("api:ext-ui-2", job_id="dr0002", owner="",
                 src="+79165550000", dst="555", agent="555",
                 started_at=time.time() - 7200, duration=20, billsec=20,
                 answered=True, station="api", pbx_uid="ext-ui-2")
    db.employee_save({"last_name": "Петрова", "first_name": "Мария",
                      "middle_name": "Ивановна", "department": "Отдел продаж",
                      "phone_ext": "101"})
    адрес, остановить = поднять(хранилище.app)
    yield адрес
    остановить()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер)
    try:
        yield окно
    finally:
        окно.закрыть()


def _карточка(вкладка, номер: str) -> str:
    с = вкладка.открыть(f"/jobs/{номер}")
    с.wait_for_selector(f".modal-backdrop[data-job-id='{номер}']")
    return с.locator(".modal-backdrop").inner_text()


def test_карточка_разговора_из_интеграции_показывает_добавочный_и_сотрудника(вкладка):
    текст = _карточка(вкладка, "dr0001")
    assert "Звонок\n" in текст or "Звонок " in текст
    assert "Звонок с АТС" not in текст
    assert "+79165551234 → 101" in текст
    assert "добавочный 101 — Петрова Мария Ивановна, Отдел продаж" in текст
    вкладка.чисто()


def test_добавочный_без_справочника_показывается_номером(вкладка):
    текст = _карточка(вкладка, "dr0002")
    assert "добавочный 555" in текст and "добавочный 555 —" not in текст
    вкладка.чисто()


def test_запись_без_звонка_блока_не_получает(вкладка):
    текст = _карточка(вкладка, "dr0003")
    assert "добавочный" not in текст and "Звонок" not in текст
    вкладка.чисто()
