"""Settings, read from the environment.

Everything the service needs to run is an environment variable, and nothing
with a secret in it has a default. A missing DATABASE_URL should stop the
process at startup, not surface later as a connection to something unintended.
"""

from functools import lru_cache
from datetime import timedelta
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

    # How long a readiness report keeps counting, for handovers claimed under
    # the protocol that renews it. Zero — the default — means no lease at all:
    # nothing expires, nothing is handed back, and the mechanism is inert.
    #
    # Off by default on purpose. The app track's measurement has to come first:
    # background work delayed longer than the lease would hand a profile back
    # while the new phone is alive and well, and that scenario is checked on two
    # handsets before the clock is allowed to run. Shipping the code dark is
    # what lets the client be built against it in the meantime.
    authority_lease_minutes: int = 0


    @property
    def authority_lease(self) -> timedelta | None:
        """The lease as a duration, or None when it is switched off."""
        return (
            timedelta(minutes=self.authority_lease_minutes)
            if self.authority_lease_minutes > 0
            else None
        )

    # Off in production by default. The schema is not a secret, but a service
    # holding article 9 health data has no reason to publish its surface to
    # anyone who asks.
    docs_enabled: bool = False

    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)

    # Signs access tokens and keys the pairing-code HMAC. No default: a shared
    # fallback secret is worse than no secret, because it looks configured.
    jwt_secret: str

    # 32 bytes, base64. Read straight from the environment by
    # app.core.crypto — a SQLAlchemy type decorator is built at import time and
    # cannot reach settings. It is declared here anyway so a missing key fails
    # at startup rather than on the first write of article 9 data.
    encryption_key: str

    # OAuth client id from Google Cloud, used as the audience when verifying an
    # ID token. Without it any Google-issued token for any app would be
    # accepted, which is the whole attack.
    google_client_id: str = ""

    # Firebase project and a service account key file, for FCM. Empty means the
    # alert loop logs what it would have sent instead of failing — a better
    # state to deploy into than one that throws on the first missed dose.
    fcm_project_id: str = ""
    fcm_credentials_path: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
