"""Поиск по смыслу: векторы частей записи и близость к запросу.

Поиск по словам находит «доставку», но не находит «курьер так и не
приехал» по запросу «недовольны сроками». Векторы смысла (эмбеддинги)
находят: близкие по смыслу фразы дают близкие векторы, какими бы словами
они ни были сказаны.

Что векторизуется у записи: пересказ модели с причиной и исходом (часть
0) и куски самой расшифровки по ~800 знаков (части 1…N, не больше
`ЧАСТЕЙ`). Расшифровка — обезличенная, если включено `llm_mask_pii`:
векторы уходят на тот же сервер модели и лежат в базе с куском текста.

Поиск — косинусная близость запроса ко всем векторам окна; у записи
берётся лучшая из её частей, в ответ — запись, часть и кусок текста.
Перебор, а не указатель: на сотне тысяч записей это секунды, и ради них
не стоит тащить отдельную векторную базу. Включается `llm_embeddings`.
"""
from __future__ import annotations

import array
import math
from typing import Any

from ..logging_setup import get_logger
from . import tasks

log = get_logger("llm")

#: Сколько кусков расшифровки векторизовать у одной записи.
ЧАСТЕЙ = 12

#: Размер куска расшифровки, знаков.
КУСОК = 800

#: Ниже этой близости совпадение не показывается: это уже не «по смыслу».
ПОРОГ = 0.25


def _куски(текст: str) -> list[str]:
    куски = []
    for кусок in tasks.chunks_of(текст, max(500, КУСОК)):
        куски.append(кусок)
        if len(куски) >= ЧАСТЕЙ:
            break
    return куски


def тексты_записи(db: Any, settings: Any, job_id: str,
                  ответ: dict[str, Any] | None = None) -> list[tuple[int, str]]:
    """Что векторизовать у записи: (номер части, текст)."""
    ответ = ответ or db.llm_get(job_id) or {}
    части: list[tuple[int, str]] = []
    пересказ = " ".join(str(ответ.get(к) or "") for к in ("summary", "reason", "outcome")
                        if ответ.get(к)).strip()
    if пересказ:
        части.append((0, пересказ))
    job = db.get_job(job_id) or {}
    разговор = tasks.transcript_of(db.get_segments(job_id), str(job.get("text") or ""))
    if разговор and settings.get("llm_mask_pii", True):
        from ..content import masking  # noqa: PLC0415

        разговор = masking.mask_text(разговор)
    for номер, кусок in enumerate(_куски(разговор), 1):
        части.append((номер, кусок))
    return части


def проиндексировать(db: Any, client: Any, settings: Any, job_id: str,
                     ответ: dict[str, Any] | None = None) -> int:
    """Векторы частей записи — в базу. Возвращает число частей."""
    части = тексты_записи(db, settings, job_id, ответ)
    if not части:
        return 0
    векторы = client.embed([т for _, т in части])
    db.vectors_put(job_id, [(н, т, в) for (н, т), в in zip(части, векторы, strict=True)],
                   model=client.embed_model)
    return len(части)


def дополнить(db: Any, client: Any, settings: Any, *, limit: int = 50) -> dict[str, Any]:
    """Векторы записей, разобранных моделью, у которых их ещё нет."""
    сделано = ошибок = 0
    последняя = ""
    for job_id in db.vectors_missing(model=client.embed_model, limit=limit):
        try:
            проиндексировать(db, client, settings, job_id)
            сделано += 1
        except Exception as exc:                             # noqa: BLE001
            ошибок += 1
            последняя = str(exc)
            log.warning("Векторы записи %s не посчитаны: %s", job_id, exc)
            if ошибок >= 3 and not сделано:
                break
    return {"indexed": сделано, "errors": ошибок, "last_error": последняя or None,
            "left": len(db.vectors_missing(model=client.embed_model, limit=limit))}


def _близость(а: list[float], б_байты: bytes, длина_а: float) -> float:
    б = array.array("f")
    б.frombytes(б_байты)
    if len(б) != len(а):
        return 0.0
    скаляр = sum(x * y for x, y in zip(а, б, strict=True))
    длина_б = math.sqrt(sum(y * y for y in б)) or 1.0
    return скаляр / (длина_а * длина_б)


def найти(db: Any, client: Any, запрос: str, *, owner: Any = None,
          since: float | None = None, until: float | None = None,
          limit: int = 20) -> dict[str, Any]:
    """Записи, близкие к запросу по смыслу, — лучшие первыми."""
    вектор = client.embed([запрос])[0]
    длина = math.sqrt(sum(x * x for x in вектор)) or 1.0
    лучшие: dict[str, dict[str, Any]] = {}
    просмотрено = 0
    for строка in db.vectors_iter(model=client.embed_model, owner=owner,
                                  since=since, until=until):
        просмотрено += 1
        близость = _близость(вектор, строка["vector"], длина)
        if близость < ПОРОГ:
            continue
        ид = str(строка["job_id"])
        прежнее = лучшие.get(ид)
        if прежнее is None or близость > прежнее["score"]:
            лучшие[ид] = {"job_id": ид, "score": round(близость, 4), "part": int(строка["part"]),
                          "text": str(строка["text"] or "")[:400],
                          "filename": строка["filename"], "created_at": строка["created_at"]}
    найдено = sorted(лучшие.values(), key=lambda з: -з["score"])[:max(1, int(limit))]
    return {"query": запрос, "model": client.embed_model, "items": найдено,
            "scanned": просмотрено, "threshold": ПОРОГ}


#: Близость пересказов, с которой записи — одна тема.
ПОРОГ_ТЕМЫ = 0.75


def кучки(db: Any, client: Any, *, owner: Any = None, since: float | None = None,
          until: float | None = None, порог: float = 0.0,
          минимум: int = 0) -> tuple[list[Any], int]:
    """Темы по смыслу: записи периода, собранные по близости пересказов.

    То же «собрать вожаками», что у тем по словам (`topics.собрать`), но
    сходство — косинус векторов пересказа (часть 0), а не TF-IDF основ:
    «курьер не приехал» и «доставку так и не привезли» — одна тема.
    """
    from .. import topics as темы_модуль  # noqa: PLC0415

    порог = порог or ПОРОГ_ТЕМЫ
    минимум = минимум or темы_модуль.МИНИМУМ_ЗАПИСЕЙ
    вожаки: list[tuple[list[float], Any, dict[str, int]]] = []
    всего = 0
    for строка in db.vectors_iter(model=client.embed_model, owner=owner,
                                  since=since, until=until):
        if int(строка["part"]) != 0:
            continue
        всего += 1
        вектор = array.array("f")
        вектор.frombytes(строка["vector"])
        длина = math.sqrt(sum(x * x for x in вектор)) or 1.0
        вектор_н = [x / длина for x in вектор]
        лучший, лучшая = -1, -1.0
        for н, (центр, _, _) in enumerate(вожаки):
            if len(центр) != len(вектор_н):
                continue
            близость = sum(x * y for x, y in zip(вектор_н, центр, strict=True))
            if близость > лучшая:
                лучший, лучшая = н, близость
        слова = {с for с in (str(строка["text"] or "").lower().replace(",", " ").split())
                 if len(с) >= 5}
        if лучший >= 0 and лучшая >= порог:
            тема = вожаки[лучший][1]
            тема.jobs.append(str(строка["job_id"]))
            for с in слова:
                вожаки[лучший][2][с] = вожаки[лучший][2].get(с, 0) + 1
        elif len(вожаки) < темы_модуль.МАКС_ВОЖАКОВ:
            вожаки.append((вектор_н, темы_модуль.Тема(jobs=[str(строка["job_id"])]),
                           dict.fromkeys(слова, 1)))
    темы = []
    for _, тема, слова in вожаки:
        if тема.size < темы_модуль.максимум_минимума(минимум):
            continue
        тема.stems = [с for с, _ in sorted(слова.items(), key=lambda п: (-п[1], п[0]))][:12]
        темы.append(тема)
    темы.sort(key=lambda т: -т.size)
    return темы[:темы_модуль.СКОЛЬКО_ТЕМ], всего


__all__ = ["дополнить", "кучки", "найти", "проиндексировать", "тексты_записи"]
