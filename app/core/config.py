from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Absolute path to the project root's .env, regardless of the process's cwd --
# so .env loads the same way whether invoked as `uvicorn app.main:app`,
# `alembic upgrade head`, or a script run from any directory.
_ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    database_url: str


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
