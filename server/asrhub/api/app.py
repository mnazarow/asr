"""Сборка приложения FastAPI: middleware, обработчики ошибок, WebSocket, статика."""
from __future__ import annotations

import asyncio
import json
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
from ..errors import ASRHubError
from ..job_queue import JobQueue
from ..logging_setup import get_logger, setup
from .deps import AppState
from .routes_catalog import router as catalog_router
from .routes_jobs import router as jobs_router
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


class EventHub:
    """Рассылка событий подписчикам WebSocket."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._history: list[dict[str, Any]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def unregister(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

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
        payload = json.dumps(message, ensure_ascii=False, default=str)
        dead: list[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_text(payload)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    @property
    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

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

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        hub.bind(asyncio.get_running_loop())
        if start_queue:
            queue.start()
        log.info("ASR Hub запущен: %s:%s, каталог данных %s",
                 settings.get("server_host"), settings.get("server_port"),
                 settings.paths.data)
        if settings.hardware_hint:
            log.info("%s", settings.hardware_hint)
        yield
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
    async def add_timing(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except ASRHubError as exc:
            return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
        response.headers["X-Process-Time"] = f"{(time.perf_counter() - started) * 1000:.1f}ms"
        return response

    @app.exception_handler(ASRHubError)
    async def asrhub_error_handler(request: Request, exc: ASRHubError):
        log.warning("Ошибка API %s: %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception):
        log.exception("Непредвиденная ошибка на %s: %s", request.url.path, exc)
        return JSONResponse(status_code=500, content={
            "code": "internal_error",
            "message": f"Внутренняя ошибка сервера: {type(exc).__name__}",
            "hint": "Подробности в журнале сервера: раздел «Журнал» или файл logs/errors.log",
        })

    app.include_router(jobs_router)
    app.include_router(catalog_router)
    app.include_router(system_router)

    ws_router = APIRouter()

    @ws_router.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await hub.register(websocket)
        try:
            await websocket.send_text(json.dumps({
                "type": "hello",
                "queue": queue.status(),
                "history": hub.history[-20:],
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

    return app
