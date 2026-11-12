"""Часть базы о языковой модели: ответы разбора, их свод и отбор, оценка по
ответам, эталоны и векторы, очередь запросов к модели.

Вынесено из `db.py` примесью к `Database`.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from typing import Any

from .db_util import (
    SERIES_MAX_BUCKETS,
    УКАЗАТЕЛЬ_ОКНА,
    УКАЗАТЕЛЬ_ОТВЕТОВ,
    ОсноваБазы,
    _экранировать_like,
    now,
)
from .instance import INSTANCE_ID, own_host, process_alive


class ЯзыковаяМодельБазы(ОсноваБазы):
    """Примесь к `Database`: всё о разборе языковой моделью."""

    # --- смысловой слой языковой модели ----------------------------------------

    def llm_save(self, job_id: str, version: int, **поля: Any) -> None:
        """Кладёт ответ модели по записи; словари и списки — строкой JSON."""
        строка = dict(поля)
        for к in ("actions", "trackers", "scorecard", "warnings", "fields", "critical"):
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
                    owner: str | list[str] | None = None,
                    min_duration_s: float = 0.0,
                    skip_suspect_share: float | None = None) -> list[dict[str, Any]]:
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
        # Пороги отбора для модели (`llm.policy`): короткие звонки и
        # сомнительные расшифровки архив не берёт — переписка порога
        # длительности не знает, длительности у неё нет.
        if min_duration_s and min_duration_s > 0:
            where.append("(COALESCE(j.source,'') = 'text' OR COALESCE(j.media_duration_s,0) <= 0 "
                         "OR j.media_duration_s >= ?)")
            args.append(float(min_duration_s))
        if skip_suspect_share is not None:
            where.append("NOT (COALESCE(j.suspect_share,0) >= ? OR "
                         "COALESCE(j.quality_flags,'') LIKE '%phrase%' OR "
                         "COALESCE(j.quality_flags,'') LIKE '%repeat%' OR "
                         "COALESCE(j.quality_flags,'') LIKE '%compression%')")
            args.append(float(skip_suspect_share) if skip_suspect_share > 0 else 2.0)
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

    @staticmethod
    def _окно_ответов(since: float, owner: str | list[str] | None, *,
                      until: float | None = None,
                      agent: str = "") -> tuple[str, list[Any]]:
        """Какие задания входят в свод ответов модели — одним местом.

        Готовые, не старше начала окна, без контрольных прогонов; с владельцем
        — только его. Все четыре условия стоят по колонкам указателя окна
        (`УКАЗАТЕЛЬ_ОКНА`), так что проверяются без чтения самих заданий.
        `until` — верхняя граница (свод за прошлый месяц, а не «до сейчас»),
        `agent` — только разговоры этого добавочного из журнала звонков.
        """
        where = ["j.status='completed'", "j.created_at >= ?",
                 "COALESCE(j.source,'') <> 'control'"]
        args: list[Any] = [since]
        if until is not None:
            where.append("j.created_at < ?")
            args.append(float(until))
        if isinstance(owner, (list, tuple)):
            where.append(f"j.owner IN ({','.join('?' for _ in owner)})")
            args.extend(owner)
        elif owner:
            where.append("j.owner=?")
            args.append(owner)
        if agent:
            where.append("EXISTS (SELECT 1 FROM calls cl WHERE cl.job_id = j.id "
                         "AND cl.agent = ?)")
            args.append(str(agent))
        return " AND ".join(where), args

    def _задания_окна(self) -> str:
        """`jobs j` — с просьбой идти по указателю окна, если он есть."""
        return ("jobs j INDEXED BY " + УКАЗАТЕЛЬ_ОКНА
                if self.has_index(УКАЗАТЕЛЬ_ОКНА) else "jobs j")

    def llm_stats(self, version: int, since: float, *,
                  owner: str | list[str] | None = None, until: float | None = None,
                  agent: str = "") -> dict[str, Any]:
        """Свод ответов модели за окно: покрытие, исходы, причины, действия,
        трекеры, задержка, ошибки.

        Отдаёт ответы целиком — с пересказом и JSON. Годится там, где окно
        короткое или нужен сам текст (сводка по расписанию); счёт за длинное
        окно — `llm_brief`, он ответов не читает.
        """
        условие, args = self._окно_ответов(since, owner, until=until, agent=agent)
        всего = int(self.query_scalar(f"SELECT COUNT(*) n FROM jobs j WHERE {условие}", args))
        rows = self.query(
            "SELECT l.job_id, l.version, l.summary, l.reason, l.reason_quote, l.outcome, "
            "       l.outcome_quote, l.resolved, l.actions, l.trackers, "
            "       l.scorecard, l.latency_ms, l.error, l.calls, l.chunks, l.warnings, j.filename, "
            "       j.created_at, j.owner "
            f"FROM llm_results l JOIN jobs j ON j.id = l.job_id WHERE {условие}", args)
        return {"total": всего, "rows": [_row_to_llm(r) for r in rows]}

    def llm_brief(self, version: int, since: float, *,
                  owner: str | list[str] | None = None, until: float | None = None,
                  agent: str = "") -> dict[str, Any]:
        """Счётная часть свода ответов модели — без чтения самих ответов.

        Сколько записей в окне, сколько из них с ответом, сколько сбоев, как
        ответы разложились по причинам и исходам, где модель сказала «решён»
        — всё это группы по трём коротким колонкам. Раньше ради них в память
        поднимались ответы целиком: пересказ, цитаты, JSON трекеров и
        скоркарты каждой записи окна, и на четырёхстах тысячах разборов
        раздел «Голосовая аналитика» открывался полминуты. Здесь база идёт
        по двум узким указателям — окно заданий и ответы — и ни одной
        широкой строки не поднимает: то же окно считается за доли секунды.

        Ответ — сырые группы; доли, сортировку и подписи собирает вызывающий.
        """
        условие, args = self._окно_ответов(since, owner, until=until, agent=agent)
        задания = self._задания_окна()
        ответы = ("llm_results l INDEXED BY " + УКАЗАТЕЛЬ_ОТВЕТОВ
                  if self.has_index(УКАЗАТЕЛЬ_ОТВЕТОВ) else "llm_results l")
        всего = int(self.query_scalar(
            f"SELECT COUNT(*) n FROM {задания} WHERE {условие}", args))
        группы = self.query(
            "SELECT COALESCE(l.error,'') = '' AS ok, COALESCE(l.reason,'') AS reason, "
            "       COALESCE(l.outcome,'') AS outcome, COUNT(*) AS n, "
            "       SUM(CASE WHEN l.version <> ? THEN 1 ELSE 0 END) AS stale, "
            "       SUM(COALESCE(l.latency_ms,0)) AS latency_ms, "
            "       SUM(CASE WHEN l.resolved IS NOT NULL THEN 1 ELSE 0 END) AS resolved_known, "
            "       SUM(CASE WHEN l.resolved THEN 1 ELSE 0 END) AS resolved_yes "
            f"FROM {задания} JOIN {ответы} ON l.job_id = j.id "
            f"WHERE {условие} GROUP BY 1, 2, 3", [int(version), *args])
        return {"total": всего, "groups": [dict(r) for r in группы]}

    def llm_by_agent(self, since: float, *,
                     owner: str | list[str] | None = None,
                     until: float | None = None) -> list[dict[str, Any]]:
        """Свод ответов модели по добавочным — группами, без чтения ответов.

        Добавочный — из журнала звонков, подзапросом по указателю
        `idx_calls_job_agent`: соединение размножило бы задание, у которого
        две строки звонка. Записи без ответа модели тоже здесь (`has` = 0):
        «разговоров у добавочного» — все его готовые записи окна, а не только
        разобранные. Без добавочного — группа с пустым `agent`.
        """
        условие, args = self._окно_ответов(since, owner, until=until)
        ответы = ("llm_results l INDEXED BY " + УКАЗАТЕЛЬ_ОТВЕТОВ
                  if self.has_index(УКАЗАТЕЛЬ_ОТВЕТОВ) else "llm_results l")
        группы = self.query(
            "SELECT COALESCE((SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id "
            "                 AND COALESCE(cl.agent,'') <> '' LIMIT 1), '') AS agent, "
            "       l.job_id IS NOT NULL AS has, COALESCE(l.error,'') = '' AS ok, "
            "       COALESCE(l.reason,'') AS reason, COALESCE(l.outcome,'') AS outcome, "
            "       COUNT(*) AS n, "
            "       SUM(CASE WHEN l.resolved IS NOT NULL THEN 1 ELSE 0 END) AS resolved_known, "
            "       SUM(CASE WHEN l.resolved THEN 1 ELSE 0 END) AS resolved_yes "
            f"FROM {self._задания_окна()} LEFT JOIN {ответы} ON l.job_id = j.id "
            f"WHERE {условие} GROUP BY 1, 2, 3, 4, 5", [*args])
        return [dict(r) for r in группы]

    def llm_recent(self, since: float, *, owner: str | list[str] | None = None,
                   limit: int = 300, until: float | None = None,
                   agent: str = "") -> list[dict[str, Any]]:
        """Последние ответы модели окна целиком — столько, сколько показывают.

        Построчный список раздела — это три сотни свежих разборов. Брать их
        сортировкой всех ответов окна значило читать всё окно; здесь база
        идёт по времени от новых и останавливается на нужной строке.
        """
        условие, args = self._окно_ответов(since, owner, until=until, agent=agent)
        rows = self.query(
            "SELECT l.job_id, l.version, l.summary, l.reason, l.reason_quote, l.outcome, "
            "       l.outcome_quote, l.resolved, l.actions, l.trackers, l.scorecard, "
            "       l.latency_ms, l.error, l.calls, l.chunks, l.warnings, j.filename, "
            "       j.created_at, j.owner "
            f"FROM {self._задания_окна()} JOIN llm_results l ON l.job_id = j.id "
            f"WHERE {условие} ORDER BY j.created_at DESC, j.id DESC LIMIT ?",
            [*args, max(1, int(limit))])
        return [_row_to_llm(r) for r in rows]

    def llm_details(self, since: float, *,
                    owner: str | list[str] | None = None, until: float | None = None,
                    agent: str = "") -> list[dict[str, Any]]:
        """Разборы окна без сбоя — только то, что лежит в них списками.

        Обязательства, трекеры, скоркарта и замечания хранятся в ответе
        строками JSON, и посчитать их можно, только прочитав каждый ответ:
        это и есть дорогая часть свода. Остального из ответа здесь не
        берётся — ни пересказа, ни цитат, — а задание читается по указателю
        окна. Строки идут от новых к старым.
        """
        условие, args = self._окно_ответов(since, owner, until=until, agent=agent)
        rows = self.query(
            "SELECT l.job_id, l.actions, l.trackers, l.scorecard, l.warnings, j.created_at "
            f"FROM {self._задания_окна()} JOIN llm_results l ON l.job_id = j.id "
            f"WHERE {условие} AND COALESCE(l.error,'') = '' "
            "ORDER BY j.created_at DESC, j.id DESC", args)
        # Разбор JSON — главная цена этого прохода, а строки повторяются:
        # пустой список обязательств и замечаний — почти у всех, трекеры, из
        # которых не сработал ни один, — одной и той же строкой у большинства
        # записей. Пустое не разбирается вовсе, повторы — один раз. Память
        # под повторы ограничена: скоркарта с цитатами у каждой записи своя.
        # Разобранное общее на все строки — вызывающий его только читает.
        память: dict[str, Any] = {}

        def разобрать(сырое: Any) -> Any:
            if not isinstance(сырое, str):
                return сырое
            if сырое == "[]":
                return []
            готовое = память.get(сырое, память)
            if готовое is память:
                try:
                    готовое = json.loads(сырое)
                except (TypeError, ValueError):
                    готовое = None
                if len(память) < 4096:
                    память[сырое] = готовое
            return готовое

        return [{"job_id": r["job_id"], "created_at": r["created_at"],
                 "actions": разобрать(r["actions"]), "trackers": разобрать(r["trackers"]),
                 "scorecard": разобрать(r["scorecard"]), "warnings": разобрать(r["warnings"])}
                for r in rows]

    def llm_latencies(self, since: float) -> list[float]:
        """Время ответа модели по разборам окна без сбоя, мс — по указателю."""
        условие, args = self._окно_ответов(since, None)
        ответы = ("llm_results l INDEXED BY " + УКАЗАТЕЛЬ_ОТВЕТОВ
                  if self.has_index(УКАЗАТЕЛЬ_ОТВЕТОВ) else "llm_results l")
        rows = self.query(
            f"SELECT l.latency_ms FROM {self._задания_окна()} JOIN {ответы} ON l.job_id = j.id "
            f"WHERE {условие} AND COALESCE(l.error,'') = '' AND l.latency_ms IS NOT NULL", args)
        return [float(r["latency_ms"]) for r in rows]

    # --- версия 36: оценка по ответам модели, эталоны, отметки, векторы ---

    def llm_window_rows(self, since: float, *, until: float | None = None,
                        owner: str | list[str] | None = None, agent: str = "",
                        by: str = "", key: str | None = None,
                        limit: int = 200_000) -> list[dict[str, Any]]:
        """Ответы модели окна без сбоя — с разговором, звонком и разбором.

        Для сводов поверх ответов модели: оценка оператора, контроль
        договорённостей, решение с первого обращения, драйверы оценки
        клиента, риск ухода. `by`/`key` — оператор в разрезе карточки
        (`AGENT_DIMENSIONS`); JSON-колонки разобраны.
        """
        условие, args = self._окно_ответов(since, owner, until=until, agent=agent)
        if by:
            выражение = self.AGENT_DIMENSIONS.get(by)
            if выражение is None:
                raise ValueError(f"неизвестный разрез оператора: {by}")
            if key is not None:
                условие += " AND " + выражение
                args.append(str(key))
        группа = self.CONTENT_GROUPS.get(by or "agent", self.CONTENT_GROUPS["agent"])
        строки = self.query(
            "SELECT j.id AS job_id, j.created_at, j.filename, j.media_duration_s, j.owner, "
            "       l.resolved, l.reason, l.outcome, l.actions, l.trackers, l.scorecard, "
            "       l.score, l.critical, l.warnings, l.fields, l.summary, "
            "       c.agent_speaker, c.nps, c.sentiment, c.sentiment_shift, c.mood_shift, "
            "       c.stress, c.frustration, c.repeat_contact, c.alerts, c.agent_score, "
            f"      {группа} AS group_key, "
            "       (SELECT cl.agent FROM calls cl WHERE cl.job_id = j.id LIMIT 1) AS agent, "
            "       (SELECT cl.src FROM calls cl WHERE cl.job_id = j.id LIMIT 1) AS src, "
            "       (SELECT cl.dst FROM calls cl WHERE cl.job_id = j.id LIMIT 1) AS dst, "
            "       (SELECT cl.direction FROM calls cl WHERE cl.job_id = j.id LIMIT 1) AS direction, "
            "       (SELECT cl.started_at FROM calls cl WHERE cl.job_id = j.id LIMIT 1) AS call_at "
            f"FROM {self._задания_окна()} JOIN llm_results l ON l.job_id = j.id "
            "LEFT JOIN content c ON c.job_id = j.id "
            f"WHERE {условие} AND COALESCE(l.error,'') = '' "
            "ORDER BY j.created_at DESC, j.id DESC LIMIT ?", [*args, max(1, int(limit))])
        готово = []
        for r in строки:
            з = dict(r)
            for к in ("actions", "trackers", "scorecard", "warnings", "fields", "critical"):
                if isinstance(з.get(к), str):
                    try:
                        з[к] = json.loads(з[к])
                    except (TypeError, ValueError):
                        з[к] = None
            if з.get("resolved") is not None:
                з["resolved"] = bool(з["resolved"])
            готово.append(з)
        return готово

    def calls_by_numbers(self, since: float, until: float) -> list[dict[str, Any]]:
        """Звонки окна с номерами — для поиска повторов и перезвонов по номеру."""
        return [dict(r) for r in self.query(
            "SELECT uniqueid, job_id, src, dst, direction, started_at, answered, billsec, agent "
            "FROM calls WHERE started_at >= ? AND started_at < ? "
            "ORDER BY started_at", (float(since), float(until)))]

    def llm_versions_last(self) -> dict[str, Any] | None:
        строка = self.query_one("SELECT * FROM llm_versions ORDER BY id DESC LIMIT 1")
        return dict(строка) if строка else None

    def llm_versions_note(self, *, version: int, model: str, fingerprint: str,
                          changes: str) -> bool:
        """Отметка смены настройки модели — если отпечаток не тот, что последний."""
        with self.write() as conn:
            последняя = conn.execute(
                "SELECT fingerprint FROM llm_versions ORDER BY id DESC LIMIT 1").fetchone()
            if последняя is not None and последняя["fingerprint"] == fingerprint:
                return False
            conn.execute("INSERT INTO llm_versions (at, version, model, fingerprint, changes) "
                         "VALUES (?,?,?,?,?)",
                         (now(), int(version), str(model or ""), str(fingerprint),
                          str(changes or "")[:500]))
            return True

    def llm_versions_list(self, since: float = 0.0,
                          until: float | None = None) -> list[dict[str, Any]]:
        где = "at >= ?" + (" AND at < ?" if until is not None else "")
        args: list[Any] = [float(since)] + ([float(until)] if until is not None else [])
        return [dict(r) for r in self.query(
            f"SELECT id, at, version, model, fingerprint, changes FROM llm_versions "
            f"WHERE {где} ORDER BY at", args)]

    def golden_put(self, job_id: str, answers: dict[str, Any], *, note: str = "",
                   by: str = "") -> bool:
        """Эталон человека по записи. False — записи нет."""
        сейчас = now()
        with self.write() as conn:
            есть = conn.execute("SELECT 1 FROM jobs WHERE id=?", (str(job_id),)).fetchone()
            if есть is None:
                return False
            conn.execute(
                "INSERT INTO llm_golden (job_id, answers, note, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET "
                "answers=excluded.answers, note=excluded.note, updated_at=excluded.updated_at",
                (str(job_id), json.dumps(answers, ensure_ascii=False), str(note or "")[:500],
                 str(by or ""), сейчас, сейчас))
            return True

    def golden_get(self, job_id: str) -> dict[str, Any] | None:
        строка = self.query_one("SELECT * FROM llm_golden WHERE job_id=?", (str(job_id),))
        if строка is None:
            return None
        з = dict(строка)
        try:
            з["answers"] = json.loads(з["answers"] or "{}")
        except (TypeError, ValueError):
            з["answers"] = {}
        return з

    def golden_list(self, *, owner: str | list[str] | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        своё, своё_args = self._owner_clause(owner, "j")
        строки = self.query(
            "SELECT g.job_id, g.answers, g.note, g.created_by, g.created_at, g.updated_at, "
            "       j.filename, j.created_at AS job_created_at "
            "FROM llm_golden g JOIN jobs j ON j.id = g.job_id "
            + (f"WHERE {своё} " if своё else "")
            + "ORDER BY g.updated_at DESC LIMIT ?", [*своё_args, max(1, int(limit))])
        готово = []
        for r in строки:
            з = dict(r)
            try:
                з["answers"] = json.loads(з["answers"] or "{}")
            except (TypeError, ValueError):
                з["answers"] = {}
            готово.append(з)
        return готово

    def golden_delete(self, job_id: str) -> bool:
        return bool(self.execute("DELETE FROM llm_golden WHERE job_id=?", (str(job_id),)))

    def golden_run_create(self, *, label: str, overrides: dict[str, Any], records: int,
                          by: str = "") -> int:
        with self.write() as conn:
            курсор = conn.execute(
                "INSERT INTO llm_golden_runs (label, status, overrides, started_at, records, "
                "created_by) VALUES (?,?,?,?,?,?)",
                (str(label or "")[:120], "идёт", json.dumps(overrides, ensure_ascii=False),
                 now(), int(records), str(by or "")))
            return int(курсор.lastrowid or 0)

    def golden_run_update(self, run_id: int, **поля: Any) -> None:
        if "result" in поля and not isinstance(поля["result"], (str, type(None))):
            поля["result"] = json.dumps(поля["result"], ensure_ascii=False)
        if not поля:
            return
        имена = ", ".join(f"{к}=?" for к in поля)
        self.execute(f"UPDATE llm_golden_runs SET {имена} WHERE id=?",
                     [*поля.values(), int(run_id)])

    def golden_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        готово = []
        for r in self.query("SELECT * FROM llm_golden_runs ORDER BY id DESC LIMIT ?",
                            (max(1, int(limit)),)):
            з = dict(r)
            for к in ("overrides", "result"):
                try:
                    з[к] = json.loads(з[к]) if з.get(к) else None
                except (TypeError, ValueError):
                    з[к] = None
            готово.append(з)
        return готово

    def vectors_put(self, job_id: str, части: list[tuple[int, str, list[float]]], *,
                    model: str) -> None:
        """Векторы смысла частей записи — заменяют прежние этой записи."""
        import array  # noqa: PLC0415

        with self.write() as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id=?", (str(job_id),)).fetchone() is None:
                return
            conn.execute("DELETE FROM llm_vectors WHERE job_id=?", (str(job_id),))
            for номер, текст, вектор in части:
                данные = array.array("f", [float(x) for x in вектор]).tobytes()
                conn.execute(
                    "INSERT INTO llm_vectors (job_id, part, model, dim, vector, text, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (str(job_id), int(номер), str(model or ""), len(вектор), данные,
                     str(текст or "")[:2000], now()))

    def vectors_iter(self, *, model: str, owner: str | list[str] | None = None,
                     since: float | None = None, until: float | None = None) -> Any:
        """Векторы модели — с задачей и временем записи; по одной строке."""
        где = ["v.model = ?"]
        args: list[Any] = [str(model or "")]
        своё, своё_args = self._owner_clause(owner, "j")
        if своё:
            где.append(своё)
            args.extend(своё_args)
        if since:
            где.append("j.created_at >= ?")
            args.append(float(since))
        if until:
            где.append("j.created_at < ?")
            args.append(float(until))
        курсор = self.conn.execute(
            "SELECT v.job_id, v.part, v.dim, v.vector, v.text, j.created_at, j.filename "
            "FROM llm_vectors v JOIN jobs j ON j.id = v.job_id "
            f"WHERE {' AND '.join(где)}", args)
        yield from курсор

    def vectors_stats(self) -> dict[str, Any]:
        строка = self.query_one(
            "SELECT COUNT(DISTINCT job_id) AS records, COUNT(*) AS parts, "
            "MAX(created_at) AS last FROM llm_vectors")
        модели = [dict(r) for r in self.query(
            "SELECT model, COUNT(DISTINCT job_id) AS records FROM llm_vectors GROUP BY model")]
        return {"records": int(строка["records"] or 0) if строка else 0,
                "parts": int(строка["parts"] or 0) if строка else 0,
                "last": строка["last"] if строка else None, "models": модели}

    def vectors_missing(self, *, model: str, limit: int = 100) -> list[str]:
        """Записи с ответом модели, у которых нет векторов этой модели."""
        return [str(r["job_id"]) for r in self.query(
            "SELECT l.job_id FROM llm_results l JOIN jobs j ON j.id = l.job_id "
            "WHERE COALESCE(l.error,'') = '' AND COALESCE(l.summary,'') <> '' "
            "AND NOT EXISTS (SELECT 1 FROM llm_vectors v WHERE v.job_id = l.job_id "
            "                AND v.model = ?) "
            "ORDER BY j.created_at DESC LIMIT ?", (str(model or ""), max(1, int(limit))))]

    def llmq_archive_done_since(self, since: float) -> int:
        """Сколько архивных записей разобрано (или взято) с момента `since`."""
        строка = self.query_one(
            "SELECT COUNT(*) AS n FROM llm_queue WHERE kind='архив' AND started_at >= ?",
            (float(since),))
        return int((строка or {"n": 0})["n"] or 0)

    def filenames(self, job_ids: Sequence[str]) -> dict[str, str]:
        """Имена файлов по номерам заданий — порциями, одним запросом на порцию."""
        имена: dict[str, str] = {}
        номера = list(dict.fromkeys(job_ids))
        for начало in range(0, len(номера), 500):
            порция = номера[начало:начало + 500]
            for r in self.query(
                    "SELECT id, filename FROM jobs WHERE id IN "
                    f"({','.join('?' for _ in порция)})", порция):
                имена[str(r["id"])] = str(r["filename"] or "")
        return имена

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

    def llmq_take(self, *, instance: str = "",
                  skip_kinds: Sequence[str] = ()) -> dict[str, Any] | None:
        """Берёт следующую запись и помечает её выполняющейся.

        Отбор и пометка — одним запросом под замком записи: иначе два
        потока (фоновый разбор и ручной вызов из интерфейса) взяли бы одну
        и ту же запись и сходили бы к модели дважды. Строка подписывается
        экземпляром сервера (`instance`) и отметкой жизни — по ним сосед на
        общей базе отличает чужой идущий разбор от брошенного.
        """

        мгновение = now()
        with self.write() as conn:
            мимо = [str(в) for в in skip_kinds if str(в)]
            строка = conn.execute(
                "SELECT * FROM llm_queue WHERE state=? "
                + (f"AND COALESCE(kind,'') NOT IN ({','.join('?' for _ in мимо)}) " if мимо else "")
                + "ORDER BY priority DESC, enqueued_at LIMIT 1",
                (self.LLMQ_ЖДЁТ, *мимо)).fetchone()
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

    def llmq_stats(self, since: float, *, until: float | None = None,
                   owner: str | list[str] | None = None) -> dict[str, Any]:
        """Сводка по завершённым запросам за окно: сколько, как долго, как часто ошибались.

        `until` — верхняя граница окна (не входит). Без неё окно открыто в
        будущее: число «сбоев за сутки» раскрывается в список отдельным
        запросом и позже, и разбор, закончившийся между сводкой и списком,
        попадал бы в список, не попав в число. Границы возвращаются в
        ответе — ими список и режется.
        """
        откуда, своё, своё_args = self._llmq_откуда(owner)
        окно, окно_args = "q.finished_at>=?", [float(since)]
        if until is not None:
            окно += " AND q.finished_at<?"
            окно_args.append(float(until))
        строка = self.query_one(
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS ok, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS failed, "
            "       AVG(q.latency_ms) AS avg_ms, MAX(q.latency_ms) AS max_ms, "
            "       SUM(q.calls) AS calls, "
            "       AVG(CASE WHEN q.started_at IS NOT NULL AND q.enqueued_at IS NOT NULL "
            "                THEN q.started_at-q.enqueued_at END) AS wait_s "
            f"{откуда} WHERE {окно} AND {своё}",
            (self.LLMQ_ГОТОВО, self.LLMQ_ОШИБКА, *окно_args, *своё_args))
        свод = dict(строка) if строка else {}
        свод["since"] = float(since)
        свод["until"] = float(until) if until is not None else None
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
            # Успешные — своим счётом, а не «всё минус сбои»: в `n` входят и
            # снятые с очереди (у отмены тоже есть время конца), и на графике
            # они рисовались разобранными успешно.
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS ok, "
            "       SUM(CASE WHEN q.state=? THEN 1 ELSE 0 END) AS failed, "
            "       AVG(q.latency_ms) AS avg_ms, "
            "       AVG(CASE WHEN q.started_at IS NOT NULL AND q.enqueued_at IS NOT NULL "
            "                THEN q.started_at-q.enqueued_at END) AS wait_s "
            f"{откуда} WHERE q.finished_at>=? AND q.finished_at<? AND {своё} "
            "GROUP BY bucket ORDER BY bucket",
            (since, шаг, self.LLMQ_ГОТОВО, self.LLMQ_ОШИБКА, since, until, *своё_args))
        return buckets, [dict(r) for r in rows]


def _row_to_llm(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    for к in ("actions", "trackers", "scorecard", "warnings", "fields", "critical"):
        raw = out.get(к)
        if isinstance(raw, str):
            try:
                out[к] = json.loads(raw)
            except (TypeError, ValueError):
                out[к] = None
    if out.get("resolved") is not None:
        out["resolved"] = bool(out["resolved"])
    return out
