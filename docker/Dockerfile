# ASR Hub — образ сервера распознавания речи.
#
# Многоступенчатая сборка: тяжёлые зависимости ставятся в отдельный слой,
# который не пересобирается при изменении кода. Профиль движков задаётся
# аргументом сборки, поэтому образ можно собрать лёгким или полным.
#
#   docker build --build-arg PROFILE=standard -t asrhub:latest .
#   docker build --build-arg PROFILE=full --build-arg ACCEL=cuda -t asrhub:cuda .

ARG PYTHON_VERSION=3.12
ARG ACCEL=cpu

# ---------------------------------------------------------------------------
# Слой 1: сборка зависимостей
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ARG PROFILE=standard
ARG ACCEL=cpu
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

# Движки по профилю
RUN set -eux; \
    case "${PROFILE}" in \
      minimal)  ENGINES="" ;; \
      light)    ENGINES="faster-whisper" ;; \
      cpu)      ENGINES="faster-whisper gigaam vosk" ;; \
      standard) ENGINES="gigaam faster-whisper whisper vad postprocess" ;; \
      russian)  ENGINES="gigaam tone vosk vad postprocess" ;; \
      full)     ENGINES="gigaam faster-whisper whisper transformers vosk vad postprocess diarization" ;; \
      *)        ENGINES="faster-whisper" ;; \
    esac; \
    for engine in ${ENGINES}; do \
      echo "=== установка движка ${engine} ==="; \
      pip install -r "requirements/engines/${engine}.txt" || \
        echo "ВНИМАНИЕ: движок ${engine} установить не удалось, продолжаем"; \
    done

# ---------------------------------------------------------------------------
# Слой 2: рабочий образ
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim

LABEL org.opencontainers.image.title="ASR Hub" \
      org.opencontainers.image.description="Сервер распознавания речи на свободных моделях" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="3.0.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    ASRHUB_DATA_DIR=/data \
    HF_HOME=/data/models \
    OMP_NUM_THREADS=0

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl tini \
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

USER asrhub
WORKDIR /app/server
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "asrhub", "--host", "0.0.0.0", "--port", "8080"]
