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

  # Пакет прямо объявил, что не работает на этой версии Python. Это видно по
  # строке «Ignored the following versions that require a different python
  # version» — и это совсем не то же самое, что «колёса ещё не собрали»:
  # ждать бесполезно, ограничение стоит в метаданных пакета.
  if [[ "${text}" == *"require a different python version"* \
     && ( "${text}" == *"No matching distribution found"* \
       || "${text}" == *"Could not find a version that satisfies"* ) ]]; then
    local wanted supported
    wanted="$(printf '%s' "${text}" \
      | sed -n 's/.*No matching distribution found for \(.*\)/\1/p' | head -1)"
    # Ограничения перечислены по возрастанию версий — берём последнее, оно
    # относится к самому свежему выпуску.
    supported="$(printf '%s' "${text}" \
      | grep -o 'Requires-Python [^;]*' | tail -1 | sed 's/Requires-Python //')"
    error "Пакет${wanted:+ «${wanted}»} не поддерживает ${pyname}."
    [[ -n "${supported}" ]] && hint "Последний выпуск требует Python ${supported}."
    hint "Это ограничение автора пакета, а не задержка сборки: ждать нечего."
    hint "Соберите окружение на проверенной версии:"
    hint "  sudo bash scripts/install.sh --python /usr/bin/python${ASRHUB_MAX_PYTHON} --force"
    hint "Либо оставьте этот движок неустановленным — на распознавание он не влияет."
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

  # Не найден заголовок чужой библиотеки. Стоит перед общим разбором сборки:
  # там же «Failed building wheel», и общее правило звало ставить компилятор,
  # который на самом деле есть и честно доложил, чего ему не хватает.
  local header=""
  header="$(set +o pipefail; grep -oE "fatal error: [^:]+\.h: No such file" <<<"${text}" \
            | head -1 | sed -e 's/fatal error: //' -e 's/: No such file//' || true)"
  if [[ -n "${header}" && "${header}" != "Python.h" ]]; then
    local package
    package="$(set +o pipefail; grep -oE "Failed building wheel for [^ ]+" <<<"${text}" \
               | head -1 | sed 's/.*for //' || true)"
    error "Не хватает заголовков чужой библиотеки: ${header}${package:+ (нужны пакету ${package})}."
    case "${header}" in
      fst/*)
        # pynini собирается только против OpenFst нужной версии, а в
        # дистрибутиве почти всегда лежит другая. Готовое колесо снимает
        # вопрос целиком: собирать нечего.
        hint "Это OpenFst, его требует pynini. Собирать не нужно — есть готовое колесо:"
        hint "  ${python:-pip} -m pip install 'pynini>=2.1.7'"
        hint "Версия из дистрибутива (libfst-dev) обычно не та, что нужна pynini." ;;
      sndfile.h)      hint "Поставьте: sudo apt install libsndfile1-dev" ;;
      ffi.h)          hint "Поставьте: sudo apt install libffi-dev" ;;
      openssl/*)      hint "Поставьте: sudo apt install libssl-dev" ;;
      zlib.h)         hint "Поставьте: sudo apt install zlib1g-dev" ;;
      lzma.h)         hint "Поставьте: sudo apt install liblzma-dev" ;;
      portaudio.h)    hint "Поставьте: sudo apt install portaudio19-dev" ;;
      *)
        hint "Нужен пакет разработки той библиотеки, что поставляет этот файл."
        hint "Найти его: apt-file search ${header}" ;;
    esac
    return 0
  fi

  if [[ "${text}" == *"Could not build wheels"* || "${text}" == *"Failed building wheel"* \
     || "${text}" == *"error: command '"* || "${text}" == *"gcc: fatal error"* \
     || "${text}" == *"Python.h: No such file"* ]]; then
    local package build_pkg="build-essential" dev_pkg="python${pyver:-3}-dev"
    package="$(printf '%s' "${text}" | sed -n 's/.*Failed building wheel for \(.*\)/\1/p' | head -1)"
    # Имена пакетов у каждого дистрибутива свои; detect.sh знает их, но
    # подключён не всегда — тогда остаются имена Debian.
    if declare -f system_package_names >/dev/null 2>&1; then
      build_pkg="$(system_package_names build)"
    fi

    # Компилятора нет и заголовков нет — это две разные установки, и звучали
    # они одинаково: человек ставил оба пакета, хотя не хватало одного.
    # Признак первой: сборка не смогла запустить сам компилятор.
    local no_compiler=0 no_headers=0
    if [[ "${text}" == *"No such file or directory: '"*"g++'"* \
       || "${text}" == *"No such file or directory: '"*"gcc'"* \
       || "${text}" == *"unable to execute"*"cc"* \
       || "${text}" == *"command 'gcc' failed: No such file"* \
       || "${text}" == *"command 'cc' failed: No such file"* \
       || "${text}" == *"command 'g++' failed: No such file"* ]]; then
      no_compiler=1
    fi
    [[ "${text}" == *"Python.h: No such file"* ]] && no_headers=1

    error "Готового пакета нет, а собрать из исходников нечем${package:+ (${package})}."
    if [[ ${no_compiler} -eq 1 && ${no_headers} -eq 0 ]]; then
      hint "Не хватает компилятора C++ — заголовки Python на месте:"
      hint "  sudo apt install ${build_pkg}"
    elif [[ ${no_headers} -eq 1 && ${no_compiler} -eq 0 ]]; then
      hint "Компилятор есть, не хватает заголовков Python:"
      hint "  sudo apt install ${dev_pkg}"
    else
      hint "Нужны компилятор и заголовки Python:"
      hint "  sudo apt install ${build_pkg} ${dev_pkg}"
    fi
    hint "Собирать приходится потому, что готового колеса у пакета нет${pyver:+ под Python ${pyver}}."
    hint "Если ставить компилятор не хочется — движок можно пропустить,"
    hint "остальные продолжат работать."
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
  local dir names nodeps optional opt_nodeps
  dir="$(dirname "${req}")"
  nodeps="${dir}/no-deps/$(basename "${req}")"
  optional="${dir}/optional/$(basename "${req}")"
  pip_install "${pip}" 2 "$@" -r "${req}" || return 1
  if [[ -f "${nodeps}" ]]; then
    pip_install "${pip}" 2 "$@" --no-deps -r "${nodeps}" || return 1
  fi
  if [[ -f "${optional}" ]]; then
    opt_nodeps="${dir}/optional/no-deps/$(basename "${req}")"
    # Необязательная часть: движок работает и без неё, просто беднее. Ронять
    # из-за такой части весь движок нельзя — человек остаётся без всего
    # сразу, хотя не хватает одной возможности, о которой он мог и не знать.
    # Так и вышло с postprocess: расстановка знаков препинания не ставилась
    # из-за нормализатора чисел, которому нужен компилятор C++.
    if pip_install "${pip}" 1 "$@" -r "${optional}" \
       && { [[ ! -f "${opt_nodeps}" ]] \
            || pip_install "${pip}" 1 "$@" --no-deps -r "${opt_nodeps}"; }; then
      :
    else
      names="$(set +o pipefail; grep -vE '^[[:space:]]*(#|$)' "${optional}" 2>/dev/null \
               | sed 's/[<>=!;[].*//' | tr -d '[:space:]' | paste -sd, - || true)"
      warn "Необязательная часть движка не установилась${names:+: ${names}}."
      hint "Движок будет работать без неё; что именно теряется — в описании движка."
      hint "Причина названа выше; поставить позже: ${pip} install -r ${optional}"
    fi
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
# Можно ли надеяться, что следующая попытка пройдёт удачнее.
#
#   pip_failure_is_permanent ФАЙЛ_С_ВЫВОДОМ
#
# 0 — причина не зависит от попытки (нет компилятора, конфликт версий, нет
# колеса под этот Python, кончилось место), 1 — могло не повезти со связью.
#
# Повтор сборки, упавшей из-за отсутствия компилятора, — это ещё раз скачать
# сотни мегабайт и ещё раз потратить минуты на тот же отказ. В журнале при
# этом два одинаковых полотна вместо одного, и найти в них причину вдвое
# труднее.
pip_failure_is_permanent() {
  local file="$1" text=""
  [[ -f "${file}" ]] || return 1
  text="$(cat "${file}" 2>/dev/null || true)"
  [[ -n "${text}" ]] || return 1
  if [[ "${text}" == *"ResolutionImpossible"* \
     || "${text}" == *"conflicting dependencies"* \
     || "${text}" == *"No matching distribution found"* \
     || "${text}" == *"Ignored the following versions that require a different python version"* \
     || "${text}" == *"requires a different Python"* \
     || "${text}" == *"Failed building wheel"* \
     || "${text}" == *"Could not build wheels"* \
     || "${text}" == *"Python.h: No such file"* \
     || "${text}" == *"externally-managed-environment"* \
     || "${text}" == *"No space left on device"* \
     || "${text}" == *"Permission denied"* \
     || "${text}" == *"is not a valid editable requirement"* \
     || "${text}" == *"Invalid requirement"* ]]; then
    return 0
  fi
  return 1
}

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
  # Свой цикл вместо retry: повторять стоит только то, что могло не выйти
  # случайно. Отказ из-за отсутствия компилятора или конфликта версий второй
  # раз даст ровно тот же отказ, но вдвое дольше и вдвое длиннее в журнале.
  local attempt=1 delay=2
  while true; do
    : > "${capture}"                    # разбираем последнюю попытку, не смесь
    status=0
    run_pip "${capture}" "${pip}" install "$@" || status=$?
    [[ ${status} -eq 0 ]] && break
    if pip_failure_is_permanent "${capture}"; then
      [[ ${attempt} -gt 1 ]] && error "Установка не удалась: установка пакетов (${label:-без имени})"
      break
    fi
    if [[ ${attempt} -ge ${attempts} ]]; then
      error "Не удалось выполнить после ${attempts} попыток: установка пакетов (${label:-без имени})"
      break
    fi
    warn "Попытка ${attempt} из ${attempts} не удалась (код ${status}), повтор через ${delay} с…"
    sleep "${delay}"
    delay=$((delay * 2))
    attempt=$((attempt + 1))
  done
  if [[ ${status} -ne 0 ]]; then
    diagnose_pip_failure "${capture}" "$(dirname "${pip}")/python" "${offline}" || true
    # Полный вывод кладём в журнал: скрипты обещают его строкой «Полный
    # вывод: …», а до сих пор обещание было пустым — вывод жил только на
    # экране и пропадал вместе с ним.
    if [[ -n "${ASRHUB_LOG_FILE:-}" ]]; then
      {
        printf '\n----- вывод pip -----\n'
        cat "${capture}"
        printf '----- конец вывода pip -----\n'
      } >> "${ASRHUB_LOG_FILE}" 2>/dev/null || true
    fi
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
# Живость сервера: проверка и разбор причин
# ---------------------------------------------------------------------------
#
# «Сервер не отвечает» — не диагноз, а вопрос. Дальше идёт всё, чем на него
# отвечают: кто слушает порт, жива ли служба, что в её журнале и запускается
# ли пакет тем python, которым его запускает служба. Без этого человек после
# неудачного обновления оставался с одной строкой на экране и шёл искать
# причину сам.

#: Заполняются http_probe.
HTTP_STATUS=""
HTTP_BODY=""

# Один HTTP-запрос, не привязанный к конкретной утилите.
#
#   http_probe АДРЕС [ТАЙМАУТ]
#
#   0 — ответ получен: код в HTTP_STATUS, тело в HTTP_BODY;
#   1 — соединения нет;
#   2 — проверять нечем (нет ни curl, ни python3, ни wget).
#
# Код ответа нужен целиком, а не «получилось/нет»: 503 от живого сервера и
# отказ в соединении — это две разные поломки с разным лечением, а `curl -f`
# сводит их к одному пустому «не удалось».
http_probe() {
  local url="$1" timeout="${2:-3}" out="" status=0
  HTTP_STATUS=""; HTTP_BODY=""
  if have curl; then
    out="$(curl -sS --max-time "${timeout}" -w $'\n%{http_code}' "${url}" 2>/dev/null)" || return 1
    HTTP_STATUS="${out##*$'\n'}"
    HTTP_BODY="${out%$'\n'*}"
    [[ "${HTTP_STATUS}" =~ ^[0-9]{3}$ ]] || return 1
    return 0
  fi
  local py=""
  for py in "${ASRHUB_PROBE_PYTHON:-}" python3 python; do
    [[ -n "${py}" ]] && have "${py}" && break
    py=""
  done
  if [[ -n "${py}" ]]; then
    out="$("${py}" - "${url}" "${timeout}" <<'PYEOF' 2>/dev/null
import sys, urllib.error, urllib.request
url, timeout = sys.argv[1], float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        code, body = response.status, response.read(4096)
except urllib.error.HTTPError as exc:          # ответ есть, просто неуспешный
    code, body = exc.code, exc.read(4096)
except Exception:                              # соединения нет вовсе
    sys.exit(1)
sys.stdout.write(f"{code}\n")
sys.stdout.write(body.decode("utf-8", "replace"))
PYEOF
    )" || return 1
    HTTP_STATUS="${out%%$'\n'*}"
    HTTP_BODY="${out#*$'\n'}"
    [[ "${HTTP_STATUS}" =~ ^[0-9]{3}$ ]] || return 1
    return 0
  fi
  if have wget; then
    # wget не показывает код ответа отдельно от тела, поэтому различаем
    # только «ответил» и «нет»: код 8 — это ответ сервера об ошибке.
    HTTP_BODY="$(wget -q -O - --tries=1 --timeout="${timeout}" "${url}" 2>/dev/null)" || status=$?
    if [[ ${status} -eq 0 ]]; then HTTP_STATUS="200"; return 0; fi
    if [[ ${status} -eq 8 ]]; then HTTP_STATUS="500"; return 0; fi
    return 1
  fi
  return 2
}

# Открыт ли TCP-порт. Последний рубеж: работает и там, где нет ни одной
# сетевой утилиты, — bash умеет открывать сокеты сам.
port_open() {
  local host="${1:-127.0.0.1}" port="$2"
  if have curl; then
    curl -sS --max-time 2 -o /dev/null "http://${host}:${port}/" 2>/dev/null && return 0
  fi
  ( exec 3<>"/dev/tcp/${host}/${port}" ) >/dev/null 2>&1 && return 0
  return 1
}

# Кто занял порт — строкой для человека. Пусто, если выяснить нечем.
port_owner() {
  local port="$1" line=""
  if have ss; then
    line="$(set +o pipefail; ss -ltnp 2>/dev/null | grep -E "[:.]${port}[[:space:]]" | head -3 || true)"
  elif have lsof; then
    line="$(set +o pipefail; lsof -iTCP:"${port}" -sTCP:LISTEN -n -P 2>/dev/null | sed -n '2,4p' || true)"
  elif have netstat; then
    line="$(set +o pipefail; netstat -anp 2>/dev/null | grep -E "[:.]${port}[[:space:]].*LISTEN" | head -3 || true)"
  fi
  printf '%s' "${line}"
}

# Порт, на котором сервер слушает на самом деле.
#
# Единственный надёжный источник — не файл, а сама работающая служба.
# Порт задаётся в трёх местах сразу (config.yaml, строка запуска в юните,
# docker/.env), и они расходятся: `--port` в юните перекрывает
# конфигурацию навсегда, поэтому смена порта в веб-интерфейсе меняет файл,
# но не то, что слушает сервер. Проверка, доверявшая файлу, стучалась не
# туда и объявляла работающий сервер мёртвым.
#
#   running_server_port [ИМЯ_СЛУЖБЫ]
running_server_port() {
  local name="${1:-asrhub}" pid="" exec_line="" port="" line=""
  if have systemctl; then
    pid="$(systemctl show -p MainPID --value "${name}.service" 2>/dev/null || true)"
    if [[ -z "${pid}" || "${pid}" == "0" ]]; then
      pid="$(systemctl --user show -p MainPID --value "${name}.service" 2>/dev/null || true)"
    fi
  fi
  if [[ -z "${pid}" || "${pid}" == "0" ]] && have pgrep; then
    pid="$(set +o pipefail; pgrep -f 'm asrhub' 2>/dev/null | head -1 || true)"
  fi

  # Что слушает процесс — самый прямой ответ, какой вообще есть.
  if [[ -n "${pid}" && "${pid}" != "0" ]] && have ss; then
    line="$(set +o pipefail; ss -ltnp 2>/dev/null | grep "pid=${pid}," | head -1 || true)"
    if [[ -n "${line}" ]]; then
      port="$(awk '{print $4}' <<<"${line}")"
      port="${port##*:}"
      if [[ "${port}" =~ ^[0-9]{1,5}$ ]]; then printf '%s' "${port}"; return 0; fi
    fi
  fi
  if [[ -n "${pid}" && "${pid}" != "0" && -r "/proc/${pid}/cmdline" ]]; then
    port="$(tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null \
            | grep -oE -- '--port[= ]+[0-9]+' | grep -oE '[0-9]+' | head -1 || true)"
    if [[ "${port}" =~ ^[0-9]{1,5}$ ]]; then printf '%s' "${port}"; return 0; fi
  fi
  # Служба может быть остановлена — тогда спрашиваем её строку запуска.
  if have systemctl; then
    exec_line="$(systemctl show -p ExecStart --value "${name}.service" 2>/dev/null || true)"
    [[ -z "${exec_line}" ]] && exec_line="$(systemctl --user show -p ExecStart --value "${name}.service" 2>/dev/null || true)"
    port="$(grep -oE -- '--port[= ]+[0-9]+' <<<"${exec_line}" | grep -oE '[0-9]+' | head -1 || true)"
    if [[ "${port}" =~ ^[0-9]{1,5}$ ]]; then printf '%s' "${port}"; return 0; fi
  fi
  return 1
}

# Порт для проверки: сперва то, что слушает служба, потом файлы.
#
#   server_port_hint КАТАЛОГ_ДАННЫХ КАТАЛОГ_ПРОГРАММЫ [РЕЖИМ] [ИМЯ_СЛУЖБЫ]
server_port_hint() {
  local data_dir="${1:-}" prefix="${2:-}" mode="${3:-native}" name="${4:-asrhub}" port=""
  if [[ "${mode}" != "docker" ]]; then
    port="$(running_server_port "${name}" || true)"
    if [[ "${port}" =~ ^[0-9]{1,5}$ ]]; then printf '%s' "${port}"; return 0; fi
  fi
  if [[ "${mode}" == "docker" && -n "${prefix}" ]]; then
    port="$(grep -E '^ASRHUB_PORT=' "${prefix}/docker/.env" 2>/dev/null | cut -d= -f2 | head -1 || true)"
  elif [[ -n "${data_dir}" ]]; then
    port="$(grep -E '^[[:space:]]*server_port:' "${data_dir}/config.yaml" 2>/dev/null \
            | awk '{print $2}' | head -1 || true)"
  fi
  port="$(printf '%s' "${port}" | tr -d "\"'\r" | awk '{print $1}')"
  if [[ "${port}" =~ ^[0-9]{1,5}$ ]] && (( port >= 1 && port <= 65535 )); then
    printf '%s' "${port}"; return 0
  fi
  printf '%s' "${ASRHUB_DEFAULT_PORT:-8080}"
  return 0
}

# Состояние службы одним словом: running, activating, failed, inactive,
# unknown. Возвращает 0, только когда служба действительно работает.
#
#   service_state [ИМЯ_СЛУЖБЫ]
service_state() {
  local name="${1:-asrhub}" state=""
  if [[ "$(uname -s 2>/dev/null)" == "Darwin" ]] && have launchctl; then
    if [[ "$(set +o pipefail; launchctl list 2>/dev/null \
             | grep -c com.asrhub.server || true)" -gt 0 ]]; then
      printf 'running'; return 0
    fi
    printf 'inactive'; return 1
  fi
  if have systemctl; then
    state="$(systemctl is-active "${name}.service" 2>/dev/null || true)"
    if [[ -z "${state}" || "${state}" == "unknown" || "${state}" == "inactive" ]]; then
      # Служба могла быть поставлена не в систему, а текущему пользователю.
      local user_state
      user_state="$(systemctl --user is-active "${name}.service" 2>/dev/null || true)"
      [[ -n "${user_state}" && "${user_state}" != "unknown" ]] && state="${user_state}"
    fi
    case "${state}" in
      active)      printf 'running';    return 0 ;;
      activating|reloading) printf 'activating'; return 1 ;;
      failed)      printf 'failed';     return 1 ;;
      inactive|deactivating) printf 'inactive'; return 1 ;;
      *)           printf 'unknown';    return 1 ;;
    esac
  fi
  if pgrep -f "m asrhub" >/dev/null 2>&1; then printf 'running'; return 0; fi
  printf 'unknown'; return 1
}

# Хвост журнала службы — тем способом, каким он вообще доступен на этой
# машине. Печатает в stdout; молчит, если журнала нет.
#
#   server_log_tail СТРОК КАТАЛОГ_ДАННЫХ [ИМЯ_СЛУЖБЫ]
server_log_tail() {
  local lines="${1:-30}" data_dir="${2:-}" name="${3:-asrhub}" text=""
  if have journalctl; then
    text="$(journalctl -u "${name}.service" -n "${lines}" --no-pager 2>/dev/null || true)"
    # journalctl на пустой журнал печатает «-- No entries --»: это не строки
    # журнала, а сообщение об их отсутствии, и показывать его как причину
    # падения — значит показывать пустоту с видом ответа.
    grep -qE '^-- No entries --' <<<"${text}" && text=""
    if [[ -z "${text}" ]]; then
      text="$(journalctl --user -u "${name}.service" -n "${lines}" --no-pager 2>/dev/null || true)"
      grep -qE '^-- No entries --' <<<"${text}" && text=""
    fi
  fi
  if [[ -z "${text}" && -n "${data_dir}" ]]; then
    local candidate
    for candidate in "${data_dir}/logs/service.log" "${data_dir}/logs/asrhub.log" \
                     "${data_dir}/logs/error.log"; do
      [[ -s "${candidate}" ]] || continue
      text="$(tail -n "${lines}" "${candidate}" 2>/dev/null || true)"
      [[ -n "${text}" ]] && break
    done
  fi
  printf '%s' "${text}"
}

# Пробный запуск пакета тем же python, которым его запускает служба.
# Возвращает 0, если пакет импортируется; иначе кладёт вывод в
# STARTUP_TRACEBACK и возвращает 1.
#
#   import_probe ПУТЬ_К_PYTHON [КАТАЛОГ_С_ИСХОДНИКАМИ]
STARTUP_TRACEBACK=""
import_probe() {
  local python="$1" src="${2:-}" out="" status=0
  STARTUP_TRACEBACK=""
  [[ -x "${python}" ]] || return 2
  # Импорт, а не запуск: сервер уже, возможно, пытается стартовать в фоне, и
  # второй настоящий запуск занял бы тот же порт и добавил путаницы.
  out="$(PYTHONPATH="${src}${src:+:}${PYTHONPATH:-}" "${python}" -c 'import asrhub.api.app' 2>&1)" || status=$?
  if [[ ${status} -ne 0 ]]; then
    STARTUP_TRACEBACK="${out}"
    return 1
  fi
  return 0
}

# Называет причину, по которой сервер не поднялся, по тексту его вывода.
# Возвращает 0, если причина опознана и напечатана, 1 — если нет.
#
#   diagnose_startup_failure ТЕКСТ [ПУТЬ_К_PYTHON] [КАТАЛОГ_ПРОГРАММЫ] [КАТАЛОГ_ДАННЫХ]
#
# Порядок — от частного к общему: несовместимая библиотека выглядит как
# обычная ошибка импорта, и общее правило перехватило бы её первым.
diagnose_startup_failure() {
  local text="$1" python="${2:-}" prefix="${3:-.}" data_dir="${4:-}" module=""
  [[ -n "${text}" ]] || return 1

  if grep -qiE "no space left on device" <<<"${text}"; then
    error "На диске кончилось место — сервер не может писать ни журнал, ни базу."
    hint "Освободите место и запустите: bash ${prefix}/scripts/service.sh restart"
    return 0
  fi
  if grep -qiE "address already in use|errno 98|only one usage of each socket" <<<"${text}"; then
    error "Порт уже занят другим процессом — сервер не смог его открыть."
    hint "Кто занял: ss -ltnp | grep :ПОРТ"
    hint "Либо остановите тот процесс, либо задайте другой порт в config.yaml."
    return 0
  fi
  if grep -qiE "permission denied|operation not permitted|errno 13" <<<"${text}"; then
    error "Не хватает прав на каталог данных или файлы программы."
    hint "Владелец каталога должен совпадать с пользователем службы."
    hint "Кто владелец: ls -ld ${data_dir:-КАТАЛОГ_ДАННЫХ}"
    hint "От кого служба: systemctl show -p User asrhub.service"
    return 0
  fi
  if grep -qiE "unable to open database|database is locked|database disk image is malformed|file is not a database" <<<"${text}"; then
    error "Сервер не смог открыть базу данных."
    hint "Проверьте права и свободное место: ls -l ${data_dir:-КАТАЛОГ_ДАННЫХ}/asrhub.db"
    hint "Проверить целостность: sqlite3 ${data_dir:-КАТАЛОГ_ДАННЫХ}/asrhub.db 'PRAGMA integrity_check;'"
    return 0
  fi
  if grep -qiE "prefer_fwd_module|_eval_type\(\)|typing\._eval_type|unexpected keyword argument.*(annotationlib|fwd)" <<<"${text}"; then
    error "Библиотека в окружении несовместима с этой версией Python."
    hint "Чаще всего это pydantic или fastapi: типизация в новых версиях Python"
    hint "меняется, и старая библиотека на ней просто не импортируется."
    hint "Обновить: ${python:-python3} -m pip install -U pydantic fastapi"
    local pyver=""
    [[ -n "${python}" ]] && pyver="$("${python}" -V 2>&1 | head -1)"
    if [[ -n "${pyver}" ]]; then
      hint "Версия Python: ${pyver}"
      # Предварительные сборки (rc, beta, alpha) — отдельный случай: под них
      # никто не собирает, и обновление библиотек здесь не поможет.
      if grep -qiE '(rc|a|b)[0-9]+$|\+?dev' <<<"${pyver}"; then
        hint "Это предварительная сборка Python — под неё библиотеки не выпускают."
        hint "Поставьте итоговый выпуск (например, python3.13) и пересоберите окружение:"
        hint "bash ${prefix}/scripts/install.sh --force --python /usr/bin/python3.13"
      fi
    fi
    return 0
  fi
  # Только настоящий сбой, а не всякое упоминание карты: в журнале
  # исправного сервера строка «Обнаружена видеокарта NVIDIA…» стоит первой,
  # и по слову «nvidia» разбор уверенно объявлял поломку драйвера там, где
  # всё работало.
  if grep -qiE "libcu[a-z0-9_.+-]*\.so[^ ]*: cannot open|CUDA error|CUDA driver version|no CUDA-capable device|libcuda\.so[^ ]* (not found|cannot open)|NVIDIA driver.*(too old|not loaded)" <<<"${text}"; then
    error "Не загружаются библиотеки видеокарты."
    hint "Проверьте драйвер: nvidia-smi"
    hint "Полная проверка окружения: bash ${prefix}/scripts/doctor.sh"
    return 0
  fi
  if grep -qiE "undefined symbol|GLIBC_|cannot open shared object|wrong ELF class|incompatible architecture" <<<"${text}"; then
    error "Двоичный модуль в окружении собран не под эту систему."
    hint "Пересоберите окружение поверх нынешнего: bash ${prefix}/scripts/install.sh --force"
    return 0
  fi
  module="$(set +o pipefail; grep -oiE "no module named '[^']+'" <<<"${text}" \
            | head -1 | sed "s/.*'\\(.*\\)'/\\1/" || true)"
  if [[ -n "${module}" ]]; then
    error "В окружении нет пакета «${module}» — сервер не смог запуститься."
    [[ -n "${python}" ]] && hint "Доставить: ${python} -m pip install -r ${prefix}/requirements/base.txt"
    hint "Или переустановить поверх нынешнего: bash ${prefix}/scripts/install.sh --force"
    return 0
  fi
  if grep -qiE "SyntaxError|IndentationError" <<<"${text}"; then
    error "Файлы программы повреждены или не соответствуют версии Python."
    hint "Откатитесь на прежнюю версию: bash ${prefix}/scripts/update.sh --rollback"
    return 0
  fi
  if grep -qiE "yaml|ScannerError|ParserError|Ошибка конфигурации" <<<"${text}"; then
    error "Сервер не смог прочитать config.yaml."
    hint "Проверьте отступы и кавычки в файле конфигурации."
    return 0
  fi
  return 1
}

# Виден ли в тексте успешный запуск сервера. Нужен, чтобы не искать причину
# падения в журнале, где сервер как раз поднялся: разбор на таком тексте
# ловится за случайные слова и уверенно называет несуществующую поломку.
server_started_ok() {
  grep -qE "Uvicorn running on|Application startup complete|ASR Hub запущен" <<<"${1:-}"
}

# Ждёт, пока сервер начнёт отвечать.
#
#   wait_for_health ПОРТ [СЕКУНД] [ИМЯ_СЛУЖБЫ] [АДРЕС]
#
#   0 — отвечает;
#   2 — отвечает, но сообщает о неисправности (тело в HTTP_BODY);
#   1 — не отвечает.
#
# Ожидание прерывается досрочно, когда служба уже упала: ждать сорок секунд
# от процесса, которого нет, — это только задержка перед той же ошибкой.
wait_for_health() {
  local port="$1" seconds="${2:-40}" name="${3:-asrhub}" host="${4:-127.0.0.1}"
  local deadline=$(( SECONDS + seconds )) state="" dead=0
  while (( SECONDS < deadline )); do
    if http_probe "http://${host}:${port}/api/health" 3; then
      [[ "${HTTP_STATUS}" == 2* ]] && return 0
      return 2
    fi
    state="$(service_state "${name}" || true)"
    if [[ "${state}" == "failed" ]]; then
      dead=$(( dead + 1 ))
      # Два раза подряд: systemd между перезапусками показывает failed и
      # сам же поднимает службу снова — на одном замере это неотличимо от
      # окончательного падения.
      if (( dead >= 2 )); then return 1; fi
    else
      dead=0
    fi
    sleep 2
  done
  return 1
}

# Печатает всё, что известно о том, почему сервер не отвечает.
#
#   diagnose_server_down ПОРТ КАТАЛОГ_ПРОГРАММЫ КАТАЛОГ_ДАННЫХ [РЕЖИМ] [ИМЯ] [АДРЕС]
#
# РЕЖИМ: native (по умолчанию) или docker.
diagnose_server_down() {
  local port="$1" prefix="$2" data_dir="$3" mode="${4:-native}" name="${5:-asrhub}"
  local host="${6:-127.0.0.1}"
  local state="" owner="" logs="" python="${2}/venv/bin/python"
  [[ -x "${python}" ]] || python="${prefix}/venv/bin/python3"

  printf '\n%sЧто известно%s\n' "${C_BOLD}" "${C_RESET}" >&2

  if [[ "${mode}" == "docker" ]]; then
    if have docker; then
      logs="$(cd "${prefix}/docker" 2>/dev/null && docker compose logs --tail 40 2>&1 || true)"
      [[ -z "${logs}" ]] && logs="$(docker logs --tail 40 asrhub 2>&1 || true)"
    fi
  else
    state="$(service_state "${name}" || true)"
    case "${state}" in
      running)    info "Служба запущена, но на запросы не отвечает." ;;
      activating) info "Служба ещё запускается — возможно, ей нужно больше времени." ;;
      failed)     error "Служба упала (systemd: failed)." ;;
      inactive)   error "Служба остановлена и не поднимается." ;;
      *)          info "Состояние службы определить не удалось." ;;
    esac
    logs="$(server_log_tail 40 "${data_dir}" "${name}")"
  fi

  if port_open "${host}" "${port}"; then
    info "Порт ${port} открыт, но /api/health не отвечает — сервер занят или отвечает ошибкой."
  else
    info "Порт ${port} на ${host} закрыт — сервера на нём нет."
    owner="$(port_owner "${port}")"
    if [[ -n "${owner}" ]]; then
      warn "Порт ${port} занят другой программой:"
      printf '%s\n' "${owner}" >&2
    fi
  fi

  # Самая частая причина «не отвечает» у работающего сервера: стучались не
  # в тот порт. Порт живёт сразу в трёх местах — в config.yaml, в строке
  # запуска юнита и в docker/.env, — и они расходятся молча.
  local real_port=""
  if [[ "${mode}" != "docker" ]]; then
    real_port="$(running_server_port "${name}" || true)"
  fi
  if [[ -n "${real_port}" && "${real_port}" != "${port}" ]]; then
    printf '\n' >&2
    error "Сервер слушает порт ${real_port}, а проверялся ${port}."
    hint "Проверьте сами: curl -i http://${host}:${real_port}/api/health"
    hint "Порт из строки запуска службы перекрывает config.yaml, поэтому смена"
    hint "порта в настройках меняет файл, но не то, что слушает сервер."
    hint "Привести к одному: bash ${prefix}/scripts/service.sh install --port ${real_port}"
    return 0
  fi

  if [[ -n "${logs}" ]]; then
    printf '\n%sПоследние строки журнала%s\n' "${C_BOLD}" "${C_RESET}" >&2
    printf '%s\n' "${logs}" | tail -30 | sed 's/^/  /' >&2
    _log_raw LOGTAIL "${logs}"
  else
    info "Журнал службы пуст или недоступен."
  fi

  # Пробный импорт тем же python: почти всегда причина именно здесь, и
  # только он показывает её текстом, а не кодом выхода.
  #
  # Журнал берём как текст сбоя, только если сервер в нём не поднялся: на
  # журнале успешного запуска разбор цепляется за случайные слова и
  # называет поломку, которой нет.
  local probe_text="${logs}"
  if server_started_ok "${logs}"; then
    info "В журнале сервер запустился без ошибок — причина не в запуске."
    probe_text=""
  fi
  if [[ "${mode}" != "docker" ]] && [[ -x "${python}" ]]; then
    if ! import_probe "${python}" "${prefix}/server"; then
      printf '\n%sПробный запуск пакета%s\n' "${C_BOLD}" "${C_RESET}" >&2
      printf '%s\n' "${STARTUP_TRACEBACK}" | tail -20 | sed 's/^/  /' >&2
      probe_text="${STARTUP_TRACEBACK}"
      _log_raw IMPORTFAIL "${STARTUP_TRACEBACK}"
    else
      info "Пакет asrhub импортируется — дело не в зависимостях."
    fi
  fi

  printf '\n' >&2
  diagnose_startup_failure "${probe_text}" "${python}" "${prefix}" "${data_dir}" || {
    error "Причину назвать не удалось."
    hint "Полный журнал: bash ${prefix}/scripts/service.sh logs -n 200"
    hint "Запуск вручную (покажет ошибку целиком): ${python} -m asrhub --port ${port}"
    hint "Состояние службы: bash ${prefix}/scripts/service.sh status"
    hint "Проверка окружения: bash ${prefix}/scripts/doctor.sh"
  }
  return 0
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
  # Уже заданный журнал не подменяем: install.sh запускает service.sh и
  # models.sh отдельными процессами, и записи всех троих должны лечь в один
  # файл — иначе искать причину придётся в трёх.
  if [[ -n "${ASRHUB_LOG_FILE:-}" ]]; then
    debug "журнал уже задан: ${ASRHUB_LOG_FILE}"
    return 0
  fi
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
