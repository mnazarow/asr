"""Файл требований с хешами по каталогу скачанных колёс.

    python3 scripts/lib/wheel_hashes.py КАТАЛОГ > КАТАЛОГ/requirements-hashes.txt

Каталог — то, что скачал `pip download` (`scripts/wheels.sh` делает это сам).
Каждый пакет — строкой `имя==версия` и хешами SHA-256 всех его файлов в
каталоге. Такой файл ставится только с `--require-hashes`: pip отказывается
от любого файла, хеш которого не совпал, — подменённое колесо на флешке или
в зеркале не встанет. Колёса зависят от платформы и версии Python, поэтому
файл собирается на машине того же вида, что и та, куда ставим.

Только стандартная библиотека: скрипт работает и до установки чего-либо.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

#: Колесо: имя-версия(-сборка)?-python-abi-платформа.whl (PEP 427).
_КОЛЕСО = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)(-\d[^-]*)?-[^-]+-[^-]+-[^-]+\.whl$")
#: Исходники: имя-версия.tar.gz / .zip; в имени бывают дефисы.
_ИСХОДНИКИ = re.compile(r"^(?P<name>.+)-(?P<version>\d[^-]*)\.(tar\.gz|zip|tar\.bz2)$")


def имя_пакета(имя: str) -> str:
    """Нормализованное имя (PEP 503): «Foo_Bar.baz» → «foo-bar-baz»."""
    return re.sub(r"[-_.]+", "-", имя).lower()


def разобрать(файл: str) -> tuple[str, str] | None:
    for шаблон in (_КОЛЕСО, _ИСХОДНИКИ):
        совпало = шаблон.match(файл)
        if совпало:
            return имя_пакета(совпало["name"]), совпало["version"]
    return None


def хеш(путь: Path) -> str:
    сумма = hashlib.sha256()
    with путь.open("rb") as файл:
        for кусок in iter(lambda: файл.read(1 << 20), b""):
            сумма.update(кусок)
    return сумма.hexdigest()


def собрать(каталог: Path) -> tuple[str, list[str]]:
    """Текст файла требований и список файлов, которые не удалось разобрать."""
    пакеты: dict[tuple[str, str], list[str]] = {}
    непонятные: list[str] = []
    for путь in sorted(каталог.iterdir()):
        if not путь.is_file() or путь.name.startswith(".") or путь.suffix in (".txt", ".sha256"):
            continue
        разобрано = разобрать(путь.name)
        if разобрано is None:
            непонятные.append(путь.name)
            continue
        пакеты.setdefault(разобрано, []).append(хеш(путь))
    версии: dict[str, set[str]] = {}
    for имя, версия in пакеты:
        версии.setdefault(имя, set()).add(версия)
    двойные = sorted(и for и, в in версии.items() if len(в) > 1)
    if двойные:
        raise SystemExit("В каталоге несколько версий одного пакета: " + ", ".join(двойные)
                         + ". Оставьте одну (скачайте в пустой каталог заново).")
    строки = ["# Создано scripts/lib/wheel_hashes.py по каталогу колёс.",
              "# Ставить: pip install --require-hashes --no-index --find-links КАТАЛОГ -r этот_файл"]
    for (имя, версия), хеши in sorted(пакеты.items()):
        строки.append(f"{имя}=={версия} \\")
        строки.extend(f"    --hash=sha256:{х}" + (" \\" if н < len(хеши) - 1 else "")
                      for н, х in enumerate(sorted(set(хеши))))
    return "\n".join(строки) + "\n", непонятные


def main(аргументы: list[str]) -> int:
    if len(аргументы) != 1 or аргументы[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if аргументы and аргументы[0] in ("-h", "--help") else 2
    каталог = Path(аргументы[0])
    if not каталог.is_dir():
        print(f"Каталог не найден: {каталог}", file=sys.stderr)
        return 2
    текст, непонятные = собрать(каталог)
    for имя in непонятные:
        print(f"Пропущен файл, не похожий на пакет: {имя}", file=sys.stderr)
    if текст.count("==") == 0:
        print(f"В {каталог} нет колёс: сначала pip download -d {каталог} …", file=sys.stderr)
        return 1
    sys.stdout.write(текст)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
