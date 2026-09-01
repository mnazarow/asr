"""Оповещения по порогам, вычисляемые на стороне сервиса.

Нужны не вместо Prometheus, а для случая, когда его нет: небольшая установка
без стороннего мониторинга всё равно должна уметь сказать «диск кончается».
Если Prometheus есть, эти же пороги отдаются готовым файлом правил, и
дублировать их здесь не обязательно — правило можно выключить.

Состояния устроены как у Prometheus: ok -> pending -> firing -> resolved.
Промежуточное pending существует, чтобы одиночный всплеск не будил дежурного:
тревога поднимается, только если условие держится дольше `for_seconds`.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .catalog import METRICS_BY_NAME
from .collector import Sample

log = logging.getLogger("asrhub.monitoring")

STATE_OK = "ok"
STATE_PENDING = "pending"
STATE_FIRING = "firing"


@dataclass
class Rule:
    """Правило: метрика, направление, порог, выдержка."""

    metric: str
    direction: str                      # above | below
    threshold: float
    severity: str = "warning"
    for_seconds: int = 300
    labels: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    summary: str = ""

    @property
    def id(self) -> str:
        suffix = "-".join(f"{k}:{v}" for k, v in sorted(self.labels.items()))
        return f"{self.metric}|{self.severity}" + (f"|{suffix}" if suffix else "")

    def matches(self, sample: Sample) -> bool:
        if sample.name != self.metric:
            return False
        return all(sample.labels.get(k) == v for k, v in self.labels.items())

    def breached(self, value: float) -> bool:
        """Нарушен ли порог.

        Для признаков со значениями 0 и 1 строгое «больше единицы» не
        сработает никогда, поэтому единица сравнивается на равенство.
        """
        if self.direction == "above":
            return value >= self.threshold if self.threshold == 1 else value > self.threshold
        return value <= self.threshold if self.threshold == 0 else value < self.threshold

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "metric": self.metric, "direction": self.direction,
                "threshold": self.threshold, "severity": self.severity,
                "for_seconds": self.for_seconds, "labels": self.labels,
                "enabled": self.enabled, "summary": self.summary}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        return cls(
            metric=str(data["metric"]),
            direction=str(data.get("direction", "above")),
            threshold=float(data["threshold"]),
            severity=str(data.get("severity", "warning")),
            for_seconds=int(data.get("for_seconds", 300)),
            labels={str(k): str(v) for k, v in (data.get("labels") or {}).items()},
            enabled=bool(data.get("enabled", True)),
            summary=str(data.get("summary", "")),
        )


@dataclass
class AlertState:
    """Текущее состояние одного правила."""

    rule: Rule
    state: str = STATE_OK
    value: float = 0.0
    since: float = 0.0
    fired_at: float = 0.0
    resolved_at: float = 0.0
    breaches: int = 0

    def to_dict(self) -> dict[str, Any]:
        spec = METRICS_BY_NAME.get(self.rule.metric)
        return {
            "id": self.rule.id,
            "state": self.state,
            "severity": self.rule.severity,
            "metric": self.rule.metric,
            "label": spec.label if spec else self.rule.metric,
            "unit": spec.unit if spec else "",
            "value": self.value,
            "threshold": self.rule.threshold,
            "direction": self.rule.direction,
            "since": self.since,
            "active_seconds": round(time.time() - self.since, 1) if self.since else 0.0,
            "fired_at": self.fired_at or None,
            "resolved_at": self.resolved_at or None,
            "breaches": self.breaches,
            "summary": self.rule.summary or (spec.label if spec else self.rule.metric),
            "hint": (spec.troubleshooting or spec.recommendation) if spec else "",
        }


def default_rules() -> list[Rule]:
    """Правила из порогов каталога метрик — то же, что уходит в Prometheus."""
    rules: list[Rule] = []
    for spec in METRICS_BY_NAME.values():
        threshold = spec.threshold
        if not threshold or spec.name == "asrhub_up":
            continue
        # Метрики, для которых порог задан в процентах или в приросте, здесь
        # не проверяются: их выражения требуют функций Prometheus.
        if spec.name in {"asrhub_jobs_total", "asrhub_http_requests_total",
                         "asrhub_ram_used_mb", "asrhub_gpu_memory_mb",
                         "asrhub_no_speech_total", "asrhub_auth_failures_total",
                         "asrhub_webhooks_total", "asrhub_uptime_seconds"}:
            continue
        labels = {}
        if spec.name in {"asrhub_rtf", "asrhub_queue_wait_seconds"}:
            labels = {"quantile": "p95"}
        elif spec.name == "asrhub_confidence":
            labels = {"quantile": "avg"}
        for severity, value in (("critical", threshold.critical),
                                ("warning", threshold.warning)):
            if value is None:
                continue
            rules.append(Rule(
                metric=spec.name, direction=threshold.direction, threshold=float(value),
                severity=severity, for_seconds=threshold.for_seconds, labels=labels,
                summary=f"{spec.label}: {'выше' if threshold.direction == 'above' else 'ниже'} "
                        f"{value}{(' ' + spec.unit) if spec.unit else ''}",
            ))
    return rules


def _state_order(state: AlertState) -> tuple[bool, bool, str]:
    """Сначала сработавшие, среди них — критичные, затем по имени метрики."""
    return (state.state != STATE_FIRING,
            state.rule.severity != "critical",
            state.rule.metric)


class AlertEngine:
    """Хранит правила, считает состояния и зовёт обработчик при смене."""

    def __init__(self, rules: list[Rule] | None = None,
                 on_change: Callable[[AlertState, str], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._rules: list[Rule] = rules if rules is not None else default_rules()
        self._states: dict[str, AlertState] = {}
        self._history: list[dict[str, Any]] = []
        self.on_change = on_change

    # -- правила -------------------------------------------------------------

    @property
    def rules(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def set_rules(self, rules: list[Rule]) -> None:
        with self._lock:
            self._rules = list(rules)
            valid = {r.id for r in rules}
            self._states = {k: v for k, v in self._states.items() if k in valid}

    def reset_rules(self) -> None:
        self.set_rules(default_rules())

    # -- вычисление ----------------------------------------------------------

    def evaluate(self, samples: list[Sample]) -> list[AlertState]:
        """Прогоняет снимок через правила и возвращает состояния."""
        now = time.time()
        by_name: dict[str, list[Sample]] = {}
        for sample in samples:
            by_name.setdefault(sample.name, []).append(sample)

        with self._lock:
            rules = list(self._rules)

        for rule in rules:
            if not rule.enabled:
                continue
            candidates = [s for s in by_name.get(rule.metric, []) if rule.matches(s)]
            if not candidates:
                # Метрика пропала из снимка — источник мог отказать. Держать
                # тревогу вечно нельзя: снимаем её, отметив в журнале.
                self._forget(rule, now)
                continue
            # Берём худшее значение среди подходящих меток: если хоть одна
            # видеокарта перегрелась, тревога должна подняться.
            value = (max(s.value for s in candidates) if rule.direction == "above"
                     else min(s.value for s in candidates))
            self._advance(rule, value, now)

        with self._lock:
            return sorted(self._states.values(), key=_state_order)

    def _forget(self, rule: Rule, now: float) -> None:
        """Снимает тревогу, если метрика исчезла из снимка."""
        with self._lock:
            state = self._states.get(rule.id)
            if state is None or state.state == STATE_OK:
                return
            previous = state.state
            state.state = STATE_OK
            state.resolved_at = now
            state.since = 0.0
            snapshot = state.to_dict()
        log.info("Тревога снята: метрика «%s» исчезла из снимка", rule.metric)
        self._record(snapshot, previous)

    def _advance(self, rule: Rule, value: float, now: float) -> None:
        with self._lock:
            state = self._states.get(rule.id)
            if state is None:
                state = self._states[rule.id] = AlertState(rule=rule)
            previous = state.state
            state.value = value
            state.rule = rule

            if rule.breached(value):
                state.breaches += 1
                if previous == STATE_OK:
                    state.state = STATE_PENDING
                    state.since = now
                elif previous == STATE_PENDING and now - state.since >= rule.for_seconds:
                    state.state = STATE_FIRING
                    state.fired_at = now
            elif previous != STATE_OK:
                state.state = STATE_OK
                state.resolved_at = now
                state.since = 0.0

            changed = previous != state.state
            snapshot = state.to_dict() if changed else None

        if changed and snapshot is not None:
            self._record(snapshot, previous)
            if self.on_change:
                try:
                    self.on_change(state, previous)
                except Exception as exc:                    # noqa: BLE001
                    log.warning("Обработчик оповещения упал: %s", exc)

    def _record(self, snapshot: dict[str, Any], previous: str) -> None:
        entry = {"ts": time.time(), "from": previous, **snapshot}
        with self._lock:
            self._history.append(entry)
            del self._history[:-500]
        if snapshot["state"] == STATE_FIRING:
            log.warning("Тревога: %s — значение %s при пороге %s",
                        snapshot["summary"], snapshot["value"], snapshot["threshold"])
        elif previous == STATE_FIRING:
            log.info("Тревога снята: %s", snapshot["summary"])

    # -- состояние -----------------------------------------------------------

    def states(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in sorted(self._states.values(), key=_state_order)]

    def firing(self) -> list[dict[str, Any]]:
        return [s for s in self.states() if s["state"] == STATE_FIRING]

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._history[-limit:]))

    def summary(self) -> dict[str, Any]:
        states = self.states()
        firing = [s for s in states if s["state"] == STATE_FIRING]
        return {
            "rules": len(self._rules),
            "firing": len(firing),
            "pending": sum(1 for s in states if s["state"] == STATE_PENDING),
            "critical": sum(1 for s in firing if s["severity"] == "critical"),
            "warning": sum(1 for s in firing if s["severity"] == "warning"),
            "worst": ("critical" if any(s["severity"] == "critical" for s in firing)
                      else "warning" if firing else "ok"),
        }
