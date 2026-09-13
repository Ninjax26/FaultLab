import pytest
from pydantic import ValidationError

from faultlab.config import Settings


def test_heartbeat_must_be_shorter_than_lease() -> None:
    with pytest.raises(ValidationError, match="heartbeat"):
        Settings(worker_lease_seconds=10, worker_heartbeat_seconds=10)


def test_retry_base_must_not_exceed_cap() -> None:
    with pytest.raises(ValidationError, match="retry base"):
        Settings(retry_base_seconds=10, retry_max_seconds=5)
