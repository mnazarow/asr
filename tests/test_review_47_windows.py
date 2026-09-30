"""Заход 47: сценарии для Windows, образ Docker, прокси nginx.

Сценарии PowerShell проверяются настоящим PowerShell 7 над временными
каталогами. Всё, чего вне Windows нет — службы, задачи планировщика,
реестр NSSM, список процессов, — подменено функциями-заглушками, которые
записывают, что их позвали; nssm, sc.exe, icacls и docker — исполняемыми
заглушками в PATH. Сценарии при этом не знают о подмене: они ищут службу
и её каталог так же, как на настоящей машине.

Главное здесь — то же, что и у сценариев для Linux: сценарий узнаёт свою
установку. Удаление установки в свой каталог раньше останавливало ВСЕ
процессы python*.exe на машине (`StartsWith('')` истинно для любого пути),
пробный запуск останавливал контейнеры, а обновление брало порт из файла
вместо службы и откатывало исправный сервер, ответивший 503.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_review_47_scripts import ДЕРЖАТЕЛЬ_БАЗЫ

КОРЕНЬ = Path(__file__).resolve().parent.parent
PWSH = shutil.which("pwsh") or shutil.which("powershell")
ВЕРСИЯ = (КОРЕНЬ / "VERSION").read_text(encoding="utf-8").strip()
МОДУЛЬ = КОРЕНЬ / "scripts" / "lib" / "Common.psm1"
нужен_pwsh = pytest.mark.skipif(PWSH is None, reason="нужен PowerShell")


# ---------------------------------------------------------------------------
# Обвязка
# ---------------------------------------------------------------------------


def _в_кавычках(значение: object) -> str:
    """Строка PowerShell в одинарных кавычках."""
    return "'" + str(значение).replace("'", "''") + "'"


def _исполняемые_заглушки(каталог: Path, журнал: Path, **поведение: str) -> Path:
    """nssm, sc.exe, icacls, docker — записывают вызов и отвечают успехом."""
    каталог.mkdir(parents=True, exist_ok=True)
    for имя in ("nssm", "sc.exe", "icacls", "docker", *поведение):
        тело = поведение.get(имя, "exit 0")
        путь = каталог / имя
        путь.write_text(f'#!/bin/bash\necho "{имя} $*" >> "{журнал}"\n{тело}\n',
                        encoding="utf-8")
        путь.chmod(0o755)
    return каталог


def _заглушки_ps(журнал: Path, *, служба: dict | None = None, задача: dict | None = None,
                 процессы: list[tuple[int, str]] = (), отказов_запуска: int = 0,
                 состояние: str = "Running") -> str:
    """Начало проверочного сценария: модуль и функции вместо недоступного вне Windows.

    У заглушек с атрибутом [Parameter] свой $ErrorAction объявлять нельзя: они
    становятся расширенными функциями и получают его сами, а у простых он
    нужен, иначе вызов с -ErrorAction не пройдёт разбор.
    """
    def таблица(значения: dict | None) -> str:
        if not значения:
            return "$null"
        части = []
        for ключ, значение in значения.items():
            if isinstance(значение, list):
                части.append(f"{ключ} = @({', '.join(_в_кавычках(v) for v in значение)})")
            else:
                части.append(f"{ключ} = {_в_кавычках(значение)}")
        return "@{ " + "; ".join(части) + " }"

    список_процессов = ", ".join(
        f"[pscustomobject]@{{ Name = 'python'; Id = {номер}; Path = {_в_кавычках(путь)} }}"
        for номер, путь in процессы)
    return f"""
$ErrorActionPreference = 'Stop'
Import-Module {_в_кавычках(МОДУЛЬ)} -Force
$global:MockLog = {_в_кавычках(журнал)}
function global:Write-MockLog([string]$Text) {{ Add-Content -LiteralPath $global:MockLog -Value $Text -Encoding UTF8 }}
$global:MockService = {таблица(служба)}
$global:MockServiceStatus = {_в_кавычках(состояние)}
$global:MockTask = {таблица(задача)}
$global:MockStartFails = {отказов_запуска}
$global:MockProcesses = @({список_процессов})
function global:Get-Service {{ param([Parameter(Position = 0)][string[]]$Name)
    if ($global:MockService -and (@($Name) -contains 'ASRHub')) {{
        [pscustomobject]@{{ Name = 'ASRHub'; Status = $global:MockServiceStatus }} }} }}
function global:Get-ItemProperty {{ param([string]$LiteralPath, [string]$Path, $ErrorAction)
    if ($global:MockService -and "$LiteralPath$Path" -like '*Services\\ASRHub\\Parameters') {{
        [pscustomobject]$global:MockService }} }}
function global:Stop-Service {{ param([Parameter(Position = 0)][string]$Name, [switch]$Force)
    Write-MockLog "Stop-Service $Name"; $global:MockServiceStatus = 'Stopped' }}
function global:Start-Service {{ param([Parameter(Position = 0)][string]$Name)
    Write-MockLog "Start-Service $Name"
    if ($global:MockStartFails -gt 0) {{ $global:MockStartFails--; throw 'Служба не запустилась: ошибка входа (1069)' }}
    $global:MockServiceStatus = 'Running' }}
function global:Restart-Service {{ param([Parameter(Position = 0)][string]$Name, [switch]$Force)
    Write-MockLog "Restart-Service $Name" }}
function global:Get-ScheduledTask {{ param([string]$TaskName, $ErrorAction)
    if ($global:MockTask -and $TaskName -eq 'ASRHub Server') {{
        [pscustomobject]@{{ TaskName = $TaskName; State = 'Ready'; Actions = @([pscustomobject]$global:MockTask) }} }} }}
function global:Get-ScheduledTaskInfo {{ param([string]$TaskName, $ErrorAction)
    [pscustomobject]@{{ LastRunTime = 'вчера'; LastTaskResult = 0 }} }}
function global:Stop-ScheduledTask {{ param([string]$TaskName, $ErrorAction) Write-MockLog "Stop-ScheduledTask $TaskName" }}
function global:Start-ScheduledTask {{ param([string]$TaskName, $ErrorAction) Write-MockLog "Start-ScheduledTask $TaskName" }}
function global:Unregister-ScheduledTask {{ param([string]$TaskName, $Confirm)
    Write-MockLog "Unregister-ScheduledTask $TaskName"; $global:MockTask = $null }}
function global:New-ScheduledTaskAction {{ param([string]$Execute, [string]$Argument, [string]$WorkingDirectory)
    Write-MockLog "Action Execute=$Execute Argument=$Argument WorkingDirectory=$WorkingDirectory"
    [pscustomobject]@{{ Execute = $Execute; Arguments = $Argument; WorkingDirectory = $WorkingDirectory }} }}
function global:New-ScheduledTaskTrigger {{ param([switch]$AtLogOn, [string]$User)
    Write-MockLog "Trigger AtLogOn=$([bool]$AtLogOn) User=$User"; 'trigger' }}
function global:New-ScheduledTaskSettingsSet {{ param([switch]$AllowStartIfOnBatteries,
    [switch]$DontStopIfGoingOnBatteries, [switch]$StartWhenAvailable, $RestartCount, $RestartInterval,
    $ExecutionTimeLimit) 'settings' }}
function global:Register-ScheduledTask {{ param([string]$TaskName, $Action, $Trigger, $Settings,
    [string]$Description, [switch]$Force)
    Write-MockLog "Register-ScheduledTask $TaskName"
    $global:MockTask = @{{ Execute = $Action.Execute; Arguments = $Action.Arguments;
                          WorkingDirectory = $Action.WorkingDirectory }} }}
function global:Get-Process {{ param([Parameter(Position = 0)][string[]]$Name, [int]$Id)
    $global:MockProcesses }}
function global:Stop-Process {{ param([int]$Id, [switch]$Force, $ErrorAction) Write-MockLog "Stop-Process $Id" }}
"""


def _pwsh(tmp_path: Path, тело: str, *, env: dict[str, str] | None = None,
          timeout: int = 300) -> subprocess.CompletedProcess:
    """Проверочный сценарий настоящим PowerShell: ввод закрыт, как у задачи."""
    сценарий = tmp_path / f"проверка-{uuid.uuid4().hex[:8]}.ps1"
    сценарий.write_text(тело, encoding="utf-8-sig")
    for каталог in ("home", "local", "pd", "appdata"):
        (tmp_path / каталог).mkdir(exist_ok=True)
    окружение = {**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1", "ASRHUB_NO_COLOR": "1",
                 "HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path / "local"),
                 "ProgramData": str(tmp_path / "pd"), "APPDATA": str(tmp_path / "appdata"),
                 **(env or {})}
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(сценарий)],
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=окружение, timeout=timeout, check=False)


def _путь_с_заглушками(tmp_path: Path, журнал: Path, **поведение: str) -> dict[str, str]:
    каталог = _исполняемые_заглушки(tmp_path / "bin", журнал, **поведение)
    return {"PATH": f"{каталог}:{os.environ['PATH']}"}


def _установка(корень: Path, имя: str, данные: Path | None, *, служба: str = "",
               режим: str = "native", метка: bool = True, окружение: bool = True,
               сервер: bool = False) -> Path:
    """Каталог установки Windows: scripts, server, VERSION, venv и метка."""
    каталог = корень / имя
    shutil.copytree(КОРЕНЬ / "scripts", каталог / "scripts")
    if сервер:
        (каталог / "server").symlink_to(КОРЕНЬ / "server")
    else:
        (каталог / "server" / "asrhub").mkdir(parents=True)
    (каталог / "VERSION").write_text(ВЕРСИЯ + "\n", encoding="utf-8")
    if окружение:
        scripts = каталог / "venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").symlink_to(sys.executable)
    if метка:
        (каталог / ".asrhub-install").write_text(
            f"prefix={каталог}\ndata_dir={данные or ''}\nservice={служба}\nmode={режим}\n"
            f"version={ВЕРСИЯ}\n", encoding="utf-8")
    if данные is not None:
        данные.mkdir(parents=True, exist_ok=True)
    return каталог


def _служба_nssm(программа: Path, данные: Path, порт: int = 8090,
                 адрес: str = "127.0.0.1") -> dict:
    """Параметры службы в реестре — так их пишет NSSM."""
    return {"Application": f"{программа}/venv/Scripts/python.exe",
            "AppParameters": f"-m asrhub --host {адрес} --port {порт}",
            "AppDirectory": f"{программа}/server",
            "AppEnvironmentExtra": [f"ASRHUB_DATA_DIR={данные}", "PYTHONUNBUFFERED=1"]}


def _журнал(путь: Path) -> str:
    return путь.read_text(encoding="utf-8-sig") if путь.exists() else ""


def _свободный_порт() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Здоровье(BaseHTTPRequestHandler):
    """/api/health с заданным кодом ответа."""
    код = 200

    def do_GET(self):  # noqa: N802
        тело = json.dumps({"status": "ok" if self.код == 200 else "degraded",
                           "version": "проба", "uptime_s": 7}).encode()
        self.send_response(self.код)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(тело)))
        self.end_headers()
        self.wfile.write(тело)

    def log_message(self, *args):
        pass


def _сервер_здоровья(код: int) -> tuple[HTTPServer, int]:
    обработчик = type("Здоровье", (_Здоровье,), {"код": код})
    сервер = HTTPServer(("127.0.0.1", 0), обработчик)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    return сервер, сервер.server_address[1]


# ---------------------------------------------------------------------------
# Удаление
# ---------------------------------------------------------------------------


@нужен_pwsh
def test_удаление_из_своего_каталога_останавливает_только_свои_процессы(tmp_path: Path):
    """Установка в свой каталог: $Prefix был пуст — и останавливались ВСЕ python*.exe.

    Команда удаления из «Что дальше» ключа -Prefix не несла, стандартных
    каталогов не было, `$_.Path.StartsWith('')` — истина для любого пути.
    Здесь установка прежней версии: метки нет, узнаётся по окружению.
    Процесс соседней установки с общим началом имени («ASR2» при «ASR») —
    тоже чужой: сравнение идёт по границе каталога.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, метка=False)
    (данные / "config.yaml").write_text("server:\n  server_port: 8090\n", encoding="utf-8")
    тело = _заглушки_ps(журнал, процессы=[
        (101, "/opt/Tools/jupyter/python3.exe"),
        (102, f"{программа}/venv/Scripts/python.exe"),
        (103, f"{tmp_path}/ASR2/venv/Scripts/python.exe"),
    ]) + f"& {_в_кавычках(программа / 'scripts' / 'uninstall.ps1')} -Yes -DataDir {_в_кавычках(данные)}\n"
    итог = _pwsh(tmp_path, тело, env=_путь_с_заглушками(tmp_path, журнал))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    остановлены = re.findall(r"Stop-Process (\d+)", _журнал(журнал))
    assert остановлены == ["102"], (остановлены, итог.stdout[-2000:])
    assert not программа.exists(), "каталог программы не удалён"
    assert (данные / "config.yaml").exists(), "без -Purge данные должны остаться"


@нужен_pwsh
def test_удаление_без_найденной_программы_процессов_не_трогает(tmp_path: Path):
    """Каталог программы не найден — останавливать нечего, а не всё подряд."""
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    данные.mkdir()
    дистрибутив = tmp_path / "дистрибутив"
    shutil.copytree(КОРЕНЬ / "scripts", дистрибутив / "scripts")
    тело = _заглушки_ps(журнал, процессы=[(101, "/opt/Tools/python.exe"),
                                          (102, "/srv/other/venv/Scripts/pythonw.exe")]) + \
        f"& {_в_кавычках(дистрибутив / 'scripts' / 'uninstall.ps1')} -Yes -DataDir {_в_кавычках(данные)}\n"
    итог = _pwsh(tmp_path, тело, env=_путь_с_заглушками(tmp_path, журнал))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "Stop-Process" not in _журнал(журнал), _журнал(журнал)
    assert "не найдена" in итог.stdout
    assert данные.exists()


@нужен_pwsh
def test_удаление_не_трогает_службу_другой_установки(tmp_path: Path):
    """Имя службы на Windows одно на машину — удаляется только своя.

    Раньше uninstall.ps1 любой установки звал service.ps1 -Action uninstall,
    а тот удалял службу «ASRHub», чья бы она ни была.
    """
    журнал = tmp_path / "вызовы.log"
    первая = _установка(tmp_path, "one", tmp_path / "данные1", служба="ASRHub")
    вторая = _установка(tmp_path, "two", tmp_path / "данные2")
    служба = _служба_nssm(первая, tmp_path / "данные1")
    окружение = _путь_с_заглушками(tmp_path, журнал)

    итог = _pwsh(tmp_path, _заглушки_ps(журнал, служба=служба) +
                 f"& {_в_кавычках(вторая / 'scripts' / 'uninstall.ps1')} -Yes\n", env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    assert "nssm remove" not in вызовы and "sc.exe delete" not in вызовы, вызовы
    assert "Stop-Service" not in вызовы, вызовы
    assert "другой установки" in итог.stdout
    assert not вторая.exists() and первая.exists()

    итог = _pwsh(tmp_path, _заглушки_ps(журнал, служба=служба) +
                 f"& {_в_кавычках(первая / 'scripts' / 'uninstall.ps1')} -Yes\n", env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    assert "Stop-Service ASRHub" in вызовы and "nssm remove ASRHub confirm" in вызовы, вызовы


@нужен_pwsh
def test_при_двух_установках_сценарии_из_дистрибутива_спрашивают_какую(tmp_path: Path):
    """Установок две, сценарий запущен не из них — ни удаления, ни обновления наугад."""
    журнал = tmp_path / "вызовы.log"
    первая = _установка(tmp_path, "one", tmp_path / "данные1", служба="ASRHub")
    _установка(tmp_path / "local", "ASRHub", tmp_path / "данные2")
    дистрибутив = tmp_path / "дистрибутив"
    shutil.copytree(КОРЕНЬ / "scripts", дистрибутив / "scripts")
    (дистрибутив / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
    (дистрибутив / "server").mkdir()
    начало = _заглушки_ps(журнал, служба=_служба_nssm(первая, tmp_path / "данные1"))
    for сценарий, доводы in (("uninstall.ps1", "-Yes -Purge"), ("update.ps1", "-Yes"),
                             ("doctor.ps1", "")):
        итог = _pwsh(tmp_path, начало +
                     f"& {_в_кавычках(дистрибутив / 'scripts' / сценарий)} {доводы}\n"
                     "exit $LASTEXITCODE\n", env=_путь_с_заглушками(tmp_path, журнал))
        assert итог.returncode == 2, (сценарий, (итог.stdout + итог.stderr)[-2000:])
        assert "несколько установок" in итог.stdout, (сценарий, итог.stdout[-1500:])
    assert первая.exists() and (tmp_path / "local" / "ASRHub").exists()


@нужен_pwsh
def test_пробный_запуск_удаления_не_останавливает_контейнеры(tmp_path: Path):
    """`uninstall.ps1 -DryRun` звал `docker compose down --remove-orphans` по-настоящему.

    И звал его у любой установки, где есть Docker и docker-compose.yml, —
    то есть и у обычной, если на машине стоит Docker Desktop.
    """
    журнал = tmp_path / "вызовы.log"
    контейнерная = _установка(tmp_path, "docker-one", tmp_path / "данные1", режим="docker",
                              окружение=False)
    (контейнерная / "docker").mkdir(exist_ok=True)
    (контейнерная / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    окружение = _путь_с_заглушками(tmp_path, журнал)
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(контейнерная / 'scripts' / 'uninstall.ps1')} -Yes -DryRun\n",
                 env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "down" not in _журнал(журнал), _журнал(журнал)
    assert "[пробный запуск]" in итог.stdout and "down --remove-orphans" in итог.stdout
    assert контейнерная.exists()

    обычная = _установка(tmp_path, "native-one", tmp_path / "данные2")
    (обычная / "docker").mkdir(exist_ok=True)
    (обычная / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    журнал.unlink(missing_ok=True)
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(обычная / 'scripts' / 'uninstall.ps1')} -Yes\n", env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "docker" not in _журнал(журнал), _журнал(журнал)


def _путь_без_docker(tmp_path: Path, журнал: Path) -> dict[str, str]:
    """PATH из одних заглушек и без docker: /usr/bin, где он мог бы стоять, не входит."""
    каталог = _исполняемые_заглушки(tmp_path / "bin-без-docker", журнал)
    (каталог / "docker").unlink()
    return {"PATH": str(каталог)}


def _контейнерная(tmp_path: Path, имя: str = "docker-one") -> Path:
    программа = _установка(tmp_path, имя, tmp_path / f"данные-{имя}", режим="docker",
                           окружение=False)
    (программа / "docker").mkdir(exist_ok=True)
    (программа / "docker" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return программа


@нужен_pwsh
def test_удаление_контейнерной_установки_говорит_о_контейнерах_и_образе(tmp_path: Path):
    """Без docker удаление писало «Установка не в контейнере» — а контейнеры работали.

    С docker образ на несколько гигабайт оставался после удаления: Windows
    его удалить не предлагала, в отличие от uninstall.sh. Удаляются только
    теги asrhub, чужие образы не трогаются.
    """
    журнал = tmp_path / "вызовы.log"
    программа = _контейнерная(tmp_path)
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(программа / 'scripts' / 'uninstall.ps1')} -Yes\n",
                 env=_путь_без_docker(tmp_path, журнал))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "docker не найден — контейнеры не остановлены" in итог.stdout, итог.stdout[-2000:]
    assert "не в контейнере" not in итог.stdout

    программа = _контейнерная(tmp_path, "docker-two")
    журнал.unlink(missing_ok=True)
    образы = ('case "$*" in\n  "image ls"*) printf \'asrhub:latest\\nasrhub:cuda\\npostgres:16\\n\' ;;\n'
              'esac\nexit 0')
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(программа / 'scripts' / 'uninstall.ps1')} -Yes\n",
                 env=_путь_с_заглушками(tmp_path, журнал, docker=образы))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    assert "down --remove-orphans" in вызовы, вызовы
    assert "docker image rm asrhub:latest" in вызовы and "docker image rm asrhub:cuda" in вызовы, вызовы
    assert "postgres" not in вызовы.replace("image ls", ""), вызовы


@нужен_pwsh
def test_обновление_контейнерной_установки_без_docker_останавливается(tmp_path: Path):
    """Без docker контейнерная установка обновлялась как обычная.

    Файлы заменялись, окружения Python не находилось, а в метку писался
    режим native — и следующее обновление, уже с docker, считало установку
    обычной. Теперь обновление останавливается до первого изменения, как
    update.sh.
    """
    журнал = tmp_path / "вызовы.log"
    программа = _контейнерная(tmp_path)
    источник = _источник(tmp_path, "99.0.0")
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(программа / 'scripts' / 'update.ps1')} -Yes -SkipGpuCheck "
                 f"-Source {_в_кавычках(источник)}\nexit $LASTEXITCODE\n",
                 env=_путь_без_docker(tmp_path, журнал))
    assert итог.returncode == 1, (итог.stdout + итог.stderr)[-3000:]
    assert "Установка в контейнере, а docker не найден" in итог.stdout, итог.stdout[-2000:]
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == ВЕРСИЯ
    assert "mode=docker" in (программа / ".asrhub-install").read_text(encoding="utf-8")
    assert not Path(f"{программа}.snapshot").exists()


@нужен_pwsh
def test_копия_базы_при_удалении_не_теряет_журнал_wal(tmp_path: Path):
    """Copy-Item одного asrhub.db терял всё, что лежит в asrhub.db-wal."""
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные)
    готово = tmp_path / "готово"
    держатель = subprocess.Popen([sys.executable, "-c", ДЕРЖАТЕЛЬ_БАЗЫ,
                                  str(данные / "asrhub.db"), str(готово)])
    try:
        for _ in range(100):
            if готово.exists():
                break
            time.sleep(0.1)
        assert (данные / "asrhub.db-wal").exists(), "журнал WAL не создан — проверка не о том"
        итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                     f"& {_в_кавычках(программа / 'scripts' / 'uninstall.ps1')} -Yes\n",
                     env=_путь_с_заглушками(tmp_path, журнал))
    finally:
        держатель.kill()
        держатель.wait(timeout=10)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    копии = list((tmp_path / "home").rglob("ASRHub-backup-*/asrhub.db"))
    assert копии, итог.stdout[-2000:]
    assert sqlite3.connect(копии[0]).execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 200


# ---------------------------------------------------------------------------
# Обновление
# ---------------------------------------------------------------------------


def _источник(tmp_path: Path, версия: str) -> Path:
    """Распакованный дистрибутив новой версии."""
    источник = tmp_path / f"источник-{версия}"
    (источник / "server" / "asrhub").mkdir(parents=True)
    (источник / "server" / "asrhub" / "новое.py").write_text("# новое\n", encoding="utf-8")
    shutil.copytree(КОРЕНЬ / "scripts", источник / "scripts")
    (источник / "requirements" / "engines").mkdir(parents=True)
    (источник / "requirements" / "base.txt").write_text("", encoding="utf-8")
    (источник / "VERSION").write_text(версия + "\n", encoding="utf-8")
    return источник


@нужен_pwsh
def test_обновление_делает_свой_снимок_с_конфигурацией_и_откатывается(tmp_path: Path):
    """Снимок был общим для всех установок под одним каталогом и без config.yaml.

    Откат второй установки возвращал код первой, а конфигурацию не
    возвращал вовсе. Теперь снимок — «<каталог>.snapshot», с
    config.yaml.snapshot; повторный запуск той же версии снимок не
    переснимает; `-Rollback -DryRun` ничего не меняет, а настоящий откат
    возвращает и код, и конфигурацию, сохранив нынешнюю рядом.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные)
    (программа / "server" / "asrhub" / "старое.py").write_text("# старое\n", encoding="utf-8")
    конфигурация = данные / "config.yaml"
    конфигурация.write_text("server:\n  server_port: 8090\n", encoding="utf-8")
    sqlite3.connect(данные / "asrhub.db").execute("CREATE TABLE jobs (id INTEGER)").connection.commit()
    источник = _источник(tmp_path, "99.0.0")
    окружение = _путь_с_заглушками(tmp_path, журнал)
    начало = _заглушки_ps(журнал)
    обновить = (f"& {_в_кавычках(программа / 'scripts' / 'update.ps1')} -Yes -SkipGpuCheck "
                f"-Source {_в_кавычках(источник)}\nexit $LASTEXITCODE\n")

    итог = _pwsh(tmp_path, начало + обновить, env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    снимок = Path(f"{программа}.snapshot")
    assert (снимок / "VERSION").read_text(encoding="utf-8").strip() == ВЕРСИЯ
    assert (снимок / "config.yaml.snapshot").read_text(encoding="utf-8") == \
        конфигурация.read_text(encoding="utf-8")
    assert not (tmp_path / "ASRHub-snapshot").exists(), "снова общий снимок"
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == "99.0.0"
    assert (программа / "server" / "asrhub" / "новое.py").exists()
    assert not (программа / "server" / "asrhub" / "старое.py").exists(), "копия поверх, а не замена"
    assert list(данные.glob("asrhub.db.bak.*")), "нет копии базы"
    метка = (программа / ".asrhub-install").read_text(encoding="utf-8")
    assert f"data_dir={данные}" in метка and "version=" in метка

    # Та же версия ещё раз: снимок прежней версии остаётся.
    итог = _pwsh(tmp_path, начало + обновить, env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert (снимок / "VERSION").read_text(encoding="utf-8").strip() == ВЕРСИЯ

    конфигурация.write_text("server:\n  server_port: 9999\n", encoding="utf-8")
    откатить = f"& {_в_кавычках(программа / 'scripts' / 'update.ps1')} -Yes -Rollback"
    итог = _pwsh(tmp_path, начало + откатить + " -DryRun\nexit $LASTEXITCODE\n", env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == "99.0.0", \
        "пробный откат заменил файлы"
    assert "9999" in конфигурация.read_text(encoding="utf-8")

    итог = _pwsh(tmp_path, начало + откатить + "\nexit $LASTEXITCODE\n", env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == ВЕРСИЯ
    assert (программа / "server" / "asrhub" / "старое.py").exists()
    assert not (программа / "server" / "asrhub" / "новое.py").exists()
    assert "8090" in конфигурация.read_text(encoding="utf-8")
    сохранённые = list(данные.glob("config.yaml.new.*"))
    assert сохранённые and "9999" in сохранённые[0].read_text(encoding="utf-8")


@нужен_pwsh
def test_обновление_не_откатывает_сервер_ответивший_503(tmp_path: Path):
    """Всё, кроме 200, считалось неудачей: с -Yes исправный сервер откатывался.

    503 — сервер жив и сам сообщает о неисправности (база, диск), откат
    этого не лечит. Порт при этом берётся из строки запуска службы, а не
    из config.yaml, где записан другой.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, служба="ASRHub")
    (данные / "config.yaml").write_text("server:\n  server_port: 1\n", encoding="utf-8")
    сервер, порт = _сервер_здоровья(503)
    try:
        источник = _источник(tmp_path, "99.0.0")
        итог = _pwsh(tmp_path, _заглушки_ps(журнал, служба=_служба_nssm(программа, данные, порт)) +
                     f"& {_в_кавычках(программа / 'scripts' / 'update.ps1')} -Yes -SkipGpuCheck "
                     f"-Source {_в_кавычках(источник)}\nexit $LASTEXITCODE\n",
                     env=_путь_с_заглушками(tmp_path, журнал))
    finally:
        сервер.shutdown()
    assert итог.returncode == 1, (итог.stdout + итог.stderr)[-3000:]
    assert "сообщает о неисправности (код 503)" in итог.stdout, итог.stdout[-2000:]
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == "99.0.0", \
        "обновление откатилось"
    вызовы = _журнал(журнал)
    assert "Stop-Service ASRHub" in вызовы and "Start-Service ASRHub" in вызовы, вызовы


@нужен_pwsh
def test_обновление_прежней_установки_сценарием_из_дистрибутива(tmp_path: Path):
    """На 3.1.19 Windows обновляется сценарием из дистрибутива.

    Установленный update.ps1 прежних версий файлов не обновлял (параметр
    -Source затирался), поэтому путь обновления — сценарий новой версии с
    -Prefix. Установка прежней версии меток не имеет: каталог данных
    берётся из окружения её службы, порт проверки — из строки запуска, а
    после обновления у установки появляется метка.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, метка=False)
    # Соседняя установка в стандартном каталоге: без -Prefix обновлять
    # наугад нельзя, и сценарий обязан взять именно заданную.
    сосед = _установка(tmp_path / "local", "ASRHub", tmp_path / "данные-соседа")
    сервер, порт = _сервер_здоровья(200)
    try:
        источник = _источник(tmp_path, "99.0.0")
        итог = _pwsh(tmp_path, _заглушки_ps(журнал, служба=_служба_nssm(программа, данные, порт)) +
                     f"& {_в_кавычках(источник / 'scripts' / 'update.ps1')} -Yes -SkipGpuCheck "
                     f"-Prefix {_в_кавычках(программа)}\nexit $LASTEXITCODE\n",
                     env=_путь_с_заглушками(tmp_path, журнал))
    finally:
        сервер.shutdown()
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == "99.0.0"
    assert (программа / "server" / "asrhub" / "новое.py").exists()
    assert (сосед / "VERSION").read_text(encoding="utf-8").strip() == ВЕРСИЯ
    метка = (программа / ".asrhub-install").read_text(encoding="utf-8")
    assert f"data_dir={данные}" in метка and "service=ASRHub" in метка, метка
    вызовы = _журнал(журнал)
    assert "Stop-Service ASRHub" in вызовы and "Start-Service ASRHub" in вызовы, вызовы


# ---------------------------------------------------------------------------
# Автозапуск
# ---------------------------------------------------------------------------


@нужен_pwsh
def test_состояние_службы_спрашивает_порт_из_её_строки_запуска(tmp_path: Path):
    """`service.ps1 -Action status` стучался в 8080 при любом настроенном порте."""
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, служба="ASRHub")
    сервер, порт = _сервер_здоровья(200)
    try:
        итог = _pwsh(tmp_path, _заглушки_ps(журнал, служба=_служба_nssm(программа, данные, порт)) +
                     f"& {_в_кавычках(программа / 'scripts' / 'service.ps1')} -Action status\n"
                     "exit $LASTEXITCODE\n", env=_путь_с_заглушками(tmp_path, журнал))
    finally:
        сервер.shutdown()
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert f"Сервер отвечает на порту {порт}" in итог.stdout, итог.stdout


@нужен_pwsh
def test_задача_планировщика_без_окна_для_своего_пользователя(tmp_path: Path):
    """Задача запускала python.exe с окном консоли и срабатывала на вход любого.

    Закрыть окно значило погасить сервер; -AtLogOn без -User поднимал сервер
    при входе любого пользователя машины. Порт и адрес без ключей — из
    config.yaml установки, а не умолчания скрипта.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, служба="ASRHub Server")
    (программа / "venv" / "Scripts" / "pythonw.exe").symlink_to(sys.executable)
    (данные / "config.yaml").write_text(
        "server:\n  server_host: 127.0.0.1\n  server_port: 8123\n", encoding="utf-8")
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) +
                 f"& {_в_кавычках(программа / 'scripts' / 'service.ps1')} -Action install\n"
                 "exit $LASTEXITCODE\n",
                 env={**_путь_с_заглушками(tmp_path, журнал), "USERNAME": "ivan", "USERDOMAIN": "OFFICE"})
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    действие = re.search(r"Action Execute=(\S+) Argument=(.*) WorkingDirectory=", вызовы)
    assert действие, вызовы
    assert действие.group(1).endswith("pythonw.exe"), действие.group(1)
    assert f'--config "{данные}/config.yaml"' in действие.group(2), действие.group(2)
    assert "--host 127.0.0.1 --port 8123" in действие.group(2), действие.group(2)
    assert "Trigger AtLogOn=True User=OFFICE\\ivan" in вызовы, вызовы


@нужен_pwsh
def test_служба_работает_от_своей_учётной_записи(tmp_path: Path):
    """Служба NSSM работала от LocalSystem — вся машина за любой уязвимостью.

    Теперь — от виртуальной записи «NT SERVICE\\ASRHub» с правами на
    каталог данных и программу. Не поднялась от неё (отказ входа) —
    возвращается LocalSystem: работающая служба лучше правильной.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, служба="ASRHub")
    окружение = _путь_с_заглушками(tmp_path, журнал)
    установить = ("function global:Test-Administrator { $true }\n"
                  f"& {_в_кавычках(программа / 'scripts' / 'service.ps1')} -Action install "
                  "-Port 8124 -BindHost 0.0.0.0\nexit $LASTEXITCODE\n")

    итог = _pwsh(tmp_path, _заглушки_ps(журнал) + установить, env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    assert f"nssm install ASRHub {программа}/venv/Scripts/python.exe -m asrhub --host 0.0.0.0 --port 8124" \
        in вызовы, вызовы
    assert f"icacls {данные} /grant NT SERVICE\\ASRHub:(OI)(CI)M" in вызовы, вызовы
    assert f"icacls {программа} /grant NT SERVICE\\ASRHub:(OI)(CI)RX" in вызовы, вызовы
    assert "sc.exe config ASRHub obj= NT SERVICE\\ASRHub" in вызовы, вызовы
    assert "obj= LocalSystem" not in вызовы
    assert "учётная запись NT SERVICE\\ASRHub" in итог.stdout

    журнал.unlink()
    итог = _pwsh(tmp_path, _заглушки_ps(журнал, отказов_запуска=1) + установить, env=окружение)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    вызовы = _журнал(журнал)
    assert "sc.exe config ASRHub obj= LocalSystem" in вызовы, вызовы
    assert вызовы.count("Start-Service ASRHub") == 2, вызовы
    assert "учётная запись LocalSystem" in итог.stdout


# ---------------------------------------------------------------------------
# Установщик (пробным запуском)
# ---------------------------------------------------------------------------


def _установщик(tmp_path: Path, журнал: Path, доводы: str, **заглушки) -> subprocess.CompletedProcess:
    дистрибутив = tmp_path / "дистрибутив"
    if not дистрибутив.exists():
        # Каталога config в дистрибутиве может не быть вовсе: git пустых
        # каталогов не хранит, и в клоне его нет.
        for часть in ("scripts", "requirements", "config", "docker"):
            if (КОРЕНЬ / часть).is_dir():
                shutil.copytree(КОРЕНЬ / часть, дистрибутив / часть)
        (дистрибутив / "server").symlink_to(КОРЕНЬ / "server")
        (дистрибутив / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
    return _pwsh(tmp_path, _заглушки_ps(журнал, **заглушки) +
                 f"& {_в_кавычках(дистрибутив / 'scripts' / 'install.ps1')} -DryRun -Yes "
                 f"-NoInteractive -Offline -NoGpuDriver {доводы}\nexit $LASTEXITCODE\n",
                 env=_путь_с_заглушками(tmp_path, журнал))


@нужен_pwsh
def test_установщик_поверх_берёт_адрес_и_порт_стоящей_установки(tmp_path: Path):
    """-Force -Yes пересоздавал службу с умолчаниями: 0.0.0.0:8080.

    Здесь же: счётчик шагов не уходит за итог («[11/9]»), а дочерние
    сценарии берутся из дистрибутива — в пробном запуске в каталоге
    установки их может не быть вовсе (или лежат прежние).
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = tmp_path / "ASR"
    (программа / "server").mkdir(parents=True)
    итог = _установщик(tmp_path, журнал,
                       f"-Prefix {_в_кавычках(программа)} -DataDir {_в_кавычках(данные)} "
                       "-Profile standard -Force",
                       служба=_служба_nssm(программа, данные, 8123, "127.0.0.1"))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-4000:]
    assert "Адрес сервера: http://127.0.0.1:8123" in итог.stdout, итог.stdout[-3000:]
    шаги = [(int(a), int(b)) for a, b in re.findall(r"^\[(\d+)/(\d+)\]", итог.stdout, re.M)]
    assert шаги and all(a <= b for a, b in шаги), шаги
    assert шаги[-1][0] == шаги[-1][1], шаги
    assert "[пробный запуск] models.ps1 -Action download" in итог.stdout
    assert "[пробный запуск] автозапуск ASRHub: install" in итог.stdout
    assert "Откат изменений" not in итог.stdout


@нужен_pwsh
def test_установщик_не_отнимает_автозапуск_у_другой_установки(tmp_path: Path):
    """Вторая установка пересоздавала службу «ASRHub» под себя."""
    журнал = tmp_path / "вызовы.log"
    первая = _установка(tmp_path, "one", tmp_path / "данные1", служба="ASRHub")
    итог = _установщик(tmp_path, журнал,
                       f"-Prefix {_в_кавычках(tmp_path / 'two')} "
                       f"-DataDir {_в_кавычках(tmp_path / 'данные2')}",
                       служба=_служба_nssm(первая, tmp_path / "данные1"))
    assert итог.returncode == 2, (итог.stdout + итог.stderr)[-3000:]
    assert "уже обслуживает другую установку" in итог.stdout
    assert "-NoService" in итог.stdout


@нужен_pwsh
def test_установщик_контейнера_в_пробном_запуске_на_чистой_машине(tmp_path: Path):
    """`install.ps1 -Mode docker -DryRun` падал на Push-Location в несуществующий каталог.

    И откатывал «7 действий», которых в пробном запуске не было.
    """
    журнал = tmp_path / "вызовы.log"
    итог = _установщик(tmp_path, журнал,
                       f"-Mode docker -Prefix {_в_кавычках(tmp_path / 'нет')} "
                       f"-DataDir {_в_кавычках(tmp_path / 'данные')}")
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "Откат изменений" not in итог.stdout
    assert "[пробный запуск] docker compose build" in итог.stdout
    assert not (tmp_path / "нет").exists()
    assert "build" not in _журнал(журнал) and "up" not in _журнал(журнал).split()


# ---------------------------------------------------------------------------
# Общий модуль
# ---------------------------------------------------------------------------


@нужен_pwsh
def test_порт_своего_сервера_узнаётся_а_соседнего_нет(tmp_path: Path):
    """Установщик переезжал на соседний порт, когда 8080 держал его же сервер."""
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные)
    сервер, порт = _сервер_здоровья(200)
    try:
        def спросить(путь_процесса: str, служба: dict | None = None) -> str:
            тело = _заглушки_ps(tmp_path / "вызовы.log", служба=служба) + f"""
function global:Get-NetTCPConnection {{ param($LocalPort, $State, $ErrorAction)
    if ('{путь_процесса}') {{ [pscustomobject]@{{ OwningProcess = 4242 }} }} }}
function global:Get-Process {{ param([int]$Id, $Name, $ErrorAction)
    [pscustomobject]@{{ Name = 'python'; Id = 4242; Path = {_в_кавычках(путь_процесса)} }} }}
"ОТВЕТ " + (Test-AsrPortOurs -Port {порт} -Prefix {_в_кавычках(программа)})
"""
            итог = _pwsh(tmp_path, тело)
            assert итог.returncode == 0, итог.stdout + итог.stderr
            return re.search(r"ОТВЕТ (\w+)", итог.stdout).group(1)

        assert спросить(f"{программа}/venv/Scripts/python.exe") == "True"
        assert спросить(f"{tmp_path}/ASR2/venv/Scripts/python.exe") == "False"
        # Процесс не узнать — своим считается порт своей службы.
        assert спросить("", _служба_nssm(программа, данные, порт)) == "True"
        assert спросить("", _служба_nssm(tmp_path / "ASR2", данные, порт)) == "False"
    finally:
        сервер.shutdown()


@нужен_pwsh
def test_питон_из_сценариев_пишет_в_канал_utf8(tmp_path: Path):
    """Python в канал писал в cp1251: --print-config падал на «×»."""
    итог = _pwsh(tmp_path, f"""
Import-Module {_в_кавычках(МОДУЛЬ)} -Force
$line = & {_в_кавычках(sys.executable)} -c "import sys; print(sys.stdout.encoding, sys.flags.utf8_mode)"
"ИТОГ $line $env:PYTHONIOENCODING"
""")
    assert "ИТОГ utf-8 1 utf-8" in итог.stdout, итог.stdout + итог.stderr


@нужен_pwsh
def test_find_python_не_берёт_слишком_новый_если_есть_проверенный(tmp_path: Path):
    """Верхней границы не было: «python» 3.14 брался, и установка падала в pip."""
    каталог = tmp_path / "bin"
    каталог.mkdir()
    for имя, версия in (("python", "3.14"), ("python3.12", "3.12")):
        путь = каталог / имя
        путь.write_text(f'#!/bin/bash\necho "{версия}"\n', encoding="utf-8")
        путь.chmod(0o755)
    тело = f"""
Import-Module {_в_кавычках(МОДУЛЬ)} -Force
"ВЫБРАН " + (Find-Python)
"""
    итог = _pwsh(tmp_path, тело, env={"PATH": str(каталог)})
    assert итог.stdout.strip().endswith(f"ВЫБРАН {каталог}/python3.12"), итог.stdout + итог.stderr
    (каталог / "python3.12").unlink()
    итог = _pwsh(tmp_path, тело, env={"PATH": str(каталог)})
    assert f"ВЫБРАН {каталог}/python" in итог.stdout, итог.stdout + итог.stderr
    assert "новее проверенной версии 3.13" in итог.stdout


@нужен_pwsh
def test_stderr_внешних_программ_перенаправляется_только_под_continue():
    """Windows PowerShell 5.1 при Stop превращает каждую строку stderr в исключение.

    На PowerShell 7 этого не видно, поэтому проверка — разбором: любое
    перенаправление потока ошибок (2>$null, 2>&1, *>…) живёт только в
    функции, которая сама ставит $ErrorActionPreference = 'Continue', или в
    сценарии, где это сделано для всего файла (doctor.ps1).
    """
    файлы = [*(КОРЕНЬ / "scripts").glob("*.ps1"), МОДУЛЬ, КОРЕНЬ / "scripts" / "client" / "asrctl.ps1"]
    сценарий = r"""
param([string[]]$Paths)
# `pwsh -File сценарий -Paths a b c` связывает с -Paths только «a», остальное
# уходит в $args — без этой строки проверялся бы один файл.
$Paths = @($Paths) + @($args)
"ФАЙЛОВ $($Paths.Count)"
foreach ($path in $Paths) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    $fileContinue = $ast.Extent.Text -match "(?m)^\`$ErrorActionPreference = 'Continue'"
    $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | ForEach-Object {
        $command = $_
        $bad = @($command.Redirections | Where-Object {
            $_.FromStream -in @('Error', 'All') })
        if (-not $bad) { return }
        $parent = $command.Parent
        while ($parent -and $parent -isnot [System.Management.Automation.Language.FunctionDefinitionAst]) { $parent = $parent.Parent }
        $ok = if ($parent) { $parent.Body.Extent.Text -match "\`$ErrorActionPreference = 'Continue'" } else { $fileContinue }
        if (-not $ok) { "ПЛОХО $(Split-Path -Leaf $path):$($command.Extent.StartLineNumber) $($command.Extent.Text)" }
    }
}
"ГОТОВО"
"""
    путь = Path(os.environ.get("TMPDIR", "/tmp")) / f"stderr-{uuid.uuid4().hex[:8]}.ps1"
    путь.write_text(сценарий, encoding="utf-8-sig")
    try:
        итог = subprocess.run([PWSH, "-NoProfile", "-File", str(путь), "-Paths", *map(str, файлы)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1"})
    finally:
        путь.unlink(missing_ok=True)
    assert "ГОТОВО" in итог.stdout, итог.stdout + итог.stderr
    assert f"ФАЙЛОВ {len(файлы)}" in итог.stdout, итог.stdout
    assert "ПЛОХО" not in итог.stdout, итог.stdout


@нужен_pwsh
def test_переменные_сценариев_не_затирают_их_параметры():
    """Имена переменных PowerShell регистра не различают.

    В update.ps1 цикл снимка писал в `$source` — и тем затирал параметр
    -Source: файлы потом «копировались» из «<установка>\\README.md\\server»,
    то есть ниоткуда, а сценарий печатал «Файлы обновлены». Обновление на
    Windows не меняло кода вовсе. Ловим всякое имя, совпадающее с параметром
    сценария с точностью до регистра, но написанное иначе.
    """
    сценарий = r"""
param([string[]]$Paths)
# `pwsh -File сценарий -Paths a b c` связывает с -Paths только «a», остальное
# уходит в $args — без этой строки проверялся бы один файл.
$Paths = @($Paths) + @($args)
"ФАЙЛОВ $($Paths.Count)"
foreach ($path in $Paths) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
    $params = @()
    if ($ast.ParamBlock) { $params = @($ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }) }
    $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) |
        ForEach-Object {
            $name = $_.VariablePath.UserPath
            foreach ($p in $params) {
                if ($name -ieq $p -and $name -cne $p) { "ЗАТИРАЕТ $(Split-Path -Leaf $path):$($_.Extent.StartLineNumber) $name" }
            }
        }
}
"ГОТОВО"
"""
    путь = Path(os.environ.get("TMPDIR", "/tmp")) / f"shadow-{uuid.uuid4().hex[:8]}.ps1"
    путь.write_text(сценарий, encoding="utf-8-sig")
    файлы = [*(КОРЕНЬ / "scripts").glob("*.ps1"), КОРЕНЬ / "scripts" / "client" / "asrctl.ps1"]
    try:
        итог = subprocess.run([PWSH, "-NoProfile", "-File", str(путь), "-Paths", *map(str, файлы)],
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120,
                              env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1"})
    finally:
        путь.unlink(missing_ok=True)
    assert "ГОТОВО" in итог.stdout, итог.stdout + итог.stderr
    assert f"ФАЙЛОВ {len(файлы)}" in итог.stdout, итог.stdout
    assert "ЗАТИРАЕТ" not in итог.stdout, итог.stdout


@нужен_pwsh
def test_модели_видят_веса_gigaam_одним_файлом(tmp_path: Path):
    """models.ps1 качал снапшот ai-sage/GigaAM-v3, который движок не открывает.

    А list, verify и remove искали только каталоги Hugging Face и скачанный
    .ckpt не видели. Теперь загрузка — функцией движка (сюда подставлена
    поддельная библиотека), поиск — тем же model_files, что у сервера.
    """
    поддельная = tmp_path / "fake" / "gigaam"
    поддельная.mkdir(parents=True)
    (поддельная / "__init__.py").write_text(
        "import os\n"
        "def _download_model(name, root):\n"
        "    path = os.path.join(root, name + '.ckpt')\n"
        "    open(path, 'wb').write(b'0' * 2048)\n"
        "    open(os.environ['GIGAAM_LOG'], 'a').write(name + '\\n')\n"
        "    return path\n", encoding="utf-8")
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные, сервер=True)
    лог = tmp_path / "gigaam.log"
    окружение = {"PYTHONPATH": str(tmp_path / "fake"), "GIGAAM_LOG": str(лог)}

    def модели(доводы: str) -> subprocess.CompletedProcess:
        return _pwsh(tmp_path, f"& {_в_кавычках(программа / 'scripts' / 'models.ps1')} {доводы}\n"
                     "exit $LASTEXITCODE\n", env=окружение)

    итог = модели("-Action download -Model gigaam-v3-e2e-rnnt")
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert лог.read_text(encoding="utf-8").split() == ["v3_e2e_rnnt"]
    assert (данные / "models" / "v3_e2e_rnnt.ckpt").exists()
    assert not list((данные / "models").glob("models--*")), "снова снапшот Hugging Face"

    итог = модели("-Action list -Installed")
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert re.search(r"\[\+\] gigaam-v3-e2e-rnnt", итог.stdout), итог.stdout[-3000:]

    итог = модели("-Action verify -Model gigaam-v3-e2e-rnnt")
    assert "Веса на диске:   да" in итог.stdout, итог.stdout[-2000:]

    итог = модели("-Action remove -Model gigaam-v3-e2e-rnnt")
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert not (данные / "models" / "v3_e2e_rnnt.ckpt").exists()

    # Неудачная загрузка — ненулевой код процесса. `powershell -File` сам код
    # последней программы не передаёт: без явного exit сценарий кончался
    # нулём, и планировщик или установщик не видели сбоя.
    итог = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-File", str(программа / "scripts" / "models.ps1"),
         "-Action", "download", "-Model", "нет-такой-модели"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=300,
        env={**os.environ, **окружение, "POWERSHELL_TELEMETRY_OPTOUT": "1",
             "HOME": str(tmp_path / "home"), "LOCALAPPDATA": str(tmp_path / "local"),
             "ProgramData": str(tmp_path / "pd")})
    assert итог.returncode == 1, итог.stdout[-2000:]


@нужен_pwsh
def test_удаление_движка_на_windows_оставляет_нужное_другим(tmp_path: Path):
    """remove-engine снимал numpy из base.txt и onnxruntime, общий с другими движками."""
    from test_review_39 import _pip, _снято

    требования = tmp_path / "requirements"
    (требования / "engines" / "no-deps").mkdir(parents=True)
    (требования / "base.txt").write_text("numpy>=1.26\nsoundfile\n", encoding="utf-8")
    (требования / "engines" / "gigaam.txt").write_text(
        "numpy\nonnxruntime>=1.18\nhydra-core\nsentencepiece\n", encoding="utf-8")
    (требования / "engines" / "no-deps" / "gigaam.txt").write_text(
        "gigaam @ git+https://example/gigaam.git\n", encoding="utf-8")
    (требования / "engines" / "tone.txt").write_text("onnxruntime\n", encoding="utf-8")
    pip = _pip(tmp_path / "venv", {"gigaam": "", "hydra-core": "gigaam", "numpy": "soundfile",
                                   "onnxruntime": "gigaam", "sentencepiece": "torchaudio-extra"})
    итог = _pwsh(tmp_path, f"""
Import-Module {_в_кавычках(МОДУЛЬ)} -Force
$plan = Get-EngineRemovalPlan -Pip {_в_кавычках(pip)} -Requirements {_в_кавычках(требования / 'engines' / 'gigaam.txt')}
"СНЯТЬ " + ($plan.Remove -join ',')
"ОСТАВИТЬ " + ($plan.Keep -join ',')
""")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    снять = re.search(r"СНЯТЬ (\S*)", итог.stdout).group(1).split(",")
    оставить = re.search(r"ОСТАВИТЬ (\S*)", итог.stdout).group(1).split(",")
    assert set(снять) == {"gigaam", "hydra-core"}, итог.stdout
    # numpy — из base.txt, onnxruntime — нужен tone, sentencepiece держится на чужом пакете.
    assert {"numpy", "onnxruntime", "sentencepiece"} <= set(оставить), итог.stdout
    assert _снято(pip) == set(), "план ничего не снимает сам"


# ---------------------------------------------------------------------------
# Клиент asrctl.ps1
# ---------------------------------------------------------------------------


class _ФальшивыйСервер(BaseHTTPRequestHandler):
    """Сервер ASR Hub в миниатюре: принимает файл, отдаёт состояние и результат."""
    состояние: dict = {}

    def _ответ(self, код: int, тело: object, тип: str = "application/json") -> None:
        данные = тело if isinstance(тело, bytes) else json.dumps(тело, ensure_ascii=False).encode()
        self.send_response(код)
        self.send_header("Content-Type", тип)
        self.send_header("Content-Length", str(len(данные)))
        self.end_headers()
        self.wfile.write(данные)

    def do_GET(self):  # noqa: N802
        с = self.состояние
        if self.path == "/api/health":
            return self._ответ(200, {"version": "проба", "uptime_s": 1, "queue_paused": False})
        if self.path.startswith("/api/jobs/job_1/download"):
            return self._ответ(200, "готовый текст\n".encode(), "text/plain")
        if self.path == "/api/jobs/job_1":
            с["опросов"] = с.get("опросов", 0) + 1
            if с["опросов"] <= с.get("сбоев", 0):
                return self._ответ(503, {"detail": {"message": "Сервер перезапускается"}})
            готово = с["опросов"] > с.get("сбоев", 0) + с.get("ждать", 1)
            return self._ответ(200, {"id": "job_1", "status": "completed" if готово else "running",
                                     "progress": 0.5, "stage": "распознавание",
                                     "filename": "..\\..\\злое имя.mp3", "rtf": 0.1,
                                     "words_count": 3})
        if self.path == "/api/jobs/missing":
            return self._ответ(404, {"detail": {"message": "Задание не найдено",
                                                "hint": "Проверьте идентификатор"}})
        return self._ответ(404, {"detail": "нет такого"})

    def do_POST(self):  # noqa: N802
        длина = int(self.headers.get("Content-Length") or 0)
        self.состояние["тело"] = self.rfile.read(длина)
        self.состояние["ключ"] = self.headers.get("X-API-Key")
        return self._ответ(200, {"id": "job_1"})

    def log_message(self, *args):
        pass


@pytest.fixture()
def фальшивый_сервер():
    обработчик = type("Сервер", (_ФальшивыйСервер,), {"состояние": {}})
    сервер = HTTPServer(("127.0.0.1", 0), обработчик)
    threading.Thread(target=сервер.serve_forever, daemon=True).start()
    yield обработчик.состояние, f"http://127.0.0.1:{сервер.server_address[1]}"
    сервер.shutdown()


def _asrctl(tmp_path: Path, доводы: list[str], *, ввод: str | None = None,
            timeout: int = 180) -> subprocess.CompletedProcess:
    (tmp_path / "appdata").mkdir(exist_ok=True)
    return subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File",
                           str(КОРЕНЬ / "scripts" / "client" / "asrctl.ps1"), *доводы],
                          input=ввод, stdin=None if ввод is not None else subprocess.DEVNULL,
                          capture_output=True, text=True, cwd=str(tmp_path), timeout=timeout,
                          env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1",
                               "APPDATA": str(tmp_path / "appdata"), "ASRHUB_SERVER": "",
                               "ASRHUB_KEY": ""})


@нужен_pwsh
def test_клиент_windows_отправляет_файл_без_form(tmp_path: Path, фальшивый_сервер):
    """У Invoke-RestMethod в Windows PowerShell 5.1 нет -Form: файл не уходил вовсе.

    Теперь — System.Net.Http. Имя файла с кириллицей и запятой уходит
    байтами UTF-8, как у браузера и curl; эталон — текстом, а не содержимым
    файла «<secret.txt»; результат ложится в каталог под безопасным именем.
    """
    состояние, адрес = фальшивый_сервер
    файл = tmp_path / "запись, часть 1.wav"
    файл.write_bytes(b"RIFF----WAVE")
    (tmp_path / "secret.txt").write_text("СЕКРЕТ", encoding="utf-8")
    итог = _asrctl(tmp_path, ["send", файл.name, "-Server", адрес, "-Model", "gigaam-v3-ctc",
                              "-Priority", "7", "-Reference", "<secret.txt", "-Wait",
                              "-Output", "результаты"])
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    тело = состояние["тело"]
    assert 'filename="запись, часть 1.wav"'.encode() in тело, тело[:400]
    assert b"=?utf-8?" not in тело
    assert b"<secret.txt" in тело and "СЕКРЕТ".encode() not in тело
    assert b'"model":"gigaam-v3-ctc"' in тело
    assert re.search(rb'name="?priority"?\r\n(?:[^\r\n]*\r\n)*\r\n7\r\n', тело), тело[-400:]
    результат = tmp_path / "результаты" / "злое имя.txt"
    assert результат.read_text(encoding="utf-8") == "готовый текст\n"
    assert not list(tmp_path.parent.glob("злое имя*")), "имя с сервера увело файл за каталог"


@нужен_pwsh
def test_клиент_windows_возвращает_коды_как_asrctl(tmp_path: Path, фальшивый_сервер):
    """Любая ошибка давала код 0: сценарии и планировщик не видели сбоев.

    Раньше же в PowerShell 7 терялся текст ошибки сервера — 404 выглядел как
    «сервер недоступен».
    """
    _, адрес = фальшивый_сервер
    итог = _asrctl(tmp_path, ["status", "missing", "-Server", адрес])
    assert итог.returncode == 1 and "Задание не найдено" in итог.stdout, итог.stdout
    assert "Проверьте идентификатор" in итог.stdout
    итог = _asrctl(tmp_path, ["health", "-Server", f"http://127.0.0.1:{_свободный_порт()}"])
    assert итог.returncode == 3, итог.stdout
    итог = _asrctl(tmp_path, ["send", "нет.wav", "-Server", адрес, "-Priority", "300"])
    assert итог.returncode == 2, итог.stdout
    итог = _asrctl(tmp_path, ["неизвестная"])
    assert итог.returncode == 2, итог.stdout
    (tmp_path / "ключ.txt").write_text("ah_ключ\n", encoding="utf-8")
    итог = _asrctl(tmp_path, ["health", "-Server", адрес, "-KeyFile", "ключ.txt"])
    assert итог.returncode == 2 and "знаки вне латиницы" in итог.stdout, итог.stdout


@нужен_pwsh
def test_клиент_windows_находит_файлы_каталога_без_recurse(tmp_path: Path, фальшивый_сервер):
    """`send-dir КАТАЛОГ` без -Recurse отвечал «нет файлов»: -Include без маски."""
    _, адрес = фальшивый_сервер
    каталог = tmp_path / "записи"
    (каталог / "вложенный").mkdir(parents=True)
    (каталог / "один.WAV").write_bytes(b"1")
    (каталог / "заметки.txt").write_text("x", encoding="utf-8")
    (каталог / "вложенный" / "два.mp3").write_bytes(b"2")
    итог = _asrctl(tmp_path, ["send-dir", str(каталог), "-Server", адрес])
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "Найдено файлов: 1" in итог.stdout and "один.WAV" in итог.stdout
    итог = _asrctl(tmp_path, ["send-dir", str(каталог), "-Recurse", "-Server", адрес])
    assert "Найдено файлов: 2" in итог.stdout, итог.stdout


@нужен_pwsh
def test_ожидание_переживает_перезапуск_и_знает_срок(tmp_path: Path, фальшивый_сервер):
    """`wait` бросал работу на первом 503 и висел вечно при остановленной очереди."""
    состояние, адрес = фальшивый_сервер
    состояние.update({"сбоев": 2, "ждать": 1})
    итог = _asrctl(tmp_path, ["wait", "job_1", "-Server", адрес, "-Output", "итог"])
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-2000:]
    assert "повтор через 5 с (2/5)" in итог.stdout
    состояние.clear()
    состояние.update({"ждать": 10_000})
    итог = _asrctl(tmp_path, ["wait", "job_1", "-Server", адрес, "-WaitLimitSec", "3"])
    assert итог.returncode == 2, итог.stdout[-1500:]
    assert "прервано по сроку (3 с)" in итог.stdout


@нужен_pwsh
def test_профиль_с_ключом_со_стандартного_ввода(tmp_path: Path, фальшивый_сервер):
    """`config add ИМЯ АДРЕС -`: ключ не виден в списке процессов и истории.

    Поданное на вход PowerShell отвергал («The input object cannot be
    bound…»), и профиль сохранялся без ключа.
    """
    состояние, адрес = фальшивый_сервер
    итог = _asrctl(tmp_path, ["config", "add", "прод", адрес + "/", "-"], ввод="ah_Secret123\n")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    профили = json.loads((tmp_path / "appdata" / "asrctl" / "profiles.json").read_text(encoding="utf-8"))
    assert профили["прод"] == {"url": адрес, "key": "ah_Secret123"}
    итог = _asrctl(tmp_path, ["send", "нет.wav", "-Profile", "прод"])
    assert итог.returncode == 1 and "Файл не найден" in итог.stdout
    (tmp_path / "а.wav").write_bytes(b"1")
    итог = _asrctl(tmp_path, ["send", "а.wav", "-Profile", "прод"])
    assert итог.returncode == 0 and состояние["ключ"] == "ah_Secret123", итог.stdout
    итог = _asrctl(tmp_path, ["config", "remove", "нет-такого"])
    assert итог.returncode == 1, итог.stdout

    # Конвейер внутри PowerShell (`Get-Content ключ.txt | .\\asrctl.ps1 …`):
    # строка приходит параметром из конвейера, а не стандартным вводом.
    клиент = КОРЕНЬ / "scripts" / "client" / "asrctl.ps1"
    итог = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command",
         f"'ah_Pipe456' | & {_в_кавычках(клиент)} config add второй {адрес} -"],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, cwd=str(tmp_path), timeout=180,
        env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1", "APPDATA": str(tmp_path / "appdata")})
    assert итог.returncode == 0, итог.stdout + итог.stderr
    профили = json.loads((tmp_path / "appdata" / "asrctl" / "profiles.json").read_text(encoding="utf-8"))
    assert профили["второй"]["key"] == "ah_Pipe456", профили


# ---------------------------------------------------------------------------
# Сервер без консоли и конфигурация с меткой BOM
# ---------------------------------------------------------------------------


def test_сервер_без_консоли_пишет_вывод_в_журнал(tmp_path: Path):
    """pythonw.exe: sys.stdout и sys.stderr — None, и uvicorn падал на isatty().

    Сообщение об ошибке конфигурации при этом пропадало бесследно. Теперь
    оба потока идут в logs/service.log каталога данных.
    """
    данные = tmp_path / "данные"
    данные.mkdir()
    (данные / "config.yaml").write_text(
        f"data_dir: {данные}\nserver:\n  server_port: восемь\n", encoding="utf-8")
    код = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.stdout = None; sys.stderr = None\n"
         "from asrhub.__main__ import main\n"
         f"raise SystemExit(main(['--config', {str(данные / 'config.yaml')!r}, '--check-config']))"],
        cwd=str(КОРЕНЬ / "server"), capture_output=True, text=True, timeout=120)
    assert код.returncode == 1, код.stderr
    журнал = (данные / "logs" / "service.log").read_text(encoding="utf-8")
    assert "Не принято значений: 1" in журнал, журнал


def test_конфигурация_с_меткой_bom_читается_и_без_pyyaml(tmp_path: Path, monkeypatch):
    """Windows PowerShell 5.1 пишет UTF-8 с меткой BOM — первый ключ терялся."""
    import builtins

    from asrhub.config import _load_structured

    файл = tmp_path / "config.yaml"
    файл.write_bytes("\ufeffdata_dir: /d\nserver:\n  server_port: 8081\n".encode("utf-8"))
    assert _load_structured(файл)["data_dir"] == "/d"
    настоящий_импорт = builtins.__import__

    def без_yaml(имя, *доводы, **ключи):
        if имя == "yaml":
            raise ModuleNotFoundError(имя)
        return настоящий_импорт(имя, *доводы, **ключи)

    monkeypatch.setattr(builtins, "__import__", без_yaml)
    assert _load_structured(файл)["data_dir"] == "/d"


# ---------------------------------------------------------------------------
# Образ Docker, прокси nginx, концы строк
# ---------------------------------------------------------------------------


def _блок_движков_из_dockerfile() -> str:
    текст = (КОРЕНЬ / "docker" / "Dockerfile").read_text(encoding="utf-8")
    начало = текст.index("RUN set -eux; \\\n    if [ -n \"${ENGINES}\" ]")
    конец = текст.index("\n\n", начало)
    блок = текст[начало + len("RUN "):конец]
    return блок.replace("\\\n", "\n")


def test_образ_ставит_движок_со_спутниками_требований(tmp_path: Path):
    """Образ ставил один основной файл: GigaAM (он только в no-deps) не попадал вовсе.

    Необязательная часть postprocess — тоже; её отказ образ не роняет.
    """
    журнал = tmp_path / "pip.log"
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    pip = bin_ / "pip"
    pip.write_text(f'#!/bin/bash\necho "$*" >> "{журнал}"\n'
                   'case "$*" in *optional/postprocess.txt*) exit 1 ;; esac\nexit 0\n',
                   encoding="utf-8")
    pip.chmod(0o755)
    shutil.copytree(КОРЕНЬ / "requirements", tmp_path / "requirements")
    блок = _блок_движков_из_dockerfile().replace("/opt/venv/.asrhub-engines",
                                                  str(tmp_path / "engines.txt"))
    итог = subprocess.run(["sh", "-c", блок], cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=60, env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}",
                                           "ENGINES": "gigaam,postprocess", "PROFILE": "standard"})
    assert итог.returncode == 0, итог.stdout + итог.stderr
    вызовы = журнал.read_text(encoding="utf-8").splitlines()
    assert "install --no-deps -r requirements/engines/no-deps/gigaam.txt" in вызовы, вызовы
    assert "install -r requirements/engines/optional/postprocess.txt" in вызовы, вызовы
    assert "необязательная часть движка postprocess не установилась" in итог.stdout


@pytest.mark.skipif(shutil.which("nginx") is None or shutil.which("openssl") is None,
                    reason="нужны nginx и openssl")
def test_шаблон_https_прокси_годен_и_закрывает_метрики(tmp_path: Path):
    """В шаблоне HTTPS не было ограничения метрик по сети и буферизация тела была включена.

    А «return 301» на весь сервер перенаправлял и /healthz — проверку самого
    прокси. Шаблон раскомментируется и проверяется настоящим nginx: /healthz
    по HTTP отвечает 200, остальное уходит на HTTPS.
    """
    текст = (КОРЕНЬ / "docker" / "nginx.conf").read_text(encoding="utf-8")
    строки = []
    for строка in текст.splitlines():
        голая = строка.lstrip()
        if голая.startswith("# set $asrhub_redirect") or голая.startswith("# if ($uri") \
                or голая.startswith("# if ($asrhub_redirect"):
            строка = строка.replace("# ", "", 1)
        строки.append(строка)
    текст = "\n".join(строки)
    https = текст.index("# server {")
    текст = текст[:https] + "\n".join(
        с[2:] if с.startswith("# ") else ("" if с.strip() == "#" else с)
        for с in текст[https:].splitlines())
    https_блок = текст[текст.index("listen 443 ssl"):]
    assert "deny all" in https_блок and "proxy_request_buffering off" in https_блок
    сертификаты = tmp_path / "certs"
    сертификаты.mkdir()
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=localhost", "-keyout", str(сертификаты / "privkey.pem"),
                    "-out", str(сертификаты / "fullchain.pem")],
                   check=True, capture_output=True, timeout=120)
    порт_http, порт_https = _свободный_порт(), _свободный_порт()
    # Образ прокси — nginx:alpine, где «http2 on» есть с 1.25.1; здешний nginx
    # бывает старше, и для него строка просто убирается.
    версия = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)",
                       subprocess.run(["nginx", "-v"], capture_output=True, text=True).stderr)
    if версия and tuple(map(int, версия.groups())) < (1, 25, 1):
        текст = текст.replace("    http2 on;\n", "")
    текст = (текст.replace("resolver 127.0.0.11", "resolver 127.0.0.53")
             .replace("http://asrhub:8080", "http://127.0.0.1:9")
             .replace("listen 80;", f"listen 127.0.0.1:{порт_http};")
             .replace("listen 443 ssl;", f"listen 127.0.0.1:{порт_https} ssl;")
             .replace("/etc/nginx/certs", str(сертификаты)))
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "conf.d" / "default.conf").write_text(текст, encoding="utf-8")
    главный = tmp_path / "nginx.conf"
    главный.write_text(f"""
worker_processes 1;
daemon off;
error_log {tmp_path}/error.log;
pid {tmp_path}/nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/body;
    proxy_temp_path {tmp_path}/proxy;
    fastcgi_temp_path {tmp_path}/fcgi;
    uwsgi_temp_path {tmp_path}/uwsgi;
    scgi_temp_path {tmp_path}/scgi;
    include {tmp_path}/conf.d/*.conf;
}}
""", encoding="utf-8")
    проверка = subprocess.run(["nginx", "-t", "-c", str(главный)], capture_output=True, text=True,
                              timeout=30)
    assert проверка.returncode == 0, проверка.stderr
    процесс = subprocess.Popen(["nginx", "-c", str(главный)], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        import urllib.error
        import urllib.request

        class БезПереходов(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        # Обработчик переходов — в build_opener: так он заменяет штатный, а
        # добавленный потом стоял бы вторым, и штатный успевал уйти по 301.
        открыватель = urllib.request.build_opener(urllib.request.ProxyHandler({}), БезПереходов)
        for _ in range(50):
            try:
                ответ = открыватель.open(f"http://127.0.0.1:{порт_http}/healthz", timeout=2)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)
        assert ответ.status == 200
        with pytest.raises(urllib.error.HTTPError) as перехват:
            открыватель.open(f"http://127.0.0.1:{порт_http}/api/jobs", timeout=2)
        assert перехват.value.code == 301
        assert перехват.value.headers["Location"].startswith("https://")
    finally:
        процесс.terminate()
        процесс.wait(timeout=10)


@pytest.mark.skipif(shutil.which("git") is None, reason="нужен git")
def test_сценарии_bash_после_клона_на_windows_остаются_с_lf(tmp_path: Path):
    """Клон с core.autocrlf=true превращал entrypoint.sh в CRLF — контейнер не стартовал."""
    репозиторий = tmp_path / "repo"
    (репозиторий / "docker").mkdir(parents=True)
    (репозиторий / "scripts" / "client").mkdir(parents=True)
    shutil.copy(КОРЕНЬ / ".gitattributes", репозиторий / ".gitattributes")
    for путь in ("docker/entrypoint.sh", "scripts/install.sh", "scripts/client/asrctl", "README.md"):
        (репозиторий / путь).write_text("строка\nещё строка\n", encoding="utf-8")
    окружение = {**os.environ, "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
                 "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

    def git(*доводы: str, где: Path = репозиторий) -> None:
        subprocess.run(["git", *доводы], cwd=str(где), check=True, capture_output=True,
                       env=окружение, timeout=60)

    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "проба")
    клон = tmp_path / "клон"
    subprocess.run(["git", "-c", "core.autocrlf=true", "clone", "-q", str(репозиторий), str(клон)],
                   check=True, capture_output=True, env=окружение, timeout=60)
    for путь in ("docker/entrypoint.sh", "scripts/install.sh", "scripts/client/asrctl"):
        assert b"\r\n" not in (клон / путь).read_bytes(), путь
    # А обычный текст autocrlf по-прежнему правит — значит, проверка о деле.
    assert b"\r\n" in (клон / "README.md").read_bytes()


# ---------------------------------------------------------------------------
# Установщик: конфигурация и брандмауэр
# ---------------------------------------------------------------------------


def _блок_конфигурации() -> str:
    текст = (КОРЕНЬ / "scripts" / "install.ps1").read_text(encoding="utf-8-sig")
    начало = текст.index("$configFile = Join-Path $DataDir 'config.yaml'")
    конец = текст.index("# Токен Hugging Face кладём в config.yaml")
    return текст[начало:конец]


@нужен_pwsh
def test_откат_после_reset_config_возвращает_прежний_файл(tmp_path: Path):
    """Откат неудачной установки с -ResetConfig удалял config.yaml.

    Копия лежала рядом, но машина оставалась вовсе без конфигурации — без
    ключей интеграций, токена и настроек. Настоящий кусок install.ps1
    выполняется над временным каталогом, затем — откат.
    """
    данные = tmp_path / "данные"
    данные.mkdir()
    прежний = "hf_token: \"hf_прежний\"\nserver:\n  server_port: 8081\n"
    (данные / "config.yaml").write_text(прежний, encoding="utf-8")
    итог = _pwsh(tmp_path, f"""
$ErrorActionPreference = 'Stop'
Import-Module {_в_кавычках(МОДУЛЬ)} -Force
$DataDir = {_в_кавычках(данные)}
$ResetConfig = $true; $Force = $false; $Models = 'demo-simulator'
$hw = @{{ Accelerator = 'cpu' }}; $BindHost = '127.0.0.1'; $Port = 8090
{_блок_конфигурации()}
"ЗАМЕНЁН " + ((Get-Content -Raw -LiteralPath $configFile) -match 'server_port: 8090')
Invoke-Rollback
""")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "ЗАМЕНЁН True" in итог.stdout, итог.stdout
    assert (данные / "config.yaml").read_text(encoding="utf-8") == прежний
    assert list(данные.glob("config.yaml.bak.*")), "копия прежнего файла пропала"


@нужен_pwsh
@pytest.mark.parametrize("адрес, ждём_правило", [("127.0.0.1", False), ("0.0.0.0", True)])
def test_доктор_спрашивает_брандмауэр_только_у_сервера_открытого_в_сеть(
        tmp_path: Path, адрес: str, ждём_правило: bool):
    """Предупреждение «правило брандмауэра не создано» звучало всегда.

    И у сервера на 127.0.0.1, которому правило не нужно вовсе, — и
    приучало его не читать.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка(tmp_path, "ASR", данные)
    (данные / "config.yaml").write_text(
        f"server:\n  server_host: {адрес}\n  server_port: {_свободный_порт()}\n", encoding="utf-8")
    итог = _pwsh(tmp_path, _заглушки_ps(журнал) + """
function global:Get-NetFirewallRule { param($Direction, $Enabled, $Action, $DisplayName, $ErrorAction) }
function global:Get-NetFirewallPortFilter { param($ErrorAction) }
""" + f"& {_в_кавычках(программа / 'scripts' / 'doctor.ps1')} -Only network\n",
                 env=_путь_с_заглушками(tmp_path, журнал))
    assert ("Правило брандмауэра" in итог.stdout) is ждём_правило, итог.stdout[-2000:]
    if ждём_правило:
        assert "нет правила для порта" in итог.stdout
