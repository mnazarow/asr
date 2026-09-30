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
    powershell -File scripts\service.ps1 -Action install -Prefix "C:\Program Files\ASRHub"
    powershell -File scripts\service.ps1 -Action status
    powershell -File scripts\service.ps1 -Action logs -Lines 200
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'status',
    [string]$Prefix = '',
    [string]$DataDir = '',
    [ValidateRange(0, 65535)][int]$Port = 0,
    [string]$BindHost = '',
    [int]$Lines = 100,
    [switch]$Follow,
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
    return ($LASTEXITCODE -eq 0)
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
        @('AppEnvironmentExtra', "ASRHUB_DATA_DIR=$DataDir", 'PYTHONUNBUFFERED=1', 'PYTHONIOENCODING=utf-8'),
        @('DisplayName', 'ASR Hub — сервер распознавания речи'),
        @('Description', 'Распознавание речи на свободных моделях (GigaAM, Whisper и другие)'),
        @('Start', 'SERVICE_AUTO_START'),
        @('AppStdout', (Join-Path $logDir 'service.log')),
        @('AppStderr', (Join-Path $logDir 'service-error.log')),
        @('AppRotateFiles', '1'),
        @('AppRotateBytes', '33554432'),
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
    try {
        Start-Service -Name $serviceName -ErrorAction Stop
    } catch {
        if (-not $switched) { throw }
        # Отказ входа (1069) и подобное: служба не поднялась именно из-за
        # учётной записи. Лучше работающая служба от LocalSystem, чем
        # неработающая от правильной записи.
        Write-Warn "Служба не запустилась от $account — возвращаем LocalSystem."
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
            Write-Hint "либо сначала удалите первую: powershell -File `"$($first.Prefix)\scripts\uninstall.ps1`""
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
            Install-AsService -Address $address
        } else {
            if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
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
        $logFile = Join-Path $logDir 'asrhub.log'
        $serviceLog = Join-Path $logDir 'service.log'
        $target = if (Test-Path $serviceLog) { $serviceLog } elseif (Test-Path $logFile) { $logFile } else { $null }
        if (-not $target) { Write-Warn "Файлы журнала не найдены в $logDir"; exit 1 }
        if ($Follow) { Get-Content -Path $target -Tail $Lines -Wait }
        else { Get-Content -Path $target -Tail $Lines }
    }
}

} finally {
    Set-DryRun $dryRunBefore
}
