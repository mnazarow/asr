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
