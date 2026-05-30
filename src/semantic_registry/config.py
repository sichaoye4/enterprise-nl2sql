from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/enterprise_nl2sql",
    )
    semantic_dir: Path = Path(os.getenv("SEMANTIC_DIR", "semantic"))
    api_port: int = int(os.getenv("API_PORT", "8000"))


def get_settings() -> Settings:
    return Settings()
