"""Заход 54: упаковка и контейнер (участок packaging) и --print-config (08-params №4).

docker в среде нет, поэтому образ не собирается: команда выбора и установки
движков из Dockerfile исполняется как обычный shell с подменённым pip (как в
test_review_47_windows), точка входа — по-настоящему с gosu на setpriv, файлы
Compose разбираются как YAML. Примеры из examples/ гоняются против
маленького HTTP-сервера в этом же процессе.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from test_review_54_install import BASH, КОРЕНЬ, _исполняемый, нужен_root

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


def _python_сервера() -> dict[str, str]:
    """Окружение, в котором `python -m asrhub` берёт пакет из этого дерева."""
    return {**os.environ, "PYTHONPATH": f"{КОРЕНЬ / 'server'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}


# ---------------------------------------------------------------------------
# 08-params №4. --print-config
# ---------------------------------------------------------------------------


def test_справочник_конфигурации_читается_самим_сервером(tmp_path: Path):
    """`--print-config` печатал YAML, на котором сервер спотыкался.

    Строки «on»/«off»/«no», числа в строках, «#» и «:» внутри значений и
    многострочные описания в комментариях YAML читал иначе, чем задумано, —
    скопированный «как есть» справочник давал десятки замечаний при запуске.
    """
    окружение = _python_сервера()
    итог = subprocess.run([sys.executable, "-m", "asrhub", "--print-config"], cwd=str(КОРЕНЬ / "server"),
                          capture_output=True, text=True, env=окружение, timeout=120, check=False)
    assert итог.returncode == 0, итог.stderr
    пример = tmp_path / "config.yaml"
    пример.write_text(итог.stdout, encoding="utf-8")
    код = ("import json, sys\nfrom asrhub import config, catalog\n"
           "s = config.load(sys.argv[1], apply_hardware=False, ensure_key=False)\n"
           "print(json.dumps(s.problems, ensure_ascii=False))\n")
    проверка = subprocess.run([sys.executable, "-c", код, str(пример)], capture_output=True, text=True,
                              env={**окружение, "ASRHUB_DATA_DIR": str(tmp_path / "data")},
                              timeout=120, check=False)
    assert проверка.returncode == 0, проверка.stderr
    замечания = json.loads(проверка.stdout.strip().splitlines()[-1])
    assert замечания == [], замечания[:10]
    if yaml is not None:
        assert isinstance(yaml.safe_load(итог.stdout), dict)


# ---------------------------------------------------------------------------
# №15. pyproject.toml
# ---------------------------------------------------------------------------


def test_pyproject_собирает_пакет_с_точкой_входа(tmp_path: Path):
    """`pip install .` падал: «Multiple top-level packages … agent, server, docker»."""
    try:
        import setuptools  # noqa: F401
        from setuptools import build_meta  # noqa: F401
    except ImportError:
        pytest.skip("нет setuptools")
    копия = tmp_path / "src"
    копия.mkdir()
    for имя in ("pyproject.toml", "README.md", "VERSION"):
        shutil.copy2(КОРЕНЬ / имя, копия / имя)
    for каталог in ("server", "requirements", "agent", "docker", "scripts"):
        shutil.copytree(КОРЕНЬ / каталог, копия / каталог,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "data", ".env"))
    мета = tmp_path / "meta"
    мета.mkdir()
    итог = subprocess.run(
        [sys.executable, "-c", "import sys\nfrom setuptools import build_meta as b\n"
                               "print(b.prepare_metadata_for_build_wheel(sys.argv[1]))", str(мета)],
        cwd=str(копия), capture_output=True, text=True, timeout=300, check=False)
    assert итог.returncode == 0, итог.stdout[-2000:] + итог.stderr[-3000:]
    dist = next(мета.glob("*.dist-info"))
    точки = (dist / "entry_points.txt").read_text(encoding="utf-8")
    assert "asrhub = asrhub.__main__:main" in точки
    сведения = (dist / "METADATA").read_text(encoding="utf-8")
    assert "Requires-Dist: uvicorn[standard]" in сведения
    assert "Requires-Dist: fastapi" in сведения
    верх = (dist / "top_level.txt").read_text(encoding="utf-8").split() \
        if (dist / "top_level.txt").exists() else ["asrhub"]
    assert верх == ["asrhub"], верх


# ---------------------------------------------------------------------------
# №6, №3, №10. Установка движков в образ
# ---------------------------------------------------------------------------


def _блок_движков() -> str:
    текст = (КОРЕНЬ / "docker" / "Dockerfile").read_text(encoding="utf-8")
    начало = текст.index("RUN set -eux; \\\n    if [ -n \"${ENGINES}\" ]")
    конец = текст.index("\n\n", начало)
    return текст[начало + len("RUN "):конец].replace("\\\n", "\n")


def _сборка(tmp_path: Path, **переменные: str) -> tuple[subprocess.CompletedProcess, Path, Path]:
    журнал = tmp_path / "pip.log"
    корзина = tmp_path / "bin"
    # Подменённый pip: «vosk» не ставится; freeze отдаёт поставленный torch.
    _исполняемый(корзина / "pip", f'#!/bin/bash\necho "pip $*" >> "{журнал}"\n'
                 'case "$*" in *engines/vosk.txt*) exit 1 ;; freeze*) echo "torch==2.5.1+cu128"; '
                 'echo "torchaudio==2.5.1+cu128"; echo "numpy==2.1.0" ;; esac\nexit 0\n')
    if not (tmp_path / "requirements").exists():
        shutil.copytree(КОРЕНЬ / "requirements", tmp_path / "requirements")
    движки = tmp_path / "engines.txt"
    torch = tmp_path / "torch.txt"
    блок = (_блок_движков().replace("/opt/venv/.asrhub-engines", str(движки))
            .replace("/opt/venv/.asrhub-torch-constraints", str(torch)))
    итог = subprocess.run(["sh", "-c", блок], cwd=str(tmp_path), capture_output=True, text=True,
                          timeout=60, check=False,
                          env={**os.environ, "PATH": f"{корзина}:{os.environ['PATH']}",
                               "TORCH_INDEX": "https://download.pytorch.org/whl/cu128",
                               "ENGINES": "", "PROFILE": "standard", **переменные})
    return итог, движки, журнал


def test_явно_заданный_движок_который_не_встал_останавливает_сборку(tmp_path: Path):
    """Образ «собирался» без движка, а контейнер рапортовал, что движок есть."""
    итог, движки, _ = _сборка(tmp_path, ENGINES="gigaam,vosk")
    assert итог.returncode != 0, итог.stdout + итог.stderr
    assert "сборка остановлена" in итог.stdout


def test_в_список_движков_образа_попадает_только_вставшее(tmp_path: Path):
    """Движок профиля, не вставший, — предупреждение, но и не «Движки в образе»."""
    итог, движки, _ = _сборка(tmp_path, PROFILE="russian")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    состав = движки.read_text(encoding="utf-8").split()
    assert "gigaam" in состав
    assert "vosk" not in состав, состав


def test_движки_ставятся_с_закреплённым_torch(tmp_path: Path):
    """Движок с собственными требованиями перетягивал torch с PyPI — процессорный."""
    итог, _, журнал = _сборка(tmp_path, ENGINES="gigaam,faster-whisper")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    вызовы = журнал.read_text(encoding="utf-8").splitlines()
    torch_ставится = [в for в in вызовы if "--index-url" in в and "torch" in в]
    assert torch_ставится and "cu128" in torch_ставится[0], вызовы
    движки = [в for в in вызовы if "-r requirements/engines/gigaam.txt" in в
              or "-r requirements/engines/faster-whisper.txt" in в]
    assert движки and all("-c " in в for в in движки), движки
    assert (tmp_path / "torch.txt").read_text(encoding="utf-8").startswith("torch==2.5.1")


def test_образ_получает_каталог_агента():
    """В образ не попадал agent/ — страница «Агент» отдавала 404 на установщик."""
    текст = (КОРЕНЬ / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY agent/ ./agent/" in текст
    исключения = (КОРЕНЬ / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "agent/" not in исключения and "agent" not in исключения


# ---------------------------------------------------------------------------
# №7, №8, №16, №3, №5. Compose
# ---------------------------------------------------------------------------


@pytest.fixture()
def compose_loader():
    if yaml is None:
        pytest.skip("нет PyYAML")

    class Загрузчик(yaml.SafeLoader):
        pass

    # Теги Compose 2.24+: «!reset» снимает значение из основного файла.
    def значение(loader, node):
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node)
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node)
        return None

    Загрузчик.add_constructor("!reset", lambda loader, node: {"__reset__": значение(loader, node)})
    Загрузчик.add_constructor("!override", значение)
    return lambda имя: yaml.load((КОРЕНЬ / "docker" / имя).read_text(encoding="utf-8"), Загрузчик)


def test_compose_не_перекрывает_настройки_из_интерфейса(compose_loader):
    """ASRHUB_MAX_CONCURRENT_JOBS: ${ASRHUB_WORKERS:-2} и LOG_LEVEL INFO — каждый
    перезапуск возвращал число заданий и уровень журнала к умолчанию."""
    основной = compose_loader("docker-compose.yml")["services"]["asrhub"]
    среда = основной["environment"]
    for имя in ("ASRHUB_MAX_CONCURRENT_JOBS", "ASRHUB_LOG_LEVEL", "ASRHUB_LLM_URL"):
        assert имя in среда and среда[имя] is None, (имя, среда.get(имя))
    assert "host.docker.internal:host-gateway" in основной["extra_hosts"]
    gpu = compose_loader("docker-compose.gpu.yml")["services"]["asrhub"]
    for имя in ("ASRHUB_DEVICE", "ASRHUB_COMPUTE_TYPE"):
        assert gpu["environment"].get(имя, "") is None, (имя, gpu["environment"].get(имя))
    индекс = gpu["build"]["args"]["TORCH_INDEX"]
    assert "ASRHUB_TORCH_INDEX" in индекс and "cu124" not in индекс, индекс


def test_надстройка_прокси_закрывает_прямой_порт(compose_loader):
    """С `--profile proxy` сервер оставался на 0.0.0.0:8080 рядом с nginx."""
    прокси = compose_loader("docker-compose.proxy.yml")["services"]
    assert прокси["asrhub"]["ports"] == {"__reset__": []}
    assert прокси["nginx"]["profiles"] == {"__reset__": []}


def _compose(*аргументы: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", "compose", *аргументы], cwd=str(КОРЕНЬ / "docker"),
                          capture_output=True, text=True, timeout=60, check=False,
                          env={**os.environ, **(env or {})})


def _есть_compose() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return _compose("version").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _есть_compose(), reason="нет docker compose")
def test_compose_разбирает_надстройку_прокси_и_передаёт_только_заданное(tmp_path: Path):
    """Настоящий `docker compose config` (демон ему не нужен).

    Надстройка прокси снимает порты сервера и поднимает nginx без профиля;
    число заданий из .env доходит до контейнера, а не заданное — нет.
    """
    итог = _compose("-f", "docker-compose.yml", "-f", "docker-compose.proxy.yml",
                    "config", "--format", "json")
    assert итог.returncode == 0, итог.stderr
    сервисы = json.loads(итог.stdout)["services"]
    assert "nginx" in сервисы
    assert not сервисы["asrhub"].get("ports"), сервисы["asrhub"].get("ports")
    env_file = tmp_path / "local.env"
    env_file.write_text("ASRHUB_MAX_CONCURRENT_JOBS=5\n", encoding="utf-8")
    итог = _compose("--env-file", str(env_file), "config", "--format", "json")
    assert итог.returncode == 0, итог.stderr
    среда = json.loads(итог.stdout)["services"]["asrhub"]["environment"]
    assert среда.get("ASRHUB_MAX_CONCURRENT_JOBS") == "5", среда
    assert среда.get("ASRHUB_LOG_LEVEL") in (None, ""), среда


def test_nginx_не_пропускает_присланный_клиентом_адрес():
    """`$proxy_add_x_forwarded_for` дописывал адрес к присланному клиентом:
    сервер брал первый — подделанный — и предел попыток входа обходился.

    Проверка по тексту: nginx в среде нет (поведенческая проверка шаблона —
    test_review_47_windows, она запускается там, где nginx есть).
    """
    текст = (КОРЕНЬ / "docker" / "nginx.conf").read_text(encoding="utf-8")
    рабочие = [с.strip() for с in текст.splitlines() if not с.lstrip().startswith("#")]
    xff = [с for с in рабочие if "X-Forwarded-For" in с]
    assert xff and all(с.endswith("$remote_addr;") for с in xff), xff
    assert "client_max_body_size 0;" in рабочие


def test_контекст_сборки_не_уносит_секреты_и_кеши():
    """Шаблоны без «**/» действуют только в корне контекста (по тексту: docker нет)."""
    строки = {с.strip() for с in (КОРЕНЬ / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    for нужно in ("**/.env", "**/__pycache__/", "**/*.pyc", "docker/data/", "tests/"):
        assert нужно in строки, нужно


# ---------------------------------------------------------------------------
# №18, №8. Точка входа
# ---------------------------------------------------------------------------


@нужен_root
def test_точка_входа_кладёт_дом_и_кеши_на_том_и_понимает_прежнее_имя():
    """После gosu HOME оставался /root, а ASRHUB_WORKERS из .env больше не читался бы."""
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        данные = база / "data"
        корзина = база / "ebin"
        _исполняемый(корзина / "gosu", '#!/bin/bash\nug="$1"; shift\n'
                     'exec setpriv --reuid="${ug%%:*}" --regid="${ug##*:}" --clear-groups "$@"\n')
        _исполняемый(корзина / "python", "#!/bin/sh\ncat >/dev/null\necho 'Ускоритель: CPU'\n")
        итог = subprocess.run(
            [BASH, str(КОРЕНЬ / "docker" / "entrypoint.sh"), "sh", "-c",
             'echo "H=$HOME|X=$XDG_CACHE_HOME|J=$ASRHUB_MAX_CONCURRENT_JOBS|U=$(id -u)"'],
            env={**os.environ, "PATH": f"{корзина}:{os.environ['PATH']}",
                 "ASRHUB_DATA_DIR": str(данные), "ASRHUB_UID": "4321", "ASRHUB_GID": "4321",
                 "HOME": "/root", "ASRHUB_WORKERS": "5"},
            capture_output=True, text=True, timeout=60, check=False)
        assert итог.returncode == 0, итог.stdout + итог.stderr
        строка = [с for с in итог.stdout.splitlines() if с.startswith("H=")][-1]
        поля = dict(часть.split("=", 1) for часть in строка.split("|"))
        assert поля["U"] == "4321"
        assert поля["H"].startswith(str(данные)), поля
        assert поля["X"].startswith(str(данные)), поля
        assert поля["J"] == "5", поля
    finally:
        shutil.rmtree(база, ignore_errors=True)


# ---------------------------------------------------------------------------
# №13, №14. Примеры
# ---------------------------------------------------------------------------


class _Стенд:
    def __init__(self, обработчик):
        класс = type("Обработчик", (BaseHTTPRequestHandler,),
                     {"do_GET": обработчик, "do_POST": обработчик,
                      "log_message": lambda self, *a: None})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), класс)
        self.адрес = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()


def _ответить(запрос, данные, код: int = 200, тип: str = "application/json"):
    тело = данные if isinstance(данные, bytes) else json.dumps(данные, ensure_ascii=False).encode()
    запрос.send_response(код)
    запрос.send_header("Content-Type", тип)
    запрос.send_header("Content-Length", str(len(тело)))
    запрос.end_headers()
    запрос.wfile.write(тело)


def _окружение_примера(адрес: str) -> dict[str, str]:
    окружение = {к: з for к, з in os.environ.items()
                 if к.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    окружение.update({"ASRHUB_SERVER": адрес, "ASRHUB_KEY": "", "NO_PROXY": "127.0.0.1,localhost"})
    return окружение


def test_пакетный_пример_листает_страницы_и_не_затирает_файлы(tmp_path: Path):
    """Один запрос limit=500 без offset: задания за первой страницей не
    находились никогда, и скрипт крутился вечно. И результат писался поверх
    лежащего рядом запись.txt — эталонного текста."""
    pytest.importorskip("requests")
    созданные: list[str] = []
    чужие = [{"id": f"other_{n}", "status": "running"} for n in range(500)]

    def обработчик(запрос):
        адрес = urlparse(запрос.path)
        длина = int(запрос.headers.get("Content-Length") or 0)
        if длина:
            запрос.rfile.read(длина)
        if запрос.command == "POST" and адрес.path == "/api/jobs":
            созданные.append(f"job_{len(созданные) + 1}")
            return _ответить(запрос, {"id": созданные[-1]})
        if адрес.path == "/api/jobs":
            параметры = parse_qs(адрес.query)
            offset = int(параметры.get("offset", ["0"])[0])
            limit = int(параметры.get("limit", ["50"])[0])
            # Новые — первыми: 500 чужих заданий закрывают первую страницу.
            все = чужие + [{"id": i, "status": "completed"} for i in созданные]
            return _ответить(запрос, {"items": все[offset:offset + limit], "total": len(все)})
        if адрес.path.endswith("/download"):
            return _ответить(запрос, "расшифровка сервера\n".encode(), тип="text/plain")
        return _ответить(запрос, {"message": "нет"}, 404)

    папка = tmp_path / "архив"
    папка.mkdir()
    (папка / "запись.wav").write_bytes(b"RIFF0000WAVE")
    (папка / "запись.txt").write_text("эталон\n", encoding="utf-8")
    with _Стенд(обработчик) as сервер:
        try:
            итог = subprocess.run([sys.executable, str(КОРЕНЬ / "examples" / "batch_folder.py"), str(папка)],
                                  capture_output=True, text=True, timeout=60, check=False,
                                  env=_окружение_примера(сервер.адрес))
        except subprocess.TimeoutExpired:
            pytest.fail("пример не нашёл своё задание за первой страницей и крутился вечно")
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert (папка / "запись.txt").read_text(encoding="utf-8") == "эталон\n"
    assert (папка / "запись.asrhub.txt").read_text(encoding="utf-8") == "расшифровка сервера\n"


def test_пример_наблюдения_не_рисует_аварий_на_здоровом_сервере(tmp_path: Path):
    """Порог доли сравнивался с сырым счётчиком: «Заданий обработано: 4 — порог > 0.2»."""
    pytest.importorskip("requests")
    метрики = {"metrics": [{
        "name": "asrhub_jobs_total", "label": "Заданий обработано", "type": "counter", "unit": "",
        "threshold": {"direction": "above", "warning": 0.1, "critical": 0.2,
                      "note": "доля отказов за 30 минут"},
        "values": [{"labels": {"status": "completed"}, "value": 4}]}]}

    def обработчик(запрос):
        путь = urlparse(запрос.path).path
        if путь == "/api/monitoring/metrics.json":
            return _ответить(запрос, метрики)
        if путь == "/api/monitoring/health":
            return _ответить(запрос, {"status": "healthy"})
        if путь == "/api/monitoring/alerts":
            return _ответить(запрос, {"summary": {"firing": 0}, "alerts": [
                {"id": "r1", "state": "ok", "severity": "critical", "metric": "asrhub_jobs_total"}]})
        return _ответить(запрос, {"message": "нет"}, 404)

    with _Стенд(обработчик) as сервер:
        итог = subprocess.run([sys.executable, str(КОРЕНЬ / "examples" / "monitoring_pull.py")],
                              capture_output=True, text=True, timeout=60, check=False,
                              env=_окружение_примера(сервер.адрес))
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "✕" not in итог.stdout, итог.stdout
    assert "Тревог нет" in итог.stdout


# ---------------------------------------------------------------------------
# №12. Сборка документации
# ---------------------------------------------------------------------------


def test_fix_docx_без_схемы_не_обрывает_сборку(tmp_path: Path):
    """Схема OOXML бралась только с машины автора — без неё шаг 7 из 8 падал."""
    pytest.importorskip("lxml")
    файл = tmp_path / "doc.docx"
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    with zipfile.ZipFile(файл, "w") as архив:
        архив.writestr("[Content_Types].xml",
                       '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                       'package/2006/content-types"></Types>')
        архив.writestr("word/document.xml",
                       f'<?xml version="1.0"?><w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>Текст'
                       '</w:t></w:r></w:p></w:body></w:document>')
    итог = subprocess.run([sys.executable, str(КОРЕНЬ / "docs" / "fix_docx.py"), str(файл)],
                          capture_output=True, text=True, timeout=120, check=False,
                          env={**os.environ, "ASRHUB_WML_XSD": str(tmp_path / "нет" / "wml.xsd")})
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "ASRHUB_WML_XSD" in итог.stdout
    with zipfile.ZipFile(файл) as архив:
        assert "word/document.xml" in архив.namelist()


def test_быстрый_старт_ставит_uvicorn_с_websocket():
    """`pip install fastapi uvicorn` — без библиотеки WebSocket /ws отвечал 404."""
    текст = (КОРЕНЬ / "README.md").read_text(encoding="utf-8")
    строка = next(с for с in текст.splitlines() if "pip install" in с and "fastapi" in с)
    assert "uvicorn[standard]" in строка, строка
