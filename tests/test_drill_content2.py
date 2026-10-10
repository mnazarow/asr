"""Заход 49: раскрытие чисел второй половины «Аналитики записей».

«Операторы» с карточкой, «Разрезы», «Темы», «Связи», «Что послушать»: под
каждым числом — список записей, из которых оно сложилось. Здесь сверяется
главное: список тот же, что число. Свод считает одним запросом, список —
другим, и сойтись они обязаны на каждом разрезе, а не «в среднем»: число,
раскрытое не в свои записи, хуже числа без раскрытия.

Общих данных (`drill_seed`) этим вкладкам мало: там один говорящий, нет
нарушений, отметок коучинга, прошлого периода и тем, которые менялись.
Недостающее досевается здесь же, поверх общего архива.
"""
from __future__ import annotations

import time
from typing import Any

import pytest
from asrhub import drill
from asrhub.db import Database
from asrhub.insights import ОТБОРЫ, ПАРЫ, РАЗРЕЗЫ
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

СУТКИ = 86400.0

#: Номер записи архива из её идентификатора (`dr0042` → 42) — прямо в SQL.
_N = "CAST(substr(job_id, 3) AS INTEGER)"


def досеять(архив: Any) -> dict[str, Any]:
    """Делает архив пёстрым для этих вкладок; отдаёт, что куда легло.

    Признаки разбора раскладываются по записям с разными периодами — чтобы
    ни два оператора, ни два показателя не давали одного и того же набора.
    """
    db = архив.db
    сейчас = архив.данные["now"]
    # Операторы: три метки говорящего, одна из них пустая (в своде «—»).
    db.execute(f"UPDATE content SET agent_speaker = 'SPEAKER_01' WHERE {_N} % 3 = 1")
    db.execute(f"UPDATE content SET agent_speaker = '' WHERE {_N} % 7 = 0")
    # Счётчики, которых в общих данных нет вовсе.
    db.execute(f"UPDATE content SET violations = 1 + {_N} % 2 WHERE {_N} % 6 = 2")
    db.execute(f"UPDATE content SET name_uses = 2 WHERE {_N} % 4 = 0")
    db.execute(f"UPDATE content SET name_uses = NULL WHERE {_N} % 9 = 5")
    db.execute(f"UPDATE content SET frustration = 1 WHERE {_N} % 9 = 2")
    db.execute(f"UPDATE content SET objections = 2, objections_unhandled = 1 WHERE {_N} % 10 = 3")
    db.execute(f"UPDATE content SET profanity_agent = 1, profanity = 2 WHERE {_N} % 15 = 4")
    db.execute(f"UPDATE content SET empathy = -20 WHERE {_N} % 8 = 5")
    db.execute(f"UPDATE content SET monologue_s = 150 + {_N} WHERE {_N} % 11 = 6")
    db.execute(f"UPDATE content SET interruptions = {_N} % 4, dead_air_s = {_N} % 5 * 9, "
               f"sentiment_shift = ({_N} % 7 - 3) * 0.1, money_max = NULLIF({_N} % 6, 0) * 1000, "
               f"repeat_contact = CASE WHEN {_N} % 13 = 2 THEN 1 ELSE 0 END, "
               f"commitments = {_N} % 3, commitments_dated = {_N} % 2, "
               f"reply_delay_s = NULLIF({_N} % 5, 0) * 0.4")
    db.execute(f"UPDATE content SET sentiment_label = 'смешанная' WHERE {_N} % 12 = 9")
    # Показатель измерен не у всех: среднее считается по части записей, и
    # список под ним обязан быть той же частью, а не всей группой.
    db.execute(f"UPDATE content SET wpm = NULL WHERE {_N} % 5 = 1")
    db.execute(f"UPDATE content SET compliance = NULL, agent_score = NULL WHERE {_N} % 7 = 3")
    db.execute(f"UPDATE content SET sentiment = NULL WHERE {_N} % 16 = 11")
    db.execute("UPDATE jobs SET media_duration_s = NULL "
               "WHERE CAST(substr(id, 3) AS INTEGER) % 14 = 9")
    # Нарушения по категориям — строками совпадений, как их кладёт разбор.
    for n, категория in ((2, "rude"), (8, "rude"), (14, "promise"), (20, "rude"),
                         (26, "promise"), (32, "rude"), (38, "rude"), (44, "promise")):
        db.execute("INSERT OR REPLACE INTO content_hits (job_id, category, kind, count, first_s) "
                   "SELECT job_id, ?, 'violation', 1 + ? % 3, 5.0 FROM content WHERE job_id = ?",
                   (категория, n, f"dr{n:04d}"))
    # Очередь коучинга: часть записей уже разобрана, одна возвращена.
    очередь = [р["job_id"] for р in db.query(
        "SELECT c.job_id FROM content c WHERE "
        + " OR ".join(f"({выр})" for _, выр in Database.COACHING_REASONS)
        + " ORDER BY c.job_id")]
    разобранные = очередь[::4]
    for job_id in разобранные:
        db.set_mark(job_id, "coaching", "done")
    db.set_mark(очередь[1], "coaching", "open")
    db.set_mark(очередь[2], "reference", "yes")
    # Метка, набранная дважды: запись всё равно одна.
    db.execute("UPDATE jobs SET tags = 'хорошо, хорошо' WHERE id = 'dr0004'")
    db.execute("UPDATE jobs SET tags = ' срочно ,доставка' WHERE id = 'dr0007'")
    # Прошлый период: самые старые записи уезжают на сорок дней назад — у
    # месяца появляется «было», у карточки оператора — прошлое окно.
    db.execute("UPDATE jobs SET created_at = created_at - ? "
               "WHERE CAST(substr(id, 3) AS INTEGER) >= 72", (40 * СУТКИ,))
    # А несколько — ещё дальше, за прошлый месяц: «было» — это окно, а не
    # «всё, что раньше».
    db.execute("UPDATE jobs SET created_at = created_at - ? "
               "WHERE CAST(substr(id, 3) AS INTEGER) >= 90", (60 * СУТКИ,))
    # Темы, которые менялись: слово только этого месяца, слово, ушедшее в
    # прошлое, и слово, которого стало больше.
    свежие = [р["job_id"] for р in db.query(
        "SELECT c.job_id FROM content c JOIN jobs j ON j.id = c.job_id "
        "WHERE j.created_at >= ? ORDER BY j.created_at DESC", (сейчас - 30 * СУТКИ,))]
    старые = [р["job_id"] for р in db.query(
        "SELECT c.job_id FROM content c JOIN jobs j ON j.id = c.job_id "
        "WHERE j.created_at < ? ORDER BY j.created_at DESC", (сейчас - 30 * СУТКИ,))]
    assert len(свежие) >= 30 and len(старые) >= 10, (len(свежие), len(старые))
    основы = (("новинк", "новинка", свежие[:5]), ("старин", "старина", старые[:6]),
              ("подорожа", "подорожание", свежие[3:14] + старые[:2]))
    for основа, слово, записи in основы:
        for job_id in записи:
            db.execute("INSERT OR REPLACE INTO content_terms (job_id, stem, n) VALUES (?,?,?)",
                       (job_id, основа, 2))
        db.execute("INSERT OR REPLACE INTO content_vocab (stem, word, n) VALUES (?,?,?)",
                   (основа, слово, len(записи)))
    # Запись, отправленную на повтор: разбор и основы у неё остались, а
    # завершённой она быть перестала.
    на_повторе = свежие[4]
    db.execute("UPDATE jobs SET status = 'queued' WHERE id = ?", (на_повторе,))
    return {"now": сейчас, "разобранные": разобранные, "на_повторе": на_повторе,
            "свежих": len(свежие) - 1}


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-content2") / "data")
    хранилище.досев = досеять(хранилище)
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _свод(c, путь: str, **параметры: Any) -> dict[str, Any]:
    ответ = c.get(f"/api/content/{путь}", params=параметры)
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    assert тело["window"]["until"] > (тело["window"]["since"] or 0), тело["window"]
    return тело


# ---------------------------------------------------------------------------
# То же соответствие, что в интерфейсе (`РаскрытиеЗаписей` в app.js)
# ---------------------------------------------------------------------------


def _группа(измерение: str, ключ: str) -> list[str]:
    """Группа разреза → отборы списка; зеркало `РаскрытиеЗаписей.ИЗМЕРЕНИЯ`."""
    if измерение == "tag":
        return ["untagged"] if ключ == "без метки" else [f"tag:{ключ}"]
    if измерение == "category":
        return [f"category:{ключ}"]
    if измерение == "weekday":
        return [f"weekday:{(int(ключ) + 6) % 7}"]
    if измерение == "hour":
        return [f"hour:{int(ключ)}"]
    if измерение == "date":
        return [f"date:{ключ}"]
    return [f"{измерение}:{'' if ключ == '—' else ключ}"]


#: Счётчик свода → отбор его числителя.
СЧЁТЧИКИ = {"records": [], "negative": ["negative"], "alert_records": ["alerts"],
            "violation_records": ["violation"], "named": ["named"],
            "frustrated": ["frustrated"]}

#: Средние: по этим величинам список упорядочен, и записи без величины в
#: него не входят — как не входят и в среднее.
СРЕДНИЕ_ТАБЛИЦЫ = ("sentiment", "agent_score", "compliance", "empathy", "wpm",
                   "interruptions", "silence_share", "talk_share", "monologue_s")
СРЕДНИЕ_КАРТОЧКИ = (*СРЕДНИЕ_ТАБЛИЦЫ, "reply_delay_s", "duration_s", "mood", "stress",
                    "effort", "fatigue", "clarity", "accuracy", "politeness",
                    "personalization", "rhythm", "filler_rate", "diminutive_rate", "nps")


def _значения(c, окно: dict[str, Any], f: list[str], величина: str) -> list[float]:
    """Величина по всем записям списка «по величине» — как его листает человек."""
    строки = все_строки(c, base="content", f=f, sort=f"{величина}:asc", limit=200,
                        **{к: v for к, v in окно.items() if v is not None})
    assert all(с["value"] is not None for с in строки)
    return [float(с["value"]) for с in строки]


def _сверить_строку(c, окно: dict[str, Any], кто: list[str], строка: dict[str, Any],
                    средние: tuple[str, ...], счёт: dict[str, int]) -> None:
    """Каждое число строки свода против длины (или среднего) своего списка."""
    for поле, отбор in СЧЁТЧИКИ.items():
        assert всего(c, окно, кто + отбор, base="content") == строка[поле], (кто, поле)
        счёт[поле] = счёт.get(поле, 0) + строка[поле]
    for величина in средние:
        значения = _значения(c, окно, кто, величина)
        if строка[величина] is None:
            assert not значения, (кто, величина)
            continue
        assert значения, (кто, величина)
        assert sum(значения) / len(значения) == pytest.approx(строка[величина], abs=0.006), \
            (кто, величина)
        счёт[величина] = счёт.get(величина, 0) + len(значения)


# ---------------------------------------------------------------------------
# Отборы раздела — выражениями свода
# ---------------------------------------------------------------------------


def test_отборы_что_послушать_взяты_из_раздела_а_не_переписаны():
    """Одноимённые отборы списка заданий у шести видов стоят с порогом
    («монолог от 150 с»), а раздел показывает рейтинг по всем записям, где
    величина есть: раскрой одно другим — и список вдвое короче таблицы."""
    for вид, описание in ОТБОРЫ.items():
        assert drill.ОТБОРЫ[f"listen_{вид}"].sql == описание["where"]
        assert drill.ОТБОРЫ[f"listen_{вид}"].нужно == {"c"}
    assert drill.ОТБОРЫ["listen_monologue"].sql != Database.CONTENT_FILTERS["monologue"]
    # Порядок — первым ключом порядка самого отбора, если он величина.
    assert drill.ПОРЯДОК_ПОСЛУШАТЬ["negative"] == "sentiment:asc"
    assert drill.ПОРЯДОК_ПОСЛУШАТЬ["monologue"] == "monologue_s:desc"
    assert drill.ПОРЯДОК_ПОСЛУШАТЬ["open_commitments"] == ""
    assert set(drill.ПОРЯДОК_ПОСЛУШАТЬ) == set(ОТБОРЫ)


def test_признаки_связей_раскрываются_теми_же_колонками():
    """«Записей» у связи — записи с обоими признаками; `has:<признак>`
    обязан смотреть в ту колонку, по которой считана выборка."""
    for первый, второй, _ in ПАРЫ:
        for признак in (первый, второй):
            имя = {"speakers": "content_speakers"}.get(признак, признак)
            assert drill.ЧИСЛА[имя][0] == Database.CONTENT_NUMERIC[признак], признак
    # Длительность без подстановки нуля: среднее считается по измеренным.
    assert drill.ЧИСЛА["duration_s"][0] == "j.media_duration_s"
    assert "COALESCE" in drill.ЧИСЛА["duration"][0]
    # Число говорящих задания и разбора — разные колонки под разными именами.
    assert drill.ОТБОРЫ["speakers"].sql.startswith("COALESCE(j.speakers_count")
    assert "speakers" not in drill.ЧИСЛА


# ---------------------------------------------------------------------------
# Разрезы и операторы: таблицы и графики
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("измерение", sorted(РАЗРЕЗЫ))
def test_строка_разреза_сходится_со_своим_списком(c, измерение):
    """Каждая группа каждого разреза: счётчики — длиной списка, средние —
    средним по списку. На «Разрезах» восемь из них, остальные — тот же
    перечень `ИЗМЕРЕНИЯ` для «Операторов» и выводов."""
    счёт: dict[str, int] = {}
    групп = 0
    for период in ("month", "all"):
        свод = _свод(c, f"breakdown/{измерение}", period=период)
        assert свод["dimension"] == измерение and свод["items"], свод
        for строка in свод["items"]:
            _сверить_строку(c, свод["window"], _группа(измерение, строка["key"]),
                            строка, СРЕДНИЕ_ТАБЛИЦЫ, счёт)
            групп += 1
    assert групп >= 2
    # Сверка нулей с нулями ничего не ловит.
    for поле in ("records", "negative", "alert_records", "violation_records", "named",
                 "frustrated", *СРЕДНИЕ_ТАБЛИЦЫ):
        assert счёт.get(поле), (измерение, поле, счёт)


def test_разрезы_на_вкладке_разные_и_метки_без_повторов(c, архив):
    """Группы не совпадают между собой — иначе сверка прошла бы и с
    перепутанными отборами; метка, набранная дважды, запись не удваивает."""
    свод = _свод(c, "breakdown/tag", period="all")
    по_меткам = {г["key"]: г["records"] for г in свод["items"]}
    assert len(set(по_меткам.values())) >= 3, по_меткам
    assert "без метки" in по_меткам
    # У dr0004 метки «хорошо, хорошо»: в группу она входит один раз.
    в_базе = архив.db.query_one(
        "SELECT COUNT(*) AS n FROM content c JOIN jobs j ON j.id = c.job_id "
        "WHERE j.status = 'completed' AND has_tag(j.tags, 'хорошо') = 1")["n"]
    assert по_меткам["хорошо"] == в_базе == всего(c, свод["window"], ["tag:хорошо"],
                                                  base="content")
    from asrhub.insights import Insights

    развёрнуто = dict(Insights._развернуть_метки(
        [{"group_key": "vip, vip ,срочно", "records": 3, "sentiment": 0.5, "scored": 3}]))
    assert развёрнуто["vip"]["records"] == 3 and развёрнуто["срочно"]["records"] == 3


@pytest.mark.parametrize("по_кому", ["speaker", "owner", "agent", "queue", "station"])
def test_таблица_операторов_сходится_со_списками(c, по_кому):
    счёт: dict[str, int] = {}
    for период in ("month", "all"):
        свод = _свод(c, "agents", period=период, by=по_кому)
        assert свод["by"] == по_кому and свод["items"]
        for строка in свод["items"]:
            _сверить_строку(c, свод["window"], _группа(по_кому, строка["key"]),
                            строка, СРЕДНИЕ_ТАБЛИЦЫ, счёт)
    assert all(счёт.get(поле) for поле in ("records", "negative", "violation_records",
                                           "named", "agent_score", "talk_share")), счёт


# ---------------------------------------------------------------------------
# Очередь коучинга
# ---------------------------------------------------------------------------


def test_счётчик_очереди_коучинга_сходится_со_списком(c, архив):
    """Очередь — то, что осталось: разобранные из неё ушли. Список под
    счётчиком обязан отсеять их так же, а с флажком «с разобранными» —
    так же не отсеивать."""
    for период in ("month", "all"):
        открытые = _свод(c, "coaching", period=период, limit=30)
        все = _свод(c, "coaching", period=период, limit=30, done="true")
        assert открытые["total"] > открытые["shown"] > 0      # счётчик больше таблицы
        assert all((з["mark"] or {}).get("status") != "done" for з in открытые["items"])
        assert всего(c, открытые["window"], ["coaching", "coaching_open"],
                     base="content") == открытые["total"]
        assert всего(c, все["window"], ["coaching"], base="content") == все["total"]
        assert все["total"] > открытые["total"]
    # Возвращённая в очередь запись (отметка «open») в ней остаётся.
    номера = {с["id"] for с in все_строки(c, base="content", limit=200,
                                          f=["coaching", "coaching_open"])}
    assert not номера & set(архив.досев["разобранные"])
    assert архив.db.query_one("SELECT job_id FROM content_marks WHERE kind='coaching' "
                              "AND status='open'")["job_id"] in номера


# ---------------------------------------------------------------------------
# Карточка оператора
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("по_кому,ключ", [("speaker", "SPEAKER_00"), ("speaker", "SPEAKER_01"),
                                          ("speaker", "—"), ("owner", "борис"),
                                          ("owner", "—"), ("agent", "101")])
def test_карточка_оператора_сходится_со_списками(c, по_кому, ключ):
    кто = _группа(по_кому, ключ)
    счёт: dict[str, int] = {}
    for период in ("month", "week", "all"):
        к = _свод(c, f"agents/{ключ}", period=период, by=по_кому)
        окно = к["window"]
        assert к["summary"]["records"], (по_кому, ключ, период)
        # Шапка, плитки и столбец «Оператор».
        _сверить_строку(c, окно, кто, к["summary"], СРЕДНИЕ_КАРТОЧКИ, счёт)
        # Столбец «Команда» — то же без оператора.
        assert всего(c, окно, [], base="content") == к["team"]["records"]
        assert всего(c, окно, ["negative"], base="content") == к["team"]["negative"]
        # Столбец «Прошлый период» — своим окном из ответа.
        if период == "all":
            assert к["previous_window"] is None and к["previous"] is None
        else:
            прошлое = к["previous_window"]
            assert прошлое["until"] == окно["since"]
            assert всего(c, прошлое, кто, base="content") == к["previous"]["records"]
            assert всего(c, прошлое, кто + ["negative"], base="content") \
                == к["previous"]["negative"]
            счёт["прошлых"] = счёт.get("прошлых", 0) + к["previous"]["records"]
        # Графики по неделям: точка — корзина со своими границами.
        ход = к["timeline"]
        assert len(ход) >= 4
        for n, точка in enumerate(ход):
            корзина = {"since": точка["since"], "until": точка["until"]}
            assert всего(c, корзина, кто, base="content") == точка["records"], (период, n)
            for величина, допуск in (("agent_score", 0.06), ("sentiment", 0.001)):
                значения = _значения(c, корзина, кто, величина)
                if точка[величина] is None:
                    assert not значения
                else:
                    assert sum(значения) / len(значения) == pytest.approx(
                        точка[величина], abs=допуск)
            if n:
                assert точка["since"] == ход[n - 1]["until"]       # без щелей и нахлёстов
        if период != "all":
            assert ход[0]["since"] == окно["since"]
            assert sum(точка["records"] for точка in ход) == к["summary"]["records"]
        # Нарушения по категориям.
        for нарушение in к["violations"]:
            assert всего(c, окно, кто + [f"violation_category:{нарушение['category']}"],
                         base="content") == нарушение["records"]
            счёт["нарушений"] = счёт.get("нарушений", 0) + нарушение["records"]
        # Очередь коучинга оператора.
        assert всего(c, окно, кто + ["coaching", "coaching_open"],
                     base="content") == к["coaching_total"]
        счёт["очередь"] = счёт.get("очередь", 0) + к["coaching_total"]
    assert счёт["records"] and счёт["очередь"], счёт
    if (по_кому, ключ) == ("speaker", "SPEAKER_00"):
        # На самом пёстром операторе не пусто ничего из сверяемого.
        for поле in ("negative", "violation_records", "named", "frustrated", "прошлых",
                     "нарушений", *СРЕДНИЕ_КАРТОЧКИ):
            if поле in ("stress", "fatigue", "clarity", "accuracy", "rhythm"):
                continue                      # на коротких репликах архива не считаются
            assert счёт.get(поле), (поле, счёт)


def test_неделя_карточки_делится_на_несколько_корзин(c):
    """Одна непустая корзина сошлась бы и с перепутанными границами."""
    к = _свод(c, "agents/SPEAKER_00", period="week", by="speaker")
    непустые = [т for т in к["timeline"] if т["records"]]
    assert len(непустые) >= 2, [(т["ts"], т["records"]) for т in к["timeline"]]
    # При «всё время» ход берётся за девяносто дней — границы у точек свои.
    все = _свод(c, "agents/SPEAKER_00", period="all", by="speaker")
    assert все["window"]["since"] is None
    assert все["timeline"][0]["since"] == pytest.approx(time.time() - 90 * СУТКИ, abs=600)


# ---------------------------------------------------------------------------
# Темы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("период", ["day", "month", "all"])
def test_темы_сходятся_со_списками(c, период):
    свод = _свод(c, "topics", period=период)
    окно = свод["window"]
    # «по N разобранным записям периода».
    assert всего(c, окно, [], base="content") == свод["corpus"] > 0
    # Число записей темы. За всё время и без владельца свод считает по
    # таблице основ, не заглядывая в задания, — и сам об этом говорит.
    assert свод["completed_only"] is (период != "all")
    основа = "content" if свод["completed_only"] else "jobs"
    assert len(свод["items"]) >= 20
    for тема in свод["items"]:
        assert всего(c, окно, [f"stem:{тема['stem']}"], base=основа) == тема["records"], тема
    assert len({тема["records"] for тема in свод["items"]}) >= 3
    # Ход тем и новые слова — своим окном: при «всё время» это месяц.
    ход = свод["trend_window"]
    assert ход["period"] == ("month" if период == "all" else период)
    assert ход["before_since"] < ход["since"] < ход["until"] == окно["until"]
    сейчас = {"since": ход["since"], "until": ход["until"]}
    было = {"since": ход["before_since"], "until": ход["since"]}
    assert свод["trend"], период
    for тема in свод["trend"]:
        отбор = [f"stem:{тема['stem']}"]
        assert всего(c, сейчас, отбор, base="content") == тема["now"], тема
        assert всего(c, было, отбор, base="content") == тема["before"], тема
    assert any(тема["now"] for тема in свод["trend"])
    assert any(тема["before"] for тема in свод["trend"])
    # «Было» — именно прошлое окно: раньше него записи тоже есть.
    assert всего(c, {"until": ход["before_since"]}, [], base="content") > 0 or период == "day"
    assert свод["new"], период
    for тема in свод["new"]:
        assert всего(c, сейчас, [f"stem:{тема['stem']}"], base="content") == тема["now"] > 0


def test_тема_за_всё_время_считает_и_запись_на_повторе(c, архив):
    """Расхождение, ради которого ответ и сообщает `completed_only`.

    Запись отправили на повтор: завершённой она быть перестала, а разбор и
    основы у неё остались. За период свод её не считает, за всё время (без
    владельца) — считает. Список под числом обязан повторить и то и другое.
    """
    статус = архив.db.query_one("SELECT status FROM jobs WHERE id = ?",
                                (архив.досев["на_повторе"],))["status"]
    assert статус == "queued"
    за_всё = _свод(c, "topics", period="all")
    за_месяц = _свод(c, "topics", period="month")
    всё = next(т for т in за_всё["items"] if т["stem"] == "новинк")
    месяц = next(т for т in за_месяц["items"] if т["stem"] == "новинк")
    assert всё["records"] == месяц["records"] + 1 == 5
    assert всего(c, за_всё["window"], ["stem:новинк"], base="jobs") == 5
    assert всего(c, за_всё["window"], ["stem:новинк"], base="content") == 4
    assert всего(c, за_месяц["window"], ["stem:новинк"], base="content") == 4


# ---------------------------------------------------------------------------
# Связи
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("период", ["month", "all"])
def test_записей_у_связи_столько_сколько_в_списке(c, период):
    свод = _свод(c, "correlations", period=период)
    assert свод["items"] and свод["sampled"] < свод["limit"]
    assert всего(c, свод["window"], [], base="content") == свод["sampled"]
    for связь in свод["items"]:
        x, y = ({"speakers": "content_speakers"}.get(п, п) for п in (связь["x"], связь["y"]))
        пара = [f"has:{x}", f"has:{y}"]
        assert всего(c, свод["window"], пара, base="content") == связь["n"], связь
        # Порядок по первому признаку никого не теряет: он у всех измерен.
        assert всего(c, свод["window"], пара, base="content", sort=f"{x}:desc") == связь["n"]
    # Признак измерен не у всех: «записей» у связи меньше выборки.
    assert any(связь["n"] < свод["sampled"] for связь in свод["items"])


def test_связи_по_усечённой_выборке_говорят_об_этом(c, monkeypatch):
    """Дальше предела связи считаются по самым свежим записям, и «все
    записи периода с обоими признаками» — уже другое число: интерфейс
    узнаёт об этом по `sampled == limit` и число не раскрывает."""
    from asrhub import insights

    monkeypatch.setattr(insights, "ПРЕДЕЛ_СВЯЗЕЙ", 20)
    monkeypatch.setattr(insights, "МИН_ПАР", 5)
    свод = _свод(c, "correlations", period="all")
    assert свод["sampled"] == свод["limit"] == 20
    assert всего(c, свод["window"], [], base="content") > свод["sampled"]


# ---------------------------------------------------------------------------
# Что послушать
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("период", ["month", "all"])
def test_что_послушать_раскрывается_тем_же_отбором_и_порядком(c, период):
    с_данными = длинных = 0
    for вид in ОТБОРЫ:
        свод = _свод(c, "records", kind=вид, period=период, limit=50)
        что = свод["drill"]
        assert что["base"] == "content" and что["f"] == [f"listen_{вид}"]
        в_списке = всего(c, свод["window"], что["f"], base="content")
        # Порядок по величине не теряет записей: у отобранных она есть.
        assert всего(c, свод["window"], что["f"], base="content", sort=что["sort"]) == в_списке
        показано = len(свод["items"])
        if свод["more"]:
            # «первые 50»: число показанных — не число записей отбора.
            assert показано == 50 < в_списке
            длинных += 1
        else:
            assert показано == в_списке, (вид, показано, в_списке)
        if not показано:
            continue
        с_данными += 1
        строки = все_строки(c, base="content", f=что["f"], sort=что["sort"], limit=200,
                            **{к: v for к, v in свод["window"].items() if v is not None})
        if not свод["more"]:
            assert {с["id"] for с in строки} == {з["job_id"] for з in свод["items"]}, вид
        if что["sort"]:
            # Сверху та же запись, что в таблице раздела, — или равная ей
            # по величине: второй ключ порядка у раздела свой.
            assert что["value"], вид
            величина = что["sort"].split(":")[0]
            колонка = drill.ЧИСЛА[величина][0].split(".")[1]
            assert строки[0]["value"] == pytest.approx(свод["items"][0][колонка]), вид
    assert с_данными >= 20 and длинных >= 2, (с_данными, длинных)


def test_предел_выдачи_что_послушать_прежний(c):
    """Лишняя строка, по которой видно «есть ещё», наружу не уходит."""
    свод = _свод(c, "records", kind="negative", period="all", limit=7)
    assert len(свод["items"]) == 7 and свод["more"] is True
    весь = _свод(c, "records", kind="mixed", period="all", limit=200)
    assert 0 < len(весь["items"]) < 200 and весь["more"] is False


# ---------------------------------------------------------------------------
# Темы без правил
# ---------------------------------------------------------------------------


def test_кучка_раскрывается_в_записи_которые_отдал_сам_отчёт(c):
    """Условия SQL у кучки нет: она собирается по похожести в памяти
    запроса. Раскрываются номера, которые отдал отчёт, — их и столько же."""
    целых = длинных = 0
    for период in ("day", "month"):
        свод = _свод(c, "discover", period=период, name="false")
        assert свод["topics"], период
        for тема in свод["topics"]:
            номера = тема["jobs"]
            assert 0 < len(номера) <= 10 and len(номера) == min(10, тема["size"])
            отбор = ["ids:" + ",".join(номера)]
            assert len(отбор[0]) <= 300
            строки = все_строки(c, f=отбор, limit=50,
                                **{к: v for к, v in свод["window"].items() if v is not None})
            assert {с["id"] for с in строки} == set(номера)
            assert всего(c, свод["window"], отбор) == len(номера)
            целых += тема["size"] <= 10
            длинных += тема["size"] > 10
    assert целых and длинных, (целых, длинных)


@pytest.mark.parametrize("перечень,сколько", [
    ("dr0001,dr0002", 2), (" dr0001 , dr0002,,", 2), ("dr0001,dr0001", 1), ("", 0),
    (",,,", 0), ("нет-такой", 0), ("dr0001' OR 1=1 --", 0), ('"],[', 0), ("%", 0),
])
def test_перечень_номеров_не_может_ни_сломать_запрос_ни_расширить_его(c, перечень, сколько):
    ответ = c.get("/api/drill", params={"f": [f"ids:{перечень}"], "limit": 5})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["total"] == сколько


# ---------------------------------------------------------------------------
# Чужого не показывать: те же числа под ключом владельца и подразделения
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def закрытый(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-content2-auth") / "data", auth=True,
                      ключи={"ah_admin": {"name": "админ", "role": "admin"},
                             "ah_anna": {"name": "анна", "role": "user"},
                             "ah_boris": {"name": "борис", "role": "user",
                                          "group": "продажи"},
                             "ah_vera": {"name": "вера", "role": "user",
                                         "group": "продажи"}})
    досеять(хранилище)
    yield хранилище
    хранилище.закрыть()


@pytest.mark.parametrize("ключ,владельцы", [("ah_anna", {"анна"}),
                                            ("ah_boris", {"борис", "вера"})])
def test_под_своим_ключом_число_раскрывается_только_в_свои_записи(закрытый, ключ, владельцы):
    with TestClient(закрытый.app, headers={"X-API-Key": ключ}) as свой, \
            TestClient(закрытый.app, headers={"X-API-Key": "ah_admin"}) as админ:
        операторы = _свод(свой, "agents", period="all", by="speaker")
        счёт: dict[str, int] = {}
        for строка in операторы["items"]:
            кто = _группа("speaker", строка["key"])
            _сверить_строку(свой, операторы["window"], кто, строка, ("agent_score",), счёт)
            чужие = {с["owner"] for с in все_строки(
                свой, base="content", f=кто, limit=200)} - владельцы
            assert not чужие, чужие
        всем = _свод(админ, "agents", period="all", by="speaker")
        assert sum(г["records"] for г in всем["items"]) > счёт["records"] > 0
        # Очередь коучинга и связи — тем же разрезом по владельцу.
        очередь = _свод(свой, "coaching", period="all", limit=5)
        assert всего(свой, очередь["window"], ["coaching", "coaching_open"],
                     base="content") == очередь["total"] > 0
        связи = _свод(свой, "correlations", period="all")
        assert всего(свой, связи["window"], [], base="content") == связи["sampled"] > 0
        # Темы: с разрезом по владельцу свод считает по завершённым и за всё
        # время — и говорит об этом.
        темы = _свод(свой, "topics", period="all")
        assert темы["completed_only"] is True and темы["items"]
        for тема in темы["items"][:12]:
            assert всего(свой, темы["window"], [f"stem:{тема['stem']}"],
                         base="content") == тема["records"]
        assert _свод(админ, "topics", period="all")["completed_only"] is False
        # Что послушать и кучки — тоже.
        отбор = _свод(свой, "records", kind="low_score", period="all", limit=200)
        assert not отбор["more"]
        assert всего(свой, отбор["window"], отбор["drill"]["f"],
                     base="content") == len(отбор["items"]) > 0
        чужая = next(с["id"] for с in все_строки(админ, limit=200)
                     if с["owner"] not in владельцы and с["owner"])
        assert всего(свой, None, [f"ids:{чужая}"]) == 0
        assert всего(админ, None, [f"ids:{чужая}"]) == 1
