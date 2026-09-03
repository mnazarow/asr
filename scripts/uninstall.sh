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

# Каталог данных может быть общим: над ним работают другие машины. Удаление
# с --purge унесло бы их базу и результаты вместе с нашими — молча.
if [[ "${PURGE}" -eq 1 ]]; then
  OTHERS="$(other_instances "${DATA_DIR}" || true)"
  if [[ -n "${OTHERS}" ]]; then
    warn "Над каталогом данных прямо сейчас работают другие серверы: ${OTHERS}"
    hint "С ключом --purge вы удалите и их базу, задания и результаты."
    hint "Если нужно убрать только эту установку — запустите без --purge."
    confirm "Всё равно удалить общий каталог данных?" "n" || { info "Отменено."; exit 0; }
  fi
fi

heading "Что будет удалено"
# du -sb — расширение GNU: на macOS ключа нет, и размер всегда выходил «0 Б».
# Запасной путь через `du -sk` есть везде, включая BSD.
dir_bytes() {
  local path="$1" value
  value="$(du -sb "${path}" 2>/dev/null | awk '{print $1}')" || value=""
  if [[ -z "${value}" ]]; then
    value="$(du -sk "${path}" 2>/dev/null | awk '{print $1 * 1024}')" || value=0
  fi
  printf '%s' "${value:-0}"
}

[[ -n "${PREFIX}" ]] && printf '  Программа        %s (%s)\n' "${PREFIX}" \
  "$(human_size "$(dir_bytes "${PREFIX}")")"
if [[ "${PURGE}" -eq 1 ]]; then
  printf '  Данные           %s (%s)%s\n' "${DATA_DIR}" \
    "$(human_size "$(dir_bytes "${DATA_DIR}")")" \
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

# Останавливаем процессы ЭТОЙ установки, запущенные вручную.
# Прежний шаблон ловил всё, где встречается «asrhub»: на машине с двумя
# установками (а с общим каталогом данных их и делают несколько) удаление
# одной убивало сервер второй, на другом порту и из другого каталога.
# Заодно под kill попадала любая посторонняя оболочка, у которой эти слова
# оказались в командной строке. Поэтому сверяем каталог программы.
if have pgrep; then
  PIDS="$(pgrep -f "${PREFIX}/venv/bin/python.*-m asrhub|${PREFIX}/venv/bin/uvicorn" \
          2>/dev/null | grep -v "^$$\$" || true)"
  if [[ -z "${PIDS}" ]] && have pgrep; then
    # Запасной путь: процесс мог быть запущен не из venv установки. Тогда
    # опознаём его по каталогу данных в окружении — он у каждой установки свой.
    for pid in $(pgrep -f "\-m asrhub" 2>/dev/null || true); do
      if tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null \
         | grep -qxF "ASRHUB_DATA_DIR=${DATA_DIR}"; then
        PIDS="${PIDS} ${pid}"
      fi
    done
    PIDS="$(printf '%s' "${PIDS}" | tr ' ' '\n' | grep -v '^$' || true)"
  fi
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

# Модели спасаем ДО удаления чего-либо. Раньше перенос стоял после
# `rm -rf "${PREFIX}"`, а на macOS каталог данных лежит внутри prefix: при
# `--purge --keep-models` модели удалялись вместе с ним, и к моменту
# переноса каталога уже не было — скрипт дважды отчитывался об успехе, а
# весов на диске не оставалось.
MODELS_KEEP=""
if [[ "${PURGE}" -eq 1 && "${KEEP_MODELS}" -eq 1 && -d "${DATA_DIR}/models" ]]; then
  MODELS_KEEP="${HOME}/asrhub-models-$(date +%Y%m%d)"
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] mv %s %s\n' "${DATA_DIR}/models" "${MODELS_KEEP}"
  else
    if mv "${DATA_DIR}/models" "${MODELS_KEEP}"; then
      ok "Модели перенесены в ${MODELS_KEEP}"
    else
      error "Не удалось перенести модели в ${MODELS_KEEP}."
      error "Удаление остановлено, чтобы не потерять веса."
      exit 1
    fi
  fi
fi

# На macOS и при установке от пользователя каталог данных лежит ВНУТРИ
# prefix. Снести prefix целиком означало бы удалить модели, базу и
# результаты, о сохранности которых мы тут же отчитываемся ниже.
if [[ -n "${PREFIX}" && -d "${PREFIX}" ]]; then
  if [[ "${PURGE}" -eq 0 && "${DATA_DIR}" == "${PREFIX}"/* ]]; then
    info "Каталог данных находится внутри каталога программы — удаляем выборочно."
    # whisper.cpp собирается внутри каталога программы и вместе с каталогом
    # build занимает несколько гигабайт. В списке его не было, и после
    # «полного удаления» он оставался на диске.
    for sub in server scripts config requirements docker venv whisper.cpp VERSION README.md; do
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
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] rm -rf %s\n' "${DATA_DIR}"
  else
    rm -rf "${DATA_DIR:?}"
    ok "Удалено: ${DATA_DIR}"
  fi
elif [[ "${PURGE}" -eq 1 ]]; then
  [[ -n "${MODELS_KEEP}" ]] && info "Модели: ${MODELS_KEEP}"
elif [[ -n "${DATA_DIR}" && -d "${DATA_DIR}" ]]; then
  info "Данные сохранены: ${DATA_DIR}"
elif [[ -n "${DATA_DIR}" ]]; then
  # Каталога уже нет — говорить «сохранены» было бы неправдой.
  info "Каталог данных ${DATA_DIR} не найден."
fi

# --- 5. Остатки -------------------------------------------------------------

step "Проверка остатков"
LEFTOVERS=()
# Снимок перед обновлением кладётся рядом с каталогом программы и весит
# столько же, сколько сама установка: без него список остатков врал.
for path in /etc/asrhub /usr/local/bin/asrctl "${HOME}/.config/asrhub" \
            /etc/systemd/system/asrhub.service "${HOME}/Library/LaunchAgents/com.asrhub.server.plist" \
            "${PREFIX}/whisper.cpp" "$(dirname "${PREFIX}")/asrhub-snapshot"; do
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
