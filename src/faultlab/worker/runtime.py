import asyncio
import contextlib
import logging
import time
import uuid

from opentelemetry import trace
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.config import Settings
from faultlab.db.models import Job
from faultlab.observability import (
    JOB_DURATION,
    JOBS_CLAIMED,
    JOBS_FINISHED,
    LEASES_RECOVERED,
    add_job_span_attributes,
)
from faultlab.repositories.jobs import (
    InvalidJobTransitionError,
    JobRepository,
    LeaseOwnershipLostError,
)
from faultlab.worker.handlers import JobCancellationRequested, JobContext, get_handler

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        worker_id: str,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._worker_id = worker_id
        self._stopping = asyncio.Event()

    def stop(self) -> None:
        self._stopping.set()

    async def run(self) -> None:
        logger.info(
            "worker started worker_id=%s queue=%s",
            self._worker_id,
            self._settings.worker_queue,
        )
        while not self._stopping.is_set():
            try:
                recovered = await self._recover_expired_leases()
                if recovered:
                    logger.warning("recovered %s expired job leases", recovered)

                job = await self._claim_one()
                if job is None:
                    await self._wait_for_poll()
                    continue
                await self._execute(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("worker loop failed; polling will resume")
                await self._wait_for_poll()

        logger.info("worker stopped worker_id=%s", self._worker_id)

    async def _recover_expired_leases(self) -> int:
        async with self._session_factory() as session, session.begin():
            count = await self._repository(session).recover_expired_leases(
                queue=self._settings.worker_queue
            )
        if count:
            LEASES_RECOVERED.labels(queue=self._settings.worker_queue).inc(count)
        return count

    async def _claim_one(self) -> Job | None:
        async with self._session_factory() as session, session.begin():
            job = await self._repository(session).claim_next(
                queue=self._settings.worker_queue,
                worker_id=self._worker_id,
                lease_seconds=self._settings.worker_lease_seconds,
            )
        if job is not None:
            JOBS_CLAIMED.labels(queue=job.queue, kind=job.kind).inc()
            logger.info(
                "claimed job job_id=%s kind=%s attempt=%s",
                job.id,
                job.kind,
                job.attempt_count,
            )
        return job

    async def _execute(self, job: Job) -> None:
        cancellation_event = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat(job.id, cancellation_event), name=f"heartbeat-{job.id}"
        )
        started = time.monotonic()

        with tracer.start_as_current_span("faultlab.execute_job") as span:
            add_job_span_attributes(
                span,
                job_id=str(job.id),
                queue=job.queue,
                kind=job.kind,
            )
            try:
                handler = get_handler(job.kind)
                result = await handler(
                    job.payload,
                    JobContext(
                        job_id=str(job.id),
                        attempt_number=job.attempt_count,
                        worker_id=self._worker_id,
                        cancellation_event=cancellation_event,
                        record_effect=self._record_effect,
                    ),
                )
            except asyncio.CancelledError:
                raise
            except JobCancellationRequested:
                await self._record_cancellation(job)
            except Exception as exc:
                span.record_exception(exc)
                await self._record_failure(job, exc)
            else:
                await self._record_success(job, result)
            finally:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
                JOB_DURATION.labels(queue=job.queue, kind=job.kind).observe(
                    time.monotonic() - started
                )

    async def _heartbeat(self, job_id: uuid.UUID, cancellation_event: asyncio.Event) -> None:
        poll = min(1.0, float(self._settings.worker_heartbeat_seconds))
        last_renew = time.monotonic()
        while True:
            try:
                async with self._session_factory() as session, session.begin():
                    repo = self._repository(session)
                    now = time.monotonic()
                    if now - last_renew >= self._settings.worker_heartbeat_seconds:
                        state = await repo.heartbeat(
                            job_id=job_id,
                            worker_id=self._worker_id,
                            lease_seconds=self._settings.worker_lease_seconds,
                        )
                        last_renew = now
                    else:
                        state = await repo.observe_lease(
                            job_id=job_id,
                            worker_id=self._worker_id,
                        )
            except Exception:
                logger.exception("heartbeat failed; handler should stop at its next safe point")
                cancellation_event.set()
                return
            if not state.lease_valid:
                logger.error("lost lease while heartbeating job_id=%s", job_id)
                cancellation_event.set()
                return
            if state.cancellation_requested:
                cancellation_event.set()
            await asyncio.sleep(poll)

    async def _record_effect(self, business_key: str, value: dict[str, object]) -> bool:
        async with self._session_factory() as session, session.begin():
            return await self._repository(session).record_once(
                business_key=business_key, value=value
            )

    async def _record_cancellation(self, job: Job) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository(session).mark_cancelled(
                    job_id=job.id, worker_id=self._worker_id
                )
        except (LeaseOwnershipLostError, InvalidJobTransitionError):
            logger.warning(
                "could not cancel job; cancellation or lease ownership is no longer valid "
                "job_id=%s",
                job.id,
            )
            return
        JOBS_FINISHED.labels(queue=job.queue, kind=job.kind, outcome="cancelled").inc()
        logger.info("job cancelled job_id=%s", job.id)

    async def _record_success(self, job: Job, result: dict[str, object]) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                await self._repository(session).mark_succeeded(
                    job_id=job.id,
                    worker_id=self._worker_id,
                    result=result,
                )
        except LeaseOwnershipLostError:
            logger.exception("could not complete job because its lease was lost job_id=%s", job.id)
            return

        JOBS_FINISHED.labels(queue=job.queue, kind=job.kind, outcome="succeeded").inc()
        logger.info("job succeeded job_id=%s", job.id)

    async def _record_failure(self, job: Job, error: Exception) -> None:
        try:
            async with self._session_factory() as session, session.begin():
                updated = await self._repository(session).mark_failed(
                    job_id=job.id,
                    worker_id=self._worker_id,
                    error=f"{type(error).__name__}: {error}",
                )
        except LeaseOwnershipLostError:
            logger.exception("could not fail job because its lease was lost job_id=%s", job.id)
            return

        JOBS_FINISHED.labels(queue=job.queue, kind=job.kind, outcome=updated.status).inc()
        logger.warning(
            "job execution failed job_id=%s next_status=%s error=%r",
            job.id,
            updated.status,
            error,
        )

    async def _wait_for_poll(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(
                self._stopping.wait(),
                timeout=self._settings.worker_poll_interval_seconds,
            )

    def _repository(self, session: AsyncSession) -> JobRepository:
        return JobRepository(
            session,
            retry_base_seconds=self._settings.retry_base_seconds,
            retry_max_seconds=self._settings.retry_max_seconds,
        )
