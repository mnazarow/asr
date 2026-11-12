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

import functools
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable
from pathlib import Path
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

#: Встроенный список — только междометия. Прежде в нём стояли и слова с
#: собственным значением: «Что это значит?» становилось «Что это?», «Ты
#: знаешь ответ» — «Ты ответ», «В общем зале тихо» — «Зале тихо». Фильтр не
#: отличает паразита от того же слова в деле, а испорченный смысл хуже
#: лишнего «типа». Свой список (`filler_words`) по-прежнему может быть любым.
FILLER_WORDS_RU = [
    "э", "э-э", "э-э-э", "эм", "ээ", "эээ", "ммм", "мм", "м-м", "хм", "ну",
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

def строки(значение: Any, *, запятые: bool = True) -> list[str]:
    """Список из параметра-текста: по строке на элемент, а с `запятые` — и через запятую.

    Строку раньше отдавали как есть, а перебор строки идёт по буквам: свой
    список «слов-паразитов» превращался в набор одиночных букв.
    """
    if not значение:
        return []
    if isinstance(значение, (list, tuple, set)):
        части = [str(э) for э in значение]
    else:
        части = str(значение).splitlines()
        if запятые:
            части = [к for ч in части for к in ч.split(",")]
    return [ч.strip() for ч in части if ч.strip()]


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

#: Когда модель постобработки в последний раз не загрузилась — по ключу кеша.
#: Отказ раньше запоминался до перезапуска: нет сети в момент первого
#: задания, и все дальнейшие получали «заглавная плюс точка» до следующего
#: перезапуска сервера, хотя сеть вернулась через минуту. Теперь отказ
#: помнится `ПАМЯТЬ_ОТКАЗА_С`, потом загрузка пробуется снова.
_ОТКАЗЫ: dict[str, float] = {}
ПАМЯТЬ_ОТКАЗА_С = 600.0


def _готово_в_кеше(кеш: dict[str, Any], ключ: str, вид: str) -> bool:
    """Есть ли в кеше ответ, которому можно верить: модель — всегда, отказ — пока свеж."""
    if ключ not in кеш:
        return False
    if кеш[ключ] is not None:
        return True
    когда = _ОТКАЗЫ.get(f"{вид}:{ключ}", 0.0)
    if time.monotonic() - когда < ПАМЯТЬ_ОТКАЗА_С:
        return True
    кеш.pop(ключ, None)
    return False


def _запомнить_отказ(вид: str, ключ: str) -> None:
    _ОТКАЗЫ[f"{вид}:{ключ}"] = time.monotonic()


#: Почему последняя загрузка модели пунктуации не удалась — для
#: предупреждения в задании (см. `process`).
_ПРИЧИНЫ_ОТКАЗА: dict[str, str] = {}


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


#: Загрузка моделей постобработки — под замком. Два задания, дошедшие до
#: пунктуации разом, грузили RUPunct дважды: полторы копии трансформера в
#: памяти, одна из которых тут же выбрасывалась.
_ЗАГРУЗКА_МОДЕЛЕЙ = threading.RLock()


def _load_punctuator(model: str, language: str) -> Callable[[str], str] | None:
    if _готово_в_кеше(_PUNCT_CACHE, model, "punct"):
        return _PUNCT_CACHE[model]
    with _ЗАГРУЗКА_МОДЕЛЕЙ:
        return _load_punctuator_unlocked(model, language)


def _load_punctuator_unlocked(model: str, language: str) -> Callable[[str], str] | None:
    # В ключе только выбор модели. Язык добавлять нельзя: загружаемая модель
    # от него не зависит, а при `language: auto` и разноязычном потоке каждый
    # новый определившийся язык добавлял ещё одну полную копию того же
    # трансформера — до полутора гигабайт за язык, до девяноста девяти копий.
    # Ни collect_idle, ни счётчик загруженных моделей про этот кеш не знали.
    key = model
    if _готово_в_кеше(_PUNCT_CACHE, key, "punct"):
        return _PUNCT_CACHE[key]
    причины: list[str] = []

    def _register(fn: Callable[[str], str] | None):
        _PUNCT_CACHE[key] = fn
        if fn is None:
            _запомнить_отказ("punct", key)
            _ПРИЧИНЫ_ОТКАЗА[key] = "; ".join(причины) or "модель не выбрана"
        else:
            _ПРИЧИНЫ_ОТКАЗА.pop(key, None)
        return fn

    choice = model
    explicit = model != "auto"
    if model == "auto":
        choice = "rupunct" if language == "ru" else "multilingual"
    if choice == "multilingual" and language == "ru":
        # У deepmultilingualpunctuation русского нет НИ В ОДНОЙ модели:
        # `fullstop-punctuation-multilang-large` — это английский, немецкий,
        # французский и итальянский, «sonar-base» добавляет нидерландский.
        # Выбранный вручную для русской записи, он раньше молча возвращал
        # текст без знаков — и это выглядело как «модель не справилась», а
        # не как «модель этого языка не знает».
        log.warning("Модель пунктуации «multilingual» русского языка не знает "
                    "— берётся RUPunct. Список языков: en, de, fr, it, nl.")
        choice = "rupunct"

    if choice == "rupunct_onnx":
        try:
            фн = _рупункт_onnx()
            if фн is not None:
                return _register(фн)
        except Exception as exc:                            # noqa: BLE001
            причины.append(f"rupunct_onnx: {exc}")
            _report_missing("Модуль расстановки знаков препинания",
                            "rupunct_onnx", explicit, exc)

    if choice == "rupunct":
        try:
            from transformers import pipeline  # type: ignore

            pipe = pipeline("ner", model="RUPunct/RUPunct_big",
                            aggregation_strategy="first")
            труба = _ТрубаОкнами(pipe)

            def apply(text: str) -> str:
                return _apply_rupunct(труба, text)

            return _register(apply)
        except Exception as exc:
            причины.append(f"rupunct: {exc}")
            _report_missing("Модуль расстановки знаков препинания", "rupunct",
                            explicit, exc)

    if choice == "sbert_punc_case_ru":
        try:
            from sbert_punc_case_ru import SbertPuncCase  # type: ignore

            model_obj = SbertPuncCase()
            return _register(lambda text: model_obj.punctuate(text))
        except Exception as exc:
            причины.append(f"sbert_punc_case_ru: {exc}")
            _report_missing("Модуль расстановки знаков препинания",
                            "sbert_punc_case_ru", explicit, exc)

    if choice == "multilingual":
        try:
            from deepmultilingualpunctuation import PunctuationModel  # type: ignore

            model_obj = PunctuationModel()
            return _register(lambda text: model_obj.restore_punctuation(text))
        except Exception as exc:
            причины.append(f"multilingual: {exc}")
            _report_missing("Модуль расстановки знаков препинания",
                            "multilingual", explicit, exc)

    return _register(None)


#: Откуда берётся лёгкая модель пунктуации. Меняется переменной окружения:
#: на сервере без доступа в интернет веса кладут рядом и указывают путь.
RUPUNCT_ONNX = "ekhodzitsky/rupunct-small-onnx"

#: Имена, под которыми лежит сама сеть в выгрузках ONNX. Порядок — от
#: квантованной к обычной: int8 втрое меньше и быстрее на процессоре, а
#: разметку знаков препинания квантование не портит.
ФАЙЛЫ_ONNX = ("onnx/model_quantized.onnx", "onnx/model.onnx",
              "model_quantized.onnx", "model.onnx")

#: Сколько слов уходит в сеть за раз. Модель принимает 512 позиций токенизатора,
#: а на русском одно слово — это в среднем два-три токена; сто восемьдесят слов
#: не переполняют вход даже на словах, которые токенизатор дробит мелко.
ОКНО_СЛОВ = 180


def _файл_onnx(источник: str) -> str:
    """Путь к файлу сети: рядом на диске или из хранилища моделей.

    Локальный путь проверяется первым и без сети: на закрытом сервере веса
    кладут рядом и указывают папку (или сразу файл) переменной окружения.
    """
    место = Path(источник)
    if место.is_file():
        return str(место)
    if место.is_dir():
        for имя in ФАЙЛЫ_ONNX:
            if (место / имя).is_file():
                return str(место / имя)
        найденные = sorted(место.rglob("*.onnx"))
        if найденные:
            return str(найденные[0])
        raise FileNotFoundError(
            f"В папке «{место}» нет ни одного файла .onnx. "
            f"Ожидались: {', '.join(ФАЙЛЫ_ONNX)}.")

    from huggingface_hub import hf_hub_download  # type: ignore

    последняя: Exception | None = None
    for имя in ФАЙЛЫ_ONNX:
        try:
            return hf_hub_download(repo_id=источник, filename=имя)
        except Exception as exc:                            # noqa: BLE001
            последняя = exc
    raise FileNotFoundError(
        f"В «{источник}» не нашлось ни одного из файлов "
        f"{', '.join(ФАЙЛЫ_ONNX)}.") from последняя


class _ТрубаОкнами:
    """Труба `ner` из transformers — окнами по `ОКНО_СЛОВ` слов и под замком.

    Окно — потому что труба режет вход по 512 токенам и возвращает слова
    только из поместившегося, а текст реплики собирается из её ответа: хвост
    длинной реплики (NeMo без пунктуации отдаёт запись одной репликой,
    склейка по паузам собирает несколько) пропадал из расшифровки. У
    варианта ONNX окно было с самого начала, здесь — не было.

    Замок — потому что одна труба на процесс, а заданий одновременно
    несколько: быстрый токенизатор при одновременном вызове из двух потоков
    падает с «Already borrowed», и реплика молча уходила в запасное правило.
    """

    def __init__(self, труба: Any) -> None:
        self.труба = труба
        self.замок = threading.Lock()

    def __call__(self, text: str) -> list[dict[str, Any]]:
        слова = str(text or "").split()
        размеченные: list[dict[str, Any]] = []
        for начало in range(0, len(слова), ОКНО_СЛОВ):
            with self.замок:
                ответ = self.труба(" ".join(слова[начало:начало + ОКНО_СЛОВ]))
            размеченные.extend(ответ or [])
        return размеченные


class _ТрубаONNX:
    """Разметка знаков препинания сетью ONNX без optimum.

    Почему не optimum, для которого это три строки. Пакет `optimum-onnx`
    прибит к `transformers` младше 4.58, а движкам распознавания нужен
    свежий — на рабочем сервере это разошлось сразу же: «optimum-onnx 0.1.0
    has requirement transformers<4.58.0, but you have transformers 5.17.0».
    Несовместимость такого рода обычно не валит запуск, а всплывает потом
    необъяснимой ошибкой загрузки модели, и ради удобства одной необязательной
    настройки её держать нельзя. Здесь тот же результат получается на голом
    onnxruntime: токенизатор — из transformers (он и так стоит), сеть —
    сессией onnxruntime, а выбор метки на слово повторяет то, что у
    transformers называется `aggregation_strategy="first"`: метка слова —
    метка его первой частицы.

    Возвращает то же, что вернула бы труба transformers: список словарей с
    `word` и `entity_group`, чтобы разбор меток остался общим с RUPunct_big.
    """

    def __init__(self, токенизатор: Any, сессия: Any, метки: dict[int, str]) -> None:
        self.токенизатор = токенизатор
        self.сессия = сессия
        self.метки = метки
        self.ожидает = {вход.name for вход in сессия.get_inputs()}

    def __call__(self, text: str) -> list[dict[str, Any]]:
        слова = str(text or "").split()
        if not слова:
            return []
        размеченные: list[dict[str, Any]] = []
        for начало in range(0, len(слова), ОКНО_СЛОВ):
            размеченные.extend(self._окно(слова[начало:начало + ОКНО_СЛОВ]))
        return размеченные

    def _окно(self, слова: list[str]) -> list[dict[str, Any]]:
        import numpy as np  # type: ignore

        кодировка = self.токенизатор(слова, is_split_into_words=True,
                                     truncation=True, max_length=512,
                                     return_tensors="np")
        входы = {имя: значение for имя, значение in dict(кодировка).items()
                 if имя in self.ожидает}
        # Сети, выгруженные из BERT, требуют `token_type_ids`, которых
        # токенизатор RoBERTa не отдаёт вовсе. Нули — ровно то, что подставил
        # бы на этом месте сам transformers: один отрезок текста, не пара.
        образец = входы.get("input_ids")
        if образец is not None:
            for имя in self.ожидает - set(входы):
                входы[имя] = np.zeros_like(образец)
        логиты = self.сессия.run(None, входы)[0][0]
        лучшие = логиты.argmax(axis=-1)

        # Метка слова — метка его первой частицы; остальные пропускаются.
        # Это ровно то, что у transformers называется
        # `aggregation_strategy="first"`.
        #
        # Слова заведомо получают метку «O»: те, что не поместились в 512
        # позиций токенизатора, вернутся в текст без знаков, но целыми. Молча
        # потерять хвост реплики хуже, чем оставить его неразмеченным.
        метки_слов: list[str] = ["O"] * len(слова)
        размечено: list[bool] = [False] * len(слова)
        for позиция, номер in enumerate(self._номера_слов(кодировка)):
            if номер is None or номер >= len(слова) or размечено[номер]:
                continue
            размечено[номер] = True
            индекс = int(лучшие[позиция]) if позиция < len(лучшие) else 0
            метки_слов[номер] = self.метки.get(индекс, "O")
        return [{"word": слово, "entity_group": метка}
                for слово, метка in zip(слова, метки_слов, strict=True)]

    @staticmethod
    def _номера_слов(кодировка: Any) -> list[int | None]:
        """Какому слову принадлежит каждая частица.

        У быстрого токенизатора это готовый `word_ids()`. У медленного его
        нет вовсе, и тогда вариант ONNX просто недоступен — сказать об этом
        прямо лучше, чем разметить текст наугад.
        """
        получить = getattr(кодировка, "word_ids", None)
        if получить is None:
            raise RuntimeError(
                "Токенизатор модели пунктуации не быстрый: у него нет "
                "word_ids(), без которого метку нельзя отнести к слову. "
                "Поставьте «tokenizers» или выберите вариант «rupunct».")
        return list(получить(0))


def _рупункт_onnx() -> Callable[[str], str] | None:
    """RUPunct в виде ONNX: двадцать девять мегабайт вместо семисот.

    Зачем ещё одна модель пунктуации, когда есть RUPunct_big. Тот весит
    711 МБ, требует transformers и torch и держит их в памяти всё время
    работы сервера. Здесь — та же разметка, но квантованная до int8 и
    исполняемая onnxruntime: он ставится на Python 3.14 (в отличие от
    половины речевых пакетов), работает на процессоре с приемлемой
    скоростью и не отнимает видеопамять у распознавания. Для сервера, где
    видеокарта занята моделью на тридцать гигабайт, это не мелочь.

    Разметка у моделей одна и та же, поэтому разбор меток общий с
    `_apply_rupunct` — расходиться им нельзя.
    """
    import onnxruntime  # type: ignore
    from transformers import AutoConfig, AutoTokenizer  # type: ignore

    источник = os.environ.get("ASRHUB_RUPUNCT_ONNX", RUPUNCT_ONNX)
    токенизатор = AutoTokenizer.from_pretrained(источник, strip_accents=False,
                                                add_prefix_space=True)
    конфиг = AutoConfig.from_pretrained(источник)
    метки = {int(ключ): str(значение)
             for ключ, значение in dict(getattr(конфиг, "id2label", None) or {}).items()}
    if not метки:
        raise RuntimeError(
            f"У модели «{источник}» в config.json нет id2label — "
            "без него номер метки не превратить в знак препинания.")
    сессия = onnxruntime.InferenceSession(_файл_onnx(источник),
                                          providers=["CPUExecutionProvider"])
    труба = _ТрубаONNX(токенизатор, сессия, метки)
    # Токенизатор тот же быстрый, что и у transformers, и так же не терпит
    # одновременного вызова из двух заданий (см. `_ТрубаОкнами`).
    замок = threading.Lock()

    def применить(text: str) -> str:
        with замок:
            return _apply_rupunct(труба, text)

    return применить


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
    return _расставить(text, model, language)[0]


def _расставить(text: str, model: str = "auto",
                language: str = "ru") -> tuple[str, str]:
    """Знаки препинания и, если модель не справилась, — почему (иначе пусто).

    Причина нужна заданию: запасное правило — заглавная буква и точка в
    конце реплики — это не пунктуация, а без причины в предупреждениях
    задание показывало «пунктуация применена», и разницу видел только тот,
    кто читал журнал сервера.
    """
    if not text.strip():
        return text, ""
    fn = _load_punctuator(model, language)
    if fn is None:
        причина = _ПРИЧИНЫ_ОТКАЗА.get(model) or "модель недоступна"
        return _naive_punctuation(text), причина
    try:
        return normalize_spaces(fn(text)), ""
    except Exception as exc:
        log.warning("Модель пунктуации дала сбой (%s), применено простое правило", exc)
        return _naive_punctuation(text), f"сбой модели: {exc}"


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
    "ноль": 0, "один": 1, "одна": 1, "одно": 1, "одну": 1, "два": 2, "две": 2,
    "три": 3, "четыре": 4,
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
    key = f"{backend}:{language}"
    if _готово_в_кеше(_ITN_CACHE, key, "itn"):
        return _ITN_CACHE[key]
    with _ЗАГРУЗКА_МОДЕЛЕЙ:
        return _load_itn_unlocked(backend, language)


def _load_itn_unlocked(backend: str, language: str):
    # Язык здесь значим: InverseNormalizer(lang=...) создаётся под конкретный
    # язык. Но число языков ограничиваем — иначе кеш растёт без предела.
    key = f"{backend}:{language}"
    if _готово_в_кеше(_ITN_CACHE, key, "itn"):
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

            # Каталог для готовых грамматик. Без него NeMo собирает их ЗАНОВО
            # при каждом запуске процесса: замеренные 125 секунд против 1,6
            # с кешем. Сервер перезапускают при каждом обновлении, а
            # воркеров бывает несколько — две минуты тишины на старте
            # выглядели как зависший сервер, и причина нигде не называлась.
            normalizer = InverseNormalizer(lang=language, cache_dir=_кеш_грамматик())
            _ITN_CACHE[key] = normalizer.inverse_normalize
            return _ITN_CACHE[key]
        except TypeError:
            # Старая версия пакета без `cache_dir` — работаем как раньше.
            try:
                from nemo_text_processing.inverse_text_normalization.inverse_normalize import (  # type: ignore
                    InverseNormalizer,
                )

                normalizer = InverseNormalizer(lang=language)
                _ITN_CACHE[key] = normalizer.inverse_normalize
                return _ITN_CACHE[key]
            except Exception as exc:
                _report_missing("Нормализатор чисел", "nemo", explicit, exc)
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
    _запомнить_отказ("itn", key)
    return None


#: Основы порядковых числительных: «четверт» + окончание → 4.
#: «ё» здесь не нужна: сравнение идёт по тексту, уже приведённому к «е».
_ПОРЯДКОВЫЕ_ОСНОВЫ: dict[str, int] = {
    "перв": 1, "втор": 2, "трет": 3, "четверт": 4, "пят": 5, "шест": 6,
    "седьм": 7, "восьм": 8, "девят": 9, "десят": 10, "одиннадцат": 11,
    "двенадцат": 12, "тринадцат": 13, "четырнадцат": 14, "пятнадцат": 15,
    "шестнадцат": 16, "семнадцат": 17, "восемнадцат": 18, "девятнадцат": 19,
    "двадцат": 20, "тридцат": 30, "сороков": 40, "пятидесят": 50,
    "шестидесят": 60, "семидесят": 70, "восьмидесят": 80, "девяност": 90,
    "сот": 100, "двухсот": 200, "трехсот": 300, "четырехсот": 400,
    "пятисот": 500, "шестисот": 600, "семисот": 700, "восьмисот": 800,
    "девятисот": 900, "тысячн": 1000,
}

#: Окончание порядкового → наращение при цифре. Правило русской типографики:
#: одна буква, если окончанию предшествует гласная, две — если согласная.
#: Здесь оно записано таблицей, потому что читать таблицу проще, чем правило.
_НАРАЩЕНИЯ: dict[str, str] = {
    "ый": "й", "ий": "й", "ой": "й", "ья": "я", "ье": "е", "ьи": "и",
    "ая": "я", "яя": "я", "ое": "е", "ее": "е", "ые": "е", "ие": "е",
    "ого": "го", "его": "го", "ьего": "го",
    "ому": "му", "ему": "му", "ьему": "му",
    "ым": "м", "им": "м", "ом": "м", "ем": "м", "ьем": "м",
    "ую": "ю", "юю": "ю", "ьюю": "ю",
    "ых": "х", "их": "х", "ьих": "х",
    "ыми": "ми", "ими": "ми", "ьими": "ми",
}


def _порядковое(слово: str) -> tuple[int, str] | None:
    """Порядковое числительное → (значение, наращение). Иначе None.

    «четвертом» → (4, «м»), «двадцать третий» разбирается по слову.
    """
    разбор = _порядковое_с_окончанием(слово)
    return (разбор[0], разбор[1]) if разбор is not None else None


def _порядковое_с_окончанием(слово: str) -> tuple[int, str, str] | None:
    """То же, что `_порядковое`, и ещё окончание слова: «пятых» → (5, «х», «ых»)."""
    низ = без_ё(слово.lower().strip(".,!?;:…()«»\"'"))
    for длина in range(len(низ) - 1, 2, -1):
        основа, окончание = низ[:длина], низ[длина:]
        значение = _ПОРЯДКОВЫЕ_ОСНОВЫ.get(основа)
        if значение is not None and окончание in _НАРАЩЕНИЯ:
            return значение, _НАРАЩЕНИЯ[окончание], окончание
    return None


#: Окончания множественного числа порядкового: «пятых», «первых», «третьими».
#: После числа это почти всегда дробь («три пятых») или счёт мест («пять
#: первых»), а не хвост того же числа.
_МНОЖЕСТВЕННЫЕ = frozenset({"ых", "их", "ьих", "ыми", "ими", "ьими"})

#: Одиночное «один» — слово, а не число: «я один дома», «ни один из них».
_ОДИН = frozenset({"один", "одна", "одно", "одну"})


def _builtin_itn_ru(text: str) -> str:
    """Встроенная нормализация русских числительных без внешних библиотек.

    Покрывает целые числа до миллиардов и проценты — самые частые случаи
    в деловых записях.
    """
    tokens = text.split()
    out: list[str] = []
    buffer: list[str] = []
    #: Где в `out` оставлено словом одиночное «один» — см. перечисления ниже.
    одиночные: list[int] = []

    def flush() -> None:
        if not buffer:
            return
        value = _parse_number_ru(buffer)
        if value is not None and len(buffer) == 1 and buffer[0].lower() in _ОДИН:
            одиночные.append(len(out))
            out.extend(buffer)
        elif value is None:
            out.extend(buffer)
        else:
            out.append(str(value))
        buffer.clear()

    trailing = ".,!?;:…()«»\"'"
    for token in tokens:
        bare = token.lower().strip(trailing)
        # Порядковое числительное ПОСЛЕ разобранной группы — хвост того же
        # числа: «две тысячи двадцать» + «четвёртом» — это 2024-м, а не
        # «2020 четвёртом». Раньше выходило именно второе: группа
        # складывалась в круглое число, а порядковое оставалось словом, и
        # получался правдоподобный мусор — «в 2020 четвёртом году». В
        # расшифровке такое не отличить от настоящей даты.
        #
        # Одиноко стоящее порядковое («первый раз», «третья попытка») не
        # трогаем: это слово, а не число, и цифра вместо него читается хуже.
        #
        # Хвост — только то, что меньше младшего разряда группы: «двадцать»
        # + «пятый», «две тысячи двадцать» + «четвёртом». Прежде годилось
        # всё, что меньше разряда, умноженного на десять, и «пять первых
        # мест» становилось «6-х мест», «одна вторая» — «3-я». И не во
        # множественном числе: «три пятых акций» — дробь, а не «8-х акций».
        # Исключение — десятилетия после года: «тысяча девятьсот девяностых»
        # — это «1990-х».
        хвост = _порядковое_с_окончанием(bare) if buffer else None
        if хвост is not None:
            значение, наращение, окончание = хвост
            начало = _parse_number_ru(buffer)
            if начало is not None:
                множественное = окончание in _МНОЖЕСТВЕННЫЕ
                десятилетие = 10 <= значение < 100 and значение % 10 == 0 and начало >= 1000
                if значение < _magnitude(начало) and (not множественное or десятилетие):
                    знаки = token[len(token.rstrip(trailing)):]
                    out.append(f"{начало + значение}-{наращение}{знаки}")
                    buffer.clear()
                    continue
                # Число, за которым порядковое, но не его хвост, — дробь или
                # счёт («три пятых», «пять первых»): оставляем словами всё.
                out.extend(buffer)
                buffer.clear()
                out.append(token)
                continue
        if bare in _UNITS_RU or bare in _SCALES_RU:
            # Знак препинания разрывает число. Раньше он снимался при
            # strip и группа продолжалась дальше, поэтому «три, четыре
            # дня» складывалось в «7 дня», а «один, два, три, поехали!» —
            # в «6 поехали!». Сам знак при этом терялся: числительное с
            # точкой в конце оставляло фразу без точки.
            tail = token[len(token.rstrip(trailing)):]
            buffer.append(token[:len(token) - len(tail)] if tail else token)
            if tail:
                flush()
                if out:
                    out[-1] = out[-1] + tail
            continue
        flush()
        out.append(token)
    flush()
    # Одиночное «один» — слово («я один дома»), но в перечислении через
    # запятую рядом с числами это число: «Один, два, три, поехали!» → «1, 2, 3».
    for место in одиночные:
        слово = out[место]
        после = out[место + 1] if место + 1 < len(out) else ""
        до = out[место - 1] if место > 0 else ""
        if (слово.endswith(",") and после[:1].isdigit()) or \
                (до.endswith(",") and до[:1].isdigit()):
            out[место] = "1" + слово[len(слово.rstrip(trailing)):]
    result = " ".join(out)
    result = re.sub(r"(\d+)\s+процент(?:ов|а|)\b", r"\1 %", result)
    result = re.sub(r"(\d+)\s+рубл(?:ей|я|ь)\b", r"\1 ₽", result)
    return normalize_spaces(result)


def _magnitude(value: int) -> int:
    """Разряд младшей значащей цифры: 123 -> 1, 120 -> 10, 100 -> 100."""
    if value <= 0:
        return 1
    step = 1
    while value % (step * 10) == 0 and step < 1000:
        step *= 10
    return step


def _parse_number_ru(words: list[str]) -> int | None:
    total = 0
    current = 0
    seen = False
    last_scale = 0
    cleaned = [raw.lower().strip(".,!?;:…()«»\"'") for raw in words]
    # «Ноль» рядом с другими числительными — это цифры, продиктованные по
    # одной: «восемь ноль ноль», «код ноль пять», «добавочный ноль ноль
    # семь». Сложение давало «8», «5» и «7» — время, код и номер пропадали.
    if len(cleaned) > 1 and "ноль" in cleaned:
        return None
    for word in cleaned:
        if word in _UNITS_RU:
            value = _UNITS_RU[word]
            # Разряды числительного идут по убыванию: «сто двадцать три».
            # Одинаковый или растущий разряд — это не одно число, а
            # перечисление: «три три три» складывалось в «9», а «код пять
            # пять» — в «10». Такую группу оставляем словами.
            if current and value >= _magnitude(current):
                return None
            current += value
            seen = True
        elif word in _SCALES_RU:
            scale = _SCALES_RU[word]
            # Множитель без своего числа после уже сложенной группы («тысяча
            # тысяч», «сто тысяч миллионов») и растущий множитель — не одно
            # число: прежде выходило «2000» и «1100000».
            if (current == 0 and total > 0) or (last_scale and scale >= last_scale):
                return None
            current = (current or 1) * scale
            total += current
            current = 0
            last_scale = scale
            seen = True
        else:
            return None
    if not seen:
        return None
    return total + current


def _кеш_грамматик() -> str:
    """Куда класть собранные грамматики NeMo.

    Каталог данных, если он задан окружением, иначе общий временный. Важно
    не то, где именно он лежит, а что он вообще есть и переживает
    перезапуск: сборка грамматик занимает две минуты.
    """
    корень = (os.environ.get("ASRHUB_DATA_DIR")
              or os.environ.get("XDG_CACHE_HOME")
              or tempfile.gettempdir())
    путь = Path(корень) / "itn-grammars"
    try:
        путь.mkdir(parents=True, exist_ok=True)
    except OSError:
        return tempfile.gettempdir()
    return str(путь)


def без_ё(text: str) -> str:
    """«ё» → «е». Нужно перед грамматиками NeMo.

    Грамматика русских числительных в NeMo написана без «ё», и слово с ней
    рассыпает разбор всей конструкции:

        «в две тысячи двадцать четвёртом году»  → «в 2020 четвёртом г.»
        «в две тысячи двадцать четвертом году»  → «в 2024г.»

    Первое — не опечатка, а то, что выдают русские движки распознавания:
    и GigaAM, и Whisper ставят «ё» охотно. То есть нормализация чисел
    ломалась ровно на тех записях, где движок работал лучше всего, и
    ломалась молча: на выходе получался правдоподобный мусор.
    """
    return text.replace("ё", "е").replace("Ё", "Е")


def вернуть_ё(исходный: str, подано: str, итог: str) -> str:
    """Возвращает «ё» в слова, которых нормализация не касалась.

    В грамматику уходит текст без «ё» (см. `без_ё`), и прежде, стоило
    нормализации поменять в реплике хоть одно число, возвращался весь её
    ответ — без единой «ё». «Всё будет готово через двадцать пять минут,
    ещё раз…» превращалось в «Все будет готово через 25 минут, еще раз…»,
    а «всё» и «все», «её» и «ее» — разные слова.

    Слова сопоставляются выравниванием: всё, что нормализация оставила как
    было, берётся из исходного текста вместе с «ё», а изменённое (числа,
    сокращения) — из её ответа.
    """
    if "ё" not in исходный and "Ё" not in исходный:
        return итог
    было, без, стало = исходный.split(), подано.split(), итог.split()
    if len(было) != len(без):
        return итог
    import difflib  # noqa: PLC0415

    out = list(стало)
    сопоставление = difflib.SequenceMatcher(a=без, b=стало, autojunk=False)
    for вид, i1, i2, j1, j2 in сопоставление.get_opcodes():
        if вид == "equal":
            for k in range(i2 - i1):
                out[j1 + k] = было[i1 + k]
        elif вид == "replace":
            # Слово внутри изменённого куска могло остаться прежним — тогда
            # и «ё» в нём прежняя.
            прежние = {без[i]: было[i] for i in range(i1, i2)}
            for j in range(j1, j2):
                if out[j] in прежние:
                    out[j] = прежние[out[j]]
    return " ".join(out)


def apply_itn(text: str, backend: str = "auto", language: str = "ru") -> str:
    if not text.strip():
        return text
    fn = _load_itn(backend, language)
    if fn is not None:
        try:
            # «ё» снимается только с того, что уходит в грамматику: если
            # нормализация ничего не изменила, возвращаем ИСХОДНЫЙ текст с
            # «ё» на месте. Терять её в расшифровке нельзя — это буква
            # русского языка, а не служебный знак.
            подано = без_ё(text) if language == "ru" else text
            итог = normalize_spaces(fn(подано))
            if language == "ru" and итог.strip() == подано.strip():
                return normalize_spaces(text)
            if language == "ru":
                return normalize_spaces(вернуть_ё(normalize_spaces(text),
                                                  normalize_spaces(подано), итог))
            return итог
        except Exception as exc:
            log.warning("Нормализация чисел дала сбой (%s), применено встроенное правило", exc)
    if language == "ru":
        return _builtin_itn_ru(text)
    return text


# --------------------------------------------------------------------------
# 5. Словарь замен
# --------------------------------------------------------------------------

#: Сколько секунд даётся словарю замен с выражениями на всю запись разом.
#: Выражения выполняются в отдельном процессе (см. `применить_словарь`), и
#: предел ограничивает именно его: простая замена слов в него не входит.
ПРЕДЕЛ_ВЫРАЖЕНИЙ_С = 20.0

#: Предел длины одного выражения: больше — это уже не правило замены, а
#: программа, и проверить её на безопасность нечем.
ДЛИНА_ВЫРАЖЕНИЯ = 500


def _опасный_повтор(шаблон: str) -> str:
    """Почему выражение может работать экспоненциально долго; пусто — не видно.

    Python re при катастрофическом откате не отпускает GIL: стоят и
    HTTP, и отметка жизни, и все воркеры. Здесь — проверка при приёме
    словаря, чтобы ошибка администратора всплыла сразу, а не первой длинной
    репликой. Ловит классику: повтор внутри повтора («(\\w+\\s?)+»), и
    повтор над вариантами, которые могут начаться одинаково («(a|ab)+»).
    Полной защитой она не является — поэтому сами выражения ещё и
    выполняются в отдельном процессе с пределом времени.
    """
    try:
        from re import _constants as C  # type: ignore[attr-defined]  # noqa: PLC0415
        from re import _parser  # type: ignore[attr-defined]  # noqa: PLC0415
    except ImportError:                                     # pragma: no cover
        return ""
    try:
        дерево = _parser.parse(шаблон)
    except Exception:                                       # noqa: BLE001
        return ""                       # синтаксис проверит re.compile

    повторы = {C.MAX_REPEAT, C.MIN_REPEAT}

    def начала(ветка: Any) -> set[Any] | None:
        """С чего может начаться ветка: множество литер или None — «с чего угодно»."""
        for op, av in ветка:
            if op == C.LITERAL:
                return {av}
            if op == C.SUBPATTERN:
                return начала(av[-1])
            return None
        return set()

    def якорь(ветка: Any) -> bool:
        """Есть ли в теле повтора обязательная литера вне вложенных повторов.

        Она делит круги повтора однозначно: «(?:\\s*,\\s*)+» без запятой
        круга не сделает, и разбить строку на круги можно одним способом.
        """
        for op, av in ветка:
            if op == C.LITERAL:
                return True
            if op == C.SUBPATTERN and якорь(av[-1]):
                return True
        return False

    def обход(ветка: Any, в_повторе: bool, под_повтором: bool) -> str:
        """`в_повторе` — внутри повтора без якоря; `под_повтором` — внутри любого."""
        for op, av in ветка:
            if op in повторы:
                _, верх, внутри = av
                # Повтор с малым пределом («{2}») не взрывается: кругов мало.
                многократный = верх > 10
                if верх > 1 and в_повторе:
                    return "повтор внутри повтора"
                причина = обход(внутри, в_повторе or (многократный and not якорь(внутри)),
                                под_повтором or многократный)
                if причина:
                    return причина
            elif op == C.SUBPATTERN:
                причина = обход(av[-1], в_повторе, под_повтором)
                if причина:
                    return причина
            elif op == C.BRANCH:
                ветви = av[1]
                if под_повтором and len(ветви) > 1:
                    виденные: set[Any] = set()
                    for в in ветви:
                        н = начала(в)
                        # Пустой вариант («a|aa» парсер сворачивает в
                        # «a(|a)») продолжается следующим кругом повтора —
                        # то есть начинается так же, как соседний.
                        if н is None or not н or н & виденные:
                            return "повтор над вариантами, которые начинаются одинаково"
                        виденные |= н
                for в in ветви:
                    причина = обход(в, в_повторе, под_повтором)
                    if причина:
                        return причина
            elif op in (C.ASSERT, C.ASSERT_NOT):
                причина = обход(av[1], в_повторе, под_повтором)
                if причина:
                    return причина
        return ""

    return обход(дерево, False, False)


def проверить_словарь(glossary: Any) -> list[str]:
    """Ошибки словаря замен: вид записей, синтаксис и опасность выражений.

    Прежде словарь принимался любым: `{"а": 5}` ронял всю постобработку
    задания (TypeError посреди замен — без пунктуации, чисел и имён
    говорящих), а выражение с катастрофическим откатом замораживало сервер.
    """
    if not isinstance(glossary, dict):
        return ['ожидается объект «что заменить: на что», например {"сбер": "Сбер"}']
    ошибки: list[str] = []
    for ключ, значение in glossary.items():
        if not isinstance(ключ, str) or not isinstance(значение, str):
            ошибки.append(f"«{ключ}»: замена должна быть строкой")
            continue
        if not ключ.startswith("re:"):
            continue
        шаблон = ключ[3:]
        if len(шаблон) > ДЛИНА_ВЫРАЖЕНИЯ:
            ошибки.append(f"«{ключ[:40]}…»: выражение длиннее {ДЛИНА_ВЫРАЖЕНИЯ} знаков")
            continue
        try:
            выражение = re.compile(шаблон, re.IGNORECASE | re.UNICODE)
            выражение.subn(значение, "")      # разбирает и строку замены
        except re.error as exc:
            ошибки.append(f"«{ключ}»: {exc}")
            continue
        # Две проверки: своя (повтор внутри повтора, варианты с общим
        # началом) и `safe_regex` (неоднозначная граница между кругами
        # повтора — «(?:\s*,\s*)+» на «, , , ,» откатывается 2ⁿ раз).
        опасность = _опасный_повтор(шаблон) or _опасное_выражение(шаблон)
        if опасность:
            ошибки.append(f"«{ключ}»: {опасность} — такое выражение на длинной "
                          "реплике работает часами")
    return ошибки


def _есть_выражения(glossary: dict[str, Any]) -> bool:
    return any(isinstance(ключ, str) and ключ.startswith("re:") for ключ in glossary)


def _без_выражений(glossary: dict[str, Any]) -> dict[str, Any]:
    return {к: з for к, з in glossary.items() if not (isinstance(к, str) and к.startswith("re:"))}


#: Что выполняется в отдельном процессе: тот же `apply_glossary`, что и здесь.
_ПРОЦЕСС_СЛОВАРЯ = (
    "import json, sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from asrhub.pipeline.postprocess import apply_glossary\n"
    "данные = json.load(sys.stdin)\n"
    "итог = [apply_glossary(t, данные['g']) for t in данные['t']]\n"
    "json.dump(итог, sys.stdout, ensure_ascii=False)\n"
)


def применить_словарь(тексты: list[str], glossary: dict[str, Any], *,
                      предел_с: float | None = None) -> tuple[list[str], int, str]:
    """Словарь замен ко всем репликам. Возвращает (тексты, замен, предупреждение).

    Без выражений — здесь же. С выражениями — в отдельном процессе с
    пределом времени: модуля regex с тайм-аутом в зависимостях нет, а
    стандартный re при катастрофическом откате держит GIL, и остановить его
    из соседнего потока нельзя. Процесс убивается по пределу, выражения
    этой записи пропускаются, простые замены применяются — и задание
    говорит об этом предупреждением, а сервер продолжает отвечать.
    """
    if not glossary:
        return list(тексты), 0, ""
    # Выражения, которые проверка признаёт опасными, не исполняются вовсе —
    # ни здесь, ни в отдельном процессе (там `apply_glossary` пропустил бы
    # их молча). Словарь мог попасть в задание до проверки: из старой
    # конфигурации или снимком настроек давнего задания.
    опасные = {к: (_опасный_повтор(к[3:]) or _опасное_выражение(к[3:]))
               for к in glossary if isinstance(к, str) and к.startswith("re:")}
    опасные = {к: причина for к, причина in опасные.items() if причина}
    заметка = ""
    if опасные:
        glossary = {к: з for к, з in glossary.items() if к not in опасные}
        первое = next(iter(опасные))
        заметка = (f"Выражения словаря замен («re:…») пропущены: «{первое[:60]}» — "
                   f"{опасные[первое]}"
                   + (f" (и ещё {len(опасные) - 1})" if len(опасные) > 1 else "")
                   + ". Простые замены применены.")
        log.warning("Словарь замен: опасные выражения пропущены: %s", ", ".join(опасные))
    if not _есть_выражения(glossary):
        итог = [apply_glossary(т, glossary) for т in тексты]
        return [т for т, _ in итог], sum(н for _, н in итог), заметка
    import json  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    предел = float(ПРЕДЕЛ_ВЫРАЖЕНИЙ_С if предел_с is None else предел_с)
    корень = str(Path(__file__).resolve().parents[2])
    причина = ""
    try:
        готово = subprocess.run(
            [sys.executable, "-I", "-c", _ПРОЦЕСС_СЛОВАРЯ, корень],
            input=json.dumps({"g": glossary, "t": list(тексты)}, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8",
            timeout=max(1.0, предел), check=False)
        if готово.returncode == 0:
            итог = json.loads(готово.stdout or "[]")
            if isinstance(итог, list) and len(итог) == len(тексты):
                return ([str(т) for т, _ in итог], sum(int(н) for _, н in итог), заметка)
        причина = ((готово.stderr or "").strip().splitlines()[-1:] or ["отказ процесса"])[0][:200]
    except subprocess.TimeoutExpired:
        причина = f"не уложились в {предел:g} с"
    except (OSError, ValueError, TypeError) as exc:
        причина = str(exc)[:200]
    log.warning("Выражения словаря замен пропущены: %s", причина)
    простые = _без_выражений(glossary)
    итог = [apply_glossary(т, простые) for т in тексты]
    return ([т for т, _ in итог], sum(н for _, н in итог),
            f"Выражения словаря замен («re:…») пропущены: {причина}. "
            "Простые замены применены.")
@functools.lru_cache(maxsize=256)
def _опасное_выражение(образец: str) -> str:
    from ..safe_regex import проблема  # noqa: PLC0415

    return проблема(образец)


def apply_glossary(text: str, glossary: dict[str, str]) -> tuple[str, int]:
    if not glossary:
        return text, 0
    count = 0
    for source, target in glossary.items():
        if not source:
            continue
        if source.startswith("re:"):
            # Выражение с вложенным повтором останавливает весь процесс: `re`
            # откатывается без предела и не отпускает GIL. Приём параметров
            # такие отвергает, а здесь — последний рубеж для словаря, попавшего
            # в задание другой дорогой (см. `safe_regex`).
            беда = _опасное_выражение(source[3:])
            if беда:
                log.warning("Выражение словаря замен пропущено: %s", беда)
                continue
            try:
                pattern = re.compile(source[3:], re.IGNORECASE | re.UNICODE)
            except re.error as exc:
                log.warning("Некорректное регулярное выражение «%s»: %s", source, exc)
                continue
            try:
                # subn тоже разбирает строку замены как шаблон, и ошибка в
                # ней — например ссылка \1 на несуществующую группу — летела
                # мимо перехвата выше. Одна опечатка в словаре пользователя
                # обрушивала весь шаг постобработки и теряла расшифровку.
                text, n = pattern.subn(target, text)
            except re.error as exc:
                log.warning("Некорректная замена для «%s»: %s", source, exc)
                continue
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
    # Корень ищется с начала слова, а не в любом его месте. Прежний
    # ведущий \w* ловил корень внутри нейтральных слов: «хлебать» и
    # «нахлебался» приходили пользователю как «х******» — фильтр портил
    # обычный текст. Приставки при этом остаются нужны, поэтому для них
    # заведён короткий явный список.
    prefixes = "|".join(("", "на", "за", "по", "от", "до", "пере", "под", "вы",
                         "при", "про", "раз", "рас", "с", "у", "о", "об"))
    pattern = re.compile(
        r"\b(?:" + prefixes + r")(?:" + "|".join(_PROFANITY_ROOTS) + r")\w*\b",
        re.IGNORECASE | re.UNICODE)
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


def _слова_без_мата(слова: list[Any], mode: str) -> list[Any]:
    """Пословные метки после фильтра: то же правило, что и для текста."""
    итог: list[Any] = []
    for слово in слова:
        if not isinstance(слово, dict):
            итог.append(слово)
            continue
        поле = "word" if "word" in слово else ("text" if "text" in слово else None)
        if поле is None:
            итог.append(слово)
            continue
        значение, n = filter_profanity(str(слово.get(поле) or ""), mode)
        if not n:
            итог.append(слово)
            continue
        if mode == "remove" or not значение.strip():
            continue
        итог.append({**слово, поле: значение})
    return итог


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
            # Условие «и там, и там» выбрасывало слова целиком, когда у
            # одной из склеиваемых реплик список пуст, — а это обычное
            # дело: движки часто не дают слов для реплики в одно слово.
            # Склейка «да» + «конечно поможем» теряла границы двух слов
            # безвозвратно, и оба параметра стоят по умолчанию.
            if last.get("words") or seg.get("words"):
                last["words"] = [*(last.get("words") or []),
                                 *(seg.get("words") or [])]
            confs = [c for c in (last.get("confidence"), seg.get("confidence")) if c is not None]
            if confs:
                last["confidence"] = sum(confs) / len(confs)
        else:
            merged.append(dict(seg))
    return merged


def build_paragraphs(segments: list[dict[str, Any]], mode: str = "speaker",
                     pause_s: float = 2.0, sentences: int = 4,
                     speaker_labels: bool = True) -> list[str]:
    """Собирает абзацы из сегментов.

    speaker_labels=False убирает префикс «Говорящий N: ». Он нужен в тексте
    для чтения, но мешает в двух местах: настройка include_speaker_labels
    на txt раньше не действовала вовсе, а в расчёте точности каждый префикс
    добавлял к эталону две лишние вставки и завышал WER.
    """
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
        if mode == "speaker" and speaker and speaker_labels and (not current):
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

def _к_движку(ключ: str, engine: str) -> bool:
    """Относится ли параметр каталога к этому движку.

    Фильтр галлюцинаций объявлен в каталоге только для семейства Whisper, и
    для остальных движков интерфейс его не показывает, а значит, и выключить
    его там нельзя. Применялся же он ко всем: на модели по умолчанию
    (GigaAM) из звонка пропадали настоящие реплики — «алло алло алло»,
    «да да да», «всем пока», «до новых встреч». Зацикливание и фразы из
    обучающих субтитров — беда порождающего декодера; у GigaAM, NeMo и Vosk
    повтор в тексте — это повтор в речи. Движок не известен — как раньше.
    """
    if not engine:
        return True
    from ..catalog import PARAMS_BY_KEY  # noqa: PLC0415

    spec = PARAMS_BY_KEY.get(ключ)
    return spec is None or not spec.engines or engine in spec.engines


def process(segments: list[dict[str, Any]], settings: dict[str, Any],
            *, model_has_punctuation: bool = False,
            engine: str = "") -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Применяет весь конвейер постобработки. Возвращает (сегменты, статистику).

    `engine` — движок, который распознавал: от него зависит, нужен ли фильтр
    галлюцинаций (см. `_к_движку`).
    """
    stats: dict[str, Any] = {
        "hallucinations_removed": 0,
        "glossary_replacements": 0,
        "profanity_hits": 0,
        "segments_before": len(segments),
    }
    #: Что сказать человеку о постобработке. Уходит в `stats["warnings"]`, а
    #: оттуда конвейер переносит в предупреждения задания.
    предупреждения: list[str] = []
    language = str(settings.get("language") or "ru")
    if language == "auto":
        language = str(segments[0].get("language") or "ru") if segments else "ru"

    галлюцинации_возможны = _к_движку("hallucination_filter", engine)
    if settings.get("hallucination_filter", True) and галлюцинации_возможны:
        segments, removed = filter_hallucinations(
            segments, строки(settings.get("hallucination_phrases"), запятые=False))
        stats["hallucinations_removed"] = removed

    for seg in segments:
        текст = normalize_spaces(seg.get("text", ""))
        # Схлопывание «да да да да» в «да да» — против зацикливания того же
        # декодера; настоящий повтор в речи оставляем как сказан.
        seg["text"] = collapse_repeats(текст) if галлюцинации_возможны else текст

    if settings.get("remove_filler_words"):
        for seg in segments:
            seg["text"] = remove_fillers(seg["text"], строки(settings.get("filler_words")))

    if settings.get("punctuation_enabled", True) and not model_has_punctuation:
        model_name = str(settings.get("punctuation_model") or "auto")
        отказы: list[str] = []
        for seg in segments:
            seg["text"], причина = _расставить(seg["text"], model_name, language)
            if причина:
                отказы.append(причина)
        # «Применено» — только если знаки расставила модель. Запасное
        # правило (заглавная и точка) пунктуацией не является, и задание
        # обязано об этом сказать, как говорит о подмене диаризации.
        stats["punctuation_applied"] = not отказы
        if отказы:
            stats["punctuation_fallback"] = len(отказы)
            предупреждения.append(
                f"Пунктуация не расставлена: модель «{model_name}» недоступна "
                f"({отказы[0]}). Применено простое правило — заглавная буква и "
                "точка в конце реплики.")

    if settings.get("itn_enabled", True):
        backend = str(settings.get("itn_backend") or "auto")
        for seg in segments:
            seg["text"] = apply_itn(seg["text"], backend, language)
        stats["itn_applied"] = True

    glossary = settings.get("glossary") or {}
    if isinstance(glossary, dict) and glossary:
        тексты, total, беда = применить_словарь([seg["text"] for seg in segments], glossary)
        for seg, текст in zip(segments, тексты, strict=True):
            seg["text"] = текст
        stats["glossary_replacements"] = total
        if беда:
            предупреждения.append(беда)

    mode = str(settings.get("profanity_filter") or "off")
    if mode != "off":
        total = 0
        for seg in segments:
            seg["text"], n = filter_profanity(seg["text"], mode)
            total += n
            # И в пословных метках: JSON выгружает `words` по умолчанию, и
            # при маскировании текста исходные слова оставались в
            # `words[].word` — фильтр работал только для глаз.
            if n and seg.get("words"):
                seg["words"] = _слова_без_мата(seg["words"], mode)
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
    if предупреждения:
        stats["warnings"] = предупреждения
    return segments, stats


#: Метка канала при разделении стерео по каналам (audio.prepare, «split»).
_КАНАЛ_RE = re.compile(r"^Канал (\d+)$")


def _speaker_mapping(segments: list[dict[str, Any]], names: list[str]) -> dict[str, str]:
    """Какое имя получает каждая метка говорящего.

    Каналы — по номеру: «Канал 1» — первое имя, «Канал 2» — второе, кто бы
    ни заговорил первым. Раньше и каналы шли по порядку первой реплики, и в
    исходящем звонке, где первым звучит «Алло» абонента из второго канала,
    вся речь клиента уходила под именем оператора. Совместимость с phone_asr
    на этом держится целиком: там первый канал — всегда SPEAKER_00, а
    `swap_sides` меняет местами каналы, а не «кто заговорил первым».

    Метки диаризации (кто есть кто, заранее не известно) — по порядку
    первого появления, как и прежде.
    """
    mapping: dict[str, str] = {}
    order: list[str] = []
    for seg in segments:
        speaker = seg.get("speaker")
        if not speaker or speaker in mapping or speaker in order:
            continue
        канал = _КАНАЛ_RE.match(str(speaker))
        if канал:
            номер = int(канал.group(1)) - 1
            if 0 <= номер < len(names):
                mapping[speaker] = names[номер]
            continue
        order.append(speaker)
    занятые = set(mapping.values())
    свободные = [имя for имя in names if имя not in занятые] if mapping else names
    for i, speaker in enumerate(order):
        if i < len(свободные):
            mapping[speaker] = свободные[i]
    return mapping
