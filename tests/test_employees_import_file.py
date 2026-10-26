"""Заход 52: импорт сотрудников из файла — как выгрузка портала.

Владелец прислал выгрузку справочника внутреннего портала: книга Excel XML
2003 с колонками «работает», «работает до», «фамилия», …, «тел. внутренний»,
«поставщики». Разбор её читал, но:

* в отделе оставалась вёрстка портала — «Отдел продаж <span>(МСК)</span>»,
  «&nbsp;» и ссылка «>> База знаний <<», — и в фильтре отделов, карточке и
  отчётах стояли теги;
* «137 (лич.149)» — общий номер и личный — склеивался в «137149», которого
  нет ни на одной станции;
* книги .xlsx не читались вовсе, а импорт шёл сразу по выбору файла, без
  предпросмотра.

Записи здесь — выдуманные, с той же формой, что у выгрузки.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from asrhub import employees as справочник
from asrhub.errors import ConfigError

ЗАГОЛОВКИ = ["работает", "работает до", "фамилия", "имя", "отчество", "должность", "отдел",
             "тел. рабочий", "тел. внутренний", "тел. мобильный", "электропочта", "поставщики"]
ЛЮДИ = [
    ["да", "", "Петрова", "Мария", "Ивановна", "Менеджер", "Отдел продаж   <span>(МСК)</span>",
     "", "101", "+7-900-000-00-01", "m.petrova@example.ru", ""],
    ["да", "", "Сидоров", "Пётр", "Алексеевич", "Инженер",
     "Отдел ИТ поддержки Help desk <span>(МСК)</span> &nbsp;&nbsp;&nbsp;&nbsp; "
     "<a href=\"https://portal.example/IT/knowledge\"> >> База знаний вопросов по ИТ << </a><br />",
     "+7-495-000-00-00", "137 (лич.149)", "+7-900-000-00-02", "p.sidorov@example.ru", "Поставщик А"],
    ["да", "", "Кузнецова", "Анна", "Сергеевна", "Специалист",
     "Отдел ИТ поддержки Help desk (Великие луки) </span>", "", "137 (лич.178)",
     "+7-900-000-00-03", "", ""],
    ["да", "2025-05-30", "Новикова", "Ирина", "Олеговна", "Бухгалтер", "<span>(МСК)</span>",
     "", "", "", "", ""],
]


def _книга_xml(строки: list[list[str]]) -> bytes:
    def ряд(ячейки: list[str]) -> str:
        return "<Row>" + "".join(f'<Cell><Data ss:Type="String">{escape(я)}</Data></Cell>'
                                 for я in ячейки) + "</Row>"
    return ('<?xml version="1.0"?>\n<?mso-application progid="Excel.Sheet"?>\n'
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
            '<Worksheet ss:Name="employees"><Table>' + ряд(ЗАГОЛОВКИ)
            + "".join(ряд(с) for с in строки) + "</Table></Worksheet></Workbook>").encode()


def _книга_xlsx(строки: list[list[str]], *, числом: dict[tuple[int, int], float] | None = None
                ) -> bytes:
    """Минимальная книга .xlsx: общие строки, лист, связи — как пишет Excel."""
    общие: list[str] = []

    def номер(текст: str) -> int:
        if текст not in общие:
            общие.append(текст)
        return общие.index(текст)

    def столбец(н: int) -> str:
        return chr(ord("A") + н)

    ряды = []
    for r, ячейки in enumerate([ЗАГОЛОВКИ, *строки], start=1):
        клетки = []
        for c, значение in enumerate(ячейки):
            if not значение:
                continue                                # Excel пустые ячейки не пишет
            адрес = f"{столбец(c)}{r}"
            if числом and (r, c) in числом:
                клетки.append(f'<c r="{адрес}"><v>{числом[(r, c)]}</v></c>')
            else:
                клетки.append(f'<c r="{адрес}" t="s"><v>{номер(значение)}</v></c>')
        ряды.append(f'<row r="{r}">{"".join(клетки)}</row>')
    лист = ('<?xml version="1.0" encoding="UTF-8"?><worksheet '
            'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(ряды)}</sheetData></worksheet>')
    строки_xml = ('<?xml version="1.0" encoding="UTF-8"?><sst '
                  'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  + "".join(f"<si><t>{escape(т)}</t></si>" for т in общие) + "</sst>")
    книга = ('<?xml version="1.0" encoding="UTF-8"?><workbook '
             'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
             'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
             '<sheets><sheet name="Справочник" sheetId="1" r:id="rId1"/></sheets></workbook>')
    связи = ('<?xml version="1.0" encoding="UTF-8"?><Relationships '
             'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
             '2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, "w", zipfile.ZIP_DEFLATED) as архив:
        архив.writestr("xl/workbook.xml", книга)
        архив.writestr("xl/_rels/workbook.xml.rels", связи)
        архив.writestr("xl/worksheets/sheet1.xml", лист)
        архив.writestr("xl/sharedStrings.xml", строки_xml)
    return буфер.getvalue()


def test_отдел_без_вёрстки_портала():
    записи = справочник.разобрать(_книга_xml(ЛЮДИ))
    отделы = [з["department"] for з in записи]
    assert отделы == ["Отдел продаж (МСК)", "Отдел ИТ поддержки Help desk (МСК)",
                      "Отдел ИТ поддержки Help desk (Великие луки)", "(МСК)"]
    assert all("<" not in о and "&" not in о and "База знаний" not in о for о in отделы)


@pytest.mark.parametrize(("ячейка", "номер", "общий"), [
    ("137 (лич.149)", "149", "137"), ("лич. 22", "22", ""), ("доб. 1043", "1043", ""),
    ("1043 (склад)", "1043", ""), ("1043.0", "1043", ""), ("101, 102", "101", ""),
    ("1 043", "1043", ""), ("", "", ""),
])
def test_внутренний_номер(ячейка, номер, общий):
    assert справочник.внутренний(ячейка) == номер
    assert справочник.общий_внутренний(ячейка) == общий


def test_общий_номер_уходит_в_примечание():
    записи = {з["last_name"]: з for з in справочник.разобрать(_книга_xml(ЛЮДИ))}
    assert записи["Сидоров"]["phone_ext"] == "149"
    assert записи["Сидоров"]["note"].startswith("общий внутренний: 137")
    assert записи["Кузнецова"]["phone_ext"] == "178"
    # Личные номера разные — ключи карточек по номеру, а не по ФИО.
    assert записи["Сидоров"]["external_id"] == "149"


def test_замечание_о_повторе_номера_одно():
    люди = [*ЛЮДИ, ["да", "", "Орлов", "Денис", "", "", "Склад", "", "101", "", "", ""]]
    разбор = справочник.разобрать_подробно(_книга_xml(люди))
    про_101 = [з for з in разбор.замечания if "номер 101" in з]
    assert len(про_101) == 1


def test_книга_xlsx_читается_как_xml():
    числом = {(2, 8): 101.0}                       # Excel решил, что номер — число
    данные = _книга_xlsx(ЛЮДИ, числом=числом)
    assert справочник.определить_тип(данные) == "xlsx"
    из_xlsx = справочник.разобрать(данные)
    из_xml = справочник.разобрать(_книга_xml(ЛЮДИ))
    assert [(з["last_name"], з["department"], з["phone_ext"]) for з in из_xlsx] == \
        [(з["last_name"], з["department"], з["phone_ext"]) for з in из_xml]
    assert из_xlsx[0]["phone_ext"] == "101"


def test_старый_двоичный_xls_называется_прямо():
    with pytest.raises(ConfigError) as отказ:
        справочник.разобрать(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 600)
    assert ".xls" in отказ.value.message and "xlsx" in отказ.value.hint


def test_распакованная_книга_не_больше_предела(monkeypatch):
    monkeypatch.setattr(справочник, "XLSX_РАСПАКОВАННЫХ_МБ", 0)
    with pytest.raises(ConfigError, match="разворачивается"):
        справочник.разобрать(_книга_xlsx(ЛЮДИ))


@pytest.fixture()
def клиент(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    app = create_app(load(), start_queue=False)
    with TestClient(app) as c:
        yield c


def _импорт(клиент, данные: bytes, имя: str = "справочник.xml", **поля):
    ответ = клиент.post("/api/employees/import", files={"file": (имя, данные)},
                        data={к: str(з).lower() for к, з in поля.items()})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def test_предпросмотр_ничего_не_пишет(клиент):
    пробно = _импорт(клиент, _книга_xml(ЛЮДИ), dry_run=True)
    assert пробно["dry_run"] is True and пробно["added"] == 4
    assert клиент.get("/api/employees").json()["total"] == 0
    assert [к["field"] for к in пробно["columns"]][:3] == ["active", "active_until", "last_name"]
    assert {к["header"] for к in пробно["columns"] if not к["field"]} == set()
    assert [о["last_name"] for о in пробно["sample"]][:2] == ["Петрова", "Сидоров"]
    assert "external_id" not in пробно["sample"][0]
    assert [ч["name"] for ч in пробно["changes"]["added"]][:1] == ["Петрова Мария Ивановна"]
    # Событий импорта предпросмотр не пишет.
    события = клиент.get("/api/logs/events", params={"limit": 50})
    if события.status_code == 200:
        assert not any(с.get("kind") == "employees_imported"
                       for с in (события.json().get("items") or []))


def test_импорт_после_предпросмотра_и_что_изменилось(клиент):
    первый = _импорт(клиент, _книга_xml(ЛЮДИ))
    assert первый["added"] == 4 and первый["dry_run"] is False
    assert клиент.get("/api/employees").json()["total"] == 4
    # Вторая выгрузка: Петрова сменила отдел, Новикова уволилась (её нет).
    изменённые = [list(с) for с in ЛЮДИ[:3]]
    изменённые[0][6] = "Отдел снабжения <span>(МСК)</span>"
    пробно = _импорт(клиент, _книга_xml(изменённые), dry_run=True, deactivate_missing=True)
    assert (пробно["added"], пробно["updated"], пробно["unchanged"], пробно["deactivated"]) \
        == (0, 1, 2, 1)
    assert пробно["changes"]["updated"][0]["fields"] == ["department"]
    assert пробно["changes"]["deactivated"][0]["name"] == "Новикова Ирина Олеговна"
    # Предпросмотр ничего не тронул…
    assert клиент.get("/api/employees", params={"active": "true"}).json()["total"] == 4
    # …а импорт сделал ровно то, что показал.
    итог = _импорт(клиент, _книга_xml(изменённые), deactivate_missing=True)
    assert (итог["updated"], итог["deactivated"]) == (1, 1)
    assert клиент.get("/api/employees", params={"active": "true"}).json()["total"] == 3


def test_xlsx_через_маршрут(клиент):
    итог = _импорт(клиент, _книга_xlsx(ЛЮДИ), имя="справочник.xlsx")
    assert итог["format"] == "xlsx" and итог["added"] == 4


def test_интерфейс_импорта_с_предпросмотром():
    код = (Path(__file__).resolve().parents[1] / "server/asrhub/web/app.js").read_text("utf-8")
    сотрудники = код.split("RENDERERS.staff = {", 1)[1].split("\n};\n", 1)[0]
    assert "id=\"st-file-import\"" in сотрудники and "Импорт из файла" in сотрудники
    assert "accept=\".xml,.xls,.xlsx,.csv,.json,.txt\"" in сотрудники
    assert "if (пробно) данные.append('dry_run', 'true');" in сотрудники
    assert "Загрузить файл" not in сотрудники
