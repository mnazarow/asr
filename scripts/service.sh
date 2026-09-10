#!/usr/bin/env bash
#
# Управление службой ASR Hub: systemd на Linux, launchd на macOS.
#
#   bash scripts/service.sh install --prefix /opt/asrhub --data /var/lib/asrhub
#   bash scripts/service.sh {start|stop|restart|status|logs|uninstall}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

# Журнал есть, а ловушки ERR нет — намеренно: и доктор, и управление
# службой штатно получают ненулевые коды (проверка не прошла, служба
# остановлена), и обрывать на них работу значило бы превращать обычный
# ответ в аварию.
setup_logging "${TMPDIR:-/tmp}"

ACTION="${1:-status}"; shift || true

PREFIX="/opt/asrhub"
DATA_DIR="/var/lib/asrhub"
PORT="8080"
HOST="0.0.0.0"
SERVICE_USER=""
SERVICE_NAME="asrhub"
FOLLOW=0
LINES=100
RESTORE_FROM=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --port)   PORT="${2:?}"; shift 2 ;;
    --host)   HOST="${2:?}"; shift 2 ;;
    --user)   SERVICE_USER="${2:?}"; shift 2 ;;
    --name)   SERVICE_NAME="${2:?}"; shift 2 ;;
    --follow|-f) FOLLOW=1; shift ;;
    --lines|-n) LINES="${2:?}"; shift 2 ;;
    --from)   RESTORE_FROM="${2:?}"; shift 2 ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    --dry-run) ASRHUB_DRY_RUN=1; shift ;;
    *) shift ;;
  esac
done

# Интерпретатор ASR Hub. Ставить пакеты в системный python нельзя, поэтому
# всё живёт в venv рядом с программой; отдельный поиск нужен потому, что
# обслуживание запускают и на машине, где venv собран другой версией.
asrhub_python() {
  local candidate
  for candidate in "${PREFIX}/venv/bin/python" "${PREFIX}/venv/bin/python3" \
                   "${PREFIX}/venv/Scripts/python.exe"; do
    [[ -x "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}

OS="$(detect_os)"
PLIST="${HOME}/Library/LaunchAgents/com.asrhub.server.plist"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
USER_UNIT="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

use_user_systemd() { [[ ! -w /etc/systemd/system ]] && ! is_root; }

# Перечитывает определения юнитов, если файл на диске разошёлся с тем, что
# systemd держит в памяти.
#
# Правка юнита сама по себе ни на что не влияет: служба продолжает работать
# по определению, загруженному в прошлый раз. Перезагрузку делала только
# установка, а update.sh юнит не переустанавливает — он лишь останавливает и
# запускает службу. Из-за этого правки жили на диске, но не в работе:
# переменные каталогов кеша (MPLCONFIGDIR и соседи) прописаны в файле, а
# служба о них не знает, и в журнале снова копятся жалобы на недоступный
# кеш. Сам systemd об этом честно предупреждает в каждой команде — «unit
# file … changed on disk» — и он же умеет ответить, нужна ли перезагрузка.
reload_units_if_needed() {
  have systemctl || return 0
  [[ "${OS}" == "macos" ]] && return 0
  local need=""
  if use_user_systemd; then
    need="$(systemctl --user show "${SERVICE_NAME}.service" \
             -p NeedDaemonReload --value 2>/dev/null || true)"
  else
    need="$(systemctl show "${SERVICE_NAME}.service" \
             -p NeedDaemonReload --value 2>/dev/null || true)"
  fi
  [[ "${need}" == "yes" ]] || return 0
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] systemctl daemon-reload\n'
    return 0
  fi
  info "Определение службы на диске новее загруженного — перечитываем."
  if use_user_systemd; then
    systemctl --user daemon-reload 2>/dev/null || true
  else
    as_root systemctl daemon-reload 2>/dev/null || true
  fi
  return 0
}

# ---------------------------------------------------------------------------

install_systemd() {
  local unit_path="${UNIT}"
  local user_mode=0
  if use_user_systemd; then
    unit_path="${USER_UNIT}"; user_mode=1
    mkdir -p "$(dirname "${USER_UNIT}")"
    info "Права на /etc/systemd отсутствуют — служба ставится для текущего пользователя."
  fi
  local run_user="${SERVICE_USER}"
  [[ -z "${run_user}" && "${user_mode}" -eq 0 ]] && run_user="root"

  local content
  # Пути с пробелами systemd режет по пробелу: ExecStart искал программу
  # «/opt/asr», ReadWritePaths отбрасывался целиком (а с ProtectSystem=full
  # это делает каталог данных доступным только на чтение), Documentation
  # превращался в неверный URL. Значения, попадающие в директивы, берём в
  # кавычки; в URL пробел заменяем на %20.
  local WORKDIR_Q EXEC_Q DATA_DIR_Q PREFIX_ESC
  # Формы экранирования у systemd разные, и перепутать их значит получить
  # молча сломанный юнит. В путях (WorkingDirectory, ReadWritePaths) кавычки
  # не принимаются — там пробел пишется как \x20. В командной строке
  # (ExecStart) наоборот работают кавычки. В URL пробел — это %20, а сам
  # знак процента у systemd начинает подстановку, поэтому удваивается.
  WORKDIR_Q="${PREFIX// /\\x20}/server"
  EXEC_Q="\"${PREFIX}/venv/bin/python\""
  DATA_DIR_Q="${DATA_DIR// /\\x20}"
  PREFIX_ESC="${PREFIX// /%%20}"

  content="$(cat <<UNITEOF
[Unit]
Description=ASR Hub — сервер распознавания речи
Documentation=file://${PREFIX_ESC}/docs/README.md
After=network-online.target
Wants=network-online.target
# StartLimit* — ключи секции [Unit]. В [Service] systemd их игнорирует с
# записью «Unknown key name», и задуманное «5 попыток за 120 с» не работало:
# оставалось умолчание.
StartLimitBurst=5
StartLimitIntervalSec=120

[Service]
Type=simple
${run_user:+User=${run_user}}
WorkingDirectory=${WORKDIR_Q}
Environment="ASRHUB_DATA_DIR=${DATA_DIR}"
Environment="PYTHONUNBUFFERED=1"
Environment="HF_HOME=${DATA_DIR}/models"
# Домашний каталог службы смонтирован только на чтение (ProtectHome), и
# библиотеки, которые кладут кеш в ~/.config, при каждом запуске ругались в
# журнал и заново собирали его во временном каталоге. Показываем им место,
# куда писать можно.
Environment="MPLCONFIGDIR=${DATA_DIR}/cache/matplotlib"
Environment="XDG_CACHE_HOME=${DATA_DIR}/cache"
Environment="NUMBA_CACHE_DIR=${DATA_DIR}/cache/numba"
EnvironmentFile=-${DATA_DIR}/env.sh
ExecStart=${EXEC_Q} -m asrhub --host ${HOST} --port ${PORT}
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=10
TimeoutStopSec=45
KillMode=mixed

# Ограничение ресурсов: сервер не должен утянуть машину при утечке
LimitNOFILE=65535
MemoryHigh=90%

# Изоляция
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=${DATA_DIR_Q}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=${SERVICE_NAME}

[Install]
WantedBy=$( [[ "${user_mode}" -eq 1 ]] && echo default.target || echo multi-user.target )
UNITEOF
)"

  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '%s\n' "${content}"
    return 0
  fi

  # Каталоги для кеша библиотек: без них MPLCONFIGDIR и его соседи указывают
  # на несуществующий путь, и всё возвращается к жалобе в журнал.
  mkdir -p "${DATA_DIR}/cache/matplotlib" "${DATA_DIR}/cache/numba" 2>/dev/null || \
    as_root mkdir -p "${DATA_DIR}/cache/matplotlib" "${DATA_DIR}/cache/numba" 2>/dev/null || true
  if [[ -n "${run_user}" && "${run_user}" != "root" ]]; then
    as_root chown -R "${run_user}" "${DATA_DIR}/cache" 2>/dev/null || true
  fi

  if [[ "${user_mode}" -eq 1 ]]; then
    printf '%s\n' "${content}" > "${unit_path}"
    run systemctl --user daemon-reload
    run systemctl --user enable --now "${SERVICE_NAME}.service"
    ok "Служба пользователя создана: ${unit_path}"
    hint "Автозапуск без входа в систему: sudo loginctl enable-linger ${USER}"
  else
    printf '%s\n' "${content}" | as_root tee "${unit_path}" >/dev/null
    as_root systemctl daemon-reload
    as_root systemctl enable --now "${SERVICE_NAME}.service"
    ok "Служба создана: ${unit_path}"
  fi
}

install_launchd() {
  mkdir -p "$(dirname "${PLIST}")" "${DATA_DIR}/logs"
  local content
  content="$(cat <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.asrhub.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>${PREFIX}/venv/bin/python</string>
    <string>-m</string><string>asrhub</string>
    <string>--host</string><string>${HOST}</string>
    <string>--port</string><string>${PORT}</string>
  </array>
  <key>WorkingDirectory</key><string>${PREFIX}/server</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>ASRHUB_DATA_DIR</key><string>${DATA_DIR}</string>
    <key>HF_HOME</key><string>${DATA_DIR}/models</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/><key>Crashed</key><true/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>${DATA_DIR}/logs/service.log</string>
  <key>StandardErrorPath</key><string>${DATA_DIR}/logs/service-error.log</string>
  <key>ProcessType</key><string>Adaptive</string>
</dict>
</plist>
PLISTEOF
)"
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then printf '%s\n' "${content}"; return 0; fi
  printf '%s\n' "${content}" > "${PLIST}"
  run launchctl unload "${PLIST}" 2>/dev/null || true
  run launchctl load -w "${PLIST}"
  ok "Служба launchd создана: ${PLIST}"
}

# ---------------------------------------------------------------------------

case "${ACTION}" in
  install)
    if [[ "${OS}" == "macos" ]]; then install_launchd
    elif have systemctl; then install_systemd
    else
      warn "Ни systemd, ни launchd не найдены — автозапуск не настроен."
      hint "Запускайте вручную: ${PREFIX}/venv/bin/python -m asrhub --port ${PORT}"
      exit 1
    fi ;;

  uninstall)
    # Пробный запуск не останавливает и не сносит работающую службу: он
    # только рассказывает, что сделал бы. Раньше флаг не доходил до этого
    # скрипта, и «uninstall --dry-run» гасил службу по-настоящему.
    if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
      if [[ "${OS}" == "macos" ]]; then
        printf '  [пробный запуск] launchctl unload %s; rm -f %s\n' "${PLIST}" "${PLIST}"
      else
        printf '  [пробный запуск] systemctl disable --now %s.service\n' "${SERVICE_NAME}"
        printf '  [пробный запуск] rm -f %s\n' "${UNIT}"
      fi
      exit 0
    fi
    if [[ "${OS}" == "macos" ]]; then
      [[ -f "${PLIST}" ]] && { run launchctl unload "${PLIST}" 2>/dev/null || true; rm -f "${PLIST}"; }
      ok "Служба launchd удалена"
    elif have systemctl; then
      if use_user_systemd; then
        systemctl --user disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
        rm -f "${USER_UNIT}"
        systemctl --user daemon-reload 2>/dev/null || true
      else
        as_root systemctl disable --now "${SERVICE_NAME}.service" 2>/dev/null || true
        as_root rm -f "${UNIT}"
        as_root systemctl daemon-reload 2>/dev/null || true
      fi
      ok "Служба systemd удалена"
    fi ;;

  start|stop|restart)
    # До самой команды: запуск по устаревшему определению — это ровно та
    # тихая поломка, ради которой всё и затевалось.
    reload_units_if_needed
    if [[ "${OS}" == "macos" ]]; then
      case "${ACTION}" in
        start)   run launchctl load -w "${PLIST}" ;;
        stop)    run launchctl unload "${PLIST}" ;;
        restart) launchctl unload "${PLIST}" 2>/dev/null || true; run launchctl load -w "${PLIST}" ;;
      esac
    elif have systemctl; then
      if use_user_systemd; then run systemctl --user "${ACTION}" "${SERVICE_NAME}.service"
      else as_root systemctl "${ACTION}" "${SERVICE_NAME}.service"; fi
    else
      error "Управление службой недоступно на этой системе."; exit 1
    fi
    # «Выполнено: start» означало только то, что команду приняли. Служба,
    # падавшая через секунду после запуска, отчитывалась галочкой — и дальше
    # обновление сообщало «сервер не отвечает», не связывая одно с другим.
    if [[ "${ACTION}" != "stop" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
      sleep 2
      STATE="$(service_state "${SERVICE_NAME}" || true)"
      case "${STATE}" in
        running)    ok "Служба запущена" ;;
        activating) ok "Служба запускается" ;;
        failed)
          error "Служба не поднялась."
          server_log_tail 20 "${DATA_DIR}" "${SERVICE_NAME}" | sed 's/^/  /' >&2
          hint "Полный журнал: bash ${SCRIPT_DIR}/service.sh logs -n 200"
          exit 1 ;;
        *)          ok "Выполнено: ${ACTION}" ;;
      esac
    else
      ok "Выполнено: ${ACTION}"
    fi ;;

  status)
    if [[ "${OS}" == "macos" ]]; then
      # `grep -q` рвёт трубу на первом совпадении, launchctl получает
      # SIGPIPE, и pipefail делает код конвейера 141 — «не нашли». А настоящий
      # `launchctl list` печатает сотни строк, поэтому служба на macOS всегда
      # объявлялась незапущенной. Отсюда же тянулось: update.sh определял
      # WAS_RUNNING этим же вызовом и не поднимал сервер после обновления,
      # оставляя его на старом коде.
      if [[ "$(set +o pipefail; launchctl list 2>/dev/null \
               | grep -c com.asrhub.server || true)" -gt 0 ]]; then
        ok "Служба запущена"
        launchctl list com.asrhub.server 2>/dev/null | head -12
      else
        warn "Служба не запущена"; exit 1
      fi
    elif have systemctl; then
      if use_user_systemd; then systemctl --user status "${SERVICE_NAME}.service" --no-pager -l || exit 1
      else systemctl status "${SERVICE_NAME}.service" --no-pager -l || exit 1; fi
    else
      pgrep -f "m asrhub" >/dev/null && ok "Процесс запущен" || { warn "Процесс не найден"; exit 1; }
    fi ;;

  logs)
    if [[ "${OS}" == "macos" ]]; then
      if [[ "${FOLLOW}" -eq 1 ]]; then tail -f "${DATA_DIR}/logs/service.log"
      else tail -n "${LINES}" "${DATA_DIR}/logs/service.log" 2>/dev/null || warn "Журнал пуст."; fi
    elif have journalctl; then
      if use_user_systemd; then journalctl --user -u "${SERVICE_NAME}.service" -n "${LINES}" $( [[ "${FOLLOW}" -eq 1 ]] && echo -f ) --no-pager
      else journalctl -u "${SERVICE_NAME}.service" -n "${LINES}" $( [[ "${FOLLOW}" -eq 1 ]] && echo -f ) --no-pager; fi
    else
      tail -n "${LINES}" "${DATA_DIR}/logs/asrhub.log" 2>/dev/null || warn "Журнал не найден."
    fi ;;

  backup)
    # Копия снимается командой SQLite «.backup», а не копированием файла:
    # база работает в режиме WAL, рядом лежат -wal и -shm, и обычная копия
    # получается несогласованной — выглядит как копия и ею не является.
    py="$(asrhub_python)" || { error "Не найден интерпретатор ASR Hub."; exit 1; }
    ASRHUB_DATA_DIR="${DATA_DIR}" "${py}" - <<'PYCODE' || exit 1
from pathlib import Path

from asrhub.config import load
from asrhub.db import Database
from asrhub.maintenance import backup_dir, make_backup

settings = load()
db = Database(Path(settings.paths.data) / "asrhub.db")
копия = make_backup(db, settings)
if копия is None:
    print("Копию сделать не удалось — причина в журнале выше.")
    raise SystemExit(1)
print(f"Копия: {копия}")
print(f"Каталог копий: {backup_dir(settings)}")
PYCODE
    ok "Резервная копия готова" ;;

  restore)
    [[ -n "${RESTORE_FROM}" ]] || { error "Укажите файл копии: --from ПУТЬ"; exit 2; }
    [[ -f "${RESTORE_FROM}" ]] || { error "Файл не найден: ${RESTORE_FROM}"; exit 1; }
    # Восстановление на работающем сервере затрёт базу под ним, и он
    # продолжит писать в файл, которого больше нет. Останавливаем сами: это
    # не тот случай, где предупреждения достаточно.
    warn "Служба будет остановлена на время восстановления."
    bash "${BASH_SOURCE[0]}" stop --data "${DATA_DIR}" >/dev/null 2>&1 || true
    py="$(asrhub_python)" || { error "Не найден интерпретатор ASR Hub."; exit 1; }
    ASRHUB_DATA_DIR="${DATA_DIR}" ASRHUB_RESTORE_FROM="${RESTORE_FROM}"       "${py}" - <<'PYCODE' || exit 1
import os
from pathlib import Path

from asrhub.config import load
from asrhub.maintenance import restore

settings = load()
цель = Path(settings.paths.data) / "asrhub.db"
restore(Path(os.environ["ASRHUB_RESTORE_FROM"]), цель)
print(f"База восстановлена: {цель}")
print("Прежняя база сохранена рядом с пометкой before-restore — "
      "удалите её, когда убедитесь, что всё на месте.")
PYCODE
    ok "Восстановление завершено. Запустите службу: bash scripts/service.sh start" ;;

  *)
    cat <<'USAGE'
Управление службой ASR Hub

  bash scripts/service.sh install [--prefix ПУТЬ] [--data ПУТЬ] [--port N] [--user ИМЯ]
  bash scripts/service.sh start | stop | restart | status
  bash scripts/service.sh logs [-n 200] [-f]
  bash scripts/service.sh backup
  bash scripts/service.sh restore --from ПУТЬ_К_КОПИИ
  bash scripts/service.sh uninstall

Linux — systemd (системная или пользовательская служба), macOS — launchd.

Резервная копия снимается командой SQLite «.backup»: обычное копирование
файла базы на работающем сервере даёт несогласованный результат. Сервер
умеет делать копии и сам — настройка backup_interval_hours.
USAGE
    exit 2 ;;
esac
