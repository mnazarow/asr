"""Готовые наборы настроек под типовые сценарии.

Пресет задаёт только те параметры, которые отличаются от значений по умолчанию.
Остальные берутся из каталога параметров.
"""
from __future__ import annotations

from .schema import PresetSpec

PRESETS: list[PresetSpec] = [
    PresetSpec(
        id="ru-accuracy",
        name="Русский: максимальная точность",
        scenario="Протоколы совещаний, юридические записи, интервью для публикации",
        description=(
            "GigaAM v3 RNNT с готовой пунктуацией, широкий луч, аккуратная нарезка по VAD. "
            "Самый точный вариант для русского языка из доступных свободных моделей."
        ),
        hardware_hint="GPU от 8 ГБ либо CPU от 8 ядер (медленнее примерно в 10 раз)",
        expected="RTF около 0.10–0.15 на RTX 3060; WER на чистой речи менее 3 %",
        values={
            "engine": "gigaam", "model": "gigaam-v3-e2e-rnnt", "language": "ru",
            "beam_size": 8, "temperature_fallback": True,
            "vad_enabled": True, "vad_backend": "silero", "vad_threshold": 0.45,
            "vad_min_silence_ms": 700, "vad_max_speech_s": 22.0, "vad_speech_pad_ms": 300,
            "audio_normalize": True, "audio_highpass_hz": 80, "audio_trim_silence": True,
            "batch_size": 8, "word_timestamps": True, "include_confidence": True,
            "punctuation_enabled": False, "itn_enabled": False,
            "merge_short_segments": True, "paragraph_mode": "speaker",
            "output_formats": ["txt", "json", "docx", "srt"],
        },
    ),
    PresetSpec(
        id="ru-speed",
        name="Русский: массовая обработка архива",
        scenario="Тысячи файлов, важна пропускная способность",
        description=(
            "GigaAM v3 CTC с жадным поиском и крупным пакетом. Потеря точности "
            "относительно максимального режима — менее одного процентного пункта, "
            "выигрыш по времени — в три-четыре раза."
        ),
        hardware_hint="GPU от 12 ГБ",
        expected="RTF около 0.03–0.05 на RTX 3090; WER на чистой речи около 3–4 %",
        values={
            "engine": "gigaam", "model": "gigaam-v3-ctc", "language": "ru",
            "beam_size": 1, "temperature_fallback": True,
            "vad_enabled": True, "vad_threshold": 0.5, "vad_min_silence_ms": 500,
            "vad_max_speech_s": 22.0, "vad_speech_pad_ms": 150,
            "audio_normalize": True, "audio_trim_silence": True,
            "batch_size": 24, "word_timestamps": False, "include_confidence": True,
            "punctuation_enabled": True, "punctuation_model": "rupunct", "itn_enabled": True,
            "priority": 10, "max_concurrent_jobs": 1,
            "output_formats": ["txt", "json"],
        },
    ),
    PresetSpec(
        id="callcenter",
        name="Колл-центр и телефония",
        scenario="Записи разговоров 8 кГц, часто стерео с разделением по каналам",
        description=(
            "T-one оптимизирована именно под телефонию и обходит на ней все остальные "
            "свободные модели. Разделение по каналам даёт безошибочную атрибуцию реплик — "
            "точнее любой диаризации."
        ),
        hardware_hint="CPU от 4 ядер и 8 ГБ RAM; GPU не обязателен",
        expected="WER на колл-центре около 8.6 %; работает в реальном времени на CPU",
        values={
            "engine": "tone", "model": "tone-ru", "language": "ru",
            "audio_channels": "split", "audio_sample_rate": 16000,
            "audio_normalize": True, "audio_highpass_hz": 100,
            "vad_enabled": True, "vad_threshold": 0.4, "vad_min_speech_ms": 200,
            "vad_min_silence_ms": 400,
            "diarization_enabled": False,
            "speaker_names": "Оператор, Клиент",
            "punctuation_enabled": True, "itn_enabled": True,
            "merge_short_segments": False, "paragraph_mode": "speaker",
            "output_formats": ["json", "txt", "csv"],
            "log_transcript_text": False,
        },
    ),
    PresetSpec(
        id="meeting",
        name="Совещание с несколькими участниками",
        scenario="Записи планёрок, интервью, круглых столов",
        description=(
            "Распознавание с разделением по говорящим и разбиением на абзацы по сменам "
            "реплик. Результат сразу пригоден для протокола."
        ),
        hardware_hint="GPU от 12 ГБ (распознавание плюс диаризация)",
        expected="Время обработки примерно в 1.5 раза больше, чем без диаризации",
        values={
            "engine": "gigaam", "model": "gigaam-v3-e2e-rnnt", "language": "ru",
            "beam_size": 5,
            "vad_enabled": True, "vad_threshold": 0.35, "vad_min_silence_ms": 600,
            "vad_speech_pad_ms": 300,
            "audio_normalize": True, "audio_highpass_hz": 80,
            "diarization_enabled": True, "diarization_backend": "pyannote",
            "diarization_min_speakers": 2, "diarization_max_speakers": 8,
            "diarization_assign_words": True,
            "word_timestamps": True, "include_speaker_labels": True,
            "merge_short_segments": True, "min_segment_duration_s": 1.5,
            "paragraph_mode": "speaker", "remove_filler_words": True,
            "summary_enabled": True,
            "output_formats": ["docx", "json", "txt"],
        },
    ),
    PresetSpec(
        id="subtitles",
        name="Субтитры для видео",
        scenario="Подготовка субтитров к роликам, курсам, вебинарам",
        description=(
            "Короткие сегменты, точные пословные таймкоды, форматирование по "
            "вещательному стандарту: 42 символа в строке, две строки."
        ),
        hardware_hint="GPU от 8 ГБ",
        expected="Готовые SRT и VTT без ручной правки таймингов",
        values={
            "engine": "gigaam", "model": "gigaam-v3-e2e-rnnt", "language": "ru",
            "beam_size": 5,
            "vad_enabled": True, "vad_min_silence_ms": 400, "vad_max_speech_s": 12.0,
            "vad_speech_pad_ms": 150,
            "word_timestamps": True,
            "merge_short_segments": False, "paragraph_mode": "none",
            "itn_enabled": False,
            "subtitle_max_line_width": 42, "subtitle_max_lines": 2,
            "subtitle_min_duration_s": 1.0,
            "output_formats": ["srt", "vtt", "json"],
        },
    ),
    PresetSpec(
        id="multilingual",
        name="Мультиязычный поток",
        scenario="Архив на нескольких языках, язык заранее неизвестен",
        description=(
            "Parakeet TDT v3: 25 европейских языков с автоопределением, пунктуация "
            "и пословные таймкоды, рекордная пропускная способность."
        ),
        hardware_hint="GPU NVIDIA от 8 ГБ; NeMo лучше ставить в отдельное окружение",
        expected="RTFx свыше 3000 на A100; FLEURS ru — 5.51 % WER",
        values={
            "engine": "nemo", "model": "parakeet-tdt-0.6b-v3", "language": "auto",
            "vad_enabled": True, "vad_max_speech_s": 300.0,
            "audio_normalize": True,
            "batch_size": 16, "word_timestamps": True,
            "punctuation_enabled": False, "itn_enabled": False,
            "output_formats": ["json", "txt", "srt"],
        },
    ),
    PresetSpec(
        id="whisper-robust",
        name="Whisper: устойчивый режим",
        scenario="Записи плохого качества, шум, длинные паузы, музыка",
        description=(
            "Полный набор мер против галлюцинаций: VAD, отключённый перенос контекста, "
            "каскад температур, словарный фильтр и пропуск длинной тишины."
        ),
        hardware_hint="GPU от 8 ГБ либо CPU с int8",
        expected="Заметно меньше выдуманного текста ценой 10–15 % времени",
        values={
            "engine": "faster_whisper", "model": "faster-whisper-large-v3", "language": "ru",
            "beam_size": 5, "temperature_fallback": True,
            "condition_on_previous_text": False,
            "compression_ratio_threshold": 2.2, "logprob_threshold": -1.0,
            "no_speech_threshold": 0.5,
            "hallucination_silence_threshold": 2.0, "hallucination_filter": True,
            "repetition_penalty": 1.1,
            "vad_enabled": True, "vad_backend": "silero", "vad_threshold": 0.5,
            "vad_min_silence_ms": 700, "vad_speech_pad_ms": 300,
            "audio_normalize": True, "audio_highpass_hz": 80,
            "compute_type": "float16", "batch_size": 8,
            "word_timestamps": True,
            "output_formats": ["json", "txt", "srt"],
        },
    ),
    PresetSpec(
        id="cpu-only",
        name="Сервер без видеокарты",
        scenario="Обычный сервер или ноутбук без GPU",
        description=(
            "Квантизация int8, умеренная модель, жадный поиск, ограниченная параллельность. "
            "Настройки подобраны так, чтобы обработка шла быстрее реального времени."
        ),
        hardware_hint="CPU от 8 физических ядер, 16 ГБ RAM",
        expected="RTF примерно 0.4–0.8 на 8 ядрах; для русского лучше GigaAM CTC в ONNX",
        values={
            "engine": "faster_whisper", "model": "faster-whisper-small", "language": "ru",
            "device": "cpu", "compute_type": "int8", "cpu_threads": 0,
            "beam_size": 1, "batch_size": 4, "num_workers": 1,
            "vad_enabled": True, "vad_threshold": 0.5,
            "condition_on_previous_text": False, "hallucination_filter": True,
            "word_timestamps": False,
            "max_concurrent_jobs": 1,
            "output_formats": ["txt", "json"],
        },
    ),
    PresetSpec(
        id="apple-silicon",
        name="macOS на Apple Silicon",
        scenario="Локальная обработка на MacBook или Mac mini",
        description=(
            "whisper.cpp с ускорением Metal и Core ML: энкодер выполняется на Neural Engine, "
            "что даёт более чем трёхкратное ускорение относительно чистого CPU."
        ),
        hardware_hint="Apple M1 и новее, от 16 ГБ объединённой памяти",
        expected="Квантованная large-v3-turbo занимает 574 МБ и работает быстрее реального времени",
        values={
            "engine": "whisper_cpp", "model": "whispercpp-large-v3-turbo-q5_0", "language": "ru",
            "device": "mps", "beam_size": 5, "flash_attention": True, "cpu_threads": 0,
            "vad_enabled": True, "vad_min_silence_ms": 500,
            "condition_on_previous_text": False, "hallucination_filter": True,
            "word_timestamps": True,
            "output_formats": ["txt", "srt", "json"],
        },
    ),
    PresetSpec(
        id="realtime",
        name="Реальное время",
        scenario="Живые субтитры, голосовые интерфейсы, мониторинг эфира",
        description=(
            "Потоковое распознавание с настраиваемой задержкой и промежуточными "
            "результатами. Для русского языка на телефонии лучший выбор — T-one."
        ),
        hardware_hint="CPU от 4 ядер для T-one; GPU для Nemotron",
        expected="Задержка около 1 секунды, работа в реальном времени",
        values={
            "engine": "tone", "model": "tone-ru", "language": "ru",
            "streaming_latency_ms": 560, "streaming_chunk_ms": 300,
            "streaming_partial_results": True, "streaming_endpoint_silence_ms": 600,
            "vad_enabled": True, "vad_threshold": 0.4,
            "punctuation_enabled": True, "itn_enabled": False,
            "word_timestamps": False,
            "output_formats": ["json", "txt"],
        },
    ),
    PresetSpec(
        id="diagnostics",
        name="Проверка установки",
        scenario="Первый запуск, тестирование очереди и интерфейса",
        description=(
            "Встроенный симулятор без внешних зависимостей. Позволяет убедиться, что "
            "сервер, очередь, аналитика и интерфейс работают, до загрузки моделей."
        ),
        hardware_hint="Любое",
        expected="Мгновенный синтетический результат",
        values={
            "engine": "demo", "model": "demo-simulator", "language": "ru",
            "vad_enabled": False, "word_timestamps": True,
            "output_formats": ["txt", "json"],
        },
    ),
]

PRESETS_BY_ID: dict[str, PresetSpec] = {p.id: p for p in PRESETS}


def get_preset(preset_id: str) -> PresetSpec | None:
    return PRESETS_BY_ID.get(preset_id)
