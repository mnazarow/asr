"""Заход 56: сплошная проверка кода — что исправлено.

* очередь: звук и ожидание считаются по всей очереди, пауза — из базы;
  ожидание занятой модели — ожидание, а не обработка;
* уведомления: адрес с токеном не попадает в журнал, тревога повторяется,
  недошедшая сводка уходит через час, а не через срок;
* приём из диалплана: станция закреплена за ключом, квота ключа, повтор
  идущего звонка — 409, замок станции не держит каждое обращение,
  выключенный забор — 503 (проверено в test_dialplan);
* сценарий отправки: неотвеченный звонок не ждёт запись две минуты,
  запасная очередь — не в /tmp с предсказуемым именем, --resend под замком
  и прерывается, когда сервер не отвечает;
* контроль качества: отказ номеру закрывает опрос и не держит обзвон,
  «не звонить» проверяется перед набором, сломанный голос не сжигает
  очередь, регистрация держится между звонками, CANCEL повторяется,
  поздний ответ кладётся, одна кнопка двумя способами — одно нажатие.
"""
from __future__ import annotations

import logging
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from asrhub.qc import sip, survey  # noqa: E402
from qc_pbx import МиниАТС  # noqa: E402
from test_dialplan import (  # noqa: E402, F401, F811
    АДМИН,
    wav,
    живой,
    запустить,
    латиница,
    приложение,
    прислать,
    станция,
    строка,
)
from test_qc import взвести, дождаться, звонок  # noqa: E402
from test_qc import приложение as приложение_кк  # noqa: E402

# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------


def test_оценка_очереди_по_всей_очереди_и_по_скорости_модели(data_dir, monkeypatch):
    app = приложение(data_dir, monkeypatch, [станция()])
    db = app.state.hub.db
    сейчас = time.time()
    for н in range(620):
        db.execute("INSERT INTO jobs (id, status, model, media_duration_s, created_at, "
                   "updated_at, filename) VALUES (?,?,?,?,?,?,?)",
                   (f"j{н}", "queued", "быстрая" if н % 2 else "медленная", 10.0,
                    сейчас, сейчас, "a.wav"))
    db.execute("INSERT INTO model_stats (model, rtf_sum, rtf_count, jobs_total, jobs_ok) "
               "VALUES ('быстрая', 0.1, 1, 1, 1), ('медленная', 1.0, 1, 1, 1)")
    app.state.hub.settings.set("max_concurrent_jobs", 2)
    состояние = app.state.hub.queue.status()
    # Раньше суммировались первые 500 заданий: 5000 с вместо 6200.
    assert состояние["pending_audio_s"] == 6200.0
    # 310 заданий по 10 с на скорости 0,1 и 310 — на 1,0, два воркера.
    assert состояние["eta_s"] == pytest.approx((3100 * 0.1 + 3100 * 1.0) / 2, rel=1e-3)


def test_пауза_очереди_берётся_из_базы_и_на_узле_без_воркеров(data_dir, monkeypatch):
    app = приложение(data_dir, monkeypatch, [станция()])
    очередь = app.state.hub.queue
    assert очередь.status()["paused"] is False
    # Остановил соседний сервер: флаг в общей базе, память этого процесса
    # о нём не знает.
    app.state.hub.db.set_kv("queue.paused", True)
    очередь._пауза_прочитана = 0.0
    assert очередь.status()["paused"] is True


def test_ожидание_модели_отдельно_от_обработки(tmp_path, sample_wav):
    import contextlib

    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.processor import process_job

    class Занятая(EngineRegistry):
        @contextlib.contextmanager
        def lease(self, settings, **kw):
            time.sleep(0.6)                     # соседний воркер держит модель
            with super().lease(settings, **kw) as движок:
                yield движок

    стадии: list[str] = []
    settings = load().merged({"model": "demo-simulator", "engine": "demo",
                              "vad_backend": "energy", "output_formats": ["json"]})
    итог = process_job(sample_wav, settings, Занятая(), workdir=tmp_path / "wd",
                       outdir=tmp_path / "out", basename="проба",
                       progress=lambda доля, стадия: стадии.append(стадия))
    assert итог.stats["engine_wait_s"] >= 0.55
    assert "ожидание модели" in стадии
    # В стадии работы ожидание не входит.
    assert sum(итог.timings.values()) < итог.stats["engine_wait_s"] + 5


# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

def test_адрес_с_токеном_не_попадает_в_журнал_и_тревога_повторяется(caplog):
    from asrhub import maintenance

    занятый = socket.socket()
    занятый.bind(("127.0.0.1", 0))
    порт = занятый.getsockname()[1]
    занятый.close()                             # на этом порту никто не слушает
    адрес = f"http://127.0.0.1:{порт}/bot123456:ТОКЕН-ЧАТА/sendMessage?chat_id=1&token=ЕЩЁ"
    with caplog.at_level(logging.INFO):
        начало = time.monotonic()
        assert maintenance.send_json({"a": 1}, адрес, what="тревога", попыток=3,
                                     паузы=(0.05,), allow_internal=True) is False
    журнал = "\n".join(з.getMessage() for з in caplog.records)
    assert "ТОКЕН-ЧАТА" not in журнал and "ЕЩЁ" not in журнал
    assert f"127.0.0.1:{порт}" in журнал
    assert журнал.count("попытка") == 3 and time.monotonic() - начало < 5


def test_недошедшая_сводка_уходит_через_час_а_не_через_срок(data_dir, monkeypatch):
    from asrhub import maintenance

    app = приложение(data_dir, monkeypatch, [станция()])
    hub = app.state.hub
    hub.settings.set("digest_url", "http://127.0.0.1:9/digest")
    hub.settings.set("digest_interval_hours", 168)
    давно = time.time() - 200 * 3600
    hub.db.set_kv(maintenance.KV_DIGEST, давно)
    monkeypatch.setattr(maintenance, "build_digest", lambda *a, **k: {"text": "сводка"})
    monkeypatch.setattr(maintenance, "send_digest", lambda *a, **k: False)
    сделано = maintenance.run_scheduled(hub.db, hub.settings, analytics=None)
    assert сделано.get("digest") is False
    отметка = float(hub.db.get_kv(maintenance.KV_DIGEST))
    # Следующая попытка — через час: отметка «отправлено» на 167 часов назад.
    assert abs((time.time() - отметка) - 167 * 3600) < 120


# ---------------------------------------------------------------------------
# Приём из диалплана
# ---------------------------------------------------------------------------

@pytest.fixture()
def клиент2(data_dir, monkeypatch, tmp_path):
    каталог = tmp_path / "monitor"
    (каталог / "asrhub").mkdir(parents=True)
    app = приложение(data_dir, monkeypatch, [станция(recordings_dir=str(каталог))])
    app.state.hub.settings.api_keys.update({
        "ah_pbx2": {"name": "другой отдел", "role": "user", "enabled": True},
    })
    with TestClient(app) as c:
        c.hub = app.state.hub
        c.каталог = каталог
        yield c


def test_станция_закреплена_за_ключом(клиент2):
    чужой = {"X-API-Key": "ah_pbx2"}
    assert прислать(клиент2, "1793000000.60").status_code == 200
    ответ = прислать(клиент2, "1793000000.61", headers=чужой)
    assert ответ.status_code == 403 and "другого ключа" in ответ.json()["message"]
    assert строка(клиент2, "main:1793000000.61") is None
    # Администратору можно всегда, и он видит, за кем станция.
    assert прислать(клиент2, "1793000000.62", headers=АДМИН).status_code == 200
    состояние = клиент2.get("/api/telephony/dialplan/status", headers=АДМИН).json()
    assert состояние["stations"][0]["intake_key"] == "станция"
    # Отвязал — станция переходит к следующему, кто пришлёт.
    assert клиент2.delete("/api/telephony/dialplan/key/main", headers=АДМИН).json()["was"] \
        == "станция"
    assert прислать(клиент2, "1793000000.63", headers=чужой).status_code == 200
    assert прислать(клиент2, "1793000000.64").status_code == 403
    assert клиент2.delete("/api/telephony/dialplan/key/main",
                          headers={"X-API-Key": "ah_pbx"}).status_code == 403


def test_проба_не_выдаёт_наличие_файлов_не_администратору(клиент2):
    (клиент2.каталог / "asrhub" / "x.wav").write_bytes(wav())
    ответ = прислать(клиент2, "1793000000.65", файл=None, dry_run="1",
                     recording="asrhub/x.wav").json()
    assert ответ["result"] == "dry_run" and "recording_exists" not in ответ
    ответ = прислать(клиент2, "1793000000.66", файл=None, dry_run="1",
                     recording="asrhub/x.wav", headers=АДМИН).json()
    assert ответ["recording_exists"] is True


def test_квота_ключа_действует_и_на_диалплан(клиент2):
    клиент2.hub.settings.api_keys["ah_pbx"]["quota_jobs_per_day"] = 1
    assert прислать(клиент2, "1793000000.67").json()["result"] == "queued"
    ответ = прислать(клиент2, "1793000000.68")
    assert ответ.status_code == 429, ответ.text
    assert строка(клиент2, "main:1793000000.68") is None


def test_идущий_звонок_повтор_получает_409(клиент2):
    from asrhub.api import routes_dialplan

    routes_dialplan._ПРИНИМАЮТСЯ.add("main:1793000000.69")
    try:
        ответ = прислать(клиент2, "1793000000.69")
        assert ответ.status_code == 409 and ответ.json()["code"] == "call_in_progress"
    finally:
        routes_dialplan._ПРИНИМАЮТСЯ.discard("main:1793000000.69")
    assert прислать(клиент2, "1793000000.69").json()["result"] == "queued"


def test_новый_звонок_не_ждёт_замка_станции(клиент2):
    импортёр = клиент2.hub.telephony.импортёр("main")
    отпустить = threading.Event()

    def держать():
        with импортёр._заход:
            отпустить.wait(10)

    поток = threading.Thread(target=держать)
    поток.start()
    time.sleep(0.1)
    try:
        начало = time.monotonic()
        assert прислать(клиент2, "1793000000.70").json()["result"] == "queued"
        assert time.monotonic() - начало < 5
    finally:
        отпустить.set()
        поток.join()


# ---------------------------------------------------------------------------
# Сценарий отправки
# ---------------------------------------------------------------------------

def test_неотвеченный_звонок_не_ждёт_запись(живой):  # noqa: F811
    адрес, hub, tmp_path = живой
    начало = time.monotonic()
    итог = запустить(tmp_path, адрес, "uniqueid=1793000000.80", "billsec=0",
                     "disposition=NO ANSWER".replace(" ", ""), "dialstatus=NOANSWER",
                     f"file={tmp_path / 'monitor' / 'нет.wav'}", ASRHUB_WAIT_S="30")
    assert итог.returncode == 0, итог.stderr
    # Раньше — все ASRHUB_WAIT_S.
    assert time.monotonic() - начало < 12


def test_запасная_очередь_не_в_tmp_и_досылается(живой):  # noqa: F811
    адрес, hub, tmp_path = живой
    дом = tmp_path / "home"
    дом.mkdir()
    # Основной каталог недоступен даже root: на его месте — обычный файл.
    (tmp_path / "spool").write_text("не каталог", encoding="utf-8")
    итог = запустить(tmp_path, "http://127.0.0.1:9", "ready=1", "uniqueid=1793000000.81",
                     "billsec=30", HOME=str(дом))
    assert list((дом / ".asrhub-dialplan" / "pending").glob("*.call")), \
        (tmp_path / "send.log").read_text(encoding="utf-8")
    assert "копятся в" in (tmp_path / "send.log").read_text(encoding="utf-8")
    итог = запустить(tmp_path, адрес, "--resend", HOME=str(дом))
    assert "дослано 1" in итог.stdout, итог.stdout + итог.stderr


def test_досылка_под_замком_и_прерывается_когда_сервер_молчит(живой):  # noqa: F811
    адрес, hub, tmp_path = живой
    for н in (82, 83, 84):
        запустить(tmp_path, "http://127.0.0.1:9", "ready=1", f"uniqueid=1793000000.{н}",
                  "billsec=30")
    очередь = tmp_path / "spool" / "pending"
    assert len(list(очередь.glob("*.call"))) == 3
    # Сервер молчит: проход прерывается после первого звонка.
    итог = запустить(tmp_path, "http://127.0.0.1:9", "--resend")
    assert "прервана" in (tmp_path / "send.log").read_text(encoding="utf-8")
    assert "осталось 3" in итог.stdout, итог.stdout
    # Идёт другой --resend — этот запуск ничего не трогает.
    (tmp_path / "spool" / ".resend.lock").mkdir()
    итог = запустить(tmp_path, адрес, "--resend")
    assert "дослано 0" in итог.stdout and len(list(очередь.glob("*.call"))) == 3
    (tmp_path / "spool" / ".resend.lock").rmdir()
    итог = запустить(tmp_path, адрес, "--resend")
    assert "дослано 3, осталось 0" in итог.stdout, итог.stdout


# ---------------------------------------------------------------------------
# Контроль качества
# ---------------------------------------------------------------------------

@pytest.fixture()
def атс():
    станции: list[МиниАТС] = []

    def завести(**параметры) -> МиниАТС:
        с = МиниАТС(**параметры)
        станции.append(с)
        return с

    yield завести
    for с in станции:
        с.закрыть()


def test_отказ_номеру_закрывает_опрос_и_не_держит_обзвон(атс, data_dir, monkeypatch):
    с = атс(ответ="notfound")
    _, hub = приложение_кк(data_dir, monkeypatch, с.порт)
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    hub.qc.обзвон()
    опрос = дождаться(hub)
    assert опрос["status"] == survey.ОШИБКА and опрос["sip_code"] == 404, опрос
    assert hub.qc._сбой_телефона_до < time.time(), "обзвон остальных не придерживается"


def test_не_звонить_проверяется_перед_набором(атс, data_dir, monkeypatch):
    с = атс(кнопки=["5", "1"])
    _, hub = приложение_кк(data_dir, monkeypatch, с.порт)
    взвести(hub)
    звонок(hub, src="89991112233")
    assert hub.qc.найти_новые() == 1
    # Номер внесли в другом написании — ждущий опрос снимается сразу.
    hub.qc.в_стоп_лист("+7 999 111-22-33", "просил не звонить")
    assert hub.db.query_one("SELECT status FROM qc_surveys")["status"] == survey.ОТМЕНЁН
    # И если строка всё же ждёт (внесли напрямую в базу) — перед набором.
    hub.db.execute("UPDATE qc_surveys SET status=?, finished_at=NULL", (survey.ЖДЁТ,))
    hub.qc.обзвон()
    опрос = дождаться(hub)
    assert опрос["status"] == survey.ОТМЕНЁН and с.набрано == []


def test_сломанный_голос_не_сжигает_очередь(атс, data_dir, monkeypatch):
    с = атс(кнопки=["5", "1"])
    _, hub = приложение_кк(data_dir, monkeypatch, с.порт,
                           qc_tts_command="/нет/такой/программы {out}")
    взвести(hub)
    звонок(hub)
    hub.qc.найти_новые()
    hub.qc.обзвон()
    конец = time.time() + 10
    while time.time() < конец:
        строка_ = dict(hub.db.query_one("SELECT * FROM qc_surveys"))
        if строка_["status"] == survey.ЖДЁТ and строка_["error"]:
            break
        time.sleep(0.1)
    assert строка_["status"] == survey.ЖДЁТ and строка_["attempts"] == 0, строка_
    assert с.набрано == []


def test_регистрация_держится_между_звонками(атс, data_dir, monkeypatch):
    с = атс()
    _, hub = приложение_кк(data_dir, monkeypatch, с.порт, qc_sip_register=True)
    hub.qc.шаг()
    assert с.регистраций == 1
    assert hub.qc.состояние()["registration"]["state"] == "зарегистрирован"
    # Срок подходит к концу — следующий шаг продлевает без всякого звонка.
    hub.qc._телефон.зарегистрирован_до = time.time() + 10
    hub.qc.шаг()
    assert с.регистраций == 2 and с.набрано == []


def test_cancel_повторяется_пока_станция_не_подтвердит(атс):
    с = атс(ответ="noanswer", глотать_отмену=1)
    телефон = sip.Телефон("127.0.0.1", с.порт, пользователь="900", пароль="secret",
                          местный="127.0.0.1")
    try:
        with pytest.raises(sip.НеОтвечает):
            телефон.позвонить("79990001122", ждать_ответа_с=1)
        assert с.журнал.count("CANCEL") >= 2 and с.отменён.is_set()
    finally:
        телефон.закрыть()


def test_поздний_ответ_на_брошенный_вызов_кладётся(атс):
    с = атс()
    телефон = sip.Телефон("127.0.0.1", с.порт, пользователь="900", пароль="secret",
                          местный="127.0.0.1")
    try:
        звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        разговор = sip.Разговор(телефон, "late-1@127.0.0.1", звук)
        with телефон._замок:
            телефон._разговоры[разговор.call_id] = разговор
        телефон._забыть(разговор)                # вызов брошен до ответа
        ответ = sip.собрать("SIP/2.0 200 OK", [
            ("Via", "SIP/2.0/UDP 127.0.0.1:1;branch=z9hG4bKx"),
            ("From", '"QC" <sip:900@127.0.0.1>;tag=a'), ("To", "<sip:7999@127.0.0.1>;tag=b"),
            ("Call-ID", "late-1@127.0.0.1"), ("CSeq", "2 INVITE"),
            ("Contact", f"<sip:pbx@127.0.0.1:{с.порт}>")])
        с.сокет.sendto(ответ, ("127.0.0.1", телефон.местный_порт))
        конец = time.time() + 3
        while time.time() < конец and "BYE" not in с.журнал:
            time.sleep(0.05)
        assert "ACK" in с.журнал and "BYE" in с.журнал
        звук.close()
    finally:
        телефон.закрыть()


def test_одна_кнопка_двумя_способами_одно_нажатие():
    звук = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        р = sip.Разговор(None, "x", звук)  # type: ignore[arg-type]
        р._кнопка("5", "тон")
        р._кнопка("5", "событие")               # тот же «5» событием — то же нажатие
        р._кнопка("5", "событие")               # снова событием — новое нажатие
        р._кнопка("3", "info")
        нажато = []
        while not р.кнопки.empty():
            нажато.append(р.кнопки.get_nowait())
        assert нажато == ["5", "5", "3"]
    finally:
        звук.close()
