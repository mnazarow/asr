"""Номера абонентов и прочие данные звонка в POST /api/jobs и /api/jobs/batch.

Проверяется ровно то, ради чего поля и заводились: разговор, приехавший
через API, попадает в раздел «Телефония» и в разрезы по оператору, очереди
и направлению — и при этом ничего не ломается ни для тех, кто этих полей не
шлёт, ни для тех, кто прислал в них ерунду.

Задание важнее метки: неверное значение одного поля не отменяет
распознавание, а объясняется в ответе.
"""
from __future__ import annotations

import io
import math
import struct
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from fastapi.testclient import TestClient


def _wav() -> bytes:
    """Секунда тона: очередь не запускается, файл нужен только как файл."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(i / 8)))
                               for i in range(16000)))
    return buf.getvalue()


@pytest.fixture()
def tel(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер без запущенной очереди: проверяем постановку, а не распознавание."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as c:
        yield c


def поставить(client: TestClient, **поля) -> dict:
    ответ = client.post("/api/jobs",
                        files={"file": ("разговор.wav", _wav(), "audio/wav")},
                        data={k: v for k, v in поля.items() if v is not None})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def звонки(client: TestClient, **отбор) -> list[dict]:
    ответ = client.get("/api/telephony/calls", params={"period": "all", **отбор})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()["calls"]


# ---------------------------------------------------------------------------
# Основной путь
# ---------------------------------------------------------------------------

def test_a_job_with_call_fields_shows_up_in_telephony(tel):
    """Задание с номерами абонентов создаёт строку в архиве звонков."""
    начало = time.time() - 3600
    job = поставить(tel, caller="79991234567", callee="4242", call_id="1757500001.7",
                    direction="входящий", agent="4242", queue="support",
                    call_started_at=str(начало), disposition="ANSWERED",
                    station="офис")

    звонок_в_ответе = job["call"]
    assert звонок_в_ответе["saved"] is True
    assert звонок_в_ответе["warnings"] == []
    # Ключ архива — «станция:идентификатор», как у импортёра АТС.
    assert звонок_в_ответе["uniqueid"] == "офис:1757500001.7"
    assert звонок_в_ответе["pbx_uid"] == "1757500001.7"

    найдено = звонки(tel)
    assert len(найдено) == 1, найдено
    звонок = найдено[0]
    assert звонок["uniqueid"] == "офис:1757500001.7"
    assert звонок["pbx_uid"] == "1757500001.7"
    assert звонок["job_id"] == job["id"]
    assert звонок["src"] == "79991234567" and звонок["dst"] == "4242"
    assert звонок["direction"] == "входящий" and звонок["queue"] == "support"
    assert звонок["agent"] == "4242" and звонок["station"] == "офис"
    assert звонок["disposition"] == "ANSWERED" and звонок["answered"] is True
    assert abs(звонок["started_at"] - начало) < 1.5
    # Разговор связан с заданием: длительность записи стала длительностью
    # разговора, иначе все сводки по времени показывали бы ноль.
    assert звонок["billsec"] >= 1 and звонок["duration"] >= 1

    # Разрезы, ради которых всё и затевалось.
    разрезы = tel.get("/api/telephony/dimensions").json()
    assert разрезы["agents"] == ["4242"]
    assert разрезы["queues"] == ["support"]
    assert разрезы["directions"] == ["входящий"]
    assert разрезы["stations"] == ["офис"]
    assert звонки(tel, agent="4242") and звонки(tel, queue="support")
    assert звонки(tel, direction="входящий")
    # Поиск по номеру находит и по ключу архива, и по номеру абонента.
    assert звонки(tel, search="79991234567")


def test_one_field_is_enough_and_the_rest_have_sane_defaults(tel):
    """Прислали только номер звонящего — строка всё равно появляется.

    Станция без имени — постоянное «api»: пустая станция дала бы ключ из
    голого идентификатора, а он приходит снаружи и может совпасть с
    идентификатором настоящей АТС.
    """
    job = поставить(tel, caller="79990000000")
    звонок = звонки(tel)[0]
    assert звонок["station"] == "api"
    assert звонок["uniqueid"] == f"api:{job['id']}"
    # Идентификатор не прислали — им стал идентификатор задания: он уже
    # уникален на весь сервер и уже напечатан в ответе на этот запрос.
    assert звонок["pbx_uid"] == job["id"]
    assert звонок["job_id"] == job["id"]
    # Время не прислали — время постановки задания: ноль выкинул бы звонок
    # из всех периодов раздела «Телефония», кроме «за всё время».
    assert звонок["started_at"] > 0
    assert звонки(tel, **{"period": "day"}) != []
    # Исход не назвали, а запись разговора у нас есть — значит отвечен.
    assert звонок["answered"] is True


def test_the_same_call_id_updates_the_row_instead_of_doubling_it(tel):
    """Повторная отправка того же звонка не задваивает архив."""
    поставить(tel, caller="111", callee="222", call_id="повтор-1", station="офис")
    второе = поставить(tel, caller="111", callee="333", call_id="повтор-1",
                       station="офис", queue="sales")
    найдено = звонки(tel)
    assert len(найдено) == 1, найдено
    assert найдено[0]["dst"] == "333" and найдено[0]["queue"] == "sales"
    assert найдено[0]["job_id"] == второе["id"]


# ---------------------------------------------------------------------------
# Ничего не прислали — ничего не изменилось
# ---------------------------------------------------------------------------

def test_a_job_without_call_fields_behaves_exactly_as_before(tel):
    """Ни строки в архиве, ни нового ключа в ответе."""
    ответ = tel.post("/api/jobs", files={"file": ("обычная.wav", _wav(), "audio/wav")})
    assert ответ.status_code == 200, ответ.text
    job = ответ.json()
    assert "call" not in job
    assert job["status"] in ("queued", "completed")
    assert звонки(tel) == []

    # И пустые строки в полях — это «не прислали», а не «прислали пустое».
    пустые = поставить(tel, caller="", callee="", call_id="", direction="")
    assert "call" not in пустые
    assert звонки(tel) == []


def test_an_unknown_form_field_is_still_an_error(tel):
    """Поля звонка не открыли дорогу опечаткам в именах параметров."""
    ответ = tel.post("/api/jobs", files={"file": ("а.wav", _wav(), "audio/wav")},
                     data={"callerr": "79990000000"})
    assert ответ.status_code == 400
    сообщение = ответ.json()
    assert "callerr" in сообщение["message"]
    # Подсказка называет поля звонка: иначе они выглядят «неизвестными».
    assert "caller" in сообщение["hint"] and "call_started_at" in сообщение["hint"]


# ---------------------------------------------------------------------------
# Кривые значения: задание важнее метки
# ---------------------------------------------------------------------------

def test_a_broken_time_does_not_break_the_job(tel):
    """Время не разобралось — задание создано, объяснение в ответе."""
    job = поставить(tel, caller="79991112233", call_started_at="позавчера вечером")
    assert job["id"]
    звонок = job["call"]
    assert звонок["saved"] is True
    assert any("время начала звонка" in б for б in звонок["warnings"]), звонок
    # Метка не записана, а разговор в архиве есть и виден за сутки.
    строка = звонки(tel, period="day")[0]
    assert строка["src"] == "79991112233"
    assert строка["started_at"] > 0


def test_a_broken_direction_does_not_break_the_job(tel):
    """Направление не из набора — не записывается, но объясняется."""
    job = поставить(tel, caller="1", direction="куда-то вбок")
    предупреждения = job["call"]["warnings"]
    assert any("направление" in б for б in предупреждения), предупреждения
    assert "входящий" in " ".join(предупреждения), "в подсказке должен быть набор"
    assert звонки(tel)[0]["direction"] == ""


@pytest.mark.parametrize("прислали,ожидаем", [
    ("inbound", "входящий"), ("IN", "входящий"), ("Исходящий", "исходящий"),
    ("outbound", "исходящий"), ("internal", "внутренний"), ("вну", "внутренний"),
])
def test_latin_directions_are_understood(tel, прислали, ожидаем):
    """Интеграции пишут на латинице — словарь на десять строк дешевле,
    чем переделка на их стороне."""
    job = поставить(tel, caller="1", direction=прислали, call_id=f"н-{прислали}")
    assert job["call"]["warnings"] == []
    assert job["call"]["direction"] == ожидаем


@pytest.mark.parametrize("значение", [
    "1757500001",                       # секунды эпохи
    "1757500001000",                    # миллисекунды: всё, что писали на JS
    "2025-09-10T14:03:11",              # ISO-8601
    "2025-09-10T14:03:11Z",             # ISO-8601 с «Z» — так шлют браузеры
    "2025-09-10 14:03:11",              # пробел вместо T — так пишет Asterisk
])
def test_the_time_is_taken_in_every_shape_it_arrives_in(tel, значение):
    job = поставить(tel, caller="1", call_started_at=значение, call_id=f"в-{значение}")
    assert job["call"]["warnings"] == [], job["call"]
    строка = звонки(tel, search=f"в-{значение}")[0]
    # Все виды должны дать 2025 год, а не 1970 и не 57000-й.
    год = datetime.fromtimestamp(строка["started_at"], tz=timezone.utc).year
    assert год == 2025, (значение, строка["started_at"])


def test_a_time_that_is_obviously_wrong_is_refused_not_stored(tel):
    """Ноль и «до 2000 года» — это ошибка ввода, а не история."""
    job = поставить(tel, caller="1", call_started_at="0")
    assert any("не похоже на дату" in б for б in job["call"]["warnings"])
    assert звонки(tel)[0]["started_at"] > 0


# ---------------------------------------------------------------------------
# Пакетная загрузка
# ---------------------------------------------------------------------------

def test_a_batch_keeps_one_row_per_recording(tel):
    """Один звонок — несколько записей (по одной на плечо), и все в архиве.

    Ключ строки собирается по заданию, иначе второй файл пакета
    перезаписал бы строку первого и половина записей пропала бы.
    """
    ответ = tel.post("/api/jobs/batch",
                     files=[("files", ("первая.wav", _wav(), "audio/wav")),
                            ("files", ("вторая.wav", _wav(), "audio/wav"))],
                     data={"caller": "79995554433", "callee": "101",
                           "call_id": "1757500009.1", "direction": "outbound",
                           "agent": "101", "station": "офис"})
    assert ответ.status_code == 200, ответ.text
    пакет = ответ.json()
    assert пакет["created"] == 2 and пакет["errors"] == []

    ключи = {з["call"]["uniqueid"] for з in пакет["jobs"]}
    assert len(ключи) == 2, "у каждой записи должен быть свой ключ в архиве"
    assert all(з["call"]["pbx_uid"] == "1757500009.1" for з in пакет["jobs"])
    assert all(з["call"]["direction"] == "исходящий" for з in пакет["jobs"])

    найдено = звонки(tel)
    assert len(найдено) == 2
    assert {з["uniqueid"] for з in найдено} == ключи
    assert {з["job_id"] for з in найдено} == {з["id"] for з in пакет["jobs"]}
    assert all(з["pbx_uid"] == "1757500009.1" for з in найдено)


def test_a_batch_without_call_fields_is_untouched(tel):
    ответ = tel.post("/api/jobs/batch",
                     files=[("files", ("одна.wav", _wav(), "audio/wav"))])
    assert ответ.status_code == 200, ответ.text
    assert "call" not in ответ.json()["jobs"][0]
    assert звонки(tel) == []


# ---------------------------------------------------------------------------
# Права и разрезы
# ---------------------------------------------------------------------------

def test_the_call_belongs_to_the_key_that_sent_it(data_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    """Звонок достаётся владельцу задания: разрез по владельцу — всё, что
    отделяет чужие разговоры от своих в отчётах."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.api_keys["ah_alice"] = {"name": "Алиса", "role": "user", "enabled": True}
    settings.api_keys["ah_bob"] = {"name": "Боб", "role": "user", "enabled": True}
    app = create_app(settings, start_queue=False)
    with TestClient(app) as c:
        создано = c.post("/api/jobs", headers={"X-API-Key": "ah_alice"},
                         files={"file": ("а.wav", _wav(), "audio/wav")},
                         data={"caller": "79990001122", "call_id": "тайна-1"})
        assert создано.status_code == 200, создано.text

        свои = c.get("/api/telephony/calls?period=all",
                     headers={"X-API-Key": "ah_alice"}).json()
        assert [з["uniqueid"] for з in свои["calls"]] == ["api:тайна-1"]
        assert свои["calls"][0]["owner"] == "Алиса"

        чужие = c.get("/api/telephony/calls?period=all",
                      headers={"X-API-Key": "ah_bob"}).json()
        assert чужие["calls"] == [], "чужой разговор не должен быть виден"
