# FaultLab Engineering Handbook

This document explains the implementation contracts that make FaultLab correct. Setup and the
short demo live in [README.md](README.md); the beginner narrative and interview answers live in
[docs/INTERVIEW_GUIDE.md](docs/INTERVIEW_GUIDE.md); review rules live in
[docs/REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md).

## System boundary

FaultLab is a local, PostgreSQL-backed background-job platform. FastAPI accepts and exposes jobs.
Independent Python or Go worker processes poll the same database, claim work, execute one of a
small set of trusted handlers, and record the result. PostgreSQL is the queue and the authoritative
state store. Prometheus and optional OpenTelemetry report behavior; neither is part of correctness.
The browser console is an operator view over the HTTP API, not another authority.

The implementation deliberately excludes authentication, tenants, arbitrary user code, a public
deployment, distributed rate limiting, and exactly-once external effects. Treat it as a systems
lab with production-style failure mechanics, not a complete hosted product.

```text
Browser console / HTTP client
             |
             v
          FastAPI ----> PostgreSQL jobs + attempts + business effects
                               ^
                               | atomic transactions and row locks
                       Python worker / Go worker
                               |
                          trusted handlers
```

## Authoritative records

`jobs` is the current state of each logical job. Important fields are:

- `id`, `queue`, and `kind`: stable job identity, routing lane, and handler name;
- `payload` and `result`: JSON input and successful output;
- `status`, `run_at`, `priority`: scheduling state and claim ordering;
- `attempt_count`, `max_attempts`: consumed and allowed execution attempts;
- `lease_owner`, `lease_expires_at`: temporary execution ownership;
- `cancellation_requested_at`: durable request to stop a running job;
- `idempotency_key`, `submission_fingerprint`: duplicate-submission protection;
- timestamps and `last_error`: operator-visible history and terminal information.

`job_attempts` is the per-execution audit trail. A claim creates a new numbered attempt in the
same transaction that changes the job to `running`. Failure, cancellation, lease expiry, or
success finishes that attempt instead of rewriting history to look successful.

`business_effects` is a deliberately small idempotent-consumer example. Its primary key is the
business operation identifier. It demonstrates one database effect committed independently of
job completion; it is not a general external exactly-once mechanism.

SQLAlchemy models are in [src/faultlab/db/models.py](src/faultlab/db/models.py). Alembic owns the
deployed schema in [migrations/versions](migrations/versions); changing a model alone does not
change an existing database.

## State machines

The job states are:

```text
                       claim
pending / retry_pending -----> running -----> succeeded
                                  |
                                  +----------> retry_pending --+
                                  |                            |
                                  +----------> dead            +-- claim again
                                  |
                                  +----------> cancelled
```

The exact transition depends on ownership, cancellation, and remaining attempts:

| Current state | Event | Next state |
| --- | --- | --- |
| `pending`, `retry_pending` | eligible atomic claim | `running` |
| `running` | handler succeeds with valid lease | `succeeded` |
| `running` | handler fails, attempts remain | `retry_pending` |
| `running` | handler fails, budget exhausted | `dead` |
| `pending`, `retry_pending` | cancellation request | `cancelled` |
| `running` | cooperative handler observes request | `cancelled` |
| `running` | lease expires, attempts remain | `retry_pending` |
| `running` | lease expires, budget exhausted | `dead` |
| `running` | lease expires after cancellation request | `cancelled` |

Attempt states are `running`, `succeeded`, `failed`, `lease_expired`, and `cancelled`. A job can
have several failed or expired attempts and one final successful attempt. Terminal jobs
(`succeeded`, `dead`, `cancelled`) are never claimable.

Core enums and deterministic retry/fingerprint functions live in
[src/faultlab/domain/jobs.py](src/faultlab/domain/jobs.py). All database transitions live in
[src/faultlab/repositories/jobs.py](src/faultlab/repositories/jobs.py) so reviewers have one
place to check the invariants.

## API admission and read paths

`POST /v1/jobs` validates queue/kind lengths, priority, attempt budget, timezone-aware scheduling,
and submission-idempotency input. Only kinds registered in the Python handler table are accepted.
The API returns `202 Accepted` because execution happens later. It does not imply completion.

An omitted `run_at` becomes the current UTC time. A future `run_at` remains `pending` but is not
claimable until that instant. List calls accept an optional queue and status and are bounded to
200 results. `GET /v1/jobs/{id}/attempts` returns ascending attempt order. Cancellation is an
idempotent request for terminal jobs: the current terminal record is returned unchanged.

HTTP routes open explicit transactions only for writes. Repository exceptions are translated to
stable HTTP semantics: missing resources are `404`; semantic reuse of an idempotency key is `409`;
Pydantic admission failures are `422`.

The API currently returns payloads and results directly. This is acceptable for trusted local
demo data and unsafe for secrets or multi-tenant workloads.

## Submission idempotency

For every submission, FaultLab canonicalizes `queue`, `kind`, `payload`, and `max_attempts` into
JSON and stores a SHA-256 fingerprint. `(queue, idempotency_key)` has a unique constraint.

The insert uses `INSERT ... ON CONFLICT DO NOTHING`:

1. a new key inserts one job and reports `deduplicated: false`;
2. an equivalent repeat loads and returns that job with `deduplicated: true`;
3. the same key with a different fingerprint is rejected as a conflict.

Priority and `run_at` are intentionally not part of the fingerprint today. Changing that policy
is an API compatibility decision, not a refactor. An omitted key means every request creates a
new logical job because PostgreSQL permits multiple `NULL` values in the unique constraint.

## Atomic claim transaction

The correctness-critical claim is conceptually:

```sql
BEGIN;
SELECT ... FROM jobs
WHERE queue = :queue
  AND status IN ('pending', 'retry_pending')
  AND run_at <= now()
ORDER BY priority DESC, run_at ASC, created_at ASC
LIMIT 1
FOR UPDATE SKIP LOCKED;

UPDATE the locked row:
  status = 'running', attempt_count += 1,
  lease_owner = :worker, lease_expires_at = now() + :lease;

INSERT the matching running attempt;
COMMIT;
```

The selection, state change, attempt increment, and attempt insert must stay in one transaction.
Splitting them creates a race in which two workers can read the same pending job. `SKIP LOCKED`
lets competing workers continue to different rows rather than block behind the first lock.

Claim ordering prefers higher priority, then earlier schedule, then earlier creation. It is useful
but does not provide strict global fairness; sustained high-priority arrivals can starve lower
priority work. Any change to the predicate or order needs an `EXPLAIN` review against the claim
index and a concurrency benchmark.

## Python worker lifecycle

[src/faultlab/worker/runtime.py](src/faultlab/worker/runtime.py) runs this loop:

1. recover expired leases for its queue;
2. atomically claim one eligible job;
3. wait for the configured poll interval when no job exists;
4. execute the handler outside the claim transaction;
5. heartbeat in a separate asyncio task;
6. record success, failure, or cancellation in a short transaction;
7. continue even when a loop-level database operation fails.

Handler work never runs inside the claim transaction. Holding the row lock for the duration of a
slow handler would serialize workers, retain database resources, and make crash recovery harder.
The worker accepts `SIGINT` and `SIGTERM`, stops claiming new jobs, and disposes its engine after
the current loop exits. An abrupt process death is handled by lease expiration.

This implementation processes one job at a time per worker process. Scale local concurrency by
running multiple worker processes, not by assuming one worker contains a hidden task pool.

## Leases, heartbeat, observation, and fencing

A lease is temporary permission to finish one job. `lease_owner` alone is insufficient because a
crashed worker would own the job forever. `lease_expires_at` creates a recovery boundary.

The Python heartbeat task checks ownership/cancellation at most once per second and extends the
lease at the configured heartbeat interval. Separating cheap observation from renewal makes a
running cancellation visible sooner than a ten-second renewal interval. A heartbeat exception or
invalid lease sets the handler cancellation event: the worker must stop at its next safe point.

Every success/failure/cancellation transition locks the job again and verifies:

- status is still `running`;
- `lease_owner` still equals this worker;
- `lease_expires_at` exists and is still in the future.

This is fencing. If worker A pauses until its lease expires and worker B later claims the job,
worker A cannot publish a late result. Fencing protects FaultLab's rows; it cannot reverse an
external side effect already performed by A.

Configuration enforces `heartbeat < lease`. In a real deployment, the safety margin must also
cover scheduler delay, database latency, and pauses. A one-second difference is valid by schema
but operationally weak.

## Lease recovery, retry, and dead jobs

Every worker can run the reaper. It locks expired rows in batches of 100 with `SKIP LOCKED`, so
multiple reapers can safely divide recovery work. For each expired job, it finishes the current
attempt as `lease_expired`, clears ownership, and chooses `retry_pending`, `dead`, or `cancelled`.

Retry delay is deterministic exponential backoff:

```text
delay = min(retry_max, retry_base * 2 ** (attempt_number - 1))
```

There is no jitter. That keeps tests and demonstrations deterministic but could synchronize a
large production retry wave. `max_attempts` counts claims, including a claim that ended through
lease expiry. Once the budget is consumed, another claim is forbidden and the job becomes `dead`.
FaultLab records dead jobs but does not implement a dead-letter replay endpoint.

The reaper and normal failure path must calculate retry timing identically across Python and Go.

## Cancellation contract

Queued cancellation locks the row and immediately sets `cancelled`, request time, and finish time.
A cancelled queued job cannot match the claim predicate.

Running cancellation records `cancellation_requested_at` but leaves the job `running`. The
heartbeat/observation loop signals the handler. Cooperative handlers call `check_cancelled()` at
safe boundaries and the worker then records the attempt/job as cancelled while it still owns the
lease. `sleep` checks every 100 ms in Python; the Go handler waits on a cancellable context.

`sleep_uncooperative` deliberately ignores cancellation. It may succeed even when a request is
stored. This is correct for the documented contract: forcibly stopping unknown code could leave
partial effects. A real handler designer must define where cancellation is safe.

If a worker crashes after cancellation is requested, lease recovery makes the job `cancelled`
instead of retrying it.

## Handler contract and built-in handlers

A Python handler accepts a JSON-like payload and `JobContext`, then returns a JSON-like result or
raises an exception. It may use the job ID, attempt number, worker ID, cancellation token, and the
database effect callback. Registered handlers are trusted code:

| Kind | Purpose |
| --- | --- |
| `echo` | normal success and visible worker/attempt metadata |
| `flaky` | deterministic failures followed by a retry success |
| `sleep` | bounded cooperative cancellation |
| `sleep_uncooperative` | demonstrates the limit of cancellation |
| `record_once` | database-backed idempotent effect |
| `benchmark-noop` | deliberately trivial performance workload |

Payload validation inside handlers becomes execution failure and may retry. Admission validation
currently validates the handler name but not a per-kind payload schema. Adding untrusted or
arbitrary code execution would cross a major security boundary and needs sandboxing, resource
limits, network policy, and a new threat model.

## Execution idempotency and the crash-after-effect boundary

Submission idempotency stops duplicate *jobs*. It does not stop one accepted job from executing
again after ambiguous failure.

`record_once` inserts `(business_key, value)` with conflict-ignore behavior. An existing matching
value means the effect already happened; a different value under the same key is an explicit
error. Its effect transaction commits separately from job completion so tests can reproduce:

```text
effect commits -> worker crashes -> lease expires -> job retries -> effect is not inserted twice
```

This is at-least-once execution with an idempotent database effect. For email, payments, or another
service, pass a stable idempotency key to that service or use an outbox/consumer design whose
transaction boundary includes the durable intent. Do not describe FaultLab as exactly-once.

## Python and Go protocol compatibility

The Go worker is a second implementation of the database protocol, not a separate service API.
It uses `pgxpool`, executes equivalent claim/reap/renew/finish SQL, supports the same built-in
handlers, and reads the same timing/backoff environment variables. Both runtimes can process one
queue concurrently because PostgreSQL locking is the coordination mechanism.

Cross-runtime invariants include status strings, retry calculation, attempt creation/finishing,
error truncation, cancellation outcome, `record_once` value comparison, and lease fencing. A
change to any of these is a protocol change and must be implemented and tested in both workers.

Known asymmetries are intentional and must remain visible:

- the Python worker exports Prometheus metrics and optional OpenTelemetry traces; the Go worker
  currently logs but does not expose equivalent telemetry;
- Python polls every configured float interval (one second by default); Go uses a millisecond
  integer setting (100 ms by default);
- the runtime benchmark constrains Go pools differently and reports that context.

Do not interpret the Go implementation as proof of general language performance.

## Database connections and backpressure

The shared SQLAlchemy engine uses `pool_size=10`, `max_overflow=20`, and pre-ping. The Go worker
defaults to four pooled connections. Multiply these settings by process count before choosing a
deployment worker count. PostgreSQL connection capacity is shared with the API and maintenance.

Polling means idle workers still contact PostgreSQL. FaultLab has no `LISTEN/NOTIFY`, broker, or
adaptive poller. Under connection exhaustion, both workers log failures, wait, and try again;
the short [connection-pressure experiment](reports/stage6-connections.md) observed recovery after
100 held slots were released. It is not evidence for sustained overload safety.

The API does not enforce global queue depth, per-queue concurrency, or producer backpressure.
Those are required design decisions if the project moves beyond a controlled local workload.

## Observability

The Python worker exposes:

- `faultlab_jobs_claimed_total{queue,kind}`;
- `faultlab_jobs_finished_total{queue,kind,outcome}`;
- `faultlab_leases_recovered_total{queue}`;
- `faultlab_job_duration_seconds{queue,kind}`.

Prometheus scrapes the API ASGI metrics endpoint and the Python worker metrics port every five
seconds. When an OTLP endpoint is configured, FastAPI, SQLAlchemy, and job execution emit traces
with job ID, queue, and kind attributes. No endpoint means tracing is cleanly disabled.

Metrics are process-local. A restart resets counters, and a second worker needs a unique metrics
port or an aggregation design. Labels intentionally exclude job ID to avoid unbounded cardinality.
Logs may contain job IDs and bounded handler error strings; handler authors must redact anything
sensitive, and payloads should never be logged wholesale.

## Local operations console

FastAPI serves a dependency-free HTML/CSS/JavaScript console at `/dashboard`. It polls the bounded
list endpoint, computes status counts over the latest 200 returned jobs, creates only registered
demo handlers, filters rows, retrieves attempt history, and sends cancellation requests. It is a
presentation and debugging tool—not a source of truth, metrics store, or authenticated admin UI.

The console handles loading, empty, unavailable, selected, and cancellation states, uses native
controls and visible focus, and recomposes at narrow widths. When disconnected it retries more
slowly and preserves last-known data. Any public deployment must protect both API and dashboard.

## Health and startup

`GET /health/live` proves only that the API process can answer. `GET /health/ready` runs `SELECT 1`
and returns `503` when PostgreSQL is unavailable. It does not prove a worker exists, a queue is
draining, Prometheus is scraping, or an OTLP collector is reachable.

Compose starts PostgreSQL, runs Alembic as a one-shot migration service, then starts API and worker.
The API health check gates Prometheus startup. Host ports are for local access; services use
Compose DNS internally. `FAULTLAB_POSTGRES_PORT` changes only the host binding.

## Schema evolution

Use an additive Alembic revision for every model change. Review forward migration, downgrade,
defaults for existing rows, lock duration, and compatibility with the currently running workers.
For a destructive or type-changing migration, use an expand/migrate/contract plan rather than
assuming the local one-shot migration is safe in production.

Model constraints are the final defense for attempt counts and idempotency uniqueness. Application
validation improves errors but must not replace database constraints for concurrent invariants.
The project currently stores status values as strings, so a typo in new raw Go SQL is not rejected
by a database enum; protocol tests and review are essential.

## Test and fault-injection strategy

Unit tests cover settings, retry math, fingerprints, handler boundaries, request validation,
benchmark helpers, and dashboard asset routing. PostgreSQL integration tests cover API semantics,
idempotency, concurrent claiming, non-blocking `SKIP LOCKED`, attempt history, leases, fencing,
cancellation, and business effects.

Reliability tests launch a real child process and terminate it at `after_claim` or `after_effect`.
They wait for a real five-second lease, recover the job, and verify no job or database effect is
lost/duplicated. The Go integration test builds a binary, runs it against the same schema, and
checks shared handlers, cancellation, conflict handling, and the mixed protocol.

Use the disposable test database only. Test and benchmark entry points reject URLs that do not
visibly contain `test`. Never point destructive fixtures at personal or production data.

## Implementation order from an empty repository

If rebuilding FaultLab to learn it, use this sequence. Each phase has a small proof before the
next reliability mechanism is introduced:

1. **Domain and schema:** define job/attempt states, the three tables, constraints, and the first
   Alembic migration. Prove a job can be inserted and read.
2. **Atomic claim:** implement only `create_job` and `claim_next`. Run two concurrent workers and
   prove the same row is never returned to both.
3. **Completion and failure:** add attempt finishing, retry backoff, maximum attempts, and dead
   state. Prove the exact status/attempt sequence for a flaky handler.
4. **Leases and recovery:** add expiry, heartbeat, reaper, and fencing. Kill a child process after
   claim and prove a replacement finishes the job while the stale worker cannot.
5. **Two idempotency layers:** protect API submission with a scoped unique key/fingerprint, then
   add `record_once` and crash after its commit to expose the execution boundary.
6. **Cancellation:** implement queued cancellation first, then heartbeat propagation and safe
   running-handler checkpoints. Preserve the uncooperative case to show the limitation.
7. **Second runtime:** port the protocol to Go only after the Python state machine is understood.
   Use the same database and cross-runtime integration tests.
8. **Evidence and presentation:** add metrics/traces, repeatable benchmarks, Compose/CI, the
   dashboard, and documentation. Presentation comes after correctness evidence.

At each phase, explain the failure that the new mechanism fixes. If you cannot explain why a
lease is different from a row lock, or why an effect can repeat after a correct claim, pause before
adding more code.

## Repository reading map

| Path | What to understand |
| --- | --- |
| `src/faultlab/domain/jobs.py` | states, terminal/claimable sets, retry math, canonical fingerprint |
| `src/faultlab/db/models.py` | authoritative rows, uniqueness, indexes, relationships |
| `migrations/versions/` | actual database evolution rather than model-only intent |
| `src/faultlab/repositories/jobs.py` | every transactional state transition and fencing check |
| `src/faultlab/api/routes/jobs.py` | HTTP transaction/error boundary |
| `src/faultlab/worker/runtime.py` | Python recover → claim → execute → heartbeat → finish loop |
| `src/faultlab/worker/handlers.py` | trusted handler and cancellation/effect contract |
| `go-worker/main.go` | equivalent protocol expressed with raw PostgreSQL operations |
| `src/faultlab/observability.py` | bounded metric labels and optional trace setup |
| `src/faultlab/api/static/` | local console as a consumer of public API state |
| `tests/integration/test_reliability.py` | executable crash/recovery and effect evidence |
| `tests/integration/test_claim_concurrency.py` | row-lock and repeated claim correctness evidence |
| `tests/integration/test_go_worker.py` | Python-created schema consumed by the Go runtime |
| `scripts/benchmark_*.py` and `reports/` | how measurements are produced and bounded |

Read in that order for interview preparation. Start with invariants and state, then transactions,
then orchestration. Reading the dashboard or benchmark numbers first hides the project’s main work.

## Performance evidence

The claim report measures concurrent claiming, not completed work. The pipeline report uses direct
batched SQL enqueue and a zero-work handler, then measures full worker completion, end-to-end
latency, backlog drain, connection count, CPU, and RSS. The runtime report repeats identical no-op
workloads for Python and Go processes. Connection pressure is a short recovery experiment.

Rules for publishing a number:

1. state the job count, handler cost, worker count, pool, machine, and database;
2. report repetitions and range, not only the best run;
3. separate enqueue rate, claim rate, and terminal completion rate;
4. report correctness (`lost`, duplicate, wrong terminal states) with speed;
5. do not extrapolate a no-op workload to video, ML, email, or external API jobs;
6. rerun after a relevant claim/schema/pool/runtime change.

The checked-in reports are evidence snapshots, not immutable product claims or SLOs.

## Security and production gaps

FaultLab assumes trusted local callers and trusted registered handlers. It does not implement:

- authentication, authorization, tenants, or queue ownership;
- payload secrecy, field redaction, encryption policy, or retention;
- request/body limits at a reverse proxy, quotas, or abuse controls;
- arbitrary-code sandboxing or per-job CPU/memory/network limits;
- backups, restore drills, high availability, or migration rollout safety;
- TLS termination, audit retention, dead-letter replay approval, or public hosting.

Therefore, do not expose port 8000 publicly, process secrets in payload/result/error fields, or
claim production readiness. These are boundaries of the current portfolio project, not minor TODOs.

## Safe implementation recipes

### Add a Python-only demonstration handler

1. Define a bounded async handler in `worker/handlers.py`.
2. Validate payload types/ranges and check cancellation at safe boundaries.
3. Register it in `HANDLERS`; admission will then accept the kind.
4. Add unit tests for success, invalid payload, and cancellation behavior.
5. Label it Python-only in documentation; a Go worker will fail it if both share that queue.

### Add a cross-runtime handler

Follow the Python steps, implement the same payload/result/error semantics in Go, add a shared
queue integration test, and compare JSON number behavior. Never rely on Python-specific payload
types that cannot round-trip through Go's `encoding/json`.

### Add a job state or transition

Update enums, claimable/terminal sets, every repository transition, raw Go SQL, response schemas,
dashboard labels/filtering, migrations/constraints if applicable, metrics outcome labels, and the
state table in this handbook. Add tests from every reachable prior state, including stale-owner,
cancellation, retry-budget, and crash cases.

### Change claim ordering or eligibility

Update Python and Go together. Prove locked rows do not block unrelated work, inspect the query
plan, test starvation/priority/scheduling behavior, rerun repeated claim correctness, and rerun
pipeline/runtime benchmarks. Treat an index change as part of the same behavior change.

### Add a real external side effect

Define a stable business key, the remote system's idempotency contract, retryable versus permanent
errors, timeouts, cancellation boundary, redaction policy, and ambiguous-response recovery before
writing the handler. If the remote system lacks idempotency, design an outbox/consumer boundary and
document the remaining duplicate risk.

## Change checklist

Before merging any reliability-sensitive change:

1. preserve the state machine and terminal non-claimability;
2. keep claim selection, state mutation, attempt increment, and attempt insert atomic;
3. require valid lease owner and unexpired lease for every running-job completion;
4. keep retry budget/backoff consistent in failure and lease recovery;
5. define cancellation and idempotency boundaries for every handler;
6. preserve or explicitly version Python–Go protocol parity;
7. add failure, recovery, concurrency, and stale-worker tests—not only success tests;
8. review database indexes, pool multiplication, and transaction duration;
9. update migration, `.env.example`, Compose, README, handbook, and reports as applicable;
10. run the checks in [docs/REVIEW_GUIDE.md](docs/REVIEW_GUIDE.md);
11. never turn local benchmark observations into unsupported production claims;
12. do not deploy or push unless the project owner requested it.
