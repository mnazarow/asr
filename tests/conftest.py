"""Общие приспособления для тестов ASR Hub."""
from __future__ import annotations

import subprocess
import sys
import wave
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

# Пароль первого запуска в проверках — прежний, известный: сотни проверок
# входят как admin/admin123. Сам сервер без этой переменной заводит
# случайный пароль (см. accounts.ensure_default_admin и test_review_57).
import os  # noqa: E402

os.environ.setdefault("ASRHUB_ADMIN_PASSWORD", "admin123")


def pytest_sessionstart(session: pytest.Session) -> None:
    """С ASRHUB_REQUIRE_BROWSER=1 нет браузера — ошибка, а не пропуск.

    Браузерных проверок — сотни, и без Playwright или Chromium они молча
    пропускаются: прогон «зелёный», а интерфейс не проверен вовсе. В
    сборочном конвейере эту переменную надо ставить.
    """
    import os

    if os.environ.get("ASRHUB_REQUIRE_BROWSER", "") not in ("1", "true", "yes"):
        return
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        pytest.exit("ASRHUB_REQUIRE_BROWSER=1, а пакета playwright нет: "
                    "pip install -r requirements/dev.txt", returncode=4)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from drill_seed import браузер

    if браузер() is None:
        pytest.exit("ASRHUB_REQUIRE_BROWSER=1, а Chromium не найден: python -m playwright "
                    "install chromium или PLAYWRIGHT_CHROMIUM=<путь>", returncode=4)


#: Метки по имени файла проверок (см. markers в pyproject.toml).
_МЕТКИ_ФАЙЛОВ = (
    ("web", ("_web.py", "test_web_ui.py", "test_manual.py")),
    ("scripts", ("_scripts.py", "test_install_scripts.py", "test_docker.py",
                 "test_gpu_detect.py", "test_review_47_windows.py", "_install.py",
                 "_install_ops.py", "_install_packaging.py", "_install_ps.py")),
    ("drill", ("test_drill_",)),
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Ярус каждой проверки — по файлу: быстрый ярус не ждёт браузера и сценариев."""
    for item in items:
        имя = Path(str(item.fspath)).name
        for метка, признаки in _МЕТКИ_ФАЙЛОВ:
            if any(имя.endswith(п) or имя.startswith(п) or имя == п for п in признаки):
                item.add_marker(getattr(pytest.mark, метка))


@pytest.fixture(autouse=True, scope="session")
def _подтверждения_как_диалоги_playwright():
    """Подтверждения интерфейса — «Отмена», пока проверка не ответила сама.

    До 3.1.29 подтверждения были системным confirm(), и Playwright без
    обработчика отвечал на них «Отмена». Теперь это окна интерфейса
    (`подтвердить` в app.js): без ответа окно ждало бы человека, а проверка,
    не знающая о нём, — своего срока. Каждая новая страница получает тот же
    ответ по умолчанию; «Да» — `drill_seed.отвечать(страница, True)`.
    """
    try:
        from playwright.sync_api import Browser
    except ImportError:
        yield
        return
    скрипт = "if (window.__asrhubConfirm === undefined) { window.__asrhubConfirm = false; }"
    контекст_был, страница_была = Browser.new_context, Browser.new_page

    # Политика содержимого страницы (3.1.28) запрещает eval, а им пользуется
    # wait_for_function с выражением строкой: проверкам она не нужна —
    # заголовок и его содержимое проверяются отдельно (test_review_56).
    def new_context(self, *args, **kwargs):
        kwargs.setdefault("bypass_csp", True)
        контекст = контекст_был(self, *args, **kwargs)
        контекст.add_init_script(скрипт)
        return контекст

    def new_page(self, *args, **kwargs):
        kwargs.setdefault("bypass_csp", True)
        страница = страница_была(self, *args, **kwargs)
        страница.add_init_script(скрипт)
        return страница

    Browser.new_context, Browser.new_page = new_context, new_page
    try:
        yield
    finally:
        Browser.new_context, Browser.new_page = контекст_был, страница_была


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Изолированный каталог данных на каждый тест."""
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    return tmp_path / "data"


@pytest.fixture(scope="session")
def sample_wav(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Тестовая запись: тон, тишина, тон — чтобы VAD нашёл два участка."""
    path = tmp_path_factory.mktemp("audio") / "sample.wav"
    try:
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=320:duration=3",
            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=480:duration=2",
            "-filter_complex",
            "[0:a]atrim=0:3[a];[1:a]atrim=0:1.2[b];[2:a]atrim=0:2[c];[a][b][c]concat=n=3:v=0:a=1[o]",
            "-map", "[o]", "-ar", "16000", "-ac", "1", str(path),
        ], check=True, capture_output=True, timeout=60)
    except (FileNotFoundError, subprocess.SubprocessError):
        # ffmpeg недоступен — собираем WAV средствами стандартной библиотеки
        import math
        import struct

        rate = 16000
        frames = bytearray()
        for index in range(rate * 6):
            second = index / rate
            amplitude = 0.0 if 3.0 <= second < 4.2 else 0.4
            value = int(amplitude * 32767 * math.sin(2 * math.pi * 320 * second))
            frames += struct.pack("<h", value)
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(rate)
            handle.writeframes(bytes(frames))
    return path


@pytest.fixture()
def client(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Сервер с демо-движком: полный цикл задания без настоящих моделей."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "2")
    app = create_app(load(), start_queue=True)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _приёмники_уведомлений_без_пауз():
    """Пауза мёртвого приёмника уведомлений (3.1.32) — на процесс, а проверки
    делят процесс: приёмник на «127.0.0.1:9», не ответивший трём прежним
    проверкам, ставился бы на паузу и в следующей."""
    try:
        from asrhub import job_queue
    except ImportError:
        yield
        return
    job_queue._УЗЛЫ_УВЕДОМЛЕНИЙ.clear()
    yield
    job_queue._УЗЛЫ_УВЕДОМЛЕНИЙ.clear()
