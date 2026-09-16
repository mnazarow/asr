"""Маршруты раздела «Сотрудники»: справочник, импорт выгрузки, связь со звонками.

Справочник отвечает на один вопрос: кто стоит за внутренним номером. Пока
его нет, в отчётах, разрезах и карточках разговоров вместо человека стоит
«1043», и каждый, кто читает отчёт, идёт выяснять это в чужую таблицу.

Кто что может. Читать справочник может любой ключ: это внутренний
телефонный список, а не секрет, и прятать от сотрудника фамилию соседа
незачем. Заводить и править карточки — ключ с правом записи. Удалять и
импортировать — только администратор: импорт переписывает справочник
целиком и может пометить уволенными всех разом, а удаление карточки
оставляет её звонки без имени.

Счётчики звонков во всех ответах считаются в разрезе владельца — так же,
как в разделе «АТС»: ключ отдела видит звонки своего отдела, а не всей
организации. Сами карточки при этом общие: у справочника владельца нет.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Body, Depends, File, Query, Request, UploadFile

from .. import employees as справочник
from ..db import Database
from ..errors import ASRHubError, ConfigError
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_write,
    scope_owner,
)

router = APIRouter(prefix="/api/employees", tags=["Сотрудники"])

#: Сколько карточек отдавать за раз. Справочник на тысячу человек — это
#: мегабайт JSON, и рисовать его целиком браузер не будет.
ПРЕДЕЛ = Query(default=100, ge=1, le=1000)


def _db(request: Request) -> Database:
    return get_state(request).db


def _флаг(значение: Any, по_умолчанию: bool) -> bool:
    """Логическое значение из тела запроса или поля формы.

    Поля многочастной формы всегда строки, и `bool("false")` — это `True`.
    Без разбора строкой «deactivate_missing=false», отправленное вместе с
    файлом, означало бы ровно обратное тому, что просили, и увольняло бы
    всех, кого нет в присланном файле.
    """
    if значение is None or значение == "":
        return по_умолчанию
    if isinstance(значение, bool):
        return значение
    текст = str(значение).strip().lower()
    if текст in ("1", "true", "yes", "да", "on", "вкл"):
        return True
    if текст in ("0", "false", "no", "нет", "off", "выкл"):
        return False
    return по_умолчанию


def _карточка_из_тела(тело: dict[str, Any], *, прежняя: dict[str, Any] | None = None
                      ) -> dict[str, Any]:
    """Поля карточки из запроса: только известные, значения — нормализованные.

    Нормализация здесь та же, что и при импорте (`employees.мобильный`,
    `employees.внутренний`), и это принципиально: карточка, заведённая
    руками с номером «8 912 345-67-89», иначе никогда не совпала бы с той
    же карточкой из выгрузки, а поиск по номеру находил бы одну из двух.

    Набор полей закрытый — он же белый список колонок для записи: принимать
    из тела запроса что угодно означало бы дать право дописать в таблицу
    что угодно.
    """
    поля: dict[str, Any] = {}
    for ключ in Database.ПОЛЯ_СОТРУДНИКА:
        if ключ not in тело:
            continue
        значение = тело[ключ]
        if ключ == "active":
            поля[ключ] = 1 if _флаг(значение, True) else 0
        elif ключ == "phone_mobile":
            поля[ключ] = справочник.мобильный(значение)
        elif ключ == "phone_ext":
            поля[ключ] = справочник.внутренний(значение)
        elif ключ == "phone_work":
            поля[ключ] = справочник.рабочий(значение)
        elif ключ == "email":
            поля[ключ] = справочник.почта(значение)
        else:
            поля[ключ] = "" if значение is None else str(значение).strip()

    фамилия = поля.get("last_name", (прежняя or {}).get("last_name") or "")
    имя = поля.get("first_name", (прежняя or {}).get("first_name") or "")
    if not str(фамилия).strip() and not str(имя).strip():
        raise error_response(ConfigError(
            "У карточки должна быть хотя бы фамилия или имя.",
            hint="Передайте поле last_name или first_name."))
    адрес_почты = поля.get("email") or ""
    if адрес_почты and not справочник.похоже_на_адрес(адрес_почты):
        raise error_response(ConfigError(
            f"«{адрес_почты}» не похоже на адрес почты.",
            hint="Ожидается вид имя@домен.зона. Если адреса нет, оставьте поле пустым."))
    return поля


def _имя(карточка: dict[str, Any]) -> str:
    """ФИО одной строкой — для журнала событий и сообщений."""
    части = [str(карточка.get(поле) or "").strip()
             for поле in ("last_name", "first_name", "middle_name")]
    return " ".join(ч for ч in части if ч) or "без имени"


def _найти(db: Database, employee_id: str) -> dict[str, Any]:
    карточка = db.employee_get(employee_id)
    if карточка is None:
        raise error_response(ConfigError(
            f"Сотрудник «{employee_id}» не найден.",
            hint="Список сотрудников: GET /api/employees."))
    return карточка


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------

@router.get("", summary="Справочник сотрудников")
def список(request: Request,
           query: str = Query(default="", max_length=128),
           department: str = Query(default="", max_length=128),
           active: bool | None = Query(default=None),
           source: str = Query(default="", max_length=64),
           limit: int = ПРЕДЕЛ,
           offset: int = Query(default=0, ge=0),
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Карточки с отбором и сводка по отделам одним ответом.

    Сводка по отделам едет вместе со списком намеренно: раздел рисует по
    ней боковое меню и счётчики, и вторым запросом она приезжала бы уже
    после отрисовки — с мигающими цифрами на каждом переключении отбора.
    Считается она по всему справочнику, а не по отобранному куску: иначе
    «Склад — 4» означало бы «четверо из тех, кого сейчас видно», и щелчок
    по отделу показывал бы двенадцать человек.
    """
    db = _db(request)
    итог = db.employee_list(query=query, department=department, active=active,
                            source=source, limit=limit, offset=offset)
    отделы = db.employee_departments()
    return {**итог, "limit": limit, "offset": offset,
            "departments": отделы,
            "query": query, "department": department,
            "active": active, "source": source,
            "shown": len(итог.get("items") or [])}


@router.get("/stats", summary="Сводка по справочнику")
def сводка(request: Request,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Сколько людей, отделов, номеров — и сколько номеров видно в звонках.

    Последнее число — главное. Справочник, где внутренние номера есть у
    всех, но ни один не встречается в журнале звонков, выглядит здоровым и
    при этом не работает: значит, нумерация станции и нумерация кадровой
    системы разъехались (префикс филиала, другое число цифр). Рядом едет
    список номеров из журнала, которых в справочнике нет, — по нему это
    видно сразу, без догадок.
    """
    try:
        return справочник.статистика(_db(request), owner=scope_owner(principal))
    except ASRHubError as exc:
        raise error_response(exc) from exc


@router.get("/{employee_id}", summary="Карточка сотрудника")
def карточка(request: Request, employee_id: str,
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Одна карточка целиком."""
    return _найти(_db(request), employee_id)


# ---------------------------------------------------------------------------
# Правка
# ---------------------------------------------------------------------------

@router.post("", summary="Завести сотрудника")
def завести(request: Request, тело: dict[str, Any] = Body(...),
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Заводит карточку руками — для тех, кого нет в выгрузке.

    Источник по умолчанию «ручной», и это не косметика: импорт справочника
    работает внутри своего источника и помечает уволенными только тех, кого
    он сам когда-то завёл. Карточка, заведённая руками, при первом же
    обновлении справочника иначе исчезла бы из штата.
    """
    require_write(principal)
    db = _db(request)
    поля = _карточка_из_тела(тело or {})
    поля.setdefault("source", "ручной")
    # Внешний ключ нужен, чтобы карточку можно было найти при повторном
    # заведении и чтобы она не дублировалась. Считаем его так же, как
    # импорт: номер, если он есть, иначе ФИО.
    if not поля.get("external_id"):
        номер = str(поля.get("phone_ext") or "")
        поля["external_id"] = номер or "|".join((
            str(поля.get("last_name") or ""), str(поля.get("first_name") or ""),
            str(поля.get("middle_name") or ""))).lower()
    try:
        карточка = db.employee_save(поля)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    db.add_event(None, "employee_added", f"Заведён сотрудник: {_имя(карточка)}")
    return карточка


@router.put("/{employee_id}", summary="Изменить карточку")
def изменить(request: Request, employee_id: str, тело: dict[str, Any] = Body(...),
             principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Правит карточку. Передавать можно только изменившиеся поля.

    Внешний ключ при правке не пересчитывается, даже если поменялся
    внутренний номер или фамилия. Это осознанно: ключ — это то, по чему
    карточку узнаёт следующий импорт, и пересчитать его здесь значит
    развести правку в интерфейсе и выгрузку в две разные карточки одного
    человека.
    """
    require_write(principal)
    db = _db(request)
    прежняя = _найти(db, employee_id)
    поля = _карточка_из_тела(тело or {}, прежняя=прежняя)
    if not поля:
        return прежняя
    # Источник передаём явно, даже когда его не просили менять. При
    # сохранении он подставляется значением «ручной», если его нет в
    # полях, и карточка из выгрузки после любой правки оказывалась в чужом
    # источнике: следующее обновление справочника не находило её по ключу,
    # заводило человека второй раз, а правка вместе с примечанием
    # оставалась на осиротевшей карточке.
    поля.setdefault("source", str(прежняя.get("source") or "ручной"))
    try:
        карточка = db.employee_save(поля, id=employee_id)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    db.add_event(None, "employee_saved",
                 f"Изменена карточка: {_имя(карточка)} ({', '.join(sorted(поля))})")
    return карточка


@router.delete("/{employee_id}", summary="Удалить карточку")
def удалить(request: Request, employee_id: str,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Убирает карточку насовсем. Звонки остаются.

    Именно поэтому удаление — право администратора, а обычный способ
    убрать человека из штата другой: пометить «не работает». Удалённая
    карточка не возвращает номеру имя, и все прошлые разговоры этого
    человека снова становятся строкой «1043» — в отчётах, которые уже
    кто-то читал и обсуждал.
    """
    require_admin(require_write(principal))
    db = _db(request)
    карточка = _найти(db, employee_id)
    if not db.employee_delete(employee_id):
        raise error_response(ConfigError(
            f"Сотрудника «{employee_id}» не удалось удалить.",
            hint="Возможно, карточку убрали параллельно. Обновите список."))
    db.add_event(None, "employee_removed", f"Удалён сотрудник: {_имя(карточка)}")
    номер = str(карточка.get("phone_ext") or "")
    осталось = db.call_counts(owner=scope_owner(principal)) if номер else {}
    return {"removed": employee_id, "employee": карточка,
            "phone_ext": номер, "calls_total": int(осталось.get("total") or 0)}


# ---------------------------------------------------------------------------
# Импорт
# ---------------------------------------------------------------------------

@router.post("/import", summary="Импорт выгрузки справочника")
async def импорт(request: Request,
                 file: UploadFile | None = File(default=None),
                 principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Забирает выгрузку по адресу или принимает присланный файл.

    Двумя способами, и оба нужны. По адресу — то, что делает расписание, и
    то, чем пользуются, когда справочник выложен на портале. Файлом — когда
    выгрузку принесли почтой, когда портал закрыт паролем и когда нужно
    проверить, как разберётся конкретный файл, ничего не настраивая.

    Тело разбирается вручную, а не описанием параметров: FastAPI не умеет
    принимать на одном адресе и объект JSON, и многочастную форму — объявив
    и то и другое, получаешь маршрут, который перестаёт принимать JSON.
    Здесь же оба способа равноправны, и выбор делается по типу содержимого.
    """
    require_admin(require_write(principal))
    state = get_state(request)
    тип_тела = (request.headers.get("content-type") or "").split(";")[0].strip().lower()

    данные: bytes | None = None
    тело: dict[str, Any] = {}
    if тип_тела == "multipart/form-data":
        форма = await request.form()
        тело = {ключ: значение for ключ, значение in форма.items()
                if isinstance(значение, str)}
        if file is not None:
            данные = await file.read()
            if not данные:
                raise error_response(ConfigError(
                    "Присланный файл пуст.",
                    hint="Проверьте, что выбран файл выгрузки: "
                         "curl -F file=@employees.xml …"))
    elif тип_тела in ("application/json", ""):
        try:
            разобрано = await request.json()
        except Exception:                                    # noqa: BLE001
            разобрано = None
        тело = разобрано if isinstance(разобрано, dict) else {}
    else:
        raise error_response(ConfigError(
            f"Тело запроса вида «{тип_тела}» не принимается.",
            hint="Пришлите объект JSON {url: …} или форму с полем file."))

    источник = str(тело.get("source") or "").strip() or "справочник"
    убирать = _флаг(тело.get("deactivate_missing"),
                    bool(state.settings.get("employees_deactivate_missing", True)))
    адрес = str(тело.get("url") or "").strip()
    if данные is None and not адрес:
        # Адрес из настройки — это «нажать кнопку и получить то же, что по
        # расписанию». Без этого человек каждый раз копировал бы адрес из
        # соседнего раздела в тело запроса.
        адрес = str(state.settings.get("employees_url") or "").strip()

    try:
        итог = справочник.импорт(state.db, url=адрес, данные=данные,
                                 source=источник, deactivate_missing=убирать,
                                 тип=str(тело.get("format") or ""))
    except ASRHubError as exc:
        raise error_response(exc) from exc
    state.db.add_event(
        None, "employees_imported",
        f"Справочник «{источник}»: +{итог.get('added')} новых, "
        f"{итог.get('updated')} изменено, {итог.get('deactivated')} уволено",
        {"source": источник, "from": итог.get("from"),
         "rows": итог.get("rows"), "skipped": итог.get("skipped")})
    return итог


@router.post("/link", summary="Сверить справочник с журналом звонков")
def связать(request: Request,
            period: str = Query(default="all",
                                pattern="^(day|week|month|quarter|year|all)$"),
            limit: int = ПРЕДЕЛ,
            principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Считает, сколько звонков приходится на каждого сотрудника.

    Архив при этом не меняется — и это не упрощение, а единственно верное
    поведение. Звонок принадлежит номеру, а не человеку: за год за одним
    столом сменится двое, и переписать старые разговоры на нового
    сотрудника значило бы подделать его отчёт. Поэтому связь считается на
    лету по совпадению внутреннего номера, и пересчёт — это проверка
    («сошлось или нет»), а не правка данных.

    Отдельно едут номера из журнала, которых в справочнике нет: это либо
    уволенные, либо очереди и служебные линии, либо разъехавшаяся нумерация.
    """
    сколько = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400,
               "quarter": 92 * 86400, "year": 365 * 86400}.get(period)
    начало = None if сколько is None else time.time() - сколько
    if not bool(get_state(request).settings.get("employees_match_ext", True)):
        # Сверку не запрещаем: она как раз и отвечает на вопрос «а стоило
        # ли выключать». Но говорим прямо, что в отчётах связи сейчас нет.
        пометка = ("Связь звонков с сотрудниками выключена настройкой "
                   "employees_match_ext: в отчётах имена не подставляются.")
    else:
        пометка = ""
    try:
        итог = справочник.связать(_db(request), owner=scope_owner(principal),
                                  since=начало, limit=limit)
    except ASRHubError as exc:
        raise error_response(exc) from exc
    return {**итог, "period": period, "note": пометка}


__all__ = ["router"]
