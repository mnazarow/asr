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
    retry 3 run git clone --depth 1 https://github.com/ggml-org/whisper.cpp "${target}"
    add_rollback "rm -rf '${target}'"
  fi

  local flags=()
  case "${accel}" in
    cuda) flags=(-DGGML_CUDA=1); info "Бэкенд: CUDA" ;;
    rocm) flags=(-DGGML_HIPBLAS=1); info "Бэкенд: ROCm" ;;
    mps)  flags=(-DWHISPER_COREML=1); info "Бэкенд: Metal + Core ML" ;;
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
  printf 'export ASRHUB_WHISPER_CPP=%s\n' "${binary}" >> "${DATA_DIR:-/tmp}/env.sh"
  return 0
}
