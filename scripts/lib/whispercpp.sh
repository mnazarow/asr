#!/usr/bin/env bash
# Сборка whisper.cpp с бэкендом под обнаруженное железо.

build_whisper_cpp() {
  local target="${PREFIX:-/opt/asrhub}/whisper.cpp"
  local accel; accel="$(detect_gpu)"
  local os; os="$(detect_os)"

  require_command cmake "Установите cmake: apt install cmake / brew install cmake" || return 1
  require_command git "Установите git" || return 1

  if [[ -d "${target}/.git" ]]; then
    info "Обновление исходников whisper.cpp…"
    ( cd "${target}" && run git pull --ff-only ) || warn "Не удалось обновить — собираем текущую версию."
  else
    info "Загрузка исходников whisper.cpp…"
    # Без «|| return 1» неудачный clone не останавливал сборку: функцию
    # зовут из условия, а там errexit не действует, и дальше cmake дважды
    # падал на несуществующем каталоге с невнятным «сборка не удалась».
    retry 3 run git clone --depth 1 https://github.com/ggml-org/whisper.cpp "${target}" || return 1
    add_rollback "rm -rf $(_q "${target}")"
  fi

  local flags=()
  case "${accel}" in
    cuda) flags=(-DGGML_CUDA=1); info "Бэкенд: CUDA" ;;
    rocm) flags=(-DGGML_HIPBLAS=1); info "Бэкенд: ROCm" ;;
    # Core ML требует рядом с весами сконвертированный энкодер
    # «ggml-<модель>-encoder.mlmodelc», которого не скачивает никто. Без
    # разрешения отступить whisper.cpp на его отсутствии отказывался
    # запускаться вовсе — движок, ради которого ставят профиль apple, не
    # работал ни с одной моделью. С отступлением: есть энкодер — Neural
    # Engine, нет — Metal.
    mps)  flags=(-DWHISPER_COREML=1 -DWHISPER_COREML_ALLOW_FALLBACK=1)
          info "Бэкенд: Metal (Core ML — если рядом с моделью лежит энкодер .mlmodelc)" ;;
    *)
      if have pkg-config && pkg-config --exists vulkan 2>/dev/null; then
        flags=(-DGGML_VULKAN=1); info "Бэкенд: Vulkan"
      else
        flags=(-DGGML_BLAS=1); info "Бэкенд: CPU с OpenBLAS"
      fi ;;
  esac

  info "Сборка (занимает 3–10 минут)…"
  if ! ( cd "${target}" && run cmake -B build "${flags[@]}" -DCMAKE_BUILD_TYPE=Release >/dev/null &&
         run cmake --build build -j "$(detect_cpu_cores)" --config Release >/dev/null ); then
    warn "Сборка с ускорением не удалась, пробуем чистый CPU…"
    ( cd "${target}" && rm -rf build && run cmake -B build -DCMAKE_BUILD_TYPE=Release >/dev/null &&
      run cmake --build build -j "$(detect_cpu_cores)" --config Release >/dev/null ) || return 1
  fi

  local binary="${target}/build/bin/whisper-cli"
  if [[ ! -x "${binary}" ]]; then
    error "Сборка прошла, но бинарник не найден: ${binary}"
    return 1
  fi
  ok "whisper.cpp собран: ${binary}"
  # Формат строго ИМЯ=ЗНАЧЕНИЕ: systemd читает env.sh через EnvironmentFile
  # и строку с «export » отбраковывает — переменная не доходила до службы,
  # и собранный whisper.cpp сервер попросту не находил. Прежнюю строку
  # убираем, иначе она копится при каждой переустановке.
  #
  # Пробный запуск файл не трогает: раньше он переписывал env.sh (было
  # 600 и чужой владелец — становилось 644 root). Запись — через write_file
  # от имени владельца каталога данных: прежнее «env.sh.new» по постоянному
  # имени root открывал по подложенной ссылке (так создавался чужой
  # authorized_keys). Права прежнего файла сохраняются, новый — 0640.
  local env_file="${DATA_DIR:-/tmp}/env.sh" mode="0640"
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '%s[пробный запуск]%s ASRHUB_WHISPER_CPP=%s в %s\n' \
      "${C_YELLOW}" "${C_RESET}" "${binary}" "${env_file}"
    return 0
  fi
  if [[ -L "${env_file}" ]]; then
    warn "${env_file} — символическая ссылка; путь к whisper-cli не записан."
    hint "Допишите вручную: ASRHUB_WHISPER_CPP=${binary}"
    return 0
  fi
  if [[ -f "${env_file}" ]]; then
    mode="$(stat -c '%a' "${env_file}" 2>/dev/null || stat -f '%Lp' "${env_file}" 2>/dev/null || echo 640)"
  fi
  { if [[ -f "${env_file}" ]]; then
      read_owned "${env_file}" | grep -vE '^(export )?ASRHUB_WHISPER_CPP=' || true
    fi
    printf 'ASRHUB_WHISPER_CPP=%s\n' "${binary}"
  } | write_file "${env_file}" "${mode}" nobackup
  return 0
}
