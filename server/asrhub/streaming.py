"""Потоковое распознавание: звук приходит кусками, текст возвращается по ходу.

Раньше файл принимался целиком: чтобы получить первое слово, надо было
дождаться конца записи. Модели GigaAM v3, T-one и Vosk умеют иначе, но
наружу это не выходило — маршрута не было.

Два способа работы, и разница между ними честная:

* **Настоящий поток.** Движок держит состояние между кусками и после
  каждого отдаёт уточнённую гипотезу. Так работает Vosk. Задержка —
  доли секунды, звук не распознаётся дважды.
* **Скользящее окно.** Движок такого состояния не держит, поэтому
  накопленный звук распознаётся заново каждые несколько секунд. Текст
  тоже появляется по ходу, но за него платят повторной работой, поэтому
  окно берётся покрупнее.

Что именно происходит, сессия сообщает первым же сообщением — гадать не
приходится.
"""
from __future__ import annotations

import math
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ASRHubError, AudioError, ConfigError
from .logging_setup import get_logger

log = get_logger("streaming")

#: Частота, в которой работают все движки. Всё, что приходит в другом виде,
#: приводится к ней через ffmpeg.
SAMPLE_RATE = 16000

#: Предел на длину одной сессии. Поток без конца — это утечка: буфер растёт,
#: модель занята, а задание в очереди так и не появляется.
MAX_SESSION_S = 3600.0

#: Сколько накопленного звука ждать перед следующей гипотезой в режиме
#: скользящего окна. Меньше — чаще обновления и больше повторной работы.
DEFAULT_WINDOW_S = 4.0


@dataclass(slots=True)
class StreamEvent:
    """Одно сообщение клиенту."""

    type: str                       # ready | partial | final | error | done
    text: str = ""
    start: float = 0.0
    end: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type}
        if self.text or self.type in ("partial", "final"):
            data["text"] = self.text
            data["start"] = round(self.start, 3)
            data["end"] = round(self.end, 3)
        data.update(self.extra)
        return data


class _Decoder:
    """Приводит входящий звук к моно 16 кГц, 16 бит.

    Клиенту не нужно уметь кодировать PCM самому: браузер отдаёт WebM/Opus
    из MediaRecorder, телефония — µ-law, а скрипт на Python — уже готовый
    PCM. Всё, кроме PCM, идёт через ffmpeg одним долгоживущим процессом:
    запускать его на каждый кусок значило бы терять на этом больше, чем на
    самом распознавании.
    """

    def __init__(self, source_format: str) -> None:
        self.format = source_format
        self._process: subprocess.Popen[bytes] | None = None
        if source_format not in ("pcm_s16le", "auto"):
            raise ConfigError(
                f"Неизвестный формат потока «{source_format}».",
                hint="Допустимо: pcm_s16le — сырой звук моно 16 кГц 16 бит; "
                     "auto — любой контейнер, который понимает ffmpeg "
                     "(WebM/Opus из браузера, Ogg, MP3).")
        if source_format == "auto" and not shutil.which("ffmpeg"):
            raise AudioError(
                "Для формата «auto» нужен ffmpeg, а он не найден.",
                hint="Установите ffmpeg или присылайте сырой звук: "
                     "format=pcm_s16le, моно 16 кГц, 16 бит.")

    def feed(self, chunk: bytes) -> bytes:
        if self.format == "pcm_s16le":
            return chunk
        if self._process is None:
            self._process = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-i", "pipe:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
                 "-f", "s16le", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL)
        assert self._process.stdin is not None
        try:
            self._process.stdin.write(chunk)
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise AudioError("Поток ffmpeg оборвался.",
                             hint="Проверьте, что присылаете один непрерывный "
                                  "контейнер, а не отдельные файлы.") from exc
        return self._drain()

    def _drain(self, final: bool = False) -> bytes:
        """Забирает готовый PCM, не блокируя приём следующего куска."""
        if self._process is None or self._process.stdout is None:
            return b""
        import os
        import select

        out = self._process.stdout
        os.set_blocking(out.fileno(), False)
        data = b""
        deadline = time.time() + (2.0 if final else 0.05)
        while time.time() < deadline:
            ready, _, _ = select.select([out], [], [], 0.02)
            if not ready:
                if not final:
                    break
                if self._process.poll() is not None:
                    break
                continue
            piece = out.read(1 << 16)
            if piece:
                data += piece
            elif final and self._process.poll() is not None:
                break
        return data

    def close(self) -> bytes:
        if self._process is None:
            return b""
        try:
            if self._process.stdin:
                self._process.stdin.close()
        except OSError:
            pass
        tail = self._drain(final=True)
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None
        return tail


class StreamSession:
    """Одна сессия распознавания «на лету».

    Работает поверх обычного реестра движков: модель берётся оттуда же,
    что и для файловых заданий, и остаётся в кеше после сессии.
    """

    def __init__(self, registry: Any, settings: dict[str, Any], *,
                 source_format: str = "pcm_s16le", workdir: Path | None = None) -> None:
        self.settings = dict(settings)
        self.registry = registry
        self.decoder = _Decoder(source_format)
        self.workdir = workdir or Path(settings.get("temp_dir") or "/tmp")
        self.workdir.mkdir(parents=True, exist_ok=True)

        self._pcm = bytearray()          # весь звук сессии
        self._since_flush = 0            # байт с прошлой гипотезы
        self._started = time.time()
        self._closed = False
        self._native: Any = None         # состояние движка, если он умеет поток
        self._final_text = ""
        self._last_partial = ""

        self.window_s = max(1.0, float(settings.get("stream_window_s") or DEFAULT_WINDOW_S))

    # -- жизненный цикл -----------------------------------------------------

    def start(self) -> StreamEvent:
        """Готовит движок и сообщает, каким способом будет работать."""
        # Движок берём из общего реестра, но не держим его занятым всю
        # сессию: на каждое распознавание берётся короткая аренда, иначе
        # один разговор блокировал бы очередь файловых заданий целиком.
        engine = self.registry.get(self.settings)
        self._native = None
        maker = getattr(engine, "stream_session", None)
        if callable(maker):
            try:
                self._native = maker(self.settings)
            except ASRHubError as exc:
                log.info("Настоящий поток недоступен (%s) — идём окном", exc.message)
            except Exception as exc:                    # noqa: BLE001
                log.info("Настоящий поток недоступен (%s) — идём окном", exc)
        mode = "native" if self._native is not None else "window"
        return StreamEvent("ready", extra={
            "mode": mode,
            "model": str(self.settings.get("model") or ""),
            "sample_rate": SAMPLE_RATE,
            "window_s": None if mode == "native" else self.window_s,
            "note": ("Движок держит состояние между кусками: гипотеза уточняется "
                     "после каждого." if mode == "native" else
                     f"Движок потока не держит, поэтому накопленный звук "
                     f"распознаётся заново каждые {self.window_s:g} с."),
        })

    def feed(self, chunk: bytes) -> list[StreamEvent]:
        """Принимает кусок звука и, если пора, отдаёт уточнённую гипотезу."""
        if self._closed:
            return [StreamEvent("error", extra={"message": "Сессия уже завершена."})]
        if time.time() - self._started > MAX_SESSION_S:
            self._closed = True
            return [StreamEvent("error", extra={
                "message": f"Сессия длиннее {MAX_SESSION_S / 60:.0f} минут прервана.",
                "hint": "Для длинных записей ставьте обычное задание: POST /api/jobs."})]

        pcm = self.decoder.feed(chunk)
        if not pcm:
            return []
        self._pcm.extend(pcm)
        self._since_flush += len(pcm)

        if self._native is not None:
            return self._feed_native(pcm)

        need = int(self.window_s * SAMPLE_RATE * 2)
        if self._since_flush < need:
            return []
        self._since_flush = 0
        text = self._recognize(bytes(self._pcm))
        if text and text != self._last_partial:
            self._last_partial = text
            return [StreamEvent("partial", text=text, start=0.0, end=self.duration_s)]
        return []

    def finish(self) -> list[StreamEvent]:
        """Завершает сессию и отдаёт окончательный текст."""
        if self._closed:
            return []
        self._closed = True
        tail = self.decoder.close()
        if tail:
            self._pcm.extend(tail)

        events: list[StreamEvent] = []
        if self._native is not None:
            events.extend(self._finish_native())
        else:
            text = self._recognize(bytes(self._pcm))
            self._final_text = text
            events.append(StreamEvent("final", text=text, start=0.0, end=self.duration_s))
        events.append(StreamEvent("done", extra={
            "duration_s": round(self.duration_s, 3),
            "text": self._final_text,
        }))
        return events

    def close(self) -> None:
        """Освобождает всё, что держит сессия, — даже если клиент оборвался."""
        self._closed = True
        try:
            self.decoder.close()
        except Exception:                               # noqa: BLE001
            pass
        native = self._native
        self._native = None
        if native is not None and hasattr(native, "close"):
            try:
                native.close()
            except Exception:                           # noqa: BLE001
                pass


    # -- внутреннее ---------------------------------------------------------

    @property
    def duration_s(self) -> float:
        return len(self._pcm) / (SAMPLE_RATE * 2)

    def _feed_native(self, pcm: bytes) -> list[StreamEvent]:
        try:
            result = self._native.accept(pcm)
        except Exception as exc:                        # noqa: BLE001
            log.warning("Поток движка дал сбой (%s) — переходим на окно", exc)
            self._native = None
            return []
        if not result:
            return []
        kind, text = result
        if not text or (kind == "partial" and text == self._last_partial):
            return []
        if kind == "final":
            self._final_text = (self._final_text + " " + text).strip()
            self._last_partial = ""
            return [StreamEvent("final", text=text, start=0.0, end=self.duration_s)]
        self._last_partial = text
        return [StreamEvent("partial", text=text, start=0.0, end=self.duration_s)]

    def _finish_native(self) -> list[StreamEvent]:
        try:
            text = self._native.finish()
        except Exception as exc:                        # noqa: BLE001
            log.warning("Завершение потока движка дало сбой: %s", exc)
            text = ""
        if text:
            self._final_text = (self._final_text + " " + text).strip()
        return [StreamEvent("final", text=self._final_text, start=0.0, end=self.duration_s)]

    def _recognize(self, pcm: bytes) -> str:
        """Распознаёт накопленный звук целиком — путь скользящего окна."""
        if len(pcm) < SAMPLE_RATE:          # меньше полусекунды — не о чем говорить
            return ""
        path = self.workdir / f"stream-{id(self)}.wav"
        try:
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(SAMPLE_RATE)
                handle.writeframes(pcm)
            with self.registry.lease(self.settings) as engine:
                result = engine.transcribe(path, self.settings, None)
        except ASRHubError as exc:
            log.info("Распознавание куска не удалось: %s", exc.message)
            return ""
        except Exception as exc:                        # noqa: BLE001
            log.warning("Распознавание куска не удалось: %s", exc)
            return ""
        finally:
            path.unlink(missing_ok=True)
        return " ".join(s.text.strip() for s in result.segments if s.text).strip()


def pcm_from_float(samples: Any) -> bytes:
    """Отсчёты в диапазоне [-1, 1] -> PCM 16 бит. Для тестов и примеров."""
    import struct

    return b"".join(struct.pack("<h", int(max(-1.0, min(1.0, float(v))) * 32767))
                    for v in samples)


def tone(seconds: float, freq: float = 320.0, rate: int = SAMPLE_RATE) -> bytes:
    """Ровный тон — чем-то надо наполнять поток в примерах и проверках."""
    return pcm_from_float(
        0.4 * math.sin(2 * math.pi * freq * i / rate) for i in range(int(seconds * rate)))
