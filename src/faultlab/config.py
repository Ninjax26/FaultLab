from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from FAULTLAB_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="FAULTLAB_",
        extra="ignore",
    )

    environment: str = "development"
    database_url: str = "postgresql+asyncpg://faultlab:faultlab@localhost:5432/faultlab"
    log_level: str = "INFO"
    access_token: str | None = None

    database_pool_size: int = Field(default=10, ge=1)
    database_max_overflow: int = Field(default=20, ge=0)

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    worker_queue: str = "default"
    worker_poll_interval_seconds: float = Field(default=1.0, gt=0)
    worker_lease_seconds: int = Field(default=30, ge=5)
    worker_heartbeat_seconds: int = Field(default=10, ge=1)
    worker_metrics_port: int = Field(default=9100, ge=0, le=65535)

    retry_base_seconds: int = Field(default=2, ge=1)
    retry_max_seconds: int = Field(default=300, ge=1)

    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: object) -> object:
        """Make managed Postgres URLs compatible with SQLAlchemy's async engine."""
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @model_validator(mode="after")
    def validate_worker_timing(self) -> "Settings":
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("worker heartbeat must be shorter than the worker lease")
        if self.retry_base_seconds > self.retry_max_seconds:
            raise ValueError("retry base must not exceed retry maximum")
        if self.environment.lower() == "production" and (
            self.access_token is None or len(self.access_token) < 24
        ):
            raise ValueError("production access token must contain at least 24 characters")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
