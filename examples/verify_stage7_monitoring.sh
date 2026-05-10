#!/usr/bin/env bash
set -euo pipefail

# Stage 7 traffic monitoring verifier:
# - validates dedicated monitoring module exists
# - checks Ryu REST polling of switches/ports/flows
# - confirms utilization, warnings, active flows, and trends are produced

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${LOG_DIR:-/tmp}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"
RYU_LOG="${LOG_DIR}/stage7_ryu.log"
TOPO_LOG="${LOG_DIR}/stage7_topology.log"
MON_LOG="${LOG_DIR}/stage7_monitor.log"
SUMMARY_JSON="${LOG_DIR}/stage7_monitor_summary.json"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${SUDO_PASSWORD:-}" ]]; then
    printf '%s\n' "${SUDO_PASSWORD}" | sudo -S -v >/dev/null
    return 0
  fi
  echo "[INFO] This verifier needs sudo privileges (Mininet/OVS)."
  sudo -v
}

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
  if [[ -n "${MPID:-}" ]] && kill -0 "${MPID}" 2>/dev/null; then
    kill "${MPID}" 2>/dev/null || true
    wait "${MPID}" 2>/dev/null || true
  fi
  if [[ -n "${TPID:-}" ]] && kill -0 "${TPID}" 2>/dev/null; then
    kill "${TPID}" 2>/dev/null || true
    wait "${TPID}" 2>/dev/null || true
  fi
  if [[ -n "${RPID:-}" ]] && kill -0 "${RPID}" 2>/dev/null; then
    kill "${RPID}" 2>/dev/null || true
    wait "${RPID}" 2>/dev/null || true
  fi
  sudo pkill -f "campus_topology.py" >/dev/null 2>&1 || true
  sudo pkill -f "campus_controller.py" >/dev/null 2>&1 || true
  sudo pkill -f "ryu-manager" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

echo "[1/10] Static Stage 7 checklist..."
for p in \
  "/stats/switches" \
  "/stats/port/" \
  "/stats/flow/" \
  "warnings" \
  "trend" \
  "top_ports"
do
  if ! has_pattern "${p}" "examples/traffic_monitor.py"; then
    echo "[FAIL] Missing Stage 7 monitoring element: ${p}"
    exit 1
  fi
done
echo "[PASS] Monitoring module source checks passed."

echo "[2/10] Cleaning runtime state..."
sudo mn -c >/dev/null 2>&1 || true
sudo pkill -f "campus_topology.py" >/dev/null 2>&1 || true
sudo pkill -f "campus_controller.py" >/dev/null 2>&1 || true
sudo pkill -f "ryu-manager" >/dev/null 2>&1 || true
pkill -f "traffic_monitor.py --host 127.0.0.1 --port 8090" >/dev/null 2>&1 || true
rm -f "${RYU_LOG}" "${TOPO_LOG}" "${MON_LOG}" "${SUMMARY_JSON}" >/dev/null 2>&1 || true

echo "[3/10] Starting Ryu controller + REST app..."
ryu-manager \
  --wsapi-host 127.0.0.1 \
  --wsapi-port 8081 \
  examples/campus_controller.py ryu.app.ofctl_rest >"${RYU_LOG}" 2>&1 &
RPID=$!

READY_CTRL=0
for _ in $(seq 1 30); do
  if ! kill -0 "${RPID}" 2>/dev/null; then
    echo "[FAIL] Ryu process exited early."
    tail -n 120 "${RYU_LOG}" || true
    exit 1
  fi
  if ss -ltn | grep -q ':6653' && ss -ltn | grep -q ':8081'; then
    READY_CTRL=1
    break
  fi
  sleep 0.5
done
if [[ "${READY_CTRL}" -ne 1 ]]; then
  echo "[FAIL] Ryu did not expose OpenFlow (:6653) and REST (:8081) in time."
  tail -n 120 "${RYU_LOG}" || true
  exit 1
fi
echo "[PASS] Ryu OpenFlow and REST are up."

echo "[4/10] Starting topology in hold mode..."
sudo -E python3 examples/campus_topology.py --no-cli --hold-seconds 120 >"${TOPO_LOG}" 2>&1 &
TPID=$!

RUNTIME_BASE=""
for _ in $(seq 1 50); do
  if ! kill -0 "${TPID}" 2>/dev/null; then
    echo "[FAIL] Topology exited before runtime API became available."
    tail -n 160 "${TOPO_LOG}" || true
    exit 1
  fi
  RUNTIME_BASE="$(grep -oE 'http://127\.0\.0\.1:[0-9]+' "${TOPO_LOG}" | tail -n1 || true)"
  if [[ -n "${RUNTIME_BASE}" ]]; then
    break
  fi
  sleep 1
done
if [[ -z "${RUNTIME_BASE}" ]]; then
  echo "[FAIL] Could not detect topology runtime API endpoint."
  tail -n 180 "${TOPO_LOG}" || true
  exit 1
fi
echo "[PASS] Runtime API detected at ${RUNTIME_BASE}"

echo "[5/10] Starting monitoring module..."
python3 examples/traffic_monitor.py \
  --host 127.0.0.1 \
  --port 8090 \
  --ryu-base http://127.0.0.1:8081 \
  --warn-util-pct 5 \
  --poll-interval 1 \
  --state-file /tmp/campus_traffic_monitor_stage7.json >"${MON_LOG}" 2>&1 &
MPID=$!

READY_MON=0
for _ in $(seq 1 30); do
  if ! kill -0 "${MPID}" 2>/dev/null; then
    echo "[FAIL] Monitoring process exited early."
    tail -n 120 "${MON_LOG}" || true
    exit 1
  fi
  if curl -fsS "http://127.0.0.1:8090/health" >/dev/null 2>&1; then
    READY_MON=1
    break
  fi
  sleep 1
done
if [[ "${READY_MON}" -ne 1 ]]; then
  echo "[FAIL] Monitoring API did not become ready on :8090."
  tail -n 120 "${MON_LOG}" || true
  exit 1
fi
echo "[PASS] Monitoring module is up."

echo "[6/10] Triggering load for visibility/warnings..."
curl -fsS -X POST "${RUNTIME_BASE}/start_stress" \
  -H "Content-Type: application/json" \
  -d '{"seconds":25,"reverse_download":true}' >/dev/null
sleep 8

echo "[7/10] Fetching monitoring summary..."
curl -fsS "http://127.0.0.1:8090/api/summary" >"${SUMMARY_JSON}"

echo "[8/10] Validating summary content..."
python3 - <<PY
import json
import sys
from pathlib import Path

p = Path("${SUMMARY_JSON}")
if not p.exists():
    print("[FAIL] summary output missing:", p)
    sys.exit(1)
d = json.loads(p.read_text())
checks = []
checks.append(("summary ok", bool(d.get("ok"))))
checks.append(("switch count > 0", int(d.get("switch_count", 0)) > 0))
checks.append(("active flows > 0", int(d.get("active_flows_total", 0)) > 0))
checks.append(("top_ports available", isinstance(d.get("top_ports"), list) and len(d.get("top_ports")) > 0))
trend = d.get("trend", {})
checks.append(("trend has points", isinstance(trend.get("points"), list) and len(trend.get("points")) >= 2))
checks.append(("warnings key available", isinstance(d.get("warnings"), list)))
checks.append(("warnings generated", int(d.get("warnings_count", 0)) >= 1))

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
if failed:
    print("[FAIL] Stage 7 summary validation failed:", ", ".join(failed))
    sys.exit(1)
print("[PASS] Monitoring summary includes utilization, flows, warnings, and trends.")
PY

echo "[9/10] Stopping stress traffic..."
curl -fsS -X POST "${RUNTIME_BASE}/stop_stress" >/dev/null || true
sleep 2

echo "[10/10] Stage 7 verification passed."
echo
echo "[PASS] Stage 7 complete: traffic monitoring module is operational."
echo "Artifacts:"
echo "  Ryu log      : ${RYU_LOG}"
echo "  Topology log : ${TOPO_LOG}"
echo "  Monitor log  : ${MON_LOG}"
echo "  Summary JSON : ${SUMMARY_JSON}"
