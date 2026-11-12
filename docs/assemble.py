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
    ("16-monitoring.md", "Мониторинг"),
    ("17-monitoring-api.md", "Программный интерфейс мониторинга"),
    ("19-content-analytics.md", "Аналитика записей"),
    ("20-trends.md", "Тренды"),
    ("21-telephony.md", "Телефония: разговоры с АТС Asterisk"),
    ("22-employees.md", "Аналитика по сотрудникам"),
    ("23-backup.md", "Резервные копии и восстановление"),
    ("24-agent.md", "Агент на станции Asterisk"),
    ("25-dashboard.md", "Дашборд и автодиагностика"),
    ("26-voice-analytics.md", "Голосовая аналитика и очередь к модели"),
    ("27-employees-directory.md", "Справочник сотрудников"),
    ("28-market.md", "Рынок речевой аналитики: что умеют другие"),
    ("29-access.md", "Доступ, журнал и персональные данные в звуке"),
    ("30-integrations.md", "Интеграции и каналы"),
    ("31-asterisk-dialplan.md", "Приём звонков из диалплана Asterisk"),
    ("32-quality-control.md", "Контроль качества: обратный звонок с опросом"),
    ("33-roadmap.md", "Что доделать и улучшить"),
    ("34-development.md", "Разработка: репозиторий, проверки и выпуск"),
    ("35-ai-analytics.md", "ИИ-аналитика: что есть и как этим пользоваться"),
    ("36-limitations.md", "Известные ограничения"),
    ("18-review.md", "Ревизия кода"),
    ("appendix-phone-asr.md", "Приложение. Разбор проектов phone_asr"),
    # Предложения и полный справочник API — приложениями, а не отдельными
    # файлами, которых в выданном документе нет: ссылки на них из глав
    # превращаются в названия, и читатель должен найти их здесь же.
    ("proposal-analytics.md", "Приложение. Мировой опыт аналитики разговоров"),
    ("proposal-2026.md", "Приложение. Что доделать в аналитике разговоров (сентябрь 2026)"),
    ("api-reference.md", "Приложение. Полный справочник программного интерфейса"),
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

    for name, title in CHAPTERS:
        path = DOCS / name
        if not path.exists():
            print(f"  пропущено (нет файла): {name}")
            continue
        lines = path.read_text(encoding="utf-8").split("\n")
        if lines and lines[0].startswith("# "):
            lines = lines[1:]
        body = "\n".join(lines).lstrip("\n")

        # Ссылки между файлами в Word не работают — оставляем название главы.
        # Любой файл .md, а не только «NN-…»: ссылки на приложение, справочник
        # API и предложения оставались в Word ссылками на несуществующие файлы.
        body = re.sub(r"\[([^\]]+)\]\((?![a-z]+://)[^)\s]+\.md(#[^)]*)?\)", r"«\1»", body)
        # Пути к картинкам считаются от корня проекта.
        body = body.replace("](images/", "](docs/images/")

        # Номер главы — из имени файла («05-…» → 5), как в ссылках внутри
        # текста («5. Веб-интерфейс»). Раньше главы считались по порядку с
        # единицы: оглавление Word расходилось со ссылками на единицу, а
        # главы вне ряда (ревизия, приложение) получали чужие номера.
        совпало = re.match(r"(\d+)-", name)
        заголовок = f"{int(совпало.group(1))}. {title}" if совпало else title
        parts.append(f"# {заголовок}\n\n{body}")

    combined = "\n\n\\newpage\n\n".join(parts)
    target = BUILD / "asr-hub-полная-документация.md"
    target.write_text(combined, encoding="utf-8")
    (BUILD / "metadata.yaml").write_text(METADATA.format(version=version()), encoding="utf-8")

    print(f"  {target.name} — {combined.count(chr(10))} строк, {len(combined) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
