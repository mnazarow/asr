"""Маршруты мониторинга: /api/monitoring/*.

Отдельная группа, а не расширение /api/metrics, потому что у неё другой режим
доступа: систему мониторинга обычно пускают без ключа с адресов сети сбора,
тогда как остальной интерфейс закрыт. Разделение позволяет настроить это на
прокси одним правилом по префиксу пути.
"""
from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from ..errors import ASRHubError, AuthError, ConfigError, ForbiddenError, MetricNotFound
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


# ---------------------------------------------------------------------------
# Метрики
# ---------------------------------------------------------------------------

@router.get("/metrics", summary="Метрики во всех поддерживаемых форматах",
            response_class=PlainTextResponse)
def metrics(request: Request,
            format: str = Query(default="prometheus",
                                description="prometheus, openmetrics, json, otlp, "
                                            "influx, graphite, zabbix, csv"),
            host: str = Query(default="asrhub", description="Имя узла для Zabbix"),
            ) -> Response:
    """Полный снимок всех параметров работы сервиса.

    Формат выбирается параметром `format`. По умолчанию — текстовый формат
    Prometheus, его же ждёт большинство систем сбора.
    """
    _guard(request)
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
    service = _monitoring(request)
    samples, errors = service.samples()
    payload = exporters.json_snapshot(samples, errors)
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
    code = {"ok": 200, "warning": 200, "degraded": 503, "critical": 503}[result["status"]]
    return JSONResponse(result, status_code=code)


@router.get("/live", summary="Проба живости")
def live(request: Request) -> Any:
    """Для оркестратора: провал означает «перезапусти контейнер»."""
    result = probes.liveness(get_state(request))
    return JSONResponse(result, status_code=200 if result["status"] == "ok" else 503)


@router.get("/ready", summary="Проба готовности")
def ready(request: Request) -> Any:
    """Для балансировщика: провал означает «не шли сюда запросы»."""
    result = probes.readiness(get_state(request))
    return JSONResponse(result, status_code=503 if result["status"] == "fail" else 200)


@router.get("/startup", summary="Проба завершения запуска")
def startup_probe(request: Request) -> Any:
    """Пока не пройдена, остальные пробы учитывать не следует."""
    result = probes.startup(get_state(request))
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
    service.alerts.set_rules(parsed)
    return {"rules": len(parsed)}


@router.post("/alerts/rules/reset", summary="Вернуть правила из каталога метрик")
def reset_alert_rules(request: Request,
                      principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    service = _monitoring(request)
    service.alerts.reset_rules()
    return {"rules": len(service.alerts.rules)}


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


@router.put("/targets", summary="Заменить список приёмников")
def set_targets(request: Request, targets: list[dict[str, Any]] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    require_admin(principal)
    service = _monitoring(request)
    try:
        parsed = [Target.from_dict(item) for item in targets]
    except (KeyError, ValueError, TypeError) as exc:
        raise error_response(ConfigError(
            f"Неверное описание приёмника: {exc}",
            hint='Каждый приёмник: {"kind": "' + "|".join(KINDS) + '", '
                 '"url": "...", "interval_s": 60}')) from exc
    service.push.set_targets(parsed)
    if parsed:
        service.push.start()
    return {"targets": len(parsed)}


@router.post("/targets/test", summary="Проверить приёмник немедленно")
def test_target(request: Request, target: dict[str, Any] = Body(...),
                principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Отправляет текущий снимок в указанный приёмник и возвращает результат.

    Приёмник можно не сохранять: описание передаётся прямо в теле запроса,
    поэтому настройку удобно проверять до того, как записать её в конфигурацию.
    """
    require_admin(principal)
    service = _monitoring(request)
    try:
        parsed = Target.from_dict(target)
    except (KeyError, ValueError, TypeError) as exc:
        raise error_response(ConfigError(f"Неверное описание приёмника: {exc}")) from exc
    return service.push.push_once(parsed)


# ---------------------------------------------------------------------------
# Готовые конфигурации
# ---------------------------------------------------------------------------

@router.get("/config/prometheus", summary="Готовые правила оповещения Prometheus",
            response_class=PlainTextResponse)
def prometheus_rules(request: Request) -> Response:
    """Файл правил, собранный из порогов каталога. Скопировать в rules.yml."""
    _guard(request)
    return Response(content=exporters.prometheus_rules(),
                    media_type="text/yaml; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="asrhub-rules.yml"'})


@router.get("/config/prometheus-scrape", summary="Фрагмент prometheus.yml для сбора",
            response_class=PlainTextResponse)
def prometheus_scrape(request: Request,
                      target: str = Query(default="", description="адрес:порт сервера")) -> Response:
    """Готовый блок scrape_configs — с правильным путём и разумным интервалом."""
    _guard(request)
    state = get_state(request)
    host = target or f"{state.settings.get('server_host') or '127.0.0.1'}:" \
                     f"{state.settings.get('server_port') or 8080}"
    body = (
        "# Фрагмент prometheus.yml для сбора метрик ASR Hub.\n"
        "scrape_configs:\n"
        "  - job_name: asrhub\n"
        "    metrics_path: /api/monitoring/metrics\n"
        "    # Сбор чаще, чем раз в 15 секунд, смысла не имеет: замеры железа\n"
        "    # обновляются раз в 20 секунд служебным циклом сервера.\n"
        "    scrape_interval: 30s\n"
        "    scrape_timeout: 10s\n"
        "    static_configs:\n"
        f"      - targets: ['{host}']\n"
        "    # Если monitoring_public выключен, добавьте ключ доступа:\n"
        "    # authorization:\n"
        "    #   type: Bearer\n"
        "    #   credentials: ah_ваш_ключ\n"
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
    return Response(content=exporters.zabbix_template(),
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
    if not principal.is_admin and isinstance(свод.get("targets"), list):
        свод = {**свод, "targets": [{**t, "url": _hide_url(str(t.get("url") or ""))}
                                    for t in свод["targets"]]}
    return свод
