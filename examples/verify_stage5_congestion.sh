#!/usr/bin/env bash
set -euo pipefail

# Stage 5 congestion verifier:
# - validates threshold-based congestion logic
# - generates heavy traffic
# - confirms congestion warnings and congested-link identification

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage5_ryu.log"
TOPO_LOG="${LOG_DIR}/stage5_topology.log"
METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
EVENTS_FILE="${CAMPUS_EVENTS_FILE:-/tmp/campus_policy_events.jsonl}"
ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/stage5_ml_action.json}"
MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/stage5_manual_settings.json}"
NETWORK_AUTOMATION_FILE="${CAMPUS_NETWORK_AUTOMATION_FILE:-/tmp/stage5_network_automation.json}"
PW="${SUDO_PASSWORD:-}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

ensure_sudo() {
  # Prompt once up front so we do not fail mid-run.
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S -v >/dev/null
    return 0
  fi
  if ! sudo -n true >/dev/null 2>&1; then
    echo "[INFO] This verifier needs sudo privileges (Mininet/OVS)."
    sudo -v
  fi
}

sudo_run() {
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S "$@"
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
  if [[ -n "${TPID:-}" ]] && kill -0 "${TPID}" 2>/dev/null; then
    kill "${TPID}" 2>/dev/null || true
    wait "${TPID}" 2>/dev/null || true
  fi
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
  sudo_run pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

echo "[1/9] Static congestion checklist..."
for p in \
  "CAMPUS_PORT_CONGEST_HIGH_PCT" \
  "CAMPUS_PORT_CONGEST_LOW_PCT" \
  "congested_ports" \
  "CONGESTION_DETECTED" \
  "port_congestion_on" \
  "switch_port_util_pct"
do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing Stage 5 element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 5 congestion logic found in controller code."

echo "[2/9] Cleaning runtime state..."
sudo_run mn -c >/dev/null 2>&1 || true
sudo_run pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
rm -f \
  "${RYU_LOG}" \
  "${TOPO_LOG}" \
  "${METRICS_FILE}" \
  "${EVENTS_FILE}" \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true
sudo_run rm -f \
  "${RYU_LOG}" \
  "${TOPO_LOG}" \
  "${METRICS_FILE}" \
  "${EVENTS_FILE}" \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true

echo "[3/9] Starting Ryu controller..."
# Use verifier-specific thresholds so congestion is reproducible in shaped labs.
VERIFY_PORT_HIGH="${CAMPUS_PORT_CONGEST_HIGH_PCT:-10}"
VERIFY_PORT_LOW="${CAMPUS_PORT_CONGEST_LOW_PCT:-5}"
CAMPUS_DQN_INTEGRATION_ENABLED="${CAMPUS_DQN_INTEGRATION_ENABLED:-0}" \
CAMPUS_PORT_CONGEST_HIGH_PCT="${VERIFY_PORT_HIGH}" \
CAMPUS_PORT_CONGEST_LOW_PCT="${VERIFY_PORT_LOW}" \
CAMPUS_ML_ACTION_FILE="${ML_ACTION_FILE}" \
CAMPUS_MANUAL_SETTINGS_FILE="${MANUAL_SETTINGS_FILE}" \
CAMPUS_NETWORK_AUTOMATION_FILE="${NETWORK_AUTOMATION_FILE}" \
ryu-manager examples/campus_controller.py >"${RYU_LOG}" 2>&1 &
RPID=$!
READY=0
for _ in $(seq 1 20); do
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

echo "[4/9] Starting topology in hold mode..."
sudo_run -E python3 examples/campus_topology.py --no-cli --hold-seconds 80 >"${TOPO_LOG}" 2>&1 &
TPID=$!

RUNTIME_BASE=""
for _ in $(seq 1 90); do
  if ! kill -0 "${TPID}" 2>/dev/null; then
    echo "[FAIL] Topology process exited before runtime API came up."
    tail -n 120 "${TOPO_LOG}" || true
    exit 1
  fi
  # Accept localhost runtime URL regardless of selected fallback port.
  RUNTIME_BASE="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  if [[ -z "${RUNTIME_BASE}" ]]; then
    # Secondary fallback: parse any local HTTP endpoint that might be logged.
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
  tail -n 160 "${TOPO_LOG}" || true
  exit 1
fi
echo "[PASS] Runtime API detected at ${RUNTIME_BASE}"

echo "[5/9] Starting heavy traffic load..."
curl -fsS -X POST "${RUNTIME_BASE}/start_stress" \
  -H "Content-Type: application/json" \
  -d '{"seconds":25,"reverse_download":true}' >/dev/null
sleep 10

echo "[6/9] Checking congestion warnings in controller logs..."
if ! has_pattern "CONGESTION_DETECTED" "${RYU_LOG}"; then
  echo "[FAIL] No congestion warning detected in controller logs."
  tail -n 180 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Controller emitted congestion warnings."

echo "[7/9] Checking metrics identify congested links/ports..."
python3 - <<PY
import json
import sys
from pathlib import Path

p = Path("${METRICS_FILE}")
if not p.exists():
    print("[FAIL] metrics file missing:", p)
    sys.exit(1)
data = json.loads(p.read_text())
cnt = int(data.get("congested_ports_count", 0))
ports = data.get("congested_ports", [])
if cnt <= 0 or not ports:
    print("[FAIL] congested ports not identified in metrics")
    sys.exit(1)
print(f"[PASS] Congested ports identified: count={cnt} ports={ports}")
PY

echo "[8/9] Checking congestion events stream..."
if [[ ! -f "${EVENTS_FILE}" ]]; then
  echo "[FAIL] events file not found: ${EVENTS_FILE}"
  exit 1
fi
if ! grep -q '"event": "port_congestion_on"' "${EVENTS_FILE}"; then
  echo "[FAIL] port_congestion_on event not found."
  tail -n 120 "${EVENTS_FILE}" || true
  exit 1
fi
echo "[PASS] Congestion events were logged."

echo "[9/9] Stopping stress traffic..."
curl -fsS -X POST "${RUNTIME_BASE}/stop_stress" >/dev/null || true
sleep 3
echo "[PASS] Stage 5 complete: automatic congestion detection is working."
echo
echo "Artifacts:"
echo "  Ryu log     : ${RYU_LOG}"
echo "  Topology log: ${TOPO_LOG}"
echo "  Metrics     : ${METRICS_FILE}"
echo "  Events      : ${EVENTS_FILE}"
