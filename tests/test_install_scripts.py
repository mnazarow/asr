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
