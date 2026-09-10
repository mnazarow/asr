"""Маршруты раздела «Телефония»: состояние импорта, проверка связи, журнал звонков.

Разрез по владельцу тот же, что у заданий: обычный ключ видит свои звонки,
ключ в группе — звонки группы, администратор — всё. Настройки станции
(адрес, учётная запись, пути) и ручной запуск захода — только
администратору: адрес АТС и имя учётной записи в manager.conf это
разведка перед атакой на телефонию, а не справочная информация.
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from ..errors import ASRHubError, ConfigError
from ..telephony.asterisk import проверить, разобрать_cdr_строку
from .deps import (
    Principal,
    authenticate,
    error_response,
    get_state,
    require_admin,
    require_write,
    scope_owner,
)

router = APIRouter(prefix="/api/telephony", tags=["Телефония"])

НАПРАВЛЕНИЕ = Query(default="", pattern="^(|входящий|исходящий|внутренний)$")


def _импортёр(request: Request) -> Any:
    """Импортёр звонков из состояния приложения."""
    state = get_state(request)
    ввозчик = getattr(state, "telephony", None)
    if ввозчик is None:
        raise error_response(ASRHubError(
            "Телефония не инициализирована.",
            hint="Сервер запущен в урезанном режиме; перезапустите его обычным способом."))
    return ввозчик


def _период(period: str) -> float | None:
    """Начало периода в секундах эпохи; «all» — без ограничения."""
    сколько = {"day": 86400, "week": 7 * 86400, "month": 30 * 86400,
               "quarter": 92 * 86400, "year": 365 * 86400}.get(period)
    return None if сколько is None else time.time() - сколько


@router.get("/status", summary="Состояние забора записей с АТС")
def status(request: Request,
           principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Включено ли, что за источник, сколько импортировано и что мешает.

    Обычному ключу отдаются счётчики его звонков и признак «работает»;
    адреса, пути и имя учётной записи — только администратору: по ним
    строится вход в телефонию организации, а не понимание своей работы.
    """
    ввозчик = _импортёр(request)
    свод = dict(ввозчик.status())
    свод["calls"] = get_state(request).db.call_counts(owner=scope_owner(principal))
    if not principal.is_admin:
        for ключ in ("host", "recordings_dir", "cdr_file", "last_error"):
            свод.pop(ключ, None)
    return свод


@router.post("/test", summary="Проверка связи с АТС")
def test(request: Request,
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Достучаться до источника и сказать, что именно не так.

    «Не работает» — не диагноз. Проверка отвечает по-разному на «порт
    закрыт», «пароль не тот», «файла нет» и «файл есть, но в нём ноль
    строк»: каждое из этих состояний чинится по-своему.
    """
    require_admin(principal)
    ввозчик = _импортёр(request)
    настройки = get_state(request).settings
    источник = ввозчик.source
    начало = time.perf_counter()
    try:
        if источник == "ami":
            итог = проверить(str(настройки.get("telephony_host") or "127.0.0.1"),
                             int(настройки.get("telephony_port") or 5038),
                             str(настройки.get("telephony_username") or ""),
                             str(настройки.get("telephony_secret") or ""))
            итог["source"] = "ami"
            return итог
        if источник == "folder":
            каталог = ввозчик.путь("telephony_recordings_dir")
            if каталог is None or not каталог.is_dir():
                raise ConfigError(
                    "Каталог записей не найден.",
                    hint="Проверьте «Каталог записей» и права на чтение.")
            файлов = sum(1 for п in каталог.rglob("*")
                         if п.suffix.lower() in (".wav", ".mp3", ".ogg"))
            return {"ok": True, "source": "folder", "dir": str(каталог),
                    "files": файлов,
                    "ms": round((time.perf_counter() - начало) * 1000, 1)}
        журнал = ввозчик.путь("telephony_cdr_file")
        if журнал is None or not журнал.is_file():
            raise ConfigError(
                "Журнал звонков не найден.",
                hint="Обычно это /var/log/asterisk/cdr-csv/Master.csv; "
                     "нужен доступ на чтение.")
        размер = журнал.stat().st_size
        # Читаем хвост, а не начало: журнал бывает в гигабайт, а вопрос
        # проверки — «разбирается ли то, что станция пишет сейчас».
        с_чем = max(0, размер - 65536)
        with журнал.open("r", encoding="utf-8", errors="replace", newline="") as файл:
            файл.seek(с_чем)
            строки = [с for с in файл.read().splitlines() if с.strip()]
        образцы = [z for z in (разобрать_cdr_строку(с) for с in строки[-5:]) if z]
        return {
            "ok": True, "source": "cdr_csv", "file": str(журнал), "bytes": размер,
            "offset": int(get_state(request).db.get_kv("telephony_cdr_offset", 0) or 0),
            "sample": [{"uniqueid": z.uniqueid, "src": z.src, "dst": z.dst,
                        "disposition": z.disposition, "billsec": z.billsec,
                        "started_at": z.started_at} for z in образцы],
            "ms": round((time.perf_counter() - начало) * 1000, 1),
        }
    except ASRHubError as exc:
        # AMIError — тоже ASRHubError, отдельной ветки ей не нужно: у обеих
        # есть код, текст и подсказка, и наружу они уходят одинаково.
        raise error_response(exc) from exc
    except OSError as exc:
        raise error_response(ConfigError(
            f"Источник недоступен: {exc}",
            hint="Проверьте путь и права пользователя, от которого работает сервер.")) from exc


@router.post("/scan", summary="Заход за новыми звонками прямо сейчас")
def scan(request: Request, limit: int = Query(default=50, ge=1, le=500),
         principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Один заход по требованию — чтобы не ждать интервала опроса.

    Отвечает тем же, чем отчитывается фоновый поток: сколько звонков
    увидели, сколько поставили в очередь и по каким причинам пропустили
    остальные. Причины важнее счётчика: «нет записи ×48» и «короткий ×48» —
    это две разные поломки.
    """
    require_admin(require_write(principal))
    try:
        return _импортёр(request).scan(limit=limit)
    except ASRHubError as exc:
        # Сюда приходят и сбой связи со станцией (AMIError, 502), и
        # ненайденный журнал (ConfigError, 400) — у каждой свой код и своя
        # подсказка, и подменять их одной общей нельзя: «проверьте
        # manager.conf» на отсутствующий файл чинить нечего.
        raise error_response(exc) from exc


@router.get("/calls", summary="Журнал импортированных звонков")
def calls(request: Request, period: str = Query(default="week",
                                                pattern="^(day|week|month|quarter|year|all)$"),
          direction: str = НАПРАВЛЕНИЕ, queue: str = Query(default="", max_length=64),
          agent: str = Query(default="", max_length=64),
          search: str = Query(default="", max_length=64),
          only_queued: bool = Query(default=False),
          limit: int = Query(default=50, ge=1, le=500),
          offset: int = Query(default=0, ge=0),
          principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Список звонков с отбором: направление, очередь, оператор, номер."""
    return get_state(request).db.list_calls(
        owner=scope_owner(principal), direction=direction, queue=queue,
        agent=agent, search=search, only_queued=only_queued,
        since=_период(period), limit=limit, offset=offset)


@router.get("/dimensions", summary="Очереди и операторы для отбора")
def dimensions(request: Request,
               principal: Principal = Depends(authenticate)) -> dict[str, Any]:
    """Что вообще встречалось в звонках — чтобы отбор был выбором, а не набором."""
    return get_state(request).db.call_dimensions(owner=scope_owner(principal))


__all__ = ["router"]
