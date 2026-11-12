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

import contextlib
import threading
import time
from typing import Any

from ..logging_setup import get_logger
from . import policy, tasks
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


def _для_записи(клиент: Any, job_id: str) -> Any:
    """Контекст «разбираем эту запись»: ответы модели кешируются за ней.

    Подставной клиент в проверках его не знает — тогда пустой контекст.
    """
    контекст = getattr(клиент, "для_записи", None)
    return контекст(job_id) if callable(контекст) else contextlib.nullcontext()


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
        """Свежая запись — в очередь разбора, если слой включён и разбор новых.

        Короткий звонок и сомнительная расшифровка (`llm.policy`) сами в
        очередь не встают: об этом событие в истории записи, а разобрать её
        по просьбе можно всегда.
        """
        if not (self.client.enabled and self.settings.get("llm_auto", True)):
            return
        try:
            job = self.db.get_job(str(job_id)) or {}
        except Exception:                                    # noqa: BLE001
            job = {}
        причина = policy.пропустить(job, self.settings) if job else ""
        if причина:
            self.db.add_event(str(job_id), "llm", f"Смысловой разбор пропущен: {причина}",
                              {"skipped": причина})
            return
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
                    из_очереди: bool = False, fresh: bool = False) -> dict[str, Any]:
        """Разбор одной записи сейчас, в вызывающем потоке; ответ — в базу.

        `fresh` — спросить модель заново, мимо кеша ответов: «Разобрать
        заново» без него всегда отдавало прежний ответ, не потратив ни одного
        вызова, — и после смены модели на ту же подсказку, и после правки,
        которая ответ и должна была изменить.
        """
        if not force:
            готовое = self.db.llm_get(job_id)
            if готовое and готовое.get("version") == tasks.VERSION and not готовое.get("error"):
                # Закрывается только ждущая строка очереди: её разбор уже
                # есть. Идущую — нет: её прямо сейчас разбирает фоновый поток,
                # и «готово» поверх неё показывало бы конец разбора, которого
                # ещё не было.
                self.db.execute(
                    "UPDATE llm_queue SET state=?, finished_at=?, error='' "
                    "WHERE job_id=? AND state=?",
                    (self.db.LLMQ_ГОТОВО, time.time(), str(job_id), self.db.LLMQ_ЖДЁТ))
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
        try:
            return self._разобрать(job_id, job, fresh=fresh)
        except LLMError as exc:
            self._отметить_сбой(job_id, str(exc))
            self.db.llmq_finish(job_id, error=str(exc))
            raise
        except Exception as exc:                             # noqa: BLE001
            # Не сбой модели, а что угодно другое: сбой базы на чтении
            # сегментов или на записи ответа, ошибка в разборе. Строка «идёт»
            # закрывалась только веткой LLMError, а отметка жизни освежала
            # все свои идущие строки — и брошенная не устаревала никогда:
            # «Запись уже разбирается прямо сейчас» до перезапуска сервера.
            # Фоновому циклу закрытие тоже нужно, он его и делает сам — с
            # возвратом в очередь и счётом попыток (см. `_loop`).
            if из_очереди:
                raise
            текст = f"{type(exc).__name__}: {exc}"
            log.warning("Смысловой разбор %s дал сбой: %s", job_id, текст)
            try:
                self._отметить_сбой(job_id, текст)
                self.db.llmq_finish(job_id, error=текст)
            except Exception as второй:                     # noqa: BLE001
                log.debug("Строка очереди %s не закрыта: %s", job_id, второй)
            raise LLMError(f"Разбор записи не удался: {текст}") from exc

    def _оператор(self, job_id: str, job: dict[str, Any],
                  сегменты: list[dict[str, Any]]) -> str:
        """Кто оператор — тот же, что у разбора содержания этой записи.

        Раньше бралась только настройка `content_agent_speaker`, а она по
        умолчанию пуста: разбор содержания определял оператора сам («кто
        заговорил первым», у переписки — названная при приёме сторона), а
        модель получала разговор без единой подписи «Сотрудник» — и отвечала
        на вопросы о поведении сотрудника, угадывая, кто он.
        """
        заданный = str(self.settings.get("content_agent_speaker") or "").strip()
        if заданный and str(job.get("source") or "") != "text":
            return заданный
        try:
            разбор = self.db.get_content(job_id) or {}
        except Exception:                                    # noqa: BLE001
            разбор = {}
        if разбор.get("agent_speaker"):
            return str(разбор["agent_speaker"])
        # Разбора содержания нет (выключен, ещё не дошёл) — то же правило,
        # что у него (`ContentIndex.оператор_записи`).
        if str(job.get("source") or "") == "text":
            from .. import textchat  # noqa: PLC0415

            параметры = job.get("params") or {}
            подсказка = str((параметры.get("agent") if isinstance(параметры, dict) else "")
                            or заданный or "")
            return str(textchat.оператор_среди(сегменты, подсказка) or "")
        from ..content import compliance  # noqa: PLC0415

        return str(compliance.agent(сегменты) or "")

    def _разобрать(self, job_id: str, job: dict[str, Any], *,
                   fresh: bool = False) -> dict[str, Any]:
        """Сам разбор: вызовы модели и запись ответа в базу."""
        сегменты = self.db.get_segments(job_id)
        скрипт = list(self.settings.get("content_script") or [])
        оператор = self._оператор(job_id, job, сегменты)
        причины, исходы = self._списки_отдела(job_id)
        self._отметить_версию()
        мимо = getattr(self.client, "мимо_кеша", None)
        with _для_записи(self.client, job_id), \
                (мимо() if fresh and callable(мимо) else contextlib.nullcontext()):
            итог = tasks.analyze(self.client, text=str(job.get("text") or ""),
                                 segments=сегменты, settings=self.settings,
                                 agent_speaker=оператор, script=скрипт,
                                 reasons=причины, outcomes=исходы)
        замечания = [str(з) for з in (итог.get("warnings") or []) if str(з).strip()]
        сомнение = policy.сомнительная(job, self.settings)
        if сомнение:
            # Распознавание, похоже, ошиблось: ответ модели — пересказ того,
            # что распознано, а не того, что сказано. Пометка видна в
            # карточке рядом с ответом.
            замечания.append(f"по сомнительной расшифровке: {сомнение}")
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
        # Сбой записи ответа (база занята, диск полон) закрывает строку «идёт»
        # там же, где любой другой сбой разбора (см. `analyze_job`).
        self.db.llm_save(
            job_id, tasks.VERSION, model=self.client.model,
            summary=итог.get("summary"), reason=итог.get("reason"),
            reason_quote=итог.get("reason_quote"), outcome=итог.get("outcome"),
            outcome_quote=итог.get("outcome_quote"), resolved=итог.get("resolved"),
            actions=итог.get("actions"), trackers=итог.get("trackers"),
            scorecard=итог.get("scorecard"), chunks=итог.get("chunks") or 1,
            calls=итог.get("calls") or 0, latency_ms=итог.get("latency_ms"),
            warnings=замечания or None,
            error=("; ".join(замечания) or None) if пусто else None,
            reason_why=итог.get("reason_why"), outcome_why=итог.get("outcome_why"),
            fields=итог.get("fields"), score=итог.get("score"),
            critical=итог.get("critical_failed") or None)
        self._сбои.pop(job_id, None)
        self.db.llmq_finish(
            job_id, error="; ".join(замечания) if пусто and замечания else "",
            latency_ms=итог.get("latency_ms"), calls=int(итог.get("calls") or 0),
            chunks=int(итог.get("chunks") or 1))
        готово = self.db.llm_get(job_id) or итог
        if not пусто:
            self._после_ответа(job_id, job, готово, замечания)
        self._после_модели(job_id)
        return готово

    # --- после ответа -----------------------------------------------------

    def _после_ответа(self, job_id: str, job: dict[str, Any], ответ: dict[str, Any],
                      замечания: list[str]) -> None:
        """Уведомление по умному трекеру, сомнительное — человеку, векторы смысла.

        Каждое — отдельно и без права уронить разбор: ответ уже в базе, а
        это надстройки над ним.
        """
        for шаг in (self._уведомить_трекеры, self._риск, self._сомнительное_человеку,
                    self._векторы):
            try:
                шаг(job_id, job, ответ, замечания)
            except Exception as exc:                         # noqa: BLE001
                log.warning("После смыслового разбора %s (%s): %s", job_id,
                            шаг.__name__, exc)

    def _уведомить_трекеры(self, job_id: str, job: dict[str, Any], ответ: dict[str, Any],
                           _замечания: list[str]) -> None:
        """Сработавший умный трекер — событием в истории и на `tracker_url`.

        Тем же адресом и в той же форме, что трекер правил (`event: tracker`),
        с пометкой `source: llm` и цитатой: приёмник, настроенный на трекеры
        правил, принимает и эти, а отличить их может по полю.
        """
        if not self.settings.get("llm_trackers_notify", True):
            return
        сработали = [т for т in (ответ.get("trackers") or [])
                     if isinstance(т, dict) and т.get("fired")]
        if not сработали:
            return
        события = []
        for т in сработали:
            данные = {"job_id": job_id, "category": str(т.get("id")),
                      "label": str(т.get("label") or т.get("id")), "kind": "llm",
                      "source": "llm", "quote": str(т.get("quote") or "")[:300],
                      "filename": job.get("filename"), "owner": job.get("owner"),
                      "created_at": job.get("created_at")}
            self.db.add_event(job_id, "tracker",
                              f"Сработал умный трекер «{данные['label']}»", данные)
            события.append(данные)
        адрес = str(self.settings.get("tracker_url") or "").strip()
        if not адрес:
            return
        from ..job_queue import check_outbound_url  # noqa: PLC0415
        from ..maintenance import send_json  # noqa: PLC0415

        внутренние = bool(self.settings.get("webhook_allow_internal"))
        check_outbound_url(адрес, внутренние)

        def отправить() -> None:
            for событие in события:
                send_json({"event": "tracker", **событие}, адрес, what="трекер",
                          allow_internal=внутренние)

        threading.Thread(target=отправить, name="asrhub-llm-tracker", daemon=True).start()

    def _риск(self, job_id: str, job: dict[str, Any], ответ: dict[str, Any],
              _замечания: list[str]) -> None:
        """Высокий риск ухода или эскалации — событием и на `tracker_url`.

        Балл — по ответу модели и разбору содержания (`insight.риск`); порог
        — `llm_risk_threshold`. Событие `risk` идёт тем же адресом, что
        трекеры: приёмник уже настроен, а отличить можно по полю `event`.
        """
        if not self.settings.get("llm_risk_notify", True):
            return
        from . import insight  # noqa: PLC0415

        разбор = self.db.get_content(job_id) or {}
        балл, причины = insight.риск({**разбор, **ответ})
        if балл < insight.порог_риска(self.settings):
            return
        данные = {"job_id": job_id, "score": балл, "why": причины,
                  "reason": ответ.get("reason"), "outcome": ответ.get("outcome"),
                  "filename": job.get("filename"), "owner": job.get("owner"),
                  "created_at": job.get("created_at")}
        self.db.add_event(job_id, "risk", f"Высокий риск ухода или эскалации: {балл}", данные)
        адрес = str(self.settings.get("tracker_url") or "").strip()
        if not адрес:
            return
        from ..job_queue import check_outbound_url  # noqa: PLC0415
        from ..maintenance import send_json  # noqa: PLC0415

        внутренние = bool(self.settings.get("webhook_allow_internal"))
        check_outbound_url(адрес, внутренние)
        threading.Thread(target=send_json, args=({"event": "risk", **данные}, адрес),
                         kwargs={"what": "риск", "allow_internal": внутренние},
                         name="asrhub-llm-risk", daemon=True).start()

    #: Замечания, по которым ответ модели стоит показать человеку.
    СОМНЕНИЯ = ("вне списка", "цитата не найдена", "не разобран", "по сомнительной расшифровке")

    def _сомнительное_человеку(self, job_id: str, job: dict[str, Any], ответ: dict[str, Any],
                               замечания: list[str]) -> None:
        """Сомнительный ответ модели — в очередь ручной проверки (`llm_doubtful_review`).

        Только когда контроль качества включён, и не больше суточной нормы
        проверок (`qa_daily`): поток проверок должен оставаться ровным, а не
        заваливаться каждым ответом мимо списка.
        """
        if not self.settings.get("llm_doubtful_review", True):
            return
        from .. import qa as qa_mod  # noqa: PLC0415

        свои = qa_mod.настройки(self.settings)
        if not свои["enabled"]:
            return
        сомнения = [з for з in замечания if any(с in з for с in self.СОМНЕНИЯ)]
        if not сомнения:
            return
        за_сутки = self.db.query_one(
            "SELECT COUNT(*) AS n FROM qa_reviews WHERE assigned_at>=? AND reason LIKE ?",
            (time.time() - 86400.0, "сомнительный ответ модели%"))
        if int((за_сутки or {"n": 0})["n"] or 0) >= max(1, свои["daily"]):
            return
        разбор = self.db.get_content(job_id) or {}
        звонок = self.db.call_for_job(job_id) or {}
        self.db.qa_assign(
            job_id, assigned_to=свои["assign_to"], assigned_by="смысловой разбор",
            due_at=time.time() + max(1.0, свои["due_hours"]) * 3600.0,
            agent=str(звонок.get("agent") or разбор.get("agent_speaker") or ""),
            auto_score=разбор.get("agent_score"),
            reason=("сомнительный ответ модели: " + "; ".join(сомнения))[:300])

    def _векторы(self, job_id: str, _job: dict[str, Any], ответ: dict[str, Any],
                 _замечания: list[str]) -> None:
        """Векторы смысла записи — для поиска по смыслу (`llm_embeddings`)."""
        if not self.settings.get("llm_embeddings", False):
            return
        from . import semantic  # noqa: PLC0415

        semantic.проиндексировать(self.db, self.client, self.settings, job_id, ответ)

    def _списки_отдела(self, job_id: str) -> tuple[list[str] | None, list[str] | None]:
        """Причины и исходы отдела сотрудника — по добавочному из журнала звонков."""
        if not self.settings.get("llm_lists_by_department"):
            return None, None
        from .. import employees as справочник  # noqa: PLC0415

        строка: dict[str, Any] = {"job_id": job_id}
        try:
            справочник.подписать_добавочные(self.db, [строка], имена=True)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Отдел сотрудника для %s не найден: %s", job_id, exc)
            return None, None
        return policy.списки_отдела(self.settings, str(строка.get("agent_department") or ""))

    def _отметить_версию(self) -> None:
        """Журнал смен настройки модели — для отметок на трендах."""
        отпечаток, значения = policy.отпечаток(self.settings, tasks.VERSION)
        if отпечаток == getattr(self, "_отпечаток", None):
            return
        try:
            последняя = self.db.llm_versions_last()
            прежние = getattr(self, "_значения", None)
            if последняя is None or последняя.get("fingerprint") != отпечаток:
                self.db.llm_versions_note(
                    version=tasks.VERSION, model=self.client.model, fingerprint=отпечаток,
                    changes=policy.что_изменилось(прежние if последняя else None, значения))
            self._отпечаток = отпечаток
            self._значения = значения
        except Exception as exc:                             # noqa: BLE001
            log.debug("Отметка смены настройки модели не записана: %s", exc)

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
        # Ollama, поставленная без root в каталог данных, не переживает
        # перезапуск сервера: она была его дочерним процессом. Поднимаем её
        # здесь — фоном, чтобы старт сервера не ждал службу модели.
        if str(self.settings.get("llm_backend") or "") == "ollama":
            threading.Thread(target=self._своя_ollama, name="asrhub-llm-ollama",
                             daemon=True).start()
        self._thread = threading.Thread(target=self._loop, name="asrhub-llm", daemon=True)
        self._thread.start()
        if self._служба is None or not self._служба.is_alive():
            self._служба = threading.Thread(target=self._служебный, name="asrhub-llm-svc",
                                            daemon=True)
            self._служба.start()

    def _своя_ollama(self) -> None:
        from . import provision  # noqa: PLC0415

        try:
            provision.поднять_свою(self.settings)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Ollama из каталога данных не поднята: %s", exc)

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
        сейчас = time.time()
        архив_можно = policy.в_окне_архива(self.settings, сейчас)
        предел = policy.предел_архива(self.settings)
        if архив_можно and предел:
            архив_можно = (self.db.llmq_archive_done_since(policy.начало_суток(сейчас))
                           < предел)
        # Вне окна архива (`llm_archive_hours`) и сверх суточного предела
        # архивные строки ждут, а свежие записи и просьбы людей идут.
        строка = self.db.llmq_take(skip_kinds=() if архив_можно else ("архив",))
        if строка is not None:
            return str(строка["job_id"])
        if not self.settings.get("llm_backfill", False) or not архив_можно:
            return None
        порция = max(1, _целое(self.settings, "llm_queue_batch", ПОРЦИЯ))
        остыть = _целое(self.settings, "llm_queue_cooldown_s", int(ОСТЫТЬ))
        свежий = time.time() - остыть
        ожидают = self.db.llm_pending(
            tasks.VERSION, limit=порция, min_duration_s=policy.мин_длительность(self.settings),
            skip_suspect_share=(policy.порог_сомнительных(self.settings)
                                if policy.режим_сомнительных(self.settings) == "skip" else None))
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
            # Взятие строки — под защитой, как и сам разбор. Оно ходит в базу
            # (llmq_take, llm_pending, llmq_put), и одно «database is locked»
            # дольше busy_timeout или переполненный диск выходили из цикла:
            # поток asrhub-llm умирал, перезапуска не было, и очередь разбора
            # стояла до перезапуска сервера, а состояние показывало «не идёт»
            # без единой ошибки. Теперь — запись в состояние и пауза.
            try:
                job_id = self._next()
            except Exception as exc:                         # noqa: BLE001
                self.last_error = f"очередь разбора недоступна: {exc}"
                log.warning("Очередь смыслового разбора недоступна: %s", exc)
                пауза = max(простой, 5.0)
                continue
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
