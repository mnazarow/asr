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
            smi_работает: bool = True) -> dict[str, str]:
    """Машина с заданным состоянием драйвера NVIDIA."""
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
  -L) echo "GPU 0: NVIDIA GeForce RTX 5090 (UUID: GPU-x)" ;;
  *) echo "NVIDIA-SMI {библиотеки}  Driver Version: {библиотеки}" ;;
esac
''')
    else:
        подложка(bin_dir, "nvidia-smi", f'''
echo "Failed to initialize NVML: Driver/library version mismatch"
echo "NVML library version: {библиотеки}"
exit 1
''')
    подложка(bin_dir, "modinfo", f'''
[[ "$1" == "-F" && "$2" == "version" ]] && {{ echo "{на_диске}"; exit 0; }}
exit 1
''')
    dev = tmp_path / "dev"
    dev.mkdir(exist_ok=True)
    (dev / "nvidia0").touch()
    (dev / "nvidiactl").touch()
    return {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ASRHUB_NVIDIA_PROC": str(proc),
        "ASRHUB_NVIDIA_DEV": str(dev),
    }


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
