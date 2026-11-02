"""Заход 54: сценарии PowerShell (участок powershell).

pwsh в среде нет, поэтому сами сценарии здесь не запускаются — правки
сделаны по коду и помечены в отчёте «не проверено запуском». Проверяется
то, что проверяется без PowerShell: код на Python, который сценарии
отдают интерпретатору (его гоняем по-настоящему с теми аргументами, что
передаст Windows PowerShell 5.1), и по тексту — то, что иначе не увидеть:
метка BOM, порядок действий, состав настроек службы.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
СЦЕНАРИИ = КОРЕНЬ / "scripts"


def _текст(имя: str) -> str:
    return (СЦЕНАРИИ / имя).read_text(encoding="utf-8-sig")


@pytest.mark.parametrize("имя", sorted(
    str(p.relative_to(СЦЕНАРИИ)) for p in [*СЦЕНАРИИ.rglob("*.ps1"), *СЦЕНАРИИ.rglob("*.psm1")]))
def test_сценарии_сохранили_метку_bom(имя: str):
    """Без BOM Windows PowerShell 5.1 читает файл в ANSI, и кириллица ломает разбор."""
    assert (СЦЕНАРИИ / имя).read_bytes()[:3] == b"\xef\xbb\xbf", имя


def test_список_моделей_работает_с_заменителем_пустого_языка(tmp_path: Path):
    """№4: `models.ps1 -Action list` без -Language падал на 5.1 с IndexError.

    PowerShell 5.1 пустую строку внешней программе не передаёт; теперь
    сценарий передаёт «-». Код пробы гоняется с ровно этими аргументами.
    """
    текст = _текст("models.ps1")
    начало = текст.index("$languageArg = if ($Language)")
    код = текст[текст.index("-Code @'\n", начало) + len("-Code @'\n"):]
    код = код[:код.index("\n'@")]
    проба = tmp_path / "проба.py"
    проба.write_text(код, encoding="utf-8")
    итог = subprocess.run([sys.executable, str(проба), "0", "-", str(tmp_path / "models")],
                          cwd=str(КОРЕНЬ / "server"), capture_output=True, text=True, timeout=120,
                          check=False, env={**os.environ, "PYTHONPATH": str(КОРЕНЬ / "server")})
    assert итог.returncode == 0, итог.stderr[-2000:]
    assert "gigaam" in итог.stdout.lower(), итог.stdout[:500]


def test_обновление_контейнера_сохраняет_env_и_пересобирает_при_откате():
    """№1 (критично): update.ps1 стирал docker\\.env — по тексту, не проверено запуском."""
    текст = _текст("update.ps1")
    assert "Copy-AsrDockerOverlay" in текст
    assert "Invoke-AsrComposeRebuild" in текст
    общий = _текст("lib/Common.psm1")
    assert re.search(r"\$script:AsrDockerLocal\s*=\s*@\('\.env',\s*'certs',\s*'data'\)", общий), \
        "местные файлы каталога docker"
    # Без .env у установки в контейнере — остановка до первого изменения.
    позиция_отказа = текст.find("if ($dockerMode -and -not (Test-Path -LiteralPath (Join-Path $Prefix 'docker\\.env')))")
    копирование = текст.index("Copy-Staged -From $sourcePath -Item $item -KeepOld")
    assert позиция_отказа != -1 and позиция_отказа < копирование, \
        "проверка .env должна стоять до копирования"


def test_служба_пишет_оба_потока_в_один_журнал_с_ротацией_на_ходу():
    """№14: журнал службы NSSM был пустым, а рос без предела."""
    текст = _текст("service.ps1")
    stdout = re.search(r"@\('AppStdout', \(Join-Path \$logDir '([^']+)'\)\)", текст)
    stderr = re.search(r"@\('AppStderr', \(Join-Path \$logDir '([^']+)'\)\)", текст)
    assert stdout and stderr and stdout.group(1) == stderr.group(1)
    assert "@('AppRotateOnline', '1')" in текст
    # logs — самый свежий непустой файл.
    действие = текст[текст.index("'logs' {"):текст.index("'backup' {")]
    assert "Length -gt 0" in действие and "LastWriteTime" in действие


def test_служба_проверяет_ответ_сервера_прежде_чем_оставить_свою_учётную_запись():
    """№5: возврат на LocalSystem срабатывал только на исключение Start-Service."""
    текст = _текст("service.ps1")
    функция = текст[текст.index("function Install-AsService"):текст.index("function Install-AsTask")]
    assert "Wait-AsrHealth" in функция
    assert функция.index("Wait-AsrHealth") < функция.index("'obj=', 'LocalSystem'")
    assert "pyvenv.cfg" in текст and "Get-VenvBaseHome" in текст


def test_без_прав_администратора_задача_останавливается_до_удаления():
    """№12: Unregister-ScheduledTask не завершал работающий экземпляр."""
    текст = _текст("service.ps1")
    ветка = текст[текст.index("Install-AsService -Address $address"):]
    ветка = ветка[:ветка.index("Install-AsTask -Address $address")]
    assert ветка.index("Stop-ScheduledTask") < ветка.index("Unregister-ScheduledTask")
    assert "Stop-PrefixProcesses" in ветка


def test_у_службы_windows_есть_копия_и_восстановление():
    """Расхождение с service.sh: backup и restore на Windows не было."""
    текст = _текст("service.ps1")
    assert re.search(r"ValidateSet\([^)]*'backup', 'restore'\)", текст)
    восстановление = текст[текст.index("'restore' {"):]
    # Пакет проверяется до остановки — иначе служба стояла бы впустую.
    assert восстановление.index("import asrhub.maintenance") < восстановление.index("Stop-Service")


def test_клиент_windows_читает_сообщение_ошибки_с_верхнего_уровня():
    """№18: «Ошибка 413» без причины — читался только detail."""
    текст = _текст("client/asrctl.ps1")
    функция = текст[текст.index("function Get-ApiResult"):текст.index("function Invoke-Api")]
    assert функция.index("-contains 'message'") < функция.index("-contains 'detail'")
    assert "[string]$Settings" in текст and "ConvertTo-Json -Compress -Depth 20" in текст


def test_модуль_включает_tls12_при_загрузке():
    """№19: проверка сети на Server 2016 видела pypi.org «недоступным»."""
    общий = _текст("lib/Common.psm1")
    начало_функций = общий.index("function ")
    assert "Tls12" in общий[:начало_функций]


def test_подсказки_запускают_сценарии_в_обход_политики():
    """Недоделка: подсказки «powershell -File …» не запускались при Restricted."""
    for путь in [*СЦЕНАРИИ.glob("*.ps1"), СЦЕНАРИИ / "lib" / "Common.psm1"]:
        текст = путь.read_text(encoding="utf-8-sig")
        assert "powershell -File " not in текст, путь.name
