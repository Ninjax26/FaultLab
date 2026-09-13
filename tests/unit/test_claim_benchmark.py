import pytest

from faultlab.benchmarking.claiming import (
    ClaimRunReport,
    ClaimSuiteReport,
    EnvironmentSummary,
    LatencySummary,
    percentile,
    redact_database_url,
    render_markdown,
    summarize_latency,
)


def test_percentile_interpolates_between_samples() -> None:
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5


def test_percentile_rejects_empty_sample() -> None:
    with pytest.raises(ValueError, match="empty"):
        percentile([], 0.5)


def test_latency_summary_has_expected_quantiles() -> None:
    summary = summarize_latency([float(value) for value in range(1, 101)])
    assert summary.mean == 50.5
    assert summary.p50 == 50.5
    assert summary.p95 == 95.05
    assert summary.p99 == 99.01
    assert summary.maximum == 100


def test_database_password_is_redacted() -> None:
    url = "postgresql+asyncpg://faultlab:secret@localhost:5432/faultlab"
    assert redact_database_url(url) == ("postgresql+asyncpg://***:***@localhost:5432/faultlab")


def test_markdown_report_includes_correctness_metrics() -> None:
    run = ClaimRunReport(
        repetition=1,
        queue="test",
        jobs_seeded=100,
        worker_count=4,
        pool_size=6,
        duration_seconds=1.0,
        throughput_jobs_per_second=100.0,
        latency_ms=LatencySummary(mean=1, p50=1, p95=2, p99=3, maximum=4),
        duplicate_claims=0,
        missing_claims=0,
        max_database_connections=4,
        worker_claims={"worker-0": 25},
        worker_claim_coefficient_of_variation=0,
        claim_query_plan=["Index Scan"],
    )
    markdown = render_markdown(
        ClaimSuiteReport(
            generated_at="2026-08-30T00:00:00+00:00",
            database_url_redacted="postgresql://***",
            environment=EnvironmentSummary(
                python_version="3.12",
                operating_system="test-os",
                machine="test-machine",
                processor="test-processor",
                logical_cpu_count=8,
                postgres_version="PostgreSQL test",
                postgres_max_connections="100",
                postgres_shared_buffers="128MB",
                postgres_work_mem="4MB",
            ),
            runs=[run],
        )
    )
    assert "Duplicates" in markdown
    assert "| 4 | 1 | 100 |" in markdown
