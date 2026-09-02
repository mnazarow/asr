#!/usr/bin/env python3
"""Следить за ходом работы через WebSocket, а не опросом.

Опрос раз в несколько секунд прост, но на большом потоке заданий он и
нагружает сервер, и опаздывает. Лента событий приходит сразу.

Ключ передаётся не в адресе: браузерный WebSocket не умеет заголовки,
поэтому сервер выдаёт одноразовый билет на минуту. Здесь показан тот же
приём — адрес с ключом оседает в журналах прокси, и незачем это делать даже
из скрипта.

    python3 examples/live_events.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import requests

try:
    import websockets
except ModuleNotFoundError:
    print("Нужен пакет websockets: pip install websockets", file=sys.stderr)
    raise SystemExit(3) from None

SERVER = os.environ.get("ASRHUB_SERVER", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("ASRHUB_KEY", "")


def ticket() -> str:
    """Берёт одноразовый билет. Пустая строка означает «сервер без ключей»."""
    if not KEY:
        return ""
    response = requests.post(f"{SERVER}/api/auth/ticket",
                             headers={"X-API-Key": KEY}, timeout=30)
    response.raise_for_status()
    return response.json().get("ticket", "")


LABELS = {
    "job.queued": "принято",
    "job.started": "в работе",
    "job.completed": "готово",
    "job.failed": "ошибка",
    "job.retry": "повтор",
    "job.cancelled": "отменено",
    "job.cached": "из кеша",
    "queue.paused": "очередь на паузе",
    "queue.resumed": "очередь запущена",
}


async def main() -> int:
    url = SERVER.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    mark = ticket()
    if mark:
        url += f"?ticket={mark}"

    async with websockets.connect(url) as socket:
        print(f"Подключено: {url.split('?')[0]}")
        # Первое сообщение — hello: в нём состояние очереди и последние
        # события, так что стартовую картину не нужно запрашивать отдельно.
        async for raw in socket:
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "hello":
                queue = message.get("queue", {})
                print(f"  очередь: ждут {queue.get('queue_depth', 0)}, "
                      f"выполняется {queue.get('active', 0)}")
                continue
            if kind == "job.progress":
                progress = float(message.get("progress") or 0) * 100
                print(f"\r  {message['id']} {progress:3.0f}% {message.get('stage', '')}",
                      end="", flush=True)
                continue
            label = LABELS.get(kind, kind)
            extra = ""
            if kind == "job.completed":
                extra = f" RTF {message.get('rtf')}"
            elif kind == "job.failed":
                error = message.get("error") or {}
                extra = f" — {error.get('message', '')}"
            print(f"\r  {label}: {message.get('id', '')}{extra}{' ' * 20}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
