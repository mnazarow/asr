#!/usr/bin/env bash
#
# Колёса для автономной установки — с файлом хешей.
#
#   bash scripts/wheels.sh КАТАЛОГ                  скачать колёса и записать хеши
#   bash scripts/wheels.sh КАТАЛОГ --no-download    только хеши к уже скачанному
#   bash scripts/wheels.sh КАТАЛОГ --python python3.12
#
# Запускать на машине с сетью и ТОЙ ЖЕ платформы и версии Python, что и
# машина без сети: колёса у пакетов с кодом на C свои под каждую пару.
# Базовые зависимости берутся ровно проверенных версий
# (requirements/constraints.txt). Итог — КАТАЛОГ/requirements-hashes.txt;
# установка с ним:
#
#   bash scripts/install.sh --offline --wheels КАТАЛОГ \
#        --hashes КАТАЛОГ/requirements-hashes.txt
#
# pip ставит только файлы с совпавшим SHA-256: колесо, подменённое на
# флешке или в зеркале, не встанет. Движки распознавания (torch и др.)
# в файл не входят — их колёса зависят ещё и от видеокарты.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DIR=""
DOWNLOAD=1
PY="${PYTHON:-python3}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-download) DOWNLOAD=0; shift ;;
    --python)      PY="${2:?нужен интерпретатор: --python python3.12}"; shift 2 ;;
    -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*)            printf 'Неизвестный параметр: %s\n' "$1" >&2; exit 2 ;;
    *)             DIR="$1"; shift ;;
  esac
done

if [[ -z "${DIR}" ]]; then
  printf 'Укажите каталог для колёс: bash scripts/wheels.sh КАТАЛОГ\n' >&2
  exit 2
fi
command -v "${PY}" >/dev/null 2>&1 || { printf 'Не найден %s\n' "${PY}" >&2; exit 2; }
mkdir -p "${DIR}"

if [[ "${DOWNLOAD}" -eq 1 ]]; then
  printf 'Скачиваю базовые зависимости в %s (Python: %s)…\n' "${DIR}" \
    "$("${PY}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  "${PY}" -m pip download --disable-pip-version-check -d "${DIR}" \
    -r "${ROOT}/requirements/base.txt" -c "${ROOT}/requirements/constraints.txt"
fi

"${PY}" "${SCRIPT_DIR}/lib/wheel_hashes.py" "${DIR}" > "${DIR}/requirements-hashes.txt.tmp"
mv "${DIR}/requirements-hashes.txt.tmp" "${DIR}/requirements-hashes.txt"
printf 'Готово: %s — пакетов: %s.\n' "${DIR}/requirements-hashes.txt" \
  "$(grep -c '==' "${DIR}/requirements-hashes.txt")"
printf 'Установка: bash scripts/install.sh --offline --wheels %s --hashes %s\n' \
  "${DIR}" "${DIR}/requirements-hashes.txt"
