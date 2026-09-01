<#
.SYNOPSIS
    Менеджер моделей и движков ASR Hub для Windows.
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
    [switch]$Yes
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lib\Common.psm1') -Force
Set-AssumeYes $Yes.IsPresent

if (-not $Prefix) {
    foreach ($c in @('C:\Program Files\ASRHub', (Join-Path $env:LOCALAPPDATA 'ASRHub'),
                     (Split-Path -Parent $PSScriptRoot))) {
        if (Test-Path (Join-Path $c 'server')) { $Prefix = $c; break }
    }
}
if (-not $DataDir) {
    foreach ($c in @((Join-Path $env:ProgramData 'ASRHub'), (Join-Path $env:LOCALAPPDATA 'ASRHub\data'))) {
        if (Test-Path $c) { $DataDir = $c; break }
    }
    if (-not $DataDir) { $DataDir = Join-Path $env:LOCALAPPDATA 'ASRHub\data' }
}

$modelsDir = Join-Path $DataDir 'models'
$venvPython = Join-Path $Prefix 'venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { Find-Python }
if (-not $python) { Write-Err 'Не найден Python.'; exit 1 }
$serverDir = Join-Path $Prefix 'server'

function Invoke-CatalogScript {
    param([string]$Code, [string[]]$Arguments = @())
    $temp = Join-Path $env:TEMP "asrhub-$([guid]::NewGuid().ToString('N')).py"
    Set-Content -Path $temp -Value $Code -Encoding UTF8
    try {
        Push-Location $serverDir
        $env:PYTHONIOENCODING = 'utf-8'
        & $python $temp @Arguments
    } finally { Pop-Location; Remove-Item $temp -Force -ErrorAction SilentlyContinue }
}

switch ($Action) {

'list' {
    Invoke-CatalogScript -Arguments @($(if ($Installed) { '1' } else { '0' }), $Language, $modelsDir) -Code @'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import MODELS, mean_ru_wer

only_installed = sys.argv[1] == "1"
language = sys.argv[2]
models_dir = Path(sys.argv[3])

def local_path(spec):
    if not models_dir.exists():
        return None
    if spec.source.startswith("http"):
        name = spec.source.rsplit("/", 1)[-1].replace(".zip", "")
        for p in models_dir.rglob(f"*{name}*"):
            if p.is_dir():
                return p
        return None
    slug = "models--" + spec.source.replace("/", "--")
    for base in (models_dir, models_dir / "hub"):
        if (base / slug).exists():
            return base / slug
    return None

quality = {"excellent": "отличное", "good": "хорошее", "fair": "среднее",
           "poor": "слабое", "none": "—"}
families, installed, total_mb = {}, 0, 0.0
for spec in MODELS:
    if language and language not in spec.languages and not any(
            x.startswith("multi") for x in spec.languages):
        continue
    path = local_path(spec)
    if only_installed and path is None:
        continue
    families.setdefault(spec.family, []).append((spec, path))

for family in sorted(families):
    print(f"\n{family}")
    for spec, path in families[family]:
        mark = "[+]" if path else "[ ]"
        if path:
            installed += 1
            total_mb += sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1024 / 1024
        wer = mean_ru_wer(spec)
        wer_text = f"{wer:5.1f} %" if wer is not None else "     -"
        size = f"{spec.disk_mb:6d} МБ" if spec.disk_mb else "        -"
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

if spec.engine == "demo":
    print("Демонстрационный движок не требует загрузки весов."); raise SystemExit(0)

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

print(f"Скачивание {spec.source}" + (f" (ветка {spec.revision})" if spec.revision else ""))
print(f"Ожидаемый размер: примерно {spec.disk_mb or '?'} МБ")
try:
    path = snapshot_download(repo_id=spec.source, revision=spec.revision or None,
                             cache_dir=str(models_dir), force_download=force,
                             token=os.environ.get("HF_TOKEN"))
except Exception as exc:
    text = str(exc).lower()
    print(f"\nОшибка загрузки: {exc}")
    if "401" in text or "gated" in text:
        print(f"\nМодель закрыта лицензией:")
        print(f"  1. Откройте https://huggingface.co/{spec.source} и примите условия")
        print("  2. Создайте токен: https://huggingface.co/settings/tokens")
        print("  3. $env:HF_TOKEN='hf_xxxxx' и повторите")
    elif "connection" in text or "timeout" in text:
        print("\nПроблема с сетью. Проверьте доступ к huggingface.co и настройки прокси.")
    raise SystemExit(1)
print(f"Готово: {path}")
'@
    if ($LASTEXITCODE -eq 0) { Write-Ok "Модель «$Model» загружена" }
}

'remove' {
    if (-not $Model) { Write-Err 'Укажите модель.'; exit 2 }
    Invoke-CatalogScript -Arguments @($Model, $modelsDir) -Code @'
import shutil, sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model
spec = get_model(sys.argv[1]); models_dir = Path(sys.argv[2])
if spec is None:
    print("Модель не найдена."); raise SystemExit(1)
slug = "models--" + spec.source.replace("/", "--")
removed = 0
for path in (models_dir / slug, models_dir / "hub" / slug):
    if path.exists():
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        shutil.rmtree(path, ignore_errors=True)
        print(f"Удалено: {path} ({size/1024/1024:.0f} МБ)")
        removed += 1
if removed == 0:
    print("Веса модели на диске не найдены.")
'@
}

'verify' {
    if (-not $Model) { Write-Err 'Укажите модель.'; exit 2 }
    Invoke-CatalogScript -Arguments @($Model, $modelsDir) -Code @'
import sys
from pathlib import Path
sys.path.insert(0, ".")
from asrhub.catalog import get_model
from asrhub.engines import ENGINE_CLASSES
spec = get_model(sys.argv[1]); models_dir = Path(sys.argv[2])
if spec is None:
    print("Модель не найдена."); raise SystemExit(1)
slug = "models--" + spec.source.replace("/", "--")
path = next((p for p in (models_dir / slug, models_dir / "hub" / slug) if p.exists()), None)
print(f"Веса на диске:   {'да, ' + str(path) if path else 'нет'}")
if path:
    files = [f for f in path.rglob("*") if f.is_file()]
    size = sum(f.stat().st_size for f in files)
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
        & $venvPip install --disable-pip-version-check $pin
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
        Invoke-Checked -Command $venvPip -Arguments @('install', '--disable-pip-version-check', '-r', $req)
    } | Out-Null
    Write-Ok "Движок «$Engine» установлен"
}

'remove-engine' {
    if (-not $Engine) { Write-Err 'Укажите движок.'; exit 2 }
    $req = Join-Path $Prefix ("requirements\engines\" + ($Engine -replace '_', '-') + '.txt')
    if (-not (Test-Path $req)) { Write-Err "Нет файла зависимостей."; exit 2 }
    $packages = Get-Content $req | Where-Object { $_ -and $_ -notmatch '^\s*(#|--)' } |
        ForEach-Object { ($_ -split '[<>=!@]')[0].Trim() } | Where-Object { $_ }
    Write-Info "Будут удалены пакеты: $($packages -join ', ')"
    Write-Warn 'Некоторые пакеты могут использоваться другими движками.'
    if (-not (Confirm-Action 'Продолжить?' 'n')) { exit 0 }
    & (Join-Path $Prefix 'venv\Scripts\pip.exe') uninstall -y @packages
    Write-Ok "Движок «$Engine» удалён"
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
    }
    foreach ($sub in @('uploads', 'results', 'logs', 'tmp')) {
        $path = Join-Path $DataDir $sub
        if (Test-Path $path) {
            $size = (Get-ChildItem $path -Recurse -File -ErrorAction SilentlyContinue |
                     Measure-Object -Property Length -Sum).Sum
            Write-Host ("  {0,-10} {1}" -f "${sub}:", (Format-Size $size))
        }
    }
    $drive = Get-PSDrive -Name (Split-Path -Qualifier $DataDir).TrimEnd(':') -ErrorAction SilentlyContinue
    if ($drive) { Write-Host ("`n  Свободно на диске: {0}" -f (Format-Size $drive.Free)) }
    Write-Host ''
}

}
