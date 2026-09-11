"""Аналитика по сотрудникам: разрезы, знаменатели, сравнение с командой.

Показатель по человеку читают, чтобы с ним поговорить. Поэтому проверки
здесь в основном про то, чтобы число не обманывало: чтобы «мало данных»
было видно, чтобы чужие разговоры не попали в свой отчёт, а предсказанный
NPS не выдавался за названный.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.insights import Insights, _индекс_nps


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def запись(db: Database, job_id: str, *, оператор: str, когда: float,
           владелец: str = "telephony", очередь: str = "продажи",
           станция: str = "pbx", **показатели) -> None:
    """Разобранный разговор со звонком — как он приезжает с АТС."""
    db.execute(
        "INSERT INTO jobs (id,status,created_at,updated_at,owner,media_duration_s,text) "
        "VALUES (?,?,?,?,?,?,?)",
        (job_id, "completed", когда, когда + 60, владелец, 180.0, "расшифровка"))
    свод = {"version": 6, "sentiment": 0.1, "agent_speaker": "SPEAKER_00",
            "agent_score": 75, "compliance": 0.7, "speakers": 2}
    свод.update(показатели)
    db.save_content(job_id, свод, [])
    db.save_call(uniqueid=f"u-{job_id}", job_id=job_id, agent=оператор,
                 queue=очередь, station=станция, direction="входящий",
                 started_at=когда, duration=190, billsec=180, answered=1,
                 owner=владелец)


@pytest.fixture()
def стенд(tmp_path):
    db = база(tmp_path)
    сейчас = time.time()
    for н in range(12):
        запись(db, f"ivan{н}", оператор="Иванов А.", когда=сейчас - н * 3600,
               stress=60, clarity=40, accuracy=50, politeness=70, mood=-0.4,
               nps=4, nps_group="критик", nps_stated=1 if н < 3 else 0,
               diminutive_rate=0.02, filler_rate=0.05)
    for н in range(10):
        запись(db, f"petr{н}", оператор="Петрова М.", когда=сейчас - н * 3600,
               stress=20, clarity=85, accuracy=88, politeness=95, mood=0.4,
               nps=9, nps_group="промоутер", nps_stated=0,
               diminutive_rate=0.001, filler_rate=0.004)
    # Два разговора чужого отдела: в свой отчёт они попадать не должны.
    for н in range(6):
        запись(db, f"alien{н}", оператор="Чужой Ч.", когда=сейчас - н * 3600,
               владелец="другой-отдел", stress=99, clarity=1, nps=0,
               nps_group="критик")
    return db, Insights(db)


# ---------------------------------------------------------------------------
# Разрезы
# ---------------------------------------------------------------------------

def test_сотрудник_берётся_из_журнала_атс(стенд):
    """«Говорящий 1» — метка внутри записи, а не человек.

    По ней все разговоры всех операторов слипаются в одну строку: разбор
    называет первого заговорившего одинаково в каждой записи.
    """
    db, insights = стенд
    по_людям = insights.employees("agent", "week")
    имена = {ч["key"] for ч in по_людям["items"]}
    assert {"Иванов А.", "Петрова М."} <= имена
    по_говорящему = insights.employees("speaker", "week")
    assert len(по_говорящему["items"]) == 1, "все операторы слились в одного"


def test_чужие_разговоры_не_попадают_в_отчёт(стенд):
    db, insights = стенд
    свои = insights.employees("agent", "week", owner="telephony")
    assert "Чужой Ч." not in {ч["key"] for ч in свои["items"]}
    assert свои["team"]["records"] == 22
    # И наоборот: у чужого отдела свой отчёт.
    чужие = insights.employees("agent", "week", owner="другой-отдел")
    assert {ч["key"] for ч in чужие["items"]} == {"Чужой Ч."}


def test_мало_данных_помечается_а_не_прячется(стенд, tmp_path):
    """У сотрудника с тремя разговорами средний балл 92 — это «мало данных».

    Увидеть это можно только рядом с ним самим, поэтому человека не
    прячут, а помечают.
    """
    db, insights = стенд
    запись(db, "new1", оператор="Новичок Н.", когда=time.time() - 60)
    люди = {ч["key"]: ч for ч in insights.employees("agent", "week")["items"]}
    assert "Новичок Н." in люди
    assert люди["Новичок Н."]["sparse"] is True
    assert люди["Иванов А."]["sparse"] is False


def test_к_показателям_разбора_добавляются_телефонные(стенд):
    """Балл без числа звонков обманывает: у трёх разговоров он ни о чём."""
    db, insights = стенд
    люди = {ч["key"]: ч for ч in insights.employees("agent", "week")["items"]}
    иван = люди["Иванов А."]
    assert иван["calls"] == 12
    assert иван["talk_s"] == 12 * 180
    assert иван["answered_share"] == 100.0
    assert иван["avg_call_s"] == 180.0


def test_прошлый_период_приезжает_вместе_с_текущим(стенд):
    """Показатель без «было столько» — это точка без направления."""
    db, insights = стенд
    сейчас = time.time()
    for н in range(6):
        запись(db, f"old{н}", оператор="Иванов А.", когда=сейчас - (8 + н) * 86400,
               stress=30, clarity=70)
    люди = {ч["key"]: ч for ч in insights.employees("agent", "week")["items"]}
    прошлое = люди["Иванов А."]["previous"]
    assert прошлое["records"] == 6
    assert прошлое["stress"] == 30.0 and прошлое["clarity"] == 70.0


def test_неизвестный_разрез_отвергается(стенд):
    db, insights = стенд
    with pytest.raises(ValueError):
        insights.employees("j.owner, (SELECT 1)", "week")


# ---------------------------------------------------------------------------
# Показатели
# ---------------------------------------------------------------------------

def test_индекс_nps_это_промоутеры_минус_критики():
    # Классика Райхельда: нейтралы в формулу не входят, их вес нулевой.
    assert _индекс_nps(6, 2, 10) == 40
    assert _индекс_nps(0, 10, 10) == -100
    assert _индекс_nps(10, 0, 10) == 100
    # Пусто, когда считать не по чему: ноль здесь читался бы как
    # «промоутеров и критиков поровну», а это другое утверждение.
    assert _индекс_nps(0, 0, 0) is None


def test_названный_балл_не_смешивается_с_предсказанным(стенд):
    """Выдать предсказание за опрос — значит соврать в отчёте."""
    db, insights = стенд
    свод = insights.summary("week", owner="telephony")
    assert свод["nps_checked"] == 22
    assert свод["nps_stated_count"] == 3, "названных баллов ровно три"
    assert свод["nps_stated_avg"] == 4.0, "среднее по названным — только по ним"
    assert свод["nps_stated_index"] == -100, "все три названных — от критика"
    assert свод["nps_index"] != свод["nps_stated_index"]


def test_знаменатель_идёт_рядом_с_каждым_средним(стенд):
    """Средняя понятность по трём записям из тысячи — не показатель отдела."""
    db, insights = стенд
    свод = insights.summary("week", owner="telephony")
    for показатель, знаменатель in (("stress", "stress_checked"),
                                    ("clarity", "clarity_checked"),
                                    ("accuracy", "accuracy_checked"),
                                    ("politeness", "politeness_checked")):
        assert свод[показатель] is not None, показатель
        assert свод[знаменатель] == 22, знаменатель


def test_карточка_сравнивает_сотрудника_с_командой(стенд):
    db, insights = стенд
    карточка = insights.agent_card("Иванов А.", by="agent", period="week",
                                   owner="telephony")
    по_ключу = {с["key"]: с for с in карточка["compare"]}
    assert по_ключу["stress"]["agent"] == 60
    assert по_ключу["stress"]["team"] < 60
    # У напряжения «больше» значит «хуже»: вердикт обязан это учитывать.
    assert по_ключу["stress"]["verdict"] == "worse"
    assert по_ключу["clarity"]["verdict"] == "worse"
    assert по_ключу["clarity"]["agent"] < по_ключу["clarity"]["team"]


def test_ход_по_неделям_несёт_новые_показатели(стенд):
    db, insights = стенд
    карточка = insights.agent_card("Петрова М.", by="agent", period="month",
                                   owner="telephony")
    непустые = [т for т in карточка["timeline"] if т["records"]]
    assert непустые, "история пуста"
    for ключ in ("stress", "clarity", "accuracy", "politeness", "nps", "mood"):
        assert ключ in непустые[0], ключ
    assert непустые[-1]["clarity"] == 85.0


def test_разрез_по_очереди_и_станции_тоже_работает(стенд):
    """Сравнивать иногда нужно не людей, а участки."""
    db, insights = стенд
    очереди = insights.employees("queue", "week", owner="telephony")
    assert [ч["key"] for ч in очереди["items"]] == ["продажи"]
    станции = insights.employees("station", "week", owner="telephony")
    assert [ч["key"] for ч in станции["items"]] == ["pbx"]
