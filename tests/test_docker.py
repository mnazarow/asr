"""Проверки развёртывания в контейнере.

Собрать образ на машине сборочного стенда нельзя — демон Docker обычно
недоступен, — но всё, что ломалось на практике, проверяется без него:
состав контекста сборки, корректность файлов Compose и порядок команд в
Dockerfile.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DOCKER_DIR = Path(__file__).resolve().parent.parent / "docker"
ROOT = DOCKER_DIR.parent


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", *args],
        cwd=DOCKER_DIR, capture_output=True, text=True, timeout=60, check=False)


needs_compose = pytest.mark.skipif(
    shutil.which("docker") is None, reason="docker не установлен")


def test_dockerignore_lives_in_build_context_root():
    """Файл .dockerignore действует только в корне контекста сборки.

    Контекст задан как «..», то есть корень репозитория. Пока файл лежал в
    docker/, он не применялся вовсе: в контекст уходили docs/images и .git.
    """
    assert (ROOT / ".dockerignore").exists(), \
        ".dockerignore должен лежать в корне репозитория — там же, где контекст"
    assert not (DOCKER_DIR / ".dockerignore").exists(), \
        "копия в docker/ не действует и вводит в заблуждение"

    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    for heavy in ("docs/", ".git/", "*.whl"):
        assert heavy in text, f"{heavy} не исключён из контекста сборки"


def test_dockerignore_keeps_what_image_needs():
    """Исключения не должны отрезать то, что образ копирует внутрь."""
    ignored = {line.strip() for line in
               (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("#")}
    for needed in ("server/", "scripts/", "requirements/", "VERSION"):
        assert needed not in ignored, f"{needed} копируется в образ, исключать нельзя"


@needs_compose
def test_compose_default_starts_the_server():
    """«docker compose up -d» обязан поднимать сервер.

    Сервис с профилем без --profile не стартует. Раньше у сервера стоял
    profiles: ["", "cpu", "default"], и пустое имя профиля спецификацией не
    предусмотрено.
    """
    result = _compose("config", "--services")
    assert result.returncode == 0, result.stderr
    assert "asrhub" in result.stdout.split()


@needs_compose
def test_compose_proxy_profile_is_valid():
    """Команда из шапки файла не должна ломать конфигурацию.

    depends_on на сервис, которого нет в проекте при выбранном профиле,
    делал недействительной всю конфигурацию: «service nginx depends on
    undefined service asrhub».
    """
    result = _compose("--profile", "proxy", "config", "--services")
    assert result.returncode == 0, result.stderr
    services = set(result.stdout.split())
    assert {"asrhub", "nginx"} <= services, services


@needs_compose
def test_compose_gpu_overlay_replaces_the_same_service():
    """Надстройка для видеокарты меняет тот же сервис, а не добавляет второй.

    Два сервиса с одинаковым портом поднимались вместе, и второй падал с
    «port is already allocated».
    """
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml",
         "-f", "docker-compose.gpu.yml", "config"],
        cwd=DOCKER_DIR, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("container_name: asrhub") == 1, \
        "контейнер должен остаться один"
    assert "ACCEL: cuda" in result.stdout
    assert "cu124" in result.stdout, "индекс пакетов PyTorch не переключился на CUDA"


@needs_compose
def test_compose_gpu_and_proxy_together():
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml",
         "-f", "docker-compose.gpu.yml", "--profile", "proxy", "config", "--services"],
        cwd=DOCKER_DIR, capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    assert {"asrhub", "nginx"} <= set(result.stdout.split())


def test_dockerfile_declares_args_after_second_from():
    """ARG не переживает FROM: во втором слое их нужно объявить заново."""
    text = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    stages = text.split("FROM python:${PYTHON_VERSION}-slim")
    runtime = stages[-1]
    assert "ARG ACCEL" in runtime, "ACCEL не объявлен в рабочем слое"
    assert "ASRHUB_BUILD_ACCEL" in runtime, "ускоритель сборки нигде не виден"


def test_dockerfile_drops_privileges_in_entrypoint():
    """Права понижаются в entrypoint, а не директивой USER.

    Владельца смонтированного тома можно поправить только от root, поэтому
    контейнер стартует от root и сразу переходит на непривилегированного
    пользователя через gosu.
    """
    text = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "gosu" in text, "gosu не установлен — понижать права нечем"
    assert "\nUSER asrhub" not in text, \
        "директива USER не даст entrypoint поправить права на томе"

    entry = (DOCKER_DIR / "entrypoint.sh").read_text(encoding="utf-8")
    assert 'exec gosu' in entry, "entrypoint не понижает права"
    directives = "\n".join(line for line in text.splitlines()
                            if not line.lstrip().startswith("#"))
    assert "OMP_NUM_THREADS" not in directives, \
        "OMP_NUM_THREADS=0 — недопустимое для OpenMP значение"


def test_nginx_resolves_backend_at_request_time():
    """Прокси должен запускаться, даже когда сервер ещё не поднялся.

    С блоком upstream nginx разрешает имя один раз при старте и выходит с
    «host not found in upstream».
    """
    text = (DOCKER_DIR / "nginx.conf").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "resolver" in body, "нет resolver — имя разрешается только при старте"
    assert "upstream asrhub" not in body, "статический upstream возвращает старую беду"
    assert "$request_uri" in body, "с переменной в proxy_pass путь нужно передавать явно"


def test_nginx_protects_all_monitoring_paths():
    """Ограничение по сети должно закрывать и /api/monitoring/."""
    text = (DOCKER_DIR / "nginx.conf").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "monitoring" in body and "deny all" in body, \
        "полный снимок метрик остаётся открыт наружу"


@pytest.mark.skipif(shutil.which("nginx") is None, reason="nginx не установлен")
def test_nginx_config_is_syntactically_valid(tmp_path: Path):
    """Конфигурацию проверяем настоящим nginx, а не глазами."""
    conf_d = tmp_path / "conf.d"
    conf_d.mkdir()
    (conf_d / "default.conf").write_text(
        (DOCKER_DIR / "nginx.conf").read_text(encoding="utf-8")
        .replace("resolver 127.0.0.11", "resolver 127.0.0.53")
        .replace("http://asrhub:8080", "http://127.0.0.1:8080"),
        encoding="utf-8")
    main = tmp_path / "nginx.conf"
    main.write_text(f"""
worker_processes 1;
error_log {tmp_path}/error.log;
pid {tmp_path}/nginx.pid;
events {{ worker_connections 64; }}
http {{
    access_log off;
    client_body_temp_path {tmp_path}/body;
    proxy_temp_path {tmp_path}/proxy;
    fastcgi_temp_path {tmp_path}/fcgi;
    uwsgi_temp_path {tmp_path}/uwsgi;
    scgi_temp_path {tmp_path}/scgi;
    include {conf_d}/*.conf;
}}
""", encoding="utf-8")
    result = subprocess.run(["nginx", "-t", "-c", str(main)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr


def test_entrypoint_is_executable_and_clean():
    entry = DOCKER_DIR / "entrypoint.sh"
    assert entry.stat().st_mode & 0o111, "entrypoint.sh не исполняемый"
    result = subprocess.run(["bash", "-n", str(entry)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
