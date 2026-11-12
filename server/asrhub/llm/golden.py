"""Золотой набор: ответы человека, по которым проверяется модель.

Смена модели или подсказки без проверки — прыжок в неизвестность: «доля
решённых выросла» может значить и «стали лучше работать», и «новая модель
чаще говорит «решён»». Золотой набор — два-три десятка разговоров, по
которым человек сам отметил причину, исход, решён ли вопрос, трекеры и
ответы скоркарты. По нему считается точность:

* **текущих ответов** — то, что модель уже сказала по этим записям;
* **прогона** — разбор этих же записей заново, с текущими настройками или
  с подменой части из них (другая модель, другие списки, добавка к
  подсказке). Подмена действует только на прогон: настройки сервера не
  меняются. Так новая подсказка сверяется со старой ДО выката.

Прогон идёт в своём потоке, по одной записи, и уступает только себе:
разбор записей сервера в это время не останавливается, а модель одна —
прогон золотого набора просто занимает очередь к ней.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..logging_setup import get_logger
from . import tasks
from .client import LLMClient

log = get_logger("llm")

#: Что можно подменить на время прогона.
ПОДМЕНА = ("llm_model", "llm_reasons", "llm_outcomes", "llm_trackers", "llm_scorecard",
           "llm_fields", "llm_prompt_extra", "llm_parts_mode", "llm_explain",
           "llm_context_chars", "llm_tasks")

ПОЛЯ = ("reason", "outcome", "resolved", "trackers", "scorecard", "fields")


class Наложение:
    """Настройки сервера с подменой части ключей — только для прогона."""

    def __init__(self, основа: Any, своё: dict[str, Any]):
        self.основа, self.своё = основа, dict(своё)

    def get(self, ключ: str, умолчание: Any = None) -> Any:
        if ключ in self.своё:
            return self.своё[ключ]
        return self.основа.get(ключ, умолчание)


def эталон(данные: dict[str, Any]) -> dict[str, Any]:
    """Ответы человека в принятой форме; лишнее отбрасывается."""
    итог: dict[str, Any] = {}
    for ключ in ("reason", "outcome"):
        if данные.get(ключ):
            итог[ключ] = str(данные[ключ])[:200]
    if данные.get("resolved") is not None:
        итог["resolved"] = bool(данные["resolved"])
    for ключ in ("trackers",):
        if isinstance(данные.get(ключ), dict):
            итог[ключ] = {str(к)[:64]: bool(з) for к, з in list(данные[ключ].items())[:50]}
    if isinstance(данные.get("scorecard"), dict):
        итог["scorecard"] = {str(к)[:64]: str(з) for к, з in list(данные["scorecard"].items())[:50]
                             if str(з) in ("да", "нет", "н/п")}
    if isinstance(данные.get("fields"), dict):
        итог["fields"] = {str(к)[:64]: (str(з)[:200] if з not in (None, "") else None)
                          for к, з in list(данные["fields"].items())[:20]}
    return итог


def _норма(значение: Any) -> str:
    return "".join(ch for ch in str(значение or "").lower().replace("ё", "е") if ch.isalnum())


def сравнить(эталон_: dict[str, Any], ответ: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Совпадения ответа модели с эталоном: {поле: {"n": сколько, "ok": верно}}."""
    итог: dict[str, dict[str, int]] = {п: {"n": 0, "ok": 0} for п in ПОЛЯ}
    for поле in ("reason", "outcome"):
        if поле in эталон_:
            итог[поле]["n"] += 1
            итог[поле]["ok"] += int(_норма(эталон_[поле]) == _норма(ответ.get(поле)))
    if "resolved" in эталон_:
        итог["resolved"]["n"] += 1
        итог["resolved"]["ok"] += int(ответ.get("resolved") is эталон_["resolved"]
                                      or ответ.get("resolved") == эталон_["resolved"])
    модель_т = {str(т.get("id")): bool(т.get("fired")) for т in (ответ.get("trackers") or [])
                if isinstance(т, dict)}
    for ид, сработал in (эталон_.get("trackers") or {}).items():
        итог["trackers"]["n"] += 1
        итог["trackers"]["ok"] += int(модель_т.get(ид) is сработал)
    модель_с = {str(о.get("id")): о.get("answer") for о in (ответ.get("scorecard") or [])
                if isinstance(о, dict)}
    for ид, ответ_ч in (эталон_.get("scorecard") or {}).items():
        итог["scorecard"]["n"] += 1
        итог["scorecard"]["ok"] += int(модель_с.get(ид) == ответ_ч)
    модель_п = ответ.get("fields") or {}
    for ид, значение in (эталон_.get("fields") or {}).items():
        итог["fields"]["n"] += 1
        найдено = модель_п.get(ид) if isinstance(модель_п, dict) else None
        итог["fields"]["ok"] += int(_норма(найдено) == _норма(значение)
                                    or (значение is None and найдено is None))
    return итог


def свести(сравнения: list[dict[str, dict[str, int]]]) -> dict[str, Any]:
    """Точность по полям и в целом — доли от 0 до 1."""
    по_полям = {}
    всего = верно = 0
    for поле in ПОЛЯ:
        n = sum(с[поле]["n"] for с in сравнения)
        ok = sum(с[поле]["ok"] for с in сравнения)
        всего += n
        верно += ok
        по_полям[поле] = {"n": n, "ok": ok, "accuracy": round(ok / n, 3) if n else None}
    return {"fields": по_полям, "n": всего,
            "accuracy": round(верно / всего, 3) if всего else None}


def точность_текущих(db: Any, *, owner: Any = None) -> dict[str, Any]:
    """Точность ответов, которые модель уже дала по записям набора."""
    набор = db.golden_list(owner=owner)
    сравнения = []
    for запись in набор:
        ответ = db.llm_get(запись["job_id"]) or {}
        if ответ and not ответ.get("error"):
            сравнения.append(сравнить(запись["answers"], ответ))
    return {"records": len(набор), "answered": len(сравнения), **свести(сравнения)}


def запустить(state: Any, *, overrides: dict[str, Any], label: str, by: str,
              owner: Any = None) -> int:
    """Прогон набора в фоне. Возвращает номер прогона."""
    подмена = {к: з for к, з in (overrides or {}).items() if к in ПОДМЕНА}
    набор = state.db.golden_list(owner=owner)
    номер = state.db.golden_run_create(label=label, overrides=подмена, records=len(набор),
                                       by=by)
    threading.Thread(target=_прогон, args=(state, номер, подмена, набор),
                     name="asrhub-llm-golden", daemon=True).start()
    return номер


def _прогон(state: Any, номер: int, подмена: dict[str, Any],
            набор: list[dict[str, Any]]) -> None:
    db = state.db
    настройки = Наложение(state.settings, подмена)
    клиент = LLMClient(настройки, db)
    сравнения = []
    записи = []
    начало = time.time()
    try:
        for n, запись in enumerate(набор, 1):
            ид = запись["job_id"]
            job = db.get_job(ид) or {}
            разбор = db.get_content(ид) or {}
            try:
                ответ = tasks.analyze(
                    клиент, text=str(job.get("text") or ""), segments=db.get_segments(ид),
                    settings=настройки, agent_speaker=str(разбор.get("agent_speaker") or ""),
                    script=list(state.settings.get("content_script") or []))
                сравнение = сравнить(запись["answers"], ответ)
                сравнения.append(сравнение)
                записи.append({"job_id": ид, "filename": запись.get("filename"),
                               "ok": sum(с["ok"] for с in сравнение.values()),
                               "n": sum(с["n"] for с in сравнение.values()),
                               "answer": {к: ответ.get(к) for к in ("reason", "outcome",
                                                                     "resolved")}})
            except Exception as exc:                         # noqa: BLE001
                записи.append({"job_id": ид, "filename": запись.get("filename"),
                               "error": str(exc)[:300]})
            db.golden_run_update(номер, done=n)
        db.golden_run_update(номер, status="готово", finished_at=time.time(),
                             result={**свести(сравнения), "records": записи,
                                     "seconds": round(time.time() - начало, 1),
                                     "model": клиент.model})
    except Exception as exc:                                 # noqa: BLE001
        log.warning("Прогон золотого набора %s сорвался: %s", номер, exc)
        db.golden_run_update(номер, status="ошибка", finished_at=time.time(),
                             error=str(exc)[:500])


def ждать(db: Any, номер: int, timeout: float = 30.0) -> dict[str, Any] | None:
    """Дождаться конца прогона — для проверок."""
    крайний = time.time() + timeout
    while time.time() < крайний:
        прогон = next((п for п in db.golden_runs(50) if п["id"] == номер), None)
        if прогон and прогон["status"] != "идёт":
            return прогон
        time.sleep(0.05)
    return None


__all__ = ["Наложение", "ПОДМЕНА", "запустить", "сравнить", "свести", "точность_текущих",
           "эталон"]
