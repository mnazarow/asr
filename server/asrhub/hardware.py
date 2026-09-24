"""Определение доступного оборудования и подбор оптимальных настроек.

Модуль намеренно не импортирует torch на уровне модуля: сервер должен
запускаться и показывать интерфейс даже без установленных движков.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any


@dataclass(slots=True)
class GPUInfo:
    index: int
    name: str
    memory_total_mb: int
    memory_free_mb: int = 0
    driver: str = ""
    compute_capability: str = ""
    vendor: str = "nvidia"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HardwareInfo:
    os_name: str
    os_version: str
    arch: str
    cpu_model: str
    cpu_cores_physical: int
    cpu_cores_logical: int
    ram_total_gb: float
    ram_available_gb: float
    disk_free_gb: float
    gpus: list[GPUInfo] = field(default_factory=list)
    accelerator: str = "cpu"          # cuda | rocm | mps | cpu
    cuda_version: str = ""
    cudnn_version: str = ""
    torch_version: str = ""
    ffmpeg: bool = False
    ffmpeg_version: str = ""
    python_version: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["gpus"] = [g.to_dict() for g in self.gpus]
        return data

    @property
    def total_vram_gb(self) -> float:
        return round(sum(g.memory_total_mb for g in self.gpus) / 1024, 1)

    @property
    def best_gpu(self) -> GPUInfo | None:
        return max(self.gpus, key=lambda g: g.memory_total_mb) if self.gpus else None


def _run(cmd: list[str], timeout: float = 6.0, *, со_стдерр: bool = False) -> str:
    """Вывод команды. Код возврата не проверяется намеренно.

    ``со_стдерр`` нужен там, где полезное сообщение программа печатает в
    поток ошибок: nvidia-smi именно так сообщает о расхождении версий
    драйвера — то есть ровно тогда, когда её и спрашивают.
    """
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        текст = out.stdout or ""
        if со_стдерр:
            текст += "\n" + (out.stderr or "")
        return текст.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_model() -> str:
    system = platform.system()
    if system == "Linux":
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
    elif system == "Darwin":
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if name:
            return name
    elif system == "Windows":
        name = os.environ.get("PROCESSOR_IDENTIFIER", "")
        if name:
            return name
    return platform.processor() or "неизвестно"


def _physical_cores() -> int:
    system = platform.system()
    if system == "Linux":
        try:
            ids = set()
            core = pkg = None
            with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if line.startswith("core id"):
                        core = line.split(":", 1)[1].strip()
                    elif line.startswith("physical id"):
                        pkg = line.split(":", 1)[1].strip()
                    elif not line.strip() and core is not None and pkg is not None:
                        ids.add((pkg, core))
                        core = pkg = None
            if ids:
                return len(ids)
        except OSError:
            pass
    elif system == "Darwin":
        val = _run(["sysctl", "-n", "hw.physicalcpu"])
        if val.isdigit():
            return int(val)
    return os.cpu_count() or 1


def _memory_gb() -> tuple[float, float]:
    """Возвращает (всего, доступно) в гигабайтах."""
    system = platform.system()
    if system == "Linux":
        try:
            info: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as fh:
                for line in fh:
                    key, _, rest = line.partition(":")
                    val = rest.strip().split()
                    if val and val[0].isdigit():
                        info[key] = int(val[0])
            total = info.get("MemTotal", 0) / 1024 / 1024
            avail = info.get("MemAvailable", info.get("MemFree", 0)) / 1024 / 1024
            return round(total, 1), round(avail, 1)
        except OSError:
            pass
    elif system == "Darwin":
        total_b = _run(["sysctl", "-n", "hw.memsize"])
        if total_b.isdigit():
            total = int(total_b) / 1024 ** 3
            return round(total, 1), round(total * 0.5, 1)
    try:
        import psutil  # type: ignore

        vm = psutil.virtual_memory()
        return round(vm.total / 1024 ** 3, 1), round(vm.available / 1024 ** 3, 1)
    except Exception:
        return 0.0, 0.0


def _nvidia_gpus() -> list[GPUInfo]:
    if not shutil.which("nvidia-smi"):
        return []
    out = _run([
        "nvidia-smi",
        "--query-gpu=index,name,memory.total,memory.free,driver_version,compute_cap",
        "--format=csv,noheader,nounits",
    ])
    gpus: list[GPUInfo] = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        try:
            gpus.append(GPUInfo(
                index=int(parts[0]),
                name=parts[1],
                memory_total_mb=int(float(parts[2])),
                memory_free_mb=int(float(parts[3])),
                driver=parts[4] if len(parts) > 4 else "",
                compute_capability=parts[5] if len(parts) > 5 else "",
                vendor="nvidia",
            ))
        except (ValueError, IndexError):
            continue
    return gpus


def _amd_gpus() -> list[GPUInfo]:
    if not shutil.which("rocm-smi"):
        return []
    out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"])
    gpus: list[GPUInfo] = []
    for idx, line in enumerate(out.splitlines()[1:]):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        name = next((p for p in parts[1:] if p and not p.replace(".", "").isdigit()), "AMD GPU")
        total = 0
        for p in parts:
            if p.isdigit() and int(p) > 1024 * 1024:
                total = int(p) // (1024 * 1024)
                break
        gpus.append(GPUInfo(index=idx, name=name, memory_total_mb=total, vendor="amd"))
    return gpus


def _ffmpeg() -> tuple[bool, str]:
    if not shutil.which("ffmpeg"):
        return False, ""
    out = _run(["ffmpeg", "-version"])
    first = out.splitlines()[0] if out else ""
    ver = first.split(" ")[2] if len(first.split(" ")) > 2 else ""
    return True, ver


def _torch_info() -> tuple[str, str, str, bool]:
    """Возвращает (версия torch, версия cuda, версия cudnn, доступен ли mps)."""
    try:
        import torch  # type: ignore
    except Exception:
        return "", "", "", False
    cuda = getattr(torch.version, "cuda", "") or ""
    cudnn = ""
    try:
        if torch.backends.cudnn.is_available():
            cudnn = str(torch.backends.cudnn.version() or "")
    except Exception:
        pass
    mps = False
    try:
        mps = bool(torch.backends.mps.is_available())
    except Exception:
        pass
    return torch.__version__, cuda, cudnn, mps


@lru_cache(maxsize=1)
def detect(data_dir: str = ".") -> HardwareInfo:
    """Полное определение оборудования. Результат кешируется на время процесса."""
    total_ram, avail_ram = _memory_gb()
    ffmpeg_ok, ffmpeg_ver = _ffmpeg()
    torch_ver, cuda_ver, cudnn_ver, mps_ok = _torch_info()

    gpus = _nvidia_gpus()
    accelerator = "cpu"
    if gpus:
        accelerator = "cuda"
    else:
        gpus = _amd_gpus()
        if gpus:
            accelerator = "rocm"
        elif mps_ok or (platform.system() == "Darwin" and platform.machine() == "arm64"):
            accelerator = "mps"

    try:
        usage = shutil.disk_usage(data_dir)
        disk_free = round(usage.free / 1024 ** 3, 1)
    except OSError:
        disk_free = 0.0

    info = HardwareInfo(
        os_name=platform.system(),
        os_version=platform.release(),
        arch=platform.machine(),
        cpu_model=_cpu_model(),
        cpu_cores_physical=_physical_cores(),
        cpu_cores_logical=os.cpu_count() or 1,
        ram_total_gb=total_ram,
        ram_available_gb=avail_ram,
        disk_free_gb=disk_free,
        gpus=gpus,
        accelerator=accelerator,
        cuda_version=cuda_ver,
        cudnn_version=cudnn_ver,
        torch_version=torch_ver,
        ffmpeg=ffmpeg_ok,
        ffmpeg_version=ffmpeg_ver,
        python_version=platform.python_version(),
    )

    if not ffmpeg_ok:
        info.warnings.append(
            "Не найден ffmpeg. Без него доступны только файлы WAV 16 кГц моно. "
            "Установите: apt install ffmpeg / brew install ffmpeg / winget install ffmpeg")
    if accelerator == "cpu" and total_ram and total_ram < 8:
        info.warnings.append(
            f"Всего {total_ram} ГБ оперативной памяти. Для моделей уровня large "
            "рекомендуется минимум 16 ГБ; выберите модель поменьше или включите int8.")
    if disk_free and disk_free < 20:
        info.warnings.append(
            f"На диске свободно {disk_free} ГБ. Полный набор моделей занимает свыше 100 ГБ.")
    if accelerator == "cuda" and cuda_ver and cudnn_ver:
        major = cudnn_ver[:1]
        if cuda_ver.startswith("12") and major == "8":
            info.warnings.append(
                "Обнаружены CUDA 12 и cuDNN 8: для faster-whisper требуется ctranslate2==4.4.0.")
    if accelerator == "mps":
        info.warnings.append(
            "Apple Silicon: часть движков (NeMo, faster-whisper на GPU) не поддерживает MPS. "
            "Для macOS рекомендуется whisper.cpp с Metal и Core ML.")
    return info


def recommended_settings(info: HardwareInfo | None = None) -> dict[str, Any]:
    """Рекомендуемые значения ключевых параметров под обнаруженное оборудование."""
    info = info or detect()
    gpu = info.best_gpu
    vram_gb = (gpu.memory_total_mb / 1024) if gpu else 0.0

    if info.accelerator == "cuda":
        cc = gpu.compute_capability if gpu else ""
        try:
            cc_val = float(cc) if cc else 0.0
        except ValueError:
            cc_val = 0.0
        compute_type = "float16" if cc_val >= 7.0 else "float32"
        if vram_gb >= 24:
            batch, model, cache = 24, "gigaam-v3-rnnt", 3
        elif vram_gb >= 12:
            batch, model, cache = 16, "gigaam-v3-rnnt", 2
        elif vram_gb >= 8:
            batch, model, cache = 8, "gigaam-v3-rnnt", 1
        else:
            batch, model, cache = 4, "gigaam-v3-ctc", 1
        device = "cuda"
        concurrent = max(1, len(info.gpus))
    elif info.accelerator == "rocm":
        device, compute_type, batch, cache = "rocm", "float16", 8, 1
        model = "gigaam-v3-ctc"
        concurrent = 1
    elif info.accelerator == "mps":
        device, compute_type, batch, cache = "mps", "float16", 4, 1
        model = "whispercpp-large-v3-turbo-q5_0"
        concurrent = 1
    else:
        device, compute_type, cache = "cpu", "int8", 1
        cores = info.cpu_cores_physical
        batch = 4 if cores >= 8 else 2
        model = "gigaam-v3-ctc" if info.ram_total_gb >= 8 else "faster-whisper-small"
        concurrent = max(1, min(4, cores // 4))

    return {
        "device": device,
        "compute_type": compute_type,
        "batch_size": batch,
        "model": model,
        "model_cache_size": cache,
        "max_concurrent_jobs": concurrent,
        "cpu_threads": 0 if info.accelerator != "cpu" else max(1, info.cpu_cores_physical - 1),
        "_reason": _explain(info, vram_gb),
    }


def _explain(info: HardwareInfo, vram_gb: float) -> str:
    if info.accelerator == "cuda":
        gpu = info.best_gpu
        return (f"Обнаружена видеокарта {gpu.name if gpu else 'NVIDIA'} "
                f"({vram_gb:.0f} ГБ). Выбраны вычисления float16 и размер пакета "
                f"под доступную видеопамять.")
    if info.accelerator == "rocm":
        return "Обнаружена видеокарта AMD (ROCm). Часть движков поддерживает её ограниченно."
    if info.accelerator == "mps":
        return ("Apple Silicon: рекомендован whisper.cpp с Metal и Core ML — "
                "это самый быстрый путь на macOS.")
    return (f"Видеокарта не обнаружена. Выбран режим int8 на {info.cpu_cores_physical} "
            f"физических ядрах — единственный практичный вариант на процессоре.")


def check_model_fits(vram_needed_gb: float, info: HardwareInfo | None = None) -> tuple[bool, str]:
    """Проверяет, поместится ли модель в доступную память."""
    info = info or detect()
    if info.accelerator in ("cuda", "rocm"):
        gpu = info.best_gpu
        if gpu is None:
            return False, "Видеокарта не найдена."
        free_gb = (gpu.memory_free_mb or gpu.memory_total_mb) / 1024
        if vram_needed_gb > free_gb:
            return False, (f"Модели нужно около {vram_needed_gb:.1f} ГБ видеопамяти, "
                           f"свободно {free_gb:.1f} ГБ. Уменьшите размер пакета, "
                           f"включите int8 или выберите модель полегче.")
        return True, ""
    needed_ram = vram_needed_gb * 1.5
    if info.ram_available_gb and needed_ram > info.ram_available_gb:
        return False, (f"Модели нужно около {needed_ram:.1f} ГБ оперативной памяти, "
                       f"доступно {info.ram_available_gb:.1f} ГБ.")
    return True, ""


def проверить_ускоритель(device: str) -> tuple[bool, str]:
    """Отвечает, доступно ли вычислительное устройство прямо сейчас.

    Кеша здесь нет намеренно: `detect()` кешируется на весь процесс, а этот
    ответ обязан быть свежим. Карта отваливается на ходу — Xid в dmesg, и
    дальше любое обращение к CUDA возвращает одну и ту же липкую ошибку. Ответ
    из кеша в такой момент врал бы ровно тогда, когда его читают.

    Проверка идёт до загрузки модели и отвечает за один вопрос: видит ли
    процесс карту. Раньше на этот вопрос отвечала сама библиотека движка —
    посреди загрузки весов, текстом про `cudaGetDeviceCount` и «invalid device
    ordinal», из которого следовало разве что «что-то с CUDA».
    """
    название = (device or "auto").strip().lower()
    if название in ("", "auto", "cpu"):
        return True, ""
    # Устройство не из тех, про которые мы что-то знаем (xpu, npu и прочая
    # экзотика): проверять нечем, и отказывать не за что. Отсев идёт до
    # импорта torch — иначе «нечем проверить» превращалось бы в «PyTorch не
    # установлен», то есть в обвинение на ровном месте.
    if not название.startswith(("cuda", "hip", "rocm", "mps")):
        return True, ""

    try:
        import torch  # type: ignore
    except Exception:                               # noqa: BLE001
        # Отсутствие torch — не доказательство того, что карты нет: движки
        # вроде whisper.cpp, vosk и sherpa-onnx работают на видеокарте без
        # него вовсе. Отказать здесь значило бы уронить их на ровном месте,
        # а движок, которому torch нужен, скажет об этом сам и точнее.
        return True, ""

    if название.startswith("mps"):
        try:
            if torch.backends.mps.is_available():
                return True, ""
        except Exception as exc:                    # noqa: BLE001
            return False, f"Metal (MPS) не отвечает: {exc}"
        return False, "Metal (MPS) недоступен на этой машине."

    try:
        всего = int(torch.cuda.device_count())
    except Exception as exc:                        # noqa: BLE001
        return False, f"CUDA не отвечает на перечислении устройств: {exc}"
    if всего <= 0:
        return False, ("CUDA не видит ни одной карты. Обычно это "
                       "CUDA_VISIBLE_DEVICES, отвалившаяся карта или драйвер, "
                       "обновлённый без перезагрузки.")

    # Номер карты из «cuda:N». Спрашивать несуществующую бессмысленно: именно
    # так и получается «invalid device ordinal», только этажом ниже и без
    # объяснения, какой номер запрашивали и сколько карт есть на самом деле.
    номер = 0
    if ":" in название:
        хвост = название.split(":", 1)[1].strip()
        if hasattr(хвост, "isdigit") and хвост.isdigit():
            номер = int(хвост)
    if номер >= всего:
        return False, (f"Запрошена карта {номер}, а доступно карт: {всего} "
                       f"(номера с 0). Проверьте параметр «device» и "
                       f"CUDA_VISIBLE_DEVICES.")

    # Обращение к самой карте: счётчик устройств отвечает и на сломанном
    # драйвере, а вот имя карты требует уже настоящей инициализации.
    try:
        torch.cuda.get_device_name(номер)
    except Exception as exc:                        # noqa: BLE001
        return False, f"Карта {номер} не отвечает: {exc}"
    return True, ""


#: Откуда читается состояние драйвера NVIDIA. Вынесено в константы по той же
#: причине, что и пути в скриптах: иначе проверить разбор можно было бы только
#: на машине, где драйвер сломан именно нужным образом.
NVIDIA_PROC = "/proc/driver/nvidia/version"
MODULES_ROOT = "/lib/modules"
NVML_GLOBS = (
    "/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*",
    "/usr/lib64/libnvidia-ml.so.*",
    "/usr/lib/aarch64-linux-gnu/libnvidia-ml.so.*",
)


def _версия_модуля(путь: str) -> str:
    """Версия модуля ядра по пути к файлу — спрашивается у modinfo."""
    if not shutil.which("modinfo"):
        return ""
    строки = _run(["modinfo", "-F", "version", путь]).splitlines()
    return строки[0].strip() if строки else ""


def модули_по_ядрам(корень: str = MODULES_ROOT) -> dict[str, str]:
    """Версия модуля nvidia под каждым установленным ядром.

    Нужно ровно для одного различения, которого иначе не сделать: модуль мог
    собраться не под то ядро, что работает сейчас. Так бывает, когда ядро
    обновилось, а DKMS под него драйвер не пересобрал — и `modinfo nvidia`,
    который смотрит только на работающее ядро, об этом честно молчит.
    """
    import glob  # noqa: PLC0415

    if not shutil.which("modinfo"):
        return {}
    # Четыре места. updates/dkms — то, что собрал DKMS; extra — RHEL; готовые
    # модули Ubuntu лежат в kernel/nvidia-<ветка>/ (например nvidia-595srv), и
    # без этого образца пакетный модуль под работающим ядром оставался невидим.
    образцы = ("*/updates/dkms/nvidia.ko*", "*/extra/nvidia.ko*",
               "*/kernel/nvidia*/nvidia.ko*",
               "*/kernel/drivers/video/nvidia.ko*")
    найдено: dict[str, str] = {}
    for образец in образцы:
        for путь in glob.glob(os.path.join(корень, образец)):
            ядро = os.path.relpath(путь, корень).split(os.sep)[0]
            if ядро in найдено:
                continue
            версия = _версия_модуля(путь)
            if версия:
                найдено[ядро] = версия
    return найдено


#: Где лежат исходники драйвера, заведённые .run-установщиком NVIDIA.
DKMS_SRC = "/usr/src"


def dkms_исходники(корень: str = "") -> str:
    """Версия исходников драйвера в /usr/src (каталог nvidia-<версия>)."""
    import glob  # noqa: PLC0415

    for путь in sorted(glob.glob(os.path.join(корень or DKMS_SRC, "nvidia-[0-9]*"))):
        if os.path.isdir(путь):
            return os.path.basename(путь).split("nvidia-", 1)[-1]
    return ""


def dkms_состояние(корень: str = "") -> str:
    """Знает ли DKMS о драйвере NVIDIA: known | stale | unknown | absent.

    Различение не теоретическое. Пустой ``dkms status`` означает не «модуль не
    собрался», а «DKMS не знает ни об одном модуле», и совет собрать модуль
    отправляет человека в пустоту. А если при этом исходники в /usr/src лежат
    — это .run-установщик NVIDIA поверх пакетных модулей: он кладёт свои
    библиотеки мимо dpkg и заводит DKMS, а при обновлении ядра регистрация
    теряется, и загружается пакетный модуль другой версии.
    """
    if not shutil.which("dkms"):
        return "absent"
    if "nvidia" in _run(["dkms", "status"], со_стдерр=True).lower():
        return "known"
    return "stale" if dkms_исходники(корень) else "unknown"


def _версии_сходятся(a: str, b: str) -> bool:
    """«595.91.07» и «595.91» — одна версия, записанная по-разному.

    Пустое значение сравнивать не с чем, и выдумывать расхождение на пустом
    месте хуже, чем промолчать.
    """
    if not a or not b:
        return True
    return a.split(".")[:2] == b.split(".")[:2]


def _версия_библиотек() -> str:
    """Версия пользовательских библиотек NVIDIA.

    nvidia-smi печатает её сам — и печатает именно тогда, когда работать
    отказывается: «Failed to initialize NVML: Driver/library version
    mismatch / NVML library version: 595.99».
    """
    import glob  # noqa: PLC0415
    import re  # noqa: PLC0415

    if shutil.which("nvidia-smi"):
        вывод = _run(["nvidia-smi"], со_стдерр=True)
        совпало = re.search(r"NVML library version:\s*([0-9][0-9.]*)", вывод)
        if совпало:
            return совпало.group(1)
        строка = _run(["nvidia-smi", "--query-gpu=driver_version",
                       "--format=csv,noheader"]).strip().splitlines()
        if строка and строка[0].strip():
            return строка[0].strip()
    for образец in NVML_GLOBS:
        for путь in sorted(glob.glob(образец)):
            хвост = путь.rsplit("libnvidia-ml.so.", 1)[-1]
            if хвост[:1].isdigit():
                return хвост
    return ""


def проверить_драйвер(*, proc: str = "", корень: str = "") -> dict[str, Any]:
    """Сходятся ли модуль ядра NVIDIA и его библиотеки.

    Отдельно от проверки устройства не случайно. «Карта недоступна» — это
    факт, а вот что с ним делать, зависит от того, где разошлись версии:
    перезагрузиться, пересобрать модуль под новое ядро или чинить пакеты. Три
    разных действия, и ошибка стоит чужого простоя.

    Возвращает состояние: ``ok`` — версии сходятся; ``reboot`` — новый модуль
    уже лежит под работающим ядром; ``other-kernel`` — модуль есть, но под
    другим установленным ядром (ядро обновилось, DKMS не пересобрал);
    ``rebuild`` — модуля нет ни под одним ядром; ``unknown`` — сравнивать
    нечего (драйвера нет вовсе); ``unknown-disk`` — нет modinfo.
    """
    proc = proc or NVIDIA_PROC
    корень = корень or MODULES_ROOT
    загружен = ""
    try:
        with open(proc, encoding="utf-8", errors="replace") as файл:
            import re  # noqa: PLC0415

            # Открытый модуль (по умолчанию у Turing и новее с R560, а у
            # Blackwell — единственный) пишет «Open Kernel Module for x86_64
            # 575.57.08»: прежний шаблон его не разбирал, версия выходила
            # пустой, и разбор «поможет ли перезагрузка» молчал ровно там,
            # где он нужен.
            совпало = re.search(r"Kernel Module(?:\s+for\s+\S+)?\s+([0-9][0-9.]*)",
                                файл.read())
            загружен = совпало.group(1) if совпало else ""
    except OSError:
        загружен = ""
    библиотеки = _версия_библиотек()
    работает = platform.release()
    итог: dict[str, Any] = {
        "state": "unknown", "loaded": загружен, "userspace": библиотеки,
        "ondisk": "", "kernel": "", "running": работает,
        "dkms": dkms_состояние(), "source": dkms_исходники(),
        "branch": библиотеки.split(".")[0] if библиотеки else "",
    }
    if not загружен or not библиотеки:
        return итог

    по_ядрам = модули_по_ядрам(корень)
    # Сначала спрашиваем modinfo по имени: он отвечает тем модулем, который
    # действительно загрузит modprobe. Обход каталогов — запасной путь: в
    # RHEL модуль лежит в extra/, и по имени modinfo его не находит.
    на_диске = ""
    if shutil.which("modinfo"):
        строки = _run(["modinfo", "-F", "version", "nvidia"]).splitlines()
        на_диске = строки[0].strip() if строки else ""
    if not на_диске:
        на_диске = по_ядрам.get(работает, "")
    итог["ondisk"] = на_диске

    if _версии_сходятся(загружен, библиотеки):
        итог["state"] = "ok"
        return итог
    if на_диске and not _версии_сходятся(загружен, на_диске):
        итог["state"] = "reboot"
        return итог
    for ядро, версия in sorted(по_ядрам.items()):
        if ядро == работает:
            continue
        if _версии_сходятся(версия, библиотеки):
            итог["state"] = "other-kernel"
            итог["kernel"] = ядро
            return итог
    итог["state"] = "unknown-disk" if not shutil.which("modinfo") else "rebuild"
    return итог
