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
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .catalog import METRICS_BY_NAME, порог, порог_нарушен, слово_порога
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
    #: Сравнивать включительно — когда порог стоит на краю шкалы метрики.
    #: None — «не сказано»: см. `__post_init__`.
    inclusive: bool | None = None
    #: Номер среди правил с тем же именем (см. `AlertEngine.set_rules`).
    ordinal: int = field(default=0, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Признак включительности всегда явный к моменту сравнения.

        Правила каталога несут его из порога — тем же знаком он уходит в
        Prometheus и Zabbix (`catalog.оператор_порога`). Своему правилу,
        где его не назвали, достаётся прежнее поведение движка: порог 1
        «выше» и 0 «ниже» — на краю шкалы признака 0/1, и строгое сравнение
        с ними не сработало бы никогда. Раньше то же самое движок решал сам
        при каждом сравнении — и для правил каталога тоже, отчего встроенные
        тревоги и выгрузка для Prometheus по одной метрике расходились.
        """
        if self.inclusive is None:
            self.inclusive = bool(
                (self.direction == "above" and self.threshold == 1)
                or (self.direction == "below" and self.threshold == 0))

    @property
    def id(self) -> str:
        """Имя правила: метрика, важность, метки — и номер, если имя занято.

        Два своих правила на одну метрику с одной важностью, но разными
        порогами (предупредить на 200 и на 500) раньше получали одно имя и
        общее состояние — и сбрасывали друг другу выдержку на каждом
        опросе. Теперь второе получает номер (`…#2`), а первое и все
        правила каталога — прежнее имя: на него ссылаются история тревог и
        лента событий.
        """
        suffix = "-".join(f"{k}:{v}" for k, v in sorted(self.labels.items()))
        имя = f"{self.metric}|{self.severity}" + (f"|{suffix}" if suffix else "")
        return f"{имя}#{self.ordinal}" if self.ordinal > 1 else имя

    @property
    def условие(self) -> tuple[Any, ...]:
        """Что правило проверяет — для поиска настоящих повторов."""
        return (self.metric, self.severity, tuple(sorted(self.labels.items())),
                self.direction, float(self.threshold), bool(self.inclusive))

    def matches(self, sample: Sample) -> bool:
        if sample.name != self.metric:
            return False
        return all(sample.labels.get(k) == v for k, v in self.labels.items())

    def breached(self, value: float) -> bool:
        """Нарушен ли порог — тем же знаком, что в Prometheus и Zabbix."""
        return порог_нарушен(value, self.direction, self.threshold, bool(self.inclusive))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "metric": self.metric, "direction": self.direction,
                "threshold": self.threshold, "severity": self.severity,
                "for_seconds": self.for_seconds, "labels": self.labels,
                "enabled": self.enabled, "summary": self.summary,
                "inclusive": self.inclusive}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Rule:
        """Правило из настроек или запроса.

        Направление и важность — без учёта регистра и только из известных:
        «Above» раньше читалось как «below» (тревога при глубине очереди 3 и
        тишина при 900), а «Critical» не считалось ни критической тревогой, ни
        предупреждением. `inclusive` терялся на круге GET → PUT, и
        критическое правило с порогом на краю шкалы не срабатывало никогда.
        """
        if not isinstance(data, dict):
            raise TypeError("правило — объект с полями metric, direction, threshold")
        направление = str(data.get("direction", "above")).strip().lower()
        if направление not in ("above", "below"):
            raise ValueError(f"direction «{data.get('direction')}»: допустимо above или below")
        важность = str(data.get("severity", "warning")).strip().lower()
        if важность not in ("warning", "critical"):
            raise ValueError(f"severity «{data.get('severity')}»: допустимо warning или critical")
        порог = float(data["threshold"])
        if not math.isfinite(порог):
            raise ValueError("threshold должен быть конечным числом")
        метки = data.get("labels") or {}
        if not isinstance(метки, dict):
            raise TypeError("labels — объект «метка: значение»")
        return cls(
            metric=str(data["metric"]),
            direction=направление,
            threshold=порог,
            severity=важность,
            for_seconds=max(0, int(float(data.get("for_seconds", 300)))),
            labels={str(k): str(v) for k, v in метки.items()},
            enabled=bool(data.get("enabled", True)),
            summary=str(data.get("summary", "")),
            # Не названо — решает __post_init__ (прежнее поведение движка).
            inclusive=_да_нет(data.get("inclusive")),
        )


def _да_нет(значение: Any) -> bool | None:
    """Признак из настроек: строка «false» — это «нет», а не непустая строка."""
    if значение is None or значение == "":
        return None
    if isinstance(значение, str):
        return значение.strip().lower() in ("1", "true", "yes", "on", "да")
    return bool(значение)


#: Сколько снимков подряд метрика должна отсутствовать, чтобы это считалось
#: отказом источника, а не заминкой сбора.
ПРОПАЖА_СНИМКОВ = 3

#: И сколько времени при этом должно пройти. Условия действуют вместе: три
#: снимка при опросе раз в полчаса — это полтора часа, а пять минут при
#: опросе раз в пять секунд — шестьдесят снимков.
ПРОПАЖА_СЕКУНД = 300.0

#: Сколько сработавшее условие должно не держаться, чтобы тревога снялась, —
#: но не дольше выдержки самого правила. Без этого значение, гуляющее у
#: порога (очередь то 51, то 49 при пороге 50), давало на каждом опросе
#: «тревога — снята — тревога» и забивало ленту событий, в которой тонуло
#: всё остальное. Правило с нулевой выдержкой снимается сразу, как и
#: поднимается.
УСПОКОЕНИЕ_С = 300.0


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
    #: Когда метрика впервые не пришла в снимок, и сколько снимков подряд.
    #: Пропажа на один-два снимка — не отказ источника, а заминка сбора.
    missing_since: float = 0.0
    missing: int = 0
    #: С какого момента сработавшее условие больше не держится (см.
    #: `УСПОКОЕНИЕ_С`); ноль — держится.
    clear_since: float = 0.0

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


def default_rules(settings: Any = None) -> list[Rule]:
    """Правила из порогов каталога метрик — то же, что уходит в Prometheus.

    `settings` сдвигает пороги, которые зависят от настроек сервера
    (свободное место — от `disk_min_free_gb`), тем же `catalog.порог`, что
    и выгрузки для Prometheus и Zabbix.
    """
    rules: list[Rule] = []
    for spec in METRICS_BY_NAME.values():
        threshold = порог(spec, settings)
        if not threshold or spec.name == "asrhub_up":
            continue
        # Метрики, для которых порог задан в процентах или в приросте, здесь
        # не проверяются: их выражения требуют функций Prometheus.
        if spec.name in {"asrhub_jobs_total", "asrhub_http_requests_total",
                         "asrhub_ram_used_bytes", "asrhub_gpu_memory_used_bytes",
                         "asrhub_no_speech_total", "asrhub_auth_failures_total",
                         "asrhub_webhooks_total", "asrhub_uptime_seconds"}:
            continue
        labels = {}
        if spec.name in {"asrhub_rtf", "asrhub_queue_wait_seconds"}:
            labels = {"stat": "p95"}
        elif spec.name == "asrhub_confidence":
            labels = {"stat": "avg"}
        уже: set[float] = set()
        for severity, value in (("critical", threshold.critical),
                                ("warning", threshold.warning)):
            # Одинаковые пороги у двух уровней — одно и то же условие: у
            # `asrhub_engines_available` (1 и 1) поднимались две тревоги об
            # одном. Выгрузка для Prometheus так и делала — оставляла
            # критическую; встроенный движок заводил обе.
            if value is None or value in уже:
                continue
            уже.add(value)
            rules.append(Rule(
                metric=spec.name, direction=threshold.direction, threshold=float(value),
                severity=severity, for_seconds=threshold.for_seconds, labels=labels,
                inclusive=bool(getattr(threshold, "inclusive", False)),
                summary=f"{spec.label}: "
                        f"{слово_порога(threshold.direction, bool(threshold.inclusive))} "
                        f"{_для_людей(float(value), spec.unit)}",
            ))
    return rules


def _для_людей(значение: float, единица: str) -> str:
    """Порог в подписи тревоги: «5 ГБ», а не «5368709120.0 Б»."""
    if единица == "Б":
        for предел, имя in ((1024 ** 4, "ТБ"), (1024 ** 3, "ГБ"), (1024 ** 2, "МБ"),
                            (1024, "КБ")):
            if abs(значение) >= предел:
                return f"{значение / предел:.4g} {имя}"
    return f"{значение:.6g}" + (f" {единица}" if единица else "")


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
        self._rules: list[Rule] = []
        self._states: dict[str, AlertState] = {}
        self._history: list[dict[str, Any]] = []
        self.on_change = on_change
        self.set_rules(rules if rules is not None else default_rules())

    # -- правила -------------------------------------------------------------

    @property
    def rules(self) -> list[Rule]:
        with self._lock:
            return list(self._rules)

    def set_rules(self, rules: list[Rule]) -> None:
        """Заменяет правила.

        Правило, повторяющее уже заведённое условие, отбрасывается: два
        одинаковых делили бы одно состояние и одну тревогу. Правило с тем
        же именем, но другим порогом получает номер — своё имя и своё
        состояние.
        """
        свои: list[Rule] = []
        условия: set[tuple[Any, ...]] = set()
        занято: dict[str, int] = {}
        for правило in rules:
            if правило.условие in условия:
                continue
            условия.add(правило.условие)
            правило.ordinal = 0
            основа = правило.id
            занято[основа] = занято.get(основа, 0) + 1
            if занято[основа] > 1:
                правило.ordinal = занято[основа]
            свои.append(правило)
        имена = {п.id for п in свои}
        with self._lock:
            self._rules = свои
            self._states = {k: v for k, v in self._states.items() if k in имена}

    def reset_rules(self, settings: Any = None) -> None:
        self.set_rules(default_rules(settings))

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
                # тревогу вечно нельзя: снимаем её, отметив в журнале. Но и
                # снимать по первой же пропаже нельзя, и это оказалось важнее.
                #
                # Один неудачный опрос источника (база занята, разбор
                # качества не сошёлся) сбрасывал выдержку в ноль, и отсчёт
                # начинался заново. У тревог с выдержкой в час — уверенность
                # модели, дрейф, доля отказов — это означало, что достаточно
                # ОДНОЙ осечки сбора в час, чтобы тревога не сработала
                # никогда. В пробе: уверенность двое суток держалась на 0,3
                # при пороге 0,6, источник отказывал раз в десять минут —
                # тревога не поднялась ни разу.
                self._forget(rule, now)
                continue
            self._вернулась(rule)
            # Берём худшее значение среди подходящих меток: если хоть одна
            # видеокарта перегрелась, тревога должна подняться.
            value = (max(s.value for s in candidates) if rule.direction == "above"
                     else min(s.value for s in candidates))
            self._advance(rule, value, now)

        with self._lock:
            return sorted(self._states.values(), key=_state_order)

    def _вернулась(self, rule: Rule) -> None:
        """Метрика снова в снимке — счётчик пропаж обнуляется."""
        with self._lock:
            state = self._states.get(rule.id)
            if state is not None and (state.missing or state.missing_since):
                state.missing = 0
                state.missing_since = 0.0

    def _forget(self, rule: Rule, now: float) -> None:
        """Снимает тревогу, если метрика надолго исчезла из снимка.

        «Надолго» — это и несколько снимков подряд, и заметное время: одно
        без другого ничего не значит. Три снимка при опросе раз в полчаса —
        полтора часа молчания, а пять минут при опросе раз в пять секунд —
        шестьдесят снимков. Поэтому оба условия сразу.
        """
        with self._lock:
            state = self._states.get(rule.id)
            if state is None or state.state == STATE_OK:
                return
            state.missing += 1
            if not state.missing_since:
                state.missing_since = now
            если_давно = now - state.missing_since >= ПРОПАЖА_СЕКУНД
            if state.missing < ПРОПАЖА_СНИМКОВ or not если_давно:
                return
            previous = state.state
            state.state = STATE_OK
            state.resolved_at = now
            state.since = 0.0
            state.clear_since = 0.0
            state.missing = 0
            state.missing_since = 0.0
            snapshot = state.to_dict()
        log.info("Тревога снята: метрика «%s» не приходит в снимок дольше %d с",
                 rule.metric, ПРОПАЖА_СЕКУНД)
        self._record(snapshot, previous)
        # В ленту событий — как и любое снятие. Раньше обработчик здесь не
        # звали: сработавшая тревога молча исчезала из раздела, а в журнале
        # событий так и оставалась «сработавшей» навсегда.
        self._сообщить(state, previous)

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
                state.clear_since = 0.0
                if previous == STATE_OK:
                    state.state = STATE_PENDING
                    state.since = now
                elif previous == STATE_PENDING and now - state.since >= rule.for_seconds:
                    state.state = STATE_FIRING
                    state.fired_at = now
            elif previous == STATE_FIRING:
                # Сработавшая тревога снимается не с первого же хорошего
                # значения, а когда оно продержалось (см. УСПОКОЕНИЕ_С).
                if not state.clear_since:
                    state.clear_since = now
                if now - state.clear_since >= min(УСПОКОЕНИЕ_С, float(rule.for_seconds)):
                    state.state = STATE_OK
                    state.resolved_at = now
                    state.since = 0.0
                    state.clear_since = 0.0
            elif previous != STATE_OK:
                state.state = STATE_OK
                state.resolved_at = now
                state.since = 0.0

            changed = previous != state.state
            snapshot = state.to_dict() if changed else None

        if changed and snapshot is not None:
            self._record(snapshot, previous)
            self._сообщить(state, previous)

    def _сообщить(self, state: AlertState, previous: str) -> None:
        """Зовёт обработчик смены состояния; его сбой — не повод падать."""
        if self.on_change:
            try:
                self.on_change(state, previous)
            except Exception as exc:                        # noqa: BLE001
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
