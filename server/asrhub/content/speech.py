"""Как говорили: темп, паузы, перебивания, монологи, доля речи.

Всё считается по разметке реплик, а не по звуку: у сервера уже есть начало,
конец, говорящий и текст каждой реплики, и этого хватает на большинство
вопросов, которые задают к записи разговора.
"""
from __future__ import annotations

from typing import Any

from .lexicons import ЗВУКИ_ПАУЗЫ, ПАРАЗИТЫ, ФРАЗЫ_ПАРАЗИТЫ
from .stemmer import words

#: Пауза между репликами разных говорящих, ниже которой это уже перебивание,
#: а не ответ. Полсекунды — обычная граница: быстрее человек физически не
#: успевает обдумать сказанное, значит он начал говорить поверх.
ПОРОГ_ПЕРЕБИВАНИЯ = -0.25

#: Пауза внутри разговора, начиная с которой это уже молчание, а не дыхание.
ПОРОГ_ПАУЗЫ = 2.0


def _минуты(сегменты: list[dict[str, Any]]) -> float:
    звучало = sum(max(0.0, float(с.get("end") or с.get("end_s") or 0.0)
                      - float(с.get("start") or с.get("start_s") or 0.0))
                  for с in сегменты)
    return звучало / 60.0


def _паразитов(слова: list[str]) -> int:
    """Сколько слов реплики — паразиты или звуки заполнения пауз.

    Сравнение по точной форме, а не по основе: паразит — неизменяемая
    частица, а её основа совпадает с основой обычного слова («так» и
    «такая», «просто» и «простой», «слушай» и «слушаю»). Обороты из двух-трёх
    слов считаются одним паразитом и вычёркиваются, чтобы «как бы» не
    досчиталось ещё и как «как».
    """
    занято = [False] * len(слова)
    сколько = 0
    for i in range(len(слова)):
        if занято[i]:
            continue
        for длина in (3, 2):
            if tuple(слова[i:i + длина]) in ФРАЗЫ_ПАРАЗИТЫ:
                сколько += 1
                for j in range(i, min(i + длина, len(слова))):
                    занято[j] = True
                break
        else:
            if слова[i] in ЗВУКИ_ПАУЗЫ or слова[i] in ПАРАЗИТЫ:
                сколько += 1
                занято[i] = True
    return сколько


def analyze(segments: list[dict[str, Any]], duration_s: float) -> dict[str, Any]:
    """Речевые характеристики разговора целиком и по говорящим."""
    сегменты = [dict(с) for с in (segments or [])]
    for с in сегменты:
        с["start"] = float(с.get("start") or с.get("start_s") or 0.0)
        с["end"] = float(с.get("end") or с.get("end_s") or 0.0)
    сегменты.sort(key=lambda с: с["start"])

    длительность = float(duration_s or 0.0) or (
        max((с["end"] for с in сегменты), default=0.0))
    звучало = sum(max(0.0, с["end"] - с["start"]) for с in сегменты)

    по_говорящим: dict[str, dict[str, Any]] = {}
    паузы: list[float] = []
    перебивания: list[dict[str, Any]] = []
    монолог = {"speaker": None, "seconds": 0.0, "start_s": 0.0}
    текущий = {"speaker": None, "seconds": 0.0, "start_s": 0.0}
    ответы: dict[str, list[float]] = {}

    предыдущий: dict[str, Any] | None = None
    for с in сегменты:
        кто = str(с.get("speaker") or "—")
        запись = по_говорящим.setdefault(кто, {
            "speaker": кто, "seconds": 0.0, "words": 0, "segments": 0,
            "fillers": 0, "questions": 0})
        длина = max(0.0, с["end"] - с["start"])
        слова_реплики = words(str(с.get("text") or ""))
        запись["seconds"] += длина
        запись["words"] += len(слова_реплики)
        запись["segments"] += 1
        запись["fillers"] += _паразитов(слова_реплики)
        if "?" in str(с.get("text") or ""):
            запись["questions"] += 1

        if предыдущий is not None:
            разрыв = с["start"] - предыдущий["end"]
            тот_же = str(предыдущий.get("speaker") or "—") == кто
            if разрыв >= ПОРОГ_ПАУЗЫ:
                паузы.append(разрыв)
            if not тот_же:
                if разрыв <= ПОРОГ_ПЕРЕБИВАНИЯ:
                    перебивания.append({
                        "start_s": round(с["start"], 2), "speaker": кто,
                        "overlap_s": round(-разрыв, 2),
                        "interrupted": предыдущий.get("speaker")})
                elif 0 <= разрыв < ПОРОГ_ПАУЗЫ:
                    ответы.setdefault(кто, []).append(разрыв)

        # Монолог — непрерывная череда реплик одного говорящего.
        if текущий["speaker"] == кто:
            текущий["seconds"] += длина
        else:
            текущий = {"speaker": кто, "seconds": длина, "start_s": с["start"]}
        if текущий["seconds"] > монолог["seconds"]:
            монолог = dict(текущий)
        предыдущий = с

    минут = звучало / 60.0
    всего_слов = sum(з["words"] for з in по_говорящим.values())
    for з in по_говорящим.values():
        мин = з["seconds"] / 60.0
        з["seconds"] = round(з["seconds"], 1)
        з["share"] = round(з["seconds"] / звучало, 3) if звучало else None
        з["wpm"] = round(з["words"] / мин) if мин > 0.05 else None
        з["filler_rate"] = round(з["fillers"] / з["words"], 4) if з["words"] else 0.0
        задержки = ответы.get(з["speaker"]) or []
        з["reply_delay_s"] = round(sum(задержки) / len(задержки), 2) if задержки else None

    молчание = max(0.0, длительность - звучало)
    всего_паразитов = sum(з["fillers"] for з in по_говорящим.values())
    return {
        "duration_s": round(длительность, 1),
        "speech_s": round(звучало, 1),
        "silence_s": round(молчание, 1),
        "words": всего_слов,
        # Доля слов-паразитов по записи целиком — она и попадает в свод.
        # По говорящим она тоже есть, но для разрезов по корпусу нужна
        # одна цифра на запись, а «средняя по говорящим» ею быть не может:
        # оператор, сказавший три слова, весил бы столько же, сколько
        # клиент, говоривший двадцать минут.
        "filler_rate": (round(всего_паразитов / всего_слов, 4)
                        if всего_слов else 0.0),
        "fillers": всего_паразитов,
        "silence_share": round(молчание / длительность, 3) if длительность else None,
        "wpm": round(всего_слов / минут) if минут > 0.05 else None,
        "pauses": len(паузы),
        "longest_pause_s": round(max(паузы), 1) if паузы else 0.0,
        "interruptions": len(перебивания),
        "interruption_examples": перебивания[:10],
        "monologue": {"speaker": монолог["speaker"],
                      "seconds": round(монолог["seconds"], 1),
                      "start_s": round(монолог["start_s"], 1)},
        "speakers": sorted(по_говорящим.values(), key=lambda з: -(з["seconds"] or 0)),
    }
