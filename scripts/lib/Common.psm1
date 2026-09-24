<#
.SYNOPSIS
    Общая библиотека скриптов ASR Hub для Windows.
.DESCRIPTION
    Журналирование, обработка ошибок с откатом, повторы, проверки окружения,
    определение оборудования. Подключается так:

        Import-Module "$PSScriptRoot\lib\Common.psm1" -Force
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$PSDefaultParameterValues['*:Encoding'] = 'utf8'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# Версия — из файла VERSION рядом с деревом скриптов; вписанное число
# расходится с файлом при первом же выпуске.
$script:AsrHubVersion = (Get-Content -Raw -ErrorAction SilentlyContinue `
    (Join-Path $PSScriptRoot '..\..\VERSION'))
if ([string]::IsNullOrWhiteSpace($script:AsrHubVersion)) {
    $script:AsrHubVersion = '3.1.15'
} else {
    $script:AsrHubVersion = $script:AsrHubVersion.Trim()
}
$script:MinPython       = [version]'3.10'
$script:LogFile         = $null
$script:RollbackActions = [System.Collections.ArrayList]::new()
$script:TempPaths       = [System.Collections.ArrayList]::new()
$script:StepIndex       = 0
$script:StepTotal       = 0
$script:CurrentStep     = ''
$script:DryRun          = $false
$script:AssumeYes       = $false
$script:Quiet           = $false

# ---------------------------------------------------------------------------
# Журналирование
# ---------------------------------------------------------------------------

function Initialize-AsrLog {
    param([string]$Directory = $env:TEMP)
    if (-not (Test-Path $Directory)) { New-Item -ItemType Directory -Path $Directory -Force | Out-Null }
    $name = "asrhub-$(Get-Date -Format 'yyyyMMdd-HHmmss').log"
    $script:LogFile = Join-Path $Directory $name
    "ASR Hub $script:AsrHubVersion — журнал от $(Get-Date)" | Set-Content -Path $script:LogFile
    return $script:LogFile
}

function Write-AsrLog {
    param([string]$Level, [string]$Message)
    if ($script:LogFile) {
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Level] $Message" |
            Add-Content -Path $script:LogFile -ErrorAction SilentlyContinue
    }
}

function Write-Info    { param([string]$m) if (-not $script:Quiet) { Write-Host "— $m" -ForegroundColor Cyan };   Write-AsrLog INFO $m }
function Write-Ok      { param([string]$m) if (-not $script:Quiet) { Write-Host "✓ $m" -ForegroundColor Green };  Write-AsrLog OK $m }
function Write-Warn    { param([string]$m) Write-Host "! $m" -ForegroundColor Yellow; Write-AsrLog WARN $m }
function Write-Err     { param([string]$m) Write-Host "✕ $m" -ForegroundColor Red;    Write-AsrLog ERROR $m }
function Write-Hint    { param([string]$m) Write-Host "  $m" -ForegroundColor DarkGray; Write-AsrLog HINT $m }
function Write-Debug2  { param([string]$m) if ($env:ASRHUB_DEBUG -eq '1') { Write-Host "· $m" -ForegroundColor DarkGray }; Write-AsrLog DEBUG $m }

function Write-Heading {
    param([string]$Text)
    if ($script:Quiet) { return }
    Write-Host ''
    Write-Host $Text -ForegroundColor White
    Write-Host ('─' * $Text.Length) -ForegroundColor DarkGray
    Write-AsrLog STEP $Text
}

function Write-Step {
    param([string]$Text)
    $script:StepIndex++
    $script:CurrentStep = $Text
    if ($script:Quiet) { return }
    Write-Host ''
    if ($script:StepTotal -gt 0) {
        Write-Host "[$($script:StepIndex)/$($script:StepTotal)] $Text" -ForegroundColor Blue
    } else {
        Write-Host "▸ $Text" -ForegroundColor Blue
    }
    Write-AsrLog STEP $Text
}

function Set-StepTotal { param([int]$Total) $script:StepTotal = $Total; $script:StepIndex = 0 }

function Show-Banner {
    if ($script:Quiet) { return }
    Write-Host @'
   _   ___ ___   _  _      _
  /_\ / __| _ \ | || |_  _| |__
 / _ \\__ \   / | __ | || | '_ \
/_/ \_\___/_|_\ |_||_|\_,_|_.__/
'@ -ForegroundColor Blue
    Write-Host "Сервер распознавания речи · версия $script:AsrHubVersion`n" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
# Откат и очистка
# ---------------------------------------------------------------------------

function Add-Rollback {
    param([scriptblock]$Action, [string]$Description = '')
    [void]$script:RollbackActions.Add(@{ Action = $Action; Description = $Description })
    Write-Debug2 "откат зарегистрирован: $Description"
}

function Invoke-Rollback {
    if ($script:RollbackActions.Count -eq 0) { return }
    Write-Warn "Откат изменений ($($script:RollbackActions.Count) действ.)…"
    for ($i = $script:RollbackActions.Count - 1; $i -ge 0; $i--) {
        $item = $script:RollbackActions[$i]
        try { & $item.Action } catch { Write-Warn "  не удалось: $($item.Description)" }
    }
    $script:RollbackActions.Clear()
    Write-Ok 'Откат завершён — система возвращена в исходное состояние.'
}

function Clear-Rollback { $script:RollbackActions.Clear() }

function Invoke-AsrFailure {
    param([System.Management.Automation.ErrorRecord]$ErrorRecord)
    Write-Host ''
    Write-Err "Сбой на шаге: $($script:CurrentStep)"
    Write-Err $ErrorRecord.Exception.Message
    if ($env:ASRHUB_DEBUG -eq '1') { Write-Host $ErrorRecord.ScriptStackTrace -ForegroundColor DarkGray }
    Write-AsrLog ERROR $ErrorRecord.Exception.ToString()

    $text = $ErrorRecord.Exception.Message
    if ($text -match 'Access.*denied|отказано в доступе') {
        Write-Hint 'Запустите PowerShell от имени администратора.'
    } elseif ($text -match 'not recognized|не является внутренней') {
        Write-Hint 'Не найдена нужная программа. Установите её или добавьте в PATH.'
    } elseif ($text -match 'space|места на диске') {
        Write-Hint 'Не хватает места на диске.'
    } elseif ($text -match 'Unable to connect|соединение') {
        Write-Hint 'Проблема с сетью. Проверьте доступ в интернет и настройки прокси.'
    }
    Invoke-Rollback
    Write-Hint "Полный журнал: $script:LogFile"
    Write-Hint 'Диагностика: powershell -ExecutionPolicy Bypass -File scripts\doctor.ps1'
    exit 1
}

# ---------------------------------------------------------------------------
# Выполнение команд
# ---------------------------------------------------------------------------

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Command,
        [string[]]$Arguments = @(),
        [switch]$IgnoreExitCode,
        [string]$Description = ''
    )
    $display = "$Command $($Arguments -join ' ')"
    if ($script:DryRun) { Write-Host "[пробный запуск] $display" -ForegroundColor Yellow; return '' }
    Write-Debug2 "выполняется: $display"
    Write-AsrLog CMD $display
    # В Windows PowerShell 5.1 слияние потоков от внешней программы при
    # $ErrorActionPreference = 'Stop' превращает КАЖДУЮ строку stderr в
    # терминирующую ошибку. Обычное «WARNING: You are using pip version…»
    # обрывало установку при коде возврата 0 — на PowerShell 7 этого нет,
    # поэтому дефект проявлялся только на штатной для Windows 5.1.
    # Решение принимаем по коду возврата, как и задумано.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    # Сброс, о котором говорит комментарий ниже, — раньше он был только в
    # комментарии: код возврата оставался от прежней внешней программы.
    $global:LASTEXITCODE = 0
    try {
        $output = & $Command @Arguments 2>&1
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    # $LASTEXITCODE относится к последней ВНЕШНЕЙ программе: если сюда
    # передали функцию или командлет, он остался от чего-то постороннего.
    # Сбрасываем перед вызовом, чтобы не объявлять успешную работу сбойной.
    $code = $LASTEXITCODE
    if (-not $IgnoreExitCode -and $code -ne 0) {
        Write-Err "Команда завершилась с кодом ${code}: $display"
        $output | Select-Object -Last 25 | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkGray }
        $reason = if ($Description) { $Description } else { 'Ошибка выполнения команды' }
        throw "$reason (код $code)"
    }
    return $output
}

function Invoke-WithRetry {
    param([int]$Attempts = 3, [Parameter(Mandatory)][scriptblock]$Action, [string]$Description = 'операция')
    $delay = 2
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try { return & $Action }
        catch {
            if ($attempt -eq $Attempts) { throw }
            Write-Warn "Попытка $attempt из ${Attempts} не удалась ($Description), повтор через $delay с…"
            Start-Sleep -Seconds $delay
            $delay *= 2
        }
    }
}

function Install-EngineRequirements {
    <#
    .SYNOPSIS
        Ставит зависимости движка так же, как это делает установщик для Linux.
    .DESCRIPTION
        Рядом с обычным файлом требований может лежать три спутника, и без
        них движок ставится НЕ ПОЛНОСТЬЮ либо не ставится вовсе:

        * engines\no-deps\<движок>.txt — пакеты, которые ставятся с
          --no-deps. У GigaAM это сам GigaAM: его собственный pyproject
          требует onnxruntime==1.23.*, колёс под свежий Python у этой версии
          нет, и обычная установка обрывается с ResolutionImpossible;
        * engines\optional\<движок>.txt — необязательная часть: её отказ
          не должен ронять весь движок;
        * engines\optional\no-deps\<движок>.txt — то же, но с --no-deps.

        Установщик PowerShell не знал ни об одном из них и ставил только
        обычный файл. Для GigaAM это означало, что сам пакет на Windows не
        ставился НИКОГДА — а скрипт при этом печатал «gigaam установлен»:
        человек получал сервер без движка и сообщение об успехе.
    #>
    param(
        [Parameter(Mandatory)][string]$Pip,
        [Parameter(Mandatory)][string]$Requirements,
        [string[]]$PipFlags = @()
    )
    $dir = Split-Path -Parent $Requirements
    $name = Split-Path -Leaf $Requirements
    Invoke-Checked -Command $Pip -Arguments (@('install') + $PipFlags + @('-r', $Requirements)) | Out-Null

    $nodeps = Join-Path (Join-Path $dir 'no-deps') $name
    if (Test-Path $nodeps) {
        Write-Debug2 "спутник --no-deps: $nodeps"
        Invoke-Checked -Command $Pip -Arguments (
            @('install') + $PipFlags + @('--no-deps', '-r', $nodeps)) | Out-Null
    }

    $optional = Join-Path (Join-Path $dir 'optional') $name
    if (Test-Path $optional) {
        $optNodeps = Join-Path (Join-Path (Join-Path $dir 'optional') 'no-deps') $name
        try {
            Invoke-Checked -Command $Pip -Arguments (
                @('install') + $PipFlags + @('-r', $optional)) | Out-Null
            if (Test-Path $optNodeps) {
                Invoke-Checked -Command $Pip -Arguments (
                    @('install') + $PipFlags + @('--no-deps', '-r', $optNodeps)) | Out-Null
            }
        } catch {
            # Движок работает и без необязательной части, просто беднее.
            Write-Warn "  необязательная часть движка не установилась ($name)"
            Write-Hint "  поставить позже: $Pip install -r $optional"
        }
    }
}

function Install-Overrides {
    <#
    .SYNOPSIS
        Возвращает версии пакетов, задавленные зависимостями движков.
    .DESCRIPTION
        requirements\overrides.txt ставится последним и обязательно с
        --no-deps: строки в нём — это версии, которые мы выбрали сами
        вопреки требованиям чужих пакетов. Установщик PowerShell его не
        применял вовсе, и Windows-установка молча оставалась с версиями,
        которые в Linux считаются негодными.
    #>
    param([Parameter(Mandatory)][string]$Pip,
          [Parameter(Mandatory)][string]$RequirementsDir)
    $file = Join-Path $RequirementsDir 'overrides.txt'
    if (-not (Test-Path $file)) { return }
    Write-Debug2 "восстановление версий: $file"
    try {
        Invoke-Checked -Command $Pip -Arguments @(
            'install', '--no-deps', '--upgrade', '-r', $file) | Out-Null
    } catch {
        Write-Warn 'Не удалось вернуть версии из overrides.txt.'
        Write-Hint "Проверьте вручную: $Pip install --no-deps -r $file"
    }
}

# ---------------------------------------------------------------------------
# Пакеты окружения: чьи они и нужны ли ещё
# ---------------------------------------------------------------------------
#
# Двойники функций из common.sh (_pip_name, required_packages,
# engine_package, remove_retired_packages). Скрипты для Linux и Windows —
# пара, и расхождение между ними видно только на чужой машине: на сервере
# лишний пакет убрался, на ноутбуке остался и жалуется до сих пор.

function ConvertTo-PipName {
    <# Имя пакета в том виде, в каком его сравнивает pip: «Nemo_Toolkit» и
       «nemo-toolkit» — одно имя. #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Name)
    return ($Name.Trim().ToLowerInvariant() -replace '[_.]', '-')
}

function Get-RequirementName {
    <# Имя пакета из строки требований; пустая строка — если это не пакет
       (комментарий, ключ pip вроде --extra-index-url). #>
    param([AllowEmptyString()][string]$Line)
    $line = ($Line -split '#')[0]
    # Прямая ссылка PEP 508 — «пакет @ git+https://…»: имя до «@».
    $line = ($line -split '@')[0]
    $name = ($line -split '[<>=!~;\[]')[0].Trim()
    if (-not $name -or $name.StartsWith('-')) { return '' }
    return $name
}

function Get-RequirementNames {
    <# Имена всех пакетов, которые ещё перечислены хоть в одном списке
       требований под каталогом, — в написании pip. #>
    param([Parameter(Mandatory)][string]$RequirementsDir)
    if (-not (Test-Path $RequirementsDir)) { return @() }
    $names = foreach ($file in (Get-ChildItem $RequirementsDir -Recurse -Filter '*.txt' -File)) {
        foreach ($line in (Get-Content $file.FullName)) {
            $name = Get-RequirementName $line
            if ($name) { ConvertTo-PipName $name }
        }
    }
    return @($names | Sort-Object -Unique)
}

function Test-PackageInFamily {
    <# То же имя или спутник через дефис: «nemo-toolkit-asr» при требуемом
       «nemo_toolkit» — семья требуемого, а не чужой пакет. #>
    param([string]$Name, [string[]]$Family = @())
    foreach ($want in $Family) {
        if (-not $want) { continue }
        if ($Name -eq $want -or $Name.StartsWith("$want-")) { return $true }
    }
    return $false
}

function Get-PipPackageUsers {
    <#
    .SYNOPSIS
        Установлен ли пакет и кто из установленных на него опирается.
    .OUTPUTS
        Объект с полями Installed и Users. Объект, а не массив: пустой
        массив, возвращённый из функции, PowerShell превращает в $null, и
        «никто не опирается» стало бы неотличимо от «не установлен».
    #>
    param([Parameter(Mandatory)][string]$Pip, [Parameter(Mandatory)][string]$Name)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = 0
    try { $shown = @(& $Pip show $Name 2>$null) } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { return [pscustomobject]@{ Installed = $false; Users = @() } }
    $users = @()
    foreach ($line in $shown) {
        if ("$line" -match '^Required-by:\s*(.*)$') {
            $users = @($Matches[1] -split ',' | ForEach-Object { $_.Trim() } |
                Where-Object { $_ } | ForEach-Object { ConvertTo-PipName $_ })
        }
    }
    return [pscustomobject]@{ Installed = $true; Users = $users }
}

function Get-RetiredPackages {
    <# Имена из списка снятых пакетов, в написании pip. #>
    param([string]$ListFile = (Join-Path $PSScriptRoot 'retired-packages.txt'))
    if (-not (Test-Path $ListFile)) { return @() }
    $names = foreach ($line in (Get-Content $ListFile)) {
        $name = ($line -split '#')[0].Trim()
        if ($name) { ConvertTo-PipName $name }
    }
    return @($names | Sort-Object -Unique)
}

function Remove-RetiredPackages {
    <#
    .SYNOPSIS
        Снимает пакеты, которые ставили прежние версии и которые больше не нужны.
    .DESCRIPTION
        Двойник remove_retired_packages из common.sh; условия и их смысл —
        в самом списке, lib\retired-packages.txt. Снимается только то, чего
        нет в требованиях и на чём не держится ничего, кроме снимаемого же.
        Вызывать после Install-Overrides: итог должен описывать окружение
        уже без них.
    #>
    param(
        [Parameter(Mandatory)][string]$Pip,
        [Parameter(Mandatory)][string]$RequirementsDir,
        [string]$ListFile = (Join-Path $PSScriptRoot 'retired-packages.txt')
    )
    if (-not (Test-Path $Pip)) { return }
    $retired = @(Get-RetiredPackages -ListFile $ListFile)
    if ($retired.Count -eq 0) { return }
    $required = @(Get-RequirementNames -RequirementsDir $RequirementsDir)

    $usersOf = @{}
    foreach ($name in $retired) {
        if (Test-PackageInFamily -Name $name -Family $required) {
            Write-Debug2 "$name снова в требованиях — строку в retired-packages.txt пора убрать"
            continue
        }
        $info = Get-PipPackageUsers -Pip $Pip -Name $name
        if ($info.Installed) { $usersOf[$name] = @($info.Users) }
    }
    $doomed = [System.Collections.Generic.List[string]]::new()
    # По алфавиту, как и в common.sh: порядок хеш-таблицы случаен, а журнал
    # двух прогонов на одном окружении должен совпадать.
    foreach ($name in @($usersOf.Keys | Sort-Object)) { $doomed.Add($name) }

    # По кругу: оставленный пакет сам становится опорой для других.
    do {
        $changed = $false
        foreach ($name in @($doomed)) {
            foreach ($user in $usersOf[$name]) {
                if (-not $doomed.Contains($user)) {
                    Write-Debug2 "$name не снимаем: на нём держится $user"
                    [void]$doomed.Remove($name)
                    $changed = $true
                    break
                }
            }
        }
    } while ($changed)
    if ($doomed.Count -eq 0) { return }

    $names = @($doomed | Sort-Object)
    if (Get-DryRun) {
        Write-Info "Пробный запуск: сняли бы оставшееся от прежних версий — $($names -join ', ')"
        return
    }
    try {
        Invoke-Checked -Command $Pip -Arguments (@('uninstall', '-y') + $names) | Out-Null
        Write-Ok "Убрано оставшееся от прежних версий: $($names -join ', ')"
    } catch {
        Write-Warn "Не удалось убрать оставшееся от прежних версий: $($names -join ', ')"
        Write-Hint "  $Pip uninstall -y $($names -join ' ')"
    }
}

function Get-EnginePackage {
    <#
    .SYNOPSIS
        Собственный пакет движка — по нему судят, установлен ли движок.
    .DESCRIPTION
        Первая настоящая строка файла-спутника no-deps, а без него — самого
        файла требований: файлы так и написаны, сначала движок, потом его
        окружение. Любая строка не годится: pyannote.audio стоит ради
        диаризации, и по нему whisperx «оказывался» установленным.
    #>
    param([Parameter(Mandatory)][string]$Requirements)
    $dir = Split-Path -Parent $Requirements
    $leaf = Split-Path -Leaf $Requirements
    $file = Join-Path (Join-Path $dir 'no-deps') $leaf
    if (-not (Test-Path $file)) { $file = $Requirements }
    if (-not (Test-Path $file)) { return '' }
    foreach ($line in (Get-Content $file)) {
        $name = Get-RequirementName $line
        if ($name) { return $name }
    }
    return ''
}

function Test-EngineInstalled {
    <# Установлен ли движок — по его собственному пакету. #>
    param([Parameter(Mandatory)][string]$Pip, [Parameter(Mandatory)][string]$Requirements)
    $name = Get-EnginePackage -Requirements $Requirements
    if (-not $name) { return $false }
    return [bool](Get-PipPackageUsers -Pip $Pip -Name $name).Installed
}

function Get-EnginePackages {
    <#
    .SYNOPSIS
        Все пакеты движка: его файл, спутник no-deps и необязательная часть.
    .DESCRIPTION
        Снимается движок тем же набором, каким ставился. Без спутников
        «remove-engine gigaam» снимал окружение GigaAM, а сам gigaam
        (он только в no-deps\gigaam.txt) оставлял на месте.
    #>
    param([Parameter(Mandatory)][string]$Requirements)
    $dir = Split-Path -Parent $Requirements
    $leaf = Split-Path -Leaf $Requirements
    $optional = Join-Path $dir 'optional'
    $files = @($Requirements,
               (Join-Path (Join-Path $dir 'no-deps') $leaf),
               (Join-Path $optional $leaf),
               (Join-Path (Join-Path $optional 'no-deps') $leaf))
    $names = foreach ($file in $files) {
        if (-not (Test-Path $file)) { continue }
        foreach ($line in (Get-Content $file)) {
            $name = Get-RequirementName $line
            if ($name) { $name }
        }
    }
    return @($names | Select-Object -Unique)
}

function Confirm-Action {
    param([string]$Message, [string]$Default = 'y')
    if ($script:AssumeYes) { return $true }
    # Без консоли берём заданное умолчание, а не «да». Прежнее безусловное
    # согласие означало, что `uninstall.ps1 -Purge` из задачи планировщика,
    # WinRM-сессии или CI удалял каталог данных с базой и моделями, ни о чём
    # не спросив, — притом что вопрос задан с умолчанием «нет». В bash-
    # двойнике это уже исправлено.
    # UserInteractive истинен и при перенаправленном вводе, поэтому защита
    # не срабатывала: скрипт уходил в Read-Host, а в неинтерактивном хосте
    # (задача планировщика, WinRM, CI) тот бросает исключение — и с
    # $ErrorActionPreference='Stop' удаление или обновление обрывалось
    # аварийно. Спрашиваем ровно то, что важно: есть ли откуда читать ответ.
    # Тот же признак использует мастер установки (Test-Interactive).
    if ([Console]::IsInputRedirected -or -not [Environment]::UserInteractive) {
        Write-Info "$Message — нет консоли, взято умолчание: $Default"
        return ($Default -eq 'y')
    }
    $suffix = if ($Default -eq 'n') { '[y/N]' } else { '[Y/n]' }
    $answer = Read-Host "? $Message $suffix"
    if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $Default }
    return $answer -match '^(y|yes|д|да)$'
}

# ---------------------------------------------------------------------------
# Проверки окружения
# ---------------------------------------------------------------------------

function Test-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Find-Python {
    $candidates = @()
    foreach ($name in 'python3.13', 'python3.12', 'python3.11', 'python3.10', 'python', 'python3') {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    if (Test-CommandExists 'py') {
        foreach ($v in '3.13', '3.12', '3.11', '3.10') {
            try {
                $path = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $path) { $candidates += $path.Trim() }
            } catch { }
        }
    }
    foreach ($path in ($candidates | Select-Object -Unique)) {
        try {
            $version = & $path -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$version -ge $script:MinPython) { return $path }
        } catch { }
    }
    return $null
}

function Test-DiskSpace {
    param([string]$Path, [int]$RequiredGb)
    # Оператор ?? отсутствует в Windows PowerShell 5.1 — пишем совместимо.
    $parent = Split-Path -Parent $Path
    if ([string]::IsNullOrEmpty($parent)) { $parent = $Path }
    $resolved = Resolve-Path -LiteralPath $parent -ErrorAction SilentlyContinue
    $target = if ($resolved) { $resolved.Path } else { $Path }
    $root = [System.IO.Path]::GetPathRoot($target)
    if (-not $root) { return $true }
    $drive = Get-PSDrive -Name $root.Substring(0, 1) -ErrorAction SilentlyContinue
    if (-not $drive) { return $true }
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -lt $RequiredGb) {
        Write-Err "На диске $root свободно $freeGb ГБ, требуется не менее $RequiredGb ГБ."
        return $false
    }
    Write-Debug2 "свободно на ${root}: $freeGb ГБ"
    return $true
}

function Test-PortFree {
    param([int]$Port)
    if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
        try {
            $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
            return -not $listener
        } catch { }
    }
    # Запасной способ: пробуем занять порт сами.
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start(); $listener.Stop()
        return $true
    } catch { return $false }
}

function Find-FreePort {
    param([int]$Start)
    for ($port = $Start; $port -lt $Start + 50; $port++) {
        if (Test-PortFree -Port $port) { return $port }
    }
    throw "Не найден свободный порт в диапазоне $Start–$($Start + 50)."
}

function Test-Internet {
    param([string]$HostName = 'pypi.org')
    try {
        $response = Invoke-WebRequest -Uri "https://$HostName" -Method Head -TimeoutSec 8 -UseBasicParsing
        return $response.StatusCode -lt 400
    } catch { return $false }
}

# ---------------------------------------------------------------------------
# Оборудование
# ---------------------------------------------------------------------------

function Resolve-GpuFromControllers {
    <#
      Разбор списка видеоадаптеров: какой из них главный и что о нём известно.
      Вынесено из Get-GpuOnBus отдельной функцией, чтобы логику выбора можно
      было проверить, подсунув придуманные адаптеры, а не только на машине с
      нужной картой.

      На вход — объекты со свойствами Name, PNPDeviceID, DriverVersion.
      На выход — @{ Vendor; Name; PnpId; Discrete; DriverVersion; DriverIsGeneric }
    #>
    param([object[]]$Cards)

    $found = [ordered]@{ Vendor = ''; Name = ''; PnpId = ''; Discrete = $false
                         DriverVersion = ''; DriverIsGeneric = $true }
    $bestRank = -1

    foreach ($card in @($Cards)) {
        $pnp = "$($card.PNPDeviceID)"
        $name = "$($card.Name)"
        $vendor = ''
        if ($pnp -match 'VEN_10DE') { $vendor = 'nvidia' }
        elseif ($pnp -match 'VEN_1002') { $vendor = 'amd' }
        elseif ($pnp -match 'VEN_8086') { $vendor = 'intel' }
        else { continue }

        # Дискретная или встроенная. AdapterRAM для этого не годится: поле
        # 32-битное, и на картах свыше 4 ГБ Windows возвращает мусор. У NVIDIA
        # встроенной графики не бывает; у AMD и Intel встроенную выдаёт имя.
        $discrete = $true
        if ($vendor -ne 'nvidia' -and
            $name -match 'UHD Graphics|HD Graphics|Iris|Vega \d+ Graphics|Radeon\(TM\) Graphics|Radeon Graphics') {
            $discrete = $false
        }

        # Стандартный адаптер Microsoft — это отсутствие драйвера производителя.
        $generic = ($name -match 'Microsoft Basic Display|Standard VGA|Basic Render')

        $rank = switch ($vendor) { 'nvidia' { 30 } 'amd' { 20 } 'intel' { 10 } default { 0 } }
        if ($discrete) { $rank += 100 }
        if ($rank -gt $bestRank) {
            $bestRank = $rank
            $found.Vendor = $vendor
            $found.Name = $name
            $found.PnpId = $pnp
            $found.Discrete = $discrete
            $found.DriverVersion = "$($card.DriverVersion)"
            $found.DriverIsGeneric = $generic
        }
    }
    return $found
}

function Get-GpuOnBus {
    <#
      Карта так, как её видит сама Windows, а не драйвер вычислений.
      Win32_VideoController отвечает и тогда, когда стоит стандартный
      видеоадаптер Microsoft, то есть когда драйвера производителя нет, —
      а это ровно тот случай, ради которого всё и затевалось.
    #>
    if (-not (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) {
        return (Resolve-GpuFromControllers @())
    }
    try {
        return (Resolve-GpuFromControllers @(Get-CimInstance Win32_VideoController -ErrorAction Stop))
    } catch {
        return (Resolve-GpuFromControllers @())
    }
}

function Get-GpuDriverPackage {
    <#
      Что ставить под найденного производителя.

      WingetIds — список кандидатов по убыванию предпочтения, а не один
      идентификатор: каталог winget живёт своей жизнью, пакеты в нём
      появляются, переименовываются и исчезают (у AMD собственного пакета
      с драйвером нет вовсе). Install-GpuDriver проверяет кандидатов по
      очереди и берёт первый существующий, а если не нашёлся ни один —
      показывает прямую ссылку. Так неверный идентификатор превращается
      в ссылку, а не в ошибку установки.
    #>
    param([ValidateSet('nvidia','amd','intel')][string]$Vendor)
    switch ($Vendor) {
        'nvidia' { return [ordered]@{
            WingetIds = @('Nvidia.GeForceExperience', 'Nvidia.NVIDIAApp', 'Nvidia.CUDA')
            Fallback  = 'https://www.nvidia.com/Download/index.aspx'
            Label     = 'драйвер NVIDIA' } }
        'amd'    { return [ordered]@{
            WingetIds = @('AMD.AMDSoftwareAdrenalinEdition', 'AMD.AMDSoftware')
            Fallback  = 'https://www.amd.com/en/support/download/drivers.html'
            Label     = 'драйвер AMD Adrenalin' } }
        'intel'  { return [ordered]@{
            WingetIds = @('Intel.IntelDriverAndSupportAssistant')
            Fallback  = 'https://www.intel.com/content/www/us/en/download/785597/intel-arc-iris-xe-graphics-windows.html'
            Label     = 'драйвер Intel Arc' } }
    }
}

function Test-WingetPackage {
    # Есть ли такой пакет в каталоге winget. Отдельной функцией, чтобы
    # проверку можно было подменить в тестах.
    param([string]$Id)
    if (-not (Test-CommandExists 'winget')) { return $false }
    try {
        $null = & winget show --id $Id -e --disable-interactivity 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Install-GpuDriver {
    <#
      Ставит драйвер видеокарты через winget, а если подходящего пакета нет —
      показывает прямую ссылку. Возвращает $true, только если установка
      действительно выполнена.

      В Windows драйвер обычно уже стоит из коробки или приезжает через
      Windows Update, поэтому вызывать это стоит лишь когда система
      показывает стандартный видеоадаптер Microsoft.
    #>
    param([ValidateSet('nvidia','amd','intel')][string]$Vendor)

    $pkg = Get-GpuDriverPackage -Vendor $Vendor
    if (-not (Test-CommandExists 'winget')) {
        Write-Warn "winget не найден — поставить $($pkg.Label) автоматически нельзя."
        Write-Hint "Скачайте вручную: $($pkg.Fallback)"
        return $false
    }
    if (-not (Test-Administrator)) {
        Write-Warn 'Установка драйвера требует прав администратора.'
        Write-Hint 'Запустите PowerShell от имени администратора и повторите.'
        Write-Hint "Либо поставьте вручную: $($pkg.Fallback)"
        return $false
    }

    $chosen = $null
    foreach ($id in $pkg.WingetIds) {
        if (Test-WingetPackage -Id $id) { $chosen = $id; break }
        Write-Debug2 "В каталоге winget нет пакета $id"
    }
    if (-not $chosen) {
        Write-Warn "В каталоге winget нет подходящего пакета ($($pkg.Label))."
        Write-Hint "Скачайте с сайта производителя: $($pkg.Fallback)"
        Write-Hint 'Либо дождитесь Windows Update — драйверы видеокарт приходят и оттуда.'
        return $false
    }

    Write-Info "Ставим $($pkg.Label) через winget ($chosen)."
    try {
        Invoke-Checked -Command 'winget' -Arguments @(
            'install', '--id', $chosen, '-e',
            '--accept-package-agreements', '--accept-source-agreements',
            '--disable-interactivity') -IgnoreExitCode
        Write-Ok "$($pkg.Label): установка выполнена."
        Write-Hint 'Драйвер вступит в силу после перезагрузки.'
        return $true
    } catch {
        Write-Warn "Установка через winget не удалась: $_"
        Write-Hint "Скачайте вручную: $($pkg.Fallback)"
        return $false
    }
}

function Get-GpuInfo {
    $result = [ordered]@{ Accelerator = 'cpu'; Name = ''; MemoryMb = 0; CudaVersion = '' }
    if (Test-CommandExists 'nvidia-smi') {
        try {
            $line = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
            if ($line) {
                $parts = $line -split ','
                $result.Accelerator = 'cuda'
                $result.Name = $parts[0].Trim()
                $result.MemoryMb = [int]($parts[1].Trim())
                $smi = & nvidia-smi 2>$null | Out-String
                if ($smi -match 'CUDA Version:\s*([\d.]+)') { $result.CudaVersion = $Matches[1] }
            }
        } catch { }
    }
    return $result
}

function Get-HardwareInfo {
    # Get-CimInstance есть только в Windows PowerShell; на других платформах
    # (например, при проверке скриптов в контейнере) обходимся тем, что доступно.
    $cpu = $null; $os = $null
    if (Get-Command Get-CimInstance -ErrorAction SilentlyContinue) {
        try { $cpu = Get-CimInstance Win32_Processor -ErrorAction SilentlyContinue | Select-Object -First 1 } catch { }
        try { $os  = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue } catch { }
    }
    $gpu = Get-GpuInfo
    return [ordered]@{
        OsName       = if ($os) { $os.Caption } else { 'Windows' }
        OsVersion    = if ($os) { $os.Version } else { [Environment]::OSVersion.Version.ToString() }
        Arch         = $env:PROCESSOR_ARCHITECTURE
        CpuName      = if ($cpu) { $cpu.Name.Trim() } else { 'неизвестно' }
        CpuCores     = if ($cpu) { $cpu.NumberOfCores } else { [Environment]::ProcessorCount }
        CpuThreads   = [Environment]::ProcessorCount
        RamGb        = if ($os) { [math]::Round($os.TotalVisibleMemorySize / 1MB, 1) } else { 0 }
        Accelerator  = $gpu.Accelerator
        GpuName      = $gpu.Name
        GpuMemoryMb  = $gpu.MemoryMb
        CudaVersion  = $gpu.CudaVersion
        Ffmpeg       = Test-CommandExists 'ffmpeg'
    }
}

function Show-Environment {
    $hw = Get-HardwareInfo
    Write-Host 'Обнаруженное окружение' -ForegroundColor White
    Write-Host ("  Система          {0} ({1})" -f $hw.OsName, $hw.OsVersion)
    Write-Host ("  Архитектура      {0}" -f $hw.Arch)
    Write-Host ("  Процессор        {0}" -f $hw.CpuName)
    Write-Host ("  Ядер             {0} физических / {1} логических" -f $hw.CpuCores, $hw.CpuThreads)
    Write-Host ("  Память           {0} ГБ" -f $hw.RamGb)
    Write-Host ("  Ускоритель       {0}" -f $hw.Accelerator)
    if ($hw.GpuName) { Write-Host ("  Видеокарта       {0} ({1} МБ)" -f $hw.GpuName, $hw.GpuMemoryMb) }
    if ($hw.CudaVersion) { Write-Host ("  CUDA             {0}" -f $hw.CudaVersion) }
    Write-Host ("  ffmpeg           {0}" -f $(if ($hw.Ffmpeg) { 'установлен' } else { 'не найден' }))
    Write-Host ''
    return $hw
}

function Get-TorchIndexUrl {
    param([string]$Accelerator, [string]$CudaVersion)
    switch ($Accelerator) {
        'cuda' {
            if ($CudaVersion -like '13.*') { return 'https://download.pytorch.org/whl/cu130' }
            if ($CudaVersion -like '12.8*' -or $CudaVersion -like '12.9*') { return 'https://download.pytorch.org/whl/cu128' }
            if ($CudaVersion -like '12.*') { return 'https://download.pytorch.org/whl/cu124' }
            if ($CudaVersion -like '11.*') { return 'https://download.pytorch.org/whl/cu118' }
            return 'https://download.pytorch.org/whl/cu124'
        }
        default { return 'https://download.pytorch.org/whl/cpu' }
    }
}

function Get-CTranslate2Pin {
    param([string]$Accelerator, [string]$CudaVersion)
    if ($Accelerator -ne 'cuda') { return 'ctranslate2>=4.5' }
    if ($CudaVersion -like '11.*') { return 'ctranslate2==3.24.0' }
    return 'ctranslate2>=4.5'
}

function Get-RecommendedProfile {
    $hw = Get-HardwareInfo
    if ($hw.Accelerator -eq 'cuda') {
        if ($hw.GpuMemoryMb -ge 20000) { return 'full' }
        if ($hw.GpuMemoryMb -ge 8000)  { return 'standard' }
        return 'light'
    }
    if ($hw.RamGb -ge 16) { return 'cpu' }
    return 'light'
}

function Format-Size {
    param([long]$Bytes)
    $units = 'Б', 'КБ', 'МБ', 'ГБ', 'ТБ'
    $value = [double]$Bytes; $i = 0
    while ($value -ge 1024 -and $i -lt 4) { $value /= 1024; $i++ }
    return ('{0:N1} {1}' -f $value, $units[$i])
}

function Set-DryRun    { param([bool]$Value) $script:DryRun = $Value }
function Set-AssumeYes { param([bool]$Value) $script:AssumeYes = $Value }
function Set-Quiet     { param([bool]$Value) $script:Quiet = $Value }
function Get-DryRun    { return $script:DryRun }
function Get-LogFile   { return $script:LogFile }


# ---------------------------------------------------------------------------
# Интерактивные диалоги мастера установки
# ---------------------------------------------------------------------------
#
# Ведут себя так же, как их аналоги в scripts/lib/wizard.sh: при -Yes или при
# запуске без консоли берётся значение по умолчанию, вопрос не задаётся.

function Test-Interactive {
    <#
        .SYNOPSIS
        Можно ли задавать вопросы: есть консоль и не задан -Yes.
    #>
    if ($script:AssumeYes) { return $false }
    try { if ([Console]::IsInputRedirected) { return $false } } catch { return $false }
    return $true
}

function Write-WizardStep {
    param([string]$Title, [string]$Subtitle = '')
    Write-Host ''
    Write-Host $Title -ForegroundColor Blue
    if ($Subtitle) { Write-Host $Subtitle -ForegroundColor DarkGray }
    Write-Host ('─' * 68) -ForegroundColor DarkGray
}

function Select-WizardOption {
    <#
        .SYNOPSIS
        Выбор одного пункта из списка.

        .PARAMETER Options
        Массив хеш-таблиц: @{ Value = 'cpu'; Label = 'Без видеокарты'; Note = '…' }
    #>
    param(
        [Parameter(Mandatory)][string]$Question,
        [Parameter(Mandatory)][array]$Options,
        [int]$DefaultIndex = 1
    )
    if (-not (Test-Interactive)) {
        $chosen = $Options[$DefaultIndex - 1]
        Write-Info "$Question → $($chosen.Label) (по умолчанию)"
        return $chosen.Value
    }

    Write-Host ''
    Write-Host $Question
    for ($i = 0; $i -lt $Options.Count; $i++) {
        $mark = if (($i + 1) -eq $DefaultIndex) { '>' } else { ' ' }
        Write-Host ("{0} {1,2}) {2}" -f $mark, ($i + 1), $Options[$i].Label)
        if ($Options[$i].Note) {
            Write-Host ("      " + $Options[$i].Note) -ForegroundColor DarkGray
        }
    }
    while ($true) {
        $answer = Read-Host ("Выбор [{0}]" -f $DefaultIndex)
        if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $DefaultIndex }
        $number = 0
        if ([int]::TryParse($answer, [ref]$number) -and $number -ge 1 -and $number -le $Options.Count) {
            Write-Ok $Options[$number - 1].Label
            return $Options[$number - 1].Value
        }
        Write-Warn "Введите число от 1 до $($Options.Count)."
    }
}

function Select-WizardMany {
    <#
        .SYNOPSIS
        Отметить несколько пунктов. Возвращает массив выбранных значений.
    #>
    param(
        [Parameter(Mandatory)][string]$Question,
        [Parameter(Mandatory)][array]$Options,
        [string]$Default = '1'
    )
    $picked = @()
    if (-not (Test-Interactive)) {
        $answer = $Default
    } else {
        Write-Host ''
        Write-Host $Question
        for ($i = 0; $i -lt $Options.Count; $i++) {
            $mark = if (",$Default," -like "*,$($i + 1),*") { '>' } else { ' ' }
            Write-Host ("{0} {1,2}) {2}" -f $mark, ($i + 1), $Options[$i].Label)
            if ($Options[$i].Note) {
                Write-Host ("      " + $Options[$i].Note) -ForegroundColor DarkGray
            }
        }
        Write-Host '   номера через запятую, «все» — всё, «нет» — ничего' -ForegroundColor DarkGray
        $answer = Read-Host ("Выбор [{0}]" -f $Default)
        if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $Default }
    }

    if ($answer -match '^(все|всё|all)$') { return $Options.Value }
    if ($answer -match '^(нет|none|-)$') { return @() }
    foreach ($part in $answer -split ',') {
        $number = 0
        if ([int]::TryParse($part.Trim(), [ref]$number) -and
            $number -ge 1 -and $number -le $Options.Count) {
            $picked += $Options[$number - 1].Value
        }
    }
    return $picked
}

function Read-WizardValue {
    <#
        .SYNOPSIS
        Свободный ввод с необязательной проверкой.

        .PARAMETER Validator
        Блок скрипта: принимает введённое значение, возвращает $true или $false.
    #>
    param(
        [Parameter(Mandatory)][string]$Question,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Default,
        [scriptblock]$Validator = $null,
        [string]$Note = ''
    )
    if (-not (Test-Interactive)) { return $Default }
    if ($Note) { Write-Host ''; Write-Host $Note -ForegroundColor DarkGray }
    while ($true) {
        $answer = Read-Host ("{0} [{1}]" -f $Question, $Default)
        if ([string]::IsNullOrWhiteSpace($answer)) { $answer = $Default }
        if ($null -eq $Validator -or (& $Validator $answer)) { return $answer }
    }
}

function Test-HfToken {
    <#
        .SYNOPSIS
        Проверка токена Hugging Face для мастера установки.

        .DESCRIPTION
        Пустая строка — законный ответ: токен нужен не всем, и заставлять его
        придумывать значило бы либо врать, либо застревать на этом вопросе.
        Двойник bash-функции wizard_valid_hf_token: правило одно и то же, иначе
        один и тот же токен принимался бы на Linux и отвергался на Windows.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Token)
    if ([string]::IsNullOrWhiteSpace($Token)) { return $true }
    if ($Token -notmatch '^hf_[A-Za-z0-9_-]{16,}$') {
        Write-Warn 'Токен Hugging Face выглядит так: hf_ и ещё не меньше шестнадцати знаков.'
        Write-Hint 'Взять его: https://huggingface.co/settings/tokens — права «read» достаточно.'
        Write-Hint 'Оставьте поле пустым, если токен не нужен.'
        return $false
    }
    return $true
}

function Show-WizardSummary {
    <#
        .SYNOPSIS
        Сводка перед началом работы.

        .PARAMETER Rows
        Упорядоченный словарь «ключ — значение».
    #>
    param([Parameter(Mandatory)]$Rows)
    Write-Host ''
    Write-Host 'Что будет сделано'
    Write-Host ('─' * 68) -ForegroundColor DarkGray
    foreach ($key in $Rows.Keys) {
        Write-Host ("  {0} {1}" -f $key.PadRight(24), $Rows[$key])
    }
    Write-Host ''
}


# ---------------------------------------------------------------------------
# Видит ли карту процесс, который будет распознавать
# ---------------------------------------------------------------------------
#
# «Карта есть в машине» и «карту видит процесс» — разные ответы, и расходятся
# они тихо: диспетчер устройств показывает исправную карту, nvidia-smi
# отвечает, а torch получает отказ. Обновление, поставленное на такую машину,
# проходит все проверки, а падает каждое задание по отдельности.
#
# На Windows это чаще всего чужой номер в CUDA_VISIBLE_DEVICES, процессорная
# сборка PyTorch или драйвер, поставленный без перезагрузки.

function Get-ConfigDevice {
    param([string]$DataDir)
    $file = Join-Path $DataDir 'config.yaml'
    if (-not (Test-Path $file)) { return '' }
    foreach ($line in Get-Content $file) {
        if ($line -match '^\s*device:\s*(.+?)\s*(#.*)?$') {
            return $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    return ''
}

function Test-GpuRuntime {
    <#
    .SYNOPSIS
    Спрашивает карту у того самого питона, который будет распознавать.
    .OUTPUTS
    $true — распознавание поедет; $false — настроена карта, которой у процесса нет.
    #>
    param(
        [string]$Python,
        [string]$DataDir,
        [string]$CodeDir
    )
    $device = Get-ConfigDevice -DataDir $DataDir
    if (-not $device) { $device = 'auto' }
    if ($device -eq 'cpu') {
        Write-Info 'Распознавание настроено на процессор — видеокарту проверять незачем.'
        return $true
    }
    if (-not (Test-Path $Python)) {
        Write-Info "Видеокарту проверить нечем: нет интерпретатора $Python"
        return $true
    }

    $probe = @'
import sys
sys.path.insert(0, sys.argv[1])
устройство = (sys.argv[2] or "auto").strip().lower()
try:
    import torch
except Exception:
    print("skip||torch не установлен — спросить карту нечем"); raise SystemExit(0)
try:
    from asrhub.hardware import проверить_ускоритель
except Exception:
    проверить_ускоритель = None
if устройство in ("", "auto"):
    try:
        есть = bool(torch.cuda.is_available())
    except Exception:
        есть = False
    print("ok|auto -> cuda|" if есть else "cpu|auto -> cpu|карта не отвечает, сервер уйдёт на процессор")
    raise SystemExit(0)
if проверить_ускоритель is not None:
    годно, причина = проверить_ускоритель(устройство)
else:
    try:
        годно, причина = (int(torch.cuda.device_count()) > 0), ""
    except Exception as exc:
        годно, причина = False, f"CUDA не отвечает на перечислении устройств: {exc}"
    if годно:
        try:
            torch.cuda.get_device_name(0)
        except Exception as exc:
            годно, причина = False, f"Карта не отвечает: {exc}"
print(f"ok|{устройство}|" if годно else f"fail|{устройство}|{причина}")
'@
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("asrhub-gpu-{0}.py" -f ([guid]::NewGuid()))
    # Файл, а не аргумент -c: код на кириллице через командную строку теряет
    # кодировку ровно на тех машинах, где он и нужен.
    [System.IO.File]::WriteAllText($tmp, $probe, [System.Text.UTF8Encoding]::new($false))
    try {
        $out = & $Python $tmp $CodeDir $device 2>$null
    } catch {
        $out = ''
    } finally {
        Remove-Item $tmp -Force -ErrorAction SilentlyContinue
    }
    if (-not $out) {
        Write-Info 'Видеокарту проверить нечем: проба не выполнилась.'
        return $true
    }
    $parts = ($out | Select-Object -Last 1) -split '\|', 3
    switch ($parts[0]) {
        'ok'   { Write-Ok "Видеокарта доступна процессу: $($parts[1])"; return $true }
        'skip' { Write-Info "Видеокарту проверить нечем: $($parts[2])"; return $true }
        'cpu'  {
            Write-Warn "Видеокарта не отвечает: $($parts[1])."
            Write-Hint 'Задания пойдут на процессоре — в несколько раз медленнее реального времени.'
            Show-GpuRuntimeDiagnosis
            return $true
        }
        default {
            Write-Err "Настроено «device: $device», но карта процессу недоступна."
            Write-Host "  $($parts[2])"
            Show-GpuRuntimeDiagnosis
            Write-Hint "Чтобы приём не стоял, пока карта чинится: device: cpu в $DataDir\config.yaml"
            return $false
        }
    }
}

function Show-GpuRuntimeDiagnosis {
    $visible = [Environment]::GetEnvironmentVariable('CUDA_VISIBLE_DEVICES', 'Machine')
    if (-not $visible) { $visible = $env:CUDA_VISIBLE_DEVICES }
    if ($visible) {
        Write-Warn "Задано CUDA_VISIBLE_DEVICES=$visible."
        Write-Hint 'Если такой карты нет, CUDA отвечает «invalid device ordinal».'
    }
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $smi = & nvidia-smi -L 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) {
            Write-Warn 'nvidia-smi не отвечает:'
            Write-Host ('  ' + $smi.Trim())
            Write-Hint 'Обычно это драйвер, поставленный без перезагрузки. Перезагрузите машину.'
        } else {
            Write-Info ('Карты по nvidia-smi: ' + $smi.Trim())
            Write-Hint 'Карта видна системе, но не питону — обычно установлена процессорная сборка PyTorch.'
            Write-Hint 'Переустановить: venv\Scripts\pip install --force-reinstall --index-url https://download.pytorch.org/whl/cu130 torch torchaudio'
        }
    } else {
        Write-Warn 'nvidia-smi не найдена — драйвер NVIDIA не установлен.'
    }
}

# ---------------------------------------------------------------------------
# Экспорт — последней строкой файла
# ---------------------------------------------------------------------------
#
# `Export-ModuleMember -Function *` видит только функции, определённые ДО
# него. Стоял он раньше проверки видеокарты, и `Test-GpuRuntime` с
# `Show-GpuRuntimeDiagnosis` наружу не выходили: update.ps1 падал с «The term
# 'Test-GpuRuntime' is not recognized», а install.ps1 — на последнем шаге,
# уже после установки, и откатывал её. Всё новое — выше этой строки.

Export-ModuleMember -Function *
