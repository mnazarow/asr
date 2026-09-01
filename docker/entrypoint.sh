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

SUBDIRS=(uploads results models logs tmp)

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

# Без ffmpeg доступны только файлы WAV
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: ffmpeg не найден в образе — читаться будут только WAV" >&2
fi

# Сообщаем об обнаруженном ускорителе
python - <<'PYEOF' || true
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
