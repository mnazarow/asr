"""Контроль качества: кому перезвонить, что спросить и что из этого вышло.

После разговора с добавочным из группы контроля сервер перезванивает
клиенту (см. `sip.Телефон`), проговаривает текст группы (см. `tts`) и
просит оценить нажатием кнопки. Здесь — всё, что между звонком в архиве и
оценкой в отчёте:

* `разобрать_группы` — список групп из настройки `qc_groups`;
* `отобрать` — нужен ли опрос по этому звонку и кому звонить;
* `провести` — сам сценарий разговора: приветствие, вопросы, повторы,
  отказ от опросов, прощание;
* `КонтрольКачества` — фоновая служба: находит новые звонки, держит
  очередь обзвона, соблюдает часы, паузу между опросами одного номера и
  список «не звонить»;
* `сводка` — оценки по группам, добавочным и вопросам.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..db import Database, new_id
from ..errors import ConfigError
from ..logging_setup import get_logger
from . import tts
from .sip import Занято, НеОтвечает, ОшибкаSIP, Разговор, Телефон

log = get_logger("qc")

#: Состояния опроса. Первые два — очередь, остальные — итог.
ЖДЁТ, ЗВОНИТ = "ждёт", "звонит"
ПРОЙДЕН, ЧАСТИЧНО, БЕЗ_ОТВЕТА = "пройден", "частично", "без ответа"
ОТКАЗ, НЕ_ДОЗВОНИЛИСЬ, ОШИБКА = "отказ", "не дозвонились", "ошибка"
УСТАРЕЛ, ОТМЕНЁН = "устарел", "отменён"
АВТООТВЕТЧИК = "автоответчик"
ИТОГОВЫЕ = (ПРОЙДЕН, ЧАСТИЧНО, БЕЗ_ОТВЕТА, ОТКАЗ, НЕ_ДОЗВОНИЛИСЬ, ОШИБКА, УСТАРЕЛ, ОТМЕНЁН,
            АВТООТВЕТЧИК)
#: Клиент снял трубку.
ДОЗВОНИЛИСЬ = (ПРОЙДЕН, ЧАСТИЧНО, БЕЗ_ОТВЕТА, ОТКАЗ)
#: Клиент ответил хотя бы на один вопрос.
С_ОЦЕНКОЙ = (ПРОЙДЕН, ЧАСТИЧНО)

НАПРАВЛЕНИЯ = {"in": "входящий", "out": "исходящий", "any": ""}
#: Опрос по звонку старше этого не начинается: клиент уже не вспомнит разговор.
СРОК_ДАВНОСТИ_Ч = 24
#: Строка «звонит» старше этого — след упавшего сервера.
ЗАВИСШИЙ_ВЫЗОВ_С = 900
#: Коды SIP, которыми станция отказывает именно этому номеру: его нет (404,
#: 410, 484, 485, 604) или на него нельзя звонить (403). Повтор не поможет.
ОТКАЗ_НОМЕРУ = frozenset({403, 404, 410, 484, 485, 604})
#: Длины номера в режиме «только номера из 3 и 4 знаков».
КОРОТКИЙ_НОМЕР = (3, 4)
ПРЕДЕЛ_ГРУПП = 50
ПРЕДЕЛ_ВОПРОСОВ = 10

ПРИВЕТСТВИЕ = ("Здравствуйте! Вы недавно обращались в нашу компанию. "
               "Пожалуйста, оцените качество обслуживания: это займёт меньше минуты.")
ПРОЩАНИЕ = "Спасибо за вашу оценку! Всего доброго."
НЕ_ПОНЯЛ = "Ответ не распознан."
ОТКАЗ_ПРИНЯТ = "Мы больше не будем звонить вам с опросом. Всего доброго."


#: Виды вопросов: оценка кнопкой (или голосом) и свободный комментарий.
ОЦЕНКА, КОММЕНТАРИЙ = "scale", "comment"
#: Условия ветвления: ответ на прежний вопрос ниже, выше или равен числу.
УСЛОВИЯ = ("below", "above", "equals")
#: Числа словами — для голосовых ответов («пять», «пятёрка»).
ЧИСЛА_СЛОВАМИ = {
    "ноль": 0, "нуль": 0, "один": 1, "одна": 1, "единица": 1, "раз": 1, "два": 2, "две": 2,
    "двойка": 2, "три": 3, "тройка": 3, "четыре": 4, "четвёрка": 4, "четверка": 4,
    "пять": 5, "пятёрка": 5, "пятерка": 5, "шесть": 6, "шестёрка": 6, "шестерка": 6,
    "семь": 7, "семёрка": 7, "семерка": 7, "восемь": 8, "восьмёрка": 8, "восьмерка": 8,
    "девять": 9, "девятка": 9,
}


@dataclass(slots=True)
class Вопрос:
    text: str
    low: int = 1
    high: int = 5
    #: `scale` — оценка кнопкой (и голосом, если группа это разрешает);
    #: `comment` — свободный ответ голосом, распознаётся в текст.
    kind: str = ОЦЕНКА
    #: Ветвление: {"question": N, "below"|"above"|"equals": число} — вопрос
    #: задаётся, только если ответ на вопрос N подходит.
    when: dict[str, int] | None = None
    #: Свои слова для голосового ответа: {"да": 1, "нет": 2}.
    words: dict[str, int] = field(default_factory=dict)
    #: Сколько секунд слушать комментарий.
    seconds: int = 15

    def to_dict(self) -> dict[str, Any]:
        данные: dict[str, Any] = {"text": self.text, "min": self.low, "max": self.high}
        if self.kind != ОЦЕНКА:
            данные.update(kind=self.kind, seconds=self.seconds)
        if self.when:
            данные["when"] = dict(self.when)
        if self.words:
            данные["words"] = dict(self.words)
        return данные

    def задать_ли(self, ответы: list[dict[str, Any]]) -> bool:
        """Условие ветвления по уже полученным ответам."""
        if not self.when:
            return True
        номер = int(self.when.get("question") or 0) - 1
        if not 0 <= номер < len(ответы):
            return False
        цифра = ответы[номер].get("digit")
        if цифра is None:
            return False
        if "below" in self.when:
            return int(цифра) < int(self.when["below"])
        if "above" in self.when:
            return int(цифра) > int(self.when["above"])
        return int(цифра) == int(self.when.get("equals", -1))

    def цифра_из_речи(self, текст: str) -> int | None:
        """Голосовой ответ → кнопка шкалы: свои слова вопроса, затем числа."""
        слова = re.findall(r"[0-9]+|[а-яёa-z]+", str(текст or "").lower())
        свои = {str(к).lower(): int(з) for к, з in self.words.items()}
        for слово in слова:
            if слово in свои:
                return свои[слово]
        for слово in слова:
            число = int(слово) if слово.isdigit() else ЧИСЛА_СЛОВАМИ.get(слово)
            if число is not None and self.low <= число <= self.high:
                return число
        return None


@dataclass(slots=True)
class Группа:
    """Группа добавочных со своим текстом опроса."""

    name: str
    enabled: bool = True
    extensions: str = ""
    direction: str = "in"
    min_talk_s: int = 20
    sample_percent: int = 100
    greeting: str = ПРИВЕТСТВИЕ
    questions: list[Вопрос] = field(default_factory=list)
    goodbye: str = ПРОЩАНИЕ
    optout_digit: str = ""
    repeats: int = 1
    #: Отделы справочника сотрудников: добавочный сотрудника из такого отдела
    #: входит в группу и без перечисления номеров (см. `содержит`).
    departments: tuple[str, ...] = ()
    #: Принимать ответ голосом, если кнопку не нажали («пять», «да»).
    voice_answers: bool = False
    #: Образец голоса группы — имя загруженной аудиозаписи (иначе общий).
    voice_sample: str = ""
    _номера: frozenset[str] = frozenset()
    _отрезки: tuple[tuple[int, int, int], ...] = ()

    def содержит(self, добавочный: str, отдел: str = "") -> bool:
        добавочный = str(добавочный or "").strip()
        if not добавочный:
            return False
        if добавочный in self._номера:
            return True
        if отдел and self.departments and \
                " ".join(отдел.split()).lower() in self.departments:
            return True
        if добавочный.isdigit():
            число = int(добавочный)
            return any(начало <= число <= конец and len(добавочный) == длина
                       for начало, конец, длина in self._отрезки)
        return False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, "extensions": self.extensions,
                "direction": self.direction, "min_talk_s": self.min_talk_s,
                "sample_percent": self.sample_percent, "greeting": self.greeting,
                "questions": [в.to_dict() for в in self.questions],
                "goodbye": self.goodbye, "optout_digit": self.optout_digit,
                "repeats": self.repeats, "departments": list(self.departments),
                "voice_answers": self.voice_answers, "voice_sample": self.voice_sample}

    def фразы(self) -> list[str]:
        """Все тексты, которые понадобятся в разговоре, — для подготовки звука."""
        тексты = [self.greeting, self.goodbye, НЕ_ПОНЯЛ]
        тексты += [в.text for в in self.questions]
        if self.optout_digit:
            тексты.append(ОТКАЗ_ПРИНЯТ)
        return [т for т in тексты if т]


def _целое(значение: Any, запасное: int, низ: int, верх: int, что: str) -> int:
    if значение is None or значение == "":
        return запасное
    try:
        число = int(float(значение))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{что}: ожидается число, получено «{значение}»") from exc
    if not низ <= число <= верх:
        raise ValueError(f"{что}: от {низ} до {верх}, получено {число}")
    return число


def _да(значение: Any) -> bool:
    if isinstance(значение, str):
        return значение.strip().lower() not in ("0", "false", "no", "off", "нет", "")
    return bool(значение)


def _условие(значение: Any, прежние: list[Вопрос], где: str) -> dict[str, int] | None:
    """Условие ветвления вопроса — по ответу на один из прежних вопросов."""
    if значение in (None, "", {}):
        return None
    if not isinstance(значение, dict):
        raise ValueError(f"{где}: when — объект {{\"question\": 1, \"below\": 3}}")
    номер = _целое(значение.get("question"), 0, 1, ПРЕДЕЛ_ВОПРОСОВ, f"{где}, when.question")
    if номер > len(прежние) or прежние[номер - 1].kind != ОЦЕНКА:
        raise ValueError(f"{где}: when.question — номер одного из прежних вопросов с оценкой")
    виды = [в for в in УСЛОВИЯ if в in значение]
    if len(виды) != 1:
        raise ValueError(f"{где}: в when одно из below, above, equals")
    return {"question": номер, виды[0]: _целое(значение[виды[0]], 0, 0, 9, f"{где}, when")}


def _отделы(значение: Any) -> tuple[str, ...]:
    """Отделы группы: список или строка через запятую; без регистра."""
    куски = (значение if isinstance(значение, (list, tuple))
             else str(значение or "").split(","))
    return tuple(sorted({" ".join(str(к).split()).lower() for к in куски
                         if str(к).strip()}))[:50]


def разобрать_добавочные(текст: Any) -> tuple[frozenset[str], tuple[tuple[int, int, int], ...]]:
    """«101, 102, 200-210» — в набор номеров и отрезки. Ошибка — ValueError."""
    if isinstance(текст, (list, tuple)):
        текст = ", ".join(str(ч) for ч in текст)
    номера: set[str] = set()
    отрезки: list[tuple[int, int, int]] = []
    for кусок in re.split(r"[,;\s]+", str(текст or "").strip()):
        if not кусок:
            continue
        совпало = re.fullmatch(r"(\d{1,12})\s*[-–—]\s*(\d{1,12})", кусок)
        if совпало:
            начало, конец = совпало.group(1), совпало.group(2)
            if len(начало) != len(конец) or int(начало) > int(конец):
                raise ValueError(f"отрезок «{кусок}»: границы одной длины, первая не больше второй")
            if int(конец) - int(начало) > 100_000:
                raise ValueError(f"отрезок «{кусок}» слишком велик")
            отрезки.append((int(начало), int(конец), len(начало)))
        elif re.fullmatch(r"[0-9A-Za-z_*#.\-]{1,32}", кусок):
            номера.add(кусок)
        else:
            raise ValueError(f"«{кусок}» не похоже на добавочный номер")
    return frozenset(номера), tuple(отрезки)


def разобрать_группы(значение: Any) -> list[Группа]:
    """Список групп из настройки. Ошибка в записи — ValueError с её именем."""
    if значение in (None, ""):
        return []
    if isinstance(значение, str):
        try:
            значение = json.loads(значение)
        except ValueError as exc:
            raise ValueError(f"не читается как JSON: {exc}") from exc
    if not isinstance(значение, list):
        raise ValueError("ожидается список групп")
    if len(значение) > ПРЕДЕЛ_ГРУПП:
        raise ValueError(f"групп больше {ПРЕДЕЛ_ГРУПП}")
    группы: list[Группа] = []
    имена: set[str] = set()
    for номер, запись in enumerate(значение, 1):
        if not isinstance(запись, dict):
            raise ValueError(f"группа {номер}: ожидается объект с полями name, extensions, questions")
        имя = " ".join(str(запись.get("name") or "").split())[:80]
        где = f"группа «{имя}»" if имя else f"группа {номер}"
        if not имя:
            raise ValueError(f"{где}: не задано имя (name)")
        if имя.lower() in имена:
            raise ValueError(f"{где}: имя повторяется")
        имена.add(имя.lower())
        try:
            номера, отрезки = разобрать_добавочные(запись.get("extensions"))
        except ValueError as exc:
            raise ValueError(f"{где}: {exc}") from exc
        направление = str(запись.get("direction") or "in").strip().lower()
        if направление not in НАПРАВЛЕНИЯ:
            raise ValueError(f"{где}: direction — in, out или any")
        вопросы: list[Вопрос] = []
        сырые = запись.get("questions") or []
        if not isinstance(сырые, list):
            raise ValueError(f"{где}: questions — список вопросов")
        if len(сырые) > ПРЕДЕЛ_ВОПРОСОВ:
            raise ValueError(f"{где}: вопросов больше {ПРЕДЕЛ_ВОПРОСОВ} — столько по телефону не выдержат")
        for н, сырой in enumerate(сырые, 1):
            if isinstance(сырой, str):
                сырой = {"text": сырой}
            if not isinstance(сырой, dict):
                raise ValueError(f"{где}, вопрос {н}: ожидается текст или объект")
            текст = " ".join(str(сырой.get("text") or "").split())
            if not текст:
                raise ValueError(f"{где}, вопрос {н}: пустой текст")
            if len(текст) > tts.ДЛИНА_ФРАЗЫ and not текст.startswith(("file:", tts.ЗАГРУЖЕННАЯ)):
                raise ValueError(f"{где}, вопрос {н}: длиннее {tts.ДЛИНА_ФРАЗЫ} знаков")
            вид = str(сырой.get("kind") or ОЦЕНКА).strip().lower()
            if вид not in (ОЦЕНКА, КОММЕНТАРИЙ):
                raise ValueError(f"{где}, вопрос {н}: kind — scale или comment")
            низ = _целое(сырой.get("min"), 1, 0, 9, f"{где}, вопрос {н}, min")
            верх = _целое(сырой.get("max"), 5, 0, 9, f"{где}, вопрос {н}, max")
            if вид == ОЦЕНКА and низ >= верх:
                raise ValueError(f"{где}, вопрос {н}: min меньше max (кнопки от {низ} до {верх}?)")
            условие = _условие(сырой.get("when"), вопросы, f"{где}, вопрос {н}")
            слова = сырой.get("words") or {}
            if not isinstance(слова, dict) or any(
                    not str(з).isdigit() or not низ <= int(з) <= верх for з in слова.values()):
                raise ValueError(f"{где}, вопрос {н}: words — слова и кнопки шкалы "
                                 f"от {низ} до {верх}, например {{\"да\": 1, \"нет\": 2}}")
            вопросы.append(Вопрос(
                текст, низ, верх, kind=вид, when=условие,
                words={" ".join(str(к).lower().split()): int(з) for к, з in слова.items()},
                seconds=_целое(сырой.get("seconds"), 15, 3, 60, f"{где}, вопрос {н}, seconds")))
        отказ = str(запись.get("optout_digit") or "").strip()
        if отказ and отказ not in tuple("0123456789*#"):
            raise ValueError(f"{где}: optout_digit — одна кнопка телефона")
        if отказ and any(в.low <= int(отказ) <= в.high for в in вопросы
                         if отказ.isdigit() and в.kind == ОЦЕНКА):
            raise ValueError(f"{где}: кнопка отказа «{отказ}» попадает в шкалу оценок")
        включена = запись.get("enabled", True)
        if isinstance(включена, str):
            включена = включена.strip().lower() not in ("0", "false", "no", "off", "нет", "")
        группа = Группа(
            name=имя, enabled=bool(включена),
            extensions=", ".join(str(ч) for ч in запись.get("extensions"))
            if isinstance(запись.get("extensions"), (list, tuple))
            else str(запись.get("extensions") or "").strip(),
            direction=направление,
            min_talk_s=_целое(запись.get("min_talk_s"), 20, 0, 3600, f"{где}, min_talk_s"),
            sample_percent=_целое(запись.get("sample_percent"), 100, 1, 100,
                                  f"{где}, sample_percent"),
            greeting=" ".join(str(запись.get("greeting") or ПРИВЕТСТВИЕ).split()),
            questions=вопросы,
            goodbye=" ".join(str(запись.get("goodbye") or ПРОЩАНИЕ).split()),
            optout_digit=отказ,
            repeats=_целое(запись.get("repeats"), 1, 0, 3, f"{где}, repeats"),
            departments=_отделы(запись.get("departments")),
            voice_answers=_да(запись.get("voice_answers", False)),
            voice_sample=str(запись.get("voice_sample") or "").strip(),
        )
        if группа.voice_sample and not tts.имя_годится(группа.voice_sample):
            raise ValueError(f"{где}: voice_sample — имя загруженной аудиозаписи")
        for что, текст in (("приветствие", группа.greeting), ("прощание", группа.goodbye)):
            if len(текст) > tts.ДЛИНА_ФРАЗЫ and not текст.startswith(("file:", tts.ЗАГРУЖЕННАЯ)):
                raise ValueError(f"{где}: {что} длиннее {tts.ДЛИНА_ФРАЗЫ} знаков")
        группа._номера, группа._отрезки = номера, отрезки
        группы.append(группа)
    return группы


def проверить_группы(значение: Any) -> list[str]:
    """Для каталога параметров: перечень ошибок (пустой — всё в порядке)."""
    try:
        группы = разобрать_группы(значение)
    except ValueError as exc:
        return [str(exc)]
    замечания = []
    for группа in группы:
        if группа.enabled and not группа.questions:
            замечания.append(f"группа «{группа.name}»: нет ни одного вопроса")
        if группа.enabled and not (группа._номера or группа._отрезки or группа.departments):
            замечания.append(f"группа «{группа.name}»: не заданы ни добавочные номера, "
                             "ни отделы справочника")
    return замечания


def разобрать_часы(текст: Any) -> tuple[int, int]:
    """«09:00-20:00» — в минуты от полуночи. Пусто — круглые сутки."""
    текст = str(текст or "").strip()
    if not текст:
        return 0, 24 * 60
    совпало = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*[-–—]\s*(\d{1,2})(?::(\d{2}))?", текст)
    if not совпало:
        raise ValueError("ожидается вид «09:00-20:00»")
    начало = int(совпало.group(1)) * 60 + int(совпало.group(2) or 0)
    конец = int(совпало.group(3)) * 60 + int(совпало.group(4) or 0)
    if начало >= 24 * 60 or конец > 24 * 60 or int(совпало.group(2) or 0) > 59 \
            or int(совпало.group(4) or 0) > 59:
        raise ValueError("часы от 0 до 24, минуты от 0 до 59")
    if начало == конец:
        raise ValueError("начало и конец совпадают")
    return начало, конец


def в_часах(часы: tuple[int, int], момент: datetime) -> bool:
    начало, конец = часы
    минута = момент.hour * 60 + момент.minute
    if начало < конец:
        return начало <= минута < конец
    return минута >= начало or минута < конец       # окно через полночь


def чистый_номер(номер: Any) -> str:
    """Номер клиента для набора: только цифры (и ведущий «+» — отброшен)."""
    return re.sub(r"\D", "", str(номер or ""))


def _доля(ключ: str) -> int:
    """Число 0–99, постоянное для звонка: выборка не зависит от порядка и повтора."""
    return int(hashlib.sha256(ключ.encode("utf-8")).hexdigest()[:8], 16) % 100


def дни_обзвона(значение: Any) -> set[int]:
    """Дни недели обзвона (1 — понедельник … 7 — воскресенье); пусто — все."""
    дни: set[int] = set()
    куски = (значение if isinstance(значение, (list, tuple, set))
             else re.split(r"[,;\s]+", str(значение or "").strip()))
    for кусок in куски:
        от, _, до = str(кусок).partition("-")
        try:
            а, б = int(от), int(до or от)
        except ValueError:
            continue
        дни.update(д for д in range(min(а, б), max(а, б) + 1) if 1 <= д <= 7)
    return дни


def праздники(значение: Any) -> set[str]:
    """Даты без обзвона, «ГГГГ-ММ-ДД»; годовые «ММ-ДД» — на текущий год."""
    куски = (значение if isinstance(значение, (list, tuple, set))
             else re.split(r"[,;\s]+", str(значение or "").strip()))
    год = time.strftime("%Y")
    даты: set[str] = set()
    for кусок in куски:
        кусок = str(кусок).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", кусок):
            даты.add(кусок)
        elif re.fullmatch(r"\d{2}-\d{2}", кусок):
            даты.add(f"{год}-{кусок}")
    return даты


def длины_коротких(значение: Any) -> tuple[int, ...]:
    """Длины «коротких» номеров из настройки: «3,4», «2-5», [3, 4]."""
    if isinstance(значение, (list, tuple, set)):
        куски = [str(з) for з in значение]
    else:
        куски = re.split(r"[,;\s]+", str(значение or "").strip())
    длины: set[int] = set()
    for кусок in куски:
        if not кусок:
            continue
        от, _, до = кусок.partition("-")
        try:
            а, б = int(от), int(до or от)
        except ValueError:
            continue
        длины.update(range(max(1, min(а, б)), min(max(а, б), 15) + 1))
    return tuple(sorted(длины)) or КОРОТКИЙ_НОМЕР


def отобрать(звонок: dict[str, Any], группы: list[Группа], *,
             короткие: bool = False,
             длины: tuple[int, ...] = КОРОТКИЙ_НОМЕР,
             отделы: dict[str, str] | None = None) -> tuple[Группа, str, str] | str:
    """Нужен ли опрос по звонку: (группа, добавочный, номер клиента) или причина отказа.

    `короткие` — режим «перезванивать только на номера из 3 и 4 знаков»:
    опрашиваются собеседники с коротким (внутренним) номером, в том числе по
    внутренним звонкам, а городские и мобильные номера не набираются вовсе.
    """
    направление = str(звонок.get("direction") or "")
    внешний = направление in ("входящий", "исходящий")
    if not внешний and not короткие:
        return "не внешний звонок"
    if not int(звонок.get("answered") or 0):
        return "без ответа"
    добавочный = str(звонок.get("agent") or "").strip()
    if not добавочный:
        return "неизвестен добавочный"
    откуда, куда = чистый_номер(звонок.get("src")), чистый_номер(звонок.get("dst"))
    if направление == "входящий":
        клиент = откуда
    elif направление == "исходящий":
        клиент = куда
    else:
        # Внутренний звонок: собеседник — тот из двоих, кто не сотрудник группы.
        клиент = куда if откуда == добавочный else откуда
    if короткие:
        if len(клиент) not in длины:
            return ("номер не из 3–4 знаков" if длины == КОРОТКИЙ_НОМЕР
                    else f"номер не из {', '.join(map(str, длины))} знаков")
        if клиент == добавочный:
            return "нет номера клиента"
    elif len(клиент) < 5:
        return "нет номера клиента"
    for группа in группы:
        отдел = (отделы or {}).get(добавочный, "")
        if not (группа.enabled and группа.questions and группа.содержит(добавочный, отдел)):
            continue
        нужное = НАПРАВЛЕНИЯ[группа.direction]
        if нужное and внешний and нужное != направление:
            return "не то направление"
        if int(звонок.get("billsec") or 0) < группа.min_talk_s:
            return "короткий разговор"
        if группа.sample_percent < 100 and \
                _доля(str(звонок.get("uniqueid") or "")) >= группа.sample_percent:
            return "не попал в выборку"
        return группа, добавочный, клиент
    return "добавочный вне групп"


# ---------------------------------------------------------------------------
# Сценарий разговора
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Звук клиента: речь, автоответчик, голосовые ответы
# ---------------------------------------------------------------------------

#: Кадр разбора речи — 20 мс при 8 кГц, 16 бит.
_КАДР_РЕЧИ = 320
#: Порог громкости кадра речи (среднеквадратичное, из 32767): около −36 дБ.
ПОРОГ_РЕЧИ = 500


def кадры_речи(pcm: bytes) -> list[bool]:
    """Есть ли речь в каждом кадре 20 мс (по громкости)."""
    import array  # noqa: PLC0415

    итог: list[bool] = []
    for начало in range(0, len(pcm) - _КАДР_РЕЧИ + 1, _КАДР_РЕЧИ):
        отсчёты = array.array("h", pcm[начало:начало + _КАДР_РЕЧИ])
        мощность = sum(о * о for о in отсчёты) / max(1, len(отсчёты))
        итог.append(мощность > ПОРОГ_РЕЧИ * ПОРОГ_РЕЧИ)
    return итог


def _самая_длинная(кадры: list[bool]) -> int:
    лучшая = подряд = 0
    for речь in кадры:
        подряд = подряд + 1 if речь else 0
        лучшая = max(лучшая, подряд)
    return лучшая


def определить_автоответчик(разговор: Any, *, срок_с: float = 4.0) -> str:
    """Человек, автоответчик или тишина — по первым секундам после ответа.

    Человек говорит коротко («Алло», «Да») и замолкает, ожидая ответа.
    Автоответчик и голосовая почта читают длинное приветствие без пауз.
    Признаки: сплошная речь дольше 2,4 с — автоответчик; короткая реплика
    (до 1,8 с речи) и за ней 0,8 с тишины — человек; ни звука 2,5 с —
    тишина (опрос идёт как обычно: молча ждут и люди).
    """
    начало = разговор.сейчас_с()
    while разговор.жив:
        разговор.молчать(0.1)
        прошло = разговор.сейчас_с() - начало
        кадры = кадры_речи(разговор.звук_клиента(с=начало))
        речи = sum(кадры)
        if _самая_длинная(кадры) * 0.02 > 2.4:
            return "автоответчик"
        if речи:
            первая = кадры.index(True)
            последняя = len(кадры) - 1 - кадры[::-1].index(True)
            тишина_после = (len(кадры) - 1 - последняя) * 0.02
            if речи * 0.02 <= 1.8 and тишина_после >= 0.8 and последняя > первая:
                return "человек"
        elif прошло >= 2.5:
            return "тишина"
        if прошло >= срок_с:
            return "автоответчик" if речи * 0.02 > 2.0 else "человек"
    return "положили"


def _выслушать(разговор: Any, срок_с: float, *, тишина_с: float = 0.9,
               кнопки: bool = True) -> tuple[str, bytes]:
    """Ждёт кнопку или голосовой ответ: (кнопка, звук речи клиента).

    Голосом ответ кончается, когда после речи наступила тишина `тишина_с`;
    кнопка обрывает ожидание сразу.
    """
    начало = разговор.сейчас_с()
    конец = time.monotonic() + max(0.0, срок_с)
    while time.monotonic() < конец and разговор.жив:
        нажато = разговор.ждать_кнопку(0.2)
        if нажато and кнопки:
            return нажато, b""
        кадры = кадры_речи(разговор.звук_клиента(с=начало))
        if any(кадры):
            последняя = len(кадры) - 1 - кадры[::-1].index(True)
            if (len(кадры) - 1 - последняя) * 0.02 >= тишина_с:
                break
    звук = разговор.звук_клиента(с=начало)
    return "", (звук if sum(кадры_речи(звук)) * 0.02 >= 0.2 else b"")


def провести(разговор: Разговор, группа: Группа, звуки: dict[str, tts.Фраза], *,
             ждать_кнопку_с: float = 7.0,
             распознать: Callable[[bytes], str] | None = None,
             автоответчик: bool = False) -> dict[str, Any]:
    """Проговаривает опрос и собирает оценки.

    Ответ: `answers` — по записи на вопрос (digit — None, если не ответили;
    `skipped` — вопрос не задавался по условию ветвления; `comment` — текст
    комментария; `voice` — что клиент сказал вместо кнопки), `optout` —
    клиент нажал кнопку отказа, `hangup` — положил трубку раньше,
    `machine` — ответил автоответчик.

    `распознать` — PCM 16 бит 8 кГц → текст: для голосовых ответов и
    комментариев. Без него вопросы-комментарии пропускаются.
    """
    def сказать(текст: str, *, прерывать: bool = False) -> str:
        фраза = звуки.get(текст)
        if фраза is None or not разговор.жив:
            return ""
        return разговор.проиграть(фраза.звук(разговор.кодек), прерывать=прерывать)

    def в_текст(звук: bytes) -> str:
        if not звук or распознать is None:
            return ""
        try:
            return " ".join(str(распознать(звук) or "").split())
        except Exception as exc:                            # noqa: BLE001
            log.warning("Контроль качества: голосовой ответ не распознан: %s", exc)
            return ""

    ответы: list[dict[str, Any]] = [
        {"text": в.text, "min": в.low, "max": в.high, "digit": None,
         **({"kind": в.kind} if в.kind != ОЦЕНКА else {})} for в in группа.questions]
    отказ = False
    # Пауза после ответа: человек подносит трубку к уху, а станция ещё
    # достраивает звуковой канал — первые полсекунды иначе теряются.
    разговор.молчать(0.6)
    if автоответчик and определить_автоответчик(разговор) == "автоответчик":
        return {"answers": ответы, "optout": False, "hangup": False, "machine": True}
    разговор.сбросить_кнопки()
    сказать(группа.greeting)
    # Нажатое на приветствии ответом на первый вопрос не становится — кроме
    # кнопки отказа: её нажимают, не дослушав.
    while группа.optout_digit:
        нажато = разговор.ждать_кнопку(0)
        if not нажато:
            break
        if нажато == группа.optout_digit:
            отказ = True
    разговор.сбросить_кнопки()
    подряд_молчал = 0
    голосом = группа.voice_answers and распознать is not None
    for номер, вопрос in enumerate(группа.questions):
        if отказ or not разговор.жив:
            break
        if not вопрос.задать_ли(ответы):
            ответы[номер]["skipped"] = True
            continue
        if вопрос.kind == КОММЕНТАРИЙ:
            if распознать is None:
                ответы[номер]["skipped"] = True
                continue
            сказать(вопрос.text)
            _, звук = _выслушать(разговор, вопрос.seconds, тишина_с=2.0, кнопки=False)
            ответы[номер]["comment"] = в_текст(звук)
            continue
        получено: int | None = None
        for попытка in range(группа.repeats + 1):
            нажато = сказать(вопрос.text, прерывать=True)
            сказано = ""
            if not нажато:
                if голосом:
                    нажато, звук = _выслушать(разговор, ждать_кнопку_с)
                    сказано = в_текст(звук) if not нажато else ""
                else:
                    нажато = разговор.ждать_кнопку(ждать_кнопку_с)
            if not разговор.жив and not нажато and not сказано:
                break
            if нажато and нажато == группа.optout_digit:
                отказ = True
                break
            if нажато.isdigit() and вопрос.low <= int(нажато) <= вопрос.high:
                получено = int(нажато)
                break
            if сказано:
                ответы[номер]["voice"] = сказано
                цифра = вопрос.цифра_из_речи(сказано)
                if цифра is not None:
                    получено = цифра
                    break
            if попытка < группа.repeats and разговор.жив:
                if нажато or сказано:
                    сказать(НЕ_ПОНЯЛ)
                разговор.сбросить_кнопки()
        ответы[номер]["digit"] = получено
        if получено is None:
            подряд_молчал += 1
            # Два вопроса подряд без ответа — на той стороне автоответчик или
            # человек не слушает: дальше спрашивать незачем.
            if подряд_молчал >= 2:
                break
        else:
            подряд_молчал = 0
    положил = not разговор.жив
    if разговор.жив:
        сказать(ОТКАЗ_ПРИНЯТ if отказ else группа.goodbye)
        разговор.молчать(0.3)
    return {"answers": ответы, "optout": отказ, "hangup": положил}


def оценка(ответы: list[dict[str, Any]]) -> tuple[float | None, float | None, int]:
    """Средняя оценка, она же в процентах шкалы, и число ответов."""
    данные = [о for о in ответы if о.get("digit") is not None]
    if not данные:
        return None, None, 0
    средняя = sum(int(о["digit"]) for о in данные) / len(данные)
    доли = [(int(о["digit"]) - int(о["min"])) / max(1, int(о["max"]) - int(о["min"]))
            for о in данные]
    return round(средняя, 2), round(100 * sum(доли) / len(доли), 1), len(данные)


# ---------------------------------------------------------------------------
# Служба
# ---------------------------------------------------------------------------

class КонтрольКачества:
    """Фоновая служба опросов: поиск новых звонков и обзвон по очереди."""

    КЛЮЧ_ОТМЕТКИ = "qc:watermark"
    ШАГ_С = 5.0

    def __init__(self, db: Database, settings: Any) -> None:
        self.db = db
        self.settings = settings
        self._стоп = threading.Event()
        self._поток: threading.Thread | None = None
        self._замок = threading.Lock()
        self._телефон: Телефон | None = None
        self._подпись_телефона = ""
        self._идут: dict[str, threading.Thread] = {}
        self.регистрация: dict[str, Any] = {"state": "выключено", "error": "", "at": 0.0}
        self.последняя_ошибка = ""
        self._сбой_телефона_до = 0.0
        #: Сколько линий станция приняла разом и до какого времени это помнить.
        self._линий: tuple[int, float] = (0, 0.0)
        self._замок_линий = threading.Lock()
        #: PCM 16 бит 8 кГц → текст: голосовые ответы и комментарии. Ставит
        #: приложение (движок распознавания по умолчанию); None — без них.
        self.распознать: Callable[[bytes], str] | None = None
        self._уборка_записей = 0.0

    # -- настройки ---------------------------------------------------------

    def _число(self, ключ: str, запасное: float) -> float:
        try:
            return float(self.settings.get(ключ))
        except (TypeError, ValueError):
            return запасное

    @property
    def включён(self) -> bool:
        return bool(self.settings.get("qc_enabled"))

    def группы(self) -> list[Группа]:
        try:
            return разобрать_группы(self.settings.get("qc_groups"))
        except ValueError as exc:
            self.последняя_ошибка = f"группы контроля качества: {exc}"
            return []

    def каталог_звука(self) -> Any:
        return self.settings.paths.data / "qc" / "tts"

    def настроен(self) -> str:
        """Пусто — телефон настроен; иначе чего не хватает."""
        if not str(self.settings.get("qc_sip_host") or "").strip():
            return "не задан адрес станции (qc_sip_host)"
        if not str(self.settings.get("qc_sip_user") or "").strip():
            return "не задан номер телефона (qc_sip_user)"
        return ""

    # -- телефон -----------------------------------------------------------

    def _подпись(self) -> str:
        return "|".join(str(self.settings.get(к) or "") for к in (
            "qc_sip_host", "qc_sip_port", "qc_sip_user", "qc_sip_password",
            "qc_sip_local_ip", "qc_caller_name", "qc_sip_register"))

    def телефон(self) -> Телефон:
        """Телефон с действующей регистрацией; при смене настроек — новый."""
        with self._замок:
            подпись = self._подпись()
            if self._телефон is not None and подпись != self._подпись_телефона:
                self._закрыть_телефон()
            if self._телефон is None:
                чего_нет = self.настроен()
                if чего_нет:
                    raise ОшибкаSIP(f"Телефон не настроен: {чего_нет}.")
                self._телефон = Телефон(
                    str(self.settings.get("qc_sip_host")).strip(),
                    int(self._число("qc_sip_port", 5060)),
                    пользователь=str(self.settings.get("qc_sip_user")).strip(),
                    пароль=str(self.settings.get("qc_sip_password") or ""),
                    имя=str(self.settings.get("qc_caller_name") or ""),
                    местный=str(self.settings.get("qc_sip_local_ip") or ""))
                self._подпись_телефона = подпись
            телефон = self._телефон
            if bool(self.settings.get("qc_sip_register")):
                if телефон.зарегистрирован_до - time.time() < 60:
                    try:
                        принято = телефон.зарегистрировать()
                        self.регистрация = {"state": "зарегистрирован", "error": "",
                                            "at": time.time(), "expires": принято}
                    except ОшибкаSIP as exc:
                        self.регистрация = {"state": "отказ", "error": str(exc),
                                            "at": time.time()}
                        raise
            else:
                self.регистрация = {"state": "без регистрации", "error": "", "at": time.time()}
            return телефон

    def _закрыть_телефон(self) -> None:
        if self._телефон is not None:
            try:
                self._телефон.закрыть()
            except Exception as exc:                        # noqa: BLE001
                log.debug("Телефон закрыт с ошибкой: %s", exc)
            self._телефон = None

    def проверить_телефон(self) -> dict[str, Any]:
        """Регистрация прямо сейчас — для кнопки «Проверить телефон»."""
        try:
            with self._замок:
                self._закрыть_телефон()
            телефон = self.телефон()
            if not bool(self.settings.get("qc_sip_register")):
                # Без регистрации станцию проверяет сам запрос: отказ в
                # пароле или молчание видны так же.
                телефон.зарегистрировать(0)
            return {"ok": True, "state": self.регистрация.get("state"),
                    "local": f"{телефон.местный}:{телефон.местный_порт}",
                    "server": f"{телефон.узел}:{телефон.порт}"}
        except ОшибкаSIP as exc:
            return {"ok": False, "error": str(exc), "code": exc.код}

    # -- жизнь службы ------------------------------------------------------

    def start(self) -> None:
        if self._поток is not None and self._поток.is_alive():
            return
        self._стоп.clear()
        self._поток = threading.Thread(target=self._цикл, name="asrhub-qc", daemon=True)
        self._поток.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._стоп.set()
        if self._поток is not None:
            self._поток.join(timeout=timeout)
        for поток in list(self._идут.values()):
            поток.join(timeout=0.1)
        with self._замок:
            self._закрыть_телефон()

    def _цикл(self) -> None:
        while not self._стоп.wait(self.ШАГ_С):
            try:
                self.шаг()
            except Exception as exc:                        # noqa: BLE001
                self.последняя_ошибка = f"{type(exc).__name__}: {exc}"
                log.warning("Контроль качества: шаг не удался: %s", exc)

    def шаг(self) -> None:
        """Один проход: новые звонки — в очередь, готовые к обзвону — в работу."""
        if not self.включён:
            if self._телефон is not None and not self._идут:
                with self._замок:
                    self._закрыть_телефон()
                self.регистрация = {"state": "выключено", "error": "", "at": time.time()}
            return
        self._вернуть_зависшие()
        self._держать_регистрацию()
        self.найти_новые()
        self.обзвон()
        if time.time() - self._уборка_записей > 3600:
            self._уборка_записей = time.time()
            try:
                self.убрать_старые_записи()
            except Exception as exc:                        # noqa: BLE001
                log.debug("Старые записи опросов не убраны: %s", exc)

    def _держать_регистрацию(self) -> None:
        """Регистрация обновляется и между звонками.

        Раньше её продлевал только очередной вызов: в тихие часы она
        истекала, станция показывала номер недоступным, а раздел — всё ещё
        «зарегистрирован».
        """
        if not bool(self.settings.get("qc_sip_register")) or self.настроен():
            return
        if time.time() < self._сбой_телефона_до:
            return
        try:
            self.телефон()
        except ОшибкаSIP as exc:
            # Неверный пароль или станция недоступна: не долбить её каждые
            # пять секунд.
            self._сбой_телефона_до = time.time() + 120
            log.info("Контроль качества: регистрация не обновлена: %s", exc)

    # -- поиск новых звонков -----------------------------------------------

    def найти_новые(self, *, предел: int = 500) -> int:
        """Просматривает звонки, сохранённые после отметки, и заводит опросы."""
        группы = [г for г in self.группы() if г.enabled]
        отметка = self.db.get_kv(self.КЛЮЧ_ОТМЕТКИ)
        if отметка is None:
            # Первое включение: историю не обзваниваем.
            self.db.set_kv(self.КЛЮЧ_ОТМЕТКИ, time.time())
            return 0
        if not группы:
            self.db.set_kv(self.КЛЮЧ_ОТМЕТКИ, time.time())
            return 0
        строки = self.db.query(
            "SELECT * FROM calls WHERE imported_at > ? ORDER BY imported_at LIMIT ?",
            (float(отметка), предел))
        if not строки:
            return 0
        короткие = bool(self.settings.get("qc_short_numbers_only"))
        длины = длины_коротких(self.settings.get("qc_short_lengths") or "3,4")
        задержка = self._число("qc_delay_s", 300)
        пауза_дней = self._число("qc_cooldown_days", 30)
        сейчас = time.time()
        заведено = 0
        отделы: dict[str, str] = {}
        if any(г.departments for г in группы):
            # Отдел сотрудника — из справочника, одним запросом на порцию.
            люди = self.db.employees_by_ext([str(с["agent"] or "") for с in строки
                                             if с["agent"]])
            отделы = {доб: str(ч.get("department") or "") for доб, ч in люди.items()}
        for строка in строки:
            звонок = dict(строка)
            итог = отобрать(звонок, группы, короткие=короткие, длины=длины, отделы=отделы)
            if isinstance(итог, str):
                continue
            группа, добавочный, клиент = итог
            конец = float(звонок.get("started_at") or 0) + float(звонок.get("duration") or 0)
            if not конец or сейчас - конец > СРОК_ДАВНОСТИ_Ч * 3600:
                continue
            if self.в_стоп_листе(клиент):
                continue
            # Тот же номер — по последним десяти цифрам: «8…» и «+7…» — один клиент.
            if пауза_дней > 0 and self.db.query_one(
                    "SELECT 1 FROM qc_surveys WHERE (number=? OR (length(number)>=10 AND "
                    "substr(number, -10)=?)) AND kind='опрос' AND created_at>? "
                    "AND status NOT IN (?,?,?) LIMIT 1",
                    (клиент, клиент[-10:], сейчас - пауза_дней * 86400,
                     УСТАРЕЛ, ОТМЕНЁН, ОШИБКА)):
                continue
            заведено += self.db.execute(
                "INSERT OR IGNORE INTO qc_surveys (id, call_key, kind, group_name, ext, number, "
                "station, owner, job_id, direction, call_at, created_at, due_at, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("qc"), звонок["uniqueid"], "опрос", группа.name, добавочный, клиент,
                 звонок.get("station") or "", звонок.get("owner") or "", звонок.get("job_id"),
                 звонок.get("direction") or "", конец, сейчас,
                 max(сейчас, конец + задержка), ЖДЁТ))
        self.db.set_kv(self.КЛЮЧ_ОТМЕТКИ, float(строки[-1]["imported_at"]))
        return заведено

    def в_стоп_листе(self, номер: str) -> bool:
        номер = чистый_номер(номер)
        if not номер:
            return False
        # Сравнение по последним десяти цифрам: «8…», «7…» и «+7…» — один номер.
        хвост = номер[-10:]
        return bool(self.db.query_one(
            "SELECT 1 FROM qc_stoplist WHERE number=? OR (length(number)>=10 AND "
            "substr(number, -10)=?) LIMIT 1", (номер, хвост)))

    def _вернуть_зависшие(self) -> None:
        self.db.execute(
            "UPDATE qc_surveys SET status=?, error='сервер остановился во время вызова' "
            "WHERE status=? AND started_at < ?",
            (ЖДЁТ, ЗВОНИТ, time.time() - ЗАВИСШИЙ_ВЫЗОВ_С))

    # -- обзвон ------------------------------------------------------------

    def _часы(self) -> tuple[int, int]:
        try:
            return разобрать_часы(self.settings.get("qc_hours"))
        except ValueError:
            return 9 * 60, 20 * 60

    def _сейчас_местное(self) -> datetime:
        from ..telephony.stations import разобрать_пояс  # noqa: PLC0415

        try:
            пояс = разобрать_пояс(str(self.settings.get("qc_timezone") or ""))
        except ValueError:
            пояс = None
        return datetime.now(пояс) if пояс else datetime.now()

    def можно_звонить(self) -> bool:
        сейчас = self._сейчас_местное()
        if not в_часах(self._часы(), сейчас):
            return False
        # Дни недели и праздники: «с 9 до 20» в субботу и первого января —
        # звонок клиенту, которого никто не просил.
        дни = дни_обзвона(self.settings.get("qc_weekdays"))
        if дни and сейчас.isoweekday() not in дни:
            return False
        return сейчас.strftime("%Y-%m-%d") not in праздники(self.settings.get("qc_holidays"))

    def обзвон(self) -> int:
        """Берёт из очереди готовые опросы и звонит; ответ — сколько взято."""
        for ключ, поток in list(self._идут.items()):
            if not поток.is_alive():
                self._идут.pop(ключ, None)
        сейчас = time.time()
        # Устаревшие — закрываем, в какие бы часы ни шёл проход.
        self.db.execute(
            "UPDATE qc_surveys SET status=?, finished_at=? WHERE status=? AND kind='опрос' "
            "AND call_at < ?", (УСТАРЕЛ, сейчас, ЖДЁТ, сейчас - СРОК_ДАВНОСТИ_Ч * 3600))
        if not self.можно_звонить() or сейчас < self._сбой_телефона_до:
            return 0
        свободно = self._предел_линий() - len(self._идут)
        if свободно <= 0:
            return 0
        строки = self.db.query(
            "SELECT * FROM qc_surveys WHERE status=? AND due_at<=? ORDER BY due_at LIMIT ?",
            (ЖДЁТ, сейчас, свободно))
        взято = 0
        for строка in строки:
            if self._занять(строка["id"]):
                self._запустить(dict(строка))
                взято += 1
        return взято

    def _занять(self, номер: str) -> bool:
        """Берёт опрос в работу; False — его уже взял другой поток или сервер."""
        return self.db.execute(
            "UPDATE qc_surveys SET status=?, started_at=?, attempts=attempts+1 "
            "WHERE id=? AND status=?", (ЗВОНИТ, time.time(), номер, ЖДЁТ)) == 1

    def _запустить(self, опрос: dict[str, Any]) -> None:
        поток = threading.Thread(target=self._вызов, args=(опрос,),
                                 name="asrhub-qc-call", daemon=True)
        self._идут[опрос["id"]] = поток
        поток.start()

    def _закрыть(self, номер: str, статус: str, **поля: Any) -> None:
        поля = {"status": статус, **поля}
        if статус in ИТОГОВЫЕ:
            поля.setdefault("finished_at", time.time())
        self.db.execute(
            f"UPDATE qc_surveys SET {', '.join(f'{к}=?' for к in поля)} WHERE id=?",  # noqa: S608
            (*поля.values(), номер))
        if статус in ИТОГОВЫЕ and статус != ОТМЕНЁН:
            self._сообщить(номер)

    def _сообщить(self, номер: str) -> None:
        """Завершённый опрос — POST на qc_webhook_url (CRM, табло, чат).

        Отдельным потоком и с повторами: обзвон не ждёт чужой сервер, а
        оценка, которую приёмник не взял с первого раза, не теряется.
        Пробные звонки не уходят — это наладка, а не клиент.
        """
        адрес = str(self.settings.get("qc_webhook_url") or "").strip()
        if not адрес:
            return
        строка = self.db.query_one("SELECT * FROM qc_surveys WHERE id=?", (номер,))
        if строка is None or строка["kind"] != "опрос":
            return
        from ..job_queue import check_outbound_url  # noqa: PLC0415
        from ..maintenance import send_json  # noqa: PLC0415

        try:
            check_outbound_url(адрес, bool(self.settings.get("webhook_allow_internal")))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Адрес уведомлений об опросах отвергнут: %s", exc)
            return
        опрос = опрос_наружу(строка)
        тело = {"event": "qc_survey_finished", "survey": опрос,
                "text": (f"Опрос {опрос.get('group_name') or ''}, добавочный "
                         f"{опрос.get('ext') or '—'}: {опрос.get('status')}"
                         + (f", оценка {опрос['score']:g}" if опрос.get("score") is not None
                            else ""))}
        threading.Thread(target=send_json, args=(тело, адрес),
                         kwargs={"what": "опрос", "попыток": 3,
                                 "allow_internal": bool(
                                     self.settings.get("webhook_allow_internal"))},
                         name="asrhub-qc-notify", daemon=True).start()

    def _не_дозвонились(self, номер: str, попытка: int, проба: bool, exc: ОшибкаSIP, *,
                        итог: str = НЕ_ДОЗВОНИЛИСЬ) -> None:
        пределе = int(self._число("qc_max_attempts", 2))
        if попытка < пределе and not проба:
            self._закрыть(номер, ЖДЁТ, sip_code=exc.код, error=str(exc),
                          due_at=time.time() + 60 * self._число("qc_retry_min", 30))
        else:
            self._закрыть(номер, итог, sip_code=exc.код, error=str(exc))

    def _упёрлись_в_линии(self, exc: ОшибкаSIP) -> bool:
        """«Занято» при наших же идущих звонках — предел линий номера на станции.

        С `qc_parallel` больше одного станция с `call-limit=1` (или
        `max_contacts=1` у номера) отвечает на второй одновременный вызов
        «занято». Раньше это считалось занятым клиентом и тратило его
        попытку. Теперь предел запоминается на десять минут: обзвон идёт не
        больше линиями, чем станция приняла, а опрос встаёт обратно.
        """
        with self._замок_линий:
            других = sum(1 for поток in self._идут.values() if поток.is_alive()) - 1
            if других < 1:
                return False
            self._линий = (max(1, других), time.time() + 600)
        log.warning("Контроль качества: станция отвечает «занято» на %d-й одновременный "
                    "вызов — обзвон идёт не больше %d линиями десять минут (call-limit у "
                    "номера телефона?)", других + 1, других)
        return True

    def _предел_линий(self) -> int:
        предел = int(self._число("qc_parallel", 1))
        линий, до = self._линий
        return min(предел, линий) if time.time() < до else предел

    def _вызов(self, опрос: dict[str, Any]) -> None:
        """Один звонок клиенту от набора до итога в базе."""
        номер = опрос["id"]
        попытка = int(опрос.get("attempts") or 0) + 1
        проба = опрос.get("kind") == "проба"
        try:
            группа = next((г for г in self.группы() if г.name == опрос.get("group_name")), None)
            if группа is None or not группа.questions:
                self._закрыть(номер, ОШИБКА, error="группа удалена или в ней нет вопросов")
                return
            # Список «не звонить» — ещё раз перед набором: номер мог попасть в
            # него после того, как опрос встал в очередь (клиент отказался в
            # другом опросе, номер внесли руками в другом написании).
            if not проба and self.в_стоп_листе(опрос["number"]):
                self._закрыть(номер, ОТМЕНЁН, error="номер в списке «не звонить»")
                return
            # Звук готовится до набора: синтез занимает секунды, а в трубке
            # они были бы тишиной.
            звуки = {текст: tts.приготовить(текст, self.settings, self.каталог_звука(),
                                           образец_голоса=группа.voice_sample)
                     for текст in группа.фразы()}
            телефон = self.телефон()
            # Префикс выхода в город внутреннему номеру не нужен.
            набор = опрос["number"] if len(опрос["number"]) < 5 else \
                str(self.settings.get("qc_dial_prefix") or "").strip() + опрос["number"]
            разговор = телефон.позвонить(
                набор, ждать_ответа_с=self._число("qc_answer_timeout_s", 30))
        except Занято as exc:
            if self._упёрлись_в_линии(exc):
                # «Занято» при наших же идущих звонках — предел вызовов у
                # номера телефона на станции (call-limit, max_contacts), а не
                # клиент: попытку не тратим, опрос ждёт свободной линии.
                self.db.execute(
                    "UPDATE qc_surveys SET status=?, attempts=MAX(attempts-1,0), due_at=? "
                    "WHERE id=?", (ЖДЁТ, time.time() + 60, номер))
                return
            self._не_дозвонились(номер, попытка, проба, exc)
            return
        except НеОтвечает as exc:
            self._не_дозвонились(номер, попытка, проба, exc)
            return
        except ОшибкаSIP as exc:
            self.последняя_ошибка = str(exc)
            log.warning("Контроль качества: вызов %s не удался: %s", номер, exc)
            if проба or exc.код in ОТКАЗ_НОМЕРУ:
                # Отказ именно этому номеру (его нет, звонить на него нельзя)
                # — опрос закрывается. Раньше такой отказ считался сбоем
                # станции: опрос с неверным номером повторялся сутки и каждый
                # раз придерживал обзвон всех остальных на две минуты.
                self._закрыть(номер, ОШИБКА, sip_code=exc.код, error=str(exc))
            else:
                # Отказ станции (молчит, не принимает пароль, перегружена) —
                # не вина клиента: попытку не тратим, обзвон придерживаем,
                # чтобы не засыпать станцию отказами. Срок давности (сутки)
                # закроет опрос, если станция так и не ответит.
                self._сбой_телефона_до = time.time() + 120
                self.db.execute(
                    "UPDATE qc_surveys SET status=?, attempts=MAX(attempts-1,0), sip_code=?, "
                    "error=?, due_at=? WHERE id=?",
                    (ЖДЁТ, exc.код, str(exc), time.time() + 120, номер))
            return
        except ConfigError as exc:
            # Не готовится голос (нет синтезатора, сломан ffmpeg) — это
            # настройка сервера, а не клиент: опросы ждут, пока её исправят,
            # а не сгорают все разом «ошибкой». Срок давности (сутки) их
            # всё равно закроет.
            self.последняя_ошибка = exc.message
            if проба:
                self._закрыть(номер, ОШИБКА, error=exc.message)
            else:
                self._сбой_телефона_до = time.time() + 120
                self.db.execute(
                    "UPDATE qc_surveys SET status=?, attempts=MAX(attempts-1,0), error=?, "
                    "due_at=? WHERE id=?", (ЖДЁТ, exc.message, time.time() + 300, номер))
            return
        except Exception as exc:                            # noqa: BLE001
            self.последняя_ошибка = f"{type(exc).__name__}: {exc}"
            log.exception("Контроль качества: вызов %s упал", номер)
            self._закрыть(номер, ОШИБКА, error=self.последняя_ошибка)
            return
        ответил = time.time()
        self.db.execute("UPDATE qc_surveys SET answered_at=?, sip_code=200, error='' WHERE id=?",
                        (ответил, номер))
        автоответчик = bool(self.settings.get("qc_amd", False))
        записать = bool(self.settings.get("qc_record", False))
        распознать = self.распознать if (группа.voice_answers or any(
            в.kind == КОММЕНТАРИЙ for в in группа.questions)) else None
        if автоответчик or записать or распознать is not None:
            разговор.начать_запись()
        try:
            итог = провести(разговор, группа, звуки,
                            ждать_кнопку_с=self._число("qc_digit_timeout_s", 7),
                            распознать=распознать, автоответчик=автоответчик)
        except Exception as exc:                            # noqa: BLE001
            log.exception("Контроль качества: разговор %s прерван ошибкой", номер)
            итог = {"answers": [], "optout": False, "hangup": True, "error": str(exc)}
        finally:
            разговор.положить()
        запись = self._сохранить_запись(номер, разговор) if записать else ""
        if итог.get("machine"):
            self._не_дозвонились(номер, попытка, проба, ОшибкаSIP("ответил автоответчик"),
                                 итог=АВТООТВЕТЧИК)
            if запись:
                self.db.execute("UPDATE qc_surveys SET recording=? WHERE id=?", (запись, номер))
            return
        средняя, процент, ответов = оценка(итог["answers"])
        # Ждали ответа только на заданные вопросы с оценкой: пропущенные по
        # ветвлению и комментарии «пройден» не портят.
        ждали = sum(1 for о in итог["answers"]
                    if not о.get("skipped") and о.get("kind", ОЦЕНКА) == ОЦЕНКА)
        if итог["optout"]:
            статус = ОТКАЗ
            if not проба:
                self.в_стоп_лист(опрос["number"], "клиент отказался от опросов", номер)
        elif ответов and ответов >= ждали:
            статус = ПРОЙДЕН
        elif ответов:
            статус = ЧАСТИЧНО
        else:
            статус = БЕЗ_ОТВЕТА
        self._закрыть(номер, статус, answers=json.dumps(итог["answers"], ensure_ascii=False),
                      score=средняя, score_pct=процент,
                      talk_s=round(time.time() - ответил, 1),
                      error=str(итог.get("error") or ""), recording=запись)

    def каталог_записей_опросов(self) -> Path:
        return Path(self.settings.paths.results) / "qc"

    def _сохранить_запись(self, номер: str, разговор: Разговор) -> str:
        """Запись опроса в WAV (слева клиент, справа сервер); путь или ''."""
        import struct  # noqa: PLC0415

        try:
            данные = разговор.запись_стерео()
            if not данные:
                return ""
            каталог = self.каталог_записей_опросов()
            каталог.mkdir(parents=True, exist_ok=True)
            путь = каталог / f"{номер}.wav"
            заголовок = (b"RIFF" + struct.pack("<I", 36 + len(данные)) + b"WAVEfmt "
                         + struct.pack("<IHHIIHH", 16, 1, 2, 8000, 32000, 4, 16)
                         + b"data" + struct.pack("<I", len(данные)))
            путь.write_bytes(заголовок + данные)
            return str(путь)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Контроль качества: запись опроса %s не сохранена: %s", номер, exc)
            return ""

    def убрать_старые_записи(self) -> int:
        """Записи опросов старше `qc_record_days` — с диска и из строки опроса."""
        дней = self._число("qc_record_days", 30)
        if дней <= 0:
            return 0
        срок = time.time() - дней * 86400
        убрано = 0
        for строка in self.db.query(
                "SELECT id, recording FROM qc_surveys WHERE COALESCE(recording,'')<>'' "
                "AND COALESCE(finished_at, created_at) < ?", (срок,)):
            Path(строка["recording"]).unlink(missing_ok=True)
            self.db.execute("UPDATE qc_surveys SET recording='' WHERE id=?", (строка["id"],))
            убрано += 1
        return убрано

    # -- ручные действия ---------------------------------------------------

    def пробный_звонок(self, номер: str, группа: str, *, owner: str = "") -> dict[str, Any]:
        """Звонит на номер сейчас — мимо часов, паузы и списка «не звонить»."""
        клиент = чистый_номер(номер)
        if len(клиент) < 2:
            raise ConfigError("Номер для пробного звонка не задан.",
                              hint="Введите внутренний или городской номер цифрами.")
        найдена = next((г for г in self.группы() if г.name == группа), None)
        if найдена is None:
            raise ConfigError(f"Группа «{группа}» не найдена.",
                              hint="Сохраните группу и повторите.")
        if not найдена.questions:
            raise ConfigError(f"В группе «{группа}» нет вопросов.",
                              hint="Добавьте хотя бы один вопрос и сохраните группу.")
        чего_нет = self.настроен()
        if чего_нет:
            raise ConfigError(f"Телефон не настроен: {чего_нет}.",
                              hint="Заполните адрес станции, номер и пароль телефона.")
        сейчас = time.time()
        опрос = {"id": new_id("qc"), "kind": "проба", "group_name": найдена.name,
                 "number": клиент, "owner": owner, "attempts": 0}
        self.db.execute(
            "INSERT INTO qc_surveys (id, call_key, kind, group_name, number, owner, call_at, "
            "created_at, due_at, started_at, status, attempts) VALUES (?,?,?,?,?,?,?,?,?,?,?,1)",
            (опрос["id"], None, "проба", найдена.name, клиент, owner, сейчас, сейчас,
             сейчас, сейчас, ЗВОНИТ))
        self._запустить(опрос)
        return {"id": опрос["id"], "number": клиент, "group": найдена.name}

    def отменить(self, номер: str) -> bool:
        return self.db.execute(
            "UPDATE qc_surveys SET status=?, finished_at=? WHERE id=? AND status=?",
            (ОТМЕНЁН, time.time(), номер, ЖДЁТ)) == 1

    def в_стоп_лист(self, номер: str, причина: str = "", опрос: str = "") -> bool:
        клиент = чистый_номер(номер)
        if len(клиент) < 3:
            raise ConfigError("Номер слишком короткий.", hint="Ожидается номер цифрами.")
        self.db.execute(
            "INSERT OR REPLACE INTO qc_stoplist (number, added_at, reason, survey_id) "
            "VALUES (?,?,?,?)", (клиент, time.time(), причина[:200], опрос))
        # Ждущие опросы этого номера снимаются сразу — в любом написании
        # («8…», «7…», «+7…»): сравнение, как и везде, по последним десяти.
        self.db.execute(
            "UPDATE qc_surveys SET status=?, finished_at=?, error='номер в списке «не звонить»' "
            "WHERE (number=? OR (length(number)>=10 AND substr(number, -10)=?)) AND status=?",
            (ОТМЕНЁН, time.time(), клиент, клиент[-10:], ЖДЁТ))
        return True

    def из_стоп_листа(self, номер: str) -> bool:
        return self.db.execute("DELETE FROM qc_stoplist WHERE number=?",
                               (чистый_номер(номер),)) == 1

    # -- состояние и отчёт -------------------------------------------------

    def состояние(self) -> dict[str, Any]:
        счёт = {с["status"]: int(с["n"]) for с in self.db.query(
            "SELECT status, COUNT(*) AS n FROM qc_surveys WHERE kind='опрос' GROUP BY status")}
        ближайший = self.db.query_one(
            "SELECT MIN(due_at) AS t FROM qc_surveys WHERE status=?", (ЖДЁТ,))
        try:
            группы = разобрать_группы(self.settings.get("qc_groups"))
            ошибка_групп = ""
        except ValueError as exc:
            группы, ошибка_групп = [], str(exc)
        try:
            часы = разобрать_часы(self.settings.get("qc_hours"))
            ошибка_часов = ""
        except ValueError as exc:
            часы, ошибка_часов = (9 * 60, 20 * 60), str(exc)
        return {
            "enabled": self.включён,
            "configured": not self.настроен(),
            "missing": self.настроен(),
            "running": bool(self._поток and self._поток.is_alive()),
            "registration": dict(self.регистрация),
            "tts": tts.движок(self.settings, tts.образец(self.каталог_звука())),
            "groups": len(группы),
            "groups_active": sum(1 for г in группы if г.enabled and г.questions),
            "groups_error": ошибка_групп,
            "hours": {"from": часы[0], "to": часы[1], "open": self.можно_звонить(),
                      "error": ошибка_часов,
                      "now": self._сейчас_местное().strftime("%H:%M")},
            "counts": счёт,
            "waiting": счёт.get(ЖДЁТ, 0),
            "calling": len([п for п in self._идут.values() if п.is_alive()]),
            "next_due": (ближайший or {})["t"] if ближайший else None,
            "stoplist": int((self.db.query_one(
                "SELECT COUNT(*) AS n FROM qc_stoplist") or {"n": 0})["n"]),
            "last_error": self.последняя_ошибка,
        }


def опрос_наружу(строка: Any) -> dict[str, Any]:
    данные = dict(строка)
    try:
        данные["answers"] = json.loads(данные.get("answers") or "[]")
    except ValueError:
        данные["answers"] = []
    # Путь на диске сервера наружу не уходит — только признак, что запись есть.
    запись = str(данные.pop("recording", "") or "")
    данные["has_recording"] = bool(запись) and Path(запись).is_file()
    return данные


def сводка(db: Database, *, since: float, until: float | None = None,
           группа: str = "", владельцы: list[str] | None = None) -> dict[str, Any]:
    """Оценки за период: всего, по группам, по добавочным и по вопросам.

    `владельцы` — только опросы по звонкам этих владельцев (руководитель
    отдела видит своё подразделение)."""
    условия = ["kind='опрос'", "created_at>=?"]
    параметры: list[Any] = [since]
    if владельцы is not None:
        условия.append("owner IN (" + (",".join("?" * len(владельцы)) or "''") + ")")
        параметры.extend(владельцы)
    if until:
        условия.append("created_at<?")
        параметры.append(until)
    if группа:
        условия.append("group_name=?")
        параметры.append(группа)
    строки = [опрос_наружу(с) for с in db.query(
        f"SELECT * FROM qc_surveys WHERE {' AND '.join(условия)}", параметры)]  # noqa: S608

    def свод(набор: list[dict[str, Any]]) -> dict[str, Any]:
        закрыто = [о for о in набор if о["status"] in ИТОГОВЫЕ
                   and о["status"] not in (УСТАРЕЛ, ОТМЕНЁН)]
        ответили = [о for о in набор if о["status"] in ДОЗВОНИЛИСЬ]
        оценили = [о for о in набор if о["status"] in С_ОЦЕНКОЙ and о.get("score") is not None]
        return {
            "surveys": len(набор),
            "waiting": sum(1 for о in набор if о["status"] in (ЖДЁТ, ЗВОНИТ)),
            "finished": len(закрыто),
            "answered": len(ответили),
            "rated": len(оценили),
            "optout": sum(1 for о in набор if о["status"] == ОТКАЗ),
            "answer_rate": round(100 * len(ответили) / len(закрыто), 1) if закрыто else None,
            "response_rate": round(100 * len(оценили) / len(закрыто), 1) if закрыто else None,
            "score": round(sum(о["score"] for о in оценили) / len(оценили), 2) if оценили else None,
            "score_pct": (round(sum(о["score_pct"] or 0 for о in оценили) / len(оценили), 1)
                          if оценили else None),
        }

    по_группам: dict[str, list[dict[str, Any]]] = {}
    по_номерам: dict[str, list[dict[str, Any]]] = {}
    вопросы: dict[tuple[str, str], dict[str, Any]] = {}
    for опрос in строки:
        по_группам.setdefault(опрос["group_name"], []).append(опрос)
        по_номерам.setdefault(опрос["ext"], []).append(опрос)
        for ответ in опрос["answers"]:
            ключ = (опрос["group_name"], str(ответ.get("text") or ""))
            запись = вопросы.setdefault(ключ, {
                "group": ключ[0], "text": ключ[1], "min": ответ.get("min"),
                "max": ответ.get("max"), "asked": 0, "answered": 0, "sum": 0,
                "distribution": {}})
            запись["asked"] += 1
            if ответ.get("digit") is not None:
                цифра = int(ответ["digit"])
                запись["answered"] += 1
                запись["sum"] += цифра
                запись["distribution"][str(цифра)] = запись["distribution"].get(str(цифра), 0) + 1
    люди = db.employees_by_ext([н for н in по_номерам if н])
    добавочные = []
    for номер, набор in по_номерам.items():
        человек = люди.get(номер) or {}
        имя = " ".join(ч for ч in (человек.get("last_name"), человек.get("first_name")) if ч)
        добавочные.append({"ext": номер, "name": имя,
                           "department": человек.get("department") or "",
                           "group": набор[0]["group_name"], **свод(набор)})
    добавочные.sort(key=lambda з: (-(з["rated"]), з["ext"]))
    список_вопросов = []
    for запись in вопросы.values():
        запись["score"] = round(запись["sum"] / запись["answered"], 2) if запись["answered"] else None
        запись.pop("sum")
        список_вопросов.append(запись)
    return {
        "since": since, "until": until,
        "total": свод(строки),
        "groups": [{"group": имя, **свод(набор)} for имя, набор in sorted(по_группам.items())],
        "extensions": добавочные,
        "questions": список_вопросов,
    }
