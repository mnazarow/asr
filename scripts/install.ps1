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
    [switch]$NoGpuDriver,
    [ValidateSet('', 'none', 'mfa', 'whisperx')][string]$Alignment = '',
    [switch]$Interactive,
    [switch]$NoInteractive,
    [switch]$Offline,
    [switch]$Force,
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

    $suggestedPort = if (Test-PortFree -Port $script:Port) { $script:Port }
                     else { Find-FreePort -Start ($script:Port + 1) }
    $script:Port = [int](Read-WizardValue -Question 'Порт сервера' -Default "$suggestedPort" -Validator {
        param($value)
        $number = 0
        if (-not [int]::TryParse($value, [ref]$number) -or $number -lt 1 -or $number -gt 65535) {
            Write-Warn 'Порт — число от 1 до 65535.'; return $false
        }
        if (-not (Test-PortFree -Port $number)) {
            Write-Warn "Порт $number уже занят."; return $false
        }
        return $true
    })

    $hostDefault = if ($explicit.Contains('BindHost') -and $script:BindHost -eq '127.0.0.1') { 1 } else { 2 }
    $script:BindHost = Select-WizardOption -Question 'Кто сможет подключаться?' -DefaultIndex $hostDefault -Options @(
        @{ Value = '127.0.0.1'; Label = 'Только эта машина';
           Note = 'Снаружи сервер не виден. Доступ — через туннель или прокси' }
        @{ Value = '0.0.0.0';   Label = 'Любой, кто дотянется по сети';
           Note = 'Обычный выбор для сервера. Оставьте включённой проверку ключей' }
    )

    $extrasDefault = if ($explicit.Contains('NoService') -and $script:NoService) { '' } else { '1' }
    $extras = Select-WizardMany -Question 'Что ещё включить?' -Default $extrasDefault -Options @(
        @{ Value = 'service';   Label = 'Автозапуск при входе в систему';
           Note = 'Служба через NSSM или задача планировщика' }
        @{ Value = 'alignment'; Label = 'Точные границы слов (WhisperX)';
           Note = 'Нужно для субтитров и дубляжа. MFA на Windows ставится сложнее' }
    )
    # Обе ветки: без второй снятая галочка не возвращала автозапуск, а
    # отмеченная не отменяла заданный в командной строке -NoService.
    $script:NoService = ($extras -notcontains 'service')
    if ($extras -contains 'alignment') { $script:Alignment = 'whisperx' }

    $summary = [ordered]@{
        'Профиль'          = $script:Profile
        'Движки'           = $script:Engines
        'Модели'           = if ($script:Models) { $script:Models } else { 'не загружать' }
        'Каталог программы' = $script:Prefix
        'Каталог данных'   = $script:DataDir
        'Адрес'            = "http://$($script:BindHost):$($script:Port)"
        'Автозапуск'       = if ($script:NoService) { 'нет' } else { 'да' }
        'Выравнивание'     = if ($script:Alignment) { $script:Alignment } else { 'нет' }
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
# Про перезагрузку — последней строкой: именно её и надо унести с собой.
if ($script:GpuRebootRequired) {
    Write-Host ''
    Write-Host 'Нужна перезагрузка' -ForegroundColor Yellow
    Write-Host '  Драйвер видеокарты установлен, но вступит в силу после перезагрузки.'
    Write-Host '  До неё сервер считает на процессоре.'
    Write-Host "  После перезагрузки проверьте: powershell -File `"$Prefix\scripts\doctor.ps1`""
}
Write-Host ''
