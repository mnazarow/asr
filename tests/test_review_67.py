"""Заход 67: окна участников по мостам (`BridgeEnter`/`BridgeLeave`, CEL).

Окна участников разговора с переводом считались по ногам CDR (секунды,
консультация доставалась второму оператору) и уточнялись событием перевода.
Мосты точнее: записанный канал слышит участника, только пока оба в одном
мосту. Здесь — разбор событий мостов из AMI и CEL, окна по пересечению
пребывания в мосту, участник очереди через `Local`-канал, хранение событий
только у разговоров с переводом и пересчёт отрезков, когда пришли мосты.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from test_review_65 import ВТОРАЯ, ЗАПИСАННАЯ, НАЧАЛО, РЕПЛИКИ

МОСТ_КЛИЕНТА = "b-client-0001"


def мост(вид: str, через: float, uid: str, канал: str, *, мост_: str = МОСТ_КЛИЕНТА,
         связь: str = "1789000000.1") -> dict:
    return {"Event": "BridgeEnter" if вид == "enter" else "BridgeLeave",
            "Timestamp": str(НАЧАЛО + через), "Linkedid": связь, "Uniqueid": uid,
            "BridgeUniqueid": мост_, "Channel": канал}


# Очередь: клиент (1.1, транк) в мосту с Local-каналом участника 101 с 5-й
# секунды; 101 консультируется с 102 в отдельном мосту (58–65 с), на 65-й
# переводит; 102 входит в мост клиента на 70-й (пока звонил его телефон,
# клиент слушал гудки), разговор кончается на 100-й.
СОБЫТИЯ_AMI = [
    мост("enter", 5, "1.1", "PJSIP/provider-00000001"),
    мост("enter", 5, "1.2", "Local/101@from-queue-00000001;1"),
    мост("enter", 5.1, "1.3", "PJSIP/101-00000002", мост_="b-local-0001"),
    мост("enter", 58, "1.5", "PJSIP/101-00000004", мост_="b-consult-0001"),
    мост("enter", 58, "1.6", "PJSIP/102-00000005", мост_="b-consult-0001"),
    мост("leave", 65, "1.2", "Local/101@from-queue-00000001;1"),
    мост("leave", 65, "1.5", "PJSIP/101-00000004", мост_="b-consult-0001"),
    мост("leave", 65, "1.6", "PJSIP/102-00000005", мост_="b-consult-0001"),
    мост("enter", 70, "1.6", "PJSIP/102-00000005"),
    мост("leave", 100, "1.6", "PJSIP/102-00000005"),
    мост("leave", 100, "1.1", "PJSIP/provider-00000001"),
]


def _мосты() -> list[dict]:
    from asrhub.telephony import transfers as П

    return [м for м in (П.из_моста_ami(с) for с in СОБЫТИЯ_AMI) if м is not None]


def _записанная() -> dict:
    return {**ЗАПИСАННАЯ, "pbx_uid": "1.1"}


def test_события_мостов_ami_и_cel_в_строки():
    from asrhub.telephony import transfers as П

    вход = П.из_моста_ami(СОБЫТИЯ_AMI[1])
    assert вход == {"ts": НАЧАЛО + 5, "linkedid": "1789000000.1", "kind": "enter",
                    "uniqueid": "1.2", "bridge": МОСТ_КЛИЕНТА, "agent": "101"}
    assert П.из_моста_ami(СОБЫТИЯ_AMI[-1])["kind"] == "leave"
    # Канал с номером клиента — без добавочного; без моста — не наше.
    наружу = П.из_моста_ami(мост("enter", 5, "1.9", "SIP/79001234567-00000009"))
    assert наружу["agent"] == ""
    assert П.из_моста_ami({**СОБЫТИЯ_AMI[0], "BridgeUniqueid": ""}) is None
    assert П.из_моста_ami({"Event": "Newchannel"}) is None
    # CEL: BRIDGE_ENTER/BRIDGE_EXIT — мост в eventextra.
    cel = П.из_строки_cel(["BRIDGE_ENTER", str(НАЧАЛО + 70), "PJSIP/102-00000005", "1.6",
                           "1789000000.1", "s", '{"bridge_id":"b-client-0001",'
                           '"bridge_technology":"simple_bridge"}'])
    assert (cel["kind"], cel["agent"], cel["bridge"], cel["uniqueid"]) == (
        "enter", "102", МОСТ_КЛИЕНТА, "1.6")
    переводы, мосты = П.разделить([cel, {"kind": "blind"}])
    assert переводы == [{"kind": "blind"}] and мосты == [cel]


def test_окна_по_мостам_очередь_и_консультация():
    from asrhub.telephony import segments as отрезки

    мосты = _мосты()
    # Ноль — первый вход записанного канала в мост (НАЧАЛО + 5).
    assert отрезки.ноль_записи(_записанная(), [ВТОРАЯ], мосты) == НАЧАЛО + 5
    окна = отрезки.окна(_записанная(), [ВТОРАЯ], длительность=95, мосты=мосты)
    # 101 — через Local-канал до перевода; 102 — с входа в мост клиента:
    # консультация (отдельный мост) и гудки клиенту не его.
    assert [(о["agent"], о["from_s"], о["to_s"], bool(о.get("by_bridge"))) for о in окна] == [
        ("101", 0.0, 60.0, True), ("102", 65.0, 95.0, True)]
    итог = отрезки.по_участникам(_записанная(), [ВТОРАЯ], РЕПЛИКИ, длительность=95,
                                 мосты=мосты)
    assert "мост" in итог["note"] and "мост" in итог["zero"]
    # «Коллега, клиент по доставке» (56–58 с) — 101: в мосту клиента был он.
    assert [(о["agent"], о["replicas"]) for о in итог["segments"]] == [("101", 4), ("102", 2)]


def test_без_записанного_канала_в_событиях_окна_по_cdr():
    from asrhub.telephony import segments as отрезки

    чужие = [м for м in _мосты() if м["uniqueid"] != "1.1"]
    окна = отрезки.окна(_записанная(), [ВТОРАЯ], длительность=95, мосты=чужие)
    assert [(о["agent"], о["from_s"], о["to_s"]) for о in окна] == [
        ("101", 0.0, 60.0), ("102", 55.0, 95.0)]
    assert not any(о.get("by_bridge") for о in окна)


def test_запись_на_канале_оператора():
    """Исходящий: записан канал самого 101 — его окно — пребывание в мостах."""
    from asrhub.telephony import segments as отрезки

    звонок = {**ЗАПИСАННАЯ, "pbx_uid": "2.1"}
    мосты = [{"ts": НАЧАЛО + 3, "kind": "enter", "uniqueid": "2.1", "bridge": "x", "agent": "101"},
             {"ts": НАЧАЛО + 40, "kind": "leave", "uniqueid": "2.1", "bridge": "x", "agent": "101"}]
    по_мостам = отрезки.окна_по_мостам(звонок, мосты, ноль=НАЧАЛО + 3, длительность=95)
    assert по_мостам == {"101": [(0.0, 37.0)]}


@pytest.fixture()
def хаб(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    app.state.hub.приложение = app
    yield app.state.hub


def _разговор(db, job_id: str, связь: str = "1789000000.1") -> None:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": "sales",
                   "media_duration_s": 95.0})
    db.update_job(job_id, status="completed", text="…", finished_at=time.time())
    db.save_segments(job_id, РЕПЛИКИ)
    for н, нога_ in enumerate((ЗАПИСАННАЯ, ВТОРАЯ)):
        поля = {к: v for к, v in нога_.items() if к not in ("uniqueid", "station", "linkedid")}
        db.save_call(f"pbx:{job_id}.{н}", job_id=job_id if н == 0 else None, owner="sales",
                     station="pbx", answered=1, linkedid=связь,
                     pbx_uid="1.1" if н == 0 else f"{job_id}.{н}", **поля)


def test_мосты_пересчитывают_отрезки_и_видны_в_карточке(хаб):
    from fastapi.testclient import TestClient

    db = хаб.db
    _разговор(db, "b67a")
    хаб.content.разложить_переводы()
    по_cdr = {с["agent"]: dict(с) for с in db.query(
        "SELECT * FROM agent_segments WHERE job_id='b67a'")}
    assert по_cdr["102"]["replicas"] == 3 and по_cdr["102"]["bridges"] == 0
    # Мосты пришли — запись снова в очереди на раскладку, и 102 теряет
    # реплику консультации.
    assert db.bridge_events_add("pbx", _мосты()) == len(_мосты())
    assert [с["job_id"] for с in db.jobs_to_split()] == ["b67a"]
    assert хаб.content.разложить_переводы() == 1
    по_мостам = {с["agent"]: dict(с) for с in db.query(
        "SELECT * FROM agent_segments WHERE job_id='b67a'")}
    assert по_мостам["102"]["replicas"] == 2 and по_мостам["101"]["replicas"] == 4
    assert по_мостам["102"]["bridges"] == len(_мосты())
    assert db.jobs_to_split() == []
    with TestClient(хаб.приложение) as клиент:
        ответ = клиент.get("/api/jobs/b67a/agents")
    assert ответ.status_code == 200, ответ.text
    assert "мост" in ответ.json()["by_agent"]["note"]


def test_мосты_разговоров_без_перевода_не_хранятся_долго(хаб):
    db = хаб.db
    _разговор(db, "b67b", связь="L-transfer")
    db.create_job({"id": "b67c", "filename": "один.wav"})
    db.save_call("pbx:one", job_id="b67c", station="pbx", answered=1, linkedid="L-single",
                 agent="101", billsec=30, duration=35, started_at=НАЧАЛО)
    давно = [{**м, "linkedid": связь} for связь in ("L-transfer", "L-single") for м in _мосты()]
    db.bridge_events_add("pbx", давно)
    свежие = [{**м, "linkedid": "L-new", "ts": time.time()} for м in _мосты()]
    db.bridge_events_add("pbx", свежие)
    db.bridge_events_prune(older_than=НАЧАЛО + 1000)
    осталось = {с["linkedid"] for с in db.query("SELECT DISTINCT linkedid FROM bridge_events")}
    assert осталось == {"L-transfer", "L-new"}
    # Срок хранения журнала звонков — и для мостов.
    db.forget_calls(before=time.time() - 60)
    assert {с["linkedid"] for с in db.query("SELECT DISTINCT linkedid FROM bridge_events")} \
        == {"L-new"}


def test_импорт_ami_сохраняет_мосты(tmp_path):
    from asrhub.db import Database
    from asrhub.telephony.importer import Импортёр
    from asrhub.telephony.stations import _станция_из

    class Клиент:
        живые: list = []

        def __init__(self, *_, события="", оставлять=None, **__):
            self.события, self.оставлять = события, оставлять
            self.очередь = [dict(с) for с in СОБЫТИЯ_AMI]
            Клиент.живые.append(self)

        def connect(self):
            return "ok"

        def close(self):
            pass

        def пакет(self, timeout=None):
            return self.очередь.pop(0) if self.очередь else {}

    import asrhub.telephony.importer as M
    настоящий, M.AMIClient = M.AMIClient, Клиент
    try:
        db = Database(tmp_path / "asrhub.db")
        станция = _станция_из({"name": "АТС", "source": "ami", "username": "a", "secret": "b"}, 0)
        импорт = Импортёр(db, станция, None, None)
        импорт._из_ami(5)
        assert {"bridgeenter", "bridgeleave"} <= Клиент.живые[-1].оставлять
        события = db.bridge_events({"station": импорт.id, "linkedid": "1789000000.1"})
        assert len(события) == len(СОБЫТИЯ_AMI)
        assert {с["agent"] for с in события} == {"provider", "101", "102"}
    finally:
        M.AMIClient = настоящий


def test_раздел_диалплана_просит_события_мостов_в_cel():
    from asrhub.telephony import dialplan_config as К

    cel = {ф["id"]: ф for ф in К.собрать(К.параметры(station="main"))["files"]}["cel"]
    assert "BRIDGE_ENTER,BRIDGE_EXIT" in cel["text"]
