# ASR Hub — образ сервера распознавания речи.
#
# Многоступенчатая сборка: тяжёлые зависимости ставятся в отдельный слой,
# который не пересобирается при изменении кода. Профиль движков задаётся
# аргументом сборки, поэтому образ можно собрать лёгким или полным.
#
#   docker build --build-arg PROFILE=standard -t asrhub:latest .
#   docker build --build-arg PROFILE=full --build-arg ACCEL=cuda -t asrhub:cuda .
#   docker build --build-arg ENGINES=gigaam,vosk -t asrhub:свой .
#
# ENGINES важнее PROFILE: список через запятую, имена как у файлов в
# requirements/engines (подчёркивания приводятся к дефисам).

ARG PYTHON_VERSION=3.12
ARG ACCEL=cpu

# ---------------------------------------------------------------------------
# Слой 1: сборка зависимостей
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG PROFILE=standard
ARG ACCEL=cpu
ARG ENGINES=""
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements/ ./requirements/

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --upgrade pip setuptools wheel && \
    pip install -r requirements/base.txt

# PyTorch ставим отдельно: у каждого ускорителя свой индекс пакетов
RUN if [ "${PROFILE}" != "minimal" ]; then \
        pip install --index-url "${TORCH_INDEX}" torch torchaudio ; \
    fi

# Движки: явный список важнее профиля.
#
# Установщик с ключом --engines писал свой список в docker/.env, а образ его
# не читал и собирал набор профиля: человеку говорили одно, в контейнер
# попадало другое. Имена приводятся к именам файлов требований — установщик
# пишет их через подчёркивание (faster_whisper), файлы названы через дефис.
#
# Наборы профилей совпадают с profile_engines() в scripts/install.sh, кроме
# перечисленного ниже. Расхождение намеренное и проверяется тестом.
#
# ИСКЛЮЧЕНО-В-ОБРАЗЕ: whisper_cpp nemo whisperx qwen3_asr
#   whisper_cpp   собирается из исходников и требует cmake и модели рядом —
#                 в образе это лишние полгигабайта инструментов сборки;
#   nemo          тянет несколько гигабайт зависимостей и свой пин protobuf;
#   whisperx      тянет pyannote вторым экземпляром поверх diarization;
#   qwen3_asr     веса грузятся только из сети при первом запуске.
# Любой из них ставится явным списком: --build-arg ENGINES=nemo,whisperx.
#
# ДОБАВЛЕНО-В-ОБРАЗЕ: vad postprocess diarization
#   При обычной установке это «дополнения» из мастера, а не движки: их нет в
#   profile_engines(), их предлагает отдельный вопрос. В образе мастера нет,
#   спросить некого, и без них молчаливо отключаются нарезка по тишине,
#   расстановка знаков препинания и разделение по говорящим. Диаризация
#   тяжёлая, поэтому только в «full» — в остальных профилях её нет.
RUN set -eux; \
    if [ -n "${ENGINES}" ]; then \
      LIST="$(echo "${ENGINES}" | tr ',_' ' -')"; \
    else \
      case "${PROFILE}" in \
        minimal)  LIST="" ;; \
        light)    LIST="faster-whisper" ;; \
        cpu)      LIST="faster-whisper gigaam vosk vad postprocess" ;; \
        apple)    LIST="faster-whisper gigaam vad postprocess" ;; \
        standard) LIST="gigaam faster-whisper whisper vad postprocess" ;; \
        russian)  LIST="gigaam tone vosk vad postprocess" ;; \
        full)     LIST="gigaam faster-whisper whisper transformers vosk tone vad postprocess diarization" ;; \
        *)        LIST="faster-whisper" ;; \
      esac; \
    fi; \
    printf '%s' "${LIST}" > /opt/venv/.asrhub-engines; \
    for engine in ${LIST}; do \
      if [ ! -f "requirements/engines/${engine}.txt" ]; then \
        echo "ВНИМАНИЕ: нет файла зависимостей для движка ${engine} — пропускаем"; \
        continue; \
      fi; \
      echo "=== установка движка ${engine} ==="; \
      pip install -r "requirements/engines/${engine}.txt" || \
        echo "ВНИМАНИЕ: движок ${engine} установить не удалось, продолжаем"; \
    done

# Версии, которые мы держим вопреки требованиям движков. Ставится последним
# и с --no-deps — ровно как в scripts/install.sh: движки ставятся по одному,
# каждый своим вызовом pip, и последний перетягивает общие пакеты на себя.
# Без этого шага образ с диаризацией собирался с protobuf, который отказывался
# грузить код googleapis-common-protos, — то есть диаризация падала на импорте.
RUN if [ -s /opt/venv/.asrhub-engines ]; then \
      pip install --no-deps --upgrade -r requirements/overrides.txt; \
    fi

# ---------------------------------------------------------------------------
# Слой 2: рабочий образ
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

ARG PROFILE=standard
ARG ACCEL=cpu

# Версия образа. Значение по умолчанию обязано совпадать с файлом VERSION —
# это проверяется тестом: раньше оно было вписано числом и тихо отставало от
# файла на каждом выпуске, а docker inspect показывал прошлогоднюю версию.
ARG VERSION=3.0.0

LABEL org.opencontainers.image.title="ASR Hub" \
      org.opencontainers.image.description="Сервер распознавания речи на свободных моделях" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"
LABEL ru.asrhub.profile="${PROFILE}" ru.asrhub.accel="${ACCEL}"

# OMP_NUM_THREADS раньше стоял в ноль. Для OpenMP это недопустимое значение:
# libgomp пишет «Invalid value» и игнорирует его. Число потоков сервер и так
# считает сам по числу физических ядер, поэтому переменную не задаём.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    ASRHUB_DATA_DIR=/data \
    HF_HOME=/data/models

# Профиль сборки виден изнутри контейнера и в docker inspect: по одному
# только имени образа не понять, какие движки в нём есть и собран ли он
# под видеокарту.
ENV ASRHUB_BUILD_PROFILE=${PROFILE} \
    ASRHUB_BUILD_ACCEL=${ACCEL}

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl tini gosu \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --shell /bin/bash --uid 1000 asrhub

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY server/ ./server/
COPY scripts/ ./scripts/
COPY requirements/ ./requirements/
COPY VERSION ./
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    mkdir -p /data/uploads /data/results /data/models /data/logs /data/tmp && \
    chown -R asrhub:asrhub /data /app

# Пользователь не переключается здесь: смонтированный том принадлежит
# владельцу с хоста, и права на него можно поправить только от root.
# entrypoint.sh делает это и сразу понижает права до asrhub через gosu.
WORKDIR /app/server
VOLUME ["/data"]
EXPOSE 8080

# /health, а не /api/health: адрес тот же по содержанию, но отвечает 503,
# когда база недоступна, — то есть curl -fsS честно признаёт контейнер
# нездоровым вместо «200, всё хорошо» на сервере, который упадёт на первом
# же задании.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "asrhub", "--host", "0.0.0.0", "--port", "8080"]
