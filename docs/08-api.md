# Программный интерфейс

HTTP-интерфейс покрывает всё, что умеет веб-интерфейс: он сам построен поверх этого же API. Обмен в JSON, загрузка файлов — `multipart/form-data`, события — по WebSocket.

Живой справочник по адресу `/api/reference` собирается сервером из схемы OpenAPI и работает без интернета. Машинно-читаемая схема — `/api/openapi.json`.

Эта глава — введение: как устроен обмен и как решаются типовые задачи. Полный перечень маршрутов со всеми параметрами, правами доступа и настоящими примерами ответов вынесен в отдельный документ — «ASR Hub — программный интерфейс». Он собирается с работающего сервера командой `bash docs/build-api.sh`, поэтому не может разойтись с тем, что сервер отдаёт на самом деле.

## Аутентификация

Ключ передаётся одним из трёх способов:

```bash
curl -H "X-API-Key: ah_ваш_ключ" ...                     # рекомендуемый
curl -H "Authorization: Bearer ah_ваш_ключ" ...
curl "http://сервер:8080/api/jobs?api_key=ah_ваш_ключ"   # только для WebSocket и ссылок
```

Третий способ оставлен для случаев, когда заголовок задать негде (адрес WebSocket, ссылка на скачивание в браузере). ⚠️ Ключ в адресе попадает в журналы прокси — в остальных случаях пользуйтесь заголовком.

Роли ключей:

| Роль | Права |
|---|---|
| `readonly` | чтение: списки, карточки, выгрузка результатов, аналитика |
| `user` | плюс создание, отмена и повтор заданий |
| `admin` | плюс настройки, ключи, обслуживание, управление очередью |

Создание ключа:

```bash
curl -X POST http://сервер:8080/api/keys \
  -H "X-API-Key: ah_ключ_администратора" -H "Content-Type: application/json" \
  -d '{"name": "интеграция CRM", "role": "user", "rate_limit": 600}'
```

Полное значение ключа возвращается один раз при создании — дальше видны только имя и первые символы. Аутентификацию можно отключить целиком (`auth_enabled: false`), но только в доверенной сети.

## Задания

### Поставить файл в очередь

```
POST /api/jobs        Content-Type: multipart/form-data
```

| Поле | Тип | Описание |
|---|---|---|
| `file` | файл | аудио или видео, обязательно |
| `settings` | JSON-строка | параметры задания; всё, что не указано, берётся из настроек сервера |
| `priority` | число | 1–100, по умолчанию 50 |
| `group_id` | строка | объединить с другими заданиями в группу |
| `tags` | строка | метки через запятую, для фильтрации |
| `reference_text` | строка | эталонная расшифровка для расчёта WER |
| `webhook_url` | строка | адрес для уведомления о завершении |

```bash
curl -X POST http://сервер:8080/api/jobs \
  -H "X-API-Key: ключ" \
  -F "file=@совещание.mp3" \
  -F 'settings={"model":"gigaam-v3-e2e-rnnt","language":"ru","diarization_enabled":true,"output_formats":["txt","srt","json"]}' \
  -F "priority=70" -F "tags=совещания,отдел-продаж"
```

```json
{
  "id": "job_7f3a9c21",
  "status": "queued",
  "filename": "совещание.mp3",
  "media_duration_s": 3612.4,
  "model": "gigaam-v3-e2e-rnnt",
  "priority": 70,
  "created_at": 1756612800.12,
  "progress": 0.0,
  "stage": "в очереди"
}
```

### Несколько файлов одной группой

```
POST /api/jobs/batch
```

```bash
curl -X POST http://сервер:8080/api/jobs/batch \
  -H "X-API-Key: ключ" \
  -F "files=@1.mp3" -F "files=@2.mp3" -F "files=@3.mp3" \
  -F 'settings={"model":"gigaam-v3-ctc"}' -F "priority=20"
```

### Список заданий

```
GET /api/jobs?status=completed&model=gigaam-v3-rnnt&limit=50&offset=0
```

| Параметр | Описание |
|---|---|
| `status` | `queued`, `running`, `completed`, `failed`, `cancelled`, `retry`, `paused`; `active` — все незавершённые; можно перечислить через запятую |
| `owner`, `model`, `group_id` | фильтры по значению |
| `search` | поиск по имени файла и тексту расшифровки |
| `since_hours` | только за последние N часов |
| `limit`, `offset` | постраничный вывод, до 500 за раз |
| `order` | сортировка, например `created_at DESC`, `media_duration_s ASC` |

### Карточка задания

```
GET /api/jobs/{id}?with_segments=true
```

Возвращает всё: статус, прогресс, стадию, времена по этапам, параметры, метрики, текст, ленту событий и — по запросу — сегменты с таймкодами, говорящими и уверенностью.

Вместе с ними приходит полоса громкости: `waveform` — разобранные точки, `waveforms` — те же данные в виде массива JSON-строк (см. следующий раздел). На часовой записи это сотни килобайт, поэтому если карточка опрашивается часто, добавьте `with_waveform=false`.

### Полоса громкости

```
GET /api/jobs/{id}/waveform
GET /api/jobs/{id}/waveform?fmt=phone_asr
```

Огибающая записи: один замер на интервал, `amplitude` — **средний модуль отсчёта** на этом интервале. Величина безразмерная, от 0 до 1: тишина даёт около 0.001, обычная речь — 0.15…0.25. Это не пик и не децибелы; среднее по модулю выбрано потому, что пик реагирует на один щелчок и делает полосу нечитаемой, а глаз ждёт от «полоски громкости» именно среднего уровня.

Кривых может быть несколько. Стереозапись даёт по кривой на канал, монозапись с диаризацией — по кривой на говорящего (вне своих реплик у него нули, поэтому наложенные полосы читаются как диалог), в остальных случаях кривая одна с меткой `all`.

```json
{
  "id": "job_7f3a9c21",
  "interval_s": 1.0,
  "duration_s": 128.4,
  "curves": [
    {
      "audio_waveform": [{"time": 0.0, "amplitude": 0.000638},
                         {"time": 1.0, "amplitude": 0.015378}],
      "sample_rate": 16000,
      "speaker": 0,
      "label": "Оператор"
    }
  ]
}
```

Форма `fmt=phone_asr` отдаёт `{"waveforms": ["{\"audio_waveform\": …}", …]}` — **массив JSON-строк**, а не объектов, и без поля `label`. Так это поле устроено в phone_asr, и приёмники, написанные под него, читают ответ ASR Hub без единой правки. То же поле есть в карточке задания и, если включить параметр `webhook_waveform`, в теле уведомления о завершении.

Шаг задаётся параметром `waveform_interval_s` (по умолчанию 1 секунда), расчёт целиком отключается параметром `waveform_enabled`.

### Сегменты отдельно

```
GET /api/jobs/{id}/segments
```

### Скачать результат

```
GET /api/jobs/{id}/download?fmt=srt
```

Форматы: `txt`, `md`, `json`, `srt`, `vtt`, `ass`, `tsv`, `csv`, `docx`.

```bash
curl -H "X-API-Key: ключ" \
  "http://сервер:8080/api/jobs/job_7f3a9c21/download?fmt=srt" -o совещание.srt
```

Имя файла в заголовке `Content-Disposition` кодируется по RFC 5987, поэтому кириллические имена скачиваются корректно.

### Управление заданием

```
POST   /api/jobs/{id}/cancel
POST   /api/jobs/{id}/retry
POST   /api/jobs/{id}/pause
POST   /api/jobs/{id}/resume
POST   /api/jobs/{id}/priority     {"priority": 90}
POST   /api/jobs/{id}/top
POST   /api/jobs/{id}/bottom
POST   /api/jobs/{id}/reference    {"text": "эталонная расшифровка"}
DELETE /api/jobs/{id}
```

## Очередь

```
GET  /api/queue
POST /api/queue/pause
POST /api/queue/resume
POST /api/queue/clear
POST /api/queue/retry-failed?limit=100
POST /api/queue/concurrency        {"workers": 3}
```

## Каталог

```
GET /api/catalog                     всё сразу: модели, движки, параметры, пресеты
GET /api/models?language=ru&engine=gigaam&license=MIT
GET /api/models/recommended?limit=8
GET /api/models/{id}
GET /api/models/{id}/status          загружены ли веса и сколько занимают
POST /api/models/{id}/download
DELETE /api/models/{id}
GET /api/engines                     состояние движков и причины недоступности
GET /api/params?engine=faster_whisper&group=vad
GET /api/presets
POST /api/presets/{id}/apply
```

`GET /api/params` отдаёт то же, что раздел [04. Справочник параметров](04-parameters.md): описание, рекомендацию, примеры, влияние, ограничения и синонимы каждого параметра. Удобно, если вы строите свой интерфейс поверх сервера — подсказки не придётся писать заново.

## Настройки

```
GET  /api/settings
PUT  /api/settings                 {"beam_size": 8, "vad_threshold": 0.45}
POST /api/settings/save            записать текущие значения в config.yaml
POST /api/settings/reset
```

`PUT` проверяет значения по каталогу и на недопустимое отвечает 400 с объяснением, что именно не так и какие значения допустимы.

## Система и аналитика

```
GET  /api/health                   без ключа: живость и версия
GET  /api/system                   железо, движки, диск, время работы
GET  /api/analytics?period=week
GET  /api/analytics/{раздел}?period=month
GET  /api/logs?level=error&search=cuda&limit=200
GET  /api/events?limit=100
GET  /api/metrics                  формат Prometheus
GET  /api/keys
POST /api/keys                     {"name": "…", "role": "user"}
DELETE /api/keys/{первые_символы}
POST /api/maintenance/cleanup
POST /api/maintenance/unload-models
```

## События по WebSocket

Браузерный WebSocket не умеет отправлять заголовки, поэтому ключ пришлось бы писать в адрес — а он попадает в историю браузера, в журнал обратного прокси и в заголовок `Referer`. Вместо ключа берётся одноразовый билет: он живёт минуту, гасится при первом подключении и не открывает доступ к HTTP-маршрутам.

```javascript
// 1. Билет — обычным запросом с заголовком
const { ticket } = await fetch('/api/auth/ticket', {
  method: 'POST',
  headers: { 'X-API-Key': ключ },
}).then((r) => r.json());

// 2. Подключение по билету
const ws = new WebSocket(`ws://сервер:8080/ws?ticket=${encodeURIComponent(ticket)}`);

ws.onmessage = (event) => {
  // Поля события лежат на верхнем уровне сообщения, вложенного data нет.
  const message = JSON.parse(event.data);
  switch (message.type) {
    case 'job.progress':  console.log(message.id, message.stage, message.progress); break;
    case 'job.completed': console.log('готово', message.id, 'RTF', message.rtf); break;
    case 'job.failed':    console.error(message.error.message, message.error.hint); break;
  }
};

setInterval(() => ws.send('ping'), 30000);   // держим соединение живым
```

Сторонние клиенты — те, что умеют задавать заголовки или живут в закрытом контуре, — могут по-прежнему подключаться с ключом: `ws://сервер:8080/ws?api_key=$КЛЮЧ`.

**Типы событий**

| Событие | Когда приходит | Полезные поля |
|---|---|---|
| `hello` | сразу после подключения | `queue` — состояние очереди, `history` — последние 20 событий |
| `job.queued` | задание принято | `id`, `filename` |
| `job.started` | задание взято в работу | `id`, `filename` |
| `job.progress` | по ходу распознавания | `id`, `progress` (0…1), `stage` |
| `job.completed` | задание готово | `id`, `rtf` |
| `job.failed` | задание не удалось | `id`, `error` — объект с `code`, `message`, `hint` |
| `job.retry` | будет повтор после сбоя | `id`, `attempt`, `delay_s`, `error` |
| `job.cancelled` | задание отменено | `id` |
| `job.cached` | результат взят из кеша | `id`, `source` — задание, из которого взят результат |
| `queue.paused` | очередь приостановлена | — |
| `queue.resumed` | очередь возобновлена | — |
| `queue` | ответ на команду `status` | состояние очереди целиком |
| `pong` | ответ на команду `ping` | — |

В каждом сообщении есть `type` и `ts` — время события. Клиент может отправить серверу две команды: `ping` (ответ `pong`, чтобы соединение не закрыл прокси) и `status` (ответ `queue`).

## Формат ошибок

Все ошибки приходят в одном виде:

```json
{
  "detail": {
    "code": "out_of_memory",
    "message": "Недостаточно памяти на устройстве GPU.",
    "hint": "Уменьшите batch_size или выберите модель полегче. Задание будет повторено автоматически с меньшим пакетом.",
    "retryable": true,
    "context": {"device": "cuda:0", "requested": "11.2 ГБ"}
  }
}
```

`retryable` говорит, имеет ли смысл повторять запрос. `hint` — не украшение: это конкретное действие, которое чаще всего решает проблему.

| Код | HTTP | Повторять | Значение |
|---|---|---|---|
| `config_error` | 400 | нет | недопустимое значение параметра |
| `audio_error` | 400 | нет | файл нечитаем или без звуковой дорожки |
| `unsupported_feature` | 400 | нет | движок не умеет запрошенного |
| `auth_error` | 401 | нет | ключ отсутствует или недействителен |
| `forbidden` | 403 | нет | недостаточно прав |
| `gated_model` | 403 | нет | нужно принять условия у правообладателя весов |
| `job_not_found` | 404 | нет | нет такого задания |
| `model_not_found` | 404 | нет | нет такой модели (в ответе — похожие имена) |
| `job_cancelled` | 409 | нет | задание уже отменено |
| `file_too_large` | 413 | нет | превышен `max_upload_mb` |
| `audio_too_long` | 413 | нет | превышен `audio_max_duration_s` |
| `unsupported_format` | 415 | нет | расширение не поддерживается |
| `no_speech` | 422 | нет | речь не найдена |
| `queue_full` | 429 | да | очередь переполнена |
| `rate_limited` | 429 | да | превышен лимит частоты |
| `engine_error` | 500 | да | сбой внутри движка |
| `dependency_missing` | 503 | нет | не установлен пакет движка |
| `binary_missing` | 503 | нет | не найдена внешняя программа (обычно ffmpeg) |
| `hardware_error` | 503 | да | проблема с устройством или драйвером |
| `out_of_memory` | 503 | да | не хватило памяти |
| `model_not_downloaded` | 503 | да | веса не загружены |
| `model_load_error` | 503 | да | веса есть, но не грузятся |
| `job_timeout` | 504 | да | превышен `job_timeout_s` |
| `storage_error` | 507 | да | нет места на диске |

## Пример на Python

```python
import time
import requests

SERVER = "http://сервер:8080"
HEADERS = {"X-API-Key": "ah_ваш_ключ"}


def transcribe(path, **settings):
    """Отправляет файл, дожидается результата, возвращает субтитры."""
    import json
    with open(path, "rb") as fh:
        response = requests.post(
            f"{SERVER}/api/jobs", headers=HEADERS,
            files={"file": fh},
            data={"settings": json.dumps(settings or {"model": "gigaam-v3-e2e-rnnt"})},
            timeout=300)
    if response.status_code != 200:
        error = response.json()["detail"]
        raise RuntimeError(f"{error['code']}: {error['message']}\n{error.get('hint', '')}")

    job_id = response.json()["id"]
    while True:
        job = requests.get(f"{SERVER}/api/jobs/{job_id}", headers=HEADERS, timeout=30).json()
        if job["status"] == "completed":
            break
        if job["status"] == "failed":
            raise RuntimeError(job["error_message"])
        time.sleep(3)

    return requests.get(f"{SERVER}/api/jobs/{job_id}/download",
                        params={"fmt": "srt"}, headers=HEADERS, timeout=60).text


print(transcribe("совещание.mp3", model="gigaam-v3-e2e-rnnt", diarization_enabled=True))
```

Ожидание в цикле подходит для скрипта; в постоянно работающем сервисе используйте webhook или WebSocket, чтобы не опрашивать сервер впустую.
