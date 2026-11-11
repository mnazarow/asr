#!/usr/bin/env python3
"""Передать звонок из своей интеграции так же, как это делает диалплан.

Станция «Asterisk: диалплан» принимает звонки не только от сценария
asrhub-dialplan: то же самое может прислать и своя программа — коллтрекинг,
CRM, обработчик записей другой АТС. Достаточно POST на
/api/telephony/dialplan/call с полями звонка и, если есть, файлом записи.

Что важно:

* `uniqueid` — обязательный и постоянный идентификатор звонка. По нему
  сервер отличает повтор от нового звонка: повторная отправка того же
  `uniqueid` ответит «duplicate», а не заведёт второе задание. Поэтому
  при сбое сети звонок можно и нужно слать ещё раз.
* 409 — тот же звонок прямо сейчас принимается другим запросом; 429, 503 и
  5xx — повторить позже (в ответе бывает `retry_after_s`). 4xx остальные —
  ошибка в самом звонке, повтор не поможет.
* `dry_run=1` — проба: сервер проверит поля и ответит, что сделал бы, но
  ничего не запишет.

    python3 examples/telephony_push_call.py запись.wav --uniqueid 1793000000.15 \\
        --src 79161234567 --dst 74950000000 --agent 101 --direction in --billsec 95
    python3 examples/telephony_push_call.py --uniqueid 1793000000.16 --dry-run
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")
HEADERS = {"X-API-Key": KEY} if KEY else {}

#: Коды, после которых звонок стоит прислать ещё раз.
ПОВТОРИТЬ = {408, 409, 425, 429, 500, 502, 503, 504}


def отправить(поля: dict[str, str], запись: Path | None, попыток: int = 5) -> dict:
    """Один звонок с повторами: пауза растёт, `retry_after_s` уважается."""
    пауза = 5.0
    for попытка in range(1, попыток + 1):
        файлы = None
        try:
            if запись is not None:
                файлы = {"file": (запись.name, запись.open("rb"), "audio/wav")}
            ответ = requests.post(f"{SERVER}/api/telephony/dialplan/call", data=поля,
                                  files=файлы, headers=HEADERS, timeout=600)
        except requests.RequestException as exc:
            print(f"попытка {попытка}: сеть — {exc}", file=sys.stderr)
            ответ = None
        finally:
            if файлы:
                файлы["file"][1].close()
        if ответ is not None and ответ.status_code < 400:
            return ответ.json()
        if ответ is not None and ответ.status_code not in ПОВТОРИТЬ:
            # Ошибка в самом звонке — сервер объясняет, что не так.
            тело = ответ.json() if ответ.headers.get("content-type", "").startswith(
                "application/json") else {"message": ответ.text[:300]}
            raise SystemExit(f"сервер отверг звонок ({ответ.status_code}): "
                             f"{тело.get('message')} {тело.get('hint') or ''}")
        if ответ is not None:
            подробности = (ответ.json() or {}).get("details") or {}
            пауза = float(подробности.get("retry_after_s") or пауза)
            print(f"попытка {попытка}: код {ответ.status_code}, повтор через {пауза:.0f} с",
                  file=sys.stderr)
        if попытка < попыток:
            time.sleep(пауза)
            пауза = min(пауза * 2, 300.0)
    raise SystemExit("звонок не доставлен — пришлите его позже тем же uniqueid")


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    разбор.add_argument("file", nargs="?", type=Path, help="запись разговора (wav, mp3, …)")
    разбор.add_argument("--uniqueid", required=True)
    разбор.add_argument("--station", default="", help="станция; пусто — единственная")
    разбор.add_argument("--src", default="")
    разбор.add_argument("--dst", default="")
    разбор.add_argument("--agent", default="", help="добавочный оператора")
    разбор.add_argument("--direction", default="", help="in, out или internal")
    разбор.add_argument("--queue", default="")
    разбор.add_argument("--billsec", default="", help="секунд разговора")
    разбор.add_argument("--duration", default="", help="секунд всего звонка")
    разбор.add_argument("--started-at", default="", help="начало, секунды эпохи")
    разбор.add_argument("--linkedid", default="", help="общий идентификатор ног разговора")
    разбор.add_argument("--dry-run", action="store_true", help="проба: ничего не записывать")
    аргументы = разбор.parse_args()

    поля = {"uniqueid": аргументы.uniqueid, "station": аргументы.station,
            "src": аргументы.src, "dst": аргументы.dst, "agent": аргументы.agent,
            "direction": аргументы.direction, "queue": аргументы.queue,
            "billsec": аргументы.billsec, "duration": аргументы.duration,
            "started_at": аргументы.started_at, "linkedid": аргументы.linkedid,
            "disposition": "ANSWERED"}
    if аргументы.file is None:
        поля["ready"] = "1"
    if аргументы.dry_run:
        поля["dry_run"] = "1"
    поля = {к: з for к, з in поля.items() if з}
    итог = отправить(поля, аргументы.file)
    print(f"{итог.get('result')}: ключ {итог.get('key')}"
          + (f", задание {итог['job_id']}" if итог.get("job_id") else "")
          + (f" ({итог.get('skipped') or итог.get('deferred') or ''})"
             if итог.get("skipped") or итог.get("deferred") else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
