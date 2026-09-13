import logging
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncEngine

from faultlab.config import Settings

logger = logging.getLogger(__name__)

JOBS_CLAIMED = Counter(
    "faultlab_jobs_claimed_total",
    "Number of jobs claimed by workers",
    labelnames=("queue", "kind"),
)
JOBS_FINISHED = Counter(
    "faultlab_jobs_finished_total",
    "Number of completed executions",
    labelnames=("queue", "kind", "outcome"),
)
LEASES_RECOVERED = Counter(
    "faultlab_leases_recovered_total",
    "Number of expired worker leases recovered",
    labelnames=("queue",),
)
JOB_DURATION = Histogram(
    "faultlab_job_duration_seconds",
    "Duration of job-handler execution",
    labelnames=("queue", "kind"),
)


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def configure_tracing(
    *,
    settings: Settings,
    service_name: str,
    app: FastAPI | None = None,
    engine: AsyncEngine | None = None,
) -> None:
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        logger.info("OTLP exporter is disabled because no endpoint is configured")
        return

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service_name,
                "deployment.environment.name": settings.environment,
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)

    if app is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    if engine is not None:
        SQLAlchemyInstrumentor().instrument(
            engine=engine.sync_engine,
            tracer_provider=provider,
        )


def add_job_span_attributes(span: Any, *, job_id: str, queue: str, kind: str) -> None:
    span.set_attribute("faultlab.job.id", job_id)
    span.set_attribute("faultlab.job.queue", queue)
    span.set_attribute("faultlab.job.kind", kind)
