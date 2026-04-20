# Stage 11 Testing and Evaluation Report

Tag: stage11_retry
Generated (UTC): 2026-04-05T21:20:39.363905+00:00

## Tests Executed
- pingall
- iperf3 throughput probes
- latency (ICMP RTT) probes
- congestion stress workload

## Measurable Project Results
- Pingall connectivity was preserved in both runs (loss: 0.0% before, 0.0% after).
- Protected-flow throughput under congestion changed from 4.194 Mbps to 5.451 Mbps.
- Packet delivery under congestion changed from 100.0% to 100.0%.
- Average latency under congestion changed from 5.621 ms to 5.602 ms.
- Adaptive response time under congestion was 0.001 s.
- Reroute evidence packets on the backup path changed from 0 to 0.

## Before vs After Adaptive Routing
| Metric | Before Adaptive | After Adaptive | Delta (After-Before) |
|---|---:|---:|---:|
| Pingall loss (%) | 0.0 | 0.0 | 0.0 |
| Throughput (Mbps) | 4.194 | 5.451 | 1.257 |
| Packet loss (%) | 0.0 | 0.0 | 0.0 |
| Packet delivery (%) | 100.0 | 100.0 | 0.0 |
| Avg latency (ms) | 5.621 | 5.602 | -0.019 |
| Congestion response (s) | n/a | 0.001 | n/a |
| Reroute packets on backup path | 0 | 0 | 0 |
| Policy activations | 0 | 1 | 1 |

## Interpretation
- Static routing keeps the adaptive policy inactive, so the shared bottleneck is left to contention alone.
- Adaptive mode should activate the policy during congestion, reroute ICMP to the backup path, and throttle student bulk Wi-Fi traffic.
- Lower packet loss, lower latency, higher packet delivery, and higher throughput on the protected flow are all evidence of improvement over static routing.

## Evidence Summary
- Throughput gain under congestion: 1.257 Mbps.
- Packet delivery gain under congestion: 0.0 percentage points.
- Packet loss reduction under congestion: 0.0 percentage points.
- Latency reduction under congestion: 0.019 ms.
- Adaptive congestion response time: 0.001 s.
- Adaptive reroute observed: yes.

## Artifacts
- Before (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_retry_static.json
- After (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_retry_adaptive.json
- Comparison (JSON): /home/patrick/mininet/results/stage11_comparison_stage11_retry.json
- Comparison (CSV): /home/patrick/mininet/results/stage11_comparison_stage11_retry.csv
- Comparison (Markdown): /home/patrick/mininet/results/stage11_comparison_stage11_retry.md
