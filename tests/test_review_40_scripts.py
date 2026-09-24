"""Заход 40: скрипты установки, удаления и обслуживания — Linux и Windows.

Здесь то, что ломало работу прямо сейчас: сценарии для Windows PowerShell
5.1 не разбирались вовсе (файлы без метки UTF-8), обновление на Windows
падало на первой же проверке видеокарты, пробный запуск удалял по-настоящему,
а `install.sh --force` — рецепт починки окружения из подсказок — стирал
config.yaml и оставлял сломанное окружение как было.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
PWSH = shutil.which("pwsh") or shutil.which("powershell")
нужен_pwsh = pytest.mark.skipif(PWSH is None, reason="нужен PowerShell")
ФАЙЛЫ_PS = sorted([*(КОРЕНЬ / "scripts").glob("*.ps1"),
                   *(КОРЕНЬ / "scripts" / "lib").glob("*.psm1"),
                   *(КОРЕНЬ / "scripts" / "client").glob("*.ps1")])


def прогнать(команда: str, *, env: dict[str, str] | None = None,
             timeout: int = 300) -> subprocess.CompletedProcess:
    окружение = {**os.environ, "ASRHUB_NO_COLOR": "1", **(env or {})}
    return subprocess.run(["bash", "-c", команда], capture_output=True, text=True,
                          env=окружение, timeout=timeout, check=False)


# ---------------------------------------------------------------------------
# Windows PowerShell 5.1: метка UTF-8
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("файл", ФАЙЛЫ_PS, ids=lambda п: п.name)
def test_сценарии_windows_начинаются_с_метки_utf8(файл: Path):
    """Без метки Windows PowerShell 5.1 читает файл в кодовой странице ANSI.

    Кириллица в UTF-8 тогда превращается в мусор, среди которого — «умные»
    кавычки: «В» даёт `’`, «Г» — `“`, длинное тире — `”`. PowerShell считает
    их кавычками, и ни один из восьми сценариев не разбирался вовсе.
    PowerShell 7 читает UTF-8 и без метки, поэтому на нём этого не видно.
    """
    assert файл.read_bytes().startswith(b"\xef\xbb\xbf"), f"{файл.name} без метки UTF-8"


@нужен_pwsh
def test_сценарии_windows_разбираются_как_их_прочтёт_51(tmp_path: Path):
    """Чтение так, как читает Windows PowerShell 5.1 на русской Windows.

    С меткой — UTF-8, без неё — кодовая страница 1251. Проверяется
    настоящим разборщиком PowerShell, а не глазами.
    """
    сценарий = tmp_path / "разбор.ps1"
    сценарий.write_text(r'''
param([string[]]$Paths)
[System.Text.Encoding]::RegisterProvider([System.Text.CodePagesEncodingProvider]::Instance)
$bad = 0
foreach ($path in $Paths) {
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    if ($bom) { $text = [System.Text.Encoding]::UTF8.GetString($bytes, 3, $bytes.Length - 3) }
    else { $text = [System.Text.Encoding]::GetEncoding(1251).GetString($bytes) }
    $tokens = $null; $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$tokens, [ref]$errors)
    if ($errors.Count) { $bad++; "ОШИБКИ $path : $($errors.Count) : $($errors[0].Message)" }
}
"ИТОГ $bad"
''', encoding="utf-8")
    итог = subprocess.run([PWSH, "-NoProfile", "-File", str(сценарий), "-Paths",
                           *[str(п) for п in ФАЙЛЫ_PS]],
                          capture_output=True, text=True, timeout=120, check=False)
    assert "ИТОГ 0" in итог.stdout, итог.stdout + итог.stderr


@нужен_pwsh
def test_проверка_видеокарты_выходит_из_модуля():
    """`Export-ModuleMember -Function *` стоял раньше трёх функций.

    Экспортируется только то, что определено до него: update.ps1 падал с
    «The term 'Test-GpuRuntime' is not recognized», install.ps1 — на
    последнем шаге, уже после установки, и откатывал её.
    """
    команда = (f"Import-Module '{КОРЕНЬ / 'scripts' / 'lib' / 'Common.psm1'}' -Force; "
               "(Get-Command -Module Common).Name -join ','")
    итог = subprocess.run([PWSH, "-NoProfile", "-Command", команда],
                          capture_output=True, text=True, timeout=120, check=False)
    имена = set(итог.stdout.strip().split(","))
    for нужная in ("Test-GpuRuntime", "Show-GpuRuntimeDiagnosis", "Get-ConfigDevice",
                   "Remove-RetiredPackages", "Set-DryRun"):
        assert нужная in имена, f"{нужная} не выходит из модуля: {итог.stderr}"


@нужен_pwsh
def test_дочерние_сценарии_не_сбрасывают_пробный_запуск(tmp_path: Path):
    """service.ps1 и models.ps1 заново грузили модуль с -Force.

    Модуль общий на процесс, и его состояние сбрасывалось у ВЫЗЫВАЮЩЕГО:
    `uninstall.ps1 -Purge -DryRun` после остановки службы удалял каталоги
    по-настоящему, `update.ps1 -DryRun` менял файлы и ставил пакеты, а
    обычная установка теряла список отката и журнал.
    """
    сценарий = tmp_path / "родитель.ps1"
    сценарий.write_text(fr'''
Import-Module '{КОРЕНЬ / "scripts" / "lib" / "Common.psm1"}' -Force
Set-DryRun $true
Set-AssumeYes $true
$null = Initialize-AsrLog -Directory '{tmp_path}'
Add-Rollback {{ Write-Host 'ОТКАТ' }} 'проба'
& '{КОРЕНЬ / "scripts" / "service.ps1"}' -Action uninstall -Prefix '{tmp_path / "prog"}' -DataDir '{tmp_path / "data"}'
& '{КОРЕНЬ / "scripts" / "models.ps1"}' -Action download -Model gigaam-v3-e2e-rnnt -Prefix '{tmp_path / "prog"}' -DataDir '{tmp_path / "data"}'
$откат = & (Get-Module Common) {{ $script:RollbackActions.Count }}
$согласие = & (Get-Module Common) {{ $script:AssumeYes }}
"ИТОГ DryRun=$(Get-DryRun) AssumeYes=$согласие Откат=$откат Журнал=$([bool](Get-LogFile))"
''', encoding="utf-8-sig")
    итог = subprocess.run([PWSH, "-NoProfile", "-File", str(сценарий)],
                          capture_output=True, text=True, timeout=180, check=False)
    assert "ИТОГ DryRun=True AssumeYes=True Откат=1 Журнал=True" in итог.stdout, \
        итог.stdout + итог.stderr
    assert "[пробный запуск] автозапуск ASRHub: uninstall" in итог.stdout, итог.stdout
    assert "[пробный запуск] models.ps1 -Action download" in итог.stdout, итог.stdout
    assert not (tmp_path / "data" / "models").exists(), "пробный запуск что-то скачивал"


def test_задача_планировщика_знает_свой_каталог_данных():
    """Установка без прав администратора кладёт данные в %LOCALAPPDATA%.

    Сервер из задачи планировщика по умолчанию брал %PROGRAMDATA%\\ASRHub:
    новая пустая база, новый ключ, config.yaml, токен и скачанные модели не
    видны. Задаче переменную окружения не задать — отсюда --config.
    """
    текст = (КОРЕНЬ / "scripts" / "service.ps1").read_text(encoding="utf-8-sig")
    задача = текст[текст.index("function Install-AsTask"):]
    задача = задача[:задача.index("\nfunction ", 10) if "\nfunction " in задача[10:] else None]
    assert "--config" in задача and "$configFile" in задача


def test_пробное_удаление_на_windows_не_трогает_переменную():
    текст = (КОРЕНЬ / "scripts" / "uninstall.ps1").read_text(encoding="utf-8-sig")
    остатки = текст[текст.index("Write-Step 'Проверка остатков'"):]
    assert остатки.index("Get-DryRun") < остатки.index(
        "SetEnvironmentVariable('ASRHUB_DATA_DIR', $null"), \
        "переменная окружения удаляется и при пробном запуске"


def test_force_на_windows_не_стирает_конфигурацию():
    текст = (КОРЕНЬ / "scripts" / "install.ps1").read_text(encoding="utf-8-sig")
    блок = текст[текст.index("$configFile = Join-Path $DataDir 'config.yaml'"):]
    блок = блок[:блок.index("Write-Ok \"Конфигурация: $configFile\"")]
    assert "-not $ResetConfig" in блок and "-not $Force" not in блок
    assert "[switch]$ResetConfig" in текст


# ---------------------------------------------------------------------------
# Поддельные системные программы для bash-сценариев
# ---------------------------------------------------------------------------


def _заглушки(каталог: Path, журнал: Path) -> Path:
    """systemctl, pgrep и прочее — только записывают, что их позвали."""
    каталог.mkdir(parents=True, exist_ok=True)
    заглушки = {
        "systemctl": '''case "$*" in
  *"show -p MainPID"*) echo 0 ;;
  *"is-active"*) echo inactive; exit 3 ;;
  *"show"*"NeedDaemonReload"*) echo no ;;
esac
exit 0''',
        "journalctl": "exit 0",
        "pgrep": "exit 1",
        "apt-get": "exit 0", "apt-cache": "exit 0", "dnf": "exit 0", "yum": "exit 0",
        "brew": "exit 0", "git": "exit 0", "useradd": "exit 0", "sudo": 'exec "$@"',
        "ffmpeg": "exit 0",
    }
    for имя, тело in заглушки.items():
        путь = каталог / имя
        путь.write_text(f'#!/bin/bash\necho "{имя} $*" >> "{журнал}"\n{тело}\n',
                        encoding="utf-8")
        путь.chmod(0o755)
    return каталог


def _поддельный_python(каталог: Path, журнал: Path) -> Path:
    """Python 3.13, который умеет только создавать «окружение» с заглушками."""
    вн_python = каталог / "venv-python"
    вн_python.write_text(f'''#!/bin/bash
echo "venv-python $*" >> "{журнал}"
case "$*" in
  *version_info*) echo "3.13"; exit 0 ;;
  -V|--version) echo "Python 3.13.3"; exit 0 ;;
  "-m pip "*) exit 0 ;;
  "-m asrhub --print-config"*) echo "# пример"; exit 0 ;;
esac
exit 1
''', encoding="utf-8")
    вн_pip = каталог / "venv-pip"
    вн_pip.write_text(f'''#!/bin/bash
echo "venv-pip $*" >> "{журнал}"
case "$1" in
  install) exit 0 ;;
  check) echo "No broken requirements found."; exit 0 ;;
  show) exit 1 ;;
esac
exit 0
''', encoding="utf-8")
    python = каталог / "python3.13"
    python.write_text(f'''#!/bin/bash
echo "python3.13 $*" >> "{журнал}"
case "$*" in
  *"import ensurepip"*) exit 0 ;;
  *version_info*) echo "3.13"; exit 0 ;;
  -V|--version) echo "Python 3.13.3"; exit 0 ;;
  "-m venv "*)
    d="$3"; mkdir -p "$d/bin"
    cp "{вн_python}" "$d/bin/python"; cp "{вн_pip}" "$d/bin/pip"
    chmod +x "$d/bin/python" "$d/bin/pip"; exit 0 ;;
esac
exit 0
''', encoding="utf-8")
    for путь in (вн_python, вн_pip, python):
        путь.chmod(0o755)
    ссылка = каталог / "python3"
    if not ссылка.exists():
        ссылка.symlink_to(python)
    return python


def _установка(tmp_path: Path, *ключи: str) -> subprocess.CompletedProcess:
    журнал = tmp_path / "вызовы.log"
    заглушки = _заглушки(tmp_path / "bin", журнал)
    python = _поддельный_python(tmp_path / "bin", журнал)
    (tmp_path / "home").mkdir(exist_ok=True)
    return прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --prefix "{tmp_path / "prog"}" '
        f'--data "{tmp_path / "data"}" --no-service --yes --skip-models '
        f'--gpu-driver none --no-interactive --offline --python "{python}" '
        + " ".join(ключи),
        env={"PATH": f"{заглушки}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
             "TMPDIR": str(tmp_path)}, timeout=600)


# ---------------------------------------------------------------------------
# install.sh --force и повторная установка
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="bash-сценарии")
def test_force_чинит_окружение_и_не_трогает_конфигурацию(tmp_path: Path):
    """`install.sh --force` советуют подсказки для починки окружения.

    Он перезаписывал config.yaml шаблоном — пропадали ключи интеграций и
    агентов, токен, настройки, «хранить бессрочно» становилось тридцатью
    днями, — а окружение при той же версии Python не пересобирал: сломанный
    пакет оставался на месте («already satisfied»).
    """
    первая = _установка(tmp_path, "--port 18095 --host 127.0.0.1")
    assert первая.returncode == 0, первая.stdout[-3000:] + первая.stderr[-3000:]
    конфиг = tmp_path / "data" / "config.yaml"
    with конфиг.open("a", encoding="utf-8") as файл:
        файл.write('api_keys: {"ah_секрет": {"name": "агент АТС"}}\nresult_retention_days: 0\n')
    журнал = tmp_path / "вызовы.log"
    журнал.write_text("", encoding="utf-8")
    повтор = _установка(tmp_path, "--force")
    assert повтор.returncode == 0, повтор.stdout[-3000:] + повтор.stderr[-3000:]
    текст = конфиг.read_text(encoding="utf-8")
    assert "ah_секрет" in текст and "result_retention_days: 0" in текст
    вызовы = журнал.read_text(encoding="utf-8")
    assert "-m venv" in вызовы, "окружение не пересобрано"
    assert not (tmp_path / "prog" / "venv.before-force").exists(), \
        "отложенное прежнее окружение не убрано после успеха"
    # Порт и адрес — у стоящей установки, а не из умолчаний.
    assert "server_port: 18095" in текст and "server_host: 127.0.0.1" in текст
    assert "Порт взят у стоящей установки: 18095" in повтор.stdout


@pytest.mark.skipif(os.name != "posix", reason="bash-сценарии")
def test_сброс_конфигурации_только_отдельным_ключом(tmp_path: Path):
    первая = _установка(tmp_path)
    assert первая.returncode == 0, первая.stdout[-3000:]
    конфиг = tmp_path / "data" / "config.yaml"
    with конфиг.open("a", encoding="utf-8") as файл:
        файл.write('api_keys: {"ah_секрет": {"name": "x"}}\n')
    сброс = _установка(tmp_path, "--reset-config")
    assert сброс.returncode == 0, сброс.stdout[-3000:]
    assert "ah_секрет" not in конфиг.read_text(encoding="utf-8")
    копии = list((tmp_path / "data").glob("config.yaml.bak.*"))
    assert копии and "ah_секрет" in копии[0].read_text(encoding="utf-8")


def test_значения_стоящей_службы_читаются_из_юнита_и_plist(tmp_path: Path):
    дом = tmp_path / "home"
    (дом / ".config" / "systemd" / "user").mkdir(parents=True)
    (дом / "Library" / "LaunchAgents").mkdir(parents=True)
    (дом / ".config" / "systemd" / "user" / "asrhub.service").write_text(
        "[Service]\nUser=служба\nWorkingDirectory=/opt/asr\\x20hub/server\n"
        'ExecStart="/opt/asr hub/venv/bin/python" -m asrhub --host 127.0.0.1 --port 8081\n',
        encoding="utf-8")
    (дом / "Library" / "LaunchAgents" / "com.asrhub.server.plist").write_text(
        "<plist><dict><key>ProgramArguments</key><array>\n"
        "<string>/Users/a/ASRHub/venv/bin/python</string>\n"
        "<string>--host</string><string>0.0.0.0</string>\n"
        "<string>--port</string>\n  <string>9000</string></array></dict></plist>\n",
        encoding="utf-8")
    конфиг = tmp_path / "config.yaml"
    конфиг.write_text('server:\n  server_port: 8082   # свой\n  server_host: "127.0.0.1"\n',
                      encoding="utf-8")
    итог = прогнать(f'''
      set -euo pipefail
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      printf 'port=%s\\n' "$(installed_service_value asrhub "/opt/asr hub" port)"
      printf 'host=%s\\n' "$(installed_service_value asrhub "/opt/asr hub" host)"
      printf 'user=%s\\n' "$(installed_service_value asrhub "/opt/asr hub" user)"
      printf 'чужой=%s\\n' "$(installed_service_value asrhub /opt/asrhub2 port || echo нет)"
      printf 'plist=%s\\n' "$(installed_service_value nobody /Users/a/ASRHub port || echo нет)"
      printf 'yaml=%s\\n' "$(config_yaml_value "{конфиг}" server_port)"
      printf 'yamlhost=%s\\n' "$(config_yaml_value "{конфиг}" server_host)"
    ''', env={"HOME": str(дом)})
    assert итог.returncode == 0, итог.stderr
    for строка in ("port=8081", "host=127.0.0.1", "user=служба", "чужой=нет",
                   "plist=9000", "yaml=8082", "yamlhost=127.0.0.1"):
        assert строка in итог.stdout, итог.stdout


def test_мастер_не_уводит_свой_сервер_на_соседний_порт():
    текст = (КОРЕНЬ / "scripts" / "install.sh").read_text(encoding="utf-8")
    мастер = текст[текст.index("wizard_ask PORT") - 600:текст.index("wizard_ask PORT")]
    assert "port_is_ours" in мастер, "мастер предлагает соседний порт вместо своего"


def test_служба_перезапускается_а_не_только_включается():
    """`enable --now` у работающей службы ничего не делает.

    Повторная установка оставляла работать старый код над новыми файлами, а
    новый порт или адрес из юнита вступали в силу при случайном перезапуске.
    """
    текст = (КОРЕНЬ / "scripts" / "service.sh").read_text(encoding="utf-8")
    юнит = текст[текст.index("install_systemd() {"):текст.index("install_launchd() {")]
    команды = [с for с in юнит.splitlines() if not с.lstrip().startswith("#")]
    assert not any("enable --now" in с for с in команды)
    assert sum("systemctl" in с and "restart" in с for с in команды) == 2


# ---------------------------------------------------------------------------
# uninstall.sh
# ---------------------------------------------------------------------------


def _стенд_удаления(tmp_path: Path) -> dict[str, Path]:
    пути = {
        "сборка": tmp_path / "opt" / "prog" / "whisper.cpp" / "build" / "bin",
        "снимок": tmp_path / "opt" / "prog.snapshot",
        "старый_снимок": tmp_path / "opt" / "asrhub-snapshot",
        "клиент": tmp_path / "home" / ".config" / "asrhub",
        "данные": tmp_path / "data",
    }
    for путь in пути.values():
        путь.mkdir(parents=True, exist_ok=True)
    (tmp_path / "opt" / "prog" / "server").mkdir(parents=True, exist_ok=True)
    (пути["снимок"] / "config.yaml.snapshot").write_text("api_keys: {}\n", encoding="utf-8")
    (пути["данные"] / "asrhub.db").write_bytes(b"")
    return пути


def _удалить(tmp_path: Path, *ключи: str) -> subprocess.CompletedProcess:
    журнал = tmp_path / "вызовы.log"
    заглушки = _заглушки(tmp_path / "bin", журнал)
    return прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "uninstall.sh"}" --prefix "{tmp_path / "opt" / "prog"}" '
        f'--data "{tmp_path / "data"}" --name asrhub-проба-40 ' + " ".join(ключи),
        env={"PATH": f"{заглушки}:{os.environ['PATH']}", "HOME": str(tmp_path / "home")})


def test_пробное_удаление_не_удаляет_остатки(tmp_path: Path):
    """Остатки удалялись голым rm мимо пробного запуска.

    `uninstall.sh --dry-run --yes` по-настоящему сносил сборку whisper.cpp на
    несколько гигабайт, снимок отката, настройки клиента и LaunchAgent, а в
    конце печатал «ASR Hub удалён».
    """
    пути = _стенд_удаления(tmp_path)
    итог = _удалить(tmp_path, "--dry-run", "--yes")
    for имя, путь in пути.items():
        assert путь.exists(), f"пробный запуск удалил {имя}: {путь}"
    assert "ничего не удалено" in итог.stdout, итог.stdout[-2000:]
    assert "ASR Hub удалён" not in итог.stdout


def test_новый_снимок_с_ключами_считается_остатком(tmp_path: Path):
    """Снимок `<каталог>.snapshot` с config.yaml удаление не знало.

    После `--purge` копия ключей доступа и токена оставалась на диске, а
    сценарий писал «Остатков не найдено».
    """
    пути = _стенд_удаления(tmp_path)
    итог = _удалить(tmp_path, "--yes")
    assert not пути["снимок"].exists(), итог.stdout[-2000:]
    assert "копия config.yaml" in итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# Версия модуля ядра NVIDIA
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("строка,версия", [
    ("NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  575.57.08  Release Build  "
     "(dvs-builder@U16)  Tue May 20 2025", "575.57.08"),
    ("NVRM version: NVIDIA UNIX x86_64 Kernel Module  570.86.15  Wed Jan 22 2025", "570.86.15"),
    ("NVRM version: NVIDIA UNIX Open Kernel Module for aarch64  580.65.06  Release Build",
     "580.65.06"),
])
def test_версия_открытого_модуля_ядра_читается(tmp_path: Path, строка: str, версия: str):
    """У Blackwell (RTX 50xx) модуль ядра только открытый.

    Он пишет «Open Kernel Module for x86_64 575.57.08», прежний шаблон этого
    не разбирал, версия выходила пустой — и разбор «поможет ли перезагрузка»
    молчал ровно на той карте, что стоит на боевом сервере.
    """
    from asrhub import hardware

    proc = tmp_path / "version"
    proc.write_text(строка + "\nGCC version:  gcc 13\n", encoding="utf-8")
    assert hardware.проверить_драйвер(proc=str(proc))["loaded"] == версия
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "gpu.sh"}"
      nvidia_loaded_version
    ''', env={"ASRHUB_NVIDIA_PROC": str(proc)})
    assert итог.stdout.strip() == версия, итог.stderr


# ---------------------------------------------------------------------------
# Прокси nginx для диктовки
# ---------------------------------------------------------------------------


def test_прокси_пропускает_websocket_диктовки():
    """/api/stream попадал в «location /» без Upgrade: через прокси — а ради
    https для микрофона его и ставят — диктовка не работала вовсе."""
    текст = (КОРЕНЬ / "docker" / "nginx.conf").read_text(encoding="utf-8")
    тело = "\n".join(с for с in текст.splitlines() if not с.lstrip().startswith("#"))
    найдено = re.search(r"location = /api/stream\s*\{", тело)
    assert найдено, "нет блока location = /api/stream"
    блок = тело[найдено.start():]
    блок = блок[:блок.index("}")]
    assert "proxy_set_header Upgrade $http_upgrade" in блок
    assert 'proxy_set_header Connection "upgrade"' in блок
    # Host с портом: вход по паролю сверяет Origin с Host.
    assert "proxy_set_header Host $host;" not in тело


# ---------------------------------------------------------------------------
# Клиент asrctl и установщик агента
# ---------------------------------------------------------------------------


def test_прогресс_с_сервера_не_исполняется_на_машине_клиента(tmp_path: Path):
    """Значение progress из ответа подставлялось в текст `python3 -c` и awk.

    Подставной сервер (или перехват по http, а по умолчанию адрес http)
    выполнял код на машине клиента при `asrctl status` и `wait`.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    метка = tmp_path / "ВЗЛОМ"
    злой_питон = (f"0') if __import__('pathlib').Path('{метка}-py').touch() is None "
                  "else float('0")
    злой_awk = f'0; system("touch {метка}-awk"); x=0'

    class Сервер(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            прогресс = злой_питон if "st" in self.path else злой_awk
            тело = json.dumps({"id": "x", "filename": "a.wav", "status": "running",
                               "progress": прогресс, "stage": "этап"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

        def log_message(self, *а):
            return

    сервер = HTTPServer(("127.0.0.1", 0), Сервер)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        адрес = f"http://127.0.0.1:{сервер.server_port}"
        asrctl = КОРЕНЬ / "scripts" / "client" / "asrctl"
        прогнать(f'bash "{asrctl}" --server "{адрес}" --key ah_x status st',
                 env={"HOME": str(tmp_path)}, timeout=60)
        прогнать(f'timeout 5 bash "{asrctl}" --server "{адрес}" --key ah_x wait wt',
                 env={"HOME": str(tmp_path)}, timeout=60)
    finally:
        сервер.shutdown()
    assert not Path(f"{метка}-py").exists(), "код из ответа сервера исполнен (python)"
    assert not Path(f"{метка}-awk").exists(), "код из ответа сервера исполнен (awk)"


def test_ключ_агента_не_виден_в_списке_процессов(tmp_path: Path):
    """Ключ передавался curl аргументом -H и был виден любому на станции в ps."""
    import sys

    sys.path.insert(0, str(КОРЕНЬ / "tests"))
    from test_agent import Станция, установщик_в_песочнице

    установщик = установщик_в_песочнице(tmp_path)
    корень = tmp_path / "система"
    журнал = tmp_path / "curl.log"
    настоящий = shutil.which("curl")
    if not настоящий:
        pytest.skip("нужен curl")
    обёртка = корень / "bin" / "curl"
    обёртка.write_text(f'#!/bin/sh\necho "$@" >> "{журнал}"\nexec "{настоящий}" "$@"\n',
                       encoding="utf-8")
    обёртка.chmod(0o755)
    (tmp_path / "Master.csv").write_text("", encoding="utf-8")
    (tmp_path / "monitor").mkdir()
    окружение = {**os.environ, "PATH": f"{корень / 'bin'}:{os.environ.get('PATH', '')}",
                 "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"), "NO_COLOR": "1"}
    with Станция() as станция:
        итог = subprocess.run(
            ["bash", str(установщик), "--url", станция.адрес, "--key", "ah_секрет1234567890",
             "--cdr-file", str(tmp_path / "Master.csv"),
             "--recordings", str(tmp_path / "monitor")],
            capture_output=True, text=True, timeout=180, env=окружение, check=False)
    assert журнал.exists(), итог.stdout + итог.stderr
    assert "ah_секрет1234567890" not in журнал.read_text(encoding="utf-8")


def test_недоступный_сервер_называется_недоступным(tmp_path: Path):
    """«HTTP 000000» и совет смотреть журнал сервера вместо проверки сети."""
    import sys

    sys.path.insert(0, str(КОРЕНЬ / "tests"))
    from test_agent import установщик_в_песочнице

    установщик = установщик_в_песочнице(tmp_path)
    корень = tmp_path / "система"
    окружение = {**os.environ, "PATH": f"{корень / 'bin'}:{os.environ.get('PATH', '')}",
                 "SYSTEMCTL_LOG": str(tmp_path / "systemctl.log"), "NO_COLOR": "1"}
    итог = subprocess.run(
        ["bash", str(установщик), "--url", "http://127.0.0.1:1", "--key", "ah_x1234567890"],
        capture_output=True, text=True, timeout=120, env=окружение, check=False)
    вывод = итог.stdout + итог.stderr
    assert итог.returncode != 0
    assert "000000" not in вывод, вывод[-800:]
    assert "не отвечает" in вывод or "недоступен" in вывод, вывод[-800:]
