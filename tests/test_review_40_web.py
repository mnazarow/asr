"""Заход 40: веб-интерфейс и диктовка.

Поток /api/stream не пускал вошедших паролем; отказ приходил кодом HTTP 403
на рукопожатии вместо кода закрытия, и интерфейс не мог его объяснить;
«Применить» в настройках отправлял снимок всей страницы и откатывал чужие
правки; раздел «Голосовая аналитика» читал поля, которых сервер не отдаёт.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

КОРЕНЬ = Path(__file__).resolve().parent.parent
APP_JS = (КОРЕНЬ / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")


def _кусок(начало: str, конец: str, текст: str = APP_JS) -> str:
    i = текст.index(начало)
    return текст[i:текст.index(конец, i + len(начало))]


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys["ah_admin"] = {"name": "админ", "role": "admin", "enabled": True}
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        учётки = app.state.hub.accounts
        учётки.create("оператор", "Пароль-оператора-1", role="user")
        учётки.create("читатель", "Пароль-читателя-1", role="readonly")
        учётки.create("новичок", "Пароль-новичка-1", role="user", must_change_password=True)
        yield клиент


def _войти(клиент: TestClient, логин: str, пароль: str) -> None:
    клиент.cookies.clear()
    ответ = клиент.post("/api/auth/login", json={"username": логин, "password": пароль},
                        headers={"X-API-Key": ""})
    assert ответ.status_code == 200, ответ.text


def _код_закрытия(клиент: TestClient, **kwargs) -> int:
    # Настройку отправляем до ожидания: если сервер вдруг пустит, он ответит
    # «ready», и проверка упадёт сразу, а не повиснет в ожидании.
    with клиент.websocket_connect("/api/stream", **kwargs) as сокет, \
            pytest.raises(WebSocketDisconnect) as отказ:
        сокет.send_text(json.dumps({"type": "config", "format": "pcm_s16le"}))
        сокет.receive_text()
    return отказ.value.code


def test_диктовка_работает_у_вошедшего_паролем(сервер):
    """Поток принимал только ключ и билет, а билет выдаётся на ключ.

    У вошедшего логином и паролем ключа нет: кнопка возвращалась, статус
    навсегда «запрашиваем микрофон…», сообщения никакого — хотя лента
    событий /ws на той же куке работала.
    """
    _войти(сервер, "оператор", "Пароль-оператора-1")
    with сервер.websocket_connect("/api/stream", headers={"X-API-Key": ""}) as сокет:
        сокет.send_text(json.dumps({"type": "config", "format": "pcm_s16le"}))
        ответ = json.loads(сокет.receive_text())
    assert ответ["type"] == "ready", ответ


def test_читателю_диктовка_закрыта_кодом_а_не_обрывом(сервер):
    _войти(сервер, "читатель", "Пароль-читателя-1")
    assert _код_закрытия(сервер, headers={"X-API-Key": ""}) == 4403


def test_пароль_по_умолчанию_сначала_сменить(сервер):
    _войти(сервер, "новичок", "Пароль-новичка-1")
    assert _код_закрытия(сервер, headers={"X-API-Key": ""}) == 4403


def test_без_ключа_и_без_куки_отказ_приходит_кодом_4401(сервер):
    """Закрытие до accept() uvicorn превращал в HTTP 403 на рукопожатии.

    Браузер видел код 1006, и объяснения интерфейса для 4401/4403/4404 не
    показывались никогда.
    """
    сервер.cookies.clear()
    assert _код_закрытия(сервер, headers={"X-API-Key": ""}) == 4401


def test_кука_с_чужой_страницы_не_открывает_поток(сервер):
    """Кука уходит и при рукопожатии: чужая страница открыла бы сокет от
    имени вошедшего. Второй рубеж — тот же, что у запросов с кукой."""
    _войти(сервер, "оператор", "Пароль-оператора-1")
    assert _код_закрытия(сервер, headers={"X-API-Key": "", "Origin": "https://evil.example"}) \
        == 4401


# ---------------------------------------------------------------------------
# Интерфейс: что уходит на сервер
# ---------------------------------------------------------------------------


def test_применить_отправляет_только_изменённое():
    """Снимок всей страницы откатывал станцию АТС и набор категорий,
    заведённые после её загрузки, а пароль станции «***» ложился поверх
    настоящего."""
    кнопки = _кусок("qs('#p-apply').onclick", "qs('#p-reset').onclick")
    assert "API.put('/api/settings', state.settings)" not in кнопки
    assert "изменённые()" in кнопки
    карточка = _кусок("box.appendChild(paramCard(spec, state.settings[spec.key]", "}));")
    assert "state.settingsChanged.add(spec.key)" in карточка


def test_задание_несёт_только_заданное_человеком():
    """В settings задания уходила копия всех настроек сервера.

    Неадминистратору секреты приходят заглушкой «***», и загрузка файла
    отвечала 400 «Адрес уведомления должен начинаться с http://».
    """
    отправка = _кусок("async function submitFiles()", "\n}\n")
    assert "JSON.stringify(state.jobSettings)" not in отправка
    assert "jobOverrides()" in отправка
    переопределения = _кусок("function jobOverrides()", "\n}\n")
    assert "'***'" in переопределения


def _без_комментариев(код: str) -> str:
    return "\n".join(re.sub(r"(^|\s)//.*$", "", с) for с in код.splitlines())


def test_голосовая_аналитика_читает_поля_сервера():
    раздел = _без_комментариев(_кусок("RENDERERS.voice = {", "\n};\n"))
    for поле in ("д.records", "д.analyzed", "д.resolved_share", "д.pairs",
                 "д.action_items", "д.rows_total"):
        assert поле in раздел, f"раздел не читает {поле}"
    for старое in ("д.total", "resolved_rate", "actions_total"):
        assert старое not in раздел, f"раздел по-прежнему читает {старое}"
    # У графиков свой формат: hbars ждёт items, donut — parts.
    графики = re.findall(r"Charts\.(hbars|donut)\(qs\([^)]*\),\s*\{\s*(\w+):", раздел)
    assert графики and all(поле == ("items" if вид == "hbars" else "parts")
                           for вид, поле in графики), графики
    assert "labels:" not in раздел


def test_диктовка_сверяется_со_своей_попыткой_и_сессией():
    """Ушли из раздела, пока браузер спрашивал микрофон, — запись всё равно
    начиналась; «Остановить» и сразу «Начать» — и `onclose` старого сокета
    гасил новую запись."""
    раздел = _кусок("RENDERERS.dictation = {", "\n};\n")
    assert раздел.count("this.alive(attempt)") >= 3
    закрытие = _кусок("socket.onclose = (event) => {", "\n    };")
    assert "if (!mine()) return;" in закрытие
    assert "session.opened" in закрытие, "закрытие до открытия не объясняется"


def test_кнопки_обслуживания_без_прав_не_роняют_раздел():
    """У неадминистратора кнопок нет, а обработчик вешался безусловно:
    «Раздел не загрузился: Cannot set properties of null»."""
    assert "qs('#btn-cleanup').onclick" not in APP_JS
    assert "qs('#btn-unload').onclick" not in APP_JS
