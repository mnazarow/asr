"""Хранилище ASR Hub на SQLite.

Выбор SQLite сознателен: сервер распознавания — не высоконагруженная OLTP-система,
а зависимость от внешней СУБД усложнила бы установку на трёх операционных системах.
Включён режим WAL, что даёт параллельное чтение во время записи.

Все обращения проходят через один пул соединений с блокировкой на запись.
Схема версионируется: при запуске выполняются недостающие миграции.
"""
from __future__ import annotations

import contextlib
import json
import re
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

SCHEMA_VERSION = 18

#: Сколько заданий убирать по сроку хранения за один заход служебного цикла.
CLEANUP_BATCH = 5000

#: Потолок числа корзин у рядов нагрузки. График шире тысячи точек всё равно
#: не читается, а число корзин задаёт стоимость запроса.
SERIES_MAX_BUCKETS = 2000

#: Такт служебного цикла: с этим шагом пишутся замеры нагрузки. Здесь он
#: потому, что от него зависит и запись, и чтение рядов.
SAMPLE_PERIOD_S = 20.0

_CONTENT_SCHEMA = """
    CREATE TABLE IF NOT EXISTS content (
        job_id            TEXT PRIMARY KEY,
        version           INTEGER NOT NULL,
        computed_at       REAL NOT NULL,
        -- Свод: по этим колонкам считаются разрезы по всему архиву, и
        -- держать их отдельно от подробностей обязательно. Разбор целиком
        -- — это килобайты JSON на запись; складывать сто тысяч таких в
        -- память ради средней тональности за месяц нельзя.
        sentiment         REAL,
        sentiment_label   TEXT,
        sentiment_shift   REAL,
        negative_segments INTEGER,
        positive_segments INTEGER,
        wpm               INTEGER,
        silence_share     REAL,
        interruptions     INTEGER,
        pauses            INTEGER,
        longest_pause_s   REAL,
        filler_rate       REAL,
        questions         INTEGER,
        commitments       INTEGER,
        commitments_dated INTEGER,
        alerts            INTEGER,
        compliance        REAL,
        money_max         REAL,
        speakers          INTEGER,
        agent_speaker     TEXT,
        -- Версия 11: стороны разговора и то, что между ними. Доля речи
        -- оператора, его самый долгий монолог против самого долгого
        -- рассказа клиента, пауза перед ответом, соотношение темпов —
        -- показатели, которые считает каждая система речевой аналитики, а
        -- у нас лежали в подробностях и в разрезы не попадали.
        overlap_s         REAL,
        dead_air_s        REAL,
        switches          INTEGER,
        talk_share        REAL,
        monologue_s       REAL,
        customer_story_s  REAL,
        reply_delay_s     REAL,
        tempo_ratio       REAL,
        -- Фрустрация и признаки повторного обращения — по репликам клиента;
        -- нецензурная лексика — NULL, пока словарь выключен: ноль читался
        -- бы как «не ругались», хотя никто и не смотрел.
        frustration       INTEGER,
        repeat_contact    INTEGER,
        profanity         INTEGER,
        profanity_agent   INTEGER,
        -- Версия 12: возражения клиента и сколько из них без отработки
        -- (NULL, когда категорий отработки в наборе нет).
        objections        INTEGER,
        objections_unhandled INTEGER,
        -- Нарушения оператора (сработавших категорий вида «нарушение»),
        -- балл оператора 0–100 и индекс эмпатии −100…+100.
        violations        INTEGER,
        agent_score       REAL,
        empathy           REAL,
        -- Сколько раз оператор обратился к клиенту по имени; NULL без
        -- определённого оператора.
        name_uses         INTEGER,
        -- Подробности: сам разбор целиком, как его показывает карточка.
        detail            TEXT
    )
"""

#: Отметки на записях, которые ставит человек: «разобрано» в очереди
#: коучинга, «эталон — показывать новичкам». Отдельно от разбора: разбор
#: пересчитывается и перезаписывается, а отметка руководителя должна
#: пережить любой пересчёт.
_CONTENT_MARKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS content_marks (
        job_id     TEXT NOT NULL,
        kind       TEXT NOT NULL,
        status     TEXT NOT NULL,
        note       TEXT,
        updated_at REAL NOT NULL,
        PRIMARY KEY (job_id, kind)
    ) WITHOUT ROWID
"""

#: Очередь ручной проверки: какие записи послушать человеку и чем это
#: кончилось. По строке на запись; причина — почему попала (случайная
#: выборка, нижний квартиль по уверенности, вручную). Правка текста
#: становится эталоном, и строка закрывается сама.
_REVIEW_SCHEMA = """
    CREATE TABLE IF NOT EXISTS review_queue (
        job_id     TEXT PRIMARY KEY,
        reason     TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        picked_at  REAL NOT NULL,
        done_at    REAL,
        reviewer   TEXT,
        note       TEXT
    ) WITHOUT ROWID
"""

#: Контрольные прогоны второй моделью: расхождение двух расшифровок одной
#: записи. Точность без эталона это не меряет; меряет согласие — и его
#: ход по дням.
_MODEL_CHECKS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS model_checks (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id        TEXT NOT NULL,
        check_job_id  TEXT NOT NULL,
        model         TEXT,
        control_model TEXT,
        wer           REAL,
        mer           REAL,
        words         INTEGER,
        created_at    REAL NOT NULL
    )
"""

#: Смысловой слой: ответы языковой модели по записи. Отдельно от разбора
#: содержания — тот считается правилами за миллисекунды и пересчитывается
#: при каждой смене словаря, а ответ модели стоит секунды видеокарты и
#: переживает пересчёты. Версия — версия подсказок: сменились подсказки,
#: старые ответы помечаются устаревшими, но не стираются.
_LLM_RESULTS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS llm_results (
        job_id      TEXT PRIMARY KEY,
        version     INTEGER NOT NULL,
        model       TEXT,
        summary     TEXT,
        reason      TEXT,
        reason_quote  TEXT,
        outcome     TEXT,
        outcome_quote TEXT,
        resolved    INTEGER,
        actions     TEXT,
        trackers    TEXT,
        scorecard   TEXT,
        chunks      INTEGER DEFAULT 1,
        calls       INTEGER DEFAULT 0,
        latency_ms  REAL,
        error       TEXT,
        warnings    TEXT,
        created_at  REAL NOT NULL
    ) WITHOUT ROWID
"""

#: Кеш ответов модели по отпечатку подсказки: одна и та же запись с теми
#: же подсказками второй раз модель не спрашивает.
_LLM_CACHE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS llm_cache (
        key         TEXT PRIMARY KEY,
        kind        TEXT,
        model       TEXT,
        response    TEXT,
        latency_ms  REAL,
        created_at  REAL NOT NULL
    ) WITHOUT ROWID
"""

#: Основы слов записи — знаменатель TF-IDF. Отдельная таблица, а не разбор
#: подробностей: чтобы взвесить ключевые слова, нужно знать, в скольких
#: записях основа встречается вообще, и вытаскивать ради этого JSON каждой
#: записи корпуса — это чтение всего архива на каждый отчёт.
#:
#: Пишем не весь словарь записи, а её же кандидатов в ключевые слова
#: (`content.keywords.LIMIT_ОСНОВ` штук). Основа, не попавшая в кандидаты
#: ни в одной записи, никогда и не взвешивается, так что на порядок термов
#: срез не влияет, а таблица меньше на порядок: 60 строк на запись вместо
#: восьмисот.
_CONTENT_TERMS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS content_terms (
        job_id  TEXT NOT NULL,
        stem    TEXT NOT NULL,
        n       INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (job_id, stem)
    ) WITHOUT ROWID
"""

#: Как основу показывать человеку. Отдельная табличка на весь сервер, а не
#: колонка в `content_terms`: форм у основы единицы, а строк с этой основой —
#: столько же, сколько записей, где она встретилась. Форма в строке значила
#: бы миллионы копий слова «поставки» и, что хуже, поиск самой частой формы
#: перебором всех этих строк: свод тем на архиве в сто тысяч записей уходил
#: из-за него с полусекунды на три.
#:
#: Счётчик здесь — «сколько раз форма встречалась когда-либо». При удалении
#: заданий он не уменьшается, и это сознательно: он выбирает подпись, а не
#: считает статистику, и подпись от устаревшего счётчика не портится.
_CONTENT_VOCAB_SCHEMA = """
    CREATE TABLE IF NOT EXISTS content_vocab (
        stem    TEXT NOT NULL,
        word    TEXT NOT NULL,
        n       INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (stem, word)
    ) WITHOUT ROWID
"""

#: Совпадения категорий: по строке на пару «запись — категория». Отдельная
#: таблица, а не колонка со списком в `content`: свод «сколько записей про
#: оплату за неделю и сколько было на прошлой» — это группировка по
#: категории, а группировать по списку в строке база не умеет. Вид
#: категории хранится рядом: правила меняются, а «нарушение», найденное
#: месяц назад, должно остаться нарушением в отчёте за тот месяц.
_CONTENT_HITS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS content_hits (
        job_id   TEXT NOT NULL,
        category TEXT NOT NULL,
        kind     TEXT NOT NULL DEFAULT 'topic',
        count    INTEGER NOT NULL DEFAULT 1,
        first_s  REAL,
        PRIMARY KEY (job_id, category)
    ) WITHOUT ROWID
"""

_CONTENT_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_content_hits_category ON content_hits(category, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_content_sentiment ON content(sentiment)",
    "CREATE INDEX IF NOT EXISTS idx_content_alerts ON content(alerts)",
    "CREATE INDEX IF NOT EXISTS idx_content_version ON content(version)",
    # Указатель по основам — покрывающий: в него входит и `n`, а первичный
    # ключ таблицы (job_id, stem) SQLite кладёт в указатель сам, потому что
    # таблица WITHOUT ROWID. В итоге запрос тем не заглядывает в таблицу ни
    # разу. С указателем по одной колонке `SUM(n)` тянул строку за строкой,
    # и свод тем по двум миллионам основ занимал две секунды вместо
    # полутора десятых.
    "DROP INDEX IF EXISTS idx_content_terms_stem",
    "CREATE INDEX IF NOT EXISTS idx_content_terms_group ON content_terms(stem, n)",
)

#: Полнотекстовый указатель по репликам. Живёт отдельно от `_SCHEMA`, и это
#: не украшение: FTS5 — необязательный модуль SQLite, и если он в сборке не
#: собран, `CREATE VIRTUAL TABLE` откатит всю миграцию и сервер не поднимется
#: вовсе. Поиск без указателя работает и так, только медленнее, — а сервер,
#: который не стартует, не работает никак.
#:
#: Указатель внешний (`content='segments'`): текст реплик не дублируется, в
#: базе лежит только сам индекс. Синхронизацию держат триггеры, а не код:
#: реплики удаляются из четырёх мест (замена результата, удаление задания,
#: уборка по сроку, каскад при повторе), и любое пятое, забывшее про
#: указатель, оставило бы в поиске записи об удалённых разговорах.
#:
#: `remove_diacritics 2` здесь работает на русский: «ещё» и «еще» становятся
#: одним словом, и человеку не приходится угадывать, как набрано в
#: расшифровке.
_FTS_SCHEMA = [
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5(
        text,
        content='segments',
        content_rowid='rowid',
        tokenize='unicode61 remove_diacritics 2'
    )
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_ai AFTER INSERT ON segments BEGIN
        INSERT INTO segments_fts(rowid, text) VALUES (new.rowid, new.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_ad AFTER DELETE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, text)
        VALUES ('delete', old.rowid, old.text);
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS segments_fts_au AFTER UPDATE ON segments BEGIN
        INSERT INTO segments_fts(segments_fts, rowid, text)
        VALUES ('delete', old.rowid, old.text);
        INSERT INTO segments_fts(rowid, text) VALUES (new.rowid, new.text);
    END
    """,
]

_SCHEMA = [
    # --- версия 10: разбор содержания записей ---------------------------
    _CONTENT_SCHEMA,
    _CONTENT_TERMS_SCHEMA,
    _CONTENT_VOCAB_SCHEMA,
    # --- версия 12: совпадения категорий обращений ----------------------
    _CONTENT_HITS_SCHEMA,
    # --- версия 13: отметки коучинга и эталонов -------------------------
    _CONTENT_MARKS_SCHEMA,
    # --- версия 16: очередь ручной проверки и контрольные прогоны --------
    _REVIEW_SCHEMA,
    _MODEL_CHECKS_SCHEMA,
    # --- версия 17: смысловой слой языковой модели ------------------------
    _LLM_RESULTS_SCHEMA,
    _LLM_CACHE_SCHEMA,
    "CREATE INDEX IF NOT EXISTS idx_llm_results_created ON llm_results(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_llm_results_outcome ON llm_results(outcome)",
    "CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status, picked_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_model_checks_created ON model_checks(created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_model_checks_job ON model_checks(job_id)",
    *_CONTENT_INDEXES,
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
        peak_memory_jobs  INTEGER,
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
        heartbeat_at      REAL,
        -- Версия 11: здоровье распознавания. Сколько сегментов выглядят
        -- подозрительно (галлюцинации Whisper, невозможный темп, повторы,
        -- известные фразы), какие признаки у записи и примеры для карточки.
        suspect_segments  INTEGER,
        suspect_share     REAL,
        quality_flags     TEXT,
        quality_detail    TEXT,
        -- Версия 14: точность по эталону подробнее одного WER. Счётчики
        -- ошибок нужны, чтобы складывать срезы по словам, а не усреднять
        -- доли; калибровка — корзины уверенности против верности слов.
        mer               REAL,
        wil               REAL,
        ref_words         INTEGER,
        sub_words         INTEGER,
        del_words         INTEGER,
        ins_words         INTEGER,
        calibration       TEXT,
        -- Версия 15: профиль звука на входе. Объясняет ошибки лучше всего
        -- остального: WER растёт с падением SNR предсказуемо.
        snr_db            REAL,
        peak_dbfs         REAL,
        clipping_share    REAL,
        loudness_lufs     REAL,
        silence_share     REAL
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
    # --- версия 6: вход по логину и паролю --------------------------------
    #
    # Ключи доступа остаются как были — они для программ. Учётные записи
    # нужны людям: чтобы открыть интерфейс, не надо заходить на сервер и
    # читать api-key.txt.
    #
    # «group» — слово, занятое в SQL, поэтому колонка называется user_group.
    """
    CREATE TABLE IF NOT EXISTS users (
        id              TEXT PRIMARY KEY,
        username        TEXT NOT NULL UNIQUE,
        password_hash   TEXT NOT NULL,
        display_name    TEXT DEFAULT '',
        role            TEXT DEFAULT 'user',
        user_group      TEXT DEFAULT '',
        enabled         INTEGER DEFAULT 1,
        must_change     INTEGER DEFAULT 0,
        created_at      REAL,
        updated_at      REAL,
        last_login      REAL,
        failed_attempts INTEGER DEFAULT 0,
        locked_until    REAL DEFAULT 0
    )
    """,
    # Хранится не токен, а его sha256: утёкшая база не даёт войти.
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash  TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  REAL,
        expires_at  REAL,
        last_seen   REAL,
        user_agent  TEXT DEFAULT '',
        address     TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)",
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
    # --- версия 7: замеры по каждой видеокарте отдельно --------------------
    #
    # В system_samples под видеокарту отведено три колонки — то есть ровно
    # одна карта. Сборщик и брал только первую строку вывода nvidia-smi, так
    # что вторая карта не существовала для сервера вовсе. Температуры и
    # потребления там нет совсем, хотя они приходят тем же запросом даром, а
    # без них «нагрузка на видеокарту» — это загрузка в процентах и больше
    # ничего: ни троттлинга, ни упора в лимит мощности по ним не видно.
    """
    CREATE TABLE IF NOT EXISTS gpu_samples (
        ts             REAL NOT NULL,
        gpu            INTEGER NOT NULL,
        name           TEXT,
        util_percent   REAL,
        mem_used_mb    REAL,
        mem_total_mb   REAL,
        temperature_c  REAL,
        power_w        REAL,
        power_limit_w  REAL,
        PRIMARY KEY (ts, gpu)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gpu_samples_ts ON gpu_samples(ts DESC)",

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
    "users": {
        "id": "TEXT",
        "username": "TEXT",
        "password_hash": "TEXT",
        "display_name": "TEXT DEFAULT ''",
        "role": "TEXT DEFAULT 'user'",
        "user_group": "TEXT DEFAULT ''",
        "enabled": "INTEGER DEFAULT 1",
        "must_change": "INTEGER DEFAULT 0",
        "created_at": "REAL",
        "updated_at": "REAL",
        "last_login": "REAL",
        "failed_attempts": "INTEGER DEFAULT 0",
        "locked_until": "REAL DEFAULT 0",
    },
    "sessions": {
        "token_hash": "TEXT",
        "user_id": "TEXT",
        "created_at": "REAL",
        "expires_at": "REAL",
        "last_seen": "REAL",
        "user_agent": "TEXT DEFAULT ''",
        "address": "TEXT DEFAULT ''",
    },
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
        "suspect_segments": "INTEGER",
        "suspect_share": "REAL",
        "quality_flags": "TEXT",
        "quality_detail": "TEXT",
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
        # Сколько заданий шло разом, когда снимался пик. Без этого числа
        # сам пик не отвечает на вопрос, ради которого его смотрят.
        "peak_memory_jobs": "INTEGER",
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
        "mer": "REAL",
        "wil": "REAL",
        "ref_words": "INTEGER",
        "sub_words": "INTEGER",
        "del_words": "INTEGER",
        "ins_words": "INTEGER",
        "calibration": "TEXT",
        "snr_db": "REAL",
        "peak_dbfs": "REAL",
        "clipping_share": "REAL",
        "loudness_lufs": "REAL",
        "silence_share": "REAL",
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
    # Таблица разбора появилась целиком в десятой версии, и дописывать ей
    # колонки было незачем — до одиннадцатой. Перечень полный, а не только
    # новые: так следующая колонка не потребует вспоминать, как это делается.
    "content": {
        "job_id": "TEXT",
        "version": "INTEGER",
        "computed_at": "REAL",
        "sentiment": "REAL",
        "sentiment_label": "TEXT",
        "sentiment_shift": "REAL",
        "negative_segments": "INTEGER",
        "positive_segments": "INTEGER",
        "wpm": "INTEGER",
        "silence_share": "REAL",
        "interruptions": "INTEGER",
        "pauses": "INTEGER",
        "longest_pause_s": "REAL",
        "filler_rate": "REAL",
        "questions": "INTEGER",
        "commitments": "INTEGER",
        "commitments_dated": "INTEGER",
        "alerts": "INTEGER",
        "compliance": "REAL",
        "money_max": "REAL",
        "speakers": "INTEGER",
        "agent_speaker": "TEXT",
        "overlap_s": "REAL",
        "dead_air_s": "REAL",
        "switches": "INTEGER",
        "talk_share": "REAL",
        "monologue_s": "REAL",
        "customer_story_s": "REAL",
        "reply_delay_s": "REAL",
        "tempo_ratio": "REAL",
        "frustration": "INTEGER",
        "repeat_contact": "INTEGER",
        "profanity": "INTEGER",
        "profanity_agent": "INTEGER",
        "objections": "INTEGER",
        "objections_unhandled": "INTEGER",
        "violations": "INTEGER",
        "agent_score": "REAL",
        "empathy": "REAL",
        "name_uses": "INTEGER",
        "detail": "TEXT",
    },
    "content_marks": {
        "job_id": "TEXT",
        "kind": "TEXT",
        "status": "TEXT",
        "note": "TEXT",
        "updated_at": "REAL",
    },
    "review_queue": {
        "job_id": "TEXT",
        "reason": "TEXT",
        "status": "TEXT DEFAULT 'pending'",
        "picked_at": "REAL",
        "done_at": "REAL",
        "reviewer": "TEXT",
        "note": "TEXT",
    },
    "llm_results": {
        "job_id": "TEXT", "version": "INTEGER", "model": "TEXT", "summary": "TEXT",
        "reason": "TEXT", "reason_quote": "TEXT", "outcome": "TEXT",
        "outcome_quote": "TEXT", "resolved": "INTEGER", "actions": "TEXT",
        "trackers": "TEXT", "scorecard": "TEXT", "chunks": "INTEGER DEFAULT 1",
        "calls": "INTEGER DEFAULT 0", "latency_ms": "REAL", "error": "TEXT",
        "warnings": "TEXT", "created_at": "REAL",
    },
    "llm_cache": {
        "key": "TEXT", "kind": "TEXT", "model": "TEXT", "response": "TEXT",
        "latency_ms": "REAL", "created_at": "REAL",
    },
    "model_checks": {
        "id": "INTEGER",
        "job_id": "TEXT",
        "check_job_id": "TEXT",
        "model": "TEXT",
        "control_model": "TEXT",
        "wer": "REAL",
        "mer": "REAL",
        "words": "INTEGER",
        "created_at": "REAL",
    },
    "content_hits": {
        "job_id": "TEXT",
        "category": "TEXT",
        "kind": "TEXT DEFAULT 'topic'",
        "count": "INTEGER DEFAULT 1",
        "first_s": "REAL",
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

def fts_query(text: str) -> str:
    """Превращает набранное человеком в запрос к указателю.

    Отдавать пользовательскую строку в MATCH напрямую нельзя: у FTS5 свой
    язык запросов, и одинокая кавычка, звёздочка или слово AND — это не
    поиск, а синтаксическая ошибка прямо в лицо человеку, который просто
    искал «договор». Поэтому берём из строки слова, каждое заключаем в
    кавычки как отдельное слово запроса, а последнему разрешаем
    продолжение: набранное «догов» находит «договор», и поиск работает по
    ходу набора, а не только после последней буквы.

    Пустая строка на выходе означает «искать нечего» — вызывающий тогда
    просто не ставит условия.
    """
    слова = re.findall(r"[^\W_]+", text or "", flags=re.UNICODE)
    if not слова:
        return ""
    # Больше десятка слов в запросе — это уже не поиск, а вставленный абзац;
    # каждое слово стоит времени, а пользы за пределами первых нет.
    слова = слова[:12]
    части = [f'"{w}"' for w in слова[:-1]]
    части.append(f'"{слова[-1]}"*')
    return " ".join(части)


class Database:
    """Тонкая обёртка над SQLite с пулом соединений по потокам."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._closed = False
        #: Есть ли полнотекстовый указатель. False — сборка SQLite без FTS5;
        #: поиск тогда работает перебором, как раньше.
        self.fts_ready = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()
        self._setup_fts()

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
            # Номер версии — вещь, которую забывают поднять. Забыли — и
            # новая колонка не появляется, а каждый запрос к таблице падает
            # с «no such column» на боевом сервере. Проверка дешёвая
            # (`PRAGMA table_info` по нескольким таблицам), поэтому идёт
            # всегда, а не только при смене номера: недостающая колонка
            # находится и молча дописывается.
            self._catch_up_columns()
            self._catch_up_indexes()
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

    def _setup_fts(self) -> None:
        """Заводит полнотекстовый указатель — если сборка SQLite его умеет.

        Отдельно от общих миграций и в своей транзакции. FTS5 — модуль
        необязательный: на сборке без него `CREATE VIRTUAL TABLE` откатил бы
        всю миграцию, и сервер не поднялся бы вовсе. Поиск без указателя
        работает и так, только перебором, — а сервер, который не стартует,
        не работает никак.

        Наполнение идёт один раз: на уже накопленном архиве указателя ещё
        нет, и без пересборки поиск не нашёл бы ни одного старого разговора.

        Пустоту видно по теневой таблице `_docsize` — по строке на
        проиндексированную реплику. Ни счётчик самого указателя, ни размер
        `_data` для этого не годятся: у внешнего указателя
        `SELECT COUNT(*) FROM segments_fts` считает строки таблицы-источника
        и равен ему всегда, даже когда в указателе нет ничего. На этом и
        попалась первая версия проверки — сервер уверенно решал, что архив
        уже проиндексирован, и поиск по нему не находил ничего.
        """
        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in _FTS_SCHEMA:
                    conn.execute(statement)
                реплик = conn.execute(
                    "SELECT COUNT(*) FROM segments").fetchone()[0]
                в_указателе = conn.execute(
                    "SELECT COUNT(*) FROM segments_fts_docsize").fetchone()[0]
                if реплик and not в_указателе:
                    log.info("Сборка поискового указателя по %d репликам…", реплик)
                    начало = time.time()
                    conn.execute(
                        "INSERT INTO segments_fts(segments_fts) VALUES('rebuild')")
                    log.info("Поисковый указатель собран за %.1f с",
                             time.time() - начало)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                log.warning(
                    "Полнотекстовый поиск недоступен (%s). Поиск по расшифровкам "
                    "будет идти перебором: на большом архиве это заметно "
                    "медленнее. Обычная причина — сборка SQLite без модуля FTS5.",
                    exc)
                return
        self.fts_ready = True

    def _catch_up_columns(self) -> None:
        """Дописывает колонки на базе, у которой номер версии уже нынешний."""
        if not self._columns_differ():
            return
        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._add_missing_columns(conn)
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise StorageError(
                    f"Не удалось дописать колонки: {exc}") from exc

    def _catch_up_indexes(self) -> None:
        """Досоздаёт указатели на базе, у которой номер версии уже нынешний.

        Та же беда, что с колонками, только тише. Забыли поднять номер
        версии — и нового указателя нет; база при этом работает, ничего не
        падает, просто свод тем считается две секунды вместо полутора
        десятых, а поиск задания идёт перебором. Такое замечают через
        неделю и ищут в совсем другом месте.

        Проверка идёт по списку имён — `PRAGMA index_list` по нескольким
        таблицам, доли миллисекунды. Отсутствующий указатель создаётся,
        существующий не трогается: `CREATE INDEX IF NOT EXISTS` на месте
        ничего не переделывает.
        """
        ожидаются = {}
        for statement in _SCHEMA:
            подготовка = statement.strip()
            if not подготовка.upper().startswith("CREATE INDEX"):
                continue
            имя = подготовка.split(" ON ")[0].split()[-1]
            ожидаются[имя] = подготовка
        if not ожидаются:
            return
        try:
            есть = {row[0] for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'")}
        except sqlite3.Error:
            return
        нехватка = [ожидаются[и] for и in ожидаются if и not in есть]
        if not нехватка:
            return
        with self._write_lock:
            conn = self.conn
            for statement in нехватка:
                try:
                    conn.execute(statement)
                except sqlite3.Error as exc:
                    # Указатель по колонке, которой в этой базе ещё нет, —
                    # не повод не подниматься: без него медленнее, и только.
                    log.warning("Не удалось создать указатель: %s", exc)
                else:
                    log.info("Миграция: создан указатель «%s»",
                             statement.split(" ON ")[0].split()[-1])

    def _columns_differ(self) -> bool:
        """Есть ли в схеме колонки, которых нет в базе."""
        for table, columns in _EXPECTED_COLUMNS.items():
            try:
                existing = {row[1] for row in
                            self.conn.execute(f"PRAGMA table_info({table})")}
            except sqlite3.Error:
                continue
            if existing and not set(columns) <= existing:
                return True
        return False

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
        _serialize_json_fields(fields)
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
        "device, file_size, progress, stage, "
        # Разрезы аналитики, добавленные позже: метки (единственный разрез,
        # который задаёт сам пользователь), пик памяти, хеш файла для учёта
        # повторов, кто отменил и чем кончилось уведомление. Каждое поле
        # молча приходило как None, и разделы показывали пустоту — тот самый
        # случай, о котором предупреждает абзац выше.
        "tags, peak_memory_mb, peak_memory_jobs, file_hash, cancelled_by, "
        "webhook_status, "
        # Здоровье распознавания: число и доля подозрительных сегментов и
        # перечень признаков. Подробности (примеры с временем) — нет: они
        # нужны только карточке.
        "suspect_segments, suspect_share, quality_flags, "
        # Точность по эталону: счётчики ошибок для срезов, складываемых по
        # словам, и корзины калибровки. Есть только у заданий с эталоном —
        # у остальных это NULL, и список они не утяжеляют.
        "mer, wil, ref_words, sub_words, del_words, ins_words, calibration, "
        # Профиль звука: пять чисел, по которым строится разрез «SNR →
        # уверенность и WER» и отбор «плохой звук».
        "snr_db, peak_dbfs, clipping_share, loudness_lufs, silence_share"
    )

    #: Отборы по содержанию разговора для списка заданий. Ключ приходит из
    #: строки запроса, выражение берётся отсюда и никогда из неё: перечень
    #: закрытый, неизвестный ключ отдаёт пустой список.
    #:
    #: Те же вопросы, что в разделе «Аналитика записей», но здесь они
    #: сочетаются с поиском по словам и с отбором по владельцу — то есть
    #: отвечают на «покажи отрицательные разговоры про возврат за неделю»,
    #: чего раздел сам по себе не умеет.
    CONTENT_FILTERS: dict[str, str] = {
        "negative": "c.sentiment < -0.15",
        "positive": "c.sentiment > 0.15",
        "downturn": "c.sentiment_shift < -0.2",
        "recovered": "c.sentiment_shift > 0.2",
        "alerts": "COALESCE(c.alerts,0) > 0",
        "open_commitments": "c.commitments > COALESCE(c.commitments_dated,0)",
        "interruptions": "COALESCE(c.interruptions,0) > 2",
        "silence": "c.silence_share > 0.3",
        "script_failed": "c.compliance IS NOT NULL AND c.compliance < 0.5",
        "money": "c.money_max IS NOT NULL",
        "monologue": "c.monologue_s >= 150",
        "mixed": "c.sentiment_label = 'смешанная'",
        "dead_air": "c.dead_air_s >= 30",
        "frustrated": "COALESCE(c.frustration,0) > 0",
        "repeat": "COALESCE(c.repeat_contact,0) > 0",
        "profanity": "COALESCE(c.profanity,0) > 0",
        "profanity_agent": "COALESCE(c.profanity_agent,0) > 0",
        "objection_unhandled": "COALESCE(c.objections_unhandled,0) > 0",
        "objection": "COALESCE(c.objections,0) > 0",
        "violation": "COALESCE(c.violations,0) > 0",
        "low_score": "c.agent_score IS NOT NULL AND c.agent_score < 60",
        "impolite": "c.empathy IS NOT NULL AND c.empathy < 0",
    }

    #: Отборы по самому заданию, без соединения с разбором содержания:
    #: подозрительные расшифровки есть и там, где разбор выключен.
    JOB_FILTERS: dict[str, str] = {
        "suspect": ("(COALESCE(jobs.suspect_segments,0) > 0 "
                    "OR COALESCE(jobs.quality_flags,'') <> '')"),
        "speakers_mismatch": "jobs.quality_flags LIKE '%speakers%'",
        "hallucination": ("(jobs.quality_flags LIKE '%phrase%' "
                          "OR jobs.quality_flags LIKE '%repeat%' "
                          "OR jobs.quality_flags LIKE '%compression%')"),
        # Плохой звук — по порогам Deepgram: SNR ниже 10 дБ или клиппинг от
        # одного процента отсчётов. Пороги те же, что в audio_profile.
        "bad_audio": "(jobs.snr_db < 10 OR jobs.clipping_share >= 0.01)",
        "noisy": "jobs.snr_db < 10",
        "clipped": "jobs.clipping_share >= 0.01",
        # По ответу языковой модели: вопрос не решён, есть договорённости.
        "llm_unresolved": "jobs.id IN (SELECT job_id FROM llm_results WHERE resolved = 0)",
        "llm_actions": ("jobs.id IN (SELECT job_id FROM llm_results "
                        "WHERE actions IS NOT NULL AND actions <> '[]')"),
    }

    #: Приставка отбора по категории обращения: `category:payment`. Имя
    #: категории — значение из настроек, а не выражение, и в запрос оно
    #: попадает только параметром.
    CATEGORY_FILTER = "category:"

    #: Отборы с приставкой и значением: категория обращения, исход и
    #: причина из ответа языковой модели. Перечень нужен и здесь, и в
    #: обработчике списка заданий: он проверяет отбор до запроса, и без
    #: общего перечня новый отбор работал бы в базе и отвергался ручкой.
    PREFIX_FILTERS: tuple[str, ...] = ("category:", "outcome:", "reason:")

    def _jobs_where(self, *, status: str | list[str] | None = None,
                    owner: str | list[str] | None = None,
                    model: str | None = None, search: str | None = None,
                    group_id: str | None = None, since: float | None = None,
                    content: str | None = None,
                    ) -> tuple[str, list[str], list[Any]] | None:
        """Условия отбора заданий — общие для списка и для счётчика.

        Раньше их было две копии, и они разошлись: список учитывал поиск,
        модель и группу, а счётчик — только статус и владельца. На экране
        это выглядело как «показано 30, всего 4000», и листалка уводила на
        пустые страницы. Одна сборка на двоих — единственный способ, при
        котором это не разойдётся снова.

        Возвращает None, когда отбор заведомо пуст (неизвестный ключ
        содержания): вызывающий отдаёт пустой ответ, не ходя в базу.
        """
        where: list[str] = []
        args: list[Any] = []
        соединение = ""
        if content in self.JOB_FILTERS:
            where.append(self.JOB_FILTERS[content])
        elif content and content.startswith(self.CATEGORY_FILTER):
            where.append("EXISTS (SELECT 1 FROM content_hits h "
                         "WHERE h.job_id = jobs.id AND h.category = ?)")
            args.append(content[len(self.CATEGORY_FILTER):])
        elif content and content.startswith(("outcome:", "reason:")):
            # Исход и причина из ответа языковой модели — значение из
            # списка настроек, в запрос попадает только параметром.
            вид, значение = content.split(":", 1)
            условие, параметры = self.llm_outcome_filter(вид, значение)
            where.append(условие)
            args.extend(параметры)
        elif content:
            # Соединение с разбором добавляется, только когда отбор задан:
            # список заданий открывают чаще всего, и лишнее соединение
            # стоило бы времени каждому, кто им не пользуется.
            условие = self.CONTENT_FILTERS.get(content)
            if условие is None:
                return None
            соединение = " JOIN content c ON c.job_id = jobs.id"
            where.append(условие)
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
            # Расшифровку ищет указатель, имя файла и номер — перебором.
            # Перебор здесь дёшев: обе колонки короткие и лежат в начале
            # записи, поэтому строку не приходится читать целиком. Дорого
            # было именно `text LIKE '%…%'` — оно поднимало с диска все
            # расшифровки подряд, и редкое слово (то есть ровно тот запрос,
            # ради которого поиском и пользуются) читало таблицу насквозь.
            найденные = self.jobs_matching(search) if self.fts_ready else None
            needle = f"%{search}%"
            if найденные is None:
                where.append("(filename LIKE ? OR text LIKE ? OR id LIKE ?)")
                args.extend([needle, needle, needle])
            elif найденные:
                места = ",".join("?" for _ in найденные)
                where.append(
                    f"(id IN ({места}) OR filename LIKE ? OR id LIKE ?)")
                args.extend([*найденные, needle, needle])
            else:
                where.append("(filename LIKE ? OR id LIKE ?)")
                args.extend([needle, needle])
        return соединение, where, args

    def list_jobs(self, *, status: str | list[str] | None = None,
                  owner: str | list[str] | None = None,
                  model: str | None = None, search: str | None = None,
                  group_id: str | None = None, since: float | None = None,
                  limit: int = 100, offset: int = 0,
                  order: str = "created_at DESC",
                  light: bool = False,
                  content: str | None = None,
                  ready_before: float | None = None) -> list[dict[str, Any]]:
        собрано = self._jobs_where(status=status, owner=owner, model=model,
                                   search=search, group_id=group_id, since=since,
                                   content=content)
        if собрано is None:
            return []
        соединение, where, args = собрано
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
        # Колонки с явным именем таблицы: при соединении с разбором голая
        # звёздочка притащила бы и его колонки, а `_row_to_job` разобрал бы
        # их как поля задания.
        if light:
            columns = (", ".join(f"jobs.{к.strip()}"
                                 for к in self.LIGHT_COLUMNS.split(","))
                       if соединение else self.LIGHT_COLUMNS)
        else:
            columns = "jobs.*" if соединение else "*"
        порядок = (f"jobs.{order}" if соединение and not order.startswith("jobs.")
                   else order)
        rows = self.query(
            f"SELECT {columns} FROM jobs{соединение} {clause} "
            f"ORDER BY {порядок} LIMIT ? OFFSET ?",
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

    def jobs_without_tag(self, tag: str, *, older_than: float,
                         limit: int = 5000) -> list[str]:
        """Завершённые задания старше указанного момента без данной метки.

        Метки лежат строкой через запятую, поэтому сравнение — по
        обрамлённой запятыми строке: иначе метка «согласие» находилась бы и
        в «несогласие».
        """
        rows = self.query(
            "SELECT id FROM jobs WHERE status='completed' AND created_at < ? "
            "  AND (',' || COALESCE(tags,'') || ',') NOT LIKE ? "
            "ORDER BY created_at LIMIT ?",
            (older_than, f"%,{tag},%", limit))
        return [str(r["id"]) for r in rows]

    #: Сколько разговоров максимум приносит один поиск. Список на экране
    #: всё равно листается страницами, а перечень номеров уезжает в SQL
    #: условием `id IN (...)`, у которого есть свой предел на число
    #: параметров.
    SEARCH_LIMIT = 400

    def jobs_matching(self, search: str, limit: int = 0) -> list[str]:
        """Номера заданий, в расшифровках которых встретилось искомое."""
        запрос = fts_query(search)
        if not запрос or not self.fts_ready:
            return []
        try:
            rows = self.query(
                "SELECT DISTINCT s.job_id FROM segments_fts f "
                "JOIN segments s ON s.rowid = f.rowid "
                "WHERE f.text MATCH ? LIMIT ?",
                (запрос, limit or self.SEARCH_LIMIT))
        except StorageError as exc:
            log.warning("Поиск по указателю не удался (%s) — идём перебором", exc)
            return []
        return [str(r["job_id"]) for r in rows]

    def search_segments(self, search: str, *, job_id: str = "",
                        job_ids: Sequence[str] = (),
                        limit: int = 200) -> list[dict[str, Any]]:
        """Найденные реплики: где сказано, на какой секунде и что вокруг.

        Список заданий отвечает «нашлось в этом разговоре» и на этом
        замолкает — дальше человек открывал карточку и искал глазами.
        Здесь возвращается сама фраза с обрамлением и её время, так что из
        результата поиска можно сразу включить запись с нужного места.
        """
        запрос = fts_query(search)
        if not запрос or not self.fts_ready:
            return []
        условие = "f.text MATCH ?"
        args: list[Any] = [запрос]
        if job_id:
            условие += " AND s.job_id = ?"
            args.append(job_id)
        elif job_ids:
            места = ",".join("?" for _ in job_ids)
            условие += f" AND s.job_id IN ({места})"
            args.extend(job_ids)
        try:
            rows = self.query(
                "SELECT s.job_id, s.idx, s.start_s, s.end_s, s.speaker, s.text, "
                "       snippet(segments_fts, 0, '\u2039', '\u203a', '…', 12) AS snippet "
                "FROM segments_fts f JOIN segments s ON s.rowid = f.rowid "
                f"WHERE {условие} ORDER BY s.job_id, s.idx LIMIT ?",
                (*args, limit))
        except StorageError as exc:
            log.warning("Поиск по репликам не удался: %s", exc)
            return []
        return [dict(r) for r in rows]

    def best_snippets(self, search: str, job_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        """По одной лучшей находке на каждое задание — для списка результатов."""
        if not job_ids or not self.fts_ready:
            return {}
        лучшее: dict[str, dict[str, Any]] = {}
        # Отбор сразу по нужным заданиям, а не фильтром после. Общий поиск с
        # отсевом выглядит тем же самым, но предел выборки съедали чужие
        # совпадения: на оживлённом архиве фраза пропадала из строки тем
        # чаще, чем активнее соседи, — и без всякой видимой причины.
        for реплика in self.search_segments(
                search, job_ids=list(job_ids), limit=len(job_ids) * 8 + 50):
            лучшее.setdefault(str(реплика["job_id"]), реплика)
        return лучшее

    # --- разбор содержания записей ---------------------------------------

    #: Колонки свода: по ним считаются разрезы по всему архиву. Подробности
    #: (`detail`) сюда не входят намеренно — это килобайты на запись.
    CONTENT_COLUMNS = (
        "job_id, version, computed_at, sentiment, sentiment_label, "
        "sentiment_shift, negative_segments, positive_segments, wpm, "
        "silence_share, interruptions, pauses, longest_pause_s, filler_rate, "
        "questions, commitments, commitments_dated, alerts, compliance, "
        "money_max, speakers, agent_speaker, overlap_s, dead_air_s, switches, "
        "talk_share, monologue_s, customer_story_s, reply_delay_s, tempo_ratio, "
        "frustration, repeat_contact, profanity, profanity_agent, objections, "
        "objections_unhandled, violations, agent_score, empathy, name_uses"
    )

    def save_content(self, job_id: str, features: dict[str, Any],
                     terms: list[dict[str, Any]] | None = None) -> None:
        """Кладёт разбор записи: свод колонками, подробности одним полем.

        Основы (`terms`) пишутся в ту же запись базы, что и свод. Раздельно
        было нельзя: при пересчёте между двумя записями оставался момент,
        когда свод уже новый, а основы ещё старые, и отчёт, попавший в эту
        щель, взвешивал ключевые слова по несуществующему корпусу.
        """
        поля = {"job_id": job_id, "computed_at": now(), **features}
        детали = поля.pop("detail", None)
        поля["detail"] = (json.dumps(детали, ensure_ascii=False)
                          if isinstance(детали, (dict, list)) else детали)
        # Совпадения категорий — строки своей таблицы, а не колонка; едут
        # в той же записи базы, что и свод, по той же причине, что и
        # основы: разбор, попавший в щель между двумя записями, видел бы
        # новую тональность со старыми категориями.
        совпадения = поля.pop("hits", None)
        имена = ", ".join(поля)
        места = ",".join("?" for _ in поля)
        with self.write() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO content ({имена}) VALUES ({места})",
                list(поля.values()))
            if совпадения is not None:
                conn.execute("DELETE FROM content_hits WHERE job_id=?", (job_id,))
                conn.executemany(
                    "INSERT OR REPLACE INTO content_hits "
                    "(job_id, category, kind, count, first_s) VALUES (?,?,?,?,?)",
                    [(job_id, str(с["category"]), str(с.get("kind") or "topic"),
                      int(с.get("count") or 1), с.get("first_s"))
                     for с in совпадения if с.get("category")])
            if terms is not None:
                годные = [т for т in terms if т.get("stem")]
                conn.execute("DELETE FROM content_terms WHERE job_id=?", (job_id,))
                conn.executemany(
                    "INSERT OR REPLACE INTO content_terms (job_id, stem, n) "
                    "VALUES (?,?,?)",
                    [(job_id, т["stem"], int(т.get("n") or 1)) for т in годные])
                conn.executemany(
                    "INSERT INTO content_vocab (stem, word, n) VALUES (?,?,1) "
                    "ON CONFLICT(stem, word) DO UPDATE SET n = n + 1",
                    [(т["stem"], т.get("word") or т["stem"]) for т in годные])

    def get_content(self, job_id: str) -> dict[str, Any] | None:
        """Разбор одной записи вместе с подробностями."""
        row = self.query_one("SELECT * FROM content WHERE job_id=?", (job_id,))
        if row is None:
            return None
        запись = dict(row)
        try:
            запись["detail"] = json.loads(запись.get("detail") or "null")
        except (TypeError, ValueError):
            запись["detail"] = None
        return запись

    def list_content(self, *, since: float | None = None,
                     owner: str | list[str] | None = None,
                     limit: int = 100000) -> list[dict[str, Any]]:
        """Свод по записям за окно — для разрезов по корпусу.

        Соединяется с заданиями, потому что окно, владелец, модель и метка
        живут там: держать их копию в таблице разбора значило бы обновлять
        её при каждой смене метки.
        """
        where = ["j.status='completed'"]
        args: list[Any] = []
        if since:
            where.append("j.created_at>=?")
            args.append(since)
        if owner:
            владельцы = [owner] if isinstance(owner, str) else list(owner)
            where.append("j.owner IN (" + ",".join("?" for _ in владельцы) + ")")
            args.extend(владельцы)
        колонки = ", ".join(f"c.{к.strip()}" for к in self.CONTENT_COLUMNS.split(","))
        rows = self.query(
            f"SELECT {колонки}, j.owner, j.model, j.engine, j.tags, "
            f"       j.created_at, j.media_duration_s, j.language, j.source "
            f"FROM content c JOIN jobs j ON j.id = c.job_id "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY j.created_at DESC LIMIT ?",
            [*args, limit])
        return [dict(r) for r in rows]

    def content_pending(self, version: int, limit: int = 200) -> list[dict[str, Any]]:
        """Завершённые задания, разбор которых устарел или его нет вовсе.

        Нужен для пересчёта: разбор появился позже архива, а словари и
        правила меняются — по версии видно, что пора считать заново.
        """
        rows = self.query(
            "SELECT j.id, j.text, j.media_duration_s, j.quality_flags FROM jobs j "
            "LEFT JOIN content c ON c.job_id = j.id "
            "WHERE j.status='completed' AND j.text IS NOT NULL AND j.text != '' "
            # Контрольные прогоны второй моделью — та же запись второй раз:
            # в разборе содержания ей не место.
            "  AND COALESCE(j.source,'') <> 'control' "
            "  AND (c.job_id IS NULL OR c.version < ?) "
            "ORDER BY j.created_at DESC LIMIT ?",
            (version, limit))
        return [dict(r) for r in rows]

    def content_stats(self, version: int) -> dict[str, int]:
        """Сколько записей разобрано, сколько ждёт разбора.

        Обе цифры считаются по одному и тому же набору заданий —
        завершённым и с расшифровкой. Раньше «разобрано» считалось по всей
        таблице разбора, без оглядки на статус задания, и повтор задания
        (`retry` меняет статус, но оставляет и текст, и строку разбора)
        уменьшал «всего», не трогая «разобрано»: покрытие показывало
        «разобрано всё» на архиве, разобранном наполовину. Предупреждение
        «показатели посчитаны по разобранной части» при этом гасло.
        """
        всего = int(self.query_one(
            "SELECT COUNT(*) n FROM jobs WHERE status='completed' "
            "AND text IS NOT NULL AND text != ''")["n"])
        разобрано = int(self.query_one(
            "SELECT COUNT(*) n FROM content c JOIN jobs j ON j.id = c.job_id "
            "WHERE c.version >= ? AND j.status='completed' "
            "AND j.text IS NOT NULL AND j.text != ''", (version,))["n"])
        return {"total": всего, "analyzed": разобрано,
                "pending": max(0, всего - разобрано)}

    def document_frequency(self, limit: int = 40000,
                           min_df: int = 2) -> tuple[dict[str, int], int]:
        """В скольких записях встречалась каждая основа — знаменатель TF-IDF.

        Считается по таблице основ, а не перебором расшифровок: слова там
        уже разложены и посчитаны, и на архиве это разница между десятками
        миллисекунд и минутами.

        Размер корпуса — число записей, попавших в таблицу, а не число
        завершённых заданий. Считать по jobs было соблазнительно, но тогда
        на свежей базе, где разобрана половина архива, знаменатель вдвое
        завышался, и вес у всех слов уезжал вверх одинаково — то есть
        порядок вроде бы сохранялся, а порог «редкое слово» переставал
        что-либо значить.

        min_df отсекает основы из одной записи: их IDF максимален, и без
        отсечки верх ключевых слов занимали опечатки распознавания. Они же
        составляют больше половины таблицы, так что отсечка ещё и заметно
        уменьшает выдачу.
        """
        rows = self.query(
            "SELECT stem, COUNT(*) AS df FROM content_terms GROUP BY stem "
            "HAVING df >= ? ORDER BY df DESC LIMIT ?", (min_df, limit))
        всего = self.query_one(
            "SELECT COUNT(DISTINCT job_id) AS n FROM content_terms")
        return ({str(r["stem"]): int(r["df"]) for r in rows},
                int(всего["n"]) if всего else 0)

    #: Показатели свода: имя в ответе -> выражение SQL. Один перечень на
    #: все разрезы: свод по корпусу, по владельцу, по метке и по часу — это
    #: один и тот же набор чисел с разной группировкой, и расходиться они не
    #: должны ни при каких обстоятельствах.
    #:
    #: Считается в базе, а не в памяти. Разрез по своду записей за месяц на
    #: оживлённом сервере — это сотня тысяч строк; поднимать их питоном ради
    #: дюжины средних значило бы либо ждать секунды, либо резать выборку —
    #: то есть показывать часть архива как целое.
    CONTENT_METRICS: tuple[tuple[str, str], ...] = (
        ("records", "COUNT(*)"),
        ("hours", "SUM(COALESCE(j.media_duration_s,0))/3600.0"),
        ("sentiment", "AVG(c.sentiment)"),
        ("negative", "SUM(CASE WHEN c.sentiment < -0.15 THEN 1 ELSE 0 END)"),
        ("positive", "SUM(CASE WHEN c.sentiment >  0.15 THEN 1 ELSE 0 END)"),
        ("scored", "SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END)"),
        ("alert_records", "SUM(CASE WHEN COALESCE(c.alerts,0) > 0 THEN 1 ELSE 0 END)"),
        ("commitments", "SUM(COALESCE(c.commitments,0))"),
        ("commitments_dated", "SUM(COALESCE(c.commitments_dated,0))"),
        ("questions", "SUM(COALESCE(c.questions,0))"),
        ("sentiment_shift", "AVG(c.sentiment_shift)"),
        ("wpm", "AVG(c.wpm)"),
        ("silence_share", "AVG(c.silence_share)"),
        ("interruptions", "AVG(c.interruptions)"),
        ("pauses", "AVG(c.pauses)"),
        ("longest_pause_s", "AVG(c.longest_pause_s)"),
        ("filler_rate", "AVG(c.filler_rate)"),
        ("alerts", "AVG(c.alerts)"),
        ("compliance", "AVG(c.compliance)"),
        ("money_max", "MAX(c.money_max)"),
        ("speakers", "AVG(c.speakers)"),
        # Записи, где говорящий один: на них перебивания, монологи, разрез
        # по операторам и ход тональности считать не по чему, и половина
        # показателей группы получается нулями. Ноль, полученный так, — это
        # не «стало лучше», и раздел обязан отличать одно от другого.
        ("mono", "SUM(CASE WHEN COALESCE(c.speakers,0) < 2 THEN 1 ELSE 0 END)"),
        ("duration_s", "AVG(j.media_duration_s)"),
        ("mixed", "SUM(CASE WHEN c.sentiment_label = 'смешанная' THEN 1 ELSE 0 END)"),
        ("overlap_s", "AVG(c.overlap_s)"),
        ("dead_air_s", "AVG(c.dead_air_s)"),
        ("switches", "AVG(c.switches)"),
        ("talk_share", "AVG(c.talk_share)"),
        ("monologue_s", "AVG(c.monologue_s)"),
        # Монолог дольше двух с половиной минут — порог Gong; доля таких
        # записей говорит больше среднего: среднее по архиву коротких
        # звонков не покажет пятиминутный монолог в каждом десятом.
        ("long_monologues", "SUM(CASE WHEN c.monologue_s >= 150 THEN 1 ELSE 0 END)"),
        ("customer_story_s", "AVG(c.customer_story_s)"),
        ("reply_delay_s", "AVG(c.reply_delay_s)"),
        ("tempo_ratio", "AVG(c.tempo_ratio)"),
        ("frustrated", "SUM(CASE WHEN COALESCE(c.frustration,0) > 0 THEN 1 ELSE 0 END)"),
        ("repeat", "SUM(CASE WHEN COALESCE(c.repeat_contact,0) > 0 THEN 1 ELSE 0 END)"),
        # Нецензурная лексика: считаем и записи с ней, и записи, где словарь
        # вообще смотрел, — без второго числа первое не прочитать.
        ("profanity_records", "SUM(CASE WHEN COALESCE(c.profanity,0) > 0 THEN 1 ELSE 0 END)"),
        ("profanity_agent_records",
         "SUM(CASE WHEN COALESCE(c.profanity_agent,0) > 0 THEN 1 ELSE 0 END)"),
        ("profanity_checked", "SUM(CASE WHEN c.profanity IS NOT NULL THEN 1 ELSE 0 END)"),
        # Возражения: всего, без отработки и в скольких записях отработку
        # вообще было чем считать — без третьего числа второе не прочитать.
        ("objections", "SUM(COALESCE(c.objections,0))"),
        ("objections_unhandled", "SUM(COALESCE(c.objections_unhandled,0))"),
        ("objections_checked",
         "SUM(CASE WHEN c.objections_unhandled IS NOT NULL THEN 1 ELSE 0 END)"),
        ("violations", "SUM(COALESCE(c.violations,0))"),
        ("violation_records", "SUM(CASE WHEN COALESCE(c.violations,0) > 0 THEN 1 ELSE 0 END)"),
        ("agent_score", "AVG(c.agent_score)"),
        ("scored_agents", "SUM(CASE WHEN c.agent_score IS NOT NULL THEN 1 ELSE 0 END)"),
        ("empathy", "AVG(c.empathy)"),
        # Обращение по имени: доля записей, где оператор назвал клиента по
        # имени, среди тех, где оператор был определён.
        ("named", "SUM(CASE WHEN COALESCE(c.name_uses,0) > 0 THEN 1 ELSE 0 END)"),
        ("name_checked", "SUM(CASE WHEN c.name_uses IS NOT NULL THEN 1 ELSE 0 END)"),
    )

    #: Как группировать свод. Значение — выражение SQL; None — без
    #: группировки, весь корпус одной строкой.
    #:
    #: `localtime` в разрезах по времени обязателен: сервер живёт в UTC, а
    #: вопрос «в какие часы разговоры тяжелее» задаёт человек, который
    #: работает по своим. Без него «тяжелее всего в 14:00» означало бы
    #: 17:00 по Москве, и вывод указывал бы не на тот час.
    CONTENT_GROUPS: dict[str, str] = {
        "owner": "COALESCE(NULLIF(j.owner,''),'—')",
        "model": "COALESCE(NULLIF(j.model,''),'—')",
        "engine": "COALESCE(NULLIF(j.engine,''),'—')",
        "language": "COALESCE(NULLIF(j.language,''),'—')",
        "source": "COALESCE(NULLIF(j.source,''),'—')",
        "speaker": "COALESCE(NULLIF(c.agent_speaker,''),'—')",
        "tag": "COALESCE(j.tags,'')",
        "label": "COALESCE(NULLIF(c.sentiment_label,''),'—')",
        "weekday": "CAST(strftime('%w', j.created_at, 'unixepoch', 'localtime') AS INTEGER)",
        "hour": "CAST(strftime('%H', j.created_at, 'unixepoch', 'localtime') AS INTEGER)",
        "date": "strftime('%Y-%m-%d', j.created_at, 'unixepoch', 'localtime')",
    }

    #: Числовые признаки записи и откуда они берутся. Перечень закрытый:
    #: имена колонок для расчёта связей приходят из настроек раздела, и
    #: подставлять их в запрос без сверки со списком нельзя ни при каких
    #: обстоятельствах.
    CONTENT_NUMERIC: dict[str, str] = {
        "sentiment": "c.sentiment",
        "sentiment_shift": "c.sentiment_shift",
        "wpm": "c.wpm",
        "silence_share": "c.silence_share",
        "interruptions": "c.interruptions",
        "pauses": "c.pauses",
        "longest_pause_s": "c.longest_pause_s",
        "filler_rate": "c.filler_rate",
        "questions": "c.questions",
        "commitments": "c.commitments",
        "commitments_dated": "c.commitments_dated",
        "alerts": "c.alerts",
        "compliance": "c.compliance",
        "money_max": "c.money_max",
        "speakers": "c.speakers",
        "negative_segments": "c.negative_segments",
        "positive_segments": "c.positive_segments",
        "duration_s": "j.media_duration_s",
        "overlap_s": "c.overlap_s",
        "dead_air_s": "c.dead_air_s",
        "switches": "c.switches",
        "talk_share": "c.talk_share",
        "monologue_s": "c.monologue_s",
        "customer_story_s": "c.customer_story_s",
        "reply_delay_s": "c.reply_delay_s",
        "tempo_ratio": "c.tempo_ratio",
        "frustration": "c.frustration",
        "repeat_contact": "c.repeat_contact",
        "objections": "c.objections",
        "objections_unhandled": "c.objections_unhandled",
        "violations": "c.violations",
        "agent_score": "c.agent_score",
        "empathy": "c.empathy",
        "name_uses": "c.name_uses",
    }

    #: Разрезы, по которым бывает карточка оператора: метка говорящего в
    #: разборе или владелец задания (ключ доступа). Выражение берётся
    #: отсюда, ключ группы уходит параметром.
    AGENT_DIMENSIONS: dict[str, str] = {
        "speaker": "COALESCE(NULLIF(c.agent_speaker,''),'—') = ?",
        "owner": "COALESCE(NULLIF(j.owner,''),'—') = ?",
    }

    def _content_where(self, since: float | None, until: float | None,
                       owner: str | list[str] | None,
                       extra: str = "",
                       agent: tuple[str, str] | None = None) -> tuple[str, list[Any]]:
        where = ["j.status='completed'"]
        args: list[Any] = []
        if agent:
            выражение = self.AGENT_DIMENSIONS.get(agent[0])
            if выражение is None:
                raise ValueError(f"неизвестный разрез оператора: {agent[0]}")
            where.append(выражение)
            args.append(agent[1])
        if since:
            where.append("j.created_at>=?")
            args.append(since)
        if until:
            where.append("j.created_at<?")
            args.append(until)
        if owner:
            владельцы = [owner] if isinstance(owner, str) else list(owner)
            where.append("j.owner IN (" + ",".join("?" for _ in владельцы) + ")")
            args.extend(владельцы)
        if extra:
            where.append(extra)
        return " AND ".join(where), args

    def content_aggregate(self, *, since: float | None = None,
                          until: float | None = None,
                          owner: str | list[str] | None = None,
                          group_by: str | None = None,
                          limit: int = 200,
                          agent: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        """Свод по разобранным записям — целиком или по группам.

        Возвращает список строк; без группировки — ровно одну. Пустой корпус
        тоже даёт строку, с нулями и None: разделу нужно показать «записей
        нет», а не свалиться на отсутствующем ключе.
        """
        # Категория — не колонка записи, а строки таблицы совпадений:
        # запись про оплату и доставку входит в обе группы, и разрез по
        # категориям считается через соединение с этой таблицей.
        соединение = ""
        if group_by == "category":
            выражение = "h.category"
            соединение = " JOIN content_hits h ON h.job_id = c.job_id"
        else:
            выражение = self.CONTENT_GROUPS.get(group_by or "")
            if group_by and выражение is None:
                return []
        показатели = ", ".join(f"{выр} AS {имя}" for имя, выр in self.CONTENT_METRICS)
        условие, args = self._content_where(since, until, owner, agent=agent)
        начало = f"SELECT {выражение} AS group_key, " if выражение else "SELECT "
        хвост = (f" GROUP BY group_key ORDER BY records DESC LIMIT {int(limit)}"
                 if выражение else "")
        rows = self.query(
            f"{начало}{показатели} FROM content c JOIN jobs j ON j.id = c.job_id"
            f"{соединение} WHERE {условие}{хвост}", args)
        return [dict(r) for r in rows]

    def category_counts(self, *, since: float | None = None,
                        until: float | None = None,
                        owner: str | list[str] | None = None,
                        ) -> list[dict[str, Any]]:
        """Сколько записей окна попало в каждую категорию и сколько раз.

        Считается по таблице совпадений, соединённой с заданиями: окно и
        владелец живут там. Категории, не встретившиеся ни разу, в ответе
        нет — их дописывает вызывающая сторона по набору из настроек:
        «0 записей про возврат» — тоже ответ, и его надо показать.
        """
        условие, args = self._content_where(since, until, owner)
        rows = self.query(
            "SELECT h.category, h.kind, COUNT(*) AS records, "
            "       SUM(h.count) AS mentions, AVG(h.first_s) AS first_s, "
            # Отрицательные и оценённые записи категории — числитель и
            # знаменатель для драйверов негатива; порог тот же, что везде.
            "       SUM(CASE WHEN c.sentiment < -0.15 THEN 1 ELSE 0 END) AS negative, "
            "       SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END) AS scored "
            "FROM content_hits h JOIN jobs j ON j.id = h.job_id "
            "LEFT JOIN content c ON c.job_id = h.job_id "
            f"WHERE {условие} GROUP BY h.category, h.kind "
            "ORDER BY records DESC", args)
        return [dict(r) for r in rows]

    def agent_violations(self, *, since: float | None = None,
                         until: float | None = None,
                         owner: str | list[str] | None = None,
                         agent: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        """Сработавшие нарушения по категориям — для карточки оператора."""
        условие, args = self._content_where(since, until, owner, agent=agent)
        rows = self.query(
            "SELECT h.category, COUNT(*) AS records, SUM(h.count) AS mentions "
            "FROM content_hits h JOIN content c ON c.job_id = h.job_id "
            "JOIN jobs j ON j.id = h.job_id "
            f"WHERE {условие} AND h.kind = 'violation' "
            "GROUP BY h.category ORDER BY records DESC", args)
        return [dict(r) for r in rows]

    # --- отметки коучинга и эталонов -------------------------------------

    MARK_KINDS = ("coaching", "reference")

    def set_mark(self, job_id: str, kind: str, status: str, note: str = "") -> None:
        """Ставит или снимает отметку; пустой статус — снять."""
        if kind not in self.MARK_KINDS:
            raise ValueError(f"неизвестный вид отметки: {kind}")
        with self.write() as conn:
            if not status:
                conn.execute("DELETE FROM content_marks WHERE job_id=? AND kind=?",
                             (job_id, kind))
                return
            conn.execute(
                "INSERT INTO content_marks (job_id, kind, status, note, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(job_id, kind) DO UPDATE SET "
                "status=excluded.status, note=excluded.note, updated_at=excluded.updated_at",
                (job_id, kind, status, note or "", now()))

    def get_marks(self, job_ids: list[str]) -> dict[str, dict[str, dict[str, Any]]]:
        """Отметки по заданиям: {job_id: {kind: {status, note, updated_at}}}."""
        if not job_ids:
            return {}
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for i in range(0, len(job_ids), 500):
            кусок = job_ids[i:i + 500]
            места = ",".join("?" for _ in кусок)
            for r in self.query(
                    f"SELECT job_id, kind, status, note, updated_at FROM content_marks "
                    f"WHERE job_id IN ({места})", кусок):
                out.setdefault(str(r["job_id"]), {})[str(r["kind"])] = {
                    "status": r["status"], "note": r["note"], "updated_at": r["updated_at"]}
        return out

    # --- очередь ручной проверки ------------------------------------------

    REVIEW_STATUSES = ("pending", "done", "skipped")
    REVIEW_REASONS = ("random", "low_confidence", "manual", "bad_audio")

    def review_add(self, job_id: str, reason: str, *, picked_at: float | None = None) -> bool:
        """Ставит запись в очередь; уже стоящую — не трогает. True — добавлена."""
        if reason not in self.REVIEW_REASONS:
            raise ValueError(f"неизвестная причина проверки: {reason}")
        with self.write() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO review_queue (job_id, reason, status, picked_at) "
                "VALUES (?,?,'pending',?)", (job_id, reason, picked_at or now()))
            return cur.rowcount > 0

    def review_update(self, job_id: str, status: str, *, reviewer: str = "",
                      note: str = "") -> bool:
        """Меняет исход проверки: done, skipped или обратно pending."""
        if status not in self.REVIEW_STATUSES:
            raise ValueError(f"неизвестный исход проверки: {status}")
        with self.write() as conn:
            cur = conn.execute(
                "UPDATE review_queue SET status=?, done_at=?, reviewer=?, note=? WHERE job_id=?",
                (status, now() if status != "pending" else None, reviewer or None,
                 note or None, job_id))
            return cur.rowcount > 0

    def review_queued_ids(self) -> set[str]:
        return {str(r["job_id"]) for r in self.query("SELECT job_id FROM review_queue")}

    def review_list(self, *, status: str | None = "pending",
                    owner: str | list[str] | None = None,
                    limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        """Очередь вместе с полями задания, нужными таблице."""
        where = []
        args: list[Any] = []
        if status:
            where.append("r.status=?")
            args.append(status)
        if isinstance(owner, (list, tuple)):
            where.append(f"j.owner IN ({','.join('?' for _ in owner)})")
            args.extend(owner)
        elif owner:
            where.append("j.owner=?")
            args.append(owner)
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.query(
            "SELECT r.job_id, r.reason, r.status, r.picked_at, r.done_at, r.reviewer, r.note, "
            "       j.filename, j.model, j.owner, j.source, j.media_duration_s, "
            "       j.avg_confidence, j.snr_db, j.wer, j.ref_words, j.created_at "
            f"FROM review_queue r JOIN jobs j ON j.id = r.job_id {clause} "
            "ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, "
            "         j.avg_confidence ASC, r.picked_at DESC LIMIT ? OFFSET ?",
            [*args, limit, offset])
        return [dict(r) for r in rows]

    def review_counts(self, since: float | None = None) -> dict[str, int]:
        """Сколько ожидает, сколько разобрано и пропущено за окно."""
        out = {"pending": 0, "done": 0, "skipped": 0}
        for r in self.query(
                "SELECT status, COUNT(*) n FROM review_queue "
                "WHERE status='pending' OR done_at >= ? GROUP BY status", (since or 0.0,)):
            out[str(r["status"])] = int(r["n"])
        return out

    # --- контрольные прогоны второй моделью ----------------------------------

    def add_model_check(self, job_id: str, check_job_id: str, *, model: str,
                        control_model: str, wer: float | None, mer: float | None,
                        words: int) -> None:
        self.execute(
            "INSERT INTO model_checks (job_id, check_job_id, model, control_model, wer, mer, "
            "words, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (job_id, check_job_id, model, control_model, wer, mer, words, now()))

    def model_checks(self, since: float, *, owner: str | list[str] | None = None,
                     limit: int = 100000) -> list[dict[str, Any]]:
        where = ["m.created_at >= ?"]
        args: list[Any] = [since]
        if isinstance(owner, (list, tuple)):
            where.append(f"j.owner IN ({','.join('?' for _ in owner)})")
            args.extend(owner)
        elif owner:
            where.append("j.owner=?")
            args.append(owner)
        rows = self.query(
            "SELECT m.id, m.job_id, m.check_job_id, m.model, m.control_model, m.wer, m.mer, "
            "       m.words, m.created_at, j.filename, j.source, j.snr_db, j.avg_confidence "
            f"FROM model_checks m JOIN jobs j ON j.id = m.job_id WHERE {' AND '.join(where)} "
            "ORDER BY m.created_at DESC LIMIT ?", [*args, limit])
        return [dict(r) for r in rows]

    # --- смысловой слой языковой модели ----------------------------------------

    def llm_save(self, job_id: str, version: int, **поля: Any) -> None:
        """Кладёт ответ модели по записи; словари и списки — строкой JSON."""
        строка = dict(поля)
        for к in ("actions", "trackers", "scorecard", "warnings"):
            if isinstance(строка.get(к), (list, dict)):
                строка[к] = json.dumps(строка[к], ensure_ascii=False)
        if строка.get("resolved") is not None:
            строка["resolved"] = 1 if строка["resolved"] else 0
        колонки = ["job_id", "version", "created_at", *строка]
        значения = [job_id, version, now(), *строка.values()]
        self.execute(
            f"INSERT OR REPLACE INTO llm_results ({', '.join(колонки)}) "
            f"VALUES ({', '.join('?' for _ in колонки)})", значения)

    def llm_get(self, job_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM llm_results WHERE job_id=?", (job_id,))
        return _row_to_llm(row) if row else None

    def llm_pending(self, version: int, limit: int = 50, *, since: float | None = None
                    ) -> list[dict[str, Any]]:
        """Завершённые записи без ответа модели текущей версии — новые первыми.

        Контрольные прогоны мимо: это та же запись второй раз. Записи из
        кеша остаются: у клона свой владелец и своя карточка, а модель по
        ним всё равно не вызывается второй раз — тот же текст даёт тот же
        отпечаток подсказки, и ответ берётся из кеша.
        """
        where = ["j.status='completed'", "j.text IS NOT NULL", "j.text != ''",
                 "COALESCE(j.source,'') <> 'control'",
                 "(l.job_id IS NULL OR l.version < ?)"]
        args: list[Any] = [version]
        if since is not None:
            where.append("j.created_at >= ?")
            args.append(since)
        rows = self.query(
            "SELECT j.id, j.text, j.media_duration_s, j.model, j.owner FROM jobs j "
            "LEFT JOIN llm_results l ON l.job_id = j.id "
            f"WHERE {' AND '.join(where)} ORDER BY j.created_at DESC LIMIT ?",
            [*args, limit])
        return [dict(r) for r in rows]

    def llm_stats(self, version: int, since: float, *,
                  owner: str | list[str] | None = None) -> dict[str, Any]:
        """Свод ответов модели за окно: покрытие, исходы, причины, действия,
        трекеры, задержка, ошибки."""
        where = ["j.status='completed'", "j.created_at >= ?",
                 "COALESCE(j.source,'') <> 'control'"]
        args: list[Any] = [since]
        if isinstance(owner, (list, tuple)):
            where.append(f"j.owner IN ({','.join('?' for _ in owner)})")
            args.extend(owner)
        elif owner:
            where.append("j.owner=?")
            args.append(owner)
        условие = " AND ".join(where)
        всего = int(self.query_one(f"SELECT COUNT(*) n FROM jobs j WHERE {условие}", args)["n"])
        rows = self.query(
            "SELECT l.job_id, l.version, l.reason, l.outcome, l.resolved, l.actions, l.trackers, "
            "       l.scorecard, l.latency_ms, l.error, l.calls, l.chunks, l.warnings, j.filename, "
            "       j.created_at, j.owner "
            f"FROM llm_results l JOIN jobs j ON j.id = l.job_id WHERE {условие}", args)
        return {"total": всего, "rows": [_row_to_llm(r) for r in rows]}

    def llm_cache_get(self, key: str) -> str | None:
        row = self.query_one("SELECT response FROM llm_cache WHERE key=?", (key,))
        return str(row["response"]) if row else None

    def llm_cache_put(self, key: str, kind: str, model: str, response: str,
                      latency_ms: float) -> None:
        self.execute(
            "INSERT OR REPLACE INTO llm_cache (key, kind, model, response, latency_ms, "
            "created_at) VALUES (?,?,?,?,?,?)", (key, kind, model, response, latency_ms, now()))

    def llm_outcome_filter(self, kind: str, value: str) -> tuple[str, list[Any]]:
        """Условие отбора заданий по исходу или причине из ответа модели."""
        колонка = {"outcome": "outcome", "reason": "reason"}[kind]
        return (f"jobs.id IN (SELECT job_id FROM llm_results WHERE {колонка} = ?)", [value])

    def file_paths(self, job_ids: list[str]) -> dict[str, str]:
        """Пути исходных файлов по номерам заданий.

        Отдельным запросом, а не колонкой облегчённого списка: путь на
        диске сервера — не то, что должно уезжать в таблицу результатов
        каждому ключу. Нужен только контрольному прогону — он ставит
        задание на тот же файл.
        """
        out: dict[str, str] = {}
        for i in range(0, len(job_ids), 500):
            кусок = job_ids[i:i + 500]
            места = ",".join("?" for _ in кусок)
            for r in self.query(f"SELECT id, file_path FROM jobs WHERE id IN ({места})", кусок):
                if r["file_path"]:
                    out[str(r["id"])] = str(r["file_path"])
        return out

    def checked_job_ids(self, since: float) -> set[str]:
        return {str(r["job_id"]) for r in self.query(
            "SELECT job_id FROM model_checks WHERE created_at >= ?", (since,))}

    #: Что зовёт запись в очередь коучинга. Выражения постоянные; причина
    #: подписывается в коде по тем же полям.
    COACHING_REASONS: tuple[tuple[str, str], ...] = (
        ("violations", "COALESCE(c.violations,0) > 0"),
        ("low_score", "c.agent_score IS NOT NULL AND c.agent_score < 60"),
        ("script", "c.compliance IS NOT NULL AND c.compliance < 0.5"),
        ("monologue", "c.monologue_s >= 150"),
        ("impolite", "c.empathy IS NOT NULL AND c.empathy < 0"),
        ("objections", "COALESCE(c.objections_unhandled,0) > 0"),
        ("frustrated", "COALESCE(c.frustration,0) > 0"),
        ("profanity", "COALESCE(c.profanity_agent,0) > 0"),
    )

    def coaching_queue(self, *, since: float | None = None,
                       until: float | None = None,
                       owner: str | list[str] | None = None,
                       agent: tuple[str, str] | None = None,
                       limit: int = 50, include_done: bool = False) -> list[dict[str, Any]]:
        """Записи, которые стоит разобрать с оператором, — худшие первыми.

        Разобранные (отметка coaching=done) не показываются, пока не
        попросят: очередь — это то, что осталось, а не всё, что было.
        """
        условие, args = self._content_where(since, until, owner, agent=agent)
        причины = " OR ".join(f"({выр})" for _, выр in self.COACHING_REASONS)
        колонки = ", ".join(f"c.{к.strip()}" for к in self.CONTENT_COLUMNS.split(","))
        готово = "" if include_done else " AND COALESCE(m.status,'') <> 'done'"
        rows = self.query(
            f"SELECT {колонки}, j.owner, j.model, j.tags, j.created_at, "
            f"       j.media_duration_s, j.filename, m.status AS mark_status, "
            f"       m.note AS mark_note, m.updated_at AS mark_at "
            f"FROM content c JOIN jobs j ON j.id = c.job_id "
            f"LEFT JOIN content_marks m ON m.job_id = c.job_id AND m.kind = 'coaching' "
            f"WHERE {условие} AND ({причины}){готово} "
            f"ORDER BY c.agent_score IS NULL, c.agent_score ASC, c.sentiment ASC LIMIT ?",
            [*args, limit])
        return [dict(r) for r in rows]

    def reference_records(self, *, since: float | None = None,
                          until: float | None = None,
                          owner: str | list[str] | None = None,
                          limit: int = 20) -> list[dict[str, Any]]:
        """Лучшие разговоры периода — по баллу и тональности, без нарушений —
        и всё, что отмечено эталоном руками, независимо от периода."""
        условие, args = self._content_where(since, until, owner)
        колонки = ", ".join(f"c.{к.strip()}" for к in self.CONTENT_COLUMNS.split(","))
        общие = (f"SELECT {колонки}, j.owner, j.model, j.tags, j.created_at, "
                 f"       j.media_duration_s, j.filename, m.status AS mark_status, "
                 f"       m.note AS mark_note "
                 f"FROM content c JOIN jobs j ON j.id = c.job_id "
                 f"LEFT JOIN content_marks m ON m.job_id = c.job_id AND m.kind = 'reference' ")
        лучшие = self.query(
            общие + f"WHERE {условие} AND c.agent_score IS NOT NULL "
            "AND COALESCE(c.violations,0) = 0 AND c.sentiment IS NOT NULL "
            "ORDER BY c.agent_score DESC, c.sentiment DESC LIMIT ?", [*args, limit])
        отмеченные = self.query(
            общие + "WHERE j.status='completed' AND m.status = 'yes' "
            "ORDER BY m.updated_at DESC LIMIT ?", [limit])
        out: list[dict[str, Any]] = []
        видели: set[str] = set()
        for r in [*отмеченные, *лучшие]:
            if r["job_id"] in видели:
                continue
            видели.add(r["job_id"])
            out.append(dict(r))
        return out

    def tracker_hits(self, *, since: float | None = None,
                     until: float | None = None) -> list[dict[str, Any]]:
        """Срабатывания трекеров за окно — по журналу событий.

        Категория лежит в данных события; группируем по ней прямо в базе.
        На сборке SQLite без JSON (редкость, но бывает) — пустой список,
        а не ошибка: сводка от этого не должна пропадать.
        """
        where = ["kind='tracker'"]
        args: list[Any] = []
        if since:
            where.append("ts>=?")
            args.append(since)
        if until:
            where.append("ts<?")
            args.append(until)
        try:
            rows = self.query(
                "SELECT json_extract(data, '$.category') AS category, "
                "       json_extract(data, '$.label') AS label, COUNT(*) AS hits, "
                "       COUNT(DISTINCT job_id) AS records "
                f"FROM events WHERE {' AND '.join(where)} "
                "GROUP BY category ORDER BY hits DESC", args)
        except StorageError:
            return []
        return [dict(r) for r in rows if r["category"]]

    def uncategorized_count(self, *, since: float | None = None,
                            until: float | None = None,
                            owner: str | list[str] | None = None) -> int:
        """Разобранные записи окна без единой категории обращения.

        Считаются по таблице разбора, а не по всем заданиям: запись, до
        которой разбор ещё не дошёл, не «без категории», а «не смотрели».
        """
        условие, args = self._content_where(since, until, owner)
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM content c JOIN jobs j ON j.id = c.job_id "
            f"WHERE {условие} AND NOT EXISTS (SELECT 1 FROM content_hits h "
            "WHERE h.job_id = c.job_id AND h.kind = 'topic')", args)
        return int(row["n"]) if row else 0

    def content_series(self, *, since: float, until: float, buckets: int = 24,
                       owner: str | list[str] | None = None,
                       agent: tuple[str, str] | None = None,
                       ) -> tuple[int, float, list[dict[str, Any]]]:
        """Ряд показателей содержания по времени — свёртка в SQL.

        Возвращает и число корзин, до которого пришлось урезать запрошенное.
        Без него вызывающая сторона рисовала запрошенные двести корзин по
        урезанному шагу: на часовом окне это восемь тысяч секунд вместо
        трёх с половиной, то есть полуторачасовой пустой хвост в будущем,
        который читается как «разговоров не было».
        """
        buckets, шаг = self._bucket_step(since, until, buckets)
        условие, args = self._content_where(since, until, owner, agent=agent)
        rows = self.query(
            "SELECT CAST((j.created_at-?)/? AS INTEGER) AS bucket, "
            "       COUNT(*) AS records, AVG(c.sentiment) AS sentiment, "
            "       SUM(CASE WHEN c.sentiment < -0.15 THEN 1 ELSE 0 END) AS negative, "
            "       SUM(CASE WHEN c.sentiment IS NOT NULL THEN 1 ELSE 0 END) AS scored, "
            "       SUM(COALESCE(c.alerts,0)) AS alerts, "
            "       AVG(c.compliance) AS compliance, AVG(c.wpm) AS wpm, "
            "       AVG(c.agent_score) AS agent_score, AVG(c.empathy) AS empathy, "
            "       SUM(CASE WHEN COALESCE(c.violations,0) > 0 THEN 1 ELSE 0 END) AS violation_records "
            "FROM content c JOIN jobs j ON j.id = c.job_id "
            f"WHERE {условие} GROUP BY bucket ORDER BY bucket",
            [since, шаг, *args])
        return buckets, шаг, [dict(r) for r in rows]

    def content_top(self, order: str, *, since: float | None = None,
                    until: float | None = None,
                    owner: str | list[str] | None = None,
                    where: str = "", limit: int = 20,
                    agent: tuple[str, str] | None = None) -> list[dict[str, Any]]:
        """Записи окна по заданному порядку — для списков «что послушать».

        `order` и `where` приходят не от пользователя, а из перечня отборов
        в `insights`: подставлять сюда строку из запроса нельзя, и вызывающая
        сторона обязана это гарантировать.
        """
        условие, args = self._content_where(since, until, owner, where, agent=agent)
        колонки = ", ".join(f"c.{к.strip()}" for к in self.CONTENT_COLUMNS.split(","))
        rows = self.query(
            f"SELECT {колонки}, j.owner, j.model, j.tags, j.created_at, "
            f"       j.media_duration_s, j.filename "
            f"FROM content c JOIN jobs j ON j.id = c.job_id "
            f"WHERE {условие} ORDER BY {order} LIMIT ?", [*args, limit])
        return [dict(r) for r in rows]

    def content_sample(self, columns: list[str], *, since: float | None = None,
                       until: float | None = None,
                       owner: str | list[str] | None = None,
                       limit: int = 20000,
                       agent: tuple[str, str] | None = None) -> list[tuple[float, ...]]:
        """Выборка числовых признаков для расчёта связей.

        Именно выборка, а не весь корпус: коэффициент корреляции по двадцати
        тысячам записей отличается от коэффициента по ста тысячам в третьем
        знаке, а поднимать впятеро больше строк ради этого незачем. Берутся
        свежие: связь на прошлогодних настройках сервера — это ответ на
        вопрос, которого никто не задавал.
        """
        безопасные = [к for к in columns if к in self.CONTENT_NUMERIC]
        if not безопасные:
            return []
        поля = ", ".join(self.CONTENT_NUMERIC[к] for к in безопасные)
        условие, args = self._content_where(since, until, owner, agent=agent)
        rows = self.query(
            f"SELECT {поля} FROM content c JOIN jobs j ON j.id = c.job_id "
            f"WHERE {условие} ORDER BY j.created_at DESC LIMIT ?", [*args, limit])
        return [tuple(r) for r in rows]

    def top_terms(self, *, since: float | None = None,
                  until: float | None = None,
                  owner: str | list[str] | None = None,
                  stems: list[str] | None = None,
                  limit: int = 60, min_records: int = 2) -> list[dict[str, Any]]:
        """Самые частые темы окна: основа, форма для показа, число записей.

        Считается в базе группировкой по основе. Поднимать ради этого своды
        записей в память нельзя: тема — это «в скольких записях встретилось
        слово», а своды слов не содержат вовсе, они про числа.

        Два запроса, а не один. Форму показа («поставки» вместо «поставк»)
        сначала выбирал подзапрос в списке колонок — по одному на каждую
        строку ответа. На двух миллионах основ он занимал шесть секунд из
        шести с половиной: подзапрос выполнялся для каждой группы заново.
        Теперь формы добираются вторым запросом и только для тех
        нескольких десятков основ, которые пойдут в ответ.

        Соединение с заданиями тоже не бесплатное, поэтому без окна и без
        владельца его нет вовсе: в таблице основ лежат только завершённые
        записи, а `delete_job` убирает их вместе с заданием — то есть
        соединение в этом случае не отбрасывает ни одной строки.
        """
        # Перечень основ (`stems`) нужен сравнению окон: сколько раз именно
        # ЭТИ темы встречались раньше. Без него прошлое окно спрашивалось
        # своей верхушкой, и тема, не попавшая в срез, считалась
        # встретившейся ноль раз — то есть новой.
        if stems is not None and not stems:
            return []
        нужен_join = bool(since or until or owner)
        отбор = ""
        отбор_args: list[Any] = []
        if stems is not None:
            места = ",".join("?" for _ in stems)
            отбор = f" AND t.stem IN ({места})" if нужен_join \
                else f" WHERE t.stem IN ({места})"
            отбор_args = list(stems)
        if нужен_join:
            условие, args = self._content_where(since, until, owner)
            источник = ("FROM content_terms t JOIN jobs j ON j.id = t.job_id "
                        f"WHERE {условие}{отбор} ")
        else:
            источник, args = f"FROM content_terms t{отбор} ", []
        args = [*args, *отбор_args]
        rows = self.query(
            # COUNT(*), а не COUNT(DISTINCT job_id): первичный ключ таблицы —
            # (job_id, stem), то есть внутри группы по основе каждое задание
            # встречается ровно один раз, и различать там нечего. Разница не
            # косметическая: DISTINCT заводит временное дерево на два
            # миллиона строк и стоит две секунды из двух с небольшим.
            "SELECT t.stem AS stem, COUNT(*) AS records, "
            "       SUM(t.n) AS mentions "
            f"{источник}"
            "GROUP BY t.stem HAVING records >= ? "
            "ORDER BY records DESC, mentions DESC LIMIT ?",
            [*args, min_records, limit])
        формы = self.term_words([str(r["stem"]) for r in rows])
        return [{"stem": r["stem"], "word": формы.get(str(r["stem"]), r["stem"]),
                 "records": int(r["records"]), "mentions": int(r["mentions"] or 0)}
                for r in rows]

    def vocabulary_size(self) -> int:
        """Сколько разных основ знает сервер — одно число, без выборки.

        Нужно состоянию разбора, которое интерфейс опрашивает раз в
        пятнадцать секунд. Раньше оно брало эту цифру из снимка корпусных
        частот, а снимок сбрасывается после каждой порции фонового
        разбора — то есть каждые пять секунд, пока архив разбирается.
        Получалось, что ровно во время разбора кеш не работал никогда, и
        каждый опрос полосы состояния делал полную группировку по таблице
        основ: на архиве в сотню тысяч записей это секунды, причём под
        общей блокировкой, которая на это же время останавливает и сам
        разбор, и открытие карточки.
        """
        row = self.query_one("SELECT COUNT(*) AS n FROM content_vocab")
        return int(row["n"]) if row else 0

    def term_words(self, stems: list[str]) -> dict[str, str]:
        """Самая частая форма показа для каждой из перечисленных основ.

        Самая частая среди записей, а не первая попавшаяся: у одной основы
        форм несколько («поставка», «поставки»), и произвольный выбор менял
        бы подпись темы от отчёта к отчёту.
        """
        if not stems:
            return {}
        места = ",".join("?" for _ in stems)
        rows = self.query(
            f"SELECT stem, word, n FROM content_vocab WHERE stem IN ({места}) "
            f"ORDER BY stem, n DESC, word", list(stems))
        формы: dict[str, str] = {}
        for r in rows:
            формы.setdefault(str(r["stem"]), str(r["word"]))
        return формы

    def content_window_size(self, *, since: float | None = None,
                            until: float | None = None,
                            owner: str | list[str] | None = None) -> int:
        """Сколько разобранных записей попадает в окно — знаменатель долей."""
        where = ["j.status='completed'", "c.job_id IS NOT NULL"]
        args: list[Any] = []
        if since:
            where.append("j.created_at>=?")
            args.append(since)
        if until:
            where.append("j.created_at<?")
            args.append(until)
        if owner:
            владельцы = [owner] if isinstance(owner, str) else list(owner)
            where.append("j.owner IN (" + ",".join("?" for _ in владельцы) + ")")
            args.extend(владельцы)
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM jobs j JOIN content c ON c.job_id = j.id "
            f"WHERE {' AND '.join(where)}", args)
        return int(row["n"]) if row else 0

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
                   model: str | None = None, search: str | None = None,
                   group_id: str | None = None,
                   content: str | None = None,
                   since: float | None = None) -> int:
        """Сколько заданий подходит под отбор.

        Тот же набор условий, что у `list_jobs`, и собирает их та же
        функция. Раньше счётчик знал только про статус и владельца: список
        с поиском показывал тридцать строк и «всего 4000», а листалка вела
        на пустые страницы.
        """
        собрано = self._jobs_where(status=status, owner=owner, model=model,
                                   search=search, group_id=group_id, since=since,
                                   content=content)
        if собрано is None:
            return 0
        соединение, where, args = собрано
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM jobs{соединение} {clause}", args)
        return int(row["n"]) if row else 0

    def delete_job(self, job_id: str) -> None:
        with self.write() as conn:
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM events WHERE job_id=?", (job_id,))
            # Разбор и основы — тоже за заданием. Без этих двух строк
            # содержание удалённой записи продолжало влиять на своды и на
            # вес ключевых слов: запись из отчётов пропадала, а её основы
            # оставались в знаменателе навсегда.
            conn.execute("DELETE FROM content WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM content_terms WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM content_hits WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM content_marks WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM review_queue WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM llm_results WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM model_checks WHERE job_id=? OR check_job_id=?",
                         (job_id, job_id))
            # content_vocab не трогаем: это словарь форм на весь сервер, а не
            # данные задания. Строка «поставк → поставки» после удаления
            # записи остаётся верной, а перебирать ради неё все прочие записи
            # с той же основой — работа на ровном месте.
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
        _serialize_json_fields(fields)
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

    def metric_values(self, name: str, since: float, *, limit: int = 100000
                      ) -> list[dict[str, Any]]:
        """Все значения одной метрики за окно — с моделью, без корзин.

        Для перцентилей нужны сами значения, а не ряд по корзинам: p95
        задержки из средних по корзинам — это не p95.
        """
        rows = self.query(
            "SELECT ts, value, model, engine, labels FROM metrics WHERE name=? AND ts>=? "
            "ORDER BY ts DESC LIMIT ?", (name, since, limit))
        return [dict(r) for r in rows]

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
                # Время берём из самого замера, если оно там есть: замеры по
                # видеокартам пишутся отдельной таблицей и должны лечь на ту
                # же ось времени, а не на «почти ту же».
                (float(sample.get("ts") or now()),
                 sample.get("cpu_percent"), sample.get("ram_used_mb"),
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

    def add_gpu_samples(self, ts: float, rows: list[dict[str, Any]]) -> None:
        """Замеры по всем картам за один момент времени."""
        if not rows:
            return
        self.executemany(
            "INSERT OR REPLACE INTO gpu_samples "
            "(ts, gpu, name, util_percent, mem_used_mb, mem_total_mb, "
            " temperature_c, power_w, power_limit_w) VALUES (?,?,?,?,?,?,?,?,?)",
            [(ts, int(r.get("gpu", 0)), r.get("name"), r.get("util_percent"),
              r.get("mem_used_mb"), r.get("mem_total_mb"), r.get("temperature_c"),
              r.get("power_w"), r.get("power_limit_w")) for r in rows])

    def gpu_samples(self, since: float, limit: int = 4000) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT * FROM gpu_samples WHERE ts>=? ORDER BY ts DESC LIMIT ?",
            (since, limit))
        return [dict(r) for r in reversed(rows)]

    def _bucket_step(self, since: float, until: float, buckets: int) -> tuple[int, float]:
        """Число корзин и их шаг — общая арифметика обоих рядов.

        Корзина уже такта сборщика бессмысленна: замеры приходят раз в
        `SAMPLE_PERIOD_S` секунд, и в корзину шириной в такт то попадают два
        замера, то ни одного. На графике это выглядело пунктиром — линия
        рвалась не там, где сервер молчал, а там, где такт разошёлся с
        границей корзины на долю секунды. Поэтому корзина не уже двух
        тактов: одного мало, ровно на такой ширине дрожание такта и даёт
        пустые корзины вперемешку с двойными.
        """
        окно = max(float(until) - float(since), 1e-6)
        предел = max(1, int(окно / (2 * SAMPLE_PERIOD_S)))
        buckets = max(1, min(int(buckets), SERIES_MAX_BUCKETS, предел))
        return buckets, окно / buckets

    def system_series(self, since: float, until: float,
                      buckets: int) -> tuple[int, list[dict[str, Any]]]:
        """Ряд нагрузки сервера, свёрнутый до `buckets` корзин силами SQLite.

        Раньше ручка графиков читала все замеры окна в память и резала их
        питоном. За неделю это тридцать тысяч строк на каждый опрос панели,
        и цена запроса зависела от ширины окна, а не от числа точек на
        графике, которых всегда меньше двух сотен. Свёртка в SQL считает то
        же самое: среднее там, где важен уровень, максимум там, где важен
        всплеск — минутный перегрев внутри получасовой корзины усреднение
        стирает, а он и есть причина смотреть на график.
        """
        buckets, шаг = self._bucket_step(since, until, buckets)
        rows = self.query(
            "SELECT CAST((ts-?)/? AS INTEGER) AS bucket,"
            "       AVG(cpu_percent)  AS cpu_percent,"
            "       AVG(ram_used_mb)  AS ram_used_mb,"
            "       MAX(ram_total_mb) AS ram_total_mb,"
            "       AVG(disk_free_gb) AS disk_free_gb,"
            "       MAX(queue_depth)  AS queue_depth,"
            "       MAX(active_jobs)  AS active_jobs,"
            "       COUNT(*)          AS samples "
            "FROM system_samples WHERE ts>=? AND ts<? "
            "GROUP BY bucket ORDER BY bucket",
            (since, шаг, since, until))
        return buckets, [dict(r) for r in rows]

    def gpu_series(self, since: float, until: float,
                   buckets: int) -> tuple[int, list[dict[str, Any]]]:
        """То же по каждой видеокарте отдельно.

        Отдельно, а не в среднем: «средняя загрузка видеокарты» на машине с
        двумя картами — величина, из которой не следует ничего.
        """
        buckets, шаг = self._bucket_step(since, until, buckets)
        rows = self.query(
            "SELECT gpu, CAST((ts-?)/? AS INTEGER) AS bucket,"
            "       MAX(name)             AS name,"
            "       AVG(util_percent)     AS util_percent,"
            "       AVG(mem_used_mb)      AS mem_used_mb,"
            "       MAX(mem_total_mb)     AS mem_total_mb,"
            "       MAX(temperature_c)    AS temperature_c,"
            "       MAX(power_w)          AS power_w,"
            "       MAX(power_limit_w)    AS power_limit_w,"
            "       COUNT(*)              AS samples "
            "FROM gpu_samples WHERE ts>=? AND ts<? "
            "GROUP BY gpu, bucket ORDER BY gpu, bucket",
            (since, шаг, since, until))
        return buckets, [dict(r) for r in rows]

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
        removed = {"jobs": 0, "metrics": 0, "events": 0, "samples": 0,
                   "gpu_samples": 0, "bytes": 0}
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
                removed["bytes"] = removed.get("bytes", 0) + _remove_job_files(
                    dict(row), self.path.parent)
                self.delete_job(row["id"])
            removed["jobs"] = len(stale)
            removed["more"] = 1 if len(stale) >= CLEANUP_BATCH else 0
        if metrics_days > 0:
            removed["metrics"] = self.execute(
                "DELETE FROM metrics WHERE ts<?", (ts - metrics_days * 86400,))
            # Обе таблицы замеров живут по одному сроку. gpu_samples пишется
            # по строке на карту за такт, то есть растёт кратно числу карт —
            # без этой строки она оставалась единственной таблицей в базе,
            # которую не чистил никто.
            граница = ts - metrics_days * 86400
            removed["samples"] = self.execute(
                "DELETE FROM system_samples WHERE ts<?", (граница,))
            removed["gpu_samples"] = self.execute(
                "DELETE FROM gpu_samples WHERE ts<?", (граница,))
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


def _within(base: Path, target: str) -> Path | None:
    """Путь, если он лежит внутри каталога данных, иначе None.

    Уборка удаляет по значениям из базы, а не по вычисленным на месте: путь
    туда кладёт сервер, но если он попадёт в базу как-то иначе — правкой
    руками, восстановлением из чужой копии, ошибкой в новом маршруте, —
    уборка по сроку хранения превратится в удаление произвольного файла,
    причём молча и раз в час. Все каталоги сервера лежат под каталогом
    данных (см. `Paths.create`), так что проверка ровно одна.
    """
    if not target:
        return None
    try:
        корень = base.resolve(strict=True)
        путь = Path(target).resolve(strict=True)
    except OSError:
        return None
    if корень != путь.parent and корень not in путь.parents:
        log.warning("Уборка не трогает «%s»: путь вне каталога данных «%s»",
                    путь, корень)
        return None
    return путь


def _remove_job_files(job: dict[str, Any], base: Path) -> int:
    """Удаляет каталог результатов и исходник задания. Возвращает объём."""
    import shutil

    freed = 0
    directory = _within(base, str(job.get("result_path") or ""))
    if directory is not None and directory.is_dir():
        try:
            freed += sum(f.stat().st_size for f in directory.rglob("*") if f.is_file())
            shutil.rmtree(directory, ignore_errors=True)
        except OSError as exc:
            log.warning("Не удалось удалить результаты %s: %s", directory, exc)
    path = _within(base, str(job.get("file_path") or ""))
    if path is not None:
        try:
            if path.is_file():
                freed += path.stat().st_size
                path.unlink()
        except OSError as exc:
            log.warning("Не удалось удалить исходник %s: %s", path, exc)
    return freed


def _row_to_llm(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for к in ("actions", "trackers", "scorecard", "warnings"):
        raw = out.get(к)
        if isinstance(raw, str):
            try:
                out[к] = json.loads(raw)
            except (TypeError, ValueError):
                out[к] = None
    if out.get("resolved") is not None:
        out["resolved"] = bool(out["resolved"])
    return out


def _serialize_json_fields(fields: dict[str, Any]) -> None:
    """Словари и списки в колонках JSON — строкой; остальное как есть."""
    for column in ("params", "waveform", "calibration", "quality_detail"):
        if isinstance(fields.get(column), (list, dict)):
            fields[column] = json.dumps(fields[column], ensure_ascii=False)


def _row_to_job(row: sqlite3.Row) -> dict[str, Any]:
    job = dict(row)
    for column, empty in (("params", {}), ("waveform", []), ("quality_detail", None),
                          ("calibration", None)):
        raw = job.get(column)
        if isinstance(raw, str):
            try:
                job[column] = json.loads(raw)
            except (TypeError, ValueError):
                job[column] = empty
        elif raw is None and column == "waveform":
            job[column] = []
    if isinstance(job.get("quality_flags"), str):
        job["quality_flags"] = [ф for ф in job["quality_flags"].split(",") if ф]
    return job
