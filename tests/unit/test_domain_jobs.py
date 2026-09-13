import pytest

from faultlab.domain.jobs import retry_delay_seconds, submission_fingerprint


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, 2), (2, 4), (3, 8), (4, 10), (10, 10)],
)
def test_retry_delay_is_exponential_and_capped(attempt: int, expected: int) -> None:
    assert retry_delay_seconds(attempt, base_seconds=2, maximum_seconds=10) == expected


def test_retry_delay_rejects_zero_attempt() -> None:
    with pytest.raises(ValueError, match="attempt_number"):
        retry_delay_seconds(0, base_seconds=2, maximum_seconds=10)


def test_submission_fingerprint_is_stable_across_payload_key_order() -> None:
    first = submission_fingerprint(
        queue="default",
        kind="echo",
        payload={"b": 2, "a": 1},
        max_attempts=3,
    )
    second = submission_fingerprint(
        queue="default",
        kind="echo",
        payload={"a": 1, "b": 2},
        max_attempts=3,
    )
    assert first == second


def test_submission_fingerprint_changes_when_semantics_change() -> None:
    original = submission_fingerprint(
        queue="default",
        kind="echo",
        payload={"message": "one"},
        max_attempts=3,
    )
    changed = submission_fingerprint(
        queue="default",
        kind="echo",
        payload={"message": "two"},
        max_attempts=3,
    )
    assert original != changed
