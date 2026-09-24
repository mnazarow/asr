"""Фоновый поток смыслового разбора.

Модель отвечает секунды, а не миллисекунды, и звать её из потока
распознавания значило бы задерживать выдачу результата. Поэтому свой
поток: свежие записи встают в очередь при завершении задания, архив — по
одной, от новых к старым, когда включён `llm_backfill`.

Уступает распознаванию: пока в очереди ждут задания, поток спит
(`llm_yield_to_queue`) — расшифровка важнее пересказа. Сбой одной записи
не останавливает остальные: ответ с ошибкой кладётся в таблицу, чтобы
запись не возвращалась в очередь бесконечно, а в карточке была видна
причина.

Очередь живёт в базе, а не в памяти процесса. Это стоило одной строки
таблицы, а дало три вещи, которых иначе не было:

* **перезапуск не теряет очередь.** Раньше сервер, перезапущенный с
  тысячей записей в разборе, просыпался с пустой очередью, и человек
  узнавал об этом через неделю по дырам в аналитике;
* **очередь видно.** Раздел «Очередь LLM» показывает, что идёт сейчас,
  что ждёт, сколько ждало, сколько заняло и чем кончилось. Из памяти
  процесса это было доступно одной строкой «в очереди: 812»;
* **очередь можно трогать.** Приостановить, поднять запись, отменить,
  повторить упавшие — всё это действия над строками таблицы, а не над
  `queue.Queue`, у которой из управления есть только «положить».

Запрос к модели идёт ровно один за раз: одна видеокарта, и делить её
между двумя разборами — значит замедлить оба. Держит это не только
семафор клиента (`llm_max_concurrent`), но и сам порядок: строку берёт
`llmq_take()` — она же помечает её выполняющейся, и второму потоку та же
запись не достанется.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..logging_setup import get_logger
from . import tasks
from .client import LLMClient, LLMError

log = get_logger("llm")

#: Пауза, когда делать нечего или очередь распознавания занята.
ПАУЗА_ПРОСТОЯ = 15.0

#: Сколько записей архива брать за один заход.
ПОРЦИЯ = 5

#: Сколько не подходить к записи после сбоя разбора.
ОСТЫТЬ = 1800.0

#: Как часто подтверждать, что идущий разбор жив. Разбор длинной записи —
#: это минуты вызовов модели, и без отметки соседний сервер на общей базе
#: не отличил бы живой разбор от брошенного.
ОТМЕТКА_С = 30.0

#: Через сколько секунд без отметки чужой идущий разбор считается брошенным
#: (сервер, который его вёл, умер или потерял базу).
СРОК_ЖИЗНИ_С = 600.0

#: Как часто подбирать брошенное соседями и досылать отложенные примечания CRM.
ОБХОД_С = 300.0


def _целое(настройки: Any, ключ: str, по_умолчанию: int) -> int:
    """Целое из настроек с сохранением осмысленного нуля.

    `int(x or N)` превращает заданный ноль в умолчание: «не ждать после
    сбоя вовсе» читалось бы как «ждать полчаса».
    """
    значение = настройки.get(ключ) if настройки is not None else None
    if значение is None or значение == "":
        return по_умолчанию
    try:
        return int(значение)
    except (TypeError, ValueError):
        return по_умолчанию


class LLMWorker:
    def __init__(self, db: Any, settings: Any, client: LLMClient, *,
                 queue_state: Any = None, content_index: Any = None):
        self.db = db
        self.settings = settings
        self.client = client
        self._queue_state = queue_state
        self._content = content_index
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        #: Служебный поток: отметка жизни, подбор брошенного соседями и
        #: досылка отложенных примечаний CRM. Отдельно от разбора — тот
        #: стоит в вызове модели минутами.
        self._служба: threading.Thread | None = None
        self.done = 0
        self.failed = 0
        self.last_error: str | None = None
        self.current: str | None = None
        #: Когда начался идущий разбор — чтобы раздел показывал не только
        #: «идёт», но и сколько уже идёт: на длинной записи это главный
        #: признак того, что модель жива, а не встала.
        self.current_since: float | None = None
        #: Когда по записи последний раз не вышло: {id: время}. Держит
        #: фоновый разбор от бесконечного возврата к записи, у которой есть
        #: прежний ответ (строку с ошибкой поверх него не пишем).
        self._сбои: dict[str, float] = {}

    # --- снаружи ----------------------------------------------------------

    def enqueue(self, job_id: str) -> None:
        """Свежая запись — в очередь разбора, если слой включён и разбор новых."""
        if self.client.enabled and self.settings.get("llm_auto", True):
            self.db.llmq_put(str(job_id), kind="свежая запись",
                             priority=_целое(self.settings, "llm_queue_priority_new", 50))

    def enqueue_many(self, job_ids: Any, *, kind: str = "по просьбе",
                     priority: int | None = None) -> int:
        """Ставит записи в очередь разбора по явной просьбе.

        Мимо `llm_auto`: эта настройка про свежие записи, а разбор архива
        запускает администратор — раз попросил, значит надо.
        """
        важность = (priority if priority is not None
                    else _целое(self.settings, "llm_queue_priority_manual", 70))
        сколько = 0
        for job_id in job_ids:
            if self.db.llmq_put(str(job_id), kind=kind, priority=важность):
                сколько += 1
        return сколько

    @property
    def paused(self) -> bool:
        return bool(self.settings.get("llm_queue_paused", False))

    def analyze_job(self, job_id: str, *, force: bool = False,
                    из_очереди: bool = False) -> dict[str, Any]:
        """Разбор одной записи сейчас, в вызывающем потоке; ответ — в базу."""
        if not force:
            готовое = self.db.llm_get(job_id)
            if готовое and готовое.get("version") == tasks.VERSION and not готовое.get("error"):
                self.db.llmq_finish(job_id, latency_ms=готовое.get("latency_ms"))
                self._после_модели(job_id)
                return готовое
        job = self.db.get_job(job_id)
        if job is None:
            # Запись удалили, пока она стояла в очереди. Строку очереди
            # надо закрыть здесь: она уже помечена «идёт» (её пометил
            # `llmq_take`), а вернуться к ней некому — задания больше нет.
            # Без этой строки раздел показывал вечный текущий запрос по
            # записи, которой не существует, и снять его было нечем.
            self.db.llmq_finish(job_id, error="запись удалена")
            raise LLMError(f"Задание «{job_id}» не найдено.")
        # Разбор по кнопке идёт в потоке запроса. В разделе очереди он всё
        # равно обязан быть виден: иначе «ничего не идёт» соседствует с
        # занятой видеокартой.
        if not из_очереди and not self.db.llmq_begin(job_id):
            # Запись уже разбирают — фоновый поток или соседняя кнопка.
            # Второй запрос к модели о том же самом стоит ещё одного прохода
            # видеокарты, а победит в базе тот ответ, который придёт вторым.
            raise LLMError(
                f"Запись «{job_id}» уже разбирается прямо сейчас. Дождитесь "
                "окончания — ход виден в разделе очереди разбора, ответ "
                "появится в карточке записи сам.")
        сегменты = self.db.get_segments(job_id)
        скрипт = list(self.settings.get("content_script") or [])
        оператор = str(self.settings.get("content_agent_speaker") or "")
        try:
            итог = tasks.analyze(self.client, text=str(job.get("text") or ""),
                                 segments=сегменты, settings=self.settings,
                                 agent_speaker=оператор, script=скрипт)
        except LLMError as exc:
            self._отметить_сбой(job_id, str(exc))
            self.db.llmq_finish(job_id, error=str(exc))
            raise
        замечания = [str(з) for з in (итог.get("warnings") or []) if str(з).strip()]
        # Пустой разбор — тот, в котором нет ответа ни на одну заказанную
        # задачу. Раньше пустым считался любой без резюме и исхода: сервер,
        # настроенный на одни трекеры или скоркарту (`llm_tasks`), получал на
        # каждой длинной записи «ошибку» из информационного замечания
        # «разбор по пересказам N частей», и свод такую запись выбрасывал.
        # Список действий, трекеров и ответов скоркарты — ответ, даже пустой:
        # «договорённостей нет» — тоже результат.
        пусто = not (итог.get("summary") or итог.get("outcome") or итог.get("reason")
                     or any(итог.get(п) is not None
                            for п in ("actions", "trackers", "scorecard")))
        self.db.llm_save(
            job_id, tasks.VERSION, model=self.client.model,
            summary=итог.get("summary"), reason=итог.get("reason"),
            reason_quote=итог.get("reason_quote"), outcome=итог.get("outcome"),
            outcome_quote=итог.get("outcome_quote"), resolved=итог.get("resolved"),
            actions=итог.get("actions"), trackers=итог.get("trackers"),
            scorecard=итог.get("scorecard"), chunks=итог.get("chunks") or 1,
            calls=итог.get("calls") or 0, latency_ms=итог.get("latency_ms"),
            warnings=замечания or None,
            error=("; ".join(замечания) or None) if пусто else None)
        self._сбои.pop(job_id, None)
        self.db.llmq_finish(
            job_id, error="; ".join(замечания) if пусто and замечания else "",
            latency_ms=итог.get("latency_ms"), calls=int(итог.get("calls") or 0),
            chunks=int(итог.get("chunks") or 1))
        self._после_модели(job_id)
        return self.db.llm_get(job_id) or итог

    def _после_модели(self, job_id: str) -> None:
        """Модель закончила с записью — пора примечанию в CRM, если оно её ждало.

        Примечание свежей записи откладывается до смыслового разбора: без
        этого оно уходило сразу после разбора содержания, с пустым
        пересказом, и дослать пересказ было уже некуда — второе примечание
        в карточке сделки хуже первого пустого.
        """
        from .. import crm  # noqa: PLC0415

        try:
            crm.после_модели(self.db, self.settings, job_id)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Примечание в CRM по %s не отправлено: %s", job_id, exc)

    def _отметить_сбой(self, job_id: str, текст: str) -> None:
        """Отметка о сбое — но не поверх удачного разбора.

        `llm_save` кладёт строку целиком, поэтому пустая строка с ошибкой
        затирала бы прежний ответ модели: нажал «Заново» при лежащем
        сервере — и разбора, который был, больше нет. Так что удачный ответ
        остаётся; о сбое узнаёт тот, кто его вызвал, — маршрут возвращает
        текст ошибки, а фоновый поток пишет её в журнал.

        Строка с ошибкой нужна записям без ответа: по ней разбор архива
        понимает, что к записи уже подходили, и не возвращается к ней
        бесконечно. Для записей с ответом ту же роль играет `_сбои`:
        полчаса после сбоя фоновый поток их не трогает.
        """
        self._сбои[job_id] = time.time()
        try:
            прежнее = self.db.llm_get(job_id)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Прежний разбор %s не прочитан: %s", job_id, exc)
            прежнее = None
        # Полезным считается любой разобранный ответ, а не только резюме с
        # исходом: сервер, настроенный на одни трекеры и скоркарту
        # (`llm_tasks`), терял их при первом же сбое модели.
        if прежнее and any(прежнее.get(п) for п in
                           ("summary", "outcome", "reason", "actions",
                            "trackers", "scorecard")):
            return
        self.db.llm_save(job_id, tasks.VERSION, model=self.client.model,
                         error=текст, calls=0, latency_ms=None)

    def status(self) -> dict[str, Any]:
        счёт = self.db.llmq_counts()
        return {"running": bool(self._thread and self._thread.is_alive()),
                "queued": int(счёт.get(self.db.LLMQ_ЖДЁТ, 0)),
                "counts": счёт,
                "current": self.current,
                "current_since": self.current_since,
                "paused": self.paused,
                "done": self.done, "failed": self.failed, "last_error": self.last_error,
                "auto": bool(self.settings.get("llm_auto", True)),
                "backfill": bool(self.settings.get("llm_backfill", False))}

    # --- поток ------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            # Прошлый поток ещё жив: остановка застала его внутри вызова
            # модели. Снимаем флаг — он продолжит работу; второй такой же
            # разбирал бы ту же очередь параллельно.
            self._stop.clear()
            return
        self._stop.clear()
        # Записи, застигнутые остановкой сервера в состоянии «идёт», иначе
        # остались бы такими навсегда: раздел показывал бы вечный текущий
        # запрос, а сама запись не разобралась бы уже никогда. Только свои и
        # брошенные: идущий разбор соседа по общей базе не трогаем.
        try:
            вернулось = self.db.llmq_reset_running(stale_s=СРОК_ЖИЗНИ_С)
            if вернулось:
                log.info("Возвращено в очередь разбора после перезапуска: %d", вернулось)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Очередь разбора не приведена в порядок: %s", exc)
        self._thread = threading.Thread(target=self._loop, name="asrhub-llm", daemon=True)
        self._thread.start()
        if self._служба is None or not self._служба.is_alive():
            self._служба = threading.Thread(target=self._служебный, name="asrhub-llm-svc",
                                            daemon=True)
            self._служба.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        служба = self._служба
        if служба is not None and служба.is_alive():
            # Служебный поток просыпается от флага сразу, если только не
            # досылает примечание в CRM прямо сейчас.
            служба.join(timeout=min(2.0, timeout))
        if служба is not None and not служба.is_alive():
            self._служба = None
        поток = self._thread
        if поток and поток.is_alive():
            поток.join(timeout=timeout)
            if поток.is_alive():
                # Вызов модели идёт минуты, ждать их на остановке нельзя.
                # Ссылку не отпускаем: поток завершится сам, а `start()` по
                # ней поймёт, что поднимать второй не нужно.
                log.warning("Поток смыслового разбора занят вызовом модели; "
                            "завершится сам после ответа.")
                return
        self._thread = None

    def _служебный(self) -> None:
        """Отметка жизни раз в полминуты, обход брошенного — раз в пять минут."""
        from .. import crm  # noqa: PLC0415

        последний_обход = time.time()
        while not self._stop.wait(timeout=ОТМЕТКА_С):
            try:
                self.db.llmq_heartbeat()
            except Exception as exc:                         # noqa: BLE001
                log.debug("Отметка жизни очереди разбора не поставлена: %s", exc)
            if time.time() - последний_обход < ОБХОД_С:
                continue
            последний_обход = time.time()
            try:
                вернулось = self.db.llmq_reset_running(stale_s=СРОК_ЖИЗНИ_С, свои=False)
                if вернулось:
                    log.info("Подобрано брошенных разборов соседних серверов: %d", вернулось)
            except Exception as exc:                         # noqa: BLE001
                log.debug("Брошенные разборы не подобраны: %s", exc)
            try:
                crm.досылка(self.db, self.settings)
            except Exception as exc:                         # noqa: BLE001
                log.warning("Досылка примечаний в CRM не удалась: %s", exc)

    def _queue_busy(self) -> bool:
        """Ждут ли задания распознавания — тогда модель подождёт."""
        if not self.settings.get("llm_yield_to_queue", True) or self._queue_state is None:
            return False
        try:
            return int(self._queue_state()) > 0
        except Exception:                                    # noqa: BLE001
            return False

    def _next(self) -> str | None:
        """Следующая запись: сперва очередь, потом архив.

        Архивные записи не берутся напрямую — они СТАВЯТСЯ в ту же очередь.
        Иначе в разделе «Очередь LLM» фоновый разбор архива выглядел бы
        пустотой: очередь пуста, а модель занята.
        """
        строка = self.db.llmq_take()
        if строка is not None:
            return str(строка["job_id"])
        if not self.settings.get("llm_backfill", False):
            return None
        порция = max(1, _целое(self.settings, "llm_queue_batch", ПОРЦИЯ))
        остыть = _целое(self.settings, "llm_queue_cooldown_s", int(ОСТЫТЬ))
        свежий = time.time() - остыть
        ожидают = self.db.llm_pending(tasks.VERSION, limit=порция)
        добавлено = 0
        for з in ожидают:
            job_id = str(з["id"])
            if self._сбои.get(job_id, 0.0) < свежий:
                if self.db.llmq_put(job_id, kind="архив", priority=_целое(
                        self.settings, "llm_queue_priority_backfill", 30)):
                    добавлено += 1
        if not добавлено:
            return None
        строка = self.db.llmq_take()
        return str(строка["job_id"]) if строка else None

    def _loop(self) -> None:
        пауза = 1.0
        while not self._stop.wait(timeout=пауза):
            простой = float(_целое(self.settings, "llm_queue_idle_s", int(ПАУЗА_ПРОСТОЯ)))
            if not self.client.enabled or self.paused or self._queue_busy():
                пауза = простой
                continue
            job_id = self._next()
            if job_id is None:
                пауза = простой
                continue
            self.current = job_id
            self.current_since = time.time()
            try:
                self.analyze_job(job_id, force=True, из_очереди=True)
                self.done += 1
                пауза = 0.5
            except LLMError as exc:
                self.failed += 1
                self.last_error = str(exc)
                log.warning("Смысловой разбор %s не удался: %s", job_id, exc)
                # Запись возвращается в очередь: причина почти всегда
                # временная — сервер модели не ответил, видеокарта занята.
                # Раньше здесь не делалось НИЧЕГО, и взятая строка
                # оставалась «идёт» навсегда: раздел показывал вечный
                # текущий запрос, а запись не разбиралась больше никогда,
                # даже когда модель возвращалась. После нескольких заходов
                # запись уходит в «ошибку» — оттуда её поднимает кнопка
                # «Повторить упавшие».
                # Сервер модели лежит или занят — заход не считается:
                # иначе минутный перезапуск Ollama уводил головные записи
                # в «ошибку» за полминуты.
                временная = bool(getattr(exc, "временная", False))
                try:
                    вернулась = self.db.llmq_release(
                        job_id, error=str(exc),
                        attempts_max=_целое(self.settings, "llm_queue_attempts", 3),
                        потратить=not временная)
                except Exception:                            # noqa: BLE001
                    log.debug("Строку очереди %s вернуть не удалось", job_id)
                    вернулась = True
                if not вернулась:
                    # Попытки кончились: примечание в CRM, ждавшее пересказа,
                    # уходит без него — ждать больше нечего.
                    self._после_модели(job_id)
                # Сервер модели лежит — не долбить его каждой записью.
                пауза = простой
            except Exception as exc:                         # noqa: BLE001
                self.failed += 1
                self.last_error = str(exc)
                log.warning("Смысловой разбор %s дал сбой: %s", job_id, exc)
                try:
                    self._отметить_сбой(job_id, str(exc))
                    self.db.llmq_finish(job_id, error=str(exc))
                except Exception:                            # noqa: BLE001
                    pass
                пауза = 2.0
            finally:
                self.current = None
                self.current_since = None
        return None


def wait_idle(worker: LLMWorker, timeout: float = 30.0) -> bool:
    """Ждёт, пока очередь разбора опустеет, — для проверок."""
    крайний = time.time() + timeout
    while time.time() < крайний:
        счёт = worker.db.llmq_counts()
        ждут = int(счёт.get(worker.db.LLMQ_ЖДЁТ, 0)) + int(счёт.get(worker.db.LLMQ_ИДЁТ, 0))
        if not ждут and worker.current is None:
            return True
        time.sleep(0.05)
    return False
