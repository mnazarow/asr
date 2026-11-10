"""Заход 55 в настоящем браузере: раздел «Контроль качества».

Телефон, служба опросов и маршруты проверены в `test_qc.py`. Здесь — что
администратор проходит раздел от начала до конца: заводит группу со своим
текстом, задаёт телефон, проверяет регистрацию на станции, делает пробный
звонок и видит ответы клиента в журнале; вносит номер в список «не звонить».
Станцию играет мини-АТС из `qc_pbx.py`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from drill_seed import Архив, Вкладка, браузер, поднять

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qc_pbx import МиниАТС  # noqa: E402

ГОЛОС = "ffmpeg -hide_banner -loglevel error -y -f lavfi -i sine=frequency=440:duration=0.5 {out}"


@pytest.fixture(scope="module")
def сервер(tmp_path_factory):
    pytest.importorskip("playwright.sync_api",
                        reason="нужен пакет playwright для проверок интерфейса")
    if браузер() is None:
        pytest.skip("браузер не найден: PLAYWRIGHT_CHROMIUM или /opt/pw-browsers")
    хранилище = Архив(tmp_path_factory.mktemp("qc-ui") / "data", заданий=2)
    станция = МиниАТС(кнопки=["", "5", "2"])
    настройки = хранилище.app.state.hub.settings
    настройки.set("qc_tts_command", ГОЛОС)
    настройки.set("qc_sip_local_ip", "127.0.0.1")
    адрес, остановить = поднять(хранилище.app)
    yield адрес, хранилище, станция
    остановить()
    станция.закрыть()
    хранилище.закрыть()


@pytest.fixture()
def вкладка(сервер):
    окно = Вкладка(сервер[0])
    try:
        yield окно
    finally:
        окно.закрыть()


def test_от_группы_до_оценки_в_журнале(вкладка, сервер):
    _, хранилище, станция = сервер
    настройки = хранилище.app.state.hub.settings
    с = вкладка.открыть("qc")
    с.wait_for_selector("#qc-group-add")
    текст = с.inner_text("#content")
    assert "Как это работает" in текст and "Обзвон выключен" in текст
    assert "Групп пока нет" in текст
    # Группа: имя, добавочные, свой текст, второй вопрос со своей шкалой.
    с.click("#qc-group-add")
    с.fill('[data-group="0"] [data-поле=name]', "Продажи")
    с.fill('[data-group="0"] [data-поле=extensions]', "101, 110-119")
    с.fill('[data-group="0"] [data-поле=greeting]', "Здравствуйте! Это служба качества.")
    с.click('[data-group="0"] [data-добавить-вопрос]')
    # Введённое до добавления вопроса не теряется.
    assert с.input_value('[data-group="0"] [data-поле=name]') == "Продажи"
    с.fill('[data-group="0"] [data-вопрос="1"] [data-в=text]', "Решён ли ваш вопрос? Один — да, два — нет.")
    с.fill('[data-group="0"] [data-вопрос="1"] [data-в=max]', "2")
    с.click("#qc-groups-save")
    с.wait_for_function("() => document.querySelector('#qc-test-group option')?.value === 'Продажи'")
    группы = настройки.get("qc_groups")
    assert группы[0]["name"] == "Продажи" and len(группы[0]["questions"]) == 2
    assert группы[0]["questions"][1]["max"] == 2
    # Ошибка в группе называется словами и ничего не сохраняет.
    с.fill('[data-group="0"] [data-поле=extensions]', "10!2")
    с.click("#qc-groups-save")
    с.wait_for_selector("#qc-groups-out .banner.err")
    assert "Продажи" in с.inner_text("#qc-groups-out")
    assert настройки.get("qc_groups")[0]["extensions"] == "101, 110-119"
    с.click("#qc-groups-reset")
    # Телефон: адрес мини-АТС, логин, пароль; проверка регистрации.
    с.fill("[data-qc=qc_sip_host]", "127.0.0.1")
    с.fill("[data-qc=qc_sip_port]", str(станция.порт))
    с.fill("[data-qc=qc_sip_user]", "900")
    с.fill("[data-qc=qc_sip_password]", "secret")
    с.click("#qc-phone-check")
    с.wait_for_selector("#qc-phone-out .banner.ok", timeout=15000)
    assert станция.регистраций >= 1
    assert настройки.get("qc_sip_user") == "900"
    # Пароль в поле после перечитывания — заглушка; повторное сохранение его не портит.
    с.click("#qc-phone-save")
    с.wait_for_timeout(600)
    assert настройки.get("qc_sip_password") == "secret"
    # Пробный звонок: абонент мини-АТС нажимает 5 и 2.
    с.fill("#qc-test-number", "79990001122")
    с.click("#qc-test-go")
    с.wait_for_selector("#qc-test-out .banner.ok")
    с.wait_for_function(
        "() => (document.querySelector('#qc-journal-table')?.innerText || '').includes('пройден')",
        timeout=40000)
    журнал = с.inner_text("#qc-journal-table")
    assert "79990001122" in журнал and "проба" in журнал
    assert станция.набрано == ["79990001122"] and станция.нажато[-2:] == ["5", "2"]
    # Список «не звонить».
    с.fill("#qc-stop-number", "8 (999) 000-11-22")
    с.click("#qc-stop-add")
    с.wait_for_selector("[data-qc-unstop]")
    assert "000" in с.inner_text("#qc-stoplist")
    вкладка.чисто()


def test_включение_одной_кнопкой_и_переход_в_настройки(вкладка, сервер):
    настройки = сервер[1].app.state.hub.settings
    с = вкладка.открыть("qc")
    с.wait_for_selector("#qc-toggle")
    с.click("#qc-toggle")
    с.wait_for_function("() => document.querySelector('#qc-state')?.innerText.includes('Обзвон включён')")
    assert настройки.get("qc_enabled") is True
    с.click("#qc-toggle")
    с.wait_for_function("() => document.querySelector('#qc-state')?.innerText.includes('Обзвон выключен')")
    assert настройки.get("qc_enabled") is False
    с.click("#qc-phone-more")
    с.wait_for_selector("#group-nav button.active")
    assert "Контроль качества" in с.inner_text("#group-nav button.active")
    вкладка.чисто()


def test_короткие_номера_образец_голоса_и_запись_диктора(вкладка, сервер, tmp_path):
    import math
    import struct

    настройки = сервер[1].app.state.hub.settings
    отсчёты = [int(9000 * math.sin(2 * math.pi * 440 * н / 8000)) for н in range(40000)]
    данные = struct.pack(f"<{len(отсчёты)}h", *отсчёты)
    файл = tmp_path / "Урок.wav"
    файл.write_bytes(b"RIFF" + struct.pack("<I", 36 + len(данные)) + b"WAVEfmt "
                     + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
                     + b"data" + struct.pack("<I", len(данные)) + данные)
    с = вкладка.открыть("qc")
    с.wait_for_selector("#qc-audio-upload")
    # Режим «только номера из 3 и 4 знаков» — флажком, с записью в конфигурацию.
    assert настройки.get("qc_short_numbers_only") is False
    с.check("[data-qc=qc_short_numbers_only]")
    с.click("#qc-phone-save")
    с.wait_for_function("() => document.querySelector('[data-qc=qc_short_numbers_only]')?.checked")
    с.wait_for_timeout(500)
    assert настройки.get("qc_short_numbers_only") is True
    # Запись загружается и появляется в списке и в выборе группы.
    с.set_input_files("#qc-audio-file", str(файл))
    с.click("#qc-audio-upload")
    с.wait_for_selector("#qc-audio-table")
    assert "Урок.wav" in с.inner_text("#qc-audio-table")
    # Образец голоса: загружается, называется, убирается. Своя команда проверок
    # образец не использует — раздел говорит об этом прямо.
    с.set_input_files("#qc-voice-file", str(файл))
    с.click("#qc-voice-upload")
    с.wait_for_selector("#qc-voice-del")
    текст = с.inner_text("#qc-voice")
    assert "Урок.wav" in текст and "не используется" in текст
    assert (сервер[1].app.state.hub.qc.каталог_звука().parent / "voice" / "sample.wav").is_file()
    с.once("dialog", lambda окно: окно.accept())
    с.click("#qc-voice-del")
    с.wait_for_function("() => !document.querySelector('#qc-voice-del')")
    вкладка.чисто()
