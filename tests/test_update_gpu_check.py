"""Проверка видеокарты перед обновлением.

Обновление, поставленное на сервер с недоступной картой, выглядит успешным
до первого задания: служба поднимается, /api/health отвечает двумястами, а
падает каждое задание по отдельности — и падает текстом про загрузку модели,
из которого причина не следует.

Поэтому карта спрашивается до снимка и до первого изменённого файла, и
спрашивается у того самого питона, который будет распознавать, — а не у
nvidia-smi: эти двое расходятся, и заданию важен первый.

Всё гоняется настоящим bash на макетах в tmp_path: подложные nvidia-smi и
modinfo, подложный /proc, подложный torch. Иначе проверить разбор можно было
бы только на машине, где драйвер сломан именно нужным образом.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="нужен bash")

# Текст, с которым это пришло с боевого сервера.
БОЕВОЙ = ("Unexpected error from cudaGetDeviceCount(). Did you run some cuda "
          "functions before calling NumCudaDevices()? Error 101: invalid device ordinal")


# ---------------------------------------------------------------------------
# Макеты
# ---------------------------------------------------------------------------


def подложка(каталог: Path, имя: str, тело: str) -> Path:
    """Исполняемый файл-обманка в каталоге, который подставляется в PATH."""
    каталог.mkdir(parents=True, exist_ok=True)
    путь = каталог / имя
    путь.write_text("#!/usr/bin/env bash\n" + тело, encoding="utf-8")
    путь.chmod(0o755)
    return путь


def драйвер(tmp_path: Path, *, загружен: str = "595.91.07",
            на_диске: str = "595.91.07", библиотеки: str = "595.91.07",
            smi_работает: bool = True, modinfo_есть: bool = True,
            ядра: dict[str, str] | None = None,
            ядро_имя: str = "") -> dict[str, str]:
    """Машина с заданным состоянием драйвера NVIDIA.

    `ядра` — модули, лежащие на диске под другими ядрами: «имя ядра: версия».
    Это отдельный случай, а не мелочь: модуль мог собраться не под то ядро,
    что работает сейчас, и тогда «на диске нет» и «чинить нечем» — разные
    вещи с разной ценой ошибки.
    """
    bin_dir = tmp_path / "bin"
    proc = tmp_path / "proc-nvidia"
    proc.write_text(
        f"NVRM version: NVIDIA UNIX x86_64 Kernel Module  {загружен}  "
        "Tue Sep  9 10:00:00 UTC 2026\nGCC version:  gcc version 13.2.0\n",
        encoding="utf-8")
    if smi_работает:
        подложка(bin_dir, "nvidia-smi", f'''
case "$*" in
  *driver_version*) echo "{библиотеки}" ;;
  *memory.total*) echo "32768" ;;
  *memory.free*) echo "32000" ;;
  *--query-gpu=name*) echo "NVIDIA GeForce RTX 5090" ;;
  -L) echo "GPU 0: NVIDIA GeForce RTX 5090 (UUID: GPU-x)" ;;
  *) echo "NVIDIA-SMI {библиотеки}  Driver Version: {библиотеки}  CUDA Version: 13.0" ;;
esac
''')
    else:
        подложка(bin_dir, "nvidia-smi", f'''
echo "Failed to initialize NVML: Driver/library version mismatch"
echo "NVML library version: {библиотеки}"
exit 1
''')
    if ядро_имя:
        # `uname -r` подменяется целиком: проверить разбор «модуль собран
        # под другое ядро» иначе можно было бы только перезагрузившись.
        подложка(bin_dir, "uname",
                 f'[[ "$1" == "-r" ]] && {{ echo "{ядро_имя}"; exit 0; }}\n'
                 'exec /usr/bin/uname "$@"\n')
    mods = tmp_path / "modules"
    ветки = ""
    for ядро, версия in (ядра or {}).items():
        каталог = mods / ядро / "updates" / "dkms"
        каталог.mkdir(parents=True, exist_ok=True)
        (каталог / "nvidia.ko").touch()
        ветки += f'    */{ядро}/*) echo "{версия}"; exit 0 ;;\n'
    mods.mkdir(parents=True, exist_ok=True)
    # Пустая «версия на диске» означает, что modinfo не находит модуль по
    # имени: так бывает, когда он лежит не в updates/dkms.
    по_имени = f'echo "{на_диске}"; exit 0' if на_диске else "exit 1"
    if modinfo_есть:
        подложка(bin_dir, "modinfo", f'''
if [[ "$1" == "-F" && "$2" == "version" ]]; then
  case "$3" in
{ветки}    nvidia) {по_имени} ;;
  esac
fi
exit 1
''')
    else:
        # PATH без modinfo вовсе: пакет kmod бывает не установлен.
        подложка(bin_dir, "modinfo", 'exit 127\n')
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    (dev / "nvidia0").touch()
    (dev / "nvidiactl").touch()
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ASRHUB_NVIDIA_PROC": str(proc),
        "ASRHUB_NVIDIA_DEV": str(dev),
        "ASRHUB_MODULES_ROOT": str(mods),
    }
    if not modinfo_есть:
        # `have modinfo` смотрит в PATH, поэтому файла быть не должно вовсе.
        (bin_dir / "modinfo").unlink()
    return env


ТОРЧ_СЛОМАН = f'''
class _Cuda:
    @staticmethod
    def device_count():
        raise RuntimeError({БОЕВОЙ!r})
    @staticmethod
    def is_available(): return False
    @staticmethod
    def get_device_name(i=0): raise RuntimeError("нет карты")
cuda = _Cuda()
class backends:
    class mps:
        @staticmethod
        def is_available(): return False
__version__ = "2.14.0+cu130"
'''

ТОРЧ_ЖИВ = '''
class _Cuda:
    @staticmethod
    def device_count(): return 1
    @staticmethod
    def is_available(): return True
    @staticmethod
    def get_device_name(i=0):
        if i >= 1: raise RuntimeError("invalid device ordinal")
        return "NVIDIA GeForce RTX 5090"
cuda = _Cuda()
class backends:
    class mps:
        @staticmethod
        def is_available(): return False
__version__ = "2.14.0+cu130"
'''


def питон(tmp_path: Path, torch_код: str | None) -> Path:
    """Интерпретатор «из venv»: тот же python3, но со своим torch."""
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    if torch_код is not None:
        (stub / "torch").mkdir(exist_ok=True)
        (stub / "torch" / "__init__.py").write_text(torch_код, encoding="utf-8")
    py = tmp_path / "venv-python"
    # PYTHONPATH заменяется, а не дополняется: иначе настоящий torch с машины
    # проверяющего перебил бы подложный, и проверки «карта сломана» проходили
    # бы по случайной причине.
    py.write_text(f'#!/usr/bin/env bash\nexec env PYTHONPATH="{stub}" python3 "$@"\n',
                  encoding="utf-8")
    py.chmod(0o755)
    return py


def установка(tmp_path: Path, *, device: str = "cuda", версия: str = "3.0.0") -> tuple[Path, Path]:
    """Каталоги установки и данных — ровно в том объёме, что читает скрипт."""
    prefix = tmp_path / "prefix"
    (prefix / "server").mkdir(parents=True, exist_ok=True)
    (prefix / "VERSION").write_text(версия + "\n", encoding="utf-8")
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    строки = "server_port: 8081\n"
    if device:
        строки += f"device: {device}\ncompute_type: float16\n"
    (data / "config.yaml").write_text(строки, encoding="utf-8")
    return prefix, data


def оболочка(repo_root: Path, тело: str, env: dict | None = None):
    """Подключает библиотеки скриптов и выполняет фрагмент."""
    lib = repo_root / "scripts" / "lib"
    скрипт = (f'source "{lib}/common.sh"\nsource "{lib}/detect.sh"\n'
              f'source "{lib}/gpu.sh"\n{тело}')
    полное = {**os.environ, "ASRHUB_QUIET": "0", **(env or {})}
    return subprocess.run([BASH, "-c", скрипт], capture_output=True, text=True,
                          env=полное, timeout=120)


# ---------------------------------------------------------------------------
# Чтение настройки
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("строка,ожидание", [
    ("device: cuda", "cuda"),
    ("device: cuda:1", "cuda:1"),
    ("  device: cpu", "cpu"),
    ('device: "cuda"', "cuda"),
    ("device: cuda   # выбрано установщиком", "cuda"),
    ("device_index: 1", ""),          # похожая строка, но не та
    ("server_port: 8081", ""),
])
def test_чтение_настройки_устройства(repo_root: Path, tmp_path: Path,
                                     строка: str, ожидание: str):
    """`device_index` не должен читаться как `device`.

    Ошибиться здесь легко и дорого: скрипт решил бы, что настроена карта
    номер такой-то, и отказался бы обновлять исправный сервер.
    """
    (tmp_path / "config.yaml").write_text(строка + "\n", encoding="utf-8")
    итог = оболочка(repo_root, f'config_device "{tmp_path}"')
    assert итог.stdout.strip() == ожидание


def test_без_файла_настроек_ответ_пустой(repo_root: Path, tmp_path: Path):
    итог = оболочка(repo_root, f'config_device "{tmp_path / "нет"}"')
    assert итог.stdout.strip() == ""
    assert итог.returncode == 0


# ---------------------------------------------------------------------------
# Версии драйвера
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a,b,сходятся", [
    ("595.91.07", "595.91", True),       # одна версия, записанная по-разному
    ("595.91.07", "595.91.07", True),
    ("595.91.07", "595.99", False),
    ("595.91.07", "596.91", False),
    ("", "595.99", True),                # сравнивать не с чем — не выдумываем
    ("595.91", "", True),
])
def test_сравнение_версий(repo_root: Path, a: str, b: str, сходятся: bool):
    итог = оболочка(repo_root, f'nvidia_versions_match "{a}" "{b}" && echo да || echo нет')
    assert итог.stdout.strip() == ("да" if сходятся else "нет")


def test_драйвер_обновлён_без_перезагрузки(repo_root: Path, tmp_path: Path):
    """Тот самый случай с боевого сервера.

    Библиотеки новее модуля в памяти, а на диске уже лежит новый модуль —
    значит, перезагрузка поможет, и сказать надо именно это.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    итог = оболочка(repo_root, "nvidia_driver_verdict", env)
    assert итог.stdout.strip() == "reboot|595.91.07|595.99.02"


def test_модуль_не_пересобрался(repo_root: Path, tmp_path: Path):
    """Библиотеки новые, а на диске лежит тот же модуль, что и в памяти.

    Перезагрузка тут не поможет, и отправлять в reboot — значит потратить
    чужой простой впустую.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False)
    assert оболочка(repo_root, "nvidia_driver_verdict", env).stdout.strip() \
        == "rebuild|595.91.07|595.99"


def test_модуль_собран_под_другое_ядро(repo_root: Path, tmp_path: Path):
    """Вместе с драйвером приехало новое ядро — и модуль собран под него.

    Под работающим ядром модуля действительно нет, и `modinfo nvidia` честно
    молчит. Но перезагрузка (уже в новое ядро) как раз помогает, а отправлять
    человека в dkms — значит гонять его по кругу вокруг исправного модуля.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False,
                  ядра={"6.8.0-60-generic": "595.99.02"})
    итог = оболочка(repo_root, "nvidia_driver_verdict", env)
    assert итог.stdout.strip() == "other-kernel|595.91.07|6.8.0-60-generic"


def test_про_другое_ядро_сказано_прямо(repo_root: Path, tmp_path: Path):
    """Одного приговора мало: человеку нужно имя ядра и что с ним делать."""
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False,
                  ядра={"6.8.0-60-generic": "595.99.02"})
    вывод = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    текст = вывод.stdout + вывод.stderr
    assert "6.8.0-60-generic" in текст
    assert "reboot" in текст


def test_модуль_работающего_ядра_не_выдаётся_за_чужой(repo_root: Path, tmp_path: Path):
    """`modinfo nvidia` находит модуль по имени не везде — в RHEL он в extra/.

    Тогда версию под работающим ядром берём обходом каталогов. Без этого
    исправно собранный модуль выглядел бы отсутствующим, и человеку
    предложили бы «загрузиться в ядро», в котором он и так сидит.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="", библиотеки="595.99",
                  smi_работает=False, ядро_имя="6.8.0-50-generic",
                  ядра={"6.8.0-50-generic": "595.99.02"})
    итог = оболочка(repo_root, "nvidia_driver_verdict", env)
    assert итог.stdout.strip() == "reboot|595.91.07|595.99.02"
    вывод = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    текст = вывод.stdout + вывод.stderr
    assert "Перезагрузка поможет" in текст
    assert "выберите" not in текст, "предложено грузиться в то ядро, что уже работает"


def test_работающее_ядро_не_предлагают_загрузить(repo_root: Path, tmp_path: Path):
    """Под работающим ядром два модуля, а modprobe берёт старый.

    Так бывает после ручной установки поверх пакетной. Новый модуль на диске
    есть, но заработает он только после depmod — а совет «загрузитесь в ядро
    6.8.0-50» человеку, который в нём и сидит, выглядит издевательством.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False,
                  ядро_имя="6.8.0-50-generic",
                  ядра={"6.8.0-50-generic": "595.99.02"})
    итог = оболочка(repo_root, "nvidia_driver_verdict", env)
    assert not итог.stdout.startswith("other-kernel|"), итог.stdout
    вывод = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    assert "выберите" not in вывод.stdout + вывод.stderr


def test_чужое_ядро_со_старым_модулем_не_спасает(repo_root: Path, tmp_path: Path):
    """Модуль под другим ядром есть, но он той же старой версии.

    Загружаться в него незачем — там будет ровно то же расхождение.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False,
                  ядра={"6.8.0-40-generic": "595.91.07"})
    assert оболочка(repo_root, "nvidia_driver_verdict", env).stdout.strip() \
        == "rebuild|595.91.07|595.99"


def test_без_modinfo_не_выдумываем_приговор(repo_root: Path, tmp_path: Path):
    """Нет modinfo — значит, сравнить с диском нечем.

    «Модуль не собрался» тут было бы выдачей отсутствия данных за вывод, и
    человек пошёл бы пересобирать исправный модуль.
    """
    env = драйвер(tmp_path, загружен="595.91.07", библиотеки="595.99",
                  smi_работает=False, modinfo_есть=False)
    итог = оболочка(repo_root, "nvidia_driver_verdict", env)
    assert итог.stdout.strip() == "unknown-disk|595.91.07|595.99"
    вывод = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    assert "нечем" in вывод.stdout + вывод.stderr


def test_совет_про_заголовки_ядра(repo_root: Path, tmp_path: Path):
    """Чаще всего dkms не собрал именно из-за отсутствующих заголовков."""
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.91.07",
                  библиотеки="595.99", smi_работает=False)
    вывод = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    текст = вывод.stdout + вывод.stderr
    assert "linux-headers" in текст
    assert "dkms autoinstall" in текст


def test_версии_сходятся(repo_root: Path, tmp_path: Path):
    env = драйвер(tmp_path)
    assert оболочка(repo_root, "nvidia_driver_verdict", env).stdout.startswith("ok|")


def test_без_драйвера_приговора_нет(repo_root: Path, tmp_path: Path):
    """Ни /proc, ни nvidia-smi — сравнивать нечего, и выдумывать нечего."""
    env = {"PATH": "/usr/bin:/bin", "ASRHUB_NVIDIA_PROC": str(tmp_path / "нет"),
           "ASRHUB_NVIDIA_DEV": str(tmp_path / "нет")}
    assert оболочка(repo_root, "nvidia_driver_verdict", env).stdout.startswith("unknown|")


def test_разбор_читает_версию_из_отказа_nvidia_smi(repo_root: Path, tmp_path: Path):
    """nvidia-smi печатает версию библиотек именно тогда, когда отказывается.

    Код возврата у неё при этом единица, и под `set -o pipefail` разбор
    когда-то падал ровно в тот момент, когда был нужен.
    """
    env = драйвер(tmp_path, библиотеки="595.99", smi_работает=False)
    итог = оболочка(repo_root, "nvidia_userspace_version", env)
    assert итог.stdout.strip() == "595.99"
    assert итог.returncode == 0


# ---------------------------------------------------------------------------
# Проба у питона
# ---------------------------------------------------------------------------


def test_проба_видит_сломанную_карту(repo_root: Path, tmp_path: Path):
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" cuda')
    assert итог.stdout.startswith("fail|cuda|")
    assert "invalid device ordinal" in итог.stdout


def test_проба_видит_исправную_карту(repo_root: Path, tmp_path: Path):
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" cuda')
    assert итог.stdout.startswith("ok|cuda|")
    assert "RTX 5090" in итог.stdout


def test_проба_ловит_чужой_номер_карты(repo_root: Path, tmp_path: Path):
    """«cuda:3» при единственной карте — не поломка драйвера, а опечатка."""
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" cuda:3')
    assert итог.stdout.startswith("fail|cuda:3|")
    assert "3" in итог.stdout and "1" in итог.stdout


def test_при_auto_карта_не_повод_для_отказа(repo_root: Path, tmp_path: Path):
    """«auto» сам уходит на процессор — это медленно, но не поломка."""
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" auto')
    assert итог.stdout.startswith("cpu|auto")


def test_на_процессоре_проверять_нечего(repo_root: Path, tmp_path: Path):
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" cpu')
    assert итог.stdout.startswith("cpu|cpu|")


def test_без_torch_проба_молчит(repo_root: Path, tmp_path: Path):
    """Движку вроде whisper.cpp или vosk torch не нужен вовсе.

    Отказ здесь означал бы, что обновление не ставится на исправный сервер.
    """
    py = питон(tmp_path, None)
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{py}" "{repo_root / "server"}" cuda')
    assert итог.stdout.startswith("skip|")


def test_без_интерпретатора_проба_молчит(repo_root: Path, tmp_path: Path):
    итог = оболочка(repo_root,
                    f'gpu_torch_probe "{tmp_path / "нет"}" "{repo_root}" cuda')
    assert итог.stdout.startswith("skip|")
    assert итог.returncode == 0


# ---------------------------------------------------------------------------
# Разбор целиком
# ---------------------------------------------------------------------------


def test_разбор_отказывает_на_настроенной_карте(repo_root: Path, tmp_path: Path):
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    _, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root,
                    f'gpu_runtime_report "{py}" "{data}" "{repo_root / "server"}" asrhub', env)
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 1
    assert "карта процессу недоступна" in вывод
    assert "модуль в памяти 595.91.07, на диске 595.99.02" in вывод
    assert "sudo reboot" in вывод


def test_разбор_не_отказывает_при_auto(repo_root: Path, tmp_path: Path):
    """Сервер уйдёт на процессор сам — обновлению это не помеха."""
    env = драйвер(tmp_path, smi_работает=False, библиотеки="595.99",
                  загружен="595.91.07", на_диске="595.99.02")
    _, data = установка(tmp_path, device="auto")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root,
                    f'gpu_runtime_report "{py}" "{data}" "{repo_root / "server"}" asrhub', env)
    assert итог.returncode == 0
    assert "на процессоре" in итог.stdout + итог.stderr


def test_разбор_молчит_на_исправной_карте(repo_root: Path, tmp_path: Path):
    env = драйвер(tmp_path)
    _, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    итог = оболочка(repo_root,
                    f'gpu_runtime_report "{py}" "{data}" "{repo_root / "server"}" asrhub', env)
    assert итог.returncode == 0
    assert "доступна процессу" in итог.stdout


def test_разбор_называет_чужой_номер_в_окружении_службы(repo_root: Path, tmp_path: Path):
    """CUDA_VISIBLE_DEVICES у службы и у человека — разные вещи.

    Спрашивать свою оболочку бессмысленно: расходятся они как раз здесь.
    """
    env = драйвер(tmp_path)
    подложка(tmp_path / "bin", "systemctl", '''
[[ "$*" == *Environment* ]] && { echo "ASRHUB_DATA_DIR=/var/lib/asrhub CUDA_VISIBLE_DEVICES=1"; exit 0; }
exit 0
''')
    итог = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    вывод = итог.stdout + итог.stderr
    assert "CUDA_VISIBLE_DEVICES=1" in вывод


def test_разбор_называет_отвал_карты(repo_root: Path, tmp_path: Path):
    """Xid — это отвал. После него процесс не поправится сам."""
    env = драйвер(tmp_path)
    подложка(tmp_path / "bin", "dmesg",
             'echo "[171657.328] NVRM: Xid (PCI:0000:01:00): 79, pid=1, GPU has fallen off the bus."\n')
    итог = оболочка(repo_root, "gpu_runtime_diagnose asrhub", env)
    вывод = итог.stdout + итог.stderr
    assert "Xid" in вывод
    assert "restart" in вывод


# ---------------------------------------------------------------------------
# Скрипт обновления
# ---------------------------------------------------------------------------


def обновление(repo_root: Path, prefix: Path, data: Path, env: dict,
               *args: str, ввод: str = ""):
    строка = [BASH, str(repo_root / "scripts" / "update.sh"),
              "--prefix", str(prefix), "--data", str(data),
              "--source", str(repo_root), *args]
    полное = {**os.environ, "ASRHUB_QUIET": "0", **env}
    return subprocess.run(строка, capture_output=True, text=True, input=ввод,
                          env=полное, timeout=300)


def test_проверка_обновления_видит_сломанную_карту(repo_root: Path, tmp_path: Path):
    """`--check` на сервере с недоступной картой обязан сказать об этом.

    И сказать отдельным кодом: задача в cron должна отличать «карта
    недоступна» от «скрипт не отработал».
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    prefix, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(py, prefix / "venv" / "bin" / "python")
    итог = обновление(repo_root, prefix, data, env, "--check")
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 3, вывод
    assert "карта процессу недоступна" in вывод
    assert "Перезагрузка поможет" in вывод


def test_проверка_обновления_на_исправной_карте(repo_root: Path, tmp_path: Path):
    env = драйвер(tmp_path)
    prefix, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(py, prefix / "venv" / "bin" / "python")
    итог = обновление(repo_root, prefix, data, env, "--check")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "доступна процессу" in итог.stdout


def test_обновление_не_трогает_установку_при_отказе(repo_root: Path, tmp_path: Path):
    """Главное, ради чего проверка стоит до снимка.

    Без терминала вопрос «обновиться всё равно?» берёт умолчание «нет» — и
    к этому моменту не должно быть ни снимка, ни единого изменённого файла.
    """
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    prefix, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(py, prefix / "venv" / "bin" / "python")
    снимок = prefix.parent / "asrhub-snapshot"
    итог = обновление(repo_root, prefix, data, env)
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 1, вывод
    assert "Сначала карта" in вывод
    assert not снимок.exists(), "снимок сделан, хотя обновление не начиналось"
    assert (prefix / "VERSION").read_text(encoding="utf-8").strip() == "3.0.0"


def test_ключ_отключает_проверку(repo_root: Path, tmp_path: Path):
    """Проверку должно быть чем обойти: бывает, что обновляются ради неё же."""
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    prefix, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(py, prefix / "venv" / "bin" / "python")
    итог = обновление(repo_root, prefix, data, env, "--check", "--skip-gpu-check")
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 0, вывод
    assert "пропущена" in вывод
    assert "карта процессу недоступна" not in вывод


def test_обновление_движков_карту_не_трогает(repo_root: Path, tmp_path: Path):
    """`--engines-only` обновляет пакеты и к устройству отношения не имеет."""
    env = драйвер(tmp_path, загружен="595.91.07", на_диске="595.99.02",
                  библиотеки="595.99", smi_работает=False)
    prefix, data = установка(tmp_path, device="cuda")
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    (prefix / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(py, prefix / "venv" / "bin" / "python")
    итог = обновление(repo_root, prefix, data, env, "--check", "--engines-only")
    assert итог.returncode == 0
    assert "карта процессу недоступна" not in итог.stdout + итог.stderr


def test_справка_называет_коды_возврата(repo_root: Path):
    итог = subprocess.run([BASH, str(repo_root / "scripts" / "update.sh"), "--help"],
                          capture_output=True, text=True, timeout=60)
    assert "3  только с --check" in итог.stdout
    assert "--skip-gpu-check" in итог.stdout


# ---------------------------------------------------------------------------
# Запасной разбор — когда спросить сервер нельзя
# ---------------------------------------------------------------------------
#
# Проба сначала зовёт ту же функцию, которой пользуется сервер, чтобы ответы
# скрипта и сервера не разошлись. Но её может не быть: доктор на старой
# установке или обновление, запущенное без --source, видят прежнюю версию
# кода. Тогда разбор идёт своими силами — и этот путь ошибается ровно так же
# дорого, как основной.


def test_запасной_разбор_видит_сломанную_карту(repo_root: Path, tmp_path: Path):
    """Каталог без asrhub — значит, спросить сервер нечем."""
    пусто = tmp_path / "старый-код"
    пусто.mkdir()
    py = питон(tmp_path, ТОРЧ_СЛОМАН)
    итог = оболочка(repo_root, f'gpu_torch_probe "{py}" "{пусто}" cuda')
    assert итог.stdout.startswith("fail|cuda|")
    assert "invalid device ordinal" in итог.stdout


def test_запасной_разбор_ловит_чужой_номер(repo_root: Path, tmp_path: Path):
    пусто = tmp_path / "старый-код"
    пусто.mkdir()
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    итог = оболочка(repo_root, f'gpu_torch_probe "{py}" "{пусто}" cuda:3')
    assert итог.stdout.startswith("fail|cuda:3|")
    assert "3" in итог.stdout and "1" in итог.stdout


def test_запасной_разбор_пропускает_исправную_карту(repo_root: Path, tmp_path: Path):
    пусто = tmp_path / "старый-код"
    пусто.mkdir()
    py = питон(tmp_path, ТОРЧ_ЖИВ)
    итог = оболочка(repo_root, f'gpu_torch_probe "{py}" "{пусто}" cuda')
    assert итог.stdout.startswith("ok|cuda|")


def test_запасной_разбор_спрашивает_саму_карту(repo_root: Path, tmp_path: Path):
    """Счётчик устройств отвечает и на сломанном драйвере.

    Настоящая инициализация начинается с обращения к карте по имени —
    поэтому разбор на счётчике не заканчивается.
    """
    пусто = tmp_path / "старый-код"
    пусто.mkdir()
    py = питон(tmp_path, '''
class _Cuda:
    @staticmethod
    def device_count(): return 1
    @staticmethod
    def is_available(): return True
    @staticmethod
    def get_device_name(i=0): raise RuntimeError("CUDA error: unknown error")
cuda = _Cuda()
class backends:
    class mps:
        @staticmethod
        def is_available(): return False
''')
    итог = оболочка(repo_root, f'gpu_torch_probe "{py}" "{пусто}" cuda')
    assert итог.stdout.startswith("fail|cuda|")
    assert "не отвечает" in итог.stdout


# ---------------------------------------------------------------------------
# Установка: чем сервер будет считать, решается один раз и надолго
# ---------------------------------------------------------------------------
#
# Устройство в config.yaml выбирает установщик — по nvidia-smi. Считать будет
# torch. Разойтись они могут и в первый же день: драйвер поставлен, карта
# видна, а колёса PyTorch приехали процессорные. «device: cuda» в этом
# положении отключает уход на процессор, и сервер падает на каждом задании —
# при внешне исправной установке.


def конфигурация(repo_root: Path, py: Path, env: dict) -> tuple[int, str]:
    """Строки устройства для config.yaml и код возврата проверки."""
    # Код возврата ловится через «|| код=$?»: функция отвечает единицей по
    # делу, а под `set -o errexit` голый вызов оборвал бы оболочку. Ровно так
    # её и зовёт установщик.
    итог = оболочка(
        repo_root,
        'rc=0\n'
        f'gpu_config_lines_checked "{py}" "{repo_root / "server"}" || rc=$?\n'
        'echo "КОД:${rc}"',
        env)
    текст = итог.stdout
    код = int(текст.rsplit("КОД:", 1)[1].strip() or 0)
    return код, текст.rsplit("КОД:", 1)[0]


def карта_на_шине(tmp_path: Path, env: dict) -> dict:
    """Машина, где nvidia-smi показывает исправную карту.

    Установщик в этом состоянии и записывает «device: cuda» — ему довольно
    того, что карта отвечает.
    """
    pci = tmp_path / "pci" / "0000:01:00.0"
    pci.mkdir(parents=True, exist_ok=True)
    (pci / "vendor").write_text("0x10de\n", encoding="utf-8")
    (pci / "class").write_text("0x030000\n", encoding="utf-8")
    (pci / "device").write_text("0x2684\n", encoding="utf-8")
    (pci / "resource").write_text(
        "0x0000004000000000 0x00000040ffffffff 0x000000000014220c\n"
        + "0x0000000000000000 0x0000000000000000 0x0000000000000000\n" * 6,
        encoding="utf-8")
    return {**env, "ASRHUB_PCI_ROOT": str(tmp_path / "pci")}


def test_установка_не_пишет_cuda_если_питон_карты_не_видит(repo_root: Path,
                                                          tmp_path: Path):
    """Главная развилка установки.

    «device: cuda» на такой машине — это сервер, у которого падает каждое
    задание. «auto» — сервер, который работает медленно и возьмёт карту, как
    только она отзовётся.
    """
    env = карта_на_шине(tmp_path, драйвер(tmp_path))
    код, строки = конфигурация(repo_root, питон(tmp_path, ТОРЧ_СЛОМАН), env)
    assert код == 1
    assert "device:" not in строки


def test_установка_пишет_cuda_на_исправной_карте(repo_root: Path, tmp_path: Path):
    env = карта_на_шине(tmp_path, драйвер(tmp_path))
    код, строки = конфигурация(repo_root, питон(tmp_path, ТОРЧ_ЖИВ), env)
    assert код == 0
    assert "device: cuda" in строки


def test_без_карты_на_шине_проверять_нечего(repo_root: Path, tmp_path: Path):
    """Машина без карты: строк нет и без всякой пробы, отказа тоже нет."""
    пусто = tmp_path / "pci-пусто"
    пусто.mkdir()
    env = {**драйвер(tmp_path), "ASRHUB_PCI_ROOT": str(пусто)}
    код, строки = конфигурация(repo_root, питон(tmp_path, ТОРЧ_СЛОМАН), env)
    assert код == 0
    assert строки.strip() == ""


def test_без_питона_выбор_установщика_остаётся(repo_root: Path, tmp_path: Path):
    """Проверять нечем — не повод отказываться от найденной карты.

    Так бывает в docker-режиме и при --dry-run: виртуального окружения ещё
    нет, а конфигурацию писать уже надо.
    """
    env = карта_на_шине(tmp_path, драйвер(tmp_path))
    код, строки = конфигурация(repo_root, tmp_path / "нет-такого", env)
    assert код == 0
    assert "device: cuda" in строки
