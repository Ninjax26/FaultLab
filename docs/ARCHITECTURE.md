# FaultLab architecture notes

## Core invariants

These invariants define correctness. Any change to the repository should preserve them.

1. At most one unexpired lease owns a job at a time.
2. A worker may complete or fail a job only while it owns that job's lease.
3. Every claim increments `attempt_count` exactly once and creates one matching attempt row.
4. A job with `attempt_count >= max_attempts` cannot be scheduled for another retry.
5. Terminal jobs are never claimable.
6. Repeating an equivalent submission idempotency key returns the original job.
7. Repeating an idempotency key with different semantics is rejected.
8. A worker crash must not leave a job permanently stuck in `running`.

## Claim transaction

```text
BEGIN
  SELECT first runnable job
    WHERE queue = desired queue
      AND status IN (pending, retry_pending)
      AND run_at <= now
    ORDER BY priority DESC, run_at ASC, created_at ASC
    FOR UPDATE SKIP LOCKED

  UPDATE in-memory locked row:
    status = running
    attempt_count += 1
    lease_owner = worker ID
    lease_expires_at = now + lease duration

  INSERT attempt history row
COMMIT
```

`SKIP LOCKED` allows several workers to avoid waiting on the same candidate row. It improves
throughput, but strict global fairness is not guaranteed. Benchmark starvation behavior when
adding more priority levels.

## Lease recovery

Workers periodically lock expired `running` rows in small batches. Each job becomes either:

- `retry_pending`, with a future `run_at`, or
- `dead`, if all attempts were consumed.

The old attempt is marked `lease_expired`. A late result from the original worker is rejected
after another worker has taken ownership.

### Known race to reason about

The original worker may still execute code after its lease expires—for example, during a long
process pause. Ownership checks protect the job row, but they cannot undo an external side
effect. Handler-level idempotency remains necessary.

## Delivery semantics

FaultLab provides at-least-once execution:

- It prefers retrying ambiguous work over silently losing it.
- Therefore, a handler may run more than once.
- A handler must be idempotent or use an idempotency record/outbox.

Exactly-once is not a generic property of a queue. It is a property that must include the
business side effect and its transactional boundary.

## Why PostgreSQL first

PostgreSQL provides durability, transactions, row locks, indexes, and an inspectable state
model with minimal infrastructure. It is an excellent first implementation.

Potential limitations to measure:

- Polling creates idle database traffic.
- High write volume may create table/index bloat.
- Large backlogs may need carefully tuned partial indexes.
- Long-running transactions harm concurrency.
- One database can become a throughput bottleneck.

Do not add a broker until a benchmark identifies which limitation matters.

## Cancellation

Queued jobs cancel immediately. Running jobs store `cancellation_requested_at`; heartbeats
propagate it to a handler token. The `sleep` handler checks safe boundaries and stops before
the worker records `cancelled`. A crash during cancellation is resolved by lease recovery.
The `sleep_uncooperative` handler deliberately ignores the token and may finish successfully.
Forcefully killing arbitrary handler code could leave partial side effects. See
[RELIABILITY.md](RELIABILITY.md).

## Security boundaries not implemented yet

- Authentication and tenant authorization
- Per-tenant quotas
- Payload size limits at the reverse proxy
- Encryption and secret references for sensitive payloads
- Handler allowlists per tenant
- Audit log retention
- Network isolation for untrusted code

FaultLab executes only registered trusted handlers. It is not a sandbox for arbitrary code.
