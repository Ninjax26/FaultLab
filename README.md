# FaultLab

FaultLab is a PostgreSQL-backed distributed job runner built to make reliability mechanics
visible. A FastAPI control plane accepts jobs; independent Python or Go worker processes claim,
execute, retry, cancel, and recover them. This is a portfolio-grade systems project and a local
engineering lab, **not** a hosted multi-tenant production service.

```text
HTTP client -> FastAPI -> PostgreSQL jobs + attempts + business effects
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
- FastAPI status/cancellation endpoints, Prometheus metrics, optional OTLP tracing, Alembic
  migrations, Docker Compose, unit/integration tests, and GitHub Actions CI.
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
docker compose up --build
```

Open [API docs](http://localhost:8000/docs) or [Prometheus](http://localhost:9090).

```bash
curl -X POST http://localhost:8000/v1/jobs \
  -H 'Content-Type: application/json' \
  -d '{"kind":"echo","payload":{"message":"hello"},"idempotency_key":"hello-001"}'
```

The response contains a job ID. Inspect it with `GET /v1/jobs/{id}`. Submit a long-running
`sleep` job and call `POST /v1/jobs/{id}/cancel` to observe cooperative cancellation.
Submit `record_once` with `{"business_key":"invoice-123","value":{"amount":42}}` to see
the database-backed idempotent effect.

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
[architecture/invariants](docs/ARCHITECTURE.md),
[Stage 1 method](docs/STAGE1_CONCURRENCY.md), and
[benchmark method and limits](docs/BENCHMARKS.md).

## Scope and honest limitations

FaultLab executes only trusted registered handlers. It has no authentication, tenant isolation,
quotas, arbitrary-code sandbox, backup/restore validation, or production deployment. The API
should not be exposed publicly as-is. `SKIP LOCKED` does not guarantee global fairness, and
high-throughput claims need a workload-specific index/vacuum review. Cancellation is cooperative.
The crash-after-external-side-effect problem remains unless that external system supports its
own idempotency key or is coordinated through an outbox/consumer protocol.

The resume-worthy claim is the **implemented failure protocol plus measured tests**, not
"exactly-once distributed processing" or an extrapolated jobs-per-second number.
