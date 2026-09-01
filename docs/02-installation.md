# Установка

![Варианты развёртывания](images/diag-04-deployment.png)

## Выбор способа

| | Нативная установка | Docker |
|---|---|---|
| Скорость на GPU | максимальная | та же (`--gpus all`) |
| Обновление | `update.sh` со снимком и откатом | пересборка образа |
| Изоляция | виртуальное окружение Python | полная |
| Ставится за | 10–40 минут | 5 минут + сборка образа |
| Apple Silicon (MPS, Core ML) | да | **нет** — Docker на macOS не даёт доступа к Metal |
| Подходит для | выделенного сервера, рабочей станции, MacBook | кластера, CI, нескольких экземпляров |

> **Рекомендация.** На отдельной машине с видеокартой и на macOS ставьте нативно: разница в производительности на Apple Silicon разительная, а откат обновления встроен в скрипт. Docker берите там, где важна воспроизводимость или экземпляров больше одного.

## Профили установки

Профиль определяет, какие движки ставятся и какие веса загружаются сразу. Если профиль не указан, он подбирается по обнаруженному железу.

| Профиль | Что ставится | Диск | Кому |
|---|---|---|---|
| `light` | сервер + faster-whisper small | ~1 ГБ | проверка, слабая машина, контейнер в CI |
| `cpu` | faster-whisper (int8) + GigaAM ONNX | ~4 ГБ | сервер без видеокарты |
| `standard` | GigaAM v3 + faster-whisper | ~8 ГБ | **умолчание для машины с GPU** |
| `full` | все движки и основные модели | 60+ ГБ | исследование, сравнение моделей |
| `apple` | whisper.cpp с Metal и Core ML | ~3 ГБ | MacBook на Apple Silicon |
| `russian` | GigaAM v3, T-one, Vosk | ~3 ГБ | только русский язык |

Профиль не запирает вас: любую модель можно доустановить позже командой `scripts/models.sh download <модель>`.

## Linux

### Установка

```bash
git clone <репозиторий> asr-hub && cd asr-hub

# посмотреть план, ничего не меняя
bash scripts/install.sh --profile standard --dry-run

# установка
sudo bash scripts/install.sh --profile standard --port 8080
```

Без `sudo` установка пройдёт в домашний каталог пользователя — это рабочий вариант, если сервер нужен только вам.

### Что делает скрипт

Девять шагов, каждый с проверкой и понятным сообщением об ошибке:

1. **Проверка окружения** — версия Python (нужна 3.9+), свободное место, свободный порт, доступность репозиториев, права на каталоги. Определяются дистрибутив, пакетный менеджер, архитектура и ускоритель.
2. **Системные зависимости** — ffmpeg, git, python3-venv, при необходимости cmake и libsndfile. Скрипт знает имена пакетов для apt, dnf, yum, pacman, zypper, apk, brew, choco и winget и предлагает поставить недостающие.
3. **Каталоги** — `PREFIX` для кода, `DATA_DIR` с подкаталогами `uploads`, `results`, `models`, `logs`, `tmp` и правами 0750.
4. **Копирование файлов** приложения в `PREFIX`.
5. **Виртуальное окружение и пакеты** — PyTorch ставится с индекса, соответствующего найденному ускорителю (CUDA 12.4/12.1, ROCm, CPU), CTranslate2 закрепляется на версии, совместимой с найденной cuDNN.
6. **Конфигурация** — генерируется `config.yaml` с комментарием к каждому параметру (около 1700 строк) и значениями, подобранными под ваше железо.
7. **Загрузка моделей** профиля и всего, что перечислено в `--models`.
8. **Служба автозапуска** — systemd (системная или пользовательская).
9. **Проверка** — сервер поднимается, опрашивается `/api/health`, выводится итог.

⚠️ Каждый шаг регистрирует действие отмены. Если что-то падает на шаге 7, шаги 1–6 откатываются: созданные каталоги удаляются, старая конфигурация возвращается. Обработчик `ERR` печатает команду, код возврата, номер строки и расшифровку кода (127 — команда не найдена, 137 — процесс убит по нехватке памяти, и так далее).

### Каталоги по умолчанию

| | От root | От пользователя |
|---|---|---|
| Код | `/opt/asrhub` | `~/.local/share/asrhub-app` |
| Данные | `/var/lib/asrhub` | `~/.local/share/asrhub` |
| Конфигурация | `/etc/asrhub/config.yaml` | `~/.config/asrhub/config.yaml` |
| Журналы | `<данные>/logs/` | то же |

### Видеокарта NVIDIA

Скрипт определяет CUDA сам, но проверить стоит заранее:

```bash
nvidia-smi                       # драйвер и версия CUDA
bash scripts/doctor.sh --hardware
```

Для CUDA 12.x подходящие версии подбираются автоматически. Самая частая проблема — несовместимость cuDNN с CTranslate2; таблица соответствий встроена в подсказку к ошибке и в раздел [11. Устранение неполадок](11-troubleshooting.md).

### Видеокарта AMD (ROCm)

```bash
rocm-smi
bash scripts/install.sh --profile standard
```

PyTorch ставится с индекса ROCm. GigaAM и faster-whisper работают; NeMo на ROCm официально не поддерживается.

## macOS

```bash
# Homebrew, если его ещё нет
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install ffmpeg python@3.12

cd asr-hub
bash scripts/install.sh --profile apple
```

На Apple Silicon доступны два ускорителя:

- **MPS** — Metal Performance Shaders для PyTorch. Работает с GigaAM и faster-whisper. Включается автоматически при `device: auto`.
- **Core ML** — через whisper.cpp, задействует Neural Engine. Даёт лучшее соотношение скорости и энергопотребления, особенно на ноутбуке от батареи.

Служба регистрируется в launchd как пользовательский агент:

```bash
bash scripts/service.sh install
bash scripts/service.sh status
launchctl list | grep asrhub
```

⚠️ На macOS файлы, загруженные из интернета, помечаются карантином Gatekeeper. Если бинарник whisper.cpp не запускается — снимите метку: `xattr -d com.apple.quarantine <путь>`.

## Windows

PowerShell **от имени администратора**:

```powershell
cd asr-hub
powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile standard -Port 8080
```

Скрипты рассчитаны на Windows PowerShell 5.1 — тот, что идёт в комплекте с Windows 10 и 11. Ставить PowerShell 7 не нужно.

Служба регистрируется через NSSM, если он найден, иначе создаётся задача планировщика с запуском при входе в систему:

```powershell
.\scripts\service.ps1 install
.\scripts\service.ps1 status
.\scripts\service.ps1 logs -Follow
```

Что стоит знать:

- **ffmpeg** ставится через `winget install Gyan.FFmpeg` или `choco install ffmpeg`. Без него принимаются только файлы WAV.
- **Длинные пути.** Кеш моделей Hugging Face легко переваливает за 260 символов. Включите поддержку длинных путей: `New-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -Value 1 -PropertyType DWORD -Force`.
- **Антивирус.** Проверка каждого файла в каталоге моделей заметно замедляет загрузку — добавьте каталог данных в исключения.
- **WSL2** — рабочая альтернатива: внутри работает установка для Linux, видеокарта NVIDIA пробрасывается.

## Docker

### Быстрый запуск

```bash
cd asr-hub/docker
docker compose --profile gpu up -d      # NVIDIA
docker compose --profile cpu up -d      # без видеокарты
docker compose --profile proxy up -d    # плюс nginx перед сервером
```

### Сборка образа

```bash
docker build -t asrhub:cuda \
  --build-arg PROFILE=standard \
  --build-arg ACCEL=cuda \
  -f docker/Dockerfile .
```

Аргументы сборки: `PROFILE` (тот же набор, что у нативной установки), `ACCEL` (`cuda`, `rocm`, `cpu`). Образ многослойный: тяжёлые зависимости ставятся в отдельном слое, поэтому пересборка после правки кода занимает секунды.

### Тома и переменные

```yaml
volumes:
  - ./data/models:/data/models     # веса — самый ценный том, переживает пересборку
  - ./data/uploads:/data/uploads
  - ./data/results:/data/results
  - ./data/db:/data/db
environment:
  ASRHUB_PORT: 8080
  ASRHUB_MAX_CONCURRENT_JOBS: 2
  ASRHUB_DEVICE: cuda
  ASRHUB_AUTH_ENABLED: "true"
```

Точка входа проверяет перед стартом: смонтированы ли тома и доступны ли они на запись, виден ли заявленный ускоритель, свободен ли порт. Если видеокарта заявлена, но не видна, контейнер сообщает об этом внятно, а не падает через десять минут внутри PyTorch.

⚠️ Загрузка образа **не проверена сборкой** в среде, где готовилась эта документация: демон Docker там недоступен. Конфигурация написана и вычитана, но первый запуск у вас может потребовать правок — начните с `docker compose --profile cpu up` без `-d`, чтобы видеть вывод.

## Управление службой

```bash
bash scripts/service.sh install     # зарегистрировать
bash scripts/service.sh start
bash scripts/service.sh stop
bash scripts/service.sh restart
bash scripts/service.sh status
bash scripts/service.sh logs -n 200 -f
bash scripts/service.sh uninstall
```

Одинаковые команды на всех системах: под ними systemd на Linux, launchd на macOS, NSSM или планировщик задач на Windows.

## Обновление

```bash
bash scripts/update.sh --check          # что изменится
bash scripts/update.sh                  # обновить
bash scripts/update.sh --engines-only   # только пакеты движков
bash scripts/update.sh --rollback       # вернуть предыдущую версию
```

Перед обновлением делается снимок: код, конфигурация и база заданий. Схема базы мигрирует по версиям — переход с версии 1 на 3 добавит недостающие таблицы и колонки, не потеряв заданий. Если после обновления сервер не отвечает, `--rollback` возвращает предыдущее состояние целиком.

Веса моделей при обновлении не трогаются.

## Удаление

```bash
bash scripts/uninstall.sh                      # код и служба, данные остаются
bash scripts/uninstall.sh --purge              # всё, включая модели и базу
bash scripts/uninstall.sh --purge --keep-models  # всё, кроме весов
bash scripts/uninstall.sh --dry-run            # что именно будет удалено
```

Перед удалением конфигурация и база заданий копируются в резервную копию, путь к которой печатается в конце. Каталог моделей по умолчанию сохраняется: скачивать десятки гигабайт заново из-за случайного `--purge` не придётся.

## Проверка после установки

```bash
bash scripts/doctor.sh
curl http://127.0.0.1:8080/api/health
bash scripts/models.sh engines     # какие движки доступны и почему нет остальных
bash scripts/models.sh disk        # сколько занято весами
```

Дальше — [05. Веб-интерфейс](05-web-interface.md) или сразу [12. Сценарии настройки](12-tuning.md).
