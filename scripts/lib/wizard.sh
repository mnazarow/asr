#!/usr/bin/env bash
#
# Интерактивные диалоги для мастера установки.
#
# Все функции ведут себя одинаково в трёх режимах:
#   * терминал есть и --yes не задан — задаётся вопрос;
#   * задан --yes или ввод не с терминала — берётся значение по умолчанию;
#   * задан --dry-run — то же, что и --yes, но без изменений на диске.
#
# Это важно: один и тот же скрипт должен работать и как мастер для человека,
# и как шаг в автоматической установке, где спрашивать некого.

# Можно ли вообще задавать вопросы.
wizard_interactive() {
  [[ "${ASRHUB_ASSUME_YES:-0}" == "1" ]] && return 1
  [[ -t 0 ]] || return 1
  return 0
}

# Заголовок шага мастера.
wizard_step() {
  local title="$1" subtitle="${2:-}"
  printf '\n%s%s%s\n' "${C_BOLD}${C_BLUE}" "${title}" "${C_RESET}"
  [[ -n "${subtitle}" ]] && printf '%s%s%s\n' "${C_DIM}" "${subtitle}" "${C_RESET}"
  printf '%s%s%s\n' "${C_DIM}" "$(printf '─%.0s' $(seq 1 68))" "${C_RESET}"
}

# Выбор из списка.
#
#   wizard_choose ПЕРЕМЕННАЯ "Вопрос" НОМЕР_ПО_УМОЛЧАНИЮ \
#     "значение|Краткое название|Пояснение" ...
#
# Пояснение печатается серым под названием: в установщике важнее объяснить
# последствия выбора, чем сэкономить строку.
wizard_choose() {
  local __var="$1" question="$2" default_index="$3"; shift 3
  local options=("$@")
  local count=${#options[@]}
  local values=() labels=() notes=()

  local option value label note
  for option in "${options[@]}"; do
    IFS='|' read -r value label note <<< "${option}"
    values+=("${value}"); labels+=("${label}"); notes+=("${note}")
  done

  if ! wizard_interactive; then
    printf -v "${__var}" '%s' "${values[$((default_index - 1))]}"
    info "${question} → ${labels[$((default_index - 1))]} (по умолчанию)"
    return 0
  fi

  printf '\n%s\n' "${question}"
  local index
  for ((index = 0; index < count; index++)); do
    local mark="  "
    [[ $((index + 1)) -eq ${default_index} ]] && mark="${C_GREEN}▸${C_RESET} "
    printf '%s%s%2d)%s %s\n' "${mark}" "${C_BOLD}" "$((index + 1))" "${C_RESET}" "${labels[$index]}"
    [[ -n "${notes[$index]}" ]] && printf '      %s%s%s\n' "${C_DIM}" "${notes[$index]}" "${C_RESET}"
  done

  local answer
  while true; do
    read -r -p "$(printf '%sВыбор%s [%d]: ' "${C_YELLOW}" "${C_RESET}" "${default_index}")" answer \
      || answer=""
    answer="${answer:-${default_index}}"
    if [[ "${answer}" =~ ^[0-9]+$ ]] && (( answer >= 1 && answer <= count )); then
      printf -v "${__var}" '%s' "${values[$((answer - 1))]}"
      ok "${labels[$((answer - 1))]}"
      return 0
    fi
    warn "Введите число от 1 до ${count}."
  done
}

# Свободный ввод с проверкой.
#
#   wizard_ask ПЕРЕМЕННАЯ "Вопрос" "значение по умолчанию" [проверка] [пояснение]
#
# Проверка — имя функции, которой передаётся введённое значение; она печатает
# причину отказа и возвращает ненулевой код, если значение не годится.
wizard_ask() {
  local __var="$1" question="$2" default="$3" validator="${4:-}" note="${5:-}"

  if ! wizard_interactive; then
    printf -v "${__var}" '%s' "${default}"
    return 0
  fi

  [[ -n "${note}" ]] && printf '\n%s%s%s\n' "${C_DIM}" "${note}" "${C_RESET}"
  local answer
  while true; do
    read -r -p "$(printf '%s%s%s [%s]: ' "${C_BOLD}" "${question}" "${C_RESET}" "${default}")" \
      answer || answer=""
    answer="${answer:-${default}}"
    if [[ -z "${validator}" ]] || "${validator}" "${answer}"; then
      printf -v "${__var}" '%s' "${answer}"
      return 0
    fi
  done
}

# Отметить несколько пунктов из списка. Возвращает значения через запятую.
#
#   wizard_multi ПЕРЕМЕННАЯ "Вопрос" "1,3" "значение|Название|Пояснение" ...
wizard_multi() {
  local __var="$1" question="$2" default="$3"; shift 3
  local options=("$@")
  local values=() labels=() notes=()

  local option value label note
  for option in "${options[@]}"; do
    IFS='|' read -r value label note <<< "${option}"
    values+=("${value}"); labels+=("${label}"); notes+=("${note}")
  done

  local -a chosen=()
  local pick
  if ! wizard_interactive; then
    IFS=',' read -ra chosen <<< "${default}"
  else
    printf '\n%s\n' "${question}"
    local index
    for ((index = 0; index < ${#values[@]}; index++)); do
      local mark="  "
      [[ ",${default}," == *",$((index + 1)),"* ]] && mark="${C_GREEN}▸${C_RESET} "
      printf '%s%s%2d)%s %s\n' "${mark}" "${C_BOLD}" "$((index + 1))" "${C_RESET}" "${labels[$index]}"
      [[ -n "${notes[$index]}" ]] && printf '      %s%s%s\n' "${C_DIM}" "${notes[$index]}" "${C_RESET}"
    done
    printf '   %sномера через запятую, «все» — всё, «нет» — ничего%s\n' "${C_DIM}" "${C_RESET}"

    local answer
    read -r -p "$(printf '%sВыбор%s [%s]: ' "${C_YELLOW}" "${C_RESET}" "${default}")" answer \
      || answer=""
    answer="${answer:-${default}}"
    case "${answer}" in
      все|всё|all|*)
        if [[ "${answer}" =~ ^(все|всё|all)$ ]]; then
          chosen=($(seq 1 ${#values[@]}))
        elif [[ "${answer}" =~ ^(нет|none|-)$ ]]; then
          chosen=()
        else
          IFS=',' read -ra chosen <<< "${answer}"
        fi ;;
    esac
  fi

  local result=""
  for pick in "${chosen[@]:-}"; do
    pick="${pick// /}"
    [[ "${pick}" =~ ^[0-9]+$ ]] || continue
    (( pick >= 1 && pick <= ${#values[@]} )) || continue
    result+="${result:+,}${values[$((pick - 1))]}"
  done
  printf -v "${__var}" '%s' "${result}"
}

# ---------------------------------------------------------------------------
# Проверки для wizard_ask
# ---------------------------------------------------------------------------

wizard_valid_port() {
  local port="$1"
  if [[ ! "${port}" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
    warn "Порт — число от 1 до 65535."
    return 1
  fi
  if (( port < 1024 )) && ! is_root; then
    warn "Порт ниже 1024 требует прав root."
    return 1
  fi
  if ! check_port_free "${port}" 2>/dev/null; then
    warn "Порт ${port} уже занят."
    if wizard_interactive && confirm "Всё равно использовать?" "n"; then
      return 0
    fi
    return 1
  fi
  return 0
}

wizard_valid_path() {
  local path="$1"
  [[ -n "${path}" ]] || { warn "Путь не может быть пустым."; return 1; }
  [[ "${path}" == /* || "${path}" == ~* ]] || {
    warn "Укажите абсолютный путь."
    return 1
  }
  local parent
  parent="$(dirname "${path}")"
  while [[ ! -d "${parent}" && "${parent}" != "/" ]]; do parent="$(dirname "${parent}")"; done
  if [[ ! -w "${parent}" ]]; then
    warn "Нет прав на запись в ${parent}."
    return 1
  fi
  return 0
}

wizard_valid_host() {
  local host="$1"
  [[ -n "${host}" ]] || { warn "Адрес не может быть пустым."; return 1; }
  return 0
}

# ---------------------------------------------------------------------------
# Сводка перед началом работы
# ---------------------------------------------------------------------------

# Дополнение строки пробелами по числу СИМВОЛОВ, а не байтов: printf %-28s
# в bash считает байты, и кириллица (два байта на символ) ломает колонки.
wizard_pad() {
  local text="$1" width="${2:-28}" length="${#1}"
  printf '%s' "${text}"
  (( length < width )) && printf '%*s' "$((width - length))" ''
}

# wizard_summary "Ключ|Значение" ...
wizard_summary() {
  printf '\n%s%s%s\n' "${C_BOLD}" "Что будет сделано" "${C_RESET}"
  printf '%s%s%s\n' "${C_DIM}" "$(printf '─%.0s' $(seq 1 68))" "${C_RESET}"
  local row key value
  for row in "$@"; do
    IFS='|' read -r key value <<< "${row}"
    printf '  %s%s%s %s\n' "${C_DIM}" "$(wizard_pad "${key}" 24)" "${C_RESET}" "${value}"
  done
  printf '\n'
}
