"""Обновляет оглавление в .docx и сохраняет рядом PDF.

Оглавление в файле pandoc — это поле Word: Word заполняет его при открытии,
а программы попроще показывают пустую страницу. Скрипт открывает документ в
LibreOffice, обновляет поля и индексы и сохраняет результат — так оглавление
с номерами страниц есть сразу, ещё до первого открытия в Word.

    python3 docs/update_toc.py "ASR Hub — документация.docx"

Нужен установленный LibreOffice и модуль uno (пакет python3-uno).
"""
import os, subprocess, sys, time
import uno
from com.sun.star.beans import PropertyValue

SRC = os.path.abspath(sys.argv[1])
PDF = os.path.splitext(SRC)[0] + ".pdf"
PORT = 2002

proc = subprocess.Popen([
    "soffice", "--headless", "--norestore", "--nologo", "--nodefault",
    f"--accept=socket,host=127.0.0.1,port={PORT};urp;",
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

ctx = None
local = uno.getComponentContext()
resolver = local.ServiceManager.createInstanceWithContext(
    "com.sun.star.bridge.UnoUrlResolver", local)
for _ in range(60):
    try:
        ctx = resolver.resolve(
            f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext")
        break
    except Exception:
        time.sleep(1)
if ctx is None:
    print("не удалось подключиться к LibreOffice"); sys.exit(1)

desktop = ctx.ServiceManager.createInstanceWithContext(
    "com.sun.star.frame.Desktop", ctx)


def prop(name, value):
    p = PropertyValue(); p.Name = name; p.Value = value; return p


url = uno.systemPathToFileUrl(SRC)
doc = desktop.loadComponentFromURL(url, "_blank", 0, (prop("Hidden", True),))

doc.getTextFields().refresh()
indexes = doc.getDocumentIndexes()
for i in range(indexes.getCount()):
    indexes.getByIndex(i).update()
doc.getTextFields().refresh()

doc.store()
doc.storeToURL(uno.systemPathToFileUrl(PDF), (prop("FilterName", "writer_pdf_Export"),))
doc.close(False)
try:
    desktop.terminate()
except Exception:
    pass
proc.wait(timeout=60)
print("оглавление обновлено, PDF пересобран:", PDF)
