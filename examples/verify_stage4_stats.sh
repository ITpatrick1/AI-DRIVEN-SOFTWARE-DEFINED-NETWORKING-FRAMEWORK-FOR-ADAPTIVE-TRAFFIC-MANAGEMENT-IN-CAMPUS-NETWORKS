#!/usr/bin/env bash
set -euo pipefail

# Stage 4 statistics verifier:
# - ensures controller sends periodic port stats requests
# - ensures per-switch/port metrics are produced
# - ensures utilization/rate/throughput values are computed and logged

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage4_ryu.log"
TOPO_LOG="${LOG_DIR}/stage4_topology.log"
METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

has_pattern() {
  local pattern="$1"
  local file="$2"
  if command -v rg >/dev/null 2>&1; then
    rg -F -q "${pattern}" "${file}"
  else
    grep -F -q "${pattern}" "${file}"
  fi
}

cleanup() {
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"

echo "[1/7] Static stats checklist..."
for p in \
  "OFPPortStatsRequest" \
  "self.port_samples" \
  "switch_port_mbps" \
  "switch_port_util_pct" \
  "switch_port_stats" \
  "PORT_STATS dpid="
do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing Stage 4 stats element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 4 stats elements found in controller code."

echo "[2/7] Cleaning runtime state..."
sudo mn -c >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
rm -f "${RYU_LOG}" "${TOPO_LOG}" "${METRICS_FILE}" >/dev/null 2>&1 || true

echo "[3/7] Starting Ryu controller..."
ryu-manager examples/campus_controller.py >"${RYU_LOG}" 2>&1 &
RPID=$!
READY=0
for _ in $(seq 1 20); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "[FAIL] Ryu process exited early."
    tail -n 120 "${RYU_LOG}" || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    READY=1
    break
  fi
  sleep 0.5
done
if [[ "${READY}" -ne 1 ]]; then
  echo "[FAIL] Ryu did not start listening on :6653."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi

echo "[4/7] Running topology to generate traffic and stats..."
if ! sudo -E python3 examples/campus_topology.py --no-cli >"${TOPO_LOG}" 2>&1; then
  echo "[FAIL] Topology run failed."
  tail -n 120 "${TOPO_LOG}" || true
  exit 1
fi

echo "[5/7] Checking continuous controller stats logs..."
STATS_COUNT="$(grep -c 'PORT_STATS dpid=' "${RYU_LOG}" || true)"
if [[ "${STATS_COUNT}" -lt 2 ]]; then
  echo "[FAIL] Expected continuous stats logs; found ${STATS_COUNT} entries."
  tail -n 160 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Controller logged continuous utilization stats (${STATS_COUNT} entries)."

echo "[6/7] Validating metrics file fields..."
python3 - <<PY
import json
import re
import sys
from pathlib import Path

p = Path("${METRICS_FILE}")
if not p.exists():
    print("[FAIL] metrics file missing:", p)
    sys.exit(1)

data = json.loads(p.read_text())
for k in ("switch_port_mbps", "switch_port_util_pct", "switch_port_stats"):
    if not isinstance(data.get(k), dict) or not data.get(k):
        print(f"[FAIL] missing or empty metrics field: {k}")
        sys.exit(1)

# Ensure at least one port has meaningful traffic-rate values.
stats = data["switch_port_stats"]
found = False
for _, ports in stats.items():
    for _, s in ports.items():
        mbps = float(s.get("mbps", 0.0))
        util = float(s.get("util_pct", 0.0))
        bps = float(s.get("bps", 0.0))
        if mbps > 0.0 and bps > 0.0 and util >= 0.0:
            found = True
            break
    if found:
        break
if not found:
    # Topology may have already stopped and metrics can decay to zeros.
    # Fallback to controller PORT_STATS logs captured during run.
    logp = Path("${RYU_LOG}")
    if not logp.exists():
        print("[FAIL] no throughput/rate values detected in switch_port_stats and Ryu log missing")
        sys.exit(1)
    txt = logp.read_text(errors="ignore")
    rates = [float(x) for x in re.findall(r"=([0-9]+(?:\\.[0-9]+)?)Mbps", txt)]
    if not rates or max(rates) <= 0.0:
        print("[FAIL] no throughput/rate values detected in switch_port_stats or PORT_STATS logs")
        sys.exit(1)
print("[PASS] metrics/logs include real-time rate/utilization/throughput values.")
PY

echo "[7/7] Stage 4 verification passed."
echo
echo "[PASS] Stage 4 complete: traffic statistics collection is operational."
echo "Logs:"
echo "  Ryu      : ${RYU_LOG}"
echo "  Topology : ${TOPO_LOG}"
echo "  Metrics  : ${METRICS_FILE}"
