#!/usr/bin/env python3
"""Склеивает главы документации в один Markdown для конвертации в Word.

Отдельные файлы удобны для чтения на GitHub, но в Word нужен один документ
со сквозной нумерацией глав. Здесь же готовится файл метаданных с титульным
листом и заголовком оглавления.

    python3 docs/assemble.py
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BUILD = ROOT / "build"

CHAPTERS: list[tuple[str, str]] = [
    ("00-README.md", "Обзор"),
    ("01-quickstart.md", "Быстрый старт"),
    ("02-installation.md", "Установка"),
    ("03-models.md", "Сравнение моделей"),
    ("04-parameters.md", "Справочник параметров"),
    ("05-web-interface.md", "Веб-интерфейс"),
    ("06-queue.md", "Очередь"),
    ("07-analytics.md", "Аналитика"),
    ("08-api.md", "Программный интерфейс"),
    ("09-clients.md", "Клиенты и удалённая работа"),
    ("10-operations.md", "Эксплуатация"),
    ("11-troubleshooting.md", "Устранение неполадок"),
    ("12-tuning.md", "Сценарии настройки"),
    ("13-presets.md", "Пресеты"),
    ("14-architecture.md", "Архитектура"),
    ("15-licenses.md", "Лицензии"),
]

METADATA = """---
title: "ASR Hub"
subtitle: "Система распознавания речи — полная документация"
author:
  - "Версия {version}"
lang: ru-RU
toc-title: "Содержание"
---
"""


def version() -> str:
    path = ROOT / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "3.0.0"


def main() -> int:
    BUILD.mkdir(exist_ok=True)
    parts: list[str] = []

    for number, (name, title) in enumerate(CHAPTERS, start=1):
        path = DOCS / name
        if not path.exists():
            print(f"  пропущено (нет файла): {name}")
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        body = "\n".join(lines).lstrip("\n")

        # Ссылки между файлами в Word не работают — оставляем название главы.
        body = re.sub(r"\[([^\]]+)\]\(\d\d-[^)]+\.md(#[^)]*)?\)", r"«\1»", body)
        # Пути к картинкам считаются от корня проекта.
        body = body.replace("](images/", "](docs/images/")

        parts.append(f"# {number}. {title}\n\n{body}")

    combined = "\n\n\\newpage\n\n".join(parts)
    target = BUILD / "asr-hub-полная-документация.md"
    target.write_text(combined, encoding="utf-8")
    (BUILD / "metadata.yaml").write_text(METADATA.format(version=version()), encoding="utf-8")

    print(f"  {target.name} — {combined.count(chr(10))} строк, {len(combined) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
