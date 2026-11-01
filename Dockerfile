# Lead Desk container image for a later VPS deployment (not built or verified yet).
# Build:  docker build -t leaddesk .
# Run:    docker run -p 127.0.0.1:8765:8765 -v leaddesk-data:/data --env-file .env leaddesk
# Put HTTPS (e.g. Caddy) in front and set LEADGEN_HTTPS_ONLY_COOKIES=true before exposing it beyond localhost.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    LEADGEN_DATA_DIR=/data
# Same uv version that wrote uv.lock locally, so `--frozen` can read it.
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations ./migrations
COPY leadgen ./leadgen

RUN useradd --create-home --uid 10001 leaddesk && mkdir -p /data && chown leaddesk /data
USER leaddesk
VOLUME ["/data"]
EXPOSE 8765
# Inside the container the app must listen on all interfaces; the -p 127.0.0.1:... mapping keeps it local.
CMD ["/app/.venv/bin/uvicorn", "leadgen.web.asgi:app", "--host", "0.0.0.0", "--port", "8765"]
