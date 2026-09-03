#!/usr/bin/env python3
"""Справочник программного интерфейса ASR Hub целиком.

Собирается из схемы OpenAPI работающего сервера: задания, каталог моделей,
настройки, очередь, обслуживание, мониторинг — всё в одном документе.
Примеры ответов снимаются с того же сервера, поэтому справочник не может
разойтись с тем, что сервер отдаёт на самом деле.

    # сервер должен быть запущен
    python3 docs/generate_api_full.py [адрес] [ключ]

Без адреса берётся http://127.0.0.1:8080; ключ ищется в файле api-key.txt
в каталоге данных.
"""
from __future__ import annotations

import sys
from typing import Any

from apidoc import (
    DOCS,
    METHOD_ORDER,
    find_key,
    load_schema,
    operation_block,
    overview_table,
)

K = "$КЛЮЧ"
HOST = "http://сервер:8080"

#: Разделы справочника: заголовок, вводный текст, префиксы маршрутов.
#: Порядок — от того, с чего начинают, к тому, что нужно реже.
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("Задания",
     "Основная работа: поставить файл в очередь, следить за ходом, забрать "
     "результат. Всё остальное в интерфейсе так или иначе обслуживает эти "
     "маршруты.",
     ("/api/jobs",)),
    ("Очередь",
     "Управление потоком заданий целиком: пауза, число одновременных "
     "заданий, массовые операции. Приоритет отдельного задания — в разделе "
     "«Задания».",
     ("/api/queue",)),
    ("Каталог моделей и параметров",
     "Что сервер умеет: доступные модели и движки, все параметры с "
     "описаниями, готовые пресеты. Каталог статичен и не требует ключа с "
     "правом записи.",
     ("/api/catalog", "/api/models", "/api/engines", "/api/params", "/api/presets")),
    ("Настройки",
     "Значения по умолчанию для новых заданий и параметры самого сервера. "
     "Изменения применяются сразу, но переживают перезапуск только после "
     "сохранения в файл.",
     ("/api/settings",)),
    ("Сведения о сервере",
     "Версия, оборудование, аналитика, журнал и лента событий.",
     ("/api/health", "/api/system", "/api/analytics", "/api/logs",
      "/api/events", "/api/reference")),
    ("Ключи доступа, подразделения и квоты",
     "Выпуск и отзыв ключей, одноразовые билеты для WebSocket, расход по "
     "суточным квотам. Ключи с одинаковым `group` видят задания друг друга.",
     ("/api/keys", "/api/auth", "/api/usage")),
    ("Обслуживание",
     "Освобождение места и памяти. Обе операции необратимы, поэтому требуют "
     "ключа администратора.",
     ("/api/maintenance",)),
    ("Мониторинг",
     "Метрики, пробы состояния и тревоги. Подробный разбор каждой метрики — "
     "в отдельном руководстве по мониторингу; здесь только интерфейс.",
     ("/api/monitoring", "/api/metrics")),
]

#: Маршруты, открытые без ключа.
OPEN_ROUTES = {"/api/health"}
#: Маршруты, выборка которых сужается до заданий самого ключа.
SCOPED_ROUTES = {"/api/jobs", "/api/analytics", "/api/analytics/{section}",
                 "/api/events"}
#: Маршруты, требующие ключа администратора.
ADMIN_ROUTES = {
    ("/api/keys", "get"), ("/api/keys", "post"), ("/api/keys/{preview}", "delete"),
    ("/api/logs", "get"),
    ("/api/settings", "put"), ("/api/settings/save", "post"),
    ("/api/settings/reset", "post"),
    ("/api/maintenance/cleanup", "post"), ("/api/maintenance/unload-models", "post"),
    ("/api/queue/concurrency", "post"), ("/api/queue/clear", "post"),
    ("/api/models/{model_id}", "delete"),
    ("/api/monitoring/alerts/rules", "put"),
    ("/api/monitoring/alerts/rules/reset", "post"),
    ("/api/monitoring/targets", "put"), ("/api/monitoring/targets/test", "post"),
    # Учётные записи заводит и правит только администратор. Без этих строк
    # правило «изменяющий метод — значит admin или user» приписывало бы
    # обычному пользователю право заводить себе администратора: справочник
    # обещал бы доступ, которого сервер не даёт.
    ("/api/users", "get"), ("/api/users", "post"),
    ("/api/users/{user_id}", "patch"), ("/api/users/{user_id}", "delete"),
}
#: Вход и выход ключа не требуют вовсе: это и есть способ его получить.
OPEN_AUTH_ROUTES = {"/api/auth/login", "/api/auth/logout"}
#: Маршруты, доступные без ключа при monitoring_public: true.
MONITORING_OPEN = {
    "/api/monitoring/metrics", "/api/monitoring/metrics.json",
    "/api/monitoring/health", "/api/monitoring/live", "/api/monitoring/ready",
    "/api/monitoring/startup", "/api/monitoring/config/prometheus",
    "/api/monitoring/config/prometheus-scrape", "/api/monitoring/config/grafana",
    "/api/monitoring/config/zabbix",
}
#: Маршруты, меняющие данные: ключу нужна роль admin или user, но не readonly.
WRITE_METHODS = {"post", "put", "delete", "patch"}


def access_note(path: str, method: str) -> str:
    if (path, method) in ADMIN_ROUTES:
        return "ключ с ролью **admin**"
    if path in OPEN_AUTH_ROUTES:
        return "без ключа: это и есть вход"
    if path == "/api/auth/password":
        return "учётная запись, вошедшая по логину и паролю"
    if path == "/api/process-call":
        return "ключ с правом записи; принимается и полем `api_key` в теле"
    if path in OPEN_ROUTES:
        return "без ключа"
    if path in SCOPED_ROUTES and method == "get":
        return "любой ключ; выборка сужается до его заданий"
    if path in MONITORING_OPEN:
        return "без ключа при `monitoring_public: true`, иначе любой ключ"
    if method in WRITE_METHODS:
        return "ключ с правом записи (**admin** или **user**)"
    return "любой действующий ключ"


INTRO = """
Программный интерфейс ASR Hub — обычный HTTP с телом в JSON. Отдельного
клиента для работы с ним не нужно: хватает `curl`, `requests` или того, что
уже есть в вашем языке.

## Что нужно знать до первого запроса

**Адрес.** Все маршруты начинаются с `/api/`. Веб-интерфейс лежит в корне и
пользуется теми же маршрутами — всё, что делает интерфейс, можно повторить
запросом.

**Ключ доступа.** Передаётся заголовком `X-API-Key` либо
`Authorization: Bearer <ключ>`. Первый ключ создаётся при первом запуске и
лежит в файле `api-key.txt` в каталоге данных. Ключ в строке запроса
(`?api_key=`) тоже принимается — ради клиентов, которые не умеют заголовки, —
но пользоваться этим не стоит: адрес попадает в журнал обратного прокси, в
историю браузера и в заголовок `Referer`.

**Роли.** У ключа три роли: `admin` — всё; `user` — свои задания и чтение;
`readonly` — только чтение. Роль указана в столбце «Доступ» у каждого
маршрута.

**Область видимости.** Ключ без прав администратора видит только свои
задания — и в карточке, и в списке, и в аналитике, и в ленте событий.
Параметр `owner` для него ничего не открывает: выборка сужается в любом
случае. Журнал сервера доступен только администратору: записи несут имена
чужих файлов и трассировки, а разделить их по владельцам нечем.

**Подразделения.** Ключи с одинаковым `group` видят задания друг друга —
всюду, где действует область видимости. Ключ без `group` видит только своё.
Подразделение не меняет роль: `readonly` в подразделении читает задания
коллег, но по-прежнему ничего не создаёт.

**Ограничение частоты.** Считается по ключу, не по адресу. При превышении
приходит 429 с заголовком `Retry-After` и подсказкой в теле.

**Квоты.** Отдельно от частоты у ключа есть суточные пределы: число заданий
(`quota_jobs_per_day`), часы звука (`quota_audio_hours_per_day`) и объём
загруженного (`quota_storage_gb`). Ноль означает «без ограничения»,
администратор не ограничен, окно скользящее — квота восстанавливается сама.
Расход виден в `GET /api/usage` до отказа; при исчерпании приходит 429 с
кодом `quota_exceeded` и именем поля, которое надо поднять.

## Формат ошибок

Ошибка — всегда объект с одинаковым набором полей, а не голая строка:

```json
{
  "detail": {
    "code": "model_not_found",
    "message": "Модель «gigaam-v4» не найдена.",
    "hint": "Похожие: gigaam-v3-e2e-rnnt, gigaam-v2-ctc. Полный список: GET /api/models",
    "retryable": false
  }
}
```

`code` пригоден для машинной обработки и не меняется между версиями,
`message` и `hint` написаны для человека, `retryable` подсказывает, имеет ли
смысл повторить запрос. Коды состояния обычные: 400 — неверный запрос,
401 — нет ключа, 403 — ключу не хватает прав, 404 — нет объекта,
409 — состояние не позволяет (например, отмена уже завершённого задания),
413 — файл больше `max_upload_mb`, 429 — превышена частота,
503 — сервер ещё запускается.

## Живые события

Помимо HTTP есть WebSocket на `/ws`: ход выполнения, завершение и ошибки
приходят сразу, без опроса. Браузерный WebSocket не умеет заголовки, поэтому
сначала берётся одноразовый билет:

```bash
TICKET=$(curl -s -X POST -H "X-API-Key: $КЛЮЧ" \\
         http://сервер:8080/api/auth/ticket | jq -r .ticket)
websocat "ws://сервер:8080/ws?ticket=${TICKET}"
```

Билет живёт минуту и гасится при первом подключении. Сторонние клиенты
могут по-прежнему подключаться с ключом: `ws://сервер:8080/ws?api_key=$КЛЮЧ`.

Сообщения приходят типизированными: `hello` при подключении (в нём состояние
очереди и последние события), `job.progress` по ходу распознавания,
`job.completed`, `job.failed`, `job.retry`, `job.queued`, `queue`. Клиент
может отправить `ping` (ответ `pong`) и `status` (ответ — состояние очереди).

## Распознавание на лету

Второй WebSocket — `/api/stream` — принимает звук кусками и отдаёт текст по
ходу, не дожидаясь конца записи. Ключ предъявляется так же, билетом.
Управление — сообщения JSON, звук — двоичные кадры:

```
-> {"type": "config", "format": "auto", "stream_window_s": 3}
<- {"type": "ready", "mode": "window", "window_s": 3.0, "note": "..."}
-> «двоичный кадр со звуком»
<- {"type": "partial", "text": "коллеги добрый", "start": 0, "end": 3.1}
-> {"type": "finish"}
<- {"type": "final", "text": "Коллеги, добрый день.", "start": 0, "end": 4.2}
<- {"type": "done", "duration_s": 4.2, "text": "Коллеги, добрый день."}
```

Поле `mode` в `ready` говорит, как сессия работает: `native` — движок держит
состояние между кусками и звук распознаётся один раз (Vosk); `window` —
движок состояния не держит, поэтому накопленный звук распознаётся заново
каждые `stream_window_s` секунд. `partial` — черновик, который заменяется
следующим целиком; `final` — то, что уже не изменится, его дописывают.

Формат `pcm_s16le` (моно, 16 кГц, 16 бит) не требует ничего; `auto`
пропускает через ffmpeg любой контейнер, но присылать надо один непрерывный
поток, а не отдельные файлы. Сессия длиннее часа прерывается: для длинных
записей есть `POST /api/jobs`. Ключу `readonly` поток закрыт — это работа, а
не чтение. Готовый клиент: `examples/stream_microphone.py`.
"""

TAIL = """
## Типовой сценарий целиком

Поставить файл, дождаться и забрать текст — четыре запроса:

```bash
KEY=$(cat /var/lib/asrhub/api-key.txt)
HOST=http://сервер:8080

# 1. Поставить в очередь
JOB=$(curl -s -H "X-API-Key: $KEY" \\
        -F 'file=@совещание.mp3' \\
        -F 'model=gigaam-v3-e2e-rnnt' \\
        -F 'diarization_enabled=true' \\
        "$HOST/api/jobs" | jq -r .id)

# 2. Дождаться завершения
while true; do
  STATUS=$(curl -s -H "X-API-Key: $KEY" "$HOST/api/jobs/$JOB" | jq -r .status)
  [ "$STATUS" = completed ] && break
  [ "$STATUS" = failed ] && { echo "не получилось"; exit 1; }
  sleep 5
done

# 3. Забрать текст
curl -s -H "X-API-Key: $KEY" "$HOST/api/jobs/$JOB/download?fmt=txt" -o совещание.txt

# 4. Убрать за собой (необязательно: чистка идёт и по расписанию)
curl -s -X DELETE -H "X-API-Key: $KEY" "$HOST/api/jobs/$JOB"
```

Опрос в шаге 2 нужен не всегда: подписка на `/ws` приносит `job.completed`
сама, и цикл со `sleep` не нужен вовсе.

## Готовые клиенты

В составе поставки есть `scripts/client/asrctl` — оболочка над этими же
маршрутами для командной строки, и примеры на Python в каталоге `examples/`.
Разбор клиентов — в главе «Клиенты и удалённая работа».

## Автономный справочник

У сервера есть собственная страница `/api/reference`: та же схема OpenAPI,
отрисованная без единого внешнего ресурса. Она работает в закрытом контуре,
где `/api/docs` и `/api/redoc` бесполезны — те подгружают скрипты из
интернета.
"""

EXAMPLES: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/jobs", "post"): {
        "curl": f"curl -H 'X-API-Key: {K}' \\\n"
                "     -F 'file=@совещание.mp3' \\\n"
                "     -F 'model=gigaam-v3-e2e-rnnt' \\\n"
                "     -F 'diarization_enabled=true' \\\n"
                f"     {HOST}/api/jobs",
        "note": "Файл передаётся как `multipart/form-data`. Любой параметр из "
                "`GET /api/params` можно передать полем формы — он подменит "
                "серверное значение только для этого задания.",
    },
    ("/api/jobs", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' "
                f"'{HOST}/api/jobs?status=completed&limit=5&light=true'",
        "show": "/api/jobs?status=completed&limit=2&light=true", "limit": 1400,
        "note": "Показан облегчённый список (`light=true`): только поля для "
                "таблицы. Без него в каждом задании приходят ещё расшифровка "
                "целиком и разбор по сегментам — на сотне часовых записей это "
                "единицы мегабайт вместо десятков килобайт.",
    },
    ("/api/usage", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/usage",
        "show": "/api/usage",
        "note": "Расход за скользящие сутки и пределы ключа. `null` в "
                "`limits` означает, что по этому измерению ограничения нет. "
                "У ключа в подразделении расход считается на всё "
                "подразделение — иначе квоту обходили бы вторым ключом.",
    },
    ("/api/jobs/{job_id}", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/jobs/j_a1b2c3",
        "note": "Пока задание выполняется, в ответе есть `progress` и `stage` — "
                "по ним рисуется полоса хода в интерфейсе.",
    },
    ("/api/jobs/{job_id}/download", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' \\\n"
                f"     '{HOST}/api/jobs/j_a1b2c3/download?fmt=srt' -o субтитры.srt",
        "note": "Форматы: `txt`, `srt`, `vtt`, `json`, `csv`, `tsv`, `docx`, "
                "`md`. Формат `json` содержит всё: сегменты, слова, уверенность, "
                "говорящих и метаданные задания.",
    },
    ("/api/jobs/batch", "post"): {
        "curl": f"curl -H 'X-API-Key: {K}' \\\n"
                "     -F 'files=@день1.mp3' -F 'files=@день2.mp3' \\\n"
                f"     -F 'model=gigaam-v3-e2e-rnnt' {HOST}/api/jobs/batch",
        "note": "Общий объём ограничен параметром `max_upload_mb`, число "
                "файлов — `max_batch_files`. Задания получают общий "
                "`batch_id`, по нему их удобно отбирать в списке.",
    },
    ("/api/queue", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/queue",
        "show": "/api/queue", "limit": 900,
    },
    ("/api/queue/concurrency", "post"): {
        "curl": f"curl -X POST -H 'X-API-Key: {K}' -H 'Content-Type: application/json' \\\n"
                f"     -d '{{\"workers\": 4}}' {HOST}/api/queue/concurrency",
        "note": "Уменьшение числа воркеров не обрывает начатые задания: "
                "лишние потоки помечаются на вывод и уходят, доработав своё.",
    },
    ("/api/models", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' '{HOST}/api/models?language=ru&streaming=true'",
        "show": "/api/models?language=ru&limit=2", "limit": 1200,
    },
    ("/api/models/recommended", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/models/recommended",
        "show": "/api/models/recommended", "limit": 900,
    },
    ("/api/models/{model_id}/download", "post"): {
        "curl": f"curl -X POST -H 'X-API-Key: {K}' \\\n"
                f"     {HOST}/api/models/gigaam-v3-e2e-rnnt/download",
        "note": "Загрузка идёт в фоне; ход виден через `GET "
                "/api/models/{model_id}/status` и в ленте событий WebSocket.",
    },
    ("/api/params", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' '{HOST}/api/params?group=decoding'",
        "show": "/api/params?group=decoding", "limit": 1200,
        "note": "У каждого параметра есть описание, диапазон, значение по "
                "умолчанию, рекомендация и примеры — из этого же справочника "
                "строится раздел «Настройки» в интерфейсе.",
    },
    ("/api/presets", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/presets",
        "show": "/api/presets", "limit": 1000,
    },
    ("/api/settings", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/settings",
        "show": "/api/settings", "limit": 900,
    },
    ("/api/settings", "put"): {
        "curl": f"curl -X PUT -H 'X-API-Key: {K}' -H 'Content-Type: application/json' \\\n"
                "     -d '{\"vad_threshold\": 0.45, \"beam_size\": 8}' \\\n"
                f"     {HOST}/api/settings",
        "note": "Значения проверяются по каталогу параметров: неизвестный ключ "
                "или значение вне диапазона отвергаются с указанием, что "
                "именно не так. Изменения живут до перезапуска, пока не вызван "
                "`POST /api/settings/save`.",
    },
    ("/api/system", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' {HOST}/api/system",
        "show": "/api/system", "limit": 1100,
    },
    ("/api/health", "get"): {
        "curl": f"curl {HOST}/api/health",
        "show": "/api/health", "limit": 400,
        "note": "Единственный маршрут без ключа — на него удобно вешать "
                "проверку балансировщика.",
    },
    ("/api/analytics", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' '{HOST}/api/analytics?period=week'",
        "show": "/api/analytics?period=week", "limit": 1100,
    },
    ("/api/logs", "get"): {
        "curl": f"curl -H 'X-API-Key: {K}' '{HOST}/api/logs?level=ERROR&limit=20'",
        "show": "/api/logs?level=ERROR&limit=3", "limit": 800,
    },
    ("/api/auth/ticket", "post"): {
        "curl": f"curl -X POST -H 'X-API-Key: {K}' {HOST}/api/auth/ticket",
        "note": "Билет живёт минуту и тратится при первом подключении к "
                "`/ws`. Он не открывает доступ к HTTP-маршрутам.",
    },
    ("/api/keys", "post"): {
        "curl": f"curl -X POST -H 'X-API-Key: {K}' -H 'Content-Type: application/json' \\\n"
                "     -d '{\"name\": \"интеграция-1С\", \"role\": \"user\"}' \\\n"
                f"     {HOST}/api/keys",
        "note": "Полный ключ показывается один раз — в ответе на создание. "
                "Дальше виден только его префикс.",
    },
    ("/api/maintenance/cleanup", "post"): {
        "curl": f"curl -X POST -H 'X-API-Key: {K}' {HOST}/api/maintenance/cleanup",
        "note": "Удаляет задания старше `result_retention_days` вместе с их "
                "файлами. То же делает уборщик по расписанию.",
    },
    ("/api/metrics", "get"): {
        "curl": f"curl {HOST}/api/metrics",
        "note": "Полный снимок в формате Prometheus — тот же, что у "
                "`/api/monitoring/metrics`. Прежние имена метрик отдаются "
                "рядом с новыми как устаревшие псевдонимы.",
    },
    ("/api/monitoring/metrics", "get"): {
        "curl": f"curl '{HOST}/api/monitoring/metrics?format=influx'",
        "note": "Форматы: `prometheus`, `openmetrics`, `json`, `otlp`, "
                "`influx`, `graphite`, `zabbix`, `csv`.",
    },
    ("/api/monitoring/health", "get"): {
        "curl": f"curl {HOST}/api/monitoring/health",
        "show": "/api/monitoring/health", "limit": 900,
        "note": "Код ответа 200 при `ok` и `warning`, 503 при `degraded` и "
                "`critical` — проверку можно навесить, не разбирая тело.",
    },
}


def section_of(path: str) -> int:
    """Номер раздела для маршрута; самый длинный подходящий префикс."""
    best, best_len = len(SECTIONS), -1
    for index, (_, _, prefixes) in enumerate(SECTIONS):
        for prefix in prefixes:
            if (path == prefix or path.startswith(prefix + "/")) and len(prefix) > best_len:
                best, best_len = index, len(prefix)
    return best


def main(argv: list[str]) -> int:
    base = (argv[1] if len(argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
    key = find_key(argv[2] if len(argv) > 2 else "")

    schema = load_schema(base)
    if schema is None:
        return 1

    paths = {p: {m: op for m, op in ops.items() if m in METHOD_ORDER}
             for p, ops in schema["paths"].items() if p.startswith("/api/")}
    paths = {p: ops for p, ops in paths.items() if ops}
    total = sum(len(ops) for ops in paths.values())

    out: list[str] = []
    add = out.append

    add("# Программный интерфейс\n")
    add("Полный справочник по всем маршрутам сервера: что принимает каждый, "
        "что возвращает, какой нужен ключ и как выглядит настоящий ответ.\n")
    add(f"Всего маршрутов: **{len(paths)}**, операций: **{total}**. Справочник "
        "собран из схемы OpenAPI работающего сервера, а примеры ответов сняты "
        "с него же, поэтому расходиться с действительностью им негде.\n")
    add(INTRO)

    add("## Обзор всех маршрутов\n")
    out.extend(overview_table(paths, access_note))

    # Разносим маршруты по разделам; ничего не потеряется — то, что не попало
    # ни в один префикс, соберётся в «Прочем».
    buckets: dict[int, list[str]] = {}
    for path in sorted(paths):
        buckets.setdefault(section_of(path), []).append(path)

    for index, (title, description, _) in enumerate(SECTIONS):
        members = buckets.get(index, [])
        if not members:
            continue
        add(f"## {title}\n")
        add(f"{description}\n")
        for path in members:
            for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
                out.extend(operation_block(path, method, paths[path][method], base, key,
                                           access=access_note, examples=EXAMPLES))

    leftovers = buckets.get(len(SECTIONS), [])
    if leftovers:
        add("## Прочие маршруты\n")
        add("Маршруты, не отнесённые ни к одному разделу. Если здесь что-то "
            "появилось, стоит дописать раздел в `docs/generate_api_full.py`.\n")
        for path in leftovers:
            for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
                out.extend(operation_block(path, method, paths[path][method], base, key,
                                           access=access_note, examples=EXAMPLES))

    add(TAIL)

    target = DOCS / "api-reference.md"
    text = "\n".join(out) + "\n"
    target.write_text(text, encoding="utf-8")
    print(f"  {target.name} — {len(text.splitlines())} строк, "
          f"{len(text) // 1024} КБ, операций {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
