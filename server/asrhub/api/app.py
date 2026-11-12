"""Сборка приложения FastAPI: middleware, обработчики ошибок, WebSocket, статика."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

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
from ..telephony import Телефония
from ..trends import Trends
from .deps import (
    SESSION_COOKIE,
    AppState,
    token_of,
    ключ_действует,
)
from .event_hub import (  # noqa: F401 — прежние импорты из api.app
    ИСТОРИЯ_СОБЫТИЙ,
    ОТПРАВКА_СОБЫТИЯ_С,
    ПЕРЕПРОВЕРКА_СОКЕТА_С,
    ПРОГРЕСС_НЕ_ЧАЩЕ_С,
    EventHub,
    Подписчик,
)
from .middleware import _ПОЛИТИКА_СТРАНИЦЫ, _НомерЗапроса, _Сжатие  # noqa: F401
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
from .routes_ws import (  # noqa: F401 — прежние импорты из api.app
    ПЕРВОЕ_СООБЩЕНИЕ_ПОТОКА_С,
    _record_stream,
    подключить_сокеты,
)

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

    #: Настройки уже не None — для вложенных классов, где проверка типов
    #: этого не видит.
    настройки: Settings = settings

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

            if настройки.get("auth_enabled", True) and путь.startswith("/api/"):
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
                        raise StarletteHTTPException(
                            status_code=413,
                            detail=ошибка.to_dict())  # type: ignore[arg-type]  # словарь ошибки, см. http_error_handler
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

    # Веб-сокеты: лента событий /ws и диктовка /api/stream (см. `routes_ws`).
    подключить_сокеты(app, settings=settings, state=state, hub=hub, queue=queue)

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
