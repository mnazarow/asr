"""Заход 36: надстройки, за которыми не было кода.

Заход 35 убрал из справочника три варианта-пустышки: извлечение вокала,
пунктуацию vosk и нормализацию runorm. Здесь на их место встаёт то, что
работает, — и три поправки к тому, что уже работало, но не так.

Общее у проверок этой главы: они описывают, чем ошибка выглядела бы в
расшифровке. Нормализация чисел, сложившая «в две тысячи двадцать
четвёртом году» в «в 2020 четвёртом году», не падает и не жалуется — она
выдаёт правдоподобную дату, отличить которую от настоящей нельзя.
"""
from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Заготовки звука
# ---------------------------------------------------------------------------

def запись(путь: Path, *, шум: float = 0.0, тон: float = 0.3,
           секунд: float = 3.0, rate: int = 16000) -> Path:
    """Речеподобный сигнал: тон через секунду плюс белый шум нужной силы."""
    путь.parent.mkdir(parents=True, exist_ok=True)
    сл = random.Random(7)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(rate)
        отсчёты = []
        for и in range(int(rate * секунд)):
            речь = тон * math.sin(2 * math.pi * 180 * и / rate) if int(и / rate) % 2 == 0 else 0.0
            значение = int(32767 * (речь + шум * (сл.random() * 2 - 1)))
            отсчёты.append(struct.pack("<h", max(-32767, min(32767, значение))))
        файл.writeframes(b"".join(отсчёты))
    return путь


# ---------------------------------------------------------------------------
# Нормализация чисел
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("сказано,ожидание", [
    ("в две тысячи двадцать четвёртом году", "в 2024-м году"),
    ("в две тысячи двадцать пятом", "в 2025-м"),
    ("двадцать третий участник", "23-й участник"),
    ("сорок первый километр", "41-й километр"),
    ("две тысячи двадцать четвертого января", "2024-го января"),
])
def test_порядковое_числительное_не_даёт_правдоподобного_мусора(сказано, ожидание):
    """«две тысячи двадцать» + «четвёртом» складывалось в «2020 четвёртом».

    Группа числительных разбиралась в круглое число, а порядковое
    оставалось словом — и получалась строка, которую от настоящей даты не
    отличить: «в 2020 четвёртом году». Ни ошибки, ни предупреждения; в
    архиве оседал год, которого в разговоре не называли.
    """
    from asrhub.pipeline.postprocess import _builtin_itn_ru

    assert _builtin_itn_ru(сказано) == ожидание


@pytest.mark.parametrize("сказано", ["первый раз", "третья попытка", "второе окно",
                                    "во вторую смену"])
def test_одинокое_порядковое_остаётся_словом(сказано):
    """Проверка парная: «первый раз» цифрой читается хуже, чем словом.

    Числом порядковое становится только там, где оно продолжает уже
    разобранную группу, — то есть там, где иначе получался бы мусор.
    """
    from asrhub.pipeline.postprocess import _builtin_itn_ru

    assert _builtin_itn_ru(сказано) == сказано


def test_буква_ё_не_ломает_нормализацию_и_не_пропадает(monkeypatch):
    """Грамматика чисел написана без «ё», а движки распознавания её ставят.

    Слово с «ё» рассыпало разбор всей конструкции, то есть нормализация
    ломалась ровно на тех записях, где движок сработал лучше всего. При
    этом сама «ё» — буква русского языка: снимать её со всей расшифровки
    ради грамматики нельзя.

    Внешний нормализатор здесь поддельный, и слеп к «ё» ровно так же, как
    настоящий: ставить NeMo ради одной проверки незачем, а воспроизводить
    надо именно его поведение.
    """
    from asrhub.pipeline import postprocess
    from asrhub.pipeline.postprocess import apply_itn, без_ё

    assert без_ё("четвёртом") == "четвертом"
    assert без_ё("ЁЛКА") == "ЕЛКА"

    def слепой_к_ё(текст: str) -> str:
        """Как настоящая грамматика: «ё» не знает и на ней спотыкается."""
        if "ё" in текст:
            return текст.replace("две тысячи двадцать", "2020")
        return (текст.replace("в две тысячи двадцать четвертом году", "в 2024г.")
                .replace("двадцать пять", "25"))

    postprocess._ITN_CACHE.clear()
    monkeypatch.setattr(postprocess, "_load_itn", lambda *_а: слепой_к_ё)
    try:
        # Подаём с «ё» — а до грамматики доходит уже без неё.
        assert apply_itn("в две тысячи двадцать четвёртом году", "auto", "ru") == "в 2024г."
        assert apply_itn("мне двадцать пять лет", "auto", "ru") == "мне 25 лет"
        # Числа нет, грамматика ничего не изменила — текст возвращается как
        # был, вместе с «ё». Терять её в расшифровке нельзя.
        assert apply_itn("ёлка у подъезда", "auto", "ru") == "ёлка у подъезда"
        assert apply_itn("всё понятно, спасибо", "auto", "ru") == "всё понятно, спасибо"
    finally:
        monkeypatch.undo()
        postprocess._ITN_CACHE.clear()

    # И то же самое встроенной нормализацией, без всякого внешнего пакета.
    assert apply_itn("в две тысячи двадцать четвёртом году", "auto", "ru") == "в 2024-м году"


def test_грамматики_нормализации_складываются_в_каталог(monkeypatch, tmp_path: Path):
    """Без каталога NeMo собирает грамматики заново при каждом запуске.

    Замеры: 125 секунд на сборку против 1,6 секунды с готовым кешем.
    Сервер перезапускают при каждом обновлении, воркеров бывает несколько —
    две минуты тишины на старте выглядели как зависший сервер, и причина
    нигде не называлась.
    """
    import inspect

    from asrhub.pipeline import postprocess

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path))
    каталог = Path(postprocess._кеш_грамматик())
    assert каталог.is_dir(), каталог
    assert str(tmp_path) in str(каталог)

    # Загрузка — в `_load_itn_unlocked`, обёртка лишь берёт замок (заход 41).
    текст = inspect.getsource(postprocess._load_itn_unlocked)
    assert "cache_dir=" in текст, "грамматики снова собираются каждый раз"


# ---------------------------------------------------------------------------
# Пунктуация
# ---------------------------------------------------------------------------

def test_multilingual_для_русского_подменяется_и_говорит_об_этом(caplog):
    """У deepmultilingualpunctuation русского нет ни в одной модели.

    `fullstop-punctuation-multilang-large` — это английский, немецкий,
    французский и итальянский; «sonar-base» добавляет нидерландский.
    Выбранный вручную для русской записи, он молча возвращал текст без
    знаков, и это выглядело как «модель не справилась», а не как «модель
    этого языка не знает».
    """
    import logging

    from asrhub.pipeline import postprocess

    postprocess._PUNCT_CACHE.clear()
    with caplog.at_level(logging.WARNING):
        итог = postprocess.restore_punctuation("привет как дела", "multilingual", "ru")
    assert "русского языка не знает" in caplog.text, caplog.text
    assert итог.strip(), итог
    postprocess._PUNCT_CACHE.clear()


def test_лёгкая_пунктуация_заявлена_и_разбирает_метки():
    """RUPunct в ONNX: 29 МБ вместо 711, без torch и без видеопамяти.

    Модель здесь не скачивается — проверяется, что вариант объявлен,
    доезжает до загрузчика и что разметку он разбирает той же функцией, что
    и большая модель: расходиться этим двум нельзя.
    """
    import inspect

    from asrhub.catalog.params import get_param
    from asrhub.pipeline import postprocess

    возможные = {в["value"] for в in get_param("punctuation_model").options}
    assert "rupunct_onnx" in возможные
    assert "vosk-punct" not in возможные, "вернулся вариант, за которым нет кода"

    assert "_apply_rupunct" in inspect.getsource(postprocess._рупункт_onnx), \
        "разбор меток разошёлся с большой моделью"

    # Разбор меток — на поддельной трубе, без единого мегабайта весов.
    метки = [{"word": "здравствуйте", "entity_group": "UPPER_COMMA"},
             {"word": "чем", "entity_group": "LOWER_O"},
             {"word": "помочь", "entity_group": "LOWER_QUESTION"}]
    assert postprocess._apply_rupunct(lambda _т: метки, "здравствуйте чем помочь") \
        == "Здравствуйте, чем помочь?"


def test_лёгкая_пунктуация_действительно_вызывается(monkeypatch):
    """Вариант в справочнике, не доходящий до кода, — это пустышка.

    Ровно такими были три варианта, убранные заходом 35. Здесь загрузчик
    модели подменён — скачивать двадцать девять мегабайт ради проверки
    незачем, — но путь до него настоящий.
    """
    from asrhub.pipeline import postprocess

    звали: list[int] = []

    def подмена():
        звали.append(1)
        return lambda текст: текст.capitalize() + "."

    postprocess._PUNCT_CACHE.clear()
    monkeypatch.setattr(postprocess, "_рупункт_onnx", подмена)
    try:
        итог = postprocess.restore_punctuation("это простая фраза", "rupunct_onnx", "ru")
        assert звали, "выбран rupunct_onnx, а загружали что-то другое"
        assert итог == "Это простая фраза."
    finally:
        postprocess._PUNCT_CACHE.clear()


def test_недоступная_модель_пунктуации_не_роняет_расшифровку():
    """Надстройки может не быть — это не повод терять текст."""
    from asrhub.pipeline import postprocess

    postprocess._PUNCT_CACHE.clear()
    итог = postprocess.restore_punctuation("это простая фраза", "rupunct_onnx", "ru")
    assert итог == "Это простая фраза."
    postprocess._PUNCT_CACHE.clear()


# ---------------------------------------------------------------------------
# Избирательное шумоподавление
# ---------------------------------------------------------------------------

def test_чистую_запись_не_чистят(tmp_path: Path):
    """Очистка чистой записи не бесполезна, а вредна.

    Вместе с шумом уходит часть речи, и распознавание становится хуже. В
    работе 2025 года популярную модель прогнали по сорока сочетаниям
    «запись × шум» и не нашли ни одного, где ошибка уменьшилась бы.
    Избирательная очистка по порогу — единственный подход, у которого в
    измерениях положительный результат.
    """
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    настройки = load().merged({"audio_denoise": "afftdn",
                               "audio_denoise_below_snr_db": 15.0})
    чистая = запись(tmp_path / "чистая.wav", шум=0.001)
    чистить, почему = модуль.нужна_очистка(чистая, настройки)
    assert чистить is False, "чистую запись отправили на шумоподавление"
    assert "выше порога" in почему, почему


def test_шумную_запись_чистят(tmp_path: Path):
    """Проверка парная: порог не должен отменять очистку вообще."""
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    настройки = load().merged({"audio_denoise": "afftdn",
                               "audio_denoise_below_snr_db": 15.0})
    шумная = запись(tmp_path / "шумная.wav", шум=0.08)
    assert модуль.нужна_очистка(шумная, настройки)[0] is True


def test_без_порога_поведение_прежнее(tmp_path: Path):
    """Ноль означает «чистить всё подряд» — как было до этой настройки."""
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    настройки = load().merged({"audio_denoise": "afftdn"})
    чистая = запись(tmp_path / "чистая.wav", шум=0.001)
    assert модуль.нужна_очистка(чистая, настройки) == (True, "")


def test_пропуск_очистки_виден_в_замечаниях(tmp_path: Path):
    """Тихое решение — плохое решение: сервер обязан сказать, что сделал."""
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    настройки = load().merged({"audio_denoise": "afftdn",
                               "audio_denoise_below_snr_db": 15.0})
    готово = модуль.prepare(запись(tmp_path / "чистая.wav", шум=0.001),
                            tmp_path / "работа", настройки)
    assert готово.warnings and "сигнал/шум" in готово.warnings[0], готово.warnings


def test_snr_меряется_по_исходнику(tmp_path: Path):
    """Решение принимается по тому, что пришло, а не по обработанному."""
    from asrhub.pipeline import audio as модуль

    чистая = модуль.оценить_snr(запись(tmp_path / "ч.wav", шум=0.001))
    шумная = модуль.оценить_snr(запись(tmp_path / "ш.wav", шум=0.08))
    assert чистая is not None and шумная is not None
    assert чистая > шумная + 10, (чистая, шумная)


# ---------------------------------------------------------------------------
# DeepFilterNet
# ---------------------------------------------------------------------------

@pytest.fixture()
def поддельный_deepfilter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Программа `deep-filter` в объёме, который нам от неё нужен.

    Настоящую здесь не поставить: это бинарь из релизов проекта, не пакет с
    PyPI. Но проверять надо не качество её работы, а то, что сервер зовёт
    её правильно и правильно поступает с тем, что она вернула.
    """
    бинарь = tmp_path / "deep-filter"
    бинарь.write_text('#!/bin/sh\n'
                      '# вызов: deep-filter -o КАТАЛОГ ФАЙЛ\n'
                      'cp "$3" "$2/$(basename "$3")"\n', encoding="utf-8")
    бинарь.chmod(0o755)
    monkeypatch.setenv("ASRHUB_DEEPFILTER", str(бинарь))
    return бинарь


def test_deepfilternet_зовут_отдельным_проходом(tmp_path: Path, поддельный_deepfilter):
    """Модель работает на 48 кГц и в цепочку фильтров ffmpeg не укладывается.

    Питоновский пакет DeepFilterNet собран расширением на Rust, и колёс
    новее Python 3.11 у него нет — рядом с сервером на 3.14 его не
    установить вовсе. Поэтому зовётся готовый бинарь из релизов проекта,
    который ни от какого Python не зависит.
    """
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    assert модуль.deepfilter_путь() == str(поддельный_deepfilter)
    настройки = load().merged({"audio_denoise": "deepfilternet"})
    готово = модуль.prepare(запись(tmp_path / "вход.wav"), tmp_path / "работа", настройки)
    assert len(готово.channels) == 1
    assert готово.warnings == [], готово.warnings
    assert модуль.peak_db(готово.channels[0][1]) > -50.0, "на выходе тишина"


def test_очистка_идёт_на_48_килогерцах(tmp_path: Path, поддельный_deepfilter):
    """Другой частоты модель не понимает: она обучена на 48 кГц.

    Подделка копирует вход в выход, поэтому по её выходу видно, что именно
    ей подали.
    """
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    настройки = load().merged({"audio_denoise": "deepfilternet"})
    очищенный, беда = модуль.очистить_deepfilter(
        запись(tmp_path / "вход.wav", rate=8000), tmp_path / "работа", настройки)
    assert беда == "", беда
    with wave.open(str(очищенный), "rb") as файл:
        assert файл.getframerate() == модуль.DEEPFILTER_ЧАСТОТА
        assert файл.getnchannels() == 1


def test_без_программы_запись_идёт_как_есть(tmp_path: Path, monkeypatch):
    """Отсутствие надстройки — не повод отказывать в распознавании.

    Но и молчать о нём нельзя: человек выбрал шумоподавление и должен
    узнать, что его не было.
    """
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    monkeypatch.setenv("ASRHUB_DEEPFILTER", str(tmp_path / "нет-такой-программы"))
    настройки = load().merged({"audio_denoise": "deepfilternet"})
    готово = модуль.prepare(запись(tmp_path / "вход.wav"), tmp_path / "работа", настройки)
    assert len(готово.channels) == 1
    assert any("deep-filter не найдена" in з for з in готово.warnings), готово.warnings


def test_пустой_ответ_очистки_не_подменяет_запись(tmp_path: Path, monkeypatch):
    """Подать дальше пустой файл — значит заменить разговор на ничто.

    И назвать это обработкой: дальше по конвейеру такая запись выглядела бы
    как молчащая, а виноватой оказалась бы станция.
    """
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    пустышка = tmp_path / "deep-filter"
    пустышка.write_text('#!/bin/sh\n: > "$2/$(basename "$3")"\n', encoding="utf-8")
    пустышка.chmod(0o755)
    monkeypatch.setenv("ASRHUB_DEEPFILTER", str(пустышка))

    исходник = запись(tmp_path / "вход.wav")
    настройки = load().merged({"audio_denoise": "deepfilternet"})
    очищенный, беда = модуль.очистить_deepfilter(исходник, tmp_path / "работа", настройки)
    assert очищенный == исходник, "пустой файл ушёл дальше как расшифровываемый"
    assert "пустой" in беда, беда


def test_сбой_программы_не_роняет_задание(tmp_path: Path, monkeypatch):
    """Надстройка падает — задание доводится до конца без неё."""
    from asrhub.config import load
    from asrhub.pipeline import audio as модуль

    сбойная = tmp_path / "deep-filter"
    сбойная.write_text('#!/bin/sh\nexit 3\n', encoding="utf-8")
    сбойная.chmod(0o755)
    monkeypatch.setenv("ASRHUB_DEEPFILTER", str(сбойная))

    настройки = load().merged({"audio_denoise": "deepfilternet"})
    готово = модуль.prepare(запись(tmp_path / "вход.wav"), tmp_path / "работа", настройки)
    assert len(готово.channels) == 1
    assert any("не отработал" in з for з in готово.warnings), готово.warnings


def test_deepfilternet_есть_в_справочнике_а_demucs_нет():
    """Вместо извлечения вокала — то, для чего измерено улучшение.

    Demucs разделяет стемы музыки на 44,1 кГц: на телефонной речи это
    максимальное расхождение с тем, на чём модель обучалась, и в измерениях
    такая связка ошибку распознавания увеличивает. DeepFilterNet — наоборот:
    9,7 % против 4,4 % на умеренном шуме.
    """
    from asrhub.catalog.params import get_param

    возможные = {в["value"] for в in get_param("audio_denoise").options}
    assert "deepfilternet" in возможные
    assert "demucs" not in возможные
    справка = get_param("audio_denoise").recommendation
    assert "УХУДШИТЬ" in справка and "9,7" in справка, \
        "рекомендация не объясняет, почему шумоподавление опасно"
