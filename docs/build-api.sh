#!/usr/bin/env bash
#
# Сборка документов Word по программному интерфейсу.
#
#   bash docs/build-api.sh                    весь интерфейс: сервис и мониторинг
#   bash docs/build-api.sh --monitoring       только маршруты /api/monitoring/*
#   bash docs/build-api.sh --all              оба документа
#   добавьте --no-pdf, чтобы пропустить оглавление и PDF
#
# Справочник собирается с работающего сервера: описания берутся из схемы
# OpenAPI, примеры ответов — настоящие. Без сервера сборка не имеет смысла,
# поэтому здесь, в отличие от полной документации, его отсутствие — ошибка.
#
# Адрес сервера задаётся переменной ASRHUB_API_BASE, ключ — ASRHUB_API_KEY
# (иначе берётся из api-key.txt в каталоге данных).

set -o errexit
set -o nounset
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
MAKE_PDF=1
VARIANTS=(full)

while [[ $# -gt 0 ]]; do
  case "$1" in
    --monitoring) VARIANTS=(monitoring); shift ;;
    --full)       VARIANTS=(full); shift ;;
    --all)        VARIANTS=(full monitoring); shift ;;
    --no-pdf)     MAKE_PDF=0; shift ;;
    -h|--help)    sed -n '2,17p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Неизвестный параметр: $1" >&2; exit 2 ;;
  esac
done

command -v pandoc >/dev/null || { echo "Нужен pandoc: apt install pandoc"; exit 127; }

mkdir -p "${BUILD}"
cd "${ROOT}"

BASE="${ASRHUB_API_BASE:-http://127.0.0.1:8080}"
KEY="${ASRHUB_API_KEY:-}"

# Мониторинг описан в 17-monitoring-api.md, весь сервис — в api-reference.md.
# Полному документу нужны оба, документу по мониторингу — только второй.
need_full=0
for v in "${VARIANTS[@]}"; do [[ "${v}" == "full" ]] && need_full=1; done

if [[ "${need_full}" -eq 1 ]]; then
  echo "Справочник по всему сервису (с ${BASE})"
  python3 docs/generate_api_full.py "${BASE}" "${KEY}" || {
    echo
    echo "Сервер на ${BASE} недоступен."
    echo "Запустите его и повторите — справочник собирается с живого сервера,"
    echo "иначе примеры ответов будут выдуманными, а маршруты — устаревшими:"
    echo "    python3 -m asrhub --port 8080"
    exit 1
  }
fi

echo "Справочник по мониторингу (с ${BASE})"
python3 docs/generate_api.py "${BASE}" "${KEY}" || {
  echo
  echo "Сервер на ${BASE} недоступен. Запустите его и повторите."
  exit 1
}

# Соответствие варианта его файлам. Держим рядом, чтобы имена не разъезжались
# с теми, что задаёт assemble_api.py.
slug_of()   { [[ "$1" == "monitoring" ]] && echo "asr-hub-monitoring-api" || echo "asr-hub-api"; }
docx_of()   { [[ "$1" == "monitoring" ]] \
              && echo "ASR Hub — программный интерфейс мониторинга.docx" \
              || echo "ASR Hub — программный интерфейс.docx"; }
footer_of() { [[ "$1" == "monitoring" ]] \
              && echo "ASR Hub — программный интерфейс мониторинга" \
              || echo "ASR Hub — программный интерфейс"; }

for variant in "${VARIANTS[@]}"; do
  slug="$(slug_of "${variant}")"
  out="${BUILD}/$(docx_of "${variant}")"

  echo
  echo "=== ${variant}: $(basename "${out}")"

  echo "  сборка единого Markdown"
  python3 docs/assemble_api.py "${variant}"

  echo "  шаблон оформления Word"
  python3 docs/make_reference.py "${BUILD}/reference-${slug}.docx" "$(footer_of "${variant}")"

  echo "  Word"
  pandoc "${BUILD}/${slug}.md" \
    --from=markdown+pipe_tables+backtick_code_blocks+auto_identifiers \
    --metadata-file="${BUILD}/metadata-${slug}.yaml" \
    --reference-doc="${BUILD}/reference-${slug}.docx" \
    --toc --toc-depth=4 \
    --resource-path=".:docs" \
    -o "${out}"

  python3 docs/fix_docx.py "${out}"

  if [[ "${MAKE_PDF}" -eq 1 ]]; then
    echo "  оглавление и PDF"
    python3 docs/update_toc.py "${out}" || {
      echo "  Оглавление не обновлено (нет LibreOffice или модуля uno)."
      echo "  Word заполнит его сам при первом открытии файла."
    }
  fi

  echo "  готово: ${out}"
done
