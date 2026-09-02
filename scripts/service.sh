#!/usr/bin/env bash
#
# Управление службой ASR Hub: systemd на Linux, launchd на macOS.
#
#   bash scripts/service.sh install --prefix /opt/asrhub --data /var/lib/asrhub
#   bash scripts/service.sh {start|stop|restart|status|logs|uninstall}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

ACTION="${1:-status}"; shift || true

PREFIX="/opt/asrhub"
DATA_DIR="/var/lib/asrhub"
PORT="8080"
HOST="0.0.0.0"
SERVICE_USER=""
SERVICE_NAME="asrhub"
FOLLOW=0
LINES=100

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
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    --dry-run) ASRHUB_DRY_RUN=1; shift ;;
    *) shift ;;
  esac
done

OS="$(detect_os)"
PLIST="${HOME}/Library/LaunchAgents/com.asrhub.server.plist"
UNIT="/etc/systemd/system/${SERVICE_NAME}.service"
USER_UNIT="${HOME}/.config/systemd/user/${SERVICE_NAME}.service"

use_user_systemd() { [[ ! -w /etc/systemd/system ]] && ! is_root; }

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
  content="$(cat <<UNITEOF
[Unit]
Description=ASR Hub — сервер распознавания речи
Documentation=file://${PREFIX}/docs/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
${run_user:+User=${run_user}}
WorkingDirectory=${PREFIX}/server
Environment="ASRHUB_DATA_DIR=${DATA_DIR}"
Environment="PYTHONUNBUFFERED=1"
Environment="HF_HOME=${DATA_DIR}/models"
EnvironmentFile=-${DATA_DIR}/env.sh
ExecStart=${PREFIX}/venv/bin/python -m asrhub --host ${HOST} --port ${PORT}
ExecReload=/bin/kill -HUP \$MAINPID
Restart=on-failure
RestartSec=10
StartLimitBurst=5
StartLimitIntervalSec=120
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
ReadWritePaths=${DATA_DIR}
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
    ok "Выполнено: ${ACTION}" ;;

  status)
    if [[ "${OS}" == "macos" ]]; then
      if launchctl list 2>/dev/null | grep -q com.asrhub.server; then
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

  *)
    cat <<'USAGE'
Управление службой ASR Hub

  bash scripts/service.sh install [--prefix ПУТЬ] [--data ПУТЬ] [--port N] [--user ИМЯ]
  bash scripts/service.sh start | stop | restart | status
  bash scripts/service.sh logs [-n 200] [-f]
  bash scripts/service.sh uninstall

Linux — systemd (системная или пользовательская служба), macOS — launchd.
USAGE
    exit 2 ;;
esac
