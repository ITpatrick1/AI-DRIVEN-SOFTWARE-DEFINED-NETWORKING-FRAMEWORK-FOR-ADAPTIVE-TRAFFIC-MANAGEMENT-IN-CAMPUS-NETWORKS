#!/usr/bin/env bash
set -euo pipefail

# Stage 6 policy-based routing verifier:
# - validates policy classes in controller code
# - boots controller + topology
# - confirms flow rules include queue-based class handling
# - confirms runtime policy metadata and live stress operation

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage6_ryu.log"
TOPO_LOG="${LOG_DIR}/stage6_topology.log"
METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
S2_FLOWS="${LOG_DIR}/stage6_s2_flows.txt"
S5_FLOWS="${LOG_DIR}/stage6_s5_flows.txt"
ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/stage6_ml_action.json}"
MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/stage6_manual_settings.json}"
SECURITY_POLICY_FILE="${CAMPUS_SECURITY_POLICY_FILE:-/tmp/stage6_security_policy.json}"
NETWORK_AUTOMATION_FILE="${CAMPUS_NETWORK_AUTOMATION_FILE:-/tmp/stage6_network_automation.json}"
PW="${SUDO_PASSWORD:-}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

ensure_sudo() {
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

echo "[1/9] Static policy checklist..."
for p in \
  "POLICY_ENGINE_COOKIE" \
  "_install_policy_engine_flows" \
  "EXAM_TCP_PORT" \
  "AUTH_UDP_PORTS" \
  "NORMAL_TCP_PORTS" \
  "BULK_TCP_PORTS" \
  "HIGH_PRIORITY_QUEUE_ID" \
  "MEDIUM_PRIORITY_QUEUE_ID" \
  "LOW_PRIORITY_QUEUE_ID"
do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing Stage 6 policy element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 6 policy elements found in controller code."

echo "[2/9] Cleaning runtime state..."
sudo_run mn -c >/dev/null 2>&1 || true
sudo_run pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
rm -f \
  "${RYU_LOG}" \
  "${TOPO_LOG}" \
  "${METRICS_FILE}" \
  "${S2_FLOWS}" \
  "${S5_FLOWS}" \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${SECURITY_POLICY_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true
sudo_run rm -f \
  "${RYU_LOG}" \
  "${TOPO_LOG}" \
  "${METRICS_FILE}" \
  "${S2_FLOWS}" \
  "${S5_FLOWS}" \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${SECURITY_POLICY_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true

echo "[3/9] Starting Ryu controller..."
CAMPUS_DQN_INTEGRATION_ENABLED=0 \
CAMPUS_ML_ACTION_FILE="${ML_ACTION_FILE}" \
CAMPUS_MANUAL_SETTINGS_FILE="${MANUAL_SETTINGS_FILE}" \
CAMPUS_SECURITY_POLICY_FILE="${SECURITY_POLICY_FILE}" \
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
  tail -n 180 "${TOPO_LOG}" || true
  exit 1
fi
echo "[PASS] Runtime API detected at ${RUNTIME_BASE}"

echo "[5/9] Checking policy engine programming logs..."
if ! grep -q "Policy Engine installed" "${RYU_LOG}"; then
  echo "[FAIL] Policy engine flow-install logs not found in controller output."
  tail -n 160 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Controller reports policy flow installation."

echo "[6/9] Validating queue-based policy flow rules..."
sudo_run ovs-ofctl -O OpenFlow13 dump-flows s2 >"${S2_FLOWS}" 2>/dev/null
sudo_run ovs-ofctl -O OpenFlow13 dump-flows s5 >"${S5_FLOWS}" 2>/dev/null
cat "${S2_FLOWS}" "${S5_FLOWS}" >"${LOG_DIR}/stage6_all_flows.txt"

if ! grep -E -q 'tp_dst=8443.*set_queue:0' "${LOG_DIR}/stage6_all_flows.txt"; then
  echo "[FAIL] Exam policy flow (queue 0) not found."
  exit 1
fi
if ! grep -E -q 'tp_dst=(67|68|1812|1813).*set_queue:0' "${LOG_DIR}/stage6_all_flows.txt"; then
  echo "[FAIL] Authentication policy flow (queue 0) not found."
  exit 1
fi
if ! grep -E -q 'tp_dst=(80|443).*set_queue:1' "${LOG_DIR}/stage6_all_flows.txt"; then
  echo "[FAIL] Normal browsing policy flow (queue 1) not found."
  exit 1
fi
if ! grep -E -q 'tp_dst=(5201|8080|6881).*set_queue:2' "${LOG_DIR}/stage6_all_flows.txt"; then
  echo "[FAIL] Entertainment/bulk policy flow (queue 2) not found."
  exit 1
fi
echo "[PASS] Queue-based policy flows detected for all traffic classes."

echo "[7/9] Validating policy metadata in metrics..."
python3 - <<PY
import json
import sys
from pathlib import Path

p = Path("${METRICS_FILE}")
if not p.exists():
    print("[FAIL] metrics file missing:", p)
    sys.exit(1)
data = json.loads(p.read_text())
profiles = data.get("priority_profiles", {})
required = [
    "exam_traffic",
    "authentication_traffic",
    "normal_browsing",
    "entertainment_bulk_download",
]
missing = [k for k in required if k not in profiles]
if missing:
    print("[FAIL] missing priority profiles:", ", ".join(missing))
    sys.exit(1)
rules = data.get("policy_engine_rules", {})
if not isinstance(rules, dict) or len(rules) < 3:
    print("[FAIL] policy_engine_rules missing or too small:", rules)
    sys.exit(1)
print("[PASS] metrics expose policy profiles and installed policy rules.")
PY

echo "[8/9] Running live low-priority stress flow..."
curl -fsS -X POST "${RUNTIME_BASE}/start_stress" \
  -H "Content-Type: application/json" \
  -d '{"seconds":20,"reverse_download":true}' >/dev/null
sleep 4
OPS_JSON="$(curl -fsS "${RUNTIME_BASE}/operations")"
python3 - <<PY
import json, sys
ops = json.loads("""${OPS_JSON}""")
running = ops.get("running_stress_clients", [])
if not running:
    print("[FAIL] stress clients are not running")
    sys.exit(1)
print("[PASS] stress clients running:", ", ".join(running))
PY
curl -fsS -X POST "${RUNTIME_BASE}/stop_stress" >/dev/null || true

echo "[9/9] Stage 6 verification passed."
echo
echo "[PASS] Stage 6 complete: policy-based routing/priority control is operational."
echo "Artifacts:"
echo "  Ryu log      : ${RYU_LOG}"
echo "  Topology log : ${TOPO_LOG}"
echo "  Metrics      : ${METRICS_FILE}"
echo "  Flow dump s2 : ${S2_FLOWS}"
echo "  Flow dump s5 : ${S5_FLOWS}"
