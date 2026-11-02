"""Заход 54: каталог параметров, пресеты и настройки сервера (ревизия 3.1.20, 08-params)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest


def _файл(путь: Path, текст: str) -> Path:
    путь.write_text(текст, encoding="utf-8")
    return путь


# ---------------------------------------------------------------------------
# №1: закреплённый движок не подменяет модель задания
# ---------------------------------------------------------------------------

def test_модель_задания_снимает_чужой_движок_сервера(data_dir):
    from asrhub.config import load

    настройки = load()
    настройки.values["engine"] = "gigaam"           # как после пресета «Русский: точность»
    итог = настройки.merged({"model": "faster-whisper-small"})
    assert итог["engine"] == "auto"
    assert настройки.merged({"model": "gigaam-v3-ctc"})["engine"] == "gigaam"


def test_явная_несовместимая_пара_отвергается(data_dir):
    from asrhub.config import load
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError, match="не умеет"):
        load().merged({"model": "gigaam-v3-rnnt", "engine": "faster_whisper"})
    # Семейство Whisper — общие веса: openai-whisper и whisperx годятся друг другу.
    assert load().merged({"model": "whisper-small", "engine": "whisper"})["engine"] == "whisper"


def test_выбор_модели_в_настройках_сбрасывает_чужой_движок(data_dir):
    from asrhub.config import load

    настройки = load()
    настройки.set("engine", "gigaam")
    настройки.set("model", "faster-whisper-small")
    assert настройки.get("engine") == "auto"
    настройки.set("engine", "faster_whisper")
    настройки.set("model", "faster-whisper-medium")
    assert настройки.get("engine") == "faster_whisper"


def test_пресеты_не_закрепляют_движок_чужой_модели():
    from asrhub import catalog

    for пресет in catalog.PRESETS:
        модель = пресет.values.get("model")
        движок = пресет.values.get("engine", "auto")
        if модель and движок not in ("auto", "demo"):
            spec = catalog.get_model(модель)
            assert движок in catalog.engines_for_model(spec), (пресет.id, модель, движок)


# ---------------------------------------------------------------------------
# №2: «Сбросить» — к тому, что дал бы запуск, и только распознавание
# ---------------------------------------------------------------------------

def test_сброс_не_включает_вход_и_не_стирает_сервер(data_dir):
    from asrhub.config import load

    настройки = load()
    каталог_моделей = настройки.get("models_dir")
    настройки.set("beam_size", 3)
    настройки.set("telephony_host", "pbx.example.local")
    изменено = настройки.сбросить()
    assert "beam_size" in изменено
    assert настройки.get("beam_size") != 3
    assert настройки.get("auth_enabled") is False           # из окружения, как при запуске
    assert настройки.get("models_dir") == каталог_моделей and каталог_моделей
    assert настройки.get("telephony_host") == "pbx.example.local"


def test_сброс_по_api_оставляет_сервер_доступным(client):
    assert client.put("/api/settings", json={"beam_size": 3}).status_code == 200
    ответ = client.post("/api/settings/reset")
    assert ответ.status_code == 200 and "beam_size" in ответ.json()["changed"]
    после = client.get("/api/settings")
    assert после.status_code == 200
    assert после.json()["values"]["beam_size"] != 3
    # Модель сервера из окружения (ASRHUB_MODEL) — как при запуске, а не умолчание каталога.
    assert после.json()["values"]["model"] == "demo-simulator"


# ---------------------------------------------------------------------------
# №3: язык — не одиннадцать значений
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("язык", ["pl", "be", "ky", "pt", "tr", "uz"])
def test_языки_моделей_каталога_задаются(data_dir, язык):
    from asrhub.config import load

    assert load().merged({"language": язык})["language"] == язык


def test_все_языки_моделей_каталога_в_перечне():
    from asrhub import catalog

    допустимые = {о["value"] for о in catalog.get_param("language").options}
    языки = {я for м in catalog.MODELS for я in м.languages if len(я) <= 3}
    assert языки <= допустимые, sorted(языки - допустимые)


def test_выдуманный_язык_отвергается(data_dir):
    """Страж: расширенный перечень по-прежнему перечень, а не «любая строка»."""
    from asrhub.config import load
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError):
        load().merged({"language": "xx"})


# ---------------------------------------------------------------------------
# №4: пример конфигурации загружается тем же сервером
# ---------------------------------------------------------------------------

def test_пример_конфигурации_читается_без_замечаний(data_dir, tmp_path):
    from asrhub.config import generate_example_config, load

    путь = _файл(tmp_path / "config.yaml", generate_example_config())
    настройки = load(путь, ensure_key=False)
    assert настройки.problems == []


# ---------------------------------------------------------------------------
# №5: «Сохранить» — только заданное людьми
# ---------------------------------------------------------------------------

def test_сохранение_не_уносит_окружение(data_dir, tmp_path, monkeypatch):
    import yaml
    from asrhub.config import load

    путь = _файл(tmp_path / "config.yaml",
                 "beam_size: 4\nбудущий_раздел:\n  что-то: 1\n")
    monkeypatch.setenv("ASRHUB_WEBHOOK_SECRET", "секрет-из-окружения")
    monkeypatch.setenv("ASRHUB_BEAM_SIZE", "9")
    monkeypatch.setenv("ASRHUB_API_KEY", "ah_" + "e" * 30)
    настройки = load(путь, ensure_key=False)
    assert настройки.get("beam_size") == 9
    настройки.set("language", "en", source="api")
    настройки.save()
    записано = yaml.safe_load(путь.read_text(encoding="utf-8"))
    плоско = {к: з for раздел in записано.values() if isinstance(раздел, dict)
              for к, з in раздел.items()} | записано
    assert плоско.get("beam_size") == 4
    assert плоско.get("language") == "en"
    assert "webhook_secret" not in плоско and "device" not in плоско
    assert "ah_" + "e" * 30 not in (записано.get("api_keys") or {})
    assert "будущий_раздел" in записано


def test_ключ_из_окружения_после_смены_переменной_не_действует(data_dir, tmp_path, monkeypatch):
    from asrhub.config import load

    старый = "ah_" + "o" * 30
    путь = _файл(tmp_path / "config.yaml",
                 f"api_keys:\n  {старый}:\n    role: admin\n    name: из переменной окружения\n")
    monkeypatch.setenv("ASRHUB_API_KEY", "ah_" + "n" * 30)
    настройки = load(путь, ensure_key=False)
    assert старый not in настройки.api_keys
    assert "ah_" + "n" * 30 in настройки.api_keys


# ---------------------------------------------------------------------------
# №6: PUT /api/settings — всё или ничего
# ---------------------------------------------------------------------------

def test_отказ_в_изменении_ничего_не_применяет(client):
    до = client.get("/api/settings").json()["values"]
    ответ = client.put("/api/settings",
                       json={"beam_size": 3, "max_concurrent_jobs": 5, "model": "опечатка"})
    assert ответ.status_code == 400
    после = client.get("/api/settings").json()["values"]
    assert (после["beam_size"], после["max_concurrent_jobs"]) == (до["beam_size"],
                                                                 до["max_concurrent_jobs"])
    assert client.get("/api/queue").json().get("worker_count") in (None, до["max_concurrent_jobs"])


# ---------------------------------------------------------------------------
# №8: номер карты в `device`
# ---------------------------------------------------------------------------

def test_номер_карты_принимается(client):
    ответ = client.put("/api/settings", json={"device": "cuda:2"})
    assert ответ.status_code == 200, ответ.text
    assert client.get("/api/settings").json()["values"]["device"] == "cuda:2"


# ---------------------------------------------------------------------------
# №10: пресеты не накладываются
# ---------------------------------------------------------------------------

def test_пресет_снимает_значения_прошлого(client):
    до = client.get("/api/settings").json()["values"]
    assert client.post("/api/presets/callcenter/apply").status_code == 200
    assert client.get("/api/settings").json()["values"]["speaker_names"] == "Оператор, Клиент"
    assert client.post("/api/presets/meeting/apply").status_code == 200
    после = client.get("/api/settings").json()["values"]
    assert после["speaker_names"] == до["speaker_names"]
    assert после["audio_channels"] == до["audio_channels"]
    assert после["vad_min_speech_ms"] == до["vad_min_speech_ms"]


# ---------------------------------------------------------------------------
# №11: что видно в GET /api/settings
# ---------------------------------------------------------------------------

def test_адреса_инфраструктуры_и_секреты_спрятаны(data_dir):
    from asrhub.config import load

    настройки = load()
    настройки.set("telephony_host", "pbx.example.local")
    настройки.set("llm_url", "http://user:pass@llm.local:8081")
    настройки.set("webhook_secret", "секрет")
    чужому = настройки.to_dict(for_admin=False)["values"]
    # Заход 54 (слияние с security-api): параметры самого сервера
    # неадминистратору не отдаются вовсе, остальное из АДМИНУ — заглушкой.
    for ключ in ("telephony_host", "llm_url", "models_dir"):
        assert чужому.get(ключ) in (None, "***"), ключ
    assert "pbx.example.local" not in str(чужому) and "user:pass" not in str(чужому)
    админу = настройки.to_dict(for_admin=True)["values"]
    assert админу["telephony_host"] == "pbx.example.local"
    assert админу["webhook_secret"] == "***"


# ---------------------------------------------------------------------------
# №12: заголовки приёмников — по имени и адресу
# ---------------------------------------------------------------------------

def test_заголовки_двух_приёмников_одного_вида_не_путаются(data_dir):
    from asrhub.config import load

    настройки = load()
    настройки.values["monitoring_targets"] = [
        {"kind": "webhook", "url": "https://a.example/hook", "headers": {"Authorization": "A"}},
        {"kind": "webhook", "url": "https://b.example/hook", "headers": {"Authorization": "B"}}]
    наружу = настройки.to_dict(for_admin=True)["values"]["monitoring_targets"]
    assert all(п["headers"]["Authorization"] == "***" for п in наружу)
    обратно = настройки.без_заглушек({"monitoring_targets": наружу})["monitoring_targets"]
    assert [п["headers"]["Authorization"] for п in обратно] == ["A", "B"]


# ---------------------------------------------------------------------------
# №13: построчные параметры, записанные списком YAML
# ---------------------------------------------------------------------------

def test_список_yaml_в_построчном_параметре_склеивается_строками():
    from asrhub import catalog

    assert catalog.coerce_value("hallucination_phrases",
                                ["Продолжение следует", "Спасибо за просмотр"]) \
        == "Продолжение следует\nСпасибо за просмотр"
    карта = catalog.coerce_value("auth_ldap_group_map",
                                 {"asr-admins": "admin", "asr-users": "user"})
    assert карта.splitlines() == ["asr-admins = admin", "asr-users = user"]


def test_карта_групп_списком_даёт_роли(data_dir, tmp_path):
    from asrhub.config import load
    from asrhub.ldap_auth import Настройки

    путь = _файл(tmp_path / "config.yaml",
                 "auth_ldap_group_map:\n  - asr-admins = admin\n  - asr-users = user\n")
    настройки = load(путь, ensure_key=False)
    assert настройки.problems == []
    assert Настройки.из_настроек(настройки).group_map == {"asr-admins": "admin",
                                                          "asr-users": "user"}


# ---------------------------------------------------------------------------
# №14: что действует сразу, а что — после перезапуска
# ---------------------------------------------------------------------------

def test_уровень_журнала_сразу_прочее_называется(client):
    журнал = logging.getLogger("asrhub")
    прежний = журнал.level
    try:
        ответ = client.put("/api/settings", json={"log_level": "DEBUG"})
        assert ответ.status_code == 200
        assert журнал.level == logging.DEBUG
        assert "log_level" not in ответ.json().get("restart_required", [])
        ответ = client.put("/api/settings", json={"cors_origins": "https://a.example"})
        assert ответ.json().get("restart_required") == ["cors_origins"]
    finally:
        журнал.setLevel(прежний)


# ---------------------------------------------------------------------------
# №15: отпечаток кеша — только то, что влияет на расшифровку
# ---------------------------------------------------------------------------

def test_отпечаток_не_зависит_от_потока_и_разбора(data_dir):
    from asrhub.config import load
    from asrhub.processor import settings_digest

    настройки = load()
    база = настройки.merged({})
    с_потоком = настройки.merged({"stream_window_s": 3.0,
                                  "streaming_partial_results": not база.get(
                                      "streaming_partial_results")})
    assert settings_digest(база) == settings_digest(с_потоком)
    разбор = настройки.merged({"content_pause_s": 9.0, "content_script": [
        {"id": "x", "label": "Приветствие", "any": ["добрый день"]}]})
    assert разбор.get("content_pause_s") == база.get("content_pause_s")
    assert settings_digest(база) == settings_digest(разбор)
    assert settings_digest(база) != settings_digest(настройки.merged({"beam_size": 2}))


# ---------------------------------------------------------------------------
# №16: приоритет звонков
# ---------------------------------------------------------------------------

def test_пример_приоритета_звонков_ставит_их_вперёд():
    from asrhub import catalog

    параметр = catalog.get_param("telephony_priority")
    вперёд = next(п for п in параметр.examples if "Вперёд" in п.title)
    assert вперёд.value > параметр.default


# ---------------------------------------------------------------------------
# №18: записи config.yaml не роняют запуск
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("текст", ["8080: x\nbeam_size: 3\n", "- beam_size\n- 3\n"])
def test_нестроковый_ключ_и_список_в_корне(data_dir, tmp_path, текст):
    from asrhub.config import load

    настройки = load(_файл(tmp_path / "config.yaml", текст), ensure_key=False)
    assert настройки.problems


def test_роль_строкой_в_ключах_доступа(data_dir, tmp_path):
    from asrhub.config import load

    ключ, кривой = "ah_" + "r" * 30, "ah_" + "z" * 30
    настройки = load(_файл(tmp_path / "config.yaml",
                           f"api_keys:\n  {ключ}: user\n  {кривой}: 5\n"), ensure_key=False)
    assert настройки.api_keys[ключ] == {"role": "user"}
    assert кривой not in настройки.api_keys and настройки.problems


def test_время_копии_без_кавычек(data_dir, tmp_path):
    from asrhub.config import load

    настройки = load(_файл(tmp_path / "config.yaml", "backup_time: 13:00\n"), ensure_key=False)
    assert настройки.get("backup_time") == "13:00" and настройки.problems == []
    настройки = load(_файл(tmp_path / "config.yaml", "backup_time: '25:99'\n"),
                     ensure_key=False)
    assert настройки.problems


# ---------------------------------------------------------------------------
# №19: форма значений json
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ключ, значение", [
    ("glossary", ["кривой"]),
    ("glossary", {"важно": 5}),
    ("content_script", ["строка"]),
    ("telephony_owner_map", []),
])
def test_форма_json_сверяется(ключ, значение):
    from asrhub import catalog

    spec = catalog.get_param(ключ)
    if ключ == "telephony_owner_map" and not isinstance(spec.default, dict):
        значение = {"a": 1}
    assert not catalog.validate_value(ключ, значение)[0]


def test_число_в_словаре_замен_не_срывает_задание(data_dir):
    from asrhub.config import load
    from asrhub.errors import ConfigError

    with pytest.raises(ConfigError):
        load().merged({"glossary": {"важно": 5}})


# ---------------------------------------------------------------------------
# №20г: ключи доступа двух серверов на одном файле
# ---------------------------------------------------------------------------

def test_ключи_двух_серверов_сливаются(data_dir, tmp_path):
    import yaml
    from asrhub.config import load

    общий = "ah_" + "k" * 30
    путь = _файл(tmp_path / "config.yaml", f"api_keys:\n  {общий}:\n    role: admin\n")
    первый, второй = load(путь, ensure_key=False), load(путь, ensure_key=False)
    первый.api_keys["ah_" + "1" * 30] = {"role": "user"}
    assert первый.persist_api_keys()
    второй.api_keys["ah_" + "2" * 30] = {"role": "user"}
    assert второй.persist_api_keys()
    ключи = set(yaml.safe_load(путь.read_text(encoding="utf-8"))["api_keys"])
    assert ключи == {общий, "ah_" + "1" * 30, "ah_" + "2" * 30}


# ---------------------------------------------------------------------------
# Недоделки: ModelSpec.default_params
# ---------------------------------------------------------------------------

def test_рекомендованные_параметры_моделей_есть_в_каталоге():
    from asrhub import catalog

    # Демонстрационный движок читает свой `simulated_rtf` прямо из карточки —
    # это его внутренняя величина, а не параметр задания.
    чужие = {(м.id, к) for м in catalog.MODELS for к in м.default_params
             if к not in catalog.PARAMS_BY_KEY and м.engine != "demo"}
    assert not чужие, sorted(чужие)


def test_описание_предела_речи_не_обещает_подстановки():
    from asrhub import catalog

    # Прежде: «ASR Hub … подставляет безопасное значение при выборе модели» —
    # подстановки не было ни у одной модели, кроме GigaAM (её режет движок).
    параметр = catalog.get_param("vad_max_speech_s")
    текст = " ".join([параметр.description, параметр.recommendation or ""]).replace("\n", " ")
    assert "подставляет безопасное значение" not in текст
