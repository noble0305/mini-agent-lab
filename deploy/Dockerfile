# S05 runtime image: 只装 openai（真实模式用），mock 模式无需第三方依赖。
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN pip install "openai>=1.60.0"

COPY s01_react_loop ./s01_react_loop
COPY s05_service ./s05_service

RUN useradd --system --uid 10001 --create-home app \
    && mkdir -p /app/logs /app/workspace \
    && chown -R app:app /app

USER app

ENV HOST=0.0.0.0 \
    PORT=8080 \
    LOG_DIR=/app/logs \
    WORKSPACE=/app/workspace

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=4s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)"]

CMD ["python", "-m", "s05_service.main"]