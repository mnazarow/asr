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
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import ConfigError, StorageError
from .instance import HOSTNAME, INSTANCE_ID, KERNEL_ID, own_host, process_alive
from .logging_setup import get_logger

log = get_logger("db")

SCHEMA_VERSION = 30

#: Сколько заданий убирать по сроку хранения за один заход служебного цикла.
CLEANUP_BATCH = 5000

#: Потолок числа корзин у рядов нагрузки. График шире тысячи точек всё равно
#: не читается, а число корзин задаёт стоимость запроса.
SERIES_MAX_BUCKETS = 2000

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

#: Предел, до которого усекается журнал после контрольной точки, байт.
#: Без него файл `-wal` сохраняет размер самого большого пика навсегда:
#: после VACUUM на базе в 260 МБ рядом оставался журнал в 174 МБ.
ПРЕДЕЛ_ЖУРНАЛА = 64 * 1024 * 1024

#: Отметка жизни экземпляра в `kv`: `instance:<машина:процесс>`.
РЕЕСТР = "instance:"

#: Поля звонка, в которых есть кто-то конкретный: номера, имя звонящего,
#: каналы (у прямых SIP-абонентов в имени канала стоит номер), путь к записи
#: (в имени файла Asterisk пишет номер) и поля, которые станция заполняет
#: своими данными. Ключ, станция, время, длительность, очередь и
#: внутренний номер оператора остаются: без ключа импорт завёл бы звонок
#: заново, а по остальному считаются нагрузка и отчёты.
ПОЛЯ_ЛИЧНЫЕ_ЗВОНКА = ("src", "dst", "clid", "channel", "dstchannel",
                      "recording", "userfield", "accountcode")
ОБЕЗЛИЧИТЬ_ЗВОНОК = ", ".join(f"{поле}=''" for поле in ПОЛЯ_ЛИЧНЫЕ_ЗВОНКА)

#: Признак «из указателя удаляли — пора вычистить» (см. `Database.fts_purge`).
КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ = "fts.dirty"

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
        created_at  REAL NOT NULL
    ) WITHOUT ROWID
"""

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
    "CREATE INDEX IF NOT EXISTS idx_llm_results_created ON llm_results(created_at DESC)",
    # Уборка кеша по сроку: `created_at` в таблице WITHOUT ROWID стоит после
    # ответа модели, и без указателя часовая уборка читала все ответы —
    # больше гигабайта на двадцати тысячах. И по записи — для удаления.
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_created ON llm_cache(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_llm_cache_job ON llm_cache(job_id)",
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
        reason      TEXT DEFAULT ''
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
        ip        TEXT DEFAULT ''
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
        deferred_at  REAL
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
    "CREATE INDEX IF NOT EXISTS idx_calls_agent ON calls(agent, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_calls_queue ON calls(queue, started_at DESC)",
    # Повторные обращения ищутся по номеру звонящего в окне в неделю: без
    # этого индекса решение с первого обращения считается перебором всего
    # архива на каждый звонок.
    "CREATE INDEX IF NOT EXISTS idx_calls_src ON calls(src, started_at)",
    # Уборка журнала звонков по сроку и множество известных звонков для
    # обхода каталога записей — по времени импорта.
    "CREATE INDEX IF NOT EXISTS idx_calls_imported ON calls(imported_at)",
    "CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit(actor, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qa_status ON qa_reviews(status, assigned_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_qa_job ON qa_reviews(job_id)",
    "CREATE INDEX IF NOT EXISTS idx_qa_agent ON qa_reviews(agent, reviewed_at DESC)",
]


def now() -> float:
    return time.time()


def new_id(prefix: str = "job") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"



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


class Database:
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
            try:
                conn.create_function("lower", 1, _нижний, deterministic=True)
            except (sqlite3.NotSupportedError, TypeError):    # старая сборка
                conn.create_function("lower", 1, _нижний)
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
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK")
                raise StorageError(f"Не удалось применить миграции: {exc}") from exc

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

    # --- разбор содержания записей ---------------------------------------

    #: Колонки свода: по ним считаются разрезы по всему архиву. Подробности
    #: (`detail`) сюда не входят намеренно — это килобайты на запись.
    CONTENT_COLUMNS = (
        "job_id, version, computed_at, sentiment, sentiment_label, "
        "sentiment_shift, negative_segments, positive_segments, wpm, "
        "silence_share, interruptions, pauses, long_pauses, longest_pause_s, "
        "filler_rate, "
        "questions, commitments, commitments_dated, alerts, compliance, "
        "money_max, speakers, agent_speaker, overlap_s, dead_air_s, switches, "
        "talk_share, monologue_s, customer_story_s, reply_delay_s, tempo_ratio, "
        "frustration, repeat_contact, profanity, profanity_agent, objections, "
        "objections_unhandled, violations, agent_score, empathy, name_uses, "
        "mood, mood_shift, intensity, stress, effort, fatigue, clarity, "
        "accuracy, politeness, personalization, rhythm, filler_top, "
        "diminutives, diminutive_rate, nps, nps_group, nps_stated, nps_said, "
        "csat_said"
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
            # Пишем только если задание ещё существует. Между тем, как
            # фоновый разбор взял список записей, и тем, как он дописал
            # ответ, проходят секунды (разбор содержания) или минуты
            # (языковая модель), — и в это окно запись могут удалить:
            # часовой уборкой по сроку, кнопкой в интерфейсе или удалением
            # по требованию субъекта. Дописанный после удаления пересказ
            # разговора не убирает потом никто: уборка ходит по `jobs`, а
            # задания уже нет. То есть данные, которые считаются
            # удалёнными, оставались в базе навсегда.
            строк = conn.execute(
                f"INSERT OR REPLACE INTO content ({имена}) "
                f"SELECT {места} WHERE EXISTS (SELECT 1 FROM jobs WHERE id=?)",
                [*поля.values(), job_id]).rowcount
            if not строк:
                log.debug("Разбор записи %s не сохранён: задание удалено", job_id)
                return
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
            "SELECT j.id, j.text, j.media_duration_s, j.quality_flags, "
            # `source` нужен пересчёту архива: у переписки нет времени, и
            # показатели в секундах для неё не считаются вовсе. Без этого
            # поля пересчёт молча считал бы их по номерам реплик.
            "       j.source, j.avg_confidence FROM jobs j "
            "LEFT JOIN content c ON c.job_id = j.id "
            "WHERE j.status='completed' AND j.text IS NOT NULL AND j.text != '' "
            # Контрольные прогоны второй моделью — та же запись второй раз:
            # в разборе содержания ей не место.
            "  AND COALESCE(j.source,'') <> 'control' "
            "  AND (c.job_id IS NULL OR c.version < ?) "
            "ORDER BY j.created_at DESC LIMIT ?",
            (version, limit))
        return [dict(r) for r in rows]

    def content_stats(self, version: int, *,
                      owner: str | list[str] | None = None) -> dict[str, int]:
        """Сколько записей разобрано, сколько ждёт разбора.

        Обе цифры считаются по одному и тому же набору заданий —
        завершённым и с расшифровкой. Раньше «разобрано» считалось по всей
        таблице разбора, без оглядки на статус задания, и повтор задания
        (`retry` меняет статус, но оставляет и текст, и строку разбора)
        уменьшал «всего», не трогая «разобрано»: покрытие показывало
        «разобрано всё» на архиве, разобранном наполовину. Предупреждение
        «показатели посчитаны по разобранной части» при этом гасло.
        """
        # Контрольные прогоны мимо: `content_pending` их не выдаёт (это та
        # же запись второй раз), и в «всего» им тоже не место — иначе
        # «ожидают разбора» растёт на пять записей в сутки и не приходит к
        # нулю никогда, а метрика покрытия показывает вечный недоразбор.
        #
        # Разрез по владельцу: те же числа попадают в отчёт по содержанию
        # под ключом «coverage», а отчёт строится для конкретного ключа.
        # Без разреза отдел с одной полностью разобранной записью видел
        # «ожидают разбора 41» и предупреждение «показатели посчитаны по
        # части архива» — при том что его часть разобрана целиком.
        условие, свои = self._owner_clause(owner, "j")
        где = f" AND {условие}" if условие else ""
        всего = int(self.query_one(
            "SELECT COUNT(*) n FROM jobs j WHERE j.status='completed' "
            "AND j.text IS NOT NULL AND j.text != '' "
            f"AND COALESCE(j.source,'') <> 'control'{где}", свои)["n"])
        разобрано = int(self.query_one(
            "SELECT COUNT(*) n FROM content c JOIN jobs j ON j.id = c.job_id "
            "WHERE c.version >= ? AND j.status='completed' "
            "AND j.text IS NOT NULL AND j.text != '' "
            f"AND COALESCE(j.source,'') <> 'control'{где}", (version, *свои))["n"])
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
        # Показатели сверх тональности. У каждого рядом со средним идёт
        # число записей, где его было чем считать: средняя понятность по
        # трём записям из тысячи — это не показатель отдела, и без
        # знаменателя её не отличить от средней по всем.
        ("mood", "AVG(c.mood)"),
        ("mood_shift", "AVG(c.mood_shift)"),
        ("recovered", "SUM(CASE WHEN c.mood_shift > 0.15 THEN 1 ELSE 0 END)"),
        ("worsened", "SUM(CASE WHEN c.mood_shift < -0.15 THEN 1 ELSE 0 END)"),
        ("intensity", "AVG(c.intensity)"),
        ("stress", "AVG(c.stress)"),
        ("stress_high", "SUM(CASE WHEN c.stress >= 55 THEN 1 ELSE 0 END)"),
        ("stress_checked", "SUM(CASE WHEN c.stress IS NOT NULL THEN 1 ELSE 0 END)"),
        ("effort", "AVG(c.effort)"),
        ("effort_high", "SUM(CASE WHEN c.effort <= -2 THEN 1 ELSE 0 END)"),
        ("fatigue", "AVG(c.fatigue)"),
        ("fatigue_checked", "SUM(CASE WHEN c.fatigue IS NOT NULL THEN 1 ELSE 0 END)"),
        ("clarity", "AVG(c.clarity)"),
        ("clarity_checked", "SUM(CASE WHEN c.clarity IS NOT NULL THEN 1 ELSE 0 END)"),
        ("clarity_low", "SUM(CASE WHEN c.clarity < 35 THEN 1 ELSE 0 END)"),
        ("accuracy", "AVG(c.accuracy)"),
        ("accuracy_checked", "SUM(CASE WHEN c.accuracy IS NOT NULL THEN 1 ELSE 0 END)"),
        ("politeness", "AVG(c.politeness)"),
        ("politeness_checked", "SUM(CASE WHEN c.politeness IS NOT NULL THEN 1 ELSE 0 END)"),
        ("personalization", "AVG(c.personalization)"),
        ("rhythm", "AVG(c.rhythm)"),
        ("rhythm_checked", "SUM(CASE WHEN c.rhythm IS NOT NULL THEN 1 ELSE 0 END)"),
        ("diminutives", "SUM(COALESCE(c.diminutives,0))"),
        ("diminutive_records",
         "SUM(CASE WHEN COALESCE(c.diminutives,0) > 0 THEN 1 ELSE 0 END)"),
        ("diminutive_rate", "AVG(c.diminutive_rate)"),
        # NPS: предсказанный и названный клиентом — разными колонками и
        # разными числами. Смешать их — значит объявить настоящим то, что
        # сервер угадал. `nps` и группы — только предсказанный балл,
        # `*_stated` — только названный по шкале от нуля до десяти.
        ("nps", "AVG(c.nps)"),
        ("nps_stated_avg", "AVG(c.nps_said)"),
        ("nps_stated_count", "SUM(CASE WHEN c.nps_said IS NOT NULL THEN 1 ELSE 0 END)"),
        ("nps_checked", "SUM(CASE WHEN c.nps IS NOT NULL THEN 1 ELSE 0 END)"),
        ("promoters", "SUM(CASE WHEN c.nps_group = 'промоутер' THEN 1 ELSE 0 END)"),
        ("passives", "SUM(CASE WHEN c.nps_group = 'нейтрал' THEN 1 ELSE 0 END)"),
        ("detractors", "SUM(CASE WHEN c.nps_group = 'критик' THEN 1 ELSE 0 END)"),
        ("promoters_stated", "SUM(CASE WHEN c.nps_said >= 9 THEN 1 ELSE 0 END)"),
        ("passives_stated", "SUM(CASE WHEN c.nps_said IN (7, 8) THEN 1 ELSE 0 END)"),
        ("detractors_stated", "SUM(CASE WHEN c.nps_said <= 6 THEN 1 ELSE 0 END)"),
        # Оценка по пятибалльной шкале — не NPS: пятёрка здесь высшая оценка.
        ("csat_avg", "AVG(c.csat_said)"),
        ("csat_count", "SUM(CASE WHEN c.csat_said IS NOT NULL THEN 1 ELSE 0 END)"),
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
        # Разрезы из журнала звонков: сотрудник, очередь, станция. Через
        # подзапрос, а не соединение: соединение с `calls` размножило бы
        # строку задания, если у него почему-то оказалось два звонка, и
        # средние поехали бы молча.
        "agent": "COALESCE(NULLIF(("
                 "SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id"
                 "),''),'—')",
        "queue": "COALESCE(NULLIF(("
                 "SELECT cl.queue FROM calls cl WHERE cl.job_id = j.id"
                 "),''),'—')",
        "station": "COALESCE(NULLIF(("
                   "SELECT cl.station FROM calls cl WHERE cl.job_id = j.id"
                   "),''),'—')",
        "direction": "COALESCE(NULLIF(("
                     "SELECT cl.direction FROM calls cl WHERE cl.job_id = j.id"
                     "),''),'—')",
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
        "long_pauses": "c.long_pauses",
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
        "mood": "c.mood",
        "stress": "c.stress",
        "effort": "c.effort",
        "fatigue": "c.fatigue",
        "clarity": "c.clarity",
        "accuracy": "c.accuracy",
        "politeness": "c.politeness",
        "personalization": "c.personalization",
        "rhythm": "c.rhythm",
        "diminutive_rate": "c.diminutive_rate",
        "nps": "c.nps",
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
        # Сотрудник из журнала АТС. Это самый полезный разрез из трёх:
        # «говорящий 1» — техническая метка внутри записи, владелец — это
        # отдел или интеграция, а здесь настоящее имя из очереди.
        "agent": "COALESCE(NULLIF(("
                 "SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id"
                 "),''),'—') = ?",
        "queue": "COALESCE(NULLIF(("
                 "SELECT cl.queue FROM calls cl WHERE cl.job_id = j.id"
                 "),''),'—') = ?",
        "station": "COALESCE(NULLIF(("
                   "SELECT cl.station FROM calls cl WHERE cl.job_id = j.id"
                   "),''),'—') = ?",
    }

    @staticmethod
    def _owner_clause(owner: str | list[str] | None,
                      alias: str = "j") -> tuple[str, list[Any]]:
        """Условие «свои записи» и его параметры — одним местом на всех.

        Разрез по владельцу пишется в базе полудюжиной методов, и каждый
        раз одинаково. Разъезжаться ему нельзя: пропущенное условие — это
        чужие разговоры в чужом отчёте, а такое находится не проверкой, а
        жалобой.
        """
        if not owner:
            return "", []
        владельцы = [owner] if isinstance(owner, str) else list(owner)
        if not владельцы:
            return "", []
        места = ",".join("?" for _ in владельцы)
        return f"{alias}.owner IN ({места})", list(владельцы)

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
                          agent: tuple[str, str] | None = None,
                          weights: bool = False) -> list[dict[str, Any]]:
        """Свод по разобранным записям — целиком или по группам.

        Возвращает список строк; без группировки — ровно одну. Пустой корпус
        тоже даёт строку, с нулями и None: разделу нужно показать «записей
        нет», а не свалиться на отсутствующем ключе.

        `weights` — к каждому среднему добавить `_n_<имя>`: по скольким
        записям оно посчитано. Нужно тому, кто складывает средние групп
        сам (разрез по меткам): вес «всех записей группы» завышал вклад
        групп, где показатель измерен у малой части.
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
        if weights:
            # COUNT по тому же выражению, что и AVG, — ровно то число
            # значений, по которому AVG посчитан: NULL не входит ни туда, ни
            # сюда.
            показатели += "".join(
                f", COUNT({выр[4:-1]}) AS _n_{имя}"
                for имя, выр in self.CONTENT_METRICS
                if выр.startswith("AVG(") and выр.endswith(")"))
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
            # Номер задания — последним ключом: у записей с одинаковой
            # уверенностью и одной секундой отбора порядок иначе не задан, и
            # листалка показывала одну запись на двух страницах, а другую —
            # ни на одной.
            "ORDER BY CASE r.status WHEN 'pending' THEN 0 ELSE 1 END, "
            "         j.avg_confidence ASC, r.picked_at DESC, r.job_id LIMIT ? OFFSET ?",
            [*args, limit, offset])
        return [dict(r) for r in rows]

    def review_counts(self, since: float | None = None, *,
                      owner: str | list[str] | None = None) -> dict[str, int]:
        """Сколько ожидает, сколько разобрано и пропущено за окно.

        Разрез по владельцу тот же, что у списка рядом. Без него в одном
        ответе оказывались список из трёх своих записей и счётчик «ожидают
        десять» — и это не только несогласованная пара на экране, но и
        сообщение о том, сколько разговоров у соседнего подразделения.
        """
        out = {"pending": 0, "done": 0, "skipped": 0}
        условие, args = self._owner_clause(owner, "j")
        соединение = " JOIN jobs j ON j.id = r.job_id" if условие else ""
        где = f" AND {условие}" if условие else ""
        for r in self.query(
                f"SELECT r.status AS status, COUNT(*) n FROM review_queue r{соединение} "
                f"WHERE (r.status='pending' OR r.done_at >= ?){где} "
                "GROUP BY r.status", (since or 0.0, *args)):
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
        # Как и у разбора содержания: ответ модели считается минутами, и
        # за это время запись могут удалить. Пересказ разговора, дописанный
        # после удаления, не находит потом ни уборка, ни удаление по
        # требованию субъекта — они ходят по таблице заданий.
        self.execute(
            f"INSERT OR REPLACE INTO llm_results ({', '.join(колонки)}) "
            f"SELECT {', '.join('?' for _ in колонки)} "
            "WHERE EXISTS (SELECT 1 FROM jobs WHERE id=?)", [*значения, job_id])

    def llm_get(self, job_id: str) -> dict[str, Any] | None:
        row = self.query_one("SELECT * FROM llm_results WHERE job_id=?", (job_id,))
        return _row_to_llm(row) if row else None

    def llm_pending(self, version: int, limit: int = 50, *, since: float | None = None,
                    owner: str | list[str] | None = None) -> list[dict[str, Any]]:
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
        # Разрез по владельцу: «разобрать всё неразобранное» от обычного ключа
        # ставило в очередь весь архив сервера, хотя тот же отбор через
        # /llm/backfill доступен только администратору.
        своё, своё_args = self._owner_clause(owner, "j")
        if своё:
            where.append(своё)
            args.extend(своё_args)
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
            "SELECT l.job_id, l.version, l.summary, l.reason, l.reason_quote, l.outcome, "
            "       l.outcome_quote, l.resolved, l.actions, l.trackers, "
            "       l.scorecard, l.latency_ms, l.error, l.calls, l.chunks, l.warnings, j.filename, "
            "       j.created_at, j.owner "
            f"FROM llm_results l JOIN jobs j ON j.id = l.job_id WHERE {условие}", args)
        return {"total": всего, "rows": [_row_to_llm(r) for r in rows]}

    def llm_cache_get(self, key: str) -> str | None:
        row = self.query_one("SELECT response FROM llm_cache WHERE key=?", (key,))
        return str(row["response"]) if row else None

    def llm_cache_forget(self, key: str) -> None:
        """Убирает запись кеша: ответ оказался неразбираемым."""
        self.execute("DELETE FROM llm_cache WHERE key=?", (key,))

    def llm_cache_put(self, key: str, kind: str, model: str, response: str,
                      latency_ms: float, *, job_id: str | None = None) -> None:
        """Кладёт ответ модели в кеш; `job_id` — чья это запись (для удаления)."""
        self.execute(
            "INSERT OR REPLACE INTO llm_cache (key, kind, model, response, latency_ms, "
            "created_at, job_id) VALUES (?,?,?,?,?,?,?)",
            (key, kind, model, response, latency_ms, now(), job_id or None))

    def llm_cache_forget_matching(self, образец: str) -> int:
        """Убирает ответы модели, в которых встречается образец.

        Для удаления по требованию: ответы, положенные в кеш до версии 29,
        не знают своей записи, а пересказ мог сохранить и номер, и фамилию.
        """
        текст = str(образец or "").strip()
        if len(текст) < 4:
            return 0
        условия = ["lower(response) LIKE ? ESCAPE '\\'"]
        args: list[Any] = [f"%{_экранировать_like(текст.lower())}%"]
        # Номер — по цифрам: модель пишет его по-своему («+7 916 123-45-67»),
        # а удалить просят так, как он записан у человека.
        цифры = self._цифры_номера(текст)
        if цифры:
            чистый = "COALESCE(response,'')"
            for знак in ("+", " ", "-", "(", ")"):
                чистый = f"REPLACE({чистый}, '{знак}', '')"
            условия.append(f"{чистый} LIKE ? ESCAPE '\\'")
            args.append(f"%{_экранировать_like(цифры)}%")
        return self.execute(f"DELETE FROM llm_cache WHERE {' OR '.join(условия)}", args)

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

        И звонки без задания: пропущенные (короткий, без записи, не
        отвеченный) тоже хранят номер — удалять по требованию нужно и их.
        """
        условие, args = self._условие_звонка(запрос)
        return self.execute(f"UPDATE calls SET {ОБЕЗЛИЧИТЬ_ЗВОНОК} WHERE {условие}", args)

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

    def coaching_count(self, *, since: float | None = None,
                       until: float | None = None,
                       owner: str | list[str] | None = None,
                       agent: tuple[str, str] | None = None,
                       include_done: bool = False) -> int:
        """Сколько записей в очереди коучинга всего — без предела выдачи.

        Число в заголовке очереди раньше было длиной выданного списка: при
        пятистах записях в очереди карточка оператора писала «20», и
        очередь выглядела разобранной.
        """
        условие, args = self._content_where(since, until, owner, agent=agent)
        причины = " OR ".join(f"({выр})" for _, выр in self.COACHING_REASONS)
        готово = "" if include_done else " AND COALESCE(m.status,'') <> 'done'"
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM content c JOIN jobs j ON j.id = c.job_id "
            "LEFT JOIN content_marks m ON m.job_id = c.job_id AND m.kind = 'coaching' "
            f"WHERE {условие} AND ({причины}){готово}", args)
        return int(row["n"]) if row else 0

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
        # Разрез по владельцу нужен обоим запросам, а не только первому:
        # отметка «эталон» ставится вручную, и без этого условия ключ
        # подразделения видел чужие разговоры целиком — с именем файла,
        # владельцем и всем разбором.
        отбор_владельца, args_владельца = self._owner_clause(owner, "j")
        отмеченные = self.query(
            общие + "WHERE j.status='completed' AND m.status = 'yes' "
            + (f"AND {отбор_владельца} " if отбор_владельца else "")
            + "ORDER BY m.updated_at DESC LIMIT ?", [*args_владельца, limit])
        out: list[dict[str, Any]] = []
        видели: set[str] = set()
        for r in [*отмеченные, *лучшие]:
            if r["job_id"] in видели:
                continue
            видели.add(r["job_id"])
            out.append(dict(r))
        return out

    def tracker_hits(self, *, since: float | None = None,
                     until: float | None = None,
                     owner: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Срабатывания трекеров за окно — по журналу событий.

        Категория лежит в данных события; группируем по ней прямо в базе.
        На сборке SQLite без JSON (редкость, но бывает) — пустой список,
        а не ошибка: сводка от этого не должна пропадать.

        Разрез по владельцу такой же, как у соседей по разделу: без него
        ключ подразделения видел строку «трекер сработал 340 раз» по всему
        серверу — и чужие числа, и сам факт чужих срабатываний.
        """
        where = ["e.kind='tracker'"]
        args: list[Any] = []
        if since:
            where.append("e.ts>=?")
            args.append(since)
        if until:
            where.append("e.ts<?")
            args.append(until)
        соединение = "FROM events e "
        if owner:
            соединение += "JOIN jobs j ON j.id = e.job_id "
            отбор, свои = self._owner_clause(owner, "j")
            if отбор:
                where.append(отбор)
                args.extend(свои)
        try:
            rows = self.query(
                "SELECT json_extract(e.data, '$.category') AS category, "
                "       json_extract(e.data, '$.label') AS label, COUNT(*) AS hits, "
                "       COUNT(DISTINCT e.job_id) AS records "
                f"{соединение}WHERE {' AND '.join(where)} "
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
            "       SUM(CASE WHEN COALESCE(c.violations,0) > 0 THEN 1 ELSE 0 END) AS violation_records, "
            # Показатели сверх тональности — тем же рядом: динамика у них
            # важнее среза, по срезу не отличить «всегда так» от «стало так».
            "       AVG(c.mood) AS mood, AVG(c.stress) AS stress, "
            "       AVG(c.effort) AS effort, AVG(c.fatigue) AS fatigue, "
            "       AVG(c.clarity) AS clarity, AVG(c.accuracy) AS accuracy, "
            "       AVG(c.politeness) AS politeness, AVG(c.rhythm) AS rhythm, "
            "       AVG(c.personalization) AS personalization, "
            "       AVG(c.diminutive_rate) AS diminutive_rate, "
            "       AVG(c.filler_rate) AS filler_rate, AVG(c.nps) AS nps, "
            "       SUM(CASE WHEN c.nps_group = 'промоутер' THEN 1 ELSE 0 END) AS promoters, "
            "       SUM(CASE WHEN c.nps_group = 'критик' THEN 1 ELSE 0 END) AS detractors, "
            "       SUM(CASE WHEN c.nps IS NOT NULL THEN 1 ELSE 0 END) AS nps_checked, "
            # Названный балл — своим рядом: на одной оси с предсказанным его
            # видно, а сложенный с ним он пропадает.
            "       AVG(c.nps_said) AS nps_said, "
            "       SUM(CASE WHEN c.nps_said IS NOT NULL THEN 1 ELSE 0 END) AS nps_said_count "
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
        if len(безопасные) == len(columns):
            return [tuple(r) for r in rows]
        # Столбец, которого здесь нет, раньше просто выпадал из выборки — а
        # тот, кто её заказал, продолжал считать по номерам запрошенного
        # списка. Номера съезжали, и «паузы» молча начинали означать «самую
        # долгую паузу»: не ошибка, не исключение, а неверные числа в отчёте.
        # Теперь позиции сохраняются, а неизвестный столбец приходит пустым.
        места = {к: н for н, к in enumerate(безопасные)}
        порядок = [места.get(к) for к in columns]
        return [tuple(None if н is None else r[н] for н in порядок) for r in rows]

    def terms_matrix(self, *, since: float | None = None,
                     until: float | None = None,
                     owner: str | list[str] | None = None,
                     limit: int = 5000,
                     min_terms: int = 5) -> list[tuple[str, dict[str, int]]]:
        """Основы каждой записи окна: готовая разрежённая матрица «запись × слово».

        Берётся из `content_terms`, а не из текстов: таблица заполняется при
        разборе, и повторно разбирать сто тысяч расшифровок ради кластеризации
        незачем — это часы работы вместо секунд.

        Записи короче `min_terms` основ выбрасываются: на трёх словах похожесть
        считается, но означает случайность, и такие записи склеиваются в один
        огромный кластер «здравствуйте — до свидания».
        """
        условие, args = self._content_where(since, until, owner)
        строки = self.query(
            "SELECT t.job_id AS job_id, t.stem AS stem, t.n AS n "
            "FROM content_terms t "
            "JOIN jobs j ON j.id = t.job_id "
            "JOIN content c ON c.job_id = t.job_id "
            f"WHERE {условие} "
            "  AND t.job_id IN ("
            "    SELECT c2.job_id FROM content c2 JOIN jobs j2 ON j2.id = c2.job_id "
            f"    WHERE {условие.replace('j.', 'j2.').replace('c.', 'c2.')} "
            "    ORDER BY j2.created_at DESC LIMIT ?)",
            [*args, *args, max(1, int(limit))])
        по_записям: dict[str, dict[str, int]] = {}
        for строка in строки:
            по_записям.setdefault(str(строка["job_id"]), {})[
                str(строка["stem"])] = int(строка["n"] or 1)
        return [(ид, основы) for ид, основы in по_записям.items()
                if len(основы) >= max(1, int(min_terms))]

    def hits_for_jobs(self, job_ids: list[str]) -> dict[str, list[str]]:
        """Какие категории сработали у каждой из записей.

        Нужно, чтобы отличить тему, под которую правило уже написано, от той,
        которой никто не заметил. Вторая — единственное, ради чего стоит
        кластеризовать: первую и так видно в отчёте по категориям.
        """
        if not job_ids:
            return {}
        итог: dict[str, list[str]] = {}
        # Партиями: SQLite держит предел на число параметров запроса, и на
        # выборке в пять тысяч записей запрос одним куском просто не собрался бы.
        шаг = 400
        for начало in range(0, len(job_ids), шаг):
            кусок = [str(и) for и in job_ids[начало:начало + шаг]]
            места = ",".join("?" for _ in кусок)
            for строка in self.query(
                    f"SELECT job_id, category FROM content_hits "
                    f"WHERE job_id IN ({места})", кусок):
                итог.setdefault(str(строка["job_id"]), []).append(
                    str(строка["category"]))
        return итог

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
        if stems is not None:
            # Перечень основ сам задаёт размер ответа. Предел по умолчанию
            # (60) молча срезал его: из девяноста запрошенных основ прошлого
            # окна возвращались шестьдесят, остальные считались «было 0», и
            # сортировка по величине изменения выносила наверх именно их —
            # «что изменилось» целиком состояло из выдуманных скачков.
            limit = max(int(limit or 0), len(stems))
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

    def term_shift(self, *, since: float, before_since: float,
                   owner: str | list[str] | None = None,
                   size_now: int, size_before: int,
                   min_records: int = 3, limit: int = 15) -> list[dict[str, Any]]:
        """Основы, доля которых сильнее всего изменилась между двумя окнами.

        Окна смежные: прошлое — от `before_since` до `since`, текущее — от
        `since`. Оба считаются одним проходом с условной суммой, и порядок —
        по модулю изменения доли прямо в базе, по ВСЕМ основам обоих окон.
        Прежний способ брал кандидатов из верхушек окон по числу записей, и
        тема, которой не стало совсем (сбой починили, акция кончилась), в
        кандидаты не попадала: в верхушке текущего окна её нет, а в верхушке
        прошлого ей мешали темы почаще — хотя её изменение самое большое.

        `size_now` и `size_before` — число разобранных записей в окнах: доля
        считается от них, как и в своде тем.
        """
        if not size_now or not size_before:
            return []
        условие, args = self._content_where(before_since, None, owner)
        rows = self.query(
            "SELECT t.stem AS stem, "
            "       SUM(CASE WHEN j.created_at >= ? THEN 1 ELSE 0 END) AS now, "
            "       SUM(CASE WHEN j.created_at < ? THEN 1 ELSE 0 END) AS before "
            "FROM content_terms t JOIN jobs j ON j.id = t.job_id "
            f"WHERE {условие} "
            "GROUP BY t.stem HAVING now + before >= ? "
            "ORDER BY ABS(now * ? - before * ?) DESC, now + before DESC, t.stem "
            "LIMIT ?",
            [since, since, *args, int(min_records),
             100.0 / size_now, 100.0 / size_before, int(limit)])
        формы = self.term_words([str(r["stem"]) for r in rows])
        return [{"stem": r["stem"], "word": формы.get(str(r["stem"]), r["stem"]),
                 "now": int(r["now"] or 0), "before": int(r["before"] or 0)}
                for r in rows]

    def new_terms(self, *, since: float, until: float | None = None,
                  before_since: float | None, before_until: float,
                  owner: str | list[str] | None = None,
                  min_records: int = 3, limit: int = 15) -> list[dict[str, Any]]:
        """Основы окна, которых в прошлом окне не было ни в одной записи.

        Отдельный запрос, а не фильтр по верхушке тренда. Раньше новые слова
        искались среди первых сотен основ по числу записей, потом среди
        первых сотни по величине изменения — и только после этого
        отбрасывалось всё, что звучало раньше. Настоящее новое слово редкое по
        определению: «мегаакция» в трёх записях стояла 676-й из 677 и до
        фильтра не доживала никогда, а список пустел.

        Прошлое окно сворачивается в набор основ один раз (`NOT IN` по
        подзапросу без связи с внешним — SQLite строит по нему временный
        указатель), так что стоимость — два прохода по окнам, как у тренда.
        """
        условие, args = self._content_where(since, until, owner)
        условие_было, args_было = self._content_where(before_since, before_until, owner)
        rows = self.query(
            "SELECT t.stem AS stem, COUNT(*) AS records, SUM(t.n) AS mentions "
            "FROM content_terms t JOIN jobs j ON j.id = t.job_id "
            f"WHERE {условие} AND t.stem NOT IN ("
            "  SELECT t2.stem FROM content_terms t2 JOIN jobs j ON j.id = t2.job_id "
            f"  WHERE {условие_было}) "
            "GROUP BY t.stem HAVING records >= ? "
            "ORDER BY records DESC, mentions DESC LIMIT ?",
            [*args, *args_было, int(min_records), int(limit)])
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
        return bool(changed)

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
        self._поколение_реплик += 1
        with self.write() as conn:
            conn.execute("DELETE FROM segments WHERE job_id=?", (job_id,))
            conn.executemany(
                "INSERT INTO segments (job_id, idx, start_s, end_s, text, speaker, "
                "confidence, no_speech, compression, temperature, language, words) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)

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

    def get_kv(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT value FROM kv WHERE key=?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return default

    # --- звонки с АТС ----------------------------------------------------
    #
    # Разговор с телефонной станции — это задание распознавания плюс
    # десяток полей, которых у обычного задания нет: кто кому звонил, через
    # какую очередь, ответили ли. Держать их в `params` задания было
    # заманчиво, но по JSON внутри колонки не построишь ни отбора «входящие
    # за вторник», ни разреза по очереди — а ради них всё и затевалось.

    #: Поля звонка, которые пишутся в таблицу. Перечень явный: словарь
    #: приходит из разбора журнала АТС, и лишний ключ в нём (у разных сборок
    #: Asterisk свой набор) не должен ломать вставку.
    ПОЛЯ_ЗВОНКА = (
        "src", "dst", "clid", "channel", "dstchannel", "context",
        "disposition", "direction", "queue", "agent", "duration", "billsec",
        "answered", "started_at", "recording", "userfield", "accountcode",
        "station", "pbx_uid",
    )

    def save_call(self, uniqueid: str, *, job_id: str | None = None,
                  owner: str | None = None, skipped: str = "",
                  **поля: Any) -> None:
        """Запоминает звонок. Повторный вызов обновляет запись, не задваивая.

        `INSERT OR REPLACE` здесь неуместен: он стирает поля, которых нет в
        текущем вызове, и звонок, сохранённый импортёром со всеми полями, а
        потом дописанный одной пометкой, терял бы номера. Поэтому вставка с
        `ON CONFLICT DO UPDATE` и явным списком того, что обновляется.

        Пустое — не то же самое, что «не передали». `job_id` и `owner`
        пустыми не затираются: пометка «нет записи», поставленная вторым
        вызовом, иначе отвязывала бы звонок от распознавания и от отдела, а
        разрез по владельцу — единственное, что отделяет чужие разговоры от
        своих в отчётах.
        """
        if not uniqueid:
            raise StorageError("Звонок без идентификатора не сохраняется.")
        отложен = str(skipped or "").startswith(self.ОТЛОЖЕН)
        данные: dict[str, Any] = {"uniqueid": str(uniqueid),
                                  "job_id": job_id or None,
                                  "owner": str(owner) if owner else None,
                                  "skipped": str(skipped or ""),
                                  "imported_at": now(),
                                  "deferred_at": now() if отложен else None}
        for имя in self.ПОЛЯ_ЗВОНКА:
            if имя not in поля:
                continue
            значение = поля[имя]
            if имя == "answered":
                значение = 1 if значение else 0
            elif имя in ("duration", "billsec"):
                значение = int(значение or 0)
            elif имя == "started_at":
                значение = float(значение or 0.0)
            else:
                значение = str(значение or "")
            данные[имя] = значение
        колонки = list(данные)
        места = ",".join("?" for _ in колонки)
        УДЕРЖИВАТЬ = ("job_id", "owner")
        обновить = ",".join(
            f"{к}=excluded.{к}" for к in колонки
            if к != "uniqueid" and к not in УДЕРЖИВАТЬ and к != "deferred_at")
        обновить += "".join(f",{к}=COALESCE(excluded.{к}, calls.{к})"
                            for к in УДЕРЖИВАТЬ)
        # Время ПЕРВОГО откладывания держится, пока звонок остаётся
        # отложенным: иначе каждое повторное откладывание продлевало ему
        # жизнь, и безнадёжный звонок не протухал никогда. У строк прежних
        # версий этого времени нет — для них отсчёт идёт от прошлой записи.
        обновить += (
            ",deferred_at=CASE WHEN excluded.deferred_at IS NULL THEN NULL "
            f"WHEN calls.skipped LIKE '{self.ОТЛОЖЕН}%' "
            "THEN COALESCE(calls.deferred_at, calls.imported_at, excluded.deferred_at) "
            "ELSE excluded.deferred_at END")
        self.execute(
            f"INSERT INTO calls ({','.join(колонки)}) VALUES ({места}) "
            f"ON CONFLICT(uniqueid) DO UPDATE SET {обновить}",
            [данные[к] for к in колонки])

    #: Приставка пометки «взять позже». Отличает звонок, который ещё
    #: нельзя распознать, от звонка, который распознавать не надо.
    ОТЛОЖЕН = "отложен: "

    def calls_deferred(self, limit: int = 50, *, station: str = "",
                       older_than: float | None = None) -> list[dict[str, Any]]:
        """Звонки, отложенные до следующего захода, — старые первыми.

        Из журнала они больше не придут: позиция чтения сдвинута сразу
        после чтения, а событие AMI вообще разовое. Без этой очереди
        звонок, у которого запись ещё дописывалась, терялся навсегда — а
        при выдержке в тридцать секунд и опросе раз в минуту под это
        попадала половина потока.

        `station` обязателен, когда станций несколько: без него импортёр
        головного офиса забирал отложенные звонки филиала, обрабатывал их
        своими правилами и переписывал им владельца и станцию — то есть
        разговоры филиала уезжали в чужой отчёт и в чужую очередь, а запись
        искалась в чужом каталоге и не находилась никогда.

        `older_than` отсекает безнадёжных: звонок, чья запись не появится
        уже никогда, иначе навечно занимает место в очереди отложенных, и
        импорт по станции встаёт целиком после одного сбоя записи.

        Срок считается от ПЕРВОГО откладывания (`deferred_at`), а не от
        времени разговора. По времени разговора любой архивный звонок
        выпадал из очереди в тот же миг, как туда попадал: сбор архива
        десятидневной давности, одна переполненная очередь — и разговор
        навсегда оставался с пометкой «отложен» и без задания. И не от
        последней записи строки (`imported_at`): её обновляет каждое
        повторное откладывание, и срок не наступал никогда.
        """
        условия = ["skipped LIKE ? ESCAPE '\\'"]
        args: list[Any] = [f"{_экранировать_like(self.ОТЛОЖЕН)}%"]
        if station:
            условия.append("COALESCE(station,'')=?")
            args.append(str(station))
        if older_than is not None:
            условия.append("COALESCE(deferred_at, imported_at, started_at, 0) >= ?")
            args.append(float(older_than))
        args.append(max(1, int(limit)))
        rows = self.query(
            f"SELECT * FROM calls WHERE {' AND '.join(условия)} "
            "ORDER BY COALESCE(imported_at, started_at, 0) LIMIT ?", args)
        звонки = []
        for r in rows:
            звонок = dict(r)
            звонок["answered"] = bool(звонок.get("answered"))
            звонки.append(звонок)
        return звонки

    #: Приставка окончательного пропуска для отложенного звонка, чья запись
    #: так и не появилась за срок хранения отложенных.
    НЕ_ДОЖДАЛИСЬ = "не дождались записи: "

    def calls_expire_deferred(self, *, station: str = "",
                              older_than: float) -> int:
        """Отложенные дольше срока — в окончательный пропуск с причиной.

        Раньше такой звонок просто переставал выбираться, но оставался
        «отложенным» навсегда: в счётчике отложенных, без ответа на вопрос,
        чем кончилось. Теперь пометка честная — «не дождались записи: ещё
        пишется» (или какая была причина откладывания), и она видна в
        разрезе пропусков.
        """
        условия = ["skipped LIKE ? ESCAPE '\\'",
                   "COALESCE(deferred_at, imported_at, started_at, 0) < ?"]
        args: list[Any] = [f"{_экранировать_like(self.ОТЛОЖЕН)}%", float(older_than)]
        if station:
            условия.append("COALESCE(station,'')=?")
            args.append(str(station))
        return self.execute(
            f"UPDATE calls SET skipped = ? || substr(skipped, ?), deferred_at = NULL "
            f"WHERE {' AND '.join(условия)}",
            [self.НЕ_ДОЖДАЛИСЬ, len(self.ОТЛОЖЕН) + 1, *args])

    def call_owner(self, uniqueid: str) -> str | None:
        """Кому принадлежит строка архива. None — строки нет вовсе.

        Нужно ровно одному вызывающему: маршрут постановки задания собирает
        ключ архива из полей формы, а `save_call` обновляет строку с таким
        ключом, не спрашивая, чья она. Без этой проверки чужой звонок менял
        владельца и пропадал из отчётов того, кому принадлежал.
        """
        if not uniqueid:
            return None
        строка = self.query_one("SELECT owner FROM calls WHERE uniqueid=?",
                                (str(uniqueid),))
        return None if строка is None else (строка["owner"] or "")

    def call_exists(self, uniqueid: str) -> bool:
        """Был ли звонок уже разобран — главный предохранитель импортёра."""
        if not uniqueid:
            return False
        return self.query_one("SELECT 1 FROM calls WHERE uniqueid=?",
                              (str(uniqueid),)) is not None

    #: Сколько известных звонков держать в памяти при обходе каталога.
    ПРЕДЕЛ_ИЗВЕСТНЫХ_ЗВОНКОВ = 100000

    def known_call_ids(self, limit: int | None = None, *,
                       station: str = "") -> set[str]:
        """Множество известных идентификаторов — для обхода папки записей.

        Спрашивать базу по файлу на каждом заходе — это тысячи запросов на
        каталог в десять тысяч записей. Одно множество дешевле и по времени,
        и по блокировкам. Множество ограничено свежими звонками; если в нём
        ровно `limit` — оно неполное, и вызывающий доспрашивает промахи
        через `call_exists` (см. `telephony.importer`).
        """
        где = " WHERE station=?" if station else ""
        args: list[Any] = [station] if station else []
        rows = self.query(
            f"SELECT uniqueid FROM calls{где} ORDER BY imported_at DESC LIMIT ?",
            (*args, max(1, int(limit or self.ПРЕДЕЛ_ИЗВЕСТНЫХ_ЗВОНКОВ))))
        return {str(r["uniqueid"]) for r in rows}

    def call_counts(self, *, owner: str | list[str] | None = None,
                    station: str = "") -> dict[str, Any]:
        """Сводка по звонкам: всего, поставлено, пропущено и почему."""
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        строка = self.query_one(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END) AS queued,"
            "       SUM(CASE WHEN COALESCE(skipped,'')<>'' "
            "                AND skipped NOT LIKE 'отложен: %' THEN 1 ELSE 0 END) AS skipped,"
            "       SUM(CASE WHEN skipped LIKE 'отложен: %' THEN 1 ELSE 0 END) AS deferred,"
            "       SUM(CASE WHEN direction='входящий' THEN 1 ELSE 0 END) AS inbound,"
            "       SUM(CASE WHEN direction='исходящий' THEN 1 ELSE 0 END) AS outbound,"
            "       SUM(COALESCE(billsec,0)) AS talk_s,"
            "       MAX(started_at) AS last_call,"
            "       MAX(imported_at) AS last_import "
            f"FROM calls c{где}", args)
        свод = {к: (строка[к] if строка and строка[к] is not None else 0)
                for к in ("total", "queued", "skipped", "deferred",
                          "inbound", "outbound", "talk_s", "last_call",
                          "last_import")}
        причины = self.query(
            "SELECT skipped AS reason, COUNT(*) AS n FROM calls c "
            "WHERE COALESCE(skipped,'')<>'' AND skipped NOT LIKE 'отложен: %'"
            f"{(' AND ' + условие) if условие else ''} "
            "GROUP BY skipped ORDER BY n DESC LIMIT 20", args)
        свод["reasons"] = {str(r["reason"]): int(r["n"]) for r in причины}
        return свод

    def call_kpi(self, *, owner: str | list[str] | None = None,
                 station: str = "", queue: str = "",
                 since: float = 0.0, until: float = 0.0,
                 service_level_s: float = 20.0,
                 repeat_window_days: float = 7.0) -> dict[str, Any]:
        """Показатели контакт-центра по журналу звонков.

        Всё считается по CDR, который и так лежит в базе: время разговора,
        признак ответа, очередь и номер звонящего. Отдельной интеграции ради
        этих чисел не нужно, а спрашивают их первыми — это тот самый список,
        по которому заказчик сравнивает системы между собой.

        Что именно считается и чего в этих числах НЕТ:

        * `aht_s` — среднее время РАЗГОВОРА по отвеченным. Настоящее «среднее
          время обработки» включает ещё удержание и постобработку; ни того ни
          другого в CDR нет, и называть эту величину полным AHT было бы
          подлогом. Здесь она честно подписана как разговор.
        * `abandoned_share` — доля входящих, на которые не ответили. Считается
          от всех входящих, а не от попавших в очередь: клиенту всё равно,
          дошёл ли его вызов до очереди.
        * `service_level` — доля отвеченных входящих, взятых быстрее порога.
          Ожидание считается как разница между общей длительностью вызова и
          временем разговора: столько вызов звонил, пока его не сняли.
        * `fcr` — доля обращений, после которых тот же номер не позвонил
          снова в течение окна. Это ОЦЕНКА решения с первого раза, а не оно
          само: повторный звонок бывает и по другому поводу. Зато считается
          без единого опроса и по всему архиву сразу.

        Звонки без номера звонящего (`src` пуст) в расчёт FCR не идут: у них
        нет ключа, по которому узнают повторное обращение, и молча считать их
        решёнными значит завышать показатель.

        Повторный звонок ищется по всему архиву, а не внутри периода и не
        внутри станции: клиент, позвонивший первого числа и перезвонивший
        третьего, решённым не был — независимо от того, что отчёт запрошен по
        одному дню и что перезвонил он в филиал.
        """
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        if queue:
            части.append("c.queue=?")
            args = [*args, queue]
        if since:
            части.append("c.started_at>=?")
            args = [*args, float(since)]
        if until:
            части.append("c.started_at<?")
            args = [*args, float(until)]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        порог = max(0.0, float(service_level_s))

        строка = self.query_one(
            "SELECT COUNT(*) AS total,"
            "       SUM(CASE WHEN c.direction='входящий' THEN 1 ELSE 0 END) AS inbound,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                THEN 1 ELSE 0 END) AS inbound_answered,"
            "       SUM(CASE WHEN COALESCE(c.answered,0)=1 THEN 1 ELSE 0 END) AS answered,"
            "       SUM(CASE WHEN COALESCE(c.answered,0)=1"
            "                THEN COALESCE(c.billsec,0) ELSE 0 END) AS talk_s,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                THEN MAX(0, COALESCE(c.duration,0) - COALESCE(c.billsec,0))"
            "                ELSE 0 END) AS wait_s,"
            "       SUM(CASE WHEN c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "                AND MAX(0, COALESCE(c.duration,0) - COALESCE(c.billsec,0)) <= ?"
            "                THEN 1 ELSE 0 END) AS in_time "
            f"FROM calls c{где}", [порог, *args])
        свод = {к: float(строка[к] or 0) if строка else 0.0
                for к in ("total", "inbound", "inbound_answered", "answered",
                          "talk_s", "wait_s", "in_time")}

        окно = max(0.0, float(repeat_window_days)) * 86400.0
        повтор = self.query_one(
            "SELECT COUNT(*) AS base,"
            "       SUM(CASE WHEN EXISTS ("
            "             SELECT 1 FROM calls p"
            "             WHERE p.src = c.src AND p.direction='входящий'"
            "               AND p.started_at > c.started_at"
            "               AND p.started_at <= c.started_at + ?"
            "               AND p.uniqueid <> c.uniqueid)"
            "           THEN 1 ELSE 0 END) AS repeated "
            f"FROM calls c{где}{' AND' if где else ' WHERE'} "
            "  c.direction='входящий' AND COALESCE(c.answered,0)=1"
            "  AND COALESCE(c.src,'')<>''", [окно, *args]) if окно > 0 else None

        основа = float((повтор["base"] if повтор else 0) or 0)
        повторных = float((повтор["repeated"] if повтор else 0) or 0)
        return {
            "total": int(свод["total"]),
            "inbound": int(свод["inbound"]),
            "answered": int(свод["answered"]),
            "inbound_answered": int(свод["inbound_answered"]),
            "abandoned": int(свод["inbound"] - свод["inbound_answered"]),
            "abandoned_share": (round((свод["inbound"] - свод["inbound_answered"])
                                      / свод["inbound"], 4)
                                if свод["inbound"] else None),
            # Среднее время разговора — по отвеченным: делить на все вызовы
            # значит смешивать разговор с гудками.
            "aht_s": (round(свод["talk_s"] / свод["answered"], 1)
                      if свод["answered"] else None),
            "wait_avg_s": (round(свод["wait_s"] / свод["inbound_answered"], 1)
                           if свод["inbound_answered"] else None),
            "service_level": (round(свод["in_time"] / свод["inbound_answered"], 4)
                              if свод["inbound_answered"] else None),
            "service_level_s": порог,
            "fcr": (round(1.0 - повторных / основа, 4) if основа else None),
            "fcr_base": int(основа),
            "repeat_window_days": round(float(repeat_window_days), 2),
        }

    def list_calls(self, *, owner: str | list[str] | None = None,
                   direction: str = "", queue: str = "", agent: str = "",
                   station: str = "", skipped: str = "",
                   since: float | None = None, until: float | None = None,
                   only_queued: bool = False, search: str = "",
                   limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """Журнал звонков с отбором — то, что показывает раздел «Телефония»."""
        where: list[str] = []
        args: list[Any] = []
        условие, свои = self._owner_clause(owner, "c")
        if условие:
            where.append(условие)
            args += свои
        if direction:
            where.append("c.direction=?")
            args.append(direction)
        if queue:
            where.append("c.queue=?")
            args.append(queue)
        if agent:
            where.append("c.agent=?")
            args.append(agent)
        if station:
            where.append("c.station=?")
            args.append(station)
        if skipped == "yes":
            where.append("COALESCE(c.skipped,'')<>'' AND c.skipped NOT LIKE 'отложен: %'")
        elif skipped == "deferred":
            where.append("c.skipped LIKE 'отложен: %'")
        elif skipped == "no":
            where.append("COALESCE(c.skipped,'')=''")
        if since is not None:
            where.append("c.started_at>=?")
            args.append(float(since))
        if until is not None:
            where.append("c.started_at<?")
            args.append(float(until))
        if only_queued:
            where.append("c.job_id IS NOT NULL")
        if search:
            образец = f"%{_экранировать_like(search)}%"
            # И по ключу архива, и по идентификатору станции: в журнале АТС
            # человек видит второй, а в наших ответах — первый, и искать он
            # будет тот, что у него перед глазами.
            where.append("(c.src LIKE ? ESCAPE '\\' OR c.dst LIKE ? ESCAPE '\\' "
                         "OR c.clid LIKE ? ESCAPE '\\' OR c.uniqueid LIKE ? ESCAPE '\\' "
                         "OR c.pbx_uid LIKE ? ESCAPE '\\')")
            args += [образец] * 5
        где = f" WHERE {' AND '.join(where)}" if where else ""
        всего = self.query_one(f"SELECT COUNT(*) AS n FROM calls c{где}", args)
        предел = max(1, min(int(limit), 500))
        rows = self.query(
            "SELECT c.*, j.status AS job_status, j.text AS job_text,"
            "       j.media_duration_s AS job_duration "
            f"FROM calls c LEFT JOIN jobs j ON j.id=c.job_id{где} "
            "ORDER BY c.started_at DESC, c.imported_at DESC LIMIT ? OFFSET ?",
            [*args, предел, max(0, int(offset))])
        звонки = []
        for r in rows:
            звонок = dict(r)
            звонок["answered"] = bool(звонок.get("answered"))
            текст = звонок.pop("job_text", None) or ""
            звонок["preview"] = текст[:200]
            звонки.append(звонок)
        return {"total": int(всего["n"]) if всего else 0, "calls": звонки,
                "limit": предел, "offset": max(0, int(offset))}

    def call_for_job(self, job_id: str) -> dict[str, Any] | None:
        """Карточка задания спрашивает: а это вообще звонок и чей?"""
        строка = self.query_one("SELECT * FROM calls WHERE job_id=?", (str(job_id),))
        if строка is None:
            return None
        звонок = dict(строка)
        звонок["answered"] = bool(звонок.get("answered"))
        return звонок

    def call_dimensions(self, *, owner: str | list[str] | None = None,
                        station: str = "") -> dict[str, list[str]]:
        """Очереди, операторы и станции, которые встречались, — для отбора."""
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, station]
        условие = " AND ".join(части)
        где = f" WHERE {условие}" if условие else ""
        def значения(поле: str) -> list[str]:
            rows = self.query(
                f"SELECT {поле} AS v, COUNT(*) AS n FROM calls c{где} "
                f"{'AND' if где else 'WHERE'} COALESCE({поле},'')<>'' "
                f"GROUP BY {поле} ORDER BY n DESC LIMIT 200", args)
            return [str(r["v"]) for r in rows]
        return {"queues": значения("c.queue"), "agents": значения("c.agent"),
                "directions": значения("c.direction"),
                "stations": значения("c.station")}

    # --- разрезы для раздела «АТС» ----------------------------------------

    def _условия_звонков(self, owner: Any, station: str, since: float | None,
                         until: float | None) -> tuple[list[str], list[Any]]:
        """Общее «где» для всех сводок раздела: владелец, станция, период.

        Одно место вместо пяти: разрез по владельцу — самая частая забытая
        строчка в этом проекте, и повторять её в каждом запросе значит рано
        или поздно один раз не повторить.
        """
        условие, args = self._owner_clause(owner, "c")
        части = [условие] if условие else []
        if station:
            части.append("c.station=?")
            args = [*args, str(station)]
        if since is not None:
            части.append("c.started_at>=?")
            args = [*args, float(since)]
        if until is not None:
            части.append("c.started_at<?")
            args = [*args, float(until)]
        return части, args

    #: Что считаем в каждой корзине времени. Держим одним списком: колонки
    #: нужны и линии нагрузки, и сводке за период, и расходиться они не должны.
    СЧЁТ_ЗВОНКОВ = (
        "COUNT(*) AS total",
        "SUM(CASE WHEN c.direction='входящий' THEN 1 ELSE 0 END) AS inbound",
        "SUM(CASE WHEN c.direction='исходящий' THEN 1 ELSE 0 END) AS outbound",
        "SUM(CASE WHEN c.direction='внутренний' THEN 1 ELSE 0 END) AS internal",
        "SUM(CASE WHEN c.answered=1 THEN 1 ELSE 0 END) AS answered",
        "SUM(CASE WHEN c.job_id IS NOT NULL THEN 1 ELSE 0 END) AS queued",
        "SUM(CASE WHEN COALESCE(c.skipped,'')<>'' "
        "         AND c.skipped NOT LIKE 'отложен: %' THEN 1 ELSE 0 END) AS skipped",
        "SUM(COALESCE(c.billsec,0)) AS talk_s",
        "SUM(CASE WHEN c.answered=1 THEN "
        "    MAX(COALESCE(c.duration,0) - COALESCE(c.billsec,0), 0) ELSE 0 END) AS wait_s",
    )

    ШАГ_КОРЗИНЫ = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}

    #: Эпоха началась в четверг: неделя, отсчитанная от неё делением, шла с
    #: четверга по среду, и «неделя с Thu 10.09» собирала четверг,
    #: воскресенье и понедельник. Первый понедельник эпохи — 5 января 1970.
    СДВИГ_НЕДЕЛИ = 4 * 86400

    def _корзина_звонка(self, шаг: int, сдвиг: int) -> tuple[str, str]:
        """SQL начала корзины (секунды эпохи) и её порядкового номера.

        Час и сутки — арифметикой по местному сдвигу. Неделя — так же, но
        от понедельника. Месяц — календарный: «30 суток» уводили корзины
        от первых чисел на день за месяц, и к декабрю «месяц» начинался
        25 ноября. Календарный месяц считает сам SQLite: `localtime` →
        начало месяца → `utc`; номер корзины — год·12 + месяц, чтобы
        соседние месяцы шли подряд, как и прочие корзины.
        """
        t = "c.started_at"
        if шаг == self.ШАГ_КОРЗИНЫ["month"]:
            начало = (f"CAST(strftime('%s', {t}, 'unixepoch', 'localtime', "
                      "'start of month', 'utc') AS INTEGER)")
            номер = (f"(CAST(strftime('%Y', {t}, 'unixepoch', 'localtime') AS INTEGER) * 12"
                     f" + CAST(strftime('%m', {t}, 'unixepoch', 'localtime') AS INTEGER))")
            return начало, номер
        неделя = self.СДВИГ_НЕДЕЛИ if шаг == self.ШАГ_КОРЗИНЫ["week"] else 0
        номер = f"CAST(({t} + {сдвиг} - {неделя}) / {шаг} AS INTEGER)"
        return f"{номер} * {шаг} + {неделя} - {сдвиг}", номер

    @staticmethod
    def _сдвиг_времени() -> int:
        """Смещение местного времени от UTC, секунды.

        Нужно там, где сутки режутся арифметикой, а не `strftime`: вопрос
        «сколько звонков было в четверг» задаёт человек, живущий по своим
        часам, и сутки у него начинаются в полночь, а не в 03:00.

        Берётся на текущий момент, а не на каждую строку: внутри одного
        отчёта сдвиг обязан быть один, иначе переход на зимнее время
        разрезал бы одни сутки на двое.
        """
        return -int(time.timezone if not time.daylight or not time.localtime().tm_isdst
                    else time.altzone)

    def call_timeline(self, *, owner: str | list[str] | None = None,
                      station: str = "", since: float | None = None,
                      until: float | None = None, bucket: str = "day",
                      limit: int = 2000) -> list[dict[str, Any]]:
        """Звонки по корзинам времени — линия нагрузки в разделе «АТС».

        Корзина считается на стороне SQLite из `started_at`, а не в Python:
        за год звонков набегает миллион, и тащить их в память ради
        группировки по часам — это секунды ожидания при каждом открытии
        раздела.
        """
        шаг = self.ШАГ_КОРЗИНЫ.get(str(bucket), 86400)
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.started_at>0")
        # Суточные и более крупные корзины режутся по МЕСТНОМУ времени, а не
        # по UTC: сервер живёт в UTC, и без поправки сутки начинались в 03:00,
        # ночные звонки попадали во вчера, а соседняя тепловая карта (она
        # считается через `localtime`) противоречила этой же линии в одном
        # и том же ответе.
        сдвиг = self._сдвиг_времени() if шаг >= 86400 else 0
        начало, _ = self._корзина_звонка(шаг, сдвиг)
        rows = self.query(
            f"SELECT {начало} AS bucket, "
            + ", ".join(self.СЧЁТ_ЗВОНКОВ)
            + f" FROM calls c WHERE {' AND '.join(части)} "
            f"GROUP BY bucket ORDER BY bucket LIMIT {int(limit)}", args)
        return [{"t": float(r["bucket"]),
                 **{к: int(r[к] or 0) for к in
                    ("total", "inbound", "outbound", "internal", "answered",
                     "queued", "skipped", "talk_s", "wait_s")}}
                for r in rows]

    def call_timeline_by_station(self, *, owner: str | list[str] | None = None,
                                 since: float | None = None,
                                 until: float | None = None, bucket: str = "day",
                                 points: int = 24) -> dict[str, list[int]]:
        """Короткая лента звонков по каждой станции — линия на её карточке.

        Возвращает ровно `points` последних корзин у всех станций, включая
        пустые: линия с пропущенными сутками врёт о нагрузке сильнее, чем
        линия с нулём, потому что читается как «в этот день было столько
        же».
        """
        шаг = self.ШАГ_КОРЗИНЫ.get(str(bucket), 86400)
        части, args = self._условия_звонков(owner, "", since, until)
        части.append("c.started_at>0")
        сдвиг = self._сдвиг_времени() if шаг >= 86400 else 0
        _, номер = self._корзина_звонка(шаг, сдвиг)
        rows = self.query(
            f"SELECT COALESCE(c.station,'') AS station, "
            f"       {номер} AS bucket, "
            "       COUNT(*) AS n "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY station, bucket", args)
        if not rows:
            return {}
        последняя = max(int(r["bucket"]) for r in rows)
        первая = последняя - max(1, int(points)) + 1
        ленты: dict[str, list[int]] = {}
        for r in rows:
            корзина = int(r["bucket"])
            if корзина < первая:
                continue
            лента = ленты.setdefault(str(r["station"] or ""), [0] * (последняя - первая + 1))
            лента[корзина - первая] = int(r["n"] or 0)
        return ленты

    def call_by_station(self, *, owner: str | list[str] | None = None,
                        since: float | None = None,
                        until: float | None = None) -> list[dict[str, Any]]:
        """Те же счётчики, но в разрезе станций — для сравнения АТС между собой."""
        части, args = self._условия_звонков(owner, "", since, until)
        где = f" WHERE {' AND '.join(части)}" if части else ""
        rows = self.query(
            "SELECT COALESCE(c.station,'') AS station, "
            + ", ".join(self.СЧЁТ_ЗВОНКОВ)
            + f" FROM calls c{где} GROUP BY c.station ORDER BY total DESC", args)
        return [{"station": str(r["station"] or ""),
                 **{к: int(r[к] or 0) for к in
                    ("total", "inbound", "outbound", "internal", "answered",
                     "queued", "skipped", "talk_s", "wait_s")}}
                for r in rows]

    def call_tops(self, field: str, *, owner: str | list[str] | None = None,
                  station: str = "", since: float | None = None,
                  until: float | None = None, limit: int = 15) -> list[dict[str, Any]]:
        """Топ по очереди, оператору, направлению, причине пропуска или номеру.

        Поле не подставляется в запрос как есть: имена колонок в SQL нельзя
        передать параметром, а значит любая опечатка снаружи стала бы
        внедрением. Разрешён только известный список.
        """
        колонки = {"queue": "c.queue", "agent": "c.agent",
                   "direction": "c.direction", "skipped": "c.skipped",
                   "src": "c.src", "dst": "c.dst", "context": "c.context",
                   "disposition": "c.disposition", "station": "c.station"}
        колонка = колонки.get(str(field))
        if колонка is None:
            raise ValueError(f"Неизвестный разрез звонков: {field}")
        части, args = self._условия_звонков(owner, station, since, until)
        части.append(f"COALESCE({колонка},'')<>''")
        if field == "skipped":
            # Отложенные — не причина пропуска, а «ещё подождём»: смешивать
            # их с «нет записи» значит каждый раз объяснять человеку, почему
            # в причинах лидирует строка, которая сама рассосётся.
            части.append(f"{колонка} NOT LIKE 'отложен: %'")
        rows = self.query(
            f"SELECT {колонка} AS value, COUNT(*) AS n, "
            "       SUM(COALESCE(c.billsec,0)) AS talk_s, "
            # Среднее — только по отвеченным: недозвон длится ноль секунд,
            # и в знаменателе он занижает «средний разговор» ровно на свою
            # долю. Соседняя гистограмма длительностей считает так же.
            "       AVG(CASE WHEN c.answered=1 THEN COALESCE(c.billsec,0) END) AS avg_s, "
            "       SUM(CASE WHEN c.answered=1 THEN 1 ELSE 0 END) AS answered "
            f"FROM calls c WHERE {' AND '.join(части)} "
            f"GROUP BY {колонка} ORDER BY n DESC LIMIT {int(limit)}", args)
        return [{"value": str(r["value"]), "count": int(r["n"] or 0),
                 "talk_s": int(r["talk_s"] or 0),
                 "avg_s": round(float(r["avg_s"] or 0), 1),
                 "answered": int(r["answered"] or 0)} for r in rows]

    def call_load_heatmap(self, *, owner: str | list[str] | None = None,
                          station: str = "", since: float | None = None,
                          until: float | None = None) -> dict[str, Any]:
        """День недели × час: когда звонят. Основание для графика смен.

        Время местное — то же, в котором человек смотрит на расписание.
        SQLite переводит эпоху в местное время сам (`localtime`), и делать
        это в Python значило бы тащить сюда все звонки поимённо.
        """
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.started_at>0")
        rows = self.query(
            "SELECT CAST(strftime('%w', c.started_at, 'unixepoch', 'localtime') AS INTEGER) AS dow,"
            "       CAST(strftime('%H', c.started_at, 'unixepoch', 'localtime') AS INTEGER) AS hour,"
            "       COUNT(*) AS n, SUM(COALESCE(c.billsec,0)) AS talk_s "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY dow, hour", args)
        # В SQLite неделя начинается с воскресенья; у нас — с понедельника.
        сетка = [[0] * 24 for _ in range(7)]
        разговор = [[0] * 24 for _ in range(7)]
        for r in rows:
            день = (int(r["dow"] or 0) + 6) % 7
            час = max(0, min(23, int(r["hour"] or 0)))
            сетка[день][час] = int(r["n"] or 0)
            разговор[день][час] = int(r["talk_s"] or 0)
        return {"days": ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"],
                "hours": list(range(24)), "calls": сетка, "talk_s": разговор}

    #: Границы корзин длительности, секунды. До полминуты — «не разговор»,
    #: дальше шаг растёт: разницу между 20 и 25 минутами никто не смотрит.
    КОРЗИНЫ_ДЛИТЕЛЬНОСТИ = (30, 60, 120, 300, 600, 1200, 1800)

    def call_duration_histogram(self, *, owner: str | list[str] | None = None,
                                station: str = "", since: float | None = None,
                                until: float | None = None) -> list[dict[str, Any]]:
        """Сколько звонков какой длины. Среднее без этого обманывает.

        Средняя длительность в три минуты бывает и у равномерных разговоров,
        и у сотни тридцатисекундных плюс десятка получасовых — а это разные
        организации с разными проблемами.
        """
        части, args = self._условия_звонков(owner, station, since, until)
        части.append("c.answered=1")
        границы = self.КОРЗИНЫ_ДЛИТЕЛЬНОСТИ
        случаи = " ".join(
            f"WHEN COALESCE(c.billsec,0) < {г} THEN {номер}"
            for номер, г in enumerate(границы))
        rows = self.query(
            f"SELECT CASE {случаи} ELSE {len(границы)} END AS корзина,"
            "        COUNT(*) AS n, SUM(COALESCE(c.billsec,0)) AS talk_s "
            f"FROM calls c WHERE {' AND '.join(части)} "
            "GROUP BY корзина ORDER BY корзина", args)
        имена = []
        предыдущая = 0
        for г in границы:
            имена.append(f"{предыдущая // 60 if предыдущая >= 60 else предыдущая}"
                         f"{'м' if предыдущая >= 60 else 'с'}–"
                         f"{г // 60 if г >= 60 else г}{'м' if г >= 60 else 'с'}")
            предыдущая = г
        имена.append(f"дольше {границы[-1] // 60}м")
        по_корзинам = {int(r["корзина"]): (int(r["n"] or 0), int(r["talk_s"] or 0))
                       for r in rows}
        return [{"label": имя,
                 "count": по_корзинам.get(н, (0, 0))[0],
                 "talk_s": по_корзинам.get(н, (0, 0))[1]}
                for н, имя in enumerate(имена)]

    def forget_calls(self, *, before: float) -> int:
        """Убирает старые записи журнала звонков вместе с их заданиями.

        Сама запись занимает сотни байт, но за год их набегает миллион, и
        обход папки записей начинает читать это множество целиком. Срок
        хранения тот же, что у заданий: звонок без задания бесполезен.
        """
        return self.execute("DELETE FROM calls WHERE imported_at < ?",
                            (float(before),))

    # --- сотрудники -------------------------------------------------------

    #: Поля карточки сотрудника, которые приходят снаружи. Список закрытый:
    #: он же — белый список колонок для записи, и через него нельзя
    #: дописать в таблицу ничего постороннего.
    ПОЛЯ_СОТРУДНИКА = (
        "external_id", "source", "active", "active_until",
        "last_name", "first_name", "middle_name", "position", "department",
        "phone_work", "phone_ext", "phone_mobile", "email", "suppliers",
        "note", "owner",
    )

    def employee_save(self, данные: dict[str, Any], *,
                      id: str | None = None) -> dict[str, Any]:       # noqa: A002
        """Заводит или обновляет карточку сотрудника; возвращает её целиком.

        Ключ поиска — сначала явный `id`, потом пара «источник и внешний
        ключ». Вторая нужна импорту: справочник отдаёт одних и тех же людей
        каждый раз, и без неё повторный импорт заводил бы отдел заново.
        """
        поля = {к: данные.get(к) for к in self.ПОЛЯ_СОТРУДНИКА if к in данные}
        поля.setdefault("source", "ручной")
        if "active" in поля:
            поля["active"] = 1 if поля["active"] else 0
        существующий = None
        if id:
            существующий = self.employee_get(str(id))
        if существующий is None and поля.get("external_id"):
            существующий = self.query_one(
                "SELECT * FROM employees WHERE source=? AND external_id=?",
                (str(поля.get("source") or "ручной"), str(поля["external_id"])))
            существующий = dict(существующий) if существующий else None
        мгновение = now()
        if существующий is None:
            новый = new_id("emp")
            колонки = ["id", *поля.keys(), "created_at", "updated_at"]
            значения = [новый, *поля.values(), мгновение, мгновение]
            self.execute(
                f"INSERT INTO employees ({', '.join(колонки)}) "
                f"VALUES ({', '.join('?' * len(колонки))})", значения)
            return self.employee_get(новый) or {}
        if not поля:
            return существующий
        назначения = ", ".join(f"{к}=?" for к in поля)
        self.execute(f"UPDATE employees SET {назначения}, updated_at=? WHERE id=?",
                     [*поля.values(), мгновение, существующий["id"]])
        return self.employee_get(str(существующий["id"])) or {}

    def employee_get(self, id: str) -> dict[str, Any] | None:         # noqa: A002
        row = self.query_one("SELECT * FROM employees WHERE id=?", (str(id),))
        return dict(row) if row else None

    def employee_by_key(self, source: str, external_id: str) -> dict[str, Any] | None:
        """Карточка по паре «источник и внешний ключ» — так её узнаёт импорт."""
        if not external_id:
            return None
        row = self.query_one(
            "SELECT * FROM employees WHERE source=? AND external_id=?",
            (str(source or "ручной"), str(external_id)))
        return dict(row) if row else None

    def employee_by_ext(self, номер: str) -> dict[str, Any] | None:
        """Сотрудник по внутреннему номеру — так звонок узнаёт оператора."""
        номер = str(номер or "").strip()
        if not номер:
            return None
        row = self.query_one(
            "SELECT * FROM employees WHERE phone_ext=? ORDER BY active DESC, updated_at DESC",
            (номер,))
        return dict(row) if row else None

    def employee_list(self, *, query: str = "", department: str = "",
                      active: bool | None = None, source: str = "",
                      limit: int = 500, offset: int = 0) -> dict[str, Any]:
        условия, параметры = [], []
        if query:
            # «_» и «%» — буквы запроса («ivan_petrov@…»), а не образцы LIKE.
            искомое = f"%{_экранировать_like(query.strip().lower())}%"
            условия.append(
                "(LOWER(last_name) LIKE ? ESCAPE '\\' OR LOWER(first_name) LIKE ? ESCAPE '\\' OR "
                " LOWER(middle_name) LIKE ? ESCAPE '\\' OR LOWER(position) LIKE ? ESCAPE '\\' OR "
                " LOWER(department) LIKE ? ESCAPE '\\' OR LOWER(email) LIKE ? ESCAPE '\\' OR "
                " phone_ext LIKE ? ESCAPE '\\' OR phone_mobile LIKE ? ESCAPE '\\' OR "
                " phone_work LIKE ? ESCAPE '\\')")
            параметры.extend([искомое] * 9)
        if department:
            условия.append("department=?")
            параметры.append(department)
        if source:
            условия.append("source=?")
            параметры.append(source)
        if active is not None:
            условия.append("active=?")
            параметры.append(1 if active else 0)
        где = (" WHERE " + " AND ".join(условия)) if условия else ""
        всего = self.query_one(f"SELECT COUNT(*) AS n FROM employees{где}", параметры)
        rows = self.query(
            f"SELECT * FROM employees{где} ORDER BY active DESC, last_name, first_name, id "
            f"LIMIT ? OFFSET ?", [*параметры, max(1, int(limit)), max(0, int(offset))])
        return {"total": int(всего["n"]) if всего else 0,
                "items": [dict(r) for r in rows]}

    def employee_delete(self, id: str) -> bool:                       # noqa: A002
        return self.execute("DELETE FROM employees WHERE id=?", (str(id),)) > 0

    def employee_departments(self) -> list[dict[str, Any]]:
        rows = self.query(
            "SELECT department, COUNT(*) AS n, "
            "       SUM(CASE WHEN active=1 THEN 1 ELSE 0 END) AS active "
            "FROM employees GROUP BY department ORDER BY n DESC")
        return [dict(r) for r in rows]

    def employees_import(self, записи: list[dict[str, Any]], *, source: str,
                         deactivate_missing: bool = True) -> dict[str, Any]:
        """Разом заводит выгрузку справочника. Возвращает, что изменилось.

        Пропавших из выгрузки не удаляем, а помечаем неработающими: по ним
        есть звонки, и удаление превратило бы половину архива в разговоры
        неизвестно с кем. Это же и честнее: человек уволился, а разговоры
        остались.
        """
        было = {str(r["external_id"]): dict(r) for r in self.query(
            "SELECT * FROM employees WHERE source=?", (source,)) if r["external_id"]}
        добавлено = обновлено = без_изменений = 0
        пришли: set[str] = set()
        for запись in записи:
            данные = {к: з for к, з in запись.items() if к in self.ПОЛЯ_СОТРУДНИКА}
            данные["source"] = source
            ключ = str(данные.get("external_id") or "").strip()
            if not ключ:
                continue
            пришли.add(ключ)
            прежний = было.get(ключ)
            if прежний is None:
                self.employee_save(данные)
                добавлено += 1
                continue
            различия = {к: з for к, з in данные.items()
                        if str(прежний.get(к) or "") != str(з or "")}
            if not различия:
                без_изменений += 1
                continue
            self.employee_save(данные, id=str(прежний["id"]))
            обновлено += 1
        уволено = 0
        if deactivate_missing:
            for ключ, прежний in было.items():
                if ключ not in пришли and int(прежний.get("active") or 0):
                    self.execute("UPDATE employees SET active=0, updated_at=? WHERE id=?",
                                 (now(), прежний["id"]))
                    уволено += 1
        return {"added": добавлено, "updated": обновлено, "unchanged": без_изменений,
                "deactivated": уволено, "received": len(пришли)}

    # --- агенты на станциях ------------------------------------------------

    ПОЛЯ_АГЕНТА = ("name", "host", "station", "version", "asterisk", "os",
                   "source", "state", "enabled", "owner")

    def agent_save(self, id: str, данные: dict[str, Any] | None = None,  # noqa: A002
                   *, seen: bool = True) -> dict[str, Any]:
        """Заводит или обновляет агента, приславшего о себе весть."""
        поля = {к: з for к, з in (данные or {}).items() if к in self.ПОЛЯ_АГЕНТА}
        if "enabled" in поля:
            поля["enabled"] = 1 if поля["enabled"] else 0
        мгновение = now()
        прежний = self.agent_get(id)
        if прежний is None:
            колонки = ["id", *поля.keys(), "first_seen", "last_seen"]
            значения = [str(id), *поля.values(), мгновение, мгновение]
            self.execute(
                f"INSERT INTO agents ({', '.join(колонки)}) "
                f"VALUES ({', '.join('?' * len(колонки))})", значения)
            return self.agent_get(id) or {}
        назначения = [f"{к}=?" for к in поля]
        параметры = list(поля.values())
        if seen:
            назначения.append("last_seen=?")
            параметры.append(мгновение)
        if назначения:
            self.execute(f"UPDATE agents SET {', '.join(назначения)} WHERE id=?",
                         [*параметры, str(id)])
        return self.agent_get(id) or {}

    def agent_get(self, id: str) -> dict[str, Any] | None:            # noqa: A002
        row = self.query_one("SELECT * FROM agents WHERE id=?", (str(id),))
        return dict(row) if row else None

    def agent_list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.query(
            "SELECT * FROM agents ORDER BY last_seen DESC")]

    def agent_delete(self, id: str) -> bool:                          # noqa: A002
        return self.execute("DELETE FROM agents WHERE id=?", (str(id),)) > 0

    def agent_count(self, id: str, *, calls: int = 0, files: int = 0,  # noqa: A002
                    size: int = 0, errors: int = 0, error: str = "") -> None:
        """Счётчики агента. Пишутся одним запросом: их шлют часто."""
        self.execute(
            "UPDATE agents SET calls_sent=calls_sent+?, files_sent=files_sent+?, "
            "bytes_sent=bytes_sent+?, errors=errors+?, "
            "last_error=CASE WHEN ?<>'' THEN ? ELSE last_error END, last_seen=? "
            "WHERE id=?",
            (int(calls), int(files), int(size), int(errors), error, error, now(), str(id)))

    def agent_command_set(self, id: str, команда: str) -> bool:       # noqa: A002
        return self.execute("UPDATE agents SET command=?, command_at=? WHERE id=?",
                            (команда, now(), str(id))) > 0

    def agent_command_take(self, id: str) -> str:                     # noqa: A002
        """Отдаёт команду агенту и тут же её снимает — чтобы не повторялась.

        Чтение и снятие — одним действием под замком записи. Раньше это
        были два шага, и между ними успевал вклиниться второй заход того же
        агента: агент опрашивает сервер по расписанию, а сеть иногда
        задваивает запрос — обе стороны читали «собрать всё» и обе его
        выполняли. Сбор архива запускался дважды, шёл по журналу с одной
        позиции и грузил станцию вдвое. Проба на восьми одновременных
        обращениях ловила повтор примерно в каждом двадцатом заходе.
        """
        with self.write() as conn:
            строка = conn.execute(
                "SELECT command FROM agents WHERE id=?", (str(id),)).fetchone()
            команда = str((строка["command"] if строка else "") or "")
            if команда:
                conn.execute("UPDATE agents SET command='' WHERE id=?", (str(id),))
        return команда

    # --- обратная запись в CRM ----------------------------------------------

    #: Состояния обратной записи по заданию. Пусто — ещё не решали.
    CRM_ЖДЁТ_МОДЕЛЬ = "ждёт модель"
    CRM_ОТПРАВЛЯЕТСЯ = "отправляется"
    CRM_ОТПРАВЛЕНО = "отправлено"
    CRM_БЕЗ_СДЕЛКИ = "без сделки"

    def crm_claim(self, job_id: str) -> bool:
        """Занимает отправку примечания по заданию; False — уже отправляли.

        Одним UPDATE с условием: два пути, дошедшие до отправки разом
        (разбор модели закончился, пока шла досылка по сроку, или два
        сервера на общей базе), не отправят примечание дважды — дубль в
        чужой ленте заметнее и неприятнее пропуска.
        """
        return self.execute(
            "UPDATE jobs SET crm_status=?, crm_at=? WHERE id=? "
            "AND COALESCE(crm_status,'') IN ('', ?, ?)",
            (self.CRM_ОТПРАВЛЯЕТСЯ, now(), str(job_id),
             self.CRM_ЖДЁТ_МОДЕЛЬ, self.CRM_БЕЗ_СДЕЛКИ)) > 0

    def crm_mark(self, job_id: str, status: str, *, только_если: str | None = None) -> bool:
        """Ставит состояние обратной записи. `updated_at` задания не трогает:
        отметка о CRM — не правка записи, и списки «недавно изменённых» от
        неё не должны перестраиваться."""
        if только_если is None:
            return self.execute("UPDATE jobs SET crm_status=?, crm_at=? WHERE id=?",
                                (str(status)[:300], now(), str(job_id))) > 0
        return self.execute(
            "UPDATE jobs SET crm_status=?, crm_at=? WHERE id=? AND COALESCE(crm_status,'')=?",
            (str(status)[:300], now(), str(job_id), только_если)) > 0

    def crm_waiting(self, limit: int = 50) -> list[dict[str, Any]]:
        """Задания, чьё примечание ждёт смыслового разбора, — со строкой очереди."""
        строки = self.query(
            "SELECT j.id AS id, j.crm_at AS crm_at, q.state AS llm_state "
            "FROM jobs j LEFT JOIN llm_queue q ON q.job_id=j.id "
            "WHERE j.crm_status=? ORDER BY j.crm_at LIMIT ?",
            (self.CRM_ЖДЁТ_МОДЕЛЬ, max(1, int(limit))))
        return [dict(с) for с in строки]

    # --- очередь запросов к языковой модели --------------------------------

    #: Состояния очереди. Строками, а не числами: их читает человек в
    #: разделе, и «ждёт» понятнее, чем 0.
    LLMQ_ЖДЁТ = "ждёт"
    LLMQ_ИДЁТ = "идёт"
    LLMQ_ГОТОВО = "готово"
    LLMQ_ОШИБКА = "ошибка"
    LLMQ_ОТМЕНЁН = "отменён"

    def llmq_put(self, job_id: str, *, kind: str = "разбор", priority: int = 50,
                 source: str = "", owner: str = "") -> bool:
        """Ставит запись в очередь к модели. Повтор не задваивает.

        Запись, которая уже ждёт, поднимается в приоритете, если новый
        выше: нажатие «разобрать сейчас» по записи, стоящей в хвосте
        фоновой очереди, должно её двигать, а не создавать вторую.
        """
        # Одним выражением, а не «прочитать, потом записать». Между двумя
        # шагами успевал вклиниться фоновый поток: кнопка «Разобрать
        # моделью» читала «готово», поток тем временем брал запись в
        # работу, а кнопка возвращала её в «ждёт» — модель разбирала одну
        # запись дважды. Одновременная вставка двух новых давала UNIQUE и
        # ошибку записи в базу. Идущую строку выражение не трогает вовсе.
        #
        # Счётчик заходов обнуляется, когда запись приходит заново из
        # конечного состояния (готово, ошибка, отменено): новая просьба —
        # новые попытки, а не «последняя из трёх прежних».
        мгновение = now()
        return self.execute(
            "INSERT INTO llm_queue (job_id, kind, state, priority, enqueued_at, "
            "source, owner) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(job_id) DO UPDATE SET "
            "kind=excluded.kind, priority=MAX(llm_queue.priority, excluded.priority), "
            "enqueued_at=CASE WHEN llm_queue.state=? THEN llm_queue.enqueued_at "
            "ELSE excluded.enqueued_at END, "
            "attempts=CASE WHEN llm_queue.state=? THEN llm_queue.attempts ELSE 0 END, "
            "state=?, error='', finished_at=NULL "
            "WHERE llm_queue.state<>?",
            (str(job_id), kind, self.LLMQ_ЖДЁТ, int(priority), мгновение, source, owner,
             self.LLMQ_ЖДЁТ, self.LLMQ_ЖДЁТ, self.LLMQ_ЖДЁТ, self.LLMQ_ИДЁТ)) > 0

    def llmq_take(self, *, instance: str = "") -> dict[str, Any] | None:
        """Берёт следующую запись и помечает её выполняющейся.

        Отбор и пометка — одним запросом под замком записи: иначе два
        потока (фоновый разбор и ручной вызов из интерфейса) взяли бы одну
        и ту же запись и сходили бы к модели дважды. Строка подписывается
        экземпляром сервера (`instance`) и отметкой жизни — по ним сосед на
        общей базе отличает чужой идущий разбор от брошенного.
        """
        from .instance import INSTANCE_ID  # noqa: PLC0415

        мгновение = now()
        with self.write() as conn:
            строка = conn.execute(
                "SELECT * FROM llm_queue WHERE state=? "
                "ORDER BY priority DESC, enqueued_at LIMIT 1",
                (self.LLMQ_ЖДЁТ,)).fetchone()
            if строка is None:
                return None
            conn.execute(
                "UPDATE llm_queue SET state=?, started_at=?, attempts=attempts+1, "
                "instance=?, heartbeat_at=? WHERE job_id=?",
                (self.LLMQ_ИДЁТ, мгновение, instance or INSTANCE_ID, мгновение,
                 строка["job_id"]))
            данные = dict(строка)
        данные["state"] = self.LLMQ_ИДЁТ
        return данные

    def llmq_begin(self, job_id: str, *, kind: str = "вручную",
                   priority: int = 70, instance: str = "") -> bool:
        """Отмечает, что к записи пошли прямо сейчас, мимо очереди.

        Разбор по кнопке «Разобрать сейчас» идёт в потоке запроса, а не в
        фоновом: человек ждёт ответа. Но в разделе очереди он обязан быть
        виден — иначе «сейчас ничего не идёт» соседствует с работающей
        видеокартой, и понять, кто её занял, нельзя.

        Возвращает False, если запись УЖЕ разбирают. Раньше пометка ставилась
        безусловно, и кнопка по записи, которую в этот момент жевал фоновый
        поток, отправляла к модели второй запрос о том же самом: две оплаты
        времени видеокарты за один ответ, два ответа поверх друг друга в
        базе — и предел `llm_max_concurrent` тут не спасает, он про
        одновременность, а не про повтор.

        Отметка ставится одним действием под замком записи: две кнопки,
        нажатые разом, иначе обе прочитали бы «свободно».
        """
        from .instance import INSTANCE_ID  # noqa: PLC0415

        мгновение = now()
        чей = instance or INSTANCE_ID
        with self.write() as conn:
            строка = conn.execute(
                "SELECT state FROM llm_queue WHERE job_id=?", (str(job_id),)).fetchone()
            if строка is not None and str(строка["state"]) == self.LLMQ_ИДЁТ:
                return False
            if строка is None:
                conn.execute(
                    "INSERT INTO llm_queue (job_id, kind, state, priority, enqueued_at, "
                    "started_at, attempts, instance, heartbeat_at) "
                    "VALUES (?,?,?,?,?,?,1,?,?)",
                    (str(job_id), kind, self.LLMQ_ИДЁТ, int(priority), мгновение,
                     мгновение, чей, мгновение))
                return True
            conn.execute(
                "UPDATE llm_queue SET state=?, kind=?, priority=MAX(priority, ?), "
                "started_at=?, attempts=attempts+1, error='', finished_at=NULL, "
                "instance=?, heartbeat_at=? WHERE job_id=?",
                (self.LLMQ_ИДЁТ, kind, int(priority), мгновение, чей, мгновение,
                 str(job_id)))
            return True

    def llmq_prune(self, keep_days: int = 14, *, limit: int = 20000) -> int:
        """Убирает старые завершённые записи очереди.

        История нужна для графика и разбора сбоев, но не вечно: на сервере,
        разбирающем тысячу записей в сутки, таблица за год станет больше
        самого архива разборов.
        """
        порог = now() - max(1, int(keep_days)) * 86400
        return self.execute(
            "DELETE FROM llm_queue WHERE state IN (?,?,?) AND finished_at<? "
            "AND job_id IN (SELECT job_id FROM llm_queue WHERE finished_at<? "
            "ORDER BY finished_at LIMIT ?)",
            (self.LLMQ_ГОТОВО, self.LLMQ_ОШИБКА, self.LLMQ_ОТМЕНЁН,
             порог, порог, max(1, int(limit))))

    def llmq_finish(self, job_id: str, *, error: str = "", latency_ms: int | None = None,
                    calls: int = 0, chunks: int = 0) -> None:
        self.execute(
            "UPDATE llm_queue SET state=?, finished_at=?, error=?, latency_ms=?, "
            "calls=?, chunks=? WHERE job_id=?",
            (self.LLMQ_ОШИБКА if error else self.LLMQ_ГОТОВО, now(), error,
             latency_ms, int(calls), int(chunks), str(job_id)))

    def llmq_release(self, job_id: str, *, error: str = "",
                     attempts_max: int = 3, потратить: bool = True) -> bool:
        """Возвращает взятую запись в очередь — сходим к модели ещё раз.

        Сбой сбою рознь. Сервер модели не отвечает, видеокарта занята,
        ответ не дочитался — это про сейчас, а не про запись: через минуту
        всё получится. Раньше такая запись оставалась в состоянии «идёт»
        навсегда: `llmq_take` берёт только ждущих, уборка по сроку ходит по
        завершённым, кнопка «Очистить» идущих не трогает намеренно — и
        строка занимала раздел вечным «сейчас разбирается», а сама запись
        не разбиралась больше никогда.

        Но и возвращать бесконечно нельзя: запись, на которой модель падает
        сама по себе, крутилась бы в очереди до скончания века. Поэтому
        считаются заходы (`attempts` растёт в `llmq_take`), и после
        `attempts_max` запись честно уходит в «ошибку» — оттуда её поднимет
        кнопка «Повторить упавшие», когда причину устранят.

        `потратить=False` — сбой не про запись, а про сервер модели: он не
        отвечает на соединение, перезапускается, занят (502/503/504) или не
        хватает видеопамяти. Такой заход не считается: минутный перезапуск
        Ollama раньше уводил головные записи очереди в «ошибку» за полминуты
        — три захода подряд, и каждый отказ мгновенный.

        Returns:
            True, если запись вернулась в очередь; False — если ушла в «ошибку».
        """
        строка = self.query_one("SELECT attempts FROM llm_queue WHERE job_id=?",
                                (str(job_id),))
        if строка is None:
            return False
        заходов = int(строка["attempts"] or 0)
        if потратить and заходов >= max(1, int(attempts_max)):
            self.llmq_finish(job_id, error=error or "разбор не удался")
            return False
        self.execute(
            "UPDATE llm_queue SET state=?, started_at=NULL, error=?, instance='', "
            "heartbeat_at=NULL, finished_at=NULL, "
            "attempts=CASE WHEN ? THEN attempts ELSE MAX(0, attempts-1) END "
            "WHERE job_id=?",
            (self.LLMQ_ЖДЁТ, error, 1 if потратить else 0, str(job_id)))
        return True

    def llmq_reset_running(self, *, instance: str = "",
                           stale_s: float | None = None, свои: bool = True) -> int:
        """Возвращает в очередь то, что «шло» у этого сервера или у мёртвых.

        Без этого запись, на которой сервер перезапустили, оставалась бы
        выполняющейся навсегда: раздел показывал бы вечный текущий запрос,
        а сама запись больше никогда не разобралась бы.

        Но возвращается только своё и брошенное. Раньше сервер при старте
        возвращал в очередь ВСЕ идущие строки — и строку, которую прямо
        сейчас разбирает соседний сервер на общей базе: запись разбиралась
        второй раз, и победителем в базе оказывался тот ответ, что пришёл
        позже. Брошенной считается строка процесса этой машины, которого
        больше нет, строка без подписи (так её оставляли прежние версии) и
        строка, чья отметка жизни старше `stale_s` (сервер на другой машине
        умер или потерял базу).

        `свои=False` — обход на ходу: свои идущие строки живы (их разбирает
        этот же процесс), подбирается только брошенное соседями.
        """
        from .instance import INSTANCE_ID, own_host, process_alive  # noqa: PLC0415

        свой = instance or INSTANCE_ID
        строки = self.query(
            "SELECT job_id, instance, heartbeat_at FROM llm_queue WHERE state=?",
            (self.LLMQ_ИДЁТ,))
        порог = now() - float(stale_s) if stale_s else None
        вернуть = []
        for строка in строки:
            чей = str(строка["instance"] or "")
            отметка = строка["heartbeat_at"]
            if чей == свой and not свои:
                continue
            if (not чей or чей == свой
                    or (own_host(чей) and not process_alive(чей))
                    or (порог is not None and (отметка is None or float(отметка) < порог))):
                вернуть.append(str(строка["job_id"]))
        вернулось = 0
        for job_id in вернуть:
            # Условие состояния повторяется в самом UPDATE: между чтением и
            # записью строку мог закончить её хозяин.
            вернулось += self.execute(
                "UPDATE llm_queue SET state=?, started_at=NULL, instance='', "
                "heartbeat_at=NULL WHERE job_id=? AND state=?",
                (self.LLMQ_ЖДЁТ, job_id, self.LLMQ_ИДЁТ))
        return вернулось

    def llmq_heartbeat(self, *, instance: str = "") -> int:
        """Отметка жизни у всех своих идущих строк очереди модели."""
        from .instance import INSTANCE_ID  # noqa: PLC0415

        return self.execute(
            "UPDATE llm_queue SET heartbeat_at=? WHERE state=? AND instance=?",
            (now(), self.LLMQ_ИДЁТ, instance or INSTANCE_ID))

    def llmq_cancel(self, job_id: str) -> bool:
        return self.execute(
            "UPDATE llm_queue SET state=?, finished_at=? WHERE job_id=? AND state=?",
            (self.LLMQ_ОТМЕНЁН, now(), str(job_id), self.LLMQ_ЖДЁТ)) > 0

    def llmq_clear(self, state: str = "") -> int:
        if state:
            return self.execute("DELETE FROM llm_queue WHERE state=?", (state,))
        return self.execute("DELETE FROM llm_queue WHERE state<>?", (self.LLMQ_ИДЁТ,))

    def _llmq_откуда(self, owner: str | list[str] | None) -> tuple[str, str, list[Any]]:
        """FROM и условие для сводок очереди разбора — с разрезом по владельцу."""
        своё, своё_args = self._owner_clause(owner, "j")
        if not своё:
            return "FROM llm_queue q", "1=1", []
        return "FROM llm_queue q JOIN jobs j ON j.id=q.job_id", своё, своё_args

    def llmq_retry_failed(self, limit: int = 500, *,
                          owner: str | list[str] | None = None,
                          priority: int = 30) -> int:
        """Упавшие записи — обратно в очередь.

        С важностью архива по умолчанию, а не выше свежих записей: «повторить
        упавшие» — это сотни записей разом, и свежие звонки ждали бы, пока
        переберётся весь этот хвост.
        """
        откуда, своё, своё_args = self._llmq_откуда(owner)
        строки = self.query(
            f"SELECT q.job_id AS job_id {откуда} WHERE q.state=? AND {своё} "
            "ORDER BY q.finished_at DESC LIMIT ?",
            (self.LLMQ_ОШИБКА, *своё_args, max(1, int(limit))))
        for строка in строки:
            self.llmq_put(str(строка["job_id"]), kind="повтор", priority=int(priority))
        return len(строки)

    def llmq_counts(self, *, owner: str | list[str] | None = None) -> dict[str, int]:
        откуда, своё, своё_args = self._llmq_откуда(owner)
        строки = self.query(
            f"SELECT q.state AS state, COUNT(*) AS n {откуда} WHERE {своё} GROUP BY q.state",
            своё_args)
        свод = {str(с["state"]): int(с["n"]) for с in строки}
        for состояние in (self.LLMQ_ЖДЁТ, self.LLMQ_ИДЁТ, self.LLMQ_ГОТОВО,
                          self.LLMQ_ОШИБКА, self.LLMQ_ОТМЕНЁН):
            свод.setdefault(состояние, 0)
        return свод

    def llmq_list(self, *, state: str = "", limit: int = 100,
                  offset: int = 0,
                  owner: str | list[str] | None = None) -> dict[str, Any]:
        """Очередь с именем файла рядом: по идентификатору задания человек
        ничего не узнаёт, а раздел показывает именно список записей.

        Разрез по владельцу обязателен: имя файла — это в колл-центре номер
        клиента, и соседние списки заданий и очереди распознавания его
        прячут. Здесь он уходил любому ключу с правом записи.
        """
        куски, параметры = [], []
        if state:
            куски.append("q.state=?")
            параметры.append(state)
        разрез, свои = self._owner_clause(owner, "j")
        if разрез:
            куски.append(разрез)
            параметры.extend(свои)
        условие = (" WHERE " + " AND ".join(куски)) if куски else ""
        соединение = ("FROM llm_queue q LEFT JOIN jobs j ON j.id=q.job_id"
                      if not разрез else
                      "FROM llm_queue q JOIN jobs j ON j.id=q.job_id")
        всего = self.query_one(
            f"SELECT COUNT(*) AS n {соединение}{условие}", параметры)
        rows = self.query(
            "SELECT q.*, j.filename, j.media_duration_s, j.model, j.created_at AS job_at "
            f"{соединение}"
            f"{условие} ORDER BY "
            "CASE q.state WHEN 'идёт' THEN 0 WHEN 'ждёт' THEN 1 ELSE 2 END, "
            "q.priority DESC, COALESCE(q.finished_at, q.enqueued_at) DESC "
            "LIMIT ? OFFSET ?", [*параметры, max(1, int(limit)), max(0, int(offset))])
        return {"total": int(всего["n"]) if всего else 0,
                "items": [dict(r) for r in rows]}

    def llmq_stats(self, since: float, *,
                   owner: str | list[str] | None = None) -> dict[str, Any]:
        """Сводка по завершённым запросам за окно: сколько, как долго, как часто ошибались."""
        откуда, своё, своё_args = self._llmq_откуда(owner)
        строка = self.query_one(
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS ok, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS failed, "
            "       AVG(q.latency_ms) AS avg_ms, MAX(q.latency_ms) AS max_ms, "
            "       SUM(q.calls) AS calls, "
            "       AVG(CASE WHEN q.started_at IS NOT NULL AND q.enqueued_at IS NOT NULL "
            "                THEN q.started_at-q.enqueued_at END) AS wait_s "
            f"{откуда} WHERE q.finished_at>=? AND {своё}",
            (self.LLMQ_ГОТОВО, self.LLMQ_ОШИБКА, float(since), *своё_args))
        свод = dict(строка) if строка else {}
        свод["since"] = float(since)
        return свод

    def llmq_series(self, since: float, until: float, buckets: int, *,
                    owner: str | list[str] | None = None) -> tuple[int, list[dict[str, Any]]]:
        """Ряд «сколько разобрано и за сколько» — для графика раздела."""
        buckets = max(1, min(int(buckets), SERIES_MAX_BUCKETS))
        шаг = max(1e-6, (float(until) - float(since)) / buckets)
        откуда, своё, своё_args = self._llmq_откуда(owner)
        rows = self.query(
            "SELECT CAST((q.finished_at-?)/? AS INTEGER) AS bucket, "
            "       COUNT(*) AS n, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS failed, "
            "       AVG(q.latency_ms) AS avg_ms, "
            "       AVG(CASE WHEN q.started_at IS NOT NULL AND q.enqueued_at IS NOT NULL "
            "                THEN q.started_at-q.enqueued_at END) AS wait_s "
            f"{откуда} WHERE q.finished_at>=? AND q.finished_at<? AND {своё} "
            "GROUP BY bucket ORDER BY bucket",
            (since, шаг, self.LLMQ_ОШИБКА, since, until, *своё_args))
        return buckets, [dict(r) for r in rows]

    # --- контроль качества работы оператора -----------------------------

    def qa_assign(self, job_id: str, *, assigned_to: str = "",
                  assigned_by: str = "", due_at: float | None = None,
                  agent: str = "", auto_score: float | None = None,
                  reason: str = "") -> int | None:
        """Ставит запись на проверку человеком. None — уже стоит.

        Повторное назначение той же записи не заводит вторую строку: две
        проверки одного разговора дают два балла, и дальше начинается спор о
        том, какой из них настоящий.

        Проверка «уже стоит» — в том же выражении, что и вставка, внутри
        одной транзакции записи. Раньше она шла отдельным чтением до неё, и
        два сервера над общей базой (или часовой набор и кнопка в одну
        секунду) заводили по проверке каждый.
        """
        with self.write() as conn:
            курсор = conn.execute(
                "INSERT INTO qa_reviews (job_id, agent, assigned_to, assigned_by, "
                "assigned_at, due_at, status, auto_score, reason) "
                "SELECT ?,?,?,?,?,?,'pending',?,? "
                "WHERE EXISTS (SELECT 1 FROM jobs WHERE id=?) "
                "AND NOT EXISTS (SELECT 1 FROM qa_reviews "
                "                WHERE job_id=? AND status='pending')",
                (str(job_id), str(agent or ""), str(assigned_to or ""),
                 str(assigned_by or ""), now(), due_at,
                 auto_score, str(reason or ""), str(job_id), str(job_id)))
            return int(курсор.lastrowid) if курсор.rowcount else None

    def qa_submit(self, review_id: int, *, reviewer: str, score: float | None,
                  agree: bool | None = None, items: Any = None,
                  comment: str = "") -> bool:
        """Записывает исход проверки. False — такой проверки нет или она закрыта.

        Чтение и запись — одной транзакцией, а запись — только пока проверка
        открыта. Два проверяющих, отправивших оценку в одну секунду, прежде
        оба получали «принято», и вторая оценка молча затирала первую.
        """
        with self.write() as conn:
            строка = conn.execute(
                "SELECT auto_score FROM qa_reviews WHERE id=? AND status='pending'",
                (int(review_id),)).fetchone()
            if строка is None:
                return False
            авто = строка["auto_score"]
            # Согласие считается само, когда о нём не сказали: расхождение
            # больше десяти баллов из ста — это уже другая оценка, а не
            # округление. Спрашивать об этом человека отдельно значит
            # получать пустое поле в половине проверок.
            своё = agree
            if своё is None and авто is not None and score is not None:
                своё = abs(float(авто) - float(score)) <= 10.0
            курсор = conn.execute(
                "UPDATE qa_reviews SET status='done', reviewer=?, reviewed_at=?, "
                "score=?, agree=?, items=?, comment=? WHERE id=? AND status='pending'",
                (str(reviewer or ""), now(),
                 None if score is None else float(score),
                 None if своё is None else int(bool(своё)),
                 json.dumps(items, ensure_ascii=False) if items is not None else None,
                 str(comment or ""), int(review_id)))
            return bool(курсор.rowcount)

    def qa_get(self, review_id: int) -> dict[str, Any] | None:
        """Одна проверка вместе с владельцем записи — для проверки прав."""
        строка = self.query_one(
            "SELECT q.*, j.owner AS job_owner FROM qa_reviews q "
            "LEFT JOIN jobs j ON j.id = q.job_id WHERE q.id=?", (int(review_id),))
        return dict(строка) if строка else None

    def qa_list(self, *, status: str = "", assigned_to: str = "",
                agent: str = "", limit: int = 100,
                owner: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Проверки с отбором; свежие первыми.

        `owner` — разрез по владельцу записи, как у соседних отчётов: без
        него ключ «только чтение» видел проверки всех — с именем файла, а в
        колл-центре это номер клиента, — и с оператором.
        """
        условия: list[str] = []
        args: list[Any] = []
        for поле, значение in (("status", status), ("assigned_to", assigned_to),
                               ("agent", agent)):
            if значение:
                условия.append(f"q.{поле}=?")
                args.append(str(значение))
        своё, своё_args = self._owner_clause(owner, "j")
        if своё:
            условия.append(своё)
            args.extend(своё_args)
        где = f" WHERE {' AND '.join(условия)}" if условия else ""
        строки = self.query(
            "SELECT q.*, j.filename AS filename, j.media_duration_s AS duration_s "
            f"FROM qa_reviews q LEFT JOIN jobs j ON j.id = q.job_id{где} "
            "ORDER BY q.assigned_at DESC, q.id DESC LIMIT ?",
            [*args, max(1, min(1000, int(limit)))])
        готово: list[dict[str, Any]] = []
        for строка in строки:
            запись = dict(строка)
            if запись.get("items"):
                try:
                    запись["items"] = json.loads(запись["items"])
                except (TypeError, ValueError):
                    запись["items"] = None
            готово.append(запись)
        return готово

    def qa_stats(self, *, since: float = 0.0,
                 owner: str | list[str] | None = None) -> dict[str, Any]:
        """Калибровка проверяющих: расходятся ли они с автоматом и между собой.

        Главное число здесь — не средний балл, а СДВИГ: насколько
        проверяющий систематически строже или мягче автомата. Средний балл
        говорит о записях, сдвиг — о самом проверяющем, и именно он отвечает
        на вопрос «можно ли сравнивать оценки двух руководителей».

        Доля согласия считается по проверкам, где согласие вообще известно:
        проверка без балла согласия не выражает, и деление на неё занижало
        долю.
        """
        своё, своё_args = self._owner_clause(owner, "j")
        откуда = "FROM qa_reviews q LEFT JOIN jobs j ON j.id = q.job_id"
        условие = "q.status='done'" + (" AND q.reviewed_at>=?" if since else "")
        args: list[Any] = [float(since)] if since else []
        if своё:
            условие += f" AND {своё}"
            args.extend(своё_args)
        общее = self.query_one(
            "SELECT COUNT(*) AS n,"
            "       SUM(CASE WHEN q.agree=1 THEN 1 ELSE 0 END) AS agreed,"
            "       SUM(CASE WHEN q.agree IS NOT NULL THEN 1 ELSE 0 END) AS rated,"
            "       AVG(q.score) AS avg_score,"
            "       AVG(q.score - q.auto_score) AS bias,"
            "       AVG(ABS(q.score - q.auto_score)) AS spread "
            f"{откуда} WHERE {условие}", args)
        по_людям = self.query(
            "SELECT q.reviewer AS reviewer, COUNT(*) AS n, AVG(q.score) AS avg_score,"
            "       AVG(q.score - q.auto_score) AS bias,"
            "       AVG(ABS(q.score - q.auto_score)) AS spread,"
            "       SUM(CASE WHEN q.agree=1 THEN 1 ELSE 0 END) AS agreed,"
            "       SUM(CASE WHEN q.agree IS NOT NULL THEN 1 ELSE 0 END) AS rated "
            f"{откуда} WHERE {условие} AND COALESCE(q.reviewer,'')<>'' "
            "GROUP BY q.reviewer ORDER BY n DESC LIMIT 50", args)
        всего = int((общее["n"] if общее else 0) or 0)
        оценено = int((общее["rated"] if общее else 0) or 0)
        ждут_условие = "q.status='pending'" + (f" AND {своё}" if своё else "")
        return {
            "done": всего,
            "pending": int((self.query_one(
                f"SELECT COUNT(*) AS n {откуда} WHERE {ждут_условие}", своё_args
            ) or {"n": 0})["n"] or 0),
            "agree_share": (round(float(общее["agreed"] or 0) / оценено, 3)
                            if оценено else None),
            "avg_score": (round(float(общее["avg_score"]), 1)
                          if общее and общее["avg_score"] is not None else None),
            "bias": (round(float(общее["bias"]), 1)
                     if общее and общее["bias"] is not None else None),
            "spread": (round(float(общее["spread"]), 1)
                       if общее and общее["spread"] is not None else None),
            "reviewers": [
                {"reviewer": str(с["reviewer"]), "n": int(с["n"] or 0),
                 "avg_score": (round(float(с["avg_score"]), 1)
                               if с["avg_score"] is not None else None),
                 "bias": round(float(с["bias"]), 1) if с["bias"] is not None else None,
                 "spread": (round(float(с["spread"]), 1)
                            if с["spread"] is not None else None),
                 "agree_share": (round(float(с["agreed"] or 0) / int(с["rated"]), 3)
                                 if с["rated"] else None)}
                for с in по_людям],
        }

    def qa_candidates(self, *, since: float = 0.0, limit: int = 50,
                      worst: bool = False,
                      owner: str | list[str] | None = None) -> list[dict[str, Any]]:
        """Записи, которые можно поставить на проверку.

        `worst=True` — самые слабые по баллу автомата; иначе случайные. Оба
        набора нужны вместе, и это не прихоть: только случайные дают честную
        картину по всем операторам, только слабые — быстро находят, кого
        учить. Один без другого превращает контроль качества либо в лотерею,
        либо в травлю одних и тех же людей.
        """
        условие, args = self._content_where(since or None, None, owner)
        порядок = ("c.agent_score ASC" if worst
                   else "RANDOM()")
        строки = self.query(
            "SELECT j.id AS job_id, j.filename AS filename, "
            "       j.media_duration_s AS duration_s, c.agent_score AS auto_score, "
            "       c.agent_speaker AS agent_speaker, "
            "       (SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id) AS agent "
            "FROM jobs j JOIN content c ON c.job_id = j.id "
            f"WHERE {условие} AND c.agent_score IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM qa_reviews q WHERE q.job_id = j.id) "
            f"ORDER BY {порядок} LIMIT ?",
            [*args, max(1, int(limit))])
        return [dict(с) for с in строки]

    # --- журнал доступа -------------------------------------------------

    def audit_add(self, *, action: str, actor: str = "", actor_id: str = "",
                  kind: str = "", role: str = "", method: str = "",
                  path: str = "", status: int = 0, ip: str = "") -> None:
        """Заносит действие в журнал доступа.

        Ошибка записи здесь не должна ронять сам запрос: журнал — это
        свидетельство, а не часть работы. Сервер, вставший из-за того, что не
        смог записать «посмотрел список записей», хуже отсутствующего
        журнала.
        """
        try:
            self.execute(
                "INSERT INTO audit (ts, actor, actor_id, kind, role, action, "
                "method, path, status, ip) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (now(), str(actor or "")[:128], str(actor_id or "")[:64],
                 str(kind or "")[:16], str(role or "")[:32],
                 str(action or "")[:200], str(method or "")[:8],
                 str(path or "")[:500], int(status or 0), str(ip or "")[:64]))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Журнал доступа не записан: %s", exc)

    def audit_list(self, *, limit: int = 200, offset: int = 0,
                   since: float = 0.0, until: float = 0.0,
                   actor: str = "", query: str = "",
                   failed_only: bool = False) -> dict[str, Any]:
        """Строки журнала доступа с отбором и общим числом.

        Общее число нужно вместе со страницей: раздел показывает «показаны
        200 из 14 512», и без второго числа человек не знает, видит он весь
        журнал или его начало.
        """
        условия: list[str] = []
        args: list[Any] = []
        if since:
            условия.append("ts>=?")
            args.append(float(since))
        if until:
            условия.append("ts<?")
            args.append(float(until))
        if actor:
            условия.append("actor=?")
            args.append(str(actor))
        if query:
            # «_» и «%» в запросе — буквы, а не образцы LIKE.
            условия.append("(action LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\' "
                           "OR ip LIKE ? ESCAPE '\\')")
            args.extend([f"%{_экранировать_like(str(query))}%"] * 3)
        if failed_only:
            # Отказы — то, ради чего журнал открывают чаще всего: чужой
            # подбор пароля и попытки дотянуться туда, куда не положено.
            условия.append("status>=400")
        где = f" WHERE {' AND '.join(условия)}" if условия else ""
        всего = self.query_one(f"SELECT COUNT(*) AS n FROM audit{где}", args)
        строки = self.query(
            f"SELECT * FROM audit{где} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            [*args, max(1, min(2000, int(limit))), max(0, int(offset))])
        return {"total": int((всего["n"] if всего else 0) or 0),
                "items": [dict(с) for с in строки]}

    def audit_actors(self, limit: int = 50) -> list[dict[str, Any]]:
        """Кто вообще есть в журнале — для выпадающего списка отбора."""
        строки = self.query(
            "SELECT actor, COUNT(*) AS n, MAX(ts) AS last FROM audit "
            "WHERE COALESCE(actor,'')<>'' GROUP BY actor "
            "ORDER BY n DESC LIMIT ?", (max(1, int(limit)),))
        return [dict(с) for с in строки]

    # --- обслуживание ---------------------------------------------------

    def cleanup(self, *, results_days: int = 30, metrics_days: int = 180,
                events_days: int = 90, audit_days: int = 365) -> dict[str, int]:
        removed = {"jobs": 0, "metrics": 0, "events": 0, "samples": 0,
                   "gpu_samples": 0, "llm_cache": 0, "calls": 0,
                   "audit": 0, "orphans": 0, "bytes": 0}
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
                строка = dict(row)
                строка["_file_shared"] = self.file_used_elsewhere(
                    str(row["file_path"] or ""), row["id"])
                removed["bytes"] = removed.get("bytes", 0) + _remove_job_files(
                    строка, self.path.parent)
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
        # Журнал доступа живёт своим сроком, и по умолчанию он гораздо
        # длиннее остальных: проверка, ради которой журнал и заводят,
        # приходит через полгода после события, а не через неделю. Ноль —
        # «хранить вечно», и это законный выбор, а не «не задано».
        if audit_days > 0:
            removed["audit"] = self.execute(
                "DELETE FROM audit WHERE ts<?", (ts - audit_days * 86400,))
        # Кеш ответов языковой модели живёт по сроку хранения результатов, а
        # не вечно. Дело не только в размере (несколько килобайт на вызов,
        # до четырёх вызовов на запись — это гигабайты в год): ключ кеша —
        # отпечаток подсказки, а в подсказке лежит расшифровка разговора.
        # Без этой уборки пересказ переживал удаление самой записи по сроку
        # хранения, то есть данные, которые считались удалёнными, оставались
        # в базе.
        if results_days > 0:
            removed["llm_cache"] = self.execute(
                "DELETE FROM llm_cache WHERE created_at<?",
                (ts - results_days * 86400,))
        # Строки без задания убирает `sweep_orphans` — раз в сутки, а не
        # здесь: на двадцати тысячах заданий подметание девяти таблиц
        # занимало секунды под блокировкой записи каждый час, а сироты
        # сегодня появляются только из баз прежних версий.

        # Журнал звонков — по тому же сроку и по той же причине. Запись
        # звонка занимает сотни байт, но это номер клиента и номер
        # оператора: пережить удаление самого разговора они не должны. К
        # тому же за год их набегает миллион, и обход каталога записей
        # начинает читать это множество целиком.
        if results_days > 0:
            removed["calls"] = self.forget_calls(
                before=ts - results_days * 86400)
        return removed

    def sweep_orphans(self) -> dict[str, Any]:
        """Строки без задания и следы удалённых разговоров. Раз в сутки.

        Сегодня сирот появляться не должно — запись разбора идёт условием
        «если задание есть», а удаление задания чистит всё за собой, — но на
        базе, пережившей прежние версии, они уже лежат, и убрать их больше
        некому: и уборка по сроку, и удаление по требованию ходят по таблице
        заданий. Здесь же то, что удобнее делать одним заходом на все
        удалённые записи разом:

        * звонки удалённых записей теряют номера и имя (ключ остаётся);
        * основы, которых не осталось ни в одной записи, уходят из словаря
          форм;
        * поисковый указатель физически забывает удалённые реплики.
        """
        итог: dict[str, Any] = {"rows": 0, "calls": 0, "vocab": 0, "fts": False}
        self._поколение_реплик += 1
        for таблица in ("content", "content_terms", "content_hits",
                        "content_marks", "review_queue", "llm_results",
                        "llm_queue", "segments", "events", "qa_reviews",
                        "llm_cache"):
            итог["rows"] += self.execute(
                f"DELETE FROM {таблица} WHERE job_id IS NOT NULL "
                "AND job_id NOT IN (SELECT id FROM jobs)")
        итог["rows"] += self.execute(
            "DELETE FROM model_checks WHERE job_id NOT IN (SELECT id FROM jobs)")
        итог["calls"] = self.execute(
            f"UPDATE calls SET {ОБЕЗЛИЧИТЬ_ЗВОНОК} WHERE job_id IS NOT NULL "
            "AND job_id NOT IN (SELECT id FROM jobs) AND (" + " OR ".join(
                f"COALESCE({поле},'')<>''" for поле in ПОЛЯ_ЛИЧНЫЕ_ЗВОНКА) + ")")
        итог["vocab"] = self.forget_unused_forms()
        if self.get_kv(КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ):
            итог["fts"] = self.fts_purge()
            if итог["fts"]:
                self.execute("DELETE FROM kv WHERE key=?", (КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ,))
        try:
            self.instances_prune()
        except StorageError as exc:
            log.debug("Старые отметки экземпляров не убраны: %s", exc)
        self.checkpoint()
        if итог["rows"] or итог["calls"] or итог["vocab"]:
            log.info("Суточная уборка базы: строк без задания %d, звонков обезличено %d, "
                     "форм слов %d, указатель %s", итог["rows"], итог["calls"],
                     итог["vocab"], "вычищен" if итог["fts"] else "не трогали")
        return итог

    def forget_unused_forms(self) -> int:
        """Убирает из словаря форм основы, которых нет ни в одной записи."""
        return self.execute(
            "DELETE FROM content_vocab WHERE NOT EXISTS ("
            "  SELECT 1 FROM content_terms t WHERE t.stem = content_vocab.stem)")

    def fts_purge(self, *, шаг: int = 2000, предел_с: float = 600.0) -> bool:
        """Физически вычищает из указателя удалённые реплики.

        FTS5 при удалении не стирает слова, а дописывает отметку «удалено»;
        сами слова лежат в `segments_fts_data`, пока сегмент не сольют с
        другими, — а это может не случиться никогда. После удаления по
        требованию субъекта номер телефона, произнесённый в разговоре,
        оставался в файле базы и уезжал в резервные копии.

        Слияние идёт порциями (`merge` с отрицательным шагом — «слить всё в
        один сегмент, но не больше N страниц за раз»), каждая — своей
        короткой транзакцией: одно `optimize` на большом архиве держало бы
        блокировку записи минутами. Порция, не изменившая почти ничего
        (меньше двух строк), значит, что сливать больше нечего.

        Возвращает True, если указатель дочищен до конца.
        """
        if not self.fts_ready:
            return False
        начало = time.monotonic()
        try:
            while time.monotonic() - начало < предел_с:
                with self.write() as conn:
                    до = conn.total_changes
                    conn.execute("INSERT INTO segments_fts(segments_fts, rank) "
                                 "VALUES('merge', ?)", (-abs(int(шаг)),))
                    сделано = conn.total_changes - до
                if сделано < 2:
                    return True
        except StorageError as exc:
            log.warning("Поисковый указатель не дочищен: %s", exc)
            return False
        log.info("Поисковый указатель дочищается дольше %.0f с — продолжим завтра", предел_с)
        return False

    def checkpoint(self) -> dict[str, int] | None:
        """Вливает журнал WAL в базу и усекает его файл до нуля.

        Нужна после больших перестроек (VACUUM, слияние указателя): сама
        собой контрольная точка отложена, пока открыт хоть один читатель, и
        всё это время рядом с базой лежит журнал размером с перестроенное.
        """
        if self.journal_mode != "wal":
            return None
        try:
            with self._write_lock:
                строка = self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error as exc:
            log.debug("Контрольная точка не прошла: %s", exc)
            return None
        return {"busy": int(строка[0]), "log": int(строка[1]),
                "checkpointed": int(строка[2])} if строка else None

    def vacuum(self) -> dict[str, Any]:
        """Сжатие файла базы — под общим замком записи. Возвращает, как прошло.

        VACUUM перестраивает базу целиком и держит исключительную
        блокировку SQLite минутами на большом архиве. Без общего замка
        пишущий поток входил в `BEGIN IMMEDIATE`, упирался в неё, ждал
        `busy_timeout` и получал «database is locked» — а это воркер,
        записывающий готовую расшифровку: текст был, и текст пропадал.

        Три условия, без которых «очистка» делала хуже:

        * **Соседи.** Замок записи — внутри процесса. Второй сервер над той
          же базой ждал свои тридцать секунд и получал ту же «database is
          locked». Если соседи живы, сжатие пропускается с объяснением.
        * **Место.** Копия базы строится рядом, а переписанное идёт через
          журнал: на время нужно до двух размеров базы. Самопроверка
          советует очистку именно тогда, когда диск почти полон, и VACUUM
          на полном диске падал, успев занять остаток места.
        * **Память.** При `temp_store=MEMORY` копия строилась в памяти
          процесса, где лежат модели: база в 534 МБ давала пик в 622 МБ
          против 82 МБ с временным файлом. После сжатия журнал усекается —
          иначе рядом оставался WAL размером с базу, и на диске было занято
          больше, чем до «очистки».
        """
        соседи = self.other_instances()
        if соседи:
            причина = ("над базой работают другие серверы ("
                       + ", ".join(с["instance"] for с in соседи[:3])
                       + "): сжатие заперло бы их запись — запустите его, "
                         "когда сервер над базой один")
            log.info("VACUUM пропущен: %s", причина)
            return {"done": False, "reason": причина}
        def размер_файлов() -> int:
            всего = 0
            for хвост in ("", "-wal"):
                with contextlib.suppress(OSError):
                    всего += Path(str(self.path) + хвост).stat().st_size
            return всего
        до = размер_файлов()
        try:
            свободно = shutil.disk_usage(self.path.parent).free
        except OSError:
            свободно = -1
        нужно = 2 * до + ПРЕДЕЛ_ЖУРНАЛА
        if 0 <= свободно < нужно:
            причина = (f"не хватит места: на время сжатия нужно около "
                       f"{нужно / 1048576:.0f} МБ, свободно {свободно / 1048576:.0f} МБ")
            log.warning("VACUUM пропущен: %s", причина)
            return {"done": False, "reason": причина, "free_bytes": свободно,
                    "need_bytes": нужно}
        начало = time.monotonic()
        with self._write_lock:
            conn = self.conn
            try:
                conn.execute("PRAGMA temp_store=FILE")
                conn.execute("VACUUM")
            except sqlite3.Error as exc:
                log.warning("VACUUM не выполнен: %s", exc)
                return {"done": False, "reason": str(exc)}
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA temp_store=MEMORY")
        self.checkpoint()
        после = размер_файлов()
        return {"done": True, "before_bytes": до, "after_bytes": после,
                "seconds": round(time.monotonic() - начало, 2)}

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
    # Отложенный на время повтора прежний результат — туда же.
    if directory is not None:
        shutil.rmtree(directory.with_name(directory.name + ".prev"), ignore_errors=True)
    path = _within(base, str(job.get("file_path") or ""))
    if path is not None and job.get("_file_shared"):
        path = None
    if path is not None:
        # Копия с заглушёнными персональными данными лежит рядом с записью и
        # удалялась только вместе с каталогом: удаление задания, уборка по
        # сроку и удаление по требованию оставляли голос клиента на диске.
        for файл in (path, путь_отредактированной(path)):
            try:
                if файл.is_file():
                    freed += файл.stat().st_size
                    файл.unlink()
            except OSError as exc:
                log.warning("Не удалось удалить %s: %s", файл, exc)
    return freed


def путь_отредактированной(путь: Path) -> Path:
    """Где лежит копия записи с заглушёнными персональными данными.

    Отдельным именем рядом с исходником, а не поверх него: редакция — это
    производная, и переписать ею запись значило бы уничтожить исходные
    данные по нажатию кнопки.
    """
    return путь.with_name(f"{путь.stem}.redacted{путь.suffix}")


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


def _экранировать_like(значение: str) -> str:
    """Обезвреживает образцы LIKE: обратная косая, процент, подчёркивание."""
    return (str(значение or "").replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


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
    # Секреты сервера в параметрах задания не отдаются никому и нигде —
    # даже если строка создана до того, как их перестали туда класть.
    if isinstance(job.get("params"), dict):
        job["params"] = Settings.для_задания(job["params"])
    if isinstance(job.get("quality_flags"), str):
        job["quality_flags"] = [ф for ф in job["quality_flags"].split(",") if ф]
    return job
