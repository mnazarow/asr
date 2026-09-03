"""Сборка приложения FastAPI: middleware, обработчики ошибок, WebSocket, статика."""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..analytics import Analytics
from ..config import Settings, load
from ..db import Database
from ..engines import EngineRegistry
from ..errors import ASRHubError, FileTooLarge
from ..job_queue import JobQueue
from ..logging_setup import get_logger, setup
from ..monitoring import RUNTIME, MonitoringService
from .deps import AppState
from .routes_catalog import router as catalog_router
from .routes_jobs import router as jobs_router
from .routes_monitoring import router as monitoring_router
from .routes_system import router as system_router

log = get_logger("app")

DESCRIPTION = """
Сервер распознавания речи с поддержкой GigaAM, Whisper и других свободных моделей.

**Аутентификация.** Передайте ключ в заголовке `X-API-Key` или
`Authorization: Bearer <ключ>`. Ключ, созданный при первом запуске,
лежит в файле `api-key.txt` в каталоге данных.

**Быстрый старт.**
```bash
curl -X POST http://сервер:8080/api/jobs \\
  -H "X-API-Key: ваш_ключ" \\
  -F "file=@запись.mp3" \\
  -F 'settings={"model":"gigaam-v3-e2e-rnnt","language":"ru"}'
```
"""


# Пути, которые считаются известными, когда шаблон маршрута недоступен
# (статика и 404). Всё остальное сводится к «other»: иначе любой перебор
# несуществующих адресов бесконечно наращивал бы число серий в памяти.
_KNOWN_PREFIXES = ("/api/", "/ws", "/static/")


#: Пути, которые сервер обслуживает. Заполняется один раз при первом
#: обращении из таблицы маршрутов приложения.
_ROUTE_WHITELIST: set[str] | None = None


def register_routes(app: Any) -> None:
    """Запоминает шаблоны маршрутов приложения для метки route."""
    global _ROUTE_WHITELIST
    paths = {"/", "/ws"}

    def walk(routes: Any, depth: int = 0) -> None:
        # Подключённые маршрутизаторы приходят обёртками без собственного
        # path, поэтому обходим вложенность, а не только верхний уровень:
        # иначе в списке оказывались четыре служебных адреса, и все
        # настоящие маршруты считались неизвестными.
        if depth > 4:
            return
        for route in routes or []:
            path = getattr(route, "path", None)
            if path:
                # Шаблон FastAPI выглядит как /api/jobs/{job_id}; приводим к
                # тому же виду, в каком его собирает _route_fallback.
                paths.add(re.sub(r"\{[^}]+\}", "{id}", path))
            # Подключённый маршрутизатор в новых версиях FastAPI приходит
            # обёрткой _IncludedRouter: настоящие маршруты лежат в
            # original_router. Заглядываем и туда, и в обычное routes.
            walk(getattr(route, "routes", None), depth + 1)
            inner = getattr(route, "original_router", None)
            if inner is not None:
                walk(getattr(inner, "routes", None), depth + 1)

    walk(getattr(app, "routes", []))
    _ROUTE_WHITELIST = paths


def _known_routes() -> set[str]:
    return _ROUTE_WHITELIST if _ROUTE_WHITELIST is not None else set()


def _route_fallback(path: str) -> str:
    """Схлопывает путь в устойчивую метку, когда шаблон маршрута недоступен.

    Число значений метки должно быть ограничено сверху: хранилище метрик
    хранит по временному ряду на каждое, и неограниченный рост — это утечка
    памяти и у нас, и в Prometheus.
    """
    if path in ("/", ""):
        return "/"
    if not path.startswith(_KNOWN_PREFIXES):
        return "other"
    parts = []
    for part in path.split("/"):
        if part.startswith(("job_", "up_", "grp_", "ah_")) or (
                len(part) > 12 and any(ch.isdigit() for ch in part)):
            parts.append("{id}")
        else:
            parts.append(part)
    collapsed = "/".join(parts) or "/"
    if len(collapsed) > 80:
        return "other"
    # Ограничение длины само по себе ничего не ограничивало: перебор коротких
    # несуществующих путей внутри /api/ добавлял по ряду на каждый, а 404
    # отдаётся маршрутизатором до проверки ключа, так что перебор бесплатный
    # и анонимный. Считаем меткой только то, что сервер действительно
    # обслуживает; остальное схлопывается в «unknown».
    known = _known_routes()
    if not known:
        # Список ещё не заполнен (модуль используется отдельно от приложения)
        # — возвращаем схлопнутый путь, как раньше.
        return collapsed
    return collapsed if collapsed in known else "unknown"


class EventHub:
    """Рассылка событий подписчикам WebSocket.

    Событие о задании адресное. Раньше рассылка шла всем подряд, и любой
    ключ — включая readonly — читал в ленте имена чужих файлов и тексты
    чужих ошибок, а при подключении получал ещё и двадцать последних
    событий с историей. Теперь у каждого подписчика запомнены владелец и
    роль, и сообщение доходит только владельцу задания и администратору.
    """

    def __init__(self) -> None:
        #: WebSocket -> (владелец, администратор ли)
        self._clients: dict[WebSocket, tuple[str, bool]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: list[dict[str, Any]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, websocket: WebSocket, owner: str = "",
                       is_admin: bool = False) -> None:
        await websocket.accept()
        self._clients[websocket] = (owner, is_admin)
        RUNTIME.websocket_clients = len(self._clients)

    def unregister(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)
        RUNTIME.websocket_clients = len(self._clients)

    @staticmethod
    def _visible(message: dict[str, Any], owner: str, is_admin: bool) -> bool:
        if is_admin:
            return True
        source = message.get("_owner")
        if source is None:          # событие не про задание — общее для всех
            return True
        return str(source) == owner

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Вызывается из рабочих потоков — переносим в цикл событий."""
        message = {"type": kind, "ts": time.time(), **data}
        self._history.append(message)
        if len(self._history) > 200:
            self._history = self._history[-200:]
        loop = self._loop
        if loop is None or not self._clients:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message), loop)
        except RuntimeError:
            pass

    async def _broadcast(self, message: dict[str, Any]) -> None:
        # Служебное поле с владельцем наружу не уходит: по нему решают,
        # кому отправлять, а клиенту оно ничего не даёт.
        public = {k: v for k, v in message.items() if k != "_owner"}
        payload = json.dumps(public, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for client, (owner, is_admin) in list(self._clients.items()):
            if not self._visible(message, owner, is_admin):
                continue
            try:
                await client.send_text(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.pop(client, None)

    def history_for(self, owner: str, is_admin: bool) -> list[dict[str, Any]]:
        """История в том же объёме, в каком подписчик получал бы её вживую."""
        return [{k: v for k, v in m.items() if k != "_owner"}
                for m in self._history if self._visible(m, owner, is_admin)]

    @property
    def history(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in m.items() if k != "_owner"} for m in self._history]

    @property
    def count(self) -> int:
        return len(self._clients)


def create_app(settings: Settings | None = None, *, start_queue: bool = True) -> FastAPI:
    settings = settings or load()
    setup(str(settings.get("log_level") or "INFO"), settings.paths.logs)

    db = Database(settings.paths.db)
    registry = EngineRegistry(int(settings.get("model_cache_size") or 2),
                              int(settings.get("model_idle_unload_s") or 900))
    hub = EventHub()
    queue = JobQueue(db, settings, registry, on_event=hub.publish)
    analytics = Analytics(db)
    state = AppState(settings=settings, db=db, registry=registry,
                     queue=queue, analytics=analytics)
    state.monitoring = MonitoringService(state)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        hub.bind(asyncio.get_running_loop())
        if start_queue:
            queue.start()
        state.monitoring.start()
        log.info("ASR Hub запущен: %s:%s, каталог данных %s",
                 settings.get("server_host"), settings.get("server_port"),
                 settings.paths.data)
        if settings.hardware_hint:
            log.info("%s", settings.hardware_hint)
        yield
        state.monitoring.stop()
        queue.stop()
        registry.unload_all()
        db.close()
        log.info("ASR Hub остановлен")

    app = FastAPI(
        title="ASR Hub",
        description=DESCRIPTION,
        version="3.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )
    app.state.hub = state
    app.state.events = hub

    origins = str(settings.get("cors_origins") or "").strip()
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"] if origins == "*" else
            [o.strip() for o in origins.split(",") if o.strip()],
            allow_credentials=origins != "*",
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        """Отсекает слишком большое тело до того, как его начнут принимать.

        Предел `max_upload_mb` проверялся в обработчике, а FastAPI к тому
        моменту уже разобрал multipart целиком: файловая часть оседала во
        временном файле без всякого верхнего предела. Запрос на десятки
        гигабайт забивал /tmp и только потом получал 413. Здесь мы смотрим
        на Content-Length до маршрутизации — это не полная защита (заголовок
        можно не прислать), но она закрывает и обычного клиента, и простой
        способ забить диск.
        """
        if request.method in ("POST", "PUT", "PATCH"):
            declared = request.headers.get("content-length")
            if declared and declared.isdigit():
                limit_mb = float(settings.get("max_upload_mb") or 2048)
                # Пакетная загрузка везёт несколько файлов одним запросом.
                multiplier = 1
                if request.url.path.rstrip("/").endswith("/batch"):
                    multiplier = max(1, int(settings.get("max_batch_files") or 20))
                # Небольшой запас на границы multipart и остальные поля формы.
                limit = int(limit_mb * 1024 * 1024) * multiplier + (1 << 20)
                if int(declared) > limit:
                    error = FileTooLarge(int(declared) / 1024 / 1024,
                                         int(limit_mb * multiplier))
                    RUNTIME.note_error(error.code, error.retryable)
                    # Тело ответа той же формы, что у остальных ошибок API,
                    # иначе клиент не найдёт привычный code и hint.
                    return JSONResponse(status_code=413, content=error.to_dict())
        return await call_next(request)

    @app.middleware("http")
    async def add_timing(request: Request, call_next):
        started = time.perf_counter()
        RUNTIME.request_started()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        except ASRHubError as exc:
            # Сюда попадают только ошибки, поднятые вне обработчика запроса;
            # остальные перехватывает @app.exception_handler ниже по стеку.
            status_code = exc.http_status
            RUNTIME.note_error(exc.code, exc.retryable)
            return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
        finally:
            RUNTIME.request_finished()
            elapsed = time.perf_counter() - started
            # Метка маршрута берётся из шаблона (/api/jobs/{job_id}), а не из
            # конкретного адреса: иначе метки размножатся по числу заданий и
            # уронят хранилище метрик.
            route = request.scope.get("route")
            path = getattr(route, "path", None) or _route_fallback(request.url.path)
            labels = {"method": request.method, "route": path}
            RUNTIME.inc("asrhub_http_requests_total", {**labels, "status": str(status_code)})
            RUNTIME.observe("asrhub_http_request_seconds", elapsed, labels)
            if status_code == 401:
                RUNTIME.inc("asrhub_auth_failures_total")
            elif status_code == 429:
                RUNTIME.inc("asrhub_rate_limited_total")
        response.headers["X-Process-Time"] = f"{elapsed * 1000:.1f}ms"
        return response

    @app.exception_handler(ASRHubError)
    async def asrhub_error_handler(request: Request, exc: ASRHubError):
        log.warning("Ошибка API %s: %s", exc.code, exc.message)
        RUNTIME.note_error(exc.code, exc.retryable)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception):
        log.exception("Непредвиденная ошибка на %s: %s", request.url.path, exc)
        RUNTIME.note_error("internal_error", retryable=False)
        return JSONResponse(status_code=500, content={
            "code": "internal_error",
            "message": f"Внутренняя ошибка сервера: {type(exc).__name__}",
            "hint": "Подробности в журнале сервера: раздел «Журнал» или файл logs/errors.log",
        })

    app.include_router(jobs_router)
    app.include_router(catalog_router)
    app.include_router(system_router)
    app.include_router(monitoring_router)

    ws_router = APIRouter()

    @ws_router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        # Лента событий несёт имена файлов, статусы и ошибки заданий, поэтому
        # закрыта тем же ключом, что и остальной интерфейс. Заголовки в
        # браузерном WebSocket задать нельзя, поэтому веб-интерфейс сначала
        # берёт одноразовый билет (POST /api/auth/ticket) и предъявляет его в
        # параметре ticket. Постоянный ключ в адресе тоже принимается — ради
        # сторонних клиентов, — но интерфейс им больше не пользуется: адрес
        # оседает в истории браузера и в журналах прокси.
        if settings.get("auth_enabled", True):
            hub_state = getattr(websocket.app.state, "hub", None)
            token = ""
            ticket = websocket.query_params.get("ticket", "")
            if ticket and hub_state is not None:
                token = hub_state.tickets.redeem(ticket)
            if not token:
                token = (websocket.query_params.get("api_key")
                         or websocket.headers.get("x-api-key") or "")
            if not token:
                header = websocket.headers.get("authorization", "")
                parts = header.split(" ", 1)
                token = parts[1].strip() if len(parts) == 2 and parts[0].lower() == "bearer" \
                    else header.strip()
            info = settings.api_keys.get(token)
            if not info or info.get("enabled") is False:
                await websocket.close(code=4401, reason="Ключ доступа отсутствует или недействителен")
                return
            # Имя выводим ровно так же, как authenticate строит Principal.name,
            # иначе владелец события и владелец задания не совпадут.
            ws_owner = str(info.get("name") or "ключ")
            ws_admin = str(info.get("role") or "user") == "admin"
        else:
            # Проверка ключей выключена — разделять некого, все равны.
            ws_owner, ws_admin = "", True

        await hub.register(websocket, owner=ws_owner, is_admin=ws_admin)
        try:
            await websocket.send_text(json.dumps({
                "type": "hello",
                "queue": queue.status(),
                "history": hub.history_for(ws_owner, ws_admin)[-20:],
            }, ensure_ascii=False, default=str))
            while True:
                message = await websocket.receive_text()
                if message == "ping":
                    await websocket.send_text('{"type":"pong"}')
                elif message == "status":
                    await websocket.send_text(json.dumps(
                        {"type": "queue", **queue.status()}, ensure_ascii=False, default=str))
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("WebSocket закрыт: %s", exc)
        finally:
            hub.unregister(websocket)

    app.include_router(ws_router)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.exists():
        app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(str(web_dir / "index.html"))

        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon():
            path = web_dir / "favicon.svg"
            if path.exists():
                return FileResponse(str(path), media_type="image/svg+xml")
            return JSONResponse(status_code=404, content={})

    # Список обслуживаемых маршрутов нужен метке route: всё, чего в нём
    # нет, схлопывается в «unknown» и не плодит временные ряды.
    # Заполняем в самом конце, когда подключены все маршрутизаторы.
    register_routes(app)

    return app
