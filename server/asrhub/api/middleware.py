"""Прослойки приложения: номер запроса, сжатие и политика содержимого.

Вынесено из `api/app.py`. Проверка доступа до чтения тела и
предел тела остаются в `create_app`: им нужны настройки и состояние
приложения из замыкания.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

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
