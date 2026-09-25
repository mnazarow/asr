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

from .. import settings_access as S
from . import exporters, probes
from .alerts import AlertEngine, Rule, default_rules
from .collector import RUNTIME, Collector, Sample
from .pushers import PushManager, Target

log = logging.getLogger("asrhub.monitoring")

#: Как часто встроенные тревоги считаются сами, без внешнего опроса.
#: Раньше их считал только опрос метрик: на установке без Prometheus и
#: без открытой вкладки «Мониторинг» — то есть ровно там, ради которой
#: встроенные тревоги и заведены, — не срабатывала ни одна, и лента
#: событий о кончающемся диске молчала.
ОЦЕНКА_ТРЕВОГ_С = 60.0

#: Сколько ждать чужой сбор снимка, прежде чем собрать самому.
ЖДАТЬ_СБОР_С = 120.0

#: Ключи настроек, от которых зависит мониторинг.
КЛЮЧИ_НАСТРОЕК = frozenset({"monitoring_targets", "monitoring_rules",
                            "monitoring_cache_ttl_s", "monitoring_push_enabled",
                            "disk_min_free_gb"})


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
        self._собран = threading.Condition(self._lock)
        self._сбор_идёт = False
        self._cache: list[Sample] = []
        self._cache_errors: list[str] = []
        self._cache_at = 0.0
        self._scrapes = 0
        self._стоп = threading.Event()
        self._оценка: threading.Thread | None = None

    # -- сбор ----------------------------------------------------------------

    def samples(self, *, fresh: bool = False) -> tuple[list[Sample], list[str]]:
        """Снимок метрик. Кешируется на несколько секунд.

        Кеш нужен потому, что Prometheus, панель интерфейса и отправка наружу
        могут прийти за метриками одновременно, а сбор трогает базу. Сбор
        при этом идёт один за раз: раньше после истечения кеша каждый из них
        собирал снимок сам, и три одинаковых обхода базы шли параллельно.
        Теперь второй ждёт первого и получает его снимок — он собран уже
        после того, как второй пришёл, то есть не старше, чем нужно.
        """
        запрошено = time.time()
        with self._собран:
            while True:
                if not fresh and time.time() - self._cache_at < self.cache_ttl_s:
                    return list(self._cache), list(self._cache_errors)
                if not self._сбор_идёт:
                    self._сбор_идёт = True
                    break
                self._собран.wait(timeout=ЖДАТЬ_СБОР_С)
                if self._cache_at >= запрошено:
                    return list(self._cache), list(self._cache_errors)
        try:
            collected, errors = self.collector.collect()
            collected.extend(self.push.healthy_samples())
            self.alerts.evaluate(collected)
            with self._собран:
                self._cache = collected
                self._cache_errors = errors
                self._cache_at = time.time()
                self._scrapes += 1
            return list(collected), list(errors)
        finally:
            with self._собран:
                self._сбор_идёт = False
                self._собран.notify_all()

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
        if fmt == "zabbix_sender":
            return exporters.zabbix_sender_lines(samples, host), "text/plain; charset=utf-8"
        if fmt == "csv":
            return exporters.csv_table(samples), "text/csv; charset=utf-8"
        if fmt == "json":
            import json

            return (json.dumps(exporters.json_snapshot(samples, errors,
                                                       settings=self.state.settings),
                               ensure_ascii=False, indent=1),
                    "application/json; charset=utf-8")
        if fmt == "otlp":
            import json

            return (json.dumps(exporters.otlp_payload(samples), ensure_ascii=False),
                    "application/json; charset=utf-8")
        raise ValueError(
            f"Неизвестный формат «{fmt}». Доступны: prometheus, openmetrics, json, "
            f"otlp, influx, graphite, zabbix, zabbix_sender, csv.")

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
        """Читает приёмники и пороги из настроек сервера.

        Зовётся и при запуске, и после каждого сохранения настроек, в
        которых есть что-то из `КЛЮЧИ_НАСТРОЕК`. Прежде — только при
        запуске: приёмник, добавленный в «Настройках», начинал работать
        после перезапуска, а выключенная отправка продолжала слать.
        """
        # Ни одна запись здесь не валит запуск: строка вместо списка (так её
        # сохранял интерфейс, пока тип был неизвестным «list») раньше давала
        # AttributeError посреди запуска, и сервер не поднимался.
        raw_targets = settings.get("monitoring_targets") or []
        if not isinstance(raw_targets, list):
            log.warning("Приёмники метрик пропущены: ожидается список, записано %s",
                        type(raw_targets).__name__)
            raw_targets = []
        targets: list[Target] = []
        for item in raw_targets:
            try:
                targets.append(Target.from_dict(item))
            except Exception as exc:                         # noqa: BLE001
                log.warning("Приёмник метрик пропущен: %s", exc)
        self.push.set_targets(targets)

        raw_rules = settings.get("monitoring_rules") or []
        if not isinstance(raw_rules, list):
            log.warning("Правила оповещения пропущены: ожидается список, записано %s",
                        type(raw_rules).__name__)
            raw_rules = []
        rules: list[Rule] = []
        for item in raw_rules:
            try:
                rules.append(Rule.from_dict(item))
            except Exception as exc:                         # noqa: BLE001
                log.warning("Правило оповещения пропущено: %s", exc)
        # Пустой список — пороги каталога, сдвинутые по настройкам сервера
        # (место на диске — от disk_min_free_gb). Раньше пустой список
        # оставлял прежние правила как есть: убранные из настроек свои
        # пороги продолжали действовать до перезапуска.
        self.alerts.set_rules(rules or default_rules(settings))
        self._переоценить()

        # Ноль — «кеш выключен», а не «по умолчанию»: `or 5.0` превращал
        # отладочный ноль из примера в каталоге обратно в пять секунд.
        self.cache_ttl_s = max(0.0, S.num(settings, "monitoring_cache_ttl_s", 5.0))
        if settings.get("monitoring_push_enabled", True) and targets:
            self.push.start()
        else:
            self.push.stop()

    def start(self) -> None:
        self.apply_settings(self.state.settings)
        self.collector.начать_замер()
        if self._оценка is None:
            self._стоп.clear()
            self._оценка = threading.Thread(target=self._оценивать, name="asrhub-alerts",
                                             daemon=True)
            self._оценка.start()

    def stop(self) -> None:
        self._стоп.set()
        if self._оценка is not None:
            self._оценка.join(timeout=5.0)
            self._оценка = None
        self.push.stop()

    def _оценивать(self) -> None:
        """Фоновый расчёт тревог: снимок раз в минуту, если его никто не брал."""
        while not self._стоп.wait(ОЦЕНКА_ТРЕВОГ_С):
            try:
                self.samples()
            except Exception as exc:                        # noqa: BLE001
                log.debug("Фоновый расчёт тревог не удался: %s", exc)

    def _переоценить(self) -> None:
        """Правила сменились — тревоги по ним считаются сразу, по последнему снимку.

        Иначе до истечения кеша снимка у новых правил не было состояния:
        раздел «Мониторинг» после «Вернуть пороги» или правки правила
        показывал «Тревог нет», пока не пройдёт следующий сбор.
        """
        with self._собран:
            снимок = list(self._cache)
        if not снимок:
            return
        try:
            self.alerts.evaluate(снимок)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Тревоги по новым правилам не пересчитаны: %s", exc)

    # -- сохранение того, что правят из раздела «Мониторинг» -----------------

    def save_targets(self, targets: list[Target]) -> dict[str, Any]:
        """Ставит приёмники в работу и записывает их в настройки и файл."""
        self.push.set_targets(targets)
        if targets and self.state.settings.get("monitoring_push_enabled", True):
            self.push.start()
        elif not targets:
            self.push.stop()
        return self._сохранить("monitoring_targets", [t.to_config() for t in targets])

    def save_rules(self, rules: list[Rule] | None) -> dict[str, Any]:
        """Ставит правила (None — пороги каталога) и записывает в настройки."""
        if rules is None:
            self.alerts.reset_rules(self.state.settings)
            self._переоценить()
            return self._сохранить("monitoring_rules", [])
        self.alerts.set_rules(rules)
        self._переоценить()
        return self._сохранить("monitoring_rules",
                               [{к: з for к, з in r.to_dict().items() if к != "id"}
                                for r in self.alerts.rules])

    def _сохранить(self, ключ: str, значение: Any) -> dict[str, Any]:
        """Значение — в настройки сервера и в файл конфигурации.

        Раньше PUT /targets и PUT /alerts/rules меняли только то, что в
        памяти: интерфейс отвечал «Приёмник добавлен», в «Настройках»
        приёмника не было, а перезапуск его стирал. В файл пишется только
        этот ключ — не всё, что на странице настроек применили на пробу.
        """
        settings = self.state.settings
        settings.set(ключ, значение, source="api")
        return settings.записать_ключи([ключ])

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
