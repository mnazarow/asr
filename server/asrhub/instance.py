"""Кто мы такие среди серверов, работающих на общей базе.

Вынесено в отдельный модуль ради одной причины: значение нужно и очереди, и
обслуживанию по расписанию, а обслуживание не должно тянуть за собой всю
очередь — с движками, конвейером и потоками — только чтобы узнать имя
машины.
"""
from __future__ import annotations

import os
import socket
import sys

#: Имя машины.
HOSTNAME = socket.gethostname()

#: Имя машины плюс идентификатор процесса: достаточно, чтобы отличить два
#: экземпляра, и понятно человеку, который смотрит в базу и хочет знать, на
#: какой машине висит задание.
INSTANCE_ID = f"{HOSTNAME}:{os.getpid()}"


def _ядро() -> str:
    """Отпечаток загрузки ядра — общий для всех контейнеров одной машины.

    Имя машины у каждого контейнера своё, и два контейнера на одной машине
    над общим томом выглядели бы «разными машинами». Для базы важно другое:
    общая ли у процессов память, а она общая у всех, кто работает на одном
    ядре. `boot_id` одинаков во всех контейнерах одной машины и различается у
    разных. На macOS и Windows его нет — там решает имя машины.
    """
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as файл:
            return файл.read().strip()
    except OSError:
        return ""


#: Отпечаток ядра этой машины ("" — неизвестен).
KERNEL_ID = _ядро()


def other_machine(отметка: dict) -> bool:
    """Работает ли экземпляр из отметки на другой машине, чем этот.

    По отпечатку ядра, если он есть у обоих, иначе — по имени машины.
    """
    чужое_ядро = str(отметка.get("kernel") or "")
    if KERNEL_ID and чужое_ядро:
        return чужое_ядро != KERNEL_ID
    машина = str(отметка.get("host") or str(отметка.get("instance") or "").rsplit(":", 1)[0])
    return bool(машина) and машина != HOSTNAME


def own_host(instance_id: str) -> bool:
    """Наша ли это машина."""
    значение = str(instance_id or "")
    return bool(значение) and значение.rsplit(":", 1)[0] == HOSTNAME


def process_alive(instance_id: str) -> bool:
    """Жив ли процесс, которому принадлежит отметка.

    Спрашивается только про свою машину: идентификатор процесса с чужого
    хоста здесь ничего не значит. Ответ «жив» — осторожный: если разобрать
    отметку не вышло или прав не хватило, считаем, что процесс работает, и
    его задания не трогаем.
    """
    значение = str(instance_id or "")
    if not own_host(значение) or ":" not in значение:
        return True
    хвост = значение.rsplit(":", 1)[1]
    if not хвост.isdigit():
        return True
    pid = int(хвост)
    if pid <= 0:
        return True
    if sys.platform == "win32":
        return _жив_в_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


#: Код завершения «ещё работает» у GetExitCodeProcess.
_STILL_ACTIVE = 259


def _жив_в_windows(pid: int) -> bool:
    """Жив ли процесс в Windows — не сигналом, а запросом к ядру.

    `os.kill(pid, 0)` в Windows не проверяет, жив ли процесс: сигнал 0 там —
    это CTRL_C_EVENT. Для несуществующего процесса выходит `OSError`, а не
    `ProcessLookupError`, и ответ всегда был «жив»: ускорение «процесс этой
    машины мёртв» не срабатывало никогда (пять минут ожидания, отложенное
    восстановление базы), а если номер достался другому процессу — ему
    отправлялось событие Ctrl+C. Здесь — OpenProcess с правом только на
    сведения и код завершения. Не проверено запуском: Windows на стенде нет.
    """
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        ядро = ctypes.WinDLL("kernel32", use_last_error=True)       # type: ignore[attr-defined]
        ядро.OpenProcess.restype = wintypes.HANDLE
        ядро.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        ядро.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        ядро.CloseHandle.argtypes = (wintypes.HANDLE,)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        описатель = ядро.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not описатель:
            # Нет такого процесса — «неверный параметр»; прочее (нет прав)
            # — осторожный ответ «жив».
            return ctypes.get_last_error() != ERROR_INVALID_PARAMETER  # type: ignore[attr-defined]
        try:
            код = wintypes.DWORD()
            if not ядро.GetExitCodeProcess(описатель, ctypes.byref(код)):
                return True
            return код.value == _STILL_ACTIVE
        finally:
            ядро.CloseHandle(описатель)
    except Exception:                                        # noqa: BLE001
        return True
