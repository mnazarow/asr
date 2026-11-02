"""Заход 54: установщик bash — находки ревизии 3.1.20 (участок bash-install).

Всё проверяется прогоном настоящих сценариев с подменёнными командами
(systemctl, pip, python и прочие — заглушки, которые только записывают
вызовы), как и в прежних заходах: test_review_40_scripts, test_review_47_scripts.
Главное здесь — пробный запуск, который больше не удаляет рабочее окружение,
запись в каталог данных службы от имени её владельца (символические ссылки),
каталог agent/ в установке, конфигурация в режиме docker до запуска
контейнера и откат, который действительно возвращает прежнее.
"""
from __future__ import annotations

import os
import pty
import re
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

КОРЕНЬ = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(os.name != "posix" or BASH is None, reason="bash-сценарии")

ВЕРСИЯ = (КОРЕНЬ / "VERSION").read_text(encoding="utf-8").strip()
ROOT = os.name == "posix" and os.geteuid() == 0
нужен_root = pytest.mark.skipif(not ROOT or shutil.which("setpriv") is None,
                                reason="нужны права root и setpriv")


def прогнать(команда: str, *, env: dict[str, str] | None = None, timeout: int = 300,
             cwd: Path | None = None) -> subprocess.CompletedProcess:
    окружение = {**os.environ, "ASRHUB_NO_COLOR": "1", **(env or {})}
    return subprocess.run([BASH, "-c", команда], capture_output=True, text=True,
                          env=окружение, timeout=timeout, check=False,
                          cwd=str(cwd) if cwd else None)


def _исполняемый(путь: Path, текст: str) -> Path:
    путь.parent.mkdir(parents=True, exist_ok=True)
    путь.write_text(текст, encoding="utf-8")
    путь.chmod(0o755)
    return путь


def _стенд(база: Path) -> dict[str, str]:
    """Заглушки системных команд и поддельный Python 3.13 с окружением.

    Поддельный pip понимает ASRHUB_TEST_PIP_FAIL: если строка его вызова
    содержит это значение, он отказывает «навсегда» (No matching
    distribution), без повторов. Содержимое файлов ограничений (-c) он
    записывает в журнал — по нему видно, что закрепление дошло до команды.
    """
    корзина = база / "bin"
    журнал = база / "вызовы.log"
    заглушки = {
        "systemctl": '''case "$*" in
  *"show -p MainPID"*) echo 0 ;;
  *"is-active"*) echo inactive; exit 3 ;;
  *"show"*"NeedDaemonReload"*) echo no ;;
  *"show -p Version"*) echo "Version=255" ;;
esac
exit 0''',
        "journalctl": "exit 0", "pgrep": "exit 1", "apt-get": "exit 0", "apt-cache": "exit 0",
        "dnf": "exit 0", "yum": "exit 0", "brew": "exit 0", "git": "exit 0",
        "useradd": "exit 0", "sudo": 'exec "$@"', "ffmpeg": "exit 0", "ss": "exit 0",
    }
    for имя, тело in заглушки.items():
        _исполняемый(корзина / имя, f'#!/bin/bash\necho "{имя} $*" >> "{журнал}"\n{тело}\n')
    вн_python = _исполняемый(база / "tpl" / "venv-python", f'''#!/bin/bash
echo "venv-python $*" >> "{журнал}"
case "$*" in
  *version_info*) echo "3.13"; exit 0 ;;
  -V|--version) echo "Python 3.13.3"; exit 0 ;;
  "-m pip "*) exit 0 ;;
  "-m asrhub --print-config"*) echo "# пример конфигурации"; exit 0 ;;
esac
exit 1
''')
    вн_pip = _исполняемый(база / "tpl" / "venv-pip", f'''#!/bin/bash
echo "venv-pip $*" >> "{журнал}"
prev=""
for a in "$@"; do
  if [[ "$prev" == "-c" && -f "$a" ]]; then echo "ограничения: $(tr '\\n' ' ' < "$a")" >> "{журнал}"; fi
  prev="$a"
done
case "$1" in
  install)
    if [[ -n "${{ASRHUB_TEST_PIP_FAIL:-}}" && "$*" == *"${{ASRHUB_TEST_PIP_FAIL}}"* ]]; then
      echo "ERROR: No matching distribution found for проба"; exit 1
    fi
    exit 0 ;;
  check) echo "No broken requirements found."; exit 0 ;;
  freeze) echo "fastapi==0.100.0"; exit 0 ;;
  show) exit 1 ;;
esac
exit 0
''')
    python = _исполняемый(корзина / "python3.13", f'''#!/bin/bash
echo "python3.13 $*" >> "{журнал}"
case "$*" in
  *"import ensurepip"*) exit 0 ;;
  *version_info*) echo "3.13"; exit 0 ;;
  -V|--version) echo "Python 3.13.3"; exit 0 ;;
  "-m venv "*)
    d="$3"; mkdir -p "$d/bin"
    cp "{вн_python}" "$d/bin/python"; cp "{вн_pip}" "$d/bin/pip"
    chmod +x "$d/bin/python" "$d/bin/pip"; exit 0 ;;
esac
exit 0
''')
    (база / "home").mkdir(parents=True, exist_ok=True)
    return {"PATH": f"{корзина}:{os.environ['PATH']}", "HOME": str(база / "home"),
            "TMPDIR": str(база), "ASRHUB_SYSTEMD_DIR": str(база / "units"),
            "ASRHUB_SYSTEMD_USER_DIR": str(база / "units-user"),
            "ASRHUB_LAUNCHAGENTS_DIR": str(база / "agents"), "PYTHON_STUB": str(python)}


def _установить(база: Path, *ключи: str, prefix: Path | None = None, data: Path | None = None,
                env: dict[str, str] | None = None, сеть: bool = False) -> subprocess.CompletedProcess:
    окружение = _стенд(база)
    окружение.update(env or {})
    prefix = prefix or база / "prog"
    data = data or база / "data"
    return прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --prefix "{prefix}" '
        f'--data "{data}" --no-service --yes --skip-models --gpu-driver none '
        f'--no-interactive {"" if сеть else "--offline"} --python "{окружение["PYTHON_STUB"]}" '
        + " ".join(ключи), env=окружение, timeout=600)


def _журнал(база: Path) -> str:
    путь = база / "вызовы.log"
    return путь.read_text(encoding="utf-8") if путь.exists() else ""


def _вывод(итог: subprocess.CompletedProcess) -> str:
    return итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# №1. Пробный запуск и откат
# ---------------------------------------------------------------------------


def test_пробный_запуск_ничего_не_откатывает(tmp_path: Path):
    """Прерванный `--dry-run --force` стирал рабочий venv.

    Изменения в пробном запуске только печатаются, а действие отката
    «rm -rf venv && mv venv.before-force venv» регистрировалось по-настоящему:
    любой сбой после шага окружения его выполнял, venv стирался, а отложенной
    копии не было вовсе.
    """
    окружение = tmp_path / "venv"
    (окружение / "bin").mkdir(parents=True)
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      enable_error_handling
      ASRHUB_DRY_RUN=1
      run mv "{окружение}" "{окружение}.before-force"
      add_rollback "rm -rf $(_q "{окружение}") && mv $(_q "{окружение}.before-force") $(_q "{окружение}")"
      false
    ''')
    assert итог.returncode != 0
    assert (окружение / "bin").is_dir(), "пробный запуск выполнил откат и стёр окружение"
    assert "Откат изменений" not in _вывод(итог)


def test_откат_свежей_установки_убирает_каталог_программы(tmp_path: Path):
    """Каталог программы оставался после отката: признак «создан нами»
    считался после замка, который сам создаёт каталог."""
    итог = _установить(tmp_path, env={"ASRHUB_TEST_PIP_FAIL": "base.txt"})
    assert итог.returncode != 0, _вывод(итог)[-2000:]
    assert not (tmp_path / "prog").exists(), \
        f"после отката остался каталог: {list((tmp_path / 'prog').iterdir())}"


def test_откат_установки_поверх_возвращает_прежний_код(tmp_path: Path):
    """Установка поверх при сбое pip возвращала venv, но не server/scripts."""
    первая = _установить(tmp_path)
    assert первая.returncode == 0, _вывод(первая)[-3000:]
    программа = tmp_path / "prog"
    (программа / "server" / "метка-прежнего-кода").write_text("старый", encoding="utf-8")
    (программа / "VERSION").write_text("3.0.0\n", encoding="utf-8")
    повтор = _установить(tmp_path, "--force", env={"ASRHUB_TEST_PIP_FAIL": "base.txt"})
    assert повтор.returncode != 0, _вывод(повтор)[-3000:]
    assert (программа / "server" / "метка-прежнего-кода").exists(), "код не возвращён откатом"
    assert (программа / "VERSION").read_text(encoding="utf-8").strip() == "3.0.0"
    assert (программа / "venv" / "bin" / "python").exists(), "окружение не возвращено"
    assert not list(программа.glob(".*.old")) and not list(программа.glob(".*.new")), \
        "остались промежуточные каталоги"


def test_откат_переживает_апостроф_в_пути(tmp_path: Path):
    """Действия отката в одинарных кавычках ломались на «John's Drive»."""
    база = tmp_path / "John's Drive"
    база.mkdir()
    итог = _установить(база, env={"ASRHUB_TEST_PIP_FAIL": "base.txt"})
    assert итог.returncode != 0
    assert "не удалось выполнить" not in _вывод(итог), _вывод(итог)[-2000:]
    assert not (база / "prog").exists()


def test_сбой_в_подоболочке_не_откатывает_за_родителя(tmp_path: Path):
    """Сбой внутри `< <(…)` запускал откат в дочернем процессе, а основной
    сценарий шёл дальше по уже снесённым каталогам."""
    каталог = tmp_path / "сделано"
    каталог.mkdir()
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      enable_error_handling
      add_rollback "rm -rf $(_q "{каталог}")"
      while read -r строка; do :; done < <(false; echo x)
      clear_rollback
      echo "родитель дошёл до конца"
    ''')
    assert "родитель дошёл до конца" in итог.stdout, _вывод(итог)
    assert каталог.is_dir(), "подоболочка выполнила откат родителя"


# ---------------------------------------------------------------------------
# №2. Запись в каталог данных службы — от имени его владельца
# ---------------------------------------------------------------------------


@нужен_root
def test_root_не_идёт_по_подложенным_ссылкам_в_каталоге_данных():
    """Скомпрометированная служба подкладывала ссылки по предсказуемым именам.

    Каталог данных после первой установки принадлежит пользователю службы.
    Повторный запуск установщика от root писал config.example.yaml по
    ссылке в чужой файл, а с --hf-token копировал прочитанное по ссылке
    config.yaml (например /etc/shadow) в файл службы.
    """
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        первая = _установить(база)
        assert первая.returncode == 0, _вывод(первая)[-3000:]
        данные = база / "data"
        for путь in [данные, *данные.rglob("*")]:
            os.lchown(путь, 65534, 65534)
        чужой = база / "файл-root.txt"
        чужой.write_text("ROOT\n", encoding="utf-8")
        чужой.chmod(0o644)
        секрет = база / "секрет-root"
        секрет.write_text("SHADOW-СОДЕРЖИМОЕ\n", encoding="utf-8")
        секрет.chmod(0o600)
        (данные / "config.example.yaml").unlink(missing_ok=True)
        (данные / "config.example.yaml").symlink_to(чужой)
        (данные / "config.yaml").unlink()
        (данные / "config.yaml").symlink_to(секрет)
        повтор = _установить(база, "--hf-token hf_проба12345678901234567")
        assert повтор.returncode == 0, _вывод(повтор)[-3000:]
        assert чужой.read_text(encoding="utf-8") == "ROOT\n", "root записал по подложенной ссылке"
        for путь in данные.rglob("*"):
            if путь.is_file() and not путь.is_symlink():
                assert "SHADOW" not in путь.read_text(encoding="utf-8", errors="replace"), \
                    f"содержимое чужого файла оказалось в {путь}"
        assert "символическая ссылка" in _вывод(повтор)
    finally:
        shutil.rmtree(база, ignore_errors=True)


# ---------------------------------------------------------------------------
# №3. agent/ в установке
# ---------------------------------------------------------------------------


def test_установка_кладёт_агент_который_отдаёт_сервер(tmp_path: Path):
    """Сервер отдаёт агента АТС из <prefix>/agent — туда его не клал никто."""
    итог = _установить(tmp_path)
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    программа = tmp_path / "prog"
    assert (программа / "agent" / "install.sh").is_file()
    assert (программа / "agent" / "asrhub-agent.py").is_file()
    # Тот же путь, что вычисляет сам маршрут: parents[3] от routes_agent.py.
    маршрут = программа / "server" / "asrhub" / "api" / "routes_agent.py"
    assert (маршрут.resolve().parents[3] / "agent" / "install.sh").is_file()


# ---------------------------------------------------------------------------
# №4. Режим docker: конфигурация до запуска, без путей хоста, своему владельцу
# ---------------------------------------------------------------------------


def _установить_в_контейнере(база: Path, *ключи: str, env: dict[str, str] | None = None):
    журнал = база / "docker.log"
    данные = база / "data"
    корзина = база / "dbin"
    _исполняемый(корзина / "docker", f'''#!/bin/sh
echo "docker $*" >> "{журнал}"
case "$*" in
  *"up -d"*) if [ -f "{данные}/config.yaml" ]; then echo "config при запуске: есть" >> "{журнал}"; \
             else echo "config при запуске: нет" >> "{журнал}"; fi ;;
esac
exit 0
''')
    окружение = {**os.environ, "PATH": f"{корзина}:{os.environ['PATH']}", "ASRHUB_NO_COLOR": "1",
                 **(env or {})}
    итог = subprocess.run(
        [BASH, str(КОРЕНЬ / "scripts" / "install.sh"), "--no-interactive", "--yes",
         "--mode", "docker", "--profile", "light", "--skip-models", "--no-service",
         "--offline", "--prefix", str(база / "app"), "--data", str(данные), *ключи],
        env=окружение, capture_output=True, text=True, timeout=600, check=False)
    return итог, (журнал.read_text(encoding="utf-8") if журнал.exists() else "")


def test_в_режиме_docker_конфигурация_пишется_до_запуска_и_без_путей_хоста(tmp_path: Path):
    итог, вызовы = _установить_в_контейнере(tmp_path, env={"SUDO_UID": "4321", "SUDO_GID": "4321"})
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    assert "config при запуске: есть" in вызовы, вызовы
    конфиг = tmp_path / "data" / "config.yaml"
    текст = конфиг.read_text(encoding="utf-8")
    assert str(tmp_path) not in текст, f"в конфигурации контейнера пути хоста:\n{текст}"
    for ключ in ("models_dir", "temp_dir", "data_dir"):
        assert f"{ключ}:" not in текст
    if ROOT:
        assert конфиг.stat().st_uid == 4321, "файл не отдан пользователю контейнера"
    sys.path.insert(0, str(КОРЕНЬ / "server"))
    from asrhub.config import load
    настройки = load(конфиг, apply_hardware=False, ensure_key=False)
    assert not настройки.problems, настройки.problems


def test_переустановка_в_контейнере_сохраняет_токен_и_свои_строки_env(tmp_path: Path):
    """.env писался заново: переустановка теряла токен и строки, дописанные руками."""
    первая, _ = _установить_в_контейнере(tmp_path, "--hf-token", "hf_проба12345678901234567")
    assert первая.returncode == 0, _вывод(первая)[-2000:]
    env_file = tmp_path / "app" / "docker" / ".env"
    with env_file.open("a", encoding="utf-8") as файл:
        файл.write("ASRHUB_MEMORY_LIMIT=8G\n")
    повтор, _ = _установить_в_контейнере(tmp_path, env={"HF_TOKEN": ""})
    assert повтор.returncode == 0, _вывод(повтор)[-2000:]
    текст = env_file.read_text(encoding="utf-8")
    assert "HF_TOKEN=hf_проба12345678901234567" in текст, текст
    assert "ASRHUB_MEMORY_LIMIT=8G" in текст, текст
    assert текст.count("ASRHUB_PORT=") == 1


# ---------------------------------------------------------------------------
# №5, №8, №18. Каталог программы и ключи
# ---------------------------------------------------------------------------


def test_чужой_непустой_каталог_не_становится_каталогом_программы(tmp_path: Path):
    """`--prefix /opt` молча удалял /opt/docker и /opt/scripts чужих программ."""
    программа = tmp_path / "opt"
    (программа / "docker" / "gitlab").mkdir(parents=True)
    (программа / "docker" / "gitlab" / "docker-compose.yml").write_text("services: {}\n")
    (программа / "scripts").mkdir()
    (программа / "scripts" / "backup.sh").write_text("#!/bin/sh\n")
    (программа / "чужая-программа").mkdir()
    итог = _установить(tmp_path, prefix=программа)
    assert итог.returncode == 2, _вывод(итог)[-2000:]
    assert (программа / "docker" / "gitlab" / "docker-compose.yml").exists()
    assert (программа / "scripts" / "backup.sh").exists()
    assert "не похож на установку" in _вывод(итог)


def test_тильда_раскрывается_а_относительный_путь_отвергается(tmp_path: Path):
    итог = _установить(tmp_path, prefix=Path("~/asrhub-app"), data=Path("~/asrhub-data"))
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    дом = tmp_path / "home"
    assert (дом / "asrhub-app" / "server").is_dir(), "установка не в домашний каталог"
    assert not Path("~").exists()
    текст = (дом / "asrhub-data" / "config.yaml").read_text(encoding="utf-8")
    assert "~" not in текст, текст
    относительный = _установить(tmp_path, prefix=Path("prog-рядом"))
    assert относительный.returncode == 2
    assert "абсолютный путь" in _вывод(относительный)


@pytest.mark.parametrize("ключи,ожидание", [
    ("--mode podman", "Неверный режим"),
    ("--profile bogus", "Неизвестный профиль"),
    ("--alignment bogus", "Неизвестное выравнивание"),
    ("--host 'bad host;x'", "Неверный адрес"),
    ("--user нет-такого-пользователя-54", "нет"),
])
def test_неверные_ключи_отвергаются_сразу(tmp_path: Path, ключи: str, ожидание: str):
    итог = _установить(tmp_path, ключи, "--dry-run")
    assert итог.returncode == 2, _вывод(итог)[-1500:]
    assert ожидание in _вывод(итог)


def test_порт_с_ведущим_нулём_и_токен_без_значения(tmp_path: Path):
    итог = _установить(tmp_path, "--port 08080", "--dry-run")
    assert итог.returncode == 0, _вывод(итог)[-1500:]
    assert "http://0.0.0.0:8080" in итог.stdout
    без_значения = _установить(tmp_path, "--hf-token")
    assert без_значения.returncode != 0
    assert "unbound variable" not in _вывод(без_значения)
    assert "нужен токен" in _вывод(без_значения)


def test_переменные_вызывающего_не_просачиваются(tmp_path: Path):
    """VENV_OLD=<каталог> из окружения CI уходил в `rm -rf` в конце установки."""
    чужой = tmp_path / "каталог-ci"
    чужой.mkdir()
    итог = _установить(tmp_path, env={"VENV_OLD": str(чужой), "SERVICE_NAME": "ci-job-name"})
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    assert чужой.is_dir(), "установщик удалил каталог из переменной окружения"


def test_без_вопросов_значит_и_без_последнего_вопроса(tmp_path: Path):
    """`--no-interactive` на терминале спрашивал «Начать установку? [Y/n]»."""
    окружение = _стенд(tmp_path)
    команда = [BASH, str(КОРЕНЬ / "scripts" / "install.sh"), "--prefix", str(tmp_path / "prog"),
               "--data", str(tmp_path / "data"), "--no-service", "--skip-models",
               "--gpu-driver", "none", "--no-interactive", "--offline", "--dry-run",
               "--python", окружение["PYTHON_STUB"]]
    вывод, код = _pty(команда, {**os.environ, **окружение, "ASRHUB_NO_COLOR": "1"}, ответов=0)
    assert код == 0, вывод[-2000:]
    assert "[Y/n]" not in вывод and "[y/N]" not in вывод, вывод[-2000:]


def test_справка_называет_все_ключи():
    итог = прогнать(f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --help')
    for ключ in ("--name", "--hf-token", "--hf-token-file", "--no-stream",
                 "--no-python-install", "--no-gpu-driver", "--wheels"):
        assert ключ in итог.stdout, f"в справке нет {ключ}"


# ---------------------------------------------------------------------------
# №6. Сеть: ни --no-index без спроса, ни отмены зеркала
# ---------------------------------------------------------------------------


def test_без_curl_и_wget_индекс_не_отключается(tmp_path: Path):
    """Без curl и wget проверка сети отвечала «нет», и включался --no-index —
    а он отменяет и PIP_INDEX_URL внутреннего зеркала."""
    итог = _установить(tmp_path, сеть=True,
                       env={"PIP_INDEX_URL": "https://nexus.внутри.example/simple"})
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    assert "--no-index" not in _журнал(tmp_path), "pip запущен без индекса"
    assert "автономном режиме" not in _вывод(итог)


def test_каталог_колёс_доходит_до_pip(tmp_path: Path):
    колёса = tmp_path / "колёса"
    колёса.mkdir()
    итог = _установить(tmp_path, f'--wheels "{колёса}"')
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    assert f"--find-links {колёса}" in _журнал(tmp_path)


# ---------------------------------------------------------------------------
# №9. umask
# ---------------------------------------------------------------------------


def test_ужесточённая_umask_не_закрывает_окружение_от_службы(tmp_path: Path):
    окружение = _стенд(tmp_path)
    итог = прогнать(
        f'umask 027; bash "{КОРЕНЬ / "scripts" / "install.sh"}" --prefix "{tmp_path / "prog"}" '
        f'--data "{tmp_path / "data"}" --no-service --yes --skip-models --gpu-driver none '
        f'--no-interactive --offline --python "{окружение["PYTHON_STUB"]}"',
        env=окружение, timeout=600)
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    for путь in (tmp_path / "prog" / "venv", tmp_path / "prog" / "venv" / "bin" / "python",
                 tmp_path / "prog" / "server"):
        права = stat.S_IMODE(путь.stat().st_mode)
        assert права & 0o005 == 0o005, f"{путь}: {oct(права)} — службе не прочитать"
    assert stat.S_IMODE((tmp_path / "data" / "config.yaml").stat().st_mode) == 0o640


# ---------------------------------------------------------------------------
# №10, №14. Мастер
# ---------------------------------------------------------------------------


def _pty(команда: list[str], env: dict[str, str], *, ответов: int = 20,
         стоп: str = "", срок: float = 240) -> tuple[str, int]:
    """Запуск на псевдотерминале: Enter на каждое приглашение."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(команда[0], команда, env)
    буфер = b""
    послано = 0
    тишина = time.time()
    конец = time.time() + срок
    приглашение = re.compile(rb"(\]: |\[Y/n\] |\[y/N\] )$")
    while time.time() < конец:
        готов, _, _ = select.select([fd], [], [], 0.3)
        if готов:
            try:
                данные = os.read(fd, 65536)
            except OSError:
                break
            if not данные:
                break
            буфер += данные
            тишина = time.time()
            if стоп and стоп.encode() in буфер:
                # SIGKILL: bash внутри `read` откладывает ловушку TERM до
                # конца чтения, и waitpid ждал бы вечно.
                os.kill(pid, 9)
                break
            continue
        хвост = re.sub(rb"\x1b\[[0-9;]*m", b"", буфер[-200:])
        if приглашение.search(хвост) and time.time() - тишина > 0.25:
            if послано >= ответов:
                os.kill(pid, 9)
                break
            os.write(fd, b"\n")
            послано += 1
            буфер += b"\n"
    else:
        os.kill(pid, 9)
    try:
        _, состояние = os.waitpid(pid, 0)
        код = os.waitstatus_to_exitcode(состояние)
    except ChildProcessError:
        код = 0
    finally:
        os.close(fd)
    return буфер.decode("utf-8", "replace"), код


def test_мастер_видит_место_у_существующего_предка(tmp_path: Path):
    """Каталога данных на свежей машине нет — мастер видел «0 ГБ свободно».

    Enter на «Продолжить всё равно? [y/N]» выходил без установки.
    """
    окружение = _стенд(tmp_path)
    команда = [BASH, str(КОРЕНЬ / "scripts" / "install.sh"), "--interactive", "--dry-run",
               "--profile", "light", "--gpu-driver", "none", "--offline",
               "--prefix", str(tmp_path / "нет" / "ещё" / "prog"),
               "--data", str(tmp_path / "нет" / "ещё" / "data"),
               "--python", окружение["PYTHON_STUB"]]
    вывод, _ = _pty(команда, {**os.environ, **окружение, "ASRHUB_NO_COLOR": "1"},
                    стоп="Каталог программы")
    assert "Каталог программы" in вывод, вывод[-2500:]
    assert " 0 ГБ свободно" not in вывод, вывод[-2500:]


def test_порт_своего_сервера_принимается_мастером(tmp_path: Path):
    """Проверка ввода не знала port_is_ours: Enter на предложенном порту
    своего же сервера давал «Порт уже занят. Всё равно?» по кругу."""
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "wizard.sh"}"
      check_port_free() {{ return 1; }}
      port_is_ours() {{ return 0; }}
      wizard_valid_port 18431 && echo ПРИНЯТ || echo ОТКАЗ
    ''')
    assert "ПРИНЯТ" in итог.stdout, _вывод(итог)


# ---------------------------------------------------------------------------
# №11. YAML в кавычках
# ---------------------------------------------------------------------------


def test_адрес_и_путь_с_особыми_знаками_читаются_сервером(tmp_path: Path):
    данные = tmp_path / "asr hub #1" / "data"
    итог = _установить(tmp_path, "--host ::", "--port 08443", data=данные)
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    sys.path.insert(0, str(КОРЕНЬ / "server"))
    from asrhub.config import _load_structured
    сырое = _load_structured(данные / "config.yaml")
    assert сырое["data_dir"] == str(данные)
    assert сырое["server"]["server_host"] == "::"
    assert сырое["server"]["server_port"] == 8443


# ---------------------------------------------------------------------------
# №12. Точка входа контейнера
# ---------------------------------------------------------------------------


def _точка_входа(tmp_path: Path, данные: Path) -> subprocess.CompletedProcess:
    корзина = tmp_path / "ebin"
    # gosu — понижение прав тем же setpriv, что и у настоящего.
    _исполняемый(корзина / "gosu", '#!/bin/bash\nug="$1"; shift\n'
                 'exec setpriv --reuid="${ug%%:*}" --regid="${ug##*:}" --clear-groups "$@"\n')
    _исполняемый(корзина / "python", "#!/bin/sh\ncat >/dev/null\necho 'Ускоритель: CPU'\n")
    return subprocess.run(
        [BASH, str(КОРЕНЬ / "docker" / "entrypoint.sh"), "true"],
        env={**os.environ, "PATH": f"{корзина}:{os.environ['PATH']}",
             "ASRHUB_DATA_DIR": str(данные), "ASRHUB_UID": "4321", "ASRHUB_GID": "4321",
             "HOME": "/root"},
        capture_output=True, text=True, timeout=60, check=False)


@нужен_root
def test_точка_входа_исправляет_владельца_каждого_подкаталога():
    """Том с верным владельцем, но без подкаталога — exit 13 на каждом старте."""
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        данные = база / "data"
        (данные / "models").mkdir(parents=True)
        for путь in (данные, данные / "models"):
            os.chown(путь, 4321, 4321)
        # config.yaml, записанный установщиком от root.
        (данные / "config.yaml").write_text("server: {}\n", encoding="utf-8")
        (данные / "config.yaml").chmod(0o640)
        итог = _точка_входа(база, данные)
        assert итог.returncode == 0, итог.stdout + итог.stderr
        for имя in ("uploads", "results", "logs", "tmp", "config.yaml"):
            assert (данные / имя).stat().st_uid == 4321, f"{имя} остался чужим"
    finally:
        shutil.rmtree(база, ignore_errors=True)


@нужен_root
def test_точка_входа_заводит_конфигурацию_на_свежем_томе():
    """Без config.yaml сохранённое в интерфейсе не переживало перезапуск."""
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        данные = база / "data"
        итог = _точка_входа(база, данные)
        assert итог.returncode == 0, итог.stdout + итог.stderr
        конфиг = данные / "config.yaml"
        assert конфиг.is_file(), "файл конфигурации не создан"
        assert конфиг.stat().st_uid == 4321, "сервер не сможет в него писать"
        assert (данные / "cache" / "home").is_dir(), "HOME не на томе"
    finally:
        shutil.rmtree(база, ignore_errors=True)


# ---------------------------------------------------------------------------
# №13. CTranslate2 под CUDA 11 — ограничением в той же команде
# ---------------------------------------------------------------------------


def _nvidia_smi(корзина: Path, cuda: str) -> None:
    _исполняемый(корзина / "nvidia-smi", f'''#!/bin/bash
case "$*" in
  -L) echo "GPU 0: NVIDIA Проба (UUID: GPU-1)"; exit 0 ;;
  *query-gpu=memory*) echo 24576 ;;
  *query-gpu=name*) echo "NVIDIA Проба" ;;
  *query-gpu=driver*) echo 470.256.02 ;;
  "") echo "| NVIDIA-SMI 470.256.02   Driver Version: 470.256.02   CUDA Version: {cuda} |" ;;
esac
exit 0
''')


def test_закрепление_ctranslate2_под_cuda_11_доходит_до_установки_движка(tmp_path: Path):
    _стенд(tmp_path)
    _nvidia_smi(tmp_path / "bin", "11.4")
    итог = _установить(tmp_path, "--engines faster_whisper")
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    вызовы = _журнал(tmp_path)
    строка = next((с for с in вызовы.splitlines()
                   if с.startswith("venv-pip install") and "faster-whisper.txt" in с), "")
    assert " -c " in строка, f"ограничение не в той же команде: {строка}"
    assert "ограничения: ctranslate2==3.24.0 faster-whisper<1.0" in вызовы, вызовы[-3000:]
    assert not re.search(r"^venv-pip install .*ctranslate2==", вызовы, re.M), \
        "версия ставится отдельной командой, которую отменит файл требований"
    требования = (КОРЕНЬ / "requirements" / "engines" / "faster-whisper.txt").read_text()
    assert not re.search(r"^ctranslate2", требования, re.M), "файл требований отменяет закрепление"


# ---------------------------------------------------------------------------
# №15. Пробный запуск не пишет на диск
# ---------------------------------------------------------------------------


def test_пробная_сборка_whisper_cpp_не_трогает_env_sh(tmp_path: Path):
    программа = tmp_path / "prog"
    исходники = программа / "whisper.cpp"
    (исходники / ".git").mkdir(parents=True)
    _исполняемый(исходники / "build" / "bin" / "whisper-cli", "#!/bin/sh\n")
    данные = tmp_path / "data"
    данные.mkdir()
    env_sh = данные / "env.sh"
    env_sh.write_text("A=1\n", encoding="utf-8")
    env_sh.chmod(0o600)
    окружение = _стенд(tmp_path)
    _исполняемый(tmp_path / "bin" / "cmake", "#!/bin/sh\nexit 0\n")
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "detect.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "whispercpp.sh"}"
      PREFIX="{программа}" DATA_DIR="{данные}" ASRHUB_DRY_RUN=1
      build_whisper_cpp
    ''', env=окружение)
    assert итог.returncode == 0, _вывод(итог)
    assert env_sh.read_text(encoding="utf-8") == "A=1\n"
    assert stat.S_IMODE(env_sh.stat().st_mode) == 0o600


@нужен_root
def test_пробная_установка_службы_не_от_root_не_создаёт_каталогов(tmp_path: Path):
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        дом = база / "home"
        дом.mkdir()
        os.chown(дом, 65534, 65534)
        итог = subprocess.run(
            ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", BASH,
             str(КОРЕНЬ / "scripts" / "service.sh"), "install", "--dry-run",
             "--prefix", str(база / "prog"), "--data", str(база / "data")],
            env={**os.environ, "HOME": str(дом), "TMPDIR": str(дом)},
            capture_output=True, text=True, timeout=60, check=False)
        assert "[Service]" in итог.stdout, итог.stdout + итог.stderr
        assert not (дом / ".config").exists(), "пробный запуск создал ~/.config/systemd/user"
    finally:
        shutil.rmtree(база, ignore_errors=True)


# ---------------------------------------------------------------------------
# №16. Журнал — новым файлом со случайным именем
# ---------------------------------------------------------------------------


def test_журнал_не_открывает_заранее_подложенный_файл(tmp_path: Path):
    журналы = tmp_path / "журналы"
    журналы.mkdir()
    # Предсказуемые имена на ближайшие секунды — как это сделал бы сосед.
    подложенные = []
    for сдвиг in range(0, 5):
        метка = time.strftime("%Y%m%d-%H%M%S", time.localtime(time.time() + сдвиг))
        путь = журналы / f"asrhub-bash-{метка}.log"
        путь.write_text("", encoding="utf-8")
        подложенные.append(путь)
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      setup_logging "{журналы}"
      printf '%s' "$ASRHUB_LOG_FILE"
    ''')
    журнал = Path(итог.stdout.strip())
    assert журнал.is_file(), _вывод(итог)
    assert журнал not in подложенные
    assert stat.S_IMODE(журнал.stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# №19. Групповые права общего каталога данных
# ---------------------------------------------------------------------------


def test_повторная_установка_не_снимает_групповую_запись(tmp_path: Path):
    первая = _установить(tmp_path)
    assert первая.returncode == 0, _вывод(первая)[-2000:]
    данные = tmp_path / "data"
    данные.chmod(0o2770)
    (данные / "results").chmod(0o770)
    повтор = _установить(tmp_path, "--force")
    assert повтор.returncode == 0, _вывод(повтор)[-2000:]
    assert stat.S_IMODE(данные.stat().st_mode) == 0o2770
    assert stat.S_IMODE((данные / "results").stat().st_mode) == 0o770


def test_существующему_каталогу_снимаются_права_прочих(tmp_path: Path):
    каталог = tmp_path / "данные"
    каталог.mkdir()
    каталог.chmod(0o755)
    прогнать(f'source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"; ensure_dir "{каталог}" 0750')
    assert stat.S_IMODE(каталог.stat().st_mode) == 0o750


# ---------------------------------------------------------------------------
# №20. Системные пакеты
# ---------------------------------------------------------------------------


def test_пакеты_ставятся_по_одному_если_одно_имя_неизвестно(tmp_path: Path):
    журнал = tmp_path / "apt.log"
    корзина = tmp_path / "bin"
    _исполняемый(корзина / "apt-get", f'''#!/bin/bash
echo "apt-get FRONTEND=${{DEBIAN_FRONTEND:-}} $*" >> "{журнал}"
[[ "$*" == *ffmpeg* && "$*" == *git* ]] && exit 100
[[ "$*" == *ffmpeg* ]] && exit 100
exit 0
''')
    _исполняемый(корзина / "sudo", '#!/bin/bash\nexec "$@"\n')
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "detect.sh"}"
      detect_package_manager() {{ printf apt-get; }}
      install_system_packages ffmpeg git cmake || echo "ЧАСТЬ НЕ ВСТАЛА"
    ''', env={"PATH": f"{корзина}:{os.environ['PATH']}"})
    вызовы = журнал.read_text(encoding="utf-8")
    assert "install -y -qq git" in вызовы and "install -y -qq cmake" in вызовы, вызовы
    assert "FRONTEND=noninteractive" in вызовы
    assert "ЧАСТЬ НЕ ВСТАЛА" in итог.stdout and "ffmpeg" in итог.stderr


def test_имя_пакета_сборки_для_zypper():
    итог = прогнать(f'''
      source "{КОРЕНЬ / "scripts" / "lib" / "common.sh"}"
      source "{КОРЕНЬ / "scripts" / "lib" / "detect.sh"}"
      detect_package_manager() {{ printf zypper; }}
      printf '[%s]' "$(system_package_names build)"
    ''')
    assert "[gcc gcc-c++ make]" in итог.stdout, итог.stdout


# ---------------------------------------------------------------------------
# Недоделки: --engines без --models, PyTorch для whisper_cpp
# ---------------------------------------------------------------------------


def test_модели_профиля_только_для_выбранных_движков(tmp_path: Path):
    """`--engines vosk` качал faster-whisper-small и делал его моделью по умолчанию."""
    окружение = _стенд(tmp_path)
    итог = прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --prefix "{tmp_path / "prog"}" '
        f'--data "{tmp_path / "data"}" --no-service --yes --gpu-driver none --no-interactive '
        f'--offline --dry-run --profile light --engines vosk --python "{окружение["PYTHON_STUB"]}"',
        env=окружение)
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    assert "faster-whisper-small" not in итог.stdout


def test_whisper_cpp_не_тянет_pytorch(tmp_path: Path):
    окружение = _стенд(tmp_path)
    итог = прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --prefix "{tmp_path / "prog"}" '
        f'--data "{tmp_path / "data"}" --no-service --yes --skip-models --gpu-driver none '
        f'--no-interactive --offline --dry-run --engines whisper_cpp '
        f'--python "{окружение["PYTHON_STUB"]}"', env=окружение)
    assert итог.returncode == 0, _вывод(итог)[-2000:]
    assert "Установка PyTorch" not in итог.stdout
