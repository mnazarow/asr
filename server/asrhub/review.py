"""Очередь ручной проверки и контрольный прогон второй моделью.

Обе вещи — про то, чтобы у сервера появились числа, которые что-то
доказывают. Уверенность модели и признаки подозрительности — косвенные;
доказывает только сравнение с расшифровкой, сделанной человеком, а на неё
нужны сами люди и порядок, по которому им дают записи.

**Очередь проверки** (по Deepgram): раз в сутки — 0,5–1 % случайных записей
плюс нижняя четверть по уверенности. Случайные — потому что только по
случайной выборке WER честный; неуверенные — потому что там ошибки чаще, и
исправлений на час работы выходит больше. Человек слушает во вкладке
«Эталон» карточки записи, правит текст, и правка становится эталоном; строка
очереди закрывается сама.

**Контрольный прогон**: несколько случайных записей в сутки распознаются
второй моделью, и расхождение двух расшифровок (WER одной относительно
другой) записывается как «согласие моделей». Точность без эталона оно не
меряет; меряет ход: два независимых распознавателя, которые вдруг стали
расходиться сильнее, — признак, что изменился вход или сломался один из
них. Контрольные задания — обычные задания с низким приоритетом, источником
`control` и меткой «контроль»; в разбор содержания они не попадают, иначе
каждый проверенный разговор считался бы дважды.
"""
from __future__ import annotations

import math
import random
import time
from pathlib import Path
from typing import Any

from .catalog import get_model
from .logging_setup import get_logger

log = get_logger("review")

#: Окно, из которого берутся кандидаты: последние сутки.
ОКНО_С = 86400.0

#: Приоритет контрольных заданий: считаются, когда очередь свободна.
ПРИОРИТЕТ_КОНТРОЛЯ = 10

#: Метка контрольных заданий и их источник.
МЕТКА_КОНТРОЛЯ = "контроль"
ИСТОЧНИК_КОНТРОЛЯ = "control"


def _кандидаты(db: Any, *, since: float) -> list[dict[str, Any]]:
    """Завершённые за окно записи, посчитанные сервером самим и не контрольные."""
    return [j for j in db.list_jobs(status="completed", since=since, limit=100000, light=True)
            if not j.get("cached_from") and str(j.get("source") or "") != ИСТОЧНИК_КОНТРОЛЯ]


def sample_review(db: Any, settings: Any, *, now: float | None = None,
                  rng: random.Random | None = None) -> dict[str, Any]:
    """Пополняет очередь проверки записями за последние сутки."""
    сейчас = now or time.time()
    rng = rng or random.Random()
    доля = float(settings.get("review_daily_share") or 0.0)
    неуверенных = int(settings.get("review_daily_low") or 0)
    предел = max(1, int(settings.get("review_daily_max") or 20))
    в_очереди = db.review_queued_ids()
    # Записи с эталоном проверять незачем — эталон уже есть.
    кандидаты = [j for j in _кандидаты(db, since=сейчас - ОКНО_С)
                 if j["id"] not in в_очереди and not j.get("ref_words")]
    выбрано: dict[str, str] = {}
    if кандидаты and доля > 0:
        сколько = min(len(кандидаты), max(1, math.ceil(len(кандидаты) * доля / 100.0)))
        for j in rng.sample(кандидаты, сколько):
            выбрано[j["id"]] = "random"
    if неуверенных > 0:
        с_уверенностью = sorted((j for j in кандидаты if j.get("avg_confidence") is not None
                                 and j["id"] not in выбрано),
                                key=lambda j: float(j["avg_confidence"]))
        # Нижняя четверть: порог — квартиль по всем кандидатам с уверенностью.
        всего = [float(j["avg_confidence"]) for j in кандидаты
                 if j.get("avg_confidence") is not None]
        if всего:
            всего.sort()
            квартиль = всего[max(0, int(len(всего) * 0.25) - 1)] if len(всего) >= 4 else всего[-1]
            for j in с_уверенностью:
                if len([r for r in выбрано.values() if r == "low_confidence"]) >= неуверенных:
                    break
                if float(j["avg_confidence"]) <= квартиль:
                    выбрано[j["id"]] = "low_confidence"
    добавлено = 0
    for job_id, причина in list(выбрано.items())[:предел]:
        if db.review_add(job_id, причина, picked_at=сейчас):
            добавлено += 1
    итог = {"candidates": len(кандидаты), "added": добавлено,
            "random": sum(1 for r in list(выбрано.values())[:предел] if r == "random"),
            "low_confidence": sum(1 for r in list(выбрано.values())[:предел]
                                  if r == "low_confidence")}
    if добавлено:
        log.info("Очередь проверки пополнена: %s", итог)
    return итог


def sample_control(db: Any, settings: Any, queue: Any, *, now: float | None = None,
                   rng: random.Random | None = None) -> dict[str, Any]:
    """Ставит контрольные задания второй моделью на случайные записи за сутки."""
    модель = str(settings.get("control_model") or "").strip()
    if not модель:
        return {"submitted": 0, "reason": "выключено"}
    spec = get_model(модель)
    if spec is None:
        log.warning("Контрольная модель «%s» неизвестна каталогу — прогон пропущен", модель)
        return {"submitted": 0, "reason": f"модель «{модель}» неизвестна"}
    сейчас = now or time.time()
    rng = rng or random.Random()
    сколько = max(1, int(settings.get("control_daily_jobs") or 5))
    уже = db.checked_job_ids(сейчас - ОКНО_С)
    в_работе = {str((j.get("params") or {}).get("control_of") or "")
                for j in db.list_jobs(status=["queued", "running", "retry", "paused"],
                                      limit=10000)}
    подходящие = [j for j in _кандидаты(db, since=сейчас - ОКНО_С)
                  if str(j.get("model") or "") != модель and j["id"] not in уже
                  and j["id"] not in в_работе]
    пути = db.file_paths([j["id"] for j in подходящие])
    # Исходный файл должен ещё лежать в uploads: при коротком сроке
    # хранения загрузок часть записей уже не повторить.
    кандидаты = [j for j in подходящие
                 if j["id"] in пути and Path(пути[j["id"]]).is_file()]
    поставлено = []
    for j in rng.sample(кандидаты, min(сколько, len(кандидаты))):
        настройки = settings.merged({"model": модель, "engine": spec.engine,
                                     "deduplicate_jobs": False})
        # Сведения о происхождении — в параметрах задания: по ним при
        # завершении считается расхождение, а в карточке видно, что это.
        настройки["control_of"] = j["id"]
        try:
            задание = queue.submit(
                file_path=Path(пути[j["id"]]), filename=str(j.get("filename") or ""),
                settings=настройки, owner=str(j.get("owner") or "anonymous"),
                api_key_name="контроль", priority=ПРИОРИТЕТ_КОНТРОЛЯ,
                source=ИСТОЧНИК_КОНТРОЛЯ, tags=МЕТКА_КОНТРОЛЯ)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Контрольный прогон для %s не поставлен: %s", j["id"], exc)
            continue
        поставлено.append(задание["id"])
        db.add_event(j["id"], "control", f"Контрольный прогон моделью {модель}: "
                                          f"задание {задание['id']}")
    if поставлено:
        log.info("Контрольные прогоны моделью %s: %d заданий", модель, len(поставлено))
    return {"submitted": len(поставлено), "candidates": len(кандидаты), "model": модель,
            "jobs": поставлено}


def record_check(db: Any, job: dict[str, Any], segments: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Расхождение контрольного задания с исходным — при завершении.

    Сравнение по сегментам, без меток говорящих; «эталоном» выступает
    исходная расшифровка — не потому, что она верна, а потому, что
    расхождение симметрично по смыслу и для ряда важен только его ход.
    """
    from .pipeline import metrics as M  # noqa: PLC0415

    исходное = str((job.get("params") or {}).get("control_of") or "")
    if not исходное:
        return None
    оригинал = db.get_job(исходное)
    if оригинал is None:
        log.info("Контрольный прогон %s: исходное задание %s уже удалено",
                 job.get("id"), исходное)
        return None
    свои = db.get_segments(исходное)
    первая = (" ".join(str(с.get("text") or "") for с in свои) if свои
              else str(оригинал.get("text") or ""))
    вторая = " ".join(str(с.get("text") or "") for с in segments)
    разбор = M.detailed(первая, вторая)
    db.add_model_check(исходное, str(job["id"]), model=str(оригинал.get("model") or ""),
                       control_model=str(job.get("model") or ""),
                       wer=разбор["wer"], mer=разбор["mer"],
                       words=int(разбор.get("reference_words") or 0))
    db.add_event(исходное, "control_done",
                 f"Контрольная модель {job.get('model')}: расхождение "
                 f"{100 * разбор['wer']:.1f} % (MER {100 * разбор['mer']:.1f} %)")
    return {"job_id": исходное, "check_job_id": job["id"], "wer": разбор["wer"],
            "mer": разбор["mer"], "words": int(разбор.get("reference_words") or 0)}
