import base64
import binascii
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import make_asgi_app

from faultlab.api.routes import health, jobs
from faultlab.config import get_settings
from faultlab.db.session import engine
from faultlab.observability import configure_logging, configure_tracing

settings = get_settings()
configure_logging(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await engine.dispose()


app = FastAPI(
    title="FaultLab",
    version="0.1.0",
    description="A learning-first distributed job execution platform.",
    lifespan=lifespan,
)


def valid_basic_credentials(authorization: str | None, access_token: str) -> bool:
    """Validate the fixed demo username and configured password without timing leaks."""
    if authorization is None or not authorization.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
        username, password = decoded.split(":", 1)
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return secrets.compare_digest(username, "faultlab") and secrets.compare_digest(
        password, access_token
    )


@app.middleware("http")
async def protect_public_deployment(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Require HTTP Basic auth when an access token is configured."""
    if request.url.path in {"/health/live", "/health/ready"} or settings.access_token is None:
        return await call_next(request)
    if valid_basic_credentials(request.headers.get("authorization"), settings.access_token):
        return await call_next(request)
    return JSONResponse(
        status_code=401,
        content={"detail": "Authentication required"},
        headers={"WWW-Authenticate": 'Basic realm="FaultLab"'},
    )


app.include_router(health.router)
app.include_router(jobs.router)
app.mount("/metrics", make_asgi_app())

dashboard_dir = Path(__file__).resolve().parent / "static"
app.mount("/dashboard/assets", StaticFiles(directory=dashboard_dir), name="dashboard-assets")


@app.get("/dashboard", include_in_schema=False)
@app.get("/dashboard/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(dashboard_dir / "index.html")


configure_tracing(
    settings=settings,
    service_name="faultlab-api",
    app=app,
    engine=engine,
)
