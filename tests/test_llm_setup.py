"""Установка языковой модели: каталог, подбор под железо, установщик.

Настоящую установку набор тестов провести не может — она качает
гигабайты и ставит службу в систему. Поэтому здесь поддельная служба
Ollama и поддельное железо, а проверяется то, что решает: правильно ли
считается свободная память, честно ли показаны модели, которые не
влезут, идут ли шаги по порядку, пропускается ли сделанное, доходит ли
дело до записи настроек и останавливается ли всё это по отмене.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from asrhub.errors import ConfigError
from asrhub.hardware import GPUInfo, HardwareInfo
from asrhub.llm import provision


class _Настройки:
    """Заглушка настроек с записью в файл, как у настоящих."""

    def __init__(self, tmp_path: Path, **значения):
        self.values = {"llm_backend": "off", "llm_url": provision.АДРЕС,
                       "llm_model": "", "llm_min_free_vram_gb": 0, **значения}
        self.paths = type("Пути", (), {"data": tmp_path})()
        self.config_file = tmp_path / "config.yaml"
        self.сохранено = 0

    def get(self, ключ, по_умолчанию=None):
        return self.values.get(ключ, по_умолчанию)

    def set(self, ключ, значение, source="runtime"):
        self.values[ключ] = значение

    def save(self, путь=None):
        цель = Path(путь or self.config_file)
        цель.write_text(json.dumps(self.values, ensure_ascii=False), encoding="utf-8")
        self.сохранено += 1
        return цель


def _железо(*, vram_gb: float = 32, свободно_gb: float = 30, диск_gb: float = 900,
            карта: bool = True) -> HardwareInfo:
    return HardwareInfo(
        os_name="Linux", os_version="6.8", arch="x86_64", cpu_model="AMD EPYC",
        cpu_cores_physical=16, cpu_cores_logical=32, ram_total_gb=128,
        ram_available_gb=100, disk_free_gb=диск_gb,
        gpus=[GPUInfo(index=0, name="NVIDIA GeForce RTX 5090",
                      memory_total_mb=int(vram_gb * 1024),
                      memory_free_mb=int(свободно_gb * 1024))] if карта else [],
        accelerator="cuda" if карта else "cpu")


class _Ответ:
    """Поддельный ответ urlopen: и как файл, и как поток строк."""

    def __init__(self, строки: list[bytes] = (), тело: bytes = b""):
        self.строки = list(строки)
        self.тело = тело

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(self.строки)

    def read(self):
        return self.тело


def _сеть(monkeypatch, *, скачано=(), версия="0.12.4", тянуть=None, манифест=None):
    """Подменяет службу Ollama и реестр: сети в проверках нет."""
    def запрос(method, url, body=None, *, timeout=10.0, headers=None):
        if url.endswith("/api/version"):
            return {"version": версия}
        if url.endswith("/api/tags"):
            return {"models": [{"name": и, "size": 5 * 1024 ** 3} for и in скачано]}
        if url.endswith("/api/chat"):
            return {"message": {"content": '{"ok": true, "lang": "ru"}'}}
        if "/manifests/" in url:
            return манифест if манифест is not None else {
                "layers": [{"size": 5 * 1024 ** 3}, {"size": 1024 ** 2}],
                "config": {"size": 1024}}
        if url.endswith("/api/delete"):
            return {}
        raise AssertionError(f"неожиданный запрос: {url}")

    monkeypatch.setattr(provision, "_запрос", запрос)

    строки = тянуть if тянуть is not None else [
        b'{"status":"pulling manifest"}\n',
        b'{"status":"pulling 5f4c","total":5368709120,"completed":2684354560}\n',
        b'{"status":"pulling 5f4c","total":5368709120,"completed":5368709120}\n',
        b'{"status":"success"}\n']

    def urlopen(запрос_объект, timeout=None):
        адрес = getattr(запрос_объект, "full_url", str(запрос_объект))
        if адрес.endswith("/api/pull"):
            return _Ответ(строки)
        return _Ответ(тело=b"#!/bin/sh\necho ok\n")

    monkeypatch.setattr(provision.urllib.request, "urlopen", urlopen)


# ---------------------------------------------------------------------------
# Каталог и подбор
# ---------------------------------------------------------------------------


def test_the_catalogue_is_matched_against_free_memory_not_the_whole_card():
    """Считается свободная видеопамять за вычетом запаса под распознавание.

    Обещать модели всю карту — значит обещать чужое: распознавание уже
    держит на ней свои веса. Модель, которая не влезет, из списка не
    убирается: владельцу сервера нужна причина, а не короткий список.
    """
    свод = provision.подобрать(_железо(vram_gb=32, свободно_gb=30),
                               settings=_Настройки(Path("/tmp")))
    б = свод["hardware"]
    assert б["kind"] == "gpu" and б["free_gb"] == 30.0
    assert б["budget_gb"] == 30.0 - provision.ЗАПАС_ГБ
    по_имени = {м["name"]: м for м in свод["models"]}
    assert len(по_имени) == len(provision.КАТАЛОГ), "модели пропали из списка"
    assert по_имени["qwen3:30b"]["state"] == "да"
    assert по_имени["gpt-oss:120b"]["state"] == "нет"
    assert "72" in по_имени["gpt-oss:120b"]["note"]
    # Рекомендуется самая крупная из поместившихся, а рядом — быстрая.
    assert свод["recommended"] == "qwen3:30b"
    assert по_имени[свод["fast_pick"]]["vram_gb"] <= по_имени["qwen3:30b"]["vram_gb"] / 2

    # Занятая карта: крупная модель уже не помещается, но и не объявляется
    # невозможной — память есть, она занята.
    занята = provision.подобрать(_железо(vram_gb=32, свободно_gb=14),
                                 settings=_Настройки(Path("/tmp")))
    занята_по_имени = {м["name"]: м for м in занята["models"]}
    assert занята_по_имени["qwen3:30b"]["state"] == "после освобождения"
    assert "занята" in занята_по_имени["qwen3:30b"]["note"]
    assert занята["recommended"] == "qwen3:8b"

    # Карта занята целиком: советовать нечего, и это говорится прямо, а не
    # подсовыванием модели, которая не запустится.
    забита = provision.подобрать(_железо(vram_gb=32, свободно_gb=7),
                                 settings=_Настройки(Path("/tmp")))
    assert забита["recommended"] is None
    assert all(м["state"] != "да" for м in забита["models"])

    # Запас берётся из настройки, если она задана.
    свой_запас = provision.подобрать(
        _железо(vram_gb=32, свободно_gb=30),
        settings=_Настройки(Path("/tmp"), llm_min_free_vram_gb=20))
    assert свой_запас["hardware"]["budget_gb"] == 10.0
    assert свой_запас["recommended"] == "qwen3:8b"


def test_without_a_card_the_catalogue_says_so_plainly():
    """Без видеокарты модель считается по оперативной памяти и с оговоркой
    о минутах на запись: молчать об этом — обмануть ожидания."""
    свод = provision.подобрать(_железо(карта=False), settings=None)
    assert свод["hardware"]["kind"] == "cpu"
    assert "процессоре" in свод["hardware"]["note"]
    крупная = next(м for м in свод["models"] if м["name"] == "qwen3:14b")
    assert "минутами" in крупная["note"]


def test_installed_models_are_marked(monkeypatch):
    """Скачанная модель помечена — иначе её предложат скачать ещё раз."""
    свод = provision.подобрать(_железо(), installed=["qwen3:14b:latest", "qwen3:8b"])
    по_имени = {м["name"]: м for м in свод["models"]}
    assert по_имени["qwen3:8b"]["installed"] is True
    assert по_имени["qwen3:14b"]["installed"] is True
    assert по_имени["qwen3:32b"]["installed"] is False


def test_the_registry_answers_how_much_the_tag_really_weighs(monkeypatch):
    """Размер берётся из реестра: каталог в коде стареет, реестр — нет."""
    _сеть(monkeypatch)
    размер, ошибка = provision.размер_в_реестре("qwen3:8b")
    assert ошибка == "" and размер == 5.0

    def нет_такого(*a, **k):
        raise provision.urllib.error.HTTPError("u", 404, "Not Found", {}, None)

    monkeypatch.setattr(provision, "_запрос", нет_такого)
    размер, ошибка = provision.размер_в_реестре("qwen3:nosuch")
    assert размер is None and "нет модели" in ошибка
    assert provision.размер_в_реестре("../../etc/passwd")[1] == "Недопустимое имя модели."


# ---------------------------------------------------------------------------
# Установщик
# ---------------------------------------------------------------------------


def test_the_installer_walks_the_steps_and_writes_the_settings(tmp_path, monkeypatch):
    """Полный проход: проверка, скачивание, прогрев, настройки.

    Сделанное пропускается — Ollama уже стоит и отвечает, значит шаги
    установки и запуска не трогаются. В конце настройки не только
    применены, но и записаны в файл: иначе после перезапуска сервер
    забудет модель, которую сам же поставил.
    """
    настройки = _Настройки(tmp_path)
    _сеть(monkeypatch)
    monkeypatch.setattr(provision.shutil, "which",
                        lambda имя: "/usr/local/bin/ollama" if имя == "ollama" else None)
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    начало = установщик.start(["qwen3:8b"], activate="qwen3:8b")
    assert начало["running"] is True and начало["models"] == ["qwen3:8b"]
    assert установщик.wait(30), "установка не закончилась"

    с = установщик.status()
    assert с["error"] is None and с["running"] is False and с["progress"] == 1.0
    состояния = {ш["key"]: ш["state"] for ш in с["steps"]}
    assert состояния == {"проверка": "готово", "установка": "пропущен",
                         "запуск": "пропущен", "скачивание": "готово",
                         "прогрев": "готово", "настройка": "готово"}
    assert с["model_progress"]["qwen3:8b"]["share"] == 1.0
    assert настройки.get("llm_backend") == "ollama"
    assert настройки.get("llm_model") == "qwen3:8b"
    assert настройки.сохранено == 1, "настройки не записаны в файл"
    assert any("ГБ по реестру" in строка for строка in с["log"])
    assert any("Пробный ответ" in строка for строка in с["log"])


def test_an_already_downloaded_model_is_not_downloaded_again(tmp_path, monkeypatch):
    """Повторный запуск на настроенном сервере ничего не ломает."""
    настройки = _Настройки(tmp_path)
    тянули = []
    _сеть(monkeypatch, скачано=["qwen3:8b"])
    monkeypatch.setattr(provision.shutil, "which", lambda имя: "/usr/local/bin/ollama")
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    monkeypatch.setattr(установщик, "_тянуть",
                        lambda имя, адрес: тянули.append(имя))
    установщик.start(["qwen3:8b"])
    assert установщик.wait(30)
    assert тянули == [], "уже скачанная модель качается заново"
    assert установщик.status()["error"] is None


def test_no_room_on_the_disk_stops_before_anything_is_downloaded(tmp_path, monkeypatch):
    """Место проверяется до скачивания и на том разделе, куда лягут веса.

    Узнать о нехватке на девятнадцатом гигабайте — значит потратить полчаса
    зря. Мерить не тот раздел — то же самое: каталог данных сервера и
    каталог весов Ollama на разных дисках бывают чаще, чем кажется.
    """
    настройки = _Настройки(tmp_path)
    _сеть(monkeypatch)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: "/usr/local/bin/ollama")
    monkeypatch.setattr(provision, "свободно_под_веса",
                        lambda: (3.0, "/var/lib/ollama/models"))
    установщик = provision.Установщик(настройки, hardware=lambda: _железо(диск_gb=900))
    установщик.start(["qwen3:32b"])
    assert установщик.wait(30)
    с = установщик.status()
    assert с["error"] and "свободно 3 ГБ" in с["error"]
    assert "/var/lib/ollama/models" in с["error"], "не сказано, где именно не хватает"
    assert {ш["key"]: ш["state"] for ш in с["steps"]}["проверка"] == "сбой"
    assert настройки.get("llm_backend") == "off", "настройки тронуты при сбое"


def test_the_free_space_is_measured_where_ollama_keeps_the_weights(tmp_path, monkeypatch):
    """Каталог весов берётся из OLLAMA_MODELS, потом служебный, потом домашний."""
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "весы"))
    assert provision.каталог_весов() == tmp_path / "весы"
    monkeypatch.delenv("OLLAMA_MODELS")
    monkeypatch.setattr(provision.Path, "home", staticmethod(lambda: tmp_path))
    ожидание = (tmp_path / ".ollama" / "models"
                if not Path("/usr/share/ollama/.ollama/models").exists()
                else Path("/usr/share/ollama/.ollama/models"))
    assert provision.каталог_весов() == ожидание
    # Каталога ещё нет — место меряется по ближайшему существующему предку.
    свободно, куда = provision.свободно_под_веса()
    assert свободно > 0 and куда.endswith("models")


def test_the_download_can_be_cancelled(tmp_path, monkeypatch):
    """Отмена останавливает скачивание, а не только гасит кнопку."""
    настройки = _Настройки(tmp_path)
    _сеть(monkeypatch)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: "/usr/local/bin/ollama")
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    установщик._stop.set()
    установщик._состояние = установщик._пусто()
    установщик._состояние["model_progress"] = {"qwen3:8b": {"share": 0.0, "status": "ждёт"}}
    установщик._тянуть("qwen3:8b", provision.АДРЕС)
    assert установщик.status()["model_progress"]["qwen3:8b"]["status"] == "отменено"
    assert настройки.get("llm_backend") == "off"


def test_the_installer_refuses_nonsense_and_a_second_run(tmp_path, monkeypatch):
    """Имя модели проверяется, а две установки разом не запускаются."""
    настройки = _Настройки(tmp_path)
    _сеть(monkeypatch)
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    with pytest.raises(ConfigError):
        установщик.start(["../../etc/passwd"])
    with pytest.raises(ConfigError):
        установщик.start([])
    with pytest.raises(ConfigError):
        установщик.start(["qwen3:8b"], activate="qwen3:32b")
    установщик._состояние["running"] = True
    with pytest.raises(ConfigError):
        установщик.start(["qwen3:8b"])


def test_the_service_is_started_when_it_does_not_answer(tmp_path, monkeypatch):
    """Служба не отвечает — её поднимают и ждут; не поднялась — говорят,
    где смотреть."""
    настройки = _Настройки(tmp_path)
    ответы = {"жива": False}

    def запрос(method, url, body=None, *, timeout=10.0, headers=None):
        if url.endswith("/api/version"):
            if not ответы["жива"]:
                raise OSError("connection refused")
            return {"version": "0.12.4"}
        raise AssertionError(url)

    monkeypatch.setattr(provision, "_запрос", запрос)
    monkeypatch.setattr(provision.shutil, "which",
                        lambda имя: "/usr/local/bin/ollama" if имя == "ollama" else None)
    запущено = []

    def popen(команда, **kw):
        запущено.append(команда)
        ответы["жива"] = True
        return type("Процесс", (), {"pid": 1})()

    monkeypatch.setattr(provision.subprocess, "Popen", popen)
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    установщик._запуск(provision.АДРЕС)
    assert запущено and запущено[0][:2] == ["ollama", "serve"]
    assert {ш["key"]: ш["state"] for ш in установщик.status()["steps"]}["запуск"] == "готово"


def test_the_installer_explains_itself_on_macos(tmp_path, monkeypatch):
    """На macOS установщик не запускается сам — вместо молчания подсказка."""
    настройки = _Настройки(tmp_path)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: None)
    monkeypatch.setattr(provision.platform, "system", lambda: "Darwin")
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    with pytest.raises(ConfigError) as сбой:
        установщик._установка(True)
    assert "brew install ollama" in str(сбой.value.hint)


# ---------------------------------------------------------------------------
# Ручки
# ---------------------------------------------------------------------------


def test_the_routes_show_the_catalogue_and_guard_the_active_model(client, monkeypatch):
    """Каталог отдаётся с пометками, включённую модель удалить нельзя."""
    from asrhub.llm import provision as п

    monkeypatch.setattr(п, "установленные", lambda адрес, **k: [
        {"name": "qwen3:8b", "size_gb": 5.2, "modified": ""}])
    monkeypatch.setattr(п, "служба", lambda адрес, **k: {"running": True,
                                                         "version": "0.12.4", "url": адрес})
    свод = client.get("/api/llm/models").json()
    assert свод["installed"][0]["name"] == "qwen3:8b"
    assert свод["service"]["running"] is True
    assert len(свод["models"]) == len(п.КАТАЛОГ)
    assert all("state" in м and "why" in м for м in свод["models"])
    assert свод["setup"]["running"] is False

    состояние = client.get("/api/llm/setup/status").json()
    assert состояние["steps"][0]["key"] == "проверка"

    client.app.state.hub.settings.set("llm_model", "qwen3:8b")
    отказ = client.post("/api/llm/models/delete", json={"model": "qwen3:8b"})
    assert отказ.status_code == 400 and "выбрана" in отказ.json()["detail"]["message"]

    удалено = []
    monkeypatch.setattr(п, "удалить", lambda имя, адрес, **k: удалено.append(имя))
    ответ = client.post("/api/llm/models/delete", json={"model": "qwen3:14b"})
    assert ответ.status_code == 200 and удалено == ["qwen3:14b"]


def test_the_setup_route_starts_the_installer_and_reports_it(client, monkeypatch):
    """Кнопка «Установить» ставит задание в работу и отдаёт его состояние."""
    from asrhub.llm import provision as п

    запуски = []
    установщик = client.app.state.hub.llm_setup
    monkeypatch.setattr(установщик, "start",
                        lambda модели, **kw: (запуски.append((модели, kw)),
                                              {"running": True, "models": модели})[1])
    ответ = client.post("/api/llm/setup", json={"models": ["qwen3:8b"],
                                                "activate": "qwen3:8b"}).json()
    assert ответ["running"] is True and запуски[0][0] == ["qwen3:8b"]

    # Без выбора ставится рекомендованная — та же, что показана в каталоге.
    monkeypatch.setattr(п, "подобрать", lambda **k: {"recommended": "qwen3:14b"})
    client.post("/api/llm/setup", json={})
    assert запуски[-1][0] == ["qwen3:14b"]

    monkeypatch.setattr(п, "подобрать", lambda **k: {"recommended": None})
    отказ = client.post("/api/llm/setup", json={})
    assert отказ.status_code == 400 and "не помещается" in отказ.json()["detail"]["message"]

    отмена = client.post("/api/llm/setup/cancel")
    assert отмена.status_code == 200


def test_the_setup_needs_an_administrator(data_dir, monkeypatch):
    """Установка ставит службу в систему — это право администратора."""
    from asrhub.api import create_app
    from asrhub.config import load
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ASRHUB_MODEL", "demo-simulator")
    monkeypatch.setenv("ASRHUB_ENGINE", "demo")
    settings = load()
    settings.set("auth_enabled", True)
    settings.api_keys["ah_read_key"] = {"name": "чтение", "role": "read", "enabled": True}
    settings.api_keys["ah_admin_key"] = {"name": "админ", "role": "admin", "enabled": True}
    with TestClient(create_app(settings, start_queue=False)) as c:
        for путь in ("/api/llm/models", "/api/llm/setup/status"):
            assert c.get(путь, headers={"X-API-Key": "ah_read_key"}).status_code == 403
        assert c.post("/api/llm/setup", json={"models": ["qwen3:8b"]},
                      headers={"X-API-Key": "ah_read_key"}).status_code == 403
        assert c.get("/api/llm/models",
                     headers={"X-API-Key": "ah_admin_key"}).status_code == 200


def test_the_setup_state_survives_a_page_reload(tmp_path, monkeypatch):
    """Ход установки спрашивается у сервера: закрытая вкладка его не теряет."""
    настройки = _Настройки(tmp_path)
    _сеть(monkeypatch)
    monkeypatch.setattr(provision.shutil, "which", lambda имя: "/usr/local/bin/ollama")
    установщик = provision.Установщик(настройки, hardware=lambda: _железо())
    установщик.start(["qwen3:8b"])
    видно = установщик.status()
    assert видно["started_at"] and видно["models"] == ["qwen3:8b"]
    assert установщик.wait(30)
    после = установщик.status()
    assert после["finished_at"] and после["running"] is False
    # Снимок независим от внутреннего состояния: страница правит то, что
    # ей отдали (обрезает журнал, дописывает пометки), и это не должно
    # доходить до установщика.
    после["log"].append("подделка")
    после["steps"][0]["note"] = "подделка"
    после["model_progress"]["qwen3:8b"]["status"] = "подделка"
    свежий = установщик.status()
    assert "подделка" not in свежий["log"]
    assert свежий["steps"][0]["note"] != "подделка"
    assert свежий["model_progress"]["qwen3:8b"]["status"] != "подделка"
    assert time.time() - после["finished_at"] < 60
