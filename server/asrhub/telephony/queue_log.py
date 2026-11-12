"""События очередей Asterisk из `queue_log`.

Без них очередь в архиве — только имя из `lastdata`, а брошенный в очереди
звонок неотличим от «без ответа». `queue_log` пишет на каждое событие
строку `время|звонок|очередь|оператор|событие|данные…`:

* ENTERQUEUE — звонок встал в очередь;
* CONNECT — оператор ответил: `данные1` — сколько ждал (с);
* ABANDON — звонящий бросил трубку: `позиция|исходная|ждал`;
* EXITWITHTIMEOUT, EXITEMPTY, EXITWITHKEY — ушёл из очереди без ответа:
  `позиция|исходная|ждал`;
* RINGNOANSWER — оператору звонили, он не взял: `данные1` — сколько
  звонило (мс);
* COMPLETECALLER, COMPLETEAGENT — разговор кончился: `ждал|говорили|…`.

Читается двумя путями: станция, чей `queue_log` виден серверу (поле
станции `queue_log`), — по приращению с запомненной позиции; станция за
агентом — строками через `POST /api/telephony/agent/queue_log`.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

#: Какие события берём: остальные (CONFIGRELOAD, PAUSE, ADDMEMBER…) для
#: разреза ожидания и потерь не нужны.
СОБЫТИЯ = {"ENTERQUEUE", "CONNECT", "ABANDON", "EXITWITHTIMEOUT", "EXITEMPTY",
           "EXITWITHKEY", "RINGNOANSWER", "COMPLETECALLER", "COMPLETEAGENT",
           "TRANSFER", "RINGCANCELED"}
#: Ушёл из очереди, не дождавшись ответа.
ПОТЕРИ = {"ABANDON", "EXITWITHTIMEOUT", "EXITEMPTY", "EXITWITHKEY"}
#: Сколько байт читать за заход: журнал станции за месяц — сотни мегабайт.
ПОРЦИЯ_БАЙТ = 4 * 1024 * 1024


def _число(значение: Any) -> float:
    try:
        return float(значение)
    except (TypeError, ValueError):
        return 0.0


def _оператор(значение: str) -> str:
    """«PJSIP/101», «Local/101@from-queue/n», «Agent/101» → «101»."""
    текст = str(значение or "").strip()
    if текст in ("", "NONE"):
        return ""
    if "/" in текст:
        текст = текст.split("/", 1)[1]
    return текст.split("@", 1)[0].split("-", 1)[0][:64]


def разобрать_строку(строка: str) -> dict[str, Any] | None:
    """Строка `queue_log` → событие; None — не наша или порченая."""
    части = строка.rstrip("\r\n").split("|")
    if len(части) < 5:
        return None
    событие = части[4].strip().upper()
    if событие not in СОБЫТИЯ:
        return None
    момент = _число(части[0])
    if момент <= 0:
        return None
    данные = части[5:] + ["", "", "", ""]
    итог: dict[str, Any] = {
        "ts": момент, "callid": части[1].strip()[:64], "queue": части[2].strip()[:64],
        "agent": _оператор(части[3]), "event": событие,
        "wait_s": None, "ring_s": None, "talk_s": None,
    }
    if событие == "CONNECT":
        итог["wait_s"] = _число(данные[0])
        # Asterisk 12+: CONNECT|ждал|uniqueid ноги оператора|звонило(с)
        if данные[2]:
            итог["ring_s"] = _число(данные[2])
    elif событие in ПОТЕРИ:
        итог["wait_s"] = _число(данные[2])
    elif событие == "RINGNOANSWER":
        итог["ring_s"] = _число(данные[0]) / 1000.0
    elif событие in ("COMPLETECALLER", "COMPLETEAGENT"):
        итог["wait_s"] = _число(данные[0])
        итог["talk_s"] = _число(данные[1])
    return итог


def разобрать(текст: str) -> list[dict[str, Any]]:
    return [с for с in (разобрать_строку(л) for л in текст.splitlines()) if с]


def прочитать_приращение(путь: Path, позиция: int, inode: int) -> tuple[list[dict[str, Any]],
                                                                        int, int]:
    """Новые строки журнала с позиции. Повёрнутый журнал (другой inode или
    файл короче позиции) читается с начала."""
    сведения = os.stat(путь)
    if сведения.st_ino != inode or сведения.st_size < позиция:
        позиция = 0
    with open(путь, "rb") as файл:
        файл.seek(позиция)
        кусок = файл.read(ПОРЦИЯ_БАЙТ)
    # Только целые строки: хвост без перевода строки дочитается в следующий раз.
    конец = кусок.rfind(b"\n")
    if конец < 0:
        return [], позиция, сведения.st_ino
    целое = кусок[:конец + 1]
    return (разобрать(целое.decode("utf-8", "replace")), позиция + len(целое),
            сведения.st_ino)


def свод(события: list[dict[str, Any]], *, порог_с: float = 20.0) -> dict[str, Any]:
    """Свод по очередям и операторам: ожидание, потери, уровень обслуживания.

    Уровень обслуживания — доля звонков, на которые ответили не дольше
    `порог_с`, среди всех, кто дождался ответа или ушёл (как его считает
    Asterisk в `servicelevel`).
    """
    очереди: dict[str, dict[str, Any]] = {}
    операторы: dict[str, dict[str, Any]] = {}
    for с in события:
        о = очереди.setdefault(с["queue"] or "—", {
            "queue": с["queue"] or "—", "entered": 0, "answered": 0, "lost": 0,
            "abandoned": 0, "wait_sum": 0.0, "lost_wait_sum": 0.0, "within_sl": 0,
            "ringnoanswer": 0, "talk_sum": 0.0})
        вид = с["event"]
        if вид == "ENTERQUEUE":
            о["entered"] += 1
        elif вид == "CONNECT":
            о["answered"] += 1
            о["wait_sum"] += float(с.get("wait_s") or 0)
            if float(с.get("wait_s") or 0) <= порог_с:
                о["within_sl"] += 1
        elif вид in ПОТЕРИ:
            о["lost"] += 1
            о["abandoned"] += 1 if вид == "ABANDON" else 0
            о["lost_wait_sum"] += float(с.get("wait_s") or 0)
        elif вид == "RINGNOANSWER":
            о["ringnoanswer"] += 1
        elif вид in ("COMPLETECALLER", "COMPLETEAGENT"):
            о["talk_sum"] += float(с.get("talk_s") or 0)
        if с.get("agent"):
            а = операторы.setdefault(с["agent"], {"agent": с["agent"], "answered": 0,
                                                 "ringnoanswer": 0, "talk_sum": 0.0})
            if вид == "CONNECT":
                а["answered"] += 1
            elif вид == "RINGNOANSWER":
                а["ringnoanswer"] += 1
            elif вид in ("COMPLETECALLER", "COMPLETEAGENT"):
                а["talk_sum"] += float(с.get("talk_s") or 0)
    for о in очереди.values():
        исходов = о["answered"] + о["lost"]
        о["avg_wait_s"] = round(о["wait_sum"] / о["answered"], 1) if о["answered"] else None
        о["avg_lost_wait_s"] = (round(о["lost_wait_sum"] / о["lost"], 1) if о["lost"]
                                else None)
        о["lost_share"] = round(о["lost"] / исходов * 100, 1) if исходов else None
        о["service_level"] = (round(о["within_sl"] / исходов * 100, 1) if исходов else None)
    return {"threshold_s": порог_с,
            "queues": sorted(очереди.values(), key=lambda о: -о["entered"]),
            "agents": sorted(операторы.values(),
                             key=lambda а: (-а["ringnoanswer"], -а["answered"]))}


# ---------------------------------------------------------------------------
# События очередей из AMI — для станций без доступного queue_log
# ---------------------------------------------------------------------------

#: Событие AMI → событие queue_log. Класс событий AMI — `agent`.
СОБЫТИЯ_AMI = {
    "queuecallerjoin": "ENTERQUEUE", "join": "ENTERQUEUE",
    "queuecallerabandon": "ABANDON",
    "agentconnect": "CONNECT",
    "agentcomplete": "COMPLETEAGENT",
    "agentringnoanswer": "RINGNOANSWER",
}


def из_события_ami(событие: dict[str, Any], *, сейчас: float | None = None) -> dict[str, Any] | None:
    """Событие AMI класса `agent` → строка разреза очередей; None — не наше.

    Время — из `Timestamp` (при `timestampevents=yes` в manager.conf), иначе
    время приёма. `HoldTime` — сколько звонок ждал, `TalkTime` — разговор,
    `RingTime` у `AgentConnect` — секунды, у `AgentRingNoAnswer` —
    миллисекунды (так их отдаёт Asterisk).
    """
    import time  # noqa: PLC0415

    вид = СОБЫТИЯ_AMI.get(str(событие.get("Event") or "").strip().lower())
    if вид is None:
        return None
    if вид == "COMPLETEAGENT" and str(событие.get("Reason") or "").lower() == "caller":
        вид = "COMPLETECALLER"
    момент = _число(событие.get("Timestamp")) or (сейчас if сейчас is not None else time.time())
    оператор = событие.get("MemberName") or событие.get("Interface") or ""
    if вид in ("CONNECT", "COMPLETEAGENT", "COMPLETECALLER", "RINGNOANSWER"):
        оператор = событие.get("Interface") or событие.get("MemberName") or ""
    итог: dict[str, Any] = {
        "ts": момент, "callid": str(событие.get("Uniqueid") or событие.get("UniqueID") or "")[:64],
        "queue": str(событие.get("Queue") or "")[:64],
        "agent": _оператор(str(оператор)) if вид != "ENTERQUEUE" and вид != "ABANDON" else "",
        "event": вид, "wait_s": None, "ring_s": None, "talk_s": None,
    }
    if вид in ("CONNECT", "ABANDON", "COMPLETEAGENT", "COMPLETECALLER"):
        итог["wait_s"] = _число(событие.get("HoldTime"))
    if вид == "CONNECT" and событие.get("RingTime") not in (None, ""):
        итог["ring_s"] = _число(событие.get("RingTime"))
    if вид == "RINGNOANSWER":
        итог["ring_s"] = _число(событие.get("RingTime")) / 1000.0
    if вид in ("COMPLETEAGENT", "COMPLETECALLER"):
        итог["talk_s"] = _число(событие.get("TalkTime"))
    return итог
