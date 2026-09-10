"""Установка, настройка и запуск локальной языковой модели.

Смысловой слой бесполезен, пока модели нет, а поставить её вручную — это
десяток команд в терминале сервера: скачать Ollama, поднять службу,
выбрать модель под видеокарту, дождаться двадцати гигабайт, прогреть,
вписать три настройки. Здесь то же самое делается кнопкой в настройках, и
делается честно: каждый шаг виден, каждая команда попадает в журнал, а
выбор модели объясняется числами обнаруженного оборудования.

Что здесь есть:

* **Каталог** — десяток моделей, которые имеет смысл держать локально для
  разбора русских разговоров. Размеры в каталоге — подсказка; настоящий
  размер спрашивается у реестра Ollama перед скачиванием, потому что
  каталог в коде стареет, а реестр — нет.
* **Подбор под оборудование** — по свободной видеопамяти за вычетом
  запаса под распознавание: та же видеокарта считает и звук. Модель,
  которая не влезает, не прячется — она показана с причиной.
* **Установщик** — фоновый поток с шагами и процентами: проверка места,
  установка Ollama, запуск службы, скачивание, прогрев, запись настроек.
  Любой шаг, который уже сделан, пропускается: повторный запуск на
  настроенном сервере — это докачать ещё одну модель, а не сломать
  работающее.

Сеть — только стандартная библиотека, как и в клиенте модели: базовая
установка не должна тянуть зависимости ради кнопки в настройках.
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..errors import ASRHubError, ConfigError
from ..hardware import HardwareInfo, detect
from ..logging_setup import get_logger

log = get_logger("llm")

#: Реестр образов Ollama: по манифесту тега видно, существует ли он и
#: сколько весит на самом деле.
РЕЕСТР = "https://registry.ollama.ai/v2/library"

#: Официальный установщик для Linux.
УСТАНОВЩИК = "https://ollama.com/install.sh"

#: Адрес службы Ollama по умолчанию.
АДРЕС = "http://127.0.0.1:11434"

#: Сколько видеопамяти оставить распознаванию, если запас не задан
#: настройкой `llm_min_free_vram_gb`. Две трети моделей распознавания
#: укладываются в это с запасом, а слой разбора не должен выталкивать
#: главную работу сервера.
ЗАПАС_ГБ = 6.0

#: Имя модели: буквы, цифры, точка, дефис, подчёркивание, косая черта для
#: чужих пространств имён, двоеточие перед тегом. Проверка нужна не от
#: подстановки команд (имя уходит в тело JSON, а не в оболочку), а чтобы
#: опечатка не превращалась в непонятную ошибку реестра.
ИМЯ = re.compile(r"^[a-z0-9][a-z0-9._-]{0,48}(/[a-z0-9][a-z0-9._-]{0,48})?"
                 r"(:[a-zA-Z0-9._-]{1,48})?$")

#: Каталог моделей для разбора разговоров на русском.
#:
#: Отбор такой: модель должна быть в библиотеке Ollama (значит, ставится
#: одной командой), заявлять многоязычность (значит, русский для неё не
#: случайность) и укладываться хоть в какое-то разумное железо. Размеры —
#: как их показывает библиотека на день написания; перед скачиванием они
#: уточняются по реестру.
#:
#: `vram_gb` — сколько видеопамяти просить под модель вместе с контекстом
#: разговора, а не только под веса: часовой разговор — это десятки тысяч
#: токенов, и память под них берётся из той же карты. Полный контекст в
#: 256 тысяч токенов сюда не заложен: столько на разговор не нужно, а
#: памяти он просит вдвое больше весов.
#:
#: `freshness` — поколение, а не дата: по нему выбирается рекомендуемая
#: модель. Размер решает не всё — новое поколение того же размера обычно
#: отвечает лучше старшего из прошлого, и советовать по одним гигабайтам
#: значило бы советовать вчерашнее.
КАТАЛОГ: tuple[dict[str, Any], ...] = (
    {
        "name": "qwen3.5:2b", "title": "Qwen3.5 2B", "params_b": 2,
        "size_gb": 2.7, "vram_gb": 4.5, "context": 256000, "family": "qwen3.5",
        "freshness": 3, "license": "Apache 2.0",
        "why": "Самая маленькая из тех, что ещё связно отвечают по-русски. "
               "Для слабой видеокарты и для процессора — с оговоркой, что "
               "качество пересказа заметно ниже старших.",
    },
    {
        "name": "qwen3.5:4b", "title": "Qwen3.5 4B", "params_b": 4,
        "size_gb": 3.4, "vram_gb": 5.5, "context": 256000, "family": "qwen3.5",
        "freshness": 3, "license": "Apache 2.0",
        "why": "Вдвое умнее двойки при тех же секундах ответа. Разумный низ "
               "для карты в 8 ГБ, где кроме модели живёт распознавание.",
    },
    {
        "name": "qwen3.5:9b", "title": "Qwen3.5 9B", "params_b": 9,
        "size_gb": 6.6, "vram_gb": 9.5, "context": 256000, "family": "qwen3.5",
        "freshness": 3, "license": "Apache 2.0",
        "why": "Рабочий минимум для разбора разговоров: 201 язык, контекст "
               "на весь разговор, помещается рядом с распознаванием на "
               "карте в 12 ГБ.",
    },
    {
        "name": "gemma4:12b", "title": "Gemma 4 12B", "params_b": 12,
        "size_gb": 7.6, "vram_gb": 11.0, "context": 256000, "family": "gemma4",
        "freshness": 4, "license": "Gemma Terms of Use",
        "why": "Сильный пересказ при небольшом размере. Хороший выбор для "
               "карты в 16 ГБ, когда важнее связность резюме, чем размер.",
    },
    {
        "name": "gpt-oss:20b", "title": "GPT-OSS 20B", "params_b": 20,
        "size_gb": 14.0, "vram_gb": 17.0, "context": 128000, "family": "gpt-oss",
        "freshness": 2, "license": "Apache 2.0",
        "why": "Открытые веса OpenAI. Аккуратный JSON и следование формату "
               "ответа — то, на чём здесь спотыкаются модели поменьше. "
               "Поколение старше остальных, но проверенное.",
    },
    {
        "name": "qwen3.5:27b", "title": "Qwen3.5 27B", "params_b": 27,
        "size_gb": 17.0, "vram_gb": 21.0, "context": 256000, "family": "qwen3.5",
        "freshness": 3, "license": "Apache 2.0",
        "why": "Крупная из линейки 3.5: заметно точнее девятки на причинах "
               "и исходах. Берут, когда 3.8 не помещается по памяти.",
    },
    {
        "name": "qwen3.6:27b", "title": "Qwen3.6 27B", "params_b": 27,
        "size_gb": 18.0, "vram_gb": 22.0, "context": 256000, "family": "qwen3.6",
        "freshness": 4, "license": "Apache 2.0",
        "why": "Промежуточное поколение с упором на удержание рассуждения. "
               "Запасной вариант, если 3.8 на ваших записях отвечает хуже.",
    },
    {
        "name": "qwen3.8:27b", "title": "Qwen3.8 27B", "params_b": 27,
        "size_gb": 18.0, "vram_gb": 22.0, "context": 256000, "family": "qwen3.8",
        "freshness": 5, "license": "Apache 2.0",
        "why": "Новейшая в линейке Qwen: контекст на 256 тысяч токенов, "
               "управляемая глубина рассуждения, аккуратный JSON. Лучшее, "
               "что целиком помещается на карту в 32 ГБ вместе с "
               "распознаванием.",
    },
    {
        "name": "gemma4:26b", "title": "Gemma 4 26B", "params_b": 26,
        "size_gb": 19.0, "vram_gb": 23.0, "context": 256000, "family": "gemma4",
        "freshness": 4, "license": "Gemma Terms of Use",
        "why": "Длинный контекст и сильный пересказ. Берут, когда важнее "
               "связность резюме, чем скорость.",
    },
    {
        "name": "gemma4:31b", "title": "Gemma 4 31B", "params_b": 31,
        "size_gb": 20.0, "vram_gb": 25.0, "context": 256000, "family": "gemma4",
        "freshness": 4, "license": "Gemma Terms of Use",
        "why": "Старшая Gemma: длинный контекст плюс размер. Требует почти "
               "всю карту в 32 ГБ — распознаванию места не останется.",
    },
    {
        "name": "qwen3.6:35b", "title": "Qwen3.6 35B A3B", "params_b": 35,
        "size_gb": 23.0, "vram_gb": 27.0, "context": 256000, "family": "qwen3.6",
        "freshness": 4, "license": "Apache 2.0",
        "why": "Разреженная: весит как тридцатипятимиллиардная, считает как "
               "трёхмиллиардная. Самая быстрая из крупных — если найдётся "
               "27 ГБ свободной видеопамяти.",
    },
    {
        "name": "gpt-oss:120b", "title": "GPT-OSS 120B", "params_b": 120,
        "size_gb": 65.0, "vram_gb": 72.0, "context": 128000, "family": "gpt-oss",
        "freshness": 2, "license": "Apache 2.0",
        "why": "Старшая открытая модель OpenAI. Имеет смысл на сервере с "
               "80 ГБ видеопамяти и больше.",
    },
    {
        "name": "qwen3.5:122b", "title": "Qwen3.5 122B", "params_b": 122,
        "size_gb": 81.0, "vram_gb": 88.0, "context": 256000, "family": "qwen3.5",
        "freshness": 3, "license": "Apache 2.0",
        "why": "Для двух больших карт. Осмысленна там, где смысловой разбор "
               "— основная работа сервера, а не спутник распознавания.",
    },
)

КАТАЛОГ_ПО_ИМЕНИ = {м["name"]: м for м in КАТАЛОГ}

#: Шаги установки. Порядок важен: каждый следующий опирается на предыдущий.
ШАГИ: tuple[tuple[str, str], ...] = (
    ("проверка", "Проверка оборудования и места на диске"),
    ("установка", "Установка Ollama"),
    ("запуск", "Запуск службы модели"),
    ("скачивание", "Скачивание моделей"),
    ("прогрев", "Прогрев и проверка ответа"),
    ("настройка", "Запись настроек"),
)


# --- сеть ---------------------------------------------------------------

def каталог_весов() -> Path:
    """Где Ollama держит веса — там и надо мерить свободное место.

    Не в каталоге данных сервера и не в рабочем: двадцать гигабайт лягут
    туда, куда их положит Ollama, а это либо `OLLAMA_MODELS`, либо домашний
    каталог службы. Мерить не тот раздел — значит пообещать место, которого
    на нужном разделе нет, и оборвать скачивание на девятнадцатом гигабайте.
    """
    задано = os.environ.get("OLLAMA_MODELS")
    if задано:
        return Path(задано)
    служебный = Path("/usr/share/ollama/.ollama/models")   # так ставит systemd-служба
    if служебный.exists():
        return служебный
    return Path.home() / ".ollama" / "models"


def свободно_под_веса() -> tuple[float, str]:
    """Свободные гигабайты на разделе, куда лягут веса, и сам путь."""
    путь = каталог_весов()
    существующий = путь
    while not существующий.exists() and существующий.parent != существующий:
        существующий = существующий.parent
    try:
        return round(shutil.disk_usage(существующий).free / 1024 ** 3, 1), str(путь)
    except OSError:
        return 0.0, str(путь)


def _не_root() -> bool:
    """Нужен ли sudo. На системах без понятия «root» (Windows) — нет."""
    получить = getattr(os, "geteuid", None)
    return bool(получить) and получить() != 0


def _запрос(method: str, url: str, body: dict[str, Any] | None = None, *,
            timeout: float = 10.0, headers: dict[str, str] | None = None) -> Any:
    """Запрос к службе модели или реестру; ответ — разобранный JSON."""
    заголовки = {"Content-Type": "application/json; charset=utf-8",
                 "Accept": "application/json", "User-Agent": "ASR Hub",
                 **(headers or {})}
    данные = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    запрос = urllib.request.Request(url, data=данные, method=method, headers=заголовки)  # noqa: S310
    with urllib.request.urlopen(запрос, timeout=timeout) as ответ:  # noqa: S310
        сырое = ответ.read()
    return json.loads(сырое.decode("utf-8")) if сырое else {}


def служба(url: str = АДРЕС, *, timeout: float = 3.0) -> dict[str, Any]:
    """Отвечает ли служба Ollama и какой она версии."""
    try:
        данные = _запрос("GET", f"{url.rstrip('/')}/api/version", timeout=timeout)
        return {"running": True, "version": str(данные.get("version") or ""), "url": url}
    except (urllib.error.URLError, OSError, ValueError) as exc:
        причина = getattr(exc, "reason", exc)
        return {"running": False, "version": "", "url": url, "reason": str(причина)}


def установленные(url: str = АДРЕС, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Какие модели уже скачаны: имя, размер, когда обновлена."""
    try:
        данные = _запрос("GET", f"{url.rstrip('/')}/api/tags", timeout=timeout)
    except (urllib.error.URLError, OSError, ValueError):
        return []
    свод = []
    for м in данные.get("models") or []:
        имя = str(м.get("name") or "")
        if not имя:
            continue
        свод.append({"name": имя, "size_gb": round(float(м.get("size") or 0) / 1024 ** 3, 1),
                     "modified": str(м.get("modified_at") or "")})
    return свод


def размер_в_реестре(name: str, *, timeout: float = 10.0) -> tuple[float | None, str]:
    """Настоящий размер скачивания по манифесту реестра.

    Каталог в коде стареет: тег переименовали, модель перевыпустили — и
    число в таблице врёт. Манифест отвечает на оба вопроса разом:
    существует ли тег и сколько он весит сейчас.
    """
    if not ИМЯ.match(name or ""):
        return None, "Недопустимое имя модели."
    репозиторий, _, тег = name.partition(":")
    if "/" in репозиторий:
        return None, "Размер известен только для моделей библиотеки Ollama."
    try:
        данные = _запрос(
            "GET", f"{РЕЕСТР}/{репозиторий}/manifests/{тег or 'latest'}", timeout=timeout,
            headers={"Accept": "application/vnd.docker.distribution.manifest.v2+json"})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, f"В реестре нет модели «{name}»."
        return None, f"Реестр ответил {exc.code}."
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"Реестр недоступен: {getattr(exc, 'reason', exc)}"
    слои = данные.get("layers") or []
    байт = sum(int(с.get("size") or 0) for с in слои)
    байт += int((данные.get("config") or {}).get("size") or 0)
    return (round(байт / 1024 ** 3, 1), "") if байт else (None, "Реестр вернул пустой манифест.")


# --- подбор под оборудование --------------------------------------------

def запас(settings: Any = None) -> float:
    """Сколько видеопамяти держать свободной под распознавание."""
    if settings is not None:
        try:
            задано = float(settings.get("llm_min_free_vram_gb") or 0)
        except (TypeError, ValueError):
            задано = 0.0
        if задано > 0:
            return задано
    return ЗАПАС_ГБ


def бюджет(info: HardwareInfo, reserve_gb: float) -> dict[str, Any]:
    """Сколько памяти есть под модель — и чьей памяти.

    На видеокарте считается свободная, а не общая: распознавание уже
    держит свои веса, и обещать модели всю карту — это обещать чужое.
    Без видеокарты в дело идёт оперативная память, но с честной пометкой,
    что это будут минуты на запись, а не секунды.
    """
    gpu = info.best_gpu
    if info.accelerator in ("cuda", "rocm") and gpu is not None:
        всего = gpu.memory_total_mb / 1024
        свободно = (gpu.memory_free_mb or gpu.memory_total_mb) / 1024
        return {"kind": "gpu", "device": gpu.name, "total_gb": round(всего, 1),
                "free_gb": round(свободно, 1), "reserve_gb": round(reserve_gb, 1),
                "budget_gb": round(max(0.0, свободно - reserve_gb), 1),
                "note": (f"{gpu.name}: свободно {свободно:.1f} из {всего:.1f} ГБ, "
                         f"{reserve_gb:g} ГБ оставлено распознаванию.")}
    if info.accelerator == "mps":
        доступно = info.ram_available_gb or info.ram_total_gb
        return {"kind": "mps", "device": info.cpu_model, "total_gb": round(info.ram_total_gb, 1),
                "free_gb": round(доступно, 1), "reserve_gb": round(reserve_gb, 1),
                "budget_gb": round(max(0.0, доступно * 0.7 - reserve_gb), 1),
                "note": ("Apple Silicon: память общая с системой, под модель "
                         "разумно отдавать не больше двух третей.")}
    доступно = info.ram_available_gb or info.ram_total_gb
    return {"kind": "cpu", "device": info.cpu_model, "total_gb": round(info.ram_total_gb, 1),
            "free_gb": round(доступно, 1), "reserve_gb": round(reserve_gb, 1),
            "budget_gb": round(max(0.0, доступно * 0.7 - 2.0), 1),
            "note": ("Видеокарта не найдена: модель пойдёт на процессоре, "
                     "это минуты на запись, а не секунды.")}


def подобрать(info: HardwareInfo | None = None, *, settings: Any = None,
              installed: Iterable[str] = (),
              catalog: Iterable[dict[str, Any]] = КАТАЛОГ) -> dict[str, Any]:
    """Каталог с пометками под обнаруженное оборудование.

    Ничего не прячет: модель, которая не поместится, остаётся в списке с
    причиной. Прятать — значит оставить владельца сервера гадать, почему
    в таблице пять строк вместо двенадцати, и не дать ему повода
    докупить память.
    """
    info = info or detect()
    б = бюджет(info, запас(settings))
    свои = {str(и).strip() for и in installed}
    # «Установлена» — это и «qwen3:14b», и «qwen3:14b» с суффиксом тега,
    # который Ollama иногда дописывает сама.
    def стоит(имя: str) -> bool:
        return имя in свои or f"{имя}:latest" in свои or any(
            с.split(":")[0] == имя.split(":")[0] and с.split(":")[-1] == имя.split(":")[-1]
            for с in свои)

    строки: list[dict[str, Any]] = []
    for м in catalog:
        нужно = float(м["vram_gb"])
        если_освободить = нужно <= max(0.0, б["total_gb"] - б["reserve_gb"])
        if нужно <= б["budget_gb"]:
            состояние, пояснение = "да", ""
        elif нужно <= б["budget_gb"] * 1.15:
            состояние, пояснение = "впритык", (
                f"Нужно около {нужно:g} ГБ, свободно {б['budget_gb']:g} ГБ: "
                "пойдёт, но без запаса на длинный разговор.")
        elif если_освободить:
            состояние, пояснение = "после освобождения", (
                f"Нужно около {нужно:g} ГБ. Память есть, но занята: выгрузите "
                "модель распознавания или уменьшите запас.")
        else:
            состояние, пояснение = "нет", (
                f"Нужно около {нужно:g} ГБ, всего на устройстве "
                f"{б['total_gb']:g} ГБ.")
        if б["kind"] == "cpu" and состояние in ("да", "впритык") and м["params_b"] > 8:
            пояснение = (пояснение + " " if пояснение else "") + (
                "На процессоре модель такого размера отвечает минутами.")
        строки.append({**м, "state": состояние, "note": пояснение,
                       "installed": стоит(str(м["name"]))})

    подходят = [с for с in строки if с["state"] == "да"]
    # Рекомендуем самую новую из поместившихся, а при равном поколении —
    # самую крупную. Порядок именно такой: модель нового поколения того же
    # размера обычно точнее, и советовать по одним гигабайтам значило бы
    # советовать вчерашнее только за то, что оно тяжелее.
    лучшая = max(подходят, key=lambda с: (с.get("freshness", 0), с["vram_gb"],
                                          с["params_b"]), default=None)
    # Вторая пометка — «быстрая»: если рекомендованная тяжёлая, рядом
    # полезно видеть ту, что отвечает за секунды.
    быстрая = None
    if лучшая is not None:
        меньше = [с for с in подходят if с["vram_gb"] <= лучшая["vram_gb"] / 2]
        быстрая = max(меньше, key=lambda с: с["vram_gb"], default=None)
    for с in строки:
        с["recommended"] = лучшая is not None and с["name"] == лучшая["name"]
        с["fast_pick"] = быстрая is not None and с["name"] == быстрая["name"]
    return {"hardware": б, "models": строки,
            "recommended": лучшая["name"] if лучшая else None,
            "fast_pick": быстрая["name"] if быстрая else None,
            "accelerator": info.accelerator, "disk_free_gb": info.disk_free_gb}


# --- установщик ---------------------------------------------------------

class Установщик:
    """Фоновая установка: Ollama, модели, настройки — по шагам.

    Один запуск за раз. Состояние — не журнал ради журнала: установка
    идёт минутами, страница за это время закроется и откроется, и
    единственный способ узнать, что происходит, — спросить сервер.
    """

    def __init__(self, settings: Any, db: Any = None, *, client: Any = None,
                 hardware: Any = None):
        self.settings = settings
        self.db = db
        self.client = client
        self._hardware = hardware or detect
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._состояние: dict[str, Any] = self._пусто()

    # --- состояние ------------------------------------------------------

    def _пусто(self) -> dict[str, Any]:
        return {
            "running": False, "step": None, "progress": 0.0, "error": None,
            "cancelled": False, "log": [], "models": [], "activate": None,
            "started_at": None, "finished_at": None, "model_progress": {},
            "steps": [{"key": к, "title": т, "state": "ждёт", "note": ""} for к, т in ШАГИ],
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            снимок = json.loads(json.dumps(self._состояние, ensure_ascii=False))
        снимок["log"] = снимок["log"][-60:]
        return снимок

    def _записать(self, строка: str) -> None:
        log.info("Установка модели: %s", строка)
        with self._lock:
            self._состояние["log"].append(f"{time.strftime('%H:%M:%S')} {строка}")
            self._состояние["log"] = self._состояние["log"][-300:]

    def _шаг(self, ключ: str, состояние: str, note: str = "") -> None:
        with self._lock:
            self._состояние["step"] = ключ if состояние == "идёт" else self._состояние["step"]
            for ш in self._состояние["steps"]:
                if ш["key"] == ключ:
                    ш["state"] = состояние
                    if note:
                        ш["note"] = note
            готово = sum(1 for ш in self._состояние["steps"]
                         if ш["state"] in ("готово", "пропущен"))
            self._состояние["progress"] = round(готово / len(ШАГИ), 3)

    # --- запуск ---------------------------------------------------------

    def start(self, models: list[str], *, activate: str | None = None,
              install_server: bool = True, url: str = "") -> dict[str, Any]:
        """Ставит задание в работу; возвращает первое состояние.

        Проверка «не идёт ли уже» и отметка о запуске — под одним замком:
        две вкладки администратора нажимают кнопку одновременно чаще, чем
        кажется, а два потока, качающих одну модель, — это две записи в
        один файл.
        """
        имена = [str(м).strip() for м in models if str(м).strip()]
        плохие = [м for м in имена if not ИМЯ.match(м)]
        if плохие:
            raise ConfigError(f"Непонятное имя модели: {', '.join(плохие[:3])}.")
        if not имена:
            raise ConfigError("Не выбрано ни одной модели.")
        if activate and activate not in имена:
            raise ConfigError("Включать можно только ту модель, которую ставим.")
        адрес = (url or str(self.settings.get("llm_url") or АДРЕС)).rstrip("/")
        with self._lock:
            if self._состояние["running"]:
                raise ConfigError("Установка уже идёт.",
                                  hint="Дождитесь окончания или отмените её.")
            # Флаг отмены снимаем ЗДЕСЬ, а не до замка: иначе вторая
            # вкладка, нажавшая «Поставить» сразу после «Отменить», сначала
            # снимала отмену и лишь потом получала «уже идёт» — установка
            # продолжалась как ни в чём не бывало и в конце переписывала
            # настройки.
            self._stop.clear()
            self._состояние = self._пусто()
            self._состояние.update({"running": True, "models": имена,
                                    "activate": activate or имена[0],
                                    "url": адрес, "started_at": time.time(),
                                    "model_progress": {м: {"share": 0.0, "status": "ждёт"}
                                                       for м in имена}})
        self._thread = threading.Thread(
            target=self._работа, args=(имена, activate or имена[0], install_server, адрес),
            name="asrhub-llm-setup", daemon=True)
        self._thread.start()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        self._stop.set()
        self._записать("Получена отмена; заканчиваем на ближайшем шаге.")
        return self.status()

    def wait(self, timeout: float = 60.0) -> bool:
        поток = self._thread
        if поток is None:
            return True
        поток.join(timeout=timeout)
        return not поток.is_alive()

    # --- сама работа ----------------------------------------------------

    def _работа(self, модели: list[str], включить: str, ставить: bool, адрес: str) -> None:
        try:
            размеры = self._проверка(модели)
            if self._отменено():
                return
            self._установка(ставить)
            if self._отменено():
                return
            self._запуск(адрес)
            if self._отменено():
                return
            self._скачивание(модели, адрес, размеры)
            if self._отменено():
                return
            self._прогрев(включить, адрес)
            self._настройка(включить, адрес)
            self._записать("Готово: модель установлена и включена.")
        except ASRHubError as exc:
            self._провал(str(exc))
        except Exception as exc:                             # noqa: BLE001
            self._провал(f"Непредвиденный сбой: {exc}")
        finally:
            with self._lock:
                self._состояние["running"] = False
                self._состояние["finished_at"] = time.time()

    def _отменено(self) -> bool:
        if not self._stop.is_set():
            return False
        with self._lock:
            self._состояние["cancelled"] = True
            for ш in self._состояние["steps"]:
                if ш["state"] in ("ждёт", "идёт"):
                    ш["state"] = "пропущен"
                    ш["note"] = ш["note"] or "отменено"
        self._записать("Установка отменена.")
        return True

    def _провал(self, текст: str) -> None:
        self._записать(f"Сбой: {текст}")
        with self._lock:
            self._состояние["error"] = текст
            for ш in self._состояние["steps"]:
                if ш["state"] == "идёт":
                    ш["state"] = "сбой"
                    ш["note"] = текст

    # --- шаги -----------------------------------------------------------

    def _проверка(self, модели: list[str]) -> dict[str, float]:
        """Место на диске и существование тегов — до всякой установки."""
        self._шаг("проверка", "идёт")
        info = self._hardware() if callable(self._hardware) else detect()
        размеры: dict[str, float] = {}
        всего = 0.0
        for имя in модели:
            размер, ошибка = размер_в_реестре(имя)
            if размер is None:
                # Реестр недоступен — не повод отказываться: скачивание
                # само скажет правду. А вот «нет такого тега» — повод.
                if "нет модели" in ошибка:
                    raise ConfigError(ошибка, hint="Выберите модель из каталога.")
                справка = КАТАЛОГ_ПО_ИМЕНИ.get(имя, {})
                размер = float(справка.get("size_gb") or 0)
                self._записать(f"{имя}: размер по реестру неизвестен ({ошибка}); "
                               f"считаем по каталогу — {размер:g} ГБ.")
            else:
                self._записать(f"{имя}: {размер:g} ГБ по реестру.")
            размеры[имя] = размер
            всего += размер
        нужно = round(всего * 1.15 + 2, 1)
        свободно, куда = свободно_под_веса()
        if not свободно:
            свободно = float(info.disk_free_gb or 0)
        if свободно and свободно < нужно:
            raise ConfigError(
                f"На диске свободно {свободно:g} ГБ, а нужно около {нужно:g} ГБ "
                f"(веса лягут в {куда}).",
                hint="Освободите место или выберите модель поменьше.")
        б = бюджет(info, запас(self.settings))
        self._записать(б["note"])
        for имя in модели:
            справка = КАТАЛОГ_ПО_ИМЕНИ.get(имя)
            if справка and float(справка["vram_gb"]) > б["budget_gb"]:
                self._записать(
                    f"Предупреждение: {имя} просит около {справка['vram_gb']:g} ГБ "
                    f"видеопамяти, а свободно {б['budget_gb']:g} ГБ — часть слоёв "
                    "уйдёт на процессор, ответы будут медленнее.")
        self._шаг("проверка", "готово",
                  f"нужно около {нужно:g} ГБ в {куда}, свободно {свободно:g} ГБ")
        return размеры

    def _установка(self, ставить: bool) -> None:
        """Ставит Ollama, если её нет. Уже стоит — шаг пропускается."""
        путь = shutil.which("ollama")
        if путь:
            self._шаг("установка", "пропущен", f"уже установлена: {путь}")
            self._записать(f"Ollama уже установлена: {путь}")
            return
        if not ставить:
            raise ConfigError(
                "Ollama не установлена, а установка сервера не запрошена.",
                hint="Включите «поставить Ollama» или установите её вручную.")
        система = platform.system()
        if система == "Darwin":
            raise ConfigError(
                "На macOS установщик не запускается сам.",
                hint="Поставьте Ollama: brew install ollama — или скачайте "
                     "приложение с ollama.com, затем повторите.")
        if система == "Windows":
            raise ConfigError(
                "На Windows установщик не запускается сам.",
                hint="Скачайте OllamaSetup.exe с ollama.com, установите "
                     "и повторите.")
        self._шаг("установка", "идёт")
        файл = Path(str(self.settings.paths.data if getattr(self.settings, "paths", None)
                        else "/tmp")) / "ollama-install.sh"
        self._записать(f"Скачиваем установщик {УСТАНОВЩИК}")
        try:
            запрос = urllib.request.Request(  # noqa: S310
                УСТАНОВЩИК, headers={"User-Agent": "ASR Hub"})
            with urllib.request.urlopen(запрос, timeout=60) as ответ:  # noqa: S310
                файл.write_bytes(ответ.read())
        except (urllib.error.URLError, OSError) as exc:
            raise ConfigError(
                f"Не удалось скачать установщик Ollama: {getattr(exc, 'reason', exc)}",
                hint="Проверьте выход в интернет с сервера или поставьте "
                     "Ollama вручную: https://ollama.com/download") from exc
        команда = ["sh", str(файл)]
        if _не_root():                                       # noqa: SIM108
            # Установщик пишет в /usr/local и заводит службу — без прав
            # это не работает. Пробуем sudo без пароля: спросить пароль
            # нам всё равно негде.
            команда = ["sudo", "-n", *команда]
        self._записать("Запускаем установщик; это займёт минуту-другую.")
        try:
            итог = subprocess.run(команда, capture_output=True, text=True,  # noqa: S603
                                  timeout=1200, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigError(f"Установщик не запустился: {exc}") from exc
        for строка in (итог.stdout or "").splitlines()[-15:]:
            self._записать(строка.strip())
        if итог.returncode != 0:
            хвост = (итог.stderr or "").strip().splitlines()[-3:]
            raise ConfigError(
                "Установщик Ollama завершился с ошибкой: " + ("; ".join(хвост) or
                                                              f"код {итог.returncode}"),
                hint=("Запустите на сервере от root: curl -fsSL "
                      "https://ollama.com/install.sh | sh"))
        try:
            файл.unlink()
        except OSError:
            pass
        self._шаг("установка", "готово", "Ollama установлена")

    def _запуск(self, адрес: str) -> None:
        """Поднимает службу и ждёт, пока она ответит."""
        состояние = служба(адрес)
        if состояние["running"]:
            self._шаг("запуск", "пропущен", f"уже отвечает, версия {состояние['version']}")
            return
        self._шаг("запуск", "идёт")
        if shutil.which("systemctl"):
            команда = ["systemctl", "enable", "--now", "ollama"]
            if _не_root():
                команда = ["sudo", "-n", *команда]
            self._записать("Поднимаем службу через systemctl.")
            try:
                subprocess.run(команда, capture_output=True, text=True,  # noqa: S603
                               timeout=60, check=False)
            except (OSError, subprocess.SubprocessError) as exc:
                self._записать(f"systemctl не сработал: {exc}")
        if not служба(адрес)["running"]:
            # Ни системы инициализации, ни прав — запускаем сами и
            # переживём перезагрузку разве что до неё.
            self._записать("Запускаем ollama serve отдельным процессом.")
            журнал = None
            try:
                каталог_данных = Path(str(self.settings.paths.data)) if getattr(
                    self.settings, "paths", None) else Path(".")
                журнал = (каталог_данных / "ollama.log").open("ab")
                subprocess.Popen(["ollama", "serve"], stdout=журнал,  # noqa: S603, S607
                                 stderr=журнал, start_new_session=True)
            except (OSError, subprocess.SubprocessError) as exc:
                if журнал is not None:
                    журнал.close()
                raise ConfigError(f"Не удалось запустить ollama serve: {exc}") from exc
        крайний = time.time() + 60
        while time.time() < крайний:
            состояние = служба(адрес)
            if состояние["running"]:
                self._шаг("запуск", "готово", f"версия {состояние['version']}")
                self._записать(f"Служба отвечает, версия {состояние['version']}.")
                return
            if self._stop.is_set():
                return
            time.sleep(1.5)
        raise ConfigError(
            f"Служба модели не ответила по адресу {адрес} за минуту.",
            hint="Проверьте на сервере: systemctl status ollama и journalctl -u ollama.")

    def _скачивание(self, модели: list[str], адрес: str, размеры: dict[str, float]) -> None:
        self._шаг("скачивание", "идёт")
        уже = {м["name"] for м in установленные(адрес)}
        for n, имя in enumerate(модели, 1):
            if self._stop.is_set():
                return
            if имя in уже or f"{имя}:latest" in уже:
                self._записать(f"{имя}: уже скачана.")
                self._доля(имя, 1.0, "готово")
                continue
            ожидаем = размеры.get(имя) or 0
            self._записать(f"Скачиваем {имя} ({ожидаем:g} ГБ) — {n} из {len(модели)}.")
            self._тянуть(имя, адрес)
        сколько = sum(1 for м in модели if not self._stop.is_set())
        self._шаг("скачивание", "готово", f"моделей: {сколько}")

    def _доля(self, имя: str, доля: float, статус: str) -> None:
        with self._lock:
            self._состояние["model_progress"][имя] = {
                "share": round(max(0.0, min(1.0, доля)), 3), "status": статус}

    def _тянуть(self, имя: str, адрес: str) -> None:
        """Скачивание с процентами: ответ Ollama — поток строк JSON."""
        тело = json.dumps({"model": имя, "stream": True}).encode("utf-8")
        запрос = urllib.request.Request(  # noqa: S310
            f"{адрес}/api/pull", data=тело, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "ASR Hub"})
        последняя_доля = -1.0
        последний_статус = ""
        try:
            with urllib.request.urlopen(запрос, timeout=120) as ответ:  # noqa: S310
                for сырая in ответ:
                    if self._stop.is_set():
                        self._доля(имя, последняя_доля if последняя_доля > 0 else 0.0,
                                   "отменено")
                        return
                    строка = сырая.decode("utf-8", "replace").strip()
                    if not строка:
                        continue
                    try:
                        шаг = json.loads(строка)
                    except ValueError:
                        continue
                    if шаг.get("error"):
                        raise ConfigError(f"{имя}: {шаг['error']}")
                    статус = str(шаг.get("status") or "")
                    всего = float(шаг.get("total") or 0)
                    сделано = float(шаг.get("completed") or 0)
                    доля = (сделано / всего) if всего else 0.0
                    if доля - последняя_доля >= 0.05 or статус != последний_статус:
                        последняя_доля, последний_статус = доля, статус
                        self._доля(имя, доля, статус)
                        if всего:
                            self._записать(
                                f"{имя}: {статус} — {сделано / 1024 ** 3:.1f} из "
                                f"{всего / 1024 ** 3:.1f} ГБ ({доля * 100:.0f}%)")
        except urllib.error.HTTPError as exc:
            raise ConfigError(f"Служба модели отказалась скачивать {имя}: {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise ConfigError(
                f"Скачивание {имя} оборвалось: {getattr(exc, 'reason', exc)}",
                hint="Проверьте выход в интернет с сервера и повторите — "
                     "скачанное не пропадёт, Ollama продолжит с места обрыва.") from exc
        self._доля(имя, 1.0, "готово")
        self._записать(f"{имя}: скачана.")

    def _прогрев(self, модель: str, адрес: str) -> None:
        """Первый вызов поднимает веса в память — пусть это будет наш."""
        self._шаг("прогрев", "идёт")
        тело = {"model": модель, "stream": False, "keep_alive": "30m",
                "options": {"temperature": 0, "num_predict": 64},
                "messages": [
                    {"role": "system", "content": "Отвечай одним объектом JSON."},
                    {"role": "user", "content": 'Ответь ровно так: {"ok": true, "lang": "ru"}'}]}
        начало = time.perf_counter()
        try:
            данные = _запрос("POST", f"{адрес}/api/chat", тело, timeout=600)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ConfigError(
                f"Модель {модель} не ответила на пробный вопрос: "
                f"{getattr(exc, 'reason', exc)}",
                hint="Смотрите journalctl -u ollama: чаще всего не хватает "
                     "видеопамяти.") from exc
        прошло = round((time.perf_counter() - начало) * 1000)
        ответ = str((данные.get("message") or {}).get("content") or "")[:200]
        self._записать(f"Пробный ответ за {прошло} мс: {ответ}")
        self._шаг("прогрев", "готово", f"первый ответ за {прошло / 1000:.1f} с")

    def _настройка(self, модель: str, адрес: str) -> None:
        """Записывает настройки слоя и сохраняет их в файл конфигурации."""
        self._шаг("настройка", "идёт")
        self.settings.set("llm_backend", "ollama", source="setup")
        self.settings.set("llm_url", адрес, source="setup")
        self.settings.set("llm_model", модель, source="setup")
        сохранено = ""
        try:
            цель = self.settings.config_file or (self.settings.paths.data / "config.yaml")
            сохранено = str(self.settings.save(цель))
        except (ASRHubError, OSError, AttributeError) as exc:
            # Настройки в памяти уже применились — сервер работает. Не
            # сохранились в файл: скажем об этом, но не будем считать
            # установку сорванной.
            self._записать(f"Настройки применены, но файл не записан: {exc}")
        if self.client is not None:
            try:
                self.client.probe(fresh=True)
            except Exception as exc:                         # noqa: BLE001
                log.debug("Проба после установки не удалась: %s", exc)
        if self.db is not None:
            try:
                self.db.add_event(None, "settings_changed",
                                  f"Языковая модель настроена: {модель} на {адрес}")
            except Exception as exc:                         # noqa: BLE001
                log.debug("Событие об установке не записано: %s", exc)
        self._шаг("настройка", "готово",
                  f"llm_backend=ollama, llm_model={модель}"
                  + (f", сохранено в {сохранено}" if сохранено else ""))


def удалить(name: str, url: str = АДРЕС, *, timeout: float = 60.0) -> None:
    """Убирает скачанную модель — место на диске тоже ресурс."""
    if not ИМЯ.match(name or ""):
        raise ConfigError("Непонятное имя модели.")
    try:
        _запрос("DELETE", f"{url.rstrip('/')}/api/delete", {"model": name}, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ConfigError(f"Модель «{name}» не скачана.") from exc
        raise ConfigError(f"Служба модели ответила {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ConfigError(
            f"Служба модели недоступна: {getattr(exc, 'reason', exc)}") from exc
