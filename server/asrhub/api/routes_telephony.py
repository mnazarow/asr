"""Маршруты раздела «АТС»: станции, их состояние, проверка связи, журнал звонков.

Станций может быть сколько угодно, и почти каждая ручка принимает
`station` — идентификатор одной из них. Без него ответ сводный: по всем
станциям сразу.

Разрез по владельцу тот же, что у заданий: обычный ключ видит свои звонки,
ключ в группе — звонки группы, администратор — всё. Настройки станции
(адрес, учётная запись, пути) и всё, что меняет состояние, — только
администратору: адрес АТС и имя учётной записи в manager.conf это
разведка перед атакой на телефонию, а не справочная информация.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request

from ..errors import ASRHubError, ConfigError
from ..telephony import stations as stations_mod
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_write,
    scope_owner,
)

router = APIRouter(prefix="/api/telephony", tags=["Телефония"])

НАПРАВЛЕНИЕ = Query(default="", pattern="^(|входящий|исходящий|внутренний)$")


def _телефония(request: Request) -> Any:
    """Набор станций из состояния приложения."""
    state = get_state(request)
    ввозчик = getattr(state, "telephony", None)
    if ввозчик is None:
        raise error_response(ASRHubError(
            "Телефония не инициализирована.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return ввозчик


def _период(period: str) -> float | None:
    """Начало периода в секундах эпохи; «all» — без ограничения."""
    сколько = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400,
               "quarter": 92 * 86400, "year": 365 * 86400}.get(period)
    return None if сколько is None else time.time() - сколько


def _корзина(period: str) -> str:
    """Шаг линии нагрузки под период: сутки по часам, год по неделям.

    Без этого «за год» превращается в 8760 точек — браузер их нарисует, но
    прочитать такую линию нельзя, а передать 8760 объектов ради картинки
    шириной в 900 точек просто расточительно.
    """
    return {"day": "hour", "week": "hour", "month": "day",
            "quarter": "day", "year": "week", "all": "week"}.get(period, "day")


def _свести_период(разрезы: list[dict[str, Any]]) -> dict[str, Any]:
    """Складывает построчные счётчики станций в один свод за период."""
    итог = dict.fromkeys(("total", "inbound", "outbound", "internal", "answered", "queued", "skipped", "talk_s", "wait_s"), 0)
    for строка in разрезы:
        for ключ in итог:
            итог[ключ] += int(строка.get(ключ) or 0)
    return итог


def _список_станций(state: Any) -> list[dict[str, Any]]:
    """Станции из настроек как есть — списком словарей, пригодным для правки."""
    сырые = state.settings.get("telephony_stations") or []
    if isinstance(сырые, dict):
        сырые = [{**значение, "id": ключ} for ключ, значение in сырые.items()]
    if not isinstance(сырые, list):
        raise error_response(ConfigError(
            "Настройка «Станции» испорчена: ожидается список.",
            hint="Поправьте telephony_stations в разделе «Настройки» "
                 "или сбросьте его в пустой список."))
    готовые = [dict(с) for с in сырые if isinstance(с, dict)]
    # Идентификаторы проставляются здесь же. В настройке, написанной руками
    # или взятой из примера каталога, поля `id` нет: `stations.список()`
    # считает его на лету, интерфейс его показывает, а правка и удаление по
    # нему отвечали «станция не найдена» — раздел показывал станцию, с
    # которой нельзя было ничего сделать. Заодно разводятся совпадающие
    # идентификаторы: иначе удаление одной станции сносило обе.
    занятые: set[str] = set()
    for номер, станция in enumerate(готовые):
        ид = str(станция.get("id") or "").strip()
        if not ид:
            ид = stations_mod._идентификатор(str(станция.get("name") or ""), str(номер + 1))
        основа, счётчик = ид, 2
        while ид in занятые:
            ид = f"{основа}-{счётчик}"
            счётчик += 1
        занятые.add(ид)
        станция["id"] = ид
    if not готовые:
        # Сервер, пришедший с прежней версии, держит одну станцию в старых
        # ключах. Первая же правка через раздел «АТС» переводит её в список
        # — иначе добавление второй станции отменяло бы первую.
        готовые = stations_mod._из_старых_ключей(state.settings)
    return готовые


def _записать_станции(state: Any, станции: list[dict[str, Any]],
                      событие: str) -> None:
    """Проверяет набор целиком и сохраняет его в настройки."""
    ошибки = stations_mod.проверить_набор(станции)
    if ошибки:
        raise error_response(ConfigError(
            "; ".join(ошибки),
            hint="Поправьте поля станции и сохраните ещё раз."))
    state.settings.set("telephony_stations", станции, source="api")
    state.db.add_event(None, событие, f"Станций в настройке: {len(станции)}")
    # Набор сводится сразу, а не на следующем опросе: человек нажал
    # «Сохранить» и ждёт, что станция появится в списке живых — а не через
    # минуту, когда до неё дойдёт фоновый круг.
    telephony = getattr(state, "telephony", None)
    if telephony is not None:
        telephony._свести()


@router.get("/status", summary="Состояние забора записей со всех АТС")
def status(request: Request,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Все станции разом: включена ли каждая, что за источник, что мешает.

    Обычному ключу отдаются счётчики его звонков и признак «работает»;
    адреса, пути и имя учётной записи — только администратору: по ним
    строится вход в телефонию организации, а не понимание своей работы.
    """
    return _телефония(request).status(for_admin=principal.is_admin,
                                      owner=scope_owner(principal))


@router.get("/stations", summary="Список станций")
def stations(request: Request,
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что настроено. Не администратору — без адресов, учётных записей и путей."""
    свод = _телефония(request).status(for_admin=principal.is_admin,
                                      owner=scope_owner(principal))
    return {"items": свод["stations"], "enabled": свод["enabled"],
            "sources": list(stations_mod.ИСТОЧНИКИ)}


@router.post("/test", summary="Проверка связи со станцией")
def test(request: Request, station: str = Query(default="", max_length=64),
         набросок: dict[str, Any] | None = Body(default=None),
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Достучаться до станции и сказать, что именно не так.

    Двумя способами. С `station` — проверяется уже настроенная. С телом
    запроса — станция, которой ещё нет в настройках: это и есть «проверить
    при добавлении», когда человек заполнил форму и хочет знать, доедет ли
    сервер, ДО того как сохранит. Заводить ради проверки станцию и потом
    убирать — способ оставить мусор при первом закрытии вкладки.
    """
    require_admin(principal)
    телефония = _телефония(request)
    try:
        if набросок:
            return телефония.проверить_набросок(набросок)
        if not station:
            raise ConfigError(
                "Не указано, какую станцию проверять.",
                hint="Передайте ?station=<идентификатор> или тело с полями станции.")
        return телефония.проверить(station)
    except ASRHubError as exc:
        # Сюда приходят и сбой связи со станцией (AMIError, 502), и
        # ненайденный журнал (ConfigError, 400) — у каждой свой код и своя
        # подсказка, и подменять их одной общей нельзя.
        raise error_response(exc) from exc
    except OSError as exc:
        raise error_response(ConfigError(
            f"Источник недоступен: {exc}",
            hint="Проверьте путь и права пользователя, от которого работает сервер.")) from exc


@router.post("/scan", summary="Заход за новыми звонками прямо сейчас")
def scan(request: Request, station: str = Query(default="", max_length=64),
         limit: int = Query(default=50, ge=1, le=500),
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Один заход по требованию — чтобы не ждать интервала опроса.

    Без `station` обходятся все включённые станции. Отвечает тем же, чем
    отчитывается фоновый поток: сколько звонков увидели, сколько поставили
    в очередь и по каким причинам пропустили остальные. Причины важнее
    счётчика: «нет записи ×48» и «короткий ×48» — две разные поломки.

    Сбой одной станции не отменяет обход остальных: он попадает в
    `errors` со своим текстом и подсказкой.
    """
    require_admin(require_write(principal))
    try:
        return _телефония(request).scan(station_id=station, limit=limit)
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/overview", summary="Всё для раздела «АТС» одним ответом")
def overview(request: Request,
             period: str = Query(default="week",
                                 pattern="^(day|week|month|quarter|year|all)$"),
             station: str = Query(default="", max_length=64),
             bucket: str = Query(default="", pattern="^(|hour|day|week|month)$"),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Состояние станций, нагрузка по времени, разрезы и длительности сразу.

    Одним ответом, а не восемью: раздел рисует восемь графиков, и восемь
    запросов подряд — это восемь разборов одного и того же отбора в базе и
    восемь поводов увидеть на экране части картины от разных моментов
    времени.
    """
    db = get_state(request).db
    владелец = scope_owner(principal)
    начало = _период(period)
    корзина = bucket or _корзина(period)
    свод = _телефония(request).status(for_admin=principal.is_admin, owner=владелец)
    разрезы = {поле: db.call_tops(поле, owner=владелец, station=station,
                                  since=начало, limit=15)
               for поле in ("queue", "agent", "skipped", "src", "dst",
                            "context", "disposition")}
    по_станциям = db.call_by_station(owner=владелец, since=начало)
    свои = ([с for с in по_станциям if с["station"] == station]
            if station else по_станциям)
    return {
        "period": period, "bucket": корзина, "station": station,
        "since": начало, "now": time.time(),
        "enabled": свод["enabled"], "running": свод["running"],
        "configured": свод["configured"], "stations": свод["stations"],
        "calls": db.call_counts(owner=владелец, station=station),
        "period_calls": _свести_период(свои),
        "by_station": по_станциям,
        "timeline": db.call_timeline(owner=владелец, station=station,
                                     since=начало, bucket=корзина),
        "station_timelines": db.call_timeline_by_station(
            owner=владелец, since=начало, bucket=корзина, points=28),
        "heatmap": db.call_load_heatmap(owner=владелец, station=station,
                                        since=начало),
        "durations": db.call_duration_histogram(owner=владелец, station=station,
                                                since=начало),
        "tops": разрезы,
    }


@router.post("/stations", summary="Добавить или изменить станцию")
def save_station(request: Request, данные: dict[str, Any] = Body(...),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Заводит новую станцию или правит существующую — по полю `id`.

    Настройка пишется целиком: список станций — один параметр, и менять в
    нём одну станцию «на месте» пришлось бы под блокировкой. Проще собрать
    новый список и проверить его целиком: половина ошибок в настройке —
    это не поля одной станции, а её отношения с соседями (одинаковые имена,
    один и тот же журнал на двоих).
    """
    require_admin(require_write(principal))
    state = get_state(request)
    сырые = _список_станций(state)
    правка = dict(данные or {})
    ид = str(правка.get("id") or "").strip()
    if ид:
        номер = next((н for н, с in enumerate(сырые)
                      if str(с.get("id") or "") == ид), None)
        if номер is None:
            raise error_response(ConfigError(
                f"Станция «{ид}» не найдена.",
                hint="Список станций: GET /api/telephony/stations."))
        сырые[номер] = {**сырые[номер], **правка}
    else:
        # Идентификатор новой станции вычисляется из имени один раз и
        # дальше живёт в настройке: к нему привязан архив её звонков.
        правка["id"] = stations_mod._идентификатор(
            str(правка.get("name") or ""), str(len(сырые) + 1))
        занятые = {str(с.get("id") or "") for с in сырые}
        основа, счётчик = правка["id"], 2
        while правка["id"] in занятые:
            правка["id"] = f"{основа}-{счётчик}"
            счётчик += 1
        сырые.append(правка)
    _записать_станции(state, сырые, "station_saved" if ид else "station_added")
    станция = stations_mod.найти(state.settings, str(правка.get("id") or ид))
    return {"station": станция.to_dict(for_admin=True) if станция else {},
            "stations": [с.to_dict(for_admin=True)
                         for с in stations_mod.список(state.settings)]}


@router.delete("/stations/{station_id}", summary="Убрать станцию")
def delete_station(request: Request, station_id: str,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Убирает станцию из настроек. Архив её звонков остаётся.

    Именно остаётся, а не удаляется: разговоры прошлого года — это записи
    организации, а не свойство железки, которую сняли со стойки. Чтобы
    убрать и их, есть срок хранения и «Забыть звонки» в обслуживании.
    """
    require_admin(require_write(principal))
    state = get_state(request)
    сырые = _список_станций(state)
    осталось = [с for с in сырые if str(с.get("id") or "") != station_id]
    if len(осталось) == len(сырые):
        raise error_response(ConfigError(
            f"Станция «{station_id}» не найдена.",
            hint="Список станций: GET /api/telephony/stations."))
    _записать_станции(state, осталось, "station_removed")
    архив = state.db.call_counts(station=station_id)
    return {"removed": station_id, "kept_calls": int(архив.get("total") or 0),
            "stations": [с.to_dict(for_admin=True)
                         for с in stations_mod.список(state.settings)]}


@router.post("/stations/{station_id}/enabled", summary="Включить или выключить станцию")
def toggle_station(request: Request, station_id: str,
                   enabled: bool = Query(default=True),
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Выключенная станция остаётся в настройках, но на АТС не ходит.

    Это не то же самое, что убрать: у выключенной сохраняются и поля, и
    позиция чтения журнала — включив её обратно, сервер продолжит с того
    места, где остановился, а не перечитает архив заново.
    """
    require_admin(require_write(principal))
    state = get_state(request)
    сырые = _список_станций(state)
    номер = next((н for н, с in enumerate(сырые)
                  if str(с.get("id") or "") == station_id), None)
    if номер is None:
        raise error_response(ConfigError(
            f"Станция «{station_id}» не найдена.",
            hint="Список станций: GET /api/telephony/stations."))
    сырые[номер] = {**сырые[номер], "enabled": bool(enabled)}
    _записать_станции(state, сырые,
                      "station_enabled" if enabled else "station_disabled")
    return {"station": station_id, "enabled": bool(enabled)}


@router.get("/calls", summary="Журнал импортированных звонков")
def calls(request: Request, period: str = Query(default="week",
                                                pattern="^(day|week|month|quarter|year|all)$"),
          direction: str = НАПРАВЛЕНИЕ, queue: str = Query(default="", max_length=64),
          agent: str = Query(default="", max_length=64),
          station: str = Query(default="", max_length=64),
          skipped: str = Query(default="", pattern="^(|yes|no|deferred)$"),
          search: str = Query(default="", max_length=64),
          only_queued: bool = Query(default=False),
          limit: int = Query(default=50, ge=1, le=500),
          offset: int = Query(default=0, ge=0),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Список звонков с отбором: направление, очередь, оператор, номер."""
    return get_state(request).db.list_calls(
        owner=scope_owner(principal), direction=direction, queue=queue,
        agent=agent, station=station, skipped=skipped, search=search,
        only_queued=only_queued, since=_период(period), limit=limit, offset=offset)


@router.get("/dimensions", summary="Очереди и операторы для отбора")
def dimensions(request: Request, station: str = Query(default="", max_length=64),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что вообще встречалось в звонках — чтобы отбор был выбором, а не набором."""
    return get_state(request).db.call_dimensions(owner=scope_owner(principal),
                                                 station=station)


__all__ = ["router"]
