"""Видеокарта, которой у процесса нет.

Это отдельный класс отказа, и путать его с остальными дорого. Снаружи он
выглядит как поломка движка: «не удалось загрузить модель», ниже — текст
библиотеки про `cudaGetDeviceCount` и «invalid device ordinal». Человек по
такой подсказке идёт переустанавливать движок и скачивать веса, то есть
чинить ровно то, что исправно, а причина всё это время лежит в окружении
службы или в драйвере.

Проверки написаны против трёх обычных причин — чужой номер карты в
CUDA_VISIBLE_DEVICES, отвал карты на ходу (после него ошибка CUDA липкая и
уходит только с перезапуском) и драйвер, обновлённый без перезагрузки, — и
против двух способов сделать хуже: отказать движку, которому torch не нужен
вовсе, и промолчать в автодиагностике, когда nvidia-smi карту показывает, а
процесс её не видит.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest
from asrhub.catalog import ModelSpec
from asrhub.catalog.schema import Quality
from asrhub.engines.base import Engine, TranscriptionResult
from asrhub.engines.gigaam_engine import _load_failure
from asrhub.errors import HardwareError, OutOfMemoryError, classify_exception
from asrhub.hardware import проверить_ускоритель

# Текст, с которым это пришло с боевого сервера. Сокращать его нельзя:
# половина проверок тут про то, что разбор цепляется именно за него.
БОЕВОЙ = (
    "Unexpected error from cudaGetDeviceCount(). Did you run some cuda "
    "functions before calling NumCudaDevices() that might have already set "
    "an error? Error 101: invalid device ordinal"
)


# ---------------------------------------------------------------------------
# Подложный torch
# ---------------------------------------------------------------------------


def подложить_torch(monkeypatch: pytest.MonkeyPatch, *, карт: int = 1,
                    имя: Any = "NVIDIA GeForce RTX 5090",
                    счётчик: Any = None, mps: bool = False) -> None:
    """Кладёт в sys.modules подложный torch с заданным поведением.

    Настоящего torch в окружении проверок нет, и это не помеха: проверяется
    не он, а наше поведение при каждом его ответе — включая те ответы,
    которых на исправной машине не добиться вовсе.
    """
    torch = types.ModuleType("torch")

    def device_count() -> int:
        if счётчик is not None:
            raise счётчик
        return карт

    def get_device_name(индекс: int = 0) -> str:
        if isinstance(имя, BaseException):
            raise имя
        return str(имя)

    torch.cuda = types.SimpleNamespace(              # type: ignore[attr-defined]
        device_count=device_count, get_device_name=get_device_name)
    torch.backends = types.SimpleNamespace(          # type: ignore[attr-defined]
        mps=types.SimpleNamespace(is_available=lambda: mps))
    monkeypatch.setitem(sys.modules, "torch", torch)


# ---------------------------------------------------------------------------
# Разбор исключений
# ---------------------------------------------------------------------------


def test_боевая_ошибка_разбирается_как_отказ_оборудования():
    """Тот самый текст с сервера обязан стать ошибкой оборудования.

    Раньше он не попадал ни под одно правило и доезжал до общего разбора,
    который советовал проверить установку движка и наличие весов.
    """
    ошибка = classify_exception(RuntimeError(БОЕВОЙ), engine="gigaam",
                                model="gigaam-v3-e2e-rnnt")
    assert isinstance(ошибка, HardwareError)
    assert ошибка.code == "hardware_error"
    assert "недоступна" in ошибка.message.lower()


def test_подсказка_не_отправляет_чинить_движок_и_веса():
    """Подсказка обязана сказать, чего проверять не нужно.

    Это половина её пользы: без этой строки человек всё равно пойдёт
    переустанавливать движок — просто потому, что так написано в прошлый раз.
    """
    подсказка = classify_exception(RuntimeError(БОЕВОЙ)).hint
    assert "ни при чём" in подсказка
    assert "nvidia-smi" in подсказка
    assert "CUDA_VISIBLE_DEVICES" in подсказка
    assert "device: cpu" in подсказка or "device=cpu" in подсказка


def test_липкость_ошибки_названа_в_подсказке():
    """После отвала карты процесс не поправится сам.

    Без этой строки человек чинит драйвер, видит исправный nvidia-smi и
    считает, что всё в порядке, — а служба продолжает падать на каждом
    задании, потому что в её процессе ошибка CUDA осталась.
    """
    подсказка = classify_exception(RuntimeError(БОЕВОЙ)).hint
    assert "перезапуск" in подсказка.lower()


@pytest.mark.parametrize("текст", [
    "CUDA error: no CUDA-capable device is detected",
    "RuntimeError: No CUDA GPUs are available",
    "Failed to initialize NVML: Driver/library version mismatch",
    "CUDA driver version is insufficient for CUDA runtime version",
    "found no NVIDIA driver: system has unsupported display driver",
    "cudaGetDeviceCount() returned 101",
])
def test_прочие_виды_отвала_карты(текст: str):
    """Каждая формулировка, которой CUDA сообщает «карты нет»."""
    assert isinstance(classify_exception(RuntimeError(текст)), HardwareError)


def test_нехватка_памяти_остаётся_нехваткой_памяти():
    """Правило про карту не должно перехватывать OOM.

    Оба текста поминают CUDA, но лечатся противоположно: при OOM карта
    исправна и нужна модель поменьше, а не разбор с драйвером.
    """
    ошибка = classify_exception(RuntimeError("CUDA out of memory. Tried to allocate 2 GiB"))
    assert isinstance(ошибка, OutOfMemoryError)


def test_свои_ошибки_не_переразбираются():
    """Готовая ошибка ASR Hub проходит насквозь."""
    свой = HardwareError("уже разобрано")
    assert classify_exception(свой) is свой


# ---------------------------------------------------------------------------
# Сообщение GigaAM
# ---------------------------------------------------------------------------


def test_gigaam_называет_причину_а_не_движок():
    """Сообщение GigaAM уходит в телефонию одной строкой.

    У принимающей стороны нет ни журнала, ни подсказки отдельным полем:
    видно ровно это сообщение. Значит, причина должна быть в нём самом.
    """
    отказ = _load_failure(
        "gigaam-v3-e2e-rnnt",
        [f"v3_e2e_rnnt: RuntimeError: {БОЕВОЙ}",
         "transformers: InstantiationException: Tensor on device cpu is not "
         "on the expected device meta!"],
        "cuda", "/var/lib/asrhub/models")
    assert "видеокарта недоступна" in отказ.message.lower()
    assert "веса" not in отказ.message.lower()


def test_gigaam_первая_попытка_важнее_второй():
    """Вторая попытка падает по своему поводу и не должна перебивать первую.

    Отказ transformers про «тензор на cpu, а ждали meta» — следствие первой
    ошибки, и разбор по нему увёл бы в сторону от настоящей причины.
    """
    отказ = _load_failure(
        "gigaam-v3-e2e-rnnt",
        [f"v3_e2e_rnnt: RuntimeError: {БОЕВОЙ}",
         "transformers: no file named pytorch_model.bin"],
        "cuda", "")
    assert "видеокарта недоступна" in отказ.message.lower()


def test_gigaam_без_cuda_разбирается_по_прежнему():
    """Прочие причины не должны съехать на новое правило."""
    отказ = _load_failure("gigaam-v3-ctc",
                          ["v3_ctc: OSError: [Errno 28] No space left on device"],
                          "cpu", "")
    assert "места" in отказ.message.lower()


# ---------------------------------------------------------------------------
# Проверка устройства
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("устройство", ["cpu", "auto", "", "  "])
def test_процессор_проверять_нечего(устройство: str):
    годно, причина = проверить_ускоритель(устройство)
    assert годно and причина == ""


def test_чужое_устройство_не_наше_дело(monkeypatch: pytest.MonkeyPatch):
    """xpu, npu и прочее проверять нечем — и отказывать не за что.

    Отсев обязан идти до расспросов torch, и подложный torch здесь именно
    поэтому отвечает «карт нет»: без раннего отсева чужое устройство
    получило бы отказ по ответу про видеокарты, к которым оно отношения не
    имеет.
    """
    подложить_torch(monkeypatch, карт=0)
    годно, причина = проверить_ускоритель("xpu")
    assert годно and причина == ""


def test_без_torch_не_отказываем(monkeypatch: pytest.MonkeyPatch):
    """Движку вроде whisper.cpp или vosk torch не нужен вовсе.

    Отказ здесь уронил бы их на ровном месте: отсутствие torch — не
    доказательство того, что карты нет.
    """
    monkeypatch.delitem(sys.modules, "torch", raising=False)
    monkeypatch.setattr("builtins.__import__", _без("torch"))
    годно, _ = проверить_ускоритель("cuda")
    assert годно


def _без(имя: str):
    """Импорт, который для одного модуля отвечает отказом."""
    настоящий = __builtins__["__import__"] if isinstance(__builtins__, dict) \
        else __builtins__.__import__

    def подмена(name, *прочее):
        if name == имя:
            raise ImportError(f"No module named '{имя}'")
        return настоящий(name, *прочее)
    return подмена


def test_карт_нет_совсем(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=0)
    годно, причина = проверить_ускоритель("cuda")
    assert not годно
    assert "ни одной" in причина


def test_перечисление_устройств_отвечает_отказом(monkeypatch: pytest.MonkeyPatch):
    """Ровно то, что случилось на сервере."""
    подложить_torch(monkeypatch, счётчик=RuntimeError(БОЕВОЙ))
    годно, причина = проверить_ускоритель("cuda")
    assert not годно
    assert "invalid device ordinal" in причина


def test_запрошена_карта_которой_нет(monkeypatch: pytest.MonkeyPatch):
    """«cuda:3» при единственной карте.

    Причина обязана назвать оба числа: без них человек читает «карта
    недоступна» и идёт проверять драйвер, хотя достаточно поправить номер.
    """
    подложить_torch(monkeypatch, карт=1)
    годно, причина = проверить_ускоритель("cuda:3")
    assert not годно
    assert "3" in причина and "1" in причина


def test_карта_по_номеру_в_пределах(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=4)
    assert проверить_ускоритель("cuda:3") == (True, "")


def test_карта_не_отвечает_на_имя(monkeypatch: pytest.MonkeyPatch):
    """Счётчик устройств отвечает и на сломанном драйвере.

    Настоящая инициализация начинается только с обращения к самой карте —
    поэтому проверка на нём и не заканчивается счётчиком.
    """
    подложить_torch(monkeypatch, имя=RuntimeError("CUDA error: unknown error"))
    годно, причина = проверить_ускоритель("cuda")
    assert not годно
    assert "не отвечает" in причина


def test_исправная_карта(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=1)
    assert проверить_ускоритель("cuda") == (True, "")


def test_mps(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, mps=False)
    годно, причина = проверить_ускоритель("mps")
    assert not годно and "MPS" in причина
    подложить_torch(monkeypatch, mps=True)
    assert проверить_ускоритель("mps") == (True, "")


# ---------------------------------------------------------------------------
# Движок не начинает загрузку на недоступной карте
# ---------------------------------------------------------------------------


class _Считающий(Engine):
    """Движок, который считает, сколько раз его просили загрузить модель."""

    id = "счётчик"

    def __init__(self, spec: ModelSpec, settings: dict[str, Any]):
        super().__init__(spec, settings)
        self.загрузок = 0

    def _load(self, settings: dict[str, Any]) -> Any:
        self.загрузок += 1
        return object()

    def _transcribe(self, audio_path: Path, settings: dict[str, Any],
                    progress: Any = None) -> TranscriptionResult:
        return TranscriptionResult()


def _движок(**настройки: Any) -> _Считающий:
    spec = ModelSpec(id="проверочная", name="Проверочная", family="проверка",
                     engine="счётчик", source="—", license="—",
                     commercial_use=True, languages=["ru"],
                     ru_quality=Quality.GOOD)
    return _Считающий(spec, настройки)


def test_загрузка_не_начинается_на_недоступной_карте(monkeypatch: pytest.MonkeyPatch):
    """Отказ обязан прийти до чтения весов.

    Иначе человек ждёт минуту, пока библиотека прочитает гигабайты с диска,
    и только потом узнаёт, что карты не было с самого начала.
    """
    подложить_torch(monkeypatch, счётчик=RuntimeError(БОЕВОЙ))
    движок = _движок(device="cuda")
    with pytest.raises(HardwareError) as отказ:
        движок.ensure_loaded({"device": "cuda"})
    assert движок.загрузок == 0
    assert "cuda" in отказ.value.message.lower()


def test_на_исправной_карте_загрузка_идёт(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=1)
    движок = _движок(device="cuda")
    движок.ensure_loaded({"device": "cuda"})
    assert движок.загрузок == 1


def test_на_процессоре_проверка_не_мешает():
    движок = _движок(device="cpu")
    движок.ensure_loaded({"device": "cpu"})
    assert движок.загрузок == 1


def test_отказ_подсказывает_что_делать(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=0)
    движок = _движок(device="cuda")
    with pytest.raises(HardwareError) as отказ:
        движок.ensure_loaded({"device": "cuda"})
    assert "nvidia-smi" in отказ.value.hint


# ---------------------------------------------------------------------------
# Автодиагностика
# ---------------------------------------------------------------------------


class _Настройки(dict):
    """Настройки ровно в том объёме, в каком их читает проверка устройства."""


class _Состояние:
    def __init__(self, **настройки: Any):
        self.settings = _Настройки(настройки)


def _устройство(состояние: Any, ускоритель: str = "cuda") -> dict[str, Any]:
    from asrhub import selfcheck

    железо = types.SimpleNamespace(accelerator=ускоритель)
    проверки: list[dict[str, Any]] = []
    selfcheck._устройство_проверка(состояние, железо, проверки)
    assert len(проверки) == 1
    return проверки[0]


def test_диагностика_видит_недоступную_карту(monkeypatch: pytest.MonkeyPatch):
    """Главный смысл проверки: nvidia-smi показывает карту, процесс — нет.

    Раздел «Видеокарты» в этот момент зелёный: он читает nvidia-smi, а не
    процесс службы. Без отдельной проверки дашборд показывал бы исправную
    систему, у которой падает каждое задание.
    """
    подложить_torch(monkeypatch, счётчик=RuntimeError(БОЕВОЙ))
    пункт = _устройство(_Состояние(device="cuda"))
    assert пункт["state"] == "fail"
    assert "недоступно" in пункт["value"]
    assert "nvidia-smi" in пункт["hint"]


def test_диагностика_молчит_на_исправной_карте(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=1)
    пункт = _устройство(_Состояние(device="cuda"))
    assert пункт["state"] == "ok"
    assert пункт["hint"] == ""


def test_диагностика_называет_номер_карты(monkeypatch: pytest.MonkeyPatch):
    подложить_torch(monkeypatch, карт=1)
    пункт = _устройство(_Состояние(device="cuda:2"))
    assert пункт["state"] == "fail"
    assert "2" in пункт["value"]


def test_при_auto_отказа_нет_но_уход_на_процессор_назван():
    """«auto» сам уходит на процессор — жаловаться не на что.

    Но молчаливый уход выглядит как «сервер без причины стал медленным»,
    поэтому он назван прямо.
    """
    пункт = _устройство(_Состояние(device="auto"), ускоритель="cpu")
    assert пункт["state"] == "ok"
    assert "процессор" in пункт["hint"]
    assert "auto" in пункт["value"]


def test_при_auto_на_карте_подсказки_нет():
    пункт = _устройство(_Состояние(device="auto"), ускоритель="cuda")
    assert пункт["state"] == "ok"
    assert пункт["hint"] == ""


def test_проверка_устройства_попадает_в_свод(data_dir: Path,
                                            monkeypatch: pytest.MonkeyPatch):
    """Проверка обязана быть в разделе «Оборудование» настоящего свода.

    Написанная, но не подключённая проверка — это ровно то молчание, ради
    устранения которого она и написана.
    """
    from asrhub import selfcheck
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)
    разделы = {к["id"]: к for к in свод["components"]}
    пункты = {п["id"] for п in разделы["hardware"]["checks"]}
    assert "device" in пункты


# ---------------------------------------------------------------------------
# Драйвер: почему карты нет
# ---------------------------------------------------------------------------
#
# «Карта недоступна» — это факт. Что с ним делать, зависит от того, где
# разошлись версии: перезагрузиться, пересобрать модуль под новое ядро или
# чинить пакеты. Три разных действия, и ошибка стоит чужого простоя — поэтому
# приговор считается, а не угадывается.


def драйвер_на_диске(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *,
                     загружен: str = "595.91.07", библиотеки: str = "595.99",
                     ядра: dict[str, str] | None = None,
                     по_имени: str = "", работает: str = "7.0.0-31-generic",
                     modinfo: bool = True, dkms: str = "",
                     исходники: str = "",
                     ядра_где: dict[str, str] | None = None) -> None:
    """Подкладывает состояние драйвера: /proc, каталоги модулей и утилиты."""
    from asrhub import hardware

    proc = tmp_path / "proc-nvidia"
    proc.write_text(
        f"NVRM version: NVIDIA UNIX x86_64 Kernel Module  {загружен}  x\n",
        encoding="utf-8")
    корень = tmp_path / "modules"
    версии = dict(ядра or {})
    for ядро in версии:
        # Куда класть модуль — не мелочь: DKMS кладёт в updates/dkms, а готовые
        # модули Ubuntu приезжают в kernel/nvidia-<ветка>/. Обходить надо оба,
        # иначе пакетный модуль под работающим ядром остаётся невидимым.
        путь = (ядра_где or {}).get(ядро, "updates/dkms")
        каталог = корень / ядро / путь
        каталог.mkdir(parents=True, exist_ok=True)
        (каталог / "nvidia.ko").touch()
    корень.mkdir(parents=True, exist_ok=True)

    src = tmp_path / "usr-src"
    src.mkdir(parents=True, exist_ok=True)
    if исходники:
        (src / f"nvidia-{исходники}").mkdir(exist_ok=True)

    monkeypatch.setattr(hardware, "NVIDIA_PROC", str(proc))
    monkeypatch.setattr(hardware, "MODULES_ROOT", str(корень))
    monkeypatch.setattr(hardware, "DKMS_SRC", str(src))
    monkeypatch.setattr(hardware.platform, "release", lambda: работает)
    def найдена(имя):
        if имя == "modinfo" and not modinfo:
            return None
        if имя == "dkms" and dkms == "нет":
            return None
        return f"/usr/bin/{имя}"

    monkeypatch.setattr(hardware.shutil, "which", найдена)

    def запуск(cmd, timeout=6.0, *, со_стдерр=False):
        if cmd[0] == "nvidia-smi":
            if "--query-gpu=driver_version" in cmd:
                return ""
            # На живой машине это уходит в поток ошибок. Мок ведёт себя так
            # же: иначе проверка прошла бы и на коде, который стдерр не
            # читает, — а именно там версия библиотек и печатается.
            if not со_стдерр:
                return ""
            return ("Failed to initialize NVML: Driver/library version mismatch\n"
                    f"NVML library version: {библиотеки}")
        if cmd[0] == "dkms":
            # Пустой ответ — это «DKMS не знает ни об одном модуле», а вовсе
            # не «модуль не собрался». Различение и проверяется.
            return "nvidia/595.99.02, 7.0.0-31-generic: installed" \
                if dkms == "знает" else ""
        if cmd[0] == "modinfo":
            цель = cmd[-1]
            if цель == "nvidia":
                return по_имени
            for ядро, версия in версии.items():
                if f"/{ядро}/" in цель:
                    return версия
            return ""
        return ""

    monkeypatch.setattr(hardware, "_run", запуск)


def test_драйвер_обновлён_без_перезагрузки(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Новый модуль уже лежит под работающим ядром — хватит перезагрузки."""
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.99.02")
    приговор = проверить_драйвер()
    assert приговор["state"] == "reboot"
    assert приговор["ondisk"] == "595.99.02"


def test_ядро_обновилось_а_драйвер_нет(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Ровно то, что случилось на боевом сервере.

    Модуль 595.99.02 собран под 7.0.0-30, а работает 7.0.0-31. Под работающим
    ядром модуля нет — и `modinfo nvidia` об этом честно молчит.
    """
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     ядра={"7.0.0-30-generic": "595.99.02",
                           "7.0.0-31-generic": "595.91.07"})
    приговор = проверить_драйвер()
    assert приговор["state"] == "other-kernel"
    assert приговор["kernel"] == "7.0.0-30-generic"
    assert приговор["running"] == "7.0.0-31-generic"


def test_модуля_нет_ни_под_одним_ядром(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07")
    assert проверить_драйвер()["state"] == "rebuild"


def test_версии_сходятся_дело_не_в_драйвере(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, загружен="595.91.07",
                     библиотеки="595.91")
    assert проверить_драйвер()["state"] == "ok"


def test_без_драйвера_проверять_нечего(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Машина без NVIDIA: сравнивать нечего, и строки в панели быть не должно."""
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, загружен="", библиотеки="")
    assert проверить_драйвер()["state"] == "unknown"
    проверки: list[dict[str, Any]] = []
    from asrhub import selfcheck

    selfcheck._драйвер_проверка(проверки)
    assert проверки == []


def test_без_modinfo_приговор_мягче(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, modinfo=False)
    assert проверить_драйвер()["state"] == "unknown-disk"


def test_панель_называет_ядро_и_команду(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Панель обязана отвечать так же подробно, как скрипт.

    Человек читает их по очереди, и расходиться им в такой мелочи нельзя.
    """
    from asrhub import selfcheck

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     dkms="знает", ядра={"7.0.0-30-generic": "595.99.02"})
    проверки: list[dict[str, Any]] = []
    selfcheck._драйвер_проверка(проверки)
    assert len(проверки) == 1
    пункт = проверки[0]
    assert пункт["id"] == "driver"
    assert пункт["state"] == "fail"
    assert "7.0.0-30-generic" in пункт["value"]
    assert "dkms autoinstall" in пункт["hint"]
    assert "linux-headers-generic" in пункт["hint"]
    # Одних команд мало: человек должен понимать, что чинит. Без объяснения
    # «ядро обновилось, драйвер не пересобран» команды выглядят заклинанием.
    assert "не пересобран" in пункт["hint"]


def test_панель_молчит_когда_версии_сходятся(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from asrhub import selfcheck

    драйвер_на_диске(tmp_path, monkeypatch, загружен="595.91.07",
                     библиотеки="595.91")
    проверки: list[dict[str, Any]] = []
    selfcheck._драйвер_проверка(проверки)
    assert проверки[0]["state"] == "ok"
    assert проверки[0]["hint"] == ""


def test_модуль_работающего_ядра_не_выдаётся_за_чужой(tmp_path,
                                                     monkeypatch: pytest.MonkeyPatch):
    """Под работающим ядром два модуля, а modprobe берёт старый.

    Совет «загрузитесь в ядро 7.0.0-31» человеку, который в нём и сидит, —
    издевательство. Приговор должен быть про пересборку, а не про ядро.
    """
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     работает="7.0.0-31-generic",
                     ядра={"7.0.0-31-generic": "595.99.02"})
    приговор = проверить_драйвер()
    assert приговор["state"] != "other-kernel", приговор
    assert приговор["kernel"] == ""


def test_версия_с_диска_берётся_обходом_когда_имени_мало(
        tmp_path, monkeypatch: pytest.MonkeyPatch):
    """`modinfo nvidia` находит модуль по имени не везде — в RHEL он в extra/.

    Тогда версию под работающим ядром берём обходом каталогов. Без этого
    исправно собранный модуль выглядел бы отсутствующим.
    """
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="",
                     работает="7.0.0-31-generic",
                     ядра={"7.0.0-31-generic": "595.99.02"})
    приговор = проверить_драйвер()
    assert приговор["state"] == "reboot"
    assert приговор["ondisk"] == "595.99.02"


@pytest.mark.parametrize("a,b,сходятся", [
    ("595.91.07", "595.91", True),
    ("595.91.07", "595.99", False),
    ("", "595.99", True),          # сравнивать не с чем — не выдумываем
    ("595.91", "", True),
])
def test_сравнение_версий_драйвера(a: str, b: str, сходятся: bool):
    """Пустое значение — это «нечего сравнивать», а не «расходятся».

    Выдуманное расхождение отправило бы человека чинить исправный драйвер.
    """
    from asrhub.hardware import _версии_сходятся

    assert _версии_сходятся(a, b) is сходятся


def test_проверка_драйвера_попадает_в_свод(data_dir: Path,
                                           monkeypatch: pytest.MonkeyPatch):
    """Написанная, но не подключённая проверка — то же молчание панели."""
    from asrhub import hardware, selfcheck
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setattr(hardware, "проверить_драйвер", lambda **_: {
        "state": "other-kernel", "loaded": "595.91.07", "userspace": "595.99",
        "ondisk": "595.91.07", "kernel": "7.0.0-30-generic",
        "running": "7.0.0-31-generic"})
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    app = create_app(load(), start_queue=False)
    with TestClient(app):
        свод = selfcheck.состояние(app.state.hub)
    разделы = {к["id"]: к for к in свод["components"]}
    пункты = {п["id"]: п for п in разделы["hardware"]["checks"]}
    assert "driver" in пункты
    assert пункты["driver"]["state"] == "fail"
    assert "7.0.0-30-generic" in пункты["driver"]["value"]


# ---------------------------------------------------------------------------
# Как поставлен драйвер — от этого зависит, что советовать
# ---------------------------------------------------------------------------


def test_run_установщик_поверх_пакетов(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Ровно то, что оказалось на боевом сервере.

    Исходники драйвера в /usr/src есть, а `dkms status` пуст: .run-установщик
    NVIDIA поверх пакетных модулей, чья регистрация в DKMS теряется при
    обновлении ядра. Совет «dkms autoinstall» тут отвечает тишиной, а совет
    «поставьте пакет модулей» — мимо: пакет уже стоит.
    """
    from asrhub import selfcheck
    from asrhub.hardware import dkms_состояние

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     dkms="молчит", исходники="595.99.02",
                     ядра={"7.0.0-30-generic": "595.99.02"})
    assert dkms_состояние() == "stale"
    проверки: list[dict[str, Any]] = []
    selfcheck._драйвер_проверка(проверки)
    подсказка = проверки[0]["hint"]
    assert "dkms add -m nvidia -v 595.99.02" in подсказка
    assert ".run" in подсказка
    assert "autoinstall" not in подсказка, "совет из чужого варианта"


def test_готовые_модули_без_dkms(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """DKMS пуст и исходников нет — драйвер приехал готовыми модулями."""
    from asrhub import selfcheck
    from asrhub.hardware import dkms_состояние

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     dkms="молчит", ядра={"7.0.0-30-generic": "595.99.02"})
    assert dkms_состояние() == "unknown"
    проверки: list[dict[str, Any]] = []
    selfcheck._драйвер_проверка(проверки)
    подсказка = проверки[0]["hint"]
    assert "linux-modules-nvidia-595" in подсказка
    assert "dkms add" not in подсказка


def test_без_dkms_вовсе(tmp_path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.hardware import dkms_состояние

    драйвер_на_диске(tmp_path, monkeypatch, dkms="нет")
    assert dkms_состояние() == "absent"


def test_ветка_драйвера_из_версии_библиотек(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """«595» из «595.99» — имя ветки для команды установки.

    Брать его из имени пакета нельзя: пакеты называются по-разному
    (nvidia-driver-595, -595-server, -595-open), а первая часть версии — это
    и есть ветка.
    """
    from asrhub.hardware import проверить_драйвер

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07",
                     библиотеки="595.99", dkms="молчит")
    assert проверить_драйвер()["branch"] == "595"


def test_пакетный_модуль_ubuntu_виден(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Готовые модули Ubuntu лежат в kernel/nvidia-595srv, а не в updates/dkms.

    Без этого каталога в обходе пакетный модуль под работающим ядром был
    невидим, и разбор считал, что под ним модуля нет вовсе.
    """
    from asrhub.hardware import модули_по_ядрам

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="",
                     работает="7.0.0-31-generic",
                     ядра={"7.0.0-31-generic": "595.91.07"},
                     ядра_где={"7.0.0-31-generic": "kernel/nvidia-595srv"})
    from asrhub import hardware

    найдено = модули_по_ядрам(hardware.MODULES_ROOT)
    assert найдено == {"7.0.0-31-generic": "595.91.07"}


def test_запасной_путь_назван_с_именем_ядра(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Совет «загрузиться обратно» обязан называть ядро и свою цену.

    Без имени ядра он бесполезен, без цены — вреден: человек уедет на ядро
    без последних исправлений и вернётся сюда же после следующего обновления.
    """
    from asrhub import selfcheck

    драйвер_на_диске(tmp_path, monkeypatch, по_имени="595.91.07", dkms="знает",
                     ядра={"7.0.0-30-generic": "595.99.02"})
    проверки: list[dict[str, Any]] = []
    selfcheck._драйвер_проверка(проверки)
    подсказка = проверки[0]["hint"]
    assert "Запасной путь" in подсказка
    assert "7.0.0-30-generic" in подсказка
    assert "без последних исправлений" in подсказка
