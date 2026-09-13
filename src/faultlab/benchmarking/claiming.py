import asyncio
import json
import math
import os
import platform
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from faultlab.db.models import Job
from faultlab.domain.jobs import JobStatus, submission_fingerprint
from faultlab.repositories.jobs import JobRepository


@dataclass(frozen=True, slots=True)
class LatencySummary:
    mean: float
    p50: float
    p95: float
    p99: float
    maximum: float


@dataclass(frozen=True, slots=True)
class ClaimRunReport:
    repetition: int
    queue: str
    jobs_seeded: int
    worker_count: int
    pool_size: int
    duration_seconds: float
    throughput_jobs_per_second: float
    latency_ms: LatencySummary
    duplicate_claims: int
    missing_claims: int
    max_database_connections: int
    worker_claims: dict[str, int]
    worker_claim_coefficient_of_variation: float
    claim_query_plan: list[str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClaimSuiteReport:
    generated_at: str
    database_url_redacted: str
    environment: "EnvironmentSummary"
    runs: list[ClaimRunReport]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class EnvironmentSummary:
    python_version: str
    operating_system: str
    machine: str
    processor: str
    logical_cpu_count: int | None
    postgres_version: str
    postgres_max_connections: str
    postgres_shared_buffers: str
    postgres_work_mem: str


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        raise ValueError("cannot calculate a percentile for an empty sample")
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between 0 and 1")

    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize_latency(values_ms: list[float]) -> LatencySummary:
    if not values_ms:
        raise ValueError("at least one successful claim is required")
    return LatencySummary(
        mean=round(statistics.fmean(values_ms), 3),
        p50=round(percentile(values_ms, 0.50), 3),
        p95=round(percentile(values_ms, 0.95), 3),
        p99=round(percentile(values_ms, 0.99), 3),
        maximum=round(max(values_ms), 3),
    )


def redact_database_url(database_url: str) -> str:
    if "@" not in database_url:
        return database_url
    prefix, suffix = database_url.rsplit("@", maxsplit=1)
    scheme = prefix.split("://", maxsplit=1)[0]
    return f"{scheme}://***:***@{suffix}"


def _job_rows(*, queue: str, count: int) -> list[dict[str, object]]:
    now = datetime.now(UTC)
    return [
        {
            "id": uuid.uuid4(),
            "queue": queue,
            "kind": "benchmark-noop",
            "payload": {"sequence": index},
            "status": JobStatus.PENDING.value,
            "priority": index % 3,
            "max_attempts": 1,
            "attempt_count": 0,
            "run_at": now,
            "idempotency_key": f"{queue}-{index}",
            "submission_fingerprint": submission_fingerprint(
                queue=queue,
                kind="benchmark-noop",
                payload={"sequence": index},
                max_attempts=1,
            ),
        }
        for index in range(count)
    ]


async def _seed_jobs(
    sessions: async_sessionmaker[AsyncSession],
    *,
    queue: str,
    count: int,
    batch_size: int = 500,
) -> None:
    rows = _job_rows(queue=queue, count=count)
    async with sessions() as session, session.begin():
        for offset in range(0, len(rows), batch_size):
            await session.execute(pg_insert(Job).values(rows[offset : offset + batch_size]))


async def _explain_claim_query(
    sessions: async_sessionmaker[AsyncSession], *, queue: str
) -> list[str]:
    statement = text(
        """
        EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
        SELECT id
        FROM jobs
        WHERE queue = :queue
          AND status IN ('pending', 'retry_pending')
          AND run_at <= now()
        ORDER BY priority DESC, run_at ASC, created_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
        """
    )
    async with sessions() as session, session.begin():
        return list((await session.scalars(statement, {"queue": queue})).all())


async def _sample_database_connections(
    engine: AsyncEngine,
    *,
    application_name: str,
    stop: asyncio.Event,
    samples: list[int],
) -> None:
    statement = text(
        "SELECT count(*) FROM pg_stat_activity WHERE application_name = :application_name"
    )
    while not stop.is_set():
        async with engine.connect() as connection:
            count = await connection.scalar(statement, {"application_name": application_name})
        samples.append(int(count or 0))
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.005)
        except TimeoutError:
            pass


async def collect_environment(database_url: str) -> EnvironmentSummary:
    engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": "faultlab-stage1-metadata"}},
    )
    try:
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT
                            version(),
                            current_setting('max_connections'),
                            current_setting('shared_buffers'),
                            current_setting('work_mem')
                        """
                    )
                )
            ).one()
    finally:
        await engine.dispose()

    return EnvironmentSummary(
        python_version=sys.version.split()[0],
        operating_system=platform.platform(),
        machine=platform.machine(),
        processor=platform.processor() or "unknown",
        logical_cpu_count=os.cpu_count(),
        postgres_version=str(row[0]),
        postgres_max_connections=str(row[1]),
        postgres_shared_buffers=str(row[2]),
        postgres_work_mem=str(row[3]),
    )


async def run_claim_benchmark(
    *,
    database_url: str,
    job_count: int,
    worker_count: int,
    pool_size: int,
    repetition: int,
    keep_jobs: bool = False,
) -> ClaimRunReport:
    if job_count < 1 or worker_count < 1 or pool_size < 1:
        raise ValueError("job_count, worker_count, and pool_size must all be positive")

    run_id = uuid.uuid4().hex[:10]
    queue = f"stage1-{worker_count}w-{repetition}r-{run_id}"
    application_name = f"faultlab-stage1-{run_id}"
    engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=0,
        pool_timeout=30,
        connect_args={"server_settings": {"application_name": application_name}},
    )
    monitor_engine = create_async_engine(
        database_url,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": f"{application_name}-monitor"}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    try:
        await _seed_jobs(sessions, queue=queue, count=job_count)
        claim_query_plan = await _explain_claim_query(sessions, queue=queue)

        start = asyncio.Event()
        stop_monitor = asyncio.Event()
        connection_samples: list[int] = []
        claimed_ids: list[uuid.UUID] = []
        claim_latencies_ms: list[float] = []
        worker_claims = {f"worker-{index}": 0 for index in range(worker_count)}

        monitor_task = asyncio.create_task(
            _sample_database_connections(
                monitor_engine,
                application_name=application_name,
                stop=stop_monitor,
                samples=connection_samples,
            )
        )
        await asyncio.sleep(0.01)

        async def claim_worker(worker_number: int) -> None:
            worker_id = f"worker-{worker_number}"
            await start.wait()
            async with sessions() as session:
                repository = JobRepository(session)
                while True:
                    claim_started = time.perf_counter()
                    async with session.begin():
                        job = await repository.claim_next(
                            queue=queue,
                            worker_id=worker_id,
                            lease_seconds=300,
                        )
                    elapsed_ms = (time.perf_counter() - claim_started) * 1_000
                    if job is None:
                        return
                    claimed_ids.append(job.id)
                    claim_latencies_ms.append(elapsed_ms)
                    worker_claims[worker_id] += 1

        tasks = [asyncio.create_task(claim_worker(index)) for index in range(worker_count)]
        try:
            benchmark_started = time.perf_counter()
            start.set()
            await asyncio.gather(*tasks)
            duration_seconds = time.perf_counter() - benchmark_started
        finally:
            stop_monitor.set()
            await monitor_task

        unique_claims = len(set(claimed_ids))
        counts = list(worker_claims.values())
        coefficient_of_variation = (
            statistics.pstdev(counts) / statistics.fmean(counts)
            if counts and statistics.fmean(counts) > 0
            else 0.0
        )

        return ClaimRunReport(
            repetition=repetition,
            queue=queue,
            jobs_seeded=job_count,
            worker_count=worker_count,
            pool_size=pool_size,
            duration_seconds=round(duration_seconds, 4),
            throughput_jobs_per_second=round(len(claimed_ids) / duration_seconds, 2),
            latency_ms=summarize_latency(claim_latencies_ms),
            duplicate_claims=len(claimed_ids) - unique_claims,
            missing_claims=job_count - unique_claims,
            max_database_connections=max(connection_samples, default=0),
            worker_claims=worker_claims,
            worker_claim_coefficient_of_variation=round(coefficient_of_variation, 4),
            claim_query_plan=claim_query_plan,
        )
    finally:
        if not keep_jobs:
            async with sessions() as session, session.begin():
                await session.execute(delete(Job).where(Job.queue == queue))
        await engine.dispose()
        await monitor_engine.dispose()


async def run_claim_suite(
    *,
    database_url: str,
    job_count: int,
    worker_counts: list[int],
    repetitions: int,
    pool_size: int,
    keep_jobs: bool = False,
) -> ClaimSuiteReport:
    environment = await collect_environment(database_url)
    runs: list[ClaimRunReport] = []
    for worker_count in worker_counts:
        for repetition in range(1, repetitions + 1):
            runs.append(
                await run_claim_benchmark(
                    database_url=database_url,
                    job_count=job_count,
                    worker_count=worker_count,
                    pool_size=pool_size,
                    repetition=repetition,
                    keep_jobs=keep_jobs,
                )
            )
    return ClaimSuiteReport(
        generated_at=datetime.now(UTC).isoformat(),
        database_url_redacted=redact_database_url(database_url),
        environment=environment,
        runs=runs,
    )


def render_markdown(report: ClaimSuiteReport) -> str:
    lines = [
        "# FaultLab Stage 1 claim benchmark",
        "",
        f"Generated: `{report.generated_at}`",
        "",
        f"Environment: `{report.environment.operating_system}`; "
        f"Python `{report.environment.python_version}`; "
        f"PostgreSQL `{report.environment.postgres_version}`",
        "",
        "| Workers | Run | Jobs | Pool | Jobs/s | p50 ms | p95 ms | p99 ms | "
        "Duplicates | Missing | Max DB connections |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report.runs:
        lines.append(
            f"| {run.worker_count} | {run.repetition} | {run.jobs_seeded} | "
            f"{run.pool_size} | {run.throughput_jobs_per_second} | "
            f"{run.latency_ms.p50} | {run.latency_ms.p95} | {run.latency_ms.p99} | "
            f"{run.duplicate_claims} | {run.missing_claims} | "
            f"{run.max_database_connections} |"
        )

    lines.extend(
        [
            "",
            "## Interpretation guardrails",
            "",
            "- This benchmark measures the PostgreSQL claim transaction, not handler execution.",
            "- A valid correctness run has zero duplicate and zero missing claims.",
            "- Throughput numbers are meaningful only with machine and database context.",
            "- More workers can reduce throughput when connection or lock contention dominates.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: ClaimSuiteReport, output_path: Path) -> tuple[Path, Path]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_path.with_suffix(".json")
    markdown_path = output_path.with_suffix(".md")
    json_path.write_text(json.dumps(report.as_dict(), indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path
