# syntax=docker/dockerfile:1

# Multi-stage build for optimal caching
FROM python:3.11-slim as base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PORT=8000 \
    NLTK_DATA=/usr/local/nltk_data \
    TORCH_HOME=/opt/torch

# Install system dependencies in separate layer (rarely changes)
FROM base as system-deps
RUN apt-get update && apt-get install -y \
    gcc g++ ffmpeg libsndfile1 portaudio19-dev python3-dev curl \
    && rm -rf /var/lib/apt/lists/*

# Base dependencies layer (stable, large libraries - rarely changes)
FROM system-deps as base-deps
COPY requirements-base.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements-base.txt

# Development dependencies layer (small, changing libraries)
FROM base-deps as python-deps
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

# NLTK data layer (rarely changes)
FROM python-deps as nltk-data
RUN python -c "import nltk; nltk.download('punkt_tab', download_dir='/usr/local/nltk_data', quiet=True)"

# PyTorch models layer (rarely changes)
FROM nltk-data as torch-models
RUN mkdir -p /opt/torch && \
    python -c "import torch; torch.hub.set_dir('/opt/torch'); torch.hub.load('snakers4/silero-vad', 'silero_vad', force_reload=True)" || \
    echo "Using cached PyTorch models if available"

# Application layer (changes frequently)
FROM torch-models as app
WORKDIR /app

# Create directories
RUN mkdir -p logs recordings data

# Copy application code (most frequently changing layer)
COPY . .

# Create non-root user and set permissions
RUN groupadd -r pipecat && useradd -r -g pipecat pipecat && \
    chown -R pipecat:pipecat /app && \
    chown -R pipecat:pipecat /opt/torch

# Switch to non-root user
USER pipecat

# Health check
HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:$PORT/health || exit 1

EXPOSE $PORT

# Command with all optimizations
CMD ["python", "-m", "uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--loop", "uvloop", "--backlog", "2048", "--limit-concurrency", "10"]