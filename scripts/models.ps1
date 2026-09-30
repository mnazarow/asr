<#
.SYNOPSIS
    Менеджер моделей и движков ASR Hub для Windows.
.DESCRIPTION
    Установка — та же, что у остальных сценариев: заданная ключом -Prefix,
    та, из которой запущен сценарий, или единственная известная на машине.
.EXAMPLE
    powershell -File scripts\models.ps1 -Action list
    powershell -File scripts\models.ps1 -Action list -Language ru
    powershell -File scripts\models.ps1 -Action info -Model gigaam-v3-rnnt
    powershell -File scripts\models.ps1 -Action download -Model gigaam-v3-e2e-rnnt
    powershell -File scripts\models.ps1 -Action install-engine -Engine faster_whisper
#>
[CmdletBinding()]
param(
    [ValidateSet('list', 'info', 'download', 'remove', 'verify', 'engines',
                 'install-engine', 'remove-engine', 'disk')]
    [string]$Action = 'list',
    [string]$Model = '',
    [string]$Engine = '',
    [string]$Prefix = '',
    [string]$DataDir = '',
    [string]$Language = '',
    [switch]$Installed,
    [switch]$Force,
    [switch]$Yes,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
# Из install.ps1 этот скрипт зовут в том же процессе, и модуль там уже
# загружен. Повторный Import-Module -Force пересоздавал его и сбрасывал у
# ВЫЗЫВАЮЩЕГО пробный запуск, список отката и журнал: `install.ps1 -DryRun`
# поверх установки качал модели и пересоздавал службу по-настоящему, а
# обычная установка после загрузки моделей теряла откат.
if (-not (Get-Module -Name Common)) {
    Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1')
}
# Согласие и пробный запуск — только на время этого сценария: модуль общий
# с вызывающим, и включённое здесь оставалось бы включённым у него.
$assumeYesBefore = Get-AssumeYes
$dryRunBefore = Get-DryRun
if ($Yes) { Set-AssumeYes $true }
if ($DryRun) { Set-DryRun $true }

try {

$resolved = Resolve-AsrInstallation -ScriptDir $PSScriptRoot -Prefix $Prefix -DataDir $DataDir
if (-not $resolved.Prefix -and @($resolved.Candidates).Count -gt 0 -and
    $Action -notin @('list', 'info', 'engines', 'disk')) {
    Show-AmbiguousInstallation $resolved.Candidates 'models.ps1'
    exit 2
}
# Без установки — код рядом со сценарием: смотреть каталог моделей можно и
# из распакованного дистрибутива.
$Prefix = if ($resolved.Prefix) { $resolved.Prefix } else { Split-Path -Parent $PSScriptRoot }
$DataDir = if ($resolved.DataDir) { $resolved.DataDir }
           elseif ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'ASRHub\data' }
           else { Join-Path $Prefix 'data' }

$modelsDir = Join-Path $DataDir 'models'

# Пробный запуск ничего не качает, не удаляет и не ставит. Смотреть список,
# карточку модели и занятое место можно как обычно.
if ((Get-DryRun) -and $Action -in @('download', 'remove', 'install-engine', 'remove-engine')) {
    $what = if ($Model) { $Model } else { $Engine }
    Write-Host "[пробный запуск] models.ps1 -Action $Action $what" -ForegroundColor Yellow
    return
}
$venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { Find-Python }
if (-not $python) { Write-Err 'Не найден Python.'; exit 1 }
$serverDir = Join-Path $Prefix 'server'

function Invoke-CatalogScript {
    <# Код на Python из временного файла; код возврата — в $LASTEXITCODE. #>
    param([string]$Code, [string[]]$Arguments = @())
    $temp = Join-Path ([System.IO.Path]::GetTempPath()) "asrhub-$([guid]::NewGuid().ToString('N')).py"
    [System.IO.File]::WriteAllText($temp, $Code, [System.Text.UTF8Encoding]::new($false))
    Push-Location -LiteralPath $serverDir
    try {
        & $python $temp @Arguments
    } finally { Pop-Location; Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
}

switch ($Action) {

'list' {
    Invoke-CatalogScript -Arguments @($(if ($Installed) { '1' } else { '0' }), $Language, $modelsDir) -Code @'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import MODELS, mean_ru_wer
# Поиск весов — тот же, которым пользуется сам сервер. Своя копия знала
# только раскладку Hugging Face и показывала скачанный GigaAM (он лежит
# одним файлом .ckpt) незагруженным.
from asrhub import model_files

only_installed = sys.argv[1] == "1"
language = sys.argv[2]
models_dir = Path(sys.argv[3])

quality = {"excellent": "отличное", "good": "хорошее", "fair": "среднее",
           "poor": "слабое", "none": "—"}
families, installed, total_mb = {}, 0, 0.0
for spec in MODELS:
    if language and language not in spec.languages and not any(
            x.startswith("multi") for x in spec.languages):
        continue
    path = model_files.find_local(models_dir, spec.source, spec.revision or "")
    if only_installed and path is None:
        continue
    families.setdefault(spec.family, []).append((spec, path))

for family in sorted(families):
    print(f"\n{family}")
    for spec, path in families[family]:
        mark = "[+]" if path else "[ ]"
        mb = model_files.directory_size(path) / 1024 / 1024 if path else 0.0
        if path:
            installed += 1
            total_mb += mb
        wer = mean_ru_wer(spec)
        wer_text = f"{wer:5.1f} %" if wer is not None else "     -"
        size = f"{mb:7.0f} МБ" if path else (f"{spec.disk_mb:7d} МБ" if spec.disk_mb else "        -")
        print(f"  {mark} {spec.id:<34} {quality[spec.ru_quality.value]:<10} "
              f"WER ru {wer_text}  {size}  {spec.license}")

print(f"\n[+] загружено   [ ] не загружено")
print(f"Загружено моделей: {installed}, занято {total_mb/1024:.1f} ГБ")
print(f"Каталог моделей: {models_dir}")
'@
}

'info' {
    if (-not $Model) { Write-Err 'Укажите модель: -Model <идентификатор>'; exit 2 }
    Invoke-CatalogScript -Arguments @($Model) -Code @'
import sys
sys.path.insert(0, ".")
from asrhub.catalog import get_model, get_engine

spec = get_model(sys.argv[1])
if spec is None:
    from asrhub.catalog import MODELS
    close = [m.id for m in MODELS if sys.argv[1].lower() in m.id.lower()][:6]
    print(f"Модель «{sys.argv[1]}» не найдена.")
    if close:
        print("Возможно: " + ", ".join(close))
    raise SystemExit(1)

print(f"\n{spec.name}  ({spec.id})\n")
rows = [
    ("Семейство", spec.family), ("Движок", spec.engine),
    ("Источник", spec.source + (f" · ветка {spec.revision}" if spec.revision else "")),
    ("Лицензия", spec.license + ("  — коммерческое использование разрешено"
                                  if spec.commercial_use else "  — НЕкоммерческая")),
    ("Языки", ", ".join(spec.languages)),
    ("Качество на русском", spec.ru_quality.value),
    ("Параметров", f"{spec.params_m} млн" if spec.params_m else "—"),
    ("Размер на диске", f"{spec.disk_mb} МБ" if spec.disk_mb else "—"),
    ("Видеопамять", f"{spec.vram_gb} ГБ" if spec.vram_gb else "—"),
    ("Потоковый режим", "да" if spec.streaming else "нет"),
    ("Пунктуация", "да" if spec.punctuation else "нет"),
    ("Диаризация", "да" if spec.diarization else "нет"),
]
for key, value in rows:
    print(f"  {key:<22} {value}")
if spec.benchmarks:
    print("\nИзмерения качества")
    for b in spec.benchmarks:
        print(f"  {b.dataset:<30} {b.metric} {b.value:6.2f}   {b.source[:60]}")
if spec.strengths:
    print("\nСильные стороны")
    for item in spec.strengths:
        print(f"  + {item}")
if spec.weaknesses:
    print("\nОграничения")
    for item in spec.weaknesses:
        print(f"  - {item}")
print()
'@
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

'download' {
    if (-not $Model) { Write-Err 'Укажите модель: -Model <идентификатор>'; exit 2 }
    if (-not (Test-Path $modelsDir)) { New-Item -ItemType Directory -Path $modelsDir -Force | Out-Null }
    Write-Info "Загрузка модели «$Model» в $modelsDir"
    Invoke-CatalogScript -Arguments @($Model, $modelsDir, $(if ($Force) { '1' } else { '0' })) -Code @'
import os, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model

spec = get_model(sys.argv[1])
if spec is None:
    print(f"Модель «{sys.argv[1]}» не найдена."); raise SystemExit(1)
models_dir = Path(sys.argv[2]); models_dir.mkdir(parents=True, exist_ok=True)
force = sys.argv[3] == "1"
token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")

if spec.engine == "demo":
    print("Демонстрационный движок не требует загрузки весов."); raise SystemExit(0)

if spec.gated and not token:
    print("Внимание: модель требует принятия лицензии и токена Hugging Face.")
    print("Задайте токен: $env:HF_TOKEN='hf_xxx'")

# GigaAM качает веса не с Hugging Face, а со своего CDN, одним файлом
# «<вариант>.ckpt». Снапшот репозитория ai-sage/GigaAM-v3, который качался
# раньше, загрузчик не открывает никогда: команда отрабатывала успешно,
# место занимала, а движок потом всё равно шёл в интернет. Та же функция,
# что у раздела «Модели» и models.sh.
if spec.engine == "gigaam":
    from asrhub.engines.gigaam_engine import download_weights, variant_name, weights_file
    from asrhub.errors import DependencyMissing
    target = models_dir / weights_file(spec.source, spec.revision or "")
    if target.exists() and not force:
        print(f"Уже загружено: {target}"); raise SystemExit(0)
    print(f"Скачивание варианта «{variant_name(spec.source, spec.revision or '')}» в {models_dir}")
    try:
        target = download_weights(spec.source, spec.revision or "", models_dir)
    except DependencyMissing:
        print("Движок gigaam не установлен.")
        print("Установите: powershell -File scripts\\models.ps1 -Action install-engine -Engine gigaam")
        raise SystemExit(1)
    except Exception as exc:
        print(f"\nОшибка загрузки: {exc}"); raise SystemExit(1)
    print(f"Готово: {target if target.exists() else models_dir}"); raise SystemExit(0)

# whisper.cpp — один файл ggml из репозитория ggerganov/whisper.cpp. Общая
# дорога ниже принимала «large-v3-turbo» за модель faster-whisper и клала на
# диск чужие веса, а варианты «…-q5_0» не находила вовсе.
if spec.engine == "whisper_cpp":
    from asrhub.engines.whisper_cpp_engine import download_weights
    from asrhub.errors import ASRHubError
    print(f"Скачивание {spec.source} (примерно {spec.disk_mb or '?'} МБ)")
    try:
        target = download_weights(spec.source, models_dir, force=force, token=token)
    except ASRHubError as exc:
        print(f"Ошибка загрузки: {exc.message}")
        if exc.hint:
            print(exc.hint)
        raise SystemExit(1)
    print(f"Готово: {target}"); raise SystemExit(0)

if spec.source.startswith("http"):
    import zipfile, urllib.request
    target = models_dir / "vosk"; target.mkdir(parents=True, exist_ok=True)
    archive = target / spec.source.rsplit("/", 1)[-1]
    print(f"Скачивание {spec.source}")
    urllib.request.urlretrieve(spec.source, archive)
    print("Распаковка…")
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(target)
    archive.unlink(missing_ok=True)
    print(f"Готово: {target}"); raise SystemExit(0)

try:
    from huggingface_hub import snapshot_download
except ModuleNotFoundError:
    print("Не установлен huggingface_hub: pip install huggingface_hub"); raise SystemExit(1)

# Репозиторий — из той же таблицы, по которой сервер ищет веса: у
# faster-whisper источник — короткое имя («small»), а качать надо
# Systran/faster-whisper-small. Раньше запрашивался репозиторий «small», и
# отказ Hugging Face разбирался как «модель закрыта лицензией».
from asrhub.model_files import huggingface_repo
repo = huggingface_repo(spec.source)
print(f"Скачивание {repo}" + (f" (ветка {spec.revision})" if spec.revision else ""))
print(f"Ожидаемый размер: примерно {spec.disk_mb or '?'} МБ")
try:
    path = snapshot_download(repo_id=repo, revision=spec.revision or None,
                             cache_dir=str(models_dir), force_download=force, token=token)
except Exception as exc:
    text = str(exc).lower()
    print(f"\nОшибка загрузки: {exc}")
    if "401" in text or "gated" in text or "authenticated" in text:
        print(f"\nМодель закрыта лицензией:")
        print(f"  1. Откройте https://huggingface.co/{repo} и примите условия")
        print("  2. Создайте токен: https://huggingface.co/settings/tokens")
        print("  3. $env:HF_TOKEN='hf_xxxxx' и повторите")
    elif "connection" in text or "timeout" in text or "resolve" in text:
        print("\nПроблема с сетью. Проверьте доступ к huggingface.co и настройки прокси.")
    elif "space" in text:
        print("\nНа диске не хватает места.")
    raise SystemExit(1)
print(f"Готово: {path}")
'@
    # Код возврата — наружу: установщик по нему сообщает о неудаче. Раньше
    # сценарий кончался нулём при любом исходе, и установка молча шла дальше
    # без моделей.
    if ($LASTEXITCODE -eq 0) { Write-Ok "Модель «$Model» загружена" }
    else { Write-Err "Модель «$Model» не загружена."; exit 1 }
}

'remove' {
    if (-not $Model) { Write-Err 'Укажите модель.'; exit 2 }
    Invoke-CatalogScript -Arguments @($Model, $modelsDir) -Code @'
import shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model
from asrhub import model_files
spec = get_model(sys.argv[1]); models_dir = Path(sys.argv[2])
if spec is None:
    print("Модель не найдена."); raise SystemExit(1)
candidates = []
found = model_files.find_local(models_dir, spec.source, spec.revision or "")
if found is not None:
    candidates.append(found)
# Прямые ссылки распаковываются в несколько каталогов сразу — берём их все.
if spec.source.startswith("http") and models_dir.exists():
    name = spec.source.rsplit("/", 1)[-1].replace(".zip", "")
    candidates += [p for p in models_dir.rglob(f"*{name}*") if p not in candidates]
removed = 0
for path in candidates:
    if path.exists():
        size = model_files.directory_size(path)
        # Веса GigaAM — один файл: rmtree на файле не работает.
        if path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)
        print(f"Удалено: {path} ({size/1024/1024:.0f} МБ)")
        removed += 1
if removed == 0:
    print("Веса модели на диске не найдены.")
'@
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

'verify' {
    if (-not $Model) { Write-Err 'Укажите модель.'; exit 2 }
    Invoke-CatalogScript -Arguments @($Model, $modelsDir) -Code @'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model
from asrhub.engines import ENGINE_CLASSES
from asrhub import model_files
spec = get_model(sys.argv[1]); models_dir = Path(sys.argv[2])
if spec is None:
    print("Модель не найдена."); raise SystemExit(1)
path = model_files.find_local(models_dir, spec.source, spec.revision or "")
print(f"Веса на диске:   {'да, ' + str(path) if path else 'нет'}")
if path:
    files = [path] if path.is_file() else [f for f in path.rglob("*") if f.is_file()]
    size = model_files.directory_size(path)
    print(f"Файлов:          {len(files)}")
    print(f"Размер:          {size/1024/1024:.0f} МБ (ожидалось {spec.disk_mb or '?'} МБ)")
    if spec.disk_mb and size/1024/1024 < spec.disk_mb * 0.5:
        print("ВНИМАНИЕ: размер заметно меньше ожидаемого — загрузка могла оборваться.")
cls = ENGINE_CLASSES.get(spec.engine)
if cls:
    available, reason = cls.check_available()
    print(f"Движок {spec.engine}: {'установлен' if available else 'НЕ установлен'}")
    if not available:
        print(f"  {reason}")
'@
    if ($LASTEXITCODE -ne 0) { exit 1 }
}

'engines' {
    Invoke-CatalogScript -Code @'
import sys
sys.path.insert(0, ".")
from asrhub.engines import engine_status
print("\nДвижки распознавания\n")
for item in engine_status():
    mark = "[+]" if item["available"] else "[ ]"
    print(f"  {mark} {item['id']:<18} {item['name']:<34} {item['license']}")
    if not item["available"]:
        print(f"      {item['reason']}")
print()
'@
}

'install-engine' {
    if (-not $Engine) { Write-Err 'Укажите движок: -Engine <имя>'; exit 2 }
    $req = Join-Path $Prefix ("requirements\engines\" + ($Engine -replace '_', '-') + '.txt')
    if (-not (Test-Path $req)) {
        Write-Err "Нет файла зависимостей для «$Engine»."
        $available = Get-ChildItem (Join-Path $Prefix 'requirements\engines') -Filter '*.txt' |
            ForEach-Object { $_.BaseName }
        Write-Hint "Доступные: $($available -join ', ')"
        exit 2
    }
    $venvPip = Join-Path $Prefix 'venv\Scripts\pip.exe'
    if (-not (Test-Path $venvPip)) { Write-Err "Не найдено окружение: $venvPip"; exit 1 }

    $hw = Get-HardwareInfo
    if ($Engine -eq 'faster_whisper' -and $hw.Accelerator -eq 'cuda') {
        $pin = Get-CTranslate2Pin -Accelerator $hw.Accelerator -CudaVersion $hw.CudaVersion
        Write-Info "Совместимая версия CTranslate2: $pin"
        Invoke-Checked -Command $venvPip -Arguments @('install', '--disable-pip-version-check', $pin) -IgnoreExitCode | Out-Null
    }
    if ($Engine -eq 'tone') {
        Write-Warn 'T-one требует KenLM, который не собирается нативно под Windows.'
        Write-Hint 'Используйте WSL или запуск через Docker.'
        if (-not (Confirm-Action 'Всё равно попробовать?' 'n')) { exit 0 }
    }
    if ($Engine -eq 'whisperx') {
        Write-Warn 'WhisperX жёстко фиксирует версию torch и может сломать другие движки.'
        if (-not (Confirm-Action 'Установить в общее окружение?' 'n')) { exit 0 }
    }

    Write-Info "Установка движка «$Engine»…"
    Invoke-WithRetry -Attempts 2 -Description $Engine -Action {
        # Та же функция, что у установщика: голый «pip install -r» не знал
        # про спутники no-deps и optional, и «models.ps1 install-engine
        # gigaam» печатал «установлен», не поставив сам gigaam — его пакет
        # только в no-deps\gigaam.txt.
        Install-EngineRequirements -Pip $venvPip -Requirements $req `
            -PipFlags @('--disable-pip-version-check')
    } | Out-Null
    # Движок мог утащить общий пакет под свой пин — возвращаем заданное нами.
    $requirementsDir = Join-Path $Prefix 'requirements'
    Install-Overrides -Pip $venvPip -RequirementsDir $requirementsDir
    Remove-RetiredPackages -Pip $venvPip -RequirementsDir $requirementsDir
    Write-Ok "Движок «$Engine» установлен"
}

'remove-engine' {
    if (-not $Engine) { Write-Err 'Укажите движок.'; exit 2 }
    $req = Join-Path $Prefix ("requirements\engines\" + ($Engine -replace '_', '-') + '.txt')
    if (-not (Test-Path $req)) { Write-Err "Нет файла зависимостей для «$Engine»."; exit 2 }
    $venvPip = Join-Path $Prefix 'venv\Scripts\pip.exe'
    if (-not (Test-Path $venvPip)) { Write-Err "Не найдено окружение: $venvPip"; exit 1 }
    # Вместе со спутниками no-deps и optional (Get-EnginePackages): движок
    # снимается тем же набором, каким ставился. Но только то, что больше
    # никому не нужно, — раньше снималось всё подряд, вместе с numpy из
    # base.txt и onnxruntime, общим с другими движками.
    $plan = Get-EngineRemovalPlan -Pip $venvPip -Requirements $req
    if ($plan.Keep.Count -gt 0) {
        Write-Info "Остаются — нужны серверу или другим пакетам: $($plan.Keep -join ', ')"
    }
    if ($plan.Remove.Count -eq 0) {
        Write-Ok "Своих пакетов у движка «$Engine» нет — удалять нечего."
        exit 0
    }
    Write-Info "Будут удалены пакеты: $($plan.Remove -join ', ')"
    if (-not (Confirm-Action 'Продолжить?' 'n')) { exit 0 }
    try {
        Invoke-Checked -Command $venvPip -Arguments (@('uninstall', '-y') + $plan.Remove) | Out-Null
        Write-Ok "Движок «$Engine» удалён"
    } catch { Write-Warn 'Часть пакетов удалить не удалось.' }
    Remove-RetiredPackages -Pip $venvPip -RequirementsDir (Join-Path $Prefix 'requirements')
}

'disk' {
    Write-Heading 'Занятое место'
    if (Test-Path $modelsDir) {
        # Таблицу рисуем вручную: Format-Table при выводе в конвейер печатает пусто.
        Get-ChildItem $modelsDir -Directory | ForEach-Object {
            $size = (Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue |
                     Measure-Object -Property Length -Sum).Sum
            [PSCustomObject]@{ Name = $_.Name; Size = $size }
        } | Sort-Object Size -Descending | Select-Object -First 25 | ForEach-Object {
            Write-Host ("  {0,-46} {1,12}" -f $_.Name, (Format-Size $_.Size))
        }
        # Веса GigaAM лежат файлами прямо в каталоге моделей.
        Get-ChildItem $modelsDir -File -Filter '*.ckpt' -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host ("  {0,-46} {1,12}" -f $_.Name, (Format-Size $_.Length))
        }
    }
    foreach ($sub in @('uploads', 'results', 'logs', 'tmp')) {
        $path = Join-Path $DataDir $sub
        if (Test-Path $path) {
            $size = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
                     Measure-Object -Property Length -Sum).Sum
            Write-Host ("  {0,-10} {1}" -f "${sub}:", (Format-Size $size))
        }
    }
    $drive = $null
    try { $drive = Get-PSDrive -Name (Split-Path -Qualifier $DataDir -ErrorAction Stop).TrimEnd(':') -ErrorAction SilentlyContinue }
    catch { $drive = $null }
    if ($drive) { Write-Host ("`n  Свободно на диске: {0}" -f (Format-Size $drive.Free)) }
    Write-Host ''
}

}

} finally {
    Set-AssumeYes $assumeYesBefore
    Set-DryRun $dryRunBefore
}
