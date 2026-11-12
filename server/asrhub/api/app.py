"""Сборка приложения FastAPI: middleware, обработчики ошибок, WebSocket, статика."""
from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import re
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.websockets import WebSocketState

from .. import __version__, audit
from .. import settings_access as S
from ..accounts import Accounts
from ..analytics import Analytics
from ..config import Settings, load
from ..content_index import ContentIndex
from ..db import Database
from ..engines import EngineRegistry
from ..errors import ASRHubError, AuthError, FileTooLarge, ForbiddenError, RequestTooLarge
from ..job_queue import JobQueue
from ..llm import LLMClient, LLMSetup, LLMWorker
from ..logging_setup import get_logger, setup
from ..monitoring import RUNTIME, MonitoringService
from ..qc import КонтрольКачества
from ..streaming import StreamSession
from ..telephony import Телефония
from ..trends import Trends
from .deps import (
    SESSION_COOKIE,
    AppState,
    scope_owner,
    token_of,
    ключ_действует,
    участник_записи,
    участник_ключа,
)
from .routes_agent import router as agent_router
from .routes_agent import управление as agent_admin_router
from .routes_ai import router as ai_router
from .routes_auth import audit_router, users_router
from .routes_auth import router as auth_router
from .routes_backup import router as backup_router
from .routes_catalog import router as catalog_router
from .routes_content import router as content_router
from .routes_dialplan import router as dialplan_router
from .routes_drill import router as drill_router
from .routes_employees import router as employees_router
from .routes_jobs import router as jobs_router
from .routes_llm import router as llm_router
from .routes_llm_proxy import router as llm_proxy_router
from .routes_manual import router as manual_router
from .routes_monitoring import router as monitoring_router
from .routes_phone import router as phone_router
from .routes_qc import router as qc_router
from .routes_review import router as review_router
from .routes_system import health_router
from .routes_system import router as system_router
from .routes_telephony import router as telephony_router
from .routes_trends import router as trends_router
from .routes_voice import router as voice_router

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


#: Методы HTTP, которые попадают в метку method как есть; остальные — «other».
_ИЗВЕСТНЫЕ_МЕТОДЫ = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})

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


#: Как часто веб-сокет перепроверяет, что доступ у подписчика ещё есть.
#: Отзыв ключа и выход закрывают сокет сразу (`EventHub.close_where`), а
#: перепроверка ловит остальное: истёкшую сессию, отключение записи с
#: другого экземпляра сервера, правку файла ключей руками.
ПЕРЕПРОВЕРКА_СОКЕТА_С = 30.0

#: Сколько ждать отправки одного события одному подписчику ленты.
ОТПРАВКА_СОБЫТИЯ_С = 5.0
#: Прогресс одного задания уходит подписчикам не чаще раза в столько секунд
#: (последнее значение побеждает): движок докладывает долю на каждый кусок
#: звука, и на десятке воркеров лента событий была в основном прогрессом.
ПРОГРЕСС_НЕ_ЧАЩЕ_С = 0.2
#: Сколько последних событий помнит лента для новых подписчиков.
ИСТОРИЯ_СОБЫТИЙ = 200

#: Сколько ждать первого сообщения сессии диктовки. Молчащий сокет держал
#: место среди сессий (и поток сервера) бесконечно.
ПЕРВОЕ_СООБЩЕНИЕ_ПОТОКА_С = 30.0


@dataclass
class Подписчик:
    """Кто слушает ленту событий и что ему положено видеть.

    Собирается из того же участника, что и для HTTP (`deps.участник_*`):
    владелец, подразделение и обезличивание у ленты должны совпадать с
    тем, что тот же ключ получает по REST, иначе лента становится обходом.
    """

    owner: str = ""
    is_admin: bool = False
    peers: frozenset[str] = frozenset()
    #: Ключ с `mask_pii`: события уходят обезличенными.
    mask: bool = False
    #: Чем вошёл — ключом или сессией. Нужно, чтобы закрыть сокет при
    #: отзыве ключа, выходе и отключении записи.
    key: str = ""
    session: str = ""
    user_id: str = ""

    @classmethod
    def из_участника(cls, участник: Any, *, session: str = "") -> Подписчик:
        return cls(owner=str(участник.name), is_admin=bool(участник.is_admin),
                   peers=frozenset(участник.peers or ()),
                   mask=bool(getattr(участник, "mask_pii", False)),
                   key=str(участник.key or ""), session=session,
                   user_id=str(участник.user_id or ""))


class EventHub:
    """Рассылка событий подписчикам WebSocket.

    Событие о задании адресное. Раньше рассылка шла всем подряд, и любой
    ключ — включая readonly — читал в ленте имена чужих файлов и тексты
    чужих ошибок, а при подключении получал ещё и двадцать последних
    событий с историей. Теперь у каждого подписчика запомнены владелец,
    подразделение и роль, и сообщение доходит только тем, кто видит это
    задание и по REST: владельцу, коллегам по группе и администратору.

    Ключу с `mask_pii` события уходят обезличенными: имя файла в
    колл-центре — это номер клиента, и лента отдавала его как есть.
    """

    def __init__(self) -> None:
        self._clients: dict[WebSocket, Подписчик] = {}
        self._streams: dict[WebSocket, Подписчик] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # Очередь с пределом и замком: публикуют рабочие потоки, читает цикл
        # событий. Список с обрезкой `[-200:]` копировался на каждое событие
        # и читался без замка — посреди замены.
        self._history: deque[dict[str, Any]] = deque(maxlen=ИСТОРИЯ_СОБЫТИЙ)
        self._замок = threading.Lock()
        self._прогресс_отправлен: dict[str, float] = {}
        self._прогресс_ждёт: dict[str, tuple[str, dict[str, Any]]] = {}
        #: Проверка «ключ ещё действует» — без базы, на каждое событие:
        #: отозванный ключ не получает больше ни одного сообщения.
        self.ключ_действует: Any = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    async def register(self, websocket: WebSocket,
                       подписчик: Подписчик | None = None) -> None:
        await websocket.accept()
        self._clients[websocket] = подписчик or Подписчик(is_admin=True)
        RUNTIME.websocket_clients = len(self._clients)

    def unregister(self, websocket: WebSocket) -> None:
        self._clients.pop(websocket, None)
        RUNTIME.websocket_clients = len(self._clients)

    # Сессии диктовки (/api/stream) событий не получают, но учитываются
    # здесь же: по ним считаются пределы числа сессий, и их так же надо
    # закрыть при отзыве ключа или выходе.

    def register_stream(self, websocket: WebSocket, подписчик: Подписчик) -> None:
        self._streams[websocket] = подписчик

    def unregister_stream(self, websocket: WebSocket) -> None:
        self._streams.pop(websocket, None)

    def streams(self) -> list[Подписчик]:
        return list(self._streams.values())

    @staticmethod
    def _как_подписчик(кто: Any, is_admin: bool | None) -> Подписчик:
        # Прежняя запись «владелец, администратор ли» тоже понимается.
        if isinstance(кто, Подписчик):
            return кто
        return Подписчик(owner=str(кто or ""), is_admin=bool(is_admin))

    @classmethod
    def _visible(cls, message: dict[str, Any], подписчик: Any,
                 is_admin: bool | None = None) -> bool:
        подписчик = cls._как_подписчик(подписчик, is_admin)
        if подписчик.is_admin:
            return True
        source = message.get("_owner")
        if source is None:          # событие не про задание — общее для всех
            return True
        return str(source) == подписчик.owner or str(source) in подписчик.peers

    @staticmethod
    def _наружу(message: dict[str, Any], mask: bool) -> dict[str, Any]:
        # Служебное поле с владельцем наружу не уходит: по нему решают,
        # кому отправлять, а клиенту оно ничего не даёт.
        public = {k: v for k, v in message.items() if k != "_owner"}
        if mask:
            from ..content import masking  # noqa: PLC0415

            public = masking.mask_payload(public)
        return public

    def _действует(self, подписчик: Подписчик) -> bool:
        проверка = self.ключ_действует
        if подписчик.key and callable(проверка):
            return bool(проверка(подписчик.key))
        return True

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Вызывается из рабочих потоков — переносим в цикл событий."""
        задание = data.get("id") if isinstance(data.get("id"), str) else None
        if задание is not None:
            if kind == "job.progress":
                if self._придержать(задание, kind, data):
                    return
            else:
                # Любое другое событие задания отменяет придержанный прогресс:
                # «готово», а за ним запоздалые «95 %» читались как откат.
                with self._замок:
                    self._прогресс_ждёт.pop(задание, None)
                    self._прогресс_отправлен.pop(задание, None)
        self._опубликовать(kind, data)

    def _придержать(self, задание: str, kind: str, data: dict[str, Any]) -> bool:
        """Прореживает прогресс: не чаще `ПРОГРЕСС_НЕ_ЧАЩЕ_С`, последнее побеждает."""
        сейчас = time.monotonic()
        with self._замок:
            последний = self._прогресс_отправлен.get(задание)
            if последний is None or сейчас - последний >= ПРОГРЕСС_НЕ_ЧАЩЕ_С:
                self._прогресс_отправлен[задание] = сейчас
                self._прогресс_ждёт.pop(задание, None)
                if len(self._прогресс_отправлен) > 4096:
                    for старый in sorted(self._прогресс_отправлен,
                                         key=self._прогресс_отправлен.get)[:2048]:
                        self._прогресс_отправлен.pop(старый, None)
                return False
            уже_ждёт = задание in self._прогресс_ждёт
            self._прогресс_ждёт[задание] = (kind, data)
        if not уже_ждёт:
            таймер = threading.Timer(max(0.01, ПРОГРЕСС_НЕ_ЧАЩЕ_С - (сейчас - последний)),
                                     self._дослать, args=(задание,))
            таймер.daemon = True
            таймер.start()
        return True

    def _дослать(self, задание: str) -> None:
        with self._замок:
            ждёт = self._прогресс_ждёт.pop(задание, None)
            if ждёт is None:
                return
            self._прогресс_отправлен[задание] = time.monotonic()
        self._опубликовать(*ждёт)

    def _опубликовать(self, kind: str, data: dict[str, Any]) -> None:
        message = {"type": kind, "ts": time.time(), **data}
        with self._замок:
            self._history.append(message)
        loop = self._loop
        if loop is None or not self._clients:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast(message), loop)
        except RuntimeError:
            pass

    async def _broadcast(self, message: dict[str, Any]) -> None:
        готовое: dict[bool, str] = {}
        dead: list[WebSocket] = []
        закрыть: list[WebSocket] = []
        for client, подписчик in list(self._clients.items()):
            if not self._действует(подписчик):
                закрыть.append(client)
                continue
            if not self._visible(message, подписчик):
                continue
            if подписчик.mask not in готовое:
                готовое[подписчик.mask] = json.dumps(
                    self._наружу(message, подписчик.mask), ensure_ascii=False, default=str)
            try:
                # С пределом: подписчики обходятся по очереди, и клиент,
                # который не читает (зависшая вкладка, оборванная сеть без
                # FIN), задерживал рассылку всем остальным до тайм-аута ping.
                await asyncio.wait_for(client.send_text(готовое[подписчик.mask]),
                                       timeout=ОТПРАВКА_СОБЫТИЯ_С)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.pop(client, None)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.close(code=1011), timeout=1.0)
        for client in закрыть:
            await self._закрыть(client, 4401, "Ключ доступа отозван или отключён")
        if dead or закрыть:
            RUNTIME.websocket_clients = len(self._clients)

    async def _закрыть(self, client: WebSocket, code: int, reason: str) -> None:
        self._clients.pop(client, None)
        with contextlib.suppress(Exception):
            await client.close(code=code, reason=reason)

    def close_where(self, условие: Any, *, code: int = 4401,
                    reason: str = "Доступ отозван") -> int:
        """Закрывает сокеты подписчиков, для которых `условие(подписчик)` истинно.

        Зовётся из обработчиков отзыва ключа, выхода, отключения и удаления
        учётной записи, смены пароля. Права у сокета проверялись один раз,
        при рукопожатии, и отозванный ключ продолжал получать ленту
        событий, пока открыта вкладка, — хотя REST отвечал ему 401.

        Обработчики — обычные функции и работают в пуле потоков, поэтому
        само закрытие переносится в цикл событий.
        """
        цели = [client for client, подписчик in
                [*self._clients.items(), *self._streams.items()]
                if условие(подписчик)]
        if not цели:
            return 0
        for client in цели:
            self._clients.pop(client, None)
            self._streams.pop(client, None)
        RUNTIME.websocket_clients = len(self._clients)
        loop = self._loop

        async def закрыть_все() -> None:
            for client in цели:
                with contextlib.suppress(Exception):
                    await client.close(code=code, reason=reason)

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and running is loop:
            loop.create_task(закрыть_все())
        elif loop is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                asyncio.run_coroutine_threadsafe(закрыть_все(), loop)
        return len(цели)

    def history_for(self, подписчик: Any,
                    is_admin: bool | None = None) -> list[dict[str, Any]]:
        """История в том же объёме и виде, в каком подписчик получал бы её вживую."""
        подписчик = self._как_подписчик(подписчик, is_admin)
        with self._замок:
            история = list(self._history)
        return [self._наружу(m, подписчик.mask)
                for m in история if self._visible(m, подписчик)]

    @property
    def history(self) -> list[dict[str, Any]]:
        with self._замок:
            история = list(self._history)
        return [{k: v for k, v in m.items() if k != "_owner"} for m in история]

    @property
    def count(self) -> int:
        return len(self._clients)


def _повторить_через(status: int, тело: Any) -> dict[str, str] | None:
    """Заголовок Retry-After для отказа «подождите».

    Срок повтора был только в теле (`details.retry_after_s`), а агент на
    станции и обычные HTTP-клиенты читают заголовок: получив 429 без него,
    агент повторял сразу и снова упирался в предел.
    """
    if status not in (429, 503) or not isinstance(тело, dict):
        return None
    подробности = тело.get("details")
    секунды = подробности.get("retry_after_s") if isinstance(подробности, dict) else None
    try:
        секунды = int(float(секунды)) if секунды is not None else None
    except (TypeError, ValueError):
        секунды = None
    if секунды is None:
        if status != 429:
            return None
        секунды = 60
    return {"Retry-After": str(max(1, секунды))}


#: Отпечатки файлов статики: путь -> (mtime, размер, отпечаток).
_ASSET_VERSIONS: dict[str, tuple[float, int, str]] = {}


def _record_stream(state: Any, settings: dict[str, Any], event: Any) -> None:
    """Запоминает задержку потока: секунды до первого текста и длину сессии.

    Файловое задание оставляет после себя строку в jobs, а поток — ничего:
    его задержка нигде не была видна. Теперь она уходит в таблицу метрик по
    модели, и раздел «Латентность» показывает её рядом с временем обработки.
    """
    try:
        первый = event.extra.get("first_text_s")
        модель = str(settings.get("model") or "")
        движок = str(settings.get("engine") or "")
        if первый is not None:
            state.db.add_metric("stream_first_text_s", float(первый),
                                model=модель, engine=движок)
        state.db.add_metric("stream_session_s", float(event.extra.get("session_s") or 0.0),
                            model=модель, engine=движок,
                            labels={"audio_s": event.extra.get("duration_s")})
    except Exception as exc:                            # noqa: BLE001
        log.debug("Метрики потока не записаны: %s", exc)


def _asset_version(path: Path) -> str:
    """Короткий отпечаток содержимого файла — для ссылки на него.

    Страница ссылалась на `/static/app.js` без всякой версии, а Starlette
    не ставит на статику `Cache-Control` вовсе. По правилам кеширования
    ответ без явного срока браузер волен держать по своему усмотрению —
    обычно десятую часть возраста файла. То есть после обновления сервера
    браузер продолжал показывать прежний интерфейс, ничего не спрашивая, и
    человек видел старую версию, при том что на сервере лежала новая.
    Ровно об этом и пришёл вопрос: «после обновления не обновился веб
    интерфейс».

    Отпечаток считается по содержимому, а не по номеру версии: между
    выпусками интерфейс правится чаще, чем меняется VERSION, и версия бы
    не спасла. Пересчитывается, когда файл на диске изменился, — сервер
    после обновления перезапускается, но с работающим тоже должно быть
    правильно.
    """
    try:
        st = path.stat()
    except OSError:
        return "0"
    ключ = str(path)
    было = _ASSET_VERSIONS.get(ключ)
    if было and было[0] == st.st_mtime and было[1] == st.st_size:
        return было[2]
    try:
        отпечаток = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return "0"
    _ASSET_VERSIONS[ключ] = (st.st_mtime, st.st_size, отпечаток)
    return отпечаток


def _index_html(web_dir: Path) -> str:
    """Разметка главной страницы со ссылками, помеченными отпечатком."""
    html = (web_dir / "index.html").read_text(encoding="utf-8")

    def подставить(match: re.Match[str]) -> str:
        имя = match.group(1)
        return f"/static/{имя}?v={_asset_version(web_dir / имя)}"

    return re.sub(r"/static/([A-Za-z0-9_.-]+)", подставить, html)


class _CachedStatic(StaticFiles):
    """Статика со сроком хранения, зависящим от того, как её попросили.

    Со ссылкой из главной страницы приходит `?v=<отпечаток>`: содержимое по
    такому адресу измениться не может, поэтому его разрешено держать год и
    не спрашивать сервер вовсе. Прямое обращение без отпечатка (закладка,
    сторонний клиент) кешируется только с перепроверкой: там за свежесть
    отвечать нечему.
    """

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        запрос = scope.get("query_string") or b""
        помечено = b"v=" in запрос
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if помечено else "no-cache")
        return response


def _открыть_базу(settings: Settings) -> Database:
    """Открывает базу в режиме журнала из настроек и отмечается в ней.

    Режим журнала — свойство файла, общее для всех серверов над базой, и
    ошибка в нём не видна, пока базу не испортит второй сервер с другой
    машины. Поэтому здесь же — сверка с соседями по их отметкам жизни:
    WAL при соседе с другой машины и разные режимы у соседей — громкая
    ошибка в журнале сервера и событие, а самопроверка повторяет её
    каждый раз, пока причина не исчезнет.
    """
    import os  # noqa: PLC0415

    from .. import fsinfo  # noqa: PLC0415
    from ..instance import other_machine  # noqa: PLC0415

    # Временные файлы SQLite (сжатие базы строит копию на диске) — в каталог
    # временных файлов сервера, а не в системный: там бывает tmpfs, то есть
    # память, или раздел в сотню мегабайт. Заданное администратором не трогаем.
    временные = str(settings.get("temp_dir") or settings.paths.tmp)
    os.environ.setdefault("SQLITE_TMPDIR", временные)

    режим = str(settings.get("db_journal_mode") or "wal")
    try:
        db = Database(settings.paths.db, journal_mode=режим)
    except ASRHubError as exc:
        if exc.code != "config_error":
            raise
        log.error("Режим журнала базы «%s» не годится (%s) — открываю в WAL", режим, exc)
        db = Database(settings.paths.db, journal_mode="wal")
    фс = fsinfo.файловая_система(settings.paths.data)
    db.сведения_отметки = {"version": __version__, "fs": фс.тип,
                           "network": фс.сетевая}
    try:
        db.instance_beat()
        соседи = db.other_instances()
    except ASRHubError as exc:
        log.warning("Отметка экземпляра не поставлена: %s", exc)
        соседи = []
    чужие_машины = sorted({str(с.get("host") or с["instance"]) for с in соседи
                           if other_machine(с)})
    if чужие_машины and db.journal_mode == "wal":
        текст = (f"Над базой работают серверы с других машин ({', '.join(чужие_машины)}), "
                 "а журнал базы — WAL. WAL опирается на общую память одной машины и по "
                 "сети не работает: записи соседей не видны, база портится. Остановите "
                 "все серверы, задайте db_journal_mode: delete всем и запускайте заново. "
                 "Если это прежний экземпляр этого же сервера, упавший без остановки, "
                 "его отметка погаснет за пять минут.")
        log.error("%s", текст)
        db.add_event(None, "storage_warning", текст)
    иные = sorted({str(с.get("journal")) for с in соседи
                   if с.get("journal") and с.get("journal") != db.journal_mode})
    if иные:
        текст = (f"У соседних серверов другой режим журнала базы ({', '.join(иные)}), "
                 f"у этого — {db.journal_mode}. Режим — свойство файла: задайте всем "
                 "одинаковый db_journal_mode и перезапустите их по очереди, остановив все.")
        log.error("%s", текст)
        db.add_event(None, "storage_warning", текст)
    if фс.сетевая:
        log.info("Каталог данных на сетевой файловой системе: %s (%s), журнал базы — %s",
                 фс.тип, фс.точка, db.journal_mode)
    return db


def create_app(settings: Settings | None = None, *, start_queue: bool = True) -> FastAPI:
    settings = settings or load()
    setup(str(settings.get("log_level") or "INFO"), settings.paths.logs)

    # Восстановление данных из копии подменяет базу здесь — до того, как её
    # откроет хоть одно соединение (см. backup._вернуть_базу).
    from .. import backup as _копии  # noqa: PLC0415

    прежняя_база = _копии.применить_отложенное(settings.paths.db)
    # Прежние ключи доступа открытым текстом — хешем, и в файл настроек
    # (см. `Settings.перевести_ключи`).
    try:
        if settings.перевести_ключи() and settings.config_file is not None:
            settings.persist_api_keys()
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Прежние ключи доступа не переведены в хеш: %s", exc)
    db = _открыть_базу(settings)
    if прежняя_база:
        db.add_event(None, "restore_applied",
                     f"База восстановлена из копии при запуске; прежняя сохранена "
                     f"рядом как {прежняя_база}")
    else:
        # Подготовленная база не поставлена (соседи над базой, истёк срок
        # ожидания) — событием, а не только строкой журнала: сервер поднялся
        # на прежней базе, а администратор считал, что восстановил.
        отсрочка = _копии.причина_отсрочки(settings.paths.db)
        if отсрочка.get("reason") and float(отсрочка.get("at") or 0) > time.time() - 60:
            db.add_event(None, "restore_deferred",
                         f"Восстановление базы из копии не поставлено при запуске: "
                         f"{отсрочка['reason']}", отсрочка)
    # Через `S`, а не через `or`: у `model_idle_unload_s` ноль означает
    # «не выгружать модели никогда», и `int(значение or 900)` включал
    # автовыгрузку через пятнадцать минут — при том что в интерфейсе стоял
    # ноль. Заметить это можно было только по внезапной паузе на первой
    # записи после затишья: веса грузятся заново полторы минуты.
    registry = EngineRegistry(max(1, S.integer(settings, "model_cache_size", 2)),
                              S.integer(settings, "model_idle_unload_s", 900))
    hub = EventHub()
    queue = JobQueue(db, settings, registry, on_event=hub.publish)
    # События заданий, которые считают соседние серверы над общей базой.
    from ..events_bridge import МостСобытий  # noqa: PLC0415

    мост = МостСобытий(db, hub.publish)
    # Живые разговоры со станции по протоколу AudioSocket (audiosocket_port).
    from ..telephony.audiosocket import ПриёмAudioSocket  # noqa: PLC0415

    живые = ПриёмAudioSocket(registry, settings, hub.publish, db)
    analytics = Analytics(db)
    state = AppState(settings=settings, db=db, registry=registry,
                     queue=queue, analytics=analytics, queue_enabled=start_queue)
    писатель = audit.Писатель(db)
    state.audit_writer = писатель
    # Копия журнала доступа — во внешний syslog, если он задан (см.
    # `audit.ПриёмникSyslog`); адрес читается на каждой строке.
    db.audit_sink = audit.ПриёмникSyslog(settings)
    # Лента событий проверяет ключ подписчика на каждое событие — тем же
    # правилом, что HTTP: отозванный ключ не получает больше ничего.
    hub.ключ_действует = lambda token: ключ_действует(state, token)
    state.accounts = Accounts(db, float(settings.get("session_ttl_hours") or 168),
                              settings=settings)
    # Учётная запись не должна называться как ключ доступа: владелец задания
    # — голое имя, общее для обоих (см. `Accounts.тёзка_ключа`).
    state.accounts.имена_ключей = lambda: [str(info.get("name") or "ключ")
                                           for info in settings.api_keys.values()]
    state.events = hub
    # Первый запуск: заводим admin с временным паролем. Без этого открыть
    # интерфейс можно было только сходив на сервер за api-key.txt.
    #
    # Ошибка здесь не должна мешать серверу подняться: вход по логину — это
    # удобство поверх ключей доступа, а не условие работы. Сервер, который
    # не стартовал из-за заведения учётной записи, отнимает и распознавание,
    # и единственный способ увидеть, что пошло не так.
    try:
        state.accounts.ensure_default_admin(settings.paths.data if settings.paths else None)
    except Exception as exc:                                   # noqa: BLE001
        log.error("Не удалось завести учётную запись по умолчанию: %s", exc)
        log.error("Вход по логину и паролю недоступен; ключи доступа работают. "
                  "Задать пароль вручную: python -m asrhub --set-password admin")
    state.monitoring = MonitoringService(state)
    state.content = ContentIndex(db, settings)
    # Тренды: ряды по времени для всех измеримых величин. Слой без
    # состояния — только ссылка на базу, поэтому собирается сразу.
    state.trends = Trends(db)
    queue.content_index = state.content
    # Смысловой слой: клиент модели и фоновый поток разбора. Поток уступает
    # распознаванию по глубине очереди — той же, что видит планировщик.
    state.llm = LLMClient(settings, db)
    state.llm_worker = LLMWorker(
        db, settings, state.llm,
        queue_state=lambda: db.count_jobs(status=["queued", "retry"]),
        content_index=state.content)
    queue.llm_worker = state.llm_worker
    # Установщик модели: та же кнопка, что у распознавания, — «поставь и
    # настрой сам». Живёт рядом с клиентом, чтобы после установки сразу
    # сбросить пробу доступности.
    state.llm_setup = LLMSetup(settings, db, client=state.llm)
    # Телефония: записи разговоров приезжают с АТС сами. Станций может
    # быть несколько, и набор приводится к настройке на каждом заходе —
    # добавили филиал, отключили архив, сменили пароль. Собирается всегда,
    # а работает только при включённой настройке: раздел должен показывать
    # журнал уже импортированного и тогда, когда забор выключен.
    state.telephony = Телефония(db, settings, queue)
    # Контроль качества: обратный звонок клиенту с опросом. Собирается
    # всегда, звонит только при включённой настройке (см. qc/survey.py).
    state.qc = КонтрольКачества(db, settings)
    state.audiosocket = живые

    def _распознать_ответ(pcm: bytes) -> str:
        """Голосовой ответ или комментарий опроса (PCM 16 бит 8 кГц) → текст.

        Модель — по умолчанию сервера (или `qc_voice_model`); ждём её не
        дольше пяти секунд и при занятой поднимаем второй экземпляр, как у
        распознавания на лету: клиент на линии ждать очередь не будет.
        """
        import tempfile  # noqa: PLC0415
        import wave  # noqa: PLC0415

        настройки = settings.merged({})
        модель = str(settings.get("qc_voice_model") or "").strip()
        if модель:
            настройки["model"] = модель
        with tempfile.NamedTemporaryFile(suffix=".wav", dir=str(settings.paths.tmp),
                                         delete=False) as файл:
            путь = Path(файл.name)
        try:
            with wave.open(str(путь), "wb") as запись:
                запись.setnchannels(1)
                запись.setsampwidth(2)
                запись.setframerate(8000)
                запись.writeframes(pcm)
            with registry.lease(настройки, wait_s=5.0, отдельный_если_занят=True) as движок:
                итог = движок.transcribe(путь, настройки, None)
        finally:
            путь.unlink(missing_ok=True)
        return " ".join(с.text.strip() for с in итог.segments if с.text).strip()

    state.qc.распознать = _распознать_ответ

    def _закрыть_пул_диктовки() -> None:
        пул = (getattr(state, "stream_pool", None) or {}).get("пул")
        if пул is not None:
            пул.shutdown(wait=False, cancel_futures=True)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        hub.bind(asyncio.get_running_loop())
        if start_queue:
            queue.start()
        мост.start()
        try:
            живые.start()
        except OSError as exc:
            log.error("AudioSocket не запущен: порт %s занят или недоступен: %s",
                      settings.get("audiosocket_port"), exc)
        state.monitoring.start()
        state.content.start()
        state.llm_worker.start()
        state.telephony.start()
        state.qc.start()
        log.info("ASR Hub запущен: %s:%s, каталог данных %s",
                 settings.get("server_host"), settings.get("server_port"),
                 settings.paths.data)
        if settings.hardware_hint:
            log.info("%s", settings.hardware_hint)
        yield
        # Остановка. Первой — очередь: вернуть идущие задания без траты
        # попытки — самое ценное, что здесь делается, а срок на остановку у
        # systemd и Docker конечен. Раньше очередь стояла последней, после
        # последовательных ожиданий мониторинга, разбора, модели и
        # телефонии (до двадцати пяти секунд), и SIGKILL успевал раньше.
        # Каждый шаг — под своей защитой: сбой одного не должен отменять
        # остальные, иначе отметка экземпляра так и висела бы пять минут.
        for название, шаг in (("очередь", queue.stop),
                              ("события соседей", мост.stop),
                              ("AudioSocket", живые.stop),
                              ("мониторинг", state.monitoring.stop),
                              ("разбор содержания", state.content.stop),
                              ("языковая модель", state.llm_worker.stop),
                              ("телефония", state.telephony.stop),
                              ("контроль качества", state.qc.stop),
                              ("журнал доступа", писатель.stop),
                              ("пул диктовки", _закрыть_пул_диктовки),
                              ("модели", lambda: registry.unload_all(кроме_занятых=False))):
            try:
                шаг()
            except Exception as exc:                        # noqa: BLE001
                log.warning("Остановка: «%s» не остановилось штатно: %s", название, exc)
        # Отметка «жив» снимается при штатной остановке: иначе ещё пять
        # минут соседи считали бы этот сервер работающим — и отказывались
        # бы от восстановления базы и смены режима журнала.
        try:
            db.instance_forget()
        except Exception as exc:                            # noqa: BLE001
            log.warning("Отметка экземпляра не снята: %s", exc)
        db.close()
        log.info("ASR Hub остановлен")

    app = FastAPI(
        title="ASR Hub",
        description=DESCRIPTION,
        version=__version__,
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

    def _предел_тела(путь: str, файл: bool = True) -> tuple[int, float, int]:
        """Предел тела запроса в байтах — общий для обеих проверок ниже.

        Файл (multipart) — до `max_upload_mb`; всё остальное — до
        `max_request_mb`. Предел был один на всех, два гигабайта, и JSON
        входа или настроек разбирался в память целиком до проверки ключа:
        анонимный POST в сотни мегабайт поднимал память сервера на гигабайт.
        """
        if not файл:
            limit_mb = float(S.integer(settings, "max_request_mb", 16) or 16)
            return int(limit_mb * 1024 * 1024), limit_mb, 1
        limit_mb = float(settings.get("max_upload_mb") or 2048)
        # Резервная копия — не запись разговора, и предел для звука ей не
        # подходит: перенос на новый сервер архивом больше 2 ГБ получал 413
        # с советом «разбейте файл на части», а собственный предел приёма
        # копии (64 ГБ) был недостижим. Маршрут только для администратора.
        if путь.rstrip("/") == "/api/backup/upload":
            from ..backup import ПРЕДЕЛ_РАСПАКОВКИ  # noqa: PLC0415

            предел_копии = ПРЕДЕЛ_РАСПАКОВКИ + (1 << 20)
            return предел_копии, предел_копии / 1048576, 1
        # Пакетная загрузка везёт несколько файлов одним запросом. Но не
        # max_batch_files предельных файлов разом: при умолчаниях это
        # 2 ГБ × 200 = 400 ГБ во временном каталоге до того, как обработчик
        # увидит первый файл. Пакет — для многих небольших записей; на всё
        # тело — не больше пяти предельных файлов.
        multiplier = 1
        if путь.rstrip("/").endswith("/batch"):
            multiplier = max(1, min(5, int(settings.get("max_batch_files") or 200)))
        # Небольшой запас на границы multipart и остальные поля формы.
        return int(limit_mb * 1024 * 1024) * multiplier + (1 << 20), limit_mb, multiplier

    def _слишком_большое(получено: int, файл: bool, limit_mb: float,
                         multiplier: int) -> ASRHubError:
        if файл:
            return FileTooLarge(получено / 1024 / 1024, int(limit_mb * multiplier))
        return RequestTooLarge(получено / 1024 / 1024, int(limit_mb))

    def _ответ_ошибкой(exc: ASRHubError) -> JSONResponse:
        """Отказ того же вида, что у обработчика ошибок API."""
        RUNTIME.note_error(exc.code, exc.retryable, api=True)
        тело = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content=тело,
                            headers=_повторить_через(exc.http_status, тело))

    #: Что разрешено присылать с телом без ключа: вход, выход и приём
    #: звонков phone_asr (ключ у него в самом теле, `authenticate_body_key`).
    _БЕЗ_КЛЮЧА = ("/api/auth/login", "/api/auth/logout", "/api/process-call")

    async def _допуск_до_тела(scope: dict[str, Any]) -> ASRHubError | None:
        """Проверка доступа ДО чтения тела. None — пускать дальше.

        FastAPI разбирает тело раньше зависимостей, то есть раньше проверки
        ключа: multipart оседал во временном файле целиком, JSON — в памяти,
        и только потом аноним получал 401. Здесь — тот же разбор участника,
        что у `authenticate` (ключ заголовком, Bearer, ключ в адресе, кука
        сессии), но без тела. Роль, пароль по умолчанию и права на маршрут
        проверяет уже сам маршрут: здесь отсекаются только чужие.
        """
        from starlette.requests import HTTPConnection  # noqa: PLC0415

        соединение = HTTPConnection(scope)
        token = token_of(соединение)
        info = settings.api_keys.get(token) if token else None
        if info is not None:
            if info.get("enabled") is False:
                return ForbiddenError("Ключ доступа отключён.")
            return None
        сессия = соединение.cookies.get(SESSION_COOKIE, "")
        if сессия and state.accounts is not None:
            # Сессия — запрос к базе, поэтому в потоке: цикл событий здесь
            # ждать базу не должен (см. журнал доступа).
            запись = await asyncio.to_thread(state.accounts.session_account, сессия)
            if запись is not None:
                return None
        if not token:
            return AuthError("Ключ доступа не передан.")
        return AuthError("Ключ доступа недействителен.",
                         hint="Проверьте, что ключ скопирован целиком (он начинается "
                              "с «ah_») и не отозван.")

    def _предел_входа(scope: dict[str, Any]) -> ASRHubError | None:
        """Предел попыток входа — до чтения тела, а не после разбора JSON.

        Обработчик входа считал попытки сам, но тело к тому времени уже
        было прочитано и разобрано: предел не спасал от тела в сотни
        мегабайт. Здесь та же отметка, а обработчик видит, что она сделана,
        и второй раз не считает.
        """
        from starlette.requests import HTTPConnection  # noqa: PLC0415

        соединение = HTTPConnection(scope)
        адрес_узла = (audit.адрес(соединение.headers, соединение.client,
                                  settings.get("trusted_proxies")) or "неизвестно")
        try:
            state.check_rate(f"login:{адрес_узла}",
                             int(settings.get("login_rate_limit") or 0))
        except ASRHubError as exc:
            return exc
        scope.setdefault("state", {})["login_rate_checked"] = True
        return None

    def _отказ_диалплана(scope: dict[str, Any], путь: str, отказ: ASRHubError) -> None:
        """Отказ в ключе на приёме из диалплана — в его журнал обращений.

        Чужой ключ отсекается здесь, до маршрута, и журнал приёма (раздел
        «АТС» → «Диалплан») его не видел: при наладке станции «неверный
        ключ» — как раз самая нужная строка.
        """
        from ..telephony import dialplan_config  # noqa: PLC0415

        if путь.rstrip("/") != dialplan_config.ПРИЁМ:
            return
        try:
            from starlette.requests import HTTPConnection  # noqa: PLC0415

            from ..telephony import dialplan  # noqa: PLC0415

            соединение = HTTPConnection(scope)
            dialplan.журнал.отметить(
                ip=соединение.client.host if соединение.client else "", key="",
                station="", result="rejected",
                uniqueid=str(соединение.query_params.get("uniqueid") or "")[:64],
                reason=отказ.message, code=отказ.code, status=отказ.http_status)
        except Exception as exc:                            # noqa: BLE001
            log.debug("Отказ диалплана не записан в журнал: %s", exc)

    class _СчётТела:
        """Предел тела по тому, что пришло на самом деле, а не по заголовку.

        Проверка Content-Length ниже закрывает обычного клиента, но заголовок
        можно не прислать: запрос с Transfer-Encoding: chunked разбирался
        FastAPI целиком и оседал во временном файле без предела — и всё это
        ДО проверки ключа: анонимный запрос успевал записать восемь
        мегабайт при пределе в один и только потом получал 401. Здесь
        считаются байты по мере чтения, и чтение обрывается на пределе.

        Здесь же — проверка доступа до чтения тела (`_допуск_до_тела`) и
        предел попыток входа (`_предел_входа`): обе обязаны случиться
        раньше, чем сервер примет хоть байт.
        """

        def __init__(self, inner: Any) -> None:
            self.app = inner

        async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
            if scope.get("type") != "http" or scope.get("method") not in ("POST", "PUT", "PATCH"):
                await self.app(scope, receive, send)
                return
            путь = str(scope.get("path") or "")
            тип = ""
            for имя, значение in scope.get("headers") or ():
                if имя == b"content-type":
                    тип = значение.decode("latin-1").lower()
                    break
            файл = тип.startswith("multipart/form-data")
            предел, limit_mb, multiplier = _предел_тела(путь, файл)

            if settings.get("auth_enabled", True) and путь.startswith("/api/"):
                отказ: ASRHubError | None = None
                if путь == "/api/auth/login":
                    отказ = _предел_входа(scope)
                elif путь not in _БЕЗ_КЛЮЧА:
                    отказ = await _допуск_до_тела(scope)
                if отказ is not None:
                    _отказ_диалплана(scope, путь, отказ)
                    await _ответ_ошибкой(отказ)(scope, receive, send)
                    return
            получено = 0

            async def считать() -> dict[str, Any]:
                nonlocal получено
                сообщение = await receive()
                if сообщение.get("type") == "http.request":
                    получено += len(сообщение.get("body") or b"")
                    if получено > предел:
                        ошибка = _слишком_большое(получено, файл, limit_mb, multiplier)
                        RUNTIME.note_error(ошибка.code, ошибка.retryable, api=True)
                        # HTTPException, а не своё исключение: FastAPI
                        # пропускает его сквозь разбор формы как есть, а
                        # любое другое превращает в «ошибку разбора тела».
                        raise StarletteHTTPException(status_code=413,
                                                     detail=ошибка.to_dict())
                return сообщение

            await self.app(scope, считать, send)

    app.add_middleware(_СчётТела)

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
                файл = (request.headers.get("content-type") or "").lower().startswith(
                    "multipart/form-data")
                limit, limit_mb, multiplier = _предел_тела(request.url.path, файл)
                if int(declared) > limit:
                    error = _слишком_большое(int(declared), файл, limit_mb, multiplier)
                    RUNTIME.note_error(error.code, error.retryable, api=True)
                    # Тело ответа той же формы, что у остальных ошибок API,
                    # иначе клиент не найдёт привычный code и hint.
                    return JSONResponse(status_code=413, content=error.to_dict())
        return await call_next(request)

    @app.middleware("http")
    async def mask_personal_data(request: Request, call_next):
        """Обезличивает ответы API ключу с флагом mask_pii.

        Ключ интеграции или аналитика, которому нужен текст разговора, но
        не персональные данные в нём. Маскируется всё, что несёт текст:
        расшифровка, сегменты, слова, эталон, имя файла, выдержки в отборах,
        найденные телефоны и адреса в разборе. Ответ читается целиком —
        для JSON это единицы килобайт на задание, — а потоки и файлы идут
        мимо: выгрузки маскируются в своём обработчике до сборки файла.
        """
        response = await call_next(request)
        if not settings.get("auth_enabled", True):
            return response
        from ..content import masking  # noqa: PLC0415

        # Кого обезличивать, решает тот же участник, которого нашла проверка
        # доступа (`authenticate` кладёт его в `request.state`). Прежде ключ
        # здесь разбирался заново: сначала своя копия принимала только
        # «Bearer», потом общая — но без одноразового билета, и по
        # `?ticket=` ключ с mask_pii получал полные телефоны и имена во всех
        # ответах JSON. Второй разбор — это второе мнение о том, кто пришёл,
        # и любое расхождение между ними — дыра.
        if not masking.нужно(getattr(request.state, "principal", None)):
            return response
        if not response.headers.get("content-type", "").startswith("application/json"):
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            data = json.loads(body) if body else None
        except (TypeError, ValueError):
            data = None
        if data is None:
            return Response(content=body, status_code=response.status_code,
                            headers=dict(response.headers),
                            media_type=response.media_type)
        new_body = json.dumps(masking.mask_payload(data), ensure_ascii=False).encode("utf-8")
        headers = {k: v for k, v in response.headers.items()
                   if k.lower() not in ("content-length",)}
        return Response(content=new_body, status_code=response.status_code,
                        headers=headers, media_type="application/json")

    @app.middleware("http")
    async def access_journal(request: Request, call_next):
        """Пишет журнал доступа: кто, когда и что сделал.

        Стоит ПОСЛЕ обработчика, а не до: до него неизвестно ни чем кончился
        запрос, ни кто его прислал — участника кладёт в `request.state`
        аутентификация, а она живёт внутри маршрута. Отказы попадают в журнал
        ровно так же, как удачи: чужой подбор пароля — это череда четырёхсот
        первых, и журнал без них отвечает «ничего не было».

        Ошибка записи не должна ронять ответ, который уже готов и верен.
        """
        response = await call_next(request)
        if not settings.get("audit_enabled", True):
            return response
        путь = request.url.path
        if not audit.записывать(request.method, путь,
                                reads=bool(settings.get("audit_reads", False))):
            return response
        участник = getattr(request.state, "principal", None)
        # Имя, названное самим маршрутом, важнее найденного аутентификацией:
        # у входа и выхода аутентификации нет вовсе, а записать их без имени
        # — значит выбросить самые нужные строки журнала.
        назвался = str(getattr(request.state, "audit_actor", "") or "")
        try:
            # Строка уходит в очередь фонового писателя, а не в базу прямо
            # отсюда: синхронная запись из цикла событий вставала на замке
            # записи базы и останавливала весь сервер (см. `audit.Писатель`).
            писатель.записать({
                "action": audit.описать(request.method, путь),
                "actor": назвался or getattr(участник, "name", "") or "аноним",
                "actor_id": getattr(участник, "user_id", "") or "",
                "kind": ("user" if (getattr(участник, "user_id", "") or назвался)
                         else "key" if getattr(участник, "key", "") else "anon"),
                "role": getattr(участник, "role", "") or "",
                "method": request.method, "path": путь,
                "status": int(getattr(response, "status_code", 0) or 0),
                "ip": audit.адрес(request.headers, request.client,
                                  settings.get("trusted_proxies"))})
        except Exception as exc:                            # noqa: BLE001
            log.warning("Журнал доступа не записан: %s", exc)
        return response

    @app.middleware("http")
    async def продлить_куку(request: Request, call_next):
        """Переставляет куку сессии, когда сервер продлил её срок.

        Сервер продлевал сессию при работе, а кука в браузере оставалась со
        сроком от входа: через неделю её выбрасывал сам браузер, и человек,
        работавший каждый день, оказывался снаружи.
        """
        response = await call_next(request)
        продление = getattr(request.state, "session_refresh", None)
        if продление:
            from .routes_auth import _set_cookie  # noqa: PLC0415

            with contextlib.suppress(Exception):
                _set_cookie(request, response, продление[0], продление[1])
        return response

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
            RUNTIME.note_error(exc.code, exc.retryable, api=True)
            return JSONResponse(status_code=exc.http_status, content=exc.to_dict())
        finally:
            RUNTIME.request_finished()
            elapsed = time.perf_counter() - started
            # Метка маршрута берётся из шаблона (/api/jobs/{job_id}), а не из
            # конкретного адреса: иначе метки размножатся по числу заданий и
            # уронят хранилище метрик.
            route = request.scope.get("route")
            path = getattr(route, "path", None) or _route_fallback(request.url.path)
            # Метод — из закрытого перечня, как и маршрут: с h11 (uvicorn без
            # httptools) каждый выдуманный метод анонимного запроса давал
            # новую серию счётчика и гистограммы — триста запросов
            # «M0000…M0299» раздували выдачу метрик с 10 до 375 КБ.
            метод = request.method if request.method in _ИЗВЕСТНЫЕ_МЕТОДЫ else "other"
            labels = {"method": метод, "route": path}
            RUNTIME.inc("asrhub_http_requests_total", {**labels, "status": str(status_code)})
            RUNTIME.observe("asrhub_http_request_seconds", elapsed, labels)
            if status_code == 401:
                RUNTIME.inc("asrhub_auth_failures_total")
            elif status_code == 429:
                RUNTIME.inc("asrhub_rate_limited_total")
        response.headers["X-Process-Time"] = f"{elapsed * 1000:.1f}ms"
        # Заголовки безопасности — на каждый ответ. Сервер отдаёт загруженные
        # файлы как inline (`/audio`, `/redacted`), а ключ интерфейса лежит в
        # localStorage: браузер не должен угадывать тип содержимого, а чужая
        # страница — встраивать интерфейс в рамку. Обратный прокси может
        # выставить свои; заданное приложением он увидит и не задвоит.
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        # Адрес страницы (в нём бывают номер задания и поиск по тексту) не
        # уходит на чужие сайты, а микрофон нужен только диктовке на своей
        # странице.
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), geolocation=(), payment=(), usb=(), "
                                  "microphone=(self)")
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        """Приводит отказы к одному виду.

        Ошибка, поднятая через error_response, уходила клиенту вложенной в
        detail, а та же самая ошибка, вылетевшая из глубины, — полями
        верхнего уровня. Клиент был обязан разбирать оба вида, и об этом
        нигде не говорилось. Теперь поля есть и там, и там: новый клиент
        читает верхний уровень, старый — detail, и никто не ломается.
        """
        detail = exc.detail
        headers = getattr(exc, "headers", None)
        if isinstance(detail, dict) and "code" in detail:
            повтор = _повторить_через(exc.status_code, detail)
            if повтор:
                headers = {**(headers or {}), **повтор}
            return JSONResponse(status_code=exc.status_code,
                                content={**detail, "detail": detail},
                                headers=headers)
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": detail}, headers=headers)

    @app.exception_handler(ASRHubError)
    async def asrhub_error_handler(request: Request, exc: ASRHubError):
        log.warning("Ошибка API %s: %s", exc.code, exc.message)
        # Отказ API — своим счётчиком (см. `Runtime.note_error`).
        RUNTIME.note_error(exc.code, exc.retryable, api=True)
        тело = exc.to_dict()
        return JSONResponse(status_code=exc.http_status, content=тело,
                            headers=_повторить_через(exc.http_status, тело))

    @app.exception_handler(Exception)
    async def unexpected_handler(request: Request, exc: Exception):
        log.exception("Непредвиденная ошибка на %s: %s", request.url.path, exc)
        RUNTIME.note_error("internal_error", retryable=False)
        return JSONResponse(status_code=500, content={
            "code": "internal_error",
            "message": f"Внутренняя ошибка сервера: {type(exc).__name__}",
            "hint": "Подробности в журнале сервера: раздел «Журнал» или файл logs/errors.log",
        })

    app.include_router(auth_router)
    app.include_router(users_router)
    app.include_router(audit_router)
    app.include_router(jobs_router)
    app.include_router(catalog_router)
    app.include_router(system_router)
    app.include_router(health_router)
    app.include_router(monitoring_router)
    app.include_router(content_router)
    app.include_router(review_router)
    app.include_router(llm_router)
    # ИИ-аналитика поверх ответов модели: своды, золотой набор, вопрос к
    # архиву, поиск по смыслу, тренажёр. Пути — свои, без «{раздел}».
    app.include_router(ai_router)
    # Шлюз к модели из сети идёт ПОСЛЕ маршрутов слоя: у них общая
    # приставка «/api/llm», и порядок здесь — единственное, что отделяет
    # «/api/llm/v1/models» (список для чужих клиентов) от «/api/llm/models»
    # (каталог установки). Пути разные, но держать их рядом надёжнее:
    # добавит кто-нибудь «/api/llm/{раздел}» — и перехват станет возможен.
    app.include_router(llm_proxy_router)
    app.include_router(trends_router)
    app.include_router(drill_router)
    app.include_router(voice_router)
    app.include_router(telephony_router)
    # Справочник сотрудников: кто стоит за внутренним номером в журнале АТС.
    app.include_router(employees_router)
    # Маршруты агента идут ПОСЛЕ маршрутов телефонии: «/api/telephony/
    # agents» не должен перехватываться «/api/telephony/{station}».
    app.include_router(agent_router)
    app.include_router(agent_admin_router)
    app.include_router(dialplan_router)
    app.include_router(qc_router)
    app.include_router(manual_router)
    app.include_router(backup_router)
    # Совместимость с phone_asr: маршруты в корне, как у него, и те же под
    # /api — чтобы новые клиенты не выглядели исключением среди прочих.
    app.include_router(phone_router)
    app.include_router(phone_router, prefix="/api")

    def _аккаунт_сессии(websocket: WebSocket, hub_state: Any) -> tuple[Any, str]:
        """Учётная запись по куке сессии для веб-сокета и сам токен — или (None, "").

        Кука уходит и при рукопожатии, поэтому чужая страница могла бы
        открыть сокет от имени вошедшего. SameSite=Lax браузер обычно такого
        не пускает; здесь второй рубеж, тот же, что у запросов с кукой:
        страница-источник должна быть с того же адреса, что и сервер.

        Вызывается в потоке: это запросы к базе, а цикл событий ждать базу
        не должен (см. журнал доступа).
        """
        if hub_state is None or hub_state.accounts is None:
            return None, ""
        token = websocket.cookies.get(SESSION_COOKIE, "")
        if not token:
            return None, ""
        origin = websocket.headers.get("origin") or ""
        host = websocket.headers.get("host") or ""
        if origin and host and origin.split("://", 1)[-1] != host:
            log.warning("Веб-сокет с чужой страницы отклонён: %s", origin)
            return None, ""
        account = hub_state.accounts.session_account(token)
        if account is None:
            return None, ""
        return участник_записи(hub_state, account), token

    async def _участник_сокета(websocket: WebSocket) -> tuple[Any, str, tuple[int, str] | None]:
        """Кто пришёл на веб-сокет: участник, токен сессии и отказ (код, причина).

        Один разбор на /ws и /api/stream, и участник тот же, что у HTTP
        (`deps.участник_*`): владелец, подразделение и обезличивание у
        сокета обязаны совпадать с тем, что тот же ключ получает по REST.
        Два собственных разбора расходились с ним: /ws пускал сессию с
        несменённым паролем по умолчанию и не знал подразделений, а ключу с
        `mask_pii` отдавал события как есть.
        """
        hub_state = getattr(websocket.app.state, "hub", None)
        token = ""
        ticket = websocket.query_params.get("ticket", "")
        if ticket and hub_state is not None:
            token = hub_state.tickets.redeem(ticket)
        if not token:
            token = (websocket.query_params.get("api_key")
                     or websocket.headers.get("x-api-key") or "")
        if not token:
            # Bearer обещан и в заголовке OpenAPI, и в докстрингах маршрутов:
            # сторонний клиент, написанный по документации, иначе получал
            # 4401 «ключ недействителен» на действующем ключе.
            token = token_of(websocket)
        info = settings.api_keys.get(token) if token else None
        if info is not None:
            if info.get("enabled") is False:
                return None, "", (4401, "Ключ доступа отсутствует или недействителен")
            if str(info.get("role") or "") == "metrics":
                # Ключ «только метрики» не получает ни ленты событий, ни
                # диктовки: у него нет прав ни на что, кроме сбора метрик.
                return None, "", (4403, "Ключ только для метрик")
            return участник_ключа(state, token, info), "", None
        # Вход по логину и паролю — кукой сессии. Браузер отправляет куку и при
        # рукопожатии WebSocket, поэтому билет здесь не нужен.
        участник, сессия = await asyncio.to_thread(_аккаунт_сессии, websocket, hub_state)
        if участник is None:
            return None, "", (4401, "Ключ доступа отсутствует или недействителен")
        if участник.must_change_password:
            # Как у REST: пока пароль по умолчанию не сменён, работать нельзя.
            # Лента событий всех заданий по admin/admin123 была доступна без
            # смены пароля и без следа в журнале.
            return None, "", (4403, "Сначала смените пароль по умолчанию")
        return участник, сессия, None

    async def _доступ_есть(подписчик: Подписчик) -> bool:
        if подписчик.key:
            return ключ_действует(state, подписчик.key)
        if подписчик.session and state.accounts is not None:
            запись = await asyncio.to_thread(state.accounts.session_account,
                                             подписчик.session)
            return запись is not None and not запись.must_change_password
        return True

    async def _сторож(websocket: WebSocket, подписчик: Подписчик) -> None:
        """Перепроверяет доступ подписчика, пока сокет открыт.

        Права у сокета проверялись один раз, при рукопожатии: отозванный
        ключ, отключённая запись и закрытая выходом сессия продолжали
        получать ленту, пока открыта вкладка. Отзыв, выход и отключение
        закрывают сокеты сразу (`EventHub.close_where`); перепроверка ловит
        остальное — истёкшую сессию, правку с соседнего экземпляра.
        """
        while True:
            await asyncio.sleep(ПЕРЕПРОВЕРКА_СОКЕТА_С)
            try:
                жив = await _доступ_есть(подписчик)
            except Exception as exc:                        # noqa: BLE001
                log.debug("Доступ веб-сокета не перепроверен: %s", exc)
                continue
            if not жив:
                hub.unregister(websocket)
                hub.unregister_stream(websocket)
                with contextlib.suppress(Exception):
                    await websocket.close(code=4401, reason="Доступ отозван")
                return

    # Распознавание диктовки — в своём пуле потоков, а не в общем пуле
    # `asyncio.to_thread`: тем же общим пулом (на двухъядерной машине — шесть
    # потоков) принимаются файлы, и десяток сессий диктовки занимал его
    # целиком — загрузки висели до своих тайм-аутов. Сессия зовёт модель
    # последовательно, поэтому потока на сессию хватает: пул размером с
    # предел сессий не даёт им отнимать потоки друг у друга.
    пул_диктовки: dict[str, Any] = {}

    async def _в_потоке_диктовки(функция: Any, *аргументы: Any) -> Any:
        from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

        пул = пул_диктовки.get("пул")
        if пул is None:
            размер = S.integer(settings, "stream_max_sessions", 16) or 16
            пул = пул_диктовки["пул"] = ThreadPoolExecutor(
                max_workers=max(2, min(размер, 256)), thread_name_prefix="asrhub-stream")
        return await asyncio.get_running_loop().run_in_executor(
            пул, functools.partial(функция, *аргументы))

    state.stream_pool = пул_диктовки

    ws_router = APIRouter()

    @ws_router.websocket("/api/stream")
    async def stream_endpoint(websocket: WebSocket):
        """Распознавание на лету: звук кусками внутрь, текст по ходу наружу.

        Управление — текстовые сообщения JSON, звук — двоичные кадры.
        Первое сообщение может задать модель, язык и формат звука; сессия
        отвечает «ready» и сообщает, работает она настоящим потоком или
        скользящим окном. «finish» завершает и отдаёт окончательный текст.

        Ключ предъявляется так же, как на /ws: одноразовым билетом в
        параметре ticket (браузер заголовки в WebSocket задать не может)
        либо ключом в заголовке или параметре — для сторонних клиентов.

        Поток подчиняется тем же правилам, что и загрузка: лимиту частоты,
        суточной квоте часов звука (расход записывается на владельца) и
        пределам числа сессий — на сервер и на участника.
        """
        from .routes_jobs import СУТКИ_С, проверить_квоту, расход, учесть_расход  # noqa: PLC0415

        # Сначала принимаем соединение, потом отказываем кодом. Закрытие до
        # accept() uvicorn превращает в HTTP 403 на рукопожатии, браузер
        # видит код 1006 — и сообщения интерфейса для 4401/4403/4404 не
        # показывались никогда: статус висел на «запрашиваем микрофон…».
        await websocket.accept()

        if not settings.get("stream_enabled", True):
            await websocket.close(
                code=4404, reason="Потоковое распознавание выключено (stream_enabled)")
            return

        участник: Any = None
        подписчик = Подписчик(owner="без аутентификации", is_admin=True)
        if settings.get("auth_enabled", True):
            участник, сессия, отказ = await _участник_сокета(websocket)
            if отказ is not None:
                await websocket.close(code=отказ[0], reason=отказ[1])
                return
            подписчик = Подписчик.из_участника(участник, session=сессия)
            # Поток — это работа, а не чтение: ключу только для чтения он
            # закрыт, как и постановка задания.
            if not участник.can_write:
                await websocket.close(code=4403,
                                      reason="Ключ доступа работает только на чтение")
                return
            # Лимит частоты и суточная квота — те же, что у загрузки файла:
            # раньше поток шёл мимо обоих, и ключ с квотой в одно задание
            # распознавал через него сколько угодно звука.
            try:
                if участник.key:
                    info = settings.api_keys.get(участник.key) or {}
                    state.check_rate(участник.key, int(
                        info.get("rate_limit") or settings.get("rate_limit_per_minute") or 0))
                else:
                    state.check_rate(f"user:{участник.user_id}",
                                     int(settings.get("rate_limit_per_minute") or 0))
                await asyncio.to_thread(проверить_квоту, state, участник,
                                        incoming_jobs=0)
            except ASRHubError as exc:
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "error", **exc.to_dict()}, ensure_ascii=False))
                await websocket.close(code=4429, reason=exc.message[:120])
                return
        principal_name = подписчик.owner

        # Пределы числа сессий: на сервер и на участника. Без них десяток
        # медленных сессий занимал общий пул потоков, которым принимаются
        # файлы, — и загрузки висели до своих тайм-аутов.
        всего = S.integer(settings, "stream_max_sessions", 16)
        на_участника = S.integer(settings, "stream_max_sessions_per_owner", 4)
        открыто = hub.streams()
        свои = sum(1 for п in открыто if п.owner == подписчик.owner)
        if (всего and len(открыто) >= всего) or (
                на_участника and settings.get("auth_enabled", True) and свои >= на_участника):
            причина = (f"Открыто сессий диктовки: {len(открыто)} при пределе {всего} на сервер"
                       if всего and len(открыто) >= всего else
                       f"У «{подписчик.owner}» уже открыто сессий диктовки: {свои} "
                       f"при пределе {на_участника}")
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(
                    {"type": "error", "code": "stream_limit", "message": причина,
                     "hint": "Закройте лишние вкладки с диктовкой или поднимите "
                             "stream_max_sessions / stream_max_sessions_per_owner."},
                    ensure_ascii=False))
            await websocket.close(code=4429, reason="Слишком много сессий диктовки")
            return
        hub.register_stream(websocket, подписчик)
        сторож = (asyncio.create_task(_сторож(websocket, подписчик))
                  if settings.get("auth_enabled", True) else None)

        # Квота часов звука проверяется и по ходу сессии: расход на открытии
        # плюс то, что уже пришло. Звук можно слать быстрее реального
        # времени, и час сессии — не час звука.
        предел_часов = float(getattr(участник, "quota_audio_hours_per_day", 0) or 0) \
            if участник is not None and not участник.is_admin else 0.0
        было_часов = 0.0
        if предел_часов:
            scope = scope_owner(участник) or участник.name
            было_часов = float((await asyncio.to_thread(
                расход, state, scope, time.time() - СУТКИ_С))["audio_hours"])

        def наружу(событие: Any) -> str:
            данные = событие.to_dict() if hasattr(событие, "to_dict") else событие
            if подписчик.mask:
                # Распознанный текст — те же персональные данные, что и
                # расшифровка: ключу с mask_pii он уходит обезличенным.
                from ..content import masking  # noqa: PLC0415

                данные = masking.mask_payload(данные)
            return json.dumps(данные, ensure_ascii=False)

        session = None
        try:
            # Первое сообщение — с пределом ожидания: молчащий сокет держал
            # место среди сессий бесконечно.
            try:
                first = await asyncio.wait_for(websocket.receive(),
                                               timeout=ПЕРВОЕ_СООБЩЕНИЕ_ПОТОКА_С)
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    await websocket.close(code=4408, reason="Не дождались начала потока")
                return
            if first.get("type") == "websocket.disconnect":
                # Клиент ушёл, ничего не прислав: отказал браузеру в микрофоне,
                # закрыл вкладку. Модель для него не грузим, трассировку в
                # журнал ошибок не пишем — это не ошибка.
                log.info("Поток закрыт до начала: %s", principal_name)
                return
            config: dict[str, Any] = {}
            pending: bytes = b""
            if first.get("text"):
                try:
                    config = json.loads(first["text"]) or {}
                except json.JSONDecodeError:
                    config = {}
                if not isinstance(config, dict):
                    config = {}
            elif first.get("bytes"):
                pending = first["bytes"]

            overrides = {k: v for k, v in config.items()
                         if k not in ("type", "format")}
            settings.выражения_задания(overrides)
            merged = settings.merged(overrides)
            session = StreamSession(
                state.registry, merged,
                source_format=str(config.get("format") or "pcm_s16le"),
                workdir=Path(settings.get("temp_dir") or settings.paths.tmp),
                llm=getattr(state, "llm", None))

            ready = await _в_потоке_диктовки(session.start)
            await websocket.send_text(наружу(ready))
            log.info("Поток открыт: %s, модель %s", principal_name,
                     merged.get("model"))
            if pending:
                for event in await _в_потоке_диктовки(session.feed, pending):
                    await websocket.send_text(наружу(event))

            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes"):
                    # Распознавание — работа для потока, а не для цикла
                    # событий: иначе сервер перестаёт отвечать всем
                    # остальным на время каждого куска.
                    events = await _в_потоке_диктовки(session.feed, message["bytes"])
                    if предел_часов and было_часов + session.duration_s / 3600 > предел_часов:
                        for event in events:
                            await websocket.send_text(наружу(event))
                        from ..errors import QuotaExceeded  # noqa: PLC0415

                        исчерпано = QuotaExceeded(
                            "audio_hours",
                            round(было_часов + session.duration_s / 3600, 3), предел_часов)
                        await websocket.send_text(json.dumps(
                            {"type": "error", **исчерпано.to_dict()}, ensure_ascii=False))
                        for event in await _в_потоке_диктовки(session.finish):
                            await websocket.send_text(наружу(event))
                            if event.type == "done":
                                await asyncio.to_thread(_record_stream, state, merged, event)
                        break
                elif message.get("text"):
                    try:
                        command = json.loads(message["text"]) or {}
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(command, dict):
                        continue
                    kind = str(command.get("type") or "")
                    if kind in ("finish", "stop", "eof"):
                        for event in await _в_потоке_диктовки(session.finish):
                            await websocket.send_text(наружу(event))
                            if event.type == "done":
                                # Метрики потока — запись в базу: в потоке.
                                await asyncio.to_thread(_record_stream, state, merged, event)
                        break
                    if kind == "ping":
                        await websocket.send_text('{"type":"pong"}')
                    continue
                else:
                    continue
                for event in events:
                    await websocket.send_text(наружу(event))
                # Сессия закрылась сама — предел длительности или отказы
                # распознавания подряд (см. `StreamSession._отказ`): `done`
                # ушёл, и соединение закрывается тоже. Иначе каждый
                # следующий кусок звука получал «Сессия уже завершена».
                итог = next((event for event in events if event.type == "done"), None)
                if итог is not None:
                    # Метрики потока — запись в базу: в потоке.
                    await asyncio.to_thread(_record_stream, state, merged, итог)
                    break
        except WebSocketDisconnect:
            log.info("Поток закрыт клиентом: %s", principal_name)
        except RuntimeError as exc:
            if WebSocketState.DISCONNECTED in (websocket.client_state,
                                               websocket.application_state):
                # Сокет закрыт с нашей стороны (доступ отозван) или клиент
                # ушёл между проверками — это конец сессии, а не сбой.
                log.info("Поток закрыт: %s (%s)", principal_name, exc)
            else:
                log.exception("Поток прерван: %s", exc)
                with contextlib.suppress(Exception):
                    await websocket.send_text(json.dumps(
                        {"type": "error", "message": str(exc)}, ensure_ascii=False))
        except ASRHubError as exc:
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(
                    {"type": "error", **exc.to_dict()}, ensure_ascii=False))
        except Exception as exc:                        # noqa: BLE001
            log.exception("Поток прерван: %s", exc)
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps(
                    {"type": "error", "message": str(exc)}, ensure_ascii=False))
        finally:
            hub.unregister_stream(websocket)
            if сторож is not None:
                сторож.cancel()
            # Сессия держит буфер, процесс ffmpeg и состояние движка —
            # оборванное соединение не должно оставлять их висеть.
            if session is not None:
                # Расход потока — на владельца, как у загрузки: GET
                # /api/usage его видит, и суточная квота часов звука его
                # учитывает (см. `routes_jobs.учесть_расход`). Записывается
                # до закрытия сессии: отмена обработчика (остановка сервера)
                # посреди закрытия иначе теряла расход целиком, а запись,
                # уже отданная потоку, доходит до базы и при отмене.
                длительность = session.duration_s
                if участник is not None and not участник.is_admin and длительность:
                    await asyncio.to_thread(учесть_расход, state, участник.name,
                                            audio_s=длительность)
                await _в_потоке_диктовки(session.close)
            with contextlib.suppress(Exception):
                await websocket.close()

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
            участник, сессия, отказ = await _участник_сокета(websocket)
            if отказ is not None:
                await websocket.close(code=отказ[0], reason=отказ[1])
                return
            подписчик = Подписчик.из_участника(участник, session=сессия)
        else:
            # Проверка ключей выключена — разделять некого, все равны.
            подписчик = Подписчик(owner="", is_admin=True)

        await hub.register(websocket, подписчик)
        сторож = (asyncio.create_task(_сторож(websocket, подписчик))
                  if settings.get("auth_enabled", True) else None)
        try:
            # Состояние очереди — десяток запросов к базе: в потоке, а не в
            # цикле событий (на ста тысячах заданий это десятки миллисекунд
            # на каждое подключение и каждый «status»).
            состояние = await asyncio.to_thread(queue.status)
            await websocket.send_text(json.dumps({
                "type": "hello",
                "queue": состояние,
                "history": hub.history_for(подписчик)[-20:],
            }, ensure_ascii=False, default=str))
            while True:
                message = await websocket.receive_text()
                # На каждое сообщение клиента — та же перепроверка доступа, что
                # у сторожа: ответ «pong» отозванному ключу — тоже утечка.
                if settings.get("auth_enabled", True) and not await _доступ_есть(подписчик):
                    hub.unregister(websocket)
                    await websocket.close(code=4401, reason="Доступ отозван")
                    break
                if message == "ping":
                    await websocket.send_text('{"type":"pong"}')
                elif message == "status":
                    состояние = await asyncio.to_thread(queue.status)
                    await websocket.send_text(json.dumps(
                        {"type": "queue", **состояние}, ensure_ascii=False, default=str))
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("WebSocket закрыт: %s", exc)
        finally:
            hub.unregister(websocket)
            if сторож is not None:
                сторож.cancel()

    app.include_router(ws_router)

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if web_dir.exists():
        app.mount("/static", _CachedStatic(directory=str(web_dir)), name="static")

        @app.get("/", include_in_schema=False)
        async def index():
            # Страница отдаётся с запретом на кеш и со ссылками, в которых
            # стоит отпечаток содержимого. Почему так — см. `_asset_version`.
            return HTMLResponse(_index_html(web_dir),
                                headers={"Cache-Control": "no-cache",
                                         "Content-Security-Policy": _ПОЛИТИКА_СТРАНИЦЫ})

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
    app.add_middleware(_НомерЗапроса)
    # Сжатие — самым внешним слоем, после всех прочих прослоек.
    app.add_middleware(_Сжатие)

    return app


#: Политика содержимого главной страницы. Скрипты — только файлами с самого
#: сервера: встроенных обработчиков (onclick="…") в разметке больше нет
#: (data-act, см. app.js), и внедрённая строка кодом не станет. Запросы,
#: картинки и звук — тоже только к самому серверу. Встроенные стили
#: остаются ('unsafe-inline' у style-src): ими раскрашены полосы и графики,
#: а исполнить через стиль ничего нельзя.
_ПОЛИТИКА_СТРАНИЦЫ = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; connect-src 'self' ws: wss:; font-src 'self' data:; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


class _НомерЗапроса:
    """Сквозной номер запроса: в ответе (X-Request-ID) и в каждой строке журнала.

    Номер берётся из заголовка X-Request-ID запроса (его ставят прокси и
    балансировщики — тогда номер один на всём пути), если он похож на номер:
    латиница, цифры, «.-_:», до 64 знаков. Иначе — свой. Жалоба «загрузка
    упала в 14:03» превращается в поиск по номеру из ответа, а не в чтение
    всего журнала за минуту.
    """

    _ГОДЕН = re.compile(r"^[A-Za-z0-9._:\-]{8,64}$")

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        from ..logging_setup import НОМЕР_ЗАПРОСА  # noqa: PLC0415

        присланный = ""
        for ключ, значение in scope.get("headers") or []:
            if ключ.lower() == b"x-request-id":
                присланный = значение.decode("latin-1", "replace").strip()
                break
        номер = присланный if self._ГОДЕН.match(присланный) else uuid.uuid4().hex[:16]
        метка = НОМЕР_ЗАПРОСА.set(номер)
        scope.setdefault("state", {})["request_id"] = номер

        async def отправить(сообщение: Any) -> None:
            if сообщение.get("type") == "http.response.start":
                заголовки = [(к, з) for к, з in сообщение.get("headers") or []
                             if к.lower() != b"x-request-id"]
                заголовки.append((b"x-request-id", номер.encode("ascii")))
                сообщение = {**сообщение, "headers": заголовки}
            await send(сообщение)

        try:
            await self.app(scope, receive, отправить)
        finally:
            НОМЕР_ЗАПРОСА.reset(метка)


class _Сжатие:
    """gzip для статики и ответов API.

    `app.js` — 1,35 МБ, сжатый — 340 КБ, а сценарий «с удалённого
    компьютера» через VPN — главный. Мимо сжатия идут: запросы с Range
    (перемотка записи — частичные ответы), веб-сокеты и всё, что не текст:
    звук, архивы, выгрузки XLSX и DOCX (они уже сжаты внутри).
    """

    def __init__(self, app: Any) -> None:
        from starlette.middleware.gzip import GZipMiddleware  # noqa: PLC0415

        self.app = app
        исключить = ("audio/*", "video/*", "image/png", "image/jpeg", "image/webp",
                     "application/zip", "application/gzip", "application/octet-stream",
                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                     "text/event-stream")
        try:
            self.сжать = GZipMiddleware(app, minimum_size=1024, compresslevel=6,
                                        exclude_content_types=исключить)
        except TypeError:
            # Starlette до 0.47 не знает exclude_content_types: сжимает всё
            # по minimum_size, а звук отсекается проверкой пути ниже.
            self.сжать = GZipMiddleware(app, minimum_size=1024, compresslevel=6)
            self._старый = True
        else:
            self._старый = False

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        заголовки = {к.lower(): з for к, з in scope.get("headers") or []}
        путь = str(scope.get("path") or "")
        if b"range" in заголовки or (self._старый and any(
                ч in путь for ч in ("/audio", "/download", "/export", "/backup", "/redacted"))):
            await self.app(scope, receive, send)
            return
        await self.сжать(scope, receive, send)
