"""Заход 71: сплошная проверка кода — что нашлось и как исправлено.

* Признаки размеченной записи опроса считались с самого ответа, а телефон
  слушает звонок после паузы в 0,6 с: модель автоответчика училась на
  одном куске звука, а решала по другому.
* Файл модели, правленный руками (короткие списки, нулевой разброс), молча
  давал неверный ответ или деление на ноль посреди звонка.
* Одна строка журнала CEL с непарной кавычкой склеивалась со следующими, а
  поле длиннее предела останавливало чтение журнала навсегда; полная порция
  без перевода строки — тоже.
* Целое вне 64 бит из запроса доходило до SQLite и отвечало 500.
* Приём AudioSocket: второй идентификатор в соединении оставлял открытой
  прежнюю сессию, числа разговоров никто не ограничивал.
"""
from __future__ import annotations

import csv
import io
import json
import socket
import sys
import time
import uuid
import wave
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_69 import (  # noqa: E402
    _запись,
    _разметка,
    голос,
    тишина,
)

# ---------------------------------------------------------------------------
# Модель автоответчика
# ---------------------------------------------------------------------------


def test_признаки_записи_считаются_с_места_где_слушает_телефон(tmp_path: Path):
    from asrhub.qc import amd_model as A
    from asrhub.qc.survey import ПАУЗА_ПОСЛЕ_ОТВЕТА_С

    # От ответа: 0,6 с паузы (в ней — щелчок линии), 0,5 с тишины, «Алло».
    путь = tmp_path / "опрос.wav"
    _запись(путь, голос(ПАУЗА_ПОСЛЕ_ОТВЕТА_С) + тишина(0.5) + голос(0.6) + тишина(3.0))
    как_звонок = dict(zip(A.ПРИЗНАКИ, A.признаки(A.звук_клиента_из_wav(путь)), strict=True))
    assert как_звонок["тишина_до_с"] == pytest.approx(0.5, abs=0.08)
    assert как_звонок["первая_реплика_с"] == pytest.approx(0.6, abs=0.12)
    с_ответа = dict(zip(A.ПРИЗНАКИ, A.признаки(A.звук_клиента_из_wav(путь, с=0.0)), strict=True))
    assert с_ответа["тишина_до_с"] == 0.0
    # Запись короче паузы — пусто, а не ошибка; не 8 кГц — отказ словами.
    короткая = tmp_path / "короткая.wav"
    _запись(короткая, голос(0.3))
    assert A.звук_клиента_из_wav(короткая) == b""
    чужая = tmp_path / "чужая.wav"
    with wave.open(str(чужая), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(bytes(3200))
    with pytest.raises(ValueError, match="8 кГц"):
        A.звук_клиента_из_wav(чужая)


def test_испорченный_файл_модели_не_доходит_до_звонка(tmp_path: Path):
    from asrhub.qc import amd_model as A

    исходная = A.обучить(_разметка()).наружу()
    for порча in ({"std": исходная["std"][:3]}, {"weights": [*исходная["weights"], 1.0]},
                  {"std": [0.0, *исходная["std"][1:]]}, {"bias": float("nan")},
                  {"mean": ["x"] * len(исходная["mean"])}):
        with pytest.raises((ValueError, TypeError)):
            A.Модель.из_словаря({**исходная, **порча})
    (tmp_path / A.ФАЙЛ).write_text(json.dumps({**исходная, "std": исходная["std"][:3]}),
                                   encoding="utf-8")
    assert A.загрузить(tmp_path) is None
    # Точность строкой (правка руками) — числом; целая модель — как была.
    целая = A.Модель.из_словаря({**исходная, "accuracy": "0.9"})
    assert целая.точность == 0.9 and целая.веса == исходная["weights"]


@pytest.fixture()
def клиент(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({"ah_admin_71": {"name": "админ", "role": "admin", "enabled": True}})
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as к:
        yield к, app.state.hub


def test_разметка_записи_которая_не_wav_отвечает_отказом(клиент):
    к, state = клиент
    каталог = state.qc.каталог_записей_опросов()
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "плохая.wav").write_bytes("это не запись".encode() * 10)
    state.db.execute("INSERT INTO qc_surveys (id, number, created_at, status, recording) "
                     "VALUES ('плохая', '+79001234567', ?, 'пройден', ?)",
                     (time.time(), str(каталог / "плохая.wav")))
    ответ = к.put("/api/qc/surveys/плохая/amd_label", headers={"X-API-Key": "ah_admin_71"},
                  json={"label": "человек"})
    assert ответ.status_code == 400 and "не читается" in ответ.json()["message"], ответ.text


# ---------------------------------------------------------------------------
# Журналы станции
# ---------------------------------------------------------------------------


def _строка_cel(вид: str, момент: float, uid: str, мост: str = "b-1") -> str:
    поток = io.StringIO()
    csv.writer(поток, lineterminator="").writerow(
        [вид, str(момент), "PJSIP/102-00000005", uid, "1789000000.1", "s",
         json.dumps({"bridge_id": мост})])
    return поток.getvalue()


def test_строка_cel_с_непарной_кавычкой_не_глотает_соседей(tmp_path: Path):
    from asrhub.telephony import transfers as П

    журнал = tmp_path / "asrhub-cel.csv"
    строки = [_строка_cel("BRIDGE_ENTER", 1_789_000_010, "1.6"),
              'CHAN_START,1789000011,"PJSIP/оборвано,1.7,1789000000.1,s,{}',
              _строка_cel("BRIDGE_EXIT", 1_789_000_020, "1.6"),
              "x" * 200_000,
              _строка_cel("BRIDGE_ENTER", 1_789_000_030, "1.8")]
    журнал.write_text("\n".join(строки) + "\n", encoding="utf-8")
    события, позиция, _ = П.прочитать_cel(журнал, 0, журнал.stat().st_ino)
    assert [(с["kind"], с["uniqueid"]) for с in события] == [
        ("enter", "1.6"), ("leave", "1.6"), ("enter", "1.8")]
    assert позиция == журнал.stat().st_size
    assert П.поля_cel('a,"b') == ['a', 'b'] and П.поля_cel("") == []


def test_порция_журнала_без_перевода_строки_не_останавливает_чтение(tmp_path: Path,
                                                                    monkeypatch):
    from asrhub.telephony import queue_log as О
    from asrhub.telephony import transfers as П

    monkeypatch.setattr(П, "ПОРЦИЯ_CEL_БАЙТ", 64)
    monkeypatch.setattr(О, "ПОРЦИЯ_БАЙТ", 64)
    не_журнал = tmp_path / "двоичный"
    не_журнал.write_bytes(b"\x01" * 200)
    inode = не_журнал.stat().st_ino
    for читать in (П.прочитать_cel, О.прочитать_приращение):
        события, позиция, _ = читать(не_журнал, 0, inode)
        assert события == [] and позиция == 64, читать.__name__
    # Хвост без перевода строки (журнал дописывается) — ждёт, как прежде.
    хвост = tmp_path / "хвост"
    хвост.write_bytes(b"BRIDGE_ENTER,1789")
    assert П.прочитать_cel(хвост, 0, хвост.stat().st_ino)[1] == 0


# ---------------------------------------------------------------------------
# Число вне 64 бит
# ---------------------------------------------------------------------------


def test_целое_вне_64_бит_это_отказ_а_не_внутренняя_ошибка(клиент):
    from asrhub.errors import ConfigError

    к, state = клиент
    ответ = к.put("/api/qa/99999999999999999999", headers={"X-API-Key": "ah_admin_71"}, json={})
    assert ответ.status_code == 400, ответ.text
    assert "слишком велико" in ответ.json()["message"]
    with pytest.raises(ConfigError):
        state.db.query("SELECT ? AS n", (2 ** 70,))
    with pytest.raises(ConfigError):
        state.db.execute("INSERT INTO kv (key, value, ts) VALUES ('много', 'x', ?)", (2 ** 70,))
    # Запись после отказа работает: транзакция откатилась, замок отпущен.
    state.db.set_kv("после", {"ok": True})
    assert state.db.get_kv("после") == {"ok": True}


# ---------------------------------------------------------------------------
# AudioSocket
# ---------------------------------------------------------------------------


def test_audiosocket_второй_идентификатор_и_предел_разговоров(data_dir: Path, monkeypatch):
    from asrhub import streaming
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.telephony.audiosocket import ВИД_ЗВУК, ВИД_ИД, ВИД_КОНЕЦ, ВИД_ОШИБКА, кадр
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    with socket.socket() as пробный:
        пробный.bind(("127.0.0.1", 0))
        порт = пробный.getsockname()[1]
    настройки = load()
    настройки.set("audiosocket_port", порт)
    настройки.set("audiosocket_host", "127.0.0.1")
    настройки.set("stream_max_sessions", 1)
    открыто: list[int] = []
    закрыто: list[int] = []
    начать, закрыть = streaming.StreamSession.start, streaming.StreamSession.close

    def старт(сессия, *a, **kw):
        открыто.append(id(сессия))
        return начать(сессия, *a, **kw)

    def конец(сессия, *a, **kw):
        закрыто.append(id(сессия))
        return закрыть(сессия, *a, **kw)

    monkeypatch.setattr(streaming.StreamSession, "start", старт)
    monkeypatch.setattr(streaming.StreamSession, "close", конец)
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as к:
        приём = к.app.state.hub.audiosocket
        with socket.create_connection(("127.0.0.1", порт), timeout=10) as первый:
            первый.sendall(кадр(ВИД_ИД, uuid.uuid4().bytes))
            первый.sendall(кадр(ВИД_ИД, uuid.uuid4().bytes))      # второй — мимо
            первый.sendall(кадр(ВИД_ЗВУК, bytes(320)))
            срок = time.monotonic() + 10
            while not приём.идут() and time.monotonic() < срок:
                time.sleep(0.05)
            assert len(приём.идут()) == 1 and len(открыто) == 1
            # Предел разговоров — один: второму соединению — кадр ошибки и отбой.
            with socket.create_connection(("127.0.0.1", порт), timeout=10) as второй:
                второй.sendall(кадр(ВИД_ИД, uuid.uuid4().bytes))
                второй.settimeout(10)
                ответ = второй.recv(16)
                assert ответ[:1] == bytes([ВИД_ОШИБКА]), ответ
                assert второй.recv(16) == b""
            assert len(открыто) == 1
            первый.sendall(кадр(ВИД_КОНЕЦ))
        срок = time.monotonic() + 10
        while приём.идут() and time.monotonic() < срок:
            time.sleep(0.05)
        assert приём.идут() == [] and закрыто == открыто


# ---------------------------------------------------------------------------
# Обход маршрутов краевыми значениями
# ---------------------------------------------------------------------------

#: Маршруты, которые обход не трогает: перезапуск, копии и восстановление,
#: сброс настроек, загрузка весов и установка модели, удаление по требованию.
_НЕ_ОБХОДИТЬ = ("/api/maintenance", "/api/system/restart", "/api/backup", "/api/settings/reset",
                "/api/models/", "/api/llm/setup", "/api/update")
_КРАЯ = ("", "-1", "99999999999999999999", "abc", "'", "%", "1e400", "а" * 300)
_ТЕЛА: tuple[object, ...] = ({}, [], "x", None, {"id": None}, {"name": {"a": 1}}, 12345)


def test_ни_один_маршрут_не_отвечает_5xx_на_краевые_значения(data_dir: Path, monkeypatch):
    """Каждый маршрут из описания API — с пустыми, огромными и чужими
    значениями в адресе, параметрах и теле. Отказ — это 4xx с причиной;
    500 значит, что значение дошло туда, где его не ждали.

    Так нашёлся `PUT /api/qa/99999999999999999999` (OverflowError из SQLite).
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    app = create_app(load(), start_queue=False)
    плохие: list[str] = []
    запросов = 0
    with TestClient(app, raise_server_exceptions=False) as к:
        пути = к.get("/api/openapi.json").json()["paths"]
        for путь, методы in sorted(пути.items()):
            if путь.startswith(_НЕ_ОБХОДИТЬ):
                continue
            for метод, описание in методы.items():
                if метод not in ("get", "post", "put", "patch", "delete"):
                    continue
                параметры = описание.get("parameters", [])
                в_адресе = [п["name"] for п in параметры if п.get("in") == "path"]
                if метод == "delete" and not в_адресе:
                    continue
                for край in _КРАЯ:
                    адрес = путь
                    for имя in в_адресе:
                        адрес = адрес.replace("{" + имя + "}", (край or "x")[:40])
                    запрос = {п["name"]: край for п in параметры if п.get("in") == "query"}
                    тела = (None,) if метод in ("get", "delete") else \
                        (_ТЕЛА if край == "" else _ТЕЛА[:1])
                    for тело in тела:
                        ответ = к.request(метод.upper(), адрес, params=запрос,
                                          **({} if тело is None and метод in ("get", "delete")
                                             else {"json": тело}))
                        запросов += 1
                        # 501, 503 и 507 — честные «не умею», «не готов», «нет места».
                        if ответ.status_code >= 500 and ответ.status_code not in (501, 503, 507):
                            плохие.append(f"{метод.upper()} {путь} [{край[:24]!r}, "
                                          f"{тело!r}] → {ответ.status_code}: {ответ.text[:120]}")
    assert запросов > 2000, запросов
    assert not плохие, "\n".join(dict.fromkeys(плохие))


# ---------------------------------------------------------------------------
# Телефон опросов: повторное приглашение сразу за ACK
# ---------------------------------------------------------------------------


def test_состояние_звука_готово_до_ack_и_приглашение_в_устанавливаемом_разговоре():
    """Между ACK и записью адреса звука телефон отвечал 486 «занято» на
    повторное приглашение в собственном разговоре: станция шлёт его сразу за
    ACK (прямой звук, обновление сессии). Проверка мини-АТС на этом падала
    изредка — по гонке двух потоков."""
    from asrhub.qc import sip

    телефон = sip.Телефон.__new__(sip.Телефон)
    телефон.узел, телефон._ip_станции = "127.0.0.1", "127.0.0.1"
    при_ack: list[object] = []
    телефон._ack = lambda разговор: при_ack.append((разговор.куда, разговор.кодек))
    звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    звук.bind(("127.0.0.1", 0))
    разговор = sip.Разговор(телефон, "вызов-71", звук)
    ответ = sip.разобрать(sip.собрать("SIP/2.0 200 OK", [
        ("Via", "SIP/2.0/UDP 127.0.0.1:5070;branch=z9hG4bK71"), ("From", "<sip:900@x>;tag=a"),
        ("To", "<sip:101@x>;tag=b"), ("Call-ID", "вызов-71"), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:pbx@127.0.0.1:5060>"), ("Content-Type", "application/sdp")],
        (b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
         b"m=audio 40000 RTP/AVP 0 101\r\na=rtpmap:0 PCMU/8000\r\n"
         b"a=rtpmap:101 telephone-event/8000\r\n")))
    try:
        телефон._установить(разговор, ответ, "<sip:900@x>;tag=a", 1)
        assert при_ack == [(("127.0.0.1", 40000), "PCMU")]
    finally:
        разговор.завершён.set()
        звук.close()

    # Приглашение в разговоре, который ещё устанавливается, — 491, не 486.
    отвечено: list[tuple[int, str]] = []
    телефон2 = sip.Телефон.__new__(sip.Телефон)
    телефон2.входящие = None
    телефон2._замок = __import__("threading").Lock()
    недостроенный = type("Р", (), {"куда": None})()
    телефон2._разговоры = {"вызов-72": недостроенный}
    телефон2._ответить = lambda запрос, откуда, код, слово, **к: отвечено.append((код, слово))
    запрос = sip.разобрать(sip.собрать("INVITE sip:900@127.0.0.1 SIP/2.0", [
        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKre"), ("From", "<sip:101@x>;tag=b"),
        ("To", "<sip:900@x>;tag=a"), ("Call-ID", "вызов-72"), ("CSeq", "2 INVITE")]))
    телефон2._запрос(запрос, ("127.0.0.1", 5060))
    assert отвечено == [(491, "Request Pending")], отвечено
