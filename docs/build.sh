#!/usr/bin/env bash
#
# Сборка документации ASR Hub: Markdown -> единый файл -> Word -> PDF.
#
#   bash docs/build.sh                 полная сборка
#   bash docs/build.sh --no-pdf        без обновления оглавления и PDF
#
# Что происходит:
#   1. Справочные разделы (модели, параметры, пресеты) собираются из каталога
#      сервера, поэтому не расходятся с кодом.
#   2. Схемы перерисовываются из исходников в SVG и PNG.
#   3. Главы склеиваются в один Markdown; ссылки между файлами превращаются
#      в названия глав — в Word они всё равно не работают.
#   4. make_reference.py готовит шаблон оформления Word.
#   5. pandoc собирает .docx по этому шаблону.
#   6. fix_docx.py приводит файл к схеме OOXML и оформляет таблицы.
#   7. update_toc.py заполняет оглавление и сохраняет PDF.

set -o errexit
set -o nounset
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
OUT="${BUILD}/ASR Hub — документация.docx"
MAKE_PDF=1

[[ "${1:-}" == "--no-pdf" ]] && MAKE_PDF=0

command -v pandoc >/dev/null || { echo "Нужен pandoc: apt install pandoc"; exit 127; }

mkdir -p "${BUILD}"
cd "${ROOT}"

echo "1/7 Справочные разделы из каталога"
python3 docs/generate.py

echo "2/7 Схемы"
python3 docs/make_diagrams.py

echo "3/7 Сборка единого Markdown"
python3 docs/assemble.py

echo "4/7 Шаблон оформления Word"
python3 docs/make_reference.py "${BUILD}/reference.docx"

echo "5/7 Word"
pandoc "${BUILD}/asr-hub-полная-документация.md" \
  --from=markdown+pipe_tables+backtick_code_blocks+auto_identifiers \
  --metadata-file="${BUILD}/metadata.yaml" \
  --reference-doc="${BUILD}/reference.docx" \
  --toc --toc-depth=3 \
  --resource-path=".:docs" \
  -o "${OUT}"

echo "6/7 Приведение к схеме OOXML и оформление таблиц"
python3 docs/fix_docx.py "${OUT}"

if [[ "${MAKE_PDF}" -eq 1 ]]; then
  echo "7/7 Оглавление и PDF"
  python3 docs/update_toc.py "${OUT}" || {
    echo "  Оглавление не обновлено (нет LibreOffice или модуля uno)."
    echo "  Word заполнит его сам при первом открытии файла."
  }
else
  echo "7/7 пропущено (--no-pdf)"
fi

echo
echo "Готово: ${OUT}"
