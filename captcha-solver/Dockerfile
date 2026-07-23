FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TURNSTILE_HEADLESS=0 \
    SOLVER_ALLOW_PRIVATE=1 \
    TURNSTILE_CONCURRENCY=0 \
    SOLVER_LOOPBACK_PROXY_HOST=host.docker.internal

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x ./run-docker.sh

EXPOSE 8877

HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=4 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8877/health', timeout=3)"

CMD ["./run-docker.sh"]
