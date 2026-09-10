"""Профиль звука на входе: SNR, пик, клиппинг, громкость LUFS, доля тишины.

Звук на входе объясняет ошибки лучше, чем что-либо ещё: по данным Deepgram,
WER около 3,5 % при SNR 20 дБ, 15 % при 10 дБ и 35 % при 5 дБ, а реальный
трафик контакт-центров живёт в 2–14 дБ. До сих пор сервер измерял только
пик и среднюю громкость через ffmpeg и предупреждал «очень тихая запись»;
ответить на вопрос «почему у этого источника WER вдвое выше» было нечем.

Пять величин, и каждая считается без эталона:

* **SNR** — отношение речи к шуму в децибелах. Оценка, а не измерение:
  речь и шум не размечены, и уровни берутся по кадрам — громкие кадры
  считаются речью, тихие — шумом. Если известны участки речи по VAD,
  шум берётся из кадров вне речи — это точнее. Оценка ограничена 60 дБ:
  на цифровой тишине формула уходит в бесконечность, а «бесконечно
  чистый звук» ничего не сообщает.
* **Пик, dBFS** — насколько запись близка к пределу шкалы; выше −0,5 дБ
  почти всегда означает срезанные вершины.
* **Доля клиппинга** — какая часть отсчётов упёрлась в шкалу; от 0,1 %
  искажения слышны, от 1 % страдает распознавание.
* **Громкость, LUFS** — интегральная громкость по ITU-R BS.1770-4 с
  K-взвешиванием и двойным гейтом (абсолютный −70 LUFS, относительный
  −10 LU); это та же величина, которую выравнивает `audio_target_lufs`.
  Коэффициенты фильтров пересчитываются под частоту записи — стандарт
  задаёт их для 48 кГц.
* **Доля тишины** — часть кадров тише −45 dBFS (тот же порог, что и у
  обрезки начальной тишины), либо, если есть разметка VAD, доля времени
  вне речи.

Измерение идёт по первым `max_seconds` секундам подготовленного файла:
профиль записи не меняется на второй час, а лишняя минута счёта на
часовом файле — меняет время ответа.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

#: Кадр для уровней — 20 мс, как в кодеках речи.
КАДР_С = 0.02

#: Порог тишины по кадру, dBFS: совпадает с обрезкой начальной тишины.
ПОРОГ_ТИШИНЫ_ДБ = -45.0

#: Порог клиппинга: доля полной шкалы 16 бит, начиная с которой отсчёт
#: считается упёршимся в предел (32767/32768 ≈ 0,99997; берём с запасом на
#: пересчёт из сжатых форматов).
ПОРОГ_КЛИППИНГА = 0.99

#: Верхняя граница оценки SNR: выше — цифровая тишина, а не чистота.
ПРЕДЕЛ_SNR = 60.0

#: Сколько секунд записи мерить.
МАКС_СЕКУНД = 1200.0

#: Пороги оценки — по Deepgram: ниже 10 дБ звук плохой, ниже 5 —
#: ожидать WER за тридцать процентов.
SNR_ПЛОХОЙ = 10.0
SNR_ОЧЕНЬ_ПЛОХОЙ = 5.0
КЛИППИНГ_ПЛОХОЙ = 0.01

#: Корзины SNR для срезов аналитики: граница и подпись.
КОРЗИНЫ_SNR: tuple[tuple[float | None, str], ...] = (
    (5.0, "ниже 5 дБ"), (10.0, "5–10 дБ"), (20.0, "10–20 дБ"), (None, "20 дБ и выше"))


def snr_band(snr: float | None) -> str | None:
    if snr is None:
        return None
    for граница, подпись in КОРЗИНЫ_SNR:
        if граница is None or snr < граница:
            return подпись
    return КОРЗИНЫ_SNR[-1][1]


def is_bad(профиль: dict[str, Any] | None) -> bool:
    """Плохой звук: SNR ниже 10 дБ или клиппинг от одного процента."""
    if not профиль:
        return False
    snr = профиль.get("snr_db")
    клип = профиль.get("clipping_share")
    return (snr is not None and float(snr) < SNR_ПЛОХОЙ) or \
        (клип is not None and float(клип) >= КЛИППИНГ_ПЛОХОЙ)


def _db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-9))


# --- фильтры K-взвешивания -------------------------------------------------

def _biquad(kind: str, fc: float, q: float, gain_db: float, fs: float
            ) -> tuple[list[float], list[float]]:
    """Коэффициенты биквада (RBJ Audio EQ Cookbook) под частоту `fs`.

    BS.1770 задаёт фильтры таблицей для 48 кГц; на 16 кГц те же значения
    были бы неверны. Пересчёт по формулам «поваренной книги» с параметрами
    pyloudnorm воспроизводит табличные коэффициенты на 48 кГц и даёт
    правильные на любой другой частоте.
    """
    a = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * math.pi * fc / fs
    alpha = math.sin(w0) / (2.0 * q)
    cos = math.cos(w0)
    if kind == "high_shelf":
        b0 = a * ((a + 1) + (a - 1) * cos + 2 * math.sqrt(a) * alpha)
        b1 = -2 * a * ((a - 1) + (a + 1) * cos)
        b2 = a * ((a + 1) + (a - 1) * cos - 2 * math.sqrt(a) * alpha)
        a0 = (a + 1) - (a - 1) * cos + 2 * math.sqrt(a) * alpha
        a1 = 2 * ((a - 1) - (a + 1) * cos)
        a2 = (a + 1) - (a - 1) * cos - 2 * math.sqrt(a) * alpha
    else:  # high_pass
        # Числитель — ровно [1, −2, 1], как в таблице стандарта: у
        # «поваренной» формы (1 + cos)/2 он на сотые доли децибела ниже.
        b0, b1, b2 = 1.0, -2.0, 1.0
        a0 = 1 + alpha
        a1 = -2 * cos
        a2 = 1 - alpha
        return [b0, b1, b2], [1.0, a1 / a0, a2 / a0]
    return [b0 / a0, b1 / a0, b2 / a0], [1.0, a1 / a0, a2 / a0]


#: Параметры двух каскадов K-взвешивания — как в pyloudnorm: полка +4 дБ
#: от 1500 Гц с Q = 1/√2 и фильтр высоких частот 38 Гц с Q = 0,5. На
#: 48 кГц они дают табличные коэффициенты BS.1770 с точностью до
#: седьмого знака (проверено в тестах).
_ШЕЛФ = ("high_shelf", 1500.0, 1.0 / math.sqrt(2.0), 4.0)
_ВЫСОКИЕ = ("high_pass", 38.0, 0.5, 0.0)


def _filter(samples: Any, b: list[float], a: list[float]) -> Any:
    """Рекурсивный фильтр второго порядка: scipy, если есть, иначе Python."""
    try:
        from scipy.signal import lfilter  # type: ignore

        return lfilter(b, a, samples)
    except ImportError:
        pass
    out = [0.0] * len(samples)
    x1 = x2 = y1 = y2 = 0.0
    b0, b1, b2 = b
    _, a1, a2 = a
    for i, x in enumerate(samples):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y
        x2, x1 = x1, x
        y2, y1 = y1, y
    return out


def loudness_lufs(samples: Any, rate: int) -> float | None:
    """Интегральная громкость по BS.1770-4 для одного канала."""
    n = len(samples)
    if n < int(0.4 * rate):
        return None
    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError:
        np = None  # type: ignore
    x = samples
    # Без scipy рекурсия идёт в Python: на длинной записи это минуты, и
    # окно режется до двух минут — громкость по ним та же с точностью до
    # десятых.
    try:
        import scipy.signal  # type: ignore  # noqa: F401
    except ImportError:
        x = x[:int(120 * rate)]
        n = len(x)
    for kind, fc, q, gain in (_ШЕЛФ, _ВЫСОКИЕ):
        b, a = _biquad(kind, fc, q, gain, float(rate))
        x = _filter(x, b, a)
    блок = int(0.4 * rate)
    шаг = int(0.1 * rate)
    мощности: list[float] = []
    if np is not None:
        arr = np.asarray(x, dtype=np.float64)
        for начало in range(0, n - блок + 1, шаг):
            кусок = arr[начало:начало + блок]
            мощности.append(float(np.mean(кусок * кусок)))
    else:
        for начало in range(0, n - блок + 1, шаг):
            кусок = x[начало:начало + блок]
            мощности.append(sum(v * v for v in кусок) / блок)
    if not мощности:
        return None
    громкости = [-0.691 + 10.0 * math.log10(max(p, 1e-12)) for p in мощности]
    выше_абс = [p for p, lk in zip(мощности, громкости, strict=True) if lk > -70.0]
    if not выше_абс:
        return None
    порог = -0.691 + 10.0 * math.log10(sum(выше_абс) / len(выше_абс)) - 10.0
    отобрано = [p for p, lk in zip(мощности, громкости, strict=True)
                if lk > max(-70.0, порог)]
    if not отобрано:
        return None
    return round(-0.691 + 10.0 * math.log10(sum(отобрано) / len(отобрано)), 2)


# --- профиль ---------------------------------------------------------------

def profile(samples: Any, rate: int, *,
            speech_spans: list[tuple[float, float]] | None = None) -> dict[str, Any]:
    """Профиль по отсчётам в [-1, 1]."""
    try:
        import numpy as np  # type: ignore
    except ModuleNotFoundError:
        np = None  # type: ignore
    n = len(samples)
    if n == 0 or rate <= 0:
        return {}
    кадр = max(1, int(КАДР_С * rate))
    if np is not None:
        arr = np.asarray(samples, dtype=np.float32)
        пик = float(np.max(np.abs(arr))) if n else 0.0
        клип = float(np.mean(np.abs(arr) >= ПОРОГ_КЛИППИНГА))
        полных = (n // кадр) * кадр
        кадры = arr[:полных].reshape(-1, кадр) if полных else arr.reshape(1, -1)
        мощность = np.mean(кадры.astype(np.float64) ** 2, axis=1)
        уровни = [10.0 * math.log10(max(float(p), 1e-12)) for p in мощность]
    else:
        пик = max(abs(float(v)) for v in samples)
        клип = sum(1 for v in samples if abs(float(v)) >= ПОРОГ_КЛИППИНГА) / n
        уровни = []
        for начало in range(0, n - кадр + 1, кадр):
            кусок = samples[начало:начало + кадр]
            уровни.append(10.0 * math.log10(max(sum(float(v) ** 2 for v in кусок) / кадр, 1e-12)))
    if not уровни:
        return {}

    # Речь и шум. С разметкой VAD: шум — кадры вне речи; без неё — по
    # перцентилям: верхние 30 % кадров считаются речью, нижние 20 % — шумом.
    речь: list[float] = []
    шум: list[float] = []
    if speech_spans:
        for i, уровень in enumerate(уровни):
            t = (i + 0.5) * КАДР_С
            (речь if any(a <= t < b for a, b in speech_spans) else шум).append(уровень)
    if not речь or not шум:
        ряд = sorted(уровни)
        k = len(ряд)
        шум = ряд[:max(1, int(k * 0.2))]
        речь = ряд[max(0, int(k * 0.7)):] or ряд[-1:]
    # Средняя мощность, а не средний децибел: один громкий кадр в тишине
    # не должен «поднимать» шум логарифмом.
    p_речь = sum(10 ** (у / 10) for у in речь) / len(речь)
    p_шум = sum(10 ** (у / 10) for у in шум) / len(шум)
    snr = 10.0 * math.log10(max(p_речь, 1e-12) / max(p_шум, 1e-12))
    snr = max(-10.0, min(ПРЕДЕЛ_SNR, snr))

    if speech_spans is not None and speech_spans:
        длит = n / rate
        речи = sum(max(0.0, min(b, длит) - max(a, 0.0)) for a, b in speech_spans)
        тишина = max(0.0, 1.0 - речи / длит) if длит else None
    else:
        тишина = sum(1 for у in уровни if у < ПОРОГ_ТИШИНЫ_ДБ) / len(уровни)

    return {
        "snr_db": round(snr, 1),
        "peak_dbfs": round(_db(пик), 1),
        "clipping_share": round(клип, 5),
        "loudness_lufs": loudness_lufs(samples, rate),
        "silence_share": round(float(тишина), 4) if тишина is not None else None,
        "measured_s": round(n / rate, 1),
        "noise_dbfs": round(10.0 * math.log10(max(p_шум, 1e-12)), 1),
        "speech_dbfs": round(10.0 * math.log10(max(p_речь, 1e-12)), 1),
        "method": "vad" if speech_spans else "percentile",
    }


def profile_file(path: Path, *, speech_spans: list[tuple[float, float]] | None = None,
                 max_seconds: float = МАКС_СЕКУНД) -> dict[str, Any]:
    """Профиль подготовленного WAV — по первым `max_seconds` секундам."""
    from .audio import load_samples

    samples, rate = load_samples(path)
    предел = int(max_seconds * rate)
    if len(samples) > предел:
        samples = samples[:предел]
        if speech_spans:
            speech_spans = [(a, min(b, max_seconds)) for a, b in speech_spans if a < max_seconds]
    return profile(samples, rate, speech_spans=speech_spans)


def merge(профили: list[dict[str, Any]]) -> dict[str, Any]:
    """Профиль записи из профилей каналов: худший SNR, худший клиппинг,
    самый громкий канал, средняя доля тишины."""
    профили = [п for п in профили if п]
    if not профили:
        return {}
    if len(профили) == 1:
        return dict(профили[0])
    значения = lambda к: [п[к] for п in профили if п.get(к) is not None]  # noqa: E731
    out = dict(профили[0])
    out["snr_db"] = min(значения("snr_db"), default=None)
    out["peak_dbfs"] = max(значения("peak_dbfs"), default=None)
    out["clipping_share"] = max(значения("clipping_share"), default=None)
    out["loudness_lufs"] = max(значения("loudness_lufs"), default=None)
    тишина = значения("silence_share")
    out["silence_share"] = round(sum(тишина) / len(тишина), 4) if тишина else None
    return out


def for_job(профиль: dict[str, Any]) -> dict[str, Any]:
    """Колонки задания из профиля."""
    if not профиль:
        return {}
    return {к: профиль.get(к) for к in
            ("snr_db", "peak_dbfs", "clipping_share", "loudness_lufs", "silence_share")}
