"""Заход 63: докачка записей агентом частями и проверка типов (с 3.1.38 — целиком).

Большая запись от агента раньше шла одним запросом: обрыв на гигабайтной
записи означал отправку с нуля, а прокси с `client_max_body_size` меньше
записи не пропускал её вовсе — звонок уходил без записи. Теперь запись
больше `chunk_mb` идёт частями (`POST /api/telephony/agent/chunk`), после
обрыва агент спрашивает, сколько принято, и продолжает с того места.

Здесь — настоящий агент (`Клиент`) против настоящего сервера на порту:
части, докачка, часть не с того места, сервер без частей, прокси с 413.
"""
from __future__ import annotations

import hashlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

import pytest
from asrhub.api import create_app
from asrhub.config import load
from test_agent import агент

КОРЕНЬ = Path(__file__).resolve().parent.parent


def _свободный_порт() -> int:
    with socket.socket() as с:
        с.bind(("127.0.0.1", 0))
        return int(с.getsockname()[1])


def _запись(путь: Path, секунд: int) -> Path:
    """Шум, а не тишина: одинаковые части не должны сходиться случайно."""
    import random

    случай = random.Random(63)
    with wave.open(str(путь), "wb") as файл:
        файл.setnchannels(1)
        файл.setsampwidth(2)
        файл.setframerate(16000)
        файл.writeframes(bytes(случай.getrandbits(8) for _ in range(32000 * секунд)))
    return путь


@pytest.fixture()
def живой(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    import uvicorn

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "true")
    настройки = load()
    настройки.api_keys.update({
        "ah_admin_63": {"name": "админ", "role": "admin", "enabled": True},
        "ah_other_63": {"name": "другой", "role": "user", "enabled": True},
    })
    приложение = create_app(настройки, start_queue=False)
    порт = _свободный_порт()
    служба = uvicorn.Server(uvicorn.Config(приложение, host="127.0.0.1", port=порт,
                                           log_level="warning"))
    поток = threading.Thread(target=служба.run, daemon=True)
    поток.start()
    for _ in range(200):
        if служба.started:
            break
        time.sleep(0.05)
    assert служба.started
    try:
        yield f"http://127.0.0.1:{порт}", приложение.state.hub
    finally:
        служба.should_exit = True
        поток.join(timeout=10)


class _Счётчик(агент.Клиент):
    """Клиент, который помнит свои запросы (и может притвориться прокси)."""

    def __init__(self, *а, прокси_до: int = 0, без_частей: bool = False, **к):
        super().__init__(*а, **к)
        self.запросы: list[tuple[str, str, int]] = []
        self.прокси_до = прокси_до
        self.без_частей = без_частей

    def _запрос(self, метод, путь, тело=None, тип=None, длина=None, **к):
        self.запросы.append((метод, путь.split("?")[0], int(длина or 0)))
        if self.без_частей and "/agent/chunk" in путь:
            raise агент.ОтказСервера(404, "", "Not Found")
        if self.прокси_до and int(длина or 0) > self.прокси_до:
            # Прокси перед сервером: 413 без кода ASR Hub.
            raise агент.ОтказСервера(413, "", "Request Entity Too Large")
        return super()._запрос(метод, путь, тело, тип, длина, **к)

    def части(self) -> list[int]:
        return [д for м, п, д in self.запросы if м == "POST" and п.endswith("/chunk")]


def _клиент(адрес: str, **к) -> _Счётчик:
    return _Счётчик(адрес, "ah_admin_63", повторов=0, ждать=lambda _с: None, **к)


def _звонок(клиент: агент.Клиент, uid: str) -> dict:
    ответ = клиент.hello({"host": "pbx63.local", "name": "Станция 63"})
    return {"agent_id": ответ["agent_id"], "uniqueid": uid, "src": "79160000063",
            "dst": "101", "direction": "входящий", "duration": 120, "billsec": 110,
            "answered": True, "started_at": 1789000000.0}


def _сумма(путь: Path) -> str:
    return hashlib.sha256(путь.read_bytes()).hexdigest()


def _принятая(state, job_id: str) -> Path:
    задание = state.db.get_job(job_id)
    return Path(задание["file_path"])


def test_большая_запись_идёт_частями_и_собирается_как_была(живой, tmp_path):
    адрес, state = живой
    запись = _запись(tmp_path / "длинная.wav", 40)            # ~1,3 МБ
    клиент = _клиент(адрес, часть_мб=0.25)
    ответ = клиент.отправить(_звонок(клиент, "1789000063.1"), str(запись))
    assert ответ.get("ok") and ответ.get("job_id"), ответ
    части = клиент.части()
    assert len(части) == -(-запись.stat().st_size // (256 * 1024))
    assert max(части) <= 256 * 1024
    # Звонок — без файла, со ссылкой на докачанное.
    assert [д for м, п, д in клиент.запросы if п.endswith("/call")][-1] < 4096
    assert _сумма(_принятая(state, ответ["job_id"])) == _сумма(запись)
    каталог = Path(state.settings.paths.uploads) / "parts"
    assert not list(каталог.glob("*")), "докачанное должно уйти в задание"


def test_после_обрыва_докачка_продолжается_с_места(живой, tmp_path):
    адрес, state = живой
    запись = _запись(tmp_path / "обрыв.wav", 40)
    клиент = _клиент(адрес, часть_мб=0.25)
    поля = _звонок(клиент, "1789000063.2")
    # Первая часть дошла, дальше связь оборвалась (агента перезапустили).
    размер = запись.stat().st_size
    сумма = _сумма(запись)
    ид = hashlib.sha256(f"{поля['agent_id']}|{поля['uniqueid']}|{размер}|{сумма}"
                        .encode()).hexdigest()[:40]
    первая = запись.read_bytes()[:256 * 1024]
    клиент._запрос("POST", "/api/telephony/agent/chunk?" + агент.разбор_url.urlencode(
        {"upload_id": ид, "agent_id": поля["agent_id"], "offset": 0, "total": размер,
         "sha256": сумма}), первая, "application/octet-stream", len(первая))
    клиент.запросы.clear()
    ответ = клиент.отправить(поля, str(запись))
    assert ответ.get("job_id"), ответ
    всего = -(-размер // (256 * 1024))
    assert len(клиент.части()) == всего - 1, "первая часть не должна уходить второй раз"
    assert _сумма(_принятая(state, ответ["job_id"])) == сумма


def test_часть_не_с_того_места_получает_409_с_принятым(живой, tmp_path):
    адрес, _state = живой
    клиент = _клиент(адрес)
    поля = _звонок(клиент, "1789000063.3")
    о_загрузке = {"upload_id": "проба-63-0001".encode().hex()[:24], "agent_id": поля["agent_id"],
                  "total": 1000, "sha256": "0" * 64}

    def адрес_части(поля: dict) -> str:
        return "/api/telephony/agent/chunk?" + агент.разбор_url.urlencode(поля)

    клиент._запрос("POST", адрес_части({**о_загрузке, "offset": 0}), b"x" * 100,
                   "application/octet-stream", 100)
    with pytest.raises(агент.ОтказСервера) as сбой:
        клиент._запрос("POST", адрес_части({**о_загрузке, "offset": 500}), b"x" * 100,
                       "application/octet-stream", 100)
    assert сбой.value.статус == 409 and сбой.value.код == "upload_offset_mismatch"
    принято = клиент._запрос("GET", адрес_части(
        {"upload_id": о_загрузке["upload_id"], "agent_id": поля["agent_id"]}))
    assert принято["received"] == 100
    # Звонок со ссылкой на недокачанное — тоже 409: агент дошлёт остаток.
    with pytest.raises(агент.ОтказСервера) as сбой:
        клиент._отправить_целиком(dict(поля, upload_id=о_загрузке["upload_id"],
                                       upload_name="a.wav"), None)
    assert сбой.value.статус == 409 and сбой.value.код == "upload_offset_mismatch"


def test_чужой_агент_не_видит_и_не_портит_чужую_загрузку(живой, tmp_path):
    адрес, _state = живой
    свой = _клиент(адрес)
    поля = _звонок(свой, "1789000063.4")
    чужой = агент.Клиент(адрес, "ah_other_63", повторов=0, ждать=lambda _с: None)
    with pytest.raises(агент.ОтказСервера) as сбой:
        чужой._запрос("GET", "/api/telephony/agent/chunk?" + агент.разбор_url.urlencode(
            {"upload_id": "abcdefgh1234", "agent_id": поля["agent_id"]}))
    assert сбой.value.статус in (403, 404)


def test_сервер_без_частей_получает_запись_целиком(живой, tmp_path):
    адрес, state = живой
    запись = _запись(tmp_path / "старый.wav", 20)
    клиент = _клиент(адрес, часть_мб=0.25, без_частей=True)
    ответ = клиент.отправить(_звонок(клиент, "1789000063.5"), str(запись))
    assert ответ.get("job_id"), ответ
    assert клиент._частями is False
    assert _сумма(_принятая(state, ответ["job_id"])) == _сумма(запись)


def test_прокси_с_413_записи_идут_частями_помельче(живой, tmp_path):
    адрес, state = живой
    запись = _запись(tmp_path / "прокси.wav", 40)            # ~1,3 МБ
    клиент = _клиент(адрес, прокси_до=400 * 1024)            # «client_max_body_size 400k»
    ответ = клиент.отправить(_звонок(клиент, "1789000063.6"), str(запись))
    assert ответ.get("job_id"), ответ
    # Сначала — целиком (прокси отверг), затем части: крупная отвергнута,
    # прошедшие помельче покрыли запись целиком.
    прошли = [д for д in клиент.части() if д <= 400 * 1024]
    assert sum(прошли) == запись.stat().st_size
    assert any(д > 400 * 1024 for м, п, д in клиент.запросы if п.endswith("/call"))
    assert _сумма(_принятая(state, ответ["job_id"])) == _сумма(запись)


def test_нуль_в_chunk_mb_выключает_части(живой, tmp_path):
    адрес, _state = живой
    запись = _запись(tmp_path / "целиком.wav", 20)
    клиент = _клиент(адрес, часть_мб=0)
    ответ = клиент.отправить(_звонок(клиент, "1789000063.7"), str(запись))
    assert ответ.get("job_id") and not клиент.части()


def test_chunk_mb_в_командной_строке_и_файле():
    разбор = агент.собрать_аргументы()
    аргументы = разбор.parse_args(["--url", "http://x", "--key", "ah_x", "--chunk-mb", "2"])
    assert агент.собрать_параметры(аргументы, {})["chunk_mb"] == 2.0
    аргументы = разбор.parse_args(["--url", "http://x", "--key", "ah_x"])
    assert агент.собрать_параметры(аргументы, {})["chunk_mb"] == агент.ЧАСТЬ_МБ


# --- проверка типов ---------------------------------------------------------

def test_with_hint_обычный_метод_и_цепочка_работает():
    from asrhub.errors import ASRHubError, DependencyMissing

    assert "with_hint" in ASRHubError.__dict__
    ошибка = DependencyMissing("whisper", "openai-whisper").with_hint("поставьте пакет")
    assert isinstance(ошибка, DependencyMissing) and ошибка.hint == "поставьте пакет"
    assert ошибка.to_dict()["hint"] == "поставьте пакет"


def test_сервер_и_агент_проходят_mypy_целиком():
    """Проверка типов без замечаний — весь сервер и агент.

    С 3.1.35 по 3.1.37 чистые модули держались храповиком (список
    `tests/mypy-clean.txt`); в 3.1.38 замечаний не осталось нигде, и
    проверяется всё: новое замечание — упавшая проверка.
    """
    if shutil.which("mypy") is None and subprocess.run(
            [sys.executable, "-m", "mypy", "--version"], capture_output=True).returncode:
        pytest.skip("mypy не установлен")
    итог = subprocess.run(
        [sys.executable, "-m", "mypy", "--ignore-missing-imports",
         "--follow-imports=silent", "--no-error-summary", "server/asrhub",
         "agent/asrhub-agent.py"],
        cwd=КОРЕНЬ, capture_output=True, text=True, timeout=900)
    assert итог.returncode == 0, итог.stdout[-4000:]


# --- хеши для автономной установки ---------------------------------------------

def _колесо(каталог: Path, имя: str = "asrhub_probe63", версия: str = "1.0",
            код: str = "ЗНАЧЕНИЕ = 63\n") -> Path:
    """Наименьшее настоящее колесо: pip его ставит."""
    import base64
    import zipfile

    файлы = {
        f"{имя}/__init__.py": код.encode("utf-8"),
        f"{имя}-{версия}.dist-info/METADATA":
            f"Metadata-Version: 2.1\nName: {имя}\nVersion: {версия}\n".encode(),
        f"{имя}-{версия}.dist-info/WHEEL":
            b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    запись = []
    for путь, данные in файлы.items():
        сумма = base64.urlsafe_b64encode(hashlib.sha256(данные).digest()).rstrip(b"=").decode()
        запись.append(f"{путь},sha256={сумма},{len(данные)}")
    запись.append(f"{имя}-{версия}.dist-info/RECORD,,")
    колесо = каталог / f"{имя}-{версия}-py3-none-any.whl"
    with zipfile.ZipFile(колесо, "w") as архив:
        for путь, данные in файлы.items():
            архив.writestr(путь, данные)
        архив.writestr(f"{имя}-{версия}.dist-info/RECORD", "\n".join(запись) + "\n")
    return колесо


def test_файл_хешей_по_каталогу_колёс(tmp_path):
    sys.path.insert(0, str(КОРЕНЬ / "scripts" / "lib"))
    try:
        import wheel_hashes
    finally:
        sys.path.pop(0)
    (tmp_path / "Foo_Bar-2.0-cp312-cp312-manylinux_2_17_x86_64.whl").write_bytes(b"a")
    (tmp_path / "Foo_Bar-2.0-cp312-cp312-musllinux_1_2_x86_64.whl").write_bytes(b"b")
    (tmp_path / "python-multipart-0.0.9.tar.gz").write_bytes(b"c")
    (tmp_path / "readme.md").write_bytes(b"d")
    текст, непонятные = wheel_hashes.собрать(tmp_path)
    assert "foo-bar==2.0 \\" in текст and "python-multipart==0.0.9 \\" in текст
    # Два файла одной версии — два хеша у одной строки.
    assert текст.count("--hash=sha256:") == 3
    assert непонятные == ["readme.md"]
    (tmp_path / "foo_bar-3.0-py3-none-any.whl").write_bytes(b"e")
    with pytest.raises(SystemExit, match="несколько версий"):
        wheel_hashes.собрать(tmp_path)


def test_pip_ставит_по_хешам_и_отвергает_подменённое_колесо(tmp_path):
    колёса = tmp_path / "wheels"
    колёса.mkdir()
    колесо = _колесо(колёса)
    итог = subprocess.run(["bash", str(КОРЕНЬ / "scripts" / "wheels.sh"), str(колёса),
                           "--no-download", "--python", sys.executable],
                          capture_output=True, text=True, timeout=120)
    assert итог.returncode == 0, итог.stderr
    хеши = колёса / "requirements-hashes.txt"
    assert "asrhub-probe63==1.0" in хеши.read_text()

    def поставить(куда: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--no-index", "--find-links", str(колёса), "--require-hashes",
             "-r", str(хеши), "--target", str(куда)],
            capture_output=True, text=True, timeout=300)

    итог = поставить(tmp_path / "t1")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert (tmp_path / "t1" / "asrhub_probe63" / "__init__.py").is_file()
    # Колесо подменили на флешке: pip отказывается.
    колесо.unlink()
    _колесо(колёса, код="import os  # подмена\n")
    итог = поставить(tmp_path / "t2")
    assert итог.returncode != 0
    assert "hash" in (итог.stdout + итог.stderr).lower()


def test_установщики_знают_файл_хешей():
    установка = (КОРЕНЬ / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--hashes ФАЙЛ" in установка
    assert '--require-hashes -r "${HASHES_FILE}"' in установка
    справка = subprocess.run(["bash", str(КОРЕНЬ / "scripts" / "install.sh"), "--help"],
                             capture_output=True, text=True, timeout=60)
    assert "--hashes" in справка.stdout
    окна = (КОРЕНЬ / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$Hashes" in окна and "'--require-hashes'" in окна


def test_whisper_cpp_берёт_ядра_с_учётом_квоты_контейнера(monkeypatch):
    """`cpu_cores_available` — свойство, а вызывалось функцией.

    TypeError гасился запасным путём (`sched_getaffinity`), и квота
    контейнера whisper.cpp не доставалась: на квоте в два ядра из шестнадцати
    он запускал шестнадцать потоков. Нашла проверка типов.
    """
    from asrhub import hardware
    from asrhub.engines import whisper_cpp_engine

    class Сведения:
        cpu_cores_available = 2

    monkeypatch.setattr(hardware, "_постоянное", lambda *_а, **_к: Сведения())
    monkeypatch.setattr(whisper_cpp_engine.os, "sched_getaffinity", lambda _п: set(range(16)),
                        raising=False)
    assert whisper_cpp_engine._ядер() == 2
