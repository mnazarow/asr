<#
.SYNOPSIS
    asrctl — клиент ASR Hub для Windows.
.DESCRIPTION
    Отправка файлов на удалённый сервер распознавания, наблюдение за
    очередью, скачивание результатов. Профили серверов хранятся в
    %APPDATA%\asrctl\profiles.json.

    Работает в Windows PowerShell 5.1 и в PowerShell 7. Запросы идут через
    System.Net.Http.HttpClient: у Invoke-RestMethod в 5.1 нет -Form (файл не
    отправлялся вовсе), ответ без charset он читает как ISO-8859-1 (кириллица
    превращалась в мусор), а в PowerShell 7 при ошибке теряет текст ответа
    сервера.

    Коды возврата — как у asrctl для Linux: 0 — успех, 1 — ошибка, 2 —
    неверный вызов, 3 — сервер не ответил, 4 — сервер или прокси временно
    недоступен (502–504).
.EXAMPLE
    .\asrctl.ps1 config add рабочий https://asr.example.com -
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
    [string]$KeyFile = '',
    [string]$Profile = 'default',
    [string]$Model = '',
    [string]$Language = '',
    [string]$Formats = 'txt',
    [string]$Output = '.',
    [string]$Format = 'txt',
    [string]$Status = '',
    [int]$Priority = -1,
    [string]$Reference = '',
    [string]$Pattern = '',
    [string]$MoveTo = '',
    [int]$Limit = 25,
    [int]$TimeoutSec = 600,
    [int]$WaitLimitSec = -1,
    [switch]$Wait,
    [switch]$Recurse,
    [switch]$Watch,
    # Строка, поданная конвейером или перенаправлением ввода, — ключ для
    # `config add ИМЯ АДРЕС -`. Без параметра, принимающего конвейер,
    # PowerShell отвергал поданное с ошибкой «The input object cannot be
    # bound to any parameters», и профиль сохранялся без ключа.
    [Parameter(ValueFromPipeline = $true)][AllowEmptyString()][string]$PipedKey = ''
)

$ErrorActionPreference = 'Stop'
$Version = '3.1.22'
# APPDATA есть только в Windows; в WSL и PowerShell для Linux берём ~/.config
$ConfigBase = if ($env:APPDATA) { $env:APPDATA }
              elseif ($env:XDG_CONFIG_HOME) { $env:XDG_CONFIG_HOME }
              else { Join-Path $HOME '.config' }
$ConfigDir = Join-Path $ConfigBase 'asrctl'
$ConfigFile = Join-Path $ConfigDir 'profiles.json'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# HttpClient в Windows PowerShell 5.1 живёт в отдельной сборке. TLS 1.2 —
# явно: на старых сборках Windows по умолчанию остаются SSL3 и TLS 1.0, и к
# серверу за https не подключиться вовсе.
try { Add-Type -AssemblyName System.Net.Http -ErrorAction Stop } catch { }
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor
        [Net.SecurityProtocolType]::Tls12
} catch { }

# Общий срок ожидания результата: при остановленной очереди `wait` висел
# вечно, и убить его можно было лишь снаружи.
if ($WaitLimitSec -lt 0) {
    $WaitLimitSec = 3600
    $fromEnv = 0
    if ($env:ASRHUB_WAIT_LIMIT -and [int]::TryParse($env:ASRHUB_WAIT_LIMIT, [ref]$fromEnv) -and $fromEnv -ge 0) {
        $WaitLimitSec = $fromEnv
    }
}

function Write-Ok2   { param([string]$m) Write-Host "✓ $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "! $m" -ForegroundColor Yellow }
function Write-Err2  { param([string]$m) Write-Host "✕ $m" -ForegroundColor Red }
function Write-Hint2 { param([string]$m) Write-Host "  $m" -ForegroundColor DarkGray }

function Stop-Client {
    <# Сообщение и код возврата. #>
    param([string]$Message, [int]$Code = 1)
    Write-Err2 $Message
    exit $Code
}

function Get-Profiles {
    if (Test-Path -LiteralPath $ConfigFile) {
        try {
            $loaded = Get-Content -LiteralPath $ConfigFile -Raw -Encoding UTF8 | ConvertFrom-Json
            if ($loaded) { return $loaded }
        } catch { }
    }
    return [pscustomobject]@{}
}

function ConvertTo-ProfileTable {
    <# Профили словарём: ConvertFrom-Json даёт объект, а правится удобнее таблица. #>
    param($Profiles)
    $table = [ordered]@{}
    if ($Profiles) {
        foreach ($property in $Profiles.PSObject.Properties) { $table[$property.Name] = $property.Value }
    }
    return $table
}

function Save-Profiles {
    param($Profiles)
    if (-not (Test-Path -LiteralPath $ConfigDir)) { New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null }
    $json = $Profiles | ConvertTo-Json -Depth 5
    [System.IO.File]::WriteAllText($ConfigFile, $json, [System.Text.UTF8Encoding]::new($false))
}

function Test-ProfileExists {
    param($Profiles, [string]$Name)
    return [bool]($Profiles.PSObject.Properties | Where-Object { $_.Name -eq $Name })
}

# Разрешение адреса и ключа: параметры > переменные окружения > профиль.
if (-not $KeyFile -and $env:ASRHUB_KEY_FILE) { $KeyFile = $env:ASRHUB_KEY_FILE }
if ($KeyFile) {
    # Ключ в командной строке виден в списке процессов любому пользователю
    # машины; файл — нет.
    if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) { Stop-Client "Файл ключа не читается: $KeyFile" 2 }
    $Key = ((Get-Content -LiteralPath $KeyFile -TotalCount 1 -Encoding UTF8) -replace '\s', '')
}
$profiles = Get-Profiles
$hasProfile = Test-ProfileExists $profiles $Profile
if (-not $Server) {
    if ($env:ASRHUB_SERVER) { $Server = $env:ASRHUB_SERVER }
    elseif ($hasProfile) { $Server = "$($profiles.$Profile.url)" }
    else { $Server = 'http://127.0.0.1:8080' }
}
if (-not $Key) {
    if ($env:ASRHUB_KEY) { $Key = $env:ASRHUB_KEY }
    elseif ($hasProfile) { $Key = "$($profiles.$Profile.key)" }
}
$Server = $Server.TrimEnd('/')
# Ключ уходит заголовком X-API-Key, а заголовок — только ASCII. Кириллица
# или невидимый знак из скопированного текста раньше превращались в
# «сервер недоступен»: запрос не уходил вовсе, а причина терялась.
if ($Key -and $Key -notmatch '^[\x21-\x7e]+$') {
    Stop-Client 'Ключ доступа содержит пробелы или знаки вне латиницы — проверьте, тот ли это ключ.' 2
}

# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------

function Invoke-AsrRequest {
    <#
    .SYNOPSIS
        Один запрос к серверу. Объект: Status (0 — сервер не ответил), Body,
        Error (причина, если не ответил).
    .PARAMETER OutFile
        Сохранить тело ответа в файл (только при успешном ответе).
    #>
    param(
        [string]$Method = 'GET',
        [Parameter(Mandatory)][string]$Path,
        $Content = $null,
        [int]$Timeout = $TimeoutSec,
        [string]$OutFile = ''
    )
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = if ($Timeout -gt 0) { [TimeSpan]::FromSeconds($Timeout) }
                      else { [System.Threading.Timeout]::InfiniteTimeSpan }
    try {
        $request = [System.Net.Http.HttpRequestMessage]::new(
            [System.Net.Http.HttpMethod]::new($Method.ToUpperInvariant()), "$Server$Path")
        [void]$request.Headers.TryAddWithoutValidation('Accept', 'application/json')
        if ($Key) { [void]$request.Headers.TryAddWithoutValidation('X-API-Key', $Key) }
        if ($null -ne $Content) { $request.Content = $Content }
        $response = $client.SendAsync($request,
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        $code = [int]$response.StatusCode
        if ($OutFile -and $response.IsSuccessStatusCode) {
            $source = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $target = [System.IO.File]::Create($OutFile)
            try { $source.CopyTo($target) } finally { $target.Dispose(); $source.Dispose() }
            return [pscustomobject]@{ Status = $code; Body = ''; Error = '' }
        }
        # Тело — в UTF-8, если сервер не назвал кодировку: так его и шлёт
        # сервер. Invoke-RestMethod в 5.1 читал его как ISO-8859-1.
        $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
        $body = [System.Text.Encoding]::UTF8.GetString($bytes)
        return [pscustomobject]@{ Status = $code; Body = $body; Error = '' }
    } catch {
        $reason = $_.Exception
        while ($reason.InnerException) { $reason = $reason.InnerException }
        $text = "$($reason.Message)"
        if ($reason -is [System.Threading.Tasks.TaskCanceledException] -or
            $reason -is [System.TimeoutException]) {
            $text = "нет ответа за $Timeout с"
        }
        return [pscustomobject]@{ Status = 0; Body = ''; Error = $text }
    } finally {
        $client.Dispose()
    }
}

function Get-ApiResult {
    <#
    .SYNOPSIS
        Запрос и разбор ответа, без выхода. Объект: Code (0 — успех, 1, 3, 4
        — как коды возврата клиента), Data, Message, Hint.
    #>
    param([string]$Method = 'GET', [Parameter(Mandatory)][string]$Path, $Content = $null,
          [int]$Timeout = $TimeoutSec)
    $response = Invoke-AsrRequest -Method $Method -Path $Path -Content $Content -Timeout $Timeout
    if ($response.Status -eq 0) {
        return [pscustomobject]@{ Code = 3; Data = $null; Status = 0
                                  Message = "Сервер $Server недоступен: $($response.Error)"; Hint = '' }
    }
    $data = $null
    if ($response.Body) { try { $data = $response.Body | ConvertFrom-Json } catch { $data = $response.Body } }
    if ($response.Status -ge 200 -and $response.Status -lt 300) {
        return [pscustomobject]@{ Code = 0; Data = $data; Status = $response.Status; Message = ''; Hint = '' }
    }
    $message = "Ошибка $($response.Status)"; $hint = ''
    if ($response.Status -eq 401) {
        $message = 'Неверный или отсутствующий ключ доступа.'
        $hint = "Задайте: .\asrctl.ps1 config add $Profile $Server -"
    } elseif ($data -and $data.PSObject -and ($data.PSObject.Properties.Name -contains 'detail')) {
        $detail = $data.detail
        if ($detail -is [string]) { $message = $detail }
        elseif ($detail -and ($detail.PSObject.Properties.Name -contains 'message')) {
            $message = "$($detail.message)"
            if ($detail.PSObject.Properties.Name -contains 'hint' -and $detail.hint) { $hint = "$($detail.hint)" }
        }
    }
    # 502–504 — прокси или сервер в перезапуске: временное, и ожидание
    # результата такой ответ переживает.
    $code = if ($response.Status -in @(502, 503, 504)) { 4 } else { 1 }
    return [pscustomobject]@{ Code = $code; Data = $data; Status = $response.Status; Message = $message; Hint = $hint }
}

function Invoke-Api {
    <# Запрос; при неудаче — сообщение и выход с кодом клиента. #>
    param([string]$Method = 'GET', [Parameter(Mandatory)][string]$Path, $Content = $null,
          [int]$Timeout = $TimeoutSec)
    $result = Get-ApiResult -Method $Method -Path $Path -Content $Content -Timeout $Timeout
    if ($result.Code -ne 0) {
        Write-Err2 $result.Message
        if ($result.Hint) { Write-Hint2 $result.Hint }
        exit $result.Code
    }
    return $result.Data
}

function ConvertTo-Latin1Header {
    <#
      Значение заголовка части с именем файла: байты UTF-8 как есть, как их
      шлют браузеры и curl. .NET пишет заголовки частей в ISO-8859-1, то есть
      знак строки — один байт; строка из байтов UTF-8 и даёт на проводе UTF-8.
      Штатный ContentDispositionHeaderValue кодирует кириллицу словом MIME
      («=?utf-8?B?…?=»), и прежние версии python-multipart принимали его за
      имя файла целиком — вместе с потерянным расширением.
    #>
    param([string]$Text)
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
    return -join ($bytes | ForEach-Object { [char]$_ })
}

function New-UploadContent {
    param([System.IO.FileInfo]$Item, [string]$SettingsJson)
    $content = [System.Net.Http.MultipartFormDataContent]::new()
    $stream = [System.IO.File]::OpenRead($Item.FullName)
    $filePart = [System.Net.Http.StreamContent]::new($stream)
    $safe = $Item.Name -replace '["\\\r\n]', '_'
    [void]$filePart.Headers.TryAddWithoutValidation('Content-Disposition',
        (ConvertTo-Latin1Header "form-data; name=`"file`"; filename=`"$safe`""))
    [void]$filePart.Headers.TryAddWithoutValidation('Content-Type', 'application/octet-stream')
    $content.Add($filePart)
    # Текстовые поля — отдельными частями как есть: значение никогда не
    # читается из файла (у curl -F значение с «<» или «@» читалось).
    $content.Add([System.Net.Http.StringContent]::new($SettingsJson, [System.Text.Encoding]::UTF8), 'settings')
    if ($Priority -ge 0) {
        $content.Add([System.Net.Http.StringContent]::new("$Priority", [System.Text.Encoding]::UTF8), 'priority')
    }
    if ($Reference) {
        $content.Add([System.Net.Http.StringContent]::new($Reference, [System.Text.Encoding]::UTF8), 'reference_text')
    }
    # Запятая: составное тело перечислимо, и без неё PowerShell вернул бы
    # не его, а массив частей.
    return ,$content
}

function Send-File {
    <# Отправка файла. Объект: Id (пусто при неудаче) и Code — код клиента. #>
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Err2 "Файл не найден: $Path"
        return [pscustomobject]@{ Id = ''; Code = 1 }
    }
    $settings = [ordered]@{}
    if ($Model) { $settings['model'] = $Model }
    if ($Language) { $settings['language'] = $Language }
    if ($Formats) { $settings['output_formats'] = @($Formats -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    $settingsJson = $settings | ConvertTo-Json -Compress
    if (-not $settingsJson -or $settingsJson -eq 'null') { $settingsJson = '{}' }

    $item = Get-Item -LiteralPath $Path
    Write-Host "Отправка: $($item.Name) ($([math]::Round($item.Length / 1MB, 1)) МБ)"
    # Загрузку общий срок запроса не ограничивает: большой файл по медленной
    # сети идёт дольше десяти минут. Срок — из расчёта 128 КБ/с, не меньше
    # -TimeoutSec.
    $uploadTimeout = [Math]::Max($TimeoutSec, 120 + [int][Math]::Ceiling($item.Length / 128KB))
    if ($TimeoutSec -le 0) { $uploadTimeout = 0 }
    $content = New-UploadContent -Item $item -SettingsJson $settingsJson
    try {
        $result = Get-ApiResult -Method 'POST' -Path '/api/jobs' -Content $content -Timeout $uploadTimeout
    } finally { $content.Dispose() }
    if ($result.Code -ne 0) {
        Write-Err2 "Не удалось отправить файл: $($result.Message)"
        if ($result.Hint) { Write-Hint2 $result.Hint }
        return [pscustomobject]@{ Id = ''; Code = $result.Code }
    }
    $id = if ($result.Data -and $result.Data.PSObject.Properties.Name -contains 'id') { "$($result.Data.id)" } else { '' }
    if (-not $id) {
        Write-Err2 'Сервер не вернул идентификатор задания.'
        return [pscustomobject]@{ Id = ''; Code = 1 }
    }
    Write-Ok2 "Задание создано: $id"
    return [pscustomobject]@{ Id = $id; Code = 0 }
}

function Get-SafeName {
    <# Имя файла из ответа сервера — только имя: «..\..\x» писало мимо каталога. #>
    param([string]$Name)
    $leaf = ("$Name" -split '[\\/]')[-1]
    $leaf = $leaf.TrimStart('.') -replace '[\x00-\x1f:*?"<>|]', '_'
    if (-not $leaf) { $leaf = 'result' }
    return $leaf
}

function Get-Result {
    <# Скачивание результата. Возвращает код клиента. #>
    param([string]$JobId, [string]$Fmt, [string]$Destination)
    $isDir = (Test-Path -LiteralPath $Destination -PathType Container) -or
             $Destination.EndsWith('\') -or $Destination.EndsWith('/') -or
             (-not [System.IO.Path]::GetExtension($Destination))
    if ($isDir) {
        if (-not (Test-Path -LiteralPath $Destination)) { New-Item -ItemType Directory -Path $Destination -Force | Out-Null }
        $job = Get-ApiResult -Path "/api/jobs/$JobId"
        $name = 'result'
        if ($job.Code -eq 0 -and $job.Data -and $job.Data.filename) {
            $name = [System.IO.Path]::GetFileNameWithoutExtension((Get-SafeName $job.Data.filename))
        }
        $target = Join-Path $Destination "$(Get-SafeName $name).$Fmt"
    } else {
        $target = $Destination
        $parent = Split-Path -Parent $target
        if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
    }
    # Относительный путь — от текущего каталога PowerShell: у процесса он
    # свой, и [IO.Path]::GetFullPath писал бы файл не туда.
    $target = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($target)
    $response = Invoke-AsrRequest -Path "/api/jobs/$JobId/download?fmt=$([uri]::EscapeDataString($Fmt))" -OutFile $target
    if ($response.Status -ge 200 -and $response.Status -lt 300) {
        Write-Ok2 "Сохранено: $target"
        return 0
    }
    if ($response.Status -eq 0) {
        Write-Err2 "Не удалось скачать формат ${Fmt}: сервер не ответил ($($response.Error))"
        return 3
    }
    Write-Err2 "Не удалось скачать формат ${Fmt}: ошибка $($response.Status)"
    return $(if ($response.Status -in @(502, 503, 504)) { 4 } else { 1 })
}

function Wait-Job {
    <# Ожидание результата. Возвращает код клиента: 0 — готово и скачано. #>
    param([string]$JobId)
    $spinner = '|', '/', '-', '\'
    $i = 0; $failures = 0
    $deadline = if ($WaitLimitSec -gt 0) { (Get-Date).AddSeconds($WaitLimitSec) } else { $null }
    while ($true) {
        if ($deadline -and (Get-Date) -ge $deadline) {
            Write-Host "`r! ожидание прервано по сроку ($WaitLimitSec с)                 " -ForegroundColor Yellow
            Write-Host "Задание осталось в очереди: .\asrctl.ps1 status $JobId"
            Write-Host "Увеличить срок: .\asrctl.ps1 wait $JobId -WaitLimitSec 7200"
            return 2
        }
        $result = Get-ApiResult -Path "/api/jobs/$JobId"
        if ($result.Code -ne 0) {
            # Сервер в перезапуске (обновление, падение службы) — не повод
            # бросать ожидание: до пяти повторов подряд.
            if ($result.Code -in @(3, 4) -and $failures -lt 5) {
                $failures++
                Write-Warn2 "сервер не ответил — повтор через 5 с ($failures/5)"
                Start-Sleep -Seconds 5
                continue
            }
            Write-Err2 $result.Message
            if ($result.Hint) { Write-Hint2 $result.Hint }
            return $result.Code
        }
        $failures = 0
        $job = $result.Data
        switch ("$($job.status)") {
            'completed' {
                Write-Host "`r✓ готово                                              " -ForegroundColor Green
                $rtf = if ($null -ne $job.rtf) { [math]::Round([double]$job.rtf, 3) } else { '—' }
                Write-Host "  RTF $rtf · слов $($job.words_count)"
                $code = 0
                foreach ($fmt in ($Formats -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
                    $got = Get-Result -JobId $JobId -Fmt $fmt -Destination $Output
                    if ($got -ne 0) { $code = $got }
                }
                return $code
            }
            'failed' {
                Write-Host "`r✕ ошибка                                              " -ForegroundColor Red
                Write-Host "  $($job.error_message)"
                if ($job.error_hint) { Write-Host "  $($job.error_hint)" -ForegroundColor DarkGray }
                return 1
            }
            'cancelled' { Write-Host "`r! отменено                                    " -ForegroundColor Yellow; return 1 }
            'paused' {
                Write-Host ("`r{0} задание приостановлено — .\asrctl.ps1 resume {1}      " -f $spinner[$i % 4], $JobId) -NoNewline
                $i++
                Start-Sleep -Seconds 2
            }
            default {
                $progress = 0.0
                try { if ($null -ne $job.progress) { $progress = [double]$job.progress } } catch { $progress = 0.0 }
                $percent = [int]($progress * 100)
                Write-Host ("`r{0} {1} {2}%  {3}        " -f $spinner[$i % 4], $job.status, $percent, $job.stage) -NoNewline
                $i++
                Start-Sleep -Seconds 2
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------

switch ($Command) {

'send' {
    $file = $Rest | Select-Object -First 1
    if (-not $file) { Stop-Client 'Укажите файл: .\asrctl.ps1 send запись.mp3' 2 }
    if ($PSBoundParameters.ContainsKey('Priority') -and ($Priority -lt 0 -or $Priority -gt 100)) {
        Stop-Client "Приоритет — число от 0 до 100: $Priority" 2
    }
    $sent = Send-File -Path $file
    if (-not $sent.Id) { exit $sent.Code }
    if ($Wait) { exit (Wait-Job -JobId $sent.Id) }
    Write-Host "Следить: .\asrctl.ps1 status $($sent.Id)"
}

'send-dir' {
    $dir = $Rest | Select-Object -First 1
    if (-not $dir -or -not (Test-Path -LiteralPath $dir -PathType Container)) { Stop-Client "Каталог не найден: $dir" 2 }
    if ($PSBoundParameters.ContainsKey('Priority') -and ($Priority -lt 0 -or $Priority -gt 100)) {
        Stop-Client "Приоритет — число от 0 до 100: $Priority" 2
    }
    # Каталог отправленных — чтобы повторный запуск (из планировщика) не слал
    # те же файлы снова: сервер их не пересчитывает, но каждый повтор
    # заводил задание-копию и тратил квоту.
    $root = (Resolve-Path -LiteralPath $dir).ProviderPath.TrimEnd('\', '/')
    if ($MoveTo) {
        if (-not (Test-Path -LiteralPath $MoveTo)) {
            try { New-Item -ItemType Directory -Path $MoveTo -Force | Out-Null }
            catch { Stop-Client "Не удалось создать каталог: $MoveTo" 2 }
        }
        $moveRoot = (Resolve-Path -LiteralPath $MoveTo).ProviderPath.TrimEnd('\', '/')
        if ($moveRoot -eq $root) { Stop-Client '-MoveTo не может совпадать с каталогом отправки' 2 }
        if ($Recurse -and ($moveRoot + [IO.Path]::DirectorySeparatorChar).StartsWith(
                $root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Client 'С -Recurse каталог -MoveTo не может лежать внутри отправляемого: перенесённое найдётся снова' 2
        }
    }
    # Отбор по расширению, а не -Include: без -Recurse и без маски в пути
    # -Include не находит ничего, и `send-dir КАТАЛОГ` отвечал «нет файлов».
    $extensions = @('.wav', '.mp3', '.m4a', '.flac', '.ogg', '.opus', '.mp4', '.mkv', '.mov', '.aac', '.wma')
    $files = @(Get-ChildItem -LiteralPath $dir -File -Recurse:$Recurse -ErrorAction SilentlyContinue |
        Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() } |
        Where-Object { -not $Pattern -or $_.Name -like $Pattern } |
        Sort-Object FullName)
    if ($files.Count -eq 0) { Stop-Client 'В каталоге нет поддерживаемых файлов.' 2 }
    Write-Host "Найдено файлов: $($files.Count)"
    $index = 0; $failed = 0; $worst = 0
    foreach ($file in $files) {
        $index++
        Write-Host "[$index/$($files.Count)] $($file.Name)" -ForegroundColor Blue
        $sent = Send-File -Path $file.FullName
        if (-not $sent.Id) {
            $failed++
            if ($sent.Code -gt $worst) { $worst = $sent.Code }
            continue
        }
        if ($MoveTo) {
            $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/')
            $target = Join-Path $moveRoot $relative
            try {
                $parent = Split-Path -Parent $target
                if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
                if (Test-Path -LiteralPath $target) {
                    $target = Join-Path $parent ("{0}.{1}{2}" -f [IO.Path]::GetFileNameWithoutExtension($target),
                        (Get-Date -Format 'yyyyMMdd-HHmmss'), [IO.Path]::GetExtension($target))
                }
                Move-Item -LiteralPath $file.FullName -Destination $target
            } catch {
                Write-Warn2 "  отправлен, но не перенесён в ${MoveTo}: следующий запуск отправит его снова"
            }
        }
        if ($Wait) {
            $code = Wait-Job -JobId $sent.Id
            if ($code -ne 0) { $failed++; if ($code -gt $worst) { $worst = $code } }
        }
    }
    Write-Ok2 "Отправлено: $($files.Count - $failed) из $($files.Count)"
    if ($failed) { Write-Warn2 "Не отправлено или не готово: $failed"; exit $(if ($worst) { $worst } else { 1 }) }
}

'status' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Stop-Client 'Укажите идентификатор задания.' 2 }
    $job = Invoke-Api -Path "/api/jobs/$jobId"
    Write-Host $job.filename -ForegroundColor White
    $progress = 0.0
    try { if ($null -ne $job.progress) { $progress = [double]$job.progress } } catch { }
    Write-Host ("  Состояние    {0} ({1} %)" -f $job.status, [int]($progress * 100))
    Write-Host ("  Этап         {0}" -f $job.stage)
    Write-Host ("  Модель       {0}" -f $job.model)
    if ($null -ne $job.media_duration_s) { Write-Host ("  Длительность {0} с" -f [math]::Round([double]$job.media_duration_s, 1)) }
    if ($job.rtf) { Write-Host ("  RTF          {0}" -f [math]::Round([double]$job.rtf, 3)) }
    if ($job.error_message) {
        Write-Host ("  Ошибка       {0}" -f $job.error_message) -ForegroundColor Red
        if ($job.error_hint) { Write-Host ("  {0}" -f $job.error_hint) -ForegroundColor DarkGray }
    }
}

'wait' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Stop-Client 'Укажите идентификатор.' 2 }
    exit (Wait-Job -JobId $jobId)
}

'resume' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Stop-Client 'Укажите идентификатор задания.' 2 }
    $null = Invoke-Api -Method 'POST' -Path "/api/jobs/$jobId/resume"
    Write-Ok2 "Задание $jobId продолжено"
}

'get' {
    $jobId = $Rest | Select-Object -First 1
    if (-not $jobId) { Stop-Client 'Укажите идентификатор.' 2 }
    exit (Get-Result -JobId $jobId -Fmt $Format -Destination $Output)
}

'queue' {
    do {
        $q = Invoke-Api -Path '/api/queue'
        if ($Watch) { Clear-Host }
        $pausedText = if ($q.paused) { 'ПАУЗА' } else { '' }
        Write-Host ("Очередь  ожидают: {0}  выполняется: {1}  воркеров: {2}  {3}" -f
            $q.queue_depth, $q.active, $q.worker_count, $pausedText) -ForegroundColor White
        if ($q.counts) {
            Write-Host ("готово {0} · ошибок {1} · отменено {2}" -f
                $q.counts.completed, $q.counts.failed, $q.counts.cancelled) -ForegroundColor DarkGray
        }
        Write-Host ''
        if (-not $q.items) { Write-Host 'Очередь пуста' -ForegroundColor DarkGray }
        foreach ($job in (@($q.items) | Select-Object -First 25)) {
            $progress = 0.0
            try { if ($null -ne $job.progress) { $progress = [double]$job.progress } } catch { }
            $percent = [int]($progress * 100)
            $barLength = [Math]::Max(0, [Math]::Min(20, [int]($progress * 20)))
            $bar = ('█' * $barLength) + ('·' * (20 - $barLength))
            $colour = switch ($job.status) {
                'running' { 'Green' } 'failed' { 'Red' } 'retry' { 'Yellow' } default { 'Gray' }
            }
            $name = "$($job.filename)"
            Write-Host ("  {0,-10} {1,-36} {2} {3,3}%  {4}" -f
                $job.status, $name.Substring(0, [Math]::Min(34, $name.Length)),
                $bar, $percent, $job.stage) -ForegroundColor $colour
        }
        if ($Watch) { Start-Sleep -Seconds 3 }
    } while ($Watch)
}

'list' {
    $path = "/api/jobs?limit=$Limit"
    if ($Status) { $path += "&status=$([uri]::EscapeDataString($Status))" }
    $data = Invoke-Api -Path $path
    # Format-Table в неинтерактивном режиме (вывод в конвейер) печатает пусто,
    # поэтому таблицу рисуем сами.
    Write-Host ("{0,-22} {1,-32} {2,-12} {3,8}  {4}" -f
        'ИДЕНТИФИКАТОР', 'ФАЙЛ', 'СОСТОЯНИЕ', 'RTF', 'МОДЕЛЬ') -ForegroundColor DarkGray
    foreach ($job in @($data.items)) {
        $rtf = if ($job.rtf) { '{0:N3}' -f [double]$job.rtf } else { '—' }
        $name = "$($job.filename)"
        if ($name.Length -gt 30) { $name = $name.Substring(0, 29) + '…' }
        $id = "$($job.id)"
        Write-Host ("{0,-22} {1,-32} {2,-12} {3,8}  {4}" -f
            $id.Substring(0, [Math]::Min(20, $id.Length)), $name, $job.status, $rtf, $job.model)
    }
    Write-Host "`nВсего: $($data.total)"
}

'models' {
    if ($Rest.Count -gt 0) { Stop-Client "Неизвестный параметр: $($Rest[0]). Язык задаётся ключом -Language." 2 }
    $path = '/api/models'
    if ($Language) { $path += "?language=$([uri]::EscapeDataString($Language))" }
    $data = Invoke-Api -Path $path
    $quality = @{ excellent = 'отличное'; good = 'хорошее'; fair = 'среднее';
                  poor = 'слабое'; none = '—' }
    @($data.items) | Group-Object family | ForEach-Object {
        Write-Host "`n$($_.Name)" -ForegroundColor White
        $_.Group | ForEach-Object {
            $wer = (@($_.benchmarks) | Where-Object { $_.language -eq 'ru' -and $_.metric -eq 'WER' } |
                    Measure-Object -Property value -Minimum).Minimum
            $werText = if ($wer) { '{0,5:N1} %' -f $wer } else { '     —' }
            Write-Host ("  {0,-34} {1,-10} WER ru {2}  {3}" -f
                $_.id, $quality["$($_.ru_quality)"], $werText, $_.license)
        }
    }
    Write-Host "`nВсего моделей: $($data.total)"
}

'config' {
    $action = $Rest | Select-Object -First 1
    switch ($action) {
        'add' {
            $name = if ($Rest.Count -gt 1) { $Rest[1] } else { '' }
            $url = if ($Rest.Count -gt 2) { $Rest[2] } else { '' }
            $keyValue = if ($Rest.Count -gt 3) { $Rest[3] } else { '' }
            if (-not $name -or -not $url) { Stop-Client 'Использование: config add ИМЯ АДРЕС [КЛЮЧ|-]' 2 }
            if ($keyValue -eq '-') {
                # Ключ со стандартного ввода: в командной строке он виден в
                # списке процессов и остаётся в истории консоли.
                $line = if ($PipedKey) { $PipedKey } else { "$([Console]::In.ReadLine())" }
                $keyValue = ($line -replace '\s', '')
            } elseif (-not $keyValue -and -not [Console]::IsInputRedirected) {
                $secure = Read-Host 'Ключ доступа (Enter — без ключа)' -AsSecureString
                $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
                    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
                $keyValue = "$plain" -replace '\s', ''
            }
            $all = ConvertTo-ProfileTable (Get-Profiles)
            $all[$name] = [ordered]@{ url = $url.TrimEnd('/'); key = $keyValue }
            Save-Profiles $all
            Write-Ok2 "Профиль «$name» сохранён: $url"
        }
        'remove' {
            $name = if ($Rest.Count -gt 1) { $Rest[1] } else { '' }
            if (-not $name) { Stop-Client 'Использование: config remove ИМЯ' 2 }
            $all = ConvertTo-ProfileTable (Get-Profiles)
            if (-not $all.Contains($name)) { Stop-Client "Профиля «$name» нет." 1 }
            $all.Remove($name)
            Save-Profiles $all
            Write-Ok2 "Профиль «$name» удалён"
        }
        default {
            $all = ConvertTo-ProfileTable (Get-Profiles)
            if ($all.Count -eq 0) {
                Write-Host 'Профилей нет. Добавить: .\asrctl.ps1 config add <имя> <адрес> [ключ|-]'
            } else {
                Write-Host ("{0,-16} {1,-42} {2}" -f 'ПРОФИЛЬ', 'АДРЕС', 'КЛЮЧ') -ForegroundColor DarkGray
                foreach ($entry in $all.GetEnumerator()) {
                    $value = $entry.Value
                    $keyText = "$($value.key)"
                    $maskedKey = if ($keyText) { $keyText.Substring(0, [Math]::Min(6, $keyText.Length)) + '…' } else { '—' }
                    Write-Host ("{0,-16} {1,-42} {2}" -f $entry.Key, $value.url, $maskedKey)
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

{ $_ -in @('help', '-h', '--help', '/?') } {
    Get-Help $PSCommandPath -Detailed
    Write-Host @'

Команды
  send ФАЙЛ [-Model M] [-Language ru] [-Formats srt,txt] [-Priority 0..100]
            [-Reference ЭТАЛОН] [-Wait] [-Output КАТАЛОГ]
  send-dir КАТАЛОГ [-Recurse] [-Pattern '*.mp3'] [-MoveTo КАТАЛОГ]
            [те же параметры]  -MoveTo — куда переносить отправленное,
                               чтобы запуск из планировщика не слал его снова
  queue [-Watch]                 состояние очереди
  list [-Status ...] [-Limit N]  список заданий
  status ИДЕНТИФИКАТОР           карточка задания
  wait ИДЕНТИФИКАТОР             ждать завершения и скачать результат
                                 (сервер в перезапуске — повтор до пяти раз)
  resume ИДЕНТИФИКАТОР           продолжить приостановленное задание
  get ИДЕНТИФИКАТОР -Format srt -Output файл
  models [-Language ru]          каталог моделей
  health                         проверка доступности
  config add ИМЯ АДРЕС [КЛЮЧ|-]  «-» — ключ со стандартного ввода; без ключа
                                 в консоли клиент спросит его, не показывая
  config list | config remove ИМЯ

Общие параметры
  -Server АДРЕС     адрес сервера; перекрывает профиль (или ASRHUB_SERVER)
  -Key КЛЮЧ         ключ доступа (виден в списке процессов — лучше -KeyFile)
  -KeyFile ФАЙЛ     ключ из файла (или ASRHUB_KEY_FILE)
  -Profile ИМЯ      профиль из config add
  -TimeoutSec N     срок одного запроса, с (по умолчанию 600); загрузку файла
                    он не ограничивает — ей срок из расчёта 128 КБ/с
  -WaitLimitSec N   общий срок ожидания результата (по умолчанию 3600,
                    0 — без ограничения; или ASRHUB_WAIT_LIMIT)

Коды возврата: 0 — успех, 1 — ошибка, 2 — неверный вызов, 3 — сервер не
ответил, 4 — сервер или прокси временно недоступен.
'@
}

default {
    Write-Err2 "Неизвестная команда: $Command"
    Write-Hint2 'Список команд: .\asrctl.ps1 help'
    exit 2
}

}
