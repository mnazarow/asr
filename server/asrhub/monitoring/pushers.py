"""Отправка метрик во внешние системы по расписанию.

Prometheus забирает метрики сам, и это правильный режим по умолчанию. Но
сервер распознавания часто стоит там, куда снаружи не достучаться: закрытый
контур, NAT, машина под столом. Тогда метрики отправляет он сам.

Поддерживаются пять приёмников. Все они работают по одной схеме: раз в
`interval_s` собирается снимок, переводится в нужный формат и отправляется.
Сбой отправки не влияет на работу сервиса — он только отмечается в метрике
asrhub_push_targets_healthy, чтобы молчащий приёмник было видно.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import exporters
from .collector import Sample

log = logging.getLogger("asrhub.monitoring")

KINDS = ("prometheus_pushgateway", "influxdb", "otlp", "statsd", "webhook")


@dataclass
class Target:
    """Описание приёмника метрик."""

    kind: str
    url: str = ""
    interval_s: int = 60
    enabled: bool = True
    name: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    job: str = "asrhub"
    instance: str = ""
    database: str = "asrhub"
    prefix: str = "asrhub"
    timeout_s: float = 10.0

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.kind
        if not self.instance:
            self.instance = socket.gethostname()

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "url": self.url,
                "interval_s": self.interval_s, "enabled": self.enabled,
                "job": self.job, "instance": self.instance, "prefix": self.prefix}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Target:
        kind = str(data.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"Неизвестный приёмник «{kind}». Доступны: {', '.join(KINDS)}")
        return cls(
            kind=kind, url=str(data.get("url") or ""),
            interval_s=max(10, int(data.get("interval_s", 60))),
            enabled=bool(data.get("enabled", True)),
            name=str(data.get("name") or kind),
            headers={str(k): str(v) for k, v in (data.get("headers") or {}).items()},
            job=str(data.get("job") or "asrhub"),
            instance=str(data.get("instance") or ""),
            database=str(data.get("database") or "asrhub"),
            prefix=str(data.get("prefix") or "asrhub"),
            timeout_s=min(60.0, max(1.0, float(data.get("timeout_s", 10.0)))),
        )


@dataclass
class TargetState:
    """Что случилось при последней отправке."""

    target: Target
    last_attempt: float = 0.0
    last_success: float = 0.0
    last_error: str = ""
    sent: int = 0
    failed: int = 0

    @property
    def healthy(self) -> bool:
        return self.last_success >= self.last_attempt and self.last_attempt > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.target.to_dict(),
            "healthy": self.healthy,
            "last_attempt": self.last_attempt or None,
            "last_success": self.last_success or None,
            "last_error": self.last_error,
            "sent": self.sent, "failed": self.failed,
        }


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        if response.status >= 300:
            raise RuntimeError(f"HTTP {response.status}")


def _base_metric_name(name: str) -> str:
    """Имя метрики без суффиксов гистограммы."""
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _statsd_name(sample: Sample, prefix: str) -> str:
    """Имя в точечной записи с сохранением имён меток.

    Graphite и StatsD меток не знают, поэтому их приходится вписывать в имя.
    Раньше записывались только значения, и метки разных измерений
    склеивались по позиции: понять, что означает `asrhub.wer.gigaam_v3`,
    было можно, а `asrhub.rtf.p95` — уже нет.
    """
    parts = [prefix, sample.name.replace("asrhub_", "")]
    for key, value in sorted(sample.labels.items()):
        clean = str(value).replace(".", "_").replace(" ", "_").replace("/", "_")
        parts.append(f"{key}.{clean}")
    return ".".join(parts)


def _format_value(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(round(float(value), 6))


def send(target: Target, samples: list[Sample]) -> None:
    """Отправляет снимок в один приёмник. Бросает исключение при неудаче."""
    if target.kind == "prometheus_pushgateway":
        # Pushgateway различает наборы по пути job/instance, а не по телу.
        url = target.url.rstrip("/")
        if "/metrics/job/" not in url:
            url = f"{url}/metrics/job/{target.job}/instance/{target.instance}"
        _post(url, exporters.prometheus(samples).encode("utf-8"),
              {"Content-Type": "text/plain; version=0.0.4", **target.headers},
              target.timeout_s)

    elif target.kind == "influxdb":
        url = target.url
        if "write" not in url and "api/v2" not in url:
            url = f"{url.rstrip('/')}/write?db={target.database}"
        _post(url, exporters.influx_line(samples).encode("utf-8"),
              {"Content-Type": "text/plain; charset=utf-8", **target.headers},
              target.timeout_s)

    elif target.kind == "otlp":
        url = target.url
        if not url.rstrip("/").endswith("/v1/metrics"):
            url = f"{url.rstrip('/')}/v1/metrics"
        payload = exporters.otlp_payload(samples)
        _post(url, json.dumps(payload).encode("utf-8"),
              {"Content-Type": "application/json", **target.headers}, target.timeout_s)

    elif target.kind == "statsd":
        from .catalog import METRICS_BY_NAME

        host, _, port = target.url.replace("udp://", "").partition(":")
        host = host or "127.0.0.1"
        port_number = int(port or 8125)
        # Семейство определяем по факту: приёмник может слушать IPv6.
        family = socket.AF_INET
        try:
            family = socket.getaddrinfo(host, port_number, type=socket.SOCK_DGRAM)[0][0]
        except (socket.gaierror, ValueError):
            pass

        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(target.timeout_s)
            for item in samples:
                spec = METRICS_BY_NAME.get(_base_metric_name(item.name))
                # Тип имеет значение: счётчик, отправленный как gauge, теряет
                # смысл — StatsD перестаёт видеть по нему прирост.
                kind = "c" if (spec and spec.type == "counter") else "g"
                line = f"{_statsd_name(item, target.prefix)}:{_format_value(item.value)}|{kind}"
                sock.sendto(line.encode("utf-8"), (host, port_number))

    elif target.kind == "webhook":
        body = json.dumps(exporters.json_snapshot(samples, with_meta=False),
                          ensure_ascii=False).encode("utf-8")
        _post(target.url, body,
              {"Content-Type": "application/json; charset=utf-8", **target.headers},
              target.timeout_s)

    else:
        raise ValueError(f"Неизвестный приёмник: {target.kind}")


class PushManager:
    """Фоновая отправка метрик во все настроенные приёмники."""

    def __init__(self, collect: Callable[[], list[Sample]],
                 targets: list[Target] | None = None) -> None:
        self._collect = collect
        self._lock = threading.Lock()
        self._states: dict[str, TargetState] = {}
        self._next_at: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.set_targets(targets or [])

    # -- настройка -----------------------------------------------------------

    def set_targets(self, targets: list[Target]) -> None:
        with self._lock:
            self._states = {t.name: TargetState(target=t) for t in targets}
            self._next_at = {t.name: 0.0 for t in targets}

    def targets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._states.values()]

    def healthy_samples(self) -> list[Sample]:
        """Метрика о состоянии самих приёмников — мониторинг мониторинга."""
        with self._lock:
            return [Sample("asrhub_push_targets_healthy", 1.0 if s.healthy else 0.0,
                           {"target": s.target.name})
                    for s in self._states.values() if s.target.enabled]

    # -- работа --------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        # Флаг остановки надо снять: иначе после stop() новый поток выходил
        # на первом же ожидании, и отправка молча прекращалась навсегда.
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="asrhub-push", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        failures = 0
        while not self._stop.wait(timeout=5.0):
            try:
                self.tick()
            except Exception as exc:                        # noqa: BLE001
                # Неудача доставки в конкретный приёмник разбирается в
                # push_once и видна в метрике. Сюда долетает только поломка
                # самого цикла — её нельзя оставлять на уровне debug, иначе
                # отправка метрик молча стоит, а понять это неоткуда.
                failures += 1
                if failures <= 3 or failures % 120 == 0:
                    log.warning("Цикл отправки метрик дал сбой (%d-й раз): %s",
                                failures, exc)
                else:
                    log.debug("Цикл отправки метрик: %s", exc)
            else:
                if failures:
                    log.info("Цикл отправки метрик восстановился после %d сбоев", failures)
                failures = 0

    def tick(self) -> None:
        """Отправляет метрики в те приёмники, у которых подошёл срок."""
        now = time.time()
        with self._lock:
            due = [s.target for s in self._states.values()
                   if s.target.enabled and self._next_at.get(s.target.name, 0) <= now]
        if not due:
            return

        samples = self._collect()
        # Каждому приёмнику свой поток: иначе один недоступный адрес с
        # десятисекундным тайм-аутом задерживал бы отправку во все остальные.
        for target in due:
            with self._lock:
                self._next_at[target.name] = time.time() + target.interval_s
            threading.Thread(target=self.push_once, args=(target, samples),
                             name=f"asrhub-push-{target.name}", daemon=True).start()

    def push_once(self, target: Target, samples: list[Sample] | None = None) -> dict[str, Any]:
        """Одна отправка. Используется и циклом, и кнопкой «проверить»."""
        payload = samples if samples is not None else self._collect()
        with self._lock:
            state = self._states.get(target.name) or TargetState(target=target)
            self._states[target.name] = state
            state.last_attempt = time.time()
        try:
            send(target, payload)
        except (urllib.error.URLError, OSError, RuntimeError, ValueError) as exc:
            with self._lock:
                state.failed += 1
                state.last_error = f"{type(exc).__name__}: {exc}"
            log.warning("Не удалось отправить метрики в «%s»: %s", target.name, exc)
            return {"ok": False, "error": state.last_error}
        with self._lock:
            state.sent += 1
            state.last_success = time.time()
            state.last_error = ""
        return {"ok": True, "sent_metrics": len(payload)}
