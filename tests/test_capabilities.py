"""Регрессии на четыре возможности, добавленные последним заходом.

Каждая закрывает не «код запускается», а конкретное поведение, ради
которого возможность появилась: кеш обязан замечать смену весов, ключи
одного подразделения — видеть общие задания и упираться в квоту, второй
экземпляр сервера — не хватать чужое задание и подбирать брошенное, а
поток — отдавать текст до конца записи.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Кеш и версия весов
# ---------------------------------------------------------------------------


def _weights(directory: Path, size: int = 2048) -> Path:
    """Каталог, похожий на кеш Hugging Face: models--владелец--имя."""
    home = directory / "models--sberdevices--GigaAM-v3"
    (home / "snapshots" / "abc").mkdir(parents=True, exist_ok=True)
    (home / "snapshots" / "abc" / "model.bin").write_bytes(b"\0" * size)
    (home / "config.json").write_text('{"kind": "тест"}', encoding="utf-8")
    return home


def test_fingerprint_changes_when_weights_change(tmp_path: Path):
    """Обновление модели под тем же именем обязано менять отпечаток.

    Ради этого отпечаток и заведён: GigaAM и Whisper выкладывают новые
    ревизии под прежним идентификатором, и кеш отдавал результат прошлой
    версии как свежий — без единого признака подмены.
    """
    from asrhub import model_files

    models = tmp_path / "models"
    home = _weights(models)
    model_files.forget(models)
    before = model_files.fingerprint(models, "sberdevices/GigaAM-v3")
    assert before, "отпечаток скачанной модели не может быть пустым"

    # Перезаписываем веса — как это делает загрузка новой ревизии.
    (home / "snapshots" / "abc" / "model.bin").write_bytes(b"\1" * 4096)
    model_files.forget(models)
    assert model_files.fingerprint(models, "sberdevices/GigaAM-v3") != before


def test_fingerprint_is_cached_and_forgettable(tmp_path: Path):
    """Обход диска не повторяется на каждое задание, но сбрасывается по требованию."""
    from asrhub import model_files

    models = tmp_path / "models"
    home = _weights(models)
    model_files.forget(models)
    first = model_files.fingerprint(models, "sberdevices/GigaAM-v3")

    (home / "config.json").write_text('{"kind": "другое"}' * 40, encoding="utf-8")
    assert model_files.fingerprint(models, "sberdevices/GigaAM-v3") == first, \
        "отпечаток обязан браться из памяти в пределах TTL"
    model_files.forget(models)
    assert model_files.fingerprint(models, "sberdevices/GigaAM-v3") != first, \
        "forget() обязан заставить пересчитать отпечаток"


def test_missing_weights_keep_the_old_cache_behaviour(tmp_path: Path):
    """Весов на диске нет — отпечаток пуст, и кеш работает как до правки."""
    from asrhub import model_files
    from asrhub.processor import settings_digest

    model_files.forget()
    assert model_files.fingerprint(tmp_path / "нет", "sberdevices/GigaAM-v3") == ""
    settings = {"model": "gigaam-v3-rnnt", "language": "ru"}
    assert settings_digest(settings, weights="") == settings_digest(settings)


def test_digest_separates_results_of_different_weights():
    """Ключ кеша обязан различать одинаковые настройки на разных весах."""
    from asrhub.processor import settings_digest

    settings = {"model": "gigaam-v3-rnnt", "language": "ru", "beam_size": 5}
    old = settings_digest(settings, weights="1111aaaa")
    new = settings_digest(settings, weights="2222bbbb")
    assert old != new, "смена весов не отразилась в ключе кеша"
    assert old == settings_digest(dict(settings), weights="1111aaaa"), \
        "ключ кеша обязан оставаться устойчивым при тех же весах"


# ---------------------------------------------------------------------------
# Подразделения и квоты
# ---------------------------------------------------------------------------


@pytest.fixture()
def keys_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с проверкой ключей и без запущенной очереди."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as client:
        admin = (tmp_path / "data" / "api-key.txt").read_text(encoding="utf-8").strip()
        client.headers.update({"X-API-Key": admin})
        yield client


def _make_key(client, name: str, **extra):
    response = client.post("/api/keys", json={"name": name, "role": "user", **extra})
    assert response.status_code == 200, response.text
    return response.json()["key"]


def test_group_members_share_jobs_and_outsiders_do_not(keys_client):
    """Подразделение — то разделение, которого не хватало трём ролям.

    Два сотрудника бухгалтерии обязаны видеть общий набор заданий, а отдел
    продаж — не видеть его вовсе. По одному ключу на человека это было
    недостижимо: «своё» кончалось на владельце.
    """
    first = _make_key(keys_client, "бухгалтер-1", group="бухгалтерия")
    second = _make_key(keys_client, "бухгалтер-2", group="бухгалтерия")
    outsider = _make_key(keys_client, "продажи-1", group="продажи")
    state = keys_client.app.state.hub
    job_id = state.db.create_job({"filename": "акт-сверки.wav", "owner": "бухгалтер-1",
                                  "status": "completed", "text": "СВОДКА ПО ОПЛАТАМ",
                                  "model": "demo-simulator"})

    listing = keys_client.get("/api/jobs", headers={"X-API-Key": second})
    assert listing.status_code == 200, listing.text
    assert [j["id"] for j in listing.json()["items"]] == [job_id], \
        "коллега по подразделению не видит общее задание"
    assert keys_client.get(f"/api/jobs/{job_id}",
                           headers={"X-API-Key": second}).status_code == 200

    alien = keys_client.get("/api/jobs", headers={"X-API-Key": outsider})
    assert alien.json()["items"] == [], "чужое подразделение видит задание"
    assert "СВОДКА ПО ОПЛАТАМ" not in alien.text
    assert keys_client.get(f"/api/jobs/{job_id}",
                           headers={"X-API-Key": outsider}).status_code == 403
    assert first  # ключ владельца создан и не мешает проверке


def test_key_without_group_sees_only_its_own(keys_client):
    """Ключ вне подразделения по-прежнему ограничен своими заданиями."""
    lonely = _make_key(keys_client, "одиночка")
    state = keys_client.app.state.hub
    state.db.create_job({"filename": "чужое.wav", "owner": "кто-то",
                         "status": "completed", "model": "demo-simulator"})
    assert keys_client.get("/api/jobs", headers={"X-API-Key": lonely}).json()["items"] == []


def test_daily_quota_stops_the_key_before_the_upload(keys_client, sample_wav: Path):
    """Роли отвечали «что можно», но не «сколько».

    Один ключ занимал всю очередь и весь диск, и остановить это можно было
    только отключив его целиком.
    """
    limited = _make_key(keys_client, "с-квотой", quota_jobs_per_day=2)
    headers = {"X-API-Key": limited}

    for attempt in (1, 2):
        with sample_wav.open("rb") as handle:
            response = keys_client.post("/api/jobs", files={"file": ("з.wav", handle,
                                                                    "audio/wav")},
                                        headers=headers)
        assert response.status_code == 200, f"задание {attempt}: {response.text}"

    with sample_wav.open("rb") as handle:
        refused = keys_client.post("/api/jobs",
                                   files={"file": ("з.wav", handle, "audio/wav")},
                                   headers=headers)
    assert refused.status_code == 429, refused.text
    detail = refused.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert "quota_jobs_per_day" in json.dumps(detail, ensure_ascii=False), \
        "в отказе нет имени параметра, который надо поднять"


def test_quota_is_visible_before_the_refusal(keys_client, sample_wav: Path):
    """О квоте узнавали только в момент отказа — уже после загрузки файла."""
    limited = _make_key(keys_client, "видимая-квота", quota_jobs_per_day=5,
                        quota_audio_hours_per_day=1.5)
    headers = {"X-API-Key": limited}
    with sample_wav.open("rb") as handle:
        assert keys_client.post("/api/jobs", files={"file": ("з.wav", handle, "audio/wav")},
                                headers=headers).status_code == 200

    usage = keys_client.get("/api/usage", headers=headers).json()
    assert usage["used"]["jobs"] == 1
    assert usage["limits"]["jobs"] == 5
    assert usage["remaining"]["jobs"] == 4
    assert usage["limits"]["storage_gb"] is None, "нулевая квота — это «без ограничения»"


def test_admin_is_not_limited_by_quotas(keys_client, sample_wav: Path):
    """Иначе обслуживание сервера упиралось бы в ту же стену, что и злоупотребление."""
    from asrhub.api.deps import Principal
    from asrhub.api.routes_jobs import check_quota

    state = keys_client.app.state.hub
    for index in range(3):
        state.db.create_job({"filename": f"{index}.wav", "owner": "админ",
                             "status": "completed", "model": "demo-simulator"})

    class _Request:
        app = keys_client.app

    admin = Principal(name="админ", role="admin", quota_jobs_per_day=1)
    check_quota(_Request(), admin)          # не должно бросить


# ---------------------------------------------------------------------------
# Несколько экземпляров сервера
# ---------------------------------------------------------------------------


@pytest.fixture()
def database(tmp_path: Path):
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    try:
        yield db
    finally:
        db.close()


def test_only_one_instance_claims_a_job(database):
    """Захват задания обязан быть неделимым.

    Раньше статус читался, а потом записывался: два сервера над одной базой
    успевали прочитать «в очереди» оба и считали одну и ту же запись дважды —
    вдвойне платя за GPU и дважды дёргая уведомление.
    """
    from asrhub.job_queue import STATUS_QUEUED, STATUS_RUNNING

    job_id = database.create_job({"filename": "спор.wav", "status": STATUS_QUEUED,
                                  "model": "demo-simulator"})
    first = database.update_job_if_status(job_id, [STATUS_QUEUED],
                                          status=STATUS_RUNNING, instance_id="сервер-1")
    second = database.update_job_if_status(job_id, [STATUS_QUEUED],
                                           status=STATUS_RUNNING, instance_id="сервер-2")
    assert first is True, "первый экземпляр не смог захватить задание"
    assert second is False, "задание захвачено дважды"
    assert database.get_job(job_id)["instance_id"] == "сервер-1"


def test_stale_job_returns_to_the_queue(database, tmp_path: Path):
    """Сервер, убитый по нехватке памяти, оставлял задание в «выполняется» навсегда.

    Пользователь видел вечные 40 % и не мог ни дождаться, ни понять, что
    случилось.
    """
    from asrhub import job_queue as jq

    old = jq.now() - jq.STALE_AFTER_S - 60
    lost = database.create_job({"filename": "брошено.wav", "status": jq.STATUS_RUNNING,
                                "model": "demo-simulator"})
    database.update_job(lost, instance_id="умерший-сервер:1", heartbeat_at=old,
                        started_at=old, progress=0.4)
    mine = database.create_job({"filename": "моё.wav", "status": jq.STATUS_RUNNING,
                                "model": "demo-simulator"})
    database.update_job(mine, instance_id=jq.INSTANCE_ID, heartbeat_at=old,
                        started_at=old)

    queue = jq.JobQueue.__new__(jq.JobQueue)       # без запуска потоков
    queue.db = database
    queue._wake = __import__("threading").Event()
    queue._reclaim_stale_jobs()

    returned = database.get_job(lost)
    assert returned["status"] == jq.STATUS_QUEUED, "брошенное задание не вернулось в очередь"
    assert returned["instance_id"] is None
    assert returned["progress"] == 0.0, "остался процент от прошлой попытки"
    kinds = [e["kind"] for e in database.get_events(lost)]
    assert "reclaimed" in kinds, "возврат прошёл молча — в журнале задания нет следа"

    assert database.get_job(mine)["status"] == jq.STATUS_RUNNING, \
        "подобрано собственное задание: свой поток мог просто грузить веса"


def test_live_instance_keeps_its_job(database):
    """Свежая отметка жизни защищает задание от подбора соседом."""
    from asrhub import job_queue as jq

    job_id = database.create_job({"filename": "идёт.wav", "status": jq.STATUS_RUNNING,
                                  "model": "demo-simulator"})
    database.update_job(job_id, instance_id="сосед:7", heartbeat_at=jq.now(),
                        started_at=jq.now() - 1000)

    queue = jq.JobQueue.__new__(jq.JobQueue)
    queue.db = database
    queue._wake = __import__("threading").Event()
    queue._reclaim_stale_jobs()
    assert database.get_job(job_id)["status"] == jq.STATUS_RUNNING


# ---------------------------------------------------------------------------
# Потоковое распознавание
# ---------------------------------------------------------------------------


class _FakeStream:
    """Движок, который держит состояние между кусками, — как Vosk."""

    def __init__(self) -> None:
        self.chunks = 0
        self.closed = False

    def accept(self, pcm: bytes):
        self.chunks += 1
        if self.chunks % 3 == 0:
            return "final", f"фраза {self.chunks // 3}"
        return "partial", f"гипотеза {self.chunks}"

    def finish(self) -> str:
        return "хвост"

    def close(self) -> None:
        self.closed = True


class _NativeEngine:
    def __init__(self) -> None:
        self.stream = _FakeStream()

    def stream_session(self, settings):
        return self.stream


class _NativeRegistry:
    def __init__(self, engine) -> None:
        self.engine = engine

    def get(self, settings):
        return self.engine


def test_native_stream_reports_its_mode_and_keeps_finals(tmp_path: Path):
    """Настоящий поток: движок сам уточняет гипотезу, звук не считается дважды."""
    from asrhub.streaming import SAMPLE_RATE, StreamSession, tone

    engine = _NativeEngine()
    session = StreamSession(_NativeRegistry(engine), {"model": "vosk-ru-0.42"},
                            workdir=tmp_path)
    ready = session.start()
    assert ready.to_dict()["mode"] == "native"
    assert ready.to_dict()["window_s"] is None, "окно в настоящем потоке не при чём"

    kinds: list[str] = []
    chunk = tone(0.2)
    for _ in range(4):
        kinds += [event.type for event in session.feed(chunk)]
    kinds += [event.type for event in session.finish()]

    assert kinds == ["partial", "partial", "final", "partial", "final", "done"], kinds
    assert "фраза 1" in session._final_text and "хвост" in session._final_text
    session.close()
    assert engine.stream.closed, "состояние движка не освобождено"
    assert len(chunk) == int(0.2 * SAMPLE_RATE) * 2


def test_window_mode_speaks_before_the_recording_ends(data_dir: Path, tmp_path: Path):
    """Движок потока не держит — текст всё равно обязан появляться по ходу."""
    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.streaming import StreamSession, tone

    settings = load()
    settings.set("model", "demo-simulator")
    settings.set("engine", "demo")
    merged = settings.merged({"stream_window_s": 1.0})
    session = StreamSession(EngineRegistry(), merged, workdir=tmp_path)

    ready = session.start()
    assert ready.to_dict()["mode"] == "window"
    assert ready.to_dict()["window_s"] == 1.0

    early: list[str] = []
    for _ in range(4):
        early += [e.text for e in session.feed(tone(0.5)) if e.type == "partial"]
    assert early, "за две секунды звука не пришло ни одной гипотезы"

    events = session.finish()
    assert [e.type for e in events] == ["final", "done"]
    assert events[0].text, "окончательный текст пуст"
    assert events[1].to_dict()["duration_s"] == pytest.approx(2.0, abs=0.05)
    session.close()


def test_stream_refuses_a_second_life_after_finish(data_dir: Path, tmp_path: Path):
    """Кусок после завершения не должен молча попадать в буфер."""
    from asrhub.config import load
    from asrhub.engines import EngineRegistry
    from asrhub.streaming import StreamSession, tone

    settings = load()
    settings.set("model", "demo-simulator")
    session = StreamSession(EngineRegistry(), settings.merged({}), workdir=tmp_path)
    session.start()
    session.finish()
    events = session.feed(tone(0.2))
    assert [e.type for e in events] == ["error"]
    assert session.finish() == [], "повторное завершение обязано быть тихим"
    session.close()


def test_unknown_stream_format_names_the_allowed_ones(tmp_path: Path):
    """Отказ обязан говорить, что прислать вместо непонятного формата."""
    from asrhub.errors import ASRHubError
    from asrhub.streaming import StreamSession

    with pytest.raises(ASRHubError) as info:
        StreamSession(_NativeRegistry(_NativeEngine()), {}, source_format="mp3-стерео",
                      workdir=tmp_path)
    assert "pcm_s16le" in (info.value.hint or ""), "в подсказке нет допустимых форматов"


def test_stream_route_refuses_readonly_and_honours_the_switch(keys_client):
    """Поток — это работа, а не чтение; и выключатель обязан выключать."""
    from starlette.websockets import WebSocketDisconnect

    readonly = keys_client.post("/api/keys",
                                json={"name": "только-чтение", "role": "readonly"})
    key = readonly.json()["key"]
    with pytest.raises(WebSocketDisconnect) as refusal, \
            keys_client.websocket_connect(f"/api/stream?api_key={key}"):
        pass
    assert refusal.value.code == 4403

    state = keys_client.app.state.hub
    state.settings.set("stream_enabled", False)
    admin = keys_client.headers["X-API-Key"]
    with pytest.raises(WebSocketDisconnect) as switched_off, \
            keys_client.websocket_connect(f"/api/stream?api_key={admin}"):
        pass
    assert switched_off.value.code == 4404
    state.settings.set("stream_enabled", True)


def test_stream_route_answers_over_the_socket(keys_client):
    """Полный обмен по сокету: настройка, звук, завершение, окончательный текст."""
    from asrhub.streaming import tone

    admin = keys_client.headers["X-API-Key"]
    with keys_client.websocket_connect(f"/api/stream?api_key={admin}") as socket:
        socket.send_text(json.dumps({"type": "config", "format": "pcm_s16le",
                                     "stream_window_s": 1.0}))
        ready = json.loads(socket.receive_text())
        assert ready["type"] == "ready" and ready["mode"] in ("native", "window")

        for _ in range(3):
            socket.send_bytes(tone(0.5))
        socket.send_text(json.dumps({"type": "finish"}))

        seen: list[dict] = []
        while True:
            message = json.loads(socket.receive_text())
            seen.append(message)
            if message["type"] == "done":
                break
    kinds = [m["type"] for m in seen]
    assert kinds[-2:] == ["final", "done"], kinds
    assert seen[-1]["duration_s"] == pytest.approx(1.5, abs=0.05)


def test_ticket_is_spent_once(keys_client):
    """Ключ в адресе оседает в журналах прокси — билет тратится и гаснет."""
    admin = keys_client.headers["X-API-Key"]
    ticket = keys_client.post("/api/auth/ticket").json()["ticket"]
    with keys_client.websocket_connect(f"/api/stream?ticket={ticket}") as socket:
        socket.send_text(json.dumps({"type": "config"}))
        assert json.loads(socket.receive_text())["type"] == "ready"

    from starlette.websockets import WebSocketDisconnect

    # Заголовок с ключом снимаем: иначе проверялся бы он, а не билет.
    with pytest.raises(WebSocketDisconnect) as reused, \
            keys_client.websocket_connect(f"/api/stream?ticket={ticket}",
                                          headers={"X-API-Key": ""}):
        pass
    assert reused.value.code == 4401
    assert admin


# ---------------------------------------------------------------------------
# Диктовка в веб-интерфейсе
# ---------------------------------------------------------------------------


def _web(repo_root: Path, name: str) -> str:
    return (repo_root / "server" / "asrhub" / "web" / name).read_text(encoding="utf-8")


def test_dictation_view_is_wired(repo_root: Path):
    """Маршрут потока без экрана — половина работы.

    Программные клиенты умели диктовку с первого дня, а пользователь
    веб-интерфейса — нет: раздела не было вовсе.
    """
    app = _web(repo_root, "app.js")
    html = _web(repo_root, "index.html")
    assert 'data-view="dictation"' in html, "нет пункта меню"
    assert "RENDERERS.dictation" in app, "нет отрисовщика раздела"
    assert "dictation:  {" in app or "dictation: {" in app, "раздел не описан в VIEWS"
    # Билет вместо ключа в адресе — то же правило, что и у ленты событий.
    assert "/api/auth/ticket" in app
    assert "`${proto}://${location.host}/api/stream" in app


def test_dictation_releases_the_microphone(repo_root: Path):
    """Уход из раздела обязан гасить запись.

    Иначе индикатор записи в браузере горит после ухода со страницы, а
    сессия на сервере держит модель.
    """
    app = _web(repo_root, "app.js")
    assert "leave() { this.stop(true); }" in app, "нет крючка на уход из раздела"
    assert "typeof leaving.leave === 'function'" in app, "крючок не вызывается"
    assert "session.stream.getTracks().forEach((track) => track.stop())" in app
    assert "JSON.stringify({ type: 'finish' })" in app, "сервер не узнаёт о конце записи"


def test_dictation_timer_belongs_to_the_recording(repo_root: Path):
    """Часы гасятся вместе с записью, а не вместе с разделом.

    viewTimer снимает таймеры только при смене раздела: после остановки
    обработчик продолжал стучать в обнулённую сессию — по ошибке в консоли
    на каждую секунду.
    """
    app = _web(repo_root, "app.js")
    assert "if (this.tick) { clearInterval(this.tick); this.tick = null; }" in app
    assert "if (!this.session) { clearInterval(this.tick); this.tick = null; return; }" in app


def test_dictation_explains_refusals_by_close_code(repo_root: Path):
    """Отказ приходит кодом закрытия и без разбора выглядит как обрыв сети."""
    app = _web(repo_root, "app.js")
    for code in ("4401", "4403", "4404"):
        assert f"{code}:" in app, f"код {code} не разобран"
    assert "https" in app and "localhost" in app, "нет объяснения про защищённый контекст"


def test_hotkey_numbers_stay_honest(repo_root: Path):
    """Разделов одиннадцать, цифр десять — об этом надо сказать, а не умолчать."""
    app = _web(repo_root, "app.js")
    assert "'transcribe', 'dictation', 'queue'" in app, "порядок номеров разошёлся с меню"
    assert "const numbers = HOTKEY_VIEWS.slice(0, 10)" in app, \
        "список номеров не показывается пользователю"


def test_dictation_documented_with_screenshot(repo_root: Path):
    """Раздел без главы и снимка экрана в документации не существует."""
    chapter = (repo_root / "docs" / "05-web-interface.md").read_text(encoding="utf-8")
    assert "## Диктовка" in chapter
    assert "images/22-dictation.png" in chapter
    assert (repo_root / "docs" / "images" / "22-dictation.png").exists()
    api = (repo_root / "docs" / "08-api.md").read_text(encoding="utf-8")
    assert "## Распознавание на лету" in api
    assert "stream_window_s" in api and "4403" in api
