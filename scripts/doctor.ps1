<#
.SYNOPSIS
    Диагностика установки ASR Hub на Windows.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1
    powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1 -Fix
#>
[CmdletBinding()]
param(
    [string]$Prefix = '',
    [string]$DataDir = '',
    [switch]$Fix,
    [ValidateSet('', 'hardware', 'engines', 'network')][string]$Only = ''
)

$ErrorActionPreference = 'Continue'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force
Show-Banner

$script:Passed = 0; $script:Warned = 0; $script:Failed = 0

function Test-Item {
    param([string]$Name, [ValidateSet('ok','warn','fail')][string]$Status,
          [string]$Detail = '', [string]$Hint = '')
    $marks = @{ ok = @('✓', 'Green'); warn = @('!', 'Yellow'); fail = @('✕', 'Red') }
    switch ($Status) {
        'ok'   { $script:Passed++ } 'warn' { $script:Warned++ } 'fail' { $script:Failed++ }
    }
    Write-Host ("  {0} {1,-38} {2}" -f $marks[$Status][0], $Name, $Detail) `
        -ForegroundColor $marks[$Status][1]
    if ($Hint -and $Status -ne 'ok') { Write-Host "      $Hint" -ForegroundColor DarkGray }
}

if (-not $Prefix) {
    foreach ($c in @('C:\Program Files\ASRHub', (Join-Path $env:LOCALAPPDATA 'ASRHub'))) {
        if (Test-Path $c) { $Prefix = $c; break }
    }
}
if (-not $DataDir) {
    foreach ($c in @((Join-Path $env:ProgramData 'ASRHub'), (Join-Path $env:LOCALAPPDATA 'ASRHub\data'))) {
        if (Test-Path $c) { $DataDir = $c; break }
    }
}

if (-not $Only -or $Only -eq 'hardware') {
    Write-Heading 'Оборудование'
    $hw = Get-HardwareInfo
    Test-Item 'Операционная система' 'ok' "$($hw.OsName) ($($hw.OsVersion))"
    Test-Item 'Права администратора' $(if (Test-Administrator) { 'ok' } else { 'warn' }) `
        $(if (Test-Administrator) { 'есть' } else { 'нет' }) `
        'Без прав администратора служба создаётся как задача планировщика.'
    Test-Item 'Ядер процессора' $(if ($hw.CpuCores -ge 4) { 'ok' } else { 'warn' }) $hw.CpuCores `
        'Меньше четырёх ядер: обработка на процессоре будет медленной.'
    Test-Item 'Оперативная память' `
        $(if ($hw.RamGb -ge 16) { 'ok' } elseif ($hw.RamGb -ge 6) { 'warn' } else { 'fail' }) `
        "$($hw.RamGb) ГБ" 'Для моделей уровня large рекомендуется 16 ГБ.'
    if ($hw.Accelerator -eq 'cuda') {
        Test-Item 'Видеокарта' 'ok' "$($hw.GpuName) — $($hw.GpuMemoryMb) МБ"
        Test-Item 'CUDA' 'ok' $hw.CudaVersion
        $venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
        if (Test-Path $venvPython) {
            $torchCuda = & $venvPython -c "import torch;print(torch.cuda.is_available())" 2>$null
            Test-Item 'PyTorch видит видеокарту' $(if ($torchCuda -eq 'True') { 'ok' } else { 'fail' }) `
                "$torchCuda" "Переустановите PyTorch: `"$Prefix\venv\Scripts\pip.exe`" install --force-reinstall --index-url $(Get-TorchIndexUrl -Accelerator cuda -CudaVersion $hw.CudaVersion) torch torchaudio"
        }
    } else {
        Test-Item 'Видеокарта' 'warn' 'не обнаружена' `
            'Работа только на процессоре. Включите int8 и берите модели полегче.'
    }
    if ($DataDir) {
        $drive = Get-PSDrive -Name (Split-Path -Qualifier $DataDir).TrimEnd(':') -ErrorAction SilentlyContinue
        if ($drive) {
            $freeGb = [math]::Round($drive.Free / 1GB, 1)
            Test-Item 'Свободное место' `
                $(if ($freeGb -ge 20) { 'ok' } elseif ($freeGb -ge 5) { 'warn' } else { 'fail' }) `
                "$freeGb ГБ" 'Полный набор моделей занимает свыше 100 ГБ.'
        }
    }
}

if (-not $Only) {
    Write-Heading 'Установка'
    Test-Item 'Каталог программы' $(if ($Prefix -and (Test-Path $Prefix)) { 'ok' } else { 'fail' }) `
        $(if ($Prefix) { $Prefix } else { 'не найден' }) `
        'Запустите установку: powershell -File scripts\install.ps1'
    Test-Item 'Каталог данных' $(if ($DataDir -and (Test-Path $DataDir)) { 'ok' } else { 'fail' }) `
        $(if ($DataDir) { $DataDir } else { 'не найден' })
    foreach ($sub in @('uploads', 'results', 'models', 'logs', 'tmp')) {
        $path = Join-Path $DataDir $sub
        $exists = Test-Path $path
        Test-Item "  $sub" $(if ($exists) { 'ok' } else { 'fail' }) `
            $(if ($exists) { 'есть' } else { 'нет каталога' }) "New-Item -ItemType Directory -Path '$path' -Force"
        if ($Fix -and -not $exists) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    }
    $venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
    Test-Item 'Виртуальное окружение' $(if (Test-Path $venvPython) { 'ok' } else { 'fail' }) `
        $(if (Test-Path $venvPython) { (& $venvPython --version) } else { 'не найдено' })
    Test-Item 'Конфигурация' $(if (Test-Path (Join-Path $DataDir 'config.yaml')) { 'ok' } else { 'warn' }) `
        $(if (Test-Path (Join-Path $DataDir 'config.yaml')) { 'есть' } else { 'используются значения по умолчанию' })
}

if (-not $Only -or $Only -eq 'engines') {
    Write-Heading 'Внешние программы'
    foreach ($tool in @('ffmpeg', 'ffprobe', 'git', 'curl')) {
        $exists = Test-CommandExists $tool
        $hint = switch ($tool) {
            'ffmpeg'  { 'winget install Gyan.FFmpeg' }
            'ffprobe' { 'Ставится вместе с ffmpeg.' }
            'git'     { 'winget install Git.Git — нужен для GigaAM.' }
            default   { '' }
        }
        Test-Item $tool $(if ($exists) { 'ok' } else { if ($tool -eq 'ffmpeg') { 'fail' } else { 'warn' } }) `
            $(if ($exists) { 'найден' } else { 'не найден' }) $hint
    }

    Write-Heading 'Движки распознавания'
    $venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
    if (Test-Path $venvPython) {
        Push-Location (Join-Path $Prefix 'server')
        try {
            & $venvPython -c @"
import sys
sys.path.insert(0, '.')
from asrhub.engines import engine_status
for item in engine_status():
    mark = 'OK ' if item['available'] else '--- '
    print(f\"  {mark} {item['id']:<20} {item['reason'][:64] if not item['available'] else 'установлен'}\")
"@
        } catch { Test-Item 'Проверка движков' 'fail' 'не удалось выполнить' }
        finally { Pop-Location }
    } else {
        Test-Item 'Движки' 'fail' 'нет виртуального окружения'
    }
}

if (-not $Only -or $Only -eq 'network') {
    Write-Heading 'Сеть'
    foreach ($hostName in @('pypi.org', 'huggingface.co', 'github.com')) {
        Test-Item $hostName $(if (Test-Internet -HostName $hostName) { 'ok' } else { 'warn' }) `
            $(if (Test-Internet -HostName $hostName) { 'доступен' } else { 'недоступен' }) `
            'Без доступа установка движков и загрузка моделей невозможны.'
    }
    $port = 8080
    $configFile = Join-Path $DataDir 'config.yaml'
    if (Test-Path $configFile) {
        $match = Select-String -Path $configFile -Pattern 'server_port:\s*(\d+)' | Select-Object -First 1
        if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3
        Test-Item 'Сервер' 'ok' "отвечает на порту $port, версия $($health.version)"
    } catch {
        Test-Item "Порт $port" 'warn' 'сервер не отвечает' `
            'Запустить: powershell -File scripts\service.ps1 -Action start'
    }
    $rule = Get-NetFirewallRule -DisplayName 'ASR Hub*' -ErrorAction SilentlyContinue
    Test-Item 'Правило брандмауэра' $(if ($rule) { 'ok' } else { 'warn' }) `
        $(if ($rule) { 'создано' } else { 'не создано' }) `
        "New-NetFirewallRule -DisplayName 'ASR Hub' -Direction Inbound -LocalPort $port -Protocol TCP -Action Allow"
}

Write-Host ''
Write-Heading 'Итог'
Write-Host ("  ✓ пройдено: {0}   ! предупреждений: {1}   ✕ ошибок: {2}" -f
    $script:Passed, $script:Warned, $script:Failed)
Write-Host ''
if ($script:Failed -gt 0) {
    Write-Host '  Есть критические проблемы — сервер может не работать.' -ForegroundColor Red
    Write-Host '  Попробуйте: powershell -File scripts\doctor.ps1 -Fix' -ForegroundColor DarkGray
    exit 1
} elseif ($script:Warned -gt 0) {
    Write-Host '  Сервер работоспособен, часть возможностей ограничена.' -ForegroundColor Yellow
} else {
    Write-Host '  Всё в порядке.' -ForegroundColor Green
}
Write-Host ''
