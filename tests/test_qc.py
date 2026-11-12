"""Заход 55: контроль качества — обратный звонок клиенту с опросом.

Сервер перезванивает клиенту как обычный SIP-телефон станции, проговаривает
вопросы группы и записывает нажатые кнопки. Станцию здесь играет мини-АТС
(`qc_pbx.py`): спрашивает пароль, снимает трубку, слушает фразы и нажимает
кнопки. Проверяется то, что стоит дорого, если сломается:

* телефон проходит аутентификацию, а с неверным паролем — отказ словами;
* кнопки доходят всеми тремя способами (события RTP, SIP INFO, тон в звуке)
  в обоих кодеках и не задваиваются;
* опрос заводится только по подходящему разговору: добавочный группы,
  направление, длительность, выборка, пауза на номер, список «не звонить»;
* вне часов обзвона сервер не звонит, а устаревший опрос снимается;
* не ответил — повтор, потом «не дозвонились»; отказ станции попытку не тратит;
* кнопка отказа вносит номер в список «не звонить»;
* настройки и журнал — только администратору, пароль телефона не отдаётся.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from asrhub.qc import sip, survey, tts
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from qc_pbx import МиниАТС  # noqa: E402

АДМИН = {"X-API-Key": "ah_admin"}
#: «Синтезатор» проверок: полсекунды тона на любую фразу.
ГОЛОС = "ffmpeg -hide_banner -loglevel error -y -f lavfi -i sine=frequency=440:duration=0.5 {out}"
ГРУППА = {"name": "Продажи", "extensions": "101, 110-119", "direction": "in",
          "min_talk_s": 20, "optout_digit": "0",
          "questions": [{"text": "Оцените вежливость оператора от одного до пяти.",
                         "min": 1, "max": 5},
                        {"text": "Решён ли ваш вопрос? Один — да, два — нет.",
                         "min": 1, "max": 2}]}


@pytest.fixture()
def атс():
    станции: list[МиниАТС] = []

    def завести(**параметры) -> МиниАТС:
        станция = МиниАТС(**параметры)
        станции.append(станция)
        return станция

    yield завести
    for станция in станции:
        станция.закрыть()


def приложение(data_dir: Path, monkeypatch: pytest.MonkeyPatch, порт: int = 5060, **поверх):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin": {"name": "админ", "role": "admin", "enabled": True},
        "ah_user": {"name": "отдел", "role": "user", "enabled": True},
    })
    значения = {"qc_enabled": True, "qc_groups": [ГРУППА], "qc_sip_host": "127.0.0.1",
                "qc_sip_port": порт, "qc_sip_user": "900", "qc_sip_password": "secret",
                "qc_tts_command": ГОЛОС, "qc_hours": "", "qc_delay_s": 0,
                "qc_digit_timeout_s": 2, "qc_answer_timeout_s": 5, **поверх}
    for ключ, значение in значения.items():
        настройки.set(ключ, значение)
    app = create_app(настройки, start_queue=False)
    return app, app.state.hub


def звонок(hub, ключ: str = "pbx:1.1", **поля) -> None:
    данные = {"src": "79991112233", "dst": "101", "agent": "101", "direction": "входящий",
              "answered": 1, "duration": 60, "billsec": 50,
              "started_at": time.time() - 70, "station": "pbx", **поля}
    hub.db.save_call(ключ, job_id=None, owner="sales", **данные)


def дождаться(hub, номер: str | None = None, *, срок: float = 30.0) -> dict:
    конец = time.time() + срок
    while time.time() < конец:
        строки = hub.db.query("SELECT * FROM qc_surveys" + (" WHERE id=?" if номер else ""),
                              (номер,) if номер else ())
        if строки and all(с["status"] in survey.ИТОГОВЫЕ for с in строки):
            return survey.опрос_наружу(строки[0])
        time.sleep(0.1)
    raise AssertionError([dict(с) for с in hub.db.query("SELECT * FROM qc_surveys")])


def взвести(hub) -> None:
    """Отметка «с какого звонка смотреть» — как после первого включения."""
    hub.db.set_kv(hub.qc.КЛЮЧ_ОТМЕТКИ, time.time() - 3600)


# ---------------------------------------------------------------------------
# Телефон
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("способ", ["rfc2833", "info", "inband"])
@pytest.mark.parametrize("кодек", ["PCMA", "PCMU"])
def test_кнопки_доходят_любым_способом(атс, tmp_path, способ, кодек):
    станция = атс(способ=способ, кодек=кодек, кнопки=["4", "7", "2"])

    class Настройки:
        def get(self, ключ, запасное=None):
            return {"qc_tts_command": ГОЛОС}.get(ключ, запасное)

    группа = survey.разобрать_группы([ГРУППА])[0]
    звуки = {т: tts.приготовить(т, Настройки(), tmp_path / "tts") for т in группа.фразы()}
    телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="secret",
                          имя="Контроль качества")
    try:
        assert телефон.зарегистрировать() == 300
        разговор = телефон.позвонить("989991112233")
        assert разговор.кодек == кодек
        итог = survey.провести(разговор, группа, звуки, ждать_кнопку_с=2)
        разговор.положить()
    finally:
        телефон.закрыть()
    # «7» вне шкалы второго вопроса — вопрос повторён, принято «2»; ни одна
    # кнопка не засчитана дважды.
    assert [о["digit"] for о in итог["answers"]] == [4, 2], (итог, станция.нажато)
    assert not итог["optout"] and not итог["hangup"]
    assert станция.набрано == ["989991112233"]
    assert "Контроль качества" in станция.от_заголовок
    assert станция.bye_от_телефона.wait(3) and станция.снята_регистрация
    assert станция.получено_звука > 8000 * 2


def test_неверный_пароль_называется_словами(атс):
    станция = атс()
    телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="не тот")
    try:
        with pytest.raises(sip.ОшибкаSIP, match="логин или пароль") as отказ:
            телефон.зарегистрировать()
        assert отказ.value.код == 401
        with pytest.raises(sip.ОшибкаSIP, match="логин или пароль"):
            телефон.позвонить("101")
    finally:
        телефон.закрыть(снять_регистрацию=False)


def test_занято_не_отвечает_и_нет_номера(атс):
    for ответ, ошибка, код in (("busy", sip.Занято, 486), ("noanswer", sip.НеОтвечает, 487),
                               ("notfound", sip.ОшибкаSIP, 404)):
        станция = атс(ответ=ответ)
        телефон = sip.Телефон("127.0.0.1", станция.порт, пользователь="900", пароль="secret")
        try:
            with pytest.raises(ошибка) as отказ:
                телефон.позвонить("101", ждать_ответа_с=5 if ответ != "noanswer" else 5.5)
            assert отказ.value.код == код
            if ответ == "noanswer":
                assert станция.отменён.is_set(), "вызов без ответа должен быть отменён (CANCEL)"
        finally:
            телефон.закрыть(снять_регистрацию=False)


def test_дайджест_совпадает_с_образцом_из_rfc():
    """RFC 2617, раздел 3.5: тот же запрос — тот же отклик."""
    вызов = {"realm": "testrealm@host.com", "qop": "auth", "algorithm": "MD5",
             "nonce": "dcd98b7102dd2f0e8b11d0f600bfb0c093",
             "opaque": "5ccc069c403ebaf9f0171e9517f40e41"}
    ответ = sip.ответ_на_вызов(вызов, метод="GET", адрес="/dir/index.html",
                               пользователь="Mufasa", пароль="Circle Of Life")
    поля = sip.разобрать_вызов(ответ)
    import hashlib

    первая = hashlib.md5(b"Mufasa:testrealm@host.com:Circle Of Life").hexdigest()  # noqa: S324
    вторая = hashlib.md5(b"GET:/dir/index.html").hexdigest()                        # noqa: S324
    ожидается = hashlib.md5(                                                         # noqa: S324
        f"{первая}:{вызов['nonce']}:{поля['nc']}:{поля['cnonce']}:auth:{вторая}".encode()
    ).hexdigest()
    assert поля["response"] == ожидается and поля["opaque"] == вызов["opaque"]
    # Образец из RFC с его nc и cnonce.
    assert hashlib.md5(                                                              # noqa: S324
        f"{первая}:{вызов['nonce']}:00000001:0a4f113b:auth:{вторая}".encode()
    ).hexdigest() == "6629fae49393a05397450978507c4ef1"


def test_голос_не_принимает_тон_за_кнопку_а_тон_находит():
    import math
    import struct

    искатель = sip.ИскательТонов("PCMA")
    голос = struct.pack("<8000h", *[
        int(7000 * math.sin(2 * math.pi * 180 * н / 8000)
            + 4000 * math.sin(2 * math.pi * 900 * н / 8000)
            + 2500 * math.sin(2 * math.pi * 2300 * н / 8000)) for н in range(8000)])
    сжатый = sip.сжать(голос, "PCMA")
    assert not any(искатель.подать(сжатый[н:н + 160]) for н in range(0, len(сжатый), 160))
    for кнопка in "0123456789*#":
        искатель = sip.ИскательТонов("PCMU")
        тон = sip.тон_кнопки(кнопка, мс=200, кодек="PCMU")
        найдено = [искатель.подать(тон[н:н + 160]) for н in range(0, len(тон), 160)]
        assert [к for к in найдено if к] == [кнопка], (кнопка, найдено)


# ---------------------------------------------------------------------------
# Группы и отбор
# ---------------------------------------------------------------------------

def test_группы_разбираются_и_ошибки_называют_группу():
    группа = survey.разобрать_группы([ГРУППА])[0]
    assert группа.содержит("101") and группа.содержит("115") and not группа.содержит("120")
    assert not группа.содержит("0115"), "отрезок сравнивает и длину номера"
    for порча, что in (({"extensions": "101, 10!2"}, "не похоже на добавочный"),
                       ({"questions": [{"text": "Вопрос", "min": 5, "max": 1}]}, "min меньше max"),
                       ({"optout_digit": "3"}, "попадает в шкалу"),
                       ({"direction": "вбок"}, "direction"),
                       ({"name": ""}, "не задано имя")):
        with pytest.raises(ValueError, match=что):
            survey.разобрать_группы([{**ГРУППА, **порча}])
    with pytest.raises(ValueError, match="повторяется"):
        survey.разобрать_группы([ГРУППА, ГРУППА])
    assert survey.проверить_группы([{"name": "Пустая", "extensions": "101"}]) == [
        "группа «Пустая»: нет ни одного вопроса"]


def test_отбор_звонка():
    группы = survey.разобрать_группы([ГРУППА, {**ГРУППА, "name": "Исходящие", "extensions": "201",
                                               "direction": "out"}])
    основа = {"uniqueid": "pbx:1", "src": "+7 (999) 111-22-33", "dst": "101", "agent": "101",
              "direction": "входящий", "answered": 1, "billsec": 50}
    группа, добавочный, клиент = survey.отобрать(основа, группы)
    assert (группа.name, добавочный, клиент) == ("Продажи", "101", "79991112233")
    for порча, причина in (({"agent": "300"}, "добавочный вне групп"),
                           ({"billsec": 5}, "короткий разговор"),
                           ({"answered": 0}, "без ответа"),
                           ({"direction": "внутренний"}, "не внешний звонок"),
                           ({"direction": "исходящий", "dst": "89990001122"}, "не то направление"),
                           ({"src": "102"}, "нет номера клиента")):
        assert survey.отобрать({**основа, **порча}, группы) == причина
    исходящий = {**основа, "agent": "201", "src": "201", "dst": "89995556677",
                 "direction": "исходящий"}
    assert survey.отобрать(исходящий, группы)[2] == "89995556677"
    # Выборка постоянна для звонка и близка к заданной доле.
    треть = survey.разобрать_группы([{**ГРУППА, "sample_percent": 30}])
    взято = [н for н in range(1000)
             if not isinstance(survey.отобрать({**основа, "uniqueid": f"pbx:{н}"}, треть), str)]
    assert 240 < len(взято) < 360
    assert взято == [н for н in range(1000)
                     if not isinstance(survey.отобрать({**основа, "uniqueid": f"pbx:{н}"}, треть), str)]


def test_часы_обзвона():
    from datetime import datetime

    assert survey.разобрать_часы("09:00-20:00") == (540, 1200)
    assert survey.разобрать_часы("") == (0, 1440)
    день, ночь = survey.разобрать_часы("9-20"), survey.разобрать_часы("22:00-06:00")
    assert survey.в_часах(день, datetime(2026, 11, 10, 9, 0))
    assert not survey.в_часах(день, datetime(2026, 11, 10, 20, 0))
    assert survey.в_часах(ночь, datetime(2026, 11, 10, 23, 30))
    assert survey.в_часах(ночь, datetime(2026, 11, 10, 5, 59))
    assert not survey.в_часах(ночь, datetime(2026, 11, 10, 12, 0))
    for плохо in ("с утра", "25:00-26:00", "10:00-10:00"):
        with pytest.raises(ValueError):
            survey.разобрать_часы(плохо)


# ---------------------------------------------------------------------------
# Служба: от звонка в архиве до оценки
# ---------------------------------------------------------------------------

def test_после_разговора_клиенту_перезванивают_и_записывают_оценки(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["5", "1"])
    app, hub = приложение(data_dir, monkeypatch, станция.порт, qc_dial_prefix="9")
    взвести(hub)
    звонок(hub)
    звонок(hub, "pbx:1.2", agent="300", dst="300")               # добавочный вне групп
    assert hub.qc.найти_новые() == 1
    assert hub.qc.найти_новые() == 0, "тот же звонок второй опрос не заводит"
    assert hub.qc.обзвон() == 1
    опрос = дождаться(hub)
    assert опрос["status"] == survey.ПРОЙДЕН and опрос["attempts"] == 1, опрос
    assert [о["digit"] for о in опрос["answers"]] == [5, 1]
    assert опрос["score"] == 3.0 and опрос["score_pct"] == 50.0
    assert опрос["ext"] == "101" and опрос["group_name"] == "Продажи"
    assert станция.набрано == ["979991112233"], "номер клиента с префиксом набора"
    with TestClient(app) as клиент:
        свод = клиент.get("/api/qc/summary", headers=АДМИН).json()
        assert свод["total"]["rated"] == 1 and свод["total"]["score"] == 3.0
        assert свод["extensions"][0]["ext"] == "101"
        вопросы = {в["text"]: в for в in свод["questions"]}
        assert вопросы[ГРУППА["questions"][0]["text"]]["distribution"] == {"5": 1}
        журнал = клиент.get("/api/qc/surveys", headers=АДМИН).json()
        assert журнал["total"] == 1 and журнал["items"][0]["number"] == "79991112233"


def test_повторный_разговор_того_же_номера_не_опрашивается(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["4", "2"])
    _, hub = приложение(data_dir, monkeypatch, станция.порт)
    взвести(hub)
    звонок(hub)
    assert hub.qc.найти_новые() == 1
    hub.qc.обзвон()
    assert дождаться(hub)["status"] == survey.ПРОЙДЕН
    звонок(hub, "pbx:2.1", src="8 999 111-22-33")
    assert hub.qc.найти_новые() == 0, "пауза на номер (qc_cooldown_days)"
    hub.settings.set("qc_cooldown_days", 0)
    звонок(hub, "pbx:3.1")
    assert hub.qc.найти_новые() == 1


def test_кнопка_отказа_вносит_номер_в_список_не_звонить(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["0"])
    app, hub = приложение(data_dir, monkeypatch, станция.порт, qc_cooldown_days=0)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    hub.qc.обзвон()
    опрос = дождаться(hub)
    assert опрос["status"] == survey.ОТКАЗ, опрос
    assert hub.qc.в_стоп_листе("+7 999 111-22-33") and hub.qc.в_стоп_листе("89991112233")
    звонок(hub, "pbx:2.1")
    assert hub.qc.найти_новые() == 0, "номеру из списка «не звонить» опрос не заводится"
    with TestClient(app) as клиент:
        список = клиент.get("/api/qc/stoplist", headers=АДМИН).json()["items"]
        assert [з["number"] for з in список] == ["79991112233"]
        assert клиент.delete("/api/qc/stoplist/79991112233", headers=АДМИН).status_code == 200
        assert клиент.delete("/api/qc/stoplist/79991112233", headers=АДМИН).status_code == 404
    звонок(hub, "pbx:3.1")
    assert hub.qc.найти_новые() == 1


def test_вне_часов_не_звонят_а_устаревший_опрос_снимается(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["5", "1"])
    _, hub = приложение(data_dir, monkeypatch, станция.порт)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    from datetime import datetime

    monkeypatch.setattr(hub.qc, "_сейчас_местное", lambda: datetime(2026, 11, 10, 3, 0))
    hub.settings.set("qc_hours", "09:00-20:00")
    assert hub.qc.обзвон() == 0 and not станция.набрано
    assert hub.qc.состояние()["hours"]["open"] is False
    hub.db.execute("UPDATE qc_surveys SET call_at=?", (time.time() - 25 * 3600,))
    hub.qc.обзвон()
    assert hub.db.query_one("SELECT status FROM qc_surveys")["status"] == survey.УСТАРЕЛ


def test_не_ответил_повтор_потом_не_дозвонились(атс, data_dir, monkeypatch):
    станция = атс(ответ="busy")
    _, hub = приложение(data_dir, monkeypatch, станция.порт, qc_max_attempts=2, qc_retry_min=1)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    assert hub.qc.обзвон() == 1
    конец = time.time() + 15
    while time.time() < конец:
        строка = dict(hub.db.query_one("SELECT * FROM qc_surveys"))
        if строка["status"] == survey.ЖДЁТ and строка["attempts"] == 1:
            break
        time.sleep(0.1)
    assert строка["status"] == survey.ЖДЁТ and строка["sip_code"] == 486, строка
    assert строка["due_at"] > time.time() + 30, "повтор — через qc_retry_min"
    assert hub.qc.обзвон() == 0
    hub.db.execute("UPDATE qc_surveys SET due_at=?", (time.time() - 1,))
    assert hub.qc.обзвон() == 1
    опрос = дождаться(hub)
    assert опрос["status"] == survey.НЕ_ДОЗВОНИЛИСЬ and опрос["attempts"] == 2


def test_отказ_станции_не_тратит_попытку_клиента(атс, data_dir, monkeypatch):
    станция = атс(пароль="другой")
    _, hub = приложение(data_dir, monkeypatch, станция.порт)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    hub.qc.обзвон()
    конец = time.time() + 15
    while time.time() < конец:
        строка = dict(hub.db.query_one("SELECT * FROM qc_surveys"))
        if строка["status"] == survey.ЖДЁТ and строка["error"]:
            break
        time.sleep(0.1)
    assert строка["status"] == survey.ЖДЁТ and строка["attempts"] == 0, строка
    assert "логин или пароль" in строка["error"]
    assert hub.qc.обзвон() == 0, "обзвон придержан после отказа станции"
    assert hub.qc.состояние()["registration"]["state"] == "отказ"


def test_клиент_положил_трубку_посреди_опроса(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["3"], положить_после=1)
    _, hub = приложение(data_dir, monkeypatch, станция.порт)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    hub.qc.обзвон()
    опрос = дождаться(hub)
    assert опрос["status"] == survey.ЧАСТИЧНО, опрос
    assert [о["digit"] for о in опрос["answers"]] == [3, None]


def test_первое_включение_не_обзванивает_историю(data_dir, monkeypatch):
    _, hub = приложение(data_dir, monkeypatch)
    звонок(hub)
    assert hub.qc.найти_новые() == 0
    assert hub.qc.найти_новые() == 0
    звонок(hub, "pbx:2.1", src="79995550000")
    assert hub.qc.найти_новые() == 1


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def test_пробный_звонок_через_api(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["5", "2"])
    app, hub = приложение(data_dir, monkeypatch, станция.порт, qc_enabled=False,
                          qc_hours="03:00-03:01")
    with TestClient(app) as клиент:
        проверка = клиент.post("/api/qc/check", headers=АДМИН).json()
        assert проверка["ok"] is True and проверка["state"] == "зарегистрирован", проверка
        ответ = клиент.post("/api/qc/test-call", headers=АДМИН,
                            json={"number": "102", "group": "Продажи"})
        assert ответ.status_code == 200, ответ.text
        опрос = дождаться(hub, ответ.json()["id"])
        assert опрос["kind"] == "проба" and опрос["status"] == survey.ПРОЙДЕН, опрос
        assert станция.набрано == ["102"]
        # Проба в отчёт не идёт.
        assert клиент.get("/api/qc/summary", headers=АДМИН).json()["total"]["surveys"] == 0
        плохо = клиент.post("/api/qc/test-call", headers=АДМИН,
                            json={"number": "102", "group": "Нет такой"})
        assert плохо.status_code == 400 and "не найдена" in плохо.json()["message"]


def test_группы_сохраняются_и_проверяются(data_dir, monkeypatch):
    app, hub = приложение(data_dir, monkeypatch, qc_groups=[])
    with TestClient(app) as клиент:
        новая = {**ГРУППА, "name": "Поддержка", "extensions": "200-205"}
        ответ = клиент.put("/api/qc/groups", headers=АДМИН, json={"groups": [новая]})
        assert ответ.status_code == 200, ответ.text
        assert клиент.get("/api/qc/groups", headers=АДМИН).json()["items"][0]["name"] == "Поддержка"
        assert hub.qc.группы()[0].содержит("203")
        плохо = клиент.put("/api/qc/groups", headers=АДМИН,
                           json={"groups": [{**новая, "extensions": "abc def!"}]})
        assert плохо.status_code == 400 and "Поддержка" in плохо.json()["message"]
        assert hub.qc.группы()[0].extensions == "200-205", "отказ ничего не меняет"
        звук = клиент.post("/api/qc/tts", headers=АДМИН, json={"text": "Проверка голоса"})
        assert звук.status_code == 200 and звук.content[:4] == b"RIFF" and len(звук.content) > 4000


def test_всё_только_администратору_и_пароль_не_отдаётся(data_dir, monkeypatch):
    app, _ = приложение(data_dir, monkeypatch)
    обычный = {"X-API-Key": "ah_user"}
    with TestClient(app) as клиент:
        # Отчёты опросов (журнал, свод, выгрузка) с 3.1.32 открыты и не
        # администратору — в охвате его заданий (руководителю — по
        # подразделению; см. test_review_60). Настройки, телефон, голос и
        # список «не звонить» — только администратору.
        for адрес in ("/api/qc/surveys", "/api/qc/summary", "/api/qc/surveys/export"):
            ответ = клиент.get(адрес, headers=обычный)
            assert ответ.status_code == 200, (адрес, ответ.status_code)
            assert клиент.get(адрес).status_code == 401
        assert клиент.get("/api/qc/surveys", headers=обычный).json()["total"] == 0
        for метод, адрес, тело in (("get", "/api/qc/status", None), ("get", "/api/qc/groups", None),
                                   ("get", "/api/qc/stoplist", None),
                                   ("post", "/api/qc/test-call", {"number": "1", "group": "x"}),
                                   ("post", "/api/qc/tts", {"text": "x"}),
                                   ("put", "/api/qc/groups", {"groups": []})):
            ответ = getattr(клиент, метод)(адрес, headers=обычный, **({"json": тело} if тело else {}))
            assert ответ.status_code == 403, (адрес, ответ.status_code)
            assert getattr(клиент, метод)(адрес, **({"json": тело} if тело else {})).status_code == 401
        админу = клиент.get("/api/settings", headers=АДМИН).json()["values"]
        assert админу["qc_sip_password"] == "***" and админу["qc_sip_user"] == "900"
        чужому = клиент.get("/api/settings", headers=обычный).json()["values"]
        assert "qc_sip_password" not in чужому or чужому["qc_sip_password"] == "***"
        assert "qc_sip_host" not in чужому and "qc_groups" not in чужому
        состояние = клиент.get("/api/qc/status", headers=АДМИН).json()
        assert состояние["enabled"] and состояние["configured"] and состояние["tts"]["available"]
        assert "secret" not in str(состояние)


def test_удаление_по_требованию_убирает_опросы_номера(data_dir, monkeypatch):
    app, hub = приложение(data_dir, monkeypatch)
    hub.db.execute(
        "INSERT INTO qc_surveys (id, call_key, kind, number, status, created_at, answers) "
        "VALUES ('qc_1', 'pbx:9', 'опрос', '79991112233', 'пройден', ?, '[]')", (time.time(),))
    with TestClient(app) as клиент:
        ответ = клиент.post("/api/maintenance/erase", headers=АДМИН,
                            json={"query": "79991112233", "dry_run": False})
        assert ответ.status_code == 200, ответ.text
        assert ответ.json()["qc_surveys"] == 1
    assert hub.db.query_one("SELECT COUNT(*) AS n FROM qc_surveys")["n"] == 0


def test_схема_обновляется_со_старой_версии(tmp_path):
    import sqlite3

    from asrhub.db import SCHEMA_VERSION, Database

    путь = tmp_path / "asrhub.db"
    Database(путь).close()
    соединение = sqlite3.connect(путь)
    соединение.execute("DROP TABLE qc_surveys")
    соединение.execute("DROP TABLE qc_stoplist")
    соединение.execute("PRAGMA user_version=33")
    соединение.commit()
    соединение.close()
    база = Database(путь)
    assert база.query_one("PRAGMA user_version")[0] == SCHEMA_VERSION >= 34
    assert база.query_one("SELECT COUNT(*) AS n FROM qc_surveys")["n"] == 0
    assert база.query_one("SELECT COUNT(*) AS n FROM qc_stoplist")["n"] == 0


# ---------------------------------------------------------------------------
# Только короткие номера и аудиозапись для обучения
# ---------------------------------------------------------------------------

def test_только_номера_из_трёх_и_четырёх_знаков():
    группы = survey.разобрать_группы([ГРУППА])
    основа = {"uniqueid": "pbx:1", "src": "205", "dst": "101", "agent": "101",
              "direction": "внутренний", "answered": 1, "billsec": 50}
    # Без режима внутренний звонок не опрашивается — как и раньше.
    assert survey.отобрать(основа, группы) == "не внешний звонок"
    assert survey.отобрать(основа, группы, короткие=True)[2] == "205"
    # Сотрудник группы звонил сам: собеседник — тот, кому он звонил.
    сам = {**основа, "src": "101", "dst": "4321"}
    assert survey.отобрать(сам, группы, короткие=True)[2] == "4321"
    # Городской и мобильный номер в этом режиме не набирается вовсе.
    for порча in ({"src": "79991112233", "direction": "входящий"}, {"src": "12345"},
                  {"src": "22"}):
        assert survey.отобрать({**основа, **порча}, группы, короткие=True) \
            == "номер не из 3–4 знаков"
    assert survey.отобрать({**основа, "src": "101"}, группы, короткие=True) == "нет номера клиента"
    # Короткий номер по внешнему звонку (станция так пометила) — годится.
    assert survey.отобрать({**основа, "direction": "входящий"}, группы, короткие=True)[2] == "205"


def test_режим_коротких_номеров_в_службе_и_префикс_не_добавляется(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["4", "1"])
    _, hub = приложение(data_dir, monkeypatch, станция.порт, qc_short_numbers_only=True,
                        qc_dial_prefix="9")
    взвести(hub)
    звонок(hub, "pbx:5.1")                                        # городской — мимо
    звонок(hub, "pbx:5.2", src="205", direction="внутренний")     # внутренний — опрос
    assert hub.qc.найти_новые() == 1
    hub.qc.обзвон()
    опрос = дождаться(hub)
    assert опрос["number"] == "205" and опрос["status"] == survey.ПРОЙДЕН, опрос
    assert станция.набрано == ["205"]              # без «9»
    # Сотрудник нажал бы «не звонить» — короткий номер в список вносится.
    assert hub.qc.в_стоп_лист("205") and hub.qc.в_стоп_листе("205")


def _wav(секунд: float = 1.0) -> bytes:
    import math
    import struct

    отсчёты = [int(9000 * math.sin(2 * math.pi * 440 * н / 8000)) for н in range(int(8000 * секунд))]
    данные = struct.pack(f"<{len(отсчёты)}h", *отсчёты)
    return (b"RIFF" + struct.pack("<I", 36 + len(данные)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, 8000, 16000, 2, 16)
            + b"data" + struct.pack("<I", len(данные)) + данные)


def test_запись_диктора_загружается_и_звучит_вместо_текста(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["5", "1"])
    app, hub = приложение(data_dir, monkeypatch, станция.порт, qc_enabled=False)
    with TestClient(app) as клиент:
        # Не звук и чужое расширение — отказ словами.
        плохой = клиент.post("/api/qc/audio", headers=АДМИН,
                             files={"file": ("урок.wav", b"not a sound at all", "audio/wav")})
        assert плохой.status_code == 400, плохой.text
        assert клиент.post("/api/qc/audio", headers=АДМИН,
                           files={"file": ("урок.exe", b"MZ", "audio/wav")}).status_code == 400
        assert клиент.get("/api/qc/audio", headers=АДМИН).json()["items"] == []
        ответ = клиент.post("/api/qc/audio", headers=АДМИН,
                            files={"file": ("../Вопрос 1.wav", _wav(1.0), "audio/wav")})
        assert ответ.status_code == 200, ответ.text
        имя = ответ.json()["name"]
        assert имя == "Вопрос 1.wav" and 0.9 < ответ.json()["seconds"] < 1.2
        вопросы = [{"text": f"audio:{имя}", "min": 1, "max": 5}, ГРУППА["questions"][1]]
        assert клиент.put("/api/qc/groups", headers=АДМИН, json={
            "groups": [{**ГРУППА, "questions": вопросы}]}).status_code == 200
        запись = клиент.get("/api/qc/audio", headers=АДМИН).json()["items"][0]
        assert запись["groups"] == ["Продажи"] and запись["phrase"] == f"audio:{имя}"
        звук = клиент.post("/api/qc/tts", headers=АДМИН, json={"text": f"audio:{имя}"})
        assert звук.status_code == 200 and звук.content[:4] == b"RIFF"
        assert клиент.get(f"/api/qc/audio/{имя}", headers=АДМИН).content[:4] == b"RIFF"
        занята = клиент.delete(f"/api/qc/audio/{имя}", headers=АДМИН)
        assert занята.status_code == 400 and "используется" in занята.json()["message"]
        проба = клиент.post("/api/qc/test-call", headers=АДМИН,
                            json={"number": "205", "group": "Продажи"}).json()
        опрос = дождаться(hub, проба["id"])
        assert опрос["status"] == survey.ПРОЙДЕН, опрос
        assert [о["digit"] for о in опрос["answers"]] == [5, 1]
        assert клиент.get("/api/qc/audio", headers={"X-API-Key": "ah_user"}).status_code == 403
        клиент.put("/api/qc/groups", headers=АДМИН, json={"groups": [ГРУППА]})
        assert клиент.delete(f"/api/qc/audio/{имя}", headers=АДМИН).status_code == 200
        assert клиент.delete(f"/api/qc/audio/{имя}", headers=АДМИН).status_code == 404
        assert клиент.get("/api/qc/audio/..%2F..%2Fconfig.yaml", headers=АДМИН).status_code == 404


#: «Синтезатор по образцу» проверок: первые полсекунды самого образца. Если
#: образец до команды не дошёл, ffmpeg упадёт — и фраза не приготовится.
ПО_ОБРАЗЦУ = "ffmpeg -hide_banner -loglevel error -y -i {sample} -t 0.5 {out}"


def test_образец_голоса_для_синтезатора(атс, data_dir, monkeypatch):
    станция = атс(кнопки=["5", "1"])
    app, hub = приложение(data_dir, monkeypatch, станция.порт, qc_enabled=False,
                          qc_tts_command=ПО_ОБРАЗЦУ)
    with TestClient(app) as клиент:
        # Команда ждёт образец, а его нет: голос не готов, и причина названа.
        пусто = клиент.get("/api/qc/voice-sample", headers=АДМИН).json()
        assert пусто["exists"] is False and пусто["engine"]["available"] is False
        assert "образец" in пусто["engine"]["reason"]
        assert клиент.post("/api/qc/tts", headers=АДМИН,
                           json={"text": "Здравствуйте"}).status_code == 400
        assert клиент.get("/api/qc/voice-sample/audio", headers=АДМИН).status_code == 404
        # Негодные образцы: не звук, слишком короткий, не то расширение.
        for файл, слово in ((("голос.wav", b"not a sound", "audio/wav"), "не читается"),
                            (("голос.wav", _wav(1.0), "audio/wav"), "короче"),
                            (("голос.txt", b"abc", "text/plain"), "не звуковой")):
            отказ = клиент.post("/api/qc/voice-sample", headers=АДМИН, files={"file": файл})
            assert отказ.status_code == 400 and слово in отказ.json()["message"], отказ.text
        assert клиент.get("/api/qc/voice-sample", headers=АДМИН).json()["exists"] is False
        ответ = клиент.post("/api/qc/voice-sample", headers=АДМИН,
                            files={"file": ("Диктор Анна.wav", _wav(5.0), "audio/wav")})
        assert ответ.status_code == 200, ответ.text
        сведения = ответ.json()
        assert сведения["exists"] and сведения["name"] == "Диктор Анна.wav"
        assert 4.8 < сведения["seconds"] < 5.2
        assert сведения["engine"]["clone"] is True and сведения["engine"]["available"] is True
        assert клиент.get("/api/qc/status", headers=АДМИН).json()["tts"]["clone"] is True
        assert клиент.get("/api/qc/voice-sample/audio", headers=АДМИН).content[:4] == b"RIFF"
        # Фраза готовится голосом из образца — и в пробе, и в звонке.
        первая = клиент.post("/api/qc/tts", headers=АДМИН, json={"text": "Здравствуйте"})
        assert первая.status_code == 200 and len(первая.content) > 4000
        проба = клиент.post("/api/qc/test-call", headers=АДМИН,
                            json={"number": "205", "group": "Продажи"}).json()
        assert дождаться(hub, проба["id"])["status"] == survey.ПРОЙДЕН
        # Новый образец — новый голос: кеш прежних фраз не используется.
        кеш = hub.qc.каталог_звука()
        было = {ф.name for ф in кеш.glob("*.alaw")}
        time.sleep(0.05)
        клиент.post("/api/qc/voice-sample", headers=АДМИН,
                    files={"file": ("Другой.wav", _wav(4.0), "audio/wav")})
        клиент.post("/api/qc/tts", headers=АДМИН, json={"text": "Здравствуйте"})
        assert {ф.name for ф in кеш.glob("*.alaw")} - было
        # Негодный файл прежний образец не затирает.
        клиент.post("/api/qc/voice-sample", headers=АДМИН,
                    files={"file": ("плохой.wav", b"zzz", "audio/wav")})
        assert клиент.get("/api/qc/voice-sample", headers=АДМИН).json()["name"] == "Другой.wav"
        # Только администратору; после удаления команда снова без голоса.
        assert клиент.get("/api/qc/voice-sample",
                          headers={"X-API-Key": "ah_user"}).status_code == 403
        убрано = клиент.delete("/api/qc/voice-sample", headers=АДМИН).json()
        assert убрано["deleted"] is True and убрано["exists"] is False
        assert клиент.delete("/api/qc/voice-sample", headers=АДМИН).json()["deleted"] is False


def test_образец_без_подходящего_синтезатора_не_ломает_обычный_голос(tmp_path, monkeypatch):
    кеш = tmp_path / "qc" / "tts"
    источник = tmp_path / "голос.wav"
    источник.write_bytes(_wav(4.0))
    tts.принять_образец(источник, кеш, "голос.wav")
    образец = tts.образец(кеш)
    assert образец is not None

    def настройки(команда: str = ""):
        class Настройки:
            def get(self, ключ, запасное=None):
                return {"qc_tts_command": команда}.get(ключ, запасное)
        return Настройки()

    # Обычная своя команда: образец есть, но ей не нужен — предупреждение, не отказ.
    описание = tts.движок(настройки(ГОЛОС), образец)
    assert описание["available"] and not описание["clone"] and "{sample}" in описание["warning"]
    # Без своей команды: есть XTTS — берётся он, нет — обычный голос с объяснением.
    monkeypatch.setattr(tts.shutil, "which", lambda имя: "/usr/bin/" + имя if имя == "tts" else None)
    с_xtts = tts.движок(настройки(), образец)
    assert с_xtts["clone"] and "xtts_v2" in с_xtts["command"] and "{say}" in с_xtts["command"]
    monkeypatch.setattr(tts.shutil, "which",
                        lambda имя: "/usr/bin/" + имя if имя == "espeak-ng" else None)
    без = tts.движок(настройки(), образец)
    assert без["available"] and not без["clone"] and "coqui-tts" in без["warning"]
    assert tts.движок(настройки(), None)["warning"] == ""
