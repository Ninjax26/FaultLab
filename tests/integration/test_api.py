from collections.abc import AsyncIterator
from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from faultlab.api.main import app
from faultlab.db.session import get_session


async def test_http_submission_idempotency_and_cancellation(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    async def test_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = test_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/health/live")).json() == {"status": "ok"}
            assert (await client.get("/health/ready")).json() == {"status": "ready"}
            payload = {
                "kind": "echo",
                "payload": {"message": "hello"},
                "idempotency_key": "http-request-1",
            }
            first = await client.post("/v1/jobs", json=payload)
            assert first.status_code == 202
            job_id = first.json()["job"]["id"]
            assert first.json()["deduplicated"] is False

            repeated = await client.post("/v1/jobs", json=payload)
            assert repeated.status_code == 202
            assert repeated.json()["job"]["id"] == job_id
            assert repeated.json()["deduplicated"] is True

            conflict = await client.post(
                "/v1/jobs", json={**payload, "payload": {"message": "changed"}}
            )
            assert conflict.status_code == 409

            listed = await client.get("/v1/jobs", params={"status": "pending"})
            assert listed.status_code == 200
            assert len(listed.json()["items"]) == 1
            assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 200

            attempts = await client.get(f"/v1/jobs/{job_id}/attempts")
            assert attempts.status_code == 200
            assert attempts.json()["items"] == []
            missing = await client.get(f"/v1/jobs/{uuid4()}/attempts")
            assert missing.status_code == 404

            unknown = await client.post("/v1/jobs", json={"kind": "not-registered", "payload": {}})
            assert unknown.status_code == 422

            cancelled = await client.post(f"/v1/jobs/{job_id}/cancel")
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"
            assert cancelled.json()["cancellation_requested_at"] is not None
    finally:
        app.dependency_overrides.pop(get_session, None)
