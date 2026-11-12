"""Часть базы о разборе содержания записей: признаки, совпадения категорий,
отметки коучинга и эталонов, очередь ручной проверки, контрольные прогоны.

Вынесено из `db.py` примесью к `Database`, как и телефония (`db_telephony.py`).
"""
from __future__ import annotations

import json
from typing import Any

from .db_util import ОсноваБазы, now
from .errors import StorageError
from .logging_setup import get_logger

log = get_logger("db")


class СодержаниеБазы(ОсноваБазы):
    """Примесь к `Database`: разбор содержания записей."""

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

    def content_pending(self, version: int, limit: int = 200, *,
                        finished_after: float | None = None) -> list[dict[str, Any]]:
        """Завершённые задания, разбор которых устарел или его нет вовсе.

        Нужен для пересчёта: разбор появился позже архива, а словари и
        правила меняются — по версии видно, что пора считать заново.

        `finished_after` — только завершённые позже метки: фоновому поиску
        не нужно каждые две минуты обходить весь архив, если всё до метки
        уже разобрано (см. `ContentIndex.backfill_once`).
        """
        после = ""
        аргументы: list[Any] = [version]
        if finished_after is not None:
            # Без отметки завершения (задание, заведённое готовым) — тоже:
            # иначе метка прятала бы его навсегда.
            после = "  AND (j.finished_at IS NULL OR j.finished_at > ?) "
            аргументы.append(float(finished_after))
        аргументы.append(limit)
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
            + после +
            "ORDER BY j.created_at DESC LIMIT ?",
            аргументы)
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
        всего = int(self.query_scalar(
            "SELECT COUNT(*) n FROM jobs j WHERE j.status='completed' "
            "AND j.text IS NOT NULL AND j.text != '' "
            f"AND COALESCE(j.source,'') <> 'control'{где}", свои))
        # Тем же проходом — сколько из «разобранных» на деле заглушки: разбор
        # записи не удался несколько раз подряд, и в строке лежит только
        # ошибка (тональности у неё нет, а у настоящего разбора она есть
        # всегда). Без этого числа такие записи были неотличимы от
        # разобранных. Признак — сама ошибка в подробностях, а не пустая
        # тональность: свод без неё бывает и у настоящего разбора.
        строка = self.query_one(
            "SELECT COUNT(*) n, SUM(CASE WHEN c.sentiment_label IS NULL "
            "AND json_valid(c.detail) AND json_extract(c.detail, '$.error') IS NOT NULL "
            "THEN 1 ELSE 0 END) f "
            "FROM content c JOIN jobs j ON j.id = c.job_id "
            "WHERE c.version >= ? AND j.status='completed' "
            "AND j.text IS NOT NULL AND j.text != '' "
            f"AND COALESCE(j.source,'') <> 'control'{где}", (version, *свои))
        разобрано = int(строка["n"] or 0) if строка else 0
        return {"total": всего, "analyzed": разобрано,
                "pending": max(0, всего - разобрано),
                "failed": int(строка["f"] or 0) if строка else 0}

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
        выражение: str | None
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
        # Неизвестный столбец — пустое место (None), а не сдвиг номеров.
        return [tuple(None if н is None else r[н] for н in порядок)  # type: ignore[misc]
                for r in rows]

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
