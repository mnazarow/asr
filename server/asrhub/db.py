"""Хранилище ASR Hub на SQLite.

Выбор SQLite сознателен: сервер распознавания — не высоконагруженная OLTP-система,
а зависимость от внешней СУБД усложнила бы установку на трёх операционных системах.
Включён режим WAL, что даёт параллельное чтение во время записи.

Все обращения проходят через один пул соединений с блокировкой на запись.
Схема версионируется: при запуске выполняются недостающие миграции.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import StorageError
from .logging_setup import get_logger

log = get_logger("db")

SCHEMA_VERSION = 5

#: Сколько заданий убирать по сроку хранения за один заход служебного цикла.
CLEANUP_BATCH = 5000

_SCHEMA = [
    # --- версия 1: основные таблицы ---------------------------------------
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id                TEXT PRIMARY KEY,
        group_id          TEXT,
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL,
        queued_at         REAL,
        started_at        REAL,
        finished_at       REAL,
        deadline          REAL,
        status            TEXT NOT NULL DEFAULT 'queued',
        stage             TEXT DEFAULT '',
        progress          REAL DEFAULT 0,
        priority          INTEGER DEFAULT 50,
        filename          TEXT,
        file_path         TEXT,
        file_size         INTEGER DEFAULT 0,
        file_hash         TEXT,
        media_duration_s  REAL DEFAULT 0,
        engine            TEXT,
        model             TEXT,
        language          TEXT,
        params            TEXT DEFAULT '{}',
        result_path       TEXT,
        text              TEXT,
        segments_count    INTEGER DEFAULT 0,
        words_count       INTEGER DEFAULT 0,
        chars_count       INTEGER DEFAULT 0,
        speakers_count    INTEGER DEFAULT 0,
        avg_confidence    REAL,
        rtf               REAL,
        queue_time_s      REAL,
        processing_time_s REAL,
        audio_prep_s      REAL,
        model_load_s      REAL,
        inference_s       REAL,
        postprocess_s     REAL,
        peak_memory_mb    REAL,
        device            TEXT,
        retries           INTEGER DEFAULT 0,
        error_code        TEXT,
        error_message     TEXT,
        error_hint        TEXT,
        cancelled_by      TEXT,
        owner             TEXT DEFAULT 'anonymous',
        api_key_name      TEXT,
        source            TEXT DEFAULT 'api',
        tags              TEXT DEFAULT '',
        reference_text    TEXT,
        wer               REAL,
        cer               REAL,
        cached_from       TEXT,
        webhook_url       TEXT,
        webhook_status    TEXT,
        waveform          TEXT,
        instance_id       TEXT,
        heartbeat_at      REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_model ON jobs(model, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_id)",
    """
    CREATE TABLE IF NOT EXISTS segments (
        job_id        TEXT NOT NULL,
        idx           INTEGER NOT NULL,
        start_s       REAL NOT NULL,
        end_s         REAL NOT NULL,
        text          TEXT NOT NULL,
        speaker       TEXT,
        confidence    REAL,
        no_speech     REAL,
        compression   REAL,
        temperature   REAL,
        language      TEXT,
        words         TEXT,
        PRIMARY KEY (job_id, idx)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_segments_job ON segments(job_id, start_s)",
    """
    CREATE TABLE IF NOT EXISTS events (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id   TEXT,
        ts       REAL NOT NULL,
        kind     TEXT NOT NULL,
        message  TEXT,
        data     TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS metrics (
        id      INTEGER PRIMARY KEY AUTOINCREMENT,
        ts      REAL NOT NULL,
        name    TEXT NOT NULL,
        value   REAL NOT NULL,
        job_id  TEXT,
        model   TEXT,
        engine  TEXT,
        labels  TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metrics_name_ts ON metrics(name, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_model ON metrics(model, name, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS api_keys (
        key         TEXT PRIMARY KEY,
        name        TEXT,
        role        TEXT DEFAULT 'user',
        created_at  REAL,
        last_used   REAL,
        requests    INTEGER DEFAULT 0,
        rate_limit  INTEGER DEFAULT 0,
        enabled     INTEGER DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kv (
        key    TEXT PRIMARY KEY,
        value  TEXT,
        ts     REAL
    )
    """,
    # --- версия 2: агрегаты и системные снимки ----------------------------
    """
    CREATE TABLE IF NOT EXISTS model_stats (
        model            TEXT PRIMARY KEY,
        engine           TEXT,
        jobs_total       INTEGER DEFAULT 0,
        jobs_ok          INTEGER DEFAULT 0,
        jobs_failed      INTEGER DEFAULT 0,
        audio_seconds    REAL DEFAULT 0,
        processing_s     REAL DEFAULT 0,
        words_total      INTEGER DEFAULT 0,
        rtf_sum          REAL DEFAULT 0,
        rtf_count        INTEGER DEFAULT 0,
        confidence_sum   REAL DEFAULT 0,
        confidence_count INTEGER DEFAULT 0,
        wer_sum          REAL DEFAULT 0,
        wer_count        INTEGER DEFAULT 0,
        last_used        REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS system_samples (
        ts             REAL PRIMARY KEY,
        cpu_percent    REAL,
        ram_used_mb    REAL,
        ram_total_mb   REAL,
        gpu_percent    REAL,
        gpu_mem_mb     REAL,
        gpu_mem_total  REAL,
        disk_free_gb   REAL,
        queue_depth    INTEGER,
        active_jobs    INTEGER
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_samples_ts ON system_samples(ts DESC)",
    # --- версия 3: сравнительные прогоны ----------------------------------
    """
    CREATE TABLE IF NOT EXISTS benchmarks (
        id           TEXT PRIMARY KEY,
        created_at   REAL,
        name         TEXT,
        dataset      TEXT,
        models       TEXT,
        status       TEXT DEFAULT 'running',
        results      TEXT,
        notes        TEXT
    )
    """,
]


def now() -> float:
    return time.time()


def new_id(prefix: str = "job") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"



#: Ожидаемый набор колонок — сверяется при миграции. Собран из _SCHEMA,
#: поэтому не может разойтись с ней при добавлении новых полей.
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "jobs": {
        "id": "TEXT",
        "group_id": "TEXT",
        "created_at": "REAL",
        "updated_at": "REAL",
        "queued_at": "REAL",
        "started_at": "REAL",
        "finished_at": "REAL",
        "deadline": "REAL",
        "status": "TEXT NOT NULL DEFAULT 'queued'",
        "stage": "TEXT DEFAULT ''",
        "progress": "REAL DEFAULT 0",
        "priority": "INTEGER DEFAULT 50",
        "filename": "TEXT",
        "file_path": "TEXT",
        "file_size": "INTEGER DEFAULT 0",
        "file_hash": "TEXT",
        "media_duration_s": "REAL DEFAULT 0",
        "engine": "TEXT",
        "model": "TEXT",
        "language": "TEXT",
        "params": "TEXT DEFAULT '{}'",
        "result_path": "TEXT",
        "text": "TEXT",
        "segments_count": "INTEGER DEFAULT 0",
        "words_count": "INTEGER DEFAULT 0",
        "chars_count": "INTEGER DEFAULT 0",
        "speakers_count": "INTEGER DEFAULT 0",
        # Огибающая громкости: массив кривых в JSON. Хранится рядом с
        # заданием, а не в файле результата, чтобы её можно было отдать в
        # карточке задания, не читая диск.
        "waveform": "TEXT",
        # Какой экземпляр сервера взял задание и когда в последний раз
        # подтвердил, что жив. Нужно, чтобы два сервера на общей базе не
        # брали одно задание и чтобы задания умершего экземпляра вернулись
        # в очередь, а не висели «выполняется» вечно.
        "instance_id": "TEXT",
        "heartbeat_at": "REAL",
        "avg_confidence": "REAL",
        "rtf": "REAL",
        "queue_time_s": "REAL",
        "processing_time_s": "REAL",
        "audio_prep_s": "REAL",
        "model_load_s": "REAL",
        "inference_s": "REAL",
        "postprocess_s": "REAL",
        "peak_memory_mb": "REAL",
        "device": "TEXT",
        "retries": "INTEGER DEFAULT 0",
        "error_code": "TEXT",
        "error_message": "TEXT",
        "error_hint": "TEXT",
        "cancelled_by": "TEXT",
        "owner": "TEXT DEFAULT 'anonymous'",
        "api_key_name": "TEXT",
        "source": "TEXT DEFAULT 'api'",
        "tags": "TEXT DEFAULT ''",
        "reference_text": "TEXT",
        "wer": "REAL",
        "cer": "REAL",
        "cached_from": "TEXT",
        "webhook_url": "TEXT",
        "webhook_status": "TEXT",
    },
    "segments": {
        "job_id": "TEXT",
        "idx": "INTEGER",
        "start_s": "REAL",
        "end_s": "REAL",
        "text": "TEXT",
        "speaker": "TEXT",
        "confidence": "REAL",
        "no_speech": "REAL",
        "compression": "REAL",
        "temperature": "REAL",
        "language": "TEXT",
        "words": "TEXT",
    },
    "events": {
        "id": "INTEGER  AUTOINCREMENT",
        "job_id": "TEXT",
        "ts": "REAL",
        "kind": "TEXT",
        "message": "TEXT",
        "data": "TEXT",
    },
    "metrics": {
        "id": "INTEGER  AUTOINCREMENT",
        "ts": "REAL",
        "name": "TEXT",
        "value": "REAL",
        "job_id": "TEXT",
        "model": "TEXT",
        "engine": "TEXT",
        "labels": "TEXT",
    },
    "api_keys": {
        "key": "TEXT",
        "name": "TEXT",
        "role": "TEXT DEFAULT 'user'",
        "created_at": "REAL",
        "last_used": "REAL",
        "requests": "INTEGER DEFAULT 0",
        "rate_limit": "INTEGER DEFAULT 0",
        "enabled": "INTEGER DEFAULT 1",
    },
    "kv": {
        "key": "TEXT",
        "value": "TEXT",
        "ts": "REAL",
    },
    "model_stats": {
        "model": "TEXT",
        "engine": "TEXT",
        "jobs_total": "INTEGER DEFAULT 0",
        "jobs_ok": "INTEGER DEFAULT 0",
        "jobs_failed": "INTEGER DEFAULT 0",
        "audio_seconds": "REAL DEFAULT 0",
        "processing_s": "REAL DEFAULT 0",
        "words_total": "INTEGER DEFAULT 0",
        "rtf_sum": "REAL DEFAULT 0",
        "rtf_count": "INTEGER DEFAULT 0",
        "confidence_sum": "REAL DEFAULT 0",
        "confidence_count": "INTEGER DEFAULT 0",
        "wer_sum": "REAL DEFAULT 0",
        "wer_count": "INTEGER DEFAULT 0",
        "last_used": "REAL",
    },
    "system_samples": {
        "ts": "REAL",
        "cpu_percent": "REAL",
        "ram_used_mb": "REAL",
        "ram_total_mb": "REAL",
        "gpu_percent": "REAL",
        "gpu_mem_mb": "REAL",
        "gpu_mem_total": "REAL",
        "disk_free_gb": "REAL",
        "queue_depth": "INTEGER",
        "active_jobs": "INTEGER",
    },
    "benchmarks": {
        "id": "TEXT",
        "created_at": "REAL",
        "name": "TEXT",
        "dataset": "TEXT",
        "models": "TEXT",
        "status": "TEXT DEFAULT 'running'",
        "results": "TEXT",
        "notes": "TEXT",
    },
}

class Database:
    """Тонкая обёртка над SQLite с пулом соединений по потокам."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # --- соединения -----------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            try:
                conn = sqlite3.connect(
                    str(self.path), timeout=30.0, isolation_level=None,
                    check_same_thread=False)
            except sqlite3.Error as exc:
                raise StorageError(f"Не удалось открыть базу {self.path}: {exc}") from exc
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("PRAGMA cache_size=-32000")
            self._local.conn = conn
        return conn

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """Транзакция на запись. Сериализуется блокировкой процесса."""
        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise StorageError(f"Ошибка записи в базу: {exc}") from exc
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        try:
            return list(self.conn.execute(sql, params))
        except sqlite3.Error as exc:
            raise StorageError(f"Ошибка чтения из базы: {exc}", details={"sql": sql[:200]}) from exc

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.write() as conn:
            cur = conn.execute(sql, params)
            return cur.rowcount

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        if not rows:
            return 0
        with self.write() as conn:
            cur = conn.executemany(sql, rows)
            return cur.rowcount

    # --- миграции -------------------------------------------------------

    def _migrate(self) -> None:
        conn = self.conn
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as exc:
            raise StorageError(f"База повреждена или недоступна: {exc}") from exc
        if current >= SCHEMA_VERSION:
            return
        log.info("Обновление схемы базы: версия %s → %s", current, SCHEMA_VERSION)
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Колонки добавляются ПЕРЕД схемой: индексы из _SCHEMA могут
                # ссылаться на поля, которых в старой таблице ещё нет.
                self._add_missing_columns(conn)
                for statement in _SCHEMA:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise StorageError(f"Не удалось применить миграции: {exc}") from exc

    def _add_missing_columns(self, conn: sqlite3.Connection) -> None:
        """Добавляет колонки, которых нет в уже созданных таблицах.

        CREATE TABLE IF NOT EXISTS не трогает существующую таблицу, поэтому
        база, созданная прошлой версией, новых колонок не получала — и после
        обновления каждый запрос падал с «no such column». Здесь сравниваем
        фактический набор колонок с ожидаемым и дописываем недостающие.
        """
        for table, columns in _EXPECTED_COLUMNS.items():
            try:
                existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if not existing:
                continue                    # таблицы ещё нет — её создаст _SCHEMA
            for name, declaration in columns.items():
                if name not in existing:
                    log.info("Миграция: в таблицу «%s» добавлена колонка «%s»", table, name)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    # --- задания --------------------------------------------------------

    def create_job(self, data: dict[str, Any]) -> str:
        job_id = data.get("id") or new_id()
        ts = now()
        payload = {
            "id": job_id,
            "created_at": ts,
            "updated_at": ts,
            "queued_at": ts,
            "status": "queued",
            **data,
        }
        if isinstance(payload.get("params"), dict):
            payload["params"] = json.dumps(payload["params"], ensure_ascii=False)
        if isinstance(payload.get("waveform"), (list, dict)):
            payload["waveform"] = json.dumps(payload["waveform"], ensure_ascii=False)
        columns = ", ".join(payload)
        holders = ", ".join("?" for _ in payload)
        self.execute(f"INSERT INTO jobs ({columns}) VALUES ({holders})", list(payload.values()))
        self.add_event(job_id, "created", f"Задание создано: {payload.get('filename', '')}")
        return job_id

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now()
        if isinstance(fields.get("params"), dict):
            fields["params"] = json.dumps(fields["params"], ensure_ascii=False)
        if isinstance(fields.get("waveform"), (list, dict)):
            fields["waveform"] = json.dumps(fields["waveform"], ensure_ascii=False)
        assignments = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE jobs SET {assignments} WHERE id=?",
                     [*fields.values(), job_id])

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return _row_to_job(row) if row else None

    #: Колонки, которых достаточно аналитике. Без них выборка тащит ещё и
    #: колонку text — то есть все расшифровки целиком.
    #: Колонки облегчённого списка. Смысл набора — не тащить расшифровку и
    #: разбор по сегментам: на сотне часовых записей это единицы мегабайт.
    #: Всё остальное, чем пользуются планировщик, аналитика и таблицы,
    #: обязано здесь быть — иначе получатель молча видит None. Так уже
    #: случилось: без deadline не работала политика планирования по сроку,
    #: без error_message и filename разбор ошибок в аналитике показывал
    #: пустые столбцы у всех строк.
    LIGHT_COLUMNS = (
        "id, status, model, engine, language, owner, source, priority, "
        "filename, deadline, created_at, queued_at, started_at, finished_at, "
        "media_duration_s, processing_time_s, queue_time_s, audio_prep_s, "
        "model_load_s, inference_s, postprocess_s, rtf, words_count, "
        "chars_count, segments_count, speakers_count, avg_confidence, wer, "
        "cer, error_code, error_message, error_hint, retries, cached_from, "
        # progress и stage — по несколько байт, но без них облегчённый список
        # не годится для очереди: на главном экране полоса выполнения и
        # название стадии берутся именно из него.
        "device, file_size, progress, stage"
    )

    def list_jobs(self, *, status: str | list[str] | None = None,
                  owner: str | list[str] | None = None,
                  model: str | None = None, search: str | None = None,
                  group_id: str | None = None, since: float | None = None,
                  limit: int = 100, offset: int = 0,
                  order: str = "created_at DESC",
                  light: bool = False,
                  ready_before: float | None = None) -> list[dict[str, Any]]:
        where: list[str] = []
        args: list[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        if owner:
            # Список владельцев — это подразделение: ключи одной группы
            # видят задания друг друга.
            owners = [owner] if isinstance(owner, str) else list(owner)
            where.append("owner IN (" + ",".join("?" for _ in owners) + ")")
            args.extend(owners)
        if model:
            where.append("model=?")
            args.append(model)
        if group_id:
            where.append("group_id=?")
            args.append(group_id)
        if since:
            where.append("created_at>=?")
            args.append(since)
        if search:
            where.append("(filename LIKE ? OR text LIKE ? OR id LIKE ?)")
            needle = f"%{search}%"
            args.extend([needle, needle, needle])
        if ready_before is not None:
            # Отбор «время повтора уже наступило» обязан идти в SQL, а не
            # после LIMIT. Планировщик выбирает окно из 500 заданий; если в
            # статусе retry накопилось больше, окно целиком забивалось
            # неготовыми, и воркеры простаивали, хотя готовые задания были.
            where.append("(queued_at IS NULL OR queued_at<=?)")
            args.append(ready_before)
        allowed_order = {
            "created_at DESC", "created_at ASC", "priority DESC", "priority ASC",
            "media_duration_s DESC", "media_duration_s ASC", "rtf ASC", "rtf DESC",
            "finished_at DESC", "processing_time_s DESC", "updated_at DESC",
            "queued_at ASC", "queued_at DESC", "deadline ASC",
        }
        if order not in allowed_order:
            order = "created_at DESC"
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        columns = self.LIGHT_COLUMNS if light else "*"
        rows = self.query(
            f"SELECT {columns} FROM jobs {clause} ORDER BY {order} LIMIT ? OFFSET ?",
            [*args, limit, offset])
        if light:
            return [dict(r) for r in rows]
        jobs = [_row_to_job(r) for r in rows]
        # Огибающая громкости в списке не нужна никому: на часовой записи это
        # сотни килобайт на задание, а рисуют её только в карточке. Полные
        # данные отдают `get_job` и `/api/jobs/{id}/waveform`.
        for job in jobs:
            job.pop("waveform", None)
        return jobs

    def owner_usage(self, owner: str | list[str], since: float) -> dict[str, float]:
        """Расход владельца (или подразделения) с указанного момента.

        Нужен для квот: сколько заданий поставлено, сколько часов звука
        принято и сколько места заняли исходные файлы. Считается в базе, а
        не перебором в памяти: на сотне тысяч заданий разница между этим и
        выборкой — секунды на каждую загрузку.
        """
        owners = [owner] if isinstance(owner, str) else list(owner)
        if not owners:
            return {"jobs": 0, "audio_hours": 0.0, "storage_gb": 0.0}
        placeholders = ",".join("?" for _ in owners)
        row = self.query_one(
            "SELECT COUNT(*) AS jobs, "
            "       COALESCE(SUM(media_duration_s), 0) AS audio_s, "
            "       COALESCE(SUM(file_size), 0) AS bytes "
            f"FROM jobs WHERE owner IN ({placeholders}) AND created_at>=? "
            "  AND status NOT IN ('cancelled', 'failed')",
            [*owners, since])
        if row is None:
            return {"jobs": 0, "audio_hours": 0.0, "storage_gb": 0.0}
        return {
            "jobs": int(row["jobs"] or 0),
            "audio_hours": round(float(row["audio_s"] or 0) / 3600, 4),
            "storage_gb": round(float(row["bytes"] or 0) / 1024 ** 3, 4),
        }

    def count_jobs(self, *, status: str | list[str] | None = None,
                   owner: str | list[str] | None = None,
                   since: float | None = None) -> int:
        where: list[str] = []
        args: list[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else list(status)
            where.append("status IN (" + ",".join("?" for _ in statuses) + ")")
            args.extend(statuses)
        if owner:
            owners = [owner] if isinstance(owner, str) else list(owner)
            where.append("owner IN (" + ",".join("?" for _ in owners) + ")")
            args.extend(owners)
        if since:
            where.append("created_at>=?")
            args.append(since)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self.query_one(f"SELECT COUNT(*) AS n FROM jobs {clause}", args)
        return int(row["n"]) if row else 0

    def delete_job(self, job_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    def update_job_if_status(self, job_id: str, expected: list[str],
                             *, expected_instance: str | None = None,
                             **fields: Any) -> bool:
        """Обновляет задание, только если его статус входит в ожидаемые.

        Нужно там, где между чтением и записью состояние может измениться:
        отмена и завершение задания идут из разных потоков, и безусловная
        запись помечала бы готовый результат отменённым.

        expected_instance добавляет к проверке владельца. Одного статуса
        мало, когда серверов несколько: экземпляр, застрявший дольше пяти
        минут (своп, ввод-вывод, долгая загрузка весов) и оживший, дописывал
        свой ответ поверх задания, которое уже считает другой сервер.
        Пользователь получал результат от процесса, объявленного мёртвым, а
        работа второго экземпляра выбрасывалась вместе с каталогом выгрузки.

        Returns:
            True, если запись состоялась.
        """
        if not fields:
            return False
        fields["updated_at"] = now()
        if isinstance(fields.get("params"), dict):
            fields["params"] = json.dumps(fields["params"], ensure_ascii=False)
        if isinstance(fields.get("waveform"), (list, dict)):
            fields["waveform"] = json.dumps(fields["waveform"], ensure_ascii=False)
        columns = ", ".join(f"{name}=?" for name in fields)
        placeholders = ",".join("?" for _ in expected)
        where = f"id=? AND status IN ({placeholders})"
        args: list[Any] = [*fields.values(), job_id, *expected]
        if expected_instance is not None:
            where += " AND instance_id=?"
            args.append(expected_instance)
        changed = self.execute(f"UPDATE jobs SET {columns} WHERE {where}", args)
        return bool(changed)

    def find_cached(self, file_hash: str, params_hash: str) -> dict[str, Any] | None:
        row = self.query_one(
            "SELECT * FROM jobs WHERE file_hash=? AND status='completed' "
            "AND json_extract(params, '$._hash')=? ORDER BY finished_at DESC LIMIT 1",
            (file_hash, params_hash))
        return _row_to_job(row) if row else None

    # --- сегменты -------------------------------------------------------

    def save_segments(self, job_id: str, segments: list[dict[str, Any]]) -> None:
        rows = []
        for idx, seg in enumerate(segments):
            rows.append((
                job_id, idx, float(seg.get("start", 0.0)), float(seg.get("end", 0.0)),
                seg.get("text", ""), seg.get("speaker"), seg.get("confidence"),
                seg.get("no_speech_prob"), seg.get("compression_ratio"),
                seg.get("temperature"), seg.get("language"),
                json.dumps(seg.get("words"), ensure_ascii=False) if seg.get("words") else None,
            ))
        with self.write() as conn:
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.executemany(
                "INSERT INTO segments (job_id, idx, start_s, end_s, text, speaker, "
                "confidence, no_speech, compression, temperature, language, words) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def get_segments(self, job_id: str) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM segments WHERE job_id=? ORDER BY idx", (job_id,))
        out = []
        for row in rows:
            seg = dict(row)
            seg["start"] = seg.pop("start_s")
            seg["end"] = seg.pop("end_s")
            if seg.get("words"):
                try:
                    seg["words"] = json.loads(seg["words"])
                except (TypeError, ValueError):
                    seg["words"] = []
            out.append(seg)
        return out

    # --- события --------------------------------------------------------

    def add_event(self, job_id: str | None, kind: str, message: str = "",
                  data: dict[str, Any] | None = None) -> None:
        try:
            self.execute(
                "INSERT INTO events (job_id, ts, kind, message, data) VALUES (?,?,?,?,?)",
                (job_id, now(), kind, message,
                 json.dumps(data, ensure_ascii=False) if data else None))
        except StorageError:
            log.warning("Не удалось записать событие %s для %s", kind, job_id)

    def get_events(self, job_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if job_id:
            rows = self.query(
                "SELECT * FROM events WHERE job_id=? ORDER BY ts DESC LIMIT ?", (job_id, limit))
        else:
            rows = self.query("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,))
        out = []
        for row in rows:
            item = dict(row)
            if item.get("data"):
                try:
                    item["data"] = json.loads(item["data"])
                except (TypeError, ValueError):
                    pass
            out.append(item)
        return out

    # --- метрики --------------------------------------------------------

    def add_metric(self, name: str, value: float, *, job_id: str | None = None,
                   model: str | None = None, engine: str | None = None,
                   labels: dict[str, Any] | None = None) -> None:
        try:
            self.execute(
                "INSERT INTO metrics (ts, name, value, job_id, model, engine, labels) "
                "VALUES (?,?,?,?,?,?,?)",
                (now(), name, float(value), job_id, model, engine,
                 json.dumps(labels, ensure_ascii=False) if labels else None))
        except (StorageError, TypeError, ValueError):
            pass

    def metric_series(self, name: str, since: float, *, model: str | None = None,
                      buckets: int = 60) -> list[dict[str, Any]]:
        args: list[Any] = [name, since]
        extra = ""
        if model:
            extra = " AND model=?"
            args.append(model)
        rows = self.query(
            f"SELECT ts, value FROM metrics WHERE name=? AND ts>=?{extra} ORDER BY ts", args)
        if not rows:
            return []
        first, last = rows[0]["ts"], rows[-1]["ts"]
        span = max(last - first, 1.0)
        width = span / max(buckets, 1)
        acc: dict[int, list[float]] = {}
        for row in rows:
            slot = int((row["ts"] - first) / width)
            acc.setdefault(slot, []).append(row["value"])
        out = []
        for slot in sorted(acc):
            vals = acc[slot]
            out.append({
                "ts": first + slot * width,
                "avg": sum(vals) / len(vals),
                "min": min(vals),
                "max": max(vals),
                "count": len(vals),
            })
        return out

    # --- агрегаты по моделям --------------------------------------------

    def bump_model_stats(self, model: str, engine: str, *, ok: bool,
                         audio_s: float = 0, processing_s: float = 0, words: int = 0,
                         rtf: float | None = None, confidence: float | None = None,
                         wer: float | None = None) -> None:
        with self.write() as conn:
            conn.execute(
                "INSERT INTO model_stats (model, engine) VALUES (?,?) "
                "ON CONFLICT(model) DO NOTHING", (model, engine))
            conn.execute(
                "UPDATE model_stats SET "
                " jobs_total=jobs_total+1,"
                " jobs_ok=jobs_ok+?,"
                " jobs_failed=jobs_failed+?,"
                " audio_seconds=audio_seconds+?,"
                " processing_s=processing_s+?,"
                " words_total=words_total+?,"
                " rtf_sum=rtf_sum+?, rtf_count=rtf_count+?,"
                " confidence_sum=confidence_sum+?, confidence_count=confidence_count+?,"
                " wer_sum=wer_sum+?, wer_count=wer_count+?,"
                " last_used=? "
                "WHERE model=?",
                (1 if ok else 0, 0 if ok else 1, audio_s, processing_s, words,
                 rtf or 0, 1 if rtf is not None else 0,
                 confidence or 0, 1 if confidence is not None else 0,
                 wer or 0, 1 if wer is not None else 0,
                 now(), model))

    def model_stats(self) -> list[dict[str, Any]]:
        rows = self.query("SELECT * FROM model_stats ORDER BY jobs_total DESC")
        out = []
        for row in rows:
            item = dict(row)
            item["rtf_avg"] = (item["rtf_sum"] / item["rtf_count"]) if item["rtf_count"] else None
            item["confidence_avg"] = ((item["confidence_sum"] / item["confidence_count"])
                                      if item["confidence_count"] else None)
            item["wer_avg"] = (item["wer_sum"] / item["wer_count"]) if item["wer_count"] else None
            item["success_rate"] = (item["jobs_ok"] / item["jobs_total"]) if item["jobs_total"] else None
            out.append(item)
        return out

    # --- снимки состояния системы ---------------------------------------

    def add_system_sample(self, sample: dict[str, Any]) -> None:
        try:
            self.execute(
                "INSERT OR REPLACE INTO system_samples "
                "(ts, cpu_percent, ram_used_mb, ram_total_mb, gpu_percent, gpu_mem_mb, "
                " gpu_mem_total, disk_free_gb, queue_depth, active_jobs) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), sample.get("cpu_percent"), sample.get("ram_used_mb"),
                 sample.get("ram_total_mb"), sample.get("gpu_percent"),
                 sample.get("gpu_mem_mb"), sample.get("gpu_mem_total"),
                 sample.get("disk_free_gb"), sample.get("queue_depth"),
                 sample.get("active_jobs")))
        except StorageError:
            pass

    def system_samples(self, since: float, limit: int = 1000) -> list[dict[str, Any]]:
        # Сортировка по убыванию с последующим разворотом: при limit=1 иначе
        # возвращался самый старый замер окна, а не самый свежий.
        rows = self.query(
            "SELECT * FROM system_samples WHERE ts>=? ORDER BY ts DESC LIMIT ?",
            (since, limit))
        return [dict(r) for r in reversed(rows)]

    # --- ключи и настройки ----------------------------------------------

    def set_kv(self, key: str, value: Any) -> None:
        self.execute("INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
                     (key, json.dumps(value, ensure_ascii=False), now()))

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    # --- обслуживание ---------------------------------------------------

    def cleanup(self, *, results_days: int = 30, metrics_days: int = 180,
                events_days: int = 90) -> dict[str, int]:
        removed = {"jobs": 0, "metrics": 0, "events": 0, "samples": 0, "bytes": 0}
        ts = now()
        if results_days > 0:
            cutoff = ts - results_days * 86400
            # Предел на заход обязателен. Без него понижение срока хранения
            # (скажем, с года до месяца) вытаскивало в память сотни тысяч
            # строк, и служебный поток на десятки минут занимал единственную
            # блокировку записи — воркеры вставали на каждом обновлении
            # прогресса. Остаток уберётся следующим заходом через час.
            #
            # Второе условие — про незавершённые. Раньше отбор шёл только по
            # finished_at, поэтому задания в очереди, на паузе и в ожидании
            # повтора не устаревали никогда, и их загруженные файлы лежали в
            # uploads вечно. Им даём срок втрое больше: они могут ждать
            # долго, но не бесконечно.
            stale = self.query(
                "SELECT id, result_path, file_path FROM jobs "
                "WHERE (finished_at IS NOT NULL AND finished_at<?) "
                "   OR (finished_at IS NULL AND created_at<?) "
                "ORDER BY COALESCE(finished_at, created_at) LIMIT ?",
                (cutoff, ts - results_days * 3 * 86400, CLEANUP_BATCH))
            for row in stale:
                # Сначала файлы, потом запись: если удаление файлов упадёт,
                # задание останется в базе и попадёт в следующую уборку.
                removed["bytes"] = removed.get("bytes", 0) + _remove_job_files(dict(row))
                self.delete_job(row["id"])
            removed["jobs"] = len(stale)
            removed["more"] = 1 if len(stale) >= CLEANUP_BATCH else 0
        if metrics_days > 0:
            removed["metrics"] = self.execute(
                "DELETE FROM metrics WHERE ts<?", (ts - metrics_days * 86400,))
            removed["samples"] = self.execute(
                "DELETE FROM system_samples WHERE ts<?", (ts - metrics_days * 86400,))
        if events_days > 0:
            removed["events"] = self.execute(
                "DELETE FROM events WHERE ts<?", (ts - events_days * 86400,))
        return removed

    def vacuum(self) -> None:
        try:
            self.conn.execute("VACUUM")
        except sqlite3.Error as exc:
            log.warning("VACUUM не выполнен: %s", exc)

    def stats(self) -> dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "size_mb": round(size / 1024 / 1024, 2),
            "schema_version": SCHEMA_VERSION,
            "jobs": self.count_jobs(),
            "segments": int(self.query_one("SELECT COUNT(*) n FROM segments")["n"]),
            "metrics": int(self.query_one("SELECT COUNT(*) n FROM metrics")["n"]),
            "events": int(self.query_one("SELECT COUNT(*) n FROM events")["n"]),
        }

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            self._local.conn = None
        self._closed = True


def _remove_job_files(job: dict[str, Any]) -> int:
    """Удаляет каталог результатов и исходник задания. Возвращает объём."""
    import shutil

    freed = 0
    result_path = job.get("result_path")
    if result_path:
        directory = Path(result_path)
        if directory.is_dir():
            try:
                freed += sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
                shutil.rmtree(directory, ignore_errors=True)
            except OSError as exc:
                log.warning("Не удалось удалить результаты %s: %s", directory, exc)
    source = job.get("file_path")
    if source:
        path = Path(source)
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
        except OSError as exc:
            log.warning("Не удалось удалить исходник %s: %s", path, exc)
    return freed


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    for column, empty in (("params", {}), ("waveform", [])):
        raw = job.get(column)
        if isinstance(raw, str):
            try:
                job[column] = json.loads(raw)
            except (TypeError, ValueError):
                job[column] = empty
        elif raw is None and column == "waveform":
            job[column] = []
    return job
