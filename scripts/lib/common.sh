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
  error "Команда: ${command}"
  error "Строка: ${line_no}, код возврата: ${exit_code}"
  [[ "${ASRHUB_DEBUG:-0}" == "1" ]] && _stack_trace
  explain_exit_code "${exit_code}"
  run_rollback
  cleanup_temp
  printf '\n'
  hint "Полный журнал: ${ASRHUB_LOG_FILE:-журнал не велся}"
  hint "Диагностика окружения: bash scripts/doctor.sh"
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
    137) hint "Процесс убит (нехватка памяти?). Проверьте свободную оперативную память." ;;
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
retry() {
  local attempts="${1}"; shift
  local delay=2 attempt=1 status=0
  while true; do
    status=0
    "$@" && return 0 || status=$?
    if [[ ${attempt} -ge ${attempts} ]]; then
      error "Не удалось выполнить после ${attempts} попыток: $*"
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
    printf '%s' "${ASRHUB_PYTHON}"
    debug "используется заданный интерпретатор ${ASRHUB_PYTHON} версии ${forced_version}"
    return 0
  fi
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    have "${candidate}" || continue
    local version
    version="$("${candidate}" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
    if version_ge "${version}" "${ASRHUB_MIN_PYTHON}"; then
      best="${candidate}"; best_version="${version}"
      break
    fi
  done
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
