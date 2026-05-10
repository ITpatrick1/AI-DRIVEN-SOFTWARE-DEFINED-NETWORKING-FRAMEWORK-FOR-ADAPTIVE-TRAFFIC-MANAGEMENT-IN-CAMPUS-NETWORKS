#!/usr/bin/env bash
set -euo pipefail

# Full-stack launcher:
# - controller
# - dashboard
# - optional ML stub
# - topology (interactive by default)
#
# Usage: run_full_stack.sh [--no-cli] [--no-ml|--ml-mode stub|dqn|none]
#
# Environment overrides (all optional):
#   VENV_PATH                  Python virtualenv (default: ~/sdn-env)
#   CAMPUS_ML_MODE             stub | dqn | none (default: stub)
#   CAMPUS_DASHBOARD_HOST      Bind host for dashboard (default: 127.0.0.1)
#   CAMPUS_DASHBOARD_PORT      Dashboard port (default: 8080)
#   CAMPUS_RYU_WSAPI_HOST      Ryu REST API host (default: 127.0.0.1)
#   CAMPUS_RYU_WSAPI_PORT      Ryu REST API port (default: 8081)
#   CAMPUS_RUNTIME_API_PORT    Topology runtime API port (default: 9091)
#   CAMPUS_CONGEST_HIGH_MBPS   Congestion high threshold in Mbps (default: 40)
#   CAMPUS_CONGEST_LOW_MBPS    Congestion low threshold in Mbps (default: 20)

_GREEN='\033[0;32m'
_RED='\033[0;31m'
_CYAN='\033[0;36m'
_BOLD='\033[1m'
_NC='\033[0m'

ok()   { echo -e "${_GREEN}[OK]${_NC}  $*"; }
fail() { echo -e "${_RED}[FAIL]${_NC} $*" >&2; }
info() { echo -e "${_CYAN}[INFO]${_NC} $*"; }

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

if [[ -z "${CAMPUS_DQN_INTEGRATION_ENABLED:-}" ]]; then
  if [[ "${ML_MODE}" == "dqn" ]]; then
    export CAMPUS_DQN_INTEGRATION_ENABLED=1
  else
    export CAMPUS_DQN_INTEGRATION_ENABLED=0
  fi
fi

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}."
  exit 1
fi

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' -v >/dev/null
    return 0
  fi
  echo "[INFO] This launcher needs sudo privileges (Mininet/OVS)."
  sudo -v
}

sudo_run() {
  if sudo -n true >/dev/null 2>&1; then
    sudo "$@"
    return
  fi
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -p '' "$@"
    return
  fi
  sudo "$@"
}

cleanup_stale() {
  sudo_run pkill -f "campus_topology.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "campus_controller.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "ryu-manager" >/dev/null 2>&1 || true
  pkill -f "campus_dashboard.py" >/dev/null 2>&1 || true
  pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
  pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
  sudo_run mn -c >/tmp/mn_cleanup.log 2>&1 || true
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

detect_primary_ipv4() {
  local candidate
  candidate="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
  if [[ -n "${candidate}" ]]; then
    echo "${candidate}"
    return 0
  fi
  candidate="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${candidate}" ]]; then
    echo "${candidate}"
    return 0
  fi
  return 1
}

dashboard_display_url() {
  local bind_host="$1"
  local port="$2"
  local display_host="${bind_host}"
  if [[ "${bind_host}" == "0.0.0.0" ]]; then
    display_host="$(detect_primary_ipv4 || true)"
    if [[ -z "${display_host}" ]]; then
      display_host="<vm-ip>"
    fi
  fi
  echo "http://${display_host}:${port}"
}

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

REQUIRED_CMDS=(python3 curl iperf3 ovs-vsctl)
for c in "${REQUIRED_CMDS[@]}"; do
  if ! command -v "${c}" >/dev/null 2>&1; then
    fail "Missing required command: ${c}"
    info "Install prerequisites first (online): examples/prepare_offline_bundle.sh"
    exit 1
  fi
done
if ! command -v ryu-manager >/dev/null 2>&1; then
  fail "ryu-manager not found in active virtualenv (${VENV_PATH})."
  info "Install Ryu while online, then re-activate the virtualenv."
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
export CAMPUS_DASHBOARD_HOST="${CAMPUS_DASHBOARD_HOST:-127.0.0.1}"
export CAMPUS_DASHBOARD_PORT="${CAMPUS_DASHBOARD_PORT:-8080}"
mkdir -p "${HOME}/.cache"
export CAMPUS_TOPOLOGY_STATE_FILE="${CAMPUS_TOPOLOGY_STATE_FILE:-${HOME}/.cache/campus_topology_state.json}"
export CAMPUS_RUNTIME_API_HOST="${CAMPUS_RUNTIME_API_HOST:-127.0.0.1}"
export CAMPUS_RUNTIME_API_PORT="${CAMPUS_RUNTIME_API_PORT:-9091}"
DASHBOARD_URL="$(dashboard_display_url "${CAMPUS_DASHBOARD_HOST}" "${CAMPUS_DASHBOARD_PORT}")"
rm -f \
  "${CAMPUS_METRICS_FILE}" \
  "${CAMPUS_EVENTS_FILE}" \
  "${CAMPUS_ML_ACTION_FILE}" || true
if [[ -e "${CAMPUS_TOPOLOGY_STATE_FILE}" ]]; then
  rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" 2>/dev/null || sudo_run rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" || true
fi
info "Topology state : ${CAMPUS_TOPOLOGY_STATE_FILE}"
info "Runtime API    : http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}"
info "Ryu REST API   : http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
info "Dashboard      : ${DASHBOARD_URL}"
info "ML mode        : ${ML_MODE} (DQN integration=${CAMPUS_DQN_INTEGRATION_ENABLED})"
echo

echo "[1/5] Cleaning stale processes..."
cleanup_stale

echo "[2/5] Starting controller..."
ryu-manager \
  --wsapi-host "${CAMPUS_RYU_WSAPI_HOST}" \
  --wsapi-port "${CAMPUS_RYU_WSAPI_PORT}" \
  examples/campus_controller.py ryu.app.ofctl_rest >/tmp/ryu_campus.log 2>&1 &
PIDS+=("$!")
if wait_http "http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}/stats/switches" 20 1; then
  ok "Controller ready  → http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
else
  fail "Controller did not become ready within 20 s."
  info "See logs: /tmp/ryu_campus.log"
  exit 1
fi

echo "[3/5] Starting dashboard..."
python3 examples/campus_dashboard.py --host "${CAMPUS_DASHBOARD_HOST}" --port "${CAMPUS_DASHBOARD_PORT}" \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --topology-state-file "${CAMPUS_TOPOLOGY_STATE_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/campus_dashboard.log 2>&1 &
PIDS+=("$!")
if wait_http "http://127.0.0.1:${CAMPUS_DASHBOARD_PORT}/health" 20 1; then
  ok "Dashboard ready   → ${DASHBOARD_URL}"
else
  fail "Dashboard did not become ready within 20 s."
  info "See logs: /tmp/campus_dashboard.log"
  exit 1
fi

if [[ "${ML_MODE}" == "stub" ]]; then
  echo "[4/5] Starting ML policy stub..."
  python3 examples/ml_policy_stub.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/campus_ml_stub.log 2>&1 &
  PIDS+=("$!")
  ok "ML stub started   (log: /tmp/campus_ml_stub.log)"
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
  ok "DQN agent started (log: /tmp/campus_dqn.log)"
else
  echo "[4/5] ML agent disabled."
fi

echo
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD} Campus SDN stack ready — starting topology...  ${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
echo -e "  Dashboard   : ${_CYAN}${DASHBOARD_URL}${_NC}"
echo -e "  Ryu REST    : ${_CYAN}http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}${_NC}"
echo -e "  Runtime API : ${_CYAN}http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}${_NC}"
echo -e "  Logs        : /tmp/ryu_campus.log  /tmp/campus_dashboard.log"
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
echo

echo "[5/5] Starting topology..."
if [[ "${TOPO_MODE}" == "--no-cli" ]]; then
  sudo_run -E python3 examples/campus_topology.py --no-cli
else
  sudo_run -E python3 examples/campus_topology.py
fi
