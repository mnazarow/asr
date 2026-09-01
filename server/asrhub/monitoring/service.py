"""Связка сбора, оповещений и отправки в один объект.

Всё, что нужно серверу от мониторинга, доступно через `MonitoringService`:
он держит сборщик, движок оповещений и отправку наружу, кеширует снимок на
короткое время и умеет отдавать себя в любом формате.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from . import exporters, probes
from .alerts import AlertEngine, Rule
from .collector import RUNTIME, Collector, Sample
from .pushers import PushManager, Target

log = logging.getLogger("asrhub.monitoring")


class MonitoringService:
    """Единая точка входа в мониторинг."""

    def __init__(self, state: Any, *, cache_ttl_s: float = 5.0) -> None:
        self.state = state
        self.runtime = RUNTIME
        self.collector = Collector(state, RUNTIME)
        self.alerts = AlertEngine(on_change=self._on_alert_change)
        self.push = PushManager(self._collect_for_push)
        self.cache_ttl_s = cache_ttl_s

        self._lock = threading.Lock()
        self._cache: list[Sample] = []
        self._cache_errors: list[str] = []
        self._cache_at = 0.0
        self._scrapes = 0

    # -- сбор ----------------------------------------------------------------

    def samples(self, *, fresh: bool = False) -> tuple[list[Sample], list[str]]:
        """Снимок метрик. Кешируется на несколько секунд.

        Кеш нужен потому, что Prometheus, панель интерфейса и отправка наружу
        могут прийти за метриками одновременно, а сбор трогает базу.
        """
        with self._lock:
            if not fresh and time.time() - self._cache_at < self.cache_ttl_s:
                return list(self._cache), list(self._cache_errors)

        collected, errors = self.collector.collect()
        collected.extend(self.push.healthy_samples())
        self.alerts.evaluate(collected)

        with self._lock:
            self._cache = collected
            self._cache_errors = errors
            self._cache_at = time.time()
            self._scrapes += 1
        return list(collected), list(errors)

    def _collect_for_push(self) -> list[Sample]:
        samples, _ = self.samples()
        return samples

    # -- выгрузка ------------------------------------------------------------

    def render(self, fmt: str, *, host: str = "asrhub") -> tuple[str, str]:
        """Возвращает пару «тело, тип содержимого» в запрошенном формате."""
        samples, errors = self.samples()
        if fmt in ("prometheus", "text", ""):
            return exporters.prometheus(samples), "text/plain; version=0.0.4; charset=utf-8"
        if fmt == "openmetrics":
            return (exporters.prometheus(samples, openmetrics=True),
                    "application/openmetrics-text; version=1.0.0; charset=utf-8")
        if fmt == "influx":
            return exporters.influx_line(samples), "text/plain; charset=utf-8"
        if fmt == "graphite":
            return exporters.graphite(samples), "text/plain; charset=utf-8"
        if fmt == "zabbix":
            return exporters.zabbix_sender(samples, host), "application/json; charset=utf-8"
        if fmt == "csv":
            return exporters.csv_table(samples), "text/csv; charset=utf-8"
        if fmt == "json":
            import json

            return (json.dumps(exporters.json_snapshot(samples, errors),
                               ensure_ascii=False, indent=1),
                    "application/json; charset=utf-8")
        if fmt == "otlp":
            import json

            return (json.dumps(exporters.otlp_payload(samples), ensure_ascii=False),
                    "application/json; charset=utf-8")
        raise ValueError(
            f"Неизвестный формат «{fmt}». Доступны: prometheus, openmetrics, json, "
            f"otlp, influx, graphite, zabbix, csv.")

    # -- состояние -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        self.samples()                      # заодно пересчитываем оповещения
        return probes.overall(self.state, self.alerts.summary())

    def info(self) -> dict[str, Any]:
        samples, errors = self.samples()
        return {
            "scrapes": self._scrapes,
            "samples": len(samples),
            "collection_errors": errors,
            "cache_ttl_s": self.cache_ttl_s,
            "alerts": self.alerts.summary(),
            "targets": self.push.targets(),
        }

    # -- настройка -----------------------------------------------------------

    def apply_settings(self, settings: Any) -> None:
        """Читает приёмники и пороги из настроек сервера."""
        raw_targets = settings.get("monitoring_targets") or []
        targets: list[Target] = []
        for item in raw_targets:
            try:
                targets.append(Target.from_dict(item))
            except (ValueError, KeyError, TypeError) as exc:
                log.warning("Приёмник метрик пропущен: %s", exc)
        self.push.set_targets(targets)

        raw_rules = settings.get("monitoring_rules") or []
        if raw_rules:
            rules: list[Rule] = []
            for item in raw_rules:
                try:
                    rules.append(Rule.from_dict(item))
                except (ValueError, KeyError, TypeError) as exc:
                    log.warning("Правило оповещения пропущено: %s", exc)
            if rules:
                self.alerts.set_rules(rules)

        self.cache_ttl_s = float(settings.get("monitoring_cache_ttl_s") or 5.0)
        if settings.get("monitoring_push_enabled", True) and targets:
            self.push.start()

    def start(self) -> None:
        self.apply_settings(self.state.settings)

    def stop(self) -> None:
        self.push.stop()

    # -- оповещения ----------------------------------------------------------

    def _on_alert_change(self, alert: Any, previous: str) -> None:
        """Пишет смену состояния в ленту событий сервера."""
        try:
            payload = alert.to_dict()
            kind = ("alert_firing" if payload["state"] == "firing"
                    else "alert_resolved" if previous == "firing" else "alert_pending")
            if kind == "alert_pending":
                return                      # промежуточное состояние в ленту не пишем
            self.state.db.add_event(None, kind, payload["summary"], payload)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Не удалось записать событие оповещения: %s", exc)
