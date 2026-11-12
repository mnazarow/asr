"""Связка сбора, оповещений и отправки в один объект.

Всё, что нужно серверу от мониторинга, доступно через `MonitoringService`:
он держит сборщик, движок оповещений и отправку наружу, кеширует снимок на
короткое время и умеет отдавать себя в любом формате.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from .. import settings_access as S
from . import exporters, probes
from .alerts import AlertEngine, Rule, default_rules, наложить
from .collector import RUNTIME, Collector, Sample
from .pushers import PushManager, Target

log = logging.getLogger("asrhub.monitoring")

#: Как часто встроенные тревоги считаются сами, без внешнего опроса.
#: Раньше их считал только опрос метрик: на установке без Prometheus и
#: без открытой вкладки «Мониторинг» — то есть ровно там, ради которой
#: встроенные тревоги и заведены, — не срабатывала ни одна, и лента
#: событий о кончающемся диске молчала.
ОЦЕНКА_ТРЕВОГ_С = 60.0

#: Сколько ждать чужой сбор снимка. Дольше — отдаётся прежний снимок с
#: пометкой, что он устарел: прежде после тайм-аута ожидание начиналось
#: заново, и при зависшем сборе висели все /metrics, /health, /alerts и
#: /info, каждый со своим потоком пула.
ЖДАТЬ_СБОР_С = 120.0

#: Файл, в котором тревоги переживают перезапуск (см. `AlertEngine.хранить_в`).
ФАЙЛ_ТРЕВОГ = "monitoring-alerts.json"

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
        данные = getattr(getattr(getattr(state, "settings", None), "paths", None), "data", None)
        if данные:
            self.alerts.хранить_в(Path(str(данные)) / ФАЙЛ_ТРЕВОГ)
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
                # Срок кеша — не меньше длительности последнего сбора: на
                # большом архиве сбор идёт секунды, и при кеше короче сбора
                # каждый опрос запускал новый сразу после предыдущего —
                # база не отдыхала вовсе. Ноль в настройке — кеш выключен.
                срок = (max(self.cache_ttl_s, getattr(self, "_длился_с", 0.0))
                        if self.cache_ttl_s > 0 else 0.0)
                if not fresh and time.time() - self._cache_at < срок:
                    return list(self._cache), list(self._cache_errors)
                if not self._сбор_идёт:
                    self._сбор_идёт = True
                    break
                self._собран.wait(timeout=ЖДАТЬ_СБОР_С)
                if self._cache_at >= запрошено:
                    return list(self._cache), list(self._cache_errors)
                if self._сбор_идёт and time.time() - запрошено >= ЖДАТЬ_СБОР_С \
                        and self._cache_at:
                    # Чужой сбор висит дольше срока — отдаём прежний снимок
                    # и говорим, что он устарел, а не ждём снова.
                    return list(self._cache), [
                        *self._cache_errors,
                        f"снимок устарел: сбор идёт дольше {ЖДАТЬ_СБОР_С:g} с, отдан "
                        f"собранный {time.time() - self._cache_at:.0f} с назад"]
        try:
            начало = time.monotonic()
            collected, errors = self.collector.collect()
            self._длился_с = time.monotonic() - начало
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

    def последний(self) -> tuple[list[Sample], list[str]]:
        """Последний собранный снимок — без нового сбора, если он уже был.

        Тревоги считает фоновый поток раз в минуту и каждый сбор метрик, а
        интерфейс спрашивает их каждые четыре секунды с любой страницы.
        Сбор по каждому такому вопросу — это обход базы за сутки на каждый
        опрос меню; ответ по последнему расчёту ничем не хуже.
        """
        with self._собран:
            if self._cache_at:
                return list(self._cache), list(self._cache_errors)
        return self.samples()

    def health(self) -> dict[str, Any]:
        self.последний()                    # тревоги — по последнему расчёту
        return probes.overall(self.state, self.alerts.summary())

    def info(self) -> dict[str, Any]:
        samples, errors = self.последний()
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
        занятые: set[str] = set()
        for item in raw_targets:
            try:
                приёмник = Target.from_dict(item)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Приёмник метрик пропущен: %s", exc)
                continue
            # Имя — ключ приёмника, а по умолчанию это его вид: два influxdb
            # без name схлопывались в один, и работал только последний. Как у
            # POST /targets — занятое имя получает номер.
            if приёмник.name in занятые:
                основа, номер = приёмник.name, 2
                while f"{основа}-{номер}" in занятые:
                    номер += 1
                log.warning("Два приёмника метрик с именем «%s»: второй назван «%s-%d»",
                            основа, основа, номер)
                приёмник.name = f"{основа}-{номер}"
            занятые.add(приёмник.name)
            targets.append(приёмник)
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
        # Свои правила — поверх порогов каталога, сдвинутых по настройкам
        # сервера (место на диске — от disk_min_free_gb), а не вместо них
        # (см. `alerts.наложить`). Пустой список — одни пороги каталога.
        self.alerts.set_rules(наложить(default_rules(settings), rules))
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
        if self._оценка is not None and self._оценка.is_alive():
            # Недождавшийся при остановке поток продолжит работу — второй
            # такой же считал бы тревоги дважды (см. `PushManager.start`).
            self._стоп.clear()
        else:
            self._стоп.clear()
            self._оценка = threading.Thread(target=self._оценивать, name="asrhub-alerts",
                                             daemon=True)
            self._оценка.start()

    def stop(self) -> None:
        self._стоп.set()
        if self._оценка is not None:
            self._оценка.join(timeout=5.0)
            if not self._оценка.is_alive():
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
        if not rules:
            # Пустой список — те же пороги каталога, что «Вернуть пороги»:
            # прежде PUT [] оставлял ноль правил до перезапуска.
            self.alerts.reset_rules(self.state.settings)
            self._переоценить()
            return self._сохранить("monitoring_rules", [])
        # Свои правила — поверх каталога; в настройку уходят только свои.
        self.alerts.set_rules(наложить(default_rules(self.state.settings), rules))
        self._переоценить()
        return self._сохранить("monitoring_rules",
                               [{к: з for к, з in r.to_dict().items() if к != "id"}
                                for r in rules])

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
        """Пишет смену состояния в ленту событий сервера и шлёт на `alert_url`."""
        try:
            payload = alert.to_dict()
            kind = ("alert_firing" if payload["state"] == "firing"
                    else "alert_resolved" if previous == "firing" else "alert_pending")
            if kind == "alert_pending":
                return                      # промежуточное состояние в ленту не пишем
            self.state.db.add_event(None, kind, payload["summary"], payload)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Не удалось записать событие оповещения: %s", exc)
            return
        self._уведомить(kind, payload)

    def _уведомить(self, kind: str, payload: dict[str, Any]) -> None:
        """Тревога и её снятие — на `alert_url`, если он задан.

        Уведомлений о встроенных тревогах не было вовсе: смена состояния
        уходила только в ленту событий, а её смотрят, когда уже знают, что
        искать. Теперь — POST с JSON на адрес чата или дежурной системы, тем
        же путём и с той же проверкой адреса, что у трекеров и сводки.
        Отправка — отдельным потоком: тревоги считает сбор метрик, и ждать в
        нём чужой сервер нельзя.
        """
        settings = getattr(self.state, "settings", None)
        адрес = str((settings.get("alert_url") if settings else "") or "").strip()
        if not адрес or settings is None:
            return
        from ..outbound import check_outbound_url  # noqa: PLC0415

        try:
            check_outbound_url(адрес, bool(settings.get("webhook_allow_internal")))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Адрес уведомлений о тревогах отвергнут: %s", exc)
            return
        тело = {"event": kind, "text": (("Тревога: " if kind == "alert_firing"
                                         else "Тревога снята: ") + str(payload.get("summary"))),
                **payload}
        from ..maintenance import send_json  # noqa: PLC0415

        threading.Thread(target=send_json, args=(тело, адрес),
                         kwargs={"what": "тревога", "попыток": 4,
                                 "allow_internal": bool(settings.get("webhook_allow_internal"))},
                         name="asrhub-alert-notify",
                         daemon=True).start()
