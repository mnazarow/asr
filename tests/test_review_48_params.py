"""Заход 48: параметры, которые ничего не делали.

Каталог обещал резервную модель, формат времени в выгрузках, краткое
содержание, пословную привязку говорящих, долю видеопамяти, запись текста в
журнал и настройки потока — а конвейер этих параметров не читал: значение
можно было выставить, сохранить в config.yaml и увидеть в карточке задания,
и ничего не менялось. Проверки здесь — по поведению: параметр выставлен —
результат другой, чем без него.

Попутно — то, что всплыло рядом: готовое после повтора задание показывало
ошибку прошлой попытки, а колонка движка у заданий хранила «auto».
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest


def _ждать(клиент, ид: str, срок: float = 60.0) -> dict[str, Any]:
    конец = time.time() + срок
    задание: dict[str, Any] = {}
    while time.time() < конец:
        задание = клиент.get(f"/api/jobs/{ид}").json()
        if задание["status"] in ("completed", "failed", "cancelled"):
            return задание
        time.sleep(0.1)
    raise AssertionError(f"задание {ид} не завершилось: {задание}")


def _отправить(клиент, запись: Path, **настройки: Any) -> str:
    with open(запись, "rb") as файл:
        ответ = клиент.post("/api/jobs", files={"file": ("звонок.wav", файл, "audio/wav")},
                            data={"settings": json.dumps(настройки)} if настройки else None)
    assert ответ.status_code == 200, ответ.text
    return ответ.json()["id"]


@pytest.fixture()
def очередь(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с очередью и демо-движком; движок выбирает модель («auto»)."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_RETRY_BACKOFF_S", "0")
    app = create_app(load(), start_queue=True)
    with TestClient(app) as клиент:
        yield клиент


# ---------------------------------------------------------------------------
# Резервная модель
# ---------------------------------------------------------------------------


def test_резервная_модель_подхватывает_задание_когда_основная_не_может(очередь, sample_wav):
    """`model_fallback` обещал «отказ превращается в замедление» — и не читался.

    Основная модель — faster-whisper, движка которого на машине нет. Без
    резервной задание падает, как и прежде; с резервной — встаёт в очередь
    заново уже с ней, заканчивается готовым, а причина остаётся событием
    задания и предупреждением в результате.
    """
    без = _ждать(очередь, _отправить(очередь, sample_wav, model="faster-whisper-small"))
    assert без["status"] == "failed" and без["error_code"] == "dependency_missing", без

    ид = _отправить(очередь, sample_wav, model="faster-whisper-small",
                    model_fallback="demo-simulator")
    задание = _ждать(очередь, ид)
    assert задание["status"] == "completed", задание
    assert задание["model"] == "demo-simulator" and задание["engine"] == "demo", задание
    assert задание["error_code"] is None and задание["error_message"] is None, задание
    виды = [событие["kind"] for событие in задание["events"]]
    assert "model_fallback" in виды, виды
    переход = next(с for с in задание["events"] if с["kind"] == "model_fallback")
    assert "faster-whisper-small" in переход["message"] and "demo-simulator" in переход["message"]

    результат = очередь.get(f"/api/jobs/{ид}/download", params={"fmt": "json"})
    assert результат.status_code == 200, результат.text
    замечания = результат.json().get("warnings") or []
    assert any("резервной моделью demo-simulator" in з and "faster-whisper-small" in з
               for з in замечания), замечания


def test_резервная_модель_не_подменяет_повтор_готового_задания(очередь, sample_wav):
    """Повтор готового задания выбранной моделью при неудаче оставляет прежний результат.

    Переход на резервную здесь был бы подменой: человек просил распознать
    запись именно этой моделью, и результат другой модели он принял бы за
    её работу.
    """
    ид = _отправить(очередь, sample_wav, model_fallback="demo-simulator")
    assert _ждать(очередь, ид)["status"] == "completed"
    _ждать_хвостов(очередь, ид)
    ответ = очередь.post("/api/jobs/rescan", json={
        "ids": [ид], "overrides": {"model": "faster-whisper-small", "engine": "auto"}})
    assert ответ.status_code == 200, ответ.text
    задание = _ждать_события(очередь, ид, "rescan_reverted")
    assert задание["status"] == "completed", задание
    # Откат возвращает и колонки: иначе прежняя расшифровка числилась бы за
    # моделью, которая её не делала.
    assert (задание["model"], задание["engine"]) == ("demo-simulator", "demo"), задание
    виды = [событие["kind"] for событие in задание["events"]]
    assert "model_fallback" not in виды, виды


def _ждать_хвостов(клиент, ид: str, срок: float = 30.0) -> None:
    """Задание «готово», но очередь ещё доделывает своё: разбор, уведомление."""
    очередь = клиент.app.state.hub.queue
    конец = time.time() + срок
    while time.time() < конец and ид in очередь._идут:
        time.sleep(0.05)


def _ждать_события(клиент, ид: str, вид: str, срок: float = 60.0) -> dict[str, Any]:
    конец = time.time() + срок
    задание: dict[str, Any] = {}
    while time.time() < конец:
        задание = клиент.get(f"/api/jobs/{ид}").json()
        if вид in [с["kind"] for с in задание.get("events") or []] and \
                задание["status"] in ("completed", "failed", "cancelled"):
            return задание
        time.sleep(0.1)
    raise AssertionError(f"у задания {ид} нет события {вид}: {задание}")


def test_повтор_сразу_после_готовности_не_теряет_расшифровку(очередь, sample_wav, monkeypatch):
    """«Распознать заново» в первые секунды после «готово» уничтожало расшифровку.

    После отметки «готово» очередь ещё доделывает своё — разбор содержания,
    уведомление. Повтор, нажатый в это время, откладывал только что готовый
    результат в сторону, а доделывающий прогон тут же удалял отложенное как
    «своё»: неудачному повтору откатывать было не к чему, и задание
    оставалось ошибкой без расшифровки.
    """
    import threading

    from asrhub.job_queue import JobQueue

    дошло, отпустить = threading.Event(), threading.Event()
    настоящее = JobQueue._send_webhook

    def медленное(self, job_id):
        дошло.set()
        отпустить.wait(20)
        return настоящее(self, job_id)

    monkeypatch.setattr(JobQueue, "_send_webhook", медленное)
    ид = _отправить(очередь, sample_wav)
    assert дошло.wait(30), "задание не дошло до уведомления"
    assert очередь.get(f"/api/jobs/{ид}").json()["status"] == "completed"
    ответ = очередь.post("/api/jobs/rescan", json={
        "ids": [ид], "overrides": {"model": "faster-whisper-small", "engine": "auto"}})
    assert ответ.status_code == 200, ответ.text
    отпустить.set()
    задание = _ждать_события(очередь, ид, "rescan_reverted")
    assert задание["status"] == "completed", задание
    выгрузка = очередь.get(f"/api/jobs/{ид}/download", params={"fmt": "txt"})
    assert выгрузка.status_code == 200 and выгрузка.text.strip(), выгрузка.text


def test_резервная_модель_с_опечаткой_не_принимается(data_dir):
    """Опечатка в имени резервной модели всплыла бы в день отказа основной."""
    from asrhub.config import load
    from asrhub.errors import ConfigError

    настройки = load()
    with pytest.raises(ConfigError, match="нет в каталоге"):
        настройки.set("model_fallback", "gigaam-v99")
    with pytest.raises(ConfigError, match="нет в каталоге"):
        настройки.merged({"model_fallback": "faster-whisper-smal"})
    настройки.set("model_fallback", "gigaam-v3-ctc")
    настройки.set("model_fallback", "")


def test_готовое_после_повтора_задание_не_показывает_прошлую_ошибку(
        очередь, sample_wav, monkeypatch):
    """Повтор писал ошибку в задание, а удачное завершение её не стирало.

    Готовое задание показывало в карточке красное «engine_error», а
    `asrctl status` — «Ошибка». Причина неудачной попытки остаётся в
    событиях задания.
    """
    from asrhub import job_queue
    from asrhub.errors import EngineError

    настоящий = job_queue.process_job
    попытки = {"n": 0}

    def process_job(*доводы, **ключи):
        попытки["n"] += 1
        if попытки["n"] == 1:
            raise EngineError("Сбой движка на первой попытке")
        return настоящий(*доводы, **ключи)

    monkeypatch.setattr(job_queue, "process_job", process_job)
    задание = _ждать(очередь, _отправить(очередь, sample_wav))
    assert задание["status"] == "completed" and int(задание["retries"]) == 1, задание
    assert задание["error_code"] is None, задание
    assert задание["error_message"] is None and задание["error_hint"] is None, задание
    assert any(с["kind"] == "retry_scheduled" and "первой попытке" in с["message"]
               for с in задание["events"]), задание["events"]


# ---------------------------------------------------------------------------
# Колонки модели и движка
# ---------------------------------------------------------------------------


def test_у_задания_настоящий_движок_а_повтор_другой_моделью_меняет_колонки(
        очередь, sample_wav):
    """Колонка движка хранила «auto», а повтор другой моделью колонок не менял.

    Разрез «по движкам» показывал один столбец «auto» на весь архив, а
    расшифровка, сделанная при повторе новой моделью, приписывалась в
    карточке, статистике моделей и метриках прежней.
    """
    ид = _отправить(очередь, sample_wav)
    задание = _ждать(очередь, ид)
    assert задание["engine"] == "demo", задание["engine"]

    ответ = очередь.post("/api/jobs/rescan", json={
        "ids": [ид], "overrides": {"model": "faster-whisper-small", "engine": "demo"}})
    assert ответ.status_code == 200, ответ.text
    time.sleep(0.3)
    задание = _ждать(очередь, ид)
    assert задание["status"] == "completed", задание
    assert (задание["model"], задание["engine"]) == ("faster-whisper-small", "demo"), задание
    разрез = очередь.get("/api/analytics/engines", params={"period": "week"})
    assert разрез.status_code == 200, разрез.text
    текст = json.dumps(разрез.json(), ensure_ascii=False)
    assert '"demo"' in текст and '"auto"' not in текст, текст


def test_миграция_заменяет_auto_движком_модели(tmp_path: Path):
    """Архив прежних версий: «auto» в колонке меняется на движок модели по каталогу."""
    from asrhub.db import Database

    путь = tmp_path / "asrhub.db"
    Database(путь).close()
    связь = sqlite3.connect(путь)
    связь.execute("INSERT INTO jobs (id, filename, status, model, engine, created_at, updated_at) "
                  "VALUES ('j1', 'a.wav', 'completed', 'gigaam-v3-ctc', 'auto', 1.0, 1.0), "
                  "('j2', 'b.wav', 'completed', 'faster-whisper-small', 'demo', 1.0, 1.0), "
                  "('j3', 'c.wav', 'completed', 'снятая-с-каталога', 'auto', 1.0, 1.0)")
    связь.execute("INSERT INTO model_stats (model, engine) VALUES ('gigaam-v3-ctc', 'auto')")
    связь.execute("PRAGMA user_version=30")
    связь.commit()
    связь.close()

    база = Database(путь)
    try:
        движки = {строка["id"]: строка["engine"] for строка in база.query(
            "SELECT id, engine FROM jobs")}
        статистика = база.query("SELECT engine FROM model_stats WHERE model='gigaam-v3-ctc'")
    finally:
        база.close()
    assert движки == {"j1": "gigaam", "j2": "demo", "j3": "auto"}, движки
    assert статистика[0]["engine"] == "gigaam"


# ---------------------------------------------------------------------------
# Журнал, краткое содержание, формат времени
# ---------------------------------------------------------------------------


def test_текст_расшифровки_попадает_в_журнал_только_с_разрешения(очередь, sample_wav):
    """`log_transcript_text` был в каталоге, а журнал текста не получал ни при каком значении."""
    def строки_журнала(ид: str) -> list[str]:
        ответ = очередь.get("/api/logs", params={"job_id": ид, "limit": 500})
        assert ответ.status_code == 200, ответ.text
        return [str(запись.get("message") or "") for запись in ответ.json()["items"]]

    без = _ждать(очередь, _отправить(очередь, sample_wav))
    assert без["status"] == "completed" and без["text"]
    assert not any("текст расшифровки" in с for с in строки_журнала(без["id"]))

    очередь.app.state.hub.settings.set("log_transcript_text", True)
    # Мимо кеша: запись та же, и из кеша задание не распознаётся вовсе.
    с_текстом = _ждать(очередь, _отправить(очередь, sample_wav, deduplicate_jobs=False))
    записи = [с for с in строки_журнала(с_текстом["id"]) if "текст расшифровки" in с]
    assert записи and с_текстом["text"].split("\n")[0][:30] in записи[0], записи


def test_краткое_содержание_попадает_в_выгрузки(очередь, sample_wav):
    """Выгрузки ждали поле summary, а конвейер его не заполнял никогда."""
    ид = _отправить(очередь, sample_wav, summary_enabled=True, output_formats=["md", "json"])
    assert _ждать(очередь, ид)["status"] == "completed"
    документ = очередь.get(f"/api/jobs/{ид}/download", params={"fmt": "md"}).text
    данные = очередь.get(f"/api/jobs/{ид}/download", params={"fmt": "json"}).json()
    assert данные.get("summary"), данные.keys()
    assert "## Краткое содержание" in документ and данные["summary"] in документ

    без = _отправить(очередь, sample_wav, output_formats=["md", "json"])
    assert _ждать(очередь, без)["status"] == "completed"
    assert "summary" not in очередь.get(f"/api/jobs/{без}/download",
                                        params={"fmt": "json"}).json()


def test_выжимка_берёт_содержательное_и_в_порядке_разговора():
    from asrhub.pipeline.summary import выжимка

    реплики = [
        {"text": "Здравствуйте, у меня не работает интернет с самого утра."},
        {"text": "Угу."},
        {"text": "Понял вас, сейчас проверю линию."},
        {"text": "На линии авария, бригада уже выехала, интернет восстановят к вечеру."},
        {"text": "А за интернет будет перерасчёт? Я работаю из дома."},
        {"text": "Да, перерасчёт за интернет сделаем автоматически."},
    ]
    итог = выжимка(реплики)
    assert итог.startswith("Здравствуйте"), итог
    assert "Угу" not in итог
    assert итог.index("интернет с самого утра") < итог.index("перерасчёт"), итог
    assert выжимка([{"text": "Да."}, {"text": "Нет."}]) == ""

    # Реплики поддержки в кандидаты не попадают вовсе.
    from asrhub.pipeline.summary import предложения
    assert предложения([{"text": "Угу."}, {"text": "Да, конечно."},
                        {"text": "Интернет не работает с самого утра."}]) == [
        "Интернет не работает с самого утра."]

    # Порядок — разговора, а не веса: здесь более «весомое» предложение
    # прозвучало позже, и в выжимке оно всё равно второе.
    реплики = [
        {"text": "Добрый день, компания слушает вас внимательно."},
        {"text": "Здравствуйте, у меня пропал интернет дома."},
        {"text": "Понял вас, сейчас посмотрю линию."},
        {"text": "На линии авария, бригада уже выехала."},
        {"text": "Спасибо, буду ждать звонка."},
        {"text": "Перерасчёт за интернет оформим, интернет вернём, интернет проверим."},
        {"text": "Хорошо, всего доброго."},
        {"text": "До свидания, хорошего вечера вам."},
    ]
    итог = выжимка(реплики)
    assert "авария" in итог and "Перерасчёт" in итог, итог
    assert итог.index("авария") < итог.index("Перерасчёт"), итог


@pytest.mark.parametrize("формат, метка, длительность", [
    ("hms", "**[00:01:05.250]**", "01:23:45.678"),
    ("seconds", "**[65.250]**", "5025.678"),
    ("srt", "**[00:01:05,250]**", "01:23:45,678"),
])
def test_формат_времени_действует_в_markdown(формат: str, метка: str, длительность: str):
    """`timestamp_format` был в каталоге, а Markdown и Word писали ЧЧ:ММ:СС.мс всегда."""
    from asrhub.pipeline import export

    результат = {"meta": {"filename": "a.wav", "duration_s": 5025.678},
                 "segments": [{"start": 65.25, "end": 67.0, "text": "Реплика."}]}
    текст = export.to_markdown(результат, {"timestamp_format": формат})
    assert метка in текст and f"**Длительность:** {длительность}" in текст, текст


def test_формат_времени_действует_в_word(tmp_path: Path):
    pytest.importorskip("docx")
    import docx
    from asrhub.pipeline import export

    результат = {"meta": {"filename": "a.wav", "duration_s": 90.0},
                 "segments": [{"start": 65.25, "end": 67.0, "text": "Реплика."}]}
    путь = export.to_docx(результат, {"timestamp_format": "seconds"}, tmp_path / "a.docx")
    текст = "\n".join(абзац.text for абзац in docx.Document(str(путь)).paragraphs)
    assert "[65.250]" in текст and "90.000" in текст, текст


# ---------------------------------------------------------------------------
# Говорящие
# ---------------------------------------------------------------------------


def _слово(текст: str, начало: float, конец: float) -> dict[str, Any]:
    return {"word": текст, "start": начало, "end": конец, "confidence": 0.9}


def _реплика_с_перебиванием():
    from asrhub.engines.base import Segment

    return [Segment(start=0.0, end=4.0, text="да я согласен нет подождите это не так",
                    words=[_слово("да", 0.0, 0.3), _слово("я", 0.4, 0.5),
                           _слово("согласен", 0.5, 1.2), _слово("нет", 2.0, 2.3),
                           _слово("подождите", 2.3, 3.0), _слово("это", 3.1, 3.3),
                           _слово("не", 3.3, 3.4), _слово("так", 3.4, 3.8)])]


def test_пословная_привязка_режет_реплику_на_смене_говорящего():
    """`diarization_assign_words` ничего не менял: слова размечались всегда, реплика не резалась.

    Перебившего собеседника реплика целиком отдавала тому, кто говорил
    дольше. Теперь с параметром реплика делится на части по сменам, без
    него — один говорящий на реплику и без пословной разметки.
    """
    from asrhub.pipeline.diarization import _assign

    ходы = [(0.0, 1.5, "A"), (1.8, 4.0, "B")]
    части = _assign(_реплика_с_перебиванием(), ходы, пословно=True)
    assert [(ч.speaker, ч.text) for ч in части] == [
        ("Говорящий 1", "да я согласен"), ("Говорящий 2", "нет подождите это не так")]
    assert части[1].start == 2.0 and части[0].end == 1.2

    целиком = _assign(_реплика_с_перебиванием(), ходы, пословно=False)
    assert len(целиком) == 1 and целиком[0].speaker == "Говорящий 1"
    assert not any("speaker" in слово for слово in целиком[0].words)


def test_короткое_угу_собеседника_реплику_не_режет():
    from asrhub.engines.base import Segment
    from asrhub.pipeline.diarization import _assign

    реплика = Segment(start=0.0, end=3.0, text="раз два угу три четыре",
                      words=[_слово("раз", 0.0, 0.5), _слово("два", 0.5, 1.0),
                             _слово("угу", 1.1, 1.3), _слово("три", 1.4, 2.0),
                             _слово("четыре", 2.0, 2.8)])
    части = _assign([реплика], [(0.0, 1.05, "A"), (1.05, 1.35, "B"), (1.35, 3.0, "A")])
    assert len(части) == 1 and части[0].text == "раз два угу три четыре"
    assert части[0].words[2]["speaker"] == "Говорящий 2", "пословная метка потеряна"


def test_параметр_пословной_привязки_доходит_до_разметки(tmp_path: Path, monkeypatch):
    from asrhub.pipeline import diarization

    monkeypatch.setattr(diarization, "_pyannote",
                        lambda путь, настройки: [(0.0, 1.5, "A"), (1.8, 4.0, "B")])
    запись = tmp_path / "a.wav"
    запись.write_bytes(b"")
    по_словам = diarization.diarize_segments(
        запись, _реплика_с_перебиванием(), {"diarization_backend": "pyannote"}, [])
    целиком = diarization.diarize_segments(
        запись, _реплика_с_перебиванием(),
        {"diarization_backend": "pyannote", "diarization_assign_words": False}, [])
    assert len(по_словам) == 2 and len(целиком) == 1


def test_whisperx_не_сливает_первых_двух_говорящих():
    """«SPEAKER_00» и «SPEAKER_01» оба становились «Говорящий 1».

    Номер получался срезанием ведущих нулей, и у первых двух он был
    одинаковым: диалог двух человек выходил у WhisperX монологом.
    """
    from asrhub.engines.whisperx_engine import имя_говорящего

    assert [имя_говорящего(м) for м in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_10")] == [
        "Говорящий 1", "Говорящий 2", "Говорящий 11"]


def test_запасное_разделение_берёт_свою_паузу():
    """`diarization_pause_s` читался, а задать его было негде: config.yaml его отбрасывал."""
    from asrhub.config import load
    from asrhub.engines.base import Segment
    from asrhub.pipeline.diarization import _by_pauses

    настройки = load()
    настройки.set("diarization_pause_s", 3.0)
    реплики = [Segment(start=0.0, end=1.0, text="раз"),
               Segment(start=2.5, end=3.0, text="два"),
               Segment(start=7.0, end=8.0, text="три")]
    размечено = _by_pauses(реплики, настройки.merged({}))
    assert [р.speaker for р in размечено] == ["Говорящий 1", "Говорящий 1", "Говорящий 2"]


# ---------------------------------------------------------------------------
# Постобработка: свои списки
# ---------------------------------------------------------------------------


def test_свои_списки_разбираются_по_строкам_а_не_по_буквам():
    """Строка перебиралась по буквам: «ну, вот» превращалось в набор одиночных букв."""
    from asrhub.pipeline.postprocess import filter_hallucinations, remove_fillers, строки

    assert строки("ну, вот\nкак бы") == ["ну", "вот", "как бы"]
    assert строки("Спасибо за просмотр, друзья\nПодписывайтесь", запятые=False) == [
        "Спасибо за просмотр, друзья", "Подписывайтесь"]
    assert remove_fillers("Ну вот как бы готово", строки("ну, вот")) == "Как бы готово"
    оставлено, убрано = filter_hallucinations(
        [{"text": "Ваш звонок очень важен для нас"}, {"text": "Алло"}],
        строки("Ваш звонок очень важен для нас", запятые=False))
    assert убрано == 1 and [р["text"] for р in оставлено] == ["Алло"]

    # Тот же разбор — в самом конвейере: фраза с запятой не рвётся на части,
    # и слово «друзья» само по себе галлюцинацией не считается.
    from asrhub.pipeline.postprocess import process
    реплики, итог = process(
        [{"text": "Спасибо за просмотр, друзья", "start": 0.0, "end": 1.0},
         {"text": "Друзья, слышно меня?", "start": 1.0, "end": 3.0}],
        {"hallucination_filter": True, "hallucination_phrases": "Спасибо за просмотр, друзья",
         "merge_short_segments": False, "punctuation_enabled": False, "itn_enabled": False},
        engine="faster_whisper")
    assert итог["hallucinations_removed"] == 1, итог
    assert [р["text"] for р in реплики] == ["Друзья, слышно меня?"], реплики


def test_новые_параметры_принимаются_из_config_yaml(tmp_path: Path, monkeypatch):
    """Ключи, которые сервер читал, а config.yaml отбрасывал как неизвестные."""
    from asrhub.config import load

    файл = tmp_path / "config.yaml"
    файл.write_text(
        "diarization_model: pyannote/speaker-diarization-3.1\n"
        "diarization_pause_s: 2.5\n"
        "hallucination_phrases: |\n  Ваш звонок очень важен для нас\n"
        "filler_words: ну, вот\n"
        "align_model: jonatasgrosman/wav2vec2-large-xlsr-53-russian\n"
        "public_url: https://asr.example.ru\n"
        "streaming_latency_ms: 560\n"
        "streaming_chunk_ms: 300\n", encoding="utf-8")
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    настройки = load(файл)
    assert настройки.problems == [], настройки.problems
    assert настройки.get("diarization_model") == "pyannote/speaker-diarization-3.1"
    assert настройки.get("diarization_pause_s") == 2.5
    assert настройки.get("public_url") == "https://asr.example.ru"
    assert "streaming_latency_ms" not in настройки.values


def test_незнакомый_ключ_отсылает_к_тому_что_есть(tmp_path: Path, monkeypatch):
    """Подсказка отправляла к `asrctl config schema` — такой команды нет."""
    from asrhub.config import load

    файл = tmp_path / "config.yaml"
    файл.write_text("такого_параметра_нет: 1\n", encoding="utf-8")
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    проблемы = " ".join(load(файл).problems)
    assert "такого_параметра_нет" in проблемы and "GET /api/params" in проблемы
    assert "config schema" not in проблемы


# ---------------------------------------------------------------------------
# Внешний адрес, видеопамять, движки
# ---------------------------------------------------------------------------


def test_внешний_адрес_задаётся_и_попадает_в_ссылку_crm(data_dir, monkeypatch):
    """`public_url` читался CRM, а задать его было нельзя ни файлом, ни переменной."""
    from asrhub import crm
    from asrhub.config import load
    from asrhub.errors import ConfigError

    monkeypatch.setenv("ASRHUB_PUBLIC_URL", "https://asr.example.ru")
    настройки = load()
    assert настройки.get("public_url") == "https://asr.example.ru"
    данные = crm.собрать({"id": "job_1"}, {}, {},
                         base_url=str(настройки.get("public_url")))
    assert данные["link"] == "https://asr.example.ru/#/jobs/job_1"
    with pytest.raises(ConfigError):
        настройки.set("public_url", "asr.example.ru")


def test_доля_видеопамяти_доходит_до_torch(monkeypatch):
    """`gpu_memory_fraction` не передавался в torch: процесс занимал карту сколько хотел."""
    import sys
    import types

    from asrhub.engines import base

    вызовы: list[tuple[float, int]] = []
    cuda = types.SimpleNamespace(
        is_available=lambda: True, current_device=lambda: 0,
        set_per_process_memory_fraction=lambda доля, карта: вызовы.append((доля, карта)))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=cuda))
    assert base.ограничить_видеопамять("cuda:1", {"gpu_memory_fraction": 0.5}) == 0.5
    assert base.ограничить_видеопамять("cuda", {"gpu_memory_fraction": 0.7}) == 0.7
    assert base.ограничить_видеопамять("cpu", {"gpu_memory_fraction": 0.5}) is None
    assert вызовы == [(0.5, 1), (0.7, 0)]

    class _Движок(base.Engine):
        id = "проба"

        def _load(self, settings):
            return object()

        def _transcribe(self, audio_path, settings, progress):
            raise AssertionError

    monkeypatch.setattr(base, "проверить_ускоритель", lambda устройство: (True, ""))
    from asrhub.catalog import get_model

    движок = _Движок(get_model("demo-simulator"), {})
    движок.ensure_loaded({"device": "cuda:0", "gpu_memory_fraction": 0.4})
    assert вызовы[-1] == (0.4, 0), "загрузка модели долю не поставила"


def test_списки_движков_у_параметров_правдивы():
    """Параметр, числившийся за движком, должен этим движком читаться.

    WhisperX и NeMo числились за beam_size, GigaAM — за batch_size, Vosk и
    T-one — за cpu_threads, а их код этих ключей не открывал: интерфейс
    показывал настройку, которая с выбранной моделью не делала ничего.
    Фильтр галлюцинаций — не параметр движка: его читает постобработка по
    тому же списку.
    """
    import inspect

    from asrhub.catalog import PARAMS
    from asrhub.engines import ENGINE_CLASSES

    через_базу = {"compute_type": "resolve_compute_type", "device": "resolve_device",
                  "language": "language_for"}
    ложные = []
    for параметр in PARAMS:
        if параметр.key == "hallucination_filter" or параметр.key.startswith("hallucination_"):
            continue
        for движок in параметр.engines or []:
            код = inspect.getsource(ENGINE_CLASSES[движок]) if движок in ENGINE_CLASSES else ""
            if f'"{параметр.key}"' in код or через_базу.get(параметр.key, "\0") in код:
                continue
            ложные.append(f"{параметр.key} → {движок}")
    assert not ложные, ложные


def test_whisper_получает_подавление_токенов(tmp_path: Path):
    """suppress_blank и suppress_tokens числились за openai-whisper, а в transcribe не шли."""
    from asrhub.catalog import get_model
    from asrhub.engines.whisper_engine import WhisperEngine

    переданное: dict[str, Any] = {}

    class _Модель:
        def transcribe(self, путь, **опции):
            переданное.update(опции)
            return {"segments": [], "language": "ru"}

    движок = WhisperEngine(get_model("whisper-large-v3"), {})
    движок._model = _Модель()
    движок._transcribe(tmp_path / "a.wav", {"suppress_blank": False,
                                            "suppress_tokens": "1, 2"}, None)
    assert переданное["suppress_blank"] is False and переданное["suppress_tokens"] == "1,2"
    движок._transcribe(tmp_path / "a.wav", {"suppress_tokens": ""}, None)
    assert переданное["suppress_tokens"] is None, "пустой список — «ничего не подавлять»"


def test_omnilingual_понимает_все_языки_из_списка(sample_wav: Path):
    """Немецкая запись распознавалась Omnilingual как русская: язык вне четырёх известных."""
    from asrhub.catalog import MODELS
    from asrhub.engines.misc_engines import OmnilingualEngine

    переданное: list[str] = []

    class _Модель:
        def transcribe(self, пути, lang, batch_size):
            переданное.extend(lang)
            return ["текст"]

    spec = next(м for м in MODELS if м.engine == "omnilingual")
    движок = OmnilingualEngine(spec, {})
    движок._model = _Модель()
    for язык in ("de", "fr", "ja", "ru"):
        движок._transcribe(sample_wav, {"language": язык}, None)
    assert переданное == ["deu_Latn", "fra_Latn", "jpn_Jpan", "rus_Cyrl"], переданное

    # Каждый язык из списка параметра «Язык» — свой код, а не русский.
    from asrhub.catalog import PARAMS_BY_KEY
    языки = [в["value"] for в in PARAMS_BY_KEY["language"].options if в["value"] != "auto"]
    переданное.clear()
    for язык in языки:
        движок._transcribe(sample_wav, {"language": язык}, None)
    assert len(переданное) == len(языки), переданное
    чужие = [(я, к) for я, к in zip(языки, переданное, strict=True)
             if я != "ru" and к == "rus_Cyrl"]
    assert not чужие, чужие


# ---------------------------------------------------------------------------
# Поток
# ---------------------------------------------------------------------------


class _ОконныйДвижок:
    """Движок без потока: распознаёт кусок целиком и считает вызовы."""

    def __init__(self) -> None:
        self.вызовы = 0

    def transcribe(self, path, settings, progress):
        self.вызовы += 1
        return type("Р", (), {"segments": [type("С", (), {"text": f"фраза {self.вызовы}"})()]})()


class _ОконныйРеестр:
    def __init__(self, движок) -> None:
        self.движок = движок

    def get(self, settings):
        return self.движок

    def lease(self, settings, **_):
        import contextlib

        return contextlib.nullcontext(self.движок)


def _тишина(секунд: float) -> bytes:
    from asrhub.streaming import SAMPLE_RATE

    return b"\x00\x00" * int(секунд * SAMPLE_RATE)


def _поток(tmp_path: Path, **настройки: Any):
    from asrhub.streaming import StreamSession

    движок = _ОконныйДвижок()
    сессия = StreamSession(_ОконныйРеестр(движок), {"stream_window_s": 4.0, **настройки},
                           workdir=tmp_path)
    сессия.start()
    return сессия, движок


def test_фраза_закрепляется_по_паузе_а_не_через_полминуты(tmp_path: Path):
    """`streaming_endpoint_silence_ms` был в каталоге и не читался.

    Движок без потока закреплял фразу только тридцатисекундным хвостом:
    сказанное полминуты висело черновиком. Теперь пауза после речи
    закрепляет фразу сразу — одним прогоном модели.
    """
    from asrhub.streaming import tone

    сессия, движок = _поток(tmp_path, streaming_endpoint_silence_ms=600)
    события = [с.type for с in сессия.feed(tone(1.5))]
    события += [с.type for с in сессия.feed(_тишина(0.8))]
    assert события == ["final"], события
    assert движок.вызовы == 1
    # Тишина после закрепления фразы новой не заканчивает.
    assert [с.type for с in сессия.feed(_тишина(1.0))] == []
    сессия.close()

    сессия, _ = _поток(tmp_path, streaming_endpoint_silence_ms=0)
    события = [с.type for с in сессия.feed(tone(1.5))] + \
        [с.type for с in сессия.feed(_тишина(0.8))]
    assert "final" not in события, "без паузы конца фразы закрепление не ждёт окна"
    сессия.close()


def test_без_промежуточных_окно_не_считается_зря(tmp_path: Path):
    """`streaming_partial_results` выключен — черновиков нет, и модель их не считает."""
    from asrhub.streaming import tone

    сессия, движок = _поток(tmp_path, streaming_partial_results=False,
                            streaming_endpoint_silence_ms=0)
    события = []
    for _ in range(4):
        события += [с.type for с in сессия.feed(tone(2.0))]
    assert "partial" not in события and движок.вызовы == 0, (события, движок.вызовы)
    события += [с.type for с in сессия.finish()]
    assert события[-2:] == ["final", "done"] and движок.вызовы == 1
    сессия.close()

    сессия, движок = _поток(tmp_path, streaming_endpoint_silence_ms=0)
    события = []
    for _ in range(4):
        события += [с.type for с in сессия.feed(tone(2.0))]
    assert события.count("partial") >= 1 and движок.вызовы >= 1
    сессия.close()


def test_настоящий_поток_без_промежуточных_отдаёт_только_закреплённое(tmp_path: Path):
    from asrhub.streaming import StreamSession, tone

    class _Поток:
        def __init__(self) -> None:
            self.куски = 0

        def accept(self, pcm):
            self.куски += 1
            return ("final", "фраза") if self.куски % 3 == 0 else ("partial", f"черновик {self.куски}")

        def finish(self):
            return ""

    class _Движок:
        def stream_session(self, settings):
            return _Поток()

    class _Реестр:
        def get(self, settings):
            return _Движок()

    сессия = StreamSession(_Реестр(), {"streaming_partial_results": False}, workdir=tmp_path)
    готов = сессия.start().to_dict()
    assert готов["mode"] == "native" and готов["partials"] is False
    события = []
    for _ in range(6):
        события += [с.type for с in сессия.feed(tone(0.2))]
    assert события == ["final", "final"], события
    сессия.close()
