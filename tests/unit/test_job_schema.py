import pytest
from pydantic import ValidationError

from faultlab.schemas.jobs import CreateJobRequest
from faultlab.worker.handlers import registered_kinds


def test_create_job_request_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError, match="unknown job kind"):
        CreateJobRequest(kind="not-registered")


def test_registered_kinds_cover_demo_handlers() -> None:
    kinds = registered_kinds()
    assert "echo" in kinds
    assert "record_once" in kinds
    CreateJobRequest(kind="echo", payload={"message": "hello"})
