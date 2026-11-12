"""Заход 70: проверки поведением вместо текста исходника.

Рабочий цикл очереди переехал в `job_queue_worker.py`, и проверки, искавшие
подстроки в `job_queue.py`, перестали бы сторожить что-либо: файл другой,
код тот же. Здесь то же самое — на настоящих воркерах.
"""
from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_review_54_queue as _очередь  # noqa: E402
from test_review_54_queue import _загрузка, _Записи, _поставить, ждать, подмена  # noqa: E402

#: Сервер без запущенной очереди — тот же, что у проверок захода 54.
сервер = _очередь.сервер


def test_подсказка_проваленного_задания_ложится_в_журнал_строками(сервер, monkeypatch):
    """В журнал попадал факт и не попадала причина с лечением."""
    from asrhub.errors import ModelLoadError

    state = сервер.app.state.hub
    monkeypatch.setattr("asrhub.job_queue_worker.process_job", подмена(fail=ModelLoadError(
        "Не удалось загрузить модель.",
        hint="Загрузите веса: models.sh download …\nКаталог: /var/lib")))
    журнал = _Записи()
    logging.getLogger("asrhub.queue").addHandler(журнал)
    try:
        # Отказ загрузки модели повторяется — здесь без повторов.
        задание = _поставить(state, _загрузка(state), overrides={"max_retries": 0})
        state.queue.start()
        assert ждать(lambda: state.db.get_job(задание["id"])["status"] == "failed")
        assert ждать(lambda: any("Каталог: /var/lib" in з.getMessage()
                                 for з in журнал.записи), срок=5)
    finally:
        logging.getLogger("asrhub.queue").removeHandler(журнал)
    строки = [з.getMessage() for з in журнал.записи if задание["id"] in з.getMessage()]
    assert any("провалено" in с for с in строки), строки
    assert any("models.sh download" in с for с in строки), строки


def test_слот_освобождается_один_раз_и_на_настоящих_воркерах(сервер, monkeypatch):
    """Освобождение слота было написано дважды, и копии разошлись.

    Два воркера, два задания одной модели, идущие разом: после них ни
    отметок одновременности, ни идущих, ни занятых слотов модели — и пик
    памяти замерялся у обоих, а обнулялся не больше чем у одного.
    """
    state = сервер.app.state.hub
    вызовы: list[dict[str, Any]] = []
    оба = threading.Barrier(2, timeout=20)
    исходная = подмена()

    def process_job(*a: Any, **kw: Any) -> Any:
        вызовы.append({"measure": kw.get("measure_memory"), "reset": kw.get("reset_memory")})
        оба.wait()
        return исходная(*a, **kw)

    monkeypatch.setattr("asrhub.job_queue_worker.process_job", process_job)
    # Разной длины: одинаковые записи — «близнецы», вторая ждёт первую.
    задания = [_поставить(state, _загрузка(state, f"{н}.wav", секунд=1.0 + н / 10))
               for н in range(2)]
    # Предел одновременных заданий — два: сервер проверок держит один.
    state.settings.set("max_concurrent_jobs", 2)
    state.queue.start(workers=2)
    assert ждать(lambda: all(state.db.get_job(з["id"])["status"] == "completed"
                             for з in задания))
    очередь = state.queue
    assert ждать(lambda: not очередь._running and not очередь._идут, срок=5), \
        (очередь._running, очередь._идут)
    assert очередь._concurrency == {}, очередь._concurrency
    assert all(число == 0 for число in очередь._model_counts.values()), очередь._model_counts
    assert [в["measure"] for в in вызовы] == [True, True], вызовы
    # Обнуляет только начавшее в одиночку; начавшие разом не обнуляют оба.
    assert sum(bool(в["reset"]) for в in вызовы) <= 1, вызовы
