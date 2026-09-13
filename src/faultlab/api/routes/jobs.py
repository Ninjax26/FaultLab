import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from faultlab.config import get_settings
from faultlab.db.session import get_session
from faultlab.domain.jobs import JobStatus
from faultlab.repositories.jobs import (
    CreateJobCommand,
    IdempotencyConflictError,
    InvalidJobTransitionError,
    JobNotFoundError,
    JobRepository,
)
from faultlab.schemas.jobs import (
    CreateJobRequest,
    CreateJobResponse,
    JobAttemptListResponse,
    JobAttemptResponse,
    JobListResponse,
    JobResponse,
)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"])
SessionDependency = Annotated[AsyncSession, Depends(get_session)]
StatusFilter = Annotated[JobStatus | None, Query(alias="status")]
QueueFilter = Annotated[str | None, Query(min_length=1, max_length=100)]
LimitFilter = Annotated[int, Query(ge=1, le=200)]


def repository(session: AsyncSession) -> JobRepository:
    settings = get_settings()
    return JobRepository(
        session,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )


@router.post("", response_model=CreateJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    request: CreateJobRequest,
    session: SessionDependency,
) -> CreateJobResponse:
    repo = repository(session)
    try:
        async with session.begin():
            created = await repo.create_job(
                CreateJobCommand(
                    queue=request.queue,
                    kind=request.kind,
                    payload=request.payload,
                    priority=request.priority,
                    max_attempts=request.max_attempts,
                    run_at=request.effective_run_at(),
                    idempotency_key=request.idempotency_key,
                )
            )
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return CreateJobResponse(
        job=JobResponse.model_validate(created.job),
        deduplicated=created.deduplicated,
    )


@router.get("", response_model=JobListResponse)
async def list_jobs(
    session: SessionDependency,
    job_status: StatusFilter = None,
    queue: QueueFilter = None,
    limit: LimitFilter = 50,
) -> JobListResponse:
    jobs = await repository(session).list_jobs(status=job_status, queue=queue, limit=limit)
    return JobListResponse(items=[JobResponse.model_validate(job) for job in jobs])


@router.get("/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: uuid.UUID,
    session: SessionDependency,
) -> JobResponse:
    try:
        job = await repository(session).get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JobResponse.model_validate(job)


@router.get("/{job_id}/attempts", response_model=JobAttemptListResponse)
async def list_job_attempts(
    job_id: uuid.UUID,
    session: SessionDependency,
) -> JobAttemptListResponse:
    try:
        attempts = await repository(session).list_attempts(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return JobAttemptListResponse(
        items=[JobAttemptResponse.model_validate(attempt) for attempt in attempts]
    )


@router.post("/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: uuid.UUID,
    session: SessionDependency,
) -> JobResponse:
    try:
        async with session.begin():
            job = await repository(session).cancel_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except InvalidJobTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return JobResponse.model_validate(job)
