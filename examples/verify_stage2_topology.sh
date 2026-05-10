#!/usr/bin/env bash
set -euo pipefail

# Stage 2 topology verifier:
# - starts Ryu controller
# - launches campus topology in non-interactive mode
# - validates controller connectivity + pingall + zone-to-server tests

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage2_ryu.log"
TOPO_LOG="${LOG_DIR}/stage2_topology.log"
ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/stage2_ml_action.json}"
MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/stage2_manual_settings.json}"
SECURITY_POLICY_FILE="${CAMPUS_SECURITY_POLICY_FILE:-/tmp/stage2_security_policy.json}"
NETWORK_AUTOMATION_FILE="${CAMPUS_NETWORK_AUTOMATION_FILE:-/tmp/stage2_network_automation.json}"
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
  echo "[INFO] This verifier needs sudo privileges (Mininet/OVS)."
  sudo -v
}

sudo_run() {
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S "$@"
  else
    sudo "$@"
  fi
}

cleanup() {
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

echo "[1/5] Cleaning Mininet state..."
sudo_run mn -c >/dev/null 2>&1 || true
sudo_run pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
rm -f \
  "${RYU_LOG}" \
  "${TOPO_LOG}" \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${SECURITY_POLICY_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true
sudo_run rm -f \
  "${ML_ACTION_FILE}" \
  "${MANUAL_SETTINGS_FILE}" \
  "${SECURITY_POLICY_FILE}" \
  "${NETWORK_AUTOMATION_FILE}" >/dev/null 2>&1 || true

echo "[2/5] Starting Ryu controller..."
CAMPUS_DQN_INTEGRATION_ENABLED=0 \
CAMPUS_ML_ACTION_FILE="${ML_ACTION_FILE}" \
CAMPUS_MANUAL_SETTINGS_FILE="${MANUAL_SETTINGS_FILE}" \
CAMPUS_SECURITY_POLICY_FILE="${SECURITY_POLICY_FILE}" \
CAMPUS_NETWORK_AUTOMATION_FILE="${NETWORK_AUTOMATION_FILE}" \
ryu-manager examples/campus_controller.py >"${RYU_LOG}" 2>&1 &
RPID=$!

RYU_READY=0
for _ in $(seq 1 20); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "[FAIL] Ryu process exited early."
    tail -n 120 "${RYU_LOG}" || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    RYU_READY=1
    break
  fi
  sleep 0.5
done
if [[ "${RYU_READY}" -ne 1 ]]; then
  echo "[FAIL] Ryu did not start listening on :6653 in time."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi

echo "[3/5] Running campus topology in verification mode..."
if ! sudo_run -E env \
  CAMPUS_ML_ACTION_FILE="${ML_ACTION_FILE}" \
  CAMPUS_MANUAL_SETTINGS_FILE="${MANUAL_SETTINGS_FILE}" \
  CAMPUS_SECURITY_POLICY_FILE="${SECURITY_POLICY_FILE}" \
  CAMPUS_NETWORK_AUTOMATION_FILE="${NETWORK_AUTOMATION_FILE}" \
  python3 examples/campus_topology.py --no-cli >"${TOPO_LOG}" 2>&1; then
  echo "[FAIL] Topology execution failed."
  echo "----- Last topology log lines -----"
  tail -n 80 "${TOPO_LOG}" || true
  exit 1
fi

echo "[4/5] Validating Stage 2 checkpoints..."
if grep -q "Controller is not reachable" "${TOPO_LOG}"; then
  echo "[FAIL] Switches did not connect to controller."
  tail -n 80 "${TOPO_LOG}" || true
  exit 1
fi

if ! grep -q "\*\*\* Full connectivity test (pingall)" "${TOPO_LOG}"; then
  echo "[FAIL] pingall validation did not run."
  tail -n 80 "${TOPO_LOG}" || true
  exit 1
fi

if ! grep -q "\*\*\* Results: 0% dropped" "${TOPO_LOG}"; then
  echo "[FAIL] pingall did not achieve full connectivity."
  tail -n 80 "${TOPO_LOG}" || true
  exit 1
fi

if ! grep -q "\*\*\* Zone-to-server reachability checks" "${TOPO_LOG}"; then
  echo "[FAIL] Zone-to-server checks were not executed."
  tail -n 80 "${TOPO_LOG}" || true
  exit 1
fi

echo "[5/5] Stage 2 verification passed."
echo "[PASS] Full 5-zone topology is operational."
echo "[PASS] Switches connected to controller."
echo "[PASS] pingall full connectivity confirmed."
echo "[PASS] Zone hosts reach server."
echo
echo "Logs:"
echo "  Ryu      : ${RYU_LOG}"
echo "  Topology : ${TOPO_LOG}"
