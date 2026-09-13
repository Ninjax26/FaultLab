from fastapi import FastAPI
from prometheus_client import make_asgi_app

from faultlab.api.routes import health, jobs
from faultlab.config import get_settings
from faultlab.db.session import engine
from faultlab.observability import configure_logging, configure_tracing

settings = get_settings()
configure_logging(settings)

app = FastAPI(
    title="FaultLab",
    version="0.1.0",
    description="A learning-first distributed job execution platform.",
)
app.include_router(health.router)
app.include_router(jobs.router)
app.mount("/metrics", make_asgi_app())

configure_tracing(
    settings=settings,
    service_name="faultlab-api",
    app=app,
    engine=engine,
)
