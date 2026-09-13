# FaultLab end-to-end benchmark

Generated: 2026-09-13T19:21:44.088943+00:00
Database: `postgresql+asyncpg://***:***@127.0.0.1:55433/faultlab_test`

This is a local, disposable-PostgreSQL measurement, not a production SLO. Run it again on the same idle machine before using a throughput figure on a resume.

## Workload and environment

- Host: macOS-26.6.2-arm64-arm-64bit; logical CPUs: 10
- PostgreSQL: PostgreSQL 17.11 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
- PostgreSQL max_connections: 100; shared_buffers: 128MB
- PostgreSQL container CPU/memory limits: not enforced by this script; inspect Docker Desktop settings.
- Enqueue uses batched direct SQL, not HTTP; completion uses real Python workers.
- Handler payload: `benchmark-noop` with one integer; no artificial sleep.

| Workers | Jobs | Pool | Enqueue/s | Complete/s | E2E p50 ms | E2E p95 ms | E2E p99 ms | Peak DB conns | Lost | CPU s | RSS MiB |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 200 | 12 | 4508.39 | 132.07 | 817.158 | 1429.296 | 1491.541 | 2 | 0 | 0.764 | 82.52 |
| 2 | 200 | 12 | 5417.31 | 226.05 | 477.351 | 819.819 | 851.673 | 3 | 0 | 0.693 | 82.52 |
| 4 | 200 | 12 | 5391.79 | 270.08 | 394.399 | 663.933 | 690.432 | 5 | 0 | 0.677 | 82.52 |
| 8 | 200 | 12 | 5477.74 | 218.33 | 462.972 | 846.249 | 879.419 | 9 | 0 | 0.898 | 82.52 |

Backlog recovery rate is the `Complete/s` column. Database utilization proxies (transaction, block and tuple counters) and backlog samples are in the JSON report. This script does not claim to measure PostgreSQL CPU or memory directly.
