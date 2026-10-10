"""Заход 49: раскрытие в «Трендах» — точка ряда, число свода и клетка карты.

Раздел показывает полсотни показателей по корзинам времени. За каждым,
кроме замеров железа и контрольных прогонов, стоят записи, и теперь их
можно открыть списком. Здесь сверяется главное: список — ровно те записи,
из которых сложилось значение. Не «примерно те» и не «за похожее окно»:

- у каждого показателя в каждой корзине записей в списке столько, сколько
  насчитал сам запрос ряда (`n`), и сумма по корзинам равна своду за окно;
- значение, пересчитанное каталогом по записям списка, равно значению ряда;
- запись ровно на границе корзины лежит в той корзине, куда её положил ряд;
- клетка карты «час × день недели» раскрывается в свои записи за весь период.

Время в модуле остановлено: окно трендов скользит от «сейчас», а проверке
границ нужны записи точно на границах.
"""
from __future__ import annotations

import math
import time
from types import SimpleNamespace
from typing import Any

import pytest
from asrhub import drill, trends
from asrhub.trends import PERIODS, ИСТОЧНИКИ_ПО_ID, ПО_ID, ПОКАЗАТЕЛИ, РАСКРЫТИЯ
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

ЧАС = 3600.0
СУТКИ = 86400.0

#: Период и шаг: по часам, по суткам и по неделям с неполной последней
#: корзиной (год — это 52 недели и сутки).
СРЕЗЫ = (("week", "auto"), ("month", "auto"), ("year", "auto"))
#: Ещё срезы — для нескольких показателей: шаг, не делящий период нацело.
РЕДКИЕ_СРЕЗЫ = (("quarter", "week"), ("year", "month"), ("month", "week"), ("day", "auto"))
ПРИМЕРЫ = ("jobs", "rtf", "negative_share", "llm_analyzed", "audio_hours")

#: Во что показатель переводит величину записи: часы из секунд, проценты из доли.
МНОЖИТЕЛЬ = {"audio_hours": 1 / 3600, "processing_hours": 1 / 3600, "clipping": 100,
             "compliance": 100, "talk_share": 100, "silence": 100, "llm_latency": 1 / 1000}

#: Знаменатель доли — тем же списком, где он им выражается. `None` — все
#: записи с разбором: такого отбора у списка нет, считаем запросом.
ЗНАМЕНАТЕЛЬ: dict[str, tuple[str, list[str]] | None] = {
    "cached_share": ("jobs", []), "success_rate": ("jobs", []),
    "low_confidence": ("jobs", ["has:confidence"]), "suspect_share": ("jobs", ["assessed"]),
    "bad_audio": ("jobs", ["has:snr"]),
    "negative_share": ("jobs", ["has:sentiment"]), "positive_share": ("jobs", ["has:sentiment"]),
    "profanity": ("jobs", ["has:profanity"]),
    "alert_records": None, "frustrated": None, "repeat": None, "mono_share": None,
    "violations": None,
    "llm_resolved": ("llm", ["resolved_known"]), "llm_actions": ("llm", ["llm_ok"]),
    "llm_off_list": ("llm", ["llm_ok"]),
}
#: Доли не записей, а штук: числитель и знаменатель — суммы величины.
ДОЛЯ_СУММ = {"commitments_dated": (["nz:commitments"], "commitments:desc"),
             "objections_unhandled": (["nz:objections"], "objections:desc")}


# ---------------------------------------------------------------------------
# Данные
# ---------------------------------------------------------------------------


def _досеять(хранилище: Архив, сейчас: float) -> dict[str, Any]:
    """Поверх общего архива — то, чего трендам в нём не хватает.

    Общий архив — четверо суток по записи в час с четырьмя текстами: у
    половины показателей содержания в нём одни нули. Сверка числа со списком
    на нулях ничего не ловит, поэтому здесь — записи на год назад, записи
    точно на границах корзин и разбор с разными значениями в каждой колонке.
    """
    from asrhub.content_index import ContentIndex

    состояние = хранилище.app.state.hub
    db = хранилище.db
    граница = float(math.ceil(сейчас))            # от неё тренды отсчитывают начало окна
    тексты = ("Здравствуйте, договор готов, отправлю завтра до обеда. Спасибо!",
              "Это безобразие, сколько можно ждать. Буду жаловаться в суд.",
              "Подскажите, когда доставка? Мне обещали вчера. Перезвоню позже.")

    def запись(номер: str, когда: float, n: int) -> None:
        db.create_job({"id": номер, "filename": f"{номер}.wav", "owner": ("анна", "борис", "")[n % 3],
                       "engine": "demo", "model": "demo-simulator", "language": "ru",
                       "source": "api", "media_duration_s": 30.0 + (n * 53) % 600})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, номер))
        if n % 9 == 4:
            db.update_job(номер, status="failed", finished_at=когда + 3,
                          error_code="audio_decode_failed", error_message="сбой", retries=1 + n % 2)
            return
        текст = тексты[n % len(тексты)]
        db.update_job(
            номер, status="completed", text=текст, finished_at=когда + 20,
            words_count=len(текст.split()), segments_count=2, rtf=0.04 + (n % 11) * 0.02,
            processing_time_s=2.0 + n % 9, queue_time_s=float((n * 29) % 500),
            avg_confidence=0.55 + (n % 9) * 0.05, speakers_count=1 + n % 3,
            snr_db=4.0 + (n % 7) * 4, clipping_share=(0.0, 0.004, 0.03)[n % 3],
            loudness_lufs=-32.0 + n % 12, cached_from="dr0000" if n % 8 == 3 else "",
            quality_flags=("", "repeat", "")[n % 3])
        db.save_segments(номер, [
            {"start": i * 9.0, "end": i * 9.0 + 7.0, "text": кусок, "speaker": f"SPEAKER_{i % 2:02d}"}
            for i, кусок in enumerate(к for к in текст.split(". ") if к)])

    # Год назад и ближе — неровным шагом, чтобы корзины по неделям и по
    # месяцам были и пустыми, и с несколькими записями.
    for n in range(70):
        запись(f"tr{n:03d}", сейчас - (4.6 + n * 4.87) * СУТКИ - (n * 7919) % 80000, n)

    # Записи точно на границах. Корзины идут от начала окна, а оно — целая
    # секунда, отстоящая от `граница` на целое число суток: значит, граница
    # корзины по часам — `граница − k часов`, по суткам — `граница − k суток`.
    края: dict[str, float] = {}
    for k, часов in enumerate((1, 5, 24, 48, 7 * 24, 30 * 24)):
        ровно = граница - часов * ЧАС
        края[f"tb{k}a"] = ровно                               # первая секунда корзины
        края[f"tb{k}b"] = math.nextafter(ровно, 0.0)          # последний миг предыдущей
    края["tbend"] = сейчас                                    # конец окна: уже не входит
    края["tblast"] = math.nextafter(сейчас, 0.0)              # последний миг окна
    for n, (номер, когда) in enumerate(края.items()):
        запись(номер, когда, n * 9)                           # n*9 — никогда не «упавшая»

    ContentIndex(db, состояние.settings).backfill_once(limit=1000)

    # Разбор: в каждой колонке — и пусто, и ноль, и разные значения.
    разобранные = [р["job_id"] for р in db.query("SELECT job_id FROM content ORDER BY job_id")]
    for n, номер in enumerate(разобранные):
        обязательств = n % 3
        возражений = n % 4
        db.execute(
            "UPDATE content SET sentiment=?, alerts=?, questions=?, commitments=?, "
            "commitments_dated=?, objections=?, objections_unhandled=?, frustration=?, "
            "repeat_contact=?, profanity=?, speakers=?, agent_score=?, empathy=?, compliance=?, "
            "violations=?, wpm=?, talk_share=?, silence_share=?, interruptions=?, monologue_s=?, "
            "reply_delay_s=? WHERE job_id=?",
            (None if n % 10 == 9 else round(-0.9 + (n * 7 % 19) / 10, 3),
             2 if n % 5 == 0 else 0, n % 4, обязательств, min(обязательств, n % 2),
             возражений, возражений // 2, 1 if n % 7 == 2 else 0, 1 if n % 5 == 1 else 0,
             None if n % 3 == 0 else (0 if n % 3 == 1 else 1 + n % 4),
             1 if n % 6 == 0 else 2, None if n % 8 == 7 else 30.0 + (n * 7) % 70,
             None if n % 9 == 8 else float(-60 + (n * 13) % 160),
             None if n % 7 == 6 else ((n * 3) % 11) / 10, 2 if n % 5 == 0 else 0,
             None if n % 12 == 11 else 90 + (n * 11) % 120,
             None if n % 13 == 12 else round(0.3 + (n % 6) / 10, 2),
             None if n % 11 == 10 else round((n % 9) / 20, 2),
             None if n % 9 == 4 else n % 4, None if n % 14 == 13 else 10.0 + (n * 17) % 200,
             None if n % 4 == 0 else round(0.3 + n % 5, 1), номер))

    # Ответы модели — и для старых записей тоже, с разными исходами.
    for номер in [р["id"] for р in db.query(
            "SELECT id FROM jobs WHERE status='completed' AND id LIKE 'tr%' ORDER BY id")][::2]:
        состояние.llm_worker.analyze_job(номер, force=True)
    for n, номер in enumerate(р["job_id"] for р in db.query(
            "SELECT job_id FROM llm_results ORDER BY job_id")):
        db.execute(
            "UPDATE llm_results SET error=?, warnings=?, resolved=?, actions=?, latency_ms=? "
            "WHERE job_id=?",
            ("сбой модели" if n % 7 == 3 else ("" if n % 2 else None),
             "причина вне списка" if n % 5 == 2 else None, (1, 0, None)[n % 3],
             ("[]", "", '[{"what": "перезвонить"}]', None)[n % 4],
             None if n % 6 == 5 else 100.0 + n * 13, номер))

    # Эталоны: счётчики слов и доли ошибок пишутся вместе, как в конвейере.
    готовые = [р["id"] for р in db.query(
        "SELECT id FROM jobs WHERE status='completed' ORDER BY id")]
    for n, номер in enumerate(готовые):
        if n % 6 == 1:
            слов, s, d, i = 20 + n, n % 5, n % 3, n % 2
            db.execute("UPDATE jobs SET ref_words=?, sub_words=?, del_words=?, ins_words=?, "
                       "wer=?, mer=? WHERE id=?",
                       (слов, s, d, i, round((s + d + i) / слов, 6),
                        round((s + d + i) / (слов + i), 6), номер))
        if n % 6 == 3:
            # Запись, оценённая до того, как появились счётчики слов: доля
            # ошибок у неё есть, а в WER «по словам» она не входит.
            db.execute("UPDATE jobs SET wer=0.25, mer=0.2 WHERE id=?", (номер,))
        if n % 10 == 4:
            db.execute("UPDATE jobs SET speakers_count=NULL, snr_db=NULL WHERE id=?", (номер,))
        if n % 14 == 6:
            db.execute("UPDATE jobs SET media_duration_s=0 WHERE id=?", (номер,))
        if n % 9 == 2:
            # Готово, а времени работы нет: в ускорении звук такой записи
            # есть, и из списка завершённых она выпадать не должна.
            db.execute("UPDATE jobs SET processing_time_s=NULL WHERE id=?", (номер,))

    # Запись, отправленная на повтор: разбор и ответ модели у неё остались,
    # а статус уже не «готово». Источник трендов статус не смотрит.
    повтор = [р["job_id"] for р in db.query(
        "SELECT c.job_id FROM content c JOIN llm_results l ON l.job_id = c.job_id "
        "ORDER BY c.job_id LIMIT 4")]
    for номер, статус in zip(повтор, ("queued", "failed", "running", "queued"), strict=True):
        db.execute("UPDATE jobs SET status=? WHERE id=?", (статус, номер))

    # Контрольные прогоны: у них своё время, и раскрытия у них нет.
    for n, номер in enumerate(готовые[:6]):
        db.execute("INSERT INTO model_checks (job_id, check_job_id, model, control_model, wer, "
                   "mer, words, created_at) VALUES (?,?,?,?,?,?,?,?)",
                   (номер, f"chk{n}", "demo-simulator", "whisper-large", 0.1 * n, 0.1 * n, 40,
                    сейчас - 100 - n * 5000))
    return {"края": края, "граница": граница, "повтор": повтор}


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    сейчас = math.floor(time.time()) + 0.37
    хранилище = Архив(tmp_path_factory.mktemp("drill-trends") / "data", с_моделью=40,
                      сейчас=сейчас)
    хранилище.сейчас = сейчас
    хранилище.досеяно = _досеять(хранилище, сейчас)
    # Останавливаем часы трендов — и только их: подменяется имя `time` в
    # самом модуле, а не функция во всём процессе.
    заплатка = pytest.MonkeyPatch()
    заплатка.setattr(trends, "time", SimpleNamespace(time=lambda: сейчас))
    yield хранилище
    заплатка.undo()
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


def _ряды(клиент: Any, period: str, bucket: str = "auto", **прочее: Any) -> dict[str, Any]:
    ответ = клиент.get("/api/trends", params={"period": period, "bucket": bucket,
                                              "compare": "false", **прочее})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _карта(клиент: Any, показатель: str, period: str) -> dict[str, Any]:
    ответ = клиент.get("/api/trends/heatmap", params={"metric": показатель, "period": period})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _по_id(строки: list[dict[str, Any]], показатель: str) -> dict[str, Any]:
    return next(с for с in строки if с["id"] == показатель)


def _корзина(свод: dict[str, Any], i: int) -> dict[str, float]:
    """Границы корзины — той же арифметикой, что в интерфейсе (`корзина` в app.js)."""
    начало = свод["buckets"][i]
    return {"since": начало, "until": min(начало + свод["step_s"], свод["window"]["until"])}


def _сколько(клиент: Any, описание: dict[str, Any], окно: dict[str, Any],
             ещё: tuple[str, ...] = ()) -> int:
    """Длина списка под числом — и в том порядке, в каком он откроется, и по
    времени: переключатель порядка в списке не должен менять его состав."""
    отборы = [*описание["f"], *ещё]
    по_времени = всего(клиент, окно, отборы, описание["base"])
    if описание.get("sort"):
        assert всего(клиент, окно, отборы, описание["base"],
                     sort=описание["sort"]) == по_времени, описание
    return по_времени


def _список(клиент: Any, описание: dict[str, Any], окно: dict[str, Any],
            **прочее: Any) -> list[dict[str, Any]]:
    параметры = {"base": описание["base"], "f": описание["f"], "limit": 200,
                 "since": окно["since"], "until": окно["until"], **прочее}
    if описание.get("sort") and "sort" not in прочее:
        параметры["sort"] = описание["sort"]
    return все_строки(клиент, **параметры)


def _выборка(номера: list[int], сколько: int = 8) -> list[int]:
    """Несколько корзин из многих: края обязательно, середина — через одну."""
    if len(номера) <= сколько:
        return номера
    шаг = max(1, len(номера) // (сколько - 4))
    return sorted({*номера[:2], *номера[-2:], *номера[2:-2:шаг]})


# ---------------------------------------------------------------------------
# Каталог раскрытий
# ---------------------------------------------------------------------------


def test_раскрывается_всё_что_про_записи_и_ничего_сверх():
    """Показатель либо раскрывается, либо за ним нет записей с временем записи."""
    без_списка = {п.id: п.source for п in ПОКАЗАТЕЛИ if п.id not in РАСКРЫТИЯ}
    assert set(без_списка.values()) == {"checks", "system", "gpu"}
    assert {п.source for п in ПОКАЗАТЕЛИ if п.id in РАСКРЫТИЯ} == {"jobs", "content", "llm"}
    assert len(РАСКРЫТИЯ) == 53 and not set(РАСКРЫТИЯ) - set(ПО_ID)
    # Источники раскрываемых показателей режут окно по времени записи — по
    # тому же, по которому его режет список. У прогонов время своё.
    for показатель in РАСКРЫТИЯ:
        assert ИСТОЧНИКИ_ПО_ID[ПО_ID[показатель].source].ts == "j.created_at"
    assert ИСТОЧНИКИ_ПО_ID["checks"].ts != "j.created_at"


@pytest.mark.parametrize("показатель", sorted(РАСКРЫТИЯ))
def test_описание_раскрытия_собрано_из_существующего(показатель):
    р = РАСКРЫТИЯ[показатель]
    источник = ИСТОЧНИКИ_ПО_ID[ПО_ID[показатель].source]
    assert р.n in источник.cols, "счётчик записей — колонка своего источника"
    assert р.title and р.вид in ("счёт", "сумма", "среднее", "доля", "отношение")
    _, _, нужно = drill.разобрать(list(р.f), drill.ОТБОРЫ)
    величина = р.sort.split(":")[0]
    if величина:
        assert величина in drill.ЧИСЛА and р.value, "упорядочен — значит, со столбцом"
        нужно = нужно | drill.ЧИСЛА[величина][1]
    # Соединения списка — ровно те, что у источника показателя. Основа
    # `content` запрещена: она добавляет статус «готово», а тренды — нет.
    assert р.base == ("llm" if источник.id == "llm" else "jobs")
    assert drill.ОСНОВЫ[р.base] | нужно == {"jobs": set(), "content": {"c"},
                                           "llm": {"l"}}[источник.id]
    if р.вид in ("сумма", "среднее"):
        assert р.sort, "за суммой и средним — величина записи, её и показываем"


def test_величины_трендов_в_перечне_те_же_что_складывает_каталог():
    """Список под «Со сроком» упорядочен по тому же выражению, что в сумме."""
    суммы = set(ИСТОЧНИКИ_ПО_ID["content"].cols.values())
    for имя in ("commitments_dated", "objections_unhandled"):
        # В перечне — сама колонка разбора; показатель складывает её же,
        # только пустое считает нулём. Под `nz:` разницы между ними нет.
        колонка, соединения = drill.ЧИСЛА[имя]
        assert (колонка, соединения) == (f"c.{имя}", drill.C)
        assert f"SUM(COALESCE({колонка},0))" in суммы
        assert any(р.sort.startswith(f"{имя}:") for р in РАСКРЫТИЯ.values())


def test_показатели_без_записей_отдают_пустое_описание(c):
    свод = _ряды(c, "week")
    for показатель in ("control_runs", "disagreement", "cpu", "gpu_util"):
        assert _по_id(свод["series"], показатель)["drill"] is None
        assert _по_id(свод["series"], показатель)["n"] is None
        assert _по_id(свод["summary"], показатель)["drill"] is None
        assert _по_id(свод["summary"], показатель)["n"] is None
        карта = _карта(c, показатель, "week")
        assert карта["drill"] is None and карта["n"] is None
    # Прогоны при этом есть — раскрытия нет не потому, что считать нечего.
    assert _по_id(свод["summary"], "control_runs")["total"] == 6


# ---------------------------------------------------------------------------
# Окно и корзины
# ---------------------------------------------------------------------------


def test_окно_отдаётся_как_есть_и_корзины_идут_от_целой_секунды(c, архив):
    for period, bucket in (*СРЕЗЫ, *РЕДКИЕ_СРЕЗЫ):
        свод = _ряды(c, period, bucket)
        окно = свод["window"]
        assert окно == {"since": свод["from"], "until": свод["to"]}
        assert окно["until"] == архив.сейчас, "конец окна — без округления"
        assert окно["since"] == архив.досеяно["граница"] - PERIODS[period]
        assert свод["buckets"][0] == окно["since"]
        assert all(граница == int(граница) for граница in свод["buckets"])
        # Корзины покрывают окно целиком и без перекрытий.
        края = [_корзина(свод, i) for i in range(len(свод["buckets"]))]
        assert all(а["until"] == б["since"] for а, б in zip(края, края[1:], strict=False))
        assert края[-1]["until"] == окно["until"]
    год = _ряды(c, "year")
    assert год["bucket"] == "week" and len(год["buckets"]) == 53
    последняя = _корзина(год, 52)
    assert 0 < последняя["until"] - последняя["since"] < год["step_s"], "последняя — неполная"


def test_запись_на_границе_корзины_лежит_там_же_где_в_ряду(c, архив):
    """Граница входит в свою корзину, последний миг перед ней — в предыдущую.

    Раньше начало окна было дробным, а в ответ уходило округлённым до
    десятой секунды: запись рядом с границей ряд считал в одной корзине, а
    список по границам из ответа показывал в соседней.
    """
    края = архив.досеяно["края"]
    описание = РАСКРЫТИЯ["jobs"].наружу()
    for period, bucket, пары in (("week", "auto", (0, 1, 2, 3)), ("month", "auto", (2, 3, 4)),
                                 ("year", "auto", (2,)), ("quarter", "week", ())):
        свод = _ряды(c, period, bucket)
        ряд = _по_id(свод["series"], "jobs")
        где: dict[str, int] = {}
        for i, записей in enumerate(ряд["n"]):
            номера = [с["id"] for с in _список(c, описание, _корзина(свод, i))] if записей else []
            assert len(номера) == записей == (ряд["values"][i] or 0), (period, i)
            где.update(dict.fromkeys(номера, i))
        for k in пары:
            assert где[f"tb{k}a"] == где[f"tb{k}b"] + 1, (period, k)
            assert свод["buckets"][где[f"tb{k}a"]] == края[f"tb{k}a"], "запись ровно на границе"
        # Конец окна не входит, последний миг перед ним — входит.
        assert "tbend" not in где and где["tblast"] == len(свод["buckets"]) - 1
    # Начало окна входит, миг до него — нет: ни в ряд, ни в список.
    неделя = _ряды(c, "week")
    все = {с["id"] for с in _список(c, описание, неделя["window"])}
    assert "tb4a" in все and "tb4b" not in все
    assert len(все) == _по_id(неделя["summary"], "jobs")["n"]
    месяц = {с["id"] for с in _список(c, описание, _ряды(c, "month")["window"])}
    assert {"tb4a", "tb4b", "tb5a"} <= месяц and "tb5b" not in месяц


# ---------------------------------------------------------------------------
# Число = список
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("показатель", sorted(РАСКРЫТИЯ))
def test_точка_ряда_и_число_свода_раскрываются_в_свои_записи(c, показатель):
    срезы = (*СРЕЗЫ, *РЕДКИЕ_СРЕЗЫ) if показатель in ПРИМЕРЫ else СРЕЗЫ
    for period, bucket in срезы:
        свод = _ряды(c, period, bucket)
        ряд, итог = _по_id(свод["series"], показатель), _по_id(свод["summary"], показатель)
        описание = ряд["drill"]
        assert описание == итог["drill"] == РАСКРЫТИЯ[показатель].наружу()
        n = ряд["n"]
        assert len(n) == len(свод["buckets"]) == len(ряд["values"])
        # Свод за окно — сумма корзин, и столько же записей в списке за окно.
        assert sum(n) == итог["n"] == _сколько(c, описание, свод["window"])
        assert итог["n"] > 0, "сверка на нуле ничего не ловит"
        непустые = [i for i, записей in enumerate(n) if записей]
        if period != "day":
            assert len(непустые) >= 2, "одна корзина — это не ряд"
        for i in _выборка(непустые):
            assert _сколько(c, описание, _корзина(свод, i)) == n[i], (period, i)
        пустые = [i for i, записей in enumerate(n) if not записей]
        for i in пустые[:1] + пустые[-1:]:
            assert _сколько(c, описание, _корзина(свод, i)) == 0, (period, i)
        # Плитка показывает последнюю непустую корзину — номер её отдан.
        значения = [i for i, v in enumerate(ряд["values"]) if v is not None]
        assert итог["last_at"] == (значения[-1] if значения else None)


def _по_записям(архив: Архив, показатель: str, номера: list[str]) -> float | None:
    """Значение показателя, пересчитанное каталогом по данным записям."""
    п = ПО_ID[показатель]
    источник = ИСТОЧНИКИ_ПО_ID[п.source]
    колонки = ", ".join(f"{выр} AS {имя}" for имя, выр in источник.cols.items())
    метки = ",".join("?" for _ in номера)
    строка = архив.db.query_one(
        f"SELECT {колонки} {источник.frm} WHERE j.id IN ({метки})", номера)
    return п.calc(dict(строка))


def _записей_с_разбором(архив: Архив, окно: dict[str, Any]) -> int:
    return int(архив.db.query_one(
        "SELECT COUNT(*) AS n FROM content c JOIN jobs j ON j.id = c.job_id "
        "WHERE j.created_at >= ? AND j.created_at < ?", (окно["since"], окно["until"]))["n"])


@pytest.mark.parametrize("показатель", sorted(РАСКРЫТИЯ))
def test_значение_показателя_сложено_из_записей_списка(c, архив, показатель):
    """Мало совпасть по числу строк: это должны быть те самые записи.

    Значение пересчитывается каталогом по номерам из списка и сравнивается
    с рядом. Лишняя или потерянная запись сдвинет и среднее, и сумму.
    """
    р = РАСКРЫТИЯ[показатель]
    п = ПО_ID[показатель]
    описание = р.наружу()
    свод = _ряды(c, "month")
    ряд, итог = _по_id(свод["series"], показатель), _по_id(свод["summary"], показатель)
    # Точка ряда округлена на два знака точнее показа, число свода — до показа.
    в_корзине, за_окно = 10 ** -(п.digits + 1), 0.51 * 10 ** -п.digits
    непустые = [i for i, записей in enumerate(ряд["n"]) if записей]
    окна = [(_корзина(свод, i), ряд["values"][i], в_корзине) for i in _выборка(непустые, 5)]
    # За всё окно: у счётных свод отдаёт сумму (`total`), у остальных — `avg`.
    окна.append((свод["window"],
                 итог["total"] if р.вид in ("счёт", "сумма") else итог["avg"], за_окно))
    for окно, ожидание, точность in окна:
        строки = _список(c, описание, окно)
        assert строки
        if р.вид == "доля" and показатель in ДОЛЯ_СУММ:
            отборы, порядок = ДОЛЯ_СУММ[показатель]
            низ = sum(с["value"] for с in _список(
                c, {"base": "jobs", "f": отборы}, окно, sort=порядок))
            assert sum(с["value"] for с in строки) / низ * 100 == pytest.approx(
                ожидание, abs=точность)
        elif р.вид == "доля":
            знаменатель = ЗНАМЕНАТЕЛЬ[показатель]
            низ = (_записей_с_разбором(архив, окно) if знаменатель is None
                   else всего(c, окно, знаменатель[1], знаменатель[0]))
            assert 0 < len(строки) <= низ
            assert len(строки) / низ * 100 == pytest.approx(ожидание, abs=точность)
        else:
            пересчёт = _по_записям(архив, показатель, [с["id"] for с in строки])
            assert пересчёт == pytest.approx(ожидание, abs=точность), окно
        if р.вид == "счёт":
            assert len(строки) == ожидание
        # Столбец величины — та самая величина, что складывается и усредняется.
        if р.вид in ("сумма", "среднее"):
            величины = [с["value"] for с in строки]
            assert None not in величины
            сложено = sum(величины) * МНОЖИТЕЛЬ.get(показатель, 1)
            if р.вид == "среднее":
                сложено /= len(величины)
            assert сложено == pytest.approx(ожидание, abs=точность), окно
            порядок = sorted(величины, reverse=р.sort.endswith("desc"))
            assert величины == порядок, "сверху — те, кто делает показатель"


def test_за_долей_раскрывается_числитель_а_не_всё_окно(c):
    """«Отрицательных 20 %» — это отрицательные, а не все оценённые."""
    свод = _ряды(c, "month")
    for показатель, шире in (("negative_share", ["has:sentiment"]), ("cached_share", []),
                             ("low_confidence", ["has:confidence"]), ("llm_resolved", [])):
        описание = _по_id(свод["summary"], показатель)["drill"]
        числитель = _сколько(c, описание, свод["window"])
        assert 0 < числитель < всего(c, свод["window"], шире, описание["base"]), показатель


def test_разбор_записи_отправленной_на_повтор_в_списке_есть(c, архив):
    """Источник трендов не смотрит на статус — и список под числом не должен.

    Основа «разобранные записи» добавила бы условие «готово», как в разделе
    «Аналитика записей», и список оказался бы короче числа.
    """
    свод = _ряды(c, "week")
    итог = _по_id(свод["summary"], "sentiment")
    повтор = set(архив.досеяно["повтор"])
    в_списке = {с["id"] for с in _список(c, итог["drill"], свод["window"])}
    assert повтор & в_списке, "в окне нет ни одной такой записи — проверять нечего"
    assert len(в_списке) == итог["n"]
    только_готовые = всего(c, свод["window"], итог["drill"]["f"], "content")
    assert только_готовые < итог["n"], "основа content сошлась бы с числом случайно"


# ---------------------------------------------------------------------------
# Карта «час × день недели»
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("показатель", sorted(РАСКРЫТИЯ))
def test_клетка_карты_раскрывается_в_записи_своего_часа_недели(c, архив, показатель):
    for period in ("week", "quarter"):
        карта = _карта(c, показатель, period)
        описание = карта["drill"]
        assert описание == РАСКРЫТИЯ[показатель].наружу()
        окно = карта["window"]
        assert окно == {"since": карта["from"], "until": карта["to"]}
        assert окно["until"] == архив.сейчас
        assert окно["since"] == архив.досеяно["граница"] - PERIODS[period]
        n = карта["n"]
        assert len(n) == 7 and all(len(строка) == 24 for строка in n)
        assert sum(map(sum, n)) == _сколько(c, описание, окно) > 0
        клетки = [(день, час) for день in range(7) for час in range(24) if n[день][час]]
        assert len(клетки) >= 2
        недель = max(1.0, PERIODS[period] / (7 * СУТКИ))
        for день, час in _выборка(клетки, 6):
            assert _сколько(c, описание, окно, (f"weekday:{день}", f"hour:{час}")) \
                == n[день][час], (period, день, час)
            if РАСКРЫТИЯ[показатель].вид == "счёт":
                # Клетка счётного показателя поделена на число недель, а в
                # списке — все записи периода: `n` это и говорит.
                assert карта["grid"][день][час] == pytest.approx(n[день][час] / недель, abs=0.01)
        пустые = [(день, час) for день in range(7) for час in range(24) if not n[день][час]]
        for день, час in пустые[:1]:
            assert _сколько(c, описание, окно, (f"weekday:{день}", f"hour:{час}")) == 0


def test_часы_карты_и_списка_одни_и_те_же(c):
    """Клетка считает час и день по часам сервера — и отбор списка тоже."""
    карта = _карта(c, "jobs", "week")
    описание = карта["drill"]
    for день in range(7):
        for час in (0, 9, 23):
            строки = _список(c, {**описание, "f": [f"weekday:{день}", f"hour:{час}"]},
                             карта["window"])
            assert len(строки) == карта["n"][день][час]
            for строка in строки:
                местное = time.localtime(строка["created_at"])
                assert (местное.tm_wday, местное.tm_hour) == (день, час)


# ---------------------------------------------------------------------------
# Чужого не показывать
# ---------------------------------------------------------------------------


def test_число_ключа_раскрывается_только_в_его_записи(tmp_path_factory, архив):
    """Свод режется по владельцу, и список под числом — тем же разрезом."""
    закрытый = Архив(tmp_path_factory.mktemp("drill-trends-auth") / "data", auth=True, заданий=48,
                     с_моделью=0, сейчас=архив.сейчас, ключи={
                         "ah_admin": {"name": "админ", "role": "admin"},
                         "ah_anna": {"name": "анна", "role": "user"}})
    try:
        числа = {}
        for ключ in ("ah_admin", "ah_anna"):
            with TestClient(закрытый.app, headers={"X-API-Key": ключ}) as клиент:
                свод = _ряды(клиент, "week")
                for показатель in ("jobs", "rtf", "sentiment"):
                    итог = _по_id(свод["summary"], показатель)
                    assert _сколько(клиент, итог["drill"], свод["window"]) == итог["n"] > 0
                    числа[ключ, показатель] = итог["n"]
                владельцы = {с["owner"] for с in _список(
                    клиент, _по_id(свод["summary"], "jobs")["drill"], свод["window"])}
                assert (владельцы == {"анна"}) == (ключ == "ah_anna")
                # Железо обычному ключу не отдаётся вовсе — и раскрывать нечего.
                assert ("cpu" in {с["id"] for с in свод["summary"]}) == (ключ == "ah_admin")
        assert числа["ah_anna", "jobs"] < числа["ah_admin", "jobs"]
    finally:
        закрытый.закрыть()
