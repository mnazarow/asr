#!/usr/bin/env python3
"""Приёмник уведомлений ASR Hub с проверкой подписи.

Сервер подписывает тело уведомления: заголовок `X-ASRHub-Signature-256` —
`sha256=` и HMAC-SHA256 от «время.тело» на секрете `webhook_secret`, а
`X-ASRHub-Timestamp` — время отправки. Приёмник обязан:

* сверить подпись постоянным по времени сравнением (`hmac.compare_digest`);
* отбросить уведомление старше пяти минут — иначе перехваченное можно
  прислать снова через месяц;
* ответить 2xx быстро: долгая обработка — после ответа, иначе сервер
  посчитает доставку неудачной и повторит.

    WEBHOOK_SECRET=секрет python3 examples/webhook_receiver.py --port 9000
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

СЕКРЕТ = os.environ.get("WEBHOOK_SECRET", "").encode("utf-8")
#: Сколько секунд уведомление считается свежим.
СВЕЖЕСТЬ_С = 300


def подпись_верна(тело: bytes, момент: str, подпись: str, секрет: bytes = СЕКРЕТ) -> bool:
    """Подпись сервера: sha256=HMAC(секрет, «время.тело»), не старше пяти минут."""
    if not секрет or not момент.isdigit() or not подпись.startswith("sha256="):
        return False
    if abs(time.time() - int(момент)) > СВЕЖЕСТЬ_С:
        return False
    ожидается = hmac.new(секрет, момент.encode() + b"." + тело, hashlib.sha256).hexdigest()
    return hmac.compare_digest(ожидается, подпись[len("sha256="):])


class Приёмник(BaseHTTPRequestHandler):
    def do_POST(self) -> None:                                # noqa: N802
        тело = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if not подпись_верна(тело, self.headers.get("X-ASRHub-Timestamp") or "",
                             self.headers.get("X-ASRHub-Signature-256") or ""):
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(204)
        self.end_headers()
        данные = json.loads(тело or b"{}")
        # Дальше — своё дело: поставить в свою очередь, записать в CRM…
        print(f"{self.headers.get('X-ASRHub-Event')}: {данные.get('id')} "
              f"{данные.get('status')} {данные.get('filename') or ''}", flush=True)

    def log_message(self, *_: object) -> None:
        return


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--port", type=int, default=9000)
    разбор.add_argument("--host", default="127.0.0.1")
    аргументы = разбор.parse_args()
    if not СЕКРЕТ:
        print("Задайте WEBHOOK_SECRET — тот же, что webhook_secret на сервере.")
        return 2
    ThreadingHTTPServer((аргументы.host, аргументы.port), Приёмник).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
