import asyncio
import os
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.config import Settings
from faultlab.db.models import BusinessEffect, Job, JobAttempt
from faultlab.domain.jobs import AttemptStatus, JobStatus
from faultlab.repositories.jobs import (
    CreateJobCommand,
    IdempotencyConflictError,
    JobRepository,
    LeaseOwnershipLostError,
)
from faultlab.worker.runtime import Worker


async def create_job(
    sessions: async_sessionmaker[AsyncSession],
    *,
    queue: str,
    kind: str = "echo",
    payload: dict[str, object] | None = None,
    max_attempts: int = 3,
) -> uuid.UUID:
    async with sessions() as session, session.begin():
        created = await JobRepository(session).create_job(
            CreateJobCommand(
                queue=queue,
                kind=kind,
                payload=payload or {},
                priority=0,
                max_attempts=max_attempts,
                run_at=datetime.now(UTC),
                idempotency_key=None,
            )
        )
        return created.job.id


async def claim(
    sessions: async_sessionmaker[AsyncSession], *, queue: str, worker_id: str
) -> Job | None:
    async with sessions() as session, session.begin():
        return await JobRepository(session).claim_next(
            queue=queue, worker_id=worker_id, lease_seconds=5
        )


async def force_lease_expiry(sessions: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> None:
    async with sessions() as session, session.begin():
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )


async def test_expired_lease_retries_and_fences_old_worker(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queue = "recovery"
    job_id = await create_job(sessions, queue=queue)
    first = await claim(sessions, queue=queue, worker_id="worker-a")
    assert first is not None
    await force_lease_expiry(sessions, job_id)

    async with sessions() as session, session.begin():
        recovered = await JobRepository(session, retry_base_seconds=1).recover_expired_leases(
            queue=queue
        )
    assert recovered == 1

    with pytest.raises(LeaseOwnershipLostError):
        async with sessions() as session, session.begin():
            await JobRepository(session).mark_succeeded(
                job_id=job_id, worker_id="worker-a", result={}
            )

    async with sessions() as session, session.begin():
        await session.execute(update(Job).where(Job.id == job_id).values(run_at=datetime.now(UTC)))
    second = await claim(sessions, queue=queue, worker_id="worker-b")
    assert second is not None
    assert second.id == job_id
    assert second.attempt_count == 2
    async with sessions() as session:
        attempts = list(
            (await session.scalars(select(JobAttempt).order_by(JobAttempt.attempt_number))).all()
        )
    assert [attempt.status for attempt in attempts] == [
        AttemptStatus.LEASE_EXPIRED.value,
        AttemptStatus.RUNNING.value,
    ]


async def test_business_effect_key_rejects_different_value(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions() as session, session.begin():
        assert await JobRepository(session).record_once(
            business_key="same-key", value={"amount": 1}
        )
    async with sessions() as session, session.begin():
        assert not await JobRepository(session).record_once(
            business_key="same-key", value={"amount": 1}
        )
    with pytest.raises(IdempotencyConflictError):
        async with sessions() as session, session.begin():
            await JobRepository(session).record_once(business_key="same-key", value={"amount": 2})


@pytest.mark.parametrize("checkpoint", ["after_claim", "after_effect"])
async def test_crashed_worker_recovers_without_lost_or_duplicate_effect(
    sessions: async_sessionmaker[AsyncSession],
    checkpoint: str,
) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    queue = f"chaos-{uuid.uuid4().hex[:8]}"
    business_key = f"effect-{uuid.uuid4().hex}"
    job_id = await create_job(
        sessions,
        queue=queue,
        kind="record_once",
        payload={"business_key": business_key, "value": {"amount": 42}},
    )
    script = Path(__file__).resolve().parents[2] / "scripts" / "chaos_worker.py"  # noqa: ASYNC240
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        "--database-url",
        database_url,
        "--queue",
        queue,
        "--checkpoint",
        checkpoint,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": str(script.parents[1] / "src")},
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
    assert process.returncode == 137, stderr.decode()
    assert f"crashed_after={checkpoint}" in stdout.decode()

    async with sessions() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(BusinessEffect)
            .where(BusinessEffect.business_key == business_key)
        ) == int(checkpoint == "after_effect")

    # Wait for the real five-second lease instead of modifying timestamps.
    recovered = 0
    while time.monotonic() - started < 9 and recovered == 0:
        async with sessions() as session, session.begin():
            recovered = await JobRepository(session, retry_base_seconds=1).recover_expired_leases(
                queue=queue
            )
        if recovered == 0:
            await asyncio.sleep(0.1)
    recovery_seconds = time.monotonic() - started
    assert recovered == 1

    replacement: Job | None = None
    while time.monotonic() - started < 11 and replacement is None:
        replacement = await claim(sessions, queue=queue, worker_id="replacement")
        if replacement is None:
            await asyncio.sleep(0.1)
    assert replacement is not None
    assert replacement.id == job_id
    assert replacement.attempt_count == 2

    worker = Worker(
        settings=Settings(worker_queue=queue, worker_lease_seconds=5, worker_heartbeat_seconds=1),
        session_factory=sessions,
        worker_id="replacement",
    )
    await worker._execute(replacement)
    async with sessions() as session:
        job = await session.get(Job, job_id)
        effects = await session.scalar(
            select(func.count())
            .select_from(BusinessEffect)
            .where(BusinessEffect.business_key == business_key)
        )
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.attempt_count == 2
    assert job.result == {
        "business_key": business_key,
        "effect_inserted": checkpoint == "after_claim",
    }
    assert effects == 1
    assert 5 <= recovery_seconds < 9


async def test_queued_and_running_cancellation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queued_id = await create_job(sessions, queue="cancel-queued")
    async with sessions() as session, session.begin():
        queued = await JobRepository(session).cancel_job(queued_id)
    assert queued.status == JobStatus.CANCELLED.value
    assert await claim(sessions, queue="cancel-queued", worker_id="worker") is None

    queue = "cancel-running"
    running_id = await create_job(sessions, queue=queue, kind="sleep", payload={"seconds": 4})
    running = await claim(sessions, queue=queue, worker_id="worker")
    assert running is not None
    worker = Worker(
        settings=Settings(worker_queue=queue, worker_lease_seconds=5, worker_heartbeat_seconds=1),
        session_factory=sessions,
        worker_id="worker",
    )
    execution = asyncio.create_task(worker._execute(running))
    await asyncio.sleep(0.2)
    async with sessions() as session, session.begin():
        requested = await JobRepository(session).cancel_job(running_id)
    assert requested.status == JobStatus.RUNNING.value
    assert requested.cancellation_requested_at is not None
    await asyncio.wait_for(execution, timeout=3)
    async with sessions() as session:
        completed = await session.get(Job, running_id)
    assert completed is not None
    assert completed.status == JobStatus.CANCELLED.value


async def test_crash_during_cancellation_and_uncooperative_handler(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queue = "cancel-crash"
    job_id = await create_job(sessions, queue=queue)
    assert await claim(sessions, queue=queue, worker_id="crashed") is not None
    async with sessions() as session, session.begin():
        await JobRepository(session).cancel_job(job_id)
    await force_lease_expiry(sessions, job_id)
    async with sessions() as session, session.begin():
        assert await JobRepository(session).recover_expired_leases(queue=queue) == 1
    async with sessions() as session:
        job = await session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.CANCELLED.value

    queue = "cancel-ignored"
    job_id = await create_job(
        sessions, queue=queue, kind="sleep_uncooperative", payload={"seconds": 1.5}
    )
    running = await claim(sessions, queue=queue, worker_id="worker")
    assert running is not None
    worker = Worker(
        settings=Settings(worker_queue=queue, worker_lease_seconds=5, worker_heartbeat_seconds=1),
        session_factory=sessions,
        worker_id="worker",
    )
    execution = asyncio.create_task(worker._execute(running))
    await asyncio.sleep(0.2)
    async with sessions() as session, session.begin():
        await JobRepository(session).cancel_job(job_id)
    await asyncio.wait_for(execution, timeout=3)
    async with sessions() as session:
        job = await session.get(Job, job_id)
    assert job is not None
    assert job.status == JobStatus.SUCCEEDED.value
    assert job.cancellation_requested_at is not None
