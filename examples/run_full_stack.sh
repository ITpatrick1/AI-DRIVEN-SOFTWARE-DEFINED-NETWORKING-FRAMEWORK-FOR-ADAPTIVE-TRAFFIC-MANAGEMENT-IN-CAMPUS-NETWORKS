#!/usr/bin/env bash
set -euo pipefail

# Full-stack launcher:
# - controller
# - dashboard
# - optional ML stub
# - topology (interactive by default)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
ML_MODE="${CAMPUS_ML_MODE:-stub}"   # stub | dqn | none
TOPO_MODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-cli)
      TOPO_MODE="--no-cli"
      ;;
    --no-ml)
      ML_MODE="none"
      ;;
    --ml-mode)
      shift
      if [[ $# -eq 0 ]]; then
        echo "Missing value for --ml-mode (expected: stub|dqn|none)"
        exit 1
      fi
      ML_MODE="$1"
      ;;
    --ml-mode=*)
      ML_MODE="${1#*=}"
      ;;
    *)
      echo "Unknown argument: $1"
      echo "Usage: $0 [--no-cli] [--no-ml|--ml-mode stub|dqn|none]"
      exit 1
      ;;
  esac
  shift
done

case "${ML_MODE}" in
  stub|dqn|none) ;;
  *)
    echo "Invalid ML mode: ${ML_MODE} (expected: stub|dqn|none)"
    exit 1
    ;;
esac

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}."
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
  sudo mn -c >/tmp/mn_cleanup.log 2>&1 || true
}

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      kill "${pid}" 2>/dev/null || true
      wait "${pid}" 2>/dev/null || true
    fi
  done
}
trap cleanup EXIT INT TERM
PIDS=()

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

REQUIRED_CMDS=(python3 curl iperf3 ovs-vsctl)
for c in "${REQUIRED_CMDS[@]}"; do
  if ! command -v "${c}" >/dev/null 2>&1; then
    echo "Missing required command: ${c}"
    echo "Install prerequisites first (online):"
    echo "  examples/prepare_offline_bundle.sh"
    exit 1
  fi
done
if ! command -v ryu-manager >/dev/null 2>&1; then
  echo "ryu-manager not found in active virtualenv (${VENV_PATH})."
  echo "Install Ryu first while online."
  exit 1
fi

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
rm -f \
  "${CAMPUS_METRICS_FILE}" \
  "${CAMPUS_EVENTS_FILE}" \
  "${CAMPUS_ML_ACTION_FILE}" || true
if [[ -e "${CAMPUS_TOPOLOGY_STATE_FILE}" ]]; then
  rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" 2>/dev/null || sudo rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" || true
fi
echo "Topology state file: ${CAMPUS_TOPOLOGY_STATE_FILE}"
echo "Runtime API        : http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}"
echo "Ryu REST API       : http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"

echo "[1/5] Cleaning stale processes..."
cleanup_stale

echo "[2/5] Starting controller..."
ryu-manager \
  --wsapi-host "${CAMPUS_RYU_WSAPI_HOST}" \
  --wsapi-port "${CAMPUS_RYU_WSAPI_PORT}" \
  examples/campus_controller.py ryu.app.ofctl_rest >/tmp/ryu_campus.log 2>&1 &
PIDS+=("$!")
sleep 2

echo "[3/5] Starting dashboard at http://127.0.0.1:8080 ..."
python3 examples/campus_dashboard.py --host 127.0.0.1 --port 8080 \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --topology-state-file "${CAMPUS_TOPOLOGY_STATE_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/campus_dashboard.log 2>&1 &
PIDS+=("$!")

if [[ "${ML_MODE}" == "stub" ]]; then
  echo "[4/5] Starting ML policy stub..."
  python3 examples/ml_policy_stub.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/campus_ml_stub.log 2>&1 &
  PIDS+=("$!")
elif [[ "${ML_MODE}" == "dqn" ]]; then
  echo "[4/5] Starting DQN routing agent..."
  DQN_ARGS=()
  if [[ "${CAMPUS_DQN_NO_TRAIN:-0}" == "1" ]]; then
    DQN_ARGS+=(--no-train)
  fi
  python3 examples/dqn_routing_agent.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" \
    --model-file "${CAMPUS_DQN_MODEL_FILE}" \
    "${DQN_ARGS[@]}" >/tmp/campus_dqn.log 2>&1 &
  PIDS+=("$!")
else
  echo "[4/5] ML agent disabled."
fi

echo "[5/5] Starting topology..."
if [[ "${TOPO_MODE}" == "--no-cli" ]]; then
  sudo -E python3 examples/campus_topology.py --no-cli
else
  sudo -E python3 examples/campus_topology.py
fi
