"""Settings, read from the environment.

Everything the service needs to run is an environment variable, and nothing
with a secret in it has a default. A missing DATABASE_URL should stop the
process at startup, not surface later as a connection to something unintended.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "production"] = "local"
    version: str = "0.1.0"

    database_url: PostgresDsn
    redis_url: RedisDsn

    # The mobile client and the server ship independently, so the contract is
    # versioned and the server keeps serving published clients (spec 5.3).
    # Endpoints themselves come with the contract in part 5; this only reserves
    # the shape so the first published client is already on a versioned path.
    api_prefix: str = "/v1"

    log_level: str = "INFO"

    # Off in production by default. The schema is not a secret, but a service
    # holding article 9 health data has no reason to publish its surface to
    # anyone who asks.
    docs_enabled: bool = False

    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)


@lru_cache
def get_settings() -> Settings:
    return Settings()
