"""Measure durable enqueue and full worker completion on a disposable PostgreSQL DB."""

import argparse
import asyncio
import json
import logging
import os
import platform
import resource
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultlab.benchmarking.claiming import (
    _seed_jobs,
    collect_environment,
    redact_database_url,
    summarize_latency,
)
from faultlab.config import Settings
from faultlab.db.models import Job
from faultlab.domain.jobs import JobStatus
from faultlab.worker.runtime import Worker


def process_usage() -> tuple[float, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime, (
        usage.ru_maxrss / 1024 / 1024 if platform.system() == "Darwin" else usage.ru_maxrss / 1024
    )


async def database_counters(sessions: async_sessionmaker[Any]) -> dict[str, int]:
    async with sessions() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT xact_commit, blks_read, blks_hit, tup_inserted, tup_updated
                    FROM pg_stat_database WHERE datname = current_database()
                    """
                )
            )
        ).one()
    return dict(
        zip(
            ("xact_commit", "blks_read", "blks_hit", "tup_inserted", "tup_updated"),
            row,
            strict=True,
        )
    )


async def run_pipeline(
    database_url: str, *, job_count: int, worker_count: int, pool_size: int, timeout_seconds: float
) -> dict[str, Any]:
    if "test" not in database_url.lower():
        raise ValueError("benchmark requires a disposable test database")
    if min(job_count, worker_count, pool_size) < 1:
        raise ValueError("jobs, workers, and pool size must be positive")
    queue = f"pipeline-{uuid.uuid4().hex[:10]}"
    app_name = f"faultlab-pipeline-{queue}"
    engine = create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=0,
        connect_args={"server_settings": {"application_name": app_name}},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(
        database_url=database_url,
        worker_queue=queue,
        worker_lease_seconds=30,
        worker_heartbeat_seconds=10,
        worker_poll_interval_seconds=0.02,
        worker_metrics_port=0,
    )
    workers = [
        Worker(settings=settings, session_factory=sessions, worker_id=f"pipeline-{index}")
        for index in range(worker_count)
    ]
    tasks: list[asyncio.Task[None]] = []
    try:
        counters_before = await database_counters(sessions)
        cpu_before, _ = process_usage()
        enqueue_start = time.perf_counter()
        await _seed_jobs(sessions, queue=queue, count=job_count)
        enqueue_duration = time.perf_counter() - enqueue_start
        tasks = [asyncio.create_task(worker.run()) for worker in workers]
        started = time.perf_counter()
        max_connections = 0
        backlog_samples: list[tuple[float, int]] = []
        terminal_count = 0
        while time.perf_counter() - started < timeout_seconds:
            async with sessions() as session:
                terminal_count = int(
                    await session.scalar(
                        select(func.count())
                        .select_from(Job)
                        .where(
                            Job.queue == queue,
                            Job.status.in_([JobStatus.SUCCEEDED.value, JobStatus.DEAD.value]),
                        )
                    )
                    or 0
                )
                max_connections = max(
                    max_connections,
                    int(
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE application_name = :name"
                            ),
                            {"name": app_name},
                        )
                        or 0
                    ),
                )
            backlog_samples.append(
                (round(time.perf_counter() - started, 3), job_count - terminal_count)
            )
            if terminal_count == job_count:
                break
            await asyncio.sleep(0.05)
        drain_seconds = time.perf_counter() - started
        for worker in workers:
            worker.stop()
        await asyncio.gather(*tasks)
        tasks.clear()
        async with sessions() as session:
            jobs = list((await session.scalars(select(Job).where(Job.queue == queue))).all())
        counters_after = await database_counters(sessions)
        cpu_after, peak_rss_mib = process_usage()
        latencies = [
            (job.finished_at - job.created_at).total_seconds() * 1000
            for job in jobs
            if job.finished_at is not None
        ]
        statuses = {
            status.value: sum(job.status == status.value for job in jobs) for status in JobStatus
        }
        return {
            "queue": queue,
            "generated_at": datetime.now(UTC).isoformat(),
            "jobs": job_count,
            "workers": worker_count,
            "pool_size": pool_size,
            "payload": "benchmark-noop, JSON {sequence: integer}, zero handler sleep",
            "enqueue_mode": "one PostgreSQL transaction, batched INSERTs of 500 rows",
            "enqueue_seconds": round(enqueue_duration, 3),
            "enqueue_jobs_per_second": round(job_count / enqueue_duration, 2),
            "backlog_drain_seconds": round(drain_seconds, 3),
            "completion_jobs_per_second": round(terminal_count / drain_seconds, 2),
            "end_to_end_latency_ms": asdict(summarize_latency(latencies)) if latencies else None,
            "status_counts": statuses,
            "lost_jobs": job_count - terminal_count,
            "max_database_connections": max_connections,
            "process_cpu_seconds": round(cpu_after - cpu_before, 3),
            "process_peak_rss_mib": round(peak_rss_mib, 2),
            "database_stat_deltas": {
                key: int(counters_after[key] - counters_before[key]) for key in counters_before
            },
            "backlog_samples": backlog_samples,
            "timed_out": terminal_count < job_count,
        }
    finally:
        for worker in workers:
            worker.stop()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FaultLab end-to-end benchmark",
        "",
        f"Generated: {report['generated_at']}",
        f"Database: `{report['database_url_redacted']}`",
        "",
        "This is a local, disposable-PostgreSQL measurement, not a production SLO. "
        "Run it again on the same idle machine before using a throughput figure on a resume.",
        "",
        "## Workload and environment",
        "",
        f"- Host: {report['environment']['operating_system']}; "
        f"logical CPUs: {report['environment']['logical_cpu_count']}",
        f"- PostgreSQL: {report['environment']['postgres_version']}",
        f"- PostgreSQL max_connections: {report['environment']['postgres_max_connections']}; "
        f"shared_buffers: {report['environment']['postgres_shared_buffers']}",
        "- PostgreSQL container CPU/memory limits: not enforced by this script; "
        "inspect Docker Desktop settings.",
        "- Enqueue uses batched direct SQL, not HTTP; completion uses real Python workers.",
        "- Handler payload: `benchmark-noop` with one integer; no artificial sleep.",
        "",
        "| Workers | Jobs | Pool | Enqueue/s | Complete/s | E2E p50 ms | E2E p95 ms | "
        "E2E p99 ms | Peak DB conns | Lost | CPU s | RSS MiB |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        latency = run["end_to_end_latency_ms"] or {}
        lines.append(
            f"| {run['workers']} | {run['jobs']} | {run['pool_size']} | "
            f"{run['enqueue_jobs_per_second']} | {run['completion_jobs_per_second']} | "
            f"{latency.get('p50', 'n/a')} | {latency.get('p95', 'n/a')} | "
            f"{latency.get('p99', 'n/a')} | {run['max_database_connections']} | "
            f"{run['lost_jobs']} | {run['process_cpu_seconds']} | {run['process_peak_rss_mib']} |"
        )
    lines += [
        "",
        "Backlog recovery rate is the `Complete/s` column. Database utilization proxies "
        "(transaction, block and tuple counters) and backlog samples are in the JSON report. "
        "This script does not claim to measure PostgreSQL CPU or memory directly.",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("TEST_DATABASE_URL"))
    parser.add_argument("--jobs", type=int, default=300)
    parser.add_argument("--workers", default="1,2,4,8")
    parser.add_argument("--pool-size", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, default=Path("reports/stage5-pipeline"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("provide --database-url or TEST_DATABASE_URL")
    logging.basicConfig(level=logging.WARNING)
    environment = await collect_environment(args.database_url)
    worker_counts = [int(value) for value in args.workers.split(",")]
    runs = [
        await run_pipeline(
            args.database_url,
            job_count=args.jobs,
            worker_count=count,
            pool_size=args.pool_size,
            timeout_seconds=args.timeout,
        )
        for count in worker_counts
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database_url_redacted": redact_database_url(args.database_url),
        "environment": asdict(environment),
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    args.output.with_suffix(".md").write_text(render_markdown(report))
    print(f"wrote {args.output.with_suffix('.md')} and {args.output.with_suffix('.json')}")


if __name__ == "__main__":
    asyncio.run(main())
