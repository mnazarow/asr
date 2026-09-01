"""Типизированные ошибки ASR Hub.

Каждая ошибка несёт:
* машинный код — для клиентов и метрик;
* понятное сообщение на русском — для интерфейса;
* подсказку, что делать — самая ценная часть при разборе инцидента;
* признак «можно ли повторить» — им пользуется очередь.
"""
from __future__ import annotations

from typing import Any


class ASRHubError(Exception):
    """Базовая ошибка. Все остальные наследуются от неё."""

    code = "internal_error"
    http_status = 500
    retryable = False
    hint = "Проверьте журнал сервера: asrctl logs --tail 200"

    def __init__(self, message: str = "", *, hint: str | None = None,
                 details: dict[str, Any] | None = None, cause: BaseException | None = None):
        self.message = message or self.__doc__ or self.code
        if hint is not None:
            self.hint = hint
        self.details = details or {}
        self.cause = cause
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
        }
        if self.details:
            data["details"] = self.details
        if self.cause is not None:
            data["cause"] = f"{type(self.cause).__name__}: {self.cause}"
        return data

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --------------------------------------------------------------------------
# Ошибки конфигурации и окружения
# --------------------------------------------------------------------------

class ConfigError(ASRHubError):
    """Ошибка конфигурации."""

    code = "config_error"
    http_status = 400
    hint = "Проверьте config.yaml и запустите: asrctl doctor"


class DependencyMissing(ASRHubError):
    """Не установлена необходимая зависимость."""

    code = "dependency_missing"
    http_status = 503
    hint = "Установите движок: asrctl engines install <движок>"

    def __init__(self, engine: str, package: str = "", cause: BaseException | None = None):
        pkg = package or engine
        super().__init__(
            f"Для движка «{engine}» не установлен пакет «{pkg}».",
            hint=(f"Выполните на сервере: bash scripts/models.sh install-engine {engine}\n"
                  f"или вручную: pip install -r requirements/engines/{engine}.txt"),
            details={"engine": engine, "package": pkg},
            cause=cause,
        )


class BinaryMissing(ASRHubError):
    """Не найдена внешняя программа."""

    code = "binary_missing"
    http_status = 503

    def __init__(self, binary: str, install_hint: str = ""):
        super().__init__(
            f"Не найдена программа «{binary}» в PATH.",
            hint=install_hint or (
                f"Установите «{binary}»: на Debian/Ubuntu — apt install {binary}; "
                f"на macOS — brew install {binary}; на Windows — winget install {binary}"),
            details={"binary": binary},
        )


class HardwareError(ASRHubError):
    """Проблема с вычислительным устройством."""

    code = "hardware_error"
    http_status = 503
    retryable = True
    hint = "Проверьте доступность устройства: asrctl doctor --hardware"


class OutOfMemoryError(ASRHubError):
    """Не хватило памяти."""

    code = "out_of_memory"
    http_status = 503
    retryable = True

    def __init__(self, device: str = "GPU", requested: str = "", cause: BaseException | None = None):
        super().__init__(
            f"Недостаточно памяти на устройстве {device}"
            + (f" (требуется около {requested})" if requested else "") + ".",
            hint=("Уменьшите размер пакета (batch_size) или выберите более лёгкую модель. "
                  "ASR Hub автоматически повторит задание с меньшим пакетом, "
                  "если разрешены повторы."),
            details={"device": device, "requested": requested},
            cause=cause,
        )


# --------------------------------------------------------------------------
# Ошибки моделей и движков
# --------------------------------------------------------------------------

class ModelNotFound(ASRHubError):
    """Модель не найдена в каталоге."""

    code = "model_not_found"
    http_status = 404

    def __init__(self, model_id: str, suggestions: list[str] | None = None):
        sug = ""
        if suggestions:
            sug = " Возможно, вы имели в виду: " + ", ".join(suggestions[:5]) + "."
        super().__init__(
            f"Модель «{model_id}» отсутствует в каталоге.{sug}",
            hint="Список доступных моделей: asrctl models list",
            details={"model": model_id, "suggestions": suggestions or []},
        )


class ModelNotDownloaded(ASRHubError):
    """Веса модели не загружены."""

    code = "model_not_downloaded"
    http_status = 503
    retryable = True

    def __init__(self, model_id: str, size_mb: int | None = None):
        size = f" (примерно {size_mb} МБ)" if size_mb else ""
        super().__init__(
            f"Веса модели «{model_id}» не загружены{size}.",
            hint=f"Загрузите заранее: asrctl models download {model_id}",
            details={"model": model_id, "size_mb": size_mb},
        )


class ModelLoadError(ASRHubError):
    """Не удалось загрузить модель."""

    code = "model_load_error"
    http_status = 503
    retryable = True
    hint = ("Проверьте целостность весов и свободное место. "
            "Повторная загрузка: asrctl models download <модель> --force")


class GatedModelError(ASRHubError):
    """Модель требует принятия лицензии."""

    code = "gated_model"
    http_status = 403

    def __init__(self, model_id: str, url: str = ""):
        super().__init__(
            f"Для модели «{model_id}» нужно принять лицензию и указать токен Hugging Face.",
            hint=(f"1. Откройте {url or 'страницу модели на huggingface.co'} и примите условия.\n"
                  "2. Создайте токен на huggingface.co/settings/tokens.\n"
                  "3. Пропишите его: asrctl config set hf_token <токен>"),
            details={"model": model_id, "url": url},
        )


class EngineError(ASRHubError):
    """Ошибка внутри движка распознавания."""

    code = "engine_error"
    http_status = 500
    retryable = True
    hint = "Попробуйте другой движок для той же модели или включите резервную модель."


class UnsupportedFeature(ASRHubError):
    """Возможность не поддерживается выбранной комбинацией."""

    code = "unsupported_feature"
    http_status = 400

    def __init__(self, feature: str, engine: str = "", model: str = ""):
        where = " ".join(x for x in (f"движком «{engine}»" if engine else "",
                                     f"моделью «{model}»" if model else "") if x)
        super().__init__(
            f"Возможность «{feature}» не поддерживается {where or 'текущей конфигурацией'}.",
            hint="Выберите другую модель или отключите эту настройку.",
            details={"feature": feature, "engine": engine, "model": model},
        )


# --------------------------------------------------------------------------
# Ошибки входных данных
# --------------------------------------------------------------------------

class AudioError(ASRHubError):
    """Ошибка обработки аудио."""

    code = "audio_error"
    http_status = 400
    hint = "Проверьте, что файл не повреждён и содержит звуковую дорожку."


class UnsupportedFormat(ASRHubError):
    """Неподдерживаемый формат файла."""

    code = "unsupported_format"
    http_status = 415

    def __init__(self, filename: str, detected: str = ""):
        super().__init__(
            f"Формат файла «{filename}» не поддерживается"
            + (f" (определён как {detected})" if detected else "") + ".",
            hint=("Поддерживаются wav, mp3, m4a, flac, ogg, opus, wma, aac, "
                  "а также видеофайлы mp4, mkv, avi, mov, webm — звук извлекается автоматически."),
            details={"filename": filename, "detected": detected},
        )


class FileTooLarge(ASRHubError):
    """Файл превышает допустимый размер."""

    code = "file_too_large"
    http_status = 413

    def __init__(self, size_mb: float, limit_mb: int):
        super().__init__(
            f"Размер файла {size_mb:.1f} МБ превышает предел {limit_mb} МБ.",
            hint=("Увеличьте max_upload_mb в настройках сервера или разбейте файл на части. "
                  "Не забудьте про client_max_body_size в nginx, если используете обратный прокси."),
            details={"size_mb": size_mb, "limit_mb": limit_mb},
        )


class AudioTooLong(ASRHubError):
    """Запись длиннее допустимого."""

    code = "audio_too_long"
    http_status = 413

    def __init__(self, duration_s: float, limit_s: int):
        super().__init__(
            f"Длительность {duration_s / 60:.1f} мин превышает предел {limit_s / 60:.0f} мин.",
            hint="Увеличьте audio_max_duration_s или разбейте запись на части.",
            details={"duration_s": duration_s, "limit_s": limit_s},
        )


class NoSpeechDetected(ASRHubError):
    """В записи не обнаружено речи."""

    code = "no_speech"
    http_status = 422
    hint = ("Проверьте запись на слух. Если речь тихая, понизьте порог детектора речи "
            "(vad_threshold) до 0.25–0.3 или отключите VAD.")


# --------------------------------------------------------------------------
# Ошибки очереди и выполнения
# --------------------------------------------------------------------------

class PresetNotFound(ASRHubError):
    """Пресета с таким идентификатором нет."""

    code = "preset_not_found"
    http_status = 404
    hint = "Список пресетов: GET /api/presets"

    def __init__(self, preset_id: str, available: list[str] | None = None):
        super().__init__(f"Пресет «{preset_id}» не найден.")
        if available:
            self.hint = "Доступные пресеты: " + ", ".join(available)


class KeyNotFound(ASRHubError):
    """Ключ доступа с таким началом не найден."""

    code = "key_not_found"
    http_status = 404
    hint = "Список ключей: GET /api/keys"

    def __init__(self, preview: str):
        super().__init__(f"Ключ, начинающийся на «{preview}», не найден.")


class MetricNotFound(ASRHubError):
    """Метрика с таким именем отсутствует в каталоге мониторинга."""

    code = "metric_not_found"
    http_status = 404
    hint = "Список метрик: GET /api/monitoring/catalog"

    def __init__(self, name: str, suggestions: list[str] | None = None):
        super().__init__(f"Метрика «{name}» не найдена.")
        if suggestions:
            self.hint = "Похожие метрики: " + ", ".join(suggestions[:5])
        self.details = {"metric": name, "suggestions": suggestions or []}


class JobNotFound(ASRHubError):
    """Задание не найдено."""

    code = "job_not_found"
    http_status = 404
    hint = "Возможно, задание удалено по сроку хранения результатов."


class JobCancelled(ASRHubError):
    """Задание отменено."""

    code = "job_cancelled"
    http_status = 409
    hint = "Задание было отменено пользователем или по тайм-ауту."


class JobTimeout(ASRHubError):
    """Превышено время выполнения."""

    code = "job_timeout"
    http_status = 504
    retryable = True

    def __init__(self, timeout_s: int):
        super().__init__(
            f"Задание не завершилось за {timeout_s / 60:.0f} мин и было прервано.",
            hint=("Увеличьте job_timeout_s либо выберите более быструю модель. "
                  "Проверьте, не ушла ли модель в зацикливание: включите VAD "
                  "и отключите перенос контекста между окнами."),
            details={"timeout_s": timeout_s},
        )


class QueueFull(ASRHubError):
    """Очередь переполнена."""

    code = "queue_full"
    http_status = 429
    retryable = True
    hint = "Дождитесь освобождения очереди или увеличьте её предельный размер."


class RateLimited(ASRHubError):
    """Превышена частота запросов."""

    code = "rate_limited"
    http_status = 429
    retryable = True

    def __init__(self, limit: int, retry_after_s: int = 60):
        super().__init__(
            f"Превышен лимит {limit} запросов в минуту.",
            hint=f"Повторите через {retry_after_s} с или используйте ключ с большим лимитом.",
            details={"limit": limit, "retry_after_s": retry_after_s},
        )


# --------------------------------------------------------------------------
# Ошибки доступа
# --------------------------------------------------------------------------

class AuthError(ASRHubError):
    """Ошибка аутентификации."""

    code = "auth_error"
    http_status = 401
    hint = "Передайте ключ в заголовке X-API-Key или Authorization: Bearer <ключ>."


class ForbiddenError(ASRHubError):
    """Недостаточно прав."""

    code = "forbidden"
    http_status = 403
    hint = "Требуется ключ с ролью администратора."


class StorageError(ASRHubError):
    """Ошибка хранилища."""

    code = "storage_error"
    http_status = 507
    retryable = True
    hint = "Проверьте свободное место и права на каталог данных."


# --------------------------------------------------------------------------
# Классификация внешних исключений
# --------------------------------------------------------------------------

_OOM_MARKERS = (
    "out of memory", "cuda out of memory", "cublas_status_alloc_failed",
    "hip out of memory", "mps backend out of memory", "не хватает памяти",
)
_CUDNN_MARKERS = (
    "libcudnn", "cudnn_ops_infer", "cudnn64_", "could not load library",
)
_NETWORK_MARKERS = (
    "connection", "timed out", "temporary failure in name resolution",
    "max retries exceeded", "ssl", "proxy",
)
_GATED_MARKERS = (
    "gated repo", "awaiting a review", "accept the conditions", "401 client error",
    "you must be authenticated",
)


def classify_exception(exc: BaseException, *, engine: str = "", model: str = "") -> ASRHubError:
    """Превращает произвольное исключение в понятную ошибку ASR Hub.

    Точная классификация здесь напрямую влияет на поведение очереди: повторять
    задание или нет, и что показать пользователю.
    """
    if isinstance(exc, ASRHubError):
        return exc

    text = f"{type(exc).__name__}: {exc}".lower()

    if any(m in text for m in _OOM_MARKERS):
        device = "GPU" if "cuda" in text or "hip" in text else "MPS" if "mps" in text else "RAM"
        return OutOfMemoryError(device=device, cause=exc)

    if any(m in text for m in _CUDNN_MARKERS):
        return DependencyMissing(
            engine or "faster_whisper", "cuDNN", cause=exc,
        ).with_hint(
            "Рассогласование версий CUDA и cuDNN. Проверьте таблицу совместимости:\n"
            "  CUDA 12 + cuDNN 9 → ctranslate2>=4.5\n"
            "  CUDA 12 + cuDNN 8 → ctranslate2==4.4.0\n"
            "  CUDA 11 + cuDNN 8 → ctranslate2==3.24.0\n"
            "Быстрое решение: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12==9.* "
            "и добавить их каталоги в LD_LIBRARY_PATH."
        )

    if any(m in text for m in _GATED_MARKERS):
        return GatedModelError(model or "неизвестная модель")

    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        name = getattr(exc, "name", "") or str(exc).split("'")[-2] if "'" in str(exc) else ""
        return DependencyMissing(engine or "неизвестный", name, cause=exc)

    if isinstance(exc, FileNotFoundError):
        return AudioError(f"Файл не найден: {exc.filename or exc}", cause=exc)

    if isinstance(exc, PermissionError):
        return StorageError(f"Нет доступа к файлу: {exc.filename or exc}", cause=exc)

    if isinstance(exc, (TimeoutError, )) or "timed out" in text:
        return EngineError("Превышено время ожидания ответа движка.", cause=exc)

    if any(m in text for m in _NETWORK_MARKERS):
        err = EngineError("Сетевая ошибка при обращении к внешнему ресурсу.", cause=exc)
        err.retryable = True
        err.hint = ("Проверьте доступ в интернет с сервера. Если он закрыт, "
                    "загрузите модели заранее на машине с доступом и перенесите каталог моделей.")
        return err

    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return StorageError("На диске закончилось место.", cause=exc)

    return EngineError(f"Непредвиденная ошибка движка: {type(exc).__name__}: {exc}", cause=exc)


def _with_hint(self: ASRHubError, hint: str) -> ASRHubError:
    self.hint = hint
    return self


ASRHubError.with_hint = _with_hint  # type: ignore[attr-defined]
