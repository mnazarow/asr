"""Маршруты смыслового слоя: /api/llm/* и /api/content/jobs/{id}/llm.

Состояние слоя и проба сервера модели, разбор одной записи по требованию,
запуск разбора архива и свод ответов за период: причины и исходы, действия
к исполнению, срабатывания умных трекеров, ответы скоркарты.

Разрез по владельцу тот же, что у аналитики записей: обычный ключ видит
свои записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..analytics import PERIODS
from ..errors import ASRHubError, ConfigError, JobNotFound
from ..llm import LLMError, provision, tasks
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_owner,
    require_write,
    same_scope,
    scope_owner,
)

router = APIRouter(prefix="/api", tags=["Языковая модель"])

ПЕРИОД = Query(default="week", pattern="^(hour|day|week|month|quarter|year|all)$")


def _установщик(request: Request) -> Any:
    """Установщик модели из состояния приложения."""
    state = get_state(request)
    if getattr(state, "llm_setup", None) is None:
        raise error_response(ASRHubError(
            "Установщик модели не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return state.llm_setup


def _slot(request: Request) -> tuple[Any, Any, Any]:
    state = get_state(request)
    if getattr(state, "llm", None) is None or getattr(state, "llm_worker", None) is None:
        raise error_response(ASRHubError(
            "Смысловой слой не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return state, state.llm, state.llm_worker


@router.get("/llm/status", summary="Состояние языковой модели")
def llm_status(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Как подключено, доступен ли сервер модели, учёт вызовов, очередь
    разбора и покрытие: сколько записей за сутки уже разобрано."""
    state, клиент, поток = _slot(request)
    since = time.time() - 86400
    # Покрытие за сутки — счётом по указателям (`llm_brief`), а не чтением
    # всех ответов суток: раздел спрашивает состояние при каждом открытии.
    свод = state.db.llm_brief(tasks.VERSION, since, owner=scope_owner(principal))
    годные = [г for г in свод["groups"] if г["ok"]]
    разобрано = sum(int(г["n"]) - int(г["stale"] or 0) for г in годные)
    ошибок = sum(int(г["n"]) for г in свод["groups"] if not г["ok"])
    return {
        **клиент.status(),
        "worker": поток.status(),
        "version": tasks.VERSION,
        "coverage": {"total": свод["total"], "analyzed": разобрано,
                     "errors": ошибок,
                     "share": round(разобрано / свод["total"], 4) if свод["total"] else None},
    }


@router.post("/llm/test", summary="Проверить сервер модели")
def llm_test(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Короткий вызов мимо кеша: доступен ли сервер, знает ли модель,
    сколько ждать ответа. Для настройки — до включения разбора."""
    _, клиент, _ = _slot(request)
    require_admin(principal)
    проба = клиент.probe(fresh=True)
    if not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена.", hint="Задайте llm_backend, llm_url и llm_model."))
    начало = time.perf_counter()
    try:
        ответ = клиент.chat(
            "Отвечай одним объектом JSON.",
            'Ответь ровно так: {"ok": true, "lang": "ru"}',
            kind="test", use_cache=False)
    except LLMError as exc:
        return {**проба, "ok": False, "error": str(exc),
                "ms": round((time.perf_counter() - начало) * 1000, 1)}
    return {**проба, "ok": True, "answer": ответ[:200],
            "ms": round((time.perf_counter() - начало) * 1000, 1), "model": клиент.model}


@router.get("/content/jobs/{job_id}/llm", summary="Смысловой разбор записи")
def llm_job(request: Request, job_id: str,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, клиент, _ = _slot(request)
    try:
        job = state.queue.get(job_id)
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    результат = state.db.llm_get(job_id)
    return {"job_id": job_id, "result": результат, "enabled": клиент.enabled,
            "stale": bool(результат and результат.get("version") != tasks.VERSION),
            "version": tasks.VERSION}


@router.post("/content/jobs/{job_id}/llm", summary="Разобрать запись моделью сейчас")
def llm_job_run(request: Request, job_id: str,
                force: bool = Query(default=True),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Синхронно, в обработчике: ответ приходит вместе с результатом.
    Длинная запись — несколько вызовов; ждать столько, сколько задано
    llm_timeout_s на каждый."""
    state, клиент, поток = _slot(request)
    require_write(principal)
    try:
        job = state.queue.get(job_id)
        require_owner(principal, job)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    if not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена.", hint="Задайте llm_backend, llm_url и llm_model."))
    if job.get("status") != "completed":
        raise error_response(ConfigError("Запись ещё не распознана."))
    try:
        результат = поток.analyze_job(job_id, force=force)
    except LLMError as exc:
        raise error_response(ASRHubError(
            f"Модель не ответила: {exc}",
            hint="Проверьте сервер модели: GET /api/llm/status и POST /api/llm/test.")) from exc
    return {"job_id": job_id, "result": результат, "version": tasks.VERSION}


@router.post("/llm/backfill", summary="Разобрать архив моделью")
def llm_backfill(request: Request, limit: int = Body(default=100, embed=True),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ставит в очередь разбора записи без ответа модели — новые первыми.
    Идут в фоне, по одной, уступая распознаванию."""
    state, клиент, поток = _slot(request)
    require_admin(principal)
    if not клиент.enabled:
        raise error_response(ConfigError("Языковая модель выключена."))
    ожидают = state.db.llm_pending(tasks.VERSION, limit=max(1, min(int(limit), 10000)))
    поставлено = поток.enqueue_many(
        (з["id"] for з in ожидают), kind="архив",
        priority=_целое_настройки(state.settings, "llm_queue_priority_backfill", 30))
    return {"queued": поставлено, "worker": поток.status()}


# ---------------------------------------------------------------------------
# Очередь запросов к модели
# ---------------------------------------------------------------------------

#: Сколько корзин рисовать на графике очереди. Двести — предел, за которым
#: линия перестаёт читаться, а ответ перестаёт быть дешёвым.
КОРЗИН = 96


@router.get("/llm/queue", summary="Очередь запросов к модели")
def llm_queue(request: Request, state_filter: str = Query(default="", alias="state"),
              limit: int = Query(default=50, ge=1, le=500),
              offset: int = Query(default=0, ge=0),
              hours: int = Query(default=24, ge=1, le=24 * 30),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Всё для раздела одним ответом: что идёт, что ждёт, что было.

    Одним, а не пятью: раздел опрашивается раз в пару секунд, и пять
    запросов вместо одного — это пятикратная нагрузка на сервер ради
    картинки, которая всё равно рисуется целиком.
    """
    state, клиент, поток = _slot(request)
    require_write(principal)
    сейчас = time.time()
    с_какого = сейчас - hours * 3600
    # Счётчики, сводка и график — по своим записям, как и список: иначе
    # «список пуст, ждут четыре» рассказывало обычному ключу о чужой работе.
    свой = scope_owner(principal)
    корзин, ряд = state.db.llmq_series(с_какого, сейчас, КОРЗИН, owner=свой)
    состояние = поток.status()
    # Текущая запись — с именем файла: по идентификатору задания человек не
    # узнаёт ничего, а в разделе он смотрит именно на «какую запись жуют».
    # Имя файла — это в колл-центре номер клиента, и соседние списки
    # заданий его прячут. Чужую запись показываем без имени: то, что
    # видеокарта сейчас занята, не секрет, а чем именно — секрет.
    текущее = None
    if состояние.get("current"):
        задание = state.db.get_job(str(состояние["current"])) or {}
        наше = not свой or same_scope(principal, str(задание.get("owner") or ""))
        текущее = {
            "job_id": состояние["current"] if наше else "",
            "filename": (задание.get("filename") or "") if наше else "чужая запись",
            "duration_s": (задание.get("media_duration_s") or 0) if наше else 0,
            "since": состояние.get("current_since"),
        }
    return {
        "worker": состояние,
        "current": текущее,
        "counts": state.db.llmq_counts(owner=свой),
        # Сводка — по тому же промежутку, что и график: [с_какого, сейчас).
        # Обе границы уходят в ответ (`stats.since`, `stats.until`): по ним
        # число «сбоев за окно» раскрывается в список записей, и список
        # обязан резаться там же, где резалось число.
        "stats": state.db.llmq_stats(с_какого, until=сейчас, owner=свой),
        # Ширина корзины — от сервера. Интерфейс считал её сам, от часов
        # браузера: при расхождении часов с сервером подписи графика
        # уезжали от корзин, которые им соответствуют.
        "series": {"buckets": корзин, "since": с_какого,
                   "bucket_seconds": (сейчас - с_какого) / корзин, "rows": ряд},
        "queue": state.db.llmq_list(state=state_filter, limit=limit,
                                    offset=offset, owner=свой),
        "client": клиент.status(),
        "settings": {
            "paused": bool(state.settings.get("llm_queue_paused", False)),
            "auto": bool(state.settings.get("llm_auto", True)),
            "backfill": bool(state.settings.get("llm_backfill", False)),
            "max_concurrent": int(state.settings.get("llm_max_concurrent") or 1),
            "batch": int(state.settings.get("llm_queue_batch") or 5),
            "idle_s": int(state.settings.get("llm_queue_idle_s") or 15),
            "keep_days": int(state.settings.get("llm_queue_keep_days") or 14),
            "yield_to_queue": bool(state.settings.get("llm_yield_to_queue", True)),
        },
        "at": time.time(),
    }


@router.post("/llm/queue/pause", summary="Приостановить или продолжить очередь")
def llm_queue_pause(request: Request, paused: bool = Body(default=True, embed=True),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, _клиент, поток = _slot(request)
    require_admin(principal)
    state.settings.set("llm_queue_paused", bool(paused), source="api")
    # Пауза переживает перезапуск: её ставят, когда видеокарта нужна для
    # другого, и обновление сервера не должно молча снимать её посреди дня.
    сохранено = state.settings.записать_ключи(["llm_queue_paused"])
    state.db.add_event(None, "llm_queue",
                       "Очередь разбора приостановлена" if paused else "Очередь разбора продолжена")
    return {"paused": bool(paused), "worker": поток.status(), **сохранено}


@router.post("/llm/queue/add", summary="Поставить записи в очередь разбора")
def llm_queue_add(request: Request, данные: dict[str, Any] = Body(default={}),
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Поставить в очередь: явный список, всё неразобранное или период.

    Отбор делается запросом к базе, а не перебором в интерфейсе: «разобрать
    всё за квартал» — это десятки тысяч записей, и присылать их списком
    значит гонять мегабайт идентификаторов ради одной кнопки.
    """
    state, клиент, поток = _slot(request)
    require_write(principal)
    if not клиент.enabled:
        raise error_response(ConfigError(
            "Языковая модель выключена.",
            hint="Включите её в настройках: «Языковая модель» → «Как подключена»."))
    ids = [str(и) for и in (данные.get("job_ids") or []) if str(и).strip()]
    # Явный список приходит из «Результатов», где человек отметил свои
    # записи. Чужие сюда попадают только подбором номеров — и раньше
    # проходили: список уезжал в очередь, не читая заданий.
    for номер in ids:
        _своё_задание(state, principal, номер)
    предел = _целое(данные.get("limit"), 1000, 1, 50000)
    # Отбор целиком (не явный список) — это архив, а не просьба об одной
    # записи: такие ставятся с важностью архива, иначе свежие звонки ждали
    # бы, пока переварится весь отбор.
    вид = str(данные.get("kind") or "по просьбе")
    важность = None
    if not ids:
        отбор = str(данные.get("scope") or "pending")
        важность = _целое_настройки(state.settings, "llm_queue_priority_backfill", 30)
        if not данные.get("kind"):
            вид = "архив"
        if отбор == "pending":
            ids = [str(з["id"]) for з in state.db.llm_pending(
                tasks.VERSION, limit=предел, owner=scope_owner(principal))]
        elif отбор == "failed":
            повторено = state.db.llmq_retry_failed(limit=предел,
                                                   owner=scope_owner(principal),
                                                   priority=важность)
            return {"queued": повторено, "scope": отбор, "worker": поток.status()}
        elif отбор == "period":
            начало = _число(данные.get("since"), 0.0)
            конец = _число(данные.get("until"), time.time())
            if not начало or конец <= начало:
                raise error_response(ConfigError(
                    "Для отбора за период нужны начало и конец промежутка."))
            # У `list_jobs` есть «с какого», но нет «по какое»: верхнюю
            # границу отсекаем сами. Брать с запасом и резать в питоне
            # дешевле, чем заводить ещё один разрез в базе ради кнопки.
            задания = state.db.list_jobs(status="completed", since=начало,
                                         limit=предел, light=True,
                                         owner=scope_owner(principal))
            ids = [str(з["id"]) for з in задания
                   if float(з.get("created_at") or 0) <= конец]
        else:
            raise error_response(ConfigError(
                f"Неизвестный отбор: «{отбор}».",
                hint="Ожидается pending, failed, period или явный список job_ids."))
    поставлено = поток.enqueue_many(ids, kind=вид, priority=важность)
    return {"queued": поставлено, "asked": len(ids), "worker": поток.status()}


def _целое_настройки(settings: Any, ключ: str, умолчание: int) -> int:
    """Целое из настроек; кривое значение — умолчание."""
    try:
        return int(settings.get(ключ, умолчание))
    except (TypeError, ValueError):
        return умолчание


def _число(значение: Any, умолчание: float) -> float:
    """Дробное из тела запроса — или понятный отказ вместо пятисотки.

    Тело здесь — свободный словарь, схемой не описанный, и `float("вчера")`
    доходил до общего обработчика: клиент получал «внутреннюю ошибку
    сервера» и трассировку в журнале, хотя виноват был он сам.
    """
    if значение is None or значение == "":
        return умолчание
    try:
        return float(значение)
    except (TypeError, ValueError):
        raise error_response(ConfigError(
            f"Ожидается число, получено «{значение}».")) from None


def _целое(значение: Any, умолчание: int, наименьшее: int, наибольшее: int) -> int:
    """Целое из тела запроса, зажатое в допустимые пределы."""
    return max(наименьшее, min(int(_число(значение, умолчание)), наибольшее))


def _своё_задание(state: Any, principal: Principal, job_id: str) -> dict[str, Any]:
    """Задание по номеру — или отказ, если оно чужое.

    Маршруты очереди принимали номер задания из пути и несли его прямо в
    базу, не читая само задание: проверить владельца было негде, и ключ с
    правом записи снимал чужой разбор или поднимал его на видеокарту.
    """
    задание = state.db.get_job(str(job_id))
    if not задание:
        raise error_response(JobNotFound(str(job_id)))
    require_owner(principal, задание)
    return задание


@router.post("/llm/queue/{job_id}/top", summary="Поднять запись в начало очереди")
def llm_queue_top(request: Request, job_id: str,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    state, _клиент, поток = _slot(request)
    require_write(principal)
    _своё_задание(state, principal, job_id)
    state.db.llmq_put(job_id, kind="срочно", priority=100)
    return {"ok": True, "job_id": job_id, "worker": поток.status()}


@router.delete("/llm/queue/{job_id}", summary="Убрать запись из очереди")
def llm_queue_cancel(request: Request, job_id: str,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Снимает запись, которая ещё ждёт. Идущую не трогаем: ответ модели
    уже оплачен временем видеокарты, и бросать его на полпути незачем."""
    state, _клиент, _поток = _slot(request)
    require_write(principal)
    _своё_задание(state, principal, job_id)
    if not state.db.llmq_cancel(job_id):
        raise error_response(ConfigError(
            f"Запись «{job_id}» в очереди не ждёт.",
            hint="Возможно, её уже разобрали или она разбирается прямо сейчас."))
    return {"ok": True, "job_id": job_id}


@router.post("/llm/queue/clear", summary="Очистить очередь")
def llm_queue_clear(request: Request, state_filter: str = Body(default="", embed=True,
                                                               alias="state"),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Пустое значение убирает всё, кроме идущего прямо сейчас."""
    state, _клиент, поток = _slot(request)
    require_admin(principal)
    убрано = state.db.llmq_clear(state_filter)
    state.db.add_event(None, "llm_queue", f"Из очереди разбора убрано записей: {убрано}")
    return {"removed": убрано, "worker": поток.status()}


#: Сколько записей идёт в построчный список раздела «Голосовая аналитика» и
#: сколько обязательств — в список действий: свод за год — это десятки тысяч
#: разборов, и отдавать их страницей незачем. Распределения и перекрёстная
#: таблица считаются по всем записям периода.
СТРОК_В_СВОДЕ = 300
ДЕЙСТВИЙ_В_СВОДЕ = 200


def _счётная_часть(state: Any, since: float,
                   owner: str | list[str] | None) -> dict[str, Any]:
    """Числа свода, которые считаются без чтения ответов модели.

    Сколько записей и сколько разобрано, сбои, причины, исходы, клетки
    «причина × исход», доля решённых — группы по коротким колонкам ответа
    (`Database.llm_brief`). Доли и порядок — как раньше: причины и исходы
    считаются по ответам без сбоя, пустые в распределение не идут, при
    равном числе записей первой стоит та, что раньше по алфавиту (прежде
    порядок равных зависел от того, какой строкой их отдала база).
    """
    сводка = state.db.llm_brief(tasks.VERSION, since, owner=owner)
    группы = сводка["groups"]
    годные = [г for г in группы if г["ok"]]
    разобрано = sum(int(г["n"]) for г in годные)
    всего_ответов = sum(int(г["n"]) for г in группы)

    def доли(поле: str) -> list[dict[str, Any]]:
        счёт: dict[str, int] = {}
        for г in годные:
            if г[поле]:
                счёт[str(г[поле])] = счёт.get(str(г[поле]), 0) + int(г["n"])
        всего = sum(счёт.values())
        return [{"key": ключ, "records": n, "share": round(100.0 * n / всего, 1)}
                for ключ, n in sorted(счёт.items(), key=lambda пара: (-пара[1], пара[0]))]

    # «Причина × исход» — по всем записям периода, а не по построчному
    # списку: тот обрезан, и клетки считались бы по его части.
    пары: dict[tuple[str, str], int] = {}
    for г in годные:
        ключ = (str(г["reason"] or "—"), str(г["outcome"] or "—"))
        пары[ключ] = пары.get(ключ, 0) + int(г["n"])
    известно = sum(int(г["resolved_known"] or 0) for г in годные)
    решено = sum(int(г["resolved_yes"] or 0) for г in годные)
    задержка = sum(float(г["latency_ms"] or 0) for г in годные)
    return {
        "records": сводка["total"], "analyzed": разобрано,
        "errors": всего_ответов - разобрано,
        "stale": sum(int(г["stale"] or 0) for г in годные),
        "coverage": round(разобрано / сводка["total"], 4) if сводка["total"] else None,
        "avg_latency_ms": round(задержка / разобрано, 1) if разобрано else None,
        "outcomes": доли("outcome"), "reasons": доли("reason"),
        "resolved_share": round(100.0 * решено / известно, 1) if известно else None,
        "pairs": [{"reason": причина, "outcome": исход, "records": n}
                  for (причина, исход), n in sorted(пары.items(),
                                                   key=lambda пара: (-пара[1], пара[0]))],
        "rows_total": всего_ответов,
    }


def _построчно(state: Any, since: float,
               owner: str | list[str] | None) -> list[dict[str, Any]]:
    """Последние разборы окна — строки для таблиц раздела."""
    return [{
        "job_id": р["job_id"], "filename": р.get("filename"),
        "created_at": р.get("created_at"), "summary": р.get("summary") or "",
        "reason": р.get("reason"), "reason_quote": р.get("reason_quote") or "",
        "outcome": р.get("outcome"), "outcome_quote": р.get("outcome_quote") or "",
        "resolved": р.get("resolved"), "actions": р.get("actions") or [],
        "trackers": р.get("trackers") or [], "scorecard": р.get("scorecard") or [],
        "error": р.get("error") or "",
    } for р in state.db.llm_recent(since, owner=owner, limit=СТРОК_В_СВОДЕ)]


def _часть_по_спискам(state: Any, since: float,
                      owner: str | list[str] | None) -> dict[str, Any]:
    """Числа свода, ради которых приходится читать каждый ответ.

    Обязательства, трекеры, скоркарта и «ответ мимо списка» лежат в ответе
    модели строками JSON. Это дорогая часть: на сотнях тысяч разборов —
    секунды, а не доли секунды. Поэтому она отделена от счётной: раздел
    сначала показывает причины и исходы, а эти числа дорисовывает, когда
    они посчитаются.
    """
    строки = state.db.llm_details(since, owner=owner)
    # Трекеры и скоркарта считаются в записях, а не в элементах списка.
    # Разница видна, когда в настройке два трекера (или два пункта) с одним
    # id: у записи тогда два элемента с этим id, и запись считалась дважды
    # — «сработал в 12 разговорах» при шести разговорах в списке под числом.
    трекеры: dict[str, dict[str, Any]] = {}
    скоркарта: dict[str, dict[str, Any]] = {}
    действий = с_действиями = мимо_списка = 0
    свежие: list[dict[str, Any]] = []
    for р in строки:
        проверено: set[str] = set()
        сработало: set[str] = set()
        for т in р.get("trackers") or []:
            ключ = str(т.get("id"))
            трекеры.setdefault(ключ, {"id": ключ, "label": т.get("label"),
                                      "checked": 0, "fired": 0})
            проверено.add(ключ)
            if т.get("fired"):
                сработало.add(ключ)
        for ключ in проверено:
            трекеры[ключ]["checked"] += 1
        for ключ in сработало:
            трекеры[ключ]["fired"] += 1
        ответы: set[tuple[str, str]] = set()
        for о in р.get("scorecard") or []:
            ключ = str(о.get("id"))
            скоркарта.setdefault(ключ, {"id": ключ, "question": о.get("question"),
                                        "да": 0, "нет": 0, "н/п": 0})
            ответы.add((ключ, о.get("answer") if о.get("answer") in ("да", "нет") else "н/п"))
        for ключ, ответ in ответы:
            скоркарта[ключ][ответ] += 1
        действий += len(tasks._список(р.get("actions")))
        if р.get("actions"):
            с_действиями += 1
            # Строки идут от новых к старым: первые и есть самые свежие.
            if len(свежие) < ДЕЙСТВИЙ_В_СВОДЕ:
                свежие.append(р)
        if any("вне списка" in str(з) for з in (р.get("warnings") or [])):
            мимо_списка += 1
    имена = state.db.filenames([р["job_id"] for р in свежие])
    действия: list[dict[str, Any]] = []
    for р in свежие:
        for д in р.get("actions") or []:
            if not isinstance(д, dict):
                д = {"what": str(д)}
            действия.append({**д, "job_id": р["job_id"], "filename": имена.get(р["job_id"]),
                             "created_at": р.get("created_at")})
            if len(действия) >= ДЕЙСТВИЙ_В_СВОДЕ:
                break
        if len(действия) >= ДЕЙСТВИЙ_В_СВОДЕ:
            break
    return {
        "off_list": мимо_списка,
        "actions": действий, "records_with_actions": с_действиями,
        "trackers": sorted(трекеры.values(), key=lambda т: -т["fired"]),
        "scorecard": list(скоркарта.values()),
        "action_items": действия,
    }


@router.get("/content/llm", summary="Свод ответов модели за период")
def llm_report(request: Request, period: str = ПЕРИОД,
               part: str = Query(default="", pattern="^(|fast|heavy)$"),
               since: float | None = Query(default=None, ge=0),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Причины и исходы по закрытым спискам, доля решённых, действия к
    исполнению, срабатывания умных трекеров, ответы скоркарты — по
    записям периода с ответом модели.

    Свод можно взять в две части — так делает раздел «Голосовая
    аналитика», чтобы на большом архиве не ждать всего сразу:

    - `part=fast` — то, что считается без чтения ответов: записей и
      разобрано, сбои, причины, исходы, «причина × исход», доля решённых,
      последние разборы построчно. Доли секунды на любом окне;
    - `part=heavy` — то, что лежит в ответах списками: обязательства,
      трекеры, скоркарта, «ответ мимо списка». Читает каждый ответ окна.

    Без `part` — обе части разом, как раньше. `since` — нижняя граница
    окна вместо отсчёта от «сейчас»: вторую часть спрашивают позже первой,
    и считаться она должна по тому же окну (`window.since` первой части).
    """
    state, клиент, поток = _slot(request)
    секунд = PERIODS.get(period, 7 * 86400)
    начало = float(since) if since is not None else (
        (time.time() - секунд) if секунд else 0.0)
    владелец = scope_owner(principal)
    ответ: dict[str, Any] = {"period": period, "enabled": клиент.enabled, "model": клиент.model}
    if part != "heavy":
        ответ.update(_счётная_часть(state, начало, владелец))
        ответ["rows"] = _построчно(state, начало, владелец)
        ответ["worker"] = поток.status()
    if part != "fast":
        ответ.update(_часть_по_спискам(state, начало, владелец))
    if part:
        ответ["part"] = part
    # Окно свода — в ответ: числа раздела раскрываются в список записей
    # отдельным запросом (`GET /api/drill`) и позже, а «неделя до сейчас»
    # через минуту — уже другая неделя. Нижняя граница одна на весь ответ,
    # верхняя — момент, когда он посчитан. Режется, как и список, временем
    # записи (`j.created_at`), а не временем ответа модели.
    ответ["window"] = {"since": начало or None, "until": time.time()}
    return ответ


# --- установка модели ---------------------------------------------------
#
# Смысловой слой без модели — это выключенный слой, а поставить модель
# руками значит зайти на сервер по ssh и выполнить полдюжины команд.
# Поэтому те же полдюжины команд собраны здесь: каталог с подбором под
# железо, установка, скачивание с процентами и запись настроек.


@router.get("/llm/models", summary="Каталог моделей и подбор под оборудование")
def llm_models(request: Request, refresh: bool = Query(default=False),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что можно поставить, что уже стоит и что поместится в память.

    Пометки считаются по свободной видеопамяти за вычетом запаса под
    распознавание: смысловой слой делит карту с главной работой сервера.
    При `refresh=true` размеры уточняются по реестру Ollama — это
    несколько секунд, поэтому по умолчанию берутся из каталога.
    """
    state = get_state(request)
    require_admin(principal)
    адрес = str(state.settings.get("llm_url") or provision.АДРЕС)
    скачанные = provision.установленные(адрес)
    свод = provision.подобрать(settings=state.settings,
                               installed=[м["name"] for м in скачанные])
    if refresh:
        for строка in свод["models"]:
            размер, ошибка = provision.размер_в_реестре(str(строка["name"]))
            строка["size_gb"] = размер if размер is not None else строка["size_gb"]
            строка["registry"] = ошибка or "ok"
    return {**свод, "installed": скачанные, "service": provision.служба(адрес),
            "backend": str(state.settings.get("llm_backend") or "off"),
            "active": str(state.settings.get("llm_model") or ""),
            "setup": _установщик(request).status()}


@router.post("/llm/setup", summary="Поставить и настроить модель")
def llm_setup(request: Request,
              models: list[str] = Body(default=[], embed=True),
              activate: str = Body(default="", embed=True),
              install_server: bool = Body(default=True, embed=True),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ставит Ollama (если её нет), скачивает модели, прогревает и
    включает выбранную. Идёт в фоне: ход установки — GET
    /api/llm/setup/status.

    Шаги, которые уже сделаны, пропускаются, поэтому повторный запуск на
    настроенном сервере просто докачивает ещё одну модель.
    """
    state = get_state(request)
    require_admin(principal)
    установщик = _установщик(request)
    выбор = [str(м) for м in (models or []) if str(м).strip()]
    if not выбор:
        # Ничего не выбрали — ставим то, что сами и советуем.
        свод = provision.подобрать(settings=state.settings)
        if not свод.get("recommended"):
            raise error_response(ConfigError(
                "Ни одна модель каталога не помещается в память этого сервера.",
                hint="Освободите видеопамять или выберите модель вручную."))
        выбор = [str(свод["recommended"])]
    try:
        return установщик.start(выбор, activate=(activate or None) if activate else выбор[0],
                                install_server=bool(install_server),
                                url=str(state.settings.get("llm_url") or provision.АДРЕС))
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/llm/setup/status", summary="Ход установки модели")
def llm_setup_status(request: Request,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Шаги, проценты и журнал последней установки."""
    require_admin(principal)
    return _установщик(request).status()


@router.post("/llm/setup/cancel", summary="Отменить установку модели")
def llm_setup_cancel(request: Request,
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Останавливает установку на ближайшем шаге. Скачанное остаётся:
    Ollama продолжит с места обрыва при следующем запуске."""
    require_admin(principal)
    return _установщик(request).cancel()


@router.post("/llm/models/delete", summary="Удалить скачанную модель")
def llm_model_delete(request: Request, model: str = Body(..., embed=True),
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Убирает веса с диска. Включённую модель удалить нельзя — сначала
    выберите другую, иначе разбор останется без модели."""
    state = get_state(request)
    require_admin(principal)
    имя = str(model or "").strip()
    if имя and имя == str(state.settings.get("llm_model") or ""):
        raise error_response(ConfigError(
            f"Модель «{имя}» сейчас выбрана для разбора.",
            hint="Сначала выберите другую модель в настройках."))
    адрес = str(state.settings.get("llm_url") or provision.АДРЕС)
    try:
        provision.удалить(имя, адрес)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {"deleted": имя, "installed": provision.установленные(адрес)}
