"""Живой разговор со станции: приём звука по протоколу AudioSocket.

Asterisk 16.6+ умеет отдавать звук канала по TCP приложением
`AudioSocket(<uuid>,<узел>:<порт>)` (и каналом `AudioSocket/…`). Протокол
простой: кадр — байт вида, два байта длины (big-endian) и данные:

* 0x01 — идентификатор разговора (UUID, 16 байт), первым кадром;
* 0x10 — звук: PCM 16 бит, 8 кГц, моно (обычно 320 байт = 20 мс);
* 0x00 — конец (трубку положили), 0xff — ошибка станции.

Сервер распознаёт звук на лету той же сессией, что `/api/stream`
(`StreamSession`), и рассылает фразы по веб-сокету событиями `call.live`
(`partial` — черновик, `final` — закреплённая фраза). Итог разговора — в
ленте событий («audiosocket»). Адресат событий — владелец из настройки
`audiosocket_owner` (пусто — только администраторам): текст живого
разговора не должен уходить всем ключам подряд.
"""
from __future__ import annotations

import array
import contextlib
import socket
import socketserver
import struct
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .. import settings_access as S
from ..logging_setup import get_logger

log = get_logger("telephony")

ВИД_КОНЕЦ, ВИД_ИД, ВИД_ЗВУК, ВИД_ОШИБКА = 0x00, 0x01, 0x10, 0xFF
#: Предел одного кадра: станция шлёт по 320 байт, больше — не AudioSocket.
ПРЕДЕЛ_КАДРА = 64 * 1024
#: Сколько молчать в сокете, прежде чем считать разговор оборванным.
ТИШИНА_СОКЕТА_С = 30.0


def в_16к(pcm8: bytes) -> bytes:
    """8 кГц → 16 кГц линейной интерполяцией (PCM 16 бит, моно)."""
    отсчёты = array.array("h")
    отсчёты.frombytes(pcm8[:len(pcm8) - len(pcm8) % 2])
    if not отсчёты:
        return b""
    итог = array.array("h", bytes(len(отсчёты) * 4))
    for н, о in enumerate(отсчёты):
        следующий = отсчёты[н + 1] if н + 1 < len(отсчёты) else о
        итог[2 * н] = о
        итог[2 * н + 1] = (о + следующий) // 2
    return итог.tobytes()


def кадр(вид: int, данные: bytes = b"") -> bytes:
    return struct.pack("!BH", вид, len(данные)) + данные


def _прочитать(сокет: socket.socket, сколько: int) -> bytes:
    данные = bytearray()
    while len(данные) < сколько:
        кусок = сокет.recv(сколько - len(данные))
        if not кусок:
            raise EOFError
        данные.extend(кусок)
    return bytes(данные)


class ПриёмAudioSocket:
    """TCP-сервер AudioSocket: разговор — сессия потокового распознавания."""

    def __init__(self, registry: Any, settings: Any,
                 опубликовать: Callable[[str, dict[str, Any]], None],
                 db: Any = None) -> None:
        self.registry = registry
        self.settings = settings
        self.опубликовать = опубликовать
        self.db = db
        self._сервер: socketserver.ThreadingTCPServer | None = None
        self._поток: threading.Thread | None = None
        self._идут: dict[str, dict[str, Any]] = {}
        self._замок = threading.Lock()

    @property
    def адрес(self) -> tuple[str, int] | None:
        if self._сервер is None:
            return None
        узел, порт = self._сервер.server_address[:2]
        return str(узел), int(порт)

    def start(self) -> bool:
        порт = int(self.settings.get("audiosocket_port") or 0)
        if порт <= 0 or self._сервер is not None:
            return False
        узел = str(self.settings.get("audiosocket_host") or "127.0.0.1")
        приём = self

        class Обработчик(socketserver.BaseRequestHandler):
            def handle(self) -> None:                       # noqa: D401
                приём._разговор(self.request, self.client_address)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._сервер = socketserver.ThreadingTCPServer((узел, порт), Обработчик)
        self._сервер.daemon_threads = True
        self._поток = threading.Thread(target=self._сервер.serve_forever,
                                       name="asrhub-audiosocket", daemon=True)
        self._поток.start()
        log.info("AudioSocket: приём живых разговоров на %s:%s", узел, порт)
        return True

    def stop(self) -> None:
        if self._сервер is not None:
            self._сервер.shutdown()
            self._сервер.server_close()
            self._сервер = None

    def идут(self) -> list[dict[str, Any]]:
        """Разговоры, которые идут сейчас: идентификатор, начало, последняя фраза."""
        with self._замок:
            return [{"uuid": к, "since": з["since"], "peer": з["peer"],
                     "phrases": len(з["final"]), "last": (з["final"] or [""])[-1]}
                    for к, з in self._идут.items()]

    # --- один разговор ------------------------------------------------------

    def _событие(self, ид: str, данные: dict[str, Any]) -> None:
        владелец = str(self.settings.get("audiosocket_owner") or "")
        try:
            self.опубликовать("call.live", {"uuid": ид, "_owner": владелец, **данные})
        except Exception as exc:                            # noqa: BLE001
            log.debug("Событие живого разговора не разослано: %s", exc)

    def _разговор(self, сокет: socket.socket, откуда: Any) -> None:
        from ..streaming import StreamSession  # noqa: PLC0415

        сокет.settimeout(ТИШИНА_СОКЕТА_С)
        ид = ""
        сессия: Any = None
        запись: dict[str, Any] = {}
        try:
            while True:
                try:
                    вид, длина = struct.unpack("!BH", _прочитать(сокет, 3))
                    данные = _прочитать(сокет, длина) if длина else b""
                except (EOFError, OSError, TimeoutError):
                    break
                if длина > ПРЕДЕЛ_КАДРА:
                    break
                if вид == ВИД_ИД:
                    if сессия is not None:
                        # Второй идентификатор в том же соединении: прежняя
                        # сессия осталась бы открытой — с моделью и буфером.
                        continue
                    предел = S.integer(self.settings, "stream_max_sessions", 16) or 16
                    with self._замок:
                        занято = len(self._идут)
                    if занято >= предел:
                        # Тот же предел, что у диктовки: каждое соединение —
                        # сессия распознавания, и без предела порт, открытый
                        # сети станции, занимал бы модель без счёта.
                        log.warning("AudioSocket: разговоров уже %s (stream_max_sessions) — "
                                    "соединение с %s не принято", занято, откуда[0])
                        with contextlib.suppress(OSError):
                            сокет.sendall(кадр(ВИД_ОШИБКА, b"\x01"))
                        break
                    ид = str(uuid.UUID(bytes=данные)) if len(данные) == 16 else данные.hex()
                    настройки = self.settings.merged({})
                    модель = str(self.settings.get("audiosocket_model") or "").strip()
                    if модель:
                        настройки["model"] = модель
                    сессия = StreamSession(
                        self.registry, настройки, source_format="pcm_s16le",
                        workdir=Path(self.settings.get("temp_dir") or self.settings.paths.tmp))
                    готово = сессия.start()
                    запись = {"since": time.time(), "peer": str(откуда[0]), "final": []}
                    with self._замок:
                        self._идут[ид] = запись
                    self._событие(ид, готово.to_dict())
                elif вид == ВИД_ЗВУК and сессия is not None:
                    for событие in сессия.feed(в_16к(данные)):
                        self._разослать(ид, событие, запись)
                elif вид == ВИД_КОНЕЦ:
                    break
                elif вид == ВИД_ОШИБКА:
                    log.warning("AudioSocket %s: станция сообщила об ошибке %s", ид or "?",
                                данные.hex())
                    break
        except Exception as exc:                            # noqa: BLE001
            # Отказ распознавания (нет модели, сбой движка) — строкой в журнал
            # сервера, а не трассировкой socketserver в stderr.
            log.warning("AudioSocket %s: разговор прерван: %s", ид or "?", exc)
        finally:
            if сессия is not None:
                try:
                    for событие in сессия.finish():
                        self._разослать(ид, событие, запись)
                except Exception as exc:                    # noqa: BLE001
                    log.debug("AudioSocket %s: сессия не завершилась штатно: %s", ид, exc)
                finally:
                    сессия.close()
                self._итог(ид, запись)
            with self._замок:
                self._идут.pop(ид, None)
            try:
                сокет.close()
            except OSError:
                pass

    def _разослать(self, ид: str, событие: Any, запись: dict[str, Any]) -> None:
        данные = событие.to_dict()
        if данные.get("type") == "final" and данные.get("text"):
            запись.setdefault("final", []).append(str(данные["text"]))
        if данные.get("type") in ("partial", "final", "error"):
            self._событие(ид, данные)

    def _итог(self, ид: str, запись: dict[str, Any]) -> None:
        текст = " ".join(запись.get("final") or [])
        длилось = time.time() - float(запись.get("since") or time.time())
        self._событие(ид, {"type": "done", "text": текст, "duration_s": round(длилось, 1)})
        if self.db is not None:
            try:
                self.db.add_event(None, "audiosocket",
                                  f"Живой разговор {ид}: {len(запись.get('final') or [])} фраз",
                                  {"uuid": ид, "text": текст[:20000],
                                   "duration_s": round(длилось, 1)})
            except Exception as exc:                        # noqa: BLE001
                log.debug("Итог живого разговора не записан: %s", exc)
