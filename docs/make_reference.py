#!/usr/bin/env python3
"""Готовит шаблон оформления Word (reference.docx) для pandoc.

За основу берётся стандартный шаблон pandoc; здесь ему задаются шрифты,
цвета заголовков, оформление таблиц и блоков кода, формат страницы A4 и
колонтитул с номером. Отдельно исправляются места, где стандартный шаблон
не соответствует схеме OOXML (лишний символ в стиле, продублированный
pStyle, перепутанный порядок элементов в settings.xml).

    python3 docs/make_reference.py [build/reference.docx] [подпись в колонтитуле]
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "build" / "reference.docx"

BLUE = "1F4E79"          # заголовки первого и второго уровня
BLUE_SOFT = "2E5F8A"     # заголовки третьего уровня и рамка цитаты
INK = "1A1A1A"

NS = ('xmlns:o="urn:schemas-microsoft-com:office:office" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
      'xmlns:v="urn:schemas-microsoft-com:vml" '
      'xmlns:w10="urn:schemas-microsoft-com:office:word" '
      'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:sl="http://schemas.openxmlformats.org/schemaLibrary/2006/main"')

#: Колонтитул. Подпись подставляется: у отдельного справочника по
#: интерфейсу она своя, иначе он подписан как полная документация.
FOOTER_TEMPLATE = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:p>
    <w:pPr>
      <w:pBdr><w:top w:val="single" w:sz="4" w:space="6" w:color="D8DEE6"/></w:pBdr>
      <w:tabs><w:tab w:val="center" w:pos="4819"/><w:tab w:val="right" w:pos="9638"/></w:tabs>
      <w:spacing w:before="0" w:after="0"/>
    </w:pPr>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr>
      <w:t>{footer_text}</w:t></w:r>
    <w:r><w:tab/></w:r><w:r><w:tab/></w:r>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr>
      <w:fldChar w:fldCharType="begin"/></w:r>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr>
      <w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr>
      <w:fldChar w:fldCharType="separate"/></w:r>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr><w:t>1</w:t></w:r>
    <w:r><w:rPr><w:color w:val="7A8794"/><w:sz w:val="16"/></w:rPr>
      <w:fldChar w:fldCharType="end"/></w:r>
  </w:p>
</w:ftr>
'''

SETTINGS = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings {NS}>
  <w:zoom w:percent="100"/>
  <w:embedSystemFonts/>
  <w:proofState w:spelling="clean" w:grammar="clean"/>
  <w:defaultTabStop w:val="720"/>
  <w:characterSpacingControl w:val="doNotCompress"/>
  <w:updateFields w:val="true"/>
  <w:compat/>
  <w:rsids><w:rsidRoot w:val="00000000"/></w:rsids>
  <m:mathPr><m:mathFont m:val="Cambria Math"/><m:brkBin m:val="before"/>
    <m:brkBinSub m:val="--"/><m:smallFrac m:val="0"/><m:dispDef/>
    <m:lMargin m:val="0"/><m:rMargin m:val="0"/><m:wrapRight/>
    <m:intLim m:val="subSup"/><m:naryLim m:val="undOvr"/></m:mathPr>
  <w:themeFontLang w:val="ru-RU"/>
  <w:decimalSymbol w:val=","/>
  <w:listSeparator w:val=";"/>
</w:settings>
'''

SOURCE_CODE_STYLE = '''<w:style w:type="paragraph" w:customStyle="1" w:styleId="SourceCode">
    <w:name w:val="Source Code"/><w:basedOn w:val="Normal"/><w:next w:val="BodyText"/>
    <w:pPr>
      <w:pBdr><w:left w:val="single" w:sz="12" w:space="6" w:color="D0D7DE"/></w:pBdr>
      <w:shd w:val="clear" w:color="auto" w:fill="F6F8FA"/>
      <w:spacing w:before="80" w:after="80" w:line="240" w:lineRule="auto"/>
      <w:ind w:left="120"/><w:contextualSpacing/>
    </w:pPr>
    <w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>
      <w:color w:val="1A1A1A"/><w:sz w:val="17"/><w:szCs w:val="17"/></w:rPr>
  </w:style>
  '''

TABLE_STYLE = '''<w:style w:type="table" w:styleId="Table">
    <w:name w:val="Table"/><w:basedOn w:val="TableNormal"/>
    <w:pPr><w:spacing w:before="20" w:after="20" w:line="240" w:lineRule="auto"/></w:pPr>
    <w:rPr><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>
    <w:tblPr>
      <w:tblBorders>
        <w:top w:val="single" w:sz="4" w:space="0" w:color="BFC7D1"/>
        <w:left w:val="single" w:sz="4" w:space="0" w:color="BFC7D1"/>
        <w:bottom w:val="single" w:sz="4" w:space="0" w:color="BFC7D1"/>
        <w:right w:val="single" w:sz="4" w:space="0" w:color="BFC7D1"/>
        <w:insideH w:val="single" w:sz="4" w:space="0" w:color="D8DEE6"/>
        <w:insideV w:val="single" w:sz="4" w:space="0" w:color="D8DEE6"/>
      </w:tblBorders>
      <w:tblCellMar><w:top w:w="60" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>
        <w:bottom w:w="60" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar>
    </w:tblPr>
    <w:tblStylePr w:type="firstRow">
      <w:pPr><w:keepNext/></w:pPr>
      <w:rPr><w:b/><w:color w:val="1F4E79"/></w:rPr>
      <w:tcPr><w:shd w:val="clear" w:color="auto" w:fill="EDF2F7"/></w:tcPr>
    </w:tblStylePr>
  </w:style>
  '''

SECT_PR = ('<w:sectPr>'
           '<w:footerReference w:type="default" r:id="rIdFooter1"/>'
           '<w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
           'w:header="708" w:footer="567" w:gutter="0"/>'
           '<w:cols w:space="708"/><w:docGrid w:linePitch="360"/>'
           '</w:sectPr>')


def restyle(xml: str, style_id: str, ppr: str, rpr: str) -> str:
    """Заменяет оформление стиля, сохраняя его имя и наследование."""
    match = re.search(r'(<w:style [^>]*w:styleId="%s"[^>]*>)(.*?)(</w:style>)' % style_id,
                      xml, re.S)
    if not match:
        return xml
    head, body, tail = match.groups()
    keep = "".join(found.group(0) for found in
                   (re.search(r'<w:name[^>]*/>', body), re.search(r'<w:basedOn[^>]*/>', body),
                    re.search(r'<w:next[^>]*/>', body), re.search(r'<w:link[^>]*/>', body))
                   if found)
    return xml[:match.start()] + head + keep + ppr + rpr + tail + xml[match.end():]


def build_styles(xml: str) -> str:
    xml = xml.replace(
        '<w:rFonts w:asciiTheme="minorHAnsi" w:eastAsiaTheme="minorHAnsi" '
        'w:hAnsiTheme="minorHAnsi" w:cstheme="minorBidi" />',
        '<w:rFonts w:ascii="Calibri" w:eastAsia="Calibri" w:hAnsi="Calibri" w:cs="Calibri" />')
    xml = xml.replace('<w:sz w:val="24" />\n        <w:szCs w:val="24" />',
                      '<w:sz w:val="21" />\n        <w:szCs w:val="21" />')
    xml = xml.replace('<w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA" />',
                      '<w:lang w:val="ru-RU" w:eastAsia="ru-RU" w:bidi="ar-SA" />')
    xml = xml.replace('<w:spacing w:after="200" />',
                      '<w:spacing w:before="0" w:after="140" w:line="276" w:lineRule="auto" />', 1)

    heading = ('<w:pPr><w:keepNext/><w:pageBreakBefore/>'
               '<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="6" w:color="%s"/></w:pBdr>'
               '<w:spacing w:before="0" w:after="280"/><w:outlineLvl w:val="0"/></w:pPr>' % BLUE)
    xml = restyle(xml, "Heading1", heading,
                  '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/><w:b/>'
                  '<w:color w:val="%s"/><w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr>' % BLUE)
    xml = restyle(xml, "Heading2",
                  '<w:pPr><w:keepNext/><w:spacing w:before="360" w:after="140"/>'
                  '<w:outlineLvl w:val="1"/></w:pPr>',
                  '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/><w:b/>'
                  '<w:color w:val="%s"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>' % BLUE)
    xml = restyle(xml, "Heading3",
                  '<w:pPr><w:keepNext/><w:spacing w:before="260" w:after="100"/>'
                  '<w:outlineLvl w:val="2"/></w:pPr>',
                  '<w:rPr><w:b/><w:color w:val="%s"/><w:sz w:val="26"/>'
                  '<w:szCs w:val="26"/></w:rPr>' % BLUE_SOFT)
    xml = restyle(xml, "Heading4",
                  '<w:pPr><w:keepNext/><w:spacing w:before="200" w:after="80"/>'
                  '<w:outlineLvl w:val="3"/></w:pPr>',
                  '<w:rPr><w:b/><w:i/><w:color w:val="%s"/><w:sz w:val="22"/>'
                  '<w:szCs w:val="22"/></w:rPr>' % INK)
    xml = restyle(xml, "Title", '<w:pPr><w:spacing w:before="0" w:after="120"/>'
                                '<w:jc w:val="center"/></w:pPr>',
                  '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/><w:b/>'
                  '<w:color w:val="%s"/><w:sz w:val="72"/><w:szCs w:val="72"/></w:rPr>' % BLUE)
    xml = restyle(xml, "Subtitle", '<w:pPr><w:spacing w:before="0" w:after="200"/>'
                                   '<w:jc w:val="center"/></w:pPr>',
                  '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/>'
                  '<w:color w:val="595959"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>')
    xml = restyle(xml, "BlockText",
                  '<w:pPr><w:pBdr><w:left w:val="single" w:sz="18" w:space="10" '
                  'w:color="%s"/></w:pBdr>'
                  '<w:shd w:val="clear" w:color="auto" w:fill="F2F6FA"/>'
                  '<w:spacing w:before="140" w:after="140"/>'
                  '<w:ind w:left="240" w:right="120"/></w:pPr>' % BLUE_SOFT,
                  '<w:rPr><w:color w:val="%s"/><w:sz w:val="21"/>'
                  '<w:szCs w:val="21"/></w:rPr>' % INK)
    xml = restyle(xml, "VerbatimChar", "",
                  '<w:rPr><w:rFonts w:ascii="Consolas" w:hAnsi="Consolas" w:cs="Consolas"/>'
                  '<w:color w:val="A31515"/><w:sz w:val="18"/><w:szCs w:val="18"/></w:rPr>')
    xml = restyle(xml, "ImageCaption",
                  '<w:pPr><w:spacing w:before="40" w:after="200"/><w:jc w:val="center"/></w:pPr>',
                  '<w:rPr><w:i/><w:color w:val="595959"/><w:sz w:val="18"/>'
                  '<w:szCs w:val="18"/></w:rPr>')
    xml = restyle(xml, "Figure",
                  '<w:pPr><w:spacing w:before="160" w:after="40"/><w:jc w:val="center"/></w:pPr>',
                  "")
    xml = restyle(xml, "Compact",
                  '<w:pPr><w:spacing w:before="0" w:after="40" w:line="264" w:lineRule="auto"/>'
                  '<w:contextualSpacing/></w:pPr>', "")
    xml = restyle(xml, "TOCHeading",
                  '<w:pPr><w:keepNext/>'
                  '<w:pBdr><w:bottom w:val="single" w:sz="12" w:space="6" w:color="%s"/></w:pBdr>'
                  '<w:spacing w:before="0" w:after="240"/></w:pPr>' % BLUE,
                  '<w:rPr><w:rFonts w:ascii="Calibri Light" w:hAnsi="Calibri Light"/><w:b/>'
                  '<w:color w:val="%s"/><w:sz w:val="44"/><w:szCs w:val="44"/></w:rPr>' % BLUE)

    if 'w:styleId="SourceCode"' not in xml:
        xml = xml.replace('<w:style w:type="paragraph" w:styleId="BlockText"',
                          SOURCE_CODE_STYLE + '<w:style w:type="paragraph" w:styleId="BlockText"',
                          1)
    old_table = re.search(r'<w:style w:type="table" w:styleId="Table">.*?</w:style>\s*', xml, re.S)
    if old_table:
        xml = xml[:old_table.start()] + TABLE_STYLE + xml[old_table.end():]
    else:
        xml = xml.replace("</w:styles>", TABLE_STYLE + "</w:styles>", 1)
    if 'w:styleId="TableNormal"' not in xml:
        xml = xml.replace("</w:styles>",
                          '<w:style w:type="table" w:default="1" w:styleId="TableNormal">'
                          '<w:name w:val="Normal Table"/><w:tblPr/></w:style></w:styles>', 1)

    # В стандартном шаблоне pandoc внутри стиля Subtitle остаётся символ «>».
    xml = xml.replace('<w:color w:val="345A8A" />&gt;', '<w:color w:val="345A8A" />')
    return xml


def main(argv: list[str]) -> int:
    out = Path(argv[1]) if len(argv) > 1 else DEFAULT_OUT
    footer = argv[2] if len(argv) > 2 else "ASR Hub — документация"
    out.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "ref"
        base = Path(tmp) / "default.docx"
        with base.open("wb") as fh:
            subprocess.run(["pandoc", "--print-default-data-file", "reference.docx"],
                           stdout=fh, check=True)
        with zipfile.ZipFile(base) as archive:
            archive.extractall(work)

        styles = work / "word" / "styles.xml"
        styles.write_text(build_styles(styles.read_text(encoding="utf-8")), encoding="utf-8")

        (work / "word" / "footer1.xml").write_text(
            FOOTER_TEMPLATE.format(footer_text=footer), encoding="utf-8")
        (work / "word" / "settings.xml").write_text(SETTINGS, encoding="utf-8")

        rels = work / "word" / "_rels" / "document.xml.rels"
        text = rels.read_text(encoding="utf-8")
        if "footer1.xml" not in text:
            rels.write_text(text.replace(
                "</Relationships>",
                '<Relationship Id="rIdFooter1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/footer" Target="footer1.xml"/>'
                "</Relationships>", 1), encoding="utf-8")

        types = work / "[Content_Types].xml"
        text = types.read_text(encoding="utf-8")
        if "footer+xml" not in text:
            override = ('<Override PartName="/word/footer1.xml" ContentType="application/vnd.'
                        'openxmlformats-officedocument.wordprocessingml.footer+xml"/>')
            types.write_text(text.replace("</Types>", override + "</Types>", 1),
                             encoding="utf-8")

        document = work / "word" / "document.xml"
        text = document.read_text(encoding="utf-8")
        document.write_text(text.replace("<w:sectPr />", SECT_PR, 1), encoding="utf-8")

        archive_path = Path(tmp) / "reference.docx"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for item in sorted(work.rglob("*")):
                if item.is_file():
                    archive.write(item, item.relative_to(work).as_posix())
        shutil.move(str(archive_path), str(out))

    # Тот же проход, что и для готового документа: порядок элементов и мусор.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fix_docx import main as fix
    fix(["fix_docx.py", str(out)])
    print(f"Шаблон оформления: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
