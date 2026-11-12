"""Заход 54, участок «безопасность и вход»: обезличивание, тело запроса,
выражения, параметры моделей, ключи, вход, веб-сокеты, квоты.

Каждая проверка закрывает находку ревизии 3.1.20 (отчёты 01-security,
02-concurrency, 03-api-core и связанные с ними) и падала до исправления.
Общий корень почти у всех один: защита стояла на одном пути и
отсутствовала на соседнем, который делает то же самое — билетом вместо
ключа, веб-сокетом вместо REST, повтором вместо загрузки.
"""
from __future__ import annotations

import io
import json
import math
import struct
import subprocess
import sys
import time
import types
import wave
import zipfile
from pathlib import Path
from typing import Any

import pytest
from asrhub.api import create_app
from asrhub.config import load, хеш_ключа
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parent.parent

АДМИН = "ah_admin_54_0000000000"
АЛИСА = "ah_alice_54_0000000000"
МАСКА = "ah_mask__54_0000000000"
ЧИТАТЕЛЬ = "ah_read__54_0000000000"
ПРОДАЖИ = "ah_sales_54_0000000000"
КВОТА = "ah_quota_54_0000000000"
ЧАСЫ = "ah_hours_54_0000000000"

НОМЕР = "79161234567"


def H(ключ: str) -> dict[str, str]:
    return {"X-API-Key": ключ}


def _wav(секунд: float = 1.0) -> bytes:
    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(и / 8)))
                                  for и in range(int(16000 * секунд))))
    return буфер.getvalue()


@pytest.fixture()
def настройки(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    н = load()
    н.api_keys.update({
        АДМИН: {"name": "админ", "role": "admin", "enabled": True},
        АЛИСА: {"name": "alice", "role": "user", "enabled": True},
        МАСКА: {"name": "маска", "role": "user", "enabled": True, "mask_pii": True},
        ЧИТАТЕЛЬ: {"name": "читатель", "role": "readonly", "enabled": True},
        ПРОДАЖИ: {"name": "продажи", "role": "user", "enabled": True, "group": "sales"},
        КВОТА: {"name": "квота", "role": "user", "enabled": True,
                "quota_jobs_per_day": 1},
        ЧАСЫ: {"name": "часы", "role": "user", "enabled": True,
               "quota_audio_hours_per_day": 0.0005},
    })
    н.values["rate_limit_per_minute"] = 0
    н.values["login_rate_limit"] = 0
    return н


@pytest.fixture()
def сервер(настройки):
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        yield клиент


@pytest.fixture()
def сервер_с_очередью(настройки):
    app = create_app(настройки, start_queue=True)
    with TestClient(app) as клиент:
        yield клиент


def _hub(клиент: TestClient) -> Any:
    return клиент.app.state.hub


def _поставить(клиент: TestClient, ключ: str, имя: str = "звонок.wav",
               секунд: float = 1.0, **поля: Any):
    return клиент.post("/api/jobs", headers=H(ключ),
                       files={"file": (имя, _wav(секунд), "audio/wav")},
                       data={k: (json.dumps(v, ensure_ascii=False) if isinstance(v, dict)
                                 else v) for k, v in поля.items()})


def _дождаться(клиент: TestClient, ключ: str, job_id: str, срок: float = 90.0) -> dict:
    конец = time.monotonic() + срок
    while time.monotonic() < конец:
        задание = клиент.get(f"/api/jobs/{job_id}", headers=H(ключ)).json()
        if задание.get("status") in ("completed", "failed", "cancelled", "no_speech", "short"):
            return задание
        time.sleep(0.3)
    raise AssertionError(f"задание {job_id} не закончилось: {задание.get('status')}")


#: Реплика с номером телефона и картой: телефон — пять слов, карта — четыре.
ТЕКСТ = ("Здравствуйте, мой телефон 8 916 765 43 21, карта 4276 3800 1234 5679, "
         "почта ivan@example.com, спасибо")


def _задание_с_номером(клиент: TestClient, owner: str = "маска",
                       имя: str = f"+{НОМЕР} Иванов.wav") -> str:
    """Готовое задание с репликой, полной персональных данных, прямо в базе."""
    db = _hub(клиент).db
    job_id = db.create_job({"filename": имя, "status": "completed", "owner": owner,
                            "text": ТЕКСТ, "model": "demo-simulator", "engine": "demo",
                            "language": "ru", "media_duration_s": 5.0})
    слова = []
    for номер, слово in enumerate(ТЕКСТ.split()):
        слова.append({"word": слово, "start": номер * 0.3, "end": номер * 0.3 + 0.2})
    db.save_segments(job_id, [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00",
                               "text": ТЕКСТ, "words": слова}])
    return job_id


# ---------------------------------------------------------------------------
# Обезличивание: одно место, один источник — участник проверки доступа
# ---------------------------------------------------------------------------


def test_билет_не_снимает_обезличивание(сервер):
    """01-security №1, 03-api-core №3, 09-content №1.

    Прослойка маскирования разбирала ключ заново и билета не знала: по
    `?ticket=` ключ с mask_pii получал полные телефоны во всех ответах JSON.
    """
    job_id = _задание_с_номером(сервер)
    билет = сервер.post("/api/auth/ticket", headers=H(МАСКА)).json()["ticket"]
    карточка = сервер.get(f"/api/jobs/{job_id}?ticket={билет}&with_segments=true")
    assert карточка.status_code == 200, карточка.text
    assert НОМЕР not in карточка.text and "4276" not in карточка.text
    assert "[телефон]" in карточка.json()["filename"]
    билет = сервер.post("/api/auth/ticket", headers=H(МАСКА)).json()["ticket"]
    список = сервер.get(f"/api/jobs?ticket={билет}")
    assert список.status_code == 200
    assert НОМЕР not in список.text


def test_лента_событий_обезличена_для_ключа_с_маской(сервер):
    """01-security №2: события /ws уходили ключу с mask_pii как есть."""
    лента = сервер.app.state.events
    лента.publish("job.queued", {"id": "j-маска", "filename": f"+{НОМЕР} Иванов.wav",
                                 "_owner": "маска"})
    with сервер.websocket_connect(f"/ws?api_key={МАСКА}") as сокет:
        привет = сокет.receive_text()
        assert НОМЕР not in привет, привет[:300]
        assert "[телефон]" in привет
        # И живое событие — тоже обезличено.
        лента.publish("job.completed", {"id": "j-маска", "filename": f"+{НОМЕР}.wav",
                                        "_owner": "маска"})
        событие = сокет.receive_text()
    assert НОМЕР not in событие and "[телефон]" in событие


def test_выдержки_поиска_обезличены(сервер):
    """09-content №2а: знаки подсветки рвали номер, край выдержки — его начало."""
    job_id = _задание_с_номером(сервер)
    по_списку = сервер.get("/api/jobs", params={"search": "916"}, headers=H(МАСКА))
    assert по_списку.status_code == 200, по_списку.text
    найдено = [з for з in по_списку.json()["items"] if з["id"] == job_id]
    assert найдено and найдено[0].get("match"), по_списку.text[:500]
    выдержка = найдено[0]["match"]["snippet"]
    assert "916" not in выдержка and "765" not in выдержка, выдержка
    assert "[телефон]" in выдержка
    по_записи = сервер.get(f"/api/jobs/{job_id}/search", params={"q": "карта"},
                           headers=H(МАСКА))
    assert по_записи.status_code == 200
    assert "4276" not in по_записи.text and "1234" not in по_записи.text, по_записи.text
    # Обычному ключу — полный текст: маскирование не задевает остальных.
    свой = _задание_с_номером(сервер, owner="alice")
    полный = сервер.get(f"/api/jobs/{свой}/search", params={"q": "916"}, headers=H(АЛИСА))
    assert "916" in полный.text


def _строки(значение: Any) -> list[str]:
    """Все строковые значения ответа, кроме идентификаторов и путей."""
    if isinstance(значение, dict):
        return [с for к, з in значение.items()
                if к not in ("id", "job_id", "file_path", "result_path", "source_path")
                for с in _строки(з)]
    if isinstance(значение, list):
        return [с for з in значение for с in _строки(з)]
    return [значение] if isinstance(значение, str) else []


def test_пословные_метки_обезличены_по_реплике(сервер):
    """06-pipeline №5, 09-content №2б: номер — несколько слов, по одному не узнаётся."""
    job_id = _задание_с_номером(сервер)
    for адрес in (f"/api/jobs/{job_id}/segments", f"/api/jobs/{job_id}?with_segments=true",
                  f"/api/jobs/{job_id}/download?fmt=json"):
        ответ = сервер.get(адрес, headers=H(МАСКА))
        assert ответ.status_code == 200, (адрес, ответ.text[:300])
        # Ищем в строках ответа, кроме идентификаторов: в случайном номере
        # задания и в метках времени те же цифры встречаются сами по себе.
        строки = " ".join(_строки(ответ.json()))
        for кусок in ("4276", "3800", "5679", "765"):
            assert кусок not in строки, (адрес, кусок)
    слова = сервер.get(f"/api/jobs/{job_id}/segments", headers=H(МАСКА)).json()[
        "items"][0]["words"]
    assert [с["word"] for с in слова].count("[карта]") == 4
    # Время слов сохраняется — по нему строят субтитры и заглушают звук.
    assert all("start" in с for с in слова)


def test_основы_тем_и_обороты_обезличены():
    """09-content №3: номер через дефис становился ключевым словом и темой."""
    from asrhub.content import masking
    from asrhub.content.keywords import _значимые

    assert not [с for _, с in _значимые("перезвоните на 916-123-45-67 или 4111-1111-1111-1111")
                if any(ц.isdigit() for ц in с)]
    итог = masking.mask_payload({
        "keywords": [{"stem": "916-123-45-67", "word": "916-123-45-67"}],
        "phrases": [{"phrase": "карту 4111-1111-1111-1111"}],
        "topics": [{"stems": ["4111-1111-1111-1111", "доставк"]}],
        "entities": {"dates": ["12.05.1980", "15 мая"]}})
    текст = json.dumps(итог, ensure_ascii=False)
    assert "916" not in текст and "4111" not in текст and "1980" not in текст, текст
    assert "доставк" in текст and "15 мая" in текст


def test_выгрузка_word_своим_файлом_и_без_следов(сервер):
    """02-concurrency №1, 03-api-core №9: общий tmp/<id>.docx на всех и навсегда."""
    job_id = _задание_с_номером(сервер)
    tmp = _hub(сервер).settings.paths.tmp
    # След прежней версии: документ, оставшийся в tmp от старой выгрузки.
    (tmp / f"{job_id}.docx").write_bytes(b"old")
    обезличенный = сервер.get(f"/api/jobs/{job_id}/download?fmt=docx", headers=H(МАСКА))
    полный = сервер.get(f"/api/jobs/{job_id}/download?fmt=docx", headers=H(АДМИН))
    assert обезличенный.status_code == 200 and полный.status_code == 200
    with zipfile.ZipFile(io.BytesIO(обезличенный.content)) as книга:
        xml = книга.read("word/document.xml").decode("utf-8")
    assert "916" not in xml and "[телефон]" in xml
    with zipfile.ZipFile(io.BytesIO(полный.content)) as книга:
        assert "916" in книга.read("word/document.xml").decode("utf-8")
    # Новые выгрузки за собой убирают, а след прежней убирает удаление записи.
    assert not [п.name for п in tmp.glob(f"{job_id}.*") if п.name != f"{job_id}.docx"]
    assert сервер.delete(f"/api/jobs/{job_id}", headers=H(АДМИН)).status_code == 200
    assert not list(tmp.glob(f"{job_id}*")), list(tmp.iterdir())


def test_выгрузка_word_без_python_docx_отдаёт_markdown(сервер, monkeypatch):
    """03-api-core, недоделки: без python-docx маршрут отдавал несуществующий .docx."""
    import sys

    job_id = _задание_с_номером(сервер)
    monkeypatch.setitem(sys.modules, "docx", None)           # «пакет не установлен»
    ответ = сервер.get(f"/api/jobs/{job_id}/download?fmt=docx", headers=H(АДМИН))
    assert ответ.status_code == 200, ответ.text[:300]
    assert ".md" in ответ.headers.get("content-disposition", ""), ответ.headers
    assert ответ.content.strip(), "пустой документ"


def test_исходная_запись_ключу_с_маской_только_заглушённая(сервер):
    """03-api-core №3: /audio отдавал ключу с mask_pii голос с номером карты."""
    hub = _hub(сервер)
    исходник = hub.settings.paths.uploads / "up_54_маска.wav"
    исходник.write_bytes(_wav(0.5))
    job_id = _задание_с_номером(сервер)
    hub.db.update_job(job_id, file_path=str(исходник))
    отказ = сервер.get(f"/api/jobs/{job_id}/audio", headers=H(МАСКА))
    assert отказ.status_code == 403, отказ.text
    assert "redact" in отказ.text
    from asrhub.db import путь_отредактированной

    копия = путь_отредактированной(исходник)
    копия.write_bytes("RIFF-заглушено".encode())
    ответ = сервер.get(f"/api/jobs/{job_id}/audio", headers=H(МАСКА))
    assert ответ.status_code == 200 and ответ.content == "RIFF-заглушено".encode()
    assert НОМЕР not in (ответ.headers.get("content-disposition") or "")
    # Остальным — исходник, как раньше.
    свой = сервер.get(f"/api/jobs/{job_id}/audio", headers=H(АДМИН))
    assert свой.status_code == 200 and свой.content == исходник.read_bytes()


# ---------------------------------------------------------------------------
# Тело запроса: доступ — до чтения, предел — по виду тела
# ---------------------------------------------------------------------------


def _asgi(app: Any, method: str, path: str, заголовки: dict[str, str],
          куски: list[bytes]) -> tuple[dict[str, Any], dict[str, int]]:
    """Запрос мимо TestClient: видно, сколько тела приложение прочитало."""
    import asyncio

    прочитано = {"байт": 0, "вызовов": 0}
    ответ: dict[str, Any] = {"body": b""}
    очередь = list(куски)

    async def receive() -> dict[str, Any]:
        прочитано["вызовов"] += 1
        if очередь:
            кусок = очередь.pop(0)
            прочитано["байт"] += len(кусок)
            return {"type": "http.request", "body": кусок, "more_body": bool(очередь)}
        return {"type": "http.disconnect"}

    async def send(сообщение: dict[str, Any]) -> None:
        if сообщение["type"] == "http.response.start":
            ответ["status"] = сообщение["status"]
            ответ["headers"] = {к.decode(): з.decode()
                                for к, з in сообщение.get("headers") or []}
        elif сообщение["type"] == "http.response.body":
            ответ["body"] += сообщение.get("body") or b""

    длина = sum(len(к) for к in куски)
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "",
        "headers": [(к.lower().encode("latin-1"), з.encode("utf-8"))
                    for к, з in {**заголовки, "content-length": str(длина),
                                 "host": "testserver"}.items()],
        "client": ("10.9.8.7", 5555), "server": ("testserver", 80), "state": {},
    }
    asyncio.run(app(scope, receive, send))
    return ответ, прочитано


def _multipart(размер: int) -> tuple[str, list[bytes]]:
    граница = "----54граница".encode().hex()
    начало = (f"--{граница}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    конец = f"\r\n--{граница}--\r\n".encode()
    тело = [начало] + [b"\x00" * (1 << 16)] * (размер // (1 << 16)) + [конец]
    return f"multipart/form-data; boundary={граница}", тело


def test_файл_без_ключа_отвергается_до_чтения_тела(настройки):
    """01-security №3, 03-api-core №2: тело принималось целиком до проверки ключа."""
    app = create_app(настройки, start_queue=False)
    тип, тело = _multipart(4 << 20)
    ответ, прочитано = _asgi(app, "POST", "/api/jobs", {"content-type": тип}, тело)
    assert ответ["status"] == 401, ответ
    assert прочитано["байт"] == 0, f"до отказа прочитано {прочитано['байт']} байт"
    # С отозванным (неизвестным) ключом — так же.
    ответ, прочитано = _asgi(app, "POST", "/api/jobs/batch",
                             {"content-type": тип, "x-api-key": "ah_нет_такого"}, тело)
    assert ответ["status"] == 401 and прочитано["байт"] == 0
    # А с действующим ключом тело читается как обычно.
    ответ, прочитано = _asgi(app, "POST", "/api/jobs",
                             {"content-type": тип, "x-api-key": АЛИСА}, тело)
    assert прочитано["байт"] > 0


def test_тело_без_файла_ограничено_своим_пределом(настройки):
    """Предел был один на всех — 2 ГБ; JSON входа разбирался в память целиком."""
    настройки.values["max_request_mb"] = 2
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        большое = b'{"username": "x", "password": "' + b"y" * (3 << 20) + b'"}'
        ответ = клиент.post("/api/auth/login", content=большое,
                            headers={"Content-Type": "application/json"})
        assert ответ.status_code == 413, ответ.text[:200]
        assert "max_request_mb" in ответ.json().get("hint", "")
        # Файл тех же размеров — в пределах max_upload_mb и принимается.
        файл = клиент.post("/api/jobs", headers=H(АЛИСА),
                           files={"file": ("a.wav", b"\x00" * (3 << 20), "audio/wav")})
        assert файл.status_code != 413, файл.text[:200]


def test_предел_попыток_входа_до_чтения_тела(настройки):
    """01-security, недоделки: предел входа проверялся после разбора тела."""
    настройки.values["login_rate_limit"] = 2
    app = create_app(настройки, start_queue=False)
    тело = [b'{"username": "nobody", "password": "wrong-password"}']
    for _ in range(2):
        ответ, _ = _asgi(app, "POST", "/api/auth/login",
                         {"content-type": "application/json"}, list(тело))
        assert ответ["status"] == 401, ответ
    ответ, прочитано = _asgi(app, "POST", "/api/auth/login",
                             {"content-type": "application/json"}, list(тело))
    assert ответ["status"] == 429, ответ
    assert прочитано["байт"] == 0, "тело прочитано до отказа по частоте"
    # 03-api-core, недоделки: срок повтора — и в заголовке, его читает агент.
    assert int(ответ["headers"].get("retry-after", "0")) >= 1


# ---------------------------------------------------------------------------
# Регулярные выражения, которые останавливают процесс
# ---------------------------------------------------------------------------


def _в_отдельном_процессе(код: str, срок: float = 30.0) -> subprocess.CompletedProcess:
    """`re` держит GIL: зависание проверяется только из другого процесса."""
    return subprocess.run([sys.executable, "-c", код], capture_output=True, text=True,
                          timeout=срок, cwd=str(ROOT),
                          env={"PYTHONPATH": str(ROOT / "server"), "PATH": "/usr/bin:/bin"})


def test_выражение_словаря_от_задания_отвергается(сервер):
    """06-pipeline №1: «re:…» от любого ключа с правом записи вешал сервер."""
    опасное = {"glossary": {"re:(\\w+\\s?)+$": "x"}}
    ответ = _поставить(сервер, АЛИСА, settings=опасное)
    assert ответ.status_code == 400, ответ.text
    # Безопасное — тоже не от задания: выражения задаёт администратор.
    своё = _поставить(сервер, АЛИСА, settings={"glossary": {"re:\\bгига\\b": "Гига"}})
    assert своё.status_code == 400, своё.text
    assert "администратор" in своё.text
    # Повторить выражение сервера заданию можно — так шлёт словарь интерфейс.
    _hub(сервер).settings.values["glossary"] = {"re:\\bгига\\b": "Гига"}
    тот_же = _поставить(сервер, АЛИСА, settings={"glossary": {"re:\\bгига\\b": "Гига",
                                                                "сбер": "Сбер"}})
    assert тот_же.status_code == 200, тот_же.text


def test_опасное_выражение_не_принимается_и_от_администратора(сервер):
    ответ = сервер.put("/api/settings", headers=H(АДМИН),
                       json={"glossary": {"re:(\\w+\\s?)+$": "x"}})
    assert ответ.status_code == 400, ответ.text
    хорошее = сервер.put("/api/settings", headers=H(АДМИН),
                         json={"glossary": {"re:\\bверсия (\\d+) точка (\\d+)": "версия \\1.\\2"}})
    assert хорошее.status_code == 200, хорошее.text


def test_словарь_не_исполняет_выражение_с_вложенным_повтором():
    """Последний рубеж — сам конвейер: выражение, попавшее в задание раньше."""
    код = (
        "from asrhub.pipeline.postprocess import apply_glossary\n"
        "текст = ' '.join(['слово'] * 40) + '!'\n"
        "итог, n = apply_glossary(текст, {'re:(\\\\w+\\\\s?)+$': 'x'})\n"
        "print(n, итог == текст)\n")
    итог = _в_отдельном_процессе(код, срок=40)
    assert итог.returncode == 0, итог.stderr
    assert итог.stdout.split() == ["0", "True"], итог.stdout


def test_подпись_стороны_разбирается_за_один_проход():
    """11-llm №1: 4 КБ пробелов в /api/jobs/text останавливали сервер на 8 с."""
    код = (
        "from asrhub import textchat\n"
        "реплики, _ = textchat.разобрать('клиент: добрый день\\n' + ' ' * 40000 + '!')\n"
        "print(len(реплики), реплики[0]['speaker'])\n")
    итог = _в_отдельном_процессе(код, срок=30)
    assert итог.returncode == 0, итог.stderr
    assert итог.stdout.split() == ["1", "клиент"], итог.stdout


def test_подпись_стороны_как_прежде():
    from asrhub import textchat

    реплики, _ = textchat.разобрать(
        "Клиент: добрый день\n  Анна Петровна :  есть вопрос\nпродолжение\nвремя: 12:30")
    assert [(р["speaker"], р["text"]) for р in реплики] == [
        ("Клиент", "добрый день"), ("Анна Петровна", "есть вопрос продолжение"),
        ("время", "12:30")]
    без_пробела, _ = textchat.разобрать("клиент:текст")
    assert без_пробела[0]["speaker"] == "—"


# ---------------------------------------------------------------------------
# Имена моделей из задания не уходят в загрузчик
# ---------------------------------------------------------------------------


def test_имена_вспомогательных_моделей_задаёт_только_сервер(сервер):
    """06-pipeline №2, 07-engines №1: чужой репозиторий с токеном сервера."""
    чужое = {"diarization_model": "attacker/evil-pipeline", "align_model": "attacker/a",
             "alignment_acoustic_model": "/tmp/uploaded/dir",
             "alignment_dictionary": "../../etc"}
    настройки = _hub(сервер).settings
    итог = настройки.merged(чужое)
    for ключ, значение in чужое.items():
        assert итог[ключ] != значение, ключ
        assert итог[ключ] == настройки.get(ключ)
    задание = _поставить(сервер, АЛИСА, settings=чужое)
    assert задание.status_code == 200, задание.text
    assert not set(чужое.values()) & set(map(str, задание.json()["params"].values()))


# ---------------------------------------------------------------------------
# Отозванный ключ, созданный при первом запуске
# ---------------------------------------------------------------------------


def test_отозванный_автоключ_не_воскресает(data_dir: Path, monkeypatch):
    """03-api-core №1: после перезапуска ключ поднимался из api-key.txt."""
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text("server:\n  auth_enabled: true\n", encoding="utf-8")
    первый = load()
    # С 3.1.32 ключ первого запуска лежит в настройках хешем: сам ключ — только
    # в api-key.txt, а словарь ключей находит его и по нему, и по хешу.
    (имя,) = list(первый.api_keys)
    ключ = (data_dir / "api-key.txt").read_text(encoding="utf-8").strip()
    assert ключ in первый.api_keys and имя in (ключ, хеш_ключа(ключ))
    app = create_app(первый, start_queue=False)
    with TestClient(app) as клиент:
        ответ = клиент.delete(f"/api/keys/{ключ[:16]}", headers=H(ключ))
        assert ответ.status_code == 200, ответ.text
        assert ответ.json()["persisted"] is True
        assert клиент.get("/api/jobs", headers=H(ключ)).status_code == 401
    второй = load()
    assert ключ not in второй.api_keys, "отозванный ключ снова действует"


# ---------------------------------------------------------------------------
# Вход: тёзки, каталог, блокировка, регистр, кука
# ---------------------------------------------------------------------------


def test_учётная_запись_не_заводится_тёзкой_ключа(сервер):
    """03-api-core №8: тёзка ключа видел весь его архив."""
    for имя in ("alice", "ALICE"):
        ответ = сервер.post("/api/users", headers=H(АДМИН),
                            json={"username": имя, "password": "пароль-надёжный"})
        assert ответ.status_code == 400, ответ.text
    assert сервер.post("/api/users", headers=H(АДМИН),
                       json={"username": "alicia", "password": "пароль-надёжный"}
                       ).status_code == 200


def test_кириллица_в_логине_без_учёта_регистра(сервер):
    """03-api-core №19б: NOCASE в SQLite — только латиница."""
    assert сервер.post("/api/users", headers=H(АДМИН), json={
        "username": "Иванов", "password": "пароль-иванова",
        "must_change_password": False}).status_code == 200
    второй = сервер.post("/api/users", headers=H(АДМИН),
                         json={"username": "иванов", "password": "пароль-иванова"})
    assert второй.status_code == 400, второй.text
    вход = сервер.post("/api/auth/login",
                       json={"username": "иванов", "password": "пароль-иванова"})
    assert вход.status_code == 200, вход.text
    assert вход.json()["user"]["username"] == "Иванов"


def test_истёкшая_блокировка_начинает_счёт_заново(сервер):
    """03-api-core №7а: одна неверная попытка раз в 15 минут держала admin запертым."""
    from asrhub.accounts import MAX_FAILED_ATTEMPTS

    accounts = _hub(сервер).accounts
    запись = accounts.create("жертва", "пароль-жертвы")
    for _ in range(MAX_FAILED_ATTEMPTS):
        сервер.post("/api/auth/login", json={"username": "жертва", "password": "мимо"})
    accounts.db.execute("UPDATE users SET locked_until=? WHERE id=?",
                        [time.time() - 1, запись.id])
    сервер.post("/api/auth/login", json={"username": "жертва", "password": "мимо"})
    ответ = сервер.post("/api/auth/login",
                        json={"username": "жертва", "password": "пароль-жертвы"})
    assert ответ.status_code == 200, ответ.text


def test_неудачный_вход_отвечает_одинаково(сервер):
    """03-api-core №7б: «заблокирован» и «отключена» выдавали существующий логин."""
    from asrhub.accounts import MAX_FAILED_ATTEMPTS

    accounts = _hub(сервер).accounts
    отключённая = accounts.create("уволенный", "пароль-уволенного")
    accounts.update(отключённая.id, enabled=False)
    accounts.create("запертый", "пароль-запертого")
    for _ in range(MAX_FAILED_ATTEMPTS):
        сервер.post("/api/auth/login", json={"username": "запертый", "password": "мимо"})

    def сообщение(логин: str, пароль: str) -> str:
        ответ = сервер.post("/api/auth/login", json={"username": логин, "password": пароль})
        assert ответ.status_code == 401, ответ.text
        return ответ.json()["message"]

    образец = сообщение("выдуманный", "любой-пароль")
    assert сообщение("уволенный", "неверный-пароль") == образец
    assert сообщение("запертый", "неверный-пароль") == образец
    # Во время блокировки и верный пароль не подсказывает, что угадан.
    assert сообщение("запертый", "пароль-запертого") == образец
    # А тому, кто пароль знает, честно говорят, что запись отключена.
    assert "отключена" in сообщение("уволенный", "пароль-уволенного")


def test_отключённого_администратора_можно_удалить(сервер):
    """03-api-core №19а: «последний администратор» считал и отключённого."""
    accounts = _hub(сервер).accounts
    второй = accounts.create("admin2", "пароль-второго", role="admin")
    accounts.update(второй.id, enabled=False)
    ответ = сервер.delete(f"/api/users/{второй.id}", headers=H(АДМИН))
    assert ответ.status_code == 200, ответ.text


def test_кука_сессии_со_сроком_и_продлением(сервер):
    """03-api-core №19в: Expires 2083 года и кука, не продлеваемая при работе."""
    accounts = _hub(сервер).accounts
    accounts.create("работник", "пароль-работника", must_change_password=False)
    вход = сервер.post("/api/auth/login",
                       json={"username": "работник", "password": "пароль-работника"})
    кука = вход.headers.get("set-cookie") or ""
    assert "2083" not in кука and str(time.gmtime().tm_year) in кука, кука
    # Сессия «давно не видела» — сервер продлевает её и переставляет куку.
    accounts.db.execute("UPDATE sessions SET last_seen=?", [time.time() - 3600])
    ответ = сервер.get("/api/auth/me")
    assert ответ.status_code == 200
    assert "asrhub_session=" in (ответ.headers.get("set-cookie") or "")


class _ЗаписьКаталога:
    def __init__(self, группы: list[str]) -> None:
        self.memberOf = list(группы)
        self.displayName = ["Человек Из Каталога"]


class _Связь:
    def __init__(self, группы: list[str]) -> None:
        self.entries = [_ЗаписьКаталога(группы)]

    def search(self, *args, **kwargs):
        return True

    def unbind(self):
        return None


def _с_каталогом(настройки, monkeypatch, связь_для):
    from asrhub import ldap_auth

    monkeypatch.setattr(ldap_auth, "соединение", связь_для)
    for ключ, значение in {
            "auth_ldap_enabled": True, "auth_ldap_url": "ldaps://dc01.corp.local:636",
            "auth_ldap_bind_template": "{username}@example.ru",
            "auth_ldap_base_dn": "dc=example,dc=ru",
            "auth_ldap_group_map": "asr-admins = admin\nasr-users = user"}.items():
        настройки.values[ключ] = значение
    return TestClient(create_app(настройки, start_queue=False))


def test_тёзка_ключа_из_каталога_не_входит(настройки, monkeypatch):
    """03-api-core №8: запись из каталога заводилась сама — с архивом ключа."""
    связь = _Связь(["CN=asr-users,OU=G,DC=example,DC=ru"])
    with _с_каталогом(настройки, monkeypatch, lambda *a: связь) as клиент:
        ответ = клиент.post("/api/auth/login", json={"username": "alice", "password": "секрет"})
        assert ответ.status_code in (400, 401, 403), ответ.text
        assert _hub(клиент).accounts.by_username("alice") is None


def test_сессия_из_каталога_живёт_не_дольше_срока(настройки, monkeypatch):
    """03-api-core №5: уволенного пускала старая кука — бессрочно."""
    связь = _Связь(["CN=asr-users,OU=G,DC=example,DC=ru"])
    настройки.values["auth_ldap_session_hours"] = 12.0
    with _с_каталогом(настройки, monkeypatch, lambda *a: связь) as клиент:
        assert клиент.post("/api/auth/login",
                           json={"username": "ivanov", "password": "секрет"}).status_code == 200
        assert клиент.get("/api/auth/me").status_code == 200
        db = _hub(клиент).db
        db.execute("UPDATE sessions SET created_at=?, last_seen=?",
                   [time.time() - 13 * 3600, time.time() - 600])
        assert клиент.get("/api/auth/me").status_code == 401


def test_снятие_с_групп_закрывает_открытые_сессии(настройки, monkeypatch):
    """03-api-core №5: роль и доступ брались из местной записи до нового входа."""
    группы = {"значение": ["CN=asr-admins,OU=G,DC=example,DC=ru"]}
    with _с_каталогом(настройки, monkeypatch,
                      lambda *a: _Связь(группы["значение"])) as клиент:
        клиент.post("/api/auth/login", json={"username": "petrov", "password": "секрет"})
        кука = клиент.cookies.get("asrhub_session")
        assert клиент.get("/api/auth/me").status_code == 200
        группы["значение"] = ["CN=бухгалтерия,OU=G,DC=example,DC=ru"]
        клиент.cookies.clear()
        assert клиент.post("/api/auth/login",
                           json={"username": "petrov", "password": "секрет"}).status_code == 401
        assert клиент.get("/api/auth/me",
                          cookies={"asrhub_session": кука}).status_code == 401


def test_ошибка_каталога_не_называет_адрес(настройки, monkeypatch):
    """03-api-core №15: аноним видел адрес контроллера и текст сетевой ошибки."""
    def падает(*args):
        raise OSError("socket connection error Connection refused (10.20.30.40:636)")

    with _с_каталогом(настройки, monkeypatch, падает) as клиент:
        ответ = клиент.post("/api/auth/login", json={"username": "ivanov", "password": "x"})
    assert ответ.status_code == 502, ответ.text
    assert "10.20.30.40" not in ответ.text and "dc01" not in ответ.text


def test_логин_со_скобкой_из_каталога_входит(настройки, monkeypatch):
    """03-api-core №15: «o(brien» проходил каталог и получал 400 от записи."""
    связь = _Связь(["CN=asr-users,OU=G,DC=example,DC=ru"])
    with _с_каталогом(настройки, monkeypatch, lambda *a: связь) as клиент:
        ответ = клиент.post("/api/auth/login", json={"username": "o(brien", "password": "x"})
    assert ответ.status_code == 200, ответ.text


def test_сертификат_каталога_проверяется(monkeypatch):
    """03-api-core №6: ldap3 без Tls принимал любой сертификат."""
    import ssl

    from asrhub import ldap_auth

    записано: dict[str, Any] = {}
    поддельный = types.ModuleType("ldap3")
    поддельный.NONE = "NONE"

    class Tls:
        def __init__(self, validate=None, ca_certs_file=None, **kwargs):
            записано["tls"] = (validate, ca_certs_file)

    class Связь:
        def __init__(self, server, **kwargs):
            pass

        def start_tls(self):
            записано["start_tls"] = True

        def bind(self):
            return False

    def server(url, **kwargs):
        записано["server_tls"] = kwargs.get("tls")
        return object()

    поддельный.Tls = Tls
    поддельный.Server = server
    поддельный.Connection = Связь
    monkeypatch.setitem(sys.modules, "ldap3", поддельный)
    каталог = ldap_auth.Настройки.из_настроек({
        "auth_ldap_enabled": True, "auth_ldap_url": "ldaps://dc01.corp.local:636",
        "auth_ldap_bind_template": "{username}@corp.local",
        "auth_ldap_ca_file": "/etc/asrhub/corp-ca.pem"})
    assert ldap_auth.войти(каталог, "ivanov", "пароль") is None
    assert записано.get("server_tls") is not None, "сервер каталога собран без Tls"
    assert записано["tls"] == (ssl.CERT_REQUIRED, "/etc/asrhub/corp-ca.pem")


# ---------------------------------------------------------------------------
# Веб-сокеты: пароль по умолчанию, отзыв, подразделение
# ---------------------------------------------------------------------------


def test_лента_не_пускает_с_паролем_по_умолчанию(сервер):
    """03-api-core №11а: /ws отдавал ленту всех заданий по admin/admin123."""
    вход = сервер.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert вход.status_code == 200 and вход.json()["must_change_password"]
    with pytest.raises(WebSocketDisconnect) as отказ, сервер.websocket_connect("/ws") as сокет:
        сокет.receive_text()
    assert отказ.value.code == 4403


def test_лента_закрывается_при_отзыве_ключа(сервер):
    """03-api-core №11б: отозванный ключ продолжал получать события."""
    ключ = сервер.post("/api/keys", headers=H(АДМИН),
                       json={"name": "временный", "role": "user"}).json()["key"]
    with сервер.websocket_connect(f"/ws?api_key={ключ}") as сокет:
        сокет.receive_text()                                # hello
        сокет.send_text("ping")
        assert сокет.receive_text() == '{"type":"pong"}'
        assert сервер.delete(f"/api/keys/{ключ[:16]}", headers=H(АДМИН)).status_code == 200
        with pytest.raises(WebSocketDisconnect) as закрыт:
            сокет.send_text("ping")
            сокет.receive_text()
    assert закрыт.value.code == 4401


def test_лента_закрывается_при_отключении_записи(сервер):
    accounts = _hub(сервер).accounts
    запись = accounts.create("оператор", "пароль-оператора", must_change_password=False)
    assert сервер.post("/api/auth/login", json={
        "username": "оператор", "password": "пароль-оператора"}).status_code == 200
    кука = сервер.cookies.get("asrhub_session")
    with сервер.websocket_connect("/ws", cookies={"asrhub_session": кука}) as сокет:
        сокет.receive_text()
        assert сервер.patch(f"/api/users/{запись.id}", headers=H(АДМИН),
                            json={"enabled": False}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as закрыт:
            сокет.send_text("ping")
            сокет.receive_text()
    assert закрыт.value.code == 4401


@pytest.mark.parametrize("как", ["выход", "смена пароля"])
def test_лента_закрывается_при_выходе_и_смене_пароля(сервер, как):
    """03-api-core №11б: закрытая выходом сессия продолжала получать ленту."""
    _hub(сервер).accounts.create("диспетчер", "пароль-диспетчера",
                                 must_change_password=False)
    assert сервер.post("/api/auth/login", json={
        "username": "диспетчер", "password": "пароль-диспетчера"}).status_code == 200
    кука = сервер.cookies.get("asrhub_session")
    with сервер.websocket_connect("/ws", cookies={"asrhub_session": кука}) as сокет:
        сокет.receive_text()
        if как == "выход":
            ответ = сервер.post("/api/auth/logout")
        else:
            # Пароль меняют из другой сессии (своя при смене остаётся): все
            # прочие сессии, а с ними и их сокеты, закрываются.
            сервер.cookies.clear()
            assert сервер.post("/api/auth/login", json={
                "username": "диспетчер", "password": "пароль-диспетчера"}).status_code == 200
            ответ = сервер.post("/api/auth/password", json={
                "current_password": "пароль-диспетчера",
                "new_password": "новый-пароль-диспетчера"})
        assert ответ.status_code == 200, ответ.text
        with pytest.raises(WebSocketDisconnect) as закрыт:
            сокет.send_text("ping")
            сокет.receive_text()
    assert закрыт.value.code == 4401


def test_события_коллег_по_группе_доходят(сервер):
    """03-api-core №14: на /ws сравнение шло по точному имени."""
    _hub(сервер).accounts.create("petrov", "пароль-петрова", group="sales",
                                 must_change_password=False)
    сервер.app.state.events.publish("job.queued", {"id": "j-коллеги",
                                                   "filename": "коллега.wav",
                                                   "_owner": "petrov"})
    with сервер.websocket_connect(f"/ws?api_key={ПРОДАЖИ}") as сокет:
        привет = json.loads(сокет.receive_text())
    assert "j-коллеги" in [с.get("id") for с in привет["history"]]
    # Чужому — по-прежнему нет.
    with сервер.websocket_connect(f"/ws?api_key={АЛИСА}") as сокет:
        assert "j-коллеги" not in сокет.receive_text()


def test_ключ_группы_видит_задания_записей_группы(сервер):
    """03-api-core №14: коллеги ключа собирались только из ключей."""
    _hub(сервер).accounts.create("dave", "пароль-дейва", group="sales",
                                 must_change_password=False)
    job_id = _hub(сервер).db.create_job({"filename": "от-дейва.wav", "status": "completed",
                                         "owner": "dave"})
    assert сервер.get(f"/api/jobs/{job_id}", headers=H(ПРОДАЖИ)).status_code == 200
    assert сервер.get(f"/api/jobs/{job_id}", headers=H(АЛИСА)).status_code == 403


# ---------------------------------------------------------------------------
# Квоты: удаление, повтор, одновременность, агент, поток
# ---------------------------------------------------------------------------


def test_удаление_не_возвращает_квоту(сервер_с_очередью):
    """03-api-core №10: «загрузил, забрал, удалил» — сколько угодно раз."""
    клиент = сервер_с_очередью
    первое = _поставить(клиент, КВОТА)
    assert первое.status_code == 200, первое.text
    _дождаться(клиент, КВОТА, первое.json()["id"])
    assert клиент.delete(f"/api/jobs/{первое.json()['id']}", headers=H(КВОТА)).status_code == 200
    расход = клиент.get("/api/usage", headers=H(КВОТА)).json()
    assert расход["used"]["jobs"] == 1, расход
    второе = _поставить(клиент, КВОТА)
    assert второе.status_code == 429, второе.text


def test_повтор_учитывается_в_часах_звука(сервер_с_очередью):
    """03-api-core №10: /retry, /rescan и пакетный повтор квоту не проверяли."""
    клиент = сервер_с_очередью
    первое = _поставить(клиент, ЧАСЫ, секунд=1.0)
    assert первое.status_code == 200, первое.text
    _дождаться(клиент, ЧАСЫ, первое.json()["id"])
    повтор = клиент.post(f"/api/jobs/{первое.json()['id']}/retry", headers=H(ЧАСЫ))
    assert повтор.status_code == 429, повтор.text
    пакетом = клиент.post("/api/jobs/bulk", headers=H(ЧАСЫ),
                          json={"action": "retry", "ids": [первое.json()["id"]]})
    assert пакетом.json()["failed"], пакетом.text


def test_одновременные_постановки_не_обходят_квоту(сервер):
    """02-concurrency №10: десять загрузок разом проходили при квоте в три."""
    import threading

    from asrhub.api.deps import участник_ключа
    from asrhub.api.routes_jobs import под_квотой
    from asrhub.errors import QuotaExceeded

    hub = _hub(сервер)
    участник = участник_ключа(hub, КВОТА, hub.settings.api_keys[КВОТА])
    итоги: list[str] = []
    старт = threading.Barrier(6)

    def поставить() -> None:
        старт.wait()

        def действие() -> None:
            time.sleep(0.2)          # постановка небыстрая: хеш, ffprobe, запись
            hub.db.create_job({"filename": "разом.wav", "status": "queued",
                               "owner": "квота"})
        try:
            под_квотой(hub, участник, действие)
            итоги.append("принято")
        except QuotaExceeded:
            итоги.append("отказ")

    потоки = [threading.Thread(target=поставить) for _ in range(6)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=60)
    assert итоги.count("принято") == 1, итоги


def test_агент_подчиняется_квоте(сервер):
    """14-agent №8: через /agent/call ключ с квотой ставил сколько угодно."""
    ответ = сервер.post("/api/telephony/agent/hello", headers=H(КВОТА),
                        json={"host": "pbx54.local", "name": "АТС"})
    assert ответ.status_code == 200, ответ.text
    agent_id = ответ.json()["agent_id"]

    def звонок(uid: str):
        return сервер.post("/api/telephony/agent/call", headers=H(КВОТА), data={
            "meta": json.dumps({"agent_id": agent_id, "uniqueid": uid, "src": "79161234567",
                                "dst": "101", "duration": 3, "billsec": 2,
                                "answered": True, "started_at": time.time() - 60})},
            files={"file": ("запись.wav", _wav(1.0), "audio/wav")})

    assert звонок("u-1").status_code == 200
    второй = звонок("u-2")
    assert второй.status_code == 429, второй.text
    assert второй.headers.get("retry-after")
    принятые = {п.name for п in _hub(сервер).settings.paths.uploads.iterdir()}
    assert len([и for и in принятые if и.startswith("pbx")]) == 1, принятые


def test_поток_подчиняется_квоте(сервер):
    """05-queue №16: поток шёл мимо квот и лимита частоты."""
    from asrhub.api.routes_jobs import учесть_расход

    учесть_расход(_hub(сервер), "часы", audio_s=3600.0)
    with сервер.websocket_connect(f"/api/stream?api_key={ЧАСЫ}") as сокет:
        сокет.send_text(json.dumps({"format": "pcm_s16le"}))
        первое = json.loads(сокет.receive_text())
    assert первое["type"] == "error" and первое.get("code") == "quota_exceeded", первое


def test_поток_ограничен_числом_сессий(сервер):
    """03-api-core №13: одновременных сессий одного ключа — сколько угодно."""
    _hub(сервер).settings.values["stream_max_sessions_per_owner"] = 1
    with сервер.websocket_connect(f"/api/stream?api_key={АЛИСА}") as первый:
        первый.send_text(json.dumps({"format": "pcm_s16le"}))
        assert json.loads(первый.receive_text())["type"] == "ready"
        with сервер.websocket_connect(f"/api/stream?api_key={АЛИСА}") as второй:
            второй.send_text(json.dumps({"format": "pcm_s16le"}))
            ответ = json.loads(второй.receive_text())
        assert ответ["type"] == "error" and ответ.get("code") == "stream_limit", ответ
    # Закрыли первую — следующая открывается.
    with сервер.websocket_connect(f"/api/stream?api_key={АЛИСА}") as снова:
        снова.send_text(json.dumps({"format": "pcm_s16le"}))
        assert json.loads(снова.receive_text())["type"] == "ready"


def test_поток_без_первого_сообщения_не_грузит_модель(сервер, monkeypatch):
    """03-api-core №13: закрытое сразу соединение всё равно грузило модель."""
    реестр = _hub(сервер).registry
    вызовы: list[Any] = []
    прежний = реестр.get
    monkeypatch.setattr(реестр, "get", lambda *a, **k: (вызовы.append(1), прежний(*a, **k))[1])
    with сервер.websocket_connect(f"/api/stream?api_key={АЛИСА}"):
        pass
    time.sleep(0.5)
    assert not вызовы, "модель загружалась для ушедшего клиента"


def test_расход_потока_записывается_владельцу(сервер):
    """05-queue №16: GET /api/usage поток не видел."""
    import array

    with сервер.websocket_connect(f"/api/stream?api_key={ЧАСЫ}") as сокет:
        сокет.send_text(json.dumps({"format": "pcm_s16le"}))
        assert json.loads(сокет.receive_text())["type"] == "ready"
        звук = array.array("h", [int(3000 * math.sin(и / 8)) for и in range(16000)])
        сокет.send_bytes(звук.tobytes())
        сокет.send_text(json.dumps({"type": "finish"}))
        while json.loads(сокет.receive_text())["type"] != "done":
            pass
    # Расход записывается при закрытии сессии — уже после ответа «done».
    конец = time.monotonic() + 20
    while time.monotonic() < конец:
        расход = сервер.get("/api/usage", headers=H(ЧАСЫ)).json()
        if расход["used"]["audio_hours"] > 0:
            break
        time.sleep(0.2)
    assert расход["used"]["audio_hours"] > 0, расход


# ---------------------------------------------------------------------------
# Лишнее неадминистратору
# ---------------------------------------------------------------------------


def test_настройки_неадминистратору_без_инфраструктуры(сервер):
    """03-api-core №12, 08-params №11, 12-monitoring №6."""
    значения = _hub(сервер).settings.values
    значения.update({
        "models_dir": "/srv/models-секрет", "backup_dir": "/mnt/backup/секрет",
        "auth_ldap_url": "ldaps://dc01.corp.local:636",
        "llm_url": "http://user:PASSWORD@10.1.2.3:11434/v1",
        "telephony_cdr_file": "/var/log/asterisk/cdr-секрет.csv",
        "monitoring_targets": [{"kind": "webhook", "name": "чат",
                                "url": "https://hooks.slack.test/services/T0/B0/XXXXSECRETPATH"}],
        "webhook_secret": "подпись-секрет", "llm_api_key": "sk-секрет",
        "telephony_secret": "пароль-ами"})
    чтение = сервер.get("/api/settings", headers=H(ЧИТАТЕЛЬ))
    assert чтение.status_code == 200
    for след in ("секрет", "dc01", "PASSWORD", "10.1.2.3", "XXXXSECRETPATH", "пароль-ами"):
        assert след not in чтение.text, след
    assert чтение.json()["values"]["webhook_secret"] == "***"
    assert "beam_size" in чтение.json()["values"], "параметры задания пропали"
    админ = сервер.get("/api/settings", headers=H(АДМИН)).json()["values"]
    assert админ["models_dir"] == "/srv/models-секрет"
    assert админ["llm_api_key"] == "***" and админ["telephony_secret"] == "***"
    assert админ["webhook_secret"] == "***"


def test_состояние_модели_без_адреса_и_чужой_записи(сервер):
    """11-llm №17: адрес модели с паролем и чужое текущее задание."""
    значения = _hub(сервер).settings.values
    значения.update({"llm_backend": "openai", "llm_model": "m",
                     "llm_url": "http://user:PASSWORD@127.0.0.1:9/v1"})
    поток = _hub(сервер).llm_worker
    чужое = _hub(сервер).db.create_job({"filename": "чужое.wav", "owner": "кто-то"})
    поток.current = чужое
    try:
        чтение = сервер.get("/api/llm/status", headers=H(ЧИТАТЕЛЬ))
        assert чтение.status_code == 200, чтение.text
        assert "PASSWORD" not in чтение.text and "127.0.0.1:9" not in чтение.text
        assert чужое not in чтение.text
        админ = сервер.get("/api/llm/status", headers=H(АДМИН)).json()
        assert "PASSWORD" not in str(админ.get("url"))
        assert админ["worker"]["current"] == чужое
    finally:
        поток.current = None


def test_аналитика_неадминистратору_без_замеров_сервера(сервер):
    """10-analytics №14: загрузка сервера и чужие модели потока."""
    db = _hub(сервер).db
    db.add_system_sample({"ts": time.time() - 60, "cpu_percent": 77.0, "queue_depth": 42})
    db.add_metric("stream_first_text_s", 1.5, model="чужая-модель-бориса")
    db.add_metric("stream_session_s", 30.0, model="чужая-модель-бориса")
    ряды = сервер.get("/api/analytics/timeseries?period=day", headers=H(АЛИСА)).json()
    assert not ряды["system"]["cpu"], ряды["system"]
    задержка = сервер.get("/api/analytics/latency?period=day", headers=H(АЛИСА)).json()
    assert "чужая-модель-бориса" not in json.dumps(задержка, ensure_ascii=False)
    админ = сервер.get("/api/analytics/timeseries?period=day", headers=H(АДМИН)).json()
    assert 77.0 in админ["system"]["cpu"]


def test_глубокая_самопроверка_не_по_каждому_запросу(сервер, monkeypatch):
    """03-api-core №20: deep=1 по запросу любого ключа, без паузы."""
    from asrhub import selfcheck
    from asrhub.api import routes_system

    глубоких: list[bool] = []
    прежняя = selfcheck.состояние

    def считать(state, глубоко=False):
        if глубоко:
            глубоких.append(True)
        return прежняя(state, глубоко=False)

    monkeypatch.setattr(selfcheck, "состояние", считать)
    monkeypatch.setitem(routes_system._ГЛУБОКИЙ_СВОД, "свод", None)
    for _ in range(3):
        assert сервер.get("/api/system/selfcheck?deep=1",
                          headers=H(ЧИТАТЕЛЬ)).status_code == 200
    assert len(глубоких) == 1, глубоких
    сервер.get("/api/system/selfcheck?deep=1", headers=H(АДМИН))
    assert len(глубоких) == 2, "администратору глубокая — всегда свежая"
