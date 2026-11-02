"""Схемы описания моделей, движков и параметров ASR Hub.

Каталог — единственный источник правды: из него формируются
* список моделей в веб-интерфейсе и CLI;
* карточки параметров с описаниями и рекомендациями;
* таблицы сравнения в документации;
* планы установки зависимостей и загрузки весов.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Quality(str, Enum):
    """Качество распознавания русского языка (экспертная шкала)."""

    EXCELLENT = "excellent"   # WER < 5 % на открытых наборах
    GOOD = "good"             # WER 5-10 %
    FAIR = "fair"             # WER 10-20 %
    POOR = "poor"             # WER > 20 %
    NONE = "none"             # русский не поддерживается


class Timestamps(str, Enum):
    NONE = "none"
    SEGMENT = "segment"
    WORD = "word"


class Maturity(str, Enum):
    STABLE = "stable"
    NEW = "new"           # выпущена менее полугода назад
    LEGACY = "legacy"     # устарела, оставлена для совместимости
    EXPERIMENTAL = "experimental"


@dataclass(slots=True)
class Benchmark:
    """Одно измерение качества с обязательной ссылкой на источник."""

    dataset: str
    metric: str            # WER | CER | DER | BLEU
    value: float
    source: str
    language: str = "ru"
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ModelSpec:
    """Полное описание одной модели распознавания речи."""

    id: str
    name: str
    family: str
    engine: str
    source: str                       # HF repo id, URL архива или имя ggml-модели
    license: str
    commercial_use: bool
    languages: list[str]
    ru_quality: Quality
    revision: str | None = None
    params_m: float | None = None     # млн параметров
    disk_mb: int | None = None
    vram_gb: float | None = None      # ориентир для fp16 инференса
    ram_gb: float | None = None       # ориентир для CPU-инференса
    streaming: bool = False
    timestamps: Timestamps = Timestamps.SEGMENT
    punctuation: bool = False
    diarization: bool = False
    translation: bool = False
    emotion: bool = False
    max_audio_s: int | None = None    # ограничение одного прохода
    rtfx: float | None = None         # во сколько раз быстрее реального времени
    rtfx_hw: str = ""
    benchmarks: list[Benchmark] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    recommended_for: list[str] = field(default_factory=list)
    not_recommended_for: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    notes: str = ""
    gated: bool = False               # требуется принятие лицензии и HF-токен
    maturity: Maturity = Maturity.STABLE
    released: str = ""
    #: Значения параметров, с которыми модель хорошо работает, — справочно,
    #: для карточки модели. Автоматически они не подставляются: настройки
    #: задания складываются из умолчаний каталога, сервера и самого задания,
    #: а предел длины прохода движок соблюдает сам. Ключи — только из
    #: каталога параметров (проверка — tests/test_review_54_params.py).
    default_params: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    #: Что модель в ASR Hub: «asr» — распознаёт речь и берётся в задание;
    #: «auxiliary» — вспомогательная (эмоции, диаризация), её выход не
    #: расшифровка; «unsupported» — адаптер её не исполняет. Последние две в
    #: задание не берутся (см. `role_note` — почему).
    role: str = "asr"
    role_note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ru_quality"] = self.ru_quality.value
        data["timestamps"] = self.timestamps.value
        data["maturity"] = self.maturity.value
        data["benchmarks"] = [b.to_dict() for b in self.benchmarks]
        return data

    @property
    def runnable(self) -> bool:
        """Можно ли поставить задание на эту модель."""
        return self.role == "asr"

    @property
    def best_ru_wer(self) -> float | None:
        vals = [b.value for b in self.benchmarks if b.language == "ru" and b.metric == "WER"]
        return min(vals) if vals else None


@dataclass(slots=True)
class EngineSpec:
    """Описание движка — программной обвязки, исполняющей модель."""

    id: str
    name: str
    description: str
    requirements_file: str            # requirements/engines/<file>.txt
    python_import: str                # модуль, по наличию которого проверяется установка
    homepage: str
    license: str
    supports_gpu: bool = True
    supports_cpu: bool = True
    supports_mps: bool = False
    supports_streaming: bool = False
    supports_batching: bool = False
    external_binaries: list[str] = field(default_factory=list)
    install_notes: str = ""
    known_issues: list[str] = field(default_factory=list)
    param_groups: list[str] = field(default_factory=list)
    weight: int = 100                 # порядок отображения

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParamExample:
    title: str
    value: Any
    comment: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParamSpec:
    """Описание одного настраиваемого параметра.

    Используется одновременно веб-интерфейсом (генерация формы с подсказками),
    валидатором конфигурации и генератором документации.
    """

    key: str
    label: str
    group: str
    type: str                         # bool | int | float | str | enum | multi | text | json
    default: Any
    description: str
    recommendation: str = ""
    examples: list[ParamExample] = field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    options: list[dict[str, Any]] = field(default_factory=list)
    unit: str = ""
    engines: list[str] = field(default_factory=list)   # пусто = применимо ко всем
    impact: dict[str, str] = field(default_factory=dict)  # quality/speed/memory: up|down|neutral
    advanced: bool = False
    experimental: bool = False
    requires: dict[str, Any] = field(default_factory=dict)  # условия отображения
    aliases: dict[str, str] = field(default_factory=dict)   # engine -> имя параметра в его API
    see_also: list[str] = field(default_factory=list)
    #: Решает только сервер: задание этот ключ не переопределяет (см.
    #: `config.Settings.ТОЛЬКО_СЕРВЕРУ`). Так помечены имена моделей,
    #: которые уходят в загрузчик весов: имя из задания означало бы, что
    #: сервер качает и грузит то, что скажет запрос.
    server_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["examples"] = [e.to_dict() for e in self.examples]
        return data


@dataclass(slots=True)
class PresetSpec:
    id: str
    name: str
    description: str
    scenario: str
    values: dict[str, Any]
    hardware_hint: str = ""
    expected: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
