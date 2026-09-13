"""Saturate disposable PostgreSQL connections, then verify worker recovery."""

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultlab.benchmarking.claiming import _seed_jobs, redact_database_url
from faultlab.db.models import Job
from faultlab.domain.jobs import JobStatus


def validated_dsn(database_url: str) -> str:
    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("connection pressure requires local disposable PostgreSQL")
    if not parsed.path.endswith("_test"):
        raise ValueError("database name must end in _test")
    return dsn


async def hold_until_full(dsn: str, max_connections: int) -> tuple[list[asyncpg.Connection], str]:
    held: list[asyncpg.Connection] = []
    failure = ""
    for _ in range(max_connections + 5):
        try:
            held.append(await asyncpg.connect(dsn, timeout=2))
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            break
    return held, failure


async def run_case(
    database_url: str, *, runtime: str, go_binary: Path, jobs: int
) -> dict[str, Any]:
    dsn = validated_dsn(database_url)
    queue = f"pressure-{runtime}-{uuid.uuid4().hex[:8]}"
    engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        max_connections = int(await session.scalar(select(func.current_setting("max_connections"))))
    await _seed_jobs(sessions, queue=queue, count=jobs)
    await engine.dispose()

    held: list[asyncpg.Connection] = []
    process: asyncio.subprocess.Process | None = None
    logs: list[str] = []
    reader: asyncio.Task[None] | None = None
    try:
        held, failure = await hold_until_full(dsn, max_connections)
        if not failure:
            raise RuntimeError("did not reach the PostgreSQL connection limit")
        held_count = len(held)

        env = {
            **os.environ,
            "FAULTLAB_DATABASE_URL": database_url,
            "FAULTLAB_WORKER_QUEUE": queue,
            "FAULTLAB_WORKER_METRICS_PORT": "0",
            "FAULTLAB_WORKER_POLL_INTERVAL_SECONDS": "0.05",
            "FAULTLAB_WORKER_POLL_MILLISECONDS": "50",
            "FAULTLAB_LOG_LEVEL": "ERROR",
            "FAULTLAB_GO_POOL_SIZE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),  # noqa: ASYNC240
        }
        args = (
            [sys.executable, "-m", "faultlab.worker.main"]
            if runtime == "python"
            else [str(go_binary), "--database-url", database_url, "--queue", queue]
        )
        process = await asyncio.create_subprocess_exec(
            *args, env=env, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )

        async def read_errors() -> None:
            assert process is not None and process.stderr is not None
            while line := await process.stderr.readline():
                logs.append(line.decode(errors="replace"))

        reader = asyncio.create_task(read_errors())
        hold_started = time.perf_counter()
        while time.perf_counter() - hold_started < 5:
            await asyncio.sleep(0.1)
            if time.perf_counter() - hold_started >= 2 and any(
                "reaper:" in line.lower()
                or "claim:" in line.lower()
                or "worker loop failed" in line.lower()
                for line in logs
            ):
                break
        held_seconds = time.perf_counter() - hold_started
        failure_lines_before_release = sum(
            "reaper:" in line.lower()
            or "claim:" in line.lower()
            or "worker loop failed" in line.lower()
            for line in logs
        )
        released_at = time.perf_counter()
        for connection in held:
            connection.terminate()
        held.clear()

        monitor_engine = create_async_engine(database_url, pool_size=1, max_overflow=0)
        monitor = async_sessionmaker(monitor_engine, expire_on_commit=False)
        completed = 0
        try:
            while time.perf_counter() - released_at < 15:
                async with monitor() as session:
                    completed = int(
                        await session.scalar(
                            select(func.count())
                            .select_from(Job)
                            .where(Job.queue == queue, Job.status == JobStatus.SUCCEEDED.value)
                        )
                        or 0
                    )
                if completed == jobs:
                    break
                await asyncio.sleep(0.1)
        finally:
            await monitor_engine.dispose()
        if completed != jobs:
            raise RuntimeError(f"{runtime} recovered only {completed}/{jobs} jobs")
        return {
            "runtime": runtime,
            "jobs": jobs,
            "postgres_max_connections": max_connections,
            "held_connections": held_count,
            "held_seconds": round(held_seconds, 3),
            "saturation_error": failure,
            "worker_failure_log_lines_before_release": failure_lines_before_release,
            "worker_connection_error_lines": sum(
                "too many clients" in line.lower() or "remaining connection slots" in line.lower()
                for line in logs
            ),
            "release_to_complete_seconds": round(time.perf_counter() - released_at, 3),
            "completed": completed,
            "lost": jobs - completed,
        }
    finally:
        if held:
            for connection in held:
                connection.terminate()
        if process is not None:
            if process.returncode is None:
                process.terminate()
            await process.wait()
        if reader is not None:
            await reader


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.getenv("TEST_DATABASE_URL"))
    parser.add_argument("--go-binary", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=10)
    parser.add_argument("--output", type=Path, default=Path("reports/stage6-connections"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("database URL is required")
    validated_dsn(args.database_url)
    if not args.go_binary.is_file():
        parser.error("--go-binary must be a built Go worker")
    runs = [
        await run_case(args.database_url, runtime=runtime, go_binary=args.go_binary, jobs=args.jobs)
        for runtime in ("python", "go")
    ]
    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database_url_redacted": redact_database_url(args.database_url),
        "warning": "Disposable local DB only. All connection slots are intentionally "
        "exhausted for 2-5s.",
        "runs": runs,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
    lines = [
        "# FaultLab connection-pressure experiment",
        "",
        "Disposable local PostgreSQL only. Both workers were started while connection slots "
        "were exhausted for at least two seconds, then all held clients were released.",
        "",
        "| Runtime | Jobs completed | Slots held | Held s | Failure log lines | "
        "Recovery s | Lost |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        lines.append(
            f"| {run['runtime']} | {run['completed']} | {run['held_connections']} | "
            f"{run['held_seconds']} | {run['worker_failure_log_lines_before_release']} | "
            f"{run['release_to_complete_seconds']} | {run['lost']} |"
        )
    lines += ["", "This is a brief recovery experiment, not sustained-load capacity evidence.", ""]
    args.output.with_suffix(".md").write_text("\n".join(lines))
    print(f"wrote {args.output.with_suffix('.md')} and {args.output.with_suffix('.json')}")


if __name__ == "__main__":
    asyncio.run(main())
