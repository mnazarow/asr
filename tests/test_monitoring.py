"""Проверки подсистемы мониторинга: каталог, сбор, выгрузка, тревоги, пробы."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from asrhub.api import create_app
from asrhub.config import load
from asrhub.monitoring import (
    METRICS,
    METRICS_BY_NAME,
    AlertEngine,
    Sample,
    default_rules,
    exporters,
)
from asrhub.monitoring import catalog as metric_catalog
from asrhub.monitoring.alerts import STATE_FIRING, STATE_OK, STATE_PENDING, Rule
from asrhub.monitoring.pushers import KINDS, Target
from fastapi.testclient import TestClient

SERVER = Path(__file__).resolve().parent.parent / "server"


@pytest.fixture()
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    settings = load()
    app = create_app(settings, start_queue=True)
    with TestClient(app) as test_client:
        yield test_client


# --- каталог метрик --------------------------------------------------------

def test_catalog_loads():
    assert len(METRICS) > 50
    assert len(metric_catalog.GROUPS) >= 10


def test_metric_names_unique_and_valid():
    names = [m.name for m in METRICS]
    assert len(names) == len(set(names)), "имена метрик должны быть уникальны"
    for name in names:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", name), f"недопустимое имя метрики: {name}"
        assert name.startswith("asrhub_"), f"метрика без общего префикса: {name}"


def test_every_metric_documented():
    for spec in METRICS:
        assert spec.description.strip(), f"{spec.name}: нет описания"
        assert spec.label.strip(), f"{spec.name}: нет подписи"
        assert spec.group in {g["id"] for g in metric_catalog.GROUPS}, \
            f"{spec.name}: чужая группа"
        assert spec.type in ("gauge", "counter", "histogram", "info")


def test_thresholds_are_sane():
    for spec in METRICS:
        threshold = spec.threshold
        if not threshold:
            continue
        assert threshold.direction in ("above", "below")
        if threshold.warning is not None and threshold.critical is not None:
            if threshold.direction == "above":
                assert threshold.critical >= threshold.warning, \
                    f"{spec.name}: критический порог ниже предупредительного"
            else:
                assert threshold.critical <= threshold.warning, \
                    f"{spec.name}: критический порог выше предупредительного"


# --- выгрузка --------------------------------------------------------------

SAMPLES = [
    Sample("asrhub_up", 1),
    Sample("asrhub_queue_depth", 7),
    Sample("asrhub_rtf", 0.12, {"quantile": "p95"}),
    Sample("asrhub_build_info", 1, {"version": "3.0.0", "python": "3.12.1"}),
]


def test_prometheus_format():
    text = exporters.prometheus(SAMPLES)
    assert "# HELP asrhub_queue_depth" in text
    assert "# TYPE asrhub_queue_depth gauge" in text
    assert 'asrhub_rtf{quantile="p95"} 0.12' in text
    # HELP не должен повторяться для одной метрики
    assert text.count("# HELP asrhub_up") == 1


def test_prometheus_escapes_label_values():
    text = exporters.prometheus([Sample("asrhub_up", 1, {"note": 'кавычка " и \\ слеш'})])
    assert r"\"" in text and r"\\" in text


def test_openmetrics_has_eof():
    assert exporters.prometheus(SAMPLES, openmetrics=True).rstrip().endswith("# EOF")


def test_influx_and_graphite():
    influx = exporters.influx_line(SAMPLES)
    assert "asrhub_rtf,quantile=p95 value=0.12" in influx
    graphite = exporters.graphite(SAMPLES)
    assert "asrhub.rtf.p95 0.12" in graphite


def test_zabbix_and_csv():
    zabbix = json.loads(exporters.zabbix_sender(SAMPLES, "server1"))
    assert zabbix["request"] == "sender data"
    assert all(item["host"] == "server1" for item in zabbix["data"])
    assert "asrhub_rtf[p95]" in [item["key"] for item in zabbix["data"]]

    csv = exporters.csv_table(SAMPLES)
    assert csv.splitlines()[0] == "metric,labels,value,unit,group"


def test_otlp_payload_shape():
    payload = exporters.otlp_payload(SAMPLES)
    metrics = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]
    assert metrics and all("gauge" in m or "sum" in m for m in metrics)
    json.dumps(payload)          # сериализуемость


def test_json_snapshot_carries_descriptions():
    payload = exporters.json_snapshot(SAMPLES)
    entry = next(m for m in payload["metrics"] if m["name"] == "asrhub_queue_depth")
    assert entry["description"] and entry["recommendation"]
    assert entry["values"][0]["value"] == 7


# --- готовые конфигурации --------------------------------------------------

def test_prometheus_rules_are_valid_yaml():
    parsed = yaml.safe_load(exporters.prometheus_rules())
    rules = parsed["groups"][0]["rules"]
    assert len(rules) > 20
    required = {"alert", "expr", "for", "labels", "annotations"}
    for rule in rules:
        assert required <= set(rule), f"неполное правило: {rule.get('alert')}"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rule["alert"]), \
            f"имя правила должно быть машинным: {rule['alert']}"
        assert rule["annotations"]["summary"]


def test_prometheus_rules_reference_existing_metrics():
    parsed = yaml.safe_load(exporters.prometheus_rules())
    known = set(METRICS_BY_NAME)
    for rule in parsed["groups"][0]["rules"]:
        mentioned = set(re.findall(r"asrhub_[a-z_]+", rule["expr"]))
        assert mentioned <= known, f"{rule['alert']}: неизвестная метрика {mentioned - known}"


def test_grafana_dashboard_is_valid_json():
    dashboard = exporters.grafana_dashboard()
    json.dumps(dashboard)
    assert dashboard["panels"]
    for panel in dashboard["panels"]:
        assert panel["targets"], f"панель без запросов: {panel['title']}"
        assert {"h", "w", "x", "y"} <= set(panel["gridPos"])


def test_zabbix_template_is_valid_yaml():
    parsed = yaml.safe_load(exporters.zabbix_template())
    items = parsed["zabbix_export"]["templates"][0]["items"]
    assert len(items) > 30
    assert all(item["key"].startswith("asrhub_") for item in items)


# --- тревоги ---------------------------------------------------------------

def test_default_rules_built_from_catalog():
    rules = default_rules()
    assert rules
    assert all(r.metric in METRICS_BY_NAME for r in rules)


def test_alert_state_machine():
    rule = Rule(metric="asrhub_disk_free_gb", direction="below", threshold=10,
                severity="critical", for_seconds=0)
    engine = AlertEngine(rules=[rule])

    engine.evaluate([Sample("asrhub_disk_free_gb", 50)])
    assert engine.states() == [] or engine.states()[0]["state"] == STATE_OK

    engine.evaluate([Sample("asrhub_disk_free_gb", 3)])
    assert engine.states()[0]["state"] == STATE_PENDING

    engine.evaluate([Sample("asrhub_disk_free_gb", 3)])
    assert engine.states()[0]["state"] == STATE_FIRING
    assert engine.summary()["firing"] == 1

    engine.evaluate([Sample("asrhub_disk_free_gb", 50)])
    assert engine.states()[0]["state"] == STATE_OK
    assert engine.summary()["firing"] == 0
    assert len(engine.history()) >= 2


def test_alert_waits_for_duration():
    """Одиночный всплеск не должен поднимать тревогу сразу."""
    rule = Rule(metric="asrhub_queue_depth", direction="above", threshold=10,
                for_seconds=3600)
    engine = AlertEngine(rules=[rule])
    for _ in range(5):
        engine.evaluate([Sample("asrhub_queue_depth", 99)])
    assert engine.states()[0]["state"] == STATE_PENDING
    assert engine.summary()["firing"] == 0


def test_alert_picks_worst_label():
    """Из нескольких видеокарт тревогу поднимает самая горячая."""
    rule = Rule(metric="asrhub_gpu_temperature_celsius", direction="above",
                threshold=80, for_seconds=0)
    engine = AlertEngine(rules=[rule])
    for _ in range(2):
        engine.evaluate([Sample("asrhub_gpu_temperature_celsius", 60, {"gpu": "0"}),
                         Sample("asrhub_gpu_temperature_celsius", 91, {"gpu": "1"})])
    state = engine.states()[0]
    assert state["state"] == STATE_FIRING
    assert state["value"] == 91


# --- приёмники -------------------------------------------------------------

def test_target_validation():
    target = Target.from_dict({"kind": "influxdb", "url": "http://influx:8086"})
    assert target.name == "influxdb" and target.instance
    with pytest.raises(ValueError):
        Target.from_dict({"kind": "carrier-pigeon", "url": "http://x"})


def test_target_interval_has_floor():
    assert Target.from_dict({"kind": "statsd", "url": "x", "interval_s": 1}).interval_s == 10


# --- программный интерфейс -------------------------------------------------

def test_metrics_endpoint(client):
    response = client.get("/api/monitoring/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    body = response.text
    assert "asrhub_up 1" in body
    assert "asrhub_queue_depth" in body


@pytest.mark.parametrize("fmt", ["prometheus", "openmetrics", "json", "otlp",
                                 "influx", "graphite", "zabbix", "csv"])
def test_all_export_formats(client, fmt):
    response = client.get(f"/api/monitoring/metrics?format={fmt}")
    assert response.status_code == 200, fmt
    assert response.text.strip(), f"пустой ответ для формата {fmt}"


def test_unknown_format_is_rejected(client):
    response = client.get("/api/monitoring/metrics?format=клинопись")
    assert response.status_code == 400
    assert "Доступны" in response.json()["detail"]["hint"] or \
           "Доступны" in response.json()["detail"]["message"]


def test_metrics_json_has_no_collection_errors(client):
    payload = client.get("/api/monitoring/metrics.json").json()
    assert not payload.get("collection_errors"), payload.get("collection_errors")
    assert len(payload["metrics"]) > 20


def test_probes(client):
    for path in ("/api/monitoring/live", "/api/monitoring/ready",
                 "/api/monitoring/startup", "/api/monitoring/health"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.json()["status"] in ("ok", "warn", "warning")


def test_catalog_endpoint(client):
    payload = client.get("/api/monitoring/catalog").json()
    assert payload["stats"]["total"] == len(METRICS)
    assert all("description" in m for m in payload["metrics"])


def test_catalog_item_not_found(client):
    response = client.get("/api/monitoring/catalog/asrhub_nonexistent")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "metric_not_found"


def test_alerts_endpoint(client):
    payload = client.get("/api/monitoring/alerts").json()
    assert "summary" in payload and "alerts" in payload
    assert payload["summary"]["rules"] > 0


def test_alert_rules_roundtrip(client):
    rules = [{"metric": "asrhub_queue_depth", "direction": "above",
              "threshold": 999, "severity": "warning", "for_seconds": 60}]
    assert client.put("/api/monitoring/alerts/rules", json=rules).status_code == 200
    assert client.get("/api/monitoring/alerts/rules").json()["rules"][0]["threshold"] == 999
    assert client.post("/api/monitoring/alerts/rules/reset").status_code == 200
    assert len(client.get("/api/monitoring/alerts/rules").json()["rules"]) == len(default_rules())


def test_bad_alert_rule_is_rejected(client):
    response = client.put("/api/monitoring/alerts/rules", json=[{"metric": "x"}])
    assert response.status_code == 400
    assert response.json()["detail"]["hint"]


def test_targets_roundtrip(client):
    assert client.get("/api/monitoring/targets").json()["kinds"] == list(KINDS)
    targets = [{"kind": "webhook", "url": "http://127.0.0.1:1/none", "interval_s": 3600}]
    assert client.put("/api/monitoring/targets", json=targets).status_code == 200
    listed = client.get("/api/monitoring/targets").json()["targets"]
    assert listed[0]["kind"] == "webhook"


def test_bad_target_is_rejected(client):
    response = client.put("/api/monitoring/targets", json=[{"kind": "смс"}])
    assert response.status_code == 400


def test_target_test_reports_failure_without_raising(client):
    """Недоступный приёмник должен вернуть ошибку в ответе, а не уронить запрос."""
    response = client.post("/api/monitoring/targets/test",
                           json={"kind": "webhook", "url": "http://127.0.0.1:1/none"})
    assert response.status_code == 200
    assert response.json()["ok"] is False


def test_generated_configs_endpoints(client):
    for path in ("/api/monitoring/config/prometheus",
                 "/api/monitoring/config/prometheus-scrape",
                 "/api/monitoring/config/zabbix"):
        response = client.get(path)
        assert response.status_code == 200, path
        yaml.safe_load(response.text)

    dashboard = client.get("/api/monitoring/config/grafana")
    assert dashboard.status_code == 200
    assert dashboard.json()["panels"]


def test_http_metrics_are_counted(client):
    client.get("/api/health")
    body = client.get("/api/monitoring/metrics").text
    assert "asrhub_http_requests_total" in body
    assert 'route="/api/health"' in body


def test_route_labels_do_not_include_ids(client):
    """Идентификатор задания не должен попадать в метку маршрута."""
    client.get("/api/jobs/job_doesnotexist")
    body = client.get("/api/monitoring/metrics").text
    assert "job_doesnotexist" not in body


def test_info_endpoint(client):
    payload = client.get("/api/monitoring/info").json()
    assert payload["samples"] > 0
    assert payload["scrapes"] >= 1


# --- регрессии после ревизии -----------------------------------------------

def test_histogram_buckets_are_cumulative_and_bounded():
    """Корзины гистограммы: неубывающие и не больше общего числа измерений."""
    from asrhub.monitoring.collector import Histogram

    hist = Histogram((0.005, 0.01, 0.05, 0.5, 1))
    for value in (0.003, 0.02, 0.02, 7.0):
        hist.observe(value)
    counts = [count for _, count in hist.cumulative()]
    assert counts == sorted(counts), "накопленные счётчики должны расти"
    assert max(counts) <= hist.total, "корзина не может быть больше числа измерений"
    assert hist.total == 4


def test_histogram_in_exposition_is_monotonic(client):
    """Накопленные корзины каждой серии должны идти по возрастанию."""
    client.get("/api/health")               # чтобы в гистограмме что-то было
    body = client.get("/api/monitoring/metrics").text

    series: dict[str, list[float]] = {}
    for line in body.splitlines():
        if line.startswith("#") or "_bucket{" not in line:
            continue
        name, _, rest = line.partition("{")
        labels, _, value = rest.rpartition("}")
        # Ключ серии — имя и все метки, кроме le: иначе смешаются разные
        # маршруты и проверка станет бессмысленной.
        key = name + "|" + ",".join(sorted(
            part for part in labels.split(",") if not part.startswith("le=")))
        series.setdefault(key, []).append(float(value.strip()))

    assert series, "гистограммы должны присутствовать в выгрузке"
    for key, values in series.items():
        assert values == sorted(values), f"{key}: корзины не монотонны — {values}"


def test_route_label_cardinality_is_bounded(client):
    """Перебор несуществующих путей не должен плодить серии метрик."""
    from asrhub.api.app import _route_fallback

    unknown = {_route_fallback(f"/scan-{i}") for i in range(50)}
    assert unknown == {"other"}
    assert _route_fallback("/api/jobs/job_abc123/download") == "/api/jobs/{id}/download"
    assert _route_fallback("/api/health") == "/api/health"


def test_alert_is_cleared_when_metric_disappears():
    """Исчезнувшая метрика снимает тревогу, а не замораживает её навсегда."""
    rule = Rule(metric="asrhub_disk_free_gb", direction="below", threshold=10,
                for_seconds=0)
    engine = AlertEngine(rules=[rule])
    for _ in range(2):
        engine.evaluate([Sample("asrhub_disk_free_gb", 1)])
    assert engine.states()[0]["state"] == STATE_FIRING
    engine.evaluate([])
    assert engine.states()[0]["state"] == STATE_OK


def test_boolean_threshold_fires():
    """Признак 0/1 с порогом «выше 1» должен срабатывать при значении 1."""
    rule = Rule(metric="asrhub_queue_paused", direction="above", threshold=1,
                for_seconds=0)
    engine = AlertEngine(rules=[rule])
    for _ in range(2):
        engine.evaluate([Sample("asrhub_queue_paused", 1)])
    assert engine.states()[0]["state"] == STATE_FIRING


def test_every_catalog_metric_is_produced_somewhere():
    """Каждая метрика каталога должна где-то вычисляться.

    Проверка статическая: часть метрик появляется только при наличии
    видеокарты или обработанных заданий, поэтому по снимку в пустой среде
    судить нельзя. Зато можно убедиться, что имя встречается в коде сборщика
    или в вызовах счётчиков — именно этого не хватало, когда девять
    счётчиков жили только в каталоге и правила поверх них молчали.
    """
    # Смотрим весь пакет сервера, кроме самого каталога метрик: иначе
    # объявление считалось бы за вычисление.
    sources = ""
    for path in sorted((SERVER / "asrhub").rglob("*.py")):
        if path.name in ("catalog.py",) and path.parent.name == "monitoring":
            continue
        if "exporters.py" in path.name or path.parent.name == "__pycache__":
            continue
        sources += path.read_text(encoding="utf-8")

    orphans = [spec.name for spec in METRICS if spec.name not in sources]
    assert not orphans, f"метрики объявлены, но нигде не вычисляются: {orphans}"


def test_generated_rules_reference_known_metrics():
    """Правила не должны ссылаться на метрики, которых нет в каталоге."""
    parsed = yaml.safe_load(exporters.prometheus_rules())
    known = set(METRICS_BY_NAME)
    unknown = {(rule["alert"], name)
               for rule in parsed["groups"][0]["rules"]
               for name in re.findall(r"asrhub_[a-z_]+", rule["expr"])
               if name not in known}
    assert not unknown, f"правила поверх неизвестных метрик: {sorted(unknown)}"


def test_monitoring_requires_key_when_not_public(data_dir, monkeypatch):
    """При выключенном monitoring_public метрики закрыты, но ключ работает."""
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    settings = load()
    settings.values["monitoring_public"] = False
    settings.api_keys["ah_test_key"] = {"name": "тест", "role": "admin", "enabled": True}
    app = create_app(settings, start_queue=False)
    with TestClient(app) as c:
        assert c.get("/api/monitoring/metrics").status_code == 401
        ok = c.get("/api/monitoring/metrics", headers={"X-API-Key": "ah_test_key"})
        assert ok.status_code == 200, "верный ключ должен пропускаться"
        assert "asrhub_up" in ok.text


def test_push_manager_restarts_after_stop():
    """После stop() отправку метрик можно запустить снова."""
    from asrhub.monitoring.pushers import PushManager

    manager = PushManager(lambda: [])
    manager.start()
    manager.stop()
    manager.start()
    assert manager._thread is not None and manager._thread.is_alive()
    manager.stop()
