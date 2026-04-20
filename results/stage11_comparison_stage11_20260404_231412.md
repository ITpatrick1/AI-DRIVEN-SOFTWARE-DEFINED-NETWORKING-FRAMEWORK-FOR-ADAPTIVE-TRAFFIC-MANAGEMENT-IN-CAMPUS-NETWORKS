# Stage 11 Testing and Evaluation Report

Tag: stage11_20260404_231412
Generated (UTC): 2026-04-04T21:15:38.725935+00:00

## Tests Executed
- pingall
- iperf3 throughput probes
- latency (ICMP RTT) probes
- congestion stress workload

## Before vs After Adaptive Routing
| Metric | Before Adaptive | After Adaptive | Delta (After-Before) |
|---|---:|---:|---:|
| Throughput (Mbps) | 0.0 | 0.0 | 0.0 |
| Packet loss (%) | 0.0 | 0.0 | 0.0 |
| Packet delivery (%) | 100.0 | 100.0 | 0.0 |
| Avg latency (ms) | 6.695 | 6.567 | -0.128 |
| Congestion response (s) | None | None | None |
| Reroute packets on backup path | 0 | 0 | 0 |

## Interpretation
- Adaptive mode should show policy activation under congestion.
- Backup-path ICMP packets provide rerouting evidence.
- Congestion response time is measured from first congestion event to policy activation.

## Artifacts
- Before (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_20260404_231412_static.json
- After (JSON): /home/patrick/mininet/results/adaptive_eval_stage11_20260404_231412_adaptive.json
- Comparison (JSON): /home/patrick/mininet/results/stage11_comparison_stage11_20260404_231412.json
- Comparison (CSV): /home/patrick/mininet/results/stage11_comparison_stage11_20260404_231412.csv
