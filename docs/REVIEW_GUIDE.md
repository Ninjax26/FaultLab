# FaultLab implementation review guide

Use this guide for pull-request review, self-review before an interview, or checking an AI-generated
change. It translates the engineering handbook into specific questions and evidence requirements.

## Review order

Review in this order because later layers depend on earlier correctness:

1. **Behavior contract:** What user/operator behavior changes? What failure is handled?
2. **State machine:** Which job and attempt transitions are added or changed?
3. **Database transaction:** Where is the concurrency boundary and which rows are locked?
4. **Worker protocol:** Can a stale, crashed, cancelled, or retried worker violate the outcome?
5. **Cross-runtime parity:** Do Python and Go still agree on schema and state semantics?
6. **API and console:** Are inputs bounded and states presented honestly?
7. **Tests and evidence:** Does a deterministic test fail without the change?
8. **Operations/docs:** Can another person migrate, run, observe, and explain it?

Do not begin with naming or CSS when a change modifies claims, leases, retries, or effects.

## Severity model

| Level | Meaning | Examples |
| --- | --- | --- |
| Blocker | Can lose work, duplicate an unprotected effect, corrupt state, or expose unsafe access | split claim transaction, completion without fencing, destructive migration |
| High | Breaks recovery, cancellation, runtime parity, or accepted API behavior | Go backoff differs, expired attempt remains running, idempotency conflict ignored |
| Medium | Misleads operators, weakens observability/tests, or creates avoidable load | global metric label, unbounded list, fast offline polling |
| Low | Maintainability or presentation issue without incorrect behavior | unclear name, stale documentation link, minor visual inconsistency |

Every finding should name the violated invariant, the concrete failure sequence, and the smallest
safe correction. “This feels risky” is not actionable review evidence.

## Core invariant checklist

Reject or redesign a change when any answer is “no” without an explicit contract change:

- Can at most one unexpired lease own a job at a time?
- Is every claim one transaction that also creates exactly one matching attempt?
- Are only `pending` and `retry_pending` jobs claimable, and only after `run_at`?
- Must a worker hold the current, unexpired lease before finishing a running job?
- Does loss of a worker eventually produce retry, dead, or cancelled rather than permanent running?
- Can `attempt_count` never exceed the configured retry budget through normal paths?
- Are terminal jobs non-claimable and cancellation requests durable?
- Does equivalent submission-key reuse return the original job?
- Does different semantic reuse of that key fail visibly?
- Is every repeated business effect safe or explicitly documented as possibly duplicated?

## Database and transaction review

For every write path, mark the transaction start/end and answer:

- Is the read-modify-write protected with `FOR UPDATE`, a conditional update, or a uniqueness
  constraint appropriate to the race?
- Is handler/network/sleep work outside database transactions?
- Can two reapers or workers select the same row? If one locks it, does the other safely skip?
- Are timestamps generated consistently in UTC/database time where comparisons matter?
- Does a transaction update both job and attempt, or can they diverge after a crash?
- Are error strings bounded before storage?
- Does a new query have a bounded result and an index compatible with its predicate/order?
- Do connection-pool settings multiplied by process count fit PostgreSQL capacity?

For raw Go SQL, compare semantics—not just text—with the Python repository. Pay special attention
to `NULL`, JSON number types, string casts, `CASE` branches, and exact status spelling.

## Claim and scheduling review

Confirm that the claim query includes queue, claimable status, and `run_at <= now()`. The selected
row must be locked with `SKIP LOCKED`; claim mutation and attempt insertion must commit together.
Ordering must remain priority descending, schedule ascending, creation ascending unless the change
explicitly revises scheduling policy.

Required evidence for a claim change:

- one test holds the first candidate row lock and proves another worker does not wait on it;
- hundreds of jobs are claimed concurrently with zero missing/duplicate claims;
- priority/scheduled behavior has focused tests;
- the query plan/index is reviewed on representative backlog sizes;
- the claim and pipeline reports are regenerated if performance could change.

## Lease, heartbeat, and fencing review

Check both renewal and completion conditions. Renewal must require current owner, running state,
and an unexpired lease. Completion/failure/cancellation must repeat those checks under a row lock.
An old worker must be unable to finish after recovery and a replacement claim.

Ask for tests covering:

- expiry followed by reaper and replacement claim;
- late success from the original worker is rejected;
- heartbeat error signals cooperative handler shutdown;
- cancellation becomes observable before an excessively long renewal delay;
- invalid `heartbeat >= lease` configuration fails at startup;
- process death waits for a real lease at least once, rather than only editing timestamps.

## Retry and dead-letter review

Verify that failures and expired leases use the same retry formula and attempt number. The next
`run_at` must be in the future; the final allowed attempt must become `dead`; cancellation must win
over retry. Python and Go need identical cap behavior, including integer overflow protection in Go.

Classify real handler errors before broadening retry behavior. Retrying validation, authorization,
or permanent business failures wastes capacity and may repeat side effects. FaultLab currently
retries all ordinary handler exceptions for demonstration simplicity—document this limitation if
adding real integrations.

## Cancellation review

Trace four cases separately:

1. queued job is cancelled before claim;
2. cooperative running handler observes and stops;
3. worker crashes after cancellation request;
4. uncooperative handler ignores the request and may succeed.

A review should reject claims of forceful or guaranteed cancellation unless the execution model
actually isolates and kills work safely. New handlers must state their safe checkpoints and what
happens to partial effects.

## Idempotency and side-effect review

Keep submission and execution idempotency separate in code and documentation.

For submission keys, review the unique scope, fingerprint fields, canonical serialization, equal
repeat, and conflicting repeat. If a field such as priority or `run_at` gains business meaning,
decide explicitly whether it belongs in the fingerprint.

For a side effect, write the failure sequence:

```text
request sent/commit begins -> effect may happen -> response/completion missing -> retry occurs
```

Then identify the stable business key and system that enforces uniqueness. A local database key
does not protect an external provider. Tests should crash after effect commit but before job
completion and assert one observable effect after replay.

## API and schema review

- New input fields have length/range/type/timezone bounds.
- New job kinds are admitted only when an implementation exists for the intended workers.
- Writes have explicit transaction scope and stable error translation.
- Lists are bounded; pagination is required before increasing the console beyond its current 200.
- Response changes remain backward compatible or are versioned.
- Payloads, results, and errors do not introduce credentials or sensitive data.
- Terminal versus accepted HTTP semantics remain clear (`202` is not success of execution).
- Model changes have an Alembic revision and an existing-row/default plan.

## Python–Go compatibility review

Use this table when protocol behavior changes:

| Concern | Python location | Go location |
| --- | --- | --- |
| claim eligibility/order | `JobRepository.claim_next` | `worker.claim` |
| lease recovery | `recover_expired_leases` | `worker.reap` |
| heartbeat/fencing | `heartbeat`, `_locked_running_job` | `renew`, `observe`, `finish` |
| retry delay | `retry_delay_seconds` | `retryDelay` |
| handlers/results | `worker/handlers.py` | `worker.handle` |
| effect idempotency | `record_once` | `record_once` case in `handle` |
| final state/attempt | `mark_*` methods | `worker.finish` |

Require a cross-runtime integration test for a shared behavior change. A Go unit test alone cannot
prove compatibility with the Python-created schema and queue.

## Handler review

A handler should have a documented payload/result shape, bounded runtime/input, timeout strategy,
safe cancellation points, retry classification, stable effect key, and error-redaction policy.
It must not open a long transaction around network or CPU work. CPU-bound Python work must not
block heartbeat progress without a thread/process execution strategy.

For each handler, review success, invalid payload, transient failure, permanent failure, timeout,
cancellation, crash after effect, and replay. A happy-path test is insufficient.

## Dashboard review

The dashboard is a local operator aid. Review it against these rules:

- It calls documented API endpoints and never invents authoritative state.
- Status counts are labelled as latest-200 view counts, not lifetime metrics.
- Loading, empty, disconnected, stale-data, success, failure, and selected states remain usable.
- Buttons and selects use native semantics, visible labels, keyboard order, and visible focus.
- Status is communicated with text as well as color.
- Polling pauses when hidden and backs off while offline.
- User-derived payloads/errors are inserted with `textContent`, not HTML injection.
- Layout has no page-level horizontal overflow at 320 px and remains usable at 200% zoom.
- Public exposure is never suggested until API/dashboard authentication exists.

## Observability review

New metrics need stable, bounded-cardinality labels. Do not label by job ID, user text, error text,
or business key. Counter outcomes should match state terminology, and histograms need a stated
measurement boundary. Logs should identify job/queue/kind without dumping payload secrets.

Readiness checks only what their name claims. If a new dependency is correctness-critical, decide
whether readiness must include it. If it is optional, expose degraded status instead of making the
entire service unavailable.

The current Go worker has no Prometheus/OTLP parity. A change must not imply otherwise.

## Benchmark and evidence review

Reject benchmark claims that omit workload or correctness. A valid report includes host/database,
job/payload cost, enqueue method, workers, pool size, duration, repetitions/range, latency
percentiles, throughput boundary, connection observation, terminal counts, and lost/duplicate jobs.

Compare like with like. The current Go/Python report uses no-op handlers and process RSS; it does
not measure real business work or pure language speed. Regenerate reports when claim SQL, pooling,
polling, worker concurrency, schema indexes, or benchmark code changes materially.

## Security and operational review

Block any change that publishes the current unauthenticated console/API. Do not accept secrets in
payloads, results, logs, screenshots, `.env`, Compose files, benchmark JSON, or documentation.
New external services require scoped credentials, timeouts, redaction, network assumptions, and a
recovery story. Any destructive test must enforce a disposable database guard.

Migration review must cover backup/rollback strategy before production use. `docker compose down`
is recoverable because it keeps the named volume; `down -v` deletes local queue data and should
never appear as the default cleanup instruction.

## Required verification before merge

Run from the repository root:

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
(cd go-worker && GOCACHE=/tmp/faultlab-go-cache go test ./...)
docker compose --profile test up -d --wait postgres-test
PYTHONPATH=src \
  TEST_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test \
  GOCACHE=/tmp/faultlab-go-cache \
  .venv/bin/pytest -q
FAULTLAB_DATABASE_URL=postgresql+asyncpg://faultlab:faultlab@localhost:55433/faultlab_test \
  .venv/bin/alembic upgrade head
docker compose config -q
```

For dashboard changes, build/start Compose and manually verify at least:

- desktop and 320 px layouts without horizontal page overflow;
- keyboard navigation and visible focus;
- empty and database-unavailable states;
- create `echo`, `flaky`, and `sleep`; inspect attempts and cancel sleep;
- filter every status and confirm selection remains understandable;
- browser console/network errors and reduced-motion behavior.

For claim/lease/retry/pool changes, also rerun the appropriate scripts and update their reports.

## Interview self-review

Score each answer from 0 to 2: `0` cannot explain, `1` explains the idea, `2` explains the exact
FaultLab implementation plus one trade-off/failure. Target at least 20/24 without notes.

1. Draw one job's state transitions.
2. Explain the exact claim transaction and why it cannot be split.
3. Explain what `SKIP LOCKED` prevents and what fairness it does not guarantee.
4. Distinguish a row lock, lease, heartbeat, and fencing check.
5. Walk through a worker crash immediately after claim.
6. Walk through a crash after `record_once` commits.
7. Explain why submission idempotency is not execution idempotency.
8. Explain why FaultLab is at-least-once rather than exactly-once.
9. Explain cooperative versus forceful cancellation.
10. Describe how Python and Go safely share the same queue.
11. Interpret one benchmark honestly, including its workload limitation.
12. Name the first three controls required before public production use.

## Reviewer sign-off template

Use this short record in a pull request or personal implementation log:

```text
Behavior changed:
Invariants affected:
Failure sequence reviewed:
Python/Go parity impact:
Migration/index/pool impact:
Tests added:
Commands run and results:
Benchmark/report impact:
Security/operations impact:
Remaining limitation:
```
