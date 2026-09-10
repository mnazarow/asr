"""Вызовы языковой модели: Ollama, OpenAI-совместимый сервер, заглушка.

Клиент делает ровно одно: отправляет подсказку и возвращает текст ответа.
Всё, что делает это безопасным для основной очереди, — здесь же:

* **ограничение одновременности** — семафор на `llm_max_concurrent`;
  на одной видеокарте с распознаванием второй вызов не ускоряет, а делит
  память;
* **тайм-аут** — `llm_timeout_s` на вызов; зависший сервер модели не
  вешает фоновый поток навсегда;
* **кеш по отпечатку** — sha256 от модели, вида задачи и подсказки: та же
  запись с теми же подсказками второй раз модель не спрашивает;
* **проверка видеопамяти** — при `llm_min_free_vram_gb` больше нуля вызов
  не делается, пока свободной памяти меньше порога;
* **учёт** — число вызовов, ошибок, попаданий в кеш, время последнего
  вызова и последняя ошибка — для состояния слоя и метрик.

Сеть — только стандартная библиотека: слой не должен тянуть зависимости
в базовую установку.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from ..logging_setup import get_logger

log = get_logger("llm")

#: Сколько держать модель в памяти Ollama после вызова: следующий вызов
#: через минуту не должен поднимать веса заново.
KEEP_ALIVE = "30m"

#: Наибольшая длина ответа в токенах: JSON с резюме и списком действий
#: укладывается в тысячу с запасом; без предела модель может «дописывать».
MAX_TOKENS = 1200

#: Сколько живёт результат пробы доступности сервера модели.
СРОК_ПРОБЫ = 60.0


class LLMError(Exception):
    """Сбой вызова модели: сеть, тайм-аут, чужой ответ, нет памяти."""


class LLMClient:
    def __init__(self, settings: Any, db: Any = None, *, hardware: Any = None):
        self.settings = settings
        self.db = db
        self._hardware = hardware
        self._semaphore = threading.BoundedSemaphore(
            max(1, int(settings.get("llm_max_concurrent") or 1)))
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        self.total_ms = 0.0
        self.last_ms: float | None = None
        self.last_error: str | None = None
        self.last_call_at: float | None = None
        self._probe: tuple[float, dict[str, Any]] | None = None

    # --- настройки ------------------------------------------------------

    @property
    def backend(self) -> str:
        return str(self.settings.get("llm_backend") or "off")

    @property
    def enabled(self) -> bool:
        return self.backend in ("ollama", "openai", "stub")

    @property
    def model(self) -> str:
        return str(self.settings.get("llm_model") or "")

    @property
    def url(self) -> str:
        return str(self.settings.get("llm_url") or "http://127.0.0.1:11434").rstrip("/")

    @property
    def timeout(self) -> float:
        return float(self.settings.get("llm_timeout_s") or 90)

    # --- состояние ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Состояние слоя: как подключено, доступен ли сервер, учёт вызовов."""
        out: dict[str, Any] = {
            "backend": self.backend, "enabled": self.enabled, "model": self.model,
            "url": self.url if self.backend in ("ollama", "openai") else None,
            "calls": self.calls, "errors": self.errors, "cache_hits": self.cache_hits,
            "avg_ms": round(self.total_ms / self.calls, 1) if self.calls else None,
            "last_ms": self.last_ms, "last_error": self.last_error,
            "last_call_at": self.last_call_at,
            "tasks": list(self.settings.get("llm_tasks") or []),
        }
        out.update(self.probe())
        return out

    def probe(self, *, fresh: bool = False) -> dict[str, Any]:
        """Доступен ли сервер модели и знает ли он модель — с кешем на минуту."""
        if not self.enabled:
            return {"available": False, "reason": "выключено"}
        if self.backend == "stub":
            return {"available": True, "reason": "заглушка отвечает без модели"}
        with self._lock:
            if not fresh and self._probe and time.time() - self._probe[0] < СРОК_ПРОБЫ:
                return self._probe[1]
        итог = self._probe_now()
        with self._lock:
            self._probe = (time.time(), итог)
        return итог

    def _probe_now(self) -> dict[str, Any]:
        try:
            if self.backend == "ollama":
                данные = self._http("GET", f"{self.url}/api/tags", None, timeout=5.0)
                имена = [str(м.get("name") or "") for м in (данные.get("models") or [])]
                известна = any(и == self.model or и.split(":")[0] == self.model.split(":")[0]
                               for и in имена)
                return {"available": True, "models": имена[:50], "model_known": известна,
                        "reason": None if известна else
                        f"модель «{self.model}» не скачана: ollama pull {self.model}"}
            данные = self._http("GET", f"{self.url}/v1/models", None, timeout=5.0)
            имена = [str(м.get("id") or "") for м in (данные.get("data") or [])]
            известна = not имена or self.model in имена
            return {"available": True, "models": имена[:50], "model_known": известна,
                    "reason": None if известна else f"сервер не знает модель «{self.model}»"}
        except LLMError as exc:
            return {"available": False, "reason": str(exc)}

    # --- вызов ----------------------------------------------------------

    def chat(self, system: str, user: str, *, kind: str = "chat",
             json_mode: bool = True, max_tokens: int = MAX_TOKENS,
             use_cache: bool = True) -> str:
        """Один вызов модели; возвращает текст ответа.

        Порядок: кеш → проверка памяти → семафор → сеть. Ошибка любого
        рода — `LLMError` с человеческим текстом; учёт ведётся всегда.
        """
        if not self.enabled:
            raise LLMError("Языковая модель выключена (llm_backend = off).")
        ключ = self.cache_key(kind, system, user)
        if use_cache and self.db is not None:
            готовое = self.db.llm_cache_get(ключ)
            if готовое is not None:
                with self._lock:
                    self.cache_hits += 1
                return готовое
        self._check_vram()
        начало = time.perf_counter()
        with self._semaphore:
            try:
                if self.backend == "stub":
                    from . import stub  # noqa: PLC0415

                    ответ = stub.reply(system, user)
                elif self.backend == "ollama":
                    ответ = self._ollama(system, user, json_mode, max_tokens)
                else:
                    ответ = self._openai(system, user, json_mode, max_tokens)
            except LLMError as exc:
                with self._lock:
                    self.calls += 1
                    self.errors += 1
                    self.last_error = str(exc)
                    self.last_call_at = time.time()
                raise
        прошло = (time.perf_counter() - начало) * 1000
        with self._lock:
            self.calls += 1
            self.total_ms += прошло
            self.last_ms = round(прошло, 1)
            self.last_error = None
            self.last_call_at = time.time()
        if use_cache and self.db is not None:
            try:
                self.db.llm_cache_put(ключ, kind, self.model, ответ, round(прошло, 1))
            except Exception as exc:                         # noqa: BLE001
                log.debug("Кеш ответа модели не записан: %s", exc)
        return ответ

    def cache_key(self, kind: str, system: str, user: str) -> str:
        отпечаток = hashlib.sha256()
        for часть in (self.backend, self.model, kind, system, user):
            отпечаток.update(часть.encode("utf-8"))
            отпечаток.update(b"\x00")
        return отпечаток.hexdigest()

    def _check_vram(self) -> None:
        порог = float(self.settings.get("llm_min_free_vram_gb") or 0)
        if порог <= 0 or self.backend == "stub":
            return
        свободно = self._free_vram_gb()
        if свободно is not None and свободно < порог:
            raise LLMError(f"Свободной видеопамяти {свободно:.1f} ГБ — меньше порога "
                           f"{порог:g} ГБ; вызов отложен.")

    def _free_vram_gb(self) -> float | None:
        """Свободная память первой видеокарты — по nvidia-smi, если он есть."""
        if self._hardware is not None:
            try:
                return float(self._hardware())
            except Exception:                                # noqa: BLE001
                return None
        try:
            import shutil  # noqa: PLC0415
            import subprocess  # noqa: PLC0415

            exe = shutil.which("nvidia-smi")
            if not exe:
                return None
            out = subprocess.run(  # noqa: S603
                [exe, "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5, check=False).stdout
            первая = out.strip().splitlines()[0] if out.strip() else ""
            return float(первая) / 1024 if первая else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    # --- бэкенды --------------------------------------------------------

    def _ollama(self, system: str, user: str, json_mode: bool, max_tokens: int) -> str:
        тело: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "keep_alive": KEEP_ALIVE,
            "options": {"temperature": 0, "num_predict": max_tokens},
        }
        if json_mode:
            тело["format"] = "json"
        данные = self._http("POST", f"{self.url}/api/chat", тело, timeout=self.timeout)
        сообщение = данные.get("message") or {}
        текст = str(сообщение.get("content") or "")
        if not текст:
            raise LLMError("Ollama вернул пустой ответ.")
        return текст

    def _openai(self, system: str, user: str, json_mode: bool, max_tokens: int) -> str:
        тело: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            тело["response_format"] = {"type": "json_object"}
        данные = self._http("POST", f"{self.url}/v1/chat/completions", тело,
                            timeout=self.timeout)
        выборы = данные.get("choices") or []
        текст = str(((выборы[0] if выборы else {}).get("message") or {}).get("content") or "")
        if not текст:
            raise LLMError("Сервер модели вернул пустой ответ.")
        return текст

    def _http(self, method: str, url: str, body: dict[str, Any] | None, *,
              timeout: float) -> dict[str, Any]:
        заголовки = {"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json", "User-Agent": "ASR Hub"}
        ключ = str(self.settings.get("llm_api_key") or "")
        if ключ:
            заголовки["Authorization"] = f"Bearer {ключ}"
        данные = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        запрос = urllib.request.Request(url, data=данные, method=method, headers=заголовки)  # noqa: S310
        try:
            with urllib.request.urlopen(запрос, timeout=timeout) as ответ:  # noqa: S310
                сырое = ответ.read()
        except urllib.error.HTTPError as exc:
            кусок = ""
            try:
                кусок = exc.read().decode("utf-8", "replace")[:300]
            except Exception:                                # noqa: BLE001
                pass
            raise LLMError(f"Сервер модели ответил {exc.code}: {кусок or exc.reason}") from exc
        except urllib.error.URLError as exc:
            причина = getattr(exc, "reason", exc)
            if isinstance(причина, TimeoutError) or "timed out" in str(причина):
                raise LLMError(f"Сервер модели не ответил за {timeout:g} с.") from exc
            raise LLMError(f"Сервер модели недоступен по адресу {self.url}: {причина}") from exc
        except TimeoutError as exc:
            raise LLMError(f"Сервер модели не ответил за {timeout:g} с.") from exc
        except OSError as exc:
            raise LLMError(f"Сервер модели недоступен: {exc}") from exc
        try:
            разобрано = json.loads(сырое.decode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise LLMError("Сервер модели ответил не JSON.") from exc
        if not isinstance(разобрано, dict):
            raise LLMError("Сервер модели ответил не объектом JSON.")
        return разобрано
