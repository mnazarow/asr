<#
.SYNOPSIS
    Удаление ASR Hub с Windows.
.DESCRIPTION
    По умолчанию удаляется только программа: результаты распознавания
    и загруженные модели сохраняются. Полное удаление — ключ -Purge.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1
    powershell -ExecutionPolicy Bypass -File scripts\uninstall.ps1 -Purge -KeepModels
#>
[CmdletBinding()]
param(
    [string]$Prefix = '',
    [string]$DataDir = '',
    [switch]$Purge,
    [switch]$KeepModels,
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

if (-not $Prefix) {
    foreach ($candidate in @('C:\Program Files\ASRHub', (Join-Path $env:LOCALAPPDATA 'ASRHub'))) {
        if (Test-Path $candidate) { $Prefix = $candidate; break }
    }
}
if (-not $DataDir) {
    foreach ($candidate in @((Join-Path $env:ProgramData 'ASRHub'),
                             (Join-Path $env:LOCALAPPDATA 'ASRHub\data'))) {
        if (Test-Path $candidate) { $DataDir = $candidate; break }
    }
}
if (-not $Prefix -and -not $DataDir) {
    Write-Warn 'Установка ASR Hub не найдена.'
    Write-Hint 'Укажите пути явно: -Prefix и -DataDir'
    exit 0
}

function Get-FolderSize {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return 0 }
    try {
        return (Get-ChildItem -Path $Path -Recurse -File -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
    } catch { return 0 }
}

Write-Heading 'Что будет удалено'
if ($Prefix) { Write-Host ("  Программа   {0} ({1})" -f $Prefix, (Format-Size (Get-FolderSize $Prefix))) }
if ($Purge) {
    Write-Host ("  Данные      {0} ({1}){2}" -f $DataDir, (Format-Size (Get-FolderSize $DataDir)),
        $(if ($KeepModels) { ' — модели будут сохранены' } else { '' }))
} else {
    Write-Host "  Данные      $DataDir — сохраняются" -ForegroundColor Green
}
Write-Host '  Служба      автозапуск будет удалён'
Write-Host ''

if (-not (Confirm-Action 'Продолжить удаление?' 'n')) { Write-Info 'Отменено.'; exit 0 }

Set-StepTotal 5

Write-Step 'Резервная копия важных файлов'
$backupDir = Join-Path ([Environment]::GetFolderPath('MyDocuments')) `
    "ASRHub-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
if ($DataDir -and (Test-Path $DataDir) -and -not (Get-DryRun)) {
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    foreach ($item in @('config.yaml', 'api-key.txt', 'asrhub.db')) {
        $source = Join-Path $DataDir $item
        if (Test-Path $source) { Copy-Item $source $backupDir -Force }
    }
    if ((Get-ChildItem $backupDir -ErrorAction SilentlyContinue).Count -gt 0) {
        Write-Ok "Резервная копия: $backupDir"
    } else { Remove-Item $backupDir -Force -ErrorAction SilentlyContinue }
}

Write-Step 'Остановка службы'
try {
    & (Join-Path $PSScriptRoot 'service.ps1') -Action uninstall -Prefix $Prefix -DataDir $DataDir
} catch { Write-Info 'Служба не найдена или уже удалена.' }

Get-Process -Name python* -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Prefix) } |
    ForEach-Object {
        Write-Info "Останавливаем процесс $($_.Id)"
        if (-not (Get-DryRun)) { Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue }
    }
Write-Ok 'Служба остановлена'

Write-Step 'Удаление контейнеров'
if ((Test-CommandExists 'docker') -and (Test-Path (Join-Path $Prefix 'docker\docker-compose.yml'))) {
    Push-Location (Join-Path $Prefix 'docker')
    try { & docker compose down --remove-orphans 2>&1 | Out-Null; Write-Ok 'Контейнеры остановлены' }
    catch { Write-Info 'Контейнеры не найдены.' } finally { Pop-Location }
}

Write-Step 'Удаление файлов'
if ($Prefix -and (Test-Path $Prefix)) {
    if (Get-DryRun) { Write-Host "  [пробный запуск] Remove-Item $Prefix" }
    else {
        Remove-Item -Path $Prefix -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Удалено: $Prefix"
    }
}
if ($Purge -and $DataDir -and (Test-Path $DataDir)) {
    if ($KeepModels) {
        $modelsDir = Join-Path $DataDir 'models'
        if (Test-Path $modelsDir) {
            $keep = Join-Path ([Environment]::GetFolderPath('MyDocuments')) "ASRHub-models-$(Get-Date -Format 'yyyyMMdd')"
            if (-not (Get-DryRun)) { Move-Item $modelsDir $keep -Force; Write-Ok "Модели перенесены: $keep" }
        }
    }
    if (Get-DryRun) { Write-Host "  [пробный запуск] Remove-Item $DataDir" }
    else { Remove-Item -Path $DataDir -Recurse -Force -ErrorAction SilentlyContinue; Write-Ok "Удалено: $DataDir" }
} elseif ($DataDir) {
    Write-Info "Данные сохранены: $DataDir"
}

Write-Step 'Проверка остатков'
$leftovers = @()
foreach ($path in @([Environment]::GetEnvironmentVariable('ASRHUB_DATA_DIR', 'Machine'))) {
    if ($path) { $leftovers += "переменная окружения ASRHUB_DATA_DIR" }
}
if ($leftovers) {
    Write-Warn "Найдены остатки: $($leftovers -join ', ')"
    if (Confirm-Action 'Удалить их?') {
        [Environment]::SetEnvironmentVariable('ASRHUB_DATA_DIR', $null, 'Machine')
        Write-Ok 'Переменная окружения удалена'
    }
} else { Write-Ok 'Остатков не найдено' }

Clear-Rollback
Write-Host ''
Write-Host 'ASR Hub удалён' -ForegroundColor Green
if (Test-Path $backupDir) { Write-Host "  Резервная копия: $backupDir" }
if (-not $Purge -and $DataDir) { Write-Host "  Данные остались в $DataDir" }
Write-Host ''
