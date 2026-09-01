<#
.SYNOPSIS
    Установка ASR Hub на Windows.

.DESCRIPTION
    Проверяет окружение, ставит зависимости, создаёт службу автозапуска
    и загружает выбранные модели. Скрипт идемпотентен: повторный запуск
    обновляет установку. При ошибке выполняется откат изменений.

.PARAMETER Prefix
    Каталог установки. По умолчанию C:\Program Files\ASRHub при запуске
    от администратора, иначе %LOCALAPPDATA%\ASRHub.

.PARAMETER Profile
    Состав установки: light, cpu, standard, full, russian.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile standard -Port 9000
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Mode docker -Profile full
#>
[CmdletBinding()]
param(
    [string]$Prefix = '',
    [string]$DataDir = '',
    [int]$Port = 8080,
    [string]$BindHost = '0.0.0.0',
    [ValidateSet('native', 'docker')][string]$Mode = 'native',
    [ValidateSet('', 'light', 'cpu', 'standard', 'full', 'russian')][string]$Profile = '',
    [string]$Engines = '',
    [string]$Models = '',
    [switch]$SkipModels,
    [switch]$NoService,
    [switch]$Offline,
    [switch]$Force,
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force

Set-DryRun $DryRun.IsPresent
Set-AssumeYes $Yes.IsPresent
Set-Quiet $Quiet.IsPresent

$RepoDir = Split-Path -Parent $PSScriptRoot
$logFile = Initialize-AsrLog
Show-Banner

trap { Invoke-AsrFailure $_ }

# ---------------------------------------------------------------------------
# Значения по умолчанию
# ---------------------------------------------------------------------------

$isAdmin = Test-Administrator
if (-not $Prefix) {
    $Prefix = if ($isAdmin) { 'C:\Program Files\ASRHub' } else { Join-Path $env:LOCALAPPDATA 'ASRHub' }
}
if (-not $DataDir) {
    $DataDir = if ($isAdmin) { Join-Path $env:ProgramData 'ASRHub' } else { Join-Path $env:LOCALAPPDATA 'ASRHub\data' }
}
if (-not $Profile) { $Profile = Get-RecommendedProfile }

$profileEngines = @{
    light    = 'faster_whisper'
    cpu      = 'faster_whisper,gigaam,vosk'
    standard = 'gigaam,faster_whisper,whisper'
    russian  = 'gigaam,vosk'
    full     = 'gigaam,faster_whisper,whisper,nemo,transformers,vosk,qwen3_asr'
}
$profileModels = @{
    light    = 'faster-whisper-small'
    cpu      = 'gigaam-v3-ctc,faster-whisper-small'
    standard = 'gigaam-v3-e2e-rnnt,faster-whisper-large-v3'
    russian  = 'gigaam-v3-e2e-rnnt,gigaam-v3-ctc,vosk-small-ru-0.22'
    full     = 'gigaam-v3-e2e-rnnt,gigaam-v3-rnnt,faster-whisper-large-v3,parakeet-tdt-0.6b-v3'
}
$profileDisk = @{ light = 3; cpu = 6; standard = 12; russian = 10; full = 60 }

if (-not $Engines) { $Engines = $profileEngines[$Profile] }
if (-not $Models -and -not $SkipModels) { $Models = $profileModels[$Profile] }

$venv = Join-Path $Prefix 'venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'
$venvPip = Join-Path $venv 'Scripts\pip.exe'

# ---------------------------------------------------------------------------

Set-StepTotal 9
Write-Step 'Проверка окружения'

$hw = Show-Environment

Write-Info "Профиль установки: $Profile"
Write-Info "Движки: $Engines"
Write-Info "Каталог установки: $Prefix"
Write-Info "Каталог данных: $DataDir"
Write-Info "Адрес сервера: http://${BindHost}:${Port}"
if ($Models) { Write-Info "Модели к загрузке: $Models" }

if (-not $isAdmin) {
    Write-Warn 'PowerShell запущен без прав администратора.'
    Write-Hint 'Установка пойдёт в профиль пользователя; служба будет создана как задача планировщика.'
    Write-Hint 'Для установки в Program Files и создания системной службы запустите от администратора.'
}

if ($Mode -eq 'native') {
    $python = Find-Python
    if (-not $python) {
        Write-Err "Не найден Python 3.10 или новее."
        Write-Hint 'Установите: winget install Python.Python.3.12'
        Write-Hint 'Либо скачайте с https://www.python.org/downloads/ (отметьте «Add python.exe to PATH»)'
        exit 1
    }
    Write-Ok "Python: $python ($(& $python --version))"
} else {
    if (-not (Test-CommandExists 'docker')) {
        Write-Err 'Не найден Docker.'
        Write-Hint 'Установите Docker Desktop: winget install Docker.DockerDesktop'
        exit 1
    }
    & docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Err 'Docker установлен, но не запущен.'
        Write-Hint 'Запустите Docker Desktop и повторите установку.'
        exit 1
    }
    Write-Ok 'Docker доступен'
}

if (-not (Test-DiskSpace -Path $DataDir -RequiredGb $profileDisk[$Profile])) {
    Write-Hint "Освободите место или укажите другой каталог: -DataDir D:\ASRHub"
    exit 1
}
if ($hw.RamGb -gt 0 -and $hw.RamGb -lt 8) {
    Write-Warn "Оперативной памяти $($hw.RamGb) ГБ — рекомендуется не менее 8 ГБ."
}

if (-not (Test-PortFree -Port $Port)) {
    Write-Warn "Порт $Port занят."
    $newPort = Find-FreePort -Start ($Port + 1)
    if (Confirm-Action "Использовать свободный порт $newPort?") {
        $Port = $newPort
        Write-Ok "Выбран порт $Port"
    } else {
        Write-Err "Освободите порт $Port и повторите."
        exit 1
    }
}

if (-not $Offline) {
    if (Test-Internet) { Write-Ok 'Доступ в интернет есть' }
    else {
        Write-Warn 'Нет доступа к pypi.org — переходим в автономный режим.'
        $Offline = $true
    }
}

if (-not (Confirm-Action 'Начать установку?')) { Write-Info 'Отменено.'; exit 0 }

# ---------------------------------------------------------------------------

Write-Step 'Внешние программы'

if (-not $hw.Ffmpeg) {
    Write-Warn 'Не найден ffmpeg — без него доступны только файлы WAV 16 кГц.'
    if ((Test-CommandExists 'winget') -and (Confirm-Action 'Установить ffmpeg через winget?')) {
        try {
            Invoke-Checked -Command 'winget' -Arguments @('install', '--id', 'Gyan.FFmpeg',
                '-e', '--accept-package-agreements', '--accept-source-agreements') -IgnoreExitCode
            Write-Ok 'ffmpeg установлен (может потребоваться перезапуск консоли для обновления PATH)'
        } catch { Write-Warn 'Автоматическая установка не удалась.' }
    } else {
        Write-Hint 'Установите вручную: winget install Gyan.FFmpeg  или  choco install ffmpeg'
    }
} else {
    Write-Ok 'ffmpeg найден'
}

# ---------------------------------------------------------------------------

Write-Step 'Создание каталогов'

foreach ($dir in @($Prefix, $DataDir,
                   (Join-Path $DataDir 'uploads'), (Join-Path $DataDir 'results'),
                   (Join-Path $DataDir 'models'), (Join-Path $DataDir 'logs'),
                   (Join-Path $DataDir 'tmp'))) {
    if (-not (Test-Path $dir)) {
        if (-not (Get-DryRun)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Rollback { Remove-Item -Path $dir -Recurse -Force -ErrorAction SilentlyContinue } "удалить $dir"
    }
}
Write-Ok 'Каталоги готовы'

# ---------------------------------------------------------------------------

Write-Step 'Копирование файлов приложения'

if (-not (Get-DryRun)) {
    foreach ($item in @('server', 'scripts', 'config', 'requirements', 'docker', 'VERSION', 'README.md')) {
        $source = Join-Path $RepoDir $item
        if (Test-Path $source) {
            $target = Join-Path $Prefix $item
            if (Test-Path $target) { Remove-Item $target -Recurse -Force }
            Copy-Item $source $target -Recurse -Force
        }
    }
}
Write-Ok "Файлы скопированы в $Prefix"

# ---------------------------------------------------------------------------

if ($Mode -eq 'docker') {
    Write-Step 'Сборка и запуск контейнера'
    $envFile = Join-Path $Prefix 'docker\.env'
    if (-not (Get-DryRun)) {
        @(
            "ASRHUB_PORT=$Port", "ASRHUB_HOST=$BindHost",
            "ASRHUB_DATA=$DataDir", "ASRHUB_PROFILE=$Profile",
            "ASRHUB_ENGINES=$Engines", "ASRHUB_ACCEL=$($hw.Accelerator)"
        ) | Set-Content -Path $envFile -Encoding UTF8
    }
    Push-Location (Join-Path $Prefix 'docker')
    try {
        Invoke-WithRetry -Attempts 2 -Description 'сборка образа' -Action {
            Invoke-Checked -Command 'docker' -Arguments @('compose', '--env-file', '.env', 'build')
        }
        Invoke-Checked -Command 'docker' -Arguments @('compose', '--env-file', '.env', 'up', '-d')
    } finally { Pop-Location }
    Write-Ok 'Контейнер запущен'

} else {
    Write-Step 'Виртуальное окружение Python'

    if (-not (Test-Path $venvPython)) {
        Invoke-Checked -Command $python -Arguments @('-m', 'venv', $venv) -Description 'создание venv'
        Add-Rollback { Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue } 'удалить venv'
    }
    if (-not (Get-DryRun) -and -not (Test-Path $venvPython)) {
        throw "Виртуальное окружение создано некорректно: нет $venvPython"
    }
    Write-Ok 'Окружение готово'

    $pipFlags = @('--disable-pip-version-check', '--no-input')
    if ($Offline) { $pipFlags += '--no-index' }

    Invoke-Checked -Command $venvPython -Arguments (@('-m', 'pip', 'install') + $pipFlags +
        @('--upgrade', 'pip', 'setuptools', 'wheel')) -IgnoreExitCode | Out-Null

    Write-Step 'Установка зависимостей сервера'
    Invoke-WithRetry -Attempts 3 -Description 'базовые зависимости' -Action {
        Invoke-Checked -Command $venvPip -Arguments (@('install') + $pipFlags +
            @('-r', (Join-Path $Prefix 'requirements\base.txt')))
    } | Out-Null
    Write-Ok 'Базовые зависимости установлены'

    $needsTorch = @('gigaam', 'faster_whisper', 'whisper', 'nemo', 'transformers', 'qwen3_asr') |
        Where-Object { $Engines -like "*$_*" }
    if ($needsTorch) {
        Write-Step "Установка PyTorch для ускорителя «$($hw.Accelerator)»"
        $indexUrl = Get-TorchIndexUrl -Accelerator $hw.Accelerator -CudaVersion $hw.CudaVersion
        Write-Info "Индекс пакетов: $indexUrl"
        try {
            Invoke-WithRetry -Attempts 3 -Description 'PyTorch' -Action {
                Invoke-Checked -Command $venvPip -Arguments (@('install') + $pipFlags +
                    @('--index-url', $indexUrl, 'torch', 'torchaudio'))
            } | Out-Null
        } catch {
            Write-Warn 'Установка с профильного индекса не удалась, пробуем обычный.'
            Invoke-Checked -Command $venvPip -Arguments (@('install') + $pipFlags +
                @('torch', 'torchaudio')) | Out-Null
        }
        Write-Ok 'PyTorch установлен'
    }

    Write-Step 'Установка движков распознавания'
    $failed = @()
    foreach ($engine in ($Engines -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $req = Join-Path $Prefix ("requirements\engines\" + ($engine -replace '_', '-') + '.txt')
        if (-not (Test-Path $req)) { Write-Warn "Нет файла зависимостей для «$engine»"; continue }
        Write-Info "Движок: $engine"

        if ($engine -eq 'faster_whisper' -and $hw.Accelerator -eq 'cuda') {
            $pin = Get-CTranslate2Pin -Accelerator $hw.Accelerator -CudaVersion $hw.CudaVersion
            Write-Info "Версия CTranslate2 под вашу CUDA: $pin"
            Invoke-Checked -Command $venvPip -Arguments (@('install') + $pipFlags + @($pin)) -IgnoreExitCode | Out-Null
        }
        if ($engine -eq 'tone') {
            Write-Warn 'T-one требует KenLM, который не собирается нативно под Windows.'
            Write-Hint 'Используйте WSL или режим -Mode docker.'
            $failed += $engine
            continue
        }
        try {
            Invoke-WithRetry -Attempts 2 -Description $engine -Action {
                Invoke-Checked -Command $venvPip -Arguments (@('install') + $pipFlags + @('-r', $req))
            } | Out-Null
            Write-Ok "  $engine установлен"
        } catch {
            $failed += $engine
            Write-Warn "  $engine — установка не удалась, сервер запустится без него"
        }
    }
}

# ---------------------------------------------------------------------------

Write-Step 'Создание конфигурации'

$configFile = Join-Path $DataDir 'config.yaml'
if ((Test-Path $configFile) -and -not $Force) {
    Write-Info 'Конфигурация уже существует — оставляем без изменений.'
} elseif (-not (Get-DryRun)) {
    $firstModel = ($Models -split ',')[0]
    if (-not $firstModel) { $firstModel = 'demo-simulator' }
    $concurrent = if ($hw.Accelerator -eq 'cpu') { 1 } else { 2 }
    @"
# Конфигурация ASR Hub — создана установщиком $(Get-Date -Format 'yyyy-MM-dd HH:mm')
# Полный список параметров: $DataDir\config.example.yaml
# Те же параметры доступны в веб-интерфейсе в разделе «Настройки».

data_dir: $($DataDir -replace '\\', '/')

model:
  model: $firstModel
  engine: auto
  language: ru

server:
  server_host: $BindHost
  server_port: $Port
  auth_enabled: true
  max_upload_mb: 2048
  log_level: INFO

batching:
  device: auto
  compute_type: auto

queue:
  max_concurrent_jobs: $concurrent
  scheduling_policy: priority_fifo
  result_retention_days: 30

runtime:
  models_dir: $(($DataDir + '\models') -replace '\\', '/')
  temp_dir: $(($DataDir + '\tmp') -replace '\\', '/')
"@ | Set-Content -Path $configFile -Encoding UTF8
    Add-Rollback { Remove-Item $configFile -Force -ErrorAction SilentlyContinue } 'удалить config.yaml'
    Write-Ok "Конфигурация: $configFile"
}

if ($Mode -eq 'native' -and -not (Get-DryRun)) {
    Push-Location (Join-Path $Prefix 'server')
    try {
        & $venvPython -m asrhub --print-config |
            Set-Content -Path (Join-Path $DataDir 'config.example.yaml') -Encoding UTF8
    } catch { } finally { Pop-Location }
}

# ---------------------------------------------------------------------------

if ($Models -and -not $SkipModels -and $Mode -eq 'native') {
    Write-Step 'Загрузка моделей'
    foreach ($model in ($Models -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        Write-Info "Модель: $model"
        try {
            & (Join-Path $Prefix 'scripts\models.ps1') -Action download -Model $model `
                -Prefix $Prefix -DataDir $DataDir
        } catch {
            Write-Warn "  не удалось загрузить $model"
            Write-Hint "  Повторить: powershell -File `"$Prefix\scripts\models.ps1`" -Action download -Model $model"
        }
    }
}

# ---------------------------------------------------------------------------

if (-not $NoService -and $Mode -eq 'native') {
    Write-Step 'Настройка автозапуска'
    try {
        & (Join-Path $Prefix 'scripts\service.ps1') -Action install `
            -Prefix $Prefix -DataDir $DataDir -Port $Port -BindHost $BindHost
    } catch {
        Write-Warn 'Служба не создана.'
        Write-Hint "Запускать вручную: `"$venvPython`" -m asrhub --port $Port"
    }
}

# ---------------------------------------------------------------------------

Write-Step 'Проверка установки'

if (Get-DryRun) { Write-Ok 'Пробный запуск завершён — изменений не вносилось.'; exit 0 }

$healthy = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3 -UseBasicParsing
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
    Start-Sleep -Seconds 2
}

if ($healthy) { Write-Ok "Сервер отвечает на http://127.0.0.1:$Port" }
else {
    Write-Warn 'Сервер пока не отвечает.'
    Write-Hint "Состояние службы: powershell -File `"$Prefix\scripts\service.ps1`" -Action status"
}

Clear-Rollback

$apiKeyFile = Join-Path $DataDir 'api-key.txt'
$apiKey = if (Test-Path $apiKeyFile) { (Get-Content $apiKeyFile -Raw).Trim() } else { '' }

Write-Host ''
Write-Host 'Установка завершена' -ForegroundColor Green
Write-Host ''
Write-Host "  Веб-интерфейс     http://127.0.0.1:$Port"
Write-Host "  Справочник API    http://127.0.0.1:$Port/api/reference"
if ($apiKey) { Write-Host "  Ключ доступа      $apiKey" -ForegroundColor White }
Write-Host "  Каталог программы $Prefix"
Write-Host "  Каталог данных    $DataDir"
Write-Host "  Журнал установки  $logFile"
Write-Host ''
Write-Host 'Что дальше' -ForegroundColor White
Write-Host "  Проверить окружение  powershell -File `"$Prefix\scripts\doctor.ps1`""
Write-Host "  Управлять моделями   powershell -File `"$Prefix\scripts\models.ps1`" -Action list"
Write-Host "  Управлять службой    powershell -File `"$Prefix\scripts\service.ps1`" -Action status"
Write-Host "  Обновить             powershell -File `"$Prefix\scripts\update.ps1`""
Write-Host "  Удалить              powershell -File `"$Prefix\scripts\uninstall.ps1`""
if ($failed) {
    Write-Host ''
    Write-Warn "Не установились движки: $($failed -join ', ')"
}
Write-Host ''
