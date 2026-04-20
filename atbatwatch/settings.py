import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(".env"))

DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://atbatwatch:atbatwatch@localhost:5432/atbatwatch",
)
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
