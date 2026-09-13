import asyncio
import os
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.db.models import Job, JobAttempt
from faultlab.domain.jobs import JobStatus
from faultlab.repositories.jobs import CreateJobCommand, JobRepository


async def wait_for_status(
    sessions: async_sessionmaker[AsyncSession], job_id: uuid.UUID, status: JobStatus
) -> Job:
    for _ in range(100):
        async with sessions() as session:
            job = await session.get(Job, job_id)
            if job is not None and job.status == status.value:
                return job
        await asyncio.sleep(0.1)
    raise AssertionError(f"job {job_id} did not become {status.value}")


async def test_go_and_python_share_queue_protocol(
    sessions: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    if shutil.which("go") is None:
        pytest.skip("Go toolchain is not installed")
    root = Path(__file__).resolve().parents[2]  # noqa: ASYNC240
    binary = tmp_path / "faultlab-go-worker"
    build = await asyncio.create_subprocess_exec(
        "go",
        "build",
        "-o",
        str(binary),
        ".",
        cwd=root / "go-worker",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, build_error = await asyncio.wait_for(build.communicate(), timeout=90)
    assert build.returncode == 0, build_error.decode()

    queue = f"mixed-{uuid.uuid4().hex[:8]}"
    async with sessions() as session, session.begin():
        created = await JobRepository(session).create_job(
            CreateJobCommand(
                queue=queue,
                kind="record_once",
                payload={"business_key": f"go-{queue}", "value": {"source": "go"}},
                priority=0,
                max_attempts=3,
                run_at=datetime.now(UTC),
                idempotency_key=None,
            )
        )
        go_job_id = created.job.id

    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--database-url",
        os.environ["TEST_DATABASE_URL"],
        "--queue",
        queue,
        "--worker-id",
        "go-integration",
        env={
            **os.environ,
            "FAULTLAB_WORKER_LEASE_SECONDS": "5",
            "FAULTLAB_WORKER_HEARTBEAT_SECONDS": "1",
            "FAULTLAB_WORKER_POLL_MILLISECONDS": "20",
        },
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        try:
            go_job = await wait_for_status(sessions, go_job_id, JobStatus.SUCCEEDED)
        except AssertionError:
            process.terminate()
            _, diagnostics = await process.communicate()
            pytest.fail(f"Go worker did not complete job: {diagnostics.decode()}")
        assert go_job.result == {"business_key": f"go-{queue}", "effect_inserted": True}

        async with sessions() as session, session.begin():
            conflicting = await JobRepository(session).create_job(
                CreateJobCommand(
                    queue=queue,
                    kind="record_once",
                    payload={"business_key": f"go-{queue}", "value": {"source": "changed"}},
                    priority=0,
                    max_attempts=1,
                    run_at=datetime.now(UTC),
                    idempotency_key=None,
                )
            )
        rejected = await wait_for_status(sessions, conflicting.job.id, JobStatus.DEAD)
        assert "different effect value" in (rejected.last_error or "")

        async with sessions() as session, session.begin():
            created = await JobRepository(session).create_job(
                CreateJobCommand(
                    queue=queue,
                    kind="sleep_uncooperative",
                    payload={"seconds": 1.5},
                    priority=0,
                    max_attempts=3,
                    run_at=datetime.now(UTC),
                    idempotency_key=None,
                )
            )
            ignored_id = created.job.id
        await wait_for_status(sessions, ignored_id, JobStatus.RUNNING)
        async with sessions() as session, session.begin():
            await JobRepository(session).cancel_job(ignored_id)
        ignored = await wait_for_status(sessions, ignored_id, JobStatus.SUCCEEDED)
        assert ignored.cancellation_requested_at is not None

        async with sessions() as session, session.begin():
            created = await JobRepository(session).create_job(
                CreateJobCommand(
                    queue=queue,
                    kind="sleep",
                    payload={"seconds": 3},
                    priority=0,
                    max_attempts=3,
                    run_at=datetime.now(UTC),
                    idempotency_key=None,
                )
            )
            cooperative_id = created.job.id
        await wait_for_status(sessions, cooperative_id, JobStatus.RUNNING)
        async with sessions() as session, session.begin():
            await JobRepository(session).cancel_job(cooperative_id)
        await wait_for_status(sessions, cooperative_id, JobStatus.CANCELLED)

        async with sessions() as session, session.begin():
            created = await JobRepository(session).create_job(
                CreateJobCommand(
                    queue=queue,
                    kind="sleep",
                    payload={"seconds": 10},
                    priority=0,
                    max_attempts=3,
                    run_at=datetime.now(UTC),
                    idempotency_key=None,
                )
            )
            crashed_job_id = created.job.id
        await wait_for_status(sessions, crashed_job_id, JobStatus.RUNNING)
        process.kill()
        await process.wait()

        # Fast-forward the lease to verify Go -> Python recovery without a five-second wait.
        async with sessions() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == crashed_job_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            assert (
                await JobRepository(session, retry_base_seconds=1).recover_expired_leases(
                    queue=queue
                )
                == 1
            )
        async with sessions() as session, session.begin():
            await session.execute(
                update(Job).where(Job.id == crashed_job_id).values(run_at=datetime.now(UTC))
            )
            recovered = await JobRepository(session).claim_next(
                queue=queue, worker_id="python-integration", lease_seconds=5
            )
        assert recovered is not None
        assert recovered.id == crashed_job_id
        assert recovered.attempt_count == 2
        async with sessions() as session, session.begin():
            await JobRepository(session).mark_succeeded(
                job_id=crashed_job_id, worker_id="python-integration", result={"recovered": True}
            )

        async with sessions() as session, session.begin():
            created = await JobRepository(session).create_job(
                CreateJobCommand(
                    queue=queue,
                    kind="echo",
                    payload={"source": "python"},
                    priority=0,
                    max_attempts=3,
                    run_at=datetime.now(UTC),
                    idempotency_key=None,
                )
            )
            python_job_id = created.job.id
            python_claim = await JobRepository(session).claim_next(
                queue=queue, worker_id="python-integration", lease_seconds=5
            )
        assert python_claim is not None
        assert python_claim.id == python_job_id
        async with sessions() as session, session.begin():
            await JobRepository(session).mark_succeeded(
                job_id=python_job_id, worker_id="python-integration", result={"source": "python"}
            )

        async with sessions() as session:
            attempts = list(
                (await session.scalars(select(JobAttempt).order_by(JobAttempt.started_at))).all()
            )
        assert {attempt.worker_id for attempt in attempts} == {
            "go-integration",
            "python-integration",
        }
    finally:
        if process.returncode is None:
            process.terminate()
        try:
            await asyncio.wait_for(process.communicate(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.communicate()
