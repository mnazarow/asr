"""Скрипты установки: регрессии шестого захода.

Проверяется не «скрипт запускается», а поведение, ради которого правка
делалась: срабатывает ли откат при сбое внутри функции, не врёт ли проверка
порта, переживает ли обновление отсутствие config.yaml, и читает ли сервер
конфигурацию, которую пишет установщик.

Всё гоняется настоящим bash на макетах в tmp_path — без установки чего-либо
в систему.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="нужен bash")


def run_bash(script: str, cwd: Path | None = None, env: dict | None = None):
    """Выполняет фрагмент оболочки и возвращает результат целиком."""
    full_env = {**os.environ, "ASRHUB_QUIET": "0", **(env or {})}
    return subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                          cwd=str(cwd) if cwd else None, env=full_env, timeout=120)


# ---------------------------------------------------------------------------
# Обработка ошибок
# ---------------------------------------------------------------------------


def test_failure_inside_a_function_still_rolls_back(repo_root: Path, tmp_path: Path):
    """Ловушка ERR не наследовалась функциями — и весь разбор ошибок был мёртв.

    Без `set -o errtrace` сбой внутри функции завершал установку с кодом 1
    без сообщения, без записи в журнал и без отката уже сделанных изменений,
    который шапка install.sh обещает прямым текстом. А вся работа скриптов
    происходит именно в функциях.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    marker = tmp_path / "должно-откатиться"
    script = f'''
      source "{common}"
      enable_error_handling
      mkdir -p "{marker}"
      add_rollback "rm -rf '{marker}'"
      шаг() {{ step "Установка чего-нибудь"; false; }}
      шаг
    '''
    result = run_bash(script)
    assert result.returncode != 0
    assert "Сбой на шаге" in result.stdout + result.stderr, \
        "сбой внутри функции прошёл молча"
    assert not marker.exists(), "откат не выполнен"


# ---------------------------------------------------------------------------
# Проверка порта
# ---------------------------------------------------------------------------


def test_busy_port_is_not_reported_free(repo_root: Path):
    """`grep -q` рвал трубу, и pipefail превращал занятый порт в свободный.

    На любой машине, где сокетов больше, чем влезает в буфер трубы,
    doctor.sh сообщал «сервер не запущен», а install.sh не предлагал сменить
    порт и вёл установку на занятый.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    script = f'''
      source "{common}"
      # Настоящий вывод ss на загруженной машине — тысячи строк.
      ss() {{ seq 1 300000 | awk '{{print "LISTEN 0 4096 127.0.0.1:8080 0.0.0.0:*"}}'; }}
      if check_port_free 8080; then echo "СВОБОДЕН"; else echo "занят"; fi
      if check_port_free 9999; then echo "свободен"; else echo "ЗАНЯТ"; fi
    '''
    out = run_bash(script).stdout
    assert out.split() == ["занят", "свободен"], f"проверка порта врёт: {out!r}"


# ---------------------------------------------------------------------------
# Пробный запуск и права
# ---------------------------------------------------------------------------


def test_dry_run_leaves_nothing_on_disk(repo_root: Path, tmp_path: Path):
    """`--dry-run` создавал каталоги и тут же писал «изменений не вносилось»."""
    prefix, data = tmp_path / "программа", tmp_path / "данные"
    result = subprocess.run(
        [BASH, str(repo_root / "scripts" / "install.sh"), "--dry-run", "--yes",
         "--no-interactive", "--prefix", str(prefix), "--data", str(data),
         "--profile", "light", "--skip-models", "--no-service", "--offline"],
        capture_output=True, text=True, timeout=300, cwd=str(repo_root))
    assert "изменений не вносилось" in result.stdout
    assert not prefix.exists(), "каталог программы создан при пробном запуске"
    assert not data.exists(), "каталог данных создан при пробном запуске"


def test_data_directory_keeps_its_permissions(repo_root: Path, tmp_path: Path):
    """Каталог данных оставался 0755, а в нём config.yaml с ключами доступа.

    `check_disk_space` создавал его первым обычным `mkdir`, и `ensure_dir`
    видел каталог существующим — chmod пропускался.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    data = tmp_path / "данные"
    script = f'''
      source "{common}"
      check_disk_space "{data}" 0
      ensure_dir "{data}" 0750
      write_file "{data}/config.yaml" 0640 <<'CFG'
api_keys:
  ah_СЕКРЕТ: {{role: admin}}
CFG
      stat -c '%a' "{data}" "{data}/config.yaml"
    '''
    out = run_bash(script).stdout.split()
    assert out == ["750", "640"], f"права разъехались: {out}"


# ---------------------------------------------------------------------------
# Обновление
# ---------------------------------------------------------------------------


def test_update_survives_a_missing_config(repo_root: Path):
    """Обновление обрывалось с кодом 2, если config.yaml нет.

    Установка без файла конфигурации — случай штатный и самый частый; код и
    зависимости к этому моменту уже заменены, а вызывающий видел провал.
    """
    source = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    line = next(row for row in source.splitlines()
                if "server_port:" in row and "grep" in row)
    assert "|| true" in line, "grep по config.yaml снова без страховки"
    script = f'''
      set -o errexit -o pipefail
      DATA_DIR=/несуществующий
      {line.strip()}
      PORT="${{PORT:-8080}}"
      echo "порт ${{PORT}}"
    '''
    result = run_bash(script)
    assert result.returncode == 0, f"строка всё ещё обрывает обновление: {result.stderr}"
    assert "порт 8080" in result.stdout


def test_update_recognises_engines_by_package_not_by_filename(repo_root: Path):
    """Имя файла требований не равно имени модуля.

    Для diarization, vad, postprocess, mfa и ещё нескольких такого модуля нет
    вовсе — проверка молча не срабатывала, и эти движки не обновлялись
    никогда.
    """
    source = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    assert "engine_installed" in source, "проверка по имени модуля вернулась"
    assert "tr '-' '_'" not in source or "module=" not in source

    names = [p.stem for p in (repo_root / "requirements" / "engines").glob("*.txt")]
    broken = [n for n in names
              if not (repo_root / "server" / "asrhub" / "engines").exists()]
    assert not broken
    # Хотя бы для одного движка имя файла и имя модуля заведомо расходятся —
    # именно на таких проверка и молчала.
    assert any(n in ("diarization", "vad", "postprocess", "mfa") for n in names)


# ---------------------------------------------------------------------------
# Согласие с сервером
# ---------------------------------------------------------------------------


def test_generated_config_is_readable_by_the_server(repo_root: Path, tmp_path: Path):
    """Установщик обязан писать конфигурацию, которую сервер примет.

    Разделы верхнего уровня сервер раскрывает как угодно, а вот неизвестный
    ПАРАМЕТР внутри — это отказ загрузки целиком: «Неизвестные параметры в
    файле конфигурации», и сервер не стартует. Опечатка в шаблоне
    установщика стоила бы ровно этого, причём на чужой машине.
    """
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    body = re.search(r"write_file \"\$\{CONFIG_FILE\}\" 0640 <<CFGEOF\n(.*?)\nCFGEOF",
                     source, re.S)
    assert body, "не найден шаблон config.yaml в install.sh"
    text = body.group(1)
    # Подставляем значения оболочки так, как это сделал бы установщик.
    substitutions = {
        "${DATA_DIR}": str(tmp_path / "данные"),
        "${RECOMMENDED_MODEL}": "demo-simulator",
        "${HOST}": "0.0.0.0",
        "${PORT}": "8080",
        "${STREAM_ENABLED}": "true",
        "${GPU_BATCHING_BLOCK}": "  device: auto\n  compute_type: auto",
        "${ALIGNMENT}": "none",
        "${MONITORING}": "prometheus_pushgateway",
        "${MONITORING_URL:-http://localhost:9091}": "http://localhost:9091",
    }
    for key, value in substitutions.items():
        text = text.replace(key, value)
    # Остатки подстановок оболочки: строки с ними в проверку не берём —
    # они зависят от найденного железа, а не от схемы.
    lines = [line for line in text.splitlines()
             if "$(" not in line and "${" not in line]
    config = tmp_path / "config.yaml"
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    from asrhub.config import load

    settings = load(config)
    assert settings.get("stream_enabled") is True
    assert settings.get("stream_window_s") == 4
    assert settings.get("server_port") == 8080

    # И наоборот: опечатка в имени параметра обязана валить загрузку — иначе
    # проверка выше ничего не стоит.
    from asrhub.errors import ASRHubError

    config.write_text(config.read_text(encoding="utf-8")
                      .replace("stream_enabled:", "stream_enable:"), encoding="utf-8")
    with pytest.raises(ASRHubError):
        load(config)


def test_doctor_knows_about_the_new_capabilities(repo_root: Path, tmp_path: Path):
    """Доктор ничего не знал о том, что сервер умеет сверх обработки файлов.

    Поток, подразделения и квоты ключей, несколько экземпляров над общей
    базой — всё это либо работает, либо тихо не работает, и заметить второе
    было нечем.
    """
    data = tmp_path / "данные"
    data.mkdir()
    (data / "config.yaml").write_text(
        "server_port: 8080\nserver_host: 127.0.0.1\nstream_enabled: true\n"
        "api_keys:\n"
        "  ah_8ADnc7xWanCu:\n    name: бухгалтер-1\n    role: user\n"
        "    group: бухгалтерия\n    quota_jobs_per_day: 500\n"
        "  ah_Kq2mZpLr4tYv:\n    name: продажи-1\n    role: user\n    group: продажи\n",
        encoding="utf-8")
    result = subprocess.run(
        [BASH, str(repo_root / "scripts" / "doctor.sh"), "--network", "--data", str(data)],
        capture_output=True, text=True, timeout=180, cwd=str(repo_root))
    out = result.stdout
    assert "Распознавание на лету" in out, "нет проверки потока"
    assert "Микрофон в браузере" in out, "нет предупреждения о https"
    assert "подразделения: бухгалтерия,продажи" in out, \
        "подразделения не разобраны (проверьте, что переменная не GROUPS)"
    assert "квот задано: 1" in out


def test_dictation_needs_https_and_scripts_say_so(repo_root: Path):
    """Микрофон браузер отдаёт только по https или на localhost.

    Установщик предлагает 0.0.0.0 вторым и умолчательным вариантом — значит
    рекомендованная им установка даёт неработающую «Диктовку». Об этом надо
    предупреждать, а не оставлять пользователя гадать.
    """
    installer = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    doctor = (repo_root / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert "https" in installer and "микрофон" in installer.lower()
    assert "нужен https или localhost" in doctor


def test_examples_are_installed(repo_root: Path):
    """Интерфейс «Диктовки» ссылается на examples/stream_microphone.py.

    Файла не было ни в одной установке: каталог examples не копировал ни
    install, ни update.
    """
    assert (repo_root / "examples" / "stream_microphone.py").exists()
    for name in ("install.sh", "update.sh"):
        text = (repo_root / "scripts" / name).read_text(encoding="utf-8")
        assert re.search(r"for item in .*\bexamples\b", text), f"{name} не ставит examples"
    for name in ("install.ps1", "update.ps1"):
        text = (repo_root / "scripts" / name).read_text(encoding="utf-8")
        assert "'examples'" in text, f"{name} не ставит examples"


# ---------------------------------------------------------------------------
# Несколько установок на одной машине
# ---------------------------------------------------------------------------


def test_uninstall_does_not_kill_other_installations(repo_root: Path):
    """Удаление одной установки убивало серверы всех остальных.

    Шаблон ловил всё, где встречается «asrhub», — включая соседнюю установку
    в другом каталоге и на другом порту, и любую постороннюю оболочку, у
    которой эти слова оказались в командной строке.
    """
    text = (repo_root / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    assert 'pgrep -f "python.*-m asrhub|uvicorn.*asrhub"' not in text, \
        "вернулся шаблон, ловящий чужие процессы"
    assert "${PREFIX}/venv/bin/python" in text, "процессы больше не сверяются с установкой"
    assert "ASRHUB_DATA_DIR=${DATA_DIR}" in text, "нет запасного пути по каталогу данных"


def test_service_name_is_passed_through(repo_root: Path):
    """Вторая установка на той же машине перезаписывала юнит первой."""
    installer = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--name \"${SERVICE_NAME}\"" in installer, "имя службы не передаётся"
    assert "--name)        SERVICE_NAME=" in installer, "нет ключа --name у установщика"


def test_shared_data_directory_is_not_taken_over(repo_root: Path):
    """`chown -R` отбирал общий каталог данных у соседних машин.

    `useradd --system` даёт на каждой машине свой uid, и вторая установка
    делала базу нечитаемой для первой.
    """
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "DATA_OWNER=" in text, "владелец каталога данных не проверяется"
    assert "принадлежит пользователю" in text


def test_systemd_unit_survives_spaces_and_keeps_limits(repo_root: Path, tmp_path: Path):
    """Юнит ломался на пробелах в путях, а StartLimit* стоял не в той секции.

    `ReadWritePaths` при этом отбрасывался целиком — вместе с
    `ProtectSystem=full` это делает каталог данных доступным только на
    чтение, и сервер не может записать ни результат, ни базу.
    """
    result = subprocess.run(
        [BASH, str(repo_root / "scripts" / "service.sh"), "install", "--dry-run",
         "--prefix", "/opt/asr hub", "--data", "/var/lib/asr hub", "--user", "asrhub"],
        capture_output=True, text=True, timeout=60, cwd=str(repo_root))
    unit = result.stdout
    assert "WorkingDirectory=/opt/asr\\x20hub/server" in unit, \
        "пробел в пути не экранирован для systemd"
    assert "ReadWritePaths=/var/lib/asr\\x20hub" in unit
    assert 'ExecStart="/opt/asr hub/venv/bin/python"' in unit
    head = unit.split("\n[Service]\n")[0]
    assert "StartLimitIntervalSec" in head, "StartLimit* снова в секции [Service]"

    checker = shutil.which("systemd-analyze")
    if checker:
        path = tmp_path / "probe.service"   # systemd не принимает кириллицу в имени юнита
        path.write_text(unit[unit.index("[Unit]"):], encoding="utf-8")
        verify = subprocess.run([checker, "verify", str(path)],
                                capture_output=True, text=True, timeout=60)
        noise = [line for line in (verify.stderr + verify.stdout).splitlines()
                 if line.strip() and "not executable" not in line]
        assert not noise, "systemd недоволен юнитом:\n" + "\n".join(noise)


def test_purge_asks_before_deleting_a_shared_data_directory(repo_root: Path,
                                                            tmp_path: Path):
    """Каталог данных может быть общим — над ним работают другие машины.

    `--purge` уносил их базу, задания и результаты вместе с нашими, и
    предупреждение говорило только «удалить и данные».
    """
    import socket

    from asrhub import job_queue as jq
    from asrhub.db import Database

    data = tmp_path / "общие-данные"
    data.mkdir()
    db = Database(data / "asrhub.db")
    job = db.create_job({"filename": "чужое.wav", "status": "running", "model": "demo"})
    db.update_job(job, instance_id="соседняя-машина:4242", heartbeat_at=jq.now())
    mine = db.create_job({"filename": "моё.wav", "status": "running", "model": "demo"})
    db.update_job(mine, instance_id=f"{socket.gethostname()}:1", heartbeat_at=jq.now())
    db.close()

    common = repo_root / "scripts" / "lib" / "common.sh"
    found = run_bash(f'source "{common}"; other_instances "{data}"').stdout.strip()
    assert found == "соседняя-машина:4242", \
        f"чужой экземпляр не найден (или свой посчитан чужим): {found!r}"

    result = subprocess.run(
        [BASH, str(repo_root / "scripts" / "uninstall.sh"), "--purge",
         "--prefix", str(tmp_path / "нет"), "--data", str(data)],
        input="n\n", capture_output=True, text=True, timeout=120, cwd=str(repo_root))
    assert "работают другие серверы" in result.stdout + result.stderr, \
        "удаление общего каталога не предупреждает о соседях"
    assert (data / "asrhub.db").exists(), "база соседей удалена после отказа"


def test_stale_neighbour_does_not_block_anything(repo_root: Path, tmp_path: Path):
    """Экземпляр, который давно молчит, соседом не считается.

    Иначе один давно умерший сервер навсегда запретил бы удаление и
    обновление — предупреждение без выхода хуже, чем его отсутствие.
    """
    from asrhub import job_queue as jq
    from asrhub.db import Database

    data = tmp_path / "данные"
    data.mkdir()
    db = Database(data / "asrhub.db")
    job = db.create_job({"filename": "брошено.wav", "status": "running", "model": "demo"})
    db.update_job(job, instance_id="умерший:1", heartbeat_at=jq.now() - 3600)
    db.close()

    common = repo_root / "scripts" / "lib" / "common.sh"
    found = run_bash(f'source "{common}"; other_instances "{data}"').stdout.strip()
    assert found == "", f"молчащий экземпляр посчитан живым: {found!r}"


# ---------------------------------------------------------------------------
# Виртуальное окружение Python
# ---------------------------------------------------------------------------


def _fake_python(directory: Path, version: str = "3.14", *,
                 has_ensurepip: bool = False) -> Path:
    """Интерпретатор, у которого venv есть, а ensurepip нет.

    Ровно так выглядит Debian и Ubuntu без пакета pythonX.Y-venv.
    """
    path = directory / "python3"
    ensurepip = "exit 0" if has_ensurepip else "exit 1"
    path.write_text(f'''#!/bin/sh
case "$*" in
  *"import ensurepip"*) {ensurepip} ;;
  *"import venv"*) exit 0 ;;
  *"-m venv"*)
      echo "The virtual environment was not created successfully because" >&2
      echo "ensurepip is not available." >&2
      exit 1 ;;
  *version_info*) echo "{version}" ;;
  -V|--version) echo "Python {version}.0" ;;
  *) exec /usr/bin/python3 "$@" ;;
esac
''', encoding="utf-8")
    path.chmod(0o755)
    return path


def _fake_apt_cache(directory: Path, known: tuple[str, ...]) -> None:
    listed = "|".join(known)
    path = directory / "apt-cache"
    path.write_text(f'#!/bin/sh\ncase "$2" in\n  {listed}) exit 0 ;;\n'
                    '  *) exit 100 ;;\nesac\n', encoding="utf-8")
    path.chmod(0o755)


def test_missing_ensurepip_is_noticed_before_the_venv_is_built(repo_root: Path,
                                                               tmp_path: Path):
    """Проверялся `import venv`, а ломался `ensurepip`.

    `venv` — часть стандартной библиотеки и импортируется всегда, даже когда
    пакета pythonX.Y-venv нет. В Debian и Ubuntu именно в нём лежит
    ensurepip, без которого `python -m venv` доходит до конца и падает — уже
    после того, как каталоги созданы. Установка сваливалась в откат на
    шестом шаге вместо того, чтобы поставить один пакет на втором.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    python = _fake_python(stub)
    _fake_apt_cache(stub, ("python3.14-venv", "python3.14-dev", "ffmpeg", "git"))

    result = subprocess.run(
        [BASH, str(repo_root / "scripts" / "install.sh"), "--dry-run",
         "--no-interactive", "--yes", "--profile", "light", "--skip-models",
         "--no-service", "--offline", "--prefix", str(tmp_path / "app"),
         "--data", str(tmp_path / "data")],
        env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin",
             "ASRHUB_PYTHON": str(python)},
        capture_output=True, text=True, timeout=300, cwd=str(repo_root))
    assert result.returncode == 0, result.stdout[-1500:]
    assert "python3.14-venv" in result.stdout, \
        f"пакет venv не попал в список недостающих:\n{result.stdout[-1500:]}"


def test_venv_package_name_follows_the_chosen_interpreter(repo_root: Path,
                                                          tmp_path: Path):
    """Метапакет python3-venv тянет ensurepip для ДРУГОГО интерпретатора.

    Если сервер ставится на python3.14 из стороннего репозитория, а мы
    просим python3-venv, ensurepip так и не появится — установка упадёт
    второй раз тем же способом.
    """
    stub = tmp_path / "bin"
    stub.mkdir()
    _fake_python(stub)
    common = repo_root / "scripts" / "lib" / "common.sh"
    detect = repo_root / "scripts" / "lib" / "detect.sh"

    def names(known: tuple[str, ...]) -> str:
        _fake_apt_cache(stub, known)
        script = (f'source "{common}"; source "{detect}"; '
                  f'export ASRHUB_PYTHON_FOR_PACKAGES="{stub}/python3"; '
                  'system_package_names python-venv')
        return subprocess.run([BASH, "-c", script],
                              env={**os.environ, "PATH": f"{stub}:/usr/bin:/bin"},
                              capture_output=True, text=True, timeout=60).stdout

    assert "python3.14-venv" in names(("python3.14-venv", "python3.14-dev")), \
        "версионный пакет не выбран"
    # Если версионного пакета в репозитории нет — берём метапакет, иначе apt
    # отвергнет всю установку целиком из-за одного несуществующего имени.
    assert names(("python3-venv", "python3-dev")).split() == ["python3-venv", "python3-dev"]


def test_venv_failure_names_the_package_to_install(repo_root: Path):
    """Отказ обязан заканчиваться командой, а не общим «смотрите выше».

    Сообщение интерпретатора правильное, но теряется среди строк отката.
    """
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = text.split('step "Виртуальное окружение Python"', 1)[1][:4000]
    assert "Не удалось создать виртуальное окружение" in block
    assert "import ensurepip" in block, "причина не различается"
    assert "sudo apt install ${VENV_PKG}" in block, "нет готовой команды"
    assert "--python /usr/bin/python3.12" in block, "не предложен другой интерпретатор"


# ---------------------------------------------------------------------------
# Проверенный диапазон версий Python
# ---------------------------------------------------------------------------


def _python_stub(directory: Path, version: str) -> Path:
    path = directory / f"python{version}" if version else directory / "python3"
    path.write_text(f'''#!/bin/sh
case "$*" in
  *version_info*) echo "{version}" ;;
  -V|--version) echo "Python {version}.0" ;;
  *) exec /usr/bin/python3 "$@" ;;
esac
''', encoding="utf-8")
    path.chmod(0o755)
    return path


def _only_these_tools(directory: Path) -> Path:
    """PATH, в котором нет ничего лишнего — иначе найдётся системный python."""
    stub = directory / "path"
    stub.mkdir(exist_ok=True)
    for name in ("bash", "sed", "awk", "tr", "cut", "grep", "head", "uname",
                 "sort", "basename", "dirname", "cat", "date", "stat", "mkdir",
                 "ls", "printf", "command"):
        source = shutil.which(name)
        if source and not (stub / name).exists():
            (stub / name).symlink_to(source)
    return stub


def test_too_new_python_is_named_before_the_engines_fail(repo_root: Path,
                                                         tmp_path: Path):
    """Установка шла на Python 3.14 и разваливалась внутри pip на восьмом шаге.

    Колёса torch, onnxruntime и nemo выходят под свежие версии Python с
    задержкой в месяцы: GigaAM требует onnxruntime==1.23.*, а у него нет
    колёс новее cp313. Сообщение pip — «no matching distributions available
    for your environment» — не подсказывает, что менять надо интерпретатор.
    """
    stub = _only_these_tools(tmp_path)
    shutil.copy(_python_stub(tmp_path, "3.14"), stub / "python3")
    common = repo_root / "scripts" / "lib" / "common.sh"
    result = subprocess.run(
        [BASH, "-c", f'source "{common}"; check_python'],
        env={"PATH": str(stub), "ASRHUB_QUIET": "0", "TERM": "dumb"},
        capture_output=True, text=True, timeout=60)
    combined = result.stdout + result.stderr
    assert "новее проверенной" in combined, f"о слишком новой версии молчим:\n{combined}"
    assert "onnxruntime" in combined, "не названа настоящая причина"
    assert "--python" in combined, "нет готовой команды с другим интерпретатором"
    # Установку при этом не запрещаем: часть движков соберётся и на новой.
    assert result.returncode == 0, "слишком новый Python стал запретом вместо предупреждения"


def test_supported_python_is_preferred_when_both_exist(repo_root: Path,
                                                       tmp_path: Path):
    """Из двух интерпретаторов берём проверенный, а не первый попавшийся."""
    stub = _only_these_tools(tmp_path)
    shutil.copy(_python_stub(tmp_path, "3.14"), stub / "python3")
    shutil.copy(_python_stub(tmp_path, "3.12"), stub / "python3.12")
    common = repo_root / "scripts" / "lib" / "common.sh"
    result = subprocess.run(
        [BASH, "-c", f'source "{common}"; check_python'],
        env={"PATH": str(stub), "ASRHUB_QUIET": "1", "TERM": "dumb"},
        capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "python3.12", \
        f"выбран не проверенный интерпретатор: {result.stdout.strip()!r}"
    assert "новее проверенной" not in result.stderr, "лишнее предупреждение"


def test_version_gt_is_strict(repo_root: Path):
    """«3.13 новее 3.13» — неверно, и на этом строится вся проверка."""
    common = repo_root / "scripts" / "lib" / "common.sh"
    script = (f'source "{common}"; '
              'for pair in "3.14 3.13" "3.13 3.13" "3.12 3.13" "3.9 3.10"; do '
              'set -- $pair; version_gt "$1" "$2" && echo "да" || echo "нет"; done')
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True,
                         timeout=60).stdout.split()
    assert out == ["да", "нет", "нет", "нет"], out


def test_engine_failure_points_at_the_interpreter(repo_root: Path):
    """Отсылка к журналу — не ответ, когда причина известна заранее."""
    models = (repo_root / "scripts" / "models.sh").read_text(encoding="utf-8")
    assert 'version_gt "${PY_VERSION}" "${ASRHUB_MAX_PYTHON}"' in models, \
        "models.sh не сверяет версию окружения при отказе"
    assert "onnxruntime==1.23" in models, "не назван пример настоящей причины"
    installer = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "Не установились движки" in installer
    assert "новее проверенной" in installer, "итог установки молчит о причине"
    doctor = (repo_root / "scripts" / "doctor.sh").read_text(encoding="utf-8")
    assert "Версия Python" in doctor, "доктор не проверяет версию Python"


# ---------------------------------------------------------------------------
# Установщик сам ставит проверенную версию Python
# ---------------------------------------------------------------------------


def _machine_with(tmp_path: Path, *, found: str, available: tuple[str, ...]) -> Path:
    """PATH, где есть только заданный Python, а apt умеет поставить нужные.

    Изоляция здесь обязательна: в сборочной машине свой python3.13, и без
    неё проверка молча измеряла бы её, а не макет.
    """
    stub = tmp_path / "машина"
    stub.mkdir(exist_ok=True)
    for name in ("bash", "sh", "sed", "awk", "tr", "cut", "grep", "head", "tail",
                 "printf", "uname", "command", "sort", "basename", "dirname",
                 "cat", "date", "stat", "mkdir", "ls", "rm", "cp", "mv", "find",
                 "id", "chmod", "tee", "wc", "env", "test", "df", "du", "xargs",
                 "touch", "readlink", "realpath", "sleep"):
        source = shutil.which(name)
        if source and not (stub / name).exists():
            (stub / name).symlink_to(source)

    (stub / "python3").write_text(f'''#!/bin/sh
case "$*" in
  *"import ensurepip"*) exit 0 ;;
  *version_info*) echo "{found}" ;;
  -V|--version) echo "Python {found}.0" ;;
  *) exec /usr/bin/python3 "$@" ;;
esac
''', encoding="utf-8")
    (stub / "python3").chmod(0o755)

    listed = "|".join([*available, "ffmpeg", "git"])
    (stub / "apt-cache").write_text(
        f'#!/bin/sh\ncase "$2" in\n  {listed}) exit 0 ;;\n  *) exit 100 ;;\nesac\n',
        encoding="utf-8")
    (stub / "apt-cache").chmod(0o755)

    # Поддельный apt-get «ставит» интерпретатор, создавая его в том же каталоге.
    versions = sorted({name.split("python")[1].split("-")[0]
                       for name in available if name.startswith("python")})
    branches = "\n".join(
        f'    if [ "$a" = "python{version}" ]; then _make "{version}"; fi'
        for version in versions)
    (stub / "apt-get").write_text(f'''#!/bin/sh
_make() {{
  printf '#!/bin/sh\\ncase "$*" in\\n  *"import ensurepip"*) exit 0 ;;\\n'\\
'  *version_info*) echo "%s" ;;\\n  -V|--version) echo "Python %s.0" ;;\\n'\\
'  *) exec /usr/bin/python3 "$@" ;;\\nesac\\n' "$1" "$1" > "$(dirname "$0")/python$1"
  chmod +x "$(dirname "$0")/python$1"
}}
for a in "$@"; do
{branches}
done
exit 0
''', encoding="utf-8")
    (stub / "apt-get").chmod(0o755)
    return stub


def _install(repo_root: Path, stub: Path, target: Path, *extra: str):
    return subprocess.run(
        [BASH, str(repo_root / "scripts" / "install.sh"), "--no-interactive", "--yes",
         "--profile", "light", "--skip-models", "--no-service", "--offline",
         "--prefix", str(target / "prog"), "--data", str(target / "data"), *extra],
        env={"PATH": str(stub), "TERM": "dumb", "HOME": str(target)},
        capture_output=True, text=True, timeout=300, cwd=str(repo_root))


def test_installer_installs_a_supported_python_itself(repo_root: Path, tmp_path: Path):
    """Предупреждения мало: человек читает совет и делает три действия руками.

    У скрипта есть и менеджер пакетов, и права, ради которых его запускают
    под sudo, — значит он и должен поставить проверенную версию сам.
    """
    stub = _machine_with(tmp_path, found="3.14",
                         available=("python3.13", "python3.13-venv", "python3.13-dev"))
    result = _install(repo_root, stub, tmp_path / "уст")
    out = result.stdout + result.stderr
    assert "Устанавливаем Python 3.13" in out, f"проверенная версия не ставится:\n{out[:900]}"
    assert "Дальше работаем на" in out, "поставили, но не переключились"
    assert "Python 3.13" in out.split("Дальше работаем на")[1].split("\n")[0], \
        "переключились не на ту версию"
    assert (stub / "python3.13").exists()


def test_installer_walks_down_to_an_available_version(repo_root: Path, tmp_path: Path):
    """Если 3.13 в репозитории нет, берём 3.12 — а не сдаёмся на первой."""
    stub = _machine_with(tmp_path, found="3.14",
                         available=("python3.12", "python3.12-venv", "python3.12-dev"))
    result = _install(repo_root, stub, tmp_path / "уст")
    out = result.stdout + result.stderr
    assert "Устанавливаем Python 3.12" in out, out[:900]
    assert "Python 3.12" in out.split("✓ Python:")[1].split("\n")[0], \
        "итоговый интерпретатор не тот"


def test_explicit_python_is_never_overridden(repo_root: Path, tmp_path: Path):
    """Указанный вручную интерпретатор — решение пользователя.

    Подменять его нельзя даже к лучшему: человек мог знать, что делает.
    """
    stub = _machine_with(tmp_path, found="3.14",
                         available=("python3.13", "python3.13-venv", "python3.13-dev"))
    result = _install(repo_root, stub, tmp_path / "уст",
                      "--python", str(stub / "python3"))
    combined = result.stdout + result.stderr
    assert "Устанавливаем Python" not in combined, "подменили заданный интерпретатор"
    assert "новее проверенной" in combined, "и даже не предупредили"


def test_python_install_can_be_declined(repo_root: Path, tmp_path: Path):
    """`--no-python-install` оставляет всё как было."""
    stub = _machine_with(tmp_path, found="3.14",
                         available=("python3.13", "python3.13-venv", "python3.13-dev"))
    result = _install(repo_root, stub, tmp_path / "уст", "--no-python-install")
    out = result.stdout + result.stderr
    assert "Устанавливаем Python" not in out
    assert "Python 3.14" in out.split("✓ Python:")[1].split("\n")[0], \
        "версию всё-таки подменили"


def test_supported_python_is_not_reinstalled(repo_root: Path, tmp_path: Path):
    """Ставить нечего, если подходящая версия уже есть — лишний apt никому не нужен."""
    stub = _machine_with(tmp_path, found="3.12",
                         available=("python3.13", "python3.13-venv", "python3.13-dev"))
    result = _install(repo_root, stub, tmp_path / "уст")
    out = result.stdout + result.stderr
    assert "Устанавливаем Python" not in out
    assert "новее проверенной" not in out


def test_helper_returns_only_a_path(repo_root: Path, tmp_path: Path):
    """Всё, кроме пути, обязано идти в stderr.

    Функция возвращает путь через stdout: строка «Устанавливаем Python…»,
    попав туда же, становится частью ответа — и вызывающий пытается её
    запустить.
    """
    stub = _machine_with(tmp_path, found="3.14",
                         available=("python3.13", "python3.13-venv", "python3.13-dev"))
    common = repo_root / "scripts" / "lib" / "common.sh"
    detect = repo_root / "scripts" / "lib" / "detect.sh"
    result = subprocess.run(
        [BASH, "-c", f'source "{common}"; source "{detect}"; install_supported_python 3.13'],
        env={"PATH": str(stub), "TERM": "dumb", "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=120)
    assert result.stdout.strip() == str(stub / "python3.13"), \
        f"в stdout попало лишнее: {result.stdout!r}"
    assert "Устанавливаем" in result.stderr, "сообщения пропали совсем"


# ---------------------------------------------------------------------------
# Окружение и токен
# ---------------------------------------------------------------------------


def test_venv_is_rebuilt_when_the_interpreter_changes(repo_root: Path):
    """Готовое окружение переиспользовалось, даже если собрано другой версией.

    Это сводило на нет весь выбор версии: установщик находил слишком новый
    Python, ставил рядом проверенный, писал «дальше работаем на 3.13» — и
    тут же брал venv, собранный на 3.14. Движки падали ровно как прежде,
    при том что в отчёте стояла правильная версия.
    """
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = text.split('step "Виртуальное окружение Python"', 1)[1][:2500]
    assert "VENV_PY_VERSION" in block, "версия существующего окружения не читается"
    assert '"${VENV_PY_VERSION}" != "${WANT_PY_VERSION}"' in block, \
        "версии не сравниваются"
    assert 'run rm -rf "${VENV}"' in block, "окружение не пересобирается"
    assert "повреждено" in block, "сломанное окружение молча переиспользуется"


def test_installer_accepts_a_hugging_face_token(repo_root: Path, tmp_path: Path):
    """Токен нельзя было задать при установке вовсе.

    Без него не скачиваются модели с ограниченным доступом: pyannote для
    диаризации требует его всегда. Приходилось дописывать env.sh руками уже
    после установки — то есть знать про этот файл.
    """
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "--hf-token)" in text and "--hf-token-file)" in text
    assert "HF_TOKEN_VALUE" in text

    # Токен обязан попасть в env.sh строкой ИМЯ=ЗНАЧЕНИЕ и с правами 0640:
    # systemd читает этот файл через EnvironmentFile, а в нём лежит секрет.
    common = repo_root / "scripts" / "lib" / "common.sh"
    env_file = tmp_path / "env.sh"
    script = f'''
      source "{common}"
      ENV_FILE="{env_file}"
      HF_TOKEN_VALUE="hf_секретное_значение_1234567890"
      write_file "${{ENV_FILE}}" 0640 <<'ENVEOF'
ASRHUB_PORT=8080
ENVEOF
      printf 'HF_TOKEN=%s\\n' "${{HF_TOKEN_VALUE}}" >> "${{ENV_FILE}}"
      chmod 0640 "${{ENV_FILE}}"
    '''
    run_bash(script)
    body = env_file.read_text(encoding="utf-8")
    assert "HF_TOKEN=hf_секретное_значение_1234567890" in body
    assert not body.startswith("export"), "systemd читает строго ИМЯ=ЗНАЧЕНИЕ"
    assert oct(env_file.stat().st_mode)[-3:] == "640", "секрет доступен на чтение всем"


def test_empty_token_does_not_overwrite_the_environment(repo_root: Path):
    """Пустое значение в env.sh перекрыло бы токен из окружения службы."""
    text = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = text.split("ASRHUB_ACCEL=${ACCEL}", 1)[1][:1200]
    assert 'if [[ -n "${HF_TOKEN_VALUE}" ]]; then' in block, \
        "токен пишется безусловно, даже пустой"


# ---------------------------------------------------------------------------
# Токен Hugging Face в мастере
# ---------------------------------------------------------------------------


def _wizard_token_block(repo_root: Path) -> str:
    """Настоящий кусок install.sh с вопросом про токен."""
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = re.search(r"(  # --- 4а\. Токен Hugging Face.*?\n)  # --- 5\.",
                      source, re.S)
    assert block, "в install.sh не найден шаг с вопросом про токен"
    return block.group(1)


def test_wizard_asks_for_the_token_and_says_when_it_is_required(repo_root: Path,
                                                                tmp_path: Path):
    """Токен спрашивается в мастере, и текст зависит от выбора диаризации.

    Раньше про токен не спрашивали вовсе: pyannote скачивался уже после
    установки и падал с «нужен токен» — на седьмом шаге, когда до конца
    оставалось меньше всего. Для диаризации он не «желателен», а обязателен,
    и вопрос обязан говорить именно это.
    """
    block = _wizard_token_block(repo_root)
    common = repo_root / "scripts" / "lib" / "common.sh"
    note_file = tmp_path / "пояснение.txt"

    def ask(extras: str, token: str = "") -> tuple[str, str]:
        script = f'''
          set -Eeuo pipefail
          source "{common}"
          # Подменяем сам вопрос: записываем то, с чем его позвали.
          wizard_ask() {{ printf '%s' "$5" > "{note_file}"; printf -v "$1" '%s' "ОТВЕТ"; }}
          run_step() {{
            local extras="{extras}"
            HF_TOKEN_VALUE="{token}"
{block}
            printf 'ТОКЕН=%s\\n' "${{HF_TOKEN_VALUE}}"
          }}
          run_step
        '''
        result = run_bash(script)
        assert result.returncode == 0, result.stderr
        note = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
        note_file.unlink(missing_ok=True)
        return result.stdout, note

    # С диаризацией — токен обязателен, и это сказано прямо.
    out, note = ask("service,diarization")
    assert "ТОКЕН=ОТВЕТ" in out, "вопрос не задан"
    assert "обязателен" in note
    assert "pyannote" in note

    # Без неё — вопрос остаётся, но с честным «можно пропустить».
    out, note = ask("service")
    assert "ТОКЕН=ОТВЕТ" in out, "вопрос не задан"
    assert "пропустить" in note
    assert "обязателен" not in note

    # Заданный ключом --hf-token не переспрашивается.
    out, note = ask("service,diarization", token="hf_ключ_из_командной_строки")
    assert "ТОКЕН=hf_ключ_из_командной_строки" in out, "мастер переспросил заданное"
    assert note == "", "мастер переспросил заданное"


def test_diarization_is_installed_as_an_engine(repo_root: Path):
    """Отмеченная галочка обязана дойти до списка движков.

    Вопрос про токен был бы бессмысленным, если бы сам pyannote не ставился:
    сначала галочки не существовало вовсе, и условие «выбрана диаризация»
    не срабатывало никогда.
    """
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    assert "diarization|Разделение по говорящим" in source, "нет пункта в мастере"

    block = re.search(r'(  # Диаризация ставится как обычный движок.*?\n  fi\n)',
                      source, re.S)
    assert block, "не найдено добавление движка"
    script = f'''
      set -Eeuo pipefail
      run_step() {{
        local extras="$1"
        ENGINES="faster_whisper,vosk"
{block.group(1)}
        printf '%s\\n' "${{ENGINES}}"
      }}
      run_step "service,diarization"
      run_step "service"
      ENGINES_TWICE() {{ :; }}
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.split()
    assert lines[0] == "faster_whisper,vosk,diarization"
    assert lines[1] == "faster_whisper,vosk", "движок добавился без галочки"

    # Файл требований должен существовать, иначе установка упадёт на шаге.
    assert (repo_root / "requirements" / "engines" / "diarization.txt").exists()


def test_token_reaches_the_configuration_the_server_reads(repo_root: Path,
                                                          tmp_path: Path):
    """Введённый токен обязан попасть туда, где сервер его найдёт.

    Он писался только в docker/.env, то есть при обычной установке молча
    пропадал: пользователь отвечал на вопрос, а диаризация всё равно падала
    с «нужен токен». Переменные окружения читает systemd, но не launchd и не
    запуск руками, а config.yaml читают все три.
    """
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = re.search(r'(if \[\[ -n "\$\{HF_TOKEN_VALUE\}" && "\$\{MODE\}" == "native" '
                      r'\]\]; then\n.*?\nfi\n)\n# Переменные окружения',
                      source, re.S)
    assert block, "в install.sh не найдена запись токена при обычной установке"

    config = tmp_path / "config.yaml"
    # Файл без перевода строки в конце — так его оставляет дописанный раздел.
    config.write_text(f'data_dir: {tmp_path}\nhf_token: "hf_старый"\n\nserver:\n'
                      '  server_port: 8080', encoding="utf-8")
    common = repo_root / "scripts" / "lib" / "common.sh"
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      CONFIG_FILE="{config}"
      MODE=native
      ASRHUB_DRY_RUN=0
      HF_TOKEN_VALUE=hf_новый12345678901234
{block.group(1)}
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr

    body = config.read_text(encoding="utf-8")
    assert body.count("hf_token:") == 1, "старая строка осталась — YAML возьмёт последнюю"
    assert "hf_старый" not in body

    from asrhub.config import load

    settings = load(config)
    assert settings.hf_token == "hf_новый12345678901234"
    assert settings.get("server_port") == 8080, "конфигурация перестала читаться"
    assert oct(config.stat().st_mode)[-3:] == "640", "в файле секрет"


def test_wizard_summary_survives_cyrillic_labels(repo_root: Path):
    """Сводка мастера падала на русских подписях.

    ${#переменная} в локали C считает БАЙТЫ: «Распознавание на лету» — это
    20 символов и 40 байт, ширина колонки 24, и выравнивание уходило в
    минус. Хуже того, функция заканчивалась на (( )), а ложное условие —
    это код возврата 1: под errexit мастер обрывался с «Сбой на шаге».
    """
    wizard = repo_root / "scripts" / "lib" / "wizard.sh"
    common = repo_root / "scripts" / "lib" / "common.sh"
    # Ширина 24 — подпись короче поля; ширина 10 — длиннее. Второй случай
    # важнее: именно на нём (( )) оказывалось ложным и возвращало единицу.
    for locale in ("C", "C.UTF-8", "ru_RU.UTF-8"):
        for width, expected in ((24, 25), (10, 22)):
            script = f'''
              set -Eeuo pipefail
              source "{common}"; source "{wizard}"
              out="$(wizard_pad "Распознавание на лету" {width})x"
              printf '%s\\n' "${{out}}"
            '''
            result = run_bash(script, env={"LC_ALL": locale, "LANG": locale})
            assert result.returncode == 0, \
                f"локаль {locale}, ширина {width}: {result.stderr}"
            line = result.stdout.rstrip("\n")
            assert line.endswith("x")
            # Считаем символы, а не байты: поле плюс наш «x».
            assert len(line) == expected, \
                f"локаль {locale}, ширина {width}: получилось {len(line)} знаков"


# ---------------------------------------------------------------------------
# То же самое на Windows
# ---------------------------------------------------------------------------

PWSH = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(PWSH is None, reason="PowerShell недоступен")
def test_windows_token_check_matches_the_linux_one(repo_root: Path):
    """Один и тот же токен обязан приниматься на обеих системах.

    Правило живёт в двух местах — wizard_valid_hf_token и Test-HfToken, — и
    разойтись им проще простого: тогда токен, принятый на сервере, мастер на
    Windows отвергал бы как «неправильный», и объяснить это было бы нечем.
    """
    cases = ["", "hf_" + "a" * 16, "hf_" + "a" * 34, "hf_короткий",
             "sk-совсем-другой-ключ", "a" * 40]
    module = repo_root / "scripts" / "lib" / "Common.psm1"
    script = (f'Import-Module "{module}" -Force; ' +
              "; ".join(f'(Test-HfToken -Token "{case}") | ForEach-Object {{ "$_" }}'
                        for case in cases))
    result = subprocess.run([PWSH, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    windows = [line for line in result.stdout.splitlines()
               if line.strip() in ("True", "False")]

    common = repo_root / "scripts" / "lib" / "common.sh"
    wizard = repo_root / "scripts" / "lib" / "wizard.sh"
    checks = "\n".join(
        f'if wizard_valid_hf_token "{case}" >/dev/null 2>&1; '
        f'then echo True; else echo False; fi' for case in cases)
    linux = run_bash(f'source "{common}"; source "{wizard}"\n{checks}')
    assert linux.returncode == 0, linux.stderr

    assert windows == linux.stdout.split(), \
        f"проверки разошлись: Windows {windows}, Linux {linux.stdout.split()}"
    # И сама проверка что-то значит: пустая строка принимается, мусор — нет.
    assert windows[0] == "True" and windows[-1] == "False"


@pytest.mark.skipif(PWSH is None, reason="PowerShell недоступен")
def test_windows_installer_asks_for_the_token_too(repo_root: Path):
    """Мастер на Windows обязан спрашивать то же, что и на Linux.

    Скрипты — двойники, и расхождение обнаруживается только на чужой машине:
    на сервере токен спросили, на ноутбуке — нет, и диаризация не работает
    ровно у того, кто ставил её на Windows.
    """
    text = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    assert "[string]$HfToken" in text and "[string]$HfTokenFile" in text
    assert "Разделение по говорящим (pyannote)" in text, "нет пункта в мастере"
    assert "Токен Hugging Face" in text, "не спрашивается токен"
    assert "Test-HfToken" in text, "ввод не проверяется"
    assert "hf_token: \"{0}\"" in text, "токен не доходит до конфигурации"

    # Разбор файла целиком: опечатка в PowerShell видна только при разборе,
    # а запустить install.ps1 вне Windows нельзя.
    checked = subprocess.run(
        [PWSH, "-NoProfile", "-Command",
         "$e = $null; $null = [System.Management.Automation.Language.Parser]"
         f"::ParseFile('{repo_root / 'scripts' / 'install.ps1'}', [ref]$null, [ref]$e); "
         "if ($e -and $e.Count) { $e | ForEach-Object { $_.Message }; exit 1 }"],
        capture_output=True, text=True, timeout=120)
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_example_configuration_never_carries_a_real_token(repo_root: Path):
    """config.example.yaml раздаётся с правами 0644 — секрета в нём быть не может.

    Пример пишется командой `--print-config` при установке, и если бы он
    печатал действующее значение, токен утекал бы в файл, который читают все.
    """
    import os as _os

    env = {**_os.environ, "ASRHUB_HF_TOKEN": "hf_реальный1234567890"}
    result = subprocess.run(
        ["python3", "-m", "asrhub", "--print-config"],
        cwd=str(repo_root / "server"), env=env,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert "hf_реальный1234567890" not in result.stdout
    assert "# hf_token:" in result.stdout, "в примере нет самой строки про токен"


@pytest.mark.skipif(PWSH is None, reason="PowerShell недоступен")
def test_windows_token_replaces_the_old_line_too(repo_root: Path, tmp_path: Path):
    """Windows-установщик обязан заменять строку, а не дописывать вторую.

    Два ключа `hf_token` в YAML — это молча выигравший последний. Если
    install.ps1 просто дописывал бы строку, повторная установка возвращала
    бы старый токен, и понять это по файлу было бы невозможно.
    """
    source = (repo_root / "scripts" / "install.ps1").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^if \(\$HfToken -and \$Mode -eq 'native'\) \{.*?^\}$", source)
    assert block, "в install.ps1 не найдена запись токена в конфигурацию"

    config = tmp_path / "config.yaml"
    config.write_text(f'data_dir: {tmp_path}\nhf_token: "hf_старый"\n\nserver:\n'
                      '  server_port: 8080\n', encoding="utf-8")
    piece = tmp_path / "block.ps1"
    piece.write_text(block.group(0), encoding="utf-8")

    script = (f'Import-Module "{repo_root / "scripts" / "lib" / "Common.psm1"}" -Force; '
              '$HfToken = "hf_новый12345678901234"; $Mode = "native"; '
              f'$configFile = "{config}"; . "{piece}"')
    result = subprocess.run([PWSH, "-NoProfile", "-Command", script],
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr

    body = config.read_text(encoding="utf-8")
    assert body.count("hf_token:") == 1, "старая строка осталась"
    assert "hf_старый" not in body

    from asrhub.config import load

    assert load(config).hf_token == "hf_новый12345678901234"
    assert load(config).get("server_port") == 8080


# ---------------------------------------------------------------------------
# Движки с прибитыми гвоздями зависимостями
# ---------------------------------------------------------------------------


def test_gigaam_installs_without_resolving_its_hard_pins(repo_root: Path):
    """GigaAM не ставился вовсе — из-за версий, прибитых в его pyproject.

    `onnxruntime==1.23.*` и `onnx==1.19.*` не имеют колёс под Python 3.14, и
    pip обрывался с ResolutionImpossible ещё до загрузки самого пакета:
    «no matching distributions available for your environment: onnxruntime».
    При этом для распознавания ни onnx, ни конкретная версия onnxruntime не
    нужны — они только для экспорта модели в ONNX.
    """
    engines = repo_root / "requirements" / "engines"
    main = (engines / "gigaam.txt").read_text(encoding="utf-8")
    companion = engines / "no-deps" / "gigaam.txt"
    assert companion.exists(), "нет файла для установки без зависимостей"

    assert "git+https://github.com/salute-developers/GigaAM" in \
        companion.read_text(encoding="utf-8")
    assert "git+" not in main, "пакет остался там, где pip разрешает зависимости"
    # Зависимости перечислены своими руками — иначе --no-deps оставит движок
    # без hydra, omegaconf и остального, и он не импортируется.
    for package in ("hydra-core", "omegaconf", "sentencepiece", "soundfile",
                    "tqdm", "numpy", "onnxruntime"):
        assert package in main, f"в требованиях нет {package}"

    # Спутник обязан лежать в подкаталоге: update.sh перебирает engines/*.txt
    # и принял бы соседний файл за отдельный движок.
    assert companion.parent.name == "no-deps"
    assert not list(engines.glob("*.no-deps.txt"))


def test_companion_file_is_installed_without_dependencies(repo_root: Path,
                                                          tmp_path: Path):
    """Спутник ставится с --no-deps, и только он.

    Обратное — `--no-deps` на весь файл требований — оставило бы без
    зависимостей и hydra-core с omegaconf, то есть движок бы не заработал.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    engines = tmp_path / "engines"
    (engines / "no-deps").mkdir(parents=True)
    (engines / "движок.txt").write_text("пакет-зависимость\n", encoding="utf-8")
    (engines / "no-deps" / "движок.txt").write_text("пакет-без-зависимостей\n",
                                                    encoding="utf-8")
    calls = tmp_path / "вызовы.txt"
    fake_pip = tmp_path / "pip"
    fake_pip.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{calls}"\n',
                        encoding="utf-8")
    fake_pip.chmod(0o755)

    script = f'''
      set -Eeuo pipefail
      source "{common}"
      install_engine_requirements "{fake_pip}" "{engines}/движок.txt" --флаг
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    lines = calls.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, f"вызовов pip: {lines}"
    assert "--no-deps" not in lines[0], "зависимости движка не установлены"
    assert lines[0].endswith("движок.txt")
    assert "--no-deps" in lines[1] and lines[1].endswith("no-deps/движок.txt")
    assert lines.index(lines[0]) < lines.index(lines[1]), \
        "пакет ставится раньше своих зависимостей"

    # Без спутника — ровно один вызов: остальные движки не должны меняться.
    calls.unlink()
    (engines / "no-deps" / "движок.txt").unlink()
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_engine_from_a_repository_is_recognised_as_installed(repo_root: Path,
                                                             tmp_path: Path):
    """Движок, поставленный из репозитория, выглядел неустановленным.

    Имя пакета вырезалось по `<>=!;[`, а прямая ссылка PEP 508 выглядит как
    «gigaam @ git+https://…»: именем становилась вся строка с адресом. Такой
    движок не обновлялся никогда — `update.sh` его просто не видел.
    """
    source = (repo_root / "scripts" / "update.sh").read_text(encoding="utf-8")
    body = re.search(r"(engine_installed\(\) \{\n.*?\n\}\n)", source, re.S)
    assert body, "не найдена функция engine_installed"

    engines = tmp_path / "engines"
    (engines / "no-deps").mkdir(parents=True)
    (engines / "движок.txt").write_text("посторонний-пакет>=1\n", encoding="utf-8")
    (engines / "no-deps" / "движок.txt").write_text(
        "нужный-пакет @ git+https://example.invalid/пакет.git\n", encoding="utf-8")

    # Макет pip: знает ровно один пакет — тот, что стоит из репозитория.
    fake_pip = tmp_path / "pip"
    fake_pip.write_text('#!/usr/bin/env bash\n'
                        '[[ "$1" == "show" && "$2" == "нужный-пакет" ]] && exit 0\n'
                        'exit 1\n', encoding="utf-8")
    fake_pip.chmod(0o755)

    script = f'''
      set -Eeuo pipefail
      VPIP="{fake_pip}"
{body.group(1)}
      if engine_installed "{engines}/движок.txt"; then echo НАЙДЕН; else echo НЕТ; fi
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["НАЙДЕН"]


# ---------------------------------------------------------------------------
# Обработка ошибок установщика
# ---------------------------------------------------------------------------


def _diagnose(repo_root: Path, tmp_path: Path, text: str, *, offline: str = "0"):
    """Прогоняет разбор вывода pip на настоящем тексте ошибки."""
    capture = tmp_path / "вывод.txt"
    capture.write_text(text, encoding="utf-8")
    common = repo_root / "scripts" / "lib" / "common.sh"
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      if diagnose_pip_failure "{capture}" "" "{offline}"; then :; else echo НЕ_ОПОЗНАНА; fi
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    return result.stdout + result.stderr


def test_pip_failure_is_explained_in_one_line(repo_root: Path, tmp_path: Path):
    """Сто строк вывода pip заменяются одной строкой о причине.

    Установка движка обрывалась словами «установка не удалась — сервер
    запустится без него», и почему — пользователь должен был выяснять сам по
    трассировке pip. Тексты здесь — настоящие, снятые с неудавшихся запусков.
    """
    # Прибитая версия, которой нет под этот Python: так не ставился GigaAM.
    text = _diagnose(repo_root, tmp_path, """
ERROR: Cannot install -r gigaam.txt (line 9) and onnxruntime>=1.17 because these package versions have conflicting dependencies.

The conflict is caused by:
    The user requested onnxruntime>=1.17
    gigaam 0.2.0 depends on onnxruntime==1.23.*

Additionally, some packages in these conflicts have no matching distributions available for your environment:
    onnxruntime

ERROR: ResolutionImpossible: for help visit https://pip.pypa.io/
""")
    assert "прибиты к версии" in text
    assert "onnxruntime" in text, "не названа виновная библиотека"
    assert "--python" in text, "нет команды, которая это лечит"

    # Нет колеса вообще.
    text = _diagnose(repo_root, tmp_path, """
ERROR: Could not find a version that satisfies the requirement onnxruntime==1.23.0 (from versions: 1.24.1, 1.29.0)
ERROR: No matching distribution found for onnxruntime==1.23.0
""")
    assert "нет подходящей версии" in text and "onnxruntime==1.23.0" in text

    # Прокси. Настоящий вывод: pip повторяет попытки и заканчивает теми же
    # словами «No matching distribution found» — сетевую причину надо узнать
    # раньше, иначе совет будет про версию Python.
    text = _diagnose(repo_root, tmp_path, """
WARNING: Retrying (Retry(total=4)) after connection broken by 'OSError('Tunnel connection failed: 403 Forbidden')': /simple/requests/
ERROR: Could not find a version that satisfies the requirement requests (from versions: none)
ERROR: No matching distribution found for requests
""")
    assert "Прокси" in text, "сетевая причина принята за отсутствие версии"
    assert "https_proxy" in text

    # Автономный режим выглядит точно так же — и тоже не про версию Python.
    text = _diagnose(repo_root, tmp_path, """
ERROR: Could not find a version that satisfies the requirement fastapi (from versions: none)
ERROR: No matching distribution found for fastapi
""", offline="1")
    assert "Автономный режим" in text and "кеш" in text

    # Остальные распознаваемые причины.
    for sample, expected in (
        ("OSError: [Errno 28] No space left on device", "место на диске"),
        ("ERROR: Failed building wheel for onnx\nPython.h: No such file", "собрать из исходников"),
        ("error: externally-managed-environment", "защищён от изменений"),
        ("urllib3.exceptions.SSLError: CERTIFICATE_VERIFY_FAILED", "сертификат"),
        ("socket.gaierror: [Errno -3] Temporary failure in name resolution", "DNS"),
    ):
        assert expected in _diagnose(repo_root, tmp_path, sample), sample

    # И наоборот: незнакомый текст не выдумывает причину.
    assert "НЕ_ОПОЗНАНА" in _diagnose(repo_root, tmp_path, "что-то совершенно неожиданное")


def test_second_installer_does_not_run_in_the_same_directory(repo_root: Path,
                                                             tmp_path: Path):
    """Две установки в один каталог ломают окружение друг друга.

    Второй установщик сносит venv, пока первый в него ставит пакеты. На
    выходе — установка, где половина движков есть, а половины нет, причём в
    журнале обеих всё успешно.
    """
    common = repo_root / "scripts" / "lib" / "common.sh"
    target = tmp_path / "установка"
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      enable_error_handling
      acquire_install_lock "{target}" || exit 9
      # Второй установщик — отдельный процесс, как оно и бывает.
      bash -c '
        set -Eeuo pipefail
        source "{common}"
        acquire_install_lock "{target}" && echo ВТОРОЙ_ВЗЯЛ || echo ВТОРОЙ_ОТКАЗ
      '
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert "ВТОРОЙ_ОТКАЗ" in result.stdout
    assert "уже идёт установка" in result.stderr
    # Замок снимается сам: иначе прерванная установка блокировала бы каталог
    # навсегда, и человеку пришлось бы удалять файл руками.
    assert not (target / ".asrhub-install.lock").exists()


def test_lock_left_by_a_dead_installer_is_removed(repo_root: Path, tmp_path: Path):
    """Замок прерванной установки не должен блокировать каталог навсегда."""
    common = repo_root / "scripts" / "lib" / "common.sh"
    target = tmp_path / "установка"
    target.mkdir()
    # Номер процесса, которого заведомо нет.
    (target / ".asrhub-install.lock").write_text("999999\n", encoding="utf-8")
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      acquire_install_lock "{target}" && echo ВЗЯЛ
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    assert "ВЗЯЛ" in result.stdout
    assert "прерванной установки" in result.stderr


def test_failure_tells_how_to_repeat_without_leaking_the_token(repo_root: Path):
    """После сбоя нужно повторить установку — теми же ключами.

    И строка запуска попадает не только на экран, но и в журнал, поэтому
    токен Hugging Face в ней обязан быть замаскирован.
    """
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = re.search(r"(ASRHUB_INVOCATION=\"bash \$0\"\n.*?\nexport ASRHUB_INVOCATION\n)",
                      source, re.S)
    assert block, "строка запуска не собирается"
    script = f'''
      set -Eeuo pipefail
      собрать() {{
{block.group(1)}
        printf '%s\\n' "${{ASRHUB_INVOCATION}}"
      }}
      собрать --profile light --hf-token hf_секрет1234567890 --yes
    '''
    result = run_bash(script)
    assert result.returncode == 0, result.stderr
    line = result.stdout.strip()
    assert "--profile light" in line and "--yes" in line
    assert "hf_секрет1234567890" not in line, "токен утёк в сообщение и в журнал"
    assert "hf_…" in line

    # И сама подсказка существует.
    common = (repo_root / "scripts" / "lib" / "common.sh").read_text(encoding="utf-8")
    assert "Повторить после исправления" in common


def _preflight_block(repo_root: Path, marker: str) -> str:
    """Достаёт из install.sh настоящий кусок предварительной проверки."""
    source = (repo_root / "scripts" / "install.sh").read_text(encoding="utf-8")
    block = re.search(marker, source, re.S)
    assert block, f"не найден блок проверки: {marker}"
    return block.group(1)


def test_missing_git_is_named_before_the_engine_step(repo_root: Path, tmp_path: Path):
    """Движок из репозитория без git падает на девятом шаге из десяти.

    pip доходит до клонирования и обрывается — после того, как окружение
    собрано, torch выкачан и полчаса потрачено. Проверить наличие git стоит
    доли секунды, и делать это надо до всего.
    """
    block = _preflight_block(
        repo_root, r'(if \[\[ "\$\{MODE\}" == "native" && "\$\{OFFLINE\}" -eq 0 \]\]; then\n'
                   r'  _needs_git=.*?\n  unset _needs_git _engine _engine_list _nodeps\nfi\n)')
    common = repo_root / "scripts" / "lib" / "common.sh"

    def probe(*, git_present: bool, install_works: bool) -> tuple[int, str]:
        script = f'''
          set -Eeuo pipefail
          source "{common}"
          MODE=native
          OFFLINE=0
          ENGINES="faster_whisper,gigaam"
          REPO_DIR="{repo_root}"
          have() {{ [[ "$1" == "git" ]] && return {0 if git_present else 1}; command -v "$1" >/dev/null 2>&1; }}
          install_system_packages() {{ return {0 if install_works else 1}; }}
{block}
          echo ДОШЛИ_ДО_КОНЦА
        '''
        result = run_bash(script)
        return result.returncode, result.stdout + result.stderr

    # git есть — проверка молчит и не мешает.
    code, text = probe(git_present=True, install_works=True)
    assert code == 0 and "ДОШЛИ_ДО_КОНЦА" in text
    assert "git" not in text.lower() or "ставятся из репозитория" not in text

    # git нет, но ставится — предупреждение и продолжение.
    code, text = probe(git_present=False, install_works=True)
    assert code == 0 and "ДОШЛИ_ДО_КОНЦА" in text
    assert "ставятся из репозитория" in text and "gigaam" in text

    # git нет и не ставится — остановка здесь, а не на девятом шаге.
    code, text = probe(git_present=False, install_works=False)
    assert code == 127, text
    assert "ДОШЛИ_ДО_КОНЦА" not in text
    assert "apt install git" in text


def test_directory_that_cannot_run_programs_is_refused(repo_root: Path,
                                                       tmp_path: Path):
    """Раздел с noexec: venv соберётся, а python из него не запустится.

    Ошибка приходит уже от pip — «Permission denied» на собственный
    интерпретатор, — и по ней не догадаться, что дело в способе монтирования.
    """
    block = _preflight_block(
        repo_root, r'(if \[\[ "\$\{MODE\}" == "native" && "\$\{ASRHUB_DRY_RUN\}" != "1" \]\]; then\n'
                   r'  _exec_probe=.*?\n  unset _exec_probe\nfi\n)')
    common = repo_root / "scripts" / "lib" / "common.sh"
    target = tmp_path / "установка"
    target.mkdir()

    # Обычный каталог: проверка обязана пропустить.
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      MODE=native; ASRHUB_DRY_RUN=0; PREFIX="{target}"
{block}
      echo ПРОПУЩЕНО
    '''
    result = run_bash(script)
    assert result.returncode == 0 and "ПРОПУЩЕНО" in result.stdout, result.stderr
    assert not list(target.glob(".asrhub-exec.*")), "проба осталась на диске"

    # Каталог, где программы не запускаются: подменяем chmod, чтобы пробный
    # файл остался без права на запуск — это ровно то, что делает noexec.
    script = f'''
      set -Eeuo pipefail
      source "{common}"
      MODE=native; ASRHUB_DRY_RUN=0; PREFIX="{target}"
      chmod() {{ return 0; }}
{block}
      echo ПРОПУЩЕНО
    '''
    result = run_bash(script)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "ПРОПУЩЕНО" not in result.stdout
    assert "noexec" in result.stderr
    assert not list(target.glob(".asrhub-exec.*")), "проба осталась после отказа"
