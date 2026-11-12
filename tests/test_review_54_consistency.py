"""Заход 54: сверка частей проекта между собой — поведение, а не текст.

* Справочник API (`docs/generate_api_full.py` → `docs/api-reference.md`)
  называл «любым ключом» и «ключом с правом записи» три десятка маршрутов,
  которые сервер давно закрыл ролью администратора: копии, журнал
  доступа, станции и агенты АТС, пауза очереди и другие. Проверка задаёт
  вопрос самому серверу — ключом с ролью user — и сверяет ответ со
  справочником в обе стороны.
* Опечатка в имени переменной окружения (`ASRHUB_SERVER_PROT`) молча не
  действовала; теперь похожее на параметр имя попадает в `problems`, а
  служебные переменные сценариев — нет.
* Пик памяти замеряется и на занятом сервере, а общий счётчик обнуляется
  только когда задание идёт одно, — раньше это сторожила проверка, искавшая
  в исходнике очереди подстроку `measure_memory=True`.
* Первая фраза главы «Мониторинг» складывается в объявленное «всего».
"""
from __future__ import annotations

import importlib
import pkgutil
import re
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _генератор_справочника():
    sys.path.insert(0, str(ROOT / "docs"))
    try:
        return importlib.import_module("generate_api_full")
    finally:
        sys.path.remove(str(ROOT / "docs"))


def _маршруты() -> list[tuple[str, str]]:
    """Все HTTP-маршруты приложения: путь и метод — из самих роутеров."""
    import asrhub.api as api

    итог: set[tuple[str, str]] = set()
    for модуль in pkgutil.iter_modules(api.__path__):
        if not модуль.name.startswith("routes_"):
            continue
        m = importlib.import_module(f"asrhub.api.{модуль.name}")
        for имя in dir(m):
            роутер = getattr(m, имя)
            if type(роутер).__name__ != "APIRouter":
                continue
            for маршрут in роутер.routes:
                for метод in getattr(маршрут, "methods", None) or ():
                    итог.add((маршрут.path, метод.lower()))
    return sorted(итог)


def test_справочник_api_называет_администратора_там_где_его_требует_сервер(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    справочник = _генератор_справочника()
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_RATE_LIMIT_PER_MINUTE", "0")
    настройки = load()
    настройки.api_keys["ah_user"] = {"enabled": True, "name": "u", "role": "user"}
    app = create_app(настройки, start_queue=False)
    маршруты = _маршруты()
    assert len(маршруты) > 150
    молчит: list[str] = []
    напрасно: list[str] = []
    with TestClient(app, raise_server_exceptions=False) as c:
        for путь, метод in маршруты:
            адрес = re.sub(r"\{[^}]+\}", "x1", путь)
            ответ = c.request(метод.upper(), адрес, headers={"X-API-Key": "ah_user"}, json={})
            требует = ответ.status_code == 403 and "администратора" in ответ.text
            обещает = справочник.access_note(путь, метод) == "ключ с ролью **admin**"
            if требует and not обещает:
                молчит.append(f"{метод.upper()} {путь}: {справочник.access_note(путь, метод)}")
            # Обещание «только администратор» там, где обычный ключ проходит.
            if обещает and ответ.status_code < 400:
                напрасно.append(f"{метод.upper()} {путь}: {ответ.status_code}")
    assert not молчит, "сервер требует администратора, справочник — нет:\n" + "\n".join(молчит)
    assert not напрасно, "справочник требует администратора, сервер — нет:\n" + "\n".join(напрасно)


# ---------------------------------------------------------------------------
# Переменные окружения
# ---------------------------------------------------------------------------


def test_опечатка_в_имени_переменной_не_проходит_молча(tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_SERVER_PROT", "9000")
    monkeypatch.setenv("ASRHUB_MAX_CONCURENT_JOBS", "7")
    настройки = load()
    assert настройки.get("server_port") != 9000
    проблемы = "\n".join(настройки.problems)
    assert "ASRHUB_SERVER_PROT" in проблемы and "ASRHUB_SERVER_PORT" in проблемы
    assert "ASRHUB_MAX_CONCURENT_JOBS" in проблемы and "ASRHUB_MAX_CONCURRENT_JOBS" in проблемы


def test_служебные_переменные_сценариев_не_считаются_опечатками(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Каждое имя ASRHUB_*, которое встречается в сценариях, Compose, агенте
    и примерах, сервер принимает молча: либо это параметр, либо служебное."""
    from asrhub import catalog
    from asrhub.config import load

    имена: set[str] = set()
    for каталог in ("scripts", "docker", "agent", "examples", "benchmarks"):
        for файл in (ROOT / каталог).rglob("*"):
            if файл.is_file() and файл.suffix not in (".png", ".jpg", ".ico"):
                try:
                    текст = файл.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                имена.update(re.findall(r"\bASRHUB_[A-Z0-9_]+\b", текст))
    assert "ASRHUB_ENGINES" in имена and "ASRHUB_ACCEL" in имена
    for имя in имена:
        if имя[len("ASRHUB_"):].lower() not in catalog.PARAMS_BY_KEY:
            monkeypatch.setenv(имя, "1")
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ASRHUB_CONFIG", raising=False)
    настройки = load()
    лишние = [п for п in настройки.problems if "такого параметра нет" in п]
    assert not лишние, лишние


# ---------------------------------------------------------------------------
# Замер памяти на занятом сервере
# ---------------------------------------------------------------------------


def test_пик_памяти_замеряется_и_на_занятом_сервере(data_dir: Path, sample_wav: Path,
                                                   monkeypatch: pytest.MonkeyPatch):
    """Два задания разом: замер — у обоих, обнуление — не у того, кто пришёл вторым.

    Обнулить общий счётчик, пока рядом идёт второе задание, значит стереть
    то, что тот накопил; а не мерить вовсе — оставить раздел «Ресурсы»
    пустым ровно там, где он нужен. Одно задание — и меряет, и обнуляет.
    """
    from asrhub import job_queue
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "2")
    настоящий = job_queue.process_job
    вызовы: list[dict[str, Any]] = []
    замок = threading.Lock()
    оба_идут = threading.Event()
    ждать_соседа = {"да": True}

    def обёртка(*args: Any, **kwargs: Any):
        with замок:
            вызовы.append({"measure": kwargs.get("measure_memory"),
                           "reset": kwargs.get("reset_memory")})
            if len(вызовы) >= 2:
                оба_идут.set()
        if ждать_соседа["да"]:
            # Первое задание держится, пока не начнётся второе: так они
            # заведомо идут разом, без расчёта на скорость машины.
            оба_идут.wait(60)
        return настоящий(*args, **kwargs)

    monkeypatch.setattr("asrhub.job_queue_worker.process_job", обёртка)
    app = create_app(load(), start_queue=True)
    with TestClient(app) as c:
        def отправить(запись: Path = sample_wav) -> str:
            with запись.open("rb") as файл:
                ответ = c.post("/api/jobs", files={"file": (запись.name, файл, "audio/wav")})
            assert ответ.status_code in (200, 201, 202), ответ.text
            return ответ.json()["id"]

        def дождаться(ид: str) -> None:
            for _ in range(600):
                if c.get(f"/api/jobs/{ид}").json()["status"] not in (
                        "queued", "running", "retry"):
                    return
                оба_идут.wait(0.1)
            raise AssertionError(f"задание {ид} не закончилось")

        # Вторая запись отличается байтом: одинаковая с теми же настройками —
        # «близнец» и ждёт первую, а не распознаётся рядом с ней (3.1.32).
        соседняя = data_dir.parent / "соседняя.wav"
        байты_соседа = bytearray(sample_wav.read_bytes())
        байты_соседа[-2] ^= 1
        соседняя.write_bytes(bytes(байты_соседа))
        первое, второе = отправить(), отправить(соседняя)
        дождаться(первое)
        дождаться(второе)
        assert оба_идут.is_set()
        assert [в["measure"] for в in вызовы] == [True, True]
        # Первое могло начаться в одиночку и обнулить; второе начиналось,
        # когда первое уже шло, — ему обнулять нельзя.
        assert вызовы[1]["reset"] is False, вызовы
        # Одно задание — и меряет, и обнуляет.
        ждать_соседа["да"] = False
        вызовы.clear()
        # Другие байты — иначе результат возьмётся из кеша, не распознаваясь.
        другая = data_dir.parent / "другая.wav"
        байты = bytearray(sample_wav.read_bytes())
        байты[-1] ^= 1
        другая.write_bytes(bytes(байты))
        третье = отправить(другая)
        дождаться(третье)
        assert вызовы == [{"measure": True, "reset": True}]


# ---------------------------------------------------------------------------
# Числа глав
# ---------------------------------------------------------------------------


def test_метрики_главы_мониторинга_складываются_в_объявленное_всего():
    sys.path.insert(0, str(ROOT / "docs"))
    try:
        генератор = importlib.import_module("generate")
    finally:
        sys.path.remove(str(ROOT / "docs"))
    from asrhub.monitoring import catalog as каталог

    фраза = next(с for с in генератор.generate_monitoring().splitlines()
                 if с.startswith("Сервис отдаёт наружу"))
    всего = int(re.search(r"\*\*(\d+) метрик", фраза).group(1))
    части = [int(re.search(rf"(\d+) {слово}", фраза).group(1))
             for слово in ("мгновенн", "счётчик", "гистограмм", "метрик[аи]?-сведени")]
    assert всего == len(каталог.METRICS) == sum(части), фраза
    # Глава собрана тем же генератором: первая фраза в ней — эта же.
    assert фраза in (ROOT / "docs" / "16-monitoring.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Удаление одного и удаление пакетом
# ---------------------------------------------------------------------------


def test_удаление_одного_и_пакетом_убирают_одно_и_то_же(client):
    """Раньше это сторожила проверка текста `routes_jobs.py` («одна функция
    `_delete_one`»). Здесь — что остаётся после каждого из двух путей:
    каталог результата, отложенный прежний результат, исходник, копия с
    заглушёнными данными, строка задания, разбор и ответ модели."""
    from asrhub.db import путь_отредактированной
    from asrhub.llm import tasks

    состояние = client.app.state.hub
    db = состояние.db
    пути = состояние.settings.paths
    следы: dict[str, list[Path]] = {}
    for ид in ("del-one", "del-bulk"):
        исходник = Path(пути.uploads) / f"{ид}.wav"
        исходник.write_bytes(b"RIFF")
        копия = путь_отредактированной(исходник)
        копия.write_bytes(b"RIFF")
        результат = Path(пути.results) / ид
        результат.mkdir(parents=True)
        (результат / "x.txt").write_text("текст", encoding="utf-8")
        прежний = результат.with_name(ид + ".prev")
        прежний.mkdir()
        db.create_job({"id": ид, "filename": f"{ид}.wav", "file_path": str(исходник)})
        db.update_job(ид, status="completed", result_path=str(результат), text="текст")
        db.execute("INSERT INTO content (job_id, version, computed_at, sentiment) "
                   "VALUES (?,1,0,0.1)", (ид,))
        db.llm_save(ид, tasks.VERSION, model="t", reason="доставка", summary="")
        следы[ид] = [исходник, копия, результат, прежний]
    assert client.delete("/api/jobs/del-one").status_code == 200
    ответ = client.post("/api/jobs/bulk", json={"action": "delete", "ids": ["del-bulk"]})
    assert ответ.status_code == 200, ответ.text
    for ид, файлы in следы.items():
        assert [п for п in файлы if п.exists()] == [], ид
        for таблица, колонка in (("jobs", "id"), ("content", "job_id"), ("llm_results", "job_id")):
            assert not db.query(f"SELECT 1 FROM {таблица} WHERE {колонка}=?", (ид,)), (ид, таблица)


# ---------------------------------------------------------------------------
# Маршруты, у которых не было ни одной проверки по HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def тихий(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер без очереди: задание остаётся в том состоянии, куда его поставили."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as c:
        yield c, app.state.hub


def test_приоритет_пауза_и_конец_очереди_по_http(тихий):
    c, состояние = тихий
    db = состояние.db
    db.create_job({"id": "q1", "filename": "q1.wav", "priority": 50})
    ответ = c.post("/api/jobs/q1/priority", json={"priority": 80})
    assert ответ.status_code == 200 and ответ.json()["priority"] == 80
    assert c.post("/api/jobs/q1/priority", json={"priority": 101}).status_code == 422
    assert c.post("/api/jobs/q1/bottom").json()["priority"] == 0
    assert c.post("/api/jobs/q1/top").json()["priority"] == 100
    assert c.post("/api/jobs/q1/pause").json()["status"] == "paused"
    assert c.post("/api/jobs/q1/resume").json()["status"] == "queued"
    assert c.post("/api/jobs/нет/priority", json={"priority": 1}).status_code == 404


def test_пресет_число_потоков_каталог_трендов_и_состояния_phone_asr(тихий):
    c, состояние = тихий
    from asrhub import catalog

    пресет = catalog.PRESETS[0]
    ответ = c.post(f"/api/presets/{пресет.id}/apply")
    assert ответ.status_code == 200, ответ.text
    применено = ответ.json()["applied"]
    assert применено and all(состояние.settings.get(к) == з for к, з in применено.items())
    assert c.post("/api/presets/нет-такого/apply").status_code == 404
    потоки = c.post("/api/queue/concurrency", json={"workers": 3})
    assert потоки.status_code == 200, потоки.text
    assert c.post("/api/queue/concurrency", json={"workers": 0}).status_code == 422
    тренды = c.get("/api/trends/catalog").json()
    assert тренды and isinstance(тренды, dict)
    # phone_asr: запись без речи — успех с пустым диалогом, а не отказ.
    db = состояние.db
    for ид, статус in (("ph1", "no_speech"), ("ph2", "failed"), ("ph3", "completed")):
        db.create_job({"id": ид, "filename": f"{ид}.wav",
                       "params": {"_phone": {"call_id": f"call-{ид}", "part": 1}}})
        db.update_job(ид, status=статус)
    состояния = {с["call_id"]: с["status"] for с in c.get("/statuses").json()}
    assert состояния == {"call-ph1": "success", "call-ph2": "failed", "call-ph3": "success"}
