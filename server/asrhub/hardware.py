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


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return (out.stdout or "").strip()
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
