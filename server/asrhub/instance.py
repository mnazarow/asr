"""Кто мы такие среди серверов, работающих на общей базе.

Вынесено в отдельный модуль ради одной причины: значение нужно и очереди, и
обслуживанию по расписанию, а обслуживание не должно тянуть за собой всю
очередь — с движками, конвейером и потоками — только чтобы узнать имя
машины.
"""
from __future__ import annotations

import os
import socket

#: Имя машины.
HOSTNAME = socket.gethostname()

#: Имя машины плюс идентификатор процесса: достаточно, чтобы отличить два
#: экземпляра, и понятно человеку, который смотрит в базу и хочет знать, на
#: какой машине висит задание.
INSTANCE_ID = f"{HOSTNAME}:{os.getpid()}"


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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True
