# Примеры работы с ASR Hub

Каждый файл запускается сам по себе и рассчитан на то, что его прочтут, а не
только выполнят: комментарии объясняют, почему сделано именно так.

Перед запуском задайте адрес и ключ:

```bash
export ASRHUB_SERVER=http://127.0.0.1:8080
export ASRHUB_KEY="$(cat /var/lib/asrhub/api-key.txt)"
```

| Файл | Что показывает |
|---|---|
| `transcribe.py` | Простейший случай: поставить файл, дождаться, забрать текст |
| `batch_folder.py` | Каталог целиком, с ограничением одновременных отправок |
| `live_events.py` | Подписка на события WebSocket вместо опроса |
| `monitoring_pull.py` | Снятие метрик и разбор порогов из справочника |
| `stream_microphone.py` | Распознавание на лету: звук кусками, текст по ходу речи |
| `telephony_push_call.py` | Передать звонок с записью из своей интеграции, как диалплан: повторы, 409/429/503, проба |
| `telephony_export_calls.py` | Выгрузить журнал звонков с АТС в CSV для сверки со своей статистикой |

Зависимости: `requests`, `websockets` для `live_events.py` и
`websocket-client` для `stream_microphone.py` (в режиме микрофона — ещё
`sounddevice`). Ставится одной строкой:
`pip install requests websockets websocket-client`.
