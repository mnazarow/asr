"""Веб-сокеты: лента событий `/ws` и распознавание на лету `/api/stream`.

Вынесено из `api/app.py`: сборка приложения зовёт `подключить_сокеты` с
теми же настройками, состоянием, лентой и очередью, что раньше видели
обработчики через замыкание.
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import time
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from .. import settings_access as S
from ..config import Settings
from ..errors import ASRHubError
from ..job_queue import JobQueue
from ..logging_setup import get_logger
from ..streaming import StreamSession
from .deps import (
    SESSION_COOKIE,
    scope_owner,
    token_of,
    ключ_действует,
    участник_записи,
    участник_ключа,
)
from .event_hub import ПЕРЕПРОВЕРКА_СОКЕТА_С, EventHub, Подписчик

log = get_logger("app")

#: Сколько ждать первого сообщения сессии диктовки. Молчащий сокет держал
#: место среди сессий (и поток сервера) бесконечно.
ПЕРВОЕ_СООБЩЕНИЕ_ПОТОКА_С = 30.0


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



def подключить_сокеты(app: FastAPI, *, settings: Settings, state: Any, hub: EventHub,
                      queue: JobQueue) -> None:
    """Маршруты веб-сокетов приложения и пул потоков диктовки (`state.stream_pool`)."""
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
                # учитывает (см. `routes_jobs.учесть_расход`).
                #
                # Уборка — под защитой от отмены. Отменённый обработчик
                # (остановка сервера; тестовый клиент отменяет его сразу после
                # закрытия сокета) снимал из пула потоков ещё не начатую
                # работу: `await` на отменённой задаче отменяет и её будущее в
                # пуле, — расход терялся, а сессия с процессом ffmpeg не
                # закрывалась. Работа короткая: запись строки и закрытие.
                длительность = session.duration_s
                with anyio.CancelScope(shield=True):
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
