# Benchmark method and limits

All reports use the disposable PostgreSQL 17 `postgres-test` service and include the host,
PostgreSQL settings, worker count, payload, and pool size. Keep the host idle and repeat runs
before comparing changes. The generated JSON is the raw evidence; Markdown is a summary.

## Stage 1: claim-only

`scripts/benchmark_claiming.py` seeds jobs, runs concurrent claimers, records duplicate and
missing IDs, claim latency, throughput, connection use, worker distribution, and `EXPLAIN
(ANALYZE, BUFFERS)`. A claim is not a completed job. The original 15-run report had no
duplicate or missing claims, but throughput was highly variable, so no headline throughput
claim should be taken from it.

## Stage 5: full Python worker pipeline

`scripts/benchmark_pipeline.py` bulk-enqueues `benchmark-noop` jobs, starts real Python
workers, and samples backlog until all jobs are terminal. It reports enqueue and completion
rates, end-to-end p50/p95/p99 latency (`finished_at - created_at`), maximum observed database
connections, worker-process CPU, RSS, and PostgreSQL transaction/block/tuple counter deltas.
The backlog drain rate is the completion rate. PostgreSQL CPU/memory are **not** directly
sampled. The Python `ru_maxrss` measurement is a cumulative process peak, not an isolated
per-case peak. This is a no-op handler workload with batched direct-SQL enqueue, not API
throughput or a real-world task mix. Container CPU/memory limits are not pinned.

## Stage 6: process-to-process comparison

`scripts/benchmark_runtimes.py` launches actual Python or Go worker processes on the same
job shape and database. It alternates runtime order across repetitions. It records rates,
latencies, time from launch to first completion, sampled sum of worker RSS, child CPU time,
connection counts, and backlog samples. `First completion` includes startup, claim, and one
no-op handler, so it is not pure binary startup time. Each Go worker uses a one-connection
pool in this comparison; Python uses SQLAlchemy's configured pool. The Python/Go results are
not normalized for connection-pool size or runtime instrumentation. They are evidence for
this workload only. Go and Python mixed-queue recovery is separately tested in CI.

The benchmark does **not** intentionally exhaust PostgreSQL's global `max_connections`,
does not simulate multi-region latency, and does not measure sustained production capacity.
These are useful next experiments, not claims the project has already proven.

## Commands

```bash
make test-integration
make stage5-benchmark
make stage6-benchmark
```

Reports are in `reports/`. Never place a raw database password or a throughput number without
the hardware, workload, and run context into a resume or README.
