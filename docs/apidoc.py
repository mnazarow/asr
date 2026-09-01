#!/usr/bin/env python3
"""Общая часть генераторов справочника по программному интерфейсу.

Здесь всё, что одинаково для справочника по мониторингу и по сервису
целиком: обращение к работающему серверу, таблицы параметров, блок операции.
Различается только состав маршрутов, разбиение на разделы, правила доступа и
примеры — их каждый генератор передаёт своими.

Смысл сборки с живого сервера в том, что справочник не может разойтись с
действительностью: описания берутся из схемы OpenAPI, а примеры ответов —
настоящие, снятые с сервера, а не переписанные руками.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"

METHOD_ORDER = {"get": 0, "post": 1, "put": 2, "delete": 3, "patch": 4}

def key_locations() -> list[Path]:
    """Где искать ключ доступа — от самого достоверного к наименее.

    Порядок важен. Раньше первым стоял каталог временного стенда в /tmp:
    оставшийся от прошлого запуска файл забирал верх над настоящим каталогом
    данных, и справочник собирался с чужим ключом — вместо примеров ответов
    в него попадали блоки «сервер недоступен: 401», похожие на поломку
    сервера.
    """
    places: list[Path] = []
    env_dir = os.environ.get("ASRHUB_DATA_DIR")
    if env_dir:
        places.append(Path(env_dir) / "api-key.txt")
    places += [
        Path("/var/lib/asrhub/api-key.txt"),
        Path.home() / ".local/share/asrhub/api-key.txt",
        Path.home() / "Library/Application Support/ASRHub/data/api-key.txt",
        Path("/tmp/asrhub-demo/api-key.txt"),          # временный стенд — последним
    ]
    return places


def find_key(explicit: str = "", base: str = "") -> str:
    """Ключ, который сервер действительно принимает.

    Если задан адрес, каждый найденный ключ проверяется запросом: молча
    собрать справочник с недействительным ключом хуже, чем не собрать вовсе.
    """
    candidates = [explicit] if explicit else []
    if not explicit:
        for path in key_locations():
            try:
                if path.exists():
                    candidates.append(path.read_text(encoding="utf-8").strip())
            except OSError:
                continue

    if not base:
        return candidates[0] if candidates else ""

    for candidate in candidates:
        if not candidate:
            continue
        try:
            fetch(f"{base}/api/queue", candidate)
            return candidate
        except (urllib.error.URLError, OSError):
            continue

    if candidates:
        print("  ВНИМАНИЕ: ни один найденный ключ сервер не принял — "
              "примеры ответов будут пустыми.")
        print("  Передайте действующий ключ вторым аргументом.")
    return ""


def fetch(url: str, key: str = "", raw: bool = False) -> Any:
    request = urllib.request.Request(url)
    if key:
        request.add_header("X-API-Key", key)
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        body = response.read().decode("utf-8")
    return body if raw else json.loads(body)


def sample(base: str, path: str, key: str, *, limit: int = 900,
           lang: str = "json") -> str:
    """Настоящий ответ сервера, обрезанный до читаемого объёма."""
    try:
        body = fetch(base + path, key, raw=True)
    except (urllib.error.URLError, OSError) as exc:
        return f"```\n(сервер недоступен: {exc})\n```"
    if lang == "json":
        try:
            body = json.dumps(json.loads(body), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n…"
    return f"```{lang}\n{body}\n```"


#: Заголовки аутентификации попадают в схему из зависимости FastAPI и
#: повторяются у каждой операции. В таблице параметров они только шум:
#: про ключ сказано один раз во введении.
AUTH_HEADERS = {"x-api-key", "authorization"}


def params_table(operation: dict[str, Any]) -> list[str]:
    parameters = [p for p in (operation.get("parameters") or [])
                  if not (p.get("in") == "header"
                          and str(p.get("name", "")).lower() in AUTH_HEADERS)]
    if not parameters:
        return []
    rows = ["", "| Параметр | Где | Тип | По умолчанию | Описание |", "|---|---|---|---|---|"]
    for item in parameters:
        schema = item.get("schema") or {}
        kind = schema.get("type") or schema.get("anyOf", [{}])[0].get("type", "—")
        default = schema.get("default")
        default_text = "обязателен" if item.get("required") else (
            f"`{default}`" if default not in (None, "") else "—")
        place = {"query": "в адресе", "path": "в пути", "header": "в заголовке"}.get(
            item.get("in", ""), item.get("in", ""))
        rows.append(f"| `{item['name']}` | {place} | {kind} | {default_text} | "
                    f"{item.get('description', '') or '—'} |")
    return rows


def body_block(operation: dict[str, Any]) -> list[str]:
    content = ((operation.get("requestBody") or {}).get("content") or {})
    schema = (content.get("application/json") or {}).get("schema")
    if not schema:
        return []
    return ["", "**Тело запроса** — JSON:", "",
            "```json", json.dumps(schema, ensure_ascii=False, indent=2)[:600], "```"]


def operation_block(path: str, method: str, operation: dict[str, Any],
                    base: str, key: str, *,
                    access: Callable[[str, str], str],
                    examples: dict[tuple[str, str], dict[str, Any]],
                    level: int = 3) -> list[str]:
    """Один маршрут: заголовок, описание, параметры, пример, ответ."""
    heading = "#" * level
    lines = [f"{heading} `{method.upper()} {path}`\n"]
    if operation.get("summary"):
        lines.append(f"{operation['summary']}.\n")
    doc = (operation.get("description") or "").strip()
    if doc:
        lines.append(doc + "\n")
    lines.append(f"**Доступ:** {access(path, method)}.\n")
    lines.extend(params_table(operation))
    lines.extend(body_block(operation))

    example = examples.get((path, method))
    if example:
        lines += ["", "**Пример**", "", "```bash", example["curl"], "```"]
        if example.get("show"):
            lines += ["", "**Ответ**", "",
                      sample(base, example["show"], key, lang=example.get("lang", "json"),
                             limit=example.get("limit", 900))]
        if example.get("note"):
            lines += ["", example["note"]]
    lines.append("")
    return lines


def overview_table(paths: dict[str, dict[str, Any]],
                   access: Callable[[str, str], str]) -> list[str]:
    """Сводная таблица всех операций — с неё удобно начинать поиск."""
    rows = ["| Метод | Адрес | Что делает | Доступ |", "|---|---|---|---|"]
    for path in sorted(paths):
        for method in sorted(paths[path], key=lambda m: METHOD_ORDER.get(m, 9)):
            if method not in METHOD_ORDER:
                continue
            operation = paths[path][method]
            rows.append(f"| `{method.upper()}` | `{path}` | "
                        f"{operation.get('summary', '')} | {access(path, method)} |")
    rows.append("")
    return rows


def load_schema(base: str) -> dict[str, Any] | None:
    try:
        return fetch(f"{base}/api/openapi.json")
    except (urllib.error.URLError, OSError) as exc:
        print(f"Не удалось получить схему с {base}: {exc}")
        print("Запустите сервер и повторите: python3 -m asrhub --port 8080")
        return None
