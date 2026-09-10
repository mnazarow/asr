"""Здоровье распознавания, этап 4: точность по эталону, калибровка, латентность.

Здесь проверяется то, что можно посчитать без моделей и видеокарты: сами
формулы (MER, WIL, ECE, AUC), выравнивание по словам, срезы по архиву и
их путь до ручек, выгрузки и метрик.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pytest
from asrhub.pipeline import calibration as C
from asrhub.pipeline import metrics as M

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# Формулы
# ---------------------------------------------------------------------------


def test_mer_and_wil_follow_morris_2004():
    """MER = ошибок / пар выравнивания, WIL = 1 − H²/(N·M) — и оба в [0, 1]
    там, где WER давно ушёл за единицу."""
    разбор = M.detailed("a b c d", "a b x d e")
    # S=1, D=0, I=1, H=3, N=4, M=5.
    assert разбор["wer"] == 0.5
    assert разбор["mer"] == pytest.approx(2 / 5)
    assert разбор["wil"] == pytest.approx(1 - (3 / 4) * (3 / 5))

    # Совпадение — ноль везде; полное расхождение — единица везде.
    assert M.detailed("один два", "один два")["mer"] == 0.0
    assert M.detailed("один два", "один два")["wil"] == 0.0
    assert M.detailed("один два", "три четыре")["mer"] == 1.0
    assert M.detailed("один два", "три четыре")["wil"] == 1.0

    # Расшифровка втрое длиннее эталона: WER 200 %, а MER — доля вставок
    # среди пар, и он не может превысить единицу.
    длинная = M.detailed("а б в", "а б в г д е ж з и")
    assert длинная["wer"] == pytest.approx(2.0)
    assert длинная["mer"] == pytest.approx(6 / 9)
    assert 0.0 <= длинная["wil"] <= 1.0
    # Разрыв WER − MER и есть признак вставок: у чистых замен его нет.
    замены = M.detailed("а б в г", "а x y г")
    assert замены["wer"] == замены["mer"] == 0.5

    # Пустые тексты не делят на ноль.
    assert M.detailed("", "")["mer"] == 0.0 and M.detailed("", "")["wil"] == 0.0
    assert M.detailed("", "лишнее")["wil"] == 1.0

    поля = M.job_fields(разбор)
    assert поля == {"wer": 0.5, "cer": разбор["cer"], "mer": 0.4, "wil": 0.55,
                    "ref_words": 4, "sub_words": 1, "del_words": 0, "ins_words": 1}
    assert M.job_fields({}) == {}


def test_align_hits_marks_each_hypothesis_word():
    """По каждому слову гипотезы — верно или нет; пропуски эталона к словам
    не привязаны; длинные тексты идут через difflib и дают тот же итог."""
    assert M.align_hits(["а", "б", "в", "г"], ["а", "x", "в", "д", "е"]) == [True, False, True, False, False]
    # Одиночное слово между двумя совпадениями — замена, соседи верны.
    assert M.align_hits(["а", "б", "в", "г"], ["а", "б", "y", "г"]) == [True, True, False, True]
    assert M.align_hits([], ["x"]) == [False]
    assert M.align_hits(["x"], []) == []
    # Пропуск в середине: слова гипотезы всё равно верны.
    assert M.align_hits(["а", "б", "в"], ["а", "в"]) == [True, True]

    rng = random.Random(7)
    словарь = [f"слово{i}" for i in range(300)]
    эталон = [rng.choice(словарь) for _ in range(2600)]
    гипотеза = list(эталон)
    for i in range(0, len(гипотеза), 40):
        гипотеза[i] = "ошибка"
    del гипотеза[100:110]
    # Матрица 2600 × 2590 больше предела прямого расчёта — путь через difflib.
    assert len(эталон) * len(гипотеза) > M._MATRIX_LIMIT
    верно = M.align_hits(эталон, гипотеза)
    assert len(верно) == len(гипотеза)
    assert sum(верно) == M._levenshtein_chunked(эталон, гипотеза).hits


def test_calibration_bins_words_and_aggregates_ece_and_auc():
    """Слово с уверенностью движка предпочтительнее сегментной; слова без
    уверенности не считаются; ECE и AUC сходятся с ручным расчётом."""
    сегменты = [
        {"text": "да нет", "confidence": 0.2,
         "words": [{"word": "да", "confidence": 0.9}, {"word": "нет", "confidence": 0.3}]},
        {"text": "может быть", "confidence": 0.55},
        {"text": "без уверенности", "confidence": None},
    ]
    итог = C.per_job(сегменты, "да да может быть без уверенности")
    assert итог["source"] == "word" and итог["words"] == 4
    корзины = итог["bins"]
    assert корзины[9] == [1, 0.9, 1]          # «да» — верно, 0,9
    assert корзины[3] == [1, 0.3, 0]          # «нет» — замена, 0,3
    assert корзины[5] == [2, 1.1, 2]          # «может быть» с уверенностью сегмента
    assert sum(к[0] for к in корзины) == 4

    свод = C.aggregate([итог, json.dumps(итог), None, "мусор", {"bins": [1, 2]}])
    assert свод["jobs"] == 2 and свод["words"] == 8
    # ECE: корзина 0,3 (2 слова, conf 0,3, acc 0) → 0,3·2/8; корзина 0,5
    # (4 слова, conf 0,55, acc 1) → 0,45·4/8; корзина 0,9 (2 слова,
    # conf 0,9, acc 1) → 0,1·2/8.
    assert свод["ece"] == pytest.approx(0.3 * 2 / 8 + 0.45 * 4 / 8 + 0.1 * 2 / 8, abs=1e-4)
    # AUC по корзинам: все верные выше единственной корзины неверных.
    assert свод["auc"] == 1.0
    assert свод["overconfidence"] == pytest.approx(свод["confidence"] - свод["accuracy"], abs=1e-4)
    assert свод["sources"] == {"word": 2}
    assert [к["words"] for к in свод["bins"]] == [0, 0, 0, 2, 0, 4, 0, 0, 0, 2]

    # Ничьи: верные и неверные в одной корзине — AUC ровно 0,5.
    ничья = {"words": 2, "source": "segment",
             "bins": [[0, 0.0, 0]] * 5 + [[2, 1.1, 1]] + [[0, 0.0, 0]] * 4}
    assert C.aggregate([ничья])["auc"] == 0.5
    assert C.aggregate([])["ece"] is None and C.aggregate([])["auc"] is None


# ---------------------------------------------------------------------------
# Срезы по архиву
# ---------------------------------------------------------------------------


def _архив(tmp_path):
    """Задания четырёх моделей с временем обработки; у части — эталон."""
    from asrhub.db import Database

    db = Database(tmp_path / "asrhub.db")
    сейчас = time.time()
    rng = random.Random(3)
    n = 0

    def задание(**поля):
        nonlocal n
        когда = сейчас - rng.uniform(600, 6 * 86400)
        job_id = db.create_job({"id": f"h{n:04d}", "filename": f"{n}.wav", "owner": "анна",
                                "media_duration_s": поля.pop("длительность", 40.0),
                                "model": поля.pop("model", "m1"), "engine": "e",
                                "source": поля.pop("source", "api"), "language": "ru"})
        db.execute("UPDATE jobs SET created_at=? WHERE id=?", (когда, job_id))
        db.update_job(job_id, status="completed", finished_at=когда + 20, **поля)
        n += 1
        return job_id

    # Короткая запись с половиной ошибок и длинная почти без ошибок: среднее
    # по записям даёт 25 %, а по словам — полтора процента.
    задание(model="m1", text="x", wer=0.5, mer=0.5, wil=0.75, ref_words=10, sub_words=5,
            del_words=0, ins_words=0, processing_time_s=4.0, rtf=0.1, queue_time_s=1.0,
            calibration={"words": 10, "source": "segment",
                         "bins": [[0, 0.0, 0]] * 9 + [[10, 9.0, 5]]})
    задание(model="m1", text="y", wer=0.01, mer=0.0099, wil=0.02, ref_words=1000,
            sub_words=5, del_words=2, ins_words=3, processing_time_s=400.0, rtf=0.2,
            queue_time_s=3.0, длительность=2000.0,
            calibration={"words": 1000, "source": "word",
                         "bins": [[0, 0.0, 0]] * 8 + [[100, 85.0, 60], [900, 855.0, 890]]})
    # Запись, посчитанная до появления счётчиков: только WER.
    задание(model="m2", text="z", wer=0.3, processing_time_s=10.0, rtf=0.25, queue_time_s=0.5)
    # Из кеша: времени обработки нет, в латентность не входит.
    задание(model="m2", text="w", processing_time_s=0.0, rtf=0.25, cached_from="h0000")
    for i in range(20):
        задание(model="m3", text="v", processing_time_s=2.0 + i, rtf=0.05 + i * 0.01,
                queue_time_s=float(i), source="web" if i % 2 else "api")
    return db


def test_accuracy_slices_pool_words_instead_of_averaging_records(tmp_path):
    from asrhub.analytics import Analytics

    а = Analytics(_архив(tmp_path))
    точность = а.accuracy("month")
    общий = точность["overall"]
    assert общий["jobs"] == 3 and общий["words"] == 1010 and общий["enough"] is False
    # Сложением слов: (5+0+0 + 5+2+3) / 1010; средним по записям — 0,27.
    assert общий["wer"] == pytest.approx(15 / 1010, abs=1e-4)
    assert общий["wer_avg"] == pytest.approx((0.5 + 0.01 + 0.3) / 3, abs=1e-4)
    h = 1010 - 10 - 2
    assert общий["mer"] == pytest.approx(15 / (15 + h), abs=1e-4)
    assert общий["wil"] == pytest.approx(1 - (h / 1010) * (h / (h + 10 + 3)), abs=1e-4)
    assert общий["gap"] == pytest.approx(общий["wer"] - общий["mer"], abs=1e-4)
    assert общий["insertion_share"] == pytest.approx(3 / 1010, abs=1e-4)
    по_моделям = {р["key"]: р for р in точность["by_model"]}
    assert по_моделям["m1"]["words"] == 1010 and по_моделям["m2"]["words"] == 0
    assert по_моделям["m2"]["wer"] is None and по_моделям["m2"]["wer_avg"] == 0.3
    assert [р["key"] for р in точность["by_duration"]] == ["до 1 мин", "20–60 мин"]
    assert точность["worst"][0]["wer"] == 0.5 and len(точность["worst"]) == 3
    assert точность["enough_words"] == 10000

    калибровка = а.calibration("month")
    assert калибровка["jobs"] == 2 and калибровка["words"] == 1010
    assert калибровка["sources"] == {"segment": 1, "word": 1}
    assert {м["key"] for м in калибровка["by_model"]} == {"m1"}
    assert калибровка["by_model"][0]["ece"] == калибровка["ece"]
    # Корзина 0,9: 910 слов, уверенность 0,95, верных 895 — переоценка около
    # 0,0 там, а в корзине 0,8 — 100 слов, 0,85 против 0,6.
    assert калибровка["bins"][8]["accuracy"] == 0.6 and калибровка["bins"][8]["gap"] == 0.25


def test_latency_reports_tails_by_model_and_duration_and_stream_first_text(tmp_path):
    from asrhub.analytics import Analytics

    db = _архив(tmp_path)
    for секунд, модель in ((0.5, "vosk"), (0.7, "vosk"), (4.0, "m1"), (6.0, "m1")):
        db.add_metric("stream_first_text_s", секунд, model=модель)
        db.add_metric("stream_session_s", 60.0, model=модель)
    л = Analytics(db).latency("month")
    # 3 + 20 заданий со своим временем; клон из кеша — нет.
    assert л["overall"]["jobs"] == 23
    по_моделям = {р["key"]: р for р in л["by_model"]}
    assert set(по_моделям) == {"m1", "m2", "m3"}
    assert по_моделям["m1"]["processing_p50"] == pytest.approx(202.0)
    assert по_моделям["m3"]["jobs"] == 20
    assert по_моделям["m3"]["processing_p95"] == pytest.approx(M.percentile(
        [2.0 + i for i in range(20)], 0.95), abs=0.01)
    assert по_моделям["m3"]["rtf_p99"] >= по_моделям["m3"]["rtf_p95"] >= по_моделям["m3"]["rtf_p50"]
    assert по_моделям["m3"]["queue_p95"] == pytest.approx(M.percentile(
        [float(i) for i in range(20)], 0.95), abs=0.01)
    assert [р["key"] for р in л["by_duration"]] == ["до 1 мин", "20–60 мин"]
    поток = л["stream"]
    assert поток["sessions"] == 4 and поток["first_text_p50"] == pytest.approx(2.35)
    assert [(м["key"], м["sessions"]) for м in поток["by_model"]] == [("m1", 2), ("vosk", 2)]
    assert поток["by_model"][1]["first_text_p95"] == pytest.approx(0.69)


# ---------------------------------------------------------------------------
# Поток
# ---------------------------------------------------------------------------


class _Движок:
    """Движок без потока: сессия идёт скользящим окном."""

    def transcribe(self, path, settings, progress=None):
        from asrhub.engines.base import Segment, TranscriptionResult

        return TranscriptionResult(segments=[Segment(0.0, 1.0, "привет")], language="ru")


class _Реестр:
    def get(self, settings):
        return _Движок()

    def lease(self, settings):
        import contextlib

        return contextlib.nullcontext(_Движок())


def test_stream_session_reports_time_to_first_text(tmp_path, monkeypatch):
    from asrhub import streaming
    from asrhub.streaming import SAMPLE_RATE, StreamSession, tone

    monkeypatch.setattr(streaming.StreamSession, "_recognize", lambda self, pcm: "привет мир")
    сессия = StreamSession(_Реестр(), {"model": "m1", "stream_window_s": 1.0,
                                       "temp_dir": str(tmp_path)})
    assert сессия.start().extra["mode"] == "window"
    assert сессия.first_text_s is None
    события = сессия.feed(tone(1.2))
    assert [с.type for с in события] == ["partial"]
    assert сессия.first_text_s is not None and сессия.first_text_s >= 0.0
    первый = сессия.first_text_s
    # Следующие гипотезы первого момента не сдвигают.
    сессия.feed(tone(1.2))
    assert сессия.first_text_s == первый
    done = [с for с in сессия.finish() if с.type == "done"][0]
    assert done.extra["first_text_s"] == первый
    assert done.extra["session_s"] >= первый
    assert done.extra["duration_s"] == pytest.approx(2.4, abs=0.05)
    assert len(tone(1.0)) == SAMPLE_RATE * 2


def test_the_websocket_records_stream_latency_as_a_metric(tmp_path):
    from asrhub.api.app import _record_stream
    from asrhub.db import Database
    from asrhub.streaming import StreamEvent

    db = Database(tmp_path / "asrhub.db")

    class _Состояние:
        pass

    состояние = _Состояние()
    состояние.db = db
    _record_stream(состояние, {"model": "vosk-ru", "engine": "vosk"},
                   StreamEvent("done", extra={"first_text_s": 0.42, "session_s": 12.5,
                                              "duration_s": 11.0, "text": "…"}))
    первые = db.metric_values("stream_first_text_s", 0)
    assert len(первые) == 1 and первые[0]["value"] == 0.42 and первые[0]["model"] == "vosk-ru"
    сессии = db.metric_values("stream_session_s", 0)
    assert сессии[0]["value"] == 12.5 and json.loads(сессии[0]["labels"]) == {"audio_s": 11.0}
    # Без первого текста — только длина сессии; сбой базы не выходит наружу.
    _record_stream(состояние, {"model": "m"}, StreamEvent("done", extra={"session_s": 3.0}))
    assert len(db.metric_values("stream_first_text_s", 0)) == 1
    состояние.db = None
    _record_stream(состояние, {}, StreamEvent("done", extra={}))


# ---------------------------------------------------------------------------
# Ручки, выгрузка, метрики
# ---------------------------------------------------------------------------


def test_the_sections_reach_the_api_the_export_and_prometheus(client, sample_wav: Path):
    """Ручки отдают разделы, выгрузка несёт листы, метрики — MER и хвосты."""
    import io
    import zipfile

    with sample_wav.open("rb") as handle:
        job = client.post("/api/jobs", files={"file": ("э.wav", handle, "audio/wav")},
                          data={"settings": json.dumps({"model": "demo-simulator",
                                                        "engine": "demo",
                                                        "vad_backend": "energy"})}).json()
    for _ in range(80):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert job["status"] == "completed"
    слова = job["text"].split()
    слова[0] = "другое"
    assert client.post(f"/api/jobs/{job['id']}/reference",
                       json={"text": " ".join(слова)}).status_code == 200

    for раздел in ("accuracy", "calibration", "latency"):
        ответ = client.get(f"/api/analytics/{раздел}?period=day")
        assert ответ.status_code == 200, ответ.text
        assert ответ.json()["period"] == "day"
    точность = client.get("/api/analytics/accuracy?period=day").json()
    assert точность["overall"]["jobs"] == 1 and точность["by_model"][0]["key"] == "demo-simulator"
    assert точность["overall"]["wer"] > 0
    латентность = client.get("/api/analytics/latency?period=day").json()
    assert латентность["overall"]["jobs"] >= 1 and латентность["by_model"][0]["processing_p95"] > 0
    отчёт = client.get("/api/analytics?period=day").json()
    assert {"accuracy", "calibration", "latency"} <= set(отчёт)

    архив = zipfile.ZipFile(io.BytesIO(client.get("/api/analytics/export?period=day&fmt=csv").content))
    имена = " ".join(архив.namelist())
    for лист in ("Точность по моделям", "Калибровка уверенности", "Латентность по моделям",
                 "Латентность по длительности"):
        assert лист in имена, имена

    метрики = client.get("/api/monitoring/metrics").text
    assert 'asrhub_mer{model="demo-simulator"}' in метрики, метрики[:2000]
    assert 'asrhub_processing_p95_seconds{model="demo-simulator"}' in метрики
    assert 'asrhub_rtf_p95_by_model{model="demo-simulator"}' in метрики
    # Калибровка — от пятисот слов: одна короткая запись её не даёт.
    assert 'asrhub_calibration_ece{' not in метрики


# ---------------------------------------------------------------------------
# Профиль звука на входе
# ---------------------------------------------------------------------------


def test_k_weighting_reproduces_bs1770_and_lufs_matches_the_reference_tone():
    """Коэффициенты фильтров на 48 кГц совпадают с таблицей BS.1770-4;
    синус 997 Гц на −20 dBFS даёт −23 LUFS на любой частоте; тишина —
    ничего; тихий хвост отсекается гейтом."""
    import numpy as np
    from asrhub.pipeline import audio_profile as AP

    b, a = AP._biquad(*AP._ШЕЛФ, 48000.0)
    assert b == pytest.approx([1.53512485958697, -2.69169618940638, 1.19839281085285], abs=2e-4)
    assert a == pytest.approx([1.0, -1.69065929318241, 0.73248077421585], abs=2e-4)
    b, a = AP._biquad(*AP._ВЫСОКИЕ, 48000.0)
    assert b == [1.0, -2.0, 1.0]
    assert a == pytest.approx([1.0, -1.99004745483398, 0.99007225036621], abs=1e-4)

    for rate in (48000, 16000):
        t = np.arange(rate * 4) / rate
        синус = (0.1 * np.sin(2 * np.pi * 997 * t)).astype(np.float32)
        assert AP.loudness_lufs(синус, rate) == pytest.approx(-23.0, abs=0.15), rate
        # Хвост в −60 dBFS шума: относительный гейт (−10 LU) его выбрасывает.
        хвост = (np.random.default_rng(1).standard_normal(rate * 4) * 0.001).astype(np.float32)
        с_хвостом = np.concatenate([синус, хвост])
        assert AP.loudness_lufs(с_хвостом, rate) == pytest.approx(-23.0, abs=0.2), rate
    assert AP.loudness_lufs(np.zeros(16000, dtype=np.float32), 16000) is None
    assert AP.loudness_lufs(np.zeros(100, dtype=np.float32), 16000) is None


def test_audio_profile_estimates_snr_peak_clipping_and_silence():
    import numpy as np
    from asrhub.pipeline import audio_profile as AP

    rate = 16000
    rng = np.random.default_rng(5)
    t = np.arange(rate * 20) / rate
    # Речь-подобный сигнал: тон −20 dBFS секунду через секунду; шум −40 dBFS
    # всё время. Истинное отношение — 20 дБ.
    речь = (0.1 * np.sqrt(2) * np.sin(2 * np.pi * 180 * t)) * (np.floor(t) % 2 == 0)
    шум = rng.standard_normal(len(t)) * 0.01
    x = (речь + шум).astype(np.float32)
    п = AP.profile(x, rate)
    assert п["method"] == "percentile" and 16.0 <= п["snr_db"] <= 24.0, п
    assert п["clipping_share"] == 0.0 and -22.0 <= п["peak_dbfs"] <= -14.0
    assert п["silence_share"] == 0.0            # шум −40 dBFS громче порога −45
    assert п["measured_s"] == 20.0
    # С разметкой речи оценка точнее и доля тишины — по VAD.
    участки = [(float(s), float(s) + 1.0) for s in range(0, 20, 2)]
    в = AP.profile(x, rate, speech_spans=участки)
    assert в["method"] == "vad" and 18.0 <= в["snr_db"] <= 22.0, в
    assert в["silence_share"] == pytest.approx(0.5, abs=0.01)
    assert isinstance(в["silence_share"], float)

    # Уровень речи — средняя мощность, а не средний децибел: одна секунда
    # на −20 dBFS среди девяти на −40 даёт среднюю мощность −29,6 dBFS,
    # а средний децибел — −38; при шуме −50 dBFS это 20 дБ против 12.
    неровная = np.zeros(len(t))
    for s in range(0, 20, 2):
        уровень = 0.1 * np.sqrt(2) if s == 0 else 0.01 * np.sqrt(2)
        неровная[(t >= s) & (t < s + 1)] = уровень * np.sin(2 * np.pi * 180 * t[(t >= s) & (t < s + 1)])
    шумок = rng.standard_normal(len(t)) * 0.00316          # −50 dBFS
    н = AP.profile((неровная + шумок).astype(np.float32), rate, speech_spans=участки)
    assert 18.5 <= н["snr_db"] <= 22.5, н

    # Срезанные вершины: пик на нуле, доля клиппинга заметна.
    срез = np.clip(речь * 30, -1, 1).astype(np.float32)
    к = AP.profile(срез, rate)
    assert к["peak_dbfs"] == 0.0 and к["clipping_share"] > 0.1
    assert AP.is_bad(к) and AP.is_bad({"snr_db": 9.9}) and not AP.is_bad({"snr_db": 10.0})
    # Цифровая тишина между словами: оценка упирается в предел, а не в бесконечность.
    assert AP.profile(np.clip(речь, -1, 1).astype(np.float32), rate)["snr_db"] == AP.ПРЕДЕЛ_SNR
    # Тишина целиком: половина кадров тише порога.
    половина = np.concatenate([x[:rate * 10], np.zeros(rate * 10, dtype=np.float32)])
    assert AP.profile(половина, rate)["silence_share"] == pytest.approx(0.5, abs=0.02)
    assert AP.profile(np.zeros(0, dtype=np.float32), rate) == {}

    assert [AP.snr_band(v) for v in (-3, 4.9, 5, 9.9, 10, 19.9, 20, 60, None)] == [
        "ниже 5 дБ", "ниже 5 дБ", "5–10 дБ", "5–10 дБ", "10–20 дБ", "10–20 дБ",
        "20 дБ и выше", "20 дБ и выше", None]
    слитый = AP.merge([{"snr_db": 20, "clipping_share": 0.0, "loudness_lufs": -30,
                        "silence_share": 0.5, "peak_dbfs": -6},
                       {"snr_db": 8, "clipping_share": 0.02, "loudness_lufs": -20,
                        "silence_share": 0.7, "peak_dbfs": -1}])
    assert слитый == {"snr_db": 8, "clipping_share": 0.02, "loudness_lufs": -20,
                      "silence_share": 0.6, "peak_dbfs": -1}
    assert AP.merge([{}, {"snr_db": 3}]) == {"snr_db": 3} and AP.merge([]) == {}
    assert AP.for_job({}) == {} and set(AP.for_job(слитый)) == {
        "snr_db", "peak_dbfs", "clipping_share", "loudness_lufs", "silence_share"}


def test_the_pipeline_stores_the_audio_profile_and_the_archive_slices_it(
        client, sample_wav: Path):
    """Профиль считается при подготовке и ложится в задание; по нему есть
    отбор, разрез в аналитике, строка в сводке и метрика."""
    from asrhub.maintenance import build_digest

    with sample_wav.open("rb") as handle:
        job = client.post("/api/jobs", files={"file": ("звук.wav", handle, "audio/wav")},
                          data={"settings": json.dumps({"model": "demo-simulator",
                                                        "engine": "demo",
                                                        "vad_backend": "energy"})}).json()
    for _ in range(80):
        job = client.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert job["status"] == "completed", job
    for поле in ("snr_db", "peak_dbfs", "clipping_share", "loudness_lufs", "silence_share"):
        assert job[поле] is not None, поле
    # Запись — тон, тишина, тон: тишины заметно, клиппинга нет.
    assert job["silence_share"] > 0.1 and job["clipping_share"] == 0.0

    состояние = client.app.state.hub
    состояние.db.update_job(job["id"], snr_db=6.0)
    плохие = client.get("/api/jobs?content=bad_audio&light=true").json()
    assert [j["id"] for j in плохие["items"]] == [job["id"]]
    assert client.get("/api/jobs?content=clipped&light=true").json()["items"] == []
    assert client.get("/api/jobs?content=noisy&light=true").json()["total"] == 1

    звук = client.get("/api/analytics/audio?period=day").json()
    assert звук["measured_jobs"] >= 1 and звук["noisy_jobs"] == 1 and звук["bad_audio_jobs"] == 1
    assert звук["bad_audio_share"] > 0 and звук["snr_db"]["p10"] <= 6.0
    корзины = {к["key"]: к for к in звук["snr_bands"]}
    assert корзины["5–10 дБ"]["jobs"] == 1 and корзины["5–10 дБ"]["confidence_avg"] > 0
    assert звук["bad_audio_by_source"][0]["bad"] == 1

    сводка = build_digest(состояние.analytics, состояние.settings, period="day")
    assert сводка["bad_audio"]["jobs"] == 1 and сводка["bad_audio"]["noisy"] == 1
    assert "Плохой звук на входе: 1 из" in сводка["text"], сводка["text"]
    метрики = client.get("/api/monitoring/metrics").text
    assert "asrhub_bad_audio_share " in метрики and 'asrhub_audio_snr_db{stat="p50"}' in метрики
    import io
    import zipfile

    архив = zipfile.ZipFile(io.BytesIO(client.get("/api/analytics/export?period=day&fmt=csv").content))
    assert any("Звук на входе" in имя for имя in архив.namelist()), архив.namelist()
