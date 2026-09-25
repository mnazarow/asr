"""Заход 40: каталог параметров, мониторинг и самопроверка.

Сервер не поднимался из-за строки в config.yaml (тип «list», которого не
знали ни приведение, ни проверка); самопроверка объявляла неисправность на
каждой штатной установке (`engine: auto`); доля успеха по моделям считала
задания в очереди неудачами и поднимала критическую тревогу ночью.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from asrhub import catalog
from asrhub.catalog.params import coerce_value, validate_value

ИЗВЕСТНЫЕ_ТИПЫ = {"bool", "int", "float", "str", "text", "enum", "multi", "json"}


def test_у_каждого_параметра_известный_тип():
    """Тип «list» и «string» не знал никто: проходило любое значение.

    В интерфейсе поле рисовалось простым текстом, строка ложилась в
    config.yaml, и при запуске разбор приёмников падал с AttributeError.
    """
    чужие = [(p.key, p.type) for p in catalog.PARAMS if p.type not in ИЗВЕСТНЫЕ_ТИПЫ]
    assert not чужие, чужие


def test_варианты_бывают_только_у_перечислений():
    """digest_period был строкой с вариантами: «monthly» молча считался сутками."""
    лишние = [p.key for p in catalog.PARAMS if p.options and p.type not in ("enum", "multi")]
    assert not лишние, лишние


def test_умолчания_проходят_свою_проверку():
    assert catalog.validate_all(catalog.defaults()) == []


@pytest.mark.parametrize("ключ,значение", [
    ("max_concurrent_jobs", float("inf")), ("max_concurrent_jobs", 2.5),
    ("max_concurrent_jobs", True), ("beam_size", "1e999"),
    ("disk_min_free_gb", float("nan")), ("disk_min_free_gb", float("inf")),
    ("digest_period", "monthly"), ("monitoring_targets", "["),
    ("monitoring_targets", [{"kind": "influx", "url": "http://x"}]),
    ("monitoring_targets", ["http://x"]),
    ("monitoring_rules", [{"metric": "asrhub_нет_такой", "threshold": 1}]),
    ("monitoring_rules", [{"metric": "asrhub_queue_depth", "threshold": "много"}]),
    ("monitoring_rules", [{"metric": "asrhub_queue_depth", "threshold": 1,
                           "direction": "сверху"}]),
])
def test_негодное_значение_отвергается_а_не_роняет(ключ, значение):
    """Бесконечность в целом параметре роняла запуск OverflowError, NaN
    проходил любые границы, а 2.5 молча становилось двойкой."""
    годно, сообщение = validate_value(ключ, coerce_value(ключ, значение))
    assert not годно and сообщение


@pytest.mark.parametrize("ключ,значение", [
    ("max_concurrent_jobs", 2.0), ("max_concurrent_jobs", "3"), ("digest_period", "month"),
    ("monitoring_targets", [{"kind": "influxdb", "url": "http://influx:8086"}]),
    ("monitoring_rules", [{"metric": "asrhub_queue_depth", "threshold": 500,
                           "direction": "Above", "severity": "Critical"}]),
])
def test_годное_значение_принимается(ключ, значение):
    годно, сообщение = validate_value(ключ, coerce_value(ключ, значение))
    assert годно, сообщение


def test_строка_вместо_списка_приёмников_не_роняет_запуск(data_dir: Path, monkeypatch):
    """Ни одна строка конфигурации не имеет права не пустить сервер."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "config.yaml").write_text(
        'monitoring:\n  monitoring_targets: "["\n  monitoring_rules: "правило"\n',
        encoding="utf-8")
    monkeypatch.setenv("ASRHUB_CONFIG", str(data_dir / "config.yaml"))
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    настройки = load()
    assert any("Приёмники метрик" in п for п in настройки.problems), настройки.problems
    app = create_app(настройки, start_queue=False)
    with TestClient(app) as клиент:
        assert клиент.get("/api/health").status_code == 200


def test_разбор_приёмников_пропускает_мусор_поштучно():
    from asrhub.monitoring.service import MonitoringService

    class _Настройки(dict):
        def get(self, к, з=None):
            return dict.get(self, к, з)

    служба = MonitoringService.__new__(MonitoringService)
    from asrhub.monitoring.alerts import AlertEngine
    from asrhub.monitoring.pushers import PushManager

    служба.push = PushManager(lambda: [])
    служба.alerts = AlertEngine()
    # Смена правил пересчитывает тревоги по последнему снимку (заход 46):
    # службе без __init__ нужен и пустой снимок под своим замком.
    служба._собран = threading.Condition(threading.Lock())
    служба._cache = []
    служба.apply_settings(_Настройки({
        "monitoring_targets": ["строка", {"kind": "webhook", "url": "http://a/x"}, 5],
        "monitoring_rules": "не список"}))
    assert [t["name"] for t in служба.push.targets()] == ["webhook"]


# ---------------------------------------------------------------------------
# Правила оповещения
# ---------------------------------------------------------------------------


def test_направление_и_важность_без_учёта_регистра():
    """«Above» читалось как «below»: тревога при глубине 3 и тишина при 900."""
    from asrhub.monitoring.alerts import Rule

    правило = Rule.from_dict({"metric": "asrhub_queue_depth", "direction": "Above",
                              "threshold": 500, "severity": "Critical"})
    assert правило.direction == "above" and правило.severity == "critical"
    assert правило.breached(900) and not правило.breached(3)


@pytest.mark.parametrize("поле,значение", [
    ("direction", "сверху"), ("severity", "паника"), ("threshold", float("nan")),
])
def test_неизвестное_направление_или_важность_это_отказ(поле, значение):
    from asrhub.monitoring.alerts import Rule

    данные = {"metric": "asrhub_queue_depth", "threshold": 5, поле: значение}
    with pytest.raises(ValueError):
        Rule.from_dict(данные)


def test_включительный_порог_переживает_круг_через_api():
    """`inclusive` терялся на круге GET → PUT: критическое правило с порогом
    на краю шкалы после сохранения не срабатывало никогда."""
    from asrhub.monitoring.alerts import Rule

    исходное = Rule(metric="asrhub_confidence_drift", direction="above", threshold=2,
                    severity="critical", inclusive=True)
    вернувшееся = Rule.from_dict(исходное.to_dict())
    assert вернувшееся.inclusive is True
    assert вернувшееся.breached(2)


def test_правило_на_несуществующую_метрику_не_принимается(client):
    ответ = client.put("/api/monitoring/alerts/rules",
                       json=[{"metric": "asrhub_quue_depth", "threshold": 5}])
    assert ответ.status_code == 400, ответ.text
    assert "asrhub_quue_depth" in ответ.text


# ---------------------------------------------------------------------------
# Доля успеха по моделям
# ---------------------------------------------------------------------------


def _задания(db, модель: str, статусы: list[str]) -> None:
    for номер, статус in enumerate(статусы):
        ид = f"{модель}-{номер}"
        db.create_job({"id": ид, "filename": f"{ид}.wav", "status": статус,
                       "model": модель, "engine": "demo"})


def test_доля_успеха_среди_завершённых(tmp_path: Path):
    """Задания в очереди, в работе и отменённые — не неудачи.

    Очередь на паузе или ночной пакет давали «0 % успеха» и через час
    критическую тревогу при нуле отказов.
    """
    from asrhub.analytics import Analytics
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    try:
        _задания(db, "очередь", ["queued"] * 3 + ["completed"] * 6 + ["cancelled"])
        _задания(db, "сбои", ["completed"] * 3 + ["failed"] * 3 + ["running"])
        строки = {с["model"]: с for с in Analytics(db).by_model("day")}
    finally:
        db.close()
    assert строки["очередь"]["success_rate"] == 1.0
    assert строки["сбои"]["success_rate"] == 0.5
    assert строки["очередь"]["finished"] == 6


def test_доля_успеха_по_двум_заданиям_в_метрику_не_идёт(tmp_path: Path):
    """Одно неудачное из двух — ещё не «50 % успеха» и не критическая тревога."""
    from asrhub.analytics import Analytics
    from asrhub.db import Database
    from asrhub.monitoring.collector import МИН_ЗАВЕРШЁННЫХ, Collector

    db = Database(tmp_path / "asrhub.db")
    try:
        _задания(db, "мало", ["completed", "failed"])
        _задания(db, "много", ["completed"] * МИН_ЗАВЕРШЁННЫХ)

        class _Состояние:
            analytics = Analytics(db)

        сборщик = Collector(_Состояние())
        строки = сборщик._by_model()
    finally:
        db.close()
    отдаётся = {с["model"] for с in строки
                if с.get("success_rate") is not None
                and int(с.get("finished") or 0) >= МИН_ЗАВЕРШЁННЫХ}
    assert отдаётся == {"много"}


# ---------------------------------------------------------------------------
# Самопроверка
# ---------------------------------------------------------------------------


def test_самопроверка_понимает_движок_auto(client):
    """Установщик пишет `engine: auto`, а самопроверка искала «auto» среди
    движков: на каждой штатной установке — «Неисправность: Выбранный
    движок» с советом сменить правильно заданный параметр."""
    from asrhub.selfcheck import _движки

    state = client.app.state.hub
    state.settings.set("engine", "auto")
    state.settings.set("model", "demo-simulator")
    итог = _движки(state, False)
    выбранный = next(п for п in итог["checks"] if п["id"] == "selected")
    assert выбранный["state"] == "ok", выбранный
    assert "demo-simulator" in выбранный["value"]


def test_самопроверка_называет_несуществующую_модель(client):
    from asrhub.selfcheck import _движки

    state = client.app.state.hub
    state.settings.values["engine"] = "auto"
    state.settings.values["model"] = "нет-такой-модели"
    итог = _движки(state, False)
    выбранный = next(п for п in итог["checks"] if п["id"] == "selected")
    assert выбранный["state"] == "fail" and "нет-такой-модели" in выбранный["value"]
