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

$script:AsrHubVersion   = '3.0.0'
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
    $output = & $Command @Arguments 2>&1
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

function Confirm-Action {
    param([string]$Message, [string]$Default = 'y')
    if ($script:AssumeYes) { return $true }
    if (-not [Environment]::UserInteractive) { return $true }
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

Export-ModuleMember -Function *
