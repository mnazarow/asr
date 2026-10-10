"""Заход 49: раскрытие чисел разделов «АТС» и «Телефония» в список звонков.

Оба раздела считают журнал звонков, и каждое их число — плитка, строка
таблицы, столбец, точка линии, клетка карты — раскрывается списком звонков
(`GET /api/drill?base=calls`). Здесь сверяется главное: список под числом —
ровно те звонки, из которых число сложилось. Свод считает одним запросом,
список другим; расходятся они молча, и находит это не проверка, а человек,
насчитавший в списке не столько, сколько написано на плитке.

Общих данных (`drill_seed.Архив`) на это не хватает: в них нет ни потерянных
входящих, ни повторных обращений, ни звонков без станции и без времени
начала — то есть ровно тех случаев, на которых свод и список расходятся.
Они досеиваются здесь.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from asrhub import drill
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

ПЕРИОДЫ = ("day", "week", "month", "all")
СТАНЦИИ = ("", "ats1")


def досеять(хранилище: Архив, каталог: Any) -> None:
    """Звонки, на которых свод и список легче всего расходятся."""
    db = хранилище.db
    сейчас = хранилище.данные["now"]
    # Станции в настройках: счётчики «за всё время» раздел берёт у станции,
    # а не из разреза периода. Выключены — на станцию никто не ходит.
    хранилище.app.state.hub.settings.set("telephony_stations", [
        {"id": ид, "name": имя, "source": "folder", "enabled": False,
         "recordings_dir": str(каталог)}
        for ид, имя in (("ats1", "Первая"), ("ats2", "Вторая"))])

    номер = iter(range(10_000))

    def звонок(ключ: str, когда: float, **поля: Any) -> None:
        # Номер звонящего у каждого свой: одинаковый сделал бы «повторным
        # обращением» половину досеянного.
        основа = {"owner": "анна", "src": f"+7900{next(номер):07d}", "dst": "100",
                  "direction": "входящий", "queue": "sales", "agent": "101",
                  "disposition": "ANSWERED", "duration": 40, "billsec": 30,
                  "answered": True, "station": "ats1", "context": "from-trunk"}
        основа.update(поля)
        db.save_call(ключ, job_id=основа.pop("job_id", None),
                     skipped=основа.pop("skipped", ""), started_at=когда, **основа)

    # Повторные обращения: один номер звонит снова через час с третью
    # секунды, через двое суток и через десять дней. При окне в семь дней
    # повторный есть у двух.
    for n, назад in enumerate((12 * 86400, 2 * 86400 + 3600.3, 2 * 86400, 3 * 3600)):
        звонок(f"ats1:повтор-{n}", сейчас - назад, src="+79161112233", owner="борис",
               duration=25 + n, billsec=20)
    # Потерянные входящие: в общих данных входящий всегда отвечен.
    for n in range(5):
        звонок(f"ats2:потерян-{n}", сейчас - 5 * 3600 - n * 4000, answered=False,
               disposition=("NO ANSWER", "BUSY", "CONGESTION")[n % 3], billsec=0,
               duration=15 + n, station="ats2", owner="вера", queue="support", agent="",
               skipped="без ответа" if n % 2 else "")
    # Без станции — отдельная строка сравнения станций.
    for n in range(3):
        звонок(f"ничей-{n}", сейчас - 9 * 3600 - n * 700, station="", direction="",
               queue="", agent="", context="from-internal", owner="анна")
    # Без времени начала: линия и карта их не считают, свод за всё время — считает.
    for n in range(2):
        звонок(f"ats1:без-времени-{n}", 0.0, src="", skipped="нет записи",
               answered=False, billsec=0, disposition="FAILED", direction="")
    # Из будущего: часы станции спешат. Свод сверху не ограничен.
    звонок("ats2:из-будущего", сейчас + 3 * 3600, station="ats2", owner="вера")
    # Давние: неделя, месяц и «всё время» обязаны различаться.
    for n, дней in enumerate((9, 12, 40, 45, 200)):
        звонок(f"ats1:давний-{n}", сейчас - дней * 86400 - 1234, direction="исходящий",
               queue="support", agent="102", owner="борис", dst=f"+7495000000{n}")
    # Длительности на границах корзин: ровно 30 с — уже вторая корзина.
    for n, секунд in enumerate((29, 30, 60, 120, 299, 300, 600, 1199, 1200, 1799,
                                 1800, 2400)):
        звонок(f"ats2:длина-{n}", сейчас - 7 * 3600 - n * 311, billsec=секунд,
               duration=секунд + 4 + n * 9, station="ats2", owner="вера",
               direction="внутренний", queue="", agent="103")
    # Задание удалили, звонок его помнит: в «распознанных» он остаётся.
    звонок("ats1:задание-удалено", сейчас - 11 * 3600, job_id="нет-такого-задания")
    # Отложенные и ждущие — «остальное» в полосе «Что доехало».
    звонок("ats1:отложен", сейчас - 600, skipped="отложен: ещё пишется", owner="борис")
    звонок("ats2:ждёт", сейчас - 900, station="ats2", owner="вера")
    # Причина, которая начинается так же, как другая: отбор по причине —
    # равенство, а не «начинается с».
    звонок("ats2:нет-записи-подробно", сейчас - 4 * 3600, station="ats2", owner="вера",
           skipped="нет записи: каталог пуст", answered=False, billsec=0,
           disposition="NO ANSWER", direction="исходящий")
    звонок("ats2:нет-записи", сейчас - 4 * 3600 - 50, station="ats2", owner="вера",
           skipped="нет записи", answered=False, billsec=0, disposition="NO ANSWER",
           direction="исходящий")
    db.agent_save("агент-1", {"name": "Агент филиала", "station": "ats2"})


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    каталог = tmp_path_factory.mktemp("drill-pbx")
    хранилище = Архив(каталог / "data", заданий=48, с_моделью=0)
    досеять(хранилище, каталог)
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def обзор(клиент: Any, **параметры: Any) -> dict[str, Any]:
    ответ = клиент.get("/api/telephony/overview", params=параметры)
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def З(клиент: Any, окно: dict[str, Any] | None, f: list[str] | None = None,
      **прочее: Any) -> int:
    """Сколько звонков в списке раскрытия."""
    return всего(клиент, окно, f, base="calls", **прочее)


def строки(клиент: Any, окно: dict[str, Any], f: list[str], sort: str = "") -> list[dict]:
    границы = {к: v for к, v in окно.items() if v is not None}
    return все_строки(клиент, base="calls", f=f, sort=sort, limit=200, **границы)


def станция(свод: dict[str, Any]) -> list[str]:
    return [f"station:{свод['station']}"] if свод["station"] else []


def весь(свод: dict[str, Any]) -> dict[str, Any]:
    """Окно чисел «за всё время»: нижней границы нет, верхняя — та же."""
    return {"until": свод["window"]["until"]}


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------


def test_свод_отдаёт_окно_по_которому_посчитан(c, архив):
    до = time.time()
    неделя = обзор(c, period="week")
    окно = неделя["window"]
    assert окно["since"] == неделя["since"]
    assert до - 7 * 86400 - 1 <= окно["since"] <= time.time() - 7 * 86400 + 1
    # Верх — не раньше «сейчас» и не раньше самого позднего звонка: звонок
    # из будущего в свод входит, и окно не должно его терять.
    assert окно["until"] > архив.данные["now"] + 3 * 3600
    assert обзор(c, period="all")["window"]["since"] is None
    шапка = c.get("/api/telephony/status").json()
    assert шапка["window"]["since"] is None and шапка["window"]["until"] >= окно["until"] - 1


def test_без_звонков_из_будущего_верх_окна_это_сейчас(tmp_path):
    from asrhub.db import Database

    db = Database(tmp_path / "a.db")
    assert db.call_horizon(1000.0) == 1000.0
    db.save_call("раньше", started_at=900.0)
    assert db.call_horizon(1000.0) == 1000.0
    db.save_call("позже", started_at=5000.0)
    assert db.call_horizon(1000.0) > 5000.0
    db.close()


def test_звонок_из_будущего_и_звонки_без_времени_в_списке_как_в_своде(c, архив):
    """Свод за период сверху не ограничен, свод за всё время не ограничен и
    снизу: звонок из будущего и звонки без времени начала в них входят.
    Список обязан показать их же — иначе число расходится со списком на той
    самой станции, где что-то и так не в порядке."""
    неделя = обзор(c, period="week")
    ключи = {с["uniqueid"] for с in строки(c, неделя["window"], [])}
    assert "ats2:из-будущего" in ключи and "ats1:без-времени-0" not in ключи
    всё = обзор(c, period="all")
    ключи = {с["uniqueid"] for с in строки(c, всё["window"], [])}
    assert {"ats2:из-будущего", "ats1:без-времени-0", "ats1:без-времени-1"} <= ключи
    assert len(ключи) == всё["period_calls"]["total"]
    # Линия и карта звонки без времени не считают — и признак `timed` их убирает.
    assert З(c, всё["window"], ["timed"]) == всё["period_calls"]["total"] - 2


# ---------------------------------------------------------------------------
# «АТС»: шапка и показатели контакт-центра
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_плитки_шапки(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, за = свод["window"], станция(свод), свод["period_calls"]
    assert З(c, окно, ст) == за["total"] > 0
    assert З(c, окно, [*ст, "answered"]) == за["answered"] > 0
    assert З(c, окно, [*ст, "recognized"]) == за["queued"] > 0
    assert З(c, окно, [*ст, "skipped"]) == за["skipped"] > 0
    # «Наговорено» — сумма, а не счёт: её делают звонки, где разговор был.
    с_разговором = строки(c, окно, [*ст, "billsec:1.."], sort="billsec:desc")
    assert sum(с["billsec"] for с in с_разговором) == за["talk_s"] > 0
    assert [с["billsec"] for с in с_разговором] == sorted(
        (с["billsec"] for с in с_разговором), reverse=True)
    # «Ожидание» — среднее по отвеченным; величина списка — само ожидание.
    ждали = строки(c, окно, [*ст, "answered"], sort="wait:desc")
    assert len(ждали) == за["answered"]
    assert sum(с["value"] for с in ждали) == за["wait_s"] > 0
    assert all(с["value"] == max(0, с["duration"] - с["billsec"]) for с in ждали)
    # «Всего в архиве» — за всё время.
    assert З(c, весь(свод), ст) == свод["calls"]["total"] >= за["total"]


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_показатели_контакт_центра(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, k = свод["window"], станция(свод), свод["kpi"]
    assert З(c, окно, [*ст, "answered"]) == k["answered"]
    assert З(c, окно, [*ст, "direction:входящий"]) == k["inbound"] > 0
    assert З(c, окно, [*ст, "abandoned"]) == k["abandoned"]
    входящие = [*ст, "direction:входящий", "answered"]
    assert З(c, окно, входящие) == k["inbound_answered"] > 0
    порог = int(k["service_level_s"])
    assert З(c, окно, [*входящие, f"wait_within:{порог}"]) == k["in_time"] > 0
    assert k["in_time"] < k["inbound_answered"], "порог ничего не отсекает — сверка пустая"
    assert k["service_level"] == round(k["in_time"] / k["inbound_answered"], 4)
    # Решение с первого раза: основа, повторные и без повтора.
    секунд = k["repeat_window_s"]
    assert секунд == k["repeat_window_days"] * 86400
    assert З(c, окно, [*ст, "fcr_base"]) == k["fcr_base"] > 0
    assert З(c, окно, [*ст, "fcr_base", f"repeated:{секунд}"]) == k["repeated"]
    assert З(c, окно, [*ст, "fcr_base", f"not_repeated:{секунд}"]) \
        == k["fcr_base"] - k["repeated"]
    assert k["fcr"] == round(1 - k["repeated"] / k["fcr_base"], 4)


def test_потерянные_и_повторные_в_данных_есть(c):
    """Сверка нулей с нулями ничего не ловит."""
    всё = обзор(c, period="all")["kpi"]
    assert всё["abandoned"] == 5 and всё["repeated"] == 2
    вторая = обзор(c, period="week", station="ats2")["kpi"]
    assert вторая["abandoned"] == 5
    повторы = строки(c, обзор(c, period="all")["window"],
                     ["fcr_base", f"repeated:{всё['repeat_window_s']}"])
    assert {с["uniqueid"] for с in повторы} == {"ats1:повтор-1", "ats1:повтор-2"}


def test_окно_повтора_бывает_дробным(c, архив):
    """Настройка — в днях и дробная, а в ответе дни округлены до сотых.
    Список обязан взять те же секунды, что и счёт: посчитай он окно из
    округлённых дней, повторное обращение на границе окна пропало бы."""
    настройки = архив.app.state.hub.settings
    прежнее = настройки.get("telephony_repeat_window_days")
    try:
        # Между «повтор-1» и «повтор-2» час и три десятых секунды. Окно в
        # 3600,5 секунды его ловит; целые 3600 секунд и те же дни,
        # округлённые до 0,04, — уже нет.
        настройки.set("telephony_repeat_window_days", 3600.5 / 86400)
        свод = обзор(c, period="all")
        k, окно = свод["kpi"], свод["window"]
        assert k["repeated"] == 1 and k["repeat_window_s"] == pytest.approx(3600.5)
        assert З(c, окно, ["fcr_base", f"repeated:{k['repeat_window_s']}"]) == 1
        assert З(c, окно, ["fcr_base", f"not_repeated:{k['repeat_window_s']}"]) \
            == k["fcr_base"] - 1
        assert З(c, окно, ["fcr_base", f"repeated:{k['repeat_window_days'] * 86400}"]) == 0
    finally:
        настройки.set("telephony_repeat_window_days", прежнее)


# ---------------------------------------------------------------------------
# «АТС»: обзор станций
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_сравнение_станций_и_их_карточки(c, период):
    # Станция выбрана — а сравнение станций от неё не зависит.
    свод = обзор(c, period=период, station="ats1")
    окно = свод["window"]
    assert {с["station"] for с in свод["by_station"]} >= {"ats1", "ats2", ""}
    for строка in свод["by_station"]:
        ст = [f"station:{строка['station']}"]
        assert З(c, окно, ст) == строка["total"] > 0, строка
        assert З(c, окно, [*ст, "answered"]) == строка["answered"], строка
        assert З(c, окно, [*ст, "recognized"]) == строка["queued"], строка
        assert З(c, окно, [*ст, "skipped"]) == строка["skipped"], строка
        assert sum(с["billsec"] for с in строки(c, окно, [*ст, "billsec:1.."])) \
            == строка["talk_s"], строка
    assert sum(с["total"] for с in свод["by_station"]) == З(c, окно)


def test_архив_станции_за_всё_время(c):
    """Строка «архив · пропущено · отложено» на карточке и колонки «Взято /
    Пропущено / Отложено» в заходах — не за период."""
    свод = обзор(c, period="day")
    assert [с["id"] for с in свод["stations"]] == ["ats1", "ats2"]
    for с in свод["stations"]:
        ст, счёт = [f"station:{с['id']}"], с["calls"]
        assert З(c, весь(свод), ст) == счёт["total"] > 0
        assert З(c, весь(свод), [*ст, "recognized"]) == счёт["queued"] > 0
        assert З(c, весь(свод), [*ст, "skipped"]) == счёт["skipped"] > 0
        assert З(c, весь(свод), [*ст, "deferred"]) == счёт["deferred"] > 0
        # За сутки звонков меньше, чем в архиве: окна и правда разные.
        assert З(c, свод["window"], ст) < счёт["total"]


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_направления(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, за = свод["window"], станция(свод), свод["period_calls"]
    for поле, отбор in (("inbound", "direction:входящий"), ("outbound", "direction:исходящий"),
                        ("internal", "direction:внутренний")):
        assert З(c, окно, [*ст, отбор]) == за[поле], поле
    прочие = за["total"] - за["inbound"] - за["outbound"] - за["internal"]
    assert З(c, окно, [*ст, "no_direction"]) == прочие
    assert за["inbound"] and за["internal"]
    if not станция_:
        # По всем станциям есть каждое направление и звонки без него.
        assert за["outbound"] and прочие


# ---------------------------------------------------------------------------
# «АТС»: нагрузка
# ---------------------------------------------------------------------------


def _корзина(окно: dict[str, Any], точка: dict[str, Any]) -> dict[str, float]:
    """Корзина линии, обрезанная окном периода, — как это делает раздел."""
    с = точка["t"] if окно["since"] is None else max(точка["t"], окно["since"])
    return {"since": с, "until": min(точка["until"], окно["until"])}


@pytest.mark.parametrize("период,шаг", [("day", "hour"), ("week", "hour"), ("week", "day"),
                                         ("month", "day"), ("all", "week"), ("all", "month"),
                                         ("month", "week")])
def test_точка_линии_нагрузки(c, период, шаг):
    свод = обзор(c, period=период, bucket=шаг, station="")
    окно, лента = свод["window"], свод["timeline"]
    assert len(лента) >= 2
    for точка in лента:
        # Час, сутки и неделя — ровно шаг; месяц календарный, у него длина своя.
        длина = {"hour": 3600, "day": 86400, "week": 7 * 86400}.get(шаг)
        assert точка["until"] - точка["t"] == длина or (шаг == "month"
                                                         and точка["until"] > точка["t"])
        корзина = _корзина(окно, точка)
        assert З(c, корзина) == точка["total"] > 0, точка
        assert З(c, корзина, ["direction:входящий"]) == точка["inbound"]
        assert З(c, корзина, ["no_direction"]) == (
            точка["total"] - точка["inbound"] - точка["outbound"] - точка["internal"])
        assert sum(с["billsec"] for с in строки(c, корзина, [])) == точка["talk_s"]
    # Корзины не пересекаются и вместе дают все звонки с известным временем.
    assert all(а["until"] <= б["t"] for а, б in zip(лента, лента[1:], strict=False))
    assert sum(т["total"] for т in лента) == З(c, окно, ["timed"])


def test_первая_корзина_обрезана_началом_периода(c, архив):
    """Период начинается посреди часа, а корзина — с начала часа: в первую
    корзину свод кладёт только звонки после начала периода. Список по
    границам корзины без обрезки показал бы и те, что раньше."""
    свод = обзор(c, period="day", bucket="week")
    окно, лента = свод["window"], свод["timeline"]
    assert лента[0]["t"] < окно["since"], "неделя обязана начинаться раньше суток"
    целиком = З(c, {"since": лента[0]["t"], "until": лента[0]["until"]})
    assert целиком > лента[0]["total"] == З(c, _корзина(окно, лента[0]))


def test_месячная_корзина_календарная(c):
    for точка in обзор(c, period="all", bucket="month")["timeline"]:
        начало, конец = time.localtime(точка["t"]), time.localtime(точка["until"])
        assert (начало.tm_mday, начало.tm_hour, начало.tm_min) == (1, 0, 0)
        assert (конец.tm_mday, конец.tm_hour, конец.tm_min) == (1, 0, 0)
        assert (конец.tm_year * 12 + конец.tm_mon) - (начало.tm_year * 12 + начало.tm_mon) == 1


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ("day", "week", "all"))
def test_карта_дня_недели_и_часа(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, карта = свод["window"], станция(свод), свод["heatmap"]["calls"]
    непустых = 0
    for день, ряд in enumerate(карта):
        for час, сколько in enumerate(ряд):
            if not сколько:
                continue
            непустых += 1
            assert З(c, окно, [*ст, "timed", f"weekday:{день}", f"hour:{час}"]) == сколько
    assert непустых >= 10
    # «Часы суток» и «Дни недели» — суммы столбцов и строк той же карты.
    for час in range(24):
        assert З(c, окно, [*ст, "timed", f"hour:{час}"]) == sum(ряд[час] for ряд in карта)
    for день in range(7):
        assert З(c, окно, [*ст, "timed", f"weekday:{день}"]) == sum(карта[день])
    assert sum(map(sum, карта)) == З(c, окно, [*ст, "timed"]) > 0


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_корзины_длительности(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, корзины = свод["window"], станция(свод), свод["durations"]
    assert корзины[0]["from"] is None and корзины[-1]["to"] is None
    for к in корзины:
        от = "" if к["from"] is None else к["from"]
        до = "" if к["to"] is None else к["to"]
        assert З(c, окно, [*ст, "answered", f"billsec:{от}..{до}"]) == к["count"], к
    assert sum(к["count"] for к in корзины) == свод["period_calls"]["answered"]
    if not станция_:
        # Граница входит в верхнюю корзину: 30 с — «30с–1м», 1800 с — «дольше 30м».
        assert all(к["count"] for к in корзины), корзины


# ---------------------------------------------------------------------------
# «АТС»: очереди, операторы, номера, причины
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
@pytest.mark.parametrize("ось,отбор", [("queue", "queue"), ("agent", "agent"), ("src", "src"),
                                       ("dst", "dst"), ("context", "context"),
                                       ("disposition", "disposition"), ("skipped", "reason")])
def test_строки_топов(c, период, станция_, ось, отбор):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, топ = свод["window"], станция(свод), свод["tops"][ось]
    assert топ, ось
    for строка in топ:
        свои = [*ст, f"{отбор}:{строка['value']}"]
        assert З(c, окно, свои) == строка["count"] > 0, строка
        assert З(c, окно, [*свои, "answered"]) == строка["answered"], строка
    # Наговорено и средний разговор — по первой строке: сумма и среднее
    # списка, а не только его длина.
    первая = топ[0]
    свои = [*ст, f"{отбор}:{первая['value']}"]
    assert sum(с["billsec"] for с in строки(c, окно, [*свои, "billsec:1.."])) == первая["talk_s"]
    отвеченные = строки(c, окно, [*свои, "answered"], sort="billsec:desc")
    if отвеченные:
        среднее = sum(с["billsec"] for с in отвеченные) / len(отвеченные)
        assert round(среднее, 1) == первая["avg_s"]


@pytest.mark.parametrize("станция_", СТАНЦИИ)
@pytest.mark.parametrize("период", ПЕРИОДЫ)
def test_что_доехало(c, период, станция_):
    свод = обзор(c, period=период, station=станция_)
    окно, ст, за = свод["window"], станция(свод), свод["period_calls"]
    остальное = за["total"] - за["queued"] - за["skipped"]
    assert З(c, окно, [*ст, "pending"]) == остальное > 0
    # Отложенные — часть «остального», а не пропуск.
    отложено = З(c, окно, [*ст, "deferred"])
    assert 0 < отложено <= остальное
    assert not {с["uniqueid"] for с in строки(c, окно, [*ст, "deferred"])} \
        & {с["uniqueid"] for с in строки(c, окно, [*ст, "skipped"])}


def test_агент_на_станции(c):
    свод = c.get("/api/telephony/agents").json()
    агент = свод["agents"][0]
    assert агент["station"] == "ats2"
    assert З(c, свод["window"], ["station:ats2"]) == агент["calls"]["total"] > 0


# ---------------------------------------------------------------------------
# «Телефония»: шапка над журналом
# ---------------------------------------------------------------------------


def test_шапка_телефонии(c):
    шапка = c.get("/api/telephony/status").json()
    окно, архив_ = шапка["window"], шапка["archive"]
    assert З(c, окно) == архив_["total"] > 0
    assert З(c, окно, ["recognized"]) == архив_["queued"] > 0
    assert З(c, окно, ["skipped"]) == архив_["skipped"] > 0
    assert З(c, окно, ["direction:входящий"]) == архив_["inbound"] > 0
    assert З(c, окно, ["direction:исходящий"]) == архив_["outbound"] > 0
    assert sum(с["billsec"] for с in строки(c, окно, ["billsec:1.."])) == архив_["talk_s"]
    assert len(архив_["reasons"]) >= 3
    for причина, сколько in архив_["reasons"].items():
        assert З(c, окно, [f"reason:{причина}"]) == сколько > 0, причина
    assert sum(архив_["reasons"].values()) == архив_["skipped"]


# ---------------------------------------------------------------------------
# Список: что в строке и чьё
# ---------------------------------------------------------------------------


def test_ссылка_только_на_живое_задание(c):
    """Звонок помнит и удалённое задание. Ссылка на него вела бы в «не
    найдено», поэтому номер задания в строке — только когда задание есть."""
    всё = обзор(c, period="all")
    распознанные = {с["uniqueid"]: с for с in строки(c, всё["window"], ["recognized"])}
    assert распознанные["ats1:задание-удалено"]["job_id"] is None
    живые = [с for с in распознанные.values() if с["job_id"]]
    assert живые and all(с["job_status"] for с in живые)
    # У пропущенного вместо ссылки — причина.
    пропущенные = строки(c, всё["window"], ["skipped"])
    assert пропущенные and all(с["skipped"] and not с["job_id"] for с in пропущенные)


def test_отборы_раздела_описаны_и_не_трогают_указатель_времени():
    """Плюс перед колонкой — не украшение: без него планировщик берёт нижней
    границей поиска ноль из `timed` вместо начала окна, а для `pending` идёт
    по указателю заданий и сортирует журнал целиком."""
    assert drill.ОТБОРЫ_ЗВОНКОВ["timed"].sql.startswith("+cl.started_at")
    assert drill.ОТБОРЫ_ЗВОНКОВ["pending"].sql.startswith("+cl.job_id IS NULL")
    for имя in ("station", "queue", "agent", "src"):
        # Равенство по самой колонке, иначе указатель не используется.
        assert drill.ОТБОРЫ_ЗВОНКОВ[имя].sql == f"cl.{имя} = ?", имя


@pytest.mark.parametrize("отборы,указатель", [
    (["station:ats1"], "idx_calls_station"), (["queue:sales"], "idx_calls_queue"),
    (["agent:101"], "idx_calls_agent"), (["src:+79161112233"], "idx_calls_src"),
    (["timed"], "idx_calls_started"), (["pending"], "idx_calls_started"),
    (["timed", "hour:3", "weekday:0"], "idx_calls_started"),
    (["fcr_base", "repeated:604800"], "idx_calls_src"),
])
def test_список_идёт_по_указателю_а_не_по_всему_журналу(архив, отборы, указатель):
    """План запроса первой страницы: поиск по указателю, без полной
    сортировки. На журнале в миллионы строк разница — доли секунды против
    срока запроса."""
    условия, параметры, _ = drill.разобрать(отборы, drill.ОТБОРЫ_ЗВОНКОВ)
    запрос = ("SELECT cl.uniqueid FROM calls cl LEFT JOIN jobs j ON j.id = cl.job_id WHERE "
              + " AND ".join([*условия, "cl.started_at >= ?", "cl.started_at < ?"])
              + " ORDER BY cl.started_at DESC, cl.uniqueid DESC LIMIT 51")
    план = " | ".join(р["detail"] for р in архив.db.query(
        "EXPLAIN QUERY PLAN " + запрос, [*параметры, 1.0, 2.0]))
    assert указатель in план, план
    assert "SCAN cl" not in план, план
    assert "USE TEMP B-TREE FOR ORDER BY" not in план, план
    if отборы in (["timed"], ["pending"], ["timed", "hour:3", "weekday:0"]):
        assert "started_at>? AND started_at<?" in план, план


# ---------------------------------------------------------------------------
# Чужого не показывать
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def закрытый(tmp_path_factory):
    каталог = tmp_path_factory.mktemp("drill-pbx-auth")
    хранилище = Архив(каталог / "data", auth=True, заданий=48, с_моделью=0, ключи={
        "ah_admin": {"name": "админ", "role": "admin"},
        "ah_anna": {"name": "анна", "role": "user"},
        "ah_boris": {"name": "борис", "role": "user", "group": "продажи"},
        "ah_vera": {"name": "вера", "role": "user", "group": "продажи"},
    })
    досеять(хранилище, каталог)
    with TestClient(хранилище.app) as клиент:
        yield клиент
    хранилище.закрыть()


@pytest.mark.parametrize("ключ", ["ah_admin", "ah_anna", "ah_boris"])
def test_число_своего_свода_раскрывается_в_свои_звонки(закрытый, ключ):
    """Свод и список режутся по владельцу одинаково: у ключа число меньше,
    чем у администратора, и список — ровно под это число."""
    заголовки = {"X-API-Key": ключ}

    def свой(окно, f=None):
        параметры = {"base": "calls", "f": f or [], "limit": 1,
                     **{к: v for к, v in окно.items() if v is not None}}
        return закрытый.get("/api/drill", params=параметры, headers=заголовки).json()["total"]

    свод = закрытый.get("/api/telephony/overview", params={"period": "all"},
                        headers=заголовки).json()
    окно, за, k = свод["window"], свод["period_calls"], свод["kpi"]
    assert свой(окно) == за["total"] > 0
    assert свой(окно, ["answered"]) == за["answered"] > 0
    assert свой(окно, ["skipped"]) == за["skipped"]
    assert свой(окно, ["fcr_base"]) == k["fcr_base"]
    for строка in свод["by_station"]:
        assert свой(окно, [f"station:{строка['station']}"]) == строка["total"]
    for строка in свод["tops"]["queue"]:
        assert свой(окно, [f"queue:{строка['value']}"]) == строка["count"]
    assert sum(sum(ряд) for ряд in свод["heatmap"]["calls"]) == свой(окно, ["timed"])
    шапка = закрытый.get("/api/telephony/status", headers=заголовки).json()
    assert свой(шапка["window"]) == шапка["archive"]["total"] == за["total"]
    админ = закрытый.get("/api/telephony/overview", params={"period": "all"},
                         headers={"X-API-Key": "ah_admin"}).json()["period_calls"]["total"]
    assert (за["total"] == админ) == (ключ == "ah_admin")
