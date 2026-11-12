"""Очередь заданий: планирование, воркеры, повторы, ограничения.

Возможности:
* четыре политики планирования (приоритет, короткие вперёд, справедливое
  разделение между пользователями, по сроку);
* приостановка и возобновление очереди целиком и отдельных заданий;
* отмена в любой момент, включая уже выполняющееся задание;
* автоматические повторы с экспоненциальной задержкой и уменьшением
  размера пакета при нехватке видеопамяти;
* ограничение параллельности глобально и по каждой модели;
* кеш результатов по содержимому файла и отпечатку настроек;
* восстановление после перезапуска: задания, застрявшие в состоянии
  «выполняется», возвращаются в очередь.
"""
from __future__ import annotations

import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import model_files
from . import settings_access as S
from .catalog import get_model
from .config import Settings
from .db import Database, new_id, now
from .engines import EngineRegistry
from .errors import (
    ASRHubError,
    ConfigError,
    JobNotFound,
    QueueFull,
    StorageError,
)
from .instance import HOSTNAME, process_alive
from .instance import INSTANCE_ID as _INSTANCE_ID
from .job_queue_notify import (  # noqa: F401 — прежние имена
    ПАУЗЫ_УВЕДОМЛЕНИЯ,
    ПОПЫТОК_ЗА_ЗАХОД,
    СРОК_ЗАХОДА_ДОСТАВКИ_С,
    УведомленияОчереди,
)
from .job_queue_upkeep import (  # noqa: F401 — прежние имена
    HEARTBEAT_S,
    STALE_AFTER_S,
    ВОЗРАСТ_ВРЕМЕННЫХ_С,
    СВОИ_ВРЕМЕННЫЕ,
    СИРОТА_ЧЕРЕЗ_С,
    ОбслуживаниеОчереди,
)
from .job_queue_worker import (  # noqa: F401 — прежние имена
    ИТОГ_БЕЗ_РАСШИФРОВКИ,
    ПРОГРЕСС_В_БАЗУ_С,
    РабочийЦиклОчереди,
    _внутри,
    _для_владельца,
    _отпечаток_каталога,
    _трассировка,
    process_job,
    движок_задания,
    свободная_память,
)
from .job_status import (  # noqa: F401 — прежние имена
    ACTIVE_STATUSES,
    EMPTY_STATUSES,
    FINISHED_STATUSES,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NO_SPEECH,
    STATUS_PAUSED,
    STATUS_QUEUED,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SHORT,
    WorkerState,
)
from .logging_setup import get_logger
from .monitoring.collector import (  # noqa: F401 — прежние имена
    JOB_DURATION_BUCKETS,
    MEDIA_DURATION_BUCKETS,
    RUNTIME,
)

# Выход наружу — в своём модуле; имена остаются и здесь.
from .outbound import (  # noqa: F401
    _ЗАМОК_УЗЛОВ,
    _УЗЛЫ_УВЕДОМЛЕНИЙ,
    _адреса_узла,
    _внутренний,
    _класс_с_проверкой,
    check_outbound_url,
    открыватель_наружу,
    узел_на_паузе,
    узел_ответил,
)
from .pipeline import audio_profile
from .pipeline import metrics as metrics_mod
from .processor import settings_digest

log = get_logger("queue")


def _важность(priority: int | None, settings: dict[str, Any]) -> int:
    """Важность задания в пределах 0–100 — из аргумента или из параметров."""
    try:
        значение = int(priority if priority is not None else settings.get("priority", 50))
    except (TypeError, ValueError):
        значение = 50
    return max(0, min(100, значение))


def разобрать_срок(значение: Any, *, сейчас: float | None = None) -> float | None:
    """Срок выполнения задания — в секунды эпохи. None — срока нет.

    Политике «По сроку выполнения» (`scheduling_policy: deadline`) неоткуда
    было взять срок: колонку заполнял только аргумент `submit`, а его не
    передавал ни один вызывающий, и политика всегда была «по приоритету».
    Срок принимается так, как его напишет человек или интеграция:

    * секунды эпохи — `1767225600`;
    * время ISO-8601 — `2026-11-01T18:00:00+03:00` (без пояса — местное);
    * относительно сейчас — `+30` (минут), `30m`, `2h`, `1d`.
    """
    if значение is None or значение == "":
        return None
    момент = time.time() if сейчас is None else float(сейчас)
    if isinstance(значение, (int, float)) and not isinstance(значение, bool):
        число = float(значение)
        if число <= 0:
            return None
        return число if число > 1e9 else момент + число * 60
    текст = str(значение).strip().lower()
    import re  # noqa: PLC0415

    относительный = re.fullmatch(r"\+?\s*(\d+(?:[.,]\d+)?)\s*(m|min|мин|h|ч|d|д)?", текст)
    if относительный and (текст.startswith("+") or относительный.group(2)):
        число = float(относительный.group(1).replace(",", "."))
        множитель = {"h": 3600, "ч": 3600, "d": 86400, "д": 86400}.get(
            относительный.group(2) or "m", 60)
        return момент + число * множитель
    try:
        простое: float | None = float(текст.replace(",", "."))
    except ValueError:
        простое = None
    if простое is not None:
        if простое <= 0:
            return None
        return простое if простое > 1e9 else момент + простое * 60
    from datetime import datetime  # noqa: PLC0415

    try:
        разобрано = datetime.fromisoformat(str(значение).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigError(
            f"Срок выполнения «{значение}» не разобран.",
            hint="Принимаются секунды эпохи, время ISO-8601 (2026-11-01T18:00:00+03:00) "
                 "или «через сколько»: +30 (минут), 30m, 2h, 1d.") from exc
    return разобрано.timestamp()


#: Кто мы такие среди серверов на общей базе (см. `instance`). Имя
#: сохранено здесь же: на него ссылается и код, и тесты.
INSTANCE_ID = _INSTANCE_ID

#: Флаг «очередь приостановлена» в `kv`. Пауза жила в памяти одного
#: процесса: перезапуск снимал её молча, а на общей базе «Приостановить»
#: через балансировщик останавливало один сервер из нескольких.
КЛЮЧ_ПАУЗЫ = "queue.paused"
#: Как часто воркеры перечитывают флаг паузы из базы, секунд.
ПАУЗА_ПЕРЕЧИТАТЬ_С = 2.0



class Будильник:
    """Пробуждение воркеров: как `threading.Event`, но будит одного.

    С `Event` каждое новое задание будило всех простаивающих воркеров: на
    сервере с восемью воркерами это восемь запросов к базе за одним
    заданием, семь из которых возвращались ни с чем. Здесь сигнал достаётся
    одному ждущему; несколько сигналов подряд (пачка заданий) копятся, но не
    больше `ПРЕДЕЛ`. Воркеры всё равно просыпаются раз в секунду сами, так
    что потерянный сигнал стоит не больше секунды. Остановка будит всех.
    """

    ПРЕДЕЛ = 16

    def __init__(self) -> None:
        self._условие = threading.Condition()
        self._сигналов = 0
        self._все = False

    def set(self) -> None:
        with self._условие:
            self._сигналов = min(self._сигналов + 1, self.ПРЕДЕЛ)
            self._условие.notify(1)

    def set_all(self) -> None:
        with self._условие:
            self._все = True
            self._условие.notify_all()

    def wait(self, timeout: float | None = None) -> bool:
        with self._условие:
            if not self._сигналов and not self._все:
                self._условие.wait(timeout)
            if self._сигналов:
                self._сигналов -= 1
                return True
            return self._все

    def clear(self) -> None:
        """Сигнал уже снят в `wait` — для совместимости с `Event`."""

    def is_set(self) -> bool:
        return bool(self._сигналов) or self._все


class JobQueue(УведомленияОчереди, ОбслуживаниеОчереди, РабочийЦиклОчереди):
    """Менеджер очереди с пулом рабочих потоков."""

    #: Необязательные соседи очереди — их ставит приложение после сборки.
    #: Объявлены на классе, а не только в `__init__`: очередь собирают и
    #: вручную (проверки строят её через `__new__` ради одного метода), и
    #: обращение к недостающему полю роняло бы такой вызов на ровном месте.
    content_index: Any = None
    llm_worker: Any = None

    def __init__(self, db: Database, settings: Any, registry: EngineRegistry,
                 *, on_event: Callable[[str, dict[str, Any]], None] | None = None):
        self.db = db
        self.settings = settings
        self.registry = registry
        self.on_event = on_event

        self._lock = threading.RLock()
        #: Проверка предела очереди и заведение задания — одним шагом (см. `submit`).
        self._замок_приёма = threading.Lock()
        self._wake = Будильник()
        self._stop = threading.Event()
        self._paused = False
        self._workers: list[threading.Thread] = []
        #: Состояния воркеров по их номеру. Именно словарь, а не список:
        #: уходящий воркер обрезал список от своего номера до конца
        #: (`del self._states[index:]`) и уносил с собой состояния живых
        #: соседей. После этого длина списка врала о числе воркеров, смена
        #: их числа снимала пометку на выход с тех, кто ещё её не увидел, и
        #: поднимала для тех же номеров вторые потоки — два потока на один
        #: `WorkerState`, затирающие друг другу задание и прогресс.
        self._states: dict[int, WorkerState] = {}
        #: Сколько воркеров заявлено. Не длина словаря: помеченный на выход
        #: воркер уходит не сразу, и до его ухода заявленное и живое
        #: расходятся.
        self._worker_count = 0
        self._cancelled: set[str] = set()
        #: Владельцы заданий для адресной рассылки событий по WebSocket.
        self._owners: dict[str, str] = {}
        # Индексы воркеров, помеченных на выход при уменьшении их числа.
        self._retiring: set[int] = set()
        self._webhooks: Any = None
        # content_index и llm_worker объявлены на классе: разбор содержания
        # и смысловой слой ставит приложение (create_app), потому что теми
        # же объектами пользуются ручки разделов — два разбора с двумя
        # снимками корпусных частот считали бы по-разному одну и ту же запись.
        self._running: dict[str, float] = {}
        #: Сколько заданий шло разом, пока выполнялось это. Замер памяти —
        #: цифра на весь процесс, и без этого числа она не говорит ничего:
        #: 27 ГБ при одном задании и 27 ГБ при трёх — разные ответы на
        #: вопрос «хватит ли карты».
        self._concurrency: dict[str, int] = {}
        self._model_counts: dict[str, int] = {}
        #: Задания, которые этот процесс сейчас действительно считает, — уже
        #: захваченные в базе. Не то же самое, что `_running`: туда задание
        #: попадает раньше захвата, и отметка жизни по нему сочла бы
        #: незахваченное задание отобранным.
        self._идут: set[str] = set()
        #: Владелец каждого идущего задания — для справедливого разделения:
        #: нагрузку владельца больше не нужно поднимать из базы под общей
        #: блокировкой очереди по запросу на каждое идущее задание.
        self._чьи_идут: dict[str, str] = {}
        #: Когда в последний раз начиналось задание каждого владельца (по
        #: монотонным часам процесса) — кого обслуживали давнее всех.
        self._последний_старт: dict[str, float] = {}
        #: Потоки воркеров по номеру — чтобы поднять заново умерший.
        self._потоки: dict[int, threading.Thread] = {}
        #: Дочерние процессы распознавания по номеру воркера
        #: (`worker_isolation: process`, см. `worker_process`).
        self._дочерние: dict[int, Any] = {}
        self._пауза_прочитана = 0.0
        self._started = False

    @property
    def _max_queue(self) -> int:
        """Предел очереди — из настроек в момент приёма, а не при запуске.

        Значение читалось один раз в конструкторе: предел, изменённый через
        PUT /api/settings, не действовал до перезапуска — при пределе 2
        принимались четыре задания, а /api/queue показывал прежнюю тысячу.
        """
        получить = getattr(self.settings, "get", None)
        if получить is None:
            return 1000
        try:
            return max(1, int(получить("max_queue_size", 1000) or 1000))
        except (TypeError, ValueError):
            return 1000

    def _предел_повторов(self, job: dict[str, Any]) -> int:
        """Сколько повторов положено заданию.

        Своё значение задания — не больше серверного и с честным нулём:
        `int(params.get("max_retries") or сервер)` превращал «без повторов»
        у задания в серверные два.
        """
        настройки = getattr(self, "settings", None)
        сервер = max(0, S.integer(настройки, "max_retries", 2)) if настройки is not None else 2
        своё = (job.get("params") or {}).get("max_retries")
        if своё is None or своё == "":
            return сервер
        try:
            return max(0, min(int(своё), сервер))
        except (TypeError, ValueError):
            return сервер

    # --- запуск и остановка ---------------------------------------------

    def start(self, workers: int | None = None) -> None:
        if self._started:
            return
        count = int(workers or self.settings.get("max_concurrent_jobs") or 2)
        self.recover()
        self._пауза_из_базы(сразу=True)
        try:
            self._подмести_временные(при_запуске=True)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Временные файлы прежнего запуска не убраны: %s", exc)
        self._states = {i: WorkerState(index=i) for i in range(count)}
        self._worker_count = count
        for index in range(count):
            self._поднять_воркера(index)
        janitor = threading.Thread(target=self._janitor_loop, name="asrhub-janitor", daemon=True)
        janitor.start()
        self._workers.append(janitor)
        # Отметка жизни — своим потоком, а не в служебном цикле: у того
        # бывают долгие шаги (снятие копии базы, уборка), и отметка за ними
        # опаздывала бы ровно так же, как опаздывала за долгими шагами
        # конвейера.
        heartbeat = threading.Thread(target=self._heartbeat_loop,
                                     name="asrhub-heartbeat", daemon=True)
        heartbeat.start()
        self._workers.append(heartbeat)
        self._started = True
        log.info("Очередь запущена: воркеров %d", count)
        # Уведомления, не дошедшие до прошлой остановки, — сразу, не дожидаясь
        # первого оборота служебного цикла.
        self._дослать_уведомления()

    def _поднять_воркера(self, index: int) -> None:
        """Заводит поток воркера с этим номером. Под общей блокировкой или до запуска."""
        thread = threading.Thread(target=self._worker_loop, args=(index,),
                                  name=f"asrhub-worker-{index}", daemon=True)
        thread.start()
        self._workers.append(thread)
        self._потоки[index] = thread

    def stop(self, timeout: float = 10.0) -> None:
        """Останавливает очередь. Недосчитанное возвращается без траты попытки.

        Идущие задания узнают об остановке на ближайшей проверке отмены и
        сами встают обратно в очередь. Те, что сидят внутри долгого вызова
        движка и проверку не проходят, возвращаются отсюда, когда истечёт
        ожидание. Раньше они оставались «выполняется» до следующего запуска,
        и тот считал их потерянными — то есть плановое обновление сервера
        тратило попытку у каждого идущего задания.
        """
        self._stop.set()
        with self._lock:
            pool, self._webhooks = self._webhooks, None
        if pool is not None:
            pool.shutdown(wait=False)
        разбудить_всех = getattr(self._wake, "set_all", self._wake.set)
        разбудить_всех()
        for thread in self._workers:
            thread.join(timeout=timeout / max(1, len(self._workers)))
        for дочерний in list(getattr(self, "_дочерние", {}).values()):
            try:
                дочерний.остановить()
            except Exception as exc:                        # noqa: BLE001
                log.debug("Процесс воркера не остановлен: %s", exc)
        try:
            возвращено = self._вернуть_свои_при_остановке()
            if возвращено:
                log.info("Возвращено в очередь при остановке: %d", возвращено)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Идущие задания не возвращены в очередь при остановке: %s", exc)
        self._started = False
        log.info("Очередь остановлена")

    def _вернуть_в_очередь_без_попытки(self, job_id: str) -> bool:
        """Своё идущее задание — обратно в очередь, счётчик повторов не трогаем."""
        вернулось = self.db.update_job_if_status(
            job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID,
            status=STATUS_QUEUED, stage="возвращено в очередь", progress=0.0,
            started_at=None, instance_id=None, heartbeat_at=None, queued_at=now())
        if вернулось:
            self.db.add_event(job_id, "requeued",
                              "Сервер остановлен штатно — задание вернётся в очередь "
                              "без траты попытки")
        return вернулось

    def _вернуть_свои_при_остановке(self) -> int:
        """Все свои «выполняется» — обратно в очередь без траты попытки.

        Не только те, что в `_идут`: задание, чью завершающую запись не
        удалось сделать (диск полон, база занята дольше busy_timeout), уже
        не числится ни у одного воркера, а в базе — «выполняется, 100 %»
        за этим экземпляром. Раньше остановка его не видела, и запуск
        списывал попытку.
        """
        with self._lock:
            свои = set(self._идут)
        try:
            for строка in self.db.query(
                    "SELECT id FROM jobs WHERE status=? AND instance_id=?",
                    (STATUS_RUNNING, INSTANCE_ID)):
                свои.add(str(строка["id"]))
        except Exception as exc:                            # noqa: BLE001
            log.debug("Свои идущие задания не перечислены: %s", exc)
        return sum(1 for job_id in свои if self._вернуть_в_очередь_без_попытки(job_id))

    def recover(self) -> int:
        """Возвращает в очередь задания, оставшиеся в состоянии «выполняется».

        Забираются только свои — этого экземпляра и ничьи (так помечены
        задания, начатые до появления учёта экземпляров). Задания соседей
        не трогаются, даже если они выглядят зависшими: за них отвечает
        `_reclaim_stale_jobs`, который сначала ждёт, пока отметка жизни
        устареет. Без этой оговорки перезапуск любого сервера на общей базе
        сбрасывал прогресс всех соседей и запускал их задания второй раз —
        двойная оплата видеокарты и два одинаковых уведомления заказчику,
        то есть ровно то, что неделимый захват и должен был исключить.

        Возврат — это попытка, и она считается, как у брошенных заданий
        соседей. Иначе запись, на которой процесс падает сам (убит по
        нехватке памяти, сбой в нативной библиотеке), крутилась по кругу:
        перезапуск службы — задание снова первым — снова падение, и так без
        конца, без единой записи об ошибке. Плановая остановка попытку не
        тратит: свои задания она возвращает в очередь сама (см. `stop`).
        """
        total = 0
        while True:
            # Спрашиваем базу напрямую: облегчённый список не отдаёт
            # instance_id, и фильтр по нему молча пропускал бы всё подряд.
            #
            # «Свои» — это не только текущий процесс. В отметке стоит номер
            # процесса, и после перезапуска он другой, поэтому по точному
            # совпадению не находилось ничего: задания висели «выполняется»
            # пять минут, пока их не подберёт возврат брошенных, а тот
            # тратит попытку и пишет «экземпляр перестал отвечать». Берём
            # ещё и задания процессов этой же машины, которых больше нет.
            кандидаты = self.db.query(
                "SELECT id, instance_id FROM jobs WHERE status=? "
                "  AND (instance_id IS NULL OR instance_id='' OR instance_id=? "
                "       OR instance_id LIKE ?) "
                "LIMIT 1000",
                (STATUS_RUNNING, INSTANCE_ID, f"{HOSTNAME}:%"))
            stuck = [з for з in кандидаты
                     if not str(з["instance_id"] or "")
                     or str(з["instance_id"]) == INSTANCE_ID
                     or not process_alive(str(з["instance_id"]))]
            if not stuck:
                break
            for job in stuck:
                self._подобрать_после_перезапуска(str(job["id"]))
            total += len(stuck)
            if len(кандидаты) < 1000:
                break
        if total:
            log.warning("Возвращено в очередь после перезапуска: %d заданий", total)
        return total

    def _подобрать_после_перезапуска(self, job_id: str) -> None:
        """Одно своё задание, оставшееся «выполняется»: в очередь или в ошибку."""
        строка = self.db.get_job(job_id) or {}
        retries = int(строка.get("retries") or 0)
        предел = self._предел_повторов(строка)
        if retries >= предел:
            снято = self.db.update_job_if_status(
                job_id, [STATUS_RUNNING],
                status=STATUS_FAILED, finished_at=now(), stage="ошибка",
                progress=0.0, instance_id=None, heartbeat_at=None,
                **self._уведомление_ждёт(строка, кроме_отката=True),
                error_code="instance_lost",
                error_message=(f"Сервер перезапускался посреди этого задания "
                               f"{retries + 1} раз подряд — оно снято."),
                error_hint=("Похоже, процесс падает на этой записи: чаще всего "
                            "это нехватка памяти или сбой в библиотеке движка. "
                            "Посмотрите журнал сервера за время перезапуска "
                            "(journalctl -u asrhub). Повторить вручную: "
                            "POST /api/jobs/{id}/retry"))
            if снято:
                log.error("Задание %s снято: сервер перезапускался на нём %d раз подряд",
                          job_id, retries + 1, extra={"job_id": job_id})
                self.db.add_event(job_id, "failed",
                                  f"Задание снято: сервер перезапускался посреди него "
                                  f"{retries + 1} раз подряд")
                self._итог_потерянного(job_id, "Повторное распознавание не удалось: "
                                               "сервер перезапускался посреди него")
            return
        вернулось = self.db.update_job_if_status(
            job_id, [STATUS_RUNNING],
            status=STATUS_QUEUED, stage="", progress=0.0, started_at=None,
            instance_id=None, heartbeat_at=None, retries=retries + 1,
            queued_at=now())
        if вернулось:
            self.db.add_event(job_id, "recovered",
                              f"Задание возвращено в очередь после перезапуска сервера "
                              f"(попытка {retries + 1} из {предел})")

    def _итог_потерянного(self, job_id: str, причина: str) -> None:
        """После снятия задания с «instance_lost»: прежний результат или уведомление.

        Обе ветки instance_lost (здесь и у брошенных соседями) не делали ни
        того, ни другого. Повтор готового задания, на котором процесс падал
        (убит по нехватке памяти), оставлял задание «ошибкой», а готовая
        расшифровка лежала в `results/<id>.prev`; обычное задание кончалось
        без обратного вызова, и интеграция ждала его вечно.
        """
        # Сбой здесь не должен срывать подбор остальных заданий: снятие уже
        # записано, а уведомление, заведённое той же записью, дошлёт
        # служебный цикл.
        try:
            if self._вернуть_прежний(job_id, причина, ожидаемые=[STATUS_FAILED]):
                # Откат — не итог для заказчика: уведомление не уходит, как и
                # при обычной неудаче повтора (см. `_уведомление_ждёт`).
                return
            self._send_webhook(job_id)
        except Exception as exc:                            # noqa: BLE001
            log.warning("Итог снятого задания %s не доведён: %s", job_id, exc,
                        extra={"job_id": job_id})

    # --- добавление -----------------------------------------------------

    def submit(self, *, file_path: Path, filename: str, settings: dict[str, Any],
               owner: str = "anonymous", api_key_name: str = "", priority: int | None = None,
               group_id: str | None = None, source: str = "api",
               tags: str = "", deadline: float | None = None,
               reference_text: str = "", webhook_url: str = "",
               duration_s: float | None = None) -> dict[str, Any]:
        """Ставит файл в очередь. Возвращает описание задания.

        `duration_s` — длительность, уже измеренная вызывающим (загрузка
        меряет её для квоты): без неё ffprobe запускался на тот же файл
        второй раз.
        """
        # Адрес уведомления приходит двумя путями: отдельным аргументом и
        # полем webhook_url внутри настроек задания. Проверялся только
        # первый, поэтому запрет на внутреннюю сеть обходился одной строкой
        # в JSON: settings={"webhook_url":"http://169.254.169.254/..."} — и
        # сервер сам ходил по этому адресу, а поле webhook_status работало
        # оракулом для обхода внутренней сети. Проверяем оба.
        target = webhook_url or str(settings.get("webhook_url") or "")
        if target:
            target = self._check_webhook_url(target)
        webhook_url = target
        deadline = разобрать_срок(deadline)
        depth = self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY])
        if depth >= self._max_queue:
            raise QueueFull(f"В очереди уже {depth} заданий (предел {self._max_queue}).")

        self._check_disk_space()

        path = Path(file_path)
        from .pipeline.audio import file_hash, probe

        digest = file_hash(path)
        spec = get_model(str(settings.get("model") or ""))
        params_digest = settings_digest(settings, weights=self._отпечаток_весов(spec))

        duration = float(duration_s or 0.0)
        if duration <= 0:
            try:
                duration = probe(path).duration_s
            except ASRHubError as exc:
                log.info("Не удалось определить длительность «%s»: %s", filename,
                         exc.message)

        # Кеш результатов по содержимому и настройкам — только среди своих
        # заданий. Общий на всех кеш говорил Бобу, что такую же запись уже
        # присылал кто-то другой (задание готово мгновенно, стадия «из
        # кеша»), а клон приносил файлы результата под именем первого
        # загрузившего — «+79161234567 Иванов Пётр.json» вместо «my.json».
        if settings.get("deduplicate_jobs", True):
            cached = self._find_cached(digest, params_digest, owner=owner)
            if cached is not None:
                job_id = self._clone_cached(cached, filename, str(path), owner,
                                            group_id, settings, webhook_url,
                                            reference_text=reference_text,
                                            api_key_name=api_key_name, source=source,
                                            tags=tags, priority=priority,
                                            deadline=deadline)
                RUNTIME.inc("asrhub_cached_jobs_total")
                self._emit("job.cached", {"id": job_id, "source": cached["id"]})
                # Клон — такая же завершённая запись, как обычная: разбор
                # содержания (и с ним CRM, трекеры) и смысловой разбор — сразу,
                # а не когда фоновый поиск «неразобранного» до него дойдёт.
                # Реплики — от исходного задания: у клона своих строк нет.
                if self.content_index is not None:
                    try:
                        self.content_index.on_job_completed(
                            job_id, self.db.get_job(job_id) or {},
                            self.db.get_segments(str(cached["id"])))
                    except Exception as exc:                 # noqa: BLE001
                        log.debug("Клон %s не разобран сразу: %s", job_id, exc)
                if self.llm_worker is not None:
                    try:
                        self.llm_worker.enqueue(job_id)
                    except Exception as exc:                 # noqa: BLE001
                        log.debug("Клон %s не поставлен на смысловой разбор: %s", job_id, exc)
                # Задание завершено мгновенно, но для отправителя оно
                # завершено — значит уведомление обязано уйти. Раньше на
                # этом пути его не отправляли вовсе, и внешняя система
                # ждала колбэка до собственного тайм-аута.
                if webhook_url:
                    self._send_webhook(job_id)
                # «Удалять исходник после обработки» действует и здесь:
                # задание из кеша тоже обработано, а загрузка оставалась в
                # uploads навсегда — уборка по сроку файл задания не трогает.
                if settings.get("delete_source_after"):
                    self._удалить_исходник(self.get(job_id))
                return self.get(job_id)

        # Секреты сервера в параметры задания не уносятся: параметры видит
        # владелец задания, а секреты при обработке берутся из настроек
        # сервера (см. Settings.НЕ_В_ЗАДАНИИ).
        payload = Settings.для_задания(settings)
        payload["_hash"] = params_digest

        # Глубина проверялась в начале, а задание заводилось после разбора
        # файла (хеш, длительность — секунды на большой записи): сто
        # одновременных загрузок при пустой очереди проходили проверку все
        # и вставали поверх max_queue_size. Повторная проверка и запись —
        # под одним замком.
        with self._замок_приёма:
            depth = self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY])
            if depth >= self._max_queue:
                raise QueueFull(f"В очереди уже {depth} заданий (предел {self._max_queue}).")
            job_id = self._завести(payload, path=path, filename=filename, digest=digest,
                                   duration=duration, settings=settings, group_id=group_id,
                                   priority=priority, owner=owner,
                                   api_key_name=api_key_name, source=source, tags=tags,
                                   deadline=deadline, reference_text=reference_text,
                                   webhook_url=webhook_url)
        self._emit("job.queued", {"id": job_id, "filename": filename})
        self._wake.set()
        return self.get(job_id)

    def _завести(self, payload: dict[str, Any], *, path: Path, filename: str, digest: str,
                 duration: float, settings: dict[str, Any], group_id: str | None,
                 priority: int | None, owner: str, api_key_name: str, source: str,
                 tags: str, deadline: float | None, reference_text: str,
                 webhook_url: str) -> str:
        return self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": str(path),
            "file_size": path.stat().st_size if path.exists() else 0,
            "file_hash": digest,
            "media_duration_s": duration,
            "engine": движок_задания(settings),
            "model": str(settings.get("model") or ""),
            "language": str(settings.get("language") or ""),
            "params": payload,
            # Тот же предел 0–100, что у POST /{id}/priority: без него поле
            # формы priority=1000000 при политике priority_fifo ставило
            # задание впереди всех чужих, а отрицательное — позади навсегда.
            "priority": _важность(priority, settings),
            "owner": owner,
            "api_key_name": api_key_name,
            "source": source,
            "tags": tags,
            "deadline": deadline,
            "reference_text": reference_text or None,
            "webhook_url": webhook_url or None,
        })

    def _отпечаток_весов(self, spec: Any) -> str:
        """Отпечаток файлов модели для ключа кеша (см. `model_files.fingerprint`).

        Отпечаток весов входит в ключ кеша: без него обновление модели под
        тем же именем отдавало старый результат как свежий, и понять это
        было нельзя ни по ответу, ни по карточке задания. Ревизия — часть
        отпечатка: у GigaAM варианты модели лежат рядом (`v3_ctc.ckpt`,
        `v3_rnnt.ckpt`), и без неё брался первый по алфавиту — обновление
        весов модели по умолчанию отпечаток не меняло.
        """
        if spec is None:
            return ""
        try:
            return model_files.fingerprint(
                self.settings.get("models_dir") or self.settings.paths.models,
                spec.source, getattr(spec, "revision", "") or "")
        except OSError as exc:
            log.debug("Отпечаток весов «%s» не снят: %s", spec.id, exc)
            return ""

    def _find_cached(self, file_digest: str, params_digest: str, *,
                     owner: str | None = None) -> dict[str, Any] | None:
        """Ищет готовый результат для той же записи с теми же настройками.

        Поиск идёт запросом по индексу, а не перебором последних заданий:
        при потоке больше полусотни файлов перебор просто не находил
        совпадений, и дедупликация тихо переставала работать.
        """
        cached = self.db.find_cached(file_digest, params_digest, owner=owner)
        if cached is None:
            return None
        # Результаты могли быть удалены очисткой, а запись остаться:
        # отдавать ссылку на несуществующий каталог нельзя.
        result_path = cached.get("result_path")
        if result_path and not Path(result_path).is_dir():
            log.debug("Кеш пропущен: каталог результатов %s исчез", result_path)
            return None
        return cached

    def _clone_cached(self, cached: dict[str, Any], filename: str, path: str,
                      owner: str, group_id: str | None,
                      settings: dict[str, Any], webhook_url: str = "",
                      reference_text: str = "", *, api_key_name: str = "",
                      source: str = "api", tags: str = "",
                      priority: int | None = None,
                      deadline: float | None = None) -> str:
        job_id = self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": path,
            # Как в обычной ветке `submit` — см. ниже.
            "api_key_name": api_key_name,
            "source": source,
            "tags": tags,
            "priority": _важность(priority, settings),
            "deadline": deadline,
            # Адрес уведомления переносим в клон: без него задание
            # завершалось молча и повторить отправку было нечем.
            "webhook_url": webhook_url or None,
            "file_hash": cached.get("file_hash"),
            # Размер обязателен: суточная квота на объём считается именно по
            # нему, и без него ключ заполнял диск без предела, повторяя
            # загрузку одного и того же файла, — а GET /api/usage показывал
            # неизменный расход и врал и пользователю, и администратору.
            "file_size": Path(path).stat().st_size if Path(path).exists() else 0,
            "media_duration_s": cached.get("media_duration_s"),
            "engine": cached.get("engine"),
            "model": cached.get("model"),
            "language": cached.get("language"),
            "params": {**Settings.для_задания(settings),
                       "_hash": (cached.get("params") or {}).get("_hash")},
            "owner": owner,
            "status": STATUS_COMPLETED,
            "cached_from": cached["id"],
        })
        self._заполнить_из_кеша(job_id, cached, filename=filename,
                                reference_text=reference_text)
        return job_id

    def _заполнить_из_кеша(self, job_id: str, cached: dict[str, Any], *,
                           filename: str, reference_text: str = "") -> None:
        """Переносит готовый результат `cached` в задание `job_id`.

        Общая часть клона при загрузке и задания, дождавшегося своего
        близнеца в очереди (см. `_взять_у_близнеца`).
        """
        # Копируем каталог результатов, а не ссылаемся на чужой: иначе
        # удаление любого из двух заданий уничтожало результаты второго.
        result_path = self._copy_results(
            cached.get("result_path"), job_id,
            прежнее_имя=str(cached.get("filename") or cached["id"]), новое_имя=filename)
        self.db.update_job(
            job_id,
            status=STATUS_COMPLETED,
            started_at=now(), finished_at=now(),
            text=cached.get("text"), result_path=result_path,
            segments_count=cached.get("segments_count"),
            words_count=cached.get("words_count"),
            # Знаки и говорящие переносятся вместе с остальным. Их не было, и
            # строка «Знаков» в сводке занижалась ровно на долю попаданий в
            # кеш — при том что «Слов» считалось полностью, так что расхождение
            # выглядело как ошибка в подсчёте, а не как пропуск. Ноль
            # говорящих вдобавок молча выбрасывал такие задания из разреза по
            # числу собеседников.
            chars_count=cached.get("chars_count"),
            speakers_count=cached.get("speakers_count"),
            avg_confidence=cached.get("avg_confidence"),
            rtf=cached.get("rtf"), processing_time_s=0.0, queue_time_s=0.0,
            # Признаки здоровья распознавания — те же, что у оригинала: это
            # свойство расшифровки, а она скопирована целиком.
            suspect_segments=cached.get("suspect_segments"),
            suspect_share=cached.get("suspect_share"),
            quality_flags=",".join(cached.get("quality_flags") or [])
            if isinstance(cached.get("quality_flags"), list) else cached.get("quality_flags"),
            quality_detail=cached.get("quality_detail"),
            # Профиль звука — свойство записи, а запись та же. Полоса
            # громкости — тоже: без неё у клона пустой проигрыватель.
            **audio_profile.for_job(cached),
            waveform=cached.get("waveform"),
            progress=1.0, stage="из кеша")
        # Имя ключа, источник, метки, важность и срок — те, что пришли с
        # этой загрузкой, как в обычной ветке `submit`. Клон их терял:
        # запись, совпавшая по содержимому с уже распознанной (повторный
        # забор с АТС, одинаковое приветствие автоответчика), попадала в срез
        # «по источникам» как «api», а без метки согласия — в список записей
        # без согласия.
        segments = self.db.get_segments(cached["id"])
        if segments:
            self.db.save_segments(job_id, segments)
        # Эталон приходит с заданием, а не с записью: у клона он свой, и
        # точность считается здесь же — иначе задание с эталоном, попавшее
        # в кеш, оставалось без WER, и в срезах точности его не было.
        эталон = str(reference_text or "").strip()
        if эталон:
            from .pipeline import calibration  # noqa: PLC0415

            гипотеза = (" ".join(str(с.get("text") or "") for с in segments)
                        if segments else str(cached.get("text") or ""))
            try:
                разбор = metrics_mod.detailed(эталон, гипотеза)
                self.db.update_job(
                    job_id, reference_text=эталон,
                    calibration=calibration.per_job(segments, эталон) if segments else None,
                    **metrics_mod.job_fields(разбор))
            except Exception as exc:                          # noqa: BLE001
                log.warning("Точность для %s из кеша не рассчитана: %s", job_id, exc,
                            extra={"job_id": job_id})
        self.db.add_event(job_id, "cached",
                          f"Результат взят из кеша задания {cached['id']}")
        if self.llm_worker is not None:
            try:
                self.llm_worker.enqueue(job_id)
            except Exception as exc:                         # noqa: BLE001
                log.debug("Клон %s не поставлен на смысловой разбор: %s", job_id, exc)
        log.info("Задание %s: результат взят из кеша (%s)", job_id, cached["id"])

    def _взять_у_близнеца(self, job: dict[str, Any], merged: dict[str, Any]) -> bool:
        """Задание, ждавшее идущего близнеца, берёт его готовый результат.

        Две одинаковые записи с одинаковыми настройками, присланные почти
        одновременно (повтор забора с АТС, двойная загрузка), раньше
        распознавались обе: кеш при приёме находит только завершённые
        задания, а первое ещё шло. Теперь выборка очереди не берёт задание,
        у которого идёт близнец (`Database.ждут_близнеца`), а когда тот
        готов — второе получает копию его результата вместо прогона модели.
        Близнец упал или отменён — второе задание распознаётся как обычно.
        """
        if not merged.get("deduplicate_jobs", True):
            return False
        отпечаток = (job.get("params") or {}).get("_hash")
        if not job.get("file_hash") or not отпечаток:
            return False
        cached = self._find_cached(str(job["file_hash"]), str(отпечаток),
                                   owner=str(job.get("owner") or "anonymous"))
        if cached is None or cached["id"] == job["id"]:
            return False
        job_id = job["id"]
        self._заполнить_из_кеша(job_id, cached, filename=str(job.get("filename") or job_id),
                                reference_text=str(job.get("reference_text") or ""))
        self.db.update_job(job_id, cached_from=cached["id"], instance_id=None,
                           heartbeat_at=None, error_code=None, error_message=None,
                           error_hint=None, **self._уведомление_ждёт(job))
        RUNTIME.inc("asrhub_cached_jobs_total")
        self._emit("job.cached", {"id": job_id, "source": cached["id"]})
        if self.content_index is not None:
            try:
                self.content_index.on_job_completed(
                    job_id, self.db.get_job(job_id) or {},
                    self.db.get_segments(str(cached["id"])))
            except Exception as exc:                         # noqa: BLE001
                log.debug("Задание %s не разобрано сразу: %s", job_id, exc)
        if job.get("webhook_url"):
            self._send_webhook(job_id)
        if merged.get("delete_source_after"):
            self._удалить_исходник(self.get(job_id))
        return True

    # --- управление -----------------------------------------------------

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.db.get_job(job_id)
        if job is None:
            raise JobNotFound(f"Задание «{job_id}» не найдено.")
        return job

    def _copy_results(self, source: str | None, job_id: str, *,
                      прежнее_имя: str = "", новое_имя: str = "") -> str | None:
        """Копирует каталог результатов для задания, отданного из кеша.

        Файлы результата названы по имени записи, и копия носила имя первого
        загрузившего: скачивание отдавало «_79161234567 Иванов Пётр.json» тому,
        кто прислал «my.wav». Скопированные файлы переименовываются под своё
        задание — так же, как их назвала бы выгрузка.
        """
        if not source:
            return None
        origin = Path(source)
        if not origin.is_dir():
            return None
        target = self.settings.paths.results / job_id
        try:
            shutil.copytree(origin, target, dirs_exist_ok=True)
        except OSError as exc:
            log.warning("Не удалось скопировать результаты из кеша: %s", exc)
            return str(origin)          # хуже, чем копия, но лучше, чем ничего
        if прежнее_имя and новое_имя:
            from .pipeline.export import safe_basename  # noqa: PLC0415

            было = safe_basename(Path(прежнее_имя).stem)
            стало = safe_basename(Path(новое_имя).stem)
            if было != стало:
                for файл in list(target.iterdir()):
                    if файл.is_file() and файл.name.startswith(было + "."):
                        новый = target / (стало + файл.name[len(было):])
                        try:
                            if not новый.exists():
                                файл.rename(новый)
                        except OSError as exc:
                            log.debug("Файл %s из кеша не переименован: %s", файл.name, exc)
        return str(target)

    def cancel(self, job_id: str, by: str = "user") -> dict[str, Any]:
        """Отменяет задание. Уже завершённое не трогает.

        Смена статуса идёт условным запросом: между проверкой и записью
        воркер мог успеть завершить задание, и безусловная запись пометила
        бы готовый результат отменённым.
        """
        job = self.get(job_id)
        if job["status"] in FINISHED_STATUSES:
            return job
        with self._lock:
            self._cancelled.add(job_id)

        changed = self.db.update_job_if_status(
            job_id,
            expected=[STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY, STATUS_PAUSED],
            status=STATUS_CANCELLED, cancelled_by=by,
            finished_at=now(), stage="отменено")
        if changed:
            self.db.add_event(job_id, "cancelled", f"Отменено ({by})")
            self._emit("job.cancelled", {"id": job_id})
            # Отменили повтор, который ещё не начался, — прежний результат
            # возвращается сразу. Идущий вернёт воркер, когда остановится.
            if job["status"] != STATUS_RUNNING:
                self._вернуть_прежний(job_id, "Повторное распознавание отменено",
                                      ожидаемые=[STATUS_CANCELLED])
            with self._lock:
                # Задание в очереди или на паузе отменяется без участия
                # воркера, поэтому пометку об отмене снимаем сразу. Иначе она
                # оставалась в множестве навсегда: cancel_all по очереди из
                # десяти тысяч заданий добавлял десять тысяч вечных записей.
                if job_id not in self._running:
                    self._cancelled.discard(job_id)
        else:
            # Задание завершилось прямо во время отмены — это не ошибка.
            with self._lock:
                self._cancelled.discard(job_id)
            log.info("Отмена %s не применена: задание уже завершилось", job_id)
        return self.get(job_id)

    def cancel_group(self, group_id: str) -> int:
        return self._отменить_всё({"group_id": group_id, "status": list(ACTIVE_STATUSES)},
                                  "group")

    def cancel_all(self) -> int:
        return self._отменить_всё(
            {"status": [STATUS_QUEUED, STATUS_RETRY, STATUS_PAUSED]}, "all")

    def _отменить_всё(self, отбор: dict[str, Any], кто: str) -> int:
        """Отменить всё по отбору — проходами, пока выборка не опустеет.

        Раньше брались первые десять тысяч, и очередь архива в сорок тысяч
        отменялась на четверть, а в ответ уходило число выбранных, а не
        отменённых (задание, успевшее завершиться, тоже считалось).
        Предел проходов — от бесконечного цикла, если отмена где-то не
        удаётся и задание остаётся в выборке.
        """
        отменено = 0
        виденные: set[str] = set()
        for _ in range(1000):
            порция = [з for з in self.db.list_jobs(**отбор, limit=5000)
                      if з["id"] not in виденные]
            if not порция:
                break
            for job in порция:
                виденные.add(job["id"])
                итог = self.cancel(job["id"], by=кто)
                if итог.get("status") == STATUS_CANCELLED:
                    отменено += 1
        return отменено

    def retry(self, job_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        """Ставит задание в очередь заново — после сбоя или ради новой модели.

        Переопределения приходят уже проверенными (см. маршруты /retry и
        /rescan): здесь они только ложатся поверх прежних параметров.

        Готовый результат не выбрасывается заранее. Повтор переделывает то же
        задание на месте, и раньше неудачный прогон — АТС успела удалить
        запись по сроку хранения, выбранного движка нет на сервере —
        сносил каталог прежнего удачного результата: задание уходило в
        «ошибку», а выгрузка отвечала 400, хотя расшифровка ещё вчера была.
        Теперь прежний каталог откладывается в сторону и возвращается на
        место, если новый прогон не удался или его отменили.
        """
        job = self.get(job_id)
        if job["status"] == STATUS_RUNNING:
            raise ConfigError(
                "Задание ещё распознаётся — повторять его сейчас незачем.",
                hint="Дождитесь окончания или отмените задание, потом повторите.")
        # Повтор без записи обречён: задание уходило в очередь, занимало
        # место в ней и заканчивалось «Повторное распознавание не удалось»
        # — а «Распознать архив заново» ставило так тысячи записей, которые
        # АТС уже удалила по сроку хранения. Говорим сразу и понятно.
        if str(job.get("source") or "") == "text":
            raise ConfigError(
                "Это переписка, а не запись: распознавать заново нечего.",
                hint="Разбор переписки пересчитывается в карточке записи: "
                     "«Пересчитать» в разделе аналитики.")
        исходник = str(job.get("file_path") or "")
        if not исходник or not Path(исходник).is_file():
            raise ConfigError(
                f"Исходной записи «{job.get('filename') or job_id}» больше нет на "
                f"сервере — распознать её заново нельзя.",
                hint="Запись удалила уборка по сроку хранения, настройка «Удалять "
                     "исходник после обработки» или сама АТС. Прежний результат "
                     "остаётся на месте; чтобы распознать заново, загрузите файл ещё раз.")
        params = Settings.для_задания(job.get("params"))
        # Ручной повтор — обычный запуск той модели, что стоит в задании
        # сейчас. Пометка о прежнем переходе на резервную модель к нему уже
        # не относится: с ней повтор снова писал бы в результат «основная
        # не справилась».
        params.pop("_fallback_from", None)
        params.pop("_fallback_reason", None)
        if overrides:
            params.update(overrides)
        params.pop("_hash", None)
        # Отпечаток — так же, как при постановке, вместе с весами модели:
        # без них отпечаток повторённого задания не совпадал ни с одной новой
        # загрузкой, и кеш по такому заданию не срабатывал никогда.
        params["_hash"] = settings_digest(
            params, weights=self._отпечаток_весов(get_model(str(params.get("model") or ""))))
        self._отложить_прежний(job)
        # Счётчик повторов — заново: ручной повтор начинает новую попытку.
        # Без сброса первая же нехватка памяти после ручного повтора давала
        # «ошибку» сразу — без автоповтора и без уменьшения пакета.
        #
        # Модель и движок в колонках — те, что будут считать. Повтор другой
        # моделью («Распознать заново» с выбором модели) менял только
        # параметры: карточка, статистика моделей и метрики приписывали
        # новую расшифровку прежней модели.
        #
        # Условной записью — из того состояния, что прочитано: между чтением
        # и записью задание мог захватить воркер (его поставил в очередь
        # кто-то ещё), и безусловная запись возвращала идущее задание в
        # очередь из-под него — лишний прогон и два исхода на одно задание.
        поставлено = self.db.update_job_if_status(
            job_id, [str(job["status"])], status=STATUS_QUEUED, error_code=None,
            error_message=None, error_hint=None, progress=0.0,
            stage="", started_at=None, finished_at=None,
            retries=0, params=params, queued_at=now(),
            webhook_status=None, webhook_attempts=0, webhook_next_at=None,
            model=str(params.get("model") or job.get("model") or ""),
            engine=движок_задания(
                {**params, "model": params.get("model") or job.get("model")}))
        if not поставлено:
            # Отложенный только что каталог — на место, без записи в базу:
            # задание уже в чужих руках, и его состояние не наше.
            прежний = self._прежний_путь(job_id)
            текущий = Path(self.settings.paths.results) / job_id
            if прежний.is_dir() and not текущий.exists():
                try:
                    прежний.rename(текущий)
                except OSError as exc:
                    log.warning("Прежний результат %s не возвращён на место: %s",
                                job_id, exc, extra={"job_id": job_id})
            self._забыть_прежние_параметры(job_id)
            raise ConfigError(
                "Задание изменилось, пока его ставили на повтор, — повтор не поставлен.",
                hint="Обновите карточку задания и повторите, если это ещё нужно.")
        self.db.add_event(job_id, "retry", "Задание поставлено в очередь повторно")
        with self._lock:
            self._cancelled.discard(job_id)
        self._wake.set()
        self._emit("job.queued", {"id": job_id})
        return self.get(job_id)

    def retry_failed(self, limit: int = 100,
                     отказы: list[dict[str, str]] | None = None) -> int:
        """Ставит неудавшиеся задания заново. Возвращает, сколько поставлено.

        Отказ на одном задании (записи уже нет — «файл не найден», запись
        удалена АТС) — обычное содержимое списка неудавшихся, и он больше не
        обрывает весь проход: прежде маршрут отвечал 400 после первого
        такого задания, часть уже стояла в очереди, остальные не тронуты, и
        в ответе об этом не было ни слова. Отказы собираются в `отказы`, как
        у пакетных действий.
        """
        jobs = self.db.list_jobs(status=STATUS_FAILED, limit=limit, light=True)
        поставлено = 0
        for job in jobs:
            try:
                self.retry(job["id"])
                поставлено += 1
            except ASRHubError as exc:
                if отказы is not None:
                    отказы.append({"id": str(job["id"]), "error": exc.message})
        return поставлено

    def set_priority(self, job_id: str, priority: int) -> dict[str, Any]:
        priority = max(0, min(100, int(priority)))
        self.db.update_job(job_id, priority=priority)
        self.db.add_event(job_id, "priority", f"Приоритет изменён на {priority}")
        self._wake.set()
        return self.get(job_id)

    def set_deadline(self, job_id: str, deadline: Any) -> dict[str, Any]:
        """Срок выполнения ждущего задания (см. `разобрать_срок`); пусто — без срока.

        Только у ждущих: у идущего и завершённого срок уже ничего не решает,
        а запись поверх «выполняется» гонялась бы с воркером.
        """
        срок = разобрать_срок(deadline)
        if not self.db.update_job_if_status(
                job_id, [STATUS_QUEUED, STATUS_RETRY, STATUS_PAUSED], deadline=срок):
            self.get(job_id)
            raise ConfigError("Срок меняется только у ждущего задания.",
                              hint="Задание уже выполняется или завершено.")
        self.db.add_event(job_id, "deadline",
                          "Срок выполнения снят" if срок is None else
                          "Срок выполнения: " + time.strftime("%d.%m.%Y %H:%M",
                                                              time.localtime(срок)))
        self._wake.set()
        return self.get(job_id)

    def move_to_top(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 100)

    def move_to_bottom(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 0)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        # Условной записью: безусловная превращала в «приостановлено»
        # задание, которое воркер захватил между чтением и записью, — оно
        # досчитывалось, а в списке висело на паузе.
        if self.db.update_job_if_status(job_id, [STATUS_QUEUED], status=STATUS_PAUSED,
                                        stage="приостановлено"):
            self.db.add_event(job_id, "paused", "Задание приостановлено")
        return self.get(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        if self.db.update_job_if_status(job_id, [STATUS_PAUSED], status=STATUS_QUEUED,
                                        stage=""):
            self.db.add_event(job_id, "resumed", "Задание возобновлено")
            self._wake.set()
        return self.get(job_id)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        self._пауза_в_базу(True)
        self.db.add_event(None, "queue_paused", "Очередь приостановлена")
        self._emit("queue.paused", {})

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._пауза_в_базу(False)
        self._wake.set()
        self.db.add_event(None, "queue_resumed", "Очередь возобновлена")
        self._emit("queue.resumed", {})

    def _пауза_в_базу(self, значение: bool) -> None:
        """Флаг паузы — в общую базу (см. `КЛЮЧ_ПАУЗЫ`)."""
        try:
            self.db.set_kv(КЛЮЧ_ПАУЗЫ, bool(значение))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Пауза очереди не записана в базу — действует только на "
                        "этом сервере до перезапуска: %s", exc)
        self._пауза_прочитана = time.monotonic()

    def _пауза_из_базы(self, *, сразу: bool = False) -> bool:
        """Флаг паузы из общей базы — не чаще раза в `ПАУЗА_ПЕРЕЧИТАТЬ_С`."""
        прочитана = getattr(self, "_пауза_прочитана", 0.0)
        if not сразу and time.monotonic() - прочитана < ПАУЗА_ПЕРЕЧИТАТЬ_С:
            return self._paused
        try:
            self._paused = bool(self.db.get_kv(КЛЮЧ_ПАУЗЫ, False))
        except Exception as exc:                            # noqa: BLE001
            log.debug("Флаг паузы очереди не прочитан: %s", exc)
        self._пауза_прочитана = time.monotonic()
        return self._paused

    @property
    def is_paused(self) -> bool:
        return self._пауза_из_базы()

    def set_concurrency(self, workers: int) -> None:
        """Меняет число воркеров на ходу.

        Новые потоки запускаются сразу; лишние помечаются на выход и
        завершаются, доработав текущее задание. Без этой пометки потоки
        оставались жить, а последовательность 2 → 8 → 2 оставляла восемь
        живых воркеров при заявленных двух.
        """
        workers = max(1, min(64, int(workers)))
        if not self._started:
            # Очередь не запущена (`--no-queue`, «только интерфейс» над
            # общей базой) — число только запоминается, потоков нет. Раньше
            # любая правка `max_concurrent_jobs` (сохранение настроек,
            # восстановление копии) поднимала воркеры от нуля: узел без
            # движков начинал брать задания и ронять их, а с движками —
            # считать без отметки жизни, и сосед через пять минут забирал
            # задание и считал его второй раз.
            self.settings.set("max_concurrent_jobs", workers)
            return
        with self._lock:
            # Считаем по заявленному числу, а не по длине словаря: ушедший
            # воркер уже убрал своё состояние, и по длине выходило, что
            # воркеров меньше, чем есть на самом деле.
            current = self._worker_count
            # Снятие пометки — до ветвления, а не внутри «стало больше».
            # Помеченный воркер уходит не сразу: он замечает пометку, только
            # когда проснётся. Уменьшить и тут же вернуть обратно означало,
            # что ни одна ветка не сработала — `current` ещё прежний, — и
            # пометка оставалась. Воркеры досыпали, выходили, и сервер жил с
            # половиной заявленных до самого перезапуска, показывая в
            # состоянии полное число.
            self._retiring -= set(range(workers))
            if workers > current:
                for index in range(current, workers):
                    # Поднимаем поток только там, где его нет. Пометка на
                    # выход снята строкой выше, а помеченный воркер уходит
                    # не сразу — он замечает её, лишь когда проснётся.
                    # Поэтому после «4 → 1 → 4» (две правки подряд на
                    # странице настроек или отказ от только что сделанной)
                    # индексы 1–3 всё ещё заняты живыми потоками: прежний
                    # код заводил им вторые, да ещё и затирал WorkerState.
                    # Три лишних потока ОС на каждую правку, и два потока
                    # на одно состояние — страница очереди показывала не то
                    # задание, а уходящий воркер убирал состояние из-под
                    # живого соседа того же номера.
                    if index in self._states:
                        continue
                    self._states[index] = WorkerState(index=index)
                    self._поднять_воркера(index)
            elif workers < current:
                self._retiring.update(range(workers, current))
            self._worker_count = workers
            # Ушедшие потоки из перечня убираем: по его длине делится общий
            # таймаут остановки, и с каждым изменением числа воркеров доля
            # на поток становилась меньше — на давно живущем сервере
            # остановка переставала ждать вообще.
            self._workers = [t for t in self._workers if t.is_alive()]
        self.settings.set("max_concurrent_jobs", workers)
        self._wake.set()

    # --- выбор следующего задания ----------------------------------------

    def _next_job(self, worker_index: int) -> dict[str, Any] | None:
        # Флаг паузы — общий для всех серверов над базой; читается вне
        # общей блокировки очереди, она нужна идущим заданиям.
        приостановлена = self._пауза_из_базы()
        предел = int(self.settings.get("max_concurrent_jobs") or 2)
        if приостановлена or worker_index >= предел:
            return None
        with self._lock:
            if len(self._running) >= предел:
                return None
            идущие = list(self._running)
        per_model_limit = int(self.settings.get("max_concurrent_per_model") or 0)
        policy = str(self.settings.get("scheduling_policy") or "priority_fifo")
        # Кандидаты читаются вне общего замка очереди: он нужен идущим
        # заданиям (прогресс, отмена, учёт слотов), а запрос к базе раз в
        # секунду на каждый простаивающий воркер держал его зря. Под замком
        # остаются только выбор и занятие слота; гонку с соседним воркером
        # закрывает отсев по `_running` под замком и атомарный захват в базе.
        candidates = self._кандидаты(policy, per_model_limit)
        candidates = [c for c in candidates if c["id"] not in идущие]
        if candidates:
            # Задание, у которого прямо сейчас распознаётся близнец (та же
            # запись, те же настройки, тот же владелец), ждёт его, а не
            # гоняет модель второй раз: см. `_взять_у_близнеца`. Близнец
            # может идти и на соседнем сервере — поэтому проверка по базе,
            # а не только по своим слотам.
            try:
                ждут = self.db.ждут_близнеца([c["id"] for c in candidates], идущие)
            except Exception as exc:                         # noqa: BLE001
                log.debug("Близнецы в очереди не проверены: %s", exc)
                ждут = set()
            candidates = [c for c in candidates if c["id"] not in ждут]
        with self._lock:
            active = len(self._running)
            if active >= предел:
                return None
            candidates = [c for c in candidates if c["id"] not in self._running]
            if per_model_limit > 0:
                candidates = [c for c in candidates
                              if self._model_counts.get(c.get("model") or "", 0) < per_model_limit]
            if not candidates:
                return None

            chosen = self._apply_policy(candidates, policy)
            if chosen is None:
                return None

            self._running[chosen["id"]] = time.time()
            self._concurrency[chosen["id"]] = 0
            self._note_concurrency()
            model = chosen.get("model") or ""
            self._model_counts[model] = self._model_counts.get(model, 0) + 1
            владелец = str(chosen.get("owner") or "anonymous")
            if "_чьи_идут" not in self.__dict__:
                # Очередь, собранная проверкой через `__new__`, без `__init__`.
                self._чьи_идут, self._последний_старт = {}, {}
            self._чьи_идут[chosen["id"]] = владелец
            self._последний_старт[владелец] = time.monotonic()
            if len(self._последний_старт) > 4096:
                # Словарь не растёт без предела: забываем тех, кого
                # обслуживали давнее всех, — они и так впереди.
                for старый in sorted(self._последний_старт,
                                     key=lambda к: self._последний_старт[к])[:2048]:
                    self._последний_старт.pop(старый, None)

        # Слот занят. Дальше два обращения к базе, и любой их срыв обязан
        # слот вернуть: освобождение живёт в _worker_loop, но только для
        # заданий, которые тот успел получить. Без отката одна ошибка «база
        # заблокирована» навсегда съедала слот, а при достижении предела
        # очередь переставала брать работу до перезапуска.
        claimed = False
        try:
            # Захват атомарный: UPDATE ... WHERE status IN (...) выполняет
            # только один процесс, остальным вернётся False. Без этого два
            # сервера на общей базе брали одно и то же задание — каждый
            # читал его как «в очереди» и записывал «выполняется» поверх
            # другого, и запись обрабатывалась дважды.
            claimed = self.db.update_job_if_status(
                chosen["id"], [STATUS_QUEUED, STATUS_RETRY],
                status=STATUS_RUNNING, started_at=now(),
                stage="запуск", progress=0.0,
                instance_id=INSTANCE_ID, heartbeat_at=now(),
                queue_time_s=max(0.0, now() - float(
                    chosen.get("queued_at") or chosen.get("created_at") or now())))
            if not claimed:
                # Задание успел взять другой экземпляр (или его отменили) —
                # это не ошибка, просто берём следующее.
                self._release_slot(chosen["id"], model)
                return None
            job = self.db.get_job(chosen["id"])
        except Exception:
            self._release_slot(chosen["id"], model)
            if claimed:
                # Захват прошёл, а чтение — нет: задание «выполняется» за
                # нами, но ни один воркер его не считает. Откатываем захват;
                # не вышло и это — его подберёт сверка своих заданий
                # (`_сверить_свои`), а не только следующий запуск.
                try:
                    self.db.update_job_if_status(
                        chosen["id"], [STATUS_RUNNING], expected_instance=INSTANCE_ID,
                        status=STATUS_QUEUED, stage="", progress=0.0, started_at=None,
                        instance_id=None, heartbeat_at=None)
                except Exception as exc:                    # noqa: BLE001
                    log.debug("Захват %s не откатан: %s", chosen["id"], exc)
            raise
        if job is None:
            # Задание удалили между выборкой и чтением.
            self._release_slot(chosen["id"], model)
            return None
        return job

    def _кандидаты(self, policy: str, per_model_limit: int) -> list[dict[str, Any]]:
        """Окно ждущих заданий для выбора — в порядке, который требует политика."""
        # Порядок предварительной выборки должен соответствовать политике,
        # иначе она работает на случайной верхушке очереди: срочное
        # задание с низким приоритетом не будет выбрано, пока не
        # разгребётся всё, что стоит выше.
        preselect = {
            "shortest_first": "media_duration_s ASC",
            # «По сроку» выбирает предварительно ПО СРОКУ, а не по
            # времени создания. Раньше стояло `created_at ASC`, и
            # срочное задание, поставленное последним, в окно выборки
            # не попадало: на очереди из шестисот заданий при окне в
            # пятьсот политика «по сроку» молча вырождалась в «по
            # очереди» — то есть не работала ровно в том случае, ради
            # которого её включают.
            "deadline": "deadline ASC",
            "fair_share": "created_at ASC",
        }.get(policy, "priority DESC")
        window = int(self.settings.get("scheduling_window") or 500)
        if policy == "fair_share":
            # По нескольку старших заданий на владельца, а не верх общей
            # очереди: иначе окно целиком состоит из заданий того, кто
            # поставил больше всех (см. `fair_share_candidates`).
            return self.db.fair_share_candidates(
                per_owner=max(3, per_model_limit + 2), ready_before=now(),
                owners=max(1, window))
        candidates = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY],
                                       limit=window, order=preselect, light=True,
                                       ready_before=now())
        if policy == "priority_fifo" and self._старение_с() > 0:
            # Состарившееся задание с низким приоритетом может не
            # попасть в окно «по приоритету» — добираем старейшие.
            есть = {c["id"] for c in candidates}
            candidates += [c for c in self.db.list_jobs(
                status=[STATUS_QUEUED, STATUS_RETRY], limit=min(window, 100),
                order="created_at ASC", light=True, ready_before=now())
                if c["id"] not in есть]
        return candidates

    def _note_concurrency(self) -> None:
        """Отмечает нынешнюю занятость у каждого идущего задания.

        Вызывается под общей блокировкой при каждом изменении состава
        работающих. Считать одновременность по началу и концу задания
        нельзя: всплеск в середине — а это ровно тот случай, когда памяти и
        не хватает, — не попал бы ни в одну из двух точек.
        """
        сейчас = len(self._running)
        for job_id in self._concurrency:
            if self._concurrency[job_id] < сейчас:
                self._concurrency[job_id] = сейчас

    def _release_slot(self, job_id: str, model: str) -> None:
        """Возвращает занятый слот и счётчик модели."""
        with self._lock:
            self._running.pop(job_id, None)
            self._concurrency.pop(job_id, None)
            getattr(self, "_чьи_идут", {}).pop(job_id, None)
            self._note_concurrency()
            if model in self._model_counts:
                self._model_counts[model] = max(0, self._model_counts[model] - 1)

    def _check_webhook_url(self, url: str) -> str:
        """Проверяет адрес уведомления — см. `check_outbound_url`."""
        return check_outbound_url(url, bool(self.settings.get("webhook_allow_internal", False)))

    def _check_disk_space(self) -> None:
        """Отказывает в приёме, пока на диске меньше порога свободного места.

        Заполнившийся диск повреждает и базу, и уже посчитанные результаты,
        поэтому честный отказ на входе дешевле, чем авария в середине ночи.
        """
        limit_gb = float(self.settings.get("disk_min_free_gb") or 0.0)
        if limit_gb <= 0:
            return
        import shutil as shutil_mod
        try:
            free_gb = shutil_mod.disk_usage(str(self.settings.paths.data)).free / 1024 ** 3
        except OSError:
            return          # не смогли измерить — не мешаем работать
        if free_gb < limit_gb:
            raise StorageError(
                f"На диске осталось {free_gb:.1f} ГБ при пороге {limit_gb:.1f} ГБ — "
                f"новые задания не принимаются.",
                hint="Освободите место или уменьшите disk_min_free_gb. "
                     "Старые задания и файлы удаляет POST /api/maintenance/cleanup.")

    def _apply_policy(self, candidates: list[dict[str, Any]],
                      policy: str) -> dict[str, Any] | None:
        if not candidates:
            return None
        if policy == "shortest_first":
            return min(candidates,
                       key=lambda c: (float(c.get("media_duration_s") or 1e9),
                                      -int(c.get("priority") or 0)))
        if policy == "deadline":
            with_deadline = [c for c in candidates if c.get("deadline")]
            if with_deadline:
                return min(with_deadline, key=lambda c: float(c["deadline"]))
            return max(candidates, key=lambda c: (int(c.get("priority") or 0),
                                                  -float(c.get("created_at") or 0)))
        if policy == "fair_share":
            # Круговое обслуживание владельцев: задание того, у кого сейчас
            # меньше всего идущих, а при равенстве — кого обслуживали давнее
            # всех. Прежде второго ключа не было: при одном воркере в момент
            # выбора не идёт ничего, все владельцы равны, и бралось самое
            # старое задание — то есть FIFO: отдел, поставивший пятьсот
            # записей, держал одинокую запись соседа до конца своих. При
            # нескольких воркерах политика не помнила, кого обслужила
            # последним, и третий владелец ждал, пока кончатся задания
            # второго.
            load: dict[str, int] = {}
            чьи = getattr(self, "_чьи_идут", {})
            for job_id in self._running:
                owner = чьи.get(job_id) or "anonymous"
                load[owner] = load.get(owner, 0) + 1
            когда = getattr(self, "_последний_старт", {})
            return min(candidates,
                       key=lambda c: (load.get(c.get("owner") or "anonymous", 0),
                                      когда.get(c.get("owner") or "anonymous", 0.0),
                                      -int(c.get("priority") or 0),
                                      float(c.get("created_at") or 0)))
        # priority_fifo
        шаг = self._старение_с()
        сейчас = now()

        def приоритет(c: dict[str, Any]) -> float:
            свой = int(c.get("priority") or 0)
            if шаг <= 0:
                return свой
            ждёт = max(0.0, сейчас - float(c.get("queued_at") or c.get("created_at") or сейчас))
            return свой + int(ждёт // шаг)

        return max(candidates, key=lambda c: (приоритет(c),
                                              -float(c.get("created_at") or 0)))

    def _старение_с(self) -> float:
        """Шаг старения приоритета в секундах; 0 — выключено."""
        try:
            return max(0.0, float(self.settings.get("priority_aging_minutes") or 0) * 60.0)
        except (TypeError, ValueError):
            return 0.0

    # --- состояние --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        counts = {status: self.db.count_jobs(status=status)
                  for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY,
                                 STATUS_PAUSED, STATUS_COMPLETED, STATUS_FAILED,
                                 STATUS_CANCELLED, *EMPTY_STATUSES)}
        # Звук в очереди и ожидание — по всей очереди одним запросом, с
        # разбивкой по моделям. Раньше суммировались первые 500 заданий, и
        # при очереди в 3000 оценка занижалась вшестеро: «очередь большая,
        # а ждать недолго». Скорость берётся у модели самого задания; у
        # модели без истории — средняя по остальным.
        по_моделям = self.db.query(
            "SELECT model, SUM(COALESCE(media_duration_s, 0)) AS audio FROM jobs "
            "WHERE status IN (?, ?) GROUP BY model", (STATUS_QUEUED, STATUS_RETRY))
        stats = self.db.model_stats()
        скорость = {s["model"]: s["rtf_avg"] for s in stats if s.get("rtf_avg")}
        avg_rtf = (sum(скорость.values()) / len(скорость)) if скорость else 0.25
        pending_audio = sum(float(r["audio"] or 0) for r in по_моделям)
        работа = sum(float(r["audio"] or 0) * скорость.get(r["model"], avg_rtf)
                     for r in по_моделям)
        workers = int(self.settings.get("max_concurrent_jobs") or 2)
        eta = (работа / max(1, workers)) if pending_audio else 0.0
        # Какие экземпляры сейчас держат задания. На одном сервере это всегда
        # он сам; на нескольких — видно, кто чем занят, и сразу заметно, если
        # один перестал отвечать и его задания вот-вот вернутся в очередь.
        instances: list[dict[str, Any]] = []
        try:
            for row in self.db.query(
                    "SELECT instance_id, COUNT(*) AS jobs, MAX(heartbeat_at) AS beat "
                    "FROM jobs WHERE status=? AND instance_id IS NOT NULL "
                    "GROUP BY instance_id", (STATUS_RUNNING,)):
                beat = float(row["beat"] or 0)
                instances.append({
                    "instance": row["instance_id"],
                    "self": row["instance_id"] == INSTANCE_ID,
                    "jobs": int(row["jobs"] or 0),
                    "last_seen_s": round(max(0.0, now() - beat), 1) if beat else None,
                    "stale": bool(beat and now() - beat > STALE_AFTER_S),
                })
        except Exception as exc:                        # noqa: BLE001
            log.debug("Список экземпляров не собран: %s", exc)

        return {
            # Флаг паузы общий для всех серверов над базой: берётся из неё,
            # а не из памяти — на узле без воркеров память не обновлялась, и
            # остановленная соседом очередь показывалась идущей.
            "paused": self.is_paused,
            "instance": INSTANCE_ID,
            "instances": instances,
            "workers": [s.to_dict() for _, s in sorted(self._states.items())],
            "worker_count": workers,
            "counts": counts,
            "queue_depth": counts[STATUS_QUEUED] + counts[STATUS_RETRY],
            "active": len(self._running),
            "pending_audio_s": round(pending_audio, 1),
            "eta_s": round(eta, 1),
            "policy": self.settings.get("scheduling_policy"),
            "max_queue_size": self._max_queue,
            "loaded_models": self.registry.loaded(),
        }
