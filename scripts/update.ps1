<#
.SYNOPSIS
    Обновление ASR Hub на Windows.
.DESCRIPTION
    Создаёт снимок текущей установки (код и config.yaml) и резервную копию
    базы, обновляет файлы и зависимости, проверяет работоспособность. При
    неудачной проверке предлагает автоматический откат.

    Какая установка обновляется: заданная ключом -Prefix; иначе та, из
    которой запущен сценарий; иначе единственная известная на машине.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\update.ps1 -Prefix "C:\Program Files\ASRHub"
    powershell -ExecutionPolicy Bypass -File "C:\Program Files\ASRHub\scripts\update.ps1" -Source D:\asr-hub
    powershell -ExecutionPolicy Bypass -File "C:\Program Files\ASRHub\scripts\update.ps1" -Rollback
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
    [switch]$SkipGpuCheck,
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
$resolved = Resolve-AsrInstallation -ScriptDir $PSScriptRoot -Prefix $Prefix -DataDir $DataDir
if (-not $resolved.Prefix -and @($resolved.Candidates).Count -gt 0) {
    Show-AmbiguousInstallation $resolved.Candidates 'update.ps1'
    exit 2
}
$Prefix = $resolved.Prefix
$DataDir = $resolved.DataDir
if (-not $Prefix) { Write-Err 'Установка не найдена. Укажите -Prefix.'; exit 2 }
if (-not $DataDir) {
    # Раньше пустой каталог данных доходил до Join-Path, и обновление
    # установки с нестандартным каталогом падало на «Cannot bind argument
    # to parameter 'Path' because it is an empty string».
    Write-Err 'Каталог данных установки не найден. Укажите его: -DataDir <каталог>'
    exit 2
}
$ownAutostart = @($resolved.Autostart)
$items = @('server', 'scripts', 'config', 'requirements', 'docker', 'examples', 'VERSION', 'README.md')

# Снимок — СВОЙ у каждой установки. Прежний путь «рядом с каталогом
# программы, имя общее» совпадал у всех установок под одним родителем:
# C:\ASR\one и C:\ASR\two писали снимок в один C:\ASR\ASRHub-snapshot, и
# откат второй возвращал код первой.
$snapshotDir = "$Prefix.snapshot"
$snapshotLegacy = Join-Path (Split-Path -Parent $Prefix) 'ASRHub-snapshot'
$venvPip = Join-Path $Prefix 'venv\Scripts\pip.exe'
$venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
$configFile = Join-Path $DataDir 'config.yaml'
$currentVersion = if (Test-Path (Join-Path $Prefix 'VERSION')) { (Get-Content (Join-Path $Prefix 'VERSION') -Raw).Trim() } else { 'неизвестна' }
$newVersion = if (Test-Path (Join-Path $Source 'VERSION')) { (Get-Content (Join-Path $Source 'VERSION') -Raw).Trim() } else { 'неизвестна' }
# Контейнерная ли установка — по метке, а без неё — по docker\.env, который
# пишет только установка в контейнере. Раньше признаком был и живой
# контейнер с именем asrhub: обычная установка на машине, где кто-то
# держит такой контейнер, обновлялась пересборкой чужого образа.
#
# Наличие docker здесь не условие: без него контейнерная установка
# считалась обычной, «обновлялась» без окружения Python, а в метку
# записывался режим native — и следующий запуск, уже с docker, обновлял бы
# её как обычную. Нет docker — обновление останавливается (ниже, до первого
# изменения), как в update.sh.
$dockerMode = ($resolved.Mode -eq 'docker')

function Invoke-ServiceScript {
    param([string]$Action)
    if (-not $ownAutostart) { return }
    & (Join-Path $PSScriptRoot 'service.ps1') -Action $Action -Prefix $Prefix -DataDir $DataDir
}

function Test-ServerRunning {
    # Работала ли служба или задача до обновления: поднимать сервер,
    # которого никто не запускал, — не дело обновления.
    foreach ($record in $ownAutostart) {
        if ($record.Kind -eq 'service') {
            $service = Get-Service -Name $record.Name -ErrorAction SilentlyContinue
            if ($service -and "$($service.Status)" -eq 'Running') { return $true }
        } else {
            $task = Get-ScheduledTask -TaskName $record.Name -ErrorAction SilentlyContinue
            if ($task -and "$($task.State)" -eq 'Running') { return $true }
        }
    }
    return $false
}

function Copy-Staged {
    <#
      Каталог за каталогом через временное имя рядом: сорвётся копирование
      на середине — прежний каталог останется целым. Копия поверх оставляла
      файлы, которых в новой версии уже нет.
    #>
    param([string]$From, [string]$Item)
    $target = Join-Path $Prefix $Item
    $staged = Join-Path $Prefix ".$Item.staged"
    if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged -Recurse -Force }
    Copy-Item -LiteralPath $From -Destination $staged -Recurse -Force
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath $staged -Destination $target
}

# --- Откат ------------------------------------------------------------------

if ($Rollback) {
    Write-Heading 'Откат к предыдущей версии'
    $fromSnapshot = $snapshotDir
    if (-not (Test-Path -LiteralPath $fromSnapshot)) {
        if (Test-Path -LiteralPath $snapshotLegacy) {
            Write-Warn "Снимка по своему пути нет, но есть по прежнему общему: $snapshotLegacy"
            Write-Hint 'Он мог быть сделан другой установкой под тем же родительским каталогом —'
            Write-Hint "сверьте версию в $snapshotLegacy\VERSION."
            if (-not (Confirm-Action 'Откатиться из него?' 'n')) { exit 0 }
            $fromSnapshot = $snapshotLegacy
        } else {
            Write-Err "Снимок предыдущей версии не найден: $snapshotDir"
            exit 2
        }
    }
    $snapshotVersion = if (Test-Path (Join-Path $fromSnapshot 'VERSION')) { (Get-Content (Join-Path $fromSnapshot 'VERSION') -Raw).Trim() } else { '?' }
    Write-Info "Снимок от $((Get-Item -LiteralPath $fromSnapshot).LastWriteTime), версия $snapshotVersion"
    if (-not (Confirm-Action 'Восстановить предыдущую версию?')) { exit 0 }
    # Пробный запуск ничего не возвращает. Раньше `-Rollback -DryRun`
    # заменял server, scripts и VERSION по-настоящему.
    if (Get-DryRun) {
        foreach ($item in $items) {
            if (Test-Path -LiteralPath (Join-Path $fromSnapshot $item)) {
                Write-Host "  [пробный запуск] $(Join-Path $Prefix $item) ← снимок" -ForegroundColor Yellow
            }
        }
        if (Test-Path -LiteralPath (Join-Path $fromSnapshot 'config.yaml.snapshot')) {
            Write-Host "  [пробный запуск] $configFile ← снимок" -ForegroundColor Yellow
        }
        Write-Ok 'Пробный запуск завершён — ничего не изменено.'
        exit 0
    }
    try { Invoke-ServiceScript -Action stop } catch { Write-Info 'Служба не остановлена — возможно, уже стоит.' }
    foreach ($item in $items) {
        $snapshotItem = Join-Path $fromSnapshot $item
        if (Test-Path -LiteralPath $snapshotItem) { Copy-Staged -From $snapshotItem -Item $item }
    }
    # Конфигурация лежит не в каталоге программы, поэтому возвращается
    # отдельно. Нынешнюю не выбрасываем: в ней могли успеть поправить нужное.
    $configSnapshot = Join-Path $fromSnapshot 'config.yaml.snapshot'
    if (Test-Path -LiteralPath $configSnapshot) {
        if ((Test-Path -LiteralPath $configFile) -and
            ((Get-FileHash -LiteralPath $configSnapshot).Hash -ne (Get-FileHash -LiteralPath $configFile).Hash)) {
            $keep = "$configFile.new.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item -LiteralPath $configFile -Destination $keep -Force
            Write-Info "Конфигурация новой версии сохранена рядом: $(Split-Path -Leaf $keep)"
        }
        Copy-Item -LiteralPath $configSnapshot -Destination $configFile -Force
        Write-Ok 'Конфигурация возвращена к виду до обновления'
    }
    try { Invoke-ServiceScript -Action start } catch { Write-Warn "Службу запустить не удалось: $($_.Exception.Message)" }
    $restored = if (Test-Path (Join-Path $Prefix 'VERSION')) { (Get-Content (Join-Path $Prefix 'VERSION') -Raw).Trim() } else { '?' }
    Write-Ok "Откат выполнен: версия $restored"
    # Зависимости и база в снимок не входят.
    Write-Hint 'Пакеты окружения остались от новой версии. Вернуть прежние:'
    Write-Hint "  & `"$venvPip`" install -r `"$Prefix\requirements\base.txt`""
    $lastCopy = Get-ChildItem -LiteralPath $DataDir -Filter 'asrhub.db.bak.*' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($lastCopy) {
        Write-Hint 'База — в том виде, в каком её оставила новая версия. Копия до обновления:'
        Write-Hint "  $($lastCopy.FullName)"
    }
    exit 0
}

# --- Проверка ---------------------------------------------------------------

Write-Heading 'Проверка обновления'
Write-Host "  Установлено   $currentVersion"
Write-Host "  Доступно      $newVersion"
Write-Host "  Источник      $Source"
Write-Host "  Установка     $Prefix"
Write-Host "  Данные        $DataDir"
Write-Host ''

if (-not $EnginesOnly -and -not (Test-Path (Join-Path $Source 'server'))) {
    Write-Err "В каталоге источника нет папки server: $Source"
    Write-Hint 'Укажите путь к распакованному дистрибутиву: -Source C:\путь\asr-hub'
    exit 2
}
# Карта проверяется до снимка и до первого изменённого файла: обновление её
# не лечит, а проверка работоспособности в конце ничего не заметит — служба
# поднимется, /api/health ответит двумястами, и падать будет каждое задание.
$gpuOk = $true
if ($SkipGpuCheck) {
    Write-Info 'Проверка видеокарты пропущена (-SkipGpuCheck).'
} elseif (-not $EnginesOnly -and -not $dockerMode) {
    Write-Host ''
    $codeDir = Join-Path $Source 'server'
    if (-not (Test-Path $codeDir)) { $codeDir = Join-Path $Prefix 'server' }
    $gpuOk = Test-GpuRuntime -Python $venvPython -DataDir $DataDir -CodeDir $codeDir
}

if ($Check) {
    Write-Info 'Режим проверки — изменения не вносились.'
    # Отдельный код, а не единица: «карта недоступна» и «скрипт не отработал» —
    # разные новости, и задача в планировщике должна их различать.
    if (-not $gpuOk) { exit 3 }
    exit 0
}

if (-not $gpuOk) {
    Write-Warn 'Обновление это не лечит: карта недоступна и до, и после него.'
    if (-not (Confirm-Action 'Обновиться всё равно?' 'n')) {
        Write-Info 'Отменено. Сначала карта.'; exit 1
    }
}

if ($dockerMode -and -not (Test-CommandExists 'docker')) {
    Write-Err 'Установка в контейнере, а docker не найден.'
    Write-Hint 'Запустите Docker Desktop (или поставьте его) и повторите обновление.'
    exit 1
}

# Понижение версии — не обновление: новая схема базы прежнему коду может
# быть незнакома. Спрашиваем с умолчанием «нет».
$parsedCurrent = $null; $parsedNew = $null
if (-not $EnginesOnly -and [version]::TryParse($currentVersion, [ref]$parsedCurrent) -and
    [version]::TryParse($newVersion, [ref]$parsedNew) -and $parsedNew -lt $parsedCurrent) {
    Write-Warn "Источник старше установки: $newVersion < $currentVersion."
    Write-Hint "Вернуться к версии до обновления надёжнее откатом: powershell -File `"$Prefix\scripts\update.ps1`" -Rollback"
    if (-not (Confirm-Action 'Всё равно поставить более старую версию?' 'n')) { Write-Info 'Отменено.'; exit 0 }
}

if (-not (Confirm-Action 'Выполнить обновление?')) { Write-Info 'Отменено.'; exit 0 }

Set-StepTotal 6

Write-Step 'Снимок текущей версии'
# Повторный запуск той же версии — после неудачного обновления или ради
# зависимостей — снимок не трогает: иначе второй запуск заменял рабочий
# снимок прежней версии копией той самой, к которой откатываться незачем.
$snapshotVersionFile = Join-Path $snapshotDir 'VERSION'
$keepSnapshot = ($currentVersion -eq $newVersion) -and (Test-Path -LiteralPath $snapshotVersionFile) -and
    ((Get-Content -LiteralPath $snapshotVersionFile -Raw).Trim() -ne $currentVersion)
if ($keepSnapshot) {
    Write-Info "Версия не меняется — снимок версии $((Get-Content -LiteralPath $snapshotVersionFile -Raw).Trim()) оставлен для отката."
} elseif (Get-DryRun) {
    Write-Host "  [пробный запуск] снимок $Prefix → $snapshotDir" -ForegroundColor Yellow
} else {
    if (Test-Path -LiteralPath $snapshotDir) { Remove-Item -LiteralPath $snapshotDir -Recurse -Force }
    New-Item -ItemType Directory -Path $snapshotDir -Force | Out-Null
    foreach ($item in $items) {
        # Не «$source»: имена переменных PowerShell регистра не различают, и
        # цикл снимка затирал параметр -Source. Файлы после этого копировались
        # из «<установка>\README.md\server» — то есть ниоткуда, а сценарий
        # печатал «Файлы обновлены»: обновление на Windows не меняло кода вовсе.
        $installedItem = Join-Path $Prefix $item
        if (Test-Path -LiteralPath $installedItem) {
            Copy-Item -LiteralPath $installedItem -Destination $snapshotDir -Recurse -Force
        }
    }
    # Конфигурация лежит в каталоге данных и обновлением не трогается, но
    # откат без неё неполон: новая версия могла записать в файл параметры,
    # которых прежняя не знает. Имя с суффиксом — чтобы откат, копирующий
    # снимок в каталог программы, не оставил там лишний config.yaml.
    if (Test-Path -LiteralPath $configFile) {
        Copy-Item -LiteralPath $configFile -Destination (Join-Path $snapshotDir 'config.yaml.snapshot') -Force
    }
    # В снимке — config.yaml с ключами и токеном: закрываем от остальных.
    $null = Set-AsrPrivateAcl -Path $snapshotDir
    Write-Ok "Снимок: $snapshotDir"
    Write-Hint "Откат при проблемах: powershell -File `"$Prefix\scripts\update.ps1`" -Rollback"
}

Write-Step 'Остановка службы'
$wasRunning = $false
if ($dockerMode) {
    Write-Info 'Установка в контейнере: остановка произойдёт при пересборке.'
    $wasRunning = $true
} elseif (-not $ownAutostart) {
    Write-Info 'Автозапуска у этой установки нет — сервер, запущенный вручную, перезапустите сами.'
} else {
    $wasRunning = Test-ServerRunning
    try { Invoke-ServiceScript -Action stop } catch { Write-Info 'Служба не запущена.' }
    Write-Ok 'Служба остановлена'
}

# Копия базы — после остановки и через резервное копирование SQLite: база
# работает в режиме WAL, и копия файла работающей базы теряла то, что лежит
# только в журнале.
Write-Step 'Резервная копия базы'
$dbFile = Join-Path $DataDir 'asrhub.db'
if ((Test-Path -LiteralPath $dbFile) -and -not (Get-DryRun)) {
    $dbBackup = "$dbFile.bak.$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    if (Backup-AsrDatabase -Source $dbFile -Destination $dbBackup -Python $venvPython) {
        Write-Ok "Копия базы: $(Split-Path -Leaf $dbBackup) ($(Format-Size (Get-Item -LiteralPath $dbBackup).Length))"
        Get-ChildItem -LiteralPath $DataDir -Filter 'asrhub.db.bak.*' |
            Sort-Object LastWriteTime -Descending | Select-Object -Skip 5 |
            Remove-Item -Force -ErrorAction SilentlyContinue
    } else {
        Write-Err 'Копию базы сделать не удалось — обновление остановлено, файлы не тронуты.'
        if ($wasRunning -and -not $dockerMode) {
            try { Invoke-ServiceScript -Action start } catch { }
        }
        exit 1
    }
} elseif ((Get-DryRun) -and (Test-Path -LiteralPath $dbFile)) {
    Write-Host "  [пробный запуск] копия базы $dbFile" -ForegroundColor Yellow
}

if (-not $EnginesOnly) {
    Write-Step 'Обновление файлов'
    # Источник по умолчанию — каталог рядом со скриптом, а скрипт запускают
    # из самой установки: именно эту команду печатает установщик. Без
    # проверки цикл удалял каталог и копировал его сам в себя, оставляя
    # установку без server.
    $srcReal = [System.IO.Path]::GetFullPath($Source)
    $dstReal = [System.IO.Path]::GetFullPath($Prefix)
    if ($srcReal -eq $dstReal) {
        Write-Info 'Источник совпадает с установкой — файлы уже на месте.'
        Write-Hint 'Чтобы обновиться из другого места: update.ps1 -Source C:\путь\к\новой\версии'
    } elseif (-not (Get-DryRun)) {
        foreach ($item in $items) {
            $sourcePath = Join-Path $Source $item
            if (Test-Path -LiteralPath $sourcePath) { Copy-Staged -From $sourcePath -Item $item }
        }
    }
    Write-Ok "Файлы обновлены до версии $newVersion"
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
        # Установлен ли движок — по его собственному пакету, как в update.sh.
        # Раньше годилась любая строка файла: whisperx считался установленным
        # из-за pyannote.audio, который стоит ради диаризации, и обновление
        # ставило движок, которого никто не выбирал. А GigaAM, наоборот,
        # узнавался по чужому hydra-core — его собственный пакет лежит только
        # в no-deps\gigaam.txt, и этот файл здесь не читался вовсе.
        if (Test-EngineInstalled -Pip $venvPip -Requirements $req.FullName) {
            Write-Info "Движок $($req.BaseName) установлен — обновляем"
            try {
                # Не голый «pip install -r»: без спутников no-deps и optional
                # сам GigaAM не обновлялся никогда, а необязательная часть
                # движка — тоже. Та же функция, что и у установщика.
                Install-EngineRequirements -Pip $venvPip -Requirements $req.FullName `
                    -PipFlags @('--upgrade', '--disable-pip-version-check')
            } catch { Write-Warn "  $($req.BaseName): обновление не удалось, остаётся прежняя версия" }
        }
    }
    # Движки перетянули общие пакеты под свои пины — возвращаем заданное нами,
    # как это делает update.sh. Здесь этого не было вовсе, и Windows после
    # обновления оставалась с версиями, которые на Linux считаются негодными.
    Install-Overrides -Pip $venvPip -RequirementsDir (Join-Path $Prefix 'requirements')
    # Пакеты, которые ставили прежние версии и которые больше не нужны.
    Remove-RetiredPackages -Pip $venvPip -RequirementsDir (Join-Path $Prefix 'requirements')
    Write-Ok 'Зависимости обновлены'
} else { Write-Warn 'Виртуальное окружение не найдено.' }

# Спрашиваем новую версию о прежнем файле ДО перезапуска. Сервер из-за
# несогласия не падает — берёт умолчание, — но узнать об этом лучше здесь,
# одной строкой, чем через неделю по формуле «настройка не действует».
if (-not $dockerMode -and (Test-Path $venvPython) -and (Test-Path -LiteralPath $configFile) -and -not (Get-DryRun)) {
    Push-Location -LiteralPath (Join-Path $Prefix 'server')
    try {
        $checkOutput = @(Invoke-Native -Command $venvPython -Arguments @('-m', 'asrhub', '--config', $configFile, '--check-config') -MergeError)
    } finally { Pop-Location }
    if (($checkOutput -join "`n") -match 'Не принято значений') {
        Write-Warn 'Часть значений конфигурации новая версия не приняла:'
        $checkOutput | ForEach-Object { Write-Host "  $_" }
        Write-Hint 'Сервер запустится с умолчаниями вместо них; поправьте файл и перезапустите.'
    }
}

$mode = if ($dockerMode) { 'docker' } else { 'native' }
$markerService = if ($ownAutostart) { $ownAutostart[0].Name } else { '' }

Write-Step 'Запуск и проверка'
if ($NoRestart) {
    Write-Info 'Перезапуск пропущен (-NoRestart).'
    $null = Write-InstallMarker -Prefix $Prefix -DataDir $DataDir -Service $markerService -Mode $mode
    exit 0
}
if (Get-DryRun) { Write-Ok 'Пробный запуск завершён.'; exit 0 }

if ($dockerMode) {
    # Без пересборки образа новый код в контейнер не попадает: проверка
    # живости отвечала бы 200 от старой версии, и обновление объявлялось
    # успешным, ничего не изменив.
    $compose = Get-AsrCompose -Prefix $Prefix
    Push-Location -LiteralPath $compose.Directory
    try {
        Write-Info 'Пересборка образа (может занять несколько минут)…'
        Invoke-Checked -Command $compose.Command -Arguments ($compose.Arguments + @('build')) | Out-Null
        Invoke-Checked -Command $compose.Command -Arguments ($compose.Arguments + @('up', '-d')) | Out-Null
    } catch {
        Write-Err 'Пересборка образа не удалась. Прежний контейнер не тронут.'
        exit 1
    } finally { Pop-Location }
} elseif ($wasRunning) {
    try { Invoke-ServiceScript -Action start } catch { Write-Warn "Службу запустить не удалось: $($_.Exception.Message)" }
} else {
    Write-Info 'До обновления сервер не работал — не запускаем.'
    $null = Write-InstallMarker -Prefix $Prefix -DataDir $DataDir -Service $markerService -Mode $mode
    Write-Host ''
    Write-Host "Обновление завершено: $currentVersion → $newVersion" -ForegroundColor Green
    if ($ownAutostart) { Write-Hint "Запустить: powershell -File `"$Prefix\scripts\service.ps1`" -Action start" }
    exit 0
}

# Порт — из строки запуска автозапуска, адрес — тот, к которому привязан
# сервер: `--port` службы перекрывает config.yaml, а сервер, привязанный к
# адресу в локальной сети, на 127.0.0.1 не отвечает вовсе. Раньше проверка
# шла в порт из файла и всегда на 127.0.0.1.
$address = Get-AsrServerAddress -Prefix $Prefix -DataDir $DataDir -Mode $mode -Autostart $ownAutostart
$probe = Wait-AsrHealth -Port $address.Port -HostName $address.ProbeHost -Seconds 60

if ($probe.Status -ge 200 -and $probe.Status -lt 300) {
    Write-Ok 'Сервер отвечает — обновление успешно'
    # Карту спрашиваем ещё раз, уже новым кодом: обновление могло подменить
    # torch, и «до» с «после» расходятся в обе стороны.
    if (-not $SkipGpuCheck -and -not $EnginesOnly -and -not $dockerMode) {
        $null = Test-GpuRuntime -Python $venvPython -DataDir $DataDir -CodeDir (Join-Path $Prefix 'server')
    }
    # Метка — после удачного обновления: установка, поставленная прежними
    # версиями, получает её здесь, и дальше сценарии узнают её сами.
    $null = Write-InstallMarker -Prefix $Prefix -DataDir $DataDir -Service $markerService -Mode $mode
    Write-Host ''
    Write-Host "Обновление завершено: $currentVersion → $newVersion" -ForegroundColor Green
    Write-Hint "Откат при необходимости: powershell -File `"$Prefix\scripts\update.ps1`" -Rollback"
} elseif ($probe.Status -gt 0) {
    # Сервер поднялся, но сам сообщает о неисправности. Это не повод для
    # отката: код обновился и работает, сломано что-то под ним — чаще всего
    # база или диск, и откат этого не лечит. Раньше любой ответ кроме 200
    # считался неудачей, и с -Yes обновление откатывалось само.
    Write-Warn "Сервер отвечает, но сообщает о неисправности (код $($probe.Status))."
    ($probe.Body -split "`n") | Select-Object -First 20 | ForEach-Object { Write-Host "  $_" }
    Write-Hint "Подробности: powershell -File `"$Prefix\scripts\doctor.ps1`""
    Write-Hint "Журнал службы: powershell -File `"$Prefix\scripts\service.ps1`" -Action logs -Lines 100"
    exit 1
} else {
    Write-Err "Сервер не отвечает после обновления (http://$($address.ProbeHost):$($address.Port))."
    Write-Hint "Журнал службы: powershell -File `"$Prefix\scripts\service.ps1`" -Action logs -Lines 100"
    if (Confirm-Action 'Откатиться к предыдущей версии?') {
        & $PSCommandPath -Rollback -Prefix $Prefix -DataDir $DataDir -Yes
        exit 1
    }
    Write-Hint "Откатиться позже: powershell -File `"$Prefix\scripts\update.ps1`" -Rollback"
    exit 1
}
