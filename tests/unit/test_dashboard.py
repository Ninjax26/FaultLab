from httpx import ASGITransport, AsyncClient

from faultlab.api.main import app


async def test_dashboard_and_local_assets_are_served() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        page = await client.get("/dashboard")
        assert page.status_code == 200
        assert "Job console" in page.text
        assert "latest 200 jobs" in page.text

        stylesheet = await client.get("/dashboard/assets/dashboard.css")
        assert stylesheet.status_code == 200
        assert "text/css" in stylesheet.headers["content-type"]

        script = await client.get("/dashboard/assets/dashboard.js")
        assert script.status_code == 200
        assert "/v1/jobs?limit=200" in script.text
