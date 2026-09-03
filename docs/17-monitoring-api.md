# Программный интерфейс мониторинга

Отдельный справочник по маршрутам `/api/monitoring/*`: что принимает каждый, что возвращает, какой нужен ключ и как выглядит настоящий ответ. Общее руководство по мониторингу — «Мониторинг»; здесь только интерфейс.

Раздел собран из схемы OpenAPI работающего сервера, а примеры ответов сняты с него же, поэтому расходиться с действительностью им негде.

Всего маршрутов: **19**, операций: **21**.

## Как обращаться

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

## Обзор маршрутов

| Метод | Адрес | Что делает | Доступ |
|---|---|---|---|
| `GET` | `/api/monitoring/alerts` | Состояние оповещений | любой действующий ключ |
| `GET` | `/api/monitoring/alerts/history` | История срабатываний | любой действующий ключ |
| `GET` | `/api/monitoring/alerts/rules` | Правила оповещения | любой действующий ключ |
| `PUT` | `/api/monitoring/alerts/rules` | Заменить правила оповещения | ключ с ролью **admin** |
| `POST` | `/api/monitoring/alerts/rules/reset` | Вернуть правила из каталога метрик | ключ с ролью **admin** |
| `GET` | `/api/monitoring/catalog` | Справочник метрик: описания, пороги, рекомендации | любой действующий ключ |
| `GET` | `/api/monitoring/catalog/{name}` | Описание одной метрики | любой действующий ключ |
| `GET` | `/api/monitoring/config/grafana` | Готовая панель Grafana | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/config/prometheus` | Готовые правила оповещения Prometheus | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/config/prometheus-scrape` | Фрагмент prometheus.yml для сбора | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/config/zabbix` | Готовый шаблон Zabbix | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/health` | Сводное состояние сервиса | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/info` | Состояние самой подсистемы мониторинга | любой действующий ключ |
| `GET` | `/api/monitoring/live` | Проба живости | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/metrics` | Метрики во всех поддерживаемых форматах | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/metrics.json` | Снимок в JSON с описанием каждой метрики | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/ready` | Проба готовности | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/startup` | Проба завершения запуска | без ключа при `monitoring_public: true`, иначе любой действующий ключ |
| `GET` | `/api/monitoring/targets` | Приёмники метрик и состояние доставки | любой действующий ключ |
| `PUT` | `/api/monitoring/targets` | Заменить список приёмников | ключ с ролью **admin** |
| `POST` | `/api/monitoring/targets/test` | Проверить приёмник немедленно | ключ с ролью **admin** |

## Метрики

Основные точки: снимок всех параметров работы сервиса.

### `GET /api/monitoring/metrics`

Метрики во всех поддерживаемых форматах.

Полный снимок всех параметров работы сервиса.

Формат выбирается параметром `format`. По умолчанию — текстовый формат
Prometheus, его же ждёт большинство систем сбора.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `format` | в адресе | string | `prometheus` | prometheus, openmetrics, json, otlp, influx, graphite, zabbix, csv |
| `host` | в адресе | string | `asrhub` | Имя узла для Zabbix |

**Пример**

```bash
curl 'http://сервер:8080/api/monitoring/metrics'
```

**Ответ**

```
# HELP asrhub_up Единица, если сервис отвечает на запрос метрик. Нуля здесь не бывает: если сервис лежит, ответа не будет вовсе — и это как раз нужный признак.
# TYPE asrhub_up gauge
asrhub_up 1
# HELP asrhub_uptime_seconds Сколько секунд прошло с момента запуска процесса. [с]
# TYPE asrhub_uptime_seconds gauge
asrhub_uptime_seconds 96.3
# HELP asrhub_build_info Постоянная метрика со значением 1 и метками: версия сервиса, версия схемы базы, версия Python, дата каталога моделей. Так принято передавать в Prometheus то, что не является числом.
# TYPE asrhub_build_info gauge
…
```

Формат задаётся параметром `format`; по умолчанию — текстовый формат Prometheus.

### `GET /api/monitoring/metrics.json`

Снимок в JSON с описанием каждой метрики.

То же, что и метрики, но с описаниями, рекомендациями и порогами.

Формат для систем, которые не понимают Prometheus, и для случая, когда
получателю нужно не только число, но и то, что оно означает.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `group` | в адресе | string | — | Только одна группа |

**Пример**

```bash
curl 'http://сервер:8080/api/monitoring/metrics.json?group=queue'
```

**Ответ**

```json
{
  "timestamp": 1788448982.4429617,
  "collected_at": "2026-09-03T15:23:02+0000",
  "metrics": [
    {
      "name": "asrhub_active_jobs",
      "values": [
        {
          "labels": {},
          "value": 0.0
        }
      ],
      "type": "gauge",
      "group": "queue",
      "label": "Выполняется сейчас",
      "unit": "",
      "description": "Сколько заданий обрабатывается прямо сейчас.",
      "recommendation": "Сравнивайте с asrhub_workers. Устойчивое равенство при непустой очереди означает, что сервер загружен полностью и очередь ограничена мощностью, а не расписанием.",
      "normal": "",
      "threshold": null
    },
    {
      "name": "asrhub_jobs_by_status",
      "values": [
        {
          "labels": {
            "status": "queued"
          },
          "value": 0.0
        },
        {
          "labels": {
            "status": "running"
          },
          "value": 0.0
        },
        {
          "labels": {
            "status": "retry"
          },
          "value": 0.0
        },
        {
          "labels": {
            "status": "paused"
          },
          "value": 0.0
        },
        {
          "labels": {
…
```

К каждой метрике приложены описание, рекомендация и порог — получатель видит не только число, но и что оно значит.

## Состояние

Пробы для оркестратора и сводка для дежурного.

### `GET /api/monitoring/health`

Сводное состояние сервиса.

Одним запросом: живость, готовность, запуск и сработавшие тревоги.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


**Пример**

```bash
curl 'http://сервер:8080/api/monitoring/health'
```

**Ответ**

```json
{
  "status": "ok",
  "uptime_s": 96.4,
  "liveness": {
    "status": "ok",
    "checks": [
      {
        "name": "process",
        "status": "ok",
        "detail": "работает 96 с",
        "hint": ""
      },
      {
        "name": "queue_thread",
        "status": "ok",
        "detail": "рабочие потоки запущены",
        "hint": ""
      }
    ]
  },
  "readiness": {
    "status": "ok",
    "checks": [
      {
        "name": "database",
        "status": "ok",
        "detail": "отвечает",
        "hint": ""
      },
      {
        "name": "engines",
        "status": "ok",
        "detail": "доступно 1",
        "hint": ""
      },
      {
        "name": "disk",
        "status": "ok",
        "detail": "свободно 16.2 ГБ",
        "hint": ""
      },
      {
        "name": "queue",
        "status": "ok",
        "detail": "ждёт 0, выполняется 0",
        "hint": ""
      }
…
```

Код ответа: 200 при состоянии `ok` и `warning`, 503 при `degraded` и `critical` — на него можно навесить проверку балансировщика без разбора тела.

### `GET /api/monitoring/live`

Проба живости.

Для оркестратора: провал означает «перезапусти контейнер».

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


**Пример**

```bash
curl -o /dev/null -w '%{http_code}\n' http://сервер:8080/api/monitoring/live
```

Провал означает «перезапусти контейнер», поэтому проба не зависит ни от базы, ни от очереди.

### `GET /api/monitoring/ready`

Проба готовности.

Для балансировщика: провал означает «не шли сюда запросы».

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


**Пример**

```bash
curl 'http://сервер:8080/api/monitoring/ready'
```

**Ответ**

```json
{
  "status": "ok",
  "checks": [
    {
      "name": "database",
      "status": "ok",
      "detail": "отвечает",
      "hint": ""
    },
    {
      "name": "engines",
      "status": "ok",
      "detail": "доступно 1",
      "hint": ""
    },
    {
      "name": "disk",
      "status": "ok",
      "detail": "свободно 16.2 ГБ",
      "hint": ""
    },
    {
      "name": "queue",
      "status": "ok",
      "detail": "ждёт 0, выполняется 0",
      "hint": ""
    }
  ]
}
```

### `GET /api/monitoring/startup`

Проба завершения запуска.

Пока не пройдена, остальные пробы учитывать не следует.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


## Справочник метрик

Что означает каждая метрика и какой у неё порог.

### `GET /api/monitoring/catalog`

Справочник метрик: описания, пороги, рекомендации.

Каталог всех метрик с описанием каждой.

Тот же источник, из которого собраны раздел документации о мониторинге,
правила Prometheus и шаблон Zabbix.

**Доступ:** любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `group` | в адресе | string | — | — |

**Пример**

```bash
curl -H 'X-API-Key: $КЛЮЧ' 'http://сервер:8080/api/monitoring/catalog?group=queue'
```

**Ответ**

```json
{
  "groups": [
    {
      "id": "service",
      "title": "Служба",
      "description": "Жив ли сервис, сколько работает, какая версия и настройки."
    },
    {
      "id": "queue",
      "title": "Очередь",
      "description": "Сколько заданий ждёт, сколько выполняется, как долго ждут."
    },
    {
      "id": "jobs",
      "title": "Задания",
      "description": "Сколько заданий прошло, чем закончились, в каких разрезах."
    },
    {
      "id": "performance",
      "title": "Производительность",
      "description": "Скорость обработки: RTF, время по стадиям, пропускная способность."
    },
    {
      "id": "quality",
      "title": "Качество",
      "description": "Уверенность модели, WER и CER, доля записей без речи."
    },
    {
      "id": "models",
      "title": "Модели и движки",
      "description": "Что загружено в память, сколько занимает, что доступно."
    },
    {
      "id": "resources",
      "title": "Оборудование",
      "description": "Процессор, память, видеокарта, диск."
    },
    {
      "id": "storage",
      "title": "Хранилище",
…
```

### `GET /api/monitoring/catalog/{name}`

Описание одной метрики.

**Доступ:** любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `name` | в пути | string | обязателен | — |

**Пример**

```bash
curl -H 'X-API-Key: $КЛЮЧ' http://сервер:8080/api/monitoring/catalog/asrhub_queue_depth
```

**Ответ**

```json
{
  "name": "asrhub_queue_depth",
  "type": "gauge",
  "group": "queue",
  "label": "Заданий ждёт",
  "description": "Сколько заданий стоит в очереди и ждёт свободного воркера. Считаются состояния «в очереди» и «ожидает повтора».",
  "unit": "",
  "labels": [],
  "recommendation": "Главный показатель того, справляется ли сервер. Смотреть надо не на значение, а на тенденцию: очередь из ста заданий, которая тает, — это нормальный ночной прогон; очередь из двадцати, которая растёт третий час, — это нехватка мощности.",
  "normal": "колеблется около нуля в рабочем режиме",
  "threshold": {
    "direction": "above",
    "warning": 50,
    "critical": 200,
    "for_seconds": 900,
    "note": "Пороги подбирайте под свой поток: значимо не число, а рост"
  },
  "troubleshooting": "Поднять max_concurrent_jobs (если хватает памяти), перевести массовые задания на низкий приоритет, включить scheduling_policy: shortest_first, взять модель полегче",
  "since_restart": false,
  "deprecated_for": "",
…
```

Если метрики нет, ответ 404 с кодом `metric_not_found` и списком похожих имён в подсказке.

## Тревоги

Состояние порогов, история срабатываний, свои правила.

### `GET /api/monitoring/alerts`

Состояние оповещений.

**Доступ:** любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `only_firing` | в адресе | boolean | `False` | — |

**Пример**

```bash
curl -H 'X-API-Key: $КЛЮЧ' 'http://сервер:8080/api/monitoring/alerts?only_firing=true'
```

**Ответ**

```json
{
  "summary": {
    "rules": 34,
    "firing": 0,
    "pending": 3,
    "critical": 0,
    "warning": 0,
    "worst": "ok"
  },
  "alerts": []
}
```

### `GET /api/monitoring/alerts/history`

История срабатываний.

**Доступ:** любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `limit` | в адресе | integer | `100` | — |

### `GET /api/monitoring/alerts/rules`

Правила оповещения.

**Доступ:** любой действующий ключ.


### `PUT /api/monitoring/alerts/rules`

Заменить правила оповещения.

**Доступ:** ключ с ролью **admin**.


**Тело запроса** — JSON:

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true
  },
  "title": "Rules"
}
```

**Пример**

```bash
curl -X PUT http://сервер:8080/api/monitoring/alerts/rules \
  -H 'X-API-Key: $КЛЮЧ' -H 'Content-Type: application/json' \
  -d '[{"metric": "asrhub_queue_depth", "direction": "above",
        "threshold": 500, "severity": "warning",
        "for_seconds": 1800}]'
```

Заменяет весь набор правил целиком. Вернуть пороги каталога — `POST /api/monitoring/alerts/rules/reset`.

### `POST /api/monitoring/alerts/rules/reset`

Вернуть правила из каталога метрик.

**Доступ:** ключ с ролью **admin**.


## Приёмники метрик

Куда сервер отправляет метрики сам.

### `GET /api/monitoring/targets`

Приёмники метрик и состояние доставки.

**Доступ:** любой действующий ключ.


**Пример**

```bash
curl -H 'X-API-Key: $КЛЮЧ' http://сервер:8080/api/monitoring/targets
```

**Ответ**

```json
{
  "kinds": [
    "prometheus_pushgateway",
    "influxdb",
    "otlp",
    "statsd",
    "webhook"
  ],
  "targets": []
}
```

### `PUT /api/monitoring/targets`

Заменить список приёмников.

**Доступ:** ключ с ролью **admin**.


**Тело запроса** — JSON:

```json
{
  "type": "array",
  "items": {
    "type": "object",
    "additionalProperties": true
  },
  "title": "Targets"
}
```

### `POST /api/monitoring/targets/test`

Проверить приёмник немедленно.

Отправляет текущий снимок в указанный приёмник и возвращает результат.

Приёмник можно не сохранять: описание передаётся прямо в теле запроса,
поэтому настройку удобно проверять до того, как записать её в конфигурацию.

**Доступ:** ключ с ролью **admin**.


**Тело запроса** — JSON:

```json
{
  "type": "object",
  "additionalProperties": true,
  "title": "Target"
}
```

**Пример**

```bash
curl -X POST http://сервер:8080/api/monitoring/targets/test \
  -H 'X-API-Key: $КЛЮЧ' -H 'Content-Type: application/json' \
  -d '{"kind": "influxdb", "url": "http://influx:8086",
       "database": "asrhub"}'
```

Отправляет текущий снимок немедленно и возвращает результат. Приёмник при этом не сохраняется — настройку удобно проверить до того, как записать её в конфигурацию.

```json
{"ok": true, "sent_metrics": 118}
```

## Готовые конфигурации

Файлы для Prometheus, Grafana и Zabbix.

### `GET /api/monitoring/config/prometheus`

Готовые правила оповещения Prometheus.

Файл правил, собранный из порогов каталога. Скопировать в rules.yml.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


**Пример**

```bash
curl http://сервер:8080/api/monitoring/config/prometheus \
  -o /etc/prometheus/asrhub-rules.yml
```

Файл правил, собранный из порогов каталога. Проверить перед применением: `promtool check rules asrhub-rules.yml`.

### `GET /api/monitoring/config/prometheus-scrape`

Фрагмент prometheus.yml для сбора.

Готовый блок scrape_configs — с правильным путём и разумным интервалом.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `target` | в адресе | string | — | адрес:порт сервера |

### `GET /api/monitoring/config/grafana`

Готовая панель Grafana.

Панель, собранная по группам каталога метрик. Импортировать в Grafana.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


| Параметр | Где | Тип | По умолчанию | Описание |
|---|---|---|---|---|
| `title` | в адресе | string | `ASR Hub` | — |

### `GET /api/monitoring/config/zabbix`

Готовый шаблон Zabbix.

**Доступ:** без ключа при `monitoring_public: true`, иначе любой действующий ключ.


## Служебное

Состояние самой подсистемы мониторинга.

### `GET /api/monitoring/info`

Состояние самой подсистемы мониторинга.

Сколько было опросов, сколько метрик, какие источники не отвечают.

**Доступ:** любой действующий ключ.


**Пример**

```bash
curl -H 'X-API-Key: $КЛЮЧ' http://сервер:8080/api/monitoring/info
```

**Ответ**

```json
{
  "scrapes": 3,
  "samples": 515,
  "collection_errors": [],
  "cache_ttl_s": 5.0,
  "alerts": {
    "rules": 34,
    "firing": 0,
    "pending": 3,
    "critical": 0,
    "warning": 0,
    "worst": "ok"
  },
  "targets": []
}
```

Поле `collection_errors` перечисляет источники, которые не удалось опросить. Пустой список — все источники отвечают.

## Примеры интеграции

### Опрос из своей программы

```python
import requests

СЕРВЕР = "http://сервер:8080"


def состояние() -> dict:
    """Одним запросом: жив ли сервис, готов ли принимать, есть ли тревоги."""
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

curl -fsS "$СЕРВЕР/api/monitoring/ready" >/dev/null \
  || { echo "сервис не готов принимать задания"; exit 1; }

# Убеждаемся, что метрики не только отдаются, но и собираются целиком
ОШИБКИ=$(curl -fsS "$СЕРВЕР/api/monitoring/metrics.json" \
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

