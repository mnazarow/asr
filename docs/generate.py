#!/usr/bin/env python3
"""Генератор справочных разделов документации из каталога ASR Hub.

Справочник параметров и таблицы сравнения моделей собираются прямо из
каталога, поэтому документация не расходится с кодом: добавили параметр —
он появился в справочнике вместе с описанием, рекомендацией и примерами.

    python3 docs/generate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
DOCS = ROOT / "docs"

from asrhub import catalog  # noqa: E402
from asrhub.catalog import Quality  # noqa: E402

QUALITY_RU = {
    Quality.EXCELLENT: "отличное",
    Quality.GOOD: "хорошее",
    Quality.FAIR: "среднее",
    Quality.POOR: "слабое",
    Quality.NONE: "нет русского",
}
MATURITY_RU = {
    "stable": "стабильная", "new": "новая",
    "legacy": "устаревшая", "experimental": "экспериментальная",
}
TIMESTAMPS_RU = {"word": "пословные", "segment": "по сегментам", "none": "нет"}


def yes(value: bool) -> str:
    return "да" if value else "—"


def clip(text: str, limit: int) -> str:
    """Обрезать по границе слова, не разрывая его посередине."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:—-")
    return (cut or text[:limit]) + "…"


def fmt(value, suffix: str = "") -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = str(value)
    return f"{text}{suffix}"


# ===========================================================================
# Справочник параметров
# ===========================================================================

def generate_parameters() -> str:
    stats = catalog.params_stats()
    out: list[str] = [
        "# Справочник параметров",
        "",
        f"Всего параметров: **{stats['total']}** в {stats['groups']} группах, "
        f"из них {stats['advanced']} помечены как «для опытных». "
        f"Примеров настройки: {stats['examples_total']}.",
        "",
        "Раздел собран автоматически из каталога параметров сервера, поэтому он "
        "всегда соответствует установленной версии. Те же описания, рекомендации "
        "и примеры доступны в веб-интерфейсе в разделе «Настройки» и через "
        "программный интерфейс: `GET /api/params`.",
        "",
        "**Как читать карточку параметра.** «Влияние» показывает, что произойдёт "
        "при увеличении значения: качество ↑ — растёт, скорость ↓ — падает. "
        "«Применимо к» пусто, если параметр действует для всех движков.",
        "",
        "## Содержание",
        "",
    ]
    for group in catalog.GROUPS:
        params = catalog.params_for_group(group["id"])
        anchor = group["title"].lower().replace(" ", "-").replace(",", "").replace("(", "").replace(")", "")
        out.append(f"- [{group['title']}](#{anchor}) — {len(params)} парам.: {group['description']}")
    out.append("")

    for group in catalog.GROUPS:
        params = catalog.params_for_group(group["id"])
        if not params:
            continue
        out += ["", f"## {group['title']}", "", group["description"], ""]

        out.append("| Параметр | Ключ | Тип | По умолчанию | Кратко |")
        out.append("|---|---|---|---|---|")
        for spec in params:
            summary = clip(spec.description.split(".")[0], 110)
            out.append(f"| {spec.label} | `{spec.key}` | {spec.type} | "
                       f"`{spec.default}` | {summary} |")
        out.append("")

        for spec in params:
            out += ["", f"### {spec.label}", ""]

            meta = [f"**Ключ:** `{spec.key}`", f"**Тип:** {spec.type}",
                    f"**По умолчанию:** `{spec.default}`"]
            if spec.minimum is not None or spec.maximum is not None:
                bounds = []
                if spec.minimum is not None:
                    bounds.append(f"от {spec.minimum:g}")
                if spec.maximum is not None:
                    bounds.append(f"до {spec.maximum:g}")
                meta.append(f"**Диапазон:** {' '.join(bounds)}")
            if spec.unit:
                meta.append(f"**Единицы:** {spec.unit}")
            if spec.advanced:
                meta.append("**Для опытных**")
            out.append(" · ".join(meta))
            out.append("")
            out.append(spec.description)
            out.append("")

            if spec.options:
                out.append("**Допустимые значения**")
                out.append("")
                for option in spec.options:
                    out.append(f"- `{option['value']}` — {option['label']}")
                out.append("")

            if spec.recommendation:
                out += ["> **Рекомендация.** " + spec.recommendation, ""]

            if spec.examples:
                out.append("**Примеры настройки**")
                out.append("")
                out.append("| Сценарий | Значение | Пояснение |")
                out.append("|---|---|---|")
                for example in spec.examples:
                    out.append(f"| {example.title} | `{example.value}` | "
                               f"{example.comment or '—'} |")
                out.append("")

            impacts = [f"{k} {'↑' if v == 'up' else '↓'}"
                       for k, v in (spec.impact or {}).items() if v != "neutral"]
            extra = []
            if impacts:
                names = {"quality": "качество", "speed": "скорость", "memory": "память"}
                readable = [f"{names.get(item.split()[0], item.split()[0])} {item.split()[1]}"
                            for item in impacts]
                extra.append("**При увеличении значения:** " + ", ".join(readable))
            if spec.engines:
                extra.append("**Применимо к движкам:** " + ", ".join(f"`{e}`" for e in spec.engines))
            if spec.requires:
                conditions = ", ".join(f"`{k}` = `{v}`" for k, v in spec.requires.items())
                extra.append(f"**Действует при:** {conditions}")
            if spec.aliases:
                names = ", ".join(f"`{engine}` → `{name}`" for engine, name in spec.aliases.items())
                extra.append(f"**Имя в движке:** {names}")
            if spec.see_also:
                extra.append("**См. также:** " + ", ".join(f"`{k}`" for k in spec.see_also))
            if extra:
                out += extra + [""]

    return "\n".join(out) + "\n"


# ===========================================================================
# Сравнение моделей
# ===========================================================================

def generate_models() -> str:
    summary = catalog.catalog_summary()
    out: list[str] = [
        "# Сравнение моделей распознавания речи",
        "",
        f"В каталоге **{summary['total']} моделей** из {len(summary['families'])} семейств; "
        f"{summary['russian']} поддерживают русский язык, {summary['streaming']} работают "
        f"в потоковом режиме, {summary['diarization']} умеют разделять говорящих. "
        f"Данные собраны по первоисточникам на {catalog.CATALOG_DATE}.",
        "",
        "> **Как читать цифры.** Значения WER взяты из карточек моделей, статей авторов "
        "и независимых бенчмарков — они измерены на **разных наборах данных**. "
        "GigaAM измеряли на Golos, Common Voice и внутренних наборах Сбера; Parakeet — "
        "на FLEURS и CoVoST2; Whisper — на Common Voice и Open ASR Leaderboard. "
        "Один и тот же набор даёт разброс в 2–3 раза между доменами (студийная запись "
        "против телефонии). Поэтому таблицы ниже годятся, чтобы **отобрать двух-трёх "
        "кандидатов**, но не для того, чтобы объявить победителя. "
        "Окончательный выбор делайте прогоном на своих записях — в разделе «Аналитика» "
        "для этого есть расчёт фактического WER по эталонному тексту.",
        "",
        "![Как выбрать модель](images/diag-05-model-choice.png)",
        "",
        "## Краткий ответ",
        "",
        "| Задача | Модель | Почему |",
        "|---|---|---|",
        "| Русский, максимум точности | `gigaam-v3-rnnt` | Лучший WER на русском среди свободных моделей, лицензия MIT |",
        "| Русский, сразу готовый текст | `gigaam-v3-e2e-rnnt` | Пунктуация, заглавные буквы и числа цифрами из коробки |",
        "| Русский, большой архив | `gigaam-v3-ctc` | В 1.5–2 раза быстрее RNNT при потере менее 1 п.п. |",
        "| Телефония, реальное время | `tone-ru` | Лучшее качество на колл-центре, задержка около секунды, 71.6 млн параметров |",
        "| Мультиязычный поток | `parakeet-tdt-0.6b-v3` | 25 языков с автоопределением, RTFx выше 3000 |",
        "| Совещания с говорящими | `moss-transcribe-diarize` | ASR и диаризация в одной модели, 50+ языков |",
        "| Перевод речи | `canary-1b-v2` | ASR и перевод между английским и 24 языками |",
        "| Сервер без видеокарты | `faster-whisper-small` + int8 | Единственный практичный режим на процессоре |",
        "| macOS на Apple Silicon | `whispercpp-large-v3-turbo-q5_0` | Metal и Core ML, 574 МБ на диске |",
        "| Редкие языки | `omnilingual-ctc-1b` | 1600+ языков, Apache-2.0 на код и веса |",
        "| Проверка установки | `demo-simulator` | Встроенный симулятор без загрузки весов |",
        "",
    ]

    # --- Сводная таблица по русскому -------------------------------------
    out += [
        "## Русский язык: измеренное качество",
        "",
        "Отсортировано по среднему WER на русских наборах. Прочерк означает, "
        "что публичных измерений на русском нет — это не то же самое, что плохое качество.",
        "",
        "| Модель | Средний WER | Лучший WER | Лицензия | Парам., млн | Диск, МБ | Поток | Пунктуация |",
        "|---|---|---|---|---|---|---|---|",
    ]
    russian = [m for m in catalog.MODELS if m.ru_quality != Quality.NONE]
    russian.sort(key=lambda m: (catalog.mean_ru_wer(m) if catalog.mean_ru_wer(m) is not None else 99))
    for model in russian:
        mean = catalog.mean_ru_wer(model)
        best = model.best_ru_wer
        out.append(
            f"| `{model.id}` | {fmt(mean, ' %') if mean is not None else '—'} | "
            f"{fmt(best, ' %') if best is not None else '—'} | {model.license} | "
            f"{fmt(model.params_m)} | {fmt(model.disk_mb)} | {yes(model.streaming)} | "
            f"{yes(model.punctuation)} |")
    out.append("")

    # --- Полная таблица ---------------------------------------------------
    out += [
        "## Полный каталог",
        "",
        "| Модель | Семейство | Движок | Русский | Языки | Лицензия | Коммерч. | "
        "Парам., млн | Диск, МБ | VRAM, ГБ | RTFx | Поток | Пункт. | Диар. | Перевод | Таймкоды |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for model in sorted(catalog.MODELS, key=lambda m: (m.family, m.id)):
        languages = ", ".join(model.languages[:3])
        if len(model.languages) > 3:
            languages += f" (+{len(model.languages) - 3})"
        out.append(
            f"| `{model.id}` | {model.family} | `{model.engine}` | "
            f"{QUALITY_RU[model.ru_quality]} | {languages} | {model.license} | "
            f"{yes(model.commercial_use)} | {fmt(model.params_m)} | {fmt(model.disk_mb)} | "
            f"{fmt(model.vram_gb)} | {fmt(model.rtfx)} | {yes(model.streaming)} | "
            f"{yes(model.punctuation)} | {yes(model.diarization)} | "
            f"{yes(model.translation)} | {TIMESTAMPS_RU[model.timestamps.value]} |")
    out.append("")

    # --- Карточки моделей --------------------------------------------------
    out += ["## Карточки моделей", ""]
    families: dict[str, list] = {}
    for model in catalog.MODELS:
        families.setdefault(model.family, []).append(model)

    for family in sorted(families):
        out += [f"### {family}", ""]
        for model in families[family]:
            out += [f"#### {model.name}", ""]
            rows = [
                ("Идентификатор", f"`{model.id}`"),
                ("Движок", f"`{model.engine}`"),
                ("Источник", f"`{model.source}`" +
                 (f" (ветка `{model.revision}`)" if model.revision else "")),
                ("Лицензия", model.license + (
                    " — коммерческое использование разрешено" if model.commercial_use
                    else " — **некоммерческая**")),
                ("Языки", ", ".join(model.languages)),
                ("Качество на русском", QUALITY_RU[model.ru_quality]),
                ("Параметров", fmt(model.params_m, " млн")),
                ("Размер на диске", fmt(model.disk_mb, " МБ")),
                ("Видеопамять", fmt(model.vram_gb, " ГБ")),
                ("Оперативная память", fmt(model.ram_gb, " ГБ")),
                ("Максимальный фрагмент", fmt(model.max_audio_s, " с") if model.max_audio_s
                 else "не ограничен"),
                ("RTFx", fmt(model.rtfx) + (f" ({model.rtfx_hw})" if model.rtfx_hw else "")),
                ("Потоковый режим", yes(model.streaming)),
                ("Пунктуация", yes(model.punctuation)),
                ("Диаризация", yes(model.diarization)),
                ("Перевод", yes(model.translation)),
                ("Таймкоды", TIMESTAMPS_RU[model.timestamps.value]),
                ("Требует токен Hugging Face", yes(model.gated)),
                ("Зрелость", MATURITY_RU.get(model.maturity.value, model.maturity.value)),
                ("Релиз", model.released or "—"),
            ]
            out.append("| Характеристика | Значение |")
            out.append("|---|---|")
            out += [f"| {key} | {value} |" for key, value in rows]
            out.append("")

            if model.benchmarks:
                out += ["**Измерения качества**", "",
                        "| Набор данных | Метрика | Значение | Язык | Источник |",
                        "|---|---|---|---|---|"]
                for bench in model.benchmarks:
                    note = f" ({bench.note})" if bench.note else ""
                    out.append(f"| {bench.dataset} | {bench.metric} | {bench.value:g} | "
                               f"{bench.language} | {bench.source}{note} |")
                out.append("")

            if model.strengths:
                out += ["**Сильные стороны**", ""]
                out += [f"- {item}" for item in model.strengths]
                out.append("")
            if model.weaknesses:
                out += ["**Ограничения**", ""]
                out += [f"- {item}" for item in model.weaknesses]
                out.append("")
            if model.recommended_for:
                out += ["**Рекомендуется для:** " + ", ".join(model.recommended_for), ""]
            if model.not_recommended_for:
                out += ["**Не подходит для:** " + ", ".join(model.not_recommended_for), ""]
            if model.notes:
                out += [f"> {model.notes}", ""]

    # --- Исключённые модели -------------------------------------------------
    out += [
        "## Модели, сознательно не включённые в каталог",
        "",
        "Эти модели встречаются в обзорах и подборках, но в ASR Hub их нет. "
        "Причины ниже — чтобы не пришлось выяснять их самостоятельно.",
        "",
        "| Модель | Лицензия | Почему не включена |",
        "|---|---|---|",
    ]
    for item in catalog.EXCLUDED_MODELS:
        out.append(f"| {item['name']} | {item['license']} | {item['reason']} |")
    out.append("")

    # --- Источники ------------------------------------------------------------
    out += [
        "## Источники данных",
        "",
        "Каждое числовое значение в каталоге сопровождается ссылкой на первоисточник. "
        "Полный список источников:",
        "",
        "| Ключ | Источник |",
        "|---|---|",
    ]
    for key, source in sorted(catalog.SOURCES.items()):
        out.append(f"| `{key}` | {source} |")
    out += [
        "",
        f"Каталог собран по состоянию на **{catalog.CATALOG_DATE}**. Модели выходят "
        "постоянно: перед принятием решения сверяйтесь с первоисточниками.",
        "",
    ]
    return "\n".join(out) + "\n"


# ===========================================================================
# Пресеты
# ===========================================================================

def generate_presets() -> str:
    out = [
        "# Готовые наборы настроек",
        "",
        "Пресет задаёт только те параметры, которые отличаются от значений по умолчанию. "
        "Это удобная отправная точка: возьмите ближайший к вашей задаче, прогоните "
        "десяток типовых файлов и правьте отдельные параметры.",
        "",
        "Применить пресет можно тремя способами: в веб-интерфейсе на вкладке "
        "«Транскрибация», командой `POST /api/presets/<id>/apply` (меняет настройки "
        "сервера) или передав те же значения в поле `settings` конкретного задания.",
        "",
    ]
    for preset in catalog.PRESETS:
        out += [
            f"## {preset.name}",
            "",
            f"**Идентификатор:** `{preset.id}`",
            "",
            preset.description,
            "",
            f"- **Сценарий:** {preset.scenario}",
            f"- **Требования к железу:** {preset.hardware_hint}",
        ]
        if preset.expected:
            out.append(f"- **Чего ожидать:** {preset.expected}")
        out += ["", "| Параметр | Значение | Что делает |", "|---|---|---|"]
        for key, value in preset.values.items():
            spec = catalog.get_param(key)
            label = spec.label if spec else key
            summary = clip(spec.description.split(".")[0], 90) if spec else ""
            out.append(f"| {label} (`{key}`) | `{value}` | {summary} |")
        out += ["", "**Как применить**", "", "```bash",
                f"curl -X POST http://сервер:8080/api/presets/{preset.id}/apply \\",
                "  -H \"X-API-Key: ваш_ключ\"", "```", ""]
    return "\n".join(out) + "\n"


def main() -> int:
    print("Сборка справочных разделов документации:")
    for name, builder in (("04-parameters.md", generate_parameters),
                          ("03-models.md", generate_models),
                          ("13-presets.md", generate_presets)):
        text = builder()
        path = DOCS / name
        path.write_text(text, encoding="utf-8")
        print(f"  {name} — {len(text.splitlines())} строк, {len(text) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
