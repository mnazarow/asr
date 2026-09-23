"""Заход 39: «Лишнее от прежней версии» — само, а не подсказкой.

Чек-лист обновления на рабочем сервере третий раз подряд выходил с «!» в
пункте «Обновление зависимостей»: optimum-onnx, поставленный версией 3.1.7
и снятый из требований в 3.1.8, так и оставался в окружении и жаловался на
transformers. Подсказка с командой удаления дошла до экрана (3.1.9), но
осталась подсказкой — а пакет туда поставили мы сами.

Проверки здесь — поведенческие: поддельный pip с памятью помнит, что с него
сняли, и отвечает на `pip check` по тому, что в нём осталось. Скрипты
гоняются настоящим bash и настоящим PowerShell.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
PWSH = shutil.which("pwsh") or shutil.which("powershell")
нужен_bash = pytest.mark.skipif(BASH is None, reason="нужен bash")
нужен_pwsh = pytest.mark.skipif(PWSH is None, reason="нужен PowerShell")
ОБЩЕЕ = КОРЕНЬ / "scripts" / "lib" / "common.sh"
МОДУЛЬ = КОРЕНЬ / "scripts" / "lib" / "Common.psm1"

# Ровно то, что пришло с рабочего сервера.
ЖАЛОБА = ("optimum-onnx 0.1.0 has requirement transformers<4.58.0,>=4.36, "
          "but you have transformers 5.17.0.")


# --------------------------------------------------------------------------
# Поддельный pip с памятью
# --------------------------------------------------------------------------


def _pip(куда: Path, установлено: dict[str, str],
         жалобы: dict[str, str] | None = None, отказ: bool = False) -> Path:
    """pip, который помнит, что с него сняли.

    установлено — {пакет: "кто на него опирается, через запятую"};
    жалобы — {пакет: строка pip check}: жалоба уходит вместе с пакетом;
    отказ — `pip uninstall` завершается ошибкой и ничего не снимает.
    Все вызовы пишутся в журнал рядом — по нему видно, что именно просили.
    """
    куда.mkdir(parents=True, exist_ok=True)
    показ = "\n".join(
        f'    {имя}) printf "Name: {имя}\\nVersion: 1.0\\nRequired-by: {кто}\\n" ;;'
        for имя, кто in установлено.items())
    проверка = "\n".join(
        f'grep -qxF "gone:{имя}" "$state" || {{ echo "{строка}"; broken=1; }}'
        for имя, строка in (жалобы or {}).items())
    pip = куда / "pip"
    pip.write_text(
        "#!/usr/bin/env bash\n"
        'state="$(dirname "$0")/state"; touch "$state"\n'
        'echo "call: $*" >> "$(dirname "$0")/calls"\n'
        'case "$1" in\n'
        "  show)\n"
        '    grep -qxF "gone:$2" "$state" && exit 1\n'
        '    case "$2" in\n'
        f"{показ}\n"
        '      *) echo "WARNING: Package(s) not found: $2" >&2; exit 1 ;;\n'
        "    esac ;;\n"
        "  uninstall)\n"
        f"    {'exit 1' if отказ else ':'}\n"
        '    shift; [ "$1" = "-y" ] && shift\n'
        '    for p in "$@"; do echo "gone:$p" >> "$state"; '
        'echo "Successfully uninstalled $p"; done ;;\n'
        "  check)\n"
        "    broken=0\n"
        f"    {проверка or ':'}\n"
        '    [ "$broken" = 1 ] && exit 1\n'
        '    echo "No broken requirements found."; exit 0 ;;\n'
        "esac\n"
        "exit 0\n", encoding="utf-8")
    pip.chmod(0o755)
    return pip


def _снято(pip: Path) -> set[str]:
    state = pip.parent / "state"
    if not state.exists():
        return set()
    return {с[5:] for с in state.read_text(encoding="utf-8").split() if с.startswith("gone:")}


def _вызовы(pip: Path) -> str:
    calls = pip.parent / "calls"
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def _требования(куда: Path, текст: str = "onnxruntime>=1.18\ntransformers>=5\n") -> Path:
    каталог = куда / "requirements"
    каталог.mkdir(parents=True, exist_ok=True)
    (каталог / "base.txt").write_text(текст, encoding="utf-8")
    return каталог


def _bash(скрипт: str, **окружение: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [BASH, "-c", f'source "{ОБЩЕЕ}"; {скрипт}'], capture_output=True, text=True,
        env={**os.environ, "ASRHUB_QUIET": "0", "ASRHUB_DRY_RUN": "0", **окружение},
        timeout=120)


def _pwsh(скрипт: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoProfile", "-Command", f"Import-Module '{МОДУЛЬ}' -Force; {скрипт}"],
        capture_output=True, text=True, timeout=180)


# Рабочий сервер: optimum-onnx не нужен никому, optimum держится только за него.
СЕРВЕР = {"optimum-onnx": "", "optimum": "optimum-onnx"}


# --------------------------------------------------------------------------
# Linux: снятие того, что ставили прежние версии
# --------------------------------------------------------------------------


@нужен_bash
def test_оставшееся_от_прежних_версий_убирается_само(tmp_path: Path):
    """Случай с рабочего сервера целиком: пункт выходит с «✓», а не с «!».

    Снимаются оба: optimum держится только за optimum-onnx, который уходит
    вместе с ним. После снятия pip check чист — и проверка это видит, потому
    что идёт после снятия, а не до.
    """
    pip = _pip(tmp_path / "venv", СЕРВЕР, {"optimum-onnx": ЖАЛОБА})
    треб = _требования(tmp_path)
    итог = _bash(f'step "Обновление зависимостей"; '
                 f'remove_retired_packages "{pip}" "{треб}"; '
                 f'check_dependency_health "{pip}" "{треб}"; checklist_print 0')
    вывод = итог.stdout + итог.stderr
    assert _снято(pip) == {"optimum-onnx", "optimum"}, _вызовы(pip)
    assert "✓  1. Обновление зависимостей" in вывод, вывод
    # Сделанное видно и в самом чек-листе — заметкой под пунктом, а не только
    # строкой, которая к концу обновления уедет за край экрана.
    чек_лист = вывод[вывод.index("Чек-лист"):]
    assert "Убрано оставшееся от прежних версий: optimum, optimum-onnx" in чек_лист, вывод
    assert "не сходятся" not in вывод, вывод
    assert "uninstall -y" not in вывод, "команда для человека там, где всё сделано"


@нужен_bash
def test_оставленный_пакет_удерживает_свою_опору(tmp_path: Path):
    """На optimum-onnx держится чужой пакет — остаются оба.

    optimum-onnx нужен кому-то ещё, значит, он остаётся; а раз он остаётся,
    остаётся и optimum, на котором он стоит, хотя кроме optimum-onnx на
    optimum не опирается никто. Проверка в один проход сняла бы optimum из-под
    живого пакета.
    """
    pip = _pip(tmp_path / "venv", {"optimum-onnx": "some-engine", "optimum": "optimum-onnx"})
    треб = _требования(tmp_path)
    итог = _bash(f'remove_retired_packages "{pip}" "{треб}"')
    assert итог.returncode == 0, итог.stderr
    assert _снято(pip) == set(), _вызовы(pip)
    assert "uninstall" not in _вызовы(pip)


@нужен_bash
def test_вернувшийся_в_требования_пакет_не_трогается(tmp_path: Path):
    """Строка списка устарела: пакет снова нужен — снимать его нельзя.

    Семья считается как у подсказки: optimum-onnx — спутник требуемого
    optimum, и уходит он только вместе с требованием на optimum.
    """
    pip = _pip(tmp_path / "venv", СЕРВЕР)
    треб = _требования(tmp_path, "optimum[onnxruntime]>=1.20\n")
    итог = _bash(f'remove_retired_packages "{pip}" "{треб}"')
    assert итог.returncode == 0, итог.stderr
    assert _снято(pip) == set(), _вызовы(pip)


@нужен_bash
def test_неустановленное_не_снимается_и_не_упоминается(tmp_path: Path):
    """Чистое окружение: ни вызова uninstall, ни строки «убрано»."""
    pip = _pip(tmp_path / "venv", {})
    треб = _требования(tmp_path)
    итог = _bash(f'step "Зависимости"; remove_retired_packages "{pip}" "{треб}"; '
                 f'checklist_print 0')
    assert "uninstall" not in _вызовы(pip)
    assert "Убрано" not in итог.stdout + итог.stderr


@нужен_bash
def test_пробный_запуск_ничего_не_снимает(tmp_path: Path):
    pip = _pip(tmp_path / "venv", СЕРВЕР)
    треб = _требования(tmp_path)
    итог = _bash(f'remove_retired_packages "{pip}" "{треб}"', ASRHUB_DRY_RUN="1")
    assert _снято(pip) == set()
    assert "uninstall" not in _вызовы(pip)
    assert "Пробный запуск: сняли бы" in итог.stdout + итог.stderr


@нужен_bash
def test_отказ_удаления_оставляет_команду_человеку(tmp_path: Path):
    """Не вышло — пункт с «!» и та самая команда, что раньше была подсказкой."""
    pip = _pip(tmp_path / "venv", СЕРВЕР, отказ=True)
    треб = _требования(tmp_path)
    итог = _bash(f'step "Обновление зависимостей"; '
                 f'remove_retired_packages "{pip}" "{треб}"; checklist_print 0')
    вывод = итог.stdout + итог.stderr
    assert "!  1. Обновление зависимостей" in вывод, вывод
    assert f"{pip} uninstall -y optimum optimum-onnx" in вывод, вывод


@нужен_bash
def test_чужой_лишний_пакет_не_снимается_а_называется(tmp_path: Path):
    """Пакета нет в списке снятых — его происхождения мы не знаем.

    Молча снести чужое хуже, чем напомнить о нём: в окружение сервера руками
    ставят и своё. Такой пакет по-прежнему только называется — и уже не
    «лишним от прежней версии», а тем, что он есть: не из наших списков.
    """
    жалоба = "debugpy 1.0 has requirement packaging<20, but you have packaging 25.0."
    pip = _pip(tmp_path / "venv", {"debugpy": ""}, {"debugpy": жалоба})
    треб = _требования(tmp_path)
    итог = _bash(f'step "Зависимости"; remove_retired_packages "{pip}" "{треб}"; '
                 f'check_dependency_health "{pip}" "{треб}"; checklist_print 0')
    вывод = итог.stdout + итог.stderr
    assert _снято(pip) == set(), _вызовы(pip)
    assert f"{pip} uninstall -y debugpy" in вывод, вывод
    assert "если ставили не вы" in вывод
    assert "от прежней версии" not in вывод, вывод


@нужен_bash
def test_последнее_имя_без_перевода_строки_читается(tmp_path: Path):
    """Файл, сохранённый редактором без перевода строки, не теряет последнее имя."""
    список = tmp_path / "retired.txt"
    список.write_text("# комментарий\nOptimum_ONNX  # хвост\nold.pkg", encoding="utf-8")
    итог = _bash(f'retired_packages "{список}"')
    assert итог.stdout.split() == ["old-pkg", "optimum-onnx"], итог.stdout


@нужен_bash
def test_ключи_pip_не_выдаются_за_пакеты(tmp_path: Path):
    """«-r base.txt» и «--extra-index-url …» — ключи, а не требования."""
    треб = _требования(tmp_path, "-r base.txt\n--extra-index-url https://x/y\ntorch>=2\n"
                                 "protobuf~=5.29\n")
    итог = _bash(f'required_packages "{треб}"')
    assert итог.stdout.split() == ["protobuf", "torch"], итог.stdout


# --------------------------------------------------------------------------
# Список снятых и места вызова
# --------------------------------------------------------------------------


@нужен_bash
def test_список_снятых_не_спорит_с_требованиями():
    """Ни одно снятое имя не вернулось в требования — иначе строка устарела.

    Снимать такой пакет функция и так не станет, но список, который говорит
    одно, а требования — другое, врёт следующему, кто будет его читать.
    """
    снятые = _bash("retired_packages").stdout.split()
    assert "optimum-onnx" in снятые and "optimum" in снятые, снятые
    требования = _bash(f'required_packages "{КОРЕНЬ / "requirements"}"').stdout.split()
    спор = [с for с in снятые
            if any(с == т or с.startswith(f"{т}-") for т in требования)]
    assert not спор, f"снова в требованиях: {спор}"


def test_снятие_идёт_перед_каждой_проверкой():
    """Везде, где подводится итог по версиям, до него снимается снятое.

    Установка, обновление и обе команды models.sh с движками: пропусти одно
    место — и там пункт снова выйдет с «!» из-за того, что мы сами знаем,
    как убрать.
    """
    for имя in ("install.sh", "update.sh", "models.sh"):
        строки = (КОРЕНЬ / "scripts" / имя).read_text(encoding="utf-8").splitlines()
        проверки = [н for н, с in enumerate(строки)
                    if с.strip().startswith("check_dependency_health")]
        assert проверки, f"{имя}: проверки нет вовсе"
        for н in проверки:
            assert строки[н - 1].strip().startswith("remove_retired_packages"), (
                f"{имя}:{н + 1}: перед проверкой не снимается снятое")


@нужен_bash
def test_снятие_движка_захватывает_необязательный_спутник(tmp_path: Path):
    """«remove-engine postprocess» снимает и nemo-text-processing.

    Он лежит в optional/no-deps/postprocess.txt, и без этого файла движок
    снимался без собственной нормализации чисел — её пакет оставался в
    окружении сиротой.
    """
    префикс = tmp_path / "asrhub"
    (префикс / "server").mkdir(parents=True)
    _pip(префикс / "venv" / "bin", {})
    итог = subprocess.run(
        [BASH, str(КОРЕНЬ / "scripts" / "models.sh"), "remove-engine", "postprocess",
         "--prefix", str(префикс), "--yes"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ASRHUB_DRY_RUN": "1", "ASRHUB_QUIET": "0"})
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 0, вывод
    команда = [с for с in вывод.splitlines() if "uninstall -y" in с]
    assert команда, вывод
    assert "nemo-text-processing" in команда[0].split(), команда[0]
    assert "pynini" in команда[0].split(), команда[0]


# --------------------------------------------------------------------------
# Движок узнаётся по собственному пакету
# --------------------------------------------------------------------------


def _первый_пакет(файл: Path) -> str:
    for строка in файл.read_text(encoding="utf-8").splitlines():
        строка = строка.split("#", 1)[0].split("@", 1)[0]
        имя = re.split(r"[<>=!~;\[]", строка, maxsplit=1)[0].strip()
        if имя and not имя.startswith("-"):
            return имя.lower().replace("_", "-").replace(".", "-")
    return ""


def _пакеты(файл: Path) -> set[str]:
    имена = set()
    for строка in файл.read_text(encoding="utf-8").splitlines():
        строка = строка.split("#", 1)[0].split("@", 1)[0]
        имя = re.split(r"[<>=!~;\[]", строка, maxsplit=1)[0].strip()
        if имя and not имя.startswith("-"):
            имена.add(имя.lower().replace("_", "-").replace(".", "-"))
    return имена


# Движки, которых узнают по общему пакету, — и почему это не ошибка.
ОБЩИЙ_ПАКЕТ_НАРОЧНО = {
    "transformers": "движок и есть transformers",
    "postprocess": "пунктуация работает на любом transformers",
    "diarization": "движок и есть pyannote.audio; whisperx просто тянет его же",
}


def test_движок_узнаётся_по_собственному_пакету():
    """Первая строка файла движка — его пакет, а не общий с соседями.

    По ней обновление решает, установлен ли движок. У Voxtral первым стоял
    transformers, и обновление считало его установленным у всех, у кого есть
    transformers, — доставляя mistral-common тем, кто Voxtral не выбирал.
    """
    каталог = КОРЕНЬ / "requirements" / "engines"
    свои = {}
    for файл in sorted(каталог.glob("*.txt")):
        спутник = каталог / "no-deps" / файл.name
        свои[файл.stem] = _первый_пакет(спутник if спутник.exists() else файл)
    чужие = {}
    for файл in sorted(каталог.glob("*.txt")):
        for имя in _пакеты(файл):
            чужие.setdefault(имя, set()).add(файл.stem)
    спорные = []
    for движок, пакет in свои.items():
        if not пакет or движок in ОБЩИЙ_ПАКЕТ_НАРОЧНО:
            continue
        соседи = чужие.get(пакет, set()) - {движок}
        if соседи:
            спорные.append(f"{движок}: «{пакет}» есть и у {sorted(соседи)}")
    assert not спорные, "\n".join(спорные)
    assert свои["voxtral"] == "mistral-common"
    assert свои["gigaam"] == "gigaam", "собственный пакет GigaAM — в no-deps"


# --------------------------------------------------------------------------
# Windows: то же самое, и те же движки
# --------------------------------------------------------------------------


@нужен_pwsh
@нужен_bash
@pytest.mark.parametrize("установлено", [
    СЕРВЕР,
    {"optimum-onnx": "some-engine", "optimum": "optimum-onnx"},
    {"optimum": ""},
    {"optimum-onnx": "", "optimum": "optimum-onnx, whisperx"},
], ids=["сервер", "нужен-чужому", "один-optimum", "optimum-нужен-whisperx"])
def test_windows_снимает_ровно_то_же_что_linux(tmp_path: Path, установлено):
    """Двойники обязаны сходиться: на сервере убралось — и на ноутбуке."""
    треб = _требования(tmp_path)
    linux = _pip(tmp_path / "linux", установлено)
    windows = _pip(tmp_path / "windows", установлено)
    _bash(f'remove_retired_packages "{linux}" "{треб}"')
    итог = _pwsh(f"Remove-RetiredPackages -Pip '{windows}' -RequirementsDir '{треб}'")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert _снято(windows) == _снято(linux), (_вызовы(linux), _вызовы(windows))


@нужен_pwsh
def test_windows_снимает_оставшееся_от_прежних_версий(tmp_path: Path):
    """Не только «так же, как Linux», но и само по себе — то, что нужно."""
    pip = _pip(tmp_path / "venv", СЕРВЕР)
    итог = _pwsh(f"Remove-RetiredPackages -Pip '{pip}' "
                 f"-RequirementsDir '{_требования(tmp_path)}'")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert _снято(pip) == {"optimum", "optimum-onnx"}
    assert "Убрано оставшееся от прежних версий" in итог.stdout


@нужен_pwsh
def test_windows_не_трогает_вернувшееся_в_требования(tmp_path: Path):
    """Та же сверка с требованиями, что и на Linux, — вместе с семьёй имени."""
    pip = _pip(tmp_path / "venv", СЕРВЕР)
    треб = _требования(tmp_path, "optimum[onnxruntime]>=1.20\n")
    итог = _pwsh(f"Remove-RetiredPackages -Pip '{pip}' -RequirementsDir '{треб}'")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert _снято(pip) == set(), _вызовы(pip)


@нужен_pwsh
def test_windows_узнаёт_движок_по_собственному_пакету(tmp_path: Path):
    """update.ps1 годилась любая строка файла — теперь как в update.sh.

    Стоит только pyannote.audio (ради диаризации): диаризация установлена,
    whisperx — нет. Стоит hydra-core из окружения GigaAM, а сам gigaam — нет:
    GigaAM не установлен.
    """
    pip = _pip(tmp_path / "venv", {"pyannote.audio": "", "hydra-core": ""})
    каталог = КОРЕНЬ / "requirements" / "engines"
    итог = _pwsh("; ".join(
        f"'{движок}=' + (Test-EngineInstalled -Pip '{pip}' "
        f"-Requirements '{каталог / (движок + '.txt')}')"
        for движок in ("diarization", "whisperx", "gigaam")))
    assert итог.returncode == 0, итог.stdout + итог.stderr
    ответы = dict(с.split("=", 1) for с in итог.stdout.split() if "=" in с)
    assert ответы == {"diarization": "True", "whisperx": "False", "gigaam": "False"}, ответы

    сам = _pip(tmp_path / "venv2", {"gigaam": ""})
    итог = _pwsh(f"Test-EngineInstalled -Pip '{сам}' -Requirements '{каталог / 'gigaam.txt'}'")
    assert итог.stdout.strip() == "True", итог.stdout + итог.stderr


@нужен_pwsh
def test_windows_снимает_движок_со_спутниками():
    """Тем же набором, каким ставился: gigaam из no-deps, NeMo ITN из optional."""
    каталог = КОРЕНЬ / "requirements" / "engines"
    итог = _pwsh(
        f"(Get-EnginePackages -Requirements '{каталог / 'gigaam.txt'}') -join ' '; "
        f"(Get-EnginePackages -Requirements '{каталог / 'postprocess.txt'}') -join ' '; "
        f"(Get-EnginePackages -Requirements '{каталог / 'voxtral.txt'}') -join ' '")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    gigaam, postprocess, voxtral = итог.stdout.strip().splitlines()
    assert "gigaam" in gigaam.split()
    assert {"nemo-text-processing", "pynini", "transformers"} <= set(postprocess.split())
    # Дополнения в скобках — не часть имени: pip их при удалении не ждёт.
    assert voxtral.split() == ["mistral-common", "transformers"], voxtral


@нужен_pwsh
def test_windows_обновление_и_движки_идут_через_общие_функции():
    """update.ps1 и models.ps1 ставили движки голым «pip install -r».

    Без спутников no-deps и optional GigaAM на Windows не обновлялся и не
    ставился командой models.ps1 вовсе, а версии из overrides.txt после
    обновления не возвращались. Проверка разбором: скрипты целиком вне
    Windows не запустить, а опечатка в PowerShell видна только при разборе.
    """
    обновление = (КОРЕНЬ / "scripts" / "update.ps1").read_text(encoding="utf-8")
    модели = (КОРЕНЬ / "scripts" / "models.ps1").read_text(encoding="utf-8")
    установка = (КОРЕНЬ / "scripts" / "install.ps1").read_text(encoding="utf-8")
    for нужное in ("Test-EngineInstalled", "Install-EngineRequirements",
                   "Install-Overrides", "Remove-RetiredPackages"):
        assert нужное in обновление, f"update.ps1 не зовёт {нужное}"
    assert обновление.index("Install-Overrides") < обновление.index("Remove-RetiredPackages")
    assert "Install-EngineRequirements" in модели and "Get-EnginePackages" in модели
    assert "Remove-RetiredPackages" in установка
    assert "'-r', $req" not in модели, "models.ps1 всё ещё ставит голый файл"
    assert "'-r', $req.FullName" not in обновление, "update.ps1 всё ещё ставит голый файл"

    for файл in ("update.ps1", "models.ps1", "install.ps1", "lib/Common.psm1"):
        путь = КОРЕНЬ / "scripts" / файл
        итог = subprocess.run(
            [PWSH, "-NoProfile", "-Command",
             "$e = $null; $null = [System.Management.Automation.Language.Parser]"
             f"::ParseFile('{путь}', [ref]$null, [ref]$e); "
             "if ($e -and $e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"],
            capture_output=True, text=True, timeout=120)
        assert итог.returncode == 0, f"{файл}: {итог.stdout}{итог.stderr}"


@нужен_pwsh
def test_invoke_checked_не_берёт_чужой_код_возврата():
    """Код возврата прошлой программы не делает сбойной успешную функцию.

    Комментарий в Invoke-Checked обещал сброс $LASTEXITCODE перед вызовом, а
    сброса не было: функция PowerShell не трогает $LASTEXITCODE, и после
    любой программы, вышедшей с ошибкой, успешный вызов объявлялся сбоем.
    """
    итог = _pwsh(
        "function Test-Ok { 'сделано' }; "
        f"& '{BASH or 'bash'}' -c 'exit 3'; "
        "try { $r = Invoke-Checked -Command 'Test-Ok'; \"итог=$r\" } "
        "catch { \"сбой=$_\" }")
    assert "итог=сделано" in итог.stdout, итог.stdout + итог.stderr
