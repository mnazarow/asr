"""Правила над словами расшифровки: И / ИЛИ / НЕ / РЯДОМ.

Один движок на три задачи раздела: категории обращений («оплата ИЛИ
платёж»), нарушения оператора («не знаю» ИЛИ «вы должны» — только у
оператора) и пункты скрипта разговора, где список примет — частный случай
правила из одних ИЛИ. Так это устроено у Genesys и NICE: правило пишется
строкой, операторы заглавными, фраза в кавычках ищется точно, без кавычек
— по основам слов.

Синтаксис:

    оплата ИЛИ платёж ИЛИ "не прошла оплата"
    возврат И НЕ (брак ИЛИ повреждение)
    дорого РЯДОМ(5) конкурент
    жалоба НЕ "жалоб нет"

* `И`, `ИЛИ`, `НЕ`, `РЯДОМ` — только заглавными: строчное «и» — обычное
  слово внутри фразы («хлеб и соль»). Английские `AND`, `OR`, `NOT`, `NEAR`
  тоже понимаются — их пишут те, кто переносит правила из Genesys.
* `НЕ` перед операндом — отрицание; между двумя операндами («жалоба НЕ
  суд») — то же, что «жалоба И НЕ суд».
* `РЯДОМ(N)` — оба операнда встречаются не дальше N слов друг от друга;
  без числа — в пределах восьми слов, как у NICE.
* Старшинство от сильного к слабому: НЕ, РЯДОМ, И, ИЛИ; скобки — до трёх
  уровней, операндов — до двадцати. Пределы те же, что у Genesys, и не
  из скромности: правило длиннее не прочитать, а значит, и не проверить.
* Фраза без кавычек сравнивается по основам: «уточнить» найдёт «уточню»
  и «уточнили». В кавычках — по точным формам: «"нет"» не найдёт «нету».

Разбор правила — отдельно от его применения. Правило разбирается один раз
при загрузке набора, применяется к каждой записи; ошибка разбора — это
ошибка настройки, и её надо показать человеку при сохранении, а не
ронять разбор архива через сутки.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .stemmer import stem, words

#: Пределы правила — как у Genesys: до двадцати операндов, скобки до трёх
#: уровней. Правило длиннее не прочитать, а значит, и не проверить.
МАКС_ОПЕРАНДОВ = 20
МАКС_ВЛОЖЕННОСТЬ = 3

#: Окно РЯДОМ по умолчанию — восемь слов, как в NICE.
РЯДОМ_ПО_УМОЛЧАНИЮ = 8
МАКС_РЯДОМ = 100

_ОПЕРАТОРЫ = {
    "И": "and", "AND": "and",
    "ИЛИ": "or", "OR": "or",
    "НЕ": "not", "NOT": "not",
    "РЯДОМ": "near", "NEAR": "near",
}

_ЛЕКСЕМА = re.compile(
    r"""\s*(?:
        (?P<open>\()|(?P<close>\))|
        (?P<quoted>"[^"]*"|«[^»]*»|“[^”]*”)|
        (?P<near>(?:РЯДОМ|NEAR)\s*\(\s*(?P<n>\d+)\s*\))|
        (?P<word>[^\s()"«»“”]+)
    )""", re.VERBOSE)


class RuleError(ValueError):
    """Ошибка в тексте правила — с позицией, чтобы редактор её подсветил."""

    def __init__(self, message: str, position: int = 0):
        super().__init__(message)
        self.message = message
        self.position = position


@dataclass(slots=True)
class Hit:
    """Одно совпадение: отрезок слов и что именно совпало."""
    start: int
    end: int
    text: str


@dataclass(slots=True)
class Result:
    matched: bool
    hits: list[Hit] = field(default_factory=list)


@dataclass(slots=True)
class Text:
    """Слова области поиска: формы, основы и номер реплики каждого слова."""
    forms: list[str]
    stems: list[str]
    segment: list[int]

    @classmethod
    def of(cls, сегменты: list[dict[str, Any]]) -> Text:
        формы: list[str] = []
        основы: list[str] = []
        номера: list[int] = []
        for i, с in enumerate(сегменты):
            for w in words(str(с.get("text") or "")):
                формы.append(w)
                основы.append(stem(w))
                номера.append(i)
        return cls(формы, основы, номера)


def _слить(*группы: list[Hit]) -> list[Hit]:
    """Совпадения нескольких операндов без повторов по отрезку слов.

    «оплата ИЛИ оплатить» сводится к одной основе «оплат», и без этого одно
    слово в записи считалось бы дважды — по разу на каждую примету.
    """
    out: list[Hit] = []
    видели: set[tuple[int, int]] = set()
    for группа in группы:
        for h in группа:
            if (h.start, h.end) not in видели:
                видели.add((h.start, h.end))
                out.append(h)
    return out


# --- дерево правила ---------------------------------------------------------

@dataclass(slots=True)
class Phrase:
    words: tuple[str, ...]
    exact: bool
    text: str

    def evaluate(self, текст: Text) -> Result:
        ряд = текст.forms if self.exact else текст.stems
        n = len(self.words)
        совпадения = [Hit(i, i + n, self.text)
                      for i in range(len(ряд) - n + 1)
                      if tuple(ряд[i:i + n]) == self.words]
        return Result(bool(совпадения), совпадения)


@dataclass(slots=True)
class Not:
    child: Any

    def evaluate(self, текст: Text) -> Result:
        return Result(not self.child.evaluate(текст).matched, [])


@dataclass(slots=True)
class And:
    children: list[Any]

    def evaluate(self, текст: Text) -> Result:
        итоги = [р.evaluate(текст) for р in self.children]
        if not all(и.matched for и in итоги):
            return Result(False, [])
        return Result(True, _слить(*(и.hits for и in итоги)))


@dataclass(slots=True)
class Or:
    children: list[Any]

    def evaluate(self, текст: Text) -> Result:
        итоги = [р.evaluate(текст) for р in self.children]
        совпавшие = [и for и in итоги if и.matched]
        return Result(bool(совпавшие), _слить(*(и.hits for и in совпавшие)))


@dataclass(slots=True)
class Near:
    left: Any
    right: Any
    window: int

    def evaluate(self, текст: Text) -> Result:
        л, п = self.left.evaluate(текст), self.right.evaluate(текст)
        if not (л.matched and п.matched):
            return Result(False, [])
        # Расстояние — между ближними краями отрезков: «дорого» и
        # «конкурент» через пять слов — это пять, а не пять плюс длина фраз.
        пары = [(a, b) for a in л.hits for b in п.hits
                if max(a.start, b.start) - min(a.end, b.end) <= self.window]
        if not пары:
            return Result(False, [])
        return Result(True, _слить([a for a, _ in пары], [b for _, b in пары]))


Node = Phrase | Not | And | Or | Near


# --- разбор -----------------------------------------------------------------

def _лексемы(правило: str) -> list[tuple[str, Any, int]]:
    """Список (вид, значение, позиция). Фразы без кавычек склеиваются из
    подряд идущих слов позже, в разборе: здесь каждое слово — своя лексема."""
    out: list[tuple[str, Any, int]] = []
    позиция = 0
    текст = правило or ""
    while позиция < len(текст):
        м = _ЛЕКСЕМА.match(текст, позиция)
        if not м or м.end() == позиция:
            if текст[позиция:].strip() == "":
                break
            raise RuleError(f"непонятный знак «{текст[позиция]}»", позиция)
        начало = м.start() + (len(м.group(0)) - len(м.group(0).lstrip()))
        if м.group("open"):
            out.append(("open", "(", начало))
        elif м.group("close"):
            out.append(("close", ")", начало))
        elif м.group("quoted"):
            out.append(("quoted", м.group("quoted")[1:-1], начало))
        elif м.group("near"):
            n = int(м.group("n"))
            if not 1 <= n <= МАКС_РЯДОМ:
                raise RuleError(f"окно РЯДОМ должно быть от 1 до {МАКС_РЯДОМ} слов",
                                начало)
            out.append(("op", ("near", n, м.group("near")), начало))
        else:
            слово = м.group("word")
            оператор = _ОПЕРАТОРЫ.get(слово)
            if оператор:
                out.append(("op", (оператор, None, слово), начало))
            else:
                out.append(("word", слово, начало))
        позиция = м.end()
    return out


class _Разбор:
    def __init__(self, правило: str):
        self.лексемы = _лексемы(правило)
        self.i = 0
        self.операндов = 0
        self.глубина = 0

    def _текущая(self) -> tuple[str, Any, int] | None:
        return self.лексемы[self.i] if self.i < len(self.лексемы) else None

    def _оператор(self) -> str | None:
        л = self._текущая()
        return л[1][0] if л and л[0] == "op" else None

    def разобрать(self) -> Node:
        if not self.лексемы:
            raise RuleError("правило пустое", 0)
        дерево = self._или()
        л = self._текущая()
        if л is not None:
            if л[0] == "close":
                raise RuleError("лишняя закрывающая скобка", л[2])
            raise RuleError("не удалось разобрать правило до конца", л[2])
        return дерево

    def _или(self) -> Node:
        части = [self._и()]
        while self._оператор() == "or":
            self.i += 1
            части.append(self._и())
        return части[0] if len(части) == 1 else Or(части)

    def _и(self) -> Node:
        части = [self._рядом()]
        while True:
            оп = self._оператор()
            if оп == "and":
                self.i += 1
                части.append(self._рядом())
            elif оп == "not":
                # «жалоба НЕ суд» — то же, что «жалоба И НЕ суд»; сам
                # унарный НЕ разберётся ниже.
                части.append(self._рядом())
            else:
                break
        return части[0] if len(части) == 1 else And(части)

    def _рядом(self) -> Node:
        левый = self._не()
        while self._оператор() == "near":
            окно = self._текущая()[1][1] or РЯДОМ_ПО_УМОЛЧАНИЮ
            self.i += 1
            левый = Near(левый, self._не(), int(окно))
        return левый

    def _не(self) -> Node:
        л = self._текущая()
        if л and л[0] == "op" and л[1][0] == "not":
            self.i += 1
            if self._текущая() is None:
                raise RuleError("после НЕ ничего нет", л[2])
            return Not(self._не())
        return self._операнд()

    def _операнд(self) -> Node:
        л = self._текущая()
        if л is None:
            прошлая = self.лексемы[-1]
            хвост = прошлая[1][2] if прошлая[0] == "op" else str(прошлая[1])
            raise RuleError("правило оборвано: после оператора нужен операнд",
                            прошлая[2] + len(хвост))
        вид, значение, позиция = л
        if вид == "open":
            self.глубина += 1
            if self.глубина > МАКС_ВЛОЖЕННОСТЬ:
                raise RuleError(f"скобки вложены глубже {МАКС_ВЛОЖЕННОСТЬ} уровней",
                                позиция)
            self.i += 1
            внутри = self._или()
            закрывающая = self._текущая()
            if not закрывающая or закрывающая[0] != "close":
                raise RuleError("не закрыта скобка", позиция)
            self.i += 1
            self.глубина -= 1
            return внутри
        if вид == "close":
            raise RuleError("закрывающая скобка без открывающей", позиция)
        if вид == "op":
            raise RuleError(f"оператор без операнда: «{значение[2]}»", позиция)
        if вид == "quoted":
            self.i += 1
            формы = words(значение)
            if not формы:
                raise RuleError("пустые кавычки", позиция)
            return self._фраза(tuple(формы), True, значение.strip(), позиция)
        # Слова без кавычек подряд — одна фраза, до оператора или скобки.
        слова: list[str] = []
        начало = позиция
        while (л := self._текущая()) and л[0] == "word":
            слова.append(л[1])
            self.i += 1
        основы = tuple(stem(w) for w in words(" ".join(слова)))
        if not основы:
            raise RuleError("во фразе нет ни одного слова", начало)
        return self._фраза(основы, False, " ".join(слова), начало)

    def _фраза(self, слова: tuple[str, ...], точно: bool, текст: str,
               позиция: int) -> Phrase:
        self.операндов += 1
        if self.операндов > МАКС_ОПЕРАНДОВ:
            raise RuleError(f"в правиле больше {МАКС_ОПЕРАНДОВ} операндов", позиция)
        return Phrase(слова, точно, текст)


def parse(правило: str) -> Node:
    """Дерево правила из строки. Бросает RuleError с позицией ошибки."""
    return _Разбор(str(правило or "")).разобрать()


def parse_any(варианты: list[str]) -> Node:
    """Правило из списка примет скрипта: любая из них, по основам.

    Пункт скрипта «Поздоровался: здравствуйте, добрый день» — это и есть
    «здравствуйте ИЛИ добрый день»; так список примет становится частным
    случаем правила, и проверяет его тот же движок.
    """
    фразы = []
    for вариант in варианты or []:
        основы = tuple(stem(w) for w in words(str(вариант)))
        if основы:
            фразы.append(Phrase(основы, False, str(вариант).strip()))
    if not фразы:
        raise RuleError("нет ни одной приметы", 0)
    return фразы[0] if len(фразы) == 1 else Or(фразы)


def operands(дерево: Node) -> list[Phrase]:
    """Все фразы правила — для проверки примет на частые слова."""
    if isinstance(дерево, Phrase):
        return [дерево]
    if isinstance(дерево, Not):
        return operands(дерево.child)
    if isinstance(дерево, Near):
        return operands(дерево.left) + operands(дерево.right)
    return [ф for р in дерево.children for ф in operands(р)]


def describe(дерево: Node) -> str:
    """Правило обратно в строку — в каноническом виде, для показа."""
    if isinstance(дерево, Phrase):
        return f'"{дерево.text}"' if дерево.exact else дерево.text
    if isinstance(дерево, Not):
        return f"НЕ {_в_скобках(дерево.child)}"
    if isinstance(дерево, Near):
        return (f"{_в_скобках(дерево.left)} РЯДОМ({дерево.window}) "
                f"{_в_скобках(дерево.right)}")
    союз = " И " if isinstance(дерево, And) else " ИЛИ "
    return союз.join(_в_скобках(р) if isinstance(р, (And, Or)) else describe(р)
                     for р in дерево.children)


def _в_скобках(узел: Node) -> str:
    return describe(узел) if isinstance(узел, (Phrase, Not)) else f"({describe(узел)})"


def evaluate(дерево: Node, текст: Text) -> Result:
    """Применить правило к области поиска."""
    return дерево.evaluate(текст)


def check(правило: str) -> str:
    """Ошибка разбора одной строкой или пустая строка, если правило годное."""
    try:
        parse(правило)
    except RuleError as exc:
        return f"{exc.message} (позиция {exc.position + 1})"
    return ""
