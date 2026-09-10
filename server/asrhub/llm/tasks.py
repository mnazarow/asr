"""Подсказки языковой модели и разбор её ответов.

Три вызова на запись самое большее:

1. **Основной** — резюме, причина обращения и исход из закрытых списков,
   действия к исполнению. Одним вызовом: модель читает разговор один раз,
   а три вопроса к нему дешевле трёх чтений.
2. **Умные трекеры** — по каждому заданному трекеру: сработал ли по смыслу
   и цитата. Только если трекеры заданы.
3. **Скоркарта** — по каждому вопросу: да, нет или неприменимо и цитата.
   Только если заданы вопросы или есть пункты скрипта.

Длинная запись сначала сжимается по кускам (по вызову на кусок), и разбор
идёт по пересказам — об этом в ответе стоит `chunks`.

Модель просят отвечать только JSON и проверяют ответ: причина и исход
обязаны быть из списка (иначе — «другое»/«неясно»), цитаты — строки,
действия — список объектов. Что не разобралось — не выдумывается, а
отмечается предупреждением.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

from .client import LLMClient, LLMError

#: Версия подсказок. Меняется, когда меняется смысл вопросов к модели:
#: старые ответы помечаются устаревшими и пересчитываются при разборе архива.
VERSION = 1

#: Метка вида задачи в начале системной подсказки — по ней заглушка
#: понимает, что от неё ждут; настоящей модели она не мешает.
МЕТКА = "### задача: {kind}\n"

_СИСТЕМА = (
    "Ты помощник аналитика контакт-центра. Тебе дают расшифровку телефонного "
    "разговора между сотрудником и клиентом. Отвечай только по расшифровке, "
    "ничего не выдумывай: если чего-то в разговоре нет, так и пиши. "
    "Отвечай на русском языке, строго одним объектом JSON без пояснений."
)

_ОСНОВНОЙ = """Расшифровка разговора:
\"\"\"
{transcript}
\"\"\"
{lists}
Верни JSON с полями:
{fields}
"""

_ПЕРЕСКАЗ = """Это часть {n} из {total} расшифровки длинного разговора:
\"\"\"
{chunk}
\"\"\"
Перескажи её в трёх-пяти предложениях: кто что сказал, о чём договорились. Верни JSON: {{"summary": "пересказ"}}
"""

_ТРЕКЕРЫ = """Расшифровка разговора:
\"\"\"
{transcript}
\"\"\"
Проверь по смыслу, а не по словам, сработал ли каждый из трекеров:
{trackers}
Верни JSON: {{"trackers": [{{"id": "…", "fired": true или false, "quote": "цитата из расшифровки, по которой сработало, или пустая строка"}}]}}
"""

_СКОРКАРТА = """Расшифровка разговора:
\"\"\"
{transcript}
\"\"\"
Ответь на вопросы о поведении сотрудника в этом разговоре. Ответ — «да», «нет» или «н/п» (неприменимо: ситуации в разговоре не было).
{questions}
Верни JSON: {{"answers": [{{"id": "…", "answer": "да|нет|н/п", "quote": "цитата или пустая строка"}}]}}
"""


def transcript_of(segments: list[dict[str, Any]], text: str, *,
                  agent_speaker: str = "") -> str:
    """Разговор строками «Кто: реплика». Без разметки говорящих — сплошной текст."""
    строки = []
    for с in segments or []:
        реплика = str(с.get("text") or "").strip()
        if not реплика:
            continue
        кто = str(с.get("speaker") or "").strip()
        if кто and agent_speaker and кто == agent_speaker:
            кто = "Сотрудник"
        строки.append(f"{кто}: {реплика}" if кто else реплика)
    return "\n".join(строки) if строки else str(text or "").strip()


def chunks_of(transcript: str, limit: int) -> list[str]:
    """Режет по строкам-репликам на куски не длиннее предела."""
    limit = max(500, int(limit))
    if len(transcript) <= limit:
        return [transcript]
    куски: list[str] = []
    текущий: list[str] = []
    длина = 0
    for строка in transcript.split("\n"):
        if длина + len(строка) + 1 > limit and текущий:
            куски.append("\n".join(текущий))
            текущий, длина = [], 0
        # Реплика длиннее предела режется по словам.
        while len(строка) > limit:
            куски.append(строка[:limit])
            строка = строка[limit:]
        текущий.append(строка)
        длина += len(строка) + 1
    if текущий:
        куски.append("\n".join(текущий))
    return куски


def parse_json(text: str) -> dict[str, Any]:
    """Объект JSON из ответа модели — даже если она обернула его в текст."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text)
    try:
        данные = json.loads(text)
    except (TypeError, ValueError):
        начало, конец = text.find("{"), text.rfind("}")
        if начало < 0 or конец <= начало:
            raise LLMError("Модель ответила не JSON.") from None
        try:
            данные = json.loads(text[начало:конец + 1])
        except (TypeError, ValueError) as exc:
            raise LLMError("Модель ответила повреждённым JSON.") from exc
    if not isinstance(данные, dict):
        raise LLMError("Модель ответила не объектом JSON.")
    return данные


def _из_списка(значение: Any, варианты: list[str],
               запасные: tuple[str, ...]) -> tuple[str | None, bool]:
    """Ответ модели — строго из списка.

    Возвращает пункт списка и признак «модель попала в список». Не попала
    — берётся запасной пункт («другое», «неясно»), но признак остаётся
    ложным: выдуманный моделью ответ должен быть виден, а не растворяться
    в общей доле «неясно».

    Неточное совпадение разбирается по смыслу, а не по порядку списка. Из
    пунктов, целиком попавших в ответ, берётся самый длинный: при списке
    «оплата; оплата картой» ответ «оплата картой в приложении» — это
    второй пункт, а не первый, который просто стоит раньше. Наоборот —
    ответ короче пункта — берётся самый узкий из содержащих его, чтобы
    короткое «оплата» не уезжало в «оплата картой при доставке».
    """
    if not варианты:
        return None, False
    текст = str(значение or "").strip().lower()
    пункты = [(в, str(в).strip().lower()) for в in варианты if str(в).strip()]
    for в, нижний in пункты:
        if текст == нижний:
            return str(в), True
    if текст:
        внутри = [(в, нижний) for в, нижний in пункты if нижний in текст]
        if внутри:
            return str(max(внутри, key=lambda пара: len(пара[1]))[0]), True
        содержат = [(в, нижний) for в, нижний in пункты if текст in нижний]
        if содержат:
            return str(min(содержат, key=lambda пара: len(пара[1]))[0]), True
    for в in варианты:
        if any(з in str(в).lower() for з in запасные):
            return str(в), False
    return None, False


def _строка(значение: Any, предел: int = 600) -> str:
    return str(значение or "").strip()[:предел]


def analyze(client: LLMClient, *, text: str, segments: list[dict[str, Any]],
            settings: Any, agent_speaker: str = "",
            script: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Полный смысловой разбор одной записи по включённым задачам."""
    задачи = set(settings.get("llm_tasks") or [])
    предел = int(settings.get("llm_context_chars") or 12000)
    причины = [str(x) for x in (settings.get("llm_reasons") or []) if str(x).strip()]
    исходы = [str(x) for x in (settings.get("llm_outcomes") or []) if str(x).strip()]
    трекеры = [т for т in (settings.get("llm_trackers") or []) if isinstance(т, dict)]
    вопросы = [в for в in (settings.get("llm_scorecard") or []) if isinstance(в, dict)]
    if not вопросы and script:
        вопросы = [{"id": str(п.get("id") or f"item{i}"),
                    "question": f"Сотрудник выполнил пункт скрипта: {п.get('label')}?"}
                   for i, п in enumerate(script) if п.get("label")]

    начало = time.perf_counter()
    итог: dict[str, Any] = {"summary": None, "reason": None, "outcome": None,
                            "resolved": None, "actions": None, "trackers": None,
                            "scorecard": None, "chunks": 1, "calls": 0,
                            "model": client.model, "warnings": []}
    разговор = transcript_of(segments, text, agent_speaker=agent_speaker)
    if not разговор:
        итог["warnings"].append("пустая расшифровка")
        итог["latency_ms"] = 0.0
        return итог

    куски = chunks_of(разговор, предел)
    if len(куски) > 1:
        пересказы = []
        for n, кусок in enumerate(куски, 1):
            ответ = client.chat(МЕТКА.format(kind="chunk") + _СИСТЕМА,
                                _ПЕРЕСКАЗ.format(n=n, total=len(куски), chunk=кусок),
                                kind="chunk", validate=parse_json)
            итог["calls"] += 1
            пересказы.append(f"Часть {n}: {_строка(parse_json(ответ).get('summary'), 2000)}")
        разговор = "\n".join(пересказы)
        итог["chunks"] = len(куски)
        итог["warnings"].append(f"разбор по пересказам {len(куски)} частей")

    основные = задачи & {"summary", "outcome", "actions"}
    if основные:
        поля = []
        списки = []
        if "summary" in задачи:
            поля.append('"summary": "резюме в три-пять предложений: причина обращения, '
                        'что сделано, чем закончилось"')
            поля.append('"resolved": true, если вопрос клиента решён в разговоре, false, '
                        'если не решён, null, если неясно')
        if "outcome" in задачи:
            if причины:
                списки.append("Причины обращения (выбери ровно одну): " + "; ".join(причины))
                поля.append('"reason": "одна причина из списка"')
                поля.append('"reason_quote": "цитата из расшифровки, по которой выбрана причина"')
            if исходы:
                списки.append("Исходы разговора (выбери ровно один): " + "; ".join(исходы))
                поля.append('"outcome": "один исход из списка"')
                поля.append('"outcome_quote": "цитата, по которой выбран исход"')
        if "actions" in задачи:
            поля.append('"actions": список действий к исполнению, о которых договорились: '
                        '[{"what": "что сделать", "who": "сотрудник" или "клиент", '
                        '"when": "срок словами из разговора или null", '
                        '"quote": "цитата"}]; пустой список, если договорённостей нет')
        ответ = client.chat(
            МЕТКА.format(kind="main") + _СИСТЕМА,
            _ОСНОВНОЙ.format(transcript=разговор,
                             lists=("\n".join(списки) + "\n") if списки else "",
                             fields="\n".join(поля)),
            kind="main", validate=parse_json)
        итог["calls"] += 1
        данные = parse_json(ответ)
        if "summary" in задачи:
            итог["summary"] = _строка(данные.get("summary"), 2000) or None
            решено = данные.get("resolved")
            итог["resolved"] = решено if isinstance(решено, bool) else None
        if "outcome" in задачи:
            if причины:
                итог["reason"], попала = _из_списка(данные.get("reason"), причины,
                                                    ("друг", "проч"))
                итог["reason_quote"] = _строка(данные.get("reason_quote"))
                if not попала:
                    итог["warnings"].append("причина вне списка")
            if исходы:
                итог["outcome"], попала = _из_списка(данные.get("outcome"), исходы,
                                                     ("неясн", "друг"))
                итог["outcome_quote"] = _строка(данные.get("outcome_quote"))
                if not попала:
                    итог["warnings"].append("исход вне списка")
        if "actions" in задачи:
            действия = данные.get("actions")
            итог["actions"] = [
                {"what": _строка(д.get("what")), "who": _строка(д.get("who"), 40) or "сотрудник",
                 "when": _строка(д.get("when"), 120) or None, "quote": _строка(д.get("quote"))}
                for д in (действия if isinstance(действия, list) else [])
                if isinstance(д, dict) and _строка(д.get("what"))][:20]

    if "trackers" in задачи and трекеры:
        описания = "\n".join(
            f"- id «{т.get('id')}», «{т.get('label')}»: {т.get('description') or т.get('label')}"
            for т in трекеры if т.get("id"))
        ответ = client.chat(МЕТКА.format(kind="trackers") + _СИСТЕМА,
                            _ТРЕКЕРЫ.format(transcript=разговор, trackers=описания),
                            kind="trackers", validate=parse_json)
        итог["calls"] += 1
        данные = parse_json(ответ)
        ответы = {str(o.get("id")): o for o in (данные.get("trackers") or [])
                  if isinstance(o, dict)}
        итог["trackers"] = [
            {"id": str(т.get("id")), "label": str(т.get("label") or т.get("id")),
             "fired": bool((ответы.get(str(т.get("id"))) or {}).get("fired")),
             "quote": _строка((ответы.get(str(т.get("id"))) or {}).get("quote"))}
            for т in трекеры if т.get("id")]

    if "scorecard" in задачи and вопросы:
        список = "\n".join(f"- id «{в.get('id')}»: {в.get('question')}" for в in вопросы
                           if в.get("id") and в.get("question"))
        ответ = client.chat(МЕТКА.format(kind="scorecard") + _СИСТЕМА,
                            _СКОРКАРТА.format(transcript=разговор, questions=список),
                            kind="scorecard", validate=parse_json)
        итог["calls"] += 1
        данные = parse_json(ответ)
        ответы = {str(o.get("id")): o for o in (данные.get("answers") or [])
                  if isinstance(o, dict)}
        итог["scorecard"] = []
        for в in вопросы:
            if not (в.get("id") and в.get("question")):
                continue
            о = ответы.get(str(в.get("id"))) or {}
            ответ_модели = _строка(о.get("answer"), 16).lower().replace("/", "")
            # «Неприменимо» проверяется ПЕРЕД «нет»: подсказка сама
            # предлагает модели это слово, а начинается оно с «не» — и
            # ответ «ситуации в разговоре не было» шёл в «нет», штрафуя
            # оператора за то, чего он не мог сделать.
            неприменимо = (ответ_модели.startswith(("нп", "н п", "неприменим",
                                                    "не применим"))
                           or "не было" in ответ_модели)
            итог["scorecard"].append({
                "id": str(в.get("id")), "question": str(в.get("question")),
                "answer": ("н/п" if неприменимо else
                           "да" if ответ_модели.startswith("да") else
                           "нет" if ответ_модели.startswith("не") else "н/п"),
                "quote": _строка(о.get("quote"))})

    итог["latency_ms"] = round((time.perf_counter() - начало) * 1000, 1)
    return итог


def summarize_for_digest(результаты: list[dict[str, Any]]) -> dict[str, Any]:
    """Распределения исходов и причин по ответам — для свода и сводки."""
    def доли(ключ: str) -> list[dict[str, Any]]:
        счёт: dict[str, int] = {}
        for р in результаты:
            з = р.get(ключ)
            if з:
                счёт[str(з)] = счёт.get(str(з), 0) + 1
        всего = sum(счёт.values())
        return [{"key": k, "records": v, "share": round(100.0 * v / всего, 1)}
                for k, v in sorted(счёт.items(), key=lambda kv: -kv[1])]
    решено = [р.get("resolved") for р in результаты if р.get("resolved") is not None]
    действий = sum(len(р.get("actions") or []) for р in результаты)
    return {
        "outcomes": доли("outcome"), "reasons": доли("reason"),
        "resolved_share": round(100.0 * sum(1 for x in решено if x) / len(решено), 1)
        if решено else None,
        "actions": действий,
        "records_with_actions": sum(1 for р in результаты if р.get("actions")),
    }
