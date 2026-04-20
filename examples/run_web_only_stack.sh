#!/usr/bin/env bash
set -euo pipefail

# Web-only launcher:
# - starts controller/dashboard/ML/topology in background
# - keeps topology alive for long hold duration
# - user can operate entirely from browser + dashboard APIs

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
ML_MODE="${CAMPUS_ML_MODE:-stub}"   # stub | dqn | none

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-ml)
      ML_MODE="none"
      ;;
    --ml-mode)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[FAIL] Missing value for --ml-mode (expected: stub|dqn|none)"
        exit 1
      fi
      ML_MODE="$1"
      ;;
    --ml-mode=*)
      ML_MODE="${1#*=}"
      ;;
    *)
      echo "[FAIL] Unknown argument: $1"
      echo "Usage: $0 [--no-ml|--ml-mode stub|dqn|none]"
      exit 1
      ;;
  esac
  shift
done

case "${ML_MODE}" in
  stub|dqn|none) ;;
  *)
    echo "[FAIL] Invalid ML mode: ${ML_MODE} (expected: stub|dqn|none)"
    exit 1
    ;;
esac

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
  echo "[INFO] This launcher needs sudo privileges (Mininet/OVS)."
  sudo -v
}

cleanup_stale() {
  sudo pkill -f "campus_topology.py" >/dev/null 2>&1 || true
  sudo pkill -f "campus_controller.py" >/dev/null 2>&1 || true
  sudo pkill -f "ryu-manager" >/dev/null 2>&1 || true
  pkill -f "campus_dashboard.py" >/dev/null 2>&1 || true
  pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
  pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
  sudo mn -c >/tmp/mn_cleanup_web.log 2>&1 || true
}

wait_http() {
  local url="$1"
  local tries="${2:-20}"
  local delay="${3:-1}"
  local i
  for i in $(seq 1 "${tries}"); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      return 0
    fi
    sleep "${delay}"
  done
  return 1
}

find_runtime_api() {
  local host="$1"
  local start_port="$2"
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

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

export CAMPUS_CONGEST_HIGH_MBPS="${CAMPUS_CONGEST_HIGH_MBPS:-40}"
export CAMPUS_CONGEST_LOW_MBPS="${CAMPUS_CONGEST_LOW_MBPS:-20}"
export CAMPUS_METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
export CAMPUS_EVENTS_FILE="${CAMPUS_EVENTS_FILE:-/tmp/campus_policy_events.jsonl}"
export CAMPUS_ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/campus_ml_action.json}"
export CAMPUS_DQN_MODEL_FILE="${CAMPUS_DQN_MODEL_FILE:-/tmp/campus_dqn_model.pt}"
export CAMPUS_RYU_WSAPI_HOST="${CAMPUS_RYU_WSAPI_HOST:-127.0.0.1}"
export CAMPUS_RYU_WSAPI_PORT="${CAMPUS_RYU_WSAPI_PORT:-8081}"
mkdir -p "${HOME}/.cache"
export CAMPUS_TOPOLOGY_STATE_FILE="${CAMPUS_TOPOLOGY_STATE_FILE:-${HOME}/.cache/campus_topology_state.json}"
export CAMPUS_RUNTIME_API_HOST="${CAMPUS_RUNTIME_API_HOST:-127.0.0.1}"
export CAMPUS_RUNTIME_API_PORT="${CAMPUS_RUNTIME_API_PORT:-9091}"
HOLD_SECONDS="${CAMPUS_WEB_HOLD_SECONDS:-86400}"
PID_FILE="/tmp/campus_web_stack.pids"
TOPO_LOG="/tmp/campus_topology_web.log"

echo "[1/5] Cleaning stale processes..."
cleanup_stale
rm -f "${CAMPUS_METRICS_FILE}" "${CAMPUS_EVENTS_FILE}" "${CAMPUS_ML_ACTION_FILE}" "${PID_FILE}" "${TOPO_LOG}" || true
if [[ -e "${CAMPUS_TOPOLOGY_STATE_FILE}" ]]; then
  rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" 2>/dev/null || sudo rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" || true
fi

echo "[2/5] Starting controller..."
nohup ryu-manager \
  --wsapi-host "${CAMPUS_RYU_WSAPI_HOST}" \
  --wsapi-port "${CAMPUS_RYU_WSAPI_PORT}" \
  examples/campus_controller.py ryu.app.ofctl_rest \
  >/tmp/ryu_campus.log 2>&1 < /dev/null &
RYU_PID=$!

echo "[3/5] Starting dashboard at http://127.0.0.1:8080 ..."
nohup python3 examples/campus_dashboard.py --host 127.0.0.1 --port 8080 \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --topology-state-file "${CAMPUS_TOPOLOGY_STATE_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/campus_dashboard.log 2>&1 < /dev/null &
DASH_PID=$!

ML_PID=""
if [[ "${ML_MODE}" == "stub" ]]; then
  echo "[4/5] Starting ML policy stub..."
  nohup python3 examples/ml_policy_stub.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/campus_ml_stub.log 2>&1 < /dev/null &
  ML_PID=$!
elif [[ "${ML_MODE}" == "dqn" ]]; then
  echo "[4/5] Starting DQN routing agent..."
  DQN_ARGS=()
  if [[ "${CAMPUS_DQN_NO_TRAIN:-0}" == "1" ]]; then
    DQN_ARGS+=(--no-train)
  fi
  nohup python3 examples/dqn_routing_agent.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" \
    --model-file "${CAMPUS_DQN_MODEL_FILE}" \
    "${DQN_ARGS[@]}" >/tmp/campus_dqn.log 2>&1 < /dev/null &
  ML_PID=$!
else
  echo "[4/5] ML agent disabled."
fi

echo "[5/5] Starting topology (web-only hold ${HOLD_SECONDS}s)..."
: >"${TOPO_LOG}"
nohup sudo -E python3 examples/campus_topology.py --no-cli --hold-seconds "${HOLD_SECONDS}" >"${TOPO_LOG}" 2>&1 < /dev/null &
TOPO_PID=$!

{
  echo "RYU_PID=${RYU_PID}"
  echo "DASH_PID=${DASH_PID}"
  [[ -n "${ML_PID}" ]] && echo "ML_PID=${ML_PID}"
  echo "TOPO_PID=${TOPO_PID}"
} >"${PID_FILE}"

if ! wait_http "http://127.0.0.1:8080/api/metrics" 30 1; then
  echo "[FAIL] Dashboard did not come up on http://127.0.0.1:8080"
  echo "See logs: /tmp/campus_dashboard.log, ${TOPO_LOG}, /tmp/ryu_campus.log"
  exit 1
fi

if ! wait_http "http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}/stats/switches" 30 1; then
  echo "[FAIL] Ryu REST API did not become ready on http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
  echo "See logs: /tmp/ryu_campus.log"
  exit 1
fi

RUNTIME_BASE=""
for _ in $(seq 1 40); do
  if ! kill -0 "${TOPO_PID}" 2>/dev/null; then
    echo "[FAIL] Topology process exited early."
    echo "See log: ${TOPO_LOG}"
    tail -n 160 "${TOPO_LOG}" || true
    exit 1
  fi
  RUNTIME_BASE="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "${TOPO_LOG}" 2>/dev/null | tail -n1 || true)"
  if [[ -z "${RUNTIME_BASE}" ]]; then
    RUNTIME_BASE="$(grep -oE 'http://localhost:[0-9]+' "${TOPO_LOG}" 2>/dev/null | tail -n1 || true)"
  fi
  if [[ -n "${RUNTIME_BASE}" ]] && curl -fsS "${RUNTIME_BASE}/health" >/dev/null 2>&1; then
    break
  fi
  RUNTIME_BASE="$(find_runtime_api "${CAMPUS_RUNTIME_API_HOST}" "${CAMPUS_RUNTIME_API_PORT}" 20 || true)"
  if [[ -n "${RUNTIME_BASE}" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "${RUNTIME_BASE}" ]]; then
  echo "[FAIL] Runtime API did not become ready."
  echo "See logs: ${TOPO_LOG}, /tmp/ryu_campus.log, /tmp/campus_dashboard.log"
  exit 1
fi

echo
echo "[PASS] Web-only stack is running."
echo "Dashboard : http://127.0.0.1:8080"
echo "Ryu REST  : http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
echo "Runtime API: ${RUNTIME_BASE}"
echo "PID file  : ${PID_FILE}"
echo
echo "To stop everything:"
echo "  examples/stop_web_only_stack.sh"
