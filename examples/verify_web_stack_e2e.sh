#!/usr/bin/env bash
set -euo pipefail

# End-to-end web verification in one shell:
# - starts controller/dashboard/ml/topology
# - runs verify_dashboard_options.sh
# - tears everything down cleanly

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
PW="${SUDO_PASSWORD:-}"
HOLD_SECONDS="${CAMPUS_WEB_HOLD_SECONDS:-300}"
SUDO_READY=0

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

sudo_run() {
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S -v >/dev/null
    return 0
  fi
  echo "[INFO] This verifier needs sudo privileges (Mininet/OVS)."
  sudo -v
}

cleanup() {
  kill "${RYU_PID:-}" "${DASH_PID:-}" "${ML_PID:-}" 2>/dev/null || true
  wait "${RYU_PID:-}" "${DASH_PID:-}" "${ML_PID:-}" 2>/dev/null || true
  if [[ "${SUDO_READY:-0}" == "1" ]]; then
    sudo_run pkill -f "examples/campus_topology.py --no-cli --hold-seconds ${HOLD_SECONDS}" >/dev/null 2>&1 || true
    sudo_run mn -c >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo
SUDO_READY=1

echo "[1/6] Cleaning stale processes..."
sudo_run pkill -f "examples/campus_topology.py" >/dev/null 2>&1 || true
sudo_run pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
pkill -f "campus_dashboard.py --host 127.0.0.1 --port 8080" >/dev/null 2>&1 || true
pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
sudo_run mn -c >/dev/null 2>&1 || true

export CAMPUS_CONGEST_HIGH_MBPS="${CAMPUS_CONGEST_HIGH_MBPS:-40}"
export CAMPUS_CONGEST_LOW_MBPS="${CAMPUS_CONGEST_LOW_MBPS:-20}"
export CAMPUS_DQN_INTEGRATION_ENABLED="${CAMPUS_DQN_INTEGRATION_ENABLED:-0}"
export CAMPUS_METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
export CAMPUS_EVENTS_FILE="${CAMPUS_EVENTS_FILE:-/tmp/campus_policy_events.jsonl}"
export CAMPUS_ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/campus_ml_action.json}"
export CAMPUS_MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/campus_manual_settings_e2e.json}"
export CAMPUS_SECURITY_POLICY_FILE="${CAMPUS_SECURITY_POLICY_FILE:-/tmp/campus_security_policy_e2e.json}"
export CAMPUS_NETWORK_AUTOMATION_FILE="${CAMPUS_NETWORK_AUTOMATION_FILE:-/tmp/campus_network_automation_e2e.json}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"
mkdir -p "${HOME}/.cache"
export CAMPUS_TOPOLOGY_STATE_FILE="${CAMPUS_TOPOLOGY_STATE_FILE:-${HOME}/.cache/campus_topology_state.json}"
export CAMPUS_RUNTIME_API_HOST="${CAMPUS_RUNTIME_API_HOST:-127.0.0.1}"
export CAMPUS_RUNTIME_API_PORT="${CAMPUS_RUNTIME_API_PORT:-9091}"
rm -f \
  "${CAMPUS_METRICS_FILE}" \
  "${CAMPUS_EVENTS_FILE}" \
  "${CAMPUS_ML_ACTION_FILE}" \
  "${CAMPUS_MANUAL_SETTINGS_FILE}" \
  "${CAMPUS_SECURITY_POLICY_FILE}" \
  "${CAMPUS_NETWORK_AUTOMATION_FILE}" || true
rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" 2>/dev/null || sudo_run rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" || true

echo "[2/6] Starting controller..."
ryu-manager examples/campus_controller.py >/tmp/ryu_campus_e2e.log 2>&1 &
RYU_PID=$!
CONTROLLER_READY=0
for _ in $(seq 1 30); do
  if ! kill -0 "${RYU_PID}" 2>/dev/null; then
    echo "[FAIL] Controller exited before listening on :6653."
    tail -n 120 /tmp/ryu_campus_e2e.log || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    CONTROLLER_READY=1
    break
  fi
  sleep 0.5
done
if [[ "${CONTROLLER_READY}" -ne 1 ]]; then
  echo "[FAIL] Controller did not start listening on :6653."
  tail -n 120 /tmp/ryu_campus_e2e.log || true
  exit 1
fi

echo "[3/6] Starting dashboard..."
python3 examples/campus_dashboard.py --host 127.0.0.1 --port 8080 \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --topology-state-file "${CAMPUS_TOPOLOGY_STATE_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/campus_dashboard_e2e.log 2>&1 &
DASH_PID=$!

echo "[4/6] Starting ML stub + topology hold (${HOLD_SECONDS}s)..."
python3 examples/ml_policy_stub.py \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/campus_ml_stub_e2e.log 2>&1 &
ML_PID=$!
sudo_run -E python3 examples/campus_topology.py --no-cli --hold-seconds "${HOLD_SECONDS}" >/tmp/campus_topology_e2e.log 2>&1 &

echo "[5/6] Waiting for dashboard + runtime APIs..."
READY=0
for _ in $(seq 1 90); do
  if curl -fsS "http://127.0.0.1:8080/api/metrics" >/dev/null 2>&1 \
    && curl -fsS "http://127.0.0.1:8080/api/operations" >/dev/null 2>&1; then
    READY=1
    break
  fi
  sleep 1
done
if [[ "${READY}" -ne 1 ]]; then
  echo "[FAIL] Dashboard/runtime APIs did not become ready."
  echo "--- /tmp/campus_dashboard_e2e.log ---"
  tail -n 80 /tmp/campus_dashboard_e2e.log || true
  echo "--- /tmp/campus_topology_e2e.log ---"
  tail -n 120 /tmp/campus_topology_e2e.log || true
  exit 1
fi

echo "[6/6] Running dashboard option verification..."
examples/verify_dashboard_options.sh
echo
echo "[PASS] End-to-end web verification complete."
