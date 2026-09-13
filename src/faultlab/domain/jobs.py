import hashlib
import json
from enum import StrEnum
from typing import Any


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    SUCCEEDED = "succeeded"
    DEAD = "dead"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"
    CANCELLED = "cancelled"


CLAIMABLE_STATUSES = (JobStatus.PENDING, JobStatus.RETRY_PENDING)
TERMINAL_STATUSES = (JobStatus.SUCCEEDED, JobStatus.DEAD, JobStatus.CANCELLED)


def retry_delay_seconds(
    attempt_number: int,
    *,
    base_seconds: int,
    maximum_seconds: int,
) -> int:
    """Return deterministic exponential backoff for a one-indexed attempt number."""

    if attempt_number < 1:
        raise ValueError("attempt_number must be at least 1")
    if base_seconds < 1 or maximum_seconds < 1:
        raise ValueError("backoff values must be positive")
    return int(min(maximum_seconds, base_seconds * (2 ** (attempt_number - 1))))


def submission_fingerprint(
    *,
    queue: str,
    kind: str,
    payload: dict[str, Any],
    max_attempts: int,
) -> str:
    """Hash fields whose meaning must remain stable for an idempotency key."""

    canonical = json.dumps(
        {
            "kind": kind,
            "max_attempts": max_attempts,
            "payload": payload,
            "queue": queue,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
