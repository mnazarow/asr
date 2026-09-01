#!/usr/bin/env bash
#
# Менеджер моделей и движков ASR Hub.
#
#   bash scripts/models.sh list                  список моделей каталога
#   bash scripts/models.sh list --installed      только загруженные
#   bash scripts/models.sh info gigaam-v3-rnnt   карточка модели
#   bash scripts/models.sh download <модель>     загрузить веса
#   bash scripts/models.sh remove <модель>       удалить веса
#   bash scripts/models.sh verify <модель>       проверить целостность
#   bash scripts/models.sh engines               состояние движков
#   bash scripts/models.sh install-engine nemo   установить движок
#   bash scripts/models.sh install-engine mfa    выравнивание (conda + модели)
#   bash scripts/models.sh disk                  сколько занято на диске

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

usage() {
  # Справка — это шапка файла: печатаем комментарии до первой строки кода,
  # чтобы добавленная команда попадала в справку сама собой.
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

PREFIX=""
DATA_DIR="${ASRHUB_DATA_DIR:-}"
ACTION="${1:-list}"; shift || true
[[ "${ACTION}" == "-h" || "${ACTION}" == "--help" || "${ACTION}" == "help" ]] \
  && { usage; exit 0; }
FILTER_INSTALLED=0
FILTER_LANGUAGE=""
FORCE=0

ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --prefix) PREFIX="${2:?}"; shift 2 ;;
    --data)   DATA_DIR="${2:?}"; shift 2 ;;
    --installed) FILTER_INSTALLED=1; shift ;;
    --language) FILTER_LANGUAGE="${2:?}"; shift 2 ;;
    --force)  FORCE=1; shift ;;
    --yes|-y) ASRHUB_ASSUME_YES=1; shift ;;
    --quiet|-q) ASRHUB_QUIET=1; shift ;;
    -h|--help)
      usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done

[[ -z "${PREFIX}" ]] && for c in "/opt/asrhub" "${HOME}/.local/share/asrhub-app" \
  "${HOME}/Library/Application Support/ASRHub" "$(cd "${SCRIPT_DIR}/.." && pwd)"; do
  [[ -d "${c}/server" ]] && { PREFIX="${c}"; break; }; done
[[ -z "${DATA_DIR}" ]] && for c in "/var/lib/asrhub" "${HOME}/.local/share/asrhub" \
  "${HOME}/Library/Application Support/ASRHub/data"; do [[ -d "${c}" ]] && { DATA_DIR="${c}"; break; }; done
DATA_DIR="${DATA_DIR:-${HOME}/.local/share/asrhub}"
MODELS_DIR="${DATA_DIR}/models"

PY="${PREFIX}/venv/bin/python"
[[ -x "${PY}" ]] || PY="$(check_python 2>/dev/null || echo python3)"

run_python() {
  ( cd "${PREFIX}/server" 2>/dev/null || cd "${SCRIPT_DIR}/../server" ; \
    ASRHUB_MODELS_DIR="${MODELS_DIR}" PYTHONPATH="." "${PY}" - "$@" )
}

# ---------------------------------------------------------------------------

case "${ACTION}" in

list)
  run_python "${FILTER_INSTALLED}" "${FILTER_LANGUAGE}" "${MODELS_DIR}" <<'PYEOF'
import sys, os
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import MODELS, mean_ru_wer

only_installed = sys.argv[1] == "1"
language = sys.argv[2]
models_dir = Path(sys.argv[3])

G, Y, R, D, B, RS = "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"

def local_path(spec):
    if not models_dir.exists():
        return None
    if spec.source.startswith("http"):
        name = spec.source.rsplit("/", 1)[-1].replace(".zip", "")
        for p in models_dir.rglob(f"*{name}*"):
            if p.is_dir():
                return p
        return None
    slug = "models--" + spec.source.replace("/", "--")
    for base in (models_dir, models_dir / "hub"):
        if (base / slug).exists():
            return base / slug
    return None

def size_mb(path):
    if path is None or not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024

rows, families = [], {}
for spec in MODELS:
    if language and language not in spec.languages and not any(
            x.startswith("multi") for x in spec.languages):
        continue
    path = local_path(spec)
    if only_installed and path is None:
        continue
    families.setdefault(spec.family, []).append((spec, path))

quality_mark = {"excellent": f"{G}отличное{RS}", "good": f"{G}хорошее{RS}",
                "fair": f"{Y}среднее{RS}", "poor": f"{R}слабое{RS}", "none": f"{D}—{RS}"}

total_installed = 0
total_mb = 0.0
for family in sorted(families):
    print(f"\n{B}{family}{RS}")
    for spec, path in families[family]:
        mark = f"{G}●{RS}" if path else f"{D}○{RS}"
        wer = mean_ru_wer(spec)
        wer_text = f"{wer:5.1f} %" if wer is not None else "     —"
        mb = size_mb(path)
        if path:
            total_installed += 1
            total_mb += mb
        size_text = f"{mb:7.0f} МБ" if path else (
            f"{spec.disk_mb:7d} МБ" if spec.disk_mb else "        —")
        print(f"  {mark} {spec.id:<34} {quality_mark[spec.ru_quality.value]:<20}"
              f" WER ru {wer_text}  {size_text}  {D}{spec.license}{RS}")

print(f"\n{D}● загружено   ○ не загружено{RS}")
print(f"Загружено моделей: {total_installed}, занято {total_mb/1024:.1f} ГБ")
print(f"Каталог моделей: {models_dir}")
PYEOF
  ;;

info)
  MODEL="${ARGS[0]:?Укажите модель: bash scripts/models.sh info <модель>}"
  run_python "${MODEL}" "${MODELS_DIR}" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model, get_engine, mean_ru_wer

spec = get_model(sys.argv[1])
if spec is None:
    from asrhub.catalog import MODELS
    close = [m.id for m in MODELS if sys.argv[1].lower() in m.id.lower()][:6]
    print(f"Модель «{sys.argv[1]}» не найдена.")
    if close:
        print("Возможно, вы имели в виду: " + ", ".join(close))
    raise SystemExit(1)

B, D, G, Y, RS = "\033[1m", "\033[90m", "\033[32m", "\033[33m", "\033[0m"
print(f"\n{B}{spec.name}{RS}  {D}({spec.id}){RS}\n")
rows = [
    ("Семейство", spec.family), ("Движок", spec.engine),
    ("Источник", spec.source + (f" · ветка {spec.revision}" if spec.revision else "")),
    ("Лицензия", spec.license + ("  — коммерческое использование разрешено"
                                  if spec.commercial_use else "  — НЕкоммерческая")),
    ("Языки", ", ".join(spec.languages)),
    ("Качество на русском", spec.ru_quality.value),
    ("Параметров", f"{spec.params_m} млн" if spec.params_m else "—"),
    ("Размер на диске", f"{spec.disk_mb} МБ" if spec.disk_mb else "—"),
    ("Видеопамять", f"{spec.vram_gb} ГБ" if spec.vram_gb else "—"),
    ("Макс. фрагмент", f"{spec.max_audio_s} с" if spec.max_audio_s else "не ограничен"),
    ("RTFx", f"{spec.rtfx} ({spec.rtfx_hw})" if spec.rtfx else "—"),
    ("Потоковый режим", "да" if spec.streaming else "нет"),
    ("Пунктуация", "да" if spec.punctuation else "нет"),
    ("Диаризация", "да" if spec.diarization else "нет"),
    ("Требует токен HF", "да" if spec.gated else "нет"),
    ("Зрелость", spec.maturity.value), ("Релиз", spec.released or "—"),
]
for key, value in rows:
    print(f"  {key:<22} {value}")

if spec.benchmarks:
    print(f"\n{B}Измерения качества{RS}")
    for b in spec.benchmarks:
        print(f"  {b.dataset:<30} {b.metric} {b.value:6.2f}  {D}{b.language} · {b.source[:60]}{RS}")

if spec.strengths:
    print(f"\n{B}Сильные стороны{RS}")
    for item in spec.strengths:
        print(f"  {G}+{RS} {item}")
if spec.weaknesses:
    print(f"\n{B}Ограничения{RS}")
    for item in spec.weaknesses:
        print(f"  {Y}−{RS} {item}")
if spec.recommended_for:
    print(f"\n{B}Рекомендуется для{RS}\n  " + " · ".join(spec.recommended_for))
if spec.notes:
    print(f"\n{D}{spec.notes}{RS}")

engine = get_engine(spec.engine)
if engine:
    print(f"\n{B}Движок «{engine.name}»{RS}")
    print(f"  {engine.description}")
    if engine.install_notes:
        print(f"  {D}{engine.install_notes}{RS}")
print()
PYEOF
  ;;

download)
  MODEL="${ARGS[0]:?Укажите модель: bash scripts/models.sh download <модель>}"
  ensure_dir "${MODELS_DIR}"
  info "Загрузка модели «${MODEL}» в ${MODELS_DIR}"
  run_python "${MODEL}" "${MODELS_DIR}" "${FORCE}" <<'PYEOF'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model

spec = get_model(sys.argv[1])
if spec is None:
    print(f"Модель «{sys.argv[1]}» не найдена в каталоге.")
    raise SystemExit(1)
models_dir = Path(sys.argv[2]); models_dir.mkdir(parents=True, exist_ok=True)
force = sys.argv[3] == "1"

if spec.engine == "demo":
    print("Демонстрационный движок не требует загрузки весов.")
    raise SystemExit(0)

if spec.gated:
    print("Внимание: модель требует принятия лицензии и токена Hugging Face.")
    if not (os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")):
        print("Задайте токен: export HF_TOKEN=hf_xxx")

if spec.source.startswith("http"):
    import zipfile, urllib.request
    target = models_dir / "vosk"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / spec.source.rsplit("/", 1)[-1]
    print(f"Скачивание {spec.source}")
    def hook(block, block_size, total):
        if total > 0:
            done = min(100, block * block_size * 100 // total)
            print(f"\\r  {done:3d} %", end="", flush=True)
    urllib.request.urlretrieve(spec.source, archive, reporthook=hook)
    print()
    print("Распаковка…")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    archive.unlink(missing_ok=True)
    print(f"Готово: {target}")
    raise SystemExit(0)

try:
    from huggingface_hub import snapshot_download
except ModuleNotFoundError:
    print("Не установлен huggingface_hub. Установите: pip install huggingface_hub")
    raise SystemExit(1)

print(f"Скачивание {spec.source}" + (f" (ветка {spec.revision})" if spec.revision else ""))
print(f"Ожидаемый размер: примерно {spec.disk_mb or '?'} МБ")
try:
    path = snapshot_download(
        repo_id=spec.source, revision=spec.revision or None,
        cache_dir=str(models_dir), force_download=force,
        token=os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
except Exception as exc:
    text = str(exc).lower()
    print(f"\\nОшибка загрузки: {exc}")
    if "401" in text or "gated" in text or "authenticated" in text:
        print("\\nМодель закрыта лицензией. Что делать:")
        print(f"  1. Откройте https://huggingface.co/{spec.source} и примите условия")
        print("  2. Создайте токен: https://huggingface.co/settings/tokens")
        print("  3. export HF_TOKEN=hf_xxxxx и повторите")
    elif "connection" in text or "timeout" in text or "resolve" in text:
        print("\\nПроблема с сетью. Проверьте доступ к huggingface.co.")
        print("Если сервер закрыт, скачайте модель на другой машине и перенесите")
        print(f"каталог {models_dir} целиком.")
    elif "space" in text or "no space" in text:
        print("\\nНа диске не хватает места.")
    raise SystemExit(1)
print(f"Готово: {path}")
PYEOF
  ok "Модель «${MODEL}» загружена"
  ;;

remove)
  MODEL="${ARGS[0]:?Укажите модель}"
  run_python "${MODEL}" "${MODELS_DIR}" <<'PYEOF'
import shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model

spec = get_model(sys.argv[1])
models_dir = Path(sys.argv[2])
if spec is None:
    print("Модель не найдена в каталоге."); raise SystemExit(1)
slug = "models--" + spec.source.replace("/", "--")
candidates = [models_dir / slug, models_dir / "hub" / slug]
if spec.source.startswith("http"):
    name = spec.source.rsplit("/", 1)[-1].replace(".zip", "")
    candidates += list(models_dir.rglob(f"*{name}*"))
removed = 0
for path in candidates:
    if path.exists():
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        shutil.rmtree(path, ignore_errors=True)
        print(f"Удалено: {path} ({size/1024/1024:.0f} МБ)")
        removed += 1
if removed == 0:
    print("Веса модели на диске не найдены.")
PYEOF
  ;;

verify)
  MODEL="${ARGS[0]:?Укажите модель}"
  info "Проверка модели «${MODEL}»"
  run_python "${MODEL}" "${MODELS_DIR}" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model
from asrhub.engines import ENGINE_CLASSES

spec = get_model(sys.argv[1])
if spec is None:
    print("Модель не найдена."); raise SystemExit(1)
models_dir = Path(sys.argv[2])
slug = "models--" + spec.source.replace("/", "--")
path = next((p for p in (models_dir / slug, models_dir / "hub" / slug) if p.exists()), None)
print(f"Веса на диске:   {'да, ' + str(path) if path else 'нет'}")
if path:
    files = [f for f in path.rglob("*") if f.is_file()]
    size = sum(f.stat().st_size for f in files)
    print(f"Файлов:          {len(files)}")
    print(f"Размер:          {size/1024/1024:.0f} МБ (в каталоге заявлено {spec.disk_mb or '?'} МБ)")
    if spec.disk_mb and size / 1024 / 1024 < spec.disk_mb * 0.5:
        print("ВНИМАНИЕ: размер заметно меньше ожидаемого — возможно, загрузка не завершилась.")
        print(f"Перезагрузить: bash scripts/models.sh download {spec.id} --force")
cls = ENGINE_CLASSES.get(spec.engine)
if cls:
    available, reason = cls.check_available()
    print(f"Движок {spec.engine}: {'установлен' if available else 'НЕ установлен'}")
    if not available:
        print(f"  {reason}")
PYEOF
  ;;

engines)
  run_python <<'PYEOF'
import sys
sys.path.insert(0, ".")
from asrhub.engines import engine_status
G, R, D, B, RS = "\033[32m", "\033[31m", "\033[90m", "\033[1m", "\033[0m"
print(f"\n{B}Движки распознавания{RS}\n")
for item in engine_status():
    mark = f"{G}✓{RS}" if item["available"] else f"{R}✕{RS}"
    print(f"  {mark} {item['id']:<18} {item['name']:<32} {D}{item['license']}{RS}")
    if not item["available"]:
        print(f"      {D}{item['reason']}{RS}")
        print(f"      {D}Установить: bash scripts/models.sh install-engine {item['id']}{RS}")
print()
PYEOF
  ;;

install-engine)
  ENGINE="${ARGS[0]:?Укажите движок: bash scripts/models.sh install-engine <движок>}"
  REQ="${PREFIX}/requirements/engines/${ENGINE//_/-}.txt"
  [[ -f "${REQ}" ]] || REQ="${SCRIPT_DIR}/../requirements/engines/${ENGINE//_/-}.txt"
  if [[ ! -f "${REQ}" ]]; then
    error "Нет файла зависимостей для движка «${ENGINE}»."
    hint "Доступные: $(ls "${SCRIPT_DIR}/../requirements/engines/" 2>/dev/null | sed 's/.txt//' | tr '\n' ' ')"
    exit 2
  fi
  VPIP="${PREFIX}/venv/bin/pip"
  [[ -x "${VPIP}" ]] || { error "Не найдено виртуальное окружение: ${VPIP}"; exit 1; }

  ACCEL="$(detect_gpu)"
  if [[ "${ENGINE}" == "faster_whisper" && "${ACCEL}" == "cuda" ]]; then
    PIN="$(ctranslate2_pin cuda)"
    info "Совместимая версия CTranslate2 для вашей CUDA: ${PIN}"
    retry 2 run "${VPIP}" install --disable-pip-version-check "${PIN}" || true
  fi
  if [[ "${ENGINE}" == "nemo" ]]; then
    warn "NeMo тянет тяжёлый и конфликтный стек зависимостей."
    hint "Если после установки перестанут работать другие движки — используйте отдельное окружение."
    install_system_packages $(system_package_names sndfile) ffmpeg 2>/dev/null || true
  fi
  # MFA живёт в conda-forge: Kaldi и OpenFst в PyPI отсутствуют, поэтому
  # обычной установкой пакетов Python здесь не обойтись.
  if [[ "${ENGINE}" == "mfa" ]]; then
    if have mfa; then
      ok "MFA уже установлен: $(mfa version 2>/dev/null | head -1)"
    else
      MAMBA=""
      for candidate in micromamba mamba conda; do have "${candidate}" && { MAMBA="${candidate}"; break; }; done
      if [[ -z "${MAMBA}" ]]; then
        error "Для MFA нужен conda, mamba или micromamba — в PyPI его нет."
        hint "Быстрая установка micromamba:"
        hint "  curl -Ls https://micro.mamba.pm/api/micromamba/\$(uname -s | tr A-Z a-z)-64/latest \\"
        hint "    | tar -xvj -C /usr/local/bin --strip-components=1 bin/micromamba"
        hint "Затем повторите: bash scripts/models.sh install-engine mfa"
        exit 127
      fi
      info "Установка MFA через ${MAMBA} (около 2–3 ГБ)…"
      run "${MAMBA}" create -y -n mfa -c conda-forge montreal-forced-aligner || {
        error "Не удалось создать окружение MFA."
        exit 1
      }
      MFA_BIN="$(${MAMBA} run -n mfa which mfa 2>/dev/null || true)"
      [[ -n "${MFA_BIN}" ]] && info "MFA установлен: ${MFA_BIN}"
      hint "Добавьте каталог MFA в PATH службы, иначе сервер его не найдёт:"
      hint "  export PATH=\"\$(dirname ${MFA_BIN:-<путь>}):\${PATH}\""
    fi

    LANG_MODEL="${ARGS[1]:-russian_mfa}"
    info "Загрузка акустической модели и словаря «${LANG_MODEL}»…"
    run mfa model download acoustic "${LANG_MODEL}" || warn "Модель не загружена — загрузите вручную."
    run mfa model download dictionary "${LANG_MODEL}" || warn "Словарь не загружен — загрузите вручную."
  fi

  if [[ "${ENGINE}" == "whisperx" ]]; then
    warn "WhisperX жёстко фиксирует версию torch и может сломать другие движки."
    confirm "Всё равно установить в общее окружение?" "n" || exit 0
  fi

  info "Установка движка «${ENGINE}»…"
  if retry 2 run "${VPIP}" install --disable-pip-version-check -r "${REQ}"; then
    ok "Движок «${ENGINE}» установлен"
    "${PREFIX}/venv/bin/python" -c "
import sys; sys.path.insert(0,'${PREFIX}/server')
from asrhub.engines import ENGINE_CLASSES
cls = ENGINE_CLASSES.get('${ENGINE}')
print('Проверка:', cls.check_available() if cls else 'движок неизвестен')" 2>/dev/null || true
  else
    error "Установка не удалась."
    hint "Полный вывод: ${ASRHUB_LOG_FILE:-журнал не велся}"
    exit 1
  fi
  ;;

remove-engine)
  ENGINE="${ARGS[0]:?Укажите движок}"
  REQ="${SCRIPT_DIR}/../requirements/engines/${ENGINE//_/-}.txt"
  [[ -f "${REQ}" ]] || { error "Нет файла зависимостей для «${ENGINE}»."; exit 2; }
  VPIP="${PREFIX}/venv/bin/pip"
  PACKAGES="$(grep -vE '^\s*(#|$|--)' "${REQ}" | sed 's/[<>=!].*//' | tr '\n' ' ')"
  info "Будут удалены пакеты: ${PACKAGES}"
  warn "Некоторые пакеты могут использоваться другими движками."
  confirm "Продолжить?" "n" || exit 0
  run "${VPIP}" uninstall -y ${PACKAGES} || warn "Часть пакетов удалить не удалось."
  ok "Движок «${ENGINE}» удалён"
  ;;

disk)
  heading "Занятое место"
  if [[ -d "${MODELS_DIR}" ]]; then
    printf '  Модели:\n'
    du -sh "${MODELS_DIR}"/* 2>/dev/null | sort -rh | head -25 | sed 's/^/    /'
    printf '\n  Всего моделей: %s\n' "$(du -sh "${MODELS_DIR}" 2>/dev/null | awk '{print $1}')"
  fi
  for sub in uploads results logs tmp; do
    [[ -d "${DATA_DIR}/${sub}" ]] && \
      printf '  %-10s %s\n' "${sub}:" "$(du -sh "${DATA_DIR}/${sub}" 2>/dev/null | awk '{print $1}')"
  done
  [[ -f "${DATA_DIR}/asrhub.db" ]] && \
    printf '  %-10s %s\n' "база:" "$(du -sh "${DATA_DIR}/asrhub.db" 2>/dev/null | awk '{print $1}')"
  printf '\n  Свободно на диске: %s\n\n' "$(df -h "${DATA_DIR}" 2>/dev/null | awk 'NR==2{print $4}')"
  ;;

*)
  error "Неизвестная команда: ${ACTION}"
  sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
  exit 2 ;;
esac
