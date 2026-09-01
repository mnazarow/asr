#!/usr/bin/env bash
#
# Обновление ASR Hub.
#
#   bash scripts/update.sh                    обновить программу и зависимости
#   bash scripts/update.sh --engines-only     обновить только движки
#   bash scripts/update.sh --check            только проверить, есть ли обновления
#   bash scripts/update.sh --rollback         откатиться к предыдущей версии
#
# Перед обновлением создаётся снимок текущей установки; при неудачной
# проверке работоспособности выполняется автоматический откат.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

PREFIX=""
DATA_DIR=""
SOURCE_DIR="${REPO_DIR}"
ENGINES_ONLY=0
CHECK_ONLY=0
DO_ROLLBACK=0
NO_RESTART=0

usage() {
  cat <<'USAGE'
Обновление ASR Hub

Использование: bash scripts/update.sh [параметры]

  --prefix ПУТЬ     Каталог установки (определяется автоматически)
  --data ПУТЬ       Каталог данных
  --source ПУТЬ     Откуда брать новую версию (по умолчанию текущий репозиторий)
  --engines-only    Обновить только пакеты движков, не трогая код сервера
  --check           Только показать, что изменится
  --rollback        Вернуть предыдущую версию из снимка
  --no-restart      Не перезапускать службу после обновления
  --yes             Не задавать вопросов
  --dry-run         Показать план без изменений
  -h, --help        Справка
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --source) SOURCE_DIR="${2:?}"; shift 2 ;;
    --engines-only) ENGINES_ONLY=1; shift ;;
    --check)  CHECK_ONLY=1; shift ;;
    --rollback) DO_ROLLBACK=1; shift ;;
    --no-restart) NO_RESTART=1; shift ;;
    --yes|-y) ASRHUB_ASSUME_YES=1; shift ;;
    --dry-run) ASRHUB_DRY_RUN=1; shift ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    --debug) ASRHUB_DEBUG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) error "Неизвестный параметр: $1"; exit 2 ;;
  esac
done

enable_error_handling
setup_logging "${TMPDIR:-/tmp}"
print_banner

[[ -z "${PREFIX}" ]] && for c in "/opt/asrhub" "${HOME}/.local/share/asrhub-app" \
  "${HOME}/Library/Application Support/ASRHub"; do [[ -d "${c}" ]] && { PREFIX="${c}"; break; }; done
[[ -z "${DATA_DIR}" ]] && for c in "/var/lib/asrhub" "${HOME}/.local/share/asrhub" \
  "${HOME}/Library/Application Support/ASRHub/data"; do [[ -d "${c}" ]] && { DATA_DIR="${c}"; break; }; done

[[ -z "${PREFIX}" ]] && { error "Установка не найдена. Укажите --prefix."; exit 2; }

VENV="${PREFIX}/venv"
VPIP="${VENV}/bin/pip"
VPY="${VENV}/bin/python"
SNAPSHOT_DIR="${PREFIX}/../asrhub-snapshot"
CURRENT_VERSION="$(cat "${PREFIX}/VERSION" 2>/dev/null || echo 'неизвестна')"
NEW_VERSION="$(cat "${SOURCE_DIR}/VERSION" 2>/dev/null || echo 'неизвестна')"

# --- Откат ------------------------------------------------------------------

if [[ "${DO_ROLLBACK}" -eq 1 ]]; then
  heading "Откат к предыдущей версии"
  if [[ ! -d "${SNAPSHOT_DIR}" ]]; then
    error "Снимок предыдущей версии не найден: ${SNAPSHOT_DIR}"
    exit 2
  fi
  info "Снимок от $(date -r "${SNAPSHOT_DIR}" '+%Y-%m-%d %H:%M' 2>/dev/null || echo '?')"
  confirm "Восстановить предыдущую версию?" || exit 0
  bash "${SCRIPT_DIR}/service.sh" stop --prefix "${PREFIX}" 2>/dev/null || true
  # Только поверх: в снимке лежит код, а --delete снёс бы venv, а на macOS
  # ещё и каталог данных внутри prefix.
  run cp -a "${SNAPSHOT_DIR}/." "${PREFIX}/"
  bash "${SCRIPT_DIR}/service.sh" start --prefix "${PREFIX}" 2>/dev/null || true
  ok "Откат выполнен: версия $(cat "${PREFIX}/VERSION" 2>/dev/null || echo '?')"
  exit 0
fi

# --- Проверка ---------------------------------------------------------------

heading "Проверка обновления"
printf '  Установлено   %s\n' "${CURRENT_VERSION}"
printf '  Доступно      %s\n' "${NEW_VERSION}"
printf '  Источник      %s\n' "${SOURCE_DIR}"
printf '\n'

if [[ "${ENGINES_ONLY}" -eq 0 && ! -d "${SOURCE_DIR}/server" ]]; then
  error "В каталоге источника нет каталога server: ${SOURCE_DIR}"
  hint "Укажите путь к распакованному дистрибутиву: --source /путь/к/asr-hub"
  exit 2
fi

CHANGED=()
if [[ -d "${SOURCE_DIR}/server" && -d "${PREFIX}/server" ]]; then
  while IFS= read -r line; do CHANGED+=("${line}"); done < <(
    diff -rq "${PREFIX}/server" "${SOURCE_DIR}/server" 2>/dev/null | head -40 || true)
fi
if [[ ${#CHANGED[@]} -gt 0 ]]; then
  info "Изменённых файлов: ${#CHANGED[@]} (показаны первые)"
  printf '    %s\n' "${CHANGED[@]:0:12}"
else
  info "Различий в коде сервера не обнаружено."
fi

if [[ "${CHECK_ONLY}" -eq 1 ]]; then
  info "Режим проверки — изменения не вносились."
  exit 0
fi

confirm "Выполнить обновление?" || { info "Отменено."; exit 0; }

# --- Снимок -----------------------------------------------------------------

set_step_total 6
step "Снимок текущей версии"
if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
  rm -rf "${SNAPSHOT_DIR}"
  mkdir -p "${SNAPSHOT_DIR}"
  for item in server scripts config requirements docker VERSION; do
    [[ -e "${PREFIX}/${item}" ]] && cp -a "${PREFIX}/${item}" "${SNAPSHOT_DIR}/"
  done
  ok "Снимок: ${SNAPSHOT_DIR}"
  hint "Откат при проблемах: bash scripts/update.sh --rollback"
fi

# --- База данных ------------------------------------------------------------

step "Резервная копия базы"
if [[ -f "${DATA_DIR}/asrhub.db" && "${ASRHUB_DRY_RUN}" != "1" ]]; then
  DB_BACKUP="${DATA_DIR}/asrhub.db.bak.$(date +%Y%m%d-%H%M%S)"
  if have sqlite3; then
    run sqlite3 "${DATA_DIR}/asrhub.db" ".backup '${DB_BACKUP}'" || cp -a "${DATA_DIR}/asrhub.db" "${DB_BACKUP}"
  else
    cp -a "${DATA_DIR}/asrhub.db" "${DB_BACKUP}"
  fi
  ok "Копия базы: $(basename "${DB_BACKUP}") ($(human_size "$(stat -c%s "${DB_BACKUP}" 2>/dev/null || stat -f%z "${DB_BACKUP}" 2>/dev/null || echo 0)"))"
  # Держим не больше пяти копий
  ls -1t "${DATA_DIR}"/asrhub.db.bak.* 2>/dev/null | tail -n +6 | while read -r old_backup; do
    rm -f "${old_backup}"
  done
fi

# --- Режим установки --------------------------------------------------------

# Установка в контейнере обновляется иначе: файлы в образе, а не в системе,
# и без пересборки образа новая версия просто не попадала в работу — команда
# отрабатывала «успешно», а сервер продолжал крутить старый код.
DOCKER_MODE=0
if [[ -f "${PREFIX}/docker/docker-compose.yml" && ! -x "${VPIP}" ]] && have docker; then
  DOCKER_MODE=1
elif [[ -f "${PREFIX}/docker/.env" ]] && have docker \
     && docker ps --filter "name=^asrhub$" --format '{{.Names}}' 2>/dev/null | grep -q asrhub; then
  DOCKER_MODE=1
fi

compose_cmd() {
  local compose="docker compose"
  docker compose version >/dev/null 2>&1 || compose="docker-compose"
  local files=(-f docker-compose.yml)
  # Надстройку с видеокартой подключаем, если ей пользовались при установке.
  if grep -qE '^ASRHUB_ACCEL=cuda' "${PREFIX}/docker/.env" 2>/dev/null \
     && [[ -f "${PREFIX}/docker/docker-compose.gpu.yml" ]]; then
    files+=(-f docker-compose.gpu.yml)
  fi
  printf '%s ' "${compose}" "${files[@]}"
}

# --- Остановка --------------------------------------------------------------

step "Остановка службы"
WAS_RUNNING=0
if [[ "${DOCKER_MODE}" -eq 1 ]]; then
  if docker ps --filter "name=^asrhub$" --format '{{.Names}}' 2>/dev/null | grep -q asrhub; then
    WAS_RUNNING=1
  fi
  info "Установка в контейнере: остановка произойдёт при пересборке."
  ok "Готово"
else
  if bash "${SCRIPT_DIR}/service.sh" status --prefix "${PREFIX}" >/dev/null 2>&1; then
    WAS_RUNNING=1
    bash "${SCRIPT_DIR}/service.sh" stop --prefix "${PREFIX}" || warn "Не удалось остановить службу."
  fi
  ok "Служба остановлена"
fi

# --- Файлы ------------------------------------------------------------------

if [[ "${ENGINES_ONLY}" -eq 0 ]]; then
  step "Обновление файлов"
  if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
    for item in server scripts config requirements docker VERSION README.md; do
      [[ -e "${SOURCE_DIR}/${item}" ]] || continue
      rm -rf "${PREFIX:?}/${item}"
      cp -a "${SOURCE_DIR}/${item}" "${PREFIX}/"
    done
    chmod +x "${PREFIX}"/scripts/*.sh 2>/dev/null || true
  fi
  ok "Файлы обновлены до версии ${NEW_VERSION}"
fi

# --- Зависимости ------------------------------------------------------------

step "Обновление зависимостей"
if [[ "${DOCKER_MODE}" -eq 1 ]]; then
  info "Зависимости живут в образе — обновятся при пересборке."
elif [[ -x "${VPIP}" ]]; then
  PIP_FLAGS=(--disable-pip-version-check --no-input --upgrade)
  retry 2 run "${VPIP}" install "${PIP_FLAGS[@]}" -r "${PREFIX}/requirements/base.txt" || \
    warn "Часть базовых зависимостей не обновилась."
  for req in "${PREFIX}"/requirements/engines/*.txt; do
    engine="$(basename "${req}" .txt)"
    module="$(printf '%s' "${engine}" | tr '-' '_')"
    if "${VPY}" -c "import ${module}" >/dev/null 2>&1; then
      info "Движок ${engine} установлен — обновляем"
      retry 2 run "${VPIP}" install "${PIP_FLAGS[@]}" -r "${req}" || \
        warn "  ${engine}: обновление не удалось, остаётся прежняя версия"
    fi
  done
  ok "Зависимости обновлены"
else
  warn "Виртуальное окружение не найдено — зависимости не обновлялись."
fi

# --- Запуск и проверка ------------------------------------------------------

step "Запуск и проверка"
if [[ "${NO_RESTART}" -eq 1 ]]; then
  info "Перезапуск пропущен (--no-restart)."
  exit 0
fi

if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
  ok "Пробный запуск завершён."
  exit 0
fi

if [[ "${DOCKER_MODE}" -eq 1 ]]; then
  COMPOSE_LINE="$(compose_cmd)"
  info "Пересборка образа (может занять несколько минут)…"
  # shellcheck disable=SC2086
  ( cd "${PREFIX}/docker" && retry 2 run ${COMPOSE_LINE} --env-file .env build ) || {
    error "Пересборка образа не удалась."
    hint "Журнал сборки выше; прежний контейнер не тронут."
    exit 1
  }
  # shellcheck disable=SC2086
  ( cd "${PREFIX}/docker" && run ${COMPOSE_LINE} --env-file .env up -d )
elif [[ "${WAS_RUNNING}" -eq 1 ]]; then
  bash "${SCRIPT_DIR}/service.sh" start --prefix "${PREFIX}" || true
else
  info "До обновления служба не работала — не запускаем."
fi

if [[ "${DOCKER_MODE}" -eq 1 ]]; then
  PORT="$(grep -E '^ASRHUB_PORT=' "${PREFIX}/docker/.env" 2>/dev/null | cut -d= -f2 | head -1)"
else
  PORT="$(grep -E '^[[:space:]]*server_port:' "${DATA_DIR}/config.yaml" 2>/dev/null | awk '{print $2}' | head -1)"
fi
PORT="${PORT:-8080}"
HEALTH_OK=0
for _ in $(seq 1 20); do
  if have curl && curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    HEALTH_OK=1; break
  fi
  sleep 2
done

if [[ "${HEALTH_OK}" -eq 1 ]]; then
  ok "Сервер отвечает — обновление успешно"
  printf '\n%s%sОбновление завершено: %s → %s%s\n\n' "${C_BOLD}" "${C_GREEN}" \
    "${CURRENT_VERSION}" "${NEW_VERSION}" "${C_RESET}"
  hint "Откат при необходимости: bash ${PREFIX}/scripts/update.sh --rollback"
else
  error "Сервер не отвечает после обновления."
  if confirm "Откатиться к предыдущей версии?"; then
    exec bash "${SCRIPT_DIR}/update.sh" --rollback --prefix "${PREFIX}" --yes
  fi
  if [[ "${DOCKER_MODE}" -eq 1 ]]; then
    hint "Журнал контейнера: cd ${PREFIX}/docker && docker compose logs --tail 100"
  else
    hint "Журнал службы: bash ${PREFIX}/scripts/service.sh logs"
  fi
  exit 1
fi
