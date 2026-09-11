"""Тренды: ряды по времени, разрез по владельцу, наклон, связи, часы недели.

Раздел показывает, как менялось всё измеримое. Ошибка здесь не роняет
сервер — она рисует убедительный график неправды, и заметить это можно
только проверкой. Поэтому каждый показатель проверяется на числе, которое
можно посчитать в уме.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from asrhub.db import Database
from asrhub.trends import (
    BUCKETS,
    PERIODS,
    ГРУППЫ,
    ПО_ID,
    ПОКАЗАТЕЛИ,
    Trends,
    _вердикт,
    _доля,
    _корреляция,
    _наклон,
    выбрать_шаг,
)

ЧАС = 3600.0
СУТКИ = 86400.0


def база(tmp_path: Path) -> Database:
    return Database(tmp_path / "asrhub.db")


def задание(db: Database, job_id: str, *, когда: float, owner: str = "анна",
            status: str = "completed", **поля) -> None:
    db.create_job({"id": job_id, "filename": f"{job_id}.wav", "owner": owner,
                   "model": "demo", "engine": "demo",
                   "media_duration_s": поля.pop("media_duration_s", 60.0),
                   "created_at": когда, **поля})
    db.update_job(job_id, status=status, finished_at=когда + 10,
                  text=поля.get("text") or "здравствуйте")
    # create_job ставит своё время — возвращаем нужное нам.
    db.execute("UPDATE jobs SET created_at=?, queued_at=?, finished_at=? WHERE id=?",
               (когда, когда, когда + 10, job_id))


# ---------------------------------------------------------------------------
# Каталог и шаг
# ---------------------------------------------------------------------------

def test_каждый_показатель_попадает_в_существующую_группу():
    известные = {и for и, _ in ГРУППЫ}
    for п in ПОКАЗАТЕЛИ:
        assert п.group in известные, f"{п.id}: группа «{п.group}» не объявлена"


def test_идентификаторы_показателей_уникальны():
    все = [п.id for п in ПОКАЗАТЕЛИ]
    assert len(все) == len(set(все))
    assert len(ПО_ID) == len(все)


def test_у_каждого_показателя_есть_подпись_и_пояснение():
    for п in ПОКАЗАТЕЛИ:
        assert п.label, п.id
        assert п.hint, f"{п.id}: без пояснения показатель — просто линия"
        assert п.better in ("", "up", "down"), п.id


@pytest.mark.parametrize("период", list(PERIODS))
def test_авто_шаг_держится_в_разумных_пределах(период):
    """Меньше двадцати точек — не ряд; больше двухсот пятидесяти — шум."""
    имя, шаг = выбрать_шаг(PERIODS[период], "auto")
    точек = PERIODS[период] / шаг
    assert имя in BUCKETS
    assert точек <= 250, f"{период}: {точек:.0f} точек — слишком мелко"


def test_явный_шаг_уважается_если_помещается():
    имя, шаг = выбрать_шаг(PERIODS["week"], "hour")
    assert имя == "hour" and шаг == ЧАС


def test_слишком_мелкий_шаг_укрупняется_а_не_отдаёт_тысячи_точек():
    """Год по часам — это почти девять тысяч точек: их не нарисовать."""
    имя, шаг = выбрать_шаг(PERIODS["year"], "hour")
    assert PERIODS["year"] / шаг <= 250
    assert имя != "hour"


# ---------------------------------------------------------------------------
# Ряды
# ---------------------------------------------------------------------------

def test_ряд_считает_задания_по_корзинам(tmp_path: Path):
    db = база(tmp_path)
    сейчас = time.time()
    for i in range(3):
        задание(db, f"a{i}", когда=сейчас - 2 * ЧАС)
    задание(db, "b0", когда=сейчас - 30 * ЧАС)
    свод = Trends(db).series("week", bucket="day", metrics=["jobs"],
                             compare=False, is_admin=True)
    значения = [v for v in свод["series"][0]["values"] if v]
    assert sum(значения) == 4
    assert max(значения) == 3, "три задания одного часа должны лечь в одну корзину"


def test_ряды_разных_показателей_на_общих_корзинах(tmp_path: Path):
    """Иначе две линии на одном графике врут друг про друга."""
    db = база(tmp_path)
    задание(db, "c0", когда=time.time() - ЧАС)
    свод = Trends(db).series("week", bucket="day",
                             metrics=["jobs", "completed"],
                             compare=False, is_admin=True)
    длины = {len(р["values"]) for р in свод["series"]}
    assert len(длины) == 1
    assert len(свод["buckets"]) == длины.pop()


def test_разрез_по_владельцу_в_рядах(tmp_path: Path):
    db = база(tmp_path)
    сейчас = time.time()
    задание(db, "d0", когда=сейчас - ЧАС, owner="анна")
    задание(db, "d1", когда=сейчас - ЧАС, owner="борис")
    свои = Trends(db).series("week", metrics=["jobs"], owner="анна",
                             compare=False, is_admin=False)
    все = Trends(db).series("week", metrics=["jobs"], compare=False,
                            is_admin=True)
    assert sum(v for v in свои["series"][0]["values"] if v) == 1
    assert sum(v for v in все["series"][0]["values"] if v) == 2


def test_показатели_железа_не_отдаются_обычному_ключу(tmp_path: Path):
    """Температура карты — это про сервер, а не про записи ключа."""
    db = база(tmp_path)
    трен = Trends(db)
    служебные = [п.id for п in ПОКАЗАТЕЛИ if п.admin_only]
    assert служебные, "хотя бы один показатель должен быть только для администратора"
    обычный = трен.series("week", metrics=служебные, compare=False, is_admin=False)
    админ = трен.series("week", metrics=служебные, compare=False, is_admin=True)
    assert обычный["series"] == []
    assert len(админ["series"]) == len(служебные)
    assert служебные[0] not in {м["id"] for м in трен.catalog(is_admin=False)["metrics"]}
    assert служебные[0] in {м["id"] for м in трен.catalog(is_admin=True)["metrics"]}


def test_пустая_база_отдаёт_ряды_а_не_ошибку(tmp_path: Path):
    свод = Trends(база(tmp_path)).series("month", is_admin=True)
    assert свод["series"], "показатели должны быть даже без данных"
    assert all(all(v is None for v in р["values"]) for р in свод["series"])
    assert all(с["avg"] is None for с in свод["summary"])


def test_доля_не_считается_там_где_знаменатель_ноль():
    """Ноль из ноля — это не ноль процентов, это «неизвестно».

    Корзина, в которой знаменатель нулевой, встречается сплошь и рядом:
    были разговоры, но ни одного обязательства — и «доля обязательств со
    сроком» тогда не ноль, а неизвестно. Ноль нарисовал бы обвал там, где
    считать было нечего, и это самый убедительный сорт неправды.
    """
    считать = _доля("commitments_dated", "commitments")
    assert считать({"commitments": 0, "commitments_dated": 0}) is None
    assert считать({"commitments": 4, "commitments_dated": 1}) == 25.0
    assert считать({"commitments": 4, "commitments_dated": 0}) == 0.0, \
        "а вот ноль из четырёх — это честный ноль"


def test_пустая_корзина_не_ноль_а_пусто(tmp_path: Path):
    """Между «не было заданий» и «ноль процентов успешных» разница большая."""
    db = база(tmp_path)
    задание(db, "z0", когда=time.time() - ЧАС)
    свод = Trends(db).series("week", bucket="day", metrics=["success_rate"],
                             compare=False, is_admin=True)
    значения = свод["series"][0]["values"]
    assert значения[-1] == 100.0, "в корзине с заданием доля считается"
    assert значения[0] is None, "в пустой корзине доли нет, а не ноль"


def test_сравнение_с_прошлым_периодом(tmp_path: Path):
    db = база(tmp_path)
    сейчас = time.time()
    for i in range(4):
        задание(db, f"e{i}", когда=сейчас - 2 * СУТКИ)
    for i in range(2):
        задание(db, f"f{i}", когда=сейчас - 9 * СУТКИ)
    свод = Trends(db).series("week", bucket="day", metrics=["jobs"],
                             compare=True, is_admin=True)
    строка = свод["summary"][0]
    assert строка["previous"] is not None
    assert строка["change_percent"] is not None
    assert свод["series"][0]["previous"], "ряд прошлого периода должен приезжать"


def test_неизвестный_показатель_не_ломает_запрос(tmp_path: Path):
    свод = Trends(база(tmp_path)).series("week", metrics=["такого-нет"],
                                         compare=False, is_admin=True)
    assert свод["series"], "неизвестное имя = «все», а не пустой ответ"


# ---------------------------------------------------------------------------
# Наклон, вердикт, связь
# ---------------------------------------------------------------------------

def test_наклон_видит_рост_и_падение():
    assert _наклон([1.0, 2.0, 3.0, 4.0]) > 0
    assert _наклон([4.0, 3.0, 2.0, 1.0]) < 0
    assert _наклон([2.0, 2.0, 2.0]) == 0


def test_наклон_не_считается_по_одной_точке():
    assert _наклон([5.0]) is None
    assert _наклон([]) is None
    assert _наклон([None, None, 3.0]) is None


def test_наклон_не_спотыкается_о_дыры_в_ряду():
    """Пустая корзина — это «не было», а не ноль: ноль тянул бы наклон вниз."""
    assert _наклон([1.0, None, 3.0, None, 5.0]) > 0


@pytest.mark.parametrize("изменение,куда,ожидание", [
    (20.0, "up", "лучше"), (20.0, "down", "хуже"),
    (-20.0, "up", "хуже"), (-20.0, "down", "лучше"),
    (40.0, "up", "заметно лучше"), (-40.0, "up", "заметно хуже"),
    (0.5, "up", "ровно"), (-0.5, "down", "ровно"),
])
def test_вердикт_учитывает_куда_лучше(изменение, куда, ожидание):
    assert _вердикт(изменение, куда) == ожидание


@pytest.mark.parametrize("изменение,куда", [(20.0, ""), (None, "up"), (None, "")])
def test_вердикт_молчит_когда_сказать_нечего(изменение, куда):
    """У объёма нет «лучше», а без прошлого периода нет и сравнения."""
    assert _вердикт(изменение, куда) == ""


def test_связь_считается_только_на_общих_точках():
    """Совпадение двух точек выглядит законом природы, а им не является."""
    значение, точек = _корреляция([1.0, 2.0, None], [1.0, 2.0, 3.0])
    assert точек == 2, "дыра в одном ряду выбрасывает пару целиком"
    assert значение is None, "по двум точкам связи не бывает"
    # Три точки — минимум, на котором коэффициент вообще определён.
    значение3, точек3 = _корреляция([1.0, 2.0, 3.0], [2.0, 4.0, 6.0])
    assert точек3 == 3 and значение3 is not None


def test_прямая_и_обратная_связь():
    прямая, _ = _корреляция([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                            [2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0])
    обратная, _ = _корреляция([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
                              [8.0, 7.0, 6.0, 5.0, 4.0, 3.0, 2.0, 1.0])
    assert прямая is not None and прямая > 0.99
    assert обратная is not None and обратная < -0.99


def test_постоянный_ряд_не_даёт_связи():
    """У постоянного ряда нет разброса — делить не на что."""
    значение, _ = _корреляция([1.0] * 10, [1.0, 2.0, 3.0, 4.0, 5.0,
                                           6.0, 7.0, 8.0, 9.0, 10.0])
    assert значение is None


def test_связи_не_обещают_причину(tmp_path: Path):
    db = база(tmp_path)
    сейчас = time.time()
    for i in range(30):
        задание(db, f"g{i}", когда=сейчас - i * ЧАС)
    итог = Trends(db).correlations("week", bucket="hour", limit=5, is_admin=True)
    assert "pairs" in итог
    for пара in итог["pairs"]:
        assert пара["points"] >= 8, "связь по трём точкам — это не связь"
        assert -1.0 <= пара["r"] <= 1.0


# ---------------------------------------------------------------------------
# Часы недели
# ---------------------------------------------------------------------------

def test_часы_недели_это_семь_на_двадцать_четыре(tmp_path: Path):
    db = база(tmp_path)
    задание(db, "h0", когда=time.time() - ЧАС)
    сетка = Trends(db).heatmap("jobs", "month", is_admin=True)
    assert len(сетка["grid"]) == 7
    assert all(len(строка) == 24 for строка in сетка["grid"])


def test_неизвестный_показатель_в_часах_недели_отвергается(tmp_path: Path):
    with pytest.raises(KeyError):
        Trends(база(tmp_path)).heatmap("такого-нет", "month", is_admin=True)


def test_часы_недели_уважают_разрез_по_владельцу(tmp_path: Path):
    db = база(tmp_path)
    сейчас = time.time()
    задание(db, "i0", когда=сейчас - ЧАС, owner="анна")
    задание(db, "i1", когда=сейчас - ЧАС, owner="борис")
    # Период — неделя: на ней клетка счётного показателя равна самой сумме
    # (делить не на что), и разрез виден напрямую. На месяце та же клетка
    # делится на число недель — см. соседнюю проверку.
    своя = Trends(db).heatmap("jobs", "week", owner="анна", is_admin=False)
    вся = Trends(db).heatmap("jobs", "week", is_admin=True)
    сумма = lambda с: sum(з or 0 for строка in с["grid"] for з in строка)  # noqa: E731
    assert сумма(своя) == 1
    assert сумма(вся) == 2


def test_клетка_счётного_показателя_не_зависит_от_длины_периода(tmp_path: Path):
    """Иначе число отвечает на вопрос про период, а не про час недели.

    В одну клетку попадают все понедельники периода, и сумма росла вместе
    с его длиной: 3 за неделю, 15 за месяц, 27 за квартал. Подпись под
    картой («от X до Y, среднее Z») подавала это как значение показателя.
    """
    db = база(tmp_path)
    сейчас = time.time()
    n = 0
    for неделя in range(13):
        for _ in range(3):
            задание(db, f"h{n}", когда=сейчас - неделя * 7 * СУТКИ)
            n += 1
    тренды = Trends(db)
    клетки = {}
    for период in ("week", "month", "quarter"):
        сетка = тренды.heatmap("jobs", период, is_admin=True)
        клетки[период] = max(з for строка in сетка["grid"] for з in строка if з is not None)
        assert сетка["per"] == "в среднем за час недели"
    for период, значение in клетки.items():
        assert 2.5 <= значение <= 3.6, f"{период}: клетка = {значение}, ожидалось около 3"
    # У отношений делить нечего — они от длины периода не зависят.
    assert тренды.heatmap("rtf", "quarter", is_admin=True)["weeks"] == 1.0


def test_служебный_показатель_в_часах_недели_закрыт(tmp_path: Path):
    служебный = next(п.id for п in ПОКАЗАТЕЛИ if п.admin_only)
    with pytest.raises((KeyError, PermissionError, Exception)):
        Trends(база(tmp_path)).heatmap(служебный, "month", is_admin=False)
