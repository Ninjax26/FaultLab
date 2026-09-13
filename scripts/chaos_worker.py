"""One-shot child process for real process-crash integration tests.

The process deliberately exits without graceful shutdown after a durable checkpoint.
Only run against a disposable test database.
"""

import argparse
import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from faultlab.repositories.jobs import JobRepository


async def crash(database_url: str, queue: str, checkpoint: str) -> None:
    if "test" not in database_url.lower():
        raise ValueError("chaos worker requires a disposable test database")
    engine = create_async_engine(database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session, session.begin():
        job = await JobRepository(session).claim_next(
            queue=queue, worker_id=f"chaos-{os.getpid()}", lease_seconds=5
        )
    if job is None:
        raise RuntimeError("no job available to crash")
    if checkpoint == "after_effect":
        async with sessions() as session, session.begin():
            await JobRepository(session).record_once(
                business_key=str(job.payload["business_key"]),
                value=dict(job.payload["value"]),
            )
    print(f"crashed_after={checkpoint} job_id={job.id}", flush=True)
    os._exit(137)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--queue", required=True)
    parser.add_argument("--checkpoint", choices=["after_claim", "after_effect"], required=True)
    args = parser.parse_args()
    try:
        asyncio.run(crash(args.database_url, args.queue, args.checkpoint))
    except Exception as exc:
        print(f"chaos setup failed: {exc}", file=sys.stderr)
        raise


if __name__ == "__main__":
    main()
