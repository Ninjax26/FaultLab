# Stage 1: prove concurrent claiming

Stage 1 turns the statement “multiple workers safely claim different jobs” into a reproducible
test and benchmark. It does not measure handler execution yet.

## What was added

- A lock-contention test proving a second worker skips an uncommitted locked row.
- Three repeated runs of 250 jobs claimed by 24 concurrent workers.
- Assertions for zero duplicate claims and exactly one attempt record per job.
- A benchmark across configurable worker counts.
- p50, p95, p99, mean, and maximum claim latency.
- Claim throughput, missing/duplicate claims, worker distribution, and connection usage.
- `EXPLAIN (ANALYZE, BUFFERS)` output for the claim query in every benchmark run.
- A disposable PostgreSQL test service on port 55433.

## Why the benchmark is structured this way

The timed section contains only this short transaction:

```text
BEGIN
  SELECT one runnable row FOR UPDATE SKIP LOCKED
  mark it RUNNING and assign a lease
  insert one attempt record
COMMIT
```

No handler sleeps, HTTP calls, or other external work occurs while the row lock is held. Long
transactions would measure handler duration rather than queue contention and would make other
workers wait unnecessarily.

Jobs are seeded in batches, and the SQLAlchemy pool is bounded with `max_overflow=0`. This
prevents a benchmark with many coroutines from silently opening an uncontrolled number of
PostgreSQL connections.

## Run the correctness tests

Start Docker Desktop, then run:

```bash
make test-integration
```

The test database uses an in-memory `tmpfs` volume and is exposed on port 55433. The test fixture
refuses any URL that does not visibly contain `test` before dropping or creating tables.

Expected result:

```text
5 passed
```

The two important tests are:

1. One worker holds the first row lock open while another worker must claim the second row in
   under one second.
2. Twenty-four workers claim 250 jobs, repeated three times, with no duplicated job IDs.

## Run the benchmark

```bash
make stage1-benchmark
```

The default matrix is:

```text
500 jobs
worker counts: 1, 2, 4, 8, 16
3 repetitions per worker count
18 pooled PostgreSQL connections
```

Reports are written to:

```text
reports/stage1-claiming.json
reports/stage1-claiming.md
```

For a smaller smoke run:

```bash
uv run --no-editable python scripts/benchmark_claiming.py \
  --jobs 100 \
  --worker-counts 1,4,8 \
  --repetitions 1 \
  --pool-size 10 \
  --output reports/stage1-smoke
```

## How to interpret the report

### Duplicate and missing claims

Both must be zero. A high throughput result with duplicates or missing jobs is invalid.

### Throughput

Throughput should initially improve as workers increase. Eventually it will flatten or fall
because database connections, CPU, transaction commits, or lock/index work dominate.

### Tail latency

p95 and p99 show what slower claims experience. If the median stays low while p99 rises sharply,
some workers are waiting for connections or contending inside PostgreSQL.

### Maximum database connections

This should not exceed the configured main pool size. The monitoring connection uses a separate
one-connection pool and is not included in the count.

### Worker distribution

Perfect equality is not expected. `SKIP LOCKED` optimizes throughput, not strict fairness. A high
coefficient of variation means a small number of workers claimed most jobs and warrants further
investigation.

### Claim query plan

The JSON report includes the actual `EXPLAIN (ANALYZE, BUFFERS)` output. Inspect it before changing
indexes. A sequential scan can be acceptable for a tiny table; it becomes suspicious when the
table is large and most rows are terminal.

## Exit condition

Stage 1 is complete only when:

- Integration tests pass repeatedly on PostgreSQL.
- Every benchmark run has zero duplicate and zero missing claims.
- The report records the machine/database context used for any published number.
- You can explain why `SKIP LOCKED` improves throughput but does not guarantee fairness.
