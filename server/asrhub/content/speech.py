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

#: Пауза, начиная с которой молчание уже заметно собеседнику: три секунды —
#: порог «заметной тишины» у NICE и Amazon Contact Lens, у Genesys две, у
#: Google пять. Считается отдельно от пауз: пауза в две секунды — это
#: обдумывание, а сумма пауз от трёх — время, которое клиент ждал.
ПОРОГ_ЗАМЕТНОЙ_ТИШИНЫ = 3.0


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
    # Смены говорящего и наложение речи — то, что Gong называет
    # интерактивностью, а Genesys считает в секундах: сколько раз речь
    # переходила от стороны к стороне и сколько времени обе говорили сразу.
    смен = 0
    наложение = 0.0
    заметная_тишина = 0.0

    предыдущий: dict[str, Any] | None = None
    for с in сегменты:
        кто = str(с.get("speaker") or "—")
        запись = по_говорящим.setdefault(кто, {
            "speaker": кто, "seconds": 0.0, "words": 0, "segments": 0,
            "fillers": 0, "questions": 0, "monologue_s": 0.0})
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
            if разрыв >= ПОРОГ_ЗАМЕТНОЙ_ТИШИНЫ:
                заметная_тишина += разрыв
            if not тот_же:
                смен += 1
                if разрыв < 0:
                    наложение += -разрыв
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
        if текущий["seconds"] > запись["monologue_s"]:
            запись["monologue_s"] = текущий["seconds"]
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
        з["monologue_s"] = round(з["monologue_s"], 1)

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
        # Наложение — в секундах и долей длительности: число перебиваний
        # говорит, сколько раз, а это — сколько времени говорили хором.
        "overlap_s": round(наложение, 1),
        "overlap_share": round(наложение / длительность, 4) if длительность else None,
        "dead_air_s": round(заметная_тишина, 1),
        "dead_air_share": (round(заметная_тишина / длительность, 4)
                           if длительность else None),
        # Смены говорящего: и всего, и на минуту разговора, потому что за
        # час их естественно больше, чем за пять минут.
        "switches": смен,
        "switches_per_min": round(смен / минут, 2) if минут > 0.05 else None,
        "monologue": {"speaker": монолог["speaker"],
                      "seconds": round(монолог["seconds"], 1),
                      "start_s": round(монолог["start_s"], 1)},
        "speakers": sorted(по_говорящим.values(), key=lambda з: -(з["seconds"] or 0)),
    }


def sides(речь: dict[str, Any], agent: str | None) -> dict[str, Any]:
    """Стороны разговора: оператор и клиент — и показатели между ними.

    Половина показателей мировой практики существует только «между
    сторонами»: доля речи оператора, его самый долгий монолог против самого
    долгого рассказа клиента, пауза перед ответом, соотношение темпов.
    Клиентом считается самый говорливый из тех, кто не оператор: в звонке
    сторон две, а третий говорящий — обычно ошибка разделения.

    Без оператора всё это None: считать «долю речи оператора», не зная,
    кто оператор, значило бы выдать за неё долю случайного говорящего.
    """
    говорящие = {з["speaker"]: з for з in (речь.get("speakers") or [])}
    пусто = {"agent": None, "customer": None, "talk_share": None,
             "monologue_s": None, "customer_story_s": None,
             "reply_delay_s": None, "customer_reply_delay_s": None,
             "tempo_ratio": None}
    if not agent or agent not in говорящие:
        return пусто
    оператор = говорящие[agent]
    остальные = [з for к, з in говорящие.items() if к != agent]
    клиент = max(остальные, key=lambda з: з["seconds"] or 0) if остальные else None
    темп_оп, темп_кл = оператор.get("wpm"), (клиент or {}).get("wpm")
    return {
        "agent": agent,
        "customer": клиент["speaker"] if клиент else None,
        "talk_share": оператор.get("share"),
        "monologue_s": оператор.get("monologue_s"),
        "customer_story_s": (клиент or {}).get("monologue_s"),
        "reply_delay_s": оператор.get("reply_delay_s"),
        "customer_reply_delay_s": (клиент or {}).get("reply_delay_s"),
        "tempo_ratio": (round(темп_оп / темп_кл, 2)
                        if темп_оп and темп_кл else None),
    }
