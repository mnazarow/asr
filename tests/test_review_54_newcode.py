"""Заход 54: свежая проверка кода заходов 49–53 — число против списка.

Голосовая аналитика за период, проблемы за период, вторая часть свода
«Голосовой аналитики», справочник сотрудников в отчётах. Каждая проверка
здесь падала на коде 3.1.25:

* общий внутренний номер (у двоих в разных отделах) относился отбором
  «отдел» к обоим отделам, а таблица «По отделам» — к одному;
* таблица отделов складывалась из первых трёхсот добавочных рейтинга, а
  «к базе» — только из тех, кто работал в обоих периодах;
* вторая часть свода (`part=heavy`) считалась по окну «до сейчас» момента
  своего запроса, а её числа раскрываются окном первой части;
* сводка проблем моделью уходила ключу с `mask_pii` с телефонами;
* медиана и средняя длина разговора считались от всех готовых записей, а
  переписка из текстовых каналов длины не имеет;
* трекер и пункт скоркарты с повтором в ответе считались по первому
  повтору, а список под числом — по любому;
* день календаря раскрывался полуночью браузера, а не сервера;
* дата вне разумных лет роняла отчёт ошибкой сервера.
"""
from __future__ import annotations

import datetime as dt
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

ТЕЛЕФОН = "+7 916 123-45-67"


def _приложение(monkeypatch: pytest.MonkeyPatch, *, ключи: dict[str, dict[str, Any]] | None = None):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    if ключи:
        monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    for ключ, сведения in (ключи or {}).items():
        настройки.api_keys[ключ] = {"enabled": True, **сведения}
    return create_app(настройки, start_queue=False)


def _задание(db: Any, ид: str, когда: float, *, агент: str = "", статус: str = "completed",
             длит: float | None = 120.0, owner: str = "", трекеры: list | None = None,
             скоркарта: list | None = None, причина: str = "", пересказ: str = "",
             решено: bool | None = None, тон: float | None = None) -> None:
    поля: dict[str, Any] = {"id": ид, "filename": f"{ид}.wav", "source": "phone",
                            "model": "demo-simulator", "owner": owner}
    if длит is not None:
        поля["media_duration_s"] = длит
    db.create_job(поля)
    db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, ид))
    db.update_job(ид, status=статус, finished_at=когда + 5)
    if агент:
        db.save_call(f"t:{ид}", job_id=ид, agent=агент, dst=агент, src="+79000000000",
                     started_at=когда - 10, answered=True, station="t", direction="входящий",
                     duration=int(длит or 0) + 15, billsec=int(длит or 0), owner=owner)
    if тон is not None:
        db.execute("INSERT INTO content (job_id, version, computed_at, sentiment, agent_score) "
                   "VALUES (?,?,?,?,?)", (ид, 1, когда, тон, 70.0))
    if причина or трекеры or скоркарта or пересказ:
        from asrhub.llm import tasks

        db.llm_save(ид, tasks.VERSION, model="t", reason=причина or "доставка",
                    outcome="перезвонить", resolved=решено, summary=пересказ,
                    trackers=трекеры or [], scorecard=скоркарта or [])


def _прошлый_месяц() -> dt.date:
    return (dt.date.today().replace(day=1) - dt.timedelta(days=1)).replace(day=1)


def _момент(день: dt.date, час: int = 11, минута: int = 0) -> float:
    return dt.datetime(день.year, день.month, день.day, час, минута).timestamp()


def _сотрудник(db: Any, ключ: str, фамилия: str, отдел: str, номер: str,
               правка: float) -> None:
    db.employee_save({"last_name": фамилия, "first_name": "И", "department": отдел,
                      "phone_ext": номер, "source": "т", "external_id": ключ})
    db.execute("UPDATE employees SET updated_at=? WHERE external_id=?", (правка, ключ))


def _список(клиент: TestClient, окно: dict[str, Any], f: list[str], base: str = "jobs",
            **заголовки: str) -> int:
    параметры: dict[str, Any] = {"base": base, "f": f, "limit": 1}
    for граница in ("since", "until"):
        if окно.get(граница) is not None:
            параметры[граница] = окно[граница]
    ответ = клиент.get("/api/drill", params=параметры, headers=заголовки or None)
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert not тело["timed_out"]
    return int(тело["total"])


# ---------------------------------------------------------------------------
# Отделы: общий номер, все добавочные, база
# ---------------------------------------------------------------------------


def test_общий_номер_относится_к_отделу_своей_карточки(data_dir: Path,
                                                       monkeypatch: pytest.MonkeyPatch):
    """Номер 137 у двоих: Старый (Продажи) и Новый (Сервис, свежая карточка).

    Отчёт подписывает номер Новым, и таблица отделов кладёт его разговоры в
    «Сервис». Отбор «Продажи» и список под числом отдела отдавали те же пять
    разговоров ещё раз — подписанными «Сервисом».
    """
    app = _приложение(monkeypatch)
    db = app.state.hub.db
    _сотрудник(db, "a", "Старый", "Продажи", "137", 1000.0)
    _сотрудник(db, "b", "Новый", "Сервис", "137", 2000.0)
    месяц = _прошлый_месяц()
    for i in range(5):
        _задание(db, f"o{i}", _момент(месяц.replace(day=3)) + i, агент="137")
    with TestClient(app) as c:
        def отчёт(**ещё: str) -> dict[str, Any]:
            ответ = c.get("/api/voice/period",
                          params={"scale": "month", "date": месяц.isoformat(), "part": "fast",
                                  **ещё})
            assert ответ.status_code == 200, ответ.text
            return ответ.json()

        весь = отчёт()
        отделы = {о["department"]: о["completed"] for о in весь["departments"]}
        assert отделы == {"Сервис": 5}
        assert весь["agents"][0]["agent_department"] == "Сервис"
        assert отчёт(department="Продажи")["totals"]["current"]["completed"] == 0
        assert отчёт(department="Сервис")["totals"]["current"]["completed"] == 5
        окно = весь["window"]
        for отдел, сколько in (("Продажи", 0), ("Сервис", 5)):
            f = ["no_control", "status:completed", f"department:{отдел}"]
            assert _список(c, окно, f) == сколько, отдел
            assert _список(c, окно, [f"department:{отдел}"], base="calls") == сколько, отдел


def test_отдел_складывается_из_всех_добавочных_и_всей_базы(data_dir: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    """Отдел «Продажи»: 101, 102, 103 в этом месяце, 104 — только в прошлом.

    Рейтинг сотрудников обрезан (здесь — до двух строк), а отдел обязан
    сложиться из всех: число в таблице отделов раскрывается списком
    отбора `department:`, а ему до рейтинга дела нет. «К базе» — все
    разговоры отдела тогда, в том числе ушедшего 104.
    """
    from asrhub import voice_period as В

    monkeypatch.setattr(В, "ДОБАВОЧНЫХ", 2)
    app = _приложение(monkeypatch)
    db = app.state.hub.db
    for номер in ("101", "102", "103", "104"):
        _сотрудник(db, номер, f"Сотрудник{номер}", "Продажи", номер, 1000.0)
    месяц = _прошлый_месяц()
    прошлый = (месяц - dt.timedelta(days=1)).replace(day=1)
    n = 0
    for номер, сейчас, было in (("101", 3, 1), ("102", 2, 1), ("103", 1, 0), ("104", 0, 6)):
        for _ in range(сейчас):
            n += 1
            _задание(db, f"m{n}", _момент(месяц.replace(day=5)) + n, агент=номер)
        for _ in range(было):
            n += 1
            _задание(db, f"m{n}", _момент(прошлый.replace(day=5)) + n, агент=номер)
    with TestClient(app) as c:
        отчёт = c.get("/api/voice/period", params={"scale": "month", "date": месяц.isoformat(),
                                                   "part": "fast"}).json()
        assert len(отчёт["agents"]) == 2 and отчёт["agents_total"] == 3
        отдел = next(о for о in отчёт["departments"] if о["department"] == "Продажи")
        assert отдел["completed"] == 6 and отдел["agents"] == 3
        assert отдел["prev_completed"] == 8
        assert _список(c, отчёт["window"],
                       ["no_control", "status:completed", "department:Продажи"]) == 6


# ---------------------------------------------------------------------------
# Вторая часть — по окну первой
# ---------------------------------------------------------------------------


def test_вторая_часть_голосовой_аналитики_по_окну_первой(data_dir: Path,
                                                        monkeypatch: pytest.MonkeyPatch):
    """Трекер второй части раскрывается окном первой — и считаться обязан им.

    Запись, пришедшая после первой части (здесь — ровно на её верхней
    границе), в число второй части не входит: интерфейс передаёт ей
    `until` первой.
    """
    app = _приложение(monkeypatch)
    db = app.state.hub.db
    сейчас = time.time()
    for i in range(3):
        _задание(db, f"v{i}", сейчас - 3600 - i, трекеры=[{"id": "t1", "fired": True}])
    with TestClient(app) as c:
        первая = c.get("/api/content/llm", params={"period": "week", "part": "fast"}).json()
        окно = первая["window"]
        assert окно["until"] <= time.time()
        _задание(db, "v-поздняя", окно["until"], трекеры=[{"id": "t1", "fired": True}])
        вторая = c.get("/api/content/llm", params={
            "period": "week", "part": "heavy", "since": окно["since"],
            "until": окно["until"]}).json()
        трекер = next(т for т in вторая["trackers"] if т["id"] == "t1")
        assert трекер["fired"] == 3
        assert _список(c, окно, ["status:completed", "no_control", "llm_ok", "tracker:t1"],
                       base="llm") == трекер["fired"]
        assert вторая["window"] == окно


def test_отчёт_за_период_вторая_часть_и_проблемы_на_момент_первой(
        data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Идущий месяц: конец окна — «сейчас» первой части (`generated_at`).

    Вторая часть и сводка проблем рядом с отчётом спрашиваются позже; с
    `now` они считаются на тот же момент, и число трекера совпадает со
    списком под ним, раскрытым окном отчёта.
    """
    app = _приложение(monkeypatch)
    db = app.state.hub.db
    сейчас = time.time()
    начало_месяца = dt.datetime.combine(dt.date.today().replace(day=1), dt.time()).timestamp()
    когда = max(начало_месяца + 1, сейчас - 120)
    for i in range(2):
        _задание(db, f"w{i}", когда + i * 0.1, трекеры=[{"id": "t1", "fired": True}],
                 причина="доставка")
    with TestClient(app) as c:
        первая = c.get("/api/voice/period", params={"scale": "month", "part": "fast"}).json()
        assert первая["complete"] is False
        момент = первая["generated_at"]
        assert первая["window"]["until"] == pytest.approx(момент)
        _задание(db, "w-поздняя", момент, трекеры=[{"id": "t1", "fired": True}], причина="доставка")
        вторая = c.get("/api/voice/period", params={"scale": "month", "part": "heavy",
                                                    "now": момент}).json()
        трекер = next(т for т in вторая["lists"]["trackers"] if т["id"] == "t1")
        assert трекер["fired"] == 2
        assert _список(c, первая["window"],
                       ["no_control", "status:completed", "llm_ok", "tracker:t1"],
                       base="llm") == 2
        assert вторая["window"] == первая["window"]
        проблемы = c.get("/api/voice/problems", params={"scale": "month", "now": момент}).json()
        assert проблемы["window"]["until"] == pytest.approx(момент)
        # Момент из будущего не принимается за «сейчас».
        будущее = c.get("/api/voice/period", params={"scale": "month", "part": "fast",
                                                     "now": time.time() + 86400}).json()
        assert будущее["generated_at"] <= time.time()


# ---------------------------------------------------------------------------
# Сводка моделью и mask_pii
# ---------------------------------------------------------------------------


def test_сводка_моделью_приходит_ключу_mask_pii_без_телефонов(
        data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    app = _приложение(monkeypatch, ключи={
        "ah_mask": {"name": "анна", "role": "user", "mask_pii": True}})
    состояние = app.state.hub
    состояние.settings.set("llm_backend", "stub")
    состояние.settings.set("llm_model", "stub")
    db = состояние.db
    сейчас = time.time()
    for i in range(3):
        _задание(db, f"p{i}", сейчас - 3600 * (i + 1), owner="анна", причина="доставка",
                 решено=False, тон=-0.6,
                 пересказ=f"Клиент просит перезвонить на {ТЕЛЕФОН}, заказ не привезли.")
    цифры = "".join(ч for ч in ТЕЛЕФОН if ч.isdigit())

    def есть_телефон(данные: Any) -> bool:
        текст = str(данные)
        return цифры in "".join(ч for ч in текст if ч.isdigit()) or ТЕЛЕФОН in текст

    with TestClient(app) as c:
        заголовки = {"X-API-Key": "ah_mask"}
        assert состояние.llm.enabled
        ответ = c.post("/api/voice/problems/digest", params={"period": "week"},
                       headers=заголовки)
        assert ответ.status_code == 200, ответ.text
        составлено = ответ.json()
        assert составлено["result"]["problems"], составлено
        assert not есть_телефон(составлено["result"])
        снова = c.get("/api/voice/problems", params={"period": "week"},
                      headers=заголовки).json()
        assert снова["digest"] and not есть_телефон(снова["digest"]["result"])
        # Хранится сводка полной: обезличивается выдача, а не запись.
        хранится = db.get_kv(next(iter(
            r["key"] for r in db.query("SELECT key FROM kv WHERE key LIKE 'voice_digest:%'"))))
        assert есть_телефон(хранится["result"])


# ---------------------------------------------------------------------------
# Медиана и средняя — по записям с длительностью
# ---------------------------------------------------------------------------


def test_медиана_и_средняя_по_записям_с_длительностью():
    """Четыре звонка и четыре переписки: у переписки длины нет (ноль).

    Медиана и средняя считали каждый чат разговором в ноль секунд.
    """
    from asrhub import voice_period as В

    группы = [{"k": 0, "s": "completed", "n": 8, "dur": 600.0, "dn": 4}]
    итог = В.Отчёт._итог_заданий(группы)
    assert итог["completed"] == 8 and итог["avg_duration_s"] == 150.0
    assert В.Отчёт._известных_длительностей(группы) == 4


def test_медиана_периода_с_перепиской(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub import voice_period as В

    app = _приложение(monkeypatch)
    db = app.state.hub.db
    месяц = _прошлый_месяц()
    for i, длит in enumerate((60.0, 120.0, 180.0, 240.0)):
        _задание(db, f"z{i}", _момент(месяц.replace(day=4)) + i, длит=длит, агент="101")
    for i in range(4):
        _задание(db, f"chat{i}", _момент(месяц.replace(day=6)) + i, длит=None)
    # Переписка из текстовых каналов: длины нет, в колонке — ноль.
    assert {r["d"] for r in db.query(
        "SELECT media_duration_s AS d FROM jobs WHERE id LIKE 'chat%'")} == {0.0}
    отчёт = В.Отчёт(db, "month", месяц, В.Отбор()).посчитать()
    т = отчёт["totals"]["current"]
    assert т["completed"] == 8
    assert т["median_duration_s"] == 180.0 and т["p90_duration_s"] == 240.0
    assert т["avg_duration_s"] == 150.0


# ---------------------------------------------------------------------------
# Трекер и пункт скоркарты с повтором
# ---------------------------------------------------------------------------


def test_повтор_трекера_и_пункта_считается_как_в_списке(data_dir: Path,
                                                       monkeypatch: pytest.MonkeyPatch):
    from asrhub import voice_period as В

    app = _приложение(monkeypatch)
    db = app.state.hub.db
    месяц = _прошлый_месяц()
    _задание(db, "dup", _момент(месяц.replace(day=7)),
             трекеры=[{"id": "t1", "fired": False}, {"id": "t1", "fired": True}],
             скоркарта=[{"id": "s1", "answer": "да"}, {"id": "s1", "answer": "нет"}])
    отчёт = В.Отчёт(db, "month", месяц, В.Отбор())
    списки = В.списки(db, отчёт)
    трекер = next(т for т in списки["trackers"] if т["id"] == "t1")
    пункт = next(п for п in списки["scorecard"] if п["id"] == "s1")
    with TestClient(app) as c:
        окно = {"since": отчёт.окно.since, "until": отчёт.окно.until}
        база = ["no_control", "status:completed", "llm_ok"]
        assert (трекер["checked"], трекер["fired"]) == (1, 1)
        assert _список(c, окно, [*база, "tracker:t1"], base="llm") == трекер["fired"]
        assert (пункт["yes"], пункт["no"]) == (1, 1)
        assert _список(c, окно, [*база, "score_no:s1"], base="llm") == пункт["no"]
        assert _список(c, окно, [*база, "score_yes:s1"], base="llm") == пункт["yes"]


# ---------------------------------------------------------------------------
# Календарь и даты
# ---------------------------------------------------------------------------


def test_день_календаря_несёт_границы_сервера(data_dir: Path,
                                             monkeypatch: pytest.MonkeyPatch):
    from asrhub import voice_period as В

    app = _приложение(monkeypatch)
    db = app.state.hub.db
    квартал = В.начало("quarter", dt.date.today())
    прошлый = В.сдвинуть("quarter", квартал, -1)
    день = прошлый + dt.timedelta(days=10)
    _задание(db, "cal", _момент(день, 23, 30))
    отчёт = В.Отчёт(db, "quarter", прошлый, В.Отбор()).посчитать()
    дни = {д["date"]: д for д in отчёт["calendar"]}
    сутки = дни[день.isoformat()]
    assert сутки["since"] == dt.datetime.combine(день, dt.time()).timestamp()
    assert сутки["until"] == dt.datetime.combine(день + dt.timedelta(days=1),
                                                 dt.time()).timestamp()
    with TestClient(app) as c:
        assert _список(c, сутки, ["no_control", "status:completed"]) == сутки["completed"] == 1
    # У идущего периода сегодняшний день кончается концом окна отчёта.
    _задание(db, "cal-сегодня", time.time() - 60)
    идущий = В.Отчёт(db, "quarter", dt.date.today(), В.Отбор()).посчитать()
    assert all(д["until"] <= идущий["window"]["until"] for д in идущий["calendar"])


@pytest.mark.parametrize("путь", ["/api/voice/period", "/api/voice/problems"])
def test_дата_вне_допустимых_лет_отвергается_понятно(data_dir: Path,
                                                    monkeypatch: pytest.MonkeyPatch, путь: str):
    app = _приложение(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        for дата in ("0001-01-15", "9999-12-31"):
            ответ = c.get(путь, params={"scale": "month" if дата < "5" else "day", "date": дата})
            assert ответ.status_code == 400, (дата, ответ.text)
            assert ответ.json()["code"] == "config_error"


# ---------------------------------------------------------------------------
# Импорт сотрудников из .xlsx: даты, битая книга, книга под паролем
# ---------------------------------------------------------------------------

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _книга(ячейки: list[list[tuple[str, str, str]]], стили: str = "",
           книга_pr: str = "") -> bytes:
    """Книга .xlsx: ячейки — (значение, тип t, стиль s); строки встроенные."""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    ряды = []
    for r, строка in enumerate(ячейки, start=1):
        клетки = []
        for c, (значение, тип, стиль) in enumerate(строка):
            адрес = f"{chr(65 + c)}{r}"
            s = f' s="{стиль}"' if стиль else ""
            if тип == "inlineStr":
                клетки.append(f'<c r="{адрес}" t="inlineStr"{s}><is><t>{escape(значение)}</t></is></c>')
            else:
                клетки.append(f'<c r="{адрес}"{s}><v>{значение}</v></c>')
        ряды.append(f'<row r="{r}">{"".join(клетки)}</row>')
    файлы = {
        "xl/workbook.xml": (f'<workbook xmlns="{_NS}" xmlns:r="http://schemas.openxmlformats.org/'
                            f'officeDocument/2006/relationships">{книга_pr}<sheets><sheet name="Штат" '
                            'sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'),
        "xl/worksheets/sheet1.xml": (f'<worksheet xmlns="{_NS}"><sheetData>{"".join(ряды)}'
                                     '</sheetData></worksheet>'),
    }
    if стили:
        файлы["xl/styles.xml"] = стили
    буфер = io.BytesIO()
    with zipfile.ZipFile(буфер, "w", zipfile.ZIP_DEFLATED) as архив:
        for имя, текст in файлы.items():
            архив.writestr(имя, текст)
    return буфер.getvalue()


_СТИЛИ = (f'<styleSheet xmlns="{_NS}"><numFmts count="1"><numFmt numFmtId="164" '
          'formatCode="dd&quot;.&quot;mm&quot;.&quot;yyyy"/></numFmts><cellXfs count="3">'
          '<xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>')


def _строки_штата(дата: str, стиль: str) -> list[list[tuple[str, str, str]]]:
    return [[("фамилия", "inlineStr", ""), ("имя", "inlineStr", ""),
             ("тел. внутренний", "inlineStr", ""), ("работает до", "inlineStr", "")],
            [("Петрова", "inlineStr", ""), ("Мария", "inlineStr", ""), ("101", "", "0"),
             (дата, "", стиль)]]


@pytest.mark.parametrize("стиль", ["1", "2"])
def test_дата_в_книге_xlsx_становится_датой_а_номер_остаётся_номером(стиль: str):
    """«Работает до 30.05.2025» лежит в ячейке числом 45807 — датой его делает стиль."""
    from asrhub import employees as Е

    разбор = Е.разобрать_подробно(_книга(_строки_штата("45807", стиль), _СТИЛИ))
    карточка = разбор.записи[0]
    assert карточка["active_until"] == "2025-05-30"
    assert карточка["phone_ext"] == "101"


def test_дата_в_книге_с_эпохой_1904():
    from asrhub import employees as Е

    данные = _книга(_строки_штата("44345", "1"), _СТИЛИ, '<workbookPr date1904="1"/>')
    assert Е.разобрать_подробно(данные).записи[0]["active_until"] == "2025-05-30"


def test_битая_книга_xlsx_отвергается_понятно_а_не_ошибкой_сервера(
        data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    данные = bytearray(_книга(_строки_штата("45807", "1"), _СТИЛИ))
    место = данные.find(b"xl/worksheets/sheet1.xml") + len("xl/worksheets/sheet1.xml") + 5
    данные[место] ^= 0xFF
    app = _приложение(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        ответ = c.post("/api/employees/import",
                       files={"file": ("штат.xlsx", bytes(данные), "application/octet-stream")},
                       data={"dry_run": "true"})
        assert ответ.status_code == 400, ответ.text
        assert ответ.json()["code"] == "config_error"
        assert "повреждена" in ответ.json()["message"]


def test_книга_под_паролем_называется_прямо():
    """Excel кладёт книгу под паролем в тот же двоичный контейнер, что и старый
    .xls; совет «пересохраните как .xlsx» ей не помогает."""
    from asrhub import employees as Е
    from asrhub.errors import ConfigError

    заголовок = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504
    каталог = "EncryptedPackage".encode("utf-16-le") + b"\x00" * 64
    with pytest.raises(ConfigError, match="паролем"):
        Е.разобрать_подробно(заголовок + каталог)
    with pytest.raises(ConfigError, match="старый двоичный"):
        Е.разобрать_подробно(заголовок + b"\x00" * 128)


def test_момент_отчёта_из_далёкого_прошлого_отвергается_понятно(data_dir: Path,
                                                               monkeypatch: pytest.MonkeyPatch):
    app = _приложение(monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        for путь in ("/api/voice/period", "/api/voice/problems"):
            ответ = c.get(путь, params={"scale": "year", "now": 0})
            assert ответ.status_code == 400, (путь, ответ.text)
