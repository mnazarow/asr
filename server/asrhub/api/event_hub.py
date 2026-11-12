"""Лента событий для веб-сокетов: кто подписан, что ему положено видеть.

Вынесено из `api/app.py`: рассылка — отдельная забота, а модуль
сборки приложения был самым большим в сервере. `api.app` по-прежнему
отдаёт эти имена — для прежних импортов.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

from ..monitoring import RUNTIME

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
                                         key=lambda к: self._прогресс_отправлен[к])[:2048]:
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
