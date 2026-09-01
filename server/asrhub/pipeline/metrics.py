"""Метрики качества распознавания: WER, CER, выравнивание и разбор ошибок."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")

# Числительные для сопоставления «25» и «двадцать пять» при нормализации
_DIGIT_WORDS_RU = {
    "0": "ноль", "1": "один", "2": "два", "3": "три", "4": "четыре",
    "5": "пять", "6": "шесть", "7": "семь", "8": "восемь", "9": "девять",
}


def normalize(text: str, *, lowercase: bool = True, drop_punct: bool = True,
              yo_to_e: bool = True, expand_digits: bool = False) -> str:
    """Приводит текст к сопоставимому виду перед расчётом метрик.

    Нормализация обязательна: без неё WER измеряет разницу в пунктуации
    и регистре, а не в распознанных словах.
    """
    text = unicodedata.normalize("NFC", text)
    if lowercase:
        text = text.lower()
    if yo_to_e:
        text = text.replace("ё", "е").replace("Ё", "Е")
    if expand_digits:
        text = "".join(f" {_DIGIT_WORDS_RU[ch]} " if ch in _DIGIT_WORDS_RU else ch for ch in text)
    if drop_punct:
        text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


@dataclass(slots=True)
class ErrorCounts:
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    hits: int = 0

    @property
    def total_reference(self) -> int:
        return self.hits + self.substitutions + self.deletions

    @property
    def error_rate(self) -> float:
        total = self.total_reference
        if total == 0:
            return 0.0 if self.insertions == 0 else 1.0
        return (self.substitutions + self.deletions + self.insertions) / total

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["error_rate"] = round(self.error_rate, 6)
        data["total_reference"] = self.total_reference
        return data


def _levenshtein(ref: Sequence[str], hyp: Sequence[str]) -> ErrorCounts:
    """Расстояние Левенштейна с подсчётом типов ошибок.

    Реализация с двумя строками матрицы: память O(min(n, m)),
    что важно для многочасовых расшифровок.
    """
    n, m = len(ref), len(hyp)
    if n == 0:
        return ErrorCounts(insertions=m)
    if m == 0:
        return ErrorCounts(deletions=n)

    # Каждая ячейка хранит (стоимость, замены, удаления, вставки, совпадения)
    prev: list[tuple[int, int, int, int, int]] = [(j, 0, 0, j, 0) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur: list[tuple[int, int, int, int, int]] = [(i, 0, i, 0, 0)]
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                c, s, d, ins, h = prev[j - 1]
                cur.append((c, s, d, ins, h + 1))
                continue
            sub = prev[j - 1]
            dele = prev[j]
            ins_cell = cur[j - 1]
            best = min(
                (sub[0] + 1, sub[1] + 1, sub[2], sub[3], sub[4]),
                (dele[0] + 1, dele[1], dele[2] + 1, dele[3], dele[4]),
                (ins_cell[0] + 1, ins_cell[1], ins_cell[2], ins_cell[3] + 1, ins_cell[4]),
                key=lambda t: t[0],
            )
            cur.append(best)
        prev = cur
    _, subs, dels, ins, hits = prev[m]
    return ErrorCounts(substitutions=subs, deletions=dels, insertions=ins, hits=hits)


def wer(reference: str, hypothesis: str, **norm: Any) -> float:
    """Доля ошибок по словам (0 — идеально, 1 — все слова неверны)."""
    ref = normalize(reference, **norm).split()
    hyp = normalize(hypothesis, **norm).split()
    return _levenshtein(ref, hyp).error_rate


def cer(reference: str, hypothesis: str, **norm: Any) -> float:
    """Доля ошибок по символам. Устойчивее WER на языках с богатой морфологией."""
    ref = list(normalize(reference, **norm).replace(" ", ""))
    hyp = list(normalize(hypothesis, **norm).replace(" ", ""))
    return _levenshtein(ref, hyp).error_rate


def detailed(reference: str, hypothesis: str, **norm: Any) -> dict[str, Any]:
    """Полный разбор: WER, CER, счётчики ошибок и список расхождений."""
    ref_words = normalize(reference, **norm).split()
    hyp_words = normalize(hypothesis, **norm).split()
    counts = _levenshtein(ref_words, hyp_words)
    char_counts = _levenshtein(list(" ".join(ref_words)), list(" ".join(hyp_words)))
    return {
        "wer": round(counts.error_rate, 6),
        "cer": round(char_counts.error_rate, 6),
        "words": counts.to_dict(),
        "chars": char_counts.to_dict(),
        "reference_words": len(ref_words),
        "hypothesis_words": len(hyp_words),
        "diff": diff_words(ref_words, hyp_words)[:500],
    }


def diff_words(ref: Sequence[str], hyp: Sequence[str]) -> list[dict[str, Any]]:
    """Пословное выравнивание для подсветки ошибок в интерфейсе."""
    n, m = len(ref), len(hyp)
    if n * m > 4_000_000:      # защита от квадратичного взрыва на длинных текстах
        return []
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(dp[i - 1][j - 1] + cost, dp[i - 1][j] + 1, dp[i][j - 1] + 1)
    out: list[dict[str, Any]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1]:
            out.append({"op": "ok", "ref": ref[i - 1], "hyp": hyp[j - 1]})
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            out.append({"op": "sub", "ref": ref[i - 1], "hyp": hyp[j - 1]})
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            out.append({"op": "del", "ref": ref[i - 1], "hyp": ""})
            i -= 1
        else:
            out.append({"op": "ins", "ref": "", "hyp": hyp[j - 1]})
            j -= 1
    out.reverse()
    return out


def percentile(values: Sequence[float], q: float) -> float:
    """Перцентиль по методу линейной интерполяции."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = (len(ordered) - 1) * max(0.0, min(1.0, q))
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return float(ordered[low] * (1 - frac) + ordered[high] * frac)


def summarize(values: Sequence[float]) -> dict[str, float]:
    """Сводка распределения: среднее, медиана, перцентили, разброс."""
    if not values:
        return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0,
                "p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "stdev": 0.0}
    data = [float(v) for v in values]
    avg = sum(data) / len(data)
    variance = sum((v - avg) ** 2 for v in data) / len(data)
    return {
        "count": len(data),
        "avg": round(avg, 6),
        "min": round(min(data), 6),
        "max": round(max(data), 6),
        "p50": round(percentile(data, 0.50), 6),
        "p90": round(percentile(data, 0.90), 6),
        "p95": round(percentile(data, 0.95), 6),
        "p99": round(percentile(data, 0.99), 6),
        "stdev": round(variance ** 0.5, 6),
    }


def confidence_buckets(values: Sequence[float], edges: Sequence[float] = (0.5, 0.7, 0.85, 0.95)
                       ) -> list[dict[str, Any]]:
    """Распределение уверенности по интервалам — для гистограммы в аналитике."""
    bounds = [0.0, *edges, 1.0001]
    buckets = []
    for idx in range(len(bounds) - 1):
        low, high = bounds[idx], bounds[idx + 1]
        count = sum(1 for v in values if low <= v < high)
        buckets.append({
            "from": round(low, 3),
            "to": round(min(high, 1.0), 3),
            "count": count,
            "share": round(count / len(values), 4) if values else 0.0,
        })
    return buckets


def speaking_rate(words: int, duration_s: float) -> float:
    """Темп речи в словах в минуту — полезный индикатор качества записи."""
    if duration_s <= 0:
        return 0.0
    return round(words / duration_s * 60, 1)
