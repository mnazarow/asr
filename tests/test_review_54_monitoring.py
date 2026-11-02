"""Заход 54, участок content-analytics: мониторинг (отчёт 12-monitoring).

Каждая проверка падает на коде до захода и проходит после.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from asrhub.monitoring import exporters, pushers
from asrhub.monitoring.alerts import AlertEngine, Rule, default_rules
from asrhub.monitoring.collector import RUNTIME, Sample
from asrhub.monitoring.pushers import PushManager, Target, TargetState

# ---------------------------------------------------------------------------
# Метки запросов и ошибки API
# ---------------------------------------------------------------------------


def test_метод_запроса_в_метках_из_закрытого_перечня(client):
    """Аноним раздувал выдачу метрик: каждый метод (PROPFIND, PURGE…) заводил
    свой ряд счётчика и гистограммы."""
    client.request("PROPFIND", "/api/jobs")
    client.request("MKCOL", "/api/jobs")
    текст = client.get("/api/monitoring/metrics").text
    assert 'method="PROPFIND"' not in текст and 'method="MKCOL"' not in текст
    assert 'method="other"' in текст


def test_отказ_api_не_ошибка_задания():
    """Запрос без ключа сдвигал «время последней ошибки» и шёл в счётчик
    ошибок заданий."""
    ключ = ("asrhub_errors_total", (("code", "auth_error"),))
    было, время = RUNTIME.counters.get(ключ, 0), RUNTIME.last_error_ts
    RUNTIME.note_error("auth_error", False, api=True)
    assert RUNTIME.counters.get(ключ, 0) == было
    assert RUNTIME.last_error_ts == время
    assert RUNTIME.counters[("asrhub_api_errors_total", (("code", "auth_error"),))] >= 1


# ---------------------------------------------------------------------------
# Правила и состояние тревог
# ---------------------------------------------------------------------------


def test_своё_правило_не_стирает_правила_каталога(client):
    правило = [{"metric": "asrhub_queue_depth", "direction": "above", "threshold": 500,
                "severity": "warning", "for_seconds": 1800}]
    assert client.put("/api/monitoring/alerts/rules", json=правило).status_code == 200
    правила = client.get("/api/monitoring/alerts/rules").json()["rules"]
    assert len(правила) > 20
    assert any(п["metric"] == "asrhub_disk_free_bytes" for п in правила)


def test_выключенное_правило_снимает_горящую_тревогу():
    правило = Rule(metric="asrhub_queue_depth", direction="above", threshold=10,
                   severity="warning", for_seconds=0)
    движок = AlertEngine(rules=[правило])
    for _ in range(2):                                       # наблюдение → тревога
        движок.evaluate([Sample("asrhub_queue_depth", 500.0, {})])
    assert движок.summary()["firing"] == 1
    выключенное = Rule.from_dict({**правило.to_dict(), "enabled": "false"})
    assert выключенное.enabled is False, "строка «false» включала правило"
    движок.set_rules([выключенное])
    assert движок.summary()["firing"] == 0
    assert any(с.get("state") == "ok" for с in движок.history())


def test_тревога_переживает_перезапуск(tmp_path: Path):
    правило = Rule(metric="asrhub_queue_depth", direction="above", threshold=10,
                   severity="warning", for_seconds=0)
    первый = AlertEngine(rules=[правило], store=tmp_path / "alerts.json")
    for _ in range(2):                                       # наблюдение → тревога
        первый.evaluate([Sample("asrhub_queue_depth", 500.0, {})])
    since = next(с for с in первый.states() if с["state"] == "firing")["since"]
    второй = AlertEngine(rules=[правило], store=tmp_path / "alerts.json")
    второй.evaluate([Sample("asrhub_queue_depth", 500.0, {})])
    состояние = next(с for с in второй.states() if с["metric"] == "asrhub_queue_depth")
    assert состояние["state"] == "firing" and состояние["since"] == since


def test_порог_доли_и_прироста_не_сравнивается_с_сырым_рядом():
    """Пример опроса сравнивал счётчик заданий с долей 0,2 и объявлял аварию
    на здоровом сервере: вид порога был только в тексте note."""
    from asrhub.monitoring.catalog import METRICS_BY_NAME

    assert METRICS_BY_NAME["asrhub_jobs_total"].threshold.kind == "ratio"
    assert METRICS_BY_NAME["asrhub_rtf"].threshold.stat == "p95"
    правила = [п for п in default_rules() if п.metric == "asrhub_stream_first_text_seconds"]
    assert правила and all(п.labels == {"stat": "p95"} for п in правила)
    for имя, spec in METRICS_BY_NAME.items():
        if spec.threshold and spec.threshold.kind in ("ratio", "percent", "increase"):
            assert имя in exporters._PROMETHEUS_COMPUTED, имя


def test_тревоги_отдаются_без_нового_сбора(client):
    """Интерфейс спрашивает тревоги каждые четыре секунды с любой страницы,
    и каждый вопрос запускал полный сбор метрик."""
    служба = client.app.state.hub.monitoring
    служба.cache_ttl_s = 0
    client.get("/api/monitoring/alerts")
    сборов = {"n": 0}
    настоящий = служба.collector.collect

    def считать() -> Any:
        сборов["n"] += 1
        return настоящий()

    служба.collector.collect = считать
    for _ in range(3):
        assert client.get("/api/monitoring/alerts").status_code == 200
    assert сборов["n"] == 0


# ---------------------------------------------------------------------------
# Приёмники
# ---------------------------------------------------------------------------


def test_перевод_строки_в_заголовке_отвергается_а_края_чистятся():
    with pytest.raises(ValueError):
        Target.from_dict({"kind": "webhook", "url": "http://x/",
                          "headers": {"Authorization": "Bearer a\nb"}})
    приёмник = Target.from_dict({"kind": "webhook", "url": "http://x/",
                                 "headers": {"Authorization": "Bearer s3cr3t\n"}})
    assert приёмник.headers["Authorization"] == "Bearer s3cr3t"


def test_заголовки_не_уходят_на_чужой_адрес():
    сохранённый = Target.from_dict({"name": "influx", "kind": "influxdb",
                                    "url": "http://influx:8086/write",
                                    "headers": {"Authorization": "Token REAL"}})
    чужой = Target.from_dict({"name": "influx", "kind": "webhook", "url": "http://свой/",
                              "headers": {"Authorization": "***"}}, прежний=сохранённый)
    assert "Authorization" not in чужой.headers
    тот_же = Target.from_dict({"name": "influx", "kind": "influxdb",
                               "url": "http://influx:8086/write",
                               "headers": {"Authorization": "***"}}, прежний=сохранённый)
    assert тот_же.headers["Authorization"] == "Token REAL"


def test_statsd_отрицательное_значение_и_пропавший_счётчик():
    приёмник = Target(name="s", kind="statsd", url="udp://127.0.0.1:8125")
    состояние = TargetState(target=приёмник)
    строки, новые = pushers._statsd_lines(
        приёмник, [Sample("asrhub_content_sentiment_avg", -0.31, {})], {})
    assert any(с.endswith(":0|g") for с in строки), строки
    состояние.statsd_last = {"asrhub.llm_calls_total": 500.0}
    отправлено: list[bytes] = []

    class Сокет:
        def __init__(self, *a: Any) -> None: ...
        def sendto(self, данные: bytes, адрес: Any) -> None:
            отправлено.append(данные)
        def close(self) -> None: ...
        def settimeout(self, *a: Any) -> None: ...
        def __enter__(self) -> Сокет:
            return self
        def __exit__(self, *a: Any) -> None: ...

    import socket
    настоящий = socket.socket
    socket.socket = Сокет                                    # type: ignore[misc,assignment]
    try:
        pushers.send(приёмник, [Sample("asrhub_content_records", 3.0, {})], состояние)
    finally:
        socket.socket = настоящий                            # type: ignore[misc]
    assert состояние.statsd_last.get("asrhub.llm_calls_total") == 500.0, \
        "счётчик, пропавший из снимка, забыт — и вернётся приростом «с нуля»"
    assert pushers._statsd_address("udp://[::1]:8125") == ("::1", 8125)
    assert pushers._statsd_address("statsd://узел:9125") == ("узел", 9125)
    del строки, новые


def test_удалённый_приёмник_не_возвращается_в_работу():
    приёмник = Target(name="старый", kind="webhook", url="http://127.0.0.1:9/old")
    менеджер = PushManager(lambda: [], [приёмник])
    менеджер.set_targets([])
    итог = менеджер.push_once(приёмник, [])
    assert not итог["ok"]
    assert менеджер.target_list() == []


def test_срок_отправки_от_прежнего_срока(monkeypatch):
    """Такт раз в пять секунд: период выходил interval_s + 5."""
    часы = {"t": 1000.0}
    monkeypatch.setattr(pushers.time, "time", lambda: часы["t"])
    приёмник = Target(name="w", kind="webhook", url="http://127.0.0.1:9/", interval_s=10)
    менеджер = PushManager(lambda: [], [приёмник])
    менеджер.push_once = lambda *a, **k: {"ok": True}          # type: ignore[method-assign]
    менеджер.tick()
    часы["t"] = 1012.0
    менеджер.tick()
    assert менеджер._next_at["w"] == 1020.0


def test_приёмник_здоров_пока_идёт_отправка():
    приёмник = Target(name="w", kind="webhook", url="http://127.0.0.1:9/")
    менеджер = PushManager(lambda: [], [приёмник])
    состояние = менеджер._states["w"]
    увидено: list[float] = []

    def медленно(*a: Any, **k: Any) -> str:
        увидено.extend(с.value for с in менеджер.healthy_samples())
        return ""

    состояние.last_attempt = состояние.last_success = 1.0
    pushers.send, настоящий = медленно, pushers.send        # type: ignore[assignment]
    try:
        менеджер.push_once(приёмник, [])
    finally:
        pushers.send = настоящий
    assert увидено == [1.0], "во время отправки приёмник объявлен нездоровым"


def test_адрес_приёмника_в_настройках_без_пути_для_неадминистратора():
    from asrhub.config import _приёмники_наружу

    адрес = "https://hooks.slack.test/services/T0001/B0002/XXXXSECRETPATH"
    наружу = _приёмники_наружу([{"kind": "webhook", "url": адрес}], False)
    assert "SECRETPATH" not in наружу[0]["url"]
    assert "hooks.slack.test" in наружу[0]["url"]


def test_два_приёмника_с_одним_именем_отвергаются_проверкой():
    from asrhub.catalog import params

    ошибки = params._ПРОВЕРКИ["monitoring_targets"]([
        {"kind": "influxdb", "url": "http://a:8086/write"},
        {"kind": "influxdb", "url": "http://b:8086/write"}])
    assert any("занято" in о for о in ошибки)


# ---------------------------------------------------------------------------
# Форматы выгрузки
# ---------------------------------------------------------------------------


def test_graphite_кириллица_не_схлопывается():
    строки = exporters.graphite([
        Sample("asrhub_content_category_share", 0.12, {"category": "жалоба_на_доставку"}),
        Sample("asrhub_content_category_share", 0.03, {"category": "просит_руководителя"})])
    имена = [с.split()[0] for с in строки.strip().splitlines()]
    assert len(set(имена)) == 2 and "zhaloba_na_dostavku" in имена[0]


def test_нечисла_не_ломают_json_и_influx():
    снимок = [Sample("asrhub_queue_depth", float("nan"), {}), Sample("asrhub_up", 1.0, {})]
    json.dumps(exporters.json_snapshot(снимок, with_meta=False), allow_nan=False)
    json.dumps(exporters.otlp_payload(снимок), allow_nan=False)
    assert "NaN" not in exporters.influx_line(снимок)


def test_openmetrics_граница_корзины_числом_с_точкой():
    текст = exporters.prometheus([Sample("asrhub_rtf_bucket", 1.0, {"le": "1"})],
                                 openmetrics=True)
    assert 'le="1.0"' in текст


def test_панель_grafana_показывает_весь_каталог():
    from asrhub.monitoring.catalog import METRICS

    панель = json.dumps(exporters.grafana_dashboard())
    нет = [m.name for m in METRICS
           if m.type != "info" and not m.deprecated_for and m.name not in панель]
    assert нет == []
