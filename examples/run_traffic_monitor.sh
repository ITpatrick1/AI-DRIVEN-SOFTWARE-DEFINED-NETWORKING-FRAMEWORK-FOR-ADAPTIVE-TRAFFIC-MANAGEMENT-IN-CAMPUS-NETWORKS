#!/usr/bin/env bash
set -euo pipefail

# Run Stage 7 monitoring module (separate component).
# Default expects Ryu ofctl REST running on 127.0.0.1:8081.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
RYU_BASE="${CAMPUS_RYU_BASE:-http://127.0.0.1:8081}"
MON_HOST="${CAMPUS_MONITOR_HOST:-127.0.0.1}"
MON_PORT="${CAMPUS_MONITOR_PORT:-8090}"
WARN_UTIL="${CAMPUS_MONITOR_WARN_UTIL_PCT:-80}"
POLL_INTERVAL="${CAMPUS_MONITOR_POLL_INTERVAL:-2}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"

echo "Starting traffic monitor..."
echo "  Monitor URL: http://${MON_HOST}:${MON_PORT}"
echo "  Ryu REST   : ${RYU_BASE}"

python3 examples/traffic_monitor.py \
  --host "${MON_HOST}" \
  --port "${MON_PORT}" \
  --ryu-base "${RYU_BASE}" \
  --warn-util-pct "${WARN_UTIL}" \
  --poll-interval "${POLL_INTERVAL}"
