"""Калибровка уверенности: насколько уверенности модели можно верить.

Уверенность — не точность: Google, Amazon и Riva предупреждают об этом
в один голос. Но у записей с эталоном можно измерить, *насколько* она
не точность: если модель говорит «0,9», верны ли девять слов из десяти?
Это и есть калибровка. Мера — ECE (expected calibration error): слова
раскладываются по корзинам уверенности, и в каждой доля верных
сравнивается со средней уверенностью; ECE — взвешенное по числу слов
расхождение. Ноль — уверенность честна, 0,2 — модель обещает на двадцать
пунктов больше, чем даёт.

Считается по каждому заданию с эталоном сразу — когда эталон задан — и
складывается в задание десятью корзинами. Свод по периоду складывает
корзины, а не пересчитывает выравнивание: так отчёт не читает сегменты
всего архива.

Слово берётся с его собственной уверенностью, если движок её отдал
(семейство Whisper), иначе — с уверенностью сегмента; чей источник,
задание помнит. Верно ли слово, решает выравнивание с эталоном
(`metrics.align_hits`).
"""
from __future__ import annotations

import json
from typing import Any

from . import metrics as M

#: Корзины уверенности: 0–0,1, …, 0,9–1,0. Десять — как в исходной работе
#: об ECE (Naeini et al., 2015); мельче — пустые корзины на малых выборках.
КОРЗИН = 10


def _корзина(уверенность: float) -> int:
    return min(КОРЗИН - 1, max(0, int(уверенность * КОРЗИН)))


def per_job(segments: list[dict[str, Any]], reference: str) -> dict[str, Any]:
    """Корзины калибровки одного задания по его сегментам и эталону.

    Возвращает `{"words": N, "source": "word"|"segment", "bins": [[n, Σconf, hits], …]}`;
    `words` — сколько слов вошло в расчёт (у слов без уверенности его нет).
    """
    токены: list[str] = []
    уверенности: list[float | None] = []
    источник = "segment"
    for сегмент in segments:
        слова = сегмент.get("words") or []
        по_словам = [w for w in слова if isinstance(w, dict)
                     and w.get("confidence") is not None and str(w.get("word") or "").strip()]
        if по_словам:
            источник = "word"
            for w in по_словам:
                # Одно «слово» движка может дать два токена («по-моему») или
                # ни одного (знак препинания): уверенность достаётся всем его
                # токенам поровну, а пустое просто не считается.
                for токен in M.normalize(str(w["word"])).split():
                    токены.append(токен)
                    уверенности.append(float(w["confidence"]))
            continue
        c = сегмент.get("confidence")
        for токен in M.normalize(str(сегмент.get("text") or "")).split():
            токены.append(токен)
            уверенности.append(float(c) if c is not None else None)

    эталон = M.normalize(reference).split()
    верно = M.align_hits(эталон, токены)
    корзины = [[0, 0.0, 0] for _ in range(КОРЗИН)]
    слов = 0
    for c, hit in zip(уверенности, верно, strict=True):
        if c is None:
            continue
        c = max(0.0, min(1.0, c))
        к = корзины[_корзина(c)]
        к[0] += 1
        к[1] += c
        к[2] += 1 if hit else 0
        слов += 1
    return {"words": слов, "source": источник,
            "bins": [[n, round(s, 4), h] for n, s, h in корзины]}


def aggregate(записи: list[dict[str, Any] | str | None]) -> dict[str, Any]:
    """Свод корзин нескольких заданий: диаграмма надёжности, ECE, AUC.

    AUC считается по корзинам — вероятность того, что случайное верное
    слово попало в корзину выше, чем случайное неверное (совпавшие
    корзины — за половину). Это AUC с округлением до корзины, а не по
    каждому слову: точнее хранить незачем.
    """
    итог = [[0, 0.0, 0] for _ in range(КОРЗИН)]
    заданий = 0
    источники: dict[str, int] = {}
    for запись in записи:
        if not запись:
            continue
        if isinstance(запись, str):
            try:
                запись = json.loads(запись)
            except (TypeError, ValueError):
                continue
        корзины = запись.get("bins") if isinstance(запись, dict) else None
        if not isinstance(корзины, list) or len(корзины) != КОРЗИН:
            continue
        заданий += 1
        источники[str(запись.get("source") or "segment")] = \
            источники.get(str(запись.get("source") or "segment"), 0) + 1
        for i, (n, s, h) in enumerate(корзины):
            итог[i][0] += int(n)
            итог[i][1] += float(s)
            итог[i][2] += int(h)

    всего = sum(к[0] for к in итог)
    верных = sum(к[2] for к in итог)
    неверных = всего - верных
    ece = 0.0
    строки = []
    for i, (n, s, h) in enumerate(итог):
        conf = (s / n) if n else None
        acc = (h / n) if n else None
        if n:
            ece += (n / всего) * abs(acc - conf)
        строки.append({"from": round(i / КОРЗИН, 1), "to": round((i + 1) / КОРЗИН, 1),
                       "words": n,
                       "confidence": round(conf, 4) if conf is not None else None,
                       "accuracy": round(acc, 4) if acc is not None else None,
                       "gap": round(conf - acc, 4) if conf is not None else None})
    auc = None
    if верных and неверных:
        # Неверные ниже текущей корзины — уже пройденные.
        ниже = 0
        сумма = 0.0
        for n, _, h in итог:
            w = n - h
            сумма += h * ниже + 0.5 * h * w
            ниже += w
        auc = сумма / (верных * неверных)
    # Средняя уверенность против средней точности — знак говорит, куда
    # модель врёт: плюс — переоценивает себя.
    conf_all = (sum(к[1] for к in итог) / всего) if всего else None
    acc_all = (верных / всего) if всего else None
    return {
        "jobs": заданий, "words": всего,
        "ece": round(ece, 4) if всего else None,
        "auc": round(auc, 4) if auc is not None else None,
        "confidence": round(conf_all, 4) if conf_all is not None else None,
        "accuracy": round(acc_all, 4) if acc_all is not None else None,
        "overconfidence": round(conf_all - acc_all, 4) if всего else None,
        "bins": строки,
        "sources": источники,
    }
