"""Заход 48: документация против кода.

Документация обещала то, чего код не делал, — или описывала код, которого
уже нет: рецепт cron, «безопасный при повторе», каждые пять минут заводил по
заданию-копии на каждый файл каталога; подсказки отправляли к командам,
которых не существует; пример разбора ошибок API читал поле, которого в
ответе нет. Здесь — проверки того, что исправлено в коде, и сверка
документов с кодом там, где расхождение можно поймать автоматически.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from test_review_47_scripts import _asrctl, _Сервер
from test_review_47_windows import PWSH, фальшивый_сервер  # noqa: F401
from test_review_47_windows import _asrctl as _asrctl_ps

КОРЕНЬ = Path(__file__).resolve().parent.parent
ДОКУМЕНТЫ = КОРЕНЬ / "docs"


# ---------------------------------------------------------------------------
# Рецепт cron: отправленное уходит из каталога
# ---------------------------------------------------------------------------


def _входящие(tmp_path: Path) -> Path:
    каталог = tmp_path / "входящие"
    (каталог / "вложенный").mkdir(parents=True)
    (каталог / "a.mp3").write_bytes(b"1")
    (каталог / "вложенный" / "b.wav").write_bytes(b"2")
    (каталог / "заметки.txt").write_text("x", encoding="utf-8")
    return каталог


def test_send_dir_переносит_отправленное_и_повтор_его_не_шлёт(tmp_path: Path):
    """Рецепт cron обещал «повторный запуск безопасен» — а каждые пять минут
    каждый файл каталога уходил снова: сервер не пересчитывал запись, но
    заводил задание-копию из кеша и тратил квоту. Теперь `--move-to`
    уносит отправленное, и следующий запуск его не видит."""
    каталог = _входящие(tmp_path)
    отправленные = tmp_path / "отправленные"
    сервер = _Сервер({"POST /api/jobs": [(200, '{"id": "j1"}')]})
    try:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send-dir", str(каталог),
                       "--recursive", "--move-to", str(отправленные))
        повтор = _asrctl(tmp_path, "--server", сервер.адрес, "send-dir", str(каталог),
                         "--recursive", "--move-to", str(отправленные))
    finally:
        сервер.закрыть()
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert (отправленные / "a.mp3").read_bytes() == b"1"
    assert (отправленные / "вложенный" / "b.wav").read_bytes() == b"2"
    assert not (каталог / "a.mp3").exists() and (каталог / "заметки.txt").exists()
    assert повтор.returncode == 1 and "нет поддерживаемых" in повтор.stderr, повтор.stderr
    assert sum(1 for з in сервер.запросы if з[0] == "POST") == 2


def test_send_dir_не_уносит_то_что_не_отправилось(tmp_path: Path):
    каталог = _входящие(tmp_path)
    сервер = _Сервер({"POST /api/jobs": [(500, '{"detail": {"message": "сбой"}}')]})
    try:
        итог = _asrctl(tmp_path, "--server", сервер.адрес, "send-dir", str(каталог),
                       "--move-to", str(tmp_path / "отправленные"))
    finally:
        сервер.закрыть()
    assert итог.returncode == 1
    assert (каталог / "a.mp3").exists(), "неотправленный файл унесён"


def test_send_dir_не_переносит_внутрь_просматриваемого(tmp_path: Path):
    каталог = _входящие(tmp_path)
    итог = _asrctl(tmp_path, "--server", "http://127.0.0.1:9", "send-dir", str(каталог),
                   "--recursive", "--move-to", str(каталог / "готово"))
    assert итог.returncode == 1 and "найдётся снова" in итог.stderr, итог.stderr
    assert (каталог / "a.mp3").exists()


@pytest.mark.skipif(PWSH is None, reason="нужен PowerShell")
def test_send_dir_на_windows_переносит_отправленное(tmp_path: Path, фальшивый_сервер):  # noqa: F811
    _, адрес = фальшивый_сервер
    каталог = _входящие(tmp_path)
    отправленные = tmp_path / "отправленные"
    итог = _asrctl_ps(tmp_path, ["send-dir", str(каталог), "-Recurse", "-MoveTo",
                                 str(отправленные), "-Server", адрес])
    assert итог.returncode == 0, итог.stdout + итог.stderr
    assert (отправленные / "a.mp3").exists() and (отправленные / "вложенный" / "b.wav").exists()
    assert not (каталог / "a.mp3").exists() and (каталог / "заметки.txt").exists()
    итог = _asrctl_ps(tmp_path, ["send-dir", str(каталог), "-Recurse", "-MoveTo",
                                 str(каталог / "готово"), "-Server", адрес])
    assert итог.returncode == 2 and "найдётся снова" in итог.stdout, итог.stdout


def test_рецепт_cron_переносит_отправленное():
    текст = (ДОКУМЕНТЫ / "09-clients.md").read_text(encoding="utf-8")
    рецепт = re.search(r"asrhub-watch.*?```", текст, re.S)
    assert рецепт and "--move-to" in рецепт.group(0), "рецепт снова шлёт весь каталог"
    assert "повторный запуск безопасен" not in текст


# ---------------------------------------------------------------------------
# Подсказки — на то, что есть
# ---------------------------------------------------------------------------


def _команды_asrctl() -> set[str]:
    текст = (КОРЕНЬ / "scripts" / "client" / "asrctl").read_text(encoding="utf-8")
    разбор = re.search(r'case "\$\{COMMAND\}" in(.*?)\nesac', текст, re.S)
    assert разбор, "не найден разбор команд asrctl"
    return {к for строка in re.findall(r"^\s+([a-z|-]+)\)", разбор.group(1), re.M)
            for к in строка.split("|")}


def test_подсказки_и_документы_называют_только_настоящие_команды_asrctl():
    """Подсказки ошибок отправляли к `asrctl logs --tail`, `asrctl doctor`,
    `asrctl models download`, `asrctl config set`, `asrctl config schema` —
    таких команд у клиента нет и не было. Человек с ошибкой на руках получал
    вторую: «Неизвестная команда»."""
    команды = _команды_asrctl()
    подкоманды = {"config": {"add", "list", "remove"}}
    места = [*КОРЕНЬ.glob("server/asrhub/**/*.py"), *КОРЕНЬ.glob("scripts/**/*.sh"),
             *КОРЕНЬ.glob("scripts/**/*.ps1"), КОРЕНЬ / "README.md",
             *[п for п in ДОКУМЕНТЫ.glob("*.md") if п.name != "18-review.md"]]
    ложные = []
    for путь in места:
        текст = путь.read_text(encoding="utf-8", errors="replace")
        for найдено in re.finditer(r"asrctl(?:\.ps1)? ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?", текст):
            команда, вторая = найдено.group(1), найдено.group(2)
            if команда not in команды:
                ложные.append(f"{путь.relative_to(КОРЕНЬ)}: asrctl {команда}")
            elif команда in подкоманды and вторая and вторая not in подкоманды[команда]:
                ложные.append(f"{путь.relative_to(КОРЕНЬ)}: asrctl {команда} {вторая}")
    assert not ложные, ложные


# ---------------------------------------------------------------------------
# Формат ошибок API
# ---------------------------------------------------------------------------


def test_ошибка_api_несёт_поля_на_верхнем_уровне(client):
    """Документация обещала `{"detail": {…, "context": …}}`, пример читал
    `response.json()["detail"]`. Часть ошибок (413 до приёма тела, ошибки,
    вылетевшие из глубины, 500) приходит без `detail` вовсе — пример падал
    на KeyError вместо того, чтобы показать причину. Общее у всех — поля
    верхнего уровня; `detail` — только копия для прежних клиентов."""
    нет = client.get("/api/jobs/нет-такого")
    тело = нет.json()
    assert нет.status_code == 404 and тело["code"] == "job_not_found", тело
    assert {"message", "hint", "retryable"} <= тело.keys()
    велик = client.post("/api/jobs", content=b"x",
                        headers={"Content-Length": str(1 << 40),
                                 "Content-Type": "application/octet-stream"})
    тело = велик.json()
    assert велик.status_code == 413 and тело["code"] == "file_too_large", тело
    assert "detail" not in тело, "пример в документации снова читает detail"
    документ = (ДОКУМЕНТЫ / "08-api.md").read_text(encoding="utf-8")
    assert 'response.json()["detail"]' not in документ
    assert '"context"' not in документ.split("## Формат ошибок")[1].split("## ")[0]


def _все_коды_ошибок() -> dict[str, int]:
    import importlib
    import pkgutil

    import asrhub
    from asrhub.errors import ASRHubError

    for модуль in pkgutil.walk_packages(asrhub.__path__, "asrhub."):
        try:
            importlib.import_module(модуль.name)
        except Exception:                                   # noqa: BLE001
            continue

    def потомки(класс):
        for п in класс.__subclasses__():
            yield п
            yield from потомки(п)

    return {к.code: к.http_status for к in (ASRHubError, *потомки(ASRHubError))}


def test_таблица_кодов_ошибок_полна_и_верна():
    """В таблице не было password_change_required, quota_exceeded, llm_busy и
    ещё десятка кодов, которые сервер отдаёт; статусы сверяются с кодом."""
    документ = (ДОКУМЕНТЫ / "08-api.md").read_text(encoding="utf-8")
    раздел = документ.split("## Формат ошибок")[1].split("\n## ")[0]
    в_таблице = {к: int(с) for к, с in re.findall(r"^\| `([a-z_]+)` \| (\d{3}) \|", раздел, re.M)}
    коды = _все_коды_ошибок()
    нет = sorted(set(коды) - set(в_таблице))
    assert not нет, f"коды без строки в таблице: {нет}"
    расходится = {к: (в_таблице[к], с) for к, с in коды.items() if в_таблице[к] != с}
    assert not расходится, f"статус в таблице ≠ в коде: {расходится}"


# ---------------------------------------------------------------------------
# Примеры
# ---------------------------------------------------------------------------


def _пример(имя: str):
    import importlib.util

    спецификация = importlib.util.spec_from_file_location(
        f"пример_{имя}", КОРЕНЬ / "examples" / f"{имя}.py")
    модуль = importlib.util.module_from_spec(спецификация)
    спецификация.loader.exec_module(модуль)
    return модуль


class _Ответ:
    def __init__(self, данные, код: int = 200) -> None:
        self.данные, self.status_code, self.ok = данные, код, код < 400

    def json(self):
        return self.данные


def test_пример_мониторинга_видит_превышение_порога(monkeypatch, capsys):
    """Пример читал `value` и `troubleshooting`, а в снимке — `values` и
    `recommendation`: он отвечал «отклонений нет» при горящей аварии. Порог
    включительный (`inclusive`) он не учитывал вовсе."""
    from asrhub.monitoring import exporters
    from asrhub.monitoring.collector import Sample

    снимок = exporters.json_snapshot([
        Sample("asrhub_confidence_drift_level", 2.0, {"model": "gigaam-v3-ctc"}),
        Sample("asrhub_queue_depth", 3.0),
    ])
    пример = _пример("monitoring_pull")
    monkeypatch.setattr(пример.requests, "get", lambda адрес, **_: _Ответ(
        снимок if адрес.endswith("metrics.json") else {"status": "degraded"}))
    monkeypatch.setattr("sys.argv", ["monitoring_pull.py"])
    assert пример.main() == 0
    вывод = capsys.readouterr().out
    assert "✕ Дрейф уверенности: уровень (model=gigaam-v3-ctc): 2" in вывод, вывод
    assert "порог: >= 2" in вывод and "что делать:" in вывод
    assert "Заданий ждёт" not in вывод and "Отклонений от порогов нет" not in вывод


@pytest.mark.parametrize("пример", ["transcribe", "batch_folder"])
def test_примеры_показывают_причину_ошибки_без_detail(пример: str, monkeypatch):
    """Примеры читали `response.json()["detail"]` — у ошибки 413, пришедшей
    до приёма тела, его нет, и человек видел «HTTP 413» вместо причины."""
    модуль = _пример(пример)
    ответ = _Ответ({"code": "file_too_large", "message": "Файл больше 2048 МБ",
                    "hint": "Уменьшите файл"}, 413)
    if пример == "transcribe":
        assert модуль.error_of(ответ)["message"] == "Файл больше 2048 МБ"
    else:
        monkeypatch.setattr(модуль.requests, "post", lambda *а, **к: ответ)
        путь = КОРЕНЬ / "README.md"
        assert модуль.submit(путь, {})[2] == "Файл больше 2048 МБ"


# ---------------------------------------------------------------------------
# Метрики в документах
# ---------------------------------------------------------------------------


def _имена_запасного_среза() -> set[str]:
    текст = (КОРЕНЬ / "server" / "asrhub" / "analytics.py").read_text(encoding="utf-8")
    тело = текст[текст.index("    def prometheus(self"):]
    return {"asrhub_" + и for и in re.findall(r'add\(\s*f?"([a-z0-9_]+)"', тело)}


def test_документы_называют_только_настоящие_метрики():
    """Главы советовали тревоги по `asrhub_jobs_current`, `asrhub_rtf_p95`,
    `asrhub_success_rate` — именам запасного среза, которого при работающем
    мониторинге нет: правила из документации не срабатывали никогда."""
    from asrhub.monitoring.catalog import METRICS_BY_NAME

    известные = set(METRICS_BY_NAME)
    for имя, метрика in METRICS_BY_NAME.items():
        if метрика.type == "histogram":
            известные |= {имя + к for к in ("_bucket", "_sum", "_count")}
    запасные = _имена_запасного_среза()
    ложные = []
    for путь in ДОКУМЕНТЫ.glob("*.md"):
        if путь.name in ("18-review.md", "proposal-analytics.md", "proposal-2026.md"):
            continue
        текст = путь.read_text(encoding="utf-8")
        for найдено in re.finditer(r"\basrhub_[a-z0-9_]+", текст):
            имя = найдено.group(0)
            if имя in известные or имя in ("asrhub_session", "asrhub_rtfx"):
                continue
            # Запасной срез упоминать можно — но не в правилах и примерах вывода.
            окрестность = текст[max(0, найдено.start() - 400):найдено.start()]
            if имя in запасные and "запасн" in окрестность:
                continue
            ложные.append(f"{путь.name}: {имя}")
    assert not ложные, sorted(set(ложные))


def _пути_jq(выражение: str) -> list[list[str]]:
    """Пути полей из простого выражения jq: `.a.b`, `{x: .a, y: .b}`, `.a, .b`."""
    выражение = выражение.strip().strip("'\"")
    return [путь.split(".")[1:] for путь in re.findall(r"(?<![\w\]])(\.[a-z_]+(?:\.[a-z_]+)*)", выражение)]


def test_выражения_jq_в_документах_попадают_в_настоящие_поля(client):
    """`jq .running`, `jq .gpu`, `jq '.device, .gpu'` читали поля, которых в
    ответе нет: рецепты печатали null, и человек решал, что заданий нет, а
    видеокарты не видно."""
    проверено, ложные = 0, []
    for имя in ("10-operations.md", "11-troubleshooting.md", "09-clients.md", "06-queue.md"):
        текст = (ДОКУМЕНТЫ / имя).read_text(encoding="utf-8")
        for найдено in re.finditer(
                r"curl [^\n]*?(/api/[a-z0-9/_-]+(?:\?[^\s\"'`]*)?)[\"']?[^\n|]*(?:\\\n[^\n|]*)?\|\s*jq\s+(?:-r\s+)?('[^']*'|\S+)",
                текст):
            адрес, выражение = найдено.group(1), найдено.group(2)
            if "POST" in найдено.group(0) or "-X" in найдено.group(0):
                continue
            ответ = client.get(адрес)
            assert ответ.status_code == 200, f"{имя}: {адрес} → {ответ.status_code}"
            for путь in _пути_jq(выражение):
                узел = ответ.json()
                for часть in путь:
                    if not isinstance(узел, dict) or часть not in узел:
                        ложные.append(f"{имя}: {адрес} | jq {выражение} — нет поля {'.'.join(путь)}")
                        break
                    узел = узел[часть]
                проверено += 1
    assert проверено >= 6, f"рецептов с jq найдено мало ({проверено}) — проверка не о том"
    assert not ложные, ложные


def test_адреса_api_в_документах_существуют(client):
    """`GET /api/monitoring/push` в главе об эксплуатации и в журнале
    сервера — такого адреса нет, состояние приёмников отдаёт
    `/api/monitoring/targets`. Каждый адрес API в документах должен вести
    туда, где сервер отвечает."""
    маршруты = [*client.app.openapi()["paths"], "/api/stream", "/ws", "/api/openapi.json",
                "/api/docs", "/api/redoc"]
    образцы = [re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", м) + "/?$") for м in маршруты]
    # Чужие адреса: Ollama, amoCRM, пример приёмника уведомлений, префикс шлюза.
    чужие = {"/api/chat", "/api/pull", "/api/v4", "/api/asr/callback", "/api/llm/v1"}
    ложные = []
    места = [*ДОКУМЕНТЫ.glob("*.md"), КОРЕНЬ / "README.md"]
    for путь in места:
        if путь.name in ("18-review.md", "api-reference.md", "proposal-2026.md",
                         "proposal-analytics.md", "appendix-phone-asr.md"):
            continue
        текст = путь.read_text(encoding="utf-8")
        for найдено in re.finditer(r"/api/[A-Za-z0-9_\-/{}.]+", текст):
            адрес = найдено.group(0).rstrip(".,)")
            if адрес.endswith(("/", "{")) or адрес in чужие or адрес.startswith("/api/v4"):
                continue
            if not any(о.match(re.sub(r"\{[^}]*\}", "X", адрес)) for о in образцы):
                ложные.append(f"{путь.relative_to(КОРЕНЬ)}: {адрес}")
    assert not ложные, sorted(set(ложные))


def test_параметры_запросов_в_документах_существуют(client):
    """`/api/content/export?format=xlsx` — у маршрута параметр `fmt`, и
    `format=csv` молча давал xlsx; `/api/models?license=MIT` отдавал весь
    каталог — отбора по лицензии не было. Каждый параметр запроса из
    документов должен быть у своего маршрута."""
    схема = client.app.openapi()["paths"]
    маршруты = []
    for путь, методы in схема.items():
        имена = {п["name"] for м in методы.values() if isinstance(м, dict)
                 for п in м.get("parameters", []) if п.get("in") == "query"}
        маршруты.append((re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", путь) + "$"), имена))
    ложные, проверено = [], 0
    for путь in [*ДОКУМЕНТЫ.glob("*.md"), КОРЕНЬ / "README.md"]:
        if путь.name in ("18-review.md", "api-reference.md", "proposal-2026.md",
                         "proposal-analytics.md", "appendix-phone-asr.md"):
            continue
        текст = путь.read_text(encoding="utf-8")
        for найдено in re.finditer(r"(/api/[A-Za-z0-9_\-/{}.]+)\?([A-Za-z0-9_=&.,\-{}%]+)", текст):
            адрес = re.sub(r"\{[^}]*\}", "X", найдено.group(1))
            свои = next((имена for образец, имена in маршруты if образец.match(адрес)), None)
            if свои is None:
                continue
            for пара in найдено.group(2).split("&"):
                имя = пара.split("=")[0]
                проверено += 1
                # Ключ в адресе разбирает сама аутентификация, у любого маршрута.
                if имя and имя not in свои and имя not in ("api_key", "ticket"):
                    ложные.append(f"{путь.name}: {найдено.group(0)} — у маршрута нет «{имя}»")
    assert проверено >= 10, проверено
    assert not ложные, ложные


def test_каталог_моделей_отбирается_по_лицензии(client):
    ответ = client.get("/api/models", params={"license": "apache-2.0"})
    assert ответ.status_code == 200
    модели = ответ.json()["items"]
    assert модели and {м["license"] for м in модели} == {"Apache-2.0"}


# ---------------------------------------------------------------------------
# Рецепты config.yaml: сервер их принимает, и движок модели их читает
# ---------------------------------------------------------------------------

_ИСТОЧНИКИ_КОМАНД = [*sorted(ДОКУМЕНТЫ.glob("*.md")), КОРЕНЬ / "README.md",
                     КОРЕНЬ / "examples" / "README.md", КОРЕНЬ / "benchmarks" / "README.md",
                     КОРЕНЬ / "docker" / "README.md"]


def _блоки_yaml() -> list[tuple[str, str]]:
    """Блоки ```yaml из документации, похожие на config.yaml: хотя бы один
    ключ — параметр каталога. Манифесты Kubernetes, compose и Prometheus
    сюда не попадают."""
    import yaml
    from asrhub import catalog

    блоки = []
    for путь in sorted(ДОКУМЕНТЫ.glob("*.md")):
        if путь.name == "18-review.md":
            continue
        текст = путь.read_text(encoding="utf-8")
        for m in re.finditer(r"```yaml\n(.*?)```", текст, re.S):
            строка = текст[:m.start()].count("\n") + 1
            данные = yaml.safe_load(m.group(1))
            if not isinstance(данные, dict):
                continue
            if any(к in catalog.PARAMS_BY_KEY
                   or (к in catalog.GROUPS_BY_ID and isinstance(з, dict))
                   for к, з in данные.items()):
                блоки.append((f"{путь.name}:{строка}", m.group(1)))
    return блоки


def test_рецепты_config_yaml_сервер_принимает_без_замечаний(tmp_path: Path, monkeypatch):
    """Рецепт «Колл-центр» писал словарь замен строкой, «Плохая запись» —
    `audio_denoise: true`: сервер откладывал такие строки в замечания и брал
    умолчание, то есть рецепт молча не делал того, ради чего его брали.
    Каждый рецепт проходит через тот же загрузчик, что и config.yaml."""
    from asrhub import config

    for ключ in [к for к in list(__import__("os").environ) if к.startswith("ASRHUB_")]:
        monkeypatch.delenv(ключ, raising=False)
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "данные"))
    блоки = _блоки_yaml()
    assert len(блоки) >= 15, блоки
    замечания = {}
    for где, текст in блоки:
        файл = tmp_path / "config.yaml"
        файл.write_text(текст, encoding="utf-8")
        настройки = config.load(файл, apply_hardware=False, ensure_key=False)
        if настройки.problems:
            замечания[где] = настройки.problems
    assert not замечания, замечания


def test_рецепты_не_задают_параметров_которых_движок_модели_не_читает():
    """«Массовая обработка» ускоряла GigaAM `beam_size: 1`, `compute_type` и
    `batch_size`, «Реальное время» ставило Vosk `beam_size: 1`, MacBook —
    whisper.cpp `compute_type`: ни одного из них эти движки не читают.
    Рецепт, который обещает ускорение строкой без действия, хуже, чем
    никакого."""
    import yaml
    from asrhub import catalog

    лишние = []
    for где, текст in _блоки_yaml():
        данные = yaml.safe_load(текст)
        плоско: dict = {}
        for к, з in данные.items():
            if к in catalog.GROUPS_BY_ID and isinstance(з, dict):
                плоско.update(з)
            else:
                плоско[к] = з
        движок = str(плоско.get("engine") or "auto")
        if движок == "auto":
            модель = catalog.MODELS_BY_ID.get(str(плоско.get("model") or ""))
            if модель is None:
                continue
            движок = модель.engine
        for ключ in плоско:
            описание = catalog.PARAMS_BY_KEY.get(ключ)
            if описание is not None and описание.engines and движок not in описание.engines:
                лишние.append((где, движок, ключ))
    assert not лишние, лишние


# ---------------------------------------------------------------------------
# Команды сценариев в документах: ключи и подкоманды существуют
# ---------------------------------------------------------------------------

_ВЫЗОВ_SH = re.compile(
    r"(?:sudo\s+)?bash\s+(?:\S*/)?scripts/([\w-]+)\.sh((?:[ \t]+[^\s#`|;&)]+)*)")


def _вызовы_sh() -> list[tuple[str, str, list[str]]]:
    вызовы = []
    for путь in _ИСТОЧНИКИ_КОМАНД:
        if not путь.exists() or путь.name == "18-review.md":
            continue
        for номер, строка in enumerate(путь.read_text(encoding="utf-8").splitlines(), 1):
            for m in _ВЫЗОВ_SH.finditer(строка):
                аргументы = [а.strip("'\"") for а in m.group(2).split()]
                вызовы.append((f"{путь.name}:{номер}", m.group(1), аргументы))
    return вызовы


def test_ключи_сценариев_в_документах_сценарии_понимают():
    """Документы звали `doctor.sh --only hardware`. Ключа `--only` у доктора
    нет: раньше он молча запускал все проверки вместо одной, после захода 47
    незнакомый ключ — ошибка, и команда из документации стала падать."""
    вызовы = _вызовы_sh()
    assert len(вызовы) >= 40, len(вызовы)
    чужие = []
    for где, сценарий, аргументы in вызовы:
        исходник = КОРЕНЬ / "scripts" / f"{сценарий}.sh"
        assert исходник.exists(), (где, сценарий)
        текст = исходник.read_text(encoding="utf-8") + \
            (КОРЕНЬ / "scripts" / "lib" / "common.sh").read_text(encoding="utf-8")
        for ключ in (а for а in аргументы if re.fullmatch(r"--?[A-Za-z][\w-]*", а)):
            if not re.search(r"(?:^|[\s|(])" + re.escape(ключ) + r"(?:=\*)?[|)]", текст, re.M):
                чужие.append((где, сценарий, ключ))
    assert not чужие, чужие


def test_подкоманды_сценариев_в_документах_существуют():
    """`models.sh` и `service.sh` разбирают подкоманду первым словом."""
    чужие = []
    проверено = 0
    for где, сценарий, аргументы in _вызовы_sh():
        if сценарий not in ("models", "service") or not аргументы \
                or аргументы[0].startswith("-"):
            continue
        текст = (КОРЕНЬ / "scripts" / f"{сценарий}.sh").read_text(encoding="utf-8")
        проверено += 1
        if not re.search(r"^\s*(?:[\w-]+\|)*" + re.escape(аргументы[0]) + r"(?:\|[\w-]+)*\)",
                         текст, re.M):
            чужие.append((где, сценарий, аргументы[0]))
    assert проверено >= 20, проверено
    assert not чужие, чужие


# ---------------------------------------------------------------------------
# Неизвестная модель: отказ на входе, как обещает таблица кодов ошибок
# ---------------------------------------------------------------------------


def _wav_тишина() -> bytes:
    import io
    import wave

    буфер = io.BytesIO()
    with wave.open(буфер, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)
    return буфер.getvalue()


@pytest.mark.parametrize("поля, код", [
    ({"settings": '{"model": "gigaam-v99"}'}, "model_not_found"),
    ({"model": "gigaam-v99"}, "model_not_found"),
    ({"settings": '{"engine": "нет-такого"}'}, "config_error"),
    ({"settings": '{"model": ""}'}, "config_error"),
])
def test_неизвестная_модель_отвергается_при_постановке(client, поля, код):
    """Опечатка в модели принималась ответом 200 «в очереди», а через минуту
    задание падало: интеграция, которая смотрит на ответ постановки, считала
    запись принятой. Таблица кодов обещала 404 model_not_found."""
    до = len(list(Path(client.app.state.hub.settings.paths.uploads).iterdir()))
    ответ = client.post("/api/jobs", files={"file": ("a.wav", _wav_тишина(), "audio/wav")},
                        data=поля)
    assert ответ.status_code == (404 if код == "model_not_found" else 400), ответ.text
    assert ответ.json()["code"] == код, ответ.text
    # Файл отказа не остаётся в загрузках.
    assert len(list(Path(client.app.state.hub.settings.paths.uploads).iterdir())) == до
    пакет = client.post("/api/jobs/batch",
                        files=[("files", ("a.wav", _wav_тишина(), "audio/wav"))], data=поля)
    assert пакет.status_code == ответ.status_code, пакет.text


def test_неизвестная_модель_в_повторе_отвергается(client):
    задание = client.post("/api/jobs", files={"file": ("a.wav", _wav_тишина(), "audio/wav")})
    assert задание.status_code == 200, задание.text
    ответ = client.post(f"/api/jobs/{задание.json()['id']}/retry", json={"model": "gigaam-v99"})
    assert ответ.status_code == 404 and ответ.json()["code"] == "model_not_found", ответ.text


def test_опечатка_в_модели_сервера_не_роняет_каждое_задание(tmp_path: Path, monkeypatch):
    """`model: gigaam-v3-rnt` в config.yaml принималась молча, и каждое
    задание падало с model_not_found. Теперь это замечание при запуске и
    модель по умолчанию — как у любого другого недопустимого значения."""
    from asrhub import catalog, config

    for ключ in [к for к in list(__import__("os").environ) if к.startswith("ASRHUB_")]:
        monkeypatch.delenv(ключ, raising=False)
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "данные"))
    файл = tmp_path / "config.yaml"
    файл.write_text("model: gigaam-v3-rnt\nengine: gigaam\n", encoding="utf-8")
    настройки = config.load(файл, apply_hardware=False, ensure_key=False)
    assert настройки.get("model") == catalog.PARAMS_BY_KEY["model"].default
    assert настройки.get("engine") == "gigaam"
    assert any("gigaam-v3-rnt" in п for п in настройки.problems), настройки.problems
    with pytest.raises(Exception, match="движка"):
        настройки.set("engine", "gigaamm")
    # Сохранённые параметры старого задания с ушедшей из каталога моделью
    # по-прежнему разбираются: иначе не скачать бы его результат.
    assert настройки.merged({"model": "ушедшая-модель"})["model"] == "ушедшая-модель"


def test_справочник_api_называет_все_отборы_списка_заданий():
    """Отборы «возражение без отработки», «нарушения оператора», «не
    разобраны моделью» и другие были в интерфейсе и в базе, а в главе об API
    их не было — интеграция о них не узнала бы."""
    from asrhub.db import Database

    текст = (ДОКУМЕНТЫ / "08-api.md").read_text(encoding="utf-8")
    нет = sorted(к for к in [*Database.CONTENT_FILTERS, *Database.JOB_FILTERS]
                 if f"`{к}`" not in текст)
    assert not нет, нет


def test_формат_результата_сравнительного_прогона_описан_верно():
    """README обещал «JSON того же вида, что GET /api/analytics/models», а
    run.py пишет свой: generated_at, host, platform, files, results."""
    import ast
    import json as _json

    исходник = (КОРЕНЬ / "benchmarks" / "run.py").read_text(encoding="utf-8")
    дерево = ast.parse(исходник)

    def ключи_словаря(имя_функции: str, переменная: str | None) -> set[str]:
        for узел in ast.walk(дерево):
            if isinstance(узел, ast.FunctionDef) and узел.name == имя_функции:
                for под in ast.walk(узел):
                    if переменная is None and isinstance(под, ast.Return) \
                            and isinstance(под.value, ast.Dict):
                        return {к.value for к in под.value.keys}
                    if переменная and isinstance(под, ast.Assign) \
                            and isinstance(под.value, ast.Dict) \
                            and any(getattr(ц, "id", "") == переменная for ц in под.targets):
                        return {к.value for к in под.value.keys}
        raise AssertionError((имя_функции, переменная))

    readme = (КОРЕНЬ / "benchmarks" / "README.md").read_text(encoding="utf-8")
    пример = _json.loads(re.search(r"```json\n(.*?)```", readme, re.S).group(1))
    assert set(пример) == ключи_словаря("main", "payload")
    assert set(пример["results"][0]) == ключи_словаря("run_model", None)
    assert "analytics/models" not in (ДОКУМЕНТЫ / "09-clients.md").read_text(encoding="utf-8")


def test_все_примеры_названы_в_документах_и_читают_одни_переменные(monkeypatch):
    """`stream_microphone.py` один читал ASRHUB_URL и ASRHUB_API_KEY, а README
    велел задавать ASRHUB_SERVER и ASRHUB_KEY — пример шёл без ключа на
    127.0.0.1. И ни README, ни глава о клиентах его не называли."""
    import importlib.util

    примеры = sorted(п.name for п in (КОРЕНЬ / "examples").glob("*.py"))
    for документ in (КОРЕНЬ / "examples" / "README.md", ДОКУМЕНТЫ / "09-clients.md"):
        текст = документ.read_text(encoding="utf-8")
        нет = [п for п in примеры if f"`{п}`" not in текст]
        assert not нет, (документ.name, нет)

    monkeypatch.setenv("ASRHUB_SERVER", "https://asr.example.ru:8443")
    monkeypatch.setenv("ASRHUB_KEY", "ah_ключ")
    спец = importlib.util.spec_from_file_location(
        "stream_microphone_48", КОРЕНЬ / "examples" / "stream_microphone.py")
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    assert модуль.API_KEY == "ah_ключ"
    assert модуль.addresses(модуль.DEFAULT_URL) == ("https://asr.example.ru:8443",
                                                   "wss://asr.example.ru:8443")
    assert модуль.addresses("ws://127.0.0.1:8080/") == ("http://127.0.0.1:8080",
                                                        "ws://127.0.0.1:8080")
    assert модуль.addresses("сервер:8080")[1] == "ws://сервер:8080"


def test_кнопки_названные_в_документах_есть_в_интерфейсе():
    """Документы описывали кнопки, которых нет: «Сверить» в справочнике
    сотрудников, «Скачать всё» у результатов. Каждая «кнопка «…»» из глав
    должна найтись в интерфейсе дословно."""
    интерфейс = "".join((КОРЕНЬ / "server" / "asrhub" / "web" / имя).read_text(encoding="utf-8")
                        for имя in ("app.js", "index.html"))
    упоминание = re.compile(r"[Кк]нопк(?:а|у|ой|и|е)\s+«([^»]{2,60})»")
    нет = []
    всего = 0
    for путь in sorted(ДОКУМЕНТЫ.glob("*.md")):
        if путь.name == "18-review.md":
            continue
        for номер, строка in enumerate(путь.read_text(encoding="utf-8").splitlines(), 1):
            for m in упоминание.finditer(строка):
                всего += 1
                надпись = m.group(1).replace("„", "«").replace("“", "»")
                if надпись not in интерфейс:
                    нет.append((f"{путь.name}:{номер}", надпись))
    assert всего >= 30, всего
    assert not нет, нет
