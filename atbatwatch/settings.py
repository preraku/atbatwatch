import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://atbatwatch:atbatwatch@localhost:5432/atbatwatch",
)
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
JWT_SECRET: str = os.getenv("JWT_SECRET", "dev-secret-change-in-prod")
CORS_ORIGINS: list[str] = [
    o.strip()
    for o in os.getenv("CORS_ORIGIN", "http://localhost:8080").split(",")
    if o.strip()
]
