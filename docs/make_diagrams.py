#!/usr/bin/env python3
"""Генератор схем для документации ASR Hub.

Схемы собираются как SVG и конвертируются в PNG. Палитра — та же, что
в веб-интерфейсе (проверена валидатором на различимость при цветовой
слепоте), шрифт системный, фон светлый: схемы попадают и в Word, и в печать.

    python3 docs/make_diagrams.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

OUT = Path(__file__).resolve().parent / "images"
OUT.mkdir(parents=True, exist_ok=True)

# Палитра для светлой поверхности (та же, что в charts.js)
BLUE = "#2a78d6"
ORANGE = "#eb6834"
AQUA = "#1baf7a"
YELLOW = "#eda100"
MAGENTA = "#e87ba4"
VIOLET = "#4a3aa7"
RED = "#e34948"
GREEN = "#008300"

INK = "#121820"
DIM = "#55637a"
FAINT = "#808da0"
LINE = "#d7dee8"
SURFACE = "#ffffff"
PANEL = "#f5f7fa"

FONT = ("-apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', "
        "'DejaVu Sans', 'Liberation Sans', Arial, sans-serif")
MONO = "'DejaVu Sans Mono', 'Liberation Mono', Consolas, monospace"


class Canvas:
    """Минимальный построитель SVG: прямоугольники, стрелки, подписи."""

    def __init__(self, width: int, height: int, title: str = "", subtitle: str = ""):
        self.width = width
        self.height = height
        self.parts: list[str] = []
        self.defs: list[str] = []
        self._arrow_markers: set[str] = set()
        self.top = 24
        if title:
            self.text(28, 34, title, size=19, weight=650, fill=INK)
            self.top = 52
        if subtitle:
            self.text(28, self.top + 4, subtitle, size=12.5, fill=DIM)
            self.top += 22

    # --- примитивы --------------------------------------------------------

    def rect(self, x, y, w, h, *, fill=SURFACE, stroke=LINE, rx=8, width=1.4,
             dash: str = "", opacity: float = 1.0):
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{width}" '
            f'opacity="{opacity}"{dash_attr}/>')

    def text(self, x, y, content, *, size=13, fill=INK, weight=400, anchor="start",
             mono=False, opacity=1.0):
        family = MONO if mono else FONT
        self.parts.append(
            f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}" '
            f'opacity="{opacity}">{escape(str(content))}</text>')

    def lines(self, x, y, items, *, size=12, fill=DIM, leading=16, anchor="start", mono=False):
        for index, item in enumerate(items):
            self.text(x, y + index * leading, item, size=size, fill=fill,
                      anchor=anchor, mono=mono)

    def line(self, x1, y1, x2, y2, *, stroke=LINE, width=1.4, dash=""):
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        self.parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linecap="round"{dash_attr}/>')

    def _marker(self, colour: str) -> str:
        ident = "arrow" + colour.replace("#", "")
        if ident not in self._arrow_markers:
            self._arrow_markers.add(ident)
            self.defs.append(
                f'<marker id="{ident}" viewBox="0 0 10 10" refX="9" refY="5" '
                f'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
                f'<path d="M 0 0 L 10 5 L 0 10 z" fill="{colour}"/></marker>')
        return ident

    def arrow(self, x1, y1, x2, y2, *, stroke=FAINT, width=1.6, dash="", label="",
              label_offset=-8, curve: float = 0.0):
        marker = self._marker(stroke)
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        if abs(curve) > 0.01:
            mx, my = (x1 + x2) / 2, (y1 + y2) / 2
            dx, dy = x2 - x1, y2 - y1
            length = max(1.0, (dx * dx + dy * dy) ** 0.5)
            nx, ny = -dy / length, dx / length
            cx, cy = mx + nx * curve, my + ny * curve
            path = f"M {x1} {y1} Q {cx} {cy} {x2} {y2}"
            self.parts.append(
                f'<path d="{path}" fill="none" stroke="{stroke}" stroke-width="{width}" '
                f'marker-end="url(#{marker})"{dash_attr}/>')
            label_x, label_y = cx, cy
        else:
            self.parts.append(
                f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{stroke}" '
                f'stroke-width="{width}" marker-end="url(#{marker})"{dash_attr}/>')
            label_x, label_y = (x1 + x2) / 2, (y1 + y2) / 2
        if label:
            self.text(label_x, label_y + label_offset, label, size=11.5,
                      fill=stroke, anchor="middle")

    # --- составные элементы ------------------------------------------------

    def box(self, x, y, w, h, title, items=(), *, accent=BLUE, fill=SURFACE,
            title_size=13.5, dash="", note=""):
        self.rect(x, y, w, h, fill=fill, stroke=accent, dash=dash)
        self.rect(x, y, 4, h, fill=accent, stroke=accent, rx=2)
        self.text(x + 16, y + 22, title, size=title_size, weight=600, fill=INK)
        if items:
            self.lines(x + 16, y + 42, items, size=11.5, fill=DIM, leading=15)
        if note:
            self.text(x + 16, y + h - 10, note, size=10.5, fill=FAINT)

    def group(self, x, y, w, h, label, *, stroke=LINE, fill=PANEL):
        self.rect(x, y, w, h, fill=fill, stroke=stroke, rx=12, dash="6 5", width=1.2)
        self.text(x + 14, y + 18, label, size=11.5, weight=600, fill=FAINT)

    def badge(self, x, y, text, *, colour=BLUE, width=None):
        approx = width or (len(str(text)) * 6.6 + 16)
        self.rect(x, y, approx, 20, fill=SURFACE, stroke=colour, rx=10, width=1.2)
        self.text(x + approx / 2, y + 14, text, size=11, fill=colour, anchor="middle")
        return approx

    # --- вывод --------------------------------------------------------------

    def render(self) -> str:
        defs = f"<defs>{''.join(self.defs)}</defs>" if self.defs else ""
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.width}" '
                f'height="{self.height}" viewBox="0 0 {self.width} {self.height}">'
                f'{defs}<rect width="{self.width}" height="{self.height}" fill="{SURFACE}"/>'
                f'{"".join(self.parts)}</svg>')

    def save(self, name: str, scale: float = 2.0) -> Path:
        svg_path = OUT / f"{name}.svg"
        svg_path.write_text(self.render(), encoding="utf-8")
        png_path = OUT / f"{name}.png"
        try:
            import cairosvg

            cairosvg.svg2png(bytestring=self.render().encode("utf-8"),
                             write_to=str(png_path), scale=scale)
        except Exception as exc:                      # noqa: BLE001
            print(f"  ! {name}: не удалось получить PNG ({exc})")
            return svg_path
        print(f"  {png_path.name} — {png_path.stat().st_size // 1024} КБ")
        return png_path


# ===========================================================================
# Схема 1. Общая архитектура
# ===========================================================================

def diagram_architecture() -> None:
    c = Canvas(1500, 880, "Архитектура ASR Hub",
               "Клиенты обращаются к одному серверу; тяжёлые вычисления и модели остаются на нём")

    c.group(30, 95, 300, 300, "КЛИЕНТЫ")
    c.box(48, 128, 264, 74, "Веб-интерфейс", ["браузер, WebSocket для прогресса"], accent=BLUE)
    c.box(48, 214, 264, 74, "asrctl", ["Linux, macOS, Windows"], accent=BLUE)
    c.box(48, 300, 264, 74, "Программный интерфейс", ["REST, вебхуки, Prometheus"], accent=BLUE)

    c.group(370, 95, 470, 690, "СЕРВЕР ASR HUB")
    c.box(390, 128, 430, 74, "HTTP-интерфейс (FastAPI)",
          ["ключи доступа, лимиты частоты, загрузка файлов"], accent=VIOLET)
    c.box(390, 216, 430, 96, "Очередь заданий",
          ["четыре политики планирования, приоритеты",
           "повторы с нарастающей задержкой, отмена",
           "кеш результатов по содержимому файла"], accent=ORANGE)
    c.box(390, 326, 430, 118, "Конвейер обработки",
          ["ffmpeg: формат, громкость, каналы",
           "детектор речи: нарезка по паузам",
           "распознавание, диаризация",
           "пунктуация, числа, словарь замен"], accent=AQUA)
    c.box(390, 458, 430, 96, "Реестр движков",
          ["ленивая загрузка, кеш моделей в памяти",
           "выгрузка по простою, единый формат ошибок"], accent=YELLOW)
    c.box(390, 568, 430, 96, "Хранилище SQLite",
          ["задания, сегменты, события",
           "метрики и снимки состояния системы"], accent=MAGENTA)
    c.box(390, 678, 430, 88, "Аналитика",
          ["RTF, перцентили, WER по эталону",
           "сравнение моделей, разбор ошибок"], accent=GREEN)

    c.group(880, 95, 300, 690, "ДВИЖКИ")
    engines = [
        ("GigaAM", "русский, MIT", AQUA),
        ("faster-whisper", "мультиязычный, int8", BLUE),
        ("whisper.cpp", "CPU, Apple Silicon", BLUE),
        ("NVIDIA NeMo", "Parakeet, Canary", GREEN),
        ("T-one", "телефония, поток", ORANGE),
        ("Vosk", "CPU без GPU", YELLOW),
        ("Transformers", "MOSS, Granite, wav2vec2", VIOLET),
        ("ещё 10 движков", "Qwen3, Voxtral, sherpa…", FAINT),
    ]
    for index, (name, note, colour) in enumerate(engines):
        y = 128 + index * 80
        c.box(898, y, 264, 64, name, [note], accent=colour, title_size=12.5)

    c.group(1220, 95, 250, 400, "МОДЕЛИ И ДАННЫЕ")
    c.box(1238, 128, 214, 74, "Веса моделей", ["каталог 72 моделей", "загрузка по требованию"],
          accent=FAINT)
    c.box(1238, 214, 214, 74, "Загруженные файлы", ["срок хранения настраивается"], accent=FAINT)
    c.box(1238, 300, 214, 74, "Результаты", ["txt, srt, vtt, json, docx…"], accent=FAINT)
    c.box(1238, 386, 214, 74, "Журналы", ["ротация, без текста расшифровок"], accent=FAINT)

    c.arrow(312, 165, 388, 165, stroke=BLUE)
    c.arrow(312, 251, 388, 200, stroke=BLUE)
    c.arrow(312, 337, 388, 210, stroke=BLUE)
    c.arrow(605, 202, 605, 214, stroke=DIM)
    c.arrow(605, 312, 605, 326, stroke=DIM)
    c.arrow(605, 444, 605, 458, stroke=DIM)
    c.arrow(605, 554, 605, 568, stroke=DIM)
    c.arrow(605, 664, 605, 678, stroke=DIM)
    c.arrow(822, 506, 894, 470, stroke=YELLOW)
    c.text(858, 494, "выбор движка", size=11, fill=YELLOW, anchor="middle")
    c.arrow(1166, 170, 1234, 165, stroke=FAINT, dash="4 4")
    c.text(1200, 148, "загрузка весов", size=10.5, fill=FAINT, anchor="middle")

    c.text(30, 822, "Что важно в этой схеме", size=13, weight=600, fill=INK)
    c.lines(30, 844, [
        "• Клиент никогда не обращается к движку напрямую: между ними всегда очередь — "
        "сервер не падает от всплеска нагрузки.",
        "• Модели живут на сервере: клиентам не нужны ни видеокарта, ни установка Python, "
        "ни доступ к весам моделей.",
        "• Движки изолированы друг от друга: неустановленный или сломанный движок не мешает "
        "работать остальным.",
    ], size=11.5, fill=DIM, leading=18)
    c.save("diag-01-architecture")


# ===========================================================================
# Схема 2. Конвейер обработки задания
# ===========================================================================

def diagram_pipeline() -> None:
    c = Canvas(1500, 640, "Конвейер обработки одного задания",
               "Доли времени показаны для типичной записи совещания на 30 минут (GigaAM v3, GPU)")

    stages = [
        ("Приём файла", ["проверка формата и размера", "контрольная сумма", "поиск в кеше"],
         "≈1 %", BLUE),
        ("Подготовка аудио", ["ffmpeg: 16 кГц моно", "нормализация громкости",
                              "фильтр низких частот"], "10 %", AQUA),
        ("Детектор речи", ["Silero VAD", "нарезка по паузам", "склейка коротких участков"],
         "4 %", YELLOW),
        ("Распознавание", ["загрузка модели из кеша", "обработка пакетами",
                           "пословные таймкоды"], "78 %", ORANGE),
        ("Диаризация", ["кто и когда говорит", "привязка к словам"], "необязательно", VIOLET),
        ("Постобработка", ["фильтр галлюцинаций", "пунктуация и числа",
                           "словарь замен"], "5 %", MAGENTA),
        ("Выгрузка", ["txt, srt, vtt, json", "docx, csv", "запись в базу"], "2 %", GREEN),
    ]

    x = 30
    width = 196
    gap = 12
    for index, (title, items, share, colour) in enumerate(stages):
        c.box(x, 110, width, 150, title, items, accent=colour, title_size=12.5)
        c.badge(x + 16, 268, share, colour=colour)
        if index < len(stages) - 1:
            c.arrow(x + width, 185, x + width + gap - 2, 185, stroke=FAINT)
        x += width + gap

    c.text(30, 330, "Полоса времени", size=13, weight=600, fill=INK)
    total_width = 1440
    shares = [(1, BLUE), (10, AQUA), (4, YELLOW), (78, ORANGE), (5, MAGENTA), (2, GREEN)]
    x = 30
    for share, colour in shares:
        w = total_width * share / 100
        c.rect(x, 344, max(2, w - 2), 26, fill=colour, stroke=colour, rx=4)
        if w > 60:
            c.text(x + w / 2, 361, f"{share} %", size=11, fill="#ffffff", anchor="middle",
                   weight=600)
        x += w

    c.text(30, 412, "Где искать узкое место", size=13, weight=600, fill=INK)
    hints = [
        ("Подготовка аудио больше 20 %", "медленный диск для временных файлов либо включено "
         "тяжёлое шумоподавление", AQUA),
        ("Загрузка модели больше 15 %", "мал кеш моделей или задания слишком часто чередуют "
         "разные модели", YELLOW),
        ("Распознавание меньше 60 %", "модель простаивает: увеличьте размер пакета "
         "или число одновременных заданий", ORANGE),
        ("Постобработка больше 15 %", "отдельная модель пунктуации; возьмите модель "
         "с готовым форматированием (GigaAM E2E)", MAGENTA),
    ]
    for index, (title, text, colour) in enumerate(hints):
        y = 436 + index * 44
        c.rect(30, y, 1440, 36, fill=PANEL, stroke=LINE, rx=6, width=1)
        c.rect(30, y, 4, 36, fill=colour, stroke=colour, rx=2)
        c.text(46, y + 16, title, size=12, weight=600, fill=INK)
        c.text(46, y + 30, text, size=11.5, fill=DIM)

    c.text(30, 626, "Разбивка по этапам видна в разделе «Аналитика» — карточка «Время по этапам».",
           size=11.5, fill=FAINT)
    c.save("diag-02-pipeline")


# ===========================================================================
# Схема 3. Очередь и планирование
# ===========================================================================

def diagram_queue() -> None:
    c = Canvas(1500, 760, "Очередь: планирование, воркеры, повторы",
               "Как задание проходит путь от постановки до результата")

    c.box(30, 100, 250, 118, "Поступление",
          ["веб-интерфейс, asrctl, API", "пакетная загрузка группой",
           "приоритет 0–100", "срок выполнения (необязательно)"], accent=BLUE)

    c.box(320, 100, 300, 118, "Проверка дубликатов",
          ["хеш содержимого файла", "отпечаток настроек задания",
           "совпало — результат из кеша"], accent=AQUA)

    c.box(660, 100, 340, 118, "Выбор следующего задания",
          ["priority_fifo — приоритет, затем очередь",
           "shortest_first — сначала короткие",
           "fair_share — поровну между пользователями",
           "deadline — по сроку"], accent=ORANGE)

    c.box(1040, 100, 430, 118, "Ограничения",
          ["одновременных заданий всего",
           "одновременных заданий на модель",
           "предельный размер очереди",
           "пауза очереди целиком"], accent=YELLOW)

    # Все три верхних блока сводятся в одну точку, из неё — к воркерам:
    # так на схеме не появляется пересечений.
    c.line(180, 200, 180, 232, stroke=LINE)
    c.line(570, 200, 570, 232, stroke=LINE)
    c.line(830, 200, 830, 232, stroke=LINE)
    c.line(1300, 200, 1300, 232, stroke=LINE)
    c.line(180, 232, 1300, 232, stroke=LINE)

    for index in range(3):
        x = 300 + index * 320
        c.box(x, 286, 280, 96, f"Воркер {index + 1}",
              ["берёт задание из очереди", "ведёт прогресс по этапам",
               "проверяет отмену и тайм-аут"], accent=VIOLET)
        c.arrow(x + 140, 232, x + 140, 282, stroke=FAINT)

    c.box(300, 440, 340, 132, "Успешное завершение",
          ["сегменты и текст в базу", "форматы на диск",
           "метрики: RTF, уверенность, время этапов",
           "вебхук с подписью HMAC"], accent=GREEN)

    c.box(680, 440, 340, 132, "Ошибка, которую можно повторить",
          ["нехватка видеопамяти → пакет вдвое меньше",
           "сеть, тайм-аут → повтор с задержкой",
           "задержка растёт: 10 с, 20 с, 40 с",
           "разброс, чтобы повторы не совпали"], accent=YELLOW)

    c.box(1060, 440, 340, 132, "Ошибка данных",
          ["повреждённый файл, нет дорожки",
           "неподдерживаемый формат",
           "повтор не выполняется",
           "в карточке — причина и что делать"], accent=RED)

    c.arrow(440, 386, 460, 436, stroke=GREEN)
    c.arrow(760, 386, 830, 436, stroke=YELLOW)
    c.arrow(1080, 386, 1200, 436, stroke=RED)

    # Возврат на повтор идёт по левому полю, не пересекая блоки.
    c.line(850, 574, 850, 600, stroke=YELLOW, width=1.6)
    c.line(850, 600, 120, 600, stroke=YELLOW, width=1.6)
    c.line(120, 600, 120, 232, stroke=YELLOW, width=1.6)
    c.arrow(120, 232, 176, 232, stroke=YELLOW)
    c.text(140, 592, "повтор через 10 / 20 / 40 с", size=11, fill=YELLOW)

    c.text(30, 646, "Состояния задания", size=13, weight=600, fill=INK)
    states = [("queued", "в очереди", FAINT), ("running", "выполняется", BLUE),
              ("retry", "ожидает повтора", YELLOW), ("paused", "приостановлено", FAINT),
              ("completed", "готово", GREEN), ("failed", "ошибка", RED),
              ("cancelled", "отменено", FAINT)]
    x = 30
    for name, label, colour in states:
        width = c.badge(x, 660, f"{name} — {label}", colour=colour)
        x += width + 10

    c.text(30, 720, "После перезапуска сервера задания, застрявшие в состоянии «выполняется», "
                    "автоматически возвращаются в очередь — ни одно задание не теряется.",
           size=11.5, fill=DIM)
    c.text(30, 742, "Отмена действует и на выполняющееся задание: воркер проверяет флаг между "
                    "этапами и корректно освобождает ресурсы.", size=11.5, fill=DIM)
    c.save("diag-03-queue")


# ===========================================================================
# Схема 4. Варианты развёртывания
# ===========================================================================

def diagram_deployment() -> None:
    c = Canvas(1500, 720, "Варианты развёртывания",
               "Три схемы под разные требования к изоляции и доступу извне")

    # Вариант A
    c.group(30, 100, 450, 470, "А. НАТИВНАЯ УСТАНОВКА — самый быстрый старт")
    c.box(50, 140, 410, 88, "Служба операционной системы",
          ["Linux — systemd, macOS — launchd", "Windows — служба или планировщик"], accent=BLUE)
    c.box(50, 244, 410, 88, "Виртуальное окружение Python",
          ["движки ставятся выборочно", "PyTorch под обнаруженный ускоритель"], accent=AQUA)
    c.box(50, 348, 410, 88, "Каталог данных",
          ["/var/lib/asrhub или ~/.local/share", "модели, результаты, база, журналы"],
          accent=FAINT)
    c.box(50, 452, 410, 96, "Порт 8080 напрямую",
          ["подходит для доверенной сети", "обязательна аутентификация по ключу",
           "TLS отсутствует"], accent=YELLOW)

    # Вариант B
    c.group(510, 100, 450, 470, "Б. DOCKER — изоляция и воспроизводимость")
    c.box(530, 140, 410, 88, "Контейнер asrhub",
          ["образ собирается по профилю", "проверка состояния встроена"], accent=VIOLET)
    c.box(530, 244, 410, 88, "Том с данными",
          ["монтируется с хоста", "модели переживают пересборку образа"], accent=FAINT)
    c.box(530, 348, 410, 88, "Профиль gpu",
          ["проброс видеокарты NVIDIA", "отдельный образ с CUDA"], accent=GREEN)
    c.box(530, 452, 410, 96, "Один файл compose",
          ["docker compose up -d", "обновление — пересборка образа",
           "откат — предыдущий тег"], accent=BLUE)

    # Вариант C
    c.group(990, 100, 480, 470, "В. ЗА ОБРАТНЫМ ПРОКСИ — рекомендуется для сети")
    c.box(1010, 140, 440, 96, "nginx",
          ["TLS-сертификат", "client_max_body_size 4G — иначе загрузка",
           "оборвётся с ошибкой 413"], accent=ORANGE)
    c.box(1010, 252, 440, 88, "ASR Hub на 127.0.0.1",
          ["наружу не смотрит", "доступен только через прокси"], accent=BLUE)
    c.box(1010, 356, 440, 88, "Проброс WebSocket",
          ["без него не работает живой прогресс", "Upgrade и Connection в конфигурации"],
          accent=AQUA)
    c.box(1010, 460, 440, 88, "Метрики только изнутри",
          ["/api/metrics закрыт списком сетей"], accent=MAGENTA)

    c.text(30, 610, "Как выбрать", size=13, weight=600, fill=INK)
    rows = [
        ("Один сервер, доверенная сеть, нужен максимум скорости",
         "А — нативная установка: нет накладных расходов контейнера", BLUE),
        ("Несколько сервисов на машине, важна повторяемость",
         "Б — Docker: зависимости движков не конфликтуют с системными", VIOLET),
        ("Доступ из интернета или из другой сети организации",
         "В — обратный прокси с TLS: единственный безопасный вариант", ORANGE),
    ]
    for index, (question, answer, colour) in enumerate(rows):
        y = 630 + index * 30
        c.rect(30, y, 1440, 26, fill=PANEL, stroke=LINE, rx=6, width=1)
        c.rect(30, y, 4, 26, fill=colour, stroke=colour, rx=2)
        c.text(46, y + 17, question, size=11.5, fill=INK)
        c.text(700, y + 17, answer, size=11.5, fill=DIM)
    c.save("diag-04-deployment")


# ===========================================================================
# Схема 5. Выбор модели
# ===========================================================================

def diagram_model_choice() -> None:
    c = Canvas(1500, 800, "Как выбрать модель",
               "Дерево решений; в скобках — измеренный WER на русском по данным авторов моделей")

    c.box(600, 96, 300, 60, "С чего начать", ["Ответьте на четыре вопроса"], accent=VIOLET)

    c.box(30, 210, 330, 78, "Речь только на русском?",
          ["самый частый случай"], accent=BLUE, title_size=13)
    c.box(400, 210, 330, 78, "Нужен ответ в реальном времени?",
          ["голосовой бот, живые субтитры"], accent=ORANGE, title_size=13)
    c.box(770, 210, 330, 78, "Несколько языков в потоке?",
          ["архив разноязычных записей"], accent=GREEN, title_size=13)
    c.box(1140, 210, 330, 78, "Нужны говорящие?",
          ["совещания, интервью"], accent=MAGENTA, title_size=13)

    c.arrow(700, 158, 200, 206, stroke=FAINT)
    c.arrow(730, 158, 560, 206, stroke=FAINT)
    c.arrow(770, 158, 930, 206, stroke=FAINT)
    c.arrow(800, 158, 1300, 206, stroke=FAINT)

    answers = [
        (30, "gigaam-v3-e2e-rnnt", ["лучшее качество на русском", "готовая пунктуация и числа",
                                    "лицензия MIT", "WER 2.4–4.4 % на чистой речи"], BLUE),
        (400, "tone-ru", ["телефония, 8 кГц", "задержка около секунды",
                          "работает на 4 ядрах CPU", "WER 8.6 % на колл-центре"], ORANGE),
        (770, "parakeet-tdt-0.6b-v3", ["25 языков с автоопределением", "RTFx выше 3000",
                                       "пунктуация из коробки", "FLEURS ru — 5.5 %"], GREEN),
        (1140, "moss-transcribe-diarize", ["ASR и диаризация в одной модели",
                                           "50+ языков, Apache-2.0", "до 90 минут за проход",
                                           "или whisperx + pyannote"], MAGENTA),
    ]
    for x, name, items, colour in answers:
        c.box(x, 330, 330, 130, name, items, accent=colour, title_size=13)
        c.arrow(x + 165, 290, x + 165, 326, stroke=colour)

    c.text(30, 512, "Особые случаи", size=13, weight=600, fill=INK)
    special = [
        ("Сервер без видеокарты", "gigaam-v3-ctc в ONNX либо faster-whisper-small с int8", AQUA),
        ("macOS на Apple Silicon", "whisper.cpp с Metal и Core ML: квантованная turbo — 574 МБ",
         AQUA),
        ("Языки СНГ: казахский, киргизский, узбекский", "gigaam-multilingual-large-ctc", YELLOW),
        ("Редкий язык, которого нет нигде", "facebook/omnilingual-asr — 1600+ языков", YELLOW),
        ("Нужен перевод речи на другой язык", "canary-1b-v2: ASR и перевод в одной модели", VIOLET),
        ("Встраивание в приложение без Python", "sherpa-onnx с GigaAM v2 CTC", VIOLET),
        ("Проверка установки до загрузки весов", "demo-simulator — встроенный симулятор", FAINT),
    ]
    for index, (case, answer, colour) in enumerate(special):
        y = 534 + index * 30
        c.rect(30, y, 1440, 26, fill=PANEL, stroke=LINE, rx=6, width=1)
        c.rect(30, y, 4, 26, fill=colour, stroke=colour, rx=2)
        c.text(46, y + 17, case, size=11.5, fill=INK)
        c.text(640, y + 17, answer, size=11.5, fill=DIM, mono=False)

    c.rect(30, 754, 1440, 34, fill="#fff8e6", stroke=YELLOW, rx=6, width=1.2)
    c.text(46, 775, "Важно: значения WER у разных моделей измерены на разных наборах данных "
                    "и потому несопоставимы напрямую. Отберите двух-трёх кандидатов по таблице, "
                    "а окончательный выбор делайте прогоном на своих записях.",
           size=11.5, fill="#7a5c00")
    c.save("diag-05-model-choice")


# ===========================================================================
# Схема 6. Обработка ошибок
# ===========================================================================

def diagram_errors() -> None:
    c = Canvas(1500, 720, "Обработка ошибок",
               "Каждая ошибка классифицируется: от этого зависит, будет ли повтор и что увидит пользователь")

    c.box(30, 100, 320, 88, "Исключение библиотеки",
          ["torch, ctranslate2, ffmpeg…", "текст на английском, без контекста"], accent=FAINT)
    c.arrow(352, 144, 428, 144, stroke=FAINT)
    c.box(430, 100, 340, 88, "Классификатор",
          ["сопоставление по признакам текста", "и типу исключения"], accent=VIOLET)

    branches = [
        ("Нехватка памяти", ["повтор: да", "пакет уменьшается вдвое", "подсказка про batch_size"],
         YELLOW, 30),
        ("Несовместимость cuDNN", ["повтор: нет", "таблица версий CTranslate2",
                                   "готовая команда установки"], ORANGE, 400),
        ("Модель под лицензией", ["повтор: нет", "три шага: принять лицензию,",
                                  "создать токен, прописать его"], MAGENTA, 770),
        ("Сетевая ошибка", ["повтор: да", "нарастающая задержка",
                            "совет перенести модели вручную"], BLUE, 1140),
    ]
    for title, items, colour, x in branches:
        c.box(x, 250, 330, 106, title, items, accent=colour, title_size=13)
        c.arrow(600, 190, x + 165, 246, stroke=colour, curve=18)

    c.box(30, 400, 700, 118, "Что видит пользователь",
          ["машинный код ошибки — для интеграций и метрик",
           "сообщение на русском — что именно произошло",
           "подсказка — конкретная команда или настройка",
           "признак «можно повторить» — им пользуется очередь"], accent=GREEN)

    c.box(770, 400, 700, 118, "Что делает сервер",
          ["решает, ставить ли задание на повтор",
           "уменьшает размер пакета при нехватке памяти",
           "пишет событие в журнал задания и метрику по коду ошибки",
           "показывает сводку по кодам в разделе «Аналитика»"], accent=AQUA)

    c.text(30, 560, "Классы ошибок и поведение", size=13, weight=600, fill=INK)
    table = [
        ("out_of_memory", "повтор с меньшим пакетом", "уменьшите batch_size или возьмите модель легче", YELLOW),
        ("dependency_missing", "без повтора", "команда установки движка приводится в сообщении", ORANGE),
        ("gated_model", "без повтора", "принять лицензию на Hugging Face и задать токен", MAGENTA),
        ("audio_error", "без повтора", "файл повреждён или без звуковой дорожки", RED),
        ("no_speech", "без повтора", "понизьте порог детектора речи до 0.25–0.3", RED),
        ("job_timeout", "повтор", "увеличьте тайм-аут либо возьмите более быструю модель", BLUE),
        ("storage_error", "повтор", "закончилось место на диске или нет прав", BLUE),
        ("rate_limited", "повтор", "превышена частота запросов для ключа доступа", FAINT),
    ]
    for index, (code, behaviour, hint, colour) in enumerate(table):
        y = 582 + index * 30
        c.rect(30, y, 1440, 26, fill=PANEL if index % 2 == 0 else SURFACE,
               stroke=LINE, rx=6, width=1)
        c.rect(30, y, 4, 26, fill=colour, stroke=colour, rx=2)
        c.text(46, y + 17, code, size=11.5, fill=INK, mono=True)
        c.text(330, y + 17, behaviour, size=11.5, fill=colour)
        c.text(620, y + 17, hint, size=11.5, fill=DIM)
    c.save("diag-06-errors")



# ===========================================================================
# Схема 7. Мониторинг
# ===========================================================================

def diagram_monitoring() -> None:
    c = Canvas(1560, 960, "Мониторинг ASR Hub",
               "Один сбор — семь форматов; забирают метрики опросом либо сервер шлёт их сам")

    # Источники
    c.group(30, 100, 330, 420, "ИСТОЧНИКИ ДАННЫХ")
    sources = [
        ("Очередь и задания", ["глубина, ожидание, статусы", "разрезы по моделям"], BLUE),
        ("Аналитика", ["RTF и перцентили", "время по стадиям, WER"], AQUA),
        ("Оборудование", ["процессор, память", "видеокарта, диск"], GREEN),
        ("Счётчики в памяти", ["запросы к API", "ошибки, повторы"], VIOLET),
    ]
    for index, (title, items, colour) in enumerate(sources):
        c.box(50, 140 + index * 92, 290, 76, title, items, accent=colour)

    # Сборщик
    c.box(400, 190, 300, 240, "Сборщик",
          ["каждый источник опрашивается",
           "отдельно: сбой одного не лишает",
           "остальных метрик",
           "",
           "снимок кешируется на 5 секунд",
           "дорогие замеры — раз в 5 минут",
           "",
           "63 метрики, 11 групп"], accent=ORANGE)
    for index in range(4):
        c.arrow(340, 178 + index * 92, 400, 290, stroke=FAINT, width=1.2)

    # Каталог метрик
    c.box(400, 470, 300, 130, "Каталог метрик",
          ["описание, единица, порог,",
           "рекомендация, что делать",
           "— один источник для API,",
           "документации и правил"], accent=MAGENTA)
    c.arrow(550, 470, 550, 434, stroke=MAGENTA, width=1.4, dash="4 3")

    # Форматы
    c.group(740, 100, 380, 300, "ФОРМАТЫ ВЫГРУЗКИ")
    formats = [
        "Prometheus · OpenMetrics",
        "JSON с описаниями метрик",
        "OTLP — OpenTelemetry",
        "InfluxDB line protocol",
        "Graphite · StatsD",
        "Zabbix sender · CSV",
    ]
    for index, name in enumerate(formats):
        y = 140 + index * 40
        c.rect(760, y, 340, 32, fill=SURFACE, stroke=LINE, rx=6, width=1)
        c.rect(760, y, 3, 32, fill=BLUE, stroke=BLUE, rx=2)
        c.text(776, y + 21, name, size=12, fill=INK)
    c.arrow(700, 300, 740, 250, stroke=ORANGE, width=1.6)

    # Способы доставки
    c.group(740, 430, 380, 170, "ДВА СПОСОБА ДОСТАВКИ")
    c.box(760, 466, 340, 56, "Опрос: система сбора приходит сама",
          ["надёжнее — молчание сервера заметно"], accent=GREEN)
    c.box(760, 532, 340, 56, "Отправка: сервер шлёт сам",
          ["для закрытого контура и NAT"], accent=YELLOW)

    # Потребители
    c.group(1160, 100, 370, 500, "ПОТРЕБИТЕЛИ")
    consumers = [
        ("Prometheus + Grafana", ["готовые правила и панель", "отдаются самим сервером"], BLUE),
        ("Zabbix", ["шаблон с 58 элементами"], ORANGE),
        ("OpenTelemetry Collector", ["общая телеметрия"], VIOLET),
        ("Kubernetes", ["liveness, readiness, startup"], AQUA),
        ("Своя система", ["JSON или webhook"], FAINT),
    ]
    for index, (title, items, colour) in enumerate(consumers):
        c.box(1180, 140 + index * 92, 330, 76, title, items, accent=colour)
    c.arrow(1120, 250, 1160, 300, stroke=FAINT, width=1.4)
    c.arrow(1120, 520, 1160, 430, stroke=FAINT, width=1.4)

    # Тревоги
    c.text(30, 648, "Тревоги считаются и внутри сервера — на случай, когда внешнего "
                    "мониторинга нет", size=12, fill=DIM)
    states = [("норма", GREEN), ("наблюдение", YELLOW), ("тревога", RED), ("снята", GREEN)]
    x = 30
    for index, (label, colour) in enumerate(states):
        c.rect(x, 668, 150, 34, fill=PANEL, stroke=colour, rx=6, width=1.4)
        c.text(x + 75, 690, label, size=12, fill=INK, anchor="middle", weight=600)
        if index < len(states) - 1:
            c.arrow(x + 150, 685, x + 190, 685, stroke=FAINT, width=1.2)
        x += 190
    c.text(30, 726, "Промежуточное состояние «наблюдение» существует, чтобы одиночный "
                    "всплеск не будил дежурного:", size=11.5, fill=DIM)
    c.text(30, 744, "тревога поднимается, только если условие держится дольше выдержки.",
           size=11.5, fill=DIM)

    c.text(30, 790, "Что помнить", size=13, weight=600, fill=INK)
    rows = [
        ("Метрики закрывают на прокси, а не ключом",
         "Prometheus не умеет обновлять истекающие ключи — ограничьте путь по адресу сети", BLUE),
        ("Сбор чаще 15 секунд бессмыслен",
         "Замеры железа обновляются раз в 20 секунд служебным циклом сервера", AQUA),
        ("Пороги из каталога — отправная точка",
         "Очередь из ста заданий бывает и нормой, и аварией: зависит от вашего потока", YELLOW),
        ("Молчащий приёмник опаснее тревоги",
         "asrhub_push_targets_healthy показывает, что вы перестали видеть проблемы", ORANGE),
    ]
    for index, (question, answer, colour) in enumerate(rows):
        y = 810 + index * 32
        c.rect(30, y, 1500, 28, fill=PANEL, stroke=LINE, rx=6, width=1)
        c.rect(30, y, 4, 28, fill=colour, stroke=colour, rx=2)
        c.text(46, y + 18, question, size=11.5, fill=INK)
        c.text(560, y + 18, answer, size=11.5, fill=DIM)
    c.save("diag-07-monitoring")


def main() -> int:
    print("Сборка схем для документации:")
    diagram_architecture()
    diagram_pipeline()
    diagram_queue()
    diagram_deployment()
    diagram_model_choice()
    diagram_errors()
    diagram_monitoring()
    print(f"Готово: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
