#!/usr/bin/env bash
set -euo pipefail

# Stage 9 verifier:
# DQN <-> Ryu integration for congestion-triggered adaptive flow updates.

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"
RYU_LOG="${LOG_DIR}/stage9_ryu.log"
TOPO_LOG="${LOG_DIR}/stage9_topology.log"
DQN_LOG="${LOG_DIR}/stage9_dqn.log"
METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
EVENTS_FILE="${CAMPUS_EVENTS_FILE:-/tmp/campus_policy_events.jsonl}"
ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/campus_ml_action.json}"
MODEL_FILE="${CAMPUS_DQN_MODEL_FILE:-/tmp/campus_dqn_model_stage9.pt}"
FLOW_DUMP="${LOG_DIR}/stage9_policy_flows.txt"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -v >/dev/null
    return 0
  fi
  echo "[INFO] This verifier needs sudo privileges (Mininet/OVS)."
  sudo -v
}

sudo_run() {
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

find_runtime_api() {
  local host="${1:-127.0.0.1}"
  local start_port="${2:-9091}"
  local span="${3:-20}"
  local p
  for p in $(seq "${start_port}" "$((start_port + span - 1))"); do
    if curl -fsS "http://${host}:${p}/health" >/dev/null 2>&1; then
      echo "http://${host}:${p}"
      return 0
    fi
  done
  return 1
}

has_pattern() {
  local pattern="$1"
  local file="$2"
  if command -v rg >/dev/null 2>&1; then
    rg -F -q "${pattern}" "${file}"
  else
    grep -F -q "${pattern}" "${file}"
  fi
}

cleanup() {
  if [[ -n "${DPID:-}" ]] && kill -0 "${DPID}" 2>/dev/null; then
    kill "${DPID}" 2>/dev/null || true
    wait "${DPID}" 2>/dev/null || true
  fi
  if [[ -n "${TPID:-}" ]] && kill -0 "${TPID}" 2>/dev/null; then
    kill "${TPID}" 2>/dev/null || true
    wait "${TPID}" 2>/dev/null || true
  fi
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
  sudo_run pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "examples/campus_controller.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "ryu-manager" >/dev/null 2>&1 || true
  pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
  pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

if ! python3 - <<'PY' >/dev/null 2>&1; then
import torch  # noqa: F401
PY
  echo "[FAIL] PyTorch is not installed in ${VENV_PATH}"
  exit 1
fi

echo "[1/12] Static Stage 9 integration checklist..."
for p in \
  "_trigger_dqn_decision" \
  "_apply_dqn_routing_decision" \
  "_check_dqn_decision_timeout" \
  "dqn_decision_requested" \
  "dqn_decision_applied" \
  "CAMPUS_DQN_INTEGRATION_ENABLED"
do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing Stage 9 integration element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 9 source checks passed."

echo "[2/12] Cleaning runtime state..."
sudo_run mn -c >/dev/null 2>&1 || true
sudo_run pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
sudo_run pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
rm -f "${RYU_LOG}" "${TOPO_LOG}" "${DQN_LOG}" "${METRICS_FILE}" "${EVENTS_FILE}" \
  "${ACTION_FILE}" "${MODEL_FILE}" "${FLOW_DUMP}" >/dev/null 2>&1 || true
sudo_run rm -f "${RYU_LOG}" "${TOPO_LOG}" "${METRICS_FILE}" "${EVENTS_FILE}" \
  "${ACTION_FILE}" "${MODEL_FILE}" "${FLOW_DUMP}" >/dev/null 2>&1 || true

echo "[3/12] Starting Ryu controller (Stage 9 mode)..."
CAMPUS_DQN_INTEGRATION_ENABLED=1 \
CAMPUS_DQN_DECISION_TIMEOUT_S=8 \
CAMPUS_CONGEST_HIGH_MBPS="${CAMPUS_CONGEST_HIGH_MBPS:-15}" \
CAMPUS_CONGEST_LOW_MBPS="${CAMPUS_CONGEST_LOW_MBPS:-6}" \
ryu-manager examples/campus_controller.py >"${RYU_LOG}" 2>&1 &
RPID=$!

READY=0
for _ in $(seq 1 30); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "[FAIL] Ryu process exited early."
    tail -n 120 "${RYU_LOG}" || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    READY=1
    break
  fi
  sleep 0.5
done
if [[ "${READY}" -ne 1 ]]; then
  echo "[FAIL] Ryu did not start listening on :6653."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Ryu is listening on :6653."

echo "[4/12] Starting DQN agent..."
python3 examples/dqn_routing_agent.py \
  --metrics-file "${METRICS_FILE}" \
  --action-file "${ACTION_FILE}" \
  --model-file "${MODEL_FILE}" \
  --interval 1 \
  --no-train >"${DQN_LOG}" 2>&1 &
DPID=$!

echo "[5/12] Starting topology in hold mode..."
sudo_run -E python3 examples/campus_topology.py --no-cli --hold-seconds 140 >"${TOPO_LOG}" 2>&1 &
TPID=$!

RUNTIME_BASE=""
for _ in $(seq 1 120); do
  if ! kill -0 "${TPID}" 2>/dev/null; then
    echo "[FAIL] Topology exited before runtime API became available."
    tail -n 200 "${TOPO_LOG}" || true
    exit 1
  fi
  RUNTIME_BASE="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  if [[ -z "${RUNTIME_BASE}" ]]; then
    RUNTIME_BASE="$(grep -oE 'http://localhost:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  fi
  if [[ -n "${RUNTIME_BASE}" ]]; then
    break
  fi
  RUNTIME_BASE="$(find_runtime_api 127.0.0.1 9091 20 || true)"
  if [[ -n "${RUNTIME_BASE}" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "${RUNTIME_BASE}" ]]; then
  echo "[FAIL] Could not determine runtime API endpoint from topology log."
  tail -n 200 "${TOPO_LOG}" || true
  exit 1
fi
echo "[PASS] Runtime API detected at ${RUNTIME_BASE}"

echo "[6/12] Waiting for live metrics..."
python3 - <<PY
import json
import time
from pathlib import Path

p = Path("${METRICS_FILE}")
deadline = time.time() + 30
while time.time() < deadline:
    if p.exists():
        try:
            d = json.loads(p.read_text())
            sw = d.get("connected_switches", [])
            if isinstance(sw, list) and len(sw) >= 5:
                print("[PASS] metrics ready with connected switches:", sw)
                raise SystemExit(0)
        except Exception:
            pass
    time.sleep(1)
print("[FAIL] metrics not ready in time")
raise SystemExit(1)
PY

echo "[7/12] Triggering congestion workload..."
curl -fsS -X POST "${RUNTIME_BASE}/start_stress" \
  -H "Content-Type: application/json" \
  -d '{"seconds":40,"reverse_download":true,"clients":["h_it1","h_it2","h_net1","h_net2","h_staff1","h_staff2","h_wifi1","h_wifi2"]}' >/dev/null
sleep 6

echo "[8/12] Waiting for DQN-driven controller decision..."
python3 - <<PY
import json
import time
from pathlib import Path

metrics = Path("${METRICS_FILE}")
deadline = time.time() + 35
while time.time() < deadline:
    if metrics.exists():
        try:
            d = json.loads(metrics.read_text())
            if (
                bool(d.get("dqn_integration_enabled", False))
                and float(d.get("dqn_last_decision_ts", 0.0)) > 0.0
                and str(d.get("dqn_last_action_name", "")).strip()
                and str(d.get("last_ml_routing_choice", "")).strip()
            ):
                print("[PASS] DQN decision observed:",
                      d.get("dqn_last_action_name"),
                      "routing=", d.get("last_ml_routing_choice"),
                      "reroute_active=", d.get("reroute_active"))
                raise SystemExit(0)
        except Exception:
            pass
    time.sleep(1)
print("[FAIL] No DQN-driven decision observed in metrics.")
raise SystemExit(1)
PY

echo "[9/12] Validating Stage 9 event stream..."
if [[ ! -f "${EVENTS_FILE}" ]]; then
  echo "[FAIL] events file not found: ${EVENTS_FILE}"
  exit 1
fi
if ! grep -q '"event": "dqn_decision_requested"' "${EVENTS_FILE}"; then
  echo "[FAIL] dqn_decision_requested event not found."
  tail -n 120 "${EVENTS_FILE}" || true
  exit 1
fi
if ! grep -q '"event": "dqn_decision_applied"' "${EVENTS_FILE}"; then
  echo "[FAIL] dqn_decision_applied event not found."
  tail -n 120 "${EVENTS_FILE}" || true
  exit 1
fi
echo "[PASS] DQN request/apply events found."

echo "[10/12] Validating switch flow-rule updates (policy cookie)..."
{
  sudo_run ovs-ofctl -O OpenFlow13 dump-flows s1 || true
  sudo_run ovs-ofctl -O OpenFlow13 dump-flows s2 || true
  sudo_run ovs-ofctl -O OpenFlow13 dump-flows s3 || true
  sudo_run ovs-ofctl -O OpenFlow13 dump-flows s5 || true
} >"${FLOW_DUMP}" 2>/dev/null
if grep -qi 'cookie=0xcafe0001' "${FLOW_DUMP}"; then
  echo "[PASS] Adaptive policy flows currently present on switches."
elif grep -q '"event": "policy_activated"' "${EVENTS_FILE}"; then
  echo "[PASS] Adaptive policy activation event observed (flows may have aged out)."
else
  echo "[FAIL] No adaptive policy flow evidence detected."
  tail -n 120 "${FLOW_DUMP}" || true
  tail -n 120 "${EVENTS_FILE}" || true
  exit 1
fi

echo "[11/12] Stopping stress workload..."
curl -fsS -X POST "${RUNTIME_BASE}/stop_stress" >/dev/null || true
sleep 2

echo "[12/12] Stage 9 verification passed."
echo
echo "[PASS] Stage 9 complete: DQN is integrated with Ryu for adaptive flow updates."
echo "Artifacts:"
echo "  Ryu log      : ${RYU_LOG}"
echo "  Topology log : ${TOPO_LOG}"
echo "  DQN log      : ${DQN_LOG}"
echo "  Metrics      : ${METRICS_FILE}"
echo "  Events       : ${EVENTS_FILE}"
echo "  Action file  : ${ACTION_FILE}"
echo "  Flow dump    : ${FLOW_DUMP}"
