"""Регрессии пятого захода ревизии.

Каждая проверка соответствует дефекту, который был воспроизведён на живом
коде: утечка чужих данных, порча результата или зависание. Названия
описывают исходный дефект, а не механику проверки, — чтобы при падении
сразу было понятно, что именно вернулось.
"""
from __future__ import annotations

import math
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Any

import pytest
from asrhub.pipeline import audio as audio_mod
from asrhub.pipeline import export as export_mod
from asrhub.pipeline import metrics as metrics_mod
from asrhub.pipeline import postprocess as pp

# ---------------------------------------------------------------------------
# Разграничение доступа
# ---------------------------------------------------------------------------

@pytest.fixture()
def auth_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с ВКЛЮЧЁННОЙ проверкой ключей.

    Общая фикстура `client` работает без неё — так проще проверять
    остальное, — но разграничение доступа без ключей не проверить: без
    проверки любой запрос считается administratorским.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as test_client:
        admin = (tmp_path / "data" / "api-key.txt").read_text(encoding="utf-8").strip()
        test_client.headers.update({"X-API-Key": admin})
        yield test_client


@pytest.fixture()
def two_users(auth_client):
    """Администратор и два обычных ключа плюс задание первого из них."""
    client = auth_client
    admin = client.headers.get("X-API-Key")
    state = client.app.state.hub

    def make_key(name: str, role: str) -> str:
        response = client.post("/api/keys", json={"name": name, "role": role})
        assert response.status_code == 200, response.text
        return response.json()["key"]

    alice = make_key("alice", "user")
    bob = make_key("bob", "user")
    readonly = make_key("readonly", "readonly")
    job_id = state.db.create_job({
        "filename": "секрет-алисы.wav", "owner": "alice", "status": "running",
        "model": "demo-simulator", "file_path": "/данные/uploads/секрет.wav",
        "text": "ОЧЕНЬ СЕКРЕТНАЯ РАСШИФРОВКА",
        "webhook_url": "https://внутренний.host/hook?token=ТОКЕН",
    })
    return {"admin": admin, "alice": alice, "bob": bob,
            "readonly": readonly, "job_id": job_id}


def test_queue_does_not_leak_other_owners(auth_client, two_users):
    """`GET /api/queue` отдавал чужие задания целиком.

    Прошлый заход закрыл список заданий, а соседний маршрут остался
    открытым: любой ключ видел имена чужих файлов, пути на диске, готовые
    расшифровки и адреса уведомлений вместе с токенами внутри.
    """
    for role in ("bob", "readonly"):
        body = auth_client.get("/api/queue", headers={"X-API-Key": two_users[role]}).text
        assert "секрет-алисы.wav" not in body, f"{role} видит чужой файл"
        assert "/данные/uploads" not in body, f"{role} видит путь на диске"
        assert "ОЧЕНЬ СЕКРЕТНАЯ" not in body, f"{role} видит чужую расшифровку"
        assert "ТОКЕН" not in body, f"{role} видит чужой токен уведомления"

    own = auth_client.get("/api/queue", headers={"X-API-Key": two_users["alice"]}).text
    assert "секрет-алисы.wav" in own, "владелец перестал видеть своё задание"
    admin = auth_client.get("/api/queue", headers={"X-API-Key": two_users["admin"]}).text
    assert "секрет-алисы.wav" in admin, "администратор перестал видеть всё"


def test_job_without_owner_is_not_public(auth_client, two_users):
    """Пустой владелец означал «проверять нечего».

    Задание, созданное клиентом командной строки или ключом без имени,
    читал и удалял кто угодно.
    """
    state = auth_client.app.state.hub
    job_id = state.db.create_job({"filename": "ничей.wav", "owner": "",
                                  "status": "completed", "text": "содержимое"})
    headers = {"X-API-Key": two_users["bob"]}
    assert auth_client.get(f"/api/jobs/{job_id}", headers=headers).status_code == 403
    assert auth_client.delete(f"/api/jobs/{job_id}", headers=headers).status_code == 403
    # Администратору по-прежнему доступно всё.
    assert auth_client.get(f"/api/jobs/{job_id}",
                      headers={"X-API-Key": two_users["admin"]}).status_code == 200


def test_events_hide_administrative_records(auth_client, two_users):
    """`not job_id` пропускало неадминам ровно административные события.

    Создание и отзыв ключей, изменение настроек, загрузка моделей — всё это
    события без job_id, и readonly читал их вместе с именами ключей и ролями.
    """
    state = auth_client.app.state.hub
    state.db.add_event(None, "key_created", "Создан ключ «бухгалтерия» с ролью admin")
    state.db.add_event(None, "queue_paused", "Очередь приостановлена")

    body = auth_client.get("/api/events?limit=50",
                      headers={"X-API-Key": two_users["readonly"]}).text
    assert "бухгалтерия" not in body, "утечка сведений о ключах"
    assert "Очередь приостановлена" in body, "общие события пропали вместе с закрытыми"
    admin_body = auth_client.get("/api/events?limit=50",
                            headers={"X-API-Key": two_users["admin"]}).text
    assert "бухгалтерия" in admin_body


def test_system_paths_are_admin_only(auth_client, two_users):
    """Раскладка файловой системы уходила любому ключу.

    Сама по себе разведка, но именно она превращает прочие находки из
    теоретических в применимые.
    """
    lean = auth_client.get("/api/system", headers={"X-API-Key": two_users["readonly"]}).json()
    assert "paths" not in lean and "database" not in lean
    assert "hardware" in lean, "полезные сведения пропали вместе с закрытыми"
    full = auth_client.get("/api/system", headers={"X-API-Key": two_users["admin"]}).json()
    assert "paths" in full and "database" in full


def test_download_does_not_walk_working_directory(client, tmp_path, monkeypatch):
    """Пустой result_path превращал поиск файла в обход рабочего каталога.

    `Path("")` — это `Path(".")`, поэтому клиенту уходил первый попавшийся
    файл с нужным расширением из каталога, откуда запущен сервер.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "секреты.json").write_text('{"пароль": "СЕКРЕТ"}', encoding="utf-8")
    state = client.app.state.hub
    job_id = state.db.create_job({"filename": "з.wav", "owner": "anonymous",
                                  "status": "completed", "text": "текст",
                                  "result_path": None})
    response = client.get(f"/api/jobs/{job_id}/download?fmt=json")
    assert "СЕКРЕТ" not in response.text
    assert response.status_code != 200 or "секреты.json" not in str(
        response.headers.get("content-disposition", ""))


def test_key_can_be_revoked_with_what_interface_sends(client):
    """Отзыв ключа из интерфейса не работал никогда.

    Интерфейс слал первые шесть символов превью, сервер требовал двенадцать
    и отвечал 400 на любой ключ.
    """
    client.post("/api/keys", json={"name": "лишний", "role": "user"})
    keys = client.get("/api/keys").json()["items"]
    target = next(k for k in keys if k["name"] == "лишний")
    assert len(target["key_id"]) >= 12, "интерфейсу нечего послать"
    assert client.delete(f"/api/keys/{target['key_id']}").status_code == 200
    remaining = [k["name"] for k in client.get("/api/keys").json()["items"]]
    assert "лишний" not in remaining


def test_oversized_upload_is_refused_before_body_is_read(client, monkeypatch):
    """Предел размера проверялся после того, как тело осело на диске.

    FastAPI разбирал multipart целиком, и запрос на десятки гигабайт
    успевал забить временный каталог, прежде чем получить 413.
    """
    import starlette.formparsers as formparsers

    client.app.state.hub.settings.values["max_upload_mb"] = 1
    seen: dict[str, int] = {"bytes": 0}
    original = formparsers.MultiPartParser.parse

    async def spy(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        for _, value in result.multi_items():
            seen["bytes"] = max(seen["bytes"], getattr(value, "size", 0) or 0)
        return result

    monkeypatch.setattr(formparsers.MultiPartParser, "parse", spy)
    payload = b"\0" * (8 * 1024 * 1024)
    response = client.post("/api/jobs",
                           files={"file": ("большой.wav", payload, "audio/wav")})
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert seen["bytes"] == 0, "тело всё-таки было принято"


def test_events_are_addressed_to_owner():
    """Рассылка по WebSocket шла всем подряд.

    Владелец подписчика нигде не запоминался, и readonly читал в ленте
    имена чужих файлов и тексты чужих ошибок, а при подключении получал
    ещё и двадцать последних событий.
    """
    from asrhub.api.app import EventHub

    hub = EventHub()
    about_job = {"type": "job.started", "id": "job_1",
                 "filename": "секрет-алисы.wav", "_owner": "alice"}
    common = {"type": "queue_paused"}

    assert hub._visible(about_job, "alice", False) is True
    assert hub._visible(about_job, "bob", False) is False
    assert hub._visible(about_job, "readonly", False) is False
    assert hub._visible(about_job, "любой", True) is True     # администратор
    assert hub._visible(common, "bob", False) is True

    hub._history = [about_job, common]
    assert [m["type"] for m in hub.history_for("bob", False)] == ["queue_paused"]
    assert all("_owner" not in m for m in hub.history_for("alice", False)), \
        "служебное поле утекло клиенту"


# ---------------------------------------------------------------------------
# Корректность результата
# ---------------------------------------------------------------------------

def _wav_with_lead_silence(path: Path, silence_s: float = 3.0,
                           total_s: float = 5.0, rate: int = 16000) -> Path:
    frames = bytearray()
    for index in range(int(rate * total_s)):
        second = index / rate
        value = 0.0 if second < silence_s else 0.4 * math.sin(2 * math.pi * 320 * second)
        frames += struct.pack("<h", int(value * 32767))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


@pytest.mark.skipif(not audio_mod.has_ffmpeg(), reason="нужен ffmpeg")
def test_trimmed_silence_does_not_move_timestamps(tmp_path: Path):
    """Обрезка начальной тишины сдвигала все таймкоды, и это никто не учитывал.

    Настройка включена по умолчанию, поэтому субтитры любой записи с
    паузой в начале уезжали ровно на длину этой паузы.
    """
    source = _wav_with_lead_silence(tmp_path / "тишина.wav", silence_s=3.0)
    settings = {"audio_trim_silence": True, "audio_sample_rate": 16000,
                "audio_channels": "mono"}
    prepared = audio_mod.prepare(source, tmp_path / "work", settings)

    assert prepared.shifted, "сдвиг не замечен"
    assert 2.5 <= prepared.offset_s <= 3.5, f"сдвиг измерен неверно: {prepared.offset_s}"
    # Речь начинается в нуле подготовленного файла и в трёх секундах исходного.
    assert 2.5 <= prepared.to_source_time(0.0) <= 3.5


@pytest.mark.skipif(not audio_mod.has_ffmpeg(), reason="нужен ffmpeg")
def test_no_shift_without_trimming(tmp_path: Path):
    source = _wav_with_lead_silence(tmp_path / "тишина.wav")
    prepared = audio_mod.prepare(source, tmp_path / "work", {
        "audio_trim_silence": False, "audio_sample_rate": 16000,
        "audio_channels": "mono"})
    assert prepared.offset_s == 0.0 and not prepared.shifted


def test_speed_change_is_accounted_for():
    """Изменение темпа сжимало таймкоды, и коэффициент нигде не применялся."""
    prepared = audio_mod.Prepared(channels=[], offset_s=2.0, speed=1.25)
    assert prepared.to_source_time(0.0) == 2.0
    assert prepared.to_source_time(4.0) == 7.0


@pytest.mark.parametrize("source,expected", [
    ("Осталось три, четыре дня.", "Осталось 3, 4 дня."),
    ("Пять, шесть человек ждут.", "5, 6 человек ждут."),
    ("Один, два, три, поехали!", "1, 2, 3, поехали!"),
    ("Ему двадцать.", "Ему 20."),
    ("Код три три три.", "Код три три три."),
    # То, что должно нормализоваться, — по-прежнему нормализуется.
    ("Двадцать пять рублей.", "25 ₽."),
    ("Сто двадцать три.", "123."),
    ("Две тысячи двадцать четыре год", "2024 год"),
    ("Скидка тридцать процентов", "Скидка 30 %"),
])
def test_numbers_do_not_merge_across_punctuation(source, expected):
    """Числительные складывались через запятую, а знак препинания терялся.

    «три, четыре дня» превращалось в «7 дня», «один, два, три, поехали!» —
    в «6 поехали!». Нормализация включена по умолчанию и работает после
    расстановки пунктуации, то есть портила именно то, что модель только
    что расставила.
    """
    assert pp._builtin_itn_ru(source) == expected


@pytest.mark.parametrize("text", [
    "Он любит хлебать горячий суп",
    "Нахлебался чаю",
    "Расхлебали кашу",
    "Сукно на столе",
])
def test_profanity_filter_leaves_ordinary_words(text):
    """Корень искался в любом месте слова, и «хлебать» становилось «х******»."""
    assert pp.filter_profanity(text, "mask") == (text, 0)


@pytest.mark.parametrize("text", ["Иди на хуй", "Полная хуйня", "Заебал уже",
                                  "Это блядство"])
def test_profanity_filter_still_works(text):
    result, hits = pp.filter_profanity(text, "mask")
    assert hits >= 1 and result != text


@pytest.mark.parametrize("seconds,expected", [
    (59.9996, "00:01:00,000"),
    (3599.9996, "01:00:00,000"),
    (59.9994, "00:00:59,999"),
    (0.0, "00:00:00,000"),
])
def test_timestamps_carry_over_correctly(seconds, expected):
    """Округление добавляло секунду, но перенос дальше не шёл.

    Получались «00:00:60,000» и «00:59:60,000» — такой блок строгие плееры
    просто отбрасывают.
    """
    assert export_mod.format_timestamp(seconds, "srt") == expected


def test_subtitle_wrapping_keeps_every_word():
    """Лишние строки отрезались вместе со словами."""
    phrase = ("Мы обсудили условия поставки и договорились перенести отгрузку "
              "на следующий понедельник, потому что склад закрыт")
    wrapped = export_mod.wrap_subtitle(phrase, 42, 2)
    assert wrapped.replace("\n", " ").split() == phrase.split()


def test_ass_escapes_braces():
    """В ASS фигурные скобки — команды оформления.

    «Скидка {30} процентов» отрисовывалась без числа: всё в скобках
    считалось командой и не показывалось.
    """
    result = export_mod.to_ass(
        {"segments": [{"start": 0.0, "end": 2.0, "text": "Скидка {30} процентов"}]}, {})
    assert r"\{30\}" in result
    assert "{30}" not in result.replace(r"\{30\}", "")


def test_speaker_labels_setting_applies_to_text():
    """`include_speaker_labels` не действовала на txt.

    Хуже того, тот же текст шёл в расчёт точности, и каждая реплика
    добавляла к эталону две лишние вставки, завышая WER.
    """
    segments = [{"start": 0.0, "end": 2.0, "text": "Первая", "speaker": "Оператор"},
                {"start": 2.0, "end": 4.0, "text": "Вторая", "speaker": "Клиент"}]
    result = {"segments": segments, "text": "", "speakers": ["Оператор", "Клиент"]}
    without = export_mod.to_txt(result, {"include_speaker_labels": False})
    assert "Оператор:" not in without and "Первая" in without
    with_labels = export_mod.to_txt(result, {"include_speaker_labels": True})
    assert "Оператор:" in with_labels


def test_accuracy_does_not_hang_on_long_texts():
    """Посимвольный расчёт был квадратичным без ограничителя.

    Часовая расшифровка считалась больше получаса, и всё это время задание
    висело на 96 % и не отменялось.
    """
    vocabulary = ["сегодня", "мы", "обсуждали", "условия", "поставки", "сроки", "отгрузки", "договор", "оплата", "склад", "менеджер", "клиент", "заявка", "счёт", "документы"]
    import random
    random.seed(7)
    reference = [random.choice(vocabulary) for _ in range(8000)]   # примерно час
    hypothesis = list(reference)
    for _ in range(800):
        hypothesis[random.randrange(len(hypothesis))] = random.choice(vocabulary)

    started = time.time()
    result = metrics_mod.detailed(" ".join(reference), " ".join(hypothesis))
    elapsed = time.time() - started
    assert elapsed < 30, f"разбор занял {elapsed:.1f} с"
    assert 0.05 < result["wer"] < 0.2, result["wer"]
    assert 0.0 < result["cer"] < 0.2, result["cer"]


def test_chunked_distance_matches_direct_computation():
    """Ускорение не должно менять числа."""
    import random
    random.seed(11)
    vocabulary = ["раз", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять"]
    reference = [random.choice(vocabulary) + str(i) for i in range(600)]
    hypothesis = list(reference)
    for _ in range(60):
        hypothesis[random.randrange(len(hypothesis))] = "ошибка"
    direct = metrics_mod._levenshtein(reference, hypothesis)
    chunked = metrics_mod._levenshtein_chunked(reference, hypothesis)
    assert direct.error_rate == chunked.error_rate


def test_glossary_error_does_not_lose_transcript():
    """Ошибка в строке замены летела мимо перехвата.

    `subn` разбирает замену как шаблон, и ссылка на несуществующую группу
    обрушивала весь шаг постобработки вместе с расшифровкой.
    """
    text, count = pp.apply_glossary("версия три", {"re:версия": "\\1 версия"})
    assert text == "версия три" and count == 0
    # Исправные правила продолжают работать.
    assert pp.apply_glossary("версия три", {"re:версия": "редакция"}) == ("редакция три", 1)


def test_waveform_reads_both_segment_shapes():
    """Полоса строится после постобработки, где сегменты — словари.

    Обращение через getattr к словарю молча давало None, и кривые по
    говорящим переставали строиться вовсе.
    """
    from asrhub.engines.base import Segment
    from asrhub.pipeline import waveform as waveform_mod

    objects = [Segment(0.0, 2.0, "раз", speaker="Оператор"),
               Segment(2.0, 4.0, "два", speaker="Клиент")]
    assert waveform_mod._field(objects[0], "speaker") == "Оператор"
    assert waveform_mod._field(objects[0].to_dict(), "speaker") == "Оператор"


# ---------------------------------------------------------------------------
# Аналитика, очередь, хранение
# ---------------------------------------------------------------------------

@pytest.fixture()
def analytics_db(tmp_path: Path):
    from asrhub.analytics import Analytics
    from asrhub.db import Database

    database = Database(tmp_path / "a.sqlite3")
    moment = time.time()
    for owner, language, engine, status, count in (
            ("alice", "ru", "whisper", "completed", 2),
            ("bob", "en", "vosk", "completed", 4),
            ("alice", "ru", "whisper", "failed", 1)):
        for _ in range(count):
            job_id = database.create_job({
                "owner": owner, "language": language, "engine": engine, "model": "m",
                "media_duration_s": 60.0, "created_at": moment - 60})
            database.update_job(job_id, status=status, finished_at=moment - 30,
                                processing_time_s=10.0, rtf=0.16)
    return Analytics(database)


def test_analytics_sections_respect_owner(analytics_db):
    """`by_language` и `by_engine` не передавали владельца в `_group`.

    Соседние методы передавали — то есть это была опечатка, из-за которой
    обычный ключ видел сводку по всему серверу.
    """
    languages = analytics_db.by_language("day", owner="alice")
    engines = analytics_db.by_engine("day", owner="alice")
    assert {row["key"] for row in languages} == {"ru"}
    assert {row["key"] for row in engines} == {"whisper"}
    assert sum(row["jobs"] for row in languages) == 3


def test_failure_rate_uses_one_population(analytics_db):
    """Числитель считался по владельцу, знаменатель — по всему серверу."""
    report = analytics_db.errors("day", owner="alice")
    assert report["total_failed"] == 1
    assert report["total_jobs"] == 3
    assert abs(report["failure_rate"] - 1 / 3) < 0.001


def test_queue_is_not_starved_by_pending_retries(tmp_path: Path):
    """Отбор «время повтора наступило» шёл после LIMIT.

    Больше пятисот заданий в статусе retry забивали окно предвыборки
    целиком, и воркеры простаивали, хотя готовые задания были.
    """
    from asrhub.db import Database

    database = Database(tmp_path / "q.sqlite3")
    moment = time.time()
    for index in range(600):
        job_id = database.create_job({"status": "retry", "media_duration_s": 60.0,
                                      "priority": 90})
        database.update_job(job_id, status="retry", queued_at=moment + 300,
                            created_at=moment - 1000 + index)
    for _ in range(5):
        job_id = database.create_job({"status": "queued", "media_duration_s": 60.0,
                                      "priority": 50})
        database.update_job(job_id, status="queued", queued_at=moment - 10)

    for order in ("created_at ASC", "media_duration_s ASC", "priority DESC"):
        ready = database.list_jobs(status=["queued", "retry"], limit=500, order=order,
                                   light=True, ready_before=time.time())
        assert len(ready) == 5, f"политика {order}: очередь встала"


def test_cleanup_is_bounded_and_covers_unfinished(tmp_path: Path):
    """Уборка выбирала всё разом и не трогала незавершённые задания.

    Понижение срока хранения вытаскивало в память сотни тысяч строк и
    занимало блокировку записи на десятки минут, а файлы заданий, застрявших
    в очереди, не удалялись никогда.
    """
    from asrhub.db import CLEANUP_BATCH, Database

    database = Database(tmp_path / "c.sqlite3")
    moment = time.time()
    ancient = moment - 400 * 86400
    for _ in range(10):
        job_id = database.create_job({"status": "completed"})
        database.update_job(job_id, status="completed", finished_at=ancient)
    for _ in range(5):
        job_id = database.create_job({"status": "queued"})
        database.update_job(job_id, created_at=ancient)
    for _ in range(3):
        database.create_job({"status": "queued"})       # свежие — трогать нельзя

    removed = database.cleanup(results_days=30)
    assert removed["jobs"] == 15
    assert database.count_jobs() == 3
    assert CLEANUP_BATCH > 0
    assert "LIMIT" in _cleanup_sql(), "предел на заход исчез из запроса"


def _cleanup_sql() -> str:
    source = Path(__file__).resolve().parent.parent / "server" / "asrhub" / "db.py"
    text = source.read_text(encoding="utf-8")
    start = text.index("def cleanup(")
    return text[start:start + 2000]


# ---------------------------------------------------------------------------
# Сценарии установки
# ---------------------------------------------------------------------------

def test_gpu_driver_state_reads_installed_driver(repo_root: Path):
    """«A || B && C» разбиралось как «(A || B) && C».

    При наличии nvidia-smi без модуля в updates/dkms состояние «драйвер
    стоит, нужна перезагрузка» читалось как «драйвера нет», и скрипт шёл
    ставить драйвер заново.
    """
    script = f"""
        set -o errexit -o nounset -o pipefail
        source "{repo_root}/scripts/lib/common.sh"
        source "{repo_root}/scripts/lib/detect.sh"
        source "{repo_root}/scripts/lib/gpu.sh"
        have() {{ [[ "$1" == nvidia-smi ]]; }}
        nvidia-smi() {{ return 1; }}
        gpu_driver_state 0x10de
    """
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                            timeout=60, env={"ASRHUB_QUIET": "1", "PATH": "/usr/bin:/bin"})
    assert result.stdout.strip() == "installed-noload", result.stdout


def test_installer_does_not_copy_over_itself(repo_root: Path, tmp_path: Path):
    """Запуск из установленной копии сносил установку.

    Цикл удалял каталог и тут же копировал его сам в себя, оставляя
    установку без server — при том что шапка скрипта обещает обратное.
    """
    import shutil

    copy = tmp_path / "установка"
    shutil.copytree(repo_root, copy, ignore=shutil.ignore_patterns(
        ".git", "build", "__pycache__", ".pytest_cache", ".ruff_cache", "*.whl"))
    result = subprocess.run(
        ["bash", "scripts/install.sh", "--dry-run", "--no-interactive", "--yes",
         "--profile", "light", "--skip-models", "--no-service",
         "--prefix", str(copy), "--data", str(tmp_path / "data")],
        cwd=copy, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "копирование пропущено" in result.stdout
    assert (copy / "server").is_dir() and (copy / "scripts").is_dir()


def test_doctor_works_from_any_directory(repo_root: Path, tmp_path: Path):
    """Путь к пакету был относительным, а doctor.sh запускают откуда угодно.

    Установщик прямо предлагает «bash /opt/asrhub/scripts/doctor.sh», и на
    исправной установке весь раздел движков объявлялся сломанным.
    """
    import shutil

    prefix = tmp_path / "установка"
    (prefix / "venv" / "bin").mkdir(parents=True)
    (prefix / "venv" / "bin" / "python").symlink_to(sys.executable)
    shutil.copytree(repo_root / "server", prefix / "server")
    elsewhere = tmp_path / "другой-каталог"
    elsewhere.mkdir()

    result = subprocess.run(
        ["bash", str(repo_root / "scripts" / "doctor.sh"), "--prefix", str(prefix)],
        cwd=elsewhere, capture_output=True, text=True, timeout=180)
    assert "не удалось выполнить" not in result.stdout, result.stdout[-1500:]
    assert "demo" in result.stdout


def test_env_file_format_suits_systemd(repo_root: Path):
    """`export` в env.sh ломал EnvironmentFile.

    systemd читает строго ИМЯ=ЗНАЧЕНИЕ и строку с «export » отбраковывает —
    собранный whisper.cpp сервер попросту не находил.
    """
    text = (repo_root / "scripts" / "lib" / "whispercpp.sh").read_text(encoding="utf-8")
    assert "printf 'export ASRHUB_WHISPER_CPP" not in text
    assert "printf 'ASRHUB_WHISPER_CPP=%s" in text


def test_powershell_confirm_respects_default(repo_root: Path):
    """`Confirm-Action` без консоли отвечала «да» на всё.

    `uninstall.ps1 -Purge` из задачи планировщика удалял каталог данных с
    базой и моделями, ни о чём не спросив, — при умолчании «нет».
    """
    text = (repo_root / "scripts" / "lib" / "Common.psm1").read_text(encoding="utf-8")
    body = text.split("function Confirm-Action")[1].split("\n}")[0]
    assert "return $true" not in body.split("UserInteractive")[1].split("\n")[0], \
        "безусловное согласие вернулось"
    assert "$Default -eq 'y'" in body


def test_powershell_rollback_captures_each_directory(repo_root: Path):
    """Блок отката связывался с переменной поздно.

    К моменту вызова $dir равнялся последнему значению цикла, и откат пять
    раз удалял data\\tmp, оставляя каталог программы с venv на диске.
    """
    text = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "GetNewClosure()" in text
    assert "$captured = $dir" in text


def test_docker_uid_is_not_root_under_sudo(repo_root: Path):
    """Под sudo id -u давал ноль, и контейнер работал от root, минуя gosu."""
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "ASRHUB_UID=${SUDO_UID:-$(id -u)}" in text
    assert "ASRHUB_GID=${SUDO_GID:-$(id -g)}" in text


def test_service_user_is_created(repo_root: Path):
    """Служба systemd по умолчанию работала от root."""
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "useradd --system --no-create-home" in text
    assert 'SERVICE_USER="asrhub"' in text


def test_windows_service_does_not_leave_broken_registration(repo_root: Path):
    """sc.exe регистрировал python.exe как службу — она не может стартовать.

    Диспетчер возвращал ошибку 1053, а служба оставалась зарегистрированной
    с автозапуском и тремя попытками перезапуска при каждой загрузке.
    """
    text = (repo_root / "scripts" / "service.ps1").read_text(encoding="utf-8")
    assert "sc.exe create" not in text, "сломанная регистрация службы вернулась"
    assert "Install-AsTask" in text


def test_web_interface_regressions(repo_root: Path):
    """Правки в интерфейсе, которые нечем проверить кроме как по коду."""
    app_js = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    # Журнал: 403 на /api/logs гасил и панель событий
    assert "Promise.allSettled" in app_js
    # Событие не перерисовывает раздел целиком, стирая фильтры
    assert "function refreshLiveViews" in app_js
    # Файлы, добавленные во время отправки, больше не пропадают
    assert "const batch = state.files.slice()" in app_js
    # Отзыв ключа шлёт то, что сервер принимает
    assert "k.key_id" in app_js
    # У кнопок-иконок есть доступное имя
    assert 'aria-label="Закрыть"' in app_js

    charts_js = (repo_root / "server" / "asrhub" / "web" / "charts.js").read_text(
        encoding="utf-8")
    assert "createTextNode(item.name)" in charts_js, "имя в легенде снова вставляется как HTML"


def test_light_listing_keeps_fields_the_interface_needs(auth_client):
    """Облегчённый список обязан нести прогресс и стадию.

    `/api/queue` перевели на light вместе с сужением по владельцу, а полосу
    выполнения на главном экране интерфейс берёт именно оттуда: без этих
    двух полей она замерла бы на нуле.
    """
    state = auth_client.app.state.hub
    job_id = state.db.create_job({"filename": "з.wav", "status": "running",
                                  "owner": "ключ"})
    state.db.update_job(job_id, status="running", progress=0.42, stage="распознавание")
    item = auth_client.get("/api/queue").json()["items"][0]
    assert item["progress"] == 0.42 and item["stage"] == "распознавание"
    assert "text" not in item, "облегчённый список снова тянет расшифровку"


# ---------------------------------------------------------------------------
# Загрузка GigaAM: куда пишем, что качаем и как называем причину отказа
# ---------------------------------------------------------------------------


РЕАЛЬНЫЙ_ОТКАЗ = [
    "v3_e2e_rnnt: OSError: [Errno 30] Read-only file system: '/home/asrhub'",
    "ai-sage/GigaAM-v3: ValueError: Model 'ai-sage/GigaAM-v3' not found. "
    "Available model names: ['ctc', 'rnnt', 'e2e_ctc', 'e2e_rnnt', 'ssl', 'emo', "
    "'v1_ctc', 'v1_rnnt', 'v1_ssl', 'v2_ctc', 'v2_rnnt', 'v2_ssl', 'v3_ctc', "
    "'v3_rnnt', 'v3_e2e_ctc', 'v3_e2e_rnnt', 'v3_ssl', 'multilingual_ctc']",
    "transformers: InstantiationException: Error in call to target "
    "'modeling_gigaam.GigaAMASR': RuntimeError('Tensor on device cpu is not on "
    "the expected device meta!')",
]


def test_a_model_name_in_the_error_does_not_become_a_diagnosis():
    """«ssl» в перечне доступных моделей выдавалось за отказ сети.

    Отказ был из-за прав на запись, а разбор нашёл в тексте подстроку «ssl» —
    внутри имени модели «ssl» из перечня, который печатает вторая попытка, —
    и объявил, что сервер не достучался до хранилища весов. Человек ушёл
    проверять интернет, а дело было в каталоге.
    """
    from asrhub.engines.gigaam_engine import _load_failure

    отказ = _load_failure("gigaam-v3-e2e-rnnt", РЕАЛЬНЫЙ_ОТКАЗ, "cuda",
                          "/var/lib/asrhub/models")
    текст = str(отказ)
    assert "хранилищу весов" not in текст, "сеть по-прежнему назначена виноватой"
    assert "только для чтения" in текст, текст


def test_the_failing_path_is_named():
    """«Куда именно не удалось записать» — самое полезное в таком отказе."""
    from asrhub.engines.gigaam_engine import _load_failure

    отказ = _load_failure("gigaam-v3-e2e-rnnt", РЕАЛЬНЫЙ_ОТКАЗ, "cuda", "")
    # Путь есть и в общем списке попыток — но там он тонет. Нужна своя строка.
    строки = [s for s in отказ.hint.splitlines() if s.startswith("Путь,")]
    assert строки, "отдельной строки с путём нет:\n" + отказ.hint
    assert "/home/asrhub" in строки[0], строки


def test_the_first_attempt_decides_the_reason():
    """Первая попытка — штатный путь; остальные падают по своим поводам.

    Разбор шёл по склейке всех попыток, и текст запасных мог перебить
    причину из первой. Порядок попыток не случаен, и разбор обязан его
    уважать.
    """
    from asrhub.engines.gigaam_engine import _load_failure

    отказ = _load_failure("m", [
        "v3_rnnt: OSError: [Errno 28] No space left on device",
        "transformers: ConnectionError: Connection refused",
    ], "cpu", "")
    assert "места на диске" in str(отказ), str(отказ)


def test_a_tokenizer_is_not_mistaken_for_a_token():
    """«token» живёт внутри «tokenizer», а его GigaAM качает каждый раз."""
    from asrhub.engines.gigaam_engine import _load_failure

    отказ = _load_failure("m", [
        "v3_e2e_rnnt: RuntimeError: Download of v3_e2e_rnnt_tokenizer.model "
        "failed after 3 attempts.",
    ], "cpu", "")
    assert "токен" not in str(отказ).lower(), str(отказ)


def test_weights_go_where_the_service_may_write(monkeypatch, tmp_path):
    """Библиотека по умолчанию пишет в ~/.cache/gigaam — службе туда нельзя.

    Юнит работает с ProtectHome=read-only, и загрузка падала с OSError
    [Errno 30] на «/home/asrhub». Переменная GIGAAM_MODEL_DIR, на которую мы
    рассчитывали, не читается никем: библиотека берёт каталог только из
    аргумента download_root.
    """
    import sys
    import types

    from asrhub.catalog import get_model
    from asrhub.engines.gigaam_engine import GigaAMEngine

    вызовы = {}

    поддельный = types.ModuleType("gigaam")

    def load_model(name, device=None, download_root=None):
        вызовы["name"] = name
        вызовы["download_root"] = download_root
        return object()

    поддельный.load_model = load_model
    monkeypatch.setitem(sys.modules, "gigaam", поддельный)

    движок = GigaAMEngine(get_model("gigaam-v3-e2e-rnnt"), {})
    движок._load({"models_dir": str(tmp_path), "device": "cpu"})

    assert вызовы["download_root"] == str(tmp_path), (
        "загрузка снова пойдёт в домашний каталог: " + repr(вызовы))
    assert вызовы["name"] == "v3_e2e_rnnt", вызовы


def test_the_repository_id_is_not_attempted():
    """Идентификатор репозитория библиотека не принимает никогда.

    load_model берёт короткое имя варианта или путь к .ckpt, а на всё прочее
    отвечает перечнем доступных имён. Попытка была не просто бесполезной —
    её ответ и сбивал разбор причины.
    """
    import sys
    import types

    from asrhub.catalog import get_model
    from asrhub.engines.gigaam_engine import GigaAMEngine
    from asrhub.errors import ModelLoadError

    попытки = []
    поддельный = types.ModuleType("gigaam")

    def load_model(name, device=None, download_root=None):
        попытки.append(name)
        raise ValueError(f"Model '{name}' not found. Available model names: ['ssl']")

    поддельный.load_model = load_model
    сохранённый = sys.modules.get("gigaam")
    sys.modules["gigaam"] = поддельный
    try:
        движок = GigaAMEngine(get_model("gigaam-v3-e2e-rnnt"), {})
        with pytest.raises(ModelLoadError):
            движок._load({"models_dir": "", "device": "cpu"})
    finally:
        if сохранённый is None:
            sys.modules.pop("gigaam", None)
        else:
            sys.modules["gigaam"] = сохранённый

    assert попытки == ["v3_e2e_rnnt"], (
        "идентификатор репозитория снова в попытках: " + repr(попытки))


def test_the_first_gigaam_repository_does_not_serve_the_third_version():
    """Голое «rnnt» библиотека сама разворачивает в v3_rnnt.

    Значит репозиторий первой версии молча отдавал третью — подмена, которую
    по выводу не заметить.
    """
    from asrhub.engines.gigaam_engine import variant_name

    for ревизия in ("ctc", "rnnt", "ssl"):
        имя = variant_name("ai-sage/GigaAM", ревизия)
        assert имя.startswith("v1_"), f"{ревизия} → {имя}"


def test_downloaded_gigaam_weights_are_seen_as_installed(tmp_path):
    """Веса GigaAM — файл, а не каталог Hugging Face.

    Поиск умел только раскладку `models--владелец--имя`, и скачанная модель
    показывалась незагруженной навсегда, а её размер — нулевым.
    """
    from asrhub import model_files

    assert model_files.find_local(tmp_path, "ai-sage/GigaAM-v3", "e2e_rnnt") is None
    веса = tmp_path / "v3_e2e_rnnt.ckpt"
    веса.write_bytes(b"x" * 2048)

    найдено = model_files.find_local(tmp_path, "ai-sage/GigaAM-v3", "e2e_rnnt")
    assert найдено == веса, найдено
    assert model_files.directory_size(найдено) == 2048, "размер файла не считается"


def test_a_short_word_cannot_match_inside_a_model_name():
    """Слова разбора должны быть неспособны совпасть с посторонним текстом.

    Правило «разбираем первую попытку» спасает лишь тогда, когда шум пришёл
    из запасных. Если перечень доступных имён напечатала сама первая попытка,
    защищают уже только сами слова: короткое «ssl» совпадёт с именем модели
    «ssl», и отказ «такой модели нет» станет отказом сети.
    """
    from asrhub.engines.gigaam_engine import _load_failure

    отказ = _load_failure("m", [
        "v9_rnnt: ValueError: Model 'v9_rnnt' not found. Available model names: "
        "['ctc', 'rnnt', 'ssl', 'emo', 'v3_ssl']",
    ], "cpu", "")
    assert "хранилищу весов" not in str(отказ), (
        "имя модели в перечне снова выдано за отказ сети: " + str(отказ))


def test_the_results_section_offers_playback(repo_root: Path):
    """Проигрыватель должен быть и в карточке, и строкой списка.

    Проверка по разметке, а не по виду: она ловит случай, когда кнопку
    потеряли при перекраивании списка, — а такое видно только глазами и
    только если открыть нужный раздел.
    """
    app = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="job-player"' in app, "в карточке задания нет проигрывателя"
    assert "__asrhub.playRecording" in app, "в списке результатов нет кнопки прослушивания"
    assert "__asrhub.saveRecording" in app, "запись нельзя скачать"

    # Связь с расшифровкой — то, ради чего всё затевалось.
    assert "segment[data-start]" in app, "щелчок по сегменту не переводит звук"
    assert "classList.add('playing')" in app, "звучащий сегмент не подсвечивается"

    css = (repo_root / "server" / "asrhub" / "web" / "styles.css").read_text(encoding="utf-8")
    assert ".segment.playing" in css, "нет оформления для звучащего сегмента"


def test_the_player_does_not_keep_playing_after_the_card_is_closed(repo_root: Path):
    """Узел удалён, а звук идёт — так ведёт себя <audio>, если его не остановить."""
    app = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(encoding="utf-8")
    место = app.index("function setupJobPlayer")
    окно = app[max(0, место - 3000):место]
    assert "asrhub:closed" in окно and "player.destroy()" in окно, (
        "проигрыватель не останавливается вместе с карточкой")


# ---------------------------------------------------------------------------
# Новые разрезы аналитики
# ---------------------------------------------------------------------------


@pytest.fixture()
def rich_db(tmp_path: Path):
    """База с разнообразием, на котором новые разрезы имеют смысл."""
    from asrhub.analytics import Analytics
    from asrhub.db import Database

    database = Database(tmp_path / "rich.sqlite3")
    момент = time.time()
    # Понедельник 10:00 по местному времени — чтобы карта недели была
    # предсказуемой, а не зависела от дня прогона тестов.
    понедельник = момент - (time.localtime(момент).tm_wday * 86400)
    основа = time.mktime(time.localtime(понедельник)[:3] + (10, 0, 0, 0, 0, -1))

    for i in range(6):
        job_id = database.create_job({
            "owner": "alice", "language": "ru", "engine": "gigaam",
            "model": "gigaam-v3-rnnt", "media_duration_s": 120.0,
            "filename": f"разговор-{i}.wav", "file_size": 2_000_000,
            "created_at": основа, "tags": "продажи" if i % 2 else "поддержка",
            "priority": 60 if i < 2 else 50, "source": "phone"})
        database.update_job(job_id, status="completed", finished_at=основа + 40,
                            processing_time_s=20.0, rtf=0.16, queue_time_s=float(i * 5),
                            words_count=300, segments_count=12, avg_confidence=0.9 - i * 0.02,
                            speakers_count=2, device="cuda", peak_memory_mb=2100.0 + i * 10)

    # Задание с повторами, которое так и не дошло.
    сломанное = database.create_job({"owner": "alice", "model": "m", "created_at": основа,
                                     "media_duration_s": 30.0, "filename": "битый.mp3"})
    database.update_job(сломанное, status="failed", retries=2, error_code="decode_error")

    # Задание в другой день недели: без него карту нельзя проверить на то,
    # ради чего она заведена, — что дни не складываются в один.
    среда = database.create_job({"owner": "alice", "model": "m", "media_duration_s": 60.0,
                                 "filename": "среда.wav",
                                 "created_at": основа + 2 * 86400})
    database.update_job(среда, status="completed", finished_at=основа + 2 * 86400 + 30,
                        processing_time_s=10.0, queue_time_s=1.0, device="cpu")

    # Повтор уже виденного файла.
    повтор = database.create_job({"owner": "alice", "model": "gigaam-v3-rnnt",
                                  "created_at": основа, "media_duration_s": 120.0,
                                  "filename": "разговор-0.wav", "file_hash": "abc"})
    database.update_job(повтор, status="completed", cached_from="job_первое",
                        finished_at=основа + 1, queue_time_s=0.2)
    return Analytics(database)


def test_the_week_map_separates_weekdays_from_weekends(rich_db):
    """Суточный профиль складывает понедельник с воскресеньем в одно число.

    Планировать по нему обслуживание нельзя: у телефонии утро понедельника и
    вечер воскресенья — разные миры, а в одной строке они неразличимы.
    """
    карта = rich_db.weekly_heatmap("month")
    assert len(карта["jobs"]) == 7 and len(карта["jobs"][0]) == 24
    assert карта["total"] == 9, карта["total"]

    # Главное: задания в разные дни лежат в разных строках. Если дни
    # схлопнуть, непустой строкой останется одна — и карта превратится в тот
    # же суточный профиль, только выше.
    непустые = [i for i, строка in enumerate(карта["jobs"]) if sum(строка)]
    assert len(непустые) == 2, f"дни сложились в один: {непустые}"
    assert непустые[1] - непустые[0] == 2, непустые

    assert карта["peak"]["jobs"] == 8, карта["peak"]
    assert карта["peak"]["hour"] == 10, карта["peak"]
    сумма = sum(v for строка in карта["jobs"] for v in строка)
    assert сумма == карта["total"], "сумма по карте разошлась с итогом"


def test_reliability_separates_the_first_attempt_from_the_third(rich_db):
    """Доля успеха не отличает безупречное задание от прошедшего с третьего раза."""
    r = rich_db.reliability("month")
    assert r["total"] == 9
    assert r["jobs_with_retries"] == 1, r
    assert r["retry_total"] == 2, r
    # Повторявшееся задание не дошло — значит со второй попытки не дошёл никто.
    assert r["completed_after_retry"] == 0, r
    assert r["first_attempt_success"] == 8, r


def test_cache_savings_are_counted_in_hours_not_in_hits(rich_db):
    """«Из кеша: 1» не отвечает на вопрос, стоило ли оно того."""
    c = rich_db.cache_savings("month")
    assert c["hits"] == 1, c
    # Две минуты записи, снятые повтором.
    assert c["audio_hours_saved"] == pytest.approx(120 / 3600, rel=0.01), c
    assert c["assumed_rtf"] and c["assumed_rtf"] > 0, "не по чему считать экономию"
    assert c["processing_seconds_saved"] > 0, c


def test_queue_latency_shows_the_tail_not_the_average(rich_db):
    """На среднее ожидание не жалуются — жалуются на хвост."""
    q = rich_db.queue_latency("month")
    assert q["overall"]["count"] == 8, q["overall"]
    assert q["overall"]["p95"] >= q["overall"]["p50"], "перцентили не по порядку"
    группы = {r["name"] for r in q["by_priority"]}
    assert "высокий (>50)" in группы and "обычный (50)" in группы, группы


def test_audio_profile_describes_the_material(rich_db):
    """Разрез не про сервер, а про то, что на него приносят."""
    a = rich_db.audio_profile("month")
    форматы = {f["format"]: f for f in a["formats"]}
    assert "wav" in форматы, форматы
    # 300 слов за две минуты — 150 слов в минуту.
    assert a["speech_rate_wpm"]["avg"] == pytest.approx(150, rel=0.02), a["speech_rate_wpm"]
    assert a["speakers"] and a["speakers"][0]["speakers"] == 2, a["speakers"]


def test_resources_answer_the_question_asked_before_buying_a_card(rich_db):
    """Сколько памяти просит модель на пике — и что уехало на процессор."""
    r = rich_db.resources("month")
    модели = {m["model"]: m for m in r["models"]}
    assert "gigaam-v3-rnnt" in модели, модели
    assert модели["gigaam-v3-rnnt"]["peak_mb"] == pytest.approx(2150, rel=0.01), модели
    устройства = {d["device"]: d for d in r["devices"]}
    assert "cuda" in устройства, устройства


def test_tags_are_the_only_breakdown_the_user_defines(rich_db):
    """Модель и движок сервер знает про себя, а метка отвечает «на какой проект»."""
    метки = {t["tag"]: t for t in rich_db.by_tag("month")}
    assert "продажи" in метки and "поддержка" in метки, метки
    assert метки["продажи"]["jobs"] == 3, метки["продажи"]
    assert метки["продажи"]["audio_hours"] > 0, метки["продажи"]


def test_quality_trend_has_a_point_where_there_is_data(rich_db):
    """Средняя уверенность за месяц — число ни о чём; полезен ход."""
    q = rich_db.quality_trend("month", buckets=12)
    assert len(q["buckets"]) == 12
    заполнено = [v for v in q["confidence"] if v is not None]
    assert заполнено, "ход уверенности пуст при наличии данных"
    assert 0 < заполнено[-1] <= 1, заполнено


def test_every_new_section_is_reachable_by_its_own_url(repo_root: Path):
    """Разделы аналитики забирают по одному — сводный отчёт весит десятки килобайт."""
    источник = (repo_root / "server" / "asrhub" / "api" / "routes_system.py").read_text(
        encoding="utf-8")
    for раздел in ("weekly", "cache", "reliability", "audio", "resources",
                   "quality", "tags", "queue"):
        assert f'"{раздел}": state.analytics.' in источник, f"раздел {раздел} не отдаётся"


def test_peak_memory_counter_is_reset_before_the_job_not_after(monkeypatch):
    """Счётчик пика у torch общий на процесс и копится с самого запуска.

    Если обнулять его после замера, первое задание отчитается за всё, что
    успело выделиться до него. Сброс должен быть в начале — и до того, как
    движок что-то посчитает.
    """
    from asrhub import processor as proc

    события: list[str] = []

    class ФейковаяCuda:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def reset_peak_memory_stats() -> None:
            события.append("сброс")

        @staticmethod
        def max_memory_allocated() -> int:
            события.append("замер")
            return 700 * 1024 * 1024

    класс_torch = type(sys)("torch")
    класс_torch.cuda = ФейковаяCuda
    monkeypatch.setitem(sys.modules, "torch", класс_torch)

    proc._reset_peak_memory()
    assert proc._peak_memory_mb("cuda:0") == 700.0
    assert события == ["сброс", "замер"], события


def test_process_job_resets_the_counter_only_when_it_measures(tmp_path: Path,
                                                              monkeypatch):
    """Сброс — не бесплатная операция для соседа по очереди.

    Когда рядом идёт второе задание, замер не делается вовсе (см. очередь),
    и трогать общий счётчик тоже нельзя.
    """
    from asrhub import processor as proc
    from asrhub.errors import AudioError

    сбросов: list[int] = []
    monkeypatch.setattr(proc, "_reset_peak_memory", lambda: сбросов.append(1))

    # Задание падает сразу на несуществующем файле — сброс к тому моменту
    # уже должен был случиться: он идёт до всякой работы.
    источник = tmp_path / "нет.wav"
    for мерить, ожидание in ((False, 0), (True, 1)):
        сбросов.clear()
        with pytest.raises(AudioError):
            proc.process_job(источник, {"model": "нет-такой"},
                             registry=None, workdir=tmp_path, outdir=tmp_path,
                             basename="x", measure_memory=мерить)
        assert len(сбросов) == ожидание, (мерить, сбросов)


def test_gpu_samples_are_cleaned_like_every_other_metric(tmp_path: Path):
    """Замеры по картам — единственная таблица, которую не чистил никто.

    Пишется по строке на карту за такт, то есть на машине с двумя картами
    растёт вдвое быстрее общих замеров. Срок хранения у обеих таблиц один.
    """
    from asrhub.db import Database

    database = Database(tmp_path / "g.sqlite3")
    сейчас = time.time()
    давно = сейчас - 400 * 86400
    for метка in (давно, сейчас):
        database.add_system_sample({"ts": метка, "cpu_percent": 10.0})
        database.add_gpu_samples(метка, [
            {"gpu": 0, "name": "RTX 5090", "util_percent": 50.0,
             "mem_used_mb": 1000.0},
            {"gpu": 1, "name": "RTX 5090", "util_percent": 60.0,
             "mem_used_mb": 1100.0},
        ])

    removed = database.cleanup(results_days=30, metrics_days=30)
    assert removed["gpu_samples"] == 2, removed
    осталось = database.gpu_samples(since=0)
    assert len(осталось) == 2, осталось
    assert all(r["ts"] >= сейчас - 1 for r in осталось), осталось


def test_the_report_reads_the_archive_once_not_twenty_times(rich_db):
    """Два десятка разрезов — два десятка полных проходов по архиву.

    Каждый разрез читал базу сам: на архиве в сорок тысяч заданий сводный
    отчёт собирался тринадцать секунд вместо двух. Разрезы по завершённым и
    по упавшим — отдельные выборки, поэтому проходов остаётся несколько, но
    не по одному на разрез.
    """
    запросы: list[tuple[Any, ...]] = []
    исходный = rich_db.db.list_jobs

    def учёт(**kw):
        запросы.append((kw.get("since"), kw.get("status"), kw.get("owner")))
        return исходный(**kw)

    rich_db.db.list_jobs = учёт
    try:
        отчёт = rich_db.full_report("month")
    finally:
        rich_db.db.list_jobs = исходный

    assert len(отчёт) >= 19, "разрезы потерялись"
    assert len(запросы) <= 5, f"{len(запросы)} проходов по архиву: {запросы}"
    assert len(set(запросы)) == len(запросы), "один и тот же запрос выполнен дважды"


def test_all_sections_of_one_report_cover_the_same_window(rich_db):
    """Иначе «месяц» у первого разреза начинался раньше, чем у последнего.

    Границу окна каждый разрез считал сам, от текущего времени. За секунды
    сборки отчёта она уезжала — разрезы расходились между собой, и общая
    выборка не попадала в кеш ни разу.
    """
    границы: list[float] = []
    исходный = rich_db.db.list_jobs

    def учёт(**kw):
        if kw.get("since") is not None:
            границы.append(float(kw["since"]))
        return исходный(**kw)

    rich_db.db.list_jobs = учёт
    try:
        rich_db.full_report("month")
    finally:
        rich_db.db.list_jobs = исходный

    assert границы, "ни один разрез не ограничил окно"
    assert len(set(границы)) == 1, f"окна разъехались: {sorted(set(границы))}"


def test_outside_a_report_each_section_still_reads_fresh(rich_db):
    """Кеш живёт ровно один отчёт — иначе это просто устаревшие данные.

    Аналитика существует всё время работы приложения; выборка, пережившая
    свой отчёт, показывала бы вчерашний архив как сегодняшний.
    """
    assert rich_db._выборки is None, "кеш остался включённым после отчёта"
    rich_db.overview("month")
    assert rich_db._выборки is None, "разрез вне отчёта включил кеш"


def test_a_recording_with_an_ascii_name_keeps_it_on_save(client, tmp_path: Path):
    """Кнопка «Скачать запись» теряла имя у обычных латинских файлов.

    Заголовок сервер шлёт по-разному: `filename*=` по RFC 5987 появляется
    только у имён с кириллицей, а у «record.wav» его нет. Разбор в плеере
    искал только его, и такой файл сохранялся как «запись-job_….wav».
    """
    исходник = tmp_path / "record.wav"
    _wav_with_lead_silence(исходник, silence_s=0.1, total_s=0.5)

    with исходник.open("rb") as fh:
        ответ = client.post("/api/jobs", files={"file": ("record.wav", fh, "audio/wav")})
    assert ответ.status_code in (200, 201), ответ.text
    job_id = ответ.json()["id"]

    аудио = client.get(f"/api/jobs/{job_id}/audio")
    assert аудио.status_code == 200, аудио.text
    заголовок = аудио.headers.get("content-disposition", "")
    assert "record.wav" in заголовок, заголовок
    assert "filename*=" not in заголовок, (
        "если сервер стал слать RFC 5987 и для латиницы — проверка устарела")

    # Плеер разбирает заголовок общей функцией, а не своей копией.
    app_js = (Path(__file__).resolve().parent.parent / "server" / "asrhub"
              / "web" / "app.js").read_text(encoding="utf-8")
    сохранение = app_js[app_js.index("  async save(id) {"):]
    сохранение = сохранение[:сохранение.index("\n  },")]
    assert "parseFilename(disposition)" in сохранение, сохранение
    assert "filename\\*=utf-8" not in сохранение, "своя копия разбора вернулась"


def test_a_status_code_is_not_recognised_inside_a_file_path():
    """«401» в имени файла отправляло чинить токен вместо загрузки весов.

    Приметы кодов состояния были обычными подстроками, и «401» находилось
    внутри «v3_rnnt-8401.ckpt» и внутри номера порта прокси. Отсутствующий
    на диске файл сервер объявлял отказом в доступе к Hugging Face.
    """
    from asrhub.engines.gigaam_engine import _load_failure

    def причина(*ошибки: str) -> str:
        текст = str(_load_failure("gigaam-v3-rnnt", list(ошибки), "cuda", "/opt"))
        return текст.split("«gigaam-v3-rnnt»", 1)[1].split(".")[0].strip(": ")

    assert причина(
        "v3_rnnt: FileNotFoundError: [Errno 2] No such file or directory: "
        "'/opt/asrhub/models/gigaam/v3_rnnt-8401.ckpt'"
    ) == "веса не найдены на диске"

    assert причина(
        "ConnectionError: HTTPConnectionPool(host='proxy', port=8403): "
        "Max retries exceeded"
    ) == "сервер не смог обратиться к хранилищу весов"

    # А настоящие коды по-прежнему опознаются.
    assert причина(
        "HTTPError: 401 Client Error: Unauthorized for url: https://huggingface.co/x"
    ) == "к весам нужен доступ по токену Hugging Face"
    assert причина(
        "GatedRepoError: 403 Client Error. Access to model is restricted."
    ) == "к весам нужен доступ по токену Hugging Face"


def test_the_two_cache_savings_figures_agree(rich_db):
    """На одной странице стояли два числа про экономию от кеша.

    Разрез «эффективность» считал её по вбитой в код скорости 0.2, а
    разрез «кеш» — по измеренной на своих же заданиях. Расходились вдвое.
    """
    отчёт = rich_db.full_report("month")
    э, к = отчёт["efficiency"], отчёт["cache"]

    assert э["assumed_rtf"] == к["assumed_rtf"], (э["assumed_rtf"], к["assumed_rtf"])
    assert э["assumed_rtf"], "скорость не измерена — экономию не по чему считать"
    assert э["cache_hits"] == к["hits"], (э["cache_hits"], к["hits"])
    # Часы округлены до трёх знаков — это 3.6 секунды; сравниваем в пределах
    # половины этого шага, иначе тест меряет округление, а не согласие.
    assert э["saved_compute_hours"] == pytest.approx(
        к["processing_seconds_saved"] / 3600, abs=0.0005), (э, к)


def test_the_measured_speed_ignores_jobs_taken_from_cache(rich_db):
    """У задания из кеша своего времени обработки нет.

    Включать его в среднее — занижать цену работы тем сильнее, чем чаще
    срабатывает кеш, то есть тем сильнее, чем важнее ответ.
    """
    задания = [
        {"status": "completed", "media_duration_s": 100.0, "processing_time_s": 50.0},
        {"status": "completed", "media_duration_s": 100.0, "cached_from": "job_x",
         "processing_time_s": 0.0},
        {"status": "failed", "media_duration_s": 100.0, "processing_time_s": 90.0},
    ]
    assert rich_db._measured_rtf(задания) == pytest.approx(0.5), "кеш попал в среднее"


def test_every_accepted_format_has_a_content_type_for_playback():
    """Ответ, который браузер не станет играть, — тот же отказ, без объяснения.

    В таблице было двенадцать расширений из двадцати девяти принимаемых, а
    остальные уходили в `mimetypes.guess_type`, который про «.caf», «.w64» и
    «.m2ts» не знает ничего: браузер получал «application/octet-stream».
    """
    from asrhub.api.routes_jobs import _AUDIO_TYPES
    from asrhub.pipeline.audio import SUPPORTED_EXTENSIONS

    без_типа = sorted(SUPPORTED_EXTENSIONS - set(_AUDIO_TYPES))
    assert not без_типа, f"принимаем, но не отдаём на прослушивание: {без_типа}"
    лишние = sorted(set(_AUDIO_TYPES) - SUPPORTED_EXTENSIONS)
    assert not лишние, f"тип есть, а файл такой сервер не принимает: {лишние}"
    for расш, тип in _AUDIO_TYPES.items():
        assert тип.startswith(("audio/", "video/")), (расш, тип)


def test_an_unknown_gigaam_repository_is_not_silently_taken_for_v3(caplog):
    """Голое «rnnt» библиотека разворачивает в v3_rnnt — молча.

    Если в справочник добавят четвёртую версию, не поправив таблицу
    вариантов, сервер будет уверенно качать и грузить веса третьей. Угадать
    тут нечего, но сказать об этом надо.
    """
    from asrhub.engines.gigaam_engine import weights_file

    with caplog.at_level("WARNING"):
        assert weights_file("ai-sage/GigaAM-v4", "rnnt") == "v3_rnnt.ckpt"
    assert any("не описан в таблице вариантов" in r.message for r in caplog.records), \
        caplog.records

    # Известные репозитории предупреждений не дают.
    caplog.clear()
    with caplog.at_level("WARNING"):
        assert weights_file("ai-sage/GigaAM", "rnnt") == "v1_rnnt.ckpt"
        assert weights_file("ai-sage/GigaAM-v3", "e2e_rnnt") == "v3_e2e_rnnt.ckpt"
    assert not caplog.records, caplog.records


def test_the_quality_axis_is_labelled_by_the_width_of_its_bucket(rich_db, repo_root):
    """На часовом окне ход качества подписывал все точки одной датой.

    Подпись была жёстко «день.месяц», а корзина за час — две с половиной
    минуты: двадцать четыре одинаковых подписи вместо оси.
    """
    for период, шире in (("hour", 7200), ("month", 0)):
        ход = rich_db.quality_trend(период)
        assert "bucket_seconds" in ход, "ширина корзины не отдаётся"
        assert ход["bucket_seconds"] > 0, ход["bucket_seconds"]
        if период == "hour":
            assert ход["bucket_seconds"] < шире, (
                "часовое окно должно подписываться временем, а не датой")

    # Пустой ход тоже отдаёт поле — иначе подпись выбирается наугад.
    пустой = rich_db.quality_trend("hour", buckets=4)
    assert "bucket_seconds" in пустой

    app_js = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(
        encoding="utf-8")
    assert "подписьВремени(t, qt.bucket_seconds)" in app_js, \
        "ход качества снова подписывается сам по себе"
    assert "подписьВремени(t, ts.bucket_seconds)" in app_js, \
        "поток заданий перестал пользоваться общей подписью"


def test_the_quality_cards_say_something_when_there_is_nothing(repo_root):
    """Две дырки без объяснения — худший из возможных ответов.

    Когда завершённых заданий за период нет, разрез отдаёт пустые корзины, и
    карточки оставались нарисованными, но пустыми внутри.
    """
    app_js = (repo_root / "server" / "asrhub" / "web" / "app.js").read_text(
        encoding="utf-8")
    начало = app_js.index("const qt = data.quality_trend")
    кусок = app_js[начало:начало + 900]
    assert "!(qt.buckets || []).length" in кусок, кусок[:300]
    assert кусок.count("Charts.empty") >= 2, "пустые карточки снова молчат"
