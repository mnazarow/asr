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
import os
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


def find_local(models_dir: Path, source: str, revision: str = "") -> Path | None:
    """Веса модели на диске, если они уже скачаны.

    Раскладка зависит от источника: Hugging Face кладёт веса в
    `models--владелец--имя`, прямые ссылки — в каталог по имени архива,
    а GigaAM — одним файлом `<вариант>.ckpt` рядом, потому что качает их не
    с Hugging Face, а со своего CDN. Без последнего случая скачанная модель
    GigaAM показывалась незагруженной навсегда.
    """
    if not models_dir.exists():
        return None
    if source.startswith("ai-sage/GigaAM"):
        from .engines.gigaam_engine import weights_file
        candidate = models_dir / weights_file(source, revision)
        if candidate.exists():
            return candidate
        # Без ревизии сказать точнее нечего — считаем скачанным любой вариант
        # этого семейства, иначе список «установленных» пустеет на ровном месте.
        if not revision:
            prefix = {"ai-sage/GigaAM-v3": "v3_", "ai-sage/GigaAM-v2": "v2_",
                      "ai-sage/GigaAM-Multilingual": "multilingual_"}.get(source, "v1_")
            found = sorted(models_dir.glob(f"{prefix}*.ckpt"))
            return found[0] if found else None
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


def размер_каталога(путь: Path | str | None, *, предел: int | None = None
                    ) -> tuple[int, int, bool]:
    """Размер каталога на диске: байты, файлы и «обход закончен».

    Один обход на весь сервер: им считают и размер модели в списке, и
    метрику `asrhub_storage_bytes`, и самопроверка. Раньше их было три, и
    два из них шли по символическим ссылкам. В кеше Hugging Face веса лежат
    в `blobs/`, а `snapshots/<ревизия>/` — это ссылки на них: `rglob` с
    `is_file()` проходил и то и другое, и каталог моделей получался вдвое
    больше настоящего. Самопроверка при этом считала верно, и две цифры об
    одном и том же расходились ровно вдвое.

    Правило такое. В каталоги по ссылке не заходим (иначе петля и чужой
    диск). Ссылка на файл внутри обходимого дерева не считается: сам файл
    будет посчитан, когда до него дойдёт обход. Ссылка на файл снаружи
    считается один раз — веса, вынесенные на другой диск ссылкой, всё же
    занимают место под модели.

    `предел` — сколько файлов обойти, прежде чем сдаться: каталог
    результатов на миллион файлов обходится минутами. Тогда третий элемент
    ответа — False, а байты — оценка снизу.
    """
    if путь is None:
        return 0, 0, True
    корень = os.path.realpath(str(путь))
    try:
        if os.path.isfile(корень):
            # Веса GigaAM — один файл, а не каталог: обход по файлу не даёт
            # ничего, и размер скачанной модели показывался нулевым.
            return os.path.getsize(корень), 1, True
        if not os.path.isdir(корень):
            return 0, 0, True
    except OSError:
        return 0, 0, False
    внутри = корень.rstrip(os.sep) + os.sep
    всего = файлов = 0
    снаружи: set[str] = set()
    полностью = True
    стек = [корень]
    while стек:
        текущий = стек.pop()
        try:
            записи = list(os.scandir(текущий))
        except OSError:
            # Каталог без прав на чтение: остальное дерево считаем, но
            # честно говорим, что посчитано не всё.
            полностью = False
            continue
        for запись in записи:
            try:
                if запись.is_dir(follow_symlinks=False):
                    стек.append(запись.path)
                    continue
                if запись.is_symlink():
                    цель = os.path.realpath(запись.path)
                    if цель.startswith(внутри) or цель in снаружи or not os.path.isfile(цель):
                        continue
                    снаружи.add(цель)
                    размер = os.path.getsize(цель)
                elif запись.is_file(follow_symlinks=False):
                    размер = запись.stat(follow_symlinks=False).st_size
                else:
                    continue
            except OSError:
                continue
            всего += размер
            файлов += 1
            if предел is not None and файлов >= предел:
                return всего, файлов, False
    return всего, файлов, полностью


def directory_size(path: Path | None) -> int:
    """Размер модели на диске в байтах — см. `размер_каталога`."""
    if path is None or not path.exists():
        return 0
    return размер_каталога(path)[0]


def fingerprint(models_dir: Path | str, source: str, revision: str = "") -> str:
    """Короткий отпечаток весов модели.

    Пустая строка означает «весов на диске нет» — так бывает, когда модель
    ещё не скачана или движок держит их в другом месте. В этом случае кеш
    работает как раньше, по имени модели: хуже, чем с отпечатком, но не
    хуже, чем было.

    Ревизия нужна там, где варианты одной модели лежат рядом: у GigaAM это
    `v3_ctc.ckpt` и `v3_rnnt.ckpt` в одном каталоге. Без неё `find_local`
    брал первый по алфавиту — `v3_ctc`, — и обновление весов модели по
    умолчанию (`gigaam-v3-rnnt`) отпечаток не меняло: кеш отдавал
    расшифровку прежними весами, ровно то, ради чего отпечаток заводили.
    """
    if not source:
        return ""
    directory = Path(models_dir)
    key = f"{directory}|{source}|{revision or ''}"
    now = time.time()
    with _lock:
        cached = _cache.get(key)
        if cached and now - cached[0] < _TTL_S:
            return cached[1]

    local = find_local(directory, source, revision or "")
    value = ""
    if local is not None:
        digest = hashlib.blake2b(digest_size=8)
        entries: list[tuple[str, int, int]] = []
        # Веса GigaAM — ОДИН ФАЙЛ `<вариант>.ckpt`, а не каталог: качаются
        # они не с Hugging Face, а со своего CDN. `rglob` по файлу не даёт
        # ничего, поэтому отпечаток у всех девяти моделей GigaAM — включая
        # ту, что стоит в каталоге умолчанием, — был пуст ВСЕГДА. Пустой
        # отпечаток означает «весов на диске нет», и кеш результатов
        # переставал различать версии модели: обновили веса, попросили ту
        # же запись заново — и в ответ приезжала расшифровка, сделанная
        # прежней версией. Ровно от этого отпечаток и придуман. Та же
        # ошибка была в `directory_size`, и там её уже чинили.
        обход = [local] if local.is_file() else sorted(local.rglob("*"))
        for item in обход:
            try:
                if not item.is_file():
                    continue
                stat = item.stat()
            except OSError:
                continue
            # Ссылки внутри кеша Hugging Face ведут на blobs; там и размер,
            # и время изменения настоящие, поэтому обходим как есть.
            имя = item.name if item == local else str(item.relative_to(local))
            entries.append((имя, stat.st_size, stat.st_mtime_ns))
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
