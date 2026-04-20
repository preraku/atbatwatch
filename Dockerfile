FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY atbatwatch/ ./atbatwatch/
COPY migrations/ ./migrations/
COPY alembic.ini ./

RUN uv sync --frozen --no-dev

ENTRYPOINT ["uv", "run", "--no-sync"]
CMD ["atbatwatch", "--help"]
