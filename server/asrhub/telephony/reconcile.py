"""Суточная сверка «станция ↔ архив».

Молчаливые потери — самое неприятное, что бывает с забором звонков:
журнал станции повернули, пока сервер лежал; сценарий диалплана не смог
отправить и не дослал; каталог записей переехал. Звонки на станции есть, а
в архиве их нет, и узнают об этом через месяц, когда руководитель
спрашивает, куда делся разговор.

Раз в сутки за прошедший день сравнивается то, что видно на станции, с
тем, что лежит в архиве:

* журнал CDR (`cdr_csv`) — число звонков и отвеченных (ноги одного
  разговора сводятся так же, как при заборе);
* диалплан (`dialplan`) и каталог записей (`folder`) — число файлов
  записей за день против строк архива с записью;
* AMI — независимого источника нет (события приходят один раз), сверка не
  делается.

Расхождение больше порога — событие в ленту, тревога на `alert_url` и
строка в разделе «АТС».
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from . import asterisk as A

log = get_logger("telephony")

#: Где лежит итог последней сверки (список по станциям).
KV_СВЕРКА = "telephony:reconcile"
#: Сколько звонков можно не досчитаться, прежде чем поднимать тревогу:
#: звонок, начатый вчера в 23:59, мог лечь в архив сегодняшним.
ДОПУСК_ЗВОНКОВ = 2
ДОПУСК_ДОЛЯ = 0.02


def окно_вчера(сейчас: float | None = None) -> tuple[float, float, str]:
    """Границы вчерашнего дня по часам сервера и его подпись."""
    момент = datetime.fromtimestamp(сейчас if сейчас is not None else time.time())
    начало = (момент - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    конец = начало + timedelta(days=1)
    return начало.timestamp(), конец.timestamp(), начало.strftime("%Y-%m-%d")


def _архив(db: Any, станция: str, начало: float, конец: float) -> dict[str, int]:
    строка = db.query_one(
        "SELECT COUNT(*) AS n, SUM(CASE WHEN answered THEN 1 ELSE 0 END) AS a, "
        "SUM(CASE WHEN COALESCE(recording,'')<>'' OR job_id IS NOT NULL THEN 1 ELSE 0 END) "
        "AS r FROM calls WHERE station=? AND started_at>=? AND started_at<?",
        (станция, начало, конец))
    return {"total": int((строка or {})["n"] or 0) if строка else 0,
            "answered": int((строка or {})["a"] or 0) if строка else 0,
            "recorded": int((строка or {})["r"] or 0) if строка else 0}


def _из_журнала(путь: Path, начало: float, конец: float,
                пояс: Any = None) -> dict[str, int]:
    звонки: list[A.Звонок] = []
    with open(путь, "rb") as файл:
        for сырая in файл:
            звонок = A.разобрать_cdr_строку(A.декодировать(сырая).rstrip("\r\n"), пояс=пояс)
            if звонок is not None and начало <= float(звонок.started_at or 0) < конец:
                звонки.append(звонок)
    звонки = A.свести_ноги(звонки)
    return {"total": len(звонки), "answered": sum(1 for з in звонки if з.answered)}


def _файлы_записей(каталог: Path, начало: float, конец: float) -> int:
    """Записи, законченные за день (по времени изменения файла).

    Вторая дорожка пары Monitor() (`-out`) не считается: она едет вместе с
    первой одной записью.
    """
    всего = 0
    for корень, _, файлы in os.walk(каталог):
        for имя in файлы:
            путь = Path(корень) / имя
            if not A._запись_ли(путь):
                continue
            try:
                изменён = путь.stat().st_mtime
            except OSError:
                continue
            if not начало <= изменён < конец:
                continue
            пара = A.пара_дорожек(путь)
            if пара is not None and путь == пара[1]:
                continue
            всего += 1
    return всего


def сверить_станцию(db: Any, станция: Any, начало: float, конец: float,
                    день: str) -> dict[str, Any] | None:
    """Итог сверки одной станции; None — сверять нечем (AMI, нет пути)."""
    источник = str(getattr(станция, "source", "") or "")
    итог: dict[str, Any] = {"station": станция.id, "name": станция.name, "source": источник,
                            "day": день, "checked_at": time.time()}
    архив = _архив(db, станция.id, начало, конец)
    try:
        if источник == "cdr_csv":
            путь = станция.путь("cdr_file")
            if путь is None or not путь.is_file():
                return None
            на_станции = _из_журнала(путь, начало, конец, getattr(станция, "пояс", None))
            итог.update({"station_total": на_станции["total"],
                         "station_answered": на_станции["answered"],
                         "archive_total": архив["total"],
                         "archive_answered": архив["answered"]})
            недостаёт = на_станции["total"] - архив["total"]
            что = "звонков в журнале станции"
        elif источник in ("dialplan", "folder"):
            каталог = станция.путь("recordings_dir")
            if каталог is None or not каталог.is_dir():
                return None
            файлов = _файлы_записей(каталог, начало, конец)
            итог.update({"station_files": файлов, "archive_recorded": архив["recorded"],
                         "archive_total": архив["total"]})
            недостаёт = файлов - архив["recorded"]
            что = "файлов записей"
        else:
            return None
    except OSError as exc:
        итог.update({"ok": False, "error": str(exc)})
        return итог
    итог["missing"] = max(0, int(недостаёт))
    порог = max(ДОПУСК_ЗВОНКОВ, int(round(
        (итог.get("station_total") or итог.get("station_files") or 0) * ДОПУСК_ДОЛЯ)))
    итог["ok"] = итог["missing"] <= порог
    if not итог["ok"]:
        итог["summary"] = (f"Станция «{станция.name}» за {день}: {что} больше, чем в "
                           f"архиве, на {итог['missing']} — звонки потеряны по дороге.")
    return итог


def сверить_все(db: Any, settings: Any, *, сейчас: float | None = None,
                тревога: Any = None) -> list[dict[str, Any]]:
    """Сверка всех включённых станций за вчера; итог — в kv и в ленту событий."""
    from .stations import список  # noqa: PLC0415

    начало, конец, день = окно_вчера(сейчас)
    итоги: list[dict[str, Any]] = []
    for станция in список(settings):
        if not getattr(станция, "enabled", True):
            continue
        try:
            итог = сверить_станцию(db, станция, начало, конец, день)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Сверка станции «%s» не удалась: %s", станция.name, exc)
            continue
        if итог is None:
            continue
        итоги.append(итог)
        if not итог.get("ok", True) and итог.get("summary"):
            log.warning("%s", итог["summary"])
            try:
                db.add_event(None, "telephony_reconcile", итог["summary"], итог)
            except Exception:                                # noqa: BLE001
                pass
            if callable(тревога):
                try:
                    тревога("alert_firing", {"summary": итог["summary"], "rule":
                                             "telephony_reconcile", **итог})
                except Exception as exc:                     # noqa: BLE001
                    log.debug("Тревога сверки не отправлена: %s", exc)
    db.set_kv(KV_СВЕРКА, {"day": день, "items": итоги, "at": time.time()})
    return итоги
