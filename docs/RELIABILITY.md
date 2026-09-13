# Reliability checkpoints

## Stage 2: worker crash and lease recovery

The integration suite launches a real child process and kills it with `os._exit(137)` after
two deterministic crash checkpoints: after a claim, and after a committed business effect.
Its five-second lease really expires (the test does not edit the timestamp). A replacement
worker then reaps and claims the job. Both paths finish with one terminal job, two attempt
records, and one business effect. The old owner cannot finish after its lease expires.

Run `make test-integration`. The deterministic checkpoints make failures reproducible;
they are more useful for CI than an unseeded random kill loop. The suite does not test every
possible instruction boundary or network partition.

## Stage 3: execution idempotency

HTTP submission idempotency only deduplicates *job creation*. It cannot prevent a handler from
running twice. `record_once` inserts into `business_effects` using a unique business key in
its own short transaction. After a crash between that commit and `mark_succeeded`, a retry
finds the existing value and returns `effect_inserted=false`. Reusing the key with a different
value fails rather than silently accepting conflicting semantics.

This is an idempotent-consumer record for a PostgreSQL effect. For an external service,
prefer the service's idempotency key. A transactional outbox is appropriate when a database
change must atomically record an intent to publish an event; a separate dispatcher can retry
delivery. An outbox alone still requires consumer deduplication because publishing can be
repeated after an ambiguous acknowledgement. FaultLab documents that alternative rather
than claiming to implement a message broker or outbox dispatcher.

## Stage 4: cancellation

- Queued `pending`/`retry_pending` jobs become `cancelled` under a row lock.
- Running jobs retain `running` and gain `cancellation_requested_at`. Heartbeats pass this
  signal to a handler cancellation token; `sleep` checks it at safe 100 ms boundaries.
- A worker marks a job `cancelled` only after the handler actually stops. If the worker
  crashes first, the reaper marks the expired, cancellation-requested job `cancelled`.
- `sleep_uncooperative` deliberately ignores the token. It can finish `succeeded` with a
  cancellation request still recorded. Force-killing arbitrary work could leave partial
  side effects, so a request is not a guarantee.

Lease ownership and unexpired time are checked again before any completion, failure, or
cancellation transition. A late worker cannot overwrite a replacement worker's result.

## Run and inspect

```bash
make test-integration
```

Relevant code: `src/faultlab/repositories/jobs.py`, `src/faultlab/worker/runtime.py`,
`src/faultlab/worker/handlers.py`, `scripts/chaos_worker.py`, and
`tests/integration/test_reliability.py`.
