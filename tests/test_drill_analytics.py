"""Заход 49, раздел «Аналитика»: число на экране равно списку под ним.

Раздел показывает больше сотни чисел: плитки, строки таблиц, точки рядов,
столбцы и клетки карт. Каждое теперь раскрывается в список заданий, и
условие списка написано руками — отдельно от свода, который это число
посчитал. Разойтись им легко: свод отбрасывает нули у RTF и уверенности, но
берёт нулевое ожидание; корзина времени считалась делением, а список режет
сравнением; «длиннее секунды» — строго. Здесь каждое подключённое число
сверяется со своим списком на архиве, куда нарочно досеяны записи ровно на
этих границах.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from asrhub.pipeline import metrics as M
from drill_seed import Архив, все_строки, всего
from fastapi.testclient import TestClient

ГОТОВЫЕ = "status:completed"


# ---------------------------------------------------------------------------
# Данные: общий архив и то, чего в нём для «Аналитики» не хватает
# ---------------------------------------------------------------------------


def _поставить(db: Any, job_id: str, **поля: Any) -> None:
    """Колонки задания напрямую: часть нужных значений `update_job` не пишет
    как есть (NULL вместо пустой строки, строка вместо JSON)."""
    db.execute(f"UPDATE jobs SET {', '.join(f'{к}=?' for к in поля)} WHERE id=?",
               [*поля.values(), job_id])


def досеять(db: Any, сейчас: float) -> None:
    """Добавляет к общему архиву то, по чему режет «Аналитика».

    Две части. Первая раскрашивает имеющиеся задания колонками, которых в
    общих данных нет: устройство, приоритет, эталон, калибровка, память.
    Вторая — записи на границах: уверенность ровно 1,0 и ровно 0,5, RTF
    ноль, ожидание ровно минута, длительность ровно секунда, имя файла с
    точкой в начале. На обычных данных список и число сходятся при почти
    любой ошибке в условии; расходятся они именно здесь.
    """
    корзины = [[4, 3.2, 3] for _ in range(10)]
    for n, строка in enumerate(db.query("SELECT id, status FROM jobs ORDER BY id")):
        job_id, готово = строка["id"], строка["status"] == "completed"
        _поставить(
            db, job_id,
            device=("cuda", "cpu", "")[n % 3],
            priority=(10, 50, 90, 0, None)[n % 5],
            webhook_status=("delivered", "failed", "", None)[n % 4],
            file_size=(100000 + n * 1000) if n % 6 else 0,
            peak_memory_jobs=(1, 2, 3, 0, None)[n % 5])
        if not готово:
            # Ожидание записано и у несостоявшихся: «Ожидание в очереди»
            # считает все статусы, а ряд по времени — только готовые.
            if n % 2:
                _поставить(db, job_id, queue_time_s=5.0 + n)
            continue
        _поставить(
            db, job_id,
            model_load_s=0.5 if n % 4 == 0 else 0.0,
            alignment_s=0.3 if n % 3 == 0 else None,
            silence_share=(0.05 + (n % 7) * 0.05) if n % 2 else None,
            suspect_segments=1 if n % 4 in (0, 2) else 0,
            suspect_share=0.5 if n % 4 in (0, 2) else 0.0)
        if n % 7 == 2:
            _поставить(db, job_id, quality_flags=None, suspect_segments=None,
                       suspect_share=None)
        if n % 4 == 0:
            _поставить(db, job_id, wer=0.05 * (n % 5), mer=0.04 * (n % 5),
                       ref_words=100 + n, sub_words=n % 5, del_words=n % 3, ins_words=n % 2)
        if n % 8 == 0:
            _поставить(db, job_id, calibration=json.dumps({"bins": корзины, "source": "word"}))
        if n % 12 == 6:
            _поставить(db, job_id, file_hash=("хеш-а", "", None)[(n // 12) % 3])

    готовое = {
        "status": "completed", "model": "demo-simulator", "engine": "demo",
        "owner": "анна", "language": "ru", "source": "web",
        "media_duration_s": 50.0, "segments_count": 4, "words_count": 40,
        "rtf": 0.2, "processing_time_s": 10.0, "queue_time_s": 3.0,
        "avg_confidence": 0.9, "file_size": 64000, "quality_flags": ""}
    края: list[dict[str, Any]] = [
        # Уверенность: границы корзин, ноль (плитка его не считает, ход — считает)
        # и ровно единица (верх последней корзины входит в неё).
        *({"avg_confidence": v} for v in (1.0, 0.0, 0.5, 0.7, 0.75, 0.85, 0.95, None)),
        # RTF: ноль и пусто в «средний RTF» не входят.
        {"rtf": 0.0}, {"rtf": None},
        # Ожидание: ноль — законное; ровно минута и ровно десять — ещё не «дольше».
        *({"queue_time_s": v} for v in (0.0, 60.0, 60.5, 600.0, 600.5, None)),
        # Длительность: границы корзин гистограммы и срезов, ноль, пусто и
        # ровно секунда — она не «длиннее секунды».
        *({"media_duration_s": v} for v in (1.0, 1.5, 0.0, None, 30.0, 60.0, 120.0, 300.0,
                                             1200.0, 3600.0, 7200.0, 9000.0)),
        # Звук: границы корзин SNR, клиппинг ровно на пороге, клиппинг без SNR.
        *({"snr_db": v, "clipping_share": 0.0, "loudness_lufs": -20.0}
          for v in (3.0, 5.0, 10.0, 20.0)),
        {"snr_db": 30.0, "clipping_share": 0.01}, {"snr_db": None, "clipping_share": 0.5},
        # Имена файлов, на которых «всё после последней точки» и «суффикс
        # пути» — разные вещи.
        *({"filename": имя} for имя in (".скрытый", "запись.", "каталог.x/файл", "ЗАПИСЬ.WAV",
                                        "архив.tar.gz", "безточки", None)),
        # Метки: повтор одной метки, одни запятые, пробел перед запятой.
        *({"tags": метки} for метки in ("срочно, срочно", " , ", "доставка ,срочно", "новая")),
        # Подозрительные: признаки без сегментов — вне основы карточки.
        {"segments_count": 0, "quality_flags": "repeat"},
        {"segments_count": 5, "quality_flags": "phrase,repeat", "suspect_segments": 3,
         "suspect_share": 0.6},
        # Калибровка: битая строка, пустая, корзины не того размера, не объект.
        *({"calibration": строка} for строка in ("не json", "", '{"bins": [[1, 0.5, 1]]}',
                                                 "[1, 2, 3]")),
        # Память: ноль и пусто в разрез не входят; «заданий разом» ноль — это одно.
        {"peak_memory_mb": 0.0}, {"peak_memory_mb": None},
        {"peak_memory_mb": 900.0, "peak_memory_jobs": 0},
        # Из кеша и без времени обработки — мимо латентности.
        {"cached_from": "dr0000", "processing_time_s": None, "file_hash": "хеш-б"},
        {"cached_from": "dr0001", "processing_time_s": 0.0, "file_hash": "хеш-б"},
        {"processing_time_s": None},
        # Не готовые: отказы без кода и без модели, отмены, пауза.
        {"status": "failed", "error_code": None, "model": "", "retries": 2, "rtf": None},
        {"status": "failed", "error_code": "", "retries": 0, "rtf": None},
        {"status": "cancelled", "cancelled_by": ""}, {"status": "cancelled", "cancelled_by": "admin"},
        {"status": "paused"}, {"status": "retry", "retries": 3},
        # Эталон у записи без длительности и с пустым источником и языком.
        {"wer": 0.0, "ref_words": 50, "media_duration_s": None, "source": "", "language": ""},
        {"wer": 1.4, "ref_words": 20, "sub_words": 10, "del_words": 2, "ins_words": 16,
         "media_duration_s": 4000.0, "source": None, "model": None},
    ]
    for n, отличие in enumerate(края):
        поля = {**готовое, "filename": f"край-{n}.wav", **отличие}
        job_id = db.create_job({"id": f"an{n:04d}", **поля})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (сейчас - n * 600 - 45, job_id))


@pytest.fixture(scope="module")
def архив(tmp_path_factory):
    хранилище = Архив(tmp_path_factory.mktemp("drill-analytics") / "data", с_моделью=0)
    досеять(хранилище.db, хранилище.данные["now"])
    yield хранилище
    хранилище.закрыть()


@pytest.fixture(scope="module")
def c(архив):
    with TestClient(архив.app) as клиент:
        yield клиент


@pytest.fixture(scope="module", params=["week", "day", "all"])
def отчёт(c, request):
    """Свод за неделю, за сутки и за всё время: у «всего времени» нет нижней
    границы окна, а у рядов по времени тогда свои границы."""
    ответ = c.get("/api/analytics", params={"period": request.param})
    assert ответ.status_code == 200, ответ.text
    return ответ.json()


def _строки(c, окно: dict[str, Any] | None, f: list[str], sort: str = "") -> list[dict[str, Any]]:
    границы = {к: v for к, v in (окно or {}).items() if v is not None}
    return все_строки(c, f=f, sort=sort, limit=200, **границы)


def _величины(c, окно, f: list[str], sort: str) -> list[float]:
    """Величины списка, упорядоченного по ним, — то, что встаёт столбцом."""
    return [float(с["value"]) for с in _строки(c, окно, f, sort)]


def _пусто(значение: Any, *заглушки: str) -> str:
    """То же, что `Drill.val` на экране: заглушка свода — пустое в базе."""
    return "" if значение in заглушки else str(значение)


def _диапазон(от: Any, до: Any) -> str:
    return f"{'' if от is None else от}..{'' if до is None else до}"


def _корзина(границы: list[float], номер: int) -> dict[str, float]:
    return {"since": границы[номер], "until": границы[номер + 1]}


def _среднее(значения: list[float], знаков: int) -> float | None:
    return round(sum(значения) / len(значения), знаков) if значения else None


def _неделя(отчёт: dict[str, Any]) -> bool:
    """За неделю в окне весь архив: там сверяемое число обязано быть не нулём,
    иначе сверка «0 = 0» ничего не ловит."""
    return отчёт["overview"]["period"] == "week"


# ---------------------------------------------------------------------------
# Плитки
# ---------------------------------------------------------------------------


def test_плитки_сводки(c, отчёт):
    окно, о = отчёт["window"], отчёт["overview"]
    assert всего(c, окно, [ГОТОВЫЕ]) == о["jobs"]["completed"]
    assert всего(c, окно, ["cached"]) == о["jobs"]["cached"]
    # «Аудио» и «слов» — суммы: сверяется сумма столбца величины.
    длительности = _величины(c, окно, [ГОТОВЫЕ, "nz:duration"], "duration:desc")
    assert sum(длительности) == pytest.approx(о["volume"]["audio_seconds"], abs=0.06)
    assert sum(_величины(c, окно, [ГОТОВЫЕ, "nz:words"], "words:desc")) == о["volume"]["words"]
    # Средние: список — ровно те задания, по которым оно посчитано.
    rtf = _величины(c, окно, [ГОТОВЫЕ, "nz:rtf"], "rtf:desc")
    assert len(rtf) == о["performance"]["rtf"]["count"]
    assert sum(rtf) / len(rtf) == pytest.approx(о["performance"]["rtf"]["avg"], abs=1e-6)
    уверенность = _величины(c, окно, [ГОТОВЫЕ, "nz:confidence"], "confidence:asc")
    assert len(уверенность) == о["quality"]["confidence"]["count"]
    assert всего(c, окно, [ГОТОВЫЕ, "nz:confidence", "low_confidence"]) \
        == о["quality"]["low_confidence_jobs"]
    # «Ускорение» — звук к машинному времени по готовым.
    обработка = _величины(c, окно, [ГОТОВЫЕ], "processing:desc")
    звук = sum(float(с["media_duration_s"] or 0) for с in _строки(c, окно, [ГОТОВЫЕ]))
    assert round(звук / sum(обработка), 2) == о["performance"]["speedup"]
    if _неделя(отчёт):
        assert о["jobs"]["cached"] and о["quality"]["low_confidence_jobs"]
        # Нули и пропуски досеяны: «не ноль» и «записано» обязаны различаться.
        assert len(rtf) < о["jobs"]["completed"]
        assert len(уверенность) < всего(c, окно, [ГОТОВЫЕ, "has:confidence"])


# ---------------------------------------------------------------------------
# Ряды по времени
# ---------------------------------------------------------------------------


def test_поток_и_скорость_по_корзинам_времени(c, отчёт):
    ряд = отчёт["timeseries"]
    края = ряд["edges"]
    assert len(края) == len(ряд["labels"]) + 1 and края[:-1] == ряд["labels"]
    assert края == sorted(края)
    сошлось = 0
    for номер in range(len(ряд["labels"])):
        окно = _корзина(края, номер)
        поток = ряд["completed"][номер] + ряд["failed"][номер]
        assert всего(c, окно, ["status:completed,failed"]) == поток, номер
        rtf = _величины(c, окно, [ГОТОВЫЕ, "nz:rtf"], "rtf:desc")
        assert _среднее(rtf, 4) == ряд["rtf"][номер], номер
        ожидание = _величины(c, окно, [ГОТОВЫЕ, "has:wait"], "wait:desc")
        assert _среднее(ожидание, 2) == ряд["queue_time"][номер], номер
        сошлось += поток
    # Корзины не пересекаются и ничего не теряют: в сумме — все готовые и
    # упавшие окна ряда (у «всего времени» оно своё — последняя неделя).
    assert сошлось == всего(c, {"since": края[0], "until": края[-1]},
                            ["status:completed,failed"])
    assert сошлось


def test_ход_качества_по_корзинам(c, отчёт):
    ход = отчёт["quality_trend"]
    края = ход["edges"]
    assert len(края) == len(ход["buckets"]) + 1 and края == sorted(края)
    низких = 0
    for номер in range(len(ход["buckets"])):
        окно = _корзина(края, номер)
        уверенность = _величины(c, окно, [ГОТОВЫЕ, "has:confidence"], "confidence:asc")
        assert len(уверенность) == ход["jobs"][номер], номер
        assert _среднее(уверенность, 4) == ход["confidence"][номер], номер
        доля = ход["low_confidence_share"][номер]
        сколько = всего(c, окно, [ГОТОВЫЕ, "has:confidence", "low_confidence"])
        assert сколько == (round(доля * len(уверенность)) if доля is not None else 0), номер
        низких += сколько
        wer = _величины(c, окно, [ГОТОВЫЕ, "has:wer"], "wer:desc")
        assert _среднее(wer, 4) == ход["wer"][номер], номер
    assert sum(ход["jobs"]) == всего(c, отчёт["window"], [ГОТОВЫЕ, "has:confidence"])
    assert sum(ход["jobs"]) and низких


def test_контрольные_карты_по_суткам(c, отчёт):
    карты = отчёт["control"]
    края = карты["edges"]
    по_ключу = {к["key"]: к["points"] for к in карты["charts"]}
    assert set(по_ключу) == {"confidence", "low_confidence_share", "suspect_share"}
    assert len(края) == len(по_ключу["confidence"]) + 1 and края == sorted(края)
    было = 0
    for номер in range(len(края) - 1):
        окно = _корзина(края, номер)
        assert всего(c, окно, [ГОТОВЫЕ]) == по_ключу["confidence"][номер]["jobs"], номер
        уверенность = _величины(c, окно, [ГОТОВЫЕ, "has:confidence"], "confidence:asc")
        assert _среднее(уверенность, 4) == по_ключу["confidence"][номер]["value"], номер
        низких = всего(c, окно, [ГОТОВЫЕ, "has:confidence", "low_confidence"])
        ожидалось = по_ключу["low_confidence_share"][номер]["value"]
        assert (round(100.0 * низких / len(уверенность), 4) if уверенность else None) \
            == ожидалось, номер
        подозрительные = _величины(c, окно, [ГОТОВЫЕ, "assessed", "nz:segments"],
                                   "suspect_pct:desc")
        assert _среднее(подозрительные, 4) == по_ключу["suspect_share"][номер]["value"], номер
        было += len(уверенность)
    assert было


# ---------------------------------------------------------------------------
# Графики распределений
# ---------------------------------------------------------------------------


def test_время_по_этапам(c, отчёт):
    этапы = отчёт["overview"]["stages"]
    assert len(этапы["keys"]) == len(этапы["labels"]) == len(этапы["seconds"])
    for колонка, секунд in zip(этапы["keys"], этапы["seconds"], strict=True):
        assert колонка.endswith("_s")
        величина = колонка[:-2]
        столбец = _величины(c, отчёт["window"], [ГОТОВЫЕ, f"nz:{величина}"], f"{величина}:desc")
        assert round(sum(столбец), 2) == секунд, колонка
    if _неделя(отчёт):
        assert {"model_load_s", "alignment_s", "vad_s"} <= set(этапы["keys"])
        assert all(этапы["seconds"][этапы["keys"].index(к)] for к in ("model_load_s", "alignment_s"))


def test_распределение_уверенности(c, отчёт):
    корзины = отчёт["overview"]["quality"]["confidence_distribution"]
    assert корзины and корзины[-1].get("inclusive") is True
    assert not any(к.get("inclusive") for к in корзины[:-1])
    for корзина in корзины:
        верх = None if корзина.get("inclusive") else корзина["to"]
        отбор = [ГОТОВЫЕ, "nz:confidence", f"confidence:{_диапазон(корзина['from'], верх)}"]
        assert всего(c, отчёт["window"], отбор) == корзина["count"], корзина
    assert sum(к["count"] for к in корзины) == отчёт["overview"]["quality"]["confidence"]["count"]
    # Задание с уверенностью ровно 1,0 досеяно: по «меньше единицы» оно бы выпало.
    assert корзины[-1]["count"] > всего(c, отчёт["window"], [
        ГОТОВЫЕ, "nz:confidence", f"confidence:{корзины[-1]['from']}..{корзины[-1]['to']}"])


def test_длительность_записей(c, отчёт):
    длины = отчёт["durations"]
    assert len(длины["edges"]) == len(длины["labels"]) + 1
    for номер, сколько in enumerate(длины["counts"]):
        отбор = [ГОТОВЫЕ, "nz:duration",
                 f"duration:{_диапазон(длины['edges'][номер], длины['edges'][номер + 1])}"]
        assert всего(c, отчёт["window"], отбор) == сколько, длины["labels"][номер]
    assert sum(длины["counts"]) == длины["total"]
    if _неделя(отчёт):
        assert sum(1 for сколько in длины["counts"] if сколько) >= 8


def test_профиль_по_часам_и_карта_недели(c, отчёт):
    окно = отчёт["window"]
    часы = отчёт["profile"]["hours"]
    for час, сколько in enumerate(часы):
        assert всего(c, окно, [f"hour:{час}"]) == сколько, час
    assert sum(часы) == отчёт["overview"]["jobs"]["total"]
    неделя = отчёт["weekly"]
    assert неделя["hours"] == list(range(24))
    непустых = 0
    for день, строка in enumerate(неделя["jobs"]):
        for час, сколько in enumerate(строка):
            # Пустые клетки — через одну: и ноль обязан сойтись с пустым списком.
            if сколько or (день + час) % 5 == 0:
                assert всего(c, окно, [f"weekday:{день}", f"hour:{час}"]) == сколько, (день, час)
            непустых += bool(сколько)
    пик = неделя["peak"]
    assert всего(c, окно, [f"weekday:{пик['day']}", f"hour:{пик['hour']}"]) == пик["jobs"]
    assert непустых > 1 and пик["jobs"]


# ---------------------------------------------------------------------------
# Разрезы по моделям, языкам, владельцам, движкам, меткам
# ---------------------------------------------------------------------------


def test_сравнение_по_моделям(c, отчёт):
    окно = отчёт["window"]
    for строка in отчёт["models"]:
        модель = f"model:{_пусто(строка['model'], '—')}"
        assert всего(c, окно, [модель]) == строка["jobs"]
        assert всего(c, окно, [модель, ГОТОВЫЕ]) == строка["completed"]
        rtf = _величины(c, окно, [ГОТОВЫЕ, "nz:rtf", модель], "rtf:desc")
        assert _среднее(rtf, 4) == строка["rtf_avg"], строка["model"]
        уверенность = _величины(c, окно, [ГОТОВЫЕ, "nz:confidence", модель], "confidence:asc")
        assert _среднее(уверенность, 4) == строка["confidence_avg"], строка["model"]
        звук = _величины(c, окно, [ГОТОВЫЕ, "nz:duration", модель], "duration:desc")
        assert round(sum(звук) / 3600, 3) == строка["audio_hours"], строка["model"]
    if _неделя(отчёт):
        assert {"—", "demo-simulator", "whisper-large"} <= {м["model"] for м in отчёт["models"]}


@pytest.mark.parametrize("разрез, отбор, заглушка", [
    ("languages", "language", "Язык не определён"),
    ("owners", "owner", "аноним"),
    # Движки на экран не выводятся, но в ответе есть — и раскрываются так же.
    ("engines", "engine", "—"),
])
def test_разрезы_по_значению(c, отчёт, разрез, отбор, заглушка):
    assert отчёт[разрез]
    for строка in отчёт[разрез]:
        значение = _пусто(строка["key"], заглушка)
        assert всего(c, отчёт["window"], [f"{отбор}:{значение}"]) == строка["jobs"], строка["key"]
    assert sum(с["jobs"] for с in отчёт[разрез]) == отчёт["overview"]["jobs"]["total"]
    if _неделя(отчёт):
        assert заглушка in {с["key"] for с in отчёт[разрез]}


def test_разрез_по_меткам(c, отчёт):
    окно = отчёт["window"]
    метки = {с["tag"]: с for с in отчёт["tags"]}
    for метка, строка in метки.items():
        отбор = "untagged" if метка == "без метки" else f"tag:{метка}"
        assert всего(c, окно, [отбор]) == строка["jobs"], метка
        assert всего(c, окно, [отбор, ГОТОВЫЕ]) == строка["completed"], метка
        assert всего(c, окно, [отбор, "status:failed"]) == строка["failed"], метка
    # «срочно, срочно» — одно задание с одной меткой, а не два.
    assert метки["новая"]["jobs"] == 1 and "без метки" in метки
    assert метки["срочно"]["jobs"] == всего(c, окно, ["tag:срочно"]) > 2
    if _неделя(отчёт):
        assert any(с["failed"] for с in метки.values())


# ---------------------------------------------------------------------------
# Ошибки, эффективность, медленные
# ---------------------------------------------------------------------------


def test_ошибки_по_кодам_и_моделям(c, отчёт):
    окно, ошибки = отчёт["window"], отчёт["errors"]
    assert всего(c, окно, ["status:failed"]) == ошибки["total_failed"]
    for строка in ошибки["by_code"]:
        код = ["status:failed", f"error:{_пусто(строка['code'], 'unknown')}"]
        assert всего(c, окно, код) == строка["count"], строка["code"]
        for модель, сколько in строка["models"].items():
            assert всего(c, окно, [*код, f"model:{_пусто(модель, '—')}"]) == сколько
        assert sum(строка["models"].values()) == строка["count"]
    коды = {с["code"]: с for с in ошибки["by_code"]}
    # Отказы без кода (NULL и пустая строка) — одна строка «unknown».
    assert коды["unknown"]["count"] == 2 and "—" in коды["unknown"]["models"]
    if _неделя(отчёт):
        assert {"audio_decode_failed", "model_load_failed"} <= set(коды)


def test_эффективность(c, отчёт):
    окно, э = отчёт["window"], отчёт["efficiency"]
    звук = _величины(c, окно, [ГОТОВЫЕ, "nz:duration"], "duration:desc")
    assert round(sum(звук) / 3600, 2) == э["audio_hours"]
    обработка = _величины(c, окно, [ГОТОВЫЕ, "nz:processing"], "processing:desc")
    assert round(sum(обработка) / 3600, 2) == э["compute_hours"]
    загрузка = _величины(c, окно, [ГОТОВЫЕ, "nz:model_load"], "model_load:desc")
    assert round(sum(загрузка), 1) == э["model_load_seconds"]
    assert всего(c, окно, [ГОТОВЫЕ, "cached"]) == э["cache_hits"]
    if _неделя(отчёт):
        assert э["cache_hits"] and э["model_load_seconds"]


def test_самые_медленные_это_верх_списка_по_rtf(c, отчёт):
    """Таблица показывает десятку; «все по RTF» под ней продолжает её же."""
    верх = _величины(c, отчёт["window"], [ГОТОВЫЕ, "nz:rtf"], "rtf:desc")[:15]
    assert [float(з["rtf"]) for з in отчёт["slowest"]] == верх
    assert верх == sorted(верх, reverse=True) and верх


# ---------------------------------------------------------------------------
# Ожидание в очереди, надёжность, кеш
# ---------------------------------------------------------------------------


def test_ожидание_в_очереди(c, отчёт):
    окно, очередь = отчёт["window"], отчёт["queue"]
    ожидания = _величины(c, окно, ["has:wait"], "wait:desc")
    assert len(ожидания) == очередь["overall"]["count"]
    assert ожидания[0] == очередь["overall"]["max"]
    assert round(M.percentile(ожидания, 0.5), 6) == очередь["overall"]["p50"]
    assert всего(c, окно, ["has:wait", "waited_minute"]) == очередь["waited_over_minute"]
    assert всего(c, окно, ["has:wait", "waited_long"]) == очередь["waited_over_10_minutes"]
    for строка in очередь["by_priority"]:
        assert всего(c, окно, ["has:wait", f"priority_{строка['key']}"]) == строка["jobs"]
    for строка in очередь["by_source"]:
        assert всего(c, окно, ["has:wait", f"source_api:{строка['name']}"]) == строка["jobs"]
    for разрез in ("by_priority", "by_source"):
        assert sum(с["jobs"] for с in очередь[разрез]) == len(ожидания)
    assert {с["key"] for с in очередь["by_priority"]} == {"high", "normal", "low"}
    # Нулевое ожидание — в счёте, а ровно минута — ещё не «дольше минуты».
    assert 0.0 in ожидания and 60.0 in ожидания and 600.0 in ожидания
    assert очередь["waited_over_minute"] == sum(1 for v in ожидания if v > 60)
    if _неделя(отчёт):
        assert "api" in {с["name"] for с in очередь["by_source"]}
        assert len(ожидания) > отчёт["overview"]["jobs"]["completed"] - 10


def test_надёжность(c, отчёт):
    окно, н = отчёт["window"], отчёт["reliability"]
    assert всего(c, окно) == н["total"]
    assert всего(c, окно, [ГОТОВЫЕ, "first_try"]) == н["first_attempt_success"]
    assert всего(c, окно, [ГОТОВЫЕ, "retried"]) == н["completed_after_retry"]
    повторы = _величины(c, окно, ["retried"], "retries:desc")
    assert len(повторы) == н["jobs_with_retries"] and sum(повторы) == н["retry_total"]
    assert всего(c, окно, ["status:cancelled"]) == н["cancelled"]
    for строка in н["cancelled_by"]:
        отбор = ["status:cancelled", f"cancelled_by:{_пусто(строка['who'], 'неизвестно')}"]
        assert всего(c, окно, отбор) == строка["jobs"], строка["who"]
    for строка in н["webhooks"]:
        assert всего(c, окно, [f"webhook:{строка['status']}"]) == строка["jobs"]
    assert {"неизвестно", "admin"} <= {с["who"] for с in н["cancelled_by"]}
    assert {с["status"] for с in н["webhooks"]} == {"delivered", "failed"}
    if _неделя(отчёт):
        assert н["completed_after_retry"] and н["jobs_with_retries"] > н["completed_after_retry"]


def test_повторы_и_экономия(c, отчёт):
    окно, кеш = отчёт["window"], отчёт["cache"]
    assert всего(c, окно, ["cached"]) == кеш["hits"]
    звук = _величины(c, окно, ["cached", "nz:duration"], "duration:desc")
    assert round(sum(звук) / 3600, 3) == кеш["audio_hours_saved"]
    assert кеш["repeats"]
    for строка in кеш["repeats"]:
        assert всего(c, окно, ["cached", f"same_file:{строка['key']}"]) == строка["hits"], строка
    # Повтор узнаётся по хешу, а без хеша — по заданию-источнику.
    ключи = {с["key"]: с["hits"] for с in кеш["repeats"]}
    assert ключи["хеш-б"] == 2
    if _неделя(отчёт):
        assert "хеш-а" in ключи and "dr0000" in ключи
        assert sum(ключи.values()) == кеш["hits"]


# ---------------------------------------------------------------------------
# Звук и ресурсы
# ---------------------------------------------------------------------------


def test_каким_бывает_звук(c, отчёт):
    окно, звук = отчёт["window"], отчёт["audio"]
    длинные = [ГОТОВЫЕ, "longer_second"]
    for поле, отбор, величина in (("speech_rate_wpm", "nz:words", "speech_rate"),
                                  ("bitrate_kbps", "nz:size", "bitrate"),
                                  ("segments_per_minute", "nz:segments", "segment_rate")):
        столбец = _величины(c, окно, [*длинные, отбор], f"{величина}:desc")
        assert len(столбец) == звук[поле]["count"], поле
        assert sum(столбец) / len(столбец) == pytest.approx(звук[поле]["avg"], abs=1e-5), поле
        assert столбец[0] == pytest.approx(звук[поле]["max"], abs=1e-5), поле
        # Запись ровно в секунду — не «длиннее секунды».
        assert len(столбец) < всего(c, окно, [ГОТОВЫЕ, "duration:1..", отбор]), поле
    форматы = {ф["format"]: ф["jobs"] for ф in звук["formats"]}
    for формат, сколько in форматы.items():
        отбор = "no_format" if формат == "без расширения" else f"format:{формат}"
        assert всего(c, окно, [ГОТОВЫЕ, отбор]) == сколько, формат
    assert sum(форматы.values()) == отчёт["overview"]["jobs"]["completed"]
    # Имена, на которых «после последней точки» и «суффикс пути» расходятся.
    assert {"скрытый": 1, "": 1, "x/файл": 1, "gz": 1} .items() <= форматы.items()
    assert форматы["wav"] >= 2 and форматы["без расширения"] >= 2
    for строка in звук["speakers"]:
        отбор = [ГОТОВЫЕ, f"speakers:{строка['speakers']}"]
        assert всего(c, окно, отбор) == строка["jobs"], строка

    измерены = [ГОТОВЫЕ, "has:snr"]
    assert всего(c, окно, измерены) == звук["measured_jobs"]
    assert всего(c, окно, [*измерены, "bad_audio"]) == звук["bad_audio_jobs"]
    assert всего(c, окно, [*измерены, "noisy"]) == звук["noisy_jobs"]
    assert всего(c, окно, [*измерены, "clipped"], sort="clipping:desc") == звук["clipped_jobs"]
    assert всего(c, окно, [*измерены, "has:loudness"], sort="loudness:asc") \
        == звук["loudness_lufs"]["count"]
    assert всего(c, окно, [*измерены, "has:audio_silence"], sort="audio_silence:desc") \
        == звук["silence_share"]["count"]
    шум = _величины(c, окно, измерены, "snr:asc")
    assert round(M.percentile(шум, 0.5), 6) == звук["snr_db"]["p50"]
    for полоса in звук["snr_bands"]:
        корзина = [*измерены, f"snr:{_диапазон(полоса['from'], полоса['to'])}"]
        assert всего(c, окно, корзина) == полоса["jobs"], полоса["key"]
        assert всего(c, окно, [*корзина, "has:wer"]) == полоса["wer_jobs"], полоса["key"]
    assert sum(п["jobs"] for п in звук["snr_bands"]) == звук["measured_jobs"]
    assert [(п["from"], п["to"]) for п in звук["snr_bands"]] \
        == [(None, 5.0), (5.0, 10.0), (10.0, 20.0), (20.0, None)]
    for строка in звук["bad_audio_by_source"]:
        источник = f"source_api:{строка['key']}"
        assert всего(c, окно, [*измерены, источник]) == строка["jobs"], строка["key"]
        assert всего(c, окно, [*измерены, "bad_audio", источник]) == строка["bad"], строка["key"]
    if _неделя(отчёт):
        assert звук["noisy_jobs"] and звук["clipped_jobs"] and звук["silence_share"]["count"]
        assert all(п["jobs"] for п in звук["snr_bands"])
        assert any(п["wer_jobs"] for п in звук["snr_bands"])


def test_расход_ресурсов(c, отчёт):
    окно, ресурсы = отчёт["window"], отчёт["resources"]
    for строка in ресурсы["devices"]:
        отбор = [ГОТОВЫЕ, f"device:{_пусто(строка['device'], 'неизвестно')}"]
        assert всего(c, окно, отбор) == строка["jobs"], строка["device"]
    assert sum(с["jobs"] for с in ресурсы["devices"]) == отчёт["overview"]["jobs"]["completed"]
    for строка in ресурсы["models"]:
        память = _величины(c, окно, [ГОТОВЫЕ, "nz:memory", f"model:{_пусто(строка['model'], '—')}"],
                           "memory:desc")
        assert len(память) == строка["jobs"] and память[0] == строка["peak_mb"], строка["model"]
    for строка in ресурсы["concurrency"]:
        отбор = [ГОТОВЫЕ, "nz:memory", f"at_once:{строка['jobs_at_once']}"]
        assert всего(c, окно, отбор) == строка["measurements"], строка
    assert sum(с["measurements"] for с in ресурсы["concurrency"]) \
        == sum(с["jobs"] for с in ресурсы["models"]) == всего(c, окно, [ГОТОВЫЕ, "nz:memory"])
    if _неделя(отчёт):
        assert {с["device"] for с in ресурсы["devices"]} == {"cuda", "cpu", "неизвестно"}
        assert [с["jobs_at_once"] for с in ресурсы["concurrency"]] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Здоровье распознавания
# ---------------------------------------------------------------------------


def test_подозрительные_расшифровки(c, отчёт):
    окно, п = отчёт["window"], отчёт["suspicious"]
    основа = [ГОТОВЫЕ, "nz:segments"]
    assert всего(c, окно, основа) == п["jobs"]
    assert всего(c, окно, [*основа, "assessed"]) == п["assessed"]
    assert всего(c, окно, [*основа, "unassessed"]) == п["jobs"] - п["assessed"]
    assert всего(c, окно, [*основа, "flagged"]) == п["flagged"]
    сегменты = _величины(c, окно, [*основа, "assessed", "suspect_segments"], "suspect_count:desc")
    assert sum(сегменты) == п["suspect_segments"]
    for строка in п["by_flag"]:
        assert всего(c, окно, [*основа, f"flag:{строка['key']}"]) == строка["jobs"], строка["key"]
    for разрез, отбор in (("by_model", "model"), ("by_source", "source")):
        for строка in п[разрез]:
            значение = f"{отбор}:{_пусто(строка['key'], '—')}"
            assert всего(c, окно, [*основа, "assessed", значение]) == строка["jobs"], строка
            assert всего(c, окно, [*основа, "flagged", значение]) == строка["flagged"], строка
        assert sum(с["jobs"] for с in п[разрез]) == п["assessed"]
    # Признаки без сегментов — вне основы: в списке «с признаками» их нет.
    assert п["flagged"] < всего(c, окно, [ГОТОВЫЕ, "flagged"])
    if _неделя(отчёт):
        assert п["jobs"] > п["assessed"] > п["flagged"] > 0 and п["suspect_segments"]
        assert {"repeat", "silence", "tempo", "phrase"} <= {с["key"] for с in п["by_flag"]}
        assert "—" in {с["key"] for с in п["by_source"]}


def test_дрейф_уверенности(c, отчёт):
    окно, дрейф = отчёт["window"], отчёт["drift"]
    база = дрейф["baseline_window"]
    assert (база is None) == (окно["since"] is None)
    if база is not None:
        assert база["until"] == окно["since"]
    for строка in (дрейф["overall"], *дрейф["models"]):
        отбор = [ГОТОВЫЕ, "has:confidence"]
        if строка["key"] != "all":
            отбор.append(f"model:{_пусто(строка['key'], '—')}")
        сейчас = всего(c, окно, отбор)
        assert сейчас == строка["current"]["n"], строка["key"]
        низких = всего(c, окно, [*отбор, "low_confidence"])
        assert (round(100.0 * низких / сейчас, 1) if сейчас else None) == строка["low_share"]
        if база is None:
            assert строка["baseline"]["n"] == 0
            continue
        было = всего(c, база, отбор)
        assert было == строка["baseline"]["n"], строка["key"]
        низких_было = всего(c, база, [*отбор, "low_confidence"])
        assert (round(100.0 * низких_было / было, 1) if было else None) \
            == строка["low_share_baseline"], строка["key"]
    assert дрейф["overall"]["current"]["n"]
    if отчёт["overview"]["period"] == "day":
        # За сутки в окне четверть архива, остальное — база дрейфа.
        assert дрейф["overall"]["baseline"]["n"] > 20


def test_точность_по_эталону(c, отчёт):
    окно, точность = отчёт["window"], отчёт["accuracy"]
    эталон = [ГОТОВЫЕ, "has:wer"]
    assert всего(c, окно, эталон, sort="wer:desc") == точность["overall"]["jobs"]
    срезы = {
        "by_model": lambda р: f"model:{_пусто(р['key'], '—')}",
        "by_language": lambda р: f"language:{_пусто(р['key'], '—')}",
        "by_duration": lambda р: f"duration:{_диапазон(р['from'], р['to'])}",
        "by_source": lambda р: f"source_api:{р['key']}",
    }
    for разрез, отбор in срезы.items():
        for строка in точность[разрез]:
            assert всего(c, окно, [*эталон, отбор(строка)]) == строка["jobs"], (разрез, строка)
        assert sum(с["jobs"] for с in точность[разрез]) == точность["overall"]["jobs"], разрез
    худшие = _величины(c, окно, эталон, "wer:desc")[:10]
    assert худшие == [float(з["wer"]) for з in точность["worst"]]
    # Нулевой WER — запись с эталоном; без длительности — в корзине «до 1 мин».
    assert 0.0 in _величины(c, окно, эталон, "wer:desc")
    корзины = {с["key"]: с for с in точность["by_duration"]}
    assert (корзины["до 1 мин"]["from"], корзины["до 1 мин"]["to"]) == (None, 60)
    assert (корзины["более часа"]["from"], корзины["более часа"]["to"]) == (3600, None)
    assert {"api", "—"} & {с["key"] for с in точность["by_source"]} == {"api"}
    if _неделя(отчёт):
        assert len(точность["by_duration"]) >= 3 and len(точность["by_source"]) >= 2
        assert len(точность["by_model"]) >= 3 and len(точность["by_language"]) >= 2


def test_калибровка_уверенности(c, отчёт):
    окно, калибровка = отчёт["window"], отчёт["calibration"]
    assert всего(c, окно, [ГОТОВЫЕ, "calibrated"]) == калибровка["jobs"]
    for строка in калибровка["by_model"]:
        отбор = [ГОТОВЫЕ, "calibrated", f"model:{_пусто(строка['key'], '—')}"]
        assert всего(c, окно, отбор) == строка["jobs"], строка["key"]
    # Битая строка, пустая, корзины не того размера и «не объект» досеяны и
    # в свод не входят — значит, и в список не должны.
    мусор = c.get("/api/drill", params={"f": [ГОТОВЫЕ, "has:calibration"], "limit": 1}).json()
    assert мусор["total"] >= калибровка["jobs"] + 4
    if _неделя(отчёт):
        assert калибровка["jobs"] >= 4 and len(калибровка["by_model"]) >= 2


def test_латентность(c, отчёт):
    окно, латентность = отчёт["window"], отчёт["latency"]
    свои = [ГОТОВЫЕ, "computed", "has:processing"]
    время = _величины(c, окно, свои, "processing:desc")
    assert len(время) == латентность["overall"]["jobs"]
    assert round(M.percentile(время, 0.95), 2) == латентность["overall"]["processing_p95"]
    rtf = _величины(c, окно, [*свои, "nz:rtf"], "rtf:desc")
    assert round(M.percentile(rtf, 0.95), 4) == латентность["overall"]["rtf_p95"]
    for строка in латентность["by_model"]:
        отбор = [*свои, f"model:{_пусто(строка['key'], '—')}"]
        assert всего(c, окно, отбор, sort="processing:desc") == строка["jobs"], строка["key"]
    for строка in латентность["by_duration"]:
        отбор = [*свои, f"duration:{_диапазон(строка['from'], строка['to'])}"]
        assert всего(c, окно, отбор, sort="processing:desc") == строка["jobs"], строка["key"]
    for разрез in ("by_model", "by_duration"):
        assert sum(с["jobs"] for с in латентность[разрез]) == len(время)
    # Взятые из кеша и без времени обработки — мимо.
    assert len(время) < отчёт["overview"]["jobs"]["completed"]


# ---------------------------------------------------------------------------
# Под ключом владельца
# ---------------------------------------------------------------------------


def test_под_ключом_владельца_число_и_список_режутся_одинаково(tmp_path_factory):
    """Свод обычного ключа считается по его заданиям, свод ключа в группе —
    по заданиям группы. Список под числом обязан резаться тем же: иначе
    «моих заданий 40» раскрылось бы в чужие или в половину своих."""
    хранилище = Архив(tmp_path_factory.mktemp("drill-analytics-auth") / "data", auth=True,
                      с_моделью=0, ключи={
                          "ah_admin": {"name": "админ", "role": "admin"},
                          "ah_anna": {"name": "анна", "role": "user"},
                          "ah_boris": {"name": "борис", "role": "user", "group": "продажи"},
                          "ah_vera": {"name": "вера", "role": "user", "group": "продажи"}})
    try:
        досеять(хранилище.db, хранилище.данные["now"])
        with TestClient(хранилище.app) as клиент:
            итоги = {}
            for ключ in ("ah_admin", "ah_anna", "ah_boris"):
                клиент.headers["X-API-Key"] = ключ
                отчёт = клиент.get("/api/analytics", params={"period": "week"}).json()
                окно = отчёт["window"]
                итоги[ключ] = отчёт["overview"]["jobs"]["total"]
                assert всего(клиент, окно) == итоги[ключ]
                assert всего(клиент, окно, [ГОТОВЫЕ, "nz:rtf"]) \
                    == отчёт["overview"]["performance"]["rtf"]["count"]
                assert всего(клиент, окно, ["status:failed"]) == отчёт["errors"]["total_failed"]
                for строка in отчёт["models"]:
                    отбор = [f"model:{_пусто(строка['model'], '—')}"]
                    assert всего(клиент, окно, отбор) == строка["jobs"], (ключ, строка["model"])
                for строка in отчёт["tags"]:
                    отбор = ["untagged" if строка["tag"] == "без метки" else f"tag:{строка['tag']}"]
                    assert всего(клиент, окно, отбор) == строка["jobs"], (ключ, строка["tag"])
                ряд = отчёт["timeseries"]
                assert sum(ряд["completed"]) + sum(ряд["failed"]) == всего(
                    клиент, {"since": ряд["edges"][0], "until": ряд["edges"][-1]},
                    ["status:completed,failed"])
                владельцы = {с["key"] for с in отчёт["owners"]}
                assert владельцы == {"ah_anna": {"анна"}, "ah_boris": {"борис", "вера"}}.get(
                    ключ, владельцы)
            assert итоги["ah_admin"] > итоги["ah_boris"] > 0 and итоги["ah_anna"] > 0
            assert итоги["ah_anna"] != итоги["ah_boris"]
    finally:
        хранилище.закрыть()
