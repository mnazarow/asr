"""Проверка регулярных выражений, которые сервер исполняет по чужой просьбе.

Модуль `re` при катастрофическом откате не отпускает GIL и не прерывается:
выражение «(\\w+\\s?)+$» на строке из тридцати слов без совпадения считается
часами, и всё это время стоит весь процесс — HTTP, отметка жизни, воркеры.
Предела времени у `re` нет, а сторонний модуль `regex` с пределом в
зависимостях не числится. Поэтому выражение проверяется до того, как его
примут, и отвергаются те виды, на которых откат растёт экспоненциально.

Откат взрывается там, где повтор можно разрезать на итерации многими
способами: внутри повторяемого куска есть свой повтор или выбор, и конец
одной итерации может оказаться началом следующей. «(\\w+\\s?)+» — пробел
необязателен, и «абв» — это одна итерация, две или три. «(\\s\\w+)*» —
безопасно: каждая итерация обязана начаться с пробела, а слово пробел не
съест, граница одна. Проверка так и устроена: у повторяемого куска со
своим повтором или выбором сравниваются знаки, которыми он может начаться и
кончиться; пересеклись — выражение отвергается.

Проверка приблизительная и осторожная: знаки сравниваются по выборке
алфавита, сомнительное считается опасным. Квадратичные выражения
(«\\s*x\\s*» на длинной строке пробелов) она пропускает: на одной реплике
— сотни знаков — они стоят миллисекунды.
"""
from __future__ import annotations

import re
import string
from typing import Any

# Разборщик выражений — внутренний модуль `re`: в Python 3.11 он переехал
# из `sre_parse` в `re._parser`, прежнее имя оставлено с предупреждением.
try:
    from re import _constants as _sre  # type: ignore[attr-defined]
    from re import _parser as _разбор  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — Python 3.10
    import sre_constants as _sre  # type: ignore[no-redef]
    import sre_parse as _разбор  # type: ignore[no-redef]

#: Длиннее этого выражение в словаре замен не бывает по делу.
ПРЕДЕЛ_ДЛИНЫ = 500

#: Выборка алфавита, по которой сравниваются наборы знаков. Классы
#: выражений бывают любые, а нам нужно одно: могут ли два набора совпасть
#: хотя бы на одном знаке. Для этого хватает представителей всех видов.
_ВЫБОРКА = frozenset(string.printable + "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
                     "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ —«»№")

_ПОВТОРЫ = tuple(op for op in (_sre.MAX_REPEAT, _sre.MIN_REPEAT,
                               getattr(_sre, "POSSESSIVE_REPEAT", None)) if op is not None)
_НУЛЕВЫЕ = tuple(op for op in (_sre.AT, _sre.ASSERT, _sre.ASSERT_NOT) if op is not None)

_КАТЕГОРИИ = {
    _sre.CATEGORY_DIGIT: re.compile(r"\d"), _sre.CATEGORY_NOT_DIGIT: re.compile(r"\D"),
    _sre.CATEGORY_SPACE: re.compile(r"\s"), _sre.CATEGORY_NOT_SPACE: re.compile(r"\S"),
    _sre.CATEGORY_WORD: re.compile(r"\w"), _sre.CATEGORY_NOT_WORD: re.compile(r"\W"),
}


def _набор_класса(элементы: Any) -> frozenset[str]:
    """Какие знаки выборки берёт класс «[…]»."""
    отрицание = False
    итог: set[str] = set()
    for op, av in элементы:
        if op == _sre.NEGATE:
            отрицание = True
        elif op == _sre.LITERAL:
            итог |= {chr(av).lower(), chr(av).upper()}
        elif op == _sre.RANGE:
            нижний, верхний = av
            итог |= {з for з in _ВЫБОРКА
                     if нижний <= ord(з) <= верхний
                     or нижний <= ord(з.lower()) <= верхний
                     or нижний <= ord(з.upper()) <= верхний}
        elif op == _sre.CATEGORY and av in _КАТЕГОРИИ:
            итог |= {з for з in _ВЫБОРКА if _КАТЕГОРИИ[av].match(з)}
        else:
            return _ВЫБОРКА          # непонятное — считаем «что угодно»
    итог &= _ВЫБОРКА
    return frozenset(_ВЫБОРКА - итог) if отрицание else frozenset(итог)


def _знаки(op: Any, av: Any) -> frozenset[str] | None:
    """Набор одного знакового узла; None — узел не знаковый."""
    if op == _sre.LITERAL:
        return frozenset({chr(av).lower(), chr(av).upper()})
    if op == _sre.NOT_LITERAL:
        return frozenset(_ВЫБОРКА - {chr(av).lower(), chr(av).upper()})
    if op == _sre.ANY:
        return frozenset(_ВЫБОРКА - {"\n"})
    if op == _sre.IN:
        return _набор_класса(av)
    if op == _sre.CATEGORY:
        шаблон = _КАТЕГОРИИ.get(av)
        return frozenset(з for з in _ВЫБОРКА if шаблон.match(з)) if шаблон else _ВЫБОРКА
    return None


def _ветви(op: Any, av: Any) -> list[Any]:
    """Вложенные последовательности узла разбора."""
    if op in _ПОВТОРЫ:
        return [av[2]]
    if op == _sre.SUBPATTERN:
        return [av[-1]]
    if op == _sre.BRANCH:
        return list(av[1])
    if op in (_sre.ASSERT, _sre.ASSERT_NOT):
        return [av[1]]
    if op == getattr(_sre, "ATOMIC_GROUP", None):
        return [av]
    if op == _sre.GROUPREF_EXISTS:
        return [ветка for ветка in av[1:] if ветка is not None]
    return []


def _пустое(последовательность: Any) -> bool:
    """Может ли последовательность совпасть с пустой строкой."""
    for op, av in последовательность:
        if op in _НУЛЕВЫЕ:
            continue
        if op in _ПОВТОРЫ:
            if av[0] == 0 or _пустое(av[2]):
                continue
            return False
        if op == _sre.SUBPATTERN:
            if _пустое(av[-1]):
                continue
            return False
        if op == _sre.BRANCH:
            if any(_пустое(в) for в in av[1]):
                continue
            return False
        if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            continue                # ссылка может оказаться пустой
        return False
    return True


def _края(последовательность: Any, *, с_конца: bool) -> frozenset[str]:
    """Знаки, которыми может начаться (или кончиться) совпадение."""
    итог: set[str] = set()
    узлы = list(последовательность)
    for op, av in (reversed(узлы) if с_конца else узлы):
        if op in _НУЛЕВЫЕ:
            continue
        набор = _знаки(op, av)
        if набор is not None:
            итог |= набор
            return frozenset(итог)
        if op in (_sre.GROUPREF, _sre.GROUPREF_EXISTS):
            return _ВЫБОРКА
        вложенные = _ветви(op, av)
        if not вложенные:
            return _ВЫБОРКА
        for в in вложенные:
            итог |= _края(в, с_конца=с_конца)
        if op in _ПОВТОРЫ:
            if av[0] > 0 and not _пустое(av[2]):
                return frozenset(итог)
            continue
        if op == _sre.BRANCH:
            if not any(_пустое(в) for в in вложенные):
                return frozenset(итог)
            continue
        if not all(_пустое(в) for в in вложенные):
            return frozenset(итог)
    return frozenset(итог)


def _со_сложностью(последовательность: Any) -> bool:
    """Есть ли внутри свой повтор или выбор — то, что делает границу неоднозначной."""
    for op, av in последовательность:
        if (op in _ПОВТОРЫ and av[1] > 1) or op == _sre.BRANCH:
            return True
        if any(_со_сложностью(в) for в in _ветви(op, av)):
            return True
    return False


def _опасно(последовательность: Any) -> str:
    for op, av in последовательность:
        if op in _ПОВТОРЫ and av[1] > 1:
            тело = av[2]
            if _со_сложностью(тело):
                if _пустое(тело):
                    return "повторяется кусок, который может быть пустым"
                if _края(тело, с_конца=False) & _края(тело, с_конца=True):
                    return ("внутри повтора свой повтор или выбор, и граница между "
                            "итерациями неоднозначна")
        for в in _ветви(op, av):
            беда = _опасно(в)
            if беда:
                return беда
    return ""


def проблема(образец: str) -> str:
    """Пустая строка — выражение годится, иначе объяснение по-русски."""
    if not isinstance(образец, str):
        return "выражение должно быть строкой"
    if len(образец) > ПРЕДЕЛ_ДЛИНЫ:
        return f"выражение длиннее {ПРЕДЕЛ_ДЛИНЫ} знаков"
    try:
        re.compile(образец, re.IGNORECASE | re.UNICODE)
        дерево = _разбор.parse(образец, re.IGNORECASE | re.UNICODE)
    except (re.error, OverflowError, RecursionError, ValueError) as exc:
        return f"выражение «{образец}» не разбирается: {exc}"
    беда = _опасно(list(дерево))
    if беда:
        return (f"«{образец}»: {беда} — на длинной реплике такое выражение "
                "считается часами и останавливает весь сервер. Сделайте границу "
                "обязательной, например «\\w+(?:\\s\\w+)*» вместо «(\\w+\\s?)+»")
    return ""
