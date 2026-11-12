"""События перевода из AMI — точная граница отрезков разговора с переводом.

Отрезки записи по участникам (`telephony.segments`) раскладываются по
времени ног CDR: у ноги есть начало, длительность и время разговора, а
граница перевода получается из них с точностью до секунд. При переводе с
предупреждением этого мало: второй оператор снимает трубку на консультации,
пока клиент слушает музыку ожидания, и по CDR его окно начинается раньше,
чем клиент его слышит.

Asterisk 12+ сообщает о переводе событиями `BlindTransfer` и
`AttendedTransfer` (класс `call`): кто переводил, кому и когда. Здесь они
превращаются в строки `call_transfers` — станция, `linkedid` разговора,
момент, вид, добавочные «от» и «к». Номер вне станции (перевод на
городской или мобильный) не хранится: в разрезах по сотрудникам он не
нужен, а хранить его — значит отвечать за него при удалении по требованию.
"""
from __future__ import annotations

import time
from typing import Any

from .queue_log import _оператор

#: События AMI, которые здесь разбираются (имена в нижнем регистре).
СОБЫТИЯ = frozenset({"blindtransfer", "attendedtransfer"})

#: Сколько цифр может быть у добавочного. Длиннее — номер вне станции.
ЦИФР_ДОБАВОЧНОГО = 6


def _добавочный(значение: Any) -> str:
    """Добавочный из канала или номера; номер вне станции — пусто."""
    текст = str(значение or "").strip()
    if "/" in текст:
        текст = _оператор(текст)
    текст = текст.strip()[:32]
    цифры = sum(1 for з in текст if з.isdigit())
    if not текст or цифры > ЦИФР_ДОБАВОЧНОГО or текст.startswith("+"):
        return ""
    return текст


def _число(значение: Any) -> float:
    try:
        return float(значение)
    except (TypeError, ValueError):
        return 0.0


def из_события_ami(событие: dict[str, Any], *,
                   сейчас: float | None = None) -> dict[str, Any] | None:
    """`BlindTransfer`/`AttendedTransfer` → строка `call_transfers`; None — не наше.

    Неудачный перевод (`Result` не `Success`) не граница: разговор остался
    у того же оператора. `linkedid` — разговора клиента (`TransfereeLinkedid`:
    переводят именно его), без него — переводившего. Время — `Timestamp`
    (при `timestampevents=yes` в manager.conf), иначе время приёма.
    """
    вид = str(событие.get("Event") or "").strip().lower()
    if вид not in СОБЫТИЯ:
        return None
    if str(событие.get("Result") or "Success").strip().lower() != "success":
        return None
    момент = _число(событие.get("Timestamp")) or (сейчас if сейчас is not None else time.time())
    if вид == "blindtransfer":
        связь = событие.get("TransfereeLinkedid") or событие.get("TransfererLinkedid")
        от = _добавочный(событие.get("TransfererChannel"))
        к = _добавочный(событие.get("Extension"))
        род = "blind"
    else:
        связь = (событие.get("TransfereeLinkedid") or событие.get("OrigTransfererLinkedid")
                 or событие.get("SecondTransfererLinkedid"))
        от = _добавочный(событие.get("OrigTransfererChannel")
                         or событие.get("SecondTransfererChannel"))
        к = _добавочный(событие.get("TransferTargetChannel") or событие.get("Extension"))
        род = "attended"
    связь = str(связь or "").strip()[:64]
    if not связь:
        return None
    return {"ts": момент, "linkedid": связь, "kind": род, "from_agent": от, "to_agent": к}


# ---------------------------------------------------------------------------
# Журнал CEL — для станций без AMI (журнал CDR, каталог записей)
# ---------------------------------------------------------------------------

#: События CEL о переводе (Asterisk 12+).
СОБЫТИЯ_CEL = frozenset({"BLINDTRANSFER", "ATTENDEDTRANSFER"})

#: Строка `cel_custom.conf`, которой станция пишет журнал для сервера:
#: событие, время, канал, uniqueid, linkedid, номер и `eventextra` (JSON с
#: адресатом перевода). Порядок полей — тот, что читает `из_строки_cel`.
ОТОБРАЖЕНИЕ_CEL = (
    "asrhub-cel.csv => ${CSV_QUOTE(${eventtype})},${CSV_QUOTE(${eventtime})},"
    "${CSV_QUOTE(${CHANNEL(channame)})},${CSV_QUOTE(${CHANNEL(uniqueid)})},"
    "${CSV_QUOTE(${CHANNEL(linkedid)})},${CSV_QUOTE(${CHANNEL(exten)})},"
    "${CSV_QUOTE(${eventextra})}")

#: Сколько байт журнала читать за заход.
ПОРЦИЯ_CEL_БАЙТ = 4 * 1024 * 1024


def _момент_cel(значение: str, пояс: Any = None) -> float:
    """Время CEL: секунды с микросекундами (по умолчанию) или `dateformat`."""
    import datetime as dt  # noqa: PLC0415

    текст = str(значение or "").strip()
    число = _число(текст)
    if число > 1e9:
        return число
    for формат in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            момент = dt.datetime.strptime(текст, формат)
        except ValueError:
            continue
        if момент.tzinfo is None and пояс is not None:
            момент = момент.replace(tzinfo=пояс)
        return момент.timestamp()
    return 0.0


def из_строки_cel(поля: list[str], *, пояс: Any = None) -> dict[str, Any] | None:
    """Строка журнала CEL (`ОТОБРАЖЕНИЕ_CEL`) → строка `call_transfers`; None — не наша.

    `BLINDTRANSFER` пишется на канале переводившего, адресат — `extension`
    в `eventextra`; `ATTENDEDTRANSFER` (мост к мосту) — на канале
    переводившего, адресат — `transfer_target_channel_name`. Перевод в
    приложение (`app` в `eventextra`) адресата-добавочного не имеет.
    """
    import json  # noqa: PLC0415

    if len(поля) < 7:
        return None
    вид = поля[0].strip().upper()
    if вид not in СОБЫТИЯ_CEL:
        return None
    момент = _момент_cel(поля[1], пояс)
    связь = поля[4].strip()[:64]
    if момент <= 0 or not связь:
        return None
    try:
        доп = json.loads(поля[6]) if поля[6].strip() else {}
    except ValueError:
        доп = {}
    if not isinstance(доп, dict):
        доп = {}
    if вид == "BLINDTRANSFER":
        к = _добавочный(доп.get("extension"))
        род = "blind"
    else:
        к = _добавочный(доп.get("transfer_target_channel_name"))
        род = "attended"
    return {"ts": момент, "linkedid": связь, "kind": род,
            "from_agent": _добавочный(поля[2]), "to_agent": к}


def прочитать_cel(путь: Any, позиция: int, inode: int, *, пояс: Any = None
                  ) -> tuple[list[dict[str, Any]], int, int]:
    """Новые строки журнала CEL с позиции; повёрнутый журнал — с начала.

    Как у `queue_log`: другой inode или файл короче позиции — журнал
    повернули, читаем сначала; хвост без перевода строки ждёт следующего
    захода.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415
    import os  # noqa: PLC0415

    сведения = os.stat(путь)
    if сведения.st_ino != inode or сведения.st_size < позиция:
        позиция = 0
    with open(путь, "rb") as файл:
        файл.seek(позиция)
        кусок = файл.read(ПОРЦИЯ_CEL_БАЙТ)
    конец = кусок.rfind(b"\n")
    if конец < 0:
        return [], позиция, сведения.st_ino
    целое = кусок[:конец + 1]
    итог = []
    for поля in csv.reader(io.StringIO(целое.decode("utf-8", "replace"))):
        перевод = из_строки_cel(поля, пояс=пояс)
        if перевод is not None:
            итог.append(перевод)
    return итог, позиция + len(целое), сведения.st_ino
