"""Заход 35: установка, обновление и удаление.

Общее у этих находок — вторая установка и повторный запуск. Всё написанное
для одного сервера на одной машине работает; стоит появиться второй
установке рядом — и обслуживание одной тихо ломает другую. Проверки гоняют
настоящий bash на макетах в tmp_path: ничего в систему не ставится.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="нужен bash")


def прогнать(script: str, cwd: Path | None = None, env: dict | None = None):
    полное = {**os.environ, "ASRHUB_QUIET": "0", **(env or {})}
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          cwd=str(cwd) if cwd else None, env=полное, timeout=180)


# ---------------------------------------------------------------------------
# Имя службы
# ---------------------------------------------------------------------------

def test_имя_службы_определяется_по_каталогу_установки(repo_root: Path,
                                                       tmp_path: Path):
    """Вторую установку заводят ключом `--name`, и служба у неё своя.

    Обновление и удаление про этот ключ не знали вовсе и всегда работали со
    службой «asrhub»: обслуживание второй установки гасило и сносило службу
    ПЕРВОЙ, продолжая бодро сообщать об успехе. Заметить это можно было
    только по остановившемуся распознаванию на другом сервере.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    юниты = tmp_path / "etc"
    юниты.mkdir()
    (юниты / "asrhub.service").write_text(
        "[Service]\nExecStart=/opt/asrhub/venv/bin/python -m asrhub\n", encoding="utf-8")
    (юниты / "asrhub2.service").write_text(
        "[Service]\nWorkingDirectory=/opt/asrhub2\n"
        "ExecStart=/opt/asrhub2/venv/bin/python -m asrhub\n", encoding="utf-8")

    # Подменяем каталог юнитов: функция ходит по /etc/systemd/system.
    исходник = common.read_text(encoding="utf-8")
    подмена = tmp_path / "common.sh"
    подмена.write_text(исходник.replace("/etc/systemd/system/asrhub*.service",
                                        f"{юниты}/asrhub*.service"), encoding="utf-8")
    итог = прогнать(f'''
      source "{подмена}"
      printf 'первая=%s\\n' "$(service_name_for /opt/asrhub)"
      printf 'вторая=%s\\n' "$(service_name_for /opt/asrhub2)"
      printf 'чужая=%s\\n' "$(service_name_for /opt/другое)"
    ''')
    assert "первая=asrhub" in итог.stdout, итог.stdout + итог.stderr
    assert "вторая=asrhub2" in итог.stdout, итог.stdout + итог.stderr
    assert "чужая=asrhub" in итог.stdout, "без юнита имя должно остаться прежним"


def test_обновление_и_удаление_передают_имя_службы(repo_root: Path):
    """Ключ есть, а вызовы идут без него — это то же самое, что ключа нет."""
    обновление = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    удаление = (repo_root / "scripts" / "uninstall.sh").read_text(encoding="utf-8")

    for имя, текст in (("update.sh", обновление), ("uninstall.sh", удаление)):
        assert "--name)" in текст, f"{имя}: нет ключа --name"
        assert "service_name_for" in текст, f"{имя}: имя службы не определяется"
    # Каждый вызов service.sh — с именем.
    for строка in обновление.splitlines() + удаление.splitlines():
        if "service.sh" not in строка or "scripts/service.sh logs" in строка:
            continue
        if "${SCRIPT_DIR}/service.sh" not in строка:
            continue
        assert "--name" in строка or строка.rstrip().endswith("\\"), строка


# ---------------------------------------------------------------------------
# Снимок и откат
# ---------------------------------------------------------------------------

def test_снимок_свой_у_каждой_установки(repo_root: Path):
    """Прежний путь совпадал у всех установок под одним родителем.

    `/opt/asrhub` и `/opt/asrhub2` писали снимок в один и тот же
    `/opt/asrhub-snapshot`: откат второй возвращал код и config.yaml первой
    — то есть чинил одно и ломал другое, молча и до неузнаваемости.
    """
    текст = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert 'SNAPSHOT_DIR="${PREFIX%/}.snapshot"' in текст, \
        "снимок снова лежит по общему пути"
    # Прежний путь остаётся известен — ровно для одного отката после
    # обновления на эту версию.
    assert "SNAPSHOT_LEGACY" in текст


def test_снимки_двух_установок_не_совпадают(repo_root: Path, tmp_path: Path):
    """Та же проверка, но счётом путей, а не чтением исходника."""
    итог = прогнать(f'''
      набор() {{ printf '%s\\n' "${{1%/}}.snapshot"; }}
      набор "{tmp_path}/opt/asrhub"
      набор "{tmp_path}/opt/asrhub2"
    ''')
    пути = [с for с in итог.stdout.splitlines() if с.strip()]
    assert len(set(пути)) == 2, пути


def test_откат_после_неудачного_обновления_несёт_каталог_данных(repo_root: Path):
    """`--rollback` без `--data` искал config.yaml в каталоге по умолчанию.

    На машине, где каталог данных задан своим ключом (а он задан у любой
    второй установки), откат возвращал конфигурацию ЧУЖОЙ установки — и
    службу дёргал тоже не ту.
    """
    текст = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    начало = текст.index("--rollback --prefix")
    кусок = текст[начало:начало + 200]
    assert "--data" in кусок, кусок
    assert "--name" in кусок, кусок


# ---------------------------------------------------------------------------
# Повторная установка
# ---------------------------------------------------------------------------

def test_занятый_своим_же_сервером_порт_не_меняется(repo_root: Path,
                                                    tmp_path: Path):
    """Установщик запускают повторно при работающем сервере — это обычное дело.

    Прежде это считалось поводом переехать на соседний порт, а с ключом
    `--yes` переезд происходил вообще молча: служба поднималась на 8081,
    config.yaml оставался с 8080, и сервер после перезапуска отвечал не там,
    где его ищут все настроенные клиенты и агенты на станциях.
    """
    текст = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    начало = текст.index('if ! check_port_free "${PORT}"; then')
    кусок = текст[начало:начало + 1400]
    # С захода 40 опрос порта живёт в common.sh (port_is_ours): им же
    # пользуется мастер, который прежде предлагал соседний порт вместо своего.
    библиотека = (repo_root / "scripts" / "lib" / "common.sh").read_text(encoding="utf-8")
    опрос = библиотека[библиотека.index("port_is_ours() {"):]
    опрос = опрос[:опрос.index("\n}\n")]
    assert "port_is_ours" in кусок and "http_probe" in опрос, \
        "занятый порт не опрашивается — чей он, неизвестно"
    assert "PORT_IS_OURS" in кусок
    # Переезд на другой порт остаётся, но только для ЧУЖОГО процесса.
    assert "find_free_port" in кусок
    порядок = кусок.index("PORT_IS_OURS=1") < кусок.index("find_free_port")
    assert порядок, "проверка «это мы» должна идти до выбора нового порта"


def test_порт_нашего_же_сервера_остаётся_прежним(repo_root: Path, tmp_path: Path):
    """Проверка на настоящем коде: блок из install.sh поверх живого сокета.

    На порту поднимается заглушка, отвечающая как ASR Hub. Блок обязан
    узнать своего и оставить порт, а не искать свободный.
    """
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Ответчик(BaseHTTPRequestHandler):
        def do_GET(self):                                    # noqa: N802
            тело = b'{"status":"ok","service":"asrhub"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

        def log_message(self, *_а):                          # тишина в выводе
            return

    with socket.socket() as гнездо:
        гнездо.bind(("127.0.0.1", 0))
        порт = гнездо.getsockname()[1]
    сервер = HTTPServer(("127.0.0.1", порт), Ответчик)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        блок = _блок_проверки_порта(repo_root)
        каталог = tmp_path / "prefix"
        каталог.mkdir()
        итог = прогнать(f'''
          source "{repo_root}/scripts/lib/common.sh"
          PORT={порт}
          PREFIX="{каталог}"
          {блок}
          printf 'ИТОГОВЫЙ_ПОРТ=%s\\n' "${{PORT}}"
        ''', env={"ASRHUB_ASSUME_YES": "1"})
    finally:
        сервер.shutdown()
        поток.join(timeout=5)
    assert f"ИТОГОВЫЙ_ПОРТ={порт}" in итог.stdout, итог.stdout + итог.stderr
    assert "занят уже установленным" in итог.stdout + итог.stderr, \
        итог.stdout + итог.stderr


def test_порт_чужого_процесса_по_прежнему_меняется(repo_root: Path, tmp_path: Path):
    """Проверка парная: осторожность не должна отменять полезное поведение."""
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Чужой(BaseHTTPRequestHandler):
        """Не наш сервер: отвечает, но своё — как nginx на том же порту."""

        def do_GET(self):                                    # noqa: N802
            тело = b"<html><head><title>Welcome to nginx</title></head></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(тело)))
            self.end_headers()
            self.wfile.write(тело)

        def log_message(self, *_а):
            return

    with socket.socket() as пробное:
        пробное.bind(("127.0.0.1", 0))
        порт = пробное.getsockname()[1]
    сервер = HTTPServer(("127.0.0.1", порт), Чужой)
    поток = threading.Thread(target=сервер.serve_forever, daemon=True)
    поток.start()
    try:
        блок = _блок_проверки_порта(repo_root)
        каталог = tmp_path / "prefix"
        каталог.mkdir()
        итог = прогнать(f'''
          source "{repo_root}/scripts/lib/common.sh"
          PORT={порт}
          PREFIX="{каталог}"
          {блок}
          printf 'ИТОГОВЫЙ_ПОРТ=%s\\n' "${{PORT}}"
        ''', env={"ASRHUB_ASSUME_YES": "1"})
    finally:
        сервер.shutdown()
        поток.join(timeout=5)
    assert f"ИТОГОВЫЙ_ПОРТ={порт}" not in итог.stdout, \
        "чужой процесс на порту — повод переехать, а не остаться"
    assert "занят" in итог.stdout + итог.stderr


def _блок_проверки_порта(repo_root: Path) -> str:
    """Тот самый кусок install.sh — целиком, как он поставляется."""
    строки = (repo_root / "scripts" / "install.sh").read_text(
        encoding="utf-8").splitlines()
    начало = next(и for и, с in enumerate(строки)
                  if с.startswith('if ! check_port_free "${PORT}"; then'))
    конец = next(и for и, с in enumerate(строки[начало + 1:], начало + 1)
                 if с == "fi")
    return "\n".join(строки[начало:конец + 1])


# ---------------------------------------------------------------------------
# Установщик для Windows
# ---------------------------------------------------------------------------

def test_установщик_windows_знает_про_спутники_требований(repo_root: Path):
    """Сам пакет GigaAM лежит ТОЛЬКО в `no-deps/gigaam.txt`.

    Установщик PowerShell ставил один обычный файл требований и не знал ни
    про `no-deps`, ни про `optional`, ни про `overrides.txt`. Для GigaAM это
    означало, что движок на Windows не ставился НИКОГДА — а скрипт при этом
    печатал «gigaam установлен»: человек получал сервер без движка и
    сообщение об успехе.
    """
    модуль = (repo_root / "scripts" / "lib" / "Common.psm1").read_text(encoding="utf-8")
    установщик = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")

    assert "function Install-EngineRequirements" in модуль
    assert "function Install-Overrides" in модуль
    for часть in ("no-deps", "optional", "overrides.txt"):
        assert часть in модуль, f"Common.psm1 не знает про «{часть}»"
    assert "Install-EngineRequirements" in установщик, \
        "install.ps1 ставит требования мимо общей функции"
    assert "Install-Overrides" in установщик

    # И сам файл-спутник существует: без него проверка ничего не значит.
    assert (repo_root / "requirements" / "engines" / "no-deps" / "gigaam.txt").is_file()


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="нужен pwsh")
def test_powershell_ставит_все_спутники_требований(repo_root: Path, tmp_path: Path):
    """Проверка настоящим PowerShell: что именно уходит в pip.

    Подменяем pip болванкой, которая записывает свои доводы в файл, и
    смотрим, дошли ли до неё оба спутника. Без этого проверка сводится к
    «в файле есть слово no-deps», а слово есть и в комментарии.
    """
    треб = tmp_path / "requirements" / "engines"
    (треб / "no-deps").mkdir(parents=True)
    (треб / "optional").mkdir(parents=True)
    (треб / "gigaam.txt").write_text("torch\n", encoding="utf-8")
    (треб / "no-deps" / "gigaam.txt").write_text("gigaam @ git+https://x\n",
                                                 encoding="utf-8")
    (треб / "optional" / "gigaam.txt").write_text("onnx\n", encoding="utf-8")
    (tmp_path / "requirements" / "overrides.txt").write_text("numpy==2.1\n",
                                                             encoding="utf-8")

    журнал = tmp_path / "pip.log"
    поддельный = tmp_path / ("pip.cmd" if os.name == "nt" else "pip.sh")
    поддельный.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{журнал}"\n',
                          encoding="utf-8")
    поддельный.chmod(0o755)

    итог = subprocess.run(
        ["pwsh", "-NoProfile", "-Command",
         f"Import-Module '{repo_root / 'scripts' / 'lib' / 'Common.psm1'}' -Force; "
         f"Install-EngineRequirements -Pip '{поддельный}' "
         f"-Requirements '{треб / 'gigaam.txt'}'; "
         f"Install-Overrides -Pip '{поддельный}' "
         f"-RequirementsDir '{tmp_path / 'requirements'}'"],
        capture_output=True, text=True, timeout=180)
    assert итог.returncode == 0, итог.stdout + итог.stderr
    записано = журнал.read_text(encoding="utf-8") if журнал.exists() else ""
    строки = [с for с in записано.splitlines() if с.strip()]
    путь_nodeps = str(треб / "no-deps" / "gigaam.txt")
    путь_optional = str(треб / "optional" / "gigaam.txt")

    свои = [с for с in строки if путь_nodeps in с]
    assert свои, f"спутник no-deps не поставлен:\n{записано}"
    # И именно с --no-deps: без ключа он и обрывался с ResolutionImpossible.
    assert "--no-deps" in свои[0], свои[0]

    assert any(путь_optional in с for с in строки), \
        f"необязательная часть не поставлена:\n{записано}"
    assert any("overrides.txt" in с and "--no-deps" in с for с in строки), \
        f"версии не возвращены:\n{записано}"


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="нужен pwsh")
def test_скрипты_powershell_разбираются(repo_root: Path):
    """Синтаксис проверяется настоящим разборщиком PowerShell."""
    файлы = [repo_root / "scripts" / "install.ps1",
             repo_root / "scripts" / "lib" / "Common.psm1"]
    for файл in файлы:
        итог = subprocess.run(
            ["pwsh", "-NoProfile", "-Command",
             f"$e=$null; [System.Management.Automation.Language.Parser]::ParseFile("
             f"'{файл}', [ref]$null, [ref]$e) | Out-Null; "
             f"if ($e) {{ $e | ForEach-Object {{ Write-Host $_ }}; exit 1 }}"],
            capture_output=True, text=True, timeout=180)
        assert итог.returncode == 0, f"{файл.name}: {итог.stdout}{итог.stderr}"
