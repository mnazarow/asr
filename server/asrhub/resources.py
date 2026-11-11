"""Файлы установки вне пакета: агент станции и руководство.

В клоне и при установке установщиком (install.sh, install.ps1, образ Docker)
они лежат рядом с каталогом server: agent/ и docs/. Колесо
(`pip install asrhub-….whl`) кладёт их в `<prefix>/share/asrhub/` — см.
`data-files` в pyproject.toml. Раньше в колесо они не попадали вовсе, и
сервер, поставленный так, на «скачать агента» отвечал «каталог agent/
отсутствует», а раздел «Документация» был пуст.
"""
from __future__ import annotations

import os
import site
import sys
import sysconfig
from pathlib import Path

#: Корень дерева программы (каталог над server/).
КОРЕНЬ = Path(__file__).resolve().parents[2]


def _общие() -> list[Path]:
    """Где колесо оставляет файлы данных: share/asrhub под префиксом."""
    префиксы = [sys.prefix, sysconfig.get_path("data") or "",
                getattr(sys, "base_prefix", "") or ""]
    try:
        префиксы.append(site.getuserbase())
    except Exception:  # noqa: BLE001 — урезанный site (встроенный Python)
        pass
    места: list[Path] = []
    for префикс in префиксы:
        if префикс:
            место = Path(префикс) / "share" / "asrhub"
            if место not in места:
                места.append(место)
    return места


def кандидаты(имя: str, переменная: str = "") -> list[Path]:
    """Где искать каталог `имя` (agent, docs) — по порядку."""
    своё = os.environ.get(переменная, "").strip() if переменная else ""
    места = [Path(своё)] if своё else []
    места.append(КОРЕНЬ / имя)
    места += [общее / имя for общее in _общие()]
    return места


def каталог(имя: str, *, переменная: str = "", признак: str = "*") -> Path | None:
    """Первый существующий каталог `имя`, где есть файлы по образцу `признак`."""
    for путь in кандидаты(имя, переменная):
        try:
            if путь.is_dir() and any(путь.glob(признак)):
                return путь
        except OSError:
            continue
    return None


def файл(каталог_: str, имя: str, *, переменная: str = "") -> Path | None:
    """Файл `имя` в первом каталоге `каталог_`, где он есть."""
    for путь in кандидаты(каталог_, переменная):
        if (путь / имя).is_file():
            return путь / имя
    return None
