# ASR Hub

Сервер распознавания речи для работы с удалённого компьютера: 17 движков, 72 свободные модели, веб-интерфейс с очередью и аналитикой, скрипты установки для Linux, macOS и Windows.

**Документация:** [docs/00-README.md](docs/00-README.md) — 16 разделов, схемы и снимки экрана. Тот же комплект в Word: `build/ASR Hub — документация.docx`.

## Быстрый старт

```bash
# проверить, что всё работает, без загрузки моделей
cd server && python3 -m pip install fastapi uvicorn python-multipart
python3 -m asrhub --host 127.0.0.1 --port 8080
```

Откройте `http://127.0.0.1:8080`, выберите модель `demo-simulator` и отправьте любой файл — задание пройдёт весь путь через очередь, конвейер и выгрузку, не загружая ни одного гигабайта весов.

Полноценная установка:

```bash
bash scripts/install.sh --profile standard --dry-run   # посмотреть план
bash scripts/install.sh --profile standard             # установить
```

На Windows — `powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile standard`.
В контейнере — `cd docker && docker compose --profile gpu up -d`.

## Что внутри

| Каталог | Что там |
|---|---|
| `server/asrhub/` | сервер: API, очередь, движки, конвейер обработки |
| `server/asrhub/catalog/` | каталог моделей, параметров и пресетов — единственный источник истины |
| `server/asrhub/engines/` | 17 адаптеров: GigaAM, Whisper, NeMo, Vosk, T-one и другие |
| `server/asrhub/pipeline/` | аудио, VAD, диаризация, постобработка, выгрузка, метрики |
| `server/asrhub/web/` | веб-интерфейс: без сборки и без внешних зависимостей |
| `scripts/` | установка, удаление, обновление, служба, диагностика (`.sh` и `.ps1`) |
| `scripts/client/` | `asrctl` — консольный клиент |
| `docker/` | Dockerfile, compose, nginx |
| `docs/` | документация, схемы, снимки экрана, сборка Word |
| `tests/` | 342 теста |

## Разработка

```bash
python3 -m pytest tests/          # тесты
ruff check .                      # линтер
bash docs/build.sh                # пересобрать документацию и Word
```

Разделы документации о моделях, параметрах и пресетах генерируются из каталога, поэтому не расходятся с кодом.

## Лицензии

Код — MIT. Веса моделей загружаются с сайтов правообладателей и остаются под своими лицензиями; в каталог включены только свободные. Разбор — [docs/15-licenses.md](docs/15-licenses.md).
