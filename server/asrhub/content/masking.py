"""Маскирование персональных данных в текстах расшифровок.

Расшифровка разговора с клиентом — это персональные данные, и чаще всего
не те, ради которых её читают: номер карты, продиктованный оператору,
нужен платёжной системе, а не аналитику. Здесь — обезличивание по форме:
телефоны, почта, номера карт, паспорт, СНИЛС, ИНН и даты рождения
заменяются пометками вида «[карта]». Всё на правилах и контрольных
суммах — карта по Луну, СНИЛС и ИНН по своим алгоритмам, — поэтому
случайные десятизначные числа за ИНН не принимаются, а паспорт и дата
рождения берутся только рядом со словами, которые их называют: голые
«4512 345678» или «12.05.1980» в разговоре означают что угодно.

Что не делается: имена, адреса и числа, произнесённые словами. Первые
правилами не берутся честно (см. `entities`), вторые требуют разбора речи,
которого здесь нет. Маскирование — защита выгрузки, а не гарантия
обезличивания; документация говорит об этом прямо.

Две точки применения: выгрузки (настройка `export_mask_pii`) и ответы
API ключу с флагом `mask_pii` — ключ аналитика или интеграции, которой
текст нужен, а персональные данные — нет.
"""
from __future__ import annotations

import re
from typing import Any

from .entities import _ПОЧТА, _ТЕЛЕФОН

#: Чем заменяется найденное — по видам.
ПОМЕТКИ: dict[str, str] = {
    "phone": "[телефон]", "email": "[почта]", "card": "[карта]",
    "passport": "[паспорт]", "snils": "[СНИЛС]", "inn": "[ИНН]",
    "birthdate": "[дата рождения]",
}

#: Международный номер: плюс, код страны и 9–14 цифр с разделителями.
_ТЕЛЕФОН_МЕЖД = re.compile(r"(?<![\d\w])\+\d[\d\s\-()]{8,16}\d(?!\d)")

#: Номер карты: 13–19 цифр с пробелами или дефисами; подтверждается Луном.
_КАРТА = re.compile(r"(?<!\d)(?:\d[ \-]?){12,18}\d(?!\d)")

#: СНИЛС: 123-456-789 01 (разделители необязательны); подтверждается суммой.
_СНИЛС = re.compile(r"(?<!\d)\d{3}[- ]?\d{3}[- ]?\d{3}[- ]?\d{2}(?!\d)")

#: ИНН: ровно 10 или 12 цифр; подтверждается контрольными цифрами.
_ИНН = re.compile(r"(?<![\d\-])(?:\d{12}|\d{10})(?![\d\-])")

#: Паспорт — только рядом с называющим словом: «паспорт 45 12 345678»,
#: «серия 4512 номер 345678», «паспортные данные: 4512 345678».
_ПАСПОРТ = re.compile(
    r"(паспорт\w*(?:\s+\w+){0,3}?[\s:№#-]*|сери[яи]\s*)"
    r"(\d{2}\s?\d{2})[\s,]*(?:номер|№|n)?[\s:]*(\d{6})(?!\d)", re.IGNORECASE)

#: Дата рождения — только рядом с называющим словом.
_ДАТА = (r"(\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}"
         r"|\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|"
         r"октябр|ноябр|декабр)\w*(?:\s+\d{4})?(?:\s+года?)?)")
_ДАТА_РОЖДЕНИЯ = re.compile(
    r"((?:дата|день|год[а]?)\s+рождения|родил[ас][сья]+|д\.?\s?р\.?)[\s:—-]*(?:\w+\s+){0,2}?"
    + _ДАТА, re.IGNORECASE)

#: Ключи ответов, где живёт текст, и ключи со списками найденных сущностей.
TEXT_KEYS = frozenset({"text", "word", "reference_text", "filename", "message",
                       "ref", "hyp", "raw", "excerpt", "snippet", "quote", "title"})
LIST_KEYS = frozenset({"phones", "emails"})


def luhn(цифры: str) -> bool:
    """Контрольная сумма Луна — по ней проверяются номера карт."""
    total = 0
    for i, ch in enumerate(reversed(цифры)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def snils_ok(цифры: str) -> bool:
    """Контрольное число СНИЛС по правилам ПФР."""
    if len(цифры) != 11:
        return False
    номер, контроль = цифры[:9], int(цифры[9:])
    # Номера до 001-001-998 контрольным числом не проверяются.
    if int(номер) <= 1001998:
        return False
    сумма = sum(int(d) * (9 - i) for i, d in enumerate(номер))
    if сумма < 100:
        ожидается = сумма
    elif сумма in (100, 101):
        ожидается = 0
    else:
        ожидается = сумма % 101
        if ожидается in (100, 101):
            ожидается = 0
    return ожидается == контроль


def inn_ok(цифры: str) -> bool:
    """Контрольные цифры ИНН: одна у организаций (10), две у людей (12)."""
    d = [int(ch) for ch in цифры]
    if len(d) == 10:
        веса = (2, 4, 10, 3, 5, 9, 4, 6, 8)
        return d[9] == sum(w * x for w, x in zip(веса, d, strict=False)) % 11 % 10
    if len(d) == 12:
        в11 = (7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        в12 = (3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8)
        return (d[10] == sum(w * x for w, x in zip(в11, d, strict=False)) % 11 % 10
                and d[11] == sum(w * x for w, x in zip(в12, d, strict=False)) % 11 % 10)
    return False


def find(text: str) -> list[dict[str, Any]]:
    """Все персональные данные в тексте: вид, границы, значение.

    Порядок видов — по надёжности: почта и телефон, потом карта, СНИЛС,
    ИНН, паспорт, дата рождения. Наложения не допускаются: цифры,
    ставшие телефоном, за карту уже не сойдут.
    """
    if not text:
        return []
    занято: list[tuple[int, int]] = []
    out: list[dict[str, Any]] = []

    def взять(kind: str, start: int, end: int, value: str) -> None:
        if any(s < end and start < e for s, e in занято):
            return
        занято.append((start, end))
        out.append({"kind": kind, "start": start, "end": end, "value": value})

    for m in _ПОЧТА.finditer(text):
        взять("email", m.start(), m.end(), m.group(0))
    for выражение in (_ТЕЛЕФОН, _ТЕЛЕФОН_МЕЖД):
        for m in выражение.finditer(text):
            взять("phone", m.start(), m.end(), m.group(0))
    for m in _КАРТА.finditer(text):
        цифры = re.sub(r"\D", "", m.group(0))
        if 13 <= len(цифры) <= 19 and luhn(цифры):
            взять("card", m.start(), m.end(), m.group(0))
    for m in _СНИЛС.finditer(text):
        if snils_ok(re.sub(r"\D", "", m.group(0))):
            взять("snils", m.start(), m.end(), m.group(0))
    for m in _ИНН.finditer(text):
        if inn_ok(m.group(0)):
            взять("inn", m.start(), m.end(), m.group(0))
    for m in _ПАСПОРТ.finditer(text):
        взять("passport", m.start(2), m.end(3), text[m.start(2):m.end(3)])
    for m in _ДАТА_РОЖДЕНИЯ.finditer(text):
        взять("birthdate", m.start(2), m.end(2), m.group(2))
    out.sort(key=lambda x: x["start"])
    return out


def mask_text(text: str) -> str:
    """Текст с пометками вместо персональных данных."""
    найдено = find(text)
    if not найдено:
        return text
    части: list[str] = []
    позиция = 0
    for н in найдено:
        части.append(text[позиция:н["start"]])
        части.append(ПОМЕТКИ[н["kind"]])
        позиция = н["end"]
    части.append(text[позиция:])
    return "".join(части)


def count(text: str) -> dict[str, int]:
    """Сколько чего найдено — для проверок и для отчёта об обезличивании."""
    итог: dict[str, int] = {}
    for н in find(text):
        итог[н["kind"]] = итог.get(н["kind"], 0) + 1
    return итог


def mask_payload(данные: Any) -> Any:
    """Обезличивает ответ API: строки под текстовыми ключами и списки
    найденных телефонов и адресов; остальное — как есть."""
    if isinstance(данные, dict):
        out: dict[str, Any] = {}
        for k, v in данные.items():
            if k in TEXT_KEYS and isinstance(v, str):
                out[k] = mask_text(v)
            elif k in LIST_KEYS and isinstance(v, list):
                out[k] = [ПОМЕТКИ["phone" if k == "phones" else "email"]
                          if isinstance(x, str) else mask_payload(x) for x in v]
            elif k == "numbers" and isinstance(v, list):
                out[k] = [{**x, "number": "[номер]"} if isinstance(x, dict) else x for x in v]
            else:
                out[k] = mask_payload(v)
        return out
    if isinstance(данные, list):
        return [mask_payload(x) for x in данные]
    return данные
