"""Маршруты раздела «Тренды»: ряды по времени, часы недели, связи, выгрузка.

Разрез по владельцу тот же, что у остальной аналитики: обычный ключ видит
свои записи, ключ в группе — записи группы, администратор — всё.
Показатели железа считаются по серверу целиком, поэтому отдаются только
администратору: ключу подразделения нечего делать с температурой карты, а
знать её — уже разведка.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response

from ..errors import ASRHubError, ConfigError
from ..trends import PERIODS, Trends
from .deps import Principal, authenticate, error_response, get_state, scope_owner

router = APIRouter(prefix="/api/trends", tags=["Тренды"])

ПЕРИОД = Query(default="month", pattern="^(day|week|month|quarter|year)$")
ШАГ = Query(default="auto", pattern="^(auto|hour|day|week|month)$")


def _слой(request: Request) -> Trends:
    state = get_state(request)
    слой = getattr(state, "trends", None)
    if слой is None:
        слой = Trends(state.db)
        state.trends = слой
    return слой


def _показатели(metrics: str | None) -> list[str]:
    """Список показателей из строки запроса; пусто — значит все."""
    if not metrics:
        return []
    return [м.strip() for м in metrics.split(",") if м.strip()][:60]


@router.get("/catalog", summary="Каталог показателей трендов")
def catalog(request: Request,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что вообще можно построить: показатели по группам, шаги, периоды."""
    return _слой(request).catalog(is_admin=principal.is_admin)


@router.get("", summary="Ряды показателей по времени")
def series(request: Request, period: str = ПЕРИОД, bucket: str = ШАГ,
           metrics: str | None = Query(default=None),
           compare: bool = Query(default=True),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Ряды выбранных показателей с общими корзинами времени.

    Без `metrics` отдаются все — это и есть «как менялось всё сразу».
    `compare=true` добавляет к каждому ряду такой же ряд предыдущего
    периода: «выросло» без «по сравнению с чем» — не утверждение.
    """
    try:
        return _слой(request).series(
            period, bucket=bucket, metrics=_показатели(metrics),
            owner=scope_owner(principal), compare=compare,
            is_admin=principal.is_admin)
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/heatmap", summary="Показатель по часам недели")
def heatmap(request: Request, metric: str = Query(...), period: str = ПЕРИОД,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Семь строк на двадцать четыре столбца: когда именно это происходит.

    Средние по периоду прячут то, что видно только здесь: очередь растёт
    не «вообще», а в понедельник с девяти до одиннадцати.
    """
    try:
        return _слой(request).heatmap(metric, period, owner=scope_owner(principal),
                                      is_admin=principal.is_admin)
    except KeyError as exc:
        raise error_response(ConfigError(
            f"Неизвестный показатель «{metric}».",
            hint="Список: GET /api/trends/catalog.")) from exc


@router.get("/correlations", summary="Показатели, которые движутся вместе")
def correlations(request: Request, period: str = ПЕРИОД, bucket: str = ШАГ,
                 metrics: str | None = Query(default=None),
                 limit: int = Query(default=12, ge=1, le=50),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Пары рядов с наибольшей связью — как повод посмотреть, а не как вывод.

    Связь не означает причину, и раздел этого не обещает. Пары считаются
    только там, где у обоих рядов есть хотя бы восемь общих непустых
    корзин: совпадение двух точек выглядит как закон природы, а им не
    является.
    """
    return _слой(request).correlations(
        period, bucket=bucket, metrics=_показатели(metrics),
        owner=scope_owner(principal), limit=limit, is_admin=principal.is_admin)


@router.get("/export", summary="Выгрузка трендов в таблицу")
def export(request: Request, period: str = ПЕРИОД, bucket: str = ШАГ,
           metrics: str | None = Query(default=None),
           fmt: str = Query(default="xlsx", pattern="^(xlsx|csv)$"),
           principal: Principal = Depends(authenticate)) -> Any:
    """Те же ряды книгой Excel или архивом CSV: строка на корзину времени."""
    from ..analytics_export import _собрать_csv, _собрать_xlsx  # noqa: PLC0415
    from .routes_jobs import content_disposition  # noqa: PLC0415

    свод = _слой(request).series(period, bucket=bucket, metrics=_показатели(metrics),
                                 owner=scope_owner(principal), compare=False,
                                 is_admin=principal.is_admin)
    ряды = свод["series"]

    def заголовок(ряд: dict[str, Any]) -> str:
        единица = str(ряд.get("unit") or "")
        return f"{ряд['label']}, {единица}" if единица else str(ряд["label"])

    заголовки = ["Время", *[заголовок(р) for р in ряды]]
    строки: list[list[Any]] = []
    for i, ts in enumerate(свод["buckets"]):
        строки.append([time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
                       *[(р["values"][i] if i < len(р["values"]) else None) for р in ряды]])
    свод_строки = [["Показатель", "Единица", "Среднее", "Минимум", "Максимум",
                    "Последнее", "Изменение, %", "Вердикт"]]
    свод_строки += [[с["label"], с["unit"], с["avg"], с["min"], с["max"], с["last"],
                     с["change_percent"], с["verdict"]] for с in свод["summary"]]
    таблицы = [("Ряды", заголовки, строки),
               ("Свод", свод_строки[0], свод_строки[1:])]
    метка = time.strftime("%Y-%m-%d")
    if fmt == "csv":
        тело = _собрать_csv(таблицы, period)
        имя, тип = f"asrhub-тренды-{period}-{метка}.zip", "application/zip"
    else:
        try:
            тело = _собрать_xlsx(таблицы, f"ASR Hub — тренды за период «{period}»")
        except ASRHubError as exc:
            raise error_response(exc) from exc
        имя = f"asrhub-тренды-{period}-{метка}.xlsx"
        тип = ("application/vnd.openxmlformats-officedocument."
               "spreadsheetml.sheet")
    return Response(content=тело, media_type=тип, headers={
        "Content-Disposition": content_disposition(имя),
        "Cache-Control": "no-store",
    })


#: Периоды раздела — те же, что у аналитики; отдаём их наружу для проверок.
__all__ = ["PERIODS", "router"]
