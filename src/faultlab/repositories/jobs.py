import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from faultlab.db.models import BusinessEffect, Job, JobAttempt
from faultlab.domain.jobs import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    AttemptStatus,
    JobStatus,
    retry_delay_seconds,
    submission_fingerprint,
)


class JobRepositoryError(RuntimeError):
    pass


class JobNotFoundError(JobRepositoryError):
    pass


class IdempotencyConflictError(JobRepositoryError):
    pass


class InvalidJobTransitionError(JobRepositoryError):
    pass


class LeaseOwnershipLostError(JobRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class CreateJobCommand:
    queue: str
    kind: str
    payload: dict[str, Any]
    priority: int
    max_attempts: int
    run_at: datetime
    idempotency_key: str | None


@dataclass(frozen=True, slots=True)
class CreatedJob:
    job: Job
    deduplicated: bool


@dataclass(frozen=True, slots=True)
class HeartbeatState:
    lease_valid: bool
    cancellation_requested: bool


class JobRepository:
    """All job state transitions live here so their invariants remain reviewable."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        retry_base_seconds: int = 2,
        retry_max_seconds: int = 300,
    ) -> None:
        self._session = session
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def create_job(self, command: CreateJobCommand) -> CreatedJob:
        fingerprint = submission_fingerprint(
            queue=command.queue,
            kind=command.kind,
            payload=command.payload,
            max_attempts=command.max_attempts,
        )
        job_id = uuid.uuid4()
        statement = (
            pg_insert(Job)
            .values(
                id=job_id,
                queue=command.queue,
                kind=command.kind,
                payload=command.payload,
                status=JobStatus.PENDING.value,
                priority=command.priority,
                max_attempts=command.max_attempts,
                attempt_count=0,
                run_at=command.run_at,
                idempotency_key=command.idempotency_key,
                submission_fingerprint=fingerprint,
            )
            .on_conflict_do_nothing(constraint="uq_jobs_queue_idempotency_key")
            .returning(Job.id)
        )
        result = await self._session.execute(statement)
        created_id = result.scalar_one_or_none()

        if created_id is not None:
            job = await self._session.scalar(select(Job).where(Job.id == created_id))
            if job is None:  # pragma: no cover - protected by the transaction
                raise JobRepositoryError("created job disappeared inside its transaction")
            return CreatedJob(job=job, deduplicated=False)

        if command.idempotency_key is None:  # NULL keys never conflict in PostgreSQL
            raise JobRepositoryError("job insert unexpectedly returned no identifier")

        existing = await self._session.scalar(
            select(Job).where(
                Job.queue == command.queue,
                Job.idempotency_key == command.idempotency_key,
            )
        )
        if existing is None:  # pragma: no cover - protected by the unique constraint
            raise JobRepositoryError("idempotency conflict occurred but no job was found")
        if existing.submission_fingerprint != fingerprint:
            raise IdempotencyConflictError(
                "the idempotency key was already used for a different job submission"
            )
        return CreatedJob(job=existing, deduplicated=True)

    async def get_job(self, job_id: uuid.UUID) -> Job:
        job = await self._session.scalar(select(Job).where(Job.id == job_id))
        if job is None:
            raise JobNotFoundError(f"job {job_id} was not found")
        return job

    async def list_attempts(self, job_id: uuid.UUID) -> list[JobAttempt]:
        await self.get_job(job_id)
        statement = (
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(JobAttempt.attempt_number.asc())
        )
        return list((await self._session.scalars(statement)).all())

    async def list_jobs(
        self,
        *,
        status: JobStatus | None,
        queue: str | None,
        limit: int,
    ) -> list[Job]:
        statement: Select[tuple[Job]] = select(Job)
        if status is not None:
            statement = statement.where(Job.status == status.value)
        if queue is not None:
            statement = statement.where(Job.queue == queue)
        statement = statement.order_by(Job.created_at.desc()).limit(limit)
        return list((await self._session.scalars(statement)).all())

    async def cancel_job(self, job_id: uuid.UUID) -> Job:
        job = await self._session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise JobNotFoundError(f"job {job_id} was not found")

        status = JobStatus(job.status)
        if status in TERMINAL_STATUSES:
            return job
        now = datetime.now(UTC)
        if status == JobStatus.RUNNING:
            job.cancellation_requested_at = job.cancellation_requested_at or now
            job.updated_at = now
            return job
        job.status = JobStatus.CANCELLED.value
        job.cancellation_requested_at = now
        job.finished_at = now
        job.updated_at = now
        return job

    async def recover_expired_leases(self, *, queue: str, batch_size: int = 100) -> int:
        """Move abandoned running jobs to retry_pending or dead.

        Rows are locked with SKIP LOCKED so multiple workers may run this reaper safely.
        """

        now = datetime.now(UTC)
        jobs = list(
            (
                await self._session.scalars(
                    select(Job)
                    .where(
                        Job.queue == queue,
                        Job.status == JobStatus.RUNNING.value,
                        Job.lease_expires_at < now,
                    )
                    .order_by(Job.lease_expires_at.asc())
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )

        for job in jobs:
            attempt = await self._get_attempt(job.id, job.attempt_count)
            if attempt is not None and attempt.status == AttemptStatus.RUNNING.value:
                attempt.status = AttemptStatus.LEASE_EXPIRED.value
                attempt.error = "worker lease expired before completion"
                attempt.finished_at = now

            job.last_error = "worker lease expired before completion"
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now

            if job.cancellation_requested_at is not None:
                job.status = JobStatus.CANCELLED.value
                job.finished_at = now
            elif job.attempt_count >= job.max_attempts:
                job.status = JobStatus.DEAD.value
                job.finished_at = now
            else:
                job.status = JobStatus.RETRY_PENDING.value
                job.run_at = now + timedelta(seconds=self._retry_delay(job.attempt_count))

        return len(jobs)

    async def claim_next(self, *, queue: str, worker_id: str, lease_seconds: int) -> Job | None:
        """Atomically claim one runnable job.

        The caller must wrap this method in a transaction. PostgreSQL row locking is the
        concurrency primitive; Redis is intentionally not involved.
        """

        now = datetime.now(UTC)
        job = await self._session.scalar(
            select(Job)
            .where(
                Job.queue == queue,
                Job.status.in_([status.value for status in CLAIMABLE_STATUSES]),
                Job.run_at <= now,
            )
            .order_by(Job.priority.desc(), Job.run_at.asc(), Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if job is None:
            return None

        job.status = JobStatus.RUNNING.value
        job.attempt_count += 1
        job.lease_owner = worker_id
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        job.started_at = job.started_at or now
        job.updated_at = now

        self._session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=job.attempt_count,
                worker_id=worker_id,
                status=AttemptStatus.RUNNING.value,
                started_at=now,
            )
        )
        return job

    async def heartbeat(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        lease_seconds: int,
    ) -> HeartbeatState:
        now = datetime.now(UTC)
        result = await self._session.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.lease_owner == worker_id,
                Job.lease_expires_at > now,
            )
            .values(
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .returning(Job.cancellation_requested_at)
        )
        row = result.one_or_none()
        return HeartbeatState(
            lease_valid=row is not None,
            cancellation_requested=row is not None and row[0] is not None,
        )

    async def observe_lease(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str,
    ) -> HeartbeatState:
        """Read lease ownership and cancellation without extending the lease."""

        now = datetime.now(UTC)
        result = await self._session.execute(
            select(Job.cancellation_requested_at).where(
                Job.id == job_id,
                Job.status == JobStatus.RUNNING.value,
                Job.lease_owner == worker_id,
                Job.lease_expires_at > now,
            )
        )
        row = result.one_or_none()
        return HeartbeatState(
            lease_valid=row is not None,
            cancellation_requested=row is not None and row[0] is not None,
        )

    async def record_once(self, *, business_key: str, value: dict[str, Any]) -> bool:
        """Commit the demo business effect once, independent of job completion."""

        if not business_key or len(business_key) > 255:
            raise ValueError("business_key must be 1-255 characters")
        result = await self._session.execute(
            pg_insert(BusinessEffect)
            .values(business_key=business_key, value=value)
            .on_conflict_do_nothing(index_elements=[BusinessEffect.business_key])
            .returning(BusinessEffect.business_key)
        )
        inserted = result.scalar_one_or_none() is not None
        if not inserted:
            existing = await self._session.get(BusinessEffect, business_key)
            if existing is None:  # pragma: no cover - protected by the unique key
                raise JobRepositoryError("effect conflict occurred but no effect was found")
            if existing.value != value:
                raise IdempotencyConflictError(
                    "business_key was already used for a different effect value"
                )
        return inserted

    async def mark_cancelled(self, *, job_id: uuid.UUID, worker_id: str) -> Job:
        job = await self._locked_running_job(job_id=job_id, worker_id=worker_id)
        if job.cancellation_requested_at is None:
            raise InvalidJobTransitionError("job has no cancellation request")
        now = datetime.now(UTC)
        attempt = await self._get_attempt(job.id, job.attempt_count)
        if attempt is None:
            raise JobRepositoryError("running job has no matching attempt record")
        attempt.status = AttemptStatus.CANCELLED.value
        attempt.finished_at = now
        job.status = JobStatus.CANCELLED.value
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = now
        job.updated_at = now
        return job

    async def mark_succeeded(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        result: dict[str, Any],
    ) -> Job:
        job = await self._locked_running_job(job_id=job_id, worker_id=worker_id)
        now = datetime.now(UTC)

        attempt = await self._get_attempt(job.id, job.attempt_count)
        if attempt is None:
            raise JobRepositoryError("running job has no matching attempt record")
        attempt.status = AttemptStatus.SUCCEEDED.value
        attempt.finished_at = now

        job.status = JobStatus.SUCCEEDED.value
        job.result = result
        job.last_error = None
        job.lease_owner = None
        job.lease_expires_at = None
        job.finished_at = now
        job.updated_at = now
        return job

    async def mark_failed(
        self,
        *,
        job_id: uuid.UUID,
        worker_id: str,
        error: str,
    ) -> Job:
        job = await self._locked_running_job(job_id=job_id, worker_id=worker_id)
        now = datetime.now(UTC)
        safe_error = error[:8_000]

        attempt = await self._get_attempt(job.id, job.attempt_count)
        if attempt is None:
            raise JobRepositoryError("running job has no matching attempt record")
        attempt.status = (
            AttemptStatus.CANCELLED.value
            if job.cancellation_requested_at is not None
            else AttemptStatus.FAILED.value
        )
        attempt.error = safe_error
        attempt.finished_at = now

        job.last_error = safe_error
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = now

        if job.cancellation_requested_at is not None:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = now
        elif job.attempt_count >= job.max_attempts:
            job.status = JobStatus.DEAD.value
            job.finished_at = now
        else:
            job.status = JobStatus.RETRY_PENDING.value
            job.run_at = now + timedelta(seconds=self._retry_delay(job.attempt_count))
        return job

    async def _locked_running_job(self, *, job_id: uuid.UUID, worker_id: str) -> Job:
        job = await self._session.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if job is None:
            raise JobNotFoundError(f"job {job_id} was not found")
        if (
            job.status != JobStatus.RUNNING.value
            or job.lease_owner != worker_id
            or job.lease_expires_at is None
            or job.lease_expires_at <= datetime.now(UTC)
        ):
            raise LeaseOwnershipLostError(
                f"worker {worker_id} no longer owns the lease for job {job_id}"
            )
        return job

    async def _get_attempt(self, job_id: uuid.UUID, attempt_number: int) -> JobAttempt | None:
        attempt: JobAttempt | None = await self._session.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == job_id,
                JobAttempt.attempt_number == attempt_number,
            )
        )
        return attempt

    def _retry_delay(self, attempt_number: int) -> int:
        return retry_delay_seconds(
            attempt_number,
            base_seconds=self._retry_base_seconds,
            maximum_seconds=self._retry_max_seconds,
        )
