import pytest

from faultlab.worker.handlers import JobContext, UnknownJobKindError, flaky, get_handler, sleep


def context(attempt: int = 1) -> JobContext:
    return JobContext(job_id="job-1", attempt_number=attempt, worker_id="worker-1")


async def test_flaky_handler_exposes_retry_behavior() -> None:
    with pytest.raises(RuntimeError, match="intentional failure"):
        await flaky({"fail_attempts": 1}, context(attempt=1))

    assert await flaky({"fail_attempts": 1}, context(attempt=2)) == {"succeeded_on_attempt": 2}


async def test_sleep_handler_rejects_unbounded_sleep() -> None:
    with pytest.raises(ValueError, match="between 0 and 30"):
        await sleep({"seconds": 31}, context())


def test_unknown_handler_is_explicit() -> None:
    with pytest.raises(UnknownJobKindError, match="not-registered"):
        get_handler("not-registered")


def test_registered_kinds_match_handler_table() -> None:
    from faultlab.worker.handlers import HANDLERS, registered_kinds

    assert registered_kinds() == frozenset(HANDLERS)
