import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from faultlab.domain.jobs import JobStatus


class CreateJobRequest(BaseModel):
    queue: str = Field(default="default", min_length=1, max_length=100)
    kind: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(default=0, ge=-100, le=100)
    max_attempts: int = Field(default=3, ge=1, le=20)
    run_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("run_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("run_at must include a timezone")
        return value

    def effective_run_at(self) -> datetime:
        return self.run_at or datetime.now(UTC)


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    queue: str
    kind: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    max_attempts: int
    attempt_count: int
    run_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    cancellation_requested_at: datetime | None
    idempotency_key: str | None
    result: dict[str, Any] | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class CreateJobResponse(BaseModel):
    job: JobResponse
    deduplicated: bool


class JobListResponse(BaseModel):
    items: list[JobResponse]
