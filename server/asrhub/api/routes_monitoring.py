"""Маршруты мониторинга: /api/monitoring/*.

Отдельная группа, а не расширение /api/metrics, потому что у неё другой режим
доступа: систему мониторинга обычно пускают без ключа с адресов сети сбора,
тогда как остальной интерфейс закрыт. Разделение позволяет настроить это на
прокси одним правилом по префиксу пути.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..errors import (
    ASRHubError,
    AuthError,
    ConfigError,
    ForbiddenError,
    MetricNotFound,
    MetricsDisabled,
)
from ..monitoring import METRICS, MetricSpec, exporters, probes
from ..monitoring import catalog as metric_catalog
from ..monitoring.alerts import Rule
from ..monitoring.pushers import KINDS, Target
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    token_of,
)

router = APIRouter(prefix="/api/monitoring", tags=["Мониторинг"])


def _monitoring(request: Request) -> Any:
    state = get_state(request)
    service = getattr(state, "monitoring", None)
    if service is None:
        raise error_response(ASRHubError(
            "Мониторинг не инициализирован.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return service


def _open_access(request: Request) -> bool:
    """Разрешён ли доступ к метрикам без ключа."""
    state = get_state(request)
    return bool(state.settings.get("monitoring_public", True))


def _guard(request: Request) -> None:
    """Пропускает без ключа, если это разрешено настройкой.

    Вызывать authenticate() напрямую нельзя: её параметры объявлены через
    Header(), и при обычном вызове туда попадут не заголовки, а объекты
    FastAPI. Поэтому заголовки читаем сами.
    """
    if _open_access(request):
        return
    state = get_state(request)
    token = token_of(request)
    info = state.settings.api_keys.get(token)
    if not info:
        raise error_response(AuthError("Ключ доступа отсутствует или недействителен."))
    if info.get("enabled") is False:
        raise error_response(ForbiddenError("Ключ доступа отключён."))


def _экспорт_включён(request: Request) -> None:
    """Экспорт опросом выключен настройкой — отвечаем 404, как /api/metrics.

    `metrics_enabled: false` закрывал только прежний адрес /api/metrics, а
    /api/monitoring/metrics продолжал отдавать всё; самопроверка при этом
    писала «Экспорт метрик: выключен». Теперь настройка значит то, что
    написано: опросом метрики не отдаются ни по одному адресу. Отправка в
    приёмники и встроенные тревоги от неё не зависят.
    """
    if not get_state(request).settings.get("metrics_enabled", True):
        raise error_response(MetricsDisabled("Экспорт метрик выключен настройкой "
                                             "metrics_enabled."))


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

@router.get("/metrics", summary="Метрики во всех поддерживаемых форматах",
            response_class=PlainTextResponse)
def metrics(request: Request,
            format: str = Query(default="prometheus",
                                description="prometheus, openmetrics, json, otlp, "
                                            "influx, graphite, zabbix, zabbix_sender, csv"),
            host: str = Query(default="asrhub", description="Имя узла для Zabbix"),
            ) -> Response:
    """Полный снимок всех параметров работы сервиса.

    Формат выбирается параметром `format`. По умолчанию — текстовый формат
    Prometheus, его же ждёт большинство систем сбора.
    """
    _guard(request)
    _экспорт_включён(request)
    service = _monitoring(request)
    try:
        body, content_type = service.render(format, host=host)
    except ValueError as exc:
        raise error_response(ConfigError(str(exc))) from exc
    return Response(content=body, media_type=content_type)


@router.get("/metrics.json", summary="Снимок в JSON с описанием каждой метрики")
def metrics_json(request: Request,
                 group: str | None = Query(default=None, description="Только одна группа"),
                 ) -> Any:
    """То же, что и метрики, но с описаниями, рекомендациями и порогами.

    Формат для систем, которые не понимают Prometheus, и для случая, когда
    получателю нужно не только число, но и то, что оно означает.
    """
    _guard(request)
    _экспорт_включён(request)
    service = _monitoring(request)
    samples, errors = service.samples()
    # Ошибки сбора — это тексты исключений, а в них абсолютные пути: путь к
    # базе и каталог данных. Маршрут по умолчанию открыт без ключа вовсе
    # (monitoring_public), то есть раскладка файловой системы уезжала
    # анониму — ровно та разведка, которую прячут GET /api/system и
    # GET /api/settings.
    payload = exporters.json_snapshot(samples, _без_путей(request, errors))
    if group:
        payload["metrics"] = [m for m in payload["metrics"] if m.get("group") == group]
    return payload


# ---------------------------------------------------------------------------
# Справочник метрик
# ---------------------------------------------------------------------------

def _spec_dict(spec: MetricSpec) -> dict[str, Any]:
    data = spec.to_dict()
    data["group_title"] = metric_catalog.GROUPS_BY_ID[spec.group]["title"]
    return data


@router.get("/catalog", summary="Справочник метрик: описания, пороги, рекомендации")
def catalog(request: Request,
            group: str | None = None,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Каталог всех метрик с описанием каждой.

    Тот же источник, из которого собраны раздел документации о мониторинге,
    правила Prometheus и шаблон Zabbix.
    """
    items = [s for s in METRICS if not group or s.group == group]
    return {
        "groups": metric_catalog.GROUPS,
        "metrics": [_spec_dict(s) for s in items],
        "stats": metric_catalog.stats(),
    }


@router.get("/catalog/{name}", summary="Описание одной метрики")
def catalog_item(name: str, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    spec = metric_catalog.METRICS_BY_NAME.get(name)
    if spec is None:
        similar = [s.name for s in METRICS if name.lower() in s.name.lower()]
        raise error_response(MetricNotFound(name, similar))
    return _spec_dict(spec)


# ---------------------------------------------------------------------------
# Пробы состояния
# ---------------------------------------------------------------------------

@router.get("/health", summary="Сводное состояние сервиса")
def health(request: Request) -> Any:
    """Одним запросом: живость, готовность, запуск и сработавшие тревоги."""
    _guard(request)
    service = _monitoring(request)
    result = service.health()
    for проба in ("liveness", "readiness", "startup"):
        if isinstance(result.get(проба), dict):
            result[проба] = _проба_без_путей(request, result[проба])
    code = {"ok": 200, "warning": 200, "degraded": 503, "critical": 503}[result["status"]]
    return JSONResponse(result, status_code=code)


def _проба_без_путей(request: Request, result: dict[str, Any]) -> dict[str, Any]:
    """Пробы открыты без ключа — тексты исключений в них без путей.

    Оркестратору ключ не нужен, и это правильно, но проба базы отдавала
    текст исключения целиком: «unable to open database file» вместе с
    путём к каталогу данных уходил анониму — то, что `/metrics.json` уже
    прячет.
    """
    проверки = [dict(п) for п in result.get("checks") or []]
    тексты = _без_путей(request, [str(п.get("detail") or "") for п in проверки])
    for проверка, текст in zip(проверки, тексты, strict=False):
        проверка["detail"] = текст
    return {**result, "checks": проверки}


@router.get("/live", summary="Проба живости")
def live(request: Request) -> Any:
    """Для оркестратора: провал означает «перезапусти контейнер»."""
    result = _проба_без_путей(request, probes.liveness(get_state(request)))
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


@router.get("/ready", summary="Проба готовности")
def ready(request: Request) -> Any:
    """Для балансировщика: провал означает «не шли сюда запросы»."""
    result = _проба_без_путей(request, probes.readiness(get_state(request)))
    return JSONResponse(result, status_code=503 if result["status"] == "fail" else 200)


@router.get("/startup", summary="Проба завершения запуска")
def startup_probe(request: Request) -> Any:
    """Пока не пройдена, остальные пробы учитывать не следует."""
    result = _проба_без_путей(request, probes.startup(get_state(request)))
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


# ---------------------------------------------------------------------------
# Ряды нагрузки
# ---------------------------------------------------------------------------

def _ряд(корзины: list[dict[str, Any]], всего: int,
         column: str) -> list[float | None]:
    """Раскладывает свёрнутые корзины в ряд фиксированной длины.

    SQL отдаёт только непустые корзины, а графику нужен ряд ровно по числу
    точек: пропуски — это перерывы в сборе, и они должны остаться дырками, а
    не сжаться в ровный участок. Значение дырки — None: график рвёт линию,
    вместо того чтобы соединять края перерыва прямой.
    """
    ряд: list[float | None] = [None] * всего
    for row in корзины:
        i = int(row.get("bucket") or 0)
        if not 0 <= i < всего:
            continue
        значение = row.get(column)
        if значение is not None:
            ряд[i] = round(float(значение), 3)
    return ряд


def _скаляр(корзины: list[dict[str, Any]], column: str) -> float | None:
    """Постоянная величина окна — объём памяти, лимит мощности.

    Берём наибольшее непустое: у карты объём не меняется, но отдельные
    замеры приходят без него, когда датчик молчит.
    """
    значения = [float(r[column]) for r in корзины if r.get(column) is not None]
    return max(значения) if значения else None


@router.get("/resources", summary="Ряды нагрузки: сервер и видеокарты")
def resources(request: Request, minutes: int = Query(default=60, ge=1, le=10080),
              points: int = Query(default=180, ge=10, le=2000)) -> Any:
    """Ряды по времени для графиков нагрузки.

    Раздел мониторинга был целиком табличным: пробы, тревоги, приёмники,
    справочник. По таблице видно текущее значение и не видно ничего из того,
    ради чего мониторинг заводят, — растёт ли нагрузка, упирается ли карта в
    лимит мощности, совпадает ли провал скорости с ростом очереди.

    Свёртка идёт в SQL: цена запроса определяется числом точек на графике, а
    не шириной окна. Раньше неделя замеров означала тридцать тысяч строк в
    память на каждый опрос панели — при открытом по умолчанию доступе к
    мониторингу этого хватало, чтобы держать базу занятой одним лишь
    обновлением графика.

    Замеры по картам отдаются по каждой отдельно: у сервера их может быть
    несколько, и «средняя загрузка видеокарты» — величина, из которой не
    следует ничего.
    """
    _guard(request)
    state = get_state(request)
    сейчас = time.time()
    since = сейчас - minutes * 60

    корзин, свёрнутые = state.db.system_series(since, сейчас, points)
    шаг = (сейчас - since) / корзин
    ряды = {
        "ts": [round(since + i * шаг) for i in range(корзин)],
        "cpu_percent": _ряд(свёрнутые, корзин, "cpu_percent"),
        "ram_used_mb": _ряд(свёрнутые, корзин, "ram_used_mb"),
        "ram_total_mb": _скаляр(свёрнутые, "ram_total_mb"),
        "disk_free_gb": _ряд(свёрнутые, корзин, "disk_free_gb"),
        # Очередь и занятость — по пику: полминуты с очередью из сорока
        # заданий важнее, чем средняя единица за полчаса вокруг них.
        "queue_depth": _ряд(свёрнутые, корзин, "queue_depth"),
        "active_jobs": _ряд(свёрнутые, корзин, "active_jobs"),
    }

    по_картам: dict[int, list[dict[str, Any]]] = {}
    _, свёрнутые_карты = state.db.gpu_series(since, сейчас, points)
    for row in свёрнутые_карты:
        по_картам.setdefault(int(row.get("gpu") or 0), []).append(row)

    карты = []
    for индекс in sorted(по_картам):
        строки = по_картам[индекс]
        имя = next((str(r["name"]) for r in строки if r.get("name")), "")
        карты.append({
            "gpu": индекс,
            "name": имя or f"GPU {индекс}",
            "ts": ряды["ts"],
            "util_percent": _ряд(строки, корзин, "util_percent"),
            "mem_used_mb": _ряд(строки, корзин, "mem_used_mb"),
            "mem_total_mb": _скаляр(строки, "mem_total_mb"),
            # Температура и мощность — по пику: перегрев и упор в лимит
            # длятся минуты, а усреднение по получасовой корзине их стирает,
            # и на графике остаётся спокойная линия вместо той единственной
            # картины, ради которой на него смотрят.
            "temperature_c": _ряд(строки, корзин, "temperature_c"),
            "power_w": _ряд(строки, корзин, "power_w"),
            "power_limit_w": _скаляр(строки, "power_limit_w"),
        })

    return {
        "minutes": minutes,
        "points": корзин,
        "sampled": sum(int(r.get("samples") or 0) for r in свёрнутые),
        "system": ряды,
        "gpus": карты,
    }


# ---------------------------------------------------------------------------
# Оповещения
# ---------------------------------------------------------------------------

@router.get("/alerts", summary="Состояние оповещений")
def alerts(request: Request, only_firing: bool = False,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    service = _monitoring(request)
    service.samples()
    engine = service.alerts
    return {
        "summary": engine.summary(),
        "alerts": engine.firing() if only_firing else engine.states(),
    }


@router.get("/alerts/history", summary="История срабатываний")
def alerts_history(request: Request,
                   limit: int = Query(default=100, ge=1, le=2000),
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return {"items": _monitoring(request).alerts.history(limit)}


@router.get("/alerts/rules", summary="Правила оповещения")
def alert_rules(request: Request,
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    return {"rules": [r.to_dict() for r in _monitoring(request).alerts.rules]}


@router.put("/alerts/rules", summary="Заменить правила оповещения")
def set_alert_rules(request: Request, rules: list[dict[str, Any]] = Body(...),
                    principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Заменяет правила и сохраняет их в настройку `monitoring_rules`.

    Раньше правила жили только в памяти процесса и пропадали при
    перезапуске, хотя интерфейс об этом не предупреждал. Теперь они
    записываются в настройки и в файл конфигурации (`persisted` в ответе
    говорит, получилось ли второе).
    """
    require_admin(principal)
    service = _monitoring(request)
    try:
        parsed = [Rule.from_dict(item) for item in rules]
    except (KeyError, ValueError, TypeError) as exc:
        raise error_response(ConfigError(
            f"Неверное описание правила: {exc}",
            hint='Каждое правило: {"metric": "...", "direction": "above|below", '
                 '"threshold": число, "severity": "warning|critical", "for_seconds": 300}')
        ) from exc
    # Опечатка в имени метрики давала правило, которое молчит всегда: такой
    # метрики нет, и порог не с чем сравнивать.
    неизвестные = sorted({п.metric for п in parsed
                          if п.metric not in metric_catalog.METRICS_BY_NAME})
    if неизвестные:
        raise error_response(ConfigError(
            f"Метрик нет в каталоге: {', '.join(неизвестные)}.",
            hint="Список метрик: GET /api/monitoring/catalog"))
    сохранено = service.save_rules(parsed)
    return {"rules": len(service.alerts.rules), **сохранено}


@router.post("/alerts/rules/reset", summary="Вернуть правила из каталога метрик")
def reset_alert_rules(request: Request,
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    service = _monitoring(request)
    сохранено = service.save_rules(None)
    return {"rules": len(service.alerts.rules), **сохранено}


# ---------------------------------------------------------------------------
# Отправка наружу
# ---------------------------------------------------------------------------

@router.get("/targets", summary="Приёмники метрик и состояние доставки")
def targets(request: Request,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Куда уходят метрики и как идёт доставка.

    Адрес приёмника отдаётся целиком только администратору. У InfluxDB и
    Pushgateway учётные данные сплошь и рядом стоят прямо в строке запроса,
    а входящий адрес чата — это токен: соседние PUT и «проверить» требуют
    администратора, а чтение отдавало то же самое ключу «только чтение».
    """
    список = _monitoring(request).push.targets()
    if not principal.is_admin:
        список = [{**t, "url": _hide_url(str(t.get("url") or ""))} for t in список]
    return {"kinds": list(KINDS), "targets": список}


def _скрыть_пути(request: Request, строки: list[str] | None) -> list[str]:
    """Прячет пути и адреса в текстах ошибок сбора.

    Разбор один на всю диагностику: `selfcheck.спрятать_пути` уже умеет
    выбрасывать каталог данных и всё, похожее на адрес, и заводить второй
    такой же было бы способом получить два разных ответа на один вопрос.
    """
    from ..selfcheck import спрятать_пути  # noqa: PLC0415

    if not строки:
        return list(строки or [])
    настройки = getattr(get_state(request), "settings", None)
    свод = спрятать_пути({"components": [{"id": "errors", "checks": [
        {"id": str(н), "title": "", "state": "fail", "value": текст,
         "hint": "", "metrics": {}} for н, текст in enumerate(строки)]}]},
        настройки)
    return [п["value"] for п in свод["components"][0]["checks"]]


def _без_путей(request: Request, строки: list[str] | None) -> list[str]:
    """То же, но для маршрута без аутентификации: прячем всегда."""
    return _скрыть_пути(request, строки)


def _hide_url(url: str) -> str:
    """Оставляет от адреса схему и узел — по ним видно, куда идёт отправка.

    Полностью прятать нельзя: страница мониторинга должна отвечать на
    вопрос «а куда мы вообще шлём», и «***» на него не отвечает.
    """
    if not url:
        return ""
    try:
        разбор = urlsplit(url)
    except ValueError:
        return "***"
    if not разбор.scheme or not разбор.hostname:
        return "***"
    порт = f":{разбор.port}" if разбор.port else ""
    хвост = "/…" if разбор.path not in ("", "/") or разбор.query else ""
    return f"{разбор.scheme}://{разбор.hostname}{порт}{хвост}"


_ПОДСКАЗКА_ПРИЁМНИКА = ('Каждый приёмник: {"kind": "' + "|".join(KINDS) + '", '
                        '"url": "...", "interval_s": 60}')


def _разобрать_приёмники(service: Any, targets: list[Any]) -> list[Target]:
    """Приёмники из запроса — поверх сохранённых с теми же именами.

    Чего в описании нет, берётся у сохранённого приёмника с тем же именем:
    заголовки (их значения GET отдаёт заглушкой), база InfluxDB, тайм-аут,
    выключенность. Раньше «Добавить приёмник» собирал список из ответа
    GET, где этих полей не было, и у всех прежних приёмников пропадал
    заголовок Authorization, база сбрасывалась в «asrhub», а выключенные
    включались.
    """
    прежние = {t.name: t for t in service.push.target_list()}
    итог: list[Target] = []
    имена: set[str] = set()
    for номер, item in enumerate(targets, 1):
        if not isinstance(item, dict):
            raise error_response(ConfigError(
                f"Приёмник {номер}: ожидается объект", hint=_ПОДСКАЗКА_ПРИЁМНИКА))
        имя = str(item.get("name") or item.get("kind") or "")
        try:
            приёмник = Target.from_dict(item, прежний=прежние.get(имя))
        except (KeyError, ValueError, TypeError) as exc:
            raise error_response(ConfigError(
                f"Неверное описание приёмника: {exc}", hint=_ПОДСКАЗКА_ПРИЁМНИКА)) from exc
        if not приёмник.url.strip():
            raise error_response(ConfigError(
                f"Приёмник «{приёмник.name}»: не задан адрес (url).",
                hint=_ПОДСКАЗКА_ПРИЁМНИКА))
        if приёмник.name in имена:
            raise error_response(ConfigError(
                f"Два приёмника с именем «{приёмник.name}».",
                hint="Имя — ключ приёмника: задайте разные поля name."))
        имена.add(приёмник.name)
        итог.append(приёмник)
    return итог


@router.put("/targets", summary="Заменить список приёмников")
def set_targets(request: Request, targets: list[dict[str, Any]] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Заменяет список целиком и сохраняет его в настройку `monitoring_targets`."""
    require_admin(principal)
    service = _monitoring(request)
    parsed = _разобрать_приёмники(service, targets)
    сохранено = service.save_targets(parsed)
    return {"targets": len(parsed), **сохранено}


@router.post("/targets", summary="Добавить приёмник")
def add_target(request: Request, target: dict[str, Any] = Body(...),
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Добавляет один приёмник, не трогая остальные.

    Имя — ключ: если его не дали, берётся вид приёмника, а занятое имя
    получает номер («influxdb-2»). Остальные приёмники остаются ровно
    такими, какими были, — со своими заголовками и базами.
    """
    require_admin(principal)
    service = _monitoring(request)
    if not isinstance(target, dict):
        raise error_response(ConfigError("Приёмник — объект с полями kind и url.",
                                         hint=_ПОДСКАЗКА_ПРИЁМНИКА))
    прежние = service.push.target_list()
    занятые = {t.name for t in прежние}
    основа = str(target.get("name") or target.get("kind") or "").strip()
    имя, номер = основа, 2
    while имя in занятые:
        имя = f"{основа}-{номер}"
        номер += 1
    новый = _разобрать_приёмники(service, [{**target, "name": имя}])[0]
    сохранено = service.save_targets([*прежние, новый])
    return {"target": новый.to_dict(), "targets": len(прежние) + 1, **сохранено}


@router.delete("/targets/{name}", summary="Убрать приёмник")
def delete_target(request: Request, name: str,
                  principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    service = _monitoring(request)
    прежние = service.push.target_list()
    осталось = [t for t in прежние if t.name != name]
    if len(осталось) == len(прежние):
        raise error_response(ConfigError(f"Приёмника «{name}» нет.",
                                         hint="Список: GET /api/monitoring/targets"))
    сохранено = service.save_targets(осталось)
    return {"removed": name, "targets": len(осталось), **сохранено}


@router.post("/targets/test", summary="Проверить приёмник немедленно")
def test_target(request: Request, target: dict[str, Any] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Отправляет текущий снимок в указанный приёмник и возвращает результат.

    Приёмник можно не сохранять: описание передаётся прямо в теле запроса,
    поэтому настройку удобно проверять до того, как записать её в конфигурацию.
    """
    require_admin(principal)
    service = _monitoring(request)
    # Проверяют и новый приёмник, и уже сохранённый (по имени): у второго
    # заголовки приходят заглушкой и берутся у сохранённого.
    прежние = {t.name: t for t in service.push.target_list()}
    try:
        parsed = Target.from_dict(target, прежний=прежние.get(
            str((target or {}).get("name") or "")) if isinstance(target, dict) else None)
    except (KeyError, ValueError, TypeError) as exc:
        raise error_response(ConfigError(f"Неверное описание приёмника: {exc}")) from exc
    return service.push.push_once(parsed, проба=True)


# ---------------------------------------------------------------------------
# Готовые конфигурации
# ---------------------------------------------------------------------------

@router.get("/config/prometheus", summary="Готовые правила оповещения Prometheus",
            response_class=PlainTextResponse)
def prometheus_rules(request: Request) -> Response:
    """Файл правил, собранный из порогов каталога. Скопировать в rules.yml."""
    _guard(request)
    return Response(content=exporters.prometheus_rules(get_state(request).settings),
                    media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="asrhub-rules.yml"'})


#: Адреса «слушать всё» — в targets Prometheus они бессмысленны.
_ВСЕ_АДРЕСА = {"", "0.0.0.0", "::", "[::]", "*"}

#: Что может стоять в `targets`: узел или адрес и порт. Всё прочее во
#: фрагмент YAML не пускаем — ни кавычку, ни перевод строки.
_АДРЕС = re.compile(r"[\w.\-]+(?::\d{1,5})?|\[[0-9A-Fa-f:.]+\](?::\d{1,5})?")


def _адрес_сбора(request: Request, state: Any) -> str:
    """Адрес, по которому Prometheus найдёт сервер.

    Раньше брался `server_host`, а он по умолчанию 0.0.0.0 — «слушать на
    всех адресах», — и во фрагмент уходило `targets: ['0.0.0.0:8080']`.
    Правильнее всего тот адрес, по которому фрагмент и забрали: заголовок
    Host запроса. Настройка — запасной путь, если она задана конкретным
    адресом.
    """
    узел = str(request.headers.get("host") or "").strip()
    if (узел and _АДРЕС.fullmatch(узел)
            and узел.rsplit(":", 1)[0].strip("[]") not in _ВСЕ_АДРЕСА):
        return узел
    настроен = str(state.settings.get("server_host") or "").strip()
    порт = state.settings.get("server_port") or 8080
    if настроен in _ВСЕ_АДРЕСА:
        настроен = "127.0.0.1"
    return f"{настроен}:{порт}"


@router.get("/config/prometheus-scrape", summary="Фрагмент prometheus.yml для сбора",
            response_class=PlainTextResponse)
def prometheus_scrape(request: Request,
                      target: str = Query(default="", description="адрес:порт сервера")) -> Response:
    """Готовое задание сбора — элемент списка `scrape_configs`.

    Прежде отдавался блок вместе с ключом `scrape_configs:`, а документация
    предлагала дописать его в конец prometheus.yml. В файле, где этот ключ
    уже есть, получался второй ключ верхнего уровня: строгий разбор YAML
    Prometheus такой файл отвергает, нестрогий теряет все прежние задания.
    Теперь это одно задание — его вставляют под существующий
    `scrape_configs:`.
    """
    _guard(request)
    state = get_state(request)
    if target.strip() and not _АДРЕС.fullmatch(target.strip()):
        raise error_response(ConfigError(
            "target — узел и порт сервера, например asr.company.ru:8080."))
    host = target.strip() or _адрес_сбора(request, state)
    схема = "https" if request.url.scheme == "https" else "http"
    body = (
        "# Задание сбора метрик ASR Hub. Вставьте его в prometheus.yml под ключ\n"
        "# scrape_configs: (второй такой ключ в файле Prometheus не примет).\n"
        "- job_name: asrhub\n"
        "  metrics_path: /api/monitoring/metrics\n"
        + (f"  scheme: {схема}\n" if схема == "https" else "")
        + "  # Сбор чаще, чем раз в 15 секунд, смысла не имеет: замеры железа\n"
        "  # обновляются раз в 20 секунд служебным циклом сервера.\n"
        "  scrape_interval: 30s\n"
        "  scrape_timeout: 10s\n"
        "  static_configs:\n"
        f"    - targets: ['{host}']\n"
        "  # Если monitoring_public выключен, добавьте ключ доступа:\n"
        "  # authorization:\n"
        "  #   type: Bearer\n"
        "  #   credentials: ah_ваш_ключ\n"
    )
    return Response(content=body, media_type="text/yaml; charset=utf-8")


@router.get("/config/grafana", summary="Готовая панель Grafana")
def grafana(request: Request, title: str = "ASR Hub") -> Response:
    """Панель, собранная по группам каталога метрик. Импортировать в Grafana."""
    _guard(request)
    body = json.dumps(exporters.grafana_dashboard(title), ensure_ascii=False, indent=1)
    return Response(content=body, media_type="application/json; charset=utf-8",
                    headers={"Content-Disposition":
                             'attachment; filename="asrhub-dashboard.json"'})


@router.get("/config/zabbix", summary="Готовый шаблон Zabbix",
            response_class=PlainTextResponse)
def zabbix(request: Request) -> Response:
    _guard(request)
    return Response(content=exporters.zabbix_template(get_state(request).settings),
                    media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition":
                             'attachment; filename="asrhub-zabbix-template.yaml"'})


@router.get("/info", summary="Состояние самой подсистемы мониторинга")
def info(request: Request, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сколько было опросов, сколько метрик, какие источники не отвечают.

    Адреса приёмников прячутся от неадминистратора ровно как в соседнем
    `/targets`: этот ответ несёт тот же список, и без такой же обрезки
    ключ «только чтение» получал строку подключения к InfluxDB вместе с
    учётными данными и входящий адрес чата вместе с токеном.
    """
    свод = _monitoring(request).info()
    if not principal.is_admin:
        if isinstance(свод.get("targets"), list):
            свод = {**свод, "targets": [{**t, "url": _hide_url(str(t.get("url") or ""))}
                                        for t in свод["targets"]]}
        # Адреса приёмников обрезались, а ошибки сбора пропускались, хотя
        # несут те же сведения в открытом виде.
        if свод.get("collection_errors"):
            свод = {**свод,
                    "collection_errors": _скрыть_пути(request,
                                                      свод["collection_errors"])}
    return свод
