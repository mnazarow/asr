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
#   bash scripts/models.sh voice ru_RU-irina-medium  голос Piper для опросов
#                                                (ставит piper-tts и качает голос)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/common.sh"
source "${SCRIPT_DIR}/lib/detect.sh"

# Ловушка ошибок и журнал. Без них сбой внутри функции завершал скрипт кодом 1
# молча, а обещание «Полный вывод: <журнал>» в конце неудачной установки
# движка было пустым: журнал никто не заводил.
enable_error_handling
setup_logging "${TMPDIR:-/tmp}"

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

# Какую установку обслуживать: ту, из которой запущен сценарий (её каталог
# данных — из метки установки или её службы). Раньше — первую из списка
# стандартных каталогов, и `models.sh download` второй установки качал через
# окружение первой в её каталог моделей.
resolve_installation "${SCRIPT_DIR}"
if [[ -n "${ASRHUB_INSTALLATIONS}" ]]; then
  case "${ACTION}" in
    list|info|disk) : ;;
    *) explain_ambiguous_installation; exit 2 ;;
  esac
fi
# Запуск из исходников без установки — работаем по ним самим: каталог и
# справочник моделей есть и там.
[[ -z "${PREFIX}" ]] && PREFIX="$(cd "${SCRIPT_DIR}/.." && pwd)"
DATA_DIR="${DATA_DIR:-${HOME}/.local/share/asrhub}"

PY="${PREFIX}/venv/bin/python"
[[ -x "${PY}" ]] || PY="$(check_python 2>/dev/null || echo python3)"

# Каталог моделей — тот же, что у сервера: переменная ASRHUB_MODELS_DIR,
# потом models_dir из конфигурации установки, и только потом
# <данные>/models. Раньше всегда брался последний: переменная из рецепта
# для закрытого сервера (docs/11-troubleshooting.md) перезаписывалась, и
# скачанное уходило не туда, а установка с models_dir на другом диске
# качала, показывала и удаляла модели в каталоге, которого сервер не видит.
MODELS_DIR="${ASRHUB_MODELS_DIR:-}"
# Относительный путь — от каталога, где запустили сценарий: python ниже
# работает из каталога server, и «./модели» указывали бы туда.
[[ -n "${MODELS_DIR}" && "${MODELS_DIR}" != /* ]] && MODELS_DIR="$(pwd)/${MODELS_DIR#./}"
if [[ -z "${MODELS_DIR}" && -d "${PREFIX}/server" ]]; then
  MODELS_DIR="$(cd "${PREFIX}/server" && ASRHUB_DATA_DIR="${DATA_DIR}" "${PY}" -c '
from asrhub.config import load
print(load(apply_hardware=False, ensure_key=False).get("models_dir") or "")' 2>/dev/null || true)"
  # Относительный models_dir сервер понимает от своего рабочего каталога.
  [[ -n "${MODELS_DIR}" && "${MODELS_DIR}" != /* ]] && MODELS_DIR="${PREFIX}/server/${MODELS_DIR#./}"
fi
MODELS_DIR="${MODELS_DIR:-${DATA_DIR}/models}"

run_python() {
  ( cd "${PREFIX}/server" 2>/dev/null || cd "${SCRIPT_DIR}/../server" ; \
    ASRHUB_MODELS_DIR="${MODELS_DIR}" PYTHONPATH=".${PYTHONPATH:+:${PYTHONPATH}}" "${PY}" - "$@" )
}

# ---------------------------------------------------------------------------

case "${ACTION}" in

voice)
  # Голос для контроля качества: Piper (piper-tts) в окружение сервера и
  # файл голоса — в <данные>/qc/piper. Русские голоса: ru_RU-irina-medium,
  # ru_RU-denis-medium, ru_RU-dmitri-medium, ru_RU-ruslan-medium.
  VOICE="${ARGS[0]:-ru_RU-irina-medium}"
  VPIP="${PREFIX}/venv/bin/pip"
  if [[ -x "${VPIP}" && ! -x "${PREFIX}/venv/bin/piper" ]]; then
    info "Установка Piper (piper-tts) в окружение сервера…"
    if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
      "${VPIP}" install --disable-pip-version-check -q piper-tts \
        || { error "piper-tts не установился"; hint "Без сети: pip download piper-tts на другой машине."; exit 1; }
    fi
  fi
  VOICE_DIR="${DATA_DIR}/qc/piper"
  VOICE_MODEL="$(run_python "${VOICE}" "${VOICE_DIR}" <<'PYEOF'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.errors import ASRHubError
from asrhub.qc import tts
try:
    print(tts.скачать_голос_piper(sys.argv[1], Path(sys.argv[2])))
except ASRHubError as exc:
    sys.stderr.write(f"{exc.message} {exc.hint or ''}\n")
    sys.exit(2)
PYEOF
)" || { error "Голос ${VOICE} не скачался"; exit 1; }
  ok "Голос ${VOICE}: ${VOICE_MODEL}"
  PIPER_BIN="${PREFIX}/venv/bin/piper"
  [[ -x "${PIPER_BIN}" ]] || PIPER_BIN="piper"
  hint "В «Настройки» → «Контроль качества» (или config.yaml):"
  printf '  qc_tts_voice: %s\n  qc_tts_command: %s -m {voice} -f {out}\n' "${VOICE_MODEL}" "${PIPER_BIN}"
  ;;

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

# Поиск весов — один на всю программу. Своя копия тут уже была, и она знала
# только раскладку каталогов Hugging Face: после того как GigaAM стал качать
# .ckpt, эта копия показывала бы скачанную модель незагруженной, а её размер
# нулевым. Две копии одного правила расходятся на первой же правке — поэтому
# берём ту, которой пользуется сам сервер.
from asrhub import model_files

def local_path(spec):
    # openai-whisper кладёт веса одним файлом «<имя>.pt» в корень каталога
    # (whisper.load_model с download_root), а короткое имя у него то же, что
    # у faster-whisper: общий поиск находил чужой каталог CTranslate2.
    if spec.engine == "whisper":
        candidate = models_dir / f"{spec.source}.pt"
        return candidate if candidate.exists() else None
    return model_files.find_local(models_dir, spec.source, spec.revision or "")

def size_mb(path):
    return model_files.directory_size(path) / 1024 / 1024

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

# GigaAM качает веса не с Hugging Face, а со своего CDN, и кладёт их одним
# файлом «<вариант>.ckpt». Снапшот репозитория, который тянулся раньше,
# загрузчик библиотеки не открывает никогда: он ищет ровно этот файл в
# download_root. То есть команда отрабатывала успешно, место занимала, а
# движок потом всё равно шёл в интернет — или падал, если интернета нет.
if spec.engine == "gigaam":
    # Загрузка — та же функция, что у раздела «Модели»: качается ровно тот
    # файл, который потом возьмёт движок.
    from asrhub.engines.gigaam_engine import download_weights, variant_name, weights_file
    from asrhub.errors import DependencyMissing
    target = models_dir / weights_file(spec.source, spec.revision or "")
    if target.exists() and not force:
        print(f"Уже загружено: {target}")
        raise SystemExit(0)
    print(f"Скачивание варианта «{variant_name(spec.source, spec.revision or '')}» в {models_dir}")
    try:
        target = download_weights(spec.source, spec.revision or "", models_dir)
    except DependencyMissing:
        print("Движок gigaam не установлен.")
        print("Установите: bash scripts/models.sh install-engine gigaam")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nОшибка загрузки: {exc}")
        raise SystemExit(1)
    print(f"Готово: {target if target.exists() else models_dir}")
    raise SystemExit(0)

# whisper.cpp — один файл ggml из репозитория ggerganov/whisper.cpp. Общая
# дорога ниже принимала «large-v3-turbo» за модель faster-whisper и клала на
# диск чужие веса CTranslate2, а варианты «…-q5_0» не находила вовсе.
if spec.engine == "whisper_cpp":
    from asrhub.engines.whisper_cpp_engine import download_weights
    from asrhub.errors import ASRHubError
    def ход(доля):
        print(f"\r  {доля * 100:5.1f} %", end="", flush=True)
    print(f"Скачивание {spec.source} (примерно {spec.disk_mb or '?'} МБ)")
    try:
        target = download_weights(spec.source, models_dir, force=force,
                                  token=os.environ.get("HF_TOKEN")
                                  or os.environ.get("HUGGING_FACE_HUB_TOKEN"),
                                  progress=ход)
    except ASRHubError as exc:
        print(f"\nОшибка загрузки: {exc.message}")
        if exc.hint:
            print(exc.hint)
        raise SystemExit(1)
    print(f"\nГотово: {target}")
    raise SystemExit(0)

# openai-whisper — один файл «<имя>.pt» его же загрузчиком. Общая дорога ниже
# брала короткое имя («large-v3») за модель faster-whisper и клала на диск
# веса CTranslate2, которые openai-whisper не открывает.
if spec.engine == "whisper":
    from asrhub.engines.whisper_engine import download_weights
    from asrhub.errors import ASRHubError
    from asrhub.model_files import файл_whisper
    target = файл_whisper(models_dir, spec.revision or f"{spec.source}.pt")
    if target.exists() and not force:
        print(f"Уже загружено: {target}")
        raise SystemExit(0)
    if force:
        target.unlink(missing_ok=True)
    print(f"Скачивание {spec.source}.pt (примерно {spec.disk_mb or '?'} МБ)")
    try:
        target = download_weights(spec.source, models_dir)
    except ASRHubError as exc:
        print(f"\nОшибка загрузки: {exc.message}")
        if exc.hint:
            print(exc.hint)
        raise SystemExit(1)
    print(f"Готово: {target}")
    raise SystemExit(0)

if spec.source.startswith("http"):
    import zipfile, urllib.request
    target = models_dir / "vosk"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / spec.source.rsplit("/", 1)[-1]
    print(f"Скачивание {spec.source}")
    def hook(block, block_size, total):
        if total > 0:
            done = min(100, block * block_size * 100 // total)
            print(f"\r  {done:3d} %", end="", flush=True)
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

# Репозиторий — из той же таблицы, по которой сервер ищет веса: у
# faster-whisper источник — короткое имя («small»), а качать надо
# Systran/faster-whisper-small. Раньше запрашивался репозиторий «small»,
# и отказ Hugging Face разбирался как «модель закрыта лицензией».
from asrhub.model_files import huggingface_repo
repo = huggingface_repo(spec.source)
print(f"Скачивание {repo}" + (f" (ветка {spec.revision})" if spec.revision else ""))
print(f"Ожидаемый размер: примерно {spec.disk_mb or '?'} МБ")
try:
    path = snapshot_download(
        repo_id=repo, revision=spec.revision or None,
        cache_dir=str(models_dir), force_download=force,
        token=os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN"))
except Exception as exc:
    text = str(exc).lower()
    print(f"\nОшибка загрузки: {exc}")
    if "401" in text or "gated" in text or "authenticated" in text:
        print("\nМодель закрыта лицензией. Что делать:")
        print(f"  1. Откройте https://huggingface.co/{repo} и примите условия")
        print("  2. Создайте токен: https://huggingface.co/settings/tokens")
        print("  3. export HF_TOKEN=hf_xxxxx и повторите")
    elif "connection" in text or "timeout" in text or "resolve" in text:
        print("\nПроблема с сетью. Проверьте доступ к huggingface.co.")
        print("Если сервер закрыт, скачайте модель на другой машине и перенесите")
        print(f"каталог {models_dir} целиком.")
    elif "space" in text or "no space" in text:
        print("\nНа диске не хватает места.")
    raise SystemExit(1)
print(f"Готово: {path}")
PYEOF
  ok "Модель «${MODEL}» загружена"
  ;;

remove)
  MODEL="${ARGS[0]:?Укажите модель}"
  # Сначала — что именно будет удалено, и вопрос; удаление — вторым
  # проходом. Раньше удаляло сразу: `remove whisper-small` молча стирал
  # веса faster-whisper-small (общий поиск путал модели двух движков с
  # одинаковым коротким именем).
  REMOVE_PY="$(cat <<'PYEOF'
import shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model

spec = get_model(sys.argv[1])
models_dir = Path(sys.argv[2])
apply = sys.argv[3] == "1"
if spec is None:
    print("Модель не найдена в каталоге."); raise SystemExit(1)
from asrhub import model_files

candidates = []
if spec.engine == "whisper":
    # openai-whisper — один файл «<имя>.pt»; каталог Systran/faster-whisper-<имя>
    # принадлежит другой модели и другому движку.
    single = models_dir / f"{spec.source}.pt"
    if single.exists():
        candidates.append(single)
else:
    found = model_files.find_local(models_dir, spec.source, spec.revision or "")
    if found is not None:
        candidates.append(found)
    # Прямые ссылки распаковываются в несколько каталогов сразу, поэтому
    # здесь берём их все, а не только первый.
    if spec.source.startswith("http"):
        name = spec.source.rsplit("/", 1)[-1].replace(".zip", "")
        candidates += [p for p in models_dir.rglob(f"*{name}*") if p not in candidates]
if not candidates:
    print("Веса модели на диске не найдены.")
    raise SystemExit(3)
for path in candidates:
    size = model_files.directory_size(path)
    if not apply:
        print(f"  {path} ({size/1024/1024:.0f} МБ)")
        continue
    # Веса GigaAM и openai-whisper — один файл: rmtree на файле не работает.
    if path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)
    print(f"Удалено: {path} ({size/1024/1024:.0f} МБ)")
PYEOF
)"
  REMOVE_RC=0
  run_python "${MODEL}" "${MODELS_DIR}" 0 <<<"${REMOVE_PY}" || REMOVE_RC=$?
  if [[ "${REMOVE_RC}" -eq 3 ]]; then exit 0; fi
  [[ "${REMOVE_RC}" -eq 0 ]] || exit "${REMOVE_RC}"
  confirm "Удалить перечисленное?" "n" || { info "Отменено."; exit 0; }
  if [[ "${ASRHUB_DRY_RUN}" == "1" ]]; then
    printf '  [пробный запуск] веса модели %s не удалены\n' "${MODEL}"
    exit 0
  fi
  run_python "${MODEL}" "${MODELS_DIR}" 1 <<<"${REMOVE_PY}"
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
from asrhub import model_files

if spec.engine == "whisper":
    # Веса openai-whisper — «<имя>.pt»; общий поиск нашёл бы faster-whisper.
    path = models_dir / f"{spec.source}.pt"
    path = path if path.exists() else None
else:
    path = model_files.find_local(models_dir, spec.source, spec.revision or "")
print(f"Веса на диске:   {'да, ' + str(path) if path else 'нет'}")
if path:
    files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]
    size = model_files.directory_size(path)
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
  # Имя — к одному виду, с подчёркиванием, как у сервера. Подсказка
  # «Доступные:» печатает имена файлов через дефис («faster-whisper»), и с
  # таким именем ветка закрепления CTranslate2 не срабатывала, а проверка
  # после установки отвечала «движок неизвестен».
  ENGINE="${ENGINE//-/_}"
  # whisper.cpp — не пакет Python, а программа, её собирают из исходников.
  # Раньше команда ставила пустой список зависимостей и отвечала «установлен»,
  # а сервер по-прежнему не находил whisper-cli — и подсказка ошибки
  # отправляла ровно сюда же.
  if [[ "${ENGINE}" == "whisper_cpp" || "${ENGINE}" == "whisper-cpp" ]]; then
    # shellcheck source=lib/whispercpp.sh
    source "${SCRIPT_DIR}/lib/whispercpp.sh"
    if ! build_whisper_cpp; then
      error "whisper.cpp собрать не удалось."
      hint "Нужны cmake, git и компилятор C++: apt install cmake git build-essential / xcode-select --install"
      exit 1
    fi
    hint "Перезапустите сервер, чтобы он увидел whisper-cli: bash scripts/service.sh restart"
    exit 0
  fi
  REQ="${PREFIX}/requirements/engines/${ENGINE//_/-}.txt"
  [[ -f "${REQ}" ]] || REQ="${SCRIPT_DIR}/../requirements/engines/${ENGINE//_/-}.txt"
  if [[ ! -f "${REQ}" ]]; then
    error "Нет файла зависимостей для движка «${ENGINE}»."
    hint "Доступные: $(cd "${SCRIPT_DIR}/../requirements/engines" 2>/dev/null \
      && ls -- *.txt 2>/dev/null | sed 's/\.txt$//' | tr '\n' ' ')"
    exit 2
  fi
  VPIP="${PREFIX}/venv/bin/pip"
  [[ -x "${VPIP}" ]] || { error "Не найдено виртуальное окружение: ${VPIP}"; exit 1; }

  ACCEL="$(detect_gpu)"
  ENGINE_FLAGS=(--disable-pip-version-check)
  if [[ "${ENGINE}" == "faster_whisper" && "${ACCEL}" == "cuda" ]]; then
    # Ограничением в ту же команду: отдельная установка ctranslate2==3.24.0
    # тут же отменялась файлом требований движка (см. ctranslate2_pin).
    PIN="$(ctranslate2_pin cuda "${PREFIX}/venv/bin/python")"
    info "Совместимая версия CTranslate2 для вашей CUDA: $(printf '%s' "${PIN}" | tr '\n' ' ')"
    if [[ "${ASRHUB_DRY_RUN}" != "1" ]]; then
      make_temp_dir PIN_DIR
      printf '%s\n' "${PIN}" > "${PIN_DIR}/faster-whisper-constraints.txt"
      ENGINE_FLAGS+=(-c "${PIN_DIR}/faster-whisper-constraints.txt")
    fi
  fi
  if [[ "${ENGINE}" == "nemo" ]]; then
    warn "NeMo тянет тяжёлый и конфликтный стек зависимостей."
    hint "Если после установки перестанут работать другие движки — используйте отдельное окружение."
    install_system_packages $(system_package_names sndfile) ffmpeg 2>/dev/null || true
  fi
  # MFA живёт в conda-forge: Kaldi и OpenFst в PyPI отсутствуют, поэтому
  # обычной установкой пакетов Python здесь не обойтись.
  if [[ "${ENGINE}" == "mfa" ]]; then
    # Команда MFA: из PATH или внутри окружения conda. Окружение
    # создаётся рядом, а не в PATH, и загрузка моделей голым `mfa` сразу
    # после установки падала на «command not found».
    MFA_CMD=()
    if have mfa; then
      MFA_CMD=(mfa)
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
      if [[ -n "${MFA_BIN}" && -x "${MFA_BIN}" ]]; then MFA_CMD=("${MFA_BIN}")
      else MFA_CMD=("${MAMBA}" run -n mfa mfa); fi
    fi

    LANG_MODEL="${ARGS[1]:-russian_mfa}"
    info "Загрузка акустической модели и словаря «${LANG_MODEL}»…"
    run "${MFA_CMD[@]}" model download acoustic "${LANG_MODEL}" || warn "Модель не загружена — загрузите вручную."
    run "${MFA_CMD[@]}" model download dictionary "${LANG_MODEL}" || warn "Словарь не загружен — загрузите вручную."
  fi

  if [[ "${ENGINE}" == "whisperx" ]]; then
    warn "WhisperX жёстко фиксирует версию torch и может сломать другие движки."
    confirm "Всё равно установить в общее окружение?" "n" || exit 0
  fi

  # Каталог требований берём тот же, из которого взят файл движка: в
  # установленной копии это ${PREFIX}/requirements, в репозитории — соседний.
  # Он нужен проверке, чтобы отличить намеренные отступления от находок.
  REQ_ROOT="$(cd "$(dirname "${REQ}")/.." && pwd)"
  export ASRHUB_REQUIREMENTS_DIR="${REQ_ROOT}"

  info "Установка движка «${ENGINE}»…"
  if install_engine_requirements "${VPIP}" "${REQ}" "${ENGINE_FLAGS[@]}"; then
    ok "Движок «${ENGINE}» установлен"
    # Каталог и имя — аргументами, а не текстом программы: путь с апострофом
    # ломал проверку, а имя движка — ввод пользователя.
    "${PREFIX}/venv/bin/python" -c '
import sys; sys.path.insert(0, sys.argv[1])
from asrhub.engines import ENGINE_CLASSES
cls = ENGINE_CLASSES.get(sys.argv[2])
print("Проверка:", cls.check_available() if cls else "движок без собственного класса — это дополнение")' \
      "${PREFIX}/server" "${ENGINE}" 2>/dev/null || true
    # Движок ставится в общее окружение и тянет за собой чужие пакеты. Если
    # он подвинул версию, на которой держится сосед, узнать об этом лучше
    # здесь, а не через неделю по необъяснимому отказу загрузки.
    # Движок мог утащить общий пакет под свой пин — возвращаем заданное нами.
    apply_overrides "${VPIP}" "${REQ_ROOT}"
    remove_retired_packages "${VPIP}" "${REQ_ROOT}"
    check_dependency_health "${VPIP}" "${REQ_ROOT}"
  else
    error "Установка не удалась."
    # Самая частая причина отказа не в самом движке, а в версии Python:
    # колёса torch, onnxruntime и nemo выходят под новые версии с задержкой
    # в месяцы, и pip в этом случае пишет «no matching distributions
    # available for your environment» — фразу, по которой не догадаешься, что
    # менять надо интерпретатор.
    PY_VERSION="$("${PREFIX}/venv/bin/python" -c \
      'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)"
    if [[ -n "${PY_VERSION}" ]] && version_gt "${PY_VERSION}" "${ASRHUB_MAX_PYTHON}"; then
      warn "Окружение собрано на Python ${PY_VERSION} — новее проверенной ${ASRHUB_MAX_PYTHON}."
      hint "Скорее всего, дело в этом: под свежие версии Python нужных колёс"
      hint "ещё нет. Например, GigaAM требует onnxruntime==1.23.*, а у него"
      hint "колёс новее cp313 не выпускали."
      hint "Пересоберите окружение на проверенной версии:"
      hint "  sudo apt install python${ASRHUB_MAX_PYTHON} python${ASRHUB_MAX_PYTHON}-venv python${ASRHUB_MAX_PYTHON}-dev"
      hint "  sudo bash ${PREFIX}/scripts/install.sh --python /usr/bin/python${ASRHUB_MAX_PYTHON} --force"
    fi
    hint "Полный вывод: ${ASRHUB_LOG_FILE:-журнал не велся}"
    exit 1
  fi
  ;;

remove-engine)
  ENGINE="${ARGS[0]:?Укажите движок}"
  REQ="${SCRIPT_DIR}/../requirements/engines/${ENGINE//_/-}.txt"
  [[ -f "${REQ}" ]] || { error "Нет файла зависимостей для «${ENGINE}»."; exit 2; }
  # Путь без «scripts/..»: ниже он сравнивается со списком find, и в
  # ненормализованном виде свои файлы попали бы в «чужие».
  REQ="$(cd "$(dirname "${REQ}")" && pwd)/$(basename "${REQ}")"
  VPIP="${PREFIX}/venv/bin/pip"
  # Читаем и файл-спутник: движок, поставленный из репозитория, перечислен
  # только в нём, и без этого «remove-engine gigaam» снимал зависимости, а
  # сам gigaam оставлял в окружении.
  #
  # `|| true` не для красоты: grep -v возвращает единицу, когда отбросил всё,
  # то есть на файле из одних комментариев — и под errexit скрипт умирал бы
  # молча, ничего не удалив.
  NODEPS="$(dirname "${REQ}")/no-deps/$(basename "${REQ}")"
  # И необязательную часть: она ставится вместе с движком, значит и
  # снимается вместе с ним — иначе в окружении оставался бы пакет, которого
  # в списке движка уже нет.
  OPTIONAL="$(dirname "${REQ}")/optional/$(basename "${REQ}")"
  # И её спутник с --no-deps: у postprocess там nemo-text-processing, и без
  # этой строки движок снимался без собственной нормализации чисел — её
  # пакет оставался в окружении сиротой.
  OPTIONAL_NODEPS="$(dirname "${REQ}")/optional/no-deps/$(basename "${REQ}")"
  OWN_NAMES="$(requirement_names "${REQ}" "${NODEPS}" "${OPTIONAL}" "${OPTIONAL_NODEPS}")"
  # Пакеты, которые нужны кому-то ещё — серверу (base.txt) и другим движкам,
  # — не трогаем. Раньше снималось всё из файлов движка подряд:
  # `remove-engine postprocess` удалял huggingface-hub, на котором держатся
  # загрузка моделей и faster-whisper, `remove-engine gigaam` — soundfile и
  # numpy из base.txt, `remove-engine faster_whisper` — onnxruntime, общий с
  # gigaam, tone и postprocess.
  REQ_ALL="$(cd "$(dirname "${REQ}")/.." && pwd)"
  SHARED_FILES=()
  while IFS= read -r _file; do
    case "${_file}" in
      "${REQ}"|"${NODEPS}"|"${OPTIONAL}"|"${OPTIONAL_NODEPS}") continue ;;
    esac
    SHARED_FILES+=("${_file}")
  done < <(find "${REQ_ALL}" -name '*.txt' 2>/dev/null | LC_ALL=C sort)
  SHARED_NAMES="$(requirement_names ${SHARED_FILES[@]+"${SHARED_FILES[@]}"})"
  PACKAGES=""
  KEPT=""
  while IFS= read -r _name; do
    [[ -n "${_name}" ]] || continue
    if grep -qxF -- "${_name}" <<<"${SHARED_NAMES}"; then
      KEPT="${KEPT} ${_name}"
    else
      PACKAGES="${PACKAGES} ${_name}"
    fi
  done <<<"${OWN_NAMES}"
  PACKAGES="${PACKAGES# }"
  # Нужные не по спискам, а по факту: numpy, regex и им подобные в файлах
  # движка стоят, а держатся на них onnxruntime, soundfile, torch —
  # транзитивно, и никакой файл требований этого не называет. Спрашиваем
  # pip, кто их требует, и оставляем всё, что нужно пакету вне удаляемых —
  # до неподвижной точки: оставленный тянет за собой свои зависимости.
  if [[ -n "${PACKAGES}" && -x "${VPIP}" ]]; then
    # shellcheck disable=SC2086
    REQUIRED_BY="$("${VPIP}" show ${PACKAGES} 2>/dev/null | awk '
      /^Name:/        { name = tolower($2); gsub(/[_.]/, "-", name) }
      /^Required-by:/ { sub(/^Required-by:[ ]*/, ""); gsub(/,/, " "); print name ":" tolower($0) }' || true)"
    CHANGED=1
    while [[ "${CHANGED}" -eq 1 ]]; do
      CHANGED=0
      NEXT=""
      for _name in ${PACKAGES}; do
        _users="$(sed -n "s/^${_name}://p" <<<"${REQUIRED_BY}" | tr '_.' '--' | head -1)"
        _keep=0
        for _user in ${_users}; do
          [[ " ${PACKAGES} " == *" ${_user} "* ]] || { _keep=1; break; }
        done
        if [[ "${_keep}" -eq 1 ]]; then
          KEPT="${KEPT} ${_name}"; CHANGED=1
        else
          NEXT="${NEXT} ${_name}"
        fi
      done
      PACKAGES="${NEXT# }"
    done
  fi
  KEPT="${KEPT# }"
  [[ -n "${KEPT}" ]] && info "Остаются — нужны серверу или другим пакетам: ${KEPT}"
  if [[ -z "${PACKAGES}" ]]; then
    ok "Своих пакетов у движка «${ENGINE}» нет — удалять нечего."
    exit 0
  fi
  info "Будут удалены пакеты: ${PACKAGES}"
  confirm "Продолжить?" "n" || exit 0
  # shellcheck disable=SC2086  # список пакетов — через пробел
  run "${VPIP}" uninstall -y ${PACKAGES} || warn "Часть пакетов удалить не удалось."
  ok "Движок «${ENGINE}» удалён"
  # Удаление ломает окружение не реже установки: предупреждение выше говорит
  # «могут использоваться другими движками», но кем именно — не говорит.
  # Проверка отвечает на это прямо, и сразу, пока понятно, из-за чего.
  REQ_ROOT="$(cd "$(dirname "${REQ}")/.." && pwd)"
  remove_retired_packages "${VPIP}" "${REQ_ROOT}"
  check_dependency_health "${VPIP}" "${REQ_ROOT}"
  ;;

disk)
  heading "Занятое место"
  if [[ -d "${MODELS_DIR}" ]]; then
    printf '  Модели:\n'
    # `head` закрывает трубу, `sort` умирает от SIGPIPE, pipefail делает код
    # 141 и errexit обрывает команду на этом месте: на кеше из тысяч
    # каталогов отчёт кончался, не дойдя ни до итога, ни до свободного места.
    (set +o pipefail; du -sh "${MODELS_DIR}"/* 2>/dev/null | sort -rh | head -25 \
      | sed 's/^/    /') || true
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
  # Та же справка, что по --help. Раньше здесь печатались строки 2–20 файла
  # целиком — шапка к тому времени стала короче, и в справку попадал код.
  usage >&2
  exit 2 ;;
esac
