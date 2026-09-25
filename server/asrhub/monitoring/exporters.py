"""Выгрузка снимка метрик в форматы систем мониторинга.

Один снимок — семь представлений. Каждая функция принимает список `Sample`
и возвращает готовый к отправке текст; ни одна из них не обращается к
серверу, поэтому их легко проверять и переиспользовать.
"""
from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any

from .. import __version__
from .catalog import (
    GROUPS_BY_ID,
    METRICS,
    METRICS_BY_NAME,
    MetricSpec,
    Threshold,
    оператор_порога,
    порог,
    слово_порога,
)
from .collector import RUNTIME, СТАДИИ, Collector, Sample

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
                  *, with_meta: bool = True, settings: Any = None) -> dict[str, Any]:
    """Снимок в JSON — для систем, которые не понимают формат Prometheus.

    При with_meta к каждой метрике прикладывается её описание, рекомендация и
    пороги: получатель видит не только число, но и что оно значит. Пороги —
    действующие на этом сервере (`settings`).
    """
    from .collector import describe

    payload: dict[str, Any] = {
        "timestamp": time.time(),
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "metrics": describe(samples, settings) if with_meta else
                   [{"name": s.name, "labels": s.labels, "value": s.value} for s in samples],
    }
    if errors:
        payload["collection_errors"] = errors
    return payload


def _influx_tag(value: str) -> str:
    """Значение метки для Influx: экранируем спецзнаки, убираем переводы строк.

    Перевод строки в метке — это не опечатка, а подлог: строка в line
    protocol заканчивается переводом строки, и всё после него читается как
    новое измерение. Значение метки `model` приходит из настроек задания и
    проверку каталога проходит любое (`model` объявлен enum без списка
    допустимых), так что ключ с ролью `user` мог одним заданием дописать в
    InfluxDB подложную точку — например, «свободного места на диске ноль».
    То же и с обратной косой в конце: она экранировала бы наш собственный
    разделитель.
    """
    text = str(value).replace("\\", "\\\\")
    for знак in ("\r", "\n", "\t", "\x00"):
        text = text.replace(знак, " ")
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


def _графит_часть(value: str) -> str:
    """Часть имени метрики Graphite: только безопасные знаки."""
    очищено = re.sub(r"[^0-9A-Za-z_\-]+", "_", value).strip("_")
    return очищено or "нет"


def graphite(samples: list[Sample], *, prefix: str = "asrhub") -> str:
    """Формат Graphite и StatsD: точка в имени вместо меток."""
    stamp = int(time.time())
    lines = []
    for sample in samples:
        parts = [prefix, sample.name.replace("asrhub_", "")]
        # В имени метрики Graphite разделители — точка и перевод строки.
        # Оставляем только буквы, цифры, дефис и подчёркивание: значение
        # метки приходит из настроек задания, а туда попадает что угодно.
        parts += [_графит_часть(str(v))
                  for _, v in sorted(sample.labels.items()) if v != ""]
        lines.append(f"{'.'.join(parts)} {_format(sample.value)} {stamp}")
    return "\n".join(lines) + "\n"


def _zabbix_param(value: str) -> str:
    """Параметр ключа Zabbix для метрики с обнаружением — всегда в кавычках.

    В кавычках — потому что значения меток обнаруживаемых метрик заранее
    неизвестны: категория «Жалоба, претензия» без кавычек разваливала ключ на
    два параметра. Прототип элемента в шаблоне объявлен так же — в кавычках,
    и Zabbix, подставляя макрос в параметр в кавычках, экранирует кавычки
    внутри значения тем же способом, что и здесь.
    """
    return '"' + str(value).replace('"', '\\"') + '"'


def _zabbix_values(sample: Sample, spec: MetricSpec | None) -> list[str]:
    """Значения меток в порядке их объявления в каталоге.

    zabbix_sender сортировал метки по имени, а прототипы в шаблоне шли в
    порядке каталога: `asrhub_jobs_by_model[demo,demo-simulator]` против
    `[{#MODEL},{#ENGINE}]` — у метрик с двумя метками ни одно значение не
    совпадало с элементом. Метки, которых каталог не знает, идут следом по
    алфавиту: ключ должен быть одинаковым от снимка к снимку.
    """
    метки = dict(sample.labels)
    порядок = [имя for имя in (spec.labels if spec else ()) if имя in метки]
    return [str(метки.pop(имя)) for имя in порядок] + [str(метки[k]) for k in sorted(метки)]


def _zabbix_key(name: str, values: list[str]) -> str:
    """Ключ элемента: как объявлен в шаблоне — см. `zabbix_template`."""
    if not values:
        return name
    if name in _KNOWN_LABEL_VALUES:
        return f"{name}[{','.join(values)}]"
    return f"{name}[{','.join(_zabbix_param(v) for v in values)}]"


def _zabbix_sendable(spec: MetricSpec | None, sample: Sample) -> bool:
    """Что из снимка вообще уходит в Zabbix.

    Гистограммы и устаревшие псевдонимы имён пропускаются: в Zabbix нет
    понятия корзины, и раньше сотни точек вида
    `asrhub_job_duration_seconds_bucket[600]` отбивались как «unsupported
    item key», забивая журнал сервера Zabbix. Метрик, которых нет в
    каталоге, в шаблоне нет тоже — отправлять их некуда.
    """
    if spec is None or spec.type in ("histogram", "info") or spec.deprecated_for:
        return False
    return sample.name == spec.name


def zabbix_discovery(samples: list[Sample]) -> dict[str, list[dict[str, str]]]:
    """Данные низкоуровневого обнаружения: по правилу на каждую метрику.

    Правило обнаружения в шаблоне было, но его никто не наполнял, и
    прототипы не превращались в элементы: из ста с лишним отправленных
    ключей шаблону соответствовали шестьдесят — видеокарты, доступность
    движков, доля успеха по моделям и запросы к API отбивались. Теперь
    отправка несёт и сами наборы меток: `asrhub.discovery[<метрика>]` со
    списком `{"{#MODEL}": "…", …}` — Zabbix создаёт по ним элементы и
    триггеры, а следующая отправка значений в них уже попадает.
    """
    наборы: dict[str, list[dict[str, str]]] = {}
    видели: set[tuple[str, tuple[str, ...]]] = set()
    for sample in samples:
        spec = METRICS_BY_NAME.get(sample.name)
        if not _zabbix_sendable(spec, sample) or not spec.labels \
                or spec.name in _KNOWN_LABEL_VALUES:
            continue
        значения = _zabbix_values(sample, spec)
        ключ = (spec.name, tuple(значения))
        if ключ in видели:
            continue
        видели.add(ключ)
        наборы.setdefault(spec.name, []).append(
            {"{#" + метка.upper() + "}": значение
             for метка, значение in zip(spec.labels, значения, strict=False)})
    return наборы


def zabbix_data(samples: list[Sample], host: str, *,
                discovery: bool = True) -> list[dict[str, Any]]:
    """Пары «узел — ключ — значение» для протокола траппера Zabbix."""
    data: list[dict[str, Any]] = []
    if discovery:
        for имя, строки in zabbix_discovery(samples).items():
            data.append({"host": host, "key": f"asrhub.discovery[{имя}]",
                         "value": json.dumps(строки, ensure_ascii=False,
                                             separators=(",", ":"))})
    for sample in samples:
        spec = METRICS_BY_NAME.get(sample.name)
        if not _zabbix_sendable(spec, sample):
            continue
        key = _zabbix_key(sample.name, _zabbix_values(sample, spec))
        data.append({"host": host, "key": key, "value": _format(sample.value)})
    return data


def zabbix_sender(samples: list[Sample], host: str) -> str:
    """Тело запроса траппера Zabbix: `{"request": "sender data", "data": […]}`.

    Это то, что `zabbix_sender` кладёт в пакет ZBXD, — для своих программ
    и для отладки. Отправляет данные сам сервер (приёмник `kind: zabbix`),
    а для cron рядом есть построчный формат `zabbix_sender_lines`.
    """
    return json.dumps({"request": "sender data", "data": zabbix_data(samples, host)},
                      ensure_ascii=False, indent=1)


def _zabbix_line_field(value: str) -> str:
    """Поле строки для `zabbix_sender -i`: в кавычках, если в нём пробел."""
    text = str(value).replace("\r", " ").replace("\n", " ")
    if text and not any(ch in text for ch in ' \t"\\'):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def zabbix_sender_lines(samples: list[Sample], host: str) -> str:
    """Входной файл `zabbix_sender -i -`: строка «узел ключ значение».

    Прежде документация предлагала забирать «JSON для zabbix_sender», но
    zabbix_sender такого на вход не принимает — он читает строки. Теперь
    так: `curl …?format=zabbix_sender&host=asr-01 | zabbix_sender -z
    zabbix -i -` по расписанию, если серверу нельзя ходить в Zabbix самому.
    """
    строки = [" ".join(_zabbix_line_field(поле) for поле in
                       (элемент["host"], элемент["key"], элемент["value"]))
              for элемент in zabbix_data(samples, host)]
    return "\n".join(строки) + ("\n" if строки else "")


def csv_table(samples: list[Sample]) -> str:
    """Плоская таблица: имя, метки, значение, единица, группа."""
    # Собираем настоящим csv, а не склейкой строк. Значение метки приходит
    # из настроек задания, и кавычка внутри него разваливала разбор всей
    # таблицы: поле `model` объявлено enum без списка допустимых, так что
    # положить туда можно что угодно.
    буфер = io.StringIO()
    писарь = csv.writer(буфер, lineterminator="\n")
    писарь.writerow(["metric", "labels", "value", "unit", "group"])
    for sample in samples:
        spec = METRICS_BY_NAME.get(_base_name(sample.name))
        labels = ";".join(f"{k}={v}" for k, v in sorted(sample.labels.items()))
        писарь.writerow([sample.name, labels, _format(sample.value),
                         spec.unit if spec else "", spec.group if spec else ""])
    return буфер.getvalue()


#: Единицы каталога в записи UCUM — её ждёт OpenTelemetry. «с» и «Б» из
#: каталога сборщик телеметрии не понимал: единица либо терялась, либо
#: уезжала в имя метрики при переводе в Prometheus.
_UCUM = {"с": "s", "Б": "By", "МБ": "MBy", "ГБ": "GBy", "%": "%", "°C": "Cel",
         "Вт": "W", "дБ": "dB", "unixtime": "s", "ч/ч": "1", "слов/мин": "{word}/min",
         "из 100": "1", "от −100 до +100": "1"}


def _otlp_attributes(labels: dict[str, str]) -> list[dict[str, Any]]:
    return [{"key": k, "value": {"stringValue": str(v)}} for k, v in sorted(labels.items())]


def _otlp_histogram_point(labels: dict[str, str], buckets: dict[str, float], total: float,
                          summa: float, start: int, stamp: int) -> dict[str, Any]:
    """Точка гистограммы OTLP из накопленных корзин Prometheus.

    Prometheus хранит «не больше le» нарастающим итогом, OTLP — счётчик в
    каждой корзине и отдельно корзину выше последней границы.
    """
    границы = sorted((float(le), значение) for le, значение in buckets.items()
                     if le not in ("+Inf", "inf", "Inf"))
    корзины: list[int] = []
    было = 0.0
    for _, нарастающий in границы:
        корзины.append(int(round(max(0.0, нарастающий - было))))
        было = нарастающий
    корзины.append(int(round(max(0.0, total - было))))
    return {
        "startTimeUnixNano": str(start), "timeUnixNano": str(stamp),
        "count": str(int(round(total))), "sum": float(summa),
        "bucketCounts": [str(к) for к in корзины],
        "explicitBounds": [граница for граница, _ in границы],
        "attributes": _otlp_attributes(labels),
    }


def otlp_payload(samples: list[Sample], service_name: str = "asrhub",
                 service_version: str = __version__, *,
                 started_at: float | None = None) -> dict[str, Any]:
    """Тело запроса OTLP/HTTP для OpenTelemetry Collector.

    Собирается вручную, без пакета opentelemetry: формат стабилен, а лишняя
    зависимость на сервере распознавания не нужна.

    Три вещи сделаны так, как их ждёт сборщик. У накопительных сумм начало
    отсчёта — запуск процесса: раньше им ставилось время самой отправки, и
    каждая точка объявляла себя новым началом — `cumulativetodelta` и
    перевод в Prometheus видели сброс на каждом интервале. Точки одной
    метрики идут одной метрикой, а не десятком одноимённых. Гистограмма —
    гистограммой: прежде каждая корзина уходила отдельным мгновенным
    значением с меткой le, и в сборщике получалось двенадцать бессмысленных
    рядов вместо одного распределения.
    """
    stamp = int(time.time() * 1_000_000_000)
    start = int((started_at if started_at is not None else RUNTIME.started_at)
                * 1_000_000_000)
    по_имени: dict[str, dict[str, Any]] = {}
    гистограммы: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

    def тело(name: str, spec: MetricSpec | None) -> dict[str, Any]:
        if name not in по_имени:
            по_имени[name] = {
                "name": name,
                "unit": _UCUM.get(spec.unit, "") if spec else "",
                "description": " ".join(spec.description.split()) if spec else "",
            }
        return по_имени[name]

    for sample in samples:
        base = _base_name(sample.name)
        spec = METRICS_BY_NAME.get(base)
        if spec is not None and spec.type == "histogram" and sample.name != base:
            метки = {k: v for k, v in sample.labels.items() if k != "le"}
            ключ = (base, tuple(sorted(метки.items())))
            h = гистограммы.setdefault(ключ, {"labels": метки, "buckets": {},
                                              "sum": 0.0, "count": 0.0})
            if sample.name.endswith("_bucket"):
                h["buckets"][str(sample.labels.get("le"))] = float(sample.value)
            elif sample.name.endswith("_sum"):
                h["sum"] = float(sample.value)
            else:
                h["count"] = float(sample.value)
            тело(base, spec)
            continue
        spec = METRICS_BY_NAME.get(sample.name)
        body = тело(sample.name, spec)
        point: dict[str, Any] = {"asDouble": float(sample.value), "timeUnixNano": str(stamp),
                                 "attributes": _otlp_attributes(sample.labels)}
        if spec and spec.type == "counter":
            point["startTimeUnixNano"] = str(start)
            body.setdefault("sum", {"dataPoints": [], "aggregationTemporality": 2,
                                    "isMonotonic": True})["dataPoints"].append(point)
        else:
            body.setdefault("gauge", {"dataPoints": []})["dataPoints"].append(point)

    for (base, _), h in гистограммы.items():
        по_имени[base].setdefault("histogram", {"dataPoints": [], "aggregationTemporality": 2})[
            "dataPoints"].append(_otlp_histogram_point(
                h["labels"], h["buckets"], h["count"], h["sum"], start, stamp))

    return {
        "resourceMetrics": [{
            "resource": {"attributes": [
                {"key": "service.name", "value": {"stringValue": service_name}},
                {"key": "service.version", "value": {"stringValue": service_version}},
            ]},
            "scopeMetrics": [{"scope": {"name": "asrhub.monitoring"},
                              "metrics": list(по_имени.values())}],
        }]
    }


# ---------------------------------------------------------------------------
# Готовые конфигурации для внешних систем
# ---------------------------------------------------------------------------

def prometheus_rules(settings: Any = None) -> str:
    """Правила оповещения Prometheus, собранные из порогов каталога.

    Отдаются как готовый YAML: скопировать в rules.yml и перезагрузить
    Prometheus. Пороги здесь — отправная точка, а не истина: подгонять их
    под свой поток всё равно придётся. `settings` сдвигает пороги, которые
    зависят от настроек сервера, — так же, как у встроенных тревог
    (`catalog.порог`).
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
             # При отправке в Pushgateway absent() не сработает никогда:
             # шлюз держит последнее присланное значение asrhub_up, и
             # замолчавший сервер для Prometheus по-прежнему «жив». Молчание
             # видно только по времени последней отправки. Без Pushgateway
             # ряда push_time_seconds нет, и правило молчит — как и должно.
             "      - alert: ASRHubPushStale",
             "        expr: time() - max(push_time_seconds{job=\"asrhub\"}) > 600",
             "        for: 5m",
             "        labels: { severity: critical }",
             "        annotations:",
             "          summary: 'Сервер распознавания не присылает метрики в Pushgateway'",
             "          description: 'Последняя отправка — больше десяти минут назад. "
             "Шлюз держит прежние значения, и по ним сервер выглядит живым.'",
             ]

    for spec in METRICS:
        threshold = порог(spec, settings)
        if not threshold or spec.name == "asrhub_up" or spec.deprecated_for:
            continue
        seen_values: set[float] = set()
        for level, value in (("critical", threshold.critical), ("warning", threshold.warning)):
            if value is None or value in seen_values:
                continue                    # одинаковые пороги дают дублирующие правила
            seen_values.add(value)
            lines += [
                f"      - alert: {_alert_name(spec, level)}",
                f"        expr: {_rule_expression(spec, threshold, value)}",
                f"        for: {max(60, threshold.for_seconds)}s",
                f"        labels: {{ severity: {level} }}",
                "        annotations:",
                f"          summary: {_yaml_str(_summary(spec, threshold, value))}",
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


def _человеку(value: float, unit: str) -> str:
    """Порог в подписи: «5 ГБ», а не «5368709120 Б»."""
    if unit == "Б":
        for предел, имя in ((1024 ** 4, "ТБ"), (1024 ** 3, "ГБ"), (1024 ** 2, "МБ")):
            if abs(value) >= предел:
                return f"{value / предел:.4g} {имя}"
    return f"{value:.6g}" + (f" {unit}" if unit else "")


def _summary(spec: MetricSpec, threshold: Threshold, value: float) -> str:
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
    слово = слово_порога(threshold.direction, bool(threshold.inclusive))
    return f"{spec.label}: {слово} {_человеку(float(value), spec.unit)}"


#: Метрики, чей порог задан не в единицах самого ряда: доля, процент,
#: прирост. Их выражения требуют функций Prometheus.
_PROMETHEUS_COMPUTED = {
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
    "asrhub_stream_first_text_seconds": 'asrhub_stream_first_text_seconds{stat="p95"}',
    "asrhub_no_speech_total": "increase(asrhub_no_speech_total[30m])",
    "asrhub_auth_failures_total": "increase(asrhub_auth_failures_total[10m])",
    "asrhub_rate_limited_total": "increase(asrhub_rate_limited_total[10m])",
    "asrhub_webhooks_total": 'increase(asrhub_webhooks_total{result="failed"}[30m])',
}


def _rule_expression(spec: MetricSpec, threshold: Threshold, value: float) -> str:
    """Собирает выражение правила с учётом особенностей конкретной метрики."""
    if spec.name == "asrhub_uptime_seconds":
        # Обнуление счётчика времени работы означает недавний перезапуск;
        # ловим именно факт падения, а не малое значение при первом старте.
        # Именно resets(): changes() растёт на каждом опросе, потому что
        # время работы меняется всегда, и правило срабатывало бы постоянно.
        return "resets(asrhub_uptime_seconds[1h]) > 2"
    # Знак — тот же, что у встроенных тревог и триггеров Zabbix
    # (`catalog.оператор_порога`): прежде здесь был свой частный случай
    # для queue_paused, а у встроенного движка — свой, и по одной метрике
    # две системы наблюдения показывали разное.
    знак = оператор_порога(threshold.direction, bool(threshold.inclusive))
    return f"{_PROMETHEUS_COMPUTED.get(spec.name, spec.name)} {знак} {value}"


# ---------------------------------------------------------------------------
# Панель Grafana
# ---------------------------------------------------------------------------

#: Единицы каталога → единицы Grafana. Метрики разного масштаба на одной
#: оси — это прижатые к нулю линии: частоты рядом с unix-временем, проценты
#: рядом с байтами. Поэтому панель группы делится по единице.
_GRAFANA_UNITS = {"с": "s", "Б": "bytes", "МБ": "decmbytes", "ГБ": "decgbytes",
                  "%": "percent", "°C": "celsius", "Вт": "watt", "дБ": "dB",
                  "unixtime": "s"}
_UNIT_TITLES = {"s": "время", "bytes": "объём", "decmbytes": "объём",
                "decgbytes": "объём", "percent": "проценты", "percentunit": "доли",
                "celsius": "температура", "watt": "мощность", "dB": "дБ",
                "ops": "в секунду", "short": "значения", "bool": "да / нет"}
_DS = {"type": "prometheus", "uid": "${datasource}"}
_ОТБОР = 'instance=~"$instance"'


def _с_отбором(выражение: str, имя: str) -> str:
    """Вставляет отбор по экземпляру во все упоминания метрики."""
    return re.sub(rf"\b{re.escape(имя)}(?:\{{([^}}]*)\}})?(?![a-z0-9_])",
                  lambda м: f"{имя}{{{(м.group(1) + ',') if м.group(1) else ''}{_ОТБОР}}}",
                  выражение)


def _grafana_target(spec: MetricSpec) -> tuple[str, str, str]:
    """Запрос, подпись ряда и единица Grafana для одной метрики."""
    подпись = spec.label + "".join(f" {{{{{метка}}}}}" for метка in spec.labels)
    if spec.type == "histogram":
        выражение = (f"histogram_quantile(0.95, sum by (le) "
                     f"(rate({_с_отбором(spec.name + '_bucket', spec.name + '_bucket')}[5m])))")
        return выражение, spec.label + " p95", _GRAFANA_UNITS.get(spec.unit, "short")
    if spec.type == "counter":
        метки = ", ".join(spec.labels)
        внутри = f"rate({_с_отбором(spec.name, spec.name)}[5m])"
        выражение = f"sum by ({метки}) ({внутри})" if метки else f"sum({внутри})"
        return выражение, подпись, "ops"
    if spec.unit == "unixtime":
        # Время последней ошибки — unix-время, около 1,8·10⁹: на общей оси
        # оно прижимало остальные ряды к нулю. Показываем давность.
        ряд = _с_отбором(spec.name, spec.name)
        return f"(time() - {ряд}) and {ряд} > 0", "с последней ошибки", "s"
    единица = _GRAFANA_UNITS.get(spec.unit)
    if единица is None:
        if spec.name.endswith(("_share", "_rate", "_coverage")):
            единица = "percentunit"
        elif spec.name.endswith(("_up", "_available", "_paused", "_healthy")):
            единица = "bool"
        else:
            единица = "short"
    return _с_отбором(spec.name, spec.name), подпись, единица


def grafana_dashboard(title: str = "ASR Hub") -> dict[str, Any]:
    """Готовая панель Grafana, собранная по группам каталога.

    Строится программно, чтобы не расходиться с набором метрик: добавили
    метрику в каталог — она появилась на панели своей группы. Внутри группы
    панели делятся по единице измерения: прежде на одну ось попадали
    частоты и unix-время, проценты и байты, и все линии, кроме одной,
    лежали на нуле. Переменная `$instance` теперь действительно отбирает
    экземпляр, а ряды с метками подписаны своими метками.
    """
    panels: list[dict[str, Any]] = []
    y = 0
    статус = [("asrhub_up", "доступен", "bool"),
              ("asrhub_queue_depth", "в очереди", "short"),
              ("asrhub_active_jobs", "выполняется", "short"),
              ("asrhub_engines_available", "движков", "short"),
              ("asrhub_disk_free_bytes", "свободно на диске", "bytes")]
    panels.append({
        "type": "stat", "title": "Состояние", "datasource": _DS,
        "gridPos": {"h": 4, "w": 24, "x": 0, "y": y},
        "targets": [{"expr": _с_отбором(имя, имя), "legendFormat": подпись,
                     "refId": chr(ord("A") + номер), "datasource": _DS}
                    for номер, (имя, подпись, _) in enumerate(статус)],
        "fieldConfig": {"defaults": {"unit": "short"}, "overrides": [
            {"matcher": {"id": "byFrameRefID", "options": chr(ord("A") + номер)},
             "properties": [{"id": "unit", "value": единица}]}
            for номер, (_, _, единица) in enumerate(статус) if единица != "short"]},
    })
    y += 4

    столбец = 0
    for group in ("queue", "jobs", "performance", "quality", "resources", "storage",
                  "api", "errors", "webhooks", "content"):
        по_единице: dict[str, list[tuple[str, str]]] = {}
        for spec in METRICS:
            if spec.group != group or spec.type == "info" or spec.deprecated_for:
                continue
            выражение, подпись, единица = _grafana_target(spec)
            по_единице.setdefault(единица, []).append((выражение, подпись))
        for единица, запросы in по_единице.items():
            запросы = запросы[:8]
            panels.append({
                "type": "timeseries",
                "title": f"{GROUPS_BY_ID[group]['title']} — "
                         f"{_UNIT_TITLES.get(единица, единица)}",
                "description": GROUPS_BY_ID[group]["description"],
                "datasource": _DS,
                "gridPos": {"h": 8, "w": 12, "x": столбец, "y": y},
                "fieldConfig": {"defaults": {"unit": единица}, "overrides": []},
                "targets": [{"expr": выражение, "legendFormat": подпись,
                             "refId": chr(ord("A") + номер), "datasource": _DS}
                            for номер, (выражение, подпись) in enumerate(запросы)],
            })
            if столбец:
                y += 8
            столбец = 12 - столбец

    return {
        "title": title,
        "uid": "asrhub-main",
        "schemaVersion": 39,
        "version": 2,
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "tags": ["asrhub", "asr"],
        "panels": panels,
        "templating": {"list": [
            {"name": "datasource", "label": "Источник", "type": "datasource",
             "query": "prometheus"},
            {"name": "instance", "label": "Экземпляр", "type": "query",
             "datasource": _DS,
             "query": {"query": "label_values(asrhub_up, instance)", "refId": "instance"},
             "definition": "label_values(asrhub_up, instance)",
             "refresh": 2, "includeAll": True, "multi": True, "allValue": ".*",
             "current": {"text": "All", "value": "$__all"}},
        ]},
    }


def _stable_uuid(seed: str) -> str:
    """Ровно 32 шестнадцатеричных знака, одинаковых от запуска к запуску.

    Zabbix различает объекты по uuid: нестабильное значение приводит к тому,
    что повторный импорт создаёт дубликаты вместо обновления существующих.
    """
    import hashlib

    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Шаблон Zabbix
# ---------------------------------------------------------------------------

#: Метрики, у которых набор значений меток известен заранее. Для них можно
#: объявить обычные элементы; всё остальное уходит в правило обнаружения.
#: Срезы совпадают с тем, что отдаёт сборщик (collector._quantiles).
_STATS = [("avg",), ("p50",), ("p90",), ("p95",), ("p99",)]

_KNOWN_LABEL_VALUES: dict[str, list[tuple[str, ...]]] = {
    "asrhub_jobs_by_status": [("queued",), ("running",), ("completed",),
                              ("failed",), ("cancelled",), ("retry",), ("paused",)],
    "asrhub_rtf": _STATS,
    "asrhub_queue_wait_seconds": _STATS,
    "asrhub_confidence": _STATS,
    # asrhub_wer здесь стоял со срезами avg…p99, но метка у него — модель:
    # пять элементов и пять триггеров не получали данных никогда, а
    # `asrhub_wer[<модель>]` отбивался. Теперь он в обнаружении.
    "asrhub_job_duration_day_seconds": _STATS,
    "asrhub_media_duration_day_seconds": _STATS,
    "asrhub_audio_snr_db": [("p10",), ("p50",), ("p90",)],
    "asrhub_llm_latency_seconds": [("p50",), ("p95",)],
    "asrhub_llm_calls_total": [("ok",), ("error",), ("cache",)],
    "asrhub_jobs_total": [("completed",), ("failed",), ("cancelled",)],
    "asrhub_webhooks_total": [("ok",), ("failed",)],
    "asrhub_database_rows": [("jobs",), ("segments",), ("events",), ("metrics",)],
    "asrhub_storage_bytes": [("uploads",), ("results",), ("models",), ("logs",)],
    "asrhub_stage_seconds": [(стадия,) for стадия in СТАДИИ],
    "asrhub_collector_source_up": [(источник,) for источник in
                                   (*Collector.ИСТОЧНИКИ, "storage_size")],
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
    # Процент от объёма карты — второй элемент в другом правиле
    # обнаружения, а прототип триггера видит только своё правило.
    "asrhub_gpu_memory_used_bytes": None,
    # Порог — доля неудач, метрика — накопительный счётчик.
    "asrhub_jobs_total": None,
    "asrhub_http_requests_total": None,
    "asrhub_webhooks_total": None,
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
}

_ПРИОРИТЕТ = {"critical": "HIGH", "warning": "WARNING"}


def _zabbix_item(spec: MetricSpec, key: str, label: str, *, indent: str = "        ",
                 tag: str = "uuid") -> list[str]:
    return [
        f"{indent}- uuid: {_stable_uuid(key + (':' + tag if tag != 'uuid' else ''))}",
        f"{indent}  name: {_yaml_str(label)}",
        f"{indent}  type: TRAP",
        f"{indent}  key: {_yaml_str(key)}",
        f"{indent}  value_type: FLOAT",
        f"{indent}  units: {_yaml_str(spec.unit)}",
        f"{indent}  description: {_yaml_str(spec.description[:250])}",
    ]


def _zabbix_levels(spec: MetricSpec, threshold: Threshold | None,
                   ) -> list[tuple[str, float]]:
    """Уровни триггеров: критический и предупреждение, без повторов."""
    if threshold is None:
        return []
    уровни: list[tuple[str, float]] = []
    for уровень, значение in (("critical", threshold.critical),
                              ("warning", threshold.warning)):
        if значение is not None and значение not in {з for _, з in уровни}:
            уровни.append((уровень, float(значение)))
    return уровни


def _zabbix_triggers(spec: MetricSpec, key: str, label: str,
                     threshold: Threshold | None, *, indent: str = "          ",
                     prototype: bool = False) -> list[str]:
    """Триггеры элемента (или прототипы триггеров) по порогам каталога.

    Прежде заводился один триггер, по критическому порогу и строгим знаком:
    предупреждения в Zabbix не было вовсе, а включительные пороги (уровень
    дрейфа 2, запись с тревожным упоминанием) не срабатывали никогда.
    """
    выражения: list[tuple[str, str, str]] = []
    if spec.name in _ZABBIX_EXPRESSIONS:
        шаблон = _ZABBIX_EXPRESSIONS[spec.name]
        if шаблон is None:
            return []
        if "{op}" in шаблон:
            for уровень, значение in _zabbix_levels(spec, threshold):
                знак = оператор_порога(threshold.direction, bool(threshold.inclusive))
                выражения.append((уровень, шаблон.format(op=знак, value=f"{значение:.15g}"),
                                  f"{label}: {слово_порога(threshold.direction, bool(threshold.inclusive))} "
                                  f"{_человеку(значение, spec.unit)}"))
        else:
            уровень = "critical" if (threshold is None or threshold.critical is not None) \
                else "warning"
            выражения.append((уровень, шаблон, f"{label}: {spec.label.lower()}"))
    else:
        for уровень, значение in _zabbix_levels(spec, threshold):
            знак = оператор_порога(threshold.direction, bool(threshold.inclusive))
            выражения.append((
                уровень, f"last(/ASR Hub/{key}){знак}{значение:.15g}",
                f"{label}: {слово_порога(threshold.direction, bool(threshold.inclusive))} "
                f"{_человеку(значение, spec.unit)}"))
    if not выражения:
        return []
    строки = [f"{indent}{'trigger_prototypes' if prototype else 'triggers'}:"]
    for уровень, выражение, имя in выражения:
        строки += [
            f"{indent}  - uuid: {_stable_uuid(key + ':trigger:' + уровень)}",
            f"{indent}    expression: {_yaml_str(выражение)}",
            f"{indent}    name: {_yaml_str(имя)}",
            f"{indent}    priority: {_ПРИОРИТЕТ[уровень]}",
        ]
    return строки


def zabbix_template(settings: Any = None) -> str:
    """Шаблон Zabbix 6+ в формате YAML.

    **Ключи.** Элемент объявлен ровно тем ключом, каким метрика приходит от
    сервера (`zabbix_data`): метрики с заранее известными метками — обычными
    элементами, остальные — прототипами в правилах обнаружения, по правилу
    на метрику (`asrhub.discovery[<метрика>]`). Наборы меток для правил
    присылает сам сервер вместе со значениями.

    **Триггеры.** Порог из каталога подставлялся в сравнение сырого значения,
    но у части метрик он задан в процентах, в долях или в приросте.
    Получалось «last(asrhub_ram_used_bytes)>95» — авария при 95 байтах
    занятой памяти, горящая всегда, — и «last(asrhub_up)<1», не срабатывающий
    никогда. Теперь у каждой метрики с порогом — триггер на каждый уровень
    (предупреждение и авария), знаком из того же места, что у Prometheus.
    """
    lines = [
        "# Шаблон Zabbix для ASR Hub.",
        "# Импорт: Настройка -> Шаблоны -> Импорт, затем привязать шаблон к узлу.",
        "# Данные присылает сам сервер: приёмник kind: zabbix в monitoring_targets,",
        "# либо по расписанию: /api/monitoring/metrics?format=zabbix_sender | zabbix_sender -i -",
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
        threshold = порог(spec, settings)
        if spec.labels:
            known = _KNOWN_LABEL_VALUES.get(spec.name)
            if known is None:
                discovery.append(spec)
                continue
            wanted = _TRIGGER_STAT.get(spec.name)
            for combo in known:
                key = _zabbix_key(spec.name, list(combo))
                label = f"{spec.label} ({' '.join(combo)})"
                lines += _zabbix_item(spec, key, label)
                if wanted is None or combo[0] == wanted:
                    lines += _zabbix_triggers(spec, key, label, threshold)
            continue
        lines += _zabbix_item(spec, spec.name, spec.label)
        lines += _zabbix_triggers(spec, spec.name, spec.label, threshold)

    if discovery:
        lines.append("      discovery_rules:")
    for spec in discovery:
        макросы = ["{#" + метка.upper() + "}" for метка in spec.labels]
        ключ = _zabbix_key(spec.name, макросы)
        подпись = f"{spec.label} [{', '.join(макросы)}]"
        lines += [
            f"        - uuid: {_stable_uuid('asrhub-discovery:' + spec.name)}",
            f"          name: {_yaml_str('Обнаружение: ' + spec.label)}",
            "          type: TRAP",
            f"          key: {_yaml_str(f'asrhub.discovery[{spec.name}]')}",
            "          lifetime: 7d",
            f"          description: {_yaml_str('Наборы меток присылает сервер ASR Hub вместе со значениями: ' + ', '.join(spec.labels))}",
            "          item_prototypes:",
        ]
        lines += _zabbix_item(spec, ключ, подпись, indent="            ", tag="proto")
        # Порог по срезу («stat») прототипу не выразить: условие на значение
        # макроса — это переопределения правила, а не триггер. Такие
        # метрики остаются без триггера в Zabbix; правило есть в Prometheus.
        if "stat" not in spec.labels:
            lines += _zabbix_triggers(spec, ключ, подпись, порог(spec, settings),
                                      indent="              ", prototype=True)
    return "\n".join(lines) + "\n"
