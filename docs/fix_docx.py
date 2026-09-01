#!/usr/bin/env python3
"""Приводит .docx к порядку элементов, который требует схема OOXML.

И pandoc, и стандартный reference.docx кое-где расставляют дочерние элементы
в произвольном порядке. Word такие файлы обычно открывает, но проверка по
схеме их отвергает, а часть программ для чтения .docx — тоже. Скрипт
переставляет элементы по порядку из самой схемы, ничего не добавляя и не
удаляя, кроме заведомого мусора (дублирующийся pStyle, «хвост» текста внутри
element-only узлов).

    python3 docs/fix_docx.py файл.docx
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
XS = "{http://www.w3.org/2001/XMLSchema}"
SCHEMA = Path("/mnt/skills/public/docx/scripts/office/schemas/"
              "ISO-IEC29500-4_2016/wml.xsd")

# Тег -> имя типа в схеме. Дополнения после списка схемы — элементы,
# объявленные в наследнике типа (например, CT_PPr расширяет CT_PPrBase).
TYPES: dict[str, tuple[str, tuple[str, ...]]] = {
    "pPr":      ("CT_PPrBase", ("rPr", "sectPr", "pPrChange")),
    "rPr":      ("CT_RPrBase", ("rStyle", "rPrChange")),
    "style":    ("CT_Style", ()),
    "settings": ("CT_Settings", ()),
    "tcPr":     ("CT_TcPrBase", ("tcPrChange",)),
    "trPr":     ("CT_TrPrBase", ("ins", "del", "trPrChange")),
    "tblPr":    ("CT_TblPrBase", ("tblPrChange",)),
    "sectPr":   ("CT_SectPrBase", ("headerReference", "footerReference",
                                   "footnotePr", "endnotePr", "sectPrChange")),
}


def schema_orders() -> dict[str, list[str]]:
    root = etree.parse(str(SCHEMA)).getroot()
    orders: dict[str, list[str]] = {}
    for tag, (type_name, extra) in TYPES.items():
        node = root.find(f'{XS}complexType[@name="{type_name}"]')
        if node is None:
            continue
        names = [e.get("name") for e in node.iter(f"{XS}element") if e.get("name")]
        # Ссылки на заголовки и колонтитулы идут перед остальным содержимым sectPr.
        orders[tag] = (list(extra[:2]) if tag == "sectPr" else []) + names + [
            e for e in extra if tag != "sectPr" or e not in extra[:2]]
    return orders


def reorder(parent: etree._Element, order: list[str]) -> int:
    known = [c for c in parent
             if isinstance(c.tag, str) and c.tag.startswith(W) and c.tag[len(W):] in order]
    if len(known) < 2:
        return 0
    names = [c.tag[len(W):] for c in known]
    if names == sorted(names, key=order.index):
        return 0
    # Элементы возвращаются на те же позиции — неизвестные схеме узлы
    # (например, из пространства имён math) остаются на своих местах.
    positions = sorted(list(parent).index(c) for c in known)
    for child in known:
        parent.remove(child)
    for pos, child in zip(positions, sorted(known, key=lambda c: order.index(c.tag[len(W):]))):
        parent.insert(pos, child)
    return 1


BORDER = "BFC7D1"
BORDER_INNER = "D8DEE6"
HEADER_FILL = "EDF2F7"


def sub(parent: etree._Element, tag: str, **attrs: str) -> etree._Element:
    node = etree.SubElement(parent, W + tag)
    for key, value in attrs.items():
        node.set(W + key, value)
    return node


def content_width(root: etree._Element) -> int:
    """Ширина текстовой колонки в твипах — из настроек страницы документа."""
    for sect in root.iter(W + "sectPr"):
        size = sect.find(W + "pgSz")
        mar = sect.find(W + "pgMar")
        if size is None or mar is None:
            continue
        try:
            return (int(size.get(W + "w")) - int(mar.get(W + "left"))
                    - int(mar.get(W + "right")))
        except (TypeError, ValueError):
            continue
    return 9638          # A4 с полями 2 см


WIDE_TABLE_COLUMNS = 8          # с этого числа колонок таблица уходит в альбомную полосу


def _sect_pr(landscape: bool) -> etree._Element:
    """Свойства секции: A4 книжной или альбомной ориентации, поля 2 см."""
    sect = etree.Element(W + "sectPr")
    size = etree.SubElement(sect, W + "pgSz")
    if landscape:
        size.set(W + "w", "16838"); size.set(W + "h", "11906")
        size.set(W + "orient", "landscape")
    else:
        size.set(W + "w", "11906"); size.set(W + "h", "16838")
    mar = etree.SubElement(sect, W + "pgMar")
    for side in ("top", "right", "bottom", "left"):
        mar.set(W + side, "1134")
    mar.set(W + "header", "708"); mar.set(W + "footer", "567"); mar.set(W + "gutter", "0")
    return sect


def _sect_paragraph(landscape: bool, footer_id: str | None) -> etree._Element:
    para = etree.Element(W + "p")
    ppr = etree.SubElement(para, W + "pPr")
    spacing = etree.SubElement(ppr, W + "spacing")
    spacing.set(W + "before", "0"); spacing.set(W + "after", "0")
    sect = _sect_pr(landscape)
    if footer_id:
        ref = etree.Element(W + "footerReference")
        ref.set(W + "type", "default")
        ref.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",
                footer_id)
        sect.insert(0, ref)
    ppr.append(sect)
    return para


def is_wide(tbl: etree._Element) -> bool:
    grid = tbl.find(W + "tblGrid")
    return grid is not None and len(grid.findall(W + "gridCol")) >= WIDE_TABLE_COLUMNS


def landscape_wide_tables(root: etree._Element) -> int:
    """Разворачивает широкие таблицы на альбомную страницу.

    Таблица на шестнадцать колонок в книжной A4 нечитаема: слова переносятся
    по одной букве. Такие таблицы выносим в собственную секцию с альбомной
    ориентацией, а следом возвращаем книжную.
    """
    body = root.find(W + "body")
    if body is None:
        return 0

    footer_id = None
    for sect in body.iter(W + "sectPr"):
        ref = sect.find(W + "footerReference")
        if ref is not None:
            footer_id = ref.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
            break

    changed = 0
    for tbl in list(body.findall(W + "tbl")):
        if not is_wide(tbl):
            continue
        index = list(body).index(tbl)
        body.insert(index, _sect_paragraph(False, footer_id))       # закрываем книжную
        body.insert(index + 2, _sect_paragraph(True, footer_id))    # закрываем альбомную
        changed += 1
    return changed


def column_weights(tbl: etree._Element, count: int, target: int) -> list[int] | None:
    """Раздаёт ширину колонок по содержимому — в твипах, а не в долях.

    Широкие таблицы pandoc делит поровну, и колонка с идентификатором модели
    получает столько же, сколько колонка с «да». Считаем для каждой колонки
    два числа: сколько нужно, чтобы самое длинное слово не разорвалось, и
    сколько хотелось бы под типичное содержимое. Первое выдаём обязательно,
    остаток делим пропорционально второму.
    """
    if count < 2:
        return None

    CHAR = 88           # средняя ширина знака при кегле 7 пт, твипы
    MARGINS = 120       # поля ячейки слева и справа

    longest = [1] * count
    typical = [1] * count
    for row in tbl.findall(W + "tr"):
        for index, cell in enumerate(row.findall(W + "tc")):
            if index >= count:
                break
            text = " ".join("".join(cell.itertext()).split())
            typical[index] = max(typical[index], min(len(text), 26))
            for word in text.split():
                longest[index] = max(longest[index], min(len(word), 22))

    minimum = [word * CHAR + MARGINS for word in longest]
    wanted = [max(chars * CHAR + MARGINS, low) for chars, low in zip(typical, minimum)]

    base = sum(minimum)
    if base >= target:      # даже минимума не хватает — ужимаем пропорционально
        return [max(300, round(value * target / base)) for value in minimum]

    spare = target - base
    demand = sum(w - m for w, m in zip(wanted, minimum)) or 1
    widths = [m + round((w - m) * spare / demand) for m, w in zip(minimum, wanted)]

    # Остаток от округления отдаём первой колонке — она всегда самая длинная.
    widths[0] += target - sum(widths)
    return widths


def style_tables(root: etree._Element) -> int:
    """Проставляет таблицам явные рамки, ширину в твипах и повтор шапки.

    Стиль таблицы в styles.xml понимают не все программы для чтения .docx —
    LibreOffice, например, игнорирует его рамки. Прямое оформление работает
    везде, поэтому дублируем его здесь.
    """
    changed = landscape_wide_tables(root)
    portrait = content_width(root)
    landscape = 16838 - 1134 * 2
    for tbl in root.iter(W + "tbl"):
        target = landscape if is_wide(tbl) else portrait
        tbl_pr = tbl.find(W + "tblPr")
        grid = tbl.find(W + "tblGrid")
        if tbl_pr is None or grid is None:
            continue

        # Ширина в процентах ломает разметку в части программ. Переводим в твипы
        # и растягиваем колонки на всю текстовую полосу, сохраняя пропорции.
        columns = grid.findall(W + "gridCol")
        weights = column_weights(tbl, len(columns), target) if is_wide(tbl) else None
        if weights:
            # Широкие таблицы pandoc делит на равные колонки, и длинные
            # идентификаторы рвутся по слогам. Раздаём ширину по содержимому.
            for col, weight in zip(columns, weights):
                col.set(W + "w", str(weight))
        total = sum(int(col.get(W + "w") or 0) for col in columns)
        if columns and total:
            scaled, running = [], 0
            for col in columns[:-1]:
                value = round(int(col.get(W + "w")) * target / total)
                scaled.append(value)
                running += value
            scaled.append(target - running)      # остаток — последней колонке
            for col, value in zip(columns, scaled):
                col.set(W + "w", str(value))
            for row in tbl.findall(W + "tr"):
                for index, cell in enumerate(row.findall(W + "tc")):
                    tc_w = cell.find(f"{W}tcPr/{W}tcW")
                    if tc_w is not None and index < len(scaled):
                        span = cell.find(f"{W}tcPr/{W}gridSpan")
                        width_cells = int(span.get(W + "val")) if span is not None else 1
                        tc_w.set(W + "w", str(sum(scaled[index:index + width_cells])))
                        tc_w.set(W + "type", "dxa")
            changed += 1

        width = tbl_pr.find(W + "tblW")
        if width is not None:
            width.set(W + "w", str(target))
            width.set(W + "type", "dxa")
            changed += 1

        if tbl_pr.find(W + "tblBorders") is None:
            borders = etree.Element(W + "tblBorders")
            for side, color in (("top", BORDER), ("left", BORDER), ("bottom", BORDER),
                                ("right", BORDER), ("insideH", BORDER_INNER),
                                ("insideV", BORDER_INNER)):
                edge = etree.SubElement(borders, W + side)
                edge.set(W + "val", "single")
                edge.set(W + "sz", "4")
                edge.set(W + "space", "0")
                edge.set(W + "color", color)
            tbl_pr.append(borders)
            changed += 1

        rows = tbl.findall(W + "tr")
        if not rows:
            continue

        if is_wide(tbl):
            # Шестнадцать колонок при кегле 9 пт не помещаются даже в альбомную
            # A4: слова рвутся по слогам. Для таких таблиц уменьшаем кегль и поля.
            cell_mar = tbl_pr.find(W + "tblCellMar")
            if cell_mar is None:
                cell_mar = etree.SubElement(tbl_pr, W + "tblCellMar")
            for side, value in (("top", "40"), ("left", "60"),
                                ("bottom", "40"), ("right", "60")):
                edge = cell_mar.find(W + side)
                if edge is None:
                    edge = etree.SubElement(cell_mar, W + side)
                edge.set(W + "w", value)
                edge.set(W + "type", "dxa")
            for run in tbl.iter(W + "r"):
                rpr = run.find(W + "rPr")
                if rpr is None:
                    rpr = etree.Element(W + "rPr")
                    run.insert(0, rpr)
                for tag in ("sz", "szCs"):
                    node = rpr.find(W + tag)
                    if node is None:
                        node = etree.SubElement(rpr, W + tag)
                    node.set(W + "val", "14")
            changed += 1

        # Шапка повторяется на каждой странице и не разрывается.
        tr_pr = rows[0].find(W + "trPr")
        if tr_pr is None:
            tr_pr = etree.Element(W + "trPr")
            rows[0].insert(0, tr_pr)
        for flag in ("tblHeader", "cantSplit"):
            if tr_pr.find(W + flag) is None:
                sub(tr_pr, flag)
                changed += 1

        for cell in rows[0].findall(W + "tc"):
            tc_pr = cell.find(W + "tcPr")
            if tc_pr is None:
                tc_pr = etree.Element(W + "tcPr")
                cell.insert(0, tc_pr)
            if tc_pr.find(W + "shd") is None:
                shd = etree.SubElement(tc_pr, W + "shd")
                shd.set(W + "val", "clear")
                shd.set(W + "color", "auto")
                shd.set(W + "fill", HEADER_FILL)
                changed += 1
            # Чёрная линия под шапкой от pandoc — заменяем на цвет из палитры.
            for edge in tc_pr.iter(W + "bottom"):
                if edge.get(W + "color") in (None, "000000", "auto"):
                    edge.set(W + "color", BORDER)
                    changed += 1
    return changed


def fix_tree(root: etree._Element, orders: dict[str, list[str]]) -> int:
    fixed = style_tables(root)
    for tag, order in orders.items():
        if tag == "settings":
            continue
        for node in root.iter(W + tag):
            fixed += reorder(node, order)
    if root.tag == W + "settings":
        fixed += reorder(root, orders["settings"])

    for container in list(root.iter(W + "rPr")) + list(root.iter(W + "pPr")):
        for child in container:
            if child.tail and child.tail.strip():
                child.tail = None
                fixed += 1
    for ppr in root.iter(W + "pPr"):
        duplicates = ppr.findall(W + "pStyle")
        for extra in duplicates[:-1]:
            ppr.remove(extra)
            fixed += 1

    # w:nsid хранит ровно четыре байта, то есть восемь шестнадцатеричных
    # цифр; pandoc выдаёт значения короче, и схема их отвергает.
    for nsid in root.iter(W + "nsid"):
        value = nsid.get(W + "val") or ""
        if len(value) != 8:
            nsid.set(W + "val", value.rjust(8, "0")[-8:].upper())
            fixed += 1

    # m:mathPr — из пространства имён math, поэтому перестановка по списку
    # схемы его не видит; его место — сразу после w:rsids.
    if root.tag == W + "settings":
        math_pr = root.find("{http://schemas.openxmlformats.org/officeDocument/2006/math}mathPr")
        rsids = root.find(W + "rsids")
        if math_pr is not None and rsids is not None:
            index = list(root).index(rsids) + 1
            if list(root).index(math_pr) != index:
                root.remove(math_pr)
                root.insert(index, math_pr)
                fixed += 1
    return fixed


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = Path(argv[1])
    if not path.exists():
        print(f"Файл не найден: {path}")
        return 1
    if not SCHEMA.exists():
        print(f"Не найдена схема {SCHEMA} — проверка невозможна.")
        return 1

    orders = schema_orders()
    schema = etree.XMLSchema(etree.parse(str(SCHEMA)))
    total = 0

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "unpacked"
        with zipfile.ZipFile(path) as archive:
            archive.extractall(work)

        for xml_path in sorted(work.rglob("*.xml")):
            if xml_path.parent.name == "_rels":
                continue
            try:
                tree = etree.parse(str(xml_path))
            except etree.XMLSyntaxError:
                continue
            changed = fix_tree(tree.getroot(), orders)
            if changed:
                tree.write(str(xml_path), xml_declaration=True,
                           encoding="UTF-8", standalone=True)
                total += changed

        # pandoc не всегда объявляет Default-расширения для вложенных картинок,
        # и часть программ тогда отказывается открывать файл.
        types_path = work / "[Content_Types].xml"
        if types_path.exists():
            types = types_path.read_text(encoding="utf-8")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "svg": "image/svg+xml", "emf": "image/x-emf",
                    "wmf": "image/x-wmf", "bmp": "image/bmp", "tiff": "image/tiff"}
            used = {f.suffix.lstrip(".").lower() for f in (work / "word" / "media").glob("*")} \
                if (work / "word" / "media").exists() else set()
            added = ""
            for ext in sorted(used & set(mime)):
                if f'Extension="{ext}"' not in types:
                    added += f'<Default Extension="{ext}" ContentType="{mime[ext]}"/>'
            if added:
                types_path.write_text(types.replace("<Types", added + "<Types", 1)
                                      .replace(added + "<Types", "<Types", 1)
                                      .replace("</Types>", added + "</Types>", 1),
                                      encoding="utf-8")
                total += added.count("<Default")

        out = Path(tmp) / "fixed.docx"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(work.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(work).as_posix())
        shutil.move(str(out), str(path))

    print(f"Исправлено блоков: {total}")

    problems = 0
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if not name.endswith(".xml") or "/_rels/" in name:
                continue
            try:
                doc = etree.fromstring(archive.read(name))
            except etree.XMLSyntaxError:
                continue
            if doc.tag.startswith(W) and not schema.validate(etree.ElementTree(doc)):
                problems += 1
                print(f"  {name}: {schema.error_log[0].message[:150]}")
    print("Проверка по схеме:", "пройдена" if problems == 0 else f"замечаний: {problems}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
