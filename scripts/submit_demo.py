import argparse
import asyncio

import httpx


async def submit_jobs(*, base_url: str, count: int, kind: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=10) as client:
        for index in range(count):
            payload: dict[str, object]
            if kind == "flaky":
                payload = {"fail_attempts": 1, "sequence": index}
            elif kind == "sleep":
                payload = {"seconds": 2, "sequence": index}
            else:
                payload = {"message": f"hello-{index}"}

            response = await client.post(
                "/v1/jobs",
                json={
                    "kind": kind,
                    "payload": payload,
                    "max_attempts": 3,
                    "idempotency_key": f"demo-{kind}-{index}",
                },
            )
            response.raise_for_status()
            body = response.json()
            print(body["job"]["id"], body["job"]["status"], body["deduplicated"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--kind", choices=("echo", "sleep", "flaky"), default="echo")
    arguments = parser.parse_args()
    asyncio.run(
        submit_jobs(base_url=arguments.base_url, count=arguments.count, kind=arguments.kind)
    )


if __name__ == "__main__":
    main()
