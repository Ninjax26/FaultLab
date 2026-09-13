# FaultLab connection-pressure experiment

Disposable local PostgreSQL only. Both workers were started while connection slots were exhausted for at least two seconds, then all held clients were released.

| Runtime | Jobs completed | Slots held | Held s | Failure log lines | Recovery s | Lost |
|---|---:|---:|---:|---:|---:|---:|
| python | 10 | 100 | 2.018 | 19 | 0.357 | 0 |
| go | 10 | 100 | 2.223 | 2 | 0.298 | 0 |

This is a brief recovery experiment, not sustained-load capacity evidence.
