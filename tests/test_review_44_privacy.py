"""Заход 44: персональные данные после удаления записи.

Удаление (кнопкой, пакетом, по сроку, по требованию субъекта) убирало само
задание и разбор, но оставляло: строку журнала звонков с номером и именем
звонящего, проверку оператора, пересказ разговора в кеше модели, слова
реплик в поисковом указателе (и в резервных копиях), копию записи с
заглушёнными данными рядом с исходником и файлы, на которые база больше не
ссылается. Удаление по требованию не находило звонок по номеру, если номер
не произнесли вслух.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

from asrhub import maintenance
from asrhub.db import Database

НОМЕР = "79161234567"


def _задание(db: Database, ид: str = "j1", *, текст: str = "Здравствуйте, чем помочь?",
             **поля) -> str:
    db.create_job({"id": ид, "filename": f"{ид}.wav", "status": "completed",
                   "text": текст, **поля})
    db.save_segments(ид, [{"start": 0.0, "end": 3.0, "text": текст, "speaker": "Клиент"}])
    return ид


def _в_указателе(db: Database, слово: str) -> bool:
    """Есть ли слово физически в файлах указателя — а не в ответах поиска."""
    return any(слово.encode() in bytes(r[0] or b"")
               for r in db.query("SELECT block FROM segments_fts_data"))


# ---------------------------------------------------------------------------
# Удаление задания
# ---------------------------------------------------------------------------


def test_удаление_задания_не_оставляет_звонок_проверку_и_пересказ(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    _задание(db)
    db.save_call("pbx:1789.1", job_id="j1", src=НОМЕР, dst="101",
                 clid=f'"Иванов Иван" <{НОМЕР}>', recording=f"/var/spool/out-{НОМЕР}.wav",
                 station="pbx", queue="продажи", agent="101", billsec=60)
    assert db.qa_assign("j1", agent="101")
    db.llm_cache_put("ключ", "main", "модель", f"Клиент Иванов, номер {НОМЕР}", 1.0,
                     job_id="j1")
    db.delete_job("j1")
    звонок = db.query_one("SELECT * FROM calls WHERE uniqueid='pbx:1789.1'")
    assert звонок is not None, "без строки импорт завёл бы звонок заново"
    assert (звонок["src"], звонок["clid"], звонок["recording"]) == ("", "", "")
    assert (звонок["queue"], звонок["agent"], звонок["billsec"]) == ("продажи", "101", 60)
    assert db.query("SELECT * FROM qa_reviews") == []
    assert db.query("SELECT * FROM llm_cache") == []
    assert db.get_kv("fts.dirty"), "уборка не узнает, что указатель пора вычистить"


def test_суточная_уборка_подметает_следы(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    _задание(db, "живое")
    # Следы прежних версий: строки без задания.
    db.execute("INSERT INTO qa_reviews (job_id, status, assigned_at) "
               "VALUES ('пропавшее', 'pending', 1)")
    db.execute("INSERT INTO llm_cache (key, response, created_at, job_id) "
               "VALUES ('к', 'пересказ', 1, 'пропавшее')")
    db.save_call("pbx:1", job_id="пропавшее", src=НОМЕР, clid="Иванов")
    db.save_call("pbx:2", job_id="живое", src="79160000000")
    # Форма слова, которой не осталось ни в одной записи, — и живая.
    db.execute("INSERT INTO content_terms (job_id, stem, n) VALUES ('живое', 'поставк', 1)")
    db.execute("INSERT INTO content_vocab (stem, word, n) VALUES ('поставк', 'поставки', 1)")
    db.execute("INSERT INTO content_vocab (stem, word, n) VALUES ('иванов', 'Иванов', 1)")
    итог = db.sweep_orphans()
    assert итог["rows"] >= 2 and итог["calls"] == 1 and итог["vocab"] == 1, итог
    assert db.query("SELECT * FROM qa_reviews") == []
    assert db.query("SELECT * FROM llm_cache") == []
    assert db.query_one("SELECT src FROM calls WHERE uniqueid='pbx:1'")["src"] == ""
    assert db.query_one("SELECT src FROM calls WHERE uniqueid='pbx:2'")["src"] == "79160000000"
    assert [r["stem"] for r in db.query("SELECT stem FROM content_vocab")] == ["поставк"]


def test_указатель_физически_забывает_удалённое(tmp_path: Path):
    db = Database(tmp_path / "a.db")
    for и in range(30):
        _задание(db, f"j{и}", текст=f"обычный разговор номер {и} про доставку")
    _задание(db, "стереть", текст="продиктую номер qzxwvjklm для связи")
    db.delete_job("стереть")
    assert _в_указателе(db, "qzxwvjklm"), "проверка ничего не проверяет"
    assert db.sweep_orphans()["fts"] is True
    assert not _в_указателе(db, "qzxwvjklm"), \
        "слово удалённой реплики осталось в файле базы и уедет в копии"
    assert db.get_kv("fts.dirty") is None
    # Поиск по живым записям после слияния работает.
    assert len(db.jobs_matching("доставку")) == 30


def test_без_удалений_указатель_не_перестраивается(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "a.db")
    _задание(db)
    вызовы = []
    monkeypatch.setattr(db, "fts_purge", lambda **k: вызовы.append(k) or True)
    db.sweep_orphans()
    assert вызовы == []


def test_отредактированная_копия_удаляется_с_заданием(client, data_dir: Path):
    db = client.app.state.hub.db
    загрузки = data_dir / "uploads"
    исходник = загрузки / "up_проба.wav"
    копия = загрузки / "up_проба.redacted.wav"
    for файл in (исходник, копия):
        файл.write_bytes(b"RIFF....WAVE")
    _задание(db, "j1", file_path=str(исходник))
    ответ = client.delete("/api/jobs/j1")
    assert ответ.status_code == 200, ответ.text
    assert not исходник.exists()
    assert not копия.exists(), "голос клиента остался на диске"


def test_уборка_по_сроку_удаляет_и_отредактированную_копию(tmp_path: Path):
    db = Database(tmp_path / "data" / "asrhub.db")
    загрузки = tmp_path / "data" / "uploads"
    загрузки.mkdir(parents=True)
    исходник = загрузки / "up_x.wav"
    копия = загрузки / "up_x.redacted.wav"
    for файл in (исходник, копия):
        файл.write_bytes(b"RIFF")
    _задание(db, "старое", file_path=str(исходник))
    db.execute("UPDATE jobs SET finished_at=1000 WHERE id='старое'")
    db.cleanup(results_days=30)
    assert not исходник.exists() and not копия.exists()


# ---------------------------------------------------------------------------
# Удаление по требованию
# ---------------------------------------------------------------------------


def _звонок_без_номера_в_тексте(db: Database) -> None:
    _задание(db, "звонок", текст="Добрый день, по заказу всё в порядке.")
    db.execute("UPDATE jobs SET filename='1789145000.12.wav' WHERE id='звонок'")
    db.save_call("pbx:1789145000.12", job_id="звонок", src=f"+{НОМЕР}",
                 clid=f'"Петров" <8{НОМЕР[1:]}>', station="pbx")
    # Пропущенный звонок того же человека — записи нет, номер есть.
    db.save_call("pbx:1789145999.1", src=f"8 ({НОМЕР[1:4]}) {НОМЕР[4:7]}-{НОМЕР[7:9]}-{НОМЕР[9:]}",
                 station="pbx", skipped="не отвечен")
    db.save_call("pbx:1789146000.1", src="79990000000", station="pbx", skipped="короткий")


def test_удаление_по_требованию_находит_звонок_по_номеру(client):
    db = client.app.state.hub.db
    _звонок_без_номера_в_тексте(db)
    db.llm_cache_put("старый-ключ", "main", "модель", f"Перезвонить на {НОМЕР}", 1.0)
    запрос = "+7 (916) 123-45-67"
    проба = client.post("/api/maintenance/erase", json={"query": запрос, "dry_run": True})
    assert проба.status_code == 200, проба.text
    assert проба.json()["ids"] == ["звонок"] and проба.json()["calls"] == 2
    итог = client.post("/api/maintenance/erase", json={"query": запрос, "dry_run": False})
    assert итог.status_code == 200, итог.text
    тело = итог.json()
    assert тело["deleted"] == 1 and тело["calls"] == 2 and тело["llm_cache"] == 1
    assert тело["index_purged"] is True, "указатель вычищается сразу, а не к утру"
    assert db.get_job("звонок") is None
    номера = {r["uniqueid"]: r["src"] for r in db.query("SELECT uniqueid, src FROM calls")}
    assert номера == {"pbx:1789145000.12": "", "pbx:1789145999.1": "",
                      "pbx:1789146000.1": "79990000000"}, номера
    assert db.query("SELECT * FROM llm_cache") == []


def test_удаление_по_требованию_по_имени_звонящего(client):
    db = client.app.state.hub.db
    _звонок_без_номера_в_тексте(db)
    ответ = client.post("/api/maintenance/erase", json={"query": "петров", "dry_run": True})
    assert ответ.json()["ids"] == ["звонок"]


def test_короткий_номер_не_ищется_по_цифрам(tmp_path: Path):
    """«12345» нашлось бы в сотне чужих номеров."""
    db = Database(tmp_path / "a.db")
    db.save_call("pbx:1", src="79161234567", station="pbx")
    assert db.erase_calls_count("12345") == 0
    assert db.erase_calls_count("1234567") == 1


# ---------------------------------------------------------------------------
# Кеш модели знает свою запись
# ---------------------------------------------------------------------------


def test_ответы_модели_кешируются_за_записью(tmp_path: Path):
    from asrhub.llm.client import LLMClient
    from asrhub.llm.worker import LLMWorker
    from test_llm import _архив, _Настройки  # noqa: PLC0415

    db = _архив(tmp_path)
    настройки = _Настройки()
    клиент = LLMClient(настройки, db)
    LLMWorker(db, настройки, клиент).analyze_job("l0")
    чьи = {r["job_id"] for r in db.query("SELECT job_id FROM llm_cache")}
    assert чьи == {"l0"}, чьи
    клиент.chat("система", "вопрос без записи", kind="test", json_mode=False)
    assert db.query_one("SELECT COUNT(*) FROM llm_cache WHERE job_id IS NULL")[0] == 1
    db.delete_job("l0")
    assert db.query_one("SELECT COUNT(*) FROM llm_cache WHERE job_id='l0'")[0] == 0


# ---------------------------------------------------------------------------
# Файлы без задания — в карантин
# ---------------------------------------------------------------------------


def _раскладка(tmp_path: Path) -> SimpleNamespace:
    данные = tmp_path / "data"
    пути = SimpleNamespace(data=данные, uploads=данные / "uploads",
                           results=данные / "results", tmp=данные / "tmp")
    for каталог in (пути.uploads, пути.results):
        каталог.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(paths=пути, get=lambda ключ, по_умолчанию=None: по_умолчанию)


def _старый(путь: Path, дней: float = 2.0) -> Path:
    момент = time.time() - дней * 86400
    os.utime(путь, (момент, момент))
    return путь


def test_файлы_без_задания_уходят_в_карантин(tmp_path: Path):
    настройки = _раскладка(tmp_path)
    db = Database(настройки.paths.data / "asrhub.db")
    загрузки, результаты = настройки.paths.uploads, настройки.paths.results
    свой = загрузки / "up_свой.wav"
    свой.write_bytes(b"1")
    _старый(свой)
    _задание(db, "j1", file_path=str(свой), result_path=str(результаты / "j1"))
    (результаты / "j1").mkdir()
    (результаты / "j1.prev").mkdir()
    for путь in (загрузки / "up_свой.redacted.wav", загрузки / "up_чужой.wav",
                 загрузки / "up_свежий.wav"):
        путь.write_bytes(b"1")
    _старый(загрузки / "up_свой.redacted.wav")
    _старый(загрузки / "up_чужой.wav")
    сирота = результаты / "job_после_копии"
    сирота.mkdir()
    (сирота / "result.json").write_text("{}")
    _старый(сирота)
    _старый(результаты / "j1")
    _старый(результаты / "j1.prev")
    древний = настройки.paths.data / "orphans" / "2000-01-01"
    древний.mkdir(parents=True)

    итог = maintenance.подмести_файлы(db, настройки)
    assert итог["moved"] == 2 and итог["expired"] == 1, итог
    карантин = настройки.paths.data / "orphans" / time.strftime("%Y-%m-%d")
    assert (карантин / "uploads" / "up_чужой.wav").is_file()
    assert (карантин / "results" / "job_после_копии" / "result.json").is_file()
    for живой in (свой, загрузки / "up_свой.redacted.wav", загрузки / "up_свежий.wav",
                  результаты / "j1", результаты / "j1.prev"):
        assert живой.exists(), f"уборка унесла {живой.name}"
    assert not древний.exists(), "карантин не истекает"
    assert (настройки.paths.data / "orphans").stat().st_mode & 0o777 == 0o700


def test_чужая_база_не_уносит_архив_в_карантин(tmp_path: Path):
    """Сирот больше половины — так выглядит не мусор, а не та база."""
    настройки = _раскладка(tmp_path)
    db = Database(настройки.paths.data / "asrhub.db")
    for номер in range(30):
        (настройки.paths.uploads / f"up_{номер}.wav").write_bytes(b"1")
        _старый(настройки.paths.uploads / f"up_{номер}.wav")
    итог = maintenance.подмести_файлы(db, настройки)
    assert итог["moved"] == 0 and "больше половины" in итог["skipped"]
    assert len(list(настройки.paths.uploads.iterdir())) == 30
    assert any(e["kind"] == "orphans_skipped" for e in db.get_events())


def test_суточная_уборка_идёт_по_расписанию(tmp_path: Path, monkeypatch):
    настройки = _раскладка(tmp_path)
    db = Database(настройки.paths.data / "asrhub.db")
    db.set_kv(maintenance.KV_SWEEP, time.time() - 25 * 3600)
    from asrhub import backup

    monkeypatch.setattr(backup, "пора", lambda *a, **k: False)
    сделано = maintenance.run_scheduled(db, _Все(настройки), analytics=None)
    assert "sweep" in сделано and "orphan_files" in сделано, сделано
    assert maintenance.run_scheduled(db, _Все(настройки), analytics=None).get("sweep") is None


class _Все:
    """Настройки для служебного захода: всё выключено, кроме уборки."""

    def __init__(self, раскладка: SimpleNamespace):
        self.paths = раскладка.paths

    def get(self, ключ, по_умолчанию=None):
        return {"review_enabled": False, "qa_enabled": False}.get(ключ, по_умолчанию)
