# FaultLab explained from scratch

This guide is for someone who knows basic Python and HTTP but is new to distributed systems.
The goal is to understand the project well enough to explain it without reading a script.

## 1. The problem

Suppose a user uploads a video. Converting it may take two minutes. Holding the HTTP request
open for two minutes is fragile: the user may close the tab, the server may time out, and the
web server cannot spend all its time waiting. Instead, the API stores a **job** and returns its
ID immediately. A separate process—the **worker**—does the slow work in the background.

Now the difficult question: what if a worker dies halfway through? What if two workers try to
take the same job? What if the user sends the same request twice? FaultLab exists to make those
failure cases visible and testable.

FaultLab's built-in handlers are examples (`echo`, `flaky`, `sleep`, and `record_once`). It does
not actually convert videos. The system underneath them is the project.

## 2. The five moving parts

```text
Client / dashboard
       |
       v
FastAPI accepts a job and returns its ID
       |
       v
PostgreSQL stores job state and attempt history
       ^
       |
Python or Go worker claims and executes the job
       |
       v
Prometheus observes counters, timings, and worker activity
```

- **API:** `POST /v1/jobs` accepts work; `GET /v1/jobs/{id}` shows the current state.
- **PostgreSQL:** durable source of truth for jobs, attempts, and the `record_once` business effect.
- **Worker:** a separate process that claims a job, calls a trusted handler, and records the result.
- **Lease:** temporary permission for one worker to own a job. The worker renews it with a heartbeat.
- **Console:** a local view of recent jobs and attempts. It uses the same public API as any client.

Start with [the API routes](../src/faultlab/api/routes/jobs.py),
[repository/state transitions](../src/faultlab/repositories/jobs.py), and
[worker runtime](../src/faultlab/worker/runtime.py) if you want to follow the code.

## 3. One job's life

1. The client posts a job. The API validates its kind and saves it as `pending`.
2. A worker claims it in a short database transaction and changes it to `running`.
3. The claim creates attempt number 1 and a lease with an expiry time.
4. While running, the worker heartbeats so the lease stays valid.
5. On success, the worker records the result and the job becomes `succeeded`.

If the handler raises an error, the job becomes `retry_pending`, waits for exponential backoff,
and is claimed for another attempt. After the configured attempt limit, it becomes `dead`.
If a worker disappears, the lease expires and another worker can recover the job. A queued job
can be cancelled immediately; a running job must cooperate and stop at a safe checkpoint.

The [console](http://localhost:8000/dashboard) shows these changes live. Select a job to see
its payload, result, lease information, and attempt history.

## 4. Why the claim is atomic

An unsafe queue would do this:

```text
Worker A reads "job 7 is pending"       Worker B reads "job 7 is pending"
Worker A starts job 7                     Worker B starts job 7
```

FaultLab instead selects and updates the row inside **one transaction**. PostgreSQL locks the
candidate row; `FOR UPDATE SKIP LOCKED` lets another worker skip that locked row and look for
another. The claim also increments the attempt number and inserts an attempt record before
commit. The repeated [claim experiment](../reports/stage1-claiming.md) observed zero duplicate
or missing claims across 7,500 claims in 15 local runs. That is evidence for that workload,
not a mathematical guarantee for every future change.

## 5. Why a lease exists

If ownership were just `worker_id = A`, job 7 could be stuck forever when A crashes. A lease
expires unless A heartbeats. After expiry, the reaper moves the job back toward retry (or
`dead` if attempts are exhausted). A late A is **fenced**: it cannot mark the job successful
after losing its lease. The [reliability tests](../tests/integration/test_reliability.py)
deliberately crash real worker processes at key checkpoints.

This does *not* mean handler code cannot run twice. A paused worker might continue executing
after its lease expires. The database ownership check rejects its late job update, but it
cannot undo an email already sent or a charge already made.

## 6. The two kinds of idempotency

**Submission idempotency:** The client includes an `idempotency_key`. If it repeats the same
request with that key, the API returns the original job ID. Reusing the key with different job
contents produces a conflict. This prevents a flaky network from creating extra *jobs*.

**Effect idempotency:** A job can still execute again after a crash. The `record_once` demo uses
a unique business key in PostgreSQL so a repeated attempt does not add another database
effect. The test crashes after effect commit but before job completion, then proves a replay
does not insert a second effect. Real external APIs need their own idempotency support or a
carefully designed outbox/consumer protocol. FaultLab is **at-least-once**, not magical
exactly-once processing.

## 7. What the benchmarks say—and do not say

The reports record the workload, worker count, machine, measurements, and limitations.
The [claim report](../reports/stage1-claiming.md) tests claiming only. The
[pipeline report](../reports/stage5-pipeline.md) measures completed 200-job local workloads,
including latency percentiles and backlog drain. The [runtime report](../reports/stage6-runtimes.md)
compares Python and Go worker **processes** over repeated no-op workloads. Go was faster and
used less sampled RSS in those specific tests; neither number predicts production throughput
for real handlers. The [connection-pressure report](../reports/stage6-connections.md) shows
short recovery after exhausting a disposable PostgreSQL instance's connections.

In an interview, say *what* was measured before quoting a number. If asked about a bottleneck,
discuss database claims and commits, worker count, payload cost, and the difference between
a no-op handler and real side effects.

## 8. A five-minute demonstration

Use the [showcase guide](SHOWCASE.md) for commands. In the [console](http://localhost:8000/dashboard):

1. Create **Echo** and show `pending → running → succeeded` (fast jobs may skip visible middle state).
2. Create **Flaky**. Select it and show attempt 1 failed and attempt 2 succeeded.
3. Create **Sleep**, wait until `running`, then choose **Cancel job**. Explain that cancellation
   is cooperative, not an instant kill.
4. Open the [API docs](http://localhost:8000/docs) and show the same job state through HTTP.
5. Open the benchmark reports and explain their workload boundaries.

The console deliberately shows only the latest 200 jobs. Its status numbers are counts *within
that view*, not whole-database totals. The separate `scripts/showcase.py` checks idempotency
and queued cancellation automatically.

## 9. Interview questions with honest short answers

**Why PostgreSQL instead of Redis or Kafka?**
It gives this small system durable rows, transactions, row locks, and an inspectable state
machine with one dependency. I would consider a broker after measuring a limit that warrants it.

**Does `SKIP LOCKED` guarantee a job runs exactly once?**
No. It prevents workers from claiming the same locked row concurrently. A crash after an
effect but before completion can still lead to replay.

**What happens if a worker crashes?**
Its heartbeat stops, the lease expires, the reaper records the expired attempt, and the job
is retried or marked dead according to its remaining attempt budget.

**Why do you need fencing?**
A slow old worker may wake up after a replacement claims its expired job. Only the current
lease owner may change the job's final state.

**Why isn't HTTP idempotency enough?**
It stops duplicate submissions, but a single accepted job may execute more than once. The
business effect needs its own idempotency boundary.

**What does cancellation mean?**
Queued work stops immediately. Running work receives a request and stops only if the handler
checks it. Force-killing arbitrary code risks partial side effects.

**Is it production ready?**
No. It is a local systems lab. It lacks auth, tenant isolation, quotas, backup/restore
validation, and a safe public deployment. Its value is the implemented and tested failure
protocol, not pretending those controls exist.

## 10. What to put on your resume

> Built a PostgreSQL-backed distributed job runner with FastAPI and Python/Go workers,
> implementing atomic claims, lease-based crash recovery, retries, cancellation, and
> idempotent database effects; validated failure cases with integration tests and measured
> local throughput/latency with reproducible benchmarks.

Do not list Go as a skill unless you can walk through [the Go worker](../go-worker/main.go),
explain how it shares the database protocol with Python, and describe its benchmark limits.
Practice the five-minute demo until you can narrate it without this document.
