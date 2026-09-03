#!/usr/bin/env python3
"""Распознавание на лету: звук кусками в сервер, текст обратно по ходу.

Пример показывает протокол целиком и работает без микрофона — по умолчанию
он «наговаривает» готовый файл, притворяясь живым источником. Так его можно
запустить где угодно и увидеть настоящий обмен, а не описание обмена.

    python3 examples/stream_microphone.py --file запись.wav
    python3 examples/stream_microphone.py --mic          # нужен sounddevice

Переменные окружения: ASRHUB_URL (по умолчанию ws://127.0.0.1:8080),
ASRHUB_API_KEY.

Зависимость одна: websocket-client (pip install websocket-client). Для
режима микрофона дополнительно sounddevice.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave

DEFAULT_URL = os.environ.get("ASRHUB_URL", "ws://127.0.0.1:8080")
API_KEY = os.environ.get("ASRHUB_API_KEY", "")
RATE = 16000
CHUNK_S = 0.25


def read_wav_chunks(path: str, chunk_s: float = CHUNK_S):
    """Кусочки готового файла — с паузами, как если бы говорили вживую."""
    with wave.open(path, "rb") as handle:
        if handle.getnchannels() != 1 or handle.getframerate() != RATE \
                or handle.getsampwidth() != 2:
            sys.exit(f"Нужен моно WAV {RATE} Гц 16 бит. "
                     f"Приведите файл: ffmpeg -i {path} -ac 1 -ar {RATE} готово.wav")
        frames = int(RATE * chunk_s)
        while True:
            data = handle.readframes(frames)
            if not data:
                return
            yield data
            time.sleep(chunk_s)          # чтобы поток был похож на живой


def read_microphone(chunk_s: float = CHUNK_S):
    try:
        import sounddevice  # type: ignore
    except ModuleNotFoundError:
        sys.exit("Для режима микрофона нужен sounddevice: pip install sounddevice")
    frames = int(RATE * chunk_s)
    with sounddevice.RawInputStream(samplerate=RATE, channels=1, dtype="int16",
                                    blocksize=frames) as stream:
        print("Говорите. Ctrl+C — закончить.\n", file=sys.stderr)
        while True:
            data, _ = stream.read(frames)
            yield bytes(data)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="WAV моно 16 кГц 16 бит")
    source.add_argument("--mic", action="store_true", help="с микрофона")
    parser.add_argument("--model", default="", help="модель, иначе серверная по умолчанию")
    parser.add_argument("--window", type=float, default=0,
                        help="шаг гипотез, секунд (для движков без потока)")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()

    try:
        import websocket  # type: ignore
    except ModuleNotFoundError:
        sys.exit("Нужен websocket-client: pip install websocket-client")

    # Ключ в адресе оседает в журналах прокси, поэтому берём одноразовый билет.
    address = f"{args.url.rstrip('/')}/api/stream"
    if API_KEY:
        import urllib.request

        http = args.url.replace("ws://", "http://").replace("wss://", "https://")
        request = urllib.request.Request(f"{http.rstrip('/')}/api/auth/ticket",
                                         method="POST",
                                         headers={"X-API-Key": API_KEY})
        with urllib.request.urlopen(request, timeout=10) as response:
            ticket = json.load(response).get("ticket", "")
        if ticket:
            address += f"?ticket={ticket}"

    connection = websocket.create_connection(address, timeout=30)

    config: dict[str, object] = {"type": "config", "format": "pcm_s16le"}
    if args.model:
        config["model"] = args.model
    if args.window:
        config["stream_window_s"] = args.window
    connection.send(json.dumps(config, ensure_ascii=False))

    ready = json.loads(connection.recv())
    if ready.get("type") == "error":
        sys.exit(f"Сервер отказал: {ready.get('message')}")
    print(f"Режим: {ready.get('mode')} — {ready.get('note')}\n", file=sys.stderr)

    connection.settimeout(0.05)
    shown = ""

    def drain(final: bool = False) -> None:
        """Читает всё, что сервер успел прислать, не блокируя отправку."""
        nonlocal shown
        while True:
            try:
                message = json.loads(connection.recv())
            except Exception:
                return
            kind = message.get("type")
            if kind == "partial":
                shown = message["text"]
                print(f"\r… {shown[-100:]}", end="", flush=True)
            elif kind == "final":
                print(f"\r✓ {message['text']}", flush=True)
            elif kind == "error":
                print(f"\nОшибка: {message.get('message')}", file=sys.stderr)
                return
            elif kind == "done":
                print(f"\nГотово: {message.get('duration_s')} с звука", file=sys.stderr)
                return
            if not final:
                return

    chunks = read_microphone() if args.mic else read_wav_chunks(args.file)
    try:
        for chunk in chunks:
            connection.send_binary(chunk)
            drain()
    except KeyboardInterrupt:
        print(file=sys.stderr)
    finally:
        connection.settimeout(30)
        connection.send(json.dumps({"type": "finish"}))
        drain(final=True)
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
