"""Совместимость конфигурации между версиями.

Повод — простой продуктивного сервера. Обновление сменило тип параметра
«Длины внутренних номеров» с числа на строку (чтобы можно было писать
«3, 4, 6»), в config.yaml осталось число, проверка отвергла файл, и сервер
после обновления не поднялся. systemd сдался после пяти попыток, порт
закрылся, а человек получил сообщение «ожидается строка» про строку,
которую не писал, и совет проверить отступы в файле, где отступы верные.

Отсюда два правила, которые здесь и закреплены:

1. Запись, сделанная прежней версией, читается текущей. Не «угадывается» —
   читается: 5 и «5» это одно значение, записанное разными руками.
2. Ни одна строка конфигурации не имеет права не пустить сервер. Значение,
   которое не прочиталось и после приведения, заменяется умолчанием и
   попадает в список проблем — громкий, но не смертельный.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from asrhub import catalog
from asrhub.config import load
from asrhub.errors import ConfigError


def конфиг(tmp_path: Path, текст: str) -> Path:
    файл = tmp_path / "config.yaml"
    файл.write_text(текст, encoding="utf-8")
    return файл


@pytest.fixture(autouse=True)
def _свой_каталог_данных(tmp_path, monkeypatch):
    """Каталог данных — свой на каждый тест, окружение — чистое."""
    monkeypatch.setenv("ASRHUB_DATA_DIR", str(tmp_path / "data"))
    for имя in list(os.environ):
        if имя.startswith("ASRHUB_") and имя != "ASRHUB_DATA_DIR":
            monkeypatch.delenv(имя, raising=False)


# ---------------------------------------------------------------------------
# Запись прежней версии
# ---------------------------------------------------------------------------

def test_конфигурация_прежней_версии_поднимает_сервер(tmp_path):
    """Та самая авария: число там, где теперь строка."""
    файл = конфиг(tmp_path, "telephony:\n  telephony_internal_digits: 5\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.problems == []
    assert настройки.get("telephony_internal_digits") == "5"


def test_длины_из_прежней_записи_считаются_так_же(tmp_path):
    """Приведение не должно менять смысл: пятизначные остаются пятизначными."""
    from asrhub.telephony.asterisk import длины_внутренних

    файл = конфиг(tmp_path, "telephony:\n  telephony_internal_digits: 4\n")
    настройки = load(файл, apply_hardware=False)
    assert длины_внутренних(настройки.get("telephony_internal_digits")) == {4}


def test_число_в_кавычках_для_числового_параметра_становится_числом(tmp_path):
    """Обратная сторона: «8081» из файла — это порт, а не строка.

    Раньше такое значение проверку проходило (int() от него получается), но
    в настройках оставалось строкой, и арифметика над ним падала уже в
    работе — далеко от файла, из-за которого всё началось.
    """
    файл = конфиг(tmp_path, 'server:\n  server_port: "8081"\n')
    настройки = load(файл, apply_hardware=False)
    assert настройки.get("server_port") == 8081
    assert isinstance(настройки.get("server_port"), int)


def test_пустой_ключ_в_файле_это_не_задано(tmp_path):
    """`telephony_host:` без значения — обычная запись «пока никак»."""
    файл = конфиг(tmp_path, "telephony:\n  telephony_host:\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.problems == []
    assert настройки.get("telephony_host") == ""


# ---------------------------------------------------------------------------
# Ни одна строка не валит запуск
# ---------------------------------------------------------------------------

def test_неизвестный_параметр_не_останавливает_запуск(tmp_path):
    """Обратная сторона обновления — откат.

    Новый интерфейс успевает записать в файл параметр, которого в прежней
    версии нет. Прежняя версия на нём падала — и откат, к которому прибегают
    именно тогда, когда уже плохо, превращался во вторую аварию.
    """
    файл = конфиг(tmp_path, "server:\n  server_port: 8081\n  параметр_из_будущего: 1\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.get("server_port") == 8081
    assert any("параметр_из_будущего" in п for п in настройки.problems)


def test_негодное_значение_заменяется_умолчанием_и_названо(tmp_path):
    файл = конфиг(tmp_path, "server:\n  log_level: ЧЕПУХА\n  server_port: 8099\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.get("log_level") == catalog.PARAMS_BY_KEY["log_level"].default
    assert настройки.get("server_port") == 8099, "соседние значения должны примениться"
    assert len(настройки.problems) == 1
    текст = настройки.problems[0]
    assert "ЧЕПУХА" in текст and "Подробность журнала" in текст


def test_переменная_окружения_с_мусором_не_роняет_сервер(tmp_path, monkeypatch):
    monkeypatch.setenv("ASRHUB_MAX_CONCURRENT_JOBS", "много")
    настройки = load(конфиг(tmp_path, "server:\n  server_port: 8081\n"),
                     apply_hardware=False)
    assert настройки.get("max_concurrent_jobs") == \
        catalog.PARAMS_BY_KEY["max_concurrent_jobs"].default
    assert any("ASRHUB_MAX_CONCURRENT_JOBS" in п for п in настройки.problems)


def test_нечитаемый_файл_остаётся_ошибкой(tmp_path):
    """Граница терпимости.

    Отдельное значение можно заменить умолчанием. Файл, который не
    разбирается вовсе, — нельзя: тогда сервер поднимется на чужом порту,
    без ключей доступа из этого файла, и будет выглядеть работающим.
    """
    файл = конфиг(tmp_path, "server:\n\tserver_port: 8081\n  и: [незакрытая\n")
    with pytest.raises(ConfigError):
        load(файл, apply_hardware=False)


# ---------------------------------------------------------------------------
# Приведение не угадывает
# ---------------------------------------------------------------------------

def test_приведение_не_подбирает_похожее_значение():
    """Молча запустить сервер не с той моделью хуже, чем сказать об ошибке."""
    assert catalog.coerce_value("log_level", "дебаг") == "дебаг"
    assert catalog.coerce_value("max_concurrent_jobs", "много") == "много"
    ok, _ = catalog.validate_value("log_level", "дебаг")
    assert not ok


def test_приведение_не_превращает_да_в_число_и_строку():
    """`True` — это «да», а не 1 и не «True».

    В Python `bool` — разновидность `int`, и невнимательное приведение
    сделало бы из «да» единицу в параметре-числе и слово «True» в
    параметре-строке. Ни то ни другое человек не писал.
    """
    assert catalog.coerce_value("max_concurrent_jobs", True) is True
    assert catalog.coerce_value("telephony_internal_digits", True) is True
    assert catalog.coerce_value("telephony_internal_digits", False) is False


def test_приведение_числа_к_строке_не_приписывает_дробную_часть():
    assert catalog.coerce_value("telephony_internal_digits", 5.0) == "5"
    assert catalog.coerce_value("telephony_internal_digits", 4) == "4"


def test_перечисление_принимает_запись_числом_и_другим_регистром():
    """Перечисление сверяется по записи, а не «на похожесть».

    `audio_sample_rate: "8000"` в кавычках и `log_level: info` строчными —
    это те самые значения из списка, просто записанные иначе. Отвергать их
    не за что, а вот «дебаг» — значение, которого в списке нет, и оно
    остаётся как есть (проверка ниже).
    """
    assert catalog.coerce_value("audio_sample_rate", "8000") == 8000
    assert catalog.coerce_value("log_level", "info") == "INFO"
    assert catalog.validate_value("log_level", catalog.coerce_value("log_level", "info"))[0]


def test_список_через_запятую_становится_списком():
    ключ = next(п.key for п in catalog.PARAMS if п.type == "multi" and п.options)
    значения = [str(о["value"]) for о in catalog.PARAMS_BY_KEY[ключ].options[:2]]
    assert catalog.coerce_value(ключ, ", ".join(значения)) == значения


def test_json_из_строки_и_из_пустоты():
    assert catalog.coerce_value("telephony_stations", "[]") == []
    assert catalog.coerce_value("telephony_stations", None) == []
    # Не JSON — не трогаем: пусть об этом скажет проверка.
    assert catalog.coerce_value("telephony_stations", "{кривой") == "{кривой"


# ---------------------------------------------------------------------------
# Проверка конфигурации
# ---------------------------------------------------------------------------

def test_проверка_конфигурации_ничего_не_создаёт(tmp_path):
    """`--check-config` запускают от root, а служба работает от своего
    пользователя: созданный «заодно» api-key.txt достался бы не тому."""
    файл = конфиг(tmp_path, "server:\n  auth_enabled: true\n")
    настройки = load(файл, apply_hardware=False, ensure_key=False)
    assert настройки.api_keys == {}
    assert not (Path(настройки.paths.data) / "api-key.txt").exists()


def test_проверка_конфигурации_возвращает_код_ошибки(tmp_path, capsys):
    from asrhub.__main__ import main

    файл = конфиг(tmp_path, "server:\n  log_level: ЧЕПУХА\n")
    assert main(["--check-config", "--config", str(файл)]) == 1
    вывод = capsys.readouterr().out
    assert "Подробность журнала" in вывод

    хороший = tmp_path / "ok.yaml"
    хороший.write_text("telephony:\n  telephony_internal_digits: 5\n", encoding="utf-8")
    assert main(["--check-config", "--config", str(хороший)]) == 0
    assert "Все значения приняты" in capsys.readouterr().out


def test_проблемы_конфигурации_видны_только_администратору(tmp_path):
    настройки = load(конфиг(tmp_path, "server:\n  лишний: 1\n"), apply_hardware=False)
    assert настройки.to_dict(for_admin=True)["problems"]
    assert настройки.to_dict(for_admin=False)["problems"] == []


def test_проверка_окружения_проваливается_из_за_конфигурации(tmp_path, capsys):
    """`--check` обязан заметить то, из-за чего сервер работает не по файлу."""
    from asrhub import doctor

    настройки = load(конфиг(tmp_path, "server:\n  log_level: ЧЕПУХА\n"),
                     apply_hardware=False)
    doctor._passed = doctor._warned = doctor._failed = 0
    assert doctor.run_checks(настройки) is False
    assert "Значение в конфигурации" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Те же правила там, где значения приходят не из файла
# ---------------------------------------------------------------------------

def test_настройки_задания_принимают_прежнюю_запись(tmp_path):
    # Параметр задания, а не сервера: телефонию задание с заходом 41
    # переопределять не может вовсе (см. Settings.ТОЛЬКО_СЕРВЕРУ).
    настройки = load(конфиг(tmp_path, "server:\n  server_port: 8081\n"),
                     apply_hardware=False)
    итог = настройки.merged({"speaker_names": 4})
    assert итог["speaker_names"] == "4"


def test_settings_set_приводит_значение(tmp_path):
    настройки = load(конфиг(tmp_path, "server:\n  server_port: 8081\n"),
                     apply_hardware=False)
    настройки.set("telephony_internal_digits", 3)
    assert настройки.get("telephony_internal_digits") == "3"
    with pytest.raises(ConfigError):
        настройки.set("log_level", "дебаг")


def test_копия_прежней_версии_восстанавливается(tmp_path):
    """Копия нужнее всего сразу после обновления, которое пошло не так."""
    import json

    from asrhub import backup

    настройки = load(конфиг(tmp_path, "server:\n  server_port: 8081\n"),
                     apply_hardware=False)
    файл = tmp_path / "settings.json"
    файл.write_text(json.dumps({"telephony_internal_digits": 5, "server_port": 8099}),
                    encoding="utf-8")
    assert backup._вернуть_настройки(файл, настройки) == 2
    assert настройки.get("telephony_internal_digits") == "5"
    assert настройки.get("server_port") == 8099


def test_негодный_секрет_не_попадает_в_сообщение(tmp_path):
    """Сообщение о проблеме идёт и в журнал, и на экран.

    Достаточно один раз ошибиться в типе пароля к АТС, чтобы он лёг в
    журнал открытым текстом и остался там навсегда.
    """
    файл = конфиг(tmp_path,
                  "telephony:\n  telephony_secret:\n    ключ: пароль-в-словаре\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.problems
    assert "пароль-в-словаре" not in " ".join(настройки.problems)
    assert "***" in " ".join(настройки.problems)


def test_длинное_значение_в_сообщении_укорачивается(tmp_path):
    файл = конфиг(tmp_path, "server:\n  log_level: " + "ы" * 400 + "\n")
    настройки = load(файл, apply_hardware=False)
    assert len(настройки.problems[0]) < 300


# ---------------------------------------------------------------------------
# Версия в одном месте
# ---------------------------------------------------------------------------

def test_версия_одна_во_всех_местах():
    """Число версии было вписано в десяток мест по отдельности.

    После обновления `/api/health` отвечал одно, интерфейс показывал другое,
    а файл VERSION знал третье — и вопрос «какая версия стоит» не имел
    одного ответа. Проверка ловит всякое новое вписанное число: место
    должно брать версию, а не хранить свою.
    """
    import re

    from asrhub import __version__

    корень = Path(__file__).resolve().parent.parent
    версия = (корень / "VERSION").read_text(encoding="utf-8").strip()
    assert __version__ == версия

    # Не `\b`: перед номером часто стоит буква («v3.0.1»), и граница слова
    # там не срабатывает — именно так подпись в шапке интерфейса и
    # ускользала от проверки.
    образец = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")
    смотрим = [
        *(корень / "server" / "asrhub").rglob("*.py"),
        *(корень / "server" / "asrhub" / "web").glob("*.js"),
        корень / "server" / "asrhub" / "web" / "index.html",
        корень / "scripts" / "lib" / "common.sh",
        корень / "scripts" / "lib" / "Common.psm1",
        корень / "scripts" / "client" / "asrctl",
        корень / "scripts" / "client" / "asrctl.ps1",
        корень / "pyproject.toml",
        корень / "docker" / "Dockerfile",
    ]
    чужие: list[str] = []
    for файл in смотрим:
        if not файл.is_file() or "__pycache__" in str(файл):
            continue
        for номер, строка in enumerate(файл.read_text(encoding="utf-8").splitlines(), 1):
            # Версии чужих пакетов, требований и Python к нам не относятся.
            if любая_чужая(строка):
                continue
            for найдено in образец.findall(строка):
                if найдено != версия and найдено.startswith(версия.split(".")[0] + "."):
                    чужие.append(f"{файл.relative_to(корень)}:{номер}: {строка.strip()[:80]}")
    assert not чужие, "версия вписана числом и разошлась с VERSION:\n" + "\n".join(чужие)


def любая_чужая(строка: str) -> bool:
    """Строка про чужую версию — питона, пакета, образа, а не про нашу."""
    низ = строка.lower().strip()
    if низ.startswith("#") or низ.startswith("//"):
        return True                      # пояснение рядом с кодом ничего не задаёт
    признаки = ("python", "cuda", "torch", "pip", "==", ">=", "<=", "~=",
                "http://", "https://", "image", "compose", "ubuntu", "debian",
                "nvidia", "ffmpeg", "node", "openapi", "schema_version")
    return any(признак in низ for признак in признаки)


def test_список_в_строковом_параметре_становится_перечислением(tmp_path):
    """«3, 4, 6» списком YAML — то же самое, что «3, 4, 6» строкой."""
    from asrhub.telephony.asterisk import длины_внутренних

    файл = конфиг(tmp_path, "telephony:\n  telephony_internal_digits:\n    - 3\n    - 4\n    - 6\n")
    настройки = load(файл, apply_hardware=False)
    assert настройки.problems == []
    assert настройки.get("telephony_internal_digits") == "3, 4, 6"
    assert длины_внутренних(настройки.get("telephony_internal_digits")) == {3, 4, 6}
