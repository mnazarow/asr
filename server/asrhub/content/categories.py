"""Категории обращений: правило, кто сказал, где в разговоре — и что нашлось.

Категория — это именованное правило (см. `rules`) с двумя уточнениями:
чьи реплики смотреть (оператора, клиента, любые) и в какой части записи
(в начале, в конце, где угодно — с окном в секундах, как у пунктов
скрипта). У Amazon Contact Lens и Genesys категория устроена ровно так,
и здесь она делает ту же работу: отвечает, о чём был разговор, — «оплата»,
«доставка», «возврат» — без модели и без разметки, по словам.

Один движок закрывает три разных вопроса раздела, и различает их поле
`kind`: категория обращения («о чём»), нарушение оператора (стоп-слова —
«не знаю», «вы должны»), возражение клиента и его отработка. Считаются они
одинаково, а показываются и штрафуются по-разному.

Результат по записи — список сработавших категорий со счётом, временем
первого совпадения и примерами реплик. Свод по архиву считает база по
таблице совпадений; здесь — только одна запись.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import rules
from .compliance import ДОЛЯ_КРАЯ, frequent_marker
from .stemmer import stem

#: Виды категорий — что означает срабатывание.
ВИДЫ: dict[str, str] = {
    "topic": "категория обращения",
    "violation": "нарушение оператора",
    "objection": "возражение клиента",
    "handling": "отработка возражения",
}

#: Чьи реплики смотреть.
КТО: dict[str, str] = {
    "any": "любой говорящий",
    "agent": "только оператор",
    "customer": "только клиент",
}

ГДЕ: dict[str, str] = {
    "any": "в любом месте",
    "start": "в начале разговора",
    "end": "в конце разговора",
}

#: Сколько категорий можно завести. Не техническое ограничение, а
#: здравый смысл: набор из трёхсот категорий никто не прочитает, а каждая
#: применяется к каждой записи.
МАКС_КАТЕГОРИЙ = 200

#: Сколько примеров реплик хранить по сработавшей категории в записи.
ПРИМЕРОВ = 5

#: Готовые категории для старта — то, с чем сталкивается почти любая
#: служба поддержки или продаж. Правила намеренно простые: у каждого
#: своё дело и свои слова, и набор здесь — заготовка, которую правят, а
#: не истина. Действует, пока свой набор не сохранён, — как скрипт
#: разговора по умолчанию.
ГОТОВЫЕ: list[dict[str, Any]] = [
    {"id": "payment", "label": "Оплата", "kind": "topic", "who": "any",
     "rule": "оплата ИЛИ оплатить ИЛИ платёж ИЛИ списание ИЛИ списали ИЛИ "
             "квитанция ИЛИ чек ИЛИ перевод ИЛИ рассрочка"},
    {"id": "delivery", "label": "Доставка", "kind": "topic", "who": "any",
     "rule": "доставка ИЛИ доставить ИЛИ курьер ИЛИ посылка ИЛИ отправление ИЛИ "
             "пункт выдачи ИЛИ самовывоз ИЛИ трек"},
    {"id": "refund", "label": "Возврат", "kind": "topic", "who": "any",
     "rule": "возврат ИЛИ вернуть деньги ИЛИ верните ИЛИ возместить ИЛИ "
             "компенсация ИЛИ обменять ИЛИ обмен"},
    {"id": "quality", "label": "Качество", "kind": "topic", "who": "any",
     "rule": "брак ИЛИ бракованный ИЛИ сломался ИЛИ не работает ИЛИ повреждён ИЛИ "
             "дефект ИЛИ некачественный ИЛИ царапина ИЛИ помятый"},
    {"id": "deadline", "label": "Сроки", "kind": "topic", "who": "any",
     "rule": "срок ИЛИ задержка ИЛИ опоздание ИЛИ когда будет ИЛИ до сих пор нет ИЛИ "
             "просрочен ИЛИ сорван ИЛИ перенесли"},
    {"id": "support", "label": "Техподдержка", "kind": "topic", "who": "any",
     "rule": "не открывается ИЛИ не загружается ИЛИ ошибка ИЛИ зависает ИЛИ пароль ИЛИ "
             "не могу войти ИЛИ приложение ИЛИ личный кабинет ИЛИ обновление"},
    {"id": "repeat", "label": "Повторное обращение", "kind": "topic", "who": "customer",
     "rule": "уже звонил ИЛИ уже обращался ИЛИ уже писал ИЛИ второй раз ИЛИ третий раз ИЛИ "
             "в который раз ИЛИ опять ИЛИ снова ИЛИ до сих пор не"},
    {"id": "escalation", "label": "Эскалация", "kind": "topic", "who": "customer",
     "rule": "руководитель ИЛИ старший ИЛИ начальник ИЛИ жалоба ИЛИ претензия ИЛИ "
             "роспотребнадзор ИЛИ суд ИЛИ прокуратура ИЛИ отзыв оставлю"},
    {"id": "competitors", "label": "Конкуренты", "kind": "topic", "who": "any",
     "rule": "конкурент ИЛИ у других ИЛИ в другом месте ИЛИ другая компания ИЛИ "
             "дешевле у ИЛИ перейду к ИЛИ уйду к"},
    {"id": "price", "label": "Цена и скидка", "kind": "topic", "who": "any",
     "rule": "дорого ИЛИ скидка ИЛИ дешевле ИЛИ цена ИЛИ стоимость ИЛИ подорожал ИЛИ "
             "акция ИЛИ промокод ИЛИ бесплатно"},
]

_НЕ_ИМЯ = re.compile(r"[^a-zа-я0-9_]+")


@dataclass(slots=True)
class Compiled:
    """Категория, готовая к применению: разобранное правило или ошибка."""
    id: str
    label: str
    kind: str
    who: str
    where: str
    within_s: float
    rule: str
    tree: Any
    error: str
    notify: bool = False
    penalty: float = 0.0
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "kind": self.kind,
                "who": self.who, "where": self.where,
                "within_s": self.within_s or None, "rule": self.rule,
                "error": self.error or None, "notify": self.notify,
                "penalty": self.penalty, "weight": self.weight}


def _имя(подпись: str, занятые: set[str], n: int) -> str:
    основа = _НЕ_ИМЯ.sub("_", str(подпись or "").lower().replace("ё", "е")).strip("_")
    имя = основа or f"category{n}"
    k = 2
    while имя in занятые:
        имя = f"{основа or 'category'}{k}"
        k += 1
    return имя


def normalize(категории: Any) -> list[dict[str, Any]]:
    """Список категорий с заполненными полями и уникальными именами.

    Терпимо к тому, что приходит из настроек: строки вместо чисел, пустые
    поля, отсутствующие имена. Непонятное не выбрасывает — заполняет
    значением по умолчанию: молча потерять категорию хуже, чем применить
    её без окна.
    """
    if not isinstance(категории, list):
        return []
    out: list[dict[str, Any]] = []
    занятые: set[str] = set()
    for n, сырое in enumerate(категории):
        if not isinstance(сырое, dict):
            continue
        имя = str(сырое.get("id") or "").strip()
        if not имя or имя in занятые:
            имя = _имя(сырое.get("label") or имя, занятые, n + 1)
        занятые.add(имя)
        try:
            окно = max(0.0, float(сырое.get("within_s") or 0))
        except (TypeError, ValueError):
            окно = 0.0
        try:
            штраф = max(0.0, float(сырое.get("penalty") or 0))
        except (TypeError, ValueError):
            штраф = 0.0
        try:
            вес = float(сырое.get("weight") if сырое.get("weight") is not None else 1)
        except (TypeError, ValueError):
            вес = 1.0
        out.append({
            "id": имя,
            "label": str(сырое.get("label") or имя).strip(),
            "kind": сырое.get("kind") if сырое.get("kind") in ВИДЫ else "topic",
            "who": сырое.get("who") if сырое.get("who") in КТО else "any",
            "where": сырое.get("where") if сырое.get("where") in ГДЕ else "any",
            "within_s": окно or None,
            "rule": str(сырое.get("rule") or "").strip(),
            "notify": bool(сырое.get("notify")),
            "penalty": штраф,
            "weight": вес,
        })
    return out


def validate(категории: Any) -> list[str]:
    """Ошибки набора — для проверки настройки при сохранении.

    Пустой список — набор годный. Ошибка правила называет категорию и
    позицию: редактор подсвечивает её, а настройка с такой ошибкой в базу
    не попадает — иначе она всплыла бы через сутки в фоновом разборе.
    """
    if not isinstance(категории, list):
        return ["ожидается список категорий"]
    if len(категории) > МАКС_КАТЕГОРИЙ:
        return [f"категорий больше {МАКС_КАТЕГОРИЙ}"]
    ошибки: list[str] = []
    имена: set[str] = set()
    for n, сырое in enumerate(категории, start=1):
        if not isinstance(сырое, dict):
            ошибки.append(f"категория {n}: ожидается объект")
            continue
        подпись = str(сырое.get("label") or "").strip()
        кто = подпись or f"категория {n}"
        if not подпись:
            ошибки.append(f"{кто}: нет названия")
        имя = str(сырое.get("id") or "").strip()
        if имя:
            if имя in имена:
                ошибки.append(f"{кто}: имя «{имя}» уже занято")
            имена.add(имя)
        for поле, допустимые in (("kind", ВИДЫ), ("who", КТО), ("where", ГДЕ)):
            значение = сырое.get(поле)
            if значение is not None and значение not in допустимые:
                ошибки.append(f"{кто}: поле {поле} — одно из "
                              f"{', '.join(допустимые)}")
        try:
            if float(сырое.get("within_s") or 0) < 0:
                raise ValueError
        except (TypeError, ValueError):
            ошибки.append(f"{кто}: окно within_s должно быть числом секунд")
        ошибка = rules.check(str(сырое.get("rule") or ""))
        if ошибка:
            ошибки.append(f"{кто}: {ошибка}")
    return ошибки


def compile(категории: Any) -> list[Compiled]:  # noqa: A001
    """Разобрать набор один раз — применять к каждой записи.

    Ошибка в правиле одной категории не останавливает остальные: она
    остаётся в `error`, попадает в ответ проверки и в состояние раздела, а
    запись разбирается по годным.
    """
    if категории and all(isinstance(к, Compiled) for к in категории):
        return list(категории)
    out: list[Compiled] = []
    for к in normalize(категории):
        дерево, ошибка = None, ""
        try:
            дерево = rules.parse(к["rule"])
        except rules.RuleError as exc:
            ошибка = f"{exc.message} (позиция {exc.position + 1})"
        out.append(Compiled(
            id=к["id"], label=к["label"], kind=к["kind"], who=к["who"],
            where=к["where"], within_s=float(к["within_s"] or 0),
            rule=к["rule"], tree=дерево, error=ошибка, notify=к["notify"],
            penalty=к["penalty"], weight=к["weight"]))
    return out


def _время(с: dict[str, Any], край: str) -> float:
    for ключ in (край, f"{край}_s"):
        if с.get(ключ) is not None:
            try:
                return float(с[ключ])
            except (TypeError, ValueError):
                continue
    return 0.0


def _область(сегменты: list[dict[str, Any]], категория: Compiled,
             стороны: dict[str, str | None]) -> tuple[list[dict[str, Any]], bool]:
    """Реплики, в которых ищется категория, и известна ли сторона.

    Когда сторона не определена (говорящий один или не размечен), ищем по
    всем репликам — лучше найти без разделения, чем не искать вовсе; но
    отмечаем это: «нарушение оператора», найденное в реплике неизвестно
    кого, — не обвинение, а повод послушать.
    """
    кто = стороны.get(категория.who) if категория.who != "any" else None
    сторона_известна = категория.who == "any" or кто is not None
    свои = [с for с in сегменты if кто is None or str(с.get("speaker") or "") == кто]
    if категория.where == "any" or not свои:
        return свои, сторона_известна
    if категория.within_s:
        первая = min((_время(с, "start") for с in сегменты), default=0.0)
        последняя = max((_время(с, "end") for с in сегменты), default=0.0)
        if категория.where == "start":
            куски = [с for с in свои if _время(с, "start") - первая <= категория.within_s]
        else:
            куски = [с for с in свои if последняя - _время(с, "end") <= категория.within_s]
        return куски, сторона_известна
    край = max(1, int(len(свои) * ДОЛЯ_КРАЯ))
    return (свои[:край] if категория.where == "start" else свои[-край:]), сторона_известна


def apply(segments: list[dict[str, Any]], categories: list[Compiled] | list[dict[str, Any]],
          *, agent: str | None = None, customer: str | None = None,
          everything: bool = False) -> dict[str, Any]:
    """Применить набор к одной записи.

    Возвращает сработавшие категории со счётом совпадений, временем первого
    и примерами реплик; с `everything=True` — все категории, включая те,
    что не сработали и не разобрались: так проверяет редактор.
    """
    набор = compile(categories)
    сегменты = list(segments or [])
    стороны = {"agent": agent, "customer": customer}
    items: list[dict[str, Any]] = []
    ошибок = 0
    for категория in набор:
        if категория.error:
            ошибок += 1
            if everything:
                items.append({**категория.to_dict(), "count": 0, "first_s": None,
                              "hits": [], "sides": True})
            continue
        куски, известна = _область(сегменты, категория, стороны)
        текст = rules.Text.of(куски)
        итог = rules.evaluate(категория.tree, текст)
        if not итог.matched and not everything:
            continue
        примеры = []
        первое = None
        for совпадение in sorted(итог.hits, key=lambda h: h.start):
            номер = текст.segment[совпадение.start] if текст.segment else 0
            реплика = куски[номер] if номер < len(куски) else {}
            когда = _время(реплика, "start")
            первое = когда if первое is None else min(первое, когда)
            if len(примеры) < ПРИМЕРОВ:
                примеры.append({
                    "start_s": round(когда, 2),
                    "speaker": реплика.get("speaker"),
                    "matched": совпадение.text,
                    "text": str(реплика.get("text") or "").strip()[:200],
                })
        items.append({
            "id": категория.id, "label": категория.label, "kind": категория.kind,
            "who": категория.who, "count": len(итог.hits),
            "first_s": round(первое, 2) if первое is not None else None,
            "hits": примеры, "sides": известна,
            **({"error": None, "rule": категория.rule, "where": категория.where,
                "within_s": категория.within_s or None} if everything else {}),
        })
    сработали = [и["id"] for и in items if и.get("count")]
    return {"checked": len(набор), "errors": ошибок, "matched": сработали,
            "items": items}


def for_db(результат: dict[str, Any]) -> list[dict[str, Any]]:
    """Строки таблицы совпадений из результата по записи."""
    return [{"category": и["id"], "kind": и.get("kind") or "topic",
             "count": int(и.get("count") or 0), "first_s": и.get("first_s")}
            for и in результат.get("items") or [] if и.get("count")]


def suspicious(категории: Any, document_frequency: dict[str, int] | None = None,
               corpus_size: int = 0) -> list[dict[str, str]]:
    """Операнды, совпадающие со слишком частыми словами, — как у скрипта.

    Правило «оплата ИЛИ это» сработает в каждой записи из-за «это», и
    категория «Оплата» покроет весь архив. Кавычки не спасают: точная
    форма частого слова так же часта.
    """
    найдено: list[dict[str, str]] = []
    for категория in compile(категории):
        if категория.error:
            continue
        for операнд in rules.operands(категория.tree):
            if len(операнд.words) != 1:
                continue
            основа = stem(операнд.words[0]) if операнд.exact else операнд.words[0]
            if frequent_marker(основа, document_frequency, corpus_size):
                найдено.append({"word": операнд.text, "label": категория.label})
    return найдено
