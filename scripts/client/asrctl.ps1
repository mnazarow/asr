<#
.SYNOPSIS
    asrctl — клиент ASR Hub для Windows.
.DESCRIPTION
    Отправка файлов на удалённый сервер распознавания, наблюдение за
    очередью, скачивание результатов. Профили серверов хранятся в
    %APPDATA%\asrctl\profiles.json.
.EXAMPLE
    .\asrctl.ps1 config add рабочий https://asr.example.com ah_ключ
    .\asrctl.ps1 send запись.mp3 -Model gigaam-v3-e2e-rnnt -Wait
    .\asrctl.ps1 send-dir .\записи -Recurse -Formats srt,txt
    .\asrctl.ps1 queue -Watch
    .\asrctl.ps1 get job_xxx -Format srt -Output субтитры.srt
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Command = 'help',
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)][string[]]$Rest = @(),
    [string]$Server = '',
    [string]$Key = '',
    [string]$Profile = 'default',
    [string]$Model = '',
    [string]$Language = '',
    [string]$Formats = 'txt',
    [string]$Output = '.',
    [string]$Format = 'txt',
    [string]$Status = '',
    [int]$Priority = 0,
    [int]$Limit = 25,
    [int]$TimeoutSec = 600,
    [switch]$Wait,
    [switch]$Recurse,
    [switch]$Watch
)

$ErrorActionPreference = 'Stop'
$Version = '3.0.0'
# APPDATA есть только в Windows; в WSL и PowerShell для Linux берём ~/.config
$ConfigBase = if ($env:APPDATA) { $env:APPDATA }
              elseif ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME }
              else { Join-Path $HOME '.config' }
$ConfigDir = Join-Path $ConfigBase 'asrctl'
$ConfigFile = Join-Path $ConfigDir 'profiles.json'

function Write-Ok2   { param([string]$m) Write-Host "✓ $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "! $m" -ForegroundColor Yellow }
function Write-Err2  { param([string]$m) Write-Host "✕ $m" -ForegroundColor Red }

function Get-Profiles {
    if (Test-Path $ConfigFile) {
        try { return Get-Content $ConfigFile -Raw | ConvertFrom-Json } catch { return @{} }
    }
    return @{}
}

function Save-Profiles {
    param($Profiles)
    if (-not (Test-Path $ConfigDir)) { New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null }
    $Profiles | ConvertTo-Json -Depth 5 | Set-Content -Path $ConfigFile -Encoding UTF8
}

# Разрешение адреса и ключа: параметры > переменные окружения > профиль
$profiles = Get-Profiles
if (-not $Server) {
    if ($env:ASRHUB_SERVER) { $Server = $env:ASRHUB_SERVER }
    elseif ($profiles.PSObject.Properties.Name -contains $Profile) { $Server = $profiles.$Profile.url }
    else { $Server = 'http://127.0.0.1:8080' }
}
if (-not $Key) {
    if ($env:ASRHUB_KEY) { $Key = $env:ASRHUB_KEY }
    elseif ($profiles.PSObject.Properties.Name -contains $Profile) { $Key = $profiles.$Profile.key }
}
$Server = $Server.TrimEnd('/')

function Invoke-Api {
    param([string]$Method = 'Get', [string]$Path, $Body = $null, [string]$OutFile = '')
    $headers = @{ 'Accept' = 'application/json' }
    if ($Key) { $headers['X-API-Key'] = $Key }
    $params = @{
        Uri = "$Server$Path"; Method = $Method; Headers = $headers
        TimeoutSec = $TimeoutSec; ErrorAction = 'Stop'
    }
    if ($Body) { $params['Body'] = $Body }
    if ($OutFile) { $params['OutFile'] = $OutFile }
    try {
        return Invoke-RestMethod @params
    } catch {
        $response = $_.Exception.Response
        if ($response -and $response.StatusCode.value__ -eq 401) {
            Write-Err2 'Неверный или отсутствующий ключ доступа.'
            Write-Host "  Задайте: .\asrctl.ps1 config add $Profile $Server <ключ>" -ForegroundColor DarkGray
            exit 1
        }
        $detail = $null
        try {
            $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
            $detail = $reader.ReadToEnd() | ConvertFrom-Json
        } catch { }
        if ($detail -and $detail.detail) {
            Write-Err2 $detail.detail.message
            if ($detail.detail.hint) { Write-Host "  $($detail.detail.hint)" -ForegroundColor DarkGray }
        } else {
            Write-Err2 "Сервер $Server недоступен: $($_.Exception.Message)"
        }
        exit 1
    }
}

function Send-File {
    param([string]$Path)
    if (-not (Test-Path $Path)) { Write-Err2 "Файл не найден: $Path"; return $null }
    $settings = @{}
    if ($Model) { $settings['model'] = $Model }
    if ($Language) { $settings['language'] = $Language }
    if ($Formats) { $settings['output_formats'] = ($Formats -split ',' | ForEach-Object { $_.Trim() }) }
    $settingsJson = $settings | ConvertTo-Json -Compress

    $item = Get-Item $Path
    Write-Host "Отправка: $($item.Name) ($([math]::Round($item.Length/1MB, 1)) МБ)"

    $form = @{ file = $item; settings = $settingsJson }
    if ($Priority -gt 0) { $form['priority'] = $Priority }

    $headers = @{}
    if ($Key) { $headers['X-API-Key'] = $Key }
    try {
        $job = Invoke-RestMethod -Uri "$Server/api/jobs" -Method Post -Form $form `
            -Headers $headers -TimeoutSec $TimeoutSec
    } catch {
        Write-Err2 "Не удалось отправить файл: $($_.Exception.Message)"
        return $null
    }
    Write-Ok2 "Задание создано: $($job.id)"
    return $job.id
}

function Wait-Job {
    param([string]$JobId)
    $spinner = '|', '/', '-', '\'
    $i = 0
    while ($true) {
        $job = Invoke-Api -Path "/api/jobs/$JobId"
        switch ($job.status) {
            'completed' {
                Write-Host "`r✓ готово                                              " -ForegroundColor Green
                Write-Host "  RTF $([math]::Round($job.rtf, 3)) · слов $($job.words_count)"
                foreach ($fmt in ($Formats -split ',' | ForEach-Object { $_.Trim() })) {
                    Get-Result -JobId $JobId -Fmt $fmt -Destination $Output
                }
                return $true
            }
            'failed' {
                Write-Host "`r✕ ошибка                                              " -ForegroundColor Red
                Write-Host "  $($job.error_message)"
                if ($job.error_hint) { Write-Host "  $($job.error_hint)" -ForegroundColor DarkGray }
                return $false
            }
            'cancelled' { Write-Host "`r! отменено                                    " -ForegroundColor Yellow; return $false }
            default {
                $progress = if ($null -ne $job.progress) { $job.progress } else { 0 }
                $percent = [int]($progress * 100)
                Write-Host ("`r{0} {1} {2}%  {3}        " -f $spinner[$i % 4], $job.status, $percent, $job.stage) -NoNewline
                $i++
                Start-Sleep -Seconds 2
            }
        }
    }
}

function Get-Result {
    param([string]$JobId, [string]$Fmt, [string]$Destination)
    $isDir = (Test-Path $Destination -PathType Container) -or
             ($Destination.EndsWith('\')) -or (-not [System.IO.Path]::GetExtension($Destination))
    if ($isDir) {
        if (-not (Test-Path $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }
        $job = Invoke-Api -Path "/api/jobs/$JobId"
        $name = [System.IO.Path]::GetFileNameWithoutExtension($job.filename)
        $target = Join-Path $Destination "$name.$Fmt"
    } else {
        $target = $Destination
    }
    $headers = @{}
    if ($Key) { $headers['X-API-Key'] = $Key }
    try {
        Invoke-WebRequest -Uri "$Server/api/jobs/$JobId/download?fmt=$Fmt" -Headers $headers `
            -OutFile $target -TimeoutSec $TimeoutSec -UseBasicParsing | Out-Null
        Write-Ok2 "Сохранено: $target"
    } catch { Write-Err2 "Не удалось скачать формат ${Fmt}: $($_.Exception.Message)" }
}

switch ($Command) {

'send' {
    $file = $Rest | Select-Object -First 1
    if (-not $file) { Write-Err2 'Укажите файл.'; exit 2 }
    $jobId = Send-File -Path $file
    if ($jobId -and $Wait) { if (-not (Wait-Job -JobId $jobId)) { exit 1 } }
    elseif ($jobId) { Write-Host "Следить: .\asrctl.ps1 status $jobId" }
}

'send-dir' {
    $dir = $Rest | Select-Object -First 1
    if (-not (Test-Path $dir)) { Write-Err2 "Каталог не найден: $dir"; exit 2 }
    $extensions = '*.wav', '*.mp3', '*.m4a', '*.flac', '*.ogg', '*.opus',
                  '*.mp4', '*.mkv', '*.mov', '*.aac', '*.wma'
    $files = Get-ChildItem -Path $dir -Include $extensions -File -Recurse:$Recurse |
             Sort-Object Name
    if (-not $files) { Write-Err2 'В каталоге нет поддерживаемых файлов.'; exit 2 }
    Write-Host "Найдено файлов: $($files.Count)"
    $index = 0; $failed = 0
    foreach ($file in $files) {
        $index++
        Write-Host "[$index/$($files.Count)] $($file.Name)" -ForegroundColor Blue
        $jobId = Send-File -Path $file.FullName
        if (-not $jobId) { $failed++ }
    }
    Write-Ok2 "Отправлено: $($files.Count - $failed) из $($files.Count)"
    if ($failed) { Write-Warn2 "Не отправлено: $failed" }
}

'status' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Write-Err2 'Укажите идентификатор задания.'; exit 2 }
    $job = Invoke-Api -Path "/api/jobs/$jobId"
    Write-Host $job.filename -ForegroundColor White
    $progress = if ($null -ne $job.progress) { $job.progress } else { 0 }
    Write-Host ("  Состояние    {0} ({1} %)" -f $job.status, [int]($progress * 100))
    Write-Host ("  Этап         {0}" -f $job.stage)
    Write-Host ("  Модель       {0}" -f $job.model)
    Write-Host ("  Длительность {0} с" -f [math]::Round($job.media_duration_s, 1))
    if ($job.rtf) { Write-Host ("  RTF          {0}" -f [math]::Round($job.rtf, 3)) }
    if ($job.error_message) {
        Write-Host ("  Ошибка       {0}" -f $job.error_message) -ForegroundColor Red
        Write-Host ("  {0}" -f $job.error_hint) -ForegroundColor DarkGray
    }
}

'wait' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Write-Err2 'Укажите идентификатор.'; exit 2 }
    if (-not (Wait-Job -JobId $jobId)) { exit 1 }
}

'get' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Write-Err2 'Укажите идентификатор.'; exit 2 }
    Get-Result -JobId $jobId -Fmt $Format -Destination $Output
}

'queue' {
    do {
        $q = Invoke-Api -Path '/api/queue'
        if ($Watch) { Clear-Host }
        $pausedText = if ($q.paused) { 'ПАУЗА' } else { '' }
        Write-Host ("Очередь  ожидают: {0}  выполняется: {1}  воркеров: {2}  {3}" -f
            $q.queue_depth, $q.active, $q.worker_count, $pausedText) -ForegroundColor White
        Write-Host ("готово {0} · ошибок {1} · отменено {2}" -f
            $q.counts.completed, $q.counts.failed, $q.counts.cancelled) -ForegroundColor DarkGray
        Write-Host ''
        if (-not $q.items) { Write-Host 'Очередь пуста' -ForegroundColor DarkGray }
        foreach ($job in ($q.items | Select-Object -First 25)) {
            $progress = if ($null -ne $job.progress) { $job.progress } else { 0 }
            $percent = [int]($progress * 100)
            $barLength = [int]($progress * 20)
            $bar = ('█' * $barLength) + ('·' * (20 - $barLength))
            $colour = switch ($job.status) {
                'running' { 'Green' } 'failed' { 'Red' } 'retry' { 'Yellow' } default { 'Gray' }
            }
            Write-Host ("  {0,-10} {1,-36} {2} {3,3}%  {4}" -f
                $job.status, $job.filename.Substring(0, [Math]::Min(34, $job.filename.Length)),
                $bar, $percent, $job.stage) -ForegroundColor $colour
        }
        if ($Watch) { Start-Sleep -Seconds 3 }
    } while ($Watch)
}

'list' {
    $path = "/api/jobs?limit=$Limit"
    if ($Status) { $path += "&status=$Status" }
    $data = Invoke-Api -Path $path
    # Format-Table в неинтерактивном режиме (вывод в конвейер) печатает пусто,
    # поэтому таблицу рисуем сами.
    Write-Host ("{0,-22} {1,-32} {2,-12} {3,8}  {4}" -f
        'ИДЕНТИФИКАТОР', 'ФАЙЛ', 'СОСТОЯНИЕ', 'RTF', 'МОДЕЛЬ') -ForegroundColor DarkGray
    foreach ($job in $data.items) {
        $rtf = if ($job.rtf) { '{0:N3}' -f $job.rtf } else { '—' }
        $name = if ($job.filename.Length -gt 30) { $job.filename.Substring(0, 29) + '…' } else { $job.filename }
        Write-Host ("{0,-22} {1,-32} {2,-12} {3,8}  {4}" -f
            $job.id.Substring(0, [Math]::Min(20, $job.id.Length)), $name, $job.status, $rtf, $job.model)
    }
    Write-Host "`nВсего: $($data.total)"
}

'models' {
    $path = '/api/models'
    if ($Language) { $path += "?language=$Language" }
    $data = Invoke-Api -Path $path
    $quality = @{ excellent = 'отличное'; good = 'хорошее'; fair = 'среднее';
                  poor = 'слабое'; none = '—' }
    $data.items | Group-Object family | ForEach-Object {
        Write-Host "`n$($_.Name)" -ForegroundColor White
        $_.Group | ForEach-Object {
            $wer = ($_.benchmarks | Where-Object { $_.language -eq 'ru' -and $_.metric -eq 'WER' } |
                    Measure-Object -Property value -Minimum).Minimum
            $werText = if ($wer) { '{0,5:N1} %' -f $wer } else { '     —' }
            Write-Host ("  {0,-34} {1,-10} WER ru {2}  {3}" -f
                $_.id, $quality[$_.ru_quality], $werText, $_.license)
        }
    }
    Write-Host "`nВсего моделей: $($data.total)"
}

'config' {
    $action = $Rest | Select-Object -First 1
    switch ($action) {
        'add' {
            $name = $Rest[1]; $url = $Rest[2]; $keyValue = if ($Rest.Count -gt 3) { $Rest[3] } else { '' }
            if (-not $name -or -not $url) { Write-Err2 'Использование: config add ИМЯ АДРЕС [КЛЮЧ]'; exit 2 }
            $all = Get-Profiles
            if ($all -isnot [hashtable]) {
                $converted = @{}
                $all.PSObject.Properties | ForEach-Object { $converted[$_.Name] = $_.Value }
                $all = $converted
            }
            $all[$name] = @{ url = $url.TrimEnd('/'); key = $keyValue }
            Save-Profiles $all
            Write-Ok2 "Профиль «$name» сохранён: $url"
        }
        'remove' {
            $name = $Rest[1]
            $all = @{}
            (Get-Profiles).PSObject.Properties | Where-Object { $_.Name -ne $name } |
                ForEach-Object { $all[$_.Name] = $_.Value }
            Save-Profiles $all
            Write-Ok2 "Профиль «$name» удалён"
        }
        default {
            $all = Get-Profiles
            if (-not $all.PSObject.Properties.Name) {
                Write-Host 'Профилей нет. Добавить: .\asrctl.ps1 config add <имя> <адрес> [ключ]'
            } else {
                Write-Host ("{0,-16} {1,-42} {2}" -f 'ПРОФИЛЬ', 'АДРЕС', 'КЛЮЧ') -ForegroundColor DarkGray
                $all.PSObject.Properties | ForEach-Object {
                    $maskedKey = if ($_.Value.key) {
                        $_.Value.key.Substring(0, [Math]::Min(6, $_.Value.key.Length)) + '…'
                    } else { '—' }
                    Write-Host ("{0,-16} {1,-42} {2}" -f $_.Name, $_.Value.url, $maskedKey)
                }
            }
        }
    }
}

'health' {
    $health = Invoke-Api -Path '/api/health'
    Write-Ok2 "Сервер $Server доступен"
    Write-Host "  Версия       $($health.version)"
    Write-Host "  Время работы $([int]$health.uptime_s) с"
    Write-Host ("  Очередь      {0}" -f $(if ($health.queue_paused) { 'на паузе' } else { 'работает' }))
}

'version' { Write-Host "asrctl $Version" }

default {
    Get-Help $PSCommandPath -Detailed
    Write-Host @'

Команды
  send ФАЙЛ [-Model M] [-Language ru] [-Formats srt,txt] [-Wait] [-Output КАТАЛОГ]
  send-dir КАТАЛОГ [-Recurse] [те же параметры]
  queue [-Watch]                 состояние очереди
  list [-Status ...] [-Limit N]  список заданий
  status ИДЕНТИФИКАТОР           карточка задания
  wait ИДЕНТИФИКАТОР             ждать завершения и скачать результат
  get ИДЕНТИФИКАТОР -Format srt -Output файл
  models [-Language ru]          каталог моделей
  health                         проверка доступности
  config add ИМЯ АДРЕС [КЛЮЧ] | config list | config remove ИМЯ

Общие параметры: -Server, -Key, -Profile, -TimeoutSec
'@
}

}
