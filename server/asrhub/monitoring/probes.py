"""Пробы состояния: живость, готовность, завершение запуска.

Три пробы отвечают на три разных вопроса, и путать их дорого:

* **liveness** — «процесс жив?». Если она провалилась, оркестратор перезапускает
  контейнер. Поэтому она не должна зависеть ни от чего внешнего: недоступная
  база или занятая очередь не повод убивать процесс.
* **readiness** — «можно ли слать сюда запросы?». Провалилась — балансировщик
  снимает нагрузку, но контейнер живёт. Здесь и проверяются база, движки и место
  на диске.
* **startup** — «запуск закончился?». Пока она не прошла, две первые не
  учитываются: загрузка весов модели занимает десятки секунд, и убивать
  контейнер за это нельзя.
"""
from __future__ import annotations

import shutil
import time
from typing import Any

CHECK_OK = "ok"
CHECK_WARN = "warn"
CHECK_FAIL = "fail"


def _check(name: str, status: str, detail: str = "", hint: str = "") -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, "hint": hint}


def _очередь_здесь(state: Any) -> bool:
    """Должна ли очередь работать в этом процессе (не запущен ли он с --no-queue)."""
    return bool(getattr(state, "queue_enabled", True))


#: Что пишет проба об очереди у сервера «только интерфейс».
БЕЗ_ОЧЕРЕДИ = ("очередь здесь не обрабатывается (запуск с --no-queue): "
               "задания считает соседний сервер")


def liveness(state: Any) -> dict[str, Any]:
    """Максимально дешёвая проверка: процесс отвечает и цикл очереди жив.

    У сервера, запущенного с `--no-queue` («только интерфейс»), рабочих
    потоков нет по замыслу. Проба называла их отсутствие провалом, и
    Kubernetes по рекомендованным пробам перезапускал такой под по кругу.
    """
    checks = [_check("process", CHECK_OK, f"работает {round(time.time() - state.started_at)} с")]

    queue = getattr(state, "queue", None)
    if not _очередь_здесь(state):
        checks.append(_check("queue_thread", CHECK_OK, БЕЗ_ОЧЕРЕДИ))
    else:
        alive = bool(getattr(queue, "_started", False)) if queue is not None else False
        # Флаг «запущена» не говорит, живы ли потоки: упавший поток флаг не
        # снимает. Смотрим сами потоки — служебные и хотя бы один рабочий.
        потоки = [п for п in (getattr(queue, "_workers", None) or [])
                  if hasattr(п, "is_alive")]
        мёртвые = [п.name for п in потоки
                   if п.name in ("asrhub-janitor", "asrhub-heartbeat") and not п.is_alive()]
        рабочие = [п for п in потоки if п.name.startswith("asrhub-worker")]
        if alive and рабочие and not any(п.is_alive() for п in рабочие):
            мёртвые.append("asrhub-worker")
        if alive and мёртвые:
            checks.append(_check(
                "queue_thread", CHECK_FAIL, "остановились потоки очереди: " + ", ".join(мёртвые),
                "Перезапустите службу: scripts/service.sh restart"))
        else:
            checks.append(_check(
                "queue_thread", CHECK_OK if alive else CHECK_FAIL,
                "рабочие потоки запущены" if alive else "рабочие потоки не запущены",
                "" if alive else "Перезапустите службу: scripts/service.sh restart"))

    # Поток смыслового разбора: умерший поток никто не поднимает, и очередь
    # разбора стоит до перезапуска. Раз начатый и не остановленный поток
    # обязан быть жив — иначе перезапуск и есть лечение.
    поток = getattr(getattr(state, "llm_worker", None), "_thread", None)
    стоп = getattr(getattr(state, "llm_worker", None), "_stop", None)
    if поток is not None and hasattr(поток, "is_alive") \
            and not (стоп is not None and стоп.is_set()):
        жив = поток.is_alive()
        checks.append(_check(
            "llm_thread", CHECK_OK if жив else CHECK_FAIL,
            "поток разбора работает" if жив else "поток смыслового разбора остановился",
            "" if жив else "Перезапустите службу: scripts/service.sh restart"))

    failed = any(c["status"] == CHECK_FAIL for c in checks)
    return {"status": "fail" if failed else "ok", "checks": checks}


def readiness(state: Any) -> dict[str, Any]:
    """Готовность принимать работу: база, движки, место, не на паузе ли очередь."""
    checks: list[dict[str, Any]] = []

    try:
        state.db.query_one("SELECT 1 AS ok")
        checks.append(_check("database", CHECK_OK, "отвечает"))
    except Exception as exc:                                # noqa: BLE001
        checks.append(_check("database", CHECK_FAIL, f"{type(exc).__name__}: {exc}",
                             "Проверьте права на каталог данных и целостность файла базы"))

    try:
        from ..engines import engine_status

        available = [e for e in engine_status() if e.get("available")]
        if available:
            checks.append(_check("engines", CHECK_OK, f"доступно {len(available)}"))
        else:
            checks.append(_check("engines", CHECK_FAIL, "нет ни одного движка",
                                 "bash scripts/models.sh engines"))
    except Exception as exc:                                # noqa: BLE001
        checks.append(_check("engines", CHECK_FAIL, str(exc)))

    try:
        usage = shutil.disk_usage(str(state.settings.paths.data))
        free_gb = usage.free / 1024 ** 3
        limit = float(state.settings.get("disk_min_free_gb") or 0)
        if limit and free_gb < limit:
            checks.append(_check("disk", CHECK_FAIL, f"свободно {free_gb:.1f} ГБ при пороге {limit}",
                                 "POST /api/maintenance/cleanup или освободите место"))
        elif free_gb < max(limit * 2, 10):
            checks.append(_check("disk", CHECK_WARN, f"свободно {free_gb:.1f} ГБ"))
        else:
            checks.append(_check("disk", CHECK_OK, f"свободно {free_gb:.1f} ГБ"))
    except OSError as exc:
        checks.append(_check("disk", CHECK_WARN, str(exc)))

    try:
        status = state.queue.status()
        if status.get("paused"):
            # Пауза — намеренное действие, а не поломка: предупреждаем, но
            # из ротации не выводим, иначе обслуживание уронит балансировщик.
            checks.append(_check("queue", CHECK_WARN, "очередь на паузе",
                                 "POST /api/queue/resume"))
        else:
            checks.append(_check("queue", CHECK_OK,
                                 f"ждёт {status.get('queue_depth', 0)}, "
                                 f"выполняется {status.get('active', 0)}"))
    except Exception as exc:                                # noqa: BLE001
        checks.append(_check("queue", CHECK_FAIL, str(exc)))

    failed = any(c["status"] == CHECK_FAIL for c in checks)
    warned = any(c["status"] == CHECK_WARN for c in checks)
    return {"status": "fail" if failed else ("warn" if warned else "ok"), "checks": checks}


def startup(state: Any) -> dict[str, Any]:
    """Завершился ли запуск: каталог прочитан, база открыта, очередь поднята."""
    очередь_здесь = _очередь_здесь(state)
    checks = [
        _check("catalog", CHECK_OK if getattr(state, "settings", None) else CHECK_FAIL,
               "настройки загружены"),
        _check("database", CHECK_OK if getattr(state, "db", None) else CHECK_FAIL,
               "база открыта"),
        _check("queue",
               CHECK_OK if (not очередь_здесь or getattr(state.queue, "_started", False))
               else CHECK_FAIL,
               "очередь запущена" if очередь_здесь else БЕЗ_ОЧЕРЕДИ),
    ]
    failed = any(c["status"] == CHECK_FAIL for c in checks)
    return {"status": "fail" if failed else "ok", "checks": checks}


def overall(state: Any, alerts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сводное состояние для панели и для внешнего опроса одним запросом."""
    live = liveness(state)
    ready = readiness(state)
    started = startup(state)

    if live["status"] == "fail" or started["status"] == "fail":
        status = "critical"
    elif ready["status"] == "fail":
        status = "degraded"
    elif ready["status"] == "warn" or (alerts or {}).get("firing"):
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "uptime_s": round(time.time() - state.started_at, 1),
        "liveness": live,
        "readiness": ready,
        "startup": started,
        "alerts": alerts or {},
    }
