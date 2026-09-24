"""Заход 45: мониторинг — выгрузки, тревоги, приёмники, сбор.

Что было не так, коротко. Суточное окно из базы выкладывалось под именем
гистограммы, и rate() принимал выход заданий из окна за перезапуск. Шаблон
Zabbix не совпадал с отправкой: метки шли по алфавиту, правило обнаружения
никто не наполнял, а «доставка» в Zabbix была HTTP-запросом на порт
траппера. StatsD получал итог вместо прироста. Встроенные тревоги, правила
Prometheus и триггеры Zabbix сравнивали порог каждый своим знаком. Раздел
«Мониторинг» стирал у приёмников заголовки и ничего не сохранял. Тревоги
считались только при опросе, а сбор шёл параллельно сам с собой.
"""
from __future__ import annotations

import json
import re
import socket
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from asrhub.monitoring import exporters
from asrhub.monitoring.alerts import STATE_FIRING, STATE_OK, AlertEngine, Rule, default_rules
from asrhub.monitoring.catalog import METRICS_BY_NAME
from asrhub.monitoring.collector import RUNTIME, Collector, Histogram, Sample
from asrhub.monitoring.pushers import PushManager, Target, TargetState, send
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Общее
# ---------------------------------------------------------------------------


def _приложение(data_dir: Path, monkeypatch: pytest.MonkeyPatch, *,
                start_queue: bool = False, config: dict[str, Any] | None = None):
    from asrhub.api import create_app
    from asrhub.config import load

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    monkeypatch.setenv("ASRHUB_VAD_BACKEND", "energy")
    if config is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "config.yaml").write_text(
            yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return create_app(load(), start_queue=start_queue)


class Часы:
    """Время для модуля тревог — двигается руками."""

    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def time(self) -> float:
        return self.t


def _значение(текст: str, серия: str) -> float | None:
    """Значение серии в текстовом формате Prometheus."""
    for строка in текст.splitlines():
        if строка.startswith(серия + " "):
            return float(строка.rsplit(" ", 1)[1])
    return None


# ---------------------------------------------------------------------------
# Гистограммы: под их именем — только счётчик с запуска
# ---------------------------------------------------------------------------


def test_гистограмма_длительности_не_берётся_из_окна_базы(data_dir: Path,
                                                         monkeypatch: pytest.MonkeyPatch):
    """Под именем гистограммы — только накопитель в памяти.

    Разбор суток по базе выкладывался гистограммой и побеждал накопитель:
    когда задания выходили из окна, `_count` и корзины убывали, rate()
    считал это сбросом, и `increase(...[1h])` давал 1424 при шести
    настоящих заданиях.
    """
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as клиент:
        db = app.state.hub.db
        for номер in range(7):
            db.create_job({"id": f"j{номер}", "filename": "a.wav", "status": "completed",
                           "processing_time_s": 1000.0 + номер, "media_duration_s": 60.0})
        было = {ключ: гист.total for ключ, гист in RUNTIME.snapshot()[2].items()
                if ключ[0] == "asrhub_job_duration_seconds"}
        текст = клиент.get("/api/monitoring/metrics").text
    в_памяти = sum(было.values())
    отдано = _значение(текст, "asrhub_job_duration_seconds_count")
    # Семь заданий в базе — не повод объявлять их наблюдениями гистограммы.
    assert (отдано or 0.0) == float(в_памяти), текст[:2000]
    assert "asrhub_job_duration_day_seconds{stat=\"p50\"}" in текст
    assert _значение(текст, 'asrhub_job_duration_day_seconds{stat="p50"}') == 1003.0


def test_суточное_окно_отдаётся_мгновенными_значениями():
    """Окно по базе — `…_day_seconds{stat}`, тип gauge в каталоге и выгрузке."""
    for имя in ("asrhub_job_duration_day_seconds", "asrhub_media_duration_day_seconds"):
        assert METRICS_BY_NAME[имя].type == "gauge"
    текст = exporters.prometheus([Sample("asrhub_job_duration_day_seconds", 5.0,
                                         {"stat": "p95"})])
    assert "# TYPE asrhub_job_duration_day_seconds gauge" in текст


# ---------------------------------------------------------------------------
# Zabbix
# ---------------------------------------------------------------------------


def _zabbix_создаст(шаблон: str, данные: list[dict[str, Any]]) -> tuple[set[str], set[str]]:
    """Какие элементы будут у узла: статичные и созданные по обнаружению.

    Повторяет то, что делает Zabbix: правило обнаружения получает наборы
    макросов и подставляет их в ключи прототипов. В параметр в кавычках
    значение попадает с экранированными кавычками.
    """
    tpl = yaml.safe_load(шаблон)["zabbix_export"]["templates"][0]
    элементы = {п["key"] for п in tpl["items"]}
    правила = {п["key"]: п for п in tpl.get("discovery_rules", [])}
    for точка in данные:
        правило = правила.get(точка["key"])
        if правило is None:
            continue
        for набор in json.loads(точка["value"]):
            for прототип in правило["item_prototypes"]:
                ключ = прототип["key"]
                for макрос, значение in набор.items():
                    ключ = ключ.replace(f'"{макрос}"', '"' + значение.replace('"', '\\"') + '"')
                    ключ = ключ.replace(макрос, значение)
                элементы.add(ключ)
    return элементы, set(правила)


def test_zabbix_шаблон_принимает_всё_отправленное(data_dir: Path,
                                                    monkeypatch: pytest.MonkeyPatch):
    """Каждое отправленное значение находит свой элемент — и по обнаружению тоже.

    Из 112 отправленных ключей шаблону соответствовали 64: правило
    обнаружения не наполнялось, метки шли по алфавиту (`[demo,demo-simulator]`
    против прототипа `[{#MODEL},{#ENGINE}]`), а WER ждали срезами avg…p99
    при метке «модель».
    """
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app):
        снимок, _ = app.state.hub.monitoring.samples(fresh=True)
    снимок = [*снимок,
              Sample("asrhub_jobs_by_model", 3, {"engine": "demo", "model": "demo-simulator"}),
              Sample("asrhub_wer", 0.12, {"model": "gigaam-v3-rnnt"}),
              Sample("asrhub_gpu_temperature_celsius", 71, {"gpu": "0"}),
              Sample("asrhub_model_disagreement", 0.2,
                     {"model": "a", "control_model": "b"}),
              Sample("asrhub_content_category_share", 0.3,
                     {"category": 'Жалоба, «претензия» "срочно"', "kind": "negative"})]
    данные = exporters.zabbix_data(снимок, "asr-01")
    элементы, правила = _zabbix_создаст(exporters.zabbix_template(), данные)
    значения = [т["key"] for т in данные if not т["key"].startswith("asrhub.discovery[")]
    обнаружение = [т["key"] for т in данные if т["key"].startswith("asrhub.discovery[")]
    мимо = [к for к in значения if к not in элементы]
    assert not мимо, f"значения без элемента: {мимо[:6]}"
    assert set(обнаружение) <= правила
    assert 'asrhub_jobs_by_model["demo-simulator","demo"]' in значения
    assert not any(к.startswith("asrhub_wer[avg") for к in элементы), \
        "элементы WER по срезам не получат данных никогда"


def test_zabbix_триггеры_на_оба_уровня_и_тем_же_знаком():
    """Предупреждение рядом с аварией, включительный порог — включительно."""
    tpl = yaml.safe_load(exporters.zabbix_template())["zabbix_export"]["templates"][0]
    элементы = {п["key"]: п for п in tpl["items"]}
    тревожные = элементы["asrhub_content_alert_records"]["triggers"]
    выражения = {т["priority"]: т["expression"] for т in тревожные}
    assert выражения["WARNING"].endswith(">=1"), выражения
    assert выражения["HIGH"].endswith(">=5"), выражения
    дрейф = next(п for п in tpl["discovery_rules"]
                 if п["key"] == "asrhub.discovery[asrhub_confidence_drift_level]")
    прототипы = {т["priority"]: т["expression"]
                 for т in дрейф["item_prototypes"][0]["trigger_prototypes"]}
    assert прототипы["HIGH"].endswith(">=2"), прототипы
    успех = next(п for п in tpl["discovery_rules"]
                 if п["key"] == "asrhub.discovery[asrhub_model_success_rate]")
    assert {т["priority"] for т in успех["item_prototypes"][0]["trigger_prototypes"]} == {
        "HIGH", "WARNING"}


class Траппер:
    """Поддельный сервер Zabbix: протокол ZBXD, отвечает заданной строкой."""

    def __init__(self, принято: int | None = None, отбито: int = 0) -> None:
        self.пакеты: list[dict[str, Any]] = []
        self.заголовки: list[bytes] = []
        self.принято = принято
        self.отбито = отбито
        self.гнездо = socket.socket()
        self.гнездо.bind(("127.0.0.1", 0))
        self.гнездо.listen(5)
        self.порт = self.гнездо.getsockname()[1]
        self.поток = threading.Thread(target=self._работать, daemon=True)
        self.поток.start()

    def _работать(self) -> None:
        while True:
            try:
                связь, _ = self.гнездо.accept()
            except OSError:
                return
            with связь:
                данные = b""
                while len(данные) < 13:
                    данные += связь.recv(65536)
                длина = int.from_bytes(данные[5:9], "little")
                while len(данные) < 13 + длина:
                    данные += связь.recv(65536)
                self.заголовки.append(данные[:5])
                тело = json.loads(данные[13:13 + длина])
                self.пакеты.append(тело)
                принято = len(тело["data"]) if self.принято is None else self.принято
                ответ = json.dumps({"response": "success",
                                   "info": f"processed: {принято}; failed: {self.отбито}; "
                                           f"total: {принято + self.отбито}; "
                                           "seconds spent: 0.000100"}).encode()
                связь.sendall(b"ZBXD\x01" + struct.pack("<II", len(ответ), 0) + ответ)

    def закрыть(self) -> None:
        self.гнездо.close()


def test_zabbix_приёмник_говорит_протоколом_траппера():
    """Доставка в Zabbix — пакет ZBXD, а не HTTP POST со снимком.

    Документация велела заводить `kind: webhook` на порт 10051: траппер
    получал «POST / HTTP/1.1…» и не принимал ни одного значения.
    """
    траппер = Траппер()
    try:
        приёмник = Target.from_dict({"kind": "zabbix", "host": "asr-01",
                                     "url": f"zabbix://127.0.0.1:{траппер.порт}"})
        менеджер = PushManager(lambda: [], [приёмник])
        снимок = [Sample("asrhub_up", 1),
                  Sample("asrhub_jobs_by_model", 2, {"model": "m", "engine": "e"})]
        итог = менеджер.push_once(приёмник, снимок)
    finally:
        траппер.закрыть()
    assert итог["ok"], итог
    assert "processed: " in итог.get("info", "")
    assert траппер.заголовки[0] == b"ZBXD\x01"
    пакет = траппер.пакеты[0]
    assert пакет["request"] == "sender data"
    assert {т["host"] for т in пакет["data"]} == {"asr-01"}
    ключи = {т["key"] for т in пакет["data"]}
    assert "asrhub.discovery[asrhub_jobs_by_model]" in ключи
    assert 'asrhub_jobs_by_model["m","e"]' in ключи


def test_zabbix_ничего_не_принял_это_ошибка():
    """«processed: 0; failed: N» — не доставка: узел не тот или шаблона нет."""
    траппер = Траппер(принято=0, отбито=5)
    try:
        приёмник = Target.from_dict({"kind": "zabbix", "host": "нет-такого",
                                     "url": f"127.0.0.1:{траппер.порт}"})
        итог = PushManager(lambda: [], [приёмник]).push_once(
            приёмник, [Sample("asrhub_up", 1)])
    finally:
        траппер.закрыть()
    assert итог["ok"] is False
    assert "нет-такого" in итог["error"] and "шаблон" in итог["error"]


def test_zabbix_наборы_меток_не_шлются_каждую_минуту():
    """Обнаружение — когда наборы изменились (и раз в полчаса), а не всегда."""
    траппер = Траппер()
    try:
        приёмник = Target.from_dict({"kind": "zabbix", "host": "h",
                                     "url": f"zabbix://127.0.0.1:{траппер.порт}"})
        менеджер = PushManager(lambda: [], [приёмник])
        снимок = [Sample("asrhub_wer", 0.1, {"model": "m"})]
        менеджер.push_once(приёмник, снимок)
        менеджер.push_once(приёмник, снимок)
        менеджер.push_once(приёмник, [*снимок, Sample("asrhub_wer", 0.2, {"model": "n"})])
    finally:
        траппер.закрыть()
    с_обнаружением = [any(т["key"].startswith("asrhub.discovery[") for т in п["data"])
                      for п in траппер.пакеты]
    assert с_обнаружением == [True, False, True]


def _разобрать_строку_sender(строка: str) -> list[str]:
    """Разбор строки входного файла zabbix_sender: поля через пробел, кавычки."""
    поля, текущее, в_кавычках, i = [], "", False, 0
    while i < len(строка):
        знак = строка[i]
        if в_кавычках:
            if знак == "\\" and i + 1 < len(строка):
                текущее += строка[i + 1]
                i += 2
                continue
            if знак == '"':
                в_кавычках = False
            else:
                текущее += знак
        elif знак == '"':
            в_кавычках = True
        elif знак == " ":
            поля.append(текущее)
            текущее = ""
        else:
            текущее += знак
        i += 1
    поля.append(текущее)
    return поля


def test_zabbix_sender_получает_строки_а_не_json():
    """Формат для `zabbix_sender -i -`: «узел ключ значение», с кавычками."""
    снимок = [Sample("asrhub_up", 1),
              Sample("asrhub_content_category_share", 0.3,
                     {"category": 'Жалоба, "срочно"', "kind": "negative"})]
    строки = exporters.zabbix_sender_lines(снимок, "asr 01").splitlines()
    разобрано = [_разобрать_строку_sender(с) for с in строки]
    assert all(len(п) == 3 for п in разобрано), строки
    ожидается = [[т["host"], т["key"], т["value"]]
                 for т in exporters.zabbix_data(снимок, "asr 01")]
    assert разобрано == ожидается


def test_zabbix_sender_формат_доступен_маршрутом(client):
    ответ = client.get("/api/monitoring/metrics?format=zabbix_sender&host=asr-01")
    assert ответ.status_code == 200
    assert ответ.text.startswith("asr-01 ")


# ---------------------------------------------------------------------------
# StatsD
# ---------------------------------------------------------------------------


def _statsd_приём() -> tuple[socket.socket, str]:
    гнездо = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    гнездо.bind(("127.0.0.1", 0))
    гнездо.settimeout(1.0)
    return гнездо, f"udp://127.0.0.1:{гнездо.getsockname()[1]}"


def _прочитать(гнездо: socket.socket) -> list[str]:
    строки = []
    while True:
        try:
            строки.append(гнездо.recv(65536).decode())
        except TimeoutError:
            return строки
        except OSError:
            return строки


def test_statsd_получает_прирост_а_не_итог():
    """Три отправки без новых заданий — ноль прироста, а не +150 каждый раз."""
    гнездо, адрес = _statsd_приём()
    приёмник = Target.from_dict({"kind": "statsd", "url": адрес})
    менеджер = PushManager(lambda: [], [приёмник])
    итог = Sample("asrhub_jobs_total", 150, {"status": "completed"})
    try:
        for _ in range(3):
            менеджер.push_once(приёмник, [итог])
        менеджер.push_once(приёмник, [Sample("asrhub_jobs_total", 4, {"status": "completed"})])
        строки = _прочитать(гнездо)
    finally:
        гнездо.close()
    assert строки == ["asrhub.jobs_total.status.completed:150|c",
                      "asrhub.jobs_total.status.completed:0|c",
                      "asrhub.jobs_total.status.completed:0|c",
                      # Значение упало — сервер перезапускался: прирост — само значение.
                      "asrhub.jobs_total.status.completed:4|c"]


def test_statsd_проба_не_портит_счёт_и_не_шлёт_корзины():
    гнездо, адрес = _statsd_приём()
    приёмник = Target.from_dict({"kind": "statsd", "url": адрес})
    гистограмма = Histogram((1, 5))
    гистограмма.observe(2)
    снимок: list[Sample] = [Sample("asrhub_jobs_total", 9, {"status": "failed"})]
    Collector._emit_histogram(снимок, "asrhub_job_duration_seconds", гистограмма, {})
    менеджер = PushManager(lambda: [], [приёмник])
    try:
        менеджер.push_once(приёмник, снимок, проба=True)
        строки = _прочитать(гнездо)
    finally:
        гнездо.close()
    assert "asrhub.jobs_total.status.failed:0|c" in строки
    assert not any("_bucket" in с or "le." in с for с in строки), строки
    assert "asrhub.job_duration_seconds_count:0|c" in строки
    assert not менеджер._states[приёмник.name].statsd_last, "проба запомнила итог"


# ---------------------------------------------------------------------------
# OpenTelemetry
# ---------------------------------------------------------------------------


def test_otlp_начало_отсчёта_единицы_и_гистограммы():
    гистограмма = Histogram((1, 5))
    for значение in (0.5, 3, 10):
        гистограмма.observe(значение)
    снимок: list[Sample] = [Sample("asrhub_jobs_total", 4, {"status": "failed"}),
                            Sample("asrhub_jobs_total", 9, {"status": "completed"}),
                            Sample("asrhub_disk_free_bytes", 1e9)]
    Collector._emit_histogram(снимок, "asrhub_job_duration_seconds", гистограмма, {})
    тело = exporters.otlp_payload(снимок, started_at=1000.0)
    метрики = {м["name"]: м for м in
               тело["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]}
    # Одна метрика на имя, а не десяток одноимённых.
    assert len(метрики["asrhub_jobs_total"]["sum"]["dataPoints"]) == 2
    точка = метрики["asrhub_jobs_total"]["sum"]["dataPoints"][0]
    assert точка["startTimeUnixNano"] == str(1000 * 1_000_000_000)
    assert точка["startTimeUnixNano"] != точка["timeUnixNano"]
    assert метрики["asrhub_disk_free_bytes"]["unit"] == "By"
    h = метрики["asrhub_job_duration_seconds"]
    assert h["unit"] == "s" and "histogram" in h
    точка = h["histogram"]["dataPoints"][0]
    assert точка["explicitBounds"] == [1.0, 5.0]
    assert точка["bucketCounts"] == ["1", "1", "1"] and точка["count"] == "3"
    assert not any(имя.endswith("_bucket") for имя in метрики)


# ---------------------------------------------------------------------------
# Один знак порога у трёх систем наблюдения
# ---------------------------------------------------------------------------


def test_встроенные_тревоги_prometheus_и_zabbix_сравнивают_одинаково():
    """На самом пороге все три системы отвечают одно и то же.

    Встроенный движок сам считал включительными пороги 1 «выше» и 0
    «ниже», Prometheus — только queue_paused: запись с тревожным
    упоминанием (порог 1, «на любое ненулевое») в Prometheus не
    срабатывала, а эмпатия на ровном нуле горела только в интерфейсе.
    """
    правила = yaml.safe_load(exporters.prometheus_rules())["groups"][0]["rules"]
    выражения = {п["alert"]: п["expr"] for п in правила}
    tpl = yaml.safe_load(exporters.zabbix_template())["zabbix_export"]["templates"][0]
    триггеры = {п["key"]: п.get("triggers", []) for п in tpl["items"]}
    проверено = 0
    for правило in default_rules():
        spec = METRICS_BY_NAME[правило.metric]
        выражение = выражения.get(exporters._alert_name(spec, правило.severity))
        if выражение is None or "rate(" in выражение or "/" in выражение:
            continue
        знак = re.search(r"(>=|<=|==|>|<)\s*[-0-9.e+]+\s*$", выражение).group(1)
        assert правило.breached(правило.threshold) is ("=" in знак), \
            f"{правило.metric}: движок и Prometheus ({выражение}) расходятся"
        for триггер in триггеры.get(правило.metric, []):
            if триггер["expression"].endswith(f"{правило.threshold:.15g}"):
                assert ("=" in триггер["expression"]) is ("=" in знак), триггер
        проверено += 1
    assert проверено > 20
    assert "asrhub_content_alert_records >= 1" in "\n".join(выражения.values())


def test_пауза_очереди_поднимает_тревогу_на_единице():
    """Признак 0/1 с порогом «выше 1» — включительно, и в каталоге, и снаружи."""
    правило = next(п for п in default_rules() if п.metric == "asrhub_queue_paused")
    assert правило.breached(1) and not правило.breached(0)
    assert "asrhub_queue_paused >= 1" in exporters.prometheus_rules()


def test_пересчёт_единиц_сохраняет_включительность():
    """Порог, собранный заново при переводе в байты, терял `inclusive`."""
    from asrhub.monitoring.catalog import Threshold, _rescale

    порог = _rescale(Threshold("above", warning=1, critical=2, inclusive=True), 1024.0)
    assert порог.inclusive is True and порог.critical == 2048


def test_правило_потока_смотрит_на_p95():
    """Порог задержки потока — по p95, как сказано в примечании, а не по p50."""
    текст = exporters.prometheus_rules()
    assert 'asrhub_stream_first_text_seconds{stat="p95"} > 6.0' in текст


def test_панель_grafana_делит_оси_и_отбирает_экземпляр():
    """На одной оси — одна единица; $instance — в каждом запросе."""
    from asrhub.monitoring.exporters import _grafana_target

    панель = exporters.grafana_dashboard()
    имена = {"datasource", "instance"}
    assert имена <= {п["name"] for п in панель["templating"]["list"]}
    for блок in панель["panels"][1:]:
        единица = блок["fieldConfig"]["defaults"]["unit"]
        for запрос in блок["targets"]:
            assert 'instance=~"$instance"' in запрос["expr"], запрос["expr"]
            for имя in set(re.findall(r"asrhub_[a-z0-9_]+", запрос["expr"])):
                spec = METRICS_BY_NAME.get(имя.removesuffix("_bucket"))
                assert spec is not None, имя
                assert _grafana_target(spec)[2] == единица, (блок["title"], имя, единица)
    ошибки = [з["expr"] for б in панель["panels"] for з in б["targets"]
              if "last_error_timestamp" in з["expr"]]
    assert ошибки and all(з.startswith("(time() - ") for з in ошибки), ошибки


def test_порог_места_следует_за_disk_min_free_gb():
    """Критично — ниже предела приёма, предупреждение — ниже двойного."""
    настройки = SimpleNamespace(get=lambda к, п=None: {"disk_min_free_gb": 50}.get(к, п))
    гиб = 1024 ** 3
    место = {п.severity: п.threshold for п in default_rules(настройки)
             if п.metric == "asrhub_disk_free_bytes"}
    assert место == {"critical": 50 * гиб, "warning": 100 * гиб}
    assert f"asrhub_disk_free_bytes < {50.0 * гиб}" in exporters.prometheus_rules(настройки)
    assert f"<{50 * гиб}" in exporters.zabbix_template(настройки)
    # И умолчание совпадает с пробой /ready: 5 ГБ и 10 ГБ.
    место = {п.severity: п.threshold for п in default_rules()
             if п.metric == "asrhub_disk_free_bytes"}
    assert место == {"critical": 5 * гиб, "warning": 10 * гиб}


def test_правила_pushgateway_и_одна_тревога_на_одно_условие():
    текст = exporters.prometheus_rules()
    assert "push_time_seconds" in текст, "молчание Pushgateway не ловится ничем"
    правила = default_rules()
    for метрика in ("asrhub_engines_available", "asrhub_llm_available"):
        assert sum(1 for п in правила if п.metric == метрика) == 1, метрика


# ---------------------------------------------------------------------------
# Встроенные тревоги
# ---------------------------------------------------------------------------


def test_свои_правила_с_разными_порогами_не_делят_состояние():
    """Предупредить на 200 и на 500 — две тревоги, а не одна на двоих."""
    движок = AlertEngine(rules=[
        Rule("asrhub_queue_depth", "above", 200, for_seconds=0),
        Rule("asrhub_queue_depth", "above", 500, for_seconds=0),
        Rule("asrhub_queue_depth", "above", 200, for_seconds=0)])
    assert len(движок.rules) == 2, "повтор правила не отброшен"
    for _ in range(3):
        движок.evaluate([Sample("asrhub_queue_depth", 300)])
    состояния = {с["threshold"]: с["state"] for с in движок.states()}
    assert состояния == {200: STATE_FIRING, 500: STATE_OK}


def test_признак_включительности_из_настроек():
    неназван = Rule.from_dict({"metric": "asrhub_queue_paused", "direction": "above",
                               "threshold": 1})
    assert неназван.inclusive is True and неназван.breached(1)
    строкой = Rule.from_dict({"metric": "asrhub_queue_depth", "direction": "above",
                              "threshold": 5, "inclusive": "false"})
    assert строкой.inclusive is False and not строкой.breached(5)


def test_тревога_не_дребезжит_у_порога(monkeypatch: pytest.MonkeyPatch):
    from asrhub.monitoring import alerts as модуль

    часы = Часы()
    monkeypatch.setattr(модуль, "time", часы)
    движок = AlertEngine(rules=[Rule("asrhub_queue_depth", "above", 50, for_seconds=600)])
    for _ in range(2):
        движок.evaluate([Sample("asrhub_queue_depth", 51)])
        часы.t += 600
    assert движок.states()[0]["state"] == STATE_FIRING
    for шаг in (10, 100, 100):
        часы.t += шаг
        движок.evaluate([Sample("asrhub_queue_depth", 49)])
        assert движок.states()[0]["state"] == STATE_FIRING, "снята на первом же колебании"
    часы.t += 5
    движок.evaluate([Sample("asrhub_queue_depth", 52)])      # снова выше — отсчёт заново
    часы.t += 250
    движок.evaluate([Sample("asrhub_queue_depth", 49)])
    часы.t += 250
    движок.evaluate([Sample("asrhub_queue_depth", 49)])
    assert движок.states()[0]["state"] == STATE_FIRING
    часы.t += 60
    движок.evaluate([Sample("asrhub_queue_depth", 49)])
    assert движок.states()[0]["state"] == STATE_OK


def test_снятие_по_пропаже_метрики_попадает_в_ленту(monkeypatch: pytest.MonkeyPatch):
    from asrhub.monitoring import alerts as модуль

    часы = Часы()
    monkeypatch.setattr(модуль, "time", часы)
    смены: list[tuple[str, str]] = []
    движок = AlertEngine(rules=[Rule("asrhub_queue_depth", "above", 5, for_seconds=0)],
                         on_change=lambda с, было: смены.append((было, с.state)))
    for _ in range(2):
        движок.evaluate([Sample("asrhub_queue_depth", 9)])
    for _ in range(12):
        часы.t += 60
        движок.evaluate([])
    assert ("firing", "ok") in смены, смены


# ---------------------------------------------------------------------------
# Сбор
# ---------------------------------------------------------------------------


def test_упавший_источник_виден_метрикой(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    def упасть(self: Collector, out: list[Sample]) -> None:
        raise RuntimeError("база занята")

    monkeypatch.setattr(Collector, "_quality", упасть)
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as клиент:
        текст = клиент.get("/api/monitoring/metrics").text
    assert _значение(текст, 'asrhub_collector_source_up{source="quality"}') == 0.0
    assert _значение(текст, 'asrhub_collector_source_up{source="queue"}') == 1.0


def test_счётчики_выложены_нулями_с_запуска(client):
    """Первый отказ после перезапуска rate() иначе не видит."""
    текст = client.get("/api/monitoring/metrics").text
    assert 'asrhub_jobs_total{status="failed"}' in текст
    assert 'asrhub_webhooks_total{result="failed"}' in текст


def test_соблюдение_скрипта_только_при_заданном_скрипте(monkeypatch: pytest.MonkeyPatch):
    """Готовый скрипт службы поддержки на совещаниях — круглосуточная тревога."""
    from asrhub import insights

    monkeypatch.setattr(insights.Insights, "summary", lambda self, period: {
        "records": 5, "compliance": 0.1, "agent_score": 20.0, "sentiment": 0.3})
    monkeypatch.setattr(insights.Insights, "categories",
                        lambda self, period: {"items": [], "trackers": []})

    def собрать(скрипт: list[Any]) -> set[str]:
        значения = {"content_script": скрипт}
        состояние = SimpleNamespace(
            db=None,
            content=SimpleNamespace(status=lambda: {"enabled": True, "analyzed": 5,
                                                    "pending": 0}),
            settings=SimpleNamespace(get=lambda к, п=None: значения.get(к, п)))
        снимок: list[Sample] = []
        Collector(состояние)._content(снимок)
        return {п.name for п in снимок}

    без = собрать([])
    assert "asrhub_content_sentiment_avg" in без
    assert "asrhub_content_compliance_avg" not in без
    assert "asrhub_content_agent_score_avg" not in без
    со = собрать([{"id": "greet", "label": "Приветствие", "phrases": ["здравствуйте"]}])
    assert {"asrhub_content_compliance_avg", "asrhub_content_agent_score_avg"} <= со


def test_доля_неуверенных_тем_же_порогом_что_аналитика(data_dir: Path,
                                                      monkeypatch: pytest.MonkeyPatch):
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as клиент:
        db = app.state.hub.db
        сейчас = time.time()
        for номер, уверенность in enumerate((0.72, 0.73, 0.95)):
            db.create_job({"id": f"c{номер}", "filename": "a.wav", "status": "completed",
                           "avg_confidence": уверенность, "created_at": сейчас,
                           "finished_at": сейчас})
        текст = клиент.get("/api/monitoring/metrics").text
        обзор = клиент.get("/api/analytics/overview?period=day").json()
    assert _значение(текст, "asrhub_low_confidence_share") == round(2 / 3, 4)
    assert обзор["quality"]["low_confidence_jobs"] == 2


def test_время_поиска_речи_хранится_и_выгружается(client, sample_wav: Path):
    """Стадии vad и diarization справочник обещал, а база не хранила."""
    with sample_wav.open("rb") as файл:
        ответ = client.post("/api/jobs", files={"file": ("а.wav", файл, "audio/wav")},
                            data={"settings": json.dumps({"model": "demo-simulator",
                                                          "engine": "demo",
                                                          "vad_backend": "energy"})})
    ид = ответ.json()["id"]
    for _ in range(120):
        задание = client.get(f"/api/jobs/{ид}").json()
        if задание["status"] in ("completed", "failed"):
            break
        time.sleep(0.25)
    assert задание["status"] == "completed", задание.get("error_message")
    строка = client.app.state.hub.db.query_one("SELECT vad_s FROM jobs WHERE id=?", (ид,))
    assert строка["vad_s"] is not None, "время поиска речи не записано"
    client.app.state.hub.db.update_job(ид, diarization_s=4.5)
    client.app.state.hub.monitoring.cache_ttl_s = 0
    текст = client.get("/api/monitoring/metrics").text
    assert 'asrhub_stage_seconds{stage="vad"}' in текст
    assert _значение(текст, 'asrhub_stage_seconds{stage="diarization"}') == 4.5


def _каталог_с_ссылками(корень: Path) -> tuple[Path, int]:
    """Кеш Hugging Face в миниатюре: blobs и ссылки на них из snapshots."""
    модель = корень / "models" / "hub" / "models--org--m"
    (модель / "blobs").mkdir(parents=True)
    (модель / "snapshots" / "rev").mkdir(parents=True)
    (модель / "blobs" / "abc").write_bytes(b"\0" * 1_000_000)
    (модель / "snapshots" / "rev" / "model.bin").symlink_to(Path("../../blobs/abc"))
    снаружи = корень / "другой-диск" / "веса.bin"
    снаружи.parent.mkdir(parents=True)
    снаружи.write_bytes(b"\0" * 300_000)
    (корень / "models" / "a.bin").symlink_to(снаружи)
    (корень / "models" / "b.bin").symlink_to(снаружи)
    return корень / "models", 1_300_000


def test_размер_каталога_моделей_не_двоится_на_ссылках(tmp_path: Path):
    from asrhub import model_files, selfcheck

    модели, настоящий = _каталог_с_ссылками(tmp_path)
    данные = tmp_path / "данные"
    Пути = SimpleNamespace(uploads=данные / "u", results=данные / "r",
                           models=данные / "m", logs=данные / "l")
    состояние = SimpleNamespace(settings=SimpleNamespace(
        paths=Пути, get=lambda к, п=None: str(модели) if к == "models_dir" else п))
    размеры = {п.labels["kind"]: п.value for п in Collector(состояние)._expensive()}
    assert размеры["models"] == настоящий
    assert model_files.directory_size(модели) == настоящий
    assert selfcheck._размер_каталога(модели)[0] == настоящий


def test_размер_каталогов_пересчитывается_в_фоне(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch):
    """Опрос, попавший на истёкший кеш, не ждёт обхода каталогов."""
    from asrhub import model_files

    каталог = tmp_path / "результаты"
    каталог.mkdir()
    (каталог / "a").write_bytes(b"\0" * 100)
    Пути = SimpleNamespace(uploads=None, results=каталог, models=None, logs=None)
    состояние = SimpleNamespace(settings=SimpleNamespace(paths=Пути,
                                                         get=lambda к, п=None: п))
    сборщик = Collector(состояние, expensive_interval_s=0.2)
    assert [п.value for п in сборщик._expensive()] == [100.0]
    настоящий = model_files.размер_каталога

    def медленно(*args: Any, **kwargs: Any) -> tuple[int, int, bool]:
        time.sleep(0.6)
        return настоящий(*args, **kwargs)

    monkeypatch.setattr(model_files, "размер_каталога", медленно)
    (каталог / "b").write_bytes(b"\0" * 50)
    time.sleep(0.25)
    начало = time.monotonic()
    старое = [п.value for п in сборщик._expensive()]
    assert time.monotonic() - начало < 0.3, "опрос ждал обхода каталогов"
    assert старое == [100.0]
    сборщик._expensive_thread.join(timeout=5)
    assert [п.value for п in сборщик._expensive()] == [150.0]


def test_первый_замер_размеров_не_ждёт_первый_опрос(data_dir: Path,
                                                    monkeypatch: pytest.MonkeyPatch):
    """Первый сбор после перезапуска не платит за обход каталогов."""
    from asrhub import model_files

    настоящий = model_files.размер_каталога

    def медленно(*args: Any, **kwargs: Any) -> tuple[int, int, bool]:
        time.sleep(0.8)
        return настоящий(*args, **kwargs)

    monkeypatch.setattr(model_files, "размер_каталога", медленно)
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app) as клиент:
        начало = time.monotonic()
        текст = клиент.get("/api/monitoring/metrics").text
        assert time.monotonic() - начало < 0.7, "первый опрос ждал обхода каталогов"
        assert 'asrhub_collector_source_up{source="storage_size"} 1' in текст
        сборщик = app.state.hub.monitoring.collector
        сборщик._expensive_thread.join(timeout=20)
        app.state.hub.monitoring.cache_ttl_s = 0
        assert 'asrhub_storage_bytes{kind="results"}' in клиент.get(
            "/api/monitoring/metrics").text


# ---------------------------------------------------------------------------
# Служба мониторинга
# ---------------------------------------------------------------------------


def test_кеш_ноль_значит_без_кеша(client):
    служба = client.app.state.hub.monitoring
    ответ = client.put("/api/settings", json={"monitoring_cache_ttl_s": 0})
    assert ответ.status_code == 200, ответ.text
    assert служба.cache_ttl_s == 0.0
    было = служба._scrapes
    служба.samples()
    служба.samples()
    assert служба._scrapes == было + 2


def test_сбор_снимка_идёт_один_за_раз(client, monkeypatch: pytest.MonkeyPatch):
    служба = client.app.state.hub.monitoring
    служба.cache_ttl_s = 0
    настоящий = служба.collector.collect
    вызовов = []

    def медленный() -> Any:
        вызовов.append(1)
        time.sleep(0.4)
        return настоящий()

    monkeypatch.setattr(служба.collector, "collect", медленный)
    потоки = [threading.Thread(target=служба.samples) for _ in range(6)]
    for поток in потоки:
        поток.start()
    for поток in потоки:
        поток.join(timeout=10)
    assert len(вызовов) == 1, f"снимок собирали {len(вызовов)} раз параллельно"


def test_тревоги_считаются_без_опроса(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """Без Prometheus и без открытой вкладки встроенные тревоги не считались."""
    from asrhub.monitoring import service

    monkeypatch.setattr(service, "ОЦЕНКА_ТРЕВОГ_С", 0.05)
    app = _приложение(data_dir, monkeypatch)
    with TestClient(app):
        служба = app.state.hub.monitoring
        конец = time.time() + 5
        while служба._scrapes == 0 and time.time() < конец:
            time.sleep(0.05)
        assert служба._scrapes > 0


def test_настройки_мониторинга_действуют_сразу(client):
    """Правило из «Настроек» — сразу, пустой список — пороги каталога."""
    своё = [{"metric": "asrhub_queue_depth", "direction": "above", "threshold": 5,
             "severity": "critical", "for_seconds": 0}]
    assert client.put("/api/settings", json={"monitoring_rules": своё}).status_code == 200
    правила = client.get("/api/monitoring/alerts/rules").json()["rules"]
    assert [(п["metric"], п["threshold"]) for п in правила] == [("asrhub_queue_depth", 5.0)]
    assert client.put("/api/settings", json={"monitoring_rules": []}).status_code == 200
    assert len(client.get("/api/monitoring/alerts/rules").json()["rules"]) > 20


def test_выключенная_отправка_останавливается(client):
    служба = client.app.state.hub.monitoring
    приёмник = {"kind": "influxdb", "url": "http://127.0.0.1:9/write", "interval_s": 60}
    assert client.post("/api/monitoring/targets", json=приёмник).status_code == 200
    assert служба.push._thread is not None
    assert client.put("/api/settings",
                      json={"monitoring_push_enabled": False}).status_code == 200
    assert служба.push._thread is None


def test_порог_места_из_настроек_доходит_до_тревог(client):
    assert client.put("/api/settings", json={"disk_min_free_gb": 50}).status_code == 200
    правила = client.get("/api/monitoring/alerts/rules").json()["rules"]
    место = {п["severity"]: п["threshold"] for п in правила
             if п["metric"] == "asrhub_disk_free_bytes"}
    assert место["critical"] == 50 * 1024 ** 3
    файл = client.get("/api/monitoring/config/prometheus").text
    assert f"asrhub_disk_free_bytes < {50.0 * 1024 ** 3}" in файл


# ---------------------------------------------------------------------------
# Приёмники: интерфейс, сохранение, секреты
# ---------------------------------------------------------------------------


def test_добавление_приёмника_не_стирает_соседей(client):
    """Заголовок, база и «выключен» у прежнего приёмника остаются."""
    прежний = {"name": "prod", "kind": "influxdb", "url": "http://influx:8086",
               "headers": {"Authorization": "Token секрет"}, "database": "prod",
               "enabled": False, "timeout_s": 3}
    assert client.put("/api/monitoring/targets", json=[прежний]).status_code == 200
    список = client.get("/api/monitoring/targets").json()["targets"]
    assert список[0]["headers"] == {"Authorization": "***"}, "значение заголовка ушло наружу"
    # Ровно то, что раньше делал интерфейс: собрать список из ответа и
    # отправить обратно с новым приёмником.
    обратно = [*список, {"kind": "statsd", "url": "udp://127.0.0.1:9"}]
    assert client.put("/api/monitoring/targets", json=обратно).status_code == 200
    ответ = client.post("/api/monitoring/targets",
                        json={"kind": "statsd", "url": "udp://127.0.0.1:9"})
    assert ответ.json()["target"]["name"] == "statsd-2"
    приёмники = {п.name: п for п in client.app.state.hub.monitoring.push.target_list()}
    assert приёмники["prod"].headers == {"Authorization": "Token секрет"}
    assert приёмники["prod"].database == "prod"
    assert приёмники["prod"].enabled is False and приёмники["prod"].timeout_s == 3
    assert client.delete("/api/monitoring/targets/statsd-2").status_code == 200
    assert {п.name for п in client.app.state.hub.monitoring.push.target_list()} == {
        "prod", "statsd"}


def test_приёмники_и_правила_переживают_перезапуск(data_dir: Path,
                                                  monkeypatch: pytest.MonkeyPatch):
    """Записываются в config.yaml — и только они, без «применённого на пробу»."""
    from asrhub.config import load

    app = _приложение(data_dir, monkeypatch,
                      config={"server": {"max_upload_mb": 2048}, "monitoring": {}})
    with TestClient(app) as клиент:
        assert клиент.put("/api/settings", json={"max_upload_mb": 100}).status_code == 200
        ответ = клиент.post("/api/monitoring/targets", json={
            "kind": "influxdb", "url": "http://influx:8086", "name": "influx",
            "headers": {"Authorization": "Token x"}})
        assert ответ.json()["persisted"] is True, ответ.text
        правило = [{"metric": "asrhub_queue_depth", "direction": "above",
                    "threshold": 300, "severity": "warning", "for_seconds": 60}]
        assert клиент.put("/api/monitoring/alerts/rules",
                          json=правило).json()["persisted"] is True
    после = load()
    приёмники = после.get("monitoring_targets")
    assert [п["name"] for п in приёмники] == ["influx"]
    assert приёмники[0]["headers"] == {"Authorization": "Token x"}
    # Имя машины не вписывается: с общей настройкой второй сервер слал бы
    # в Pushgateway под чужим `instance` и затирал первый.
    assert (приёмники[0]["instance"], приёмники[0]["host"]) == ("", "")
    assert [(п["metric"], п["threshold"]) for п in после.get("monitoring_rules")] == [
        ("asrhub_queue_depth", 300.0)]
    assert после.get("max_upload_mb") == 2048, "в файл ушло применённое на пробу"


def test_настройки_не_отдают_заголовки_приёмников(client):
    client.post("/api/monitoring/targets", json={
        "kind": "influxdb", "url": "http://influx:8086/write?db=a&p=пароль", "name": "i",
        "headers": {"Authorization": "Token x"}})
    значения = client.get("/api/settings").json()["values"]
    приёмник = значения["monitoring_targets"][0]
    assert приёмник["headers"] == {"Authorization": "***"}
    # Отправили обратно то, что получили, — заголовок остался настоящим.
    assert client.put("/api/settings",
                      json={"monitoring_targets": значения["monitoring_targets"]}
                      ).status_code == 200
    приёмники = client.app.state.hub.monitoring.push.target_list()
    assert приёмники[0].headers == {"Authorization": "Token x"}


def test_правка_списка_не_обнуляет_состояние_приёмника():
    приёмник = Target.from_dict({"kind": "influxdb", "url": "http://a:8086", "name": "a"})
    менеджер = PushManager(lambda: [], [приёмник])
    менеджер._states["a"].sent = 3
    менеджер.set_targets([Target.from_dict({"kind": "influxdb", "url": "http://a:8086",
                                            "name": "a", "interval_s": 30})])
    assert менеджер._states["a"].sent == 3
    менеджер.set_targets([Target.from_dict({"kind": "influxdb", "url": "http://b:8086",
                                            "name": "a"})])
    assert менеджер._states["a"].sent == 0


# ---------------------------------------------------------------------------
# Фрагмент для Prometheus, пробы, выключенный экспорт
# ---------------------------------------------------------------------------


PROMETHEUS_YML = """global:
  scrape_interval: 15s
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ['n:9100']
"""


def test_фрагмент_сбора_вставляется_под_scrape_configs(client):
    """Второй ключ scrape_configs строгий Prometheus отвергает, нестрогий — теряет."""
    фрагмент = client.get("/api/monitoring/config/prometheus-scrape",
                          headers={"host": "asr.example:8443"}).text
    настройка = yaml.safe_load(PROMETHEUS_YML + "\n".join(
        "  " + строка for строка in фрагмент.splitlines()) + "\n")
    задания = {з["job_name"]: з for з in настройка["scrape_configs"]}
    assert set(задания) == {"node", "asrhub"}
    assert задания["asrhub"]["static_configs"][0]["targets"] == ["asr.example:8443"]


def test_фрагмент_сбора_не_берёт_адрес_всех_интерфейсов(client):
    текст = client.get("/api/monitoring/config/prometheus-scrape",
                       headers={"host": "0.0.0.0:8080"}).text
    assert "0.0.0.0" not in текст
    плохой = client.get("/api/monitoring/config/prometheus-scrape",
                        params={"target": "a']\n- job_name: x"})
    assert плохой.status_code == 400


def test_пробы_сервера_без_очереди(data_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """`--no-queue` — «только интерфейс», а не мёртвый процесс."""
    app = _приложение(data_dir, monkeypatch, start_queue=False)
    with TestClient(app) as клиент:
        живость = клиент.get("/api/monitoring/live")
        запуск = клиент.get("/api/monitoring/startup")
    assert живость.status_code == 200, живость.text
    assert запуск.status_code == 200, запуск.text
    assert "--no-queue" in живость.text


def test_проба_не_выдаёт_путь_каталога_данных(client, monkeypatch: pytest.MonkeyPatch):
    import sqlite3

    db = client.app.state.hub.db
    каталог = str(client.app.state.hub.settings.paths.data)

    def упасть(*_: Any, **__: Any) -> None:
        raise sqlite3.OperationalError(f"unable to open database file {каталог}/asrhub.db")

    monkeypatch.setattr(db, "query_one", упасть)
    ответ = client.get("/api/monitoring/ready")
    assert ответ.status_code == 503
    assert каталог not in ответ.text


def test_выключенный_экспорт_закрывает_оба_адреса(client):
    assert client.put("/api/settings", json={"metrics_enabled": False}).status_code == 200
    новый = client.get("/api/monitoring/metrics")
    assert новый.status_code == 404
    assert новый.json()["detail"]["code"] == "metrics_disabled"
    assert client.get("/api/monitoring/metrics.json").status_code == 404
    assert client.get("/api/metrics").status_code == 404
    assert client.get("/api/monitoring/health").status_code in (200, 503)


def test_send_без_состояния_ничего_не_запоминает():
    """Разовая отправка (без состояния рассылки) ни на что прошлое не опирается."""
    гнездо, адрес = _statsd_приём()
    try:
        приёмник = Target.from_dict({"kind": "statsd", "url": адрес})
        состояние = TargetState(target=приёмник)
        send(приёмник, [Sample("asrhub_jobs_total", 7, {"status": "ok"})], состояние)
        send(приёмник, [Sample("asrhub_jobs_total", 7, {"status": "ok"})], None)
        строки = _прочитать(гнездо)
    finally:
        гнездо.close()
    assert строки == ["asrhub.jobs_total.status.ok:7|c", "asrhub.jobs_total.status.ok:0|c"]
    assert состояние.statsd_last == {"asrhub.jobs_total.status.ok": 7.0}
