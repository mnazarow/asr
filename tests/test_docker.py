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


# ---------------------------------------------------------------------------
# Согласие образа с установщиком
#
# Мест, где написан состав установки, два: profile_engines() в install.sh и
# case по профилям в Dockerfile. Разойтись они могут молча — и разошлись:
# «full» при обычной установке давал десять движков, а в образе семь, причём
# без tone, ради которого профиль «russian» и заводился. Ниже оба списка
# разбираются и сверяются, а намеренные расхождения перечислены в самом
# Dockerfile строками «ИСКЛЮЧЕНО-В-ОБРАЗЕ» и «ДОБАВЛЕНО-В-ОБРАЗЕ».
# ---------------------------------------------------------------------------

INSTALL_SH = ROOT / "scripts" / "install.sh"


def _набор(строка: str) -> set[str]:
    """Список движков в едином виде: через дефис, без запятых и кавычек."""
    return {с for с in строка.replace(",", " ").replace("_", "-").split() if с}


def _профили_установщика() -> dict[str, set[str]]:
    import re
    текст = INSTALL_SH.read_text(encoding="utf-8")
    тело = текст.split("profile_engines()", 1)[1].split("}", 1)[0]
    найдено = re.findall(r"^\s*(\w+)\)\s*printf '([^']*)'", тело, re.M)
    return {имя: _набор(движки) for имя, движки in найдено if имя != "*"}


def _профили_образа() -> dict[str, set[str]]:
    import re
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    тело = текст.split("case \"${PROFILE}\" in", 1)[1].split("esac", 1)[0]
    найдено = re.findall(r'^\s*([\w|]+)\)\s*LIST="([^"]*)"', тело, re.M)
    разобрано: dict[str, set[str]] = {}
    for имена, движки in найдено:
        for имя in имена.split("|"):
            if имя != "*":
                разобрано[имя] = _набор(движки)
    return разобрано


def _помеченные(метка: str) -> set[str]:
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    for строка in текст.splitlines():
        if метка in строка:
            return _набор(строка.split(метка, 1)[1])
    return set()


def test_image_knows_every_profile_the_installer_offers():
    """Профиль, которого нет в Dockerfile, тихо превращался в «light».

    «install.sh --mode docker --profile apple» собирал образ с одним
    faster-whisper и ничего об этом не говорил.
    """
    установщик = _профили_установщика()
    образ = _профили_образа()
    assert установщик, "не удалось разобрать profile_engines() в install.sh"
    пропущены = sorted(set(установщик) - set(образ))
    assert not пропущены, (
        "профили установщика без ветки в Dockerfile: " + ", ".join(пропущены))


def test_engine_sets_match_the_installer_except_where_stated():
    """Состав движков расходится только там, где это написано словами."""
    установщик = _профили_установщика()
    образ = _профили_образа()
    исключено = _помеченные("ИСКЛЮЧЕНО-В-ОБРАЗЕ:")
    добавлено = _помеченные("ДОБАВЛЕНО-В-ОБРАЗЕ:")
    assert исключено, "в Dockerfile нет строки ИСКЛЮЧЕНО-В-ОБРАЗЕ"

    for профиль, ожидалось in установщик.items():
        есть = образ.get(профиль, set())
        лишние = есть - ожидалось - добавлено
        нехватка = ожидалось - есть - исключено
        assert not лишние, (
            f"профиль {профиль}: в образе лишние движки {sorted(лишние)} — "
            "либо уберите, либо впишите в ДОБАВЛЕНО-В-ОБРАЗЕ")
        assert not нехватка, (
            f"профиль {профиль}: в образе нет {sorted(нехватка)}, "
            "а обычная установка их ставит — впишите в ИСКЛЮЧЕНО-В-ОБРАЗЕ")


def test_explicit_engine_list_wins_over_the_profile():
    """Ключ --engines установщика доходил до .env и там умирал.

    Человеку говорили «Движки: gigaam,vosk», а собирался набор профиля.
    """
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "ARG ENGINES" in текст, "аргумент сборки ENGINES не объявлен"
    выбор = текст.split("case \"${PROFILE}\" in", 1)[0].rsplit("RUN set -eux", 1)[-1]
    assert 'if [ -n "${ENGINES}" ]' in выбор, \
        "явный список не проверяется раньше профиля"
    assert "tr ',_' ' -'" in выбор, \
        "имена движков установщика (faster_whisper) не приводятся к именам файлов"


@needs_compose
def test_compose_passes_the_engine_list_from_the_env_file():
    """Список из .env должен доезжать до аргументов сборки."""
    import os
    среда = dict(os.environ, ASRHUB_ENGINES="gigaam,vosk")
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config"],
        cwd=DOCKER_DIR, capture_output=True, text=True, timeout=60,
        check=False, env=среда)
    assert result.returncode == 0, result.stderr
    assert "ENGINES: gigaam,vosk" in result.stdout, result.stdout[:800]


@needs_compose
def test_installer_host_limits_where_the_port_is_published():
    """«--host 127.0.0.1» в режиме docker открывал порт на всех интерфейсах.

    Установщик писал ASRHUB_HOST в .env, а Compose знал только про порт:
    установка «только для себя» выставляла сервер в сеть.
    """
    import os
    среда = dict(os.environ, ASRHUB_HOST="127.0.0.1", ASRHUB_PORT="9111")
    result = subprocess.run(
        ["docker", "compose", "-f", "docker-compose.yml", "config"],
        cwd=DOCKER_DIR, capture_output=True, text=True, timeout=60,
        check=False, env=среда)
    assert result.returncode == 0, result.stderr
    assert "host_ip: 127.0.0.1" in result.stdout, result.stdout[:800]
    assert 'published: "9111"' in result.stdout, result.stdout[:800]


def test_image_version_label_follows_the_version_file():
    """Версия в метке образа была вписана числом и отставала от VERSION."""
    версия = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert 'org.opencontainers.image.version="${VERSION}"' in текст, \
        "метка версии не берётся из аргумента сборки"
    assert f"ARG VERSION={версия}" in текст, (
        f"значение по умолчанию разошлось с файлом VERSION ({версия})")


def test_image_restores_the_versions_the_installer_restores():
    """overrides.txt применялся только при обычной установке.

    В образе с диаризацией это оставляло protobuf, который отказывается
    грузить код googleapis-common-protos, — движок падал на импорте.
    """
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "overrides.txt" in текст, "версии из overrides.txt не восстанавливаются"
    строка = next(с for с in текст.splitlines() if "overrides.txt" in с)
    assert "--no-deps" in строка, \
        "без --no-deps pip вернёт ResolutionImpossible: строки спорят с метаданными"
    место = текст.index("overrides.txt")
    assert место > текст.index("установка движка"), \
        "восстанавливать версии нужно после движков, иначе последний перетянет"


# ---------------------------------------------------------------------------
# Выбор движков исполняется, а не читается глазами
#
# Собрать образ на стенде нельзя: реестры образов недоступны. Но команда,
# которая выбирает движки, — обычный shell, и её можно выполнить как есть,
# подставив вместо pip запись в файл. Так проверяется то, ради чего блок и
# писался: что явный список побеждает профиль, что имена с подчёркиванием
# доезжают до файлов с дефисом и что неизвестный движок не роняет сборку.
# ---------------------------------------------------------------------------

ДВИЖКИ_В_ФАЙЛАХ = ("faster-whisper", "gigaam", "vosk", "whisper", "tone",
                   "transformers", "vad", "postprocess", "diarization")


def _команда_выбора() -> str:
    """Тот же текст, что исполняет BuildKit, — без директивы RUN."""
    текст = (DOCKER_DIR / "Dockerfile").read_text(encoding="utf-8")
    начало = текст.index("RUN set -eux; \\")
    конец = текст.index("\n\n", начало)
    блок = текст[начало:конец]
    строки = [с[4:] if i == 0 else с
              for i, с in enumerate(блок.splitlines())]
    return "\n".join(с.rstrip("\\").rstrip() for с in строки)


def _выбрать(tmp_path: Path, профиль: str = "standard",
             движки: str = "") -> tuple[list[str], str]:
    """Выполняет блок выбора и возвращает (что ставили, что напечатали)."""
    (tmp_path / "requirements" / "engines").mkdir(parents=True)
    for имя in ДВИЖКИ_В_ФАЙЛАХ:
        (tmp_path / "requirements" / "engines" / f"{имя}.txt").write_text("")
    (tmp_path / "opt" / "venv").mkdir(parents=True)

    корзина = tmp_path / "bin"
    корзина.mkdir()
    # Заглушка ведёт себя как pip в главном: на отсутствующий файл
    # требований выходит с ошибкой. Иначе проверка «неизвестный движок
    # пропускается» проходила бы и без самой проверки в Dockerfile.
    (корзина / "pip").write_text(
        f'#!/bin/sh\n'
        f'echo "$@" >> "{tmp_path}/ставили.txt"\n'
        f'for a in "$@"; do case "$a" in *.txt) [ -f "$a" ] || exit 1 ;; esac; done\n'
        f'exit 0\n')
    (корзина / "pip").chmod(0o755)

    скрипт = _команда_выбора().replace("/opt/venv/", f"{tmp_path}/opt/venv/")
    result = subprocess.run(
        ["sh", "-c", скрипт], cwd=tmp_path, capture_output=True, text=True,
        timeout=30, check=False,
        env={"PATH": f"{корзина}:/usr/bin:/bin", "PROFILE": профиль,
             "ENGINES": движки})
    assert result.returncode == 0, result.stderr
    файл = tmp_path / "ставили.txt"
    слова = файл.read_text().split() if файл.exists() else []
    ставили = [с.split("/")[-1].removesuffix(".txt")
               for с in слова if с.endswith(".txt")]
    return ставили, result.stdout + result.stderr


def test_explicit_engines_replace_the_profile_set(tmp_path: Path):
    """--engines gigaam,vosk — и в образе ровно эти два, а не набор профиля."""
    ставили, _ = _выбрать(tmp_path, профиль="full", движки="gigaam,vosk")
    assert ставили == ["gigaam", "vosk"], ставили


def test_underscored_engine_names_reach_the_requirement_files(tmp_path: Path):
    """Установщик пишет faster_whisper, файл называется faster-whisper.txt."""
    ставили, вывод = _выбрать(tmp_path, движки="faster_whisper,gigaam")
    assert ставили == ["faster-whisper", "gigaam"], ставили
    assert "не удалось" not in вывод, вывод


def test_profile_russian_brings_the_russian_engines(tmp_path: Path):
    ставили, _ = _выбрать(tmp_path, профиль="russian")
    assert set(ставили) == {"gigaam", "tone", "vosk", "vad", "postprocess"}, ставили


def test_profile_apple_is_not_silently_downgraded(tmp_path: Path):
    """Профиля apple в case не было, и он молча превращался в один движок."""
    ставили, _ = _выбрать(tmp_path, профиль="apple")
    assert "gigaam" in ставили, ставили
    assert len(ставили) > 1, ставили


def test_unknown_engine_warns_instead_of_failing_the_build(tmp_path: Path):
    """Опечатка в --engines роняла сборку на середине пятнадцатой минуты."""
    ставили, вывод = _выбрать(tmp_path, движки="gigaam,вымышленный")
    assert ставили == ["gigaam"], ставили
    assert "вымышленный" in вывод and "пропускаем" in вывод, вывод


def test_minimal_profile_installs_no_engines_at_all(tmp_path: Path):
    ставили, _ = _выбрать(tmp_path, профиль="minimal")
    assert ставили == [], ставили
