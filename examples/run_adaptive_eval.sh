#!/usr/bin/env bash
set -euo pipefail

# End-to-end evaluator:
# 1) clean mininet
# 2) start controller with adaptive thresholds
# 3) run automated baseline/congestion evaluation
# 4) write JSON+CSV artifacts into results/

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/results}"
TAG="${1:-$(date +%Y%m%d_%H%M%S)}"
PW="${SUDO_PASSWORD:-}"
EVAL_ACTION_FILE="/tmp/campus_ml_action_eval_${TAG}.json"
EVAL_MANUAL_SETTINGS_FILE="${CAMPUS_EVAL_MANUAL_SETTINGS_FILE:-/tmp/campus_manual_settings_eval_${TAG}.json}"

# Tunable default thresholds for evaluation.
HIGH="${CAMPUS_CONGEST_HIGH_MBPS:-40}"
LOW="${CAMPUS_CONGEST_LOW_MBPS:-20}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}."
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
  echo "[INFO] This evaluator needs sudo privileges (Mininet/OVS)."
  sudo -v
}

cleanup() {
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
  rm -f "${EVAL_ACTION_FILE}" >/dev/null 2>&1 || true
  rm -f "${EVAL_MANUAL_SETTINGS_FILE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

mkdir -p "${RESULTS_DIR}"
cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

if ! command -v ryu-manager >/dev/null 2>&1; then
  echo "ryu-manager not found in active virtualenv (${VENV_PATH})."
  exit 1
fi

echo "[1/4] Cleaning Mininet state..."
sudo_run mn -c >/tmp/mn_cleanup.log 2>&1 || true
pkill -f "examples/campus_controller.py" >/dev/null 2>&1 || true
pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
rm -f "${EVAL_ACTION_FILE}" >/dev/null 2>&1 || true

echo "[2/4] Starting controller (high=${HIGH}, low=${LOW})..."
export CAMPUS_CONGEST_HIGH_MBPS="${HIGH}"
export CAMPUS_CONGEST_LOW_MBPS="${LOW}"
export CAMPUS_METRICS_FILE="/tmp/campus_metrics.json"
export CAMPUS_EVENTS_FILE="/tmp/campus_policy_events.jsonl"
export CAMPUS_DQN_INTEGRATION_ENABLED="${CAMPUS_DQN_INTEGRATION_ENABLED:-0}"
export CAMPUS_ML_ACTION_FILE="${EVAL_ACTION_FILE}"
export CAMPUS_MANUAL_SETTINGS_FILE="${EVAL_MANUAL_SETTINGS_FILE}"
controller_env=(
  "CAMPUS_CONGEST_HIGH_MBPS=${CAMPUS_CONGEST_HIGH_MBPS}"
  "CAMPUS_CONGEST_LOW_MBPS=${CAMPUS_CONGEST_LOW_MBPS}"
  "CAMPUS_METRICS_FILE=${CAMPUS_METRICS_FILE}"
  "CAMPUS_EVENTS_FILE=${CAMPUS_EVENTS_FILE}"
  "CAMPUS_DQN_INTEGRATION_ENABLED=${CAMPUS_DQN_INTEGRATION_ENABLED}"
  "CAMPUS_ML_ACTION_FILE=${CAMPUS_ML_ACTION_FILE}"
  "CAMPUS_MANUAL_SETTINGS_FILE=${CAMPUS_MANUAL_SETTINGS_FILE}"
)
rm -f /tmp/campus_metrics.json /tmp/campus_policy_events.jsonl "${CAMPUS_MANUAL_SETTINGS_FILE}"
env "${controller_env[@]}" ryu-manager examples/campus_controller.py >/tmp/ryu_campus.log 2>&1 &
RPID=$!
READY=0
for _ in $(seq 1 40); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "Controller failed to start:"
    tail -n 120 /tmp/ryu_campus.log || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    READY=1
    break
  fi
  sleep 0.5
done
if [[ "${READY}" -ne 1 ]]; then
  echo "Controller port 6653 not listening:"
  tail -n 120 /tmp/ryu_campus.log || true
  exit 1
fi

echo "[3/4] Running adaptive evaluation..."
if [[ -n "${PW}" ]]; then
  env_keep=(
    "CAMPUS_CONGEST_HIGH_MBPS=${CAMPUS_CONGEST_HIGH_MBPS}"
    "CAMPUS_CONGEST_LOW_MBPS=${CAMPUS_CONGEST_LOW_MBPS}"
    "CAMPUS_METRICS_FILE=${CAMPUS_METRICS_FILE}"
    "CAMPUS_EVENTS_FILE=${CAMPUS_EVENTS_FILE}"
    "CAMPUS_DQN_INTEGRATION_ENABLED=${CAMPUS_DQN_INTEGRATION_ENABLED}"
    "CAMPUS_ML_ACTION_FILE=${CAMPUS_ML_ACTION_FILE}"
    "CAMPUS_MANUAL_SETTINGS_FILE=${CAMPUS_MANUAL_SETTINGS_FILE}"
  )
  printf '%s\n' "${PW}" | sudo -S env "${env_keep[@]}" python3 examples/adaptive_eval.py \
    --results-dir "${RESULTS_DIR}" --tag "${TAG}"
else
  sudo -E python3 examples/adaptive_eval.py --results-dir "${RESULTS_DIR}" --tag "${TAG}"
fi

echo "[4/4] Done. Artifacts:"
echo "  ${RESULTS_DIR}/adaptive_eval_${TAG}.json"
echo "  ${RESULTS_DIR}/adaptive_eval_${TAG}.csv"
