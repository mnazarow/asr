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

.PARAMETER NoGpuDriver
    Не ставить драйвер видеокарты. По умолчанию установщик находит карту
    через Windows (она видна и без драйвера производителя) и, если стоит
    стандартный адаптер, ставит драйвер через winget.

.PARAMETER Force
    Переустановить поверх существующей установки: код, зависимости и
    окружение Python собираются заново. config.yaml, ключи и данные не
    трогаются.

.PARAMETER ResetConfig
    Заменить config.yaml шаблоном. Прежний файл остаётся рядом копией
    config.yaml.bak.*; пропадут ключи интеграций, токен Hugging Face и все
    настройки.

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
    [string]$HfToken = '',
    [string]$HfTokenFile = '',
    [switch]$SkipModels,
    [switch]$NoService,
    [switch]$NoGpuDriver,
    [ValidateSet('', 'none', 'mfa', 'whisperx')][string]$Alignment = '',
    [switch]$Interactive,
    [switch]$NoInteractive,
    [switch]$Offline,
    [switch]$Force,
    [switch]$ResetConfig,
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$Quiet
)

# Имена параметров, заданных явно: мастер обязан их уважать, а не
# подставлять поверх свои умолчания.
$script:ExplicitArgs = [System.Collections.Generic.HashSet[string]]::new(
    [string[]]$PSBoundParameters.Keys)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force

# Токен Hugging Face: файл важнее ключа (в командной строке секрет виден в
# списке процессов), а окружение — на случай запуска из готового скрипта.
if ($HfTokenFile) {
    $HfToken = (Get-Content -LiteralPath $HfTokenFile -Raw).Trim()
    $script:ExplicitArgs.Add('HfToken') | Out-Null
}
if (-not $HfToken) {
    $HfToken = if ($env:HF_TOKEN) { $env:HF_TOKEN }
               elseif ($env:HUGGING_FACE_HUB_TOKEN) { $env:HUGGING_FACE_HUB_TOKEN }
               else { '' }
}

$script:GpuRebootRequired = $false
$script:GpuTargetAccel = ''

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
# Запуск из уже установленной копии — её install.ps1 советуют с -Force для
# починки окружения — ставит поверх неё же, со своим каталогом данных, а не
# в стандартный каталог рядом.
$ownDir = Split-Path -Parent $PSScriptRoot
if (-not $Prefix -and (Test-AsrInstallation -Path $ownDir)) {
    $Prefix = ConvertTo-AsrFullPath $ownDir
    if (-not $DataDir) {
        $ownInstall = Resolve-AsrInstallation -ScriptDir $PSScriptRoot -Prefix $Prefix
        if ($ownInstall.DataDirSource -in @('marker', 'service')) { $DataDir = $ownInstall.DataDir }
    }
}
if (-not $Prefix) {
    $Prefix = if ($isAdmin) { 'C:\Program Files\ASRHub' } else { Join-Path $env:LOCALAPPDATA 'ASRHub' }
}
if (-not $DataDir) {
    $DataDir = if ($isAdmin) { Join-Path $env:ProgramData 'ASRHub' } else { Join-Path $env:LOCALAPPDATA 'ASRHub\data' }
}
if (-not $Profile) { $Profile = Get-RecommendedProfile }

# Адрес и порт стоящей установки, если их не задали ключами. Повторная
# установка поверх (-Force для починки окружения) брала умолчания скрипта:
# сервер переезжал с настроенного порта на 8080, а установка, закрытая
# адресом 127.0.0.1, открывалась в сеть на 0.0.0.0.
function Get-ExistingAddress {
    # Имена не $port и $bindHost: регистр в именах переменных PowerShell не
    # различается, и такие имена читались бы как параметры сценария.
    $own = @(Get-AsrOwnAutostart -Prefix $script:Prefix | Where-Object { $_.Prefix })
    $configPath = Join-Path $script:DataDir 'config.yaml'
    $foundPort = 0; $foundHost = ''
    foreach ($record in $own) {
        if (-not $foundPort -and $record.Port) { $foundPort = [int]$record.Port }
        if (-not $foundHost -and $record.BindHost) { $foundHost = $record.BindHost }
    }
    if (-not $foundPort) {
        $number = 0
        if ([int]::TryParse((Get-ConfigYamlValue -File $configPath -Key 'server_port'), [ref]$number) -and
            $number -ge 1 -and $number -le 65535) { $foundPort = $number }
    }
    if (-not $foundHost) { $foundHost = Get-ConfigYamlValue -File $configPath -Key 'server_host' }
    return [pscustomobject]@{ Port = $foundPort; BindHost = $foundHost }
}
$existing = Get-ExistingAddress
if (-not $script:ExplicitArgs.Contains('Port') -and $existing.Port) {
    $Port = $existing.Port
    Write-Debug2 "порт стоящей установки: $Port"
}
if (-not $script:ExplicitArgs.Contains('BindHost') -and $existing.BindHost) {
    $BindHost = $existing.BindHost
    Write-Debug2 "адрес стоящей установки: $BindHost"
}
if ($Port -lt 1 -or $Port -gt 65535) {
    Write-Err "Порт — число от 1 до 65535, а не $Port."
    exit 2
}

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

# ---------------------------------------------------------------------------
# Мастер установки
# ---------------------------------------------------------------------------
#
# Запускается, когда есть консоль и не задан -NoInteractive или -Yes.
# У каждого вопроса есть ответ по умолчанию, подобранный по обнаруженному
# железу, поэтому «Enter пять раз» даёт разумную установку.

function Invoke-InstallWizard {
    $accelLabel = switch ($hw.Accelerator) {
        'cuda' { 'видеокарта NVIDIA' }
        'rocm' { 'видеокарта AMD' }
        default { 'только процессор' }
    }

    Write-WizardStep 'Установка ASR Hub' `
        'Enter принимает предложенное значение — оно подобрано по вашему железу'
    Write-Host ("  Обнаружено: Windows, {0}, {1} ГБ памяти" -f $accelLabel, $hw.RamGb) `
        -ForegroundColor DarkGray

    # Что задано в командной строке — то мастер не переспрашивает и, главное,
    # не затирает. Раньше -Profile, -BindHost и -Engines молча пропадали:
    # пользователь писал -BindHost 127.0.0.1, а сервер выставлялся в сеть на
    # 0.0.0.0, потому что умолчание мастера было записано в коде числом. В
    # bash-двойнике это уже исправлено, здесь — нет.
    $explicit = $script:ExplicitArgs

    $profileOrder = @('light', 'cpu', 'standard', 'russian', 'full')
    $defaultProfile = if ($explicit.Contains('Profile') -and $script:Profile) {
        [Math]::Max(1, $profileOrder.IndexOf($script:Profile) + 1)
    } else {
        [Math]::Max(1, $profileOrder.IndexOf((Get-RecommendedProfile)) + 1)
    }

    $script:Profile = Select-WizardOption -Question 'Что установить?' -DefaultIndex $defaultProfile -Options @(
        @{ Value = 'light';    Label = 'Минимум — проверить, что всё работает';
           Note = '~1 ГБ. faster-whisper small. Годится, чтобы посмотреть интерфейс' }
        @{ Value = 'cpu';      Label = 'Сервер без видеокарты';
           Note = '~4 ГБ. int8-квантизация. Час записи обрабатывается за час-полтора' }
        @{ Value = 'standard'; Label = 'Стандартный набор';
           Note = '~8 ГБ. GigaAM v3 + faster-whisper. Лучший выбор для машины с GPU' }
        @{ Value = 'russian';  Label = 'Только русский язык';
           Note = '~3 ГБ. GigaAM v3 и Vosk. Ничего лишнего' }
        @{ Value = 'full';     Label = 'Всё сразу';
           Note = '60+ ГБ и час установки. Для сравнения моделей между собой' }
    )
    # Явно перечисленные движки профиль не переопределяет.
    if (-not $explicit.Contains('Engines')) { $script:Engines = $profileEngines[$script:Profile] }
    if (-not $explicit.Contains('Models'))  { $script:Models  = $profileModels[$script:Profile] }

    if ($hw.Accelerator -eq 'cpu' -and $script:Profile -in @('standard', 'full')) {
        Write-Warn 'Видеокарта не обнаружена: выбранные модели будут работать в 10–30 раз медленнее.'
        Write-Hint 'Профиль «cpu» подобран как раз для такой машины.'
        if (-not (Confirm-Action 'Оставить выбранный профиль?')) {
            $script:Profile = 'cpu'
            if (-not $explicit.Contains('Engines')) { $script:Engines = $profileEngines['cpu'] }
            if (-not $explicit.Contains('Models'))  { $script:Models  = $profileModels['cpu'] }
        }
    }

    $script:Prefix = Read-WizardValue -Question 'Каталог программы' -Default $script:Prefix
    $script:DataDir = Read-WizardValue -Question 'Каталог данных' -Default $script:DataDir `
        -Note 'Здесь будут веса моделей, загруженные файлы, результаты и база заданий.'

    # Порт, занятый сервером этой же установки, — свой: установщик
    # запускают повторно при работающем сервере, и он будет перезапущен.
    # Раньше мастер не давал его оставить и уводил на соседний.
    $wizardPrefix = $script:Prefix
    $suggestedPort = if ((Test-PortFree -Port $script:Port) -or
                         (Test-AsrPortOurs -Port $script:Port -Prefix $wizardPrefix)) { $script:Port }
                     else { Find-FreePort -Start ($script:Port + 1) }
    $script:Port = [int](Read-WizardValue -Question 'Порт сервера' -Default "$suggestedPort" -Validator {
        param($value)
        $number = 0
        if (-not [int]::TryParse($value, [ref]$number) -or $number -lt 1 -or $number -gt 65535) {
            Write-Warn 'Порт — число от 1 до 65535.'; return $false
        }
        if (-not (Test-PortFree -Port $number) -and -not (Test-AsrPortOurs -Port $number -Prefix $wizardPrefix)) {
            Write-Warn "Порт $number уже занят."; return $false
        }
        return $true
    }.GetNewClosure())

    # Умолчание — то, что стоит сейчас: заданное ключом или адрес стоящей
    # установки. Сервер, привязанный к адресу в локальной сети, получает
    # третий пункт, чтобы мастер не отнимал его привязку.
    $hostOptions = @(
        @{ Value = '127.0.0.1'; Label = 'Только эта машина';
           Note = 'Снаружи сервер не виден. Доступ — через туннель или прокси' }
        @{ Value = '0.0.0.0';   Label = 'Любой, кто дотянется по сети';
           Note = 'Обычный выбор для сервера. Оставьте включённой проверку ключей' }
    )
    $hostDefault = 2
    if ($script:BindHost -in @('127.0.0.1', 'localhost')) { $hostDefault = 1 }
    elseif ($script:BindHost -and $script:BindHost -notin @('0.0.0.0', '::', '*')) {
        $hostOptions += @{ Value = $script:BindHost; Label = "Только адрес $($script:BindHost) (как сейчас)";
                           Note = 'Сервер слушает один сетевой адрес этой машины' }
        $hostDefault = 3
    }
    $script:BindHost = Select-WizardOption -Question 'Кто сможет подключаться?' -DefaultIndex $hostDefault -Options $hostOptions

    $extrasDefault = if ($explicit.Contains('NoService') -and $script:NoService) { '' } else { '1' }
    $extras = Select-WizardMany -Question 'Что ещё включить?' -Default $extrasDefault -Options @(
        @{ Value = 'service';   Label = 'Автозапуск при входе в систему';
           Note = 'Служба через NSSM или задача планировщика' }
        @{ Value = 'alignment'; Label = 'Точные границы слов (WhisperX)';
           Note = 'Нужно для субтитров и дубляжа. MFA на Windows ставится сложнее' }
        @{ Value = 'diarization'; Label = 'Разделение по говорящим (pyannote)';
           Note = '+1 ГБ. Нужен токен Hugging Face и согласие с лицензией модели' }
    )
    # Обе ветки: без второй снятая галочка не возвращала автозапуск, а
    # отмеченная не отменяла заданный в командной строке -NoService.
    $script:NoService = ($extras -notcontains 'service')
    if ($extras -contains 'alignment') { $script:Alignment = 'whisperx' }
    # Диаризация ставится как обычный движок: файл требований у неё свой.
    if ($extras -contains 'diarization' -and $script:Engines -notlike '*diarization*') {
        $script:Engines = "$($script:Engines),diarization"
    }

    # Токен Hugging Face. Не спрашиваем, если он уже задан ключом или пришёл из
    # окружения: мастер не переспрашивает то, что ему сказали.
    #
    # Текст пояснения зависит от выбора выше. Для диаризации токен не
    # «желателен», а обязателен — pyannote без него не скачается, и узнать об
    # этом на седьмом шаге хуже, чем ответить на вопрос сейчас.
    if (-not $script:HfToken) {
        $tokenNote = if ($extras -contains 'diarization') {
            @'
Для разделения по говорящим он обязателен: pyannote без токена не
скачается. Возьмите на huggingface.co/settings/tokens (права «read») и примите
лицензию модели pyannote/speaker-diarization-community-1.
'@
        } else {
            @'
Нужен для моделей с ограниченным доступом. Сейчас вы такие не
выбрали, так что можно пропустить — Enter. Дописать потом:
install.ps1 -HfToken hf_… или строкой hf_token: в config.yaml.
'@
        }
        $script:HfToken = Read-WizardValue -Question 'Токен Hugging Face' -Default '' `
            -Note $tokenNote -Validator { param($value) Test-HfToken -Token $value }
    }

    $summary = [ordered]@{
        'Профиль'          = $script:Profile
        'Движки'           = $script:Engines
        'Модели'           = if ($script:Models) { $script:Models } else { 'не загружать' }
        'Каталог программы' = $script:Prefix
        'Каталог данных'   = $script:DataDir
        'Адрес'            = "http://$($script:BindHost):$($script:Port)"
        'Автозапуск'       = if ($script:NoService) { 'нет' } else { 'да' }
        'Выравнивание'     = if ($script:Alignment) { $script:Alignment } else { 'нет' }
        'Токен Hugging Face' = if ($script:HfToken) {
            "{0}… ({1} знаков)" -f $script:HfToken.Substring(0, 6), $script:HfToken.Length
        } else { 'не задан' }
        'Займёт на диске'  = "около $($profileDisk[$script:Profile]) ГБ"
    }
    Show-WizardSummary -Rows $summary

    if (-not (Confirm-Action 'Начинать установку?')) { Write-Info 'Отменено.'; exit 0 }
}

if (-not $NoInteractive -and ($Interactive -or (Test-Interactive))) {
    Invoke-InstallWizard
    # После мастера значения могли измениться — пересобираем производные.
    $venv = Join-Path $Prefix 'venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    $venvPip = Join-Path $venv 'Scripts\pip.exe'
}


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

# Число шагов — по тому, что действительно будет сделано. Раньше стояло «9»
# на любой случай, и полная установка доходила до «[11/9]».
$stepCount = 7
if ($Mode -eq 'docker') { $stepCount += 1 }
else {
    $stepCount += 3
    if (@('gigaam', 'faster_whisper', 'whisper', 'nemo', 'transformers', 'qwen3_asr') |
        Where-Object { $Engines -like "*$_*" }) { $stepCount += 1 }
    if ($Models -and -not $SkipModels) { $stepCount += 1 }
    if (-not $NoService) { $stepCount += 1 }
}
Set-StepTotal $stepCount -KeepIndex

# Автозапуск на Windows один на машину: служба «ASRHub» и задача «ASRHub
# Server». Вторая установка в свой каталог пересоздавала их под себя и молча
# отнимала автозапуск у первой. Проверяем до первого изменения на диске.
if (-not $NoService -and $Mode -eq 'native') {
    $foreignAutostart = @(Get-AsrAutostart | Where-Object {
        $_.Prefix -and -not (Test-AsrSamePath $_.Prefix $Prefix) -and
        (Test-Path -LiteralPath (Join-AsrPath $_.Prefix 'server'))
    })
    if ($foreignAutostart) {
        $first = $foreignAutostart[0]
        Write-Err "Автозапуск «$($first.Name)» уже обслуживает другую установку: $($first.Prefix)"
        Write-Hint "Переустановить её: install.ps1 -Prefix `"$($first.Prefix)`""
        Write-Hint 'Поставить вторую рядом: install.ps1 -Prefix <каталог> -DataDir <каталог> -NoService -Port <порт>'
        exit 2
    }
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
    # Invoke-Native: docker info пишет предупреждения в stderr, и на Windows
    # PowerShell 5.1 первое же из них обрывало установку исключением.
    $null = Invoke-Native -Command 'docker' -Arguments @('info')
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

if (-not (Test-PortFree -Port $Port) -and (Test-AsrPortOurs -Port $Port -Prefix $Prefix)) {
    # Порт держит сервер этой же установки: установщик запускают повторно,
    # чтобы доставить движок или починить окружение, а сервер работает.
    # Раньше установщик в этом случае переезжал на соседний порт (с -Yes —
    # молча), и служба оказывалась на 8081, а все клиенты — на 8080.
    Write-Ok "Порт $Port занят сервером этой же установки — он будет перезапущен."
} elseif (-not (Test-PortFree -Port $Port)) {
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

Write-Step 'Видеокарта'

# Win32_VideoController отвечает и без драйвера производителя, поэтому карта
# находится и на свежей системе — там, где раньше установщик считал, что её
# нет, и собирал всё под процессор.
$gpuBus = Get-GpuOnBus
if (-not $gpuBus.Vendor) {
    Write-Info 'Видеокарта не найдена — сервер будет считать на процессоре.'
} else {
    Write-Host ("  Видеокарта       {0}" -f $gpuBus.Name)
    Write-Host ("  Тип              {0}" -f $(if ($gpuBus.Discrete) { 'дискретная' } else { 'встроенная' }))
    Write-Host ("  Драйвер          {0}" -f $(if ($gpuBus.DriverIsGeneric) { 'стандартный адаптер Windows' } else { "версия $($gpuBus.DriverVersion)" }))

    if (-not $gpuBus.Discrete) {
        Write-Info 'Встроенная графика для расчётов не годится — ставить драйвер незачем.'
    } elseif ($gpuBus.Vendor -eq 'intel') {
        # То же, что и в Linux: движки ASR Hub пока не умеют считать на XPU.
        Write-Info 'Найдена Intel Arc, но движки распознавания её пока не используют.'
    } elseif (-not $gpuBus.DriverIsGeneric -and $hw.Accelerator -eq 'cuda') {
        Write-Ok 'Драйвер установлен и работает.'
    } elseif ($NoGpuDriver) {
        Write-Info 'Задан -NoGpuDriver: драйвер не трогаем.'
    } elseif ($DryRun) {
        Write-Info "[пробный запуск] Здесь был бы установлен драйвер $($gpuBus.Vendor)."
    } elseif ($gpuBus.DriverIsGeneric) {
        if (Install-GpuDriver -Vendor $gpuBus.Vendor) { $script:GpuRebootRequired = $true }
    } else {
        # Драйвер производителя стоит, но nvidia-smi не отвечает: чаще всего
        # это ноутбук с переключаемой графикой либо драйвер без CUDA.
        Write-Warn 'Драйвер установлен, но карта недоступна для расчётов.'
        Write-Hint 'Проверьте, что в панели управления NVIDIA карта не отключена, и что установлен полный драйвер, а не только видеодрайвер.'
    }

    # Колёса PyTorch выбираем под найденную карту, а не под ту, что видна
    # прямо сейчас: до перезагрузки nvidia-smi молчит, и обычная проверка
    # дала бы процессорный torch.
    if ($gpuBus.Vendor -eq 'nvidia' -and $gpuBus.Discrete) { $script:GpuTargetAccel = 'cuda' }
}

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

# Движки, которые ставятся прямо из репозитория (gigaam, tone, moonshine —
# строки «пакет @ git+https://…»), требуют git. Без него pip доходил до
# клонирования и падал, а установщик писал про движок «установка не
# удалась» без единого слова о причине.
if ($Mode -eq 'native' -and -not $Offline) {
    $needsGit = @()
    foreach ($engine in ($Engines -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $leaf = ($engine -replace '_', '-') + '.txt'
        $engineDir = Join-Path $RepoDir 'requirements\engines'
        foreach ($file in @((Join-Path $engineDir $leaf), (Join-Path (Join-Path $engineDir 'no-deps') $leaf))) {
            if ((Test-Path -LiteralPath $file) -and (Select-String -LiteralPath $file -Pattern 'git\+' -Quiet)) {
                $needsGit += $engine
                break
            }
        }
    }
    if ($needsGit -and -not (Test-CommandExists 'git')) {
        Write-Warn "Движки $($needsGit -join ', ') ставятся из репозитория, а git не найден."
        if ((Test-CommandExists 'winget') -and (Confirm-Action 'Установить git через winget?')) {
            try {
                Invoke-Checked -Command 'winget' -Arguments @('install', '--id', 'Git.Git', '-e',
                    '--accept-package-agreements', '--accept-source-agreements',
                    '--disable-interactivity') -IgnoreExitCode | Out-Null
                # Новый PATH придёт только в новую консоль — этой добавляем сами.
                foreach ($gitDir in @((Join-AsrPath $env:ProgramFiles 'Git\cmd'),
                                      (Join-AsrPath $env:LOCALAPPDATA 'Programs\Git\cmd'))) {
                    if ($gitDir -and (Test-Path -LiteralPath (Join-Path $gitDir 'git.exe'))) {
                        $env:Path = "$env:Path;$gitDir"
                    }
                }
            } catch { Write-Warn 'Автоматическая установка git не удалась.' }
        }
        if (Test-CommandExists 'git') { Write-Ok 'git установлен' }
        elseif (-not (Get-DryRun)) {
            Write-Warn "Без git движки $($needsGit -join ', ') не установятся — остальные поставятся."
            Write-Hint 'Поставьте git и повторите установку: winget install Git.Git'
        }
    } elseif ($needsGit) {
        Write-Ok 'git найден'
    }
}

# ---------------------------------------------------------------------------

Write-Step 'Создание каталогов'

foreach ($dir in @($Prefix, $DataDir,
                   (Join-Path $DataDir 'uploads'), (Join-Path $DataDir 'results'),
                   (Join-Path $DataDir 'models'), (Join-Path $DataDir 'logs'),
                   (Join-Path $DataDir 'tmp'))) {
    if (-not (Test-Path $dir)) {
        if (-not (Get-DryRun)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        # GetNewClosure обязателен: блок скрипта связывается с переменной
        # поздно, и к моменту отката $dir равнялся последнему значению
        # цикла. Откат пять раз удалял data\tmp, а каталог программы с venv
        # оставался на диске — при том что скрипт писал «система возвращена
        # в исходное состояние».
        $captured = $dir
        Add-Rollback ({ Remove-Item -LiteralPath $captured -Recurse -Force `
                                    -ErrorAction SilentlyContinue }).GetNewClosure() "удалить $captured"
    }
}
# Каталог данных — только для администраторов, SYSTEM и того, кто ставит:
# %ProgramData% по умолчанию читают все пользователи машины, и api-key.txt,
# config.yaml с токеном и база со всеми расшифровками были открыты любому,
# кто вошёл в систему. Учётной записи службы права выдаёт service.ps1.
if ($isAdmin) {
    if (Set-AsrPrivateAcl -Path $DataDir) { Write-Debug2 "каталог данных закрыт: $DataDir" }
}
Write-Ok 'Каталоги готовы'

# ---------------------------------------------------------------------------

Write-Step 'Копирование файлов приложения'

# Запуск из уже установленной копии — обычное дело: скрипт сам печатает
# путь вида "C:\Program Files\ASRHub\scripts\update.ps1". Без проверки
# цикл удалял каталог и тут же копировал его сам в себя: установка
# оставалась без server, а откат ничего не возвращал.
$srcReal = [System.IO.Path]::GetFullPath($RepoDir)
$dstReal = [System.IO.Path]::GetFullPath($Prefix)
if ($srcReal -eq $dstReal) {
    Write-Info 'Источник совпадает с установкой — файлы уже на месте, копирование пропущено.'
} elseif (-not (Get-DryRun)) {
    # examples: интерфейс «Диктовки» ссылается на stream_microphone.py.
    foreach ($item in @('server', 'scripts', 'config', 'requirements', 'docker', 'examples', 'VERSION', 'README.md')) {
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
    $envLines = @("ASRHUB_PORT=$Port", "ASRHUB_HOST=$BindHost",
                  "ASRHUB_DATA=$DataDir", "ASRHUB_PROFILE=$Profile",
                  "ASRHUB_ACCEL=$($hw.Accelerator)")
    # Список движков — только заданный явно, как в install.sh. Иначе в .env
    # попадал набор профиля из установщика, а он у образа свой: без vad,
    # postprocess и diarization, зато в «full» с nemo и qwen3_asr, которых в
    # образе нарочно нет. Пустой ASRHUB_ENGINES — образ берёт набор профиля.
    if ($script:ExplicitArgs.Contains('Engines')) { $envLines += "ASRHUB_ENGINES=$Engines" }
    # Токен — отдельной строкой и только если он есть: пустое значение в .env
    # перекрыло бы токен из окружения контейнера.
    if ($HfToken) { $envLines += "HF_TOKEN=$HfToken" }
    if (Get-DryRun) {
        Write-Host "[пробный запуск] запись $envFile ($($envLines.Count) строк)" -ForegroundColor Yellow
        Write-Host '[пробный запуск] docker compose build && docker compose up -d' -ForegroundColor Yellow
    } else {
        # Без метки BOM: Set-Content -Encoding UTF8 в Windows PowerShell 5.1
        # её ставит, и первая строка читалась как ключ «\ufeffASRHUB_PORT» —
        # контейнер публиковался на порту по умолчанию.
        [System.IO.File]::WriteAllLines($envFile, [string[]]$envLines, [System.Text.UTF8Encoding]::new($false))
        # В .env — токен Hugging Face, а каталог программы в Program Files
        # читают все пользователи машины.
        $null = Set-AsrPrivateAcl -Path $envFile
        if ($HfToken) {
            Write-Ok ("Токен Hugging Face записан ({0}…, {1} символов)" -f `
                $HfToken.Substring(0, 6), $HfToken.Length)
        }
        # Файл-надстройка для видеокарты подключается так же, как при
        # обновлении: раньше его подключал только update.ps1, и контейнер
        # после установки работал на процессоре, а после обновления — на карте.
        $compose = Get-AsrCompose -Prefix $Prefix
        Push-Location -LiteralPath $compose.Directory
        try {
            Invoke-WithRetry -Attempts 2 -Description 'сборка образа' -Action {
                Invoke-Checked -Command $compose.Command -Arguments ($compose.Arguments + @('build'))
            } | Out-Null
            Invoke-Checked -Command $compose.Command -Arguments ($compose.Arguments + @('up', '-d')) | Out-Null
        } finally { Pop-Location }
    }
    Write-Ok 'Контейнер запущен'

} else {
    Write-Step 'Виртуальное окружение Python'

    # -Force советуют для починки окружения, а при той же версии Python оно
    # раньше переиспользовалось как есть, и сломанный пакет оставался на
    # месте. Прежнее окружение откладывается до конца установки: сорвётся
    # новая сборка — откат вернёт его на место.
    $venvOld = "$venv.before-force"
    $venvMoved = $false
    if ($Force -and (Test-Path $venvPython)) {
        Write-Info 'Ключ -Force: окружение Python собирается заново.'
        Write-Hint "Прежнее отложено в $venvOld и удалится в конце установки."
        if (-not (Get-DryRun)) {
            # Работающий сервер держит python.exe и загруженные .pyd, и без
            # остановки перенос каталога падает с «отказано в доступе».
            # Служба поднимется снова на шаге «Настройка автозапуска».
            try { & (Join-Path $PSScriptRoot 'service.ps1') -Action stop -Prefix $Prefix -DataDir $DataDir | Out-Null } catch { }
            if (Test-Path $venvOld) { Remove-Item $venvOld -Recurse -Force }
            try { Move-Item -Path $venv -Destination $venvOld }
            catch {
                throw ("Окружение $venv занято работающим процессом — видимо, сервер " +
                       "запущен вручную. Остановите его и повторите установку.")
            }
            $venvMoved = $true
            Add-Rollback ({
                if (Test-Path $venv) { Remove-Item $venv -Recurse -Force -ErrorAction SilentlyContinue }
                Move-Item -Path $venvOld -Destination $venv
            }.GetNewClosure()) 'вернуть прежнее окружение'
        }
    }
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
        # Ускоритель берём тот, под который собираем: после установки драйвера
        # до перезагрузки nvidia-smi молчит, и обычная проверка дала бы
        # процессорный torch — с ним и после перезагрузки считал бы процессор.
        $torchAccel = if ($script:GpuTargetAccel) { $script:GpuTargetAccel } else { $hw.Accelerator }
        Write-Step "Установка PyTorch для ускорителя «$torchAccel»"
        if ($torchAccel -ne $hw.Accelerator) {
            Write-Info 'Карта пока не отвечает, но драйвер установлен — берём колёса под неё.'
        }
        $indexUrl = Get-TorchIndexUrl -Accelerator $torchAccel -CudaVersion $hw.CudaVersion
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
    # В пробном запуске файлы не копируются — списки требований читаем из
    # дистрибутива, иначе пробный запуск на чистой машине сообщал «нет файла
    # зависимостей» про каждый движок.
    $requirementsRoot = if (Get-DryRun) { $RepoDir } else { $Prefix }
    foreach ($engine in ($Engines -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        $req = Join-Path $requirementsRoot ("requirements\engines\" + ($engine -replace '_', '-') + '.txt')
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
                # Не просто «pip install -r файл»: рядом лежат спутники
                # no-deps и optional, без которых часть движков ставится
                # неполностью, а GigaAM — не ставится вовсе (его пакет
                # только в no-deps\gigaam.txt). Раньше их здесь не знали,
                # и скрипт печатал «установлен» про движок, которого нет.
                Install-EngineRequirements -Pip $venvPip -Requirements $req -PipFlags $pipFlags
            } | Out-Null
            Write-Ok "  $engine установлен"
        } catch {
            $failed += $engine
            Write-Warn "  $engine — установка не удалась, сервер запустится без него"
        }
    }

    # Версии, которые мы выбрали сами вопреки требованиям чужих пакетов.
    # Ставится последним и с --no-deps — иначе смысла нет.
    Install-Overrides -Pip $venvPip -RequirementsDir (Join-Path $Prefix 'requirements')
    # Установка поверх прежней оставляет её окружение — вместе с тем, что
    # прежние версии ставили и потом перестали.
    Remove-RetiredPackages -Pip $venvPip -RequirementsDir (Join-Path $Prefix 'requirements')
}

# ---------------------------------------------------------------------------

Write-Step 'Создание конфигурации'

$configFile = Join-Path $DataDir 'config.yaml'
# -Force конфигурацию не трогает: его советуют для починки окружения, а он
# стирал ключи интеграций, токен и настройки. Заменить её шаблоном — отдельный
# ключ -ResetConfig, и прежний файл остаётся рядом копией.
if ((Test-Path $configFile) -and -not $ResetConfig) {
    Write-Info 'Конфигурация уже существует — оставляем без изменений.'
    if ($Force) { Write-Hint '-Force конфигурацию не трогает. Заменить её шаблоном — ключ -ResetConfig.' }
} elseif (-not (Get-DryRun)) {
    $configBackup = ''
    if (Test-Path $configFile) {
        $configBackup = "$configFile.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
        Copy-Item $configFile $configBackup -Force
        Write-Warn 'Ключ -ResetConfig: config.yaml заменяется шаблоном.'
        Write-Hint "Прежний файл: $configBackup"
    }
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
    # Откат после -ResetConfig возвращает прежний файл, а не удаляет его:
    # раньше неудачная установка с этим ключом оставляла машину вовсе без
    # config.yaml — без ключей интеграций, токена и настроек, — хотя копия
    # лежала рядом.
    if ($configBackup) {
        Add-Rollback ({ Copy-Item -LiteralPath $configBackup -Destination $configFile -Force }).GetNewClosure() `
            'вернуть прежний config.yaml'
    } else {
        Add-Rollback ({ Remove-Item -LiteralPath $configFile -Force -ErrorAction SilentlyContinue }).GetNewClosure() `
            'удалить config.yaml'
    }
    Write-Ok "Конфигурация: $configFile"
}

# Токен Hugging Face кладём в config.yaml, а не в переменную окружения: её
# пришлось бы прописывать службе, задаче планировщика и своей консоли по
# отдельности, а конфигурацию читают все три. Строку заменяем, а не
# дописываем: два ключа hf_token в YAML — это молча выигравший последний, то
# есть потерянный только что введённый токен.
if ($HfToken -and $Mode -eq 'native') {
    if (Get-DryRun) {
        Write-Info "[пробный запуск] hf_token записан бы в $configFile"
    } elseif (Test-Path $configFile) {
        $kept = @(Get-Content -Path $configFile -Encoding UTF8 |
                  Where-Object { $_ -notmatch '^hf_token:' })
        ($kept + ('hf_token: "{0}"' -f $HfToken)) |
            Set-Content -Path $configFile -Encoding UTF8
        Write-Ok ("Токен Hugging Face записан в конфигурацию ({0}…, {1} символов)" -f `
            $HfToken.Substring(0, 6), $HfToken.Length)
    } else {
        Write-Warn 'Файл конфигурации не найден — токен Hugging Face не записан.'
        Write-Hint "Дописать вручную: строка hf_token: в $configFile"
    }
}

# Пример со всеми параметрами. Раньше вывод шёл через канал в cp1251 и
# обрывался на первом «×» (UnicodeEncodeError), а ошибку глотал пустой
# catch: config.example.yaml оставался пустым, и никто об этом не знал.
if ($Mode -eq 'native' -and -not (Get-DryRun) -and (Test-Path $venvPython)) {
    Push-Location -LiteralPath (Join-Path $Prefix 'server')
    try {
        $example = @(Invoke-Native -Command $venvPython -Arguments @('-m', 'asrhub', '--print-config'))
        $exampleCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($exampleCode -eq 0 -and $example.Count -gt 0) {
        [System.IO.File]::WriteAllLines((Join-Path $DataDir 'config.example.yaml'), [string[]]$example,
            [System.Text.UTF8Encoding]::new($false))
    } else {
        Write-Warn "Пример конфигурации config.example.yaml не создан (код $exampleCode)."
    }
}

# ---------------------------------------------------------------------------

if ($Models -and -not $SkipModels -and $Mode -eq 'native') {
    Write-Step 'Загрузка моделей'
    foreach ($model in ($Models -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
        Write-Info "Модель: $model"
        try {
            # Сценарий из дистрибутива, а не из каталога установки: в пробном
            # запуске файлы не копируются, и там лежит прежняя версия (до
            # 3.1.12 она перезагружала модуль и сбрасывала пробный запуск).
            $global:LASTEXITCODE = 0
            & (Join-Path $PSScriptRoot 'models.ps1') -Action download -Model $model `
                -Prefix $Prefix -DataDir $DataDir
            if ($LASTEXITCODE -ne 0) { throw "models.ps1: код $LASTEXITCODE" }
        } catch {
            Write-Warn "  не удалось загрузить $model"
            Write-Hint "  Повторить: powershell -File `"$Prefix\scripts\models.ps1`" -Action download -Model $model"
        }
    }
}

# ---------------------------------------------------------------------------

$serviceCreated = $false
if (-not $NoService -and $Mode -eq 'native') {
    Write-Step 'Настройка автозапуска'
    try {
        & (Join-Path $PSScriptRoot 'service.ps1') -Action install `
            -Prefix $Prefix -DataDir $DataDir -Port $Port -BindHost $BindHost
        # Код возврата здесь ничего не говорит — его оставляет последняя
        # внешняя программа внутри; спрашиваем сам автозапуск.
        $serviceCreated = @(Get-AsrAutostart | Where-Object {
            $_.Prefix -and (Test-AsrSamePath $_.Prefix $Prefix) }).Count -gt 0
    } catch {
        Write-Warn 'Служба не создана.'
        Write-Hint "Запускать вручную: `"$venvPython`" -m asrhub --port $Port"
    }
}

# Брандмауэр. Сервер, открытый в сеть (0.0.0.0 или адрес в сети), без
# правила из сети не виден: Windows молча отбрасывает входящие. Раньше
# правила не создавал никто, а doctor.ps1 предупреждал о нём всегда — даже у
# сервера на 127.0.0.1, которому правило не нужно вовсе. Правило — только
# для частных и доменных сетей; описание связывает его с установкой, чтобы
# удаление убрало своё и не тронуло чужое.
if ($Mode -eq 'native' -and $BindHost -notin @('127.0.0.1', 'localhost', '::1')) {
    $ruleName = "ASR Hub ($Port)"
    $ruleDescription = "ASR Hub: $Prefix"
    $ruleCommand = "New-NetFirewallRule -DisplayName '$ruleName' -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain,Private"
    if (-not $isAdmin -or -not (Get-Command New-NetFirewallRule -ErrorAction SilentlyContinue)) {
        Write-Hint 'Чтобы сервер был виден из сети, разрешите порт в брандмауэре (от администратора):'
        Write-Hint "  $ruleCommand"
    } elseif (Get-DryRun) {
        Write-Host "[пробный запуск] правило брандмауэра «$ruleName»" -ForegroundColor Yellow
    } else {
        try {
            Get-NetFirewallRule -DisplayName 'ASR Hub*' -ErrorAction SilentlyContinue |
                Where-Object { "$($_.Description)" -eq $ruleDescription } |
                Remove-NetFirewallRule -ErrorAction SilentlyContinue
            New-NetFirewallRule -DisplayName $ruleName -Description $ruleDescription -Direction Inbound `
                -Protocol TCP -LocalPort $Port -Action Allow -Profile Domain, Private -ErrorAction Stop | Out-Null
            Add-Rollback ({ Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue |
                Where-Object { "$($_.Description)" -eq $ruleDescription } |
                Remove-NetFirewallRule -ErrorAction SilentlyContinue }).GetNewClosure() "удалить правило $ruleName"
            Write-Ok "Брандмауэр: входящие на порт $Port разрешены (частные и доменные сети)"
        } catch {
            Write-Warn "Правило брандмауэра не создано: $($_.Exception.Message)"
            Write-Hint "  $ruleCommand"
        }
    }
}

# ---------------------------------------------------------------------------

Write-Step 'Проверка установки'

if (Get-DryRun) { Write-Ok 'Пробный запуск завершён — изменений не вносилось.'; exit 0 }

# Чем сервер будет считать — до того, как стучаться в порт. Порт ответит и на
# машине, где распознавание не работает вовсе: /api/health видеокарту не
# спрашивает, а первое задание спросит.
if (Test-Path $venvPython) {
    Test-GpuRuntime -Python $venvPython -DataDir $DataDir `
        -CodeDir (Join-Path $Prefix 'server') | Out-Null
}

# Сервер, привязанный к одному адресу в сети, на 127.0.0.1 не отвечает
# вовсе — проверяем по тому адресу, к которому он привязан.
$probeHost = if ($BindHost -and $BindHost -notin @('0.0.0.0', '::', '*', 'localhost', '127.0.0.1')) {
    if ($BindHost -match ':' -and -not $BindHost.StartsWith('[')) { "[$BindHost]" } else { $BindHost }
} else { '127.0.0.1' }
$probe = Wait-AsrHealth -Port $Port -HostName $probeHost -Seconds 40
if ($probe.Status -ge 200 -and $probe.Status -lt 300) {
    Write-Ok "Сервер отвечает на http://${probeHost}:$Port"
} elseif ($probe.Status -gt 0) {
    Write-Warn "Сервер отвечает, но сообщает о неисправности (код $($probe.Status))."
    Write-Hint "Подробности: powershell -File `"$Prefix\scripts\doctor.ps1`""
} else {
    Write-Warn 'Сервер пока не отвечает.'
    Write-Hint "Состояние службы: powershell -File `"$Prefix\scripts\service.ps1`" -Action status"
}

Clear-Rollback
# Метка установки: по ней сценарии обслуживания из этого каталога находят
# свои каталог данных и автозапуск, а не установку в стандартном каталоге.
$markerService = ''
if ($serviceCreated) {
    $ownRecords = @(Get-AsrOwnAutostart -Prefix $Prefix | Where-Object { $_.Prefix })
    $markerService = if ($ownRecords) { $ownRecords[0].Name }
                     elseif ($isAdmin -and (Get-Command nssm -ErrorAction SilentlyContinue)) { Get-AsrServiceName }
                     else { Get-AsrTaskName }
}
$null = Write-InstallMarker -Prefix $Prefix -DataDir $DataDir -Service $markerService -Mode $Mode
# Новое окружение собрано и сервер проверен — отложенное прежнее больше не нужно.
if ($Mode -eq 'native' -and $venvMoved -and (Test-Path $venvOld)) {
    Remove-Item $venvOld -Recurse -Force -ErrorAction SilentlyContinue
}

$apiKeyFile = Join-Path $DataDir 'api-key.txt'
$apiKey = if (Test-Path $apiKeyFile) { (Get-Content $apiKeyFile -Raw).Trim() } else { '' }

Write-Host ''
Write-Host 'Установка завершена' -ForegroundColor Green
Write-Host ''
Write-Host "  Веб-интерфейс     http://${probeHost}:$Port"
Write-Host "  Справочник API    http://${probeHost}:$Port/api/reference"
if ($apiKey) { Write-Host "  Ключ доступа      $apiKey" -ForegroundColor White }
Write-Host "  Каталог программы $Prefix"
Write-Host "  Каталог данных    $DataDir"
Write-Host "  Журнал установки  $logFile"
Write-Host ''
Write-Host 'Что дальше' -ForegroundColor White
Write-Host "  Проверить окружение  powershell -File `"$Prefix\scripts\doctor.ps1`""
Write-Host "  Управлять моделями   powershell -File `"$Prefix\scripts\models.ps1`" -Action list"
Write-Host "  Управлять службой    powershell -File `"$Prefix\scripts\service.ps1`" -Action status"
Write-Host "  Обновить             powershell -File `"$Prefix\scripts\update.ps1`" -Source <каталог новой версии>"
Write-Host "  Удалить              powershell -File `"$Prefix\scripts\uninstall.ps1`""
if ($failed) {
    Write-Host ''
    Write-Warn "Не установились движки: $($failed -join ', ')"
}
# Про перезагрузку — последней строкой: именно её и надо унести с собой.
if ($script:GpuRebootRequired) {
    Write-Host ''
    Write-Host 'Нужна перезагрузка' -ForegroundColor Yellow
    Write-Host '  Драйвер видеокарты установлен, но вступит в силу после перезагрузки.'
    Write-Host '  До неё сервер считает на процессоре.'
    Write-Host "  После перезагрузки проверьте: powershell -File `"$Prefix\scripts\doctor.ps1`""
}
Write-Host ''
