#!/usr/bin/env bash
# Определение операционной системы, архитектуры и вычислительного устройства.
# Подключается после common.sh.

detect_os() {
  case "$(uname -s)" in
    Linux)   printf 'linux' ;;
    Darwin)  printf 'macos' ;;
    FreeBSD) printf 'freebsd' ;;
    MINGW*|MSYS*|CYGWIN*) printf 'windows' ;;
    *)       printf 'unknown' ;;
  esac
}

detect_arch() {
  case "$(uname -m)" in
    x86_64|amd64)  printf 'x86_64' ;;
    aarch64|arm64) printf 'arm64' ;;
    armv7l)        printf 'armv7' ;;
    riscv64)       printf 'riscv64' ;;
    *)             printf '%s' "$(uname -m)" ;;
  esac
}

detect_distro() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s' "${ID:-unknown}"
  elif [[ "$(detect_os)" == "macos" ]]; then
    printf 'macos'
  else
    printf 'unknown'
  fi
}

detect_distro_version() {
  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    printf '%s' "${VERSION_ID:-}"
  elif [[ "$(detect_os)" == "macos" ]]; then
    sw_vers -productVersion 2>/dev/null || printf ''
  fi
}

detect_package_manager() {
  local manager
  for manager in apt-get dnf yum zypper pacman apk brew; do
    have "${manager}" && { printf '%s' "${manager}"; return 0; }
  done
  printf 'none'
}

install_system_packages() {
  # install_system_packages ffmpeg git build-essential
  local manager; manager="$(detect_package_manager)"
  [[ $# -eq 0 ]] && return 0
  info "Установка системных пакетов: $*"
  case "${manager}" in
    apt-get)
      as_root apt-get update -qq
      DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq "$@" ;;
    dnf)    as_root dnf install -y -q "$@" ;;
    yum)    as_root yum install -y -q "$@" ;;
    zypper) as_root zypper --non-interactive install "$@" ;;
    pacman) as_root pacman -S --noconfirm --needed "$@" ;;
    apk)    as_root apk add --no-cache "$@" ;;
    brew)   run brew install "$@" ;;
    *)
      warn "Менеджер пакетов не определён. Установите вручную: $*"
      return 1 ;;
  esac
}

system_package_names() {
  # Соответствие логических имён реальным пакетам разных дистрибутивов
  local manager; manager="$(detect_package_manager)"
  local logical="$1"
  case "${logical}:${manager}" in
    ffmpeg:*)            printf 'ffmpeg' ;;
    build:apt-get)       printf 'build-essential' ;;
    build:dnf|build:yum) printf 'gcc gcc-c++ make' ;;
    build:pacman)        printf 'base-devel' ;;
    build:apk)           printf 'build-base' ;;
    build:brew)          printf '' ;;
    python-venv:apt-get) printf 'python3-venv python3-dev' ;;
    python-venv:dnf|python-venv:yum) printf 'python3-devel' ;;
    python-venv:*)       printf '' ;;
    sndfile:apt-get)     printf 'libsndfile1' ;;
    sndfile:dnf|sndfile:yum) printf 'libsndfile' ;;
    sndfile:brew)        printf 'libsndfile' ;;
    sndfile:*)           printf '' ;;
    cmake:*)             printf 'cmake' ;;
    git:*)               printf 'git' ;;
    *)                   printf '%s' "${logical}" ;;
  esac
}

detect_gpu() {
  # Печатает: cuda | rocm | mps | cpu
  if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then
    printf 'cuda'; return 0
  fi
  if have rocm-smi && rocm-smi --showid >/dev/null 2>&1; then
    printf 'rocm'; return 0
  fi
  if [[ "$(detect_os)" == "macos" && "$(detect_arch)" == "arm64" ]]; then
    printf 'mps'; return 0
  fi
  printf 'cpu'
}

detect_cuda_version() {
  have nvidia-smi || { printf ''; return 0; }
  nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -1
}

detect_gpu_memory_mb() {
  have nvidia-smi || { printf '0'; return 0; }
  nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' '
}

detect_gpu_name() {
  have nvidia-smi || { printf ''; return 0; }
  nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1
}

detect_cpu_cores() {
  case "$(detect_os)" in
    linux) nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1' ;;
    macos) sysctl -n hw.physicalcpu 2>/dev/null || printf '1' ;;
    *)     getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1' ;;
  esac
}

detect_ram_gb() {
  case "$(detect_os)" in
    linux) awk '/MemTotal/{printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || printf '0' ;;
    macos) printf '%d' "$(( $(sysctl -n hw.memsize 2>/dev/null || echo 0) / 1024 / 1024 / 1024 ))" ;;
    *)     printf '0' ;;
  esac
}

# Индекс PyTorch, подходящий обнаруженному ускорителю.
torch_index_url() {
  local accel="${1:-$(detect_gpu)}"
  local cuda; cuda="$(detect_cuda_version)"
  case "${accel}" in
    cuda)
      case "${cuda}" in
        13.*) printf 'https://download.pytorch.org/whl/cu130' ;;
        12.[89]*|12.[1-9][0-9]*) printf 'https://download.pytorch.org/whl/cu128' ;;
        12.*) printf 'https://download.pytorch.org/whl/cu124' ;;
        11.*) printf 'https://download.pytorch.org/whl/cu118' ;;
        *)    printf 'https://download.pytorch.org/whl/cu124' ;;
      esac ;;
    rocm) printf 'https://download.pytorch.org/whl/rocm6.2' ;;
    mps)  printf '' ;;      # на macOS ставится обычный пакет
    *)    printf 'https://download.pytorch.org/whl/cpu' ;;
  esac
}

# Версия ctranslate2, совместимая с установленным cuDNN.
# Самая частая ошибка установки faster-whisper — рассогласование версий.
ctranslate2_pin() {
  local accel="${1:-$(detect_gpu)}"
  local cuda; cuda="$(detect_cuda_version)"
  if [[ "${accel}" != "cuda" ]]; then printf 'ctranslate2>=4.5'; return 0; fi
  case "${cuda}" in
    11.*) printf 'ctranslate2==3.24.0' ;;
    12.*) printf 'ctranslate2>=4.5' ;;
    13.*) printf 'ctranslate2>=4.5' ;;
    *)    printf 'ctranslate2>=4.5' ;;
  esac
}

print_environment() {
  local os arch distro accel cores ram gpu_name gpu_mem cuda
  os="$(detect_os)"; arch="$(detect_arch)"; distro="$(detect_distro)"
  accel="$(detect_gpu)"; cores="$(detect_cpu_cores)"; ram="$(detect_ram_gb)"
  gpu_name="$(detect_gpu_name)"; gpu_mem="$(detect_gpu_memory_mb)"; cuda="$(detect_cuda_version)"

  printf '%sОбнаруженное окружение%s\n' "${C_BOLD}" "${C_RESET}"
  printf '  Система          %s %s (%s)\n' "${os}" "$(detect_distro_version)" "${distro}"
  printf '  Архитектура      %s\n' "${arch}"
  printf '  Процессор        %s физических ядер\n' "${cores}"
  printf '  Память           %s ГБ\n' "${ram}"
  printf '  Ускоритель       %s\n' "${accel}"
  [[ -n "${gpu_name}" ]] && printf '  Видеокарта       %s (%s МБ)\n' "${gpu_name}" "${gpu_mem}"
  [[ -n "${cuda}" ]] && printf '  CUDA             %s\n' "${cuda}"
  printf '  Менеджер пакетов %s\n' "$(detect_package_manager)"
  printf '  ffmpeg           %s\n' "$(have ffmpeg && ffmpeg -version 2>/dev/null | head -1 | cut -d' ' -f3 || echo 'не найден')"
  printf '\n'
}

# Рекомендуемый профиль установки под обнаруженное железо.
recommend_profile() {
  local accel; accel="$(detect_gpu)"
  local ram; ram="$(detect_ram_gb)"
  local gpu_mem; gpu_mem="$(detect_gpu_memory_mb)"
  case "${accel}" in
    cuda)
      if [[ "${gpu_mem}" -ge 20000 ]]; then printf 'full'
      elif [[ "${gpu_mem}" -ge 8000 ]]; then printf 'standard'
      else printf 'light'; fi ;;
    rocm) printf 'standard' ;;
    mps)  printf 'apple' ;;
    *)    if [[ "${ram}" -ge 16 ]]; then printf 'cpu'; else printf 'light'; fi ;;
  esac
}
