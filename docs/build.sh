#!/usr/bin/env bash
#
# Сборка документации ASR Hub: Markdown -> единый файл -> Word -> PDF.
#
#   bash docs/build.sh                 полная сборка
#   bash docs/build.sh --no-pdf        без обновления оглавления и PDF
#   bash docs/build.sh --screenshots   сначала снять снимки интерфейса заново
#                                      (docs/make_screenshots.py, нужен playwright)
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
#
# Зависимости: pip install -r requirements/docs.txt, pandoc и LibreOffice
# (подробности — в шапке requirements/docs.txt).

set -o errexit
set -o nounset
set -o pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/build"
OUT="${BUILD}/ASR Hub — документация.docx"
MAKE_PDF=1
SCREENSHOTS=0

for arg in "$@"; do
  case "${arg}" in
    --no-pdf) MAKE_PDF=0 ;;
    --screenshots) SCREENSHOTS=1 ;;
    *) echo "Неизвестный ключ: ${arg} (есть --no-pdf и --screenshots)" >&2; exit 2 ;;
  esac
done

command -v pandoc >/dev/null || { echo "Нужен pandoc: apt install pandoc"; exit 127; }

mkdir -p "${BUILD}"
cd "${ROOT}"

if [ "${SCREENSHOTS}" -eq 1 ]; then
  echo "0/8 Снимки интерфейса"
  PYTHONPATH="${ROOT}/server" python3 docs/make_screenshots.py
fi

echo "1/8 Справочные разделы из каталога"
python3 docs/generate.py

echo "2/8 Справочник API мониторинга"
# Собирается с работающего сервера: примеры ответов настоящие. Если сервер
# не поднят, оставляем прошлую версию файла — сборка не должна падать из-за этого.
ASRHUB_API_BASE="${ASRHUB_API_BASE:-http://127.0.0.1:8080}"
# Ключ передаём так же, как это делает build-api.sh: без него защищённые
# маршруты отвечают 401, и глава собирается с пустыми примерами — молча.
ASRHUB_API_KEY="${ASRHUB_API_KEY:-}"
# Код 3 — «сервер недоступен» (оставляем прошлую версию главы); любой другой
# ненулевой — ошибка генератора, и сборка с ней не должна молча проходить:
# прежде любая ошибка выдавалась за «сервер недоступен».
api_rc=0
python3 docs/generate_api.py "${ASRHUB_API_BASE}" "${ASRHUB_API_KEY}" \
  2>"${TMPDIR:-/tmp}/asrhub-generate-api.err" || api_rc=$?
if [ "${api_rc}" -eq 3 ]; then
  echo "  сервер на ${ASRHUB_API_BASE} недоступен — оставлена прошлая версия"
  echo "  (запустите сервер и повторите, чтобы примеры обновились)"
elif [ "${api_rc}" -ne 0 ]; then
  echo "  генератор справочника API мониторинга упал (код ${api_rc}):" >&2
  cat "${TMPDIR:-/tmp}/asrhub-generate-api.err" >&2
  exit "${api_rc}"
fi

echo "3/8 Схемы"
python3 docs/make_diagrams.py

echo "4/8 Сборка единого Markdown"
python3 docs/assemble.py

echo "5/8 Шаблон оформления Word"
python3 docs/make_reference.py "${BUILD}/reference.docx"

# tex_math_dollars отключён намеренно: формул в документации нет, зато
# полно шаблонов вида ${UNIQUEID} и ${HOME}. Pandoc принимал два доллара
# в одной строке за формулу и съедал всё между ними — вместе с половиной
# строки таблицы.
echo "6/8 Word"
pandoc "${BUILD}/asr-hub-полная-документация.md" \
  --from=markdown+pipe_tables+backtick_code_blocks+auto_identifiers-tex_math_dollars \
  --metadata-file="${BUILD}/metadata.yaml" \
  --reference-doc="${BUILD}/reference.docx" \
  --toc --toc-depth=3 \
  --resource-path=".:docs" \
  -o "${OUT}"

echo "7/8 Приведение к схеме OOXML и оформление таблиц"
python3 docs/fix_docx.py "${OUT}"

if [[ "${MAKE_PDF}" -eq 1 ]]; then
  echo "8/8 Оглавление и PDF"
  python3 docs/update_toc.py "${OUT}" || {
    echo "  Оглавление не обновлено (нет LibreOffice или модуля uno)."
    echo "  Word заполнит его сам при первом открытии файла."
  }
else
  echo "8/8 пропущено (--no-pdf)"
fi

echo
echo "Готово: ${OUT}"
