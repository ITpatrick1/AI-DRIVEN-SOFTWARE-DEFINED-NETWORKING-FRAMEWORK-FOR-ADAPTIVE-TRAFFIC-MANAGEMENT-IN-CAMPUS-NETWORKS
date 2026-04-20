#!/usr/bin/env bash
set -euo pipefail

PID_FILE="/tmp/campus_web_stack.pids"

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -v >/dev/null
    return 0
  fi
  echo "[INFO] This stop script needs sudo privileges (Mininet/OVS)."
  sudo -v
}

kill_pid() {
  local pid="$1"
  if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    wait "${pid}" 2>/dev/null || true
  fi
}

ensure_sudo

if [[ -f "${PID_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${PID_FILE}" || true
  kill_pid "${RYU_PID:-}"
  kill_pid "${DASH_PID:-}"
  kill_pid "${ML_PID:-}"
  kill_pid "${TOPO_PID:-}"
  rm -f "${PID_FILE}" || true
fi

sudo pkill -f "campus_topology.py" >/dev/null 2>&1 || true
sudo pkill -f "campus_controller.py" >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager" >/dev/null 2>&1 || true
pkill -f "campus_dashboard.py" >/dev/null 2>&1 || true
pkill -f "examples/ml_policy_stub.py" >/dev/null 2>&1 || true
pkill -f "examples/dqn_routing_agent.py" >/dev/null 2>&1 || true
sudo mn -c >/tmp/mn_cleanup_web_stop.log 2>&1 || true

echo "[PASS] Web-only stack stopped."
