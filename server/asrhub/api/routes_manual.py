"""Раздел «Документация»: главы руководства, картинки и поиск.

Путь — /api/manual, потому что /api/docs занят страницей Swagger.
Главы читаются из каталога docs той версии, что установлена (см.
`asrhub.manual`), и отдаются готовым безопасным HTML. Картинки глав —
без ключа, как статика интерфейса: тег <img> ключ передать не может.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import manual
from ..errors import JobNotFound
from .deps import Principal, authenticate, error_response, get_state

router = APIRouter(prefix="/api/manual", tags=["Документация"])


def _нет(что: str, подсказка: str) -> Any:
    ошибка = JobNotFound(что)
    ошибка.message = что
    ошибка.hint = подсказка
    return error_response(ошибка)


@router.get("", summary="Главы руководства")
async def chapters(request: Request,
                   principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Оглавление: главы с номером по порядку, затем дополнения."""
    state = get_state(request)
    папка = manual.каталог()
    return {"available": папка is not None,
            "chapters": [г.to_dict() for г in await asyncio.to_thread(manual.главы)],
            "version": str(getattr(state, "version", "") or "")}


@router.get("/chapter/{slug}", summary="Глава руководства")
async def chapter(slug: str, principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Глава готовым HTML, её оглавление (заголовки 2-го и 3-го уровня) и
    соседние главы."""
    данные = await asyncio.to_thread(manual.глава, slug)
    if данные is None:
        raise _нет(f"Главы «{slug}» нет.", "Откройте оглавление раздела «Документация».")
    return данные


@router.get("/raw/{slug}", summary="Глава руководства в Markdown",
            response_class=PlainTextResponse)
async def raw(slug: str, principal: Principal = Depends(authenticate)) -> PlainTextResponse:
    путь = manual.найти(slug)
    if путь is None:
        raise _нет(f"Главы «{slug}» нет.", "Откройте оглавление раздела «Документация».")
    текст = await asyncio.to_thread(путь.read_text, encoding="utf-8")
    return PlainTextResponse(текст, media_type="text/markdown; charset=utf-8", headers={
        "Content-Disposition": f"inline; filename*=UTF-8''{путь.name}"})


@router.get("/search", summary="Поиск по руководству")
async def search(q: str = Query(..., min_length=2, max_length=200,
                                description="Слова, которые должны быть в строке"),
                 limit: int = Query(50, ge=1, le=200),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Строки глав, где есть все слова запроса, — с заголовком, под которым
    они стоят, и якорем для перехода."""
    найдено = await asyncio.to_thread(manual.искать, q, предел=limit)
    return {"query": q, "items": найдено}


@router.get("/images/{name}", summary="Картинка из руководства")
async def image(name: str) -> FileResponse:
    найдено = manual.картинка(name)
    if найдено is None:
        raise _нет("Такой картинки в руководстве нет.", "")
    путь, вид = найдено
    # SVG — тоже документ: открытая отдельно картинка не должна исполнять
    # ничего от имени сервера.
    return FileResponse(путь, media_type=вид, headers={
        "Cache-Control": "public, max-age=3600",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"})
