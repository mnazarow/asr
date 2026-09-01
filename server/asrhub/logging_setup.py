"""Журналирование ASR Hub: файлы с ротацией, консоль и кольцевой буфер для интерфейса."""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

_RING: deque[dict[str, Any]] = deque(maxlen=2000)
_RING_LOCK = threading.Lock()

_LEVEL_COLORS = {
    "DEBUG": "\033[90m", "INFO": "\033[36m", "WARNING": "\033[33m",
    "ERROR": "\033[31m", "CRITICAL": "\033[1;31m",
}
_RESET = "\033[0m"

# Ключи, значения которых никогда не попадают в журнал целиком
_SECRET_KEYS = {"api_key", "hf_token", "authorization", "x-api-key", "token", "password"}


def _mask(value: Any) -> Any:
    text = str(value)
    if len(text) <= 8:
        return "***"
    return f"{text[:4]}…{text[-2:]}"


class RingHandler(logging.Handler):
    """Держит последние записи в памяти — интерфейс показывает их без чтения файла."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                "ts": record.created,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            for key in ("job_id", "model", "engine", "duration", "error_code"):
                val = getattr(record, key, None)
                if val is not None:
                    entry[key] = val
            if record.exc_info:
                entry["exception"] = self.format(record).split("\n", 1)[-1][:4000]
            with _RING_LOCK:
                _RING.append(entry)
        except Exception:  # журнал не должен ронять приложение
            pass


class JsonFormatter(logging.Formatter):
    """Строчный JSON — удобно для систем сбора логов."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
                  + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in ("args", "msg", "levelname", "levelno", "pathname", "filename",
                       "module", "exc_info", "exc_text", "stack_info", "lineno",
                       "funcName", "created", "msecs", "relativeCreated", "thread",
                       "threadName", "processName", "process", "name", "taskName"):
                continue
            payload[key] = _mask(value) if key.lower() in _SECRET_KEYS else value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[:8000]
        return json.dumps(payload, ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Читаемый формат для консоли, с цветом если это терминал."""

    def __init__(self, color: bool = True):
        super().__init__()
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = record.levelname
        prefix = f"{stamp} {level:<8}"
        if self.color:
            prefix = f"{stamp} {_LEVEL_COLORS.get(level, '')}{level:<8}{_RESET}"
        extras = []
        for key in ("job_id", "model", "engine"):
            val = getattr(record, key, None)
            if val:
                extras.append(f"{key}={val}")
        tail = ("  [" + " ".join(extras) + "]") if extras else ""
        line = f"{prefix} {record.name:<22} {record.getMessage()}{tail}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup(level: str = "INFO", log_dir: Path | None = None,
          *, json_logs: bool = False, quiet: bool = False) -> logging.Logger:
    """Настраивает журналирование. Безопасно вызывать повторно."""
    root = logging.getLogger("asrhub")
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    root.handlers.clear()
    root.propagate = False

    if not quiet:
        stream = logging.StreamHandler(sys.stderr)
        is_tty = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        stream.setFormatter(JsonFormatter() if json_logs
                            else HumanFormatter(color=is_tty and os.environ.get("NO_COLOR") is None))
        root.addHandler(stream)

    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_dir / "asrhub.log", maxBytes=32 * 1024 * 1024,
                backupCount=5, encoding="utf-8")
            handler.setFormatter(JsonFormatter())
            root.addHandler(handler)

            errors = logging.handlers.RotatingFileHandler(
                log_dir / "errors.log", maxBytes=16 * 1024 * 1024,
                backupCount=3, encoding="utf-8")
            errors.setLevel(logging.WARNING)
            errors.setFormatter(JsonFormatter())
            root.addHandler(errors)
        except OSError as exc:
            root.warning("Не удалось открыть файл журнала в %s: %s", log_dir, exc)

    root.addHandler(RingHandler())

    for noisy in ("urllib3", "httpx", "asyncio", "filelock", "huggingface_hub", "numba"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"asrhub.{name}")


def recent(limit: int = 200, level: str = "", search: str = "",
           job_id: str = "") -> list[dict[str, Any]]:
    """Последние записи журнала с фильтрацией — используется интерфейсом."""
    with _RING_LOCK:
        items = list(_RING)
    if level:
        wanted = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
        threshold = wanted.get(level.upper(), 0)
        items = [i for i in items
                 if wanted.get(str(i.get("level")), 0) >= threshold]
    if search:
        needle = search.lower()
        items = [i for i in items if needle in json.dumps(i, ensure_ascii=False).lower()]
    if job_id:
        items = [i for i in items if i.get("job_id") == job_id]
    return items[-limit:]


def counts() -> dict[str, int]:
    with _RING_LOCK:
        items = list(_RING)
    out: dict[str, int] = {}
    for item in items:
        lvl = str(item.get("level", "INFO"))
        out[lvl] = out.get(lvl, 0) + 1
    return out
