"""Проверка окружения из состава сервера: python -m asrhub --check.

Дублирует часть проверок scripts/doctor.sh, но работает изнутри
установленного окружения и потому видит фактически доступные движки.
"""
from __future__ import annotations

from pathlib import Path

from . import catalog
from .config import Settings
from .hardware import detect, recommended_settings

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"

_passed = 0
_warned = 0
_failed = 0


def _check(name: str, status: str, detail: str = "", hint: str = "") -> None:
    global _passed, _warned, _failed
    mark, colour = {"ok": ("✓", GREEN), "warn": ("!", YELLOW), "fail": ("✕", RED)}[status]
    if status == "ok":
        _passed += 1
    elif status == "warn":
        _warned += 1
    else:
        _failed += 1
    print(f"  {colour}{mark}{RESET} {name:<38} {detail}")
    if hint and status != "ok":
        print(f"      {DIM}{hint}{RESET}")


def _heading(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(f"{DIM}{'─' * len(text)}{RESET}")


def run_checks(settings: Settings) -> bool:
    """Выполняет все проверки. Возвращает True, если критических ошибок нет."""
    print(f"\n{BOLD}Проверка окружения ASR Hub{RESET}")

    _heading("Оборудование")
    hardware = detect(str(settings.paths.data))
    _check("Операционная система", "ok", f"{hardware.os_name} {hardware.os_version} ({hardware.arch})")
    _check("Python", "ok", hardware.python_version)
    _check("Ядер процессора", "ok" if hardware.cpu_cores_physical >= 4 else "warn",
           str(hardware.cpu_cores_physical),
           "Меньше четырёх ядер: обработка на процессоре будет медленной.")
    _check("Оперативная память",
           "ok" if hardware.ram_total_gb >= 16 else ("warn" if hardware.ram_total_gb >= 6 else "fail"),
           f"{hardware.ram_total_gb} ГБ",
           "Для моделей уровня large рекомендуется 16 ГБ.")
    _check("Свободно на диске",
           "ok" if hardware.disk_free_gb >= 20 else ("warn" if hardware.disk_free_gb >= 5 else "fail"),
           f"{hardware.disk_free_gb} ГБ", "Полный набор моделей занимает свыше 100 ГБ.")

    if hardware.gpus:
        for gpu in hardware.gpus:
            _check(f"Видеокарта {gpu.index}", "ok",
                   f"{gpu.name} — {gpu.memory_total_mb} МБ")
        if hardware.cuda_version:
            _check("CUDA", "ok", hardware.cuda_version)
        if hardware.cudnn_version:
            _check("cuDNN", "ok", hardware.cudnn_version)
    else:
        _check("Видеокарта", "warn" if hardware.accelerator == "cpu" else "ok",
               hardware.accelerator,
               "Работа только на процессоре — включите int8 и берите модели полегче.")

    _check("ffmpeg", "ok" if hardware.ffmpeg else "fail",
           hardware.ffmpeg_version or "не найден",
           "Debian/Ubuntu: apt install ffmpeg · macOS: brew install ffmpeg · "
           "Windows: winget install Gyan.FFmpeg")
    _check("PyTorch", "ok" if hardware.torch_version else "warn",
           hardware.torch_version or "не установлен",
           "Нужен большинству движков. Установите через scripts/install.sh.")

    for warning in hardware.warnings:
        print(f"      {YELLOW}{warning}{RESET}")

    _heading("Каталоги и права")
    paths = settings.paths
    for label, path in (("данные", paths.data), ("загрузки", paths.uploads),
                        ("результаты", paths.results), ("модели", paths.models),
                        ("журналы", paths.logs), ("временные", paths.tmp)):
        writable = path.exists() and _writable(path)
        _check(f"Каталог {label}", "ok" if writable else "fail", str(path),
               f"Создайте и дайте права: mkdir -p '{path}' && chmod 750 '{path}'")

    _heading("Конфигурация")
    _check("Файл конфигурации", "ok" if settings.config_file else "warn",
           str(settings.config_file) if settings.config_file else "не используется",
           "Значения по умолчанию подобраны автоматически. "
           "Создать файл: python -m asrhub --print-config > config.yaml")
    _check("Аутентификация", "ok" if settings.get("auth_enabled") else "warn",
           "включена" if settings.get("auth_enabled") else "ОТКЛЮЧЕНА",
           "На сервере, доступном по сети, аутентификация обязательна.")
    model_id = str(settings.get("model") or "")
    spec = catalog.get_model(model_id)
    _check("Модель по умолчанию", "ok" if spec else "fail", model_id,
           "Список моделей: python -m asrhub --print-config | grep 'model:'")

    _heading("Движки распознавания")
    from .engines import engine_status

    for item in engine_status():
        _check(item["id"], "ok" if item["available"] else "warn",
               item["name"] if item["available"] else item["reason"][:60],
               f"Установить: bash scripts/models.sh install-engine {item['id']}")

    _heading("Рекомендации для вашего оборудования")
    recommended = recommended_settings(hardware)
    print(f"  {DIM}{recommended.pop('_reason', '')}{RESET}")
    for key, value in recommended.items():
        param = catalog.get_param(key)
        current = settings.get(key)
        same = str(current) == str(value)
        mark = f"{GREEN}=" if same else f"{YELLOW}→"
        print(f"  {mark}{RESET} {(param.label if param else key):<38} "
              f"{value}" + ("" if same else f"   {DIM}(сейчас {current}){RESET}"))

    print()
    _heading("Итог")
    print(f"  {GREEN}✓ пройдено: {_passed}{RESET}   "
          f"{YELLOW}! предупреждений: {_warned}{RESET}   "
          f"{RED}✕ ошибок: {_failed}{RESET}\n")
    if _failed:
        print(f"  {RED}Есть критические проблемы — сервер может не работать.{RESET}\n")
        return False
    if _warned:
        print(f"  {YELLOW}Сервер работоспособен, часть возможностей ограничена.{RESET}\n")
    else:
        print(f"  {GREEN}Всё в порядке.{RESET}\n")
    return True


def _writable(path: Path) -> bool:
    try:
        probe = path / ".write-test"
        probe.touch()
        probe.unlink()
        return True
    except OSError:
        return False
