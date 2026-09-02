"""Видеокарта: обнаружение по шине, выбор драйвера, настройка.

Проверять это на живом железе невозможно — в сборочной машине нет ни NVIDIA,
ни AMD, ни Intel Arc. Поэтому скриптам подсовывается поддельное дерево
/sys/bus/pci/devices, собранное здесь же: ровно те файлы, которые они читают.
Так проверяется главное — разбор идентификаторов и решение «дискретная или
встроенная», из которого следует всё остальное.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

GIB = 1024 ** 3


# ---------------------------------------------------------------------------
# Поддельная шина PCI
# ---------------------------------------------------------------------------

def _pci_device(root: Path, address: str, klass: str, vendor: str, device: str,
                bar_bytes: int) -> None:
    """Один видеоадаптер в дереве sysfs — те же файлы, что читает gpu.sh."""
    directory = root / address
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "class").write_text(f"{klass}\n")
    (directory / "vendor").write_text(f"{vendor}\n")
    (directory / "device").write_text(f"{device}\n")
    # Первая область — регистры, вторая — окно памяти карты, третья пустая.
    lines = ["0x00000000f6000000 0x00000000f6ffffff 0x0000000000040200"]
    if bar_bytes:
        start = 0xC0000000
        lines.append(f"0x{start:016x} 0x{start + bar_bytes - 1:016x} 0x000000000014220c")
    lines.append("0x0000000000000000 0x0000000000000000 0x0000000000000000")
    (directory / "resource").write_text("\n".join(lines) + "\n")


CARDS = {
    # имя: (адрес, класс, вендор, устройство, окно памяти)
    "rtx4090":   ("0000:01:00.0", "0x030000", "0x10de", "0x2684", 32 * GIB),
    "a100":      ("0000:06:00.0", "0x030200", "0x10de", "0x20b2", 64 * GIB),
    "rx7900":    ("0000:03:00.0", "0x030000", "0x1002", "0x744c", 16 * GIB),
    "ryzen_igpu": ("0000:07:00.0", "0x030000", "0x1002", "0x1638", 256 * 1024 * 1024),
    "arc_a770":  ("0000:03:00.0", "0x030000", "0x8086", "0x56a0", 16 * GIB),
    "intel_igpu": ("0000:00:02.0", "0x030000", "0x8086", "0x4680", 256 * 1024 * 1024),
    "rtx4060m":  ("0000:01:00.0", "0x030200", "0x10de", "0x28e0", 8 * GIB),
    "intel_igpu2": ("0000:00:02.0", "0x030000", "0x8086", "0x9a49", 256 * 1024 * 1024),
}


@pytest.fixture()
def pci_tree(tmp_path: Path):
    """Собирает дерево из перечисленных карт и отдаёт путь к нему."""
    def build(*names: str) -> Path:
        root = tmp_path / ("pci-" + "-".join(names or ("empty",)))
        root.mkdir(parents=True, exist_ok=True)
        for name in names:
            _pci_device(root, *CARDS[name])
        return root
    return build


def _run_gpu(root: Path, snippet: str, repo_root: Path) -> str:
    """Выполняет кусок кода при подключённой gpu.sh и подменённой шине."""
    script = textwrap.dedent(f"""
        set -o errexit -o nounset -o pipefail
        source "{repo_root}/scripts/lib/common.sh"
        source "{repo_root}/scripts/lib/detect.sh"
        source "{repo_root}/scripts/lib/gpu.sh"
        {snippet}
    """)
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "ASRHUB_PCI_ROOT": str(root), "ASRHUB_QUIET": "1"},
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _primary(root: Path, repo_root: Path) -> dict[str, str]:
    line = _run_gpu(root, "gpu_primary", repo_root)
    if not line:
        return {}
    address, vendor, device, discrete, bar = line.split("|")
    return {"address": address, "vendor": vendor, "device": device,
            "discrete": discrete, "bar": bar}


# ---------------------------------------------------------------------------
# Обнаружение
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cards,vendor,discrete", [
    (["rtx4090"], "0x10de", "1"),
    (["a100"], "0x10de", "1"),                      # 3D-контроллер без видеовыхода
    (["rx7900"], "0x1002", "1"),
    (["ryzen_igpu"], "0x1002", "0"),                # встроенная в процессор
    (["arc_a770"], "0x8086", "1"),
    (["intel_igpu"], "0x8086", "0"),
])
def test_single_card(pci_tree, repo_root: Path, cards, vendor, discrete):
    found = _primary(pci_tree(*cards), repo_root)
    assert found, f"карта {cards} не найдена"
    assert found["vendor"] == vendor
    assert found["discrete"] == discrete, "неверно определено, дискретная карта или нет"


def test_laptop_prefers_discrete(pci_tree, repo_root: Path):
    """В ноутбуке две карты: встроенная и дискретная. Считать — на дискретной."""
    found = _primary(pci_tree("intel_igpu2", "rtx4060m"), repo_root)
    assert found["vendor"] == "0x10de"
    assert found["discrete"] == "1"


def test_igpu_plus_arc_prefers_arc(pci_tree, repo_root: Path):
    found = _primary(pci_tree("intel_igpu", "arc_a770"), repo_root)
    assert found["device"] == "0x56a0"


def test_no_cards(pci_tree, repo_root: Path):
    assert _primary(pci_tree(), repo_root) == {}


def test_non_display_devices_ignored(tmp_path: Path, repo_root: Path):
    """Сетевая карта того же производителя видеокартой считаться не должна."""
    root = tmp_path / "pci"
    _pci_device(root, "0000:00:01.0", "0x020000", "0x8086", "0x1533", 0)   # сеть
    _pci_device(root, "0000:00:1f.3", "0x040300", "0x8086", "0xa348", 0)   # звук
    assert _primary(root, repo_root) == {}


def test_bar_size_read_without_gawk(pci_tree, repo_root: Path):
    """Размер окна памяти считается средствами оболочки.

    Раньше здесь был awk со strtonum — расширением gawk. В Ubuntu по
    умолчанию стоит mawk, где эта функция молча возвращает ноль, и любая
    карта оказалась бы «встроенной».
    """
    found = _primary(pci_tree("rtx4090"), repo_root)
    assert int(found["bar"]) == 32 * GIB


# ---------------------------------------------------------------------------
# Согласованность со списком устройств сервера
# ---------------------------------------------------------------------------

def test_config_device_names_are_known_to_server(repo_root: Path):
    """Скрипт не должен писать в конфигурацию устройство, которого нет.

    Установщик подставляет device прямо в config.yaml, а сервер сверяет
    значение со списком в каталоге. Расхождение проявилось бы только при
    первом запуске сервера — сообщением об ошибке в конфигурации.
    """
    import sys
    sys.path.insert(0, str(repo_root / "server"))
    from asrhub import catalog

    spec = next(p for p in catalog.PARAMS if p.key == "device")
    allowed = {c["value"] for c in spec.to_dict()["options"]}

    text = (repo_root / "scripts" / "lib" / "gpu.sh").read_text(encoding="utf-8")
    written = set()
    for line in text.splitlines():
        if "printf 'device: " in line:
            written.add(line.split("printf 'device: ")[1].split("\\n")[0])
    assert written, "в gpu.sh не нашлось ни одной строки device:"
    assert written <= allowed, f"сервер не знает устройств: {written - allowed}"


def test_config_compute_types_are_known_to_server(repo_root: Path):
    import sys
    sys.path.insert(0, str(repo_root / "server"))
    from asrhub import catalog

    spec = next(p for p in catalog.PARAMS if p.key == "compute_type")
    allowed = {c["value"] for c in spec.to_dict()["options"]}

    text = (repo_root / "scripts" / "lib" / "gpu.sh").read_text(encoding="utf-8")
    written = {line.split("compute_type: ")[1].split("\\n")[0]
               for line in text.splitlines() if "compute_type: " in line and "printf" in line}
    assert written <= allowed, f"сервер не знает точности: {written - allowed}"


# ---------------------------------------------------------------------------
# Установщик целиком
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cards,expect", [
    (["rtx4090"], "драйвер nvidia"),
    (["rx7900"], "драйвер amd"),
    (["ryzen_igpu"], "встроенная графика"),
    (["arc_a770"], "движки распознавания её пока не используют"),
    ([], "Видеокарт на шине PCI не найдено"),
])
def test_installer_dry_run(pci_tree, repo_root: Path, tmp_path: Path, cards, expect):
    """Пробный запуск проходит целиком и говорит про карту то, что нужно."""
    root = pci_tree(*cards)
    result = subprocess.run(
        ["bash", str(repo_root / "scripts" / "install.sh"), "--dry-run",
         "--no-interactive", "--yes", "--profile", "light", "--skip-models",
         "--no-service", "--prefix", str(tmp_path / "app"),
         "--data", str(tmp_path / "data")],
        env={**os.environ, "ASRHUB_PCI_ROOT": str(root)},
        capture_output=True, text=True, timeout=300, cwd=repo_root)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-2000:]
    assert "Пробный запуск завершён" in result.stdout
    assert expect in result.stdout, result.stdout[-2000:]


def test_installer_picks_gpu_wheels_before_reboot(pci_tree, repo_root: Path,
                                                  tmp_path: Path):
    """Колёса PyTorch берутся под найденную карту, а не под текущее состояние.

    Драйвер поставлен, но карта заработает только после перезагрузки. Если
    в этот момент выбрать колёса обычной проверкой, установится процессорный
    torch — и после перезагрузки сервер всё равно считал бы процессором.
    """
    result = subprocess.run(
        ["bash", str(repo_root / "scripts" / "install.sh"), "--dry-run",
         "--no-interactive", "--yes", "--profile", "standard", "--skip-models",
         "--no-service", "--prefix", str(tmp_path / "app"),
         "--data", str(tmp_path / "data")],
        env={**os.environ, "ASRHUB_PCI_ROOT": str(pci_tree("rtx4090"))},
        capture_output=True, text=True, timeout=300, cwd=repo_root)
    assert result.returncode == 0, result.stdout[-2000:]
    assert "PyTorch для ускорителя «cuda»" in result.stdout
    assert "download.pytorch.org/whl/cu" in result.stdout


@pytest.mark.parametrize("flag,expect_install,expect_text", [
    (["--no-gpu-driver"], False, "Видеокарта       NVIDIA"),
    (["--gpu-driver", "none"], False, "Видеокарта       NVIDIA"),
    (["--gpu-driver", "amd"], False, "не совпал с найденной картой"),
    (["--gpu-driver", "auto"], True, "Здесь был бы установлен драйвер nvidia"),
    ([], True, "Здесь был бы установлен драйвер nvidia"),
])
def test_installer_gpu_flags(pci_tree, repo_root: Path, tmp_path: Path,
                             flag, expect_install, expect_text):
    """Ключи управляют установкой драйвера, но карту показывают всегда."""
    result = subprocess.run(
        ["bash", str(repo_root / "scripts" / "install.sh"), "--dry-run",
         "--no-interactive", "--yes", "--profile", "light", "--skip-models",
         "--no-service", "--prefix", str(tmp_path / "app"),
         "--data", str(tmp_path / "data"), *flag],
        env={**os.environ, "ASRHUB_PCI_ROOT": str(pci_tree("rtx4090"))},
        capture_output=True, text=True, timeout=300, cwd=repo_root)
    assert result.returncode == 0, result.stdout[-2000:]
    assert expect_text in result.stdout, result.stdout[-1500:]
    installed = "Здесь был бы установлен драйвер" in result.stdout
    assert installed is expect_install


def test_dry_run_installs_nothing_as_root(repo_root: Path):
    """`as_root` обязан уважать пробный запуск.

    Ветка для root вызывала команду напрямую, минуя обёртку, и пробный
    запуск от имени root по-настоящему ставил системные пакеты — а в
    контейнерах и в Ansible он запускается именно так.
    """
    text = (repo_root / "scripts" / "lib" / "common.sh").read_text(encoding="utf-8")
    body = text.split("as_root() {")[1].split("\n}")[0]
    assert 'if is_root; then run "$@"' in body, "root-ветка as_root снова минует run"


# ---------------------------------------------------------------------------
# Разбор команд установки драйвера
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("manager,distro,version,ubuntu_drivers,expected", [
    ("apt-get", "ubuntu", "24.04", "1", "ubuntu-drivers install"),
    ("apt-get", "debian", "12", "0", "repos/debian12/x86_64/cuda-keyring"),
    ("dnf", "fedora", "41", "0", "akmod-nvidia"),
    ("dnf", "rocky", "9.4", "0", "cuda-rhel9.repo"),
    ("pacman", "arch", "", "0", "nvidia-dkms"),
    ("zypper", "opensuse-leap", "15.6", "0", "nvidia-video-G06"),
])
def test_nvidia_commands_per_distro(repo_root: Path, manager, distro, version,
                                    ubuntu_drivers, expected):
    """Под каждый дистрибутив выбирается свой способ установки."""
    snippet = textwrap.dedent(f"""
        detect_package_manager() {{ printf '{manager}'; }}
        detect_distro()         {{ printf '{distro}'; }}
        detect_distro_version() {{ printf '{version}'; }}
        have() {{ case "$1" in ubuntu-drivers) [[ "{ubuntu_drivers}" == 1 ]] ;; *) return 0 ;; esac; }}
        download() {{ printf '%s\\n' "$1"; return 0; }}
        is_root() {{ return 0; }}
        rpm() {{ printf '41'; }}
        ASRHUB_DRY_RUN=1
        ASRHUB_QUIET=0
        gpu_install_nvidia
    """)
    out = _run_gpu(Path("/nonexistent"), snippet, repo_root)
    assert expected in out, out


def test_unsupported_package_manager_is_reported(repo_root: Path):
    snippet = textwrap.dedent("""
        detect_package_manager() { printf 'apk'; }
        ASRHUB_DRY_RUN=1
        gpu_install_nvidia || echo "ОТКАЗ"
    """)
    assert "ОТКАЗ" in _run_gpu(Path("/nonexistent"), snippet, repo_root)


def test_amd_uses_one_rocm_version_everywhere(repo_root: Path):
    """Номер версии ROCm задан в одном месте, а не рассыпан по ссылкам."""
    import re

    text = (repo_root / "scripts" / "lib" / "gpu.sh").read_text(encoding="utf-8")
    hardcoded = [
        line.strip() for line in text.splitlines()
        # интересуют строки, где ссылка собирается с номером версии,
        # а не упоминания домена в комментарии и подсказке
        if "repo.radeon.com/amdgpu-install/" in line
        and re.search(r"amdgpu-install/\d", line)
        and "ASRHUB_ROCM_VERSION" not in line
    ]
    assert not hardcoded, f"версия ROCm вписана прямо в ссылку: {hardcoded}"
    assert "ASRHUB_ROCM_VERSION=" in text and "ASRHUB_ROCM_PKG=" in text


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

pwsh = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(pwsh is None, reason="PowerShell недоступен")
@pytest.mark.parametrize("cards,vendor,discrete", [
    ([("NVIDIA GeForce RTX 4090", "PCI\\VEN_10DE&DEV_2684")], "nvidia", True),
    ([("AMD Radeon RX 7900 XTX", "PCI\\VEN_1002&DEV_744C")], "amd", True),
    ([("AMD Radeon(TM) Graphics", "PCI\\VEN_1002&DEV_1638")], "amd", False),
    ([("Intel(R) UHD Graphics", "PCI\\VEN_8086&DEV_9A49")], "intel", False),
    ([("Intel(R) Arc(TM) A770 Graphics", "PCI\\VEN_8086&DEV_56A0")], "intel", True),
    ([("Intel(R) UHD Graphics", "PCI\\VEN_8086&DEV_9A49"),
      ("NVIDIA GeForce RTX 4060 Laptop GPU", "PCI\\VEN_10DE&DEV_28E0")], "nvidia", True),
    ([("VMware SVGA 3D", "PCI\\VEN_15AD&DEV_0405")], "", False),
])
def test_windows_card_selection(repo_root: Path, cards, vendor, discrete):
    """Тот же выбор, что и в Linux, но по данным Windows."""
    items = ", ".join(
        f'[pscustomobject]@{{Name="{name}";PNPDeviceID="{pnp}";DriverVersion="31.0.0.1"}}'
        for name, pnp in cards)
    script = (
        f'Import-Module "{repo_root}/scripts/lib/Common.psm1" -Force; '
        f'$r = Resolve-GpuFromControllers @({items}); '
        '$r | ConvertTo-Json -Compress')
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["Vendor"] == vendor
    assert bool(data["Discrete"]) == discrete


@pytest.mark.skipif(pwsh is None, reason="PowerShell недоступен")
def test_windows_generic_driver_detected(repo_root: Path):
    """Стандартный адаптер Microsoft — это и есть «драйвера нет»."""
    script = (
        f'Import-Module "{repo_root}/scripts/lib/Common.psm1" -Force; '
        '$r = Resolve-GpuFromControllers @([pscustomobject]@{'
        'Name="Microsoft Basic Display Adapter";'
        'PNPDeviceID="PCI\\VEN_10DE&DEV_2684";DriverVersion="10.0.0.1"}); '
        '$r | ConvertTo-Json -Compress')
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["Vendor"] == "nvidia"
    assert data["DriverIsGeneric"] is True


@pytest.mark.skipif(pwsh is None, reason="PowerShell недоступен")
def test_windows_driver_packages_have_fallback(repo_root: Path):
    """У каждого производителя есть и кандидаты winget, и прямая ссылка.

    Каталог winget меняется: пакеты переименовываются и исчезают, а у AMD
    своего пакета с драйвером может не быть вовсе. Поэтому кандидатов
    несколько, и обязателен запасной путь — ссылка на сайт производителя.
    """
    script = (
        f'Import-Module "{repo_root}/scripts/lib/Common.psm1" -Force; '
        '@("nvidia","amd","intel") | ForEach-Object { Get-GpuDriverPackage -Vendor $_ } '
        '| ConvertTo-Json -Compress -Depth 4')
    result = subprocess.run([pwsh, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    for entry in json.loads(result.stdout):
        assert entry["WingetIds"], "нет ни одного кандидата winget"
        assert entry["Fallback"].startswith("https://"), "нет запасной ссылки"
        assert entry["Label"]


# ---------------------------------------------------------------------------
# Документация
# ---------------------------------------------------------------------------

def test_documented_flags_exist(repo_root: Path):
    """Ключи, названные в документации, должны существовать в скриптах.

    Читатель копирует команду из главы про установку. Опечатка или
    переименованный ключ дают «Неизвестный параметр» — и виноватой
    выглядит инструкция.
    """
    install = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    docs = (repo_root / "docs" / "02-installation.md").read_text(encoding="utf-8")

    import re
    mentioned = set(re.findall(r"install\.sh[^\n`]*?(--gpu-driver|--no-gpu-driver|"
                               r"--force-gpu-driver)", docs))
    assert mentioned, "в документации не упомянут ни один ключ видеокарты"
    for flag in mentioned:
        assert f"{flag})" in install, f"ключа {flag} нет в install.sh"


def test_windows_flag_documented_and_exists(repo_root: Path):
    ps1 = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    docs = (repo_root / "docs" / "02-installation.md").read_text(encoding="utf-8")
    assert "[switch]$NoGpuDriver" in ps1
    assert "-NoGpuDriver" in docs


def test_troubleshooting_covers_driver_states(repo_root: Path):
    """У каждого состояния драйвера есть разбор в главе про неполадки."""
    text = (repo_root / "docs" / "11-troubleshooting.md").read_text(encoding="utf-8")
    for phrase in ("Secure Boot", "dkms autoinstall", "HSA_OVERRIDE_GFX_VERSION",
                   "render,video"):
        assert phrase in text, f"в главе про неполадки нет разбора: {phrase}"
