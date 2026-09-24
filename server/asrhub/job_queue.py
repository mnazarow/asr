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

import random
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import model_files
from . import settings_access as S
from .catalog import get_model
from .config import Settings
from .db import SAMPLE_PERIOD_S, Database, new_id, now
from .engines import EngineRegistry
from .errors import (
    ASRHubError,
    ConfigError,
    JobCancelled,
    JobNotFound,
    OutOfMemoryError,
    QueueFull,
    StorageError,
    classify_exception,
)
from .instance import HOSTNAME, process_alive
from .instance import INSTANCE_ID as _INSTANCE_ID
from .logging_setup import get_logger
from .monitoring.collector import (
    JOB_DURATION_BUCKETS,
    MEDIA_DURATION_BUCKETS,
    RUNTIME,
)
from .pipeline import audio_profile
from .pipeline import metrics as metrics_mod
from .processor import cleanup_workdir, process_job, safe_workdir, settings_digest

log = get_logger("queue")


#: Кто мы такие среди серверов на общей базе (см. `instance`). Имя
#: сохранено здесь же: на него ссылается и код, и тесты.
INSTANCE_ID = _INSTANCE_ID

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

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_PAUSED = "paused"
STATUS_RETRY = "retry"

ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY, STATUS_PAUSED)


def check_outbound_url(url: str, allow_internal: bool = False) -> str:
    """Проверяет адрес, на который сервер пойдёт сам: обратный вызов, трекер.

    Адрес приходит из запроса или настроек и уходит в urlopen, поэтому без
    проверки сервер становится инструментом обращения к внутренней сети от
    своего имени: file:// читает диск, а http://169.254.169.254 достаёт
    учётные данные облака. Пропускаем только http и https и запрещаем
    адреса, которые указывают внутрь.

    Узел проверяется по тем адресам, в которые он на самом деле
    превращается. Раньше проверка смотрела только на запись вида
    «10.0.0.1»: `ipaddress` не понимает «127.1», «2130706433», «0x7f000001»
    и «017700000001», считал их доменными именами «проверять некому» — а
    сокет затем честно превращал их в 127.0.0.1. Доменное имя тоже
    превращается в адреса здесь же: имя, указывающее на 127.0.0.1, ничем
    не лучше самого 127.0.0.1.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ConfigError(
            f"Адрес уведомления должен начинаться с http:// или https:// "
            f"(получено «{parsed.scheme or url[:20]}»).",
            hint="Другие схемы запрещены: через них сервер читал бы "
                 "собственные файлы вместо отправки уведомления.")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ConfigError("В адресе уведомления не указан узел.")

    if allow_internal:
        return url

    lowered = host.lower()
    if lowered in ("localhost", "localhost.localdomain") or lowered.endswith(".localhost"):
        raise ConfigError(
            "Адрес уведомления указывает на сам сервер.",
            hint="Если это намеренно, включите webhook_allow_internal.")
    for address in _адреса_узла(host):
        if _внутренний(address):
            raise ConfigError(
                f"Адрес уведомления {host} находится во внутренней сети ({address}).",
                hint="Если это намеренно, включите webhook_allow_internal.")
    return url


def _адреса_узла(host: str) -> list[Any]:
    """Во что превращается узел: запись адреса или имя, которое ещё надо узнать."""
    import ipaddress
    import socket

    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    # Старые записи IPv4, которые понимает сокет, но не `ipaddress`.
    try:
        return [ipaddress.IPv4Address(socket.inet_aton(host))]
    except (OSError, ValueError):
        pass
    try:
        ответы = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        # Имя не узнаётся сейчас — не узнается и при отправке: запрос не
        # уйдёт никуда, и запрещать тут нечего.
        return []
    адреса = []
    for ответ in ответы:
        try:
            адреса.append(ipaddress.ip_address(str(ответ[4][0]).split("%", 1)[0]))
        except ValueError:
            continue
    return адреса


def _внутренний(address: Any) -> bool:
    """Адрес смотрит внутрь: сам сервер, локальная сеть, служебные диапазоны."""
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(address.is_loopback or address.is_private or address.is_link_local
                or address.is_reserved or address.is_multicast or address.is_unspecified)


def открыватель_наружу(allow_internal: bool = False,
                       проверка: Callable[[str], Any] | None = None) -> Any:
    """urllib-открыватель, который проверяет и каждое перенаправление.

    Обычный urlopen идёт по 301/302 куда скажут, превращая POST в GET: внешний
    приёмник, ответивший «302 → http://169.254.169.254/…», заставлял сервер
    сходить туда самому, а поле webhook_status работало оракулом. `проверка`
    заменяет проверку по умолчанию — у маршрута /process-call она своя.
    """
    import urllib.request

    def проверить(адрес: str) -> None:
        if проверка is not None:
            проверка(адрес)
        else:
            check_outbound_url(адрес, allow_internal)

    class Проверенные(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
            проверить(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(Проверенные)


def _внутри(корень: Path, путь: str) -> Path | None:
    """Путь, если он лежит внутри каталога `корень`, иначе None."""
    if not путь:
        return None
    try:
        база = корень.resolve(strict=True)
        итог = Path(путь).resolve(strict=True)
    except OSError:
        return None
    return итог if база in итог.parents else None


@dataclass
class WorkerState:
    index: int
    job_id: str | None = None
    model: str = ""
    started_at: float = 0.0
    stage: str = ""
    progress: float = 0.0
    busy: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = {
            "index": self.index,
            "busy": self.busy,
            "job_id": self.job_id,
            "model": self.model,
            "stage": self.stage,
            "progress": round(self.progress, 3),
        }
        if self.busy and self.started_at:
            data["elapsed_s"] = round(time.time() - self.started_at, 1)
        return data


class JobQueue:
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
        self._wake = threading.Event()
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
        self._states = {i: WorkerState(index=i) for i in range(count)}
        self._worker_count = count
        for index in range(count):
            thread = threading.Thread(target=self._worker_loop, args=(index,),
                                      name=f"asrhub-worker-{index}", daemon=True)
            thread.start()
            self._workers.append(thread)
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
        self._wake.set()
        for thread in self._workers:
            thread.join(timeout=timeout / max(1, len(self._workers)))
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
        with self._lock:
            свои = list(self._идут)
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

    # --- добавление -----------------------------------------------------

    def submit(self, *, file_path: Path, filename: str, settings: dict[str, Any],
               owner: str = "anonymous", api_key_name: str = "", priority: int | None = None,
               group_id: str | None = None, source: str = "api",
               tags: str = "", deadline: float | None = None,
               reference_text: str = "", webhook_url: str = "") -> dict[str, Any]:
        """Ставит файл в очередь. Возвращает описание задания."""
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
        depth = self.db.count_jobs(status=[STATUS_QUEUED, STATUS_RETRY])
        if depth >= self._max_queue:
            raise QueueFull(f"В очереди уже {depth} заданий (предел {self._max_queue}).")

        self._check_disk_space()

        path = Path(file_path)
        from .pipeline.audio import file_hash, probe

        digest = file_hash(path)
        spec = get_model(str(settings.get("model") or ""))
        params_digest = settings_digest(settings, weights=self._отпечаток_весов(spec))

        duration = 0.0
        try:
            duration = probe(path).duration_s
        except ASRHubError as exc:
            log.info("Не удалось определить длительность «%s»: %s", filename, exc.message)

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
                                            reference_text=reference_text)
                RUNTIME.inc("asrhub_cached_jobs_total")
                self._emit("job.cached", {"id": job_id, "source": cached["id"]})
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

        job_id = self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": str(path),
            "file_size": path.stat().st_size if path.exists() else 0,
            "file_hash": digest,
            "media_duration_s": duration,
            "engine": str(settings.get("engine") or (spec.engine if spec else "")),
            "model": str(settings.get("model") or ""),
            "language": str(settings.get("language") or ""),
            "params": payload,
            # Тот же предел 0–100, что у POST /{id}/priority: без него поле
            # формы priority=1000000 при политике priority_fifo ставило
            # задание впереди всех чужих, а отрицательное — позади навсегда.
            "priority": max(0, min(100, int(priority if priority is not None
                                            else settings.get("priority", 50)))),
            "owner": owner,
            "api_key_name": api_key_name,
            "source": source,
            "tags": tags,
            "deadline": deadline,
            "reference_text": reference_text or None,
            "webhook_url": webhook_url or None,
        })
        self._emit("job.queued", {"id": job_id, "filename": filename})
        self._wake.set()
        return self.get(job_id)

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
                      reference_text: str = "") -> str:
        job_id = self.db.create_job({
            "id": new_id(),
            "group_id": group_id,
            "filename": filename,
            "file_path": path,
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
            # Профиль звука — свойство записи, а запись та же.
            **audio_profile.for_job(cached),
            progress=1.0, stage="из кеша")
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
        return job_id

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
        if job["status"] in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED):
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
        jobs = self.db.list_jobs(group_id=group_id, status=list(ACTIVE_STATUSES), limit=10000)
        for job in jobs:
            self.cancel(job["id"], by="group")
        return len(jobs)

    def cancel_all(self) -> int:
        jobs = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY, STATUS_PAUSED], limit=10000)
        for job in jobs:
            self.cancel(job["id"], by="all")
        return len(jobs)

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
        self.db.update_job(job_id, status=STATUS_QUEUED, error_code=None,
                           error_message=None, error_hint=None, progress=0.0,
                           stage="", started_at=None, finished_at=None,
                           retries=0, params=params, queued_at=now())
        self.db.add_event(job_id, "retry", "Задание поставлено в очередь повторно")
        with self._lock:
            self._cancelled.discard(job_id)
        self._wake.set()
        self._emit("job.queued", {"id": job_id})
        return self.get(job_id)

    def retry_failed(self, limit: int = 100) -> int:
        jobs = self.db.list_jobs(status=STATUS_FAILED, limit=limit)
        for job in jobs:
            self.retry(job["id"])
        return len(jobs)

    def set_priority(self, job_id: str, priority: int) -> dict[str, Any]:
        priority = max(0, min(100, int(priority)))
        self.db.update_job(job_id, priority=priority)
        self.db.add_event(job_id, "priority", f"Приоритет изменён на {priority}")
        self._wake.set()
        return self.get(job_id)

    def move_to_top(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 100)

    def move_to_bottom(self, job_id: str) -> dict[str, Any]:
        return self.set_priority(job_id, 0)

    def pause_job(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == STATUS_QUEUED:
            self.db.update_job(job_id, status=STATUS_PAUSED, stage="приостановлено")
            self.db.add_event(job_id, "paused", "Задание приостановлено")
        return self.get(job_id)

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self.get(job_id)
        if job["status"] == STATUS_PAUSED:
            self.db.update_job(job_id, status=STATUS_QUEUED, stage="")
            self.db.add_event(job_id, "resumed", "Задание возобновлено")
            self._wake.set()
        return self.get(job_id)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
        self.db.add_event(None, "queue_paused", "Очередь приостановлена")
        self._emit("queue.paused", {})

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        self._wake.set()
        self.db.add_event(None, "queue_resumed", "Очередь возобновлена")
        self._emit("queue.resumed", {})

    @property
    def is_paused(self) -> bool:
        return self._paused

    def set_concurrency(self, workers: int) -> None:
        """Меняет число воркеров на ходу.

        Новые потоки запускаются сразу; лишние помечаются на выход и
        завершаются, доработав текущее задание. Без этой пометки потоки
        оставались жить, а последовательность 2 → 8 → 2 оставляла восемь
        живых воркеров при заявленных двух.
        """
        workers = max(1, min(64, int(workers)))
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
                    thread = threading.Thread(target=self._worker_loop, args=(index,),
                                              name=f"asrhub-worker-{index}", daemon=True)
                    thread.start()
                    self._workers.append(thread)
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
        with self._lock:
            if self._paused or worker_index >= int(
                    self.settings.get("max_concurrent_jobs") or 2):
                return None
            active = len(self._running)
            if active >= int(self.settings.get("max_concurrent_jobs") or 2):
                return None
            per_model_limit = int(self.settings.get("max_concurrent_per_model") or 0)

            policy = str(self.settings.get("scheduling_policy") or "priority_fifo")
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
            candidates = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY],
                                           limit=window, order=preselect, light=True,
                                           ready_before=now())
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

        # Слот занят. Дальше два обращения к базе, и любой их срыв обязан
        # слот вернуть: освобождение живёт в _worker_loop, но только для
        # заданий, которые тот успел получить. Без отката одна ошибка «база
        # заблокирована» навсегда съедала слот, а при достижении предела
        # очередь переставала брать работу до перезапуска.
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
            raise
        if job is None:
            # Задание удалили между выборкой и чтением.
            self._release_slot(chosen["id"], model)
            return None
        return job

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
            # Круговое обслуживание владельцев: берём задание того, у кого
            # сейчас меньше всего выполняющихся заданий.
            load: dict[str, int] = {}
            for job_id in self._running:
                job = self.db.get_job(job_id)
                if job:
                    owner = job.get("owner") or "anonymous"
                    load[owner] = load.get(owner, 0) + 1
            return min(candidates,
                       key=lambda c: (load.get(c.get("owner") or "anonymous", 0),
                                      -int(c.get("priority") or 0),
                                      float(c.get("created_at") or 0)))
        # priority_fifo
        return max(candidates, key=lambda c: (int(c.get("priority") or 0),
                                              -float(c.get("created_at") or 0)))

    # --- рабочий цикл -----------------------------------------------------

    def _worker_loop(self, index: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                if index in self._retiring:
                    self._retiring.discard(index)
                    # Убираем только себя: соседи по номерам живы.
                    self._states.pop(index, None)
                    log.info("Воркер %d завершён: число воркеров уменьшено", index)
                    return
            job = None
            try:
                job = self._next_job(index)
            except Exception as exc:
                log.error("Ошибка выбора задания: %s", exc)
            if job is None:
                self._wake.wait(timeout=1.0)
                self._wake.clear()
                continue
            # Состояние берём под замком: соседний воркер может уйти и
            # убрать своё в тот же момент. Раньше здесь была проверка длины
            # списка и обращение по индексу без замка — между ними успевал
            # вклиниться `del`, и поток умирал с IndexError уже ПОСЛЕ
            # захвата задания: слот в `_running` не освобождался никогда, а
            # очередь теряла воркера навсегда.
            with self._lock:
                state = self._states.get(index) or WorkerState(index)
            state.busy = True
            state.job_id = job["id"]
            state.model = job.get("model") or ""
            state.started_at = time.time()
            state.progress = 0.0
            try:
                self._execute(job, state)
            except Exception as exc:
                log.exception("Непредвиденная ошибка воркера: %s", exc)
                # Задание уже захвачено и стоит в базе как «выполняется».
                # Сбой до конвейера (кончилось место под рабочий каталог,
                # неверные настройки задания, отказ базы на событии
                # «старт») сюда и приходит — и без этой строки задание
                # висело бы «выполняется, 0 %» вечно: подхват зависших
                # берёт только чужие экземпляры, а свои — лишь при старте.
                self._fail_unexpected(job, exc)
            finally:
                # Освобождение — одной функцией, а не второй копией здесь.
                # Копия и разошлась: она снимала слот и счётчик модели, но не
                # снимала отметку одновременности, и та копилась по записи на
                # каждое проведённое задание. Мало того что без предела —
                # `_note_concurrency` проходит этот словарь под общей
                # блокировкой при каждом старте, и на сотне тысяч заданий
                # выходило девять миллисекунд блокировки на задание. Со
                # стороны это выглядит как «сервер к вечеру тупеет».
                self._release_slot(job["id"], str(job.get("model") or ""))
                state.busy = False
                state.job_id = None
                state.stage = ""
                state.progress = 0.0
                self._wake.set()

    def _execute(self, job: dict[str, Any], state: WorkerState) -> None:
        job_id = job["id"]
        params = dict(job.get("params") or {})
        merged = self.settings.merged(params)
        merged["reference_text"] = job.get("reference_text") or ""
        merged.setdefault("hf_token", self.settings.hf_token)

        paths = self.settings.paths
        workdir = safe_workdir(paths.tmp, job_id)
        outdir = paths.results / job_id
        timeout = max(0, S.integer(merged, "job_timeout_s", 0))
        started = time.time()

        def progress(value: float, stage: str) -> None:
            # Полоса только вперёд: этапы конвейера и движки докладывают
            # долю каждый по-своему, и откат с 0,82 на 0,80 выглядел как
            # «задание пошло назад».
            value = max(float(value), state.progress)
            state.progress = value
            state.stage = stage
            # Запись — только пока задание за нами. Безусловная запись
            # переписывала стадию и прогресс уже отменённого задания: отмена,
            # пришедшая на соседний сервер, жила только в памяти его
            # процесса, а этот досчитывал до конца, превращая «отменено» в
            # «распознавание, 76 %». Не записалось — задание больше не наше:
            # останавливаемся на ближайшей проверке отмены.
            своё = self.db.update_job_if_status(
                job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID,
                progress=round(value, 4), stage=stage, heartbeat_at=now())
            if not своё:
                self._отобрано(job_id)
                return
            self._emit("job.progress", {"id": job_id, "progress": round(value, 4),
                                        "stage": stage})

        def cancelled() -> bool:
            # Остановка сервера — тоже повод прерваться: задание вернётся в
            # очередь без траты попытки (см. `_handle_failure`).
            if self._stop.is_set():
                return True
            with self._lock:
                return job_id in self._cancelled

        self.db.add_event(job_id, "started", f"Обработка начата: {job.get('filename')}")
        self._emit("job.started", {"id": job_id, "filename": job.get("filename")})
        log.info("Задание %s: старт (%s)", job_id, job.get("model"),
                 extra={"job_id": job_id, "model": job.get("model")})

        # Счётчики пика — и у torch, и у psutil — общие на процесс, поэтому
        # замер всегда получается «сколько занял сервер», а не «сколько
        # заняла эта модель». Раньше из-за этого замер делался только когда
        # задание в очереди одно — и на сервере с двумя воркерами, то есть
        # там, где вопрос «хватит ли карты» и стоит, раздел «Ресурсы»
        # оставался пустым.
        #
        # Теперь мерим всегда, а рядом пишем, сколько заданий шло разом.
        # «При двух заданиях карта поднималась до 27 ГБ из 32» — это и есть
        # ответ, нужный для планирования, и он не требует делить память
        # между заданиями, чего счётчики всё равно не умеют.
        #
        # Обнуление — только когда задание стартует в одиночестве: иначе оно
        # стёрло бы то, что уже накопил сосед по очереди, и его собственный
        # замер оказался бы заниженным.
        with self._lock:
            одно = len(self._running) <= 1
            self._идут.add(job_id)
        try:
            self._execute_claimed(job, state, merged, workdir=workdir, outdir=outdir,
                                  timeout=timeout, started=started, одно=одно,
                                  progress=progress, cancelled=cancelled)
        finally:
            with self._lock:
                self._идут.discard(job_id)
                # Исполнение кончилось — пометка отмены больше ничего не
                # значит. Отметка жизни могла поставить её в последний миг,
                # между записью «готово» и этой строкой.
                self._cancelled.discard(job_id)

    def _отобрано(self, job_id: str) -> None:
        """Задание больше не за нами: его отменили на соседнем сервере,
        удалили или отдали другому экземпляру. Останавливаем конвейер на
        ближайшей проверке отмены."""
        with self._lock:
            if job_id not in self._идут or job_id in self._cancelled:
                return
            self._cancelled.add(job_id)
        log.info("Задание %s больше не за этим сервером — обработка прерывается",
                 job_id, extra={"job_id": job_id})

    def _execute_claimed(self, job: dict[str, Any], state: WorkerState,
                         merged: dict[str, Any], *, workdir: Path, outdir: Path,
                         timeout: int, started: float, одно: bool,
                         progress: Callable[[float, str], None],
                         cancelled: Callable[[], bool]) -> None:
        job_id = job["id"]
        try:
            outcome = process_job(
                Path(job["file_path"]), merged, self.registry,
                workdir=workdir, outdir=outdir,
                basename=Path(job.get("filename") or job_id).stem,
                filename=str(job.get("filename") or ""),
                progress=progress, cancelled=cancelled,
                deadline=(started + timeout) if timeout else None, timeout_s=timeout,
                measure_memory=True, reset_memory=одно)
        except ASRHubError as exc:
            self._handle_failure(job, exc, merged, outdir=outdir)
            return
        except Exception as exc:
            self._handle_failure(job, classify_exception(
                exc, engine=str(job.get("engine")), model=str(job.get("model"))), merged,
                outdir=outdir)
            return
        finally:
            cleanup_workdir(workdir)

        elapsed = time.time() - started
        accuracy = outcome.stats.get("accuracy") or {}
        # Реплики пишутся ДО отметки «готово», а не после. Порядок решает:
        # между отметкой и записью реплик стоял незащищённый вызов, и любое
        # исключение там (занятая база — обычное дело на общем архиве)
        # оставляло задание в состоянии «готово» с `segments_count = 40` и
        # пустой таблицей реплик. Восстановиться из этого состояния не
        # может ничто: подхват зависших ищет «выполняется», а обработчик
        # сбоя пишет только из него же.
        #
        # Если отметка ниже не пройдёт (задание отменили или отобрали),
        # реплики останутся без задания — их уберёт `delete_job` или
        # подметание сирот в уборке. Это несравнимо дешевле, чем «готово»
        # без текста.
        self.db.save_segments(job_id, outcome.segments)
        # Только из состояния «выполняется». Отмена аккуратно сверяет статус
        # перед записью, а обратное направление было незащищено: отмена,
        # пришедшая между последней проверкой в конвейере и этой записью,
        # затиралась, и пользователь, которому уже ответили «отменено»,
        # получал задание в состоянии «готово».
        finished = self.db.update_job_if_status(
            job_id,
            [STATUS_RUNNING],
            # Задание могли отобрать, пока мы считали: если отметка жизни
            # устарела, его уже перезапустил сосед. Своё имя в условии
            # означает «пишем, только если оно всё ещё за нами».
            expected_instance=INSTANCE_ID,
            status=STATUS_COMPLETED, finished_at=now(), progress=1.0, stage="готово",
            instance_id=None, heartbeat_at=None,
            text=outcome.text,
            result_path=str(outdir),
            segments_count=len(outcome.segments),
            words_count=int(outcome.stats.get("words") or 0),
            chars_count=int(outcome.stats.get("chars") or 0),
            speakers_count=len(outcome.speakers),
            avg_confidence=outcome.stats.get("avg_confidence"),
            rtf=outcome.rtf,
            processing_time_s=round(elapsed, 3),
            audio_prep_s=outcome.timings.get("audio_prep"),
            model_load_s=outcome.timings.get("model_load"),
            inference_s=outcome.timings.get("inference"),
            postprocess_s=outcome.timings.get("postprocess"),
            # Поиск речи, выравнивание и разделение по говорящим конвейер
            # мерил всегда, а база не хранила: метрика стадий обещала
            # «долю diarization», которой в выгрузке не было никогда.
            vad_s=outcome.timings.get("vad"),
            alignment_s=outcome.timings.get("alignment"),
            diarization_s=outcome.timings.get("diarization"),
            language=outcome.language,
            device=str(outcome.stats.get("device") or merged.get("device") or ""),
            peak_memory_mb=outcome.peak_memory_mb or None,
            peak_memory_jobs=max(1, self._concurrency.get(job_id, 1)),
            waveform=outcome.waveform,
            calibration=outcome.stats.get("calibration") or None,
            **metrics_mod.job_fields(accuracy),
            **audio_profile.for_job(outcome.stats.get("audio_profile") or {}),
        )
        if not finished:
            # Задание успели отменить или удалить, пока оно считалось.
            # Записывать результат некуда — убираем и файлы выгрузки.
            log.info("Задание %s завершилось, но его состояние уже изменено — "
                     "результат отброшен", job_id, extra={"job_id": job_id})
            self._discard_unless_taken(outdir, job_id)
            self.db.clear_segments(job_id)
            with self._lock:
                self._cancelled.discard(job_id)
            return
        with self._lock:
            self._cancelled.discard(job_id)

        # Здоровье распознавания — тут же, по тем же сегментам: галлюцинации
        # Whisper, невозможный темп, повторы, известные фразы, разметка
        # говорящих. Считается всегда, а не только при включённом разборе
        # содержания: это признак самого распознавания, и сбой внутри не
        # должен трогать результат.
        try:
            from . import quality  # noqa: PLC0415

            оценка = quality.assess(
                outcome.segments,
                expected_speakers=int(
                    self.settings.get("quality_expected_speakers") or 0)
                if self.settings is not None else 0)
            self.db.update_job(job_id, **quality.for_job(оценка))
        except Exception as exc:                             # noqa: BLE001
            log.warning("Признаки качества для %s не посчитаны: %s", job_id, exc,
                        extra={"job_id": job_id})

        # Контрольный прогон второй моделью: расхождение с исходной
        # расшифровкой — в таблицу согласия моделей. Само задание — обычное,
        # но в разбор содержания оно не идёт: разговор уже разобран по
        # исходной записи.
        if (job.get("params") or {}).get("control_of"):
            try:
                from . import review  # noqa: PLC0415

                review.record_check(self.db, {**job, "model": job.get("model")},
                                    outcome.segments)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Расхождение контрольного прогона %s не записано: %s",
                            job_id, exc, extra={"job_id": job_id})
        # Разбор содержания — здесь, а не в фоновом потоке: признаки нужны
        # сразу, вместе с результатом. Стоит он десятки миллисекунд против
        # минут распознавания, а ошибки внутри не выходят наружу.
        if self.content_index is not None and str(job.get("source") or "") != "control":
            self.content_index.on_job_completed(
                job_id, {**job, "text": outcome.text,
                         "media_duration_s": job.get("media_duration_s")},
                outcome.segments)
        # Смысловой разбор — в свой поток: модель отвечает секунды.
        if self.llm_worker is not None and str(job.get("source") or "") != "control":
            try:
                self.llm_worker.enqueue(job_id)
            except Exception as exc:                         # noqa: BLE001
                log.debug("Запись %s не поставлена на смысловой разбор: %s", job_id, exc)

        RUNTIME.inc("asrhub_jobs_total", {"status": "completed"})
        RUNTIME.inc("asrhub_audio_seconds_total",
                    value=float(job.get("media_duration_s") or 0))
        RUNTIME.inc("asrhub_words_total", value=float(outcome.stats.get("words") or 0))
        RUNTIME.observe("asrhub_job_duration_seconds", elapsed, buckets=JOB_DURATION_BUCKETS)
        if job.get("media_duration_s"):
            RUNTIME.observe("asrhub_media_duration_seconds",
                            float(job["media_duration_s"]), buckets=MEDIA_DURATION_BUCKETS)

        self.db.bump_model_stats(
            str(job.get("model") or ""), str(job.get("engine") or ""), ok=True,
            audio_s=float(job.get("media_duration_s") or 0.0),
            processing_s=elapsed, words=int(outcome.stats.get("words") or 0),
            rtf=outcome.rtf, confidence=outcome.stats.get("avg_confidence"),
            wer=accuracy.get("wer"))
        for name, value in (("rtf", outcome.rtf),
                            ("processing_time_s", elapsed),
                            ("queue_time_s", float(job.get("queue_time_s") or 0.0)),
                            ("confidence", outcome.stats.get("avg_confidence") or 0.0)):
            self.db.add_metric(name, value, job_id=job_id, model=str(job.get("model")),
                               engine=str(job.get("engine")))
        for warning in outcome.warnings:
            self.db.add_event(job_id, "warning", warning)

        log.info("Задание %s: готово за %.1f с, RTF %.3f", job_id, elapsed, outcome.rtf,
                 extra={"job_id": job_id, "model": job.get("model")})
        # Дальше — только уведомление, и оно тоже не должно ронять готовое
        # задание: сбой здесь уходил в общий обработчик, а тот пытался
        # записать «ошибка» поверх «готово», не мог и не писал ничего —
        # даже в журнал событий задания.
        self.db.add_event(job_id, "completed",
                          f"Готово за {elapsed:.1f} с, RTF {outcome.rtf:.3f}")
        self._emit("job.completed", {"id": job_id, "rtf": outcome.rtf,
                                     "duration_s": elapsed})
        self._send_webhook(job_id)

        # Повтор удался — отложенный прежний результат больше не нужен.
        прежний = self._прежний_путь(job_id)
        if прежний.is_dir():
            shutil.rmtree(прежний, ignore_errors=True)

        if merged.get("delete_source_after"):
            self._удалить_исходник(job)

    def _удалить_исходник(self, job: dict[str, Any]) -> None:
        """Удаляет загруженный файл после обработки — только из uploads.

        Настройка обещает удалять «загруженный аудиофайл», а удаляла любой
        путь задания. Импорт с АТС ставит в очередь оригинал записи прямо в
        архиве станции, и с включённой настройкой каждое распознанное
        задание стирало разговор из архива Asterisk. Тот же файл бывает
        общим у нескольких заданий (контрольный прогон второй моделью идёт
        по записи исходного задания) — такой не трогаем, пока он нужен
        другому.
        """
        путь = _внутри(Path(self.settings.paths.uploads), str(job.get("file_path") or ""))
        if путь is None:
            return
        try:
            if self.db.file_used_elsewhere(str(job.get("file_path") or ""), job["id"]):
                return
            путь.unlink(missing_ok=True)
        except OSError:
            pass

    # --- прежний результат при повторе --------------------------------------

    def _прежний_путь(self, job_id: str) -> Path:
        return Path(self.settings.paths.results) / f"{job_id}.prev"

    def _отложить_прежний(self, job: dict[str, Any]) -> None:
        """Откладывает каталог готового результата на время повтора."""
        if job.get("status") != STATUS_COMPLETED:
            return
        текущий = Path(self.settings.paths.results) / str(job["id"])
        if not текущий.is_dir():
            return
        прежний = self._прежний_путь(str(job["id"]))
        try:
            if прежний.exists():
                shutil.rmtree(прежний, ignore_errors=True)
            текущий.rename(прежний)
        except OSError as exc:
            log.warning("Прежний результат %s не отложен: %s", job["id"], exc,
                        extra={"job_id": job["id"]})

    def _вернуть_прежний(self, job_id: str, причина: str, *,
                         ожидаемые: list[str] | None = None) -> bool:
        """Возвращает отложенный результат, если повтор не удался.

        Без `ожидаемые` запись идёт как у собственного исхода — только пока
        задание за нами (см. `_write_own`); с ними — условным запросом по
        статусу, для заданий, которые уже никто не считает.
        """
        прежний = self._прежний_путь(job_id)
        if not прежний.is_dir():
            return False
        текущий = Path(self.settings.paths.results) / job_id
        поля: dict[str, Any] = {
            "status": STATUS_COMPLETED, "finished_at": now(), "progress": 1.0,
            "stage": "готово", "error_code": None, "error_message": None,
            "error_hint": None, "instance_id": None, "heartbeat_at": None,
            "result_path": str(текущий)}
        if ожидаемые is None:
            записалось = self._write_own(job_id, **поля)
        else:
            записалось = self.db.update_job_if_status(job_id, ожидаемые, **поля)
        if not записалось:
            return False
        try:
            if текущий.exists():
                shutil.rmtree(текущий, ignore_errors=True)
            прежний.rename(текущий)
        except OSError as exc:
            log.warning("Прежний результат %s не возвращён на место: %s", job_id, exc,
                        extra={"job_id": job_id})
        self.db.add_event(job_id, "rescan_reverted",
                          f"{причина} — оставлен прежний результат")
        self._emit("job.completed", {"id": job_id, "reverted": True})
        return True

    def _write_own(self, job_id: str, **fields: Any) -> bool:
        """Записывает исход задания, только если оно всё ещё за нами.

        Успешное завершение было защищено этим условием, а все три ветки
        отказа писали безусловно — и защита оказывалась половинчатой.
        Сценарий: наш экземпляр застрял дольше отметки жизни, сосед забрал
        задание себе и считает; у нас всплывает ошибка движка и затирает его
        работающее задание отказом. Когда сосед досчитает, его собственная
        защищённая запись не пройдёт, и **готовая расшифровка будет
        выброшена** — пользователь получит «ошибка» вместо результата.

        Заодно снимается вторая беда: `retries` брался из снимка задания,
        сделанного на старте, и перезаписывал увеличение, которое поставил
        перехват. С этим условием такая запись просто не состоится.
        """
        записалось = self.db.update_job_if_status(
            job_id, [STATUS_RUNNING], expected_instance=INSTANCE_ID, **fields)
        if not записалось:
            log.info("Задание %s больше не за нами — исход не записан "
                     "(его перехватил другой экземпляр или оно уже завершено)",
                     job_id, extra={"job_id": job_id})
        return записалось

    def _fail_unexpected(self, job: dict[str, Any], exc: Exception) -> None:
        """Отметить задание неудавшимся после сбоя вне конвейера.

        Пишется тем же защищённым способом, что и обычная неудача: если
        задание за это время перехватил другой экземпляр, наша запись не
        проходит и его работа не портится.
        """
        job_id = str(job.get("id") or "")
        if not job_id:
            return
        try:
            ошибка = classify_exception(exc, engine=str(job.get("engine") or ""),
                                        model=str(job.get("model") or ""))
            merged = self.settings.merged(dict(job.get("params") or {}))
        except Exception:                                    # noqa: BLE001
            # Код у базовой ошибки уже «internal_error» — этого хватает.
            ошибка = ASRHubError(f"Внутренняя ошибка сервера: {type(exc).__name__}")
            merged = dict(self.settings.values)
        try:
            self._handle_failure(job, ошибка, merged)
        except Exception as повторный:                       # noqa: BLE001
            log.error("Задание %s не удалось отметить неудавшимся: %s",
                      job_id, повторный, extra={"job_id": job_id})

    def _handle_failure(self, job: dict[str, Any], error: ASRHubError,
                        merged: dict[str, Any], outdir: Path | None = None) -> None:
        job_id = job["id"]
        with self._lock:
            was_cancelled = job_id in self._cancelled
            self._cancelled.discard(job_id)
        if was_cancelled:
            if not self._write_own(job_id, status=STATUS_CANCELLED, finished_at=now(),
                                   stage="отменено", instance_id=None,
                                   heartbeat_at=None):
                self._discard_unless_taken(outdir, job_id)
                self._вернуть_прежний(job_id, "Повторное распознавание отменено",
                                      ожидаемые=[STATUS_CANCELLED])
                return
            RUNTIME.inc("asrhub_jobs_total", {"status": "cancelled"})
            self._discard_results(outdir)
            self._вернуть_прежний(job_id, "Повторное распознавание отменено",
                                  ожидаемые=[STATUS_CANCELLED])
            return

        # Прервано остановкой сервера — это не отказ задания: оно вернётся в
        # очередь без траты попытки и досчитается после запуска.
        if self._stop.is_set() and isinstance(error, JobCancelled):
            if not self._вернуть_в_очередь_без_попытки(job_id):
                self._discard_unless_taken(outdir, job_id)
                return
            self._discard_results(outdir)
            return

        retries = int(job.get("retries") or 0)
        # Уже в пределах серверного: `Settings.merged` не даёт заданию больше.
        max_retries = max(0, S.integer(merged, "max_retries", 0))

        if error.retryable and retries < max_retries:
            # Ноль — законное значение: «повторять сразу». `or 10.0`
            # превращал его в десять секунд.
            delay = max(0.0, S.num(merged, "retry_backoff_s", 10.0)) * (2 ** retries)
            delay *= 0.75 + random.random() * 0.5      # разброс, чтобы повторы не совпали
            params = dict(job.get("params") or {})
            if isinstance(error, OutOfMemoryError):
                divisor = max(1, int(merged.get("oom_retry_batch_divisor") or 2))
                old_batch = int(params.get("batch_size", merged.get("batch_size", 8)))
                params["batch_size"] = max(1, old_batch // divisor)
                self.db.add_event(
                    job_id, "retry_adjust",
                    f"Размер пакета уменьшен с {old_batch} до {params['batch_size']} "
                    f"из-за нехватки памяти")
            if not self._write_own(
                    job_id, status=STATUS_RETRY, retries=retries + 1,
                    error_code=error.code, error_message=error.message,
                    error_hint=error.hint,
                    stage=f"повтор через {int(delay)} с", progress=0.0,
                    instance_id=None, heartbeat_at=None,
                    queued_at=now() + delay, params=params):
                self._discard_unless_taken(outdir, job_id)
                return
            self.db.add_event(job_id, "retry_scheduled",
                              f"Повтор {retries + 1} из {max_retries} через {int(delay)} с: "
                              f"{error.message}")
            log.warning("Задание %s: %s — повтор через %.0f с", job_id, error.message, delay,
                        extra={"job_id": job_id, "error_code": error.code})
            RUNTIME.inc("asrhub_retries_total")
            self._emit("job.retry", {"id": job_id, "attempt": retries + 1,
                                     "delay_s": round(delay, 1), "error": error.message})
            return

        # Не удался повтор уже готового задания — возвращаем прежний
        # результат, а не превращаем готовую расшифровку в «ошибку».
        if self._вернуть_прежний(job_id, f"Повторное распознавание не удалось: {error.message}"):
            self.db.bump_model_stats(str(job.get("model") or ""),
                                     str(job.get("engine") or ""), ok=False)
            RUNTIME.inc("asrhub_jobs_total", {"status": "failed"})
            RUNTIME.note_error(error.code, error.retryable)
            log.warning("Задание %s: повтор не удался (%s) — оставлен прежний результат",
                        job_id, error.message, extra={"job_id": job_id, "error_code": error.code})
            return
        if not self._write_own(
                job_id, status=STATUS_FAILED, finished_at=now(), progress=0.0,
                stage="ошибка", error_code=error.code, error_message=error.message,
                error_hint=error.hint, instance_id=None, heartbeat_at=None):
            self._discard_unless_taken(outdir, job_id)
            return
        self.db.bump_model_stats(str(job.get("model") or ""), str(job.get("engine") or ""),
                                 ok=False)
        RUNTIME.inc("asrhub_jobs_total", {"status": "failed"})
        RUNTIME.note_error(error.code, error.retryable)
        if error.code == "no_speech":
            RUNTIME.inc("asrhub_no_speech_total")
        self.db.add_event(job_id, "failed", error.message, error.to_dict())
        log.error("Задание %s провалено: %s", job_id, error.message,
                  extra={"job_id": job_id, "error_code": error.code})
        # Подсказка — это и есть причина с лечением. В журнал она не
        # попадала вовсе: там оставался тот же факт, что и в карточке
        # задания, и разбираться приходилось наугад.
        if error.hint:
            for строка in str(error.hint).splitlines():
                if строка.strip():
                    log.error("Задание %s: %s", job_id, строка.strip(),
                              extra={"job_id": job_id, "error_code": error.code})
        self._emit("job.failed", {"id": job_id, "error": error.to_dict()})
        self._discard_results(outdir)
        self._send_webhook(job_id)

    def _discard_unless_taken(self, outdir: Path | None, job_id: str) -> None:
        """Убрать свою неудавшуюся выгрузку — но не чужую работу.

        Все места, откуда сюда приходят, — это провал защищённой записи:
        задание за это время либо отменили, либо удалили, либо его
        перехватил другой экземпляр по устаревшей отметке жизни. В первых
        двух случаях каталог наш и его надо убрать. В третьем каталог
        `results/<id>` уже принадлежит перехватившему — путь-то общий, — и
        `rmtree` сносил бы готовую работу соседа, оставляя его задание
        завершённым со ссылкой на пустоту.
        """
        try:
            строка = self.db.get_job(job_id) or {}
        except Exception as exc:                             # noqa: BLE001
            log.debug("Владелец задания %s не выяснен (%s) — каталог не трогаем",
                      job_id, exc)
            return
        чей = str(строка.get("instance_id") or "")
        статус = str(строка.get("status") or "")
        if чей and чей != INSTANCE_ID and статус == STATUS_RUNNING:
            log.info("Каталог результатов %s оставлен: заданием занят экземпляр «%s»",
                     job_id, чей, extra={"job_id": job_id})
            return
        # Готовое задание — тоже чужая работа, и её здесь сносили. Проверка
        # закрывала только случай «сосед ещё считает», а завершённое
        # задание пишет instance_id=NULL и статус completed: условие выше
        # оказывалось ложным, и rmtree сносил каталог результатов соседа.
        #
        # Случай не выдуманный: экземпляр A залипает дольше STALE_AFTER_S,
        # B возвращает задание в очередь и досчитывает его за сорок минут,
        # A оживает и либо досчитывает сам, либо получает отказ движка — и
        # в обоих случаях стирает файлы B. В базе задание остаётся
        # completed с result_path, указывающим в пустоту: все скачивания
        # отвечают 404 навсегда.
        if статус == STATUS_COMPLETED:
            log.info("Каталог результатов %s оставлен: задание уже завершено",
                     job_id, extra={"job_id": job_id})
            return
        self._discard_results(outdir)

    @staticmethod
    def _discard_results(outdir: Path | None) -> None:
        """Убирает каталог результатов задания, которое не дошло до конца.

        Выгрузка создаёт каталог и пишет файлы до отметки «готово», а отмена
        или тайм-аут срабатывали уже после этого. result_path в базу при этом
        не попадал, поэтому ни удаление задания, ни уборка по сроку хранения
        такой каталог не находили — он оставался на диске навсегда.
        """
        if outdir is None:
            return
        try:
            if outdir.is_dir():
                shutil.rmtree(outdir, ignore_errors=True)
        except OSError as exc:
            log.debug("Каталог результатов %s убрать не удалось: %s", outdir, exc)

    # --- уведомления -----------------------------------------------------

    def _send_webhook(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        if not job or not job.get("webhook_url"):
            return
        if self._stop.is_set():
            # После остановки пул заводить нельзя: `stop` уже обнулил ссылку
            # на него, и новый оказался бы непогашенным — его потоки не
            # демоны и держали бы выход из процесса.
            log.info("Уведомление для %s не отправлено: очередь остановлена",
                     job_id, extra={"job_id": job_id})
            return
        self._webhook_pool().submit(self._webhook_worker, job)

    def _webhook_pool(self) -> Any:
        """Ограниченный пул для доставки уведомлений.

        Создаётся при первой доставке, чтобы не держать потоки на серверах,
        где уведомления не используются.
        """
        with self._lock:
            if self._webhooks is None:
                from concurrent.futures import ThreadPoolExecutor

                size = max(1, int(self.settings.get("webhook_workers") or 4))
                self._webhooks = ThreadPoolExecutor(
                    max_workers=size, thread_name_prefix="asrhub-webhook")
                log.debug("Пул доставки уведомлений: %d потоков", size)
            return self._webhooks

    def _webhook_worker(self, job: dict[str, Any]) -> None:
        import json as json_mod

        # Задание, принятое по схеме phone_asr, и отвечать должно её телом:
        # приёмник на той стороне разбирает результат по своей схеме и на
        # наши поля не рассчитан. Признак — блок _phone в параметрах задания,
        # который кладёт маршрут /process-call.
        call = (job.get("params") or {}).get("_phone")
        if call:
            body = self._phone_callback_body(job, call)
            payload = json_mod.dumps(body, ensure_ascii=False).encode("utf-8")
            self._deliver_webhook(job, payload)
            return

        body: dict[str, Any] = {
            "id": job["id"], "status": job["status"], "filename": job.get("filename"),
            "model": job.get("model"), "rtf": job.get("rtf"),
            "duration_s": job.get("media_duration_s"),
            "words": job.get("words_count"),
            "error": job.get("error_message"),
        }
        # Огибающая в уведомлении — совместимость с приёмниками phone_asr,
        # которые ждут её полем `waveforms`. По умолчанию выключено: тело
        # уведомления задумано коротким, а повторов доставки до пяти.
        if self.settings.get("webhook_waveform") and job.get("waveform"):
            from .pipeline.waveform import to_phone_asr

            body["waveforms"] = to_phone_asr(job["waveform"])
        payload = json_mod.dumps(body, ensure_ascii=False).encode("utf-8")

        self._deliver_webhook(job, payload)

    def _phone_callback_body(self, job: dict[str, Any],
                             call: dict[str, Any]) -> dict[str, Any]:
        """Тело обратного вызова в схеме phone_asr."""
        from .phone_compat import PhoneRequest, callback_body

        request = PhoneRequest(
            call_id=str(call.get("call_id") or ""), files=[],
            base_url="", part=int(call.get("part") or 1),
            total_parts=int(call.get("total_parts") or 1),
            swap_sides=bool(call.get("swap_sides")))
        segments = self.db.get_segments(job["id"])
        body = callback_body(request, job, segments)
        # base_path храним с приёма: имена исходных файлов после обработки
        # уже не восстановить, а приёмник по ним и сопоставляет запись.
        body["base_path"] = str(call.get("base_path") or body["base_path"])
        return body

    def _deliver_webhook(self, job: dict[str, Any], payload: bytes) -> None:
        """Доставка с повторами. Одна на оба вида тела."""
        import hashlib
        import hmac
        import urllib.error
        import urllib.request

        secret = str(self.settings.get("webhook_secret") or "").encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8",
                   "User-Agent": "ASRHub/3.0"}
        if secret:
            headers["X-ASRHub-Signature"] = hmac.new(secret, payload, hashlib.sha256).hexdigest()

        # Адрес кодируем перед отправкой: urllib требует уже закодованный
        # путь и бросает «'ascii' codec can't encode characters», не дойдя до
        # сети. Кириллица в адресе обратного вызова — обычное дело (имя
        # проекта в пути), и без этого доставка молча падала пять раз подряд
        # и помечалась «failed», а вызывающая сторона ничего не получала.
        # Преобразование идемпотентно: уже закодованный адрес не меняется.
        from .phone_compat import encode_url

        target = encode_url(str(job["webhook_url"]))
        # Адрес проверяется ещё раз — перед отправкой: имя, которое при приёме
        # указывало наружу, к этому времени могло начать указывать внутрь.
        allow_internal = bool(self.settings.get("webhook_allow_internal", False))
        try:
            check_outbound_url(target, allow_internal)
        except ConfigError as exc:
            log.warning("Уведомление для %s не отправлено: %s", job["id"], exc.message,
                        extra={"job_id": job["id"]})
            self.db.update_job(job["id"], webhook_status="blocked")
            RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})
            return
        opener = открыватель_наружу(allow_internal)

        attempts = 5
        for attempt in range(attempts):
            if self._stop.is_set():
                log.info("Доставка уведомления для %s не начата: очередь "
                         "остановлена", job["id"], extra={"job_id": job["id"]})
                return
            try:
                request = urllib.request.Request(target, data=payload,
                                                 headers=headers, method="POST")
                with opener.open(request, timeout=15) as response:
                    if 200 <= response.status < 300:
                        self.db.update_job(job["id"], webhook_status=f"ok:{response.status}")
                        RUNTIME.inc("asrhub_webhooks_total", {"result": "ok"})
                        return
            except ConfigError as exc:
                # Перенаправление внутрь сети — повторять незачем.
                log.warning("Уведомление для %s не отправлено: %s", job["id"], exc.message,
                            extra={"job_id": job["id"]})
                self.db.update_job(job["id"], webhook_status="blocked")
                RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})
                return
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log.info("Уведомление для %s не доставлено (попытка %d из %d): %s",
                         job["id"], attempt + 1, attempts, exc)
            if attempt < attempts - 1:      # после последней попытки ждать незачем
                # Ждём на событии остановки, а не в sleep: пауза между
                # попытками доходит до минуты, а всего попыток пять. Пул
                # доставки гасится без ожидания, и уже запущенная попытка
                # продолжала жить своей жизнью — процесс не завершался ещё
                # полторы минуты после «Очередь остановлена», при недоступном
                # приёмнике дольше. Остановка — это причина бросить доставку,
                # а не повод её дождаться.
                if self._stop.wait(timeout=min(60, 2 ** attempt)):
                    log.info("Доставка уведомления для %s прервана остановкой",
                             job["id"], extra={"job_id": job["id"]})
                    return
        self.db.update_job(job["id"], webhook_status="failed")
        RUNTIME.inc("asrhub_webhooks_total", {"result": "failed"})

    def _owner_of(self, job_id: str) -> str:
        """Владелец задания для адресной рассылки событий.

        Держим небольшой словарь вместо запроса к базе: события прогресса
        идут по нескольку раз в секунду на каждое задание, и поход в базу
        на каждое сделал бы рассылку дороже самой работы.
        """
        with self._lock:
            cached = self._owners.get(job_id)
        if cached is not None:
            return cached
        job = self.db.get_job(job_id)
        owner = str((job or {}).get("owner") or "")
        with self._lock:
            self._owners[job_id] = owner
            # Словарь не должен расти бесконечно на долгоживущем сервере.
            if len(self._owners) > 4096:
                for key in list(self._owners)[:2048]:
                    self._owners.pop(key, None)
        return owner

    def _emit(self, kind: str, data: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        # Событие о задании адресное: имя файла, текст ошибки и сам факт
        # работы — сведения владельца. Подписчику, который не владелец и не
        # администратор, оно не уходит. Поле служебное и до клиента не
        # доезжает — рассылка снимает его перед отправкой.
        if "_owner" not in data and isinstance(data.get("id"), str):
            data = {**data, "_owner": self._owner_of(data["id"])}
        try:
            self.on_event(kind, data)
        except Exception as exc:
            log.debug("Обработчик события «%s» дал сбой: %s", kind, exc)

    # --- фоновое обслуживание ---------------------------------------------

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
                    from .maintenance import retention_days  # noqa: PLC0415

                    retention = retention_days(self.settings)
                    removed = self.db.cleanup(
                        results_days=retention,
                        audit_days=S.integer(self.settings, "audit_days", 365))
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

    # --- состояние --------------------------------------------------------

    def status(self) -> dict[str, Any]:
        counts = {status: self.db.count_jobs(status=status)
                  for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_RETRY,
                                 STATUS_PAUSED, STATUS_COMPLETED, STATUS_FAILED,
                                 STATUS_CANCELLED)}
        queued = self.db.list_jobs(status=[STATUS_QUEUED, STATUS_RETRY], limit=500,
                                   light=True)
        pending_audio = sum(float(j.get("media_duration_s") or 0) for j in queued)
        stats = self.db.model_stats()
        rtf_values = [s["rtf_avg"] for s in stats if s.get("rtf_avg")]
        avg_rtf = sum(rtf_values) / len(rtf_values) if rtf_values else 0.25
        workers = int(self.settings.get("max_concurrent_jobs") or 2)
        eta = (pending_audio * avg_rtf / max(1, workers)) if pending_audio else 0.0
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
            "paused": self._paused,
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
