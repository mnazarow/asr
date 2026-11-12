"""Обслуживание базы: уборка по срокам хранения, сироты на диске,
вычистка указателя полнотекстового поиска, оптимизация, снимок, контрольная
точка и VACUUM.

Вынесено из `db.py` примесью к `Database`. Удаление файлов задания
(`_remove_job_files`) и путь отредактированной копии — здесь же; `db.py`
по-прежнему отдаёт их имена.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db_util import (
    CLEANUP_BATCH,
    КЛЮЧ_УКАЗАТЕЛЬ_ГРЯЗНЫЙ,
    ОБЕЗЛИЧИТЬ_ЗВОНОК,
    ПОЛЯ_ЛИЧНЫЕ_ЗВОНКА,
    ПРЕДЕЛ_ЖУРНАЛА,
    ОсноваБазы,
    now,
)
from .errors import StorageError
from .logging_setup import get_logger

log = get_logger("db")


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


class ОбслуживаниеБазы(ОсноваБазы):
    """Примесь к `Database`: уборка и обслуживание файла базы."""

    def cleanup(self, *, results_days: int = 30, metrics_days: int = 180,
                events_days: int = 90, audit_days: int = 365,
                calls_file_days: int = 0) -> dict[str, int]:
        """Уборка по срокам хранения (`calls_file_days` — см. `forget_calls`)."""
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
            # Исключения (эталон, открытая проверка качества) — ниже.
            stale = self.query(
                "SELECT id, result_path, file_path FROM jobs "
                "WHERE ((finished_at IS NOT NULL AND finished_at<?) "
                "   OR (finished_at IS NULL AND created_at<?)) "
                "  AND id NOT IN (SELECT job_id FROM content_marks "
                "                 WHERE kind='reference' AND status='yes') "
                "  AND id NOT IN (SELECT job_id FROM qa_reviews WHERE status='pending' "
                "                 AND assigned_at>=?) "
                "ORDER BY COALESCE(finished_at, created_at) LIMIT ?",
                (cutoff, ts - results_days * 3 * 86400, ts - results_days * 3 * 86400,
                 CLEANUP_BATCH))
            # Исключения — то, что человек оставил намеренно: запись,
            # отмеченная эталоном, и запись с неоконченной проверкой качества
            # работы оператора. По общему сроку они удалялись наравне со
            # всеми: эталоны раздела «Лучшие разговоры» пропадали через
            # месяц, а проверяющий открывал назначенную ему проверку и видел
            # пустоту. Брошенная проверка держит запись не дольше втрое
            # большего срока — как и задание, застрявшее в очереди. Удаление
            # по требованию удаляет и их.
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
                before=ts - results_days * 86400,
                file_keys_before=ts - max(results_days, calls_file_days) * 86400)
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
                        "llm_cache", "llm_golden", "llm_vectors"):
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

    def optimize(self, *, analysis_limit: int = 400) -> bool:
        """Суточное обновление статистики планировщика — `PRAGMA optimize`.

        Не полный `ANALYZE`: проверено запуском, что на перекошенных данных
        он ухудшил один запрос (`crm_waiting`: 0 мс → 340 мс, полный проход
        вместо `idx_jobs_crm`). `optimize` пересобирает статистику только
        таблиц, где она нужна, а `analysis_limit` ограничивает выборку
        строк — заход идёт доли секунды и на большом архиве.
        """
        try:
            with self.write() as conn:
                conn.execute(f"PRAGMA analysis_limit={max(100, int(analysis_limit))}")
                conn.execute("PRAGMA optimize")
            return True
        except StorageError as exc:
            log.warning("PRAGMA optimize не выполнен: %s", exc)
            return False

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

    #: Шаг снимка в режиме журнала отката, страниц (по 4 КБ — 8 МБ за шаг).
    ШАГ_СНИМКА_СТРАНИЦ = 2048
    #: Пауза между шагами снимка, секунд: окно для писателей других серверов.
    ПАУЗА_СНИМКА_С = 0.05
    #: Сколько раз снимок по шагам может начаться заново из-за чужой записи,
    #: прежде чем доделать его одним шагом.
    ПЕРЕЗАПУСКОВ_СНИМКА = 3

    def snapshot(self, приёмник: sqlite3.Connection) -> None:
        """Согласованный снимок базы в `приёмник` («.backup» SQLite).

        В WAL снимок идёт одним шагом: читатель писателям не мешает. В
        режиме журнала отката (`db_journal_mode: delete`, раскладка для
        нескольких машин над общим каталогом) снимок держит разделяемую
        блокировку, и писатели — воркер, дописывающий расшифровку, отметки
        жизни — через 30 с получали «database is locked»: копия базы в
        гигабайты идёт минуты, и ночная копия стоила результатов заданий.

        Поэтому в режиме отката снимок идёт под замком записи этого
        процесса: свои писатели ждут на замке, а не падают по тайм-ауту
        SQLite. Запись другим соединением начинает снимок заново, так что
        отпускать своих писателей между шагами бессмысленно — снимок под
        непрерывными отметками прогресса не кончился бы никогда. Писателям
        других серверов оставлено окно между шагами; если из-за их записи
        снимок начинался заново больше `ПЕРЕЗАПУСКОВ_СНИМКА` раз, он
        доделывается одним шагом.
        """
        источник = self.conn
        if self.journal_mode == "wal":
            источник.backup(приёмник)
            return

        class _Перезапуски(Exception):
            pass

        перезапусков = 0
        было: int | None = None

        def ход(статус: int, осталось: int, _всего: int) -> None:
            nonlocal перезапусков, было
            if было is not None and осталось > было:
                перезапусков += 1
                if перезапусков > self.ПЕРЕЗАПУСКОВ_СНИМКА:
                    raise _Перезапуски
            было = осталось
            # Пауза — только после сделанного шага: на занятой базе SQLite
            # и так ждёт сам (параметр `sleep`).
            if осталось and статус == 0:              # SQLITE_OK
                time.sleep(self.ПАУЗА_СНИМКА_С)

        with self._write_lock:
            try:
                источник.backup(приёмник, pages=self.ШАГ_СНИМКА_СТРАНИЦ, progress=ход,
                                sleep=self.ПАУЗА_СНИМКА_С)
                return
            except _Перезапуски:
                log.info("Снимок базы начинался заново %d раз из-за записи других "
                         "серверов — доделываю одним шагом", перезапусков)
            источник.backup(приёмник, sleep=self.ПАУЗА_СНИМКА_С)

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
