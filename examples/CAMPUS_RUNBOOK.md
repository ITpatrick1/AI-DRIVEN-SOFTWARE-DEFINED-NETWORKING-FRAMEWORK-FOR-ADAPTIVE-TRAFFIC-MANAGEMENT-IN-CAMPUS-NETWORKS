# Campus SDN Operations Runbook

Date: 2026-05-10  
Student: Patrick Manishimwe  
Scope: SDN Architecture for Adaptive Traffic Management — full implementation  
Reference: SDN_Capstone_Project_Guide_Patrick_full.pdf

---

## Quick Start (PDF Guide — Chapter 5-9 Commands)

### Step 1 — Activate the virtual environment
```bash
source ~/sdn-env/bin/activate
```

### Step 2 — Start Ryu controller (Terminal 1)
```bash
cd ~/Desktop/campus-sdn
ryu-manager examples/campus_controller.py ryu.app.ofctl_rest --wsapi-port 8081 --verbose
# Wait for: "Connected from address 127.0.0.1" for each switch
```

### Step 3 — Start Mininet topology (Terminal 2, new window)
```bash
cd ~/Desktop/campus-sdn
sudo -E python3 examples/campus_topology.py
# Wait for: mininet> prompt
# Verify connectivity:  mininet> pingall
```

### Step 4 — Start Flask dashboard (Terminal 3)
```bash
cd ~/Desktop/campus-sdn
source ~/sdn-env/bin/activate
python3 examples/campus_dashboard.py --host 0.0.0.0 --port 8080 \
  --metrics-file /tmp/campus_metrics.json
# Open browser: http://localhost:8080
```

### Step 5 — Start DQN routing agent (Terminal 4, optional)
```bash
cd ~/Desktop/campus-sdn
source ~/sdn-env/bin/activate
python3 examples/dqn_routing_agent.py \
  --metrics-file /tmp/campus_metrics.json \
  --action-file /tmp/campus_ml_action.json
```

### Step 6 — Start traffic monitor (Terminal 5, optional)
```bash
cd ~/Desktop/campus-sdn
source ~/sdn-env/bin/activate
python3 examples/traffic_monitor.py --host 0.0.0.0 --port 8090 \
  --ryu-base http://127.0.0.1:8081
# Open browser: http://localhost:8090
```

### OR — One-command full stack launch
```bash
cd ~/Desktop/campus-sdn
examples/run_full_stack.sh                    # interactive (opens mininet> CLI)
# OR
examples/run_full_stack.sh --ml-mode dqn      # with real DQN agent
# OR
CAMPUS_DASHBOARD_HOST=0.0.0.0 examples/start_campus.sh  # background, LAN-accessible
```

### Stop everything
```bash
cd ~/Desktop/campus-sdn
examples/stop_web_only_stack.sh
# OR press Ctrl-C in run_full_stack.sh terminal
```

---

## Campus Network Topology (Chapter 5)

| Zone | Switch | Subnet | Hosts | Link to Core | Host Links |
|------|--------|--------|-------|-------------|------------|
| Server Farm | s1 (core) | 10.0.0.0/24 | h_server (10.0.0.100), h_server_b (10.0.0.101) | — | 1 Gbps, 1ms |
| IT Lab | s2 | 10.0.0.0/24 | h_it1 (10.0.0.11), h_it2 (10.0.0.12) | 1 Gbps, 1ms | 100 Mbps, 1ms |
| Networking Lab | s3 | 10.0.0.0/24 | h_net1 (10.0.0.21), h_net2 (10.0.0.22) | 1 Gbps, 1ms | 100 Mbps, 1ms |
| Staff LAN | s4 | 10.0.0.0/24 | h_staff1 (10.0.0.31), h_staff2 (10.0.0.32) | 1 Gbps, 1ms | 100 Mbps, 1ms |
| Student Wi-Fi | s5 | 10.0.0.0/24 | h_wifi1 (10.0.0.41), h_wifi2 (10.0.0.42) | 1 Gbps, 2ms | 50 Mbps, 5ms |

Controller: Ryu on 127.0.0.1:6633 | OpenFlow 1.3

---

## Performance Targets (Chapter 11 Test Plan)

| Test | Command | Expected |
|------|---------|----------|
| Full connectivity | `pingall` | 0% loss |
| IT Lab bandwidth | `iperf h_it1 h_server` | >90 Mbps |
| Wi-Fi bandwidth | `iperf h_wifi1 h_server` | >45 Mbps |
| Wired latency | `h_it1 ping -c 10 h_server` | <5 ms RTT |
| Wi-Fi latency | `h_wifi1 ping -c 10 h_server` | <15 ms RTT |
| Flow rules | `sh ovs-ofctl -O OpenFlow13 dump-flows s1` | Rules present |
| Congestion detection | Multi-stream iperf | Controller logs warning |
| DQN rerouting | Run dqn_routing_agent.py | Rerouted in <30s |
| Dashboard live | Open localhost:8080 | Live metrics visible |

---

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

To open the dashboard from the physical host while it runs inside the VM:

```bash
cd ~/mininet
CAMPUS_DASHBOARD_HOST=0.0.0.0 examples/run_web_only_stack.sh
```

Then browse to `http://<vm-ip>:8080` from the host PC. You can find the VM IP with:

```bash
ip -br addr
```

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
Dashboard API (`127.0.0.1:8080` by default, or `http://<vm-ip>:8080` if launched with `CAMPUS_DASHBOARD_HOST=0.0.0.0`):
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
