# Pico Optimization Dashboard

This dashboard records the A-stage security and trust status. Current local evidence is in [the security and trust baseline directory](../../benchmarks/results/security-trust-baseline-2026-07-10/).

| ID | Status | Acceptance | Evidence |
| --- | --- | --- | --- |
| A-01 Sensitive data | Done | Provider/session/artifact/CLI canary clean | security integration tests |
| A-02 Safe execution | Done | zero automatic bypass in fixed shell corpus | shell security corpus |
| A-03 Recovery integrity | Done | durable intent, reconciliation, review, quarantine | A2 and durability E2E tests |
| A-04 Local evidence | Done | full check, deterministic benchmarks, perf smokes | current A evidence directory |
| A-05 Real E2E | Done | one DeepSeek native-tool run with key/artifact checks | `qwen3.7-max`; 43/43; 8 native actions; 10/15 calls; ignored key-clean report |

The prior C-stage gate remains historical evidence. The final A-05 report is intentionally ignored and records a complete key-clean native-tool run; earlier diagnostic attempts remain local and were never automatically retried.
