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

import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import catalog
from .errors import ConfigError
from .hardware import detect, recommended_settings

log = logging.getLogger("asrhub.config")

ENV_PREFIX = "ASRHUB_"
_TRUE = {"1", "true", "yes", "on", "да", "истина"}
_FALSE = {"0", "false", "no", "off", "нет", "ложь"}


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
    text = path.read_text(encoding="utf-8")
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


@dataclass
class Settings:
    """Итоговая конфигурация сервера."""

    values: dict[str, Any] = field(default_factory=dict)
    paths: Paths | None = None
    config_file: Path | None = None
    api_keys: dict[str, dict[str, Any]] = field(default_factory=dict)
    hf_token: str = ""
    hardware_hint: str = ""
    sources: dict[str, str] = field(default_factory=dict)   # ключ -> откуда взято значение

    # --- доступ ---------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> Any:
        if key not in self.values:
            raise ConfigError(f"Неизвестный параметр конфигурации: «{key}»")
        return self.values[key]

    def set(self, key: str, value: Any, source: str = "runtime") -> None:
        ok, msg = catalog.validate_value(key, value)
        if not ok:
            raise ConfigError(msg)
        self.values[key] = value
        self.sources[key] = source

    def merged(self, overrides: dict[str, Any] | None) -> dict[str, Any]:
        """Настройки задания поверх серверных, с проверкой значений."""
        result = dict(self.values)
        if overrides:
            errors = catalog.validate_all(
                {k: v for k, v in overrides.items() if k in catalog.PARAMS_BY_KEY})
            if errors:
                raise ConfigError("; ".join(errors))
            for key, value in overrides.items():
                if key in catalog.PARAMS_BY_KEY:
                    result[key] = value
        return result

    #: Параметры, которые нельзя показывать без include_secrets. Это обычные
    #: значения каталога, поэтому они лежали в values и уходили любому ключу
    #: вместе с настройками. webhook_secret подписывает уведомления: ключ
    #: «только чтение» получал возможность подделывать результаты для
    #: принимающей стороны.
    SECRET_KEYS = ("webhook_secret",)

    def to_dict(self, include_secrets: bool = False) -> dict[str, Any]:
        values = dict(self.values)
        if not include_secrets:
            for key in self.SECRET_KEYS:
                if values.get(key):
                    values[key] = "***"
        data = {
            "values": values,
            "sources": dict(self.sources),
            "config_file": str(self.config_file) if self.config_file else None,
            "paths": {k: str(v) for k, v in vars(self.paths).items()} if self.paths else {},
            "hardware_hint": self.hardware_hint,
            "api_key_count": len(self.api_keys),
        }
        if include_secrets:
            data["api_keys"] = self.api_keys
            data["hf_token"] = self.hf_token
        return data

    def save(self, path: Path | None = None) -> Path:
        """Сохраняет текущие значения в файл конфигурации."""
        target = path or self.config_file
        if target is None:
            raise ConfigError("Не задан путь к файлу конфигурации.")
        target = Path(target)
        grouped: dict[str, dict[str, Any]] = {}
        for spec in catalog.PARAMS:
            if spec.key in self.values:
                grouped.setdefault(spec.group, {})[spec.key] = self.values[spec.key]
        payload = {
            "_generated": "ASR Hub",
            "data_dir": str(self.paths.data) if self.paths else "",
            **grouped,
        }
        # Ключи доступа хранятся в том же файле и не относятся ни к одной
        # группе параметров. Без этой строки сохранение настроек стирало их
        # целиком, и после перезапуска доступ к серверу терялся.
        if self.api_keys:
            payload["api_keys"] = self.api_keys
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            if target.suffix in (".yaml", ".yml"):
                tmp.write_text(_dump_yaml(payload), encoding="utf-8")
            else:
                tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(target)
        except OSError as exc:
            raise ConfigError(f"Не удалось сохранить конфигурацию: {exc}") from exc
        return target


    def persist_api_keys(self) -> bool:
        """Записывает ключи доступа в файл конфигурации.

        Вызывается при создании и отзыве ключа: иначе изменение живёт только
        в памяти процесса и теряется при перезапуске, хотя интерфейс обещает
        обратное («ключ показывается один раз — сохраните его»).
        """
        if self.config_file is None:
            return False
        try:
            self.save()
            return True
        except ConfigError as exc:
            log.warning("Ключи доступа не сохранены: %s", exc)
            return False


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


def _yaml_scalar(value: Any) -> str:
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
    if text == "" or any(ch in text for ch in ":#{}[]," ) or text.strip() != text:
        return json.dumps(text, ensure_ascii=False)
    return text


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
                hint="Создайте его командой: asrctl config init",
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


def load(config_path: str | os.PathLike[str] | None = None,
         *, apply_hardware: bool = True) -> Settings:
    """Собирает конфигурацию из всех источников."""
    values: dict[str, Any] = catalog.defaults()
    sources: dict[str, str] = dict.fromkeys(values, "default")

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

    cfg_file = find_config_file(config_path)
    data_dir_value: str | None = None
    api_keys: dict[str, dict[str, Any]] = {}
    hf_token = ""

    if cfg_file is not None:
        try:
            raw = _load_structured(cfg_file)
        except Exception as exc:
            raise ConfigError(
                f"Не удалось прочитать {cfg_file}: {exc}",
                hint="Проверьте синтаксис YAML: отступы пробелами, двоеточие после ключа.",
            ) from exc
        flat: dict[str, Any] = {}
        for key, value in (raw or {}).items():
            if key == "data_dir" and isinstance(value, str):
                data_dir_value = value
            elif key == "api_keys" and isinstance(value, dict):
                api_keys = value
            elif key == "hf_token" and isinstance(value, str):
                hf_token = value
            elif isinstance(value, dict) and key in catalog.GROUPS_BY_ID:
                flat.update(value)
            elif key.startswith("_"):
                continue
            else:
                flat[key] = value
        unknown = [k for k in flat if k not in catalog.PARAMS_BY_KEY]
        if unknown:
            raise ConfigError(
                "Неизвестные параметры в файле конфигурации: " + ", ".join(sorted(unknown)),
                hint="Полный список параметров: asrctl config schema",
            )
        errors = catalog.validate_all(flat)
        if errors:
            raise ConfigError("Ошибки в файле конфигурации: " + "; ".join(errors))
        for key, value in flat.items():
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
            continue
        if name == "api_key":
            api_keys.setdefault(env_val, {"role": "admin", "name": "из переменной окружения"})
            continue
        spec = catalog.PARAMS_BY_KEY.get(name)
        if spec is None:
            continue
        try:
            parsed = _parse_env_value(env_val, spec.type)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ConfigError(
                f"Переменная {env_key}: не удалось разобрать значение «{env_val}» ({exc})") from exc
        ok, msg = catalog.validate_value(name, parsed)
        if not ok:
            raise ConfigError(f"Переменная {env_key}: {msg}")
        values[name] = parsed
        sources[name] = f"env:{env_key}"

    paths = Paths.create(data_dir_value or default_data_dir())

    if not values.get("models_dir"):
        values["models_dir"] = str(paths.models)
    if not values.get("temp_dir"):
        values["temp_dir"] = str(paths.tmp)

    hf_token = hf_token or os.environ.get("HF_TOKEN", "") or os.environ.get(
        "HUGGING_FACE_HUB_TOKEN", "")

    settings = Settings(
        values=values,
        paths=paths,
        config_file=cfg_file,
        api_keys=api_keys,
        hf_token=hf_token,
        hardware_hint=hardware_hint,
        sources=sources,
    )

    if settings.get("auth_enabled") and not api_keys:
        key = "ah_" + secrets.token_urlsafe(24)
        settings.api_keys[key] = {"role": "admin", "name": "создан автоматически при первом запуске"}
        keyfile = paths.data / "api-key.txt"
        try:
            # Файл создаётся сразу с нужными правами: если выставлять их после
            # записи, остаётся окно, в котором ключ доступен на чтение всем.
            handle = os.open(str(keyfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                fh.write(key + "\n")
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
                    comment = f"  — {ex.comment}" if ex.comment else ""
                    lines.append(f"  #   {ex.title}: {val}{comment}")
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
                opts = ", ".join(str(o["value"]) for o in spec.options)
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
