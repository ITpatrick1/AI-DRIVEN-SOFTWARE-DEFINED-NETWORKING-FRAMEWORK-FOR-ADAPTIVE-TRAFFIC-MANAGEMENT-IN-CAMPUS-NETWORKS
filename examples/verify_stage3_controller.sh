#!/usr/bin/env bash
set -euo pipefail

# Stage 3 controller verifier:
# - validates required control-plane elements in code
# - runs controller + topology smoke run
# - checks for switch registration, PACKET_IN handling, and FLOW_MOD installation

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
RYU_LOG="${LOG_DIR}/stage3_ryu.log"
TOPO_LOG="${LOG_DIR}/stage3_topology.log"
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

echo "[1/7] Static controller checklist..."
for p in \
  "switch_features_handler" \
  "OFPMatch()" \
  "OFPActionOutput(ofproto.OFPP_CONTROLLER" \
  "self.mac_to_port" \
  "EventOFPPacketIn" \
  "OFPFlowMod" \
  "OFPP_FLOOD"
do
  if ! has_pattern "${p}" "examples/campus_controller.py"; then
    echo "[FAIL] Missing required control-plane element: ${p}"
    exit 1
  fi
done
echo "[PASS] Required controller elements found."

echo "[2/7] Cleaning Mininet state..."
sudo mn -c >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager examples/campus_controller.py" >/dev/null 2>&1 || true
rm -f "${RYU_LOG}" "${TOPO_LOG}" "${METRICS_FILE}" >/dev/null 2>&1 || true

echo "[3/7] Starting Ryu controller..."
ryu-manager examples/campus_controller.py >"${RYU_LOG}" 2>&1 &
RPID=$!

RYU_READY=0
for _ in $(seq 1 20); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "[FAIL] Ryu process exited early."
    tail -n 120 "${RYU_LOG}" || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653'; then
    RYU_READY=1
    break
  fi
  sleep 0.5
done
if [[ "${RYU_READY}" -ne 1 ]]; then
  echo "[FAIL] Ryu did not start listening on :6653."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi

echo "[4/7] Running topology to trigger controller logic..."
if ! sudo -E python3 examples/campus_topology.py --no-cli >"${TOPO_LOG}" 2>&1; then
  echo "[FAIL] Topology run failed."
  tail -n 120 "${TOPO_LOG}" || true
  exit 1
fi

echo "[5/7] Checking switch registration..."
if ! has_pattern "Switch connected: dpid=" "${RYU_LOG}"; then
  echo "[FAIL] No switch registration logs found in Ryu output."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Switches registered with Ryu."

echo "[6/7] Checking PACKET_IN and FLOW_MOD behavior..."
if ! has_pattern "PACKET_IN table-miss" "${RYU_LOG}"; then
  echo "[FAIL] PACKET_IN table-miss logic not observed."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi
if ! has_pattern "FLOW_MOD install" "${RYU_LOG}"; then
  echo "[FAIL] FLOW_MOD installation not observed."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] PACKET_IN and FLOW_MOD activity observed."

echo "[7/7] Checking controller counters..."
python3 - <<PY
import json
import sys
from pathlib import Path

p = Path("${METRICS_FILE}")
if not p.exists():
    print("[FAIL] metrics file not found:", p)
    sys.exit(1)
data = json.loads(p.read_text())
pkt = int(data.get("controller_packet_ins", 0))
fmod = int(data.get("controller_flow_mods", 0))
learn = int(data.get("controller_mac_learns", 0))
if pkt <= 0:
    print("[FAIL] controller_packet_ins <= 0")
    sys.exit(1)
if fmod <= 0:
    print("[FAIL] controller_flow_mods <= 0")
    sys.exit(1)
if learn <= 0:
    print("[FAIL] controller_mac_learns <= 0")
    sys.exit(1)
print(f"[PASS] Counters packet_ins={pkt} flow_mods={fmod} mac_learns={learn}")
PY

echo
echo "[PASS] Stage 3 complete: basic Ryu SDN controller is working."
echo "Logs:"
echo "  Ryu      : ${RYU_LOG}"
echo "  Topology : ${TOPO_LOG}"
