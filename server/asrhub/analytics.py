"""Аналитика: сводные показатели, ряды по времени, сравнение моделей, разбор ошибок.

Все расчёты выполняются на стороне базы либо на выборках ограниченного
размера, чтобы страница аналитики оставалась быстрой даже при сотнях
тысяч заданий.
"""
from __future__ import annotations

import copy
import threading
import time
from bisect import bisect_right
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from .catalog import get_model
from .db import Database
from .logging_setup import get_logger
from .pipeline import metrics as M

log = get_logger("analytics")

#: Чьи задания в разрезе: один владелец, несколько (отдел) или все (None).
Владелец = str | list[str] | None

#: Сколько меток показывать в разрезе. Метку задаёт клиент, и её мощность
#: ничем не ограничена, в отличие от моделей, движков и языков.
TAG_LIMIT = 100

#: Ниже какой средней уверенности задание считается «неуверенным». Одно
#: число на весь сервер: этой же границей считает долю мониторинг
#: (`asrhub_low_confidence_share`), и раньше там стояло 0,7 против 0,75
#: здесь — одна и та же доля в двух местах давала два разных числа.
НИЗКАЯ_УВЕРЕННОСТЬ = 0.75

PERIODS = {
    "hour": 3600,
    "day": 86400,
    "week": 604800,
    "month": 2592000,
    "quarter": 7776000,
    "year": 31536000,
    "all": 0,
}


def _since(period: str) -> float:
    seconds = PERIODS.get(period, 86400)
    return 0.0 if seconds == 0 else time.time() - seconds


def _корзина(нижние: list[float], момент: float) -> int:
    """Номер корзины ряда по её точным нижним границам.

    Корзину считали делением: `int((момент - начало) / шаг)`. Числу это
    безразлично, а раскрытию — нет: список под точкой графика спрашивает
    базу условием «от границы до границы», и на самой границе деление и
    сравнение расходятся на последнем знаке. Поэтому корзина выбирается
    сравнением с теми же числами, которые уходят в ответ (`edges`) и
    возвращаются границами списка: одно правило на число и на список.
    Момент раньше первой границы и позже последней — в крайние корзины,
    как и было.
    """
    return min(max(bisect_right(нижние, момент) - 1, 0), len(нижние) - 1)


def _номер_корзины(нижние: list[float], столбец: str = "created_at") -> tuple[str, list[float]]:
    """Выражение SQL с номером корзины ряда — по тем же границам, что `_корзина`.

    Не деление, а сравнение с теми же числами: «меньше второй границы —
    нулевая, меньше третьей — первая…», и всё, что позже последней нижней
    границы, — в последнюю корзину. Ровно так выбирает `_корзина`, и ровно
    так режет базу список под точкой графика: запись на самой границе
    попадает в одну и ту же корзину и в число, и в список.
    """
    if len(нижние) <= 1:
        return "0", []
    ветви = " ".join(f"WHEN {столбец} < ? THEN {i}" for i in range(len(нижние) - 1))
    return f"CASE {ветви} ELSE {len(нижние) - 1} END", list(нижние[1:])


def _escape_label(value: Any) -> str:
    """Экранирует значение метки по правилам формата Prometheus."""
    text = str(value if value is not None else "")
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


class Analytics:
    def __init__(self, db: Database):
        self.db = db
        #: Кеш выборок и границы окон на время одного сводного отчёта.
        #:
        #: Потоко-локальные, и это не перестраховка. Объект `Analytics` один
        #: на приложение, а обработчики синхронные и живут в пуле потоков:
        #: общий кеш означал бы, что второй отчёт подхватывает чужую
        #: заморозку окна, а когда первый заканчивается — теряет её на
        #: середине. Половина разрезов посчиталась бы по одному окну, вторая
        #: по другому, то есть ровно тот дефект, ради которого заморозка и
        #: делалась, возвращался бы под нагрузкой и через раз.
        self._местное = threading.local()
        #: Готовые сводные отчёты: (период, владелец) → (момент, поколение
        #: заданий, отчёт). См. `full_report`.
        self._готовые: dict[tuple[str, str], tuple[float, int, dict[str, Any]]] = {}
        self._замок_готовых = threading.Lock()

    @property
    def _выборки(self) -> dict[tuple[Any, ...], list[dict[str, Any]]] | None:
        return getattr(self._местное, "выборки", None)

    @_выборки.setter
    def _выборки(self, значение: dict[tuple[Any, ...], list[dict[str, Any]]] | None) -> None:
        self._местное.выборки = значение

    @property
    def _окна(self) -> dict[str, float] | None:
        return getattr(self._местное, "окна", None)

    @_окна.setter
    def _окна(self, значение: dict[str, float] | None) -> None:
        self._местное.окна = значение

    def _since(self, period: str) -> float:
        """Начало окна разреза; внутри отчёта — общее для всех разрезов.

        Каждый разрез считал границу сам, от текущего времени. За те
        секунды, что собирается отчёт, «месяц» у последнего разреза
        начинался позже, чем у первого: разрезы одного отчёта расходились
        между собой, а общая выборка заданий не попадала в кеш ни разу —
        ключ у каждого получался свой.
        """
        if self._окна is None:
            return _since(period)
        if period not in self._окна:
            self._окна[period] = _since(period)
        return self._окна[period]

    #: Сколько строк самое большее читает выборка значений одного разреза.
    #: Это предохранитель, а не размер окна: разрезы, которым нужны
    #: отдельные значения (перцентили, распределения), читают только свои
    #: несколько колонок, и миллион строк таких колонок — десятки мегабайт.
    #: Раньше каждый разрез брал сто тысяч самых свежих заданий целиком,
    #: вместе с началом расшифровки, и сверх этого молча считал период по
    #: свежей части; теперь упор в предел виден признаком `truncated`.
    ПРЕДЕЛ_ЗНАЧЕНИЙ = 1_000_000

    @property
    def _усечено(self) -> bool:
        return bool(getattr(self._местное, "усечено", False))

    @_усечено.setter
    def _усечено(self, значение: bool) -> None:
        self._местное.усечено = значение

    def _условие(self, *, since: float | None = None,
                 status: str | list[str] | None = None,
                 owner: str | list[str] | None = None,
                 where: tuple[str, ...] = ()) -> tuple[str, list[Any]]:
        """WHERE разреза: окно, статус, владелец — тем же правилом, что у
        списка заданий (`Database._jobs_where`) и у раскрытия числа."""
        части: list[str] = []
        args: list[Any] = []
        if since:
            части.append("created_at>=?")
            args.append(since)
        if status:
            статусы = [status] if isinstance(status, str) else list(status)
            части.append("status IN (" + ",".join("?" for _ in статусы) + ")")
            args.extend(статусы)
        отбор, свои = self.db._owner_clause(owner, "jobs")
        if отбор:
            части.append(отбор)
            args.extend(свои)
        части.extend(where)
        return (" WHERE " + " AND ".join(части)) if части else "", args

    def _группы(self, выражения: str, *, group: str = "",
                args_before: list[Any] | tuple[Any, ...] = (),
                **условия: Any) -> list[dict[str, Any]]:
        """Счётный разрез группировкой в базе: строки «группа → числа».

        Так считаются тренды, и так теперь считаются счётные разрезы
        аналитики. Прежде они читали в память сто тысяч заданий целиком и
        сверх этого молча считали период по свежей части: на потоке в пять
        тысяч звонков в сутки месячный отчёт показывал 4900 часов звука из
        6370, а раздел ошибок в том же отчёте — все 130 000 заданий. Счёт в
        базе не упирается ни в какой предел и не тащит строк в память.

        `args_before` — параметры выражений SELECT (границы корзин, подписи
        пустых значений): в тексте запроса они стоят раньше условий.
        """
        где, args = self._условие(**условия)
        запрос = f"SELECT {выражения} FROM jobs{где}"
        if group:
            запрос += f" GROUP BY {group}"
        return [dict(r) for r in self.db.query(запрос, [*args_before, *args])]

    def _значения(self, столбцы: str, **условия: Any) -> list[dict[str, Any]]:
        """Отдельные значения для разреза — только нужные колонки, новые первыми.

        Перцентилям и распределениям нужны сами значения, и группировкой в
        базе их не заменить. Но им нужны две-три колонки, а не задание
        целиком с началом расшифровки: месячная выборка в 130 000 заданий
        стоила 12 с и 330 МБ, из них 19,6 млн знаков `text_preview`, которые
        аналитике не нужны вовсе. Внутри одного сводного отчёта одинаковые
        выборки берутся из кеша (см. `_общая_выборка`).
        """
        отбор = условия.get("owner")
        статус = условия.get("status")
        # Владелец бывает списком: ключ в подразделении видит задания всей
        # группы, и `scope_owner` отдаёт перечень. Список нехешируем, и без
        # этого приведения любой такой ключ получал 500 на всей аналитике —
        # ровно там, где разграничение по владельцу и работает.
        ключ = (столбцы, условия.get("since"),
                tuple(статус) if isinstance(статус, (list, tuple)) else статус,
                tuple(отбор) if isinstance(отбор, (list, tuple)) else отбор,
                tuple(условия.get("where") or ()))
        готовое = self._выборки.get(ключ) if self._выборки is not None else None
        if готовое is not None:
            return готовое
        где, args = self._условие(**условия)
        строки = self.db.query(
            f"SELECT {столбцы} FROM jobs{где} ORDER BY created_at DESC LIMIT ?",
            [*args, self.ПРЕДЕЛ_ЗНАЧЕНИЙ + 1])
        if len(строки) > self.ПРЕДЕЛ_ЗНАЧЕНИЙ:
            строки = строки[:self.ПРЕДЕЛ_ЗНАЧЕНИЙ]
            self._усечено = True
            log.warning("Выборка аналитики упёрлась в предел %d строк: часть разреза "
                        "посчитана по свежей части окна", self.ПРЕДЕЛ_ЗНАЧЕНИЙ)
        итог = [dict(r) for r in строки]
        if self._выборки is not None:
            self._выборки[ключ] = итог
        return итог

    @contextmanager
    def _общая_выборка(self) -> Iterator[None]:
        """Включает кеш на время сборки одного отчёта и гасит его после.

        Вложенность допустима: внутренний вызов не сбрасывает чужой кеш.
        Держать его дольше отчёта нельзя — аналитика живёт в приложении
        всё время его работы, и кеш стал бы просто устаревшими данными.
        """
        свой = self._выборки is None
        if свой:
            self._выборки = {}
            self._окна = {}
            self._усечено = False
        try:
            yield
        finally:
            if свой:
                self._выборки = None
                self._окна = None

    # --- сводка ---------------------------------------------------------

    #: Колонки этапов обработки и их подписи — для разбивки «Время по этапам».
    ЭТАПЫ: dict[str, str] = {
        "audio_prep_s": "Подготовка аудио",
        "vad_s": "Поиск речи",
        "model_load_s": "Загрузка модели",
        "inference_s": "Распознавание",
        "alignment_s": "Выравнивание",
        "diarization_s": "Разделение по говорящим",
        "postprocess_s": "Постобработка",
    }

    #: Счёт заданий одной группы: сколько и чем кончились, звук и слова по
    #: готовым. Одно выражение на сводку, модели, языки, владельцев и
    #: движки — чтобы «готовых» и «часов звука» не считали пять раз пятью
    #: способами.
    _СЧЁТ = (
        "COUNT(*) AS jobs, "
        "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
        "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
        "SUM(CASE WHEN status='completed' THEN COALESCE(media_duration_s,0) ELSE 0 END) "
        "AS audio, "
        "SUM(CASE WHEN status='completed' THEN COALESCE(processing_time_s,0) ELSE 0 END) "
        "AS proc, "
        "SUM(CASE WHEN status='completed' THEN COALESCE(words_count,0) ELSE 0 END) AS words, "
        # Средние — по тем же правилам, что прежде в Питоне: RTF и
        # уверенность без нулей и пропусков, WER — всё записанное.
        "AVG(CASE WHEN status='completed' AND rtf <> 0 THEN rtf END) AS rtf_avg, "
        "AVG(CASE WHEN status='completed' AND avg_confidence <> 0 THEN avg_confidence END) "
        "AS confidence_avg, "
        "AVG(CASE WHEN status='completed' THEN wer END) AS wer_avg")

    def overview(self, period: str = "day", owner: Владелец = None) -> dict[str, Any]:
        since = self._since(period)
        этапы = ", ".join(
            f"SUM(CASE WHEN status='completed' THEN COALESCE({к},0) ELSE 0 END) AS {к}"
            for к in self.ЭТАПЫ)
        по_статусам = {str(с["status"]): с for с in self._группы(
            "status, COUNT(*) AS n, "
            "SUM(CASE WHEN COALESCE(cached_from,'') <> '' THEN 1 ELSE 0 END) AS cached, "
            "SUM(COALESCE(media_duration_s,0)) AS audio, "
            "SUM(COALESCE(processing_time_s,0)) AS proc, "
            "SUM(COALESCE(words_count,0)) AS words, SUM(COALESCE(chars_count,0)) AS chars, "
            "SUM(COALESCE(segments_count,0)) AS segments, MIN(created_at) AS first, " + этапы,
            group="status", since=since or None, owner=owner)}

        def сколько(*статусы: str) -> int:
            return sum(int((по_статусам.get(с) or {}).get("n") or 0) for с in статусы)

        готовые = по_статусам.get("completed") or {}
        всего = sum(int(с["n"] or 0) for с in по_статусам.values())
        done = сколько("completed")
        # Без речи и короткие — итоги, а не ошибки: в «успешные» они идут
        # (задание дошло до конца без сбоя), в объём и скорость — нет.
        без_речи, коротких = сколько("no_speech"), сколько("short")
        audio_s = float(готовые.get("audio") or 0)
        proc_s = float(готовые.get("proc") or 0)
        words = int(готовые.get("words") or 0)

        # Значения для перцентилей — пять колонок готовых заданий, а не
        # задания целиком.
        значения = self._значения(
            "rtf, processing_time_s, queue_time_s, avg_confidence, wer",
            status="completed", since=since or None, owner=owner)
        rtf_values = [float(j["rtf"]) for j in значения if j.get("rtf")]
        # `is not None`, а не «истинно»: ноль — это законное ожидание
        # (задание из кеша отвечает мгновенно), и выбрасывание нулей
        # завышало перцентиль в разы. Соседний раздел «Ожидание в очереди»
        # всегда считал по этому правилу, и одно и то же число в одной
        # выгрузке отличалось впятеро.
        queue_values = [float(j["queue_time_s"]) for j in значения
                        if j.get("queue_time_s") is not None]
        conf_values = [float(j["avg_confidence"]) for j in значения if j.get("avg_confidence")]
        wer_values = [float(j["wer"]) for j in значения if j.get("wer") is not None]

        первое = min((float(с["first"]) for с in по_статусам.values()
                      if с.get("first") is not None), default=time.time())
        span_h = ((time.time() - since) / 3600) if since else max(
            1.0, (time.time() - первое) / 3600)
        # Доля успеха — среди ЗАВЕРШЁННЫХ, тем же знаменателем, что в
        # таблице моделей: задания в очереди, в работе и отменённые — не
        # неудачи. Делитель «все задания окна» давал на одном экране 0,56 в
        # сводке и 0,71 в таблице моделей при одной модели, а очередь на
        # паузе — «0 % успеха» при нуле отказов.
        успешно = done + без_речи + коротких
        завершено = успешно + сколько("failed")

        return {
            "period": period,
            "generated_at": time.time(),
            "jobs": {
                "total": всего,
                "completed": done,
                "failed": сколько("failed"),
                "cancelled": сколько("cancelled"),
                "no_speech": без_речи,
                "short": коротких,
                "in_progress": сколько("queued", "running", "retry"),
                "cached": sum(int(с.get("cached") or 0) for с in по_статусам.values()),
                "finished": завершено,
                "success_rate": round(успешно / завершено, 4) if завершено else None,
            },
            "volume": {
                "audio_seconds": round(audio_s, 1),
                "audio_hours": round(audio_s / 3600, 2),
                "processing_seconds": round(proc_s, 1),
                "words": words,
                "characters": int(готовые.get("chars") or 0),
                "segments": int(готовые.get("segments") or 0),
                "files_per_hour": round(done / span_h, 2) if span_h else 0,
                "audio_hours_per_hour": round(audio_s / 3600 / span_h, 2) if span_h else 0,
            },
            "performance": {
                "rtf": M.summarize(rtf_values),
                "processing_time_s": M.summarize(
                    [float(j["processing_time_s"]) for j in значения
                     if j.get("processing_time_s")]),
                "queue_time_s": M.summarize(queue_values),
                "speedup": round(audio_s / proc_s, 2) if proc_s else None,
            },
            "quality": {
                "confidence": M.summarize(conf_values),
                "wer": M.summarize(wer_values) if wer_values else None,
                "low_confidence_jobs": sum(1 for c in conf_values if c < НИЗКАЯ_УВЕРЕННОСТЬ),
                "confidence_distribution": self._корзины_уверенности(conf_values),
            },
            "stages": self._stage_breakdown(
                {к: float(готовые.get(к) or 0) for к in self.ЭТАПЫ}),
        }

    @staticmethod
    def _корзины_уверенности(значения: list[float]) -> list[dict[str, Any]]:
        """Корзины уверенности с пометкой, у какой из них верх входит.

        Верх последней корзины в ответе — ровно единица: так его и пишут на
        оси. Считается же она до 1,0001, то есть задание с уверенностью
        ровно 1,0 в неё входит. Раскрытию столбца в список это надо знать:
        по условию «меньше единицы» такие задания из списка выпали бы, и
        список оказался бы короче столбца.
        """
        корзины = M.confidence_buckets(значения) if значения else []
        if корзины:
            корзины[-1]["inclusive"] = True
        return корзины

    def _stage_breakdown(self, totals: dict[str, float]) -> dict[str, Any]:
        """Разбивка времени готовых заданий по этапам — по суммам колонок."""
        keys = dict(self.ЭТАПЫ)
        # Поиск речи, выравнивание и разделение по говорящим хранятся с
        # версии 3.1.17 и бывают не у всех заданий. Стадия, которой в периоде
        # не было, в разбивку не идёт: «Выравнивание — 0 с» в легенде читается
        # как «выравнивание ничего не стоит», а его просто не включали.
        keys = {k: v for k, v in keys.items()
                if totals.get(k) or k in ("audio_prep_s", "model_load_s",
                                          "inference_s", "postprocess_s")}
        grand = sum(totals.get(k, 0.0) for k in self.ЭТАПЫ) or 1.0
        return {
            # Колонки заданий в том же порядке, что подписи: по ним доля
            # раскрывается в список заданий, у которых этот этап был.
            "keys": list(keys),
            "labels": [keys[k] for k in keys],
            "seconds": [round(totals.get(k, 0.0), 2) for k in keys],
            "share": [round(totals.get(k, 0.0) / grand, 4) for k in keys],
            "total_seconds": round(grand, 2),
        }

    # --- ряды по времени -------------------------------------------------

    def timeseries(self, period: str = "day", buckets: int = 48,
                   owner: Владелец = None) -> dict[str, Any]:
        since = self._since(period) or (time.time() - PERIODS["week"])
        span = max(time.time() - since, 60.0)
        width = span / buckets
        # Границы корзин — одни на подсчёт и на раскрытие точки в список.
        # Последняя корзина закрывается моментом после выборки: в неё, как
        # и раньше, попадает всё, что создано позже расчётного конца окна.
        нижние = [since + slot * width for slot in range(buckets)]
        номер, границы = _номер_корзины(нижние)
        grid = {int(с["slot"]): с for с in self._группы(
            f"{номер} AS slot, "
            "SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed, "
            # Без речи и короткие — свой ряд: не ошибки и не расшифровки.
            "SUM(CASE WHEN status IN ('no_speech','short') THEN 1 ELSE 0 END) AS empty, "
            "SUM(CASE WHEN status='completed' THEN COALESCE(media_duration_s,0) ELSE 0 END) "
            "AS audio, "
            "SUM(CASE WHEN status='completed' THEN COALESCE(words_count,0) ELSE 0 END) AS words, "
            "AVG(CASE WHEN status='completed' AND rtf <> 0 THEN rtf END) AS rtf, "
            # `is not None`, а не «истинно»: ноль — законное ожидание
            # (задание из кеша, пустая очередь), и выброшенные нули
            # завышали среднее ряда против сводки и трендов.
            "AVG(CASE WHEN status='completed' THEN queue_time_s END) AS queue",
            group="slot", args_before=границы, since=since, owner=owner)}
        конец = max(time.time(), нижние[-1])

        series: dict[str, Any] = {
            "labels": [], "completed": [], "failed": [], "empty": [], "audio_minutes": [],
            "rtf": [], "queue_time": [], "words": [],
        }
        for slot in range(buckets):
            cell = grid.get(slot) or {}
            series["labels"].append(нижние[slot])
            series["completed"].append(int(cell.get("completed") or 0))
            series["failed"].append(int(cell.get("failed") or 0))
            series["empty"].append(int(cell.get("empty") or 0))
            series["audio_minutes"].append(round(float(cell.get("audio") or 0) / 60, 2))
            series["rtf"].append(round(float(cell["rtf"]), 4)
                                 if cell.get("rtf") is not None else None)
            series["queue_time"].append(round(float(cell["queue"]), 2)
                                        if cell.get("queue") is not None else None)
            series["words"].append(int(cell.get("words") or 0))

        # Замеры железа и очереди — всего сервера, а не владельца: обычному
        # ключу они отдавались здесь целиком, хотя в трендах те же ряды
        # закрыты для не администратора («знать её — уже разведка»). Разрез
        # по владельцу (`owner`) бывает только у не администратора.
        samples = self.db.system_samples(since, limit=2000) if owner is None else []
        series["system"] = {
            "ts": [s["ts"] for s in samples],
            "cpu": [s.get("cpu_percent") for s in samples],
            "ram_used_mb": [s.get("ram_used_mb") for s in samples],
            "gpu": [s.get("gpu_percent") for s in samples],
            "gpu_mem_mb": [s.get("gpu_mem_mb") for s in samples],
            "queue_depth": [s.get("queue_depth") for s in samples],
            "active_jobs": [s.get("active_jobs") for s in samples],
        }
        series["bucket_seconds"] = round(width, 1)
        series["since"] = since
        # Корзина i — от `edges[i]` включительно до `edges[i + 1]`; без
        # округления: по этим числам список режет базу, и округлённая
        # граница отдала бы задание соседней точке.
        series["edges"] = [*нижние, конец]
        return series

    # --- сравнение моделей -------------------------------------------------

    def by_model(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        since = self._since(period)
        # Движок — у самого свежего задания модели: SQLite отдаёт голую
        # колонку из той строки, на которой достигнут MAX (единственный
        # MIN/MAX в запросе). Так было и прежде — первое задание списка,
        # упорядоченного «сначала новые».
        группы = self._группы(
            "COALESCE(NULLIF(model,''),'—') AS model, engine, MAX(created_at) AS last, "
            + self._СЧЁТ, group="1", since=since or None, owner=owner)
        # p90 RTF — по значениям: перцентиль группировкой в базе не посчитать.
        по_rtf: dict[str, list[float]] = {}
        for j in self._значения("model, rtf", status="completed", since=since or None,
                                owner=owner, where=("rtf <> 0",)):
            по_rtf.setdefault(str(j.get("model") or "—"), []).append(float(j["rtf"]))

        rows: list[dict[str, Any]] = []
        for г in группы:
            model = str(г["model"])
            done = int(г["completed"] or 0)
            отказов = int(г["failed"] or 0)
            # Доля успеха — среди ЗАВЕРШЁННЫХ: задания в очереди, в работе и
            # отменённые — не неудачи. Раньше знаменателем были все задания
            # окна, и очередь на паузе или ночной пакет давали «0 % успеха»
            # и критическую тревогу при нуле отказов.
            завершено = done + отказов
            audio = float(г["audio"] or 0)
            proc = float(г["proc"] or 0)
            rtf = по_rtf.get(model) or []
            spec = get_model(model)
            rows.append({
                "model": model,
                "name": spec.name if spec else model,
                "family": spec.family if spec else "",
                "engine": г.get("engine"),
                "license": spec.license if spec else "",
                "jobs": int(г["jobs"] or 0),
                "completed": done,
                "failed": отказов,
                "finished": завершено,
                "success_rate": round(done / завершено, 4) if завершено else None,
                "audio_hours": round(audio / 3600, 3),
                "processing_hours": round(proc / 3600, 3),
                "speedup": round(audio / proc, 2) if proc else None,
                "rtf_avg": round(float(г["rtf_avg"]), 4) if г.get("rtf_avg") is not None else None,
                "rtf_p90": round(M.percentile(rtf, 0.9), 4) if rtf else None,
                "confidence_avg": (round(float(г["confidence_avg"]), 4)
                                   if г.get("confidence_avg") is not None else None),
                "wer_avg": round(float(г["wer_avg"]), 4) if г.get("wer_avg") is not None else None,
                "words": int(г["words"] or 0),
                "avg_duration_s": round(audio / done, 1) if done else None,
                "catalog_ru_wer": spec.best_ru_wer if spec else None,
                "catalog_rtfx": spec.rtfx if spec else None,
            })
        rows.sort(key=lambda r: r["jobs"], reverse=True)
        return rows

    # --- прочие срезы -------------------------------------------------------

    # Владелец передаётся во все срезы без исключения. В двух он терялся, и
    # обычный ключ видел в разделах «языки» и «движки» сводку по всему
    # серверу: и чужие числа, и чужие названия моделей.
    def by_language(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        return self._group(period, "language", "Язык не определён", owner)

    def by_owner(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        return self._group(period, "owner", "аноним", owner)

    def by_engine(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        return self._group(period, "engine", "—", owner)

    def by_source(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        return self._group(period, "source", "api", owner)

    #: Колонки, по которым бывает простой разрез: имя в базе — из этого
    #: перечня, а не из запроса.
    _РАЗРЕЗЫ = frozenset({"language", "owner", "engine", "source"})

    def _group(self, period: str, field: str, fallback: str,
               owner: Владелец = None) -> list[dict[str, Any]]:
        if field not in self._РАЗРЕЗЫ:
            raise ValueError(f"неизвестный разрез: {field}")
        since = self._since(period)
        rows: list[dict[str, Any]] = [{
            "key": str(г["k"]),
            "jobs": int(г["jobs"] or 0),
            "completed": int(г["completed"] or 0),
            "failed": int(г["failed"] or 0),
            "audio_hours": round(float(г["audio"] or 0) / 3600, 3),
            "words": int(г["words"] or 0),
            "rtf_avg": round(float(г["rtf_avg"]), 4) if г.get("rtf_avg") is not None else None,
        } for г in self._группы(f"COALESCE(NULLIF({field},''), ?) AS k, " + self._СЧЁТ,
                                group="1", args_before=[fallback],
                                since=since or None, owner=owner)]
        rows.sort(key=lambda r: r["jobs"], reverse=True)
        return rows

    def errors(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        since = self._since(period)
        # Упавшие — своей узкой выборкой без предела в десять тысяч: на
        # большом периоде коды переставали расти после десятитысячного
        # отказа, а «всего отказов» строкой ниже считалось честно.
        jobs = self._значения(
            "id, filename, created_at, model, error_code, error_message, error_hint",
            status="failed", since=since or None, owner=owner)
        by_code: dict[str, dict[str, Any]] = {}
        for job in jobs:
            code = str(job.get("error_code") or "unknown")
            entry = by_code.setdefault(code, {
                "code": code, "count": 0, "message": job.get("error_message") or "",
                "hint": job.get("error_hint") or "", "models": {}, "examples": []})
            entry["count"] += 1
            model = str(job.get("model") or "—")
            entry["models"][model] = entry["models"].get(model, 0) + 1
            if len(entry["examples"]) < 5:
                entry["examples"].append({
                    "id": job["id"], "filename": job.get("filename"),
                    "created_at": job.get("created_at")})
        rows = sorted(by_code.values(), key=lambda r: r["count"], reverse=True)
        # Числитель и знаменатель обязаны считаться по одному множеству.
        # Раньше отказы брались по владельцу, а всего заданий — по всему
        # серверу, и доля выходила заниженной в разы. Плюс len(jobs) упирался
        # в limit выборки, поэтому на большом периоде отказы переставали
        # расти после десяти тысяч.
        total_jobs = self.db.count_jobs(since=since or None, owner=owner)
        total_failed = self.db.count_jobs(status="failed", since=since or None, owner=owner)
        return {
            "total_failed": total_failed,
            "total_jobs": total_jobs,
            "failure_rate": round(total_failed / total_jobs, 4) if total_jobs else 0.0,
            "by_code": rows,
            # Не ошибки, но рядом с ними их ищут: «а куда делись молчащие?»
            "no_speech": self.db.count_jobs(status="no_speech", since=since or None,
                                            owner=owner),
            "short": self.db.count_jobs(status="short", since=since or None, owner=owner),
        }

    def duration_histogram(self, period: str = "month", bins: int = 10,
                          owner: Владелец = None) -> dict[str, Any]:
        since = self._since(period)
        durations = [float(j["media_duration_s"]) for j in self._значения(
            "media_duration_s", status="completed", since=since or None, owner=owner,
            where=("media_duration_s <> 0",))]
        if not durations:
            return {"edges": [], "counts": [], "labels": []}
        edges = [0, 30, 60, 120, 300, 600, 1200, 1800, 3600, 7200, 1e9]
        labels = ["<30 с", "30–60 с", "1–2 мин", "2–5 мин", "5–10 мин", "10–20 мин",
                  "20–30 мин", "30–60 мин", "1–2 ч", ">2 ч"]
        counts = [0] * len(labels)
        for value in durations:
            for idx in range(len(labels)):
                if edges[idx] <= value < edges[idx + 1]:
                    counts[idx] += 1
                    break
        # Границы корзин уходят в ответ: столбец раскрывается в список
        # записей этой длины, и считать границы заново на экране — значит
        # однажды разойтись с этим перечнем.
        return {"labels": labels, "counts": counts, "edges": edges,
                "total": len(durations), "summary": M.summarize(durations)}

    def slowest(self, period: str = "month", limit: int = 15,
                owner: Владелец = None) -> list[dict[str, Any]]:
        """Самые медленные готовые задания по RTF — запросом с пределом.

        Задания из кеша сюда не идут: у клона RTF чужой (оригинала), а
        обработка — ноль секунд, и в «самых медленных» стояли повторы
        одного и того же файла, которые сервер не считал вовсе.
        """
        since = self._since(period)
        где, args = self._условие(status="completed", since=since or None, owner=owner,
                                  where=("rtf <> 0", "COALESCE(cached_from,'') = ''"))
        строки = self.db.query(
            "SELECT id, filename, model, rtf, media_duration_s, processing_time_s, created_at "
            f"FROM jobs{где} ORDER BY rtf DESC, created_at DESC LIMIT ?",
            [*args, max(1, int(limit))])
        return [{
            "id": j["id"], "filename": j["filename"], "model": j["model"],
            "rtf": j["rtf"], "duration_s": j["media_duration_s"],
            "processing_time_s": j["processing_time_s"],
            "created_at": j["created_at"],
        } for j in строки]

    #: Час суток и день недели задания — по местному времени сервера, тем же
    #: выражением, каким режет список раскрытия (`hour:`, `weekday:`).
    #: `%w` в SQLite — 0 у воскресенья; здесь неделя с понедельника.
    _ЧАС = "CAST(strftime('%H', created_at, 'unixepoch', 'localtime') AS INTEGER)"
    _ДЕНЬ = "((CAST(strftime('%w', created_at, 'unixepoch', 'localtime') AS INTEGER) + 6) % 7)"

    def hourly_profile(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        """Распределение нагрузки по часам суток и дням недели."""
        since = self._since(period)
        hours = [0] * 24
        weekdays = [0] * 7
        for г in self._группы(f"{self._ДЕНЬ} AS d, {self._ЧАС} AS h, COUNT(*) AS n",
                              group="1, 2", since=since or None, owner=owner):
            hours[int(г["h"])] += int(г["n"])
            weekdays[int(г["d"])] += int(г["n"])
        return {
            "hours": hours,
            "hour_labels": [f"{h:02d}" for h in range(24)],
            "weekdays": weekdays,
            "weekday_labels": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "peak_hour": max(range(24), key=lambda h: hours[h]) if any(hours) else None,
        }

    #: Цена работы своими силами: звук и время обработки готовых заданий,
    #: посчитанных сервером, а не взятых из кеша.
    _СВОИ = ("SUM(CASE WHEN status='completed' AND COALESCE(cached_from,'') = '' "
             "THEN COALESCE(media_duration_s,0) ELSE 0 END) AS own_audio, "
             "SUM(CASE WHEN status='completed' AND COALESCE(cached_from,'') = '' "
             "THEN COALESCE(processing_time_s,0) ELSE 0 END) AS own_proc")

    @staticmethod
    def _measured_rtf(строка: dict[str, Any] | list[dict[str, Any]]) -> float:
        """Во сколько раз обработка медленнее реального времени.

        Считается по заданиям, которые сервер действительно считал сам:
        у взятого из кеша собственного времени обработки нет, и включать
        его в среднее — значит занижать цену работы тем сильнее, чем чаще
        срабатывает кеш.

        Раньше экономию от кеша в одном разрезе считали по этой измеренной
        величине, а в другом — по вбитой в код 0.2, и на одной странице
        стояли два числа про одно и то же, различавшиеся вдвое.
        """
        if isinstance(строка, list):
            # Список заданий — тот же счёт в Питоне: строка свода из базы
            # (`_СВОИ`) и список дают одно число.
            свои = [j for j in строка
                    if j.get("status") == "completed" and not j.get("cached_from")]
            строка = {"own_audio": sum(float(j.get("media_duration_s") or 0) for j in свои),
                      "own_proc": sum(float(j.get("processing_time_s") or 0) for j in свои)}
        звук = float(строка.get("own_audio") or 0)
        время = float(строка.get("own_proc") or 0)
        return (время / звук) if звук else 0.0

    def efficiency(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        """Оценка эффективности: сколько ресурсов уходит на час аудио."""
        since = self._since(period)
        строка = (self._группы(
            "COUNT(*) AS jobs, SUM(COALESCE(media_duration_s,0)) AS audio, "
            "SUM(COALESCE(processing_time_s,0)) AS proc, "
            "SUM(COALESCE(model_load_s,0)) AS load, "
            "SUM(CASE WHEN COALESCE(cached_from,'') <> '' THEN 1 ELSE 0 END) AS hits, "
            "SUM(CASE WHEN COALESCE(cached_from,'') <> '' THEN COALESCE(media_duration_s,0) "
            "ELSE 0 END) AS hit_audio, " + self._СВОИ,
            status="completed", since=since or None, owner=owner) or [{}])[0]
        jobs = int(строка.get("jobs") or 0)
        audio = float(строка.get("audio") or 0)
        proc = float(строка.get("proc") or 0)
        load = float(строка.get("load") or 0)
        попаданий = int(строка.get("hits") or 0)
        rtf = self._measured_rtf(строка)
        return {
            "audio_hours": round(audio / 3600, 2),
            "compute_hours": round(proc / 3600, 2),
            # Здесь кеш учитывается: это фактическая цена часа выданного
            # звука, и снижать её — ровно то, ради чего кеш существует.
            "compute_per_audio_hour": round(proc / audio, 3) if audio else None,
            "model_load_share": round(load / proc, 4) if proc else None,
            "model_load_seconds": round(load, 1),
            "cache_hits": попаданий,
            # Доля попаданий — среди готовых, как в разделе «Повторы»: там
            # знаменателем были все задания окна, и одна и та же доля на
            # одной странице выходила 0,2 и 0,11.
            "cache_hit_rate": round(попаданий / jobs, 4) if jobs else None,
            # А здесь — нет: экономия считается по цене работы, которую
            # пришлось бы выполнить, то есть по скорости своих заданий.
            "assumed_rtf": round(rtf, 4) if rtf else None,
            "saved_compute_hours": round(float(строка.get("hit_audio") or 0) * rtf / 3600, 3),
        }

    # --- сводный отчёт -------------------------------------------------------

    # --- нагрузка по дням недели ------------------------------------------

    def weekly_heatmap(self, period: str = "month",
                       owner: Владелец = None) -> dict[str, Any]:
        """Карта нагрузки «день недели × час».

        Суточный профиль усредняет будни с выходными и потому отвечает не на
        тот вопрос. Планировать обслуживание, окна обновления и запас
        мощности приходится по неделе: у телефонии понедельник в десять утра
        и воскресенье в десять вечера — это разные миры.
        """
        since = self._since(period)
        счёт = [[0] * 24 for _ in range(7)]
        часы = [[0.0] * 24 for _ in range(7)]
        for г in self._группы(
                f"{self._ДЕНЬ} AS d, {self._ЧАС} AS h, COUNT(*) AS n, "
                "SUM(COALESCE(media_duration_s,0)) AS audio",
                group="1, 2", since=since or None, owner=owner,
                where=("COALESCE(created_at,0) <> 0",)):
            счёт[int(г["d"])][int(г["h"])] = int(г["n"])
            часы[int(г["d"])][int(г["h"])] = float(г["audio"] or 0) / 3600

        плоско = [v for строка in счёт for v in строка]
        пик = max(плоско) if плоско else 0
        индекс = плоско.index(пик) if пик else -1
        return {
            "days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
            "hours": list(range(24)),
            "jobs": счёт,
            "audio_hours": [[round(v, 3) for v in строка] for строка in часы],
            "peak": {
                "jobs": пик,
                "day": индекс // 24 if индекс >= 0 else None,
                "hour": индекс % 24 if индекс >= 0 else None,
            },
            "total": sum(плоско),
        }

    # --- экономия на повторах ---------------------------------------------

    def cache_savings(self, period: str = "month",
                      owner: Владелец = None) -> dict[str, Any]:
        """Сколько работы сняли повторы одного и того же файла.

        Сервер узнаёт уже виденный файл по хешу и отдаёт готовый результат.
        Пока это видно только счётчиком «из кеша», а вопрос у владельца
        сервера другой: сколько часов звука и машинного времени это
        сэкономило — то есть стоило ли оно того.
        """
        since = self._since(period)
        строка = (self._группы(
            "SUM(CASE WHEN COALESCE(cached_from,'') <> '' THEN 1 ELSE 0 END) AS hits, "
            "SUM(CASE WHEN COALESCE(cached_from,'') = '' AND status='completed' "
            "THEN 1 ELSE 0 END) AS misses, "
            "SUM(CASE WHEN COALESCE(cached_from,'') <> '' THEN COALESCE(media_duration_s,0) "
            "ELSE 0 END) AS hit_audio, " + self._СВОИ,
            since=since or None, owner=owner) or [{}])[0]
        попаданий = int(строка.get("hits") or 0)
        промахов = int(строка.get("misses") or 0)
        # Стоимость повтора считаем по средней скорости своих заданий: у
        # взятого из кеша задания собственного времени обработки нет.
        звук_кеш = float(строка.get("hit_audio") or 0)
        rtf = self._measured_rtf(строка)

        # Самые частые повторы: по ним видно, не шлёт ли клиент одно и то же
        # по кругу из-за ошибки в своей очереди. Ключ уходит в ответ: по нему
        # строка «файл пришёл N раз» раскрывается в сами эти N заданий. Имя
        # файла — у самого свежего повтора (голая колонка при MAX).
        топ: list[dict[str, Any]] = [{"key": str(г["k"]), "hits": int(г["n"]),
                "filename": г.get("filename") or "",
                "audio_hours": round(float(г["audio"] or 0) / 3600, 3)}
               for г in self._группы(
                   "COALESCE(NULLIF(file_hash,''), cached_from) AS k, COUNT(*) AS n, "
                   "filename, MAX(created_at) AS last, "
                   "SUM(COALESCE(media_duration_s,0)) AS audio",
                   group="1", since=since or None, owner=owner,
                   where=("COALESCE(cached_from,'') <> ''",
                          "COALESCE(NULLIF(file_hash,''), cached_from, '') <> ''"))]
        топ.sort(key=lambda r: -r["hits"])

        return {
            "period": period,
            "hits": попаданий,
            "misses": промахов,
            # Доля попаданий — среди выданных результатов (из кеша и своих
            # готовых), тем же знаменателем, что в «Эффективности».
            "hit_rate": (round(попаданий / (попаданий + промахов), 4)
                         if попаданий + промахов else None),
            "audio_hours_saved": round(звук_кеш / 3600, 3),
            "processing_seconds_saved": round(звук_кеш * rtf, 1),
            "assumed_rtf": round(rtf, 4) if rtf else None,
            "repeats": топ[:10],
        }

    # --- надёжность --------------------------------------------------------

    def reliability(self, period: str = "month",
                    owner: Владелец = None) -> dict[str, Any]:
        """Что происходит с заданием между приёмом и выдачей результата.

        Доля успеха отвечает «сколько дошло», но не «какой ценой». Задание,
        прошедшее с третьей попытки, считается успешным наравне с тем, что
        прошло с первой, — а это разные состояния сервера.
        """
        since = self._since(period)
        окно: dict[str, Any] = {"since": since or None, "owner": owner}
        распределение: dict[int, int] = {}
        всего = готово = с_повтором = отказов = 0
        for г in self._группы("COALESCE(retries,0) AS r, status, COUNT(*) AS n",
                              group="1, 2", **окно):
            n, r = int(г["n"]), int(г["r"])
            распределение[r] = распределение.get(r, 0) + n
            всего += n
            if г["status"] == "completed":
                готово += n
                с_повтором += n if r > 0 else 0
            elif г["status"] == "failed":
                отказов += n
        кем = sorted(((str(г["k"]), int(г["n"])) for г in self._группы(
            "COALESCE(NULLIF(cancelled_by,''),'неизвестно') AS k, COUNT(*) AS n",
            group="1", status="cancelled", **окно)), key=lambda kv: -kv[1])
        вебхуки = sorted(((str(г["k"]), int(г["n"])) for г in self._группы(
            "webhook_status AS k, COUNT(*) AS n", group="1",
            where=("COALESCE(webhook_status,'') <> ''",), **окно)), key=lambda kv: -kv[1])
        # Доля «с первой попытки» — среди завершённых (готовые и упавшие),
        # как доля успеха в таблице моделей: стоящие в очереди и отменённые
        # первую попытку ещё не сделали или не доделали.
        завершено = готово + отказов
        return {
            "period": period,
            "total": всего,
            "first_attempt_success": готово - с_повтором,
            "first_attempt_rate": round((готово - с_повтором) / завершено, 4)
                                  if завершено else None,
            # Названия здесь важнее обычного: «повторялось: 0» рядом с
            # «повторов всего: 7» читается как противоречие, хотя это разные
            # вопросы — сколько заданий дошло со второй попытки и сколько
            # повторов было вообще (включая те, что так и не дошли).
            "completed_after_retry": с_повтором,
            "jobs_with_retries": sum(v for k, v in распределение.items() if k > 0),
            "retry_total": sum(k * v for k, v in распределение.items()),
            "retry_distribution": [{"retries": k, "jobs": v}
                                   for k, v in sorted(распределение.items())],
            "cancelled": sum(v for _, v in кем),
            "cancelled_by": [{"who": k, "jobs": v} for k, v in кем],
            "webhooks": [{"status": k, "jobs": v} for k, v in вебхуки],
        }

    # --- каким бывает звук --------------------------------------------------

    def audio_profile(self, period: str = "month",
                      owner: Владелец = None) -> dict[str, Any]:
        """Свойства самих записей: формат, битрейт, темп речи, говорящие.

        Это разрез не про сервер, а про материал. Темп речи и доля тишины
        объясняют, почему одна и та же модель на одном потоке работает
        вдвое медленнее, чем на другом, — и стоит ли менять модель или
        источник записи.
        """
        since = self._since(period)
        jobs = self._значения(
            "filename, media_duration_s, file_size, words_count, speakers_count, "
            "segments_count, source, snr_db, loudness_lufs, peak_dbfs, silence_share, "
            "clipping_share, avg_confidence, wer, suspect_share",
            status="completed", since=since or None, owner=owner)

        форматы: dict[str, dict[str, float]] = {}
        темпы: list[float] = []
        битрейты: list[float] = []
        говорящие: dict[int, int] = {}
        плотность: list[float] = []

        for job in jobs:
            имя = str(job.get("filename") or "")
            расш = имя.rsplit(".", 1)[-1].lower() if "." in имя else "без расширения"
            запись = форматы.setdefault(расш, {"jobs": 0, "audio_hours": 0.0, "bytes": 0.0})
            запись["jobs"] += 1
            запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
            запись["bytes"] += float(job.get("file_size") or 0)

            длительность = float(job.get("media_duration_s") or 0)
            слова = int(job.get("words_count") or 0)
            if длительность > 1 and слова:
                темпы.append(слова / (длительность / 60))
            размер = float(job.get("file_size") or 0)
            if длительность > 1 and размер:
                битрейты.append(размер * 8 / длительность / 1000)
            n = int(job.get("speakers_count") or 0)
            if n:
                говорящие[n] = говорящие.get(n, 0) + 1
            # Сегментов на минуту записи: по этому числу видно, что за
            # материал приносят. Диктовка даёт единицы, живой диалог с
            # перебиваниями — десятки. Это не доля времени под речью:
            # длину сегментов сервер в разрезе не хранит.
            сегменты = float(job.get("segments_count") or 0)
            if длительность > 1 and сегменты:
                плотность.append(сегменты / (длительность / 60))

        строки = []
        for имя, данные in sorted(форматы.items(), key=lambda kv: -kv[1]["jobs"]):
            строки.append({
                "format": имя,
                "jobs": int(данные["jobs"]),
                "audio_hours": round(данные["audio_hours"], 2),
                "avg_mb": round(данные["bytes"] / данные["jobs"] / 1024 / 1024, 2)
                          if данные["jobs"] else 0,
            })

        return {
            "period": period,
            "formats": строки,
            "speech_rate_wpm": M.summarize(темпы),
            "bitrate_kbps": M.summarize(битрейты),
            "segments_per_minute": M.summarize(плотность),
            "speakers": [{"speakers": k, "jobs": v} for k, v in sorted(говорящие.items())],
            **self._звук_на_входе(jobs),
        }

    @staticmethod
    def _звук_на_входе(jobs: list[dict[str, Any]]) -> dict[str, Any]:
        """Профиль звука по записям: корзины SNR против уверенности, WER и
        подозрительных сегментов, доля плохого звука, громкость, клиппинг.

        Это ответ на «почему у этого источника ошибок вдвое больше»:
        WER растёт с падением SNR предсказуемо (Deepgram: 3,5 % при 20 дБ,
        15 % при 10, 35 % при 5), и разрез по корзинам показывает, где
        именно в этой зависимости живут свои записи.
        """
        from .pipeline import audio_profile as AP  # noqa: PLC0415

        измеренные = [j for j in jobs if j.get("snr_db") is not None]
        корзины: dict[str, list[dict[str, Any]]] = {}
        for j in измеренные:
            корзины.setdefault(AP.snr_band(float(j["snr_db"])) or "", []).append(j)

        def свод(items: list[dict[str, Any]]) -> dict[str, Any]:
            conf = [float(j["avg_confidence"]) for j in items if j.get("avg_confidence")]
            wer = [float(j["wer"]) for j in items if j.get("wer") is not None]
            подозр = [float(j["suspect_share"]) for j in items
                      if j.get("suspect_share") is not None]
            return {
                "jobs": len(items),
                "audio_hours": round(sum(float(j.get("media_duration_s") or 0)
                                         for j in items) / 3600, 2),
                "confidence_avg": round(sum(conf) / len(conf), 4) if conf else None,
                "low_confidence_share": round(sum(1 for c in conf if c < НИЗКАЯ_УВЕРЕННОСТЬ) / len(conf), 4)
                if conf else None,
                "wer_avg": round(sum(wer) / len(wer), 4) if wer else None,
                "wer_jobs": len(wer),
                "suspect_share_avg": round(sum(подозр) / len(подозр), 4) if подозр else None,
            }

        плохие = [j for j in измеренные if AP.is_bad(j)]
        # Границы корзины — рядом с подписью: строка раскрывается в записи
        # с таким отношением речи к шуму. Нижняя входит, верхняя нет;
        # `None` — граница открыта.
        границы: dict[str, tuple[float | None, float | None]] = {}
        низ: float | None = None
        for верх, подпись in AP.КОРЗИНЫ_SNR:
            границы[подпись] = (низ, верх)
            низ = верх
        return {
            "measured_jobs": len(измеренные),
            "snr_db": M.summarize([float(j["snr_db"]) for j in измеренные]),
            "loudness_lufs": M.summarize([float(j["loudness_lufs"]) for j in измеренные
                                          if j.get("loudness_lufs") is not None]),
            "peak_dbfs": M.summarize([float(j["peak_dbfs"]) for j in измеренные
                                      if j.get("peak_dbfs") is not None]),
            "silence_share": M.summarize([float(j["silence_share"]) for j in измеренные
                                          if j.get("silence_share") is not None]),
            "clipped_jobs": sum(1 for j in измеренные
                                if (j.get("clipping_share") or 0) >= AP.КЛИППИНГ_ПЛОХОЙ),
            "noisy_jobs": sum(1 for j in измеренные if float(j["snr_db"]) < AP.SNR_ПЛОХОЙ),
            "bad_audio_jobs": len(плохие),
            "bad_audio_share": round(len(плохие) / len(измеренные), 4) if измеренные else None,
            "snr_bands": [{"key": подпись, "from": границы[подпись][0],
                           "to": границы[подпись][1], **свод(корзины.get(подпись, []))}
                          for _, подпись in AP.КОРЗИНЫ_SNR],
            "bad_audio_by_source": sorted(
                ({"key": имя, "jobs": n, "bad": b, "share": round(b / n, 4)}
                 for имя, (n, b) in (
                     (src, (sum(1 for j in измеренные if str(j.get("source") or "api") == src),
                            sum(1 for j in плохие if str(j.get("source") or "api") == src)))
                     for src in {str(j.get("source") or "api") for j in измеренные})
                 if n),
                key=lambda r: -r["share"]),
        }

    # --- расход ресурсов ----------------------------------------------------

    def resources(self, period: str = "month",
                  owner: Владелец = None) -> dict[str, Any]:
        """Память и устройства в разрезе моделей.

        Отвечает на вопрос, который задают перед покупкой второй карты:
        какая модель сколько памяти просит на пике и сколько заданий вообще
        уехало на процессор вместо видеокарты.

        Пик — величина на весь процесс, а не на модель: счётчики и torch, и
        psutil другого не умеют. Поэтому рядом с ним идёт число заданий,
        шедших разом. «27 ГБ при одном задании» и «27 ГБ при трёх» — разные
        ответы на вопрос о железе, и разрез по этому числу отвечает на
        главное: сколько заданий разом карта выдержит.
        """
        since = self._since(period)
        jobs = self._значения(
            "model, peak_memory_mb, peak_memory_jobs, device, media_duration_s, "
            "processing_time_s", status="completed", since=since or None, owner=owner)

        по_модели: dict[str, list[float]] = {}
        по_одновременности: dict[int, list[float]] = {}
        устройства: dict[str, dict[str, float]] = {}
        for job in jobs:
            память = float(job.get("peak_memory_mb") or 0)
            if память:
                по_модели.setdefault(str(job.get("model") or "—"), []).append(память)
                разом = int(job.get("peak_memory_jobs") or 1)
                по_одновременности.setdefault(max(1, разом), []).append(память)
            dev = str(job.get("device") or "неизвестно")
            запись = устройства.setdefault(dev, {"jobs": 0, "audio_hours": 0.0,
                                                 "processing_s": 0.0})
            запись["jobs"] += 1
            запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
            запись["processing_s"] += float(job.get("processing_time_s") or 0)

        модели = []
        for имя, значения in sorted(по_модели.items(),
                                    key=lambda kv: -max(kv[1])):
            сводка = M.summarize(значения)
            модели.append({"model": имя, "jobs": len(значения),
                           "peak_mb": сводка["max"], "avg_mb": сводка["avg"],
                           "p95_mb": сводка["p95"]})

        разрез = []
        for имя, данные in sorted(устройства.items(), key=lambda kv: -kv[1]["jobs"]):
            звук = данные["audio_hours"]
            разрез.append({
                "device": имя,
                "jobs": int(данные["jobs"]),
                "audio_hours": round(звук, 2),
                "rtf": round(данные["processing_s"] / (звук * 3600), 4) if звук else None,
            })

        одновременно = []
        for сколько, значения in sorted(по_одновременности.items()):
            сводка = M.summarize(значения)
            одновременно.append({"jobs_at_once": сколько, "measurements": len(значения),
                                 "peak_mb": сводка["max"], "avg_mb": сводка["avg"],
                                 "p95_mb": сводка["p95"]})

        return {"period": period, "models": модели, "devices": разрез,
                "concurrency": одновременно}

    # --- качество во времени ------------------------------------------------

    def quality_trend(self, period: str = "month", buckets: int = 24,
                      owner: Владелец = None) -> dict[str, Any]:
        """Уверенность и WER по времени.

        Средняя уверенность за месяц — число, которое ничего не сообщает.
        Полезен ход: провал в среду означает, что в среду что-то поменялось
        — источник записей, модель или параметры.
        """
        since = self._since(period)
        jobs = self._значения("created_at, avg_confidence, wer", status="completed",
                              since=since or None, owner=owner)
        if not jobs:
            return {"period": period, "buckets": [], "edges": [], "bucket_seconds": 0,
                    "confidence": [], "wer": [], "jobs": [],
                    "low_confidence_share": []}

        начало = since or min(float(j["created_at"]) for j in jobs)
        конец = time.time()
        шаг = max(1.0, (конец - начало) / buckets)
        нижние = [начало + i * шаг for i in range(buckets)]

        корзины: list[dict[str, list[float]]] = [
            {"conf": [], "wer": [], "low": []} for _ in range(buckets)]
        for job in jobs:
            i = _корзина(нижние, float(job["created_at"]))
            c = job.get("avg_confidence")
            if c is not None:
                корзины[i]["conf"].append(float(c))
                корзины[i]["low"].append(1.0 if float(c) < НИЗКАЯ_УВЕРЕННОСТЬ else 0.0)
            w = job.get("wer")
            if w is not None:
                корзины[i]["wer"].append(float(w))

        среднее = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
        return {
            "period": period,
            "buckets": [round(начало + i * шаг) for i in range(buckets)],
            # `buckets` округлены до секунды — это подписи оси. Точные
            # границы — здесь: корзина i от `edges[i]` до `edges[i + 1]`,
            # по ним точка графика раскрывается в список своих заданий.
            "edges": [*нижние, max(конец, нижние[-1])],
            # Ширина корзины — чтобы подписать ось. За час она в минутах, за
            # год — в датах; без неё все подписи печатались как «день.месяц»
            # и на часовом окне читались как одна и та же дата двадцать
            # четыре раза подряд.
            "bucket_seconds": round(шаг),
            "confidence": [среднее(k["conf"]) for k in корзины],
            "wer": [среднее(k["wer"]) for k in корзины],
            "low_confidence_share": [среднее(k["low"]) for k in корзины],
            "jobs": [len(k["conf"]) for k in корзины],
        }

    # --- здоровье распознавания -----------------------------------------------

    #: Базовое окно дрейфа — четыре недели до периода, как у Deepgram.
    БАЗА_ДНЕЙ = 28

    def drift(self, period: str = "week", owner: Владелец = None) -> dict[str, Any]:
        """Дрейф уверенности: распределение за период против четырёх недель
        до него — по моделям и по всем вместе.

        Средняя уверенность по всем моделям может не сдвинуться, когда одна
        модель стала хуже, а другую стали реже использовать; поэтому по
        моделям. И распределение, а не среднее: половина записей лучше,
        половина хуже — среднее то же, а критерий Колмогорова — Смирнова
        это видит. Пороги — по Deepgram: 10 % сдвига и p < 0,05 вместе —
        критично, что-то одно — предупреждение.
        """
        from . import stats  # noqa: PLC0415

        since = self._since(period)
        # Граница периода — общая для всех разрезов отчёта (см. `_since`);
        # база — четыре недели до неё. У «всего времени» границы нет, а
        # значит, нет и «до»: сравнивать не с чем, вердикт — unknown.
        конец_базы = since or time.time()
        база_от = (конец_базы - self.БАЗА_ДНЕЙ * 86400) if since else None
        все = self._значения("created_at, model, avg_confidence", status="completed",
                             since=база_от, owner=owner,
                             where=("avg_confidence IS NOT NULL",))
        сейчас = [j for j in все if float(j["created_at"]) >= конец_базы] if since else все
        база = [j for j in все if float(j["created_at"]) < конец_базы] if since else []

        def сравнить(имя: str, б: list[dict[str, Any]], т: list[dict[str, Any]]) -> dict[str, Any]:
            итог = stats.drift_verdict([float(j["avg_confidence"]) for j in б],
                                       [float(j["avg_confidence"]) for j in т])
            низких = sum(1 for j in т if float(j["avg_confidence"]) < НИЗКАЯ_УВЕРЕННОСТЬ)
            низких_базы = sum(1 for j in б if float(j["avg_confidence"]) < НИЗКАЯ_УВЕРЕННОСТЬ)
            return {"key": имя, **итог,
                    "low_share": round(100.0 * низких / len(т), 1) if т else None,
                    "low_share_baseline": round(100.0 * низких_базы / len(б), 1) if б else None}

        по_моделям: dict[str, tuple[list, list]] = {}
        for j in база:
            по_моделям.setdefault(str(j.get("model") or "—"), ([], []))[0].append(j)
        for j in сейчас:
            по_моделям.setdefault(str(j.get("model") or "—"), ([], []))[1].append(j)
        модели = [сравнить(имя, б, т) for имя, (б, т) in sorted(по_моделям.items())
                  if т]
        порядок = {"critical": 0, "warning": 1, "ok": 2, "unknown": 3}
        модели.sort(key=lambda м: (порядок.get(м["verdict"], 4), -м["current"]["n"]))
        общий = сравнить("all", база, сейчас)
        return {
            "period": period, "baseline_days": self.БАЗА_ДНЕЙ,
            "baseline_from": round(база_от, 1) if база_от is not None else None,
            "baseline_to": round(конец_базы, 1) if since else None,
            # То же окно базы без округления: по нему число «заданий в
            # базе» раскрывается в список. Округлённая до десятой секунды
            # граница для подписи годится, для отбора — нет.
            "baseline_window": ({"since": база_от, "until": конец_базы}
                                if база_от is not None else None),
            "overall": общий, "models": модели,
            "worst": min((м["verdict"] for м in [общий, *модели]),
                         key=lambda v: порядок.get(v, 4), default="unknown"),
            "note": ("Сравнивается распределение средней уверенности по заданиям за "
                     "период с четырьмя неделями до него. Уверенность модели — не "
                     "точность: она падает и на честно трудном звуке. Дрейф — повод "
                     "посмотреть, что изменилось на входе, а не вердикт модели."),
        }

    def control(self, period: str = "week", owner: Владелец = None) -> dict[str, Any]:
        """Контрольные карты по дням: уверенность, доля заданий с низкой
        уверенностью, доля подозрительных сегментов — пределы 2σ и 3σ по
        четырём неделям до периода."""
        from . import stats  # noqa: PLC0415

        since = self._since(period)
        конец = time.time()
        начало = since or конец - 30 * 86400
        база_от = начало - self.БАЗА_ДНЕЙ * 86400
        все = self._значения(
            "created_at, avg_confidence, quality_flags, segments_count, suspect_segments",
            status="completed", since=база_от, owner=owner)
        дней = max(1, int(round((конец - начало) / 86400)))
        # Период короче суток сравнивать с суточными пределами нельзя:
        # база собрана по дням, и одна часовая точка на восьми разговорах
        # прыгает по биномиальному шуму далеко за 3σ — «критично» на ровном
        # месте при каждом открытии раздела за час. В таком случае карту
        # рисуем, а вердикты не выносим.
        короткий_период = (конец - начало) < 0.75 * 86400

        def границы_дней(от: float, до: float, n: int) -> list[float]:
            шаг = max(1.0, (до - от) / n)
            return [от + i * шаг for i in range(n)]

        def по_дням(от: float, до: float, n: int) -> list[dict[str, Any]]:
            нижние = границы_дней(от, до, n)
            корзины: list[dict[str, Any]] = [
                {"conf": [], "low": [], "suspect": 0.0, "segments": 0.0, "assessed": 0,
                 "jobs": 0} for _ in range(n)]
            for j in все:
                t = float(j["created_at"])
                if not от <= t < до:
                    continue
                i = _корзина(нижние, t)
                c = j.get("avg_confidence")
                if c is not None:
                    корзины[i]["conf"].append(float(c))
                    корзины[i]["low"].append(1.0 if float(c) < НИЗКАЯ_УВЕРЕННОСТЬ else 0.0)
                if j.get("quality_flags") is not None and int(j.get("segments_count") or 0):
                    # Доля подозрительных — сумма к сумме, как в разделе
                    # «Подозрительные расшифровки»: среднее долей по заданиям
                    # под тем же названием давало 25 % там, где раздел рядом
                    # показывал 0,1 % (запись 1 из 2 сегментов весила столько
                    # же, сколько 0 из 998).
                    корзины[i]["suspect"] += float(j.get("suspect_segments") or 0)
                    корзины[i]["segments"] += float(j["segments_count"])
                    корзины[i]["assessed"] += 1
                корзины[i]["jobs"] += 1
            return корзины

        база = по_дням(база_от, начало, self.БАЗА_ДНЕЙ)
        текущие = по_дням(начало, конец, дней)
        шаг = max(1.0, (конец - начало) / дней)
        среднее = lambda xs: (sum(xs) / len(xs)) if xs else None  # noqa: E731

        def значение(k: dict[str, Any], поле: str) -> float | None:
            if поле == "suspect":
                return 100.0 * k["suspect"] / k["segments"] if k["segments"] else None
            v = среднее(k[поле])
            return v * (100.0 if поле == "low" else 1.0) if v is not None else None

        def записей(k: dict[str, Any], поле: str) -> int:
            return k["assessed"] if поле == "suspect" else len(k[поле])

        карты = []
        for ключ, поле, подпись, лучше in (
                ("confidence", "conf", "Средняя уверенность", 1),
                ("low_confidence_share", "low", "Доля заданий с уверенностью ниже 0,75, %", -1),
                ("suspect_share", "suspect", "Доля подозрительных сегментов, %", -1)):
            ряд_базы = [значение(k, поле) for k in база]
            годные = [x for x in ряд_базы if x is not None]
            # Нижняя граница σ. Месяц без единого подозрительного сегмента
            # или без единого неуверенного задания — обычное состояние
            # чистой установки, и при σ = 0 «за 3σ» оказывалось любое
            # отклонение: одна неуверенная запись из пятидесяти — «критично».
            # У долей граница биномиальная (по числу записей в точке базы;
            # у подозрительных — по числу сегментов), у уверенности — полсотой.
            в_точке = [записей(k, поле) for k in база if значение(k, поле) is not None]
            сегментов = [k["segments"] for k in база if k["segments"]]
            if поле == "conf":
                граница = 0.005
            else:
                μ = sum(годные) / len(годные) if годные else 0.0
                n = (sum(сегментов) / len(сегментов) if поле == "suspect" and сегментов
                     else sum(в_точке) / len(в_точке) if в_точке else 0.0)
                граница = stats.binomial_std(μ, n)
            пределы = stats.control_limits(годные, min_std=граница)
            точки = [значение(k, поле) for k in текущие]
            # Точка из одной-двух записей вердикта не получает, а у долей за
            # предел выходит точка, где лишних против среднего хотя бы двое:
            # одна запись из пятидесяти — не отклонение процесса.
            отметки = [] if короткий_период else stats.spc_flags(
                точки, пределы, counts=[записей(k, поле) for k in текущие],
                min_n=stats.МИН_В_ТОЧКЕ, share=поле == "low", min_excess=2)
            карты.append({
                "key": ключ, "title": подпись, "good": лучше, "limits": пределы,
                "baseline": [{"ts": round(база_от + i * (начало - база_от) / self.БАЗА_ДНЕЙ, 1),
                              "value": (round(v, 4) if v is not None else None)}
                             for i, v in enumerate(ряд_базы)],
                "points": [{"ts": round(начало + i * шаг, 1),
                            "value": (round(v, 4) if v is not None else None),
                            "jobs": текущие[i]["jobs"],
                            # По скольким записям посчитана точка: у доли
                            # подозрительных это оценённые задания с сегментами.
                            "n": записей(текущие[i], поле)}
                           for i, v in enumerate(точки)],
                "flags": отметки,
                "worst": max((ф["level"] for ф in отметки),
                             key=lambda у: {"critical": 2, "warning": 1}.get(у, 0),
                             default=None),
            })
        return {"period": period, "baseline_days": self.БАЗА_ДНЕЙ, "step_s": round(шаг, 1),
                # Точные границы точек всех трёх карт: точка i — задания от
                # `edges[i]` до `edges[i + 1]`. `ts` у точек округлён до
                # десятой — для подписи; список по нему не сошёлся бы.
                "edges": [*границы_дней(начало, конец, дней), конец],
                "verdicts": not короткий_период,
                "note_short": ("Период короче суток: карта показана, вердикты не выносятся — "
                               "пределы посчитаны по суточным точкам."
                               if короткий_период else None),
                "charts": карты}

    # --- точность по эталону ---------------------------------------------------

    #: Корзины длительности записи для срезов: граница в секундах и подпись.
    ДЛИТЕЛЬНОСТИ: tuple[tuple[float | None, str], ...] = (
        (60, "до 1 мин"), (300, "1–5 мин"), (1200, "5–20 мин"),
        (3600, "20–60 мин"), (None, "более часа"))

    #: Сколько слов эталона нужно срезу, чтобы его WER что-то значил —
    #: около часа речи (Speechmatics). Меньше — число показывается, но
    #: помечается как «мало слов».
    ЗНАЧИМО_СЛОВ = 10000

    @classmethod
    def _с_границами(cls, строки: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Строки разреза по длительности — с границами своей корзины.

        Подпись «5–20 мин» человеку понятна, а раскрытию строки в список
        нужны секунды: нижняя граница входит, верхняя нет, `None` — граница
        открыта. Берутся из того же перечня, по которому запись попала в
        корзину, а не пишутся на экране второй раз.
        """
        границы: dict[str, tuple[float | None, float | None]] = {}
        низ: float | None = None
        for верх, подпись in cls.ДЛИТЕЛЬНОСТИ:
            границы[подпись] = (низ, верх)
            низ = верх
        return [{**строка, "from": границы[строка["key"]][0],
                 "to": границы[строка["key"]][1]} for строка in строки]

    @classmethod
    def _корзина_длительности(cls, секунд: float) -> str:
        for граница, подпись in cls.ДЛИТЕЛЬНОСТИ:
            if граница is None or секунд < граница:
                return подпись
        return cls.ДЛИТЕЛЬНОСТИ[-1][1]

    @classmethod
    def _свод_точности(cls, items: list[dict[str, Any]]) -> dict[str, Any]:
        """WER, MER и WIL по срезу — сложением слов, а не усреднением долей.

        Средний WER по записям — не WER среза: десятисекундная реплика с
        одной ошибкой (WER 50 %) весила бы столько же, сколько часовая
        встреча. Счётчики ошибок складываются, и деление идёт один раз.
        Записи, посчитанные до появления счётчиков, входят только в
        среднее по записям — оно показывается рядом как справочное.
        """
        со_счётчиками = [j for j in items if j.get("ref_words")]
        n = sum(int(j["ref_words"]) for j in со_счётчиками)
        s = sum(int(j.get("sub_words") or 0) for j in со_счётчиками)
        d = sum(int(j.get("del_words") or 0) for j in со_счётчиками)
        i = sum(int(j.get("ins_words") or 0) for j in со_счётчиками)
        h = max(0, n - s - d)
        m = h + s + i
        ошибок = s + d + i
        wer_по_записям = [float(j["wer"]) for j in items if j.get("wer") is not None]
        return {
            "jobs": len(items), "words": n, "enough": n >= cls.ЗНАЧИМО_СЛОВ,
            "wer": round(ошибок / n, 4) if n else None,
            "mer": round(ошибок / (ошибок + h), 4) if (ошибок + h) else None,
            "wil": round(1.0 - (h / n) * (h / m), 4) if n and m else None,
            "gap": round(ошибок / n - ошибок / (ошибок + h), 4) if n and (ошибок + h) else None,
            "substitutions": s, "deletions": d, "insertions": i,
            "insertion_share": round(i / n, 4) if n else None,
            "wer_avg": round(sum(wer_по_записям) / len(wer_по_записям), 4)
            if wer_по_записям else None,
        }

    def accuracy(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        """Точность по записям с эталоном — срезами: модель, длительность,
        источник, язык. С MER и WIL: WER не ограничен единицей, и разрыв
        WER − MER показывает избыток вставок, то есть галлюцинации.
        """
        since = self._since(period)
        jobs = self._значения(
            "id, filename, model, source, language, media_duration_s, wer, mer, ref_words, "
            "sub_words, del_words, ins_words", status="completed", since=since or None,
            owner=owner, where=("wer IS NOT NULL",))

        def срез(ключ: Any) -> list[dict[str, Any]]:
            группы: dict[str, list[dict[str, Any]]] = {}
            for j in jobs:
                группы.setdefault(ключ(j), []).append(j)
            строки = [{"key": имя, **self._свод_точности(items)}
                      for имя, items in группы.items()]
            строки.sort(key=lambda r: (-r["words"], -r["jobs"]))
            return строки

        худшие = sorted(jobs, key=lambda j: -float(j["wer"]))[:10]
        return {
            "period": period, "enough_words": self.ЗНАЧИМО_СЛОВ,
            "overall": self._свод_точности(jobs),
            "by_model": срез(lambda j: str(j.get("model") or "—")),
            "by_duration": self._с_границами(sorted(
                срез(lambda j: self._корзина_длительности(
                    float(j.get("media_duration_s") or 0))),
                key=lambda r: [п for _, п in self.ДЛИТЕЛЬНОСТИ].index(r["key"]))),
            "by_source": срез(lambda j: str(j.get("source") or "api")),
            "by_language": срез(lambda j: str(j.get("language") or "—")),
            "worst": [{"id": j["id"], "filename": j.get("filename"), "model": j.get("model"),
                       "wer": j.get("wer"), "mer": j.get("mer"), "words": j.get("ref_words"),
                       "insertions": j.get("ins_words")} for j in худшие],
            "note": ("Точность считается только по записям с эталоном: он приходит "
                     "полем reference_text при постановке задания или задаётся потом "
                     "в карточке. WER складывается по словам всех записей среза, а не "
                     "усредняется по записям; срез значим от десяти тысяч слов "
                     "эталона — это около часа речи."),
        }

    def calibration(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        """Калибровка уверенности по записям с эталоном: ECE, AUC и диаграмма
        надёжности — по всем и по моделям."""
        from .pipeline import calibration as C  # noqa: PLC0415

        since = self._since(period)
        jobs = [j for j in self._значения(
            "model, calibration", status="completed", since=since or None, owner=owner,
            where=("calibration IS NOT NULL",)) if j.get("calibration")]
        по_моделям: dict[str, list[Any]] = {}
        for j in jobs:
            по_моделям.setdefault(str(j.get("model") or "—"), []).append(j["calibration"])
        модели = []
        for имя, записи in по_моделям.items():
            свод = C.aggregate(записи)
            модели.append({"key": имя, "jobs": свод["jobs"], "words": свод["words"],
                           "ece": свод["ece"], "auc": свод["auc"],
                           "confidence": свод["confidence"], "accuracy": свод["accuracy"],
                           "overconfidence": свод["overconfidence"]})
        модели.sort(key=lambda м: -м["words"])
        return {
            "period": period, **C.aggregate([j["calibration"] for j in jobs]),
            "by_model": модели,
            "note": ("ECE — среднее расхождение между уверенностью модели и долей "
                     "верных слов по десяти корзинам: ноль — уверенности можно верить "
                     "буквально. Переоценка со знаком плюс — модель обещает больше, "
                     "чем даёт. AUC — насколько уверенность вообще отделяет верные "
                     "слова от неверных: 0,5 — никак, 1 — безошибочно."),
        }

    # --- латентность -----------------------------------------------------------

    def latency(self, period: str = "month", owner: Владелец = None) -> dict[str, Any]:
        """Задержка по моделям и по длительности: p50/p95/p99 времени
        обработки и RTF, ожидание в очереди; для потока — секунды до первого
        текста.

        Среднее время обработки скрывает хвост, а жалуются на хвост: p95 —
        то, что видит каждый двадцатый.
        """
        since = self._since(period)
        jobs = self._значения(
            "model, media_duration_s, processing_time_s, rtf, queue_time_s",
            status="completed", since=since or None, owner=owner,
            where=("COALESCE(cached_from,'') = ''", "processing_time_s IS NOT NULL"))

        def свод(items: list[dict[str, Any]]) -> dict[str, Any]:
            время = [float(j["processing_time_s"]) for j in items]
            rtf = [float(j["rtf"]) for j in items if j.get("rtf")]
            очередь = [float(j["queue_time_s"]) for j in items
                       if j.get("queue_time_s") is not None]
            звук = sum(float(j.get("media_duration_s") or 0) for j in items)
            return {
                "jobs": len(items), "audio_hours": round(звук / 3600, 3),
                "processing_p50": round(M.percentile(время, 0.5), 2) if время else None,
                "processing_p95": round(M.percentile(время, 0.95), 2) if время else None,
                "processing_p99": round(M.percentile(время, 0.99), 2) if время else None,
                "rtf_p50": round(M.percentile(rtf, 0.5), 4) if rtf else None,
                "rtf_p95": round(M.percentile(rtf, 0.95), 4) if rtf else None,
                "rtf_p99": round(M.percentile(rtf, 0.99), 4) if rtf else None,
                "queue_p50": round(M.percentile(очередь, 0.5), 2) if очередь else None,
                "queue_p95": round(M.percentile(очередь, 0.95), 2) if очередь else None,
            }

        def срез(ключ: Any) -> list[dict[str, Any]]:
            группы: dict[str, list[dict[str, Any]]] = {}
            for j in jobs:
                группы.setdefault(ключ(j), []).append(j)
            return [{"key": имя, **свод(items)} for имя, items in
                    sorted(группы.items(), key=lambda kv: -len(kv[1]))]

        # Поток: сессии не оставляют заданий, их задержка живёт в метриках.
        # Метрики — без владельца: это сессии всего сервера с названиями
        # моделей, которыми пользуются другие. Поэтому — только
        # администратору, как замеры железа в рядах по времени.
        поток: dict[str, list[float]] = {}
        сессии: list[dict[str, Any]] = []
        if owner is None:
            for строка in self.db.metric_values("stream_first_text_s", since):
                поток.setdefault(str(строка.get("model") or "—"), []).append(
                    float(строка["value"]))
            сессии = self.db.metric_values("stream_session_s", since)
        все_первые = [x for v in поток.values() for x in v]
        return {
            "period": period,
            "overall": свод(jobs),
            "by_model": срез(lambda j: str(j.get("model") or "—")),
            "by_duration": self._с_границами(sorted(
                срез(lambda j: self._корзина_длительности(
                    float(j.get("media_duration_s") or 0))),
                key=lambda r: [п for _, п in self.ДЛИТЕЛЬНОСТИ].index(r["key"]))),
            "stream": {
                "sessions": len(сессии),
                "first_text_p50": round(M.percentile(все_первые, 0.5), 2) if все_первые else None,
                "first_text_p95": round(M.percentile(все_первые, 0.95), 2) if все_первые else None,
                "by_model": [{"key": имя, "sessions": len(v),
                              "first_text_p50": round(M.percentile(v, 0.5), 2),
                              "first_text_p95": round(M.percentile(v, 0.95), 2)}
                             for имя, v in sorted(поток.items(),
                                                  key=lambda kv: (-len(kv[1]), kv[0]))],
            },
            "note": ("Только задания, которые сервер считал сам: у взятых из кеша "
                     "времени обработки нет. Для потока — секунды от начала сессии "
                     "до первого текста на экране, по модели."),
        }

    # --- согласие моделей ------------------------------------------------------

    #: От скольких прогонов среднее расхождение что-то значит.
    МИН_ПРОГОНОВ = 5

    def agreement(self, period: str = "week",
                  owner: str | list[str] | None = None) -> dict[str, Any]:
        """Согласие моделей по контрольным прогонам: расхождение двух
        расшифровок одной записи — по дням, по парам моделей и против
        предыдущего такого же периода.

        Точность без эталона это не меряет; рост расхождения — сигнал, что
        изменился вход или сломалась одна из моделей. Порог — относительный
        рост среднего к прошлому периоду: четверть — предупреждение, половина
        — критично; на выборке меньше пяти прогонов вердикта нет.
        """
        since = self._since(period)
        сейчас = time.time()
        проверки = self.db.model_checks(since or 0.0, owner=owner)
        прошлые: list[dict[str, Any]] = []
        if since:
            длина = сейчас - since
            прошлые = [п for п in self.db.model_checks(since - длина, owner=owner)
                       if float(п["created_at"]) < since]

        def среднее(items: list[dict[str, Any]]) -> float | None:
            значения = [float(п["wer"]) for п in items if п.get("wer") is not None]
            return round(sum(значения) / len(значения), 4) if значения else None

        по_дням: dict[int, list[dict[str, Any]]] = {}
        for п in проверки:
            по_дням.setdefault(int(float(п["created_at"]) // 86400), []).append(п)
        пары: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for п in проверки:
            пары.setdefault((str(п.get("model") or "—"), str(п.get("control_model") or "—")),
                            []).append(п)
        текущее, прошлое = среднее(проверки), среднее(прошлые)
        вердикт = "unknown"
        рост = None
        if текущее is not None and len(проверки) >= self.МИН_ПРОГОНОВ:
            вердикт = "ok"
            if прошлое and len(прошлые) >= self.МИН_ПРОГОНОВ:
                рост = round((текущее - прошлое) / прошлое, 4)
                if рост >= 0.5:
                    вердикт = "critical"
                elif рост >= 0.25:
                    вердикт = "warning"
        значения = sorted(float(п["wer"]) for п in проверки if п.get("wer") is not None)
        return {
            "period": period, "checks": len(проверки), "previous_checks": len(прошлые),
            "wer_avg": текущее, "previous_wer_avg": прошлое, "growth": рост,
            "wer_p50": round(M.percentile(значения, 0.5), 4) if значения else None,
            "wer_p90": round(M.percentile(значения, 0.9), 4) if значения else None,
            "verdict": вердикт,
            "by_day": [{"ts": день * 86400.0, "checks": len(items), "wer_avg": среднее(items)}
                       for день, items in sorted(по_дням.items())],
            "by_pair": sorted(({"model": м, "control_model": к, "checks": len(items),
                                "wer_avg": среднее(items),
                                "mer_avg": round(sum(float(п["mer"]) for п in items
                                                     if п.get("mer") is not None)
                                                 / max(1, sum(1 for п in items
                                                              if п.get("mer") is not None)), 4)}
                               for (м, к), items in пары.items()),
                              key=lambda r: -r["checks"]),
            "worst": [{"job_id": п["job_id"], "check_job_id": п["check_job_id"],
                       "filename": п.get("filename"), "model": п.get("model"),
                       "control_model": п.get("control_model"), "wer": п.get("wer"),
                       "snr_db": п.get("snr_db"), "confidence": п.get("avg_confidence")}
                      for п in sorted(проверки, key=lambda x: -float(x.get("wer") or 0))[:10]],
            "note": ("Расхождение — WER расшифровки контрольной модели относительно "
                     "исходной; какая из двух права, без эталона неизвестно. Смотрите "
                     "ход: рост среднего на четверть к прошлому периоду — "
                     "предупреждение, на половину — критично."),
        }

    def suspicious(self, period: str = "month", owner: Владелец = None,
                   limit: int = 15) -> dict[str, Any]:
        """Подозрительные расшифровки: галлюцинации, повторы, разметка.

        Средняя уверенность по заданию оставалась приличной и тогда, когда
        Whisper дописывал на тишине «Субтитры сделал DimaTorzok»: признаки
        считаются по сегментам, и здесь они собраны по моделям, по
        источникам и по признакам — чтобы видеть, у какой модели и на каком
        входе расшифровки начинают выдумывать.
        """
        from . import quality  # noqa: PLC0415

        since = self._since(period)
        jobs = self._значения(
            "id, filename, model, source, created_at, segments_count, suspect_segments, "
            "suspect_share, quality_flags", status="completed", since=since or None,
            owner=owner, where=("segments_count <> 0",))
        # Задания, сделанные до появления признаков, не считаются ни
        # подозрительными, ни чистыми — их просто не смотрели.
        оценённые = [j for j in jobs if j.get("quality_flags") is not None]
        сегментов = sum(int(j.get("segments_count") or 0) for j in оценённые)
        подозрительных = sum(int(j.get("suspect_segments") or 0) for j in оценённые)
        с_признаками = [j for j in оценённые if (j.get("quality_flags") or "")]

        по_признакам: dict[str, int] = dict.fromkeys(quality.ПРИЗНАКИ, 0)
        for j in с_признаками:
            for признак in str(j.get("quality_flags") or "").split(","):
                if признак in по_признакам:
                    по_признакам[признак] += 1

        def группы(поле: str) -> list[dict[str, Any]]:
            корзины: dict[str, dict[str, float]] = {}
            for j in оценённые:
                ключ = str(j.get(поле) or "—")
                к = корзины.setdefault(ключ, {"jobs": 0, "flagged": 0,
                                              "segments": 0, "suspect": 0})
                к["jobs"] += 1
                к["flagged"] += 1 if j.get("quality_flags") else 0
                к["segments"] += int(j.get("segments_count") or 0)
                к["suspect"] += int(j.get("suspect_segments") or 0)
            out = []
            for ключ, к in корзины.items():
                out.append({
                    "key": ключ, "jobs": к["jobs"], "flagged": к["flagged"],
                    "flagged_share": round(100.0 * к["flagged"] / к["jobs"], 1)
                    if к["jobs"] else None,
                    "suspect_share": round(100.0 * к["suspect"] / к["segments"], 2)
                    if к["segments"] else None,
                })
            return sorted(out, key=lambda x: -(x["suspect_share"] or 0))

        худшие = sorted(с_признаками, key=lambda j: -float(j.get("suspect_share") or 0))
        return {
            "period": period,
            "jobs": len(jobs),
            "assessed": len(оценённые),
            "flagged": len(с_признаками),
            "flagged_share": round(100.0 * len(с_признаками) / len(оценённые), 1)
            if оценённые else None,
            "segments": сегментов,
            "suspect_segments": подозрительных,
            "suspect_share": round(100.0 * подозрительных / сегментов, 2)
            if сегментов else None,
            "by_flag": [{"key": к, "title": quality.ПРИЗНАКИ[к], "jobs": n}
                        for к, n in sorted(по_признакам.items(), key=lambda x: -x[1])
                        if n],
            "by_model": группы("model"),
            "by_source": группы("source"),
            "worst": [{
                "id": j["id"], "filename": j.get("filename"), "model": j.get("model"),
                "created_at": j.get("created_at"),
                "suspect_segments": j.get("suspect_segments"),
                "segments_count": j.get("segments_count"),
                "suspect_share": j.get("suspect_share"),
                "flags": [ф for ф in str(j.get("quality_flags") or "").split(",") if ф],
            } for j in худшие[:limit]],
        }

    # --- разрез по меткам ---------------------------------------------------

    def by_tag(self, period: str = "month", owner: Владелец = None) -> list[dict[str, Any]]:
        """Метки — единственный разрез, который задаёт сам пользователь.

        Всё остальное сервер знает про себя: модель, движок, источник. А
        метка отвечает на вопрос «сколько ушло на этот проект» — и потому
        для отчётности она важнее прочих разрезов.
        """
        since = self._since(period)
        jobs = self._значения("tags, status, media_duration_s, processing_time_s, words_count",
                              since=since or None, owner=owner)
        собрано: dict[str, dict[str, Any]] = {}
        for job in jobs:
            # Без повторов: «срочно, срочно» — одна метка, а не две. Иначе
            # задание входило в строку метки дважды — и числом заданий, и
            # часами звука, — а в списке под этим числом стояло один раз.
            метки = list(dict.fromkeys(
                t.strip() for t in str(job.get("tags") or "").split(",") if t.strip()))
            for метка in метки or ["без метки"]:
                запись = собрано.setdefault(метка, {
                    "tag": метка, "jobs": 0, "completed": 0, "failed": 0, "empty": 0,
                    "audio_hours": 0.0, "processing_s": 0.0, "words": 0})
                запись["jobs"] += 1
                if job["status"] == "completed":
                    запись["completed"] += 1
                    запись["audio_hours"] += float(job.get("media_duration_s") or 0) / 3600
                    запись["processing_s"] += float(job.get("processing_time_s") or 0)
                    запись["words"] += int(job.get("words_count") or 0)
                elif job["status"] == "failed":
                    запись["failed"] += 1
                elif job["status"] in ("no_speech", "short"):
                    запись["empty"] += 1

        строки = []
        for запись in собрано.values():
            звук = запись["audio_hours"]
            строки.append({
                **запись,
                "audio_hours": round(звук, 2),
                "processing_s": round(запись["processing_s"], 1),
                "rtf": round(запись["processing_s"] / (звук * 3600), 4) if звук else None,
            })
        # Предел: метку задаёт клиент в запросе, и она единственная в отчёте
        # с неограниченной мощностью. Клиент, ставящий уникальную метку на
        # каждое задание (номер разговора, отметка времени), превращал этот
        # разрез в сто тысяч строк — и в таблице на экране, и в листе
        # выгрузки, где остальные разрезы не длиннее полусотни. Верх списка
        # отвечает на вопрос «куда ушло время», хвост из одиночных меток —
        # нет.
        строки.sort(key=lambda r: -r["jobs"])
        return строки[:TAG_LIMIT]

    # --- ожидание в очереди -------------------------------------------------

    def queue_latency(self, period: str = "month",
                      owner: Владелец = None) -> dict[str, Any]:
        """Сколько задание ждало и от чего это зависело.

        Среднее ожидание скрывает именно то, на что жалуются: хвост. Ждали
        три секунды девятьсот заданий и сорок минут — десять, и среднее
        покажет «всё хорошо».
        """
        since = self._since(period)
        jobs = self._значения("queue_time_s, priority, source", since=since or None,
                              owner=owner, where=("queue_time_s IS NOT NULL",))

        по_приоритету: dict[str, list[float]] = {}
        по_источнику: dict[str, list[float]] = {}
        for job in jobs:
            ожидание = float(job.get("queue_time_s") or 0)
            приоритет = int(job.get("priority") or 50)
            группа = ("высокий (>50)" if приоритет > 50 else
                      "обычный (50)" if приоритет == 50 else "низкий (<50)")
            по_приоритету.setdefault(группа, []).append(ожидание)
            по_источнику.setdefault(str(job.get("source") or "api"), []).append(ожидание)

        разрез = lambda d: [  # noqa: E731
            {"name": k, "jobs": len(v), **M.summarize(v)}
            for k, v in sorted(d.items(), key=lambda kv: -len(kv[1]))]

        все = [float(j["queue_time_s"]) for j in jobs]
        # Подпись группы — для человека, ключ — для раскрытия строки в
        # список: по скобкам в подписи отбор не выбирают.
        ключи = {"высокий (>50)": "high", "обычный (50)": "normal", "низкий (<50)": "low"}
        return {
            "period": period,
            "overall": M.summarize(все),
            "by_priority": [{**строка, "key": ключи[строка["name"]]}
                            for строка in разрез(по_приоритету)],
            "by_source": разрез(по_источнику),
            "waited_over_minute": sum(1 for v in все if v > 60),
            "waited_over_10_minutes": sum(1 for v in все if v > 600),
        }

    def full_report(self, period: str = "week",
                    owner: str | list[str] | None = None) -> dict[str, Any]:
        """Все разрезы сразу — по одной выборке заданий на каждый запрос.

        Отдельные разрезы забирают по своим адресам и читают базу сами;
        здесь их два десятка подряд, и общий кеш убирает два десятка
        одинаковых проходов по архиву.
        """
        # Готовый отчёт живёт полминуты, пока задания не менялись (заведение,
        # удаление, смена статуса — см. `Database.поколение_заданий`): раздел
        # «Аналитика» открывают несколько человек, а на большом архиве сборка
        # — секунды. Прогресс идущих заданий отчёт не меняет и кеш не сбивает.
        ключ = (str(period), repr(sorted(owner) if isinstance(owner, list) else owner))
        поколение = int(getattr(self.db, "поколение_заданий", 0) or 0)
        сейчас = time.monotonic()
        with self._замок_готовых:
            готовый = self._готовые.get(ключ)
        if готовый and сейчас - готовый[0] < self.СРОК_ГОТОВОГО_ОТЧЁТА_С \
                and готовый[1] == поколение:
            return copy.deepcopy(готовый[2])
        with self._общая_выборка():
            отчёт = self._full_report(period, owner)
        with self._замок_готовых:
            if len(self._готовые) > 64:
                self._готовые.clear()
            self._готовые[ключ] = (сейчас, поколение, copy.deepcopy(отчёт))
        return отчёт

    #: Сколько секунд отдавать готовый сводный отчёт без пересборки.
    СРОК_ГОТОВОГО_ОТЧЁТА_С = 30.0

    def _full_report(self, period: str, owner: str | list[str] | None) -> dict[str, Any]:
        отчёт = self._разрезы(period, owner)
        # Окно, по которому всё это посчитано. Интерфейс раскрывает любое
        # число отчёта в список записей (`GET /api/drill`), и границы обязан
        # взять отсюда: посчитай он «неделю до сейчас» сам, через минуту это
        # была бы уже другая неделя, и список разошёлся бы с числом. Верхняя
        # граница — конец сборки: задания, созданные позже, в отчёт не вошли.
        начало = self._since(period)
        отчёт["window"] = {"since": начало or None, "until": time.time()}
        # Упёрлась ли хоть одна выборка значений в предел: тогда часть
        # перцентилей посчитана по свежей части окна, и отчёт говорит это
        # прямо, а не молча.
        отчёт["truncated"] = self._усечено
        return отчёт

    def _разрезы(self, period: str, owner: str | list[str] | None) -> dict[str, Any]:
        return {
            "overview": self.overview(period, owner),
            "timeseries": self.timeseries(period, owner=owner),
            "models": self.by_model(period, owner),
            "languages": self.by_language(period, owner),
            "owners": self.by_owner(period, owner),
            "engines": self.by_engine(period, owner),
            "errors": self.errors(period, owner),
            "durations": self.duration_histogram(period, owner=owner),
            "slowest": self.slowest(period, owner=owner),
            "profile": self.hourly_profile(period, owner),
            "efficiency": self.efficiency(period, owner),
            "weekly": self.weekly_heatmap(period, owner),
            "cache": self.cache_savings(period, owner),
            "reliability": self.reliability(period, owner),
            "audio": self.audio_profile(period, owner),
            "resources": self.resources(period, owner),
            "quality_trend": self.quality_trend(period, owner=owner),
            "suspicious": self.suspicious(period, owner),
            "drift": self.drift(period, owner),
            "control": self.control(period, owner),
            "accuracy": self.accuracy(period, owner),
            "calibration": self.calibration(period, owner),
            "latency": self.latency(period, owner),
            "agreement": self.agreement(period, owner),
            "tags": self.by_tag(period, owner),
            "queue": self.queue_latency(period, owner),
        }

    # --- экспорт метрик Prometheus --------------------------------------------

    def prometheus(self, insights: Any = None) -> str:
        """Метрики для Prometheus.

        `insights` — свод по содержанию записей. Если он передан, к метрикам
        сервера добавляются метрики разговоров: без них система мониторинга
        видит, что сервер жив и быстр, и не видит, что каждый третий
        разговор кончается руганью. Тревога на это ставится тем же
        правилом, что и на нехватку памяти, — и это единственный способ
        узнать о таком не через неделю.
        """
        lines: list[str] = []

        def add(name: str, value: Any, labels: str = "", help_text: str = "",
                kind: str = "gauge") -> None:
            if value is None:
                return
            if help_text:
                lines.append(f"# HELP asrhub_{name} {help_text}")
                lines.append(f"# TYPE asrhub_{name} {kind}")
            suffix = "{" + labels + "}" if labels else ""
            lines.append(f"asrhub_{name}{suffix} {value}")

        # Имена здесь не должны совпадать с накопительными счётчиками
        # подсистемы мониторинга: `asrhub_jobs_total`, `asrhub_audio_seconds_total`
        # и `asrhub_words_total` там растут с запуска, а тут это «сколько
        # сейчас» и «сколько за сутки». Под одним именем Prometheus видел
        # то счётчик, то скользящее окно, и `rate()` считал каждое снижение
        # сбросом счётчика — поток завышался в разы. Поэтому запасной путь
        # отдаёт свои показания под своими именами.
        counts = {status: self.db.count_jobs(status=status)
                  for status in ("queued", "running", "completed", "failed",
                                 "cancelled", "retry", "no_speech", "short")}
        for status, value in counts.items():
            add("jobs_current", value, f'status="{status}"',
                "Число заданий по статусам сейчас" if status == "queued" else "", "gauge")

        overview = self.overview("day")
        add("audio_seconds_day", overview["volume"]["audio_seconds"], "",
            "Обработано аудио за сутки, секунд", "gauge")
        add("words_day", overview["volume"]["words"], "",
            "Распознано слов за сутки", "gauge")
        add("rtf_avg", overview["performance"]["rtf"]["avg"], "",
            "Средний коэффициент реального времени")
        add("rtf_p95", overview["performance"]["rtf"]["p95"])
        add("queue_time_p95", overview["performance"]["queue_time_s"]["p95"], "",
            "95-й перцентиль ожидания в очереди, секунд")
        add("confidence_avg", overview["quality"]["confidence"]["avg"], "",
            "Средняя уверенность распознавания")
        add("success_rate", overview["jobs"]["success_rate"], "",
            "Доля успешно завершённых заданий")

        for row in self.by_model("day"):
            # Значение метки экранируется: имя модели приходит из запроса и
            # без этого позволяло бы вписать в вывод поддельные метрики.
            label = 'model="{}"'.format(_escape_label(row["model"]))
            add("model_jobs", row["jobs"], label)
            add("model_rtf_avg", row["rtf_avg"], label)
            add("model_success_rate", row["success_rate"], label)

        samples = self.db.system_samples(time.time() - 300, limit=200)
        if samples:
            sample = samples[-1]
            add("cpu_percent", sample.get("cpu_percent"))
            add("ram_used_mb", sample.get("ram_used_mb"))
            add("gpu_percent", sample.get("gpu_percent"))
            add("gpu_memory_mb", sample.get("gpu_mem_mb"))
            add("disk_free_gb", sample.get("disk_free_gb"))

        self._prometheus_content(insights, add)
        return "\n".join(lines) + "\n"

    #: Метрики содержания: имя, откуда брать, пояснение. Одним перечнем —
    #: чтобы выгрузка и каталог мониторинга не разошлись: там они описаны
    #: теми же именами, и правило тревоги ссылается на имя.
    CONTENT_METRICS: tuple[tuple[str, str, str], ...] = (
        ("content_records", "records", "Разобранных записей за сутки"),
        ("content_sentiment_avg", "sentiment", "Средняя тональность разговоров"),
        ("content_negative_share", "negative_share",
         "Доля отрицательных разговоров, процентов"),
        ("content_alert_records", "alert_records",
         "Записей с упоминанием суда, жалоб и огласки"),
        ("content_commitments_open", "commitments_open",
         "Обещаний без названного срока"),
        ("content_compliance_avg", "compliance",
         "Средняя доля выполненных пунктов скрипта разговора"),
        ("content_interruptions_avg", "interruptions", "Перебиваний на запись"),
        ("content_silence_share", "silence_share", "Доля тишины в записях"),
        ("content_filler_rate", "filler_rate", "Доля слов-паразитов"),
        ("content_wpm_avg", "wpm", "Средний темп речи, слов в минуту"),
        ("content_talk_share_avg", "talk_share",
         "Доля времени речи оператора"),
        ("content_monologue_avg", "monologue_s",
         "Самый долгий монолог оператора, секунд, в среднем по записям"),
        ("content_reply_delay_avg", "reply_delay_s",
         "Пауза оператора перед ответом, секунд"),
        ("content_dead_air_avg", "dead_air_s",
         "Заметная тишина (паузы от порога content_dead_air_s), секунд на запись"),
        ("content_objections_unhandled_share", "objections_unhandled_share",
         "Доля возражений клиента без отработки, процентов"),
        ("content_agent_score_avg", "agent_score",
         "Средний балл оператора, от 0 до 100"),
        ("content_empathy_avg", "empathy",
         "Средний индекс эмпатии оператора, от −100 до +100"),
        ("content_violation_records", "violation_records",
         "Записей с нарушениями оператора (стоп-слова и другие категории вида «нарушение»)"),
    )

    def _prometheus_content(self, insights: Any, add: Any) -> None:
        """Метрики содержания за сутки плюс состояние разбора.

        Сутки, а не «за всё время»: система мониторинга строит ряд сама, и
        значение, посчитанное по всему архиву, менялось бы на третьем знаке
        в год — то есть ни одно правило по нему никогда не сработает.

        Сбой этой части не должен ронять выгрузку метрик целиком: Prometheus
        считает пустой ответ падением цели, и тогда из-за разбора содержания
        сервер выглядел бы недоступным.
        """
        if insights is None:
            return
        try:
            свод = insights.summary("day")
            состояние = insights.index.status() if insights.index else {}
        except Exception as exc:                             # noqa: BLE001
            log.warning("Метрики содержания не собраны: %s", exc)
            return
        if not свод.get("records"):
            return
        for имя, ключ, пояснение in self.CONTENT_METRICS:
            add(имя, свод.get(ключ), "", пояснение)
        add("content_analyzed", состояние.get("analyzed"), "",
            "Записей архива, для которых разбор посчитан")
        add("content_pending", состояние.get("pending"), "",
            "Записей архива, ожидающих разбора")
        # Категории обращений — по метке на категорию с хотя бы одной записью
        # за сутки, и срабатывания трекеров по журналу событий.
        try:
            категории = insights.categories("day")
        except Exception as exc:                             # noqa: BLE001
            log.warning("Метрики категорий не собраны: %s", exc)
            return
        # Пояснение — только у первой строки метрики: HELP и TYPE в формате
        # Prometheus стоят один раз на имя, а не на каждую метку.
        первая = True
        for к in категории.get("items") or []:
            if к.get("records") and к.get("share") is not None:
                # Метки — через экранирование: имя категории задаёт человек, и
                # кавычка в нём ломала весь снимок метрик, а не одну строку.
                add("content_category_share", round(float(к["share"]) / 100.0, 4),
                    f'category="{_escape_label(к["id"])}",'
                    f'kind="{_escape_label(к.get("kind"))}"',
                    "Доля разобранных записей за сутки в категории обращения"
                    if первая else "")
                первая = False
        первая = True
        for т in категории.get("trackers") or []:
            add("content_tracker_hits", т.get("hits"),
                f'category="{_escape_label(т["category"])}"',
                "Срабатываний трекера за сутки" if первая else "")
            первая = False
