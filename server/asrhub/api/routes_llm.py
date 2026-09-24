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
    свод = state.db.llm_stats(tasks.VERSION, since, owner=scope_owner(principal))
    разобрано = [р for р in свод["rows"] if not р.get("error") and р.get("version") == tasks.VERSION]
    return {
        **клиент.status(),
        "worker": поток.status(),
        "version": tasks.VERSION,
        "coverage": {"total": свод["total"], "analyzed": len(разобрано),
                     "errors": sum(1 for р in свод["rows"] if р.get("error")),
                     "share": round(len(разобрано) / свод["total"], 4) if свод["total"] else None},
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
    с_какого = time.time() - hours * 3600
    # Счётчики, сводка и график — по своим записям, как и список: иначе
    # «список пуст, ждут четыре» рассказывало обычному ключу о чужой работе.
    свой = scope_owner(principal)
    корзин, ряд = state.db.llmq_series(с_какого, time.time(), КОРЗИН, owner=свой)
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
        "stats": state.db.llmq_stats(с_какого, owner=свой),
        "series": {"buckets": корзин, "since": с_какого, "rows": ряд},
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
    state.db.add_event(None, "llm_queue",
                       "Очередь разбора приостановлена" if paused else "Очередь разбора продолжена")
    return {"paused": bool(paused), "worker": поток.status()}


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
                                                   owner=scope_owner(principal))
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


@router.get("/content/llm", summary="Свод ответов модели за период")
def llm_report(request: Request, period: str = ПЕРИОД,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Причины и исходы по закрытым спискам, доля решённых, действия к
    исполнению, срабатывания умных трекеров, ответы скоркарты — по
    записям периода с ответом модели."""
    state, клиент, поток = _slot(request)
    секунд = PERIODS.get(period, 7 * 86400)
    since = (time.time() - секунд) if секунд else 0.0
    свод = state.db.llm_stats(tasks.VERSION, since, owner=scope_owner(principal))
    строки = [р for р in свод["rows"] if not р.get("error")]
    распределения = tasks.summarize_for_digest(строки)
    трекеры: dict[str, dict[str, Any]] = {}
    for р in строки:
        for т in р.get("trackers") or []:
            запись = трекеры.setdefault(str(т.get("id")), {"id": str(т.get("id")),
                                                            "label": т.get("label"),
                                                            "checked": 0, "fired": 0})
            запись["checked"] += 1
            запись["fired"] += 1 if т.get("fired") else 0
    скоркарта: dict[str, dict[str, Any]] = {}
    for р in строки:
        for о in р.get("scorecard") or []:
            запись = скоркарта.setdefault(str(о.get("id")), {"id": str(о.get("id")),
                                                             "question": о.get("question"),
                                                             "да": 0, "нет": 0, "н/п": 0})
            запись[о.get("answer") if о.get("answer") in ("да", "нет", "н/п") else "н/п"] += 1
    свежие = sorted(свод["rows"], key=lambda x: -float(x.get("created_at") or 0))
    действия = []
    for р in свежие:
        if р.get("error"):
            continue
        for д in р.get("actions") or []:
            if not isinstance(д, dict):
                д = {"what": str(д)}
            действия.append({**д, "job_id": р["job_id"], "filename": р.get("filename"),
                             "created_at": р.get("created_at")})
            if len(действия) >= ДЕЙСТВИЙ_В_СВОДЕ:
                break
        if len(действия) >= ДЕЙСТВИЙ_В_СВОДЕ:
            break
    # «Причина × исход» — по всем записям периода, а не по построчному
    # списку ниже: тот обрезан, и клетки считались бы по его части.
    пары: dict[tuple[str, str], int] = {}
    for р in строки:
        ключ = (str(р.get("reason") or "—"), str(р.get("outcome") or "—"))
        пары[ключ] = пары.get(ключ, 0) + 1
    # Построчный список для раздела «Голосовая аналитика»: он рисовал
    # таблицы и цитаты по полю `rows`, которого в ответе не было вовсе, и
    # показывал «модель ничего не разобрала» при разобранном архиве.
    построчно = [{
        "job_id": р["job_id"], "filename": р.get("filename"),
        "created_at": р.get("created_at"), "summary": р.get("summary") or "",
        "reason": р.get("reason"), "reason_quote": р.get("reason_quote") or "",
        "outcome": р.get("outcome"), "outcome_quote": р.get("outcome_quote") or "",
        "resolved": р.get("resolved"), "actions": р.get("actions") or [],
        "trackers": р.get("trackers") or [], "scorecard": р.get("scorecard") or [],
        "error": р.get("error") or "",
    } for р in свежие[:СТРОК_В_СВОДЕ]]
    return {
        "period": period, "enabled": клиент.enabled, "model": клиент.model,
        "records": свод["total"], "analyzed": len(строки),
        "errors": sum(1 for р in свод["rows"] if р.get("error")),
        "stale": sum(1 for р in строки if р.get("version") != tasks.VERSION),
        "coverage": round(len(строки) / свод["total"], 4) if свод["total"] else None,
        "avg_latency_ms": round(sum(float(р.get("latency_ms") or 0) for р in строки)
                                / len(строки), 1) if строки else None,
        "off_list": sum(1 for р in строки
                        if any("вне списка" in str(з) for з in (р.get("warnings") or []))),
        **распределения,
        "trackers": sorted(трекеры.values(), key=lambda т: -т["fired"]),
        "scorecard": list(скоркарта.values()),
        "action_items": действия,
        "pairs": [{"reason": п, "outcome": и, "records": n}
                  for (п, и), n in sorted(пары.items(), key=lambda kv: -kv[1])],
        "rows": построчно,
        "rows_total": len(свод["rows"]),
        "worker": поток.status(),
    }


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
