"""Часть базы о проверке работы: оценка разговоров руководителем
(`qa_reviews`) и журнал доступа с цепочкой хешей.

Вынесено из `db.py` примесью к `Database`.
"""
from __future__ import annotations

import json
from typing import Any

from .db_util import ОсноваБазы, _экранировать_like, now
from .logging_setup import get_logger

log = get_logger("db")


#: Поля строки журнала доступа, входящие в хеш, — в этом порядке.
ПОЛЯ_ЖУРНАЛА = ("ts", "actor", "actor_id", "kind", "role", "action", "method",
                "path", "status", "ip")


def хеш_строки_журнала(прежний: str, поля: dict[str, Any]) -> str:
    """sha256 от хеша предыдущей строки и полей этой — звено цепочки журнала."""
    import hashlib  # noqa: PLC0415

    тело = json.dumps([прежний, *[поля.get(к) if к != "ts" else round(float(поля.get(к) or 0), 6)
                                  for к in ПОЛЯ_ЖУРНАЛА]],
                      ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(тело.encode("utf-8")).hexdigest()


class ПроверкаБазы(ОсноваБазы):
    """Примесь к `Database`: оценка разговоров и журнал доступа."""

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
            return int(курсор.lastrowid or 0) if курсор.rowcount else None

    def qa_submit(self, review_id: int, *, reviewer: str, score: float | None,
                  agree: bool | None = None, items: Any = None,
                  comment: str = "", scorecard: dict[str, str] | None = None) -> bool:
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
                "score=?, agree=?, items=?, comment=?, scorecard=? "
                "WHERE id=? AND status='pending'",
                (str(reviewer or ""), now(),
                 None if score is None else float(score),
                 None if своё is None else int(bool(своё)),
                 json.dumps(items, ensure_ascii=False) if items is not None else None,
                 str(comment or ""),
                 json.dumps(scorecard, ensure_ascii=False) if scorecard else None,
                 int(review_id)))
            return bool(курсор.rowcount)

    def qa_for_job(self, job_id: str) -> dict[str, Any] | None:
        """Последняя проверка качества по записи — для карточки задания.

        Очередь «Контроль качества» вела в карточку записи, а оценить там
        было нечем: закрыть проверку можно было только запросом
        PUT /api/qa/{id}. Карточка теперь знает, стоит ли запись на проверке.
        """
        строка = self.query_one(
            "SELECT id, status, agent, assigned_to, assigned_by, assigned_at, due_at, "
            "reviewer, reviewed_at, auto_score, score, agree, comment, reason, scorecard "
            "FROM qa_reviews WHERE job_id=? ORDER BY id DESC LIMIT 1", (str(job_id),))
        if строка is None:
            return None
        запись = dict(строка)
        if запись.get("agree") is not None:
            запись["agree"] = bool(запись["agree"])
        try:
            запись["scorecard"] = json.loads(запись["scorecard"]) if запись.get("scorecard") else None
        except (TypeError, ValueError):
            запись["scorecard"] = None
        return запись

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
        # Согласие человека и модели по каждому вопросу скоркарты: где
        # модель спорит с проверяющими, вопрос надо переформулировать или не
        # доверять ему.
        по_вопросам: dict[str, dict[str, Any]] = {}
        for строка in self.query(
                "SELECT q.scorecard AS human, l.scorecard AS model "
                f"{откуда} JOIN llm_results l ON l.job_id = q.job_id "
                f"WHERE {условие} AND q.scorecard IS NOT NULL AND l.scorecard IS NOT NULL",
                args):
            try:
                человек = json.loads(строка["human"] or "{}")
                модель = {str(о.get("id")): о.get("answer")
                          for о in json.loads(строка["model"] or "[]") if isinstance(о, dict)}
            except (TypeError, ValueError, AttributeError):
                continue
            if not isinstance(человек, dict):
                continue
            for ид, ответ in человек.items():
                if ид not in модель or ответ not in ("да", "нет", "н/п"):
                    continue
                пункт = по_вопросам.setdefault(str(ид), {"id": str(ид), "n": 0, "agree": 0,
                                                         "model_yes_human_no": 0,
                                                         "model_no_human_yes": 0})
                пункт["n"] += 1
                if модель[ид] == ответ:
                    пункт["agree"] += 1
                elif модель[ид] == "да" and ответ == "нет":
                    пункт["model_yes_human_no"] += 1
                elif модель[ид] == "нет" and ответ == "да":
                    пункт["model_no_human_yes"] += 1
        согласие_модели = [{**п, "agree_share": round(п["agree"] / п["n"], 3)}
                           for п in sorted(по_вопросам.values(), key=lambda п: п["id"])]
        return {
            "scorecard": согласие_модели,
            "done": всего,
            "pending": int((self.query_one(
                f"SELECT COUNT(*) AS n {откуда} WHERE {ждут_условие}", своё_args
            ) or {"n": 0})["n"] or 0),
            "agree_share": (round(float(общее["agreed"] or 0) / оценено, 3)
                            if оценено and общее else None),
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
        поля = {"ts": now(), "actor": str(actor or "")[:128],
                "actor_id": str(actor_id or "")[:64], "kind": str(kind or "")[:16],
                "role": str(role or "")[:32], "action": str(action or "")[:200],
                "method": str(method or "")[:8], "path": str(path or "")[:500],
                "status": int(status or 0), "ip": str(ip or "")[:64]}
        try:
            with self.write() as conn:
                прежняя = conn.execute(
                    "SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
                поля["hash"] = хеш_строки_журнала(
                    str(прежняя[0] or "") if прежняя else "", поля)
                conn.execute(
                    "INSERT INTO audit (ts, actor, actor_id, kind, role, action, "
                    "method, path, status, ip, hash) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(поля[к] for к in (*ПОЛЯ_ЖУРНАЛА, "hash")))
        except Exception as exc:                            # noqa: BLE001
            log.warning("Журнал доступа не записан: %s", exc)
            return
        приёмник = self.audit_sink
        if приёмник is not None:
            try:
                приёмник(поля)
            except Exception as exc:                        # noqa: BLE001
                log.debug("Строка журнала не отправлена во внешний приёмник: %s", exc)

    #: Внешний приёмник строк журнала доступа (syslog) — ставит сервер.
    audit_sink: Any = None

    def audit_verify(self, *, limit: int = 1_000_000) -> dict[str, Any]:
        """Проверяет цепочку хешей журнала доступа.

        Первая оставшаяся строка — опора (строки старше срока хранения
        убраны уборкой, их хешей нет). Строки без хеша — записанные до
        появления цепочки — пропускаются и цепочку не рвут, если идут до первой
        строки с хешем. Возвращает число проверенных строк и первую
        несошедшуюся — по ней видно, где журнал правили или вырезали.
        """
        проверено = 0
        прежний: str | None = None
        без_хеша = 0
        for строка in self.query(
                "SELECT id, hash, " + ", ".join(ПОЛЯ_ЖУРНАЛА)
                + " FROM audit ORDER BY id LIMIT ?", (max(1, int(limit)),)):
            запись = dict(строка)
            хеш = str(запись.get("hash") or "")
            if not хеш:
                if прежний is not None:
                    return {"ok": False, "checked": проверено, "broken_id": запись["id"],
                            "reason": "строка без хеша посреди цепочки"}
                без_хеша += 1
                continue
            if прежний is not None and хеш_строки_журнала(прежний, запись) != хеш:
                return {"ok": False, "checked": проверено, "broken_id": запись["id"],
                        "reason": "хеш не сходится: строку правили, или перед ней "
                                  "удалены строки"}
            прежний = хеш
            проверено += 1
        return {"ok": True, "checked": проверено, "legacy": без_хеша,
                "last_hash": прежний or ""}

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
