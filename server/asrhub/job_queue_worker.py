"""Рабочий цикл очереди: воркер берёт задание, считает его (в потоке или
в дочернем процессе), пишет итог, переносит прежний результат при повторе и
разбирает отказы — повтор, резервная модель, итог без расшифровки.

Вынесено из `job_queue.py` примесью к `JobQueue`. Процессор
(`process_job`) и замер свободной памяти (`свободная_память`) зовутся
отсюда: проверки, подменяющие их, подменяют их в этом модуле;
`job_queue` по-прежнему отдаёт их имена.
"""
from __future__ import annotations

import random
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import settings_access as S
from .catalog import get_model
from .db_util import now
from .errors import (
    ASRHubError,
    JobCancelled,
    OutOfMemoryError,
    classify_exception,
)
from .instance import INSTANCE_ID
from .job_status import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_NO_SPEECH,
    STATUS_QUEUED,
    STATUS_RETRY,
    STATUS_RUNNING,
    STATUS_SHORT,
    WorkerState,
)
from .logging_setup import get_logger
from .monitoring.collector import JOB_DURATION_BUCKETS, MEDIA_DURATION_BUCKETS, RUNTIME
from .pipeline import audio_profile
from .pipeline import metrics as metrics_mod
from .processor import cleanup_workdir, process_job, safe_workdir, settings_digest

if TYPE_CHECKING:
    from .db import Database
    from .engines import EngineRegistry

log = get_logger("queue")


def _отпечаток_каталога(путь: Path) -> tuple[int, int] | None:
    """Номер узла и время изменения каталога — чтобы узнать «тот же самый»."""
    try:
        сведения = путь.stat()
    except OSError:
        return None
    return (сведения.st_ino, сведения.st_mtime_ns) if путь.is_dir() else None


def движок_задания(settings: dict[str, Any]) -> str:
    """Движок, которым задание будет распознано, — для колонки таблицы.

    В параметрах движок обычно «auto»: его выбирает модель. В колонку так
    и писалось «auto», и разрез аналитики «по движкам» показывал на весь
    архив один столбец «auto», а статистика моделей — движок «auto» у
    каждой.
    """
    движок = str(settings.get("engine") or "").strip()
    if движок and движок != "auto":
        return движок
    spec = get_model(str(settings.get("model") or ""))
    return spec.engine if spec is not None else ""


#: Не чаще, чем раз в столько секунд, доклад прогресса пишется в базу (смена
#: стадии пишется всегда). Запись на каждый кусок звука — в режиме журнала
#: отката на сетевом хранилище это fsync на каждый кусок.
ПРОГРЕСС_В_БАЗУ_С = 1.0

#: Код «отказа» процессора → итог без расшифровки. Процессор сообщает о
#: молчащей и о короткой записи исключением — это единственный способ
#: прервать обработку на полпути, — а очередь превращает его в итог.
ИТОГ_БЕЗ_РАСШИФРОВКИ: dict[str, str] = {
    "no_speech": STATUS_NO_SPEECH,
    "audio_too_short": STATUS_SHORT,
}


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


def _для_владельца(error: ASRHubError) -> dict[str, Any]:
    """Ошибка в том виде, в каком её видит владелец задания: без сырой причины.

    `to_dict()` кладёт в `cause` текст исключения как есть — с полными путями
    к весам модели и к каталогу загрузок. Он уходил в событие задания и по
    WebSocket владельцу, в обход `без_путей()`, который введён именно против
    показа раскладки хранилища. Причина целиком — в журнале сервера, с
    трассировкой (см. `_трассировка`).
    """
    данные = error.to_dict()
    данные.pop("cause", None)
    return данные


def _трассировка(error: ASRHubError) -> Any:
    """Что передать `exc_info`: исходное исключение с трассировкой, если оно есть."""
    причина = getattr(error, "cause", None)
    if isinstance(причина, BaseException):
        return (type(причина), причина, причина.__traceback__)
    if error.__traceback__ is not None:
        return (type(error), error, error.__traceback__)
    return None





def свободная_память(устройство: str) -> int | None:
    """Свободная память видеокарты `cuda:N` в байтах; None — узнать нечем.

    Сначала torch (`mem_get_info` — то, что видит процесс), затем
    nvidia-smi. Ответ nvidia-smi держится две секунды: воркеры стартуют
    пачкой, и десять вызовов программы подряд ни к чему.
    """
    номер = устройство.split(":", 1)[1] if ":" in устройство else "0"
    if not номер.isdigit():
        return None
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            return int(torch.cuda.mem_get_info(int(номер))[0])
    except Exception:                                        # noqa: BLE001
        pass
    сейчас = time.monotonic()
    if сейчас - _NVIDIA_SMI["при"] > 2.0:
        _NVIDIA_SMI["при"], _NVIDIA_SMI["карты"] = сейчас, {}
        программа = shutil.which("nvidia-smi")
        if программа:
            import subprocess  # noqa: PLC0415

            try:
                вывод = subprocess.run(
                    [программа, "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=5, check=False).stdout
                for строка in вывод.splitlines():
                    части = [ч.strip() for ч in строка.split(",")]
                    if len(части) == 2 and части[0].isdigit() and части[1].isdigit():
                        _NVIDIA_SMI["карты"][части[0]] = int(части[1]) * 1024 * 1024
            except (OSError, subprocess.SubprocessError):
                pass
    return _NVIDIA_SMI["карты"].get(номер)


_NVIDIA_SMI: dict[str, Any] = {"при": 0.0, "карты": {}}


class РабочийЦиклОчереди:
    """Примесь к `JobQueue`: воркеры и исполнение задания."""

    if TYPE_CHECKING:
        db: Database
        settings: Any
        registry: EngineRegistry
        content_index: Any
        llm_worker: Any
        on_event: Callable[[str, dict[str, Any]], None] | None
        _lock: threading.RLock
        _stop: threading.Event
        _wake: Any
        _states: dict[int, WorkerState]
        _cancelled: set[str]
        _retiring: set[int]
        _running: dict[str, float]
        _concurrency: dict[str, int]
        _идут: set[str]
        _потоки: dict[int, threading.Thread]
        _дочерние: dict[int, Any]

        def _emit(self, kind: str, data: dict[str, Any]) -> None: ...
        def _send_webhook(self, job_id: str) -> None: ...
        def _уведомление_ждёт(self, job: dict[str, Any], *,
                              кроме_отката: bool = False) -> dict[str, Any]: ...
        def _next_job(self, worker_index: int) -> dict[str, Any] | None: ...
        def _release_slot(self, job_id: str, model: str) -> None: ...
        def _вернуть_в_очередь_без_попытки(self, job_id: str) -> bool: ...
        def _взять_у_близнеца(self, job: dict[str, Any], merged: dict[str, Any]) -> bool: ...
        def _отпечаток_весов(self, spec: Any) -> str: ...

    def _изоляция_процессом(self) -> bool:
        try:
            return str(self.settings.get("worker_isolation") or "thread") == "process"
        except Exception:                                    # noqa: BLE001
            return False

    def _дочерний(self, index: int) -> Any:
        """Процесс распознавания воркера — в режиме `worker_isolation: process`."""
        if not self._изоляция_процессом():
            return None
        дочерние = self.__dict__.setdefault("_дочерние", {})
        with self._lock:
            дочерний = дочерние.get(index)
            if дочерний is None:
                from .worker_process import ДочернийВоркер  # noqa: PLC0415

                дочерний = ДочернийВоркер(
                    index, размер_кеша=max(1, int(self.settings.get("model_cache_size") or 1)),
                    простой_с=int(self.settings.get("model_idle_unload_s") or 900))
                дочерние[index] = дочерний
            return дочерний

    def _карта_воркера(self, index: int, merged: dict[str, Any]) -> str:
        """Карта из пула `gpu_devices` для воркера; пусто — пула нет или карта задана."""
        try:
            сырые = self.settings.get("gpu_devices") or []
        except Exception:                                    # noqa: BLE001
            return ""
        if isinstance(сырые, str):
            сырые = [ч.strip() for ч in сырые.replace(";", ",").split(",")]
        карты = [str(к).strip() for к in сырые if str(к).strip()]
        if not карты:
            return ""
        просили = str(merged.get("device") or "auto").strip().lower()
        # Явная карта («cuda:1») и процессор — как попросили.
        if просили not in ("", "auto", "cuda", "gpu", "rocm"):
            return ""
        своя = карты[int(index) % len(карты)]
        try:
            по_памяти = str(self.settings.get("gpu_pick") or "worker") == "free_memory"
        except Exception:                                    # noqa: BLE001
            по_памяти = False
        if not по_памяти or len(карты) < 2:
            return своя
        # По свободной памяти: карта, где её больше всего. Своя карта воркера
        # остаётся, пока на ней свободно не меньше 90 % от лучшей: прыжок на
        # другую карту стоит загрузки модели заново.
        свободно = {к: свободная_память(к) for к in карты}
        известно = {к: м for к, м in свободно.items() if м is not None}
        if not известно:
            return своя
        лучшая = max(известно, key=lambda к: известно[к])
        if своя in известно and известно[своя] >= 0.9 * известно[лучшая]:
            return своя
        return лучшая

    def _заменён(self, index: int) -> bool:
        """Поток воркера заменён сторожем зависших — ему пора уйти."""
        return self._потоки.get(index) not in (None, threading.current_thread())

    def _worker_loop(self, index: int) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._заменён(index):
                    log.warning("Воркер %d: поток, снятый сторожем, вернулся из движка "
                                "и завершается", index)
                    return
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
                if self._заменён(index):
                    # Задание и слот уже сняты сторожем зависших, слот отдан
                    # новому потоку — второй раз освобождать нечего.
                    return  # noqa: B012
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
        # Задание переведено на резервную модель (см. `_на_резервную`).
        # Служебных ключей каталога `merged` не пропускает, поэтому причину
        # несём в конвейер отдельно: она уходит в предупреждения результата.
        if params.get("_fallback_from"):
            merged["_fallback_from"] = str(params["_fallback_from"])
            merged["_fallback_reason"] = str(params.get("_fallback_reason") or "")

        # Пул видеокарт: задание без явного номера карты считает воркер на
        # «своей» карте (номер воркера по кругу), и у каждой карты свой
        # экземпляр модели в кеше — вторая карта больше не простаивает.
        карта = self._карта_воркера(state.index, merged)
        if карта:
            merged["device"] = карта
        paths = self.settings.paths
        workdir = safe_workdir(self._каталог_временных(), self._имя_рабочего(job_id))
        outdir = paths.results / job_id
        timeout = max(0, S.integer(merged, "job_timeout_s", 0))
        started = time.time()
        state.срок_с, state.доклад = float(timeout), time.monotonic()
        записано: dict[str, Any] = {"при": 0.0, "стадия": None}

        def progress(value: float, stage: str) -> None:
            # Полоса только вперёд: этапы конвейера и движки докладывают
            # долю каждый по-своему, и откат с 0,82 на 0,80 выглядел как
            # «задание пошло назад».
            value = max(float(value), state.progress)
            state.progress = value
            state.stage = stage
            state.доклад = time.monotonic()
            # В базу — не чаще раза в секунду (смена стадии — всегда): в
            # режиме журнала отката на сетевом хранилище запись на каждый
            # кусок звука — это fsync на каждый кусок. Отметку жизни ставит
            # и свой поток (см. `_отметить_живые`), так что пропуск записи
            # её не задерживает. Рассылка по WebSocket идёт как прежде.
            сейчас = time.monotonic()
            if stage == записано["стадия"] and сейчас - записано["при"] < ПРОГРЕСС_В_БАЗУ_С:
                self._emit("job.progress", {"id": job_id, "progress": round(value, 4),
                                            "stage": stage})
                return
            записано["при"], записано["стадия"] = сейчас, stage
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

    def _каталог_временных(self) -> Path:
        """Где заводить рабочие каталоги заданий: `temp_dir` или `<данные>/tmp`.

        Рабочий каталог всегда был `<данные>/tmp/<id>`, хотя параметр
        `temp_dir` обещает «где создавать промежуточные файлы предобработки
        — укажите быстрый локальный диск», а doctor и самопроверка проверяют
        именно его. При каталоге данных на сетевом диске многогигабайтные
        подготовленные записи писались на сетевой диск.
        """
        задан = str(self.settings.get("temp_dir") or "").strip() \
            if hasattr(self.settings, "get") else ""
        return Path(задан) if задан else Path(self.settings.paths.tmp)

    @staticmethod
    def _имя_рабочего(job_id: str) -> str:
        """Имя рабочего каталога задания — со своим экземпляром.

        Путь `tmp/<id>` общий у всех серверов над общим каталогом данных, и
        «оживший» экземпляр (замер дольше отметки жизни) сносил в `finally`
        рабочий каталог задания, которое за это время взял и считал сосед.
        """
        from .phone_compat import safe_path_key  # noqa: PLC0415

        return f"{job_id}.{safe_path_key(INSTANCE_ID)[:40] or 'экземпляр'}"

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
        # Какой прежний результат отложен перед этим прогоном (повтор готового
        # задания): убрать в конце можно только его — см. ниже.
        отложенный = _отпечаток_каталога(self._прежний_путь(job_id))
        try:
            if self._взять_у_близнеца(job, merged):
                cleanup_workdir(workdir)
                return
        except Exception as exc:                             # noqa: BLE001
            log.warning("Результат близнеца для %s не взят: %s", job_id, exc,
                        extra={"job_id": job_id})
        аргументы: dict[str, Any] = {
            "workdir": workdir, "outdir": outdir,
            "basename": Path(job.get("filename") or job_id).stem,
            "filename": str(job.get("filename") or ""),
            "progress": progress, "cancelled": cancelled,
            "deadline": (started + timeout) if timeout else None, "timeout_s": timeout,
            "measure_memory": True, "reset_memory": одно}
        дочерний = self._дочерний(state.index)
        try:
            if дочерний is not None:
                outcome = дочерний.выполнить(Path(job["file_path"]), merged, **аргументы)
            else:
                outcome = process_job(Path(job["file_path"]), merged, self.registry,
                                      **аргументы)
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
        # Ожидание занятой модели — это ожидание, а не обработка: при двух
        # воркерах на одной модели второй стоит за замком движка. Раньше оно
        # входило в `processing_time_s` и завышало «самые медленные» и
        # длительность заданий, а «задержка очереди» выходила меньше.
        ждали_модель = max(0.0, min(elapsed, float(outcome.stats.get("engine_wait_s") or 0.0)))
        elapsed -= ждали_модель
        job["queue_time_s"] = float(job.get("queue_time_s") or 0.0) + ждали_модель
        accuracy = outcome.stats.get("accuracy") or {}
        # Здоровье распознавания — тут же, по тем же сегментам: галлюцинации
        # Whisper, невозможный темп, повторы, известные фразы, разметка
        # говорящих. Считается всегда, а не только при включённом разборе
        # содержания: это признак самого распознавания, и сбой внутри не
        # должен трогать результат. Пишется вместе с «готово».
        признаки: dict[str, Any] = {}
        try:
            from . import quality  # noqa: PLC0415

            признаки = quality.for_job(quality.assess(
                outcome.segments,
                expected_speakers=int(
                    self.settings.get("quality_expected_speakers") or 0)
                if self.settings is not None else 0))
        except Exception as exc:                             # noqa: BLE001
            log.warning("Признаки качества для %s не посчитаны: %s", job_id, exc,
                        extra={"job_id": job_id})
        модель_задания, движок_задания = str(job.get("model")), str(job.get("engine"))
        # Реплики и отметка «готово» — одной транзакцией и одним условием
        # (см. `Database.finish_job`). Прежде реплики писались первыми, а при
        # провале отметки стирались без проверки, чьё задание: «оживший»
        # экземпляр так уничтожал реплики, которые за это время записал
        # досчитавший задание сосед.
        #
        # Только из состояния «выполняется». Отмена аккуратно сверяет статус
        # перед записью, а обратное направление было незащищено: отмена,
        # пришедшая между последней проверкой в конвейере и этой записью,
        # затиралась, и пользователь, которому уже ответили «отменено»,
        # получал задание в состоянии «готово».
        finished = self.db.finish_job(
            job_id, outcome.segments,
            expected=[STATUS_RUNNING],
            # Задание могли отобрать, пока мы считали: если отметка жизни
            # устарела, его уже перезапустил сосед. Своё имя в условии
            # означает «пишем, только если оно всё ещё за нами».
            expected_instance=INSTANCE_ID,
            status=STATUS_COMPLETED, finished_at=now(), progress=1.0, stage="готово",
            instance_id=None, heartbeat_at=None,
            # Уведомление ждёт доставки с той же записью, что и «готово»:
            # процесс, упавший сразу после неё, иначе не оставлял следа, что
            # уведомление должно было уйти (см. `_дослать_уведомления`).
            **self._уведомление_ждёт(job),
            # Ошибка прошлой попытки уходит вместе с неудачей. Повтор и
            # переход на резервную модель пишут её в задание, чтобы в
            # очереди было видно, почему оно ждёт, — а удачное завершение её
            # не стирало: готовое задание показывало красную карточку
            # «engine_error», а `asrctl status` — «Ошибка». Причина остаётся
            # в событиях задания.
            error_code=None, error_message=None, error_hint=None,
            text=outcome.text,
            result_path=str(outdir),
            segments_count=len(outcome.segments),
            words_count=int(outcome.stats.get("words") or 0),
            chars_count=int(outcome.stats.get("chars") or 0),
            speakers_count=len(outcome.speakers),
            avg_confidence=outcome.stats.get("avg_confidence"),
            rtf=outcome.rtf,
            processing_time_s=round(elapsed, 3),
            queue_time_s=round(float(job.get("queue_time_s") or 0.0), 3),
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
            **признаки,
            # Показатели и события — той же транзакцией (см. `finish_job`).
            metrics=[(имя, float(значение or 0.0), модель_задания, движок_задания)
                     for имя, значение in (
                         ("rtf", outcome.rtf), ("processing_time_s", elapsed),
                         ("queue_time_s", float(job.get("queue_time_s") or 0.0)),
                         ("confidence", outcome.stats.get("avg_confidence") or 0.0))],
            events=[*(("warning", str(w)) for w in outcome.warnings),
                    ("completed", f"Готово за {elapsed:.1f} с, RTF {outcome.rtf:.3f}")],
        )
        if not finished:
            # Задание успели отменить или удалить, пока оно считалось, или
            # его отобрал сосед. Записывать результат некуда — убираем свои
            # файлы выгрузки (чужие не трогаем, см. `_discard_unless_taken`).
            # Реплики не писались вовсе: отметка и они — одна транзакция.
            log.info("Задание %s завершилось, но его состояние уже изменено — "
                     "результат отброшен", job_id, extra={"job_id": job_id})
            self._discard_unless_taken(outdir, job_id)
            with self._lock:
                self._cancelled.discard(job_id)
            # Поздняя отмена повтора готового задания: прежний результат —
            # на место. Без этого задание оставалось «отменено» без
            # расшифровки, а она лежала в `results/<id>.prev`.
            self._вернуть_прежний(job_id, "Повторное распознавание отменено",
                                  ожидаемые=[STATUS_CANCELLED])
            return
        with self._lock:
            self._cancelled.discard(job_id)

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

        # Статистика, показатели и события — после «готово», и сбой любого
        # (занятая база — обычное дело на общем архиве) не должен уводить
        # готовое задание в обработчик отказа: тот пытался записать «ошибка»
        # поверх «готово», не мог, и до уведомления дело не доходило.
        try:
            self.db.bump_model_stats(
                str(job.get("model") or ""), str(job.get("engine") or ""), ok=True,
                audio_s=float(job.get("media_duration_s") or 0.0),
                processing_s=elapsed, words=int(outcome.stats.get("words") or 0),
                rtf=outcome.rtf, confidence=outcome.stats.get("avg_confidence"),
                wer=accuracy.get("wer"))
        except Exception as exc:                             # noqa: BLE001
            log.warning("Задание %s готово, но статистика модели не записана: %s",
                        job_id, exc, extra={"job_id": job_id})

        log.info("Задание %s: готово за %.1f с, RTF %.3f", job_id, elapsed, outcome.rtf,
                 extra={"job_id": job_id, "model": job.get("model")})
        # Текст разговора в журнал — только с явного разрешения
        # администратора (параметр сервера `log_transcript_text`, задание
        # его не переопределяет). Каталог обещал такой переключатель для
        # отладки, а журнал текста не получал ни при каком значении.
        if merged.get("log_transcript_text") and outcome.text:
            log.info("Задание %s: текст расшифровки: %s", job_id, outcome.text,
                     extra={"job_id": job_id})
        # Дальше — только уведомление, и оно тоже не должно ронять готовое
        # задание. Событие «готово» записано вместе с отметкой (`finish_job`).
        self._emit("job.completed", {"id": job_id, "rtf": outcome.rtf,
                                     "duration_s": elapsed})
        self._send_webhook(job_id)

        # Повтор удался — отложенный прежний результат больше не нужен. Но
        # только тот, что отложен перед ЭТИМ прогоном. Пока после отметки
        # «готово» шли разбор содержания, уведомление и прочее, человек мог
        # уже нажать «Распознать заново» — и новый повтор отложил на то же
        # место только что готовый результат. Прежде он удалялся здесь же, и
        # неудачный новый повтор откатывать было не к чему: расшифровка
        # пропадала целиком.
        прежний = self._прежний_путь(job_id)
        if отложенный is not None and _отпечаток_каталога(прежний) == отложенный:
            shutil.rmtree(прежний, ignore_errors=True)
            self._забыть_прежние_параметры(job_id)

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

    #: Приставка ключа, под которым на время повтора лежат прежние параметры.
    ПРЕЖНИЕ_ПАРАМЕТРЫ = "rescan_prev_params:"

    def _отложить_прежний(self, job: dict[str, Any]) -> None:
        """Откладывает каталог готового результата на время повтора.

        Вместе с ним — прежние параметры задания. Повтор кладёт новые
        параметры сразу, и при откате текст и модель в карточке были
        прежними, а в параметрах стояла новая модель, которой задание так
        и не посчитано: разбор «какой моделью это распознано» врал.
        """
        if job.get("status") != STATUS_COMPLETED:
            return
        текущий = Path(self.settings.paths.results) / str(job["id"])
        if not текущий.is_dir():
            return
        try:
            # Модель и движок — вместе с параметрами: повтор другой моделью
            # меняет и колонки задания, и откат обязан вернуть их тоже.
            self.db.set_kv(f"{self.ПРЕЖНИЕ_ПАРАМЕТРЫ}{job['id']}",
                           {"params": job.get("params") or {},
                            "model": job.get("model"), "engine": job.get("engine")})
        except Exception as exc:                            # noqa: BLE001
            log.debug("Прежние параметры %s не отложены: %s", job["id"], exc)
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
        отложено = self.db.get_kv(f"{self.ПРЕЖНИЕ_ПАРАМЕТРЫ}{job_id}")
        if isinstance(отложено, dict) and isinstance(отложено.get("params"), dict):
            поля["params"] = отложено["params"]
            # Отложенное прежней версией — без колонок: берём их из параметров.
            модель = отложено.get("model") or отложено["params"].get("model")
            if модель:
                поля["model"] = str(модель)
                поля["engine"] = str(отложено.get("engine") or движок_задания(
                    {**отложено["params"], "model": модель}))
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
        self._забыть_прежние_параметры(job_id)
        self.db.add_event(job_id, "rescan_reverted",
                          f"{причина} — оставлен прежний результат")
        # Причина — в самом событии: интерфейс показывал на откат зелёное
        # «Задание готово», и массовый повтор выглядел пачкой успехов.
        self._emit("job.completed", {"id": job_id, "reverted": True,
                                     "reason": причина})
        return True

    def _забыть_прежние_параметры(self, job_id: str) -> None:
        try:
            self.db.execute("DELETE FROM kv WHERE key=?",
                            (f"{self.ПРЕЖНИЕ_ПАРАМЕТРЫ}{job_id}",))
        except Exception as exc:                            # noqa: BLE001
            log.debug("Прежние параметры %s не убраны: %s", job_id, exc)

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

    #: Отказы, при которых задание переходит на резервную модель: беда в
    #: самой модели или в том, что ей нужно (пакет движка, веса, память,
    #: видеокарта, лицензия), — а не в записи, не в очереди и не в отмене.
    РЕЗЕРВ_ПРИ = frozenset({
        "dependency_missing", "binary_missing", "out_of_memory", "model_not_found",
        "model_not_downloaded", "model_load_error", "gated_model", "engine_error",
        "hardware_error",
    })

    def _на_резервную(self, job: dict[str, Any], error: ASRHubError,
                      merged: dict[str, Any], outdir: Path | None = None) -> bool:
        """Ставит задание в очередь заново — с резервной моделью.

        Параметр `model_fallback` обещал «превратить отказ в замедление
        вместо потери задания», а очередь его не читала: задание с
        неустановленным движком, нескачанными весами или нехваткой памяти
        просто падало. Теперь окончательный отказ модели — повторы, если они
        положены, уже исчерпаны — отправляет задание на резервную модель со
        свежим счётчиком повторов. Причина остаётся событием задания и
        предупреждением в результате.

        Не переключаем: без резервной модели; когда резервная — та же самая
        (так и петли нет: у задания, уже переведённого на резервную, она
        совпадает с основной); при отказе не из-за модели; у повтора готового
        задания — там при неудаче возвращается прежний результат, а подменять
        модель, выбранную для повтора, нельзя.

        Возвращает True, когда исход задания решён здесь.
        """
        резервная = str(merged.get("model_fallback") or "").strip()
        основная = str(merged.get("model") or job.get("model") or "")
        job_id = str(job.get("id") or "")
        if (not резервная or резервная == основная or error.code not in self.РЕЗЕРВ_ПРИ
                or not job_id or self._прежний_путь(job_id).is_dir()):
            return False
        spec = get_model(резервная)
        if spec is None:
            log.warning("Задание %s: резервной модели «%s» нет в каталоге — "
                        "переключения не будет", job_id, резервная,
                        extra={"job_id": job_id})
            return False
        params = dict(job.get("params") or {})
        params.update(model=spec.id, engine="auto",
                      _fallback_from=основная, _fallback_reason=error.message)
        params.pop("_hash", None)
        # Отпечаток — уже для резервной модели: иначе кеш отдавал бы её
        # расшифровку новой загрузке с основной моделью.
        params["_hash"] = settings_digest(params, weights=self._отпечаток_весов(spec))
        if not self._write_own(
                job_id, status=STATUS_QUEUED, retries=0, model=spec.id,
                engine=spec.engine, params=params, error_code=error.code,
                error_message=error.message, error_hint=error.hint,
                stage=f"резервная модель {spec.id}", progress=0.0,
                instance_id=None, heartbeat_at=None, started_at=None, queued_at=now()):
            self._discard_unless_taken(outdir, job_id)
            return True
        self._discard_results(outdir)
        # Отказ основной модели — в её статистику: иначе раздел моделей
        # показывал бы её безупречной, а все её неудачи доставались бы
        # резервной как успехи.
        self.db.bump_model_stats(основная, str(job.get("engine") or ""), ok=False)
        RUNTIME.note_error(error.code, error.retryable)
        сообщение = (f"Модель {основная} не справилась ({error.message}) — задание "
                     f"поставлено в очередь с резервной моделью {spec.id}")
        self.db.add_event(job_id, "model_fallback", сообщение, _для_владельца(error))
        log.warning("Задание %s: %s", job_id, сообщение,
                    extra={"job_id": job_id, "error_code": error.code},
                    exc_info=_трассировка(error))
        self._emit("job.fallback", {"id": job_id, "from": основная, "to": spec.id,
                                    "error": _для_владельца(error)})
        self._wake.set()
        return True

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
        except Exception:                                    # noqa: BLE001
            # Код у базовой ошибки уже «internal_error» — этого хватает.
            ошибка = ASRHubError(f"Внутренняя ошибка сервера: {type(exc).__name__}",
                                 cause=exc)
        # Параметры — отдельно от разбора ошибки. Стояли в одном `try`, и
        # задание с параметрами, которые новая версия уже не принимает
        # («Ширина луча: максимум 20»), получало вместо этой понятной причины
        # «Внутренняя ошибка сервера: ConfigError»: `merged` падал второй раз
        # на тех же параметрах и затирал уже разобранную ошибку.
        try:
            merged = self.settings.merged(dict(job.get("params") or {}))
        except Exception:                                    # noqa: BLE001
            merged = dict(getattr(self.settings, "values", {}) or {})
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

        # Молчащая или короткая запись — итог, а не отказ: ни повторов, ни
        # резервной модели (речь ищет детектор, а не модель, и длительность
        # от модели не зависит), ни строки в ошибках.
        итог = ИТОГ_БЕЗ_РАСШИФРОВКИ.get(error.code)
        if итог:
            # Повтор готового задания с другими настройками не стирает
            # прежнюю расшифровку — как и при настоящем отказе.
            if self._вернуть_прежний(job_id, f"Повторное распознавание: {error.message}"):
                return
            self._без_расшифровки(job, error, итог, outdir)
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

        # Повторы не помогли (или отказ из тех, что повтором не лечатся), а
        # у задания есть резервная модель — отказ становится замедлением.
        if self._на_резервную(job, error, merged, outdir):
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
                error_hint=error.hint, instance_id=None, heartbeat_at=None,
                **self._уведомление_ждёт(job)):
            self._discard_unless_taken(outdir, job_id)
            return
        self.db.bump_model_stats(str(job.get("model") or ""), str(job.get("engine") or ""),
                                 ok=False)
        RUNTIME.inc("asrhub_jobs_total", {"status": "failed"})
        RUNTIME.note_error(error.code, error.retryable)
        if error.code == "no_speech":
            RUNTIME.inc("asrhub_no_speech_total")
        self.db.add_event(job_id, "failed", error.message, _для_владельца(error))
        # Исходная причина — в журнал сервера, с трассировкой: здесь её
        # искали, а находили только `error.message` («Непредвиденная ошибка
        # движка: KeyError: 'segments'»), и найти место в коде было нечем.
        # Владельцу задания, наоборот, сырой текст исключения (полные пути к
        # весам и загрузкам) больше не уходит — см. `_для_владельца`.
        log.error("Задание %s провалено: %s", job_id, error.message,
                  extra={"job_id": job_id, "error_code": error.code},
                  exc_info=_трассировка(error))
        # Подсказка — это и есть причина с лечением. В журнал она не
        # попадала вовсе: там оставался тот же факт, что и в карточке
        # задания, и разбираться приходилось наугад.
        if error.hint:
            for строка in str(error.hint).splitlines():
                if строка.strip():
                    log.error("Задание %s: %s", job_id, строка.strip(),
                              extra={"job_id": job_id, "error_code": error.code})
        self._emit("job.failed", {"id": job_id, "error": _для_владельца(error)})
        self._discard_results(outdir)
        self._send_webhook(job_id)

    def _без_расшифровки(self, job: dict[str, Any], error: ASRHubError, статус: str,
                         outdir: Path | None) -> None:
        """Завершает задание итогом «без речи» или «короткая запись».

        Причина остаётся в полях `error_*`: других полей «почему так» у
        задания нет, а карточка и API показывают её не как ошибку, а как
        пояснение к итогу — состояние задания говорит, что это не отказ.
        """
        job_id = job["id"]
        поля: dict[str, Any] = {
            "status": статус, "finished_at": now(), "progress": 1.0,
            "stage": "без речи" if статус == STATUS_NO_SPEECH else "короткая запись",
            "error_code": error.code, "error_message": error.message,
            "error_hint": error.hint, "instance_id": None, "heartbeat_at": None,
            **self._уведомление_ждёт(job)}
        длительность = (error.details or {}).get("duration_s")
        if длительность is not None and not float(job.get("media_duration_s") or 0):
            поля["media_duration_s"] = float(длительность)
        if not self._write_own(job_id, **поля):
            self._discard_unless_taken(outdir, job_id)
            return
        RUNTIME.inc("asrhub_jobs_total", {"status": статус})
        if статус == STATUS_NO_SPEECH:
            RUNTIME.inc("asrhub_no_speech_total")
        self.db.add_event(job_id, статус, error.message, _для_владельца(error))
        log.info("Задание %s: %s", job_id, error.message,
                 extra={"job_id": job_id, "error_code": error.code})
        данные = {"id": job_id, "status": статус, "message": error.message}
        # Имя события — строкой, а не сборкой: по строкам сверяется таблица
        # событий в документации (tests/test_api.py).
        if статус == STATUS_NO_SPEECH:
            self._emit("job.no_speech", данные)
        else:
            self._emit("job.short", данные)
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
