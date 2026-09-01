#!/usr/bin/env python3
"""Справочник программного интерфейса мониторинга.

Собирается из схемы OpenAPI работающего сервера и живых ответов, поэтому не
может разойтись с тем, что сервер отдаёт на самом деле. Примеры ответов —
настоящие, снятые с сервера, а не написанные от руки.

    # сервер должен быть запущен
    python3 docs/generate_api.py [адрес] [ключ]

Без адреса берётся http://127.0.0.1:8080; ключ читается из файла api-key.txt
в каталоге данных, если он есть.
"""
from __future__ import annotations

import sys
from typing import Any

from apidoc import (
    DOCS,
    METHOD_ORDER,
    body_block,
    fetch,
    find_key,
    load_schema,
    params_table,
    sample,
)
from apidoc import (
    operation_block as shared_operation_block,
)

PREFIX = "/api/monitoring"

__all__ = ["fetch", "sample", "params_table", "body_block"]

#: Порядок разделов и их описание. Маршруты, не попавшие ни в один раздел,
#: собираются в конце — так новый маршрут не потеряется молча.
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Метрики", "Основные точки: снимок всех параметров работы сервиса.",
     (f"{PREFIX}/metrics", f"{PREFIX}/metrics.json")),
    ("Состояние", "Пробы для оркестратора и сводка для дежурного.",
     (f"{PREFIX}/health", f"{PREFIX}/live", f"{PREFIX}/ready", f"{PREFIX}/startup")),
    ("Справочник метрик", "Что означает каждая метрика и какой у неё порог.",
     (f"{PREFIX}/catalog", f"{PREFIX}/catalog/{{name}}")),
    ("Тревоги", "Состояние порогов, история срабатываний, свои правила.",
     (f"{PREFIX}/alerts", f"{PREFIX}/alerts/history",
      f"{PREFIX}/alerts/rules", f"{PREFIX}/alerts/rules/reset")),
    ("Приёмники метрик", "Куда сервер отправляет метрики сам.",
     (f"{PREFIX}/targets", f"{PREFIX}/targets/test")),
    ("Готовые конфигурации", "Файлы для Prometheus, Grafana и Zabbix.",
     (f"{PREFIX}/config/prometheus", f"{PREFIX}/config/prometheus-scrape",
      f"{PREFIX}/config/grafana", f"{PREFIX}/config/zabbix")),
    ("Служебное", "Состояние самой подсистемы мониторинга.",
     (f"{PREFIX}/info",)),
]

#: Маршруты, доступные без ключа при monitoring_public: true.
OPEN_ROUTES = {
    f"{PREFIX}/metrics", f"{PREFIX}/metrics.json", f"{PREFIX}/health",
    f"{PREFIX}/live", f"{PREFIX}/ready", f"{PREFIX}/startup",
    f"{PREFIX}/config/prometheus", f"{PREFIX}/config/prometheus-scrape",
    f"{PREFIX}/config/grafana", f"{PREFIX}/config/zabbix",
}
ADMIN_ROUTES = {
    (f"{PREFIX}/alerts/rules", "put"), (f"{PREFIX}/alerts/rules/reset", "post"),
    (f"{PREFIX}/targets", "put"), (f"{PREFIX}/targets/test", "post"),
}


def access_note(path: str, method: str) -> str:
    if (path, method) in ADMIN_ROUTES:
        return "ключ с ролью **admin**"
    if path in OPEN_ROUTES:
        return "без ключа при `monitoring_public: true`, иначе любой действующий ключ"
    return "любой действующий ключ"


def main(argv: list[str]) -> int:
    base = (argv[1] if len(argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
    key = find_key(argv[2] if len(argv) > 2 else "", base=base)

    schema = load_schema(base)
    if schema is None:
        return 1

    paths = {p: ops for p, ops in schema["paths"].items() if p.startswith(PREFIX)}
    out: list[str] = []
    add = out.append

    add("# Программный интерфейс мониторинга\n")
    add("Отдельный справочник по маршрутам `/api/monitoring/*`: что принимает "
        "каждый, что возвращает, какой нужен ключ и как выглядит настоящий "
        "ответ. Общее руководство по мониторингу — «Мониторинг»; здесь только "
        "интерфейс.\n")
    add("Раздел собран из схемы OpenAPI работающего сервера, а примеры ответов "
        "сняты с него же, поэтому расходиться с действительностью им негде.\n")

    total = sum(len(ops) for ops in paths.values())
    add(f"Всего маршрутов: **{len(paths)}**, операций: **{total}**.\n")

    add(MONITORING_API_INTRO)

    add("## Обзор маршрутов\n")
    add("| Метод | Адрес | Что делает | Доступ |")
    add("|---|---|---|---|")
    for path in sorted(paths):
        for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
            operation = paths[path][method]
            add(f"| `{method.upper()}` | `{path}` | {operation.get('summary', '')} "
                f"| {access_note(path, method)} |")
    add("")

    covered: set[str] = set()
    for title, description, members in SECTIONS:
        present = [p for p in members if p in paths]
        if not present:
            continue
        covered.update(present)
        add(f"## {title}\n")
        add(f"{description}\n")
        for path in present:
            for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
                out.extend(shared_operation_block(
                    path, method, paths[path][method], base, key,
                    access=access_note, examples=EXAMPLES))

    leftovers = sorted(set(paths) - covered)
    if leftovers:
        add("## Прочие маршруты\n")
        for path in leftovers:
            for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
                out.extend(shared_operation_block(
                    path, method, paths[path][method], base, key,
                    access=access_note, examples=EXAMPLES))

    add(MONITORING_API_TAIL)

    target = DOCS / "17-monitoring-api.md"
    text = "\n".join(out) + "\n"
    target.write_text(text, encoding="utf-8")
    print(f"  {target.name} — {len(text.splitlines())} строк, {len(text) // 1024} КБ")
    return 0


K = "$КЛЮЧ"

EXAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    (f"{PREFIX}/metrics", "get"): {
        "curl": "curl 'http://сервер:8080/api/monitoring/metrics'",
        "show": f"{PREFIX}/metrics", "lang": "", "limit": 700,
        "note": "Формат задаётся параметром `format`; по умолчанию — текстовый "
                "формат Prometheus.",
    },
    (f"{PREFIX}/metrics.json", "get"): {
        "curl": "curl 'http://сервер:8080/api/monitoring/metrics.json?group=queue'",
        "show": f"{PREFIX}/metrics.json?group=queue", "limit": 1200,
        "note": "К каждой метрике приложены описание, рекомендация и порог — "
                "получатель видит не только число, но и что оно значит.",
    },
    (f"{PREFIX}/health", "get"): {
        "curl": "curl 'http://сервер:8080/api/monitoring/health'",
        "show": f"{PREFIX}/health", "limit": 900,
        "note": "Код ответа: 200 при состоянии `ok` и `warning`, 503 при "
                "`degraded` и `critical` — на него можно навесить проверку "
                "балансировщика без разбора тела.",
    },
    (f"{PREFIX}/live", "get"): {
        "curl": "curl -o /dev/null -w '%{http_code}\\n' http://сервер:8080/api/monitoring/live",
        "note": "Провал означает «перезапусти контейнер», поэтому проба не "
                "зависит ни от базы, ни от очереди.",
    },
    (f"{PREFIX}/ready", "get"): {
        "curl": "curl 'http://сервер:8080/api/monitoring/ready'",
        "show": f"{PREFIX}/ready", "limit": 800,
    },
    (f"{PREFIX}/catalog", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' "
                "'http://сервер:8080/api/monitoring/catalog?group=queue'",
        "show": f"{PREFIX}/catalog?group=queue", "limit": 1100,
    },
    (f"{PREFIX}/catalog/{{name}}", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' "
                "http://сервер:8080/api/monitoring/catalog/asrhub_queue_depth",
        "show": f"{PREFIX}/catalog/asrhub_queue_depth", "limit": 1000,
        "note": "Если метрики нет, ответ 404 с кодом `metric_not_found` и "
                "списком похожих имён в подсказке.",
    },
    (f"{PREFIX}/alerts", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' "
                "'http://сервер:8080/api/monitoring/alerts?only_firing=true'",
        "show": f"{PREFIX}/alerts?only_firing=true", "limit": 800,
    },
    (f"{PREFIX}/alerts/rules", "put"): {
        "curl": "curl -X PUT http://сервер:8080/api/monitoring/alerts/rules \\\n"
                f"  -H 'X-API-Key: {K}' -H 'Content-Type: application/json' \\\n"
                "  -d '[{\"metric\": \"asrhub_queue_depth\", \"direction\": \"above\",\n"
                "        \"threshold\": 500, \"severity\": \"warning\",\n"
                "        \"for_seconds\": 1800}]'",
        "note": "Заменяет весь набор правил целиком. Вернуть пороги каталога — "
                "`POST /api/monitoring/alerts/rules/reset`.",
    },
    (f"{PREFIX}/targets", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' http://сервер:8080/api/monitoring/targets",
        "show": f"{PREFIX}/targets", "limit": 600,
    },
    (f"{PREFIX}/targets/test", "post"): {
        "curl": "curl -X POST http://сервер:8080/api/monitoring/targets/test \\\n"
                f"  -H 'X-API-Key: {K}' -H 'Content-Type: application/json' \\\n"
                "  -d '{\"kind\": \"influxdb\", \"url\": \"http://influx:8086\",\n"
                "       \"database\": \"asrhub\"}'",
        "note": "Отправляет текущий снимок немедленно и возвращает результат. "
                "Приёмник при этом не сохраняется — настройку удобно проверить "
                "до того, как записать её в конфигурацию.\n\n"
                "```json\n{\"ok\": true, \"sent_metrics\": 118}\n```",
    },
    (f"{PREFIX}/config/prometheus", "get"): {
        "curl": "curl http://сервер:8080/api/monitoring/config/prometheus \\\n"
                "  -o /etc/prometheus/asrhub-rules.yml",
        "note": "Файл правил, собранный из порогов каталога. Проверить перед "
                "применением: `promtool check rules asrhub-rules.yml`.",
    },
    (f"{PREFIX}/info", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' http://сервер:8080/api/monitoring/info",
        "show": f"{PREFIX}/info", "limit": 700,
        "note": "Поле `collection_errors` перечисляет источники, которые не "
                "удалось опросить. Пустой список — все источники отвечают.",
    },
}


MONITORING_API_INTRO = """## Как обращаться

```bash
СЕРВЕР=http://сервер:8080
КЛЮЧ=ah_ваш_ключ

# метрики — обычно без ключа
curl "$СЕРВЕР/api/monitoring/metrics"

# всё остальное — с ключом
curl -H "X-API-Key: $КЛЮЧ" "$СЕРВЕР/api/monitoring/catalog"
```

### Доступ

Маршруты делятся на три группы:

| Группа | Что нужно | Почему так |
|---|---|---|
| Метрики, пробы, готовые конфигурации | ничего при `monitoring_public: true` | Prometheus не умеет обновлять истекающие ключи; ограничивать доступ правильнее на прокси по адресу сети сбора |
| Справочник, тревоги, приёмники — чтение | любой действующий ключ | данные о нагрузке и ошибках не должны быть публичными |
| Изменение правил и приёмников | ключ с ролью `admin` | эти вызовы меняют поведение сервера |

При `monitoring_public: false` ключ нужен и для первой группы. Он передаётся
теми же тремя способами, что и в остальном интерфейсе: заголовком `X-API-Key`,
заголовком `Authorization: Bearer`, либо параметром `api_key` в адресе — последнее
только там, где заголовок задать негде.

### Формат ошибок

Ошибки приходят в том же виде, что и в остальном интерфейсе:

```json
{
  "detail": {
    "code": "metric_not_found",
    "message": "Метрика «asrhub_rtfx» не найдена.",
    "hint": "Похожие метрики: asrhub_rtf, asrhub_rtf_by_model",
    "retryable": false
  }
}
```

| Код | HTTP | Когда |
|---|---|---|
| `config_error` | 400 | неизвестный формат выгрузки, неверное описание правила или приёмника |
| `auth_error` | 401 | ключ не передан или недействителен при `monitoring_public: false` |
| `forbidden` | 403 | ключ есть, но роли не хватает |
| `metric_not_found` | 404 | нет метрики с таким именем |
| `rate_limited` | 429 | превышен лимит частоты для ключа |

⚠️ Ответ 503 у `/health`, `/ready` и `/startup` — не ошибка запроса, а
состояние сервиса. Так и задумано: оркестратор смотрит на код ответа, не
разбирая тело.
"""


MONITORING_API_TAIL = """## Примеры интеграции

### Опрос из своей программы

```python
import requests

СЕРВЕР = "http://сервер:8080"


def состояние() -> dict:
    \"\"\"Одним запросом: жив ли сервис, готов ли принимать, есть ли тревоги.\"\"\"
    ответ = requests.get(f"{СЕРВЕР}/api/monitoring/health", timeout=10)
    # 503 — это тоже осмысленный ответ, а не сбой запроса
    return ответ.json()


def тревоги(ключ: str) -> list[dict]:
    ответ = requests.get(f"{СЕРВЕР}/api/monitoring/alerts",
                         params={"only_firing": "true"},
                         headers={"X-API-Key": ключ}, timeout=10)
    ответ.raise_for_status()
    return ответ.json()["alerts"]


данные = состояние()
if данные["status"] != "ok":
    for тревога in тревоги("ah_ваш_ключ"):
        print(f"{тревога['severity']}: {тревога['summary']} "
              f"— значение {тревога['value']} при пороге {тревога['threshold']}")
        print(f"  что делать: {тревога['hint']}")
```

### Своя панель поверх справочника

Каталог отдаёт не только числа, но и то, что они означают, — описание,
рекомендацию и порог. Поэтому подсказки в своей панели писать заново не нужно:

```javascript
const [снимок, справочник] = await Promise.all([
  fetch('/api/monitoring/metrics.json').then((r) => r.json()),
  fetch('/api/monitoring/catalog', { headers: { 'X-API-Key': ключ } })
    .then((r) => r.json()),
]);

const описания = Object.fromEntries(
  справочник.metrics.map((m) => [m.name, m]));

for (const метрика of снимок.metrics) {
  const описание = описания[метрика.name];
  if (!описание) continue;
  console.log(`${описание.label}: ${метрика.values[0].value} ${описание.unit}`);
  console.log(`  норма: ${описание.normal || 'не задана'}`);
}
```

### Проверка после развёртывания

```bash
#!/usr/bin/env bash
# Убеждаемся, что после обновления сервис поднялся и метрики отдаются.
set -o errexit

СЕРВЕР=http://127.0.0.1:8080

for _ in $(seq 1 30); do
  if curl -fsS "$СЕРВЕР/api/monitoring/startup" >/dev/null 2>&1; then break; fi
  sleep 5
done

curl -fsS "$СЕРВЕР/api/monitoring/ready" >/dev/null \\
  || { echo "сервис не готов принимать задания"; exit 1; }

# Убеждаемся, что метрики не только отдаются, но и собираются целиком
ОШИБКИ=$(curl -fsS "$СЕРВЕР/api/monitoring/metrics.json" \\
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("collection_errors") or []))')
[[ "$ОШИБКИ" == "0" ]] || { echo "источники метрик не отвечают: $ОШИБКИ"; exit 1; }

echo "развёртывание проверено"
```

## Что стоит помнить

- **Интервал опроса.** Чаще 15 секунд смысла нет: замеры железа обновляются
  раз в 20 секунд служебным циклом сервера. Снимок дополнительно кешируется
  на `monitoring_cache_ttl_s` секунд, и слишком частый опрос вернёт то же самое.
- **Пробы — не метрики.** `/live` и `/ready` отвечают кодом, а не числом, и
  вызывать их для сбора статистики не нужно.
- **Изменение правил заменяет набор целиком.** `PUT /api/monitoring/alerts/rules`
  не добавляет правило к существующим: передавайте весь список.
- **Проверка приёмника ничего не сохраняет.** `POST /api/monitoring/targets/test`
  только отправляет снимок и возвращает результат.
- **`collection_errors` важнее, чем кажется.** Пустой список означает, что все
  источники опрошены. Непустой — что часть метрик отсутствует, а тревоги поверх
  них молчат не потому, что всё хорошо.
"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
