"""Заход 54: обслуживание на bash (участок bash-ops) и клиент asrctl.

Сценарии прогоняются по-настоящему, системные команды подменены
заглушками (systemctl, launchctl, cp, nvidia-smi), сервер для клиента —
маленький HTTP-сервер в этом же процессе. Установка для обновления и
удаления — настоящий install.sh со стендом из test_review_54_install.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_review_54_install import (
    BASH,
    КОРЕНЬ,
    _вывод,
    _исполняемый,
    _стенд,
    _установить,
    нужен_root,
    прогнать,
)

pytestmark = pytest.mark.skipif(os.name != "posix" or BASH is None, reason="bash-сценарии")


def _свободный_порт() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Поддельный сервер для asrctl
# ---------------------------------------------------------------------------


def _json(данные, код: int = 200) -> tuple[int, dict, bytes]:
    # Без пробелов, как отвечает FastAPI: разбор без python рассчитан на него.
    return код, {"Content-Type": "application/json"}, json.dumps(
        данные, ensure_ascii=False, separators=(",", ":")).encode()


class _Сервер:
    """HTTP-сервер с ответами по (метод, путь); запросы записываются."""

    def __init__(self, маршруты: dict, адрес: str = "127.0.0.1"):
        self.запросы: list[tuple[str, str]] = []
        сервер = self

        class Обработчик(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _ответ(self):
                длина = int(self.headers.get("Content-Length") or 0)
                if длина:
                    self.rfile.read(длина)
                путь = self.path.split("?")[0]
                сервер.запросы.append((self.command, self.path))
                ответ = маршруты.get((self.command, путь))
                if callable(ответ):
                    ответ = ответ()
                if ответ is None:
                    ответ = _json({"message": "нет такого"}, 404)
                код, заголовки, тело = ответ
                self.send_response(код)
                for ключ, значение in заголовки.items():
                    self.send_header(ключ, значение)
                self.send_header("Content-Length", str(len(тело)))
                self.end_headers()
                self.wfile.write(тело)

            do_GET = do_POST = _ответ

        self.httpd = ThreadingHTTPServer((адрес, 0), Обработчик)
        self.порт = self.httpd.server_address[1]
        self.адрес = f"http://{адрес}:{self.порт}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.httpd.shutdown()
        self.httpd.server_close()


def _чистое_окружение(tmp_path: Path, **ещё: str) -> dict[str, str]:
    """Окружение без прокси: curl иначе понёс бы запрос к 127.0.0.1 через него."""
    окружение = {к: з for к, з in os.environ.items()
                 if к.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
                 and к not in ("ASRHUB_SERVER", "ASRHUB_KEY", "ASRHUB_KEY_FILE")}
    (tmp_path / "home").mkdir(exist_ok=True)
    окружение.update({"HOME": str(tmp_path / "home"), "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
                      "NO_COLOR": "1", "ASRHUB_WAIT_LIMIT": "30", "NO_PROXY": "*",
                      "TMPDIR": str(tmp_path), **ещё})
    return окружение


def _asrctl(tmp_path: Path, *аргументы: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([BASH, str(КОРЕНЬ / "scripts" / "client" / "asrctl"), *аргументы],
                          capture_output=True, text=True, timeout=120, check=False,
                          env={**_чистое_окружение(tmp_path), **(env or {})})


# ---------------------------------------------------------------------------
# asrctl
# ---------------------------------------------------------------------------


def test_send_dir_с_ожиданием_переносит_файл_даже_при_неудаче_задания(tmp_path: Path):
    """`send-dir --wait --move-to`: задание создано, но упало — файл оставался.

    Каждый следующий запуск из cron заводил по нему ещё одно задание.
    """
    вход = tmp_path / "вход"
    вход.mkdir()
    (вход / "звонок.wav").write_bytes(b"RIFF0000WAVE")
    маршруты = {("POST", "/api/jobs"): _json({"id": "job_1"}),
                ("GET", "/api/jobs/job_1"): _json({"status": "failed", "error_message": "сбой"})}
    with _Сервер(маршруты) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send-dir", str(вход),
                       "--wait", "--move-to", str(tmp_path / "отправлено"))
    вывод = итог.stdout + итог.stderr
    assert (tmp_path / "отправлено" / "звонок.wav").exists(), вывод
    assert not (вход / "звонок.wav").exists(), вывод
    assert итог.returncode == 1, вывод
    assert "результата не дождались" in вывод


def test_перенаправление_не_выдаётся_за_ответ_сервера(tmp_path: Path):
    """Профиль http:// за прокси с 301 на https давал «✓ Сервер доступен»."""
    маршруты = {("GET", "/api/health"): (301, {"Location": "https://asr.example/api/health"}, b"")}
    with _Сервер(маршруты) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "health")
    assert итог.returncode == 3, итог.stdout + итог.stderr
    assert "перенаправлением" in итог.stderr
    assert "https://asr.example" in итог.stderr


def test_страница_не_json_с_кодом_200_не_успех(tmp_path: Path):
    """Страница входа прокси с кодом 200 разбиралась как пустой «успех»."""
    маршруты = {("GET", "/api/health"): (200, {"Content-Type": "text/html"},
                                         "<html>Вход в портал</html>".encode())}
    with _Сервер(маршруты) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "health")
    assert итог.returncode == 3, итог.stdout + итог.stderr
    assert "не ASR Hub" in итог.stderr


def test_send_с_двумя_файлами_отказывает_и_ничего_не_шлёт(tmp_path: Path):
    """`asrctl send *.mp3` отправлял последний файл и отвечал успехом."""
    for имя in ("a.wav", "b.wav"):
        (tmp_path / имя).write_bytes(b"RIFF")
    with _Сервер({("POST", "/api/jobs"): _json({"id": "job_1"})}) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send",
                       str(tmp_path / "a.wav"), str(tmp_path / "b.wav"))
        запросы = list(сервер.запросы)
    assert итог.returncode == 2, итог.stdout + итог.stderr
    assert not запросы


def test_send_dir_с_неразборным_settings_не_отправляет_ничего(tmp_path: Path):
    """Опечатка в --settings отправляла каждый файл на модель по умолчанию."""
    вход = tmp_path / "вход"
    вход.mkdir()
    for имя in ("a.wav", "b.wav"):
        (вход / имя).write_bytes(b"RIFF")
    with _Сервер({("POST", "/api/jobs"): _json({"id": "job_1"})}) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send-dir", str(вход),
                       "--settings", "{beam_size: 5")
        запросы = list(сервер.запросы)
    assert итог.returncode == 2, итог.stdout + итог.stderr
    assert not запросы, запросы


def test_без_python_пауза_очереди_видна(tmp_path: Path):
    """Режим «только bash и curl»: true из sed не равнялось True из python."""
    корзина = tmp_path / "bin"
    # python3 есть, но не работает — как заглушка на macOS без инструментов.
    _исполняемый(корзина / "python3", "#!/bin/sh\nexit 1\n")
    маршруты = {("GET", "/api/health"): _json({"status": "ok", "version": "9.9.9",
                                               "uptime_s": 5, "queue_paused": True})}
    with _Сервер(маршруты) as сервер:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "health",
                       env={"PATH": f"{корзина}:{os.environ['PATH']}"})
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "на паузе" in итог.stdout
    assert "9.9.9" in итог.stdout


# ---------------------------------------------------------------------------
# service.sh
# ---------------------------------------------------------------------------


def test_юнит_без_execreload_и_установка_не_зовёт_reload(tmp_path: Path):
    """`systemctl reload` посылал серверу SIGHUP — uvicorn на нём завершается."""
    журнал = tmp_path / "systemctl.log"
    корзина = tmp_path / "bin"
    _исполняемый(корзина / "systemctl", f'#!/bin/bash\necho "systemctl $*" >> "{журнал}"\n'
                 'case "$*" in *"show -p Version"*) echo Version=255 ;; esac\nexit 0\n')
    (tmp_path / "prog" / "server").mkdir(parents=True)
    итог = прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "service.sh"}" install --dry-run --prefix "{tmp_path / "prog"}" '
        f'--data "{tmp_path / "data"}" --name asrtest --port 8099',
        env={"PATH": f"{корзина}:{os.environ['PATH']}", "ASRHUB_SYSTEMD_DIR": str(tmp_path / "units"),
             "HOME": str(tmp_path)})
    assert "[Service]" in итог.stdout, итог.stdout + итог.stderr
    assert "ExecReload" not in итог.stdout.replace("# ExecReload", "")
    вызовы = журнал.read_text(encoding="utf-8") if журнал.exists() else ""
    assert "reload" not in вызовы.replace("daemon-reload", ""), вызовы


@нужен_root
def test_системная_служба_из_обычной_учётной_записи_спрашивается_у_системы():
    """Юнит в /etc/systemd/system, команда от пользователя — уходила в `--user`.

    `service.sh status` от обычной учётной записи отвечал «служба не найдена»
    про работающую системную службу.
    """
    база = Path(tempfile.mkdtemp(prefix="asrhub-54-", dir="/tmp"))
    try:
        база.chmod(0o755)
        журнал = база / "systemctl.log"
        журнал.touch()
        журнал.chmod(0o666)
        корзина = база / "bin"
        _исполняемый(корзина / "systemctl", f'#!/bin/bash\necho "systemctl $*" >> "{журнал}"\n'
                     'case "$*" in *"show -p Version"*) echo Version=255 ;; esac\nexit 0\n')
        (база / "prog" / "server").mkdir(parents=True)
        юниты = база / "units"
        юниты.mkdir()
        (юниты / "asrtest.service").write_text("[Service]\nExecStart=/bin/true\n", encoding="utf-8")
        дом = база / "home"
        дом.mkdir()
        os.chown(дом, 65534, 65534)
        итог = subprocess.run(
            ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups", BASH,
             str(КОРЕНЬ / "scripts" / "service.sh"), "status", "--prefix", str(база / "prog"),
             "--data", str(база / "data"), "--name", "asrtest"],
            env={**os.environ, "HOME": str(дом), "TMPDIR": str(дом), "ASRHUB_NO_COLOR": "1",
                 "PATH": f"{корзина}:{os.environ['PATH']}", "ASRHUB_SYSTEMD_DIR": str(юниты),
                 "ASRHUB_SYSTEMD_USER_DIR": str(дом / "units-user")},
            capture_output=True, text=True, timeout=60, check=False)
        вызовы = журнал.read_text(encoding="utf-8")
        assert "systemctl status asrtest.service" in вызовы, вызовы + итог.stdout + итог.stderr
        assert "--user status" not in вызовы
    finally:
        shutil.rmtree(база, ignore_errors=True)


def _библиотеки(тело: str, env: dict | None = None) -> subprocess.CompletedProcess:
    lib = КОРЕНЬ / "scripts" / "lib"
    return прогнать(f'source "{lib}/common.sh"\nsource "{lib}/detect.sh"\nsource "{lib}/gpu.sh"\n{тело}',
                    env=env, timeout=120)


def test_упавший_агент_launchd_не_считается_запущенным(tmp_path: Path):
    """Строка в `launchctl list` остаётся и у упавшего агента — с «-» вместо PID."""
    корзина = tmp_path / "bin"
    _исполняемый(корзина / "uname", '#!/bin/sh\ncase "$1" in -s|"") echo Darwin ;; -m) echo arm64 ;; '
                 '-r) echo 23.0.0 ;; *) echo Darwin ;; esac\n')
    # Голый `launchctl list` — таблица со строкой агента; `list МЕТКА` —
    # карточка упавшего агента: процесса нет, последний выход с ошибкой.
    _исполняемый(корзина / "launchctl", '#!/bin/bash\nif [[ "$1" == list && -n "${2:-}" ]]; then\n'
                 '  printf \'{\\n\\t"LastExitStatus" = 256;\\n\\t"Label" = "%s";\\n};\\n\' "$2"\n'
                 'elif [[ "$1" == list ]]; then\n  printf "PID\\tStatus\\tLabel\\n-\\t256\\tcom.asrhub.server\\n'
                 '-\\t256\\tru.asrhub.server\\n-\\t256\\tasrhub\\n"\nfi\nexit 0\n')
    итог = _библиотеки('state="$(service_state asrhub)" && rc=0 || rc=$?; echo "[${state}|${rc}]"',
                       env={"PATH": f"{корзина}:{os.environ['PATH']}"})
    assert "[failed|1]" in итог.stdout, итог.stdout + итог.stderr


def test_порт_для_проверки_не_берётся_у_чужого_сервера(tmp_path: Path):
    """Служба этой установки упала — проверка стучалась в порт соседней."""
    корзина = tmp_path / "bin"
    _исполняемый(корзина / "systemctl", "#!/bin/sh\nexit 0\n")
    prefix = tmp_path / "prog"
    (prefix / "server").mkdir(parents=True)
    данные = tmp_path / "data"
    данные.mkdir()
    (данные / "config.yaml").write_text("server:\n  server_port: 8123\n", encoding="utf-8")
    sleep = shutil.which("sleep")
    чужой = subprocess.Popen(["python3 -m asrhub --port 9911", "120"], executable=sleep)
    try:
        итог = _библиотеки(f'server_port_hint "{данные}" "{prefix}" native asrtest',
                           env={"PATH": f"{корзина}:{os.environ['PATH']}"})
    finally:
        чужой.kill()
        чужой.wait()
    assert итог.stdout.strip().endswith("8123"), итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# gpu.sh
# ---------------------------------------------------------------------------


def test_текст_отказа_nvidia_smi_не_принимается_за_версию(tmp_path: Path):
    """«Unable to determine the device handle…» становилось версией библиотек."""
    корзина = tmp_path / "bin"
    _исполняемый(корзина / "nvidia-smi", '#!/bin/sh\necho "Unable to determine the device handle '
                 'for GPU0000:01:00.0: Unknown Error"\nexit 0\n')
    итог = _библиотеки('printf "[%s]" "$(nvidia_userspace_version)"',
                       env={"PATH": f"{корзина}:{os.environ['PATH']}",
                            "ASRHUB_NVIDIA_LIB_GLOB": str(tmp_path / "нет" / "libnvidia-ml.so.*")})
    assert "Unable" not in итог.stdout, итог.stdout
    assert "[]" in итог.stdout, итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# doctor.sh
# ---------------------------------------------------------------------------


def test_доктор_стучится_по_адресу_сервера_и_видит_битую_базу(tmp_path: Path):
    """Три ошибки одним прогоном.

    Сервер с server_host 127.0.0.2 на 127.0.0.1 не отвечает — его порт
    объявлялся «занятым другой программой»; микрофон «доступен локально» для
    любого адреса, кроме 0.0.0.0; база, которая не открывается вовсе, шла
    зелёной галочкой «проверить нечем».
    """
    prefix = tmp_path / "prog"
    (prefix / "server").mkdir(parents=True)
    данные = tmp_path / "data"
    данные.mkdir()
    (данные / "asrhub.db").write_bytes(b"\x00 not a sqlite database \x00" * 200)
    маршруты = {("GET", "/api/health"): _json({"status": "ok", "version": "9.9.9"})}
    try:
        сервер = _Сервер(маршруты, адрес="127.0.0.2")
    except OSError:
        pytest.skip("127.0.0.2 недоступен")
    with сервер:
        (данные / "config.yaml").write_text(
            f"server:\n  server_host: 127.0.0.2\n  server_port: {сервер.порт}\n", encoding="utf-8")
        корзина = tmp_path / "bin"
        _исполняемый(корзина / "systemctl", "#!/bin/sh\nexit 0\n")
        итог = прогнать(f'bash "{КОРЕНЬ / "scripts" / "doctor.sh"}" --prefix "{prefix}" --data "{данные}"',
                        env={**_чистое_окружение(tmp_path), "PATH": f"{корзина}:{os.environ['PATH']}",
                             "ASRHUB_NO_COLOR": "1"}, timeout=300)
    вывод = итог.stdout + итог.stderr
    assert "отвечает на порту" in вывод and "9.9.9" in вывод, вывод[-3000:]
    строки = [с for с in вывод.splitlines() if "Микрофон" in с]
    assert строки and "нужен https" in строки[0], строки
    база = [с for с in вывод.splitlines() if "База заданий" in с]
    assert база and ("не открывается" in база[0] or "целостность" in база[0]), база
    assert "в порядке" not in база[0] and "нечем" not in база[0], база


# ---------------------------------------------------------------------------
# models.sh
# ---------------------------------------------------------------------------


def test_удаление_whisper_не_трогает_веса_faster_whisper(tmp_path: Path):
    """`remove whisper-small` стирал веса faster-whisper-small: имя источника общее."""
    модели = tmp_path / "data" / "models"
    чужие = модели / "models--Systran--faster-whisper-small" / "snapshots" / "x"
    чужие.mkdir(parents=True)
    (чужие / "model.bin").write_bytes(b"0" * 1024)
    (модели / "small.pt").write_bytes(b"1" * 2048)
    итог = прогнать(f'bash "{КОРЕНЬ / "scripts" / "models.sh"}" remove whisper-small --yes '
                    f'--data "{tmp_path / "data"}"',
                    env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path)}, timeout=120)
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 0, вывод
    assert not (модели / "small.pt").exists(), вывод
    assert (чужие / "model.bin").exists(), "удалены веса другой модели"


def test_каталог_моделей_тот_же_что_у_сервера(tmp_path: Path):
    """models_dir из config.yaml models.sh не читал: качал и искал в <данные>/models."""
    данные = tmp_path / "data"
    данные.mkdir()
    свои = tmp_path / "веса"
    свои.mkdir()
    (свои / "small.pt").write_bytes(b"1" * 2048)
    (данные / "config.yaml").write_text(f'runtime:\n  models_dir: "{свои}"\n', encoding="utf-8")
    итог = прогнать(f'bash "{КОРЕНЬ / "scripts" / "models.sh"}" remove whisper-small --yes '
                    f'--data "{данные}"', env={"HOME": str(tmp_path), "TMPDIR": str(tmp_path)},
                    timeout=120)
    assert итог.returncode == 0, _вывод(итог)
    assert not (свои / "small.pt").exists(), _вывод(итог)


# ---------------------------------------------------------------------------
# uninstall.sh
# ---------------------------------------------------------------------------


def test_удаление_отказывается_от_каталога_не_установки(tmp_path: Path):
    """Опечатка в --prefix стоила каталога целиком: проверки не было никакой."""
    чужой = tmp_path / "проекты"
    (чужой / "важное").mkdir(parents=True)
    (чужой / "важное" / "отчёт.txt").write_text("не удалять\n", encoding="utf-8")
    окружение = _стенд(tmp_path)
    итог = прогнать(f'bash "{КОРЕНЬ / "scripts" / "uninstall.sh"}" --prefix "{чужой}" '
                    f'--data "{tmp_path / "data"}" --yes', env=окружение, timeout=120)
    assert итог.returncode == 2, _вывод(итог)
    assert (чужой / "важное" / "отчёт.txt").exists()


def test_совпадающие_каталоги_без_purge_оставляют_данные(tmp_path: Path):
    """Каталог данных = каталог программы: обычное удаление сносило базу и модели."""
    prefix = tmp_path / "prog"
    итог = _установить(tmp_path, prefix=prefix)
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    (prefix / "asrhub.db").write_bytes(b"")
    (prefix / "models").mkdir()
    (prefix / "models" / "веса.bin").write_bytes(b"0")
    окружение = _стенд(tmp_path)
    итог = прогнать(f'bash "{prefix / "scripts" / "uninstall.sh"}" --prefix "{prefix}" '
                    f'--data "{prefix}" --yes', env=окружение, timeout=300)
    assert (prefix / "asrhub.db").exists(), _вывод(итог)[-3000:]
    assert (prefix / "models" / "веса.bin").exists()
    assert not (prefix / "server").exists(), "код программы должен быть удалён"


# ---------------------------------------------------------------------------
# update.sh
# ---------------------------------------------------------------------------


def _установка_со_службой(база: Path) -> tuple[Path, Path, dict[str, str], Path]:
    """Установка и «работающая» служба asrtest: systemctl с состоянием в файле."""
    порт = _свободный_порт()
    prefix = база / "prog"
    итог = _установить(база, f"--port {порт}", prefix=prefix)
    assert итог.returncode == 0, _вывод(итог)[-3000:]
    (prefix / "server" / "СТАРЫЙ").write_text("прежняя версия\n", encoding="utf-8")
    окружение = _стенд(база)
    состояние = база / "служба.state"
    состояние.write_text("active\n", encoding="utf-8")
    журнал = база / "systemctl.log"
    корзина = база / "sbin"
    _исполняемый(корзина / "systemctl", f'''#!/bin/bash
echo "systemctl $*" >> "{журнал}"
case "$*" in
  *"show -p Version"*) echo Version=255 ;;
  *"show -p MainPID"*) echo 0 ;;
  *"show -p"*) echo "" ;;
  *is-active*) if grep -qx active "{состояние}"; then echo active; exit 0; fi; echo inactive; exit 3 ;;
  *" status "*|status*) grep -qx active "{состояние}" && exit 0; exit 3 ;;
  *restart*|*" start "*|start*) echo active > "{состояние}" ;;
  *" stop "*|stop*) echo inactive > "{состояние}" ;;
esac
exit 0
''')
    юниты = Path(окружение["ASRHUB_SYSTEMD_DIR"])
    юниты.mkdir(parents=True, exist_ok=True)
    (юниты / "asrtest.service").write_text(
        f"[Service]\nWorkingDirectory={prefix}/server\n"
        f"ExecStart={prefix}/venv/bin/python -m asrhub --port {порт}\n", encoding="utf-8")
    окружение["PATH"] = f"{корзина}:{окружение['PATH']}"
    окружение["HEALTH_WAIT_S"] = "4"
    return prefix, состояние, окружение, журнал


def _обновить(prefix: Path, база: Path, окружение: dict, *ключи: str) -> subprocess.CompletedProcess:
    return прогнать(f'bash "{КОРЕНЬ / "scripts" / "update.sh"}" --prefix "{prefix}" '
                    f'--data "{база / "data"}" --source "{КОРЕНЬ}" --name asrtest --yes '
                    + " ".join(ключи), env=окружение, timeout=600)


def test_сбой_копирования_посреди_обновления_не_ломает_установку(tmp_path: Path):
    """Каталоги подменялись по одному: сбой на scripts оставлял новый server
    при старых scripts, а остановленную службу никто не поднимал."""
    prefix, состояние, окружение, журнал = _установка_со_службой(tmp_path)
    корзина = tmp_path / "fbin"
    _исполняемый(корзина / "cp", f'''#!/bin/bash
for a in "$@"; do
  [[ "$a" == "{КОРЕНЬ}/scripts" || "$a" == "{КОРЕНЬ}/scripts/" ]] && {{ echo "cp: отказ (проба)" >&2; exit 1; }}
done
exec /bin/cp "$@"
''')
    окружение["PATH"] = f"{корзина}:{окружение['PATH']}"
    итог = _обновить(prefix, tmp_path, окружение)
    вывод = _вывод(итог)
    assert итог.returncode != 0, вывод[-3000:]
    assert (prefix / "server" / "СТАРЫЙ").exists(), "каталог server подменён наполовину\n" + вывод[-3000:]
    assert (prefix / "scripts" / "update.sh").exists()
    assert состояние.read_text(encoding="utf-8").strip() == "active", \
        журнал.read_text(encoding="utf-8") + вывод[-3000:]


def test_неудачное_обновление_с_откатом_кончается_кодом_1_и_возвращает_пакеты(tmp_path: Path):
    """Сервер не ответил → откат → «exit 0»: cron видел успех.

    И откат не возвращал пакеты окружения: прежний код оставался на новых.
    """
    prefix, состояние, окружение, журнал = _установка_со_службой(tmp_path)
    итог = _обновить(prefix, tmp_path, окружение)
    вывод = _вывод(итог)
    assert итог.returncode == 1, вывод[-3000:]
    assert "Откат" in вывод or "откат" in вывод
    вызовы = (tmp_path / "вызовы.log").read_text(encoding="utf-8")
    assert "pip-freeze.txt" in вызовы, вывод[-3000:]
    assert (prefix / "server" / "СТАРЫЙ").exists()


def test_прерванное_обновление_не_переписывает_снимок_и_no_restart_не_гасит_службу(tmp_path: Path):
    """Два дефекта одного места.

    Метка незаконченного обновления: следующий запуск снимал «снимок» с
    наполовину обновлённого каталога и терял рабочий. --no-restart
    останавливал службу и не запускал её.
    """
    prefix, состояние, окружение, журнал = _установка_со_службой(tmp_path)
    снимок = Path(f"{prefix}.snapshot")
    for имя in ("server", "scripts", "VERSION"):
        источник = prefix / имя
        if источник.is_dir():
            shutil.copytree(источник, снимок / имя, symlinks=True)
        else:
            снимок.mkdir(parents=True, exist_ok=True)
            shutil.copy2(источник, снимок / имя)
    (снимок / "server" / "СНИМОК").write_text("рабочая версия\n", encoding="utf-8")
    (prefix / ".asrhub-update-in-progress").write_text("3.1.0\n", encoding="utf-8")
    итог = _обновить(prefix, tmp_path, окружение, "--no-restart")
    вывод = _вывод(итог)
    assert итог.returncode == 0, вывод[-3000:]
    assert (снимок / "server" / "СНИМОК").exists(), "снимок рабочей версии переписан\n" + вывод[-3000:]
    assert "не завершилось" in вывод
    вызовы = журнал.read_text(encoding="utf-8") if журнал.exists() else ""
    assert "stop" not in вызовы, вызовы
    assert состояние.read_text(encoding="utf-8").strip() == "active"
    assert not (prefix / ".asrhub-update-in-progress").exists()
