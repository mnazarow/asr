#!/usr/bin/env bash
# Точка входа контейнера ASR Hub: проверка окружения перед запуском сервера.
set -euo pipefail

DATA_DIR="${ASRHUB_DATA_DIR:-/data}"

echo "ASR Hub $(cat /app/VERSION 2>/dev/null || echo '?') — запуск контейнера"

# Каталоги данных должны быть доступны на запись: типовая ошибка при
# монтировании тома с чужим владельцем.
for sub in uploads results models logs tmp; do
  path="${DATA_DIR}/${sub}"
  mkdir -p "${path}" 2>/dev/null || true
  if [[ ! -w "${path}" ]]; then
    echo "ОШИБКА: нет прав на запись в ${path}" >&2
    echo "Смонтированный том принадлежит другому пользователю." >&2
    echo "Решение: chown -R 1000:1000 <каталог на хосте> либо запуск с --user \$(id -u):\$(id -g)" >&2
    exit 13
  fi
done

# Проверяем ffmpeg — без него доступны только WAV
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: ffmpeg не найден в образе" >&2
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

# Первый запуск: показываем ключ доступа в журнале, иначе его негде взять
if [[ ! -f "${DATA_DIR}/api-key.txt" && "${ASRHUB_AUTH_ENABLED:-true}" != "false" ]]; then
  echo "Первый запуск: ключ доступа будет создан и сохранён в ${DATA_DIR}/api-key.txt"
fi

exec "$@"
