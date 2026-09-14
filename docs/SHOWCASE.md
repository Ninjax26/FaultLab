# Showcasing FaultLab

## What this project is, in plain English

Imagine an app where users upload a video. Processing the video can take minutes, so the web
request should not stay open. The app places a **job** in a queue and returns immediately. A
separate **worker** picks it up and processes it. FaultLab is a small version of that infrastructure.
It focuses on what happens when workers fail, work must be retried, or a user cancels a job.

PostgreSQL is both the queue and the record of what happened. The FastAPI service accepts and
shows jobs. Python and Go workers can process the same queue. Prometheus exposes operational
metrics. The demo handlers do safe, fake work; FaultLab is an infrastructure demonstration,
not a video-processing product.

## Five-minute live demo

From the repository root, with Docker running:

```bash
cp .env.example .env
docker compose up --build -d --wait
uv sync --no-editable
uv run --no-editable python scripts/showcase.py
```

Each run creates fresh demo jobs and checks the result. Use `--quick` to skip the running-job
cancellation step. The full demo usually waits a few seconds for retry and cancellation. If the
command fails, check `docker compose ps` and `docker compose logs worker api`.

What to point out as it runs:

1. **Normal job:** The API accepts work; the worker completes it asynchronously. Open its job ID
   at `GET /v1/jobs/{id}` in [the API docs](http://localhost:8000/docs).
2. **Duplicate submission:** Two requests using one idempotency key return one job ID. This
   protects against a client retrying the *request*.
3. **Retry:** A `flaky` handler fails on purpose. The attempt-history endpoint shows `failed`
   then `succeeded`, so failure was not silently hidden.
4. **Cancellation:** A future job is cancelled immediately; a running `sleep` job receives a
   cooperative cancellation request. Cancellation is not a guarantee for uncooperative handlers.
5. **Business effect:** `record_once` writes a uniquely keyed database effect. The live demo
   inserts it once; the [crash-replay integration test](../tests/integration/test_reliability.py)
   proves that retrying after the effect commits does not insert a second copy.

Open [Prometheus](http://localhost:9090) and query `faultlab_jobs_total` or inspect available
series with `{__name__=~"faultlab_.*"}`. For measured performance and experiment boundaries,
show the [pipeline report](../reports/stage5-pipeline.md),
[runtime comparison](../reports/stage6-runtimes.md), and
[connection-pressure report](../reports/stage6-connections.md). These are local measurements,
not promises of production throughput.

## A 30-second explanation for a recruiter

"FaultLab is a distributed background-job system I built with Python, PostgreSQL, and a Go
worker. The interesting part is failure handling: workers claim jobs atomically, renew leases,
and recover work after a crash. I added retries, cancellation, idempotency, metrics, and tests
that deliberately kill workers. I benchmarked the end-to-end pipeline and compared Python and
Go workers under the same local workload."

## Questions to prepare for an interview

- Why can a job run twice, even when only one worker owns its lease at a time?
- What does `FOR UPDATE SKIP LOCKED` prevent? What does it *not* guarantee?
- What happens if a worker crashes after committing a side effect but before marking success?
- Why does a lease need a heartbeat and an ownership/fencing check?
- What do the reported benchmark numbers measure, and what would change on another machine?

Be ready to answer with the [architecture](ARCHITECTURE.md) and
[reliability notes](RELIABILITY.md) open. Do not claim exactly-once arbitrary side effects,
global queue fairness, or production readiness. The API has no authentication or tenant
isolation and should only be run locally or behind appropriate controls.

## Suggested resume bullet

> Built FaultLab, a PostgreSQL-backed distributed job runner with Python and Go workers,
> atomic job claims, lease-based crash recovery, retries, cooperative cancellation, and
> idempotent database effects; validated failure behavior with integration tests and measured
> throughput/latency in reproducible local benchmarks.

Only use this bullet after you can explain and run the project yourself. If space is tight,
keep the failure protocol and one measured result, not a long list of technologies.
