#!/usr/bin/env bash
#
# Установка ASR Hub на Linux и macOS.
#
#   bash scripts/install.sh                       установка с автонастройкой
#   bash scripts/install.sh --mode docker         развёртывание в контейнере
#   bash scripts/install.sh --profile full        все движки и модели
#   bash scripts/install.sh --dry-run             показать план без изменений
#
# Скрипт идемпотентен: повторный запуск обновляет установку, не ломая её.
# При любой ошибке выполняется откат уже сделанных изменений.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=lib/common.sh
source "${SCRIPT_DIR}/lib/common.sh"
# shellcheck source=lib/detect.sh
source "${SCRIPT_DIR}/lib/detect.sh"
# shellcheck source=lib/gpu.sh
source "${SCRIPT_DIR}/lib/gpu.sh"
# shellcheck source=lib/whispercpp.sh
source "${SCRIPT_DIR}/lib/whispercpp.sh"
# shellcheck source=lib/wizard.sh
source "${SCRIPT_DIR}/lib/wizard.sh"

# ---------------------------------------------------------------------------
# Значения по умолчанию
# ---------------------------------------------------------------------------

PREFIX=""
DATA_DIR=""
PORT="${ASRHUB_DEFAULT_PORT}"
HOST="0.0.0.0"
MODE="native"
PROFILE=""
ENGINES=""
MODELS=""
CREATE_SERVICE=1
SERVICE_USER=""
OFFLINE=0
FORCE=0
SKIP_MODELS=0
# Заполняется только в нативном режиме, а читается в самом конце для обоих.
# Без этой строки docker-режим падал под `set -o nounset` уже после слов
# «Установка завершена», и любой вызывающий видел код возврата 1.
FAILED_ENGINES=()
INTERACTIVE=auto
ENGINES_EXPLICIT=""
MODELS_EXPLICIT=""
ALIGNMENT=""
MONITORING=""
# Распознавание на лету. Включено по умолчанию — как и на сервере; ключ нужен,
# чтобы выключить его на машине, где ffmpeg ставить не будут.
STREAM_ENABLED="true"
# Ставить ли проверенную версию Python, когда найденная слишком новая.
PYTHON_INSTALL=1
# Токен Hugging Face: задаётся ключом или берётся из окружения.
HF_TOKEN_VALUE="${HF_TOKEN:-${HUGGING_FACE_HUB_TOKEN:-}}"
# auto — поставить драйвер под найденную карту; none — не трогать;
# nvidia/amd/intel — ставить только если найдена карта этого производителя.
GPU_DRIVER="auto"
# Ускоритель, под который собираем окружение. Отличается от ACCEL, когда
# драйвер поставлен, но ещё не работает: до перезагрузки nvidia-smi молчит,
# а колёса PyTorch нужны уже под видеокарту, иначе после перезагрузки
# окажется, что установлен процессорный torch.
GPU_TARGET_ACCEL=""

usage() {
  cat <<'USAGE'
Установка ASR Hub

Использование: bash scripts/install.sh [параметры]

Основные параметры
  --prefix ПУТЬ         Каталог установки (по умолчанию /opt/asrhub,
                        на macOS ~/Library/Application Support/ASRHub)
  --data ПУТЬ           Каталог данных: загрузки, модели, база, журналы
  --port ЧИСЛО          Порт сервера (по умолчанию 8080)
  --host АДРЕС          Адрес прослушивания (по умолчанию 0.0.0.0)
  --mode native|docker  Способ развёртывания (по умолчанию native)
  --python ПУТЬ         Использовать конкретный интерпретатор Python
                        (по умолчанию выбирается новейший подходящий)

Состав установки
  --profile ИМЯ         light | cpu | standard | full | apple | russian
                        По умолчанию подбирается по обнаруженному железу
  --engines СПИСОК      Движки через запятую: gigaam,faster_whisper,nemo,vosk,…
  --models СПИСОК       Модели через запятую, загрузить сразу после установки
  --skip-models         Не загружать веса моделей
  --alignment ИМЯ       Принудительное выравнивание: none | mfa | whisperx

Режим работы
  --interactive         Мастер с вопросами (по умолчанию, если есть терминал)
  --no-interactive      Без вопросов, все значения из ключей и автоопределения

Видеокарта
  --gpu-driver РЕЖИМ    auto (по умолчанию) | none | nvidia | amd | intel
                        Карта ищется на шине PCI, поэтому находится и там,
                        где драйвера ещё нет. auto — поставить драйвер и
                        настроить, none — оставить как есть
  --force-gpu-driver    Ставить драйвер даже при включённом Secure Boot
                        (собранный модуль будет без подписи и не загрузится,
                        пока вы не зарегистрируете ключ MOK)

Служба и права
  --no-service          Не создавать службу автозапуска
  --user ИМЯ            Пользователь, от которого работает служба

Прочее
  --offline             Не обращаться в интернет (пакеты из локального кеша)
  --force               Переустановить поверх существующей установки
  --dry-run             Показать план действий, ничего не меняя
  --yes                 Не задавать вопросов
  --quiet               Минимум вывода
  --debug               Подробный вывод и трассировка
  -h, --help            Эта справка

Профили
  light      Минимум: сервер + faster-whisper small (около 1 ГБ)
  cpu        Для сервера без видеокарты: faster-whisper + int8 + GigaAM ONNX
  standard   GigaAM v3 + faster-whisper (рекомендуется, около 8 ГБ)
  full       Все движки и основные модели (свыше 60 ГБ)
  apple      whisper.cpp с Metal и Core ML для Apple Silicon
  russian    Только русские модели: GigaAM v3, T-one, Vosk

Примеры
  bash scripts/install.sh --profile standard --port 9000
  bash scripts/install.sh --mode docker --profile full
  bash scripts/install.sh --profile russian --models gigaam-v3-e2e-rnnt,tone-ru
  bash scripts/install.sh --dry-run --profile full
USAGE
}

# ---------------------------------------------------------------------------
# Разбор аргументов
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)      ASRHUB_PYTHON="${2:?нужен путь к интерпретатору}"; export ASRHUB_PYTHON; shift 2 ;;
    --prefix)      PREFIX="${2:?нужен путь}"; shift 2 ;;
    --data)        DATA_DIR="${2:?нужен путь}"; shift 2 ;;
    --port)        PORT="${2:?нужен номер порта}"; shift 2 ;;
    --host)        HOST="${2:?нужен адрес}"; shift 2 ;;
    --mode)        MODE="${2:?native или docker}"; shift 2 ;;
    --profile)     PROFILE="${2:?имя профиля}"; shift 2 ;;
    --engines)     ENGINES="${2:?список движков}"; ENGINES_EXPLICIT=1; shift 2 ;;
    --models)      MODELS="${2:?список моделей}"; MODELS_EXPLICIT=1; shift 2 ;;
    --user)        SERVICE_USER="${2:?имя пользователя}"; shift 2 ;;
    --skip-models) SKIP_MODELS=1; shift ;;
    --interactive) INTERACTIVE=1; shift ;;
    --no-interactive) INTERACTIVE=0; shift ;;
    --alignment)   ALIGNMENT="${2:?none, mfa или whisperx}"; shift 2 ;;
    --gpu-driver)  GPU_DRIVER="${2:?auto, none, nvidia, amd или intel}"; shift 2 ;;
    --no-gpu-driver) GPU_DRIVER="none"; shift ;;
    --force-gpu-driver) ASRHUB_FORCE_GPU_DRIVER=1; export ASRHUB_FORCE_GPU_DRIVER; shift ;;
    --no-service)  CREATE_SERVICE=0; shift ;;
    --offline)     OFFLINE=1; shift ;;
    --force)       FORCE=1; shift ;;
    --dry-run)     ASRHUB_DRY_RUN=1; shift ;;
    # Имя службы. Нужно, когда на машине больше одной установки: без него
    # вторая переписывала юнит первой (/etc/systemd/system/asrhub.service).
    --name)        SERVICE_NAME="$2"; shift 2 ;;
    --no-stream)   STREAM_ENABLED="false"; shift ;;
    # Не ставить Python самим, даже если найденный слишком новый.
    --no-python-install) PYTHON_INSTALL=0; shift ;;
    # Токен Hugging Face. Без него не скачиваются модели с ограниченным
    # доступом — pyannote для диаризации требует его всегда, часть весов
    # GigaAM и Whisper тоже. Раньше его нельзя было задать при установке
    # вовсе: приходилось дописывать env.sh руками уже после.
    --hf-token)    HF_TOKEN_VALUE="$2"; shift 2 ;;
    --hf-token-file) HF_TOKEN_VALUE="$(cat "${2:?}")"; shift 2 ;;
    --yes|-y)      ASRHUB_ASSUME_YES=1; shift ;;
    --quiet|-q)    ASRHUB_QUIET=1; shift ;;
    --debug)       ASRHUB_DEBUG=1; shift ;;
    -h|--help)     usage; exit 0 ;;
    *) error "Неизвестный параметр: $1"; hint "Справка: bash scripts/install.sh --help"; exit 2 ;;
  esac
done

enable_error_handling
setup_logging "${TMPDIR:-/tmp}"
print_banner

OS="$(detect_os)"
ARCH="$(detect_arch)"
ACCEL="$(detect_gpu)"
# Профиль по умолчанию — с учётом карты, драйвер для которой поставим сами.
# Без этого установка без вопросов на машине с RTX 4090 выбирала «light»:
# faster-whisper small на видеокарте за две тысячи долларов.
GPU_PENDING_DRIVER=0
[[ "${GPU_DRIVER}" != "none" && "${OS}" == "linux" ]] && GPU_PENDING_DRIVER=1
[[ -z "${PROFILE}" ]] && PROFILE="$(recommend_profile "${GPU_PENDING_DRIVER}")"

if [[ -z "${PREFIX}" ]]; then
  case "${OS}" in
    macos) PREFIX="${HOME}/Library/Application Support/ASRHub" ;;
    *)     if is_root; then PREFIX="/opt/asrhub"; else PREFIX="${HOME}/.local/share/asrhub-app"; fi ;;
  esac
fi
if [[ -z "${DATA_DIR}" ]]; then
  case "${OS}" in
    macos) DATA_DIR="${HOME}/Library/Application Support/ASRHub/data" ;;
    *)     if is_root; then DATA_DIR="/var/lib/asrhub"; else DATA_DIR="${HOME}/.local/share/asrhub"; fi ;;
  esac
fi

VENV="${PREFIX}/venv"
PY=""

# ---------------------------------------------------------------------------
# Профили
# ---------------------------------------------------------------------------

profile_engines() {
  case "$1" in
    light)    printf 'faster_whisper' ;;
    cpu)      printf 'faster_whisper,gigaam,vosk' ;;
    standard) printf 'gigaam,faster_whisper,whisper' ;;
    russian)  printf 'gigaam,tone,vosk' ;;
    apple)    printf 'whisper_cpp,faster_whisper,gigaam' ;;
    full)     printf 'gigaam,faster_whisper,whisper,whisper_cpp,nemo,transformers,vosk,tone,whisperx,qwen3_asr' ;;
    *)        printf 'faster_whisper' ;;
  esac
}

profile_models() {
  case "$1" in
    light)    printf 'faster-whisper-small' ;;
    cpu)      printf 'gigaam-v3-ctc,faster-whisper-small' ;;
    standard) printf 'gigaam-v3-e2e-rnnt,faster-whisper-large-v3' ;;
    russian)  printf 'gigaam-v3-e2e-rnnt,gigaam-v3-ctc,tone-ru,vosk-small-ru-0.22' ;;
    apple)    printf 'whispercpp-large-v3-turbo-q5_0,gigaam-v3-ctc' ;;
    full)     printf 'gigaam-v3-e2e-rnnt,gigaam-v3-rnnt,gigaam-v3-ctc,faster-whisper-large-v3,parakeet-tdt-0.6b-v3,tone-ru' ;;
    *)        printf '' ;;
  esac
}

profile_disk_gb() {
  case "$1" in
    light) printf '3' ;; cpu) printf '6' ;; standard) printf '12' ;;
    russian) printf '10' ;; apple) printf '8' ;; full) printf '70' ;; *) printf '5' ;;
  esac
}

[[ -z "${ENGINES}" ]] && ENGINES="$(profile_engines "${PROFILE}")"
if [[ -z "${MODELS}" && "${SKIP_MODELS}" -eq 0 ]]; then
  MODELS="$(profile_models "${PROFILE}")"
fi


# ---------------------------------------------------------------------------
# Мастер установки
# ---------------------------------------------------------------------------
#
# Запускается, когда есть терминал и не задан --no-interactive или --yes.
# Каждый вопрос имеет ответ по умолчанию, подобранный по обнаруженному железу,
# поэтому «Enter пять раз» даёт разумную установку.

# Имя службы. По умолчанию одно на машину; для второй установки задаётся
# ключом --name, иначе она перезаписала бы юнит первой.
SERVICE_NAME="${SERVICE_NAME:-asrhub}"

run_wizard() {
  local ram_gb disk_gb accel_label

  ram_gb="$(detect_ram_gb 2>/dev/null || echo 0)"
  # `|| true`: каталога данных на новой машине ещё нет, df на таком пути
  # возвращает 1, и мастер молча выходил, не задав ни одного вопроса.
  disk_gb="$(df -Pk "$(dirname "${DATA_DIR}")" 2>/dev/null \
             | awk 'NR==2{printf "%d", $4/1048576}' || true)"
  disk_gb="${disk_gb:-0}"
  case "${ACCEL}" in
    cuda) accel_label="видеокарта NVIDIA" ;;
    rocm) accel_label="видеокарта AMD (ROCm)" ;;
    mps)  accel_label="Apple Silicon (Metal)" ;;
    *)    accel_label="только процессор" ;;
  esac
  # «Только процессор» при видеокарте на шине — правда лишь до установки
  # драйвера, и как итог обследования читается неверно.
  if [[ "${ACCEL}" == "cpu" ]]; then
    local pending_label; pending_label="$(gpu_pending || true)"
    [[ -n "${pending_label}" ]] && \
      accel_label="процессор (видеокарта найдена, драйвера ещё нет)"
  fi

  wizard_step "Установка ASR Hub" \
    "Enter принимает предложенное значение — оно подобрано по вашему железу"

  printf '  %sОбнаружено:%s %s, %s, %s ГБ памяти, %s ГБ свободно\n' \
    "${C_DIM}" "${C_RESET}" "${OS}/${ARCH}" "${accel_label}" "${ram_gb}" "${disk_gb:-?}"

  # Карта на шине есть, а драйвера нет — самый обидный случай: без этой
  # строки человек выбирал бы профиль для машины без видеокарты, имея её.
  local gpu_line gpu_vendor gpu_discrete
  gpu_line="$(gpu_primary)"
  if [[ -n "${gpu_line}" && "${ACCEL}" == "cpu" ]]; then
    gpu_vendor="$(printf '%s' "${gpu_line}" | cut -d'|' -f2)"
    gpu_discrete="$(printf '%s' "${gpu_line}" | cut -d'|' -f4)"
    if [[ "${gpu_discrete}" == "1" ]]; then
      printf '  %sНа шине найдена видеокарта %s, драйвер не установлен.%s\n' \
        "${C_YELLOW}" "$(gpu_vendor_label "${gpu_vendor}")" "${C_RESET}"
      if [[ "${GPU_DRIVER}" == "none" ]]; then
        printf '  %sЗадан --no-gpu-driver: ставить не будем.%s\n' "${C_DIM}" "${C_RESET}"
      else
        printf '  %sУстановщик поставит драйвер — обычно нужна перезагрузка.%s\n' \
          "${C_DIM}" "${C_RESET}"
      fi
    fi
  fi

  # --- 1. Что ставим ------------------------------------------------------
  # Единица означает «драйвер поставим прямо сейчас»: карта на шине считается
  # рабочей. Иначе профиль подбирался для машины без видеокарты, а через
  # минуту установщик сам же ставил драйвер — человек с RTX 4090 получал
  # предложение «light».
  local default_profile=1
  case "$(recommend_profile "${GPU_PENDING_DRIVER}")" in
    light) default_profile=1 ;; cpu) default_profile=2 ;;
    standard) default_profile=3 ;; russian) default_profile=4 ;;
    apple) default_profile=5 ;; full) default_profile=6 ;;
  esac

  # Если профиль задан ключом, мастер предлагает именно его, а не своё умолчание.
  case "${PROFILE}" in
    light) default_profile=1 ;; cpu) default_profile=2 ;;
    standard) default_profile=3 ;; russian) default_profile=4 ;;
    apple) default_profile=5 ;; full) default_profile=6 ;;
  esac

  wizard_choose PROFILE "Что установить?" "${default_profile}" \
    "light|Минимум — проверить, что всё работает|~1 ГБ. faster-whisper small. Годится, чтобы посмотреть интерфейс" \
    "cpu|Сервер без видеокарты|~4 ГБ. int8-квантизация. Час записи обрабатывается за час-полтора" \
    "standard|Стандартный набор|~8 ГБ. GigaAM v3 + faster-whisper. Лучший выбор для машины с GPU" \
    "russian|Только русский язык|~3 ГБ. GigaAM v3, T-one, Vosk. Ничего лишнего" \
    "apple|MacBook на Apple Silicon|~3 ГБ. whisper.cpp с Metal и Core ML, работает от батареи" \
    "full|Всё сразу|60+ ГБ и час установки. Для сравнения моделей между собой"

  # Явно заданные --engines и --models мастер не перетирает.
  [[ -z "${ENGINES_EXPLICIT}" ]] && ENGINES="$(profile_engines "${PROFILE}")"
  [[ -z "${MODELS_EXPLICIT}" ]] && MODELS="$(profile_models "${PROFILE}")"

  # Предупреждаем до начала работы, а не на седьмом шаге.
  local need_gb; need_gb="$(profile_disk_gb "${PROFILE}")"
  if [[ -n "${disk_gb}" && "${disk_gb}" -lt "${need_gb}" ]]; then
    warn "Профилю нужно около ${need_gb} ГБ, свободно ${disk_gb} ГБ."
    confirm "Продолжить всё равно?" "n" || exit 0
  fi
  if [[ "${ACCEL}" == "cpu" && "${PROFILE}" =~ ^(standard|full)$ ]]; then
    warn "Видеокарта не обнаружена: выбранные модели будут работать в 10–30 раз медленнее."
    hint "Профиль «cpu» подобран как раз для такой машины."
    confirm "Оставить выбранный профиль?" "n" || { PROFILE="cpu";
      ENGINES="$(profile_engines cpu)"; MODELS="$(profile_models cpu)"; }
  fi

  # --- 2. Куда ставим -----------------------------------------------------
  wizard_ask PREFIX "Каталог программы" "${PREFIX}" wizard_valid_path
  wizard_ask DATA_DIR "Каталог данных" "${DATA_DIR}" wizard_valid_path \
    "Здесь будут веса моделей, загруженные файлы, результаты и база заданий."
  VENV="${PREFIX}/venv"

  # --- 3. Сеть ------------------------------------------------------------
  local suggested_port="${PORT}"
  check_port_free "${PORT}" || suggested_port="$(find_free_port "${PORT}")"
  wizard_ask PORT "Порт сервера" "${suggested_port}" wizard_valid_port

  # Умолчание подстраивается под то, что задано ключом: --host 127.0.0.1
  # означает «не выставлять наружу», и мастер не должен молча это отменять.
  local host_default=2
  [[ "${HOST}" == "127.0.0.1" || "${HOST}" == "localhost" ]] && host_default=1
  wizard_choose HOST "Кто сможет подключаться?" "${host_default}" \
    "127.0.0.1|Только эта машина|Снаружи сервер не виден. Доступ — через SSH-туннель или прокси" \
    "0.0.0.0|Любой, кто дотянется по сети|Обычный выбор для сервера. Оставьте включённой проверку ключей"

  # --- 4. Дополнительные возможности --------------------------------------
  # То же для автозапуска: --no-service снимает пункт из умолчания.
  local extras=""
  local extras_default="1"
  [[ "${CREATE_SERVICE}" == "0" ]] && extras_default=""
  # Поток включён по умолчанию — пункт отмечен, чтобы его можно было снять
  # осознанно, а не обнаружить включённым постфактум.
  extras_default="${extras_default:+${extras_default},}4"
  wizard_multi extras "Что ещё включить?" "${extras_default}" \
    "service|Автозапуск при загрузке машины|systemd, launchd или планировщик Windows" \
    "alignment|Точные границы слов (MFA)|+2–3 ГБ и conda. Нужно для субтитров и дубляжа" \
    "monitoring|Отправку метрик в систему мониторинга|Настроим адрес приёмника на следующем шаге" \
    "stream|Распознавание на лету (диктовка с микрофона)|Раздел «Диктовка» и WebSocket /api/stream. Нужен ffmpeg" \
    "diarization|Разделение по говорящим (pyannote)|+1 ГБ. Нужен токен Hugging Face и согласие с лицензией модели"

  [[ ",${extras}," == *",service,"* ]] && CREATE_SERVICE=1 || CREATE_SERVICE=0
  [[ ",${extras}," == *",alignment,"* ]] && ALIGNMENT="mfa"
  [[ ",${extras}," == *",stream,"* ]] && STREAM_ENABLED="true" || STREAM_ENABLED="false"
  # Диаризация ставится как обычный движок: файл требований у неё свой.
  if [[ ",${extras}," == *",diarization,"* && "${ENGINES}" != *diarization* ]]; then
    ENGINES="${ENGINES},diarization"
  fi
  # Микрофон браузер отдаёт только по https или на localhost. Сказать об этом
  # надо до установки, а не после первого недоумения пользователя.
  if [[ "${STREAM_ENABLED}" == "true" && "${HOST}" == "0.0.0.0" ]]; then
    warn "Диктовка с микрофона по http работать не будет."
    hint "Браузер отдаёт микрофон только по https или на localhost."
    hint "Поставьте перед сервером обратный прокси с сертификатом —"
    hint "готовый пример: ${REPO_DIR}/docker/nginx.conf. Всё остальное, включая"
    hint "поток из своей программы (examples/stream_microphone.py), работает и так."
  fi
  if [[ ",${extras}," == *",monitoring,"* ]]; then
    wizard_choose MONITORING "Куда отправлять метрики?" 1 \
      "prometheus_pushgateway|Prometheus Pushgateway|Для сервера за NAT, до которого не достучаться" \
      "influxdb|InfluxDB|Метрики пишутся в базу временных рядов" \
      "otlp|OpenTelemetry Collector|Общий сборщик телеметрии"
    wizard_ask MONITORING_URL "Адрес приёмника" "http://localhost:9091" wizard_valid_host
  fi

  # --- 4а. Токен Hugging Face ---------------------------------------------
  # Не спрашиваем, если значение уже задано ключом --hf-token или пришло из
  # окружения: мастер не переспрашивает то, что ему сказали.
  #
  # Текст пояснения зависит от выбора выше. Для диаризации токен не «желателен»,
  # а обязателен — pyannote без него не скачается, и узнать об этом на седьмом
  # шаге хуже, чем ответить на вопрос сейчас.
  if [[ -z "${HF_TOKEN_VALUE}" ]]; then
    local token_note
    if [[ ",${extras}," == *",diarization,"* ]]; then
      token_note="Для разделения по говорящим он обязателен: pyannote без токена не
скачается. Возьмите на huggingface.co/settings/tokens (права «read») и примите
лицензию модели pyannote/speaker-diarization-community-1."
    else
      token_note="Нужен для моделей с ограниченным доступом. Сейчас вы такие не
выбрали, так что можно пропустить — Enter. Дописать потом:
scripts/install.sh --hf-token hf_… или строкой hf_token: в config.yaml."
    fi
    wizard_ask HF_TOKEN_VALUE "Токен Hugging Face" "" wizard_valid_hf_token \
      "${token_note}"
  fi

  # --- 5. Подтверждение ---------------------------------------------------
  # Профиль мог смениться после предупреждения о видеокарте — пересчитываем.
  need_gb="$(profile_disk_gb "${PROFILE}")"
  [[ "${ALIGNMENT}" == "mfa" ]] && need_gb=$((need_gb + 3))

  local models_line="${MODELS:-не загружать}"
  [[ "${SKIP_MODELS}" -eq 1 ]] && models_line="пропустить (--skip-models)"

  wizard_summary \
    "Профиль|${PROFILE}" \
    "Движки|${ENGINES}" \
    "Модели|${models_line}" \
    "Каталог программы|${PREFIX}" \
    "Каталог данных|${DATA_DIR}" \
    "Адрес|http://${HOST}:${PORT}" \
    "Автозапуск|$([[ ${CREATE_SERVICE} -eq 1 ]] && echo "да" || echo "нет")" \
    "Выравнивание|${ALIGNMENT:-нет}" \
    "Мониторинг|${MONITORING:-только по запросу}" \
    "Распознавание на лету|$([[ "${STREAM_ENABLED}" == "true" ]] && echo "включено" || echo "выключено")" \
    "Токен Hugging Face|$([[ -n "${HF_TOKEN_VALUE}" ]] \
        && echo "${HF_TOKEN_VALUE:0:6}… (${#HF_TOKEN_VALUE} знаков)" || echo "не задан")" \
    "Займёт на диске|около ${need_gb} ГБ"

  confirm "Начинать установку?" "y" || { info "Отменено."; exit 0; }
}

if [[ "${INTERACTIVE}" == "1" ]] || { [[ "${INTERACTIVE}" == "auto" ]] && wizard_interactive; }; then
  run_wizard
fi

# ---------------------------------------------------------------------------
# Шаг 1. Предварительные проверки
# ---------------------------------------------------------------------------

# Число шагов считаем по фактическому составу установки, а не берём
# постоянным: раньше счётчик показывал «11 из 10», потому что часть шагов
# условная — PyTorch, whisper.cpp, модели, служба.
_steps=10                                   # нативная установка без дополнений
if [[ "${MODE}" == "docker" ]]; then
  _steps=8                                  # вместо venv и движков — сборка образа
else
  for _engine in gigaam faster_whisper whisper nemo transformers whisperx qwen3_asr voxtral; do
    [[ "${ENGINES}" == *"${_engine}"* ]] && { _steps=$((_steps + 1)); break; }
  done
  [[ "${ENGINES}" == *whisper_cpp* ]] && _steps=$((_steps + 1))
fi
[[ -n "${MODELS}" && "${SKIP_MODELS}" -eq 0 ]] && _steps=$((_steps + 1))
[[ "${CREATE_SERVICE}" -eq 1 ]] && _steps=$((_steps + 1))
[[ -n "${ALIGNMENT}" && "${ALIGNMENT}" != "none" ]] && _steps=$((_steps + 1))
set_step_total "${_steps}"
unset _engine

step "Проверка окружения"

print_environment

info "Профиль установки: ${C_BOLD}${PROFILE}${C_RESET}"
info "Движки: ${ENGINES}"
info "Каталог установки: ${PREFIX}"
info "Каталог данных: ${DATA_DIR}"
info "Адрес сервера: http://${HOST}:${PORT}"
[[ -n "${MODELS}" ]] && info "Модели к загрузке: ${MODELS}"

if [[ "${OS}" == "unknown" || "${OS}" == "windows" ]]; then
  error "Эта система не поддерживается этим скриптом."
  hint "Для Windows используйте: powershell -ExecutionPolicy Bypass -File scripts\\install.ps1"
  exit 2
fi

if [[ "${MODE}" == "native" ]]; then
  PY="$(check_python)" || exit 1

  # Слишком новый интерпретатор чиним сами, а не советуем починить.
  # check_python в этом случае уже объяснил, чем грозит, и назвал команду —
  # но человеку остаётся выйти, поставить пакет и запустить заново. Три
  # действия там, где скрипт умеет все три: у него есть и менеджер пакетов,
  # и права, ради которых его и запускают под sudo.
  #
  # Условие «явно задан --python» важно: указанный вручную интерпретатор —
  # это решение пользователя, и подменять его нельзя даже к лучшему.
  PY_VERSION="$("${PY}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  if [[ -z "${ASRHUB_PYTHON:-}" && -n "${PY_VERSION}" ]] \
     && version_gt "${PY_VERSION}" "${ASRHUB_MAX_PYTHON}" \
     && [[ "${PYTHON_INSTALL}" != "0" ]]; then
    if confirm "Поставить Python ${ASRHUB_MAX_PYTHON} и собрать окружение на нём?" "y"; then
      NEW_PY="$(install_supported_python "${ASRHUB_MAX_PYTHON}" || true)"
      if [[ -n "${NEW_PY}" ]]; then
        PY="${NEW_PY}"
        ok "Дальше работаем на ${PY} ($("${PY}" --version 2>&1))"
      elif [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
        warn "Проверенную версию поставить не удалось — продолжаем на ${PY_VERSION}."
        hint "Часть движков не установится; это будет видно в итоге установки."
      fi
    else
      info "Остаёмся на Python ${PY_VERSION} по вашему выбору."
      hint "Если движки не встанут, причина будет эта."
    fi
  fi

  ok "Python: ${PY} ($("${PY}" --version 2>&1))"
  # Имя пакета venv выводится по выбранному интерпретатору — он мог смениться.
  export ASRHUB_PYTHON_FOR_PACKAGES="${PY}"
else
  require_command docker "Установите Docker: https://docs.docker.com/engine/install/" || exit 127
  if ! docker info >/dev/null 2>&1; then
    error "Docker установлен, но демон недоступен."
    hint "Запустите: sudo systemctl start docker"
    hint "Либо добавьте пользователя в группу docker: sudo usermod -aG docker \$USER"
    exit 1
  fi
  ok "Docker доступен"
fi

check_disk_space "${DATA_DIR}" "$(profile_disk_gb "${PROFILE}")"
check_memory 8

if ! check_port_free "${PORT}"; then
  warn "Порт ${PORT} занят."
  NEW_PORT="$(find_free_port "$((PORT + 1))")"
  if confirm "Использовать свободный порт ${NEW_PORT}?"; then
    PORT="${NEW_PORT}"
    ok "Выбран порт ${PORT}"
  else
    error "Освободите порт ${PORT} и повторите установку."
    exit 1
  fi
fi

if [[ "${OFFLINE}" -eq 0 ]]; then
  if check_network pypi.org; then
    ok "Доступ в интернет есть"
  else
    warn "Нет доступа к pypi.org."
    hint "Продолжаем в автономном режиме: пакеты берутся из локального кеша pip."
    OFFLINE=1
  fi
fi

if [[ -d "${PREFIX}" && "${FORCE}" -eq 0 ]]; then
  if [[ -f "${PREFIX}/VERSION" ]]; then
    info "Обнаружена установка версии $(cat "${PREFIX}/VERSION" 2>/dev/null || echo '?')."
    confirm "Обновить существующую установку?" || { info "Отменено."; exit 0; }
  fi
fi

confirm "Начать установку?" || { info "Отменено пользователем."; exit 0; }

# ---------------------------------------------------------------------------
# Шаг 2. Системные зависимости
# ---------------------------------------------------------------------------

step "Системные зависимости"

MISSING_SYS=()
have ffmpeg || MISSING_SYS+=("$(system_package_names ffmpeg)")
have git || MISSING_SYS+=("$(system_package_names git)")
if [[ "${MODE}" == "native" ]]; then
  # Имя пакета зависит от версии выбранного интерпретатора — сообщаем её.
  export ASRHUB_PYTHON_FOR_PACKAGES="${PY}"
  # Проверять надо ensurepip, а не venv: venv — часть стандартной библиотеки
  # и импортируется всегда, даже когда пакета python3.X-venv нет. В Debian и
  # Ubuntu в этом пакете лежит именно ensurepip, без которого `python -m venv`
  # доходит до конца и падает словами «ensurepip is not available» — уже
  # после того, как каталоги созданы. Проверка смотрела на venv и поэтому
  # ничего не находила.
  if ! "${PY}" -c 'import ensurepip' >/dev/null 2>&1; then
    MISSING_SYS+=($(system_package_names python-venv))
  fi
  [[ "${ENGINES}" == *nemo* ]] && MISSING_SYS+=($(system_package_names sndfile))
  [[ "${ENGINES}" == *whisper_cpp* ]] && MISSING_SYS+=("$(system_package_names cmake)" $(system_package_names build))
fi

# Осторожно: при пустом массиве grep не находит строк и возвращает 1,
# что под pipefail роняет установку. Поэтому пустоту проверяем заранее.
if [[ ${#MISSING_SYS[@]} -gt 0 ]]; then
  # readarray появился в bash 4.0, а macOS штатно поставляет 3.2.57 — и
  # именно на macOS эта ветка срабатывает всегда, потому что ffmpeg там не
  # предустановлен. Установка обрывалась на втором шаге с «readarray:
  # command not found», а подсказка звала «установить недостающую
  # программу», хотя не хватало встроенной команды оболочки.
  _uniq=""
  while IFS= read -r _line; do
    [[ -n "${_line}" ]] && _uniq="${_uniq} ${_line}"
  done < <(printf '%s\n' "${MISSING_SYS[@]}" | tr ' ' '\n' | grep -v '^$' | sort -u || true)
  # shellcheck disable=SC2206
  MISSING_SYS=(${_uniq})
  unset _uniq _line
fi
if [[ ${#MISSING_SYS[@]} -gt 0 ]]; then
  info "Не хватает: ${MISSING_SYS[*]}"
  if confirm "Установить системные пакеты?"; then
    install_system_packages "${MISSING_SYS[@]}" || {
      warn "Не удалось установить часть пакетов."
      hint "Установите вручную: ${MISSING_SYS[*]}"
      confirm "Продолжить без них?" || exit 1
    }
  else
    warn "Продолжаем без системных пакетов — часть возможностей будет недоступна."
  fi
else
  ok "Все системные зависимости на месте"
fi

# ---------------------------------------------------------------------------
# Шаг 3. Видеокарта: драйвер и настройка
# ---------------------------------------------------------------------------
#
# Раньше установщик просто спрашивал у nvidia-smi, есть ли видеокарта. На
# машине без драйвера ответ был «нет», и всё дальнейшее — колёса PyTorch,
# профиль, конфигурация — собиралось под процессор. Карта в этой машине при
# этом стояла. Здесь она ищется на шине PCI и, если драйвера нет, ставится.

step "Видеокарта"

if [[ "${MODE}" == "docker" ]]; then
  # В контейнере драйвер берётся с хоста; ставить его внутрь нечего.
  gpu_report
  [[ "${ACCEL}" == "cuda" ]] || info "Для видеокарты в контейнере нужен NVIDIA Container Toolkit на хосте."
elif [[ "${OS}" != "linux" ]]; then
  gpu_report
else
  gpu_report
  if gpu_ensure_driver "${GPU_DRIVER}"; then
    # Пересчитываем: драйвер мог появиться только что.
    ACCEL="$(detect_gpu)"
    GPU_LINE="$(gpu_primary)"
    if [[ -n "${GPU_LINE}" ]]; then
      GPU_VENDOR="$(printf '%s' "${GPU_LINE}" | cut -d'|' -f2)"
      case "$(gpu_vendor_key "${GPU_VENDOR}")" in
        nvidia) GPU_TARGET_ACCEL="cuda" ;;
        amd)    GPU_TARGET_ACCEL="rocm" ;;
        intel)  GPU_TARGET_ACCEL="xpu" ;;
      esac
    fi
    gpu_tune
  fi
  # Профиль подбирался по железу до установки драйвера. Если карта появилась
  # только что, «cpu» больше не лучший выбор — но молча менять то, что
  # пользователь подтвердил в мастере, нельзя.
  if [[ "${ACCEL}" != "cpu" && "${PROFILE}" == "cpu" ]]; then
    info "Видеокарта заработала — профиль «cpu» теперь занижен."
    hint "Сменить набор моделей можно позже: bash scripts/models.sh --profile standard"
  fi
fi

# ---------------------------------------------------------------------------
# Шаг 4. Каталоги
# ---------------------------------------------------------------------------

step "Создание каталогов"

CREATED_PREFIX=0
[[ -d "${PREFIX}" ]] || CREATED_PREFIX=1
ensure_dir "${PREFIX}"
ensure_dir "${DATA_DIR}" 0750
for sub in uploads results models logs tmp; do ensure_dir "${DATA_DIR}/${sub}" 0750; done
# На macOS каталог данных лежит внутри prefix, поэтому откат удаляет только
# подкаталоги программы, а не prefix целиком: иначе снесёт модели и базу.
if [[ "${CREATED_PREFIX}" -eq 1 ]]; then
  if [[ "${DATA_DIR}" == "${PREFIX}"/* ]]; then
    for sub in server scripts config requirements docker examples venv; do
      add_rollback "rm -rf '${PREFIX}/${sub}'"
    done
  else
    add_rollback "rm -rf '${PREFIX}'"
  fi
fi
ok "Каталоги готовы"

# ---------------------------------------------------------------------------
# Шаг 5. Копирование файлов
# ---------------------------------------------------------------------------

step "Копирование файлов приложения"

# Запуск из уже установленной копии — обычное дело: установщик сам печатает
# путь вида /opt/asrhub/scripts/. Без этой проверки цикл удалял каталог и
# тут же пытался скопировать его сам в себя: установка оставалась без
# server, а шапка файла обещала «повторный запуск не ломает установку».
SRC_REAL="$(cd "${REPO_DIR}" 2>/dev/null && pwd -P || echo "${REPO_DIR}")"
DST_REAL="$(cd "${PREFIX}" 2>/dev/null && pwd -P || echo "${PREFIX}")"
if [[ "${SRC_REAL}" == "${DST_REAL}" ]]; then
  info "Источник совпадает с установкой — файлы уже на месте, копирование пропущено."
elif [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
  # examples тоже: раздел «Диктовка» и документация ссылаются на
  # examples/stream_microphone.py как на запасной путь без микрофона, а в
  # установке этого файла не было вовсе.
  for item in server scripts config requirements docker examples VERSION README.md; do
    [[ -e "${REPO_DIR}/${item}" ]] || continue
    rm -rf "${PREFIX:?}/${item}"
    cp -a "${REPO_DIR}/${item}" "${PREFIX}/"
  done
  chmod +x "${PREFIX}"/scripts/*.sh 2>/dev/null || true
  chmod +x "${PREFIX}"/scripts/client/asrctl 2>/dev/null || true
fi
ok "Файлы скопированы в ${PREFIX}"

# ---------------------------------------------------------------------------
# Шаг 6. Развёртывание
# ---------------------------------------------------------------------------

if [[ "${MODE}" == "docker" ]]; then
  step "Сборка и запуск контейнера"

  COMPOSE="docker compose"
  docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"
  have "${COMPOSE%% *}" || { error "Не найден docker compose."; exit 127; }

  ENV_FILE="${PREFIX}/docker/.env"
  write_file "${ENV_FILE}" 0640 <<ENVEOF
# Создано установщиком ASR Hub $(date '+%Y-%m-%d %H:%M')
ASRHUB_PORT=${PORT}
ASRHUB_HOST=${HOST}
ASRHUB_DATA=${DATA_DIR}
ASRHUB_PROFILE=${PROFILE}
ASRHUB_ENGINES=${ENGINES}
ASRHUB_ACCEL=${ACCEL}
ENVEOF

  # Токен пишем отдельной строкой и только если он есть: пустое значение в
  # env.sh перекрыло бы токен из окружения службы. Файл уже 0640 — в нём
  # лежит секрет, и читать его должен только владелец установки.
  if [[ -n "${HF_TOKEN_VALUE}" ]]; then
    if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
      printf '%s[пробный запуск]%s HF_TOKEN дописан в %s\n' \
        "${C_YELLOW}" "${C_RESET}" "${ENV_FILE}"
    else
      printf 'HF_TOKEN=%s\n' "${HF_TOKEN_VALUE}" >> "${ENV_FILE}"
      chmod 0640 "${ENV_FILE}" 2>/dev/null || true
      ok "Токен Hugging Face записан (${HF_TOKEN_VALUE:0:6}…, ${#HF_TOKEN_VALUE} символов)"
    fi
  fi

  # Вариант с видеокартой — файл-надстройка, а не отдельный профиль: сервис
  # без профиля поднимается всегда, поэтому «--profile gpu up» запускал и
  # процессорный контейнер тоже, и второй падал с конфликтом за порт.
  COMPOSE_FILES=(-f docker-compose.yml)
  if [[ "${ACCEL}" == "cuda" ]]; then
    COMPOSE_FILES+=(-f docker-compose.gpu.yml)
    info "Сборка под видеокарту NVIDIA (docker-compose.gpu.yml)"
  fi

  # Владелец каталога данных: контейнер приведёт права к нему при запуске.
  {
    # Под sudo id -u даёт ноль, и контейнер запускался от root, минуя
    # понижение прав через gosu. Берём того, кто вызвал sudo.
    echo "ASRHUB_UID=${SUDO_UID:-$(id -u)}"
    echo "ASRHUB_GID=${SUDO_GID:-$(id -g)}"
  } >> "${ENV_FILE}"

  info "Сборка образа (первый раз занимает 10–25 минут)…"
  ( cd "${PREFIX}/docker" \
      && retry 2 run ${COMPOSE} --env-file .env "${COMPOSE_FILES[@]}" build )
  ( cd "${PREFIX}/docker" \
      && run ${COMPOSE} --env-file .env "${COMPOSE_FILES[@]}" up -d )
  add_rollback "cd '${PREFIX}/docker' && ${COMPOSE} ${COMPOSE_FILES[*]} down"
  ok "Контейнер запущен"

else
  step "Виртуальное окружение Python"

  # Готовое окружение переиспользуется только если оно собрано ТЕМ ЖЕ
  # интерпретатором. Раньше проверялось лишь наличие каталога, и это сводило
  # на нет всю работу по выбору версии: установщик находил слишком новый
  # Python, ставил рядом проверенный, объявлял «дальше работаем на 3.13» — и
  # тут же брал venv, собранный на 3.14. Движки падали ровно как прежде, при
  # том что в отчёте стояла правильная версия.
  if [[ -d "${VENV}" ]]; then
    VENV_PY_VERSION=""
    if [[ -x "${VENV}/bin/python" ]]; then
      VENV_PY_VERSION="$("${VENV}/bin/python" -c \
        'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    fi
    WANT_PY_VERSION="$("${PY}" -c \
      'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    if [[ -z "${VENV_PY_VERSION}" ]]; then
      warn "Существующее окружение повреждено — пересоберём."
      run rm -rf "${VENV}"
    elif [[ "${VENV_PY_VERSION}" != "${WANT_PY_VERSION}" ]]; then
      info "Окружение собрано на Python ${VENV_PY_VERSION}, а ставим на ${WANT_PY_VERSION}."
      hint "Пакеты придётся установить заново — иначе движки останутся от прошлой версии."
      if [[ "${FORCE}" -eq 1 ]] || confirm "Пересобрать окружение?" "y"; then
        run rm -rf "${VENV}"
      else
        warn "Оставляем окружение на Python ${VENV_PY_VERSION}."
        hint "Движки, которых нет под эту версию, так и не установятся."
      fi
    else
      debug "окружение уже собрано на Python ${VENV_PY_VERSION} — переиспользуем"
    fi
  fi

  if [[ ! -d "${VENV}" ]]; then
    if ! run "${PY}" -m venv "${VENV}"; then
      # Самая частая причина в Debian и Ubuntu — отсутствующий ensurepip.
      # Сообщение интерпретатора выше правильное, но теряется среди отката,
      # поэтому повторяем его своими словами и с готовой командой.
      error "Не удалось создать виртуальное окружение Python."
      if ! "${PY}" -c 'import ensurepip' >/dev/null 2>&1; then
        VENV_PKG="$(system_package_names python-venv)"
        hint "В этой сборке Python нет ensurepip — он поставляется отдельным пакетом."
        hint "Установите его и повторите: sudo apt install ${VENV_PKG}"
        hint "Интерпретатор: ${PY} ($("${PY}" -V 2>&1))"
      else
        hint "Проверьте права на «${PREFIX}» и свободное место на диске."
      fi
      hint "Другой интерпретатор можно задать ключом: --python /usr/bin/python3.12"
      exit 1
    fi
    add_rollback "rm -rf '${VENV}'"
  fi
  VPY="${VENV}/bin/python"
  VPIP="${VENV}/bin/pip"
  [[ "${ASRHUB_DRY_RUN}" == "1" ]] || {
    [[ -x "${VPY}" ]] || { error "Виртуальное окружение создано некорректно: нет ${VPY}"; exit 1; }
  }

  PIP_FLAGS=(--disable-pip-version-check --no-input)
  [[ "${OFFLINE}" -eq 1 ]] && PIP_FLAGS+=(--no-index)
  [[ "${ASRHUB_QUIET}" == "1" ]] && PIP_FLAGS+=(-q)

  info "Обновление pip…"
  run_quiet "${VPY}" -m pip install "${PIP_FLAGS[@]}" --upgrade pip setuptools wheel >/dev/null || \
    warn "Не удалось обновить pip — продолжаем с текущей версией."

  step "Установка зависимостей сервера"
  retry 3 run "${VPIP}" install "${PIP_FLAGS[@]}" -r "${PREFIX}/requirements/base.txt"
  ok "Базовые зависимости установлены"

  # PyTorch ставим отдельно: у него свой индекс под каждый ускоритель
  NEEDS_TORCH=0
  for engine in gigaam faster_whisper whisper nemo transformers whisperx qwen3_asr voxtral; do
    [[ "${ENGINES}" == *"${engine}"* ]] && NEEDS_TORCH=1
  done
  if [[ "${NEEDS_TORCH}" -eq 1 ]]; then
    # Колёса выбираем под ту карту, что найдена на шине, а не под ту, что
    # видна прямо сейчас: после установки драйвера до перезагрузки карта не
    # отвечает, и обычная проверка дала бы процессорный torch — с ним и
    # после перезагрузки считал бы процессор.
    TORCH_ACCEL="${GPU_TARGET_ACCEL:-${ACCEL}}"
    [[ "${TORCH_ACCEL}" == "xpu" ]] && TORCH_ACCEL="cpu"   # для Intel индекса нет
    step "Установка PyTorch для ускорителя «${TORCH_ACCEL}»"
    if [[ "${TORCH_ACCEL}" != "${ACCEL}" ]]; then
      info "Карта пока не отвечает, но драйвер установлен — берём колёса под неё."
    fi
    TORCH_INDEX="$(torch_index_url "${TORCH_ACCEL}")"
    if [[ -n "${TORCH_INDEX}" ]]; then
      info "Индекс пакетов: ${TORCH_INDEX}"
      retry 3 run "${VPIP}" install "${PIP_FLAGS[@]}" --index-url "${TORCH_INDEX}" torch torchaudio || {
        warn "Установка с индекса ${TORCH_INDEX} не удалась, пробуем обычный индекс."
        retry 2 run "${VPIP}" install "${PIP_FLAGS[@]}" torch torchaudio
      }
    else
      retry 3 run "${VPIP}" install "${PIP_FLAGS[@]}" torch torchaudio
    fi
    ok "PyTorch установлен"
  fi

  step "Установка движков распознавания"
  IFS=',' read -ra ENGINE_LIST <<< "${ENGINES}"
  FAILED_ENGINES=()
  for engine in "${ENGINE_LIST[@]}"; do
    engine="$(printf '%s' "${engine}" | tr -d ' ')"
    [[ -z "${engine}" ]] && continue
    REQ="${PREFIX}/requirements/engines/${engine//_/-}.txt"
    if [[ ! -f "${REQ}" ]]; then
      warn "Нет файла зависимостей для движка «${engine}» — пропускаем."
      continue
    fi
    info "Движок: ${engine}"
    if [[ "${engine}" == "faster_whisper" && "${GPU_TARGET_ACCEL:-${ACCEL}}" == "cuda" ]]; then
      # Пин ctranslate2 под установленный cuDNN — иначе типовая ошибка libcudnn
      PIN="$(ctranslate2_pin "${ACCEL}")"
      info "Версия CTranslate2 под вашу CUDA: ${PIN}"
      retry 2 run "${VPIP}" install "${PIP_FLAGS[@]}" "${PIN}" || true
    fi
    if retry 2 run "${VPIP}" install "${PIP_FLAGS[@]}" -r "${REQ}"; then
      ok "  ${engine} установлен"
    else
      FAILED_ENGINES+=("${engine}")
      warn "  ${engine}: установка не удалась — сервер запустится без него"
    fi
  done

  if [[ "${ENGINES}" == *whisper_cpp* ]]; then
    step "Сборка whisper.cpp"
    build_whisper_cpp || warn "whisper.cpp собрать не удалось — движок будет недоступен."
  fi
fi

# ---------------------------------------------------------------------------
# Шаг 7. Конфигурация
# ---------------------------------------------------------------------------

step "Создание конфигурации"

CONFIG_FILE="${DATA_DIR}/config.yaml"
if [[ -f "${CONFIG_FILE}" && "${FORCE}" -eq 0 ]]; then
  info "Конфигурация уже существует — оставляем без изменений."
  info "Полный пример со всеми параметрами: ${DATA_DIR}/config.example.yaml"
else
  if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
    RECOMMENDED_MODEL="$(printf '%s' "${MODELS}" | cut -d, -f1)"
    [[ -z "${RECOMMENDED_MODEL}" ]] && RECOMMENDED_MODEL="demo-simulator"
    # Устройство и точность под найденную карту. Пусто — оставляем auto:
    # сервер определит сам, и это правильное поведение для машины без карты
    # и для той, где драйвер заработает только после перезагрузки.
    GPU_BATCHING_BLOCK="$(gpu_config_lines | sed 's/^/  /')"
    [[ -z "${GPU_BATCHING_BLOCK}" ]] && GPU_BATCHING_BLOCK="  device: auto
  compute_type: auto"
    # 0640: в config.yaml сервер дописывает ключи доступа с их группами и
  # квотами. Читать его должен только владелец установки.
  write_file "${CONFIG_FILE}" 0640 <<CFGEOF
# Конфигурация ASR Hub — создана установщиком $(date '+%Y-%m-%d %H:%M')
# Полный список параметров с описаниями: ${DATA_DIR}/config.example.yaml
# Все параметры также доступны в веб-интерфейсе в разделе «Настройки».

data_dir: ${DATA_DIR}

model:
  model: ${RECOMMENDED_MODEL}
  engine: auto
  language: ru

server:
  server_host: ${HOST}
  server_port: ${PORT}
  auth_enabled: true
  max_upload_mb: 2048
  log_level: INFO
  # Распознавание на лету: раздел «Диктовка» и WebSocket /api/stream.
  # stream_window_s — шаг гипотез для движков, которые не держат состояние
  # между кусками: меньше значит чаще обновления и больше повторной работы.
  stream_enabled: ${STREAM_ENABLED}
  stream_window_s: 4

batching:
${GPU_BATCHING_BLOCK}

queue:
  max_concurrent_jobs: $( [[ "${ACCEL}" == "cpu" ]] && echo 1 || echo 2 )
  scheduling_policy: priority_fifo
  result_retention_days: 30

runtime:
  models_dir: ${DATA_DIR}/models
  temp_dir: ${DATA_DIR}/tmp
CFGEOF

    # Выбранное в мастере дописывается отдельно, чтобы шаблон выше оставался
    # одинаковым для любой установки.
    if [[ -n "${ALIGNMENT}" && "${ALIGNMENT}" != "none" ]]; then
      cat >> "${CONFIG_FILE}" <<CFGEOF

postprocess:
  # Уточнение границ слов по звуку. Если выравниватель недоступен,
  # задание доводится до конца на таймкодах модели.
  alignment_backend: ${ALIGNMENT}
CFGEOF
    fi

    if [[ -n "${MONITORING}" ]]; then
      cat >> "${CONFIG_FILE}" <<CFGEOF

monitoring:
  monitoring_push_enabled: true
  monitoring_targets:
    - kind: ${MONITORING}
      url: ${MONITORING_URL:-http://localhost:9091}
      interval_s: 60
CFGEOF
    fi

    add_rollback "rm -f '${CONFIG_FILE}'"
  fi
  ok "Конфигурация: ${CONFIG_FILE}"
fi

# Токен Hugging Face. Пишем его в config.yaml, а не в env.sh: переменные
# окружения читает systemd, но не launchd на macOS и не запуск руками через
# asrctl, а конфигурацию читают все три. Раньше токен, введённый в мастере,
# доходил только до docker/.env — при обычной установке он молча пропадал, и
# диаризация падала на седьмом шаге с «нужен токен».
#
# Строку заменяем, а не дописываем: два ключа hf_token в YAML — это молча
# выигравший последний, то есть потерянный только что введённый токен.
if [[ -n "${HF_TOKEN_VALUE}" && "${MODE}" == "native" ]]; then
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    info "[пробный запуск] hf_token записан бы в ${CONFIG_FILE}"
  elif [[ -f "${CONFIG_FILE}" ]]; then
    HF_TMP="${CONFIG_FILE}.hf.$$"
    grep -v '^hf_token:' "${CONFIG_FILE}" > "${HF_TMP}" 2>/dev/null || true
    # Файл мог остаться без перевода строки в конце — тогда ключ приклеился бы
    # к последней строке и YAML перестал бы читаться целиком.
    if [[ -s "${HF_TMP}" ]] && [[ -n "$(tail -c1 "${HF_TMP}")" ]]; then
      printf '\n' >> "${HF_TMP}"
    fi
    printf 'hf_token: "%s"\n' "${HF_TOKEN_VALUE}" >> "${HF_TMP}"
    mv -f "${HF_TMP}" "${CONFIG_FILE}"
    chmod 0640 "${CONFIG_FILE}" 2>/dev/null || true
    ok "Токен Hugging Face записан в конфигурацию (${HF_TOKEN_VALUE:0:6}…, ${#HF_TOKEN_VALUE} символов)"
  else
    warn "Файл конфигурации не найден — токен Hugging Face не записан."
    hint "Дописать вручную: строка hf_token: \"${HF_TOKEN_VALUE:0:6}…\" в ${CONFIG_FILE}"
  fi
fi

# Переменные окружения под видеокарту. Их читает служба (EnvironmentFile) и
# ручной запуск через asrctl. В конфигурацию они не идут: это настройки
# драйвера, а не сервера, и в разных машинах они разные.
if [[ -n "${GPU_ENV_OVERRIDE}" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
  ENV_SH="${DATA_DIR}/env.sh"
  # Строки с прошлого запуска убираем: иначе после смены видеокарты
  # осталась бы подсказка про старую, и новая работала бы неправильно.
  if [[ -f "${ENV_SH}" ]]; then
    grep -v '^# ASRHUB-GPU$' "${ENV_SH}" 2>/dev/null \
      | grep -vE '^(HSA_OVERRIDE_GFX_VERSION|NEOReadDebugKeys|ClDeviceGlobalMemSizeAvailablePercent)=' \
      > "${ENV_SH}.new" || true
    mv -f "${ENV_SH}.new" "${ENV_SH}"
  fi
  {
    printf '# ASRHUB-GPU\n'
    printf '%s\n' "${GPU_ENV_OVERRIDE//;/$'\n'}"
  } >> "${ENV_SH}"
  ok "Переменные окружения видеокарты: ${ENV_SH}"
fi

if [[ "${MODE}" == "native" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
  ( cd "${PREFIX}/server" && "${VENV}/bin/python" -m asrhub --print-config \
      > "${DATA_DIR}/config.example.yaml" 2>/dev/null ) || true
fi

# ---------------------------------------------------------------------------
# Выравнивание (по запросу мастера или ключа --alignment)
# ---------------------------------------------------------------------------

if [[ -n "${ALIGNMENT}" && "${ALIGNMENT}" != "none" && "${MODE}" == "native" ]]; then
  info "Установка выравнивания «${ALIGNMENT}»…"
  if ! ASRHUB_DATA_DIR="${DATA_DIR}" run bash "${PREFIX}/scripts/models.sh" \
       install-engine "${ALIGNMENT}" --prefix "${PREFIX}"; then
    warn "Выравнивание не установлено — распознавание это не затронет."
    hint "Повторить позже: bash ${PREFIX}/scripts/models.sh install-engine ${ALIGNMENT}"
  fi
fi

# ---------------------------------------------------------------------------
# Шаг 8. Загрузка моделей
# ---------------------------------------------------------------------------

if [[ -n "${MODELS}" && "${SKIP_MODELS}" -eq 0 && "${MODE}" == "native" ]]; then
  step "Загрузка моделей"
  IFS=',' read -ra MODEL_LIST <<< "${MODELS}"
  for model in "${MODEL_LIST[@]}"; do
    model="$(printf '%s' "${model}" | tr -d ' ')"
    [[ -z "${model}" ]] && continue
    info "Модель: ${model}"
    if ! ASRHUB_DATA_DIR="${DATA_DIR}" run bash "${PREFIX}/scripts/models.sh" download "${model}"; then
      warn "  не удалось загрузить ${model} — можно повторить позже:"
      hint "  bash ${PREFIX}/scripts/models.sh download ${model}"
    fi
  done
else
  [[ "${SKIP_MODELS}" -eq 1 ]] && info "Загрузка моделей пропущена (--skip-models)."
fi

# ---------------------------------------------------------------------------
# Шаг 9. Служба автозапуска
# ---------------------------------------------------------------------------

if [[ "${CREATE_SERVICE}" -eq 1 && "${MODE}" == "native" ]]; then
  step "Настройка автозапуска"

  # Служба по умолчанию работала от root: пользователь не задавался, а
  # service.sh подставлял root для системного юнита. Сервер принимает
  # загрузку файлов из сети, распаковывает архивы моделей и исполняет код
  # движков — делать это от root незачем. Заводим отдельную учётную запись
  # без домашнего каталога и оболочки входа.
  if [[ -z "${SERVICE_USER}" && "${OS}" == "linux" ]] && is_root; then
    if id -u asrhub >/dev/null 2>&1; then
      SERVICE_USER="asrhub"
      info "Служба будет работать от существующего пользователя asrhub."
    elif have useradd; then
      if run useradd --system --no-create-home --shell /usr/sbin/nologin asrhub 2>/dev/null \
         || run useradd --system --no-create-home --shell /sbin/nologin asrhub 2>/dev/null; then
        SERVICE_USER="asrhub"
        add_rollback "userdel asrhub 2>/dev/null || true"
        ok "Создан системный пользователь asrhub для службы."
      else
        warn "Не удалось создать пользователя asrhub — служба будет работать от root."
      fi
    else
      warn "Команда useradd недоступна — служба будет работать от root."
    fi
    if [[ -n "${SERVICE_USER}" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
      # Каталог данных может быть общим: сервер умеет работать в нескольких
      # экземплярах над одной базой. `useradd --system` даёт на каждой машине
      # свой свободный uid, поэтому безусловный chown -R отбирал общий
      # каталог у соседей — они переставали читать собственную базу.
      # Если каталог уже принадлежит кому-то другому, спрашиваем.
      DATA_OWNER="$(stat -c '%U' "${DATA_DIR}" 2>/dev/null \
                    || stat -f '%Su' "${DATA_DIR}" 2>/dev/null || echo '')"
      if [[ -n "${DATA_OWNER}" && "${DATA_OWNER}" != "root" \
            && "${DATA_OWNER}" != "${SERVICE_USER}" ]]; then
        warn "Каталог данных «${DATA_DIR}» принадлежит пользователю ${DATA_OWNER}."
        hint "Похоже, над ним уже работает другая установка ASR Hub."
        hint "Смена владельца отберёт у неё доступ к собственной базе."
        if confirm "Всё равно передать каталог пользователю ${SERVICE_USER}?" "n"; then
          run chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" || \
            warn "Не удалось передать каталог данных пользователю ${SERVICE_USER}."
        else
          info "Владелец каталога данных не меняется."
          hint "Для общего каталога заведите одну группу и дайте её обоим"
          hint "пользователям: chgrp -R asrhub '${DATA_DIR}' && chmod -R g+rwX '${DATA_DIR}'"
        fi
      else
        run chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}" || \
          warn "Не удалось передать каталог данных пользователю ${SERVICE_USER}."
      fi
    fi
  fi

  bash "${SCRIPT_DIR}/service.sh" install \
    --prefix "${PREFIX}" --data "${DATA_DIR}" \
    --port "${PORT}" --host "${HOST}" --name "${SERVICE_NAME}" \
    ${SERVICE_USER:+--user "${SERVICE_USER}"} || {
      warn "Служба не создана."
      hint "Запускать вручную: ${VENV}/bin/python -m asrhub --port ${PORT}"
    }
else
  info "Служба автозапуска не создаётся."
fi

# ---------------------------------------------------------------------------
# Шаг 10. Проверка
# ---------------------------------------------------------------------------

step "Проверка установки"

if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
  ok "Пробный запуск завершён — изменений не вносилось."
  exit 0
fi

HEALTH_OK=0
for attempt in $(seq 1 20); do
  if have curl && curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    HEALTH_OK=1; break
  fi
  sleep 2
done

if [[ "${HEALTH_OK}" -eq 1 ]]; then
  ok "Сервер отвечает на http://127.0.0.1:${PORT}"
else
  warn "Сервер пока не отвечает."
  hint "Проверьте состояние: bash ${PREFIX}/scripts/service.sh status"
  hint "Журнал службы: bash ${PREFIX}/scripts/service.sh logs"
fi

clear_rollback

API_KEY="$(cat "${DATA_DIR}/api-key.txt" 2>/dev/null || echo '')"

printf '\n%s%sУстановка завершена%s\n\n' "${C_BOLD}" "${C_GREEN}" "${C_RESET}"
printf '  Веб-интерфейс     %shttp://%s:%s%s\n' "${C_BOLD}" \
  "$( [[ "${HOST}" == "0.0.0.0" ]] && hostname -I 2>/dev/null | awk '{print $1}' || echo "${HOST}" )" "${PORT}" "${C_RESET}"
printf '  Локально          http://127.0.0.1:%s\n' "${PORT}"
printf '  Справочник API    http://127.0.0.1:%s/api/reference\n' "${PORT}"
[[ -n "${API_KEY}" ]] && printf '  Ключ доступа      %s%s%s\n' "${C_BOLD}" "${API_KEY}" "${C_RESET}"
printf '  Каталог программы %s\n' "${PREFIX}"
printf '  Каталог данных    %s\n' "${DATA_DIR}"
printf '  Конфигурация      %s\n' "${CONFIG_FILE}"
printf '  Журнал установки  %s\n' "${ASRHUB_LOG_FILE}"
printf '\n%sЧто дальше%s\n' "${C_BOLD}" "${C_RESET}"
printf '  Проверить окружение     bash %s/scripts/doctor.sh\n' "${PREFIX}"
printf '  Управлять моделями      bash %s/scripts/models.sh list\n' "${PREFIX}"
printf '  Управлять службой       bash %s/scripts/service.sh {start|stop|status|logs}\n' "${PREFIX}"
printf '  Обновить                bash %s/scripts/update.sh\n' "${PREFIX}"
printf '  Удалить                 bash %s/scripts/uninstall.sh\n' "${PREFIX}"
if [[ ${#FAILED_ENGINES[@]} -gt 0 ]]; then
  printf '\n%sНе установились движки: %s%s\n' "${C_YELLOW}" "${FAILED_ENGINES[*]}" "${C_RESET}"
  # Если окружение собрано на слишком новом Python, причина почти наверняка
  # в этом, и повторять установку тем же способом бессмысленно.
  PY_VERSION="$("${PY}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  if [[ -n "${PY_VERSION}" ]] && version_gt "${PY_VERSION}" "${ASRHUB_MAX_PYTHON}"; then
    printf '  Окружение собрано на Python %s — новее проверенной %s.\n' \
      "${PY_VERSION}" "${ASRHUB_MAX_PYTHON}"
    printf '  Под свежие версии колёс torch, onnxruntime и nemo ещё нет.\n'
    printf '  Пересоберите на проверенной версии:\n'
    printf '    sudo apt install python%s python%s-venv python%s-dev\n' \
      "${ASRHUB_MAX_PYTHON}" "${ASRHUB_MAX_PYTHON}" "${ASRHUB_MAX_PYTHON}"
    printf '    sudo bash %s/scripts/install.sh --python /usr/bin/python%s --force\n' \
      "${PREFIX}" "${ASRHUB_MAX_PYTHON}"
  else
    printf '  Повторить: bash %s/scripts/models.sh install-engine <движок>\n' "${PREFIX}"
  fi
fi

# Про перезагрузку говорим последней строкой, а не в середине установки:
# именно её пользователь и должен унести с собой.
if gpu_reboot_required; then
  printf '\n%s%sНужна перезагрузка%s\n' "${C_BOLD}" "${C_YELLOW}" "${C_RESET}"
  printf '  Драйвер видеокарты установлен, но модуль ядра загрузится только\n'
  printf '  после перезагрузки. До неё сервер считает на процессоре.\n'
  printf '  После перезагрузки проверьте: bash %s/scripts/doctor.sh\n' "${PREFIX}"
fi
printf '\n'
