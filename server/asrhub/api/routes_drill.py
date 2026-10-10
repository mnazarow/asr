"""Маршрут раскрытия: список записей или звонков, из которых сложилось число.

Одна ручка на все разделы аналитики. Свод отдаёт число и своё окно, а
интерфейс по щелчку спрашивает здесь «какие именно» — тем же окном и тем же
отбором. Разрез по владельцу тот же, что у сводов: обычный ключ видит свои
записи, ключ в группе — записи группы, администратор — всё; иначе число из
своего отчёта раскрывалось бы в чужие разговоры.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..drill import ПРЕДЕЛ_СТРАНИЦЫ, СТРАНИЦА, Drill, Отказ, describe
from ..errors import ASRHubError
from .deps import Principal, authenticate, error_response, get_state, scope_owner

router = APIRouter(prefix="/api/drill", tags=["Аналитика"])

#: Поля строки списка, в которых бывает телефон или имя клиента.
_ЛИЧНОЕ = ("filename", "src", "dst")


def _слой(request: Request) -> Drill:
    state = get_state(request)
    слой = getattr(state, "drill", None)
    if слой is None:
        слой = Drill(state.db)
        state.drill = слой
    return слой


@router.get("/filters", summary="Чем можно отобрать список раскрытия")
def filters(principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Основы, отборы и величины, которые понимает `GET /api/drill`."""
    return describe()


@router.get("", summary="Список записей, из которых сложилось число")
def page(request: Request,
         base: str = Query(default="jobs", pattern="^(jobs|content|llm|calls)$"),
         since: float | None = Query(default=None, ge=0),
         until: float | None = Query(default=None, ge=0),
         f: list[str] = Query(default=[], max_length=24),
         sort: str = Query(default="", max_length=64),
         limit: int = Query(default=СТРАНИЦА, ge=1, le=ПРЕДЕЛ_СТРАНИЦЫ),
         cursor: str = Query(default="", max_length=600),
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Страница списка: лёгкие поля записи и ссылка на продолжение.

    - `base` — что перечисляем: `jobs` (задания), `content` (разобранные
      записи), `llm` (записи с ответом языковой модели), `calls` (журнал
      звонков, в том числе без записи);
    - `since`, `until` — границы окна в секундах эпохи, нижняя входит,
      верхняя нет. Их отдаёт сам свод (`window`), чтобы список совпал с
      числом, а не с тем, что набежало с тех пор;
    - `f` — отбор, можно несколько: `f=model:whisper&f=status:failed`.
      Перечень — в `GET /api/drill/filters`;
    - `sort` — `time` (по умолчанию) или величина: `duration:desc`;
    - `cursor` — поле `next` прошлого ответа, чтобы продолжить.

    `total` считается только на первой странице и не дальше десяти тысяч
    (`total_capped`). Если запрос не уложился в срок, вернётся
    `timed_out: true` и пустой список — сузьте период.
    """
    try:
        if any(len(отбор) > 300 for отбор in f):
            raise Отказ("Отбор слишком длинный.", hint="Значение отбора — до 300 знаков.")
        ответ = _слой(request).page(
            base=base, since=since, until=until, filters=f,
            owner=scope_owner(principal), sort=sort, limit=limit, cursor=cursor)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    if principal.mask_pii:
        # Посредник обезличивания видит только ключ из заголовка; вошедшему
        # по билету или куке с тем же флагом он бы отдал номера как есть.
        from ..content import masking  # noqa: PLC0415

        for строка in ответ["items"]:
            for поле in _ЛИЧНОЕ:
                if isinstance(строка.get(поле), str):
                    строка[поле] = masking.mask_text(строка[поле])
    return ответ


__all__ = ["router"]
