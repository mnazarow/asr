"""Постобработка распознанного текста.

Этапы выполняются в фиксированном порядке:
1. фильтр типовых галлюцинаций (до всего остального — мусор не должен попасть в статистику);
2. удаление слов-паразитов;
3. восстановление пунктуации и регистра;
4. нормализация чисел и дат;
5. словарь замен (последний, чтобы перекрыть решения предыдущих этапов);
6. фильтр ненормативной лексики;
7. склейка коротких сегментов и разбиение на абзацы.
"""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable
from typing import Any

from .. import settings_access as S
from ..logging_setup import get_logger

log = get_logger("postprocess")

# --------------------------------------------------------------------------
# Словарь типовых галлюцинаций Whisper
# --------------------------------------------------------------------------

HALLUCINATION_PHRASES_RU = [
    "субтитры сделал dimatorzok", "субтитры делал dimatorzok", "субтитры сделал димarzok",
    "субтитры создавал dimatorzok", "продолжение следует...", "продолжение следует",
    "спасибо за просмотр", "спасибо за внимание!", "подписывайтесь на канал",
    "подписывайтесь на наш канал", "ставьте лайки и подписывайтесь",
    "редактор субтитров", "корректор", "субтитры и перевод",
    "не забудьте поставить лайк", "всем пока", "до новых встреч",
    "перевод и субтитры", "субтитры", "ставьте лайк",
]
HALLUCINATION_PHRASES_EN = [
    "thank you.", "thank you", "thanks for watching", "thank you for watching",
    "thanks for watching!", "please subscribe", "subscribe to my channel",
    "like and subscribe", "see you next time", "bye bye", "you", "♪", "[music]",
    "[applause]", "amara.org", "subtitles by", "transcription by",
]
HALLUCINATION_PHRASES = HALLUCINATION_PHRASES_RU + HALLUCINATION_PHRASES_EN

FILLER_WORDS_RU = [
    "э", "э-э", "эм", "ээ", "ммм", "мм", "ну", "вот", "как бы", "типа",
    "короче", "в общем", "это самое", "так сказать", "значит", "собственно",
    "как говорится", "понимаешь", "знаешь",
]

_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_MULTISPACE_RE = re.compile(r"[ \t]{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:…])")
_REPEAT_RE = re.compile(r"\b(\w{2,})(?:\s+\1\b){2,}", re.IGNORECASE | re.UNICODE)


def normalize_spaces(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ").replace("​", "")
    text = _MULTISPACE_RE.sub(" ", text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return text.strip()


# --------------------------------------------------------------------------
# 1. Фильтр галлюцинаций
# --------------------------------------------------------------------------

def is_hallucination(text: str, extra: Iterable[str] = ()) -> bool:
    """Проверяет, состоит ли сегмент целиком из типовой выдуманной фразы."""
    cleaned = normalize_spaces(text).lower().strip(" .!?…-–—\"'«»")
    if not cleaned:
        return True
    phrases = set(HALLUCINATION_PHRASES) | {p.lower().strip() for p in extra if p}
    if cleaned in phrases:
        return True
    for phrase in phrases:
        if len(phrase) >= 12 and cleaned.startswith(phrase) and len(cleaned) < len(phrase) * 1.4:
            return True
    if _REPEAT_RE.fullmatch(cleaned):
        return True
    words = cleaned.split()
    if len(words) >= 6 and len(set(words)) <= 2:
        return True
    return False


def filter_hallucinations(segments: list[dict[str, Any]],
                          extra: Iterable[str] = ()) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    removed = 0
    for seg in segments:
        if is_hallucination(seg.get("text", ""), extra):
            removed += 1
            continue
        kept.append(seg)
    if removed:
        log.info("Фильтр галлюцинаций убрал сегментов: %d", removed)
    return kept, removed


def collapse_repeats(text: str) -> str:
    """Схлопывает многократные повторы одного слова до двух вхождений."""
    return _REPEAT_RE.sub(lambda m: f"{m.group(1)} {m.group(1)}", text)


# --------------------------------------------------------------------------
# 2. Слова-паразиты
# --------------------------------------------------------------------------

def remove_fillers(text: str, fillers: Iterable[str] = ()) -> str:
    words = list(fillers) or FILLER_WORDS_RU
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
        + r")(?![\w-])[,]?\s*", re.IGNORECASE | re.UNICODE)
    result = pattern.sub("", text)
    result = normalize_spaces(result)
    if result and result[0].islower():
        result = result[0].upper() + result[1:]
    return result


# --------------------------------------------------------------------------
# 3. Пунктуация
# --------------------------------------------------------------------------

_PUNCT_CACHE: dict[str, Any] = {}


def _report_missing(what: str, choice: str, explicit: bool, exc: Exception) -> None:
    """Сообщает о недоступной надстройке ровно настолько громко, насколько надо.

    При `auto` перебор вариантов — штатный ход, и место ему в debug. Но если
    надстройку выбрали явно, молчание обманывает: пользователь просил
    расстановку знаков препинания, получил текст без них и нигде не увидел
    почему. Такой случай — предупреждение.
    """
    if explicit:
        log.warning("%s «%s» недоступен, обработка пропущена: %s. "
                    "Установите пакет или выберите другой вариант.", what, choice, exc)
    else:
        log.debug("%s «%s» недоступен: %s", what, choice, exc)


#: Сколько разных нормализаторов держать в памяти. Каждый — полноценная
#: модель, поэтому предел небольшой и вытеснение простое: при переполнении
#: кеш очищается целиком, следующая запись загрузит нужное заново.
_ITN_CACHE_LIMIT = 4


def unload_text_models() -> int:
    """Выгружает модели постобработки. Возвращает, сколько их было.

    Вызывается из реестра движков вместе с выгрузкой моделей распознавания:
    иначе освобождение памяти «выгрузить всё» освобождало не всё.
    """
    count = len(_PUNCT_CACHE) + len(_ITN_CACHE)
    _PUNCT_CACHE.clear()
    _ITN_CACHE.clear()
    if count:
        try:
            import gc

            gc.collect()
        except Exception:                                   # noqa: BLE001
            pass
    return count


def _load_punctuator(model: str, language: str) -> Callable[[str], str] | None:
    # В ключе только выбор модели. Язык добавлять нельзя: загружаемая модель
    # от него не зависит, а при `language: auto` и разноязычном потоке каждый
    # новый определившийся язык добавлял ещё одну полную копию того же
    # трансформера — до полутора гигабайт за язык, до девяноста девяти копий.
    # Ни collect_idle, ни счётчик загруженных моделей про этот кеш не знали.
    key = model
    if key in _PUNCT_CACHE:
        return _PUNCT_CACHE[key]

    def _register(fn: Callable[[str], str] | None):
        _PUNCT_CACHE[key] = fn
        return fn

    choice = model
    explicit = model != "auto"
    if model == "auto":
        choice = "rupunct" if language == "ru" else "multilingual"

    if choice == "rupunct":
        try:
            from transformers import pipeline  # type: ignore

            pipe = pipeline("ner", model="RUPunct/RUPunct_big",
                            aggregation_strategy="first")

            def apply(text: str) -> str:
                return _apply_rupunct(pipe, text)

            return _register(apply)
        except Exception as exc:
            _report_missing("Модуль расстановки знаков препинания", "rupunct",
                            explicit, exc)

    if choice == "sbert_punc_case_ru":
        try:
            from sbert_punc_case_ru import SbertPuncCase  # type: ignore

            model_obj = SbertPuncCase()
            return _register(lambda text: model_obj.punctuate(text))
        except Exception as exc:
            _report_missing("Модуль расстановки знаков препинания",
                            "sbert_punc_case_ru", explicit, exc)

    if choice == "multilingual":
        try:
            from deepmultilingualpunctuation import PunctuationModel  # type: ignore

            model_obj = PunctuationModel()
            return _register(lambda text: model_obj.restore_punctuation(text))
        except Exception as exc:
            _report_missing("Модуль расстановки знаков препинания",
                            "multilingual", explicit, exc)

    return _register(None)


def _apply_rupunct(pipe: Any, text: str) -> str:
    labels = pipe(text.lower())
    out: list[str] = []
    for item in labels:
        word = item.get("word", "").strip()
        tag = str(item.get("entity_group", "O"))
        if not word:
            continue
        if "UPPER_TOTAL" in tag:
            word = word.upper()
        elif "UPPER" in tag:
            word = word.capitalize()
        for mark, symbol in (("PERIOD", "."), ("COMMA", ","), ("QUESTION", "?"),
                             ("EXCLAM", "!"), ("COLON", ":"), ("SEMICOLON", ";"),
                             ("DASH", " —"), ("ELLIPSIS", "…")):
            if mark in tag:
                word += symbol
                break
        out.append(word)
    result = normalize_spaces(" ".join(out))
    return result[:1].upper() + result[1:] if result else result


def restore_punctuation(text: str, model: str = "auto", language: str = "ru") -> str:
    if not text.strip():
        return text
    fn = _load_punctuator(model, language)
    if fn is None:
        return _naive_punctuation(text)
    try:
        return normalize_spaces(fn(text))
    except Exception as exc:
        log.warning("Модель пунктуации дала сбой (%s), применено простое правило", exc)
        return _naive_punctuation(text)


def _naive_punctuation(text: str) -> str:
    """Запасной вариант: заглавная буква в начале и точка в конце.

    Не пытается расставлять знаки внутри — лучше отсутствие пунктуации,
    чем неверная.
    """
    text = normalize_spaces(text)
    if not text:
        return text
    if text[0].islower():
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?…":
        text += "."
    return text


# --------------------------------------------------------------------------
# 4. Нормализация чисел (ITN)
# --------------------------------------------------------------------------

_ITN_CACHE: dict[str, Any] = {}

_UNITS_RU = {
    "ноль": 0, "один": 1, "одна": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40,
    "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80,
    "девяносто": 90, "сто": 100, "двести": 200, "триста": 300, "четыреста": 400,
    "пятьсот": 500, "шестьсот": 600, "семьсот": 700, "восемьсот": 800,
    "девятьсот": 900,
}
_SCALES_RU = {"тысяча": 1000, "тысячи": 1000, "тысяч": 1000, "тысячу": 1000,
              "миллион": 10 ** 6, "миллиона": 10 ** 6, "миллионов": 10 ** 6,
              "миллиард": 10 ** 9, "миллиарда": 10 ** 9, "миллиардов": 10 ** 9}


def _load_itn(backend: str, language: str):
    # Язык здесь значим: InverseNormalizer(lang=...) создаётся под конкретный
    # язык. Но число языков ограничиваем — иначе кеш растёт без предела.
    key = f"{backend}:{language}"
    if key in _ITN_CACHE:
        return _ITN_CACHE[key]
    if len(_ITN_CACHE) >= _ITN_CACHE_LIMIT:
        _ITN_CACHE.clear()
    choice = backend
    explicit = backend != "auto"
    if backend == "auto":
        choice = "nemo"
    if choice == "nemo":
        try:
            from nemo_text_processing.inverse_text_normalization.inverse_normalize import (  # type: ignore
                InverseNormalizer,
            )

            normalizer = InverseNormalizer(lang=language)
            _ITN_CACHE[key] = normalizer.inverse_normalize
            return _ITN_CACHE[key]
        except Exception as exc:
            _report_missing("Нормализатор чисел", "nemo", explicit, exc)
    if choice in ("rus2num", "nemo") and language == "ru":
        try:
            from rus2num import Rus2Num  # type: ignore

            conv = Rus2Num()
            _ITN_CACHE[key] = conv.convert
            return _ITN_CACHE[key]
        except Exception as exc:
            _report_missing("Нормализатор чисел", "rus2num", explicit, exc)
    _ITN_CACHE[key] = None
    return None


def _builtin_itn_ru(text: str) -> str:
    """Встроенная нормализация русских числительных без внешних библиотек.

    Покрывает целые числа до миллиардов и проценты — самые частые случаи
    в деловых записях.
    """
    tokens = text.split()
    out: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if not buffer:
            return
        value = _parse_number_ru(buffer)
        if value is None:
            out.extend(buffer)
        else:
            out.append(str(value))
        buffer.clear()

    for token in tokens:
        bare = token.lower().strip(".,!?;:…()«»\"'")
        if bare in _UNITS_RU or bare in _SCALES_RU:
            buffer.append(token)
            continue
        flush()
        out.append(token)
    flush()
    result = " ".join(out)
    result = re.sub(r"(\d+)\s+процент(?:ов|а|)\b", r"\1 %", result)
    result = re.sub(r"(\d+)\s+рубл(?:ей|я|ь)\b", r"\1 ₽", result)
    return normalize_spaces(result)


def _parse_number_ru(words: list[str]) -> int | None:
    total = 0
    current = 0
    seen = False
    for raw in words:
        word = raw.lower().strip(".,!?;:…()«»\"'")
        if word in _UNITS_RU:
            current += _UNITS_RU[word]
            seen = True
        elif word in _SCALES_RU:
            scale = _SCALES_RU[word]
            current = (current or 1) * scale
            total += current
            current = 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + current


def apply_itn(text: str, backend: str = "auto", language: str = "ru") -> str:
    if not text.strip():
        return text
    fn = _load_itn(backend, language)
    if fn is not None:
        try:
            return normalize_spaces(fn(text))
        except Exception as exc:
            log.warning("Нормализация чисел дала сбой (%s), применено встроенное правило", exc)
    if language == "ru":
        return _builtin_itn_ru(text)
    return text


# --------------------------------------------------------------------------
# 5. Словарь замен
# --------------------------------------------------------------------------

def apply_glossary(text: str, glossary: dict[str, str]) -> tuple[str, int]:
    if not glossary:
        return text, 0
    count = 0
    for source, target in glossary.items():
        if not source:
            continue
        if source.startswith("re:"):
            try:
                pattern = re.compile(source[3:], re.IGNORECASE | re.UNICODE)
            except re.error as exc:
                log.warning("Некорректное регулярное выражение «%s»: %s", source, exc)
                continue
            text, n = pattern.subn(target, text)
            count += n
            continue
        pattern = re.compile(rf"(?<![\w-]){re.escape(source)}(?![\w-])",
                             re.IGNORECASE | re.UNICODE)

        def _make_replacer(replacement: str):
            # Значение связываем явно: иначе замыкание захватит переменную цикла.
            def _replace(match: re.Match[str]) -> str:
                original = match.group(0)
                if original[:1].isupper() and replacement[:1].islower():
                    return replacement[:1].upper() + replacement[1:]
                return replacement
            return _replace

        text, n = pattern.subn(_make_replacer(target), text)
        count += n
    return text, count


# --------------------------------------------------------------------------
# 6. Ненормативная лексика
# --------------------------------------------------------------------------

_PROFANITY_ROOTS = ["хуй", "хуе", "хуё", "пизд", "ебат", "ебан", "ебал", "бляд",
                    "муда", "залуп", "пидор", "пидар", "сука", "гандон", "долбоеб",
                    "долбоёб", "ублюд", "fuck", "shit", "bitch", "cunt"]


def filter_profanity(text: str, mode: str = "off") -> tuple[str, int]:
    if mode == "off" or not text:
        return text, 0
    pattern = re.compile(
        r"\b\w*(?:" + "|".join(_PROFANITY_ROOTS) + r")\w*\b", re.IGNORECASE | re.UNICODE)
    hits = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal hits
        hits += 1
        word = match.group(0)
        if mode == "remove":
            return ""
        if mode == "tag":
            return f"[{word}]"
        return word[0] + "*" * (len(word) - 1)

    result = pattern.sub(_replace, text)
    return normalize_spaces(result), hits


# --------------------------------------------------------------------------
# 7. Структура текста
# --------------------------------------------------------------------------

def merge_segments(segments: list[dict[str, Any]], min_duration: float = 1.5,
                   max_gap: float = 1.0, max_duration: float = 30.0) -> list[dict[str, Any]]:
    """Склеивает короткие соседние сегменты одного говорящего."""
    if not segments:
        return []
    merged: list[dict[str, Any]] = [dict(segments[0])]
    for seg in segments[1:]:
        last = merged[-1]
        same_speaker = seg.get("speaker") == last.get("speaker")
        gap = float(seg.get("start", 0)) - float(last.get("end", 0))
        short = (float(last.get("end", 0)) - float(last.get("start", 0))) < min_duration
        combined = float(seg.get("end", 0)) - float(last.get("start", 0))
        if same_speaker and short and gap <= max_gap and combined <= max_duration:
            last["end"] = seg.get("end")
            last["text"] = normalize_spaces(f"{last.get('text', '')} {seg.get('text', '')}")
            if last.get("words") and seg.get("words"):
                last["words"] = list(last["words"]) + list(seg["words"])
            confs = [c for c in (last.get("confidence"), seg.get("confidence")) if c is not None]
            if confs:
                last["confidence"] = sum(confs) / len(confs)
        else:
            merged.append(dict(seg))
    return merged


def build_paragraphs(segments: list[dict[str, Any]], mode: str = "speaker",
                     pause_s: float = 2.0, sentences: int = 4) -> list[str]:
    if not segments:
        return []
    if mode == "none":
        return [normalize_spaces(" ".join(s.get("text", "") for s in segments))]

    paragraphs: list[str] = []
    current: list[str] = []
    prev_speaker = segments[0].get("speaker")
    prev_end = float(segments[0].get("start", 0))
    sentence_count = 0

    for seg in segments:
        text = seg.get("text", "").strip()
        if not text:
            continue
        speaker = seg.get("speaker")
        gap = float(seg.get("start", 0)) - prev_end
        boundary = False
        if mode == "speaker" and speaker != prev_speaker or mode == "pause" and gap >= pause_s or mode == "sentences" and sentence_count >= sentences:
            boundary = True
        if boundary and current:
            paragraphs.append(normalize_spaces(" ".join(current)))
            current = []
            sentence_count = 0
        if mode == "speaker" and speaker and (not current):
            current.append(f"{speaker}: {text}")
        else:
            current.append(text)
        sentence_count += text.count(".") + text.count("!") + text.count("?") or 1
        prev_speaker = speaker
        prev_end = float(seg.get("end", prev_end))
    if current:
        paragraphs.append(normalize_spaces(" ".join(current)))
    return paragraphs


# --------------------------------------------------------------------------
# Основная точка входа
# --------------------------------------------------------------------------

def process(segments: list[dict[str, Any]], settings: dict[str, Any],
            *, model_has_punctuation: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Применяет весь конвейер постобработки. Возвращает (сегменты, статистику)."""
    stats: dict[str, Any] = {
        "hallucinations_removed": 0,
        "glossary_replacements": 0,
        "profanity_hits": 0,
        "segments_before": len(segments),
    }
    language = str(settings.get("language") or "ru")
    if language == "auto":
        language = str(segments[0].get("language") or "ru") if segments else "ru"

    if settings.get("hallucination_filter", True):
        segments, removed = filter_hallucinations(
            segments, settings.get("hallucination_phrases") or ())
        stats["hallucinations_removed"] = removed

    for seg in segments:
        seg["text"] = collapse_repeats(normalize_spaces(seg.get("text", "")))

    if settings.get("remove_filler_words"):
        for seg in segments:
            seg["text"] = remove_fillers(seg["text"], settings.get("filler_words") or ())

    if settings.get("punctuation_enabled", True) and not model_has_punctuation:
        model_name = str(settings.get("punctuation_model") or "auto")
        for seg in segments:
            seg["text"] = restore_punctuation(seg["text"], model_name, language)
        stats["punctuation_applied"] = True

    if settings.get("itn_enabled", True):
        backend = str(settings.get("itn_backend") or "auto")
        for seg in segments:
            seg["text"] = apply_itn(seg["text"], backend, language)
        stats["itn_applied"] = True

    glossary = settings.get("glossary") or {}
    if isinstance(glossary, dict) and glossary:
        total = 0
        for seg in segments:
            seg["text"], n = apply_glossary(seg["text"], glossary)
            total += n
        stats["glossary_replacements"] = total

    mode = str(settings.get("profanity_filter") or "off")
    if mode != "off":
        total = 0
        for seg in segments:
            seg["text"], n = filter_profanity(seg["text"], mode)
            total += n
        stats["profanity_hits"] = total

    segments = [s for s in segments if s.get("text", "").strip()]

    if settings.get("merge_short_segments", True):
        segments = merge_segments(
            segments, S.num(settings, "min_segment_duration_s", 1.5))

    names = str(settings.get("speaker_names") or "").strip()
    if names:
        mapping = _speaker_mapping(segments, [n.strip() for n in names.split(",") if n.strip()])
        for seg in segments:
            if seg.get("speaker") in mapping:
                seg["speaker"] = mapping[seg["speaker"]]

    stats["segments_after"] = len(segments)
    return segments, stats


def _speaker_mapping(segments: list[dict[str, Any]], names: list[str]) -> dict[str, str]:
    order: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker")
        if speaker and speaker not in order:
            order.append(speaker)
    return {speaker: names[i] for i, speaker in enumerate(order) if i < len(names)}
