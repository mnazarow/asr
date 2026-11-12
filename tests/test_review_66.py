"""Заход 66: отрезки участников разговора с переводом — в разрезах по сотрудникам.

В 3.1.37 отрезки записи по участникам считались только для карточки записи,
а «Голосовая аналитика» по сотрудникам отдавала весь разговор добавочному
записанной ноги. Здесь фоновый проход раскладывает записи с переводом по
участникам (`agent_segments`), поздняя нога перевода пересчитывает запись,
а отчёт за период показывает по каждому добавочному его участие.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path

import pytest
from test_review_65 import ВТОРАЯ, ЗАПИСАННАЯ, РЕПЛИКИ, нога


@pytest.fixture()
def хаб(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    yield app.state.hub


def _перевод(db, job_id: str, *ноги: dict, станция: str = "pbx") -> None:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": "sales",
                   "media_duration_s": 95.0})
    db.update_job(job_id, status="completed", text="…", finished_at=time.time())
    db.save_segments(job_id, РЕПЛИКИ)
    for н, нога_ in enumerate(ноги):
        поля = {к: v for к, v in нога_.items() if к not in ("uniqueid", "station", "linkedid")}
        db.save_call(f"{job_id}:{нога_['uniqueid']}", job_id=job_id if н == 0 else None,
                     owner="sales", station=станция, answered=1,
                     linkedid=f"{job_id}.link", **поля)


def _отрезки(db, job_id: str) -> dict[str, dict]:
    return {с["agent"]: dict(с) for с in db.query(
        "SELECT * FROM agent_segments WHERE job_id=?", (job_id,))}


def test_схема_39_и_таблица_отрезков(хаб):
    from asrhub.db import SCHEMA_VERSION

    assert SCHEMA_VERSION == 39
    колонки = {с["name"] for с in хаб.db.query("PRAGMA table_info(agent_segments)")}
    assert {"job_id", "agent", "talk_s", "words", "sentiment", "recorded", "legs"} <= колонки


def test_фоновый_проход_раскладывает_и_не_повторяется(хаб):
    db = хаб.db
    _перевод(db, "t66a", ЗАПИСАННАЯ, ВТОРАЯ)
    assert [с["job_id"] for с in db.jobs_to_split()] == ["t66a"]
    assert хаб.content.разложить_переводы() == 1
    отрезки = _отрезки(db, "t66a")
    assert set(отрезки) == {"101", "102"}
    assert отрезки["101"]["recorded"] == 1 and отрезки["102"]["recorded"] == 0
    assert отрезки["101"]["talk_s"] == 47.0 and отрезки["102"]["talk_s"] == 34.0
    assert отрезки["102"]["sentiment"] > отрезки["101"]["sentiment"]
    assert {с["legs"] for с in отрезки.values()} == {2}
    # Ничего не изменилось — второй проход запись не трогает.
    assert db.jobs_to_split() == [] and хаб.content.разложить_переводы() == 0


def test_поздняя_нога_перевода_пересчитывает(хаб):
    db = хаб.db
    _перевод(db, "t66b", ЗАПИСАННАЯ, ВТОРАЯ)
    хаб.content.разложить_переводы()
    # Третий участник пришёл отдельной строкой CDR позже: 102 перевёл на 103.
    третья = нога("pbx:1.3", "103", 80, 16, 15, порядок=3)
    поля = {к: v for к, v in третья.items() if к not in ("uniqueid", "station", "linkedid")}
    db.save_call("t66b:pbx:1.3", owner="sales", station="pbx", answered=1,
                 linkedid="t66b.link", **поля)
    assert [с["job_id"] for с in db.jobs_to_split()] == ["t66b"]
    assert хаб.content.разложить_переводы() == 1
    отрезки = _отрезки(db, "t66b")
    assert set(отрезки) == {"101", "102", "103"}
    assert {с["legs"] for с in отрезки.values()} == {3}


def test_нечего_делить_помечается_и_в_разрезы_не_идёт(хаб):
    db = хаб.db
    # Вторая нога ответила уже после конца записи: в окна не годится.
    миг = нога("pbx:1.2", "102", 100, 5, 3, порядок=2)
    _перевод(db, "t66c", ЗАПИСАННАЯ, миг)
    assert [с["job_id"] for с in db.jobs_to_split()] == ["t66c"]
    assert хаб.content.разложить_переводы() == 1
    assert set(_отрезки(db, "t66c")) == {""}
    assert db.jobs_to_split() == []
    assert db.agent_segments_by_agent("1=1", []) == []


def test_один_участник_и_незавершённое_не_в_очереди(хаб):
    db = хаб.db
    тот_же = нога("pbx:1.2", "101", 58, 42, 40, порядок=2)
    _перевод(db, "t66d", ЗАПИСАННАЯ, тот_же)
    _перевод(db, "t66e", ЗАПИСАННАЯ, ВТОРАЯ)
    db.update_job("t66e", status="processing")
    assert db.jobs_to_split() == []


def test_удаление_записи_убирает_отрезки(хаб):
    db = хаб.db
    _перевод(db, "t66f", ЗАПИСАННАЯ, ВТОРАЯ)
    хаб.content.разложить_переводы()
    assert _отрезки(db, "t66f")
    db.delete_job("t66f")
    assert _отрезки(db, "t66f") == {}
    # Запись, которой нет, — отрезки не пишутся.
    db.save_agent_segments("нет-такой", [{"agent": "101"}])
    assert _отрезки(db, "нет-такой") == {}


def test_отчёт_за_период_показывает_участие(хаб):
    from asrhub import voice_period as В

    db = хаб.db
    db.employee_save({"last_name": "Сидоров", "first_name": "Пётр", "department": "Сервис",
                      "phone_ext": "102", "source": "т", "external_id": "102"})
    _перевод(db, "t66g", ЗАПИСАННАЯ, ВТОРАЯ)
    _перевод(db, "t66h", ЗАПИСАННАЯ, ВТОРАЯ)
    хаб.content.разложить_переводы()
    сейчас = time.time()
    сегодня = dt.date.fromtimestamp(сейчас)

    def отчёт(**отбор):
        о = В.Отчёт(db, "day", сегодня, В.Отбор(**отбор), сейчас=сейчас + 1).посчитать()
        return {с["agent"]: с for с in о["agents"]}

    люди = отчёт()
    assert люди["101"]["completed"] == 2 and люди["101"]["split_records"] == 2
    assert люди["101"]["split_received"] == 0
    # 102 записанных разговоров не имеет, но участвовал в двух — принял перевод.
    assert люди["102"]["completed"] == 0
    assert (люди["102"]["split_records"], люди["102"]["split_received"]) == (2, 2)
    assert люди["102"]["split_talk_hours"] == round(68 / 3600, 2)
    assert люди["102"]["split_negative_share"] == 0.0
    # Отбор по добавочному и по отделу — по участнику, а не по записанной ноге.
    assert отчёт(agent="102")["102"]["split_records"] == 2
    отдел = отчёт(department="Сервис")
    assert отдел["102"]["split_records"] == 2 and отдел.get("101", {}).get("split_records", 0) == 0


def test_свод_голосовой_аналитики_показывает_участие(хаб):
    from asrhub.api import routes_llm

    db = хаб.db
    _перевод(db, "t66i", ЗАПИСАННАЯ, ВТОРАЯ)
    хаб.content.разложить_переводы()
    свод = routes_llm._по_добавочным(хаб, time.time() - 3600, None)
    люди = {с["agent"]: с for с in свод["agents"]}
    assert люди["101"]["records"] == 1 and люди["101"]["split_records"] == 1
    # 102 записанных разговоров нет, но в разрезе он есть — со своим участием.
    assert люди["102"]["records"] == 0
    assert (люди["102"]["split_records"], люди["102"]["split_received"]) == (1, 1)
    assert люди["102"]["split_negative_share"] == 0.0


# --- события перевода из AMI -------------------------------------------------------

СЛЕПОЙ = {"Event": "BlindTransfer", "Result": "Success", "Timestamp": "1789000065.25",
          "TransfererChannel": "PJSIP/101-0000002a", "TransfererLinkedid": "1789000000.1",
          "TransfereeChannel": "PJSIP/trunk-0000002b", "TransfereeLinkedid": "1789000000.1",
          "Extension": "102", "Context": "from-internal", "IsExternal": "No"}
С_ПРЕДУПРЕЖДЕНИЕМ = {
    "Event": "AttendedTransfer", "Result": "Success", "Timestamp": "1789000065",
    "OrigTransfererChannel": "PJSIP/101-0000002a", "OrigTransfererLinkedid": "1789000000.1",
    "SecondTransfererChannel": "PJSIP/101-0000002c", "SecondTransfererLinkedid": "1789000058.3",
    "TransfereeChannel": "PJSIP/trunk-0000002b", "TransfereeLinkedid": "1789000000.1",
    "TransferTargetChannel": "PJSIP/102-0000002d", "TransferTargetLinkedid": "1789000058.3",
    "DestType": "Bridge"}


def test_события_перевода_ami_в_строки():
    from asrhub.telephony import transfers as П

    слепой = П.из_события_ami(СЛЕПОЙ)
    assert слепой == {"ts": 1789000065.25, "linkedid": "1789000000.1", "kind": "blind",
                      "from_agent": "101", "to_agent": "102"}
    с_предупреждением = П.из_события_ami(С_ПРЕДУПРЕЖДЕНИЕМ)
    assert (с_предупреждением["kind"], с_предупреждением["from_agent"],
            с_предупреждением["to_agent"]) == ("attended", "101", "102")
    assert с_предупреждением["linkedid"] == "1789000000.1"      # разговор клиента
    # Неудачный перевод — не граница; чужое событие — не наше.
    assert П.из_события_ami({**СЛЕПОЙ, "Result": "Fail"}) is None
    assert П.из_события_ami({"Event": "Newchannel"}) is None
    assert П.из_события_ami({**СЛЕПОЙ, "TransfereeLinkedid": "", "TransfererLinkedid": ""}) is None
    # Перевод на городской номер: номер вне станции не хранится.
    наружу = П.из_события_ami({**СЛЕПОЙ, "Extension": "+79001234567"})
    assert наружу["to_agent"] == "" and П.из_события_ami({**СЛЕПОЙ, "Extension": "89001234567"
                                                          })["to_agent"] == ""


def test_фоновое_чтение_ami_оставляет_нужные_события():
    import socket

    from asrhub.telephony.asterisk import AMIClient

    свой, станция = socket.socketpair()
    клиент = AMIClient("127.0.0.1", события="cdr,call",
                       оставлять={"cdr", "blindtransfer", "attendedtransfer"})
    клиент._sock = свой
    try:
        клиент.читать_в_фоне()
        поток = "".join(
            f"Event: {имя}\r\nUniqueid: {н}\r\n\r\n"
            for н, имя in enumerate(["Newchannel", "Newstate", "BlindTransfer", "Hangup",
                                     "Cdr", "BridgeEnter", "AttendedTransfer"] * 50))
        станция.sendall(поток.encode())
        станция.sendall(b"Response: Success\r\nActionID: 1\r\n\r\n")
        пришло = []
        while len(пришло) < 150:
            пакет = клиент.пакет(timeout=5)
            assert пакет, "события не дошли"
            пришло.append(пакет["Event"])
        assert set(пришло) == {"BlindTransfer", "Cdr", "AttendedTransfer"}
        assert клиент.пакет(timeout=0.2) == {}
        assert клиент._ответы_фона, "ответ на действие не отброшен"
    finally:
        клиент._sock = None
        свой.close()
        станция.close()


def test_импорт_ami_сохраняет_переводы(tmp_path):
    from asrhub.db import Database
    from asrhub.telephony.importer import Импортёр
    from asrhub.telephony.stations import _станция_из

    class Клиент:
        живые: list = []

        def __init__(self, *_, события="", оставлять=None, **__):
            self.события, self.оставлять = события, оставлять
            self.очередь = [
                {"Event": "Newchannel"}, dict(СЛЕПОЙ), dict(С_ПРЕДУПРЕЖДЕНИЕМ),
                {**СЛЕПОЙ, "Result": "Fail", "Timestamp": "1789000070"}]
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
        assert станция.transfers is True
        имп = Импортёр(db, станция, None, None)
        имп._из_ami(5)
        клиент = Клиент.живые[-1]
        assert клиент.события == "cdr,agent,call"
        assert {"cdr", "blindtransfer", "attendedtransfer", "agentconnect"} <= клиент.оставлять
        переводы = db.call_transfers({"station": имп.id, "linkedid": "1789000000.1"})
        assert [(п["kind"], п["from_agent"], п["to_agent"]) for п in переводы] == [
            ("attended", "101", "102"), ("blind", "101", "102")]
        # Выключенные события перевода — класса call не просим.
        выкл = _станция_из({"name": "АТС", "source": "ami", "transfers": False}, 0)
        assert Импортёр(db, выкл, None, None)._классы_событий_ami() == "cdr,agent"
        assert Импортёр._события_ami_нужны("cdr,agent") is None
        # Срок хранения — тот же, что у журнала звонков.
        db.forget_calls(before=1789000065.1)
        assert [п["kind"] for п in db.call_transfers(
            {"station": имп.id, "linkedid": "1789000000.1"})] == ["blind"]
    finally:
        M.AMIClient = настоящий


def test_граница_перевода_по_событию(хаб):
    from asrhub.telephony import segments as отрезки

    # Перевод с предупреждением завершён на 60-й секунде записи (ноль —
    # ответ записанной ноги, НАЧАЛО + 5): до него клиент говорил с 101,
    # хотя 102 снял трубку на консультации на 55-й.
    перевод = {"ts": ЗАПИСАННАЯ["started_at"] + 65, "kind": "attended",
               "from_agent": "101", "to_agent": "102"}
    окна = отрезки.окна(ЗАПИСАННАЯ, [ВТОРАЯ], длительность=95, переводы=[перевод])
    assert [(о["agent"], о["from_s"], о["to_s"]) for о in окна] == [
        ("101", 0.0, 60.0), ("102", 60.0, 95.0)]
    итог = отрезки.по_участникам(ЗАПИСАННАЯ, [ВТОРАЯ], РЕПЛИКИ, длительность=95,
                                 переводы=[перевод])
    # «Коллега, клиент по доставке» (56–58 с) — снова 101: это он говорил.
    assert [(о["agent"], о["replicas"]) for о in итог["segments"]] == [("101", 4), ("102", 2)]
    assert "событию AMI" in итог["note"]
    assert итог["transfers"] == [{"at_s": 60.0, "kind": "attended", "from_agent": "101",
                                  "to_agent": "102"}]
    # Без события — как раньше: общее окно отдаётся подключившемуся позже.
    без = отрезки.по_участникам(ЗАПИСАННАЯ, [ВТОРАЯ], РЕПЛИКИ, длительность=95)
    assert [(о["agent"], о["replicas"]) for о in без["segments"]] == [("101", 3), ("102", 3)]


def test_событие_перевода_после_подсчёта_пересчитывает(хаб):
    db = хаб.db
    _перевод(db, "t66j", ЗАПИСАННАЯ, ВТОРАЯ)
    хаб.content.разложить_переводы()
    assert _отрезки(db, "t66j")["101"]["replicas"] == 3
    db.call_transfers_add("pbx", [{"ts": ЗАПИСАННАЯ["started_at"] + 65, "linkedid": "t66j.link",
                                   "kind": "attended", "from_agent": "101", "to_agent": "102"}])
    assert [с["job_id"] for с in db.jobs_to_split()] == ["t66j"]
    assert хаб.content.разложить_переводы() == 1
    отрезки = _отрезки(db, "t66j")
    assert отрезки["101"]["replicas"] == 4 and отрезки["101"]["transfers"] == 1
    assert db.jobs_to_split() == []


# --- журнал CEL для станций без AMI ---------------------------------------------------

def _строка_cel(вид: str, время: str, канал: str, связь: str, доп: str) -> str:
    import csv
    import io

    буфер = io.StringIO()
    csv.writer(буфер, quoting=csv.QUOTE_ALL, lineterminator="\n").writerow(
        [вид, время, канал, "1789000000.5", связь, "s", доп])
    return буфер.getvalue()


def test_строки_cel_в_переводы():
    import datetime as dt

    from asrhub.telephony import transfers as П

    слепой = П.из_строки_cel(["BLINDTRANSFER", "1789000065.250000", "PJSIP/101-0000002a",
                              "1789000000.5", "1789000000.1", "s",
                              '{"extension":"102","context":"from-internal","bridge_id":"b1"}'])
    assert слепой == {"ts": 1789000065.25, "linkedid": "1789000000.1", "kind": "blind",
                      "from_agent": "101", "to_agent": "102"}
    с_предупреждением = П.из_строки_cel([
        "ATTENDEDTRANSFER", "2026-09-16 12:00:05", "PJSIP/101-0000002a", "u", "1789000000.1",
        "s", '{"bridge1_id":"b1","channel2_name":"PJSIP/101-0000002c","bridge2_id":"b2",'
             '"transferee_channel_name":"PJSIP/trunk-1","transfer_target_channel_name":'
             '"PJSIP/102-0000002d"}'], пояс=dt.timezone(dt.timedelta(hours=3)))
    assert с_предупреждением["to_agent"] == "102" and с_предупреждением["kind"] == "attended"
    assert с_предупреждением["ts"] == dt.datetime(2026, 9, 16, 9, 0, 5,
                                                  tzinfo=dt.timezone.utc).timestamp()
    # Перевод в приложение — без адресата; чужое событие и порченая строка — не наши.
    в_приложение = П.из_строки_cel(["ATTENDEDTRANSFER", "1789000065", "PJSIP/101-1", "u",
                                    "L", "s", '{"bridge1_id":"b1","app":"Voicemail"}'])
    assert в_приложение["to_agent"] == ""
    assert П.из_строки_cel(["HANGUP", "1789000065", "PJSIP/101-1", "u", "L", "s", ""]) is None
    assert П.из_строки_cel(["BLINDTRANSFER", "вчера", "PJSIP/101-1", "u", "L", "s", ""]) is None
    assert П.из_строки_cel(["BLINDTRANSFER"]) is None
    assert П.из_строки_cel(["BLINDTRANSFER", "1789000065", "PJSIP/101-1", "u", "L", "s",
                            "не json"])["to_agent"] == ""


def test_импорт_читает_журнал_cel_с_позиции(tmp_path):
    from asrhub.db import Database
    from asrhub.telephony.importer import Импортёр
    from asrhub.telephony.stations import _станция_из

    журнал = tmp_path / "asrhub-cel.csv"
    журнал.write_text(
        _строка_cel("CHAN_START", "1789000000", "PJSIP/101-1", "L1", "")
        + _строка_cel("BLINDTRANSFER", "1789000065", "PJSIP/101-1", "L1", '{"extension":"102"}')
        + '"BLINDTRANSFER","17890', encoding="utf-8")         # хвост без перевода строки
    db = Database(tmp_path / "asrhub.db")
    станция = _станция_из({"name": "АТС", "source": "cdr_csv", "cel_file": str(журнал)}, 0)
    импорт = Импортёр(db, станция, None, None)
    assert импорт._переводы_cel() == 1
    assert импорт._переводы_cel() == 0                         # позиция запомнена
    with журнал.open("a", encoding="utf-8") as файл:
        файл.write('00070","PJSIP/102-2","u","L1","s","{""extension"":""103""}"\n')
    assert импорт._переводы_cel() == 1
    переводы = db.call_transfers({"station": импорт.id, "linkedid": "L1"})
    assert [(п["from_agent"], п["to_agent"]) for п in переводы] == [("101", "102"),
                                                                  ("102", "103")]
    # Повёрнутый журнал (файл короче позиции) читается с начала, повтор не задваивается.
    журнал.write_text(_строка_cel("BLINDTRANSFER", "1789000065", "PJSIP/101-1", "L1",
                                  '{"extension":"102"}'), encoding="utf-8")
    assert импорт._переводы_cel() == 0
    assert len(db.call_transfers({"station": импорт.id, "linkedid": "L1"})) == 2


def test_раздел_диалплана_даёт_журнал_cel():
    from asrhub.telephony import dialplan_config as К
    from asrhub.telephony.transfers import ОТОБРАЖЕНИЕ_CEL

    файлы = {ф["id"]: ф for ф in К.собрать(К.параметры(station="main"))["files"]}
    cel = файлы["cel"]
    assert cel["path"] == "/etc/asterisk/cel_custom.conf"
    assert ОТОБРАЖЕНИЕ_CEL in cel["text"] and "BLINDTRANSFER,ATTENDEDTRANSFER" in cel["text"]
    # Порядок полей отображения — тот, что читает разбор.
    assert ОТОБРАЖЕНИЕ_CEL.index("eventtype") < ОТОБРАЖЕНИЕ_CEL.index("eventtime") \
        < ОТОБРАЖЕНИЕ_CEL.index("channame") < ОТОБРАЖЕНИЕ_CEL.index("linkedid") \
        < ОТОБРАЖЕНИЕ_CEL.index("eventextra")


from test_agent_api import здравствуй  # noqa: E402
from test_agent_api import сервер as агентский_сервер  # noqa: E402,F401


def test_агент_присылает_журнал_cel(агентский_сервер):  # noqa: F811
    ответ = здравствуй(агентский_сервер)
    строки = [_строка_cel("BLINDTRANSFER", "1789000065", "PJSIP/101-1", "L7",
                          '{"extension":"102"}').strip(),
              _строка_cel("HANGUP", "1789000099", "PJSIP/101-1", "L7", "").strip()]
    итог = агентский_сервер.post("/api/telephony/agent/cel", headers={"X-API-Key": "ah_admin"},
                                 json={"agent_id": ответ["agent_id"], "lines": строки})
    assert итог.status_code == 200, итог.text
    assert итог.json() == {"received": 2, "transfers": 1, "added": 1}
    слишком = агентский_сервер.post("/api/telephony/agent/cel",
                                    headers={"X-API-Key": "ah_admin"},
                                    json={"agent_id": ответ["agent_id"], "lines": "не список"})
    assert слишком.status_code == 400


def test_агент_читает_журнал_cel_и_шлёт_строки(tmp_path):
    import importlib.util
    import sys

    корень = Path(__file__).resolve().parent.parent
    спец = importlib.util.spec_from_file_location("asrhub_agent66", корень / "agent" / "asrhub-agent.py")
    агент_мод = importlib.util.module_from_spec(спец)
    sys.modules["asrhub_agent66"] = агент_мод
    спец.loader.exec_module(агент_мод)

    журнал = tmp_path / "asrhub-cel.csv"
    журнал.write_text(_строка_cel("BLINDTRANSFER", "1789000065", "PJSIP/101-1", "L7",
                                  '{"extension":"102"}'), encoding="utf-8")
    отправлено: list[tuple[str, dict]] = []

    class Клиент:
        def _запрос(self, метод, адрес, тело, *_, **__):
            import json
            отправлено.append((адрес, json.loads(тело)))
            return {}

    class Состояние:
        данные: dict = {}

        def сохранить(self):
            pass

    агент = агент_мод.Агент.__new__(агент_мод.Агент)
    агент.параметры = {"cel_file": str(журнал), "queue_log": ""}
    агент.пробно, агент.agent_id = False, "a" * 16
    агент.клиент, агент.состояние = Клиент(), Состояние()
    assert агент.переводы() == 1
    assert отправлено[0][0] == "/api/telephony/agent/cel"
    assert отправлено[0][1]["lines"][0].startswith('"BLINDTRANSFER"')
    assert агент.переводы() == 0                               # позиция запомнена
    assert агент.состояние.данные["cel_file"]["offset"] == журнал.stat().st_size
