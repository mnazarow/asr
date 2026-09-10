"""Свод по корпусу записей: разрезы, темы, связи и выводы.

Раздел «Аналитика» отвечает на вопросы о сервере: сколько заданий, с какой
скоростью, что падало. Здесь — о самих разговорах: какими они были, чем
отличаются друг от друга и что из этого следует.

Три уровня, и они разной природы:

* Разрезы — арифметика. Средняя оценка по оператору, доля отрицательных по
  метке, темп речи по дням недели. Тут ошибиться можно только в коде, и
  считает их база: свод за месяц на оживлённом сервере — сотня тысяч
  записей, а поднимать их в память ради дюжины средних значит либо ждать
  секунды, либо резать выборку и показывать часть архива как целое.
* Связи — статистика. Коэффициент корреляции посчитается для любых двух
  колонок, даже когда за ним ничего не стоит, поэтому пары перечислены
  руками, каждая со своей формулировкой, и слабые связи не показываются.
* Выводы — правила. Не модель и не «искусственный интеллект»: набор
  проверок вида «доля отрицательных выросла в полтора раза». Каждая знает
  свой порог и своё сравнение и называет числа, из которых сделана, чтобы
  вывод можно было проверить, а не поверить ему.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Any

from . import content, stats
from .analytics import PERIODS
from .db import Database
from .logging_setup import get_logger

log = get_logger("insights")

#: Порог «отрицательной» и «положительной» записи. Тот же, что у разбора
#: одной записи, и тот же, что зашит в выражения свода: три разные границы в
#: одном разделе — это три разных ответа на вопрос «сколько было недовольных».
ПОРОГ = 0.15

#: Сколько записей должно быть в группе, чтобы показывать её отдельно.
#: Оператор с двумя разговорами всегда либо лучший, либо худший, и оба раза
#: это ничего не значит.
МИН_ГРУППА = 5

#: В скольких записях должно встретиться слово, которого раньше не было,
#: чтобы назвать его новым: из одной — это ошибка распознавания.
МИН_НОВОГО = 3

#: Сколько записей должно быть в окне, чтобы делать выводы по корпусу.
#: Отдельно от МИН_ГРУППА, потому что это другой вопрос: там «можно ли
#: сравнивать группу с остальными», здесь «есть ли о чём говорить вообще».
МИН_КОРПУСА = 10

#: Драйверы негатива: категория попадает в список, когда среди отрицательных
#: разговоров она встречается заметно чаще, чем вообще, — подъём (lift) от
#: 1,25, — и на достаточном числе записей. Десять — тот же порог, что у
#: выводов по группам: на трёх записях подъём в два раза — это одна запись.
МИН_ДРАЙВЕРА = 10
ПОДЪЁМ = 1.25

#: Базовое окно для норм и контрольных карт — четыре недели до периода,
#: как у Deepgram (1500–2500 записей за четыре недели — обычная база).
#: Пороги из чужих методик — ориентир; своя медиана за четыре недели —
#: норма.
БАЗА_ДНЕЙ = 28

#: Признаки, по которым считается норма: числовые показатели записи, у
#: которых есть значение в каждой записи (не суммы за период).
ПРИЗНАКИ_НОРМЫ: tuple[str, ...] = (
    "sentiment", "wpm", "silence_share", "interruptions", "pauses", "longest_pause_s",
    "filler_rate", "compliance", "duration_s", "talk_share", "monologue_s",
    "customer_story_s", "switches", "reply_delay_s", "overlap_s", "dead_air_s",
    "tempo_ratio", "agent_score", "empathy",
)

#: Ряды для контрольных карт содержания: ключ корзины, подпись, куда лучше.
КАРТЫ: tuple[tuple[str, str, int], ...] = (
    ("negative_share", "Доля отрицательных, %", -1),
    ("agent_score", "Балл оператора", 1),
    ("sentiment", "Тональность", 1),
    ("compliance", "Скрипт", 1),
)

#: Сколько записей берётся на расчёт связей. Коэффициент по двадцати тысячам
#: отличается от коэффициента по ста тысячам в третьем знаке — при пороге в
#: две десятых это не имеет значения.
ПРЕДЕЛ_СВЯЗЕЙ = 20000

#: Сколько групп запрашивать у базы для упорядоченного разреза. Часов
#: двадцать четыре, дней недели семь, а календарных дней за год — триста
#: шестьдесят пять; берём с запасом, чтобы ни один не выпал.
ПРЕДЕЛ_УПОРЯДОЧЕННЫХ = 500

#: Условия для связи: меньше пятидесяти пар или слабее 0.2 — не показываем.
#: Не потому, что связи нет, а потому, что отличить её от случайности на
#: таких числах нельзя, а показанная цифра будет прочитана как факт.
МИН_ПАР = 50
МИН_СВЯЗЬ = 0.2

#: Числовые признаки записи: ключ, название, единица, направление «больше —
#: это лучше» (1 — лучше, -1 — хуже, 0 — само по себе никак). Направление
#: нужно выводам: без него «темп речи вырос» — ни хорошо ни плохо, и писать
#: про это нечего.
ПРИЗНАКИ: list[dict[str, Any]] = [
    {"key": "sentiment", "title": "Тональность", "unit": "", "good": 1, "digits": 2},
    {"key": "sentiment_shift", "title": "Разворот тональности", "unit": "",
     "good": 1, "digits": 2},
    {"key": "wpm", "title": "Темп речи", "unit": "слов/мин", "good": 0, "digits": 0},
    {"key": "silence_share", "title": "Доля тишины", "unit": "", "good": -1,
     "digits": 3},
    {"key": "interruptions", "title": "Перебивания", "unit": "", "good": -1,
     "digits": 1},
    {"key": "pauses", "title": "Долгие паузы", "unit": "", "good": -1, "digits": 1},
    {"key": "longest_pause_s", "title": "Самая долгая пауза", "unit": "с",
     "good": -1, "digits": 1},
    {"key": "filler_rate", "title": "Слова-паразиты", "unit": "доля", "good": -1,
     "digits": 4},
    # Вопросы и обещания в своде — это суммы по всем записям периода, а не
    # средние на запись: «1027 вопросов» отвечает на вопрос, ради которого
    # разрез и открывают, а «1.04 вопроса на разговор» — ни на какой.
    # Поэтому и знаков после запятой у них ноль.
    {"key": "questions", "title": "Вопросов всего", "unit": "", "good": 0,
     "digits": 0},
    {"key": "commitments", "title": "Обещаний всего", "unit": "", "good": 0,
     "digits": 0},
    {"key": "alerts", "title": "Тревожные упоминания", "unit": "", "good": -1,
     "digits": 2},
    {"key": "compliance", "title": "Скрипт разговора", "unit": "доля", "good": 1,
     "digits": 2},
    {"key": "speakers", "title": "Говорящих", "unit": "", "good": 0, "digits": 1},
    {"key": "duration_s", "title": "Длительность", "unit": "с", "good": 0,
     "digits": 0},
    # Стороны разговора. Ориентиры («hint») — из опубликованной практики
    # Gong, Avoma, Genesys и Fireflies, и все они про английские продажи,
    # а не про русскую поддержку. Поэтому это подсказка рядом с числом, а
    # не порог: порогом должна стать своя обычная величина.
    {"key": "talk_share", "title": "Доля речи оператора", "unit": "доля",
     "good": 0, "digits": 2,
     "hint": "ориентир 0,40–0,60 (Avoma); у Gong для продаж — 0,43"},
    {"key": "monologue_s", "title": "Самый долгий монолог оператора", "unit": "с",
     "good": -1, "digits": 0,
     "hint": "ориентир до 150 с (Gong); Fireflies считает монологом от 90 с"},
    {"key": "customer_story_s", "title": "Самый долгий рассказ клиента",
     "unit": "с", "good": 1, "digits": 0,
     "hint": "чем дольше, тем полнее клиент высказался (Gong)"},
    {"key": "switches", "title": "Смен говорящего", "unit": "", "good": 1,
     "digits": 1, "hint": "ориентир от 5 за разговор (Gong)"},
    {"key": "reply_delay_s", "title": "Пауза оператора перед ответом",
     "unit": "с", "good": 0, "digits": 2,
     "hint": "ориентир 0,6–1,0 с (Gong); паузы от 2 с считаются тишиной"},
    {"key": "overlap_s", "title": "Наложение речи", "unit": "с", "good": -1,
     "digits": 1, "hint": "сколько секунд говорили одновременно"},
    {"key": "dead_air_s", "title": "Заметная тишина", "unit": "с", "good": -1,
     "digits": 1, "hint": "сумма пауз от 3 с (порог NICE и Amazon Contact Lens)"},
    {"key": "tempo_ratio", "title": "Темп оператора к темпу клиента", "unit": "",
     "good": 0, "digits": 2,
     "hint": "около 1 — подстраивается под собеседника (UIS)"},
    # Оценка оператора. Балл — как у Verint и Google: веса выполненных
    # пунктов скрипта к сумме всех, минус штрафы категорий; индекс эмпатии
    # — по формуле Genesys: (вежливых − невежливых) ÷ сумму.
    {"key": "agent_score", "title": "Балл оператора", "unit": "из 100",
     "good": 1, "digits": 0,
     "hint": "скрипт с весами минус штрафы стоп-слов (Verint, Google Quality AI)"},
    {"key": "empathy", "title": "Индекс эмпатии", "unit": "от −100 до +100",
     "good": 1, "digits": 0,
     "hint": "(вежливых − невежливых) ÷ сумму по репликам оператора (Genesys)"},
    {"key": "named_share", "title": "Обратился по имени", "unit": "% записей",
     "good": 1, "digits": 1,
     "hint": "доля записей, где оператор назвал клиента по имени; по словарю имён"},
]
ПРИЗНАКИ_ПО_КЛЮЧУ = {п["key"]: п for п in ПРИЗНАКИ}

#: Пары для проверки связи — перечислены руками. Автоматический перебор всех
#: пар из дюжины признаков даёт шестьдесят шесть коэффициентов, из которых
#: три-четыре пройдут любой порог просто по случайности, и раздел начнёт
#: сообщать открытия на пустом месте.
ПАРЫ: list[tuple[str, str, str]] = [
    ("wpm", "sentiment", "Чем быстрее речь, тем {} оценка разговора"),
    ("interruptions", "sentiment", "Чем больше перебиваний, тем {} оценка"),
    ("silence_share", "sentiment", "Чем больше молчания, тем {} оценка"),
    ("longest_pause_s", "sentiment", "Чем дольше паузы, тем {} оценка"),
    ("filler_rate", "sentiment", "Чем больше слов-паразитов, тем {} оценка"),
    ("compliance", "sentiment", "Чем полнее соблюдён скрипт, тем {} оценка"),
    ("duration_s", "sentiment", "Чем длиннее разговор, тем {} оценка"),
    ("questions", "commitments", "Чем больше вопросов, тем {} обещаний"),
    ("compliance", "commitments", "Чем полнее скрипт, тем {} обещаний"),
    ("duration_s", "interruptions", "Чем длиннее разговор, тем {} перебиваний"),
    ("speakers", "interruptions", "Чем больше говорящих, тем {} перебиваний"),
    ("talk_share", "sentiment", "Чем больше говорит оператор, тем {} оценка"),
    ("monologue_s", "sentiment", "Чем длиннее монологи оператора, тем {} оценка"),
    ("customer_story_s", "sentiment", "Чем дольше клиенту дают говорить, тем {} оценка"),
    ("dead_air_s", "sentiment", "Чем больше заметной тишины, тем {} оценка"),
    ("switches", "sentiment", "Чем живее разговор (смены говорящего), тем {} оценка"),
    ("agent_score", "sentiment", "Чем выше балл оператора, тем {} оценка"),
    ("empathy", "sentiment", "Чем выше индекс эмпатии, тем {} оценка"),
]

#: Разрезы: ключ группировки в базе -> название и особенности показа.
РАЗРЕЗЫ: dict[str, dict[str, Any]] = {
    "owner": {"title": "Владелец"},
    "speaker": {"title": "Оператор"},
    "tag": {"title": "Метка", "multi": True},
    # Запись про оплату и доставку входит в обе группы: суммы по разрезу
    # больше числа записей, и это не ошибка — так же, как у меток.
    "category": {"title": "Категория обращения"},
    "model": {"title": "Модель"},
    "engine": {"title": "Движок"},
    "language": {"title": "Язык"},
    "source": {"title": "Источник"},
    "label": {"title": "Тональность"},
    "weekday": {"title": "День недели", "ordered": True},
    "hour": {"title": "Час", "ordered": True},
    "date": {"title": "День", "ordered": True},
}

_ДНИ = ("воскресенье", "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота")

#: Разрезы, по которым делаются выводы, и как их называть в тексте. Один
#: перечень и для расчёта, и для перебора — чтобы выводы на экране и в
#: выгрузке были одни и те же.
РАЗРЕЗЫ_ВЫВОДОВ: dict[str, str] = {
    "owner": "владельца", "speaker": "оператора", "tag": "метки",
    "category": "категории",
}

#: Разрезы, которые выводам нужны целиком: три сравниваемых с общим средним
#: плюс часы — у них своё правило («тяжелее всего разговоры идут в…»), и
#: сравнивать каждый час с общим средним отдельно значило бы дать двадцать
#: четыре вывода вместо одного.
РАЗРЕЗЫ_ДЛЯ_ВЫВОДОВ = (*РАЗРЕЗЫ_ВЫВОДОВ, "hour")

#: Отборы для списка «что послушать». Каждый — ответ на вопрос, с которым в
#: раздел и приходят, а не просто сортировка по колонке. Выражения SQL здесь
#: постоянные: снаружи приходит только ключ, и он сверяется с этим перечнем.
ОТБОРЫ: dict[str, dict[str, str]] = {
    "negative": {"title": "Самые тяжёлые разговоры",
                 "order": "c.sentiment ASC",
                 "where": "c.sentiment IS NOT NULL"},
    "downturn": {"title": "Начались хорошо, кончились плохо",
                 "order": "c.sentiment_shift ASC",
                 "where": "c.sentiment_shift < -0.2"},
    "recovered": {"title": "Начались плохо, кончились хорошо",
                  "order": "c.sentiment_shift DESC",
                  "where": "c.sentiment_shift > 0.2"},
    "alerts": {"title": "С тревожными упоминаниями",
               "order": "c.alerts DESC, c.sentiment ASC",
               "where": "COALESCE(c.alerts,0) > 0"},
    "open_commitments": {"title": "Обещания без названного срока",
                         "order": "(c.commitments - c.commitments_dated) DESC",
                         "where": "c.commitments > COALESCE(c.commitments_dated,0)"},
    "interruptions": {"title": "Больше всего перебиваний",
                      "order": "c.interruptions DESC",
                      "where": "COALESCE(c.interruptions,0) > 0"},
    "silence": {"title": "Больше всего молчания",
                "order": "c.silence_share DESC",
                "where": "c.silence_share > 0.3"},
    "script": {"title": "Скрипт соблюдён хуже всего",
               "order": "c.compliance ASC",
               "where": "c.compliance IS NOT NULL"},
    "fillers": {"title": "Больше всего слов-паразитов",
                "order": "c.filler_rate DESC",
                "where": "COALESCE(c.filler_rate,0) > 0"},
    "money": {"title": "Самые крупные суммы",
              "order": "c.money_max DESC",
              "where": "c.money_max IS NOT NULL"},
    "pauses": {"title": "Самые долгие паузы",
               "order": "c.longest_pause_s DESC",
               "where": "COALESCE(c.longest_pause_s,0) > 0"},
    "fast": {"title": "Самая быстрая речь",
             "order": "c.wpm DESC", "where": "c.wpm IS NOT NULL"},
    "monologue": {"title": "Самые долгие монологи оператора",
                  "order": "c.monologue_s DESC",
                  "where": "c.monologue_s IS NOT NULL"},
    "customer_story": {"title": "Самый долгий рассказ клиента",
                       "order": "c.customer_story_s DESC",
                       "where": "c.customer_story_s IS NOT NULL"},
    "mixed": {"title": "Противоречивые разговоры",
              "order": "(c.negative_segments + c.positive_segments) DESC",
              "where": "c.sentiment_label = 'смешанная'"},
    "dead_air": {"title": "Больше всего заметной тишины",
                 "order": "c.dead_air_s DESC",
                 "where": "COALESCE(c.dead_air_s,0) > 0"},
    "impatient": {"title": "Самые короткие паузы перед ответом",
                  "order": "c.reply_delay_s ASC",
                  "where": "c.reply_delay_s IS NOT NULL"},
    "frustrated": {"title": "Клиент раздражён",
                   "order": "c.frustration DESC, c.sentiment ASC",
                   "where": "COALESCE(c.frustration,0) > 0"},
    "repeat": {"title": "Повторные обращения",
               "order": "c.repeat_contact DESC, c.sentiment ASC",
               "where": "COALESCE(c.repeat_contact,0) > 0"},
    "profanity_agent": {"title": "Нецензурная лексика у сотрудника",
                        "order": "c.profanity_agent DESC",
                        "where": "COALESCE(c.profanity_agent,0) > 0"},
    "profanity": {"title": "Нецензурная лексика в разговоре",
                  "order": "c.profanity DESC",
                  "where": "COALESCE(c.profanity,0) > 0"},
    "objections": {"title": "Возражения без отработки",
                   "order": "c.objections_unhandled DESC, c.objections DESC",
                   "where": "COALESCE(c.objections_unhandled,0) > 0"},
    "violations": {"title": "С нарушениями оператора",
                   "order": "c.violations DESC, c.agent_score ASC",
                   "where": "COALESCE(c.violations,0) > 0"},
    "low_score": {"title": "Самый низкий балл оператора",
                  "order": "c.agent_score ASC",
                  "where": "c.agent_score IS NOT NULL"},
    "impolite": {"title": "Невежливый оператор",
                 "order": "c.empathy ASC",
                 "where": "c.empathy IS NOT NULL AND c.empathy < 0"},
}


def _округлить(значение: Any, знаков: int = 4) -> Any:
    if значение is None:
        return None
    try:
        return round(float(значение), знаков)
    except (TypeError, ValueError):
        return None


def _процент(часть: Any, целое: Any) -> float | None:
    """Доля в процентах. None, когда делить не на что.

    Ноль здесь был неверным ответом, и не безобидно. Окно, где ни одна
    запись не получила оценки (разбор ещё не дошёл или сорвался), давало
    «отрицательных 0 %» — и правило сравнения с прошлым периодом объявляло
    рост с нуля до тридцати двух процентов там, где прошлый период не был
    измерен вовсе.
    """
    часть, целое = int(часть or 0), int(целое or 0)
    return round(100.0 * часть / целое, 1) if целое else None


_ПОРЯДКОВЫЕ = {2: "Каждый второй", 3: "Каждый третий", 4: "Каждый четвёртый",
               5: "Каждый пятый", 6: "Каждый шестой", 7: "Каждый седьмой",
               8: "Каждый восьмой", 9: "Каждый девятый", 10: "Каждый десятый"}


def _каждый(доля_процентов: float) -> str:
    """«Каждый третий», «каждый четвёртый» — по самой доле, а не наугад.

    Фраза была зашита в текст: при 100 % вывод сообщал «каждый четвёртый
    разговор отрицательный: 100.0 %», то есть сам себе противоречил. Вывод,
    который спорит с собственным числом, проверить нельзя — а именно
    проверяемость от выводов здесь и требуется.
    """
    if доля_процентов >= 95:
        return "Почти каждый"
    n = max(2, round(100.0 / max(доля_процентов, 1e-6)))
    return _ПОРЯДКОВЫЕ.get(n, f"Каждый {n}-й")


def _каждое(доля: float) -> str:
    """То же для среднего рода: «каждое двадцать пятое слово»."""
    if доля >= 0.5:
        return "каждое второе слово"
    n = max(2, round(1.0 / max(доля, 1e-9)))
    словами = {2: "второе", 3: "третье", 4: "четвёртое", 5: "пятое",
               10: "десятое", 20: "двадцатое", 25: "двадцать пятое",
               50: "пятидесятое"}
    return f"примерно каждое {словами.get(n, f'{n}-е')} слово"


def _корреляция(x: list[float], y: list[float]) -> float | None:
    """Коэффициент Пирсона. None, когда считать его не на чем."""
    n = len(x)
    if n < 2 or n != len(y):
        return None
    сx, сy = sum(x) / n, sum(y) / n
    числитель = sum((a - сx) * (b - сy) for a, b in zip(x, y, strict=True))
    дx = math.sqrt(sum((a - сx) ** 2 for a in x))
    дy = math.sqrt(sum((b - сy) ** 2 for b in y))
    if дx == 0 or дy == 0:
        # Постоянная величина: корреляции нет по определению, а формула даёт
        # деление на ноль. Случай не выдуманный: за короткий период все
        # записи могут оказаться на одной модели с одинаковым скриптом.
        return None
    return round(числитель / (дx * дy), 3)


#: Чем взвешивать среднее при склейке групп: у части показателей рядом в
#: том же агрегате лежит число записей, по которым среднее и посчитано.
#: Для остальных лучшего веса нет — берём число записей группы.
_ВЕС_СРЕДНЕГО: dict[str, str] = {
    "sentiment": "scored",
    "sentiment_shift": "scored",
    "agent_score": "scored_agents",
    "empathy": "scored_agents",
}


class Insights:
    """Свод по разобранным записям: разрезы, темы, связи, выводы."""

    def __init__(self, db: Database, index: Any = None):
        self.db = db
        self.index = index
        #: Замороженные границы окон на время сборки отчёта — своя для
        #: каждого потока, как в аналитике заданий.
        self._местное = threading.local()

    # --- окна -------------------------------------------------------------

    @property
    def _окна(self) -> dict[str, tuple[float | None, float | None]] | None:
        return getattr(self._местное, "окна", None)

    @_окна.setter
    def _окна(self, значение: dict[str, tuple[float | None, float | None]] | None) -> None:
        self._местное.окна = значение

    def window(self, period: str) -> tuple[float | None, float | None]:
        """Границы окна периода: (начало, конец предыдущего такого же).

        Внутри одного отчёта граница общая для всех разрезов. Считать её
        заново в каждом разрезе — значит расходиться с самим собой: отчёт
        по стотысячному архиву собирается секундами, и записи, приехавшие
        за это время, попадали в поздние разрезы и не попадали в ранние.
        Свод переставал сходиться с суммой по разрезам, а выводы сравнивали
        числа, посчитанные по трём разным границам. В аналитике заданий та
        же заморозка живёт с двадцать первого захода.
        """
        окна = self._окна
        if окна is not None and period in окна:
            return окна[period]
        секунды = PERIODS.get(period, 604800)
        значение: tuple[float | None, float | None]
        if not секунды:
            значение = (None, None)
        else:
            начало = time.time() - секунды
            значение = (начало, начало - секунды)
        if окна is not None:
            окна[period] = значение
        return значение

    # --- свод -------------------------------------------------------------

    def summary(self, period: str = "week", owner: str | list[str] | None = None,
                *, since: float | None = None,
                until: float | None = None) -> dict[str, Any]:
        """Показатели корпуса за окно — одной строкой из базы."""
        если_окно = self.window(period)[0] if since is None and until is None else None
        строки = self.db.content_aggregate(
            since=если_окно if since is None else since, until=until, owner=owner)
        return self._свод(строки[0] if строки else {})

    @staticmethod
    def _свод(строка: dict[str, Any]) -> dict[str, Any]:
        """Строка агрегата базы -> свод раздела: доли и округление."""
        оценено = int(строка.get("scored") or 0)
        отрицательных = int(строка.get("negative") or 0)
        положительных = int(строка.get("positive") or 0)
        обещаний = int(строка.get("commitments") or 0)
        со_сроком = int(строка.get("commitments_dated") or 0)
        тревожных = int(строка.get("alert_records") or 0)
        всего = int(строка.get("records") or 0)
        свод: dict[str, Any] = {
            "records": всего,
            "scored": оценено,
            "hours": _округлить(строка.get("hours"), 2) or 0.0,
            "negative": отрицательных,
            "positive": положительных,
            "neutral": max(0, оценено - отрицательных - положительных),
            "negative_share": _процент(отрицательных, оценено),
            "positive_share": _процент(положительных, оценено),
            "alert_records": тревожных,
            "alert_share": _процент(тревожных, всего),
            "commitments": обещаний,
            "commitments_dated": со_сроком,
            "commitments_open": max(0, обещаний - со_сроком),
            "questions": int(строка.get("questions") or 0),
            "money_max": _округлить(строка.get("money_max"), 2),
            # Записи с одним говорящим: показатели речи по ним вырождаются,
            # и знать их долю нужно раньше, чем читать эти показатели.
            "mono": int(строка.get("mono") or 0),
            "mono_share": _процент(строка.get("mono"), всего),
            "mixed": int(строка.get("mixed") or 0),
            "long_monologues": int(строка.get("long_monologues") or 0),
            "long_monologue_share": _процент(строка.get("long_monologues"), всего),
            "frustrated": int(строка.get("frustrated") or 0),
            "frustrated_share": _процент(строка.get("frustrated"), всего),
            "repeat": int(строка.get("repeat") or 0),
            "repeat_share": _процент(строка.get("repeat"), всего),
            "profanity_records": int(строка.get("profanity_records") or 0),
            "profanity_agent_records": int(строка.get("profanity_agent_records") or 0),
            "profanity_checked": int(строка.get("profanity_checked") or 0),
            "objections": int(строка.get("objections") or 0),
            "objections_unhandled": int(строка.get("objections_unhandled") or 0),
            "objections_checked": int(строка.get("objections_checked") or 0),
            # Доля возражений без отработки — от возражений в записях, где
            # отработку было чем считать; None, когда таких записей нет.
            "objections_unhandled_share": (
                _процент(строка.get("objections_unhandled"), строка.get("objections"))
                if int(строка.get("objections_checked") or 0) else None),
            "violations": int(строка.get("violations") or 0),
            "violation_records": int(строка.get("violation_records") or 0),
            "violation_share": _процент(строка.get("violation_records"), всего),
            "scored_agents": int(строка.get("scored_agents") or 0),
            # Обращение по имени — доля среди записей с определённым оператором.
            "named": int(строка.get("named") or 0),
            "name_checked": int(строка.get("name_checked") or 0),
            "named_share": (_процент(строка.get("named"), строка.get("name_checked"))
                            if int(строка.get("name_checked") or 0) else None),
        }
        for признак in ПРИЗНАКИ:
            ключ = признак["key"]
            if ключ not in свод:
                свод[ключ] = _округлить(строка.get(ключ), признак["digits"] + 2)
        return свод

    # --- разрезы ----------------------------------------------------------

    def breakdown(self, dimension: str = "owner", period: str = "week",
                  owner: str | list[str] | None = None,
                  limit: int = 50) -> dict[str, Any]:
        """Один разрез корпуса: те же показатели по группам."""
        описание = РАЗРЕЗЫ.get(dimension)
        if описание is None:
            return {"dimension": dimension, "title": dimension, "items": []}
        начало = self.window(period)[0]
        # Меток может быть сколько угодно, и группируются они не по значению
        # колонки, а по каждой метке в ней; поэтому запрашиваем с запасом и
        # разбираем сочетания сами.
        #
        # Упорядоченные разрезы (часы, дни недели, календарные дни) берутся
        # целиком: база отдаёт группы по убыванию числа записей, и предел в
        # полсотни оставлял от разреза «по дням за год» полсотни самых
        # оживлённых дней — а потом сортировка по дате превращала их в
        # непрерывный трёхмесячный отрезок. Выглядело это как «дальше
        # записей не было», хотя две трети архива просто не доехали.
        if описание.get("multi"):
            запас = limit * 20
        elif описание.get("ordered"):
            запас = ПРЕДЕЛ_УПОРЯДОЧЕННЫХ
        else:
            запас = limit + 50
        строки = self.db.content_aggregate(
            since=начало, owner=owner, group_by=dimension, limit=запас)
        группы = (self._развернуть_метки(строки) if описание.get("multi")
                  else [(str(с.get("group_key")), с) for с in строки])
        подписи = self._подписи_категорий() if dimension == "category" else {}
        items = []
        for ключ, строка in группы:
            свод = self._свод(строка)
            if свод["records"] < МИН_ГРУППА and not описание.get("ordered"):
                continue
            items.append({"key": ключ,
                          "label": подписи.get(ключ, {}).get("label")
                          or self._подпись(dimension, ключ),
                          **({"kind": подписи[ключ]["kind"]} if ключ in подписи else {}),
                          **свод})
        отсеяно = len(группы) - len(items)
        if описание.get("ordered"):
            items.sort(key=lambda з: self._ключ_порядка(dimension, з["key"]))
        else:
            items.sort(key=lambda з: -з["records"])
        показанные = items[:limit]
        # Считаем скрытое после среза, а не до: раньше «скрыто: 0» стояло
        # рядом со списком, урезанным вдвое.
        return {"dimension": dimension, "title": описание["title"],
                "items": показанные,
                "hidden": отсеяно + max(0, len(items) - len(показанные))}

    @staticmethod
    def _развернуть_метки(строки: list[dict[str, Any]]) -> list[tuple[str, dict]]:
        """Сочетания меток -> отдельные метки со сложением показателей.

        База группирует по строке меток целиком («продажи,vip»), потому что
        разбить строку на элементы она не умеет. Сочетаний немного — десятки,
        а не сотни тысяч, — поэтому досложить их в памяти дёшево и точно.
        Средние при этом взвешиваются по числу записей: простое среднее от
        средних завысило бы вклад сочетания, встретившегося дважды.
        """
        суммы: dict[str, dict[str, Any]] = {}
        средние = {и for и, выр in Database.CONTENT_METRICS if выр.startswith("AVG")}
        for строка in строки:
            метки = [м.strip() for м in str(строка.get("group_key") or "").split(",")
                     if м.strip()] or ["без метки"]
            n = float(строка.get("records") or 0)
            for метка in метки:
                цель = суммы.setdefault(метка, {"records": 0})
                for имя, _ in Database.CONTENT_METRICS:
                    значение = строка.get(имя)
                    if значение is None:
                        continue
                    if имя in средние:
                        # Вес — число записей, по которым среднее и
                        # посчитано, а не всех записей группы. AVG в SQL
                        # считается по непустым значениям: сочетание из ста
                        # записей, где тональность измерена у пяти, весило
                        # как сто — и метка с сотней неразобранных записей
                        # перебивала метку с двадцатью разобранными.
                        вес = float(строка.get(_ВЕС_СРЕДНЕГО.get(имя, "records")) or 0) or n
                        цель[имя] = цель.get(имя, 0.0) + float(значение) * вес
                        цель[f"_{имя}_вес"] = цель.get(f"_{имя}_вес", 0.0) + вес
                    elif имя == "money_max":
                        цель[имя] = max(цель.get(имя) or 0.0, float(значение))
                    else:
                        цель[имя] = (цель.get(имя) or 0) + значение
        out = []
        for метка, цель in суммы.items():
            for имя in средние:
                вес = цель.pop(f"_{имя}_вес", 0.0)
                цель[имя] = (цель[имя] / вес) if вес and имя in цель else None
            out.append((метка, цель))
        return out

    def _подписи_категорий(self) -> dict[str, dict[str, str]]:
        """Имя категории -> подпись и вид, по действующему набору."""
        from .content import categories as категории_модуль  # noqa: PLC0415

        набор = (self.index.categories() if self.index
                 else категории_модуль.compile(категории_модуль.ГОТОВЫЕ))
        return {к.id: {"label": к.label, "kind": к.kind} for к in набор}

    @staticmethod
    def _подпись(dimension: str, ключ: str) -> str:
        if dimension == "weekday":
            return _ДНИ[int(ключ) % 7].capitalize()
        if dimension == "hour":
            return f"{int(ключ):02d}:00"
        return ключ

    @staticmethod
    def _ключ_порядка(dimension: str, ключ: str) -> Any:
        if dimension in ("weekday", "hour"):
            # Понедельник первым: strftime('%w') считает от воскресенья, а
            # рабочая неделя начинается не с него.
            n = int(ключ)
            return (n + 6) % 7 if dimension == "weekday" else n
        return ключ

    # --- ряд по времени ---------------------------------------------------

    def timeline(self, period: str = "week", owner: str | list[str] | None = None,
                 buckets: int = 24) -> dict[str, Any]:
        """Как менялись показатели корпуса во времени."""
        конец = time.time()
        начало = self.window(period)[0]
        if начало is None:
            первая = self.db.query_one(
                "SELECT MIN(j.created_at) AS ts FROM content c "
                "JOIN jobs j ON j.id = c.job_id")
            начало = float((первая["ts"] if первая else None) or конец - 86400)
        buckets, шаг, строки = self.db.content_series(
            since=начало, until=конец, buckets=buckets, owner=owner)
        по_корзинам = {int(с["bucket"]): с for с in строки}
        точки = []
        for n in range(buckets):
            с = по_корзинам.get(n) or {}
            точки.append({
                "ts": round(начало + n * шаг, 1),
                "records": int(с.get("records") or 0),
                "sentiment": _округлить(с.get("sentiment"), 3),
                "negative_share": _процент(с.get("negative"), с.get("scored")),
                "alerts": int(с.get("alerts") or 0),
                "compliance": _округлить(с.get("compliance"), 3),
                "wpm": _округлить(с.get("wpm"), 0),
            })
        return {"buckets": точки, "step_s": round(шаг, 1),
                "from": round(начало, 1), "to": round(конец, 1)}

    # --- темы -------------------------------------------------------------

    def topics(self, period: str = "all", owner: str | list[str] | None = None,
               limit: int = 40) -> dict[str, Any]:
        """О чём говорят чаще всего.

        Доля считается от числа разобранных записей окна, а не от всех
        завершённых: пока архив разбирается фоном, второй знаменатель
        занижал бы каждую тему — и тем сильнее, чем меньше успел разбор.
        """
        от = self.window(period)[0]
        темы = self.db.top_terms(since=от, owner=owner, limit=limit)
        всего = self.db.content_window_size(since=от, owner=owner)
        for тема in темы:
            тема["share"] = _процент(тема["records"], всего)
            # Вес темы — не частота, а редкость: основа из девяноста
            # процентов записей («здравствуйте») — это фон, а не тема.
            тема["weight"] = round(
                math.log((всего + 1) / (тема["records"] + 1)) + 1.0, 3)
        return {"corpus": всего, "period": period, "items": темы}

    def topic_trend(self, period: str = "week",
                    owner: str | list[str] | None = None,
                    limit: int = 15) -> list[dict[str, Any]]:
        """Темы, которые стали звучать заметно чаще или реже.

        Сравниваются доли, а не количества: если записей за неделю стало
        вдвое больше, чаще станут встречаться все темы разом, и список
        «что изменилось» превратится в список «о чём вообще говорят».
        """
        начало, прошлое = self.window(period)
        if начало is None:
            # «За всё время» сравнивать не с чем: предыдущего окна нет.
            return []
        сейчас = self.db.top_terms(since=начало, owner=owner, limit=limit * 6)
        # Прошлое окно спрашиваем точечно по основам текущего, а не его
        # верхушкой. Верхушкой было неверно: основ в корпусе тысячи, в срез
        # попадает верхушка, и отсутствие основы в срезе трактовалось как
        # ноль употреблений. Список «стало звучать чаще» из-за этого
        # состоял из тем, которые не менялись вовсе, — сортировка по
        # величине изменения выносила выдуманные скачки в самое начало.
        раньше = {т["stem"]: т for т in self.db.top_terms(
            since=прошлое, until=начало, owner=owner,
            stems=[т["stem"] for т in сейчас], min_records=0)}
        n_сейчас = self.db.content_window_size(since=начало, owner=owner)
        n_раньше = self.db.content_window_size(
            since=прошлое, until=начало, owner=owner)
        if not n_сейчас or not n_раньше:
            return []
        out = []
        for тема in сейчас:
            было = раньше.get(тема["stem"], {}).get("records", 0)
            доля = _процент(тема["records"], n_сейчас)
            доля_было = _процент(было, n_раньше)
            out.append({"stem": тема["stem"], "word": тема["word"],
                        "now": тема["records"], "before": было,
                        "share_now": доля, "share_before": доля_было,
                        "delta": round(доля - доля_было, 1)})
        out.sort(key=lambda т: -abs(т["delta"]))
        return out[:limit]

    def new_topics(self, period: str = "week",
                   owner: str | list[str] | None = None,
                   limit: int = 15) -> list[dict[str, Any]]:
        """Слова, которых в прошлом окне не было вовсе.

        Сырьё для новых категорий обращений — то, что у Genesys делает Topic
        Miner: новая модель, новая акция, новый сбой называются словом,
        которого раньше в разговорах не звучало. Порог в три записи
        отсекает опечатки распознавания: слово из одной записи — шум.
        """
        строки = self.topic_trend(period, owner, limit=max(limit * 8, 80))
        новые = [т for т in строки if т["before"] == 0 and т["now"] >= МИН_НОВОГО]
        новые.sort(key=lambda т: -т["now"])
        return новые[:limit]

    # --- категории обращений ---------------------------------------------

    def categories(self, period: str = "week",
                   owner: str | list[str] | None = None) -> dict[str, Any]:
        """Счёт и динамика по категориям: сколько записей, доля, что
        изменилось к прошлому окну, что растёт и что угасает.

        Доли считаются от числа разобранных записей окна — того же
        знаменателя, что у тем. Категории набора, не встретившиеся ни разу,
        в списке остаются с нулём: «про возврат за неделю не говорили» —
        тоже ответ, и, возможно, самый важный.
        """
        from .content import categories as категории_модуль  # noqa: PLC0415

        начало, прошлое = self.window(period)
        набор = (self.index.categories() if self.index
                 else категории_модуль.compile(категории_модуль.ГОТОВЫЕ))
        сейчас = {с["category"]: с for с in self.db.category_counts(
            since=начало, owner=owner)}
        раньше = ({с["category"]: с for с in self.db.category_counts(
            since=прошлое, until=начало, owner=owner)} if начало is not None else {})
        n_сейчас = self.db.content_window_size(since=начало, owner=owner)
        n_раньше = (self.db.content_window_size(since=прошлое, until=начало, owner=owner)
                    if начало is not None else 0)
        items = []
        известные = set()
        for к in набор:
            известные.add(к.id)
            items.append(self._категория(к.to_dict(), сейчас.get(к.id), раньше.get(к.id),
                                         n_сейчас, n_раньше))
        # Совпадения категорий, которых в наборе уже нет (набор сменили, а
        # архив ещё не пересчитан): показываем как есть, с пометкой.
        for имя, строка in сейчас.items():
            if имя not in известные:
                items.append(self._категория(
                    {"id": имя, "label": имя, "kind": строка.get("kind") or "topic",
                     "stale": True}, строка, раньше.get(имя), n_сейчас, n_раньше))
        items.sort(key=lambda з: (-з["records"], з["label"]))
        растут = sorted((з for з in items if (з["delta"] or 0) >= 5 and з["records"] >= МИН_ГРУППА),
                        key=lambda з: -з["delta"])
        угасают = sorted((з for з in items if (з["delta"] or 0) <= -5
                          and (з["previous"] or 0) >= МИН_ГРУППА),
                         key=lambda з: з["delta"])
        # Срабатывания трекеров — по журналу событий; подпись берём из
        # действующего набора, а у переименованной с тех пор — из события.
        подписи_набора = {к.id: к.label for к in набор}
        трекеры = self.db.tracker_hits(since=начало)
        for т in трекеры:
            т["label"] = подписи_набора.get(т["category"], т.get("label") or т["category"])
        return {"period": period, "corpus": n_сейчас, "corpus_previous": n_раньше,
                "items": items, "rising": [з["id"] for з in растут],
                "fading": [з["id"] for з in угасают],
                "uncategorized": self._без_категории(начало, owner, n_сейчас),
                "trackers": трекеры,
                "own": bool(self.index and self.index.categories_own()),
                "kinds": категории_модуль.ВИДЫ, "who": категории_модуль.КТО}

    @staticmethod
    def _категория(описание: dict[str, Any], сейчас: dict[str, Any] | None,
                   раньше: dict[str, Any] | None, n_сейчас: int,
                   n_раньше: int) -> dict[str, Any]:
        записей = int((сейчас or {}).get("records") or 0)
        было = int((раньше or {}).get("records") or 0)
        доля = _процент(записей, n_сейчас)
        доля_было = _процент(было, n_раньше) if n_раньше else None
        return {
            "id": описание.get("id"), "label": описание.get("label"),
            "kind": описание.get("kind") or "topic",
            "who": описание.get("who"), "rule": описание.get("rule"),
            "error": описание.get("error"), "stale": bool(описание.get("stale")),
            "records": записей, "share": доля,
            "mentions": int((сейчас or {}).get("mentions") or 0),
            "first_s": _округлить((сейчас or {}).get("first_s"), 1),
            "previous": было, "share_previous": доля_было,
            # Изменение в процентных пунктах — только когда есть оба окна:
            # рост «с нуля» на пустом прошлом окне — это не рост.
            "delta": (round(доля - доля_было, 1)
                      if доля is not None and доля_было is not None else None),
        }

    def drivers(self, period: str = "week",
                owner: str | list[str] | None = None) -> dict[str, Any]:
        """Драйверы негатива: какие категории тянут разговоры вниз.

        Подъём (lift) — частота категории среди отрицательных разговоров,
        делённая на её частоту вообще: 2,0 значит «в отрицательных эта тема
        встречается вдвое чаще». Это то, что у Verint называется Cross
        Correlation, и то, ради чего категории и заводят: «доставка» сама по
        себе — тема, «доставка в каждом втором отрицательном разговоре» —
        причина. Связь, не причина в строгом смысле: категория может быть
        и следствием — «эскалация» чаще в отрицательных потому, что
        разговор уже отрицательный.
        """
        from .content import categories as категории_модуль  # noqa: PLC0415

        начало = self.window(period)[0]
        свод = self.summary(period, owner)
        оценено = int(свод.get("scored") or 0)
        отрицательных = int(свод.get("negative") or 0)
        подписи = self._подписи_категорий()
        строки = self.db.category_counts(since=начало, owner=owner)
        items = []
        for с in строки:
            n = int(с.get("scored") or 0)
            neg = int(с.get("negative") or 0)
            if n < МИН_ДРАЙВЕРА or not оценено or not отрицательных:
                continue
            доля_вообще = n / оценено
            доля_в_отриц = neg / отрицательных
            подъём = round(доля_в_отриц / доля_вообще, 2) if доля_вообще else None
            описание = подписи.get(с["category"], {})
            items.append({
                "id": с["category"],
                "label": описание.get("label") or с["category"],
                "kind": описание.get("kind") or с.get("kind") or "topic",
                "records": n, "negative": neg,
                "negative_share": _процент(neg, n),
                "share": _процент(n, оценено),
                "share_in_negative": _процент(neg, отрицательных),
                "lift": подъём,
                "kind_title": категории_модуль.ВИДЫ.get(
                    описание.get("kind") or с.get("kind") or "topic"),
            })
        вниз = sorted((з for з in items if (з["lift"] or 0) >= ПОДЪЁМ),
                      key=lambda з: -з["lift"])
        вверх = sorted((з for з in items if з["lift"] is not None
                        and з["lift"] <= 1 / ПОДЪЁМ),
                       key=lambda з: з["lift"])
        return {"period": period, "scored": оценено, "negative": отрицательных,
                "negative_share": свод.get("negative_share"),
                "min_records": МИН_ДРАЙВЕРА, "lift_threshold": ПОДЪЁМ,
                "items": sorted(items, key=lambda з: -(з["lift"] or 0)),
                "down": вниз, "up": вверх,
                "note": ("Подъём — во сколько раз категория чаще среди отрицательных "
                         "разговоров, чем вообще. Связь, а не причина: тема может "
                         "быть и следствием плохого разговора.")}

    def _без_категории(self, начало: float | None, owner: Any,
                       всего: int) -> dict[str, Any] | None:
        """Сколько записей окна не попало ни в одну категорию обращения.

        Это число — главный довод завести новую категорию: когда без
        категории половина архива, набор описывает не то, о чём звонят.
        """
        if not всего:
            return None
        без = self.db.uncategorized_count(since=начало, owner=owner)
        return {"records": без, "share": _процент(без, всего)}

    # --- нормы от своего архива и контрольные карты -----------------------

    def norms(self, period: str = "week", owner: str | list[str] | None = None,
              *, agent: tuple[str, str] | None = None) -> dict[str, Any]:
        """Своя обычная величина каждого показателя — медиана и квартили по
        четырём неделям до периода — и где относительно неё период.

        Ориентиры из чужих методик («темп 140–160», «доля речи 40–60 %»)
        сделаны для английских продаж; норма здесь — то, как обычно бывает
        на этом сервере. Коридор — межквартильный размах: в нём лежит
        половина записей; выброс — дальше полутора размахов от квартилей.
        """
        начало = self.window(period)[0]
        конец = начало if начало is not None else time.time()
        база_от = конец - БАЗА_ДНЕЙ * 86400
        столбцы = list(ПРИЗНАКИ_НОРМЫ)
        база = self.db.content_sample(столбцы, since=база_от, until=конец, owner=owner,
                                      limit=ПРЕДЕЛ_СВЯЗЕЙ, agent=agent)
        # Период сравнивается медианой, а не средним: среднее по многим
        # записям почти всегда внутри коридора, даже когда распределение
        # уехало; медиана периода против медианы базы — честнее.
        текущее = self.db.content_sample(столбцы, since=начало, owner=owner,
                                         limit=ПРЕДЕЛ_СВЯЗЕЙ, agent=agent)
        items = []
        for n, ключ in enumerate(столбцы):
            значения = [float(р[n]) for р in база if р[n] is not None]
            норма = stats.norm_band(значения, знаков=4)
            описание = ПРИЗНАКИ_ПО_КЛЮЧУ.get(ключ, {})
            свои = [float(р[n]) for р in текущее if р[n] is not None]
            сейчас = stats.percentile(свои, 0.5)
            items.append({
                "key": ключ, "title": описание.get("title", ключ),
                "unit": описание.get("unit", ""), "good": описание.get("good", 0),
                "digits": описание.get("digits", 2), "hint": описание.get("hint"),
                **норма, "current": _округлить(сейчас, описание.get("digits", 2) + 2),
                "current_n": len(свои),
                "status": stats.against_norm(сейчас, норма),
            })
        return {"period": period, "baseline_days": БАЗА_ДНЕЙ,
                "baseline_from": round(база_от, 1), "baseline_to": round(конец, 1),
                "baseline_records": len(база), "current_records": len(текущее),
                "min_sample": stats.МИН_ВЫБОРКА, "items": items}

    def control(self, period: str = "week", owner: str | list[str] | None = None,
                *, agent: tuple[str, str] | None = None) -> dict[str, Any]:
        """Контрольные карты по дням: пределы 2σ и 3σ по четырём неделям до
        периода и точки периода за ними.

        Правило серии — семь дней подряд по одну сторону от среднего —
        ловит сдвиг, который ни один день по отдельности не выдаёт: доля
        отрицательных выросла на треть, но каждый день по-прежнему внутри
        2σ. Дни без записей серию прерывают.
        """
        начало = self.window(period)[0]
        конец = time.time()
        if начало is None:
            начало = конец - 30 * 86400
        база_от = начало - БАЗА_ДНЕЙ * 86400
        дней_базы = БАЗА_ДНЕЙ
        дней = max(1, int(round((конец - начало) / 86400)))
        # Период короче суток сравнивать с суточными пределами нельзя:
        # база собрана по дням, и одна часовая точка на восьми разговорах
        # прыгает по биномиальному шуму далеко за 3σ — «критично» на ровном
        # месте при каждом открытии раздела за час. В таком случае карту
        # рисуем, а вердикты не выносим.
        короткий_период = (конец - начало) < 0.75 * 86400
        n_б, шаг_б, база = self.db.content_series(since=база_от, until=начало,
                                                  buckets=дней_базы, owner=owner, agent=agent)
        n_т, шаг_т, период_ряд = self.db.content_series(since=начало, until=конец,
                                                        buckets=дней, owner=owner, agent=agent)
        по_б = {int(с["bucket"]): с for с in база}
        по_т = {int(с["bucket"]): с for с in период_ряд}

        def значение(с: dict[str, Any] | None, ключ: str) -> float | None:
            if not с or not int(с.get("records") or 0):
                return None
            if ключ == "negative_share":
                return _процент(с.get("negative"), с.get("scored"))
            v = с.get(ключ)
            return float(v) if v is not None else None

        карты = []
        for ключ, подпись, лучше in КАРТЫ:
            ряд_базы = [значение(по_б.get(k), ключ) for k in range(n_б)]
            пределы = stats.control_limits([x for x in ряд_базы if x is not None])
            точки = [значение(по_т.get(k), ключ) for k in range(n_т)]
            отметки = [] if короткий_период else stats.spc_flags(точки, пределы)
            карты.append({
                "key": ключ, "title": подпись, "good": лучше,
                "limits": пределы,
                "baseline": [{"ts": round(база_от + k * шаг_б, 1), "value": ряд_базы[k]}
                             for k in range(n_б)],
                "points": [{"ts": round(начало + k * шаг_т, 1), "value": точки[k],
                            "records": int((по_т.get(k) or {}).get("records") or 0)}
                           for k in range(n_т)],
                "flags": отметки,
                "worst": max((ф["level"] for ф in отметки),
                             key=lambda у: {"critical": 2, "warning": 1}.get(у, 0),
                             default=None),
            })
        return {"period": period, "baseline_days": БАЗА_ДНЕЙ, "step_s": round(шаг_т, 1),
                "verdicts": not короткий_период,
                "note_short": ("Период короче суток: карта показана, вердикты не выносятся — "
                               "пределы посчитаны по суточным точкам."
                               if короткий_период else None),
                "baseline_step_s": round(шаг_б, 1), "charts": карты}

    # --- оператор ---------------------------------------------------------

    #: Что сравнивать в карточке оператора с командой: ключ свода, подпись,
    #: куда лучше (1 — больше, −1 — меньше, 0 — никуда), знаков.
    СРАВНЕНИЕ: tuple[tuple[str, str, int, int], ...] = (
        ("agent_score", "Балл оператора", 1, 0),
        ("sentiment", "Тональность", 1, 2),
        ("negative_share", "Отрицательных, %", -1, 1),
        ("compliance", "Скрипт", 1, 2),
        ("empathy", "Индекс эмпатии", 1, 0),
        ("violation_share", "С нарушениями, %", -1, 1),
        ("named_share", "Обратился по имени, %", 1, 1),
        ("talk_share", "Доля речи оператора", 0, 2),
        ("monologue_s", "Самый долгий монолог, с", -1, 0),
        ("reply_delay_s", "Пауза перед ответом, с", 0, 2),
        ("interruptions", "Перебиваний", -1, 1),
        ("objections_unhandled_share", "Возражений без отработки, %", -1, 1),
        ("frustrated_share", "Клиент раздражён, %", -1, 1),
        ("duration_s", "Длительность, с", 0, 0),
    )

    #: Причины попадания в очередь коучинга — по полям записи.
    ПРИЧИНЫ_КОУЧИНГА: tuple[tuple[str, Any, str], ...] = (
        ("violations", lambda з: (з.get("violations") or 0) > 0,
         "нарушение оператора"),
        ("low_score", lambda з: з.get("agent_score") is not None and з["agent_score"] < 60,
         "балл ниже 60"),
        ("script", lambda з: з.get("compliance") is not None and з["compliance"] < 0.5,
         "скрипт меньше половины"),
        ("monologue", lambda з: (з.get("monologue_s") or 0) >= 150,
         "монолог дольше 2,5 мин"),
        ("impolite", lambda з: з.get("empathy") is not None and з["empathy"] < 0,
         "невежливых оборотов больше вежливых"),
        ("objections", lambda з: (з.get("objections_unhandled") or 0) > 0,
         "возражение без отработки"),
        ("frustrated", lambda з: (з.get("frustration") or 0) > 0,
         "клиент раздражён"),
        ("profanity", lambda з: (з.get("profanity_agent") or 0) > 0,
         "нецензурная лексика у сотрудника"),
    )

    def _причины(self, запись: dict[str, Any]) -> list[str]:
        out = []
        for _, правило, подпись in self.ПРИЧИНЫ_КОУЧИНГА:
            try:
                if правило(запись):
                    out.append(подпись)
            except (TypeError, ValueError):
                continue
        return out

    def agents(self, by: str = "speaker", period: str = "week",
               owner: str | list[str] | None = None,
               limit: int = 100) -> dict[str, Any]:
        """Список операторов — разрез с баллом, эмпатией и нарушениями."""
        if by not in Database.AGENT_DIMENSIONS:
            raise ValueError(f"неизвестный разрез оператора: {by}")
        разрез = self.breakdown(by, period, owner, limit=limit)
        return {"by": by, "period": period, "items": разрез["items"],
                "hidden": разрез.get("hidden", 0)}

    def agent_card(self, key: str, *, by: str = "speaker", period: str = "week",
                   owner: str | list[str] | None = None) -> dict[str, Any]:
        """Карточка оператора: показатели против команды, ход по неделям,
        нарушения, лучшие и худшие записи, очередь коучинга.

        Оператор здесь — либо метка говорящего в разборе (по умолчанию),
        либо владелец задания: ключ доступа, под которым записи загружены.
        Второе точнее там, где у каждого сотрудника свой ключ: метка
        «SPEAKER_00» — это «кто заговорил первым», а не человек.
        """
        if by not in Database.AGENT_DIMENSIONS:
            raise ValueError(f"неизвестный разрез оператора: {by}")
        оператор = (by, key)
        начало, прошлое = self.window(period)
        строки = self.db.content_aggregate(since=начало, owner=owner, agent=оператор)
        свой = self._свод(строки[0] if строки else {})
        команда = self.summary(period, owner)
        прошлый_свой = (self._свод((self.db.content_aggregate(
            since=прошлое, until=начало, owner=owner, agent=оператор) or [{}])[0])
                        if начало is not None else None)
        сравнение = []
        for ключ, подпись, лучше, знаков in self.СРАВНЕНИЕ:
            своё, общее = свой.get(ключ), команда.get(ключ)
            разница = (round(float(своё) - float(общее), знаков + 1)
                       if своё is not None and общее is not None else None)
            сравнение.append({
                "key": ключ, "title": подпись, "digits": знаков,
                "agent": _округлить(своё, знаков + 1), "team": _округлить(общее, знаков + 1),
                "previous": _округлить((прошлый_свой or {}).get(ключ), знаков + 1),
                "delta": разница,
                "verdict": (None if разница is None or not лучше or abs(разница) < 1e-9
                            else ("better" if разница * лучше > 0 else "worse")),
            })
        # Ход по неделям: недельные корзины за период, но не меньше четырёх.
        конец = time.time()
        от = начало if начало is not None else конец - 90 * 86400
        корзин = max(4, min(53, int(round((конец - от) / (7 * 86400)))))
        n, шаг, ряд = self.db.content_series(since=от, until=конец, buckets=корзин,
                                             owner=owner, agent=оператор)
        по_корзинам = {int(с["bucket"]): с for с in ряд}
        ход = []
        for k in range(n):
            с = по_корзинам.get(k) or {}
            ход.append({"ts": round(от + k * шаг, 1), "records": int(с.get("records") or 0),
                        "agent_score": _округлить(с.get("agent_score"), 1),
                        "sentiment": _округлить(с.get("sentiment"), 3),
                        "empathy": _округлить(с.get("empathy"), 1),
                        "violation_records": int(с.get("violation_records") or 0)})
        подписи = self._подписи_категорий()
        нарушения = [{**н, "label": подписи.get(н["category"], {}).get("label", н["category"])}
                     for н in self.db.agent_violations(since=начало, owner=owner,
                                                       agent=оператор)]
        худшие = self.db.content_top("c.agent_score IS NULL, c.agent_score ASC, c.sentiment ASC",
                                     since=начало, owner=owner, agent=оператор, limit=5)
        лучшие = self.db.content_top("c.agent_score DESC, c.sentiment DESC",
                                     since=начало, owner=owner, agent=оператор,
                                     where="c.agent_score IS NOT NULL", limit=5)
        очередь = self.coaching(period, owner, agent=оператор, limit=20)
        return {
            "by": by, "key": key, "period": period,
            "summary": свой, "team": команда, "previous": прошлый_свой,
            "compare": сравнение, "timeline": ход, "step_s": round(шаг, 1),
            "violations": нарушения, "worst": худшие, "best": лучшие,
            "coaching": очередь["items"], "coaching_total": очередь["total"],
        }

    def coaching(self, period: str = "week", owner: str | list[str] | None = None,
                 *, agent: tuple[str, str] | None = None, limit: int = 50,
                 include_done: bool = False) -> dict[str, Any]:
        """Очередь коучинга: записи с причиной и отметкой «разобрано»."""
        начало = self.window(period)[0]
        строки = self.db.coaching_queue(since=начало, owner=owner, agent=agent,
                                        limit=limit, include_done=include_done)
        items = []
        for з in строки:
            items.append({**з, "reasons": self._причины(з),
                          "mark": ({"status": з.get("mark_status"), "note": з.get("mark_note"),
                                    "updated_at": з.get("mark_at")}
                                   if з.get("mark_status") else None)})
        return {"period": period, "items": items, "total": len(items),
                "reasons": [{"key": к, "title": п} for к, _, п in self.ПРИЧИНЫ_КОУЧИНГА]}

    def references(self, period: str = "week", owner: str | list[str] | None = None,
                   limit: int = 20) -> dict[str, Any]:
        """Эталонные разговоры: отмеченные руками и лучшие за период."""
        начало = self.window(period)[0]
        строки = self.db.reference_records(since=начало, owner=owner, limit=limit)
        return {"period": period, "items": [
            {**з, "marked": з.get("mark_status") == "yes"} for з in строки]}

    # --- связи ------------------------------------------------------------

    def correlations(self, period: str = "week",
                     owner: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Связи между признаками — с порогами и без причинности."""
        нужные = sorted({к for пара in ПАРЫ for к in пара[:2]})
        выборка = self.db.content_sample(
            нужные, since=self.window(period)[0], owner=owner,
            limit=ПРЕДЕЛ_СВЯЗЕЙ)
        место = {имя: n for n, имя in enumerate(нужные)}
        out = []
        for первый, второй, шаблон in ПАРЫ:
            пары = [(строка[место[первый]], строка[место[второй]])
                    for строка in выборка]
            пары = [(float(a), float(b)) for a, b in пары
                    if a is not None and b is not None]
            if len(пары) < МИН_ПАР:
                continue
            r = _корреляция([a for a, _ in пары], [b for _, b in пары])
            if r is None or abs(r) < МИН_СВЯЗЬ:
                continue
            out.append({
                "x": первый, "y": второй, "r": r, "n": len(пары),
                "x_title": ПРИЗНАКИ_ПО_КЛЮЧУ.get(первый, {}).get("title", первый),
                "y_title": ПРИЗНАКИ_ПО_КЛЮЧУ.get(второй, {}).get("title", второй),
                "text": шаблон.format("выше" if r > 0 else "ниже"),
                "strength": ("заметная" if abs(r) >= 0.5 else
                             "умеренная" if abs(r) >= 0.35 else "слабая"),
            })
        out.sort(key=lambda з: -abs(з["r"]))
        return {"items": out, "sampled": len(выборка),
                "limit": ПРЕДЕЛ_СВЯЗЕЙ,
                "note": ("Связь — не причина. Совпадение двух величин говорит, "
                         "что их стоит посмотреть вместе, а не что одна "
                         "вызывает другую.")}

    # --- записи, которые стоит послушать ----------------------------------

    def records(self, kind: str = "negative", period: str = "week",
                owner: str | list[str] | None = None,
                limit: int = 20) -> dict[str, Any]:
        """Список записей по одному из отборов."""
        отбор = ОТБОРЫ.get(kind)
        if отбор is None:
            return {"kind": kind, "title": kind, "items": []}
        строки = self.db.content_top(
            отбор["order"], since=self.window(period)[0], owner=owner,
            where=отбор["where"], limit=limit)
        return {"kind": kind, "title": отбор["title"], "items": строки}

    @staticmethod
    def kinds() -> list[dict[str, str]]:
        return [{"key": к, "title": о["title"]} for к, о in ОТБОРЫ.items()]

    # --- выводы -----------------------------------------------------------

    def findings(self, period: str = "week", owner: str | list[str] | None = None,
                 *, свод: dict[str, Any] | None = None,
                 прошлый: dict[str, Any] | None = None,
                 разрезы: dict[str, Any] | None = None,
                 категории: dict[str, Any] | None = None,
                 драйверы: dict[str, Any] | None = None,
                 нормы: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Готовые выводы: правила с порогами, а не пересказ цифр.

        Каждый вывод несёт уровень внимания и числа, из которых сделан.
        Вывод без чисел проверить нельзя, а непроверяемым выводам в отчёте
        не место.
        """
        начало, прошлое = self.window(period)
        свод = свод if свод is not None else self.summary(period, owner)
        if прошлый is None and начало is not None:
            прошлый = self.summary(period, owner, since=прошлое, until=начало)
        прошлый = прошлый or {}
        # Те же разрезы, что перебирает правило ниже, — иначе выводы на
        # экране и выводы в выгрузке расходятся: отчёт передаёт сюда набор
        # с метками, а вкладка «Свод» считала без них, и в присланном
        # руководителю файле оказывался вывод, которого в интерфейсе нет.
        разрезы = разрезы if разрезы is not None else {
            d: self.breakdown(d, period, owner) for d in РАЗРЕЗЫ_ДЛЯ_ВЫВОДОВ}
        out: list[dict[str, Any]] = []
        # Тот же порог, что у групп разреза, и по той же причине. Без него
        # на одной записи выходило семь выводов уровня «требует внимания»:
        # «каждый четвёртый разговор отрицательный: 100 % (1 из 1)». Новый
        # сервер встречал человека набором ложных тревог.
        if int(свод.get("records") or 0) < МИН_КОРПУСА:
            return out

        def добавить(уровень: str, текст: str, **числа: Any) -> None:
            out.append({"severity": уровень, "text": текст, **числа})

        # 1. Доля отрицательных и её изменение.
        доля = свод["negative_share"]
        было = прошлый.get("negative_share")
        if доля is not None and доля >= 25:
            добавить("warning",
                     f"{_каждый(доля)} разговор отрицательный: {доля}% "
                     f"({свод['negative']} из {свод['scored']})",
                     metric="negative_share", value=доля)
        # Сравнение с прошлым окном только когда в обоих есть на чём
        # сравнивать: «доля отрицательных выросла с 0% до 50%» на четырёх
        # записях — это не рост, а две записи.
        # Сравнивать можно, только если в обоих окнах есть что сравнивать —
        # и не по числу записей, а по числу ОЦЕНЁННЫХ: окно, где разбор
        # сорвался у всех, имеет двадцать записей и ноль оценок, а доля
        # отрицательных в нём не ноль, а неизвестна.
        if (было is not None and доля is not None
                and прошлый.get("scored", 0) >= 20 and свод.get("scored", 0) >= 20):
            if доля - было >= 5:
                добавить("warning",
                         f"Доля отрицательных разговоров выросла: {было}% → {доля}%",
                         metric="negative_share", value=доля, previous=было)
            elif было - доля >= 5:
                добавить("good",
                         f"Доля отрицательных разговоров снизилась: {было}% → {доля}%",
                         metric="negative_share", value=доля, previous=было)

        # 2. Тревожные упоминания.
        if свод["alert_records"] and свод["alert_share"] is not None:
            добавить("warning" if свод["alert_share"] >= 3 else "info",
                     f"Суд, жалобы и огласка упоминаются в "
                     f"{свод['alert_records']} записях ({свод['alert_share']}%)",
                     metric="alert_records", value=свод["alert_records"])

        # 3. Обещания без срока — то, за чем никто не следит.
        открытых = свод["commitments_open"]
        if открытых and свод["commitments"]:
            добавить("warning" if открытых >= свод["commitments"] * 0.5 else "info",
                     f"Обещаний без названного срока: {открытых} из "
                     f"{свод['commitments']}",
                     metric="commitments_open", value=открытых)

        # 4. Скрипт разговора.
        скрипт = свод.get("compliance")
        if скрипт is not None and скрипт < 0.7:
            добавить("warning",
                     f"Скрипт разговора соблюдается в среднем на "
                     f"{round(скрипт * 100)}%",
                     metric="compliance", value=скрипт)

        # 5. Выделяющиеся группы. Сравнение с общим средним, а не между
        #    собой: «худший оператор» есть всегда, даже когда все работают
        #    одинаково хорошо.
        среднее = свод.get("sentiment")
        for разрез, подпись in РАЗРЕЗЫ_ВЫВОДОВ.items():
            данные = (разрезы.get(разрез) or {}).get("items") or []
            if len(данные) < 2 or среднее is None:
                continue
            for группа in данные:
                своё = группа.get("sentiment")
                if своё is None or группа["records"] < max(МИН_ГРУППА, 10):
                    continue
                # Два знака: тональность в своде показана с двумя, и вывод с
                # четырьмя выглядел бы посчитанным по другим числам.
                if своё <= среднее - 0.25:
                    добавить("warning",
                             f"У {подпись} «{группа['label']}» тональность заметно "
                             f"ниже средней: {round(своё, 2)} против {round(среднее, 2)} "
                             f"({группа['records']} записей)",
                             metric="sentiment", value=своё, group=группа["label"],
                             dimension=разрез)
                elif своё >= среднее + 0.25:
                    добавить("good",
                             f"У {подпись} «{группа['label']}» тональность заметно "
                             f"выше средней: {round(своё, 2)} против {round(среднее, 2)} "
                             f"({группа['records']} записей)",
                             metric="sentiment", value=своё, group=группа["label"],
                             dimension=разрез)

        # 6. Речь: темп и молчание.
        темп = свод.get("wpm")
        if темп and темп >= 180:
            добавить("info", f"Средний темп речи высокий — {round(темп)} слов в "
                             f"минуту; на таком темпе клиенты переспрашивают",
                     metric="wpm", value=темп)
        тишина = свод.get("silence_share")
        if тишина is not None and тишина >= 0.35:
            добавить("info",
                     f"Молчание занимает {round(тишина * 100)}% времени записей: "
                     f"либо долгие поиски в системе, либо запись длиннее разговора",
                     metric="silence_share", value=тишина)
        паразиты = свод.get("filler_rate")
        if паразиты is not None and паразиты >= 0.04:
            добавить("info",
                     f"Слова-паразиты занимают {round(паразиты * 100, 1)}% "
                     f"всех слов: {_каждое(паразиты)} — «э-э», «как бы» или "
                     f"«короче»",
                     metric="filler_rate", value=паразиты)

        # 6а. Стороны разговора. Пороги — из опубликованной практики
        #     (Gong, Avoma), и это ориентиры для английских продаж, поэтому
        #     уровень «info», а не «warning»: вывод показывает число, а
        #     решать, норма ли это здесь, — человеку.
        доля_речи = свод.get("talk_share")
        if доля_речи is not None and доля_речи >= 0.65:
            добавить("info",
                     f"Оператор говорит больше клиента: {round(доля_речи * 100)}% "
                     f"времени речи — ориентир 40–60%",
                     metric="talk_share", value=доля_речи)
        монологов = свод.get("long_monologue_share")
        if монологов is not None and монологов >= 20 and свод.get("long_monologues"):
            добавить("info",
                     f"{_каждый(монологов)} разговор содержит монолог оператора "
                     f"дольше двух с половиной минут: {монологов}% "
                     f"({свод['long_monologues']} из {свод['records']})",
                     metric="long_monologue_share", value=монологов)
        тишина_с = свод.get("dead_air_s")
        длительность = свод.get("duration_s")
        if тишина_с and длительность and тишина_с / длительность >= 0.2:
            добавить("info",
                     f"Заметная тишина занимает {round(100 * тишина_с / длительность)}% "
                     f"записи: в среднем {round(тишина_с)} с пауз от трёх секунд",
                     metric="dead_air_s", value=тишина_с)
        смешанных = свод.get("mixed")
        if смешанных and свод.get("scored") and смешанных / свод["scored"] >= 0.15:
            добавить("info",
                     f"Противоречивых разговоров — и с резкими, и с тёплыми "
                     f"репликами — {смешанных} ({_процент(смешанных, свод['scored'])}%): "
                     f"средняя оценка по ним ничего не говорит, их стоит послушать",
                     metric="mixed", value=смешанных)

        # 6б. Фрустрация, повторные обращения, нецензурная лексика.
        раздражённых = свод.get("frustrated_share")
        if раздражённых is not None and раздражённых >= 10:
            добавить("warning",
                     f"{_каждый(раздражённых)} разговор — с признаками раздражения "
                     f"клиента: {раздражённых}% ({свод['frustrated']} из {свод['records']})",
                     metric="frustrated_share", value=раздражённых)
        повторных = свод.get("repeat_share")
        if повторных is not None and повторных >= 10:
            добавить("warning",
                     f"{_каждый(повторных)} разговор — повторное обращение "
                     f"по нерешённому вопросу: {повторных}% ({свод['repeat']} из "
                     f"{свод['records']}); это обратная сторона решения с первого раза",
                     metric="repeat_share", value=повторных)
        if свод.get("profanity_agent_records"):
            добавить("warning",
                     f"Нецензурная лексика у сотрудника — в "
                     f"{свод['profanity_agent_records']} записях",
                     metric="profanity_agent_records",
                     value=свод["profanity_agent_records"])

        # 6в. Категории обращений: что выросло, что угасло, сколько записей
        #     не описано набором. Порог в пять процентных пунктов — тот же,
        #     что у доли отрицательных: меньше на недельном окне — шум.
        if категории is None:
            try:
                категории = self.categories(period, owner)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Категории для выводов не посчитаны: %s", exc)
                категории = {}
        по_имени = {к["id"]: к for к in (категории.get("items") or [])}
        for имя in (категории.get("rising") or [])[:2]:
            к = по_имени[имя]
            добавить("warning" if к["kind"] == "violation" else "info",
                     f"Обращений про «{к['label']}» стало больше: "
                     f"{к['share_previous']}% → {к['share']}% записей "
                     f"({к['previous']} → {к['records']})",
                     metric="category", value=к["share"], previous=к["share_previous"],
                     group=к["label"], dimension="category")
        for имя in (категории.get("fading") or [])[:1]:
            к = по_имени[имя]
            добавить("info",
                     f"Обращений про «{к['label']}» стало меньше: "
                     f"{к['share_previous']}% → {к['share']}% записей "
                     f"({к['previous']} → {к['records']})",
                     metric="category", value=к["share"], previous=к["share_previous"],
                     group=к["label"], dimension="category")
        # Балл оператора, нарушения и эмпатия.
        балл = свод.get("agent_score")
        if балл is not None and балл < 60 and int(свод.get("scored_agents") or 0) >= МИН_ГРУППА:
            добавить("warning",
                     f"Средний балл оператора — {round(балл)} из 100: скрипт с весами "
                     f"минус штрафы за стоп-слова",
                     metric="agent_score", value=балл)
        нарушений = свод.get("violation_share")
        if нарушений is not None and нарушений >= 10 and свод.get("violation_records"):
            добавить("warning",
                     f"{_каждый(нарушений)} разговор — с нарушением оператора: "
                     f"{нарушений}% ({свод['violation_records']} из {свод['records']}); "
                     f"стоп-слова и другие категории вида «нарушение»",
                     metric="violation_share", value=нарушений)
        эмпатия = свод.get("empathy")
        if эмпатия is not None and эмпатия < 0:
            добавить("warning",
                     f"Индекс эмпатии операторов отрицательный: {round(эмпатия)} — "
                     f"невежливых оборотов («подождите», «вы должны») больше, чем вежливых",
                     metric="empathy", value=эмпатия)

        # Возражения без отработки — когда отработку было чем считать.
        доля_без_ответа = свод.get("objections_unhandled_share")
        if (доля_без_ответа is not None and доля_без_ответа >= 30
                and int(свод.get("objections") or 0) >= МИН_ГРУППА):
            добавить("warning",
                     f"Возражения клиентов остаются без отработки: "
                     f"{свод['objections_unhandled']} из {свод['objections']} "
                     f"({доля_без_ответа}%) — за «дорого» и «подумаю» в следующих "
                     f"репликах не прозвучало ничего из «отработки»",
                     metric="objections_unhandled_share", value=доля_без_ответа)
        # Драйвер негатива — одна строка про самый сильный подъём.
        if драйверы is None:
            try:
                драйверы = self.drivers(period, owner)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Драйверы для выводов не посчитаны: %s", exc)
                драйверы = {}
        for д in (драйверы.get("down") or [])[:1]:
            добавить("warning",
                     f"Разговоры про «{д['label']}» отрицательные чаще других: "
                     f"{д['negative_share']}% против {драйверы.get('negative_share')}% "
                     f"в среднем — в {д['lift']} раза чаще среди отрицательных "
                     f"({д['negative']} из {д['records']})",
                     metric="lift", value=д["lift"], group=д["label"],
                     dimension="category")
        без = категории.get("uncategorized") or {}
        if без.get("share") is not None and без["share"] >= 50 and по_имени:
            добавить("info",
                     f"{_каждый(без['share'])} разговор не попал ни в одну категорию "
                     f"обращений ({без['share']}%, {без['records']} из "
                     f"{категории.get('corpus')}): набор категорий описывает не то, "
                     f"о чём звонят, — посмотрите новые слова периода на вкладке «Темы»",
                     metric="uncategorized", value=без["share"])

        # 6г. Своя норма: показатель периода за коридором четырёх недель до
        #     него — в плохую сторону. Чужие ориентиры выше говорят «ориентир»,
        #     это говорит «у вас обычно не так».
        if нормы is None:
            try:
                нормы = self.norms(period, owner)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Нормы для выводов не посчитаны: %s", exc)
                нормы = {}
        for н in (нормы.get("items") or []):
            статус = н.get("status")
            if not статус or not статус.startswith("outlier"):
                continue
            выше = статус == "outlier_high"
            # Показатель с направлением: выброс в хорошую сторону — не
            # вывод; без направления (темп, длительность) — «к сведению»:
            # отклонение от своей нормы стоит знать, даже когда неясно,
            # хорошо это или плохо.
            if н.get("good"):
                плохо = (н["good"] < 0) if выше else (н["good"] > 0)
                if not плохо:
                    continue
                уровень = "warning"
            else:
                уровень = "info"
            добавить(уровень,
                     f"{н['title']} за период {'выше' if выше else 'ниже'} своей нормы: "
                     f"медиана {round(н['current'], н['digits'])} при обычных "
                     f"{round(н['q1'], н['digits'])}–{round(н['q3'], н['digits'])} "
                     f"(медиана {round(н['median'], н['digits'])} по {н['n']} записям "
                     f"за {нормы.get('baseline_days')} дней до периода)",
                     metric=н["key"], value=н["current"], previous=н["median"])

        # 7. Часы, когда разговоры тяжелее.
        часы = (разрезы.get("hour") or {}).get("items") or []
        годные = [ч for ч in часы if ч["records"] >= max(МИН_ГРУППА, 10)
                  and ч.get("sentiment") is not None]
        if len(годные) >= 4 and среднее is not None:
            худший = min(годные, key=lambda ч: ч["sentiment"])
            if худший["sentiment"] <= среднее - 0.2:
                добавить("info",
                         f"Тяжелее всего разговоры идут в {худший['label']}: "
                         f"тональность {худший['sentiment']} против {среднее} "
                         f"в среднем",
                         metric="sentiment", value=худший["sentiment"],
                         group=худший["label"], dimension="hour")

        # 8. Записи без разделения по говорящим. Не дефект и не тревога, но
        #    без этой строки человек смотрит на «перебиваний 0.0» и читает
        #    это как «перебиваний не было», хотя считать их было не по чему.
        доля_моно = свод.get("mono_share")
        if доля_моно is not None and доля_моно >= 20:
            добавить("info",
                     f"В {доля_моно}% записей говорящий не разделён "
                     f"({свод.get('mono')} из {свод['records']}): перебивания, "
                     f"монологи, разрез по операторам и ход тональности по "
                     f"ним не считаются",
                     metric="mono_share", value=доля_моно)

        # 9. Разбор ещё не закончен — об этом надо сказать до выводов, а не
        #    после: свод по половине архива читается как свод по архиву.
        состояние = self.index.status() if self.index else {}
        if состояние.get("pending"):
            добавить("info",
                     f"Разобрано {состояние.get('analyzed', 0)} записей из "
                     f"{состояние.get('total', 0)}; остальные считаются в фоне, "
                     f"и показатели ещё сдвинутся",
                     metric="pending", value=состояние["pending"])

        порядок = {"warning": 0, "good": 1, "info": 2}
        out.sort(key=lambda в: порядок.get(в["severity"], 3))
        return out

    # --- полный отчёт -----------------------------------------------------

    def report(self, period: str = "week", owner: str | list[str] | None = None,
               *, dimensions: tuple[str, ...] = ("owner", "speaker", "tag", "category",
                                                 "model", "source", "label", "weekday",
                                                 "hour"),
               ) -> dict[str, Any]:
        """Всё сразу: свод, разрезы, темы, связи, выводы и что послушать."""
        начало_счёта = time.time()
        свои_окна = self._окна is None
        if свои_окна:
            self._окна = {}
        try:
            return self._собрать(period, owner, dimensions, начало_счёта)
        finally:
            if свои_окна:
                self._окна = None

    def _собрать(self, period: str, owner: str | list[str] | None,
                 dimensions: tuple[str, ...], начало_счёта: float) -> dict[str, Any]:
        начало, прошлое = self.window(period)
        свод = self.summary(period, owner)
        прошлый = (self.summary(period, owner, since=прошлое, until=начало)
                   if начало is not None else None)
        разрезы = {d: self.breakdown(d, period, owner) for d in dimensions}
        категории = self.categories(period, owner)
        драйверы = self.drivers(period, owner)
        нормы = self.norms(period, owner)
        return {
            "period": period,
            "generated_at": time.time(),
            "took_s": round(time.time() - начало_счёта, 3),
            "coverage": self.index.status() if self.index else {},
            "summary": свод,
            "previous": прошлый,
            "timeline": self.timeline(period, owner),
            "breakdowns": разрезы,
            "topics": self.topics(period, owner),
            "topic_trend": self.topic_trend(period, owner),
            "correlations": self.correlations(period, owner),
            "categories": категории,
            "drivers": драйверы,
            "norms": нормы,
            "control": self.control(period, owner),
            "findings": self.findings(period, owner, свод=свод, прошлый=прошлый,
                                      разрезы=разрезы, категории=категории,
                                      драйверы=драйверы, нормы=нормы),
            "highlights": {k: self.records(k, period, owner, limit=10)
                           for k in ("negative", "downturn", "alerts",
                                     "open_commitments", "script")},
            "features": ПРИЗНАКИ,
            "kinds": self.kinds(),
            "dimensions": [{"key": k, "title": v["title"]}
                           for k, v in РАЗРЕЗЫ.items()],
            "version": content.VERSION,
        }
