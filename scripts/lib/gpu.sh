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
    # `|| true`: карта могла исчезнуть с шины между обходом и этим вызовом
    # (виртуалка, горячее отключение), и lspci возвращает единицу — под
    # errexit это обрывало бы определение оборудования целиком.
    line="$(lspci -s "${address}" 2>/dev/null | sed 's/^[^ ]* [^:]*: //' || true)"
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

# ---------------------------------------------------------------------------
# Видит ли карту тот процесс, который будет распознавать
# ---------------------------------------------------------------------------
#
# Это не тот же вопрос, что «есть ли карта в машине», и расхождение между
# двумя ответами — отдельный класс отказа, дорогой именно тем, что снаружи
# выглядит исправной системой. Обновление, поставленное на такую машину,
# проходит все проверки: служба поднимается, /api/health отвечает двумястами,
# а каждое задание падает с невнятным текстом про загрузку модели.
#
# Разбираются три причины. Драйвер, обновлённый без перезагрузки (модуль в
# памяти старый, userspace новые). Карта, отвалившаяся на ходу, — после неё
# ошибка CUDA липкая, и процесс не поправится без перезапуска. И чужой номер
# карты: CUDA_VISIBLE_DEVICES в окружении службы или device: cuda:N с
# номером больше, чем карт в машине.

# Откуда читается состояние драйвера. Пути вынесены в переменные по той же
# причине, что и ASRHUB_PCI_ROOT: иначе проверить разбор можно было бы
# только на машине, где драйвер сломан именно нужным образом.
ASRHUB_NVIDIA_PROC="${ASRHUB_NVIDIA_PROC:-/proc/driver/nvidia/version}"
ASRHUB_NVIDIA_DEV="${ASRHUB_NVIDIA_DEV:-/dev}"
ASRHUB_MODULES_ROOT="${ASRHUB_MODULES_ROOT:-/lib/modules}"

nvidia_loaded_version() {
  # Версия модуля, который сейчас в памяти. Читается из /proc, а не из
  # nvidia-smi: в том самом случае, ради которого всё это написано,
  # nvidia-smi уже не отвечает, а /proc отвечает по-прежнему.
  [[ -r "${ASRHUB_NVIDIA_PROC}" ]] || { printf ''; return 0; }
  sed -n 's/.*Kernel Module *\([0-9][0-9.]*\).*/\1/p' \
    "${ASRHUB_NVIDIA_PROC}" 2>/dev/null | head -1
}

nvidia_ondisk_version() {
  # Версия модуля, лежащего на диске под текущим ядром. Отвечает на вопрос,
  # ради которого разбор и затеян: поможет ли перезагрузка. Если на диске
  # лежит ровно тот модуль, что уже loaded, — не поможет, и человека надо
  # отправить не в reboot, а в dkms.
  have modinfo || { printf ''; return 0; }
  # Код возврата гасится: modinfo отвечает единицей, когда не находит модуль
  # по имени, — а это не поломка, а обычное дело (в RHEL он лежит в extra/,
  # и находится обходом каталогов). Под `set -o pipefail` эта единица роняла
  # весь разбор, и роняла молча: падение случалось внутри подстановки.
  { modinfo -F version nvidia 2>/dev/null || true; } | head -1
}

nvidia_module_versions() {
  # Печатает «ядро<TAB>версия» по каждому установленному ядру, где модуль
  # nvidia лежит на диске.
  #
  # Нужно ровно для одного различения, которого иначе не сделать: модуль мог
  # собраться не под то ядро, что работает сейчас. Так бывает, когда вместе с
  # драйвером приехало и новое ядро. Под работающим ядром модуля тогда
  # действительно нет — и `modinfo nvidia` честно об этом молчит, — но
  # перезагрузка (уже в новое ядро) как раз помогает, и отправлять человека
  # в dkms значит гонять его по кругу.
  local root="${ASRHUB_MODULES_ROOT}" f kern ver
  have modinfo || return 0
  # Три каталога: dkms кладёт по-разному в Debian и RHEL, а бывает и модуль
  # из самого ядра.
  # Четыре места. updates/dkms — то, что собрал DKMS; extra — RHEL; готовые
  # модули Ubuntu лежат в kernel/nvidia-<ветка>/ (например nvidia-595srv), и
  # без этого образца пакетный модуль под работающим ядром оставался невидим.
  for f in "${root}"/*/updates/dkms/nvidia.ko* "${root}"/*/extra/nvidia.ko* \
           "${root}"/*/kernel/nvidia*/nvidia.ko* \
           "${root}"/*/kernel/drivers/video/nvidia.ko*; do
    [[ -e "${f}" ]] || continue
    kern="${f#"${root}/"}"; kern="${kern%%/*}"
    ver="$( { modinfo -F version "${f}" 2>/dev/null || true; } | head -1)"
    [[ -n "${ver}" ]] && printf '%s\t%s\n' "${kern}" "${ver}"
  done
  return 0
}

nvidia_userspace_version() {
  # Версия пользовательских библиотек. nvidia-smi печатает её сам — и
  # печатает именно тогда, когда работать отказывается:
  #     Failed to initialize NVML: Driver/library version mismatch
  #     NVML library version: 595.99
  local v="" so=""
  # Код возврата гасится намеренно, и дважды: nvidia-smi в этой ситуации
  # отвечает единицей, а под `set -o pipefail` это роняет весь разбор —
  # ровно тогда, когда он и нужен.
  if have nvidia-smi; then
    v="$( { nvidia-smi 2>&1 || true; } \
         | sed -n 's/.*NVML library version: *\([0-9][0-9.]*\).*/\1/p' | head -1)"
    [[ -z "${v}" ]] && v="$( { nvidia-smi --query-gpu=driver_version \
                               --format=csv,noheader 2>/dev/null || true; } | head -1)"
  fi
  # Запасной путь — имя файла userspace. Версия в нём полная (595.99.02),
  # а NVML печатает укороченную (595.99), поэтому сравнение идёт по началу.
  if [[ -z "${v}" ]]; then
    for so in "${ASRHUB_NVIDIA_LIB_GLOB:-/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*}" \
              /usr/lib64/libnvidia-ml.so.* /usr/lib/aarch64-linux-gnu/libnvidia-ml.so.*; do
      [[ -e "${so}" ]] || continue
      case "${so##*libnvidia-ml.so.}" in
        [0-9]*) v="${so##*libnvidia-ml.so.}"; break ;;
      esac
    done
  fi
  printf '%s' "${v}"
}

nvidia_dkms_state() {
  # Знает ли DKMS о драйвере NVIDIA. Печатает: known | unknown | absent.
  #
  # Различение не теоретическое. Пустой `dkms status` означает не «модуль не
  # собрался», а «DKMS не знает ни об одном модуле»: драйвер поставлен
  # готовыми модулями (linux-modules-nvidia-*) или пакет nvidia-dkms был
  # удалён при обновлении. В обоих случаях `dkms autoinstall` отвечает
  # тишиной, и совет собрать модуль отправляет человека в пустоту.
  have dkms || { printf 'absent'; return 0; }
  if { dkms status 2>/dev/null || true; } | grep -qi nvidia; then
    printf 'known'; return 0
  fi
  # Исходники на месте, а регистрации нет — это .run-установщик NVIDIA поверх
  # пакетов: он кладёт свои библиотеки мимо dpkg и заводит DKMS, а при
  # обновлении ядра регистрация теряется. Снаружи выглядит как «драйвер
  # поставлен не через DKMS», и совет про пакеты в этом случае — мимо.
  [[ -n "$(nvidia_dkms_source)" ]] && { printf 'stale'; return 0; }
  printf 'unknown'
}

nvidia_dkms_source() {
  # Версия исходников драйвера в /usr/src (каталог nvidia-<версия>).
  local d
  for d in "${ASRHUB_DKMS_SRC:-/usr/src}"/nvidia-[0-9]*; do
    [[ -d "${d}" ]] || continue
    printf '%s' "${d##*/nvidia-}"
    return 0
  done
  printf ''
}

nvidia_branch_hint() {
  # Имя ветки драйвера для команд установки: «595» из nvidia-driver-595.
  # Берётся из версии библиотек, а не из имени пакета: пакет может
  # называться по-разному, а первая часть версии — это и есть ветка.
  local v="${1:-}"
  [[ -n "${v}" ]] || v="$(nvidia_userspace_version)"
  printf '%s' "${v%%.*}"
}

nvidia_versions_match() {
  # Сравнение по первым двум числам: «595.91.07» и «595.91» — одна версия,
  # записанная по-разному. Пустое значение сравнивать не с чем, и выдумывать
  # расхождение на пустом месте хуже, чем промолчать.
  local a="$1" b="$2"
  [[ -z "${a}" || -z "${b}" ]] && return 0
  [[ "$(printf '%s' "${a}" | cut -d. -f1-2)" == "$(printf '%s' "${b}" | cut -d. -f1-2)" ]]
}

nvidia_driver_verdict() {
  # Печатает приговор и два числа через «|»:
  #   ok|<в памяти>|<библиотеки>       версии сходятся, дело не в драйвере
  #   reboot|<в памяти>|<на диске>     на диске новый модуль — перезагрузка поможет
  #   rebuild|<в памяти>|<библиотеки>  на диске тот же модуль — не поможет
  #   unknown||                        сравнивать нечего
  local loaded ondisk userspace running kern ver
  loaded="$(nvidia_loaded_version)"
  ondisk="$(nvidia_ondisk_version)"
  userspace="$(nvidia_userspace_version)"
  running="$(uname -r 2>/dev/null || true)"
  # `modinfo nvidia` ищет модуль по имени и находит его не везде: в RHEL он
  # лежит в extra/, а не в updates/dkms/. Тогда версию под работающим ядром
  # берём из обхода каталогов — иначе исправно собранный модуль выглядел бы
  # как отсутствующий, и дальше его приняли бы за «собран под другое ядро».
  if [[ -z "${ondisk}" && -n "${running}" ]]; then
    while IFS=$'\t' read -r kern ver; do
      [[ "${kern}" == "${running}" ]] && { ondisk="${ver}"; break; }
    done < <(nvidia_module_versions)
  fi
  if [[ -z "${loaded}" || -z "${userspace}" ]]; then
    printf 'unknown|%s|%s' "${loaded}" "${userspace}"
    return 0
  fi
  if nvidia_versions_match "${loaded}" "${userspace}"; then
    printf 'ok|%s|%s' "${loaded}" "${userspace}"
    return 0
  fi
  if [[ -n "${ondisk}" ]] && ! nvidia_versions_match "${loaded}" "${ondisk}"; then
    printf 'reboot|%s|%s' "${loaded}" "${ondisk}"
    return 0
  fi
  # Под работающим ядром нового модуля нет. Прежде чем отправлять в dkms,
  # смотрим на остальные установленные ядра: если под одним из них модуль
  # уже собран и сходится с библиотеками, чинить нечего — надо загрузиться
  # в него.
  while IFS=$'\t' read -r kern ver; do
    [[ -n "${kern}" ]] || continue
    [[ "${kern}" == "${running}" ]] && continue
    if nvidia_versions_match "${ver}" "${userspace}"; then
      printf 'other-kernel|%s|%s' "${loaded}" "${kern}"
      return 0
    fi
  done < <(nvidia_module_versions)
  # Без modinfo сравнивать не с чем, и утверждать «модуль не собрался» —
  # значит выдавать отсутствие данных за вывод.
  if ! have modinfo; then
    printf 'unknown-disk|%s|%s' "${loaded}" "${userspace}"
    return 0
  fi
  printf 'rebuild|%s|%s' "${loaded}" "${userspace}"
}

config_device() {
  # Значение device из config.yaml. Пусто — значит «auto»: сервер выберет
  # сам. Читается grep'ом, а не разбором YAML, по той же причине, что и
  # порт, — питона под рукой может не быть вовсе.
  local data_dir="$1" v=""
  [[ -r "${data_dir}/config.yaml" ]] || { printf ''; return 0; }
  v="$(grep -E '^[[:space:]]*device:[[:space:]]*' "${data_dir}/config.yaml" 2>/dev/null \
       | head -1 | sed 's/^[[:space:]]*device:[[:space:]]*//')"
  v="$(printf '%s' "${v}" | sed 's/[[:space:]]*#.*$//' | tr -d "\"'\\r" | tr -d '[:space:]')"
  printf '%s' "${v}"
}

service_cuda_devices() {
  # Что служба видит в CUDA_VISIBLE_DEVICES. Спрашиваем у systemd, а не у
  # своей оболочки: окружение службы и окружение человека, запустившего
  # скрипт, — разные вещи, и расходятся они как раз в этом месте.
  local service="${1:-asrhub}"
  have systemctl || { printf ''; return 0; }
  { systemctl show "${service}" -p Environment --value 2>/dev/null || true; } \
    | tr ' ' '\n' | sed -n 's/^CUDA_VISIBLE_DEVICES=//p' | head -1
}

gpu_torch_probe() {
  # Спрашивает карту у того самого питона, который будет распознавать.
  # Печатает «состояние|устройство|подробность»:
  #   ok|cuda|NVIDIA GeForce RTX 5090     карта отвечает
  #   cpu|auto → cpu|…                    карты нет, сервер уйдёт на процессор
  #   fail|cuda|…                         карта настроена, но недоступна
  #   skip|…|…                            проверить нечем
  #
  # Ответ берётся у torch, а не у nvidia-smi: они расходятся, и заданию
  # важен именно первый.
  local py="$1" code_dir="$2" device="${3:-auto}"
  [[ -x "${py}" ]] || { printf 'skip|%s|нет интерпретатора: %s' "${device}" "${py}"; return 0; }
  "${py}" - "${code_dir}" "${device}" 2>/dev/null <<'PYEOF' || printf 'skip|%s|проба не выполнилась' "${device}"
import sys

sys.path.insert(0, sys.argv[1])
устройство = (sys.argv[2] or "auto").strip().lower()

try:
    import torch
except Exception:                                   # noqa: BLE001
    torch = None


def имя_карты() -> str:
    try:
        return torch.cuda.get_device_name(0) if torch.cuda.device_count() else ""
    except Exception:                               # noqa: BLE001
        return ""


def проверка(устройство: str):
    # Спрашиваем ту же функцию, которой пользуется сервер, чтобы ответы
    # скрипта и сервера не разошлись. Её может не быть: перед обновлением в
    # каталоге лежит прежняя версия. Тогда — своими силами, тем же способом.
    try:
        from asrhub.hardware import проверить_ускоритель
    except Exception:                               # noqa: BLE001
        pass
    else:
        return проверить_ускоритель(устройство)
    try:
        всего = int(torch.cuda.device_count())
    except Exception as exc:                        # noqa: BLE001
        return False, f"CUDA не отвечает на перечислении устройств: {exc}"
    if всего <= 0:
        return False, "CUDA не видит ни одной карты."
    номер = 0
    if ":" in устройство:
        хвост = устройство.split(":", 1)[1].strip()
        if хвост.isdigit():
            номер = int(хвост)
    if номер >= всего:
        return False, f"Запрошена карта {номер}, а доступно карт: {всего} (номера с 0)."
    try:
        torch.cuda.get_device_name(номер)
    except Exception as exc:                        # noqa: BLE001
        return False, f"Карта {номер} не отвечает: {exc}"
    return True, ""


if устройство == "cpu":
    print("cpu|cpu|распознавание настроено на процессор")
    raise SystemExit(0)

if torch is None:
    print(f"skip|{устройство}|torch не установлен — спросить карту нечем")
    raise SystemExit(0)

if устройство in ("", "auto"):
    try:
        есть = bool(torch.cuda.is_available())
    except Exception:                               # noqa: BLE001
        есть = False
    if есть:
        print(f"ok|auto → cuda|{имя_карты()}")
    else:
        print("cpu|auto → cpu|карта не отвечает, сервер уйдёт на процессор")
    raise SystemExit(0)

годно, причина = проверка(устройство)
print(f"ok|{устройство}|{имя_карты()}" if годно else f"fail|{устройство}|{причина}")
PYEOF
}

gpu_runtime_report() {
  # Полный разбор: спросить карту и, если её нет, объяснить почему.
  #
  #   gpu_runtime_report <питон> <каталог_данных> <каталог_кода> [служба]
  #
  # Код возврата: 0 — распознавание поедет, 1 — настроена карта, которой у
  # процесса нет. Второе сознательно не считается «просто предупреждением»:
  # на таком сервере падает каждое задание.
  local py="$1" data_dir="$2" code_dir="$3" service="${4:-asrhub}"
  local device probe state shown detail
  device="$(config_device "${data_dir}")"
  [[ -z "${device}" ]] && device="auto"

  probe="$(gpu_torch_probe "${py}" "${code_dir}" "${device}")"
  state="$(printf '%s' "${probe}" | cut -d'|' -f1)"
  shown="$(printf '%s' "${probe}" | cut -d'|' -f2)"
  detail="$(printf '%s' "${probe}" | cut -d'|' -f3-)"

  case "${state}" in
    ok)
      ok "Видеокарта доступна процессу: ${shown}${detail:+ — ${detail}}"
      return 0 ;;
    cpu)
      if [[ "${device}" == "cpu" ]]; then
        info "Распознавание настроено на процессор — видеокарту проверять незачем."
        return 0
      fi
      warn "Видеокарта не отвечает: ${shown}."
      hint "Задания пойдут на процессоре — в несколько раз медленнее реального времени."
      gpu_runtime_diagnose "${service}"
      return 0 ;;
    skip)
      info "Видеокарту проверить нечем: ${detail}"
      return 0 ;;
    *)
      error "Настроено «device: ${device}», но карта процессу недоступна."
      printf '  %s\n' "${detail}" >&2
      gpu_runtime_diagnose "${service}"
      hint "Чтобы приём не стоял, пока карта чинится: device: cpu в ${data_dir}/config.yaml"
      return 1 ;;
  esac
}

nvidia_repair_hint() {
  # Что делать с несобранным модулем. Ответ зависит от того, как вообще
  # поставлен драйвер, и спутать эти два пути дорого: команда из чужого
  # варианта отвечает тишиной, а человек считает, что починил.
  local branch
  branch="$(nvidia_branch_hint)"
  case "$(nvidia_dkms_state)" in
    known)
      hint "Собрать под работающее ядро:"
      hint "  sudo apt-get install -y \"linux-headers-\$(uname -r)\" linux-headers-generic"
      hint "  sudo dkms autoinstall -k \"\$(uname -r)\" && sudo reboot"
      hint "Метапакет linux-headers-generic ставится не зря: без него DKMS молча"
      hint "пропускает пересборку при каждом следующем обновлении ядра."
      hint "Если сборка упадёт, её журнал: /var/lib/dkms/nvidia/*/build/make.log" ;;
    stale)
      local src
      src="$(nvidia_dkms_source)"
      warn "Исходники драйвера ${src} лежат в /usr/src, но DKMS о них не знает."
      hint "Так выглядит .run-установщик NVIDIA поверх пакетных модулей: его"
      hint "регистрация в DKMS теряется при обновлении ядра, и загружается"
      hint "пакетный модуль — другой версии, чем библиотеки."
      hint "Вернуть сборку и собрать под работающее ядро:"
      hint "  sudo dkms add -m nvidia -v ${src} 2>/dev/null || true"
      hint "  sudo dkms build -m nvidia -v ${src} -k \"\$(uname -r)\""
      hint "  sudo dkms install -m nvidia -v ${src} -k \"\$(uname -r)\" && sudo reboot"
      hint "Журнал сборки, если упадёт: /var/lib/dkms/nvidia/${src}/build/make.log"
      hint "На будущее надёжнее вернуться к пакетному драйверу целиком:"
      hint "  sudo nvidia-uninstall && sudo apt-get install -y nvidia-driver-${branch:-<ветка>}-server"
      hint "Гибрид «.run поверх пакетов» ломается при каждом обновлении ядра." ;;
    unknown)
      warn "DKMS о драйвере NVIDIA не знает — собирать ему нечего."
      hint "Драйвер поставлен готовыми модулями (или пакет nvidia-dkms удалён при"
      hint "обновлении). Модули под каждое ядро приезжают своим пакетом:"
      hint "  sudo apt-get install -y \"linux-modules-nvidia-${branch:-<ветка>}-\$(uname -r)\""
      hint "  либо метапакетом, который следит за ядром сам:"
      hint "  sudo apt-get install -y linux-modules-nvidia-${branch:-<ветка>}-generic"
      hint "Вернуть сборку через DKMS: sudo apt-get install -y nvidia-dkms-${branch:-<ветка>}"
      hint "Что вообще стоит: dpkg -l | grep -i nvidia" ;;
    *)
      hint "DKMS не установлен. Модули под каждое ядро приезжают пакетом:"
      hint "  sudo apt-get install -y \"linux-modules-nvidia-${branch:-<ветка>}-\$(uname -r)\"" ;;
  esac
  # Незавершённое обновление пакетов — частая причина того, что библиотеки
  # уехали вперёд, а модули остались. Проверяется дёшево, а объясняет много.
  hint "Проверьте заодно, что обновление пакетов доведено до конца:"
  hint "  sudo dpkg --configure -a && sudo apt-get -f install"
  hint "  apt list --upgradable | grep -i nvidia"
}

gpu_runtime_diagnose() {
  # Почему карты нет. Три причины по порядку проверки — и по каждой сразу
  # команда, а не совет «разберитесь с драйвером».
  local service="${1:-asrhub}"
  local verdict word loaded other visible xid

  verdict="$(nvidia_driver_verdict)"
  word="$(printf '%s' "${verdict}" | cut -d'|' -f1)"
  loaded="$(printf '%s' "${verdict}" | cut -d'|' -f2)"
  other="$(printf '%s' "${verdict}" | cut -d'|' -f3)"
  case "${word}" in
    reboot)
      warn "Драйвер обновлён без перезагрузки: модуль в памяти ${loaded}, на диске ${other}."
      hint "Перезагрузка поможет: sudo reboot"
      hint "Без перезагрузки — только модуль (карту должны отпустить все, включая Ollama):"
      hint "  sudo systemctl stop ${service} ollama"
      hint "  sudo rmmod nvidia_uvm nvidia_drm nvidia_modeset nvidia && sudo modprobe nvidia_uvm" ;;
    other-kernel)
      warn "Ядро обновилось, а драйвер под него не пересобран: модуль есть под ${other}, работает $(uname -r 2>/dev/null)."
      nvidia_repair_hint
      hint "Запасной путь — загрузиться обратно в ${other}. Работает сразу, но"
      hint "возвращает на ядро без последних исправлений, и при следующем"
      hint "обновлении всё повторится." ;;
    rebuild)
      warn "Модуль ядра ${loaded} и библиотеки ${other} разошлись, а нового модуля нет ни под одно установленное ядро."
      hint "Перезагрузка тут не поможет:"
      nvidia_repair_hint ;;
    unknown-disk)
      warn "Модуль ядра ${loaded} и библиотеки ${other} разошлись."
      hint "Сравнить с модулем на диске нечем: нет modinfo (пакет kmod)."
      hint "Обычно помогает перезагрузка; если не помогла — sudo dkms autoinstall" ;;
    ok)
      info "Версии драйвера сходятся (модуль ${loaded}, библиотеки ${other}) — дело не в них." ;;
  esac

  visible="$(service_cuda_devices "${service}")"
  if [[ -n "${visible}" ]]; then
    warn "В окружении службы задано CUDA_VISIBLE_DEVICES=${visible}."
    hint "Если такой карты нет, CUDA отвечает «invalid device ordinal». Убрать: env.sh в каталоге данных."
  fi

  if have dmesg; then
    xid="$( { dmesg 2>/dev/null || true; } | grep -iE 'NVRM: Xid' | tail -2 || true)"
    if [[ -n "${xid}" ]]; then
      warn "Карта отваливалась (Xid в журнале ядра):"
      printf '  %s\n' "${xid}" >&2
      hint "Ошибка CUDA липкая: процесс службы не поправится сам — sudo systemctl restart ${service}"
    fi
  fi

  if [[ ! -e "${ASRHUB_NVIDIA_DEV}/nvidia0" && ! -e "${ASRHUB_NVIDIA_DEV}/nvidiactl" ]]; then
    warn "Узлов /dev/nvidia* нет — карта процессу не видна вовсе."
    hint "Проверьте, что модуль драйвера загружен: lsmod | grep nvidia"
  fi

  hint "Первая проверка руками: nvidia-smi -L"
}

gpu_config_lines_checked() {
  # То же, что gpu_config_lines, но с проверкой у питона.
  #
  #   gpu_config_lines_checked <питон> <каталог_кода>
  #
  # Карту для config.yaml выбирает nvidia-smi, а считать будет torch — и эти
  # двое расходятся. Записать «device: cuda» там, где torch карты не видит,
  # значит завести сервер, у которого падает каждое задание: жёсткое
  # устройство отключает уход на процессор. Поэтому в таком положении строки
  # не пишутся вовсе — остаётся «auto», то есть медленно, но работает.
  #
  # Печатает строки для конфигурации; пусто — значит оставить auto. Код
  # возврата 1 означает «карта есть, но питон её не видит» — на случай, если
  # вызывающий хочет об этом сказать.
  local py="$1" code_dir="$2" lines wanted probe
  lines="$(gpu_config_lines)"
  [[ -n "${lines}" ]] || return 0
  [[ -x "${py}" ]] || { printf '%s\n' "${lines}"; return 0; }
  wanted="$(printf '%s' "${lines}" | sed -n 's/^[[:space:]]*device:[[:space:]]*//p' | head -1)"
  [[ -n "${wanted}" ]] || { printf '%s\n' "${lines}"; return 0; }
  probe="$(gpu_torch_probe "${py}" "${code_dir}" "${wanted}")"
  case "${probe}" in
    fail\|*) return 1 ;;
    *) printf '%s\n' "${lines}"; return 0 ;;
  esac
}
