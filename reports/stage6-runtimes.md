# FaultLab Python vs Go worker processes

Generated: 2026-09-13T19:34:35.033628+00:00

Both runtimes consumed the same `benchmark-noop` payload shape from the same PostgreSQL instance. Enqueue was batched direct SQL before process launch. This is a local comparison, not a production capacity claim.

## Median and observed range across repetitions

| Runtime | Workers | Median complete/s | Range complete/s | Median peak RSS MiB |
|---|---:|---:|---:|---:|
| go | 1 | 249.01 | 189.24-292.40 | 11.57 |
| go | 2 | 344.62 | 330.04-432.59 | 22.11 |
| go | 4 | 306.03 | 155.85-541.89 | 43.21 |
| python | 1 | 76.77 | 66.81-80.97 | 79.06 |
| python | 2 | 116.65 | 88.82-127.62 | 155.52 |
| python | 4 | 100.73 | 75.41-112.70 | 312.86 |

## Individual runs

| Runtime | Rep | Workers | Jobs | Complete/s | E2E p95 ms | First completion s | Peak worker RSS MiB | Child CPU s | Lost |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| python | 1 | 1 | 200 | 66.81 | 2822.569 | 0.847 | 78.95 | 1.674 | 0 |
| go | 1 | 1 | 200 | 249.01 | 778.72 | 0.13 | 11.69 | 0.324 | 0 |
| python | 1 | 2 | 200 | 88.82 | 2110.101 | 0.962 | 154.61 | 2.897 | 0 |
| go | 1 | 2 | 200 | 344.62 | 509.652 | 0.127 | 22.5 | 0.441 | 0 |
| python | 1 | 4 | 200 | 100.73 | 1839.42 | 1.03 | 314.72 | 5.572 | 0 |
| go | 1 | 4 | 200 | 306.03 | 541.816 | 0.168 | 43.95 | 0.772 | 0 |
| go | 2 | 1 | 200 | 292.4 | 630.924 | 0.12 | 11.57 | 0.277 | 0 |
| python | 2 | 1 | 200 | 76.77 | 2392.216 | 0.672 | 79.06 | 1.541 | 0 |
| go | 2 | 2 | 200 | 330.04 | 533.619 | 0.145 | 22.1 | 0.445 | 0 |
| python | 2 | 2 | 200 | 116.65 | 1577.068 | 0.692 | 155.52 | 2.255 | 0 |
| go | 2 | 4 | 200 | 541.89 | 351.696 | 0.135 | 43.21 | 0.588 | 0 |
| python | 2 | 4 | 200 | 75.41 | 2502.46 | 1.112 | 312.42 | 6.232 | 0 |
| python | 3 | 1 | 200 | 80.97 | 2337.405 | 0.674 | 79.38 | 1.457 | 0 |
| go | 3 | 1 | 200 | 189.24 | 931.957 | 0.121 | 11.25 | 0.378 | 0 |
| python | 3 | 2 | 200 | 127.62 | 1514.763 | 0.676 | 158.62 | 2.115 | 0 |
| go | 3 | 2 | 200 | 432.59 | 437.152 | 0.124 | 22.11 | 0.378 | 0 |
| python | 3 | 4 | 200 | 112.7 | 1712.309 | 0.955 | 312.86 | 5.291 | 0 |
| go | 3 | 4 | 200 | 155.85 | 1151.183 | 0.183 | 43.07 | 0.894 | 0 |

`First completion` includes process startup, first claim, and one job completion; it is not pure binary startup time. RSS is the sampled sum of worker processes. Each Go process was limited to one pool connection; Python uses its configured SQLAlchemy pool. Database connections and backlog traces are in the JSON report.
