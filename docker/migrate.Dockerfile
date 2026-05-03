FROM python:3.11-slim
RUN pip install --no-cache-dir alembic asyncpg sqlalchemy
WORKDIR /app
COPY migrations/ ./migrations/
COPY alembic.ini ./
CMD ["alembic", "upgrade", "head"]
