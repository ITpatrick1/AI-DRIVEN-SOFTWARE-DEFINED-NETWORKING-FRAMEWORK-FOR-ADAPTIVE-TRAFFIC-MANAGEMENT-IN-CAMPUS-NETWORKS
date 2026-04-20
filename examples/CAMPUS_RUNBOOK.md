# Campus SDN Operations Runbook

Date: 2026-03-21  
Scope: Mininet + Ryu + Adaptive QoS + Web Network Manager

## 1. Offline Preparation (Do This While Online)
Create an offline bundle with all runtime packages and Python artifacts:

```bash
cd ~/mininet
examples/prepare_offline_bundle.sh
```

Default bundle location:
- `~/campus-offline-bundle`

If you need to reinstall later without internet:

```bash
cd ~/mininet
examples/install_offline_bundle.sh ~/campus-offline-bundle
```

The installer stages the bundled `.deb` files into the local apt cache and uses `apt-get --no-download`, so it does not depend on live package repositories during reinstall.

## 2. Start Full Stack
Run in terminal A:

```bash
cd ~/mininet
examples/run_full_stack.sh
```

To run with the real Stage 8 DQN agent instead of the stub:

```bash
cd ~/mininet
examples/run_full_stack.sh --ml-mode dqn
```

Expected readiness markers:
- `Runtime API : http://127.0.0.1:9091`
- `Ryu REST API : http://127.0.0.1:8081`
- `*** Waiting for switches to connect`
- `s1 s2 s3 s4 s5`
- `mininet>` prompt

Dashboard URL:
- `http://127.0.0.1:8080`

## 3. Real Dashboard Operations
From the web UI, all actions are live against the active Mininet topology:
- `Run pingall` (returns real packet loss + RTT summary)
- `Start film download demo` (launches Wi-Fi congestion workload)
- `Stop demo`
- `+ Add device` (creates real host + link)
- `Flows` tab (reads switch flow table)
- `Operations` tab (runtime operation history)

## 4. Priority and Congestion Behavior
When load rises above threshold:
- Adaptive policy activates.
- ICMP reroute policy is enabled for service continuity.
- Student bulk traffic (Wi-Fi) is throttled via OVS queue policy.

When load drops below lower threshold:
- Adaptive policy deactivates.
- Student throttle policy is removed.

## 5. Generate Congestion from CLI
In Mininet CLI (terminal A):

```bash
h_server iperf3 -s -p 5201 >/tmp/iperf3_server.log 2>&1 &
h_wifi1 iperf3 -c 10.0.0.100 -p 5201 -t 45 -R
h_wifi2 iperf3 -c 10.0.0.100 -p 5201 -t 45 -R
```

What to watch in dashboard:
- Core load and Wi-Fi load cards
- Link utilization bars
- Policy badge (`Adaptive reroute ON`)
- Operations tab events

## 6. Verify All Web Options
Run in terminal B after `mininet>` is visible:

```bash
cd ~/mininet
examples/verify_dashboard_options.sh
```

The verifier checks:
- metrics/topology/events/flows APIs
- real `pingall`
- real device add
- operations endpoint
- start/stop stress operations

## 7. Stage 7 Monitoring Module (Separate Component)
Run in terminal C (while stack is active):

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/run_traffic_monitor.sh
```

Monitoring URL:
- `http://127.0.0.1:8090`

What it shows:
- switch/port utilization
- active flows
- warnings (high utilization, drop spikes)
- traffic trend history

Validate Stage 7 end-to-end:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/verify_stage7_monitoring.sh
```

## 8. Stage 8 DQN Adaptive Routing Module
Run DQN agent standalone (against live controller metrics):

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/run_dqn_routing_agent.sh
```

Run full stack in DQN mode (controller + dashboard + topology + DQN):

```bash
cd ~/mininet
examples/run_full_stack.sh --ml-mode dqn
```

Verify Stage 8 end-to-end:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/verify_stage8_dqn.sh
```

## 9. Stage 9 DQN-Ryu Integration (Adaptive Flow Installation)
Stage 9 validates the full integration path:
- congestion is detected by controller
- DQN recommendation is consumed by Ryu
- controller installs/removes adaptive flow rules automatically

Run Stage 9 verifier:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/verify_stage9_dqn_ryu_integration.sh
```

## 10. Stage 10 Flask Monitoring Dashboard
Stage 10 validates the user-facing dashboard with real-time operational data:
- link utilization
- latency trend
- queue depth estimate
- alerts
- active flow rules
- controller actions

Run Stage 10 verifier:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/verify_stage10_dashboard.sh
```

## 11. Stage 11 Testing and Evaluation
Stage 11 produces measurable evidence for the final project report:
- `pingall` connectivity result
- `iperf3` protected-flow throughput under congestion
- ICMP latency and packet delivery under congestion
- congestion response time from detection to adaptive policy activation
- rerouting evidence on the backup server path

Run the Stage 11 comparison:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/run_stage11_evaluation.sh
```

Verify the full Stage 11 workflow:

```bash
cd ~/mininet
source ~/sdn-env/bin/activate
examples/verify_stage11_testing.sh
```

Outputs:
- `results/adaptive_eval_<tag>_static.json`
- `results/adaptive_eval_<tag>_adaptive.json`
- `results/stage11_comparison_<tag>.json`
- `results/stage11_comparison_<tag>.csv`
- `results/stage11_comparison_<tag>.md`

## 12. API Reference
Dashboard API (`127.0.0.1:8080`):
- `GET /api/metrics`
- `GET /api/events`
- `GET /api/topology`
- `GET /api/dashboard`
- `GET /api/devices`
- `POST /api/devices`
- `GET /api/flows?switch=s1`
- `GET /api/operations`
- `POST /api/actions/pingall`
- `POST /api/actions/start-stress`
- `POST /api/actions/stop-stress`

Runtime API (`127.0.0.1:9091`):
- `GET /health`
- `GET /topology`
- `GET /operations`
- `POST /pingall`
- `POST /add_host`
- `POST /start_stress`
- `POST /stop_stress`

Monitoring API (`127.0.0.1:8090`):
- `GET /health`
- `GET /api/summary`
- `GET /api/history`

Ryu REST API (`127.0.0.1:8081`):
- `GET /stats/switches`
- `GET /stats/port/<dpid>`
- `GET /stats/flow/<dpid>`

## 13. Troubleshooting

### 13.1 Controller unreachable (`127.0.0.1:6653`)
```bash
cd ~/mininet
source ~/sdn-env/bin/activate
sudo mn -c
examples/run_full_stack.sh
```

### 13.2 Stale Mininet state or interface exists
```bash
sudo mn -c
```

### 13.3 Dashboard action returns 503
Cause: topology not fully ready or Mininet busy.

Fix:
- wait for `mininet>` prompt
- re-run operation once load settles

### 13.4 `Operation not permitted` deleting topology state
```bash
sudo rm -f /tmp/campus_topology_state.json
unset CAMPUS_TOPOLOGY_STATE_FILE
```

### 13.5 Full-stack script says required command is missing
Run:
```bash
cd ~/mininet
examples/prepare_offline_bundle.sh
```

## 14. Evaluation Run
```bash
cd ~/mininet
examples/run_adaptive_eval.sh
```

Outputs:
- `results/adaptive_eval_<tag>.json`
- `results/adaptive_eval_<tag>.csv`
