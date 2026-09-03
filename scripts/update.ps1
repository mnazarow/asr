<#
.SYNOPSIS
    Обновление ASR Hub на Windows.
.DESCRIPTION
    Создаёт снимок текущей установки и резервную копию базы, обновляет
    файлы и зависимости, проверяет работоспособность. При неудачной
    проверке предлагает автоматический откат.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\update.ps1
    powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Check
    powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Rollback
#>
[CmdletBinding()]
param(
    [string]$Prefix = '',
    [string]$DataDir = '',
    [string]$Source = '',
    [switch]$EnginesOnly,
    [switch]$Check,
    [switch]$Rollback,
    [switch]$NoRestart,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force
Set-AssumeYes $Yes.IsPresent
Set-DryRun $DryRun.IsPresent
Initialize-AsrLog | Out-Null
Show-Banner
trap { Invoke-AsrFailure $_ }

if (-not $Source) { $Source = Split-Path -Parent $PSScriptRoot }
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
if (-not $Prefix) { Write-Err 'Установка не найдена. Укажите -Prefix.'; exit 2 }

$snapshotDir = Join-Path (Split-Path -Parent $Prefix) 'ASRHub-snapshot'
$venvPip = Join-Path $Prefix 'venv\Scripts\pip.exe'
$venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
$currentVersion = if (Test-Path (Join-Path $Prefix 'VERSION')) { (Get-Content (Join-Path $Prefix 'VERSION') -Raw).Trim() } else { 'неизвестна' }
$newVersion = if (Test-Path (Join-Path $Source 'VERSION')) { (Get-Content (Join-Path $Source 'VERSION') -Raw).Trim() } else { 'неизвестна' }

if ($Rollback) {
    Write-Heading 'Откат к предыдущей версии'
    if (-not (Test-Path $snapshotDir)) { Write-Err "Снимок не найден: $snapshotDir"; exit 2 }
    Write-Info "Снимок от $((Get-Item $snapshotDir).LastWriteTime)"
    if (-not (Confirm-Action 'Восстановить предыдущую версию?')) { exit 0 }
    & (Join-Path $PSScriptRoot 'service.ps1') -Action stop -Prefix $Prefix -DataDir $DataDir
    foreach ($item in @('server', 'scripts', 'config', 'requirements', 'docker', 'examples', 'VERSION', 'README.md')) {
        $source = Join-Path $snapshotDir $item
        if (Test-Path $source) {
            $target = Join-Path $Prefix $item
            if (Test-Path $target) { Remove-Item $target -Recurse -Force }
            Copy-Item $source $target -Recurse -Force
        }
    }
    & (Join-Path $PSScriptRoot 'service.ps1') -Action start -Prefix $Prefix -DataDir $DataDir
    Write-Ok 'Откат выполнен'
    exit 0
}

Write-Heading 'Проверка обновления'
Write-Host "  Установлено   $currentVersion"
Write-Host "  Доступно      $newVersion"
Write-Host "  Источник      $Source"
Write-Host ''

if (-not $EnginesOnly -and -not (Test-Path (Join-Path $Source 'server'))) {
    Write-Err "В каталоге источника нет папки server: $Source"
    Write-Hint 'Укажите путь к распакованному дистрибутиву: -Source C:\путь\asr-hub'
    exit 2
}
if ($Check) { Write-Info 'Режим проверки — изменения не вносились.'; exit 0 }
if (-not (Confirm-Action 'Выполнить обновление?')) { Write-Info 'Отменено.'; exit 0 }

Set-StepTotal 6

Write-Step 'Снимок текущей версии'
if (-not (Get-DryRun)) {
    if (Test-Path $snapshotDir) { Remove-Item $snapshotDir -Recurse -Force }
    New-Item -ItemType Directory -Path $snapshotDir -Force | Out-Null
    foreach ($item in @('server', 'scripts', 'config', 'requirements', 'docker', 'examples', 'VERSION', 'README.md')) {
        $source = Join-Path $Prefix $item
        if (Test-Path $source) { Copy-Item $source $snapshotDir -Recurse -Force }
    }
    Write-Ok "Снимок: $snapshotDir"
    Write-Hint 'Откат при проблемах: powershell -File scripts\update.ps1 -Rollback'
}

Write-Step 'Резервная копия базы'
$dbFile = Join-Path $DataDir 'asrhub.db'
if ((Test-Path $dbFile) -and -not (Get-DryRun)) {
    $dbBackup = "$dbFile.bak.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    Copy-Item $dbFile $dbBackup -Force
    Write-Ok "Копия базы: $(Split-Path -Leaf $dbBackup) ($(Format-Size (Get-Item $dbBackup).Length))"
    Get-ChildItem -Path $DataDir -Filter 'asrhub.db.bak.*' |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

Write-Step 'Остановка службы'
try { & (Join-Path $PSScriptRoot 'service.ps1') -Action stop -Prefix $Prefix -DataDir $DataDir }
catch { Write-Info 'Служба не запущена.' }

if (-not $EnginesOnly) {
    Write-Step 'Обновление файлов'
    # Источник по умолчанию — каталог рядом со скриптом, а скрипт скрипт
    # запускают из самой установки: именно эту команду печатает установщик.
    # Без проверки цикл удалял каталог и копировал его сам в себя, оставляя
    # установку без server.
    $srcReal = [System.IO.Path]::GetFullPath($Source)
    $dstReal = [System.IO.Path]::GetFullPath($Prefix)
    if ($srcReal -eq $dstReal) {
        Write-Info 'Источник совпадает с установкой — файлы уже на месте.'
        Write-Hint 'Чтобы обновиться из другого места: update.ps1 -Source C:\путь\к\новой\версии'
    } elseif (-not (Get-DryRun)) {
        foreach ($item in @('server', 'scripts', 'config', 'requirements', 'docker', 'examples', 'VERSION', 'README.md')) {
            $sourcePath = Join-Path $Source $item
            if (Test-Path $sourcePath) {
                $target = Join-Path $Prefix $item
                if (Test-Path $target) { Remove-Item $target -Recurse -Force }
                Copy-Item $sourcePath $target -Recurse -Force
            }
        }
    }
    Write-Ok "Файлы обновлены до версии $newVersion"
}

# Установка в контейнере обновляется иначе: файлы лежат в образе, и без его
# пересборки новая версия просто не попадает в работу. В bash-двойнике этот
# путь есть с самого начала, здесь его не было вовсе: venv не находился,
# образ не пересобирался, работающий контейнер отвечал на проверку живости
# СТАРЫМ кодом — и скрипт объявлял обновление успешным.
$dockerMode = $false
if ((Test-Path (Join-Path $Prefix 'docker\.env')) -and (Get-Command docker -ErrorAction SilentlyContinue)) {
    $dockerMode = $true
} elseif (Get-Command docker -ErrorAction SilentlyContinue) {
    $running = & docker ps --filter 'name=^asrhub$' --format '{{.Names}}' 2>$null
    if ($running) { $dockerMode = $true }
}

function Get-ComposeCommand {
    & docker compose version *> $null
    $base = if ($LASTEXITCODE -eq 0) { @('docker', 'compose') } else { @('docker-compose') }
    $files = @('-f', 'docker-compose.yml')
    $envFile = Join-Path $Prefix 'docker\.env'
    if ((Test-Path $envFile) -and (Select-String -Path $envFile -Pattern '^ASRHUB_ACCEL=cuda' -Quiet) `
        -and (Test-Path (Join-Path $Prefix 'docker\docker-compose.gpu.yml'))) {
        $files += @('-f', 'docker-compose.gpu.yml')
    }
    return @{ Command = $base[0]; Prefix = ($base[1..($base.Count - 1)] + $files) }
}

Write-Step 'Обновление зависимостей'
if ($dockerMode) {
    Write-Info 'Зависимости живут в образе — обновятся при пересборке.'
} elseif (Test-Path $venvPip) {
    Invoke-WithRetry -Attempts 2 -Description 'базовые зависимости' -Action {
        Invoke-Checked -Command $venvPip -Arguments @('install', '--upgrade',
            '--disable-pip-version-check', '-r', (Join-Path $Prefix 'requirements\base.txt'))
    } | Out-Null
    foreach ($req in (Get-ChildItem (Join-Path $Prefix 'requirements\engines') -Filter '*.txt')) {
        # Имя файла требований не равно имени модуля: у diarization, vad,
        # postprocess, mfa, qwen3-asr и ещё нескольких такого модуля нет
        # вовсе, и проверка молча не срабатывала — эти движки не
        # обновлялись никогда. Спрашиваем pip про сами пакеты из файла.
        $installed = $false
        foreach ($line in (Get-Content $req.FullName)) {
            $name = ($line -split '#')[0]
            $name = ($name -split '[<>=!;\[]')[0].Trim()
            if (-not $name) { continue }
            & $venvPip show $name *> $null
            if ($LASTEXITCODE -eq 0) { $installed = $true; break }
        }
        if ($installed) {
            Write-Info "Движок $($req.BaseName) установлен — обновляем"
            try {
                Invoke-Checked -Command $venvPip -Arguments @('install', '--upgrade',
                    '--disable-pip-version-check', '-r', $req.FullName) | Out-Null
            } catch { Write-Warn "  $($req.BaseName): обновление не удалось" }
        }
    }
    Write-Ok 'Зависимости обновлены'
} else { Write-Warn 'Виртуальное окружение не найдено.' }

Write-Step 'Запуск и проверка'
if ($NoRestart) { Write-Info 'Перезапуск пропущен.'; exit 0 }
if (Get-DryRun) { Write-Ok 'Пробный запуск завершён.'; exit 0 }

$port = 8080
if ($dockerMode) {
    # Без пересборки образа новый код в контейнер не попадает: проверка
    # живости отвечала бы 200 от старой версии, и обновление объявлялось
    # успешным, ничего не изменив.
    $compose = Get-ComposeCommand
    Push-Location (Join-Path $Prefix 'docker')
    try {
        Write-Info 'Пересборка образа (может занять несколько минут)…'
        Invoke-Checked -Command $compose.Command `
            -Arguments ($compose.Prefix + @('--env-file', '.env', 'build')) | Out-Null
        Invoke-Checked -Command $compose.Command `
            -Arguments ($compose.Prefix + @('--env-file', '.env', 'up', '-d')) | Out-Null
    } catch {
        Write-Err 'Пересборка образа не удалась. Прежний контейнер не тронут.'
        exit 1
    } finally { Pop-Location }
    $envFile = Join-Path $Prefix 'docker\.env'
    if (Test-Path $envFile) {
        $match = Select-String -Path $envFile -Pattern '^ASRHUB_PORT=(\d+)' | Select-Object -First 1
        if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
    }
} else {
    & (Join-Path $PSScriptRoot 'service.ps1') -Action start -Prefix $Prefix -DataDir $DataDir
    $configFile = Join-Path $DataDir 'config.yaml'
    if (Test-Path $configFile) {
        $match = Select-String -Path $configFile -Pattern 'server_port:\s*(\d+)' | Select-Object -First 1
        if ($match) { $port = [int]$match.Matches[0].Groups[1].Value }
    }
}

$healthy = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}

if ($healthy) {
    Write-Ok 'Сервер отвечает — обновление успешно'
    Write-Host ''
    Write-Host "Обновление завершено: $currentVersion → $newVersion" -ForegroundColor Green
    Write-Hint 'Откат при необходимости: powershell -File scripts\update.ps1 -Rollback'
} else {
    Write-Err 'Сервер не отвечает после обновления.'
    if (Confirm-Action 'Откатиться к предыдущей версии?') {
        & $PSCommandPath -Rollback -Prefix $Prefix -DataDir $DataDir -Yes
    }
    exit 1
}
