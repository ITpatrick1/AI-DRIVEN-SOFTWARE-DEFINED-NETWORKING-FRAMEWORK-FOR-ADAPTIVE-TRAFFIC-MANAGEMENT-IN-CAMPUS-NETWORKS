#!/usr/bin/env bash
set -euo pipefail

# One-command launcher for the campus SDN demo.
# - Starts Ryu controller from ~/sdn-env
# - Cleans stale Mininet state
# - Runs campus topology (interactive by default, or --no-cli)

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RYU_APP="${REPO_ROOT}/examples/campus_controller.py"
TOPO_APP="${REPO_ROOT}/examples/campus_topology.py"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
NO_CLI="${1:-}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}. Set VENV_PATH or create ~/sdn-env."
  exit 1
fi

cleanup() {
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"

echo "[1/3] Cleaning Mininet state..."
sudo mn -c >/tmp/mn_cleanup.log 2>&1 || true

echo "[2/3] Starting Ryu controller..."
ryu-manager "${RYU_APP}" >/tmp/ryu_campus.log 2>&1 &
RPID=$!
sleep 2
if ! kill -0 "${RPID}" 2>/dev/null; then
  echo "Ryu failed to start. Last logs:"
  tail -n 120 /tmp/ryu_campus.log || true
  exit 1
fi
if ! ss -ltn | grep -q ':6653'; then
  echo "Ryu process started but port 6653 is not listening. Logs:"
  tail -n 120 /tmp/ryu_campus.log || true
  exit 1
fi

echo "[3/3] Starting campus topology..."
if [[ "${NO_CLI}" == "--no-cli" ]]; then
  sudo python3 "${TOPO_APP}" --no-cli
else
  sudo python3 "${TOPO_APP}"
fi
