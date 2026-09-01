#!/usr/bin/env python3
"""Готовит документы Word по программному интерфейсу.

Полная документация большая, а справочник по интерфейсу нужен другим людям и
в другой обстановке: его открывают рядом с редактором, ищут в нём маршрут и
копируют пример. Поэтому он собирается отдельно.

Два варианта:

    python3 docs/assemble_api.py full         весь сервис и мониторинг
    python3 docs/assemble_api.py monitoring   только маршруты /api/monitoring/*

Второй нужен тем, кто настраивает сбор метрик и не имеет отношения к
распознаванию: дежурному, администратору Prometheus или Zabbix. Отдавать
ему документ, где девять десятых — про задания и модели, значит заставить
искать своё среди чужого.

Содержимое берётся из docs/api-reference.md и docs/17-monitoring-api.md —
оба собираются генераторами с работающего сервера.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
BUILD = ROOT / "build"


@dataclass(frozen=True)
class Variant:
    """Описание одного собираемого документа."""

    #: Имя файла Markdown в build/ и суффикс файла метаданных.
    slug: str
    #: Подзаголовок на титульном листе.
    subtitle: str
    #: Подпись в колонтитуле каждой страницы.
    footer: str
    #: Имя готового файла Word.
    docx: str
    #: Вводный текст перед содержимым.
    preamble: str
    #: Исходные файлы: имя и заголовок части.
    parts: list[tuple[str, str]] = field(default_factory=list)
    #: Нужно ли опускать заголовки на уровень. Для документа из одной части
    #: этого делать не надо: её разделы и есть главы.
    numbered_parts: bool = True
    #: Убирать ли вводные абзацы исходного файла. Они написаны для главы
    #: внутри полной документации и в отдельном документе повторяют
    #: собственное вступление слово в слово.
    strip_intro: bool = False


FULL_PREAMBLE = """
Этот документ — справочник по программному интерфейсу ASR Hub: все маршруты
сервера с описанием параметров, правами доступа и настоящими примерами
ответов.

Он собран из схемы OpenAPI работающего сервера, а примеры сняты с него же.
Это значит, что справочник не может разойтись с действительностью: если
маршрут изменился, изменится и описание при следующей сборке. Перечень
маршрутов не переписывается вручную — новый маршрут попадает сюда сам.

Документ состоит из двух частей. Первая описывает интерфейс сервиса:
задания, очередь, каталог моделей, настройки, ключи. Вторая — интерфейс
подсистемы мониторинга: метрики, пробы состояния, тревоги, приёмники
телеметрии. Разбор того, что означает каждая метрика и какие пороги
выставлять, — в отдельном руководстве по мониторингу; здесь только
интерфейс.

Полная документация по системе — «ASR Hub — документация»; там установка,
сравнение моделей, справочник параметров и эксплуатация.
"""

MONITORING_PREAMBLE = """
Этот документ — справочник по маршрутам `/api/monitoring/*`: всё, что нужно,
чтобы забрать из ASR Hub метрики и состояние службы и завести их в свою
систему наблюдения.

Он рассчитан на того, кто настраивает сбор, а не на того, кто распознаёт
речь. Поэтому здесь нет ни заданий, ни моделей, ни очереди: только интерфейс
телеметрии. Полный справочник по всем маршрутам сервера — «ASR Hub —
программный интерфейс»; разбор того, что означает каждая метрика, какие
пороги выставлять и как читать отклонения, — глава «Мониторинг» в основной
документации.

Справочник собран из схемы OpenAPI работающего сервера, а примеры ответов
сняты с него же. Это значит, что расходиться с действительностью ему негде:
если маршрут изменился, изменится и описание при следующей сборке.

**С чего начать.** Если нужен обычный сбор в Prometheus, хватит одного
маршрута — `GET /api/monitoring/metrics` — и готового фрагмента настройки,
который сервер отдаёт сам по `GET /api/monitoring/config/prometheus-scrape`.
Правила оповещения, панель Grafana и шаблон Zabbix тоже собираются сервером
по каталогу метрик, а не пишутся руками: см. раздел «Готовые конфигурации».
Если сервер стоит в закрытом контуре и снаружи до него не достучаться,
смотрите раздел «Приёмники метрик» — тогда сервер отправляет метрики сам.
"""

VARIANTS: dict[str, Variant] = {
    "full": Variant(
        slug="asr-hub-api",
        subtitle="Программный интерфейс — справочник",
        footer="ASR Hub — программный интерфейс",
        docx="ASR Hub — программный интерфейс.docx",
        preamble=FULL_PREAMBLE,
        parts=[
            ("api-reference.md", "Программный интерфейс сервиса"),
            ("17-monitoring-api.md", "Программный интерфейс мониторинга"),
        ],
    ),
    "monitoring": Variant(
        slug="asr-hub-monitoring-api",
        subtitle="Программный интерфейс мониторинга — инструкция",
        footer="ASR Hub — программный интерфейс мониторинга",
        docx="ASR Hub — программный интерфейс мониторинга.docx",
        preamble=MONITORING_PREAMBLE,
        parts=[("17-monitoring-api.md", "")],
        numbered_parts=False,
        strip_intro=True,
    ),
}

METADATA = """---
title: "ASR Hub"
subtitle: "{subtitle}"
author:
  - "Версия {version}"
lang: ru-RU
toc-title: "Содержание"
---
"""


def version() -> str:
    path = ROOT / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "3.0.0"


def shift_headings(body: str) -> str:
    """Опускает все заголовки на уровень: части документа становятся главами.

    Внутри блоков кода решётка — это комментарий оболочки, а не заголовок,
    поэтому содержимое ``` … ``` не трогаем.
    """
    out: list[str] = []
    in_code = False
    for line in body.split("\n"):
        if line.lstrip().startswith("```"):
            in_code = not in_code
            out.append(line)
            continue
        if not in_code and re.match(r"^#{1,5} ", line):
            out.append("#" + line)
        else:
            out.append(line)
    return "\n".join(out)


def drop_intro(body: str) -> str:
    """Убирает вводные абзацы файла, оставляя счётчик маршрутов.

    Генератор пишет вступление под главу внутри полной документации. В
    отдельном документе оно идёт сразу за собственным вступлением и
    повторяет его — читатель дважды подряд узнаёт, что справочник собран из
    схемы OpenAPI. А вот строка «Всего маршрутов…» полезна и остаётся.
    """
    match = re.search(r"^## ", body, flags=re.M)
    if not match:
        return body
    intro, rest = body[:match.start()], body[match.start():]
    keep = [line for line in intro.split("\n") if line.startswith("Всего маршрутов")]
    return ("\n".join(keep) + "\n\n" + rest) if keep else rest


def clean(body: str) -> str:
    """Убирает то, что в Word не работает: ссылки между файлами документации."""
    body = re.sub(r"\[([^\]]+)\]\(\d\d-[^)]+\.md(#[^)]*)?\)", r"«\1»", body)
    body = re.sub(r"\[([^\]]+)\]\(api-reference\.md(#[^)]*)?\)", r"«\1»", body)
    return body.replace("](images/", "](docs/images/")


def main(argv: list[str]) -> int:
    name = argv[1] if len(argv) > 1 else "full"
    variant = VARIANTS.get(name)
    if variant is None:
        print(f"Неизвестный вариант «{name}». Доступны: {', '.join(VARIANTS)}")
        return 2

    BUILD.mkdir(exist_ok=True)
    missing = [f for f, _ in variant.parts if not (DOCS / f).exists()]
    if missing:
        print(f"  нет файлов: {', '.join(missing)}")
        print("  соберите их: python3 docs/generate_api_full.py и generate_api.py")
        return 1

    parts: list[str] = [variant.preamble]
    for number, (file_name, title) in enumerate(variant.parts, start=1):
        lines = (DOCS / file_name).read_text(encoding="utf-8").split("\n")
        if lines and lines[0].startswith("# "):
            lines = lines[1:]                 # заголовок файла заменяет титульный лист
        body = clean("\n".join(lines).lstrip("\n"))
        if variant.strip_intro:
            body = drop_intro(body)

        if variant.numbered_parts:
            parts.append(f"# {number}. {title}\n\n{shift_headings(body)}")
        else:
            parts.append(body)

    combined = "\n\n\\newpage\n\n".join(parts)
    target = BUILD / f"{variant.slug}.md"
    target.write_text(combined, encoding="utf-8")
    (BUILD / f"metadata-{variant.slug}.yaml").write_text(
        METADATA.format(subtitle=variant.subtitle, version=version()), encoding="utf-8")

    print(f"  {target.name} — {combined.count(chr(10))} строк, "
          f"{len(combined) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
