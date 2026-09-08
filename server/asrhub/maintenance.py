"""Обслуживание по расписанию: резервная копия базы и сводка о работе.

Оба дела до сих пор лежали на администраторе. Копия базы — строкой в `cron`
из главы «Эксплуатация»; сводка не существовала вовсе, и о том, как шли дела
за неделю, можно было узнать, только открыв раздел аналитики самому.
Отчёта, который никто не открывает, не существует.

Расписание здесь простое, без выражений `cron`: «раз в N часов, считая от
предыдущего раза». Сервер перезапускают, и привязка к календарю потребовала
бы помнить, что уже сделано; отметка «когда в последний раз» лежит в базе и
переживает перезапуск сама.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .instance import INSTANCE_ID
from .logging_setup import get_logger

log = get_logger("maintenance")

#: Ключи отметок в таблице настроек: когда обслуживание выполнялось.
KV_BACKUP = "maintenance.backup_at"
KV_DIGEST = "maintenance.digest_at"

#: Сколько ждать ответа от адреса сводки. Дольше держать служебный поток
#: незачем: сводка — не то, ради чего стоит задерживать уборку хранилища.
DIGEST_TIMEOUT = 20.0


def _пора(db: Any, ключ: str, часов: float) -> bool:
    """Наступил ли срок очередного захода.

    Первый заход после запуска не делается сразу: сервер только поднялся,
    показывать в сводке ещё нечего, а копию базы разумнее снять, когда
    работа уже идёт. Поэтому отсутствующая отметка ставится «сейчас», и
    первый настоящий заход случится через положенный срок.
    """
    if часов <= 0:
        return False
    было = db.get_kv(ключ)
    сейчас = time.time()
    if not было:
        db.set_kv(ключ, сейчас)
        return False
    try:
        прошло = сейчас - float(было)
    except (TypeError, ValueError):
        db.set_kv(ключ, сейчас)
        return False
    return прошло >= часов * 3600


def backup_dir(settings: Any) -> Path:
    """Каталог для копий: заданный настройкой или «backups» рядом с данными."""
    задан = str(settings.get("backup_dir") or "").strip()
    return Path(задан) if задан else Path(settings.paths.data) / "backups"


def make_backup(db: Any, settings: Any) -> Path | None:
    """Снимает копию базы и убирает лишние.

    Копирование идёт командой SQLite «.backup», а не `cp`: база работает в
    режиме WAL, рядом лежат файлы `-wal` и `-shm`, и обычная копия
    получается несогласованной — то есть выглядит как копия и не является
    ею. Ровно об этом предупреждает глава «Эксплуатация», и сервер обязан
    делать так же, как советует человеку.
    """
    каталог = backup_dir(settings)
    try:
        каталог.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("Каталог для резервных копий недоступен (%s): %s", каталог, exc)
        return None

    имя = f"asrhub-{time.strftime('%Y%m%d-%H%M%S')}.db"
    цель = каталог / имя
    начало = time.time()
    try:
        приёмник = sqlite3.connect(str(цель))
        try:
            with приёмник:
                db.conn.backup(приёмник)
        finally:
            приёмник.close()
    except (sqlite3.Error, OSError) as exc:
        log.warning("Резервная копия не удалась: %s", exc)
        цель.unlink(missing_ok=True)
        return None

    размер = цель.stat().st_size
    log.info("Резервная копия: %s, %.1f МБ, за %.1f с",
             цель, размер / 1024 / 1024, time.time() - начало)
    _подчистить(каталог, int(settings.get("backup_keep") or 7))
    return цель


def _подчистить(каталог: Path, держать: int) -> None:
    """Удаляет копии сверх заданного числа, начиная со старых."""
    try:
        копии = sorted(каталог.glob("asrhub-*.db"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        log.warning("Не удалось перечислить копии в %s: %s", каталог, exc)
        return
    for лишняя in копии[max(1, держать):]:
        try:
            лишняя.unlink()
            log.info("Удалена старая копия: %s", лишняя.name)
        except OSError as exc:
            log.warning("Старую копию %s удалить не удалось: %s", лишняя.name, exc)


def build_digest(analytics: Any, settings: Any, *, period: str = "") -> dict[str, Any]:
    """Сводка о работе сервера — то же, что в разделе «Аналитика».

    Собирается по всем заданиям, без разреза по владельцу: сводка уходит
    тому, кто отвечает за сервер целиком.
    """
    срок = period or str(settings.get("digest_period") or "week")
    отчёт = analytics.full_report(срок, owner=None)
    сводка = отчёт.get("overview") or {}
    ошибки = отчёт.get("errors") or {}
    очередь = (отчёт.get("queue") or {}).get("overall") or {}
    надёжность = отчёт.get("reliability") or {}
    return {
        "kind": "asrhub.digest",
        "period": срок,
        "generated_at": time.time(),
        # Кто прислал: на общей базе серверов несколько, и сводка без
        # имени отправителя не отвечает на первый же вопрос получателя.
        "instance": INSTANCE_ID,
        "jobs": сводка.get("jobs"),
        "volume": сводка.get("volume"),
        "performance": сводка.get("performance"),
        "quality": сводка.get("quality"),
        "failure_rate": ошибки.get("failure_rate"),
        "top_errors": (ошибки.get("by_code") or [])[:5],
        "queue_wait": {k: очередь.get(k) for k in ("count", "p50", "p95", "max")},
        "first_attempt_rate": надёжность.get("first_attempt_rate"),
        "models": (отчёт.get("models") or [])[:10],
        "text": digest_text(сводка, ошибки, очередь),
    }


def digest_text(сводка: dict[str, Any], ошибки: dict[str, Any],
                очередь: dict[str, Any]) -> str:
    """Та же сводка словами.

    Приёмник входящих сообщений в мессенджере показывает поле `text` и
    ничего не знает про остальные: без этой строки в чат приходил бы
    свёрнутый JSON, который никто не разворачивает.
    """
    задания = сводка.get("jobs") or {}
    объём = сводка.get("volume") or {}
    скорость = сводка.get("performance") or {}
    качество = сводка.get("quality") or {}

    def число(значение: Any, знаков: int = 1) -> str:
        try:
            return f"{float(значение):.{знаков}f}".rstrip("0").rstrip(".")
        except (TypeError, ValueError):
            return "—"

    строки = [
        f"ASR Hub, сводка за период «{сводка.get('period') or '—'}»",
        f"Заданий: {задания.get('total') or 0}, "
        f"готово {задания.get('completed') or 0}, "
        f"ошибок {задания.get('failed') or 0}",
        f"Звука обработано: {число(объём.get('audio_hours'))} ч, "
        f"слов {объём.get('words') or 0}",
        f"Скорость: RTF {число((скорость.get('rtf') or {}).get('avg'), 3)} "
        f"(в {число(скорость.get('speedup'))} раза быстрее реального времени)",
    ]
    уверенность = (качество.get("confidence") or {}).get("avg")
    if уверенность is not None:
        строки.append(f"Средняя уверенность: {число(уверенность, 3)}")
    ошибка_wer = (качество.get("wer") or {}).get("avg")
    if ошибка_wer is not None:
        строки.append(f"WER на заданиях с эталоном: {число(ошибка_wer, 3)}")
    if очередь.get("p95") is not None:
        строки.append(f"Ожидание в очереди: p50 {число(очередь.get('p50'))} с, "
                      f"p95 {число(очередь.get('p95'))} с")
    верхние = (ошибки.get("by_code") or [])[:3]
    if верхние:
        перечень = ", ".join(f"{o.get('code')} ×{o.get('count')}" for o in верхние)
        строки.append(f"Чаще всего падало: {перечень}")
    return "\n".join(строки)


def send_digest(digest: dict[str, Any], url: str) -> bool:
    """Отправляет сводку на заданный адрес."""
    тело = json.dumps(digest, ensure_ascii=False).encode("utf-8")
    try:
        # Сборка запроса — тоже внутри: негодный адрес в настройках
        # («без схемы», опечатка) роняет уже конструктор, и без этого
        # служебный цикл писал бы в журнал ошибку каждые двадцать секунд.
        запрос = urllib.request.Request(                 # noqa: S310
            url, data=тело, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "User-Agent": "ASR Hub"})
        with urllib.request.urlopen(запрос, timeout=DIGEST_TIMEOUT) as ответ:  # noqa: S310
            log.info("Сводка отправлена на %s: код %s", url, ответ.status)
            return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # Сбой отправки никогда не влияет на работу сервера: сводка — это
        # удобство, а не часть обработки заданий.
        log.warning("Сводку отправить не удалось (%s): %s", url, exc)
        return False


def run_scheduled(db: Any, settings: Any, analytics: Any) -> dict[str, Any]:
    """Один заход обслуживания. Вызывается служебным циклом.

    Возвращает, что было сделано, — чтобы вызывающий мог это записать, а
    тест проверить, не подглядывая в журнал.
    """
    сделано: dict[str, Any] = {}
    if _пора(db, KV_BACKUP, float(settings.get("backup_interval_hours") or 0)):
        db.set_kv(KV_BACKUP, time.time())
        копия = make_backup(db, settings)
        сделано["backup"] = str(копия) if копия else None

    адрес = str(settings.get("digest_url") or "").strip()
    if адрес and _пора(db, KV_DIGEST, float(settings.get("digest_interval_hours") or 0)):
        db.set_kv(KV_DIGEST, time.time())
        try:
            сводка = build_digest(analytics, settings)
        except Exception as exc:                             # noqa: BLE001
            log.warning("Сводку собрать не удалось: %s", exc)
        else:
            сделано["digest"] = send_digest(сводка, адрес)
    return сделано


def restore(path: Path, target: Path) -> None:
    """Возвращает базу из копии на место рабочей.

    Рабочую базу не удаляем, а переименовываем: восстановление из не той
    копии — обычная ошибка, и она должна быть обратимой. Файлы `-wal` и
    `-shm` от прежней базы обязаны уйти вместе с ней, иначе SQLite
    достроит по ним состояние, которого в восстановленной копии нет.
    """
    if not path.exists():
        raise FileNotFoundError(f"Копия не найдена: {path}")
    # Проверяем ДО того, как трогать рабочую базу. Восстановление из битого
    # файла, обнаруженное после подмены, оставляет человека вообще без
    # базы — с двумя нерабочими файлами вместо одного.
    try:
        with sqlite3.connect(str(path)) as проверка:
            итог = проверка.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise ValueError(
            f"Файл «{path}» не похож на базу ASR Hub: {exc}. "
            "Проверьте, что указана копия, а не архив или журнал.") from exc
    if итог != "ok":
        raise ValueError(f"Копия повреждена, восстановление отменено: {итог}")

    метка = time.strftime("%Y%m%d-%H%M%S")
    if target.exists():
        target.rename(target.with_suffix(f".db.before-restore-{метка}"))
    for хвост in ("-wal", "-shm"):
        спутник = Path(str(target) + хвост)
        if спутник.exists():
            спутник.rename(Path(str(спутник) + f".before-restore-{метка}"))
    shutil.copy2(path, target)
    log.info("База восстановлена из %s", path)
