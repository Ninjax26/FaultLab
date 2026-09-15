import pytest
from pydantic import ValidationError

from faultlab.config import Settings


def test_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        Settings(worker_lease_seconds=10, worker_heartbeat_seconds=10)


def test_retry_base_must_not_exceed_cap() -> None:
    with pytest.raises(ValidationError, match="retry base"):
        Settings(retry_base_seconds=10, retry_max_seconds=5)


def test_managed_postgres_url_uses_asyncpg_driver() -> None:
    settings = Settings(database_url="postgresql://user:pass@db.example/faultlab")
    assert settings.database_url == "postgresql+asyncpg://user:pass@db.example/faultlab"


def test_production_requires_a_long_access_token() -> None:
    with pytest.raises(ValidationError, match="access token"):
        Settings(environment="production", access_token="too-short")


def test_production_accepts_a_long_access_token() -> None:
    settings = Settings(environment="production", access_token="a" * 24)
    assert settings.access_token == "a" * 24
