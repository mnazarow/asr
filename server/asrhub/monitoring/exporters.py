"""Выгрузка снимка метрик в форматы систем мониторинга.

Один снимок — семь представлений. Каждая функция принимает список `Sample`
и возвращает готовый к отправке текст; ни одна из них не обращается к
серверу, поэтому их легко проверять и переиспользовать.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .catalog import GROUPS_BY_ID, METRICS, METRICS_BY_NAME, MetricSpec
from .collector import Sample

# Prometheus запрещает в значении метки перевод строки, кавычку и обратную
# косую; экранируем ровно эти три знака.
_ESCAPE = str.maketrans({"\\": r"\\", '"': r"\"", "\n": r"\n"})
_INVALID_NAME = re.compile(r"[^a-zA-Z0-9_:]")


def _labels(labels: dict[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{_INVALID_NAME.sub("_", k)}="{str(v).translate(_ESCAPE)}"'
                     for k, v in sorted(labels.items()))
    return "{" + inner + "}"


def _base_name(name: str) -> str:
    for suffix in ("_bucket", "_sum", "_count"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _format(value: float) -> str:
    if value != value:                                  # NaN
        return "NaN"
    if value in (float("inf"), float("-inf")):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return repr(round(float(value), 6))


def prometheus(samples: list[Sample], *, openmetrics: bool = False) -> str:
    """Текстовый формат Prometheus (или OpenMetrics при openmetrics=True).

    HELP и TYPE берутся из каталога метрик, поэтому в Grafana и в alertmanager
    видно то же описание, что и в документации.
    """
    lines: list[str] = []
    seen: set[str] = set()

    ordered: dict[str, list[Sample]] = {}
    for sample in samples:
        ordered.setdefault(_base_name(sample.name), []).append(sample)

    for base, items in ordered.items():
        spec = METRICS_BY_NAME.get(base)
        if base not in seen:
            seen.add(base)
            if spec:
                help_text = " ".join(spec.description.split())
                if spec.unit:
                    help_text += f" [{spec.unit}]"
                kind = "gauge" if spec.type == "info" else spec.type
                # В OpenMetrics имя семейства счётчика идёт БЕЗ суффикса
                # _total — его несут только измерения. Пока имя объявлялось
                # целиком, эталонный разборщик отвергал весь снимок целиком
                # («Clashing name»), а не одну метрику: пропадали все
                # семейства разом, включая asrhub_up, и авария выглядела как
                # падение сервиса.
                family = base
                if openmetrics and kind == "counter" and family.endswith("_total"):
                    family = family[: -len("_total")]
                lines.append(f"# HELP {family} {help_text}")
                lines.append(f"# TYPE {family} {kind}")
        for sample in items:
            lines.append(f"{sample.name}{_labels(sample.labels)} {_format(sample.value)}")

    if openmetrics:
        lines.append("# EOF")
    return "\n".join(lines) + "\n"


def json_snapshot(samples: list[Sample], errors: list[str] | None = None,
                  *, with_meta: bool = True) -> dict[str, Any]:
    """Снимок в JSON — для систем, которые не понимают формат Prometheus.

    При with_meta к каждой метрике прикладывается её описание, рекомендация и
    пороги: получатель видит не только число, но и что оно значит.
    """
    from .collector import describe

    payload: dict[str, Any] = {
        "timestamp": time.time(),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "metrics": describe(samples) if with_meta else
                   [{"name": s.name, "labels": s.labels, "value": s.value} for s in samples],
    }
    if errors:
        payload["collection_errors"] = errors
    return payload


def _influx_tag(value: str) -> str:
    """Influx требует экранировать пробел, запятую и знак равенства в тегах."""
    text = str(value)
    for char in (",", " ", "="):
        text = text.replace(char, "\\" + char)
    return text


def influx_line(samples: list[Sample], *, measurement_prefix: str = "") -> str:
    """Line protocol InfluxDB / Telegraf.

    Имя метрики становится названием измерения, метки — тегами. Пустые теги
    Influx не принимает, поэтому они отбрасываются.
    """
    stamp = int(time.time() * 1_000_000_000)
    lines = []
    for sample in samples:
        name = measurement_prefix + sample.name
        tags = "".join(f",{k}={_influx_tag(v)}"
                       for k, v in sorted(sample.labels.items()) if v != "")
        lines.append(f"{name}{tags} value={_format(sample.value)} {stamp}")
    return "\n".join(lines) + "\n"


def graphite(samples: list[Sample], *, prefix: str = "asrhub") -> str:
    """Формат Graphite и StatsD: точка в имени вместо меток."""
    stamp = int(time.time())
    lines = []
    for sample in samples:
        parts = [prefix, sample.name.replace("asrhub_", "")]
        parts += [str(v).replace(".", "_").replace(" ", "_")
                  for _, v in sorted(sample.labels.items()) if v != ""]
        lines.append(f"{'.'.join(parts)} {_format(sample.value)} {stamp}")
    return "\n".join(lines) + "\n"


def zabbix_sender(samples: list[Sample], host: str) -> str:
    """JSON для zabbix_sender: список пар «ключ — значение» с именем узла.

    Отправляется ровно то, что шаблон умеет принять. Гистограммы и
    устаревшие псевдонимы имён пропускаются: в Zabbix нет понятия корзины,
    и раньше сотни точек вида `asrhub_job_duration_seconds_bucket[600]`
    отбивались как «unsupported item key», забивая журнал сервера Zabbix.
    """
    data = []
    for sample in samples:
        base = _base_name(sample.name)
        spec = METRICS_BY_NAME.get(base)
        if spec is not None and (spec.type in ("histogram", "info")
                                 or spec.deprecated_for):
            continue
        if sample.name != base:                 # _bucket, _sum, _count
            continue
        key = sample.name
        if sample.labels:
            args = ",".join(str(v) for _, v in sorted(sample.labels.items()))
            key = f"{sample.name}[{args}]"
        data.append({"host": host, "key": key, "value": _format(sample.value)})
    return json.dumps({"request": "sender data", "data": data},
                      ensure_ascii=False, indent=1)


def csv_table(samples: list[Sample]) -> str:
    """Плоская таблица: имя, метки, значение, единица, группа."""
    rows = ["metric,labels,value,unit,group"]
    for sample in samples:
        spec = METRICS_BY_NAME.get(_base_name(sample.name))
        labels = ";".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
        rows.append(f'{sample.name},"{labels}",{_format(sample.value)},'
                    f'{spec.unit if spec else ""},{spec.group if spec else ""}')
    return "\n".join(rows) + "\n"


def otlp_payload(samples: list[Sample], service_name: str = "asrhub",
                 service_version: str = "3.0.0") -> dict[str, Any]:
    """Тело запроса OTLP/HTTP для OpenTelemetry Collector.

    Собирается вручную, без пакета opentelemetry: формат стабилен, а лишняя
    зависимость на сервере распознавания не нужна.
    """
    stamp = int(time.time() * 1_000_000_000)
    metrics = []
    for sample in samples:
        spec = METRICS_BY_NAME.get(_base_name(sample.name))
        point = {
            "asDouble": float(sample.value),
            "timeUnixNano": str(stamp),
            "startTimeUnixNano": str(stamp),
            "attributes": [{"key": k, "value": {"stringValue": str(v)}}
                           for k, v in sorted(sample.labels.items())],
        }
        body: dict[str, Any] = {
            "name": sample.name,
            "unit": spec.unit if spec else "",
            "description": " ".join(spec.description.split()) if spec else "",
        }
        if spec and spec.type == "counter":
            body["sum"] = {"dataPoints": [point], "aggregationTemporality": 2,
                           "isMonotonic": True}
        else:
            body["gauge"] = {"dataPoints": [point]}
        metrics.append(body)

    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}},
                {"key": "service.version", "value": {"stringValue": service_version}},
            ]},
            "scopeMetrics": [{"scope": {"name": "asrhub.monitoring"}, "metrics": metrics}],
        }]
    }


# ---------------------------------------------------------------------------
# Готовые конфигурации для внешних систем
# ---------------------------------------------------------------------------

def prometheus_rules() -> str:
    """Правила оповещения Prometheus, собранные из порогов каталога.

    Отдаются как готовый YAML: скопировать в rules.yml и перезагрузить
    Prometheus. Пороги здесь — отправная точка, а не истина: подгонять их
    под свой поток всё равно придётся.
    """
    lines = ["# Правила оповещения ASR Hub.",
             "# Сгенерированы из каталога метрик; пороги — отправная точка,",
             "# подгоняйте под свою нагрузку.",
             "groups:",
             "  - name: asrhub",
             "    rules:",
             "      - alert: ASRHubDown",
             "        expr: absent(asrhub_up) == 1",
             "        for: 5m",
             "        labels: { severity: critical }",
             "        annotations:",
             "          summary: 'Сервис распознавания не отвечает'",
             "          description: 'Метрики не собираются пять минут подряд.'",
             ]

    for spec in METRICS:
        threshold = spec.threshold
        if not threshold or spec.name == "asrhub_up" or spec.deprecated_for:
            continue
        seen_values: set[float] = set()
        for level, value in (("critical", threshold.critical), ("warning", threshold.warning)):
            if value is None or value in seen_values:
                continue                    # одинаковые пороги дают дублирующие правила
            seen_values.add(value)
            lines += [
                f"      - alert: {_alert_name(spec, level)}",
                f"        expr: {_rule_expression(spec, threshold.direction, value)}",
                f"        for: {max(60, threshold.for_seconds)}s",
                f"        labels: {{ severity: {level} }}",
                "        annotations:",
                f"          summary: {_yaml_str(_summary(spec, threshold.direction, value))}",
                "          description: " + _yaml_str(
                    (spec.troubleshooting or spec.recommendation)[:220]),
            ]
            if threshold.note:
                lines.append("          note: " + _yaml_str(threshold.note))
    return "\n".join(lines) + "\n"


def _alert_name(spec: MetricSpec, level: str) -> str:
    """Имя правила: только латиница и цифры — этого требует Prometheus.

    Собирается из имени метрики, а не из русской подписи: подпись читается
    человеком в summary, а имя должно быть машинным и стабильным.
    """
    core = "".join(part.capitalize() for part in
                   spec.name.replace("asrhub_", "").split("_"))
    return f"ASRHub{core}{level.capitalize()}"


def _yaml_str(text: str) -> str:
    """Скалярная строка YAML в одинарных кавычках.

    Значения аннотаций содержат двоеточия («Заданий ждёт: выше 200») и без
    кавычек ломают разбор файла.
    """
    return "'" + " ".join(str(text).split()).replace("'", "''") + "'"


def _summary_word(direction: str) -> str:
    return "выше" if direction == "above" else "ниже"


def _summary(spec: MetricSpec, direction: str, value: float) -> str:
    """Человеческая формулировка тревоги — она попадает дежурному в уведомление."""
    special = {
        "asrhub_uptime_seconds": "Служба перезапускалась более двух раз за час",
        "asrhub_queue_paused": "Очередь остаётся на паузе",
        "asrhub_jobs_total": f"Доля неудачных заданий выше {value:.0%}",
        "asrhub_http_requests_total": f"Доля ответов 5xx выше {value:.0%}",
        "asrhub_ram_used_bytes": f"Оперативная память занята более чем на {value:.0f} %",
        "asrhub_gpu_memory_used_bytes": f"Видеопамять занята более чем на {value:.0f} %",
    }
    if spec.name in special:
        return special[spec.name]
    unit = f" {spec.unit}" if spec.unit else ""
    return f"{spec.label}: {_summary_word(direction)} {value}{unit}"


def _rule_expression(spec: MetricSpec, direction: str, value: float) -> str:
    """Собирает выражение правила с учётом особенностей конкретной метрики."""
    operator = ">" if direction == "above" else "<"

    # Метрики, значение которых 0 или 1: строгое сравнение с единицей никогда
    # не сработает, поэтому проверяем равенство.
    if spec.name in {"asrhub_queue_paused"}:
        return f"{spec.name} == 1"

    # Метрики, для которых порог задан в долях или в приросте: простое
    # сравнение бессмысленно, нужны функции Prometheus.
    computed = {
        "asrhub_jobs_total": (
            'sum(rate(asrhub_jobs_total{status="failed"}[30m])) '
            "/ clamp_min(sum(rate(asrhub_jobs_total[30m])), 0.001)"),
        "asrhub_http_requests_total": (
            'sum(rate(asrhub_http_requests_total{status=~"5.."}[5m])) '
            "/ clamp_min(sum(rate(asrhub_http_requests_total[5m])), 0.001)"),
        "asrhub_ram_used_bytes": (
            "asrhub_ram_used_bytes / clamp_min(asrhub_ram_total_bytes, 1) * 100"),
        "asrhub_gpu_memory_used_bytes": (
            "asrhub_gpu_memory_used_bytes / clamp_min(asrhub_gpu_memory_total_bytes, 1) * 100"),
        "asrhub_rtf": 'asrhub_rtf{stat="p95"}',
        "asrhub_queue_wait_seconds": 'asrhub_queue_wait_seconds{stat="p95"}',
        "asrhub_confidence": 'asrhub_confidence{stat="avg"}',
        "asrhub_no_speech_total": "increase(asrhub_no_speech_total[30m])",
        "asrhub_auth_failures_total": "increase(asrhub_auth_failures_total[10m])",
        "asrhub_rate_limited_total": "increase(asrhub_rate_limited_total[10m])",
        "asrhub_webhooks_total": 'increase(asrhub_webhooks_total{result="failed"}[30m])',
        # Обнуление счётчика времени работы означает недавний перезапуск;
        # ловим именно факт падения, а не малое значение при первом старте.
        "asrhub_uptime_seconds": "resets(asrhub_uptime_seconds[1h])",
    }
    if spec.name == "asrhub_uptime_seconds":
        # Именно resets(): changes() растёт на каждом опросе, потому что
        # время работы меняется всегда, и правило срабатывало бы постоянно.
        return "resets(asrhub_uptime_seconds[1h]) > 2"

    return f"{computed.get(spec.name, spec.name)} {operator} {value}"


def grafana_dashboard(title: str = "ASR Hub") -> dict[str, Any]:
    """Готовая панель Grafana, собранная по группам каталога.

    Строится программно, чтобы не расходиться с набором метрик: добавили
    метрику в каталог — она появилась на панели своей группы.
    """
    panels: list[dict[str, Any]] = []
    y = 0

    panels.append({
        "type": "stat", "title": "Состояние", "gridPos": {"h": 4, "w": 24, "x": 0, "y": y},
        "targets": [
            {"expr": "asrhub_up", "legendFormat": "доступен", "refId": "A"},
            {"expr": "asrhub_queue_depth", "legendFormat": "в очереди", "refId": "B"},
            {"expr": "asrhub_active_jobs", "legendFormat": "выполняется", "refId": "C"},
            {"expr": "asrhub_engines_available", "legendFormat": "движков", "refId": "D"},
            {"expr": "asrhub_disk_free_bytes", "legendFormat": "свободно на диске", "refId": "E"},
        ],
    })
    y += 4

    for group in ("queue", "performance", "quality", "resources", "errors", "api"):
        specs = [m for m in METRICS
                 if m.group == group and m.type != "info" and not m.deprecated_for][:6]
        if not specs:
            continue
        targets = []
        for index, spec in enumerate(specs):
            expr = spec.name
            if spec.type == "counter":
                expr = f"rate({spec.name}[5m])"
            elif spec.type == "histogram":
                expr = (f"histogram_quantile(0.95, "
                        f"sum by (le) (rate({spec.name}_bucket[5m])))")
            targets.append({"expr": expr, "legendFormat": spec.label,
                            "refId": chr(ord("A") + index)})
        panels.append({
            "type": "timeseries",
            "title": GROUPS_BY_ID[group]["title"],
            "description": GROUPS_BY_ID[group]["description"],
            "gridPos": {"h": 8, "w": 12, "x": 0 if len(panels) % 2 else 12, "y": y},
            "targets": targets,
        })
        if len(panels) % 2 == 0:
            y += 8

    return {
        "title": title,
        "uid": "asrhub-main",
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "tags": ["asrhub", "asr"],
        "panels": panels,
        "templating": {"list": [{
            "name": "instance", "type": "query", "datasource": "Prometheus",
            "query": "label_values(asrhub_up, instance)", "includeAll": True,
        }]},
    }


def _stable_uuid(seed: str) -> str:
    """Ровно 32 шестнадцатеричных знака, одинаковых от запуска к запуску.

    Zabbix различает объекты по uuid: нестабильное значение приводит к тому,
    что повторный импорт создаёт дубликаты вместо обновления существующих.
    """
    import hashlib

    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:32]


#: Метрики, у которых набор значений меток известен заранее. Для них можно
#: объявить обычные элементы; всё остальное уходит в правило обнаружения.
#: Срезы совпадают с тем, что отдаёт сборщик (collector.summarize).
_STATS = [("avg",), ("p50",), ("p90",), ("p95",), ("p99",)]

_KNOWN_LABEL_VALUES: dict[str, list[tuple[str, ...]]] = {
    "asrhub_jobs_by_status": [("queued",), ("running",), ("completed",),
                              ("failed",), ("cancelled",), ("retry",), ("paused",)],
    "asrhub_rtf": _STATS,
    "asrhub_queue_wait_seconds": _STATS,
    "asrhub_confidence": _STATS,
    "asrhub_wer": _STATS,
    "asrhub_job_duration_seconds": _STATS,
    "asrhub_media_duration_seconds": _STATS,
}

#: Для метрик со срезами (avg/p50/p95) порог из каталога относится к
#: одному конкретному срезу — тому же, что и в правилах Prometheus.
#: Без этого один порог заводил три одинаковых триггера, и дежурный получал
#: три письма про одно и то же.
_TRIGGER_STAT: dict[str, str] = {
    "asrhub_rtf": "p95",
    "asrhub_queue_wait_seconds": "p95",
    "asrhub_confidence": "avg",
}

#: Выражения триггеров для метрик, чей порог задан не в единицах метрики.
#: None означает, что осмысленного триггера в терминах Zabbix нет и
#: выпускать его не нужно — лучше ни одного, чем заведомо ложный.
_ZABBIX_EXPRESSIONS: dict[str, str | None] = {
    # Порог в процентах от общего объёма, а метрика — в байтах.
    "asrhub_ram_used_bytes":
        "last(/ASR Hub/asrhub_ram_used_bytes)"
        "/last(/ASR Hub/asrhub_ram_total_bytes)*100{op}{value}",
    "asrhub_gpu_memory_used_bytes": None,      # метка gpu уходит в обнаружение
    # Порог — доля неудач, метрика — накопительный счётчик.
    "asrhub_jobs_total": None,
    "asrhub_http_requests_total": None,
    "asrhub_webhooks_total": None,
    "asrhub_model_success_rate": None,
    # Порог — прирост за окно, метрика — накопительный счётчик.
    "asrhub_no_speech_total":
        "(last(/ASR Hub/asrhub_no_speech_total)"
        "-last(/ASR Hub/asrhub_no_speech_total,#1:now-30m)){op}{value}",
    "asrhub_auth_failures_total":
        "(last(/ASR Hub/asrhub_auth_failures_total)"
        "-last(/ASR Hub/asrhub_auth_failures_total,#1:now-10m)){op}{value}",
    "asrhub_rate_limited_total":
        "(last(/ASR Hub/asrhub_rate_limited_total)"
        "-last(/ASR Hub/asrhub_rate_limited_total,#1:now-10m)){op}{value}",
    # Метрика существует, только когда равна единице: сравнивать бессмысленно,
    # недоступность ловится отсутствием данных.
    "asrhub_up": "nodata(/ASR Hub/asrhub_up,5m)=1",
    # Падение видно по обнулению счётчика, а не по малому значению.
    "asrhub_uptime_seconds":
        "last(/ASR Hub/asrhub_uptime_seconds)<last(/ASR Hub/asrhub_uptime_seconds,#2)",
    "asrhub_queue_paused": "last(/ASR Hub/asrhub_queue_paused)=1",
}


def _zabbix_item(spec: MetricSpec, key: str, label: str) -> list[str]:
    return [
        f"        - uuid: {_stable_uuid(key)}",
        f"          name: {_yaml_str(label)}",
        "          type: TRAP",
        f"          key: {key}",
        "          value_type: FLOAT",
        f"          units: {_yaml_str(spec.unit)}",
        f"          description: {_yaml_str(spec.description[:250])}",
    ]


def _zabbix_trigger(spec: MetricSpec, key: str, label: str) -> list[str]:
    if spec.name in _ZABBIX_EXPRESSIONS:
        template = _ZABBIX_EXPRESSIONS[spec.name]
        if template is None:
            return []
        if "{op}" in template:
            if not spec.threshold or spec.threshold.critical is None:
                return []
            operator = ">" if spec.threshold.direction == "above" else "<"
            expression = template.format(op=operator, value=spec.threshold.critical)
        else:
            expression = template
    else:
        if not spec.threshold or spec.threshold.critical is None:
            return []
        operator = ">" if spec.threshold.direction == "above" else "<"
        expression = f"last(/ASR Hub/{key}){operator}{spec.threshold.critical}"
    return [
        "          triggers:",
        f"            - uuid: {_stable_uuid(key + ':trigger')}",
        f"              expression: {_yaml_str(expression)}",
        f"              name: {_yaml_str(label + ': порог превышен')}",
        "              priority: HIGH",
    ]


def zabbix_template() -> str:
    """Шаблон Zabbix 6+ в формате YAML.

    Две вещи, из-за которых прежний шаблон не работал.

    **Ключи.** zabbix_sender отправляет метрику с метками как «имя[значения]»,
    а шаблон объявлял «имя». Zabbix сопоставляет trapper-элементы по точному
    ключу, поэтому доезжало около пяти процентов данных, остальное отбивалось
    как «unsupported item key»: ни очереди по состояниям, ни RTF, ни
    уверенности, ни доступности движков. Теперь элемент объявляется ровно тем
    ключом, каким метрика приходит, а метрики с заранее неизвестным составом
    меток (модель, маршрут, код ошибки) собираются правилом обнаружения.

    **Триггеры.** Порог из каталога подставлялся в сравнение сырого значения,
    но у части метрик он задан в процентах, в долях или в приросте.
    Получалось «last(asrhub_ram_used_bytes)>95» — авария при 95 байтах
    занятой памяти, горящая всегда, — и «last(asrhub_up)<1», не срабатывающий
    никогда, потому что метрика существует только когда равна единице.
    """
    lines = [
        "# Шаблон Zabbix для ASR Hub.",
        "# Импорт: Настройка -> Шаблоны -> Импорт.",
        "# Данные приходят zabbix_sender'ом: см. /api/monitoring/metrics?format=zabbix",
        "zabbix_export:",
        "  version: '6.0'",
        "  templates:",
        f"    - uuid: {_stable_uuid('asrhub-template')}",
        "      template: 'ASR Hub'",
        "      name: 'ASR Hub — распознавание речи'",
        "      groups:",
        "        - name: Applications",
        "      items:",
    ]

    discovery: list[MetricSpec] = []
    for spec in METRICS:
        if spec.type in ("info", "histogram") or spec.deprecated_for:
            continue
        if spec.labels:
            known = _KNOWN_LABEL_VALUES.get(spec.name)
            if known is None:
                discovery.append(spec)
                continue
            wanted = _TRIGGER_STAT.get(spec.name)
            for combo in known:
                key = f"{spec.name}[{','.join(combo)}]"
                label = f"{spec.label} ({' '.join(combo)})"
                lines += _zabbix_item(spec, key, label)
                if wanted is None or combo[0] == wanted:
                    lines += _zabbix_trigger(spec, key, label)
            continue
        lines += _zabbix_item(spec, spec.name, spec.label)
        lines += _zabbix_trigger(spec, spec.name, spec.label)

    if discovery:
        lines += [
            "      discovery_rules:",
            f"        - uuid: {_stable_uuid('asrhub-discovery')}",
            "          name: 'Метрики с произвольными метками'",
            "          type: TRAP",
            "          key: asrhub.discovery",
            "          description: 'Метрики, состав меток у которых заранее "
            "не известен: имя модели, маршрут, код ошибки.'",
            "          item_prototypes:",
        ]
        for spec in discovery:
            args = ",".join("{#" + label.upper() + "}" for label in spec.labels)
            lines += [
                f"            - uuid: {_stable_uuid(spec.name + ':proto')}",
                f"              name: {_yaml_str(spec.label + ' [' + ', '.join(spec.labels) + ']')}",
                "              type: TRAP",
                f"              key: {spec.name}[{args}]",
                "              value_type: FLOAT",
                f"              units: {_yaml_str(spec.unit)}",
                f"              description: {_yaml_str(spec.description[:250])}",
            ]
    return "\n".join(lines) + "\n"
