"""Каталог моделей распознавания речи.

Все сведения проверены по первичным источникам (карточки Hugging Face,
README репозиториев, статьи arXiv, независимые бенчмарки) по состоянию
на 31 августа 2026 года. Каждое числовое значение сопровождается ссылкой
на источник в поле ``Benchmark.source``. Там, где публичных данных нет,
поле остаётся пустым — выдуманных цифр в каталоге нет.
"""
from __future__ import annotations

from .schema import Benchmark as B
from .schema import Maturity as M
from .schema import ModelSpec
from .schema import Quality as Q
from .schema import Timestamps as T

CATALOG_DATE = "2026-08-31"

# Источники, на которые ссылаются бенчмарки
SOURCES: dict[str, str] = {
    "sber-v3": "developers.sber.ru/kak-v-sbere/culture/gigaAM-v3 + habr.com/ru/companies/sberdevices/articles/973160/",
    "gigaam-hf": "huggingface.co/ai-sage/GigaAM-v3",
    "gigaam-is2025": "Kutsakov et al., Interspeech 2025, arXiv:2506.01192",
    "gigaam-ml": "arXiv:2607.10371 (GigaAM Multilingual)",
    "alphacephei-2025": "alphacephei.com/nsh/2025/04/18/russian-models.html (независимый бенчмарк, апрель 2025)",
    "vosk-models": "alphacephei.com/vosk/models",
    "vosk-hf-054": "huggingface.co/alphacep/vosk-model-ru",
    "tone-habr": "habr.com/ru/companies/tbank/articles/929850/",
    "openasr-arxiv": "Open ASR Leaderboard, arXiv:2510.06961v4 (снимок 27.03.2026)",
    "openasr-live": "huggingface.co/datasets/hf-audio/open-asr-leaderboard (снимок 08.2026)",
    "parakeet-v3": "huggingface.co/nvidia/parakeet-tdt-0.6b-v3",
    "parakeet-v2": "huggingface.co/nvidia/parakeet-tdt-0.6b-v2",
    "canary-v2": "huggingface.co/nvidia/canary-1b-v2",
    "canary-flash": "huggingface.co/nvidia/canary-1b-flash",
    "canary-180m": "huggingface.co/nvidia/canary-180m-flash",
    "canary-qwen": "huggingface.co/nvidia/canary-qwen-2.5b",
    "nemotron-stream": "huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b",
    "qwen3asr": "huggingface.co/Qwen/Qwen3-ASR-1.7B + arXiv:2601.21337",
    "moss": "huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize",
    "cohere": "huggingface.co/CohereLabs/cohere-transcribe-03-2026",
    "granite": "huggingface.co/ibm-granite/granite-speech-4.1-2b",
    "voxtral": "huggingface.co/mistralai/Voxtral-Mini-3B-2507",
    "voxtral-rt": "huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602 + arXiv:2602.11298",
    "whisper-readme": "github.com/openai/whisper (README)",
    "whisper-ru": "huggingface.co/antony66/whisper-large-v3-russian (Common Voice 17.0 ru)",
    "fw-readme": "github.com/SYSTRAN/faster-whisper (README, RTX 3070 Ti, CUDA 12.4)",
    "distil": "huggingface.co/distil-whisper/distil-large-v3.5",
    "w2v-grosman": "huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-russian",
    "w2v-grosman-1b": "huggingface.co/jonatasgrosman/wav2vec2-xls-r-1b-russian",
    "w2v-bond005": "huggingface.co/bond005/wav2vec2-large-ru-golos",
    "omnilingual": "github.com/facebookresearch/omnilingual-asr",
    "sensevoice": "huggingface.co/FunAudioLLM/SenseVoiceSmall + funasr.com",
    "kyutai": "huggingface.co/kyutai/stt-1b-en_fr",
    "moonshine": "huggingface.co/UsefulSensors/moonshine",
    "owsm": "huggingface.co/espnet/owsm_ctc_v4_1B",
    "phi4": "huggingface.co/microsoft/Phi-4-multimodal-instruct",
    "borealis": "huggingface.co/Vikhrmodels/Borealis",
    "pyannote": "huggingface.co/pyannote/speaker-diarization-community-1",
    "sortformer": "huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2",
    "silero-vad": "github.com/snakers4/silero-vad",
    "whispercpp": "github.com/ggml-org/whisper.cpp (README)",
}

_RU = ["ru"]
_EU25 = ["bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
         "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk"]
_WHISPER_LANGS = ["multi-99"]

MODELS: list[ModelSpec] = []


def _add(spec: ModelSpec) -> ModelSpec:
    MODELS.append(spec)
    return spec


# ---------------------------------------------------------------------------
# GigaAM (SaluteDevices / Сбер) — эталон для русского языка
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="gigaam-v3-rnnt",
    name="GigaAM v3 RNNT",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v3",
    revision="rnnt",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=220,
    disk_mb=446,
    vram_gb=2.0,
    ram_gb=4.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2025-12",
    maturity=M.STABLE,
    benchmarks=[
        B("Golos Farfield", "WER", 3.9, SOURCES["sber-v3"]),
        B("Golos Crowd", "WER", 2.4, SOURCES["sber-v3"]),
        B("Russian LibriSpeech", "WER", 4.4, SOURCES["sber-v3"]),
        B("Common Voice 19 ru", "WER", 0.9, SOURCES["sber-v3"]),
        B("Natural Speech", "WER", 6.9, SOURCES["sber-v3"]),
        B("Callcenter", "WER", 9.5, SOURCES["sber-v3"]),
        B("Среднее по 7 наборам", "WER", 6.7, SOURCES["sber-v3"]),
    ],
    tags=["русский", "sota", "офлайн", "mit"],
    recommended_for=["Протоколы совещаний", "Медиаархивы", "Субтитры", "Максимальная точность на русском"],
    not_recommended_for=["Реальное время", "Мультиязычные потоки"],
    strengths=[
        "Лучшее качество на русском среди свободных моделей",
        "Лицензия MIT без ограничений",
        "Пословные таймкоды, экспорт в ONNX и TensorRT",
        "Не требует NVIDIA NeMo начиная с версии v3",
    ],
    weaknesses=[
        "Нет потокового режима",
        "Один проход ограничен 25 секундами — длинное аудио через VAD-нарезку",
        "Режим transcribe_longform требует pyannote и токен Hugging Face",
    ],
    notes="RNNT-декодер даёт лучшее качество, но работает медленнее CTC примерно в 1.5–2 раза.",
    default_params={"batch_size": 8, "chunk_length_s": 22.0, "vad_enabled": True},
    aliases=["gigaam", "gigaam-v3"],
))

_add(ModelSpec(
    id="gigaam-v3-ctc",
    name="GigaAM v3 CTC",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v3",
    revision="ctc",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=220,
    disk_mb=442,
    vram_gb=2.0,
    ram_gb=4.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2025-12",
    benchmarks=[
        B("Golos Farfield", "WER", 4.5, SOURCES["sber-v3"]),
        B("Golos Crowd", "WER", 2.8, SOURCES["sber-v3"]),
        B("Russian LibriSpeech", "WER", 4.7, SOURCES["sber-v3"]),
        B("Common Voice 19 ru", "WER", 1.3, SOURCES["sber-v3"]),
        B("Callcenter", "WER", 10.3, SOURCES["sber-v3"]),
        B("Среднее по 7 наборам", "WER", 7.4, SOURCES["sber-v3"]),
    ],
    tags=["русский", "быстрый", "офлайн", "mit"],
    recommended_for=["Пакетная обработка больших архивов", "Ограниченные вычислительные ресурсы"],
    strengths=["В 1.5–2 раза быстрее RNNT при потере точности менее 1 п.п.",
               "Хорошо квантуется в ONNX int8"],
    weaknesses=["Немного выше WER, чем у RNNT"],
    notes=("В независимом бенчмарке Alphacephei (апрель 2025) вариант CTC с внешней "
           "языковой моделью обошёл RNNT — на длинных доменных записях стоит проверить оба."),
    default_params={"batch_size": 16, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-v3-e2e-rnnt",
    name="GigaAM v3 E2E RNNT (с пунктуацией)",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v3",
    revision="e2e_rnnt",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=220,
    disk_mb=449,
    vram_gb=2.0,
    timestamps=T.WORD,
    punctuation=True,
    max_audio_s=25,
    released="2025-12",
    benchmarks=[
        B("Golos Farfield", "WER", 5.5, SOURCES["sber-v3"], note="после расформатирования текста"),
        B("Common Voice 19 ru", "WER", 3.0, SOURCES["sber-v3"], note="после расформатирования текста"),
        B("Среднее по 7 наборам", "WER", 9.7, SOURCES["sber-v3"], note="метрика занижена из-за форматирования"),
    ],
    tags=["русский", "пунктуация", "готовый текст", "mit"],
    recommended_for=["Готовые к публикации протоколы", "Субтитры без постобработки", "Документооборот"],
    strengths=[
        "Сразу выдаёт пунктуацию, заглавные буквы и числа цифрами",
        "Не нужны отдельные модели пунктуации и нормализации",
        "В слепом сравнении выигрывает у Whisper large-v3 со счётом 70:30",
    ],
    weaknesses=[
        "Формальный WER выше — метрика штрафует форматирование, а не ошибки",
        "Нормализацию чисел нельзя отключить",
    ],
    notes=("Более высокий WER у E2E-вариантов — артефакт измерения: для расчёта метрики "
           "форматированный текст приходится приводить обратно к «сырому» виду."),
    default_params={"batch_size": 8, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-v3-e2e-ctc",
    name="GigaAM v3 E2E CTC (с пунктуацией)",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v3",
    revision="e2e_ctc",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=220,
    disk_mb=445,
    vram_gb=2.0,
    timestamps=T.WORD,
    punctuation=True,
    max_audio_s=25,
    released="2025-12",
    benchmarks=[B("Среднее по 7 наборам", "WER", 10.2, SOURCES["sber-v3"],
                  note="метрика занижена из-за форматирования")],
    tags=["русский", "пунктуация", "быстрый", "mit"],
    recommended_for=["Массовая обработка с готовым форматированием"],
    strengths=["Пунктуация плюс скорость CTC"],
    weaknesses=["Точность чуть ниже E2E RNNT"],
    default_params={"batch_size": 16, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-v2-rnnt",
    name="GigaAM v2 RNNT",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v2",
    revision="rnnt",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=240,
    disk_mb=480,
    vram_gb=2.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2024-12",
    maturity=M.LEGACY,
    benchmarks=[
        B("Среднее по 11 наборам", "WER", 8.64, SOURCES["alphacephei-2025"],
          note="независимый бенчмарк, 2-е место"),
        B("Golos Farfield", "WER", 4.4, SOURCES["alphacephei-2025"]),
        B("Команды Яндекса", "WER", 1.9, SOURCES["alphacephei-2025"]),
    ],
    tags=["русский", "проверенный", "mit"],
    recommended_for=["Совместимость с существующими конвейерами", "Работа через sherpa-onnx"],
    strengths=["Единственная версия GigaAM с готовыми сборками в sherpa-onnx"],
    weaknesses=["Уступает v3 примерно на 30 % по WER на новых доменах"],
    notes="Оставлена для совместимости; для новых установок берите v3.",
    default_params={"batch_size": 8, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-v2-ctc",
    name="GigaAM v2 CTC",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-v2",
    revision="ctc",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.EXCELLENT,
    params_m=240,
    disk_mb=476,
    vram_gb=2.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2024-12",
    maturity=M.LEGACY,
    benchmarks=[B("Среднее по 11 наборам", "WER", 8.42, SOURCES["alphacephei-2025"],
                  note="с языковой моделью; 1-е место в независимом бенчмарке")],
    tags=["русский", "проверенный", "mit"],
    recommended_for=["Встраивание через sherpa-onnx без Python"],
    strengths=["Лучший результат в независимом бенчмарке Alphacephei с внешней LM"],
    weaknesses=["Уступает v3 на новых доменах"],
    default_params={"batch_size": 16, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-multilingual-large-ctc",
    name="GigaAM Multilingual Large CTC",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-Multilingual",
    revision="large_ctc",
    license="MIT",
    commercial_use=True,
    languages=["ru", "kk", "ky", "uz", "en"],
    ru_quality=Q.GOOD,
    params_m=600,
    disk_mb=1200,
    vram_gb=4.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2026-07",
    maturity=M.NEW,
    benchmarks=[
        B("Common Voice ru", "WER", 5.1, SOURCES["gigaam-ml"]),
        B("Common Voice kk", "WER", 13.8, SOURCES["gigaam-ml"], language="kk"),
        B("Common Voice ky", "WER", 10.2, SOURCES["gigaam-ml"], language="ky"),
        B("Common Voice uz", "WER", 9.2, SOURCES["gigaam-ml"], language="uz"),
        B("Common Voice en", "WER", 21.5, SOURCES["gigaam-ml"], language="en"),
    ],
    tags=["мультиязычный", "снг", "mit"],
    recommended_for=["Казахский, киргизский, узбекский", "Смешанная речь в странах СНГ"],
    not_recommended_for=["Чисто русские записи — v3 точнее в разы"],
    strengths=["Предобучение на 2 млн часов", "Лучший свободный охват языков СНГ"],
    weaknesses=["На чистом русском заметно уступает GigaAM v3"],
    default_params={"batch_size": 8, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-multilingual-ctc",
    name="GigaAM Multilingual CTC",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM-Multilingual",
    revision="ctc",
    license="MIT",
    commercial_use=True,
    languages=["ru", "kk", "ky", "uz", "en"],
    ru_quality=Q.GOOD,
    params_m=220,
    disk_mb=445,
    vram_gb=2.0,
    timestamps=T.WORD,
    max_audio_s=25,
    released="2026-07",
    maturity=M.NEW,
    benchmarks=[
        B("Common Voice ru", "WER", 7.1, SOURCES["gigaam-ml"]),
        B("Common Voice kk", "WER", 17.2, SOURCES["gigaam-ml"], language="kk"),
    ],
    tags=["мультиязычный", "снг", "лёгкий", "mit"],
    recommended_for=["Языки СНГ на слабом железе"],
    strengths=["Втрое легче Large при умеренной потере качества"],
    weaknesses=["Заметно уступает Large на всех языках"],
    default_params={"batch_size": 16, "chunk_length_s": 22.0, "vad_enabled": True},
))

_add(ModelSpec(
    id="gigaam-emo",
    name="GigaAM Emotion",
    family="GigaAM",
    engine="gigaam",
    source="ai-sage/GigaAM",
    revision="emo",
    license="MIT",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.NONE,
    params_m=240,
    disk_mb=480,
    vram_gb=2.0,
    timestamps=T.NONE,
    emotion=True,
    max_audio_s=25,
    released="2024-12",
    tags=["эмоции", "аналитика", "mit"],
    recommended_for=["Аналитика колл-центра", "Оценка тональности разговора"],
    strengths=["Классификация эмоций поверх того же энкодера"],
    weaknesses=["Не выполняет распознавание речи — только классификацию"],
    notes="Вспомогательная модель: подключается как дополнительный этап конвейера, не как основной движок.",
))

# ---------------------------------------------------------------------------
# T-one (Т-Банк) — лучший свободный русский стриминг
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="tone-ru",
    name="T-one (Т-Банк)",
    family="T-one",
    engine="tone",
    source="t-tech/T-one",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.GOOD,
    params_m=71.6,
    disk_mb=290,
    vram_gb=1.0,
    ram_gb=8.0,
    streaming=True,
    timestamps=T.SEGMENT,
    max_audio_s=None,
    rtfx=None,
    rtfx_hw="работает в реальном времени; пропускная способность на TensorRT — "
            "5952 RPS на T4, 26112 на A100, 57344 на H100",
    released="2025-07-22",
    benchmarks=[
        B("Колл-центр", "WER", 8.63, SOURCES["tone-habr"], note="с языковой моделью"),
        B("Прочая телефония", "WER", 6.20, SOURCES["tone-habr"]),
        B("Именованные сущности", "WER", 5.83, SOURCES["tone-habr"]),
        B("Common Voice 19 ru", "WER", 5.32, SOURCES["tone-habr"]),
        B("OpenSTT", "WER", 7.94, SOURCES["tone-habr"]),
    ],
    tags=["русский", "стриминг", "телефония", "apache"],
    recommended_for=["Колл-центры", "Голосовые боты", "Живые субтитры", "Телефонные записи 8 кГц"],
    not_recommended_for=["Студийные записи — GigaAM точнее вдвое"],
    strengths=[
        "Лучшее качество на телефонии среди свободных моделей",
        "Настоящий потоковый режим: чанки по 300 мс, задержка 1.0–1.2 с",
        "Всего 71.6 млн параметров — работает на 4 ядрах CPU",
        "Пропускная способность на TensorRT: T4 — 5952 RPS, H100 — 57344 RPS",
    ],
    weaknesses=[
        "На чистой студийной речи уступает GigaAM примерно вдвое",
        "KenLM не собирается нативно под Windows — нужен WSL или Docker",
    ],
    notes="Требует Python 3.9+, Poetry 2.1+, минимум 4 ядра CPU и 8 ГБ RAM.",
    default_params={"chunk_ms": 300, "use_lm": True},
))

# ---------------------------------------------------------------------------
# Vosk — офлайн и встраиваемые сценарии
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="vosk-ru-0.54",
    name="Vosk ru 0.54 (Zipformer2)",
    family="Vosk",
    engine="vosk",
    source="alphacep/vosk-model-ru",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.GOOD,
    disk_mb=1010,
    ram_gb=4.0,
    streaming=True,
    timestamps=T.WORD,
    released="2025",
    maturity=M.NEW,
    benchmarks=[
        B("Common Voice ru", "WER", 6.1, SOURCES["vosk-hf-054"]),
        B("Среднее по 11 наборам", "WER", 11.02, SOURCES["alphacephei-2025"]),
        B("Аудиокниги", "WER", 1.2, SOURCES["alphacephei-2025"]),
        B("Звонки поддержки", "WER", 12.9, SOURCES["alphacephei-2025"]),
    ],
    tags=["русский", "стриминг", "cpu", "apache"],
    recommended_for=["Потоковое распознавание на CPU", "Серверы без GPU"],
    strengths=["Архитектура Zipformer2, заметно лучше прежних моделей Vosk",
               "Настоящий стриминг, работа без GPU"],
    weaknesses=["Не входит в официальный список моделей на сайте Vosk",
                "Уступает GigaAM на большинстве наборов"],
    notes="Опубликована на Hugging Face, но отсутствует в официальном перечне alphacephei.com/vosk/models.",
))

_add(ModelSpec(
    id="vosk-ru-0.42",
    name="Vosk ru 0.42",
    family="Vosk",
    engine="vosk",
    source="https://alphacephei.com/vosk/models/vosk-model-ru-0.42.zip",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.FAIR,
    disk_mb=1800,
    ram_gb=6.0,
    streaming=True,
    timestamps=T.WORD,
    released="2022",
    benchmarks=[
        B("Аудиокниги", "WER", 4.5, SOURCES["vosk-models"]),
        B("Golos Crowd", "WER", 4.4, SOURCES["vosk-models"]),
        B("OpenSTT audiobooks", "WER", 11.1, SOURCES["vosk-models"]),
        B("YouTube", "WER", 19.5, SOURCES["vosk-models"]),
        B("Телефонные звонки", "WER", 36.0, SOURCES["vosk-models"]),
    ],
    tags=["русский", "стриминг", "cpu", "apache"],
    recommended_for=["Офлайн-распознавание без GPU", "Стабильный проверенный вариант"],
    strengths=["Стриминг из коробки, биндинги для 8 языков программирования"],
    weaknesses=["Очень слабый результат на телефонии (WER 36 %)"],
))

_add(ModelSpec(
    id="vosk-small-ru-0.22",
    name="Vosk small ru 0.22",
    family="Vosk",
    engine="vosk",
    source="https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.POOR,
    disk_mb=45,
    ram_gb=0.3,
    streaming=True,
    timestamps=T.WORD,
    released="2022",
    benchmarks=[
        B("Golos Crowd", "WER", 11.79, SOURCES["vosk-models"]),
        B("OpenSTT audiobooks", "WER", 22.71, SOURCES["vosk-models"]),
        B("YouTube", "WER", 31.97, SOURCES["vosk-models"]),
    ],
    tags=["русский", "встраиваемый", "45мб", "apache"],
    recommended_for=["Голосовые команды", "Raspberry Pi и мобильные устройства", "Резервный движок"],
    not_recommended_for=["Транскрибация свободной речи"],
    strengths=["45 МБ на диске и 300 МБ оперативной памяти",
               "Динамическая перенастройка словаря под ограниченный набор команд"],
    weaknesses=["Качество непригодно для транскрибации связной речи"],
))

_add(ModelSpec(
    id="vosk-en-us-0.22",
    name="Vosk en-us 0.22",
    family="Vosk",
    engine="vosk",
    source="https://alphacephei.com/vosk/models/vosk-model-en-us-0.22.zip",
    license="Apache-2.0",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    disk_mb=1800,
    ram_gb=6.0,
    streaming=True,
    timestamps=T.WORD,
    tags=["английский", "стриминг", "cpu", "apache"],
    recommended_for=["Английский офлайн на CPU"],
    strengths=["Стриминг без GPU"],
    weaknesses=["Заметно уступает современным нейросетевым моделям"],
))

# ---------------------------------------------------------------------------
# Vikhr Borealis — русскоязычная аудио-LLM
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="borealis-ru",
    name="Vikhr Borealis",
    family="Vikhr",
    engine="transformers",
    source="Vikhrmodels/Borealis",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.FAIR,
    params_m=5000,
    vram_gb=12.0,
    timestamps=T.NONE,
    punctuation=True,
    released="2025",
    maturity=M.EXPERIMENTAL,
    benchmarks=[
        B("Среднее (заявлено авторами)", "WER", 6.33, SOURCES["borealis"],
          note="самозаявленная метрика"),
        B("Среднее по 11 наборам", "WER", 15.99, SOURCES["alphacephei-2025"],
          note="независимая проверка, результат вдвое хуже заявленного"),
    ],
    tags=["русский", "аудио-llm", "пунктуация", "apache"],
    recommended_for=["Эксперименты с аудио-LLM", "Транскрипт с последующим анализом"],
    not_recommended_for=["Продуктивная транскрибация — расхождение метрик не разрешено"],
    strengths=["Пунктуация из коробки", "Архитектура в духе Voxtral"],
    weaknesses=["Самозаявленные метрики вдвое расходятся с независимой проверкой",
                "Требует много видеопамяти"],
    notes="При выборе учитывайте расхождение: 6.33 по карточке модели против 15.99 у Alphacephei.",
))

# ---------------------------------------------------------------------------
# wav2vec2 / XLS-R для русского — исторические модели
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="w2v2-xlsr53-ru",
    name="wav2vec2 XLSR-53 Russian",
    family="wav2vec2",
    engine="transformers",
    source="jonatasgrosman/wav2vec2-large-xlsr-53-russian",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.FAIR,
    params_m=317,
    disk_mb=1260,
    vram_gb=3.0,
    timestamps=T.WORD,
    maturity=M.LEGACY,
    benchmarks=[
        B("Common Voice ru", "WER", 13.30, SOURCES["w2v-grosman"], note="без языковой модели"),
        B("Common Voice ru", "WER", 9.57, SOURCES["w2v-grosman"], note="с языковой моделью"),
        B("Common Voice ru", "CER", 2.88, SOURCES["w2v-grosman"]),
    ],
    tags=["русский", "ctc", "выравнивание", "apache"],
    recommended_for=["Форсированное выравнивание пословных таймкодов", "Дообучение под свой домен"],
    not_recommended_for=["Продуктивная транскрибация"],
    strengths=["Чистая CTC-модель без trust_remote_code", "Годится как алайнер для WhisperX"],
    weaknesses=["Уровень 2021 года, уступает GigaAM в 3–4 раза"],
))

_add(ModelSpec(
    id="w2v2-xlsr1b-ru",
    name="wav2vec2 XLS-R 1B Russian",
    family="wav2vec2",
    engine="transformers",
    source="jonatasgrosman/wav2vec2-xls-r-1b-russian",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.GOOD,
    params_m=1000,
    disk_mb=3900,
    vram_gb=6.0,
    timestamps=T.WORD,
    maturity=M.LEGACY,
    benchmarks=[
        B("Common Voice 8 ru", "WER", 9.82, SOURCES["w2v-grosman-1b"], note="без языковой модели"),
        B("Common Voice 8 ru", "WER", 7.08, SOURCES["w2v-grosman-1b"], note="с языковой моделью"),
    ],
    tags=["русский", "ctc", "apache"],
    recommended_for=["Исследования", "Дообучение"],
    strengths=["Лучший результат среди открытых wav2vec2 для русского"],
    weaknesses=["Требует 6 ГБ видеопамяти при качестве ниже GigaAM"],
))

_add(ModelSpec(
    id="w2v2-golos-ru",
    name="wav2vec2 Golos Russian (bond005)",
    family="wav2vec2",
    engine="transformers",
    source="bond005/wav2vec2-large-ru-golos",
    license="Apache-2.0",
    commercial_use=True,
    languages=_RU,
    ru_quality=Q.FAIR,
    params_m=317,
    disk_mb=1260,
    vram_gb=3.0,
    timestamps=T.WORD,
    maturity=M.LEGACY,
    benchmarks=[
        B("Golos Crowd", "WER", 10.14, SOURCES["w2v-bond005"]),
        B("Golos Farfield", "WER", 20.35, SOURCES["w2v-bond005"]),
        B("Common Voice ru", "WER", 18.55, SOURCES["w2v-bond005"]),
    ],
    tags=["русский", "ctc", "golos", "apache"],
    recommended_for=["Дообучение на данных Golos"],
    strengths=["Обучена с аугментациями на Golos"],
    weaknesses=["Слабый перенос на другие домены"],
))

# ---------------------------------------------------------------------------
# Whisper (OpenAI) — эталонная мультиязычная модель
# ---------------------------------------------------------------------------

_WHISPER_SIZES = [
    # id-суффикс, имя, параметры (млн), диск МБ, VRAM ГБ, отн. скорость, только англ.
    ("tiny", "Whisper tiny", 39, 75, 1.0, 10.0, False),
    ("tiny.en", "Whisper tiny.en", 39, 75, 1.0, 10.0, True),
    ("base", "Whisper base", 74, 142, 1.0, 7.0, False),
    ("base.en", "Whisper base.en", 74, 142, 1.0, 7.0, True),
    ("small", "Whisper small", 244, 466, 2.0, 4.0, False),
    ("small.en", "Whisper small.en", 244, 466, 2.0, 4.0, True),
    ("medium", "Whisper medium", 769, 1500, 5.0, 2.0, False),
    ("medium.en", "Whisper medium.en", 769, 1500, 5.0, 2.0, True),
    ("large-v1", "Whisper large-v1", 1550, 2900, 10.0, 1.0, False),
    ("large-v2", "Whisper large-v2", 1550, 2900, 10.0, 1.0, False),
    ("large-v3", "Whisper large-v3", 1550, 2900, 10.0, 1.0, False),
    ("large-v3-turbo", "Whisper large-v3-turbo", 809, 1620, 6.0, 8.0, False),
]

for _sid, _name, _par, _disk, _vram, _spd, _en_only in _WHISPER_SIZES:
    _is_large_v3 = _sid in ("large-v3", "large-v3-turbo")
    _bench = []
    if _sid == "large-v3":
        _bench = [
            B("Common Voice 17.0 ru", "WER", 9.84, SOURCES["whisper-ru"]),
            B("Среднее по 11 наборам (ru)", "WER", 16.21, SOURCES["alphacephei-2025"]),
            B("Open ASR Leaderboard (en)", "WER", 7.44, SOURCES["openasr-arxiv"], language="en"),
            B("Golos Farfield", "WER", 17.6, SOURCES["alphacephei-2025"]),
        ]
    elif _sid == "large-v3-turbo":
        _bench = [
            B("Среднее по 11 наборам (ru)", "WER", 16.84, SOURCES["alphacephei-2025"]),
            B("Open ASR Leaderboard (en)", "WER", 7.83, SOURCES["openasr-arxiv"], language="en"),
        ]
    _add(ModelSpec(
        id=f"whisper-{_sid}",
        name=_name,
        family="Whisper",
        engine="whisper",
        source=_sid,
        license="MIT",
        commercial_use=True,
        languages=["en"] if _en_only else _WHISPER_LANGS,
        ru_quality=Q.NONE if _en_only else (
            Q.FAIR if _is_large_v3 else (Q.POOR if _par >= 769 else Q.POOR)),
        params_m=_par,
        disk_mb=_disk,
        vram_gb=_vram,
        ram_gb=max(2.0, _vram),
        timestamps=T.WORD,
        translation=not _en_only and _sid != "large-v3-turbo",
        max_audio_s=None,
        rtfx=None,
        released="2024-09" if _sid == "large-v3-turbo" else "2023-11",
        maturity=M.STABLE if _is_large_v3 else M.LEGACY,
        benchmarks=_bench,
        tags=(["английский"] if _en_only else ["мультиязычный", "99 языков"]) + ["mit", "эталон"],
        recommended_for=(
            ["Мультиязычные записи", "Языки без специализированных моделей"]
            if not _en_only else ["Английская речь"]),
        not_recommended_for=(["Русский язык — GigaAM точнее в 2–6 раз"] if not _en_only else []),
        strengths=[
            "99 языков и автоопределение языка",
            "Огромная экосистема реализаций и инструментов",
            "Лицензия MIT",
        ],
        weaknesses=[
            "Склонность к галлюцинациям на тишине и шуме",
            "Окно 30 секунд, длинное аудио обрабатывается скользящим окном",
        ] + (["На русском заметно уступает GigaAM и parakeet"] if not _en_only else []),
        notes=("Официальный пакет openai-whisper — эталонная, но самая медленная реализация. "
               "Для продуктивной работы используйте faster-whisper или whisper.cpp."),
        default_params={"beam_size": 5, "best_of": 5, "temperature": 0.0,
                        "condition_on_previous_text": False, "vad_enabled": True},
    ))

# faster-whisper (CTranslate2)
_FW = [
    ("tiny", "faster-whisper tiny", 39, 75, 1.0),
    ("base", "faster-whisper base", 74, 142, 1.0),
    ("small", "faster-whisper small", 244, 466, 1.5),
    ("medium", "faster-whisper medium", 769, 1500, 3.0),
    ("large-v2", "faster-whisper large-v2", 1550, 3100, 5.0),
    ("large-v3", "faster-whisper large-v3", 1550, 3100, 5.0),
    ("large-v3-turbo", "faster-whisper large-v3-turbo", 809, 1620, 3.5),
    ("distil-large-v3", "faster-whisper distil-large-v3", 756, 1510, 3.0),
]
for _sid, _name, _par, _disk, _vram in _FW:
    _en_only = _sid.startswith("distil")
    _bench = []
    if _sid == "large-v3":
        _bench = [B("Common Voice 17.0 ru", "WER", 9.84, SOURCES["whisper-ru"],
                    note="качество совпадает с оригиналом Whisper")]
    elif _sid == "distil-large-v3":
        _bench = [B("Short-form OOD (en)", "WER", 7.53, SOURCES["distil"], language="en")]
    _add(ModelSpec(
        id=f"faster-whisper-{_sid}",
        name=_name,
        family="Whisper",
        engine="faster_whisper",
        source=_sid,
        license="MIT",
        commercial_use=True,
        languages=["en"] if _en_only else _WHISPER_LANGS,
        ru_quality=Q.NONE if _en_only else (Q.FAIR if "large-v3" in _sid else Q.POOR),
        params_m=_par,
        disk_mb=_disk,
        vram_gb=_vram,
        ram_gb=max(2.0, _vram),
        timestamps=T.WORD,
        translation=not _en_only,
        rtfx=None,
        released="2024",
        benchmarks=_bench,
        tags=["мультиязычный", "быстрый", "ctranslate2", "mit",
              "батчинг", "int8"] + (["английский"] if _en_only else []),
        recommended_for=["Продуктивная транскрибация", "Пакетная обработка с батчингом",
                         "CPU-инференс с квантизацией int8"],
        strengths=[
            "В 2–4 раза быстрее оригинального Whisper при том же качестве",
            "Квантизация int8/float16/bfloat16 без переобучения",
            "Встроенный фильтр Silero VAD и пакетный конвейер",
            "На CPU (small, int8, batch 8): 51 с против 6 мин 58 с у openai-whisper",
        ],
        weaknesses=[
            "Требует согласованных версий CUDA и cuDNN — частый источник ошибок",
            "transcribe() возвращает ленивый генератор: ошибки возникают при итерации",
        ],
        notes=("Основной рекомендуемый движок для семейства Whisper. "
               "Для CUDA 12 + cuDNN 8 требуется ctranslate2==4.4.0, для cuDNN 9 — ctranslate2>=4.5."),
        default_params={"beam_size": 5, "compute_type": "auto", "vad_enabled": True,
                        "condition_on_previous_text": False, "batch_size": 8},
    ))

_add(ModelSpec(
    id="distil-large-v3.5",
    name="Distil-Whisper large-v3.5",
    family="Whisper",
    engine="faster_whisper",
    source="distil-whisper/distil-large-v3.5-ct2",
    license="MIT",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=756,
    disk_mb=1510,
    vram_gb=3.0,
    timestamps=T.WORD,
    released="2025-04",
    benchmarks=[
        B("Short-form OOD", "WER", 7.08, SOURCES["distil"], language="en"),
        B("Long-form OOD", "WER", 11.39, SOURCES["distil"], language="en"),
    ],
    tags=["английский", "дистилляция", "быстрый", "mit"],
    recommended_for=["Английские записи большого объёма", "Черновой прогон перед large-v3"],
    not_recommended_for=["Любые неанглийские записи"],
    strengths=["В 1.5 раза быстрее large-v3-turbo на длинном аудио",
               "Обучена на 98 000 часов, лучше прежних дистилляций"],
    weaknesses=["Только английский язык"],
    notes="Авторы рекомендуют обязательно ставить condition_on_previous_text=False.",
    default_params={"condition_on_previous_text": False, "beam_size": 5},
))

# whisper.cpp — GGML-сборки
_WCPP = [
    ("tiny", "whisper.cpp tiny", 75), ("base", "whisper.cpp base", 142),
    ("small", "whisper.cpp small", 466), ("medium", "whisper.cpp medium", 1500),
    ("large-v3", "whisper.cpp large-v3", 2900),
    ("large-v3-q5_0", "whisper.cpp large-v3 q5_0", 1080),
    ("large-v3-turbo", "whisper.cpp large-v3-turbo", 1620),
    ("large-v3-turbo-q5_0", "whisper.cpp large-v3-turbo q5_0", 574),
    ("large-v3-turbo-q8_0", "whisper.cpp large-v3-turbo q8_0", 874),
]
for _sid, _name, _disk in _WCPP:
    _q = "-q" in _sid or "q5" in _sid or "q8" in _sid
    _add(ModelSpec(
        id=f"whispercpp-{_sid}",
        name=_name,
        family="Whisper",
        engine="whisper_cpp",
        source=_sid,
        license="MIT",
        commercial_use=True,
        languages=_WHISPER_LANGS,
        ru_quality=Q.FAIR if "large-v3" in _sid else Q.POOR,
        disk_mb=_disk,
        ram_gb=max(1.0, _disk / 800),
        timestamps=T.WORD,
        translation=True,
        released="2024",
        tags=["мультиязычный", "cpu", "apple-silicon", "ggml", "mit"] + (["квантованная"] if _q else []),
        recommended_for=["Apple Silicon", "Серверы без GPU", "Встраивание без Python"],
        strengths=[
            "Работа на Metal, CoreML, CUDA, Vulkan, ROCm, OpenVINO",
            "Core ML переносит энкодер на Neural Engine — ускорение более чем втрое",
            "Один бинарник без зависимостей Python",
        ] + (["Квантизация уменьшает файл в 2–5 раз при небольшой потере качества"] if _q else []),
        weaknesses=["Требуется компиляция под конкретную платформу",
                    "Параметры VAD по умолчанию отличаются от faster-whisper"],
        notes="Модели скачиваются скриптом models/download-ggml-model.sh из репозитория whisper.cpp.",
        default_params={"beam_size": 5, "threads": 0, "flash_attn": True},
    ))

_add(ModelSpec(
    id="whisperx-large-v3",
    name="WhisperX large-v3 (выравнивание + диаризация)",
    family="Whisper",
    engine="whisperx",
    source="large-v3",
    license="BSD-2-Clause",
    commercial_use=True,
    languages=_WHISPER_LANGS,
    ru_quality=Q.FAIR,
    params_m=1550,
    disk_mb=3100,
    vram_gb=8.0,
    timestamps=T.WORD,
    diarization=True,
    released="2026-05",
    gated=True,
    benchmarks=[B("Common Voice 17.0 ru", "WER", 9.84, SOURCES["whisper-ru"],
                  note="качество ASR совпадает с large-v3")],
    tags=["диаризация", "выравнивание", "мультиязычный", "bsd"],
    recommended_for=["Записи с несколькими говорящими", "Точные пословные таймкоды", "Субтитры"],
    strengths=[
        "Форсированное выравнивание точнее встроенных таймкодов Whisper",
        "Диаризация через pyannote, до 70-кратной скорости реального времени",
        "VAD-препроцессинг снижает галлюцинации без потери качества",
    ],
    weaknesses=[
        "Для диаризации нужен токен Hugging Face и принятие лицензии pyannote",
        "Жёсткий пин torch~=2.8 — требуется изолированное окружение",
        "Для русского нужно явно указывать модель выравнивания",
    ],
    notes=("Модели pyannote распространяются по лицензии CC-BY-4.0, отличной от BSD-2 самого WhisperX. "
           "Русская модель выравнивания по умолчанию — jonatasgrosman/wav2vec2-large-xlsr-53-russian."),
    default_params={"batch_size": 16, "compute_type": "float16", "diarize": True,
                    "align_model": "jonatasgrosman/wav2vec2-large-xlsr-53-russian"},
))

# ---------------------------------------------------------------------------
# NVIDIA NeMo — parakeet, canary, nemotron
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="parakeet-tdt-0.6b-v3",
    name="NVIDIA Parakeet TDT 0.6B v3",
    family="NeMo",
    engine="nemo",
    source="nvidia/parakeet-tdt-0.6b-v3",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=_EU25,
    ru_quality=Q.GOOD,
    params_m=600,
    disk_mb=2400,
    vram_gb=2.0,
    timestamps=T.WORD,
    punctuation=True,
    max_audio_s=1440,
    rtfx=3332.74,
    rtfx_hw="A100, batch 128",
    released="2025-08-14",
    benchmarks=[
        B("FLEURS ru", "WER", 5.51, SOURCES["parakeet-v3"]),
        B("CoVoST2 ru", "WER", 3.00, SOURCES["parakeet-v3"]),
        B("FLEURS среднее по 25 языкам", "WER", 11.97, SOURCES["parakeet-v3"], language="multi"),
        B("Open ASR Leaderboard (en)", "WER", 6.34, SOURCES["parakeet-v3"], language="en"),
        B("Long-form трек", "WER", 10.7, SOURCES["openasr-arxiv"], language="en"),
    ],
    tags=["мультиязычный", "25 языков", "очень быстрый", "пунктуация", "cc-by"],
    recommended_for=[
        "Массовая пакетная транскрибация",
        "Мультиязычные архивы с русским и украинским",
        "Максимальная пропускная способность на GPU",
    ],
    strengths=[
        "RTFx 3332 — быстрее Whisper large-v3 более чем в 20 раз",
        "Автоопределение языка без подсказки",
        "Пунктуация, капитализация и пословные таймкоды из коробки",
        "До 24 минут аудио за один проход, до 3 часов с локальным вниманием",
    ],
    weaknesses=[
        "Только 25 европейских языков",
        "Тянет тяжёлый стек NeMo с конфликтными зависимостями",
        "Требуется атрибуция по условиям CC-BY-4.0",
    ],
    notes="Для продакшена без NeMo используйте ONNX-экспорт или sherpa-onnx.",
    default_params={"batch_size": 16, "timestamps": True},
))

_add(ModelSpec(
    id="parakeet-tdt-0.6b-v2",
    name="NVIDIA Parakeet TDT 0.6B v2 (английский)",
    family="NeMo",
    engine="nemo",
    source="nvidia/parakeet-tdt-0.6b-v2",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=600,
    disk_mb=2400,
    vram_gb=2.0,
    timestamps=T.WORD,
    punctuation=True,
    max_audio_s=1440,
    rtfx=3386.02,
    rtfx_hw="A100, batch 128",
    released="2025-05-01",
    benchmarks=[B("Open ASR Leaderboard", "WER", 6.05, SOURCES["parakeet-v2"], language="en")],
    tags=["английский", "очень быстрый", "cc-by"],
    recommended_for=["Английские конвейеры с высокой нагрузкой"],
    strengths=["Рекордная пропускная способность"],
    weaknesses=["Только английский — для остальных языков берите v3"],
    default_params={"batch_size": 32, "timestamps": True},
))

_add(ModelSpec(
    id="canary-1b-v2",
    name="NVIDIA Canary 1B v2 (ASR + перевод)",
    family="NeMo",
    engine="nemo",
    source="nvidia/canary-1b-v2",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=_EU25,
    ru_quality=Q.GOOD,
    params_m=978,
    disk_mb=3900,
    vram_gb=6.0,
    timestamps=T.WORD,
    punctuation=True,
    translation=True,
    max_audio_s=40,
    rtfx=749.0,
    rtfx_hw="A100",
    released="2025-08-14",
    benchmarks=[B("Open ASR Leaderboard", "WER", 7.15, SOURCES["canary-v2"], language="en")],
    tags=["мультиязычный", "перевод", "25 языков", "cc-by"],
    recommended_for=["Перевод речи в текст другого языка", "Мультиязычные совещания"],
    not_recommended_for=["Чистая транскрибация — parakeet v3 быстрее и точнее"],
    strengths=["Перевод в обе стороны между английским и 24 языками",
               "Автоматическая нарезка длинного аудио с параллельной обработкой"],
    weaknesses=["Один проход ограничен 40 секундами", "Требует минимум 6 ГБ видеопамяти"],
    default_params={"batch_size": 8, "task": "asr", "timestamps": True},
))

_add(ModelSpec(
    id="canary-1b-flash",
    name="NVIDIA Canary 1B Flash",
    family="NeMo",
    engine="nemo",
    source="nvidia/canary-1b-flash",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en", "de", "fr", "es"],
    ru_quality=Q.NONE,
    params_m=883,
    disk_mb=3500,
    vram_gb=5.0,
    timestamps=T.WORD,
    punctuation=True,
    translation=True,
    max_audio_s=40,
    rtfx=1045.75,
    rtfx_hw="A100",
    released="2025",
    benchmarks=[
        B("Open ASR Leaderboard", "WER", 6.35, SOURCES["canary-flash"], language="en"),
        B("LibriSpeech clean", "WER", 1.48, SOURCES["canary-flash"], language="en"),
    ],
    tags=["четыре языка", "быстрый", "перевод", "cc-by"],
    recommended_for=["Английский, немецкий, французский, испанский"],
    strengths=["RTFx 1669 на H100", "Работает на Jetson"],
    weaknesses=["Нет русского языка"],
    default_params={"batch_size": 16, "task": "asr"},
))

_add(ModelSpec(
    id="canary-180m-flash",
    name="NVIDIA Canary 180M Flash",
    family="NeMo",
    engine="nemo",
    source="nvidia/canary-180m-flash",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en", "de", "fr", "es"],
    ru_quality=Q.NONE,
    params_m=182,
    disk_mb=730,
    vram_gb=1.5,
    timestamps=T.WORD,
    punctuation=True,
    translation=True,
    max_audio_s=40,
    rtfx=1233.0,
    rtfx_hw="A100",
    released="2025",
    benchmarks=[
        B("LibriSpeech clean", "WER", 1.87, SOURCES["canary-180m"], language="en"),
        B("Common Voice en", "WER", 9.53, SOURCES["canary-180m"], language="en"),
        B("Точность таймкодов (F1)", "WER", 93.48, SOURCES["canary-180m"], language="en",
          note="F1 таймкодов на LibriSpeech test-clean, не WER"),
    ],
    tags=["лёгкая", "edge", "четыре языка", "cc-by"],
    recommended_for=["Jetson и другие edge-устройства", "Форсированное выравнивание"],
    strengths=["182 млн параметров при качестве, близком к Whisper medium"],
    weaknesses=["Нет русского языка"],
    default_params={"batch_size": 32, "task": "asr"},
))

_add(ModelSpec(
    id="canary-qwen-2.5b",
    name="NVIDIA Canary-Qwen 2.5B",
    family="NeMo",
    engine="nemo",
    source="nvidia/canary-qwen-2.5b",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=2500,
    disk_mb=10000,
    vram_gb=12.0,
    timestamps=T.SEGMENT,
    punctuation=True,
    max_audio_s=40,
    rtfx=418.0,
    rtfx_hw="A100",
    released="2025",
    benchmarks=[B("Open ASR Leaderboard", "WER", 5.63, SOURCES["canary-qwen"], language="en")],
    tags=["английский", "llm", "суммаризация", "cc-by"],
    recommended_for=["Транскрипт и краткое содержание одной моделью"],
    strengths=["Два режима: чистое ASR и работа как LLM над транскриптом"],
    weaknesses=["Только английский, окно 40 секунд, 12 ГБ видеопамяти"],
    default_params={"batch_size": 4, "mode": "asr"},
))

_add(ModelSpec(
    id="nemotron-asr-streaming-0.6b",
    name="NVIDIA Nemotron 3.5 ASR Streaming 0.6B",
    family="NeMo",
    engine="nemo",
    source="nvidia/nemotron-3.5-asr-streaming-0.6b",
    license="OpenMDW-1.1",
    commercial_use=True,
    languages=["en", "es", "fr", "it", "pt", "nl", "de", "tr", "ru", "ar", "hi",
               "ja", "ko", "vi", "uk", "pl", "sv", "cs", "nb", "da", "bg", "fi",
               "hr", "sk", "zh", "hu", "ro", "et"],
    ru_quality=Q.GOOD,
    params_m=600,
    disk_mb=2400,
    vram_gb=3.0,
    streaming=True,
    timestamps=T.WORD,
    punctuation=True,
    released="2026-06-04",
    maturity=M.NEW,
    benchmarks=[
        B("FLEURS ru", "WER", 9.17, SOURCES["nemotron-stream"], note="задержка 1.12 с"),
        B("FLEURS en", "WER", 7.91, SOURCES["nemotron-stream"], language="en"),
        B("FLEURS es", "WER", 4.11, SOURCES["nemotron-stream"], language="es"),
    ],
    tags=["стриминг", "40 локалей", "реальное время", "openmdw"],
    recommended_for=["Голосовые агенты", "Живые субтитры", "Реальное время с русским"],
    strengths=[
        "Настраиваемая задержка: 80 / 160 / 320 / 560 / 1120 мс",
        "На одной H100 держит примерно в 17 раз больше потоков, чем Parakeet RNNT 1.1B",
        "40 локалей, русский в верхнем уровне поддержки",
    ],
    weaknesses=[
        "Лицензия OpenMDW-1.1 пока без валидного идентификатора SPDX",
        "Инференс на CPU не заявлен",
    ],
    notes="Пермиссивная лицензия Linux Foundation: коммерческое использование разрешено.",
    default_params={"latency_ms": 560, "batch_size": 8},
))

_add(ModelSpec(
    id="sortformer-diar-4spk-v2",
    name="NVIDIA Streaming Sortformer 4 спикера",
    family="NeMo",
    engine="nemo",
    source="nvidia/diar_streaming_sortformer_4spk-v2",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=117,
    disk_mb=470,
    vram_gb=2.0,
    streaming=True,
    timestamps=T.SEGMENT,
    diarization=True,
    released="2025-07",
    benchmarks=[
        B("DIHARD III Eval (1–4 спикера)", "DER", 13.24, SOURCES["sortformer"], language="en"),
        B("CALLHOME (2 спикера)", "DER", 6.57, SOURCES["sortformer"], language="en"),
    ],
    tags=["диаризация", "стриминг", "cc-by"],
    recommended_for=["Потоковая диаризация до 4 говорящих"],
    not_recommended_for=["Записи с пятью и более говорящими"],
    strengths=["Задержка от 0.32 секунды"],
    weaknesses=["При 5 и более говорящих DER вырастает до 42 %", "Оптимизирована под английский"],
    notes="Вспомогательная модель диаризации, подключается как этап конвейера.",
))

# ---------------------------------------------------------------------------
# Qwen3-ASR (Alibaba)
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="qwen3-asr-1.7b",
    name="Qwen3-ASR 1.7B",
    family="Qwen",
    engine="qwen3_asr",
    source="Qwen/Qwen3-ASR-1.7B",
    license="Apache-2.0",
    commercial_use=True,
    languages=["zh", "en", "yue", "ar", "de", "fr", "es", "pt", "id", "it", "ko",
               "ru", "th", "vi", "ja", "tr", "hi", "ms", "nl", "sv", "da", "fi",
               "pl", "cs", "fil", "fa", "el", "hu", "mk", "ro"],
    ru_quality=Q.GOOD,
    params_m=2000,
    disk_mb=4000,
    vram_gb=8.0,
    streaming=True,
    timestamps=T.SEGMENT,
    punctuation=True,
    rtfx=148.0,
    rtfx_hw="Open ASR Leaderboard",
    released="2026-01-29",
    maturity=M.NEW,
    benchmarks=[
        B("Open ASR Leaderboard (en)", "WER", 5.76, SOURCES["qwen3asr"], language="en"),
        B("LibriSpeech clean", "WER", 1.63, SOURCES["qwen3asr"], language="en"),
        B("FLEURS zh", "WER", 2.41, SOURCES["qwen3asr"], language="zh"),
    ],
    tags=["мультиязычный", "30 языков", "apache", "vllm"],
    recommended_for=["Мультиязычные записи с чистой лицензией Apache-2.0",
                     "Развёртывание через vLLM", "Записи с музыкой и пением"],
    strengths=[
        "Apache-2.0 — самая чистая лицензия среди топовых моделей",
        "30 языков и 22 китайских диалекта, русский поддерживается",
        "Распознаёт пение и речь на фоне музыки",
        "Поддержка vLLM с первого дня",
    ],
    weaknesses=["Отдельных опубликованных метрик по русскому нет",
                "Таймкоды выдаёт отдельная модель Qwen3-ForcedAligner"],
    default_params={"batch_size": 8, "language": "auto"},
))

_add(ModelSpec(
    id="qwen3-asr-0.6b",
    name="Qwen3-ASR 0.6B",
    family="Qwen",
    engine="qwen3_asr",
    source="Qwen/Qwen3-ASR-0.6B",
    license="Apache-2.0",
    commercial_use=True,
    languages=["zh", "en", "ru", "de", "fr", "es", "pt", "it", "ja", "ko", "ar", "hi"],
    ru_quality=Q.GOOD,
    params_m=900,
    disk_mb=1800,
    vram_gb=4.0,
    streaming=True,
    timestamps=T.SEGMENT,
    punctuation=True,
    released="2026-01-29",
    maturity=M.NEW,
    benchmarks=[
        B("LibriSpeech clean", "WER", 2.11, SOURCES["qwen3asr"], language="en"),
        B("FLEURS среднее", "WER", 3.48, SOURCES["qwen3asr"], language="multi"),
    ],
    tags=["мультиязычный", "лёгкая", "apache", "vllm"],
    recommended_for=["Мультиязычность на скромном GPU"],
    strengths=["Хорошее соотношение качества и размера"],
    weaknesses=["Уступает версии 1.7B"],
    default_params={"batch_size": 16, "language": "auto"},
))

# ---------------------------------------------------------------------------
# MOSS / Cohere / Granite / Voxtral / Kyutai / Omnilingual / прочее
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="moss-transcribe-diarize",
    name="MOSS Transcribe + Diarize",
    family="MOSS",
    engine="transformers",
    source="OpenMOSS-Team/MOSS-Transcribe-Diarize",
    license="Apache-2.0",
    commercial_use=True,
    languages=["multi-50", "ru", "en", "zh"],
    ru_quality=Q.GOOD,
    params_m=900,
    disk_mb=3600,
    vram_gb=8.0,
    timestamps=T.SEGMENT,
    punctuation=True,
    diarization=True,
    max_audio_s=5400,
    rtfx=294.02,
    rtfx_hw="Open ASR Leaderboard",
    released="2026-07-09",
    maturity=M.NEW,
    benchmarks=[B("Open ASR Leaderboard (en)", "WER", 5.17, SOURCES["moss"], language="en")],
    tags=["диаризация", "мультиязычный", "50 языков", "apache"],
    recommended_for=["Совещания с несколькими говорящими", "Подкасты и интервью",
                     "Когда нужна диаризация без отдельного pyannote"],
    strengths=[
        "ASR и диаризация в одной модели — метки [S01], [S02] прямо в транскрипте",
        "Контекст до 90 минут аудио за один проход",
        "50+ языков, включая русский, лицензия Apache-2.0",
    ],
    weaknesses=["Выпущена в июле 2026, мало полевого опыта",
                "Отдельных метрик по русскому нет"],
    default_params={"batch_size": 4, "diarize": True},
))

_add(ModelSpec(
    id="cohere-transcribe-2026-03",
    name="Cohere Labs Transcribe",
    family="Cohere",
    engine="transformers",
    source="CohereLabs/cohere-transcribe-03-2026",
    license="Apache-2.0",
    commercial_use=True,
    languages=["en", "fr", "de", "it", "es", "pt", "el", "nl", "pl", "zh", "ja", "ko", "vi", "ar"],
    ru_quality=Q.NONE,
    params_m=2000,
    disk_mb=8000,
    vram_gb=8.0,
    timestamps=T.NONE,
    punctuation=True,
    rtfx=524.88,
    rtfx_hw="Open ASR Leaderboard",
    released="2026-03-26",
    maturity=M.NEW,
    benchmarks=[
        B("Open ASR Leaderboard (en)", "WER", 5.42, SOURCES["cohere"], language="en"),
        B("Мультиязычный трек", "WER", 3.83, SOURCES["openasr-arxiv"], language="multi"),
    ],
    tags=["мультиязычный", "14 языков", "apache"],
    recommended_for=["Европейские и азиатские языки, кроме русского"],
    not_recommended_for=["Русский язык — не поддерживается"],
    strengths=["Отличное сочетание точности и скорости"],
    weaknesses=["Нет русского, нет таймкодов, нет автоопределения языка",
                "Склонна «расшифровывать» неречевые звуки"],
    default_params={"batch_size": 8},
))

_add(ModelSpec(
    id="granite-speech-4.1-2b",
    name="IBM Granite Speech 4.1 2B",
    family="Granite",
    engine="transformers",
    source="ibm-granite/granite-speech-4.1-2b",
    license="Apache-2.0",
    commercial_use=True,
    languages=["en", "fr", "de", "es", "pt", "ja"],
    ru_quality=Q.NONE,
    params_m=2000,
    disk_mb=8000,
    vram_gb=8.0,
    timestamps=T.SEGMENT,
    punctuation=True,
    translation=True,
    rtfx=231.29,
    rtfx_hw="Open ASR Leaderboard",
    released="2026-04-29",
    maturity=M.NEW,
    benchmarks=[B("Open ASR Leaderboard (en)", "WER", 5.33, SOURCES["granite"], language="en")],
    tags=["шесть языков", "apache", "ключевые слова"],
    recommended_for=["Английский с подсказками по терминологии"],
    not_recommended_for=["Русский язык — не поддерживается"],
    strengths=["Смещение распознавания по списку ключевых слов", "Перевод в обе стороны"],
    weaknesses=["Нет русского"],
    default_params={"batch_size": 4},
))

_add(ModelSpec(
    id="voxtral-mini-4b-realtime",
    name="Mistral Voxtral Mini 4B Realtime",
    family="Voxtral",
    engine="voxtral",
    source="mistralai/Voxtral-Mini-4B-Realtime-2602",
    license="Apache-2.0",
    commercial_use=True,
    languages=["ar", "de", "en", "es", "fr", "hi", "it", "nl", "pt", "zh", "ja", "ko", "ru"],
    ru_quality=Q.FAIR,
    params_m=4000,
    disk_mb=8000,
    vram_gb=16.0,
    streaming=True,
    timestamps=T.SEGMENT,
    punctuation=True,
    rtfx=93.32,
    rtfx_hw="Open ASR Leaderboard",
    released="2026-02",
    maturity=M.NEW,
    benchmarks=[B("Среднее по мультиязычным наборам", "WER", 8.72, SOURCES["voxtral-rt"],
                  language="multi", note="при задержке 480 мс")],
    tags=["стриминг", "13 языков", "apache", "vllm"],
    recommended_for=["Потоковое распознавание с настраиваемой задержкой", "Голосовые агенты"],
    strengths=["Задержка настраивается от 80 мс до 2.4 с шагом 80 мс",
               "Нативно потоковая архитектура с каузальным аудиоэнкодером"],
    weaknesses=["Требует минимум 16 ГБ видеопамяти"],
    default_params={"latency_ms": 480},
))

_add(ModelSpec(
    id="voxtral-mini-3b",
    name="Mistral Voxtral Mini 3B",
    family="Voxtral",
    engine="voxtral",
    source="mistralai/Voxtral-Mini-3B-2507",
    license="Apache-2.0",
    commercial_use=True,
    languages=["en", "es", "fr", "pt", "hi", "de", "nl", "it"],
    ru_quality=Q.NONE,
    params_m=3000,
    disk_mb=9500,
    vram_gb=9.5,
    timestamps=T.SEGMENT,
    punctuation=True,
    max_audio_s=1800,
    released="2025-07",
    benchmarks=[B("Среднее (FLEURS + CV + MLS)", "WER", 7.05, SOURCES["voxtral"], language="multi")],
    tags=["восемь языков", "аудио-llm", "apache"],
    recommended_for=["Транскрипт с ответами на вопросы по аудио"],
    not_recommended_for=["Русский язык — не поддерживается"],
    strengths=["Контекст 32k токенов: до 30 минут транскрипции", "Встроенные QA и суммаризация"],
    weaknesses=["Нет русского"],
    default_params={},
))

_add(ModelSpec(
    id="kyutai-stt-1b-en-fr",
    name="Kyutai STT 1B (en/fr)",
    family="Kyutai",
    engine="kyutai",
    source="kyutai/stt-1b-en_fr",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["en", "fr"],
    ru_quality=Q.NONE,
    params_m=1000,
    disk_mb=4000,
    vram_gb=4.0,
    streaming=True,
    timestamps=T.SEGMENT,
    released="2025",
    benchmarks=[B("Open ASR Leaderboard", "WER", 6.40, SOURCES["kyutai"], language="en",
                  note="вторичный источник")],
    tags=["стриминг", "низкая задержка", "cc-by"],
    recommended_for=["Английский и французский в реальном времени"],
    not_recommended_for=["Русский язык — не поддерживается"],
    strengths=["Задержка 0.5 с", "Встроенный семантический VAD"],
    weaknesses=["Всего два языка"],
    default_params={},
))

_add(ModelSpec(
    id="omnilingual-ctc-1b",
    name="Meta Omnilingual ASR CTC 1B",
    family="Omnilingual",
    engine="omnilingual",
    source="facebook/omnilingual-asr-ctc-1b",
    license="Apache-2.0",
    commercial_use=True,
    languages=["multi-1600", "ru"],
    ru_quality=Q.FAIR,
    params_m=1000,
    disk_mb=4000,
    vram_gb=4.0,
    timestamps=T.SEGMENT,
    released="2025-11",
    maturity=M.NEW,
    benchmarks=[B("Мультиязычный трек (7B v2)", "WER", 5.84, SOURCES["openasr-arxiv"],
                  language="multi", note="метрика для варианта 7B")],
    tags=["1600 языков", "редкие языки", "apache"],
    recommended_for=["Редкие языки", "Лингвистические и архивные проекты"],
    not_recommended_for=["Мейнстримные языки — parakeet и GigaAM точнее"],
    strengths=["1600+ языков нативно и 5400+ в режиме zero-shot",
               "Apache-2.0 и на код, и на веса — свободная замена Meta MMS"],
    weaknesses=["Отдельных метрик по русскому нет"],
    notes="Код русского языка в этой модели — rus_Cyrl.",
    default_params={"lang_code": "rus_Cyrl"},
))

_add(ModelSpec(
    id="sensevoice-small",
    name="FunASR SenseVoice Small",
    family="FunASR",
    engine="funasr",
    source="FunAudioLLM/SenseVoiceSmall",
    license="Alibaba model-license",
    commercial_use=True,
    languages=["zh", "yue", "en", "ja", "ko"],
    ru_quality=Q.NONE,
    disk_mb=250,
    ram_gb=2.0,
    timestamps=T.SEGMENT,
    punctuation=True,
    emotion=True,
    released="2024",
    benchmarks=[B("Средний CER", "CER", 7.81, SOURCES["sensevoice"], language="multi")],
    tags=["cpu", "эмоции", "аудиособытия", "азиатские языки"],
    recommended_for=["Китайский, японский, корейский", "CPU-инференс",
                     "Аналитика эмоций и аудиособытий"],
    not_recommended_for=["Русский язык — не заявлен"],
    strengths=["70 мс на 10 секунд аудио — в 15 раз быстрее Whisper Large",
               "Одновременно выдаёт язык, эмоцию и аудиособытия"],
    weaknesses=["Нестандартная лицензия Alibaba — нужна юридическая проверка",
                "Русский не заявлен"],
    notes="Лицензия не одобрена OSI: формально разрешает коммерческое использование, но требует проверки.",
    default_params={},
))

_add(ModelSpec(
    id="moonshine-base",
    name="Moonshine base",
    family="Moonshine",
    engine="moonshine",
    source="UsefulSensors/moonshine-base",
    license="MIT",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=61,
    disk_mb=240,
    ram_gb=1.0,
    timestamps=T.SEGMENT,
    released="2024-10",
    tags=["edge", "английский", "onnx", "mit"],
    recommended_for=["Микроконтроллеры и браузер", "Короткие голосовые команды"],
    not_recommended_for=["Длинные записи", "Любые неанглийские записи"],
    strengths=["Обрабатывает аудио пропорционально длине без дополнения до 30 секунд",
               "61 млн параметров, официальный экспорт в ONNX"],
    weaknesses=["Склонность к повторам на коротких сегментах", "Только английский"],
    default_params={},
))

_add(ModelSpec(
    id="moonshine-tiny",
    name="Moonshine tiny",
    family="Moonshine",
    engine="moonshine",
    source="UsefulSensors/moonshine-tiny",
    license="MIT",
    commercial_use=True,
    languages=["en"],
    ru_quality=Q.NONE,
    params_m=27,
    disk_mb=110,
    ram_gb=0.5,
    timestamps=T.SEGMENT,
    released="2024-10",
    tags=["edge", "английский", "onnx", "mit"],
    recommended_for=["Активация по ключевому слову", "Сверхлёгкие устройства"],
    strengths=["27 млн параметров"],
    weaknesses=["Только английский, качество ограничено"],
    default_params={},
))

_add(ModelSpec(
    id="owsm-ctc-v4-1b",
    name="ESPnet OWSM CTC v4 1B",
    family="ESPnet",
    engine="transformers",
    source="espnet/owsm_ctc_v4_1B",
    license="CC-BY-4.0",
    commercial_use=True,
    languages=["multi-150"],
    ru_quality=Q.FAIR,
    params_m=1010,
    disk_mb=4000,
    vram_gb=4.0,
    timestamps=T.SEGMENT,
    rtfx=453.97,
    rtfx_hw="Open ASR Leaderboard",
    released="2025",
    benchmarks=[B("Open ASR Leaderboard (en)", "WER", 7.42, SOURCES["owsm"], language="en")],
    tags=["воспроизводимость", "исследования", "cc-by"],
    recommended_for=["Исследования с полностью открытыми данными и рецептами обучения"],
    strengths=["Полностью открытые данные, код и рецепты — можно повторить обучение"],
    weaknesses=["Поддержка русского в карточке модели явно не подтверждена"],
    default_params={},
))

_add(ModelSpec(
    id="phi4-multimodal",
    name="Microsoft Phi-4 multimodal",
    family="Phi",
    engine="transformers",
    source="microsoft/Phi-4-multimodal-instruct",
    license="MIT",
    commercial_use=True,
    languages=["en", "zh", "de", "fr", "it", "ja", "es", "pt"],
    ru_quality=Q.NONE,
    params_m=5600,
    disk_mb=11000,
    vram_gb=16.0,
    timestamps=T.NONE,
    punctuation=True,
    max_audio_s=40,
    released="2025-03",
    benchmarks=[B("Open ASR Leaderboard", "WER", 6.02, SOURCES["phi4"], language="en")],
    tags=["мультимодальная", "llm", "mit"],
    recommended_for=["Когда нужны и распознавание, и анализ содержимого"],
    not_recommended_for=["Чистая транскрибация — избыточна по ресурсам"],
    strengths=["Универсальная мультимодальная модель"],
    weaknesses=["Нет русского, требует A100 или новее"],
    default_params={},
))

# ---------------------------------------------------------------------------
# Отладочный движок — работает без установки тяжёлых зависимостей
# ---------------------------------------------------------------------------

_add(ModelSpec(
    id="demo-simulator",
    name="Демонстрационный симулятор",
    family="Demo",
    engine="demo",
    source="builtin",
    license="MIT",
    commercial_use=True,
    languages=["ru", "en"],
    ru_quality=Q.NONE,
    params_m=0,
    disk_mb=0,
    vram_gb=0,
    timestamps=T.WORD,
    punctuation=True,
    released="2026",
    tags=["отладка", "тесты", "без зависимостей"],
    recommended_for=["Проверка установки", "Нагрузочное тестирование очереди",
                     "Демонстрация интерфейса без моделей"],
    not_recommended_for=["Реальное распознавание"],
    strengths=["Не требует ни одной внешней зависимости",
               "Позволяет проверить весь конвейер, очередь и аналитику"],
    weaknesses=["Не выполняет настоящее распознавание"],
    notes="Возвращает синтетический транскрипт; используется для тестов и первичной проверки.",
    default_params={"simulated_rtf": 0.15},
))


# ---------------------------------------------------------------------------
# Модели, сознательно исключённые из каталога
# ---------------------------------------------------------------------------

EXCLUDED_MODELS: list[dict[str, str]] = [
    {"name": "Meta MMS (facebook/mms-1b-all)", "license": "CC-BY-NC-4.0",
     "reason": "Некоммерческая лицензия. Полностью заменяется Omnilingual ASR (Apache-2.0), "
               "которая и точнее, и свободнее."},
    {"name": "Meta SeamlessM4T v2", "license": "CC-BY-NC-4.0",
     "reason": "Некоммерческая лицензия. Для перевода речи используйте Canary 1B v2 (CC-BY-4.0)."},
    {"name": "Silero STT (русский)", "license": "CC-NC-BY / AGPL-3.0",
     "reason": "Русские веса STT удалены из открытого репозитория, оставшиеся модели "
               "распространяются по некоммерческой лицензии. Silero VAD (MIT) при этом остаётся."},
    {"name": "NVIDIA Riva", "license": "NVIDIA AI Enterprise (платная)",
     "reason": "Не открытое ПО: продуктивное развёртывание требует платной лицензии."},
    {"name": "nvidia/parakeet-unified-en-0.6b", "license": "NVIDIA Open Model License",
     "reason": "Лицензия отличается от CC-BY-4.0 и требует отдельной юридической проверки."},
    {"name": "Coqui STT", "license": "MPL-2.0",
     "reason": "Проект прекращён вместе с закрытием компании; развития нет."},
    {"name": "Kaldi (классический)", "license": "Apache-2.0",
     "reason": "Устарел, активная разработка перешла в k2 / icefall / sherpa."},
    {"name": "Qwen2-Audio-7B-Instruct", "license": "Apache-2.0",
     "reason": "Вытеснена специализированной Qwen3-ASR: точнее и легче."},
    {"name": "whisper-timestamped", "license": "GPL-3.0",
     "reason": "Вирусная лицензия несовместима с проприетарным использованием; "
               "функциональность покрыта встроенными пословными таймкодами и WhisperX."},
    {"name": "insanely-fast-whisper / whisper-jax", "license": "MIT / Apache-2.0",
     "reason": "Проекты заброшены; их возможности покрывает transformers и faster-whisper."},
    {"name": "AssemblyAI, Deepgram, ElevenLabs Scribe, Zoom Scribe", "license": "проприетарные API",
     "reason": "Закрытые облачные сервисы. Приведены в документации только как ориентир качества."},
]


# ---------------------------------------------------------------------------
# Индексы и утилиты доступа
# ---------------------------------------------------------------------------

MODELS_BY_ID: dict[str, ModelSpec] = {m.id: m for m in MODELS}

for _m in MODELS:
    for _al in _m.aliases:
        MODELS_BY_ID.setdefault(_al, _m)


def get_model(model_id: str) -> ModelSpec | None:
    return MODELS_BY_ID.get(model_id)


def suggest_models(query: str, limit: int = 5) -> list[str]:
    """Похожие идентификаторы моделей — для сообщений об ошибках.

    Сначала точные вхождения подстроки, затем нечёткое совпадение по
    Левенштейну и по общему префиксу семейства: пользователь, набравший
    «gigaam-v99», должен увидеть реально существующие варианты GigaAM.
    """
    import difflib

    query = (query or "").strip().lower()
    if not query:
        return []
    ids = [m.id for m in MODELS]

    exact = [i for i in ids if query in i.lower()]
    if len(exact) >= limit:
        return exact[:limit]

    close = difflib.get_close_matches(query, ids, n=limit, cutoff=0.45)

    prefix = query.split("-")[0]
    same_family = [i for i in ids if i.lower().startswith(prefix)] if len(prefix) >= 3 else []

    out: list[str] = []
    for candidate in exact + close + same_family:
        if candidate not in out:
            out.append(candidate)
        if len(out) >= limit:
            break
    return out


def models_for_engine(engine_id: str) -> list[ModelSpec]:
    return [m for m in MODELS if m.engine == engine_id]


def models_for_language(lang: str) -> list[ModelSpec]:
    lang = lang.lower()
    out = []
    for m in MODELS:
        langs = [x.lower() for x in m.languages]
        if lang in langs or any(x.startswith("multi") for x in langs):
            out.append(m)
    return out


def mean_ru_wer(spec: ModelSpec) -> float | None:
    """Средний WER по всем русским наборам — устойчивее одиночного лучшего значения."""
    vals = [b.value for b in spec.benchmarks
            if b.language == "ru" and b.metric == "WER" and "Среднее" not in b.dataset]
    agg = [b.value for b in spec.benchmarks
           if b.language == "ru" and b.metric == "WER" and "Среднее" in b.dataset]
    if agg:
        return round(sum(agg) / len(agg), 2)
    return round(sum(vals) / len(vals), 2) if vals else None


def recommended_ru(limit: int = 5) -> list[ModelSpec]:
    """Модели, пригодные для русского языка, по возрастанию среднего WER."""
    ranked = [m for m in MODELS if m.ru_quality in (Q.EXCELLENT, Q.GOOD)]
    ranked.sort(key=lambda m: (mean_ru_wer(m) if mean_ru_wer(m) is not None else 99.0))
    return ranked[:limit]


def catalog_summary() -> dict[str, object]:
    families: dict[str, int] = {}
    licenses: dict[str, int] = {}
    for m in MODELS:
        families[m.family] = families.get(m.family, 0) + 1
        licenses[m.license] = licenses.get(m.license, 0) + 1
    return {
        "date": CATALOG_DATE,
        "total": len(MODELS),
        "families": families,
        "licenses": licenses,
        "streaming": sum(1 for m in MODELS if m.streaming),
        "diarization": sum(1 for m in MODELS if m.diarization),
        "russian": sum(1 for m in MODELS if m.ru_quality != Q.NONE),
        "excluded": len(EXCLUDED_MODELS),
    }
