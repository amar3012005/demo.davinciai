# ============================================================================
# Visual Copilot Microservice Dockerfile
# ============================================================================

FROM python:3.11-slim AS base

# Install uv via pip
RUN pip install uv
ENV UV_SYSTEM_PYTHON=1

WORKDIR /app

# Install system dependencies
RUN --mount=type=cache,target=/var/cache/apt \
    apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    make \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt ./requirements.txt
ENV UV_HTTP_TIMEOUT=120
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.txt

# ============================================================================
# Runtime Stage
# ============================================================================
FROM base AS runtime

# Copy all source code directly (flat structure, no legacy package wrapper)
COPY . /app

# Python environment
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PORT="4005"
ENV LOG_LEVEL="INFO"

EXPOSE 4005

# Run service
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "4005", "--workers", "1"]
