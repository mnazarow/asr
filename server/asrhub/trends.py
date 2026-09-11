"""Тренды: как меняются со временем все измеримые величины сервера.

Разделы аналитики отвечают на вопрос «как дела сейчас» и сравнивают
период с предыдущим одним числом. Вопрос «когда это началось» они не
берут: ряд по времени есть у горстки показателей, и каждый живёт в своей
карточке. А ответ на него нужен чаще всего — «WER пополз вверх» и «WER
пополз вверх после обновления движка двадцать третьего» это разные
сообщения, и второе можно проверить.

Здесь собраны в один ряд **все** величины, которые сервер вообще
измеряет: объём и скорость, качество распознавания и звук на входе,
содержание разговоров и работа оператора, смысловой слой, здоровье
распознавания и загрузка железа. Полсотни показателей на одной оси
времени, с общими корзинами, сравнением с предыдущим периодом,
разложением по часам недели и связями между рядами.

Как это устроено внутри. Показателей полсотни, а запросов к базе — по
одному на источник (задания, содержание, смысловой слой, контрольные
прогоны, замеры, железо): каждый запрос сворачивает своё в корзины
времени и отдаёт сырые суммы, а показатели считаются из этих сумм уже в
питоне. Так добавление показателя не добавляет запроса, а доли считаются
там, где видны и числитель, и знаменатель.

Чего здесь намеренно нет — перцентилей. Их честно считают по всей
выборке разделы аналитики; по корзине в час на десятке заданий
перцентиль это не перцентиль, а случайное число, и рисовать его рядом со
средним значило бы предлагать выводы, которых данные не выдерживают.
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .logging_setup import get_logger

log = get_logger("trends")

#: Периоды, за которые строится ряд, — те же, что у остальной аналитики.
PERIODS: dict[str, float] = {
    "day": 86400.0,
    "week": 7 * 86400.0,
    "month": 30 * 86400.0,
    "quarter": 90 * 86400.0,
    "year": 365 * 86400.0,
}

#: Шаг корзины. «Авто» подбирает такой, чтобы точек было от двадцати до
#: полутора сотен: меньше — ряд не читается, больше — шум вместо линии.
BUCKETS: dict[str, float] = {
    "hour": 3600.0,
    "day": 86400.0,
    "week": 7 * 86400.0,
    "month": 30 * 86400.0,
}

#: Сколько точек считать разумным пределом. Двести пятьдесят — это год по
#: дням с запасом; больше рисовать бессмысленно, ширины экрана не хватит.
МАКС_ТОЧЕК = 250


@dataclass(frozen=True)
class Показатель:
    """Один ряд: откуда берётся, как считается, чем измеряется."""

    id: str
    label: str
    group: str
    source: str
    calc: Callable[[dict[str, Any]], float | None]
    unit: str = ""
    digits: int = 2
    #: Куда «лучше»: вверх, вниз или никуда (объёмы сами по себе не
    #: хорошие и не плохие).
    better: str = ""
    hint: str = ""
    #: Только администратору: показатели железа — это про сервер целиком,
    #: а не про записи ключа.
    admin_only: bool = False


def _доля(числитель: str, знаменатель: str, *, процент: bool = True
          ) -> Callable[[dict[str, Any]], float | None]:
    def считать(строка: dict[str, Any]) -> float | None:
        низ = float(строка.get(знаменатель) or 0)
        if not низ:
            return None
        значение = float(строка.get(числитель) or 0) / низ
        return значение * 100 if процент else значение
    return считать


def _число(ключ: str, множитель: float = 1.0) -> Callable[[dict[str, Any]], float | None]:
    def считать(строка: dict[str, Any]) -> float | None:
        значение = строка.get(ключ)
        return None if значение is None else float(значение) * множитель
    # Пометка «величина складывается». Нужна разложению по часам недели:
    # там в одну клетку попадают все понедельники периода, и сумма растёт
    # вместе с длиной периода. Отношения (доли, средневзвешенные) от длины
    # периода не зависят и делить их не надо.
    считать.суммируемый = True                               # type: ignore[attr-defined]
    return считать


def _средневзвешенно(сумма: str, вес: str) -> Callable[[dict[str, Any]], float | None]:
    def считать(строка: dict[str, Any]) -> float | None:
        в = float(строка.get(вес) or 0)
        return (float(строка.get(сумма) or 0) / в) if в else None
    return считать


# ---------------------------------------------------------------------------
# Источники: по запросу на каждый, все сырые суммы разом
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Источник:
    """Откуда берутся сырые суммы по корзинам времени."""

    id: str
    frm: str
    ts: str
    where: tuple[str, ...] = ()
    owner_alias: str = "j"
    cols: dict[str, str] = field(default_factory=dict)
    admin_only: bool = False


ИСТОЧНИКИ: tuple[Источник, ...] = (
    Источник(
        id="jobs", frm="FROM jobs j", ts="j.created_at",
        cols={
            "jobs": "COUNT(*)",
            "completed": "SUM(CASE WHEN j.status='completed' THEN 1 ELSE 0 END)",
            "failed": "SUM(CASE WHEN j.status='failed' THEN 1 ELSE 0 END)",
            "cancelled": "SUM(CASE WHEN j.status='cancelled' THEN 1 ELSE 0 END)",
            "cached": "SUM(CASE WHEN COALESCE(j.cached_from,'') <> '' THEN 1 ELSE 0 END)",
            "retries": "SUM(COALESCE(j.retries,0))",
            "audio_s": "SUM(COALESCE(j.media_duration_s,0))",
            "proc_s": "SUM(COALESCE(j.processing_time_s,0))",
            # Те же суммы, но ТОЛЬКО по завершённым. Средняя длительность и
            # «быстрее реального времени» делили сумму по всем заданиям
            # корзины на число завершённых: десять заданий, из них пять
            # завершены — и средняя длительность выходила вдвое больше
            # настоящей, а ускорение вдесятеро. Справка при этом честно
            # обещала «считается по завершённым». Перекос тем сильнее, чем
            # больше в корзине незавершённых и упавших.
            "audio_done_s": "SUM(CASE WHEN j.status='completed' "
                            "THEN COALESCE(j.media_duration_s,0) ELSE 0 END)",
            "proc_done_s": "SUM(CASE WHEN j.status='completed' "
                           "THEN COALESCE(j.processing_time_s,0) ELSE 0 END)",
            "words": "SUM(COALESCE(j.words_count,0))",
            "queue_sum": "SUM(COALESCE(j.queue_time_s,0))",
            "queue_n": "SUM(CASE WHEN j.queue_time_s IS NOT NULL THEN 1 ELSE 0 END)",
            "rtf_sum": "SUM(COALESCE(j.rtf,0))",
            "rtf_n": "SUM(CASE WHEN j.rtf IS NOT NULL THEN 1 ELSE 0 END)",
            "conf_sum": "SUM(COALESCE(j.avg_confidence,0))",
            "conf_n": "SUM(CASE WHEN j.avg_confidence IS NOT NULL THEN 1 ELSE 0 END)",
            "low_conf": "SUM(CASE WHEN j.avg_confidence < 0.75 THEN 1 ELSE 0 END)",
            "ref_words": "SUM(COALESCE(j.ref_words,0))",
            "sub_words": "SUM(COALESCE(j.sub_words,0))",
            "del_words": "SUM(COALESCE(j.del_words,0))",
            "ins_words": "SUM(COALESCE(j.ins_words,0))",
            "hit_words": "SUM(COALESCE(j.ref_words,0) - COALESCE(j.sub_words,0) "
                         "- COALESCE(j.del_words,0))",
            "scored": "SUM(CASE WHEN COALESCE(j.ref_words,0) > 0 THEN 1 ELSE 0 END)",
            "suspect": "SUM(CASE WHEN COALESCE(j.quality_flags,'') <> '' THEN 1 ELSE 0 END)",
            "checked": "SUM(CASE WHEN j.quality_flags IS NOT NULL THEN 1 ELSE 0 END)",
            "snr_sum": "SUM(COALESCE(j.snr_db,0))",
            "snr_n": "SUM(CASE WHEN j.snr_db IS NOT NULL THEN 1 ELSE 0 END)",
            "bad_audio": "SUM(CASE WHEN j.snr_db IS NOT NULL AND j.snr_db < 10 THEN 1 ELSE 0 END)",
            "clip_sum": "SUM(COALESCE(j.clipping_share,0))",
            "clip_n": "SUM(CASE WHEN j.clipping_share IS NOT NULL THEN 1 ELSE 0 END)",
            "lufs_sum": "SUM(COALESCE(j.loudness_lufs,0))",
            "lufs_n": "SUM(CASE WHEN j.loudness_lufs IS NOT NULL THEN 1 ELSE 0 END)",
            "speakers_sum": "SUM(COALESCE(j.speakers_count,0))",
            "speakers_n": "SUM(CASE WHEN j.speakers_count IS NOT NULL THEN 1 ELSE 0 END)",
        }),
    Источник(
        id="content", frm="FROM content c JOIN jobs j ON j.id = c.job_id",
        ts="j.created_at",
        cols={
            "records": "COUNT(*)",
            "sent_sum": "SUM(COALESCE(c.sentiment,0))",
            "sent_n": "SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END)",
            "negative": "SUM(CASE WHEN c.sentiment < -0.15 THEN 1 ELSE 0 END)",
            "positive": "SUM(CASE WHEN c.sentiment > 0.15 THEN 1 ELSE 0 END)",
            "alerts": "SUM(COALESCE(c.alerts,0))",
            "alert_records": "SUM(CASE WHEN COALESCE(c.alerts,0) > 0 THEN 1 ELSE 0 END)",
            "score_sum": "SUM(COALESCE(c.agent_score,0))",
            "score_n": "SUM(CASE WHEN c.agent_score IS NOT NULL THEN 1 ELSE 0 END)",
            "empathy_sum": "SUM(COALESCE(c.empathy,0))",
            "empathy_n": "SUM(CASE WHEN c.empathy IS NOT NULL THEN 1 ELSE 0 END)",
            "compliance_sum": "SUM(COALESCE(c.compliance,0))",
            "compliance_n": "SUM(CASE WHEN c.compliance IS NOT NULL THEN 1 ELSE 0 END)",
            "wpm_sum": "SUM(COALESCE(c.wpm,0))",
            "wpm_n": "SUM(CASE WHEN c.wpm IS NOT NULL THEN 1 ELSE 0 END)",
            "silence_sum": "SUM(COALESCE(c.silence_share,0))",
            "silence_n": "SUM(CASE WHEN c.silence_share IS NOT NULL THEN 1 ELSE 0 END)",
            "interrupt_sum": "SUM(COALESCE(c.interruptions,0))",
            "interrupt_n": "SUM(CASE WHEN c.interruptions IS NOT NULL THEN 1 ELSE 0 END)",
            "talk_sum": "SUM(COALESCE(c.talk_share,0))",
            "talk_n": "SUM(CASE WHEN c.talk_share IS NOT NULL THEN 1 ELSE 0 END)",
            "monolog_sum": "SUM(COALESCE(c.monologue_s,0))",
            "monolog_n": "SUM(CASE WHEN c.monologue_s IS NOT NULL THEN 1 ELSE 0 END)",
            "reply_sum": "SUM(COALESCE(c.reply_delay_s,0))",
            "reply_n": "SUM(CASE WHEN c.reply_delay_s IS NOT NULL THEN 1 ELSE 0 END)",
            "questions": "SUM(COALESCE(c.questions,0))",
            "commitments": "SUM(COALESCE(c.commitments,0))",
            "commitments_dated": "SUM(COALESCE(c.commitments_dated,0))",
            "objections": "SUM(COALESCE(c.objections,0))",
            "objections_unhandled": "SUM(COALESCE(c.objections_unhandled,0))",
            "violations": "SUM(CASE WHEN COALESCE(c.violations,0) > 0 THEN 1 ELSE 0 END)",
            "frustrated": "SUM(CASE WHEN COALESCE(c.frustration,0) > 0 THEN 1 ELSE 0 END)",
            "repeat": "SUM(CASE WHEN COALESCE(c.repeat_contact,0) > 0 THEN 1 ELSE 0 END)",
            "profanity": "SUM(CASE WHEN COALESCE(c.profanity,0) > 0 THEN 1 ELSE 0 END)",
            "profanity_checked": "SUM(CASE WHEN c.profanity IS NOT NULL THEN 1 ELSE 0 END)",
            "mono": "SUM(CASE WHEN COALESCE(c.speakers,0) < 2 THEN 1 ELSE 0 END)",
        }),
    Источник(
        id="llm", frm="FROM llm_results l JOIN jobs j ON j.id = l.job_id",
        ts="j.created_at",
        cols={
            "analyzed": "SUM(CASE WHEN COALESCE(l.error,'')='' THEN 1 ELSE 0 END)",
            "errors": "SUM(CASE WHEN COALESCE(l.error,'')<>'' THEN 1 ELSE 0 END)",
            "resolved": "SUM(CASE WHEN l.resolved=1 THEN 1 ELSE 0 END)",
            "unresolved": "SUM(CASE WHEN l.resolved=0 THEN 1 ELSE 0 END)",
            "with_actions": "SUM(CASE WHEN COALESCE(l.actions,'[]') NOT IN ('[]','') "
                            "THEN 1 ELSE 0 END)",
            "lat_sum": "SUM(COALESCE(l.latency_ms,0))",
            "lat_n": "SUM(CASE WHEN l.latency_ms IS NOT NULL THEN 1 ELSE 0 END)",
            "calls": "SUM(COALESCE(l.calls,0))",
            "off_list": "SUM(CASE WHEN COALESCE(l.warnings,'') LIKE '%вне списка%' "
                        "THEN 1 ELSE 0 END)",
        }),
    Источник(
        id="checks", frm="FROM model_checks m JOIN jobs j ON j.id = m.job_id",
        ts="m.created_at",
        cols={
            "checks": "COUNT(*)",
            "wer_sum": "SUM(COALESCE(m.wer,0))",
            "wer_n": "SUM(CASE WHEN m.wer IS NOT NULL THEN 1 ELSE 0 END)",
        }),
    Источник(
        id="system", frm="FROM system_samples s", ts="s.ts", owner_alias="",
        admin_only=True,
        cols={
            "n": "COUNT(*)",
            "cpu_sum": "SUM(COALESCE(s.cpu_percent,0))",
            "ram_sum": "SUM(COALESCE(s.ram_used_mb,0))",
            "ram_total": "SUM(COALESCE(s.ram_total_mb,0))",
            "disk_sum": "SUM(COALESCE(s.disk_free_gb,0))",
        }),
    Источник(
        id="gpu", frm="FROM gpu_samples g", ts="g.ts", owner_alias="",
        admin_only=True,
        cols={
            "n": "COUNT(*)",
            "util_sum": "SUM(COALESCE(g.util_percent,0))",
            "mem_sum": "SUM(COALESCE(g.mem_used_mb,0))",
            "temp_sum": "SUM(COALESCE(g.temperature_c,0))",
            "power_sum": "SUM(COALESCE(g.power_w,0))",
        }),
)

ИСТОЧНИКИ_ПО_ID = {и.id: и for и in ИСТОЧНИКИ}


# ---------------------------------------------------------------------------
# Каталог показателей
# ---------------------------------------------------------------------------

def _показатели() -> tuple[Показатель, ...]:
    П = Показатель
    return (
        # --- объём ---------------------------------------------------------
        П("jobs", "Заданий", "объём", "jobs", _число("jobs"), "шт", 0,
          hint="Сколько записей пришло на сервер за корзину времени."),
        П("completed", "Завершено", "объём", "jobs", _число("completed"), "шт", 0,
          hint="Задания, доведённые до расшифровки. Разница с «Заданий» — то, что "
               "отвалилось или ещё в работе."),
        П("failed", "С ошибкой", "объём", "jobs", _число("failed"), "шт", 0, "down",
          hint="Задания, которые сервер не смог довести до конца."),
        П("audio_hours", "Часов аудио", "объём", "jobs", _число("audio_s", 1 / 3600), "ч", 2,
          hint="Суммарная длительность звука, а не время работы сервера: два часа "
               "записи могут обработаться за десять минут."),
        П("words", "Слов распознано", "объём", "jobs", _число("words"), "шт", 0,
          hint="Слов в расшифровках. Растёт и от объёма, и от разговорчивости — "
               "сам по себе ни о чём не говорит, но резкий провал означает, что "
               "распознавание молчит."),
        П("cached_share", "Из кеша", "объём", "jobs", _доля("cached", "jobs"), "%", 1,
          hint="Доля заданий, отданных из кеша: тот же файл с теми же настройками."),
        П("avg_duration", "Средняя длительность", "объём", "jobs",
          _средневзвешенно("audio_done_s", "completed"), "с", 0,
          hint="Средняя длина записи. Считается по завершённым: незавершённые ещё "
               "не знают своей длительности."),

        # --- скорость ------------------------------------------------------
        П("rtf", "RTF", "скорость", "jobs", _средневзвешенно("rtf_sum", "rtf_n"), "", 3, "down",
          hint="Секунда обработки на секунду звука. Меньше — быстрее."),
        П("speedup", "Быстрее реального времени", "скорость", "jobs",
          lambda с: (float(с.get("audio_done_s") or 0) / float(с["proc_done_s"]))
          if float(с.get("proc_done_s") or 0) else None, "×", 1, "up",
          hint="Во сколько раз обработка быстрее реального времени. Единица — "
               "сервер едва успевает за потоком."),
        П("queue_wait", "Ожидание в очереди", "скорость", "jobs",
          _средневзвешенно("queue_sum", "queue_n"), "с", 1, "down",
          hint="Сколько задание пролежало в очереди до начала работы. Это то, что "
               "чувствует человек, ждущий расшифровку."),
        П("processing_hours", "Часов работы", "скорость", "jobs",
          _число("proc_s", 1 / 3600), "ч", 2,
          hint="Часы, которые сервер потратил на распознавание. Вместе с «Часов "
               "аудио» показывает запас мощности."),
        П("retries", "Повторов", "скорость", "jobs", _число("retries"), "шт", 0, "down",
          hint="Повторные попытки после сбоя. Ноль — норма; постоянный ненулевой "
               "уровень означает, что что-то ломается регулярно."),
        П("success_rate", "Доля успешных", "скорость", "jobs",
          _доля("completed", "jobs"), "%", 1, "up",
          hint="Доля заданий, дошедших до конца. Считается от всех пришедших за "
               "корзину, включая ещё не завершённые, — на свежей корзине занижена."),

        # --- качество распознавания ----------------------------------------
        П("confidence", "Уверенность", "качество", "jobs",
          _средневзвешенно("conf_sum", "conf_n"), "", 3, "up",
          hint="Средняя уверенность модели, взвешенная по числу записей. Само по "
               "себе число ничего не значит — важно, как оно меняется."),
        П("low_confidence", "Записей с низкой уверенностью", "качество", "jobs",
          _доля("low_conf", "conf_n"), "%", 1, "down",
          hint="Записи, где модель сомневалась. Обычно это плохой звук, а не "
               "плохая модель."),
        П("wer", "WER по эталонам", "качество", "jobs",
          lambda с: ((float(с.get("sub_words") or 0) + float(с.get("del_words") or 0)
                      + float(с.get("ins_words") or 0)) / float(с["ref_words"]))
          if float(с.get("ref_words") or 0) else None, "", 4, "down",
          hint="Складывается по словам всех записей корзины, а не усредняется по записям."),
        П("mer", "MER по эталонам", "качество", "jobs",
          lambda с: ((float(с.get("sub_words") or 0) + float(с.get("del_words") or 0)
                      + float(с.get("ins_words") or 0))
                     / max(1.0, float(с.get("sub_words") or 0) + float(с.get("del_words") or 0)
                           + float(с.get("ins_words") or 0) + max(0.0, float(с.get("hit_words") or 0))))
          if float(с.get("ref_words") or 0) else None, "", 4, "down",
          hint="Доля ошибок среди всех сопоставленных слов. В отличие от WER не "
               "превышает единицы, поэтому читается как процент."),
        П("scored_records", "Записей с эталоном", "качество", "jobs", _число("scored"), "шт", 0,
          hint="Записи, для которых есть эталонный текст. От их числа зависит, "
               "насколько можно верить WER рядом."),
        П("suspect_share", "Подозрительных расшифровок", "качество", "jobs",
          _доля("suspect", "checked"), "%", 1, "down",
          hint="Расшифровки с признаками выдуманного текста: повторы, "
               "зацикливания, текст на тишине."),
        П("speakers", "Говорящих на записи", "качество", "jobs",
          _средневзвешенно("speakers_sum", "speakers_n"), "", 2,
          hint="Среднее число говорящих на записи. Устойчивая единица там, где "
               "ждали двоих, — разделение не работает."),

        # --- звук на входе --------------------------------------------------
        П("snr", "SNR", "звук", "jobs", _средневзвешенно("snr_sum", "snr_n"), "дБ", 1, "up",
          hint="Отношение сигнал/шум по речевым кадрам. Ниже 10 дБ — плохо."),
        П("bad_audio", "Доля плохого звука", "звук", "jobs",
          _доля("bad_audio", "snr_n"), "%", 1, "down",
          hint="Доля записей с отношением сигнал/шум ниже порога. Это про источник "
               "звука, а не про распознавание."),
        П("clipping", "Клиппинг", "звук", "jobs",
          lambda с: (float(с.get("clip_sum") or 0) / float(с["clip_n"]) * 100)
          if float(с.get("clip_n") or 0) else None, "%", 3, "down",
          hint="Доля отсчётов, упёршихся в предел. Даже доли процента слышны как "
               "хрип и сбивают распознавание."),
        П("loudness", "Громкость", "звук", "jobs",
          _средневзвешенно("lufs_sum", "lufs_n"), "LUFS", 1,
          hint="Интегральная громкость. Уход от привычного уровня означает, что на "
               "линии что-то поменяли."),

        # --- содержание разговоров ------------------------------------------
        П("sentiment", "Тональность", "содержание", "content",
          _средневзвешенно("sent_sum", "sent_n"), "", 3, "up",
          hint="Средняя тональность разговоров: от −1 до +1. Смотреть надо на "
               "изменение, а не на само число."),
        П("negative_share", "Отрицательных разговоров", "содержание", "content",
          _доля("negative", "sent_n"), "%", 1, "down",
          hint="Доля разговоров, признанных отрицательными. Всплеск обычно "
               "совпадает с чем-то внешним — сбоем, ценами, рассылкой."),
        П("positive_share", "Положительных разговоров", "содержание", "content",
          _доля("positive", "sent_n"), "%", 1, "up",
          hint="Доля разговоров, признанных положительными."),
        П("alert_records", "Записей с сигналами", "содержание", "content",
          _доля("alert_records", "records"), "%", 1, "down",
          hint="Записи, где сработал хотя бы один сигнал: угроза уйти, упоминание "
               "конкурента, обещание без срока."),
        П("questions", "Вопросов", "содержание", "content", _число("questions"), "шт", 0,
          hint="Вопросов в разговорах. Много вопросов от клиента — обычно "
               "непонятная услуга или инструкция."),
        П("commitments", "Обязательств", "содержание", "content",
          _число("commitments"), "шт", 0,
          hint="Обещаний, найденных в разговорах: «перезвоню», «отправлю», "
               "«сделаем»."),
        П("commitments_dated", "Со сроком", "содержание", "content",
          _доля("commitments_dated", "commitments"), "%", 1, "up",
          hint="Доля обещаний с названным сроком. Обещание без срока не "
               "проверяется и потому чаще не выполняется."),
        П("objections", "Возражений", "содержание", "content", _число("objections"), "шт", 0,
          hint="Возражений клиентов. Само по себе не плохо: возражают там, где "
               "ведут разговор, а не зачитывают текст."),
        П("objections_unhandled", "Возражений без отработки", "содержание", "content",
          _доля("objections_unhandled", "objections"), "%", 1, "down",
          hint="Доля возражений, оставшихся без ответа. Это и есть цена обучения "
               "операторов."),
        П("frustrated", "Записей с раздражением", "содержание", "content",
          _доля("frustrated", "records"), "%", 1, "down",
          hint="Записи с признаками раздражения: повышенный тон, перебивания, "
               "повторные требования."),
        П("repeat", "Повторных обращений", "содержание", "content",
          _доля("repeat", "records"), "%", 1, "down",
          hint="Доля повторных обращений: человек звонит второй раз по тому же "
               "поводу. Прямая мера того, решают ли вопрос с первого раза."),
        П("profanity", "Записей с бранью", "содержание", "content",
          _доля("profanity", "profanity_checked"), "%", 1, "down",
          hint="Записи с бранью. Считается только по тем, где проверка вообще "
               "применима."),
        П("mono_share", "Записей без разделения", "содержание", "content",
          _доля("mono", "records"), "%", 1, "down",
          hint="Говорящий один: показатели диалога на таких записях не считаются."),

        # --- работа оператора -----------------------------------------------
        П("agent_score", "Балл оператора", "оператор", "content",
          _средневзвешенно("score_sum", "score_n"), "балл", 1, "up",
          hint="Сводный балл работы оператора по разбору разговора. Это оценка "
               "разговора, а не человека."),
        П("empathy", "Эмпатия", "оператор", "content",
          _средневзвешенно("empathy_sum", "empathy_n"), "", 2, "up",
          hint="Признаки внимания к собеседнику: обращение по имени, отклик на "
               "сказанное, отсутствие перебиваний."),
        П("compliance", "Выполнение скрипта", "оператор", "content",
          lambda с: (float(с.get("compliance_sum") or 0) / float(с["compliance_n"]) * 100)
          if float(с.get("compliance_n") or 0) else None, "%", 1, "up",
          hint="Насколько разговор следовал скрипту: приветствие, представление, "
               "проверка, прощание."),
        П("violations", "Записей с нарушениями", "оператор", "content",
          _доля("violations", "records"), "%", 1, "down",
          hint="Записи, где нарушены обязательные правила разговора."),
        П("wpm", "Темп речи", "оператор", "content",
          _средневзвешенно("wpm_sum", "wpm_n"), "сл/мин", 0,
          hint="Слов в минуту. Выше 180 — быстро для телефона; собеседник "
               "перестаёт успевать."),
        П("talk_share", "Доля речи оператора", "оператор", "content",
          lambda с: (float(с.get("talk_sum") or 0) / float(с["talk_n"]) * 100)
          if float(с.get("talk_n") or 0) else None, "%", 1,
          hint="Доля времени, которую говорил оператор. Устойчивые 80 % означают "
               "монолог, а не разговор."),
        П("silence", "Тишина", "оператор", "content",
          lambda с: (float(с.get("silence_sum") or 0) / float(с["silence_n"]) * 100)
          if float(с.get("silence_n") or 0) else None, "%", 1, "down",
          hint="Доля тишины в разговоре. Растёт, когда оператор ищет ответ в "
               "системе."),
        П("interruptions", "Перебиваний на запись", "оператор", "content",
          _средневзвешенно("interrupt_sum", "interrupt_n"), "шт", 2, "down",
          hint="Сколько раз собеседники перебивали друг друга, в среднем на "
               "запись."),
        П("monologue", "Самый долгий монолог", "оператор", "content",
          _средневзвешенно("monolog_sum", "monolog_n"), "с", 0, "down",
          hint="Самая длинная непрерывная реплика. Полторы минуты без паузы "
               "собеседник уже не слушает."),
        П("reply_delay", "Задержка ответа", "оператор", "content",
          _средневзвешенно("reply_sum", "reply_n"), "с", 2, "down",
          hint="Пауза между репликой собеседника и ответом оператора."),

        # --- смысловой слой --------------------------------------------------
        П("llm_analyzed", "Разобрано моделью", "смысл", "llm", _число("analyzed"), "шт", 0,
          hint="Записей, разобранных языковой моделью. Меньше числа завершённых — "
               "значит, слой не успевает или выключен."),
        П("llm_resolved", "Вопрос решён", "смысл", "llm",
          lambda с: (float(с.get("resolved") or 0)
                     / (float(с.get("resolved") or 0) + float(с.get("unresolved") or 0)) * 100)
          if (float(с.get("resolved") or 0) + float(с.get("unresolved") or 0)) else None,
          "%", 1, "up",
          hint="Доля разговоров, где модель сочла вопрос решённым, среди тех, по "
               "которым она вынесла суждение."),
        П("llm_actions", "Записей с действиями", "смысл", "llm",
          _доля("with_actions", "analyzed"), "%", 1,
          hint="Записи, из которых модель вынесла хотя бы одно действие к "
               "исполнению."),
        П("llm_latency", "Ответ модели", "смысл", "llm",
          lambda с: (float(с.get("lat_sum") or 0) / float(с["lat_n"]) / 1000)
          if float(с.get("lat_n") or 0) else None, "с", 1, "down",
          hint="Среднее время ответа модели. Растёт, когда карта занята "
               "распознаванием."),
        П("llm_off_list", "Ответов мимо списка", "смысл", "llm",
          _доля("off_list", "analyzed"), "%", 1, "down",
          hint="Ответы, не попавшие в закрытый список причин и исходов. Растёт — "
               "значит, список разошёлся с жизнью."),
        П("llm_errors", "Сбоев модели", "смысл", "llm", _число("errors"), "шт", 0, "down",
          hint="Сбои обращения к модели: недоступна, перегружена, ответила не тем."),

        # --- контрольные прогоны ---------------------------------------------
        П("disagreement", "Расхождение моделей", "здоровье", "checks",
          _средневзвешенно("wer_sum", "wer_n"), "", 4, "down",
          hint="WER контрольной расшифровки относительно исходной: согласие "
               "двух независимых распознавателей."),
        П("control_runs", "Контрольных прогонов", "здоровье", "checks",
          _число("checks"), "шт", 0,
          hint="Сколько записей прошли повторное распознавание второй моделью."),

        # --- железо ----------------------------------------------------------
        П("cpu", "Загрузка процессора", "железо", "system",
          _средневзвешенно("cpu_sum", "n"), "%", 1, admin_only=True,
          hint="Средняя загрузка процессора по замерам корзины."),
        П("ram", "Занято памяти", "железо", "system",
          lambda с: (float(с.get("ram_sum") or 0) / float(с["ram_total"]) * 100)
          if float(с.get("ram_total") or 0) else None, "%", 1, admin_only=True,
          hint="Доля занятой оперативной памяти."),
        П("disk_free", "Свободно на диске", "железо", "system",
          _средневзвешенно("disk_sum", "n"), "ГБ", 1, "up", admin_only=True,
          hint="Свободное место на диске данных. Падение до нуля останавливает "
               "приём записей."),
        П("gpu_util", "Загрузка видеокарты", "железо", "gpu",
          _средневзвешенно("util_sum", "n"), "%", 1, admin_only=True,
          hint="Средняя загрузка видеокарты. Устойчивые 100 % — карта и есть узкое "
               "место."),
        П("gpu_mem", "Видеопамять занята", "железо", "gpu",
          lambda с: (float(с.get("mem_sum") or 0) / float(с["n"]) / 1024)
          if float(с.get("n") or 0) else None, "ГБ", 1, admin_only=True,
          hint="Занятая видеопамять. Вместе со смысловым слоем именно она "
               "кончается первой."),
        П("gpu_temp", "Температура видеокарты", "железо", "gpu",
          _средневзвешенно("temp_sum", "n"), "°C", 1, "down", admin_only=True,
          hint="Температура видеокарты. Около предела карта сбрасывает частоты, и "
               "RTF растёт сам собой."),
        П("gpu_power", "Потребление видеокарты", "железо", "gpu",
          _средневзвешенно("power_sum", "n"), "Вт", 0, admin_only=True,
          hint="Потребление видеокарты. Упор в предел мощности выглядит как "
               "необъяснимое замедление."),
    )


ПОКАЗАТЕЛИ: tuple[Показатель, ...] = _показатели()
ПО_ID: dict[str, Показатель] = {п.id: п for п in ПОКАЗАТЕЛИ}

#: Порядок групп в интерфейсе — от «сколько» к «как» и «на чём».
ГРУППЫ: tuple[tuple[str, str], ...] = (
    ("объём", "Объём"),
    ("скорость", "Скорость и очередь"),
    ("качество", "Качество распознавания"),
    ("звук", "Звук на входе"),
    ("содержание", "Содержание разговоров"),
    ("оператор", "Работа оператора"),
    ("смысл", "Смысловой слой"),
    ("здоровье", "Здоровье распознавания"),
    ("железо", "Железо"),
)


# ---------------------------------------------------------------------------
# Счёт
# ---------------------------------------------------------------------------

def выбрать_шаг(секунд: float, bucket: str = "auto") -> tuple[str, float]:
    """Шаг корзины: заданный или подобранный под длину окна."""
    if bucket in BUCKETS:
        шаг = BUCKETS[bucket]
        if секунд / шаг > МАКС_ТОЧЕК:
            # Запрошенный шаг даёт больше точек, чем можно нарисовать.
            # Молча рисовать тысячу — значит рисовать шум; берём следующий.
            for имя, значение in BUCKETS.items():
                if секунд / значение <= МАКС_ТОЧЕК:
                    return имя, значение
        return bucket, шаг
    for имя, значение in BUCKETS.items():
        точек = секунд / значение
        if 20 <= точек <= МАКС_ТОЧЕК:
            return имя, значение
    return ("day", BUCKETS["day"]) if секунд > 3 * 86400 else ("hour", BUCKETS["hour"])


class Trends:
    """Ряды по времени: считает, сравнивает и раскладывает по часам."""

    def __init__(self, db: Any):
        self.db = db

    # --- сырьё ------------------------------------------------------------

    def _сырьё(self, источник: Источник, since: float, until: float, шаг: float,
               owner: str | list[str] | None) -> dict[int, dict[str, Any]]:
        """Суммы источника по корзинам: {номер корзины: {колонка: сумма}}."""
        колонки = ", ".join(f"{выр} AS {имя}" for имя, выр in источник.cols.items())
        where = [f"{источник.ts}>=?", f"{источник.ts}<?"]
        args: list[Any] = [since, until]
        if owner and источник.owner_alias:
            отбор, свои = self.db._owner_clause(owner, источник.owner_alias)
            if отбор:
                where.append(отбор)
                args.extend(свои)
        запрос = (f"SELECT CAST(({источник.ts}-?)/? AS INTEGER) AS bucket, {колонки} "
                  f"{источник.frm} WHERE {' AND '.join(where)} "
                  "GROUP BY bucket ORDER BY bucket")
        try:
            rows = self.db.query(запрос, [since, шаг, *args])
        except Exception as exc:                             # noqa: BLE001
            # Источника может не быть на старой базе (таблица появилась
            # позже) — ряд просто пустой, а не отчёт целиком с ошибкой.
            log.debug("Источник трендов «%s» не прочитан: %s", источник.id, exc)
            return {}
        return {int(r["bucket"]): dict(r) for r in rows}

    def _ряды(self, показатели: list[Показатель], since: float, until: float,
              шаг: float, корзин: int, owner: str | list[str] | None
              ) -> dict[str, list[float | None]]:
        нужные = {п.source for п in показатели}
        сырьё = {и: self._сырьё(ИСТОЧНИКИ_ПО_ID[и], since, until, шаг, owner)
                 for и in нужные if и in ИСТОЧНИКИ_ПО_ID}
        out: dict[str, list[float | None]] = {}
        for п in показатели:
            строки = сырьё.get(п.source, {})
            ряд: list[float | None] = []
            for i in range(корзин):
                строка = строки.get(i)
                if строка is None:
                    ряд.append(None)
                    continue
                try:
                    значение = п.calc(строка)
                except (TypeError, ValueError, ZeroDivisionError):
                    значение = None
                ряд.append(None if значение is None else round(float(значение), п.digits + 2))
            out[п.id] = ряд
        return out

    # --- наружу -----------------------------------------------------------

    def catalog(self, *, is_admin: bool = False) -> dict[str, Any]:
        """Каталог показателей: что вообще можно построить."""
        return {
            "groups": [{"id": и, "title": т} for и, т in ГРУППЫ],
            "buckets": [{"id": "auto", "title": "авто"},
                        *({"id": и, "title": т} for и, т in
                          (("hour", "час"), ("day", "сутки"),
                           ("week", "неделя"), ("month", "месяц")))],
            "periods": list(PERIODS),
            "metrics": [
                {"id": п.id, "label": п.label, "group": п.group, "unit": п.unit,
                 "digits": п.digits, "better": п.better, "hint": п.hint}
                for п in ПОКАЗАТЕЛИ if is_admin or not п.admin_only],
        }

    def series(self, period: str = "month", *, bucket: str = "auto",
               metrics: list[str] | None = None,
               owner: str | list[str] | None = None,
               compare: bool = True, is_admin: bool = False) -> dict[str, Any]:
        """Ряды выбранных показателей и свод по каждому.

        Свод — не украшение: ряд отвечает «когда», а свод отвечает «на
        сколько» и «в какую сторону». Наклон считается по методу
        наименьших квадратов на самих корзинах: последняя точка бывает
        случайной, а наклон — нет.
        """
        секунд = PERIODS.get(period, PERIODS["month"])
        имя_шага, шаг = выбрать_шаг(секунд, bucket)
        конец = time.time()
        начало = конец - секунд
        корзин = max(1, min(МАКС_ТОЧЕК, int(math.ceil(секунд / шаг))))

        выбранные = [ПО_ID[м] for м in (metrics or []) if м in ПО_ID]
        if not выбранные:
            выбранные = list(ПОКАЗАТЕЛИ)
        if not is_admin:
            выбранные = [п for п in выбранные if not п.admin_only]

        ряды = self._ряды(выбранные, начало, конец, шаг, корзин, owner)
        прошлые: dict[str, list[float | None]] = {}
        if compare:
            прошлые = self._ряды(выбранные, начало - секунд, начало, шаг, корзин, owner)

        точки_времени = [round(начало + i * шаг, 1) for i in range(корзин)]
        свод = []
        for п in выбранные:
            значения = [v for v in ряды.get(п.id, []) if v is not None]
            прошлое = [v for v in прошлые.get(п.id, []) if v is not None]
            сейчас = _среднее(значения)
            было = _среднее(прошлое)
            изменение = None
            if сейчас is not None and было not in (None, 0):
                изменение = round((сейчас - было) / abs(было) * 100, 1)
            свод.append({
                "id": п.id, "label": п.label, "group": п.group, "unit": п.unit,
                "digits": п.digits, "better": п.better, "hint": п.hint,
                "points": len(значения),
                "avg": _округлить(сейчас, п.digits),
                "min": _округлить(min(значения), п.digits) if значения else None,
                "max": _округлить(max(значения), п.digits) if значения else None,
                "last": _округлить(значения[-1], п.digits) if значения else None,
                "total": _округлить(sum(значения), п.digits) if значения else None,
                "previous": _округлить(было, п.digits),
                "change_percent": изменение,
                "slope": _наклон(ряды.get(п.id, [])),
                "verdict": _вердикт(изменение, п.better),
            })
        return {
            "period": period, "bucket": имя_шага, "step_s": шаг,
            "from": round(начало, 1), "to": round(конец, 1),
            "buckets": точки_времени,
            "series": [{"id": п.id, "label": п.label, "unit": п.unit,
                        "digits": п.digits, "better": п.better, "group": п.group,
                        "values": ряды.get(п.id, []),
                        "previous": прошлые.get(п.id, []) if compare else []}
                       for п in выбранные],
            "summary": свод,
        }

    def heatmap(self, metric: str, period: str = "month", *,
                owner: str | list[str] | None = None,
                is_admin: bool = False) -> dict[str, Any]:
        """Разложение показателя по часам недели.

        Средние по периоду прячут то, что видно только здесь: очередь
        растёт не «вообще», а в понедельник с девяти до одиннадцати, и
        плохой звук приходит не «иногда», а вечером с одной площадки.
        """
        п = ПО_ID.get(metric)
        if п is None or (п.admin_only and not is_admin):
            raise KeyError(metric)
        секунд = PERIODS.get(period, PERIODS["month"])
        конец = time.time()
        начало = конец - секунд
        источник = ИСТОЧНИКИ_ПО_ID[п.source]
        колонки = ", ".join(f"{выр} AS {имя}" for имя, выр in источник.cols.items())
        where = [f"{источник.ts}>=?", f"{источник.ts}<?"]
        args: list[Any] = [начало, конец]
        if owner and источник.owner_alias:
            отбор, свои = self.db._owner_clause(owner, источник.owner_alias)
            if отбор:
                where.append(отбор)
                args.extend(свои)
        # День недели и час — средствами SQLite: localtime, чтобы «девять
        # утра» было девятью утра там, где стоит сервер.
        запрос = (
            f"SELECT CAST(strftime('%w', {источник.ts}, 'unixepoch', 'localtime') AS INTEGER) AS дн, "
            f"       CAST(strftime('%H', {источник.ts}, 'unixepoch', 'localtime') AS INTEGER) AS час, "
            f"       {колонки} {источник.frm} WHERE {' AND '.join(where)} "
            "GROUP BY дн, час")
        try:
            rows = self.db.query(запрос, args)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Разложение по часам не посчитано: %s", exc)
            rows = []
        сетка = [[None] * 24 for _ in range(7)]
        for r in rows:
            try:
                значение = п.calc(dict(r))
            except (TypeError, ValueError, ZeroDivisionError):
                значение = None
            # В SQLite воскресенье — ноль; в интерфейсе неделя начинается
            # с понедельника, поэтому сдвигаем.
            день = (int(r["дн"]) + 6) % 7
            сетка[день][int(r["час"])] = (None if значение is None
                                          else round(float(значение), п.digits + 2))
        # Счётные показатели делим на число недель в периоде: иначе одна и
        # та же клетка «понедельник, десять утра» показывает 3 за неделю,
        # 15 за месяц и 27 за квартал — то есть отвечает на вопрос про
        # длину периода, а не про час недели. Подпись под картой («от X до
        # Y, среднее Z») подаёт это как значение показателя.
        суммируемый = bool(getattr(п.calc, "суммируемый", False))
        недель = max(1.0, секунд / (7 * 86400.0)) if суммируемый else 1.0
        if недель > 1.0:
            for строка in сетка:
                for i, v in enumerate(строка):
                    if v is not None:
                        строка[i] = round(v / недель, п.digits + 2)
        плоско = [v for строка in сетка for v in строка if v is not None]
        return {
            "metric": п.id, "label": п.label, "unit": п.unit, "digits": п.digits,
            "better": п.better, "period": period,
            # Клетка счётного показателя — среднее за такой час недели, а не
            # сумма по периоду. Интерфейс подписывает карту этим полем.
            "per": "в среднем за час недели" if суммируемый else "среднее по замерам",
            "weeks": round(недель, 2),
            "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "grid": сетка,
            "min": _округлить(min(плоско), п.digits) if плоско else None,
            "max": _округлить(max(плоско), п.digits) if плоско else None,
            "avg": _округлить(_среднее(плоско), п.digits),
        }

    def correlations(self, period: str = "month", *, bucket: str = "auto",
                     metrics: list[str] | None = None,
                     owner: str | list[str] | None = None,
                     limit: int = 12, is_admin: bool = False) -> dict[str, Any]:
        """Какие ряды движутся вместе.

        Связь — не причина, и раздел этого не обещает: он показывает пары,
        которые стоит посмотреть глазами. Пары считаются только там, где
        у обоих рядов есть хотя бы восемь общих непустых корзин, — иначе
        совпадение двух точек выглядит как закон природы.
        """
        свод = self.series(period, bucket=bucket, metrics=metrics, owner=owner,
                           compare=False, is_admin=is_admin)
        ряды = {с["id"]: с["values"] for с in свод["series"]}
        подписи = {с["id"]: с["label"] for с in свод["series"]}
        пары = []
        ключи = list(ряды)
        for i, а in enumerate(ключи):
            for б in ключи[i + 1:]:
                r, n = _корреляция(ряды[а], ряды[б])
                if r is None or n < 8:
                    continue
                пары.append({"a": а, "b": б, "a_label": подписи[а], "b_label": подписи[б],
                             "r": round(r, 3), "points": n})
        пары.sort(key=lambda п: -abs(п["r"]))
        return {"period": свод["period"], "bucket": свод["bucket"],
                "pairs": пары[:max(1, int(limit))]}


# ---------------------------------------------------------------------------
# Мелкая арифметика
# ---------------------------------------------------------------------------

def _среднее(значения: list[float]) -> float | None:
    return (sum(значения) / len(значения)) if значения else None


def _округлить(значение: float | None, digits: int) -> float | None:
    return None if значение is None else round(float(значение), digits)


def _наклон(ряд: list[float | None]) -> float | None:
    """Наклон по методу наименьших квадратов — «на сколько за корзину»."""
    точки = [(i, v) for i, v in enumerate(ряд) if v is not None]
    if len(точки) < 3:
        return None
    n = len(точки)
    sx = sum(x for x, _ in точки)
    sy = sum(y for _, y in точки)
    sxx = sum(x * x for x, _ in точки)
    sxy = sum(x * y for x, y in точки)
    знаменатель = n * sxx - sx * sx
    if not знаменатель:
        return None
    return round((n * sxy - sx * sy) / знаменатель, 6)


def _вердикт(изменение: float | None, better: str) -> str:
    """Стало лучше, хуже или всё равно — по направлению «лучше»."""
    if изменение is None or not better:
        return ""
    if abs(изменение) < 5:
        return "ровно"
    вверх = изменение > 0
    хорошо = (вверх and better == "up") or (not вверх and better == "down")
    if abs(изменение) >= 25:
        return "заметно лучше" if хорошо else "заметно хуже"
    return "лучше" if хорошо else "хуже"


def _корреляция(а: list[float | None], б: list[float | None]) -> tuple[float | None, int]:
    """Коэффициент Пирсона по общим непустым корзинам."""
    # strict=False намеренно: ряды разной длины бывают, когда у
    # источника нет данных в хвосте окна, и обрезать по короткому —
    # ровно то, что нужно.
    пары = [(x, y) for x, y in zip(а, б, strict=False)
            if x is not None and y is not None]
    n = len(пары)
    if n < 3:
        return None, n
    sx = sum(x for x, _ in пары)
    sy = sum(y for _, y in пары)
    sxx = sum(x * x for x, _ in пары)
    syy = sum(y * y for _, y in пары)
    sxy = sum(x * y for x, y in пары)
    низ = math.sqrt(max(0.0, n * sxx - sx * sx)) * math.sqrt(max(0.0, n * syy - sy * sy))
    if низ <= 0:
        return None, n
    return (n * sxy - sx * sy) / низ, n
