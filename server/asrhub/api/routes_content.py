"""Маршруты аналитики записей: /api/content/*.

Отдельная группа, а не расширение /api/analytics, потому что отвечает на
другие вопросы. Аналитика — про сервер: сколько сделано, с какой скоростью,
что падало. Здесь — про разговоры: какими они были и что из этого следует.
Смешивать их в одном адресе значило бы отдавать оба отчёта тому, кому нужен
один, а они оба недешёвые.

Разрез по владельцу тот же, что у аналитики: обычный ключ видит только свои
записи, ключ в группе — записи группы, администратор — всё.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response

from ..errors import ASRHubError, ConfigError, JobNotFound
from ..insights import ОТБОРЫ, ПРИЗНАКИ, РАЗРЕЗЫ, Insights
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_owner,
    require_write,
    scope_owner,
)

router = APIRouter(prefix="/api/content", tags=["Аналитика записей"])

#: Сколько записей можно пересчитать одним запросом. Пересчёт по списку идёт
#: прямо в обработчике, синхронно, — по семь-восемь миллисекунд на запись;
#: пятьсот штук это уже четыре секунды в одном запросе.
ПРЕДЕЛ_ПЕРЕСЧЁТА = 100

#: Периоды разрезов — те же, что в аналитике сервера.
ПЕРИОД = Query(default="week",
               pattern="^(hour|day|week|month|quarter|year|all)$")


def _index(request: Request) -> Any:
    состояние = getattr(get_state(request), "content", None)
    if состояние is None:
        raise error_response(ASRHubError(
            "Разбор содержания записей не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его "
                 "обычным способом."))
    return состояние


def _insights(request: Request) -> Insights:
    """Свод. Заводится на запрос: он не хранит состояния, только ссылки."""
    state = get_state(request)
    return Insights(state.db, _index(request))


@router.get("", summary="Полный отчёт по содержанию записей")
def report(request: Request, period: str = ПЕРИОД,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Всё сразу: свод, разрезы, темы, связи, выводы и что послушать.

    Ручка недешёвая — на архиве в сотню тысяч записей это несколько секунд:
    каждый разрез считается своим запросом. Интерфейс берёт разделы по
    отдельности, а эта нужна выгрузке и сводке по расписанию, где отчёт
    требуется целиком и один раз.
    """
    return _insights(request).report(period, owner=scope_owner(principal))


@router.get("/status", summary="Состояние разбора")
def status(request: Request,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сколько записей разобрано, сколько ждёт, какой версией."""
    return _index(request).status()


@router.get("/summary", summary="Свод по корпусу")
def summary(request: Request, period: str = ПЕРИОД,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    свод = _insights(request)
    начало, прошлое = свод.window(period)
    return {
        "period": period,
        "current": свод.summary(period, owner=scope_owner(principal)),
        "previous": (свод.summary(period, owner=scope_owner(principal),
                                  since=прошлое, until=начало)
                     if начало is not None else None),
        "features": ПРИЗНАКИ,
    }


@router.get("/timeline", summary="Показатели по времени")
def timeline(request: Request, period: str = ПЕРИОД,
             buckets: int = Query(default=24, ge=4, le=200),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return _insights(request).timeline(period, owner=scope_owner(principal),
                                       buckets=buckets)


@router.get("/breakdown/{dimension}", summary="Разрез по признаку")
def breakdown(request: Request, dimension: str, period: str = ПЕРИОД,
              limit: int = Query(default=50, ge=1, le=500),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    if dimension not in РАЗРЕЗЫ:
        raise error_response(ConfigError(
            f"Неизвестный разрез «{dimension}».",
            hint="Доступные разрезы: " + ", ".join(sorted(РАЗРЕЗЫ))))
    return _insights(request).breakdown(dimension, period,
                                        owner=scope_owner(principal), limit=limit)


@router.get("/topics", summary="Темы корпуса")
def topics(request: Request, period: str = Query(default="all",
                                                 pattern="^(hour|day|week|month|"
                                                         "quarter|year|all)$"),
           limit: int = Query(default=40, ge=1, le=200),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    свод = _insights(request)
    владелец = scope_owner(principal)
    окно = period if period != "all" else "month"
    return {**свод.topics(period, владелец, limit=limit),
            "trend": свод.topic_trend(окно, владелец),
            "new": свод.new_topics(окно, владелец)}


@router.get("/correlations", summary="Связи между признаками")
def correlations(request: Request, period: str = ПЕРИОД,
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return _insights(request).correlations(period, owner=scope_owner(principal))


@router.get("/findings", summary="Готовые выводы")
def findings(request: Request, period: str = ПЕРИОД,
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return {"items": _insights(request).findings(
        period, owner=scope_owner(principal))}


@router.get("/records", summary="Записи, которые стоит послушать")
def records(request: Request, kind: str = Query(default="negative"),
            period: str = ПЕРИОД,
            limit: int = Query(default=20, ge=1, le=200),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    if kind not in ОТБОРЫ:
        raise error_response(ConfigError(
            f"Неизвестный отбор «{kind}».",
            hint="Доступные отборы: " + ", ".join(sorted(ОТБОРЫ))))
    return _insights(request).records(kind, period, owner=scope_owner(principal),
                                      limit=limit)


@router.get("/kinds", summary="Перечень отборов и разрезов")
def kinds(request: Request,
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что раздел умеет показывать — чтобы интерфейс не держал копию списка.

    Здесь же набор скрипта по умолчанию. Он нужен редактору: без него
    человек, у которого своего скрипта ещё нет, видел пустой список и не
    мог понять, что же сервер проверяет сейчас. Начинать правку с восьми
    готовых пунктов правильнее, чем с чистого листа: скрипт у каждого свой,
    но начинается он обычно не с нуля.
    """
    from ..content import categories as категории  # noqa: PLC0415
    from ..content.compliance import ПО_УМОЛЧАНИЮ  # noqa: PLC0415

    индекс = _index(request)
    return {
        "kinds": [{"key": к, "title": о["title"]} for к, о in ОТБОРЫ.items()],
        "dimensions": [{"key": к, "title": о["title"]}
                       for к, о in РАЗРЕЗЫ.items()],
        "features": ПРИЗНАКИ,
        "default_script": ПО_УМОЛЧАНИЮ,
        # Категории — действующий набор (свой или готовый): по нему список
        # заданий строит отбор «про оплату», а редактор — заготовки.
        "categories": [к.to_dict() for к in индекс.categories()],
        "categories_own": индекс.categories_own(),
        "default_categories": категории.ГОТОВЫЕ,
        "category_kinds": категории.ВИДЫ,
        "category_who": категории.КТО,
        "category_where": категории.ГДЕ,
    }


@router.get("/categories", summary="Категории обращений: счёт и динамика")
def categories_report(request: Request, period: str = ПЕРИОД,
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сколько записей в каждой категории, доля, изменение к прошлому
    окну, что растёт и угасает, сколько записей без категории вовсе.

    Категории считаются по правилам из настройки `content_categories`
    (пустая — готовый набор). Правило пишется строкой: «оплата ИЛИ платёж»,
    «возврат И НЕ брак», «дорого РЯДОМ(5) конкурент»; у категории есть
    фильтр «кто сказал» и окно «где в разговоре».
    """
    return _insights(request).categories(period, owner=scope_owner(principal))


@router.get("/drivers", summary="Драйверы негатива")
def drivers(request: Request, period: str = ПЕРИОД,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Какие категории чаще среднего встречаются в отрицательных разговорах.

    Подъём (lift) — частота категории среди отрицательных разговоров к её
    частоте вообще; в список попадают категории с подъёмом от 1,25 на
    десяти записях и больше. Связь, а не причина: тема может быть и
    следствием плохого разговора.
    """
    return _insights(request).drivers(period, owner=scope_owner(principal))


#: Разрез оператора: метка говорящего или владелец задания.
ПО_КОМУ = Query(default="speaker", pattern="^(speaker|owner)$")


@router.get("/norms", summary="Нормы от своего архива")
def norms(request: Request, period: str = ПЕРИОД, by: str = ПО_КОМУ,
          agent: str = Query(default=""),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Обычная величина каждого показателя — медиана и межквартильный размах
    по четырём неделям до периода — и где относительно неё медиана периода.
    С `agent` — норма и период по одному оператору."""
    return _insights(request).norms(period, owner=scope_owner(principal),
                                    agent=(by, agent) if agent else None)


@router.get("/control", summary="Контрольные карты по дням")
def control(request: Request, period: str = ПЕРИОД, by: str = ПО_КОМУ,
            agent: str = Query(default=""),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Доля отрицательных, балл оператора, тональность и скрипт по дням с
    пределами 2σ и 3σ по четырём неделям до периода; отметки за пределами
    и серии по одну сторону от среднего."""
    return _insights(request).control(period, owner=scope_owner(principal),
                                      agent=(by, agent) if agent else None)


@router.get("/agents", summary="Операторы: балл, эмпатия, нарушения")
def agents(request: Request, period: str = ПЕРИОД, by: str = ПО_КОМУ,
           limit: int = Query(default=100, ge=1, le=500),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Список операторов с показателями — разрез по говорящему или по
    владельцу задания (ключу доступа); строка ведёт в карточку."""
    return _insights(request).agents(by, period, owner=scope_owner(principal), limit=limit)


@router.get("/agents/{key}", summary="Карточка оператора")
def agent_card(request: Request, key: str, period: str = ПЕРИОД, by: str = ПО_КОМУ,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Все показатели оператора против команды, ход по неделям, нарушения по
    категориям, лучшие и худшие записи, очередь коучинга.

    `by=speaker` — оператор по метке говорящего в разборе («кто заговорил
    первым»); `by=owner` — по владельцу задания, точнее там, где у каждого
    сотрудника свой ключ доступа.
    """
    return _insights(request).agent_card(key, by=by, period=period,
                                         owner=scope_owner(principal))


@router.get("/coaching", summary="Очередь коучинга")
def coaching(request: Request, period: str = ПЕРИОД,
             by: str = ПО_КОМУ, agent: str = Query(default=""),
             limit: int = Query(default=50, ge=1, le=500),
             done: bool = Query(default=False),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Записи, которые стоит разобрать с оператором, с причиной: нарушение,
    низкий балл, скрипт меньше половины, долгий монолог, невежливость,
    возражение без отработки, раздражённый клиент. Разобранные скрыты,
    пока не попросят `done=true`."""
    return _insights(request).coaching(
        period, owner=scope_owner(principal),
        agent=(by, agent) if agent else None, limit=limit, include_done=done)


@router.get("/references", summary="Эталонные разговоры")
def references(request: Request, period: str = ПЕРИОД,
               limit: int = Query(default=20, ge=1, le=200),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Лучшие разговоры периода по баллу и тональности без нарушений — и всё,
    что отмечено эталоном руками, независимо от периода."""
    return _insights(request).references(period, owner=scope_owner(principal), limit=limit)


@router.put("/marks/{job_id}", summary="Отметить запись: разобрано, эталон")
def set_mark(request: Request, job_id: str, body: dict[str, Any] = Body(default={}),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Отметка руководителя на записи: `kind` — `coaching` (статус `done`
    или `open`) или `reference` (статус `yes`); пустой статус снимает
    отметку. Отметки переживают пересчёт разбора: они лежат отдельно."""
    require_write(principal)
    state = get_state(request)
    задание = state.db.get_job(job_id)
    if not задание:
        raise error_response(JobNotFound(job_id))
    require_owner(principal, задание)
    вид = str(body.get("kind") or "")
    статус = str(body.get("status") or "")
    допустимые = {"coaching": {"", "open", "done"}, "reference": {"", "yes"}}
    if вид not in допустимые or статус not in допустимые[вид]:
        raise error_response(ConfigError(
            "Поле kind — coaching или reference; статус — done/open или yes; "
            "пустой статус снимает отметку."))
    state.db.set_mark(job_id, вид, статус, str(body.get("note") or "")[:500])
    state.db.add_event(job_id, "mark",
                       f"Отметка «{вид}»: {статус or 'снята'}",
                       {"kind": вид, "status": статус, "by": principal.name})
    return {"job_id": job_id, "kind": вид, "status": статус or None,
            "marks": state.db.get_marks([job_id]).get(job_id, {})}


@router.post("/categories/check", summary="Проверить набор категорий на записи")
def categories_check(request: Request, body: dict[str, Any] = Body(default={}),
                     principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Прогоняет категории по выбранной записи, ничего не сохраняя.

    Ради этого редактор и существует: понять по правилу, годится ли оно,
    нельзя — «оплата ИЛИ это» выглядит безобидно и покрывает весь архив.
    Видно это только на настоящей записи: что совпало, в какой реплике, на
    какой секунде. Ошибки разбора правил возвращаются по каждой категории
    отдельно, с позицией в строке.

    Набор приходит в теле и в базу не попадает: это черновик. Без набора в
    теле проверяется действующий.
    """
    from ..content import categories as категории  # noqa: PLC0415
    from ..content import compliance  # noqa: PLC0415

    state = get_state(request)
    job_id = str(body.get("job_id") or "")
    задание = state.db.get_job(job_id) if job_id else None
    if not задание:
        raise error_response(JobNotFound(job_id))
    require_owner(principal, задание)
    набор = body.get("categories")
    if набор is not None and not isinstance(набор, list):
        raise error_response(ConfigError("Поле categories должно быть списком."))
    индекс = _index(request)
    сегменты = state.db.get_segments(job_id)
    if not сегменты and задание.get("text"):
        сегменты = [{"start": 0.0, "end": float(задание.get("media_duration_s") or 0.0),
                     "text": задание["text"]}]
    оператор = str(state.settings.get("content_agent_speaker") or "").strip() or None
    # Стороны — те же, что у разбора записи: оператор по скрипту (первый
    # заговоривший), клиент — самый говорливый из остальных.
    from ..content.analyze import _клиент  # noqa: PLC0415

    кто = compliance.agent(сегменты, оператор)
    клиент = _клиент(сегменты, кто)
    проверяемые = набор if набор is not None else индекс.categories()
    итог = категории.apply(сегменты, проверяемые, agent=кто, customer=клиент,
                           everything=True)
    частоты, корпус = индекс.corpus_frequency()
    return {
        "job_id": job_id,
        "filename": задание.get("filename"),
        "agent": кто, "customer": клиент,
        "result": итог,
        "suspicious": категории.suspicious(проверяемые, частоты, корпус),
        "errors": категории.validate(набор) if набор is not None else [],
        "default": набор is None and not индекс.categories_own(),
    }


@router.get("/jobs/{job_id}", summary="Разбор одной записи")
def job_content(request: Request, job_id: str,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Карточка разбора: тональность по ходу разговора, речь, темы, скрипт.

    Если разбора ещё нет, он считается тут же и складывается в базу: ждать
    фонового прохода по всему архиву ради одной открытой записи незачем.
    """
    state = get_state(request)
    задание = state.db.get_job(job_id)
    if not задание:
        raise error_response(JobNotFound(job_id))
    require_owner(principal, задание)
    готовое = state.db.get_content(job_id)
    from .. import content as разбор_модуль

    if готовое and int(готовое.get("version") or 0) >= разбор_модуль.VERSION:
        разбор = готовое.get("detail") or {}
        посчитан = готовое.get("computed_at")
    else:
        # Считаем на месте, но кладём в базу только если разбор вообще
        # включён и ключ имеет право писать. Обе оговорки не теоретические:
        # без первой настройка «не разбирать содержание» ничего не
        # выключала — карточка складывала разбор и словарь слов в базу
        # мимо неё; без второй запись в три таблицы делал ключ, выданный
        # только на чтение, причём обычным GET, который может дёрнуть и
        # предзагрузка ссылок в браузере.
        индекс = _index(request)
        сохранять = индекс.enabled and principal.can_write
        разбор = индекс.analyze_job(job_id, job=задание, save=сохранять) or {}
        посчитан = time.time() if сохранять else None
    return {"job_id": job_id, "version": разбор_модуль.VERSION,
            "computed_at": посчитан, "analysis": разбор,
            "filename": задание.get("filename"),
            "created_at": задание.get("created_at"),
            "duration_s": задание.get("media_duration_s")}


@router.post("/jobs/{job_id}/recompute", summary="Пересчитать разбор записи")
def recompute_job(request: Request, job_id: str,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_write(principal)
    state = get_state(request)
    задание = state.db.get_job(job_id)
    if not задание:
        raise error_response(JobNotFound(job_id))
    require_owner(principal, задание)
    разбор = _index(request).analyze_job(job_id, job=задание)
    return {"recomputed": разбор is not None, "job_id": job_id}


@router.post("/script/check", summary="Проверить скрипт на одной записи")
def script_check(request: Request, body: dict[str, Any] = Body(default={}),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Прогоняет скрипт по выбранной записи, ничего не сохраняя.

    Ради этой ручки редактор скрипта и сделан отдельно от общего списка
    настроек. Понять по списку слов, годится ли примета, нельзя: «это» и
    «то есть» выглядят безобидно и делают пункт выполненным всегда. Видно
    это только на настоящей записи — в столбце «что нашли».

    Скрипт приходит в теле и в базу не попадает: это черновик, который
    человек ещё правит. Сохранение — обычная запись настройки.
    """
    state = get_state(request)
    job_id = str(body.get("job_id") or "")
    задание = state.db.get_job(job_id) if job_id else None
    if not задание:
        raise error_response(JobNotFound(job_id))
    require_owner(principal, задание)

    скрипт = body.get("script")
    if скрипт is not None and not isinstance(скрипт, list):
        raise error_response(ConfigError(
            "Поле script должно быть списком пунктов."))
    if скрипт is None:
        значение = state.settings.get("content_script")
        скрипт = значение if isinstance(значение, list) and значение else None

    from ..content import compliance  # noqa: PLC0415

    сегменты = state.db.get_segments(job_id)
    if not сегменты and задание.get("text"):
        сегменты = [{"start": 0.0, "end": float(задание.get("media_duration_s") or 0.0),
                     "text": задание["text"]}]
    оператор = str(state.settings.get("content_agent_speaker") or "").strip() or None
    итог = compliance.check(сегменты, script=скрипт, speaker=оператор)
    частоты, корпус = _index(request).corpus_frequency()
    return {
        "job_id": job_id,
        "filename": задание.get("filename"),
        "compliance": итог,
        "suspicious": compliance.suspicious(
            скрипт if скрипт is not None else compliance.ПО_УМОЛЧАНИЮ,
            частоты, корпус),
        "default": скрипт is None,
    }


@router.post("/recompute", summary="Пересчитать разбор архива")
def recompute(request: Request, body: dict[str, Any] = Body(default={}),
              principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Пересчёт по требованию — после смены словарей или скрипта.

    Без списка заданий помечает к пересчёту весь архив; сам пересчёт идёт в
    фоне порциями. Держать HTTP-запрос полчаса, пока считается сто тысяч
    записей, нельзя: он оборвётся по тайм-ауту, оставив работу наполовину
    сделанной, а повторить его будет нечем.

    Пересчёт всего архива — дело администратора: он занимает служебный поток
    на часы и меняет числа во всех отчётах сервера, включая чужие.
    """
    require_write(principal)
    задания = body.get("job_ids") or body.get("jobs")
    if задания is not None:
        # Именно `is not None`, а не проверка на непустоту. С проверкой на
        # непустоту список из одного несуществующего задания отсеивался
        # фильтром ниже, `разрешённые` оказывался пустым, и `recompute([])`
        # уходил в ветку «весь архив» — то есть любой ключ одним запросом
        # сбрасывал разбор всех записей сервера, включая чужие, а
        # `require_admin` строкой ниже не выполнялся никогда.
        if not isinstance(задания, list):
            raise error_response(ConfigError(
                "Поле job_ids должно быть списком идентификаторов."))
        state = get_state(request)
        разрешённые = []
        for job_id in [str(j) for j in задания][:ПРЕДЕЛ_ПЕРЕСЧЁТА]:
            задание = state.db.get_job(job_id)
            if задание:
                require_owner(principal, задание)
                разрешённые.append(job_id)
        # Пустой список после отсева — это «нечего пересчитывать», а не
        # «пересчитать всё».
        return {"recomputed": 0, "queued": 0} if not разрешённые \
            else _index(request).recompute(разрешённые)
    require_admin(principal)
    return _index(request).recompute()


@router.get("/export", summary="Выгрузка отчёта в таблицу")
def export(request: Request,
           period: str = Query(default="month",
                               pattern="^(hour|day|week|month|quarter|year|all)$"),
           fmt: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
           principal: Principal = Depends(authenticate)) -> Any:
    """Тот же отчёт, что на экране, — книгой Excel или архивом CSV."""
    from ..content_export import to_csv_zip, to_xlsx
    from .routes_jobs import content_disposition

    отчёт = _insights(request).report(period, owner=scope_owner(principal))
    метка = time.strftime("%Y-%m-%d")
    if fmt == "csv":
        тело = to_csv_zip(отчёт, period)
        имя, тип = f"asrhub-записи-{period}-{метка}.zip", "application/zip"
    else:
        try:
            тело = to_xlsx(отчёт, period)
        except ASRHubError as exc:
            raise error_response(exc) from exc
        имя = f"asrhub-записи-{period}-{метка}.xlsx"
        тип = ("application/vnd.openxmlformats-officedocument."
               "spreadsheetml.sheet")
    return Response(content=тело, media_type=тип, headers={
        "Content-Disposition": content_disposition(имя),
        # Отчёт считается на момент запроса: закешированная выгрузка — это
        # вчерашние числа под сегодняшним именем.
        "Cache-Control": "no-store",
    })
