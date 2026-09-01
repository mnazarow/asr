#!/usr/bin/env bash
#
# Диагностика установки ASR Hub.
#
#   bash scripts/doctor.sh              полная проверка
#   bash scripts/doctor.sh --fix        попытаться исправить найденное
#   bash scripts/doctor.sh --hardware   только оборудование
#
# Каждая проверка выводит состояние и, если что-то не так, конкретную
# команду для исправления.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

set +o errexit    # диагностика не должна прерываться на первой проблеме

PREFIX=""
DATA_DIR=""
FIX=0
ONLY=""
PASSED=0
WARNED=0
FAILED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --fix)    FIX=1; shift ;;
    --hardware) ONLY="hardware"; shift ;;
    --engines)  ONLY="engines"; shift ;;
    --network)  ONLY="network"; shift ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    -h|--help)
      cat <<'USAGE'
Диагностика ASR Hub

  bash scripts/doctor.sh [--fix] [--hardware|--engines|--network]
                         [--prefix ПУТЬ] [--data ПУТЬ]

  --fix        Пытаться устранить найденные проблемы
  --hardware   Только оборудование и драйверы
  --engines    Только движки распознавания
  --network    Только сеть и доступность репозиториев
USAGE
      exit 0 ;;
    *) shift ;;
  esac
done

[[ -z "${PREFIX}" ]] && for c in "/opt/asrhub" "${HOME}/.local/share/asrhub-app" \
  "${HOME}/Library/Application Support/ASRHub"; do [[ -d "${c}" ]] && { PREFIX="${c}"; break; }; done
[[ -z "${DATA_DIR}" ]] && for c in "/var/lib/asrhub" "${HOME}/.local/share/asrhub" \
  "${HOME}/Library/Application Support/ASRHub/data"; do [[ -d "${c}" ]] && { DATA_DIR="${c}"; break; }; done

# Выравнивание считаем по символам, а не по байтам: printf %-38s в bash
# считает байты, и кириллица (два байта на символ) ломает колонки.
pad() {
  local text="$1" width="${2:-38}" length="${#1}"
  printf '%s' "${text}"
  if [[ ${length} -lt ${width} ]]; then
    printf '%*s' "$((width - length))" ''
  fi
}

check() {
  local name="$1" status="$2" detail="${3:-}" fix_hint="${4:-}"
  local mark colour
  case "${status}" in
    ok)   mark='✓'; colour="${C_GREEN}"; PASSED=$((PASSED+1)) ;;
    warn) mark='!'; colour="${C_YELLOW}"; WARNED=$((WARNED+1)) ;;
    fail) mark='✕'; colour="${C_RED}"; FAILED=$((FAILED+1)) ;;
    *)    mark='·'; colour="" ;;
  esac
  printf '  %s%s%s %s %s\n' "${colour}" "${mark}" "${C_RESET}" "$(pad "${name}" 38)" "${detail}"
  if [[ "${status}" != "ok" && -n "${fix_hint}" ]]; then
    printf '      %s%s%s\n' "${C_DIM}" "${fix_hint}" "${C_RESET}"
  fi
}

print_banner

# ---------------------------------------------------------------------------
# Оборудование
# ---------------------------------------------------------------------------

if [[ -z "${ONLY}" || "${ONLY}" == "hardware" ]]; then
heading "Оборудование"

OS="$(detect_os)"; ARCH="$(detect_arch)"; ACCEL="$(detect_gpu)"
CORES="$(detect_cpu_cores)"; RAM="$(detect_ram_gb)"

check "Операционная система" ok "${OS} $(detect_distro_version) (${ARCH})"
check "Ядер процессора" "$( [[ ${CORES} -ge 4 ]] && echo ok || echo warn )" "${CORES}" \
  "Меньше четырёх ядер: обработка на процессоре будет медленной."
check "Оперативная память" "$( [[ ${RAM} -ge 16 ]] && echo ok || ( [[ ${RAM} -ge 6 ]] && echo warn || echo fail ) )" \
  "${RAM} ГБ" "Для моделей уровня large рекомендуется 16 ГБ. При меньшем объёме выбирайте модели до 1 млрд параметров и включайте int8."

case "${ACCEL}" in
  cuda)
    GPU_NAME="$(detect_gpu_name)"; GPU_MEM="$(detect_gpu_memory_mb)"; CUDA="$(detect_cuda_version)"
    check "Видеокарта" ok "${GPU_NAME} — ${GPU_MEM} МБ"
    check "CUDA" ok "версия ${CUDA}"
    if [[ -n "${PREFIX}" && -x "${PREFIX}/venv/bin/python" ]]; then
      TORCH_CUDA="$("${PREFIX}/venv/bin/python" -c 'import torch;print(torch.cuda.is_available())' 2>/dev/null)"
      check "PyTorch видит видеокарту" "$( [[ "${TORCH_CUDA}" == "True" ]] && echo ok || echo fail )" \
        "${TORCH_CUDA:-нет данных}" \
        "Переустановите PyTorch: ${PREFIX}/venv/bin/pip install --force-reinstall --index-url $(torch_index_url cuda) torch torchaudio"
      CUDNN="$("${PREFIX}/venv/bin/python" -c 'import torch;print(torch.backends.cudnn.version())' 2>/dev/null)"
      [[ -n "${CUDNN}" ]] && check "cuDNN" ok "версия ${CUDNN}"
      if [[ "${CUDA}" == 12.* && "${CUDNN}" == 8* ]]; then
        check "Совместимость CTranslate2" warn "CUDA 12 с cuDNN 8" \
          "Нужен ctranslate2==4.4.0: ${PREFIX}/venv/bin/pip install 'ctranslate2==4.4.0'"
      fi
    fi ;;
  rocm) check "Видеокарта" ok "AMD ROCm"
        check "Поддержка движков" warn "ROCm поддерживается ограниченно" \
          "На ROCm стабильно работают transformers и gigaam; faster-whisper — только на CPU." ;;
  mps)  check "Ускоритель" ok "Apple Silicon (Metal)"
        check "Поддержка движков" warn "NeMo и faster-whisper на GPU недоступны" \
          "На macOS используйте whisper_cpp с Core ML — это самый быстрый путь." ;;
  *)    check "Видеокарта" warn "не обнаружена" \
          "Работа только на процессоре. Включите int8 и выберите модель поменьше." ;;
esac

if [[ -n "${DATA_DIR}" ]]; then
  FREE_GB="$(df -Pk "${DATA_DIR}" 2>/dev/null | awk 'NR==2{printf "%d", $4/1024/1024}')"
  check "Свободное место" "$( [[ ${FREE_GB:-0} -ge 20 ]] && echo ok || ( [[ ${FREE_GB:-0} -ge 5 ]] && echo warn || echo fail ) )" \
    "${FREE_GB:-?} ГБ в ${DATA_DIR}" "Полный набор моделей занимает свыше 100 ГБ."
fi
fi

# ---------------------------------------------------------------------------
# Установка
# ---------------------------------------------------------------------------

if [[ -z "${ONLY}" ]]; then
heading "Установка"

if [[ -n "${PREFIX}" && -d "${PREFIX}" ]]; then
  check "Каталог программы" ok "${PREFIX} (версия $(cat "${PREFIX}/VERSION" 2>/dev/null || echo '?'))"
else
  check "Каталог программы" fail "не найден" "Запустите установку: bash scripts/install.sh"
fi

if [[ -n "${DATA_DIR}" && -d "${DATA_DIR}" ]]; then
  check "Каталог данных" ok "${DATA_DIR}"
  for sub in uploads results models logs tmp; do
    if [[ -d "${DATA_DIR}/${sub}" && -w "${DATA_DIR}/${sub}" ]]; then
      check "  ${sub}" ok "доступен на запись"
    else
      check "  ${sub}" fail "нет каталога или прав" "mkdir -p '${DATA_DIR}/${sub}' && chmod 750 '${DATA_DIR}/${sub}'"
      [[ "${FIX}" -eq 1 ]] && mkdir -p "${DATA_DIR}/${sub}" 2>/dev/null && chmod 750 "${DATA_DIR}/${sub}" 2>/dev/null
    fi
  done
else
  check "Каталог данных" fail "не найден" "Укажите путь: --data ПУТЬ"
fi

if [[ -x "${PREFIX}/venv/bin/python" ]]; then
  PYVER="$("${PREFIX}/venv/bin/python" --version 2>&1)"
  check "Виртуальное окружение" ok "${PYVER}"
else
  check "Виртуальное окружение" fail "не найдено" \
    "Пересоздайте: python3 -m venv '${PREFIX}/venv' && '${PREFIX}/venv/bin/pip' install -r '${PREFIX}/requirements/base.txt'"
fi

if [[ -f "${DATA_DIR}/config.yaml" ]]; then
  check "Конфигурация" ok "${DATA_DIR}/config.yaml"
else
  check "Конфигурация" warn "нет файла — используются значения по умолчанию" \
    "Создать: '${PREFIX}/venv/bin/python' -m asrhub --print-config > '${DATA_DIR}/config.yaml'"
fi

if [[ -f "${DATA_DIR}/asrhub.db" ]]; then
  DB_SIZE="$(human_size "$(stat -c%s "${DATA_DIR}/asrhub.db" 2>/dev/null || stat -f%z "${DATA_DIR}/asrhub.db" 2>/dev/null || echo 0)")"
  if have sqlite3; then
    if sqlite3 "${DATA_DIR}/asrhub.db" "PRAGMA integrity_check" 2>/dev/null | grep -q '^ok$'; then
      check "База заданий" ok "${DB_SIZE}, целостность в порядке"
    else
      check "База заданий" fail "нарушена целостность" \
        "Восстановите из копии: ls ${DATA_DIR}/asrhub.db.bak.*"
    fi
  else
    check "База заданий" ok "${DB_SIZE} (sqlite3 не установлен, целостность не проверена)"
  fi
else
  check "База заданий" warn "не создана — будет создана при первом запуске"
fi
fi

# ---------------------------------------------------------------------------
# Зависимости и движки
# ---------------------------------------------------------------------------

if [[ -z "${ONLY}" || "${ONLY}" == "engines" ]]; then
heading "Внешние программы"

if have ffmpeg; then
  check "ffmpeg" ok "$(ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3)"
else
  check "ffmpeg" fail "не найден" \
    "Debian/Ubuntu: sudo apt install ffmpeg · macOS: brew install ffmpeg"
  [[ "${FIX}" -eq 1 ]] && install_system_packages ffmpeg
fi
have ffprobe && check "ffprobe" ok "есть" || check "ffprobe" warn "не найден" "Обычно ставится вместе с ffmpeg."
have git && check "git" ok "$(git --version | cut -d' ' -f3)" || check "git" warn "не найден" "Нужен для установки GigaAM и whisper.cpp."
have curl && check "curl" ok "есть" || check "curl" warn "не найден" "Используется для загрузок и проверки состояния."

heading "Движки распознавания"

if [[ -x "${PREFIX}/venv/bin/python" ]]; then
  "${PREFIX}/venv/bin/python" - <<'PYEOF' 2>/dev/null || check "Проверка движков" fail "не удалось выполнить"
import sys
sys.path.insert(0, "server")
try:
    from asrhub.engines import engine_status
except Exception as exc:
    print(f"  ошибка импорта: {exc}")
    raise SystemExit(1)
green, red = "\033[32m", "\033[31m"
grey, reset = "\033[90m", "\033[0m"
for item in engine_status():
    mark = f"{green}✓{reset}" if item["available"] else f"{red}✕{reset}"
    name = item["id"]
    detail = "установлен" if item["available"] else item["reason"][:70]
    print(f"  {mark} {name:<20} {detail}")
    if not item["available"] and item.get("requirements_file"):
        print(f"      {grey}Установить: bash scripts/models.sh install-engine {name}{reset}")
PYEOF
else
  check "Движки" fail "нет виртуального окружения" "Сначала установите сервер."
fi
fi

# ---------------------------------------------------------------------------
# Сеть и служба
# ---------------------------------------------------------------------------

if [[ -z "${ONLY}" || "${ONLY}" == "network" ]]; then
heading "Сеть"

for host in pypi.org huggingface.co github.com; do
  if check_network "${host}"; then
    check "${host}" ok "доступен"
  else
    check "${host}" warn "недоступен" \
      "Без доступа установка новых движков и загрузка моделей невозможны."
  fi
done

PORT="$(grep -E '^[[:space:]]*server_port:' "${DATA_DIR}/config.yaml" 2>/dev/null | awk '{print $2}' | head -1)"
PORT="${PORT:-8080}"
if check_port_free "${PORT}"; then
  check "Порт ${PORT}" warn "свободен — сервер не запущен" \
    "Запустить: bash scripts/service.sh start"
else
  if have curl && curl -fsS --max-time 3 "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then
    VERSION="$(curl -fsS "http://127.0.0.1:${PORT}/api/health" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p')"
    check "Сервер" ok "отвечает на порту ${PORT}, версия ${VERSION}"
  else
    check "Порт ${PORT}" fail "занят другой программой" \
      "Освободите порт или смените: asrctl config set server_port 9000"
  fi
fi
fi

# ---------------------------------------------------------------------------
# Итог
# ---------------------------------------------------------------------------

printf '\n'
heading "Итог"
printf '  %s✓ пройдено: %d%s   %s! предупреждений: %d%s   %s✕ ошибок: %d%s\n\n' \
  "${C_GREEN}" "${PASSED}" "${C_RESET}" "${C_YELLOW}" "${WARNED}" "${C_RESET}" \
  "${C_RED}" "${FAILED}" "${C_RESET}"

if [[ ${FAILED} -gt 0 ]]; then
  printf '  %sЕсть критические проблемы — сервер может не работать.%s\n' "${C_RED}" "${C_RESET}"
  printf '  Попробуйте автоматическое исправление: bash scripts/doctor.sh --fix\n\n'
  exit 1
elif [[ ${WARNED} -gt 0 ]]; then
  printf '  %sСервер работоспособен, но часть возможностей ограничена.%s\n\n' "${C_YELLOW}" "${C_RESET}"
  exit 0
else
  printf '  %sВсё в порядке.%s\n\n' "${C_GREEN}" "${C_RESET}"
  exit 0
fi
