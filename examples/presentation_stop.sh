#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  PRESENTATION STOP — Campus SDN NOC
#  Cleanly shuts down the traffic driver and full stack.
#
#  Usage:
#    bash examples/presentation_stop.sh
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

DRIVER_PID_FILE="/tmp/campus_traffic_driver.pid"
STACK_PID_FILE="/tmp/campus_web_stack.pids"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
SUDO_PASSWORD="${SUDO_PASSWORD:-patr6446179!}"

_G='\033[0;32m'; _C='\033[0;36m'; _Y='\033[0;33m'; _R='\033[0;31m'; _N='\033[0m'
ok()   { echo -e "${_G}[OK]${_N}   $*"; }
info() { echo -e "${_C}[INFO]${_N} $*"; }
warn() { echo -e "${_Y}[WARN]${_N} $*"; }

# Ensure sudo available
if ! sudo -n true >/dev/null 2>&1; then
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -v >/dev/null 2>&1
  fi
fi

# ── Stop traffic driver ────────────────────────────────────────────
if [[ -f "${DRIVER_PID_FILE}" ]]; then
  OLD_PID=$(cat "${DRIVER_PID_FILE}" 2>/dev/null || true)
  if [[ -n "${OLD_PID}" ]] && kill -0 "${OLD_PID}" 2>/dev/null; then
    kill "${OLD_PID}" 2>/dev/null || true
    ok "Traffic driver stopped (PID ${OLD_PID})"
  else
    warn "Traffic driver was not running"
  fi
  rm -f "${DRIVER_PID_FILE}"
else
  warn "No traffic driver PID file found"
fi

# ── Stop traffic stress via API (best-effort) ──────────────────────
DASHBOARD_PORT="${CAMPUS_DASHBOARD_PORT:-8080}"
curl -s -X POST "http://127.0.0.1:${DASHBOARD_PORT}/api/actions/stop-stress" >/dev/null 2>&1 || true

# ── Stop web stack processes ───────────────────────────────────────
if [[ -f "${STACK_PID_FILE}" ]]; then
  info "Stopping web stack processes..."
  source "${STACK_PID_FILE}" 2>/dev/null || true

  for VAR in TOPO_PID ML_PID DASH_PID RYU_PID; do
    PID="${!VAR:-}"
    if [[ -n "${PID}" ]] && kill -0 "${PID}" 2>/dev/null; then
      kill "${PID}" 2>/dev/null || true
      ok "Stopped ${VAR} (PID ${PID})"
    fi
  done
  rm -f "${STACK_PID_FILE}"
else
  warn "No stack PID file found — killing by process name"
fi

# ── Fallback: kill by name ─────────────────────────────────────────
sudo pkill -f "campus_topology.py"  >/dev/null 2>&1 || true
sudo pkill -f "campus_controller.py" >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager"          >/dev/null 2>&1 || true
pkill -f "campus_dashboard.py"       >/dev/null 2>&1 || true
pkill -f "ml_policy_stub.py"         >/dev/null 2>&1 || true
pkill -f "dqn_routing_agent.py"      >/dev/null 2>&1 || true
pkill -f "campus_traffic_driver"     >/dev/null 2>&1 || true

# ── Clean up Mininet state ─────────────────────────────────────────
info "Cleaning Mininet state..."
sudo mn -c >/tmp/mn_cleanup_stop.log 2>&1 || true
ok "Mininet cleaned"

echo ""
echo -e "${_G}════════════════════════════════════════════════════════${_N}"
echo -e "${_G}  Campus SDN stack fully stopped                       ${_N}"
echo -e "${_G}════════════════════════════════════════════════════════${_N}"
echo ""
