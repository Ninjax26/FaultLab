"""Compare Python and Go worker *processes* on identical durable jobs."""

import argparse
import asyncio
import json
import os
import resource
import statistics
import sys
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
from faultlab.db.models import Job
from faultlab.domain.jobs import JobStatus


async def total_rss_mib(processes: list[asyncio.subprocess.Process]) -> float:
    pids = [str(process.pid) for process in processes if process.returncode is None]
    if not pids:
        return 0
    command = await asyncio.create_subprocess_exec(
        "ps",
        "-o",
        "rss=",
        "-p",
        ",".join(pids),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await command.communicate()
    return sum(int(value) for value in output.split()) / 1024


async def run_case(
    database_url: str,
    *,
    runtime: str,
    worker_count: int,
    job_count: int,
    repetition: int,
    go_binary: Path,
    timeout_seconds: float,
) -> dict[str, Any]:
    queue = f"runtime-{runtime}-{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(database_url, pool_size=4, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    processes: list[asyncio.subprocess.Process] = []
    try:
        enqueue_started = time.perf_counter()
        await _seed_jobs(sessions, queue=queue, count=job_count)
        enqueue_seconds = time.perf_counter() - enqueue_started
        children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
        env = {
            **os.environ,
            "FAULTLAB_DATABASE_URL": database_url,
            "FAULTLAB_WORKER_QUEUE": queue,
            "FAULTLAB_WORKER_METRICS_PORT": "0",
            "FAULTLAB_WORKER_POLL_INTERVAL_SECONDS": "0.02",
            "FAULTLAB_WORKER_POLL_MILLISECONDS": "20",
            "FAULTLAB_LOG_LEVEL": "ERROR",
            "FAULTLAB_GO_POOL_SIZE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),  # noqa: ASYNC240
        }
        started = time.perf_counter()
        for index in range(worker_count):
            args = (
                [sys.executable, "-m", "faultlab.worker.main"]
                if runtime == "python"
                else [
                    str(go_binary),
                    "--database-url",
                    database_url,
                    "--queue",
                    queue,
                    "--worker-id",
                    f"go-benchmark-{index}",
                ]
            )
            processes.append(
                await asyncio.create_subprocess_exec(
                    *args,
                    env=env,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            )
        first_completion: float | None = None
        peak_worker_rss = 0.0
        max_connections = 0
        backlog_samples: list[tuple[float, int]] = []
        terminal = 0
        while time.perf_counter() - started < timeout_seconds:
            if any(process.returncode is not None for process in processes):
                raise RuntimeError(f"{runtime} worker exited before benchmark completed")
            async with sessions() as session:
                terminal = int(
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
                                "WHERE datname=current_database()"
                            )
                        )
                        or 0
                    ),
                )
            elapsed = time.perf_counter() - started
            if terminal and first_completion is None:
                first_completion = elapsed
            backlog_samples.append((round(elapsed, 3), job_count - terminal))
            peak_worker_rss = max(peak_worker_rss, await total_rss_mib(processes))
            if terminal == job_count:
                break
            await asyncio.sleep(0.1)
        drain_seconds = time.perf_counter() - started
        for process in processes:
            if process.returncode is None:
                process.terminate()
        await asyncio.gather(*(process.wait() for process in processes))
        children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
        child_cpu = (
            children_after.ru_utime
            + children_after.ru_stime
            - children_before.ru_utime
            - children_before.ru_stime
        )
        async with sessions() as session:
            jobs = list((await session.scalars(select(Job).where(Job.queue == queue))).all())
        latencies = [
            (job.finished_at - job.created_at).total_seconds() * 1000
            for job in jobs
            if job.finished_at is not None
        ]
        return {
            "runtime": runtime,
            "repetition": repetition,
            "workers": worker_count,
            "jobs": job_count,
            "enqueue_seconds": round(enqueue_seconds, 3),
            "startup_to_first_completion_seconds": round(first_completion, 3)
            if first_completion is not None
            else None,
            "drain_seconds": round(drain_seconds, 3),
            "completion_jobs_per_second": round(terminal / drain_seconds, 2),
            "end_to_end_latency_ms": asdict(summarize_latency(latencies)) if latencies else None,
            "succeeded": sum(job.status == JobStatus.SUCCEEDED.value for job in jobs),
            "dead": sum(job.status == JobStatus.DEAD.value for job in jobs),
            "lost": job_count - terminal,
            "peak_worker_rss_mib": round(peak_worker_rss, 2),
            "peak_rss_mib_per_worker": round(peak_worker_rss / worker_count, 2),
            "worker_cpu_seconds": round(child_cpu, 3),
            "max_database_connections": max_connections,
            "backlog_samples": backlog_samples,
            "timed_out": terminal < job_count,
        }
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
        if processes:
            await asyncio.gather(*(process.wait() for process in processes))
        await engine.dispose()


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# FaultLab Python vs Go worker processes",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "Both runtimes consumed the same `benchmark-noop` payload shape from the same "
        "PostgreSQL instance. Enqueue was batched direct SQL before process launch. "
        "This is a local comparison, not a production capacity claim.",
    ]
    lines += [
        "",
        "## Median and observed range across repetitions",
        "",
        "| Runtime | Workers | Median complete/s | Range complete/s | Median peak RSS MiB |",
        "|---|---:|---:|---:|---:|",
    ]
    groups = sorted({(run["runtime"], run["workers"]) for run in report["runs"]})
    for runtime, workers in groups:
        matched = [
            run for run in report["runs"] if run["runtime"] == runtime and run["workers"] == workers
        ]
        rates = [run["completion_jobs_per_second"] for run in matched]
        rss = [run["peak_worker_rss_mib"] for run in matched]
        lines.append(
            f"| {runtime} | {workers} | {statistics.median(rates):.2f} | "
            f"{min(rates):.2f}-{max(rates):.2f} | {statistics.median(rss):.2f} |"
        )
    lines += [
        "",
        "## Individual runs",
        "",
        "| Runtime | Rep | Workers | Jobs | Complete/s | E2E p95 ms | First completion s | "
        "Peak worker RSS MiB | Child CPU s | Lost |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in report["runs"]:
        latency = run["end_to_end_latency_ms"] or {}
        lines.append(
            f"| {run['runtime']} | {run['repetition']} | {run['workers']} | {run['jobs']} | "
            f"{run['completion_jobs_per_second']} | {latency.get('p95', 'n/a')} | "
            f"{run['startup_to_first_completion_seconds']} | "
            f"{run['peak_worker_rss_mib']} | {run['worker_cpu_seconds']} | {run['lost']} |"
        )
    lines += [
        "",
        "`First completion` includes process startup, first claim, and one job completion; "
        "it is not pure binary startup time. RSS is the sampled sum of worker processes. "
        "Each Go process was limited to one pool connection; Python uses its configured "
        "SQLAlchemy pool. Database connections and backlog traces are in the JSON report.",
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("TEST_DATABASE_URL"))
    parser.add_argument("--go-binary", type=Path, required=True)
    parser.add_argument("--workers", default="1,2,4")
    parser.add_argument("--jobs", type=int, default=200)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", type=Path, default=Path("reports/stage6-runtimes"))
    args = parser.parse_args()
    if not args.database_url or "test" not in args.database_url.lower():
        parser.error("a disposable test database URL is required")
    if not args.go_binary.is_file():
        parser.error("--go-binary must point to a built Go worker")
    environment = await collect_environment(args.database_url)
    runs = []
    for repetition in range(1, args.repetitions + 1):
        runtime_order = ("python", "go") if repetition % 2 else ("go", "python")
        for count in (int(value) for value in args.workers.split(",")):
            for runtime in runtime_order:
                runs.append(
                    await run_case(
                        args.database_url,
                        runtime=runtime,
                        worker_count=count,
                        job_count=args.jobs,
                        repetition=repetition,
                        go_binary=args.go_binary,
                        timeout_seconds=args.timeout,
                    )
                )
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database_url_redacted": redact_database_url(args.database_url),
        "environment": asdict(environment),
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    args.output.with_suffix(".md").write_text(markdown(report))
    print(f"wrote {args.output.with_suffix('.md')} and {args.output.with_suffix('.json')}")


if __name__ == "__main__":
    asyncio.run(main())
