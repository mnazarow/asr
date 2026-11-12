"""Распознавание в дочернем процессе воркера (`worker_isolation: process`).

Поток Python нельзя прервать снаружи. Сторож зависших снимает
задание, движок которого молчит дольше предела, и отдаёт слот новому
потоку, но сам застрявший вызов — драйвер видеокарты, взаимная блокировка
внутри библиотеки — живёт вместе с памятью модели до перезапуска службы.

В этом режиме каждый воркер держит свой постоянный дочерний процесс со
своим кешем моделей. Поток воркера отдаёт ему задание и ждёт ответа,
пересылая доклады о ходе и отмену. Зависший процесс сторож завершает
(`terminate`, затем `kill`): видеопамять и память процесса освобождаются
сразу, а следующий вызов поднимает процесс заново.

Цена — своя копия модели в каждом процессе: на одной видеокарте при двух
воркерах это две копии. Режим поэтому естественно сочетается с пулом карт
(`gpu_devices`): воркер — своя карта, свой процесс.
"""
from __future__ import annotations

import multiprocessing as mp
import threading
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import errors as E
from .logging_setup import get_logger

log = get_logger("queue")

#: Сколько ждать мягкого завершения процесса перед `kill`.
ЖДАТЬ_ЗАВЕРШЕНИЯ_С = 5.0
#: Как часто поток воркера проверяет отмену, пока процесс считает.
ОПРОС_С = 0.25


def _ошибка_из(сведения: dict[str, Any]) -> BaseException:
    """Исключение дочернего процесса — обратно в класс ASR Hub.

    Классы ошибок ASR Hub принимают разные аргументы, и через pickle их не
    передать надёжно: едет словарь, а здесь находится класс с тем же кодом.
    """
    код = str(сведения.get("code") or "")
    if код:
        for имя in dir(E):
            класс = getattr(E, имя)
            if isinstance(класс, type) and issubclass(класс, E.ASRHubError) \
                    and getattr(класс, "code", None) == код:
                ошибка = класс.__new__(класс)
                ошибка.message = str(сведения.get("message") or код)
                if сведения.get("hint") is not None:
                    ошибка.hint = str(сведения["hint"])
                ошибка.details = dict(сведения.get("details") or {})
                ошибка.cause = None
                Exception.__init__(ошибка, ошибка.message)
                return ошибка
    # Не наша ошибка — с тем же текстом, чтобы classify_exception узнал
    # нехватку памяти, недоступный драйвер и прочее по сообщению.
    return RuntimeError(f"{сведения.get('type') or 'Error'}: {сведения.get('message') or ''}")


def _сведения_об(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, E.ASRHubError):
        данные = exc.to_dict()
        данные["details"] = getattr(exc, "details", {}) or {}
        return данные
    return {"type": type(exc).__name__, "message": str(exc),
            "trace": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]}


def _цикл(связь: Any, отмена: Any, размер_кеша: int, простой_с: int) -> None:
    """Тело дочернего процесса: задания по одному, пока связь жива."""
    from .engines import EngineRegistry  # noqa: PLC0415
    from .processor import process_job  # noqa: PLC0415

    реестр = EngineRegistry(max(1, int(размер_кеша)), int(простой_с))
    while True:
        try:
            запрос = связь.recv()
        except (EOFError, OSError):
            return
        if запрос is None:
            return
        отмена.clear()

        def доклад(значение: float, стадия: str) -> None:
            связь.send(("progress", float(значение), str(стадия)))

        try:
            итог = process_job(
                Path(запрос["source"]), запрос["settings"], реестр,
                workdir=Path(запрос["workdir"]), outdir=Path(запрос["outdir"]),
                basename=запрос["basename"], filename=запрос.get("filename", ""),
                progress=доклад, cancelled=отмена.is_set,
                deadline=запрос.get("deadline"), timeout_s=int(запрос.get("timeout_s") or 0),
                measure_memory=bool(запрос.get("measure_memory")),
                reset_memory=bool(запрос.get("reset_memory")))
            связь.send(("done", итог))
        except BaseException as exc:                         # noqa: BLE001
            try:
                связь.send(("error", _сведения_об(exc)))
            except (OSError, EOFError):
                return
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                return


class ДочернийВоркер:
    """Постоянный процесс распознавания одного воркера."""

    def __init__(self, index: int, *, размер_кеша: int = 1, простой_с: int = 900) -> None:
        self.index = index
        self.размер_кеша = размер_кеша
        self.простой_с = простой_с
        self._ctx = mp.get_context("spawn")
        self._процесс: Any = None
        self._связь: Any = None
        self._отмена: Any = None
        self._замок = threading.Lock()
        self._снят = threading.Event()

    # --- жизнь процесса ---------------------------------------------------

    @property
    def pid(self) -> int | None:
        return getattr(self._процесс, "pid", None)

    def жив(self) -> bool:
        return self._процесс is not None and self._процесс.is_alive()

    def _поднять(self) -> None:
        родная, дочерняя = self._ctx.Pipe()
        self._отмена = self._ctx.Event()
        self._процесс = self._ctx.Process(
            target=_цикл, args=(дочерняя, self._отмена, self.размер_кеша, self.простой_с),
            name=f"asrhub-worker-{self.index}", daemon=True)
        self._процесс.start()
        дочерняя.close()
        self._связь = родная
        log.info("Воркер %d: процесс распознавания поднят (pid %s)", self.index,
                 self._процесс.pid)

    def завершить(self, причина: str = "") -> bool:
        """Завершить процесс (сторож, остановка). True — процесс был жив."""
        процесс = self._процесс
        if процесс is None:
            return False
        был_жив = процесс.is_alive()
        self._снят.set()
        if был_жив:
            log.warning("Воркер %d: процесс распознавания %s завершается%s", self.index,
                        процесс.pid, f" — {причина}" if причина else "")
            процесс.terminate()
            процесс.join(ЖДАТЬ_ЗАВЕРШЕНИЯ_С)
            if процесс.is_alive():
                процесс.kill()
                процесс.join(ЖДАТЬ_ЗАВЕРШЕНИЯ_С)
        try:
            if self._связь is not None:
                self._связь.close()
        except OSError:
            pass
        self._процесс = self._связь = None
        return был_жив

    def остановить(self) -> None:
        """Мягкая остановка: попросить процесс выйти, иначе завершить."""
        if self._процесс is None:
            return
        try:
            self._связь.send(None)
            self._процесс.join(ЖДАТЬ_ЗАВЕРШЕНИЯ_С)
        except (OSError, EOFError, ValueError):
            pass
        self.завершить("остановка сервера")

    def _разобрать_смерть(self, timeout_s: int,
                          cancelled: Callable[[], bool] | None = None) -> None:
        """Процесс ушёл посреди задания: снят сторожем, остановкой или умер сам."""
        процесс = self._процесс
        if процесс is not None:
            процесс.join(ЖДАТЬ_ЗАВЕРШЕНИЯ_С)
        код = getattr(процесс, "exitcode", None)
        снят = self._снят.is_set()
        self.завершить()
        if cancelled is not None and cancelled():
            # Отмена или остановка сервера: задание вернётся в очередь без
            # траты попытки, как при отмене в потоке.
            raise E.JobCancelled("Задание прервано вместе с процессом распознавания.")
        if снят:
            raise E.EngineHung(int(timeout_s or 0))
        raise E.WorkerProcessDied(f"Процесс распознавания завершился (код {код}).")

    # --- задание ----------------------------------------------------------

    def выполнить(self, source: Path, settings: dict[str, Any], *, workdir: Path,
                  outdir: Path, basename: str, filename: str = "",
                  progress: Callable[[float, str], None] | None = None,
                  cancelled: Callable[[], bool] | None = None,
                  deadline: float | None = None, timeout_s: int = 0,
                  measure_memory: bool = False, reset_memory: bool = False) -> Any:
        """То же, что `process_job`, но в дочернем процессе."""
        with self._замок:
            if not self.жив():
                self.завершить()
                self._поднять()
            self._снят.clear()
            связь, отмена = self._связь, self._отмена
            связь.send({
                "source": str(source), "settings": dict(settings), "workdir": str(workdir),
                "outdir": str(outdir), "basename": basename, "filename": filename,
                "deadline": deadline, "timeout_s": timeout_s,
                "measure_memory": measure_memory, "reset_memory": reset_memory})
            while True:
                if cancelled is not None and cancelled() and not отмена.is_set():
                    отмена.set()
                try:
                    есть = связь.poll(ОПРОС_С)
                    сообщение = связь.recv() if есть else None
                except (OSError, EOFError, ValueError):
                    # Процесс завершён сторожем или умер сам: связь закрыта.
                    self._разобрать_смерть(timeout_s, cancelled)
                if сообщение is None:
                    if not self.жив():
                        self._разобрать_смерть(timeout_s, cancelled)
                    continue
                вид, *данные = сообщение
                if вид == "progress":
                    if progress is not None:
                        progress(данные[0], данные[1])
                elif вид == "done":
                    return данные[0]
                elif вид == "error":
                    raise _ошибка_из(данные[0])
