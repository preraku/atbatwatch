import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class PlayerConfig:
    name: str
    player_id: int | None = None


@dataclass
class Config:
    players: list[PlayerConfig]
    poll_interval_seconds: int = 10


def get_poll_interval() -> int:
    """Get poll interval from env var, fall back to 10."""
    return int(os.getenv("POLL_INTERVAL_SECONDS", "10"))


def load_config(path: Path = Path("config.toml")) -> Config:
    if not path.exists():
        return Config(players=[])
    with open(path, "rb") as f:
        data = tomllib.load(f)
    players = [
        PlayerConfig(name=p["name"], player_id=p.get("player_id"))
        for p in data.get("players", [])
    ]
    return Config(
        players=players,
        poll_interval_seconds=get_poll_interval(),
    )
