"""Голосовая аналитика за период: день, месяц, квартал, год.

Раздел «Голосовая аналитика» отвечает на вопрос «что происходит» за
скользящее окно — сутки, неделю до сейчас. Руководителю нужен другой
вопрос: «как прошёл октябрь» и «чем третий квартал отличается от второго».
Это календарный период с началом и концом, с которым сравнивают соседние,
и отчёт за него — сводный: объём, качество, причины, исходы, сотрудники,
отделы, риски, — со сравнением и графиками. Так устроены периодные отчёты
у ведущих систем речевой аналитики (Amazon Contact Lens, NICE, Genesys,
Calabrio, Observe.AI): один отчёт, у которого меняются период, шаг по
времени и база сравнения.

Что здесь важно знать заранее:

* **Границы — календарные и по местному времени сервера.** День — с
  полуночи до полуночи, месяц — с первого числа, квартал — с первого числа
  первого месяца, год — с первого января. Разрезы по часам и дням во всей
  аналитике считаются в `localtime`, и здесь так же: иначе «понедельник»
  отчёта начинался бы в три часа ночи по Москве.
* **Незакрытый период сравнивается «на ту же дату».** Октябрь до 26-го
  против всего сентября — это минус пятнадцать процентов на ровном месте.
  Пока период идёт, база сравнения обрезается тем же числом прошедших
  секунд от своего начала, и отчёт об этом говорит.
* **Без речи и короткие — в объёме, но не в средних.** Они входят в «всего
  записей», и у них своя строка, но доли тональности, решённых, причин и
  исходов считаются по разговорам с речью (готовым записям): иначе тишина
  тянула бы вниз всё сразу.
* **Малые числа не красятся.** Изменение доли проверяется критерием для
  двух долей; изменение, которое могло получиться случайно, показывается,
  но значимым не называется.
* **Всё считается группами в базе.** Отчёт за год — сотни тысяч записей, и
  поднимать их в память нельзя. Запросы группируют по дате или часу, по
  причине, по добавочному; в Python складываются уже группы.
"""
from __future__ import annotations

import bisect
import datetime as dt
import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .errors import ConfigError

МАСШТАБЫ = ("day", "month", "quarter", "year")

МЕСЯЦЫ = ("январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
          "сентябрь", "октябрь", "ноябрь", "декабрь")
МЕСЯЦЫ_РОД = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря")
МЕСЯЦЫ_КОРОТКО = ("янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен",
                  "окт", "ноя", "дек")
ДНИ = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")
ДНИ_КОРОТКО = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
КВАРТАЛЫ = ("I", "II", "III", "IV")

#: Ниже скольки разговоров в обеих выборках изменение доли не называется
#: значимым, какой бы ни вышла статистика: на трёх звонках «рост на 33
#: пункта» — это один звонок.
МАЛО_ДЛЯ_ВЫВОДА = 20
#: Порог критерия для двух долей: |z| ≥ 1,96 — уровень 5 %.
Z_ЗНАЧИМО = 1.96
#: Сколько причин идёт в ряды по времени (малые графики за год и квартал).
ПРИЧИН_В_РЯДАХ = 12
#: Сколько добавочных отдаёт рейтинг сотрудников.
ДОБАВОЧНЫХ = 300
#: Порог «отрицательной» тональности — тот же, что во всей аналитике.
НЕГАТИВ = -0.15
ПОЗИТИВ = 0.15
#: Сколько запросов счётной части идут одновременно. Они независимы друг
#: от друга, каждый — проход по окну периода, и за год это десятки проходов
#: по сотням тысяч записей. SQLite отпускает GIL на время запроса, а чтение
#: в WAL не мешает соседнему чтению, так что запросы, пущенные рядом,
#: занимают все ядра вместо одного.
ПОТОКОВ = 4

_пул_запросов: ThreadPoolExecutor | None = None
_замок_пула = threading.Lock()


def _пул() -> ThreadPoolExecutor:
    """Общий пул на все отчёты: потоки и их соединения с базой переиспользуются."""
    global _пул_запросов
    with _замок_пула:
        if _пул_запросов is None:
            _пул_запросов = ThreadPoolExecutor(max_workers=ПОТОКОВ,
                                               thread_name_prefix="asrhub-period")
        return _пул_запросов


# ---------------------------------------------------------------------------
# Календарь
# ---------------------------------------------------------------------------

def _секунды(день: dt.date, час: int = 0) -> float:
    """Начало дня (и часа) по местному времени сервера — секундами эпохи."""
    return dt.datetime(день.year, день.month, день.day, час).timestamp()


def _плюс_месяцы(день: dt.date, месяцев: int) -> dt.date:
    номер = день.year * 12 + (день.month - 1) + месяцев
    return dt.date(номер // 12, номер % 12 + 1, 1)


def начало(масштаб: str, день: dt.date) -> dt.date:
    """Первый день периода, в который попадает `день`."""
    if масштаб == "day":
        return день
    if масштаб == "month":
        return день.replace(day=1)
    if масштаб == "quarter":
        return dt.date(день.year, (день.month - 1) // 3 * 3 + 1, 1)
    return dt.date(день.year, 1, 1)


def сдвинуть(масштаб: str, первый: dt.date, шагов: int) -> dt.date:
    """Первый день периода через `шагов` периодов (отрицательное — назад)."""
    if масштаб == "day":
        return первый + dt.timedelta(days=шагов)
    if масштаб == "month":
        return _плюс_месяцы(первый, шагов)
    if масштаб == "quarter":
        return _плюс_месяцы(первый, 3 * шагов)
    return dt.date(первый.year + шагов, 1, 1)


def подпись(масштаб: str, первый: dt.date) -> str:
    """«26 октября 2026, понедельник», «октябрь 2026», «IV квартал 2026», «2026 год»."""
    if масштаб == "day":
        return (f"{первый.day} {МЕСЯЦЫ_РОД[первый.month - 1]} {первый.year}, "
                f"{ДНИ[первый.weekday()]}")
    if масштаб == "month":
        return f"{МЕСЯЦЫ[первый.month - 1]} {первый.year}"
    if масштаб == "quarter":
        return f"{КВАРТАЛЫ[(первый.month - 1) // 3]} квартал {первый.year}"
    return f"{первый.year} год"


@dataclass
class Окно:
    """Период отчёта или база сравнения: границы и подпись."""

    id: str
    label: str
    first: dt.date
    since: float
    until: float
    #: Полный конец периода — `until` бывает обрезан «на ту же дату».
    end: float
    to_date: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "date": self.first.isoformat(),
                "since": self.since, "until": self.until, "end": self.end,
                "to_date": self.to_date}


def окно(масштаб: str, первый: dt.date, *, id: str = "current",  # noqa: A002
         label: str = "") -> Окно:
    следующий = сдвинуть(масштаб, первый, 1)
    с, до = _секунды(первый), _секунды(следующий)
    return Окно(id, label or подпись(масштаб, первый), первый, с, до, до)


def базы(масштаб: str, первый: dt.date) -> list[Окно]:
    """С чем сравнивать период — по масштабу.

    День сравнивают со вчера и с тем же днём неделей раньше: у звонков
    недельный ритм, и понедельник со вчерашним воскресеньем сравнивать
    бесполезно. Месяц и квартал — с прошлым и с тем же периодом год назад
    (сезонность). Год — с прошлым годом.
    """
    if масштаб == "day":
        вчера, неделя = первый - dt.timedelta(days=1), первый - dt.timedelta(days=7)
        # Главная база дня — неделей раньше: понедельник со вчерашним
        # воскресеньем сравнивать бесполезно. Вчера — вторая.
        return [окно(масштаб, неделя, id="week",
                     label=f"неделей раньше ({подпись(масштаб, неделя)})"),
                окно(масштаб, вчера, id="prev", label=f"вчера ({подпись(масштаб, вчера)})")]
    прошлый = сдвинуть(масштаб, первый, -1)
    итог = [окно(масштаб, прошлый, id="prev", label=подпись(масштаб, прошлый))]
    if масштаб in ("month", "quarter"):
        год_назад = dt.date(первый.year - 1, первый.month, 1)
        итог.append(окно(масштаб, год_назад, id="yoy",
                         label=f"год назад ({подпись(масштаб, год_назад)})"))
    return итог


#: Годы, за которые отчёт можно спросить. Шире незачем: у года 1 нет
#: прошлого года для сравнения, у 9999 — следующего дня, и такая дата
#: роняла отчёт ошибкой сервера вместо понятного ответа.
ГОДЫ = (1972, 2999)


def разобрать_дату(значение: str | None, сейчас: float | None = None) -> dt.date:
    """День из параметра `date`; пусто — день момента `сейчас` (или сегодня)."""
    if not значение:
        день = dt.date.today() if сейчас is None else dt.date.fromtimestamp(сейчас)
        значение = день.isoformat()
    else:
        try:
            день = dt.date.fromisoformat(str(значение)[:10])
        except ValueError:
            raise ConfigError(f"Дата «{значение}» не разобрана.",
                              hint="Ожидается ГГГГ-ММ-ДД, например 2026-10-26.") from None
    if not ГОДЫ[0] <= день.year <= ГОДЫ[1]:
        raise ConfigError(f"Дата «{значение}» вне допустимых лет.",
                          hint=f"Год — от {ГОДЫ[0]} до {ГОДЫ[1]}.")
    return день


# ---------------------------------------------------------------------------
# Корзины времени
# ---------------------------------------------------------------------------

@dataclass
class Корзины:
    """Шаг по времени внутри периода и как время записи ложится в корзину.

    Корзина у дня — час, у месяца — день, у квартала — неделя с
    понедельника, у года — месяц. Границы (`edges`) — местные полночи и
    часы, посчитанные питоном, так что переход на летнее время учтён.

    База группирует записи не по дате, а по номеру отрезка от начала
    периода — `CAST((время - начало) / шаг AS INTEGER)`, — а отрезок к
    корзине относит питон (`номер`). Функции даты SQLite здесь не годятся:
    `strftime(…, 'localtime')` на каждой строке не только медленнее
    арифметики вдвое, но и держит общий замок SQLite, и запросы отчёта,
    пущенные рядом (`ПОТОКОВ`), шли бы по очереди — на годовом окне
    в четыре раза медленнее каждый. Шаг — сутки, если внутри окна нет
    перевода часов (начало окна — местная полночь, и сутки от него — ровно
    местные сутки), иначе час; у отчёта за день — всегда час.
    """

    масштаб: str
    первый: dt.date
    labels: list[str] = field(default_factory=list)
    edges: list[float] = field(default_factory=list)
    _по_ключу: dict[str, int] = field(default_factory=dict)
    #: Длина отрезка, по которому группирует база, секунды.
    шаг: int = 86400

    @property
    def формат(self) -> str:
        return "%Y-%m-%d %H" if self.масштаб == "day" else "%Y-%m-%d"

    def выражение(self, столбец: str) -> str:
        """Номер отрезка от начала периода — для GROUP BY."""
        return f"CAST(({столбец} - {self.edges[0]:.3f}) / {self.шаг} AS INTEGER)"

    def момент(self, отрезок: Any) -> float:
        """Начало отрезка, секунды эпохи."""
        return self.edges[0] + int(отрезок) * self.шаг

    def номер(self, ключ: Any) -> int | None:
        """Корзина по номеру отрезка из базы или по местной дате «ГГГГ-ММ-ДД»."""
        if isinstance(ключ, str):
            return self._по_ключу.get(ключ)
        if ключ is None:
            return None
        момент = self.момент(ключ)
        if момент < self.edges[0] or момент >= self.edges[-1]:
            return None
        return bisect.bisect_right(self.edges, момент) - 1

    def __len__(self) -> int:
        return len(self.labels)


def _часы_переводят(с: float, до: float) -> bool:
    """Меняется ли смещение местного времени от UTC внутри [с, до)."""
    смещения = {time.localtime(момент).tm_gmtoff for момент in range(int(с), int(до), 86400)}
    смещения.add(time.localtime(max(с, до - 1)).tm_gmtoff)
    return len(смещения) > 1


def корзины(масштаб: str, первый: dt.date) -> Корзины:
    к = Корзины(масштаб, первый)
    следующий = сдвинуть(масштаб, первый, 1)
    if масштаб == "day":
        к.шаг = 3600
        for час in range(24):
            к.labels.append(f"{час:02d}:00")
            к.edges.append(_секунды(первый, час))
            к._по_ключу[f"{первый.isoformat()} {час:02d}"] = час
        к.edges.append(_секунды(следующий))
        return к
    день = первый
    if масштаб == "month":
        номер = 0
        while день < следующий:
            к.labels.append(f"{день.day}")
            к.edges.append(_секунды(день))
            к._по_ключу[день.isoformat()] = номер
            номер += 1
            день += dt.timedelta(days=1)
    elif масштаб == "quarter":
        номер = -1
        while день < следующий:
            if номер < 0 or день.weekday() == 0:
                номер += 1
                к.labels.append(f"{день.day} {МЕСЯЦЫ_КОРОТКО[день.month - 1]}")
                к.edges.append(_секунды(день))
            к._по_ключу[день.isoformat()] = номер
            день += dt.timedelta(days=1)
    else:
        for месяц in range(12):
            начало_месяца = dt.date(первый.year, месяц + 1, 1)
            к.labels.append(МЕСЯЦЫ_КОРОТКО[месяц])
            к.edges.append(_секунды(начало_месяца))
            конец = _плюс_месяцы(начало_месяца, 1)
            д = начало_месяца
            while д < конец:
                к._по_ключу[д.isoformat()] = месяц
                д += dt.timedelta(days=1)
    к.edges.append(_секунды(следующий))
    if _часы_переводят(к.edges[0], к.edges[-1]):
        к.шаг = 3600
    return к


# ---------------------------------------------------------------------------
# Отбор
# ---------------------------------------------------------------------------

#: Разговор отдела — добавочный из журнала звонков, отдел — из справочника
#: сотрудников по этому номеру, по его главной карточке (той, чьим именем
#: номер подписан в отчёте). Тот же отбор есть у списков раскрытия
#: (`department:` в `drill`), и список под числом сходится с числом.
ОТДЕЛ_ЗАДАНИЯ = Database.ОТДЕЛ_ЗАДАНИЯ
ДОБАВОЧНЫЙ_ЗАДАНИЯ = "EXISTS (SELECT 1 FROM calls cl WHERE cl.job_id = j.id AND cl.agent = ?)"
ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ = ("COALESCE((SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id "
                        "AND COALESCE(cl.agent,'') <> '' LIMIT 1), '')")
НАПРАВЛЕНИЕ_ВЫРАЖЕНИЕ = ("COALESCE(NULLIF((SELECT cl.direction FROM calls cl "
                         "WHERE cl.job_id = j.id LIMIT 1),''),'—')")


@dataclass
class Отбор:
    owner: Any = None
    agent: str = ""
    department: str = ""

    def где(self, db: Any, since: float, until: float, *,
            готовые: bool = False) -> tuple[str, list[Any]]:
        """Условие по заданиям окна: время, не контрольный прогон, владелец, отбор."""
        условия = ["j.created_at >= ?", "j.created_at < ?",
                   "COALESCE(j.source,'') <> 'control'"]
        аргументы: list[Any] = [since, until]
        if готовые:
            условия.append("j.status = 'completed'")
        своё, свои = db._owner_clause(self.owner, "j")
        if своё:
            условия.append(своё)
            аргументы.extend(свои)
        if self.agent:
            условия.append(ДОБАВОЧНЫЙ_ЗАДАНИЯ)
            аргументы.append(self.agent)
        if self.department:
            условия.append(ОТДЕЛ_ЗАДАНИЯ)
            аргументы.append(self.department)
        return " AND ".join(условия), аргументы

    def где_звонков(self, db: Any, since: float, until: float) -> tuple[str, list[Any]]:
        """То же для журнала звонков: время начала звонка, владелец, отбор."""
        условия = ["cl.started_at >= ?", "cl.started_at < ?"]
        аргументы: list[Any] = [since, until]
        своё, свои = db._owner_clause(self.owner, "cl")
        if своё:
            условия.append(своё)
            аргументы.extend(свои)
        if self.agent:
            условия.append("cl.agent = ?")
            аргументы.append(self.agent)
        if self.department:
            условия.append(Database.ОТДЕЛ_ЗВОНКА)
            аргументы.append(self.department)
        return " AND ".join(условия), аргументы

    def drill(self) -> list[str]:
        """Те же условия — отборами списка раскрытия (`GET /api/drill`)."""
        отборы = ["no_control"]
        if self.agent:
            отборы.append(f"agent:{self.agent}")
        if self.department:
            отборы.append(f"department:{self.department}")
        return отборы


# ---------------------------------------------------------------------------
# Счёт
# ---------------------------------------------------------------------------

def _доля(числитель: float, знаменатель: float) -> float | None:
    return round(числитель / знаменатель, 4) if знаменатель else None


def _сравнить_доли(а: int, n_а: int, б: int, n_б: int) -> dict[str, Any]:
    """Изменение доли а/n_а против б/n_б: пункты, z и «значимо ли»."""
    if not n_а or not n_б:
        return {"delta_pp": None, "z": None, "significant": False}
    p1, p2 = а / n_а, б / n_б
    общая = (а + б) / (n_а + n_б)
    разброс = math.sqrt(общая * (1 - общая) * (1 / n_а + 1 / n_б)) if 0 < общая < 1 else 0.0
    z = (p1 - p2) / разброс if разброс else 0.0
    return {"delta_pp": round((p1 - p2) * 100, 2), "z": round(z, 2),
            "significant": bool(abs(z) >= Z_ЗНАЧИМО and n_а >= МАЛО_ДЛЯ_ВЫВОДА
                                and n_б >= МАЛО_ДЛЯ_ВЫВОДА)}


def _изменение(сейчас: float | None, было: float | None) -> float | None:
    """Относительное изменение, доля: 0,08 — «+8 %». Без базы — None."""
    if сейчас is None or было is None or not было:
        return None
    return round((сейчас - было) / abs(было), 4)


class Отчёт:
    """Сводный отчёт за период. Создаётся на запрос и живёт один ответ."""

    def __init__(self, db: Any, масштаб: str, день: dt.date, отбор: Отбор, *,
                 сейчас: float | None = None) -> None:
        if масштаб not in МАСШТАБЫ:
            raise ConfigError(f"Неизвестный масштаб «{масштаб}».",
                              hint="Масштабы: " + ", ".join(МАСШТАБЫ))
        self.db = db
        self.масштаб = масштаб
        self.отбор = отбор
        self.сейчас = time.time() if сейчас is None else float(сейчас)
        первый = начало(масштаб, день)
        self.окно = окно(масштаб, первый)
        self.базы = базы(масштаб, первый)
        # Незакрытый период: конец — «сейчас», базы — на ту же дату.
        прошло = self.сейчас - self.окно.since
        self.идёт = self.окно.since <= self.сейчас < self.окно.end
        self.будущее = self.сейчас < self.окно.since
        if self.идёт:
            self.окно.until = self.сейчас
            for база in self.базы:
                база.until = min(база.end, база.since + прошло)
                база.to_date = True
        self.корзины = корзины(масштаб, первый)
        #: Все строки сотрудников периода и добавочные главной базы — после
        #: `посчитать` (см. там же).
        self.сотрудники: list[dict[str, Any]] = []
        self.сотрудники_базы: list[dict[str, Any]] = []

    # --- навигация ---------------------------------------------------------

    def навигация(self) -> dict[str, Any]:
        первый = self.окно.first
        следующий = сдвинуть(self.масштаб, первый, 1)
        return {
            "prev": сдвинуть(self.масштаб, первый, -1).isoformat(),
            "next": следующий.isoformat() if _секунды(следующий) <= self.сейчас else None,
            "today": начало(self.масштаб, dt.date.fromtimestamp(self.сейчас)).isoformat(),
        }

    # --- задания: объём и состав ---------------------------------------------

    def _корзины_окна(self, о: Окно) -> Корзины:
        return self.корзины if о.id == "current" else корзины(self.масштаб, о.first)

    def _задания(self, о: Окно) -> list[dict[str, Any]]:
        где, арг = self.отбор.где(self.db, о.since, о.until)
        корз = self._корзины_окна(о)
        return [dict(r) for r in self.db.query(
            f"SELECT {корз.выражение('j.created_at')} AS k, j.status AS s, COUNT(*) AS n, "
            "       SUM(COALESCE(j.media_duration_s, 0)) AS dur, "
            "       SUM(CASE WHEN j.media_duration_s > 0 THEN 1 ELSE 0 END) AS dn "
            f"FROM jobs j WHERE {где} GROUP BY k, s", арг)]

    @staticmethod
    def _известных_длительностей(группы: list[dict[str, Any]]) -> int:
        """Сколько готовых записей окна с известной длительностью.

        Переписка из текстовых каналов (`POST /api/jobs/text`) — готовая
        запись без длительности: в колонке у неё ноль. Медиана, 90-й
        процентиль и средняя длина разговора считаются по записям, у
        которых длина есть, — и номер строки медианы, и делитель средней.
        От всех готовых каждый чат шёл разговором в ноль секунд: на месяце
        с сотней звонков и сотней чатов медиана падала до самого короткого
        звонка, а средняя — вдвое.
        """
        return sum(int(г.get("dn") or 0) for г in группы if г["s"] == "completed")

    @staticmethod
    def _итог_заданий(группы: list[dict[str, Any]]) -> dict[str, Any]:
        по = {"completed": 0, "no_speech": 0, "short": 0, "failed": 0, "cancelled": 0}
        всего = длит = 0.0
        в_работе = 0
        for г in группы:
            n = int(г["n"] or 0)
            всего += n
            if г["s"] in по:
                по[г["s"]] += n
            else:
                в_работе += n
            if г["s"] == "completed":
                длит += float(г["dur"] or 0)
        с_длиной = Отчёт._известных_длительностей(группы)
        return {
            "jobs": int(всего), **по, "active": в_работе,
            "empty": по["no_speech"] + по["short"],
            "talk_hours": round(длит / 3600, 2),
            "avg_duration_s": round(длит / с_длиной, 1) if с_длиной else None,
            "empty_share": _доля(по["no_speech"] + по["short"], всего),
            "failed_share": _доля(по["failed"], всего),
        }

    def _ряд_заданий(self, группы: list[dict[str, Any]], база: Корзины | None = None
                     ) -> dict[str, list[Any]]:
        корз = база or self.корзины
        n = len(корз)
        ряд: dict[str, list[Any]] = {"completed": [0] * n, "empty": [0] * n,
                                     "failed": [0] * n, "no_speech": [0] * n,
                                     "short": [0] * n, "talk_hours": [0.0] * n}
        for г in группы:
            номер = корз.номер(г["k"])
            if номер is None:
                continue
            k = int(г["n"] or 0)
            if г["s"] == "completed":
                ряд["completed"][номер] += k
                ряд["talk_hours"][номер] += float(г["dur"] or 0) / 3600
            elif г["s"] in ("no_speech", "short"):
                ряд["empty"][номер] += k
                ряд[г["s"]][номер] += k
            elif г["s"] == "failed":
                ряд["failed"][номер] += k
        ряд["talk_hours"] = [round(ч, 2) for ч in ряд["talk_hours"]]
        return ряд

    def _медиана(self, о: Окно, готово: int) -> dict[str, Any]:
        """Медиана и 90-й процентиль длительности разговора — без выборки в память.

        `готово` — сколько готовых записей окна с известной длительностью:
        ровно столько строк отбирает запрос ниже, и номер строки медианы
        считается от них (см. `_известных_длительностей`).
        """
        if not готово:
            return {"median_duration_s": None, "p90_duration_s": None}
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        итог: dict[str, Any] = {}
        for имя, доля in (("median_duration_s", 0.5), ("p90_duration_s", 0.9)):
            строка = self.db.query_one(
                f"SELECT j.media_duration_s AS v FROM jobs j WHERE {где} "
                "AND j.media_duration_s > 0 ORDER BY j.media_duration_s "
                "LIMIT 1 OFFSET ?", [*арг, max(0, min(готово - 1, int(готово * доля)))])
            итог[имя] = round(float(строка["v"]), 1) if строка and строка["v"] is not None else None
        return итог

    # --- содержание разговора -------------------------------------------------

    def _содержание(self, о: Окно) -> dict[str, Any]:
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        показатели = ", ".join(f"{выр} AS {имя}" for имя, выр in self.db.CONTENT_METRICS)
        строка = self.db.query_one(
            f"SELECT {показатели} FROM content c JOIN jobs j ON j.id = c.job_id WHERE {где}",
            арг)
        с = dict(строка) if строка else {}
        оценено = int(с.get("scored") or 0)
        записей = int(с.get("records") or 0)
        с["negative_share"] = _доля(int(с.get("negative") or 0), оценено)
        с["positive_share"] = _доля(int(с.get("positive") or 0), оценено)
        с["neutral_share"] = (round(1 - (с["negative_share"] or 0) - (с["positive_share"] or 0), 4)
                              if оценено else None)
        с["repeat_share"] = _доля(int(с.get("repeat") or 0), записей)
        с["frustrated_share"] = _доля(int(с.get("frustrated") or 0), записей)
        с["profanity_share"] = _доля(int(с.get("profanity_records") or 0),
                                     int(с.get("profanity_checked") or 0))
        с["violation_share"] = _доля(int(с.get("violation_records") or 0), записей)
        с["long_monologue_share"] = _доля(int(с.get("long_monologues") or 0), записей)
        с["objections_unhandled_share"] = _доля(int(с.get("objections_unhandled") or 0),
                                                int(с.get("objections") or 0))
        nps_n = int(с.get("nps_checked") or 0)
        с["nps_index"] = (round(100.0 * (int(с.get("promoters") or 0)
                                         - int(с.get("detractors") or 0)) / nps_n, 1)
                          if nps_n else None)
        for имя, значение in list(с.items()):
            if isinstance(значение, float):
                с[имя] = round(значение, 4)
        return с

    def _ряд_содержания(self) -> dict[str, list[Any]]:
        о = self.окно
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        n = len(self.корзины)
        суммы = {имя: [0.0] * n for имя in (
            "records", "scored", "negative", "positive", "score_sum", "score_n",
            "compliance_sum", "compliance_n", "repeat", "frustrated")}
        for г in self.db.query(
                f"SELECT {self.корзины.выражение('j.created_at')} AS k, "
                "COUNT(*) AS records, "
                "SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
                f"SUM(CASE WHEN c.sentiment < {НЕГАТИВ} THEN 1 ELSE 0 END) AS negative, "
                f"SUM(CASE WHEN c.sentiment > {ПОЗИТИВ} THEN 1 ELSE 0 END) AS positive, "
                "SUM(COALESCE(c.agent_score, 0)) AS score_sum, COUNT(c.agent_score) AS score_n, "
                "SUM(COALESCE(c.compliance, 0)) AS compliance_sum, "
                "COUNT(c.compliance) AS compliance_n, "
                "SUM(CASE WHEN COALESCE(c.repeat_contact,0) > 0 THEN 1 ELSE 0 END) AS repeat, "
                "SUM(CASE WHEN COALESCE(c.frustration,0) > 0 THEN 1 ELSE 0 END) AS frustrated "
                f"FROM content c JOIN jobs j ON j.id = c.job_id WHERE {где} GROUP BY k", арг):
            номер = self.корзины.номер(г["k"])
            if номер is None:
                continue
            for имя in суммы:
                суммы[имя][номер] += float(г[имя] or 0)
        return {
            "records": [int(v) for v in суммы["records"]],
            "negative_share": [_доля(суммы["negative"][i], суммы["scored"][i]) for i in range(n)],
            "positive_share": [_доля(суммы["positive"][i], суммы["scored"][i]) for i in range(n)],
            "agent_score": [round(суммы["score_sum"][i] / суммы["score_n"][i], 1)
                            if суммы["score_n"][i] else None for i in range(n)],
            "compliance": [round(суммы["compliance_sum"][i] / суммы["compliance_n"][i], 4)
                           if суммы["compliance_n"][i] else None for i in range(n)],
            "repeat_share": [_доля(суммы["repeat"][i], суммы["records"][i]) for i in range(n)],
            "scored": [int(v) for v in суммы["scored"]],
            "negative": [int(v) for v in суммы["negative"]],
            "positive": [int(v) for v in суммы["positive"]],
        }

    # --- ответы модели ----------------------------------------------------------

    def _ответы(self, о: Окно) -> list[dict[str, Any]]:
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        return [dict(r) for r in self.db.query(
            "SELECT COALESCE(l.error,'') = '' AS ok, COALESCE(l.reason,'') AS reason, "
            "       COALESCE(l.outcome,'') AS outcome, COUNT(*) AS n, "
            "       SUM(CASE WHEN l.resolved IS NOT NULL THEN 1 ELSE 0 END) AS known, "
            "       SUM(CASE WHEN l.resolved THEN 1 ELSE 0 END) AS yes "
            f"FROM jobs j JOIN llm_results l ON l.job_id = j.id WHERE {где} "
            "GROUP BY 1, 2, 3", арг)]

    @staticmethod
    def _итог_ответов(группы: list[dict[str, Any]]) -> dict[str, Any]:
        годные = [г for г in группы if г["ok"]]
        разобрано = sum(int(г["n"]) for г in годные)
        известно = sum(int(г["known"] or 0) for г in годные)
        решено = sum(int(г["yes"] or 0) for г in годные)

        def счёт(поле: str) -> dict[str, int]:
            итог: dict[str, int] = {}
            for г in годные:
                if г[поле]:
                    итог[г[поле]] = итог.get(г[поле], 0) + int(г["n"])
            return итог

        пары: dict[tuple[str, str], int] = {}
        for г in годные:
            ключ = (str(г["reason"] or "—"), str(г["outcome"] or "—"))
            пары[ключ] = пары.get(ключ, 0) + int(г["n"])
        return {
            "analyzed": разобрано,
            "llm_errors": sum(int(г["n"]) for г in группы) - разобрано,
            "resolved_known": известно, "resolved_yes": решено,
            "resolved_share": _доля(решено, известно),
            "reasons": счёт("reason"), "outcomes": счёт("outcome"),
            "pairs": пары,
        }

    def _ряд_ответов(self) -> dict[str, list[Any]]:
        о = self.окно
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        n = len(self.корзины)
        разобрано, известно, решено = [0] * n, [0] * n, [0] * n
        for г in self.db.query(
                f"SELECT {self.корзины.выражение('j.created_at')} AS k, "
                "COUNT(*) AS n, "
                "SUM(CASE WHEN l.resolved IS NOT NULL THEN 1 ELSE 0 END) AS known, "
                "SUM(CASE WHEN l.resolved THEN 1 ELSE 0 END) AS yes "
                f"FROM jobs j JOIN llm_results l ON l.job_id = j.id WHERE {где} "
                "AND COALESCE(l.error,'') = '' GROUP BY k", арг):
            номер = self.корзины.номер(г["k"])
            if номер is None:
                continue
            разобрано[номер] += int(г["n"] or 0)
            известно[номер] += int(г["known"] or 0)
            решено[номер] += int(г["yes"] or 0)
        return {"analyzed": разобрано,
                "resolved_share": [_доля(решено[i], известно[i]) for i in range(n)]}

    def _ряды_причин(self, причины: list[str]) -> dict[str, list[int]]:
        """Сколько раз каждая из главных причин звучала в каждой корзине."""
        if not причины:
            return {}
        о = self.окно
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        места = ",".join("?" for _ in причины)
        n = len(self.корзины)
        ряды = {п: [0] * n for п in причины}
        for г in self.db.query(
                f"SELECT {self.корзины.выражение('j.created_at')} AS k, "
                "l.reason AS reason, COUNT(*) AS n "
                f"FROM jobs j JOIN llm_results l ON l.job_id = j.id WHERE {где} "
                f"AND COALESCE(l.error,'') = '' AND l.reason IN ({места}) GROUP BY k, reason",
                [*арг, *причины]):
            номер = self.корзины.номер(г["k"])
            if номер is not None and г["reason"] in ряды:
                ряды[г["reason"]][номер] += int(г["n"] or 0)
        return ряды

    # --- когда звонят ------------------------------------------------------------

    def _карта_недели(self) -> list[list[int]]:
        """Разговоры по дням недели (с понедельника) и часам."""
        о = self.окно
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        карта = [[0] * 24 for _ in range(7)]
        # Часами от начала окна (см. `Корзины`): день недели и час — питоном.
        for г in self.db.query(
                f"SELECT CAST((j.created_at - {о.since:.3f}) / 3600 AS INTEGER) AS k, "
                f"COUNT(*) AS n FROM jobs j WHERE {где} GROUP BY k", арг):
            местное = time.localtime(о.since + int(г["k"]) * 3600)
            карта[местное.tm_wday][местное.tm_hour] += int(г["n"] or 0)
        return карта

    def _календарь(self) -> list[dict[str, Any]]:
        """Разговоры по дням периода — для календарной карты квартала и года."""
        о = self.окно
        где, арг = self.отбор.где(self.db, о.since, о.until)
        дни: dict[str, dict[str, Any]] = {}
        корз = self.корзины
        for г in self.db.query(
                f"SELECT {корз.выражение('j.created_at')} AS k, "
                f"j.status AS s, COUNT(*) AS n FROM jobs j WHERE {где} GROUP BY k, s", арг):
            дата = time.strftime("%Y-%m-%d", time.localtime(корз.момент(г["k"])))
            if дата not in дни:
                # Границы дня — по часам сервера и в ответ: список под клеткой
                # календаря открывается ими, а не полуночью браузера — у того
                # бывает другой часовой пояс. Сегодняшний день кончается
                # концом окна отчёта, а не завтрашней полуночью.
                сутки = dt.date.fromisoformat(дата)
                дни[дата] = {"date": дата, "completed": 0, "jobs": 0,
                             "since": _секунды(сутки),
                             "until": min(_секунды(сутки + dt.timedelta(days=1)), о.until)}
            день = дни[дата]
            день["jobs"] += int(г["n"] or 0)
            if г["s"] == "completed":
                день["completed"] += int(г["n"] or 0)
        return sorted(дни.values(), key=lambda д: д["date"])

    # --- сотрудники и отделы ------------------------------------------------------

    def _по_добавочным(self, о: Окно) -> dict[str, dict[str, Any]]:
        где, арг = self.отбор.где(self.db, о.since, о.until)
        люди: dict[str, dict[str, Any]] = {}

        def строка(номер: str) -> dict[str, Any]:
            return люди.setdefault(номер, {
                "agent": номер, "jobs": 0, "completed": 0, "empty": 0, "failed": 0,
                "talk_s": 0.0, "scored": 0, "negative": 0, "score_sum": 0.0, "score_n": 0,
                "compliance_sum": 0.0, "compliance_n": 0, "analyzed": 0, "known": 0,
                "yes": 0, "reasons": {}})

        for г in self.db.query(
                f"SELECT {ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ} AS a, j.status AS s, COUNT(*) AS n, "
                "SUM(COALESCE(j.media_duration_s,0)) AS dur "
                f"FROM jobs j WHERE {где} GROUP BY a, s", арг):
            ч = строка(str(г["a"] or ""))
            n = int(г["n"] or 0)
            ч["jobs"] += n
            if г["s"] == "completed":
                ч["completed"] += n
                ч["talk_s"] += float(г["dur"] or 0)
            elif г["s"] in ("no_speech", "short"):
                ч["empty"] += n
            elif г["s"] == "failed":
                ч["failed"] += n
        где_г, арг_г = self.отбор.где(self.db, о.since, о.until, готовые=True)
        for г in self.db.query(
                f"SELECT {ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ} AS a, "
                "SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
                f"SUM(CASE WHEN c.sentiment < {НЕГАТИВ} THEN 1 ELSE 0 END) AS negative, "
                "SUM(COALESCE(c.agent_score,0)) AS score_sum, COUNT(c.agent_score) AS score_n, "
                "SUM(COALESCE(c.compliance,0)) AS compliance_sum, "
                "COUNT(c.compliance) AS compliance_n "
                f"FROM content c JOIN jobs j ON j.id = c.job_id WHERE {где_г} GROUP BY a",
                арг_г):
            ч = строка(str(г["a"] or ""))
            for имя in ("scored", "negative", "score_n", "compliance_n"):
                ч[имя] += int(г[имя] or 0)
            for имя in ("score_sum", "compliance_sum"):
                ч[имя] += float(г[имя] or 0)
        for г in self.db.query(
                f"SELECT {ДОБАВОЧНЫЙ_ВЫРАЖЕНИЕ} AS a, COALESCE(l.reason,'') AS reason, "
                "COUNT(*) AS n, SUM(CASE WHEN l.resolved IS NOT NULL THEN 1 ELSE 0 END) AS known, "
                "SUM(CASE WHEN l.resolved THEN 1 ELSE 0 END) AS yes "
                f"FROM jobs j JOIN llm_results l ON l.job_id = j.id WHERE {где_г} "
                "AND COALESCE(l.error,'') = '' GROUP BY a, reason", арг_г):
            ч = строка(str(г["a"] or ""))
            n = int(г["n"] or 0)
            ч["analyzed"] += n
            ч["known"] += int(г["known"] or 0)
            ч["yes"] += int(г["yes"] or 0)
            if г["reason"]:
                ч["reasons"][г["reason"]] = ч["reasons"].get(г["reason"], 0) + n
        return люди

    @staticmethod
    def _строка_сотрудника(ч: dict[str, Any], было: dict[str, Any] | None) -> dict[str, Any]:
        причины = sorted(ч["reasons"].items(), key=lambda п: (-п[1], п[0]))
        итог = {
            "agent": ч["agent"], "jobs": ч["jobs"], "completed": ч["completed"],
            "empty": ч["empty"], "failed": ч["failed"],
            "talk_hours": round(ч["talk_s"] / 3600, 2),
            "avg_duration_s": round(ч["talk_s"] / ч["completed"], 1) if ч["completed"] else None,
            "negative_share": _доля(ч["negative"], ч["scored"]),
            "agent_score": round(ч["score_sum"] / ч["score_n"], 1) if ч["score_n"] else None,
            "compliance": round(ч["compliance_sum"] / ч["compliance_n"], 4)
            if ч["compliance_n"] else None,
            "analyzed": ч["analyzed"], "resolved_share": _доля(ч["yes"], ч["known"]),
            "resolved_known": ч["known"], "resolved_yes": ч["yes"],
            "scored": ч["scored"], "negative": ч["negative"],
            "score_sum": round(ч["score_sum"], 2), "score_n": ч["score_n"],
            "top_reason": причины[0][0] if причины else "",
            "top_reason_n": причины[0][1] if причины else 0,
            "prev_completed": int((было or {}).get("completed") or 0),
            "prev_resolved_share": _доля(int((было or {}).get("yes") or 0),
                                         int((было or {}).get("known") or 0)),
        }
        итог["change"] = _изменение(float(итог["completed"]),
                                    float(итог["prev_completed"]) or None)
        return итог

    # --- журнал звонков ----------------------------------------------------------

    def _звонки(self, о: Окно) -> dict[str, Any]:
        где, арг = self.отбор.где_звонков(self.db, о.since, о.until)
        строка = self.db.query_one(
            "SELECT COUNT(*) AS calls, SUM(CASE WHEN cl.answered THEN 1 ELSE 0 END) AS answered, "
            "SUM(CASE WHEN NOT cl.answered AND cl.direction = 'входящий' THEN 1 ELSE 0 END) "
            "    AS missed, "
            "SUM(CASE WHEN cl.direction = 'входящий' THEN 1 ELSE 0 END) AS inbound, "
            "SUM(CASE WHEN cl.direction = 'исходящий' THEN 1 ELSE 0 END) AS outbound, "
            "SUM(CASE WHEN cl.direction = 'внутренний' THEN 1 ELSE 0 END) AS internal, "
            "SUM(CASE WHEN cl.answered AND cl.duration >= cl.billsec "
            "         THEN cl.duration - cl.billsec ELSE 0 END) AS wait_sum, "
            "SUM(CASE WHEN cl.answered AND cl.duration >= cl.billsec THEN 1 ELSE 0 END) "
            "    AS wait_n, "
            "SUM(COALESCE(cl.billsec,0)) AS talk_s "
            f"FROM calls cl WHERE {где}", арг)
        с = {к: (строка[к] if строка else 0) or 0 for к in (
            "calls", "answered", "missed", "inbound", "outbound", "internal", "wait_sum",
            "wait_n", "talk_s")}
        return {
            "calls": int(с["calls"]), "answered": int(с["answered"]),
            "missed": int(с["missed"]), "inbound": int(с["inbound"]),
            "outbound": int(с["outbound"]), "internal": int(с["internal"]),
            "missed_share": _доля(int(с["missed"]), int(с["inbound"])),
            "answer_share": _доля(int(с["answered"]), int(с["calls"])),
            "avg_wait_s": round(float(с["wait_sum"]) / int(с["wait_n"]), 1)
            if с["wait_n"] else None,
        }

    def _ряд_звонков(self) -> dict[str, list[int]]:
        о = self.окно
        где, арг = self.отбор.где_звонков(self.db, о.since, о.until)
        n = len(self.корзины)
        отвечено, пропущено, прочие = [0] * n, [0] * n, [0] * n
        for г in self.db.query(
                f"SELECT {self.корзины.выражение('cl.started_at')} AS k, "
                "SUM(CASE WHEN cl.answered THEN 1 ELSE 0 END) AS a, "
                "SUM(CASE WHEN NOT cl.answered AND cl.direction = 'входящий' THEN 1 ELSE 0 END) "
                "AS m, COUNT(*) AS n "
                f"FROM calls cl WHERE {где} GROUP BY k", арг):
            номер = self.корзины.номер(г["k"])
            if номер is None:
                continue
            отвечено[номер] += int(г["a"] or 0)
            пропущено[номер] += int(г["m"] or 0)
            прочие[номер] += int(г["n"] or 0) - int(г["a"] or 0) - int(г["m"] or 0)
        return {"answered": отвечено, "missed": пропущено, "other": прочие}

    def _направления(self, о: Окно) -> dict[str, int]:
        где, арг = self.отбор.где(self.db, о.since, о.until, готовые=True)
        return {str(г["d"]): int(г["n"] or 0) for г in self.db.query(
            f"SELECT {НАПРАВЛЕНИЕ_ВЫРАЖЕНИЕ} AS d, COUNT(*) AS n FROM jobs j WHERE {где} "
            "GROUP BY d", арг)}

    # --- сборка -----------------------------------------------------------------

    def посчитать(self) -> dict[str, Any]:
        """Счётная часть отчёта — всё, что считается группами без чтения ответов."""
        окна = [self.окно, *self.базы]
        if self.будущее:
            return {**self._шапка(), "future": True}
        # Запросы независимы — и уходят разом, а ответы собираются ниже. На
        # годовом отчёте по архиву в полмиллиона записей это вдвое быстрее
        # на двух ядрах (см. `ПОТОКОВ`).
        пул = _пул()
        ж_задания = {о.id: пул.submit(self._задания, о) for о in окна}
        ж_содержание = {о.id: пул.submit(self._содержание, о) for о in окна}
        ж_ответы = {о.id: пул.submit(self._ответы, о) for о in окна}
        ж_звонки = {о.id: пул.submit(self._звонки, о) for о in окна}
        ж_люди = пул.submit(self._по_добавочным, self.окно)
        ж_люди_было: Future[Any] | None = (
            пул.submit(self._по_добавочным, self.базы[0]) if self.базы else None)
        ж_ряд_содержания = пул.submit(self._ряд_содержания)
        ж_ряд_ответов = пул.submit(self._ряд_ответов)
        ж_ряд_звонков = пул.submit(self._ряд_звонков)
        ж_направления = {о.id: пул.submit(self._направления, о) for о in окна[:2]}
        ж_карта = пул.submit(self._карта_недели) if self.масштаб != "day" else None
        ж_календарь = (пул.submit(self._календарь)
                       if self.масштаб in ("quarter", "year") else None)

        задания = {о.id: ж_задания[о.id].result() for о in окна}
        итоги = {о.id: self._итог_заданий(задания[о.id]) for о in окна}
        ж_медианы = {о.id: пул.submit(self._медиана, о,
                                      self._известных_длительностей(задания[о.id]))
                     for о in окна[:2]}
        содержание = {о.id: ж_содержание[о.id].result() for о in окна}
        ответы = {о.id: self._итог_ответов(ж_ответы[о.id].result()) for о in окна}
        звонки = {о.id: ж_звонки[о.id].result() for о in окна}
        for ид, ж in ж_медианы.items():
            итоги[ид].update(ж.result())
        for о in окна:
            итоги[о.id]["content"] = содержание[о.id]
            итоги[о.id]["llm"] = {к: з for к, з in ответы[о.id].items()
                                  if к not in ("reasons", "outcomes", "pairs")}
            итоги[о.id]["llm"]["coverage"] = _доля(ответы[о.id]["analyzed"],
                                                   итоги[о.id]["completed"])
            итоги[о.id]["telephony"] = звонки[о.id]

        # Ряды: текущий период по корзинам и базы — «наложением» по номеру
        # корзины (час, день месяца, неделя квартала, месяц года).
        ряд = self._ряд_заданий(задания["current"])
        ряды_баз: dict[str, list[int]] = {}
        for б in self.базы:
            корз_б = корзины(self.масштаб, б.first)
            ряды_баз[б.id] = self._ряд_заданий(задания[б.id], корз_б)["completed"]

        главная = self.базы[0].id if self.базы else ""
        причины = self._распределение(ответы, "reasons", главная)
        исходы = self._распределение(ответы, "outcomes", главная)
        топ = [п["key"] for п in причины[:ПРИЧИН_В_РЯДАХ]]

        люди = ж_люди.result()
        люди_было = ж_люди_было.result() if ж_люди_было is not None else {}
        # Все добавочные периода и главной базы остаются при отчёте: свод по
        # отделам складывается из них, а не из показанных трёхсот. Иначе на
        # большом колл-центре отдел недосчитывал разговоров тех, кто не
        # попал в рейтинг, а «к базе» сравнивалось только с теми, кто
        # работал в обоих периодах, — и число в таблице отделов расходилось
        # со списком под ним.
        self.сотрудники = sorted(
            (self._строка_сотрудника(ч, люди_было.get(номер))
             for номер, ч in люди.items() if номер),
            key=lambda с: (-с["completed"], -с["jobs"], с["agent"]))
        self.сотрудники_базы = [{"agent": номер, "completed": int(ч.get("completed") or 0)}
                                for номер, ч in люди_было.items() if номер]
        сотрудники = self.сотрудники[:ДОБАВОЧНЫХ]
        без_добавочного = люди.get("", {})

        ответ = {
            **self._шапка(),
            "totals": итоги,
            "series": {
                "labels": self.корзины.labels, "edges": self.корзины.edges,
                **ряд, **ж_ряд_содержания.result(), **ж_ряд_ответов.result(),
                "telephony": ж_ряд_звонков.result() if звонки["current"]["calls"] else None,
                "baselines": ряды_баз,
            },
            "reasons": причины, "outcomes": исходы,
            "pairs": [{"reason": п, "outcome": и, "records": n} for (п, и), n in sorted(
                ответы["current"]["pairs"].items(), key=lambda пара: (-пара[1], пара[0]))],
            "reason_series": self._ряды_причин(топ),
            "agents": сотрудники,
            "agents_total": sum(1 for номер in люди if номер),
            "without_agent": {"jobs": int(без_добавочного.get("jobs") or 0),
                              "completed": int(без_добавочного.get("completed") or 0)},
            "directions": {ид: ж.result() for ид, ж in ж_направления.items()},
        }
        if ж_карта is not None:
            ответ["week_map"] = ж_карта.result()
        if ж_календарь is not None:
            ответ["calendar"] = ж_календарь.result()
        return ответ

    def _шапка(self) -> dict[str, Any]:
        прошло = None
        if self.идёт:
            прошло = round((self.сейчас - self.окно.since) / (self.окно.end - self.окно.since), 4)
        return {
            "scale": self.масштаб, "label": self.окно.label,
            "window": {"since": self.окно.since, "until": self.окно.until},
            "period": self.окно.to_dict(),
            "baselines": [б.to_dict() for б in self.базы],
            "complete": not self.идёт and not self.будущее,
            "elapsed": прошло, "nav": self.навигация(),
            "filter": {"agent": self.отбор.agent, "department": self.отбор.department},
            "drill": self.отбор.drill(),
            "generated_at": self.сейчас,
        }

    @staticmethod
    def _распределение(ответы: dict[str, dict[str, Any]], поле: str,
                       база: str) -> list[dict[str, Any]]:
        """Причины или исходы: сейчас, в базе, изменение доли и его значимость."""
        сейчас = ответы["current"][поле]
        было = ответы.get(база, {}).get(поле, {}) if база else {}
        всего = sum(сейчас.values())
        всего_было = sum(было.values())
        ключи = set(сейчас) | set(было)
        строки = []
        for ключ in ключи:
            n, n_было = сейчас.get(ключ, 0), было.get(ключ, 0)
            строки.append({
                "key": ключ, "records": n, "share": _доля(n, всего),
                "prev_records": n_было, "prev_share": _доля(n_было, всего_было),
                **_сравнить_доли(n, всего, n_было, всего_было),
            })
        строки.sort(key=lambda с: (-с["records"], -с["prev_records"], с["key"]))
        накоплено = 0
        for с in строки:
            накоплено += с["records"]
            с["cumulative_share"] = _доля(накоплено, всего)
        return строки


# ---------------------------------------------------------------------------
# Вторая часть: то, что лежит в ответах модели списками
# ---------------------------------------------------------------------------

def списки(db: Any, отчёт: Отчёт) -> dict[str, Any]:
    """Трекеры, скоркарта, обязательства — за период и за главную базу.

    Читается каждый ответ модели окна, поэтому это отдельная часть: отчёт
    рисуется без неё и дорисовывает её, когда она посчитается.
    """
    def свод(о: Окно) -> dict[str, Any]:
        где, арг = отчёт.отбор.где(db, о.since, о.until, готовые=True)
        трекеры: dict[str, dict[str, Any]] = {}
        пункты: dict[str, dict[str, Any]] = {}
        обязательств = с_обязательствами = записей = 0
        import json  # noqa: PLC0415

        память: dict[str, Any] = {}

        def разобрать(сырое: Any) -> Any:
            if not isinstance(сырое, str) or сырое == "[]":
                return [] if сырое == "[]" else сырое
            if сырое not in память:
                try:
                    память[сырое] = json.loads(сырое)
                except (TypeError, ValueError):
                    память[сырое] = None
                if len(память) > 4096:
                    память.clear()
            return память.get(сырое)

        for r in db.query(
                "SELECT l.actions, l.trackers, l.scorecard FROM jobs j "
                f"JOIN llm_results l ON l.job_id = j.id WHERE {где} "
                "AND COALESCE(l.error,'') = ''", арг):
            записей += 1
            действия = разобрать(r["actions"]) or []
            if isinstance(действия, list) and действия:
                обязательств += len(действия)
                с_обязательствами += 1
            # Запись считается один раз на трекер и на ответ пункта — как в
            # «Голосовой аналитике» и как отбирает список под числом: трекер
            # сработал, если сработал любой из его повторов (два трекера с
            # одним id в настройке), пункт «не выполнен», если так ответил
            # любой. Прежде решал первый повтор, и при «не сработал,
            # сработал» число говорило ноль, а список показывал запись.
            свои_трекеры: dict[str, bool] = {}
            for т in разобрать(r["trackers"]) or []:
                if not isinstance(т, dict):
                    continue
                ключ = str(т.get("id") or т.get("label") or "")
                if not ключ:
                    continue
                трекеры.setdefault(ключ, {"id": ключ, "label": т.get("label") or ключ,
                                          "checked": 0, "fired": 0})
                свои_трекеры[ключ] = свои_трекеры.get(ключ, False) or bool(т.get("fired"))
            for ключ, сработал in свои_трекеры.items():
                трекеры[ключ]["checked"] += 1
                if сработал:
                    трекеры[ключ]["fired"] += 1
            свои_ответы: set[tuple[str, str]] = set()
            for п in разобрать(r["scorecard"]) or []:
                if not isinstance(п, dict):
                    continue
                ключ = str(п.get("id") or п.get("question") or "")
                if not ключ:
                    continue
                пункты.setdefault(ключ, {"id": ключ, "question": п.get("question") or ключ,
                                         "yes": 0, "no": 0, "na": 0})
                ответ = п.get("answer")
                свои_ответы.add((ключ, "yes" if ответ == "да" else "no" if ответ == "нет" else "na"))
            for ключ, вид in свои_ответы:
                пункты[ключ][вид] += 1
        return {"records": записей, "actions": обязательств,
                "records_with_actions": с_обязательствами,
                "trackers": трекеры, "scorecard": пункты}

    # Период и база — по очереди, а не рядом: здесь время уходит на разбор
    # JSON питоном, а он держит GIL, и два потока вместе шли медленнее одного.
    сейчас = свод(отчёт.окно)
    база = свод(отчёт.базы[0]) if отчёт.базы else {"records": 0, "trackers": {},
                                                   "scorecard": {}, "actions": 0,
                                                   "records_with_actions": 0}
    трекеры = []
    for ключ, т in сейчас["trackers"].items():
        б = база["trackers"].get(ключ, {})
        трекеры.append({**т, "share": _доля(т["fired"], т["checked"]),
                        "prev_fired": int(б.get("fired") or 0),
                        "prev_checked": int(б.get("checked") or 0),
                        "prev_share": _доля(int(б.get("fired") or 0), int(б.get("checked") or 0)),
                        **_сравнить_доли(т["fired"], т["checked"], int(б.get("fired") or 0),
                                         int(б.get("checked") or 0))})
    трекеры.sort(key=lambda т: (-т["fired"], т["label"]))
    пункты = []
    for ключ, п in сейчас["scorecard"].items():
        б = база["scorecard"].get(ключ, {})
        оценено, оценено_было = п["yes"] + п["no"], int(б.get("yes") or 0) + int(б.get("no") or 0)
        пункты.append({**п, "pass_share": _доля(п["yes"], оценено),
                       "prev_pass_share": _доля(int(б.get("yes") or 0), оценено_было),
                       **_сравнить_доли(п["yes"], оценено, int(б.get("yes") or 0), оценено_было)})
    пункты.sort(key=lambda п: (п["pass_share"] if п["pass_share"] is not None else 2, п["question"]))
    return {
        "records": сейчас["records"], "prev_records": база["records"],
        "actions": сейчас["actions"], "prev_actions": база["actions"],
        "records_with_actions": сейчас["records_with_actions"],
        "prev_records_with_actions": база["records_with_actions"],
        "trackers": трекеры, "scorecard": пункты,
    }


# ---------------------------------------------------------------------------
# Коротко о периоде
# ---------------------------------------------------------------------------

def _число(n: float | int | None) -> str:
    if n is None:
        return "—"
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.1f}".replace(",", " ").replace(".", ",")
    return f"{int(n):,}".replace(",", " ")


def _проценты(доля: float | None, знаков: int = 0) -> str:
    if доля is None:
        return "—"
    return f"{доля * 100:.{знаков}f}".replace(".", ",") + " %"


def _сдвиг(изменение: float | None) -> str:
    if изменение is None:
        return ""
    знак = "+" if изменение > 0 else "−" if изменение < 0 else "±"
    return f"{знак}{abs(изменение) * 100:.0f} %".replace(".", ",")


def _пункты(дельта: float | None) -> str:
    if дельта is None:
        return ""
    знак = "+" if дельта > 0 else "−" if дельта < 0 else "±"
    return f"{знак}{abs(дельта):.1f}".replace(".", ",") + " п. п."


def коротко(отчёт: dict[str, Any]) -> list[str]:
    """Несколько фраз о периоде — то, что читают первым, до графиков.

    Фразы собираются из тех же чисел, что стоят на плитках, без языковой
    модели: такой пересказ не выдумывает и не опаздывает. Каждая фраза — о
    том, что изменилось заметно; спокойное «как обычно» не повторяется.
    """
    итоги = отчёт.get("totals") or {}
    т = итоги.get("current") or {}
    базы = отчёт.get("baselines") or []
    б = итоги.get(базы[0]["id"]) if базы else None
    имя_базы = базы[0]["label"] if базы else ""
    фразы: list[str] = []
    if not т.get("jobs"):
        return [f"За {отчёт.get('label')} записей нет."]
    состав = []
    if т.get("no_speech"):
        состав.append(f"без речи {_число(т['no_speech'])}")
    if т.get("short"):
        состав.append(f"коротких {_число(т['short'])}")
    if т.get("failed"):
        состав.append(f"с ошибкой {_число(т['failed'])}")
    фраза = (f"Разговоров с речью — {_число(т.get('completed'))}"
             + (f" из {_число(т.get('jobs'))} записей ({', '.join(состав)})" if состав else ""))
    if б and б.get("completed"):
        фраза += f"; {_сдвиг(_изменение(float(т.get('completed') or 0), float(б['completed'])))} " \
                 f"к периоду «{имя_базы}»"
    фразы.append(фраза + ".")
    if т.get("talk_hours"):
        фразы.append(f"Наговорено {_число(т['talk_hours'])} ч, медиана разговора — "
                     f"{_число(т.get('median_duration_s'))} с.")
    llm, llm_б = т.get("llm") or {}, (б or {}).get("llm") or {}
    if llm.get("resolved_share") is not None:
        дельта = _сравнить_доли(int(llm.get("resolved_yes") or 0), int(llm.get("resolved_known") or 0),
                                int(llm_б.get("resolved_yes") or 0),
                                int(llm_б.get("resolved_known") or 0))
        фразы.append(f"Вопрос решён при обращении в {_проценты(llm['resolved_share'])} "
                     f"разговоров" + (f" ({_пункты(дельта['delta_pp'])}"
                                      f"{', значимо' if дельта['significant'] else ''})"
                                      if дельта["delta_pp"] is not None else "") + ".")
    if llm.get("coverage") is not None and llm["coverage"] < 0.9 and т.get("completed"):
        фразы.append(f"Модель разобрала {_проценты(llm['coverage'])} разговоров — причины, "
                     "исходы и доля решённых пока предварительные.")
    с, с_б = т.get("content") or {}, (б or {}).get("content") or {}
    if с.get("negative_share") is not None:
        дельта = _сравнить_доли(int(с.get("negative") or 0), int(с.get("scored") or 0),
                                int(с_б.get("negative") or 0), int(с_б.get("scored") or 0))
        фразы.append(f"Отрицательная тональность — {_проценты(с['negative_share'])}"
                     + (f" ({_пункты(дельта['delta_pp'])}"
                        f"{', значимо' if дельта['significant'] else ''})"
                        if дельта["delta_pp"] is not None else "") + ".")
    причины = отчёт.get("reasons") or []
    if причины and причины[0]["records"]:
        фразы.append(f"Чаще всего звонили по причине «{причины[0]['key']}» — "
                     f"{_проценты(причины[0]['share'])}.")
        растут = [п for п in причины if п.get("significant") and (п.get("delta_pp") or 0) > 0]
        падают = [п for п in причины if п.get("significant") and (п.get("delta_pp") or 0) < 0]
        if растут:
            п = max(растут, key=lambda x: x["delta_pp"])
            фразы.append(f"Заметнее всего выросла причина «{п['key']}»: "
                         f"{_проценты(п['prev_share'])} → {_проценты(п['share'])}.")
        if падают:
            п = min(падают, key=lambda x: x["delta_pp"])
            фразы.append(f"Реже стали звонить по причине «{п['key']}»: "
                         f"{_проценты(п['prev_share'])} → {_проценты(п['share'])}.")
    сотрудники = [с for с in (отчёт.get("agents") or []) if с.get("completed")]
    if сотрудники:
        первый = сотрудники[0]
        кто = первый.get("agent_name") or f"добавочный {первый['agent']}"
        фразы.append(f"Больше всех разговоров — {кто}: {_число(первый['completed'])}.")
    тел = т.get("telephony") or {}
    if тел.get("calls") and тел.get("missed_share") is not None and тел.get("inbound"):
        фразы.append(f"Пропущено {_проценты(тел['missed_share'])} входящих звонков"
                     + (f", среднее ожидание ответа — {_число(тел.get('avg_wait_s'))} с"
                        if тел.get("avg_wait_s") is not None else "") + ".")
    if not отчёт.get("complete"):
        фразы.append("Период ещё идёт: сравнение — с тем же отрезком от начала базы.")
    return фразы


__all__ = ["МАСШТАБЫ", "Отбор", "Отчёт", "базы", "корзины", "коротко", "начало",
           "подпись", "разобрать_дату", "сдвинуть", "списки"]
