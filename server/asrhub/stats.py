"""Статистика для норм, дрейфа и контрольных карт — без сторонних библиотек.

Три инструмента, и у каждого — один вопрос.

* **Норма от своего архива** — медиана и межквартильный размах (IQR):
  «обычная величина» показателя и коридор, в котором лежит половина
  записей. Порог «темп речи 140–160 слов в минуту» из чужой методики для
  русской службы поддержки ничего не значит; своя медиана за четыре недели
  — значит. Выброс — дальше полутора размахов от квартилей (правило Тьюки).
* **Дрейф распределения** — двухвыборочный критерий Колмогорова — Смирнова:
  насколько распределение уверенности за период отличается от базового.
  Среднее может не сдвинуться, когда половина записей стала лучше, а
  половина хуже; KS видит и это. Пороги — по Deepgram: 10 % сдвига и
  p < 0,05 вместе — критично, что-то одно — предупреждение.
* **Контрольная карта** — пределы μ ± 2σ и μ ± 3σ по базовому окну и
  точки текущего окна за ними; плюс правило «семь подряд по одну сторону
  от среднего» — сдвиг, который ни одна точка по отдельности не выдаёт.

Всё считается на списках чисел: вызывающая сторона решает, что базовое
окно, а что текущее, — обычно четыре недели до периода и сам период.
"""
from __future__ import annotations

import math
from typing import Any

#: Сколько значений нужно, чтобы норма или пределы что-то значили. Меньше
#: — квартили скачут от одной записи, и «выше нормы» ничего не сообщает.
МИН_ВЫБОРКА = 30

#: Множитель правила Тьюки для выбросов.
ТЬЮКИ = 1.5

#: Правило серии: столько точек подряд по одну сторону от среднего —
#: сдвиг процесса (правило 4 Western Electric берёт восемь, Nelson — девять;
#: семь — распространённый компромисс).
СЕРИЯ = 7


def percentile(значения: list[float], доля: float) -> float | None:
    """Процентиль с линейной интерполяцией (как numpy по умолчанию)."""
    if not значения:
        return None
    ряд = sorted(float(x) for x in значения)
    if len(ряд) == 1:
        return ряд[0]
    позиция = (len(ряд) - 1) * max(0.0, min(1.0, доля))
    нижний = int(math.floor(позиция))
    верхний = min(нижний + 1, len(ряд) - 1)
    вес = позиция - нижний
    return ряд[нижний] * (1 - вес) + ряд[верхний] * вес


def percentiles(значения: list[float], доли: tuple[float, ...] = (0.1, 0.25, 0.5, 0.75, 0.9),
                знаков: int = 4) -> dict[str, float | None]:
    """Набор процентилей одним словарём: p10, p25, p50…"""
    return {f"p{int(round(д * 100))}": (round(v, знаков) if (v := percentile(значения, д))
                                          is not None else None)
            for д in доли}


def mean_std(значения: list[float]) -> tuple[float | None, float | None]:
    """Среднее и выборочное стандартное отклонение."""
    n = len(значения)
    if not n:
        return None, None
    среднее = sum(значения) / n
    if n < 2:
        return среднее, 0.0
    дисперсия = sum((x - среднее) ** 2 for x in значения) / (n - 1)
    return среднее, math.sqrt(дисперсия)


def norm_band(значения: list[float], знаков: int = 4) -> dict[str, Any]:
    """Норма от архива: медиана, квартили, границы выбросов по Тьюки.

    `enough` говорит, хватило ли выборки; без него норма по десяти записям
    выглядела бы такой же уверенной, как по тысяче.
    """
    ряд = [float(x) for x in значения if x is not None]
    if not ряд:
        return {"n": 0, "enough": False, "median": None, "q1": None, "q3": None,
                "low": None, "high": None}
    q1, медиана, q3 = (percentile(ряд, 0.25), percentile(ряд, 0.5), percentile(ряд, 0.75))
    размах = q3 - q1
    return {
        "n": len(ряд), "enough": len(ряд) >= МИН_ВЫБОРКА,
        "median": round(медиана, знаков), "q1": round(q1, знаков), "q3": round(q3, знаков),
        "low": round(q1 - ТЬЮКИ * размах, знаков), "high": round(q3 + ТЬЮКИ * размах, знаков),
    }


def against_norm(значение: float | None, норма: dict[str, Any]) -> str | None:
    """Где значение относительно нормы: inside, above, below, outlier_high,
    outlier_low; None — сравнивать не с чем."""
    if значение is None or not норма.get("enough"):
        return None
    x = float(значение)
    if x > норма["high"]:
        return "outlier_high"
    if x < норма["low"]:
        return "outlier_low"
    if x > норма["q3"]:
        return "above"
    if x < норма["q1"]:
        return "below"
    return "inside"


def ks_test(первая: list[float], вторая: list[float]) -> dict[str, Any]:
    """Двухвыборочный критерий Колмогорова — Смирнова.

    Возвращает статистику D — наибольшее расхождение эмпирических функций
    распределения — и асимптотическое p-значение. Асимптотика честна от
    нескольких десятков наблюдений в каждой выборке; меньше — `enough`
    ложно, и вердикт по нему не выносится.
    """
    a = sorted(float(x) for x in первая if x is not None)
    b = sorted(float(x) for x in вторая if x is not None)
    n, m = len(a), len(b)
    if not n or not m:
        return {"n": n, "m": m, "d": None, "p": None, "enough": False}
    i = j = 0
    d = 0.0
    # Пока обе выборки не кончились: когда одна исчерпана, расхождение
    # дальше только убывает, и хвост считать незачем.
    while i < n and j < m:
        if a[i] <= b[j]:
            i += 1
        else:
            j += 1
        d = max(d, abs(i / n - j / m))
    λ = d * math.sqrt(n * m / (n + m))
    # Ряд Колмогорова: p = 2·Σ(−1)^(k−1)·exp(−2k²λ²); сходится быстро.
    if λ < 1e-9:
        p = 1.0
    else:
        p = 2.0 * sum((-1) ** (k - 1) * math.exp(-2.0 * k * k * λ * λ) for k in range(1, 101))
        p = max(0.0, min(1.0, p))
    return {"n": n, "m": m, "d": round(d, 4), "p": round(p, 4),
            "enough": n >= МИН_ВЫБОРКА and m >= МИН_ВЫБОРКА}


def drift_verdict(база: list[float], сейчас: list[float], *,
                  relative_warning: float = 0.10, p_critical: float = 0.05,
                  higher_is_better: bool = True) -> dict[str, Any]:
    """Сдвиг распределения по порогам Deepgram.

    Критично — распределения различимы по KS с p < 0,05 и среднее ушло в
    плохую сторону на 10 % и больше; предупреждение — либо распределение
    различимо (p < 0,05) при меньшем сдвиге, либо сдвиг на 10 % на выборке,
    где KS ещё не уверен. Сдвиг в хорошую сторону вердикта не даёт: дрейф
    вверх — новость, а не тревога.
    """
    μ, σ = mean_std(база)
    μ2, _ = mean_std(сейчас)
    ks = ks_test(база, сейчас)
    out: dict[str, Any] = {
        "baseline": {"n": len(база), "mean": round(μ, 4) if μ is not None else None,
                     "std": round(σ, 4) if σ is not None else None,
                     **percentiles(база)},
        "current": {"n": len(сейчас), "mean": round(μ2, 4) if μ2 is not None else None,
                    **percentiles(сейчас)},
        "ks": ks, "verdict": "unknown", "shift": None, "shift_relative": None,
    }
    if μ is None or μ2 is None or not ks["enough"]:
        return out
    сдвиг = μ2 - μ
    out["shift"] = round(сдвиг, 4)
    out["shift_relative"] = round(сдвиг / μ, 4) if μ else None
    хуже = сдвиг < 0 if higher_is_better else сдвиг > 0
    вердикт = "ok"
    if хуже:
        различимы = ks["p"] is not None and ks["p"] < p_critical
        сильно = bool(μ) and abs(сдвиг) / abs(μ) >= relative_warning
        if различимы and сильно:
            вердикт = "critical"
        elif различимы or сильно:
            вердикт = "warning"
    out["verdict"] = вердикт
    return out


def control_limits(база: list[float], знаков: int = 4) -> dict[str, Any]:
    """Пределы контрольной карты по базовому окну: μ ± 2σ и μ ± 3σ."""
    ряд = [float(x) for x in база if x is not None]
    μ, σ = mean_std(ряд)
    if μ is None or σ is None:
        return {"n": 0, "enough": False, "mean": None, "std": None}
    return {
        "n": len(ряд), "enough": len(ряд) >= 8, "mean": round(μ, знаков),
        "std": round(σ, знаков),
        "warn_low": round(μ - 2 * σ, знаков), "warn_high": round(μ + 2 * σ, знаков),
        "crit_low": round(μ - 3 * σ, знаков), "crit_high": round(μ + 3 * σ, знаков),
    }


def spc_flags(точки: list[float | None], пределы: dict[str, Any]) -> list[dict[str, Any]]:
    """Отметки по точкам текущего окна: за 2σ, за 3σ, серия по одну сторону.

    Каждая отметка — номер точки и почему: `critical` за 3σ, `warning` за
    2σ, `run` — седьмая подряд по одну сторону от среднего. Точки без
    значения (день без записей) серию прерывают: молчание — не сдвиг.
    """
    if not пределы.get("enough"):
        return []
    μ = пределы["mean"]
    out = []
    сторона = 0
    подряд = 0
    for i, x in enumerate(точки):
        if x is None:
            сторона, подряд = 0, 0
            continue
        x = float(x)
        if x > пределы["crit_high"] or x < пределы["crit_low"]:
            out.append({"index": i, "value": x, "level": "critical",
                        "why": "за пределом 3σ"})
        elif x > пределы["warn_high"] or x < пределы["warn_low"]:
            out.append({"index": i, "value": x, "level": "warning",
                        "why": "за пределом 2σ"})
        знак = 1 if x > μ else -1 if x < μ else 0
        if знак and знак == сторона:
            подряд += 1
        else:
            сторона, подряд = знак, 1 if знак else 0
        if подряд == СЕРИЯ:
            out.append({"index": i, "value": x, "level": "warning",
                        "why": f"{СЕРИЯ} точек подряд по одну сторону от среднего"})
    return out
