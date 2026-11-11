"""Руководство в интерфейсе: главы `docs/*.md` — в безопасный HTML.

Документация лежала только файлами в репозитории и собиралась в Word и PDF
отдельной сборкой; на сервере в закрытом контуре её не было вовсе, а
справку в интерфейсе приходилось держать отдельно и вручную. Теперь раздел
«Документация» показывает те же главы, что в репозитории, — той версии,
что установлена.

Разметка — своя, небольшая: заголовки с якорями (как у GitHub — по ним
ссылаются главы друг на друга), абзацы, списки с вложенностью, таблицы,
блоки кода, цитаты, картинки, ссылки и выделение в строке. Весь текст
экранируется: в HTML попадают только теги, которые ставит сам разбор, —
строка «<script>» из примера в главе остаётся текстом.
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import resources as ресурсы

#: Главы без номера — в конце, в этом порядке; прочие файлы не показываются.
ДОПОЛНЕНИЯ = ("api-reference.md", "appendix-phone-asr.md",
              "proposal-2026.md", "proposal-analytics.md")
#: Картинки глав — только эти расширения и только из docs/images.
КАРТИНКИ = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml", ".gif": "image/gif", ".webp": "image/webp"}


def каталог() -> Path | None:
    """Где лежит документация: ASRHUB_DOCS_DIR, docs рядом с server или
    share/asrhub/docs (установка из колеса, см. `asrhub.resources`)."""
    return ресурсы.каталог("docs", переменная="ASRHUB_DOCS_DIR", признак="[0-9][0-9]-*.md")


def якорь(текст: str) -> str:
    """Якорь заголовка, как его строит GitHub: по нему ссылаются главы."""
    текст = re.sub(r"`|\*\*|__|\*", "", текст).strip().lower()
    текст = re.sub(r"[^\w\s-]", "", текст, flags=re.UNICODE)
    return re.sub(r"\s", "-", текст)


@dataclass
class Глава:
    файл: str
    ключ: str
    номер: int | None
    заголовок: str
    слов: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.файл, "slug": self.ключ, "number": self.номер,
                "title": self.заголовок, "words": self.слов}


def главы(папка: Path | None = None) -> list[Глава]:
    """Главы по порядку: с номером — по номеру, затем дополнения."""
    папка = папка or каталог()
    if папка is None:
        return []
    итог: list[Глава] = []
    for путь in sorted(папка.glob("[0-9][0-9]-*.md")):
        итог.append(_глава(путь, int(путь.name[:2])))
    for имя in ДОПОЛНЕНИЯ:
        if (папка / имя).is_file():
            итог.append(_глава(папка / имя, None))
    return итог


def _глава(путь: Path, номер: int | None) -> Глава:
    текст = путь.read_text(encoding="utf-8", errors="replace")
    первая = next((с for с in текст.splitlines() if с.startswith("# ")), путь.stem)
    return Глава(путь.name, путь.stem, номер, первая.lstrip("# ").strip(),
                 len(текст.split()))


def найти(ключ: str, папка: Path | None = None) -> Path | None:
    """Файл главы по ключу (имя без .md) — только из списка глав."""
    папка = папка or каталог()
    if папка is None:
        return None
    for глава in главы(папка):
        if глава.ключ == ключ:
            return папка / глава.файл
    return None


# ---------------------------------------------------------------------------
# Разметка
# ---------------------------------------------------------------------------

_КОД = re.compile(r"`([^`]+)`")
_КАРТИНКА = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_ССЫЛКА = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_ЖИРНЫЙ = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_КУРСИВ = re.compile(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])")


def _адрес(цель: str) -> tuple[str, dict[str, str]]:
    """Ссылка из главы — куда её вести в интерфейсе."""
    if re.match(r"^[a-z][a-z0-9+.-]*:", цель, re.I):
        if цель.lower().startswith(("http:", "https:", "mailto:")):
            return цель, {"target": "_blank", "rel": "noopener"}
        return "#", {}                              # javascript: и прочее — никуда
    if цель.startswith("#"):
        return цель, {"data-anchor": цель[1:]}
    файл, _, хвост = цель.partition("#")
    if файл.endswith(".md"):
        ключ = Path(файл).stem
        return f"#manual/{ключ}" + (f"/{хвост}" if хвост else ""), {
            "data-doc": ключ, "data-anchor": хвост}
    if файл.startswith("images/"):
        return f"/api/manual/images/{Path(файл).name}", {"target": "_blank"}
    return "#", {}


def _строка(текст: str) -> str:
    """Разметка внутри строки: код, картинки, ссылки, выделение."""
    куски: list[str] = []

    def отложить(готовое: str) -> str:
        куски.append(готовое)
        return f"\x00{len(куски) - 1}\x00"

    текст = _КОД.sub(lambda м: отложить(f"<code>{html.escape(м.group(1))}</code>"), текст)

    def картинка(м: re.Match[str]) -> str:
        адрес = м.group(2)
        if адрес.startswith("images/"):
            адрес = f"/api/manual/images/{Path(адрес).name}"
        elif not адрес.lower().startswith(("http:", "https:")):
            return отложить(html.escape(м.group(0)))
        return отложить(f'<img src="{html.escape(адрес)}" alt="{html.escape(м.group(1))}" '
                        'loading="lazy">')

    текст = _КАРТИНКА.sub(картинка, текст)

    def ссылка(м: re.Match[str]) -> str:
        адрес, свойства = _адрес(м.group(2))
        лишнее = "".join(f' {к}="{html.escape(з)}"' for к, з in свойства.items())
        return отложить(f'<a href="{html.escape(адрес)}"{лишнее}>'
                        f"{_выделить(html.escape(м.group(1)))}</a>")

    текст = _ССЫЛКА.sub(ссылка, текст)
    текст = _выделить(html.escape(текст, quote=False))
    return re.sub(r"\x00(\d+)\x00", lambda м: куски[int(м.group(1))], текст)


def _выделить(текст: str) -> str:
    текст = _ЖИРНЫЙ.sub(lambda м: f"<strong>{м.group(1) or м.group(2)}</strong>", текст)
    return _КУРСИВ.sub(lambda м: f"<em>{м.group(1) or м.group(2)}</em>", текст)


def _ячейки(строка: str) -> list[str]:
    строка = строка.strip()
    if строка.startswith("|"):
        строка = строка[1:]
    if строка.endswith("|") and not строка.endswith("\\|"):
        строка = строка[:-1]
    # «\|» внутри ячейки — сам знак, а не граница.
    return [ч.replace("\x01", "|").strip()
            for ч in строка.replace("\\|", "\x01").split("|")]


@dataclass
class Разбор:
    """Итог разбора главы: HTML и оглавление (уровни 2–3)."""

    html: str
    заголовки: list[dict[str, Any]] = field(default_factory=list)


_ПУНКТ = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")


def в_html(текст: str) -> Разбор:
    строки = текст.replace("\r\n", "\n").split("\n")
    вывод: list[str] = []
    заголовки: list[dict[str, Any]] = []
    занятые: dict[str, int] = {}
    абзац: list[str] = []
    н = 0

    def закрыть_абзац() -> None:
        if абзац:
            вывод.append(f"<p>{_строка(' '.join(с.strip() for с in абзац))}</p>")
            абзац.clear()

    while н < len(строки):
        строка = строки[н]
        чистая = строка.strip()
        # Блок кода.
        if чистая.startswith("```"):
            закрыть_абзац()
            язык = re.sub(r"[^\w+-]", "", чистая[3:].strip())[:20]
            код: list[str] = []
            н += 1
            while н < len(строки) and not строки[н].strip().startswith("```"):
                код.append(строки[н])
                н += 1
            н += 1
            класс = f' class="lang-{язык}"' if язык else ""
            вывод.append(f"<pre><code{класс}>{html.escape(chr(10).join(код))}</code></pre>")
            continue
        if not чистая:
            закрыть_абзац()
            н += 1
            continue
        # Заголовок.
        совпало = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", строка)
        if совпало:
            закрыть_абзац()
            уровень = len(совпало.group(1))
            сырой = совпало.group(2)
            ид = якорь(сырой) or f"h{н}"
            if ид in занятые:
                занятые[ид] += 1
                ид = f"{ид}-{занятые[ид]}"
            else:
                занятые[ид] = 0
            вывод.append(f'<h{уровень} id="{html.escape(ид)}">{_строка(сырой)}</h{уровень}>')
            if уровень in (2, 3):
                заголовки.append({"level": уровень, "id": ид,
                                  "text": re.sub(r"<[^>]+>", "", html.unescape(_строка(сырой)))})
            н += 1
            continue
        # Черта.
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", чистая):
            закрыть_абзац()
            вывод.append("<hr>")
            н += 1
            continue
        # Таблица: строка с «|» и под ней разделитель.
        if "|" in чистая and н + 1 < len(строки) and re.match(
                r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", строки[н + 1]):
            закрыть_абзац()
            шапка = _ячейки(строка)
            выравнивание = []
            for ч in _ячейки(строки[н + 1]):
                ч = ч.strip()
                выравнивание.append("right" if ч.endswith(":") and not ч.startswith(":")
                                    else "center" if ч.startswith(":") and ч.endswith(":")
                                    else "")
            н += 2
            тело: list[list[str]] = []
            while н < len(строки) and "|" in строки[н] and строки[н].strip():
                тело.append(_ячейки(строки[н]))
                н += 1

            def ячейка(тег: str, текст: str, столбец: int,
                       выравнивание: list[str] = выравнивание) -> str:
                вид = выравнивание[столбец] if столбец < len(выравнивание) else ""
                стиль = f' style="text-align:{вид}"' if вид else ""
                return f"<{тег}{стиль}>{_строка(текст)}</{тег}>"

            вывод.append(
                '<div class="table-wrap"><table class="table"><thead><tr>'
                + "".join(ячейка("th", т, и) for и, т in enumerate(шапка))
                + "</tr></thead><tbody>"
                + "".join("<tr>" + "".join(ячейка("td", т, и) for и, т in enumerate(р))
                          + "</tr>" for р in тело)
                + "</tbody></table></div>")
            continue
        # Цитата.
        if чистая.startswith(">"):
            закрыть_абзац()
            внутри: list[str] = []
            while н < len(строки) and строки[н].strip().startswith(">"):
                внутри.append(re.sub(r"^\s*>\s?", "", строки[н]))
                н += 1
            вывод.append(f"<blockquote>{в_html(chr(10).join(внутри)).html}</blockquote>")
            continue
        # Список.
        if _ПУНКТ.match(строка):
            закрыть_абзац()
            блок: list[str] = []
            while н < len(строки):
                с = строки[н]
                if not с.strip():
                    # Пустая строка внутри списка: список продолжается, если
                    # дальше снова пункт или продолжение с отступом.
                    if н + 1 < len(строки) and (_ПУНКТ.match(строки[н + 1])
                                                or строки[н + 1].startswith("  ")):
                        н += 1
                        continue
                    break
                if not (_ПУНКТ.match(с) or с.startswith((" ", "\t"))):
                    break
                блок.append(с)
                н += 1
            вывод.append(_список(блок))
            continue
        абзац.append(строка)
        н += 1
    закрыть_абзац()
    return Разбор("\n".join(вывод), заголовки)


def _список(строки: list[str]) -> str:
    """Пункты с отступом — вложенный список; продолжение — к пункту."""
    пункты: list[tuple[int, str, list[str]]] = []   # (отступ, вид, строки)
    for строка in строки:
        совпало = _ПУНКТ.match(строка)
        отступ = len(строка) - len(строка.lstrip())
        if совпало and (not пункты or отступ <= пункты[0][0] + 1):
            вид = "ol" if совпало.group(2)[0].isdigit() else "ul"
            пункты.append((отступ, вид, [совпало.group(3)]))
        elif пункты:
            пункты[-1][2].append(строка)
    if not пункты:
        return ""
    # Смена вида пункта («-» → «1.») начинает новый список, как в Markdown.
    списки: list[str] = []
    начало = 0
    for и in range(1, len(пункты) + 1):
        if и == len(пункты) or пункты[и][1] != пункты[начало][1]:
            списки.append(_пункты(пункты[начало:и]))
            начало = и
    return "".join(списки)


def _пункты(пункты: list[tuple[int, str, list[str]]]) -> str:
    вид = пункты[0][1]
    части = []
    for _, _, тело in пункты:
        первая, *прочие = тело
        вложенные = [с for с in прочие if _ПУНКТ.match(с)]
        if вложенные:
            начало = next(и for и, с in enumerate(прочие) if _ПУНКТ.match(с))
            текст = " ".join([первая, *(с.strip() for с in прочие[:начало])])
            внутри = _список(прочие[начало:])
        else:
            текст = " ".join([первая, *(с.strip() for с in прочие)])
            внутри = ""
        части.append(f"<li>{_строка(текст)}{внутри}</li>")
    return f"<{вид}>{''.join(части)}</{вид}>"


# ---------------------------------------------------------------------------
# Главы целиком и поиск
# ---------------------------------------------------------------------------

@lru_cache(maxsize=64)
def _разобрать(путь: str, размер: int, время: float) -> Разбор:
    del размер, время                       # часть ключа кеша: файл обновили — разбор новый
    return в_html(Path(путь).read_text(encoding="utf-8", errors="replace"))


def глава(ключ: str, папка: Path | None = None) -> dict[str, Any] | None:
    """Глава для раздела: HTML, оглавление, соседние главы."""
    папка = папка or каталог()
    if папка is None:
        return None
    все = главы(папка)
    место = next((и for и, г in enumerate(все) if г.ключ == ключ), None)
    if место is None:
        return None
    путь = папка / все[место].файл
    сведения = путь.stat()
    разбор = _разобрать(str(путь), сведения.st_size, сведения.st_mtime)
    return {**все[место].to_dict(), "html": разбор.html, "headings": разбор.заголовки,
            "prev": все[место - 1].to_dict() if место > 0 else None,
            "next": все[место + 1].to_dict() if место + 1 < len(все) else None,
            "updated_at": сведения.st_mtime}


def искать(запрос: str, папка: Path | None = None, *, предел: int = 50) -> list[dict[str, Any]]:
    """Совпадения по всем главам: глава, ближайший заголовок, отрывок."""
    папка = папка or каталог()
    слова = [с for с in re.split(r"\s+", запрос.lower().strip()) if len(с) >= 2]
    if папка is None or not слова:
        return []
    итог: list[dict[str, Any]] = []
    for г in главы(папка):
        if г.файл == "api-reference.md" and not any(с.startswith("/api") for с in слова):
            continue                        # справочник API — только по прямому запросу
        заголовок, ид = г.заголовок, ""
        занятые: dict[str, int] = {}
        в_коде = False
        for строка in (папка / г.файл).read_text(encoding="utf-8", errors="replace").splitlines():
            if строка.strip().startswith("```"):
                в_коде = not в_коде
            совпало = None if в_коде else re.match(r"^(#{1,6})\s+(.*?)\s*#*$", строка)
            if совпало:
                заголовок = совпало.group(2).replace("`", "")
                ид = якорь(совпало.group(2))
                if ид in занятые:
                    занятые[ид] += 1
                    ид = f"{ид}-{занятые[ид]}"
                else:
                    занятые[ид] = 0
            нижняя = строка.lower()
            if all(с in нижняя for с in слова):
                место = нижняя.find(слова[0])
                начало = max(0, место - 80)
                отрывок = строка[начало:место + 160].strip()
                итог.append({"slug": г.ключ, "title": г.заголовок, "number": г.номер,
                             "heading": заголовок, "anchor": ид,
                             "snippet": ("…" if начало else "") + re.sub(r"[`*]", "", отрывок)})
                if len(итог) >= предел:
                    return итог
    return итог


def картинка(имя: str, папка: Path | None = None) -> tuple[Path, str] | None:
    """Картинка главы: только файл из docs/images с известным расширением."""
    папка = папка or каталог()
    if папка is None or Path(имя).name != имя or имя.startswith("."):
        return None
    вид = КАРТИНКИ.get(Path(имя).suffix.lower())
    путь = папка / "images" / имя
    if not вид or not путь.is_file():
        return None
    return путь, вид
