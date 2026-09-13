import asyncio
import os
import signal
import socket

from prometheus_client import start_http_server

from faultlab.config import get_settings
from faultlab.db.session import engine, session_factory
from faultlab.observability import configure_logging, configure_tracing
from faultlab.worker.runtime import Worker


async def async_main() -> None:
    settings = get_settings()
    configure_logging(settings)
    configure_tracing(
        settings=settings,
        service_name="faultlab-worker",
        engine=engine,
    )

    if settings.worker_metrics_port:
        start_http_server(settings.worker_metrics_port)

    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    worker = Worker(
        settings=settings,
        session_factory=session_factory,
        worker_id=worker_id,
    )

    loop = asyncio.get_running_loop()
    for signal_name in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signal_name, worker.stop)

    try:
        await worker.run()
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
