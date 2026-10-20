from enum import StrEnum
from typing import Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-insecure-secret-key-change-me"  # noqa: S105


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Application settings, read from environment variables (and `.env`)."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+asyncpg://healthy:healthy@localhost:5432/healthy_eating"
    secret_key: SecretStr = SecretStr(DEV_SECRET_KEY)
    log_level: str = "INFO"

    access_token_ttl_seconds: int = Field(default=3600, gt=0)
    refresh_token_ttl_seconds: int = Field(default=60 * 60 * 24 * 90, gt=0)
    authorization_code_ttl_seconds: int = Field(default=300, gt=0)
    login_ttl_seconds: int = Field(default=900, gt=0)
    # How often expired login requests, codes and tokens are deleted; 0 turns it off.
    cleanup_interval_seconds: int = Field(default=3600, ge=0)

    # Lets you sign in with any email address, without a social provider.
    # Only allowed in development.
    dev_login_enabled: bool = False

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None

    microsoft_client_id: str | None = None
    microsoft_client_secret: SecretStr | None = None

    apple_services_id: str | None = None
    apple_team_id: str | None = None
    apple_key_id: str | None = None
    apple_private_key: SecretStr | None = None

    @model_validator(mode="after")
    def _check_production_safety(self) -> Self:
        if self.environment is Environment.PRODUCTION:
            secret = self.secret_key.get_secret_value()
            if secret == DEV_SECRET_KEY or len(secret) < 32:
                raise ValueError("SECRET_KEY must be set to at least 32 characters in production")
            if self.dev_login_enabled:
                raise ValueError("DEV_LOGIN_ENABLED is not allowed in production")
            if not self.base_url.startswith("https://"):
                raise ValueError("BASE_URL must use https in production")
        return self

    @property
    def is_https(self) -> bool:
        return self.base_url.startswith("https://")

    @property
    def public_url(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def microsoft_enabled(self) -> bool:
        return bool(self.microsoft_client_id and self.microsoft_client_secret)

    @property
    def apple_enabled(self) -> bool:
        return bool(
            self.apple_services_id
            and self.apple_team_id
            and self.apple_key_id
            and self.apple_private_key
        )
