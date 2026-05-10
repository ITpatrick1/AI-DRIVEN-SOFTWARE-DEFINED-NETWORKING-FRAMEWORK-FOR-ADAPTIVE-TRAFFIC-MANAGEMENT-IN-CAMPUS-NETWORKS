#!/usr/bin/env bash
set -euo pipefail

# Web-only launcher:
# - starts controller/dashboard/ML/topology in background
# - keeps topology alive for long hold duration
# - user can operate entirely from browser + dashboard APIs
#
# Usage: run_web_only_stack.sh [--no-ml|--ml-mode stub|dqn|none]
#
# Environment overrides (all optional):
#   VENV_PATH                  Python virtualenv (default: ~/sdn-env)
#   CAMPUS_ML_MODE             stub | dqn | none (default: stub)
#   CAMPUS_DASHBOARD_HOST      Bind host (default: 127.0.0.1; use 0.0.0.0 for LAN access)
#   CAMPUS_DASHBOARD_PORT      Dashboard port (default: 8080)
#   CAMPUS_RYU_WSAPI_PORT      Ryu REST API port (default: 8081)
#   CAMPUS_WEB_HOLD_SECONDS    How long topology stays alive (default: 86400)

_GREEN='\033[0;32m'
_RED='\033[0;31m'
_CYAN='\033[0;36m'
_BOLD='\033[1m'
_NC='\033[0m'

ok()   { echo -e "${_GREEN}[OK]${_NC}  $*"; }
fail() { echo -e "${_RED}[FAIL]${_NC} $*" >&2; }
info() { echo -e "${_CYAN}[INFO]${_NC} $*"; }
ts()   { date '+%H:%M:%S'; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# When run via `sudo bash`, HOME becomes /root. Detect the real user's home.
REAL_USER="${SUDO_USER:-${USER}}"
REAL_HOME="$(getent passwd "${REAL_USER}" | cut -d: -f6)"
REAL_HOME="${REAL_HOME:-${HOME}}"

VENV_PATH="${VENV_PATH:-${REAL_HOME}/sdn-env}"
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

if [[ -z "${CAMPUS_DQN_INTEGRATION_ENABLED:-}" ]]; then
  if [[ "${ML_MODE}" == "dqn" ]]; then
    export CAMPUS_DQN_INTEGRATION_ENABLED=1
  else
    export CAMPUS_DQN_INTEGRATION_ENABLED=0
  fi
fi

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
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

sudo_bg() {
  local log_file="$1"
  shift
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    nohup bash -lc 'printf "%s\n" "$SUDO_PASSWORD" | sudo -S -p "" "$@"' bash "$@" \
      >"${log_file}" 2>&1 < /dev/null &
  else
    nohup sudo "$@" >"${log_file}" 2>&1 < /dev/null &
  fi
}

cleanup_stale() {
  sudo_run pkill -f "campus_topology.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "campus_controller.py" >/dev/null 2>&1 || true
  sudo_run pkill -f "ryu-manager" >/dev/null 2>&1 || true
  pkill -f "campus_dashboard.py" >/dev/null 2>&1 || true
  pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
  pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
  sudo_run mn -c >/tmp/mn_cleanup_web.log 2>&1 || true
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

find_runtime_api() {
  local host="$1"
  local start_port="$2"
  local span="${3:-20}"
  local p
  for p in $(seq "${start_port}" "$((start_port + span - 1))"); do
    if runtime_api_ready "http://${host}:${p}"; then
      echo "http://${host}:${p}"
      return 0
    fi
  done
  return 1
}

runtime_api_ready() {
  local base="$1"
  local body
  if ! body="$(curl -fsS "${base}/health" 2>/dev/null)"; then
    return 1
  fi
  grep -q '"hosts"' <<<"${body}"
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
export CAMPUS_ML_ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/sdn_patrick/campus_ml_action.json}"
export CAMPUS_DQN_MODEL_FILE="${CAMPUS_DQN_MODEL_FILE:-/tmp/sdn_patrick/campus_dqn_model.pt}"
export CAMPUS_RYU_WSAPI_HOST="${CAMPUS_RYU_WSAPI_HOST:-127.0.0.1}"
export CAMPUS_RYU_WSAPI_PORT="${CAMPUS_RYU_WSAPI_PORT:-8081}"
export CAMPUS_DASHBOARD_HOST="${CAMPUS_DASHBOARD_HOST:-0.0.0.0}"
export CAMPUS_DASHBOARD_PORT="${CAMPUS_DASHBOARD_PORT:-8080}"
mkdir -p "${REAL_HOME}/.cache"
mkdir -p /tmp/sdn_patrick
export CAMPUS_TOPOLOGY_STATE_FILE="${CAMPUS_TOPOLOGY_STATE_FILE:-${REAL_HOME}/.cache/campus_topology_state.json}"
export CAMPUS_RUNTIME_API_HOST="${CAMPUS_RUNTIME_API_HOST:-127.0.0.1}"
export CAMPUS_RUNTIME_API_PORT="${CAMPUS_RUNTIME_API_PORT:-9091}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"
HOLD_SECONDS="${CAMPUS_WEB_HOLD_SECONDS:-86400}"
PID_FILE="/tmp/sdn_patrick/campus_web_stack.pids"
TOPO_LOG="/tmp/sdn_patrick/campus_topology_web.log"
DASHBOARD_URL="$(dashboard_display_url "${CAMPUS_DASHBOARD_HOST}" "${CAMPUS_DASHBOARD_PORT}")"

info "Dashboard      : ${DASHBOARD_URL}"
info "Ryu REST API   : http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
info "ML mode        : ${ML_MODE} (DQN integration=${CAMPUS_DQN_INTEGRATION_ENABLED})"
info "Hold duration  : ${HOLD_SECONDS}s"
echo

echo "[$(ts)] [1/5] Cleaning stale processes..."
cleanup_stale
rm -f "${CAMPUS_METRICS_FILE}" "${CAMPUS_EVENTS_FILE}" "${CAMPUS_ML_ACTION_FILE}" "${PID_FILE}" "${TOPO_LOG}" || true
if [[ -e "${CAMPUS_TOPOLOGY_STATE_FILE}" ]]; then
  rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" 2>/dev/null || sudo_run rm -f "${CAMPUS_TOPOLOGY_STATE_FILE}" || true
fi

echo "[$(ts)] [2/5] Starting controller..."
nohup ryu-manager \
  --wsapi-host "${CAMPUS_RYU_WSAPI_HOST}" \
  --wsapi-port "${CAMPUS_RYU_WSAPI_PORT}" \
  examples/campus_controller.py ryu.app.ofctl_rest \
  >/tmp/sdn_patrick/ryu_campus.log 2>&1 < /dev/null &
RYU_PID=$!

echo "[$(ts)] [3/5] Starting dashboard..."
nohup "${VENV_PATH}/bin/python3" examples/campus_dashboard.py --host "${CAMPUS_DASHBOARD_HOST}" --port "${CAMPUS_DASHBOARD_PORT}" \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --topology-state-file "${CAMPUS_TOPOLOGY_STATE_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/sdn_patrick/campus_dashboard.log 2>&1 < /dev/null &
DASH_PID=$!

ML_PID=""
if [[ "${ML_MODE}" == "stub" ]]; then
  echo "[$(ts)] [4/5] Starting ML policy stub..."
  nohup "${VENV_PATH}/bin/python3" examples/ml_policy_stub.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/sdn_patrick/campus_ml_stub.log 2>&1 < /dev/null &
  ML_PID=$!
elif [[ "${ML_MODE}" == "dqn" ]]; then
  echo "[$(ts)] [4/5] Starting DQN routing agent..."
  DQN_ARGS=()
  if [[ "${CAMPUS_DQN_NO_TRAIN:-0}" == "1" ]]; then
    DQN_ARGS+=(--no-train)
  fi
  nohup "${VENV_PATH}/bin/python3" examples/dqn_routing_agent.py \
    --metrics-file "${CAMPUS_METRICS_FILE}" \
    --action-file "${CAMPUS_ML_ACTION_FILE}" \
    --model-file "${CAMPUS_DQN_MODEL_FILE}" \
    "${DQN_ARGS[@]}" >/tmp/sdn_patrick/campus_dqn.log 2>&1 < /dev/null &
  ML_PID=$!
else
  echo "[$(ts)] [4/5] ML agent disabled."
fi

echo "[$(ts)] [5/5] Starting topology (web-only hold ${HOLD_SECONDS}s)..."
: >"${TOPO_LOG}"
sudo_bg "${TOPO_LOG}" -E python3 examples/campus_topology.py --no-cli --hold-seconds "${HOLD_SECONDS}"
TOPO_PID=$!

# Start performance evaluator (port 9093) and simulation runner (port 9094)
PERF_EVAL_PORT="${CAMPUS_PERF_EVAL_PORT:-9093}"
SIM_RUNNER_PORT="${CAMPUS_SIM_RUNNER_PORT:-9094}"
pkill -f "examples/performance_evaluator.py" >/dev/null 2>&1 || true
pkill -f "examples/simulation_runner.py" >/dev/null 2>&1 || true
nohup "${VENV_PATH}/bin/python3" examples/performance_evaluator.py --port "${PERF_EVAL_PORT}" \
  >/tmp/sdn_patrick/campus_perf_eval.log 2>&1 < /dev/null &
PERF_EVAL_PID=$!
nohup "${VENV_PATH}/bin/python3" examples/simulation_runner.py --port "${SIM_RUNNER_PORT}" \
  >/tmp/sdn_patrick/campus_sim_runner.log 2>&1 < /dev/null &
SIM_RUNNER_PID=$!

{
  echo "RYU_PID=${RYU_PID}"
  echo "DASH_PID=${DASH_PID}"
  [[ -n "${ML_PID}" ]] && echo "ML_PID=${ML_PID}"
  echo "TOPO_PID=${TOPO_PID}"
  echo "PERF_EVAL_PID=${PERF_EVAL_PID}"
  echo "SIM_RUNNER_PID=${SIM_RUNNER_PID}"
} >"${PID_FILE}"

if ! wait_http "http://127.0.0.1:${CAMPUS_DASHBOARD_PORT}/api/metrics" 30 1; then
  echo "[FAIL] Dashboard did not come up on ${DASHBOARD_URL}"
  echo "See logs: /tmp/campus_dashboard.log, ${TOPO_LOG}, /tmp/ryu_campus.log"
  exit 1
fi

if ! wait_http "http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}/stats/switches" 30 1; then
  echo "[FAIL] Ryu REST API did not become ready on http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}"
  echo "See logs: /tmp/ryu_campus.log"
  exit 1
fi

RUNTIME_BASE=""
for _ in $(seq 1 90); do
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
  if [[ -n "${RUNTIME_BASE}" ]] && runtime_api_ready "${RUNTIME_BASE}"; then
    break
  fi
  RUNTIME_BASE="$(find_runtime_api "${CAMPUS_RUNTIME_API_HOST}" "${CAMPUS_RUNTIME_API_PORT}" 20 || true)"
  if [[ -n "${RUNTIME_BASE}" ]]; then
    break
  fi
  sleep 1
done

if [[ -z "${RUNTIME_BASE}" ]]; then
  echo "[WARN] Topology Runtime API did not come up — starting stub fallback on port ${CAMPUS_RUNTIME_API_PORT}..."
  pkill -f "examples/runtime_api_stub.py" >/dev/null 2>&1 || true
  nohup "${VENV_PATH}/bin/python3" examples/runtime_api_stub.py \
    --host "${CAMPUS_RUNTIME_API_HOST}" --port "${CAMPUS_RUNTIME_API_PORT}" \
    >/tmp/sdn_patrick/campus_runtime_stub.log 2>&1 < /dev/null &
  STUB_PID=$!
  echo "STUB_PID=${STUB_PID}" >>"${PID_FILE}"
  sleep 3
  RUNTIME_BASE="http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}"
  if ! runtime_api_ready "${RUNTIME_BASE}"; then
    echo "[FAIL] Runtime API stub also failed. See /tmp/sdn_patrick/campus_runtime_stub.log"
    exit 1
  fi
  echo "[OK] Runtime API stub is running (simulation mode — no Mininet hosts)"
fi

echo
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD} Campus SDN web-only stack is running           ${_NC}"
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
ok "Dashboard   → ${_CYAN}${DASHBOARD_URL}${_NC}"
ok "Ryu REST    → ${_CYAN}http://${CAMPUS_RYU_WSAPI_HOST}:${CAMPUS_RYU_WSAPI_PORT}${_NC}"
ok "Runtime API → ${_CYAN}${RUNTIME_BASE}${_NC}"
ok "Perf Eval   → ${_CYAN}http://127.0.0.1:${PERF_EVAL_PORT}${_NC}"
ok "Sim Runner  → ${_CYAN}http://127.0.0.1:${SIM_RUNNER_PORT}${_NC}"
echo -e "  PID file  : ${PID_FILE}"
echo -e "  Logs      : /tmp/ryu_campus.log  /tmp/campus_dashboard.log  ${TOPO_LOG}"
echo -e "             /tmp/sdn_patrick/campus_perf_eval.log  /tmp/sdn_patrick/campus_sim_runner.log"
echo -e "  Hold      : ${HOLD_SECONDS}s from $(date '+%H:%M:%S')"
echo -e "${_BOLD}════════════════════════════════════════════════${_NC}"
echo
info "To stop everything: examples/stop_web_only_stack.sh"
