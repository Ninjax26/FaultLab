# FaultLab

[![CI](https://github.com/Ninjax26/FaultLab/actions/workflows/ci.yml/badge.svg)](https://github.com/Ninjax26/FaultLab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

FaultLab is a PostgreSQL-backed distributed job runner built to make reliability mechanics
visible. A FastAPI control plane accepts jobs; independent Python or Go worker processes claim,
execute, retry, cancel, and recover them. This is a portfolio-grade systems project and a local
engineering lab, **not** a hosted multi-tenant production service.

**New to background jobs?** Start with the [from-scratch project and interview guide](docs/INTERVIEW_GUIDE.md).
**Want to see it work?** Open the [local job console](http://localhost:8000/dashboard) after
starting the stack, or follow the [five-minute showcase](docs/SHOWCASE.md).
**Reviewing the implementation?** Use the [engineering handbook](ENGINEERING_HANDBOOK.md) and
[change-review guide](docs/REVIEW_GUIDE.md).

## See it working

![FaultLab retry inspector showing a failed first attempt and successful retry](artifacts/demo/faultlab-retry-detail.png)

- [Watch the 14-second dashboard demo](artifacts/demo/faultlab-demo.mp4)
- [Open the full dashboard screenshot](artifacts/demo/faultlab-dashboard.png)

The recording creates a deliberately flaky job, shows it move through the queue, opens its
two-attempt history, and finishes on the cancelled-job view. Regenerate these artifacts against
a running local stack with `node scripts/capture_demo.cjs` and Playwright available on `NODE_PATH`.

## Deploy the portfolio demo

The included `render.yaml` provisions a free Render web service and PostgreSQL database in
Singapore. Because Render does not offer free background workers, the demo web container runs
the API and one Python worker as separate processes. Use separate API and worker services for a
paid or production-style deployment.

[Deploy FaultLab on Render](https://dashboard.render.com/blueprint/new?repo=https://github.com/Ninjax26/FaultLab)

After the deployment finishes, open `/dashboard`. The browser asks for HTTP Basic credentials:
the username is `faultlab`; reveal `FAULTLAB_ACCESS_TOKEN` in the Render service's Environment
page and use it as the password. `/health/live` and `/health/ready` remain public for platform
health checks. The free PostgreSQL database expires after 30 days and has no backups, so this is
a portfolio demo, not a production service.

```text
Browser console / HTTP client
             |
             v
          FastAPI ----> PostgreSQL jobs + attempts + business effects
                               ^
                               | short transactions, FOR UPDATE SKIP LOCKED
                        Python workers / Go workers
                               |
                          registered handlers
```

## What is implemented

- Atomic claims with `FOR UPDATE SKIP LOCKED`, bounded connection pools, priority, scheduling,
  and per-attempt history.
- Leases, heartbeat renewal, late-worker fencing, exponential retry, dead-letter state, and
  expired-lease recovery.
- HTTP submission idempotency with conflict detection and a separate `record_once` business
  effect protected by a unique key and value-conflict check.
- Cooperative running-job cancellation; queued cancellation is immediate. A handler that ignores
  cancellation can still finish successfully, by design.
- Python and Go workers that share the same job/attempt schema and lease protocol.
- FastAPI status, cancellation, and attempt-history endpoints, Prometheus metrics, optional OTLP
  tracing, Alembic migrations, Docker Compose, unit/integration tests, and GitHub Actions CI.
- A local operations console for creating safe demo jobs and inspecting recent status,
  payloads, results, leases, cancellation, and per-attempt history.
- Submit-time validation so only registered handler kinds enter the queue.
- Reproducible claim, end-to-end pipeline, and cross-runtime benchmarks under `reports/`.

FaultLab guarantees **at-least-once execution**, not exactly-once arbitrary side effects.
The `record_once` demo shows how a unique business key can make one database effect idempotent;
it does not make calls to external APIs exactly-once. Read [reliability notes](docs/RELIABILITY.md)
for the crash and cancellation boundaries.

## Evidence, not marketing numbers

- [Stage 1 claim report](reports/stage1-claiming.md): 15 runs, 7,500 total claims, zero duplicate
  or missing claims in that workload. This tests claiming, not completed job execution.
- [Stage 2-4 integration tests](tests/integration/test_reliability.py): child-process crashes
  after claim and after effect commit; real five-second lease expiry; recovery without a lost job
  or duplicate business effect; queued/running/crash-during-cancellation cases.
- [Stage 5 pipeline report](reports/stage5-pipeline.md): local 200-job workloads with enqueue,
  completion, p50/p95/p99 latency, backlog drain, connection, CPU, and memory observations.
- [Stage 6 runtime report](reports/stage6-runtimes.md): three repetitions per configuration,
  200 jobs per run, Python and Go *processes* on identical no-op jobs. The report gives medians
  and ranges; this trivial workload is not representative of real business handlers.
- [Connection-pressure report](reports/stage6-connections.md): each worker encountered a
  disposable PostgreSQL instance with all 100 connection slots held, then completed 10 jobs
  after those slots were released, with no lost jobs in this short experiment.

On this machine the repeated 2-worker no-op comparison measured median completion rates of
116.65 jobs/s (Python) and 344.62 jobs/s (Go), with median sampled worker RSS of 155.52 MiB
and 22.11 MiB respectively. Those are **local experimental observations**, not capacity or
production SLOs. Results vary, and the database/host were not isolated.

## Run it

Requirements: Docker Compose. For local Python commands, Python 3.12 and
[`uv`](https://docs.astral.sh/uv/). Go 1.23+ is needed only for the optional Go worker.

```bash
cp .env.example .env
docker compose up --build -d --wait
```

If another application already uses host port 5432, start Compose with
`FAULTLAB_POSTGRES_PORT=55434 docker compose up --build -d --wait` instead.
The API and worker still talk to PostgreSQL inside the Compose network.
If you run a Python or Go worker outside Compose, use the matching host port in its database URL.

Open the [job console](http://localhost:8000/dashboard),
[API docs](http://localhost:8000/docs), or [Prometheus](http://localhost:9090).
In the console, create a **Flaky** job and select it to watch a failed attempt become a
successful retry. Create a **Sleep** job to try running-job cancellation. The console shows
only the latest 200 jobs; its counts are not lifetime totals. It talks to the same API that
any external client would use. It is plain HTML, CSS, and JavaScript served by FastAPI, with no
separate frontend build step. **Do not expose the console or API publicly** without adding
authentication and other production controls.

For a self-checking walkthrough of retries, idempotency, cancellation, and the visible job
history, run `uv sync --no-editable` and then
`uv run --no-editable python scripts/showcase.py`. See the
[five-minute showcase guide](docs/SHOWCASE.md) for what to say and which screens to show.

```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind":"echo","payload":{"message":"hello"},"idempotency_key":"hello-001"}'
```

The response contains a job ID. Inspect it with `GET /v1/jobs/{id}` and its attempt history
with `GET /v1/jobs/{id}/attempts`. Submit a long-running `sleep` job and call
`POST /v1/jobs/{id}/cancel` to observe cooperative cancellation.
Submit `record_once` with `{"business_key":"invoice-123","value":{"amount":42}}` to see
the database-backed idempotent effect. Unknown `kind` values are rejected with HTTP 422.

To run locally instead of Compose:

```bash
docker compose up -d --wait postgres
uv sync --no-editable
uv run --no-editable alembic upgrade head
uv run --no-editable uvicorn faultlab.api.main:app --reload
```

In another terminal:

```bash
uv run --no-editable python -m faultlab.worker.main
```

To use Go against the same database:

```bash
cd go-worker
go build -o faultlab-go-worker .
./faultlab-go-worker --database-url 'postgres://faultlab:faultlab@localhost:5432/faultlab' --queue default
```

The Python and Go workers can run together. Use a unique worker ID for each Go process.
When you finish the demo, use `docker compose down` to stop its containers without deleting
the named PostgreSQL data volume.

## Verify and reproduce

```bash
make check
make test-integration
make go-test
make stage5-benchmark
make stage6-benchmark
make stage6-pressure
```

The integration/benchmark database is a disposable `postgres-test` Compose service on host
port 55433. Its data directory is tmpfs. The tests refuse a URL that does not contain `test`.
The benchmark scripts also enforce that guard. Do not run these against real data.

Read the [learning path](docs/LEARNING_PATH.md),
[from-scratch interview guide](docs/INTERVIEW_GUIDE.md),
[engineering handbook](ENGINEERING_HANDBOOK.md),
[implementation review guide](docs/REVIEW_GUIDE.md),
[architecture/invariants](docs/ARCHITECTURE.md),
[Stage 1 method](docs/STAGE1_CONCURRENCY.md), and
[benchmark method and limits](docs/BENCHMARKS.md).

## Scope and honest limitations

FaultLab executes only trusted registered handlers. Its hosted demo has a single shared HTTP
Basic password, but it has no user accounts, tenant isolation, quotas, arbitrary-code sandbox,
or backup/restore validation. Do not treat that demo gate as production identity or authorization.
`SKIP LOCKED` does not guarantee global fairness, and
high-throughput claims need a workload-specific index/vacuum review. Cancellation is cooperative.
The crash-after-external-side-effect problem remains unless that external system supports its
own idempotency key or is coordinated through an outbox/consumer protocol.

The resume-worthy claim is the **implemented failure protocol plus measured tests**, not
"exactly-once distributed processing" or an extrapolated jobs-per-second number.
