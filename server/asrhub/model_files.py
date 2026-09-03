"""Файлы моделей на диске: поиск и отпечаток весов.

Отпечаток нужен кешу результатов. Кеш отвечает готовой расшифровкой, когда
совпали содержимое файла и настройки задания, — но имя модели ничего не
говорит о том, какие веса за ним стоят. Модель обновляют под тем же именем
(GigaAM и Whisper выкладывают новые ревизии постоянно), и старый результат
уходил пользователю как свежий, без единого признака, что он посчитан
прошлой версией.

Отпечаток берётся по метаданным файлов — путям, размерам и времени
изменения, — а не по их содержимому: веса весят гигабайты, читать их на
каждое задание нельзя, а для ответа на вопрос «те же это файлы или другие»
метаданных достаточно.
"""
from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

from .logging_setup import get_logger

log = get_logger("model_files")

#: Отпечатки живут недолго: за это время каталог всё равно не успеет
#: измениться незаметно, а обход диска не повторяется на каждое задание.
_TTL_S = 60.0

_cache: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


def find_local(models_dir: Path, source: str) -> Path | None:
    """Каталог с весами модели, если они уже скачаны.

    Раскладка зависит от источника: Hugging Face кладёт веса в
    `models--владелец--имя`, прямые ссылки — в каталог по имени архива.
    """
    if not models_dir.exists():
        return None
    if source.startswith("http"):
        name = source.rsplit("/", 1)[-1].replace(".zip", "")
        for candidate in models_dir.rglob(f"*{name}*"):
            if candidate.is_dir():
                return candidate
        return None
    slug = "models--" + source.replace("/", "--")
    for base in (models_dir, models_dir / "hub"):
        candidate = base / slug
        if candidate.exists():
            return candidate
    direct = models_dir / source.replace("/", "_")
    return direct if direct.exists() else None


def directory_size(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def fingerprint(models_dir: Path | str, source: str) -> str:
    """Короткий отпечаток весов модели.

    Пустая строка означает «весов на диске нет» — так бывает, когда модель
    ещё не скачана или движок держит их в другом месте. В этом случае кеш
    работает как раньше, по имени модели: хуже, чем с отпечатком, но не
    хуже, чем было.
    """
    if not source:
        return ""
    directory = Path(models_dir)
    key = f"{directory}|{source}"
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _TTL_S:
            return cached[1]

    local = find_local(directory, source)
    value = ""
    if local is not None:
        digest = hashlib.blake2b(digest_size=8)
        entries: list[tuple[str, int, int]] = []
        for item in sorted(local.rglob("*")):
            try:
                if not item.is_file():
                    continue
                stat = item.stat()
            except OSError:
                continue
            # Ссылки внутри кеша Hugging Face ведут на blobs; там и размер,
            # и время изменения настоящие, поэтому обходим как есть.
            entries.append((str(item.relative_to(local)), stat.st_size, stat.st_mtime_ns))
        for name, size, mtime in entries:
            digest.update(f"{name}|{size}|{mtime}\n".encode())
        if entries:
            value = digest.hexdigest()

    with _lock:
        _cache[key] = (now, value)
        if len(_cache) > 256:
            for stale in list(_cache)[:128]:
                _cache.pop(stale, None)
    return value


def forget(models_dir: Path | str | None = None) -> None:
    """Сбрасывает запомненные отпечатки — после загрузки или удаления весов."""
    with _lock:
        if models_dir is None:
            _cache.clear()
            return
        prefix = f"{Path(models_dir)}|"
        for key in [k for k in _cache if k.startswith(prefix)]:
            _cache.pop(key, None)
