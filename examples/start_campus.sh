#!/usr/bin/env bash
# Campus SDN – unified start / stop script.
#
# This is the merged launcher for the newer Campus SDN stack plus the Tumba
# project runtime pieces:
# - stakeholder-driven controller/DQN/security policy generation
# - timetable engine
# - web-only dashboard/controller/topology stack
#
# Usage:
#   sudo ./examples/start_campus.sh
#   sudo ./examples/start_campus.sh stop
#   sudo ./examples/start_campus.sh status
#
# Options (env vars):
#   CAMPUS_DASHBOARD_PORT          web UI port (default 8080)
#   CAMPUS_ML_MODE                 stub | dqn | none (default stub)
#   CAMPUS_WEB_HOLD_SECONDS        topology hold time (default 86400)
#   CAMPUS_USE_STAKEHOLDER_PROFILE 1|0 auto-generate policy from survey CSV
#   CAMPUS_SURVEY_CSV              path to stakeholder survey CSV
#   CAMPUS_ENABLE_TIMETABLE        1|0 start timetable engine
#   CAMPUS_TIMETABLE_PORT          timetable API port (default 9092)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
PID_FILE="/tmp/sdn_patrick/campus_web_stack.pids"
TIMETABLE_LOG="/tmp/sdn_patrick/campus_timetable.log"
PROFILE_LOG="/tmp/sdn_patrick/campus_stakeholder_profile.log"
STACK_STOPPED=0

_GREEN='\033[0;32m'
_RED='\033[0;31m'
_CYAN='\033[0;36m'
_YELLOW='\033[1;33m'
_BOLD='\033[1m'
_NC='\033[0m'

ok()    { echo -e "${_GREEN}[OK]${_NC}  $*"; }
fail()  { echo -e "${_RED}[FAIL]${_NC} $*" >&2; }
info()  { echo -e "${_CYAN}[INFO]${_NC} $*"; }
warn()  { echo -e "${_YELLOW}[WARN]${_NC} $*"; }

# ── helpers ─────────────────────────────────────────────────────────────────

detect_lan_ip() {
  local ip
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null \
        | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1);exit}}')"
  [[ -n "$ip" ]] && { echo "$ip"; return; }
  hostname -I 2>/dev/null | awk '{print $1}'
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

bool_enabled() {
  case "${1:-1}" in
    0|false|FALSE|False|no|NO|off|OFF) return 1 ;;
    *) return 0 ;;
  esac
}

prepare_stakeholder_profile() {
  local survey_csv="$1"

  export CAMPUS_STAKEHOLDER_REPORT_FILE="${CAMPUS_STAKEHOLDER_REPORT_FILE:-/tmp/sdn_patrick/campus_stakeholder_report.json}"
  export CAMPUS_MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/sdn_patrick/campus_manual_settings.json}"
  export CAMPUS_SECURITY_POLICY_FILE="${CAMPUS_SECURITY_POLICY_FILE:-/tmp/sdn_patrick/campus_security_policy.json}"
  export CAMPUS_DQN_POLICY_FILE="${CAMPUS_DQN_POLICY_FILE:-/tmp/sdn_patrick/campus_dqn_policy.json}"

  if ! bool_enabled "${CAMPUS_USE_STAKEHOLDER_PROFILE:-1}"; then
    info "Stakeholder-driven profile disabled by CAMPUS_USE_STAKEHOLDER_PROFILE."
    rm -f \
      "${CAMPUS_STAKEHOLDER_REPORT_FILE}" \
      "${CAMPUS_MANUAL_SETTINGS_FILE}" \
      "${CAMPUS_SECURITY_POLICY_FILE}" \
      "${CAMPUS_DQN_POLICY_FILE}" || true
    return 0
  fi

  if [[ ! -f "${survey_csv}" ]]; then
    warn "Survey CSV not found at ${survey_csv}; continuing with built-in defaults."
    rm -f \
      "${CAMPUS_STAKEHOLDER_REPORT_FILE}" \
      "${CAMPUS_MANUAL_SETTINGS_FILE}" \
      "${CAMPUS_SECURITY_POLICY_FILE}" \
      "${CAMPUS_DQN_POLICY_FILE}" || true
    return 0
  fi

  info "Generating stakeholder-driven policy artifacts from survey CSV..."
  if python3 "${SCRIPT_DIR}/stakeholder_requirements.py" \
      --csv "${survey_csv}" \
      --report-json "${CAMPUS_STAKEHOLDER_REPORT_FILE}" \
      --report-md "${REPO_ROOT}/results/stakeholder_analysis_latest.md" \
      --manual-settings-file "${CAMPUS_MANUAL_SETTINGS_FILE}" \
      --security-policy-file "${CAMPUS_SECURITY_POLICY_FILE}" \
      --dqn-policy-file "${CAMPUS_DQN_POLICY_FILE}" \
      --output-dir "${REPO_ROOT}/results" \
      >"${PROFILE_LOG}" 2>&1; then
    ok "Stakeholder profile ready."
  else
    fail "Failed to generate stakeholder policy profile."
    info "See log: ${PROFILE_LOG}"
    return 1
  fi
}

start_timetable() {
  if ! bool_enabled "${CAMPUS_ENABLE_TIMETABLE:-1}"; then
    info "Timetable engine disabled by CAMPUS_ENABLE_TIMETABLE."
    return 0
  fi

  export CAMPUS_TIMETABLE_PORT="${CAMPUS_TIMETABLE_PORT:-9092}"
  export CAMPUS_TIMETABLE_DB="${CAMPUS_TIMETABLE_DB:-/tmp/sdn_patrick/campus_timetable.db}"
  export CAMPUS_TIMETABLE_STATE="${CAMPUS_TIMETABLE_STATE:-/tmp/sdn_patrick/campus_timetable_state.json}"
  export CAMPUS_TIMETABLE_OVERRIDE="${CAMPUS_TIMETABLE_OVERRIDE:-/tmp/sdn_patrick/campus_timetable_override.json}"

  pkill -f "examples/timetable_engine.py" >/dev/null 2>&1 || true

  info "Starting timetable engine..."
  nohup python3 "${SCRIPT_DIR}/timetable_engine.py" \
    --db "${CAMPUS_TIMETABLE_DB}" \
    --state "${CAMPUS_TIMETABLE_STATE}" \
    --override "${CAMPUS_TIMETABLE_OVERRIDE}" \
    --port "${CAMPUS_TIMETABLE_PORT}" \
    >"${TIMETABLE_LOG}" 2>&1 < /dev/null &
  TT_PID=$!

  if wait_http "http://127.0.0.1:${CAMPUS_TIMETABLE_PORT}/health" 20 1; then
    ok "Timetable API ready → http://127.0.0.1:${CAMPUS_TIMETABLE_PORT}"
    return 0
  fi

  fail "Timetable API did not become ready."
  info "See log: ${TIMETABLE_LOG}"
  return 1
}

stop_stack() {
  if [[ "${STACK_STOPPED}" == "1" ]]; then
    return 0
  fi
  STACK_STOPPED=1
  echo
  info "Stopping Campus SDN stack..."
  bash "${SCRIPT_DIR}/stop_web_only_stack.sh" 2>/dev/null || true
  ok "Stack stopped."
}

status_stack() {
  if [[ ! -f "$PID_FILE" ]]; then
    warn "No PID file found – stack is probably not running."
    exit 0
  fi

  # shellcheck disable=SC1090
  source "$PID_FILE" 2>/dev/null || true
  local running=0
  local varname pid

  for varname in RYU_PID DASH_PID ML_PID TOPO_PID TT_PID; do
    pid="${!varname:-}"
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      ok "${varname}=${pid} running"
      ((running++)) || true
    else
      warn "${varname}=${pid:-?} NOT running"
    fi
  done

  if [[ $running -gt 0 ]]; then
    local dash_port="${CAMPUS_DASHBOARD_PORT:-8080}"
    local tt_port="${CAMPUS_TIMETABLE_PORT:-9092}"
    local ip
    ip="$(detect_lan_ip)"
    info "Dashboard → http://${ip}:${dash_port}"
    info "Timetable → http://127.0.0.1:${tt_port}"
  fi
}

# ── argument parsing ─────────────────────────────────────────────────────────

CMD="${1:-start}"
if [[ $# -gt 0 ]]; then
  shift
fi

case "$CMD" in
  stop)    stop_stack; exit 0 ;;
  status)  status_stack; exit 0 ;;
  start)   ;;
  *)
    echo "Usage: $0 [start|stop|status]"
    exit 1
    ;;
esac

# ── start ────────────────────────────────────────────────────────────────────

export CAMPUS_DASHBOARD_HOST="0.0.0.0"
export CAMPUS_DASHBOARD_PORT="${CAMPUS_DASHBOARD_PORT:-8080}"
export CAMPUS_ML_MODE="${CAMPUS_ML_MODE:-stub}"
export CAMPUS_WEB_HOLD_SECONDS="${CAMPUS_WEB_HOLD_SECONDS:-86400}"
export CAMPUS_SURVEY_CSV="${CAMPUS_SURVEY_CSV:-${REPO_ROOT}/Stakeholder Requirement Survey Intelligent SDN Project .csv}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"

LAN_IP="$(detect_lan_ip)"
DASHBOARD_URL="http://${LAN_IP}:${CAMPUS_DASHBOARD_PORT}"

echo
echo -e "${_BOLD}╔══════════════════════════════════════════════════════════╗${_NC}"
echo -e "${_BOLD}║      Campus SDN + Tumba SDN – Unified Runtime Stack     ║${_NC}"
echo -e "${_BOLD}╚══════════════════════════════════════════════════════════╝${_NC}"
echo
info "Dashboard URL   → ${_CYAN}${DASHBOARD_URL}${_NC}"
info "Survey profile  → ${CAMPUS_SURVEY_CSV}"
info "ML mode         : ${CAMPUS_ML_MODE}"
info "Hold time       : ${CAMPUS_WEB_HOLD_SECONDS}s"
info "Topology smoke  : ${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS} (1 means skip legacy pingall gate)"
echo
info "Preparing merged runtime (Ctrl-C stops everything cleanly)..."
echo
mkdir -p /tmp/sdn_patrick
trap 'stop_stack' INT TERM EXIT

prepare_stakeholder_profile "${CAMPUS_SURVEY_CSV}"
start_timetable

bash "${SCRIPT_DIR}/run_web_only_stack.sh" "$@"

if [[ -n "${TT_PID:-}" ]] && [[ -f "${PID_FILE}" ]]; then
  if ! grep -q '^TT_PID=' "${PID_FILE}" 2>/dev/null; then
    echo "TT_PID=${TT_PID}" >> "${PID_FILE}"
  fi
fi

echo
echo -e "${_BOLD}════════════════════════════════════════════════════════════${_NC}"
echo -e "${_BOLD} Unified stack is running. Open your browser:${_NC}"
echo -e "   ${_CYAN}${DASHBOARD_URL}${_NC}"
echo
echo -e " Features active:"
echo -e "   • Real-time topology map with animated traffic flow"
echo -e "   • Stakeholder-driven threshold, security, and DQN policy files"
echo -e "   • Timetable-aware controller state and exam-mode overrides"
echo -e "   • Adaptive rerouting, QoS policy enforcement, and DDoS simulation"
echo -e "   • Device inventory, flow inspection, and runtime operations"
echo
echo -e " Press ${_BOLD}Ctrl-C${_NC} to stop everything."
echo -e "${_BOLD}════════════════════════════════════════════════════════════${_NC}"
echo

while true; do
  sleep 60

  if [[ -f "$PID_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$PID_FILE" 2>/dev/null || true
    if [[ -n "${TOPO_PID:-}" ]] && ! kill -0 "${TOPO_PID}" 2>/dev/null; then
      warn "Topology process exited unexpectedly. Run 'stop' then 'start' to restart."
    fi
    if [[ -n "${TT_PID:-}" ]] && ! kill -0 "${TT_PID}" 2>/dev/null; then
      warn "Timetable process exited unexpectedly. Run 'stop' then 'start' to restart."
    fi
  fi
done
