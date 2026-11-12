"""Что, когда и с какими списками отдавать модели.

Правила отбора записей для смыслового разбора — одним местом, чтобы
фоновый поток, разбор архива и счёт ожидающих не разошлись:

* **порог длительности** (`llm_min_duration_s`): короткий звонок («алло,
  не туда») модель не разбирает — видеокарта занята делом;
* **сомнительная расшифровка** (`llm_suspect_mode`, `llm_suspect_share`):
  распознавание ошиблось — модель уверенно перескажет то, чего не
  говорили. Такую запись можно не разбирать или разбирать с пометкой;
* **окно архива** (`llm_archive_hours`, `llm_archive_daily`): архив
  разбирается в заданные часы и не больше заданного числа в сутки, свежие
  записи — всегда;
* **списки отдела** (`llm_lists_by_department`): у продаж и поддержки
  разные причины обращений;
* **отпечаток настройки**: что из настроек модели меняет её ответы — по
  нему ведётся журнал смен (`llm_versions`) для отметок на трендах.

Явная просьба человека (кнопка, `POST /api/content/jobs/{job_id}/llm`)
порогов не знает: раз попросил — значит надо.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from typing import Any

#: Флаги здоровья распознавания, которые говорят о выдуманном тексте, а не
#: о плохой разметке говорящих (см. `quality`).
_ВЫДУМКА = ("phrase", "repeat", "compression")


def мин_длительность(settings: Any) -> float:
    try:
        return max(0.0, float(settings.get("llm_min_duration_s") or 0))
    except (TypeError, ValueError):
        return 0.0


def режим_сомнительных(settings: Any) -> str:
    режим = str(settings.get("llm_suspect_mode") or "mark").strip().lower()
    return режим if режим in ("mark", "skip", "off") else "mark"


def порог_сомнительных(settings: Any) -> float:
    try:
        return min(1.0, max(0.0, float(settings.get("llm_suspect_share", 0.3))))
    except (TypeError, ValueError):
        return 0.3


def сомнительная(job: dict[str, Any], settings: Any) -> str:
    """Почему расшифровке не стоит верить — или пустая строка.

    Доля подозрительных сегментов не меньше порога или флаг выдуманного
    текста (повторы, известные фразы, сжатие).
    """
    if режим_сомнительных(settings) == "off":
        return ""
    причины = []
    try:
        доля = float(job.get("suspect_share") or 0)
    except (TypeError, ValueError):
        доля = 0.0
    порог = порог_сомнительных(settings)
    if порог > 0 and доля >= порог:
        причины.append(f"подозрительных сегментов {round(доля * 100)}%")
    флаги = str(job.get("quality_flags") or "")
    if any(ф in флаги for ф in _ВЫДУМКА):
        причины.append("признаки выдуманного текста")
    return ", ".join(причины)


def пропустить(job: dict[str, Any], settings: Any) -> str:
    """Почему запись не ставить к модели САМОЙ (без просьбы) — или пусто."""
    try:
        длительность = float(job.get("media_duration_s") or 0)
    except (TypeError, ValueError):
        длительность = 0.0
    порог = мин_длительность(settings)
    if порог and str(job.get("source") or "") != "text" and 0 < длительность < порог:
        return f"короче {порог:g} с"
    if режим_сомнительных(settings) == "skip":
        причина = сомнительная(job, settings)
        if причина:
            return f"сомнительная расшифровка: {причина}"
    return ""


def _часы(значение: Any) -> tuple[int, int] | None:
    """«22-7» → (22, 7); пусто или негодное — None (окна нет)."""
    m = re.fullmatch(r"\s*(\d{1,2})\s*[-–—]\s*(\d{1,2})\s*", str(значение or ""))
    if not m:
        return None
    с, до = int(m.group(1)), int(m.group(2))
    if not (0 <= с <= 24 and 0 <= до <= 24) or с == до:
        return None
    return с % 24, до % 24


def в_окне_архива(settings: Any, сейчас: float) -> bool:
    """Можно ли сейчас брать архивные записи (`llm_archive_hours`)."""
    часы = _часы(settings.get("llm_archive_hours"))
    if часы is None:
        return True
    час = dt.datetime.fromtimestamp(сейчас).hour
    с, до = часы
    return с <= час < до if с < до else (час >= с or час < до)


def предел_архива(settings: Any) -> int:
    try:
        return max(0, int(settings.get("llm_archive_daily") or 0))
    except (TypeError, ValueError):
        return 0


def начало_суток(сейчас: float) -> float:
    return dt.datetime.combine(dt.datetime.fromtimestamp(сейчас).date(),
                               dt.time()).timestamp()


def списки_отдела(settings: Any, отдел: str) -> tuple[list[str] | None, list[str] | None]:
    """Причины и исходы отдела (`llm_lists_by_department`) — или (None, None).

    Ключ — название отдела из справочника сотрудников, без регистра. Чего у
    отдела нет (только причины), то берётся из общих списков.
    """
    свои = settings.get("llm_lists_by_department") or {}
    if not isinstance(свои, dict) or not отдел:
        return None, None
    искомое = " ".join(str(отдел).split()).lower()
    for ключ, списки in свои.items():
        if " ".join(str(ключ).split()).lower() != искомое or not isinstance(списки, dict):
            continue
        причины = списки.get("reasons")
        исходы = списки.get("outcomes")
        return ([str(x) for x in причины if str(x).strip()] if isinstance(причины, list) else None,
                [str(x) for x in исходы if str(x).strip()] if isinstance(исходы, list) else None)
    return None, None


#: Настройки, от которых зависят ответы модели. Их смена — отметка на
#: трендах: «доля решённых выросла» после неё — не обязательно правда.
ВЛИЯЮТ = ("llm_backend", "llm_model", "llm_tasks", "llm_reasons", "llm_outcomes",
          "llm_trackers", "llm_scorecard", "llm_fields", "llm_prompt_extra",
          "llm_lists_by_department", "llm_parts_mode", "llm_explain", "llm_mask_pii",
          "llm_context_chars")


def отпечаток(settings: Any, version: int) -> tuple[str, dict[str, Any]]:
    """Отпечаток настройки модели и сами значения (для описания смены)."""
    значения = {к: settings.get(к) for к in ВЛИЯЮТ}
    сырое = json.dumps({"version": version, **значения}, ensure_ascii=False,
                       sort_keys=True, default=str)
    return hashlib.sha256(сырое.encode("utf-8")).hexdigest()[:16], значения


ПОДПИСИ = {
    "llm_backend": "сервер модели", "llm_model": "модель", "llm_tasks": "задачи",
    "llm_reasons": "причины", "llm_outcomes": "исходы", "llm_trackers": "умные трекеры",
    "llm_scorecard": "скоркарта", "llm_fields": "свои поля",
    "llm_prompt_extra": "добавка к подсказке", "llm_lists_by_department": "списки отделов",
    "llm_parts_mode": "длинные разговоры", "llm_explain": "пояснения",
    "llm_mask_pii": "обезличивание", "llm_context_chars": "размер куска",
}


def что_изменилось(прежние: dict[str, Any] | None, новые: dict[str, Any]) -> str:
    if not прежние:
        return "первая отметка"
    изменилось = [ПОДПИСИ.get(к, к) for к in ВЛИЯЮТ if прежние.get(к) != новые.get(к)]
    return ", ".join(изменилось) or "версия подсказок"


__all__ = ["ВЛИЯЮТ", "в_окне_архива", "мин_длительность", "начало_суток", "отпечаток",
           "предел_архива", "пропустить", "режим_сомнительных", "сомнительная",
           "списки_отдела", "что_изменилось"]
