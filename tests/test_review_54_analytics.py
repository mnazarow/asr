"""Заход 54, участок content-analytics: аналитика, тренды и сотрудники в отчётах.

Каждая проверка падает на коде до захода и проходит после (отчёт
10-analytics и раздел аналитики из 09-content).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from asrhub.analytics import Analytics
from asrhub.db import Database
from asrhub.trends import Trends


def _база(tmp_path: Path) -> Database:
    return Database(tmp_path / "a.db")


def _задание(db: Database, номер: str, *, когда: float, статус: str = "completed",
             владелец: str = "", **поля: Any) -> None:
    db.create_job({"id": номер, "filename": f"{номер}.wav", "owner": владелец,
                   "engine": "demo", "model": "demo-simulator", "language": "ru",
                   "source": "api", "media_duration_s": поля.pop("media_duration_s", 60.0)})
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, номер))
    if статус != "queued":
        db.update_job(номер, status=статус, finished_at=когда + 5, **поля)


# ---------------------------------------------------------------------------
# Аналитика: выборка без обрезки, знаменатели, чужое
# ---------------------------------------------------------------------------


def test_счёт_заданий_не_режется_пределом_выборки(tmp_path, monkeypatch):
    """Счётные разрезы шли по выборке `list_jobs(limit=100000)`: всё, что
    сверх предела, молча пропадало. Предел здесь сжат до 50 подменой — на
    120 заданиях старый счёт давал 50."""
    db = _база(tmp_path)
    сейчас = time.time()
    for n in range(120):
        _задание(db, f"j{n:03d}", когда=сейчас - 60 - n, processing_time_s=1.0, rtf=0.1)
    настоящий = db.list_jobs

    def с_пределом(*a: Any, **k: Any) -> Any:
        k["limit"] = min(int(k.get("limit") or 50), 50)
        return настоящий(*a, **k)

    monkeypatch.setattr(db, "list_jobs", с_пределом)
    свод = Analytics(db).overview("day")
    assert свод["jobs"]["total"] == 120
    assert свод["jobs"]["completed"] == 120
    db.close()


def test_доля_успеха_считается_по_завершённым(tmp_path):
    """Стоящее в очереди задание не неудача: доля успеха 3 из 3, а не 3 из 4."""
    db = _база(tmp_path)
    сейчас = time.time()
    for n in range(3):
        _задание(db, f"ok{n}", когда=сейчас - 100 - n)
    _задание(db, "q", когда=сейчас - 50, статус="queued")
    assert Analytics(db).overview("day")["jobs"]["success_rate"] == 1.0
    db.close()


def test_самые_медленные_без_клонов_из_кеша(tmp_path):
    """Клон из кеша несёт чужой RTF и нулевую обработку."""
    db = _база(tmp_path)
    сейчас = time.time()
    _задание(db, "src", когда=сейчас - 300, rtf=0.2, processing_time_s=12.0)
    _задание(db, "clone", когда=сейчас - 100, rtf=0.9, processing_time_s=0.0,
             cached_from="src")
    номера = [з["id"] for з in Analytics(db).slowest("day")]
    assert "clone" not in номера and "src" in номера
    db.close()


def test_замеры_сервера_ключу_подразделения_не_отдаются(tmp_path):
    db = _база(tmp_path)
    _задание(db, "a1", когда=time.time() - 100, владелец="анна")
    ряды = Analytics(db).timeseries("day", owner="анна")
    # Ряды железа у обычного ключа пусты (ключ остаётся — интерфейс его ждёт).
    assert not any((ряды.get("system") or {}).values())
    db.close()


# ---------------------------------------------------------------------------
# Тренды: календарные корзины, неполная последняя, нули, знаменатели
# ---------------------------------------------------------------------------


def test_корзины_трендов_по_местному_календарю(tmp_path):
    """Корзины шли от «сейчас»: сутки «с 15:42 до 15:42» под датой."""
    db = _база(tmp_path)
    _задание(db, "a", когда=time.time() - 3600)
    свод = Trends(db).series("month", bucket="day", metrics=["jobs"], compare=False)
    for граница in свод["buckets"]:
        местное = time.localtime(граница)
        assert (местное.tm_hour, местное.tm_min, местное.tm_sec) == (0, 0, 0)
    assert свод["partial_last"] is True
    db.close()


def test_неполная_последняя_корзина_не_портит_последнее_и_наклон(tmp_path):
    """Ровный поток: по 10 заданий в сутки, сегодня — только 1. «Последнее»
    — вчерашние 10, а не сегодняшний обрубок, и наклон не уходит вниз."""
    db = _база(tmp_path)
    полночь = time.mktime(time.localtime(time.time())[:3] + (0, 0, 0, 0, 0, -1))
    n = 0
    for день in range(1, 20):
        for к in range(10):
            _задание(db, f"d{n}", когда=полночь - день * 86400 + 3600 + к * 60)
            n += 1
    _задание(db, "today", когда=min(time.time() - 1, полночь + 1))
    итог = Trends(db).series("month", bucket="day", metrics=["jobs"],
                             compare=False)["summary"][0]
    assert итог["last"] == 10
    assert итог["slope"] in (None, 0) or abs(итог["slope"]) < 0.01
    db.close()


def test_пустая_корзина_счётного_показателя_ноль(tmp_path):
    """Поток, упавший до нуля, выглядел ровным: пустые корзины были None."""
    db = _база(tmp_path)
    полночь = time.mktime(time.localtime(time.time())[:3] + (0, 0, 0, 0, 0, -1))
    for день in range(10, 15):
        _задание(db, f"x{день}", когда=полночь - день * 86400 + 3600)
    итог = Trends(db).series("month", bucket="day", metrics=["jobs"],
                             compare=False)["summary"][0]
    assert итог["min"] == 0 and итог["last"] == 0
    db.close()


def test_говорящих_на_записи_по_завершённым_с_разделением(tmp_path):
    db = _база(tmp_path)
    сейчас = time.time()
    _задание(db, "two", когда=сейчас - 500, speakers_count=2)
    _задание(db, "nodiar", когда=сейчас - 400, speakers_count=0)
    _задание(db, "fail", когда=сейчас - 300, статус="failed")
    итог = Trends(db).series("week", metrics=["speakers"], compare=False)["summary"][0]
    assert итог["avg"] == 2
    db.close()


def test_часы_аудио_по_завершённым(tmp_path):
    """Тренды считали длительность и упавших, и стоящих в очереди."""
    db = _база(tmp_path)
    сейчас = time.time()
    _задание(db, "done", когда=сейчас - 500, media_duration_s=3600.0)
    _задание(db, "failed", когда=сейчас - 400, статус="failed", media_duration_s=3600.0)
    _задание(db, "queued", когда=сейчас - 300, статус="queued", media_duration_s=3600.0)
    итог = Trends(db).series("week", metrics=["audio_hours"], compare=False)["summary"][0]
    assert итог["total"] == 1.0
    db.close()


def test_неизмеренное_железо_не_ноль(tmp_path):
    db = _база(tmp_path)
    сейчас = time.time()
    db.execute("INSERT INTO gpu_samples (ts, gpu, util_percent, mem_used_mb, temperature_c, "
               "power_w) VALUES (?,?,?,?,?,?)", (сейчас - 100, 0, 50.0, 1000.0, 60.0, None))
    db.execute("INSERT INTO gpu_samples (ts, gpu, util_percent, mem_used_mb, temperature_c, "
               "power_w) VALUES (?,?,?,?,?,?)", (сейчас - 90, 0, 50.0, 1000.0, None, None))
    свод = {с["id"]: с for с in Trends(db).series(
        "day", metrics=["gpu_temp", "gpu_power"], compare=False, is_admin=True)["summary"]}
    assert свод["gpu_temp"]["avg"] == 60.0
    assert свод["gpu_power"]["avg"] is None
    db.close()


def test_выгрузка_трендов_со_сравнением_и_всеми_показателями(client):
    """Выгрузка звала ряды без прошлого периода — «Изменение, %» и «Вердикт»
    были пусты всегда; явный список показателей резался на 60."""
    import io
    import zipfile

    from asrhub.trends import ПОКАЗАТЕЛИ

    ответ = client.get("/api/trends", params={
        "period": "week", "metrics": ",".join(п.id for п in ПОКАЗАТЕЛИ)})
    assert ответ.status_code == 200, ответ.text
    assert len(ПОКАЗАТЕЛИ) > 60
    assert len(ответ.json()["series"]) == len(ПОКАЗАТЕЛИ)
    выгрузка = client.get("/api/trends/export", params={"period": "week", "fmt": "csv"})
    assert выгрузка.status_code == 200
    with zipfile.ZipFile(io.BytesIO(выгрузка.content)) as архив:
        имя = next(и for и in архив.namelist() if "Свод" in и)
        шапка = архив.read(имя).decode("utf-8-sig").splitlines()[0]
    assert "Прошлый период" in шапка


# ---------------------------------------------------------------------------
# Справочник сотрудников в отчётах
# ---------------------------------------------------------------------------


def test_в_аналитике_по_сотрудникам_имя_из_справочника(tmp_path):
    """В таблице и карточке оператора стоял добавочный «1043», хотя карточка
    с фамилией лежала в справочнике."""
    from asrhub.insights import Insights

    db = _база(tmp_path)
    сейчас = time.time()
    for n in range(4):
        _задание(db, f"c{n}", когда=сейчас - 100 - n, text="Здравствуйте, спасибо.")
        db.execute("INSERT INTO calls (uniqueid, job_id, agent) VALUES (?,?,?)",
                   (f"call{n}", f"c{n}", "1043"))
        db.save_content(f"c{n}", {"version": 1, "sentiment": 0.2, "sentiment_label": "neutral"})
    db.employees_import([{"external_id": "e1", "last_name": "Иванов", "first_name": "Пётр",
                          "phone_ext": "1043", "department": "Продажи"}], source="проверка")
    итог = Insights(db).employees("agent", "week")
    строка = next(с for с in итог["items"] if с["key"] == "1043")
    assert строка["name"].startswith("Иванов") and строка["department"] == "Продажи"
    assert "Иванов" in строка["label"] and "1043" in строка["label"]
    db.close()
