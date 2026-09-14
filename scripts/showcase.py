"""Run a small, self-checking FaultLab demo against a running Compose stack."""

import argparse
import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

TERMINAL = {"succeeded", "dead", "cancelled"}


async def request(
    client: httpx.AsyncClient, method: str, path: str, **kwargs: Any
) -> dict[str, Any]:
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    body: dict[str, Any] = response.json()
    return body


async def wait_for_status(
    client: httpx.AsyncClient,
    job_id: str,
    expected: str,
    *,
    max_wait_seconds: float = 45,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + max_wait_seconds
    last_status = "unknown"
    while asyncio.get_running_loop().time() < deadline:
        job = await request(client, "GET", f"/v1/jobs/{job_id}")
        last_status = job["status"]
        if last_status == expected:
            return job
        if last_status in TERMINAL and last_status != expected:
            raise RuntimeError(f"job {job_id} ended as {last_status}, expected {expected}")
        await asyncio.sleep(0.25)
    raise TimeoutError(f"job {job_id} remained {last_status}; expected {expected}")


async def run_showcase(base_url: str, *, quick: bool) -> None:
    run_id = uuid.uuid4().hex[:12]
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        await request(client, "GET", "/health/ready")
        print(f"FaultLab is ready. Demo ID: {run_id}\n")

        print("1. A normal job is accepted, claimed, and completed")
        created = await request(
            client,
            "POST",
            "/v1/jobs",
            json={"kind": "echo", "payload": {"message": "hello from the showcase"}},
        )
        echo_id = created["job"]["id"]
        echo = await wait_for_status(client, echo_id, "succeeded")
        print(f"   {echo_id}: {echo['status']}; result={echo['result']}\n")

        print("2. The same submission key does not create a second job")
        submission = {
            "kind": "echo",
            "payload": {"message": "submit once"},
            "idempotency_key": f"showcase-{run_id}",
        }
        first = await request(client, "POST", "/v1/jobs", json=submission)
        second = await request(client, "POST", "/v1/jobs", json=submission)
        if first["job"]["id"] != second["job"]["id"] or not second["deduplicated"]:
            raise RuntimeError("submission idempotency check failed")
        print(f"   both requests returned {first['job']['id']}; deduplicated=true\n")

        print("3. An intentional failure is retried and its attempts are visible")
        created = await request(
            client,
            "POST",
            "/v1/jobs",
            json={"kind": "flaky", "payload": {"fail_attempts": 1}, "max_attempts": 3},
        )
        flaky_id = created["job"]["id"]
        await wait_for_status(client, flaky_id, "succeeded")
        attempts = (await request(client, "GET", f"/v1/jobs/{flaky_id}/attempts"))["items"]
        statuses = [attempt["status"] for attempt in attempts]
        if statuses != ["failed", "succeeded"]:
            raise RuntimeError(f"unexpected retry history: {statuses}")
        print(f"   {flaky_id}: {' -> '.join(statuses)}\n")

        print("4. A scheduled job can be cancelled before a worker claims it")
        run_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        created = await request(
            client,
            "POST",
            "/v1/jobs",
            json={"kind": "echo", "payload": {}, "run_at": run_at},
        )
        queued_id = created["job"]["id"]
        cancelled = await request(client, "POST", f"/v1/jobs/{queued_id}/cancel")
        if cancelled["status"] != "cancelled":
            raise RuntimeError(f"queued cancellation failed: {cancelled['status']}")
        print(f"   {queued_id}: cancelled\n")

        print("5. A business effect uses a unique key, even across job attempts")
        business_key = f"showcase-effect-{run_id}"
        created = await request(
            client,
            "POST",
            "/v1/jobs",
            json={
                "kind": "record_once",
                "payload": {"business_key": business_key, "value": {"amount": 42}},
            },
        )
        effect = await wait_for_status(client, created["job"]["id"], "succeeded")
        if effect["result"]["effect_inserted"] is not True:
            raise RuntimeError("first business effect was not inserted")
        print(f"   {business_key}: inserted once (crash replay is proved by tests)\n")

        if not quick:
            print("6. A running job notices a cancellation request")
            created = await request(
                client,
                "POST",
                "/v1/jobs",
                json={"kind": "sleep", "payload": {"seconds": 25}},
            )
            running_id = created["job"]["id"]
            await wait_for_status(client, running_id, "running", max_wait_seconds=30)
            await request(client, "POST", f"/v1/jobs/{running_id}/cancel")
            await wait_for_status(client, running_id, "cancelled", max_wait_seconds=30)
            print(f"   {running_id}: running -> cancellation requested -> cancelled\n")

        print("All showcase checks passed. Explore the job IDs in http://localhost:8000/docs")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--quick", action="store_true", help="skip the slower running-job cancellation check"
    )
    arguments = parser.parse_args()
    try:
        asyncio.run(run_showcase(arguments.base_url, quick=arguments.quick))
    except (httpx.HTTPError, RuntimeError, TimeoutError) as exc:
        parser.exit(1, f"Showcase failed: {exc}\nCheck that the API and worker are running.\n")


if __name__ == "__main__":
    main()
