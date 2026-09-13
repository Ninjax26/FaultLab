# FaultLab learning path

The repository now contains implementations and evidence for Stages 1-6. The Stage 5 and 6
reports are local experiments, not production capacity claims. The Stage 6 global
PostgreSQL-connection-exhaustion experiment and the optional advanced work are not complete;
see [BENCHMARKS.md](BENCHMARKS.md). Stage 0 and the interview checklist still require the
project owner to explain the design independently; code cannot certify understanding.

Build one stage at a time. After each stage, explain the request flow without reading the code
and deliberately test the associated failure.

## Stage 0: read the foundation

Understand:

- The job state machine
- PostgreSQL transactions
- Row-level locks
- `FOR UPDATE SKIP LOCKED`
- At-least-once versus at-most-once execution
- Submission versus execution idempotency

Questions you must be able to answer:

1. Why is selecting a job and updating it in two transactions incorrect?
2. Why does an expiring lease exist instead of a `worker_id` alone?
3. What happens if a worker stops heartbeating but continues executing?
4. Why is an HTTP idempotency key insufficient for external side effects?

## Stage 1: prove concurrent claiming

Implementation tooling: [STAGE1_CONCURRENCY.md](STAGE1_CONCURRENCY.md). The phase is complete only
after the PostgreSQL tests pass and a real benchmark report records zero duplicate/missing claims.

Run multiple workers and expand the integration test to claim hundreds of jobs concurrently.

Measure:

- Duplicate claims
- Claim latency
- Throughput as worker count increases
- Database connection usage

Exit condition: no duplicate active ownership in repeated tests.

## Stage 2: prove crash recovery

Implemented as deterministic real-process crash checkpoints in
[RELIABILITY.md](RELIABILITY.md) and `tests/integration/test_reliability.py`. The tests record
lease recovery time and repeated attempts. Random instruction-boundary fault injection is a
future extension.

Add an automated chaos test that terminates workers at random points. Record:

- Time until lease expiry
- Time until a replacement worker claims the job
- Number of lost jobs
- Number of repeated handler executions

Exit condition: no permanently stuck job after the recovery window.

## Stage 3: implement execution idempotency

The `record_once` handler and unique `business_effects` table implement the consumer-record
approach. The transactional outbox is analyzed, not implemented; see
[RELIABILITY.md](RELIABILITY.md).

Add a `record_once` handler backed by a table with a unique business idempotency key. Then
simulate a crash after committing the side effect but before marking the job successful.

Compare two approaches:

- Idempotent consumer record
- Transactional outbox

Exit condition: repeated execution does not repeat the observable business effect.

## Stage 4: cooperative cancellation

Implemented and tested for queued, running, crash-during-cancellation, and intentionally
uncooperative handlers.

Implement the design in `ARCHITECTURE.md`. Add tests for:

- Cancelling a queued job
- Requesting cancellation during a long handler
- Worker crash during cancellation
- Handler that ignores cancellation

## Stage 5: benchmark and tune

Implemented Python workload generator and
[pipeline report](../reports/stage5-pipeline.md). PostgreSQL CPU/memory and pinned container
limits remain future measurement work; see [BENCHMARKS.md](BENCHMARKS.md).

Use k6 or a Python workload generator. Produce a reproducible report containing:

- Hardware and container limits
- PostgreSQL settings
- Job payload and duration distribution
- Worker count and connection-pool size
- Enqueue throughput
- Claim throughput
- End-to-end p50, p95, and p99 latency
- CPU, memory, and database utilization
- Backlog recovery rate

Never publish a throughput number without this context.

## Stage 6: Go worker, only after understanding the Python worker

Go worker implemented under `go-worker/`, with shared-queue integration tests and a
[repeated comparison](../reports/stage6-runtimes.md). Global PostgreSQL connection
exhaustion has not been tested.

Port one worker implementation to Go while preserving the database protocol and invariants.
Run Python and Go workers against the same queue and compare:

- Throughput
- Memory per worker
- Startup time
- Behavior under connection exhaustion
- Recovery behavior

The point is a measured comparison, not adding `Go` to the skills section.

## Optional advanced work

- Weighted fair queues and starvation tests
- Per-tenant concurrency limits
- Cron scheduling with duplicate-scheduler protection
- Payload schema registry
- PostgreSQL `LISTEN/NOTIFY` to reduce idle polling
- NATS or Redis Streams transport behind a common queue interface
- Dead-letter replay with audit history
- OpenTelemetry trace propagation inside job payload metadata
- Backup/restore and migration compatibility tests

## Interview readiness checklist

Before adding FaultLab to your resume, demonstrate all of the following without AI assistance:

- Draw the state machine.
- Walk through the exact claim transaction.
- Explain why the system is at-least-once.
- Identify the crash-after-side-effect race.
- Explain the index used by the claim query.
- Show a concurrent claim test.
- Kill a worker and predict every database transition.
- Explain the observed benchmark bottleneck.
- Describe one design you rejected and why.
