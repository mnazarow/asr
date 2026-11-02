<#
.SYNOPSIS
    Управление автозапуском ASR Hub на Windows.
.DESCRIPTION
    От администратора создаётся служба Windows (через NSSM, если он есть,
    иначе задача планировщика). Без прав администратора создаётся задача
    планировщика, запускающаяся при входе этого пользователя.

    Установка, к которой относится команда, определяется так же, как в
    остальных сценариях: ключ -Prefix; иначе та, из которой запущен
    сценарий; иначе единственная известная на машине. Автозапуск чужой
    установки сценарий не трогает.
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\service.ps1 -Action install -Prefix "C:\Program Files\ASRHub"
    powershell -ExecutionPolicy Bypass -File scripts\service.ps1 -Action status
    powershell -ExecutionPolicy Bypass -File scripts\service.ps1 -Action logs -Lines 200
    powershell -ExecutionPolicy Bypass -File scripts\service.ps1 -Action backup
    powershell -ExecutionPolicy Bypass -File scripts\service.ps1 -Action restore -From D:\ASRData\backups\asrhub-20250101.db
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs', 'backup', 'restore')]
    [string]$Action = 'status',
    [string]$Prefix = '',
    [string]$DataDir = '',
    [ValidateRange(0, 65535)][int]$Port = 0,
    [string]$BindHost = '',
    [int]$Lines = 100,
    [switch]$Follow,
    # Файл копии для restore: база *.db или архив *.asrhub.tar.gz.
    [string]$From = '',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# Из install.ps1, update.ps1 и uninstall.ps1 этот скрипт зовут в том же
# процессе, и модуль у них уже загружен. Повторный Import-Module -Force
# пересоздавал его и сбрасывал у ВЫЗЫВАЮЩЕГО пробный запуск, список отката и
# журнал: `uninstall.ps1 -Purge -DryRun` после остановки службы удалял
# каталоги программы и данных по-настоящему, а `update.ps1 -DryRun` менял
# файлы и ставил пакеты. Грузим модуль, только если его ещё нет.
if (-not (Get-Module -Name Common)) {
    Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1')
}
# Пробный запуск — только на время этого сценария: модуль общий с
# вызывающим, и включённый здесь флаг оставался бы включённым у него.
$dryRunBefore = Get-DryRun
if ($DryRun) { Set-DryRun $true }

$serviceName = Get-AsrServiceName
$taskName = Get-AsrTaskName

try {

$resolved = Resolve-AsrInstallation -ScriptDir $PSScriptRoot -Prefix $Prefix -DataDir $DataDir
if (-not $resolved.Prefix -and @($resolved.Candidates).Count -gt 0 -and $Action -ne 'status') {
    Show-AmbiguousInstallation $resolved.Candidates 'service.ps1'
    exit 2
}
$Prefix = $resolved.Prefix
$DataDir = $resolved.DataDir
if (-not $DataDir) {
    $DataDir = if (Test-Administrator) { Join-AsrPath $env:ProgramData 'ASRHub' }
               else { Join-AsrPath $env:LOCALAPPDATA 'ASRHub\data' }
}

# Автозапуск ЭТОЙ установки. Без известного каталога программы — любой,
# какой есть: так чистится автозапуск, чей каталог уже удалён руками.
$allAutostart = @(Get-AsrAutostart)
$ownAutostart = if ($Prefix) { @($resolved.Autostart) } else { $allAutostart }
$foreignAutostart = @($allAutostart | Where-Object {
    $_.Prefix -and $Prefix -and -not (Test-AsrSamePath $_.Prefix $Prefix) -and
    (Test-Path -LiteralPath (Join-AsrPath $_.Prefix 'server'))
})
$ownService = @($ownAutostart | Where-Object { $_.Kind -eq 'service' }).Count -gt 0
$ownTask = @($ownAutostart | Where-Object { $_.Kind -eq 'task' }).Count -gt 0

$python = if ($Prefix) { Join-Path $Prefix 'venv\Scripts\python.exe' } else { '' }
$pythonw = if ($Prefix) { Join-Path $Prefix 'venv\Scripts\pythonw.exe' } else { '' }
$workDir = if ($Prefix) { Join-Path $Prefix 'server' } else { '' }
$logDir = Join-Path $DataDir 'logs'
$configFile = Join-Path $DataDir 'config.yaml'

function Show-ForeignAutostart {
    foreach ($record in $foreignAutostart) {
        $what = if ($record.Kind -eq 'service') { "Служба «$($record.Name)»" } else { "Задача «$($record.Name)»" }
        Write-Info "$what принадлежит другой установке ($($record.Prefix)) — её не трогаем."
    }
}

function Get-InstallAddress {
    <#
      Адрес и порт для новой записи автозапуска: заданные ключами, иначе
      прежние этой же установки, иначе из config.yaml, иначе умолчания.
      Раньше брались умолчания скрипта — 0.0.0.0:8080, — и повторная
      установка переносила сервер с настроенного порта и открывала в сеть
      сервер, который слушал только 127.0.0.1.
    #>
    # Имена не $port и $bindHost: регистр в именах переменных PowerShell не
    # различается, и такие имена совпадали бы с параметрами сценария.
    $address = Get-AsrServerAddress -Prefix $Prefix -DataDir $DataDir -Autostart $ownAutostart
    $chosenPort = if ($Port -gt 0) { $Port } else { $address.Port }
    $chosenHost = if ($BindHost) { $BindHost }
                  elseif ($address.BindHost) { $address.BindHost }
                  else { '0.0.0.0' }
    return [pscustomobject]@{ Port = $chosenPort; BindHost = $chosenHost }
}

function Get-VenvBaseHome {
    <#
      Каталог базового интерпретатора, на который ссылается venv (строка
      «home = …» в pyvenv.cfg). python.exe в venv — только переходник:
      библиотеки и python3xx.dll он берёт оттуда.
    #>
    $cfg = Join-Path $Prefix 'venv\pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg -PathType Leaf)) { return '' }
    foreach ($line in @(Get-Content -LiteralPath $cfg -ErrorAction SilentlyContinue)) {
        if ("$line" -match '^\s*home\s*=\s*(.+?)\s*$') { return $Matches[1] }
    }
    return ''
}

function Grant-ServiceAccess {
    <#
      Права учётной записи службы: изменять каталог данных, читать и
      запускать программу. Без них служба от своей учётной записи не
      поднимется, поэтому при отказе остаёмся на LocalSystem.
    #>
    param([string]$Account)
    if (-not (Test-CommandExists 'icacls')) { return $false }
    $null = Invoke-Native -Command 'icacls' -Arguments @($DataDir, '/grant', "${Account}:(OI)(CI)M", '/Q')
    if ($LASTEXITCODE -ne 0) { return $false }
    $null = Invoke-Native -Command 'icacls' -Arguments @($Prefix, '/grant', "${Account}:(OI)(CI)RX", '/Q')
    if ($LASTEXITCODE -ne 0) { return $false }
    # Базовый Python. Установщик python.org по умолчанию ставит его «для
    # текущего пользователя» — в C:\Users\<имя>\AppData\Local\Programs\Python,
    # куда у виртуальной записи службы доступа нет. Права выдавались только
    # на каталог программы, и python.exe из venv падал при каждом запуске, а
    # NSSM перезапускал его раз в 10 секунд. Каталоги Program Files и
    # Windows и так читаемы всеми — их не трогаем.
    $baseHome = Get-VenvBaseHome
    if ($baseHome -and (Test-Path -LiteralPath $baseHome -PathType Container) -and
        -not (Test-AsrPathInside -Path $baseHome -Parent $Prefix) -and
        -not (Test-AsrPathInside -Path $baseHome -Parent $env:ProgramFiles) -and
        -not (Test-AsrPathInside -Path $baseHome -Parent $env:SystemRoot)) {
        if ($baseHome -match '(?i)\\WindowsApps\\') {
            # Python из Microsoft Store — псевдоним приложения: службе он
            # недоступен ни с какими правами.
            Write-Warn "Python из Microsoft Store ($baseHome) службе недоступен."
            Write-Hint 'Поставьте Python с python.org «для всех пользователей» и переустановите ASR Hub.'
            return $false
        }
        $null = Invoke-Native -Command 'icacls' -Arguments @($baseHome, '/grant', "${Account}:(OI)(CI)RX", '/Q')
        if ($LASTEXITCODE -ne 0) { return $false }
        Write-Info "Учётной записи службы открыто чтение базового Python: $baseHome"
    }
    return $true
}

function Get-ServicePath {
    <#
      PATH для службы. Службы получают окружение services.exe, снятое при
      загрузке Windows, и ffmpeg, только что поставленный установщиком через
      winget, служба увидела бы лишь после перезагрузки: сервер принимал
      только WAV, а doctor.ps1 из консоли говорил «ffmpeg найден». Поэтому
      PATH машины берём свежим из реестра и добавляем каталог ffmpeg.
    #>
    $parts = @(([Environment]::GetEnvironmentVariable('Path', 'Machine') -split ';') | Where-Object { $_ })
    $ffmpeg = Get-Command ffmpeg -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $ffmpeg) {
        foreach ($dir in $parts) {
            if (Test-Path -LiteralPath (Join-Path $dir 'ffmpeg.exe') -PathType Leaf) { return ($parts -join ';') }
        }
        Write-Warn 'ffmpeg не найден — служба будет принимать только WAV.'
        return ($parts -join ';')
    }
    $ffmpegDir = Split-Path -Parent $ffmpeg.Source
    if (-not ($parts | Where-Object { Test-AsrSamePath $_ $ffmpegDir })) {
        $parts += $ffmpegDir
        if ($ffmpegDir -match '(?i)^[a-z]:\\Users\\') {
            Write-Warn "ffmpeg лежит в профиле пользователя ($ffmpegDir): учётной записи службы он может быть недоступен."
            Write-Hint 'Надёжнее для всех пользователей: winget install Gyan.FFmpeg --scope machine'
        }
    }
    return ($parts -join ';')
}

function Get-ServiceLogSize {
    $total = 0
    foreach ($name in 'service.log', 'service-error.log', 'asrhub.log') {
        $file = Join-Path $logDir $name
        if (Test-Path -LiteralPath $file -PathType Leaf) {
            try { $total += (Get-Item -LiteralPath $file).Length } catch { }
        }
    }
    return $total
}

function Stop-PrefixProcesses {
    <#
      Процессы python этой установки. Удаление задачи планировщика её
      работающий экземпляр не завершает: прежний pythonw держал порт, новый
      не мог его занять, а проверка получала ответ старого процесса —
      «установка завершена» при работающем прежнем коде.
    #>
    if (-not $Prefix) { return }
    foreach ($process in @(Get-Process -Name python* -ErrorAction SilentlyContinue)) {
        $path = ''
        try { $path = "$($process.Path)" } catch { $path = '' }
        if (-not (Test-AsrPathInside -Path $path -Parent $Prefix)) { continue }
        Write-Info "Останавливаем процесс $($process.Id) ($path)"
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
}

function Install-AsService {
    param([pscustomobject]$Address)
    $nssm = Get-Command nssm -ErrorAction SilentlyContinue
    if (-not $nssm) {
        # Через sc.exe обычную консольную программу службой не сделать: она
        # не вызывает StartServiceCtrlDispatcher, и диспетчер через полминуты
        # возвращает ошибку 1053. Раньше служба при этом оставалась
        # зарегистрированной с автозапуском и тремя попытками перезапуска —
        # Windows безуспешно поднимала её при каждой загрузке. Поэтому без
        # NSSM идём в задачу планировщика: она работает и ничего за собой не
        # тянет.
        Write-Info 'NSSM не найден — служба Windows создана не будет.'
        Write-Hint 'Для настоящей службы установите NSSM: winget install NSSM.NSSM'
        Write-Hint 'Пока используем задачу планировщика — она запускает сервер при входе.'
        if (-not (Test-Path -LiteralPath $configFile)) {
            # Задаче без config.yaml каталог данных больше передать нечем.
            [Environment]::SetEnvironmentVariable('ASRHUB_DATA_DIR', $DataDir, 'Machine')
        }
        Install-AsTask -Address $Address
        return
    }
    Write-Info 'Создание службы через NSSM'
    Invoke-Checked -Command 'nssm' -Arguments @('install', $serviceName, $python, '-m', 'asrhub',
        '--host', $Address.BindHost, '--port', "$($Address.Port)") -Description 'регистрация службы' | Out-Null
    $settings = @(
        @('AppDirectory', $workDir),
        @('AppEnvironmentExtra', "ASRHUB_DATA_DIR=$DataDir", 'PYTHONUNBUFFERED=1', 'PYTHONIOENCODING=utf-8',
          "PATH=$(Get-ServicePath)"),
        @('DisplayName', 'ASR Hub — сервер распознавания речи'),
        @('Description', 'Распознавание речи на свободных моделях (GigaAM, Whisper и другие)'),
        @('Start', 'SERVICE_AUTO_START'),
        # Оба потока — в один файл. Сервер пишет журнал в stderr, и при
        # раздельных файлах «-Action logs» показывал пустой service.log, а
        # причина отказа лежала в service-error.log.
        @('AppStdout', (Join-Path $logDir 'service.log')),
        @('AppStderr', (Join-Path $logDir 'service.log')),
        @('AppRotateFiles', '1'),
        # Без AppRotateOnline NSSM ротирует журнал только при перезапуске
        # службы, и у долго работающего сервера файл рос месяцами.
        @('AppRotateOnline', '1'),
        @('AppRotateBytes', '33554432'),
        # Мягкая остановка по Ctrl+C: по умолчанию NSSM ждёт 1,5 с и убивает
        # процесс посреди записи в базу.
        @('AppStopMethodConsole', '15000'),
        @('AppExit', 'Default', 'Restart'),
        @('AppRestartDelay', '10000'))
    foreach ($setting in $settings) {
        Invoke-Checked -Command 'nssm' -Arguments (@('set', $serviceName) + $setting) `
            -Description "настройка службы: $($setting[0])" | Out-Null
    }

    # Служба от собственной виртуальной учётной записи, а не от LocalSystem.
    # Сервер принимает файлы из сети и разбирает их десятком библиотек; от
    # LocalSystem любая уязвимость в них — это вся машина. У виртуальной
    # записи «NT SERVICE\ASRHub» нет ни пароля, ни лишних прав: только
    # каталог данных на запись и программа на чтение.
    $account = "NT SERVICE\$serviceName"
    $switched = $false
    # Каталог данных — только администраторам, SYSTEM и учётной записи
    # службы. Установщик закрывает его и сам, но службу пересоздают и
    # отдельно — после обновления, чтобы перейти со старой учётной записи.
    $null = Set-AsrPrivateAcl -Path $DataDir
    if (Grant-ServiceAccess -Account $account) {
        $null = Invoke-Native -Command 'sc.exe' -Arguments @('config', $serviceName, 'obj=', $account)
        if ($LASTEXITCODE -eq 0) { $switched = $true }
        else { Write-Warn "Не удалось перевести службу на учётную запись $account — остаётся LocalSystem." }
    } else {
        Write-Warn "Не удалось выдать права учётной записи $account — служба остаётся от LocalSystem."
    }
    $logBefore = Get-ServiceLogSize
    $started = $true
    try {
        Start-Service -Name $serviceName -ErrorAction Stop
    } catch {
        if (-not $switched) { throw }
        $started = $false
    }
    # NSSM объявляет службу запущенной, как только создал процесс, и
    # исключение Start-Service ловило только отказ входа (1069). Python,
    # которому учётная запись не может прочитать собственные библиотеки,
    # падал сразу, а NSSM перезапускал его раз в 10 секунд — установщик же
    # печатал «запущена (NT SERVICE\ASRHub)». Поэтому ждём ответа сервера.
    # Молчит, и в журнал службы не попало ни строки — процесс не дошёл даже
    # до старта: это учётная запись. Журнал растёт — сервер жив, просто
    # долго поднимается, и запись службы оставляем.
    if ($switched -and $started) {
        $probeHost = if ($Address.BindHost -and $Address.BindHost -notin @('0.0.0.0', '::', '*', 'localhost', '[::]')) {
            if ($Address.BindHost -match ':' -and -not $Address.BindHost.StartsWith('[')) { "[$($Address.BindHost)]" } else { $Address.BindHost }
        } else { '127.0.0.1' }
        $probe = Wait-AsrHealth -Port $Address.Port -HostName $probeHost -Seconds 40
        if ($probe.Status -eq 0 -and (Get-ServiceLogSize) -le $logBefore) { $started = $false }
    }
    if (-not $started) {
        # Лучше работающая служба от LocalSystem, чем неработающая от
        # правильной записи.
        Write-Warn "Служба не запустилась от $account — возвращаем LocalSystem."
        Write-Hint 'Частая причина — Python, поставленный «для текущего пользователя»: у учётной записи службы нет доступа к его каталогу.'
        Write-Hint 'Python с python.org «для всех пользователей» снимет это ограничение; затем: service.ps1 -Action install.'
        Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
        $null = Invoke-Native -Command 'sc.exe' -Arguments @('config', $serviceName, 'obj=', 'LocalSystem')
        $switched = $false
        Start-Service -Name $serviceName -ErrorAction Stop
    }
    $who = if ($switched) { $account } else { 'LocalSystem' }
    Write-Ok "Служба «$serviceName» создана и запущена (учётная запись $who)"
}

function Install-AsTask {
    param([pscustomobject]$Address)
    Write-Info 'Создание задачи планировщика (запуск при входе пользователя)'
    # Каталог данных задаче передаётся через --config: переменную окружения
    # задаче планировщика не задать, а своей у сервера на Windows по
    # умолчанию %PROGRAMDATA%\ASRHub. Установка без прав администратора
    # кладёт данные в %LOCALAPPDATA%\ASRHub\data, и сервер из задачи
    # работал мимо них: новая пустая база, новый ключ, config.yaml, токен и
    # скачанные модели не видны. В config.yaml установщик пишет data_dir.
    $arguments = "-m asrhub --host $($Address.BindHost) --port $($Address.Port)"
    if (Test-Path -LiteralPath $configFile) {
        $arguments = "-m asrhub --config `"$configFile`" --host $($Address.BindHost) --port $($Address.Port)"
    } else {
        Write-Warn "Нет $configFile — сервер из задачи возьмёт каталог данных по умолчанию."
    }
    # pythonw.exe, а не python.exe: у python.exe при каждом входе в систему
    # открывалось окно консоли, и закрыть его значило погасить сервер. Вывод
    # сервер без консоли пишет в logs\service.log каталога данных.
    $runner = if (Test-Path -LiteralPath $pythonw) { $pythonw } else { $python }
    $taskAction = New-ScheduledTaskAction -Execute $runner `
        -Argument $arguments -WorkingDirectory $workDir
    # -User: без него триггер срабатывал на вход ЛЮБОГО пользователя машины.
    $user = if ($env:USERDOMAIN) { "$env:USERDOMAIN\$env:USERNAME" } else { $env:USERNAME }
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $trigger `
        -Settings $settings -Description 'ASR Hub — сервер распознавания речи' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Ok "Задача «$taskName» создана и запущена"
    Write-Hint 'Задача стартует при входе пользователя. Для запуска без входа нужны права администратора.'
}

function Invoke-ServerPython {
    <#
      Код на Python с пакетом asrhub ЭТОЙ установки: PYTHONPATH — только
      её каталог server, каталог данных — её же. Код идёт через временный
      файл: «python -» со стандартным вводом Windows PowerShell 5.1 передаёт
      в кодировке консоли, и кириллица в нём ломается.
    #>
    param([string]$Code, [hashtable]$Environment = @{}, [switch]$Quiet)
    $tmp = Join-AsrPath ([System.IO.Path]::GetTempPath()) ('asrhub-service-{0}.py' -f ([guid]::NewGuid().ToString('N')))
    [System.IO.File]::WriteAllText($tmp, $Code, [System.Text.UTF8Encoding]::new($false))
    $values = @{ PYTHONPATH = $workDir; ASRHUB_DATA_DIR = $DataDir; PYTHONIOENCODING = 'utf-8' }
    foreach ($name in @($Environment.Keys)) { $values[$name] = $Environment[$name] }
    $saved = @{}
    foreach ($name in @($values.Keys)) {
        $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        [Environment]::SetEnvironmentVariable($name, $values[$name], 'Process')
    }
    try {
        $output = @(Invoke-Native -Command $python -Arguments @($tmp) -MergeError)
        $rc = $LASTEXITCODE
        if (-not $Quiet) { foreach ($line in $output) { Write-Host $line } }
        return $rc
    } finally {
        foreach ($name in @($saved.Keys)) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
        Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
    }
}

$backupCode = @'
from pathlib import Path

from asrhub.config import load
from asrhub.db import Database
from asrhub.maintenance import backup_dir, make_backup

settings = load()
база = Path(settings.paths.data) / "asrhub.db"
db = Database(база)
копия = make_backup(db, settings)
db.close()
if копия is None:
    print("Копию сделать не удалось — причина в журнале выше.")
    raise SystemExit(1)
print(f"Копия: {копия}")
print(f"Каталог копий: {backup_dir(settings)}")
'@

$restoreCode = @'
import os
from pathlib import Path

from asrhub.config import load
from asrhub.maintenance import restore

settings = load()
цель = Path(settings.paths.db)
try:
    прежняя = restore(Path(os.environ["ASRHUB_RESTORE_FROM"]), цель, settings)
except (FileNotFoundError, ValueError) as exc:
    print(f"Восстановление отменено, рабочая база не тронута: {exc}")
    raise SystemExit(1)
print(f"База восстановлена: {цель}")
if прежняя:
    print(f"Прежняя база сохранена рядом: {прежняя} (вместе со своим журналом) — "
          "удалите её, когда убедитесь, что всё на месте.")
'@

# Пробный запуск ничего не меняет: ни службу, ни задачу. Проверка состояния
# и журнал ничего и не меняют — они работают как обычно.
if ((Get-DryRun) -and $Action -notin @('status', 'logs')) {
    Write-Host "[пробный запуск] автозапуск ${serviceName}: $Action" -ForegroundColor Yellow
    return
}

switch ($Action) {

    'install' {
        if (-not $Prefix) {
            Write-Err 'Не найдена установка ASR Hub. Укажите каталог ключом -Prefix.'
            exit 2
        }
        if (-not (Test-Path -LiteralPath $python)) {
            throw "Не найден интерпретатор: $python. Сначала выполните установку."
        }
        if ($foreignAutostart) {
            # Имя службы на Windows одно на машину. Пересоздать её значило
            # молча отнять автозапуск у первой установки.
            $first = $foreignAutostart[0]
            Write-Err "Автозапуск «$($first.Name)» уже обслуживает другую установку: $($first.Prefix)"
            Write-Hint 'Вторую установку ставьте с -NoService и запускайте вручную,'
            Write-Hint "либо сначала удалите первую: powershell -ExecutionPolicy Bypass -File `"$($first.Prefix)\scripts\uninstall.ps1`""
            exit 2
        }
        $address = Get-InstallAddress
        if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        if (Test-Administrator) {
            # Чужая служба сюда не доходит (выход выше); осталась своя или
            # бесхозная — от установки, чей каталог уже удалён.
            if (Get-Service -Name $serviceName -ErrorAction SilentlyContinue) {
                Write-Info 'Служба уже существует — пересоздаём'
                Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
                $null = Invoke-Native -Command 'sc.exe' -Arguments @('delete', $serviceName)
                Start-Sleep -Seconds 2
            }
            if ($ownTask) {
                # Служба вместо задачи: задача, оставленная рядом, при входе
                # поднимала бы второй сервер на тот же порт.
                Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
            Stop-PrefixProcesses
            Install-AsService -Address $address
        } else {
            if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                # Сначала остановка: Unregister работающий экземпляр не
                # завершает (в ветке администратора остановка была, здесь — нет).
                Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
            Stop-PrefixProcesses
            Install-AsTask -Address $address
        }
    }

    'uninstall' {
        Show-ForeignAutostart
        if ($ownService) {
            Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
            if (Get-Command nssm -ErrorAction SilentlyContinue) {
                $null = Invoke-Native -Command 'nssm' -Arguments @('remove', $serviceName, 'confirm')
            } else {
                $null = Invoke-Native -Command 'sc.exe' -Arguments @('delete', $serviceName)
            }
            Write-Ok 'Служба удалена'
        }
        if ($ownTask) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Ok 'Задача планировщика удалена'
        }
        if (-not $ownService -and -not $ownTask) {
            Write-Info 'Автозапуск у этой установки не был настроен.'
        }
    }

    'start' {
        if ($ownService) { Start-Service -Name $serviceName; Write-Ok 'Служба запущена' }
        elseif ($ownTask) { Start-ScheduledTask -TaskName $taskName; Write-Ok 'Задача запущена' }
        else { Show-ForeignAutostart; Write-Err 'Автозапуск у этой установки не настроен.'; exit 1 }
    }

    'stop' {
        if ($ownService) { Stop-Service -Name $serviceName -Force; Write-Ok 'Служба остановлена' }
        elseif ($ownTask) { Stop-ScheduledTask -TaskName $taskName; Write-Ok 'Задача остановлена' }
        else { Show-ForeignAutostart; Write-Info 'Останавливать нечего.' }
    }

    'restart' {
        if ($ownService) { Restart-Service -Name $serviceName -Force; Write-Ok 'Служба перезапущена' }
        elseif ($ownTask) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Start-ScheduledTask -TaskName $taskName
            Write-Ok 'Задача перезапущена'
        }
        else { Show-ForeignAutostart; Write-Err 'Автозапуск у этой установки не настроен.'; exit 1 }
    }

    'status' {
        $records = if ($Prefix) { $ownAutostart } else { $allAutostart }
        $code = 0
        foreach ($record in $records) {
            if ($record.Kind -eq 'service') {
                $service = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
                $state = if ($service) { "$($service.Status)" } else { 'не найдена' }
                Write-Host "Служба $serviceName : $state"
                if ($state -ne 'Running') { $code = 1 }
            } else {
                $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
                Write-Host "Задача $taskName : $(if ($task) { $task.State } else { 'не найдена' })"
                if ($task) {
                    $info = Get-ScheduledTaskInfo -TaskName $taskName -ErrorAction SilentlyContinue
                    if ($info) { Write-Host "  Последний запуск: $($info.LastRunTime), код $($info.LastTaskResult)" }
                }
            }
            if ($record.Prefix) { Write-Host "  Каталог программы: $($record.Prefix)" }
        }
        if (-not $records) {
            Show-ForeignAutostart
            Write-Warn 'Автозапуск у этой установки не настроен.'
            exit 1
        }
        # Порт — из строки запуска автозапуска: раньше проверка стучалась в
        # 8080 при любом настроенном порте и объявляла работающий сервер
        # мёртвым.
        $address = Get-AsrServerAddress -Prefix $Prefix -DataDir $DataDir -Autostart $records
        $probe = Invoke-AsrHealth -Url "http://$($address.ProbeHost):$($address.Port)/api/health"
        if ($probe.Status -ge 200 -and $probe.Status -lt 300) {
            $health = $null
            try { $health = $probe.Body | ConvertFrom-Json } catch { }
            if ($health) {
                Write-Ok "Сервер отвечает на порту $($address.Port): версия $($health.version), время работы $([int]$health.uptime_s) с"
            } else {
                Write-Ok "Сервер отвечает на порту $($address.Port)"
            }
        } elseif ($probe.Status -gt 0) {
            Write-Warn "Сервер на порту $($address.Port) отвечает, но сообщает о неисправности (код $($probe.Status))."
            $code = 1
        } else {
            Write-Warn "Сервер не отвечает на порту $($address.Port)"
            $code = 1
        }
        if ($code -ne 0) { exit $code }
    }

    'logs' {
        # Самый свежий НЕПУСТОЙ из журналов. Раньше брался service.log, раз он
        # есть, — а NSSM создаёт его и пустым, когда сервер пишет только в
        # stderr (service-error.log у служб, созданных прежними версиями).
        $candidates = @('service.log', 'service-error.log', 'asrhub.log') |
            ForEach-Object { Join-Path $logDir $_ } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            ForEach-Object { Get-Item -LiteralPath $_ } |
            Where-Object { $_.Length -gt 0 } |
            Sort-Object LastWriteTime -Descending
        $target = @($candidates | Select-Object -First 1)
        if ($target.Count -eq 0) { Write-Warn "Файлы журнала не найдены (или пусты) в $logDir"; exit 1 }
        $path = $target[0].FullName
        Write-Info "Журнал: $path"
        if ($Follow) { Get-Content -LiteralPath $path -Tail $Lines -Wait }
        else { Get-Content -LiteralPath $path -Tail $Lines }
    }

    'backup' {
        # Копия — командой SQLite «.backup» через пакет установки, как в
        # service.sh: база работает в режиме WAL, и копия одного файла
        # несогласованна. Прежде у Windows этого действия не было вовсе.
        if (-not $Prefix -or -not (Test-Path -LiteralPath $python)) {
            Write-Err 'Не найден интерпретатор ASR Hub. Укажите каталог установки ключом -Prefix.'
            exit 1
        }
        $rc = Invoke-ServerPython -Code $backupCode
        if ($rc -ne 0) { Write-Err 'Резервная копия не сделана.'; exit 1 }
        Write-Ok 'Резервная копия готова'
    }

    'restore' {
        if (-not $From) { Write-Err 'Укажите файл копии: -From ПУТЬ'; exit 2 }
        $From = ConvertTo-AsrFullPath $From
        if (-not (Test-Path -LiteralPath $From -PathType Leaf)) { Write-Err "Файл не найден: $From"; exit 1 }
        if (-not $Prefix -or -not (Test-Path -LiteralPath $python)) {
            Write-Err 'Не найден интерпретатор ASR Hub. Укажите каталог установки ключом -Prefix.'
            exit 1
        }
        # Пакет — до остановки службы: иначе служба стояла бы, а база не
        # восстановлена (так было в service.sh до захода 47).
        if ((Invoke-ServerPython -Code 'import asrhub.maintenance' -Quiet) -ne 0) {
            Write-Err "Пакет asrhub не загружается из $workDir — восстановление отменено, служба не тронута."
            exit 1
        }
        # Восстановление на работающем сервере затрёт базу под ним.
        Write-Warn 'Сервер будет остановлен на время восстановления.'
        if ($ownService) { Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue }
        if ($ownTask) { Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue }
        Stop-PrefixProcesses
        Start-Sleep -Seconds 2
        $service = if ($ownService) { Get-Service -Name $serviceName -ErrorAction SilentlyContinue } else { $null }
        $alive = @(Get-Process -Name python* -ErrorAction SilentlyContinue | Where-Object {
            $p = ''; try { $p = "$($_.Path)" } catch { }; Test-AsrPathInside -Path $p -Parent $Prefix })
        if (($service -and "$($service.Status)" -ne 'Stopped') -or $alive.Count -gt 0) {
            Write-Err 'Сервер не остановился — восстановление отменено.'
            Write-Hint 'Остановите его вручную (service.ps1 -Action stop от администратора) и повторите.'
            exit 1
        }
        $rc = Invoke-ServerPython -Code $restoreCode -Environment @{ ASRHUB_RESTORE_FROM = $From }
        if ($rc -ne 0) { exit 1 }
        Write-Ok "Восстановление завершено. Запустите сервер: powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Action start"
    }
}

} finally {
    Set-DryRun $dryRunBefore
}
