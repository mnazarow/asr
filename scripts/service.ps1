<#
.SYNOPSIS
    Управление автозапуском ASR Hub на Windows.
.DESCRIPTION
    От администратора создаётся служба Windows (через NSSM, если он есть,
    иначе через sc.exe с оболочкой). Без прав администратора создаётся
    задача планировщика, запускающаяся при входе пользователя.
.EXAMPLE
    powershell -File scripts\service.ps1 -Action install -Prefix "C:\Program Files\ASRHub"
    powershell -File scripts\service.ps1 -Action status
    powershell -File scripts\service.ps1 -Action logs -Lines 200
#>
[CmdletBinding()]
param(
    [ValidateSet('install', 'uninstall', 'start', 'stop', 'restart', 'status', 'logs')]
    [string]$Action = 'status',
    [string]$Prefix = 'C:\Program Files\ASRHub',
    [string]$DataDir = '',
    [int]$Port = 8080,
    [string]$BindHost = '0.0.0.0',
    [int]$Lines = 100,
    [switch]$Follow
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force

if (-not $DataDir) {
    $DataDir = if (Test-Administrator) { Join-Path $env:ProgramData 'ASRHub' }
               else { Join-Path $env:LOCALAPPDATA 'ASRHub\data' }
}

$serviceName = 'ASRHub'
$taskName = 'ASRHub Server'
$python = Join-Path $Prefix 'venv\Scripts\python.exe'
$workDir = Join-Path $Prefix 'server'
$logDir = Join-Path $DataDir 'logs'

function Test-ServiceExists {
    return [bool](Get-Service -Name $serviceName -ErrorAction SilentlyContinue)
}
function Test-TaskExists {
    return [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
}

function Install-AsService {
    $nssm = Get-Command nssm -ErrorAction SilentlyContinue
    if ($nssm) {
        Write-Info 'Создание службы через NSSM'
        & nssm install $serviceName $python '-m' 'asrhub' '--host' $BindHost '--port' $Port
        & nssm set $serviceName AppDirectory $workDir
        & nssm set $serviceName AppEnvironmentExtra "ASRHUB_DATA_DIR=$DataDir" "PYTHONUNBUFFERED=1"
        & nssm set $serviceName DisplayName 'ASR Hub — сервер распознавания речи'
        & nssm set $serviceName Description 'Распознавание речи на свободных моделях (GigaAM, Whisper и другие)'
        & nssm set $serviceName Start SERVICE_AUTO_START
        & nssm set $serviceName AppStdout (Join-Path $logDir 'service.log')
        & nssm set $serviceName AppStderr (Join-Path $logDir 'service-error.log')
        & nssm set $serviceName AppRotateFiles 1
        & nssm set $serviceName AppRotateBytes 33554432
        & nssm set $serviceName AppExit Default Restart
        & nssm set $serviceName AppRestartDelay 10000
        Start-Service -Name $serviceName
        Write-Ok "Служба «$serviceName» создана и запущена"
        return
    }

    # Через sc.exe обычную консольную программу службой не сделать: она не
    # вызывает StartServiceCtrlDispatcher, и диспетчер через полминуты
    # возвращает ошибку 1053. Раньше служба при этом оставалась
    # зарегистрированной с автозапуском и тремя попытками перезапуска —
    # Windows безуспешно поднимала её при каждой загрузке. Поэтому без NSSM
    # идём в задачу планировщика: она работает и ничего за собой не тянет.
    Write-Info 'NSSM не найден — служба Windows создана не будет.'
    Write-Hint 'Для настоящей службы установите NSSM: winget install NSSM.NSSM'
    Write-Hint 'Пока используем задачу планировщика — она запускает сервер при входе.'
    [Environment]::SetEnvironmentVariable('ASRHUB_DATA_DIR', $DataDir, 'Machine')
    Install-AsTask
}

function Install-AsTask {
    Write-Info 'Создание задачи планировщика (запуск при входе пользователя)'
    $action = New-ScheduledTaskAction -Execute $python `
        -Argument "-m asrhub --host $BindHost --port $Port" -WorkingDirectory $workDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description 'ASR Hub — сервер распознавания речи' -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Ok "Задача «$taskName» создана и запущена"
    Write-Hint 'Задача стартует при входе пользователя. Для запуска без входа нужны права администратора.'
}

switch ($Action) {

    'install' {
        if (-not (Test-Path $python)) {
            throw "Не найден интерпретатор: $python. Сначала выполните установку."
        }
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        if (Test-Administrator) {
            if (Test-ServiceExists) {
                Write-Info 'Служба уже существует — пересоздаём'
                Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
                & sc.exe delete $serviceName | Out-Null
                Start-Sleep -Seconds 2
            }
            Install-AsService
        } else {
            if (Test-TaskExists) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
            Install-AsTask
        }
    }

    'uninstall' {
        if (Test-ServiceExists) {
            Stop-Service -Name $serviceName -Force -ErrorAction SilentlyContinue
            if (Get-Command nssm -ErrorAction SilentlyContinue) { & nssm remove $serviceName confirm | Out-Null }
            else { & sc.exe delete $serviceName | Out-Null }
            Write-Ok 'Служба удалена'
        }
        if (Test-TaskExists) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            Write-Ok 'Задача планировщика удалена'
        }
        if (-not (Test-ServiceExists) -and -not (Test-TaskExists)) {
            Write-Info 'Автозапуск не был настроен.'
        }
    }

    'start' {
        if (Test-ServiceExists) { Start-Service -Name $serviceName; Write-Ok 'Служба запущена' }
        elseif (Test-TaskExists) { Start-ScheduledTask -TaskName $taskName; Write-Ok 'Задача запущена' }
        else { Write-Err 'Автозапуск не настроен.'; exit 1 }
    }

    'stop' {
        if (Test-ServiceExists) { Stop-Service -Name $serviceName -Force; Write-Ok 'Служба остановлена' }
        elseif (Test-TaskExists) { Stop-ScheduledTask -TaskName $taskName; Write-Ok 'Задача остановлена' }
        else { Write-Info 'Останавливать нечего.' }
    }

    'restart' {
        if (Test-ServiceExists) { Restart-Service -Name $serviceName -Force; Write-Ok 'Служба перезапущена' }
        elseif (Test-TaskExists) {
            Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Start-ScheduledTask -TaskName $taskName
            Write-Ok 'Задача перезапущена'
        }
        else { Write-Err 'Автозапуск не настроен.'; exit 1 }
    }

    'status' {
        $found = $false
        if (Test-ServiceExists) {
            $found = $true
            $service = Get-Service -Name $serviceName
            Write-Host "Служба $serviceName : $($service.Status)"
            if ($service.Status -ne 'Running') { exit 1 }
        }
        if (Test-TaskExists) {
            $found = $true
            $task = Get-ScheduledTask -TaskName $taskName
            $info = Get-ScheduledTaskInfo -TaskName $taskName
            Write-Host "Задача $taskName : $($task.State)"
            Write-Host "  Последний запуск: $($info.LastRunTime), код $($info.LastTaskResult)"
        }
        if (-not $found) { Write-Warn 'Автозапуск не настроен.'; exit 1 }
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 3
            Write-Ok "Сервер отвечает: версия $($health.version), время работы $([int]$health.uptime_s) с"
        } catch { Write-Warn "Сервер не отвечает на порту $Port" }
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
