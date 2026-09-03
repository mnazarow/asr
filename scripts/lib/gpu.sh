#!/usr/bin/env bash
# Видеокарта: обнаружение, установка драйвера, настройка.
# Подключается после common.sh и detect.sh.
#
# Отличие от detect.sh: тот отвечает на вопрос «чем считать прямо сейчас» и
# опирается на nvidia-smi, то есть видит карту только с уже установленным
# драйвером. Здесь карта ищется по шине PCI — так она находится и на голой
# системе, где ставить драйвер как раз и нужно.
#
# Всё, что читается из системы, берётся из каталога ASRHUB_PCI_ROOT. По
# умолчанию это /sys/bus/pci/devices, а в тестах — подготовленное дерево:
# иначе проверить разбор идентификаторов можно было бы только на машине с
# нужной картой.

ASRHUB_PCI_ROOT="${ASRHUB_PCI_ROOT:-/sys/bus/pci/devices}"

# Где искать базу имён устройств (файл pci.ids из пакета hwdata). Список
# путей через пробел; переопределяется в тестах по той же причине, что и
# ASRHUB_PCI_ROOT.

# Идентификаторы производителей на шине PCI.
PCI_VENDOR_NVIDIA="0x10de"
PCI_VENDOR_AMD="0x1002"
PCI_VENDOR_INTEL="0x8086"

# Версия ROCm, под которую собран пакет amdgpu-install. Вынесена наверх:
# ссылки на repo.radeon.com включают номер версии дважды, и при обновлении
# правится ровно эта пара строк.
ASRHUB_ROCM_VERSION="${ASRHUB_ROCM_VERSION:-7.2.4}"
ASRHUB_ROCM_PKG="${ASRHUB_ROCM_PKG:-7.2.4.70204-1}"

# Заполняется gpu_tune: переменные окружения, без которых карта работает
# не так, как могла бы. Читается установщиком при создании службы.
# shellcheck disable=SC2034  # читается в install.sh, а не здесь
GPU_ENV_OVERRIDE=""

# ---------------------------------------------------------------------------
# Обнаружение
# ---------------------------------------------------------------------------

_pci_read() {
  # _pci_read <каталог устройства> <файл> — содержимое без перевода строки
  local file="$1/$2"
  [[ -r "${file}" ]] || { printf ''; return 0; }
  tr -d '\n' < "${file}" 2>/dev/null || printf ''
}

_pci_is_display() {
  # Класс 0x0300xx — VGA-совместимый контроллер, 0x0302xx — 3D-контроллер.
  # Второй важен не меньше: так видятся карты в ноутбуках с переключаемой
  # графикой и все ускорители без видеовыхода (Tesla, часть Arc Pro).
  local class="$1"
  [[ "${class}" == 0x0300* || "${class}" == 0x0302* ]]
}

_pci_bar_bytes() {
  # Наибольший размер области памяти устройства (BAR), в байтах.
  # Файл resource содержит по строке на область: начало, конец, флаги — всё
  # шестнадцатеричное. Считаем средствами оболочки, а не awk: strtonum есть
  # только в gawk, а в Ubuntu по умолчанию стоит mawk, где эта строка молча
  # вернула бы нули и все карты стали бы «встроенными».
  local file="$1/resource" start end size max=0
  [[ -r "${file}" ]] || { printf '0'; return 0; }
  while read -r start end _; do
    [[ "${start}" == 0x* && "${end}" == 0x* ]] || continue
    size=$(( 16#${end#0x} - 16#${start#0x} ))
    (( size > 0 )) || continue
    size=$(( size + 1 ))
    # `if`, а не `&&`: `(( ))` возвращает единицу, когда выражение ложно, и
    # на последней области файла (самая большая идёт не последней) весь цикл
    # заканчивался бы неудачей. Здесь это сходит с рук только потому, что
    # следом идёт printf — стоит строке оказаться последней в функции, и
    # errexit оборвёт установку. Ровно так это и случилось в wizard_pad.
    if (( size > max )); then max=${size}; fi
  done < "${file}"
  printf '%s' "${max}"
}

_gpu_is_discrete() {
  # Встроенная графика непригодна для наших движков: у неё нет своей памяти,
  # а ROCm и XPU на ней либо не работают, либо работают медленнее процессора.
  # Ставить ради неё многогигабайтный набор драйверов — впустую занятый диск.
  #
  # Признак: у дискретной карты есть окно памяти под свою VRAM от гигабайта.
  # У встроенной оно 256 МБ и меньше. Дополнительно встроенная почти всегда
  # сидит на нулевой шине, а дискретная — за мостом PCIe.
  local dev="$1" vendor="$2" address bar
  address="$(basename "${dev}")"
  bar="$(_pci_bar_bytes "${dev}")"

  # У NVIDIA встроенной графики в этом смысле не бывает: любая карта дискретная.
  [[ "${vendor}" == "${PCI_VENDOR_NVIDIA}" ]] && return 0

  [[ "${bar}" -ge 1073741824 ]] && return 0
  [[ "${address}" == 0000:00:* ]] && return 1
  # Память меньше гигабайта и не на нулевой шине — считаем встроенной,
  # но говорим об этом вслух: пусть решение будет видно в журнале.
  debug "Устройство ${address}: окно памяти $(human_size "${bar}") — считаем встроенным"
  return 1
}

gpu_scan() {
  # Печатает по строке на найденную видеокарту:
  #   адрес|вендор|ид_устройства|дискретная(1/0)|размер_окна_памяти
  # Пустой вывод означает «видеокарт на шине нет».
  local dev class vendor device discrete bar
  [[ -d "${ASRHUB_PCI_ROOT}" ]] || return 0
  for dev in "${ASRHUB_PCI_ROOT}"/*; do
    [[ -d "${dev}" ]] || continue
    class="$(_pci_read "${dev}" class)"
    _pci_is_display "${class}" || continue
    vendor="$(_pci_read "${dev}" vendor)"
    device="$(_pci_read "${dev}" device)"
    case "${vendor}" in
      "${PCI_VENDOR_NVIDIA}"|"${PCI_VENDOR_AMD}"|"${PCI_VENDOR_INTEL}") ;;
      *) continue ;;
    esac
    discrete=0; _gpu_is_discrete "${dev}" "${vendor}" && discrete=1
    bar="$(_pci_bar_bytes "${dev}")"
    printf '%s|%s|%s|%s|%s\n' "$(basename "${dev}")" "${vendor}" "${device}" \
      "${discrete}" "${bar}"
  done
}

gpu_vendor_label() {
  case "$1" in
    "${PCI_VENDOR_NVIDIA}") printf 'NVIDIA' ;;
    "${PCI_VENDOR_AMD}")    printf 'AMD' ;;
    "${PCI_VENDOR_INTEL}")  printf 'Intel' ;;
    *)                      printf 'неизвестный (%s)' "$1" ;;
  esac
}

gpu_vendor_key() {
  case "$1" in
    "${PCI_VENDOR_NVIDIA}") printf 'nvidia' ;;
    "${PCI_VENDOR_AMD}")    printf 'amd' ;;
    "${PCI_VENDOR_INTEL}")  printf 'intel' ;;
    *)                      printf 'unknown' ;;
  esac
}

gpu_model_name() {
  # Человеческое имя карты. Порядок источников — от точного к приблизительному.
  local vendor="$1" address="${2:-}"
  if [[ "${vendor}" == "${PCI_VENDOR_NVIDIA}" ]] && have nvidia-smi; then
    local name; name="$(detect_gpu_name)"
    [[ -n "${name}" ]] && { printf '%s' "${name}"; return 0; }
  fi
  # lspci знает базу имён, но есть не везде; без него довольствуемся вендором.
  if have lspci && [[ -n "${address}" ]]; then
    local line
    line="$(lspci -s "${address}" 2>/dev/null | sed 's/^[^ ]* [^:]*: //')"
    [[ -n "${line}" ]] && { printf '%s' "${line}"; return 0; }
  fi
  # Та же база имён без lspci: файл pci.ids ставится пакетом hwdata и лежит
  # на большинстве систем сам по себе. Ищем строку модели внутри блока
  # производителя — без него в отчёте оставалось «NVIDIA (устройство 0x2684)»,
  # по которому человек не узнаёт свою карту.
  local ids name
  for ids in ${ASRHUB_PCI_IDS:-/usr/share/hwdata/pci.ids /usr/share/misc/pci.ids /usr/share/pci.ids}; do
    [[ -r "${ids}" ]] || continue
    name="$(awk -v vend="${vendor#0x}" -v dev="${3#0x}" '
      $0 ~ "^"vend"  " { inside = 1; next }
      /^[0-9a-f]/     { inside = 0 }
      inside && $1 == dev { sub("^\t"dev"  ", ""); print; exit }' "${ids}" 2>/dev/null)"
    [[ -n "${name}" ]] && { printf '%s' "${name}"; return 0; }
  done
  printf '%s' "$(gpu_vendor_label "${vendor}") (устройство $3)"
}

gpu_primary() {
  # Одна строка: карта, под которую и будем ставить драйвер.
  # Дискретные важнее встроенных, NVIDIA важнее прочих — просто потому,
  # что её поддерживают все движки, а Intel XPU только часть.
  local best="" line vendor discrete rank best_rank=-1
  while IFS= read -r line; do
    [[ -n "${line}" ]] || continue
    vendor="$(printf '%s' "${line}" | cut -d'|' -f2)"
    discrete="$(printf '%s' "${line}" | cut -d'|' -f4)"
    case "$(gpu_vendor_key "${vendor}")" in
      nvidia) rank=30 ;; amd) rank=20 ;; intel) rank=10 ;; *) rank=0 ;;
    esac
    [[ "${discrete}" == "1" ]] && rank=$((rank + 100))
    if [[ ${rank} -gt ${best_rank} ]]; then best_rank=${rank}; best="${line}"; fi
  done < <(gpu_scan)
  printf '%s' "${best}"
}

gpu_pending() {
  # Карта, которая заработает после установки драйвера.
  #
  # Печатает «ключ_вендора|модель», если на шине есть дискретная карта, а
  # драйвер для неё ещё не готов; иначе пусто.
  #
  # Нужна потому, что `detect_gpu` отвечает на другой вопрос — «что работает
  # прямо сейчас», и на свежей машине честно отвечает «процессор». Отчёт об
  # окружении спрашивал только его и поэтому молчал о карте, которую сам же
  # установщик через минуту и включит: человек с RTX 4090 видел строку
  # «Ускоритель cpu», ни слова о видеокарте и предложенный профиль «light».
  local line address vendor device discrete state
  line="$(gpu_primary)"
  [[ -n "${line}" ]] || return 0
  IFS='|' read -r address vendor device discrete _ <<< "${line}"
  [[ "${discrete}" == "1" ]] || return 0
  state="$(gpu_driver_state "${vendor}")"
  [[ "${state}" == "ready" ]] && return 0
  printf '%s|%s' "$(gpu_vendor_key "${vendor}")" \
    "$(gpu_model_name "${vendor}" "${address}" "${device}")"
}

gpu_driver_state() {
  # Печатает: ready | loaded-nofunc | installed-noload | absent
  #
  #   ready           драйвер работает, карту видно
  #   loaded-nofunc   модуль загружен, но утилита не отвечает — обычно
  #                   несовпадение версий модуля и библиотек после обновления
  #   installed-noload пакеты стоят, модуль не загружен — чаще всего нужна
  #                   перезагрузка или сборка DKMS под новое ядро
  #   absent          драйвера нет
  local vendor="$1"
  case "$(gpu_vendor_key "${vendor}")" in
    nvidia)
      if have nvidia-smi && nvidia-smi -L >/dev/null 2>&1; then printf 'ready'; return 0; fi
      if [[ -e /proc/driver/nvidia/version ]]; then printf 'loaded-nofunc'; return 0; fi
      # Скобки обязательны: «A || B && C» разбирается как «(A || B) && C»,
      # и при наличии nvidia-smi без модуля в updates/dkms ветка не
      # срабатывала — состояние «драйвер стоит, нужна перезагрузка»
      # читалось как «драйвера нет», и скрипт шёл ставить его заново.
      local dkms="/usr/lib/modules/$(uname -r)/updates/dkms"
      if have nvidia-smi \
         || { [[ -d "${dkms}" ]] && ls "${dkms}"/nvidia*.ko* >/dev/null 2>&1; }
      then printf 'installed-noload'; return 0; fi
      printf 'absent' ;;
    amd)
      if have rocminfo && rocminfo >/dev/null 2>&1; then printf 'ready'; return 0; fi
      if have rocm-smi && rocm-smi --showid >/dev/null 2>&1; then printf 'ready'; return 0; fi
      # amdgpu входит в ядро, поэтому «модуль есть» ещё не значит «ROCm есть».
      if [[ -d /sys/module/amdgpu ]]; then printf 'installed-noload'; return 0; fi
      printf 'absent' ;;
    intel)
      # Через подоболочку без pipefail: `clinfo` печатает сотни строк, и
      # SIGPIPE от `grep -q` превращал код конвейера в 141. Рабочая Intel Arc
      # объявлялась «драйвер не установлен», а gpu_ensure_driver шёл ставить
      # набор Intel заново поверх работающего.
      if have clinfo && [[ "$(set +o pipefail; clinfo 2>/dev/null \
           | grep -ci 'Intel.*Graphics' || true)" -gt 0 ]]; then
        printf 'ready'; return 0
      fi
      if [[ -e /dev/dri/renderD128 && -d /sys/module/i915 ]] || [[ -d /sys/module/xe ]]; then
        printf 'installed-noload'; return 0
      fi
      printf 'absent' ;;
    *) printf 'absent' ;;
  esac
}

gpu_secure_boot_enabled() {
  # Secure Boot запрещает грузить неподписанные модули. Драйверы NVIDIA и
  # amdgpu-dkms собираются на месте и подписи не имеют, поэтому после
  # установки модуль просто не загрузится, а установщик об этом не скажет.
  if have mokutil; then
    mokutil --sb-state 2>/dev/null | grep -qi 'enabled' && return 0
    return 1
  fi
  local var
  for var in /sys/firmware/efi/efivars/SecureBoot-*; do
    [[ -r "${var}" ]] || continue
    # Первые четыре байта — атрибуты, пятый — само значение.
    [[ "$(od -An -t u1 -j 4 -N 1 "${var}" 2>/dev/null | tr -d ' ')" == "1" ]] && return 0
  done
  return 1
}

gpu_report() {
  # Что нашли на шине — до всякой установки.
  local line address vendor device discrete bar state
  line="$(gpu_primary)"
  if [[ -z "${line}" ]]; then
    if [[ "$(detect_os)" == "macos" && "$(detect_arch)" == "arm64" ]]; then
      info "Видеоядро Apple Silicon: драйвер входит в систему, ставить нечего."
    else
      info "Видеокарт на шине PCI не найдено — работаем на процессоре."
    fi
    return 0
  fi
  IFS='|' read -r address vendor device discrete bar <<< "${line}"
  state="$(gpu_driver_state "${vendor}")"
  printf '  %sВидеокарта%s       %s\n' "${C_BOLD}" "${C_RESET}" \
    "$(gpu_model_name "${vendor}" "${address}" "${device}")"
  printf '  Адрес на шине    %s, окно памяти %s%s\n' "${address}" \
    "$(human_size "${bar}")" \
    "$([[ "${discrete}" == "1" ]] && echo ", дискретная" || echo ", встроенная")"
  case "${state}" in
    ready)            printf '  Драйвер          установлен и работает\n' ;;
    loaded-nofunc)    printf '  Драйвер          загружен, но не отвечает\n' ;;
    installed-noload) printf '  Драйвер          установлен, модуль не загружен\n' ;;
    absent)           printf '  Драйвер          не установлен\n' ;;
  esac
  # Именно if, а не «условие && печать»: при выключенном Secure Boot такая
  # строка вернула бы из функции единицу, и весь установщик прекращался бы
  # на шаге, который ничего не делает, кроме печати.
  if gpu_secure_boot_enabled; then
    printf '  Secure Boot      включён\n'
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Установка драйвера
# ---------------------------------------------------------------------------

_gpu_reboot_required=0
gpu_reboot_required() { [[ "${_gpu_reboot_required}" -eq 1 ]]; }

_nvidia_install_apt() {
  local distro version repo
  distro="$(detect_distro)"; version="$(detect_distro_version)"
  # Готовый пакет из репозитория дистрибутива проще и обновляется вместе с
  # системой. Репозиторий NVIDIA нужен, только когда своего пакета нет.
  if have ubuntu-drivers; then
    info "Ставим драйвер средствами дистрибутива (ubuntu-drivers)."
    as_root ubuntu-drivers install || return 1
    return 0
  fi
  # Ключ репозитория ставится пакетом cuda-keyring: так подпись обновляется
  # сама и не превращается в просроченный ключ через год.
  repo="$(printf '%s%s' "${distro}" "${version//./}")"
  info "Подключаем репозиторий NVIDIA для ${repo}."
  local keyring="/tmp/cuda-keyring.deb"
  download "https://developer.download.nvidia.com/compute/cuda/repos/${repo}/x86_64/cuda-keyring_1.1-1_all.deb" \
    "${keyring}" || {
      error "Репозитория NVIDIA для «${repo}» нет."
      hint "Поставьте драйвер средствами дистрибутива и повторите установку."
      return 1
    }
  as_root dpkg -i "${keyring}" || return 1
  as_root apt-get update -qq || return 1
  # nvidia-open — открытые модули ядра; на картах Turing и новее это
  # рекомендованный вариант, на более старых работают только закрытые.
  DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq nvidia-open \
    || DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq cuda-drivers \
    || return 1
}

_nvidia_install_dnf() {
  local distro; distro="$(detect_distro)"
  case "${distro}" in
    fedora)
      info "Подключаем RPM Fusion — там лежит собранный драйвер для Fedora."
      as_root dnf install -y -q \
        "https://mirrors.rpmfusion.org/free/fedora/rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm" \
        "https://mirrors.rpmfusion.org/nonfree/fedora/rpmfusion-nonfree-release-$(rpm -E %fedora).noarch.rpm" \
        || return 1
      as_root dnf install -y -q akmod-nvidia xorg-x11-drv-nvidia-cuda || return 1 ;;
    rhel|centos|rocky|almalinux)
      local ver; ver="$(detect_distro_version | cut -d. -f1)"
      as_root dnf config-manager --add-repo \
        "https://developer.download.nvidia.com/compute/cuda/repos/rhel${ver}/x86_64/cuda-rhel${ver}.repo" \
        || return 1
      as_root dnf module -y install nvidia-driver:latest-dkms 2>/dev/null \
        || as_root dnf install -y -q nvidia-open || return 1 ;;
    *) return 1 ;;
  esac
}

_nvidia_install_pacman() {
  # На Arch пакет собран под ядро дистрибутива, DKMS нужен только для
  # нестандартного ядра — его и ставим, чтобы не гадать.
  as_root pacman -S --noconfirm --needed nvidia-dkms nvidia-utils cuda || return 1
}

_nvidia_install_zypper() {
  as_root zypper --non-interactive install nvidia-video-G06 nvidia-compute-G06 || return 1
}

gpu_install_nvidia() {
  local manager; manager="$(detect_package_manager)"
  info "Устанавливаем драйвер NVIDIA (менеджер пакетов: ${manager})."
  case "${manager}" in
    apt-get) _nvidia_install_apt ;;
    dnf|yum) _nvidia_install_dnf ;;
    pacman)  _nvidia_install_pacman ;;
    zypper)  _nvidia_install_zypper ;;
    *)
      error "Менеджер пакетов «${manager}» не поддержан для установки драйвера."
      hint "Поставьте драйвер вручную: https://www.nvidia.com/Download/index.aspx"
      return 1 ;;
  esac
}

gpu_install_amd() {
  # Ядро содержит amdgpu, поэтому карта работает и без нас. Ставим ROCm —
  # без него ускорителя для расчётов нет, есть только вывод изображения.
  local manager version url pkg
  manager="$(detect_package_manager)"
  info "Устанавливаем ROCm для видеокарты AMD (менеджер пакетов: ${manager})."
  case "${manager}" in
    apt-get)
      local codename; codename="$(. /etc/os-release 2>/dev/null && printf '%s' "${VERSION_CODENAME:-}")"
      [[ -z "${codename}" ]] && { error "Не удалось определить выпуск Ubuntu."; return 1; }
      url="https://repo.radeon.com/amdgpu-install/${ASRHUB_ROCM_VERSION}/ubuntu/${codename}/amdgpu-install_${ASRHUB_ROCM_PKG}_all.deb"
      pkg="/tmp/amdgpu-install.deb"
      download "${url}" "${pkg}" || {
        error "Пакета amdgpu-install для «${codename}» нет по адресу ${url}."
        hint "Список выпусков: https://repo.radeon.com/amdgpu-install/"
        return 1
      }
      as_root apt-get install -y -qq "${pkg}" || return 1
      as_root apt-get update -qq || return 1
      DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq \
        "linux-headers-$(uname -r)" "linux-modules-extra-$(uname -r)" || true
      DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq amdgpu-dkms rocm || return 1 ;;
    dnf|yum)
      local ver major; ver="$(detect_distro_version)"; major="${ver%%.*}"
      url="https://repo.radeon.com/amdgpu-install/${ASRHUB_ROCM_VERSION}/rhel/${ver}/amdgpu-install-${ASRHUB_ROCM_PKG}.el${major}.noarch.rpm"
      as_root dnf install -y -q "${url}" || {
        error "Пакета amdgpu-install для RHEL ${ver} нет."
        return 1
      }
      as_root dnf install -y -q amdgpu-dkms rocm || return 1 ;;
    pacman)
      as_root pacman -S --noconfirm --needed rocm-hip-sdk rocminfo || return 1 ;;
    *)
      error "Установка ROCm для «${manager}» не автоматизирована."
      hint "Инструкция: https://rocm.docs.amd.com/projects/install-on-linux/"
      return 1 ;;
  esac
  # Доступ к устройству идёт через группы render и video; без них процесс
  # сервера получит «permission denied» на /dev/kfd и уедет на процессор.
  local target_user="${SERVICE_USER:-${SUDO_USER:-${USER:-}}}"
  [[ -n "${target_user}" ]] && as_root usermod -a -G render,video "${target_user}" 2>/dev/null || true
}

gpu_install_intel() {
  local manager; manager="$(detect_package_manager)"
  info "Устанавливаем набор Intel для дискретной карты Arc (${manager})."
  case "${manager}" in
    apt-get)
      as_root install -d -m 0755 /usr/share/keyrings || return 1
      # Ключ репозитория Intel: без него apt откажется от пакетов молча.
      # Через файл, а не конвейером из curl: в конвейере обёртка run видит
      # только правую половину, и при пробном запуске левая выполняется
      # по-настоящему, а сбой загрузки маскируется кодом возврата gpg.
      local key="/tmp/intel-graphics.key"
      download "https://repositories.intel.com/gpu/intel-graphics.key" "${key}" || {
        error "Не удалось получить ключ репозитория Intel."
        return 1
      }
      as_root gpg --dearmor --yes -o /usr/share/keyrings/intel-graphics.gpg "${key}" || return 1
      local codename; codename="$(. /etc/os-release 2>/dev/null && printf '%s' "${VERSION_CODENAME:-}")"
      printf 'deb [arch=amd64 signed-by=/usr/share/keyrings/intel-graphics.gpg] https://repositories.intel.com/gpu/ubuntu %s unified\n' \
        "${codename}" > /tmp/intel-gpu.list
      as_root install -m 0644 /tmp/intel-gpu.list /etc/apt/sources.list.d/intel-gpu.list || return 1
      as_root apt-get update -qq || return 1
      DEBIAN_FRONTEND=noninteractive as_root apt-get install -y -qq \
        intel-opencl-icd intel-level-zero-gpu libze1 clinfo || return 1
      local target_user="${SERVICE_USER:-${SUDO_USER:-${USER:-}}}"
      [[ -n "${target_user}" ]] && as_root usermod -a -G render,video "${target_user}" 2>/dev/null || true ;;
    *)
      error "Установка драйвера Intel для «${manager}» не автоматизирована."
      hint "Инструкция: https://dgpu-docs.intel.com/driver/installation.html"
      return 1 ;;
  esac
}

gpu_ensure_driver() {
  # Возвращает 0, если после вызова карта готова или будет готова после
  # перезагрузки; 1 — если поставить не удалось и считать придётся процессором.
  local mode="${1:-auto}" line address vendor device discrete bar state key

  [[ "${mode}" == "none" ]] && { debug "Установка драйвера отключена ключом"; return 1; }
  if [[ "$(detect_os)" != "linux" ]]; then
    debug "Установка драйвера предусмотрена только для Linux"
    return 1
  fi

  line="$(gpu_primary)"
  [[ -n "${line}" ]] || return 1
  IFS='|' read -r address vendor device discrete bar <<< "${line}"
  key="$(gpu_vendor_key "${vendor}")"
  [[ "${mode}" != "auto" && "${mode}" != "${key}" ]] && {
    info "Ключ --gpu-driver=${mode} не совпал с найденной картой (${key}) — пропускаем."
    return 1
  }

  if [[ "${discrete}" != "1" ]]; then
    info "Найдена только встроенная графика — для расчётов она не годится."
    hint "Драйвер для неё уже есть в ядре; ставить набор для вычислений незачем."
    return 1
  fi

  # Intel Arc: карта хорошая, но ни один движок ASR Hub пока не умеет считать
  # на XPU — сервер знает только cuda, rocm, mps и cpu. Ставить ради этого
  # набор Intel на несколько сотен мегабайт значит занять диск впустую,
  # поэтому по умолчанию не ставим, а по явному --gpu-driver intel ставим:
  # набор нужен, например, для сборки whisper.cpp с SYCL.
  if [[ "${key}" == "intel" && "${mode}" == "auto" ]]; then
    info "Найдена Intel Arc, но движки распознавания её пока не используют."
    hint "Ставить набор Intel незачем: сервер будет считать на процессоре."
    hint "Если он всё же нужен, повторите с ключом --gpu-driver intel."
    return 1
  fi

  state="$(gpu_driver_state "${vendor}")"
  case "${state}" in
    ready)
      ok "Драйвер $(gpu_vendor_label "${vendor}") уже установлен и работает."
      return 0 ;;
    installed-noload)
      warn "Драйвер установлен, но модуль ядра не загружен."
      if as_root modprobe "$([[ "${key}" == "nvidia" ]] && echo nvidia || echo amdgpu)" 2>/dev/null \
         && [[ "$(gpu_driver_state "${vendor}")" == "ready" ]]; then
        ok "Модуль загружен, карта доступна."
        return 0
      fi
      hint "Обычно помогает перезагрузка: модуль пересобирается под текущее ядро."
      _gpu_reboot_required=1
      return 0 ;;
    loaded-nofunc)
      warn "Модуль драйвера загружен, но утилита не отвечает."
      hint "Так бывает после обновления пакетов без перезагрузки."
      _gpu_reboot_required=1
      return 0 ;;
  esac

  # Secure Boot: собранный на месте модуль без подписи не загрузится, и об
  # этом узнают только после перезагрузки — по неработающей карте. Ставить
  # вслепую хуже, чем сказать заранее.
  if gpu_secure_boot_enabled && [[ "${ASRHUB_FORCE_GPU_DRIVER:-0}" != "1" ]]; then
    warn "Включён Secure Boot: собранный модуль драйвера не будет подписан и не загрузится."
    hint "Отключите Secure Boot в UEFI либо зарегистрируйте ключ MOK:"
    hint "  sudo apt-get install -y dkms mokutil && sudo mokutil --import /var/lib/shim-signed/mok/MOK.der"
    hint "После этого повторите установку с ключом --force-gpu-driver."
    return 1
  fi

  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    info "[пробный запуск] Здесь был бы установлен драйвер ${key} для ${address}."
    return 0
  fi

  local installed=0
  case "${key}" in
    nvidia) gpu_install_nvidia && installed=1 ;;
    amd)    gpu_install_amd && installed=1 ;;
    intel)  gpu_install_intel && installed=1 ;;
  esac

  if [[ "${installed}" -ne 1 ]]; then
    warn "Драйвер поставить не удалось — сервер будет работать на процессоре."
    hint "Это не мешает установке: смените устройство в настройках после того,"
    hint "как драйвер появится, — перестанавливать ничего не нужно."
    return 1
  fi

  if [[ "$(gpu_driver_state "${vendor}")" == "ready" ]]; then
    ok "Драйвер $(gpu_vendor_label "${vendor}") установлен, карта доступна."
    return 0
  fi
  ok "Драйвер $(gpu_vendor_label "${vendor}") установлен."
  _gpu_reboot_required=1
  return 0
}

# ---------------------------------------------------------------------------
# Настройка
# ---------------------------------------------------------------------------

gpu_tune() {
  # Мелкие настройки, которые заметно влияют на работу и которые иначе
  # приходится вспоминать вручную на каждой машине.
  local line address vendor device discrete bar key
  line="$(gpu_primary)"; [[ -n "${line}" ]] || return 0
  IFS='|' read -r address vendor device discrete bar <<< "${line}"
  key="$(gpu_vendor_key "${vendor}")"
  [[ "${discrete}" == "1" ]] || return 0

  case "${key}" in
    nvidia)
      have nvidia-smi || return 0
      nvidia-smi -L >/dev/null 2>&1 || return 0
      # Постоянный режим: без него драйвер выгружается между заданиями, и
      # первое распознавание после паузы теряет секунды на инициализацию.
      if as_root nvidia-smi -pm 1 >/dev/null 2>&1; then
        ok "Постоянный режим видеокарты включён."
      else
        debug "Постоянный режим включить не удалось — не критично"
      fi
      # На картах с ECC (Tesla, часть RTX Pro) память под задание меньше
      # заявленной; сообщаем, чтобы выбор модели не оказался неожиданностью.
      local ecc
      ecc="$(nvidia-smi --query-gpu=ecc.mode.current --format=csv,noheader 2>/dev/null | head -1 || true)"
      [[ "${ecc}" == "Enabled" ]] && info "Включена ECC: доступной памяти примерно на 6 % меньше." ;;
    amd)
      # Потребительские Radeon определяются как неподдерживаемые, хотя
      # работают: ROCm сверяет ревизию gfx со своим списком. Подсказка
      # HSA_OVERRIDE_GFX_VERSION — стандартный способ это обойти.
      local gfx=""
      # Проверка на наличие обязательна: без неё «команда не найдена» под
      # errexit прекращает установку прямо здесь, ничего не напечатав, —
      # а rocminfo как раз и не существует, пока ROCm не поставлен.
      if have rocminfo; then
        gfx="$(rocminfo 2>/dev/null | sed -n 's/.*gfx\([0-9a-f]*\).*/\1/p' | head -1 || true)"
      fi
      case "${gfx}" in
        1031|1032|1033|1034|1035|1036)
          GPU_ENV_OVERRIDE="HSA_OVERRIDE_GFX_VERSION=10.3.0"
          info "Карта RDNA2 вне списка ROCm — добавлена подсказка HSA_OVERRIDE_GFX_VERSION=10.3.0." ;;
        1101|1102|1103)
          GPU_ENV_OVERRIDE="HSA_OVERRIDE_GFX_VERSION=11.0.0"
          info "Карта RDNA3 вне списка ROCm — добавлена подсказка HSA_OVERRIDE_GFX_VERSION=11.0.0." ;;
      esac ;;
    intel)
      # Драйвер Intel по умолчанию отдаёт под одно выделение четверть памяти,
      # чего не хватает большим моделям.
      # shellcheck disable=SC2034  # читается в install.sh
      GPU_ENV_OVERRIDE="NEOReadDebugKeys=1;ClDeviceGlobalMemSizeAvailablePercent=90" ;;
  esac
  return 0
}

gpu_config_lines() {
  # Строки для config.yaml под найденное железо. Пусто — значит оставить
  # автоопределение сервера, оно и так справляется.
  local line address vendor device discrete bar key mem_mb
  line="$(gpu_primary)"; [[ -n "${line}" ]] || return 0
  IFS='|' read -r address vendor device discrete bar <<< "${line}"
  [[ "${discrete}" == "1" ]] || return 0
  key="$(gpu_vendor_key "${vendor}")"
  [[ "$(gpu_driver_state "${vendor}")" == "ready" ]] || return 0

  case "${key}" in
    nvidia)
      mem_mb="$(detect_gpu_memory_mb)"
      printf 'device: cuda\n'
      # float16 быстрее и точнее int8, но на картах до 8 ГБ большие модели
      # в неё не помещаются — там осмысленнее int8_float16.
      if [[ "${mem_mb}" -ge 8000 ]]; then printf 'compute_type: float16\n'
      else printf 'compute_type: int8_float16\n'; fi ;;
    # Имена устройств берутся из каталога параметров сервера: там их
    # ровно четыре — cuda, rocm, mps, cpu. «xpu» сервер не примет и не
    # поймёт, поэтому для Intel оставляем автоопределение.
    amd)   printf 'device: rocm\ncompute_type: float16\n' ;;
    intel) return 0 ;;
  esac
}
