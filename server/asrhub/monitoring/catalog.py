"""Каталог метрик мониторинга.

Единственный источник истины о том, что сервис отдаёт наружу. Отсюда берут
данные сборщик метрик, программный интерфейс `/api/monitoring/catalog`,
готовые правила Prometheus, шаблон Zabbix и раздел документации о
мониторинге — поэтому описание метрики не может разойтись с самой метрикой.

Для каждой метрики хранится не только имя и тип, но и то, чего обычно не
хватает в экспортёрах: что она означает, какое значение считать нормальным,
при каком пороге поднимать тревогу и что делать, когда она сработала.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

MetricType = Literal["gauge", "counter", "histogram", "info"]
Severity = Literal["info", "warning", "critical"]


@dataclass(frozen=True)
class Threshold:
    """Порог для тревоги.

    `warning` и `critical` — значения, за которыми метрика считается
    подозрительной и опасной. `direction` говорит, в какую сторону плохо:
    «above» — беда при росте (глубина очереди), «below» — при падении
    (свободное место на диске).
    """

    direction: Literal["above", "below"]
    warning: float | None = None
    critical: float | None = None
    for_seconds: int = 300
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "warning": self.warning,
                "critical": self.critical, "for_seconds": self.for_seconds,
                "note": self.note}


@dataclass(frozen=True)
class MetricSpec:
    """Описание одной метрики."""

    name: str
    type: MetricType
    group: str
    label: str
    description: str
    unit: str = ""
    labels: tuple[str, ...] = ()
    recommendation: str = ""
    normal: str = ""
    threshold: Threshold | None = None
    troubleshooting: str = ""
    since_restart: bool = False
    expensive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "type": self.type, "group": self.group,
            "label": self.label, "description": self.description, "unit": self.unit,
            "labels": list(self.labels), "recommendation": self.recommendation,
            "normal": self.normal,
            "threshold": self.threshold.to_dict() if self.threshold else None,
            "troubleshooting": self.troubleshooting,
            "since_restart": self.since_restart,
        }


GROUPS: list[dict[str, str]] = [
    {"id": "service", "title": "Служба",
     "description": "Жив ли сервис, сколько работает, какая версия и настройки."},
    {"id": "queue", "title": "Очередь",
     "description": "Сколько заданий ждёт, сколько выполняется, как долго ждут."},
    {"id": "jobs", "title": "Задания",
     "description": "Сколько заданий прошло, чем закончились, в каких разрезах."},
    {"id": "performance", "title": "Производительность",
     "description": "Скорость обработки: RTF, время по стадиям, пропускная способность."},
    {"id": "quality", "title": "Качество",
     "description": "Уверенность модели, WER и CER, доля записей без речи."},
    {"id": "models", "title": "Модели и движки",
     "description": "Что загружено в память, сколько занимает, что доступно."},
    {"id": "resources", "title": "Оборудование",
     "description": "Процессор, память, видеокарта, диск."},
    {"id": "storage", "title": "Хранилище",
     "description": "База заданий, загрузки, результаты, веса моделей."},
    {"id": "api", "title": "Программный интерфейс",
     "description": "Запросы, задержки, отказы, соединения WebSocket."},
    {"id": "errors", "title": "Ошибки",
     "description": "Коды ошибок, повторы, доля восстановимых."},
    {"id": "webhooks", "title": "Уведомления",
     "description": "Доставка результатов на внешние адреса."},
]

GROUPS_BY_ID = {g["id"]: g for g in GROUPS}

METRICS: list[MetricSpec] = []


def _m(spec: MetricSpec) -> MetricSpec:
    METRICS.append(spec)
    return spec


# ---------------------------------------------------------------------------
# Служба
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_up", type="gauge", group="service", label="Сервис отвечает",
    description=(
        "Единица, если сервис отвечает на запрос метрик. Нуля здесь не бывает: "
        "если сервис лежит, ответа не будет вовсе — и это как раз нужный признак."
    ),
    recommendation=(
        "Основа любого оповещения о недоступности. Правило пишется через absent(): "
        "absent(asrhub_up) == 1 за пять минут означает, что сервис не отвечает. "
        "Проверять сам факт равенства единице бессмысленно."
    ),
    normal="1",
    threshold=Threshold("below", warning=1, critical=1, for_seconds=300,
                        note="Отслеживается через absent(), а не сравнением значения"),
    troubleshooting="scripts/service.sh status, затем scripts/service.sh logs -n 200",
))

_m(MetricSpec(
    name="asrhub_build_info", type="info", group="service", label="Версия сборки",
    description=(
        "Постоянная метрика со значением 1 и метками: версия сервиса, версия схемы "
        "базы, версия Python, дата каталога моделей. Так принято передавать в "
        "Prometheus то, что не является числом."
    ),
    labels=("version", "schema_version", "python", "catalog_date", "platform"),
    recommendation=(
        "Пригодится, чтобы после обновления убедиться, что все экземпляры "
        "действительно перешли на новую версию: count by (version) (asrhub_build_info)."
    ),
))

_m(MetricSpec(
    name="asrhub_uptime_seconds", type="gauge", group="service",
    label="Время работы с последнего запуска", unit="с",
    description="Сколько секунд прошло с момента запуска процесса.",
    recommendation=(
        "Само по себе неинтересно; ценность в производной. Регулярное обнуление "
        "означает, что служба перезапускается — смотрите changes(asrhub_uptime_seconds[1h]) "
        "и журнал на предмет нехватки памяти."
    ),
    normal="растёт непрерывно",
    threshold=Threshold("below", warning=600, for_seconds=0,
                        note="Значение меньше десяти минут после того, как было больше, "
                             "означает недавний перезапуск"),
    troubleshooting="Частые перезапуски — почти всегда OOM-killer. dmesg | grep -i oom",
))

_m(MetricSpec(
    name="asrhub_queue_paused", type="gauge", group="service", label="Очередь на паузе",
    description="Единица, если приём заданий в работу приостановлен вручную.",
    recommendation=(
        "Оповещение стоит сделать обязательно: очередь, забытая на паузе после "
        "обслуживания, выглядит как «сервис работает, но ничего не делает» — "
        "и это самая обидная из простых аварий."
    ),
    normal="0",
    threshold=Threshold("above", warning=1, for_seconds=1800,
                        note="Полчаса на паузе — уже подозрительно"),
    troubleshooting="POST /api/queue/resume",
))

_m(MetricSpec(
    name="asrhub_config_reloads_total", type="counter", group="service",
    label="Изменений настроек", since_restart=True,
    description="Сколько раз настройки менялись через программный интерфейс.",
    recommendation=(
        "Всплеск изменений в момент, когда никто ничего не настраивал, — повод "
        "посмотреть, кто ходит в API с ключом администратора."
    ),
))

# ---------------------------------------------------------------------------
# Очередь
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_queue_depth", type="gauge", group="queue", label="Заданий ждёт",
    description=(
        "Сколько заданий стоит в очереди и ждёт свободного воркера. Считаются "
        "состояния «в очереди» и «ожидает повтора»."
    ),
    recommendation=(
        "Главный показатель того, справляется ли сервер. Смотреть надо не на "
        "значение, а на тенденцию: очередь из ста заданий, которая тает, — это "
        "нормальный ночной прогон; очередь из двадцати, которая растёт третий час, "
        "— это нехватка мощности."
    ),
    normal="колеблется около нуля в рабочем режиме",
    threshold=Threshold("above", warning=50, critical=200, for_seconds=900,
                        note="Пороги подбирайте под свой поток: значимо не число, а рост"),
    troubleshooting=(
        "Поднять max_concurrent_jobs (если хватает памяти), перевести массовые задания "
        "на низкий приоритет, включить scheduling_policy: shortest_first, взять модель полегче"
    ),
))

_m(MetricSpec(
    name="asrhub_jobs_by_status", type="gauge", group="queue", label="Заданий по состояниям",
    labels=("status",),
    description=(
        "Число заданий в каждом состоянии: queued, running, paused, retry, "
        "completed, failed, cancelled."
    ),
    recommendation=(
        "Растущее число в состоянии retry говорит, что задания падают на "
        "восстановимых ошибках и повторяются по кругу — чаще всего это нехватка "
        "памяти на видеокарте. Разбирайтесь по коду ошибки, а не по числу повторов."
    ),
))

_m(MetricSpec(
    name="asrhub_active_jobs", type="gauge", group="queue", label="Выполняется сейчас",
    description="Сколько заданий обрабатывается прямо сейчас.",
    recommendation=(
        "Сравнивайте с asrhub_workers. Устойчивое равенство при непустой очереди "
        "означает, что сервер загружен полностью и очередь ограничена мощностью, "
        "а не расписанием."
    ),
))

_m(MetricSpec(
    name="asrhub_workers", type="gauge", group="queue", label="Воркеров",
    description="Сколько заданий сервер готов обрабатывать одновременно.",
    recommendation=(
        "Меняется на ходу через POST /api/queue/concurrency. Если значение "
        "изменилось само — значит, кто-то правил настройки."
    ),
))

_m(MetricSpec(
    name="asrhub_queue_oldest_seconds", type="gauge", group="queue",
    label="Возраст самого старого задания", unit="с",
    description=(
        "Сколько секунд самое давнее ожидающее задание провело в очереди. "
        "Ноль, если очередь пуста."
    ),
    recommendation=(
        "Честнее глубины очереди: показывает не «сколько», а «как долго ждут». "
        "Именно эту величину чувствует человек, отправивший файл. Порог ставьте "
        "от обещанного пользователям времени ответа."
    ),
    threshold=Threshold("above", warning=1800, critical=7200, for_seconds=300),
    troubleshooting="Проверьте политику планирования: при priority_fifo длинная "
                    "запись может ждать за многочасовым архивом",
))

_m(MetricSpec(
    name="asrhub_queue_wait_seconds", type="gauge", group="queue",
    label="Ожидание в очереди", unit="с", labels=("quantile",),
    description=(
        "Время ожидания завершённых заданий: средние и перцентили p50, p90, p95, p99 "
        "за последние сутки."
    ),
    recommendation=(
        "Разрыв между p50 и p95 важнее обоих чисел по отдельности. Если медиана "
        "секунды, а p95 — часы, значит, часть заданий систематически проигрывает "
        "борьбу за воркер; посмотрите на приоритеты и политику планирования."
    ),
    threshold=Threshold("above", warning=900, critical=3600, for_seconds=900,
                        note="Порог применяйте к квантили 0.95"),
))

_m(MetricSpec(
    name="asrhub_queue_pending_audio_seconds", type="gauge", group="queue",
    label="Аудио в ожидании", unit="с",
    description=(
        "Суммарная длительность записей, стоящих в очереди. Не путать с числом "
        "заданий: десять пятиминутных планёрок и одна восьмичасовая конференция "
        "дают одинаковую глубину очереди и совершенно разную нагрузку."
    ),
    recommendation=(
        "Планировать мощность надо по этой величине, а не по глубине очереди. "
        "Она же честно отвечает на вопрос «когда всё разгребётся»."
    ),
))

_m(MetricSpec(
    name="asrhub_queue_eta_seconds", type="gauge", group="queue",
    label="Оценка времени разбора очереди", unit="с",
    description=(
        "Сколько примерно осталось до опустошения очереди. Считается из "
        "ожидающего аудио, среднего RTF по моделям и числа воркеров."
    ),
    recommendation=(
        "Оценка грубая: она не знает, какой моделью пойдут задания и не упрётся "
        "ли сервер в память. Годится как ориентир для дежурного, а не как обещание "
        "пользователю."
    ),
    threshold=Threshold("above", warning=7200, critical=28800, for_seconds=1800),
))

_m(MetricSpec(
    name="asrhub_queue_capacity", type="gauge", group="queue",
    label="Предел длины очереди",
    description="Значение max_queue_size: сверх него задания не принимаются.",
    recommendation=(
        "Оповещение по отношению asrhub_queue_depth / asrhub_queue_capacity > 0.8 "
        "предупредит до того, как клиенты начнут получать отказы."
    ),
))

_m(MetricSpec(
    name="asrhub_scheduling_policy_info", type="info", group="queue",
    label="Политика планирования", labels=("policy",),
    description="Действующая политика выбора следующего задания.",
    recommendation="Полезно видеть на панели рядом с очередью: неожиданное "
                   "изменение политики объясняет странное распределение ожиданий.",
))

# ---------------------------------------------------------------------------
# Задания
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_jobs_total", type="counter", group="jobs", label="Заданий обработано",
    labels=("status",), since_restart=True,
    description=(
        "Накопительный счётчик заданий по итоговому состоянию с момента запуска "
        "сервиса. В отличие от asrhub_jobs_by_status, значение только растёт — "
        "по нему считают частоту через rate()."
    ),
    recommendation=(
        "Доля отказов: rate(asrhub_jobs_total{status=\"failed\"}[30m]) / "
        "rate(asrhub_jobs_total[30m]). Оповещение по доле, а не по числу — иначе "
        "ночной прогон архива поднимет тревогу на ровном месте."
    ),
    threshold=Threshold("above", warning=0.05, critical=0.20, for_seconds=1800,
                        note="Порог для доли отказов, не для самого счётчика"),
))

_m(MetricSpec(
    name="asrhub_jobs_by_model", type="gauge", group="jobs", label="Заданий по моделям",
    labels=("model", "engine"),
    description="Сколько заданий обработано каждой моделью за последние сутки.",
    recommendation=(
        "Показывает, какие модели действительно используются. Модель без заданий "
        "неделю — кандидат на удаление весов: место освободится сразу."
    ),
))

_m(MetricSpec(
    name="asrhub_jobs_by_source", type="gauge", group="jobs", label="Заданий по источникам",
    labels=("source",),
    description="Разрез по способу отправки: web, api, cli, web-batch.",
    recommendation="Резкий рост доли api при неизменной работе людей — обычно "
                   "признак того, что интеграция зациклилась на повторах.",
))

_m(MetricSpec(
    name="asrhub_audio_seconds_total", type="counter", group="jobs",
    label="Обработано аудио", unit="с", since_restart=True,
    description="Суммарная длительность успешно распознанных записей.",
    recommendation=(
        "Основная единица нагрузки: заданий может быть мало, а часов аудио много. "
        "Планируйте мощность по rate(asrhub_audio_seconds_total[1h]), а не по числу заданий."
    ),
))

_m(MetricSpec(
    name="asrhub_words_total", type="counter", group="jobs", label="Распознано слов",
    since_restart=True,
    description="Сколько слов выдал распознаватель суммарно.",
    recommendation="Резкое падение слов на час аудио при неизменном потоке — "
                   "признак того, что модель перестала справляться со звуком.",
))

_m(MetricSpec(
    name="asrhub_cached_jobs_total", type="counter", group="jobs",
    label="Отдано из кеша", since_restart=True,
    description=(
        "Сколько заданий вернулись из кеша дедупликации, без повторного распознавания."
    ),
    recommendation=(
        "Высокая доля — это хорошо: столько вычислений сэкономлено. Но доля выше "
        "половины при работе людей означает, что клиент шлёт одно и то же и не "
        "забирает результат."
    ),
))

_m(MetricSpec(
    name="asrhub_job_duration_seconds", type="histogram", group="jobs",
    label="Длительность обработки", unit="с",
    description="Гистограмма времени обработки одного задания.",
    recommendation=(
        "По гистограмме считается любая квантиль на стороне Prometheus, и она "
        "переживает перезапуск сервиса, в отличие от посчитанных перцентилей."
    ),
))

_m(MetricSpec(
    name="asrhub_media_duration_seconds", type="histogram", group="jobs",
    label="Длительность записей", unit="с",
    description="Гистограмма длительности входных файлов.",
    recommendation=(
        "Полезна при подборе scheduling_policy: если распределение двугорбое — "
        "много коротких и немного очень длинных, — shortest_first резко снизит "
        "среднее ожидание."
    ),
))

# ---------------------------------------------------------------------------
# Производительность
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_rtf", type="gauge", group="performance",
    label="Коэффициент реального времени", labels=("quantile",),
    description=(
        "Отношение времени обработки к длительности записи. RTF 0,1 значит, что "
        "часовая запись обрабатывается за шесть минут. Меньше — лучше. "
        "Отдаются среднее и перцентили p50, p90, p95, p99."
    ),
    recommendation=(
        "Ключевая метрика скорости. Запомните своё обычное значение сразу после "
        "установки: рост RTF при неизменной модели и нагрузке означает, что что-то "
        "изменилось в железе — троттлинг видеокарты, вытеснение памяти, сосед по машине."
    ),
    normal="0,03–0,15 на видеокарте, 0,8–1,5 на процессоре",
    threshold=Threshold("above", warning=0.5, critical=1.0, for_seconds=1800,
                        note="Порог по квантили 0.95; для процессорной установки поднимите вдвое"),
    troubleshooting=(
        "Проверьте, что задействован ускоритель: GET /api/system → device. "
        "Задание, случайно посчитанное на процессоре, идёт в 10–30 раз дольше"
    ),
))

_m(MetricSpec(
    name="asrhub_rtf_by_model", type="gauge", group="performance",
    label="RTF по моделям", labels=("model",),
    description="Средний RTF каждой модели за сутки.",
    recommendation=(
        "Единственный честный способ сравнить скорость моделей — на своём железе "
        "и своих записях. Цифры RTFx из карточек моделей измерены в других условиях "
        "и с вашими не совпадут."
    ),
))

_m(MetricSpec(
    name="asrhub_stage_seconds", type="gauge", group="performance",
    label="Время по стадиям", unit="с", labels=("stage",),
    description=(
        "Среднее время каждой стадии конвейера: audio_prep, model_load, vad, "
        "inference, diarization, postprocess."
    ),
    recommendation=(
        "Разбивка отвечает на вопрос «что именно тормозит». Большая доля model_load "
        "означает, что модель выгружается между заданиями — увеличьте model_cache_size. "
        "Большая доля diarization — выключите её, если говорящие не нужны."
    ),
    troubleshooting="GET /api/analytics/efficiency покажет долю загрузки моделей за период",
))

_m(MetricSpec(
    name="asrhub_model_load_share", type="gauge", group="performance",
    label="Доля времени на загрузку моделей",
    description=(
        "Какая часть машинного времени уходит не на распознавание, а на загрузку "
        "весов в память."
    ),
    recommendation=(
        "Выше 0,2 — модель выгружается слишком рано. Поднимите model_cache_size "
        "и model_idle_unload_s: на потоке коротких файлов это даёт кратное ускорение."
    ),
    normal="менее 0,1",
    threshold=Threshold("above", warning=0.2, critical=0.4, for_seconds=3600),
))

_m(MetricSpec(
    name="asrhub_throughput_audio_hours", type="gauge", group="performance",
    label="Пропускная способность", unit="ч/ч",
    description="Сколько часов аудио сервер обрабатывает за час календарного времени.",
    recommendation=(
        "Прямо отвечает на вопрос «хватит ли мощности». Если суточный поток — "
        "200 часов записей, а пропускная способность 6 ч/ч, то за сутки сервер "
        "осилит 144 часа и очередь будет расти каждый день."
    ),
))

# ---------------------------------------------------------------------------
# Качество
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_confidence", type="gauge", group="quality",
    label="Уверенность модели", labels=("quantile",),
    description=(
        "Оценка, которую модель даёт собственному результату: среднее и перцентили "
        "за сутки."
    ),
    recommendation=(
        "Абсолютное значение между разными моделями несравнимо — у каждой своя шкала. "
        "Следите за изменением у одной модели: падение при неизменных настройках "
        "означает, что изменилось качество входного звука."
    ),
    normal="0,85–0,95 на разборчивой речи",
    threshold=Threshold("below", warning=0.75, critical=0.6, for_seconds=3600),
    troubleshooting="Включите audio_normalize и audio_highpass_hz; проверьте, "
                    "не сменился ли источник записей",
))

_m(MetricSpec(
    name="asrhub_low_confidence_share", type="gauge", group="quality",
    label="Доля неуверенных сегментов",
    description="Какая часть сегментов имеет уверенность ниже 0,7.",
    recommendation=(
        "Практичнее среднего: показывает, сколько текста придётся вычитывать вручную. "
        "Рост доли — первый признак, что во входном потоке появились плохие записи."
    ),
    threshold=Threshold("above", warning=0.2, critical=0.4, for_seconds=3600),
))

_m(MetricSpec(
    name="asrhub_wer", type="gauge", group="quality", label="WER", labels=("model",),
    description=(
        "Доля неверно распознанных слов. Считается только для заданий, которым "
        "задан эталонный текст."
    ),
    recommendation=(
        "Единственная объективная мера качества. Заведите десяток эталонных записей "
        "и прогоняйте их по расписанию — так деградация после смены модели или "
        "обновления библиотек видна сразу, а не через месяц по жалобам."
    ),
    normal="0,03–0,08 на чистой русской речи",
    threshold=Threshold("above", warning=0.15, critical=0.30, for_seconds=3600),
))

_m(MetricSpec(
    name="asrhub_no_speech_total", type="counter", group="quality",
    label="Записей без речи", since_restart=True,
    description="Сколько заданий завершилось с признаком «речь не найдена».",
    recommendation=(
        "Единичные срабатывания нормальны. Всплеск означает, что источник начал "
        "присылать тишину: сломался микрофон, отвалилась дорожка при перекодировании "
        "или в файлах видео без звука."
    ),
    threshold=Threshold("above", warning=5, critical=20, for_seconds=1800,
                        note="Порог для прироста за полчаса"),
))

# ---------------------------------------------------------------------------
# Модели и движки
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_models_loaded", type="gauge", group="models",
    label="Моделей в памяти",
    description="Сколько моделей сейчас держится загруженными в реестре движков.",
    recommendation=(
        "Сравнивайте с model_cache_size. Постоянное упирание в предел при разнородном "
        "потоке означает, что модели вытесняют друг друга, и время уходит на перезагрузку."
    ),
))

_m(MetricSpec(
    name="asrhub_model_loads_total", type="counter", group="models",
    label="Загрузок моделей", labels=("model",), since_restart=True,
    description="Сколько раз веса модели загружались в память.",
    recommendation=(
        "Число загрузок, близкое к числу заданий, — прямое доказательство, что кеш "
        "не работает. Каждая загрузка стоит от 5 до 40 секунд."
    ),
))

_m(MetricSpec(
    name="asrhub_model_success_rate", type="gauge", group="models",
    label="Доля успеха по моделям", labels=("model",),
    description="Какая часть заданий этой модели завершилась успешно за сутки.",
    recommendation=(
        "Позволяет отличить общую поломку от проблемы одной модели. Если у всех "
        "моделей доля упала — дело в сервере; если у одной — в её весах или зависимостях."
    ),
    threshold=Threshold("below", warning=0.9, critical=0.7, for_seconds=3600),
))

_m(MetricSpec(
    name="asrhub_engines_available", type="gauge", group="models",
    label="Движков доступно",
    description="Сколько движков распознавания установлено и готово к работе.",
    recommendation=(
        "Падение после обновления означает, что обновление сломало зависимости. "
        "GET /api/engines покажет по каждому движку причину недоступности."
    ),
    threshold=Threshold("below", warning=1, critical=1, for_seconds=60,
                        note="Ноль доступных движков — сервис не может работать"),
    troubleshooting="bash scripts/models.sh engines",
))

_m(MetricSpec(
    name="asrhub_engine_available", type="gauge", group="models",
    label="Доступность движка", labels=("engine",),
    description="Единица, если движок готов к работе; ноль, если нет.",
    recommendation="Заведите оповещение на те движки, которые вам действительно "
                   "нужны: недоступность остальных ни на что не влияет.",
))

# ---------------------------------------------------------------------------
# Оборудование
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_cpu_percent", type="gauge", group="resources",
    label="Загрузка процессора", unit="%",
    description="Средняя загрузка процессора машины на момент последнего замера.",
    recommendation=(
        "На установке с видеокартой высокая загрузка процессора при низкой загрузке "
        "GPU — признак того, что узкое место в подготовке звука, а не в распознавании. "
        "Проверьте, не включено ли шумоподавление без надобности."
    ),
    threshold=Threshold("above", warning=85, critical=95, for_seconds=1800),
))

_m(MetricSpec(
    name="asrhub_ram_used_mb", type="gauge", group="resources",
    label="Занято оперативной памяти", unit="МБ",
    description="Сколько оперативной памяти занято на машине.",
    recommendation=(
        "Следите за отношением к asrhub_ram_total_mb. Приближение к пределу на "
        "машине с моделями заканчивается тем, что процесс убивает OOM-killer, и "
        "выглядит это как необъяснимый перезапуск службы."
    ),
    threshold=Threshold("above", warning=85, critical=95, for_seconds=600,
                        note="Порог для процента от общего объёма"),
    troubleshooting="Уменьшите max_concurrent_jobs и model_cache_size",
))

_m(MetricSpec(
    name="asrhub_ram_total_mb", type="gauge", group="resources",
    label="Всего оперативной памяти", unit="МБ",
    description="Полный объём оперативной памяти машины.",
))

_m(MetricSpec(
    name="asrhub_gpu_percent", type="gauge", group="resources",
    label="Загрузка видеокарты", unit="%", labels=("gpu",),
    description="Загрузка вычислительных блоков видеокарты.",
    recommendation=(
        "Устойчивые 95–100 % при непустой очереди — это норма и признак, что железо "
        "используется полностью. Тревожно обратное: очередь есть, а загрузка низкая — "
        "значит, время уходит не на вычисления."
    ),
    normal="80–100 % под нагрузкой",
))

_m(MetricSpec(
    name="asrhub_gpu_memory_mb", type="gauge", group="resources",
    label="Занято видеопамяти", unit="МБ", labels=("gpu",),
    description="Сколько видеопамяти занято на устройстве.",
    recommendation=(
        "Приближение к asrhub_gpu_memory_total_mb — прямой путь к ошибкам "
        "out_of_memory. Порог 90 % даёт время среагировать: снизить batch_size "
        "или число воркеров до того, как задания начнут падать."
    ),
    threshold=Threshold("above", warning=90, critical=97, for_seconds=600,
                        note="Порог для процента от общего объёма видеопамяти"),
    troubleshooting="Уменьшите batch_size, смените compute_type на int8_float16, "
                    "снизьте max_concurrent_jobs до 1",
))

_m(MetricSpec(
    name="asrhub_gpu_memory_total_mb", type="gauge", group="resources",
    label="Всего видеопамяти", unit="МБ", labels=("gpu",),
    description="Полный объём памяти видеокарты.",
))

_m(MetricSpec(
    name="asrhub_gpu_temperature_celsius", type="gauge", group="resources",
    label="Температура видеокарты", unit="°C", labels=("gpu",),
    description="Температура графического процессора.",
    recommendation=(
        "Выше 83 °C большинство карт снижает частоты, и RTF растёт без всякой "
        "причины со стороны программы. Если видите одновременный рост температуры "
        "и RTF — дело в охлаждении, а не в настройках."
    ),
    normal="60–80 °C под нагрузкой",
    threshold=Threshold("above", warning=83, critical=90, for_seconds=600),
))

_m(MetricSpec(
    name="asrhub_gpu_power_watts", type="gauge", group="resources",
    label="Потребление видеокарты", unit="Вт", labels=("gpu",),
    description="Текущее энергопотребление видеокарты.",
    recommendation="Косвенный признак реальной работы: карта, потребляющая "
                   "холостой минимум при непустой очереди, ничего не считает.",
))

_m(MetricSpec(
    name="asrhub_process_threads", type="gauge", group="resources",
    label="Потоков процесса",
    description="Сколько потоков в процессе сервера.",
    recommendation="Непрерывный рост означает утечку потоков; нормальное значение "
                   "стабильно и близко к числу воркеров плюс десяток служебных.",
))

_m(MetricSpec(
    name="asrhub_process_memory_mb", type="gauge", group="resources",
    label="Память процесса", unit="МБ",
    description="Сколько оперативной памяти занимает сам процесс сервера.",
    recommendation=(
        "В отличие от общей памяти машины показывает именно ASR Hub. Пилообразный "
        "рост с падениями при выгрузке моделей — норма; монотонный рост без падений "
        "— утечка."
    ),
))

# ---------------------------------------------------------------------------
# Хранилище
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_disk_free_gb", type="gauge", group="storage",
    label="Свободно на диске", unit="ГБ",
    description="Свободное место на разделе с каталогом данных.",
    recommendation=(
        "Самая недооценённая метрика. Заполненный диск ночью оставляет наполовину "
        "записанные результаты и повреждает базу. Порог ставьте выше disk_min_free_gb, "
        "чтобы успеть среагировать до того, как сервер начнёт отказывать в приёме."
    ),
    threshold=Threshold("below", warning=20, critical=5, for_seconds=300),
    troubleshooting="POST /api/maintenance/cleanup, затем bash scripts/models.sh disk",
))

_m(MetricSpec(
    name="asrhub_disk_used_percent", type="gauge", group="storage",
    label="Занято на диске", unit="%",
    description="Доля занятого места на разделе с каталогом данных.",
    threshold=Threshold("above", warning=85, critical=95, for_seconds=300),
))

_m(MetricSpec(
    name="asrhub_storage_bytes", type="gauge", group="storage",
    label="Размер каталогов", unit="Б", labels=("kind",), expensive=True,
    description=(
        "Сколько занимают загрузки, результаты, веса моделей и журналы. "
        "Замер по каталогам делается не чаще раза в пять минут: обход каталога "
        "моделей на десятки гигабайт стоит дорого."
    ),
    recommendation=(
        "Растущие uploads означают, что delete_source_after выключен, а исходники "
        "накапливаются. Для большинства установок их правильно удалять сразу после "
        "обработки."
    ),
))

_m(MetricSpec(
    name="asrhub_database_size_mb", type="gauge", group="storage",
    label="Размер базы", unit="МБ",
    description="Размер файла базы заданий.",
    recommendation=(
        "Растёт линейно с числом заданий. Несколько сотен мегабайт — норма; "
        "гигабайты означают, что result_retention_days слишком велик или равен нулю."
    ),
    threshold=Threshold("above", warning=2048, critical=8192, for_seconds=3600),
))

_m(MetricSpec(
    name="asrhub_database_rows", type="gauge", group="storage",
    label="Строк в таблицах", labels=("table",),
    description="Число записей в таблицах базы: jobs, segments, events, metrics.",
    recommendation="Таблица segments растёт быстрее прочих: у часовой записи "
                   "несколько сотен сегментов. Она же первой выигрывает от очистки.",
))

# ---------------------------------------------------------------------------
# Программный интерфейс
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_http_requests_total", type="counter", group="api",
    label="Запросов", labels=("method", "route", "status"), since_restart=True,
    description="Счётчик запросов с разбивкой по методу, маршруту и коду ответа.",
    recommendation=(
        "Маршрут берётся из шаблона (/api/jobs/{job_id}), а не из конкретного "
        "адреса — иначе метки размножились бы по числу заданий и уронили Prometheus. "
        "Доля пятисотых: rate(...{status=~\"5..\"}[5m]) / rate(...[5m])."
    ),
    threshold=Threshold("above", warning=0.01, critical=0.05, for_seconds=600,
                        note="Порог для доли ответов 5xx"),
))

_m(MetricSpec(
    name="asrhub_http_request_seconds", type="histogram", group="api",
    label="Время ответа", unit="с", labels=("method", "route"),
    description="Гистограмма времени обработки запроса.",
    recommendation=(
        "Загрузка файла попадает в тот же гистограмм, что и получение списка "
        "заданий, поэтому смотрите по маршрутам, а не в целом. Медленный "
        "GET /api/jobs означает, что база разрослась и пора чистить."
    ),
))

_m(MetricSpec(
    name="asrhub_http_in_flight", type="gauge", group="api",
    label="Запросов в обработке",
    description="Сколько запросов обрабатывается прямо сейчас.",
    recommendation="Устойчиво высокое значение при быстрых ответах означает, "
                   "что клиенты опрашивают сервер чаще, чем нужно.",
))

_m(MetricSpec(
    name="asrhub_auth_failures_total", type="counter", group="api",
    label="Отказов аутентификации", since_restart=True,
    description="Сколько запросов отклонено из-за отсутствующего или неверного ключа.",
    recommendation=(
        "Единичные — обычно забытый ключ в интеграции. Поток с одного адреса — "
        "перебор ключей; закройте сервис на уровне сети, а не приложения."
    ),
    threshold=Threshold("above", warning=50, critical=500, for_seconds=600,
                        note="Порог для прироста за десять минут"),
))

_m(MetricSpec(
    name="asrhub_rate_limited_total", type="counter", group="api",
    label="Отказов по частоте", since_restart=True,
    description="Сколько запросов отклонено ограничителем частоты.",
    recommendation=(
        "Постоянные отказы у пакетного клиента означают, что ему нужен отдельный "
        "ключ с повышенным rate_limit, а не отключение ограничителя целиком."
    ),
))

_m(MetricSpec(
    name="asrhub_websocket_clients", type="gauge", group="api",
    label="Клиентов WebSocket",
    description="Сколько интерфейсов подключено к ленте событий.",
    recommendation=(
        "Ноль при открытых вкладках означает, что WebSocket не проходит через "
        "прокси; интерфейс при этом работает, но через опрос и с задержкой."
    ),
))

# ---------------------------------------------------------------------------
# Ошибки
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_errors_total", type="counter", group="errors",
    label="Ошибок по кодам", labels=("code", "retryable"), since_restart=True,
    description=(
        "Счётчик ошибок заданий по коду с признаком, имеет ли смысл повторять. "
        "Коды те же, что в ответах API: out_of_memory, dependency_missing, audio_error "
        "и остальные."
    ),
    recommendation=(
        "Оповещение по коду полезнее, чем по общему числу: out_of_memory лечится "
        "настройками, dependency_missing — установкой пакета, audio_error — "
        "разговором с тем, кто присылает файлы. Общая цифра не подсказывает ничего."
    ),
    troubleshooting="GET /api/analytics/errors?period=day покажет разбивку и динамику",
))

_m(MetricSpec(
    name="asrhub_retries_total", type="counter", group="errors",
    label="Повторов заданий", since_restart=True,
    description="Сколько раз задания перепланировались на повтор.",
    recommendation=(
        "Повторы — штатный механизм, а не авария. Тревожно, когда их доля к числу "
        "заданий превышает четверть: значит, сервер работает на пределе памяти и "
        "половину времени тратит на вторые попытки."
    ),
))

_m(MetricSpec(
    name="asrhub_last_error_timestamp", type="gauge", group="errors",
    label="Время последней ошибки", unit="unixtime",
    description="Момент последней ошибки задания в формате unix-времени.",
    recommendation="Удобно для панели: time() - asrhub_last_error_timestamp "
                   "показывает, сколько сервис работает без ошибок.",
))

# ---------------------------------------------------------------------------
# Уведомления
# ---------------------------------------------------------------------------

_m(MetricSpec(
    name="asrhub_webhooks_total", type="counter", group="webhooks",
    label="Уведомлений отправлено", labels=("result",), since_restart=True,
    description="Доставка уведомлений о завершении заданий: ok или failed.",
    recommendation=(
        "Растущее число неудач означает, что принимающая сторона недоступна. На "
        "сами задания это не влияет — они выполнены, — но клиент о результатах "
        "не узнает и, скорее всего, пришлёт их повторно."
    ),
    threshold=Threshold("above", warning=10, critical=50, for_seconds=1800,
                        note="Порог для прироста неудачных доставок за полчаса"),
))

_m(MetricSpec(
    name="asrhub_push_targets_healthy", type="gauge", group="webhooks",
    label="Приёмников метрик доступно", labels=("target",),
    description=(
        "Единица, если последняя отправка метрик во внешнюю систему прошла успешно."
    ),
    recommendation=(
        "Мониторинг самого мониторинга. Молчащий приёмник означает, что вы "
        "перестали видеть проблемы, а не что их нет."
    ),
    threshold=Threshold("below", warning=1, for_seconds=900),
))


METRICS_BY_NAME = {m.name: m for m in METRICS}


def metrics_for_group(group: str) -> list[MetricSpec]:
    return [m for m in METRICS if m.group == group]


def stats() -> dict[str, Any]:
    return {
        "total": len(METRICS),
        "groups": len(GROUPS),
        "with_thresholds": sum(1 for m in METRICS if m.threshold),
        "with_recommendation": sum(1 for m in METRICS if m.recommendation),
        "counters": sum(1 for m in METRICS if m.type == "counter"),
        "gauges": sum(1 for m in METRICS if m.type == "gauge"),
        "histograms": sum(1 for m in METRICS if m.type == "histogram"),
    }
