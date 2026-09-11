"""Разбор содержания записей: расчёт, хранение, пересчёт архива.

Пакет `content` умеет разобрать одну расшифровку и ничего не знает ни о базе,
ни о настройках — это чистые функции над текстом. Здесь всё остальное: когда
считать, с какими словарями, куда положить и как разобрать то, что уже
накоплено.

Разделение не формальное. Разбор — самая изменчивая часть раздела: словари
пополняются, правила уточняются, скрипт разговора у каждого свой. Держать его
свободным от базы значит иметь возможность прогнать новый словарь по тысяче
расшифровок в отдельном скрипте и посмотреть, что изменилось, — не поднимая
сервер.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from . import content
from .db import Database

log = logging.getLogger("asrhub.content")

#: Сколько живёт снимок корпусной частоты основ. Знаменатель TF-IDF меняется
#: медленно: на архиве в десять тысяч записей сотня новых сдвигает вес слова в
#: третьем знаке. Считать его на каждую запись — это лишний проход по таблице
#: основ там, где ответ заведомо тот же.
СРОК_ЧАСТОТ = 300.0

#: Пауза между заходами разбора архива. Разбор держит блокировку записи, и без
#: паузы фоновая работа соревновалась бы за неё с обновлениями прогресса
#: заданий — то есть замедляла бы то, ради чего сервер и стоит.
ПАУЗА_РАЗБОРА = 5.0

#: Пауза, когда разбирать нечего. Заходить в базу каждые пять секунд ради
#: ответа «ничего не изменилось» незачем.
ПАУЗА_ПРОСТОЯ = 120.0


class ContentIndex:
    """Признаки содержания: расчёт при завершении задания и пересчёт архива."""

    def __init__(self, db: Database, settings: Any):
        self.db = db
        self.settings = settings
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._частоты: dict[str, int] = {}
        self._размер_корпуса = 0
        self._частоты_на = 0.0
        #: Последняя ошибка разбора — её показывает состояние раздела. Без
        #: неё выключенный по сбою разбор выглядел бы просто как «архив ещё
        #: не разобран», и разбираться было бы не с чем.
        self.last_error: str | None = None
        self.backfilled = 0
        self._категории: list[Any] = []
        self._категории_ключ = ""

    # --- корпусная частота ------------------------------------------------

    def corpus_frequency(self, *, fresh: bool = False) -> tuple[dict[str, int], int]:
        """Снимок частот основ по корпусу — знаменатель TF-IDF."""
        with self._lock:
            устарел = time.time() - self._частоты_на > СРОК_ЧАСТОТ
            if fresh or устарел or not self._частоты_на:
                try:
                    self._частоты, self._размер_корпуса = self.db.document_frequency()
                except Exception as exc:                     # noqa: BLE001
                    # Без частот ключевые слова считаются по частоте внутри
                    # записи — хуже, но работает. Ронять из-за этого разбор
                    # целиком нельзя.
                    log.warning("Не удалось получить частоты основ: %s", exc)
                    self._частоты, self._размер_корпуса = {}, 0
                self._частоты_на = time.time()
            return self._частоты, self._размер_корпуса

    def forget_frequency(self) -> None:
        """Забыть снимок частот — после пересчёта архива он заведомо не тот."""
        with self._lock:
            self._частоты_на = 0.0

    # --- настройки разбора ------------------------------------------------

    def _скрипт(self) -> list[dict[str, Any]] | None:
        значение = self.settings.get("content_script") if self.settings else None
        return значение if isinstance(значение, list) and значение else None

    def _оператор(self) -> str | None:
        значение = str((self.settings.get("content_agent_speaker")
                        if self.settings else "") or "").strip()
        return значение or None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.get("content_analysis", True)) if self.settings else True

    def _мат(self) -> bool:
        """Считать ли нецензурную лексику. По умолчанию — нет, см. словарь."""
        return bool(self.settings.get("content_profanity", False)) if self.settings else False

    def categories(self) -> list[Any]:
        """Набор категорий, разобранный один раз на значение настройки.

        Пустая настройка — готовый набор, как у скрипта разговора. Разбор
        правил дешёвый, но делать его на каждую запись архива незачем:
        набор меняется раз в неделю, а записей — тысячи в день. Ключ кеша —
        само значение настройки: сменилось — разберём заново.
        """
        from .content import categories as категории  # noqa: PLC0415

        значение = self.settings.get("content_categories") if self.settings else None
        сырой = значение if isinstance(значение, list) and значение else категории.ГОТОВЫЕ
        ключ = json.dumps(сырой, ensure_ascii=False, sort_keys=True)
        with self._lock:
            if self._категории_ключ != ключ:
                self._категории = категории.compile(сырой)
                self._категории_ключ = ключ
            return self._категории

    def categories_own(self) -> bool:
        """Сохранён ли свой набор категорий (иначе действует готовый)."""
        значение = self.settings.get("content_categories") if self.settings else None
        return isinstance(значение, list) and bool(значение)

    # --- разбор одной записи ----------------------------------------------

    def analyze_job(self, job_id: str, *, job: dict[str, Any] | None = None,
                    segments: list[dict[str, Any]] | None = None,
                    save: bool = True) -> dict[str, Any] | None:
        """Разбирает одну запись и (по умолчанию) кладёт результат в базу."""
        задание = job or self.db.get_job(job_id)
        if not задание:
            return None
        текст = str(задание.get("text") or "")
        if not текст.strip():
            return None
        реплики = segments if segments is not None else self.db.get_segments(job_id)
        частоты, размер = self.corpus_frequency()
        разбор = content.analyze(
            text=текст, segments=реплики,
            duration_s=float(задание.get("media_duration_s") or 0.0),
            script=self._скрипт(), agent_speaker=self._оператор(),
            document_frequency=частоты, corpus_size=размер,
            profanity=self._мат(), categories=self.categories())
        свод, основы = content.features(разбор)
        if save:
            self.db.save_content(job_id, свод, основы)
            # Пересчёт записи пересчитывает и здоровье распознавания: после
            # смены ожидаемого числа говорящих иначе пришлось бы ждать
            # фонового разбора, который эту запись уже прошёл.
            try:
                self._оценить_качество(job_id, реплики)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Признаки качества для %s не пересчитаны: %s",
                            job_id, exc, extra={"job_id": job_id})
        # Возвращаем то же, что легло бы в базу: `features` убирает из
        # разбора служебные основы для знаменателя TF-IDF. Без этого
        # карточка свежей записи приходила с лишним полем, а карточка
        # разобранной раньше — без него, и интерфейсу приходилось бы
        # знать, каким путём она пришла.
        return свод["detail"]

    def on_job_completed(self, job_id: str, job: dict[str, Any],
                         segments: list[dict[str, Any]] | None = None) -> None:
        """Разбор сразу после распознавания — в потоке воркера.

        Сбой разбора не должен трогать задание: запись распознана, результат
        сохранён и отдан, а признаки — надстройка над ним. Раньше здесь не
        было обёртки, и опечатка в пользовательском скрипте разговора роняла
        задание уже после того, как оно завершилось: клиент получал ошибку на
        готовый результат.
        """
        if not self.enabled:
            return
        try:
            разбор = self.analyze_job(job_id, job=job, segments=segments)
        except Exception as exc:                             # noqa: BLE001
            self.last_error = str(exc)
            log.warning("Разбор содержания задания %s не удался: %s", job_id, exc,
                        extra={"job_id": job_id})
            return
        try:
            self._трекеры(job_id, job, разбор or {})
        except Exception as exc:                             # noqa: BLE001
            log.warning("Трекеры по заданию %s не сработали: %s", job_id, exc,
                        extra={"job_id": job_id})

    def _трекеры(self, job_id: str, job: dict[str, Any], разбор: dict[str, Any]) -> None:
        """Категории с флагом «сообщать»: событие в журнал и вызов наружу.

        Только для свежих записей — при пересчёте архива не срабатывает:
        трекер отвечает на «сейчас прозвучало», а не на «когда-то было».
        Отправка наружу идёт отдельным потоком: это поток воркера, и ждать
        в нём чужой сервер значило бы задерживать следующее задание.
        """
        сработали = [и for и in (разбор.get("categories") or {}).get("items") or []
                     if и.get("count")]
        уведомлять = {к.id: к for к in self.categories() if к.notify}
        события = []
        for и in сработали:
            к = уведомлять.get(и["id"])
            if к is None:
                continue
            данные = {
                "job_id": job_id, "category": к.id, "label": к.label, "kind": к.kind,
                "count": и.get("count"), "first_s": и.get("first_s"),
                "hits": (и.get("hits") or [])[:3],
                "filename": job.get("filename"), "owner": job.get("owner"),
                "created_at": job.get("created_at"),
            }
            self.db.add_event(job_id, "tracker",
                              f"Сработал трекер «{к.label}»: {и.get('count')} совп., "
                              f"первое на {float(и.get('first_s') or 0):.0f} с", данные)
            события.append(данные)
        адрес = str(self.settings.get("tracker_url") or "").strip() if self.settings else ""
        if события and адрес:
            # Та же проверка, что у обратного вызова задания: адрес из
            # настроек уходит в urlopen, и внутренняя сеть для него закрыта.
            from .job_queue import check_outbound_url  # noqa: PLC0415

            try:
                check_outbound_url(адрес, bool(self.settings.get("webhook_allow_internal")))
            except Exception as exc:                         # noqa: BLE001
                log.warning("Адрес трекеров отвергнут: %s", exc)
                return
            threading.Thread(target=self._отправить, args=(адрес, события),
                             name="asrhub-tracker", daemon=True).start()

    @staticmethod
    def _отправить(адрес: str, события: list[dict[str, Any]]) -> None:
        from .maintenance import send_json  # noqa: PLC0415

        for событие in события:
            send_json({"event": "tracker", **событие}, адрес, what="трекер")

    def _оценить_качество(self, job_id: str, реплики: list[dict[str, Any]]) -> None:
        """Признаки подозрительной расшифровки — по уже поднятым сегментам."""
        from . import quality  # noqa: PLC0415

        ожидается = int(self.settings.get("quality_expected_speakers") or 0) \
            if self.settings else 0
        оценка = quality.assess(реплики, expected_speakers=ожидается)
        self.db.update_job(job_id, **quality.for_job(оценка))

    # --- пересчёт архива --------------------------------------------------

    def backfill_once(self, limit: int | None = None) -> int:
        """Разбирает порцию записей, у которых разбора нет или он устарел."""
        размер = limit or int(self.settings.get("content_backfill_batch") or 50)
        ожидают = self.db.content_pending(content.VERSION, размер)
        if not ожидают:
            return 0
        частоты, корпус = self.corpus_frequency()
        скрипт, оператор, мат = self._скрипт(), self._оператор(), self._мат()
        набор = self.categories()
        сделано = 0
        for запись in ожидают:
            if self._stop.is_set():
                break
            job_id = str(запись["id"])
            try:
                реплики = self.db.get_segments(job_id)
                разбор = content.analyze(
                    text=str(запись.get("text") or ""),
                    segments=реплики,
                    duration_s=float(запись.get("media_duration_s") or 0.0),
                    script=скрипт, agent_speaker=оператор,
                    document_frequency=частоты, corpus_size=корпус,
                    profanity=мат, categories=набор)
                свод, основы = content.features(разбор)
                self.db.save_content(job_id, свод, основы)
                # Здоровье распознавания у записей, сделанных до его
                # появления: те же сегменты уже подняты, второй раз ходить
                # за ними незачем.
                if запись.get("quality_flags") is None:
                    self._оценить_качество(job_id, реплики)
                сделано += 1
            except Exception as exc:                         # noqa: BLE001
                # Одна битая запись не должна останавливать разбор архива.
                # Но и крутиться на ней вечно нельзя: `content_pending`
                # отбирает по отсутствию разбора, и запись, на которой мы
                # каждый раз падаем, возвращалась бы в следующую же порцию.
                # Кладём пустой разбор текущей версии: из очереди она уйдёт,
                # а в разделе будет видна как неразобранная.
                self.last_error = f"{job_id}: {exc}"
                log.warning("Разбор записи %s не удался: %s", job_id, exc,
                            extra={"job_id": job_id})
                try:
                    self.db.save_content(
                        job_id, {"version": content.VERSION,
                                 "detail": {"error": str(exc)[:500]}}, {})
                except Exception:                            # noqa: BLE001
                    log.debug("Не удалось пометить запись %s", job_id)
        self.backfilled += сделано
        return сделано

    def recompute(self, job_ids: list[str] | None = None) -> dict[str, Any]:
        """Пересчёт по требованию: перечисленные записи или весь архив.

        Весь архив пересчитывается не здесь, а фоновым потоком: пересчёт ста
        тысяч записей в обработчике HTTP-запроса — это запрос, который висит
        полчаса и обрывается по тайм-ауту, оставив работу наполовину
        сделанной. Поэтому у записей просто снимается отметка версии, и
        дальше их разбирает тот же поток, что разбирает архив.
        """
        if job_ids:
            сделано = 0
            for job_id in job_ids:
                if self.analyze_job(job_id) is not None:
                    сделано += 1
            self.forget_frequency()
            return {"recomputed": сделано, "queued": 0}
        # Отметка версии — единственное, что отделяет разобранную запись от
        # ожидающей разбора. Обнулять её, а не удалять строки: строка с
        # прошлым разбором остаётся видна в разделе, пока не посчитан новый.
        сброшено = self.db.execute("UPDATE content SET version=0")
        self.forget_frequency()
        return {"recomputed": 0, "queued": int(сброшено)}

    def status(self, *, owner: str | list[str] | None = None) -> dict[str, Any]:
        """Состояние разбора: сколько посчитано, сколько ждёт, чем занят.

        Считается двумя счётчиками, а не снимком корпусных частот. Снимок
        сбрасывается после каждой порции фонового разбора, поэтому во время
        разбора — ровно тогда, когда интерфейс и опрашивает состояние раз в
        пятнадцать секунд — он не попадал в кеш ни разу, и каждый опрос
        делал полную группировку по таблице основ под общей блокировкой.
        """
        сведения = self.db.content_stats(content.VERSION, owner=owner)
        набор = self.categories()
        return {
            **сведения,
            "version": content.VERSION,
            "categories": len(набор),
            "categories_broken": [к.label for к in набор if к.error],
            "enabled": self.enabled,
            "backfill": bool(self.settings.get("content_backfill", True))
            if self.settings else True,
            "running": bool(self._thread and self._thread.is_alive()),
            "vocabulary": self.db.vocabulary_size(),
            "corpus": self.db.content_window_size(owner=owner),
            "backfilled": self.backfilled,
            "last_error": self.last_error,
        }

    # --- фоновый поток ----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="asrhub-content",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        поток, self._thread = self._thread, None
        if поток and поток.is_alive():
            поток.join(timeout=timeout)

    def _loop(self) -> None:
        сбоев = 0
        пауза = ПАУЗА_РАЗБОРА
        while not self._stop.wait(timeout=пауза):
            if not (self.enabled and (self.settings.get("content_backfill", True)
                                      if self.settings else True)):
                пауза = ПАУЗА_ПРОСТОЯ
                continue
            try:
                сделано = self.backfill_once()
            except Exception as exc:                         # noqa: BLE001
                сбоев += 1
                self.last_error = str(exc)
                if сбоев <= 3 or сбоев % 60 == 0:
                    log.warning("Разбор архива дал сбой (%d-й раз): %s", сбоев, exc)
                пауза = ПАУЗА_ПРОСТОЯ
                continue
            сбоев = 0
            if сделано:
                # Разбор архива меняет знаменатель TF-IDF заметно: первые
                # сотни записей могут увеличить корпус вдвое. Снимок частот
                # сбрасываем, иначе весь архив разобрался бы по частотам,
                # снятым на пустой базе.
                self.forget_frequency()
                пауза = ПАУЗА_РАЗБОРА
                log.debug("Разобрано записей архива: %d", сделано)
            else:
                пауза = ПАУЗА_ПРОСТОЯ
