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
import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import settings_access as S
from ..logging_setup import get_logger

log = get_logger("llm")

#: Сколько держать модель в памяти Ollama после вызова: следующий вызов
#: через минуту не должен поднимать веса заново.
KEEP_ALIVE = "30m"

#: Наибольшая длина ответа в токенах: JSON с резюме и списком действий
#: укладывается в тысячу с запасом; без предела модель может «дописывать».
#: Это предел САМОГО ответа — рассуждение, если оно включено, получает свой
#: бюджет сверху (см. БЮДЖЕТ_РАССУЖДЕНИЯ).
MAX_TOKENS = 1200

#: Сколько живёт результат пробы доступности сервера модели.
СРОК_ПРОБЫ = 60.0

#: Сколько токенов прибавить к пределу ответа, когда модель рассуждает.
#:
#: Это и была причина «Ollama вернул пустой ответ» в очереди. У Ollama
#: рассуждение идёт в тот же счёт `num_predict`, что и ответ: модель с
#: пределом в 1200 токенов, подумавшая на 1300, не успевает сказать ни
#: слова, и сервер честно возвращает пустой `content` — а всё сказанное
#: лежит в поле `thinking`, которое клиент не читал. Рекомендуемые модели
#: (Qwen 3.x, gpt-oss) рассуждают по умолчанию, так что с ними это
#: случалось не изредка, а почти на каждой записи.
БЮДЖЕТ_РАССУЖДЕНИЯ: dict[str, int] = {"off": 0, "low": 2048, "medium": 4096,
                                       "high": 8192, "model": 4096}

#: Модели, у которых рассуждение нельзя выключить — только ослабить. gpt-oss
#: принимает в поле think лишь low/medium/high; «false» для неё не значит
#: «не рассуждай», и просить надо «low».
ВСЕГДА_РАССУЖДАЮТ = ("gpt-oss",)

#: Сколько знаков русского текста приходится на токен — с запасом в худшую
#: сторону. Кириллица режется токенизаторами мельче латиницы: у Qwen около
#: трёх знаков на токен, у Gemma и gpt-oss ближе к четырём. С 2,5 подсказка
#: в посчитанное окно помещается гарантированно.
ЗНАКОВ_НА_ТОКЕН = 2.5

#: Сколько знаков сверх куска расшифровки занимает подсказка: системная
#: часть, перечень полей, списки причин и исходов.
ЗАПАС_ПОДСКАЗКИ = 3000

#: Окно контекста: не меньше, не больше и каким шагом.
#:
#: Нижняя граница — не для куска расшифровки: под кусок окно считается
#: точно. Она для основного вызова длинного разговора, который идёт не по
#: куску, а по склейке пересказов всех кусков, — и склейка часового
#: разговора выходит длиннее маленького предела текста.
ОКНО_НАИМЕНЬШЕЕ = 8192
ОКНО_НАИБОЛЬШЕЕ = 131072
ШАГ_ОКНА = 4096


def уровень_рассуждения(settings: Any) -> str:
    """Глубина рассуждения из настроек: off, low, medium, high или model."""
    значение = str((settings.get("llm_think") if settings else "") or "off")
    значение = значение.strip().lower()
    return значение if значение in БЮДЖЕТ_РАССУЖДЕНИЯ else "off"


def всегда_рассуждает(модель: str) -> bool:
    """Из тех ли модель, что рассуждают всегда и выключить это нельзя."""
    имя = str(модель or "").strip().lower()
    return any(имя.startswith(семья) for семья in ВСЕГДА_РАССУЖДАЮТ)


def поле_think(модель: str, уровень: str) -> bool | str | None:
    """Что послать в поле think запроса Ollama; None — не посылать вовсе.

    «model» — значит, решает сама модель, и поле не посылается. Для
    остальных уровней у обычных моделей это да/нет, а у тех, что рассуждают
    всегда, — сам уровень, и «выключено» у них превращается в «low».
    """
    if уровень == "model":
        return None
    if всегда_рассуждает(модель):
        return "low" if уровень == "off" else уровень
    return уровень != "off"


def бюджет_рассуждения(think: bool | str | None, уровень: str) -> int:
    """Сколько токенов под рассуждение прибавить к пределу ответа."""
    if think is False:
        return 0
    if isinstance(think, str):
        return БЮДЖЕТ_РАССУЖДЕНИЯ.get(think, БЮДЖЕТ_РАССУЖДЕНИЯ["medium"])
    if think is True:
        return БЮДЖЕТ_РАССУЖДЕНИЯ.get(уровень) or БЮДЖЕТ_РАССУЖДЕНИЯ["medium"]
    # Поле не послано: модель решает сама, и рассуждать она вполне может.
    return БЮДЖЕТ_РАССУЖДЕНИЯ["model"]


def предел_ответа(settings: Any) -> int:
    """Предел самого ответа в токенах — без рассуждения."""
    return max(64, S.integer(settings or {}, "llm_max_tokens", MAX_TOKENS))


def окно_контекста(settings: Any, модель: str = "") -> int:
    """Окно контекста для Ollama: заданное или посчитанное по настройкам.

    Посчитанное зависит только от настроек и модели — не от длины очередной
    подсказки. Это важно: Ollama держит загруженную модель с тем окном, с
    каким её подняли, и запрос с другим `num_ctx` заставляет выгрузить и
    загрузить веса заново — десятки секунд на каждом вызове. Окно, которое
    прыгает от записи к записи, превратило бы разбор в сплошную перезагрузку.

    Без явного окна Ollama берёт своё по умолчанию, и оно бывает меньше
    куска расшифровки: двенадцать тысяч знаков — это до пяти тысяч токенов
    одного только текста. Лишнее Ollama отрезает молча, с начала подсказки —
    вместе с указаниями, что делать.
    """
    задано = S.integer(settings or {}, "llm_num_ctx", 0)
    if задано > 0:
        return задано
    знаков = S.integer(settings or {}, "llm_context_chars", 12000)
    уровень = уровень_рассуждения(settings)
    рассуждение = бюджет_рассуждения(поле_think(модель, уровень), уровень)
    нужно = ((знаков + ЗАПАС_ПОДСКАЗКИ) / ЗНАКОВ_НА_ТОКЕН
             + предел_ответа(settings) + рассуждение)
    окно = int(math.ceil(нужно / ШАГ_ОКНА)) * ШАГ_ОКНА
    return max(ОКНО_НАИМЕНЬШЕЕ, min(ОКНО_НАИБОЛЬШЕЕ, окно))


def без_рассуждения(текст: str) -> tuple[str, str]:
    """Ответ без рассуждения, вписанного прямо в текст, и само рассуждение.

    Старые версии Ollama и часть шаблонов отдают рассуждение не отдельным
    полем, а в самом ответе: «<think>…</think>ответ». Бывает и хвост без
    начала — когда «<think>» подставлен шаблоном в подсказку, — и начало без
    хвоста, когда рассуждение оборвалось на пределе. Все три случая здесь.
    """
    мысли = ""
    конец = текст.lower().rfind("</think>")
    if конец >= 0:
        мысли = текст[:конец]
        текст = текст[конец + len("</think>"):]
    начало = текст.lower().find("<think>")
    if начало >= 0:
        мысли += текст[начало:]
        текст = текст[:начало]
    return текст, мысли


@dataclass(frozen=True)
class ОтветOllama:
    """Что вернул Ollama: ответ, рассуждение и счётчики — для диагноза."""

    текст: str
    мысли: str = ""
    причина: str = ""
    сказано: int = 0
    подсказка: int = 0

    @property
    def почему_пусто(self) -> str:
        """Почему ответ пустой: рассуждение, предел, сразу или пусто."""
        if self.текст:
            return ""
        if self.мысли.strip():
            return "рассуждение"
        if self.причина == "length":
            return "предел"
        if self.сказано <= 1:
            return "сразу"
        return "пусто"


def разобрать_ollama(данные: dict[str, Any]) -> ОтветOllama:
    """Разбирает ответ /api/chat: текст отдельно, рассуждение отдельно."""
    сообщение = данные.get("message") or {}
    текст, внутри = без_рассуждения(str(сообщение.get("content") or ""))
    мысли = str(сообщение.get("thinking") or "") or внутри
    return ОтветOllama(
        текст=текст.strip(), мысли=мысли,
        причина=str(данные.get("done_reason") or ""),
        сказано=int(данные.get("eval_count") or 0),
        подсказка=int(данные.get("prompt_eval_count") or 0))


def тело_ollama(модель: str, system: str, user: str, *,
                think: bool | str | None, num_predict: int, num_ctx: int,
                json_mode: bool) -> dict[str, Any]:
    """Тело запроса /api/chat. Одно на разбор, прогрев и шлюз."""
    тело: dict[str, Any] = {
        "model": модель,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": 0, "num_predict": int(num_predict),
                    "num_ctx": int(num_ctx)},
    }
    if think is not None:
        тело["think"] = think
    if json_mode:
        тело["format"] = "json"
    return тело


def про_think(exc: Exception) -> bool:
    """Отказ Ollama из-за поля think: модель рассуждать не умеет."""
    тело = str(getattr(exc, "body", "") or exc).lower()
    return getattr(exc, "status", None) == 400 and "think" in тело


def _годится(ответ: str, validate: Any) -> bool:
    """Разбирается ли ответ так, как его собирается читать вызывающий."""
    if validate is None:
        return True
    try:
        validate(ответ)
    except Exception:                                        # noqa: BLE001
        return False
    return True


class LLMError(Exception):
    """Сбой вызова модели: сеть, тайм-аут, чужой ответ, нет памяти.

    `status` и `body` — код и начало тела ответа сервера модели, когда он
    ответил ошибкой. Нужны тем, кто решает, стоит ли спросить иначе: отказ
    из-за поля think лечится запросом без него, а не повтором того же.
    """

    status: int | None = None
    body: str = ""
    #: Сбой не про запись, а про сервер модели: он не принимает соединение,
    #: перезапускается, за прокси отвечает 502/503/504, занят (429) или не
    #: хватает видеопамяти. Такой заход очередь не считает попыткой:
    #: минутный перезапуск Ollama раньше уводил головные записи в «ошибку»
    #: за полминуты. Тайм-аут сюда не входит намеренно: запись, на которой
    #: модель не укладывается в срок, повторялась бы вечно.
    временная: bool = False


def _временная(текст: str, *, status: int | None = None, body: str = "") -> LLMError:
    ошибка = LLMError(текст)
    ошибка.временная = True
    if status is not None:
        ошибка.status = status
        ошибка.body = body
    return ошибка


#: Ответы сервера модели, которые говорят о нём самом, а не о запросе.
_ВРЕМЕННЫЕ_КОДЫ = frozenset({429, 502, 503, 504})


def та_же_модель(установлена: str, нужна: str) -> bool:
    """Одна ли это модель в записи Ollama.

    Совпадение только полное — с точностью до тега `:latest`, который Ollama
    дописывает сама («qwen3» и «qwen3:latest» — одно). Раньше совпадением
    считалось одно семейство: задано `qwen3.5:27b`, скачана `qwen3.5:9b` —
    проба отвечала «модель есть», панель горела зелёным, а каждый вызов
    получал 404 «model not found».
    """
    def полное(имя: str) -> str:
        имя = str(имя or "").strip()
        return имя if ":" in имя.rsplit("/", 1)[-1] else f"{имя}:latest"

    return bool(нужна) and полное(установлена) == полное(нужна)


class LLMClient:
    def __init__(self, settings: Any, db: Any = None, *, hardware: Any = None):
        self.settings = settings
        self.db = db
        self._hardware = hardware
        self._предел = max(1, int(settings.get("llm_max_concurrent") or 1))
        self._semaphore = threading.BoundedSemaphore(self._предел)
        self._lock = threading.Lock()
        self.calls = 0
        self.errors = 0
        self.cache_hits = 0
        self.total_ms = 0.0
        self.last_ms: float | None = None
        self.last_error: str | None = None
        self.last_call_at: float | None = None
        self._probe: tuple[float, tuple[str, str, str], dict[str, Any]] | None = None
        #: Модели, которые отказали из-за поля think: им его больше не шлём.
        self._без_think: set[str] = set()

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

    def _семафор(self) -> threading.BoundedSemaphore:
        """Ограничитель одновременности под текущую настройку.

        `llm_max_concurrent` меняют на странице настроек, и менять её
        имеет смысл на живом сервере: одновременных вызовов на одной
        видеокарте с распознаванием больше двух не нужно, а меньше —
        бывает нужно срочно. Семафор, собранный один раз при запуске,
        делал такую правку бессмысленной до перезапуска.

        Держатели старого семафора освободят старый объект — это
        безопасно: он только считает, а считает уже никому не нужное.
        """
        предел = max(1, int(self.settings.get("llm_max_concurrent") or 1))
        with self._lock:
            if предел != self._предел:
                self._semaphore = threading.BoundedSemaphore(предел)
                self._предел = предел
            return self._semaphore

    def _подпись(self) -> tuple[str, str, str]:
        """Чем задан сервер модели: сменилось — прошлая проба не о нём."""
        return (self.backend, self.url, self.model)

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
            # Видно в разделе «Языковая модель»: с каким окном и глубиной
            # рассуждения идут вызовы. Без этого «почему модель отвечает
            # пустотой» приходилось выяснять по журналу Ollama.
            "think": уровень_рассуждения(self.settings),
            "num_ctx": (окно_контекста(self.settings, self.model)
                        if self.backend == "ollama" else None),
            "max_tokens": предел_ответа(self.settings),
        }
        out.update(self.probe())
        return out

    def probe(self, *, fresh: bool = False) -> dict[str, Any]:
        """Доступен ли сервер модели и знает ли он модель — с кешем на минуту."""
        if not self.enabled:
            return {"available": False, "reason": "выключено"}
        if self.backend == "stub":
            return {"available": True, "reason": "заглушка отвечает без модели"}
        подпись = self._подпись()
        with self._lock:
            if (not fresh and self._probe and self._probe[1] == подпись
                    and time.time() - self._probe[0] < СРОК_ПРОБЫ):
                return self._probe[2]
        итог = self._probe_now()
        with self._lock:
            self._probe = (time.time(), подпись, итог)
        return итог

    def _probe_now(self) -> dict[str, Any]:
        try:
            if self.backend == "ollama":
                данные = self._http("GET", f"{self.url}/api/tags", None, timeout=5.0)
                имена = [str(м.get("name") or "") for м in (данные.get("models") or [])]
                известна = any(та_же_модель(и, self.model) for и in имена)
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
             json_mode: bool = True, max_tokens: int | None = None,
             use_cache: bool = True, validate: Any = None) -> str:
        """Один вызов модели; возвращает текст ответа.

        Порядок: кеш → проверка памяти → семафор → сеть. Ошибка любого
        рода — `LLMError` с человеческим текстом; учёт ведётся всегда.
        """
        if not self.enabled:
            raise LLMError("Языковая модель выключена (llm_backend = off).")
        max_tokens = int(max_tokens or предел_ответа(self.settings))
        ключ = self.cache_key(kind, system, user)
        if use_cache and self.db is not None:
            готовое = self.db.llm_cache_get(ключ)
            if готовое is not None and _годится(готовое, validate):
                with self._lock:
                    self.cache_hits += 1
                return готовое
            if готовое is not None:
                # В кеше лежит ответ, который не разбирается. Так бывает:
                # рассуждающая модель на первый вызов ответила размышлением
                # без JSON. Раньше он оседал в кеше навсегда, и «Разобрать
                # заново» вечно возвращало ту же ошибку, не спрашивая
                # сервер модели. Забываем и спрашиваем заново.
                log.info("Ответ модели из кеша не разобрался — спрашиваем заново")
                self._забыть(ключ)
        self._check_vram()
        начало = time.perf_counter()
        with self._семафор():
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
        # В кеш — только то, что разбирается: иначе один сбойный ответ
        # закрывает запись от повторных попыток навсегда.
        if use_cache and self.db is not None and _годится(ответ, validate):
            try:
                self.db.llm_cache_put(ключ, kind, self.model, ответ, round(прошло, 1))
            except Exception as exc:                         # noqa: BLE001
                log.debug("Кеш ответа модели не записан: %s", exc)
        return ответ

    def _забыть(self, ключ: str) -> None:
        try:
            self.db.llm_cache_forget(ключ)
        except Exception as exc:                             # noqa: BLE001
            log.debug("Запись кеша не убрана: %s", exc)

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
            raise _временная(f"Свободной видеопамяти {свободно:.1f} ГБ — меньше порога "
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

    def _think(self, уровень: str) -> bool | str | None:
        """Поле think для текущей модели — с учётом её прошлых отказов."""
        with self._lock:
            if self.model in self._без_think:
                return None
        return поле_think(self.model, уровень)

    def _ollama(self, system: str, user: str, json_mode: bool, max_tokens: int) -> str:
        """Один вызов Ollama — и не больше одного повтора по диагнозу.

        Пустой ответ бывает четырёх видов, и лечатся они по-разному:

        * **рассуждение** — модель потратила весь предел на рассуждение и не
          успела ответить. Повтор с выключенным рассуждением и вдвое большим
          пределом: у моделей, которые рассуждают всегда, выключить нельзя, и
          помогает только запас.
        * **предел** — ответ оборвался на пределе, не начавшись. Вдвое больше.
        * **сразу** — модель закончила, не сказав ни слова. С форматом JSON это
          бывает, когда грамматика ответа спорит с шаблоном модели: повтор без
          формата, а JSON из свободного текста разбор достаёт и так.
        * **пусто** — ничего из перечисленного; повторять то же самое незачем.

        Повтор один: вызов модели — это секунды, а очередь к ней одна.
        """
        уровень = уровень_рассуждения(self.settings)
        think = self._think(уровень)
        окно = окно_контекста(self.settings, self.model)
        предел = max_tokens + бюджет_рассуждения(think, уровень)
        первый = self._ollama_раз(system, user, think=think, num_predict=предел,
                                  num_ctx=окно, json_mode=json_mode)
        if первый.текст:
            return первый.текст

        почему = первый.почему_пусто
        think2, предел2, json2 = think, предел, json_mode
        if почему == "рассуждение":
            think2 = self._think("off")
            предел2 = max(предел * 2, max_tokens + БЮДЖЕТ_РАССУЖДЕНИЯ["medium"])
        elif почему == "предел":
            предел2 = предел * 2
        elif почему == "сразу" and json_mode:
            json2 = False
        else:
            raise LLMError(self._объяснить(первый, предел, окно, think=think))
        log.info("Пустой ответ модели (%s) — спрашиваем ещё раз: think=%r, "
                 "предел %d, формат JSON: %s", почему, think2, предел2, json2)
        второй = self._ollama_раз(system, user, think=think2, num_predict=предел2,
                                  num_ctx=окно, json_mode=json2)
        if второй.текст:
            log.info("Ответ модели получен со второй попытки (первая: %s)", почему)
            return второй.текст
        raise LLMError(self._объяснить(второй, предел2, окно, think=think2,
                                       первая=почему))

    def _ollama_раз(self, system: str, user: str, *, think: bool | str | None,
                    num_predict: int, num_ctx: int, json_mode: bool) -> ОтветOllama:
        """Один запрос /api/chat. Отказ из-за think — запрос без него."""
        тело = тело_ollama(self.model, system, user, think=think,
                           num_predict=num_predict, num_ctx=num_ctx,
                           json_mode=json_mode)
        адрес = f"{self.url}/api/chat"
        try:
            данные = self._http("POST", адрес, тело, timeout=self.timeout)
        except LLMError as exc:
            if "think" not in тело or not про_think(exc):
                raise
            # Модель рассуждать не умеет, и поле think Ollama в таком случае
            # не пропускает мимо, а отвечает отказом. Запоминаем и спрашиваем
            # без него: второй раз этот отказ ловить незачем.
            with self._lock:
                self._без_think.add(self.model)
            log.info("Модель «%s» не принимает поле think — дальше без него",
                     self.model)
            тело.pop("think", None)
            данные = self._http("POST", адрес, тело, timeout=self.timeout)
        ответ = разобрать_ollama(данные)
        if not ответ.текст:
            log.warning(
                "Пустой ответ Ollama: модель %s, done_reason=%s, сказано токенов "
                "%d, подсказка %d токенов при окне %d, рассуждение %d знаков, "
                "think=%r, предел %d", self.model, ответ.причина or "—",
                ответ.сказано, ответ.подсказка, num_ctx, len(ответ.мысли),
                тело.get("think"), num_predict)
        return ответ

    def _объяснить(self, ответ: ОтветOllama, предел: int, окно: int, *,
                   think: bool | str | None, первая: str = "") -> str:
        """Текст ошибки: что случилось, с какими числами и что делать.

        Этот текст видит человек в разделе «Очередь LLM». «Ollama вернул
        пустой ответ» без подробностей отправлял его в журнал Ollama, а
        подробности были в самом ответе — их просто не читали.
        """
        модель = self.model
        повтор = " — и со второй попытки тоже" if первая else ""
        почему = ответ.почему_пусто
        if почему == "рассуждение":
            if think is False:
                return (f"Ollama вернул пустой ответ: модель «{модель}» рассуждает, "
                        f"даже когда рассуждение выключено, и потратила на него весь "
                        f"предел ({предел} токенов){повтор}. Поднимите «Предел ответа "
                        f"модели» (llm_max_tokens) или выберите модель без "
                        f"обязательного рассуждения.")
            return (f"Ollama вернул пустой ответ: модель «{модель}» потратила весь "
                    f"предел ({предел} токенов) на рассуждение и не успела "
                    f"ответить{повтор}. Выключите «Рассуждение модели» (llm_think) или "
                    f"поднимите «Предел ответа модели» (llm_max_tokens).")
        if почему == "предел":
            return (f"Ollama вернул пустой ответ: ответ оборвался на пределе "
                    f"({предел} токенов), не начавшись{повтор}. Поднимите «Предел "
                    f"ответа модели» (llm_max_tokens).")
        if почему == "сразу":
            if ответ.подсказка and ответ.подсказка >= окно * 0.9:
                return (f"Ollama вернул пустой ответ: подсказка ({ответ.подсказка} "
                        f"токенов) не помещается в окно контекста ({окно}), и "
                        f"модель закончила, не сказав ни слова{повтор}. Уменьшите "
                        f"«Предел текста на один вызов» (llm_context_chars) или "
                        f"увеличьте окно (llm_num_ctx).")
            return (f"Ollama вернул пустой ответ: модель «{модель}» закончила, не "
                    f"сказав ни слова (подсказка {ответ.подсказка} токенов, окно "
                    f"{окно}){повтор}. Так бывает, когда шаблон модели не сходится "
                    f"с её версией: обновите модель (ollama pull {модель}).")
        return (f"Ollama вернул пустой ответ: модель «{модель}», причина остановки "
                f"«{ответ.причина or 'не названа'}», сказано токенов: "
                f"{ответ.сказано}{повтор}.")

    def _openai(self, system: str, user: str, json_mode: bool, max_tokens: int) -> str:
        """Вызов OpenAI-совместимого сервера — с тем же лечением пустоты.

        Поля, выключающего рассуждение, у совместимых серверов общего нет
        (у vLLM оно одно, у llama.cpp другое, настоящий OpenAI отвергает
        оба), поэтому здесь только запас: пустой ответ с рассуждением или
        обрывом на пределе — повтор с пределом вдвое больше.
        """
        текст, мысли, причина = self._openai_раз(system, user, json_mode, max_tokens)
        if текст:
            return текст
        if мысли or причина == "length":
            предел2 = max(max_tokens * 2, max_tokens + БЮДЖЕТ_РАССУЖДЕНИЯ["medium"])
            log.info("Пустой ответ совместимого сервера (%s) — спрашиваем с "
                     "пределом %d", "рассуждение" if мысли else "предел", предел2)
            текст, мысли, причина = self._openai_раз(system, user, json_mode, предел2)
            if текст:
                return текст
            if мысли:
                raise LLMError(
                    f"Сервер модели вернул пустой ответ: модель «{self.model}» "
                    f"потратила весь предел ({предел2} токенов) на рассуждение и не "
                    f"успела ответить. Выключите рассуждение на стороне сервера "
                    f"модели или поднимите «Предел ответа модели» (llm_max_tokens).")
            raise LLMError(
                f"Сервер модели вернул пустой ответ: ответ оборвался на пределе "
                f"({предел2} токенов), не начавшись.")
        raise LLMError(f"Сервер модели вернул пустой ответ (причина остановки "
                       f"«{причина or 'не названа'}»).")

    def _openai_раз(self, system: str, user: str, json_mode: bool,
                    max_tokens: int) -> tuple[str, str, str]:
        """Один запрос: текст ответа, рассуждение и причина остановки."""
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
        первый = выборы[0] if выборы else {}
        сообщение = первый.get("message") or {}
        текст, внутри = без_рассуждения(str(сообщение.get("content") or ""))
        # Рассуждение у совместимых серверов лежит под разными именами:
        # reasoning_content у vLLM и DeepSeek, reasoning у совместимого входа
        # самой Ollama.
        мысли = (str(сообщение.get("reasoning_content") or "")
                 or str(сообщение.get("reasoning") or "") or внутри)
        return текст.strip(), мысли, str(первый.get("finish_reason") or "")

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
            ошибка = LLMError(f"Сервер модели ответил {exc.code}: {кусок or exc.reason}")
            ошибка.status = int(exc.code)
            ошибка.body = кусок
            ошибка.временная = int(exc.code) in _ВРЕМЕННЫЕ_КОДЫ
            raise ошибка from exc
        except urllib.error.URLError as exc:
            причина = getattr(exc, "reason", exc)
            if isinstance(причина, TimeoutError) or "timed out" in str(причина):
                raise LLMError(f"Сервер модели не ответил за {timeout:g} с.") from exc
            raise _временная(
                f"Сервер модели недоступен по адресу {self.url}: {причина}") from exc
        except TimeoutError as exc:
            raise LLMError(f"Сервер модели не ответил за {timeout:g} с.") from exc
        except OSError as exc:
            raise _временная(f"Сервер модели недоступен: {exc}") from exc
        except http.client.HTTPException as exc:
            # IncompleteRead и родня не наследуют ни URLError, ни OSError,
            # и уходили из клиента сырым исключением: состояние слоя
            # отвечало 500 вместо «сервер не отвечает», а счётчик ошибок
            # оставался нулём. Обрыв за обратным прокси с коротким
            # тайм-аутом чтения — обычное дело.
            raise LLMError(f"Ответ сервера модели оборвался: "
                           f"{type(exc).__name__}") from exc
        try:
            разобрано = json.loads(сырое.decode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise LLMError("Сервер модели ответил не JSON.") from exc
        if not isinstance(разобрано, dict):
            raise LLMError("Сервер модели ответил не объектом JSON.")
        return разобрано
