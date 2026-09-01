"""Каталог движков — программных обвязок, исполняющих модели."""
from __future__ import annotations

from .schema import EngineSpec

ENGINES: list[EngineSpec] = [
    EngineSpec(
        id="gigaam",
        name="GigaAM",
        description=(
            "Официальная реализация SaluteDevices. Лучшее качество на русском языке. "
            "Начиная с версии v3 не требует NVIDIA NeMo. Один проход ограничен 25 секундами, "
            "длинное аудио обрабатывается через нарезку по VAD."
        ),
        requirements_file="gigaam",
        python_import="gigaam",
        homepage="https://github.com/salute-developers/GigaAM",
        license="MIT",
        supports_mps=True,
        supports_batching=True,
        install_notes=(
            "Устанавливается ТОЛЬКО из git: пакет gigaam на PyPI застыл на версии 0.1.0 "
            "от апреля 2025 и не содержит v3. Требуется Python 3.10+ и ffmpeg в PATH."
        ),
        known_issues=[
            "transcribe_longform требует токен Hugging Face с доступом к pyannote/segmentation-3.0",
            "Экстра [longform] тянет тяжёлые зависимости (pyannote.audio 4.x, torchcodec)",
        ],
        param_groups=["decoding", "audio", "vad", "batching", "output"],
        weight=10,
    ),
    EngineSpec(
        id="faster_whisper",
        name="faster-whisper (CTranslate2)",
        description=(
            "Самая производительная реализация Whisper. Квантизация int8/float16/bfloat16, "
            "пакетный конвейер, встроенный фильтр Silero VAD. Рекомендуемый способ "
            "запускать модели семейства Whisper."
        ),
        requirements_file="faster-whisper",
        python_import="faster_whisper",
        homepage="https://github.com/SYSTRAN/faster-whisper",
        license="MIT",
        supports_batching=True,
        install_notes=(
            "Для CUDA 12 + cuDNN 9 нужен ctranslate2>=4.5; для CUDA 12 + cuDNN 8 — "
            "ctranslate2==4.4.0; для CUDA 11 + cuDNN 8 — ctranslate2==3.24.0."
        ),
        known_issues=[
            "Ошибка «Could not load library libcudnn_ops_infer.so.8» — рассогласование версий cuDNN",
            "transcribe() возвращает ленивый генератор: исключения возникают при итерации",
            "У BatchedInferencePipeline другие значения по умолчанию: vad_filter=True, without_timestamps=True",
        ],
        param_groups=["decoding", "whisper", "vad", "batching", "audio", "output"],
        weight=20,
    ),
    EngineSpec(
        id="whisper",
        name="Whisper (OpenAI, эталон)",
        description=(
            "Эталонная реализация от OpenAI. Самая медленная, но воспроизводит поведение, "
            "описанное в статье. Полезна для сверки результатов и как запасной вариант."
        ),
        requirements_file="whisper",
        python_import="whisper",
        homepage="https://github.com/openai/whisper",
        license="MIT",
        supports_mps=True,
        install_notes="Требуется ffmpeg в PATH. Последний релиз пакета — 20250625.",
        known_issues=[
            "В Python API beam_size по умолчанию None (жадный поиск), в CLI — 5. "
            "Это частая причина расхождения результатов между запусками.",
        ],
        param_groups=["decoding", "whisper", "audio", "output"],
        weight=30,
    ),
    EngineSpec(
        id="whisper_cpp",
        name="whisper.cpp",
        description=(
            "Реализация на C++ без зависимостей Python. Metal и Core ML на Apple Silicon, "
            "CUDA, Vulkan, ROCm, OpenVINO на остальных платформах. Лучший выбор для "
            "серверов без GPU и для macOS."
        ),
        requirements_file="whisper-cpp",
        python_import="",
        homepage="https://github.com/ggml-org/whisper.cpp",
        license="MIT",
        supports_mps=True,
        external_binaries=["whisper-cli"],
        install_notes=(
            "Собирается из исходников через CMake. Установщик ASR Hub определяет платформу "
            "и включает нужный бэкенд: Metal и Core ML на macOS, CUDA или Vulkan на Linux и Windows."
        ),
        known_issues=[
            "Значения VAD по умолчанию сильно отличаются от faster-whisper "
            "(min_silence 100 мс против 2000 мс) — не переносите настройки один в один",
        ],
        param_groups=["decoding", "whisper", "vad", "audio", "output", "runtime"],
        weight=40,
    ),
    EngineSpec(
        id="nemo",
        name="NVIDIA NeMo",
        description=(
            "Движок для моделей Parakeet, Canary и Nemotron. Рекордная пропускная "
            "способность на GPU, потоковый режим, диаризация Sortformer."
        ),
        requirements_file="nemo",
        python_import="nemo",
        homepage="https://github.com/NVIDIA-NeMo/Speech",
        license="Apache-2.0",
        supports_streaming=True,
        supports_batching=True,
        install_notes=(
            "Рекомендуется отдельное виртуальное окружение: стек NeMo конфликтует с другими "
            "библиотеками. Системные зависимости: libsndfile1, ffmpeg, Cython, packaging."
        ),
        known_issues=[
            "nemo-toolkit[asr] несовместим с numpy>=2.0 в некоторых версиях",
            "PyTorch 2.6+ требует TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1 для старых чекпоинтов",
        ],
        param_groups=["decoding", "batching", "audio", "output", "streaming"],
        weight=25,
    ),
    EngineSpec(
        id="transformers",
        name="Hugging Face Transformers",
        description=(
            "Универсальный движок для моделей, публикуемых как обычные чекпоинты "
            "Transformers: wav2vec2, MOSS, Cohere Transcribe, Granite Speech, OWSM, Phi-4, Borealis."
        ),
        requirements_file="transformers",
        python_import="transformers",
        homepage="https://github.com/huggingface/transformers",
        license="Apache-2.0",
        supports_mps=True,
        supports_batching=True,
        install_notes="Часть моделей требует trust_remote_code=True и свежей версии transformers.",
        known_issues=["Разные модели требуют разных минимальных версий transformers"],
        param_groups=["decoding", "batching", "audio", "output"],
        weight=50,
    ),
    EngineSpec(
        id="qwen3_asr",
        name="Qwen3-ASR",
        description=(
            "Движок Alibaba Qwen3-ASR: 30 языков и 22 китайских диалекта, лицензия Apache-2.0, "
            "поддержка vLLM и потокового режима."
        ),
        requirements_file="qwen3-asr",
        python_import="qwen_asr",
        homepage="https://github.com/QwenLM/Qwen3-ASR",
        license="Apache-2.0",
        supports_streaming=True,
        supports_batching=True,
        install_notes="pip install -U qwen-asr, для сервера — qwen-asr[vllm].",
        known_issues=["Рекомендуется чистое окружение Python 3.12"],
        param_groups=["decoding", "batching", "audio", "output"],
        weight=45,
    ),
    EngineSpec(
        id="vosk",
        name="Vosk",
        description=(
            "Потоковое распознавание на CPU без GPU. Крошечные модели (от 45 МБ), "
            "биндинги для восьми языков программирования, работа на Raspberry Pi и мобильных."
        ),
        requirements_file="vosk",
        python_import="vosk",
        homepage="https://github.com/alphacep/vosk-api",
        license="Apache-2.0",
        supports_gpu=False,
        supports_streaming=True,
        install_notes="Модели скачиваются отдельными архивами и распаковываются в каталог моделей.",
        known_issues=["Качество заметно уступает нейросетевым моделям последнего поколения"],
        param_groups=["decoding", "audio", "output", "streaming"],
        weight=60,
    ),
    EngineSpec(
        id="tone",
        name="T-one (Т-Банк)",
        description=(
            "Потоковое распознавание русской речи, оптимизированное под телефонию. "
            "71.6 млн параметров, чанки по 300 мс, работа на 4 ядрах CPU."
        ),
        requirements_file="tone",
        python_import="tone",
        homepage="https://github.com/voicekit-team/T-one",
        license="Apache-2.0",
        supports_streaming=True,
        install_notes="Требует Poetry 2.1+. Под Windows KenLM собирается только через WSL или Docker.",
        known_issues=["Сборка KenLM под нативным Windows невозможна"],
        param_groups=["decoding", "audio", "output", "streaming"],
        weight=35,
    ),
    EngineSpec(
        id="whisperx",
        name="WhisperX",
        description=(
            "Конвейер поверх faster-whisper: форсированное выравнивание пословных таймкодов "
            "и диаризация через pyannote."
        ),
        requirements_file="whisperx",
        python_import="whisperx",
        homepage="https://github.com/m-bain/whisperX",
        license="BSD-2-Clause",
        supports_batching=True,
        install_notes=(
            "Обязательно отдельное окружение: жёсткий пин torch~=2.8. "
            "Для диаризации нужен токен Hugging Face и принятие лицензии pyannote."
        ),
        known_issues=[
            "Конфликт версий cuDNN между torch и ctranslate2",
            "Для каждого языка нужна своя модель выравнивания",
        ],
        param_groups=["decoding", "whisper", "diarization", "batching", "audio", "output"],
        weight=55,
    ),
    EngineSpec(
        id="funasr",
        name="FunASR / SenseVoice",
        description=(
            "Движок Alibaba: SenseVoice-Small и Paraformer. За один неавторегрессивный "
            "проход выдаёт транскрипт, язык, эмоцию и аудиособытия."
        ),
        requirements_file="funasr",
        python_import="funasr",
        homepage="https://github.com/modelscope/FunASR",
        license="MIT (код) / Alibaba model-license (веса)",
        supports_batching=True,
        install_notes="Лицензия весов не одобрена OSI — требуется юридическая проверка.",
        known_issues=["Русский язык не заявлен разработчиком"],
        param_groups=["decoding", "audio", "output"],
        weight=70,
    ),
    EngineSpec(
        id="moonshine",
        name="Moonshine",
        description="Сверхлёгкий движок для edge-устройств и браузера. Только английский.",
        requirements_file="moonshine",
        python_import="moonshine",
        homepage="https://github.com/usefulsensors/moonshine",
        license="MIT",
        install_notes="Бэкенды Keras: PyTorch, TensorFlow или JAX. Есть официальный экспорт в ONNX.",
        known_issues=["Склонность к повторам на коротких сегментах"],
        param_groups=["decoding", "audio", "output"],
        weight=80,
    ),
    EngineSpec(
        id="omnilingual",
        name="Meta Omnilingual ASR",
        description="1600+ языков нативно и 5400+ в режиме zero-shot. Apache-2.0 на код и веса.",
        requirements_file="omnilingual",
        python_import="omnilingual_asr",
        homepage="https://github.com/facebookresearch/omnilingual-asr",
        license="Apache-2.0",
        supports_batching=True,
        install_notes="pip install omnilingual-asr. Код русского языка — rus_Cyrl.",
        known_issues=["Для крупных вариантов нужно до 20 ГБ видеопамяти"],
        param_groups=["decoding", "batching", "audio", "output"],
        weight=75,
    ),
    EngineSpec(
        id="voxtral",
        name="Mistral Voxtral",
        description="Аудио-LLM Mistral: транскрипция, ответы на вопросы по аудио, потоковый режим.",
        requirements_file="voxtral",
        python_import="mistral_common",
        homepage="https://huggingface.co/mistralai/Voxtral-Mini-3B-2507",
        license="Apache-2.0",
        supports_streaming=True,
        install_notes="Рекомендуется запуск через vLLM: vllm serve <модель>.",
        known_issues=["Потоковой версии требуется минимум 16 ГБ видеопамяти"],
        param_groups=["decoding", "audio", "output", "streaming"],
        weight=85,
    ),
    EngineSpec(
        id="kyutai",
        name="Kyutai STT",
        description="Потоковое распознавание с задержкой 0.5 с и семантическим VAD. Английский и французский.",
        requirements_file="kyutai",
        python_import="moshi",
        homepage="https://github.com/kyutai-labs/delayed-streams-modeling",
        license="CC-BY-4.0",
        supports_streaming=True,
        install_notes="pip install moshi",
        known_issues=["Русский язык не поддерживается"],
        param_groups=["decoding", "audio", "output", "streaming"],
        weight=90,
    ),
    EngineSpec(
        id="sherpa_onnx",
        name="sherpa-onnx",
        description=(
            "Лёгкий рантайм ONNX без PyTorch: ASR, VAD и диаризация на CPU, мобильных "
            "устройствах и в браузере через WASM."
        ),
        requirements_file="sherpa-onnx",
        python_import="sherpa_onnx",
        homepage="https://github.com/k2-fsa/sherpa-onnx",
        license="Apache-2.0",
        supports_streaming=True,
        install_notes="Готовые сборки GigaAM v2 CTC и Parakeet доступны в галерее моделей проекта.",
        known_issues=["GigaAM v3 официально ещё не включён в релизы"],
        param_groups=["decoding", "audio", "output", "streaming"],
        weight=65,
    ),
    EngineSpec(
        id="demo",
        name="Демонстрационный симулятор",
        description=(
            "Встроенный движок без внешних зависимостей. Генерирует правдоподобный "
            "транскрипт и метрики, чтобы можно было проверить очередь, аналитику и "
            "интерфейс до установки тяжёлых моделей."
        ),
        requirements_file="",
        python_import="",
        homepage="",
        license="MIT",
        supports_batching=True,
        supports_streaming=True,
        install_notes="Не требует установки.",
        param_groups=["decoding", "output"],
        weight=999,
    ),
]

ENGINES_BY_ID: dict[str, EngineSpec] = {e.id: e for e in ENGINES}


def get_engine(engine_id: str) -> EngineSpec | None:
    return ENGINES_BY_ID.get(engine_id)
