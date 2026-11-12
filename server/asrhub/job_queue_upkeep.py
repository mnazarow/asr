"""Фоновое обслуживание очереди: отметка жизни, возврат брошенных
заданий, сторож зависших, подъём умерших воркеров, уборка временных
файлов и замеры нагрузки.

Вынесено из `job_queue.py` примесью к `JobQueue`. Сроки — здесь же;
`job_queue` по-прежнему отдаёт их имена.
"""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import settings_access as S
from .db import SAMPLE_PERIOD_S
from .db_util import now
from .errors import EngineHung
from .instance import HOSTNAME, INSTANCE_ID, process_alive
from .job_status import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RETRY,
    STATUS_RUNNING,
    WorkerState,
)
from .logging_setup import get_logger
from .monitoring.collector import RUNTIME

if TYPE_CHECKING:
    from .db import Database
    from .engines import EngineRegistry

log = get_logger("queue")

#: Через сколько секунд без отметки задание считается брошенным. Пять минут
#: с запасом покрывают паузу на выгрузке большой модели: отметка ставится
#: на каждом шаге конвейера, а самый долгий из них — загрузка весов.
STALE_AFTER_S = 300.0

#: Как часто отдельный поток подтверждает, что идущие задания живы.
#:
#: Раньше отметку ставил только обработчик прогресса, а шагов, которые идут
#: без прогресса дольше пяти минут, хватает: второй воркер ждёт, пока
#: первый дочитает длинный файл той же моделью; подготовка двухчасовой
#: записи — один вызов ffmpeg; половина движков сообщает прогресс только в
#: начале и в конце. Сосед на общей базе принимал живое задание за
#: брошенное, возвращал его в очередь и считал второй раз, а после пары
#: таких кругов снимал с «instance_lost» — задание, которое всё это время
#: честно считалось.
HEARTBEAT_S = 30.0

#: Через сколько секунд своё задание «выполняется» без живого исполнителя в
#: этом процессе возвращается в очередь (см. `_сверить_свои`). С запасом на
#: зазор между захватом задания и пометкой «идёт».
СИРОТА_ЧЕРЕЗ_С = 2 * HEARTBEAT_S

#: Временные файлы старше этого в каталоге временных файлов сервера —
#: брошенные: падение процесса посреди задания, выгрузка DOCX, черновики
#: копий. Рабочие каталоги идущих заданий не трогаются никогда.
ВОЗРАСТ_ВРЕМЕННЫХ_С = 86400.0

#: Начала имён во временном каталоге, которые даёт сам сервер: рабочие
#: каталоги и выгрузки заданий, приём записей по ссылке (phone_asr), поток
#: диктовки, распаковка копий, временные каталоги движков.
СВОИ_ВРЕМЕННЫЕ = ("job_", "phone-", "stream-", "asrhub-", "nemo-", "gigaam-",
                  "voxtral-", "whispercpp-")


class ОбслуживаниеОчереди:
    """Примесь к `JobQueue`: служебные циклы и уборка."""

    if TYPE_CHECKING:
        db: Database
        settings: Any
        registry: EngineRegistry
        content_index: Any
        _lock: threading.RLock
        _stop: threading.Event
        _wake: Any
        _workers: list[threading.Thread]
        _states: dict[int, WorkerState]
        _worker_count: int
        _cancelled: set[str]
        _retiring: set[int]
        _running: dict[str, float]
        _идут: set[str]
        _потоки: dict[int, threading.Thread]
        _started: bool
        _insights_obj: Any
        _analytics_obj: Any

        def _emit(self, kind: str, data: dict[str, Any]) -> None: ...
        def _send_webhook(self, job_id: str) -> None: ...
        def _дослать_уведомления(self) -> int: ...
        def _уведомление_ждёт(self, job: dict[str, Any], *,
                              кроме_отката: bool = False) -> dict[str, Any]: ...
        def _release_slot(self, job_id: str, model: str) -> None: ...
        def _итог_потерянного(self, job_id: str, причина: str) -> None: ...
        def _каталог_временных(self) -> Path: ...
        def _отобрано(self, job_id: str) -> None: ...
        def _поднять_воркера(self, index: int) -> None: ...
        def _предел_повторов(self, job: dict[str, Any]) -> int: ...

    def _reclaim_stale_jobs(self) -> None:
        """Возвращает в очередь задания экземпляров, которые перестали отвечать.

        Сервер, убитый по нехватке памяти или остановленный вместе с
        машиной, оставляет свои задания в состоянии «выполняется» навсегда:
        пользователь видит вечные 40 % и не может ни дождаться, ни понять,
        что случилось. Раз отметка жизни устарела — задание никто не
        считает, и его можно отдать заново.

        Свои задания не трогаем: за них отвечает этот процесс, и его
        собственный поток может просто задержаться на загрузке весов.
        """
        cutoff = now() - STALE_AFTER_S
        try:
            stale = self.db.query(
                "SELECT id, instance_id, retries FROM jobs "
                "WHERE status=? AND instance_id IS NOT NULL AND instance_id<>? "
                "  AND COALESCE(heartbeat_at, started_at, 0)<? LIMIT 100",
                (STATUS_RUNNING, INSTANCE_ID, cutoff))
        except Exception as exc:                        # noqa: BLE001
            log.debug("Проверка брошенных заданий не удалась: %s", exc)
            return
        for row in stale:
            job_id = row["id"]
            retries = int(row["retries"] or 0)
            # Возврат — это попытка, и её надо считать. Иначе задание,
            # роняющее сервер (а «убит по нехватке памяти» обычно повторяется
            # на том же файле), возвращалось в очередь без конца и убивало
            # экземпляры один за другим: ни отказа, ни записи об ошибке, ни
            # сигнала администратору. Обычный путь повторов сюда не доходит —
            # процесс умирает раньше, чем успевает его пройти.
            job = self.db.get_job(job_id) or {}
            limit = self._предел_повторов(job)
            if retries >= limit:
                given_up = self.db.update_job_if_status(
                    job_id, [STATUS_RUNNING],
                    status=STATUS_FAILED, finished_at=now(),
                    stage="", progress=0.0,
                    instance_id=None, heartbeat_at=None,
                    **self._уведомление_ждёт(job, кроме_отката=True),
                    error_code="instance_lost",
                    error_message=(f"Экземпляр «{row['instance_id']}» перестал "
                                   f"отвечать, попыток израсходовано: {retries}."),
                    error_hint=("Задание возвращалось в очередь и снова обрывало "
                                "обработку. Проверьте журнал сервера и память: "
                                "чаще всего так выглядит нехватка памяти на "
                                "конкретной записи. Повторить вручную: "
                                "POST /api/jobs/{id}/retry"))
                if given_up:
                    log.error("Задание %s снято: экземпляры теряются на нём "
                              "%d раз подряд", job_id, retries,
                              extra={"job_id": job_id})
                    self.db.add_event(
                        job_id, "failed",
                        f"Задание снято: экземпляры теряются на нём {retries} "
                        "раз подряд")
                    self._итог_потерянного(
                        job_id, "Повторное распознавание не удалось: экземпляры "
                                "теряются на этой записи")
                continue

            returned = self.db.update_job_if_status(
                job_id, [STATUS_RUNNING],
                status=STATUS_QUEUED, stage="возвращено в очередь", progress=0.0,
                started_at=None, instance_id=None, heartbeat_at=None,
                retries=retries + 1, queued_at=now())
            if returned:
                log.warning("Задание %s возвращено в очередь: экземпляр «%s» "
                            "не отвечает (попытка %d из %d)", job_id,
                            row["instance_id"], retries + 1, limit,
                            extra={"job_id": job_id})
                self.db.add_event(
                    job_id, "reclaimed",
                    f"Экземпляр «{row['instance_id']}» перестал отвечать — "
                    f"задание возвращено в очередь (попытка {retries + 1} "
                    f"из {limit})")
                self._wake.set()

    def _heartbeat_loop(self) -> None:
        """Раз в `HEARTBEAT_S` подтверждает, что идущие задания живы.

        Отметка ставится условной записью — только пока задание за нами. Не
        записалось — значит, его отменили на соседнем сервере, удалили или
        уже отдали другому экземпляру, и досчитывать его незачем.
        """
        while not self._stop.wait(timeout=HEARTBEAT_S):
            self._отметить_живые()
            self._отметиться()

    def _отметиться(self) -> None:
        """Отметка «этот сервер жив» в общей базе — и без идущих заданий.

        По заданиям видно только того, кто сейчас считает. Простаивающий
        сосед был невидим, и смена режима журнала, восстановление базы и
        VACUUM не знали, что над базой работает кто-то ещё.
        """
        try:
            self.db.instance_beat(workers=self._worker_count)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Отметка экземпляра не поставлена: %s", exc)

    def _отметить_живые(self) -> None:
        with self._lock:
            идут = list(self._идут)
        for job_id in идут:
            try:
                своё = self.db.update_job_if_status(
                    job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID,
                    heartbeat_at=now())
            except Exception as exc:                        # noqa: BLE001
                # Занятая база — не повод объявлять задание чужим: следующий
                # оборот попробует снова, а срок устаревания в десять раз
                # длиннее шага.
                log.debug("Отметка жизни %s не поставлена: %s", job_id, exc)
                continue
            if not своё:
                self._отобрано(job_id)

    def _janitor_loop(self) -> None:
        last_cleanup = 0.0
        failures = 0
        while not self._stop.wait(timeout=SAMPLE_PERIOD_S):
            # Каждый шаг в своей обёртке: раньше сбой первого (например,
            # ошибка при выгрузке модели с видеокарты) означал, что уборка
            # хранилища не выполняется НИКОГДА — last_cleanup не обновлялся,
            # и диск не чистился, пока сбой не пройдёт сам.
            сбой_на_обороте = False
            try:
                for step_fn in (self.registry.collect_idle, self._sample_system):
                    try:
                        step_fn()
                    except Exception as exc:            # noqa: BLE001
                        # Пометка нужна, чтобы ветка else ниже не объявила
                        # восстановление: сбой шага ловится здесь, внешний
                        # try завершается штатно, и счётчик обнулялся на
                        # каждом обороте. Ограничитель записей не включался
                        # никогда, а в журнал каждые двадцать секунд шла пара
                        # строк «дал сбой (1-й раз)» и «восстановился» —
                        # ровно то заливание, против которого он и написан.
                        сбой_на_обороте = True
                        failures += 1
                        if failures <= 3 or failures % 180 == 0:
                            log.warning("Служебный шаг %s дал сбой (%d-й раз): %s",
                                        getattr(step_fn, "__name__", step_fn), failures, exc)
                self._reclaim_stale_jobs()
                # Свои «выполняется» без исполнителя, умершие воркеры и
                # недошедшие уведомления — каждый оборот: это дешёвые
                # запросы по указателям, а цена пропуска — задание, висящее
                # «выполняется» до перезапуска, и результат, не дошедший до
                # заказчика.
                for шаг in (self._сверить_свои, self._сторож_зависших,
                            self._поднять_умерших, self._дослать_уведомления):
                    try:
                        шаг()
                    except Exception as exc:            # noqa: BLE001
                        сбой_на_обороте = True
                        failures += 1
                        if failures <= 3 or failures % 180 == 0:
                            log.warning("Служебный шаг %s дал сбой (%d-й раз): %s",
                                        getattr(шаг, "__name__", шаг), failures, exc)
                # Обслуживание по расписанию: копия базы и сводка о работе.
                # Свой заход, а не внутри уборки: у уборки собственный час,
                # а у этих дел — собственные сроки из настроек, и связывать
                # их значит либо делать копию каждый час, либо не делать
                # вовсе, когда уборка отключена.
                try:
                    from .maintenance import run_scheduled  # noqa: PLC0415

                    run_scheduled(self.db, self.settings, self._analytics(),
                                  self._insights(), queue=self)
                except Exception as exc:                # noqa: BLE001
                    failures += 1
                    if failures <= 3 or failures % 180 == 0:
                        log.warning("Обслуживание по расписанию дало сбой "
                                    "(%d-й раз): %s", failures, exc)
                if time.time() - last_cleanup > 3600:
                    from .maintenance import calls_file_days, retention_days  # noqa: PLC0415

                    # Брошенные временные файлы — тем же часовым заходом:
                    # `tmp/` не чистил никто, а после падения процесса там
                    # оставались рабочие каталоги заданий — самые большие
                    # файлы сервера.
                    try:
                        self._подмести_временные()
                    except Exception as exc:            # noqa: BLE001
                        log.warning("Временные файлы не убраны: %s", exc)

                    retention = retention_days(self.settings)
                    removed = self.db.cleanup(
                        results_days=retention,
                        audit_days=S.integer(self.settings, "audit_days", 365),
                        calls_file_days=calls_file_days(self.settings))
                    if any(removed.values()):
                        log.info("Очистка хранилища: %s", removed)
                    # Если уборка упёрлась в предел на заход, следующий
                    # делаем не через час, а через минуту.
                    last_cleanup = time.time() - 3540 if removed.get("more") else time.time()
            except Exception as exc:                    # noqa: BLE001
                # Раньше здесь стоял debug, и постоянно падающая очистка диска
                # не давала ни одной записи при штатном уровне INFO. Первые
                # сбои показываем, дальше переходим на debug, чтобы не залить
                # журнал одной и той же строкой раз в двадцать секунд.
                failures += 1
                if failures <= 3 or failures % 180 == 0:
                    log.warning("Служебный цикл дал сбой (%d-й раз): %s", failures, exc)
                else:
                    log.debug("Служебный цикл: %s", exc)
            else:
                if failures and not сбой_на_обороте:
                    log.info("Служебный цикл восстановился после %d сбоев", failures)
                    failures = 0

    def _сверить_свои(self) -> int:
        """Свои «выполняется» без исполнителя — обратно в очередь без траты попытки.

        Свои задания экземпляр не подбирал никогда: возврат брошенных берёт
        только чужие, `recover()` идёт при запуске, а `stop()` возвращал лишь
        те, что в `_идут`. Задание оставалось «выполняется» за нами вне
        `_running` и `_идут`, если запись исхода не удалась и в обработчике
        отказа (диск полон, база занята дольше busy_timeout), если сорвалось
        чтение после захвата или поток воркера умер, — и висело так до
        перезапуска, а тот ещё и списывал попытку. Попытку здесь не тратим:
        процесс жив, отказ был в записи исхода, а не в самом задании.

        Задание, захваченное мгновение назад, ещё может не значиться у
        воркера, поэтому возвращаются только те, чья отметка жизни старше
        `СИРОТА_ЧЕРЕЗ_С`.
        """
        строки = self.db.query(
            "SELECT id FROM jobs WHERE status=? AND instance_id=? "
            "  AND COALESCE(heartbeat_at, started_at, 0) < ? LIMIT 200",
            (STATUS_RUNNING, INSTANCE_ID, now() - СИРОТА_ЧЕРЕЗ_С))
        возвращено = 0
        for строка in строки:
            job_id = str(строка["id"])
            with self._lock:
                занято = job_id in self._running or job_id in self._идут
            if занято:
                continue
            вернулось = self.db.update_job_if_status(
                job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID,
                status=STATUS_QUEUED, stage="возвращено в очередь", progress=0.0,
                started_at=None, instance_id=None, heartbeat_at=None, queued_at=now())
            if вернулось:
                возвращено += 1
                log.warning("Задание %s возвращено в очередь: оно числилось за этим "
                            "сервером, но его не считал ни один воркер", job_id,
                            extra={"job_id": job_id})
                self.db.add_event(job_id, "requeued",
                                  "Задание числилось выполняющимся, но его никто не "
                                  "считал (сбой записи исхода) — возвращено в очередь "
                                  "без траты попытки")
        if возвращено:
            self._wake.set()
        return возвращено

    #: Сколько ждать сверх предела задания, прежде чем снять зависшее.
    ЗАПАС_ЗАВИСАНИЯ_С = 120.0

    def _сторож_зависших(self) -> int:
        """Снимает задание, движок которого не отвечает дольше предела.

        Предел задания (`job_timeout_s`) проверяется на докладах движка о
        ходе работы. Движок, зависший внутри своей библиотеки (драйвер
        видеокарты, бесконечный цикл декодера), не докладывает ничего — и
        задание висело «выполняется» вечно, занимая слот, а очередь за ним
        стояла. Теперь задание, у которого вышел предел с запасом и давно не
        было доклада, отмечается неудачным, слот отдаётся новому потоку
        воркера, а экземпляр модели забывается (следующее задание загрузит
        свой — у зависшего держится замок). Сам зависший поток прервать
        нельзя: если он когда-нибудь вернётся, он молча завершится.
        """
        if self._stop.is_set():
            return 0
        сейчас, миг = time.time(), time.monotonic()
        with self._lock:
            состояния = list(self._states.items())
        снято = 0
        for index, state in состояния:
            if not state.busy or not state.job_id or state.срок_с <= 0:
                continue
            запас = max(self.ЗАПАС_ЗАВИСАНИЯ_С, state.срок_с * 0.25)
            if сейчас - state.started_at < state.срок_с + запас \
                    or миг - state.доклад < запас:
                continue
            try:
                if self._снять_зависшее(index, state):
                    снято += 1
            except Exception as exc:                         # noqa: BLE001
                log.warning("Зависшее задание %s не снято: %s", state.job_id, exc)
        return снято

    def _снять_зависшее(self, index: int, state: WorkerState) -> bool:
        job_id, model = str(state.job_id), str(state.model or "")
        # Процесс распознавания — завершить: память и замок модели уходят с
        # ним, а поток воркера получает «движок не отвечает» и сам проводит
        # задание как неудачное. Поток заменять не нужно.
        дочерний = getattr(self, "_дочерние", {}).get(index)
        if дочерний is not None and дочерний.жив():
            log.error("Воркер %d: движок не отвечает по заданию %s дольше предела %.0f с — "
                      "процесс распознавания завершается", index, job_id, state.срок_с,
                      extra={"job_id": job_id, "model": model})
            state.доклад = time.monotonic()
            return дочерний.завершить("движок не отвечает")
        job = self.db.get_job(job_id) or {"id": job_id}
        ошибка = EngineHung(int(state.срок_с), миг_доклада=time.monotonic() - state.доклад)
        снято = self.db.update_job_if_status(
            job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID,
            status=STATUS_FAILED, finished_at=now(), stage="движок не отвечает",
            error_code=ошибка.code, error_message=ошибка.message, error_hint=ошибка.hint,
            instance_id=None, heartbeat_at=None, **self._уведомление_ждёт(job))
        log.error("Воркер %d: движок не отвечает по заданию %s дольше предела %.0f с — "
                  "задание снято, поток заменён", index, job_id, state.срок_с,
                  extra={"job_id": job_id, "model": model})
        with self._lock:
            self._cancelled.add(job_id)
            self._states[index] = WorkerState(index=index)
            self._поднять_воркера(index)
        self._release_slot(job_id, model)
        try:
            self.registry.забыть_занятые(model)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Экземпляр модели %s не забыт: %s", model, exc)
        if снято:
            RUNTIME.inc("asrhub_jobs_total", {"status": "failed"})
            try:
                self.db.add_event(job_id, "failed", ошибка.message)
            except Exception:                                # noqa: BLE001
                pass
            self._emit("job.failed", {"id": job_id, "error": ошибка.to_dict()})
            if job.get("webhook_url"):
                self._send_webhook(job_id)
        self._wake.set()
        return снято

    def _поднять_умерших(self) -> int:
        """Поднимает заново поток воркера, умерший не по команде.

        Потоки воркеров не перезапускались: `BaseException` из задания
        (например, `SystemExit` из библиотеки движка) убивал поток, и сервер
        жил с меньшим числом воркеров до перезапуска, показывая в состоянии
        полное число.
        """
        if self._stop.is_set() or not self._started:
            return 0
        поднято = 0
        with self._lock:
            for index, поток in list(self._потоки.items()):
                if поток.is_alive() or index >= self._worker_count \
                        or index in self._retiring or index not in self._states:
                    continue
                log.error("Воркер %d завершился непредвиденно — поднимаю заново", index)
                self._states[index] = WorkerState(index=index)
                self._поднять_воркера(index)
                поднято += 1
            self._workers = [t for t in self._workers if t.is_alive()]
        return поднято

    def _подмести_временные(self, *, при_запуске: bool = False) -> dict[str, int]:
        """Убирает брошенное в каталоге временных файлов сервера.

        * рабочие каталоги заданий этого сервера, которые сейчас никто не
          считает, — при запуске сразу (прежний процесс этого экземпляра
          умер), дальше — старше `ВОЗРАСТ_ВРЕМЕННЫХ_С`;
        * рабочие каталоги чужих экземпляров и прочие файлы (выгрузки
          DOCX, черновики приёма записей, поток диктовки) — только старше
          `ВОЗРАСТ_ВРЕМЕННЫХ_С`: каталог бывает общим у нескольких серверов.

        Рабочие каталоги идущих заданий не трогаются никогда. И только своё
        — по именам, которые даёт сам сервер (`СВОИ_ВРЕМЕННЫЕ`): `temp_dir`
        может указывать на общий системный каталог, и чужие файлы в нём
        уборке не принадлежат.
        """
        корни = {self._каталог_временных()}
        запасной = getattr(getattr(self.settings, "paths", None), "tmp", None)
        if запасной:
            корни.add(Path(запасной))
        with self._lock:
            идут = set(self._идут) | set(self._running)
        try:
            чужие_идут = {str(с["id"]) for с in self.db.query(
                "SELECT id FROM jobs WHERE status=?", (STATUS_RUNNING,))}
        except Exception:                                   # noqa: BLE001
            чужие_идут = set()
        from .phone_compat import safe_path_key  # noqa: PLC0415

        машина = safe_path_key(HOSTNAME) + "_"

        def мёртвого_процесса(имя: str) -> bool:
            # Рабочий каталог процесса этой машины, которого больше нет:
            # «<задание>.<машина>_<pid>». Его прежний хозяин не вернётся.
            хвост = имя.split(".", 1)[1] if "." in имя else ""
            номер_процесса = хвост[len(машина):] if хвост.startswith(машина) else ""
            return (номер_процесса.isdigit()
                    and not process_alive(f"{HOSTNAME}:{номер_процесса}"))

        граница = time.time() - ВОЗРАСТ_ВРЕМЕННЫХ_С
        итог = {"dirs": 0, "files": 0}
        for корень in корни:
            if not корень.is_dir():
                continue
            for путь in корень.iterdir():
                имя = путь.name
                if not имя.startswith(СВОИ_ВРЕМЕННЫЕ):
                    continue
                номер = имя.split(".", 1)[0]
                if номер in идут or номер in чужие_идут:
                    continue
                try:
                    старый = путь.stat().st_mtime < граница
                except OSError:
                    continue
                if not (старый or (при_запуске and мёртвого_процесса(имя))):
                    continue
                try:
                    if путь.is_dir() and not путь.is_symlink():
                        shutil.rmtree(путь, ignore_errors=True)
                        итог["dirs"] += 1
                    else:
                        путь.unlink(missing_ok=True)
                        итог["files"] += 1
                except OSError as exc:
                    log.debug("Временный файл %s не убран: %s", путь, exc)
        if итог["dirs"] or итог["files"]:
            log.info("Убрано брошенных временных файлов: каталогов %d, файлов %d",
                     итог["dirs"], итог["files"])
        return итог

    def _insights(self) -> Any:
        """Свод по содержанию для сводки по расписанию.

        Заводится по требованию и на том же объекте разбора, что и раздел:
        второй объект означал бы второй снимок корпусных частот, и одна и
        та же запись считалась бы по-разному в зависимости от того, кто её
        посчитал. Без разбора (сервер поднят в урезанном режиме) сводка
        просто выходит без раздела о разговорах.
        """
        if self.content_index is None:
            return None
        готовый = getattr(self, "_insights_obj", None)
        if готовый is None:
            from .insights import Insights  # noqa: PLC0415

            готовый = Insights(self.db, self.content_index)
            self._insights_obj = готовый
        return готовый

    def _analytics(self) -> Any:
        """Аналитика для сводки. Заводится по требованию и один раз.

        Держать её в конструкторе незачем: сводка отключена по умолчанию, и
        на серверах, где её не включили, объект не понадобится никогда.
        """
        готовая = getattr(self, "_analytics_obj", None)
        if готовая is None:
            from .analytics import Analytics  # noqa: PLC0415

            готовая = Analytics(self.db)
            self._analytics_obj = готовая
        return готовая

    def _sample_gpus(self) -> list[dict[str, Any]]:
        """Замер по каждой видеокарте, а не только по первой.

        Прежний сбор читал `splitlines()[0]`, то есть вторая карта для
        сервера не существовала. Температура и потребление приходят тем же
        запросом даром, а без них «нагрузка на видеокарту» — это загрузка в
        процентах и больше ничего: ни троттлинга, ни упора в лимит мощности
        по ней не видно.
        """
        try:
            from .hardware import _run

            out = _run(["nvidia-smi",
                        "--query-gpu=index,name,utilization.gpu,memory.used,"
                        "memory.total,temperature.gpu,power.draw,power.limit",
                        "--format=csv,noheader,nounits"])
        except Exception:                                   # noqa: BLE001
            return []
        if not out:
            return []

        def число(текст: str) -> float | None:
            # nvidia-smi отдаёт «[N/A]» там, где датчика нет: у части карт
            # нет телеметрии по мощности, и это не повод терять всю строку.
            try:
                return float(текст)
            except ValueError:
                return None

        карты: list[dict[str, Any]] = []
        for line in out.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 5 or not parts[0].isdigit():
                continue
            карты.append({
                "gpu": int(parts[0]),
                "name": parts[1],
                "util_percent": число(parts[2]),
                "mem_used_mb": число(parts[3]),
                "mem_total_mb": число(parts[4]),
                "temperature_c": число(parts[5]) if len(parts) > 5 else None,
                "power_w": число(parts[6]) if len(parts) > 6 else None,
                "power_limit_w": число(parts[7]) if len(parts) > 7 else None,
            })
        return карты

    def _sample_system(self) -> None:
        момент = now()
        sample: dict[str, Any] = {
            "ts": момент,
            "queue_depth": self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY]),
            "active_jobs": len(self._running),
        }
        try:
            import psutil  # type: ignore

            sample["cpu_percent"] = psutil.cpu_percent(interval=None)
            vm = psutil.virtual_memory()
            sample["ram_used_mb"] = round((vm.total - vm.available) / 1024 / 1024)
            sample["ram_total_mb"] = round(vm.total / 1024 / 1024)
        except Exception:
            try:
                with open("/proc/meminfo", encoding="utf-8") as fh:
                    info = {}
                    for line in fh:
                        key, _, rest = line.partition(":")
                        parts = rest.strip().split()
                        if parts and parts[0].isdigit():
                            info[key] = int(parts[0])
                total = info.get("MemTotal", 0) / 1024
                avail = info.get("MemAvailable", 0) / 1024
                sample["ram_total_mb"] = round(total)
                sample["ram_used_mb"] = round(total - avail)
            except OSError:
                pass
        gpus = self._sample_gpus()
        if gpus:
            # В system_samples под видеокарту три колонки — оставляем там
            # первую карту, как было: на этих колонках держатся прежние
            # метрики Prometheus и графики аналитики. Полная картина по всем
            # картам лежит рядом, в своей таблице.
            первая = gpus[0]
            sample["gpu_percent"] = первая.get("util_percent")
            sample["gpu_mem_mb"] = первая.get("mem_used_mb")
            sample["gpu_mem_total"] = первая.get("mem_total_mb")
        try:
            import shutil as shutil_mod

            usage = shutil_mod.disk_usage(str(self.settings.paths.data))
            sample["disk_free_gb"] = round(usage.free / 1024 ** 3, 2)
        except Exception:
            pass
        self.db.add_system_sample(sample)
        if gpus:
            # Тем же временем, что и общий замер: иначе ряды по картам и по
            # системе не совместить на одной оси.
            self.db.add_gpu_samples(момент, gpus)
