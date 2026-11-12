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
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Settings
from .db_audit import ПроверкаБазы
from .db_content import СодержаниеБазы
from .db_llm import ЯзыковаяМодельБазы
from .db_maintenance import (  # noqa: F401 — прежние имена
    ОбслуживаниеБазы,
    _remove_job_files,
    _within,
    путь_отредактированной,
)
from .db_people import ЛюдиБазы
from .db_telephony import ТелефонияБазы
from .db_util import (  # noqa: F401 — прежние имена
    CLEANUP_BATCH,
    SERIES_MAX_BUCKETS,
    КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ,
    КОЛОНКИ_УКАЗАТЕЛЯ_ОТВЕТОВ,
    ОБЕЗЛИЧИТЬ_ЗВОНОК,
    ПОЛЯ_ЛИЧНЫЕ_ЗВОНКА,
    ПРЕДЕЛ_ЖУРНАЛА,
    УКАЗАТЕЛЬ_ОКНА,
    УКАЗАТЕЛЬ_ОТВЕТОВ,
    _экранировать_like,
    new_id,
    now,
)
from .errors import ConfigError, StorageError
from .instance import HOSTNAME, INSTANCE_ID, KERNEL_ID, own_host, process_alive
from .logging_setup import get_logger

log = get_logger("db")

SCHEMA_VERSION = 41




#: Такт служебного цикла: с этим шагом пишутся замеры нагрузки. Здесь он
#: потому, что от него зависит и запись, и чтение рядов.
SAMPLE_PERIOD_S = 20.0

#: Режимы журнала, которые сервер умеет держать (настройка `db_journal_mode`).
#: WAL — умолчание: читатели не мешают писателю. Журнал отката — для
#: нескольких машин над общим каталогом: WAL опирается на общую память одной
#: машины и по сети не работает (см. `fsinfo`).
РЕЖИМЫ_ЖУРНАЛА = ("wal", "delete")

#: Кеш страниц одного соединения, КБ. Соединение у каждого потока своё, а
#: потоков у сервера под сорок (пул запросов AnyIO): при 32 МБ на каждое
#: после тяжёлых запросов процесс держал 1,35 ГБ одних кешей страниц. В
#: пуле запросов кеш соединения почти не помогает — следующий запрос
#: приходит в другой поток, — а файловый кеш системы общий для всех.
КЕШ_СОЕДИНЕНИЯ_КБ = 8000


#: Отметка жизни экземпляра в `kv`: `instance:<машина:процесс>`.
РЕЕСТР = "instance:"



#: Сколько секунд отметка экземпляра считается свежей. Отметку ставит
#: служебный поток очереди раз в полминуты; срок тот же, что у заданий
#: (`job_queue.STALE_AFTER_S`): медленный, но живой сервер не должен
#: считаться умершим.
СВЕЖЕСТЬ_ОТМЕТКИ_С = 300.0


def _режим_журнала(значение: Any) -> str | None:
    """Режим из настройки: «wal», «delete» или None — «оставить как в файле»."""
    текст = str(значение or "").strip().lower()
    if not текст or текст == "auto":
        return None
    if текст not in РЕЖИМЫ_ЖУРНАЛА:
        raise ConfigError(f"Неизвестный режим журнала базы: {значение!r}",
                          hint="Бывают: wal, delete.")
    return текст


def _живые_отметки(строки: Sequence[Any], *, свежее: float) -> list[dict[str, Any]]:
    """Отметки экземпляров из `kv`, свежее порога, со своими признаками.

    Своя машина проверяется по процессу: отметка сервера, которого только
    что перезапустили, ещё свежая, а процесса уже нет — ждать пять минут,
    пока она устареет, незачем. Чужую машину так не проверить, и там
    решает только свежесть.
    """
    итог = []
    for строка in строки:
        ключ, значение, момент = строка[0], строка[1], строка[2]
        if float(момент or 0) < свежее:
            continue
        ид = str(ключ)[len(РЕЕСТР):]
        try:
            данные = json.loads(значение) if значение else {}
        except (TypeError, ValueError):
            данные = {}
        if not isinstance(данные, dict):
            данные = {}
        машина = str(данные.get("host") or ид.rsplit(":", 1)[0])
        свой = ид == INSTANCE_ID
        if not свой and own_host(ид) and not process_alive(ид):
            continue
        итог.append({**данные, "instance": ид, "host": машина, "self": свой,
                     "seen_at": float(момент or 0)})
    итог.sort(key=lambda з: (not з["self"], з["instance"]))
    return итог

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
        -- Версия 25: паузы по отраслевому порогу (четыре секунды) рядом со
        -- своим (две). Два счётчика отвечают на разные вопросы: обычный —
        -- «сколько раз собеседник задумался», длинный — «сколько раз он успел
        -- решить, что связь оборвалась». Отчёт заказчика считает второй, и
        -- без своей колонки его пришлось бы доставать из подробностей —
        -- то есть поднимать килобайты JSON на каждую запись.
        long_pauses       INTEGER,
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
        -- Версия 21: показатели сверх тональности. Каждый — своей колонкой,
        -- а не полем в `detail`: разрез «средняя понятность речи по
        -- сотруднику за квартал» по JSON означал бы поднять в память весь
        -- разбор каждой записи квартала ради одного числа из него.
        --
        -- Шкалы: у mood_* это −1…+1, у nps 0…10, у effort −5…+5, у
        -- intensity 1…5, у остальных 0…100. У stress и fatigue больше —
        -- хуже; у прочих индексов больше — лучше.
        mood              REAL,
        mood_shift        REAL,
        intensity         INTEGER,
        stress            INTEGER,
        effort            REAL,
        fatigue           INTEGER,
        clarity           INTEGER,
        accuracy          INTEGER,
        politeness        INTEGER,
        personalization   INTEGER,
        rhythm            INTEGER,
        filler_top        TEXT,
        diminutives       INTEGER,
        diminutive_rate   REAL,
        nps               INTEGER,
        nps_group         TEXT,
        nps_stated        INTEGER,
        -- Версия 28: названный клиентом балл — отдельно от предсказанного
        -- `nps`. `nps_said` — по шкале от нуля до десяти, `csat_said` — по
        -- пятибалльной: пятёрка из пяти и пятёрка из десяти — противоположные
        -- оценки, и в одной колонке их не различить.
        nps_said          INTEGER,
        csat_said         INTEGER,
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
        created_at  REAL NOT NULL,
        -- Версия 36: пояснения модели к причине и исходу, свои поля
        -- (`llm_fields`), итог скоркарты 0–100 с весами и проваленные
        -- критичные пункты.
        reason_why  TEXT,
        outcome_why TEXT,
        fields      TEXT,
        score       REAL,
        critical    TEXT
    ) WITHOUT ROWID
"""

#: Версия 36: проверка модели и поиск по смыслу.
#:
#: `llm_golden` — золотой набор: ответы человека по записи (причина, исход,
#: решён ли вопрос, трекеры, скоркарта). По нему прогоняется текущая или
#: новая настройка модели до выката (`llm_golden_runs`).
#: `llm_versions` — когда и на что менялась настройка модели: отметки на
#: трендах, чтобы «доля решённых выросла» не путалась с «поменяли подсказку».
#: `llm_vectors` — векторы смысла частей записи для поиска по смыслу.
_LLM_TRUST_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS llm_golden (
        job_id      TEXT PRIMARY KEY,
        answers     TEXT NOT NULL,
        note        TEXT DEFAULT '',
        created_by  TEXT DEFAULT '',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_golden_runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        label       TEXT DEFAULT '',
        status      TEXT NOT NULL DEFAULT 'идёт',
        overrides   TEXT,
        started_at  REAL NOT NULL,
        finished_at REAL,
        records     INTEGER DEFAULT 0,
        done        INTEGER DEFAULT 0,
        result      TEXT,
        error       TEXT DEFAULT '',
        created_by  TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_versions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        at          REAL NOT NULL,
        version     INTEGER NOT NULL,
        model       TEXT DEFAULT '',
        fingerprint TEXT NOT NULL,
        changes     TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_vectors (
        job_id      TEXT NOT NULL,
        part        INTEGER NOT NULL,
        model       TEXT DEFAULT '',
        dim         INTEGER NOT NULL,
        vector      BLOB NOT NULL,
        text        TEXT DEFAULT '',
        created_at  REAL NOT NULL,
        PRIMARY KEY (job_id, part)
    ) WITHOUT ROWID
    """,
    "CREATE INDEX IF NOT EXISTS idx_llm_versions_at ON llm_versions(at)",
]

#: Кеш ответов модели по отпечатку подсказки: одна и та же запись с теми
#: же подсказками второй раз модель не спрашивает.
#:
#: Версия 29: `job_id` — чья это запись. Пересказ разговора с именем и
#: номером клиента жил в кеше до срока хранения, а при «хранить бессрочно»
#: — вечно: удаление записи (кнопкой, по сроку, по требованию субъекта) его
#: не находило, потому что ключ кеша — отпечаток подсказки, а не задание.
_LLM_CACHE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS llm_cache (
        key         TEXT PRIMARY KEY,
        kind        TEXT,
        model       TEXT,
        response    TEXT,
        latency_ms  REAL,
        created_at  REAL NOT NULL,
        job_id      TEXT
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
#: считает статистику, и подпись от устаревшего счётчика не портится. Но
#: основа, которой не осталось ни в одной записи, уходит целиком — суточной
#: уборкой и удалением по требованию (`sweep_orphans`): фамилия клиента,
#: прозвучавшая только в стёртом разговоре, не должна пережить разговор.
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
    *_LLM_TRUST_SCHEMA,
    "CREATE INDEX IF NOT EXISTS idx_llm_results_created ON llm_results(created_at DESC)",
    # Уборка кеша по сроку: `created_at` в таблице WITHOUT ROWID стоит после
    # ответа модели, и без указателя часовая уборка читала все ответы —
    # больше гигабайта на двадцати тысячах. И по записи — для удаления.
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_job ON llm_cache(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_llm_results_outcome ON llm_results(outcome)",
    # --- версия 32: счёт и отбор ответов модели без чтения самих ответов ---
    f"CREATE INDEX IF NOT EXISTS {УКАЗАТЕЛЬ_ОТВЕТОВ} "
    f"ON llm_results({', '.join(КОЛОНКИ_УКАЗАТЕЛЯ_ОТВЕТОВ)})",
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
        vad_s             REAL,
        alignment_s       REAL,
        diarization_s     REAL,
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
        -- Заход 54: уведомление — состояние, а не разовая попытка.
        -- «pending» ждёт доставки (досылает служебный цикл, в том числе
        -- после перезапуска), «sending» — доставка идёт до webhook_next_at.
        webhook_attempts  INTEGER DEFAULT 0,
        webhook_next_at   REAL,
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
        silence_share     REAL,
        -- Версия 27: обратная запись в CRM. Отметка по заданию — чтобы
        -- примечание в карточке сделки появлялось один раз: раньше каждый
        -- пересчёт разбора и каждое открытие карточки записи слали в CRM
        -- новое примечание. «ждёт модель» — примечание отложено до
        -- смыслового разбора, иначе оно уходило без пересказа.
        crm_status        TEXT,
        crm_at            REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, priority DESC, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_crm ON jobs(crm_status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC)",
    # Версия 32. Порядок — как у списков: сначала новые, при равном времени
    # по номеру; статус, источник и владелец — довеском, чтобы условие по
    # ним проверялось по указателю, а строка задания читалась только у тех,
    # кто его прошёл.
    f"CREATE INDEX IF NOT EXISTS {УКАЗАТЕЛЬ_ОКНА} "
    "ON jobs(created_at DESC, id DESC, status, source, owner)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_model ON jobs(model, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_hash ON jobs(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_group ON jobs(group_id)",
    # Общий файл записи: контрольный прогон идёт по записи исходного задания,
    # и удаление одного из двух не должно уносить звук второго.
    "CREATE INDEX IF NOT EXISTS idx_jobs_file ON jobs(file_path)",
    # Уборка по сроку и сборщик показателей отбирают по времени окончания.
    "CREATE INDEX IF NOT EXISTS idx_jobs_finished ON jobs(finished_at)",
    # Задания с расшифровкой — частичный указатель. Полоса разбора (раз в
    # пятнадцать секунд), метрика покрытия и поиск неразобранного (раз в две
    # минуты) спрашивают «завершённые, с текстом, не контрольные». Условие
    # `text != ''` заставляло читать каждую расшифровку целиком, а `source`
    # и `owner` лежат в строке ПОСЛЕ текста — до них SQLite добирается
    # только через все страницы переполнения. На четырёх тысячах часовых
    # записей это 590 МБ чтения на вопрос, ответ на который — «нечего».
    # Условие указателя совпадает с условием запросов буква в букву: только
    # так SQLite знает, что указатель годится, и текст не читает вовсе.
    "CREATE INDEX IF NOT EXISTS idx_jobs_text_ready ON jobs(status, source, owner, created_at) "
    "WHERE text IS NOT NULL AND text != ''",
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
        data     TEXT,
        -- Какой сервер записал событие: соседи над общей базой пересылают
        -- своим подписчикам только чужие (см. events_bridge).
        instance TEXT DEFAULT ''
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
    # Уборка показателей по сроку идёт по одному времени, без имени.
    "CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts)",
    "CREATE INDEX IF NOT EXISTS idx_metrics_model ON metrics(model, name, ts DESC)",
    """
    CREATE TABLE IF NOT EXISTS qa_reviews (
        -- Контроль качества работы оператора — не то же самое, что очередь
        -- ручной проверки распознавания (`review_queue`). Там проверяют,
        -- верно ли машина расслышала слова; здесь человек оценивает работу
        -- другого человека, и цена ошибки другая: это попадает в разговор о
        -- премии. Поэтому отдельная таблица, своя история и обязательная
        -- оценка автомата рядом с оценкой человека — чтобы было видно, где
        -- они расходятся.
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT NOT NULL,
        agent       TEXT DEFAULT '',
        assigned_to TEXT DEFAULT '',
        assigned_by TEXT DEFAULT '',
        assigned_at REAL NOT NULL,
        due_at      REAL,
        status      TEXT NOT NULL DEFAULT 'pending',
        reviewer    TEXT DEFAULT '',
        reviewed_at REAL,
        -- Оценка автомата на момент назначения: если пересчитать разбор
        -- позже, сравнение «человек против автомата» перестало бы иметь
        -- смысл — автомат был бы уже другой.
        auto_score  REAL,
        score       REAL,
        agree       INTEGER,
        items       TEXT,
        comment     TEXT,
        reason      TEXT DEFAULT '',
        -- Версия 36: ответы проверяющего на вопросы скоркарты модели
        -- ({"id вопроса": "да|нет|н/п"}) — по ним считается согласие
        -- человека и модели по каждому вопросу.
        scorecard   TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit (
        -- Журнал доступа: кто, когда и что сделал. Отдельно от `events`,
        -- потому что отвечает на другой вопрос. `events` — это история
        -- задания («модель загружена», «распознавание началось»), и живёт
        -- она вместе с заданием: удалили запись — ушли и её события. Журнал
        -- доступа обязан пережить то, к чему относится: «кто удалил эту
        -- запись» — вопрос, который задают уже после удаления.
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        ts        REAL NOT NULL,
        -- Имя учётной записи или ключа. Хранится строкой, а не ссылкой:
        -- учётную запись удаляют, а журнал должен остаться читаемым.
        actor     TEXT DEFAULT '',
        actor_id  TEXT DEFAULT '',
        kind      TEXT DEFAULT '',
        role      TEXT DEFAULT '',
        action    TEXT DEFAULT '',
        method    TEXT DEFAULT '',
        path      TEXT DEFAULT '',
        status    INTEGER DEFAULT 0,
        ip        TEXT DEFAULT '',
        -- Цепочка хешей: sha256 от хеша предыдущей строки и полей этой.
        -- Правка или удаление строки посреди журнала рвёт цепочку, и
        -- проверка (`audit_verify`) показывает, где именно.
        hash      TEXT DEFAULT ''
    )
    """,
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
        locked_until    REAL DEFAULT 0,
        -- Версия 25: откуда взялась запись. «local» — заведена здесь и
        -- проверяется по своему паролю; «ldap» — пришла из каталога
        -- предприятия, пароля у неё нет, и роль ей переназначается при
        -- каждом входе по группам каталога.
        source          TEXT DEFAULT 'local'
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

    # --- версия 19: звонки с АТС ------------------------------------------
    #
    # Запись разговора приезжает с Asterisk сама, и одну и ту же её нельзя
    # завести дважды: `uniqueid` — первичный ключ, поэтому повторный проход
    # по журналу ничего не добавит, даже если позиция чтения потерялась.
    #
    # Таблица хранит и пропущенные звонки (`skipped` непустой): без этого
    # импортёр на каждом заходе заново искал бы файл записи для разговора,
    # которого никто не писал, и заново решал бы, что он короткий.
    """
    CREATE TABLE IF NOT EXISTS calls (
        uniqueid     TEXT PRIMARY KEY,
        job_id       TEXT,
        src          TEXT DEFAULT '',
        dst          TEXT DEFAULT '',
        clid         TEXT DEFAULT '',
        channel      TEXT DEFAULT '',
        dstchannel   TEXT DEFAULT '',
        context      TEXT DEFAULT '',
        disposition  TEXT DEFAULT '',
        direction    TEXT DEFAULT '',
        queue        TEXT DEFAULT '',
        agent        TEXT DEFAULT '',
        duration     INTEGER DEFAULT 0,
        billsec      INTEGER DEFAULT 0,
        answered     INTEGER DEFAULT 0,
        started_at   REAL DEFAULT 0,
        recording    TEXT DEFAULT '',
        userfield    TEXT DEFAULT '',
        accountcode  TEXT DEFAULT '',
        owner        TEXT DEFAULT '',
        skipped      TEXT DEFAULT '',
        imported_at  REAL,
        -- С какой станции приехал звонок. Станций может быть несколько, и
        -- без этой колонки архив филиала неотличим от архива головного
        -- офиса: ни разреза, ни ответа на вопрос «эта станция вообще
        -- присылает записи?».
        station      TEXT DEFAULT '',
        -- Версия 22: идентификатор звонка так, как его дала станция.
        --
        -- `uniqueid` — ключ архива, и он обязан быть уникальным на весь
        -- сервер. У Asterisk он уникален только внутри одной АТС:
        -- «эпоха.счётчик», где счётчик локален станции и сбрасывается при
        -- её перезапуске. Две станции в одну секунду дают одинаковый
        -- идентификатор, и второй звонок молча выбрасывался как «уже
        -- импортирован». Поэтому ключ собирается как «станция:идентификатор»,
        -- а настоящий идентификатор станции живёт здесь: по нему ищется
        -- файл записи и по нему звонок узнают на самой АТС.
        pbx_uid      TEXT DEFAULT '',
        -- Версия 27: когда звонок впервые отложили. Срок жизни отложенного
        -- считался от `imported_at`, а его обновляет каждое повторное
        -- откладывание — и безнадёжный звонок (запись нулевой длины, не
        -- задан каталог) крутился в отложенных вечно.
        deferred_at  REAL,
        -- Версия 35: разговор целиком. Asterisk 12+ даёт каждой ноге свой
        -- uniqueid, а общий для всех ног разговора — linkedid (у первой ноги
        -- он равен её uniqueid); sequence — порядок строки CDR. По ним в
        -- карточке звонка видны остальные ноги: перевод, очередь, второй
        -- оператор. Источники, которые их не знают, оставляют пустыми.
        linkedid     TEXT DEFAULT '',
        sequence     INTEGER DEFAULT 0,
        -- Версия 37: набранный городской номер (DID) — линия, на которую
        -- звонили (FreePBX пишет его в CDR отдельной колонкой).
        did          TEXT DEFAULT ''
    )
    """,
    # --- версия 23: сотрудники, агенты на АТС, очередь к языковой модели ---
    """
    CREATE TABLE IF NOT EXISTS employees (
        id            TEXT PRIMARY KEY,
        -- Внешний ключ источника: по нему запись узнаётся при повторном
        -- импорте. Для справочника это «фамилия|имя|отчество» или
        -- внутренний номер — что устойчивее в конкретной выгрузке.
        external_id   TEXT DEFAULT '',
        source        TEXT DEFAULT 'ручной',
        active        INTEGER DEFAULT 1,
        active_until  TEXT DEFAULT '',
        last_name     TEXT DEFAULT '',
        first_name    TEXT DEFAULT '',
        middle_name   TEXT DEFAULT '',
        position      TEXT DEFAULT '',
        department    TEXT DEFAULT '',
        phone_work    TEXT DEFAULT '',
        phone_ext     TEXT DEFAULT '',
        phone_mobile  TEXT DEFAULT '',
        email         TEXT DEFAULT '',
        suppliers     TEXT DEFAULT '',
        note          TEXT DEFAULT '',
        owner         TEXT DEFAULT '',
        created_at    REAL,
        updated_at    REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_employees_ext ON employees(phone_ext)",
    "CREATE INDEX IF NOT EXISTS idx_employees_dept ON employees(department, last_name)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_employees_external ON employees(source, external_id)",
    """
    CREATE TABLE IF NOT EXISTS agents (
        id           TEXT PRIMARY KEY,
        name         TEXT DEFAULT '',
        host         TEXT DEFAULT '',
        station      TEXT DEFAULT '',
        version      TEXT DEFAULT '',
        asterisk     TEXT DEFAULT '',
        os           TEXT DEFAULT '',
        source       TEXT DEFAULT '',
        first_seen   REAL,
        last_seen    REAL,
        calls_sent   INTEGER DEFAULT 0,
        files_sent   INTEGER DEFAULT 0,
        bytes_sent   INTEGER DEFAULT 0,
        errors       INTEGER DEFAULT 0,
        last_error   TEXT DEFAULT '',
        state        TEXT DEFAULT '',
        -- Задание агенту: собрать всё, собрать за период. Ставится из
        -- раздела «АТС», забирается агентом на следующем обращении.
        command      TEXT DEFAULT '',
        command_at   REAL,
        enabled      INTEGER DEFAULT 1,
        owner        TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agents_seen ON agents(last_seen DESC)",
    # --- версия 34: контроль качества — обратный звонок с опросом ----------
    #
    # Строка — один опрос: клиент, которому надо перезвонить после разговора
    # с добавочным из группы контроля. Она же и очередь обзвона (`status`,
    # `due_at`), и итог (`answers`, `score`). `call_key` — ключ исходного
    # звонка в `calls`: уникален, поэтому один разговор не даст двух
    # опросов, сколько бы раз его ни сохранили; у пробного звонка он пуст.
    """
    CREATE TABLE IF NOT EXISTS qc_surveys (
        id           TEXT PRIMARY KEY,
        call_key     TEXT,
        kind         TEXT DEFAULT 'опрос',
        group_name   TEXT DEFAULT '',
        ext          TEXT DEFAULT '',
        number       TEXT DEFAULT '',
        station      TEXT DEFAULT '',
        owner        TEXT DEFAULT '',
        job_id       TEXT,
        direction    TEXT DEFAULT '',
        call_at      REAL,
        created_at   REAL,
        due_at       REAL,
        started_at   REAL,
        answered_at  REAL,
        finished_at  REAL,
        status       TEXT DEFAULT 'ждёт',
        attempts     INTEGER DEFAULT 0,
        sip_code     INTEGER DEFAULT 0,
        error        TEXT DEFAULT '',
        answers      TEXT DEFAULT '',
        score        REAL,
        score_pct    REAL,
        talk_s       REAL DEFAULT 0,
        -- Запись опроса (qc_record): путь к WAV, слева клиент, справа сервер.
        recording    TEXT DEFAULT ''
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_qc_call ON qc_surveys(call_key)",
    "CREATE INDEX IF NOT EXISTS idx_qc_due ON qc_surveys(status, due_at)",
    "CREATE INDEX IF NOT EXISTS idx_qc_number ON qc_surveys(number, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qc_created ON qc_surveys(created_at DESC)",
    # Номера, на которые больше не звонить: клиент нажал кнопку отказа или
    # номер внёс администратор.
    """
    CREATE TABLE IF NOT EXISTS qc_stoplist (
        number     TEXT PRIMARY KEY,
        added_at   REAL,
        reason     TEXT DEFAULT '',
        survey_id  TEXT DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_queue (
        job_id       TEXT PRIMARY KEY,
        kind         TEXT DEFAULT 'разбор',
        state        TEXT DEFAULT 'ждёт',
        priority     INTEGER DEFAULT 50,
        enqueued_at  REAL,
        started_at   REAL,
        finished_at  REAL,
        attempts     INTEGER DEFAULT 0,
        error        TEXT DEFAULT '',
        latency_ms   INTEGER,
        calls        INTEGER DEFAULT 0,
        chunks       INTEGER DEFAULT 0,
        source       TEXT DEFAULT '',
        owner        TEXT DEFAULT '',
        -- Версия 27: какой сервер разбирает запись и когда он последний раз
        -- подтвердил, что жив. Без них второй сервер на общей базе при
        -- старте возвращал в очередь ЧУЖОЙ идущий разбор и разбирал его
        -- второй раз.
        instance     TEXT DEFAULT '',
        heartbeat_at REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_llmq_state ON llm_queue(state, priority DESC, enqueued_at)",
    "CREATE INDEX IF NOT EXISTS idx_llmq_finished ON llm_queue(finished_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_calls_started ON calls(started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_calls_station ON calls(station, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_calls_job ON calls(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_calls_linked ON calls(station, linkedid)",
    # Версия 37: события очередей из queue_log — ожидание, брошенные,
    # пропущенные операторами. Ключ — станция, звонок, событие, время и
    # оператор: повторное чтение журнала строк не задваивает.
    """
    CREATE TABLE IF NOT EXISTS queue_events (
        station  TEXT NOT NULL DEFAULT '',
        ts       REAL NOT NULL,
        callid   TEXT NOT NULL DEFAULT '',
        queue    TEXT NOT NULL DEFAULT '',
        agent    TEXT NOT NULL DEFAULT '',
        event    TEXT NOT NULL,
        wait_s   REAL,
        ring_s   REAL,
        talk_s   REAL,
        PRIMARY KEY (station, callid, event, ts, agent)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_queue_events_ts ON queue_events(station, ts)",
    # Версия 39: запись разговора с переводами — по участникам (см.
    # `telephony.segments`): сколько звучал каждый, его слова и тональность
    # своих отрезков. Разрезы по сотрудникам берут отсюда участие в
    # переводах, а не отдают всю запись добавочному записанной ноги.
    """
    CREATE TABLE IF NOT EXISTS agent_segments (
        job_id      TEXT NOT NULL,
        agent       TEXT NOT NULL,
        talk_s      REAL DEFAULT 0,
        words       INTEGER DEFAULT 0,
        replicas    INTEGER DEFAULT 0,
        sentiment   REAL,
        share       REAL,
        recorded    INTEGER DEFAULT 0,
        legs        INTEGER DEFAULT 0,
        transfers   INTEGER DEFAULT 0,
        bridges     INTEGER DEFAULT 0,
        computed_at REAL,
        PRIMARY KEY (job_id, agent)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_agent_segments_agent ON agent_segments(agent)",
    # События перевода из AMI (`telephony.transfers`): точная граница
    # отрезков участников. Номер вне станции не хранится.
    """
    CREATE TABLE IF NOT EXISTS call_transfers (
        station    TEXT NOT NULL,
        linkedid   TEXT NOT NULL,
        ts         REAL NOT NULL,
        kind       TEXT NOT NULL,
        from_agent TEXT NOT NULL DEFAULT '',
        to_agent   TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (station, linkedid, ts, kind)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_call_transfers_ts ON call_transfers(ts)",
    # Версия 40: вход в мост и выход из него (`telephony.transfers`) — окно
    # участника по тому, когда записанный канал его слышит. Имя канала не
    # хранится: в нём бывает номер клиента.
    """
    CREATE TABLE IF NOT EXISTS bridge_events (
        station  TEXT NOT NULL,
        linkedid TEXT NOT NULL,
        ts       REAL NOT NULL,
        kind     TEXT NOT NULL,
        uniqueid TEXT NOT NULL,
        bridge   TEXT NOT NULL,
        agent    TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (station, linkedid, uniqueid, bridge, kind, ts)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bridge_events_ts ON bridge_events(ts)",
    # Версия 41: разметка записей опросов «человек» или «автоответчик» для
    # модели автоответчика (`qc.amd_model`). Признаки хранятся с меткой:
    # запись опроса уходит по сроку, а обучать можно и после.
    """
    CREATE TABLE IF NOT EXISTS qc_amd_labels (
        survey_id  TEXT PRIMARY KEY,
        label      TEXT NOT NULL,
        features   TEXT NOT NULL,
        labeled_by TEXT DEFAULT '',
        labeled_at REAL
    )
    """,
    # Версия 33: добавочный по заданию без чтения строки звонка — им
    # подписываются таблицы «Голосовой аналитики» и разрез по добавочным.
    "CREATE INDEX IF NOT EXISTS idx_calls_job_agent ON calls(job_id, agent)",
    "CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_calls_queue ON calls(queue, started_at DESC)",
    # Повторные обращения ищутся по номеру звонящего в окне в неделю: без
    # этого индекса решение с первого обращения считается перебором всего
    # архива на каждый звонок.
    "CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src, started_at)",
    # Уборка журнала звонков по сроку и множество известных звонков для
    # обхода каталога записей — по времени импорта.
    "CREATE INDEX IF NOT EXISTS idx_calls_imported ON calls(imported_at)",
    # Отдан ли файл записи другому звонку станции — подбор записи по
    # номерам спрашивает это на каждом звонке (см. `call_has_recording`).
    "CREATE INDEX IF NOT EXISTS idx_calls_recording ON calls(recording)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qa_status ON qa_reviews(status, assigned_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qa_job ON qa_reviews(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_agent ON qa_reviews(agent, reviewed_at DESC)",
    # Заход 54: уведомления, ждущие доставки, — без чтения всех заданий.
    "CREATE INDEX IF NOT EXISTS idx_jobs_webhook ON jobs(webhook_status, webhook_next_at) "
    "WHERE webhook_status IN ('pending', 'sending')",
]


def _ожидаемые_колонки() -> dict[str, dict[str, str]]:
    """Колонки каждой таблицы — из самой схемы, выполненной в памяти.

    Набор сверяется при каждом открытии базы: колонку, которой в ней нет,
    дописывает `ALTER TABLE`. Прежде он был написан руками, с комментарием
    «собран из _SCHEMA, поэтому не может разойтись» — и разошёлся: таблиц
    сотрудников, агентов и очереди модели в нём не было вовсе, и новая
    колонка в любой из них на базе прежней версии не появилась бы никогда —
    каждый запрос к таблице падал бы с «no such column».

    Объявление — тип, `NOT NULL` при значении по умолчанию и само значение:
    `ALTER TABLE … ADD COLUMN` принимает `NOT NULL` только вместе с ним.
    """
    соединение = sqlite3.connect(":memory:")
    try:
        for statement in _SCHEMA:
            соединение.execute(statement)
        таблицы = [строка[0] for строка in соединение.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        итог: dict[str, dict[str, str]] = {}
        for таблица in таблицы:
            колонки: dict[str, str] = {}
            for _, имя, тип, обязательна, умолчание, _ in соединение.execute(
                    f"PRAGMA table_info({таблица})"):
                объявление = str(тип or "").strip()
                if умолчание is not None:
                    if обязательна:
                        объявление += " NOT NULL"
                    объявление += f" DEFAULT {умолчание}"
                колонки[str(имя)] = объявление
            итог[таблица] = колонки
        return итог
    finally:
        соединение.close()


#: Ожидаемый набор колонок — сверяется при миграции (см. `_ожидаемые_колонки`).
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = _ожидаемые_колонки()


#: С какой длины последнее слово поиска ищется как начало слова.
ПРЕФИКС_ОТ = 2

#: Сколько секунд поток помнит ответ указателя на тот же поиск. Один запрос
#: списка «Результатов» спрашивал указатель трижды — список, счётчик и
#: признак обрезки, — и на большом архиве каждое обращение стоило столько же,
#: сколько первое.
ПАМЯТЬ_ПОИСКА_С = 5.0


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

    Продолжение — только от двух букв. Одна буква со звёздочкой («д»*) —
    это полсловаря: на четырёхстах тысячах реплик такой запрос шёл больше
    полусекунды, а найти по нему нельзя ничего осмысленного. Однобуквенное
    последнее слово ищется как слово.
    """
    слова = re.findall(r"[^\W_]+", text or "", flags=re.UNICODE)
    if not слова:
        return ""
    # Больше десятка слов в запросе — это уже не поиск, а вставленный абзац;
    # каждое слово стоит времени, а пользы за пределами первых нет.
    слова = слова[:12]
    части = [f'"{w}"' for w in слова[:-1]]
    последнее = слова[-1]
    части.append(f'"{последнее}"*' if len(последнее) >= ПРЕФИКС_ОТ else f'"{последнее}"')
    return " ".join(части)


def _нижний(значение: Any) -> Any:
    """LOWER() для SQLite, знающий кириллицу. NULL остаётся NULL."""
    return значение.lower() if isinstance(значение, str) else значение


def _есть_метка(строка: Any, метка: Any) -> int:
    """Есть ли метка в строке «через запятую» — для SQL.

    Метки и признаки качества лежат одной строкой, и сравнение образцом
    LIKE зависит от того, как именно её набрали: «срочно, согласие» и
    «срочно,согласие» — одна и та же пара меток. Аналитика читает колонку
    через `split(",")` и `strip()`; раскрытие числа в список записей обязано
    читать её так же, иначе список не сойдётся с числом, которое раскрывали.
    """
    if not isinstance(строка, str) or not строка:
        return 0
    искомая = str(метка or "").strip()
    return int(any(часть.strip() == искомая for часть in строка.split(",")))


class QueryTimeout(Exception):
    """Запрос на чтение не уложился в отведённый срок и был прерван.

    Не ошибка сервера и не `ASRHubError`: это ожидаемый исход запроса с
    предельным сроком, и решает, что с ним делать, тот, кто срок поставил.
    """


def _порядок_заданий(order: str, соединение: bool) -> str:
    """Порядок выборки заданий в виде SQL.

    Отдельно стоит «по сроку». В SQLite `ORDER BY deadline ASC` ставит NULL
    ПЕРВЫМИ, то есть задания без срока — впереди срочных: ровно наоборот
    смыслу. Поэтому сначала «срок есть», потом сам срок, и только потом
    время постановки. Без этого предварительная выборка политики «по сроку»
    шла по времени создания, и срочное задание, поставленное последним, в
    окно выборки не попадало вовсе: на очереди из шестисот заданий при окне
    в пятьсот его не выбирали, пока не разгребётся всё остальное — то есть
    политика «по сроку» не работала именно там, где она нужна.
    """
    п = "jobs." if соединение else ""
    if order == "deadline ASC":
        return (f"({п}deadline IS NULL), {п}deadline ASC, {п}created_at ASC, {п}id ASC")
    # Внутри одного приоритета — по времени постановки. Без второго ключа
    # SQLite отдавал сначала задания в «queued», потом в «retry», и задание,
    # у которого время повтора давно наступило, не попадало в окно выборки
    # планировщика, пока очередь не станет меньше окна.
    if order in ("priority DESC", "priority ASC"):
        return f"{п}{order}, {п}created_at ASC, {п}id ASC"
    # Номер задания — последним ключом. Задания пакета заводятся в одну
    # секунду, и при равном времени SQLite вправе отдавать их в любом
    # порядке: страница вторая повторяла строку с первой, а соседняя не
    # попадала ни на одну.
    направление = "DESC" if order.endswith("DESC") else "ASC"
    основа = f"{п}{order}" if соединение and not order.startswith("jobs.") else order
    return f"{основа}, {п}id {направление}"


class Database(ТелефонияБазы, СодержаниеБазы, ЯзыковаяМодельБазы, ЛюдиБазы, ПроверкаБазы,
               ОбслуживаниеБазы):
    """Тонкая обёртка над SQLite с пулом соединений по потокам."""

    def __init__(self, path: Path | str, *, journal_mode: str | None = None):
        self.path = Path(path)
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._closed = False
        #: Есть ли полнотекстовый указатель. False — сборка SQLite без FTS5;
        #: поиск тогда работает перебором, как раньше.
        self.fts_ready = False
        #: Какой режим журнала просили: «wal», «delete» или None — «как в
        #: файле» (новая база получает WAL). Сервер передаёт настройку
        #: `db_journal_mode`; сценарии командной строки — ничего, чтобы не
        #: переключать режим под работающим сервером.
        self._журнал_просили = _режим_журнала(journal_mode)
        #: Режим, в котором база работает на самом деле, — после первого
        #: соединения. Им же выбирается `synchronous` у остальных.
        self.journal_mode = ""
        self._журнал_выбран = False
        #: Поколение реплик: растёт при каждой их записи и удалении. Память
        #: поиска (`jobs_matching`) действительна только в своём поколении.
        self._поколение_реплик = 0
        #: Поколение заданий: растёт на каждом заведении, удалении и смене
        #: статуса задания (не на прогрессе). По нему кеш сводных отчётов
        #: понимает, что считать заново (`Analytics.full_report`).
        self.поколение_заданий = 0
        #: Указатели, про которые уже известно, что они в базе есть.
        self._известные_указатели: set[str] = set()
        #: Что сервер рассказывает о себе в отметке жизни (версия, файловая
        #: система) — заполняет `create_app`.
        self.сведения_отметки: dict[str, Any] = {}
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
            try:
                # Ожидание занятой базы — первым делом: всё ниже может
                # упереться в чужую блокировку и без него падало бы сразу.
                conn.execute("PRAGMA busy_timeout=30000")
                if not self._журнал_выбран:
                    with self._write_lock:
                        if not self._журнал_выбран:
                            self.journal_mode = self._выбрать_журнал(conn)
                            self._журнал_выбран = True
                # WAL переживает закрытие соединения: новое открывает базу в
                # нём само, и ставить его каждому потоку заново незачем — а
                # под соседним сервером в режиме отката и вредно. У журнала
                # отката NORMAL при сбое питания может испортить базу, у WAL —
                # нет: отсюда разный `synchronous`.
                conn.execute("PRAGMA synchronous="
                             + ("NORMAL" if self.journal_mode == "wal" else "FULL"))
                conn.execute("PRAGMA foreign_keys=ON")
                # Освобождённые страницы — затирать. Удаление по требованию
                # вычищало строки и указатель, а фамилия и номер оставались в
                # свободных страницах файла и уезжали в следующую резервную
                # копию — до VACUUM. В сборках Debian и Ubuntu это включено
                # при компиляции, в других (python.org для Windows и macOS)
                # — нет, и стирание зависело от того, чей Python стоит.
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute(f"PRAGMA cache_size=-{КЕШ_СОЕДИНЕНИЯ_КБ}")
                conn.execute(f"PRAGMA journal_size_limit={ПРЕДЕЛ_ЖУРНАЛА}")
            except sqlite3.DatabaseError as exc:
                with contextlib.suppress(sqlite3.Error):
                    conn.close()
                raise StorageError(f"База повреждена или недоступна: {self.path}: {exc}",
                                   hint="Проверьте файл базы (PRAGMA integrity_check) и "
                                        "права на каталог данных; при порче — "
                                        "восстановите базу из резервной копии.") from exc
            # LOWER() в SQLite понимает только латиницу: «Иванов» она
            # оставляет как есть, и поиск по справочнику не находил
            # человека, если регистр не совпал буква в букву. Подменяем
            # своей — питоновский str.lower() знает все алфавиты.
            for имя, число, функция in (("lower", 1, _нижний),
                                        ("has_tag", 2, _есть_метка)):
                try:
                    conn.create_function(имя, число, функция, deterministic=True)
                except (sqlite3.NotSupportedError, TypeError):    # старая сборка
                    conn.create_function(имя, число, функция)
            self._local.conn = conn
        return conn

    def _выбрать_журнал(self, conn: sqlite3.Connection) -> str:
        """Ставит просимый режим журнала первому соединению; возвращает итог.

        Режим — свойство файла, а не соединения: WAL, однажды включённый,
        переживает закрытие, и все соединения всех процессов работают в нём.
        Поэтому менять его можно только когда над базой не работает никто:
        сосед со старым режимом и мы с новым видят базу по-разному, и это
        кончается порчей. Сосед виден по его отметке в `kv` (`instance:…`);
        есть живой сосед — режим не меняется, а журнал сервера говорит, что
        делать. Своё соединение здесь единственное: зовётся из `__init__`.
        """
        текущий = str(conn.execute("PRAGMA journal_mode").fetchone()[0] or "").lower()
        if текущий == "memory" or str(self.path) == ":memory:":
            return текущий
        новая = int(conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0]) == 0
        желаемый = self._журнал_просили or ("wal" if новая else "")
        if not желаемый or желаемый == текущий:
            return текущий
        if not новая:
            соседи = [с for с in self._отметки(conn) if not с["self"]]
            if соседи:
                log.error(
                    "Режим журнала базы не сменён (%s → %s): над ней работают другие "
                    "серверы — %s. Остановите все серверы над этой базой, задайте "
                    "всем одинаковый db_journal_mode и запускайте заново.",
                    текущий, желаемый, ", ".join(с["instance"] for с in соседи))
                return текущий
        try:
            итог = str(conn.execute(f"PRAGMA journal_mode={желаемый}").fetchone()[0]
                       or "").lower()
        except sqlite3.Error as exc:
            log.error("Режим журнала базы не сменён (%s → %s): %s", текущий, желаемый, exc)
            return текущий
        if итог != желаемый:
            log.warning("База осталась в режиме «%s» вместо «%s»: файловая система "
                        "не дала его сменить.", итог, желаемый)
        elif not новая:
            log.warning("Режим журнала базы сменён: %s → %s", текущий, итог)
        return итог

    @staticmethod
    def _отметки(conn: sqlite3.Connection, *,
                 свежесть: float = СВЕЖЕСТЬ_ОТМЕТКИ_С) -> list[dict[str, Any]]:
        """Живые отметки экземпляров — прямо через соединение, без `self.conn`."""
        try:
            строки = conn.execute(
                "SELECT key, value, ts FROM kv WHERE key LIKE ? ESCAPE '\\'",
                (_экранировать_like(РЕЕСТР) + "%",)).fetchall()
        except sqlite3.Error:
            return []                       # таблицы ещё нет — новая база
        return _живые_отметки(строки, свежее=now() - свежесть)

    # --- экземпляры над общей базой ---------------------------------------

    def instance_beat(self, **сведения: Any) -> None:
        """Отметка «этот сервер жив» — раз в полминуты из служебного потока.

        Задания показывают только тех, кто сейчас считает; простаивающий
        сосед был невидим, и ни смена режима журнала, ни восстановление
        базы, ни VACUUM не знали, что над базой работает кто-то ещё.
        """
        данные = {"host": HOSTNAME, "pid": os.getpid(), "kernel": KERNEL_ID,
                  "journal": self.journal_mode, **self.сведения_отметки, **сведения}
        self.set_kv(f"{РЕЕСТР}{INSTANCE_ID}", данные)

    def instance_forget(self) -> None:
        """Снимает свою отметку — при штатной остановке сервера."""
        try:
            self.execute("DELETE FROM kv WHERE key=?", (f"{РЕЕСТР}{INSTANCE_ID}",))
        except StorageError as exc:
            log.debug("Отметка экземпляра не снята: %s", exc)

    def instances(self, *, свежесть: float = СВЕЖЕСТЬ_ОТМЕТКИ_С) -> list[dict[str, Any]]:
        """Живые экземпляры над этой базой, свой — первым."""
        return self._отметки(self.conn, свежесть=свежесть)

    def other_instances(self, *, свежесть: float = СВЕЖЕСТЬ_ОТМЕТКИ_С) -> list[dict[str, Any]]:
        """Живые соседи — все, кроме себя."""
        return [э for э in self.instances(свежесть=свежесть) if not э["self"]]

    def instances_prune(self, *, старше: float = 7 * 86400) -> int:
        """Убирает отметки давно умерших экземпляров."""
        return self.execute("DELETE FROM kv WHERE key LIKE ? ESCAPE '\\' AND ts < ?",
                            (_экранировать_like(РЕЕСТР) + "%", now() - старше))

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
                _значение_не_того_вида(exc)
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
            _значение_не_того_вида(exc)
            # Текст запроса — в журнал сервера, а не в ответ: ответ видит
            # любой клиент, и кусок SQL в нём — это рассказ об устройстве
            # базы тому, кто его подбирает.
            log.warning("Ошибка чтения из базы: %s; запрос: %s", exc, sql[:200])
            raise StorageError(f"Ошибка чтения из базы: {exc}") from exc

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def query_scalar(self, sql: str, params: Sequence[Any] = (), default: Any = 0) -> Any:
        """Первое значение первой строки; нет строки или там NULL — `default`.

        `query_one(...)["n"]` падал бы на пустом ответе и не проходит
        проверку типов: строки может не быть.
        """
        строка = self.query_one(sql, params)
        if строка is None or строка[0] is None:
            return default
        return строка[0]

    def has_index(self, name: str) -> bool:
        """Есть ли в базе указатель с таким именем.

        Запрос с `INDEXED BY` по указателю, которого нет, — ошибка, а не
        медленный запрос. Указатели создаются при открытии базы, но создание
        может не пройти (см. `_catch_up_indexes`: это повод работать
        медленнее, а не падать), поэтому тот, кто просит базу идти по
        указателю, сначала спрашивает здесь. Утвердительный ответ
        запоминается: указатели на ходу не удаляют.
        """
        if name in self._известные_указатели:
            return True
        строка = self.query_one(
            "SELECT 1 AS x FROM sqlite_master WHERE type='index' AND name=?", (name,))
        if строка:
            self._известные_указатели.add(name)
        return bool(строка)

    def query_bounded(self, sql: str, params: Sequence[Any] = (), *,
                      seconds: float = 5.0) -> list[sqlite3.Row]:
        """Чтение с предельным сроком: дольше — прерывается `QueryTimeout`.

        Нужно запросам, чья цена зависит от того, что спросил человек, а не
        от того, что написал программист: редкий отбор по всему архиву
        читает архив целиком, и на миллионе записей это уже не «мгновенно».
        Обычный запрос в таком случае держал бы поток пула, пока не
        дочитает, — а человек давно закрыл список и открыл соседний.

        Срок проверяет сам SQLite между шагами своей машины: соединение у
        потока своё, так что обработчик хода никому больше не мешает и
        снимается сразу после запроса.
        """
        conn = self.conn
        срок = time.monotonic() + max(0.05, float(seconds))
        conn.set_progress_handler(lambda: 1 if time.monotonic() > срок else 0, 5000)
        try:
            return list(conn.execute(sql, params))
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                raise QueryTimeout(sql[:120]) from exc
            _значение_не_того_вида(exc)
            log.warning("Ошибка чтения из базы: %s; запрос: %s", exc, sql[:200])
            raise StorageError(f"Ошибка чтения из базы: {exc}") from exc
        except sqlite3.Error as exc:
            _значение_не_того_вида(exc)
            log.warning("Ошибка чтения из базы: %s; запрос: %s", exc, sql[:200])
            raise StorageError(f"Ошибка чтения из базы: {exc}") from exc
        finally:
            conn.set_progress_handler(None, 0)

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
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise StorageError(f"Не удалось начать обновление схемы базы: {exc}") from exc
            try:
                # Версию перечитываем уже под блокировкой записи: два сервера,
                # запущенные разом на старой схеме, оба видели прежний номер
                # до BEGIN, и второй, дождавшись первого, повторял починки
                # данных поверх уже обновлённой базы.
                current = conn.execute("PRAGMA user_version").fetchone()[0]
                if current >= SCHEMA_VERSION:
                    conn.execute("COMMIT")
                    return
                # Колонки добавляются ПЕРЕД схемой: индексы из _SCHEMA могут
                # ссылаться на поля, которых в старой таблице ещё нет.
                self._add_missing_columns(conn)
                for statement in _SCHEMA:
                    if statement.strip().upper().startswith("CREATE UNIQUE INDEX"):
                        self._уникальный_указатель(conn, statement)
                    else:
                        conn.execute(statement)
                # Починки данных — после колонок и схемы: им нужны и новые
                # поля, и новые указатели. Каждая идёт один раз, при
                # переходе через свою версию.
                if current < 24:
                    self._починка_ключей_звонков(conn)
                if current < 26:
                    self._вычистить_секреты_заданий(conn)
                if current < 28:
                    self._разделить_названный_nps(conn)
                    self._снять_качество_с_переписки(conn)
                if current < 31:
                    self._движок_вместо_auto(conn)
                if current < 33:
                    self._итоги_без_расшифровки(conn)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise StorageError(f"Не удалось применить миграции: {exc}") from exc

    @staticmethod
    def _уникальный_указатель(conn: sqlite3.Connection, statement: str) -> None:
        """Уникальный указатель — в своей точке сохранения, а не всей миграцией.

        На базе, где до появления указателя завелись дубли («Admin» и
        «admin»), `CREATE UNIQUE INDEX` падает. `_catch_up_indexes` в таком
        случае предупреждал и шёл дальше, а миграция на тот же указатель
        откатывала ВСЁ: база, сегодня работающая с предупреждением,
        следующим выпуском с новым номером схемы не поднималась вовсе — с
        сообщением, не называющим ни таблицы, ни способа лечения. Теперь
        указатель пропускается с предупреждением и перечнем дублей, а
        остальная миграция идёт; `_catch_up_indexes` попробует снова при
        следующем запуске, когда дубли уберут.
        """
        conn.execute("SAVEPOINT уникальный")
        try:
            conn.execute(statement)
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK TO уникальный")
            conn.execute("RELEASE уникальный")
            log.warning("Уникальный указатель не создан (%s): %s. Дубли: %s. Уберите "
                        "или переименуйте их — указатель создастся при следующем запуске.",
                        statement.split(" ON ")[0].split()[-1], exc,
                        Database._дубли(conn, statement) or "не найдены")
            return
        conn.execute("RELEASE уникальный")

    @staticmethod
    def _дубли(conn: sqlite3.Connection, statement: str) -> str:
        """Значения, которые мешают уникальному указателю, — до десяти."""
        try:
            таблица, поля = statement.split(" ON ", 1)[1].split("(", 1)
            поля = поля.rsplit(")", 1)[0]
            строки = conn.execute(
                f"SELECT {поля}, COUNT(*) FROM {таблица.strip()} "     # noqa: S608
                f"GROUP BY {поля} HAVING COUNT(*) > 1 LIMIT 10").fetchall()
        except (sqlite3.Error, ValueError, IndexError):
            return ""
        return "; ".join(" / ".join(str(з) for з in строка[:-1]) + f" ×{строка[-1]}"
                         for строка in строки)

    #: Порог короткой записи для архива до версии 33 — значение
    #: `audio_min_duration_s` по умолчанию. Настроек у миграции нет: она
    #: идёт при открытии базы, до того как их прочли.
    ПОРОГ_КОРОТКОЙ_АРХИВ_С = 2.0

    def _итоги_без_расшифровки(self, conn: sqlite3.Connection) -> None:
        """Прежние «ошибки» молчащих и коротких записей — в их итоги.

        До версии 33 запись без речи и пустой файл кончались состоянием
        `failed` и попадали в ошибки, в долю отказов и в «требуют внимания».
        Новые задания получают `no_speech` и `short` сами (см.
        `job_queue.ИТОГ_БЕЗ_РАСШИФРОВКИ`); здесь то же делается с архивом,
        иначе отчёт за прошлый месяц показывал бы те же ошибки, что и
        раньше. Узнаются они по коду отказа и по тексту, который писал
        этот же сервер: «близка к нулю», «нет ни одного отсчёта звука»,
        «Файл пуст».
        """
        порог = self.ПОРОГ_КОРОТКОЙ_АРХИВ_С
        conn.execute(
            "UPDATE jobs SET status='short', stage='короткая запись', progress=1.0, "
            "error_code='audio_too_short' "
            "WHERE status='failed' AND ("
            "  (error_code='no_speech' AND COALESCE(media_duration_s, 0) > 0 "
            "   AND media_duration_s < ?)"
            "  OR (error_code='audio_error' AND ("
            "      error_message LIKE '%близка к нулю%' "
            "      OR error_message LIKE '%нет ни одного отсчёта звука%' "
            "      OR error_message LIKE 'Файл пуст:%')))", (порог,))
        conn.execute(
            "UPDATE jobs SET status='no_speech', stage='без речи', progress=1.0 "
            "WHERE status='failed' AND error_code='no_speech'")

    def _движок_вместо_auto(self, conn: sqlite3.Connection) -> None:
        """«auto» в колонке движка заменяется движком модели.

        До версии 31 задание записывало в колонку движок из параметров, а
        там почти всегда «auto»: разрез аналитики «по движкам» показывал
        один столбец «auto» на весь архив, статистика моделей — движок
        «auto» у каждой. Настоящий движок однозначно следует из модели по
        каталогу; модели, которой в каталоге уже нет, не трогаются.
        """
        from .catalog import get_model  # noqa: PLC0415

        for таблица in ("jobs", "model_stats"):
            модели = [str(строка[0]) for строка in conn.execute(
                f"SELECT DISTINCT model FROM {таблица} "
                "WHERE model IS NOT NULL AND (engine IS NULL OR engine IN ('', 'auto'))")]
            for модель in модели:
                spec = get_model(модель)
                if spec is None:
                    continue
                conn.execute(
                    f"UPDATE {таблица} SET engine=? WHERE model=? "
                    "AND (engine IS NULL OR engine IN ('', 'auto'))", (spec.engine, модель))

    def _починка_ключей_звонков(self, conn: sqlite3.Connection) -> None:
        """Ключ звонка в архиве приводится к виду «станция:идентификатор».

        Версия 22 завела вторую АТС и вместе с ней составной ключ: у
        Asterisk `uniqueid` уникален только внутри одной станции, и две
        станции в одну секунду дают одинаковый. Настоящий идентификатор
        уехал в колонку `pbx_uid`, а ключом стало «станция:идентификатор».

        Миграция тогда заполнила `pbx_uid`, но сам ключ оставила старым — и
        после обновления импортёр не узнавал НИ ОДНОГО прежнего звонка:
        `call_exists("pbx:1789145000.1")` отвечал «нет», потому что в
        таблице лежал «1789145000.1». Весь накопленный архив заезжал
        заново — второй строкой на тот же разговор, вторым заданием на ту
        же запись, вторым проходом видеокарты по всему прошлому. На архиве
        в сорок тысяч звонков это неделя работы сервера впустую и удвоенная
        аналитика за весь прошлый период.

        Починка идёт ровно один раз, при переходе на версию 24, и делает
        две вещи. Строку, чей ключ свободен, просто переименовывает. Если
        ключ уже занят — значит сервер успел завести дубль, — из двух строк
        остаётся составная, и в неё переносится задание прежней: именно та
        расшифровка лежит в отчётах и по ней стоят ссылки. Дубль после
        этого удаляется: две строки на один разговор удваивают его в каждом
        подсчёте.

        Строки без станции (архив ещё более ранних версий) не трогаются:
        составить ключ не из чего, а выдумывать станцию значит спрятать
        звонки из раздела той АТС, к которой они на самом деле не
        относятся.
        """
        условие = ("station IS NOT NULL AND station <> '' "
                   "AND pbx_uid IS NOT NULL AND pbx_uid <> '' "
                   "AND uniqueid <> station || ':' || pbx_uid")
        всего = conn.execute(f"SELECT COUNT(*) FROM calls WHERE {условие}").fetchone()[0]
        if not всего:
            return

        # Сначала дубли: перенести задание и убрать старую строку. Задание
        # прежней строки главнее всегда, а не только когда у дубля своего
        # нет: обычный дубль своё задание уже получил (импорт поставил
        # запись второй раз), и прежнее условие `job_id IS NULL` оставляло
        # звонок привязанным к дублю — у исходной расшифровки, той, что в
        # отчётах и по которой стоят ссылки, связь со звонком терялась.
        прежняя = (
            "SELECT c.job_id FROM calls c WHERE "
            + условие.replace("station", "c.station").replace("pbx_uid", "c.pbx_uid")
            .replace("uniqueid", "c.uniqueid")
            + " AND c.station = calls.station AND c.pbx_uid = calls.pbx_uid"
              " AND c.job_id IS NOT NULL")
        брошенные = [str(строка[0]) for строка in conn.execute(
            "SELECT job_id FROM calls WHERE job_id IS NOT NULL "
            "AND uniqueid = station || ':' || pbx_uid "
            f"AND EXISTS ({прежняя} AND c.job_id <> calls.job_id)")]
        conn.execute(
            f"UPDATE calls SET job_id = COALESCE(({прежняя} LIMIT 1), job_id) "
            "WHERE uniqueid = station || ':' || pbx_uid")
        дублей = conn.execute(
            f"DELETE FROM calls WHERE {условие} AND EXISTS ("
            "  SELECT 1 FROM calls c WHERE c.uniqueid = calls.station || ':' || calls.pbx_uid)"
        ).rowcount
        переименовано = conn.execute(
            f"UPDATE calls SET uniqueid = station || ':' || pbx_uid WHERE {условие}"
        ).rowcount
        log.info("Миграция: ключи звонков приведены к виду «станция:идентификатор» — "
                 "переименовано %s, дублей убрано %s", переименовано, дублей)
        if брошенные:
            # Второе задание на ту же запись не удаляется молча: это
            # расшифровка, которую человек мог уже открыть. Но и в отчётах
            # оно считает разговор второй раз — поэтому список в журнал.
            log.warning(
                "Миграция: у %s звонков было по два задания; звонок оставлен за "
                "прежним, повторное осталось без звонка — удалите его в "
                "«Результатах», если оно не нужно: %s",
                len(брошенные), ", ".join(брошенные[:20])
                + (" …" if len(брошенные) > 20 else ""))


    def _вычистить_секреты_заданий(self, conn: sqlite3.Connection) -> None:
        """Убирает секреты сервера из параметров уже созданных заданий.

        До версии 26 в параметры каждого задания ложился полный снимок
        настроек — вместе с ключом модели, секретом подписи уведомлений,
        токеном CRM и паролями станций, — и отдавался владельцу задания.
        Выдача теперь чистит параметры сама (`_row_to_job`), а здесь они
        вычищаются и в самой базе: копии базы уходят за пределы сервера.

        Переписываются только строки, где секрет действительно задан, —
        остальные не трогаются: у заданий с длинной расшифровкой перезапись
        строки обходится дорого, а пустой ключ секретом не является.
        """
        ключи = Settings.НЕ_В_ЗАДАНИИ
        try:
            пути = ", ".join(f"'$.{к}'" for к in ключи)
            условие = " OR ".join(
                f"(json_type(params, '$.{к}') IS NOT NULL AND "
                f"json_extract(params, '$.{к}') NOT IN ('', '[]', 0))"
                for к in ключи)
            вычищено = conn.execute(
                f"UPDATE jobs SET params = json_remove(params, {пути}) "
                f"WHERE json_valid(params) AND ({условие})").rowcount
        except sqlite3.OperationalError as exc:
            # Сборка SQLite без JSON1 — редкость, но сервер из-за этого не
            # должен отказываться стартовать: выдача и так чистит параметры.
            log.warning("Параметры заданий не вычищены (%s) — выдача чистит их сама", exc)
            return
        if вычищено:
            log.info("Миграция: из параметров %s заданий убраны секреты сервера", вычищено)

    def _разделить_названный_nps(self, conn: sqlite3.Connection) -> None:
        """Названный клиентом балл — в свою колонку, из предсказанного — вон.

        До версии 28 в `nps` лежал названный балл, если клиент его назвал, и
        предсказанный — если нет, а индекс NPS складывал их вместе. Теперь
        `nps` — только предсказанный. Прежний названный переезжает в
        `nps_said`, а предсказанного у таких записей пока нет: его посчитает
        пересчёт разбора (версия разбора тоже поднята). Держать названный
        балл в колонке предсказанного до пересчёта значило бы продолжать
        складывать одно с другим.
        """
        перенесено = conn.execute(
            "UPDATE content SET nps_said = nps, nps = NULL, nps_group = NULL "
            "WHERE nps_stated = 1 AND nps IS NOT NULL").rowcount
        if перенесено:
            log.info("Миграция: названный клиентом балл NPS перенесён в свою "
                     "колонку у %s записей", перенесено)

    def _снять_качество_с_переписки(self, conn: sqlite3.Connection) -> None:
        """Снимает признаки «подозрительной расшифровки» с переписки.

        Распознавания у переписки не было, а у её реплик нет длительности:
        любая переписка от шести сообщений получала «осколки разметки
        говорящих» и попадала в отбор подозрительных, в тренд и в метрику по
        модели «переписка:chat» с долей 1,0.
        """
        снято = conn.execute(
            "UPDATE jobs SET suspect_segments = NULL, suspect_share = NULL, "
            "quality_flags = NULL, quality_detail = NULL "
            "WHERE source = 'text' AND quality_flags IS NOT NULL").rowcount
        if снято:
            log.info("Миграция: признаки качества распознавания сняты с %s "
                     "переписок", снято)

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
                # «Есть ли хоть одна», а не «сколько»: COUNT(*) по репликам и
                # по `_docsize` шёл при каждом открытии базы под BEGIN
                # IMMEDIATE — на архиве в миллионы реплик это секунды
                # блокировки записи на каждом запуске и у каждого сценария.
                реплик = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM segments)").fetchone()[0]
                в_указателе = conn.execute(
                    "SELECT EXISTS(SELECT 1 FROM segments_fts_docsize)").fetchone()[0]
                if реплик and not в_указателе:
                    log.info("Сборка поискового указателя по репликам архива…")
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
            # «CREATE UNIQUE INDEX» тоже указатель — и как раз тот, что
            # хоть что-то гарантирует. Из двадцати шести указателей схемы
            # защитная сетка ловила двадцать пять и пропускала ровно
            # уникальный указатель по имени пользователя, без которого две
            # учётные записи «Admin» и «admin» заводятся разом.
            верх = подготовка.upper()
            if not (верх.startswith("CREATE INDEX")
                    or верх.startswith("CREATE UNIQUE INDEX")):
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
                    # У уникального — ещё и перечень дублей: без него
                    # непонятно, что именно убрать.
                    дубли = (self._дубли(conn, statement)
                             if isinstance(exc, sqlite3.IntegrityError) else "")
                    log.warning("Не удалось создать указатель: %s%s", exc,
                                f". Дубли: {дубли}" if дубли else "")
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
            добавлены = []
            for name, declaration in columns.items():
                if name not in existing:
                    log.info("Миграция: в таблицу «%s» добавлена колонка «%s»", table, name)
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
                    добавлены.append(name)
            if table == "calls" and "pbx_uid" in добавлены:
                # Прежние звонки лежат под ключом станции — он же и был их
                # настоящим идентификатором, пока станция была одна. Без
                # этого переноса у всего старого архива не нашлось бы ни
                # записи (поиск идёт по идентификатору в имени файла), ни
                # самого звонка на АТС.
                conn.execute("UPDATE calls SET pbx_uid = uniqueid "
                             "WHERE COALESCE(pbx_uid,'') = ''")

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
        # Задание и событие «создано» — одной транзакцией.
        with self.write() as conn:
            conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({holders})",
                         list(payload.values()))
            conn.execute(
                "INSERT INTO events (job_id, ts, kind, message, data, instance) "
                "VALUES (?,?,?,?,NULL,?)",
                (job_id, ts, "created", f"Задание создано: {payload.get('filename', '')}",
                 INSTANCE_ID))
        self.поколение_заданий += 1
        return job_id

    #: Поля, смена которых не меняет сводных отчётов: прогресс и отметки жизни.
    _НЕ_МЕНЯЮТ_ОТЧЁТЫ = frozenset({"progress", "stage", "heartbeat_at", "updated_at",
                                   "instance_id"})

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        if not set(fields) <= self._НЕ_МЕНЯЮТ_ОТЧЁТЫ:
            self.поколение_заданий += 1
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
        "model_load_s, inference_s, postprocess_s, vad_s, alignment_s, "
        "diarization_s, rtf, words_count, "
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
        # Записи, до которых модель ещё не дошла, и записи со сбоем разбора.
        # Без этих двух отборов «разобрать всё, что не разобрано» в разделе
        # «Результаты» приходилось делать глазами: список показывает сто
        # строк, а в архиве их сорок тысяч.
        "llm_missing": ("jobs.status = 'completed' AND COALESCE(jobs.text,'') <> '' "
                        "AND jobs.id NOT IN (SELECT job_id FROM llm_results "
                        "WHERE COALESCE(error,'') = '')"),
        "llm_failed": ("jobs.id IN (SELECT job_id FROM llm_results "
                       "WHERE COALESCE(error,'') <> '')"),
        "llm_done": ("jobs.id IN (SELECT job_id FROM llm_results "
                     "WHERE COALESCE(error,'') = '')"),
    }

    #: Приставка отбора по категории обращения: `category:payment`. Имя
    #: категории — значение из настроек, а не выражение, и в запрос оно
    #: попадает только параметром.
    CATEGORY_FILTER = "category:"

    #: Отборы с приставкой и значением: категория обращения, исход и
    #: причина из ответа языковой модели. Перечень нужен и здесь, и в
    #: обработчике списка заданий: он проверяет отбор до запроса, и без
    #: общего перечня новый отбор работал бы в базе и отвергался ручкой.
    PREFIX_FILTERS: tuple[str, ...] = ("category:", "outcome:", "reason:", "qc:")

    #: Отбор по оценке клиента из опроса контроля качества: `qc:low` (ниже
    #: 60 %), `qc:mid`, `qc:high` (от 80 %), `qc:any` (оценка есть), `qc:none`
    #: (опрос был, оценки нет).
    QC_FILTERS = {
        "low": "q.score_pct < 60", "mid": "q.score_pct >= 60 AND q.score_pct < 80",
        "high": "q.score_pct >= 80", "any": "q.score_pct IS NOT NULL",
        "none": "q.score_pct IS NULL",
    }

    def qc_by(self, by: str, *, since: float | None = None,
              owner: str | list[str] | None = None) -> dict[str, dict[str, Any]]:
        """Оценка клиента по сотрудникам (добавочный опроса), очередям или станциям.

        `agent` — добавочный оператора (`ext` опроса), `station` — станция,
        `queue` — очередь звонка (из журнала звонков по заданию).
        """
        колонка = {"agent": "q.ext", "station": "q.station",
                   "queue": "(SELECT c.queue FROM calls c WHERE c.job_id = q.job_id LIMIT 1)",
                   "owner": "q.owner"}.get(by)
        if колонка is None:
            return {}
        условия: list[str] = ["q.score_pct IS NOT NULL"]
        аргументы: list[Any] = []
        if since:
            условия.append("q.created_at >= ?")
            аргументы.append(float(since))
        if owner:
            владельцы = [owner] if isinstance(owner, str) else list(owner)
            условия.append("q.owner IN (" + ",".join("?" * len(владельцы)) + ")")
            аргументы.extend(владельцы)
        строки = self.query(
            f"SELECT {колонка} AS k, COUNT(*) AS n, AVG(q.score_pct) AS avg_pct, "
            "SUM(CASE WHEN q.score_pct < 60 THEN 1 ELSE 0 END) AS low "
            f"FROM qc_surveys q WHERE {' AND '.join(условия)} GROUP BY k", аргументы)
        return {str(с["k"] or "—"): {"surveys": int(с["n"] or 0),
                                     "avg_pct": round(float(с["avg_pct"]), 1)
                                     if с["avg_pct"] is not None else None,
                                     "low": int(с["low"] or 0)}
                for с in строки}

    def qc_scores(self, job_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Оценки клиента по заданиям страницы — одним запросом."""
        if not job_ids:
            return {}
        места = ",".join("?" * len(job_ids))
        return {str(с["job_id"]): {"score": с["score"], "score_pct": с["score_pct"],
                                   "status": с["status"]}
                for с in self.query(
                    "SELECT job_id, score, score_pct, status FROM qc_surveys "
                    f"WHERE job_id IN ({места})", list(job_ids))}

    def _jobs_where(self, *, status: str | list[str] | None = None,
                    owner: str | list[str] | None = None,
                    model: str | None = None, search: str | None = None,
                    group_id: str | None = None, since: float | None = None,
                    content: str | None = None, until: float | None = None,
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
        elif content and content.startswith("qc:"):
            условие = self.QC_FILTERS.get(content[3:])
            if условие is None:
                return None
            where.append("EXISTS (SELECT 1 FROM qc_surveys q WHERE q.job_id = jobs.id "
                         f"AND {условие})")
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
        # Верхняя граница — та же, что у списков раскрытия (`drill`):
        # полуоткрытое окно [since, until). Иначе «Записи» из отчёта за
        # период показывали бы и то, что пришло после отчёта.
        if until is not None:
            where.append("created_at<?")
            args.append(float(until))
        if search:
            # Расшифровку ищет указатель, имя файла и номер — перебором.
            # Перебор здесь дёшев: обе колонки короткие и лежат в начале
            # записи, поэтому строку не приходится читать целиком. Дорого
            # было именно `text LIKE '%…%'` — оно поднимало с диска все
            # расшифровки подряд, и редкое слово (то есть ровно тот запрос,
            # ради которого поиском и пользуются) читало таблицу насквозь.
            найденные = (self.jobs_matching(search, owner=owner)
                         if self.fts_ready else None)
            # Достигнутый предел — не повод молчать. Список сортирован
            # «сначала новые», и на шестистах совпадениях девятая страница
            # оказывалась последней без единого слова об этом: человек
            # делал вывод, что старых разговоров нет, а они просто не
            # попали в четыреста. Признак поднимается здесь и уезжает в
            # ответ, чтобы интерфейс мог предложить уточнить запрос.
            self.last_search_truncated = bool(
                найденные is not None and len(найденные) >= self.SEARCH_LIMIT)
            # Проценты и подчёркивания экранируем: «скидка 50%» и «part_1»
            # — это то, что люди ищут, а не образцы LIKE. Без экранирования
            # запрос «%» находил вообще всё, и то же самое находило
            # удаление по требованию субъекта (там минимум четыре знака —
            # «%%%%» проходило).
            needle = f"%{_экранировать_like(search)}%"
            if найденные is None:
                where.append("(filename LIKE ? ESCAPE '\\' OR text LIKE ? ESCAPE '\\' "
                             "OR id LIKE ? ESCAPE '\\')")
                args.extend([needle, needle, needle])
            elif найденные:
                места = ",".join("?" for _ in найденные)
                where.append(
                    f"(id IN ({места}) OR filename LIKE ? ESCAPE '\\' "
                    f"OR id LIKE ? ESCAPE '\\')")
                args.extend([*найденные, needle, needle])
            else:
                where.append("(filename LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')")
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
                  ready_before: float | None = None,
                  until: float | None = None) -> list[dict[str, Any]]:
        собрано = self._jobs_where(status=status, owner=owner, model=model,
                                   search=search, group_id=group_id, since=since,
                                   content=content, until=until)
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
            # Столбцы «Результатов» — щелчком по заголовку (3.1.34).
            "finished_at ASC", "filename ASC", "filename DESC", "model ASC", "model DESC",
            "words_count DESC", "words_count ASC", "segments_count DESC", "segments_count ASC",
            "avg_confidence DESC", "avg_confidence ASC", "speakers_count DESC",
            "speakers_count ASC",
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
            # Начало расшифровки — строкой в двести знаков: список
            # «Результатов» показывает его под именем файла, и ради этой
            # строки тянул полные тексты всех ста пятидесяти записей.
            columns += (", substr(jobs.text, 1, 200) AS text_preview" if соединение
                        else ", substr(text, 1, 200) AS text_preview")
        else:
            columns = "jobs.*" if соединение else "*"
        порядок = _порядок_заданий(order, bool(соединение))
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

        Пробелы вокруг метки убираются. Колонку заполняют трое, и все
        по-разному: интерфейс кладёт то, что человек набрал (`prompt` плюс
        `trim`), телефония склеивает свои метки через запятую, а аналитика
        читает ту же колонку через `split(",")` и `strip()`. Из-за этого
        «срочно, согласие» и «АТС, входящий, согласие» считались здесь
        записями БЕЗ согласия — и попадали в список на уничтожение по
        152-ФЗ, хотя согласие у них есть. Вторая метка телефонии вместе с
        правкой руками давала ровно такую строку.

        Метасимволы LIKE в самой метке экранируются: это единственный LIKE
        в файле, где их не экранировали, а метка приходит из настройки.
        Значение `%` означало «без метки — никто», и проверка согласия
        отвечала «нарушений нет» на всём архиве.
        """
        образец = f"%,{_экранировать_like(str(tag).strip())},%"
        rows = self.query(
            "SELECT id FROM jobs WHERE status='completed' AND created_at < ? "
            # REPLACE убирает пробелы вокруг запятых — «срочно, согласие»
            # становится «срочно,согласие», и обрамление работает как
            # задумано, а не только на метках без пробелов.
            "  AND (',' || REPLACE(REPLACE(COALESCE(tags,''), ', ', ','), ' ,', ',') "
            "       || ',') NOT LIKE ? ESCAPE '\\' "
            "ORDER BY created_at LIMIT ?",
            (older_than, образец, limit))
        return [str(r["id"]) for r in rows]

    #: Сколько разговоров максимум приносит один поиск. Список на экране
    #: всё равно листается страницами, а перечень номеров уезжает в SQL
    #: условием `id IN (...)`, у которого есть свой предел на число
    #: параметров.
    SEARCH_LIMIT = 400

    @property
    def last_search_truncated(self) -> bool:
        """Упёрся ли последний поиск ЭТОГО ПОТОКА в предел `SEARCH_LIMIT`.

        Признак лежал в общем объекте базы: параллельный поиск другого
        пользователя перезаписывал его между `list_jobs` и чтением, и отдел,
        упёршийся в предел, получал `search_truncated=false` — то есть
        уверенность, что старых разговоров нет. Обработчик запроса читает
        признак в том же потоке сразу после выборки.
        """
        return bool(getattr(getattr(self, "_local", None), "search_truncated", False))

    @last_search_truncated.setter
    def last_search_truncated(self, значение: bool) -> None:
        if getattr(self, "_local", None) is None:
            self._local = threading.local()
        self._local.search_truncated = bool(значение)

    def jobs_matching(self, search: str, limit: int = 0, *,
                      owner: str | list[str] | None = None) -> list[str]:
        """Номера заданий, в расшифровках которых встретилось искомое.

        Разрез по владельцу применяется ЗДЕСЬ, внутри запроса, а не снаружи
        к результату. Предел в четыреста совпадений иначе съедали чужие
        записи: отдел с одной вчерашней записью на фоне четырёхсот более
        свежих чужих не находил её вовсе — поиск отвечал «ничего», при том
        что слово в разговоре было.

        Тем же порядком чинится и листалка администратора: предел,
        наложенный до `LIMIT/OFFSET`, обрезал выборку на четырёхстах, и
        страница девятая была последней даже там, где совпадений шестьсот.
        """
        запрос = fts_query(search)
        if not запрос or not self.fts_ready:
            return []
        условие, свои = self._owner_clause(owner, "j")
        где = f" AND {условие}" if условие else ""
        предел = limit or self.SEARCH_LIMIT
        # Память — у потока: обработчик запроса зовёт список и счётчик в
        # одном потоке подряд, а другим потокам чужой ответ не нужен.
        ключ = (запрос, где, tuple(свои), предел, self._поколение_реплик)
        помню = getattr(self._local, "поиск", None)
        if помню and помню[0] == ключ and time.monotonic() - помню[1] < ПАМЯТЬ_ПОИСКА_С:
            return list(помню[2])
        try:
            # Порядок обязателен: без него FTS отдаёт совпадения по
            # возрастанию номера строки, то есть от самых старых реплик, а
            # предел отрезает всё остальное. Человек, который ищет вчерашний
            # разговор в архиве на тысячу совпадений, не находил его вовсе:
            # список сортирован «сначала новые», но в него попадали только
            # самые древние четыреста.
            rows = self.query(
                "SELECT s.job_id, MAX(j.created_at) AS свежесть FROM segments_fts f "
                "JOIN segments s ON s.rowid = f.rowid "
                "JOIN jobs j ON j.id = s.job_id "
                f"WHERE f.text MATCH ?{где} GROUP BY s.job_id "
                "ORDER BY свежесть DESC, s.job_id LIMIT ?",
                (запрос, *свои, предел))
        except StorageError as exc:
            log.warning("Поиск по указателю не удался (%s) — идём перебором", exc)
            return []
        найдено = [str(r["job_id"]) for r in rows]
        self._local.поиск = (ключ, time.monotonic(), tuple(найдено))
        return найдено

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

    # --- удаление по требованию --------------------------------------------

    @staticmethod
    def _цифры_номера(запрос: str) -> str:
        """Цифры номера из запроса — десять последних, если их хотя бы семь.

        Номер пишут кто как: «+7 (916) 123-45-67», «89161234567»,
        «9161234567»; станция кладёт его своим способом. Сравнение по
        десяти последним цифрам ловит все три записи одного номера. Меньше
        семи цифр — это внутренний номер или номер заказа, а не телефон:
        «12345» нашлось бы в сотне чужих номеров.
        """
        цифры = re.sub(r"\D", "", str(запрос or ""))
        return цифры[-10:] if len(цифры) >= 7 else ""

    def _условие_звонка(self, запрос: str) -> tuple[str, list[Any]]:
        """Условие «звонок про этого человека»: номер в любом поле или имя.

        Цифры поля перед сравнением очищаются от «+», пробелов, скобок и
        дефисов — у SQLite нет замены по образцу, а REPLACE по пяти знакам
        покрывает всё, что пишет Asterisk и что набирает человек.
        """
        части: list[str] = []
        args: list[Any] = []
        цифры = self._цифры_номера(запрос)
        if цифры:
            for поле in ("src", "dst", "clid", "channel", "dstchannel", "recording"):
                чистое = f"COALESCE({поле},'')"
                for знак in ("+", " ", "-", "(", ")"):
                    чистое = f"REPLACE({чистое}, '{знак}', '')"
                части.append(f"{чистое} LIKE ? ESCAPE '\\'")
                args.append(f"%{_экранировать_like(цифры)}%")
        текст = str(запрос or "").strip().lower()
        if текст:
            части.append("lower(COALESCE(clid,'')) LIKE ? ESCAPE '\\'")
            args.append(f"%{_экранировать_like(текст)}%")
        return ("(" + " OR ".join(части) + ")") if части else "0", args

    def erase_call_job_ids(self, запрос: str, *, limit: int = 1000) -> list[str]:
        """Задания звонков, где встречается номер или имя из запроса.

        Поиск «Результатов» смотрит в имя файла, номер задания и текст. Номер
        клиента он находил, только если его произнесли вслух: запись,
        названная `${UNIQUEID}.wav`, по номеру не находилась вовсе — субъекту
        отвечали «удалено», а разговор оставался.
        """
        условие, args = self._условие_звонка(запрос)
        строки = self.query(
            f"SELECT DISTINCT job_id FROM calls WHERE job_id IS NOT NULL AND {условие} "
            "LIMIT ?", [*args, max(1, int(limit))])
        return [str(с["job_id"]) for с in строки]

    def erase_calls_count(self, запрос: str) -> int:
        """Сколько строк журнала звонков обезличит `erase_calls` — для пробы."""
        условие, args = self._условие_звонка(запрос)
        строка = self.query_one(f"SELECT COUNT(*) AS n FROM calls WHERE {условие}", args)
        return int(строка["n"]) if строка else 0

    def erase_calls(self, запрос: str) -> int:
        """Обезличивает строки журнала звонков, где встречается номер или имя.

        Звонки без задания: пропущенные (короткий, без записи, не отвеченный)
        тоже хранят номер — удалять по требованию нужно и их. Звонки, чьё
        задание ещё есть, не трогаются: их обезличивает удаление самого
        задания (`delete_job`). Прежде обезличивалось всё совпавшее, и
        задания сверх предела одного запроса оставались с расшифровкой, но
        без номера — найти их по номеру повторным запросом было уже нельзя.
        """
        условие, args = self._условие_звонка(запрос)
        return self.execute(
            f"UPDATE calls SET {ОБЕЗЛИЧИТЬ_ЗВОНОК} WHERE {условие} "
            "AND (job_id IS NULL OR job_id NOT IN (SELECT id FROM jobs))", args)

    def qc_amd_label(self, survey_id: str, метка: str, признаки: list[list[float]], *,
                     кто: str = "") -> None:
        """Разметка записи опроса для модели автоответчика; пустая метка — убрать."""
        if not метка:
            self.execute("DELETE FROM qc_amd_labels WHERE survey_id=?", (str(survey_id),))
            return
        self.execute(
            "INSERT INTO qc_amd_labels (survey_id, label, features, labeled_by, labeled_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(survey_id) DO UPDATE SET label=excluded.label, "
            "features=excluded.features, labeled_by=excluded.labeled_by, "
            "labeled_at=excluded.labeled_at",
            (str(survey_id), метка, json.dumps(признаки), str(кто or ""), now()))

    def qc_amd_labels(self) -> list[tuple[list[list[float]], str]]:
        """Все размеченные записи: (признаки на каждом окне, метка)."""
        итог: list[tuple[list[list[float]], str]] = []
        for с in self.query("SELECT label, features FROM qc_amd_labels"):
            try:
                окна = json.loads(с["features"])
                итог.append(([[float(x) for x in окно] for окно in окна], str(с["label"])))
            except (ValueError, TypeError):
                continue
        return итог

    def erase_qc(self, запрос: str) -> int:
        """Опросы контроля качества по номеру клиента — по требованию субъекта.

        Опрос хранит номер и нажатые им оценки; из списка «не звонить» номер
        не убирается намеренно: это воля самого субъекта, и забыть её
        значило бы позвонить ему снова.
        """
        цифры = self._цифры_номера(запрос)
        if not цифры or len(цифры) < 5:
            return 0
        # Запись опроса — голос субъекта: уходит вместе со строкой. Разметка
        # для модели автоответчика (только числа) — тоже: она про его опрос.
        for строка in self.query(
                "SELECT recording FROM qc_surveys WHERE instr(number, ?) > 0 "
                "AND COALESCE(recording,'')<>''", (цифры,)):
            with contextlib.suppress(OSError):
                Path(строка["recording"]).unlink(missing_ok=True)
        self.execute("DELETE FROM qc_amd_labels WHERE survey_id IN (SELECT id FROM qc_surveys "
                     "WHERE instr(number, ?) > 0 AND status NOT IN ('ждёт','звонит'))", (цифры,))
        return self.execute(
            "DELETE FROM qc_surveys WHERE instr(number, ?) > 0 AND status NOT IN ('ждёт','звонит')",
            (цифры,)) + self.execute(
            "UPDATE qc_surveys SET status='отменён', finished_at=? "
            "WHERE instr(number, ?) > 0 AND status='ждёт'", (now(), цифры))

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
            "  AND status NOT IN ('cancelled', 'failed') "
            # Контрольный прогон второй моделью ставит сервер, а не человек:
            # записывался он на владельца исходной записи и съедал его
            # суточную квоту — ключ с квотой в сто заданий упирался в неё
            # на семидесяти своих.
            "  AND COALESCE(source, '')<>'control'",
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
                   since: float | None = None,
                   until: float | None = None) -> int:
        """Сколько заданий подходит под отбор.

        Тот же набор условий, что у `list_jobs`, и собирает их та же
        функция. Раньше счётчик знал только про статус и владельца: список
        с поиском показывал тридцать строк и «всего 4000», а листалка вела
        на пустые страницы.
        """
        собрано = self._jobs_where(status=status, owner=owner, model=model,
                                   search=search, group_id=group_id, since=since,
                                   content=content, until=until)
        if собрано is None:
            return 0
        соединение, where, args = собрано
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        row = self.query_one(
            f"SELECT COUNT(*) AS n FROM jobs{соединение} {clause}", args)
        return int(row["n"]) if row else 0

    def archive_models(self, *, owner: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Какими моделями распознан архив и сколько заданий у каждой.

        Для отбора по модели: в каталоге семь десятков моделей, а в архиве
        обычно две, и выбор из всего каталога приводил к пустым спискам.
        Считается по указателю `idx_jobs_model` — без чтения самих заданий.
        """
        собрано = self._jobs_where(owner=owner)
        if собрано is None:
            return []
        соединение, where, args = собрано
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        строки = self.query(
            f"SELECT model, COUNT(*) AS n FROM jobs{соединение} {clause} "
            "GROUP BY model ORDER BY n DESC, model", args)
        return [{"model": str(с["model"] or ""), "jobs": int(с["n"])}
                for с in строки if с["model"]]

    def job_file_refs(self) -> tuple[set[str], set[str], set[str]]:
        """Все пути, которые держат задания: исходники, каталоги результатов, номера.

        Для уборки файлов без задания (`maintenance.подмести_файлы`). Пути
        приводятся к настоящим (`resolve`), чтобы ссылка и файл сравнивались
        одинаково; номера нужны отдельно — каталог результатов называется
        номером задания, даже если путь в базе ведёт в другое место.
        """
        файлы: set[str] = set()
        каталоги: set[str] = set()
        номера: set[str] = set()

        def настоящий(путь: str) -> str:
            try:
                return str(Path(путь).resolve())
            except (OSError, RuntimeError, ValueError):
                return путь

        for строка in self.query("SELECT id, file_path, result_path FROM jobs"):
            номера.add(str(строка[0]))
            if строка[1]:
                файлы.add(настоящий(str(строка[1])))
            if строка[2]:
                каталоги.add(настоящий(str(строка[2])))
        return файлы, каталоги, номера

    def file_referenced(self, пути: list[str], имя: str) -> bool:
        """Держит ли какое-нибудь задание этот файл загрузки.

        Для суточной уборки, которая идёт от файлов на диске к базе: сперва
        точные написания пути по индексу `idx_jobs_file`, и только если их
        нет — сравнение по имени файла (путь в базе мог быть записан через
        ссылку или из другого корня). До второго шага доходят одни сироты.
        """
        пути = [п for п in dict.fromkeys(пути) if п]
        if пути and self.query_one(
                "SELECT 1 FROM jobs WHERE file_path IN (" + ",".join("?" * len(пути))
                + ") LIMIT 1", пути) is not None:
            return True
        if not имя:
            return False
        имя = имя.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        # Разделитель — и прямая, и обратная черта (сервер под Windows).
        return self.query_one(
            "SELECT 1 FROM jobs WHERE file_path LIKE ? ESCAPE '\\' "
            "OR file_path LIKE ? ESCAPE '\\' LIMIT 1",
            ("%/" + имя, "%\\\\" + имя)) is not None

    def results_referenced(self, номер: str, пути: list[str]) -> bool:
        """Держит ли задание каталог результатов: по номеру (ключ) или по пути."""
        if номер and self.query_one("SELECT 1 FROM jobs WHERE id=?", (номер,)) is not None:
            return True
        пути = [п for п in dict.fromkeys(пути) if п]
        return bool(пути) and self.query_one(
            "SELECT 1 FROM jobs WHERE result_path IN (" + ",".join("?" * len(пути))
            + ") LIMIT 1", пути) is not None

    def file_used_elsewhere(self, file_path: str, except_id: str) -> bool:
        """Ссылается ли на этот файл записи другое задание.

        Контрольный прогон второй моделью ставится на запись исходного
        задания, а не на её копию. Удаление контрольного — кнопкой, пакетом,
        уборкой — уносило звук исходного: прослушивание отвечало 404,
        перераспознать было нечего.
        """
        if not file_path:
            return False
        return self.query_one(
            "SELECT 1 FROM jobs WHERE file_path=? AND id<>? LIMIT 1",
            (file_path, except_id)) is not None

    def delete_job(self, job_id: str) -> None:
        self._поколение_реплик += 1
        self.поколение_заданий += 1
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
            # Отрезки по участникам — тональность и слова разговора.
            conn.execute("DELETE FROM agent_segments WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM review_queue WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM llm_results WHERE job_id=?", (job_id,))
            # Эталон человека и векторы смысла — тоже о разговоре: эталон
            # хранит причину и исход словами проверяющего, вектор — кусок
            # пересказа.
            conn.execute("DELETE FROM llm_golden WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM llm_vectors WHERE job_id=?", (job_id,))
            # И строку очереди разбора. Без неё удаление записи оставляло в
            # очереди сироту: в состоянии «идёт» её не брала ни одна уборка
            # (`llmq_prune` ходит по завершённым), кнопка «Очистить» её
            # пропускала намеренно, а раздел показывал вечный текущий
            # запрос к модели по записи, которой больше нет. Заодно это
            # требование 152-ФЗ: строка очереди хранит имя файла и владельца.
            conn.execute("DELETE FROM llm_queue WHERE job_id=?", (job_id,))
            conn.execute("DELETE FROM model_checks WHERE job_id=? OR check_job_id=?",
                         (job_id, job_id))
            # Проверка оператора по этой записи. Её не удалял никто: она
            # висела в «ожидает» и «просрочено» вечно, считалась в очереди
            # проверяющего и хранила оператора, комментарий и пункты оценки
            # разговора, которого больше нет.
            conn.execute("DELETE FROM qa_reviews WHERE job_id=?", (job_id,))
            # Ответы модели по этой записи: пересказ с именем и номером.
            # Ключ кеша — отпечаток подсказки, поэтому до версии 29 удаление
            # записи их не находило вовсе (см. `_LLM_CACHE_SCHEMA`).
            conn.execute("DELETE FROM llm_cache WHERE job_id=?", (job_id,))
            # Звонок остаётся строкой журнала — без неё импорт завёл бы его
            # заново, — но без номеров, имени звонящего и пути к записи: они
            # переживали разговор и были видны в журнале и топах раздела
            # «АТС» до срока хранения, а при «хранить бессрочно» — всегда.
            conn.execute(f"UPDATE calls SET {ОБЕЗЛИЧИТЬ_ЗВОНОК} WHERE job_id=?", (job_id,))
            # Поисковый указатель при удалении пишет «надгробия», а сами
            # слова физически остаются в нём до слияния сегментов — и уезжают
            # в резервные копии. Суточная уборка вычищает их (`fts_purge`),
            # если с прошлого раза что-то удаляли.
            conn.execute("INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
                         (КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ, "true", now()))
            # content_vocab здесь не трогаем: это словарь форм на весь
            # сервер. Основы, которых не осталось ни в одной записи, убирает
            # `sweep_orphans` — одним запросом на все удалённые разом.
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
        if changed and not set(fields) <= self._НЕ_МЕНЯЮТ_ОТЧЁТЫ:
            self.поколение_заданий += 1
        return bool(changed)

    # --- уведомления ----------------------------------------------------

    def webhook_due(self, *, limit: int = 20) -> list[str]:
        """Задания, чьё уведомление пора (до)слать: ждущие и брошенные посреди доставки."""
        строки = self.query(
            "SELECT id FROM jobs WHERE webhook_status IN ('pending', 'sending') "
            "AND COALESCE(webhook_next_at, 0) <= ? "
            # Старшие первыми: без порядка выборка шла по rowid, и при
            # большом хвосте свежие ждущие могли обгонять давно ждущие.
            "ORDER BY COALESCE(webhook_next_at, 0) LIMIT ?", (now(), max(1, int(limit))))
        return [str(с["id"]) for с in строки]

    def webhook_claim(self, job_id: str, *, hold_s: float) -> bool:
        """Берёт доставку уведомления на себя — неделимо, как захват задания.

        Серверов над базой бывает несколько, и без условной записи оба
        досылали бы одно уведомление. «sending» со сроком: доставка,
        брошенная падением процесса, по истечении срока снова считается
        ждущей.
        """
        момент = now()
        return bool(self.execute(
            "UPDATE jobs SET webhook_status='sending', webhook_next_at=? "
            "WHERE id=? AND webhook_status IN ('pending', 'sending') "
            "AND COALESCE(webhook_next_at, 0) <= ?",
            (момент + max(1.0, float(hold_s)), job_id, момент)))

    # --- планирование ---------------------------------------------------

    def fair_share_candidates(self, *, per_owner: int, ready_before: float,
                              owners: int = 500) -> list[dict[str, Any]]:
        """По нескольку старших ждущих заданий на каждого владельца.

        Справедливому разделению нужен не «верх очереди», а «верх каждого
        владельца»: окно в пятьсот заданий по времени постановки у отдела,
        поставившего шестьсот записей, целиком состояло из его заданий, и
        одинокая запись соседа выбиралась сто первой. Порядок внутри
        владельца — как у «по приоритету»: важность, потом время постановки.
        """
        колонки = self.LIGHT_COLUMNS
        строки = self.query(
            f"SELECT {колонки} FROM ("
            f"  SELECT {колонки}, ROW_NUMBER() OVER ("
            "      PARTITION BY COALESCE(owner, 'anonymous') "
            "      ORDER BY priority DESC, created_at ASC, id ASC) AS место "
            "  FROM jobs WHERE status IN ('queued', 'retry') "
            "    AND (queued_at IS NULL OR queued_at <= ?)"
            ") WHERE место <= ? ORDER BY created_at ASC LIMIT ?",
            (float(ready_before), max(1, int(per_owner)),
             max(1, int(per_owner)) * max(1, int(owners))))
        return [dict(r) for r in строки]

    def find_cached(self, file_hash: str, params_hash: str, *,
                    owner: str | None = None) -> dict[str, Any] | None:
        """Готовое задание с той же записью и теми же настройками.

        `owner` ограничивает поиск своими заданиями: общий кеш говорил
        одному владельцу, что такую же запись уже присылал другой.
        """
        условие, аргументы = "", [file_hash, params_hash]
        if owner is not None:
            условие = " AND owner=?"
            аргументы.append(owner)
        row = self.query_one(
            "SELECT * FROM jobs WHERE file_hash=? AND status='completed' "
            f"AND json_extract(params, '$._hash')=?{условие} "
            "ORDER BY finished_at DESC LIMIT 1", аргументы)
        return _row_to_job(row) if row else None

    def ждут_близнеца(self, ids: list[str], идущие: list[str]) -> set[str]:
        """Какие из заданий `ids` ждут идущего близнеца.

        Близнец — задание того же владельца с той же записью и теми же
        настройками (отпечаток `_hash` в параметрах), которое выполняется
        сейчас: в базе или только что взято этим процессом (`идущие` —
        захват в базе пишется уже после выборки). Задание с выключенной
        дедупликацией не ждёт никого.
        """
        if not ids:
            return set()
        место_ids = ",".join("?" * len(ids))
        место_идущих = ",".join("?" * len(идущие)) if идущие else "''"
        rows = self.query(
            "SELECT c.id FROM jobs c WHERE c.id IN (" + место_ids + ") "
            "AND c.file_hash IS NOT NULL AND c.file_hash<>'' "
            "AND IFNULL(json_extract(c.params, '$.deduplicate_jobs'), 1) "
            "AND EXISTS (SELECT 1 FROM jobs r WHERE r.file_hash=c.file_hash "
            "AND r.id<>c.id AND r.owner IS c.owner "
            "AND (r.status='running' OR r.id IN (" + место_идущих + ")) "
            "AND json_extract(r.params, '$._hash')=json_extract(c.params, '$._hash'))",
            [*ids, *идущие])
        return {str(r["id"]) for r in rows}

    # --- сегменты -------------------------------------------------------

    def save_segments(self, job_id: str, segments: list[dict[str, Any]]) -> None:
        """Реплики задания. Принимает и вид движка, и вид из базы.

        У двух полей два написания: движок отдаёт `no_speech_prob` и
        `compression_ratio`, а колонки в таблице зовутся `no_speech` и
        `compression` — и `get_segments` возвращает их именно так. Здесь
        читались только имена движка, поэтому круг «прочитать и записать»
        обе величины терял. Ходит этим кругом клонирование результата из
        кеша: повторная загрузка того же файла копирует реплики прежнего
        задания — и у копии пропадали признаки галлюцинации. Запись, которую
        разбор пометил «сжимаемый текст, тишина», у клона выглядела чистой,
        и в сводке качества таких записей становилось меньше, чем есть.
        """
        rows = self._строки_реплик(job_id, segments)
        self._поколение_реплик += 1
        with self.write() as conn:
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.executemany(self._ВСТАВКА_РЕПЛИКИ, rows)

    _ВСТАВКА_РЕПЛИКИ = (
        "INSERT INTO segments (job_id, idx, start_s, end_s, text, speaker, "
        "confidence, no_speech, compression, temperature, language, words) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)")

    @staticmethod
    def _строки_реплик(job_id: str, segments: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
        """Реплики — строками для вставки (см. `save_segments`)."""
        def взять(seg: dict[str, Any], *имена: str) -> Any:
            for имя in имена:
                if seg.get(имя) is not None:
                    return seg[имя]
            return None

        rows = []
        for idx, seg in enumerate(segments):
            rows.append((
                job_id, idx, float(seg.get("start", 0.0)), float(seg.get("end", 0.0)),
                seg.get("text", ""), seg.get("speaker"), seg.get("confidence"),
                взять(seg, "no_speech_prob", "no_speech"),
                взять(seg, "compression_ratio", "compression"),
                seg.get("temperature"), seg.get("language"),
                json.dumps(seg.get("words"), ensure_ascii=False) if seg.get("words") else None,
            ))
        return rows

    def finish_job(self, job_id: str, segments: list[dict[str, Any]], *,
                   expected: list[str], expected_instance: str | None,
                   events: list[tuple[str, str]] | None = None,
                   metrics: list[tuple[str, float, str, str]] | None = None,
                   **fields: Any) -> bool:
        """Отметка «готово» и реплики — одной транзакцией и одним условием.

        Реплики писались отдельно и ДО отметки «готово», а при её провале
        (задание отменили или отобрали) стирались `clear_segments` без
        всякой проверки, чьё задание. «Оживший» экземпляр — замерший дольше
        отметки жизни (заморозка ВМ, подвисшее хранилище) или с часами,
        ушедшими вперёд, — стирал так реплики задания, которое за это время
        честно досчитал сосед: в базе оставалось «готово, сегментов 40» и
        пустая таблица реплик. Теперь реплики пишутся, только если прошла
        отметка, и тем же условием: не прошла — не тронуто ничего, ни
        реплик соседа, ни прежних реплик задания (повтор готового задания,
        отменённый в последний миг, остаётся с прежним текстом).

        События (`events`: вид, текст) и показатели (`metrics`: имя,
        значение, модель, движок) пишутся той же транзакцией: раньше на
        каждое готовое задание шёл десяток отдельных транзакций — признаки
        качества, четыре показателя, предупреждения, «готово», — и на
        сетевом хранилище это десяток fsync.
        """
        rows = self._строки_реплик(job_id, segments)
        fields["updated_at"] = now()
        _serialize_json_fields(fields)
        columns = ", ".join(f"{name}=?" for name in fields)
        placeholders = ",".join("?" for _ in expected)
        where = f"id=? AND status IN ({placeholders})"
        args: list[Any] = [*fields.values(), job_id, *expected]
        if expected_instance is not None:
            where += " AND instance_id=?"
            args.append(expected_instance)
        self.поколение_заданий += 1
        with self.write() as conn:
            if conn.execute(f"UPDATE jobs SET {columns} WHERE {where}", args).rowcount != 1:
                return False
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.executemany(self._ВСТАВКА_РЕПЛИКИ, rows)
            момент = now()
            if metrics:
                conn.executemany(
                    "INSERT INTO metrics (ts, name, value, job_id, model, engine, labels) "
                    "VALUES (?,?,?,?,?,?,NULL)",
                    [(момент, str(имя), float(значение or 0.0), job_id, модель, движок)
                     for имя, значение, модель, движок in metrics])
            if events:
                conn.executemany(
                    "INSERT INTO events (job_id, ts, kind, message, data, instance) "
                    "VALUES (?,?,?,?,NULL,?)",
                    [(job_id, момент, str(вид), str(текст), INSTANCE_ID)
                     for вид, текст in events])
        self._поколение_реплик += 1
        return True

    def clear_segments(self, job_id: str) -> None:
        """Убирает реплики задания.

        Нужно там, где реплики записаны, а отметка «готово» не прошла:
        задание успели отменить или отобрать. Реплики пишутся первыми —
        так «готово» никогда не врёт про их число, — и остаться без
        задания они могут только на этом пути.
        """
        self._поколение_реплик += 1
        self.execute("DELETE FROM segments WHERE job_id=?", (job_id,))

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
                "INSERT INTO events (job_id, ts, kind, message, data, instance) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, now(), kind, message,
                 json.dumps(data, ensure_ascii=False) if data else None, INSTANCE_ID))
        except StorageError:
            log.warning("Не удалось записать событие %s для %s", kind, job_id)

    def последнее_событие(self) -> int:
        строка = self.query_one("SELECT MAX(id) AS n FROM events")
        return int(строка["n"] or 0) if строка is not None else 0

    def события_соседей(self, после: int, *, предел: int = 500) -> list[dict[str, Any]]:
        """События заданий, записанные другими серверами над этой базой."""
        # Все строки после метки, а не только чужие: метка должна проходить
        # и через свои, иначе каждый опрос заново перебирал бы их.
        return [dict(с, own=str(с["instance"] or "") in ("", INSTANCE_ID)) for с in self.query(
            "SELECT e.id, e.job_id, e.kind, e.message, e.instance, j.owner, j.status, "
            "j.filename FROM events e LEFT JOIN jobs j ON j.id = e.job_id "
            "WHERE e.id > ? ORDER BY e.id LIMIT ?", (int(после), int(предел)))]

    def ход_соседей(self) -> list[dict[str, Any]]:
        """Ход заданий, которые сейчас считают другие серверы."""
        return [dict(с) for с in self.query(
            "SELECT id, owner, progress, stage FROM jobs "
            "WHERE status='running' AND COALESCE(instance_id,'') NOT IN ('', ?)",
            (INSTANCE_ID,))]

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

    def get_events_for(self, owners: str | list[str], *, public_kinds: tuple[str, ...] = (),
                       limit: int = 100) -> list[dict[str, Any]]:
        """Лента событий для владельца (или группы) — отбором в SQL.

        Раньше лента обычного ключа поднимала до двух тысяч его заданий и
        сверяла события с ними в Python на каждый опрос; события старше этих
        двух тысяч тихо пропадали. Теперь — соединение с `jobs` по владельцу.
        """
        владельцы = [owners] if isinstance(owners, str) else list(owners or [])
        if not владельцы:
            return []
        места = ",".join("?" * len(владельцы))
        условия = [f"(e.job_id IS NOT NULL AND j.owner IN ({места}))"]
        аргументы: list[Any] = [*владельцы]
        if public_kinds:
            условия.append("(e.job_id IS NULL AND e.kind IN ("
                           + ",".join("?" * len(public_kinds)) + "))")
            аргументы.extend(public_kinds)
        rows = self.query(
            "SELECT e.* FROM events e LEFT JOIN jobs j ON j.id=e.job_id "
            f"WHERE {' OR '.join(условия)} ORDER BY e.ts DESC LIMIT ?",
            [*аргументы, int(limit)])
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

    def set_kv_many(self, пары: dict[str, Any]) -> None:
        """Несколько ключей одной транзакцией — когда половина пары хуже ничего.

        Позиция журнала звонков — это смещение И номер файла, в котором оно
        отсчитано. Записанные по отдельности, они могли разойтись на сбое
        между двумя записями: смещение из нового файла при номере старого
        дочитывало бы старый файл с чужого места.
        """
        мгновение = now()
        with self.write() as conn:
            for ключ, значение in пары.items():
                conn.execute("INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
                             (ключ, json.dumps(значение, ensure_ascii=False), мгновение))

    def lease_take(self, key: str, holder: str, ttl_s: float) -> str | None:
        """Берёт аренду на время; None — взята, иначе — кто её держит.

        Аренда нужна там, где работу по одному предмету (станции АТС) может
        начать любой из серверов над общей базой, а делать её вдвоём нельзя.
        Свободна та, которой нет, которая уже наша, чей срок вышел, или чей
        держатель — процесс этой же машины, которого больше нет (сервер
        перезапустили, и ждать истечения срока незачем).
        """
        from .instance import process_alive  # noqa: PLC0415

        мгновение = now()
        with self.write() as conn:
            строка = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if строка is not None:
                try:
                    данные = json.loads(строка["value"]) or {}
                except (TypeError, ValueError):
                    данные = {}
                чей = str(данные.get("holder") or "") if isinstance(данные, dict) else ""
                до = float(данные.get("until") or 0) if isinstance(данные, dict) else 0.0
                if чей and чей != holder and до > мгновение and process_alive(чей):
                    return чей
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
                (key, json.dumps({"holder": holder, "until": мгновение + float(ttl_s)},
                                 ensure_ascii=False), мгновение))
        return None

    def lease_release(self, key: str, holder: str) -> bool:
        """Отдаёт свою аренду; чужую не трогает."""
        with self.write() as conn:
            строка = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            if строка is None:
                return False
            try:
                данные = json.loads(строка["value"]) or {}
            except (TypeError, ValueError):
                данные = {}
            if not isinstance(данные, dict) or str(данные.get("holder") or "") != holder:
                return False
            conn.execute("DELETE FROM kv WHERE key=?", (key,))
        return True

    def kv_claim(self, key: str, value: Any, *, stale_s: float) -> Any:
        """Занимает ключ, если он свободен или устарел; иначе отдаёт значение.

        Проверка и запись — одной транзакцией записи: два одновременных
        запроса не займут один ключ оба. Ответ None — ключ наш.
        """
        мгновение = now()
        with self.write() as conn:
            строка = conn.execute("SELECT value, ts FROM kv WHERE key=?", (key,)).fetchone()
            if строка is not None and мгновение - float(строка["ts"] or 0) < stale_s:
                try:
                    return json.loads(строка["value"])
                except (TypeError, ValueError):
                    return {}
            conn.execute("INSERT OR REPLACE INTO kv (key, value, ts) VALUES (?,?,?)",
                         (key, json.dumps(value, ensure_ascii=False), мгновение))
        return None

    def del_kv(self, key: str) -> None:
        self.execute("DELETE FROM kv WHERE key=?", (key,))

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    def stats(self) -> dict[str, Any]:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {
            "path": str(self.path),
            "size_mb": round(size / 1024 / 1024, 2),
            "schema_version": SCHEMA_VERSION,
            "jobs": self.count_jobs(),
            "segments": int(self.query_scalar("SELECT COUNT(*) n FROM segments")),
            "metrics": int(self.query_scalar("SELECT COUNT(*) n FROM metrics")),
            "events": int(self.query_scalar("SELECT COUNT(*) n FROM events")),
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


def _значение_не_того_вида(exc: sqlite3.Error) -> None:
    """Значение не того вида из запроса — это 400, а не «ошибка базы».

    `PATCH /api/users/{id}` с `{"display_name": {...}}` доходил до SQLite
    словарём и возвращался клиенту как 507 «Ошибка записи в базу: Error
    binding parameter 1: type 'dict' is not supported» — отказ хранилища
    вместо «поле заполнено не тем» и внутренности базы в ответе.
    """
    if isinstance(exc, (sqlite3.ProgrammingError, sqlite3.InterfaceError)) and \
            "binding parameter" in str(exc).lower():
        raise ConfigError(
            "Значение поля не того вида: ожидались текст, число или «да/нет».",
            hint="Проверьте тело запроса: вложенные объекты и списки в этих "
                 "полях не принимаются.") from exc


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
    # Секреты сервера в параметрах задания не отдаются никому и нигде —
    # даже если строка создана до того, как их перестали туда класть.
    if isinstance(job.get("params"), dict):
        job["params"] = Settings.для_задания(job["params"])
    if isinstance(job.get("quality_flags"), str):
        job["quality_flags"] = [ф for ф in job["quality_flags"].split(",") if ф]
    return job
