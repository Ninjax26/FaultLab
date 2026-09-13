import argparse
import asyncio
import json
from pathlib import Path

from faultlab.benchmarking.claiming import run_claim_suite, write_report
from faultlab.config import get_settings


def parse_worker_counts(raw_value: str) -> list[int]:
    counts = [int(value.strip()) for value in raw_value.split(",") if value.strip()]
    if not counts or any(count < 1 for count in counts):
        raise argparse.ArgumentTypeError("worker counts must be positive comma-separated integers")
    return counts


async def async_main(arguments: argparse.Namespace) -> None:
    report = await run_claim_suite(
        database_url=arguments.database_url,
        job_count=arguments.jobs,
        worker_counts=arguments.worker_counts,
        repetitions=arguments.repetitions,
        pool_size=arguments.pool_size,
        keep_jobs=arguments.keep_jobs,
    )
    json_path, markdown_path = write_report(report, arguments.output)
    print(json.dumps(report.as_dict(), indent=2))
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")


def main() -> None:
    default_database_url = get_settings().database_url
    parser = argparse.ArgumentParser(
        description="Measure FaultLab's concurrent PostgreSQL claim path."
    )
    parser.add_argument("--database-url", default=default_database_url)
    parser.add_argument("--jobs", type=int, default=500)
    parser.add_argument("--worker-counts", type=parse_worker_counts, default=[1, 2, 4, 8, 16])
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--pool-size", type=int, default=18)
    parser.add_argument("--output", type=Path, default=Path("reports/stage1-claiming"))
    parser.add_argument("--keep-jobs", action="store_true")
    arguments = parser.parse_args()

    if arguments.jobs < 1 or arguments.repetitions < 1 or arguments.pool_size < 1:
        parser.error("jobs, repetitions, and pool size must be positive")

    asyncio.run(async_main(arguments))


if __name__ == "__main__":
    main()
