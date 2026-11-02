#!/usr/bin/env bash
# Точка входа контейнера ASR Hub: подготовка тома и запуск сервера.
#
# Скрипт начинается от root — только так можно поправить владельца
# смонтированного каталога — и сразу понижает права до пользователя asrhub.
# Если контейнер запущен с --user, root недоступен, и мы просто проверяем
# права и говорим, что делать.
set -euo pipefail

DATA_DIR="${ASRHUB_DATA_DIR:-/data}"
RUN_USER="${ASRHUB_UID:-1000}"
RUN_GROUP="${ASRHUB_GID:-1000}"

echo "ASR Hub $(cat /app/VERSION 2>/dev/null || echo '?') — запуск контейнера"
if [[ -n "${ASRHUB_BUILD_PROFILE:-}" ]]; then
  echo "Сборка: профиль ${ASRHUB_BUILD_PROFILE}, ускоритель ${ASRHUB_BUILD_ACCEL:-cpu}"
fi
# Что именно попало в образ. По имени профиля этого не понять: набор движков
# задаётся ещё и ключом ENGINES, а часть движков в образ не входит вовсе.
if [[ -s /opt/venv/.asrhub-engines ]]; then
  echo "Движки в образе: $(tr ' ' ',' < /opt/venv/.asrhub-engines)"
fi

SUBDIRS=(uploads results models logs tmp cache)

if [[ "$(id -u)" == "0" ]]; then
  # Каталог тома создаёт Docker от имени root, поэтому на первом запуске
  # сервер под непривилегированным пользователем не мог в него писать и
  # падал с «Permission denied» ещё до вывода ключа доступа.
  mkdir -p "${DATA_DIR}"
  for sub in "${SUBDIRS[@]}"; do
    mkdir -p "${DATA_DIR}/${sub}"
  done

  # chown делаем только если владелец не совпадает: на большом каталоге
  # моделей рекурсивный проход занимает минуты, и повторять его при каждом
  # перезапуске незачем.
  current_owner="$(stat -c '%u' "${DATA_DIR}")"
  if [[ "${current_owner}" != "${RUN_USER}" ]]; then
    echo "Выставляю владельца ${RUN_USER}:${RUN_GROUP} на ${DATA_DIR}…"
    if ! chown -R "${RUN_USER}:${RUN_GROUP}" "${DATA_DIR}" 2>/dev/null; then
      # Сетевые тома (NFS, SMB, CIFS) владельца менять не дают — это не
      # повод падать, если запись всё равно возможна.
      echo "ВНИМАНИЕ: сменить владельца не удалось (сетевой том?)." >&2
    fi
  fi
  # Владелец верхнего каталога — ещё не владелец всего. Подкаталог, которого
  # не было (каталог на хосте заранее создан пользователем, `data/tmp`
  # удалили, новая версия добавила подкаталог), только что создал root, и
  # проверка записи ниже падала с кодом 13 — на каждом перезапуске, потому
  # что верхний каталог «уже свой». Поэтому — каждый подкаталог отдельно, и
  # файлы, которые сервер читает при старте: config.yaml, записанный
  # установщиком от root, иначе оставался ему недоступным.
  for path in "${SUBDIRS[@]/#/${DATA_DIR}/}" "${DATA_DIR}/config.yaml" "${DATA_DIR}/api-key.txt"; do
    [[ -e "${path}" && ! -L "${path}" ]] || continue
    [[ "$(stat -c '%u' "${path}")" == "${RUN_USER}" ]] && continue
    chown -R "${RUN_USER}:${RUN_GROUP}" "${path}" 2>/dev/null || \
      echo "ВНИМАНИЕ: не удалось сменить владельца ${path}." >&2
  done

  if [[ "${RUN_USER}" != "0" ]]; then
    # Проверяем запись уже от имени будущего пользователя.
    for sub in "${SUBDIRS[@]}"; do
      if ! gosu "${RUN_USER}:${RUN_GROUP}" test -w "${DATA_DIR}/${sub}"; then
        echo "ОШИБКА: нет прав на запись в ${DATA_DIR}/${sub}" >&2
        echo "Смонтированный том не даёт писать пользователю ${RUN_USER}." >&2
        echo "Решение: chown -R ${RUN_USER}:${RUN_GROUP} <каталог на хосте>," >&2
        echo "         либо запуск с --user \$(id -u):\$(id -g)," >&2
        echo "         либо ASRHUB_UID/ASRHUB_GID под владельца тома." >&2
        exit 13
      fi
    done
  fi
else
  # Контейнер запущен с --user: прав на chown нет, только проверяем.
  for sub in "${SUBDIRS[@]}"; do
    path="${DATA_DIR}/${sub}"
    mkdir -p "${path}" 2>/dev/null || true
    if [[ ! -w "${path}" ]]; then
      echo "ОШИБКА: нет прав на запись в ${path}" >&2
      echo "Контейнер запущен от uid $(id -u), а каталог принадлежит другому." >&2
      echo "Решение: chown -R $(id -u):$(id -g) <каталог на хосте>." >&2
      exit 13
    fi
  done
fi

# От имени рабочего пользователя — всё, что после подготовки тома.
as_user() {
  if [[ "$(id -u)" == "0" && "${RUN_USER}" != "0" ]]; then
    gosu "${RUN_USER}:${RUN_GROUP}" "$@"
  else
    "$@"
  fi
}

# Домашний каталог и кеши — на томе. gosu выставляет HOME, только если он
# пуст, а у root он /root: всё, что пишет в ~/.cache (кеш numba у librosa,
# torch hub, pyannote), упиралось в закрытый каталог. При ASRHUB_UID не 1000
# у процесса нет даже записи в passwd. Те же переменные, что задаёт юнит
# systemd.
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-${DATA_DIR}/cache}"
export NUMBA_CACHE_DIR="${NUMBA_CACHE_DIR:-${DATA_DIR}/cache/numba}"
export MPLCONFIGDIR="${MPLCONFIGDIR:-${DATA_DIR}/cache/matplotlib}"
if [[ "$(id -u)" == "0" && "${RUN_USER}" != "0" ]]; then
  run_home="$(getent passwd "${RUN_USER}" 2>/dev/null | cut -d: -f6 || true)"
  if [[ -z "${run_home}" ]] || ! gosu "${RUN_USER}:${RUN_GROUP}" test -w "${run_home}"; then
    run_home="${DATA_DIR}/cache/home"
  fi
  export HOME="${run_home}"
fi
as_user mkdir -p "${HOME}" "${NUMBA_CACHE_DIR}" "${MPLCONFIGDIR}" 2>/dev/null || true

# Файл конфигурации на свежем томе. Без него сервер жил на умолчаниях, а
# всё, что сохраняют в интерфейсе, — ключи доступа, приёмники метрик,
# правила тревог, станции АТС, настройки — оставалось в памяти процесса:
# пересоздание контейнера при обновлении — и интеграция получает 401.
# Создаётся от имени рабочего пользователя, чтобы сервер мог в него писать.
if [[ ! -e "${DATA_DIR}/config.yaml" && -z "${ASRHUB_CONFIG:-}" ]]; then
  echo "Создаю ${DATA_DIR}/config.yaml — в него сервер сохраняет настройки из интерфейса."
  as_user sh -c 'umask 027 && cat > "$1"' sh "${DATA_DIR}/config.yaml" <<'CFGEOF' || \
    echo "ВНИМАНИЕ: не удалось создать ${DATA_DIR}/config.yaml — настройки не переживут перезапуск." >&2
# Конфигурация ASR Hub в контейнере. Сюда сервер сохраняет то, что меняют
# в разделе «Настройки»: ключи доступа, приёмники метрик, станции АТС.
# Полный список параметров: docker compose exec asrhub python -m asrhub --print-config
CFGEOF
fi

# Прежнее имя числа одновременных заданий в .env. Раньше Compose сам
# переводил его в ASRHUB_MAX_CONCURRENT_JOBS (с умолчанием 2, которое
# перекрывало настройку из интерфейса); теперь переменная передаётся как есть,
# и строку «ASRHUB_WORKERS=4», дописанную руками, переводим здесь.
if [[ -n "${ASRHUB_WORKERS:-}" && -z "${ASRHUB_MAX_CONCURRENT_JOBS:-}" ]]; then
  export ASRHUB_MAX_CONCURRENT_JOBS="${ASRHUB_WORKERS}"
fi

# Без ffmpeg доступны только файлы WAV
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: ffmpeg не найден в образе — читаться будут только WAV" >&2
fi

# Сообщаем об обнаруженном ускорителе — от имени рабочего пользователя:
# проба от root при каждом старте создавала в кеше файлы root.
as_user python - <<'PYEOF' || true
try:
    import torch
    if torch.cuda.is_available():
        print(f"Ускоритель: CUDA, устройств {torch.cuda.device_count()}, "
              f"первое — {torch.cuda.get_device_name(0)}")
    else:
        print("Ускоритель: CPU")
except Exception:
    print("PyTorch не установлен — доступны только движки без него")
PYEOF

# Первый запуск: показываем, где искать ключ доступа
if [[ ! -f "${DATA_DIR}/api-key.txt" && "${ASRHUB_AUTH_ENABLED:-true}" != "false" ]]; then
  echo "Первый запуск: ключ доступа будет создан и сохранён в ${DATA_DIR}/api-key.txt"
  echo "Показать его позже: docker compose exec asrhub cat ${DATA_DIR}/api-key.txt"
fi

# Понижаем права и запускаем сервер
if [[ "$(id -u)" == "0" && "${RUN_USER}" != "0" ]]; then
  exec gosu "${RUN_USER}:${RUN_GROUP}" "$@"
fi
exec "$@"
