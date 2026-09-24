"""На какой файловой системе лежит каталог: локальный диск, сеть, папка ВМ.

Вопрос нужен базе. SQLite в режиме WAL держит указатель журнала в общей
памяти (`-shm`) — а общая память бывает только у процессов одной машины.
Документация SQLite говорит прямо: «все процессы, работающие с базой, должны
быть на одной машине; WAL не работает на сетевой файловой системе». Два
сервера на разных машинах над общим каталогом на NFS получают невидимые
друг другу записи и порчу базы, причём не сразу, а под нагрузкой.

Сам по себе сетевой диск ещё не беда: один сервер на NFS работает — общая
память у него своя. Беда начинается со второй машины, и тогда базе нужен
журнал отката (`db_journal_mode: delete`) и настоящие блокировки NFS.
Поэтому здесь не приговор, а сведения: тип файловой системы, признак
«сетевая» и признак «блокировки только локальные» (`nolock`,
`local_lock=all`) — при котором два сервера испортят базу и в режиме отката.

Общие папки виртуальных машин (9p в WSL2, virtiofs и grpcfuse в Docker
Desktop, vboxsf) — отдельный случай: машина одна, но блокировки и
отображение файла в память там реализованы через посредника, и SQLite на
них известна «database is locked» и порчей. Им место в предупреждении
самопроверки, а не в журнале отката.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("asrhub.fsinfo")

#: Файловые системы, к которым могут обращаться несколько машин сразу:
#: сетевые и кластерные (у ocfs2 и gfs2 общий блочный диск, а не сеть, но
#: для SQLite разницы нет — общей памяти между машинами нет и там).
СЕТЕВЫЕ = frozenset({
    "nfs", "nfs4", "cifs", "smb", "smb2", "smb3", "smbfs", "afs", "ncpfs",
    "coda", "ceph", "glusterfs", "lustre", "gpfs", "ocfs2", "gfs2", "beegfs",
    "orangefs", "pvfs2", "davfs", "webdav", "sshfs", "fuse.sshfs",
    "fuse.glusterfs", "fuse.cephfs", "fuse.ceph", "fuse.s3fs", "fuse.rclone",
    "fuse.gcsfuse", "fuse.davfs2", "fuse.juicefs", "fuse.seaweedfs",
})

#: Общие папки виртуальных машин и контейнеров: машина одна, но файловая
#: система — посредник между гостем и хозяином.
ПАПКИ_ВМ = frozenset({
    "9p", "virtiofs", "vboxsf", "vmhgfs", "fuse.vmhgfs-fuse", "prl_fs",
    "drvfs", "fakeowner", "fuse.grpcfuse", "osxfs", "fuse.osxfs",
})

#: Параметры монтирования, при которых блокировки NFS видны только своей
#: машине: SQLite на двух машинах при них не защищена ничем.
_ЛОКАЛЬНЫЕ_БЛОКИРОВКИ = re.compile(r"(?:^|,)(?:nolock|local_lock=(?:all|posix|flock))(?:,|$)")


@dataclass
class ФС:
    """Что известно о файловой системе каталога."""

    тип: str = ""
    точка: str = ""
    источник: str = ""
    параметры: str = ""
    известна: bool = False
    сетевая: bool = False
    папка_вм: bool = False
    локальные_блокировки: bool = False
    подробности: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.тип, "mount": self.точка, "source": self.источник,
                "options": self.параметры, "known": self.известна,
                "network": self.сетевая, "vm_share": self.папка_вм,
                "local_locks": self.локальные_блокировки}

    @property
    def подпись(self) -> str:
        """Коротко для человека: «ext4», «nfs4 (сетевая)», «9p (папка ВМ)»."""
        if not self.известна:
            return "не определена"
        if self.сетевая:
            return f"{self.тип} (сетевая)"
        if self.папка_вм:
            return f"{self.тип} (общая папка ВМ)"
        return self.тип


def _раскодировать(поле: str) -> str:
    """В mountinfo пробел, табуляция и обратная косая записаны восьмерично."""
    return re.sub(r"\\([0-7]{3})", lambda м: chr(int(м.group(1), 8)), поле)


def разобрать_mountinfo(текст: str) -> list[tuple[str, str, str, str]]:
    """Строки `/proc/self/mountinfo` → (точка, тип, источник, параметры).

    Формат: `ид родитель major:minor корень точка параметры [поля…] - тип
    источник суперпараметры`. Необязательных полей бывает сколько угодно,
    поэтому хвост ищется по разделителю « - », а не по номеру поля.
    Параметры — объединение параметров точки и суперблока: `nolock` у NFS
    живёт во втором наборе.
    """
    итог = []
    for строка in текст.splitlines():
        if " - " not in строка:
            continue
        голова, хвост = строка.split(" - ", 1)
        поля = голова.split()
        части = хвост.split()
        if len(поля) < 6 or len(части) < 2:
            continue
        точка = _раскодировать(поля[4])
        тип = части[0]
        источник = _раскодировать(части[1])
        параметры = поля[5] + ("," + части[2] if len(части) > 2 else "")
        итог.append((точка, тип, источник, параметры))
    return итог


def разобрать_mount_macos(текст: str) -> list[tuple[str, str, str, str]]:
    """Вывод `mount` на macOS: «источник on точка (тип, параметры…)»."""
    итог = []
    for строка in текст.splitlines():
        совпало = re.match(r"^(.*) on (.*) \(([^,)]+)(?:, ([^)]*))?\)\s*$", строка)
        if not совпало:
            continue
        источник, точка, тип, параметры = совпало.groups()
        итог.append((точка, тип.strip(), источник,
                     ",".join(п.strip() for п in (параметры or "").split(","))))
    return итог


def _лучшая_точка(путь: str, точки: list[tuple[str, str, str, str]]
                  ) -> tuple[str, str, str, str] | None:
    """Точка монтирования с самым длинным общим началом пути.

    При равной длине побеждает последняя: в mountinfo позже перечислено то,
    что смонтировано поверх.
    """
    лучшая, длина = None, -1
    for запись in точки:
        точка = запись[0].rstrip("/") or "/"
        подходит = точка == "/" or путь == точка or путь.startswith(точка + "/")
        if подходит and len(точка) >= длина:
            лучшая, длина = запись, len(точка)
    return лучшая


def _сведения(тип: str, точка: str, источник: str, параметры: str) -> ФС:
    тип = str(тип or "").strip().lower()
    сетевая = тип in СЕТЕВЫЕ or тип.startswith(("nfs", "smb", "cifs"))
    return ФС(тип=тип, точка=точка, источник=источник, параметры=параметры,
              известна=bool(тип), сетевая=сетевая,
              папка_вм=тип in ПАПКИ_ВМ and not сетевая,
              локальные_блокировки=bool(
                  сетевая and _ЛОКАЛЬНЫЕ_БЛОКИРОВКИ.search(параметры or "")))


def _windows(путь: Path) -> ФС:
    """Windows: UNC-путь или подключённый сетевой диск — сетевая ФС."""
    строка = str(путь)
    if строка.startswith(("\\\\", "//")):
        return ФС(тип="smb", точка=строка[:2], источник=строка, известна=True,
                  сетевая=True)
    try:
        import ctypes  # noqa: PLC0415

        корень = os.path.splitdrive(строка)[0] + "\\"
        вид = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(корень))  # type: ignore[attr-defined]
        буфер = ctypes.create_unicode_buffer(64)
        ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(корень), None, 0, None, None, None, буфер, 64)
        тип = (буфер.value or "").lower() or ("remote" if вид == 4 else "")
        return ФС(тип=тип, точка=корень, известна=bool(тип), сетевая=вид == 4)
    except Exception as exc:                                  # noqa: BLE001
        log.debug("Тип диска Windows не определён: %s", exc)
        return ФС()


def файловая_система(путь: Path | str, *, mountinfo: str | None = None,
                     mount_macos: str | None = None) -> ФС:
    """Сведения о файловой системе, на которой лежит путь.

    `mountinfo` и `mount_macos` — готовый текст вместо чтения системы: так
    проверяется разбор без настоящего NFS. Ничего не бросает: не удалось
    узнать — `известна=False`, и вызывающий об этом так и говорит.
    """
    цель = Path(путь)
    try:
        цель = цель.resolve()
    except OSError:
        pass
    строка = str(цель)
    if mountinfo is None and mount_macos is None and os.name == "nt":
        return _windows(цель)
    try:
        if mountinfo is not None:
            точки = разобрать_mountinfo(mountinfo)
        elif mount_macos is not None:
            точки = разобрать_mount_macos(mount_macos)
        elif sys.platform.startswith("linux") and Path("/proc/self/mountinfo").is_file():
            точки = разобрать_mountinfo(
                Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace"))
        elif sys.platform == "darwin":
            вывод = subprocess.run(["/sbin/mount"], capture_output=True, text=True,
                                   timeout=5, check=False)
            точки = разобрать_mount_macos(вывод.stdout or "")
        else:
            return ФС()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("Точки монтирования не прочитаны: %s", exc)
        return ФС()
    запись = _лучшая_точка(строка, точки)
    if запись is None:
        return ФС()
    return _сведения(запись[1], запись[0], запись[2], запись[3])


__all__ = ["ПАПКИ_ВМ", "СЕТЕВЫЕ", "ФС", "разобрать_mount_macos",
           "разобрать_mountinfo", "файловая_система"]
