"""Заход 65: кто говорил на каком отрезке записи в разговоре с переводами.

Запись разговора с переводом одна — на канале клиента, — а ног CDR
несколько. Раньше вся запись доставалась одному добавочному. Здесь — окна
участников по ногам, реплики по окнам и оценка каждого участника.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from asrhub.telephony import segments as отрезки

НАЧАЛО = 1_789_000_000.0


def нога(uid: str, агент: str, начало: float, длительность: float, разговор: float, *,
         порядок: int = 0, **поля) -> dict:
    return {"uniqueid": uid, "agent": агент, "started_at": НАЧАЛО + начало,
            "duration": длительность, "billsec": разговор, "sequence": порядок,
            "linkedid": "1789000000.1", "station": "pbx", **поля}


# Клиент звонит, 5 с гудков, оператор 101 говорит 60 с, затем перевод на 102
# с предупреждением: 102 снимает на 55-й секунде (общее окно 5 с), говорит 40 с.
ЗАПИСАННАЯ = нога("pbx:1.1", "101", 0, 65, 60, порядок=1)
ВТОРАЯ = нога("pbx:1.2", "102", 58, 42, 40, порядок=2)
РЕПЛИКИ = [
    {"start": 1.0, "end": 6.0, "text": "Здравствуйте, отдел продаж, слушаю вас."},
    {"start": 7.0, "end": 20.0, "text": "У меня не работает доставка, это ужасно."},
    {"start": 21.0, "end": 50.0, "text": "Понимаю, сейчас переведу на специалиста."},
    {"start": 56.0, "end": 58.0, "text": "Коллега, клиент по доставке."},
    {"start": 61.0, "end": 80.0, "text": "Добрый день, всё исправим, спасибо за терпение!"},
    {"start": 81.0, "end": 94.0, "text": "Спасибо большое, вы очень помогли."},
]


def test_окна_по_ногам_на_времени_записи():
    окна = отрезки.окна(ЗАПИСАННАЯ, [ВТОРАЯ], длительность=95)
    assert [(о["agent"], о["from_s"], о["to_s"]) for о in окна] == [
        ("101", 0.0, 60.0), ("102", 55.0, 95.0)]       # 58+2 гудка −5 ответа; конец — по записи
    assert окна[0]["recorded"] and not окна[1]["recorded"]


def test_реплики_по_участникам_и_оценка_каждого():
    итог = отрезки.по_участникам(ЗАПИСАННАЯ, [ВТОРАЯ], РЕПЛИКИ, длительность=95)
    assert итог is not None
    assert [(о["agent"], о["replicas"]) for о in итог["segments"]] == [("101", 3), ("102", 3)]
    # Общее окно (консультация): реплика на 57-й секунде — уже второго.
    второй = итог["segments"][1]
    assert второй["from_s"] == 56.0 and второй["to_s"] == 94.0
    участники = {у["agent"]: у for у in итог["agents"]}
    assert участники["101"]["talk_s"] == 47.0 and участники["102"]["talk_s"] == 34.0
    assert участники["101"]["share"] + участники["102"]["share"] == pytest.approx(1.0, abs=0.01)
    assert участники["102"]["sentiment"]["label"] == "положительная"
    assert участники["101"]["sentiment"]["score"] < участники["102"]["sentiment"]["score"]


def test_один_участник_не_делится():
    assert отрезки.по_участникам(ЗАПИСАННАЯ, [], РЕПЛИКИ) is None
    тот_же = нога("pbx:1.2", "101", 58, 42, 40, порядок=2)
    assert отрезки.по_участникам(ЗАПИСАННАЯ, [тот_же], РЕПЛИКИ) is None
    без_ответа = нога("pbx:1.3", "103", 58, 20, 0, порядок=3)
    assert отрезки.по_участникам(ЗАПИСАННАЯ, [без_ответа], РЕПЛИКИ) is None


def test_реплика_мимо_окон_уходит_ближайшему():
    итог = отрезки.по_участникам(ЗАПИСАННАЯ, [ВТОРАЯ],
                                 [{"start": 0.0, "end": 1.0, "text": "алло"},
                                  {"start": 120.0, "end": 121.0, "text": "до свидания"}])
    assert [о["agent"] for о in итог["segments"]] == ["101", "102"]


@pytest.fixture()
def сервер(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin_65": {"name": "админ", "role": "admin", "enabled": True},
        "ah_sales_65": {"name": "sales", "role": "user", "enabled": True},
    })
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        yield клиент, app.state.hub


def test_карточка_и_ручка_отдают_участников(сервер):
    клиент, state = сервер
    db = state.db
    job_id = db.create_job({"id": "seg65", "filename": "перевод.wav", "owner": "sales",
                            "media_duration_s": 95.0})
    db.update_job(job_id, status="completed", text="…", finished_at=НАЧАЛО + 200)
    db.save_segments(job_id, РЕПЛИКИ)
    поля = {к: v for к, v in ЗАПИСАННАЯ.items() if к not in ("uniqueid", "station")}
    db.save_call("pbx:1.1", job_id=job_id, owner="sales", station="pbx", answered=1, **поля)
    поля = {к: v for к, v in ВТОРАЯ.items() if к not in ("uniqueid", "station")}
    db.save_call("pbx:1.2", job_id=None, owner="support", station="pbx", answered=1, **поля)

    ответ = клиент.get(f"/api/jobs/{job_id}", headers={"X-API-Key": "ah_admin_65"})
    assert ответ.status_code == 200, ответ.text
    по_участникам = ответ.json()["call"]["by_agent"]
    assert [у["agent"] for у in по_участникам["agents"]] == ["101", "102"]
    ручка = клиент.get(f"/api/jobs/{job_id}/agents", headers={"X-API-Key": "ah_admin_65"})
    assert ручка.status_code == 200 and ручка.json()["by_agent"] == по_участникам
    # Ключ отдела видит своё время, а чужую ногу — без добавочного.
    свой = клиент.get(f"/api/jobs/{job_id}/agents", headers={"X-API-Key": "ah_sales_65"}).json()
    assert [у["agent"] for у in свой["by_agent"]["agents"]] == ["101", "другой участник"]
    assert all(о["uniqueid"] in ("pbx:1.1", "") for о in свой["by_agent"]["windows"])



# --- гудок голосовой почты -------------------------------------------------------

def _тон(частота: float, секунд: float, *, громкость: int = 8000, плывёт: float = 0.0) -> bytes:
    import math
    import struct

    фаза, итог = 0.0, []
    for н in range(int(секунд * 8000)):
        f = частота * (1 + плывёт * math.sin(2 * math.pi * 4 * н / 8000))
        фаза += 2 * math.pi * f / 8000
        итог.append(struct.pack("<h", int(громкость * math.sin(фаза))))
    return b"".join(итог)


def _голос(секунд: float) -> bytes:
    """Похоже на голос: основной тон плывёт, гармоники, шум."""
    import math
    import random
    import struct

    случай = random.Random(65)
    итог = []
    фаза = 0.0
    for н in range(int(секунд * 8000)):
        f0 = 140 * (1 + 0.15 * math.sin(2 * math.pi * 3 * н / 8000))
        фаза += 2 * math.pi * f0 / 8000
        отсчёт = sum(math.sin(k * фаза) / k for k in range(1, 12)) * 3000
        итог.append(struct.pack("<h", max(-32000, min(32000, int(отсчёт + случай.gauss(0, 600))))))
    return b"".join(итог)


def test_гудок_голосовой_почты_узнаётся_а_голос_нет():
    from asrhub.qc import survey

    for частота in (425, 1000, 1400):
        assert survey.гудок(_тон(частота, 0.4)), частота
    assert not survey.гудок(_тон(1000, 0.15))                 # щелчок, не гудок
    assert not survey.гудок(_тон(300, 1.0))                   # ниже полосы гудка
    assert not survey.гудок(_тон(1000, 1.0, плывёт=0.2))      # плывущий тон — не гудок
    assert not survey.гудок(_голос(3.0))
    assert not survey.гудок(bytes(16000))                     # тишина


def test_короткое_приветствие_с_гудком_это_автоответчик():
    import sys

    from asrhub.qc import survey

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_review_61 import Трубка

    тишина = bytes(int(0.3 * 8000) * 2)
    почта = Трубка([], лента=тишина + _голос(1.2) + bytes(int(0.3 * 8000) * 2) + _тон(1000, 0.5))
    assert survey.определить_автоответчик(почта) == "автоответчик"
    человек = Трубка([], лента=тишина + _голос(0.6))
    assert survey.определить_автоответчик(человек) == "человек"


# --- голос Piper ---------------------------------------------------------------------

def test_голос_piper_качается_из_закреплённой_версии(tmp_path):
    from asrhub.errors import ConfigError
    from asrhub.qc import tts

    запросы: list[dict] = []

    def загрузить(**поля):
        запросы.append(поля)
        путь = Path(поля["local_dir"]) / поля["filename"]
        путь.parent.mkdir(parents=True, exist_ok=True)
        путь.write_bytes(b"x" * 1000)
        return str(путь)

    модель = tts.скачать_голос_piper("ru_RU-irina-medium", tmp_path, загрузить=загрузить)
    assert модель == tmp_path / "ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx"
    assert {з["filename"] for з in запросы} == {
        "ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx",
        "ru/ru_RU/irina/medium/ru_RU-irina-medium.onnx.json"}
    assert all(з["repo_id"] == "rhasspy/piper-voices" and з["revision"] == "v1.0.0"
               for з in запросы)
    for плохое in ("irina", "ru_RU-irina", "../ru_RU-irina-medium", "ru_RU-irina-huge"):
        with pytest.raises(ConfigError):
            tts.скачать_голос_piper(плохое, tmp_path, загрузить=загрузить)


def test_piper_зовётся_короткими_ключами():
    """`-m` и `-f` понимают и прежний Piper, и пакет piper-tts."""
    from asrhub.qc import tts

    piper = next(к for и, п, к, г in tts.ДВИЖКИ if п == "piper")
    assert piper == "piper -m {voice} -f {out}"


def test_models_sh_знает_голос():
    import subprocess

    корень = Path(__file__).resolve().parent.parent
    справка = subprocess.run(["bash", str(корень / "scripts" / "models.sh"), "--help"],
                             capture_output=True, text=True, timeout=60)
    assert "voice ru_RU-irina-medium" in справка.stdout
