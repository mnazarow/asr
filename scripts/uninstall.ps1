<#
.SYNOPSIS
    Удаление ASR Hub с Windows.
.DESCRIPTION
    По умолчанию удаляется только программа: результаты распознавания
    и загруженные модели сохраняются. Полное удаление — ключ -Purge.

    Какая установка удаляется: заданная ключом -Prefix; иначе та, из
    которой запущен сценарий (её каталог данных и автозапуск берутся из
    метки установки); иначе единственная известная на машине. Установок
    несколько, а сценарий запущен не из них, — удаления нет, пока не
    сказано, какую.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File "C:\Program Files\ASRHub\scripts\uninstall.ps1"
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

# Раньше установка искалась в двух стандартных каталогах, а команда,
# которую печатает установщик, ключа -Prefix не несла. Для установки в свой
# каталог это кончалось пустым $Prefix — и остановкой ВСЕХ процессов
# python*.exe на машине (`StartsWith('')` истинно для любого пути).
$resolved = Resolve-AsrInstallation -ScriptDir $PSScriptRoot -Prefix $Prefix -DataDir $DataDir
if (-not $resolved.Prefix -and @($resolved.Candidates).Count -gt 0) {
    Show-AmbiguousInstallation $resolved.Candidates 'uninstall.ps1'
    exit 2
}
$Prefix = $resolved.Prefix
$DataDir = $resolved.DataDir
$ownAutostart = @($resolved.Autostart)
$foreignAutostart = @(Get-AsrAutostart | Where-Object {
    $_.Prefix -and -not ($Prefix -and (Test-AsrSamePath $_.Prefix $Prefix))
})
if (-not $Prefix -and -not $DataDir) {
    Write-Warn 'Установка ASR Hub не найдена.'
    Write-Hint 'Укажите пути явно: -Prefix и -DataDir'
    exit 0
}

function Get-FolderSize {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) { return 0 }
    try {
        $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
                Measure-Object -Property Length -Sum).Sum
        if ($sum) { return [long]$sum } else { return 0 }
    } catch { return 0 }
}

Write-Heading 'Что будет удалено'
if ($Prefix) { Write-Host ("  Программа   {0} ({1})" -f $Prefix, (Format-Size (Get-FolderSize $Prefix))) }
else { Write-Host '  Программа   не найдена — удаляются только данные' }
if ($Purge) {
    Write-Host ("  Данные      {0} ({1}){2}" -f $DataDir, (Format-Size (Get-FolderSize $DataDir)),
        $(if ($KeepModels) { ' — модели будут сохранены' } else { '' }))
} else {
    Write-Host "  Данные      $DataDir — сохраняются" -ForegroundColor Green
}
if ($ownAutostart) {
    $names = @($ownAutostart | ForEach-Object {
        if ($_.Kind -eq 'service') { "служба «$($_.Name)»" } else { "задача «$($_.Name)»" } })
    Write-Host "  Автозапуск  $($names -join ', ') — будет удалён"
} else {
    Write-Host '  Автозапуск  у этой установки не настроен'
}
foreach ($record in $foreignAutostart) {
    Write-Host "  Автозапуск другой установки ($($record.Prefix)) не трогается" -ForegroundColor DarkGray
}
Write-Host ''

# Каталог данных, который не подтвердили ни метка, ни автозапуск, — всего
# лишь стандартный путь. Он может быть чужим: вторая установка в свой
# каталог, удаляемая с -Purge, снесла бы базу и модели первой.
if ($Purge -and $DataDir -and $resolved.DataDirSource -eq 'guess') {
    Write-Warn "Каталог данных $DataDir найден по стандартному пути — ни метка установки, ни её автозапуск его не подтверждают."
    Write-Hint 'Он может принадлежать другой установке. Укажите его явно: -DataDir <каталог>'
    if (Get-AssumeYes) {
        Write-Err 'С ключом -Yes каталог данных наугад не удаляется.'
        exit 2
    }
    if (-not (Confirm-Action "Удалить $DataDir вместе с базой и моделями?" 'n')) {
        Write-Info 'Отменено.'
        exit 0
    }
}

if (-not (Confirm-Action 'Продолжить удаление?' 'n')) { Write-Info 'Отменено.'; exit 0 }

Set-StepTotal 5

# Сначала остановка, потом копия: база работает в режиме WAL, и копия,
# снятая с работающего сервера, могла не увидеть последних записей.
Write-Step 'Остановка службы'
if ($ownAutostart -and $Prefix) {
    try {
        & (Join-Path $PSScriptRoot 'service.ps1') -Action uninstall -Prefix $Prefix -DataDir $DataDir
    } catch { Write-Info "Автозапуск не удалён: $($_.Exception.Message)" }
} elseif ($ownAutostart) {
    # Каталог программы уже удалён руками, а автозапуск остался.
    try {
        & (Join-Path $PSScriptRoot 'service.ps1') -Action uninstall -DataDir $DataDir
    } catch { Write-Info "Автозапуск не удалён: $($_.Exception.Message)" }
} else {
    Write-Info 'Автозапуска у этой установки нет — службы и задачи не трогаем.'
}

# Процессы — только запущенные из этого каталога программы. Без каталога
# программы процессы не трогаем вовсе: пустой путь прежде совпадал с любым.
if ($Prefix) {
    foreach ($process in @(Get-Process -Name python* -ErrorAction SilentlyContinue)) {
        $path = ''
        try { $path = "$($process.Path)" } catch { $path = '' }
        if (-not (Test-AsrPathInside -Path $path -Parent $Prefix)) { continue }
        Write-Info "Останавливаем процесс $($process.Id) ($path)"
        if (Get-DryRun) { Write-Host "  [пробный запуск] Stop-Process $($process.Id)" -ForegroundColor Yellow }
        else { Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue }
    }
}
Write-Ok 'Сервер этой установки остановлен'

Write-Step 'Резервная копия важных файлов'
# «Документы» бывают пустым путём (учётная запись без профиля, запуск вне
# Windows) — тогда Join-Path падал на пустой строке посреди удаления.
$documents = [Environment]::GetFolderPath('MyDocuments')
if (-not $documents) { $documents = if ($HOME) { $HOME } else { [System.IO.Path]::GetTempPath() } }
$backupDir = Join-Path $documents "ASRHub-backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
$backupOk = $true
if ($DataDir -and (Test-Path -LiteralPath $DataDir)) {
    $items = @('config.yaml', 'api-key.txt') | Where-Object { Test-Path -LiteralPath (Join-Path $DataDir $_) }
    $database = Join-Path $DataDir 'asrhub.db'
    if (Get-DryRun) {
        foreach ($item in $items) { Write-Host "  [пробный запуск] копия $item → $backupDir" -ForegroundColor Yellow }
        if (Test-Path -LiteralPath $database) {
            Write-Host "  [пробный запуск] копия базы $database → $backupDir" -ForegroundColor Yellow
        }
    } elseif ($items -or (Test-Path -LiteralPath $database)) {
        New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
        foreach ($item in $items) { Copy-Item -LiteralPath (Join-Path $DataDir $item) -Destination $backupDir -Force }
        if (Test-Path -LiteralPath $database) {
            # Через резервное копирование SQLite, а не Copy-Item: свежие
            # записи живут в asrhub.db-wal, и копия одного файла их теряла.
            $venvPython = if ($Prefix) { Join-Path $Prefix 'venv\Scripts\python.exe' } else { '' }
            if (Backup-AsrDatabase -Source $database -Destination (Join-Path $backupDir 'asrhub.db') -Python $venvPython) {
                Write-Ok 'База скопирована'
            } else {
                $backupOk = $false
                Write-Warn 'Копию базы снять не удалось.'
            }
        }
        Write-Ok "Резервная копия: $backupDir"
    }
}
if (-not $backupOk -and $Purge) {
    if (-not (Confirm-Action 'Копии базы нет. Всё равно удалить данные?' 'n')) {
        Write-Info 'Удаление остановлено: база на месте.'
        exit 1
    }
}

Write-Step 'Удаление контейнеров'
# Только у установки в контейнере. Раньше `docker compose down` звали у
# любой установки, где есть Docker и docker-compose.yml, — и в пробном
# запуске тоже: `-DryRun` останавливал и удалял контейнеры по-настоящему.
if (-not $Prefix -or $resolved.Mode -ne 'docker') {
    Write-Info 'Установка не в контейнере — пропускаем.'
} elseif (-not (Test-CommandExists 'docker')) {
    # Раньше здесь печаталось «Установка не в контейнере» — и человек не
    # узнавал, что контейнеры продолжают работать и держать порт.
    Write-Warn 'Установка в контейнере, а docker не найден — контейнеры не остановлены.'
    Write-Hint 'Когда Docker будет доступен: docker rm -f asrhub asrhub-proxy'
} elseif (-not (Test-Path -LiteralPath (Join-Path $Prefix 'docker\docker-compose.yml'))) {
    Write-Warn 'Нет docker\docker-compose.yml — контейнеры не остановлены.'
    Write-Hint 'Остановите их сами: docker rm -f asrhub asrhub-proxy'
} else {
    $compose = Get-AsrCompose -Prefix $Prefix
    $arguments = $compose.Arguments + @('down', '--remove-orphans')
    if (Get-DryRun) {
        Write-Host "  [пробный запуск] $($compose.Command) $($arguments -join ' ')" -ForegroundColor Yellow
    } else {
        Push-Location -LiteralPath $compose.Directory
        try {
            # Invoke-Native: docker пишет ход работы в stderr, и на Windows
            # PowerShell 5.1 первая же строка обрывала удаление с ложным
            # «Контейнеры не найдены».
            $output = @(Invoke-Native -Command $compose.Command -Arguments $arguments -MergeError)
            if ($LASTEXITCODE -eq 0) { Write-Ok 'Контейнеры остановлены и удалены' }
            else {
                Write-Warn "docker compose down завершился с кодом $LASTEXITCODE"
                $output | Select-Object -Last 5 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
            }
        } finally { Pop-Location }
        # Образ весит несколько гигабайт и оставался после «полного»
        # удаления, а uninstall.sh удалить его предлагает давно. Тегов
        # может быть несколько: latest — сборка для процессора, cuda — под
        # видеокарту. Образ, которым пользуется контейнер другой установки,
        # docker сам не отдаст: удаление без -f на нём остановится.
        $images = @(Invoke-Native -Command 'docker' -Arguments @('image', 'ls', '--format', '{{.Repository}}:{{.Tag}}') |
            Where-Object { "$_" -like 'asrhub:*' })
        if ($images.Count -gt 0 -and (Confirm-Action "Удалить образ ($($images -join ', '))?" 'n')) {
            foreach ($image in $images) {
                $null = Invoke-Native -Command 'docker' -Arguments @('image', 'rm', $image)
                if ($LASTEXITCODE -eq 0) { Write-Ok "Образ $image удалён" }
                else { Write-Warn "Образ $image не удалён — им пользуется другой контейнер?" }
            }
        }
    }
}

Write-Step 'Удаление файлов'

# Модели спасаем до всякого удаления: при установке без прав администратора
# каталог данных лежит ВНУТРИ каталога программы (%LOCALAPPDATA%\ASRHub\data),
# и снос prefix уносил их вместе с собой.
$keptModels = ''
if ($Purge -and $KeepModels -and $DataDir) {
    $modelsDir = Join-Path $DataDir 'models'
    if (Test-Path -LiteralPath $modelsDir) {
        $keptModels = Join-Path $documents "ASRHub-models-$(Get-Date -Format 'yyyyMMdd')"
        if (Get-DryRun) { Write-Host "  [пробный запуск] Move-Item $modelsDir $keptModels" -ForegroundColor Yellow }
        else {
            try {
                Move-Item -LiteralPath $modelsDir -Destination $keptModels -Force -ErrorAction Stop
                Write-Ok "Модели перенесены: $keptModels"
            } catch {
                Write-Err "Не удалось перенести модели: $_"
                Write-Err 'Удаление остановлено, чтобы не потерять веса.'
                exit 1
            }
        }
    }
}

# Каталог данных внутри каталога программы — обычная раскладка на Windows и
# macOS. Снести prefix целиком означало бы удалить модели, базу и
# результаты, о сохранности которых скрипт тут же отчитывается. В bash-версии
# такая защита была, в этой — нет: обычный запуск без -Purge удалял всё и
# писал «Данные сохранены».
$dataInsidePrefix = $false
if ($Prefix -and $DataDir) {
    $dataInsidePrefix = Test-AsrPathInside -Path $DataDir -Parent $Prefix
}

if ($Prefix -and (Test-Path -LiteralPath $Prefix)) {
    if (-not $Purge -and $dataInsidePrefix) {
        Write-Info 'Каталог данных находится внутри каталога программы — удаляем выборочно.'
        foreach ($sub in @('server', 'scripts', 'config', 'requirements', 'docker', 'examples',
                           'venv', 'VERSION', 'README.md', '.asrhub-install')) {
            $target = Join-Path $Prefix $sub
            if (-not (Test-Path -LiteralPath $target)) { continue }
            if (Get-DryRun) { Write-Host "  [пробный запуск] Remove-Item $target" -ForegroundColor Yellow }
            else { Remove-Item -LiteralPath $target -Recurse -Force -ErrorAction SilentlyContinue }
        }
        if (-not (Get-DryRun)) { Write-Ok "Программа удалена, данные оставлены: $DataDir" }
    }
    elseif (Get-DryRun) { Write-Host "  [пробный запуск] Remove-Item $Prefix" -ForegroundColor Yellow }
    else {
        Remove-Item -LiteralPath $Prefix -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "Удалено: $Prefix"
    }
}

if ($Purge -and $DataDir -and (Test-Path -LiteralPath $DataDir)) {
    if (Get-DryRun) { Write-Host "  [пробный запуск] Remove-Item $DataDir" -ForegroundColor Yellow }
    else { Remove-Item -LiteralPath $DataDir -Recurse -Force -ErrorAction SilentlyContinue; Write-Ok "Удалено: $DataDir" }
    if ($keptModels) { Write-Info "Модели: $keptModels" }
} elseif ($Purge) {
    if ($keptModels) { Write-Info "Модели: $keptModels" }
} elseif ($DataDir -and (Test-Path -LiteralPath $DataDir)) {
    Write-Info "Данные сохранены: $DataDir"
} elseif ($DataDir) {
    Write-Info "Каталог данных $DataDir не найден."
}

Write-Step 'Проверка остатков'
$leftovers = @()
# Снимок перед обновлением: свой у каждой установки и прежний общий. В
# своём лежит config.yaml.snapshot — с ключами доступа и токеном.
if ($Prefix) {
    foreach ($candidate in @("$Prefix.snapshot", (Join-Path (Split-Path -Parent $Prefix) 'ASRHub-snapshot'))) {
        if (Test-Path -LiteralPath $candidate) { $leftovers += $candidate }
    }
}
# Переменная машины — только если она указывает на ЭТОТ каталог данных:
# чужую установку она бы оставила без данных.
$machineData = [Environment]::GetEnvironmentVariable('ASRHUB_DATA_DIR', 'Machine')
$dropVariable = [bool]($machineData -and $DataDir -and (Test-AsrSamePath $machineData $DataDir))
# Правило брандмауэра, которое завёл установщик этой установки.
$firewallRules = @()
if ($Prefix -and (Get-Command Get-NetFirewallRule -ErrorAction SilentlyContinue)) {
    $firewallRules = @(Get-NetFirewallRule -DisplayName 'ASR Hub*' -ErrorAction SilentlyContinue |
        Where-Object { "$($_.Description)" -eq "ASR Hub: $Prefix" })
}
if ($leftovers -or $dropVariable -or $firewallRules) {
    Write-Warn 'Найдены остатки:'
    foreach ($path in $leftovers) { Write-Host "    $path" }
    if ($dropVariable) { Write-Host "    переменная окружения ASRHUB_DATA_DIR=$machineData" }
    foreach ($rule in $firewallRules) { Write-Host "    правило брандмауэра «$($rule.DisplayName)»" }
    if (Get-DryRun) {
        Write-Info 'Пробный запуск: остатки не удаляются.'
    } elseif (Confirm-Action 'Удалить их?') {
        foreach ($path in $leftovers) {
            try { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction Stop }
            catch { Write-Warn "  не удалось: $path" }
        }
        if ($dropVariable) {
            try { [Environment]::SetEnvironmentVariable('ASRHUB_DATA_DIR', $null, 'Machine') }
            catch { Write-Warn '  не удалось убрать переменную окружения — нужны права администратора' }
        }
        foreach ($rule in $firewallRules) {
            try { $rule | Remove-NetFirewallRule -ErrorAction Stop }
            catch { Write-Warn "  не удалось удалить правило «$($rule.DisplayName)»" }
        }
        Write-Ok 'Остатки удалены'
    }
} else { Write-Ok 'Остатков не найдено' }

Clear-Rollback
Write-Host ''
if (Get-DryRun) {
    Write-Host 'Пробный запуск завершён — ничего не удалено.' -ForegroundColor Green
    Write-Host ''
    exit 0
}
Write-Host 'ASR Hub удалён' -ForegroundColor Green
if (Test-Path -LiteralPath $backupDir) { Write-Host "  Резервная копия: $backupDir" }
if (-not $Purge -and $DataDir) { Write-Host "  Данные остались в $DataDir" }
Write-Host ''
