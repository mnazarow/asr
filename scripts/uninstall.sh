#!/usr/bin/env bash
#
# Удаление ASR Hub.
#
#   bash scripts/uninstall.sh                 удалить программу, данные оставить
#   bash scripts/uninstall.sh --purge         удалить всё, включая данные и модели
#   bash scripts/uninstall.sh --keep-models   удалить всё, кроме весов моделей
#
# По умолчанию данные сохраняются: результаты распознавания и загруженные
# модели часто ценнее самой программы.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

PREFIX=""
DATA_DIR=""
PURGE=0
KEEP_MODELS=0
MODE="auto"

usage() {
  cat <<'USAGE'
Удаление ASR Hub

Использование: bash scripts/uninstall.sh [параметры]

  --prefix ПУТЬ    Каталог установки (определяется автоматически)
  --data ПУТЬ      Каталог данных (определяется автоматически)
  --purge          Удалить и данные: результаты, модели, базу, журналы
  --keep-models    При --purge сохранить загруженные веса моделей
  --mode native|docker   Что удалять (по умолчанию определяется)
  --yes            Не задавать вопросов
  --dry-run        Показать, что будет удалено
  -h, --help       Справка

Перед удалением создаётся резервная копия конфигурации и базы заданий.
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --mode)   MODE="${2:?}"; shift 2 ;;
    --purge)  PURGE=1; shift ;;
    --keep-models) KEEP_MODELS=1; shift ;;
    --yes|-y) ASRHUB_ASSUME_YES=1; shift ;;
    --dry-run) ASRHUB_DRY_RUN=1; shift ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) error "Неизвестный параметр: $1"; exit 2 ;;
  esac
done

enable_error_handling
setup_logging "${TMPDIR:-/tmp}"
print_banner

OS="$(detect_os)"

# Автоопределение путей установки
if [[ -z "${PREFIX}" ]]; then
  for candidate in "/opt/asrhub" "${HOME}/.local/share/asrhub-app" \
                   "${HOME}/Library/Application Support/ASRHub"; do
    [[ -d "${candidate}" ]] && { PREFIX="${candidate}"; break; }
  done
fi
if [[ -z "${DATA_DIR}" ]]; then
  for candidate in "/var/lib/asrhub" "${HOME}/.local/share/asrhub" \
                   "${HOME}/Library/Application Support/ASRHub/data"; do
    [[ -d "${candidate}" ]] && { DATA_DIR="${candidate}"; break; }
  done
fi

if [[ -z "${PREFIX}" && -z "${DATA_DIR}" ]]; then
  warn "Установка ASR Hub не найдена."
  hint "Укажите пути явно: --prefix и --data"
  exit 0
fi

heading "Что будет удалено"
[[ -n "${PREFIX}" ]] && printf '  Программа        %s (%s)\n' "${PREFIX}" \
  "$(human_size "$(du -sb "${PREFIX}" 2>/dev/null | awk '{print $1}' || echo 0)")"
if [[ "${PURGE}" -eq 1 ]]; then
  printf '  Данные           %s (%s)%s\n' "${DATA_DIR}" \
    "$(human_size "$(du -sb "${DATA_DIR}" 2>/dev/null | awk '{print $1}' || echo 0)")" \
    "$( [[ "${KEEP_MODELS}" -eq 1 ]] && echo ' — модели будут сохранены' )"
else
  printf '  Данные           %s %sсохраняются%s\n' "${DATA_DIR}" "${C_GREEN}" "${C_RESET}"
fi
printf '  Служба           автозапуск будет удалён\n'
printf '\n'

confirm "Продолжить удаление?" "n" || { info "Отменено."; exit 0; }

# --- 1. Резервная копия -----------------------------------------------------

step "Резервная копия важных файлов"
BACKUP_DIR="${HOME}/asrhub-backup-$(date +%Y%m%d-%H%M%S)"
if [[ -n "${DATA_DIR}" && -d "${DATA_DIR}" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
  mkdir -p "${BACKUP_DIR}"
  for item in config.yaml api-key.txt asrhub.db; do
    [[ -f "${DATA_DIR}/${item}" ]] && cp -a "${DATA_DIR}/${item}" "${BACKUP_DIR}/" 2>/dev/null || true
  done
  if [[ -n "$(ls -A "${BACKUP_DIR}" 2>/dev/null)" ]]; then
    ok "Резервная копия: ${BACKUP_DIR}"
  else
    rmdir "${BACKUP_DIR}" 2>/dev/null || true
    info "Нечего копировать."
  fi
fi

# --- 2. Остановка службы ----------------------------------------------------

step "Остановка службы"
bash "${SCRIPT_DIR}/service.sh" uninstall --prefix "${PREFIX}" 2>/dev/null || \
  info "Служба не найдена или уже удалена."

# Останавливаем возможные процессы, запущенные вручную
if have pgrep; then
  PIDS="$(pgrep -f "python.*-m asrhub|uvicorn.*asrhub" 2>/dev/null | grep -v "^$$\$" || true)"
  if [[ -n "${PIDS}" ]]; then
    info "Останавливаем процессы: ${PIDS}"
    for pid in ${PIDS}; do run kill "${pid}" 2>/dev/null || true; done
    sleep 2
    for pid in ${PIDS}; do kill -0 "${pid}" 2>/dev/null && run kill -9 "${pid}" 2>/dev/null || true; done
  fi
fi
ok "Служба остановлена"

# --- 3. Docker --------------------------------------------------------------

if [[ "${MODE}" == "docker" || ( "${MODE}" == "auto" && -f "${PREFIX}/docker/docker-compose.yml" ) ]]; then
  step "Удаление контейнеров"
  if have docker; then
    COMPOSE="docker compose"
    docker compose version >/dev/null 2>&1 || COMPOSE="docker-compose"
    ( cd "${PREFIX}/docker" 2>/dev/null && run ${COMPOSE} down --remove-orphans ) || \
      info "Контейнеры не найдены."
    if confirm "Удалить образ asrhub?" "n"; then
      # Тегов может быть несколько: latest — процессорная сборка, cuda — под
      # видеокарту. Раньше удалялся только latest, и образ на 8 ГБ оставался
      # лежать после «полного» удаления.
      for tag in $(docker image ls --format '{{.Repository}}:{{.Tag}}' 2>/dev/null \
                   | grep '^asrhub:' || true); do
        run docker image rm "${tag}" 2>/dev/null || true
      done
    fi
    ok "Контейнеры удалены"
  fi
fi

# --- 4. Удаление файлов -----------------------------------------------------

step "Удаление файлов"

# На macOS и при установке от пользователя каталог данных лежит ВНУТРИ
# prefix. Снести prefix целиком означало бы удалить модели, базу и
# результаты, о сохранности которых мы тут же отчитываемся ниже.
if [[ -n "${PREFIX}" && -d "${PREFIX}" ]]; then
  if [[ "${PURGE}" -eq 0 && "${DATA_DIR}" == "${PREFIX}"/* ]]; then
    info "Каталог данных находится внутри каталога программы — удаляем выборочно."
    for sub in server scripts config requirements docker venv VERSION README.md; do
      if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
        printf '  [пробный запуск] rm -rf %s\n' "${PREFIX}/${sub}"
      else
        rm -rf "${PREFIX:?}/${sub}"
      fi
    done
    [[ "${ASRHUB_DRY_RUN}" == "1" ]] || ok "Программа удалена, данные оставлены: ${DATA_DIR}"
  elif [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] rm -rf %s\n' "${PREFIX}"
  else
    rm -rf "${PREFIX:?}"
    ok "Удалено: ${PREFIX}"
  fi
fi

if [[ "${PURGE}" -eq 1 && -n "${DATA_DIR}" && -d "${DATA_DIR}" ]]; then
  if [[ "${KEEP_MODELS}" -eq 1 && -d "${DATA_DIR}/models" ]]; then
    MODELS_KEEP="${HOME}/asrhub-models-$(date +%Y%m%d)"
    if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
      mv "${DATA_DIR}/models" "${MODELS_KEEP}"
      ok "Модели перенесены в ${MODELS_KEEP}"
    fi
  fi
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] rm -rf %s\n' "${DATA_DIR}"
  else
    rm -rf "${DATA_DIR:?}"
    ok "Удалено: ${DATA_DIR}"
  fi
else
  [[ -n "${DATA_DIR}" ]] && info "Данные сохранены: ${DATA_DIR}"
fi

# --- 5. Остатки -------------------------------------------------------------

step "Проверка остатков"
LEFTOVERS=()
for path in /etc/asrhub /usr/local/bin/asrctl "${HOME}/.config/asrhub" \
            /etc/systemd/system/asrhub.service "${HOME}/Library/LaunchAgents/com.asrhub.server.plist"; do
  [[ -e "${path}" ]] && LEFTOVERS+=("${path}")
done
if [[ ${#LEFTOVERS[@]} -gt 0 ]]; then
  warn "Найдены остатки:"
  for path in "${LEFTOVERS[@]}"; do printf '    %s\n' "${path}"; done
  if confirm "Удалить их?"; then
    for path in "${LEFTOVERS[@]}"; do
      if [[ "${path}" == /etc/* || "${path}" == /usr/* ]]; then
        as_root rm -rf "${path}" 2>/dev/null || warn "  не удалось: ${path}"
      else
        rm -rf "${path}" 2>/dev/null || warn "  не удалось: ${path}"
      fi
    done
  fi
else
  ok "Остатков не найдено"
fi

clear_rollback
printf '\n%s%sASR Hub удалён%s\n\n' "${C_BOLD}" "${C_GREEN}" "${C_RESET}"
[[ -d "${BACKUP_DIR}" ]] && printf '  Резервная копия конфигурации и базы: %s\n' "${BACKUP_DIR}"
[[ "${PURGE}" -eq 0 && -n "${DATA_DIR}" ]] && \
  printf '  Данные остались в %s — удалите вручную, если не нужны.\n' "${DATA_DIR}"
printf '\n'
