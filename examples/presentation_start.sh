#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════
#  PRESENTATION START — Campus SDN NOC
#  Launches the full stack + auto-cycling traffic demo for the panel.
#
#  Usage:
#    bash examples/presentation_start.sh
#    bash examples/presentation_start.sh --ml-mode stub
#
#  What it does:
#    1. Starts Ryu controller, dashboard, selected ML mode, Mininet topology
#    2. Waits for everything to be ready
#    3. Starts the auto-traffic driver in the background
#    4. Prints the browser URL to share with the panel
# ═══════════════════════════════════════════════════════════════════
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
SUDO_PASSWORD="${SUDO_PASSWORD:-patr6446179!}"
DASHBOARD_PORT="${CAMPUS_DASHBOARD_PORT:-8080}"
ML_MODE="${CAMPUS_ML_MODE:-dqn}"
DRIVER_LOG="/tmp/campus_traffic_driver.log"
DRIVER_PID_FILE="/tmp/campus_traffic_driver.pid"

_G='\033[0;32m'; _C='\033[0;36m'; _Y='\033[0;33m'; _B='\033[1m'; _R='\033[0;31m'; _N='\033[0m'
ok()   { echo -e "${_G}[OK]${_N}   $*"; }
info() { echo -e "${_C}[INFO]${_N} $*"; }
warn() { echo -e "${_Y}[WARN]${_N} $*"; }
fail() { echo -e "${_R}[FAIL]${_N} $*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ml-mode)
      shift
      if [[ $# -eq 0 ]]; then
        fail "Missing value for --ml-mode (expected: dqn|stub|none)"
        exit 1
      fi
      ML_MODE="$1"
      ;;
    --ml-mode=*)
      ML_MODE="${1#*=}"
      ;;
    *)
      fail "Unknown argument: $1"
      echo "Usage: bash examples/presentation_start.sh [--ml-mode dqn|stub|none]"
      exit 1
      ;;
  esac
  shift
done

case "${ML_MODE}" in
  dqn|stub|none) ;;
  *)
    fail "Invalid ML mode: ${ML_MODE} (expected: dqn|stub|none)"
    exit 1
    ;;
esac

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"

if [[ "${ML_MODE}" == "dqn" ]]; then
  if ! python3 - <<'PY' >/dev/null 2>&1; then
import torch  # noqa: F401
PY
    fail "PyTorch is not installed in ${VENV_PATH}, so DQN mode cannot start."
    echo "Install while online: examples/prepare_offline_bundle.sh"
    exit 1
  fi
fi

# ── Stop any stale driver ──────────────────────────────────────────
if [[ -f "${DRIVER_PID_FILE}" ]]; then
  OLD_PID=$(cat "${DRIVER_PID_FILE}" 2>/dev/null || true)
  kill "${OLD_PID}" 2>/dev/null || true
  rm -f "${DRIVER_PID_FILE}"
fi

# ── Start the full web stack ───────────────────────────────────────
echo ""
info "Starting Campus SDN full stack in ${ML_MODE} mode..."
CAMPUS_DASHBOARD_HOST=0.0.0.0 SUDO_PASSWORD="${SUDO_PASSWORD}" \
  bash examples/run_web_only_stack.sh --ml-mode "${ML_MODE}"

# ── Wait for dashboard to be fully ready ──────────────────────────
for i in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${DASHBOARD_PORT}/api/metrics" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

# ── Run initial pingall ───────────────────────────────────────────
info "Running initial reachability check..."
curl -s -X POST "http://127.0.0.1:${DASHBOARD_PORT}/api/actions/pingall" >/dev/null 2>&1 || true
ok "Reachability check done"

# ── Start auto-traffic driver ─────────────────────────────────────
info "Starting continuous traffic driver..."
cat > /tmp/campus_traffic_driver.sh << 'DRIVER_EOF'
#!/usr/bin/env bash
PORT="${1:-8080}"
API="http://127.0.0.1:${PORT}"
_G='\033[0;32m'; _C='\033[0;36m'; _N='\033[0m'
log() { echo -e "[$(date '+%H:%M:%S')] $*"; }

SCENARIOS=(
  '{"seconds":50,"reverse_download":true,"clients":["h_wifi1","h_wifi2"]}'
  '{"seconds":40,"reverse_download":true,"clients":["h_wifi1","h_wifi2","h_it1","h_net1"]}'
  '{"seconds":45,"reverse_download":true,"clients":["h_wifi1"]}'
  '{"seconds":55,"reverse_download":true,"clients":["h_wifi1","h_wifi2","h_it1","h_it2","h_staff1"]}'
)
IDX=0

while true; do
  SCENARIO="${SCENARIOS[$((IDX % ${#SCENARIOS[@]}))]}"
  log "▶ Starting scenario $((IDX % ${#SCENARIOS[@]} + 1))/${#SCENARIOS[@]}: ${SCENARIO}"
  curl -s -X POST "${API}/api/actions/start-stress" \
    -H "Content-Type: application/json" \
    -d "${SCENARIO}" >/dev/null 2>&1

  DURATION=$(echo "${SCENARIO}" | grep -oP '"seconds":\s*\K[0-9]+' || echo 45)
  log "   Running for ${DURATION}s..."
  sleep "${DURATION}"

  log "■ Stopping traffic..."
  curl -s -X POST "${API}/api/actions/stop-stress" >/dev/null 2>&1
  sleep 3

  log "⚡ Running pingall..."
  curl -s -X POST "${API}/api/actions/pingall" >/dev/null 2>&1
  sleep 5

  IDX=$((IDX + 1))
done
DRIVER_EOF

chmod +x /tmp/campus_traffic_driver.sh
nohup bash /tmp/campus_traffic_driver.sh "${DASHBOARD_PORT}" \
  >"${DRIVER_LOG}" 2>&1 &
echo $! > "${DRIVER_PID_FILE}"
ok "Traffic driver started (PID $(cat ${DRIVER_PID_FILE})) → log: ${DRIVER_LOG}"

# ── Print the panel URL ────────────────────────────────────────────
VM_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
echo ""
echo -e "${_B}════════════════════════════════════════════════════════${_N}"
echo -e "${_B}  PRESENTATION READY — Campus SDN NOC                  ${_N}"
echo -e "${_B}════════════════════════════════════════════════════════${_N}"
ok "Dashboard → ${_C}http://${VM_IP}:${DASHBOARD_PORT}${_N}"
ok "ML mode   → ${_C}${ML_MODE}${_N}"
echo ""
echo -e "  Traffic auto-cycles every ~50 s — dashboard updates every 2 s"
echo -e "  To stop:  ${_C}bash examples/presentation_stop.sh${_N}"
echo -e "  Driver log: tail -f ${DRIVER_LOG}"
echo -e "${_B}════════════════════════════════════════════════════════${_N}"
echo ""
