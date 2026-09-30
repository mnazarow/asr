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
SERVICE_NAME=""
PURGE=0
KEEP_MODELS=0
MODELS_TO=""
MODE="auto"

usage() {
  cat <<'USAGE'
Удаление ASR Hub

Использование: bash scripts/uninstall.sh [параметры]

  --prefix ПУТЬ    Каталог установки (определяется автоматически)
  --data ПУТЬ      Каталог данных (определяется автоматически)
  --name ИМЯ       Имя службы этой установки (по умолчанию определяется по каталогу)
  --purge          Удалить и данные: результаты, модели, базу, журналы
  --keep-models    При --purge сохранить загруженные веса моделей
  --models-to ПУТЬ Куда перенести модели при --keep-models
                   (по умолчанию ~/asrhub-models-ДАТА)
  --mode native|docker   Что удалять (по умолчанию определяется)
  --yes            Не задавать вопросов
  --dry-run        Показать, что будет удалено
  -h, --help       Справка

Установка определяется по месту запуска: команда из «Что дальше» каталога
/opt/asrhub2 удаляет именно /opt/asrhub2, её данные и её службу. Запуск из
распакованного дистрибутива на машине с несколькими установками требует
--prefix.

После остановки службы делается резервная копия конфигурации, ключа и базы
заданий (командой копирования SQLite — вместе с журналом WAL).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --name)   SERVICE_NAME="${2:?}"; shift 2 ;;
    --mode)   MODE="${2:?}"; shift 2 ;;
    --purge)  PURGE=1; shift ;;
    --keep-models) KEEP_MODELS=1; shift ;;
    --models-to) MODELS_TO="${2:?}"; shift 2 ;;
    --yes|-y) ASRHUB_ASSUME_YES=1; shift ;;
    --dry-run) ASRHUB_DRY_RUN=1; shift ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) error "Неизвестный параметр: $1"; exit 2 ;;
  esac
done

# shellcheck disable=SC2034  # читается в common.sh при печати итога
ASRHUB_CHECKLIST_TITLE="Чек-лист удаления"
enable_error_handling
setup_logging "${TMPDIR:-/tmp}"
print_banner

OS="$(detect_os)"

# Какую установку удалять. Раньше — первую из списка стандартных каталогов,
# и команда удаления из «Что дальше» второй установки
# (`bash /opt/asrhub2/scripts/uninstall.sh --purge`) удаляла ПЕРВУЮ:
# /opt/asrhub, /var/lib/asrhub с базой и моделями и службу asrhub, а вторая
# оставалась. Теперь установка — та, из которой запущен сценарий; каталог
# данных и служба — из её метки или её службы.
resolve_installation "${SCRIPT_DIR}"
if [[ -n "${ASRHUB_INSTALLATIONS}" ]]; then
  explain_ambiguous_installation
  exit 2
fi

if [[ -z "${PREFIX}" && -z "${DATA_DIR}" ]]; then
  warn "Установка ASR Hub не найдена."
  hint "Укажите пути явно: --prefix и --data"
  exit 0
fi

# Каталог данных взят по стандартному пути, а ни метка установки, ни её
# служба его не подтверждают: это могут быть данные ДРУГОЙ установки.
# Удалять их с --purge вслепую нельзя.
if [[ "${PURGE}" -eq 1 && "${ASRHUB_DATA_DIR_SOURCE:-}" == "guess" && -n "${DATA_DIR}" ]]; then
  warn "Каталог данных ${DATA_DIR} найден по стандартному пути,"
  warn "но ни метка установки, ни её служба его не подтверждают."
  hint "Если это данные другой установки, --purge удалит и их."
  if [[ "${ASRHUB_ASSUME_YES}" == "1" ]]; then
    error "С --yes каталог данных для --purge указывается явно: --data '${DATA_DIR}'"
    exit 2
  fi
  confirm "Удалять именно ${DATA_DIR}?" "n" || { info "Отменено."; exit 0; }
fi

# Два удаления (или удаление и обновление) одновременно портят друг другу
# снимки и каталоги — тот же замок, что у установщика.
if [[ -n "${PREFIX}" && -d "${PREFIX}" ]]; then
  acquire_install_lock "${PREFIX}" || exit 1
fi

# Процессы ЭТОЙ установки: строка запуска называет её окружение или рабочий
# каталог — её server. Прежний запасной поиск по ASRHUB_DATA_DIR ловил и
# сервер соседней установки над тем же каталогом данных (так делают, см.
# «Общий каталог данных» в главе установки): сосед получал kill, а через две
# секунды kill -9 — даже без --purge.
own_server_pids() {
  local pid cmd cwd
  [[ -n "${PREFIX}" ]] && have pgrep || return 0
  for pid in $(pgrep -f -- '-m asrhub|/uvicorn' 2>/dev/null || true); do
    [[ "${pid}" == "$$" ]] && continue
    cmd="$(process_command "${pid}")"
    cwd="$(readlink "/proc/${pid}/cwd" 2>/dev/null || true)"
    if [[ "${cmd}" == *"${PREFIX}/venv/"* || "${cwd}" == "${PREFIX}/server" ]]; then
      printf '%s\n' "${pid}"
    fi
  done
}

# Серверы других установок этой машины над тем же каталогом данных.
# other_instances видит только чужие машины: своя — «своя» по имени хоста.
local_neighbours() {
  local pid own=" $(own_server_pids | tr '\n' ' ') "
  [[ -n "${DATA_DIR}" ]] && have pgrep || return 0
  for pid in $(pgrep -f -- '-m asrhub' 2>/dev/null || true); do
    [[ "${own}" == *" ${pid} "* ]] && continue
    if tr '\0' '\n' < "/proc/${pid}/environ" 2>/dev/null \
       | grep -qxF "ASRHUB_DATA_DIR=${DATA_DIR}"; then
      printf '%s ' "${pid}"
    fi
  done
}

# Каталог данных может быть общим: над ним работают другие машины. Удаление
# с --purge унесло бы их базу и результаты вместе с нашими — молча.
if [[ "${PURGE}" -eq 1 ]]; then
  OTHERS="$(other_instances "${DATA_DIR}" || true)"
  NEIGHBOURS="$(local_neighbours || true)"
  [[ -n "${NEIGHBOURS}" ]] && OTHERS="${OTHERS:+${OTHERS}, }процессы этой машины: ${NEIGHBOURS% }"
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
if [[ -n "${SERVICE_NAME}" ]]; then
  printf '  Служба           %s — автозапуск будет удалён\n' "${SERVICE_NAME}"
else
  printf '  Служба           у этой установки её нет\n'
fi
printf '\n'

confirm "Продолжить удаление?" "n" || { info "Отменено."; exit 0; }

# --- 1. Остановка службы ----------------------------------------------------

# Сначала остановка, потом копия базы: копия работающей базы в режиме WAL
# теряет то, что лежит только в журнале, а удаление с --purge тут же стирало
# и журнал.
step "Остановка службы"
if [[ -n "${SERVICE_NAME}" ]]; then
  bash "${SCRIPT_DIR}/service.sh" uninstall --prefix "${PREFIX}" \
    --name "${SERVICE_NAME}" 2>/dev/null || \
    info "Служба не найдена или уже удалена."
else
  info "Службы автозапуска у этой установки нет."
fi

# Останавливаем процессы ЭТОЙ установки, запущенные вручную.
PIDS="$(own_server_pids | tr '\n' ' ' || true)"
PIDS="${PIDS% }"
if [[ -n "${PIDS}" ]]; then
  info "Останавливаем процессы: ${PIDS}"
  for pid in ${PIDS}; do run kill "${pid}" 2>/dev/null || true; done
  [[ "${ASRHUB_DRY_RUN}" == "1" ]] || sleep 2
  for pid in ${PIDS}; do
    kill -0 "${pid}" 2>/dev/null && { run kill -9 "${pid}" 2>/dev/null || true; }
  done
fi
ok "Служба остановлена"

# --- 2. Резервная копия -----------------------------------------------------

step "Резервная копия важных файлов"
BACKUP_DIR="${HOME}/asrhub-backup-$(date +%Y%m%d-%H%M%S)"
if [[ -n "${DATA_DIR}" && -d "${DATA_DIR}" ]]; then
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] копия config.yaml, api-key.txt и базы в %s\n' "${BACKUP_DIR}"
  else
    # В копии ключ доступа и токен: читать её — только владельцу.
    ( umask 077; mkdir -p "${BACKUP_DIR}" )
    for item in config.yaml api-key.txt; do
      [[ -f "${DATA_DIR}/${item}" ]] && cp -a "${DATA_DIR}/${item}" "${BACKUP_DIR}/" 2>/dev/null || true
    done
    if [[ -f "${DATA_DIR}/asrhub.db" ]]; then
      if sqlite_backup "${DATA_DIR}/asrhub.db" "${BACKUP_DIR}/asrhub.db" \
           "${PREFIX:+${PREFIX}/venv/bin/python}"; then
        ok "Копия базы: ${BACKUP_DIR}/asrhub.db"
      else
        error "Копию базы сделать не удалось."
        if [[ "${PURGE}" -eq 1 ]]; then
          hint "С --purge база будет удалена без копии."
          confirm "Удалять данные без копии базы?" "n" || { info "Отменено: данные не тронуты."; exit 1; }
        fi
      fi
    fi
    if [[ -n "$(ls -A "${BACKUP_DIR}" 2>/dev/null)" ]]; then
      ok "Резервная копия: ${BACKUP_DIR}"
    else
      rmdir "${BACKUP_DIR}" 2>/dev/null || true
      info "Нечего копировать."
    fi
  fi
fi

# --- 3. Docker --------------------------------------------------------------

# Признак установки в контейнере — метка установки или docker/.env, который
# пишет только такая установка. docker-compose.yml лежит в любой копии
# исходников, и по нему удаление обычной установки выполняло
# `docker compose down` — над чужим проектом с тем же именем каталога.
DOCKER_INSTALL=0
if [[ "${MODE}" == "docker" ]]; then
  DOCKER_INSTALL=1
elif [[ "${MODE}" == "auto" && -n "${PREFIX}" ]] \
     && { [[ "$(install_marker_value "${PREFIX}" mode || true)" == "docker" ]] \
          || [[ -f "${PREFIX}/docker/.env" ]]; }; then
  DOCKER_INSTALL=1
fi
if [[ "${DOCKER_INSTALL}" -eq 1 ]]; then
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
  else
    # Раньше здесь не говорилось ничего: удаление объявлялось законченным,
    # а контейнеры продолжали работать и держать порт.
    warn "Установка в контейнере, а docker не найден — контейнеры не остановлены."
    hint "Когда Docker будет доступен: docker rm -f asrhub asrhub-proxy"
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
  MODELS_KEEP="${MODELS_TO:-${HOME}/asrhub-models-$(date +%Y%m%d)}"
  # Перенос на другой раздел — это копирование десятков гигабайт: если
  # места не хватит, mv оборвётся на середине. Проверяем заранее.
  MODELS_KB="$(du -sk "${DATA_DIR}/models" 2>/dev/null | awk '{print $1}' || echo 0)"
  TARGET_PARENT="$(dirname "${MODELS_KEEP}")"
  if [[ "$(df -Pk "${DATA_DIR}/models" 2>/dev/null | awk 'NR==2{print $1}')" \
        != "$(df -Pk "${TARGET_PARENT}" 2>/dev/null | awk 'NR==2{print $1}')" ]]; then
    FREE_KB="$(df -Pk "${TARGET_PARENT}" 2>/dev/null | awk 'NR==2{print $4}' || echo 0)"
    if [[ "${MODELS_KB:-0}" =~ ^[0-9]+$ && "${FREE_KB:-0}" =~ ^[0-9]+$ ]] \
       && (( FREE_KB < MODELS_KB + 1048576 )); then
      error "Для моделей ($(human_size $((MODELS_KB * 1024)))) в ${TARGET_PARENT} не хватит места:"
      error "свободно $(human_size $((FREE_KB * 1024)))."
      hint "Укажите другой каталог: --models-to ПУТЬ"
      exit 1
    fi
  fi
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] mv %s %s\n' "${DATA_DIR}/models" "${MODELS_KEEP}"
  else
    mkdir -p "${TARGET_PARENT}" 2>/dev/null || true
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
# Снимков два вида: нынешний `<каталог>.snapshot` (с 3.1.3, у каждой
# установки свой) и прежний общий `asrhub-snapshot`. В нынешнем лежит
# config.yaml.snapshot — ключи доступа и токен Hugging Face, — и после
# `--purge` он оставался на диске, а скрипт писал «Остатков не найдено».
#
# Юнит и агент launchd — только своей службы: у второй установки своё имя,
# и удаление её остатков сносило юнит первой. Пути от каталога программы —
# только когда он известен: с пустым PREFIX «${PREFIX%/}.snapshot»
# превращался в «.snapshot» ТЕКУЩЕГО каталога, и его удаляли по вопросу с
# умолчанием «да».
CANDIDATES=(/etc/asrhub /usr/local/bin/asrctl)
if [[ -n "${SERVICE_NAME}" ]]; then
  CANDIDATES+=("${ASRHUB_SYSTEMD_DIR:-/etc/systemd/system}/${SERVICE_NAME}.service"
               "$(plist_path_for "${SERVICE_NAME}")")
fi
if [[ -n "${PREFIX}" ]]; then
  CANDIDATES+=("${PREFIX}/whisper.cpp" "${PREFIX%/}.snapshot"
               "$(dirname "${PREFIX}")/asrhub-snapshot")
fi
for path in "${CANDIDATES[@]}"; do
  [[ -e "${path}" ]] && LEFTOVERS+=("${path}")
done
if [[ ${#LEFTOVERS[@]} -gt 0 ]]; then
  warn "Найдены остатки:"
  for path in "${LEFTOVERS[@]}"; do printf '    %s\n' "${path}"; done
  if [[ -e "${PREFIX%/}.snapshot/config.yaml.snapshot" ]]; then
    hint "В снимке ${PREFIX%/}.snapshot лежит копия config.yaml — с ключами доступа и токеном."
  fi
  # Пробный запуск только перечисляет. Раньше удаление шло голым rm мимо
  # `run`, и `--dry-run --yes` по-настоящему сносил сборку whisper.cpp на
  # несколько гигабайт, снимок отката, настройки клиента и LaunchAgent.
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    info "Пробный запуск: остатки не удаляются."
  elif confirm "Удалить их?"; then
    for path in "${LEFTOVERS[@]}"; do
      if [[ "${path}" == /etc/* || "${path}" == /usr/* ]]; then
        as_root rm -rf "${path}" 2>/dev/null || warn "  не удалось: ${path}"
      else
        run rm -rf "${path}" 2>/dev/null || warn "  не удалось: ${path}"
      fi
    done
  fi
else
  ok "Остатков не найдено"
fi
# Профили клиента asrctl — не остаток сервера: клиент ходит и на другие
# серверы. Но в них лежат ключи, и о них стоит сказать. Прежний список
# остатков искал ~/.config/asrhub — каталога, которого клиент не заводит.
if [[ -f "${HOME}/.config/asrctl/profiles" ]]; then
  hint "Профили клиента asrctl с ключами доступа: ${HOME}/.config/asrctl/profiles"
  hint "удалите их, если клиент больше не нужен."
fi

# Системный пользователь службы, заведённый установщиком. С --purge
# удаляется и он — если его не использует другая служба.
if [[ "${PURGE}" -eq 1 && "${OS}" == "linux" ]] && is_root && id -u asrhub >/dev/null 2>&1 \
   && have userdel; then
  USED_BY=""
  while IFS=$'\t' read -r _name _prefix _ _unit; do
    [[ "${_prefix}" == "${PREFIX}" ]] && continue
    grep -qxF "User=asrhub" "${_unit}" 2>/dev/null && USED_BY="${USED_BY} ${_name}"
  done < <(asrhub_services)
  if [[ -n "${USED_BY}" ]]; then
    info "Пользователь asrhub оставлен: от него работает служба${USED_BY}."
  elif [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] userdel asrhub\n'
  elif confirm "Удалить системного пользователя asrhub?" "y"; then
    userdel asrhub 2>/dev/null && ok "Пользователь asrhub удалён" \
      || warn "Пользователя asrhub удалить не удалось."
  fi
fi

clear_rollback
if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
  printf '\n%s%sПробный запуск завершён — ничего не удалено.%s\n\n' \
    "${C_BOLD}" "${C_GREEN}" "${C_RESET}"
  exit 0
fi
printf '\n%s%sASR Hub удалён%s\n\n' "${C_BOLD}" "${C_GREEN}" "${C_RESET}"
[[ -d "${BACKUP_DIR}" ]] && printf '  Резервная копия конфигурации и базы: %s\n' "${BACKUP_DIR}"
[[ "${PURGE}" -eq 0 && -n "${DATA_DIR}" ]] && \
  printf '  Данные остались в %s — удалите вручную, если не нужны.\n' "${DATA_DIR}"
printf '\n'
