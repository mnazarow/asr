"""Очередь запросов к языковой модели: порядок, живучесть и управление.

Раньше очередь смыслового разбора жила в памяти процесса — обычная
`queue.Queue`. Это стоило трёх вещей сразу:

* перезапуск сервера терял её целиком: тысяча записей, поставленных на
  разбор, просто исчезала, и узнавали об этом через неделю по дырам в
  аналитике;
* её не было видно: «в очереди 812» — всё, что мог показать раздел, а на
  вопрос «что именно сейчас жуёт видеокарту» ответа не было;
* ею нельзя было управлять: у `queue.Queue` из управления есть только
  «положить».

Теперь очередь — таблица в базе, и проверки здесь ровно про это: порядок
выбора, единственность идущего запроса, возврат в строй после перезапуска,
пауза, повтор упавших и отсутствие двойников.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from asrhub.db import Database
from asrhub.llm.client import LLMClient
from asrhub.llm.worker import LLMWorker, wait_idle


class Настройки:
    """Настройки сервера в объёме, который нужен очереди."""

    def __init__(self, **значения: Any):
        self.значения = {
            "llm_backend": "stub", "llm_model": "test", "llm_auto": True,
            "llm_backfill": False, "llm_yield_to_queue": False,
            "llm_queue_paused": False, "llm_max_concurrent": 1,
            "llm_queue_batch": 5, "llm_queue_idle_s": 1,
            "llm_queue_cooldown_s": 1800, "llm_tasks": ["summary", "outcome"],
            **значения,
        }

    def get(self, ключ: str, по_умолчанию: Any = None) -> Any:
        return self.значения.get(ключ, по_умолчанию)


def база(tmp_path: Path, записей: int = 3) -> Database:
    """База с несколькими готовыми заданиями и расшифровками."""
    db = Database(tmp_path / "asrhub.db")
    for i in range(записей):
        job_id = db.create_job({"id": f"job{i}", "filename": f"запись-{i}.wav",
                                "owner": "анна", "model": "demo", "engine": "demo",
                                "media_duration_s": 30.0})
        db.update_job(job_id, status="completed", finished_at=time.time(),
                      text=f"Здравствуйте, это разговор номер {i}. Спасибо, до свидания.")
        db.save_segments(job_id, [
            {"start": 0.0, "end": 4.0, "speaker": "SPEAKER_00",
             "text": "Здравствуйте, у меня вопрос по заказу."},
            {"start": 4.0, "end": 9.0, "speaker": "SPEAKER_01",
             "text": "Сейчас посмотрю и перезвоню вам завтра."}])
    return db


# ---------------------------------------------------------------------------
# Порядок и единственность
# ---------------------------------------------------------------------------

def test_из_очереди_берут_по_важности_а_при_равной_по_очереди(tmp_path):
    """Разбор по кнопке обгоняет фоновый, но не отменяет справедливости.

    Порядок задуман так: разбор по просьбе (70) обгоняет свежую запись
    (50), а та — разбор архива (30). Внутри одной важности — кто дольше
    ждёт, тот и первый: без этого запись, поставленная утром, могла бы
    ждать вечно, пока подсыпаются новые.
    """
    db = база(tmp_path)
    db.llmq_put("job0", kind="архив", priority=30)
    time.sleep(0.01)
    db.llmq_put("job1", kind="свежая запись", priority=50)
    time.sleep(0.01)
    db.llmq_put("job2", kind="свежая запись", priority=50)

    assert db.llmq_take()["job_id"] == "job1", "первой берут ту, что ждёт дольше"
    assert db.llmq_take()["job_id"] == "job2"
    assert db.llmq_take()["job_id"] == "job0", "архив идёт последним"
    assert db.llmq_take() is None
    db.close()


def test_одну_запись_не_возьмут_двое(tmp_path):
    """Отбор и пометка — одним действием под замком записи.

    Фоновый поток и разбор по кнопке ходят к очереди одновременно. Если бы
    выбор и пометка «идёт» были двумя шагами, оба взяли бы одну запись и
    сходили бы к модели дважды: двойная плата временем видеокарты за один
    и тот же ответ.
    """
    db = база(tmp_path, записей=40)
    for i in range(40):
        db.llmq_put(f"job{i}")

    взятые: list[str] = []
    замок = threading.Lock()

    def брать() -> None:
        while True:
            строка = db.llmq_take()
            if строка is None:
                return
            with замок:
                взятые.append(str(строка["job_id"]))

    потоки = [threading.Thread(target=брать) for _ in range(4)]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join(timeout=20)

    assert len(взятые) == 40
    assert len(set(взятые)) == 40, f"одну запись взяли дважды: {len(взятые) - len(set(взятые))}"
    db.close()


def test_повторная_постановка_не_задваивает_а_поднимает(tmp_path):
    """Нажатие «разобрать сейчас» по записи из хвоста двигает её, а не клонирует."""
    db = база(tmp_path)
    db.llmq_put("job0", kind="архив", priority=30)
    db.llmq_put("job0", kind="по просьбе", priority=70)

    очередь = db.llmq_list()
    assert очередь["total"] == 1, "запись в очереди задвоилась"
    assert очередь["items"][0]["priority"] == 70
    assert очередь["items"][0]["kind"] == "по просьбе"
    db.close()


def test_идущую_запись_повторная_постановка_не_трогает(tmp_path):
    """Пока модель отвечает, менять строку нельзя: ответ придёт и запишется в неё."""
    db = база(tmp_path)
    db.llmq_put("job0")
    db.llmq_take()
    assert db.llmq_put("job0", priority=100) is False
    assert db.llmq_counts()[db.LLMQ_ИДЁТ] == 1
    db.close()


# ---------------------------------------------------------------------------
# Живучесть
# ---------------------------------------------------------------------------

def test_после_перезапуска_идущая_запись_возвращается_в_очередь(tmp_path):
    """Сервер перезапустили посреди ответа модели — запись не должна пропасть.

    Без этого возврата строка осталась бы «идущей» навсегда: раздел
    показывал бы вечный текущий запрос, а сама запись не разобралась бы
    уже никогда — её больше никто не выберет.
    """
    db = база(tmp_path)
    db.llmq_put("job0")
    db.llmq_put("job1")
    db.llmq_take()
    assert db.llmq_counts()[db.LLMQ_ИДЁТ] == 1

    вернулось = db.llmq_reset_running()

    assert вернулось == 1
    assert db.llmq_counts()[db.LLMQ_ИДЁТ] == 0
    assert db.llmq_counts()[db.LLMQ_ЖДЁТ] == 2
    db.close()


def test_поток_поднимает_очередь_из_базы_а_не_из_памяти(tmp_path):
    """Записи, поставленные до запуска потока, разбираются после него.

    Это и есть смысл переноса очереди в базу: она переживает не только
    перезапуск потока, но и перезапуск всего сервера.
    """
    db = база(tmp_path)
    настройки = Настройки()
    for i in range(3):
        db.llmq_put(f"job{i}")

    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток.start()
    assert wait_idle(поток, timeout=30), "очередь не разошлась"
    поток.stop()

    счёт = db.llmq_counts()
    assert счёт[db.LLMQ_ГОТОВО] == 3, счёт
    assert all(db.llm_get(f"job{i}") for i in range(3))
    db.close()


def test_пауза_останавливает_разбор_но_не_теряет_очередь(tmp_path):
    """Пауза — это «подожди», а не «забудь»."""
    db = база(tmp_path)
    настройки = Настройки(llm_queue_paused=True)
    db.llmq_put("job0")

    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток.start()
    time.sleep(1.5)
    assert db.llmq_counts()[db.LLMQ_ЖДЁТ] == 1, "разбор пошёл при поставленной паузе"
    assert поток.done == 0

    настройки.значения["llm_queue_paused"] = False
    assert wait_idle(поток, timeout=30), "после снятия паузы очередь не пошла"
    поток.stop()
    assert db.llmq_counts()[db.LLMQ_ГОТОВО] == 1
    db.close()


def test_разбор_архива_виден_в_очереди(tmp_path):
    """Фоновый разбор архива не должен выглядеть пустотой.

    Раньше он брал записи мимо очереди, и раздел показывал ноль ждущих при
    работающей видеокарте: понять, кто её занял, было нельзя.
    """
    db = база(tmp_path, записей=2)
    настройки = Настройки(llm_auto=False, llm_backfill=True)
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))

    job_id = поток._next()

    assert job_id in {"job0", "job1"}
    строка = db.llmq_list()["items"][0]
    assert строка["kind"] == "архив"
    assert строка["state"] == db.LLMQ_ИДЁТ
    db.close()


# ---------------------------------------------------------------------------
# Учёт и управление
# ---------------------------------------------------------------------------

def test_завершение_пишет_время_ответа_и_ошибку(tmp_path):
    """По этим числам раздел строит график и находит затыки."""
    db = база(tmp_path)
    db.llmq_put("job0")
    db.llmq_take()
    db.llmq_finish("job0", latency_ms=1234, calls=2, chunks=1)
    db.llmq_put("job1")
    db.llmq_take()
    db.llmq_finish("job1", error="сервер модели не ответил")

    свод = db.llmq_stats(0)
    assert свод["n"] == 2 and свод["ok"] == 1 and свод["failed"] == 1
    assert свод["avg_ms"] == pytest.approx(1234)
    строки = {с["job_id"]: с for с in db.llmq_list()["items"]}
    assert строки["job1"]["state"] == db.LLMQ_ОШИБКА
    assert "не ответил" in строки["job1"]["error"]
    db.close()


def test_повтор_упавших_возвращает_их_в_очередь(tmp_path):
    """Сервер модели полежал час — записи не должны остаться брошенными."""
    db = база(tmp_path)
    for i in range(2):
        db.llmq_put(f"job{i}")
        db.llmq_take()
        db.llmq_finish(f"job{i}", error="сервер лёг")

    повторено = db.llmq_retry_failed()

    assert повторено == 2
    assert db.llmq_counts()[db.LLMQ_ЖДЁТ] == 2
    assert db.llmq_counts()[db.LLMQ_ОШИБКА] == 0
    db.close()


def test_очистка_не_трогает_идущую_запись(tmp_path):
    """Ответ модели уже оплачен временем видеокарты — бросать его на полпути незачем."""
    db = база(tmp_path)
    db.llmq_put("job0")
    db.llmq_put("job1")
    db.llmq_take()

    убрано = db.llmq_clear()

    assert убрано == 1
    assert db.llmq_counts()[db.LLMQ_ИДЁТ] == 1
    db.close()


def test_отмена_снимает_только_ждущую(tmp_path):
    db = база(tmp_path)
    db.llmq_put("job0")
    db.llmq_put("job1")
    db.llmq_take()                      # job0 пошла в работу
    assert db.llmq_cancel("job1") is True
    assert db.llmq_cancel("job0") is False, "идущую отменять нечем: ответ уже запрошен"
    db.close()


def test_старая_история_подчищается_а_свежая_остаётся(tmp_path):
    """История нужна для графика, но не вечно."""
    db = база(tmp_path, записей=2)
    db.llmq_put("job0")
    db.llmq_take()
    db.llmq_finish("job0", latency_ms=100)
    db.execute("UPDATE llm_queue SET finished_at=? WHERE job_id=?",
               (time.time() - 40 * 86400, "job0"))
    db.llmq_put("job1")
    db.llmq_take()
    db.llmq_finish("job1", latency_ms=100)

    убрано = db.llmq_prune(keep_days=14)

    assert убрано == 1
    оставшиеся = {с["job_id"] for с in db.llmq_list()["items"]}
    assert оставшиеся == {"job1"}
    db.close()


def test_разбор_по_кнопке_виден_в_очереди(tmp_path):
    """Разбор в потоке запроса всё равно обязан быть виден.

    Иначе «сейчас ничего не идёт» соседствует с занятой видеокартой.
    """
    db = база(tmp_path)
    настройки = Настройки()
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))

    поток.analyze_job("job0", force=True)

    строки = db.llmq_list()["items"]
    assert len(строки) == 1
    assert строки[0]["job_id"] == "job0"
    assert строки[0]["state"] == db.LLMQ_ГОТОВО
    assert строки[0]["kind"] == "вручную"
    db.close()


def test_готовый_разбор_не_идёт_к_модели_второй_раз(tmp_path):
    """Повторная постановка уже разобранной записи закрывается сразу.

    Без этого кнопка «разобрать всё неразобранное», нажатая дважды, гнала
    бы к модели весь архив заново.
    """
    db = база(tmp_path)
    настройки = Настройки()
    поток = LLMWorker(db, настройки, LLMClient(настройки, db))
    поток.analyze_job("job0", force=True)
    вызовов = поток.client.calls

    поток.analyze_job("job0")           # без force: ответ уже есть

    assert поток.client.calls == вызовов, "к модели сходили за готовым ответом"
    assert db.llmq_counts()[db.LLMQ_ГОТОВО] == 1
    db.close()


def test_ряд_для_графика_считается_в_базе(tmp_path):
    """График раздела строится по корзинам, а не по тысяче строк в браузере."""
    db = база(tmp_path, записей=4)
    сейчас = time.time()
    for i in range(4):
        db.llmq_put(f"job{i}")
        db.llmq_take()
        db.llmq_finish(f"job{i}", latency_ms=1000 * (i + 1),
                       error="сбой" if i == 3 else "")
        db.execute("UPDATE llm_queue SET finished_at=? WHERE job_id=?",
                   (сейчас - 3600 + i * 60, f"job{i}"))

    корзин, ряд = db.llmq_series(сейчас - 7200, сейчас, 24)

    assert корзин == 24
    assert sum(int(с["n"]) for с in ряд) == 4
    assert sum(int(с["failed"]) for с in ряд) == 1
    db.close()


# ---------------------------------------------------------------------------
# Маршруты
# ---------------------------------------------------------------------------

def test_маршрут_очереди_отдаёт_всё_одним_ответом(client):
    """Раздел опрашивается раз в пару секунд: пять запросов вместо одного —
    это пятикратная нагрузка ради одной картинки."""
    ответ = client.get("/api/llm/queue")
    assert ответ.status_code == 200, ответ.text
    тело = ответ.json()
    for ключ in ("worker", "counts", "stats", "series", "queue", "client", "settings"):
        assert ключ in тело, ключ
    assert set(тело["counts"]) >= {"ждёт", "идёт", "готово", "ошибка", "отменён"}


def test_пауза_через_маршрут_меняет_настройку(client):
    ответ = client.post("/api/llm/queue/pause", json={"paused": True})
    assert ответ.status_code == 200, ответ.text
    assert ответ.json()["paused"] is True
    assert client.get("/api/llm/queue").json()["settings"]["paused"] is True
    client.post("/api/llm/queue/pause", json={"paused": False})


def test_при_выключенной_модели_очередь_говорит_почему(client):
    """Отказ обязан называть причину и место, где она чинится.

    Пустая очередь при выключенном слое выглядит как исправная работа, и
    человек, нажавший «разобрать всё», не понимает, почему ничего не
    происходит.
    """
    ответ = client.post("/api/llm/queue/add", json={"scope": "pending"})
    assert ответ.status_code == 400
    assert "выключена" in ответ.text
    assert "Языковая модель" in ответ.text


def test_отборы_проверяются_при_включённом_слое(client):
    """Границы периода и имя отбора проверяются до похода в базу."""
    client.put("/api/settings", json={"llm_backend": "stub", "llm_model": "test"})

    период = client.post("/api/llm/queue/add", json={"scope": "period"})
    assert период.status_code == 400
    assert "промежутк" in период.text

    чепуха = client.post("/api/llm/queue/add", json={"scope": "чепуха"})
    assert чепуха.status_code == 400
    assert "pending" in чепуха.text

    client.put("/api/settings", json={"llm_backend": "off"})
