"""Заход 54: справочник сотрудников — находки 10-analytics №3, №4, №15, №18.

Все данные здесь вымышленные: имена и номера придуманы для проверок.
"""
from __future__ import annotations

import http.server
import threading
import time
from pathlib import Path

import pytest
from asrhub import employees as справочник
from asrhub.db import Database
from asrhub.errors import ConfigError


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def csv_штата(люди: list[tuple[str, str, str]]) -> str:
    строки = ["Фамилия;Имя;Внутренний номер"]
    строки += [f"{ф};{и};{н}" for ф, и, н in люди]
    return "\n".join(строки) + "\n"


ШТАТ = [(f"Сотрудников{н}", "Тест", str(1000 + н)) for н in range(40)]


# ---------------------------------------------------------------------------
# №3: номер, переданный новому человеку, не переписывает карточку прежнего
# ---------------------------------------------------------------------------

def test_новый_человек_на_прежнем_номере_получает_свою_карточку(tmp_path):
    db = база(tmp_path)
    справочник.импорт(db, данные=csv_штата([("Ивановский", "Пётр", "1043"),
                                            ("Соседов", "Глеб", "1044")]))
    прежний = db.query_one("SELECT * FROM employees WHERE phone_ext='1043'")
    db.execute("UPDATE employees SET note='примечание о прежнем' WHERE id=?", (прежний["id"],))

    итог = справочник.импорт(db, данные=csv_штата([("Петровский", "Олег", "1043"),
                                                  ("Соседов", "Глеб", "1044")]))
    assert итог["replaced"] == 1 and итог["added"] == 1, итог
    assert any("другие люди" in з for з in итог["notes"]), итог["notes"]
    старый = db.query_one("SELECT * FROM employees WHERE id=?", (прежний["id"],))
    assert старый["last_name"] == "Ивановский" and старый["active"] == 0
    assert старый["note"] == "примечание о прежнем" and старый["active_until"]
    новые = db.query("SELECT * FROM employees WHERE phone_ext='1043' AND active=1")
    assert [н["last_name"] for н in новые] == ["Петровский"]
    assert not новые[0]["note"]

    # Повторный импорт той же выгрузки ничего не меняет.
    снова = справочник.импорт(db, данные=csv_штата([("Петровский", "Олег", "1043"),
                                                   ("Соседов", "Глеб", "1044")]))
    assert снова["replaced"] == 0 and снова["added"] == 0 and снова["unchanged"] == 2, снова


def test_смена_одной_фамилии_это_тот_же_человек(tmp_path):
    db = база(tmp_path)
    справочник.импорт(db, данные=csv_штата([("Ивановская", "Анна", "1043")]))
    итог = справочник.импорт(db, данные=csv_штата([("Петровская", "Анна", "1043")]))
    assert итог["replaced"] == 0 and итог["updated"] == 1, итог
    assert db.query_one("SELECT COUNT(*) AS n FROM employees")["n"] == 1


def test_предпросмотр_называет_смену_человека_и_не_пишет(tmp_path):
    db = база(tmp_path)
    справочник.импорт(db, данные=csv_штата([("Ивановский", "Пётр", "1043")]))
    итог = справочник.импорт(db, данные=csv_штата([("Петровский", "Олег", "1043")]),
                             dry_run=True)
    assert итог["changes"]["replaced"][0]["previous"].startswith("Ивановский"), итог
    assert db.query_one("SELECT last_name FROM employees")["last_name"] == "Ивановский"


# ---------------------------------------------------------------------------
# №4: неполная выгрузка не «увольняет» остаток штата
# ---------------------------------------------------------------------------

def test_незакрытая_кавычка_отвергается_а_не_увольняет(tmp_path):
    db = база(tmp_path)
    справочник.импорт(db, данные=csv_штата(ШТАТ))
    люди = list(ШТАТ)
    люди[11] = ('"Ромашка ООО', "Тест", "1011")
    with pytest.raises(ConfigError, match="кавычк"):
        справочник.импорт(db, данные=csv_штата(люди))
    assert db.query_one("SELECT COUNT(*) AS n FROM employees WHERE active=1")["n"] == 40


def test_массовое_увольнение_требует_force(tmp_path):
    db = база(tmp_path)
    справочник.импорт(db, данные=csv_штата(ШТАТ))
    треть = csv_штата(ШТАТ[:13])
    итог = справочник.импорт(db, данные=треть)
    assert итог["deactivated"] == 0 and итог["deactivation_blocked"] == 27, итог
    assert any("force=true" in з for з in итог["notes"]), итог["notes"]
    assert db.query_one("SELECT COUNT(*) AS n FROM employees WHERE active=1")["n"] == 40
    # Немного уволенных — без вопросов.
    итог = справочник.импорт(db, данные=csv_штата(ШТАТ[:37]))
    assert итог["deactivated"] == 3 and not итог["deactivation_blocked"], итог
    # Явное «да, штат сократился».
    итог = справочник.импорт(db, данные=треть, force=True)
    assert итог["deactivated"] == 24, итог


class _Обрыв(http.server.BaseHTTPRequestHandler):
    тело = csv_штата(ШТАТ).encode("utf-8")

    def do_GET(self):                                           # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(self.тело)))
        self.end_headers()
        self.wfile.write(self.тело[: len(self.тело) // 3])
        self.wfile.flush()
        self.close_connection = True

    def log_message(self, *a):                                  # noqa: D102
        pass


def test_оборванная_передача_не_импортируется(tmp_path):
    сервер = http.server.HTTPServer(("127.0.0.1", 0), _Обрыв)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        адрес = f"http://127.0.0.1:{сервер.server_address[1]}/staff.csv"
        with pytest.raises(ConfigError, match="не целиком"):
            справочник.загрузить(адрес, timeout=10)
    finally:
        сервер.shutdown()


# ---------------------------------------------------------------------------
# №15: сверка — общий номер считается один раз, неизвестные — по всему справочнику
# ---------------------------------------------------------------------------

def test_сверка_не_считает_звонки_общего_номера_дважды(tmp_path):
    db = база(tmp_path)
    for кто in (("Первов", "Иван"), ("Второв", "Пётр")):
        db.employee_save({"last_name": кто[0], "first_name": кто[1], "phone_ext": "2001",
                          "external_id": "|".join(кто).lower(), "source": "справочник"})
    сейчас = time.time()
    for н in range(7):
        db.save_call(uniqueid=f"c{н}", job_id=None, agent="2001", station="pbx",
                     started_at=сейчас - н * 60, duration=60, billsec=50, answered=1,
                     owner="telephony")
    итог = справочник.связать(db)
    assert итог["calls_matched"] == 7, итог
    assert all(ч.get("shared_ext") == 2 for ч in итог["items"])


def test_неизвестные_номера_ищутся_по_всему_справочнику(tmp_path):
    db = база(tmp_path)
    for н in range(30):
        db.employee_save({"last_name": f"Штатный{н}", "first_name": "Тест",
                          "phone_ext": str(3000 + н), "external_id": str(3000 + н),
                          "source": "справочник"})
    db.save_call(uniqueid="x1", job_id=None, agent="3029", station="pbx",
                 started_at=time.time(), duration=60, billsec=50, answered=1,
                 owner="telephony")
    итог = справочник.связать(db, limit=2)          # читается 8 карточек из 30
    assert "3029" not in итог["unknown_agents"], итог


# ---------------------------------------------------------------------------
# №18: тело с ошибкой в JSON — отказ, а не импорт по адресу из настроек
# ---------------------------------------------------------------------------

def test_кривое_тело_импорта_отвергается(client):
    ответ = client.post("/api/employees/import",
                        content=b'{"url": "http://127.0.0.1:9/x.csv", "deactivate_missing": false,}',
                        headers={"Content-Type": "application/json"})
    assert ответ.status_code == 400, ответ.text
    assert "не JSON" in ответ.json()["message"]
    список = client.post("/api/employees/import", json=["http://127.0.0.1:9/x.csv"])
    assert список.status_code == 400, список.text
