#!/usr/bin/env bash
#
# Установка агента ASR Hub на станцию Asterisk.
#
# Скрипт отдаёт сам сервер распознавания, уже подставив в него свой адрес,
# поэтому установка на станции — это одна строка:
#
#   curl -fsSL 'http://сервер:8081/api/telephony/agent/install.sh' \
#       -H 'X-API-Key: ah_…' | sudo bash -s -- \
#       --url http://сервер:8081 --key ah_… --name pbx-msk
#
# Что делает:
#   * проверяет, что на станции есть всё нужное (root, python 3.6+, curl или
#     wget, доступный сервер, права на /etc и /var/lib);
#   * кладёт агента в /opt/asrhub-agent, скачивая его с того же сервера;
#   * пишет /etc/asrhub-agent.conf с правами 600 (ключ доступа — не то, что
#     должно лежать читаемым для всех на чужой станции);
#   * заводит службу systemd asrhub-agent.service, но включает её ТОЛЬКО
#     после успешной самопроверки: служба, которая перезапускается раз в
#     десять секунд из-за неверного пути к журналу, не лечит ничего, зато
#     засоряет journal и выглядит работающей;
#   * печатает, чем смотреть журнал, чем останавливать и чем удалять.
#
# Удаление: sudo bash install.sh --uninstall [--yes]
#
# Любая неудача объясняет, что делать. Скрипт идемпотентен: повторный
# запуск обновляет агента и настройки, не ломая уже собранное состояние.

set -euo pipefail
set -o errtrace

# Адрес сервера и версия подставляются при выдаче файла сервером
# (см. server/asrhub/api/routes_agent.py). Если файл взяли прямо из
# исходников, подстановки не было — тогда адрес спрашиваем ключом --url.
SERVER_URL_DEFAULT="@@SERVER_URL@@"
AGENT_VERSION="@@VERSION@@"

INSTALL_DIR="/opt/asrhub-agent"
AGENT_FILE="${INSTALL_DIR}/asrhub-agent.py"
CONFIG_FILE="/etc/asrhub-agent.conf"
STATE_DIR="/var/lib/asrhub-agent"
LOG_FILE="/var/log/asrhub-agent.log"
SERVICE_NAME="asrhub-agent"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

URL=""
KEY=""
AGENT_NAME=""
SOURCE=""
CDR_FILE=""
RECORDINGS=""
INTERVAL=""
CREATE_SERVICE=1
FORCE=0
UNINSTALL=0
ASSUME_YES=0
PYTHON_BIN=""
SERVICE_USER="root"
SERVICE_GROUP="root"
TMP_DIR=""

# ---------------------------------------------------------------------------
# Вывод
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'
  C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi

info() { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s\n' "${C_DIM}" "${C_OFF}" "$*"; }
ok()   { printf '%s  ок%s  %s\n' "${C_OK}" "${C_OFF}" "$*"; }
warn() { printf '%s  ! %s  %s\n' "${C_WARN}" "${C_OFF}" "$*" >&2; }

# Ошибка — это всегда «что случилось» плюс «что делать». Сообщение без
# второй половины заставляет человека идти в поиск, а он на станции за
# NAT обычно недоступен.
die() {
  printf '\n%sОшибка:%s %s\n' "${C_ERR}" "${C_OFF}" "$1" >&2
  if [ -n "${2:-}" ]; then
    printf '%sЧто делать:%s %s\n' "${C_DIM}" "${C_OFF}" "$2" >&2
  fi
  exit 1
}

# Ловушка на всё, что упало само: без неё `set -e` завершает установку
# молча, и человек видит только пустой вывод и код возврата.
on_error() {
  local code=$? line=${1:-?}
  printf '\n%sУстановка прервана%s на строке %s (код %s).\n' \
    "${C_ERR}" "${C_OFF}" "${line}" "${code}" >&2
  printf 'Уже сделанное не удалено. Повторный запуск установщика безопасен: \n' >&2
  printf 'он обновляет файлы на месте. Полностью убрать агента: \n' >&2
  printf '  sudo bash install.sh --uninstall\n' >&2
  cleanup
  exit "${code}"
}
trap 'on_error ${LINENO}' ERR

cleanup() {
  if [ -n "${TMP_DIR}" ] && [ -d "${TMP_DIR}" ]; then
    rm -rf "${TMP_DIR}" || true
  fi
}
trap cleanup EXIT

have() { command -v "$1" >/dev/null 2>&1; }

# Ключ доступа в журнал установки не пишем даже в подтверждении: вывод
# установщика часто копируют в переписку с подрядчиком целиком.
mask_key() {
  local key="${1:-}"
  if [ -z "${key}" ]; then printf '(не задан)'; return; fi
  if [ "${#key}" -le 8 ]; then printf '…'; return; fi
  printf '%s…' "${key:0:7}"
}

usage() {
  cat <<'СПРАВКА'
Установка агента ASR Hub на станцию Asterisk.

Использование:
  sudo bash install.sh --url http://сервер:8081 --key ah_… [параметры]
  sudo bash install.sh --uninstall [--yes]

Параметры:
  --url АДРЕС         адрес сервера ASR Hub (обязателен, если сервер не
                      подставил свой при выдаче файла)
  --key КЛЮЧ          ключ доступа с правом записи (начинается с ah_)
  --name ИМЯ          как станция будет называться в разделе «АТС»
                      (по умолчанию — имя хоста)
  --source ВИД        auto | cdr_csv | mysql | folder (по умолчанию auto)
  --cdr-file ПУТЬ     путь к Master.csv, если он лежит не на обычном месте
  --recordings ПУТЬ   каталог записей (по умолчанию
                      /var/spool/asterisk/monitor)
  --interval СЕКУНД   пауза между заходами службы (по умолчанию 60)
  --no-service        не заводить службу systemd, только поставить файлы
  --force             перезаписать существующие настройки целиком и
                      поставить службу даже при неуспешной самопроверке
  --uninstall         остановить и удалить службу, файлы и настройки
  --yes               не спрашивать подтверждения (нужен для --uninstall,
                      когда установщик запущен через «curl | bash»)
  -h, --help          эта справка
СПРАВКА
}

# ---------------------------------------------------------------------------
# Разбор ключей
# ---------------------------------------------------------------------------

need_value() {
  # $1 — имя ключа, $2 — сколько аргументов осталось
  if [ "$2" -lt 2 ]; then
    die "Ключу ${1} нужно значение." "Например: ${1} значение"
  fi
}

parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --url)         need_value "$1" $#; URL="$2"; shift 2 ;;
      --key)         need_value "$1" $#; KEY="$2"; shift 2 ;;
      --name)        need_value "$1" $#; AGENT_NAME="$2"; shift 2 ;;
      --source)      need_value "$1" $#; SOURCE="$2"; shift 2 ;;
      --cdr-file)    need_value "$1" $#; CDR_FILE="$2"; shift 2 ;;
      --recordings)  need_value "$1" $#; RECORDINGS="$2"; shift 2 ;;
      --interval)    need_value "$1" $#; INTERVAL="$2"; shift 2 ;;
      --url=*)        URL="${1#*=}"; shift ;;
      --key=*)        KEY="${1#*=}"; shift ;;
      --name=*)       AGENT_NAME="${1#*=}"; shift ;;
      --source=*)     SOURCE="${1#*=}"; shift ;;
      --cdr-file=*)   CDR_FILE="${1#*=}"; shift ;;
      --recordings=*) RECORDINGS="${1#*=}"; shift ;;
      --interval=*)   INTERVAL="${1#*=}"; shift ;;
      --no-service)  CREATE_SERVICE=0; shift ;;
      --force)       FORCE=1; shift ;;
      --uninstall)   UNINSTALL=1; shift ;;
      --yes|-y)      ASSUME_YES=1; shift ;;
      -h|--help)     usage; exit 0 ;;
      *)
        die "Неизвестный ключ: «$1»." "Список ключей: bash install.sh --help"
        ;;
    esac
  done
}

# ---------------------------------------------------------------------------
# Проверки окружения
# ---------------------------------------------------------------------------

require_root() {
  if [ "$(id -u)" -ne 0 ]; then
    die "Установщик должен работать от root: он пишет в /opt, /etc и /var/lib." \
        "Повторите через sudo: curl … | sudo bash -s -- --url … --key …"
  fi
}

# Python ищем сам, а не берём первый попавшийся: на CentOS 7 рядом живут
# /usr/bin/python (это 2.7, агент на нём не запустится вовсе) и
# /usr/bin/python3, а на части станций — ещё и сборка из SCL.
find_python() {
  local candidate version
  for candidate in python3 /usr/bin/python3 /usr/libexec/platform-python \
                  /opt/rh/rh-python36/root/usr/bin/python3; do
    if have "${candidate}" || [ -x "${candidate}" ]; then
      if "${candidate}" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 6) else 1)' \
         >/dev/null 2>&1; then
        PYTHON_BIN="$(command -v "${candidate}" 2>/dev/null || printf '%s' "${candidate}")"
        version="$("${PYTHON_BIN}" -c 'import platform; print(platform.python_version())' 2>/dev/null || true)"
        ok "Python: ${PYTHON_BIN} (${version:-версия не определилась})"
        return 0
      fi
    fi
  done
  die "Не найден Python 3.6 или новее." \
      "На CentOS 7: yum install -y python3 (или rh-python36 из SCL). \
На Debian/Ubuntu: apt-get install -y python3. \
Агенту нужен только сам интерпретатор — ни одного пакета из pip он не требует."
}

check_downloader() {
  if have curl || have wget; then
    return 0
  fi
  die "На станции нет ни curl, ни wget — скачать агента нечем." \
      "Поставьте любой из них: yum install -y curl (CentOS) \
или apt-get install -y curl (Debian/Ubuntu)."
}

check_writable() {
  local dir="$1" why="$2"
  if [ ! -d "${dir}" ]; then
    mkdir -p "${dir}" 2>/dev/null || die \
      "Каталог ${dir} не создаётся (${why})." \
      "Проверьте, что раздел смонтирован на запись и на нём есть место: df -h ${dir}"
  fi
  if [ ! -w "${dir}" ]; then
    die "Каталог ${dir} недоступен на запись (${why})." \
        "Запустите установщик от root."
  fi
}

# Скачивание с ключом в заголовке. Ключ в заголовке, а не в адресе,
# намеренно: адрес попадает в журнал обратного прокси и в историю команд.
#
# Ключ идёт в curl файлом настроек (-K), а не аргументом -H: аргументы любой
# программы видны каждому пользователю станции в `ps` и /proc/*/cmdline, и
# ключ с правом записи утекал бы любому, кто зайдёт на станцию. Файл лежит
# во временном каталоге установщика (mktemp -d — права 0700) и удаляется
# вместе с ним. У GNU wget то же делает --config; у wget из busybox его нет,
# и там ключ, увы, идёт аргументом — curl на станциях встречается чаще.
download() {
  local address="$1" dest="$2" code=""
  if have curl; then
    ( umask 077; printf 'header = "X-API-Key: %s"\n' "${KEY}" >"${TMP_DIR}/curl.cfg" )
    # Код ответа — только то, что напечатал сам curl. Прежнее
    # «… -w '%{http_code}' … || printf '000'» при недоступном сервере
    # давало «000000»: curl печатает 000 и выходит с ошибкой, и к его
    # ответу дописывался ещё один.
    code="$(curl -sS --connect-timeout 10 --max-time 300 -K "${TMP_DIR}/curl.cfg" \
             -o "${dest}" -w '%{http_code}' "${address}" \
             2>"${TMP_DIR}/curl.err")" || true
  else
    local key_option="--header=X-API-Key: ${KEY}"
    if wget --help 2>&1 | grep -q -- '--config'; then
      ( umask 077; printf 'header = X-API-Key: %s\n' "${KEY}" >"${TMP_DIR}/wgetrc" )
      key_option="--config=${TMP_DIR}/wgetrc"
    fi
    if wget -q --timeout=30 --tries=2 "${key_option}" \
            -O "${dest}" "${address}" 2>"${TMP_DIR}/wget.err"; then
      code="200"
    else
      code="000"
    fi
  fi
  case "${code}" in
    ''|000*) code="000" ;;
  esac
  case "${code}" in
    200) return 0 ;;
    401|403)
      die "Сервер не принял ключ доступа (HTTP ${code})." \
          "Проверьте, что ключ скопирован целиком (начинается с «ah_»), \
не отозван и имеет право записи. Ключи заводятся в разделе «Доступ» \
веб-интерфейса." ;;
    404)
      die "Сервер не знает адреса ${address} (HTTP 404)." \
          "Похоже, сервер старее агента: раздача агента появилась в ASR Hub 3.0. \
Обновите сервер." ;;
    000)
      die "Не удалось скачать ${address}: сервер недоступен." \
          "Проверьте со станции: curl -sS -m 10 ${URL}/api/health \
— и что исходящие соединения на этот порт не закрыты межсетевым экраном. \
Подробности: $(tr '\n' ' ' <"${TMP_DIR}/curl.err" 2>/dev/null || true)" ;;
    *)
      die "Сервер ответил HTTP ${code} на ${address}." \
          "Посмотрите журнал сервера ASR Hub: раздел «Журнал» веб-интерфейса." ;;
  esac
}

check_server() {
  local code=""
  if have curl; then
    code="$(curl -sS --connect-timeout 10 --max-time 30 -o /dev/null \
            -w '%{http_code}' "${URL}/api/health" 2>/dev/null)" || true
    case "${code}" in
      ''|000*) code="000" ;;
    esac
  else
    if wget -q --timeout=15 --tries=1 -O /dev/null "${URL}/api/health" 2>/dev/null; then
      code="200"
    else
      code="000"
    fi
  fi
  case "${code}" in
    200|503)
      # 503 — сервер жив, но ещё прогревается или без движка: ставить
      # агента это не мешает, звонки подождут в очереди.
      ok "Сервер отвечает: ${URL} (HTTP ${code})" ;;
    000)
      die "Сервер ${URL} со станции не отвечает." \
          "Проверьте адрес и порт, а также что со станции есть выход наружу: \
curl -sS -m 10 ${URL}/api/health. Агент ходит на сервер сам — входящие \
соединения станции открывать не нужно." ;;
    *)
      warn "Сервер ${URL} ответил HTTP ${code} на /api/health — продолжаю, \
но если установка сорвётся, начните с этого." ;;
  esac
}

# ---------------------------------------------------------------------------
# Настройки
# ---------------------------------------------------------------------------

# Правка одной строки в INI: ключ либо заменяется на месте, либо
# дописывается в секцию [agent]. Так обновление url и key не трогает
# остальное, что человек успел там настроить руками.
set_option() {
  local name="$1" value="$2" file="$3"
  if [ -z "${value}" ]; then return 0; fi
  if grep -qE "^[[:space:]]*${name}[[:space:]]*=" "${file}" 2>/dev/null; then
    local tmp="${file}.tmp$$"
    # Временный файл — с правами 0600 с самого начала: в нём весь файл
    # настроек вместе с ключом доступа, а по умолчанию он создавался 0644
    # в /etc и был виден любому пользователю станции.
    ( umask 077; : >"${tmp}" )
    # Значение идёт в awk окружением, а не подстановкой в текст
    # программы: в ключе доступа и в пути встречаются косые и кавычки,
    # и одна такая превратила бы правку настроек в синтаксическую
    # ошибку awk — с пустым файлом настроек на выходе.
    # Имена переменных внутри awk — латиницей: mawk и busybox awk, которые
    # стоят на станциях чаще gawk, кириллицу в именах не принимают вовсе.
    OPT_NAME="${name}" OPT_VALUE="${value}" awk '
      BEGIN { name = ENVIRON["OPT_NAME"]; value = ENVIRON["OPT_VALUE"]; done = 0 }
      {
        if (!done && $0 ~ "^[ \t]*" name "[ \t]*=") {
          print name " = " value
          done = 1
        } else {
          print $0
        }
      }' "${file}" >"${tmp}"
    cat "${tmp}" >"${file}"
    rm -f "${tmp}"
  else
    printf '%s = %s\n' "${name}" "${value}" >>"${file}"
  fi
}

write_config() {
  if [ -f "${CONFIG_FILE}" ] && [ "${FORCE}" -eq 0 ]; then
    info "Файл ${CONFIG_FILE} уже есть — сохраняю его и обновляю только адрес и ключ."
    chmod 600 "${CONFIG_FILE}"
    set_option "url" "${URL}" "${CONFIG_FILE}"
    set_option "key" "${KEY}" "${CONFIG_FILE}"
    set_option "name" "${AGENT_NAME}" "${CONFIG_FILE}"
    set_option "source" "${SOURCE}" "${CONFIG_FILE}"
    set_option "cdr_file" "${CDR_FILE}" "${CONFIG_FILE}"
    set_option "recordings_dir" "${RECORDINGS}" "${CONFIG_FILE}"
    set_option "interval" "${INTERVAL}" "${CONFIG_FILE}"
    ok "Настройки обновлены: ${CONFIG_FILE}"
    return 0
  fi

  # Файл создаётся сразу с правами 600 и только потом наполняется: между
  # созданием и chmod ключ доступа успел бы полежать читаемым для всех.
  local tmp="${CONFIG_FILE}.new$$"
  : >"${tmp}"
  chmod 600 "${tmp}"
  {
    printf '# Настройки агента ASR Hub. Ключи командной строки перекрывают этот файл.\n'
    printf '# Права 600 — здесь лежит ключ доступа ко всему архиву разговоров.\n'
    printf '[agent]\n'
    printf 'url = %s\n' "${URL}"
    printf 'key = %s\n' "${KEY}"
    printf 'name = %s\n' "${AGENT_NAME:-$(hostname)}"
    printf 'source = %s\n' "${SOURCE:-auto}"
    if [ -n "${CDR_FILE}" ]; then printf 'cdr_file = %s\n' "${CDR_FILE}"; fi
    printf 'recordings_dir = %s\n' "${RECORDINGS:-/var/spool/asterisk/monitor}"
    printf 'state_dir = %s\n' "${STATE_DIR}"
    printf 'log_file = %s\n' "${LOG_FILE}"
    printf '# Шаблон имени файла записи, если он известен. Подстановки:\n'
    printf '# {uniqueid}, {src}, {dst}, {год}, {месяц}, {день} (и ${UNIQUEID} и т. п.)\n'
    printf '# filename =\n'
    printf 'interval = %s\n' "${INTERVAL:-60}"
    printf '# Отсев на станции: не слать разговоры короче стольких секунд\n'
    printf '# и не слать неотвеченные. Пусто — правила задаёт сервер.\n'
    printf '# min_duration_s = 10\n'
    printf '# skip_unanswered = да\n'
    printf '# max_file_mb =\n'
    printf '# insecure = нет\n'
  } >>"${tmp}"
  mv -f "${tmp}" "${CONFIG_FILE}"
  chmod 600 "${CONFIG_FILE}"
  ok "Настройки записаны: ${CONFIG_FILE} (права 600)"
}

# ---------------------------------------------------------------------------
# Служба
# ---------------------------------------------------------------------------

pick_service_user() {
  # От пользователя asterisk агент читает журнал и записи без лишних прав.
  # От root он тоже прочитает, но на чужой станции лишние права — это то,
  # за что потом отвечает подрядчик.
  if id asterisk >/dev/null 2>&1; then
    SERVICE_USER="asterisk"
    SERVICE_GROUP="$(id -gn asterisk 2>/dev/null || printf 'asterisk')"
  else
    SERVICE_USER="root"
    SERVICE_GROUP="root"
    warn "Пользователя asterisk на станции нет — служба будет работать от root."
  fi
}

write_unit() {
  cat >"${UNIT_FILE}" <<ЮНИТ
[Unit]
Description=Агент ASR Hub: сбор звонков со станции Asterisk
Documentation=${URL}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
ExecStart=${PYTHON_BIN} ${AGENT_FILE} --config ${CONFIG_FILE} --follow
Restart=always
RestartSec=10
# Агенту нечего запускать с повышением прав и незачем видеть чужой /tmp.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=true
# Пишет он ровно в два места: своё состояние и свой журнал.
ReadWritePaths=${STATE_DIR} /var/log
StandardOutput=journal
StandardError=journal
SyslogIdentifier=asrhub-agent

[Install]
WantedBy=multi-user.target
ЮНИТ
  chmod 644 "${UNIT_FILE}"
  ok "Служба описана: ${UNIT_FILE}"
}

run_selftest() {
  local code=0
  step "Самопроверка (ничего не отправляется)"
  if [ "${SERVICE_USER}" != "root" ] && have runuser; then
    runuser -u "${SERVICE_USER}" -- "${PYTHON_BIN}" "${AGENT_FILE}" \
      --config "${CONFIG_FILE}" --selftest || code=$?
  else
    "${PYTHON_BIN}" "${AGENT_FILE}" --config "${CONFIG_FILE}" --selftest || code=$?
  fi
  return "${code}"
}

# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------

confirm() {
  local question="$1"
  if [ "${ASSUME_YES}" -eq 1 ]; then return 0; fi
  # Установщик обычно запущен как «curl … | bash», и на входе у него —
  # собственный текст, а не клавиатура. Спрашиваем у терминала напрямую;
  # терминала нет — честно требуем --yes, а не «молча соглашаемся».
  # Проверяем не права на /dev/tty, а то, что он вообще открывается:
  # у процесса без управляющего терминала файл на месте, а открытие даёт
  # «No such device or address». Молча считать это согласием нельзя —
  # речь об удалении.
  if ! (: </dev/tty) 2>/dev/null; then
    die "Нужно подтверждение, но терминала нет (установщик запущен через конвейер)." \
        "Повторите с ключом --yes: … | sudo bash -s -- --uninstall --yes"
  fi
  printf '%s [д/N] ' "${question}" >/dev/tty
  local answer=""
  read -r answer </dev/tty || answer=""
  case "${answer}" in
    д|Д|y|Y|yes|да|ДА) return 0 ;;
    *) return 1 ;;
  esac
}

do_uninstall() {
  step "Удаление агента ASR Hub"
  info "Будут убраны:"
  info "  служба      ${UNIT_FILE}"
  info "  программа   ${INSTALL_DIR}"
  info "  настройки   ${CONFIG_FILE}"
  info "  состояние   ${STATE_DIR}"
  info "  журнал      ${LOG_FILE}"
  info "Звонки, уже отправленные на сервер, остаются в архиве сервера."
  if ! confirm "Удалить агента со станции?"; then
    info "Отменено. Ничего не изменилось."
    exit 0
  fi
  if have systemctl; then
    systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
  fi
  rm -f "${UNIT_FILE}"
  if have systemctl; then
    systemctl daemon-reload 2>/dev/null || true
    systemctl reset-failed "${SERVICE_NAME}" 2>/dev/null || true
  fi
  rm -rf "${INSTALL_DIR}"
  rm -f "${CONFIG_FILE}"
  rm -rf "${STATE_DIR}"
  rm -f "${LOG_FILE}" "${LOG_FILE}".[0-9] "${LOG_FILE}".[0-9].*
  ok "Агент удалён со станции."
  info "Если агент больше не нужен и на сервере, уберите его в разделе «АТС»."
  exit 0
}

# ---------------------------------------------------------------------------
# Установка
# ---------------------------------------------------------------------------

do_install() {
  step "Проверка станции"
  find_python
  check_downloader
  check_writable "/etc" "сюда пишутся настройки"
  check_writable "${STATE_DIR}" "здесь агент хранит, докуда дочитал журнал"
  check_writable "${INSTALL_DIR}" "сюда кладётся сам агент"
  check_server

  step "Загрузка агента с сервера"
  download "${URL}/api/telephony/agent/asrhub-agent.py" "${TMP_DIR}/asrhub-agent.py"
  if ! head -c 200 "${TMP_DIR}/asrhub-agent.py" | grep -q 'python'; then
    die "С сервера приехал не агент, а что-то другое." \
        "Скорее всего, по адресу ${URL} отвечает прокси или страница входа. \
Проверьте адрес: он должен вести прямо на ASR Hub."
  fi
  if ! "${PYTHON_BIN}" -c "import ast,io,sys; ast.parse(io.open(sys.argv[1], encoding='utf-8').read())" \
       "${TMP_DIR}/asrhub-agent.py" >/dev/null 2>&1; then
    die "Скачанный файл агента повреждён (не разбирается как программа на Python)." \
        "Повторите установку; если повторяется — проверьте, не режет ли ответ \
прокси на пути к серверу."
  fi
  install -m 0755 "${TMP_DIR}/asrhub-agent.py" "${AGENT_FILE}"
  ok "Агент установлен: ${AGENT_FILE}"

  step "Настройки"
  write_config
  chmod 700 "${STATE_DIR}"

  pick_service_user
  if [ "${SERVICE_USER}" != "root" ]; then
    chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "${STATE_DIR}" || \
      warn "Не удалось передать ${STATE_DIR} пользователю ${SERVICE_USER}."
    # Журнал создаём заранее и от нужного пользователя: иначе первая же
    # запись службы упрётся в права на /var/log, и агент уйдёт писать
    # только в journal — там его никто искать не станет.
    touch "${LOG_FILE}" 2>/dev/null || true
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${LOG_FILE}" 2>/dev/null || true
    chmod 640 "${LOG_FILE}" 2>/dev/null || true
    chown "${SERVICE_USER}:${SERVICE_GROUP}" "${CONFIG_FILE}" 2>/dev/null || true
  fi

  local selftest_code=0
  run_selftest || selftest_code=$?
  if [ "${selftest_code}" -ne 0 ] && [ "${FORCE}" -eq 0 ]; then
    warn "Самопроверка нашла неисправности (см. таблицу выше)."
    warn "Служба НЕ включена намеренно: она перезапускалась бы каждые десять \
секунд и выглядела бы работающей."
    info ""
    info "Исправьте отмеченное и повторите:"
    info "  sudo ${PYTHON_BIN} ${AGENT_FILE} --selftest"
    info "  sudo systemctl enable --now ${SERVICE_NAME}   # когда всё [ ок ]"
    if [ "${CREATE_SERVICE}" -eq 1 ] && have systemctl; then
      write_unit
      systemctl daemon-reload
    fi
    exit 1
  fi

  if [ "${CREATE_SERVICE}" -eq 0 ]; then
    step "Служба не заводится (--no-service)"
    info "Запуск вручную: ${PYTHON_BIN} ${AGENT_FILE} --once"
    print_summary
    return 0
  fi

  if ! have systemctl; then
    step "systemd на станции нет"
    warn "Служба не заведена. Запускайте агента из cron или из своей системы \
запуска:"
    info "  */5 * * * * ${PYTHON_BIN} ${AGENT_FILE} --once >/dev/null 2>&1"
    print_summary
    return 0
  fi

  step "Служба"
  write_unit
  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1 || \
    warn "Не удалось включить автозапуск службы — проверьте systemctl status."
  systemctl restart "${SERVICE_NAME}"
  sleep 2
  if systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "Служба ${SERVICE_NAME} запущена и включена в автозапуск."
  else
    warn "Служба не удержалась. Что она сказала:"
    systemctl status "${SERVICE_NAME}" --no-pager --lines 20 || true
    die "Служба ${SERVICE_NAME} не запустилась." \
        "Посмотрите журнал: journalctl -u ${SERVICE_NAME} -n 50 --no-pager \
— и запустите самопроверку: ${PYTHON_BIN} ${AGENT_FILE} --selftest"
  fi
  print_summary
}

print_summary() {
  cat <<ИТОГ

──────────────────────────────────────────────────────────────────────
Агент ASR Hub ${AGENT_VERSION} установлен.

  сервер      ${URL}
  ключ        $(mask_key "${KEY}")
  станция     ${AGENT_NAME:-$(hostname)}
  программа   ${AGENT_FILE}
  настройки   ${CONFIG_FILE}
  состояние   ${STATE_DIR}
  журнал      ${LOG_FILE}

Что дальше:
  посмотреть журнал     journalctl -u ${SERVICE_NAME} -f
                        tail -f ${LOG_FILE}
  состояние службы      systemctl status ${SERVICE_NAME}
  проверить настройку   ${PYTHON_BIN} ${AGENT_FILE} --selftest
  собрать весь архив    ${PYTHON_BIN} ${AGENT_FILE} --all
                        (или кнопкой «Собрать всё» в разделе «АТС»)
  остановить            systemctl stop ${SERVICE_NAME}
  удалить               sudo bash install.sh --uninstall

Станция появится в разделе «АТС» веб-интерфейса после первого обращения
агента к серверу — обычно в течение минуты.
──────────────────────────────────────────────────────────────────────
ИТОГ
}

# ---------------------------------------------------------------------------
# Порядок работы
# ---------------------------------------------------------------------------

main() {
  parse_args "$@"
  require_root

  if [ "${UNINSTALL}" -eq 1 ]; then
    do_uninstall
  fi

  # Подстановка не сработала (файл взяли из исходников, а не с сервера) —
  # тогда адрес обязан быть в ключах. Сравнение с «@@» намеренно частичное:
  # так же выглядит и не подставленная версия.
  if [ -z "${URL}" ]; then
    case "${SERVER_URL_DEFAULT}" in
      @@*) URL="" ;;
      *)   URL="${SERVER_URL_DEFAULT}" ;;
    esac
  fi
  URL="${URL%/}"
  case "${AGENT_VERSION}" in
    @@*) AGENT_VERSION="из исходников" ;;
  esac

  if [ -z "${URL}" ]; then
    die "Не задан адрес сервера ASR Hub." \
        "Добавьте ключ --url http://сервер:8081 — тот же адрес, по которому \
вы открываете веб-интерфейс."
  fi
  case "${URL}" in
    http://*|https://*) : ;;
    *) die "Адрес сервера должен начинаться с http:// или https://: «${URL}»." \
           "Например: --url http://192.168.0.10:8081" ;;
  esac
  if [ -z "${KEY}" ]; then
    die "Не задан ключ доступа." \
        "Заведите ключ с правом записи в разделе «Доступ» веб-интерфейса \
и добавьте --key ah_…"
  fi

  TMP_DIR="$(mktemp -d /tmp/asrhub-agent.XXXXXX)" || die \
    "Не удалось создать временный каталог в /tmp." \
    "Проверьте свободное место и права: df -h /tmp"

  info "Установка агента ASR Hub ${AGENT_VERSION}"
  info "  сервер: ${URL}"
  info "  ключ:   $(mask_key "${KEY}")"
  do_install
}

main "$@"
