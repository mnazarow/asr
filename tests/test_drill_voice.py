"""Заход 49: раскрытие чисел «Голосовой аналитики» и «Очереди LLM».

Голосовая аналитика — это то, что языковая модель поняла из разговоров:
причины, исходы, обязательства, трекеры, скоркарта. Свод считает это питоном
по ответам модели, список — запросом к базе; здесь сверяется, что одно равно
другому на каждом числе раздела, в том числе там, где они легче всего
расходятся: ответы со сбоем, пустые причина и исход, контрольные прогоны,
повторы одного трекера в ответе.

Очередь LLM считает не ответы, а строки очереди, и окно у неё по концу
разбора, а не по времени записи — у неё свои отборы и своя сверка.
"""
from __future__ import annotations

import pytest
from asrhub.llm import tasks
from drill_seed import Архив, всего
from fastapi.testclient import TestClient

#: С чего начинается любой список раздела: то же, чем свод отбирает записи.
ЗАПИСИ = ["status:completed", "no_control"]
РАЗБОР = [*ЗАПИСИ, "llm_ok"]

ПРИЧИНЫ = ("оплата: счёт", "доставка", "другое", "", None)
ИСХОДЫ = ("вопрос решён", "отказ клиента", "перезвонят или передано", None)


def _ответ(n: int) -> dict:
    """Ответ модели по записи — пёстрый, чтобы разрезы не совпадали."""
    if n % 8 == 5:
        сбой = {"error": "модель не ответила", "calls": 0}
        if n % 16 == 5:
            # Сбой, у которого поля всё же заполнены. Свод такие строки
            # выбрасывает целиком; список без `llm_ok` их бы показал.
            сбой.update(resolved=True, reason="доставка", outcome="вопрос решён",
                        actions=[{"what": "перезвонить"}],
                        trackers=[{"id": "жалоба", "label": "Жалоба", "fired": True}],
                        scorecard=[{"id": "greeting", "question": "Поздоровался",
                                    "answer": "да"}])
        return сбой
    трекеры = None
    if n % 6:
        трекеры = [
            {"id": "жалоба", "label": "Жалоба", "fired": n % 2 == 0, "quote": ""},
            {"id": "конкурент", "label": "Конкурент", "fired": n % 5 == 0, "quote": ""},
            {"id": "7", "label": "Отток", "fired": n % 4 == 3, "quote": ""}]
        if n % 7 == 3:
            # Два трекера с одним id в настройке — два элемента в ответе.
            трекеры.append({"id": "жалоба", "label": "Жалоба", "fired": True, "quote": ""})
    скоркарта = None
    if n % 5:
        скоркарта = [
            {"id": "greeting", "question": "Поздоровался",
             "answer": ("да", "нет", "н/п", "не знаю")[n % 4]},
            {"id": "item2", "question": "Назвал срок", "answer": ("да", "да", "нет")[n % 3]}]
        if n % 9 == 2:
            скоркарта.append({"id": "item2", "question": "Назвал срок",
                              "answer": ("да", "да", "нет")[n % 3]})
    действия = ([], None, [{"what": "прислать счёт", "who": "сотрудник"}],
                [{"what": "перезвонить"}, {"what": "уточнить срок"}])[n % 4]
    return {
        "summary": f"разговор {n}", "reason": ПРИЧИНЫ[n % 5], "outcome": ИСХОДЫ[n % 4],
        "resolved": (True, False, None)[n % 3], "actions": действия, "trackers": трекеры,
        "scorecard": скоркарта, "calls": 2, "latency_ms": 900.0 + n,
        "warnings": ["причина вне списка"] if n % 10 == 3 else None,
    }


def _досеять(хранилище: Архив) -> dict:
    """Ответы модели сверх шести от заглушки и строки очереди разбора."""
    db = хранилище.db
    сейчас = хранилище.данные["now"]
    свободные = [р["id"] for р in db.query(
        "SELECT j.id FROM jobs j WHERE j.status='completed' "
        "AND NOT EXISTS (SELECT 1 FROM llm_results l WHERE l.job_id = j.id) "
        "ORDER BY j.created_at DESC")]
    сырые: dict[str, dict] = {}
    for n, job_id in enumerate(свободные[:56]):
        поля = _ответ(n)
        # Один ответ — прежней версии подсказок: в «Разобрано» он входит.
        db.llm_save(job_id, tasks.VERSION - (1 if n == 9 else 0), model="stub", **поля)
        сырые[job_id] = поля
    # Контрольный прогон и запись, которую после разбора отправили на
    # повтор: ответ модели у обеих есть, в свод не входит ни та, ни другая.
    контроль, повтор = свободные[10], свободные[12]
    db.execute("UPDATE jobs SET source='control' WHERE id=?", (контроль,))
    db.execute("UPDATE jobs SET status='failed' WHERE id=?", (повтор,))
    for job_id in (контроль, повтор):
        сырые.pop(job_id)
    # Очередь: что закончено успешно, со сбоем, снято и что ещё ждёт.
    готовые = [р["id"] for р in db.query(
        "SELECT id FROM jobs WHERE status='completed' ORDER BY created_at DESC")]
    состояния = ("готово", "готово", "ошибка", "отменён", "ждёт", "готово", "ошибка")
    for n, job_id in enumerate(готовые[:49]):
        состояние = состояния[n % len(состояния)]
        кончено = None if состояние == "ждёт" else сейчас - n * 1700 - 5
        db.execute(
            "INSERT OR REPLACE INTO llm_queue (job_id, kind, state, priority, enqueued_at, "
            "started_at, finished_at, attempts, error, latency_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, "разбор", состояние, 50, сейчас - n * 1700 - 90,
             None if кончено is None else кончено - 20, кончено, 1,
             "модель не ответила" if состояние == "ошибка" else "",
             1500 if состояние == "готово" else None))
    return сырые


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-voice") / "data")
    хранилище.сырые = _досеять(хранилище)
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _свод(c, period="all"):
    ответ = c.get("/api/content/llm", params={"period": period})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _модель(c, окно, *отборы, сбой=False):
    """Длина списка записей с ответом модели — как его строит раздел."""
    return всего(c, окно, [*(ЗАПИСИ if сбой else РАЗБОР), *отборы], base="llm")


# ---------------------------------------------------------------------------
# Окно
# ---------------------------------------------------------------------------


def test_свод_отдаёт_окно_и_режет_временем_записи(c, архив):
    """Свод режет `j.created_at`, как и список; время ответа модели тут ни
    при чём — все ответы в этих данных записаны только что."""
    сутки, всё = _свод(c, "day"), _свод(c, "all")
    assert всё["window"]["since"] is None and всё["window"]["until"] > архив.данные["now"]
    assert сутки["window"]["until"] - сутки["window"]["since"] == pytest.approx(86400, abs=5)
    assert 0 < сутки["analyzed"] < всё["analyzed"]
    assert 0 < сутки["records"] < всё["records"]
    свежих = архив.db.query_one(
        "SELECT COUNT(*) AS n FROM llm_results WHERE created_at >= ?",
        (сутки["window"]["since"],))["n"]
    assert свежих > сутки["rows_total"], "ответы свежие все, а в окне суток — только часть"


# ---------------------------------------------------------------------------
# Шапка
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["day", "week", "all"])
def test_плитки_шапки_сходятся_со_списком(c, period):
    свод = _свод(c, period)
    окно = свод["window"]
    assert свод["records"] and всего(c, окно, ЗАПИСИ) == свод["records"]
    assert свод["analyzed"] and _модель(c, окно) == свод["analyzed"]
    # «Разобранные записи» считают и ответы со сбоем, плитка «Разобрано» — нет.
    assert _модель(c, окно, сбой=True) == свод["rows_total"]
    assert свод["errors"] and _модель(c, окно, "llm_failed", сбой=True) == свод["errors"]
    assert свод["rows_total"] == свод["analyzed"] + свод["errors"]
    # Доля решённых раскрывается числителем; знаменатель — где модель ответила.
    решено, известно = _модель(c, окно, "resolved:1"), _модель(c, окно, "resolved_known")
    assert решено and известно > решено
    assert свод["resolved_share"] == round(100.0 * решено / известно, 1)
    assert _модель(c, окно, "resolved:1") + _модель(c, окно, "resolved:0") == известно
    assert свод["records_with_actions"] and \
        _модель(c, окно, "with_actions") == свод["records_with_actions"]
    # Обещаний больше, чем разговоров с ними: это число записями не раскрыть.
    assert свод["actions"] > свод["records_with_actions"]
    assert _модель(c, окно, "off_list") == свод["off_list"] > 0


def test_без_условий_свода_список_был_бы_длиннее(c):
    """Каждое из трёх условий держит свою запись: контрольный прогон,
    запись не в статусе «готово» и ответ со сбоем."""
    окно = _свод(c)["window"]
    точно = _модель(c, окно)
    assert всего(c, окно, ["no_control", "llm_ok"], base="llm") == точно + 1
    assert всего(c, окно, ["status:completed", "llm_ok"], base="llm") == точно + 1
    assert всего(c, окно, ЗАПИСИ, base="llm") > точно
    # Сбой с заполненными полями: без `llm_ok` он попал бы в «решено».
    assert всего(c, окно, [*ЗАПИСИ, "resolved:1"], base="llm") > _модель(c, окно, "resolved:1")


# ---------------------------------------------------------------------------
# Причины, исходы и клетки «причина × исход»
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("period", ["week", "all"])
def test_причины_и_исходы(c, period):
    свод = _свод(c, period)
    окно = свод["window"]
    assert len(свод["reasons"]) >= 3 and len(свод["outcomes"]) >= 3
    for причина in свод["reasons"]:
        assert причина["records"] and \
            _модель(c, окно, f"reason:{причина['key']}") == причина["records"], причина
    for исход in свод["outcomes"]:
        assert _модель(c, окно, f"outcome:{исход['key']}") == исход["records"], исход
    # Двоеточие в причине значение не режет.
    assert any(":" in п["key"] for п in свод["reasons"])
    # Пустые причина и исход в распределения не входят — и остаются записями.
    assert sum(п["records"] for п in свод["reasons"]) + _модель(c, окно, "reason:") \
        == свод["analyzed"]
    assert _модель(c, окно, "reason:") > 0 and _модель(c, окно, "outcome:") > 0


def test_клетки_причина_исход(c):
    свод = _свод(c)
    окно = свод["window"]
    assert len(свод["pairs"]) >= 8
    for пара in свод["pairs"]:
        отборы = [f"reason:{'' if пара['reason'] == '—' else пара['reason']}",
                  f"outcome:{'' if пара['outcome'] == '—' else пара['outcome']}"]
        assert пара["records"] and _модель(c, окно, *отборы) == пара["records"], пара
    assert sum(п["records"] for п in свод["pairs"]) == свод["analyzed"]


# ---------------------------------------------------------------------------
# Трекеры и скоркарта
# ---------------------------------------------------------------------------


def _в_записях(архив, поле, условие):
    """Независимый счёт: в скольких засеянных ответах без сбоя есть элемент."""
    return sum(1 for ответ in архив.сырые.values()
               if not ответ.get("error") and any(условие(э) for э in ответ.get(поле) or []))


@pytest.mark.parametrize("period", ["week", "all"])
def test_трекеры(c, period):
    свод = _свод(c, period)
    окно = свод["window"]
    assert {т["id"] for т in свод["trackers"]} == {"жалоба", "конкурент", "7"}
    for трекер in свод["trackers"]:
        assert трекер["fired"] and \
            _модель(c, окно, f"tracker:{трекер['id']}") == трекер["fired"], трекер
        assert _модель(c, окно, f"tracker_checked:{трекер['id']}") == трекер["checked"]
        assert трекер["checked"] > трекер["fired"]


def test_трекер_считается_в_записях_а_не_в_элементах(c, архив):
    """Два трекера с одним id — два элемента в ответе по записи. Свод считал
    элементы, список — записи: «сработал в 23 разговорах» над списком из 20."""
    свод = _свод(c)
    жалоба = next(т for т in свод["trackers"] if т["id"] == "жалоба")
    элементов = sum(1 for о in архив.сырые.values() if not о.get("error")
                    for т in о.get("trackers") or [] if т["id"] == "жалоба" and т["fired"])
    записей = _в_записях(архив, "trackers", lambda т: т["id"] == "жалоба" and т["fired"])
    assert элементов > записей == жалоба["fired"]
    assert жалоба["checked"] == _в_записях(архив, "trackers", lambda т: т["id"] == "жалоба")


@pytest.mark.parametrize("period", ["week", "all"])
def test_скоркарта(c, архив, period):
    свод = _свод(c, period)
    окно = свод["window"]
    assert {п["id"] for п in свод["scorecard"]} == {"greeting", "item2"}
    for пункт in свод["scorecard"]:
        for ответ, отбор in (("да", "score_yes"), ("нет", "score_no"), ("н/п", "score_na")):
            assert _модель(c, окно, f"{отбор}:{пункт['id']}") == пункт[ответ], (пункт, ответ)
        assert пункт["да"] and пункт["нет"]
    # «Не знаю» — не «да» и не «нет»: и свод, и список кладут его в «н/п».
    приветствие = next(п for п in свод["scorecard"] if п["id"] == "greeting")
    assert приветствие["н/п"] > 0
    if period == "all":
        assert приветствие["н/п"] == _в_записях(
            архив, "scorecard",
            lambda о: о["id"] == "greeting" and о["answer"] not in ("да", "нет"))
        assert _в_записях(архив, "scorecard", lambda о: о["answer"] == "не знаю") > 0


def test_пункт_скоркарты_считается_в_записях(c, архив):
    свод = _свод(c)
    срок = next(п for п in свод["scorecard"] if п["id"] == "item2")
    элементов = sum(1 for о in архив.сырые.values() if not о.get("error")
                    for п in о.get("scorecard") or [] if п["id"] == "item2")
    assert элементов > срок["да"] + срок["нет"] + срок["н/п"] == _в_записях(
        архив, "scorecard", lambda п: п["id"] == "item2")


# ---------------------------------------------------------------------------
# Очередь LLM
# ---------------------------------------------------------------------------


def _очередь(c, часов=24):
    ответ = c.get("/api/llm/queue", params={"hours": часов, "limit": 100})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _промежуток(вид, от, до):
    # repr — кратчайшая запись, которая читается обратно в то же число:
    # так же пишет границы JavaScript.
    return [f"llmq_{вид}:{от!r}..{до!r}"]


@pytest.mark.parametrize("часов", [6, 24])
def test_плитки_очереди_раскрываются_строками_очереди(c, часов):
    свод = _очередь(c, часов)["stats"]
    от, до = свод["since"], свод["until"]
    assert до - от == pytest.approx(часов * 3600, abs=1)
    assert свод["ok"] and всего(c, None, _промежуток("ok", от, до)) == свод["ok"]
    assert свод["failed"] and всего(c, None, _промежуток("failed", от, до)) == свод["failed"]
    assert всего(c, None, _промежуток("done", от, до)) == свод["ok"] + свод["failed"]
    # В `n` сервер считает и снятые с очереди: плитка «Разобрано за окно»
    # показывает успешные плюс сбойные, а не `n`.
    assert свод["n"] > свод["ok"] + свод["failed"]


def test_окно_очереди_по_концу_разбора_а_не_по_времени_записи(c, архив):
    """Запись четырёхдневной давности, разобранная час назад, — в числе «за
    шесть часов». Общее окно списка (время записи) её бы отрезало."""
    свод = _очередь(c, 6)["stats"]
    от = свод["since"]
    старая = архив.db.query_one(
        "SELECT id FROM jobs WHERE status='completed' AND created_at < ? "
        "ORDER BY created_at LIMIT 1", (от,))["id"]
    архив.db.execute(
        "INSERT OR REPLACE INTO llm_queue (job_id, kind, state, priority, enqueued_at, "
        "started_at, finished_at, attempts, error) VALUES (?,?,?,?,?,?,?,?,?)",
        (старая, "архив", "ошибка", 30, от + 10, от + 20, от + 30, 3, "модель не ответила"))
    try:
        теперь = _очередь(c, 6)["stats"]
        assert теперь["failed"] == свод["failed"] + 1
        окно = [теперь["since"], теперь["until"]]
        assert всего(c, None, _промежуток("failed", *окно)) == теперь["failed"]
        assert всего(c, {"since": окно[0], "until": окно[1]},
                     _промежуток("failed", *окно)) < теперь["failed"]
    finally:
        архив.db.execute("DELETE FROM llm_queue WHERE job_id=?", (старая,))


def test_сбой_очереди_не_то_же_что_ответ_со_сбоем(c):
    """Сбой поверх прежнего удачного ответа ответ не затирает: в очереди
    «ошибка», а в ответах модели сбоя нет. Потому у очереди свой отбор."""
    свод = _очередь(c, 24)["stats"]
    от, до = свод["since"], свод["until"]
    в_очереди = всего(c, None, _промежуток("failed", от, до))
    из_них_со_сбоем_в_ответе = всего(c, None, [*_промежуток("failed", от, до), "llm_failed"])
    assert в_очереди == свод["failed"] > из_них_со_сбоем_в_ответе


def test_столбцы_графика_очереди(c):
    ответ = _очередь(c, 24)
    ряд, свод = ответ["series"], ответ["stats"]
    начало, шаг = ряд["since"], ряд["bucket_seconds"]
    assert len(ряд["rows"]) >= 10
    for корзина in ряд["rows"]:
        от = начало + корзина["bucket"] * шаг
        до = начало + (корзина["bucket"] + 1) * шаг
        assert всего(c, None, _промежуток("ok", от, до)) == корзина["ok"], корзина
        assert всего(c, None, _промежуток("failed", от, до)) == корзина["failed"], корзина
    assert sum(к["ok"] for к in ряд["rows"]) == свод["ok"]
    assert sum(к["failed"] for к in ряд["rows"]) == свод["failed"]
    # «Всё минус сбои» записывало в успешные и снятые с очереди записи.
    assert sum(к["n"] - к["failed"] for к in ряд["rows"]) > свод["ok"]


def test_список_очереди_идёт_от_очереди_а_не_перебирает_архив(архив):
    """Три сбоя за сутки при архиве в миллион записей: список обязан идти от
    строк очереди по первичному ключу заданий, а не читать архив целиком."""
    from asrhub import drill

    условия, параметры, _ = drill.разобрать(["llmq_failed:0..9999999999"], drill.ОТБОРЫ)
    план = " ".join(str(р["detail"]) for р in архив.db.query(
        "EXPLAIN QUERY PLAN SELECT j.id FROM jobs j WHERE " + " AND ".join(условия)
        + " ORDER BY j.created_at DESC, j.id DESC LIMIT 51", параметры))
    assert "SCAN j" not in план and "SCAN jobs" not in план, план
    assert "PRIMARY KEY" in план or "sqlite_autoindex_jobs" in план, план


def test_список_голосовой_аналитики_идёт_по_индексу_времени(архив, monkeypatch):
    """Каждый список раздела начинается с «задание готово»: это условие не
    должно уводить запрос на индекс по статусу — он под «готово» отдаёт
    почти весь архив, и страница ждала бы его сортировки."""
    from asrhub import drill

    пойманы: list[tuple[str, list]] = []
    настоящий = архив.db.query_bounded

    def подсмотреть(sql, params=(), **прочее):
        пойманы.append((sql, list(params)))
        return настоящий(sql, params, **прочее)

    monkeypatch.setattr(архив.db, "query_bounded", подсмотреть)
    for отборы in ([], ["reason:доставка"], ["tracker:жалоба"]):
        пойманы.clear()
        drill.Drill(архив.db).page(base="llm", since=0.0, until=2.0,
                                   filters=[*РАЗБОР, *отборы])
        sql, аргументы = пойманы[0]
        план = " | ".join(str(р["detail"]) for р in архив.db.query(
            "EXPLAIN QUERY PLAN " + sql, аргументы))
        assert "idx_jobs_created" in план and "idx_jobs_status" not in план, план
        assert "USE TEMP B-TREE FOR ORDER BY" not in план, план
    # Редкому статусу индекс по статусу, наоборот, оставлен.
    пойманы.clear()
    drill.Drill(архив.db).page(since=1.0, until=2.0, filters=["status:failed"])
    план = " | ".join(str(р["detail"]) for р in архив.db.query(
        "EXPLAIN QUERY PLAN " + пойманы[0][0], пойманы[0][1]))
    assert "idx_jobs_status" in план, план


# ---------------------------------------------------------------------------
# Разметка: раздел раскрывает тем же, что сверено выше
# ---------------------------------------------------------------------------


def test_интерфейс_раскрывает_теми_же_отборами():
    from pathlib import Path

    код = (Path(__file__).resolve().parents[1] / "server/asrhub/web/app.js").read_text("utf-8")
    голос = код.split("RENDERERS.voice = {", 1)[1].split("\n};\n", 1)[0]
    assert "ЗАПИСИ: ['status:completed', 'no_control']," in голос
    assert "f: [...this.ЗАПИСИ, ...(сбой ? [] : ['llm_ok']), ...(отборы || [])]," in голос
    for отбор in ("['resolved:1']", "['with_actions']", "[`reason:${причина.label}`]",
                  "[`outcome:${исход.label}`]", "[`reason:${п}`, `outcome:${и}`]",
                  "[`tracker:${трекер.id}`]", "[`score_yes:${пункт}`]",
                  "[`score_no:${пункт}`]", "[`score_na:${пункт}`]", "['llm_failed']"):
        assert отбор in голос, отбор
    очередь = код.split("RENDERERS.llmqueue = {", 1)[1].split("\n};\n", 1)[0]
    assert "f: [`llmq_${вид}:${от}..${до}`]" in очередь
    assert "this.раскрыть(вид, свод.since, свод.until, заголовок)" in очередь
    # Состояние очереди, скорость и настройки не раскрываются.
    for плитка in ("'Ждут разбора'", "'Среднее время ответа'", "'Среднее ожидание'",
                   "'Идёт сейчас'"):
        строка = очередь.split(f"kpi({плитка}", 1)[1].split("${kpi(", 1)[0].split("</div>", 1)[0]
        assert "число(" not in строка and "Drill" not in строка, плитка
