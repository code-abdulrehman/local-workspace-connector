# Multi-stage: builder -> runtime
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --user --default-timeout=120 --retries=5 -r requirements.txt

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/home/appuser/.local/bin:${PATH}"

# Create non-root user
RUN groupadd -r -g 1000 appuser && \
    useradd -r -u 1000 -g appuser -d /home/appuser -s /bin/bash appuser && \
    mkdir -p /home/appuser/.local && \
    chown -R appuser:appuser /home/appuser

# Install runtime deps + docker CLI (for spawning exec containers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Copy app code and installed packages
COPY --from=builder /root/.local /home/appuser/.local
COPY --chown=appuser:appuser server/ ./server/

USER appuser

EXPOSE 8000
CMD ["python", "-m", "server.main"]
