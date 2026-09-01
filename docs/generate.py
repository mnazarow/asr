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


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское согласование существительного с числительным."""
    tail100 = number % 100
    tail10 = number % 10
    if 11 <= tail100 <= 14:
        word = many
    elif tail10 == 1:
        word = one
    elif 2 <= tail10 <= 4:
        word = few
    else:
        word = many
    return f"{number} {word}"


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




# ===========================================================================
# Раздел 16. Мониторинг
# ===========================================================================

def generate_monitoring() -> str:
    """Собирает справочную часть раздела о мониторинге из каталога метрик."""
    from asrhub.monitoring import catalog as mon

    stats = mon.stats()
    out: list[str] = []
    add = out.append

    add("# Мониторинг\n")
    add("![Устройство мониторинга](images/diag-07-monitoring.png)\n")
    add("Сервис отдаёт наружу **"
        + plural(stats["total"], "метрику", "метрики", "метрик") + "** в "
        + plural(stats["groups"], "группе", "группах", "группах") + ": "
        + plural(stats["gauges"], "мгновенное значение", "мгновенных значения",
                 "мгновенных значений") + ", "
        + plural(stats["counters"], "счётчик", "счётчика", "счётчиков") + " и "
        + plural(stats["histograms"], "гистограмма", "гистограммы", "гистограмм")
        + ". У " + plural(stats["with_thresholds"], "метрики", "метрик", "метрик")
        + " заданы пороги тревоги, у каждой есть описание и рекомендация.\n")
    add("Раздел собран из каталога метрик сервера, поэтому описание не может разойтись "
        "с тем, что сервис отдаёт на самом деле. Тот же каталог доступен программно: "
        "`GET /api/monitoring/catalog`.\n")
    add("Подробный справочник по каждому маршруту — с параметрами, схемами ответов "
        "и настоящими примерами — вынесен отдельно: "
        "«Программный интерфейс мониторинга».\n")

    # --- быстрый старт ------------------------------------------------------
    add("## С чего начать\n")
    add("Три шага, после которых мониторинг работает:\n")
    add("```bash")
    add("# 1. Убедиться, что метрики отдаются")
    add("curl http://сервер:8080/api/monitoring/metrics | head -20")
    add("")
    add("# 2. Забрать готовый блок для prometheus.yml")
    add("curl http://сервер:8080/api/monitoring/config/prometheus-scrape >> prometheus.yml")
    add("")
    add("# 3. Забрать готовые правила оповещения и панель Grafana")
    add("curl http://сервер:8080/api/monitoring/config/prometheus -o asrhub-rules.yml")
    add("curl http://сервер:8080/api/monitoring/config/grafana -o asrhub-dashboard.json")
    add("```\n")
    add("> **Рекомендация.** Начните с пяти метрик и не пытайтесь следить за всеми сразу. "
        "`asrhub_up`, `asrhub_queue_depth`, `asrhub_disk_free_gb`, `asrhub_rtf` и доля "
        "неудачных заданий закрывают почти все аварии, которые случаются на практике. "
        "Остальное пригодится, когда будете разбираться в причинах.\n")

    # --- точки доступа ------------------------------------------------------
    add("## Точки доступа\n")
    add("| Адрес | Что отдаёт | Ключ |")
    add("|---|---|---|")
    rows = [
        ("`GET /api/monitoring/metrics`",
         "Все метрики. Формат — параметр `format`", "не нужен*"),
        ("`GET /api/monitoring/metrics.json`",
         "Снимок с описанием, рекомендацией и порогом каждой метрики", "не нужен*"),
        ("`GET /api/monitoring/health`",
         "Сводное состояние: живость, готовность, запуск, тревоги", "не нужен"),
        ("`GET /api/monitoring/live`", "Проба живости для оркестратора", "не нужен"),
        ("`GET /api/monitoring/ready`", "Проба готовности для балансировщика", "не нужен"),
        ("`GET /api/monitoring/startup`", "Проба завершения запуска", "не нужен"),
        ("`GET /api/monitoring/catalog`", "Справочник метрик целиком", "нужен"),
        ("`GET /api/monitoring/catalog/{имя}`", "Описание одной метрики", "нужен"),
        ("`GET /api/monitoring/alerts`", "Состояние тревог", "нужен"),
        ("`GET /api/monitoring/alerts/history`", "История срабатываний", "нужен"),
        ("`GET`/`PUT /api/monitoring/alerts/rules`", "Правила оповещения", "нужен"),
        ("`GET`/`PUT /api/monitoring/targets`", "Приёмники метрик", "нужен"),
        ("`POST /api/monitoring/targets/test`", "Проверить приёмник немедленно", "нужен"),
        ("`GET /api/monitoring/config/prometheus`", "Готовые правила оповещения", "не нужен*"),
        ("`GET /api/monitoring/config/grafana`", "Готовая панель", "не нужен*"),
        ("`GET /api/monitoring/config/zabbix`", "Готовый шаблон", "не нужен*"),
        ("`GET /api/monitoring/info`", "Состояние самой подсистемы мониторинга", "нужен"),
    ]
    for path, what, key in rows:
        add(f"| {path} | {what} | {key} |")
    add("")
    add("\\* Пока включена настройка `monitoring_public` (по умолчанию включена). "
        "Так сделано намеренно: Prometheus не умеет обновлять истекающие ключи, и "
        "правильное место для ограничения доступа — прокси, а не приложение.\n")

    # --- форматы ------------------------------------------------------------
    add("## Форматы выгрузки\n")
    add("Один и тот же снимок отдаётся в семи форматах — параметром `format`.\n")
    add("| Значение | Формат | Для чего |")
    add("|---|---|---|")
    formats = [
        ("`prometheus`", "текстовый формат Prometheus", "умолчание; понимают почти все системы"),
        ("`openmetrics`", "OpenMetrics 1.0", "строгий стандарт, требуется некоторым сборщикам"),
        ("`json`", "JSON с описаниями", "своя система; получатель видит и число, и его смысл"),
        ("`otlp`", "OTLP/HTTP", "OpenTelemetry Collector"),
        ("`influx`", "InfluxDB line protocol", "InfluxDB, Telegraf, VictoriaMetrics"),
        ("`graphite`", "Graphite plaintext", "Graphite, StatsD, Carbon"),
        ("`zabbix`", "JSON для zabbix_sender", "Zabbix"),
        ("`csv`", "плоская таблица", "разовая выгрузка в таблицу"),
    ]
    for value, name, why in formats:
        add(f"| {value} | {name} | {why} |")
    add("")
    add("```bash")
    add("curl 'http://сервер:8080/api/monitoring/metrics?format=influx'")
    add("curl 'http://сервер:8080/api/monitoring/metrics?format=zabbix&host=asr-01'")
    add("```\n")

    add(MONITORING_SETUP)

    # --- справочник метрик --------------------------------------------------
    add("## Справочник метрик\n")
    add("Для каждой метрики указано, что она означает, какое значение считать обычным, "
        "при каком пороге поднимать тревогу и что делать, когда она сработала. "
        "Пороги — отправная точка: их придётся подогнать под свой поток.\n")

    add("### Сводка по группам\n")
    add("| Группа | Метрик | С порогами | О чём |")
    add("|---|---|---|---|")
    for group in mon.GROUPS:
        items = mon.metrics_for_group(group["id"])
        with_threshold = sum(1 for m in items if m.threshold)
        add(f"| **{group['title']}** | {len(items)} | {with_threshold} | {group['description']} |")
    add("")

    for group in mon.GROUPS:
        items = mon.metrics_for_group(group["id"])
        if not items:
            continue
        add(f"### {group['title']}\n")
        add(f"{group['description']}\n")
        for spec in items:
            add(f"#### {spec.label}\n")
            meta = [f"**Метрика:** `{spec.name}`", f"**Тип:** {TYPE_RU[spec.type]}"]
            if spec.unit:
                meta.append(f"**Единица:** {spec.unit}")
            if spec.labels:
                meta.append("**Метки:** " + ", ".join(f"`{label}`" for label in spec.labels))
            add(" · ".join(meta) + "\n")
            add(spec.description + "\n")
            if spec.normal:
                add(f"**Обычное значение:** {spec.normal}\n")
            if spec.recommendation:
                add(f"> **Рекомендация.** {spec.recommendation}\n")
            if spec.threshold:
                threshold = spec.threshold
                word = "выше" if threshold.direction == "above" else "ниже"
                parts = []
                if threshold.warning is not None:
                    parts.append(f"предупреждение — {word} {threshold.warning}")
                if threshold.critical is not None:
                    parts.append(f"критично — {word} {threshold.critical}")
                line = f"**Порог:** {'; '.join(parts)}"
                if threshold.for_seconds:
                    line += f"; выдержка {threshold.for_seconds} с"
                add(line + ".\n")
                if threshold.note:
                    add(f"⚠️ {threshold.note}\n")
            if spec.troubleshooting:
                add(f"**Что делать при срабатывании:** {spec.troubleshooting}\n")
            if spec.since_restart:
                add("Счётчик обнуляется при перезапуске сервиса — это нормально: "
                    "функция `rate()` в Prometheus обрабатывает обнуление правильно.\n")

    add(MONITORING_TAIL)
    return "\n".join(out) + "\n"


MONITORING_SETUP = """## Раздел «Мониторинг» в интерфейсе

![Раздел мониторинга](images/18-monitoring.png)

Всё, что описано ниже, видно и настраивается из браузера: состояние проб, тревоги
с их порогами, приёмники метрик, ссылки на выгрузку и справочник по каждой метрике.

Верхняя строка отвечает на вопрос «всё ли в порядке» одним взглядом: состояние,
число тревог, сколько метрик в снимке, сколько было опросов.

### Пробы состояния

Три пробы отвечают на три разных вопроса, и путать их дорого.

| Проба | Вопрос | Что делает оркестратор при провале |
|---|---|---|
| `live` | процесс жив? | перезапускает контейнер |
| `ready` | можно ли слать запросы? | снимает нагрузку, контейнер оставляет |
| `startup` | запуск закончился? | ждёт, не считая две другие пробы |

⚠️ Частая и дорогая ошибка — повесить на `live` проверку базы или очереди. Тогда
короткая недоступность диска приводит к бесконечному циклу перезапусков, а при
каждом перезапуске заново загружаются веса моделей. Проверка внешних зависимостей
— это `ready`, и только она.

Проба `ready` намеренно не считает поставленную на паузу очередь отказом: пауза —
осознанное действие оператора, и снимать сервер с нагрузки во время обслуживания
не нужно. Она отмечается предупреждением.

### Справочник метрик прямо в интерфейсе

![Справочник метрик](images/19-metric-catalog.png)

Каждая метрика разворачивается в карточку: описание, обычное значение,
рекомендация, порог с выдержкой и что делать при срабатывании. Тот же текст
возвращает `GET /api/monitoring/catalog` — если вы строите свою панель, подсказки
не придётся писать заново.

## Настройка Prometheus

Сервер отдаёт готовый блок для `prometheus.yml`:

```bash
curl http://сервер:8080/api/monitoring/config/prometheus-scrape
```

```yaml
scrape_configs:
  - job_name: asrhub
    metrics_path: /api/monitoring/metrics
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ['asr.company.ru:8080']
```

> **Рекомендация.** Интервал сбора чаще 15 секунд смысла не имеет: замеры процессора,
> памяти и видеокарты обновляются раз в 20 секунд служебным циклом сервера, и более
> частый опрос вернёт те же самые числа, потратив ресурсы на запросы к базе. 30 секунд
> — разумное умолчание.

Если `monitoring_public` выключен, добавьте ключ:

```yaml
    authorization:
      type: Bearer
      credentials: ah_ваш_ключ
```

### Правила оповещения

Готовый файл правил собирается из порогов каталога:

```bash
curl http://сервер:8080/api/monitoring/config/prometheus -o /etc/prometheus/asrhub-rules.yml
promtool check rules /etc/prometheus/asrhub-rules.yml
curl -X POST http://prometheus:9090/-/reload
```

Внутри — правила вида:

```yaml
- alert: ASRHubDiskFreeGbCritical
  expr: asrhub_disk_free_gb < 5
  for: 300s
  labels: { severity: critical }
  annotations:
    summary: 'Свободно на диске: ниже 5 ГБ'
    description: 'POST /api/maintenance/cleanup, затем bash scripts/models.sh disk'
```

Часть правил считается не прямым сравнением, а выражением — иначе они были бы
бессмысленны:

```promql
# доля неудачных заданий, а не их число: ночной прогон архива
# иначе поднимет тревогу на ровном месте
sum(rate(asrhub_jobs_total{status="failed"}[30m]))
  / clamp_min(sum(rate(asrhub_jobs_total[30m])), 0.001) > 0.2

# видеопамять в процентах от общего объёма, а не в мегабайтах
asrhub_gpu_memory_mb / clamp_min(asrhub_gpu_memory_total_mb, 1) * 100 > 90

# недоступность ловится через absent(): если сервис лежит,
# метрики нет вообще, и сравнивать её значение не с чем
absent(asrhub_up) == 1
```

⚠️ Пороги в готовом файле — отправная точка, а не истина. Очередь из ста заданий
бывает и нормой, и аварией: это зависит от вашего потока. Прогоните файл неделю,
посмотрите, какие правила срабатывают впустую, и поднимите их пороги. Правило,
которое дежурный привык игнорировать, хуже отсутствующего.

## Панель Grafana

```bash
curl http://сервер:8080/api/monitoring/config/grafana -o asrhub-dashboard.json
```

Импортируется как есть: Dashboards → Import → Upload JSON. Панель собирается по
группам каталога метрик, поэтому не расходится с тем, что сервер отдаёт.

Что стоит вынести на первый экран собственной панели:

| Панель | Запрос |
|---|---|
| Состояние | `asrhub_up` |
| Очередь и её возраст | `asrhub_queue_depth`, `asrhub_queue_oldest_seconds` |
| Скорость | `asrhub_rtf{stat="p95"}` |
| Доля отказов | `sum(rate(asrhub_jobs_total{status="failed"}[30m])) / clamp_min(sum(rate(asrhub_jobs_total[30m])), 0.001)` |
| Место на диске | `asrhub_disk_free_gb` |
| Видеопамять, % | `asrhub_gpu_memory_mb / asrhub_gpu_memory_total_mb * 100` |
| Часы аудио в час | `rate(asrhub_audio_seconds_total[1h]) * 3.6` |

## Zabbix

```bash
curl http://сервер:8080/api/monitoring/config/zabbix -o asrhub-template.yaml
```

Импорт: Настройка → Шаблоны → Импорт. Элементы создаются типом «Zabbix trapper»,
данные отправляет сам сервер:

```yaml
monitoring_targets:
  - kind: webhook
    url: http://zabbix-proxy:10051/
    interval_s: 60
```

Либо забирайте HTTP-агентом с `/api/monitoring/metrics?format=zabbix&host=asr-01`.

## OpenTelemetry

```yaml
monitoring_targets:
  - kind: otlp
    url: http://otel-collector:4318
    interval_s: 30
```

Метрики уходят в общий сборщик телеметрии в формате OTLP/HTTP и дальше — куда
настроен коллектор. Пакет `opentelemetry` на сервере не нужен: тело запроса
собирается вручную, лишняя зависимость на сервере распознавания ни к чему.

## Kubernetes

```yaml
livenessProbe:
  httpGet: { path: /api/monitoring/live, port: 8080 }
  periodSeconds: 20
  failureThreshold: 3

readinessProbe:
  httpGet: { path: /api/monitoring/ready, port: 8080 }
  periodSeconds: 10
  failureThreshold: 2

# Загрузка весов модели занимает десятки секунд, а на холодном кеше — минуты.
# Без startupProbe контейнер будет убит до того, как успеет запуститься.
startupProbe:
  httpGet: { path: /api/monitoring/startup, port: 8080 }
  periodSeconds: 10
  failureThreshold: 60
```

Для сбора метрик оператором Prometheus:

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: asrhub
spec:
  selector:
    matchLabels: { app: asrhub }
  endpoints:
    - port: http
      path: /api/monitoring/metrics
      interval: 30s
```

## Отправка метрик наружу

Забирать метрики опросом надёжнее: система сбора сразу видит, что сервер замолчал.
Отправка нужна там, где до сервера не достучаться — закрытый контур, NAT, машина
без постоянного адреса.

![Добавление приёмника](images/20-monitoring-target.png)

```yaml
monitoring_push_enabled: true
monitoring_targets:
  - kind: prometheus_pushgateway
    url: http://pushgw:9091
    interval_s: 60
  - kind: influxdb
    url: http://influx:8086
    database: asrhub
    interval_s: 30
```

Проверить настройку можно до сохранения — кнопкой «Проверить» в интерфейсе или
запросом:

```bash
curl -X POST http://сервер:8080/api/monitoring/targets/test \\
  -H "X-API-Key: ключ" -H "Content-Type: application/json" \\
  -d '{"kind": "influxdb", "url": "http://influx:8086", "database": "asrhub"}'
```

```json
{"ok": true, "sent_metrics": 118}
```

⚠️ Настроив отправку, заведите оповещение на `asrhub_push_targets_healthy`.
Молчащий приёмник означает не отсутствие проблем, а то, что вы перестали их видеть,
— и это опаснее любой тревоги.
"""


MONITORING_TAIL = """## Тревоги внутри сервера

Пороги считаются и на стороне сервиса — на случай, когда внешнего мониторинга нет.
Состояния устроены как у Prometheus:

```
норма → наблюдение → тревога → снята
```

Промежуточное «наблюдение» существует, чтобы одиночный всплеск не будил дежурного:
тревога поднимается, только если условие держится дольше выдержки. Смена состояния
пишется в ленту событий сервера, поэтому историю видно и в разделе «Журнал».

```bash
curl -H "X-API-Key: ключ" http://сервер:8080/api/monitoring/alerts
curl -H "X-API-Key: ключ" http://сервер:8080/api/monitoring/alerts/history
```

Свои пороги:

```bash
curl -X PUT http://сервер:8080/api/monitoring/alerts/rules \\
  -H "X-API-Key: ключ" -H "Content-Type: application/json" \\
  -d '[{"metric": "asrhub_queue_depth", "direction": "above",
        "threshold": 500, "severity": "warning", "for_seconds": 1800}]'
```

Вернуть пороги каталога: `POST /api/monitoring/alerts/rules/reset`.

> **Рекомендация.** Если Prometheus у вас есть, оповещения держите в нём: там
> история, группировка, подавление и маршрутизация дежурным. Встроенные тревоги —
> для установки без внешнего мониторинга, чтобы кончающийся диск было видно хотя
> бы в интерфейсе.

## Если что-то не работает

**Метрики отдаются, но половины значений нет.** Посмотрите
`GET /api/monitoring/info` — поле `collection_errors` перечисляет источники,
которые не удалось опросить, с причиной. Сбой одного источника не лишает вас
остальных метрик: это сделано намеренно, чтобы неработающий `nvidia-smi` не
оставлял без данных об очереди.

**В Prometheus метрики есть, но все старые.** Проверьте `monitoring_cache_ttl_s`:
если он больше интервала сбора, вы получаете один и тот же снимок несколько раз.
Значение должно быть примерно вдвое меньше интервала.

**Метки маршрутов размножились.** Такого быть не должно: маршрут берётся из шаблона
(`/api/jobs/{job_id}`), а не из конкретного адреса. Если вы видите метки с
идентификаторами заданий — сообщите об этом, это ошибка.

**Хранилище метрик распухает.** Разрезы по моделям ограничены сорока значениями,
но при большом числе моделей и владельцев объём всё равно растёт. Уберите ненужные
разрезы на стороне Prometheus через `metric_relabel_configs`.

**Тревога срабатывает впустую.** Поднимите порог или выдержку — правило, которое
дежурный привык игнорировать, хуже отсутствующего. Пороги каталога рассчитаны на
типичную установку и не знают вашего потока.

**Отправка не доходит.** `GET /api/monitoring/targets` показывает по каждому
приёмнику время последней попытки, время последнего успеха и текст ошибки.
Сбой отправки никогда не влияет на работу сервиса — задания продолжают
обрабатываться.

## Что мониторить в первую очередь

Если заводите оповещения с нуля, начните с этих шести и не добавляйте остальные,
пока эти не отработают неделю без ложных срабатываний.

| Что | Выражение | Почему именно это |
|---|---|---|
| Сервис не отвечает | `absent(asrhub_up) == 1` | Самая частая авария; всё остальное вторично |
| Кончается диск | `asrhub_disk_free_gb < 20` | Заполненный диск повреждает базу и результаты |
| Очередь растёт | `asrhub_queue_depth > 50` за 15 мин | Не хватает мощности; чем раньше видно, тем дешевле |
| Люди ждут | `asrhub_queue_oldest_seconds > 1800` | То, что чувствует пользователь, а не сервер |
| Задания падают | доля `failed` > 5 % за 30 мин | Отличает поломку от единичного плохого файла |
| Нет ни одного движка | `asrhub_engines_available < 1` | Обычно означает, что обновление сломало зависимости |
"""


TYPE_RU = {
    "gauge": "мгновенное значение",
    "counter": "счётчик",
    "histogram": "гистограмма",
    "info": "справочная",
}


def main() -> int:
    print("Сборка справочных разделов документации:")
    for name, builder in (("04-parameters.md", generate_parameters),
                          ("03-models.md", generate_models),
                          ("13-presets.md", generate_presets),
                          ("16-monitoring.md", generate_monitoring)):
        text = builder()
        path = DOCS / name
        path.write_text(text, encoding="utf-8")
        print(f"  {name} — {len(text.splitlines())} строк, {len(text) // 1024} КБ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
