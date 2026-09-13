import asyncio
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.db.models import Job, JobAttempt
from faultlab.domain.jobs import JobStatus, submission_fingerprint
from faultlab.repositories.jobs import JobRepository

JOB_COUNT = 250
WORKER_COUNT = 24
REPETITIONS = 3


def job_rows(*, queue: str, count: int) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    return [
        {
            "id": uuid.uuid4(),
            "queue": queue,
            "kind": "echo",
            "payload": {"sequence": index},
            "status": JobStatus.PENDING.value,
            "priority": 0,
            "max_attempts": 3,
            "attempt_count": 0,
            "run_at": now,
            "idempotency_key": f"{queue}-{index}",
            "submission_fingerprint": submission_fingerprint(
                queue=queue,
                kind="echo",
                payload={"sequence": index},
                max_attempts=3,
            ),
        }
        for index in range(count)
    ]


async def seed_jobs(sessions: async_sessionmaker[AsyncSession], *, queue: str, count: int) -> None:
    async with sessions() as session, session.begin():
        await session.execute(pg_insert(Job).values(job_rows(queue=queue, count=count)))


async def test_skip_locked_does_not_wait_behind_another_claim(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    queue = "skip-locked-proof"
    await seed_jobs(sessions, queue=queue, count=2)

    async with sessions() as first_session, sessions() as second_session:
        first_transaction = await first_session.begin()
        first = await JobRepository(first_session).claim_next(
            queue=queue,
            worker_id="worker-holding-lock",
            lease_seconds=30,
        )
        assert first is not None

        async def claim_while_first_row_is_locked() -> Job | None:
            async with second_session.begin():
                return await JobRepository(second_session).claim_next(
                    queue=queue,
                    worker_id="worker-skipping-lock",
                    lease_seconds=30,
                )

        second = await asyncio.wait_for(claim_while_first_row_is_locked(), timeout=1)
        assert second is not None
        assert second.id != first.id
        await first_transaction.rollback()


async def test_hundreds_of_jobs_have_no_duplicate_claims_across_repeated_runs(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    for repetition in range(REPETITIONS):
        queue = f"concurrency-proof-{repetition}"
        await seed_jobs(sessions, queue=queue, count=JOB_COUNT)
        start = asyncio.Event()
        claimed: list[uuid.UUID] = []

        async def worker(
            worker_number: int,
            *,
            queue_name: str = queue,
            start_event: asyncio.Event = start,
            claims: list[uuid.UUID] = claimed,
        ) -> None:
            await start_event.wait()
            async with sessions() as session:
                repository = JobRepository(session)
                while True:
                    async with session.begin():
                        job = await repository.claim_next(
                            queue=queue_name,
                            worker_id=f"worker-{worker_number}",
                            lease_seconds=30,
                        )
                    if job is None:
                        return
                    claims.append(job.id)

        tasks = [asyncio.create_task(worker(index)) for index in range(WORKER_COUNT)]
        start.set()
        await asyncio.gather(*tasks)

        assert len(claimed) == JOB_COUNT
        assert len(set(claimed)) == JOB_COUNT

        async with sessions() as session:
            running_jobs = await session.scalar(
                select(func.count())
                .select_from(Job)
                .where(
                    Job.queue == queue,
                    Job.status == JobStatus.RUNNING.value,
                    Job.attempt_count == 1,
                )
            )
            attempt_count = await session.scalar(
                select(func.count())
                .select_from(JobAttempt)
                .join(Job, Job.id == JobAttempt.job_id)
                .where(Job.queue == queue)
            )
            distinct_attempt_jobs = await session.scalar(
                select(func.count(func.distinct(JobAttempt.job_id)))
                .select_from(JobAttempt)
                .join(Job, Job.id == JobAttempt.job_id)
                .where(Job.queue == queue)
            )

        assert running_jobs == JOB_COUNT
        assert attempt_count == JOB_COUNT
        assert distinct_attempt_jobs == JOB_COUNT
