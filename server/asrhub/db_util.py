"""Мелочи, общие для частей базы (`db.py` и её примесей): постоянные
указателей, время, экранирование и объявления основы `Database` для
проверки типов в примесях."""
from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Sequence
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import threading
    from pathlib import Path

#: Сколько заданий убирать по сроку хранения за один заход служебного цикла.
CLEANUP_BATCH = 5000

#: Предел, до которого усекается журнал после контрольной точки, байт.
#: Без него файл `-wal` сохраняет размер самого большого пика навсегда:
#: после VACUUM на базе в 260 МБ рядом оставался журнал в 174 МБ.
ПРЕДЕЛ_ЖУРНАЛА = 64 * 1024 * 1024

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

#: Узкий указатель по ответам языковой модели: всё, чем ответы считают и
#: отбирают (сбой, причина, исход, «решён ли вопрос», версия подсказок,
#: время ответа), — рядом с номером записи и без самого ответа.
#:
#: Строка ответа широкая: пересказ, цитаты, JSON трекеров и скоркарты — три-
#: четыре килобайта. Чтобы узнать причину обращения, базе приходилось
#: поднимать её всю, и на четырёхстах тысячах разборов «сколько звонков про
#: возврат за год» означало прочитать два гигабайта: свод раздела собирался
#: полминуты, а список под числом не укладывался в срок. По этому указателю
#: тот же вопрос — проход по тридцати мегабайтам.
#:
#: Колонки перечислены здесь же: список раскрытия (`drill`) просит базу идти
#: по указателю только тогда, когда запросу хватает именно их.
УКАЗАТЕЛЬ_ОТВЕТОВ = "idx_llm_results_brief"

КОЛОНКИ_УКАЗАТЕЛЯ_ОТВЕТОВ = ("job_id", "error", "reason", "outcome", "resolved",
                             "version", "latency_ms")

#: Окно заданий по времени — вместе с тем, чем окно обычно сужают: статус,
#: источник, владелец. Строка задания тоже широкая (в ней расшифровка), и
#: без этих колонок в указателе «готовые за год, без контрольных прогонов»
#: читали каждое задание года с диска ради трёх коротких полей.
УКАЗАТЕЛЬ_ОКНА = "idx_jobs_window"

#: Потолок числа корзин у рядов нагрузки. График шире тысячи точек всё равно
#: не читается, а число корзин задаёт стоимость запроса.
SERIES_MAX_BUCKETS = 2000



def now() -> float:
    return time.time()


def new_id(prefix: str = "job") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _экранировать_like(значение: str) -> str:
    """Обезвреживает образцы LIKE: обратная косая, процент, подчёркивание."""
    return (str(значение or "").replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_"))


class ОсноваБазы:
    """Основа `Database` (соединения, запросы, запись, отбор по владельцу) —
    объявления для проверки типов в примесях. Настоящее — в `db.py`."""

    if TYPE_CHECKING:
        @property
        def conn(self) -> sqlite3.Connection: ...
        def write(self) -> AbstractContextManager[sqlite3.Connection]: ...
        def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]: ...
        def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None: ...
        def query_scalar(self, sql: str, params: Sequence[Any] = (),
                         default: Any = 0) -> Any: ...
        def execute(self, sql: str, params: Sequence[Any] = ()) -> int: ...
        def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int: ...
        def has_index(self, name: str) -> bool: ...

        @staticmethod
        def _owner_clause(owner: str | list[str] | None,
                          alias: str = "j") -> tuple[str, list[Any]]: ...

        @staticmethod
        def _цифры_номера(запрос: str) -> str: ...

        path: Path
        _write_lock: threading.RLock
        _поколение_реплик: int
        поколение_заданий: int
        fts_ready: bool
        journal_mode: str

        def _bucket_step(self, since: float, until: float, buckets: int) -> tuple[int, float]: ...
        def get_kv(self, key: str, default: Any = None) -> Any: ...
        def count_jobs(self, *, status: str | list[str] | None = None,
                       owner: str | list[str] | None = None,
                       model: str | None = None, search: str | None = None,
                       group_id: str | None = None,
                       content: str | None = None,
                       since: float | None = None,
                       until: float | None = None) -> int: ...
        def delete_job(self, job_id: str) -> None: ...
        def file_used_elsewhere(self, file_path: str, except_id: str) -> bool: ...
        def forget_calls(self, *, before: float,
                         file_keys_before: float | None = None) -> int: ...
        def other_instances(self, *, свежесть: float = ...) -> list[dict[str, Any]]: ...
        def instances_prune(self, *, старше: float = ...) -> int: ...

        def _content_where(self, since: float | None, until: float | None,
                           owner: str | list[str] | None, extra: str = "",
                           agent: tuple[str, str] | None = None
                           ) -> tuple[str, list[Any]]: ...

        CONTENT_GROUPS: dict[str, str]
        AGENT_DIMENSIONS: dict[str, str]
