"""Заход 69: автоответчик моделью — разметка записей опросов и обучение.

Правило длины реплики ошибается на людях, которые отвечают длинно («Добрый
день, компания такая-то, слушаю вас»), и на тихой голосовой почте без гудка.
Здесь — признаки первых секунд звука клиента, логистическая регрессия,
обученная на размеченных записях опросов, её место рядом с правилами и
разметка в журнале.
"""
from __future__ import annotations

import math
import random
import struct
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_65 import _тон  # noqa: E402

ТИШИНА = b"\x00\x00"


def голос(секунд: float, *, громкость: float = 1.0, зерно: int = 69) -> bytes:
    """Похоже на голос: основной тон плывёт, гармоники, шум."""
    случай = random.Random(зерно)
    итог = []
    фаза = 0.0
    for н in range(int(секунд * 8000)):
        f0 = 140 * (1 + 0.15 * math.sin(2 * math.pi * 3 * н / 8000))
        фаза += 2 * math.pi * f0 / 8000
        отсчёт = sum(math.sin(k * фаза) / k for k in range(1, 12)) * 3000 * громкость
        итог.append(struct.pack("<h", max(-32000, min(32000, int(отсчёт + случай.gauss(0, 300))))))
    return b"".join(итог)


def тишина(секунд: float) -> bytes:
    return ТИШИНА * int(секунд * 8000)


def человек_длинно(случай: random.Random) -> bytes:
    """Человек отвечает не сразу, тише и длинно, потом ждёт."""
    return (тишина(0.7 + случай.random() * 0.5)
            + голос(2.6 + случай.random() * 0.6, громкость=0.5, зерно=случай.randrange(10**6))
            + тишина(1.5))


def почта(случай: random.Random) -> bytes:
    """Голосовая почта без гудка: сразу, громко и сплошной речью."""
    return (тишина(0.05 + случай.random() * 0.1)
            + голос(3.5 + случай.random() * 0.5, громкость=1.6, зерно=случай.randrange(10**6)))


def _разметка(n: int = 15) -> list[tuple[list[float], str]]:
    from asrhub.qc import amd_model as A

    случай = random.Random(69)
    return ([(A.признаки_по_окнам(человек_длинно(случай)), A.ЧЕЛОВЕК) for _ in range(n)]
            + [(A.признаки_по_окнам(почта(случай)), A.АВТООТВЕТЧИК) for _ in range(n)])


def test_признаки_первых_секунд():
    from asrhub.qc import amd_model as A

    алло = dict(zip(A.ПРИЗНАКИ, A.признаки(тишина(0.4) + голос(0.6) + тишина(2.0)), strict=True))
    assert алло["тишина_до_с"] == pytest.approx(0.4, abs=0.06)
    assert алло["первая_реплика_с"] == pytest.approx(0.6, abs=0.1)
    assert алло["реплик"] == 1.0 and алло["гудок"] == 0.0
    assert алло["тишина_после_первой_с"] > 1.5
    с_гудком = dict(zip(A.ПРИЗНАКИ, A.признаки(голос(1.2) + тишина(0.3) + _тон(1000, 0.5)),
                        strict=True))
    assert с_гудком["гудок"] == 1.0
    пусто = A.признаки(b"")
    assert len(пусто) == len(A.ПРИЗНАКИ) and пусто[2] == 0.0
    # Размеченная запись — признаки на каждом окне, сколько звука уже слышно.
    окна = A.признаки_по_окнам(голос(5.0))
    assert [о[0] for о in окна] == list(A.ОКНА_С)
    assert len(A.признаки_по_окнам(голос(2.7))) == 2


def test_обучение_и_точность():
    from asrhub.qc import amd_model as A

    with pytest.raises(ValueError, match="Мало размеченных"):
        A.обучить(_разметка(4))
    модель = A.обучить(_разметка())
    assert модель.образцов == 30 and модель.по_классам == {A.ЧЕЛОВЕК: 15, A.АВТООТВЕТЧИК: 15}
    assert модель.точность is not None and модель.точность >= 0.9
    случай = random.Random(7)
    assert модель.вероятность(A.признаки(почта(случай))) > 0.9
    assert модель.вероятность(A.признаки(человек_длинно(случай))) < 0.1


def test_модель_сохраняется_и_перечитывается(tmp_path):
    from asrhub.qc import amd_model as A

    assert A.загрузить(tmp_path) is None
    модель = A.обучить(_разметка())
    A.сохранить(модель, tmp_path)
    прочитанная = A.загрузить(tmp_path)
    assert прочитанная is not None and прочитанная.веса == модель.веса
    # Испорченный файл и чужой набор признаков — модели нет, звонки идут по правилам.
    (tmp_path / A.ФАЙЛ).write_text('{"features": ["другое"]}', encoding="utf-8")
    time.sleep(0.01)
    import os
    os.utime(tmp_path / A.ФАЙЛ, (time.time() + 5, time.time() + 5))
    assert A.загрузить(tmp_path) is None


def test_модель_спасает_длинное_приветствие_человека():
    """Правило длинной речи видит автоответчик; обученная модель — человека."""
    from asrhub.qc import amd_model as A
    from asrhub.qc import survey
    from test_review_61 import Трубка

    модель = A.обучить(_разметка())
    случай = random.Random(11)
    человек = человек_длинно(случай)
    assert survey.определить_автоответчик(Трубка([], лента=человек)) == "автоответчик"
    assert survey.определить_автоответчик(Трубка([], лента=человек), модель=модель) == "человек"
    # Почта и гудок — по-прежнему автоответчик.
    assert survey.определить_автоответчик(Трубка([], лента=почта(случай)),
                                          модель=модель) == "автоответчик"
    с_гудком = тишина(0.2) + голос(1.0) + тишина(0.3) + _тон(1000, 0.5)
    assert survey.определить_автоответчик(Трубка([], лента=с_гудком),
                                          модель=модель) == "автоответчик"
    # Обычное «Алло» модель не ломает.
    алло = тишина(0.3) + голос(0.6, громкость=0.5) + тишина(2.0)
    assert survey.определить_автоответчик(Трубка([], лента=алло), модель=модель) == "человек"


def _запись(путь: Path, клиент: bytes) -> None:
    """Запись опроса: слева клиент, справа сервер (тишина)."""
    import wave

    with wave.open(str(путь), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"".join(клиент[н:н + 2] + ТИШИНА for н in range(0, len(клиент), 2)))


@pytest.fixture()
def клиент(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({"ah_admin_69": {"name": "админ", "role": "admin", "enabled": True},
                               "ah_user_69": {"name": "sales", "role": "user", "enabled": True}})
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as к:
        yield к, app.state.hub


def test_разметка_в_журнале_и_обучение_через_api(клиент):
    from asrhub.qc import amd_model as A

    к, state = клиент
    админ = {"X-API-Key": "ah_admin_69"}
    каталог = state.qc.каталог_записей_опросов()
    каталог.mkdir(parents=True, exist_ok=True)
    случай = random.Random(5)
    for н in range(24):
        вид = A.ЧЕЛОВЕК if н % 2 else A.АВТООТВЕТЧИК
        путь = каталог / f"s{н}.wav"
        _запись(путь, человек_длинно(случай) if вид == A.ЧЕЛОВЕК else почта(случай))
        state.db.execute("INSERT INTO qc_surveys (id, number, created_at, status, recording) "
                         "VALUES (?,?,?,?,?)", (f"s{н}", "+79001234567", time.time(), "автоответчик",
                                                str(путь)))
        ответ = к.put(f"/api/qc/surveys/s{н}/amd_label", headers=админ, json={"label": вид})
        assert ответ.status_code == 200, ответ.text
    assert ответ.json()["features"]["первая_реплика_с"] > 2.0
    журнал = к.get("/api/qc/surveys", headers=админ).json()["items"]
    assert {з["id"]: з["amd_label"] for з in журнал}["s1"] == A.ЧЕЛОВЕК
    # Метки — только администратору, только из закрытого списка, только с записью.
    assert к.put("/api/qc/surveys/s1/amd_label", headers={"X-API-Key": "ah_user_69"},
                 json={"label": A.ЧЕЛОВЕК}).status_code == 403
    assert к.put("/api/qc/surveys/s1/amd_label", headers=админ,
                 json={"label": "робот"}).status_code == 400
    state.db.execute("INSERT INTO qc_surveys (id, number, created_at, status) VALUES "
                     "('без', '+79001234567', ?, 'пройден')", (time.time(),))
    assert к.put("/api/qc/surveys/без/amd_label", headers=админ,
                 json={"label": A.ЧЕЛОВЕК}).status_code == 404
    сводка = к.get("/api/qc/amd", headers=админ).json()
    assert сводка["labels"] == {A.ЧЕЛОВЕК: 12, A.АВТООТВЕТЧИК: 12} and сводка["model"] is None
    обучено = к.post("/api/qc/amd/train", headers=админ)
    assert обучено.status_code == 200, обучено.text
    assert обучено.json()["model"]["accuracy"] >= 0.9
    assert к.get("/api/qc/amd", headers=админ).json()["model"]["samples"] == 24
    assert A.загрузить(Path(state.settings.paths.data)) is not None
    # Разметка переживает удаление записи по сроку: признаки — в базе.
    for путь in каталог.glob("*.wav"):
        путь.unlink()
    assert к.post("/api/qc/amd/train", headers=админ).status_code == 200
    # Снять метку; удаление опросов по номеру субъекта уносит и разметку.
    assert к.put("/api/qc/surveys/s1/amd_label", headers=админ, json={"label": ""}).json() \
        == {"id": "s1", "label": "", "features": {}}
    state.db.erase_qc("+79001234567")
    assert state.db.qc_amd_labels() == []


def test_уборка_потока_не_теряется_при_отмене(клиент, monkeypatch):
    """Расход диктовки терялся примерно в половине сессий.

    Тестовый клиент (и остановка сервера) отменяет обработчик сразу после
    закрытия сокета. `await` на отменённой задаче отменял и ещё не начатую
    работу пула потоков: запись расхода и закрытие сессии не выполнялись.
    Уборка идёт под защитой от отмены; медленная запись метрик потока
    делает так, что отмена приходит наверняка до уборки.
    """
    import array
    import json
    import threading

    from asrhub.api import routes_jobs, routes_ws
    from asrhub.streaming import StreamSession

    к, state = клиент
    записано: list[float] = []
    закрыто = threading.Semaphore(0)
    прежнее_закрытие = StreamSession.close

    def закрыть(сессия, *a, **kw):
        try:
            return прежнее_закрытие(сессия, *a, **kw)
        finally:
            закрыто.release()

    monkeypatch.setattr(StreamSession, "close", закрыть)
    monkeypatch.setattr(routes_ws, "_record_stream", lambda *a, **kw: time.sleep(0.3))
    monkeypatch.setattr(routes_jobs, "учесть_расход",
                        lambda state, owner, *, audio_s=0.0, **kw: записано.append(audio_s))
    звук = array.array("h", [int(3000 * math.sin(н / 8)) for н in range(16000)]).tobytes()
    for _ in range(4):
        with к.websocket_connect("/api/stream?api_key=ah_user_69") as сокет:
            сокет.send_text(json.dumps({"format": "pcm_s16le"}))
            assert json.loads(сокет.receive_text())["type"] == "ready"
            сокет.send_bytes(звук)
            сокет.send_text(json.dumps({"type": "finish"}))
            while json.loads(сокет.receive_text())["type"] != "done":
                pass
    for н in range(4):
        assert закрыто.acquire(timeout=10), f"сессия {н + 1} из 4 не закрыта"
    конец = time.monotonic() + 10
    while len(записано) < 4 and time.monotonic() < конец:
        time.sleep(0.05)
    assert len(записано) == 4 and all(с > 0 for с in записано), записано


def test_копия_с_чужим_паролем_не_расшифровывается_в_мусор(tmp_path: Path):
    """AES-CBC без подписи: неверный пароль иногда «расшифровывал» копию.

    Примерно в одном случае из двухсот пятидесяти выравнивание последнего
    блока сходилось случайно, openssl завершался успешно, и мусор шёл
    дальше. Соль здесь закреплена, а чужой пароль подобран так, что
    выравнивание сходится.
    """
    import shutil
    import subprocess

    from asrhub import backup
    from asrhub.errors import ConfigError

    if shutil.which("openssl") is None:
        pytest.skip("нет openssl")
    исходный = tmp_path / "копия.asrhub.tar.gz"
    исходный.write_bytes(b"\x1f\x8b" + "данные".encode() * 100)
    соль = "0123456789abcdef"
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                    "-S", соль, "-in", str(исходный), "-out", str(tmp_path / "тело"),
                    "-pass", "pass:длинный-пароль"], check=True, capture_output=True)
    шифр = tmp_path / "копия.asrhub.tar.gz.enc"
    шифр.write_bytes(b"Salted__" + bytes.fromhex(соль) + (tmp_path / "тело").read_bytes())
    assert backup.зашифрована(шифр)
    assert backup.расшифровать(шифр, "длинный-пароль", tmp_path / "своя").read_bytes() \
        == исходный.read_bytes()
    чужой = "чужой-225"
    сам = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
                          "-in", str(шифр), "-out", str(tmp_path / "мусор"),
                          "-pass", f"pass:{чужой}"], capture_output=True, check=False)
    assert сам.returncode == 0, "у этого openssl выравнивание с чужим паролем не сходится"
    with pytest.raises(ConfigError, match="пароль не подходит"):
        backup.расшифровать(шифр, чужой, tmp_path / "мимо")
    assert not (tmp_path / "мимо").exists()
    # Файл базы SQLite своим паролем — расшифровывается.
    база = tmp_path / "asrhub.db"
    база.write_bytes(b"SQLite format 3\x00" + bytes(100))
    assert backup.расшифровать(backup.зашифровать(база, "п"), "п",
                               tmp_path / "база").read_bytes() == база.read_bytes()
