"""Двадцать седьмой заход: сплошной разбор кода.

Проверки на дефекты, найденные при сплошном чтении всего сервера — базы,
маршрутов, очереди, потока, аналитики, смыслового слоя и скриптов. Каждая
написана так, чтобы падать на прежнем поведении: разрез по владельцу,
который не применялся; ноль, который означал тридцать; предел, который
снимал предел; ошибка, которая затирала работу соседа.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from asrhub.db import Database


def _база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def _запись(db: Database, job_id: str, *, owner: str = "анна", **поля) -> str:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": owner,
                   "model": "demo", "engine": "demo", "media_duration_s": 30.0,
                   **поля})
    db.update_job(job_id, status="completed", finished_at=time.time(),
                  text=поля.get("text") or "здравствуйте, у меня вопрос по оплате")
    return job_id


# ---------------------------------------------------------------------------
# База: разрезы по владельцу, поиск, уборка
# ---------------------------------------------------------------------------


def test_reference_records_do_not_leak_across_owners(tmp_path):
    """Отмеченные эталоном записи отдаются только своему владельцу.

    Метод делает два запроса, и разрез по владельцу стоял только в первом.
    Достаточно было отметить свой разговор эталоном, чтобы он появился в
    разделе у соседнего подразделения — с именем файла, владельцем и всем
    разбором.
    """
    db = _база(tmp_path)
    for job_id, owner in (("j-анна", "анна"), ("j-борис", "борис")):
        _запись(db, job_id, owner=owner)
        db.save_content(job_id, {"version": 1, "sentiment": 0.5, "agent_score": 80})
        db.set_mark(job_id, "reference", "yes", note="эталон")

    свои = db.reference_records(owner="анна", limit=10)
    assert {з["job_id"] for з in свои} == {"j-анна"}, "чужая запись в эталонах"
    чужие = db.reference_records(owner="борис", limit=10)
    assert {з["job_id"] for з in чужие} == {"j-борис"}
    # Администратору (owner=None) видно всё — так и задумано.
    assert {з["job_id"] for з in db.reference_records(limit=10)} == {"j-анна", "j-борис"}


def test_tracker_hits_are_counted_within_the_owner_scope(tmp_path):
    """Срабатывания трекеров считаются по своим записям.

    У метода не было параметра владельца вовсе, и раздел «Категории»
    показывал ключу подразделения числа по всему серверу.
    """
    db = _база(tmp_path)
    for job_id, owner in (("t-анна", "анна"), ("t-борис", "борис")):
        _запись(db, job_id, owner=owner)
        db.add_event(job_id, "tracker", "сработал",
                     {"category": "просрочка", "label": "Просрочка"})
    все = db.tracker_hits()
    assert все and все[0]["hits"] == 2
    свои = db.tracker_hits(owner="анна")
    assert свои and свои[0]["hits"] == 1 and свои[0]["records"] == 1


def test_search_returns_the_newest_matches_not_the_oldest(tmp_path):
    """Предел поиска отрезает старое, а не новое.

    `LIMIT` без сортировки отдавал совпадения в порядке добавления реплик,
    то есть самые давние. Список сортирован «сначала новые», и человек,
    искавший вчерашний разговор в архиве на тысячу совпадений, не находил
    его вовсе.
    """
    db = _база(tmp_path)
    if not db.fts_ready:
        pytest.skip("сборка SQLite без FTS5")
    сейчас = time.time()
    # Вставляем от старых к новым: без сортировки предел отрезал бы именно
    # новые, а по номеру строки они здесь последние.
    for i in range(6):
        job_id = _запись(db, f"s{i}")
        db.execute("UPDATE jobs SET created_at=? WHERE id=?",
                   (сейчас - (6 - i) * 3600, job_id))
        db.save_segments(job_id, [{"start": 0.0, "end": 2.0,
                                   "text": f"договор номер {i} обсудили"}])
    свежие = db.jobs_matching("договор", limit=3)
    assert свежие == ["s5", "s4", "s3"], свежие
    # Главное: самых старых записей в усечённой выдаче быть не должно.
    assert "s0" not in свежие and "s1" not in свежие


def test_like_wildcards_from_the_user_are_taken_literally(tmp_path):
    """Проценты и подчёркивания в запросе — это знаки, а не образцы.

    Без экранирования запрос «%» находил вообще всё, и то же самое находило
    удаление по требованию субъекта персональных данных.
    """
    db = _база(tmp_path)
    _запись(db, "p1", filename="скидка 50%.wav")
    _запись(db, "p2", filename="part_1.wav")
    _запись(db, "p3", filename="обычный.wav")
    db.execute("UPDATE jobs SET filename=? WHERE id=?", ("скидка 50%.wav", "p1"))
    db.execute("UPDATE jobs SET filename=? WHERE id=?", ("part_1.wav", "p2"))
    db.execute("UPDATE jobs SET filename=? WHERE id=?", ("обычный.wav", "p3"))

    по_знаку = db.list_jobs(search="%", limit=50, light=True)
    assert {з["id"] for з in по_знаку} == {"p1"}, "образец LIKE сработал как шаблон"
    точное = db.list_jobs(search="50%", limit=50, light=True)
    assert {з["id"] for з in точное} == {"p1"}
    подчёркивание = db.list_jobs(search="part_1", limit=50, light=True)
    assert {з["id"] for з in подчёркивание} == {"p2"}


def test_the_unique_index_is_restored_by_the_catch_up(tmp_path):
    """Догонялка указателей чинит и уникальный указатель.

    Она пропускала «CREATE UNIQUE INDEX» — то есть единственный указатель,
    который что-то гарантирует. Без него две учётные записи «Admin» и
    «admin» заводятся разом, и вход достаётся произвольной из них.
    """
    db = _база(tmp_path)
    db.execute("DROP INDEX IF EXISTS idx_users_username")
    db.execute("DROP INDEX IF EXISTS idx_jobs_hash")
    db._catch_up_indexes()
    указатели = {r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_users_username" in указатели, "уникальный указатель не восстановлен"
    assert "idx_jobs_hash" in указатели


def test_control_runs_do_not_hang_in_the_pending_count(tmp_path):
    """Контрольные прогоны не попадают в «ожидают разбора».

    Разбирать их незачем — это та же запись второй раз, — и `content_pending`
    их не выдаёт. В «всего» они оставались, поэтому «ожидают» росло на пять
    записей в сутки и не приходило к нулю никогда.
    """
    db = _база(tmp_path)
    _запись(db, "c-обычная")
    _запись(db, "c-контроль", source="control")
    db.save_content("c-обычная", {"version": 1, "sentiment": 0.1})
    свод = db.content_stats(version=1)
    assert свод == {"total": 1, "analyzed": 1, "pending": 0}, свод


def test_the_llm_cache_is_cleaned_by_retention(tmp_path):
    """Кеш ответов модели живёт по сроку хранения, а не вечно.

    В отпечаток подсказки входит расшифровка разговора: без уборки пересказ
    переживал удаление самой записи, то есть данные, которые считались
    удалёнными, оставались в базе.
    """
    db = _база(tmp_path)
    db.llm_cache_put("старый", "main", "модель", "{}", 10.0)
    db.execute("UPDATE llm_cache SET created_at=? WHERE key=?",
               (time.time() - 90 * 86400, "старый"))
    db.llm_cache_put("свежий", "main", "модель", "{}", 10.0)
    убрано = db.cleanup(results_days=30)
    assert убрано["llm_cache"] == 1
    assert db.llm_cache_get("свежий") is not None
    assert db.llm_cache_get("старый") is None


def test_zero_retention_means_forever(tmp_path):
    """Ноль в сроке хранения — «хранить бессрочно», а не «тридцать дней».

    `int(настройка or 30)` превращал ноль в месяц, потому что ноль ложен:
    администратор просил ничего не удалять, а часовая уборка сносила архив
    вместе с исходными файлами.
    """
    from asrhub.maintenance import retention_days

    class _Настройки:
        def __init__(self, значение):
            self.значение = значение

        def get(self, ключ, по_умолчанию=None):
            return self.значение

    assert retention_days(_Настройки(0)) == 0
    assert retention_days(_Настройки("0")) == 0
    assert retention_days(_Настройки(None)) == 30
    assert retention_days(_Настройки("")) == 30
    assert retention_days(_Настройки(15)) == 15
    assert retention_days(_Настройки("чушь")) == 30

    db = _база(tmp_path)
    старое = _запись(db, "old")
    db.execute("UPDATE jobs SET finished_at=?, created_at=? WHERE id=?",
               (time.time() - 400 * 86400, time.time() - 400 * 86400, старое))
    assert db.cleanup(results_days=0)["jobs"] == 0, "ноль удалил записи"
    assert db.get_job("old") is not None


# ---------------------------------------------------------------------------
# Маршруты и права
# ---------------------------------------------------------------------------


def test_monitoring_info_hides_target_urls_from_non_admins(data_dir, monkeypatch):
    """Адреса приёмников метрик прячутся от неадминистратора.

    Соседний `/targets` прятал их нарочно — «у InfluxDB учётные данные
    сплошь и рядом стоят в строке запроса, а входящий адрес чата это
    токен», — а `/info` отдавал тот же список целиком.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    settings = load()
    settings.set("auth_enabled", True)
    settings.set("monitoring_targets", [
        {"kind": "influxdb", "url": "http://пользователь:пароль@influx:8086/write?db=asr",
         "interval_s": 60}])
    settings.api_keys["ah_read_key"] = {"name": "чтение", "role": "read", "enabled": True}
    settings.api_keys["ah_admin_key"] = {"name": "админ", "role": "admin", "enabled": True}
    with TestClient(create_app(settings, start_queue=False)) as c:
        приёмники = c.app.state.hub.monitoring.push.targets()
        assert приёмники, "приёмник не настроился — проверять нечего"
        свод = c.get("/api/monitoring/info", headers={"X-API-Key": "ah_read_key"}).json()
        assert свод.get("targets"), "в ответе нет приёмников"
        адреса = " ".join(str(t.get("url") or "") for t in (свод.get("targets") or []))
        assert "пароль" not in адреса, свод.get("targets")
        админ = c.get("/api/monitoring/info", headers={"X-API-Key": "ah_admin_key"}).json()
        адреса_админа = " ".join(str(t.get("url") or "") for t in (админ.get("targets") or []))
        if админ.get("targets"):
            assert "influx" in адреса_админа


def test_the_event_feed_limit_is_bounded(client):
    """Предел ленты событий ограничен снизу и сверху.

    Голый `int` пропускал ноль и отрицательное, а «LIMIT -1» в SQLite
    снимает предел вовсе: любой ключ поднимал в память всю таблицу событий
    за девяносто дней.
    """
    assert client.get("/api/events?limit=-1").status_code == 422
    assert client.get("/api/events?limit=0").status_code == 422
    assert client.get("/api/events?limit=100000").status_code == 422
    assert client.get("/api/events?limit=50").status_code == 200


def test_the_model_list_needs_a_key_and_hides_paths(data_dir, monkeypatch):
    """Список моделей требует ключа, а путь к весам виден только админу.

    Разрез `installed=true` обходит каталог моделей на диске, для части
    моделей рекурсивно, — без аутентификации это делал кто угодно из сети,
    минуя и учёт частоты запросов. Путь к весам — раскладка файловой
    системы сервера, её соседний `/api/system` прячет нарочно.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    settings = load()
    settings.set("auth_enabled", True)
    settings.api_keys["ah_read_key"] = {"name": "чтение", "role": "read", "enabled": True}
    settings.api_keys["ah_admin_key"] = {"name": "админ", "role": "admin", "enabled": True}
    from asrhub import model_files

    monkeypatch.setattr(model_files, "find_local",
                        lambda *a, **k: Path("/srv/asrhub/data/models/веса"))
    monkeypatch.setattr(model_files, "directory_size", lambda p: 1024)
    with TestClient(create_app(settings, start_queue=False)) as c:
        assert c.get("/api/models").status_code in (401, 403)
        свой = c.get("/api/models?installed=true", headers={"X-API-Key": "ah_read_key"})
        assert свой.status_code == 200
        весь = c.get("/api/models", headers={"X-API-Key": "ah_read_key"}).json()
        модель = весь["items"][0]["id"]
        читателю = c.get(f"/api/models/{модель}/status",
                         headers={"X-API-Key": "ah_read_key"}).json()
        assert читателю["downloaded"] is True
        assert читателю["path"] is None, "путь к весам ушёл неадминистратору"
        админу = c.get(f"/api/models/{модель}/status",
                       headers={"X-API-Key": "ah_admin_key"}).json()
        assert админу["path"] == "/srv/asrhub/data/models/веса"


def test_masking_covers_what_the_model_retells(client):
    """Маскирование покрывает поля смыслового слоя.

    Модель пересказывает разговор своими словами, и номер карты попадает в
    пересказ так же, как в расшифровку. Раньше `quote` маскировалась, а
    `summary` в том же объекте — нет.
    """
    from asrhub.content import masking

    ответ = masking.mask_payload({
        "summary": "Клиент продиктовал карту 4111 1111 1111 1111",
        "reason_quote": "звоните на 8 916 123 45 67",
        "outcome_quote": "почта ivan@example.com",
        "actions": [{"what": "перезвонить на 8 916 123 45 67", "who": "сотрудник"}],
        "scorecard": [{"answer": "да", "question": "назвал ли карту 4111111111111111"}],
    })
    сплошняком = json.dumps(ответ, ensure_ascii=False)
    for утечка in ("4111", "916 123", "ivan@example.com"):
        assert утечка not in сплошняком, сплошняком
    assert "[карта]" in ответ["summary"] and "[телефон]" in ответ["reason_quote"]


# ---------------------------------------------------------------------------
# Очередь и поток
# ---------------------------------------------------------------------------


def test_a_failure_before_the_pipeline_does_not_leave_the_job_running(tmp_path, monkeypatch):
    """Сбой до конвейера отмечает задание неудавшимся, а не бросает его.

    Участок между «взял задание» и запуском конвейера ничем не был
    защищён: кончилось место под рабочий каталог — и задание висело
    «выполняется, 0 %» вечно. Подхват зависших берёт только чужие
    экземпляры, а свои — лишь при старте.
    """
    import threading

    from asrhub import job_queue as jq

    db = _база(tmp_path)
    db.create_job({"id": "стоп", "filename": "a.wav", "owner": "анна",
                   "model": "demo", "engine": "demo", "file_path": str(tmp_path / "a.wav")})
    job = db.get_job("стоп")
    db.update_job("стоп", status="running", instance_id=jq.INSTANCE_ID)

    очередь = jq.JobQueue.__new__(jq.JobQueue)
    очередь.db = db
    очередь.settings = type("Н", (), {"merged": lambda s, p: {}, "values": {},
                                      "paths": type("П", (), {"tmp": tmp_path,
                                                              "results": tmp_path})()})()
    очередь._lock = threading.RLock()
    очередь._stop = threading.Event()
    очередь._wake = threading.Event()
    очередь._retiring = set()
    очередь._states = {0: jq.WorkerState(index=0)}
    очередь._cancelled = set()
    очередь._release_slot = lambda job_id, model: None
    выдать = [job]

    def _next_job(index):
        if выдать:
            return выдать.pop()
        очередь._stop.set()
        return None

    def _execute(job, state):
        raise OSError("кончилось место под рабочий каталог")

    очередь._next_job = _next_job
    очередь._execute = _execute
    # Прогоняем настоящий цикл воркера: дефект был именно в нём — сбой до
    # конвейера ловился только записью в журнал.
    jq.JobQueue._worker_loop(очередь, 0)

    строка = db.get_job("стоп")
    assert строка["status"] == "failed", строка["status"]
    assert строка["error_message"]


def test_restart_recovers_jobs_of_a_process_that_is_gone(tmp_path):
    """После перезапуска свои задания возвращаются в очередь сразу.

    В отметке экземпляра стоит номер процесса, и после перезапуска он
    другой: по точному совпадению не находилось ничего, задания висели
    «выполняется» до подхвата зависших — а тот тратит попытку и пишет
    «экземпляр перестал отвечать».
    """
    from asrhub import job_queue as jq
    from asrhub.instance import HOSTNAME

    db = _база(tmp_path)
    for job_id, отметка in (("мой-прошлый", f"{HOSTNAME}:999999"),
                            ("соседний", "другая-машина:17"),
                            ("ничей", "")):
        db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": "анна",
                       "model": "demo", "engine": "demo"})
        db.update_job(job_id, status="running", instance_id=отметка)
    очередь = jq.JobQueue.__new__(jq.JobQueue)
    очередь.db = db
    assert jq.JobQueue.recover(очередь) == 2
    assert db.get_job("мой-прошлый")["status"] == "queued"
    assert db.get_job("ничей")["status"] == "queued"
    assert db.get_job("соседний")["status"] == "running", "забрали чужое"


def test_the_results_of_another_instance_are_not_deleted(tmp_path):
    """Каталог результатов, которым занят другой экземпляр, не сносится.

    Все места, откуда зовут уборку, — это провал защищённой записи, то есть
    в том числе «задание перехватил сосед». Путь `results/<id>` общий, и
    `rmtree` сносил готовую работу соседа.
    """
    from asrhub import job_queue as jq

    db = _база(tmp_path)
    db.create_job({"id": "общее", "filename": "a.wav", "owner": "анна",
                   "model": "demo", "engine": "demo"})
    каталог = tmp_path / "results" / "общее"
    каталог.mkdir(parents=True)
    (каталог / "готово.txt").write_text("работа соседа", encoding="utf-8")
    очередь = jq.JobQueue.__new__(jq.JobQueue)
    очередь.db = db

    db.update_job("общее", status="running", instance_id="сосед:42")
    очередь._discard_unless_taken(каталог, "общее")
    assert каталог.exists(), "снесли работу перехватившего экземпляра"

    # А свою неудавшуюся выгрузку убрать нужно.
    db.update_job("общее", status="cancelled", instance_id=None)
    очередь._discard_unless_taken(каталог, "общее")
    assert not каталог.exists()


def test_the_streaming_tail_is_not_recognised_twice(tmp_path):
    """Закреплённый текст покрывает ровно тот звук, который выброшен.

    Раньше текст брался по всему хвосту, а звук выбрасывался только до
    тихого места: остаток в одну-две секунды, чей текст уже ушёл как final,
    распознавался следующим окном ещё раз — в расшифровке это повтор
    последних слов каждые полминуты.
    """
    import struct

    from asrhub import streaming

    сессия = streaming.StreamSession.__new__(streaming.StreamSession)
    громко = struct.pack("<16000h", *([8000, -8000] * 8000))
    тихо = struct.pack("<16000h", *([0] * 16000))
    сессия._pcm = bytearray(громко + тихо)
    сессия._committed_s = 0.0
    сессия._last_partial = "хвост"
    сессия._final_text = ""
    сессия._first_text_at = None
    сессия._native = None
    спрошено: list[int] = []

    def распознать(данные: bytes) -> str:
        спрошено.append(len(данные))
        return "распознанный кусок"

    сессия._recognize = распознать
    события = сессия._commit()
    assert события and события[0].type == "final"
    assert спрошено, "текст не пересчитан по закрепляемой части"
    assert спрошено[0] < 64000, "распознан весь хвост, а не закрепляемая часть"
    assert спрошено[0] == 64000 - len(сессия._pcm), "текст и выброшенный звук разошлись"


def test_the_native_stream_does_not_hoard_audio():
    """В режиме движка звук не копится в памяти сессии.

    Хвост нужен только оконному режиму; в режиме потока движок ведёт его
    сам, а копия здесь давала бы сто десять мегабайт на предельной сессии.
    """
    from asrhub import streaming

    сессия = streaming.StreamSession.__new__(streaming.StreamSession)
    сессия._pcm = bytearray()
    сессия._native_bytes = 0
    сессия._committed_s = 0.0
    сессия._closed = False
    сессия._started = time.time()
    сессия._since_flush = 0
    сессия._native = type("Движок", (), {"accept": lambda s, pcm: None})()
    сессия.decoder = type("Декодер", (), {"feed": lambda s, c: c})()
    for _ in range(50):
        сессия.feed(b"\x00\x01" * 1600)
    assert len(сессия._pcm) == 0, "звук копится в буфере"
    assert сессия.duration_s > 0, "длительность сессии потерялась"


# ---------------------------------------------------------------------------
# Показатели
# ---------------------------------------------------------------------------


def test_the_ks_test_survives_repeated_values():
    """Критерий Колмогорова — Смирнова не выдумывает расхождение на повторах.

    Прежний цикл двигал один указатель и на блоке одинаковых значений
    «видел» разницу в целый блок: две одинаковые выборки давали D = 0,29
    вместо 0,05 — и вердикт «критично» на ровном месте. А так бывает:
    запись из кеша копирует уверенность оригинала.
    """
    from asrhub.stats import ks_test

    a = [0.9] * 200 + [0.8] * 200
    b = [0.9] * 200 + [0.8] * 200
    итог = ks_test(a, b)
    assert итог["d"] == 0.0, итог
    assert итог["p"] == 1.0
    # Настоящее расхождение по-прежнему видно.
    разные = ks_test([0.9] * 200, [0.5] * 200)
    assert разные["d"] == 1.0 and разные["p"] == 0.0


def test_the_wer_metric_is_pooled_by_words(client):
    """WER в метриках складывается по словам, а не усредняется по записям.

    Три десятисекундные реплики с WER 0,5 и часовая встреча с WER 0,03
    давали «средний» 0,38 и пробивали порог тревоги, тогда как по словам
    это 0,03.
    """
    state = client.app.state.hub
    сейчас = time.time()
    for i in range(3):
        job_id = f"м{i}"
        state.db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": "анна",
                             "model": "быстрая", "engine": "demo"})
        state.db.update_job(job_id, status="completed", finished_at=сейчас, wer=0.5,
                            ref_words=10, sub_words=5, del_words=0, ins_words=0)
    state.db.create_job({"id": "мдолгая", "filename": "долгая.wav", "owner": "анна",
                         "model": "быстрая", "engine": "demo"})
    state.db.update_job("мдолгая", status="completed", finished_at=сейчас, wer=0.03,
                        ref_words=10000, sub_words=300, del_words=0, ins_words=0)

    метрики = client.get("/api/monitoring/metrics").text
    строки = [с for с in метрики.splitlines()
              if с.startswith("asrhub_wer{") and 'model="быстрая"' in с]
    assert строки, метрики[:400]
    значение = float(строки[0].rsplit(" ", 1)[1])
    assert значение < 0.1, f"WER усреднён по записям: {значение}"


def test_the_snr_method_says_when_it_is_not_snr():
    """Профиль звука не называет перцентили разметкой речи.

    Если VAD не оставил ни одного кадра под шум, «шумом» становятся провалы
    между слогами — это динамический диапазон речи, а не отношение
    сигнал/шум. С пометкой «vad» ровный диктор в студии попадал в «плохой
    звук», а эмоциональный — в отличный.
    """
    import math

    from asrhub.pipeline import audio_profile as ап

    частота = 16000
    сэмплы = [0.25 * math.sin(2 * math.pi * 220 * t / частота) for t in range(частота * 2)]
    вся_речь = ап.profile(сэмплы, частота, speech_spans=[(0.0, 2.0)])
    assert вся_речь["method"] == "percentile", вся_речь["method"]
    с_паузой = ап.profile(сэмплы, частота, speech_spans=[(0.0, 1.0)])
    assert с_паузой["method"] == "vad"


def test_the_insights_report_freezes_its_window(tmp_path):
    """Все разрезы отчёта считаются по одной границе периода.

    Граница бралась заново в каждом разрезе — за секунды сборки отчёта по
    большому архиву поздние разрезы видели записи, которых не видели
    ранние, и свод переставал сходиться с суммой по разрезам.
    """
    from asrhub.insights import Insights

    db = _база(tmp_path)
    отчёты = Insights(db)
    отчёты._окна = {}
    первая = отчёты.window("week")
    time.sleep(0.05)
    вторая = отчёты.window("week")
    assert первая == вторая, "граница окна уехала внутри отчёта"
    отчёты._окна = None
    третья = отчёты.window("week")
    time.sleep(0.05)
    assert третья != отчёты.window("week"), "без отчёта граница должна быть живой"


# ---------------------------------------------------------------------------
# Смысловой слой и очередь проверки
# ---------------------------------------------------------------------------


def test_a_broken_answer_does_not_poison_the_cache(tmp_path):
    """Неразбираемый ответ модели не оседает в кеше.

    Рассуждающая модель на первый вызов отвечает размышлением без JSON.
    Такой ответ ложился в кеш навсегда, и «Разобрать заново» вечно
    возвращало ту же ошибку, не спрашивая сервер модели.
    """
    from asrhub.llm.client import LLMClient
    from asrhub.llm.tasks import parse_json

    db = _база(tmp_path)

    class _Настройки:
        значения = {"llm_backend": "openai", "llm_model": "м", "llm_max_concurrent": 1,
                    "llm_timeout_s": 5, "llm_min_free_vram_gb": 0, "llm_url": "http://x"}

        def get(self, ключ, по_умолчанию=None):
            return self.значения.get(ключ, по_умолчанию)

    клиент = LLMClient(_Настройки(), db)
    ответы = ["сначала подумаю вслух, без всякого JSON", '{"summary": "готово"}']
    клиент._openai = lambda *a, **k: ответы.pop(0)

    первый = клиент.chat("с", "п", kind="main", validate=parse_json)
    assert "подумаю" in первый
    ключ = клиент.cache_key("main", "с", "п")
    assert db.llm_cache_get(ключ) is None, "неразбираемый ответ осел в кеше"
    второй = клиент.chat("с", "п", kind="main", validate=parse_json)
    assert второй == '{"summary": "готово"}', "мусор из кеша вернулся вторым вызовом"
    третий = клиент.chat("с", "п", kind="main", validate=parse_json)
    assert третий == '{"summary": "готово"}' and клиент.cache_hits >= 1


def test_a_failure_does_not_erase_trackers_and_scorecard(tmp_path):
    """Сбой модели не затирает разбор, состоящий из трекеров и скоркарты.

    Защита смотрела только на резюме и исход, а сервер, настроенный на один
    контроль качества (`llm_tasks`), терял всё при первом же сбое.
    """
    from asrhub.llm import tasks
    from asrhub.llm.worker import LLMWorker

    db = _база(tmp_path)
    _запись(db, "к1")
    db.llm_save("к1", tasks.VERSION, model="м",
                trackers=[{"id": "т", "label": "Трекер", "fired": True, "quote": ""}],
                scorecard=[{"id": "в", "question": "Поздоровался?", "answer": "да"}])
    поток = LLMWorker.__new__(LLMWorker)
    поток.db = db
    поток.client = type("К", (), {"model": "м"})()
    поток._сбои = {}
    поток._отметить_сбой("к1", "сервер лёг")
    осталось = db.llm_get("к1")
    assert осталось["trackers"], "трекеры затёрты строкой с ошибкой"
    assert осталось["scorecard"]


def test_not_applicable_is_not_counted_as_no():
    """Ответ «неприменимо» не превращается в «нет».

    Подсказка сама предлагает модели это слово, а начинается оно с «не» —
    и ответ «ситуации в разговоре не было» штрафовал оператора за то, чего
    он не мог сделать.
    """
    from asrhub.llm import tasks

    class _Клиент:
        model = "заглушка"
        enabled = True

        def chat(self, system, user, *, kind="chat", **_):
            return json.dumps({"answers": [
                {"id": "в1", "answer": "неприменимо", "quote": ""},
                {"id": "в2", "answer": "н/п", "quote": ""},
                {"id": "в3", "answer": "нет", "quote": ""},
                {"id": "в4", "answer": "да", "quote": ""}]}, ensure_ascii=False)

    class _Настройки:
        значения = {
            "llm_tasks": ["scorecard"], "llm_context_chars": 12000,
            "llm_scorecard": [{"id": f"в{i}", "question": f"Вопрос {i}?"} for i in range(1, 5)],
            "llm_reasons": [], "llm_outcomes": [], "llm_trackers": [],
        }

        def get(self, ключ, по_умолчанию=None):
            return self.значения.get(ключ, по_умолчанию)

    итог = tasks.analyze(_Клиент(), text="разговор", segments=[], settings=_Настройки())
    ответы = {о["id"]: о["answer"] for о in итог["scorecard"]}
    assert ответы == {"в1": "н/п", "в2": "н/п", "в3": "нет", "в4": "да"}, ответы


def test_the_daily_review_quota_is_shared_between_reasons(monkeypatch):
    """Дневной предел очереди проверки делится между причинами.

    Случайные отбирались первыми и на потоке в две тысячи записей занимали
    предел целиком: нижняя четверть по уверенности не попадала в очередь
    никогда, а настройка `review_daily_low` ни на что не влияла.
    """
    import random

    from asrhub import review

    class _БД:
        def __init__(self, n):
            self.n = n
            self.добавлено: list[tuple[str, str]] = []

        def review_queued_ids(self):
            return set()

        def review_add(self, job_id, причина, picked_at=None):
            self.добавлено.append((job_id, причина))
            return True

    class _Настройки:
        def get(self, ключ, по_умолчанию=None):
            return {"review_daily_share": 1, "review_daily_low": 3,
                    "review_daily_max": 20}.get(ключ, по_умолчанию)

    monkeypatch.setattr(review, "_кандидаты", lambda db, since=None: [
        {"id": f"j{i}", "avg_confidence": 0.5 + (i % 100) / 200.0, "ref_words": None}
        for i in range(db.n)])

    итог = review.sample_review(_БД(2000), _Настройки(), rng=random.Random(1))
    assert итог["low_confidence"] == 3, итог
    assert итог["random"] + итог["low_confidence"] <= 20
    # На трёх записях за сутки «нижней четверти» нет — и выдумывать её не надо.
    мало = review.sample_review(_БД(3), _Настройки(), rng=random.Random(1))
    assert мало["low_confidence"] == 0, мало


# ---------------------------------------------------------------------------
# Интерфейс и скрипты — то, что проверяется чтением файла
# ---------------------------------------------------------------------------


def test_the_ui_escapes_apostrophes(repo_root: Path):
    """Экранирование разметки закрывает апостроф.

    Половина обработчиков написана как onclick="…('${esc(id)}')", то есть
    значение попадает внутрь строки JS, ограниченной апострофом. Кавычка
    там не спасает — разбор ломает именно апостроф.
    """
    текст = (repo_root / "server/asrhub/web/app.js").read_text(encoding="utf-8")
    начало = текст.index("function esc(value)")
    тело = текст[начало:начало + 400]
    assert "&#39;" in тело, "апостроф не экранируется"
    графики = (repo_root / "server/asrhub/web/charts.js").read_text(encoding="utf-8")
    assert "escText(part.label)" in графики, "подпись кольца попадает в разметку как есть"


def test_the_installer_survives_a_broken_nvidia_smi(repo_root: Path):
    """Сломанный nvidia-smi не обрывает установку.

    Драйвер поставлен, но NVML ещё не отвечает — обычное состояние сразу
    после того, как этот же установщик драйвер и поставил. Под `pipefail`
    код возврата становился кодом функции, и `errexit` убивал установку на
    шаге «окружение», не сказав ни слова про видеокарту.
    """
    текст = (repo_root / "scripts/lib/detect.sh").read_text(encoding="utf-8")
    for функция in ("detect_gpu_name", "detect_gpu_memory_mb", "detect_cuda_version"):
        начало = текст.index(f"{функция}()")
        тело = текст[начало:текст.index("\n}", начало)]
        assert "|| true" in тело, функция
    служба = (repo_root / "scripts/service.sh").read_text(encoding="utf-8")
    assert "{ err " not in служба, "вызов несуществующей функции err"
