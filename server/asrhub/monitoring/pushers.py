"""Отправка метрик во внешние системы по расписанию.

Prometheus забирает метрики сам, и это правильный режим по умолчанию. Но
сервер распознавания часто стоит там, куда снаружи не достучаться: закрытый
контур, NAT, машина под столом. Тогда метрики отправляет он сам.

Поддерживаются шесть приёмников. Все они работают по одной схеме: раз в
`interval_s` собирается снимок, переводится в нужный формат и отправляется.
Сбой отправки не влияет на работу сервиса — он только отмечается в метрике
asrhub_push_targets_healthy, чтобы молчащий приёмник было видно.
"""
from __future__ import annotations

import json
import logging
import re
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

KINDS = ("prometheus_pushgateway", "influxdb", "otlp", "statsd", "webhook", "zabbix")

#: Заглушка вместо значения заголовка в ответах сервера. Пришла обратно —
#: значит «не менял»: заголовок берётся у сохранённого приёмника.
ЗАГЛУШКА = "***"

#: Как часто повторять данные обнаружения Zabbix, даже если наборы меток не
#: менялись: сервер Zabbix мог перезапуститься, шаблон — переимпортироваться.
ZABBIX_LLD_ПОВТОР_С = 1800.0

#: Как часто жаловаться в журнал на один и тот же недоступный приёмник.
COMPLAIN_INTERVAL_S = 3600.0


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
    #: Имя узла в Zabbix — ровно как он заведён там. Пусто — имя машины.
    host: str = ""
    #: Что задано руками: пустое — «имя машины». В config.yaml пишется
    #: именно это, а не подставленное имя: иначе приёмник, сохранённый из
    #: интерфейса, навсегда получал имя той машины, где его сохранили, и
    #: два сервера с общей настройкой слали в Pushgateway под одним
    #: `instance`, затирая друг друга.
    instance_задан: str = field(default="", init=False, repr=False, compare=False)
    host_задан: str = field(default="", init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.kind
        self.instance_задан = self.instance
        self.host_задан = self.host
        if not self.instance:
            self.instance = socket.gethostname()
        if not self.host:
            self.host = self.instance

    def to_dict(self) -> dict[str, Any]:
        """Описание приёмника — со всеми полями, какие у него есть.

        Прежде здесь не было заголовков, базы, тайм-аута и имени узла, а
        интерфейс собирал список для сохранения из этого ответа: добавление
        нового приёмника стирало у прежних заголовок Authorization,
        сбрасывало базу InfluxDB в «asrhub» и включало выключенные. Значения
        заголовков по-прежнему не отдаются — вместо них заглушка, которая
        при сохранении означает «оставить как было».
        """
        return {"name": self.name, "kind": self.kind, "url": self.url,
                "interval_s": self.interval_s, "enabled": self.enabled,
                "job": self.job, "instance": self.instance_задан, "prefix": self.prefix,
                "database": self.database, "timeout_s": self.timeout_s,
                "host": self.host_задан,
                "headers": dict.fromkeys(self.headers, ЗАГЛУШКА)}

    def to_config(self) -> dict[str, Any]:
        """Запись для config.yaml: всё, включая настоящие заголовки."""
        return {**self.to_dict(), "headers": dict(self.headers)}

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, прежний: Target | None = None) -> Target:
        """Приёмник из настроек или запроса.

        `прежний` — сохранённый приёмник с тем же именем: из него берётся
        всё, чего в описании нет, и значения заголовков, пришедшие
        заглушкой. Так список можно отредактировать по ответу GET и
        отправить обратно, ничего не потеряв.
        """
        if not isinstance(data, dict):
            raise TypeError("приёмник — объект с полями kind и url")
        if data.get("headers") is not None and not isinstance(data["headers"], dict):
            raise TypeError("headers — объект «заголовок: значение»")
        основа = прежний.to_config() if прежний is not None else {}
        поля = {**основа, **{к: з for к, з in data.items() if з is not None}}
        kind = str(поля.get("kind") or "")
        if kind not in KINDS:
            raise ValueError(f"Неизвестный приёмник «{kind}». Доступны: {', '.join(KINDS)}")
        заголовки: dict[str, str] = {}
        прежние = dict(основа.get("headers") or {})
        for ключ, значение in (поля.get("headers") or {}).items():
            if str(значение) == ЗАГЛУШКА:
                if ключ in прежние:
                    заголовки[str(ключ)] = str(прежние[ключ])
                continue
            заголовки[str(ключ)] = str(значение)
        return cls(
            kind=kind, url=str(поля.get("url") or ""),
            interval_s=max(10, int(float(поля.get("interval_s", 60)))),
            enabled=_да(поля.get("enabled", True)),
            name=str(поля.get("name") or kind),
            headers=заголовки,
            job=str(поля.get("job") or "asrhub"),
            instance=str(поля.get("instance") or ""),
            database=str(поля.get("database") or "asrhub"),
            prefix=str(поля.get("prefix") or "asrhub"),
            timeout_s=min(60.0, max(1.0, float(поля.get("timeout_s", 10.0)))),
            host=str(поля.get("host") or ""),
        )


def _да(значение: Any) -> bool:
    """«false» из формы или YAML — это «нет», а не непустая строка."""
    if isinstance(значение, str):
        return значение.strip().lower() not in ("0", "false", "no", "off", "нет", "")
    return bool(значение)


@dataclass
class TargetState:
    """Что случилось при последней отправке."""

    target: Target
    last_attempt: float = 0.0
    last_success: float = 0.0
    last_error: str = ""
    sent: int = 0
    failed: int = 0
    #: Когда о неудаче последний раз писали в журнал. Отправка идёт раз в
    #: минуту, и недоступный приёмник давал по строке в минуту круглосуточно:
    #: за ночь это полторы тысячи одинаковых предупреждений, в которых тонет
    #: всё остальное — в том числе причина, по которой пришли в журнал.
    last_complaint: float = 0.0
    #: StatsD: значения накопительных счётчиков при последней удачной
    #: отправке — чтобы слать прирост, а не итог (см. `_statsd_lines`).
    statsd_last: dict[str, float] = field(default_factory=dict)
    #: Zabbix: отпечаток наборов меток обнаружения и когда их слали.
    zabbix_lld: str = ""
    zabbix_lld_at: float = 0.0
    #: Что ответил приёмник на последнюю отправку, если он что-то сказал
    #: (Zabbix: «processed: 120; failed: 3»).
    last_info: str = ""

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
            "last_info": self.last_info,
            "sent": self.sent, "failed": self.failed,
        }


class _БезПеренаправлений(urllib.request.HTTPRedirectHandler):
    """Перенаправление — это ошибка доставки, а не повод идти дальше.

    urllib на 301/302/303 превращает POST в GET без тела и идёт по новому
    адресу. Приёмник за прокси с перенаправлением http→https «принимал»
    отправку: ответ 200 на GET, приёмник здоров, счётчик отправленного
    растёт — а данных нет. Проверка `status >= 300` при этом не
    срабатывала никогда: до неё доходил уже ответ второго запроса.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_ОТКРЫВАТЕЛЬ = urllib.request.build_opener(_БезПеренаправлений)


def _post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, data=body, method="POST")
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with _ОТКРЫВАТЕЛЬ.open(request, timeout=timeout) as response:
            if response.status >= 300:
                raise RuntimeError(f"HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        куда = exc.headers.get("Location") if exc.headers else ""
        if 300 <= exc.code < 400:
            raise RuntimeError(
                f"HTTP {exc.code}: приёмник перенаправляет"
                + (f" на {скрыть_адрес(куда)}" if куда else "")
                + " — укажите конечный адрес") from exc
        raise


#: Параметры адреса, в которых приёмники обычно носят учётные данные:
#: InfluxDB 1.x — u и p, Pushgateway и прочие — token, key, password.
_СЕКРЕТЫ_В_АДРЕСЕ = re.compile(
    r"(?i)([?&](?:u|p|user|pass|password|pwd|token|key|apikey|api_key|"
    r"access_token|auth|secret)=)[^&#\s]+")


def скрыть_адрес(адрес: str) -> str:
    """Адрес без учётных данных: в запросе и перед «@»."""
    текст = _СЕКРЕТЫ_В_АДРЕСЕ.sub(r"\1***", str(адрес or ""))
    return re.sub(r"(//)[^/@\s]+@", r"\1***@", текст)


def _без_секретов(текст: str, target: Target) -> str:
    """Текст ошибки без учётных данных приёмника.

    urllib кладёт в сообщение об ошибке весь адрес — вместе с `?p=пароль`.
    Оно уходило и в журнал, и в last_error, а last_error отдаётся ключу
    «только чтение»: адрес ему прятали, а текст ошибки с тем же адресом —
    нет.
    """
    итог = str(текст or "")
    if target.url:
        итог = итог.replace(target.url, скрыть_адрес(target.url))
    return скрыть_адрес(итог)


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


def _statsd_lines(target: Target, samples: list[Sample],
                  прежние: dict[str, float] | None) -> tuple[list[str], dict[str, float]]:
    """Строки StatsD и новые «прежние» значения счётчиков.

    StatsD складывает присланное с типом `|c` как ПРИРОСТ. А слали итог с
    запуска: `jobs_total:150|c` каждую минуту без единого нового задания
    давал в StatsD +150 за каждый интервал — за три отправки 450 при нуле
    новых. Теперь уходит разница с прошлой удачной отправкой; если значение
    уменьшилось (сервер перезапускался), приростом считается само значение
    — ровно так rate() в Prometheus понимает сброс счётчика.

    `прежние` = None — разовая проба приёмника: прирост ей считать не от
    чего, а итог с запуска удвоил бы счёт у рабочей отправки. Проба шлёт
    по счётчикам ноль — связь проверена, данные не испорчены.

    Корзины гистограмм не отправляются: StatsD их не понимает, а двенадцать
    рядов на гистограмму — шум. Число наблюдений и сумма уходят приростом.
    """
    from .catalog import METRICS_BY_NAME

    строки: list[str] = []
    новые: dict[str, float] = {}
    for item in samples:
        base = _base_metric_name(item.name)
        spec = METRICS_BY_NAME.get(base)
        if spec is not None and spec.type == "histogram" and item.name.endswith("_bucket"):
            continue
        имя = _statsd_name(item, target.prefix)
        накопительный = spec is not None and (
            spec.type == "counter" or (spec.type == "histogram" and item.name != base))
        if not накопительный:
            строки.append(f"{имя}:{_format_value(item.value)}|g")
            continue
        значение = float(item.value)
        новые[имя] = значение
        if прежние is None:
            прирост = 0.0
        else:
            было = прежние.get(имя, 0.0)
            прирост = значение - было if значение >= было else значение
        строки.append(f"{имя}:{_format_value(прирост)}|c")
    return строки, новые


#: Заголовок пакета протокола Zabbix: «ZBXD» и флаги (1 — обычный пакет).
_ZBXD = b"ZBXD"


def _zabbix_address(url: str) -> tuple[str, int]:
    """Узел и порт траппера из адреса: zabbix://узел:10051, tcp://…, узел:порт."""
    текст = str(url or "").strip()
    for схема in ("zabbix://", "tcp://"):
        if текст.startswith(схема):
            текст = текст[len(схема):]
    текст = текст.split("/", 1)[0]
    if текст.startswith("["):                      # [::1]:10051
        узел, _, хвост = текст[1:].partition("]")
        порт = хвост.lstrip(":")
    else:
        узел, _, порт = текст.rpartition(":") if текст.count(":") == 1 else (текст, "", "")
    if not узел:
        raise ValueError("адрес Zabbix: укажите узел, например zabbix://zabbix:10051")
    return узел, int(порт or 10051)


def _zabbix_send(target: Target, данные: list[dict[str, Any]]) -> str:
    """Отправляет значения серверу или прокси Zabbix по протоколу траппера.

    Прежде документация велела слать в Zabbix «webhook» на порт 10051 — это
    HTTP POST со снимком в JSON, которого траппер не понимает: данных не
    доходило ни одного. Протокол траппера — тот же, что у zabbix_sender:
    заголовок ZBXD, длина, JSON `{"request": "sender data", "data": […]}`.
    Ответ — строка «processed: N; failed: M; total: K»; если не принято ни
    одно значение — это ошибка доставки, а не успех.
    """
    import struct
    import zlib

    узел, порт = _zabbix_address(target.url)
    тело = json.dumps({"request": "sender data", "data": данные, "clock": int(time.time())},
                      ensure_ascii=False).encode("utf-8")
    пакет = _ZBXD + b"\x01" + struct.pack("<II", len(тело), 0) + тело
    with socket.create_connection((узел, порт), timeout=target.timeout_s) as связь:
        связь.settimeout(target.timeout_s)
        связь.sendall(пакет)
        ответ = b""
        while True:
            кусок = связь.recv(65536)
            if not кусок:
                break
            ответ += кусок
            if len(ответ) >= 13 and ответ[:4] == _ZBXD:
                флаги = ответ[4]
                ширина = 8 if флаги & 0x04 else 4
                длина = int.from_bytes(ответ[5:5 + ширина], "little")
                if len(ответ) >= 5 + 2 * ширина + длина:
                    break
    if len(ответ) < 13 or ответ[:4] != _ZBXD:
        raise RuntimeError("Zabbix ответил не по протоколу траппера — это точно порт "
                           "10051 сервера или прокси Zabbix?")
    флаги = ответ[4]
    ширина = 8 if флаги & 0x04 else 4
    длина = int.from_bytes(ответ[5:5 + ширина], "little")
    данные_ответа = ответ[5 + 2 * ширина:5 + 2 * ширина + длина]
    if флаги & 0x02:
        данные_ответа = zlib.decompress(данные_ответа)
    try:
        разбор = json.loads(данные_ответа.decode("utf-8", errors="replace"))
    except ValueError as exc:
        raise RuntimeError(f"Zabbix ответил не JSON: {данные_ответа[:120]!r}") from exc
    сведения = str(разбор.get("info") or "")
    if разбор.get("response") != "success":
        raise RuntimeError(f"Zabbix отказал: {разбор.get('response')} {сведения}".strip())
    принято = re.search(r"processed:?\s*(\d+)", сведения)
    отбито = re.search(r"failed:?\s*(\d+)", сведения)
    if принято and отбито and int(принято.group(1)) == 0 and int(отбито.group(1)) > 0:
        raise RuntimeError(
            f"Zabbix не принял ни одного значения ({сведения}). Проверьте, что узел "
            f"«{target.host}» заведён в Zabbix под этим именем и к нему привязан шаблон "
            "«ASR Hub» (GET /api/monitoring/config/zabbix). Первая отправка после "
            "привязки шаблона бывает отбита: элементы по обнаружению Zabbix создаёт "
            "с задержкой.")
    return сведения


def send(target: Target, samples: list[Sample],
         state: TargetState | None = None) -> str:
    """Отправляет снимок в один приёмник. Бросает исключение при неудаче.

    `state` — состояние приёмника в рассылке: StatsD и Zabbix помнят в нём,
    что уже отправлено. Без него (разовая проба) отправка ни на что
    прошлое не опирается и ничего не запоминает. Ответ — что сказал
    приёмник, если он что-то говорит (Zabbix), иначе пустая строка.
    """
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
        host, _, port = target.url.replace("udp://", "").partition(":")
        host = host or "127.0.0.1"
        port_number = int(port or 8125)
        # Семейство определяем по факту: приёмник может слушать IPv6.
        family = socket.AF_INET
        try:
            family = socket.getaddrinfo(host, port_number, type=socket.SOCK_DGRAM)[0][0]
        except (socket.gaierror, ValueError):
            pass

        строки, новые = _statsd_lines(target, samples,
                                      state.statsd_last if state is not None else None)
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(target.timeout_s)
            for line in строки:
                sock.sendto(line.encode("utf-8"), (host, port_number))
        # Запоминаем только после отправки: упавшая на полпути отправка не
        # должна съесть прирост, который до приёмника не дошёл.
        if state is not None:
            state.statsd_last = новые

    elif target.kind == "zabbix":
        # Наборы меток для обнаружения — когда изменились и раз в полчаса:
        # обработка правил обнаружения для Zabbix дорогая, а каждую минуту
        # слать одно и то же незачем. Проба шлёт их всегда.
        наборы = exporters.zabbix_discovery(samples)
        отпечаток = json.dumps(наборы, sort_keys=True, ensure_ascii=False)
        сейчас = time.time()
        с_обнаружением = (state is None or отпечаток != state.zabbix_lld
                          or сейчас - state.zabbix_lld_at > ZABBIX_LLD_ПОВТОР_С)
        сведения = _zabbix_send(target, exporters.zabbix_data(
            samples, target.host, discovery=с_обнаружением))
        if state is not None and с_обнаружением:
            state.zabbix_lld = отпечаток
            state.zabbix_lld_at = сейчас
        return сведения

    elif target.kind == "webhook":
        body = json.dumps(exporters.json_snapshot(samples, with_meta=False),
                          ensure_ascii=False).encode("utf-8")
        _post(target.url, body,
              {"Content-Type": "application/json; charset=utf-8", **target.headers},
              target.timeout_s)

    else:
        raise ValueError(f"Неизвестный приёмник: {target.kind}")
    return ""


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
        """Заменяет список приёмников.

        Приёмник, который остался тем же (имя, вид и адрес), сохраняет своё
        состояние: счётчики отправок, время последнего успеха, что уже
        ушло в StatsD и Zabbix. Раньше любое изменение списка — добавили
        соседний приёмник — обнуляло всё это у остальных, а StatsD после
        этого получал итог с запуска как прирост.
        """
        with self._lock:
            прежние = self._states
            состояния: dict[str, TargetState] = {}
            сроки: dict[str, float] = {}
            for t in targets:
                было = прежние.get(t.name)
                if было is not None and (было.target.kind, было.target.url) == (t.kind, t.url):
                    было.target = t
                    состояния[t.name] = было
                    сроки[t.name] = self._next_at.get(t.name, 0.0)
                else:
                    состояния[t.name] = TargetState(target=t)
                    сроки[t.name] = 0.0
            self._states = состояния
            self._next_at = сроки

    def target_list(self) -> list[Target]:
        """Сами приёмники — для сохранения в настройки."""
        with self._lock:
            return [s.target for s in self._states.values()]

    def targets(self) -> list[dict[str, Any]]:
        with self._lock:
            return [s.to_dict() for s in self._states.values()]

    def healthy_samples(self) -> list[Sample]:
        """Метрика о состоянии самих приёмников — мониторинг мониторинга.

        Приёмник, к которому ещё ни разу не ходили, в снимок не попадает.
        Раньше он попадал нулём — «последняя отправка не удалась», — и
        тревога «приёмников доступно ниже единицы» поднималась через
        пятнадцать минут после того, как приёмник ЗАВЕЛИ: до первой
        отправки, по свежей настройке, ни на чём. Самопроверка в том же
        месте отвечала «ok, последняя отправка никогда», и две части
        сервера говорили об одном и том же противоположное.

        Отсутствие метрики движок тревог понимает правильно: судить не о
        чем. Как только первая отправка состоится — удачно или нет, — в
        снимке появится честная единица или честный ноль.
        """
        with self._lock:
            return [Sample("asrhub_push_targets_healthy", 1.0 if s.healthy else 0.0,
                           {"target": s.target.name})
                    for s in self._states.values()
                    if s.target.enabled and s.last_attempt > 0]

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

    def push_once(self, target: Target, samples: list[Sample] | None = None,
                  *, проба: bool = False) -> dict[str, Any]:
        """Одна отправка. Используется и циклом, и кнопкой «проверить».

        `проба` — разовая проверка несохранённого приёмника: его состояние
        не заводится в общем списке. Раньше кнопка «Проверить» ставила
        проверяемый приёмник в работу: цикл слал туда по расписанию, а
        неудачная проверка через четверть часа поднимала тревогу — при том
        что справочник обещает «ничего не сохраняет».
        """
        payload = samples if samples is not None else self._collect()
        with self._lock:
            if проба:
                state = TargetState(target=target)
            else:
                state = self._states.get(target.name) or TargetState(target=target)
                self._states[target.name] = state
            state.last_attempt = time.time()
        try:
            сведения = send(target, payload, None if проба else state)
        except Exception as exc:                             # noqa: BLE001
            # Любое исключение, а не только сетевые: http.client.InvalidURL
            # (опечатка в порту) и BadStatusLine (приёмник отвечает не по
            # HTTP) пролетали мимо, поток отправки падал с трассой в stderr на
            # каждом интервале, а last_error оставался пустым.
            now = time.time()
            with self._lock:
                state.failed += 1
                state.last_error = _без_секретов(f"{type(exc).__name__}: {exc}", target)
                # Первая неудача — вслух, дальше не чаще раза в час. Само
                # состояние никуда не девается: оно целиком видно в
                # /api/monitoring/push и в разделе наблюдения.
                complain = now - state.last_complaint > COMPLAIN_INTERVAL_S
                if complain:
                    state.last_complaint = now
                    failures = state.failed
            if complain:
                log.warning("Не удалось отправить метрики в «%s»: %s", target.name,
                            state.last_error)
                if failures > 1:
                    log.warning("Это %s-я неудача подряд; следующая жалоба — не раньше "
                                "чем через час. Состояние: /api/monitoring/push",
                                failures)
            return {"ok": False, "error": state.last_error}
        with self._lock:
            if state.failed and state.last_complaint:
                log.info("Отправка метрик в «%s» восстановилась после %s неудач",
                         target.name, state.failed)
            state.sent += 1
            state.last_success = time.time()
            state.last_error = ""
            state.last_complaint = 0.0
            state.last_info = _без_секретов(сведения or "", target)
        итог: dict[str, Any] = {"ok": True, "sent_metrics": len(payload)}
        if сведения:
            итог["info"] = state.last_info
        return итог
