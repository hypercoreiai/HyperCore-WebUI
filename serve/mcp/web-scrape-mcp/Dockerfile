FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENV PYTHONUNBUFFERED=1 \
    MCP_ALLOWED_DIR=/data/downloads \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8766

RUN mkdir -p /data/downloads
EXPOSE 8766/tcp

CMD ["python", "-m", "webscrape", "--http"]
