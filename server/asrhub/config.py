"""Конфигурация ASR Hub.

Источники значений, в порядке возрастания приоритета:
1. значения по умолчанию из каталога параметров;
2. рекомендации, вычисленные по обнаруженному оборудованию;
3. файл config.yaml (или config.json, если PyYAML недоступен);
4. переменные окружения с префиксом ASRHUB_;
5. параметры конкретного задания.

Такой порядок позволяет запустить сервер вообще без файла конфигурации:
он подберёт разумные настройки сам.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from . import catalog
from .errors import ConfigError
from .hardware import detect, recommended_settings

log = logging.getLogger("asrhub.config")

ENV_PREFIX = "ASRHUB_"

#: Переменные ASRHUB_*, которые не параметры каталога, но и не опечатки: их
#: читают сервер отдельно (каталог данных, файл настроек, пути к сборкам
#: whisper.cpp и моделям шумоподавления), сценарии установки и обслуживания,
#: Compose, агент на АТС и примеры. О похожих на параметр именах из этого
#: перечня сервер не предупреждает: ASRHUB_ENGINES — список движков
#: установщика, а не опечатка в ASRHUB_ENGINE.
СЛУЖЕБНЫЕ_ПЕРЕМЕННЫЕ = frozenset({
    "data_dir", "hf_token", "api_key", "config", "whisper_cpp", "whisper_cpp_models",
    "whisper_cpp_vad", "rupunct_onnx", "deepfilter", "arnndn_model",
    "accel", "agent_key", "assume_yes", "build_accel", "build_profile", "certs",
    "checklist_title", "data", "data_dir_source", "debug", "default_port", "dkms_src",
    "dry_run", "engines", "force_gpu_driver", "gid", "host", "https_port", "http_port",
    "installations", "install_marker", "invocation", "key", "key_file",
    "launchagents_dir", "lib_dir", "log_file", "max_python", "memory_limit",
    "min_python", "modules_root", "no_color", "nvidia_dev", "nvidia_lib_glob",
    "nvidia_proc", "pci_ids", "pci_root", "port", "probe_python", "profile", "python",
    "python_for_packages", "quiet", "requirements_dir", "restore_from", "retry_label",
    "rocm_pkg", "rocm_version", "server", "systemd_dir", "systemd_user_dir", "tag",
    "uid", "url", "version", "vpip", "wait_limit", "workers",
    # Сценарий отправки звонков из диалплана (agent/asrhub-dialplan): его
    # настройки живут в /etc/asrhub/dialplan.conf станции, а не у сервера.
    "send_mode", "send_timeout_s",
    # Каталог руководства для раздела «Документация» (заход 56).
    "docs_dir",
    # Проверки: без браузера — ошибка, а не пропуск (tests/conftest.py).
    "require_browser",
})


def _похожий_параметр(имя: str) -> str:
    """Параметр каталога, на который похоже незнакомое имя переменной, или пусто."""
    import difflib  # noqa: PLC0415

    if имя in СЛУЖЕБНЫЕ_ПЕРЕМЕННЫЕ or имя in catalog.УБРАННЫЕ:
        return ""
    похожие = difflib.get_close_matches(имя, list(catalog.PARAMS_BY_KEY), n=1, cutoff=0.8)
    return похожие[0] if похожие else ""
_TRUE = {"1", "true", "yes", "on", "да", "истина"}
_FALSE = {"0", "false", "no", "off", "нет", "ложь"}


def parse_scalar(raw: str, kind: str) -> Any:
    """Строка в значение объявленного типа.

    Пользуются и разбор переменных окружения, и приём параметров задания
    отдельными полями формы: и там, и там значение приходит строкой, а
    проверка по каталогу ждёт настоящий тип.
    """
    return _parse_env_value(raw, kind)


def _parse_env_value(raw: str, kind: str) -> Any:
    raw = raw.strip()
    if kind == "bool":
        low = raw.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"Не удалось разобрать логическое значение: «{raw}»")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    if kind == "multi":
        if raw.startswith("["):
            return json.loads(raw)
        return [x.strip() for x in raw.split(",") if x.strip()]
    if kind == "json":
        return json.loads(raw)
    return raw


def _load_structured(path: Path) -> dict[str, Any]:
    # utf-8-sig: Windows PowerShell 5.1 пишет UTF-8 с меткой BOM
    # (`Set-Content -Encoding UTF8`), и файл, созданный установщиком или
    # поправленный из консоли, начинается с U+FEFF. PyYAML метку пропускает,
    # а запасной разбор без него принимал её за часть первого ключа.
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore

            return yaml.safe_load(text) or {}
        except ModuleNotFoundError:
            return _mini_yaml(text)
    return json.loads(text) if text.strip() else {}


def _mini_yaml(text: str) -> dict[str, Any]:
    """Минимальный разбор YAML для случая, когда PyYAML не установлен.

    Поддерживает плоские секции, скаляры и простые списки — этого достаточно
    для формата config.yaml, который генерирует установщик.
    """
    data: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    list_key: str | None = None
    #: Ключ верхнего уровня, про который ещё не известно, секция это или список.
    pending_top_level: str | None = None
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if stripped.startswith("- "):
            if list_key is None:
                continue
            if pending_top_level == list_key:
                # Оказалось, что это список верхнего уровня, а не секция.
                data[list_key] = []
                section = None
                pending_top_level = None
            target = section if section is not None else data
            target.setdefault(list_key, [])
            target[list_key].append(_scalar(stripped[2:].strip()))
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        if indent == 0:
            if val == "":
                # Ключ верхнего уровня без значения — это либо секция, либо
                # список. Что именно, покажет следующая непустая строка:
                # «- элемент» означает список. Раньше здесь всегда заводилась
                # секция, и списки верхнего уровня молча терялись.
                section = {}
                data[key] = section
                list_key = key
                pending_top_level = key
            else:
                section = None
                list_key = None
                pending_top_level = None
                data[key] = _scalar(val)
        else:
            pending_top_level = None
            target = section if section is not None else data
            if val == "":
                target[key] = []
                list_key = key
            else:
                list_key = None
                target[key] = _scalar(val)
    return data


def _scalar(val: str) -> Any:
    if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) > 1:
        return val[1:-1]
    low = val.lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    if low in ("null", "none", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", val):
        return int(val)
    if re.fullmatch(r"-?\d*\.\d+", val):
        return float(val)
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [_scalar(x.strip()) for x in inner.split(",")] if inner else []
    return val


@dataclass
class Paths:
    root: Path
    data: Path
    uploads: Path
    results: Path
    models: Path
    logs: Path
    tmp: Path
    db: Path

    @classmethod
    def create(cls, data_dir: str | os.PathLike[str]) -> Paths:
        data = Path(data_dir).expanduser().resolve()
        paths = cls(
            root=data.parent,
            data=data,
            uploads=data / "uploads",
            results=data / "results",
            models=data / "models",
            logs=data / "logs",
            tmp=data / "tmp",
            db=data / "asrhub.db",
        )
        for directory in (paths.data, paths.uploads, paths.results,
                          paths.models, paths.logs, paths.tmp):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ConfigError(
                    f"Не удалось создать каталог «{directory}»: {exc}",
                    hint="Проверьте права доступа и свободное место на диске.",
                ) from exc
        return paths


def _станции_наружу(значение: Any, for_admin: bool) -> Any:
    """Прячет учётные данные станций в значении telephony_stations.

    Пароль маскируется всегда, адреса и пути — всем, кроме администратора.
    Список станций разбирается здесь, а не в telephony.stations, чтобы
    настройки не тянули за собой телефонию: config.py грузится раньше.
    """
    if not isinstance(значение, list):
        return значение
    наружу = []
    for станция in значение:
        if not isinstance(станция, dict):
            наружу.append(станция)
            continue
        копия = dict(станция)
        for поле in ("secret", "пароль"):
            if копия.get(поле):
                копия[поле] = "***"
        if not for_admin:
            for поле in Settings.ПОЛЯ_СТАНЦИИ_АДМИНУ:
                копия.pop(поле, None)
        наружу.append(копия)
    return наружу


def _приёмники_наружу(значение: Any, for_admin: bool) -> Any:
    """Прячет заголовки и учётные данные приёмников метрик.

    Приёмники пишутся в настройки разделом «Мониторинг» вместе с
    заголовками — а в заголовке Authorization лежит токен InfluxDB или
    Pushgateway. Значения заголовков не отдаются никому (как пароли
    станций). Адрес — целиком только администратору, остальным — схема и
    узел, как в /api/monitoring/targets: токен входящего адреса чата лежит
    в пути, и прежняя обрезка одних параметров запроса отдавала его ключу
    «только чтение».
    """
    if not isinstance(значение, list):
        return значение
    from .monitoring.pushers import адрес_для_всех  # noqa: PLC0415

    наружу = []
    for приёмник in значение:
        if not isinstance(приёмник, dict):
            наружу.append(приёмник)
            continue
        копия = dict(приёмник)
        if isinstance(копия.get("headers"), dict):
            копия["headers"] = dict.fromkeys(копия["headers"], Settings.ЗАГЛУШКА)
        if not for_admin and копия.get("url"):
            копия["url"] = адрес_для_всех(str(копия["url"]))
        наружу.append(копия)
    return наружу


def _приёмники_с_заголовками(новые: list[Any], прежние: Any) -> list[Any]:
    """Возвращает приёмникам заголовки, спрятанные за «***», — по имени и адресу.

    Только по имени было нельзя: имя по умолчанию — вид приёмника, и у двух
    InfluxDB без `name` оно одно, так что первому доставался токен второго.
    Адрес отличает их; приёмник со сменённым адресом прежнего токена не
    получает — заглушка остаётся заглушкой и в заголовок не идёт.
    """
    def ключ(п: dict[str, Any]) -> tuple[str, str]:
        return (str(п.get("name") or п.get("kind") or ""), str(п.get("url") or ""))

    по_ключу = {ключ(п): п for п in (прежние if isinstance(прежние, list) else [])
                if isinstance(п, dict)}
    итог = []
    for приёмник in новые:
        if isinstance(приёмник, dict) and isinstance(приёмник.get("headers"), dict):
            было = по_ключу.get(ключ(приёмник), {})
            заголовки_были = было.get("headers") if isinstance(было.get("headers"), dict) else {}
            заголовки = {}
            for заголовок, значение in приёмник["headers"].items():
                if значение != Settings.ЗАГЛУШКА:
                    заголовки[заголовок] = значение
                elif заголовок in заголовки_были:
                    заголовки[заголовок] = заголовки_были[заголовок]
            приёмник = {**приёмник, "headers": заголовки}
        итог.append(приёмник)
    return итог


def _станции_с_паролями(новые: list[Any], прежние: Any) -> list[Any]:
    """Возвращает станциям пароли, спрятанные за «***» (см. `без_заглушек`)."""
    прежние_по_ключу: dict[str, dict[str, Any]] = {}
    for станция in прежние if isinstance(прежние, list) else []:
        if isinstance(станция, dict):
            for поле in ("id", "name"):
                if станция.get(поле):
                    прежние_по_ключу.setdefault(f"{поле}:{станция[поле]}", станция)
    итог = []
    for станция in новые:
        if not isinstance(станция, dict):
            итог.append(станция)
            continue
        копия = dict(станция)
        for поле in ("secret", "пароль"):
            if копия.get(поле) != Settings.ЗАГЛУШКА:
                continue
            прежняя = next((прежние_по_ключу[f"{к}:{копия[к]}"] for к in ("id", "name")
                            if копия.get(к) and f"{к}:{копия[к]}" in прежние_по_ключу), None)
            if прежняя is not None and прежняя.get(поле):
                копия[поле] = прежняя[поле]
            else:
                # Станции с таким именем не было — пароля взять неоткуда, и
                # заглушка паролем не станет.
                копия.pop(поле, None)
        итог.append(копия)
    return итог


def хеш_ключа(ключ: str) -> str:
    """Под каким именем ключ доступа хранится в config.yaml: sha256 от ключа."""
    return "sha256:" + hashlib.sha256(str(ключ).encode("utf-8")).hexdigest()


def запись_ключа(ключ: str, сведения: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Как ключ лежит в файле настроек: хеш вместо ключа, превью и начало.

    Превью — для списка ключей, начало (16 знаков из 35) — для отзыва по нему
    (`DELETE /api/keys/{первые знаки}`); остальные 19 знаков — больше 110 бит.
    """
    ключ = str(ключ)
    return хеш_ключа(ключ), {
        "preview": f"{ключ[:6]}…{ключ[-4:]}" if len(ключ) > 12 else "***",
        "prefix": ключ[:16],
        **{к: з for к, з in dict(сведения or {}).items() if к not in ("preview", "prefix")},
    }


class КлючиДоступа(dict):
    """Ключи доступа: ищутся и по самому ключу, и по его хешу.

    Ключи, выпущенные сервером, хранятся в файле настроек хешем
    («sha256:…») с превью для списка: файл конфигурации уезжает в копии,
    в снимки и в заявки поддержки, и ключ открытым текстом из него
    становился чужим пропуском. Прежние ключи открытым текстом продолжают
    действовать как были.
    """

    def _найти(self, ключ: Any) -> Any:
        if not isinstance(ключ, str) or not ключ:
            return None
        запись = dict.get(self, ключ)
        if запись is None and not ключ.startswith("sha256:"):
            запись = dict.get(self, хеш_ключа(ключ))
        return запись

    def get(self, ключ: Any, default: Any = None) -> Any:  # noqa: A003
        запись = self._найти(ключ)
        return default if запись is None else запись

    def __getitem__(self, ключ: Any) -> Any:
        запись = self._найти(ключ)
        if запись is None:
            raise KeyError(ключ)
        return запись

    def pop(self, ключ: Any, *default: Any) -> Any:  # noqa: A003
        имя = self.имя_в_словаре(ключ) if isinstance(ключ, str) and ключ else None
        if имя is None:
            if default:
                return default[0]
            raise KeyError(ключ)
        return dict.pop(self, имя)

    def __contains__(self, ключ: Any) -> bool:
        return self._найти(ключ) is not None

    def имя_в_словаре(self, ключ: str) -> str | None:
        """Под каким именем ключ лежит в словаре (сам ключ или его хеш)."""
        if dict.__contains__(self, ключ):
            return ключ
        хеш = хеш_ключа(ключ)
        return хеш if dict.__contains__(self, хеш) else None


@dataclass
class Settings:
    """Итоговая конфигурация сервера."""

    values: dict[str, Any] = field(default_factory=dict)
    #: Каталоги установки. `load()` задаёт их всегда; None — только у
    #: настроек, собранных руками (проверки), где каталоги не нужны.
    #: Для проверки типов — Paths: иначе каждое обращение в обработчиках
    #: требовало бы проверки, которой на деле нужны только эти настройки.
    paths: Paths = None  # type: ignore[assignment]
    config_file: Path | None = None
    api_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    hf_token: str = ""
    hardware_hint: str = ""
    sources: dict[str, str] = field(default_factory=dict)   # ключ -> откуда взято значение
    #: Что в конфигурации не сошлось: лишний ключ, значение не того вида.
    #: Сервер при этом работает на умолчаниях — см. `load`.
    problems: list[str] = field(default_factory=list)
    #: Ключи доступа, пришедшие только из окружения (ASRHUB_API_KEY), и
    #: токен Hugging Face из окружения. В файл они не пишутся: прежде ключ из
    #: переменной уходил в config.yaml при создании любого другого ключа, и
    #: после смены переменной прежний ключ продолжал работать — с ролью admin.
    api_keys_env: set[str] = field(default_factory=set)
    hf_token_env: str = ""
    #: Ключи доступа из файла на момент загрузки — чтобы при записи отличить
    #: ключ, отозванный здесь, от ключа, который в тот же файл дописал
    #: соседний сервер (см. `persist_keys`).
    api_keys_file: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.api_keys, КлючиДоступа):
            self.api_keys = КлючиДоступа(self.api_keys or {})

    def перевести_ключи(self) -> int:
        """Прежние ключи открытым текстом — в хеш (в памяти). Сколько переведено.

        Ключи, выпущенные до перехода на хеши, и ключ первого запуска лежали в файле
        настроек как есть, а файл уезжает в копии и заявки поддержки. Ключ
        из окружения (ASRHUB_API_KEY) в файл не пишется вовсе и остаётся как
        есть. Запись в файл — `persist_api_keys`: она же сохраняет ключи,
        дописанные соседним сервером (`api_keys_file`).
        """
        переведено = 0
        for ключ in [к for к in list(dict.keys(self.api_keys))
                     if not str(к).startswith("sha256:") and к not in self.api_keys_env]:
            сведения = dict.pop(self.api_keys, ключ)
            хеш, запись = запись_ключа(ключ, сведения)
            dict.__setitem__(self.api_keys, хеш, запись)
            переведено += 1
        return переведено

    # --- доступ ---------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key not in self.values:
            raise ConfigError(f"Неизвестный параметр конфигурации: «{key}»")
        return self.values[key]

    def set(self, key: str, value: Any, source: str = "runtime") -> None:
        # Приведение до проверки: «5» для параметра-числа и 5 для
        # параметра-строки — одно и то же значение, записанное разными
        # руками. Интерфейс, API и восстановление из копии приходят сюда же,
        # и каждому из них незачем знать про тип каталога.
        value = catalog.coerce_value(key, value)
        ok, msg = catalog.validate_server_value(key, value)
        if not ok:
            raise ConfigError(msg)
        self.values[key] = value
        self.sources[key] = source
        if key == "log_level":
            # Уровень журнала — сразу: «DEBUG на время диагностики» через
            # «Применить» прежде отвечал «применено» и не действовал до
            # перезапуска.
            logging.getLogger("asrhub").setLevel(
                getattr(logging, str(value).upper(), logging.INFO))
        if key == "model":
            # Выбор модели снимает закреплённый движок, который её не умеет:
            # иначе модель из одного пресета и движок из другого давали
            # результат не той модели (см. `_модель_и_движок`).
            движок = str(self.values.get("engine") or "auto")
            spec = catalog.get_model(str(value))
            if spec is not None and движок not in ("", "auto") \
                    and движок not in catalog.engines_for_model(spec):
                log.info("Движок «%s» не умеет модель «%s» — движок сброшен на auto",
                         движок, value)
                self.values["engine"] = "auto"
                self.sources["engine"] = source

    def merged(self, overrides: dict[str, Any] | None) -> dict[str, Any]:
        """Настройки задания поверх серверных, с проверкой значений.

        Секреты и учётные данные сервера заданием не переопределяются:
        ключ модели, пароль станции, токен CRM принадлежат серверу, и
        задание их не читает — подпись уведомлений, разбор моделью и
        отправка в CRM берут их из настроек сервера. Исключение одно —
        адрес уведомления: у задания он может быть свой. Заглушка «***»
        вместо значения — то, что интерфейс показывает неадминистратору,
        и принимать её за новое значение нельзя: загрузка файла с
        webhook_url="***" отвечала 400 любому, кроме администратора.
        """
        result = dict(self.values)
        if overrides:
            свои = catalog.coerce_all(
                {k: v for k, v in overrides.items()
                 if k in catalog.PARAMS_BY_KEY and not self.чужое_заданию(k, v)})
            self.выражения_задания(свои)
            errors = catalog.validate_all(свои)
            if errors:
                raise ConfigError("; ".join(errors))
            self._в_пределах_сервера(свои)
            result.update(свои)
        _модель_и_движок(result, свои_ключи=set(overrides or {}))
        return result

    def выражения_задания(self, свои: dict[str, Any]) -> None:
        """Регулярные выражения в словаре замен задания — только серверные.

        Ключ «re:…» словаря замен компилируется и применяется как есть, а
        модуль `re` при катастрофическом откате не отпускает GIL: одно
        выражение вида «(\\w+\\s?)+$» на реплике из тридцати слов
        останавливало весь сервер — HTTP, отметку жизни, все воркеры — на
        часы, и после перезапуска задание вешало его снова. Принимать такое
        от любого ключа с правом записи нельзя. Задание может повторить
        выражения из настроек сервера (интерфейс так и присылает словарь
        целиком), но своих — не может: их задаёт администратор, и там они
        ещё и проверяются на вложенные повторы (`safe_regex`).
        """
        словарь = (свои or {}).get("glossary")
        if not isinstance(словарь, dict):
            return
        серверный = self.values.get("glossary")
        серверный = серверный if isinstance(серверный, dict) else {}
        чужие = sorted(str(ключ) for ключ, значение in словарь.items()
                       if str(ключ).startswith("re:")
                       and серверный.get(ключ) != значение)
        if чужие:
            raise ConfigError(
                "Регулярные выражения в словаре замен (ключи «re:») задаются только "
                "в настройках сервера — их задаёт администратор: "
                f"{', '.join(чужие[:5])}.",
                hint="Уберите их из параметров задания или попросите "
                     "администратора добавить их в словарь замен сервера "
                     "(«Настройки» → «Постобработка»). Обычные замены слов "
                     "заданию по-прежнему доступны.")

    def _в_пределах_сервера(self, свои: dict[str, Any]) -> None:
        """Тайм-аут и число повторов задания — не больше серверных.

        Меньше — пожалуйста: задание вправе сдаться раньше. Больше или
        «без предела» — нет: предел ставит администратор против зависших
        и бесконечно падающих заданий, и обычный ключ снимал его одной
        строкой в настройках загрузки.
        """
        if "job_timeout_s" in свои:
            сервер = _целое(self.values.get("job_timeout_s"), 0)
            своё = _целое(свои.get("job_timeout_s"), 0)
            if сервер > 0 and (своё <= 0 or своё > сервер):
                свои["job_timeout_s"] = сервер
        if "max_retries" in свои:
            сервер = max(0, _целое(self.values.get("max_retries"), 2))
            свои["max_retries"] = max(0, min(_целое(свои.get("max_retries"), сервер), сервер))

    #: Параметры, которые нельзя показывать без include_secrets. Это обычные
    #: значения каталога, поэтому они лежали в values и уходили любому ключу
    #: вместе с настройками. webhook_secret подписывает уведомления: ключ
    #: «только чтение» получал возможность подделывать результаты для
    #: принимающей стороны.
    #: Значения, которые нельзя отдавать наружу без прав администратора.
    #:
    #: Адрес обратного вызова и адрес сводки — это тоже секреты, хотя и не
    #: выглядят ими: входящий адрес чата (Slack, Teams, Mattermost) несёт
    #: токен прямо в строке адреса, и знающий его пишет в чат от имени
    #: сервера. Раньше в перечне стоял один webhook_secret, и оба адреса
    #: уходили открытым текстом любому ключу, включая доступ «только чтение».
    #: Пароль AMI сюда же: он открывает интерфейс управления телефонной
    #: станцией, а это не «одна из настроек», а вход в телефонию
    #: организации.
    #: Адрес трекера — третий адрес того же рода, что webhook_url и
    #: digest_url: рекомендация каталога прямо советует направить его во
    #: входящий адрес рабочего чата, то есть токен снова лежит в строке.
    #: Адрес и токен CRM — туда же: у Bitrix24 ключ входящего вебхука лежит
    #: прямо в адресе, у amoCRM долгоживущий токен открывает всю CRM
    #: организации, и каталог обещал «хранится вместе с прочими секретами»,
    #: а GET /api/settings отдавал оба ключу «только чтение». Ссылка на
    #: выгрузку сотрудников открывается без пароля — сама ссылка и есть
    #: пропуск к списку сотрудников. Адрес уведомлений о тревогах — того же
    #: рода, что адрес сводки.
    SECRET_KEYS = ("webhook_secret", "webhook_url", "digest_url", "hf_token",
                   "llm_api_key", "telephony_secret", "tracker_url",
                   "crm_url", "crm_token", "employees_url", "alert_url",
                   "qc_sip_password", "qc_webhook_url", "backup_password",
                   "backup_post_command", "employees_auth_header",
                   "employees_ldap_password")

    #: Параметры, которые задаются только в файле настроек или окружении,
    #: а не из интерфейса: команда «после копии» — это запуск программы на
    #: сервере, и права администратора приложения не должны давать оболочку.
    ТОЛЬКО_ИЗ_ФАЙЛА = ("backup_post_command",)

    #: Секреты, которые не отдаются и администратору (см. `to_dict`).
    СЕКРЕТЫ_ВСЕГДА = ("webhook_secret", "hf_token", "llm_api_key", "telephony_secret",
                      "crm_token", "crm_url", "qc_sip_password", "backup_password",
                      "employees_auth_header", "employees_ldap_password")

    #: Что не уносится в параметры задания. Параметры задания — снимок
    #: настроек на момент постановки, и отдаются они владельцу задания:
    #: в ответе на загрузку, в карточке, в списке и в очереди. Снимок
    #: делался целиком, и обычный ключ получал в карточке своего задания
    #: ровно то, что GET /api/settings от него прячет, — ключ модели,
    #: секрет подписи уведомлений, пароли станций. Заданию всё это не
    #: нужно: при обработке недостающее берётся из настроек сервера.
    НЕ_В_ЗАДАНИИ = (*SECRET_KEYS, "telephony_stations", "telephony_username")

    #: Что задание может задать само, хотя это и секрет: свой адрес
    #: уведомления — обычная возможность API.
    СВОЁ_У_ЗАДАНИЯ = ("webhook_url",)

    #: Параметры, которые решает только сервер (см. `_только_серверу`).
    #: ClassVar, а не поле: с аннотацией без него dataclass делал перечень
    #: полем экземпляра с пустым умолчанием, и `self.ТОЛЬКО_СЕРВЕРУ` у
    #: настроек был пуст — «Сбросить» возвращало адрес АТС к умолчанию.
    ТОЛЬКО_СЕРВЕРУ: ClassVar[frozenset[str]] = frozenset()

    #: Так интерфейс показывает секрет тому, кому его знать не положено.
    ЗАГЛУШКА = "***"

    #: Параметры, которые читаются один раз при запуске: изменение
    #: сохраняется, но действует со следующего запуска. PUT /api/settings
    #: называет их в `restart_required` — прежде они уходили в «применено».
    ПОСЛЕ_ПЕРЕЗАПУСКА = frozenset({"session_ttl_hours", "cors_origins", "webhook_workers",
                                   "server_host", "server_port", "db_journal_mode"})

    @classmethod
    def чужое_заданию(cls, ключ: str, значение: Any) -> bool:
        """Не даёт заданию переопределить то, что принадлежит серверу.

        Это секреты (см. `merged`) и параметры самого сервера: каталог
        моделей, адрес языковой модели, станции АТС, копии, журнал доступа.
        Задание переопределяло любой ключ каталога, и обычный ключ мог
        указать движку свой `models_dir` — веса модели грузились бы оттуда,
        откуда скажет запрос, — или снять `job_timeout_s`, который
        администратор поставил против зависших заданий.
        """
        if ключ in cls.ТОЛЬКО_СЕРВЕРУ:
            return True
        if ключ not in cls.НЕ_В_ЗАДАНИИ:
            return False
        if ключ in cls.СВОЁ_У_ЗАДАНИЯ:
            return значение == cls.ЗАГЛУШКА
        return True

    @classmethod
    def для_задания(cls, параметры: dict[str, Any] | None) -> dict[str, Any]:
        """Параметры задания без секретов и без параметров сервера.

        Для хранения и выдачи. Параметры сервера в снимке задания — это
        адреса и пути инфраструктуры (языковая модель, АТС, каталог
        предприятия, каталоги на диске), и владелец задания видел их в
        карточке. Заданию они не нужны: при обработке они берутся из
        нынешних настроек сервера.
        """
        return {к: з for к, з in (параметры or {}).items()
                if к not in cls.НЕ_В_ЗАДАНИИ and к not in cls.ТОЛЬКО_СЕРВЕРУ}

    def без_заглушек(self, значения: dict[str, Any]) -> dict[str, Any]:
        """Убирает из изменения настроек заглушки «***» вместо секретов.

        Интерфейс получает настройки с секретами, спрятанными за «***», а
        отправляет их обратно целиком — и заглушка ложилась на место
        настоящего значения: «Применить» ставило станции пароль «***», а
        «Сохранить» записывало его в config.yaml, после чего АТС переставала
        пускать сервер. Заглушка означает «не менял», и значение остаётся
        прежним. У станций секрет лежит внутри списка, поэтому для них
        прежний пароль подставляется по идентификатору (или имени) станции.
        """
        итог = {}
        for ключ, значение in значения.items():
            if ключ in self.SECRET_KEYS and значение == self.ЗАГЛУШКА:
                continue
            if ключ == "telephony_stations" and isinstance(значение, list):
                значение = _станции_с_паролями(значение, self.values.get(ключ))
            if ключ == "monitoring_targets" and isinstance(значение, list):
                значение = _приёмники_с_заголовками(значение, self.values.get(ключ))
            итог[ключ] = значение
        return итог

    #: Поля станции, которые не отдаются никому, кроме администратора, —
    #: тот же перечень, что прячет Станция.to_dict. Пароль станции не
    #: отдаётся и администратору: он открывает интерфейс управления
    #: телефонией организации, а «показать пароль» не нужно тому, кто его
    #: и так задал.
    ПОЛЯ_СТАНЦИИ_АДМИНУ = ("host", "port", "username", "cdr_file",
                           "recordings_dir", "filename", "owner_map")

    #: Адреса и пути инфраструктуры — не секреты, но и не дело ключа «только
    #: чтение»: то же, что ПОЛЯ_СТАНЦИИ_АДМИНУ у станций и `paths` у
    #: раскладки каталогов, только отдельными параметрами. Прежде станция
    #: в списке прятала адрес АТС, а параметр `telephony_host` рядом его
    #: отдавал; `paths` скрывались, а `models_dir` и `backup_dir` — нет.
    АДМИНУ = ("telephony_host", "telephony_username", "telephony_cdr_file",
              "telephony_recordings_dir", "models_dir", "temp_dir", "backup_dir",
              "auth_ldap_url", "auth_ldap_base_dn", "auth_ldap_bind_template",
              "auth_ldap_user_filter", "llm_url")

    #: Секреты, которые не отдаются и администратору: ключ модели, секрет
    #: подписи, пароль AMI, токен CRM. Каталог обещает «хранится вместе с
    #: прочими секретами», а GET /api/settings отдавал их открытым текстом.
    #: Показывать их незачем — их и так знает тот, кто задал, а заглушка
    #: «***» при сохранении значения не меняет (см. `без_заглушек`).
    НИКОМУ = ("webhook_secret", "llm_api_key", "telephony_secret", "crm_token")

    def to_dict(self, for_admin: bool = False) -> dict[str, Any]:
        """Настройки для выдачи наружу.

        `for_admin` расширяет ответ параметрами самого сервера (всё, что
        задание задать не вправе, — `ТОЛЬКО_СЕРВЕРУ`), раскладкой каталогов и
        адресами обратных вызовов. Ключи доступа и токен Hugging Face не
        отдаются никому и никогда: у токена есть своя ручка, которая
        показывает шесть знаков и длину, а полное значение не нужно даже
        тому, кто его задал, — он его и так знает. То же — с паролем АМИ,
        ключом модели, токеном и адресом CRM (`СЕКРЕТЫ_ВСЕГДА`).
        """
        values = dict(self.values)
        sources = dict(self.sources)
        if not for_admin:
            # Неадминистратору — только то, что задание вправе задать само.
            # Прятались секреты и `paths`, а остальное уходило целиком: пути на
            # диске (models_dir, temp_dir, backup_dir — та самая раскладка),
            # адрес контроллера домена и шаблон присоединения, адрес модели
            # вместе с «user:pass@», узел и пути АТС, адреса приёмников метрик
            # с токеном в пути. Из карточки задания эти же параметры убирали
            # намеренно, а здесь они лежали в одном запросе. Секреты остаются
            # ключом с заглушкой: интерфейсу нужно знать, что значение задано.
            # Через класс, а не через self: у экземпляра (это dataclass) поле
            # ТОЛЬКО_СЕРВЕРУ своё и пустое, перечень заполняется на классе.
            серверные = type(self).ТОЛЬКО_СЕРВЕРУ
            values = {к: з for к, з in values.items()
                      if к not in серверные or к in self.SECRET_KEYS}
            sources = {к: з for к, з in sources.items() if к in values}
            for key in (*self.SECRET_KEYS, *self.АДМИНУ):
                if values.get(key):
                    values[key] = self.ЗАГЛУШКА
        else:
            # Секреты в собственном смысле не отдаются и администратору —
            # так обещает каталог («не показывается наружу», «показывается
            # замаскированным»): тот, кто задал, и так его знает, а ответ
            # оседает в кеше браузера и журналах прокси. Сохранение заглушку
            # понимает как «не менял» (см. `без_заглушек`). Адреса уведомлений
            # и трекера администратор видит: он их правит.
            for key in (*self.СЕКРЕТЫ_ВСЕГДА, *self.НИКОМУ):
                if values.get(key):
                    values[key] = self.ЗАГЛУШКА
        # Секреты станций лежат не отдельным ключом, а внутри значения
        # telephony_stations, и перечень SECRET_KEYS до них не доставал:
        # пароль AMI уходил ключу «только чтение» открытым текстом, притом
        # что соседний GET /api/telephony/stations прячет его даже от
        # администратора.
        if values.get("telephony_stations"):
            values["telephony_stations"] = _станции_наружу(
                values["telephony_stations"], for_admin)
        if values.get("monitoring_targets"):
            values["monitoring_targets"] = _приёмники_наружу(
                values["monitoring_targets"], for_admin)
        data = {
            "values": values,
            "sources": sources,
            # Путь к файлу настроек — путь к файлу, в котором лежат ключи
            # доступа и токен Hugging Face. Соседний GET /api/system прячет
            # его за правами администратора; здесь он уходил любому ключу,
            # что делало ту защиту бессмысленной.
            "config_file": (str(self.config_file)
                            if (self.config_file and for_admin) else None),
            # Раскладка файловой системы — разведка перед атакой, и соседний
            # GET /api/system прячет её за правами администратора с этой же
            # мыслью. Здесь она уходила любому ключу, что делало ту защиту
            # бессмысленной: путь к базе и каталогам был в двух запросах.
            "paths": ({k: str(v) for k, v in vars(self.paths).items()}
                      if (self.paths and for_admin) else {}),
            "hardware_hint": self.hardware_hint,
            "api_key_count": len(self.api_keys),
            # Проблемы конфигурации — не секрет, но и не повод пугать
            # оператора без прав: чинит их тот, кто правит файл.
            "problems": list(self.problems) if for_admin else [],
        }
        return data

    def save(self, path: Path | None = None) -> Path:
        """Сохраняет в файл конфигурации то, что задано людьми.

        Только значения из файла, из интерфейса и API, из пресетов — не
        умолчания, не подбор под оборудование и не переменные окружения.
        Прежде «Сохранить в конфигурацию» записывало всё: секрет подписи из
        `ASRHUB_WEBHOOK_SECRET`, который каталог велит держать вне файла,
        `beam_size` из окружения поверх записанного в файле, `device: cpu`
        и абсолютные каталоги этой машины. В уже существующем файле меняются
        только эти ключи (`persist_keys`): незнакомые разделы остаются.
        """
        target = path or self.config_file
        if target is None:
            raise ConfigError("Не задан путь к файлу конфигурации.")
        ключи = [ключ for ключ in self.values
                 if ключ in catalog.PARAMS_BY_KEY and self._своё(ключ)]
        return self.persist_keys([*ключи, *self.ОСОБЫЕ_ЗАПИСИ], target=Path(target))

    def _своё(self, ключ: str) -> bool:
        """Задано ли значение людьми (файл, интерфейс, API, пресет), а не взято само."""
        откуда = str(self.sources.get(ключ) or "default")
        return not (откуда in ("default", "hardware") or откуда.startswith("env:"))

    def _ключи_в_файл(self) -> dict[str, dict[str, Any]]:
        """Ключи доступа для записи в файл — без пришедших из окружения."""
        return {ключ: запись for ключ, запись in self.api_keys.items()
                if ключ not in self.api_keys_env}

    #: Записи файла конфигурации, которые не параметры каталога, но тоже
    #: сохраняются по одной (см. `persist_keys`).
    ОСОБЫЕ_ЗАПИСИ = ("api_keys", "hf_token")

    def persist_keys(self, keys: Iterable[str], *,
                     target: str | os.PathLike[str] | None = None) -> Path | None:
        """Записывает в файл конфигурации только названные параметры.

        Нужно разделам, которые сами хранят свою часть настроек: приёмники
        метрик, правила тревог, станции АТС, скрипт и категории разбора,
        ключи доступа. Раньше они жили только в памяти процесса —
        интерфейс отвечал «Сохранено», а перезапуск их стирал.
        Полное `save()` здесь не годится: вместе с приёмником в файл ушло бы
        и всё, что администратор «применил на пробу» на странице настроек.
        Поэтому файл читается, в нём меняются ровно эти ключи — там, где они
        уже лежат, или в группе параметра, — и файл пишется обратно тем же
        атомарным способом, что и при полном сохранении. Кроме параметров
        каталога так пишутся ключи доступа (`api_keys`) и токен Hugging Face
        (`hf_token`): их полное сохранение тоже уносило в файл пробные
        значения, а при первом запуске — и подобранные под оборудование.

        `target` — другой файл вместо того, с которым сервер запущен.
        Ответ — путь к файлу; None — файла конфигурации у сервера нет
        (запуск без него), и сохранять некуда.
        """
        цель = target or self.config_file
        if цель is None:
            return None
        target = Path(цель)
        with _ЗАМОК_СОХРАНЕНИЯ:
            if not target.exists():
                return self._save_locked(target)
            try:
                raw = _load_structured(target) or {}
            except Exception as exc:                          # noqa: BLE001
                raise ConfigError(f"Не удалось прочитать {target}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{target}: ожидается словарь параметров")
            по_группам = any(isinstance(значение, dict) and ключ in catalog.GROUPS_BY_ID
                             for ключ, значение in raw.items())
            for ключ in keys:
                if ключ in self.ОСОБЫЕ_ЗАПИСИ:
                    if ключ == "api_keys":
                        # Два сервера над одним файлом затирали ключи друг
                        # друга: каждый писал свой словарь из памяти. Ключи,
                        # которых не было при нашей загрузке, — дописаны
                        # соседом, и они остаются; бывшие при загрузке, но
                        # пропавшие у нас — отозваны здесь.
                        в_файле = raw.get("api_keys") if isinstance(raw.get("api_keys"), dict) else {}
                        значение: Any = {к: з for к, з in в_файле.items()
                                         if к not in self.api_keys_file and к not in self.api_keys
                                         and к not in self.api_keys_env}
                        значение.update(self._ключи_в_файл())
                        self.api_keys_file = set(значение)
                    else:
                        if self.hf_token_env and self.hf_token == self.hf_token_env:
                            continue        # токен из окружения в файл не пишем
                        значение = self.hf_token
                    if значение:
                        raw[ключ] = значение
                    else:
                        raw.pop(ключ, None)
                    continue
                spec = catalog.PARAMS_BY_KEY.get(ключ)
                if spec is None:
                    continue
                # Параметр мог быть записан и плоско, и в группе: при
                # чтении побеждает последний, поэтому убираем все копии.
                raw.pop(ключ, None)
                for группа, значение in raw.items():
                    if isinstance(значение, dict) and группа in catalog.GROUPS_BY_ID:
                        значение.pop(ключ, None)
                if ключ not in self.values:
                    continue
                if по_группам:
                    группа = raw.setdefault(spec.group, {})
                    if not isinstance(группа, dict):
                        raw[spec.group] = группа = {}
                    группа[ключ] = self.values[ключ]
                else:
                    raw[ключ] = self.values[ключ]
            return self._write_locked(target, raw)

    def записать_ключи(self, keys: Iterable[str], *,
                       target: str | os.PathLike[str] | None = None) -> dict[str, Any]:
        """`persist_keys` с ответом для интерфейса: записано ли, а если нет — почему.

        Правка из раздела применяется в любом случае; не записаться в файл
        она может, если сервер запущен без файла конфигурации или файл
        недоступен на запись. Об этом говорится прямо: «Сохранено», которое
        стирает перезапуск, хуже честного «применено до перезапуска».
        """
        ключи = list(keys)
        try:
            путь = self.persist_keys(ключи, target=target)
        except Exception as exc:                              # noqa: BLE001
            log.warning("%s применено, но в файл конфигурации не записано: %s",
                        ", ".join(ключи), exc)
            return {"persisted": False, "reason": str(exc)}
        if путь is None:
            return {"persisted": False,
                    "reason": "сервер запущен без файла конфигурации"}
        return {"persisted": True}

    def _save_locked(self, target: Path) -> Path:
        """Сохранение под `_ЗАМОК_СОХРАНЕНИЯ` — см. `save`."""
        grouped: dict[str, dict[str, Any]] = {}
        for spec in catalog.PARAMS:
            # Только заданное людьми (см. `save`): умолчания и подбор под
            # оборудование в файл не уходят — иначе они переживали переезд
            # на другую машину и перекрывали её собственный подбор.
            if spec.key in self.values and self._своё(spec.key):
                grouped.setdefault(spec.group, {})[spec.key] = self.values[spec.key]
        payload = {
            "_generated": "ASR Hub",
            "data_dir": str(self.paths.data) if self.paths else "",
            **grouped,
        }
        # Ключи доступа хранятся в том же файле и не относятся ни к одной
        # группе параметров. Без этой строки сохранение настроек стирало их
        # целиком, и после перезапуска доступ к серверу терялся.
        if self._ключи_в_файл():
            payload["api_keys"] = self._ключи_в_файл()
        # Токен Hugging Face — тоже не параметр каталога, и его тоже стирало
        # первое же сохранение: сервер, создавая ключ доступа при первом
        # запуске, переписывал файл и терял записанный установщиком токен.
        # Диаризация после этого падала с «нужен токен» на ровном месте.
        if self.hf_token and self.hf_token != self.hf_token_env:
            payload["hf_token"] = self.hf_token
        return self._write_locked(target, payload)

    @staticmethod
    def _write_locked(target: Path, payload: dict[str, Any]) -> Path:
        """Атомарная запись файла настроек (под `_ЗАМОК_СОХРАНЕНИЯ`)."""
        # Права наследуем от существующего файла. Временный создаётся по umask
        # (обычно 0644), а replace() отдаёт цели ЕГО права — установщик ставил
        # 0640, и первый же запуск сервера, тот самый, который дописывает сюда
        # автоматически созданный ключ доступа, делал файл с ключами и токеном
        # Hugging Face читаемым для всех в системе.
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError:
            mode = 0o600
        # Два сохранения разом (создали ключ, пока применялись настройки)
        # писали в один и тот же «.tmp» каждое со своего дескриптора: второе
        # обрезало файл первого посреди записи, и в конфигурацию мог лечь
        # обрывок YAML — сервер после перезапуска не поднимался. Теперь у
        # каждого сохранения свой временный файл, а сами сохранения идут по
        # одному (замок — в `save`).
        tmp: Path | None = None
        try:
            text = (_dump_yaml(payload) if target.suffix in (".yaml", ".yml")
                    else json.dumps(payload, ensure_ascii=False, indent=2))
            # mkstemp создаёт файл сразу с правами 0600: выставлять их после
            # записи — значит оставить окно, в котором ключи доступны на
            # чтение всем.
            handle, имя = tempfile.mkstemp(prefix=f".{target.name}.",
                                           suffix=".tmp", dir=str(target.parent))
            tmp = Path(имя)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            tmp.replace(target)
            tmp = None
        except OSError as exc:
            raise ConfigError(f"Не удалось сохранить конфигурацию: {exc}") from exc
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        return target


    def базовые_значения(self) -> tuple[dict[str, Any], dict[str, str]]:
        """Что дал бы запуск: умолчания, подбор под оборудование, файл, окружение.

        Тот же порядок источников, что у `load`, — без ключей доступа и без
        создания чего бы то ни было: это эталон для «Сбросить» и для снятия
        прошлого пресета.
        """
        свежие = load(self.config_file, apply_hardware=True, ensure_key=False,
                      искать_файл=False)
        return dict(свежие.values), dict(свежие.sources)

    def сбросить(self) -> list[str]:
        """Возвращает параметры распознавания к тому, что дал бы запуск.

        Прежде каждому ключу ставилось умолчание каталога — мимо файла,
        окружения и подбора по оборудованию: `auth_enabled` становился
        True на сервере без ключей (следующий запрос — 401), `models_dir` и
        `temp_dir` пустели, станции, секреты, LDAP и CRM очищались, а
        следующее «Сохранить» записывало опустошение в файл. Параметры
        самого сервера (доступ, каталоги, телефония, интеграции, очередь)
        сброс не трогает вовсе. Ответ — какие ключи изменились.
        """
        значения, источники = self.базовые_значения()
        изменено: list[str] = []
        for ключ in catalog.PARAMS_BY_KEY:
            if ключ in self.ТОЛЬКО_СЕРВЕРУ or ключ in self.НЕ_В_ЗАДАНИИ or ключ not in значения:
                continue
            if self.values.get(ключ) != значения[ключ] or self.sources.get(ключ) != источники.get(ключ):
                self.values[ключ] = значения[ключ]
                self.sources[ключ] = источники.get(ключ, "default")
                изменено.append(ключ)
        return изменено

    def применить_пресет(self, пресет_id: str, значения: dict[str, Any]) -> dict[str, Any]:
        """Ставит значения пресета, сняв значения прошлого пресета.

        Пресет задаёт только то, что отличается от умолчаний, а писался
        поверх текущего: после «Колл-центра» и «Совещания» оставались
        `audio_channels: split`, `vad_min_speech_ms: 200` и `speaker_names:
        «Оператор, Клиент»` — участники совещания получали имена оператора и
        клиента. Теперь ключи прошлого пресета, которых нет в новом,
        возвращаются к тому, что дал бы запуск (см. `базовые_значения`).
        """
        прошлые = [ключ for ключ, откуда in self.sources.items()
                   if str(откуда).startswith("preset:") and ключ not in значения]
        if прошлые:
            базовые, источники = self.базовые_значения()
            for ключ in прошлые:
                if ключ in базовые:
                    self.values[ключ] = базовые[ключ]
                    self.sources[ключ] = источники.get(ключ, "default")
        применено: dict[str, Any] = {}
        for ключ, значение in значения.items():
            try:
                self.set(ключ, значение, source=f"preset:{пресет_id}")
                применено[ключ] = значение
            except ConfigError as exc:
                log.warning("Пресет %s: параметр %s не применён (%s)",
                            пресет_id, ключ, exc.message)
        return применено

    def persist_api_keys(self) -> bool:
        """Записывает ключи доступа в файл конфигурации.

        Вызывается при создании и отзыве ключа: иначе изменение живёт только
        в памяти процесса и теряется при перезапуске, хотя интерфейс обещает
        обратное («ключ показывается один раз — сохраните его»).
        """
        # Только сами ключи: полное сохранение уносило в файл и то, что на
        # странице настроек применили на пробу, а при первом запуске — ещё и
        # значения, подобранные под оборудование этой машины. Переехав на
        # другую машину, сервер продолжал считать на восьми потоках из
        # файла, хотя их было два.
        if self.config_file is None:
            return False
        try:
            return self.persist_keys(["api_keys"]) is not None
        except ConfigError as exc:
            log.warning("Ключи доступа не сохранены: %s", exc)
            return False


#: Сохранения файла настроек — по одному (см. `Settings.save`).
_ЗАМОК_СОХРАНЕНИЯ = threading.Lock()


#: Роли ключа доступа.
_РОЛИ = ("admin", "user", "readonly", "metrics")


def _ключи_доступа(записи: dict[Any, Any], файл: str,
                   problems: list[str]) -> dict[str, dict[str, Any]]:
    """Ключи доступа из файла: только годные записи.

    `ah_…: admin` — роль строкой вместо раздела — грузилась без замечаний, а
    первый же запрос с этим ключом отвечал 500: разбор ждал словарь.
    Короткая запись принимается как роль; прочее — в `problems`.
    """
    итог: dict[str, dict[str, Any]] = {}
    for ключ, запись in записи.items():
        if not isinstance(ключ, str) or not ключ.strip():
            problems.append(f"{файл}: в api_keys ключ «{ключ}» — не строка. Пропущен")
            continue
        if isinstance(запись, str) and запись.strip().lower() in _РОЛИ:
            запись = {"role": запись.strip().lower()}
        if not isinstance(запись, dict):
            problems.append(f"{файл}: у ключа доступа «{ключ[:6]}…» вместо раздела с "
                            "role записано значение. Ключ не действует")
            continue
        роль = str(запись.get("role") or "admin").strip().lower()
        if роль not in _РОЛИ:
            problems.append(f"{файл}: у ключа доступа «{ключ[:6]}…» неизвестная роль "
                            f"«{роль}» — допустимы {', '.join(_РОЛИ)}. Ключ не действует")
            continue
        итог[ключ] = запись
    return итог


def _модель_и_движок(настройки: dict[str, Any], *, свои_ключи: set[str]) -> None:
    """Сверяет модель с движком и с тем, исполняется ли она вообще.

    Пару раньше не сверял никто. Все пресеты закрепляют `engine`, быстрый
    выбор модели при загрузке меняет только `model` — и задание с моделью
    `gigaam-v3-e2e-rnnt` проходило «готово» с текстом демонстрационного
    движка: в карточке одна модель, текст — другой. Правило:

    * движок не задан заданием, а модель задана — значит, человек выбрал
      модель, и закреплённый у сервера чужой движок сбрасывается на auto;
    * оба заданы явно и не сходятся — отказ с объяснением;
    * модель вспомогательная или неисполняемая — отказ: прежде такие
      модели принимались, ставили движок, качали веса и падали на первом
      задании.
    """
    spec = catalog.get_model(str(настройки.get("model") or ""))
    if spec is None:
        return                          # неизвестную модель называет реестр движков
    if not spec.runnable:
        raise ConfigError(
            f"Модель «{spec.id}» в задание не берётся: {spec.role_note or 'не исполняется'}.",
            hint="Выберите модель распознавания из раздела «Модели».")
    движок = str(настройки.get("engine") or "auto")
    if движок in ("", "auto"):
        return
    допустимые = catalog.engines_for_model(spec)
    if движок in допустимые:
        return
    if движок == "demo" and "engine" in свои_ключи:
        # Демонстрационный движок, названный заданием ЯВНО, — осознанный
        # выбор: симуляция под именем любой модели нужна нагрузочным
        # прогонам и проверкам очереди, и в карточке честно стоит «demo».
        # Подмена опасна, когда движок унаследован от пресета сервера.
        return
    if "engine" not in свои_ключи or "model" not in свои_ключи:
        настройки["engine"] = "auto"
        return
    raise ConfigError(
        f"Движок «{движок}» не умеет модель «{spec.id}».",
        hint="Подходят: " + ", ".join(["auto", *допустимые])
             + ". Не указывайте движок — он возьмётся от модели.")


def _целое(значение: Any, умолчание: int) -> int:
    """Целое с честным нулём; мусор — умолчание."""
    if значение is None or значение == "":
        return int(умолчание)
    try:
        return int(float(значение))
    except (TypeError, ValueError, OverflowError):
        return int(умолчание)


#: Группы каталога, которые целиком принадлежат серверу: доступ, каталоги
#: на диске, наблюдение, языковая модель, телефония, CRM, справочник
#: сотрудников, контроль качества. Задание в них ничего не решает.
ГРУППЫ_СЕРВЕРА = frozenset({"server", "access", "runtime", "monitoring", "llm",
                            "llm_queue", "telephony", "crm", "employees", "health", "qc",
                            # Разбор содержания читает только настройки
                            # сервера (content_index): `content_script` или
                            # `content_pause_s` из задания принимались,
                            # хранились и не действовали, а отпечаток кеша от
                            # них менялся.
                            "content"})

#: Параметры сервера в группах, где остальное задание задаёт само: очередь
#: целиком (предел, число воркеров, политика), хранение и копии, фоновый
#: разбор архива.
КЛЮЧИ_СЕРВЕРА = frozenset({
    "webhook_allow_internal", "webhook_workers", "scheduling_window", "max_batch_files",
    "max_queue_size", "max_concurrent_jobs", "max_concurrent_per_model",
    "scheduling_policy", "consent_tag", "consent_days", "result_retention_days",
    "backup_enabled", "backup_time", "backup_kind", "backup_keep_days",
    "backup_include_results", "backup_interval_hours", "backup_keep", "backup_dir",
    "content_backfill", "content_backfill_batch", "digest_content",
    # Имена вспомогательных моделей уходят прямо в загрузчик —
    # `Pipeline.from_pretrained(имя, token=<токен сервера>)`,
    # `SortformerEncLabelModel.from_pretrained`, `whisperx.load_align_model`,
    # аргументы mfa. Это тот же класс, что `models_dir`, закрытый от
    # заданий в заходе 41: обычный ключ называл чужой репозиторий, и сервер
    # качал его с токеном Hugging Face сервера и разворачивал (контрольные
    # точки pyannote и NeMo — это pickle). Выбирать их — дело администратора.
    "diarization_model", "align_model", "alignment_acoustic_model",
    "alignment_dictionary",
})

#: Из группы сервера сессия диктовки вправе задать себе окно гипотез: это
#: свойство самой сессии, а не сервера.
СВОЁ_У_СЕССИИ = frozenset({"stream_window_s"})


def _только_серверу() -> frozenset[str]:
    # `server_only` каталога — то же, что КЛЮЧИ_СЕРВЕРА, но объявлено у
    # самого параметра: имена моделей диаризации и выравнивания уходили
    # из задания прямо в from_pretrained с токеном сервера — тот же класс
    # дефекта, что был закрыт для models_dir.
    return frozenset(
        p.key for p in catalog.PARAMS
        if (p.group in ГРУППЫ_СЕРВЕРА or p.key in КЛЮЧИ_СЕРВЕРА or p.server_only)
        and p.key not in СВОЁ_У_СЕССИИ and p.key not in Settings.СВОЁ_У_ЗАДАНИЯ)


Settings.ТОЛЬКО_СЕРВЕРУ = _только_серверу()


def _dump_yaml(payload: dict[str, Any]) -> str:
    try:
        import yaml  # type: ignore

        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, default_flow_style=False)
    except ModuleNotFoundError:
        pass
    lines: list[str] = []
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub, subval in value.items():
                lines.append(f"  {sub}: {_yaml_scalar(subval)}")
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


#: Слова, которые YAML 1.1 (его читает PyYAML) понимает не как строку:
#: `off` — это false, `~` — null. Без кавычек `profanity_filter: off` из
#: примера конфигурации читался логическим «нет» и не проходил проверку.
_СЛОВА_YAML = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false",
                         "null", "none", "~"})
#: Запись, которую YAML прочтёт числом: в том числе 0x1F, 0o17, .inf и .nan.
_YAML_NUMBER = re.compile(r"^[-+]?(\.\d+|\d[\d_]*(\.\d*)?)([eE][-+]?\d+)?$|^0[xob][0-9a-fA-F_]+$"
                          r"|^[-+]?\.(inf|nan)$", re.I)


def _yaml_scalar(value: Any) -> str:
    """Значение для config.yaml, которое PyYAML прочтёт ровно этим значением.

    Строки, которые YAML понял бы иначе — с двоеточием, решёткой, переводом
    строки, похожие на число или на логическое слово, — пишутся в кавычках
    через json.dumps: строка JSON — законная строка YAML в двойных кавычках.
    Раньше перевод строки в значении (`auth_ldap_group_map`, `crm_fields`)
    уходил в файл как есть, вторая строка оказывалась вне комментария, и
    пример `--print-config`, который советует doctor.sh, сервер не читал вовсе.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_yaml_scalar(v) for v in value) + "]"
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value)
    # В кавычки (JSON — подмножество YAML) — всё, что YAML прочёл бы иначе:
    # перевод строки (вторая строка значения из примера вылезала из
    # комментария и ломала весь файл), служебные знаки, слова-логические и
    # запись, похожая на число.
    if (text == "" or any(ch in text for ch in ":#{}[],&*!|>'\"%@`\n\r\t\\")
            or text.strip() != text or text[0] in "-?"
            or text.lower() in _СЛОВА_YAML or _похоже_на_число(text)):
        return json.dumps(text, ensure_ascii=False)
    return text


def _похоже_на_число(text: str) -> bool:
    if _YAML_NUMBER.match(text):
        return True
    try:
        float(text.replace("_", ""))
    except ValueError:
        return False
    return True


def _comment_text(value: Any) -> str:
    """Текст для строки комментария — в одну строку: перевод строки внутри
    закончил бы комментарий, и остаток стал бы строкой YAML."""
    return " ".join(str(value).split())


def default_data_dir() -> Path:
    env = os.environ.get(f"{ENV_PREFIX}DATA_DIR")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("PROGRAMDATA") or os.environ.get("LOCALAPPDATA") or "."
        return Path(base) / "ASRHub"
    for candidate in (Path("/var/lib/asrhub"), Path.home() / ".local/share/asrhub"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.touch()
            probe.unlink()
            return candidate
        except OSError:
            continue
    return Path.cwd() / "asrhub-data"


def find_config_file(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise ConfigError(
                f"Файл конфигурации не найден: {path}",
                hint=("Пример со всеми параметрами и описаниями: "
                      "python -m asrhub --print-config > config.yaml"),
            )
        return path
    env = os.environ.get(f"{ENV_PREFIX}CONFIG")
    candidates = [Path(env).expanduser()] if env else []
    candidates += [
        Path.cwd() / "config.yaml",
        Path.cwd() / "config" / "config.yaml",
        default_data_dir() / "config.yaml",
        Path("/etc/asrhub/config.yaml"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _видимое(key: str, value: Any) -> str:
    """Значение в виде, пригодном для журнала и интерфейса.

    Сообщение о негодном значении попадает и в журнал, и на экран, поэтому
    пароль АМИ или адрес чата с токеном внутри показывать в нём нельзя:
    достаточно один раз ошибиться в типе секрета, чтобы он лёг в журнал
    открытым текстом. Длинное значение ещё и укорачиваем — строка с
    полутысячей знаков делает сообщение нечитаемым.
    """
    if key in Settings.SECRET_KEYS:
        return "***"
    текст = str(value)
    return текст if len(текст) <= 80 else текст[:77] + "…"


def load(config_path: str | os.PathLike[str] | None = None,
         *, apply_hardware: bool = True, ensure_key: bool = True,
         искать_файл: bool = True) -> Settings:
    """Собирает конфигурацию из всех источников.

    `ensure_key=False` просит ничего не создавать: ни ключа доступа, ни
    файла с ним. Нужно тому, кто только смотрит, — проверке конфигурации из
    обновления. Она запускается от root, а служба работает от своего
    пользователя: созданный «заодно» api-key.txt достался бы не тому
    владельцу, и сервер после обновления не смог бы его прочитать.
    """
    values: dict[str, Any] = catalog.defaults()
    sources: dict[str, str] = dict.fromkeys(values, "default")
    problems: list[str] = []

    hardware_hint = ""
    if apply_hardware:
        try:
            rec = recommended_settings(detect())
            hardware_hint = str(rec.pop("_reason", ""))
            for key, value in rec.items():
                if key in values:
                    values[key] = value
                    sources[key] = "hardware"
        except Exception:  # определение железа не должно ломать запуск
            hardware_hint = "Не удалось определить оборудование, используются значения по умолчанию."

    # `искать_файл=False` — только названный файл, без поиска по известным
    # местам: эталон для сброса берётся из того же файла, что у сервера, а
    # сервер без файла не должен вдруг найти чужой config.yaml в каталоге.
    cfg_file = (find_config_file(config_path) if (искать_файл or config_path)
                else None)
    data_dir_value: str | None = None
    api_keys: dict[str, dict[str, Any]] = {}
    hf_token = ""
    ключи_окружения: set[str] = set()
    токен_окружения = ""

    if cfg_file is not None:
        try:
            raw = _load_structured(cfg_file)
        except Exception as exc:
            raise ConfigError(
                f"Не удалось прочитать {cfg_file}: {exc}",
                hint="Проверьте синтаксис YAML: отступы пробелами, двоеточие после ключа.",
            ) from exc
        flat: dict[str, Any] = {}
        if raw is not None and not isinstance(raw, dict):
            # Файл-список или одно значение: прежде AttributeError на
            # `.items()` не пускал сервер вовсе.
            problems.append(f"{cfg_file.name}: ожидается словарь параметров «ключ: "
                            "значение», а записан список или одно значение. Файл пропущен")
            raw = {}
        for key, value in (raw or {}).items():
            if not isinstance(key, str):
                # «8080:» или «on:» без кавычек — YAML читает число или
                # логическое, и `key.startswith` ронял запуск.
                problems.append(f"{cfg_file.name}: ключ «{key}» — не имя параметра "
                                "(число или да/нет). Пропущен; имя пишется словом")
                continue
            if key == "data_dir" and isinstance(value, str):
                data_dir_value = value
            elif key == "api_keys" and isinstance(value, dict):
                api_keys = _ключи_доступа(value, cfg_file.name, problems)
            elif key == "hf_token" and isinstance(value, str):
                hf_token = value
            elif isinstance(value, dict) and key in catalog.GROUPS_BY_ID:
                flat.update(value)
            elif key.startswith("_"):
                continue
            else:
                flat[key] = value
        # Ниже — главное правило этого места: НИ ОДНА строка конфигурации не
        # имеет права не пустить сервер. Раньше имела, и это стоило простоя.
        #
        # Обновление меняет форму записи параметра (было одно число — стало
        # «несколько через запятую»), файл на сервере остаётся прежним, и
        # сервер после обновления не поднимается: «ожидается строка» про
        # строку, которую человек не писал. Откат наступает на те же грабли с
        # другой стороны: новый интерфейс успел записать в файл параметр,
        # которого в прежней версии нет, и прежняя версия падает на
        # «неизвестном параметре» — откат из спасения превращается во вторую
        # аварию.
        #
        # Поэтому: сперва приводим запись к типу этой версии, а то, что не
        # сошлось и после приведения, откладываем в `problems` и берём
        # умолчание. Молчать об этом нельзя — иначе «я поправил настройку, а
        # она не работает». Проблемы кричат в трёх местах: ошибкой в журнале
        # при старте, красной полосой в интерфейсе и провалом `--check`.
        for ключ in sorted(k for k in flat if k in catalog.УБРАННЫЕ):
            flat.pop(ключ, None)
            log.info("%s: параметр %s больше не существует и пропущен (%s)",
                     cfg_file.name, ключ, catalog.УБРАННЫЕ[ключ])
        неизвестные = sorted(k for k in flat if k not in catalog.PARAMS_BY_KEY)
        if неизвестные:
            # Подсказка — на то, что есть: раньше она отправляла к команде
            # клиента «config schema», которой не было никогда.
            problems.append(
                f"{cfg_file.name}: неизвестные параметры — {', '.join(неизвестные)}. "
                "Они пропущены; полный список параметров с описаниями: "
                "GET /api/params или python -m asrhub --print-config")
            for ключ in неизвестные:
                flat.pop(ключ, None)
        flat = catalog.coerce_all(flat)
        for key, value in flat.items():
            ok, msg = catalog.validate_server_value(key, value)
            if not ok:
                problems.append(
                    f"{cfg_file.name}: {msg}; записано «{_видимое(key, value)}». "
                    f"Взято значение по умолчанию: «{_видимое(key, values.get(key))}»")
                continue
            values[key] = value
            sources[key] = f"config:{cfg_file.name}"

    for env_key, env_val in os.environ.items():
        if not env_key.startswith(ENV_PREFIX):
            continue
        name = env_key[len(ENV_PREFIX):].lower()
        if name == "data_dir":
            data_dir_value = env_val
            continue
        if name == "hf_token":
            hf_token = env_val
            токен_окружения = env_val
            continue
        if name == "api_key":
            if env_val not in api_keys:
                ключи_окружения.add(env_val)
            api_keys.setdefault(env_val, {"role": "admin", "name": "из переменной окружения"})
            continue
        spec = catalog.PARAMS_BY_KEY.get(name)
        if spec is None:
            # Неизвестный ключ в config.yaml попадает в `problems`, а
            # неизвестная переменная молча пропускалась: ASRHUB_SERVER_PROT=9000
            # — сервер на 8080, и нигде ни слова. Сказать нельзя только про
            # всё подряд: ASRHUB_* читают и сценарии установки, и Compose.
            # Поэтому — про имя, похожее на параметр каталога, и не из
            # перечня служебных.
            похожее = _похожий_параметр(name)
            if похожее:
                problems.append(
                    f"Переменная {env_key}: такого параметра нет — возможно, имелась в "
                    f"виду {ENV_PREFIX}{похожее.upper()}. Значение не применено")
            continue
        try:
            parsed = _parse_env_value(env_val, spec.type)
        except (ValueError, json.JSONDecodeError, ConfigError) as exc:
            problems.append(f"Переменная {env_key}: не удалось разобрать значение "
                            f"«{_видимое(name, env_val)}» ({exc}). Переменная пропущена")
            continue
        parsed = catalog.coerce_value(name, parsed)
        ok, msg = catalog.validate_server_value(name, parsed)
        if not ok:
            problems.append(f"Переменная {env_key}: {msg}; "
                            f"записано «{_видимое(name, env_val)}». Переменная пропущена")
            continue
        values[name] = parsed
        sources[name] = f"env:{env_key}"

    # Ключ, который прежние версии унесли в файл из ASRHUB_API_KEY (его имя —
    # «из переменной окружения»), действует, только пока переменная та же:
    # после смены переменной прежний ключ с ролью admin продолжал работать.
    из_окружения = os.environ.get(f"{ENV_PREFIX}API_KEY", "")
    for ключ in [к for к, з in api_keys.items()
                 if isinstance(з, dict) and з.get("name") == "из переменной окружения"]:
        if ключ == из_окружения:
            ключи_окружения.add(ключ)
        else:
            api_keys.pop(ключ, None)
            log.warning("Ключ доступа «из переменной окружения» в %s больше не совпадает "
                        "с ASRHUB_API_KEY и не действует", cfg_file.name if cfg_file else "файле")

    paths = Paths.create(data_dir_value or default_data_dir())

    if not values.get("models_dir"):
        values["models_dir"] = str(paths.models)
    if not values.get("temp_dir"):
        values["temp_dir"] = str(paths.tmp)

    if not hf_token:
        токен_окружения = (os.environ.get("HF_TOKEN", "")
                           or os.environ.get("HUGGING_FACE_HUB_TOKEN", ""))
    hf_token = hf_token or токен_окружения

    settings = Settings(
        values=values,
        paths=paths,
        config_file=cfg_file,
        api_keys=api_keys,
        hf_token=hf_token,
        hardware_hint=hardware_hint,
        sources=sources,
        problems=problems,
        api_keys_env=ключи_окружения,
        hf_token_env=токен_окружения,
        api_keys_file=set(api_keys) - ключи_окружения,
    )
    for проблема in problems:
        log.error("Конфигурация: %s", проблема)

    if settings.get("auth_enabled") and not api_keys and ensure_key:
        keyfile = paths.data / "api-key.txt"

        # Сначала пробуем прочитать уже созданный ключ. Без этого каждая
        # загрузка настроек выпускала новый и переписывала файл: достаточно
        # было выполнить `python3 -m asrhub --check` при работающем сервере,
        # чтобы в api-key.txt оказался ключ, которого сервер не знает, — и
        # веб-интерфейс переставал пускать с файлом, выглядящим правильным.
        # Ключи из config.yaml читаются выше; сюда доходят установки без
        # файла конфигурации, а таких большинство.
        existing = ""
        try:
            if keyfile.exists():
                existing = keyfile.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("Не удалось прочитать ключ доступа из %s: %s", keyfile, exc)

        # Ключ первого запуска в файле настроек — хешем, как и выпущенные
        # позже: сам ключ живёт только в api-key.txt (права 0600).
        if existing.startswith("ah_") and len(existing) >= 12:
            хеш, запись = запись_ключа(existing, {
                "role": "admin", "name": "создан автоматически при первом запуске"})
            settings.api_keys[хеш] = запись
            return settings

        key = "ah_" + secrets.token_urlsafe(24)
        хеш, запись = запись_ключа(key, {"role": "admin",
                                         "name": "создан автоматически при первом запуске"})
        settings.api_keys[хеш] = запись
        try:
            # Файл создаётся сразу с нужными правами: если выставлять их после
            # записи, остаётся окно, в котором ключ доступен на чтение всем.
            # O_EXCL — чтобы две загрузки, начавшиеся одновременно, не
            # затирали ключ друг друга.
            handle = os.open(str(keyfile),
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(key + "\n")
        except FileExistsError:
            # Кто-то успел создать файл между проверкой и записью — берём его.
            try:
                other = keyfile.read_text(encoding="utf-8").strip()
            except OSError:
                other = ""
            if other.startswith("ah_"):
                settings.api_keys.pop(хеш, None)
                хеш2, запись2 = запись_ключа(other, {
                    "role": "admin", "name": "создан автоматически при первом запуске"})
                settings.api_keys[хеш2] = запись2
                return settings
        except OSError as exc:
            log.error("Не удалось сохранить ключ доступа в %s: %s", keyfile, exc)
            log.error("Ключ доступа этого запуска: %s", key)
        settings.persist_api_keys()
    return settings


def generate_example_config() -> str:
    """Полный пример config.yaml со всеми параметрами и комментариями."""
    lines: list[str] = [
        "# Конфигурация ASR Hub",
        "# Все параметры необязательны: при отсутствии файла сервер подбирает",
        "# значения автоматически по обнаруженному оборудованию.",
        "#",
        "# Приоритет источников (по возрастанию):",
        "#   умолчания -> автоопределение железа -> этот файл -> переменные ASRHUB_* -> параметры задания",
        "",
        "# Каталог данных: загрузки, результаты, модели, журналы, база.",
        "# По умолчанию /var/lib/asrhub (Linux), ~/.local/share/asrhub или %PROGRAMDATA%\\ASRHub.",
        "# data_dir: /var/lib/asrhub",
        "",
        "# Токен Hugging Face — нужен для моделей с принятием лицензии (pyannote, GigaAM longform).",
        "# hf_token: hf_xxxxxxxxxxxxxxxxxxxx",
        "",
        "# Ключи доступа. Если раздел пуст и включена аутентификация,",
        "# при первом запуске создаётся ключ и сохраняется в <data_dir>/api-key.txt.",
        "# api_keys:",
        "#   ah_ваш_ключ:",
        "#     role: admin        # admin | user | readonly",
        "#     name: Основной ключ",
        "#     rate_limit: 600",
        "",
    ]
    for group in catalog.GROUPS:
        params = catalog.params_for_group(group["id"])
        if not params:
            continue
        lines.append("")
        lines.append("# " + "=" * 74)
        lines.append(f"# {group['title'].upper()}")
        lines.append(f"# {group['description']}")
        lines.append("# " + "=" * 74)
        lines.append(f"{group['id']}:")
        for spec in params:
            lines.append("")
            for chunk in _wrap(spec.description, 74):
                lines.append(f"  # {chunk}")
            if spec.recommendation:
                lines.append("  #")
                for chunk in _wrap("Рекомендация: " + spec.recommendation, 74):
                    lines.append(f"  # {chunk}")
            if spec.examples:
                lines.append("  #")
                lines.append("  # Примеры:")
                for ex in spec.examples[:3]:
                    val = _yaml_scalar(ex.value)
                    comment = f"  — {_comment_text(ex.comment)}" if ex.comment else ""
                    lines.append(f"  #   {_comment_text(ex.title)}: {val}{comment}")
            rng = []
            if spec.minimum is not None:
                rng.append(f"мин. {spec.minimum:g}")
            if spec.maximum is not None:
                rng.append(f"макс. {spec.maximum:g}")
            if spec.unit:
                rng.append(f"единицы: {spec.unit}")
            if rng:
                lines.append(f"  # Диапазон: {', '.join(rng)}")
            if spec.options:
                opts = ", ".join(_comment_text(o["value"]) for o in spec.options)
                lines.append(f"  # Допустимые значения: {opts}")
            lines.append(f"  {spec.key}: {_yaml_scalar(spec.default)}")
    return "\n".join(lines) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    out: list[str] = []
    cur = ""
    for word in words:
        if len(cur) + len(word) + 1 > width:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        out.append(cur)
    return out or [""]
