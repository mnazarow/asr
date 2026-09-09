"""Полный разбор одной записи: всё, что можно узнать из расшифровки.

Собирается из отдельных разборов — оценка, речь, сущности, ключевые слова,
скрипт — и дополняется тем, что видно только на целом: вопросы,
обязательства, тревожные признаки, итоговая сводка.

Разбор дешёвый: чистые строковые операции без обращений наружу. Часовой
разговор на десять тысяч слов разбирается за десятки миллисекунд, поэтому
он делается сразу после распознавания и складывается в базу — считать его
заново на каждый показ раздела было бы расточительно, а на архиве в сто
тысяч записей просто невозможно.
"""
from __future__ import annotations

import re
from typing import Any

from . import compliance, entities, keywords, sentiment, speech
from .lexicons import (
    ВЕЖЛИВОСТЬ,
    ВОПРОСИТЕЛЬНЫЕ,
    НЕ_ВОПРОСЫ,
    НЕ_ТРЕВОЖНЫЕ,
    ОБЯЗАТЕЛЬСТВА,
    СООТНОСИТЕЛЬНЫЕ,
    СРОКИ,
    ТРЕВОЖНЫЕ,
)
from .stemmer import sentences, stem, words

#: Версия разбора. Меняется, когда меняются словари или правила: по ней
#: видно, какие записи разобраны старым набором и требуют пересчёта.
VERSION = 1

_ЗНАК_ВОПРОСА = re.compile(r"\?")


def _вопросы(сегменты: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Вопросы разговора.

    Ищем и по знаку, и по вопросительным словам: распознавание ставит знаки
    само и теряет их постоянно, а «когда вы отправите» без знака — всё
    равно вопрос.
    """
    найдено = []
    for с in сегменты:
        текст = str(с.get("text") or "")
        for предложение in sentences(текст) or [текст]:
            основы = [stem(w) for w in words(предложение)]
            if not основы:
                continue
            по_знаку = bool(_ЗНАК_ВОПРОСА.search(предложение))
            # Вопросительное слово засчитываем только в начале: «как» в
            # середине фразы чаще союз («так же, как вчера»), чем вопрос.
            #
            # Одного начала мало. Соотносительные обороты в русском
            # начинаются теми же словами, и правило засчитывало вопросами
            # «Как я уже сказал…», «Что касается доставки…», «Сколько нужно,
            # столько и сделаем» — четыре утверждения из четырёх. Поэтому
            # отбрасываем known-обороты по первым двум основам и
            # предложения, где дальше стоит соотносительное слово.
            по_слову = (основы[0] in ВОПРОСИТЕЛЬНЫЕ
                        and tuple(основы[:2]) not in НЕ_ВОПРОСЫ
                        and not any(о in СООТНОСИТЕЛЬНЫЕ for о in основы[1:]))
            if по_знаку or по_слову:
                найдено.append({
                    "start_s": float(с.get("start") or с.get("start_s") or 0.0),
                    "speaker": с.get("speaker"),
                    "text": предложение.strip()[:300],
                })
    return найдено


def _обязательства(сегменты: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Обещания и сроки — то, за что потом спросят.

    Разрез ценен именно этим: «перезвоню завтра» в записи есть, а в системе
    учёта его нет, и вспоминают о нём, когда клиент звонит сам.
    """
    найдено = []
    for с in сегменты:
        текст = str(с.get("text") or "")
        for предложение in sentences(текст) or [текст]:
            основы = [stem(w) for w in words(предложение)]
            обещание = next((о for о in основы if о in ОБЯЗАТЕЛЬСТВА), None)
            if обещание is None:
                continue
            сроки = entities.extract(предложение)
            срок = (сроки["deadlines"] or sorted(сроки["dates"]) or
                    [о for о in основы if о in СРОКИ])
            найдено.append({
                "start_s": float(с.get("start") or с.get("start_s") or 0.0),
                "speaker": с.get("speaker"),
                "text": предложение.strip()[:300],
                "deadline": срок[0] if срок else None,
            })
    return найдено


def _вежливость(текст: str) -> dict[str, bool]:
    низкий = " " + (текст or "").lower().replace("ё", "е") + " "
    return {назначение: any(в.replace("ё", "е") in низкий for в in варианты)
            for назначение, варианты in ВЕЖЛИВОСТЬ.items()}


def _тревожные(сегменты: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Упоминания суда, жалобы, огласки — повод послушать запись целиком."""
    найдено = []
    for с in сегменты:
        текст = str(с.get("text") or "")
        слова_ = words(текст)
        сработали = sorted({w for w in слова_
                            if stem(w) in ТРЕВОЖНЫЕ and w not in НЕ_ТРЕВОЖНЫЕ})
        if сработали:
            найдено.append({
                "start_s": float(с.get("start") or с.get("start_s") or 0.0),
                "speaker": с.get("speaker"),
                "words": сработали,
                "text": текст.strip()[:300],
            })
    return найдено


def analyze(*, text: str, segments: list[dict[str, Any]] | None = None,
            duration_s: float = 0.0,
            script: list[dict[str, Any]] | None = None,
            agent_speaker: str | None = None,
            document_frequency: dict[str, int] | None = None,
            corpus_size: int = 0) -> dict[str, Any]:
    """Разбор одной записи.

    `agent_speaker` — кто из говорящих оператор: по нему проверяется скрипт.
    `document_frequency` и `corpus_size` — сведения о корпусе для TF-IDF;
    без них ключевые слова считаются по простой частоте.
    """
    сегменты = list(segments or [])
    if not сегменты and text:
        # Записи без разбора на реплики тоже разбираются — по одной
        # «реплике» на всю запись. Речевые характеристики при этом
        # вырождаются, но оценка, сущности и ключевые слова работают.
        сегменты = [{"start": 0.0, "end": float(duration_s or 0.0), "text": text}]

    целиком = text or " ".join(str(с.get("text") or "") for с in сегменты)
    оценки = sentiment.score_segments(сегменты)
    общая = sentiment.score_text(целиком)
    речь = speech.analyze(сегменты, duration_s)
    вопросы = _вопросы(сегменты)
    обещания = _обязательства(сегменты)
    тревога = _тревожные(сегменты)

    отрицательные = sorted((о for о in оценки if о["score"] < -sentiment.ПОРОГ),
                           key=lambda о: о["score"])
    положительные = sorted((о for о in оценки if о["score"] > sentiment.ПОРОГ),
                           key=lambda о: -о["score"])

    return {
        "version": VERSION,
        "sentiment": {
            **общая.to_dict(),
            "trajectory": sentiment.trajectory(оценки),
            "turn": sentiment.turn(оценки),
            "negative_segments": len(отрицательные),
            "positive_segments": len(положительные),
            "worst": отрицательные[:5],
            "best": положительные[:5],
            "by_speaker": _по_говорящим(оценки),
        },
        "speech": речь,
        "entities": entities.extract(целиком),
        "keywords": keywords.keywords(целиком, document_frequency=document_frequency,
                                      corpus_size=corpus_size),
        "phrases": keywords.phrases(целиком),
        "questions": {"count": len(вопросы), "items": вопросы[:30]},
        "commitments": {"count": len(обещания),
                        "with_deadline": sum(1 for о in обещания if о["deadline"]),
                        "items": обещания[:30]},
        "politeness": _вежливость(целиком),
        "alerts": {"count": len(тревога), "items": тревога[:10]},
        "compliance": compliance.check(сегменты, script=script, speaker=agent_speaker),
        # Основы записи для корпусного знаменателя. Считаются здесь, а не
        # отдельным проходом при сохранении: текст уже разобран на слова, и
        # второй проход по часовой расшифровке ради того же результата —
        # это ровно вдвое больше работы на каждую запись архива.
        "terms": keywords.corpus_terms(целиком),
    }


def features(разбор: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Раскладывает разбор на то, как он лежит в базе.

    Первое — свод: два десятка чисел, по которым считаются разрезы по
    всему архиву. Второе — основы для знаменателя TF-IDF. Всё остальное
    уходит в `detail` одним полем: карточке записи оно нужно целиком, а
    сводам — никогда, и поднимать килобайты JSON ради средней тональности
    за месяц не придётся.
    """
    разбор = dict(разбор)
    основы = разбор.pop("terms", None) or {}
    тон = разбор.get("sentiment") or {}
    речь = разбор.get("speech") or {}
    сущности = разбор.get("entities") or {}
    вопросы = разбор.get("questions") or {}
    обещания = разбор.get("commitments") or {}
    тревога = разбор.get("alerts") or {}
    скрипт = разбор.get("compliance") or {}
    свод = {
        "version": int(разбор.get("version") or VERSION),
        "sentiment": тон.get("score"),
        "sentiment_label": тон.get("label"),
        "sentiment_shift": (тон.get("turn") or {}).get("shift"),
        "negative_segments": тон.get("negative_segments"),
        "positive_segments": тон.get("positive_segments"),
        "wpm": речь.get("wpm"),
        "silence_share": речь.get("silence_share"),
        "interruptions": речь.get("interruptions"),
        "pauses": речь.get("pauses"),
        "longest_pause_s": речь.get("longest_pause_s"),
        "filler_rate": речь.get("filler_rate"),
        "questions": вопросы.get("count"),
        "commitments": обещания.get("count"),
        "commitments_dated": обещания.get("with_deadline"),
        "alerts": тревога.get("count"),
        "compliance": скрипт.get("score"),
        "money_max": сущности.get("money_max"),
        "speakers": len(речь.get("speakers") or []),
        "agent_speaker": скрипт.get("speaker"),
        "detail": разбор,
    }
    return свод, основы


def _по_говорящим(оценки: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Оценка каждого говорящего отдельно.

    Средняя по разговору смешивает раздражённого клиента с ровным
    оператором и получает «нейтрально» — то есть теряет ровно то, что
    хотели узнать.
    """
    по: dict[str, list[float]] = {}
    for о in оценки:
        по.setdefault(str(о.get("speaker") or "—"), []).append(float(о["score"]))
    out = []
    for кто, значения in по.items():
        среднее = sum(значения) / len(значения)
        out.append({"speaker": кто, "score": round(среднее, 3),
                    "segments": len(значения),
                    "label": ("отрицательная" if среднее < -sentiment.ПОРОГ else
                              "положительная" if среднее > sentiment.ПОРОГ else
                              "нейтральная")})
    return sorted(out, key=lambda з: з["score"])
