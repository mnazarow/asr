"""Заход 35: молчаливые сбои.

Здесь собраны находки, у которых нет ни ошибки, ни записи в журнале, ни
следа в интерфейсе — только тишина там, где должен быть сигнал. Тревога,
которая не срабатывает никогда. Отмена, которую не услышали. Запись, за
которую заплатили видеокартой дважды. Разбор длинного разговора, который
ничего не разобрал и не пожаловался.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from asrhub.db import Database

# ---------------------------------------------------------------------------
# Тревоги
# ---------------------------------------------------------------------------

class Часы:
    """Управляемое время для движка тревог."""

    def __init__(self, начало: float = 1_700_000_000.0):
        self.t = начало

    def time(self) -> float:
        return self.t


@pytest.fixture()
def часы(monkeypatch: pytest.MonkeyPatch) -> Часы:
    from asrhub.monitoring import alerts

    ч = Часы()
    monkeypatch.setattr(alerts, "time", ч)
    return ч


def test_осечка_сбора_не_сбрасывает_выдержку_тревоги(часы: Часы):
    """Один неудачный опрос источника обнулял отсчёт, и тревога не срабатывала.

    У тревог с выдержкой в час — уверенность модели, дрейф, доля отказов —
    это означало, что достаточно ОДНОЙ осечки сбора в час, чтобы тревога не
    поднялась никогда. В пробе: уверенность двое суток держится на 0,3 при
    пороге 0,6, разбор качества спотыкается раз в десять минут — и тревога
    не срабатывает ни разу. Ни ошибки, ни записи в журнале: наблюдение
    просто молчит.
    """
    from asrhub.monitoring.alerts import AlertEngine, Rule
    from asrhub.monitoring.collector import Sample

    правило = Rule(metric="asrhub_confidence", direction="below", threshold=0.6,
                   for_seconds=3600, labels={"stat": "avg"}, severity="critical")
    движок = AlertEngine(rules=[правило])
    плохо = Sample("asrhub_confidence", 0.3, {"stat": "avg"})

    состояния = []
    for шаг in range(2 * 24 * 120):                 # двое суток по 30 секунд
        осечка = шаг % 20 == 19                     # раз в десять минут
        движок.evaluate([] if осечка else [плохо])
        состояния.append(движок.states()[0]["state"])
        часы.t += 30
    assert "firing" in состояния, "тревога не сработала ни разу за двое суток"


def test_надолго_пропавшая_метрика_всё_же_снимает_тревогу(часы: Часы):
    """Проверка парная: держать тревогу вечно по исчезнувшей метрике нельзя."""
    from asrhub.monitoring.alerts import AlertEngine, Rule
    from asrhub.monitoring.collector import Sample

    правило = Rule(metric="asrhub_disk_free_bytes", direction="below",
                   threshold=10, for_seconds=60)
    движок = AlertEngine(rules=[правило])
    мало = Sample("asrhub_disk_free_bytes", 1.0)

    for _ in range(5):
        движок.evaluate([мало])
        часы.t += 30
    assert движок.states()[0]["state"] == "firing"

    for _ in range(2):                              # короткая пропажа — держим
        движок.evaluate([])
        часы.t += 30
    assert движок.states()[0]["state"] == "firing", "тревога слетела от двух осечек"

    for _ in range(20):                             # одиннадцать минут тишины
        движок.evaluate([])
        часы.t += 30
    assert движок.states()[0]["state"] == "ok", "тревога висит по исчезнувшей метрике"


def test_счётчик_пропаж_обнуляется_когда_метрика_вернулась(часы: Часы):
    """Иначе редкие осечки накопились бы и сняли тревогу на ровном месте."""
    from asrhub.monitoring.alerts import AlertEngine, Rule
    from asrhub.monitoring.collector import Sample

    правило = Rule(metric="asrhub_disk_free_bytes", direction="below",
                   threshold=10, for_seconds=60)
    движок = AlertEngine(rules=[правило])
    мало = Sample("asrhub_disk_free_bytes", 1.0)
    for _ in range(5):
        движок.evaluate([мало])
        часы.t += 30
    for _ in range(30):                             # осечка через раз, полчаса
        движок.evaluate([])
        часы.t += 30
        движок.evaluate([мало])
        часы.t += 30
    assert движок.states()[0]["state"] == "firing"


def test_критическая_тревога_по_дрейфу_срабатывает():
    """Порог стоял на краю шкалы, а сравнение было строгим.

    Уровень дрейфа принимает три значения — 0, 1, 2, — критический порог у
    него 2, и «больше двух» не бывает. Критическая тревога была нарисована
    в справочнике, показана в разделе и не могла подняться ни при каких
    данных. Правило для Prometheus выкладывалось с тем же строгим знаком,
    так что и внешнее наблюдение молчало ровно так же.
    """
    from asrhub.monitoring.alerts import default_rules
    from asrhub.monitoring.exporters import prometheus_rules

    правила = {п.id: п for п in default_rules()}
    критическое = правила["asrhub_confidence_drift_level|critical"]
    assert критическое.breached(2.0) is True, "критический дрейф не срабатывает"
    assert критическое.breached(1.0) is False, "срабатывает на предупреждении"

    текст = prometheus_rules()
    assert "asrhub_confidence_drift_level >= 2.0" in текст, \
        "правило Prometheus разошлось со встроенным движком"


def test_обычные_пороги_остались_строгими():
    """Включительное сравнение — исключение для краёв шкалы, а не правило."""
    from asrhub.monitoring.alerts import default_rules

    правила = {п.id: п for п in default_rules()}
    очередь = правила.get("asrhub_queue_depth|warning")
    if очередь is not None:
        assert очередь.breached(очередь.threshold) is False
        assert очередь.breached(очередь.threshold + 1) is True


def test_приёмник_метрик_без_единой_попытки_не_считается_сломанным():
    """Свежезаведённый приёмник давал ноль — «последняя отправка не удалась».

    Тревога «приёмников доступно ниже единицы» поднималась через пятнадцать
    минут после того, как приёмник ЗАВЕЛИ: до первой отправки, ни на чём.
    Самопроверка в том же месте отвечала «ok, последняя отправка никогда» —
    две части сервера говорили об одном и том же противоположное.
    """
    from asrhub.monitoring.pushers import PushManager, Target

    приёмник = Target(name="influxdb", kind="influxdb",
                      url="http://127.0.0.1:8086/write?db=asrhub",
                      enabled=True, interval_s=60)
    менеджер = PushManager(lambda: ([], []), [приёмник])
    assert менеджер.healthy_samples() == [], \
        "приёмник, к которому не ходили, объявлен сломанным"

    # А после первой попытки метрика появляется — честная.
    состояние = next(iter(менеджер._states.values()))
    состояние.last_attempt = time.time()
    пробы = менеджер.healthy_samples()
    assert len(пробы) == 1 and пробы[0].value == 0.0
    состояние.last_success = состояние.last_attempt
    assert менеджер.healthy_samples()[0].value == 1.0


# ---------------------------------------------------------------------------
# Снимок метрик
# ---------------------------------------------------------------------------

def test_в_снимке_нет_повторяющихся_серий():
    """Одна и та же пара «имя + метки» дважды — для Prometheus ошибка.

    Гистограммы длительности выкладывал и разбор заданий по базе, и
    накопитель в памяти: двадцать семь задвоенных серий на каждый ответ
    `/api/monitoring/metrics`. При разборе побеждает произвольная из двух, а
    `promtool check metrics` называет это ошибкой.
    """
    from asrhub.monitoring.collector import Collector, Sample

    пробы = [
        Sample("asrhub_job_duration_seconds_bucket", 1.0, {"le": "5"}),
        Sample("asrhub_job_duration_seconds_bucket", 7.0, {"le": "5"}),
        Sample("asrhub_job_duration_seconds_bucket", 2.0, {"le": "15"}),
        Sample("asrhub_up", 1.0, {}),
        Sample("asrhub_up", 0.0, {}),
    ]
    итог = Collector._без_повторов(пробы)
    ключи = [(п.name, tuple(sorted(п.labels.items()))) for п in итог]
    assert len(ключи) == len(set(ключи)), ключи
    # Остаётся ПЕРВОЕ значение: разбор по базе идёт раньше накопителя в
    # памяти, и он же переживает перезапуск сервиса.
    assert [п.value for п in итог] == [1.0, 2.0, 1.0]


def test_выгрузка_метрик_не_содержит_повторов(data_dir: Path,
                                              monkeypatch: pytest.MonkeyPatch):
    """Та же проверка, но через настоящую выгрузку сервера.

    Ровно так дубли и появлялись: гистограмму длительности выкладывал разбор
    заданий по базе, а следом — накопитель в памяти, куда её кладёт очередь
    по каждому готовому заданию.
    """
    from asrhub.api import create_app
    from asrhub.config import load
    from asrhub.job_queue import JOB_DURATION_BUCKETS
    from asrhub.monitoring.collector import RUNTIME
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")

    приложение = create_app(load(), start_queue=False)
    with TestClient(приложение) as клиент:
        RUNTIME.observe("asrhub_job_duration_seconds", 1.5,
                        buckets=JOB_DURATION_BUCKETS)
        тело = клиент.get("/api/monitoring/metrics").text
    серии = [с.rsplit(" ", 1)[0] for с in тело.splitlines()
             if с.strip() and not с.startswith("#")]
    повторы = {с for с in серии if серии.count(с) > 1}
    assert not повторы, sorted(повторы)[:5]


def test_размер_каталога_моделей_берётся_из_настройки(tmp_path: Path):
    """Веса лежат там, куда указывает `models_dir`, а не в каталоге данных.

    На сервере, где модели вынесены на отдельный диск (а их выносят почти
    всегда: девяносто гигабайт), метрика показывала ноль — «место под
    модели» в наблюдении было нулём при полном диске. Самопроверка при этом
    считает по `models_dir` и видит настоящий размер.
    """
    import types

    from asrhub.monitoring.collector import Collector

    веса = tmp_path / "веса"
    веса.mkdir()
    (веса / "model.bin").write_bytes(b"\x00" * 5_000_000)
    данные = tmp_path / "данные"
    (данные / "models").mkdir(parents=True)          # пусто: сюда смотрели раньше

    class Пути:
        uploads = данные / "uploads"
        results = данные / "results"
        models = данные / "models"
        logs = данные / "logs"

    состояние = types.SimpleNamespace(
        settings=types.SimpleNamespace(paths=Пути(),
                                       get=lambda к, п=None: str(веса)
                                       if к == "models_dir" else п))
    сборщик = Collector(состояние)
    размеры = {п.labels["kind"]: п.value for п in сборщик._expensive()}
    assert размеры.get("models") == 5_000_000, размеры


# ---------------------------------------------------------------------------
# Языковая модель
# ---------------------------------------------------------------------------

class Настройки:
    ПО_УМОЛЧАНИЮ: dict[str, Any] = {
        "llm_backend": "stub", "llm_model": "stub", "llm_timeout_s": 5,
        "llm_max_concurrent": 1, "llm_context_chars": 12000,
        "llm_min_free_vram_gb": 0, "llm_auto": True, "llm_backfill": False,
        "llm_queue_paused": False, "llm_queue_batch": 5, "llm_queue_idle_s": 1,
        "llm_queue_cooldown_s": 1800, "llm_queue_attempts": 3,
        "llm_tasks": ["summary", "outcome"],
        "llm_reasons": ["вопрос по оплате", "другое"],
        "llm_outcomes": ["вопрос решён", "неясно"],
        "llm_trackers": [], "llm_scorecard": [],
        "content_script": [], "content_agent_speaker": "",
    }

    def __init__(self, **значения: Any):
        self.значения = {**self.ПО_УМОЛЧАНИЮ, **значения}

    def get(self, ключ: str, по_умолчанию: Any = None) -> Any:
        return self.значения.get(ключ, по_умолчанию)

    def set(self, ключ: str, значение: Any, **_: Any) -> None:
        self.значения[ключ] = значение


class Ответы:
    """Модель, отвечающая заранее заданным текстом на каждый вид задачи."""

    enabled = True
    model = "поддельная"

    def __init__(self, **ответы: str):
        self.ответы = ответы
        self.вызовы = 0

    def chat(self, system: str, user: str, *, kind: str = "chat", **_: Any) -> str:
        from asrhub.llm import LLMError

        self.вызовы += 1
        if kind not in self.ответы:
            raise LLMError(f"нет ответа для «{kind}»")
        return self.ответы[kind]


def _база(tmp_path: Path, записей: int = 2) -> Database:
    db = Database(tmp_path / "asrhub.db")
    for и in range(записей):
        job_id = db.create_job({"id": f"job{и}", "filename": f"запись-{и}.wav",
                                "owner": "анна", "model": "demo", "engine": "demo"})
        db.update_job(job_id, status="completed", finished_at=time.time(),
                      text="Здравствуйте, вопрос по заказу. Спасибо, до свидания.")
        db.save_segments(job_id, [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
             "text": "Здравствуйте, у меня вопрос по заказу."},
            {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
             "text": "Сейчас посмотрю и перезвоню вам завтра."}])
    return db


def test_ответ_модели_не_той_формы_не_роняет_разбор():
    """Поле, описанное в подсказке списком, приезжало числом.

    `or []` такое пропускает (число истинно), а перебор по нему падает с
    `TypeError: 'int' object is not iterable`. Дальше это исключение никто
    не ждал: разбор по кнопке отдавал человеку пятисотку без объяснений, а
    строка очереди оставалась «идёт» навсегда.
    """
    from asrhub.llm import tasks

    настройки = Настройки(llm_tasks=["trackers"], llm_trackers=[
        {"id": "t1", "label": "приветствие", "description": "поздоровался"}])
    клиент = Ответы(trackers=json.dumps({"trackers": 1}))
    итог = tasks.analyze(клиент, text="Здравствуйте.", segments=[], settings=настройки)
    assert итог["trackers"] == [
        {"id": "t1", "label": "приветствие", "fired": False, "quote": ""}]


def test_мусорный_ответ_закрывает_строку_очереди(tmp_path: Path):
    """Вечное «идёт» стоило записи навсегда: её больше никто не возьмёт."""
    from asrhub.llm.worker import LLMWorker

    db = _база(tmp_path, записей=1)
    настройки = Настройки(llm_tasks=["trackers"], llm_trackers=[
        {"id": "t1", "label": "приветствие", "description": "поздоровался"}])
    поток = LLMWorker(db, настройки, Ответы(trackers=json.dumps({"trackers": 1})))
    db.llmq_put("job0")
    db.llmq_take()
    поток.analyze_job("job0", force=True, из_очереди=True)
    строка = db.query_one("SELECT state FROM llm_queue WHERE job_id='job0'")
    assert строка["state"] != db.LLMQ_ИДЁТ, "строка очереди осталась «идёт»"
    db.close()


def test_длинная_запись_без_пересказов_не_выдаёт_пустой_разбор(tmp_path: Path):
    """Склейка «Часть 1: \\nЧасть 2: …» уходила в модель как расшифровка.

    Модель честно отвечала на пустоту — «ничего конкретного не
    обсуждалось», — и этот ответ ложился в базу обычным разбором, с пустым
    полем ошибки. Длинный разговор, которого разбор не увидел вовсе,
    выглядел разобранным и попадал в отчёты как «без содержания». Часовые
    разговоры режутся на части всегда, так что это про них.
    """
    from asrhub.llm import tasks

    настройки = Настройки(llm_context_chars=500, llm_tasks=["summary"])
    реплики = [{"speaker": "SPEAKER_00", "text": f"Реплика {и} про доставку заказа."}
               for и in range(60)]
    # Пересказ вернулся под другим именем поля — для нас он пуст.
    клиент = Ответы(chunk=json.dumps({"пересказ": "Клиент спрашивал про доставку."}),
                    main=json.dumps({"summary": "ничего конкретного",
                                     "resolved": None}))
    итог = tasks.analyze(клиент, text="", segments=реплики, settings=настройки)
    assert итог["summary"] is None, "пустота подана как разбор"
    assert any("не пересказала" in з for з in итог["warnings"]), итог["warnings"]
    assert клиент.вызовы == итог["chunks"], "основной вызов всё-таки состоялся"


def test_часть_без_пересказа_отмечается_но_разбор_идёт():
    """Одна потерянная часть из шести — повод для замечания, а не для отказа."""
    from asrhub.llm import tasks

    настройки = Настройки(llm_context_chars=500, llm_tasks=["summary"])
    реплики = [{"speaker": "SPEAKER_00", "text": f"Реплика {и} про доставку заказа."}
               for и in range(60)]

    class ЧерезРаз(Ответы):
        def __init__(self):
            super().__init__(main=json.dumps({"summary": "Клиент спрашивал про доставку.",
                                              "resolved": True}))
            self.частей = 0

        def chat(self, system, user, *, kind="chat", **_):
            if kind == "chunk":
                self.частей += 1
                self.вызовы += 1
                пусто = self.частей == 1
                return json.dumps({"summary": "" if пусто else f"Часть {self.частей}."})
            return super().chat(system, user, kind=kind)

    клиент = ЧерезРаз()
    итог = tasks.analyze(клиент, text="", segments=реплики, settings=настройки)
    assert итог["summary"] == "Клиент спрашивал про доставку."
    assert any("без пересказа" in з for з in итог["warnings"]), итог["warnings"]


def test_разбор_по_кнопке_не_дублирует_идущий(tmp_path: Path):
    """Кнопка по записи, которую жуёт фоновый поток, шла к модели второй раз.

    Две оплаты времени видеокарты за один ответ и два ответа поверх друг
    друга в базе. Предел `llm_max_concurrent` тут не спасает: он про
    одновременность, а не про повтор.
    """
    from asrhub.llm import LLMError
    from asrhub.llm.worker import LLMWorker

    db = _база(tmp_path, записей=1)
    клиент = Ответы(main=json.dumps({"summary": "Разбор.", "resolved": True}))
    поток = LLMWorker(db, Настройки(), клиент)

    db.llmq_put("job0")
    db.llmq_take()                                   # запись уже разбирают
    with pytest.raises(LLMError) as сбой:
        поток.analyze_job("job0", force=True)
    assert "уже разбирается" in str(сбой.value)
    assert клиент.вызовы == 0, "к модели всё-таки сходили второй раз"
    db.close()


def test_свободную_запись_кнопка_разбирает_как_прежде(tmp_path: Path):
    """Проверка парная: отказ должен касаться только идущего разбора."""
    from asrhub.llm.worker import LLMWorker

    db = _база(tmp_path, записей=1)
    клиент = Ответы(main=json.dumps({"summary": "Разбор.", "resolved": True}))
    поток = LLMWorker(db, Настройки(), клиент)
    итог = поток.analyze_job("job0", force=True)
    assert итог["summary"] == "Разбор."
    строка = db.query_one("SELECT state FROM llm_queue WHERE job_id='job0'")
    assert строка["state"] == db.LLMQ_ГОТОВО
    db.close()


def test_отмена_на_прогреве_не_переписывает_настройки(tmp_path: Path):
    """Прогрев — самый долгий шаг после скачивания, на нём и отменяют.

    Отмена доходила до сервера, ход останавливался — а следующий шаг всё
    равно выполнялся: переписывал `llm_backend` и `llm_model` и сохранял
    config.yaml. Человек отменял установку и получал сервер, переключённый
    на модель, которую он ставить передумал.
    """
    import inspect

    from asrhub.llm import provision

    текст = inspect.getsource(provision.Установщик._работа)
    после_прогрева = текст.split("self._прогрев(")[1]
    до_настройки = после_прогрева.split("self._настройка(")[0]
    assert "_отменено()" in до_настройки, \
        "между прогревом и записью настроек нет проверки отмены"


def test_шлюз_в_сеть_соблюдает_порог_видеопамяти(tmp_path: Path, monkeypatch):
    """Порог держит место под веса РАСПОЗНАВАНИЯ, а шлюз ходил мимо него.

    Любой сторонний клиент мог занять память, которую сервер для себя
    берёг, — а распознавание после этого падало с нехваткой памяти.
    """
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")

    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    настройки = load()
    настройки.set("llm_network_enabled", True, source="test")
    # Не заглушка: у неё проверка памяти не нужна и пропускается. Адрес
    # заведомо закрытый — если бы шлюз пошёл к модели, ответом была бы 502
    # «сервер модели не ответил», и по коду видно, дошло до сети или нет.
    настройки.set("llm_backend", "ollama", source="test")
    настройки.set("llm_url", "http://127.0.0.1:1", source="test")
    настройки.set("llm_model", "qwen3:14b", source="test")
    настройки.set("llm_timeout_s", 5, source="test")
    настройки.set("llm_min_free_vram_gb", 200.0, source="test")

    приложение = create_app(настройки, start_queue=False)
    with TestClient(приложение) as клиент:
        клиент.app.state.hub.llm._hardware = lambda: 0.5
        ответ = клиент.post("/api/llm/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "привет"}]})
        assert ответ.status_code == 503, ответ.text
        assert "видеопамят" in ответ.text.lower(), ответ.text
        # И потоком — тем же отказом.
        поток = клиент.post("/api/llm/v1/chat/completions", json={
            "messages": [{"role": "user", "content": "привет"}], "stream": True})
        assert поток.status_code == 503, поток.text
