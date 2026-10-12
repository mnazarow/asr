"""Заход 51: номера разговора из интеграции поверх phone_asr.

Интеграция шлёт в `POST /process-call` рядом с `call_id` два номера: кто
звонил (`phone`) и кто говорил (`internalPhone`, добавочный сотрудника).
phone_asr их не читал, и ASR Hub тоже: разговор распознавался, а номера
терялись — запись не попадала ни в журнал звонков, ни в разрез по
сотрудникам, а в карточке не было видно, чей это разговор.

Здесь проверяется, что номера доходят до журнала звонков, связаны с
записью, сводятся к виду, в каком добавочный хранят АТС и справочник
сотрудников, и что без них всё работает как раньше.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_phone_compat import _mono_wav, _Receiver


@pytest.fixture()
def приёмник(tmp_path: Path):
    """Файловый сервер с записями и приёмник обратных вызовов."""
    сервер = _Receiver(tmp_path)
    try:
        yield сервер
    finally:
        сервер.close()


@pytest.fixture()
def клиент(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с очередью и демо-движком, как в проверках phone_asr."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_WEBHOOK_ALLOW_INTERNAL", "true")
    app = create_app(load(), start_queue=True)
    with TestClient(app) as c:
        c.headers.update({
            "X-API-Key": (tmp_path / "data" / "api-key.txt").read_text().strip()})
        yield c


def _отправить(клиент, приёмник, папка: Path, **поля) -> dict:
    """Разговор с одним файлом; ответ 202 — как у phone_asr."""
    имя = f"{поля.get('call_id', 'звонок')}.wav"
    if not (папка / имя).exists():
        _mono_wav(папка / имя, seconds=2.0)
    тело = {"call_id": "звонок", "base_url": приёмник.base,
            "files": [f"{приёмник.base}/{имя}"], **поля}
    ответ = клиент.post("/process-call", json=тело)
    assert ответ.status_code == 202, ответ.text
    return ответ.json()


def _задание(клиент, call_id: str, part: int = 1, ждать: float = 60.0) -> dict:
    """Задание разговора — когда скачивание закончилось и оно поставлено."""
    конец = time.time() + ждать
    while time.time() < конец:
        for задание in клиент.get("/api/jobs", params={"limit": 50}).json()["items"]:
            if задание.get("group_id") == call_id:
                подробно = клиент.get(f"/api/jobs/{задание['id']}",
                                      params={"with_waveform": False}).json()
                свой = (подробно.get("params") or {}).get("_phone") or {}
                if int(свой.get("part") or 1) == part:
                    return подробно
        time.sleep(0.2)
    raise AssertionError(f"задание разговора {call_id} не появилось")


def _звонки(клиент, **отбор) -> list[dict]:
    ответ = клиент.get("/api/telephony/calls", params={"period": "all", **отбор})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()["calls"]


def _звонок_задания(клиент, job_id: str, ждать: float = 10.0) -> dict | None:
    """Строка журнала пишется сразу после постановки задания — даём миг."""
    конец = time.time() + ждать
    while time.time() < конец:
        найдено = [з for з in _звонки(клиент) if з.get("job_id") == job_id]
        if найдено:
            return найдено[0]
        time.sleep(0.2)
    return None


def test_номера_разговора_попадают_в_журнал_звонков(клиент, приёмник, tmp_path):
    """Кто звонил — `src`, кто говорил — `dst` и `agent`: журнал показывает
    «кто → кому», а разрез по оператору и справочник сотрудников идут по
    `agent`."""
    _отправить(клиент, приёмник, tmp_path, call_id="ext-1",
               phone="+7 916 555-12-34", internalPhone="101")
    задание = _задание(клиент, "ext-1")
    assert задание["source"] == "phone"
    звонок = _звонок_задания(клиент, задание["id"])
    assert звонок is not None, "строки звонка нет"
    assert звонок["src"] == "+7 916 555-12-34"
    assert звонок["dst"] == "101" and звонок["agent"] == "101"
    assert звонок["station"] == "api"
    # Отбор по оператору в разделе «Телефония» его находит.
    assert [з["job_id"] for з in _звонки(клиент, agent="101")] == [задание["id"]]
    # Карточка записи показывает звонок — с добавочным.
    карточка = клиент.get(f"/api/jobs/{задание['id']}",
                                params={"with_waveform": False}).json()
    assert карточка["call"]["agent"] == "101"
    assert карточка["call"]["pbx_uid"] == "ext-1"
    # Событие задания говорит, что номера записаны.
    события = {с["kind"]: с for с in карточка["events"]}
    assert "call" in события and "добавочный 101" in события["call"]["message"]
    # И список «звонки этого добавочного» под числом отчёта — тоже.
    список = клиент.get("/api/drill", params={"base": "calls", "f": ["agent:101"]})
    assert список.json()["total"] == 1


def test_номер_числом_не_отменяет_разговор(клиент, приёмник, tmp_path):
    """PHP кладёт в JSON номер тем типом, каким он был в переменной:
    `'internalPhone' => $to` приезжает и числом. Отказ 422 из-за подписи
    стоил бы распознавания."""
    _отправить(клиент, приёмник, tmp_path, call_id="ext-2",
               phone=79165551234, internalPhone=102)
    задание = _задание(клиент, "ext-2")
    звонок = _звонок_задания(клиент, задание["id"])
    assert звонок["src"] == "79165551234" and звонок["agent"] == "102"


def test_без_номеров_всё_как_раньше(клиент, приёмник, tmp_path):
    """Номеров нет — строки в журнале нет, и карточка без блока звонка."""
    _отправить(клиент, приёмник, tmp_path, call_id="ext-3")
    задание = _задание(клиент, "ext-3")
    time.sleep(0.5)
    assert _звонок_задания(клиент, задание["id"], ждать=1.0) is None
    карточка = клиент.get(f"/api/jobs/{задание['id']}",
                                params={"with_waveform": False}).json()
    assert "call" not in карточка


def test_добавочный_с_подписью_сводится_к_цифрам(клиент, приёмник, tmp_path):
    """АТС и справочник хранят добавочный одними цифрами, и сравниваются
    они строкой: «доб. 103» и «103» — один стол."""
    _отправить(клиент, приёмник, tmp_path, call_id="ext-4",
               phone="84951234567", internal_phone="доб. 103", direction="in")
    задание = _задание(клиент, "ext-4")
    звонок = _звонок_задания(клиент, задание["id"])
    assert звонок["agent"] == "103" and звонок["dst"] == "доб. 103"
    # Направление — словом журнала, как у звонков с АТС.
    assert звонок["direction"] == "входящий"


def test_непонятое_направление_не_мешает_номерам(клиент, приёмник, tmp_path):
    _отправить(клиент, приёмник, tmp_path, call_id="ext-5",
               internalPhone="104", direction="вбок")
    задание = _задание(клиент, "ext-5")
    звонок = _звонок_задания(клиент, задание["id"])
    assert звонок["agent"] == "104" and звонок["direction"] == ""
    карточка = клиент.get(f"/api/jobs/{задание['id']}",
                                params={"with_waveform": False}).json()
    событие = next(с for с in карточка["events"] if с["kind"] == "call")
    assert "вбок" in str(событие.get("data"))


def test_части_одного_звонка_не_затирают_друг_друга(клиент, приёмник, tmp_path):
    """Запись частями — несколько заданий одного звонка. Ключ строки по
    `call_id` у второй части переписал бы строку первой."""
    for часть in (1, 2):
        _отправить(клиент, приёмник, tmp_path, call_id="ext-6",
                   part=часть, total_parts=2, phone="555", internalPhone="105")
    задания = [_задание(клиент, "ext-6", part=часть) for часть in (1, 2)]
    строки = [_звонок_задания(клиент, з["id"]) for з in задания]
    assert all(строки)
    assert len({с["uniqueid"] for с in строки}) == 2
    assert {с["pbx_uid"] for с in строки} == {"ext-6"}
    assert len(_звонки(клиент, agent="105")) == 2


def test_карточка_называет_сотрудника_по_добавочному(клиент, приёмник, tmp_path):
    """Добавочный — это стол, а читающему нужен человек."""
    ответ = клиент.post("/api/employees", json={
        "last_name": "Петрова", "first_name": "Мария", "middle_name": "Ивановна",
        "department": "Отдел продаж", "phone_ext": "106"})
    assert ответ.status_code == 200, ответ.text
    _отправить(клиент, приёмник, tmp_path, call_id="ext-7",
               phone="+7 916 555-12-34", internalPhone="106")
    задание = _задание(клиент, "ext-7")
    assert _звонок_задания(клиент, задание["id"])
    карточка = клиент.get(f"/api/jobs/{задание['id']}",
                                params={"with_waveform": False}).json()
    assert карточка["call"]["agent_name"] == "Петрова Мария Ивановна"
    assert карточка["call"]["agent_department"] == "Отдел продаж"
    # Ключу без персональных данных номер абонента приходит пометкой, а
    # добавочный — как есть: это стол, а не человек снаружи.
    ключ = клиент.post("/api/keys", json={
        "name": "без-пд", "role": "admin", "mask_pii": True}).json()["key"]
    скрыто = клиент.get(f"/api/jobs/{задание['id']}",
                              params={"with_waveform": False},
                              headers={"X-API-Key": ключ}).json()
    assert "555-12-34" not in скрыто["call"]["src"]
    assert скрыто["call"]["agent"] == "106"


@pytest.mark.parametrize("поле", ["internalPhone", "internal_phone"])
def test_схема_называет_поле_как_интеграция(клиент, поле):
    """В схеме OpenAPI поле — `internalPhone`, как его пишет интеграция;
    по имени `internal_phone` оно тоже принимается."""
    схема = клиент.app.openapi()
    тело = схема["components"]["schemas"]["ProcessCallBody"]["properties"]
    assert {"phone", "internalPhone", "direction"} <= set(тело)
    from asrhub.api.routes_phone import ProcessCallBody

    разобрано = ProcessCallBody.model_validate({
        "call_id": "x", "files": ["https://example.com/a.wav"],
        "base_url": "https://example.com", поле: 107})
    assert разобрано.internal_phone == "107"


def test_имя_не_подставляется_когда_связь_по_номеру_выключена(data_dir, monkeypatch):
    """`employees_match_ext: false` ставят там, где нумерация справочника и
    станции разная: имя по номеру было бы чужим."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as c:
        состояние = app.state.hub
        db = состояние.db
        номер = db.create_job({"id": "ext-name", "filename": "разговор.wav",
                               "source": "phone", "model": "demo-simulator"})
        db.save_call("api:ext-name", job_id=номер, src="555", dst="108", agent="108",
                     started_at=time.time(), answered=True, station="api")
        db.employee_save({"last_name": "Сидоров", "first_name": "Пётр", "phone_ext": "108"})
        карточка = c.get(f"/api/jobs/{номер}", params={"with_waveform": False}).json()
        assert карточка["call"]["agent_name"] == "Сидоров Пётр"
        состояние.settings.set("employees_match_ext", False)
        карточка = c.get(f"/api/jobs/{номер}", params={"with_waveform": False}).json()
        assert карточка["call"]["agent"] == "108" and "agent_name" not in карточка["call"]
