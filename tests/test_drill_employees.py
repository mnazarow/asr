"""Заход 49: раскрытие чисел раздела «Аналитика по сотрудникам».

Таблица сотрудников и карточка под ней — это полсотни чисел на человека, и
каждое теперь раскрывается: показатели разбора — списком записей, телефония
— списком звонков. Здесь сверяется главное обещание: под числом лежит ровно
то, из чего оно сложилось. Счётные числа сверяются с длиной списка, средние
— со средним по значениям самого списка: совпасть длиной и разойтись
составом для среднего было бы слишком легко.
"""
from __future__ import annotations

import pytest
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

РАЗРЕЗЫ = ("agent", "speaker", "owner", "queue", "station")
ЗВОНКИ = ("agent", "queue", "station")

#: Показатель свода → величина списка. Тот же перечень, что в интерфейсе
#: (`СОТРУДНИК_ВЕЛИЧИНЫ` в app.js); разойдутся — тест про разметку ниже скажет.
ВЕЛИЧИНЫ = {
    "agent_score": "agent_score", "sentiment": "sentiment", "compliance": "compliance",
    "empathy": "empathy", "talk_share": "talk_share", "monologue_s": "monologue_s",
    "reply_delay_s": "reply_delay_s", "interruptions": "interruptions",
    "duration_s": "duration_s", "mood": "mood", "stress": "stress", "effort": "effort",
    "fatigue": "fatigue", "clarity": "clarity", "accuracy": "accuracy",
    "politeness": "politeness", "personalization": "personalization", "rhythm": "rhythm",
    "filler_rate": "filler_rate", "diminutive_rate": "diminutive_rate", "nps": "nps",
}
#: Доля → отбор её числителя и поле свода, где числитель лежит счётом.
ДОЛИ = {
    "negative_share": ("negative", "negative"),
    "violation_share": ("violation", "violation_records"),
    "named_share": ("named", "named"),
    "frustrated_share": ("frustrated", "frustrated"),
}


def _досеять(хранилище: Архив) -> None:
    """Общий архив — про задания; сотрудникам в нём тесно.

    Запись с оператором в нём одна на троих, нарушений нет вовсе, а половина
    показателей разбора пуста. Здесь добавлено то, на чём число и список
    могут разойтись: второй и третий оператор, звонки вне окна при записи в
    окне, нарушения по категориям, показатели, измеренные не у всех.
    """
    db = хранилище.db
    сейчас = хранилище.данные["now"]
    свободные = [dict(р) for р in db.query(
        "SELECT j.id, j.owner, j.created_at FROM jobs j WHERE j.status='completed' "
        "AND NOT EXISTS (SELECT 1 FROM calls cl WHERE cl.job_id = j.id) "
        "ORDER BY j.created_at DESC")]
    for n, задание in enumerate(свободные[:42]):
        db.save_call(
            f"ats3:{1800000000 + n}.{n}", job_id=задание["id"], owner=задание["owner"],
            src=f"+7495000{n:04d}", dst=("102", "103", "104")[n % 3],
            direction=("входящий", "исходящий")[n % 2],
            queue=("support", "vip", "")[n % 3], agent=("102", "103", "")[(n // 2) % 3],
            disposition="ANSWERED", duration=90 + n, billsec=40 + n * 3, answered=True,
            # Каждый пятый звонок начался сорок дней назад, а запись его в
            # окне: звонков у сотрудника меньше, чем записей, и раскрыть одно
            # другим нельзя.
            started_at=(сейчас - 40 * 86400 - n) if n % 5 == 0 else задание["created_at"] - 60,
            station=("ats2", "ats3")[n % 2], context="from-queue")
    for n in range(18):
        # Звонки без записи: недозвоны свежие и трёхдневной давности.
        db.save_call(
            f"ats3:{1810000000 + n}.{n}", owner=("анна", "борис", "вера")[n % 3],
            src=f"+7495111{n:04d}", dst="102", direction="входящий",
            queue=("support", "vip")[n % 2], agent=("102", "103")[n % 2],
            disposition="NO ANSWER", duration=20 + n, billsec=0, answered=False,
            started_at=сейчас - (n % 2) * 3 * 86400 - n * 700 - 15, station="ats3")
    разобранные = [р["job_id"] for р in db.query(
        "SELECT c.job_id FROM content c JOIN jobs j ON j.id = c.job_id "
        "ORDER BY j.created_at DESC")]
    for n, job_id in enumerate(разобранные):
        нарушений = 1 + n % 3 if n % 4 == 1 else 0
        db.execute(
            "UPDATE content SET violations=?, stress=?, clarity=?, accuracy=?, rhythm=?, "
            "fatigue=?, name_uses=?, frustration=?, reply_delay_s=? WHERE job_id=?",
            (нарушений, (20 + (n * 7) % 70) if n % 3 else None,
             (30 + (n * 11) % 60) if n % 5 else None, 40 + (n * 13) % 55,
             (35 + (n * 17) % 60) if n % 2 else None, (n * 19) % 80 if n % 7 else None,
             (None, 0, 2)[n % 3], 1 if n % 6 == 2 else 0,
             0.4 + (n % 9) * 0.3 if n % 4 else None, job_id))
        if нарушений:
            db.execute(
                "INSERT OR REPLACE INTO content_hits (job_id, category, kind, count) "
                "VALUES (?,?,?,?)", (job_id, ("грубость", "обещал_невозможное")[(n // 4) % 2],
                                     "violation", нарушений))
        elif n % 9 == 4:
            # То же имя, но это тема разговора, а не нарушение оператора:
            # в чипы «Нарушения» такая запись не входит.
            db.execute(
                "INSERT OR REPLACE INTO content_hits (job_id, category, kind, count) "
                "VALUES (?,?,?,?)", (job_id, "грубость", "topic", 1))
    # Запись без длительности: в «Длительность, с» она не входит.
    db.execute("UPDATE jobs SET media_duration_s=NULL WHERE id=?", (разобранные[3],))


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-emp") / "data")
    _досеять(хранилище)
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _кто(by: str, ключ: str) -> str:
    """Отбор сотрудника, как его собирает интерфейс: «—» — это пусто в базе."""
    return f"{by}:{'' if ключ == '—' else ключ}"


def _границы(окно: dict | None) -> dict:
    return {к: з for к, з in (окно or {}).items() if з is not None}


def _по_величине(c, окно, отборы, величина, base="content"):
    """Значения списка «записи, где величина измерена» — в порядке величины."""
    строки = все_строки(c, base=base, f=[*отборы, f"has:{величина}"],
                        sort=f"{величина}:asc", limit=200, **_границы(окно))
    значения = [с["value"] for с in строки]
    # Переключение порядка на «сначала новые» набор не меняет.
    assert всего(c, окно, [*отборы, f"has:{величина}"], base=base) == len(значения)
    return значения


def _сверить_среднее(c, окно, отборы, ключ, число, знаков, чьё):
    значения = _по_величине(c, окно, отборы, ВЕЛИЧИНЫ[ключ])
    if число is None:
        assert not значения, (чьё, ключ, len(значения))
        return 0
    assert значения, (чьё, ключ)
    среднее = sum(значения) / len(значения)
    assert среднее == pytest.approx(число, abs=0.6 * 10 ** -знаков + 1e-9), (чьё, ключ)
    return len(значения)


def _сотрудники(c, by, period):
    ответ = c.get("/api/content/employees", params={"by": by, "period": period})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _карточка(c, by, ключ, period):
    ответ = c.get(f"/api/content/agents/{ключ}", params={"by": by, "period": period})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


# ---------------------------------------------------------------------------
# Плитки команды
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["day", "week", "all"])
def test_плитки_команды_сходятся_со_списком(c, period):
    свод = _сотрудники(c, "agent", period)
    окно, команда = свод["window"], свод["team"]
    assert (окно["since"] is None) == (period == "all")
    assert команда["records"] and всего(c, окно, base="content") == команда["records"]
    # У каждого среднего свой знаменатель — записи, где показатель измерен.
    знаменатели = {"agent_score": "scored_agents", "stress": "stress_checked",
                   "clarity": "clarity_checked", "mood": None}
    for ключ, знаменатель in знаменатели.items():
        сколько = _сверить_среднее(c, окно, [], ключ, команда[ключ], 2, "команда")
        assert сколько, ключ
        if знаменатель:
            assert сколько == команда[знаменатель], ключ
    # Данные и правда неоднородны: иначе `has:` ничего бы не проверял.
    assert команда["stress_checked"] < команда["records"]
    assert команда["clarity_checked"] < команда["records"]


# ---------------------------------------------------------------------------
# Таблица сотрудников
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("by", РАЗРЕЗЫ)
@pytest.mark.parametrize("period", ["day", "week", "all"])
def test_показатели_разбора_в_строке_сотрудника(c, by, period):
    """Записей, балл, тональность и «нарушений, %» — столбцы, которые
    раскрываются прямо в таблице."""
    свод = _сотрудники(c, by, period)
    окно = свод["window"]
    assert len(свод["items"]) >= (1 if by == "speaker" else 2), by
    for ч in свод["items"]:
        кто = [_кто(by, ч["key"])]
        assert ч["records"] and всего(c, окно, кто, base="content") == ч["records"], ч["key"]
        assert всего(c, окно, [*кто, "violation"], base="content") == ч["violation_records"]
        assert ч["violation_share"] == pytest.approx(
            100.0 * ч["violation_records"] / ч["records"], abs=0.06), ч["key"]
        assert _сверить_среднее(c, окно, кто, "agent_score", ч["agent_score"], 2,
                                ч["key"]) == ч["scored_agents"]
        assert _сверить_среднее(c, окно, кто, "sentiment", ч["sentiment"], 2,
                                ч["key"]) == ч["scored"]
    # Строки разреза — разбиение команды: вместе дают её всю.
    assert sum(ч["records"] for ч in свод["items"]) == свод["team"]["records"]
    assert sum(ч["violation_records"] for ч in свод["items"]) > 0


@pytest.mark.parametrize("by", ЗВОНКИ)
@pytest.mark.parametrize("period", ["day", "week", "all"])
def test_телефония_в_строке_сотрудника_раскрывается_звонками(c, by, period):
    """«Звонков» и «Наговорено» — журнал АТС, а не записи: в счёт входят и
    недозвоны, а окно режется началом звонка, не временем записи."""
    свод = _сотрудники(c, by, period)
    окно = свод["window"]
    со_звонками = [ч for ч in свод["items"] if ч["calls"]]
    assert len(со_звонками) >= 2, by
    расходятся = 0
    for ч in со_звонками:
        кто = [_кто(by, ч["key"])]
        assert всего(c, окно, кто, base="calls") == ч["calls"], ч["key"]
        звонки = все_строки(c, base="calls", f=кто, sort="billsec:desc", limit=200,
                            **_границы(окно))
        # По длительности встают все звонки, в том числе нулевые недозвоны.
        assert len(звонки) == ч["calls"]
        assert sum(з["billsec"] or 0 for з in звонки) == ч["talk_s"], ч["key"]
        assert sum(1 for з in звонки if з["answered"]) == ч["answered"]
        # Список записей под этим числом был бы другим списком.
        записей = всего(c, окно, кто, base="content")
        расходятся += записей != ч["calls"]
        if period == "all":
            assert any(з["job_id"] for з in звонки) and any(not з["job_id"] for з in звонки)
    assert расходятся, "звонков и записей поровну — проверка ничего не ловит"
    # У строки «без оператора» звонков нет: пустое поле в журнале — не сотрудник.
    assert all(ч["calls"] is None for ч in свод["items"] if ч["key"] == "—")


def test_у_разрезов_не_из_журнала_телефонии_нет(c):
    for by in ("speaker", "owner"):
        assert all(ч["calls"] is None and ч["talk_s"] is None
                   for ч in _сотрудники(c, by, "week")["items"])


def test_звонок_вне_окна_при_записи_в_окне(c):
    """Запись создана вчера, а её звонок — сорок дней назад (поздний импорт):
    в «Записей» она входит, в «Звонков» за неделю — нет. Каждое число
    раскрывается своим списком и своим временем."""
    неделя = _сотрудники(c, "agent", "week")
    всё = _сотрудники(c, "agent", "all")
    for ключ in ("102", "103"):
        за_неделю = next(ч for ч in неделя["items"] if ч["key"] == ключ)
        за_всё = next(ч for ч in всё["items"] if ч["key"] == ключ)
        assert за_неделю["records"] == за_всё["records"]
        assert за_неделю["calls"] < за_всё["calls"]
        assert всего(c, неделя["window"], [f"agent:{ключ}"], base="calls") == за_неделю["calls"]


# ---------------------------------------------------------------------------
# Карточка сотрудника
# ---------------------------------------------------------------------------


def _ключи(c, by, period, сколько=3):
    return [ч["key"] for ч in _сотрудники(c, by, period)["items"][:сколько]]


@pytest.mark.parametrize("by", РАЗРЕЗЫ)
def test_таблица_сравнения_карточки_три_столбца(c, by):
    """«Сотрудник», «Команда» и «Прошлый период» — три разных набора записей:
    свой за окно, вся команда за окно, свой за прошлое окно."""
    проверено = {"свой": 0, "команда": 0, "прошлый": 0, "доли": 0}
    for ключ in _ключи(c, by, "day"):
        карточка = _карточка(c, by, ключ, "day")
        окно, прошлое = карточка["window"], карточка["previous_window"]
        assert прошлое["until"] == окно["since"] and прошлое["since"] < прошлое["until"]
        кто = [_кто(by, ключ)]
        свой, команда, прежде = карточка["summary"], карточка["team"], карточка["previous"]
        assert всего(c, окно, кто, base="content") == свой["records"] > 0
        assert всего(c, окно, base="content") == команда["records"]
        assert всего(c, прошлое, кто, base="content") == прежде["records"]
        for строка in карточка["compare"]:
            имя, знаков = строка["key"], строка["digits"] + 1
            if имя in ВЕЛИЧИНЫ:
                проверено["свой"] += bool(_сверить_среднее(
                    c, окно, кто, имя, строка["agent"], знаков, ключ))
                проверено["команда"] += bool(_сверить_среднее(
                    c, окно, [], имя, строка["team"], знаков, "команда"))
                проверено["прошлый"] += bool(_сверить_среднее(
                    c, прошлое, кто, имя, строка["previous"], знаков, ключ))
            elif имя in ДОЛИ:
                отбор, поле = ДОЛИ[имя]
                for границы, отборы, набор in ((окно, кто, свой), (окно, [], команда),
                                               (прошлое, кто, прежде)):
                    assert всего(c, границы, [*отборы, отбор], base="content") == набор[поле], \
                        (ключ, имя)
                    проверено["доли"] += bool(набор[поле])
            else:
                # Разность долей и доля возражений: одного списка за числом нет.
                assert имя in ("objections_unhandled_share", "nps_index"), имя
    assert all(сколько >= 10 for сколько in проверено.values()), проверено


def test_длительность_раскрывается_своей_величиной(c, архив):
    """`duration` подставляет ноль вместо пустоты, а среднее пустых не считает:
    под «Длительность, с» записи без длительности быть не должно."""
    свод = _сотрудники(c, "agent", "all")
    окно = свод["window"]
    с_длительностью = всего(c, окно, ["has:duration_s"], base="content")
    assert с_длительностью == свод["team"]["records"] - 1
    assert всего(c, окно, ["has:duration"], base="content") == свод["team"]["records"]
    значения = _по_величине(c, окно, [], "duration_s")
    точно = архив.db.query_one(
        "SELECT AVG(j.media_duration_s) AS v FROM content c JOIN jobs j ON j.id = c.job_id "
        "WHERE j.status='completed'")["v"]
    assert sum(значения) / len(значения) == pytest.approx(точно)


@pytest.mark.parametrize("by", РАЗРЕЗЫ)
def test_шапка_и_нарушения_карточки(c, by):
    чипов = 0
    for ключ in _ключи(c, by, "all"):
        карточка = _карточка(c, by, ключ, "all")
        окно, кто = карточка["window"], [_кто(by, ключ)]
        assert окно["since"] is None and карточка["previous_window"] is None
        assert всего(c, окно, кто, base="content") == карточка["summary"]["records"]
        for нарушение in карточка["violations"]:
            assert нарушение["records"] and всего(
                c, окно, [*кто, f"violation_category:{нарушение['category']}"],
                base="content") == нарушение["records"], (ключ, нарушение)
            чипов += 1
        # Тема с тем же именем нарушением не считается — ни в числе, ни в списке.
        if any(н["category"] == "грубость" for н in карточка["violations"]):
            assert всего(c, окно, [*кто, "category:грубость"], base="content") >= всего(
                c, окно, [*кто, "violation_category:грубость"], base="content")
    assert чипов >= 2, by


def test_тема_с_именем_нарушения_в_чип_не_входит(c):
    свод = _сотрудники(c, "speaker", "all")
    окно = свод["window"]
    assert всего(c, окно, ["category:грубость"], base="content") > всего(
        c, окно, ["violation_category:грубость"], base="content") > 0


@pytest.mark.parametrize("by", РАЗРЕЗЫ)
@pytest.mark.parametrize("period", ["day", "week"])
def test_точка_хода_по_неделям_раскрывается_своей_корзиной(c, by, period):
    непустых = 0
    for ключ in _ключи(c, by, period, 2):
        карточка = _карточка(c, by, ключ, period)
        ход = карточка["timeline"]
        assert len(ход) >= 4
        for n, точка in enumerate(ход):
            assert точка["since"] < точка["until"]
            # Корзины стыкуются без щелей и нахлёстов — точными границами,
            # а не округлённой подписью.
            if n:
                assert точка["since"] == ход[n - 1]["until"]
            assert всего(c, {"since": точка["since"], "until": точка["until"]},
                         [_кто(by, ключ)], base="content") == точка["records"], (ключ, n)
            непустых += bool(точка["records"])
        assert sum(т["records"] for т in ход) == карточка["summary"]["records"]
    assert непустых >= 2, (by, period)


# ---------------------------------------------------------------------------
# Разметка: интерфейс раскрывает тем же, что сверено выше
# ---------------------------------------------------------------------------


def test_интерфейс_раскрывает_теми_же_отборами():
    """Сверка выше бессмысленна, если раздел строит отбор иначе."""
    import re
    from pathlib import Path

    код = (Path(__file__).resolve().parents[1] / "server/asrhub/web/app.js").read_text("utf-8")
    величины = код.split("const СОТРУДНИК_ВЕЛИЧИНЫ = {", 1)[1].split("\n};", 1)[0]
    пары = dict(re.findall(r"^\s*(\w+): \['(\w+)', '(?:asc|desc)'", величины, re.M))
    assert пары == ВЕЛИЧИНЫ
    доли = код.split("const СОТРУДНИК_ДОЛИ = {", 1)[1].split("\n};", 1)[0]
    assert dict(re.findall(r"^\s*(\w+): \['(\w+)'", доли, re.M)) == {
        имя: отбор for имя, (отбор, _) in ДОЛИ.items()}
    раздел = код.split("RENDERERS.employees = {", 1)[1].split("\n};\n", 1)[0]
    # Сотрудник — одноимённый разрезу отбор, «—» превращается в пусто.
    assert "`${state.employeeBy}:${Drill.val(ключ, '—')}`" in раздел
    assert "base: 'calls', f: [this.who(ч.key)]" in раздел
    assert "`violation_category:${н.category}`" in раздел
    assert "Drill.spec({ since: т.since, until: т.until }" in раздел


# ---------------------------------------------------------------------------
# Скорость: список идёт по индексу времени
# ---------------------------------------------------------------------------


def _план(архив, monkeypatch, **параметры) -> str:
    """План настоящего запроса страницы — того, что уходит в базу."""
    from asrhub import drill

    пойманы: list[tuple[str, list]] = []
    настоящий = архив.db.query_bounded

    def подсмотреть(sql, params=(), **прочее):
        пойманы.append((sql, list(params)))
        return настоящий(sql, params, **прочее)

    monkeypatch.setattr(архив.db, "query_bounded", подсмотреть)
    drill.Drill(архив.db).page(**параметры)
    sql, аргументы = пойманы[0]
    return " | ".join(str(р["detail"]) for р in архив.db.query(
        "EXPLAIN QUERY PLAN " + sql, аргументы))


def test_список_записей_сотрудника_идёт_по_индексу_времени(архив, monkeypatch):
    """Условие «задание готово» отбирает почти весь архив, а планировщик без
    статистики всё равно брал индекс по статусу: читал все готовые задания,
    сортировал их во временной таблице и только потом отдавал первые
    полсотни. На 400 тысячах записей — две секунды на страницу «за всё
    время» вместо долей миллисекунды; на паре миллионов список не укладывался
    бы в срок вовсе."""
    for параметры in (
            {"base": "content", "since": 1.0, "until": 2.0, "filters": ["agent:101"]},
            {"base": "content", "filters": ["speaker:SPEAKER_00", "violation"]},
            {"base": "content", "since": 1.0, "until": 2.0, "filters": []}):
        план = _план(архив, monkeypatch, **параметры)
        assert ("idx_jobs_window" in план or "idx_jobs_created" in план) \
            and "idx_jobs_status" not in план, план
        # Досортировка одинаковых времён — не сортировка всего набора.
        assert "USE TEMP B-TREE FOR ORDER BY" not in план, план
    # Звонки сотрудника — по указателю «оператор + начало звонка» (или по
    # началу звонка): в любом случае не проход по журналу и не сортировка
    # всего набора.
    план = _план(архив, monkeypatch, base="calls", since=1.0, until=2.0,
                 filters=["agent:101"])
    assert ("idx_calls_agent" in план or "idx_calls_started" in план) \
        and "SCAN cl" not in план, план
    assert "USE TEMP B-TREE FOR ORDER BY" not in план, план


# ---------------------------------------------------------------------------
# Чужого не показывать
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def закрытый(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-emp-auth") / "data", auth=True,
                      заданий=60, с_моделью=0, ключи={
                          "ah_admin": {"name": "админ", "role": "admin"},
                          "ah_anna": {"name": "анна", "role": "user"},
                          "ah_boris": {"name": "борис", "role": "user", "group": "продажи"},
                          "ah_vera": {"name": "вера", "role": "user", "group": "продажи"}})
    _досеять(хранилище)
    with TestClient(хранилище.app) as клиент:
        yield клиент
    хранилище.закрыть()


class _Ключом:
    """Клиент, который ходит одним ключом: свод и список — одним и тем же."""

    def __init__(self, клиент, ключ):
        self._клиент, self._ключ = клиент, ключ

    def get(self, адрес, **прочее):
        return self._клиент.get(адрес, headers={"X-API-Key": self._ключ}, **прочее)


@pytest.mark.parametrize("ключ", ["ah_anna", "ah_boris", "ah_admin"])
def test_под_своим_ключом_число_и_список_сходятся(закрытый, ключ):
    """Свод режет записи по `jobs.owner`, а звонки — по `calls.owner`; список
    обязан резать там же, иначе число из своего отчёта откроет чужие звонки."""
    c = _Ключом(закрытый, ключ)
    видно = {}
    for by in ("agent", "queue"):
        свод = _сотрудники(c, by, "all")
        окно = свод["window"]
        assert свод["items"], (ключ, by)
        for ч in свод["items"]:
            кто = [_кто(by, ч["key"])]
            assert всего(c, окно, кто, base="content") == ч["records"]
            if ч["calls"]:
                assert всего(c, окно, кто, base="calls") == ч["calls"]
        видно[by] = sum(ч["records"] for ч in свод["items"])
    полный = sum(ч["records"] for ч in _сотрудники(
        _Ключом(закрытый, "ah_admin"), "agent", "all")["items"])
    assert (видно["agent"] == полный) == (ключ == "ah_admin")
