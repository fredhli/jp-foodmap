# 4.3.0 station-layer performance gate

Verdict: **DIAGNOSTIC**

Diagnostic only: idle window/repeat count was shortened, so PASS is intentionally unavailable.

Payload: 8954 stations, 512544 B raw, 186897 B gzip; one immutable request.

| profile | cold start Δ | warm start Δ | pan CPU Δ / 10 drags | pan Δ | rAF p95 Δ | heap Δ | DOM Δ | idle CPU Δ | result |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| fold-inner-4x | -95.1 ms | 135.9 ms | 69.5 ms | 2.3% | 0.00 ms | 1.59 MiB | 2 | -38.9 ms | FAIL |
| fold-outer-6x | -163.4 ms | 39.8 ms | 420.0 ms | 8.2% | 0.00 ms | 1.58 MiB | 2 | -44.1 ms | FAIL |
| desktop-4x | -1.4 ms | 143.4 ms | 317.0 ms | 4.4% | 0.00 ms | 1.55 MiB | 2 | -2.3 ms | FAIL |

## Failed checks

- fold-inner-4x: `warm_start_delta_le_100ms`
- fold-inner-4x: `pan_task_absolute_delta_le_15ms`
- fold-outer-6x: `pan_task_relative_le_8pct`
- fold-outer-6x: `pan_task_absolute_delta_le_15ms`
- desktop-4x: `warm_start_delta_le_100ms`
- desktop-4x: `pan_task_absolute_delta_le_15ms`

## Method

Same docs/ build, formal full payload, stations=0/1 localStorage A/B, railway buckets off. Cold and browser-cache-warm starts use medians. The station response is gzip + 150 ms RTT + 1.6 Mbit/s shaping. Each steady page is force-GC'd, dragged 10 times in three trials, and left idle for 15 seconds under tracing. Fold inner uses 4x CPU, Fold outer 6x, desktop 4x.

Raw JSON and idle traces are beside this report.
