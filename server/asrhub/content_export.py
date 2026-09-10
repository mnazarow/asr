"""Выгрузка отчёта по содержанию записей: книга Excel и набор CSV.

Устроено так же, как выгрузка аналитики сервера, и намеренно: человек,
привыкший к одной, открывает вторую и находит те же разделители, ту же
кодировку и те же русские заголовки. Механику файлов — книгу, архив,
ширину столбцов, метку порядка байтов — берём оттуда же, а не пишем
заново: расхождение между двумя выгрузками одного сервера пришлось бы
объяснять, и объяснить его было бы нечем.

Здесь — только описание того, что выгружается: перечень листов, их
заголовки и как достать строки из отчёта.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from .analytics_export import МОМЕНТ, _собрать_csv, _собрать_xlsx
from .insights import ПРИЗНАКИ_ПО_КЛЮЧУ
from .logging_setup import get_logger

log = get_logger("content.export")

#: Столбцы свода: подпись по-русски и поле в ответе. Подписи именно
#: русские — файл уходит руководителю, а не разработчику, и «filler_rate»
#: для него столбец, который сначала надо расшифровать.
СВОД: list[tuple[str, str]] = [
    ("Записей", "records"),
    ("Часов звука", "hours"),
    ("Тональность", "sentiment"),
    ("Отрицательных", "negative"),
    ("Доля отрицательных, %", "negative_share"),
    ("Положительных", "positive"),
    ("Доля положительных, %", "positive_share"),
    ("Нейтральных", "neutral"),
    ("Разворот тональности", "sentiment_shift"),
    ("Записей с тревожными словами", "alert_records"),
    ("Доля тревожных, %", "alert_share"),
    ("Обещаний", "commitments"),
    ("Из них со сроком", "commitments_dated"),
    ("Обещаний без срока", "commitments_open"),
    ("Вопросов", "questions"),
    ("Темп речи, слов/мин", "wpm"),
    ("Доля тишины", "silence_share"),
    ("Перебиваний на запись", "interruptions"),
    ("Долгих пауз на запись", "pauses"),
    ("Самая долгая пауза, с", "longest_pause_s"),
    ("Слова-паразиты, доля", "filler_rate"),
    ("Скрипт разговора, доля", "compliance"),
    ("Говорящих", "speakers"),
    ("Длительность, с", "duration_s"),
    ("Противоречивых (и резко, и тепло)", "mixed"),
    ("Доля речи оператора", "talk_share"),
    ("Самый долгий монолог оператора, с", "monologue_s"),
    ("Записей с монологом дольше 2,5 мин", "long_monologues"),
    ("Самый долгий рассказ клиента, с", "customer_story_s"),
    ("Смен говорящего на запись", "switches"),
    ("Пауза оператора перед ответом, с", "reply_delay_s"),
    ("Наложение речи, с", "overlap_s"),
    ("Заметная тишина (паузы от 3 с), с", "dead_air_s"),
    ("Темп оператора к темпу клиента", "tempo_ratio"),
    ("Возражений клиентов", "objections"),
    ("Возражений без отработки", "objections_unhandled"),
    ("Доля возражений без отработки, %", "objections_unhandled_share"),
]

#: Колонки карточки записи в списках «что послушать».
ЗАПИСЬ: list[tuple[str, str]] = [
    ("Задание", "job_id"),
    ("Файл", "filename"),
    ("Владелец", "owner"),
    ("Метки", "tags"),
    ("Тональность", "sentiment"),
    ("Оценка словом", "sentiment_label"),
    ("Разворот", "sentiment_shift"),
    ("Тревожных упоминаний", "alerts"),
    ("Обещаний", "commitments"),
    ("Из них со сроком", "commitments_dated"),
    ("Перебиваний", "interruptions"),
    ("Доля тишины", "silence_share"),
    ("Скрипт", "compliance"),
    ("Темп, слов/мин", "wpm"),
    ("Доля речи оператора", "talk_share"),
    ("Монолог оператора, с", "monologue_s"),
    ("Рассказ клиента, с", "customer_story_s"),
    ("Пауза перед ответом, с", "reply_delay_s"),
    ("Заметная тишина, с", "dead_air_s"),
    ("Возражений", "objections"),
    ("Возражений без отработки", "objections_unhandled"),
    ("Крупнейшая сумма", "money_max"),
    ("Длительность, с", "media_duration_s"),
]


def _момент(значение: Any) -> Any:
    """Секунды эпохи — в дату. Число в клетке человеку ничего не говорит."""
    try:
        return datetime.fromtimestamp(float(значение)).replace(microsecond=0)
    except (TypeError, ValueError, OSError, OverflowError):
        return значение


def _свод(отчёт: dict[str, Any]) -> list[list[Any]]:
    """Свод и предыдущий период — двумя колонками рядом, чтобы сравнивать."""
    сейчас = отчёт.get("summary") or {}
    раньше = отчёт.get("previous") or {}
    if not сейчас.get("records"):
        return []
    строки = []
    for подпись, ключ in СВОД:
        было = раньше.get(ключ)
        стало = сейчас.get(ключ)
        изменение = None
        if isinstance(было, (int, float)) and isinstance(стало, (int, float)):
            изменение = round(стало - было, 4)
        строки.append([подпись, стало, было, изменение])
    return строки


def _разрез(отчёт: dict[str, Any], имя: str) -> list[list[Any]]:
    данные = ((отчёт.get("breakdowns") or {}).get(имя) or {}).get("items") or []
    return [[г.get("label"), *(г.get(к) for _, к in СВОД)] for г in данные]


def _записи(отчёт: dict[str, Any]) -> list[list[Any]]:
    """Все списки «что послушать» одной таблицей — с колонкой отбора."""
    строки = []
    for отбор in (отчёт.get("highlights") or {}).values():
        for з in отбор.get("items") or []:
            строки.append([отбор.get("title"), _момент(з.get("created_at")),
                           *(з.get(к) for _, к in ЗАПИСЬ)])
    return строки


def _лента(отчёт: dict[str, Any]) -> list[list[Any]]:
    точки = (отчёт.get("timeline") or {}).get("buckets") or []
    return [[_момент(т.get("ts")), т.get("records"), т.get("sentiment"),
             т.get("negative_share"), т.get("alerts"), т.get("compliance"),
             т.get("wpm")] for т in точки if т.get("records")]


def _темы(отчёт: dict[str, Any]) -> list[list[Any]]:
    сдвиги = {т["stem"]: т for т in (отчёт.get("topic_trend") or [])}
    строки = []
    for т in (отчёт.get("topics") or {}).get("items") or []:
        сдвиг = сдвиги.get(т.get("stem")) or {}
        строки.append([т.get("word"), т.get("records"), т.get("share"),
                       т.get("mentions"), т.get("weight"),
                       сдвиг.get("share_before"), сдвиг.get("delta")])
    return строки


def _категории(отчёт: dict[str, Any]) -> list[list[Any]]:
    from .content.categories import ВИДЫ  # noqa: PLC0415

    return [[к.get("label"), ВИДЫ.get(к.get("kind"), к.get("kind")), к.get("records"),
             к.get("share"), к.get("mentions"), к.get("previous"),
             к.get("share_previous"), к.get("delta"), к.get("rule")]
            for к in (отчёт.get("categories") or {}).get("items") or []]


def _драйверы(отчёт: dict[str, Any]) -> list[list[Any]]:
    return [[д.get("label"), д.get("kind_title"), д.get("records"), д.get("negative"),
             д.get("negative_share"), д.get("share"), д.get("share_in_negative"),
             д.get("lift")]
            for д in (отчёт.get("drivers") or {}).get("items") or []]


def _связи(отчёт: dict[str, Any]) -> list[list[Any]]:
    return [[с.get("text"), с.get("x_title"), с.get("y_title"), с.get("r"),
             с.get("strength"), с.get("n")]
            for с in (отчёт.get("correlations") or {}).get("items") or []]


def _выводы(отчёт: dict[str, Any]) -> list[list[Any]]:
    уровни = {"warning": "требует внимания", "good": "хорошо", "info": "к сведению"}
    return [[уровни.get(в.get("severity"), в.get("severity")), в.get("text"),
             ПРИЗНАКИ_ПО_КЛЮЧУ.get(в.get("metric", ""), {}).get("title",
                                                                в.get("metric")),
             в.get("value"), в.get("previous"), в.get("group")]
            for в in отчёт.get("findings") or []]


def _покрытие(отчёт: dict[str, Any]) -> list[list[Any]]:
    с = отчёт.get("coverage") or {}
    if not с:
        return []
    return [["Завершённых записей", с.get("total")],
            ["Разобрано", с.get("analyzed")],
            ["Ожидают разбора", с.get("pending")],
            ["Версия разбора", с.get("version")],
            ["Словарь основ", с.get("vocabulary")],
            ["Период отчёта", отчёт.get("period")],
            [МОМЕНТ(), _момент(отчёт.get("generated_at"))]]


ШАПКА_СВОДА = ["Показатель", "Значение", "Прошлый период", "Изменение"]
ШАПКА_РАЗРЕЗА = ["Группа", *(п for п, _ in СВОД)]

#: Лист выгрузки: ключ, имя листа, заголовки, как достать строки.
#:
#: Имя листа не длиннее 31 знака и без символов `[]:*?/\` — ограничение
#: самого формата: книга с листом «Тональность: по операторам/дням» не
#: открывается вовсе.
ЛИСТЫ: list[tuple[str, list[str], Any]] = [
    ("Свод", ШАПКА_СВОДА, _свод),
    ("Выводы", ["Уровень", "Вывод", "Показатель", "Значение", "Было", "Группа"],
     _выводы),
    ("По владельцам", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "owner")),
    ("По операторам", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "speaker")),
    ("По меткам", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "tag")),
    ("По категориям", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "category")),
    ("По моделям", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "model")),
    ("По источникам", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "source")),
    ("По дням недели", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "weekday")),
    ("По часам", ШАПКА_РАЗРЕЗА, lambda о: _разрез(о, "hour")),
    ("Динамика", [МОМЕНТ(), "Записей", "Тональность", "Доля отрицательных, %",
                  "Тревожных упоминаний", "Скрипт", "Темп, слов/мин"], _лента),
    ("Темы", ["Тема", "Записей", "Доля, %", "Упоминаний", "Вес",
              "Доля в прошлом периоде, %", "Изменение, п.п."], _темы),
    ("Категории", ["Категория", "Вид", "Записей", "Доля, %", "Упоминаний",
                   "Записей в прошлом периоде", "Доля в прошлом периоде, %",
                   "Изменение, п.п.", "Правило"], _категории),
    ("Драйверы негатива", ["Категория", "Вид", "Записей", "Отрицательных",
                           "Отрицательных, %", "Доля вообще, %",
                           "Доля среди отрицательных, %", "Подъём"], _драйверы),
    ("Связи", ["Наблюдение", "Признак", "С чем", "Коэффициент", "Сила", "Записей"],
     _связи),
    ("Что послушать", ["Отбор", МОМЕНТ(), *(п for п, _ in ЗАПИСЬ)], _записи),
    ("О разборе", ["Показатель", "Значение"], _покрытие),
]


def _таблицы(отчёт: dict[str, Any]) -> list[tuple[str, list[str], list[list[Any]]]]:
    """Отчёт — в набор именованных таблиц. Пустые листы пропускаются."""
    out = []
    for имя, заголовки, достать in ЛИСТЫ:
        try:
            строки = достать(отчёт)
        except Exception as exc:                             # noqa: BLE001
            # Один сломавшийся лист не должен уносить всю выгрузку: у
            # человека на руках останется файл без одного листа, а не
            # пятисотая ошибка вместо отчёта.
            log.warning("Лист «%s» не попал в выгрузку: %s", имя, exc)
            continue
        if строки:
            out.append((имя, заголовки, строки))
    return out


def to_csv_zip(отчёт: dict[str, Any], period: str) -> bytes:
    """Архив с файлом CSV на каждый лист."""
    return _собрать_csv(_таблицы(отчёт), period)


def to_xlsx(отчёт: dict[str, Any], period: str) -> bytes:
    """Книга Excel: по листу на раздел отчёта."""
    return _собрать_xlsx(_таблицы(отчёт),
                         f"ASR Hub — аналитика записей за период «{period}»")
