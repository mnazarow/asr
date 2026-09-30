"""Заход 47: сценарии установки и обслуживания, клиент, агент, Docker.

Главное, что здесь проверяется: сценарии узнают свою установку. На машине с
двумя установками команда удаления из «Что дальше» второй удаляла первую —
её каталог, базу, модели и службу, — обновление копировало код второй
поверх первой, а установщик без `--name` переписывал юнит первой. Копия
базы перед удалением теряла всё, что лежало в журнале WAL. Остальное —
по находкам сплошной проверки захода 40: удаление движка, проба видеокарты
от root, клиент asrctl, агент на станции.

Проверки идут настоящими сценариями над временными каталогами: системные
команды (systemctl, pgrep и прочие) подменены заглушками, каталоги юнитов —
переменными ASRHUB_SYSTEMD_DIR, чтобы ничего не трогать в /etc.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from test_review_40_scripts import КОРЕНЬ, _заглушки, _поддельный_python, прогнать

pytestmark = pytest.mark.skipif(os.name != "posix", reason="bash-сценарии")

ВЕРСИЯ = (КОРЕНЬ / "VERSION").read_text(encoding="utf-8").strip()


def _установка_на_месте(корень: Path, имя: str, данные: Path, *, служба: str = "",
                         сервер: bool = False) -> Path:
    """Каталог установки: копия scripts, server, VERSION и метка установки."""
    каталог = корень / имя
    shutil.copytree(КОРЕНЬ / "scripts", каталог / "scripts")
    if сервер:
        (каталог / "server").symlink_to(КОРЕНЬ / "server")
    else:
        (каталог / "server" / "asrhub").mkdir(parents=True)
    (каталог / "VERSION").write_text(ВЕРСИЯ + "\n", encoding="utf-8")
    (каталог / ".asrhub-install").write_text(
        f"prefix={каталог}\ndata_dir={данные}\nservice={служба}\nmode=native\n"
        f"version={ВЕРСИЯ}\n", encoding="utf-8")
    данные.mkdir(parents=True, exist_ok=True)
    return каталог


def _юнит(каталог_юнитов: Path, имя: str, программа: Path, данные: Path) -> Path:
    каталог_юнитов.mkdir(parents=True, exist_ok=True)
    путь = каталог_юнитов / f"{имя}.service"
    путь.write_text(
        "[Service]\nUser=asrhub\n"
        f"WorkingDirectory={программа}/server\n"
        f'Environment="ASRHUB_DATA_DIR={данные}"\n'
        f'ExecStart="{программа}/venv/bin/python" -m asrhub --host 127.0.0.1 --port 8090\n',
        encoding="utf-8")
    return путь


def _окружение(tmp_path: Path, журнал: Path, **ещё: str) -> dict[str, str]:
    заглушки = _заглушки(tmp_path / "bin", журнал)
    (tmp_path / "home").mkdir(exist_ok=True)
    return {"PATH": f"{заглушки}:{os.environ['PATH']}", "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path), "ASRHUB_SYSTEMD_DIR": str(tmp_path / "units"),
            "ASRHUB_SYSTEMD_USER_DIR": str(tmp_path / "units-user"),
            "ASRHUB_LAUNCHAGENTS_DIR": str(tmp_path / "agents"), **ещё}


# ---------------------------------------------------------------------------
# Какая это установка
# ---------------------------------------------------------------------------


def test_удаление_второй_установки_не_трогает_первую(tmp_path: Path):
    """Команда удаления из «Что дальше» второй установки удаляла первую.

    Установка искалась по списку стандартных каталогов: `bash
    /opt/asrhub2/scripts/uninstall.sh --purge` сносил /opt/asrhub,
    /var/lib/asrhub с базой и моделями и службу asrhub, а вторая
    оставалась. Теперь установка — та, из которой запущен сценарий.
    """
    журнал = tmp_path / "вызовы.log"
    первая = _установка_на_месте(tmp_path / "opt", "asrhub", tmp_path / "данные1",
                                 служба="asrhub")
    вторая = _установка_на_месте(tmp_path / "opt", "asrhub2", tmp_path / "данные2",
                                 служба="asrhub2")
    итог = прогнать(f'bash "{вторая}/scripts/uninstall.sh" --purge --dry-run --yes',
                    env=_окружение(tmp_path, журнал))
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 0, вывод[-3000:]
    assert f"rm -rf {вторая}" in вывод, вывод[-3000:]
    assert str(tmp_path / "данные2") in вывод
    assert str(tmp_path / "данные1") not in вывод and f"rm -rf {первая}\n" not in вывод
    assert "asrhub2 — автозапуск будет удалён" in вывод
    assert "asrhub2.service" in вывод
    assert "disable --now asrhub.service" not in вывод


def test_из_дистрибутива_при_двух_установках_спрашивается_каталог(tmp_path: Path):
    """Не из установки и установок две — удалять и обновлять наугад нельзя."""
    журнал = tmp_path / "вызовы.log"
    for имя in ("asrhub", "asrhub2"):
        программа = tmp_path / "opt" / имя
        (программа / "server").mkdir(parents=True)
        (программа / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
        _юнит(tmp_path / "units", имя, программа, tmp_path / f"данные-{имя}")
    окружение = _окружение(tmp_path, журнал)
    for сценарий, ключи in (("uninstall.sh", "--purge --yes"), ("update.sh", "--check --yes"),
                            ("doctor.sh", "")):
        итог = прогнать(f'bash "{КОРЕНЬ}/scripts/{сценарий}" {ключи}', env=окружение)
        вывод = итог.stdout + итог.stderr
        assert итог.returncode == 2, f"{сценарий}: {вывод[-2000:]}"
        assert "несколько установок" in вывод, вывод[-2000:]
        assert str(tmp_path / "opt" / "asrhub2") in вывод


def test_установка_без_метки_узнаётся_по_своей_службе(tmp_path: Path):
    """Поставленные до 3.1.19 установки метки не имеют — их узнаёт служба."""
    программа = tmp_path / "opt" / "вторая"
    (программа / "server").mkdir(parents=True)
    (программа / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
    _юнит(tmp_path / "units", "asrhub-вторая", программа, tmp_path / "данные-второй")
    # И соседняя установка со своей службой: единственная известная на машине
    # нашлась бы и без узнавания себя — а так сценарий обязан узнать именно
    # себя, иначе установок две и какую брать, не сказано.
    соседняя = tmp_path / "opt" / "первая"
    (соседняя / "server").mkdir(parents=True)
    _юнит(tmp_path / "units", "asrhub", соседняя, tmp_path / "данные-первой")
    итог = прогнать(f'''
      export ASRHUB_SYSTEMD_DIR="{tmp_path / "units"}" ASRHUB_SYSTEMD_USER_DIR="{tmp_path}/нет"
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      PREFIX="" DATA_DIR="" SERVICE_NAME=""
      mkdir -p "{программа}/scripts"
      resolve_installation "{программа}/scripts"
      printf 'prefix=%s\\ndata=%s\\nsource=%s\\nservice=%s\\n' \\
        "$PREFIX" "$DATA_DIR" "$ASRHUB_DATA_DIR_SOURCE" "$SERVICE_NAME"
    ''')
    assert f"prefix={программа}\n" in итог.stdout, итог.stdout + итог.stderr
    assert f"data={tmp_path / 'данные-второй'}\n" in итог.stdout
    assert "source=service" in итог.stdout
    assert "service=asrhub-вторая" in итог.stdout


def test_удаление_без_подтверждённого_каталога_данных_требует_его_явно(tmp_path: Path):
    """--purge с каталогом данных, найденным по стандартному пути, — не вслепую."""
    журнал = tmp_path / "вызовы.log"
    программа = tmp_path / "prog"
    (программа / "server").mkdir(parents=True)
    (программа / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
    дом = tmp_path / "home"
    (дом / ".local" / "share" / "asrhub").mkdir(parents=True)
    # Пробный запуск: если защита сломается, сценарий только напишет, что
    # удалил бы, — а не удалит чужой каталог на машине, где идут проверки.
    итог = прогнать(f'bash "{КОРЕНЬ}/scripts/uninstall.sh" --prefix "{программа}" '
                    '--purge --yes --dry-run', env=_окружение(tmp_path, журнал))
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 2, вывод[-2000:]
    assert "--data" in вывод
    assert (дом / ".local" / "share" / "asrhub").exists()


# ---------------------------------------------------------------------------
# Копия базы, служба, восстановление
# ---------------------------------------------------------------------------


ДЕРЖАТЕЛЬ_БАЗЫ = """
import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1])
c.execute("PRAGMA journal_mode=WAL")
c.execute("PRAGMA wal_autocheckpoint=0")
c.execute("CREATE TABLE jobs (id INTEGER)")
c.executemany("INSERT INTO jobs VALUES (?)", [(i,) for i in range(200)])
c.commit()
open(sys.argv[2], "w").write("готово")
time.sleep(60)
"""


def test_копия_базы_при_удалении_не_теряет_журнал_wal(tmp_path: Path):
    """Копия asrhub.db обычным cp — без журнала WAL, где лежат задания.

    На проверке в такой копии не нашлось даже таблицы jobs, а удаление с
    --purge тут же стирало и журнал. Теперь копия снимается копированием
    SQLite и после остановки службы.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка_на_месте(tmp_path, "prog", данные)
    готово = tmp_path / "готово"
    держатель = subprocess.Popen([sys.executable, "-c", ДЕРЖАТЕЛЬ_БАЗЫ,
                                  str(данные / "asrhub.db"), str(готово)])
    try:
        for _ in range(100):
            if готово.exists():
                break
            time.sleep(0.1)
        assert (данные / "asrhub.db-wal").exists(), "журнал WAL не создан — проверка не о том"
        итог = прогнать(f'bash "{программа}/scripts/uninstall.sh" --yes',
                        env=_окружение(tmp_path, журнал))
    finally:
        держатель.kill()
        держатель.wait(timeout=10)
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    копии = list((tmp_path / "home").glob("asrhub-backup-*/asrhub.db"))
    assert копии, итог.stdout[-2000:]
    строк = sqlite3.connect(копии[0]).execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    assert строк == 200


def test_копия_службой_делается_из_любого_каталога(tmp_path: Path):
    """`service.sh backup` падал на «No module named 'asrhub'» вне server/."""
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка_на_месте(tmp_path, "prog", данные, сервер=True)
    (программа / "venv" / "bin").mkdir(parents=True)
    (программа / "venv" / "bin" / "python").symlink_to(sys.executable)
    итог = subprocess.run(
        ["bash", str(программа / "scripts" / "service.sh"), "backup",
         "--prefix", str(программа), "--data", str(данные)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=300,
        env={**os.environ, **_окружение(tmp_path, журнал), "ASRHUB_NO_COLOR": "1"})
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert "Копия:" in итог.stdout
    assert list((данные / "backups").glob("asrhub-*.db"))


def test_восстановление_не_гасит_службу_при_неисправном_окружении(tmp_path: Path):
    """Раньше служба останавливалась, а потом сценарий падал на импорте.

    Пакет из PYTHONPATH того, кто запустил сценарий, сломанную установку
    прикрывать не должен: служба работает без него. Раньше проверка брала
    и его — и при исправном пакете в окружении (у разработчика, у другой
    версии) проходила, служба гасилась, а восстановление шло чужим кодом.
    """
    журнал = tmp_path / "вызовы.log"
    данные = tmp_path / "данные"
    программа = _установка_на_месте(tmp_path, "prog", данные, служба="asrhub-проба-47")
    (программа / "venv" / "bin").mkdir(parents=True)
    (программа / "venv" / "bin" / "python").symlink_to(sys.executable)
    копия = tmp_path / "копия.db"
    sqlite3.connect(копия).close()
    итог = прогнать(f'bash "{программа}/scripts/service.sh" restore --from "{копия}"',
                    env=_окружение(tmp_path, журнал, PYTHONPATH=str(КОРЕНЬ / "server")))
    assert итог.returncode == 1, итог.stdout + итог.stderr
    assert "не загружается" in итог.stdout + итог.stderr
    вызовы = журнал.read_text(encoding="utf-8") if журнал.exists() else ""
    assert "stop" not in вызовы, вызовы


def test_пробный_импорт_после_сбоя_не_берёт_чужой_пакет(tmp_path: Path):
    """Разбор незапустившегося сервера отвечал «пакет импортируется».

    Пробный импорт брал и PYTHONPATH вызывающего, и пакет другой версии из
    окружения прикрывал сломанную установку — разбор уводил от настоящей
    причины («дело не в зависимостях»).
    """
    исходники = tmp_path / "server"
    (исходники / "asrhub").mkdir(parents=True)
    сценарий = f'''
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      if import_probe "{sys.executable}" "{исходники}"; then echo "ИМПОРТ=да"; else echo "ИМПОРТ=нет"; fi
    '''
    итог = subprocess.run(["bash", "-c", сценарий], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONPATH": str(КОРЕНЬ / "server"),
                               "ASRHUB_NO_COLOR": "1", "TMPDIR": str(tmp_path)})
    assert "ИМПОРТ=нет" in итог.stdout, итог.stdout + итог.stderr


def test_у_установки_без_службы_не_трогается_чужая(tmp_path: Path):
    """Установка без службы: «asrhub» по умолчанию было бы службой первой."""
    журнал = tmp_path / "вызовы.log"
    программа = _установка_на_месте(tmp_path, "prog", tmp_path / "данные", служба="")
    итог = прогнать(f'bash "{программа}/scripts/service.sh" stop',
                    env=_окружение(tmp_path, журнал))
    assert итог.returncode == 1, итог.stdout + итог.stderr
    assert "нет службы" in итог.stdout + итог.stderr
    вызовы = журнал.read_text(encoding="utf-8") if журнал.exists() else ""
    assert "stop" not in вызовы


# ---------------------------------------------------------------------------
# Установщик
# ---------------------------------------------------------------------------


def _установщик(tmp_path: Path, *ключи: str, службу: bool = False) -> subprocess.CompletedProcess:
    журнал = tmp_path / "вызовы.log"
    окружение = _окружение(tmp_path, журнал)
    python = _поддельный_python(tmp_path / "bin", журнал)
    return прогнать(
        f'bash "{КОРЕНЬ / "scripts" / "install.sh"}" --yes --skip-models '
        f'--gpu-driver none --no-interactive --offline --python "{python}" '
        + ("" if службу else "--no-service ") + " ".join(ключи),
        env=окружение, timeout=600)


def test_вторая_установка_без_имени_не_переписывает_чужую_службу(tmp_path: Path):
    """Без --name вторая установка переписывала юнит первой на свой каталог."""
    юнит = _юнит(tmp_path / "units", "asrhub", tmp_path / "opt" / "первая",
                 tmp_path / "данные1")
    было = юнит.read_text(encoding="utf-8")
    итог = _установщик(tmp_path, f'--prefix "{tmp_path / "opt" / "вторая"}"',
                       f'--data "{tmp_path / "данные2"}"', службу=True)
    вывод = итог.stdout + итог.stderr
    assert итог.returncode == 2, вывод[-3000:]
    assert "уже обслуживает другую установку" in вывод
    assert юнит.read_text(encoding="utf-8") == было
    assert not (tmp_path / "opt" / "вторая").exists()


def test_неверный_порт_и_имя_службы_отвергаются_сразу(tmp_path: Path):
    for ключ, слова in (("--port abc", "Неверный порт"), ("--port 70000", "Неверный порт"),
                        ("--name 'моя служба'", "Неверное имя службы")):
        итог = _установщик(tmp_path, f'--prefix "{tmp_path / "prog"}"',
                           f'--data "{tmp_path / "data"}"', ключ)
        assert итог.returncode == 2, ключ + (итог.stdout + итог.stderr)[-1500:]
        assert слова in итог.stdout + итог.stderr


def test_установщик_кладёт_метку_установки(tmp_path: Path):
    итог = _установщик(tmp_path, f'--prefix "{tmp_path / "prog"}"',
                       f'--data "{tmp_path / "data"}"')
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    метка = (tmp_path / "prog" / ".asrhub-install").read_text(encoding="utf-8")
    assert f"data_dir={tmp_path / 'data'}\n" in метка
    # Службу не создавали — и в метке её нет, а не «asrhub» наугад.
    assert "service=\n" in метка
    assert "mode=native" in метка


def test_откат_выполняется_и_на_явный_выход_с_ошибкой(tmp_path: Path):
    """Ловушка ERR видит упавшую команду, а сценарии выходят сами: `exit 1`.

    `install.sh --force` с упавшим `python -m venv` оставлял установку без
    окружения: откат, обещанный шапкой, не выполнялся.
    """
    метка = tmp_path / "откат-выполнен"
    итог = прогнать(f'''
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      enable_error_handling
      add_rollback "touch '{метка}'"
      exit 1
    ''')
    assert итог.returncode == 1
    assert метка.exists(), итог.stdout + итог.stderr
    # Удачный конец (clear_rollback) не откатывает ничего.
    метка.unlink()
    итог = прогнать(f'''
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      enable_error_handling
      add_rollback "touch '{метка}'"
      clear_rollback
      exit 1
    ''')
    assert not метка.exists()


def test_журнал_сценария_читает_только_владелец(tmp_path: Path):
    """В журнал попадают хвосты журнала службы — имена файлов, то есть номера."""
    итог = прогнать(f'''
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      setup_logging "{tmp_path}"
      stat -c '%a' "$ASRHUB_LOG_FILE"
    ''')
    assert итог.stdout.strip().splitlines()[-1] == "600", итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# Порт и видеокарта
# ---------------------------------------------------------------------------


ЗАГЛУШКА = """
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
        b = b'{"status":"ok","version":"x"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *_):
        return

HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def _свободный_порт() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_порт_соседней_установки_не_считается_своим(tmp_path: Path):
    """Своим признавался любой ASR Hub на порту — и сервер соседней установки.

    Мастер второй установки оставлял ей порт первой: две службы на одном.
    """
    соседняя = tmp_path / "соседняя"
    своя = tmp_path / "своя"
    for каталог in (соседняя, своя):
        (каталог / "venv" / "bin").mkdir(parents=True)
        (каталог / "venv" / "bin" / "python").symlink_to(sys.executable)
    порт = _свободный_порт()
    процесс = subprocess.Popen([str(соседняя / "venv" / "bin" / "python"), "-c", ЗАГЛУШКА,
                               str(порт)])
    try:
        time.sleep(1.5)
        итог = прогнать(f'''
          source "{КОРЕНЬ}/scripts/lib/common.sh"
          port_is_ours {порт} "{своя}" asrhub-проба && echo своя=да || echo своя=нет
          port_is_ours {порт} "{соседняя}" asrhub-проба && echo соседняя=да || echo соседняя=нет
        ''', env={"ASRHUB_SYSTEMD_DIR": str(tmp_path / "нет")})
    finally:
        процесс.terminate()
        процесс.wait(timeout=10)
    assert "своя=нет" in итог.stdout, итог.stdout + итог.stderr
    assert "соседняя=да" in итог.stdout


@pytest.mark.skipif(os.geteuid() != 0, reason="проба от чужого имени — только от root")
def test_проба_видеокарты_идёт_от_пользователя_службы(tmp_path: Path):
    """Проба от root проходила там, где у службы нет доступа к /dev/kfd."""
    журнал = tmp_path / "runuser.log"
    каталог = tmp_path / "bin"
    каталог.mkdir()
    (каталог / "runuser").write_text(
        f'#!/bin/bash\necho "runuser $*" >> "{журнал}"\nshift 3\nexec "$@"\n', encoding="utf-8")
    (каталог / "runuser").chmod(0o755)
    python = tmp_path / "python"
    python.write_text("#!/bin/bash\necho 'ok|cuda|проба'\n", encoding="utf-8")
    python.chmod(0o755)
    итог = прогнать(f'''
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      source "{КОРЕНЬ}/scripts/lib/detect.sh"
      source "{КОРЕНЬ}/scripts/lib/gpu.sh"
      gpu_torch_probe "{python}" "{tmp_path}" auto nobody
    ''', env={"PATH": f"{каталог}:{os.environ['PATH']}"})
    assert "ok|cuda" in итог.stdout, итог.stdout + итог.stderr
    assert "runuser -u nobody --" in журнал.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Модели и движки
# ---------------------------------------------------------------------------


def test_удаление_движка_оставляет_пакеты_нужные_другим(tmp_path: Path):
    """numpy в файле gigaam есть, а держатся на нём soundfile и onnxruntime."""
    программа = tmp_path / "prog"
    shutil.copytree(КОРЕНЬ / "scripts", программа / "scripts")
    shutil.copytree(КОРЕНЬ / "requirements", программа / "requirements")
    (программа / "VERSION").write_text(ВЕРСИЯ, encoding="utf-8")
    (программа / "venv" / "bin").mkdir(parents=True)
    журнал = tmp_path / "pip.log"
    pip = программа / "venv" / "bin" / "pip"
    pip.write_text(f'''#!/bin/bash
echo "pip $*" >> "{журнал}"
case "$1" in
  check) echo "No broken requirements found." ;;
  show) shift; for p in "$@"; do echo "Name: $p"
          case "$p" in numpy) echo "Required-by: soundfile, onnxruntime" ;;
                       *) echo "Required-by: " ;; esac; echo "---"; done ;;
esac
exit 0
''', encoding="utf-8")
    pip.chmod(0o755)
    итог = прогнать(f'bash "{программа}/scripts/models.sh" remove-engine gigaam '
                    f'--prefix "{программа}" --data "{tmp_path / "data"}" --yes',
                    env={"HOME": str(tmp_path)})
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-2000:]
    снято = [с for с in журнал.read_text(encoding="utf-8").splitlines()
             if с.startswith("pip uninstall") and "optimum" not in с]
    assert снято and "gigaam" in снято[0], снято
    for нужный in ("numpy", "soundfile", "onnxruntime"):
        assert f" {нужный}" not in снято[0], f"снят {нужный}: {снято[0]}"


ПОДДЕЛЬНЫЙ_HUB = """
import os
def snapshot_download(repo_id, **kwargs):
    with open(os.environ["HUB_LOG"], "a", encoding="utf-8") as f:
        f.write(repo_id + "\\n")
    return os.path.join(kwargs.get("cache_dir") or ".", "models--" + repo_id.replace("/", "--"))
"""


def test_загрузка_faster_whisper_просит_настоящий_репозиторий(tmp_path: Path):
    """Источник «small» уходил в Hugging Face как репозиторий «small».

    Отказ разбирался как «модель закрыта лицензией»: модель лёгкого профиля
    по умолчанию не загружалась ни сценарием, ни из раздела «Модели».
    """
    пакет = tmp_path / "fake" / "huggingface_hub"
    пакет.mkdir(parents=True)
    (пакет / "__init__.py").write_text(ПОДДЕЛЬНЫЙ_HUB, encoding="utf-8")
    журнал = tmp_path / "hub.log"
    итог = прогнать(f'bash "{КОРЕНЬ}/scripts/models.sh" download faster-whisper-small '
                    f'--prefix "{КОРЕНЬ}" --data "{tmp_path / "data"}"',
                    env={"PYTHONPATH": str(tmp_path / "fake"), "HUB_LOG": str(журнал),
                         "HOME": str(tmp_path)})
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-2000:]
    assert журнал.read_text(encoding="utf-8").split() == ["Systran/faster-whisper-small"]
    assert "\\r" not in итог.stdout and "\\n" not in итог.stdout


def test_скачанная_модель_faster_whisper_находится_на_диске(tmp_path: Path):
    from asrhub import catalog, model_files

    spec = catalog.get_model("faster-whisper-small")
    assert spec is not None and spec.source == "small"
    (tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "x").mkdir(parents=True)
    найдено = model_files.find_local(tmp_path, spec.source, spec.revision or "")
    assert найдено == tmp_path / "models--Systran--faster-whisper-small"
    # Отпечаток весов больше не пуст — кеш результатов различает версии.
    ((tmp_path / "models--Systran--faster-whisper-small" / "snapshots" / "x" / "model.bin")
     .write_bytes(b"1"))
    model_files.forget(tmp_path)
    assert model_files.fingerprint(tmp_path, spec.source)
    # Турбо берётся движком из другого репозитория — и ищется там же.
    assert model_files.huggingface_repo("large-v3-turbo") == \
        "deepdml/faster-whisper-large-v3-turbo-ct2"
    assert model_files.huggingface_repo("ai-sage/GigaAM-v3") == "ai-sage/GigaAM-v3"


def _приложение(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_AUTH_ENABLED", "false")
    return create_app(load(), start_queue=False)


def _дождаться_загрузки(клиент, модель: str) -> dict:
    for _ in range(100):
        ответ = клиент.get(f"/api/models/{модель}/status").json()
        состояние = (ответ.get("download") or {})
        if состояние.get("status") in ("completed", "failed"):
            return состояние
        time.sleep(0.05)
    raise AssertionError("загрузка не закончилась")


def test_раздел_моделей_качает_faster_whisper_и_gigaam_по_правилам_движков(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Раздел «Модели» качал снапшоты, которых движки не открывают.

    faster-whisper просил репозиторий «small», GigaAM — снапшот ai-sage/GigaAM
    на Hugging Face, хотя библиотека берёт .ckpt со своего CDN.
    """
    import types

    from fastapi.testclient import TestClient

    вызовы: list[tuple[str, str]] = []
    hub = types.ModuleType("huggingface_hub")
    hub.snapshot_download = lambda repo_id, **kw: вызовы.append(("hf", repo_id)) or "."
    gigaam = types.ModuleType("gigaam")
    gigaam._download_model = lambda name, root: вызовы.append(("gigaam", name))
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)
    monkeypatch.setitem(sys.modules, "gigaam", gigaam)
    with TestClient(_приложение(tmp_path, monkeypatch)) as клиент:
        for модель in ("faster-whisper-small", "gigaam-v3-e2e-rnnt"):
            ответ = клиент.post(f"/api/models/{модель}/download")
            assert ответ.status_code == 200, ответ.text
            assert _дождаться_загрузки(клиент, модель)["status"] == "completed"
    assert ("hf", "Systran/faster-whisper-small") in вызовы, вызовы
    assert ("gigaam", "v3_e2e_rnnt") in вызовы, вызовы
    assert not any(вид == "hf" and "GigaAM" in имя for вид, имя in вызовы), вызовы


# ---------------------------------------------------------------------------
# Доктор и агент
# ---------------------------------------------------------------------------


def test_доктор_не_пропускает_незнакомый_ключ(tmp_path: Path):
    """`--only hardware` из документации молча запускал все проверки."""
    итог = прогнать(f'bash "{КОРЕНЬ}/scripts/doctor.sh" --only hardware',
                    env={"HOME": str(tmp_path)})
    assert итог.returncode == 2, итог.stdout[-1500:]
    assert "Неизвестный параметр: --only" in итог.stdout + итог.stderr


def test_агент_читает_ключ_из_файла(tmp_path: Path):
    """Ключ аргументом виден в `ps` любому пользователю станции."""
    ключ = tmp_path / "ключ"
    ключ.write_text("ah_" + "z" * 30 + "\n", encoding="utf-8")
    функция = subprocess.run(
        ["sed", "-n", "/^read_key_file()/,/^}/p", str(КОРЕНЬ / "agent" / "install.sh")],
        capture_output=True, text=True, check=True).stdout
    итог = прогнать(f'''
      die() {{ echo "ОШИБКА: $1"; exit 1; }}
      KEY="" KEY_FILE_ARG="{ключ}"
      {функция}
      read_key_file
      printf 'ключ=%s\\n' "$KEY"
    ''')
    assert f"ключ=ah_{'z' * 30}\n" in итог.stdout, итог.stdout + итог.stderr
    итог = прогнать(f'bash "{КОРЕНЬ}/agent/install.sh" --url http://127.0.0.1:9 '
                    f'--key-file "{tmp_path / "нет-файла"}"')
    assert итог.returncode == 1
    assert "не читается" in итог.stdout + итог.stderr


# ---------------------------------------------------------------------------
# Клиент asrctl
# ---------------------------------------------------------------------------


class _Сервер:
    """Поддельный ASR Hub: запоминает запросы, отвечает по сценарию."""

    def __init__(self, ответы: dict[str, list[tuple[int, str]]]):
        self.ответы = ответы
        self.запросы: list[tuple[str, str, dict, bytes]] = []
        сервер = self

        class Обработчик(BaseHTTPRequestHandler):
            def _ответ(self):
                длина = int(self.headers.get("Content-Length") or 0)
                тело = self.rfile.read(длина) if длина else b""
                сервер.запросы.append((self.command, self.path, dict(self.headers), тело))
                путь = self.path.split("?")[0]
                очередь = сервер.ответы.get(f"{self.command} {путь}") or [(404, "{}")]
                код, текст = очередь.pop(0) if len(очередь) > 1 else очередь[0]
                данные = текст.encode("utf-8")
                self.send_response(код)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(данные)))
                self.end_headers()
                self.wfile.write(данные)

            do_GET = do_POST = _ответ

            def log_message(self, *_):
                return

        self.порт = _свободный_порт()
        self.http = HTTPServer(("127.0.0.1", self.порт), Обработчик)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    @property
    def адрес(self) -> str:
        return f"http://127.0.0.1:{self.порт}"

    def закрыть(self):
        self.http.shutdown()


def _asrctl(tmp_path: Path, *аргументы: str, вход: str | None = None,
            **окружение: str) -> subprocess.CompletedProcess:
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(["bash", str(КОРЕНЬ / "scripts" / "client" / "asrctl"), *аргументы],
                          capture_output=True, text=True, timeout=120, input=вход,
                          cwd=str(tmp_path),
                          env={**os.environ, "HOME": str(tmp_path / "home"),
                               "NO_COLOR": "1", **окружение})


def test_удаление_последнего_профиля_удаляет_ключ(tmp_path: Path):
    """`config remove` последнего профиля: «удалён», код 0 — а ключ оставался."""
    итог = _asrctl(tmp_path, "config", "add", "один", "http://127.0.0.1:9", "-",
                   вход="ah_секретный\n")
    assert итог.returncode == 0, итог.stderr
    профили = tmp_path / "home" / ".config" / "asrctl" / "profiles"
    assert "ah_секретный" in профили.read_text(encoding="utf-8")
    итог = _asrctl(tmp_path, "config", "remove", "один")
    assert итог.returncode == 0, итог.stderr
    assert "ah_секретный" not in профили.read_text(encoding="utf-8")
    итог = _asrctl(tmp_path, "config", "remove", "один")
    assert итог.returncode == 1 and "нет" in итог.stderr


def test_текст_эталона_не_читается_из_файла(tmp_path: Path):
    """У `curl -F` значение с «<» читается из локального файла."""
    секрет = tmp_path / "секрет.txt"
    секрет.write_text("ПАРОЛЬ-ОТ-ВСЕГО", encoding="utf-8")
    запись = tmp_path / "запись.wav"
    запись.write_bytes(b"RIFF0000WAVE")
    сервер = _Сервер({"POST /api/jobs": [(200, '{"id": "j1"}')]})
    try:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send", str(запись),
                       "--reference", f"<{секрет}")
    finally:
        сервер.закрыть()
    assert итог.returncode == 0, итог.stderr
    тело = сервер.запросы[0][3]
    assert "ПАРОЛЬ-ОТ-ВСЕГО".encode() not in тело
    assert f"<{секрет}".encode() in тело


def test_имя_файла_с_сервера_не_уводит_за_каталог(tmp_path: Path):
    """Имя «../../x» записывало результат вне каталога назначения."""
    сервер = _Сервер({
        "GET /api/jobs/j1": [(200, '{"id": "j1", "filename": "../../ESCAPED.wav"}')],
        "GET /api/jobs/j1/download": [(200, "текст")],
    })
    каталог = tmp_path / "а" / "б" / "результаты"
    try:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "get", "j1",
                       "--output", str(каталог))
    finally:
        сервер.закрыть()
    assert итог.returncode == 0, итог.stderr
    assert (каталог / "ESCAPED.txt").exists()
    assert not (tmp_path / "а" / "ESCAPED.txt").exists()


def test_ожидание_переживает_перезапуск_сервера(tmp_path: Path):
    """`wait` бросал ожидание на первом же 503 или обрыве соединения."""
    сервер = _Сервер({
        "GET /api/jobs/j1": [(503, '{"message": "перезапуск"}'),
                             (503, '{"message": "перезапуск"}'),
                             (200, '{"id": "j1", "status": "completed", "filename": "з.wav"}')],
        "GET /api/jobs/j1/download": [(200, "готовый текст")],
    })
    try:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "wait", "j1",
                       "--output", str(tmp_path / "итог"))
    finally:
        сервер.закрыть()
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert "повтор" in итог.stderr
    assert (tmp_path / "итог" / "з.txt").read_text(encoding="utf-8") == "готовый текст"


def test_ключ_из_файла_и_незнакомая_подкоманда_моделей(tmp_path: Path):
    ключ = tmp_path / "ключ"
    ключ.write_text("ah_from_file_0123456789\n", encoding="utf-8")
    сервер = _Сервер({"GET /api/health": [(200, '{"status": "ok", "version": "x"}')]})
    try:
        итог = _asrctl(tmp_path, "--key-file", str(ключ), "--server", сервер.адрес, "health")
    finally:
        сервер.закрыть()
    assert итог.returncode == 0, итог.stderr
    заголовки = {k.lower(): v for k, v in сервер.запросы[0][2].items()}
    assert заголовки.get("x-api-key") == "ah_from_file_0123456789"
    итог = _asrctl(tmp_path, "models", "download", "gigaam-v3-ctc")
    assert итог.returncode == 2
    assert "models.sh download" in итог.stderr
    # Код выхода не попадает в текст сообщения.
    assert not итог.stderr.rstrip().endswith(" 2")


def test_пустые_массивы_клиента_не_ломают_bash_без_ключей(tmp_path: Path):
    """В bash 3.2 "${a[@]}" пустого массива — «unbound variable».

    Здесь bash новее, поэтому смотрим на сам текст: каждое раскрытие
    массивов ARGS, rest, depth и FMTS — защищённое.
    """
    текст = (КОРЕНЬ / "scripts" / "client" / "asrctl").read_text(encoding="utf-8")
    import re

    for имя in ("ARGS", "rest", "depth", "FMTS"):
        голые = re.findall(r'(?<!\+)"\$\{' + имя + r'\[@\]\}"', текст)
        защищённые = re.findall(r'\$\{' + имя + r'\[@\]\+"\$\{' + имя + r'\[@\]\}"\}', текст)
        assert not голые, f"{имя}: незащищённое раскрытие {голые}"
        assert защищённые, f"{имя}: раскрытий не найдено — проверка не о том"


def test_документация_сценариев_не_советует_несуществующих_команд():
    """`asrctl config set` советовали доктор и подсказки — такой команды нет."""
    for путь in ("scripts/doctor.sh", "scripts/lib/common.sh", "scripts/client/asrctl"):
        текст = (КОРЕНЬ / путь).read_text(encoding="utf-8")
        assert "asrctl config set" not in текст, путь
        assert "asrctl config schema" not in текст, путь



# ---------------------------------------------------------------------------
# Заодно с Windows: то же, найденное там, — и здесь
# ---------------------------------------------------------------------------


def test_установка_прежней_версии_без_службы_узнаёт_себя_по_окружению(tmp_path: Path):
    """Установка до 3.1.19 с --no-service: ни метки, ни службы.

    Её собственный uninstall.sh её не узнавал и брался за установку в
    стандартном каталоге. Окружение venv без .git — признак установки;
    рабочая копия исходников (.git рядом) установкой не считается.
    """
    журнал = tmp_path / "вызовы.log"
    программа = _установка_на_месте(tmp_path, "prog", tmp_path / "данные")
    (программа / ".asrhub-install").unlink()
    (программа / "venv" / "bin").mkdir(parents=True)
    итог = прогнать(f'bash "{программа}/scripts/uninstall.sh" --yes --dry-run',
                    env=_окружение(tmp_path, журнал))
    assert итог.returncode == 0, (итог.stdout + итог.stderr)[-3000:]
    assert re.search(rf"Программа\s+{re.escape(str(программа))} ", итог.stdout), итог.stdout[-2000:]

    исходники = _установка_на_месте(tmp_path, "checkout", tmp_path / "данные2")
    (исходники / ".asrhub-install").unlink()
    (исходники / "venv" / "bin").mkdir(parents=True)
    (исходники / ".git").mkdir()
    итог = прогнать(f'bash "{исходники}/scripts/uninstall.sh" --yes --dry-run',
                    env=_окружение(tmp_path, журнал))
    assert not re.search(rf"Программа\s+{re.escape(str(исходники))} ", итог.stdout), \
        итог.stdout[-2000:]


def _блок_режима_обновления() -> str:
    текст = (КОРЕНЬ / "scripts" / "update.sh").read_text(encoding="utf-8")
    начало = текст.index("DOCKER_MODE=0\n")
    конец = текст.index("\ncompose_cmd()", начало)
    return текст[начало:конец]


@pytest.mark.parametrize("метка, env_файл, venv, ожидается", [
    ("native", False, True, "0"),    # чужой контейнер «asrhub» — не повод
    ("native", True, False, "0"),    # .env от прежней установки в контейнере — метка важнее
    ("", False, True, "0"),          # без метки: окружение есть — не контейнер
    ("", False, False, "1"),         # без метки и без окружения — прежний признак
    ("docker", False, True, "1"),
    ("", True, True, "1"),
])
def test_обновление_не_считает_чужой_контейнер_своей_установкой(
        tmp_path: Path, метка: str, env_файл: bool, venv: bool, ожидается: str):
    """Живой контейнер «asrhub» делал любую установку контейнерной.

    Обычная установка на машине, где кто-то держит такой контейнер,
    обновлялась пересборкой чужого образа. Теперь решает метка установки.
    """
    программа = tmp_path / "prog"
    (программа / "docker").mkdir(parents=True)
    if метка:
        (программа / ".asrhub-install").write_text(f"mode={метка}\n", encoding="utf-8")
    if env_файл:
        (программа / "docker" / ".env").write_text("ASRHUB_PORT=8080\n", encoding="utf-8")
    if venv:
        (программа / "venv").mkdir()
    сценарий = f'''
      set -Eeuo pipefail
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      PREFIX="{программа}"
      have() {{ [[ "$1" == docker ]] && return 0; command -v "$1" >/dev/null 2>&1; }}
      docker() {{ [[ "$1" == ps ]] && echo asrhub; return 0; }}
{_блок_режима_обновления()}
      echo "РЕЖИМ=${{DOCKER_MODE}}"
    '''
    итог = subprocess.run(["bash", "-c", сценарий], capture_output=True, text=True, timeout=60)
    assert f"РЕЖИМ={ожидается}" in итог.stdout, итог.stdout + итог.stderr


def _блок_контейнеров_удаления() -> str:
    текст = (КОРЕНЬ / "scripts" / "uninstall.sh").read_text(encoding="utf-8")
    начало = текст.index("DOCKER_INSTALL=0\n")
    конец = текст.index("\n# --- 4. Удаление файлов", начало)
    return текст[начало:конец]


def test_удаление_контейнерной_установки_без_docker_предупреждает(tmp_path: Path):
    """Без docker удаление контейнерной установки молчало о контейнерах.

    Шаг «Удаление контейнеров» заканчивался ничем, удаление объявлялось
    законченным, а контейнеры продолжали работать и держать порт.
    """
    программа = tmp_path / "prog"
    программа.mkdir()
    (программа / ".asrhub-install").write_text("mode=docker\n", encoding="utf-8")
    сценарий = f'''
      set -Eeuo pipefail
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      PREFIX="{программа}"
      MODE=auto
      have() {{ [[ "$1" == docker ]] && return 1; command -v "$1" >/dev/null 2>&1; }}
{_блок_контейнеров_удаления()}
      echo "ДОШЛО"
    '''
    итог = subprocess.run(["bash", "-c", сценарий], capture_output=True, text=True, timeout=60,
                          env={**os.environ, "ASRHUB_NO_COLOR": "1", "TMPDIR": str(tmp_path)})
    вывод = итог.stdout + итог.stderr
    assert "ДОШЛО" in итог.stdout, вывод
    assert "docker не найден — контейнеры не остановлены" in вывод, вывод
    assert "docker rm -f asrhub asrhub-proxy" in вывод, вывод


def _блок_проверки_конфигурации() -> str:
    текст = (КОРЕНЬ / "scripts" / "update.sh").read_text(encoding="utf-8")
    начало = текст.index("# --- Конфигурация под новой версией")
    конец = текст.index("# --- Запуск и проверка", начало)
    return текст[начало:конец]


def test_проверку_конфигурации_при_обновлении_делает_пакет_установки(tmp_path: Path):
    """О старом файле должна судить новая версия, а не пакет из окружения.

    Проверка брала и PYTHONPATH того, кто запустил обновление: при
    недоставленном пакете установки о конфигурации судила другая версия,
    лежавшая в окружении, — и её мнение печаталось как мнение новой.
    """
    программа = tmp_path / "prog"
    (программа / "server" / "asrhub").mkdir(parents=True)
    чужой = tmp_path / "чужой" / "asrhub"
    чужой.mkdir(parents=True)
    (чужой / "__init__.py").write_text("", encoding="utf-8")
    (чужой / "__main__.py").write_text(
        'print("Не принято значений: 1 — ЧУЖОЙ ПАКЕТ")\n', encoding="utf-8")
    сценарий = f'''
      set -Eeuo pipefail
      source "{КОРЕНЬ}/scripts/lib/common.sh"
      PREFIX="{программа}"
      DATA_DIR="{tmp_path / "данные"}"
      DOCKER_MODE=0
      VPY="{sys.executable}"
{_блок_проверки_конфигурации()}
      echo "ДОШЛО"
    '''
    итог = subprocess.run(["bash", "-c", сценарий], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "PYTHONPATH": str(tmp_path / "чужой"),
                               "ASRHUB_NO_COLOR": "1", "TMPDIR": str(tmp_path)})
    вывод = итог.stdout + итог.stderr
    assert "ДОШЛО" in итог.stdout, вывод
    assert "ЧУЖОЙ ПАКЕТ" not in вывод, вывод
