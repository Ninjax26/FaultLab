# FaultLab Stage 1 claim benchmark

Generated: `2026-08-30T19:29:25.548445+00:00`

Environment: `macOS-26.5.2-arm64-arm-64bit`; Python `3.12.13`; PostgreSQL `PostgreSQL 17.11 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit`

| Workers | Run | Jobs | Pool | Jobs/s | p50 ms | p95 ms | p99 ms | Duplicates | Missing | Max DB connections |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1 | 500 | 18 | 82.31 | 8.014 | 30.654 | 92.595 | 0 | 0 | 1 |
| 1 | 2 | 500 | 18 | 184.98 | 3.623 | 14.635 | 24.549 | 0 | 0 | 1 |
| 1 | 3 | 500 | 18 | 299.82 | 2.844 | 6.044 | 9.737 | 0 | 0 | 1 |
| 2 | 1 | 500 | 18 | 290.49 | 5.063 | 14.196 | 21.806 | 0 | 0 | 2 |
| 2 | 2 | 500 | 18 | 344.36 | 4.303 | 11.829 | 17.56 | 0 | 0 | 2 |
| 2 | 3 | 500 | 18 | 432.71 | 4.121 | 8.149 | 10.768 | 0 | 0 | 2 |
| 4 | 1 | 500 | 18 | 67.23 | 27.03 | 109.72 | 511.624 | 0 | 0 | 4 |
| 4 | 2 | 500 | 18 | 82.6 | 24.392 | 161.672 | 359.566 | 0 | 0 | 4 |
| 4 | 3 | 500 | 18 | 73.03 | 34.704 | 143.027 | 394.806 | 0 | 0 | 4 |
| 8 | 1 | 500 | 18 | 119.42 | 34.794 | 169.361 | 971.419 | 0 | 0 | 8 |
| 8 | 2 | 500 | 18 | 76.37 | 60.097 | 308.768 | 727.437 | 0 | 0 | 8 |
| 8 | 3 | 500 | 18 | 187.87 | 28.507 | 85.166 | 455.368 | 0 | 0 | 8 |
| 16 | 1 | 500 | 18 | 141.76 | 68.946 | 236.238 | 914.63 | 0 | 0 | 16 |
| 16 | 2 | 500 | 18 | 205.26 | 44.556 | 117.881 | 933.073 | 0 | 0 | 16 |
| 16 | 3 | 500 | 18 | 355.13 | 31.16 | 63.073 | 409.835 | 0 | 0 | 16 |

## Interpretation guardrails

- This benchmark measures the PostgreSQL claim transaction, not handler execution.
- A valid correctness run has zero duplicate and zero missing claims.
- Throughput numbers are meaningful only with machine and database context.
- More workers can reduce throughput when connection or lock contention dominates.
