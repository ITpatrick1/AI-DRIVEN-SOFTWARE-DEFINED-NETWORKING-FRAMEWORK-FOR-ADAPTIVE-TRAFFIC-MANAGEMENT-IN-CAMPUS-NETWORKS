# Stage 11 Testing and Evaluation Report

Tag: stage11_20260405_231054
Generated (UTC): 2026-04-05T21:12:32.935481+00:00

## Tests Executed
- pingall
- iperf3 throughput probes
- latency (ICMP RTT) probes
- congestion stress workload

## Measurable Project Results
- Pingall connectivity was preserved in both runs (loss: 0.0% before, 0.0% after).
- Protected-flow throughput under congestion changed from 4.193 Mbps to 5.452 Mbps.
- Packet delivery under congestion changed from 100.0% to 100.0%.
- Average latency under congestion changed from 5.745 ms to 5.456 ms.
- Adaptive response time under congestion was n/a s.
- Reroute evidence packets on the backup path changed from 0 to 0.

## Before vs After Adaptive Routing
| Metric | Before Adaptive | After Adaptive | Delta (After-Before) |
|---|---:|---:|---:|
| Pingall loss (%) | 0.0 | 0.0 | 0.0 |
| Throughput (Mbps) | 4.193 | 5.452 | 1.259 |
| Packet loss (%) | 0.0 | 0.0 | 0.0 |
| Packet delivery (%) | 100.0 | 100.0 | 0.0 |
| Avg latency (ms) | 5.745 | 5.456 | -0.289 |
| Congestion response (s) | n/a | n/a | n/a |
| Reroute packets on backup path | 0 | 0 | 0 |
| Policy activations | 0 | 1 | 1 |

## Interpretation
- Static routing keeps the adaptive policy inactive, so the shared bottleneck is left to contention alone.
- Adaptive mode should activate the policy during congestion, reroute ICMP to the backup path, and throttle student bulk Wi-Fi traffic.
- Lower packet loss, lower latency, higher packet delivery, and higher throughput on the protected flow are all evidence of improvement over static routing.

## Evidence Summary
- Throughput gain under congestion: 1.259 Mbps.
- Packet delivery gain under congestion: 0.0 percentage points.
- Packet loss reduction under congestion: 0.0 percentage points.
- Latency reduction under congestion: 0.289 ms.
- Adaptive congestion response time: n/a s.
- Adaptive reroute observed: yes.

## Artifacts
- Before (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_20260405_231054_static.json
- After (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_20260405_231054_adaptive.json
- Comparison (JSON): /home/patrick/mininet/results/stage11_comparison_stage11_20260405_231054.json
- Comparison (CSV): /home/patrick/mininet/results/stage11_comparison_stage11_20260405_231054.csv
- Comparison (Markdown): /home/patrick/mininet/results/stage11_comparison_stage11_20260405_231054.md
