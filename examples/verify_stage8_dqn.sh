#!/usr/bin/env bash
set -euo pipefail

# Stage 8 DQN adaptive routing verifier:
# - validates DQN module structure (state/action/reward)
# - runs controller + topology
# - runs DQN for fixed steps against live metrics
# - confirms controller consumes routing decision from ML action file

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage8_ryu.log"
TOPO_LOG="${LOG_DIR}/stage8_topology.log"
DQN_LOG="${LOG_DIR}/stage8_dqn.log"
METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
EVENTS_FILE="${CAMPUS_EVENTS_FILE:-/tmp/campus_policy_events.jsonl}"
ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/campus_ml_action.json}"
MODEL_FILE="${CAMPUS_DQN_MODEL_FILE:-/tmp/campus_dqn_model_stage8.pt}"

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
  sudo pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
  sudo pkill -f "examples/campus_controller.py" >/dev/null 2>&1 || true
  sudo pkill -f "ryu-manager" >/dev/null 2>&1 || true
  pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
  pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

if ! python3 - <<'PY' >/dev/null 2>&1; then
import torch  # noqa: F401
PY
  echo "[FAIL] PyTorch is not installed in ${VENV_PATH}."
  echo "Install dependencies first (online) using:"
  echo "  examples/prepare_offline_bundle.sh"
  exit 1
fi

echo "[1/11] Static Stage 8 checklist..."
for p in \
  "ACTION_NAMES" \
  "class DQN(" \
  "class ReplayBuffer" \
  "def _extract_state" \
  "def _reward" \
  "routing_choice" \
  "q_values" \
  "queue_pressure_pct" \
  "estimated_latency_ms" \
  "congested_ports_count"
do
  if ! has_pattern "${p}" "examples/dqn_routing_agent.py"; then
    echo "[FAIL] Missing Stage 8 DQN element: ${p}"
    exit 1
  fi
done
for p in "_apply_ml_action_hook" "last_ml_routing_choice" "ml_routing_choice"; do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing Stage 8 controller ML-hook element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 8 source checks passed."

echo "[2/11] Cleaning runtime state..."
sudo mn -c >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
sudo pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
rm -f "${RYU_LOG}" "${TOPO_LOG}" "${DQN_LOG}" "${METRICS_FILE}" "${EVENTS_FILE}" \
  "${ACTION_FILE}" "${MODEL_FILE}" >/dev/null 2>&1 || true
sudo rm -f "${RYU_LOG}" "${TOPO_LOG}" "${METRICS_FILE}" "${EVENTS_FILE}" \
  "${ACTION_FILE}" "${MODEL_FILE}" >/dev/null 2>&1 || true

echo "[3/11] Starting Ryu controller..."
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

echo "[4/11] Starting topology in hold mode..."
sudo -E python3 examples/campus_topology.py --no-cli --hold-seconds 120 >"${TOPO_LOG}" 2>&1 &
TPID=$!

RUNTIME_BASE=""
for _ in $(seq 1 50); do
  if ! kill -0 "${TPID}" 2>/dev/null; then
    echo "[FAIL] Topology exited before runtime API became available."
    tail -n 160 "${TOPO_LOG}" || true
    exit 1
  fi
  RUNTIME_BASE="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  if [[ -z "${RUNTIME_BASE}" ]]; then
    RUNTIME_BASE="$(grep -oE 'http://localhost:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  fi
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

echo "[5/11] Waiting for live metrics..."
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
print("[FAIL] metrics not ready or no connected switches")
raise SystemExit(1)
PY

echo "[6/11] Generating live load to enrich DQN state..."
curl -fsS -X POST "${RUNTIME_BASE}/start_stress" \
  -H "Content-Type: application/json" \
  -d '{"seconds":25,"reverse_download":true}' >/dev/null
sleep 6

echo "[7/11] Running DQN module (fixed verification steps)..."
python3 examples/dqn_routing_agent.py \
  --metrics-file "${METRICS_FILE}" \
  --action-file "${ACTION_FILE}" \
  --model-file "${MODEL_FILE}" \
  --interval 1 \
  --max-steps 8 \
  --no-train >"${DQN_LOG}" 2>&1 &
DPID=$!
wait "${DPID}"
DPID=""
echo "[PASS] DQN run completed."

echo "[8/11] Validating DQN action payload..."
python3 - <<PY
import json
import sys
from pathlib import Path

p = Path("${ACTION_FILE}")
if not p.exists():
    print("[FAIL] action file missing:", p)
    sys.exit(1)
d = json.loads(p.read_text())
checks = []
checks.append(("routing_choice exists", isinstance(d.get("routing_choice"), str) and bool(d.get("routing_choice"))))
checks.append(("force_reroute is bool", isinstance(d.get("force_reroute"), bool)))
checks.append(("q_values map exists", isinstance(d.get("q_values"), dict) and len(d.get("q_values", {})) >= 3))
checks.append(("dqn section exists", isinstance(d.get("dqn"), dict)))
state = (d.get("dqn") or {}).get("state", {})
checks.append(("state has utilization", "max_util_pct" in state))
checks.append(("state has congestion", "congested_ports_count" in state))
checks.append(("state has latency", "estimated_latency_ms" in state))
checks.append(("state has queue signal", "queue_pressure_pct" in state))
checks.append(("dqn has reward", isinstance((d.get("dqn") or {}).get("reward"), (int, float))))
checks.append(("dqn has action_name", isinstance((d.get("dqn") or {}).get("action_name"), str)))

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
if failed:
    print("[FAIL] DQN payload validation failed:", ", ".join(failed))
    sys.exit(1)
print("[PASS] DQN payload validated.")
PY

echo "[9/11] Waiting for controller to consume DQN action..."
python3 - <<PY
import json
import time
from pathlib import Path

metrics = Path("${METRICS_FILE}")
deadline = time.time() + 20
while time.time() < deadline:
    if metrics.exists():
        try:
            d = json.loads(metrics.read_text())
            choice = d.get("last_ml_routing_choice")
            qvals = d.get("last_ml_q_values")
            if choice and isinstance(qvals, dict) and len(qvals) >= 1:
                print("[PASS] Controller consumed ML action:", choice)
                raise SystemExit(0)
        except Exception:
            pass
    time.sleep(1)
print("[FAIL] Controller did not consume DQN action in time.")
raise SystemExit(1)
PY

echo "[10/11] Validating controller event stream..."
if [[ ! -f "${EVENTS_FILE}" ]]; then
  echo "[FAIL] events file not found: ${EVENTS_FILE}"
  exit 1
fi
if ! grep -q '"event": "ml_routing_choice"' "${EVENTS_FILE}"; then
  echo "[FAIL] ml_routing_choice event not found in events stream."
  tail -n 120 "${EVENTS_FILE}" || true
  exit 1
fi
echo "[PASS] ml_routing_choice event logged."

echo "[11/11] Stopping stress traffic..."
curl -fsS -X POST "${RUNTIME_BASE}/stop_stress" >/dev/null || true
sleep 2

echo
echo "[PASS] Stage 8 complete: DQN adaptive routing module is operational."
echo "Artifacts:"
echo "  Ryu log      : ${RYU_LOG}"
echo "  Topology log : ${TOPO_LOG}"
echo "  DQN log      : ${DQN_LOG}"
echo "  Metrics      : ${METRICS_FILE}"
echo "  Events       : ${EVENTS_FILE}"
echo "  ML action    : ${ACTION_FILE}"
