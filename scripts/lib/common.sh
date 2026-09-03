#!/usr/bin/env bash
# Общая библиотека скриптов ASR Hub для Linux и macOS.
#
# Подключение:  source "$(dirname "$0")/lib/common.sh"
#
# Предоставляет: журналирование, обработку ошибок с трассировкой, откат
# изменений, повторы с нарастающей задержкой, проверки окружения и
# безопасные операции с файлами.

# Локаль UTF-8 нужна, чтобы ${#строка} считала символы, а не байты:
# без неё выравнивание таблиц с кириллицей разъезжается.
if [[ -z "${LC_ALL:-}" ]]; then
  for _candidate in C.UTF-8 C.utf8 en_US.UTF-8 ru_RU.UTF-8; do
    if locale -a 2>/dev/null | grep -qix "${_candidate}"; then
      export LC_ALL="${_candidate}"
      break
    fi
  done
  unset _candidate
fi

# Строгий режим: падаем на первой ошибке, на необъявленной переменной
# и на ошибке в любом звене конвейера.
set -o errexit
set -o nounset
set -o pipefail
# errtrace обязателен, а не «на всякий случай»: без него ловушка ERR не
# наследуется функциями, а вся работа скриптов в них и происходит. Сбой в
# run_wizard, download, install_system_packages, gpu_install_* или check_python
# просто завершал установку с кодом 1 — без сообщения об ошибке, без записи в
# журнал и, главное, без отката уже сделанных изменений, который шапка
# install.sh обещает прямым текстом.
set -o errtrace
shopt -s inherit_errexit 2>/dev/null || true

# ---------------------------------------------------------------------------
# Константы и глобальное состояние
# ---------------------------------------------------------------------------

ASRHUB_VERSION="3.0.0"
ASRHUB_MIN_PYTHON="3.10"
# Верхняя граница — не каприз, а состояние экосистемы. Движки распознавания
# тянут за собой torch, onnxruntime, nemo и десяток библиотек с колёсами под
# конкретные версии Python; на версии новее поддерживаемой их просто нет, и
# установка обрывается не у нас, а внутри pip — на восьмом шаге, когда всё
# остальное уже сделано. Пример: GigaAM требует onnxruntime==1.23.*, а у
# 1.23 колёса есть только до cp313 включительно.
ASRHUB_MAX_PYTHON="${ASRHUB_MAX_PYTHON:-3.13}"
ASRHUB_DEFAULT_PORT="8080"

: "${ASRHUB_LOG_FILE:=}"
: "${ASRHUB_DRY_RUN:=0}"
# Флаг обязан переживать вызов дочернего скрипта: install.sh и uninstall.sh
# запускают service.sh отдельным bash, и без export в дочернем процессе он
# снова становился нулём — «пробный запуск» на самом деле создавал юнит
# systemd, включал его в автозапуск и (при удалении) сносил работающую
# службу.
export ASRHUB_DRY_RUN
: "${ASRHUB_ASSUME_YES:=0}"
: "${ASRHUB_QUIET:=0}"
: "${ASRHUB_NO_COLOR:=0}"

declare -a _ROLLBACK_ACTIONS=()
declare -a _CLEANUP_PATHS=()
_STEP_CURRENT=""
_STEP_INDEX=0
_STEP_TOTAL=0

# Цвета включаем только для терминала и когда их не запретили
if [[ -t 1 && "${ASRHUB_NO_COLOR}" != "1" && -z "${NO_COLOR:-}" ]]; then
  C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m'; C_GREY=$'\033[90m'
else
  C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""
  C_YELLOW=""; C_BLUE=""; C_CYAN=""; C_GREY=""
fi

# ---------------------------------------------------------------------------
# Журналирование
# ---------------------------------------------------------------------------

_log_raw() {
  local level="$1"; shift
  local stamp; stamp="$(date '+%Y-%m-%d %H:%M:%S')"
  if [[ -n "${ASRHUB_LOG_FILE}" ]]; then
    printf '%s [%s] %s\n' "${stamp}" "${level}" "$*" >> "${ASRHUB_LOG_FILE}" 2>/dev/null || true
  fi
}

log()      { [[ "${ASRHUB_QUIET}" == "1" ]] || printf '%s\n' "$*"; _log_raw INFO "$*"; }
info()     { [[ "${ASRHUB_QUIET}" == "1" ]] || printf '%s—%s %s\n' "${C_CYAN}" "${C_RESET}" "$*"; _log_raw INFO "$*"; }
ok()       { [[ "${ASRHUB_QUIET}" == "1" ]] || printf '%s✓%s %s\n' "${C_GREEN}" "${C_RESET}" "$*"; _log_raw OK "$*"; }
warn()     { printf '%s!%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*" >&2; _log_raw WARN "$*"; }
error()    { printf '%s✕%s %s\n' "${C_RED}" "${C_RESET}" "$*" >&2; _log_raw ERROR "$*"; }
debug()    { [[ "${ASRHUB_DEBUG:-0}" == "1" ]] && printf '%s· %s%s\n' "${C_GREY}" "$*" "${C_RESET}" >&2; _log_raw DEBUG "$*"; return 0; }
hint()     { printf '  %s%s%s\n' "${C_DIM}" "$*" "${C_RESET}" >&2; _log_raw HINT "$*"; }

heading() {
  [[ "${ASRHUB_QUIET}" == "1" ]] && return 0
  printf '\n%s%s%s\n' "${C_BOLD}" "$*" "${C_RESET}"
  printf '%s%s%s\n' "${C_GREY}" "$(printf '─%.0s' $(seq 1 ${#1}))" "${C_RESET}"
  _log_raw STEP "$*"
}

step() {
  _STEP_INDEX=$((_STEP_INDEX + 1))
  _STEP_CURRENT="$*"
  [[ "${ASRHUB_QUIET}" == "1" ]] && return 0
  if [[ ${_STEP_TOTAL} -gt 0 ]]; then
    printf '\n%s[%d/%d]%s %s\n' "${C_BOLD}${C_BLUE}" "${_STEP_INDEX}" "${_STEP_TOTAL}" "${C_RESET}" "$*"
  else
    printf '\n%s▸%s %s\n' "${C_BOLD}${C_BLUE}" "${C_RESET}" "$*"
  fi
  _log_raw STEP "$*"
}

set_step_total() { _STEP_TOTAL="$1"; _STEP_INDEX=0; }

# ---------------------------------------------------------------------------
# Обработка ошибок
# ---------------------------------------------------------------------------

_stack_trace() {
  local frame=1
  printf '%sСтек вызовов:%s\n' "${C_GREY}" "${C_RESET}" >&2
  while caller "${frame}" >/dev/null 2>&1; do
    local line func file
    read -r line func file < <(caller "${frame}")
    printf '  %s%s:%s в %s()%s\n' "${C_GREY}" "${file}" "${line}" "${func}" "${C_RESET}" >&2
    frame=$((frame + 1))
  done
}

on_error() {
  local exit_code=$?
  local line_no="${1:-?}"
  local command="${2:-?}"
  set +o errexit
  printf '\n'
  error "Сбой на шаге: ${_STEP_CURRENT:-неизвестный шаг}"
  # «Команда: return ${status}» — внутренняя строка библиотеки, и человеку она
  # не говорит ничего: настоящая причина уже названа выше разбором вывода.
  # Показываем команду только когда она осмысленна.
  case "${command}" in
    return*|exit*) : ;;
    *) error "Команда: ${command}" ;;
  esac
  error "Строка: ${line_no}, код возврата: ${exit_code}"
  if [[ "${ASRHUB_DEBUG:-0}" == "1" ]]; then _stack_trace; fi
  explain_exit_code "${exit_code}"
  run_rollback
  cleanup_temp
  printf '\n'
  hint "Полный журнал: ${ASRHUB_LOG_FILE:-журнал не велся}"
  hint "Диагностика окружения: bash scripts/doctor.sh"
  # Повторить ровно то же самое: после отката установка начинается с чистого
  # листа, и подбирать ключи заново не нужно.
  if [[ -n "${ASRHUB_INVOCATION:-}" ]]; then
    hint "Повторить после исправления: ${ASRHUB_INVOCATION}"
  fi
  exit "${exit_code}"
}

explain_exit_code() {
  case "$1" in
    1)   hint "Общая ошибка. Смотрите сообщение выше." ;;
    2)   hint "Неверные аргументы или отсутствует файл." ;;
    13)  hint "Отказано в доступе. Запустите с sudo или выберите другой каталог установки." ;;
    28)  hint "Закончилось место на диске. Освободите место и повторите." ;;
    100) hint "Ошибка менеджера пакетов. Проверьте доступ в интернет и права." ;;
    126) hint "Файл найден, но не исполняемый. Проверьте права: chmod +x" ;;
    127) hint "Команда не найдена. Установите недостающую программу." ;;
    130) hint "Прервано пользователем (Ctrl+C)." ;;
    134) hint "Программа завершилась аварийно (SIGABRT). Обычно это ошибка в самой программе." ;;
    137) hint "Процесс убит (нехватка памяти?). Проверьте свободную оперативную память." ;;
    139) hint "Обращение к чужой памяти (SIGSEGV). Чаще всего — несовместимая сборка пакета." ;;
    141) hint "Оборван канал (SIGPIPE): команда справа закрылась раньше времени." ;;
    143) hint "Процесс остановлен извне (SIGTERM)." ;;
  esac
}

on_interrupt() {
  set +o errexit
  printf '\n'
  warn "Прервано пользователем."
  run_rollback
  cleanup_temp
  exit 130
}

enable_error_handling() {
  trap 'on_error "${LINENO}" "${BASH_COMMAND}"' ERR
  trap 'on_interrupt' INT TERM
  trap 'cleanup_temp' EXIT
}

# ---------------------------------------------------------------------------
# Откат и очистка
# ---------------------------------------------------------------------------

add_rollback() { _ROLLBACK_ACTIONS+=("$1"); debug "откат зарегистрирован: $1"; }

run_rollback() {
  [[ ${#_ROLLBACK_ACTIONS[@]} -eq 0 ]] && return 0
  warn "Откат изменений (${#_ROLLBACK_ACTIONS[@]} действ.)…"
  local i
  for (( i=${#_ROLLBACK_ACTIONS[@]}-1; i>=0; i-- )); do
    debug "откат: ${_ROLLBACK_ACTIONS[i]}"
    eval "${_ROLLBACK_ACTIONS[i]}" >/dev/null 2>&1 || warn "  не удалось выполнить: ${_ROLLBACK_ACTIONS[i]}"
  done
  _ROLLBACK_ACTIONS=()
  ok "Откат завершён — система возвращена в исходное состояние."
}

clear_rollback() { _ROLLBACK_ACTIONS=(); }

register_temp() { _CLEANUP_PATHS+=("$1"); }

cleanup_temp() {
  local path
  for path in "${_CLEANUP_PATHS[@]:-}"; do
    [[ -n "${path}" && -e "${path}" ]] && rm -rf "${path}" 2>/dev/null || true
  done
  _CLEANUP_PATHS=()
}

make_temp_dir() {
  local dir
  dir="$(mktemp -d "${TMPDIR:-/tmp}/asrhub.XXXXXXXX")"
  register_temp "${dir}"
  printf '%s' "${dir}"
}

# ---------------------------------------------------------------------------
# Выполнение команд
# ---------------------------------------------------------------------------

run() {
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '%s[пробный запуск]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"
    return 0
  fi
  debug "выполняется: $*"
  _log_raw CMD "$*"
  "$@"
}

run_quiet() {
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '%s[пробный запуск]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"
    return 0
  fi
  local output status=0
  debug "выполняется тихо: $*"
  output="$("$@" 2>&1)" || status=$?
  if [[ ${status} -ne 0 ]]; then
    error "Команда завершилась с кодом ${status}: $*"
    printf '%s\n' "${output}" | tail -25 >&2
    _log_raw CMDFAIL "$* -> ${status}: ${output}"
    return ${status}
  fi
  _log_raw CMDOK "$*"
  printf '%s' "${output}"
}

# Повтор с нарастающей задержкой: сеть и зеркала пакетов бывают нестабильны.
# ---------------------------------------------------------------------------
# Замок на каталог установки
# ---------------------------------------------------------------------------
#
# Два установщика в одном каталоге ломают друг другу окружение: второй сносит
# venv, пока первый в него ставит пакеты. Заканчивается это установкой, где
# половина движков есть, а половины нет, и найти причину по журналу нельзя —
# в нём всё успешно.

acquire_install_lock() {
  local dir="$1" lock owner=""
  [[ "${ASRHUB_DRY_RUN}" == "1" ]] && return 0
  lock="${dir}/.asrhub-install.lock"
  mkdir -p "${dir}" 2>/dev/null || true
  # noclobber + > — атомарное создание: проверка и создание одной операцией,
  # иначе два процесса успевают проскочить между ними.
  if ( set -o noclobber; printf '%s\n' "$$" > "${lock}" ) 2>/dev/null; then
    register_temp "${lock}"
    return 0
  fi
  owner="$(cat "${lock}" 2>/dev/null || true)"
  if [[ "${owner}" =~ ^[0-9]+$ ]] && kill -0 "${owner}" 2>/dev/null; then
    error "В каталоге ${dir} уже идёт установка (процесс ${owner})."
    hint "Дождитесь её окончания или остановите: kill ${owner}"
    hint "Если процесса давно нет, удалите файл: rm -f ${lock}"
    return 1
  fi
  if [[ -e "${lock}" ]]; then
    # Замок от прерванной установки: процесса с таким номером уже нет.
    warn "Найден замок прерванной установки — снимаем."
    rm -f "${lock}" 2>/dev/null || true
    if ( set -o noclobber; printf '%s\n' "$$" > "${lock}" ) 2>/dev/null; then
      register_temp "${lock}"
      return 0
    fi
  fi
  # Не смогли создать вовсе — каталог недоступен на запись. Это выяснится и
  # без нас на первом же шаге, поэтому не мешаем, но говорим вслух.
  warn "Не удалось поставить замок в ${dir} — установка продолжается без него."
  return 0
}

# ---------------------------------------------------------------------------
# Разбор ошибок pip
# ---------------------------------------------------------------------------
#
# Установка пакетов — единственное место, где чужая программа печатает сто
# строк и уходит с кодом 1. Пользователь видел «установка не удалась» и
# должен был сам искать причину в этой стене текста. Здесь причина называется
# одной строкой и вместе с командой, которая её лечит.

# Выполняет pip, показывая вывод и одновременно складывая его в файл: без
# сохранения причина остаётся только на экране, и скрипт о ней ничего не знает.
#
#   run_pip ФАЙЛ_ДЛЯ_ВЫВОДА КОМАНДА…
run_pip() {
  local capture="$1"; shift
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '%s[пробный запуск]%s %s\n' "${C_YELLOW}" "${C_RESET}" "$*"
    return 0
  fi
  debug "выполняется: $*"
  _log_raw CMD "$*"
  local had_errexit=0
  [[ $- == *e* ]] && had_errexit=1
  set +o errexit
  "$@" 2>&1 | tee -a "${capture}"
  local status=${PIPESTATUS[0]}
  if [[ ${had_errexit} -eq 1 ]]; then set -o errexit; fi
  [[ ${status} -ne 0 ]] && _log_raw CMDFAIL "$* -> ${status}"
  return "${status}"
}

# Ставит диагноз по сохранённому выводу pip.
#
#   diagnose_pip_failure ФАЙЛ [ПУТЬ_К_PYTHON]
#
# Возвращает 0, если причина опознана и напечатана, 1 — если нет.
# Порядок проверок от частного к общему: «нет колеса под этот Python»
# выглядит как обычный конфликт версий, и общее правило перехватило бы его.
diagnose_pip_failure() {
  local file="$1" python="${2:-}" offline="${3:-0}" text pyver=""
  [[ -f "${file}" ]] || return 1
  text="$(cat "${file}" 2>/dev/null)" || return 1
  [[ -n "${text}" ]] || return 1

  if [[ -n "${python}" && -x "${python}" ]]; then
    pyver="$("${python}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
  fi
  local pyname="Python${pyver:+ ${pyver}}"

  # Прибитая гвоздями зависимость, которой нет под этот Python. Ровно так
  # не ставился GigaAM: onnxruntime==1.23.* без колёс под 3.14.
  if [[ "${text}" == *ResolutionImpossible* \
     && "${text}" == *"no matching distributions available"* ]]; then
    local culprit
    culprit="$(printf '%s' "${text}" | sed -n 's/.*depends on \([A-Za-z0-9._-]*\)==.*/\1/p' | head -1)"
    error "Зависимости пакета прибиты к версии, которой нет под ${pyname}."
    [[ -n "${culprit}" ]] && hint "Не нашлось: ${culprit}"
    hint "Это не ошибка установки — под эту версию Python колёс ещё не выпустили."
    hint "Соберите окружение на проверенной версии:"
    hint "  sudo bash scripts/install.sh --python /usr/bin/python${ASRHUB_MAX_PYTHON} --force"
    hint "Либо перечислите зависимости вручную в requirements/engines/<движок>.txt,"
    hint "а сам пакет — в requirements/engines/no-deps/<движок>.txt."
    return 0
  fi

  if [[ "${text}" == *"conflicting dependencies"* || "${text}" == *ResolutionImpossible* ]]; then
    error "Пакеты требуют несовместимых версий одной библиотеки."
    printf '%s' "${text}" | sed -n '/The conflict is caused by/,/^$/p' | head -8 >&2
    hint "Чаще всего лечится установкой движка в отдельное окружение."
    return 0
  fi

  # Сеть. Каждая причина лечится по-разному, поэтому и разделены.
  if [[ "${text}" == *"Temporary failure in name resolution"* \
     || "${text}" == *"Could not resolve host"* \
     || "${text}" == *"Name or service not known"* \
     || "${text}" == *"nodename nor servname provided"* ]]; then
    error "Не разрешается имя сервера пакетов — не работает DNS."
    hint "Проверьте: getent hosts pypi.org"
    return 0
  fi
  if [[ "${text}" == *"Tunnel connection failed"* || "${text}" == *ProxyError* \
     || "${text}" == *"407 Proxy"* ]]; then
    error "Прокси не пропускает запросы к серверу пакетов."
    hint "Задайте его явно и повторите:"
    hint "  export https_proxy=http://адрес:порт; export http_proxy=\$https_proxy"
    return 0
  fi
  if [[ "${text}" == *CERTIFICATE_VERIFY_FAILED* || "${text}" == *SSLError* \
     || "${text}" == *SSLCertVerificationError* ]]; then
    error "Не проверяется сертификат сервера пакетов."
    hint "Обычно так ведёт себя корпоративный шлюз, подменяющий TLS."
    hint "Добавьте его корневой сертификат в систему или укажите:"
    hint "  ${VPIP:-pip} config set global.cert /путь/к/корневому.pem"
    return 0
  fi
  if [[ "${text}" == *"Read timed out"* || "${text}" == *ReadTimeoutError* \
     || "${text}" == *"Connection refused"* || "${text}" == *"Network is unreachable"* \
     || "${text}" == *"Connection reset"* || "${text}" == *"Failed to establish a new connection"* ]]; then
    error "Сервер пакетов недоступен — обрывается соединение."
    hint "Проверьте сеть и повторите: часть загрузок весит сотни мегабайт."
    return 0
  fi

  # Автономный режим отличаем до общего разбора: без индекса pip говорит ровно
  # то же самое — «нет подходящей версии», — и совет про версию Python увёл бы
  # в сторону от настоящей причины, пустого кеша.
  if [[ "${offline}" == "1" && ( "${text}" == *"No matching distribution found"* \
     || "${text}" == *"Could not find a version that satisfies"* ) ]]; then
    error "Автономный режим: нужного пакета нет в кеше pip."
    hint "Кеш заполняется при установке с доступом в интернет."
    hint "Либо снимите автономный режим, либо подготовьте кеш заранее:"
    hint "  pip download -r requirements/base.txt -d /путь/к/кешу"
    return 0
  fi

  if [[ "${text}" == *"No matching distribution found"* \
     || "${text}" == *"Could not find a version that satisfies"* ]]; then
    local missing
    missing="$(printf '%s' "${text}" | sed -n 's/.*No matching distribution found for \(.*\)/\1/p' | head -1)"
    error "Для ${pyname} нет подходящей версии${missing:+: ${missing}}."
    if [[ "${text}" == *"(from versions:"* ]]; then
      hint "Доступные версии перечислены в выводе выше — нужной среди них нет."
    fi
    hint "Проверьте версию Python и архитектуру: некоторые пакеты выходят с задержкой."
    return 0
  fi

  if [[ "${text}" == *"No space left on device"* || "${text}" == *"Errno 28"* ]]; then
    error "Закончилось место на диске."
    hint "Кеш pip нередко занимает гигабайты: ${VPIP:-pip} cache purge"
    return 0
  fi

  if [[ "${text}" == *MemoryError* || "${text}" == *"Killed"* \
     || "${text}" == *"virtual memory exhausted"* ]]; then
    error "Не хватило оперативной памяти при сборке пакета."
    hint "Соберите пакеты по одному или добавьте файл подкачки."
    return 0
  fi

  if [[ "${text}" == *"Could not build wheels"* || "${text}" == *"Failed building wheel"* \
     || "${text}" == *"error: command '"* || "${text}" == *"gcc: fatal error"* \
     || "${text}" == *"Python.h: No such file"* ]]; then
    local package
    package="$(printf '%s' "${text}" | sed -n 's/.*Failed building wheel for \(.*\)/\1/p' | head -1)"
    error "Готового пакета нет, а собрать из исходников нечем${package:+ (${package})}."
    hint "Нужны компилятор и заголовки Python:"
    hint "  sudo apt install build-essential python${pyver:-3}-dev"
    return 0
  fi

  if [[ "${text}" == *externally-managed-environment* ]]; then
    error "Установка идёт в системный Python, а он защищён от изменений."
    hint "Так и задумано: пакеты ставятся только в виртуальное окружение."
    return 0
  fi

  if [[ "${text}" == *"Permission denied"* || "${text}" == *"Errno 13"* ]]; then
    error "Отказано в доступе при записи пакетов."
    hint "Запустите установку с sudo или выберите каталог, доступный на запись."
    return 0
  fi

  if [[ "${text}" == *"THESE PACKAGES DO NOT MATCH THE HASHES"* \
     || "${text}" == *"HASH mismatch"* ]]; then
    error "Загруженный пакет не совпадает с контрольной суммой."
    hint "Обычно это испорченный кеш: ${VPIP:-pip} cache purge"
    return 0
  fi

  if [[ "${text}" == *"is not a supported wheel on this platform"* ]]; then
    error "Пакет собран под другую архитектуру."
    hint "Проверьте разрядность и платформу: $(uname -m 2>/dev/null || echo '?')"
    return 0
  fi

  if [[ "${text}" == *"Invalid requirement"* ]]; then
    error "Файл требований прочитать не удалось — ошибка в его синтаксисе."
    hint "Это ошибка в самом ASR Hub: сообщите строку из вывода выше."
    return 0
  fi

  return 1
}

# Установка требований движка.
#
#   install_engine_requirements ПУТЬ_К_PIP ФАЙЛ_ТРЕБОВАНИЙ [ключи pip…]
#
# Рядом с обычным файлом может лежать engines/no-deps/<движок>.txt —
# перечисленное в нём ставится с --no-deps, а его зависимости берутся из
# обычного файла. Это нужно пакетам, которые прибивают версии гвоздями:
# GigaAM требует onnxruntime==1.23.*, а колёс под Python 3.14 у этой версии
# нет — pip обрывался с ResolutionImpossible, и движок не ставился вовсе,
# хотя onnxruntime нужен ему только для экспорта в ONNX.
#
# Подкаталог, а не сосед по имени: engines/*.txt перебирается циклом в
# update.sh, и файл-спутник попал бы в него как отдельный «движок».
install_engine_requirements() {
  local pip="$1" req="$2"; shift 2
  local nodeps
  nodeps="$(dirname "${req}")/no-deps/$(basename "${req}")"
  pip_install "${pip}" 2 "$@" -r "${req}" || return 1
  if [[ -f "${nodeps}" ]]; then
    pip_install "${pip}" 2 "$@" --no-deps -r "${nodeps}" || return 1
  fi
  return 0
}

# Установка пакетов: повторы при обрыве плюс разбор причины отказа.
#
#   pip_install ПУТЬ_К_PIP ЧИСЛО_ПОПЫТОК аргументы_pip…
#
# Повтор нужен для сетевых обрывов — загрузки идут сотнями мегабайт. Но
# повторять бессмысленно, когда колеса под эту версию Python просто нет:
# причина называется сразу после последней попытки, и пользователю не нужно
# искать её в выводе pip.
pip_install() {
  local pip="$1" attempts="$2"; shift 2
  local capture status=0 label="" arg offline=0
  # Ключи в подпись не берём: имя нужно человеку, а не для повторения строки.
  # Заодно замечаем автономный режим — от него зависит объяснение отказа.
  for arg in "$@"; do
    [[ "${arg}" == "--no-index" ]] && offline=1
    [[ "${arg}" == -* ]] && continue
    label="${label:+${label} }$(basename "${arg}")"
  done
  capture="$(mktemp "${TMPDIR:-/tmp}/asrhub-pip.XXXXXX")"
  register_temp "${capture}"
  # Присваивание перед вызовом функции в разных bash ведёт себя по-разному
  # (в 3.2 на macOS оно переживает вызов), поэтому ставим и снимаем вручную.
  ASRHUB_RETRY_LABEL="установка пакетов (${label:-без имени})"
  retry "${attempts}" run_pip "${capture}" "${pip}" install "$@" || status=$?
  unset ASRHUB_RETRY_LABEL
  if [[ ${status} -ne 0 ]]; then
    diagnose_pip_failure "${capture}" "$(dirname "${pip}")/python" "${offline}" || true
  fi
  rm -f "${capture}" 2>/dev/null || true
  return "${status}"
}

retry() {
  local attempts="${1}"; shift
  local delay=2 attempt=1 status=0
  while true; do
    status=0
    "$@" && return 0 || status=$?
    if [[ ${attempt} -ge ${attempts} ]]; then
      # Имя вместо всей команды: у pip она разрастается до временного файла и
      # десятка ключей, из которых пользователю не пригодится ни один.
      error "Не удалось выполнить после ${attempts} попыток: ${ASRHUB_RETRY_LABEL:-$*}"
      return ${status}
    fi
    warn "Попытка ${attempt} из ${attempts} не удалась (код ${status}), повтор через ${delay} с…"
    sleep "${delay}"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
}

confirm() {
  local prompt="${1}"
  local default="${2:-y}"
  [[ "${ASRHUB_ASSUME_YES}" == "1" ]] && return 0

  # Без терминала берём заданное умолчание, а не «да». Прежнее безусловное
  # согласие означало, что `ssh машина 'bash uninstall.sh'`, задача Ansible
  # или строка в cron выполняли полное удаление, ни о чём не спросив, —
  # притом что вопрос задан именно с умолчанием «нет».
  if [[ ! -t 0 ]]; then
    printf '%s? %s %s — нет терминала, взято умолчание: %s%s\n' \
      "${C_YELLOW}" "${C_RESET}" "${prompt}" "${default}" "${C_RESET}" >&2
    [[ "${default}" == "y" ]]
    return
  fi
  local suffix="[Y/n]"
  [[ "${default}" == "n" ]] && suffix="[y/N]"
  local answer
  read -r -p "$(printf '%s?%s %s %s ' "${C_YELLOW}" "${C_RESET}" "${prompt}" "${suffix}")" answer || answer=""
  answer="${answer:-${default}}"
  [[ "${answer}" =~ ^([yY]|[дД]|да|yes)$ ]]
}

# ---------------------------------------------------------------------------
# Проверки окружения
# ---------------------------------------------------------------------------

have() { command -v "$1" >/dev/null 2>&1; }

require_command() {
  local cmd="$1" hint_text="${2:-}"
  if ! have "${cmd}"; then
    error "Не найдена программа «${cmd}»."
    [[ -n "${hint_text}" ]] && hint "${hint_text}"
    return 127
  fi
  debug "найдено: ${cmd} -> $(command -v "${cmd}")"
}

version_gt() {
  # Строго больше: «3.14 новее 3.13», но «3.13» не новее самой себя.
  [[ "$1" != "$2" ]] && version_ge "$1" "$2"
}

version_ge() {
  # version_ge 3.11.2 3.10  -> истина
  printf '%s\n%s\n' "$2" "$1" | sort -V -C
}

check_python() {
  local candidate best="" best_version=""
  # Явно указанный интерпретатор имеет приоритет: он приходит из --python
  # или переменной ASRHUB_PYTHON и проверяется теми же правилами, что и найденный.
  if [[ -n "${ASRHUB_PYTHON:-}" ]]; then
    local forced_version
    if ! have "${ASRHUB_PYTHON}"; then
      error "Указанный интерпретатор не найден: ${ASRHUB_PYTHON}"
      return 1
    fi
    forced_version="$("${ASRHUB_PYTHON}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || {
      error "Не удалось запустить ${ASRHUB_PYTHON}."
      return 1
    }
    if ! version_ge "${forced_version}" "${ASRHUB_MIN_PYTHON}"; then
      error "Указан Python ${forced_version}, требуется ${ASRHUB_MIN_PYTHON} или новее."
      return 1
    fi
    if version_gt "${forced_version}" "${ASRHUB_MAX_PYTHON}"; then
      # Заданное явно — уважаем, но предупреждаем: человек мог не знать,
      # что колёс под эту версию ещё нет.
      warn "Python ${forced_version} новее проверенной версии ${ASRHUB_MAX_PYTHON}."
      hint "Часть движков может не установиться: под свежие версии Python"
      hint "колёса torch, onnxruntime и nemo выходят с задержкой в месяцы."
    fi
    printf '%s' "${ASRHUB_PYTHON}"
    debug "используется заданный интерпретатор ${ASRHUB_PYTHON} версии ${forced_version}"
    return 0
  fi
  # Два прохода. Сначала ищем интерпретатор в проверенном диапазоне, и только
  # если такого нет — берём слишком новый, о чём честно предупреждаем. Раньше
  # проход был один и брал первый подходящий «снизу»: на машине, где есть
  # только python3.14, установка шла на нём и разваливалась внутри pip.
  local too_new="" too_new_version=""
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    have "${candidate}" || continue
    local version
    version="$("${candidate}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
    version_ge "${version}" "${ASRHUB_MIN_PYTHON}" || continue
    if version_gt "${version}" "${ASRHUB_MAX_PYTHON}"; then
      [[ -z "${too_new}" ]] && { too_new="${candidate}"; too_new_version="${version}"; }
      continue
    fi
    best="${candidate}"; best_version="${version}"
    break
  done
  if [[ -z "${best}" && -n "${too_new}" ]]; then
    best="${too_new}"; best_version="${too_new_version}"
    warn "Найден только Python ${best_version} — он новее проверенной версии ${ASRHUB_MAX_PYTHON}."
    hint "Часть движков под него не соберётся: колёса torch, onnxruntime и"
    hint "nemo выходят с задержкой в месяцы. Например, GigaAM требует"
    hint "onnxruntime==1.23.*, а у него колёс новее cp313 нет."
    case "$(uname -s)" in
      Linux)  hint "Поставьте проверенную версию и повторите:";
              hint "  sudo apt install python${ASRHUB_MAX_PYTHON} python${ASRHUB_MAX_PYTHON}-venv python${ASRHUB_MAX_PYTHON}-dev";
              hint "  затем: bash scripts/install.sh --python /usr/bin/python${ASRHUB_MAX_PYTHON}" ;;
      Darwin) hint "Поставьте проверенную версию: brew install python@${ASRHUB_MAX_PYTHON}";
              hint "затем: bash scripts/install.sh --python \"\$(brew --prefix)/bin/python${ASRHUB_MAX_PYTHON}\"" ;;
    esac
  fi
  if [[ -z "${best}" ]]; then
    error "Не найден Python ${ASRHUB_MIN_PYTHON} или новее."
    case "$(uname -s)" in
      Linux)  hint "Debian/Ubuntu: sudo apt install python3.12 python3.12-venv python3-pip";
              hint "RHEL/Fedora:   sudo dnf install python3.12" ;;
      Darwin) hint "macOS: brew install python@3.12" ;;
    esac
    return 1
  fi
  printf '%s' "${best}"
  debug "выбран интерпретатор ${best} версии ${best_version}"
}

# Свободное место в гигабайтах у ближайшего существующего предка пути.
# Отдельной функцией, потому что спрашивают об этом в трёх местах, и каждое
# со своей причиной: каталог данных, каталог установки и /tmp, где pip
# распаковывает и собирает пакеты.
free_space_gb() {
  local probe="$1" avail_kb
  while [[ ! -d "${probe}" && "${probe}" != "/" && "${probe}" != "." ]]; do
    probe="$(dirname "${probe}")"
  done
  if ! have df; then printf '%s' ""; return 0; fi
  avail_kb="$(df -Pk "${probe}" 2>/dev/null | awk 'NR==2 {print $4}')" || avail_kb=0
  printf '%s' "$(( ${avail_kb:-0} / 1024 / 1024 ))"
}

check_disk_space() {
  local path="$1" needed_gb="$2"
  local avail_kb avail_gb probe="$1"
  # Каталог здесь не создаём. Раньше `mkdir -p` шёл прямо тут, в обход `run`,
  # и это стоило двух вещей: `--dry-run` оставлял на диске каталоги, о которых
  # тут же писал «изменений не вносилось», а каталог данных доставался
  # ensure_dir уже существующим — тот пропускал chmod, и вместо 0750 он
  # оставался 0755 вместе с config.yaml, куда сервер дописывает ключи
  # доступа. Свободное место меряем по ближайшему существующему предку.
  while [[ ! -d "${probe}" && "${probe}" != "/" && "${probe}" != "." ]]; do
    probe="$(dirname "${probe}")"
  done
  path="${probe}"
  if have df; then
    avail_kb="$(df -Pk "${path}" 2>/dev/null | awk 'NR==2 {print $4}')" || avail_kb=0
    avail_gb=$(( ${avail_kb:-0} / 1024 / 1024 ))
    if [[ ${avail_gb} -lt ${needed_gb} ]]; then
      error "На «${path}» свободно ${avail_gb} ГБ, требуется не менее ${needed_gb} ГБ."
      hint "Освободите место или укажите другой каталог: --prefix /другой/путь"
      return 28
    fi
    debug "свободно на ${path}: ${avail_gb} ГБ"
  fi
}

check_memory() {
  local needed_gb="$1" total_gb=0
  case "$(uname -s)" in
    Linux)
      if [[ -r /proc/meminfo ]]; then
        total_gb=$(( $(awk '/MemTotal/{print $2}' /proc/meminfo) / 1024 / 1024 ))
      fi ;;
    Darwin)
      total_gb=$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 / 1024 )) ;;
  esac
  if [[ ${total_gb} -gt 0 && ${total_gb} -lt ${needed_gb} ]]; then
    warn "Оперативной памяти ${total_gb} ГБ, рекомендуется не менее ${needed_gb} ГБ."
    hint "Установка продолжится, но крупные модели могут не поместиться."
  fi
}

# Кто ещё работает над этим каталогом данных.
#
# Сервер умеет работать в нескольких экземплярах над общей базой: задание
# захватывается неделимо, и каждый экземпляр подписывает взятое своим именем.
# Для скриптов это значит, что каталог данных может быть не только «наш»:
# удаление с --purge или подмена кода под работающими соседями — это потеря
# чужой работы. Спрашиваем саму базу: она знает, кто держит задания.
#
# Печатает имена посторонних экземпляров через запятую; пусто — все свои.
other_instances() {
  local data_dir="$1" db="$1/asrhub.db"
  [[ -f "${db}" ]] || return 0
  have python3 || return 0
  python3 - "${db}" <<'PYEOF' 2>/dev/null || true
import sqlite3, socket, sys, time
try:
    conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=2)
    rows = conn.execute(
        "SELECT DISTINCT instance_id, MAX(COALESCE(heartbeat_at, started_at, 0)) "
        "FROM jobs WHERE status='running' AND instance_id IS NOT NULL "
        "GROUP BY instance_id").fetchall()
except Exception:
    sys.exit(0)
host = socket.gethostname()
# Свежая отметка жизни — экземпляр действительно работает; пять минут это тот
# же порог, по которому сервер возвращает брошенные задания в очередь.
alive = [name for name, beat in rows
         if name and not name.startswith(f"{host}:") and time.time() - (beat or 0) < 300]
print(",".join(sorted(alive)))
PYEOF
}

check_port_free() {
  local port="$1" listening=""
  # Без подоболочки с отключённым pipefail этот конвейер врал. `grep -q`
  # выходит по первому совпадению, `ss`/`netstat` продолжают писать, получают
  # SIGPIPE, и код конвейера становится 141 — то есть «не нашли». На машине,
  # где сокетов больше, чем влезает в буфер трубы (а это любой сервер),
  # занятый порт объявлялся свободным: doctor.sh сообщал, что сервер не
  # запущен, а install.sh не предлагал выбрать другой порт.
  if have ss; then
    listening="$(set +o pipefail; ss -ltn 2>/dev/null | awk '{print $4}' \
                 | grep -cE "[:.]${port}\$" || true)"
  elif have lsof; then
    lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1 && return 1
    listening=0
  elif have netstat; then
    listening="$(set +o pipefail; netstat -an 2>/dev/null \
                 | grep -cE "[:.]${port}[[:space:]].*LISTEN" || true)"
  fi
  [[ "${listening:-0}" -gt 0 ]] && return 1
  return 0
}

find_free_port() {
  local port="$1" limit=$(( $1 + 50 ))
  while [[ ${port} -lt ${limit} ]]; do
    check_port_free "${port}" && { printf '%s' "${port}"; return 0; }
    port=$((port + 1))
  done
  error "Не найден свободный порт в диапазоне $1–${limit}."
  return 1
}

check_network() {
  local host="${1:-pypi.org}"
  if have curl; then
    curl -fsS --max-time 8 -o /dev/null "https://${host}" 2>/dev/null && return 0
  elif have wget; then
    wget -q --timeout=8 --spider "https://${host}" 2>/dev/null && return 0
  fi
  return 1
}

# ---------------------------------------------------------------------------
# Безопасные операции с файлами
# ---------------------------------------------------------------------------

backup_file() {
  local path="$1"
  [[ -e "${path}" ]] || return 0
  local backup="${path}.bak.$(date +%Y%m%d%H%M%S)"
  cp -a "${path}" "${backup}"
  add_rollback "mv -f '${backup}' '${path}'"
  debug "резервная копия: ${backup}"
  printf '%s' "${backup}"
}

write_file() {
  # write_file <путь> [права] <<'EOF' ... EOF — атомарная запись с копией.
  # Права выставляются до переноса на место: config.yaml с ключами доступа
  # не должен существовать даже мгновение доступным всем на чтение.
  local path="$1" mode="${2:-}"
  local dir; dir="$(dirname "${path}")"
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    cat > /dev/null
    printf '%s[пробный запуск]%s запись %s\n' "${C_YELLOW}" "${C_RESET}" "${path}"
    return 0
  fi
  mkdir -p "${dir}"
  [[ -e "${path}" ]] && backup_file "${path}" >/dev/null
  local tmp="${path}.tmp.$$"
  cat > "${tmp}"
  [[ -n "${mode}" ]] && chmod "${mode}" "${tmp}"
  mv -f "${tmp}" "${path}"
  debug "записан файл: ${path}"
}

ensure_dir() {
  local path="$1" mode="${2:-0755}"
  # Создание каталога — изменение на диске, значит подчиняется пробному
  # запуску наравне с командами. Раньше не подчинялось: `--dry-run` оставлял
  # после себя каталог программы и весь каталог данных с подкаталогами и тут
  # же писал «изменений не вносилось».
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    [[ -d "${path}" ]] || printf '%s[пробный запуск]%s mkdir -m %s %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${mode}" "${path}"
    return 0
  fi
  if [[ ! -d "${path}" ]]; then
    mkdir -p "${path}"
    add_rollback "rmdir '${path}' 2>/dev/null || true"
    debug "создан каталог: ${path}"
  fi
  # chmod и для существующего каталога: иначе права зависели от того, кто
  # создал его первым. Каталог данных так и оставался 0755 — а в нём лежит
  # config.yaml с ключами доступа, их группами и квотами.
  chmod "${mode}" "${path}" 2>/dev/null || true
}

download() {
  local url="$1" target="$2"
  info "Загрузка: $(basename "${target}")"
  if have curl; then
    retry 3 curl -fL --progress-bar --connect-timeout 15 --retry 2 -o "${target}.part" "${url}"
  elif have wget; then
    retry 3 wget -q --show-progress --timeout=15 -O "${target}.part" "${url}"
  else
    error "Нужен curl или wget для загрузки файлов."
    return 127
  fi
  mv -f "${target}.part" "${target}"
}

verify_checksum() {
  local file="$1" expected="$2"
  [[ -z "${expected}" ]] && return 0
  local actual=""
  if have sha256sum; then
    actual="$(sha256sum "${file}" | awk '{print $1}')"
  elif have shasum; then
    actual="$(shasum -a 256 "${file}" | awk '{print $1}')"
  else
    warn "Нет sha256sum и shasum — контрольная сумма не проверена."
    return 0
  fi
  if [[ "${actual}" != "${expected}" ]]; then
    error "Контрольная сумма не совпала для ${file}."
    hint "Ожидалось: ${expected}"
    hint "Получено:  ${actual}"
    return 1
  fi
  ok "Контрольная сумма верна."
}

# ---------------------------------------------------------------------------
# Прочее
# ---------------------------------------------------------------------------

is_root() { [[ "$(id -u)" -eq 0 ]]; }

as_root() {
  # Пробный запуск обязан быть пробным и от имени root. Раньше ветка для
  # root вызывала команду напрямую, минуя run, и `install.sh --dry-run`,
  # запущенный от root — а в контейнерах и в Ansible он запускается именно
  # так, — по-настоящему ставил системные пакеты.
  if is_root; then run "$@"; return; fi
  if have sudo; then run sudo "$@"; return; fi
  error "Нужны права суперпользователя, но sudo не найден."
  hint "Запустите скрипт от имени root или установите sudo."
  return 13
}

human_size() {
  local bytes="${1:-0}"
  awk -v b="${bytes}" 'BEGIN{
    split("Б КБ МБ ГБ ТБ", u, " "); i=1;
    while (b >= 1024 && i < 5) { b /= 1024; i++ }
    printf (i == 1 ? "%d %s" : "%.1f %s"), b, u[i]
  }'
}

setup_logging() {
  local dir="${1:-/tmp}"
  mkdir -p "${dir}" 2>/dev/null || dir="/tmp"
  ASRHUB_LOG_FILE="${dir}/asrhub-$(basename "${0%.sh}")-$(date +%Y%m%d-%H%M%S).log"
  : > "${ASRHUB_LOG_FILE}" 2>/dev/null || ASRHUB_LOG_FILE=""
  [[ -n "${ASRHUB_LOG_FILE}" ]] && debug "журнал: ${ASRHUB_LOG_FILE}"
  export ASRHUB_LOG_FILE
}

print_banner() {
  [[ "${ASRHUB_QUIET}" == "1" ]] && return 0
  printf '%s' "${C_BOLD}${C_BLUE}"
  cat <<'BANNER'
   _   ___ ___   _  _      _
  /_\ / __| _ \ | || |_  _| |__
 / _ \\__ \   / | __ | || | '_ \
/_/ \_\___/_|_\ |_||_|\_,_|_.__/
BANNER
  printf '%s' "${C_RESET}"
  printf '%sСервер распознавания речи · версия %s%s\n\n' "${C_GREY}" "${ASRHUB_VERSION}" "${C_RESET}"
}
