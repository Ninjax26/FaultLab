import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.repositories.jobs import (
    CreateJobCommand,
    IdempotencyConflictError,
    JobRepository,
)


def command(*, key: str | None, message: str = "hello") -> CreateJobCommand:
    return CreateJobCommand(
        queue="default",
        kind="echo",
        payload={"message": message},
        priority=0,
        max_attempts=3,
        run_at=datetime.now(UTC),
        idempotency_key=key,
    )


async def test_same_idempotency_key_returns_original_job(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        first = await JobRepository(session).create_job(command(key="request-1"))
    async with sessions() as session, session.begin():
        second = await JobRepository(session).create_job(command(key="request-1"))

    assert first.job.id == second.job.id
    assert first.deduplicated is False
    assert second.deduplicated is True


async def test_reusing_key_for_different_submission_is_rejected(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        await JobRepository(session).create_job(command(key="request-1", message="first"))

    with pytest.raises(IdempotencyConflictError):
        async with sessions() as session, session.begin():
            await JobRepository(session).create_job(command(key="request-1", message="changed"))


async def test_concurrent_workers_claim_different_jobs(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        repo = JobRepository(session)
        for index in range(10):
            await repo.create_job(command(key=f"request-{index}"))

    async def claim(worker_id: str) -> uuid.UUID | None:
        async with sessions() as session, session.begin():
            job = await JobRepository(session).claim_next(
                queue="default",
                worker_id=worker_id,
                lease_seconds=30,
            )
            return job.id if job else None

    claimed = await asyncio.gather(*(claim(f"worker-{index}") for index in range(5)))

    assert None not in claimed
    assert len(set(claimed)) == 5
