import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


class JobCancellationRequested(Exception):
    """Raised only at a handler-defined safe cancellation boundary."""


@dataclass(frozen=True, slots=True)
class JobContext:
    job_id: str
    attempt_number: int
    worker_id: str
    cancellation_event: asyncio.Event | None = None
    record_effect: Callable[[str, dict[str, Any]], Awaitable[bool]] | None = None

    def check_cancelled(self) -> None:
        if self.cancellation_event is not None and self.cancellation_event.is_set():
            raise JobCancellationRequested("job cancellation requested")


JobHandler = Callable[[dict[str, Any], JobContext], Awaitable[dict[str, Any]]]


class UnknownJobKindError(RuntimeError):
    pass


async def echo(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    return {
        "echo": payload,
        "attempt_number": context.attempt_number,
        "worker_id": context.worker_id,
    }


async def sleep(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    seconds = float(payload.get("seconds", 1))
    if not 0 <= seconds <= 30:
        raise ValueError("sleep seconds must be between 0 and 30")
    remaining = seconds
    while remaining > 0:
        context.check_cancelled()
        step = min(0.1, remaining)
        await asyncio.sleep(step)
        remaining -= step
    context.check_cancelled()
    return {"slept_seconds": seconds}


async def sleep_uncooperative(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    """Deliberately ignores cancellation to demonstrate its cooperative limit."""

    del context
    seconds = float(payload.get("seconds", 1))
    if not 0 <= seconds <= 30:
        raise ValueError("sleep seconds must be between 0 and 30")
    await asyncio.sleep(seconds)
    return {"slept_seconds": seconds}


async def record_once(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    """Demonstrate an effect committed before the job completion transaction."""

    if context.record_effect is None:
        raise RuntimeError("record_once requires a database-backed worker context")
    key = payload.get("business_key")
    if not isinstance(key, str):
        raise ValueError("business_key must be a string")
    value = payload.get("value", {})
    if not isinstance(value, dict):
        raise ValueError("value must be an object")
    inserted = await context.record_effect(key, value)
    context.check_cancelled()
    return {"business_key": key, "effect_inserted": inserted}


async def benchmark_noop(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    del payload, context
    return {}


async def flaky(payload: dict[str, Any], context: JobContext) -> dict[str, Any]:
    """Fail the first N attempts so retry behavior can be observed locally."""

    fail_attempts = int(payload.get("fail_attempts", 1))
    if fail_attempts < 0:
        raise ValueError("fail_attempts must be nonnegative")
    if context.attempt_number <= fail_attempts:
        raise RuntimeError(f"intentional failure on attempt {context.attempt_number}")
    return {"succeeded_on_attempt": context.attempt_number}


HANDLERS: dict[str, JobHandler] = {
    "echo": echo,
    "sleep": sleep,
    "sleep_uncooperative": sleep_uncooperative,
    "flaky": flaky,
    "record_once": record_once,
    "benchmark-noop": benchmark_noop,
}


def registered_kinds() -> frozenset[str]:
    return frozenset(HANDLERS)


def get_handler(kind: str) -> JobHandler:
    try:
        return HANDLERS[kind]
    except KeyError as exc:
        raise UnknownJobKindError(f"no handler is registered for job kind '{kind}'") from exc
