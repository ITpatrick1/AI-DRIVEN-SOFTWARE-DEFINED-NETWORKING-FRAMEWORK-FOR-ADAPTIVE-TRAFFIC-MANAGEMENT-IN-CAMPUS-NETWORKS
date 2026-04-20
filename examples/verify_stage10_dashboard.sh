#!/usr/bin/env bash
set -euo pipefail

# Stage 10 verifier:
# - validates Flask dashboard source elements
# - launches full web-only stack
# - verifies live dashboard options/actions
# - validates Stage 10 dashboard payload and UI widgets

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
PW="${SUDO_PASSWORD:-}"

STAGE10_STACK_LOG="/tmp/stage10_stack.log"
STAGE10_OPTIONS_LOG="/tmp/stage10_options.log"
STAGE10_INDEX_HTML="/tmp/stage10_index.html"
STAGE10_DASH_JSON="/tmp/stage10_dashboard.json"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

ensure_sudo() {
  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi
  if [[ -n "${PW}" ]]; then
    printf '%s\n' "${PW}" | sudo -S -v >/dev/null
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
  SUDO_PASSWORD="${PW}" "${SCRIPT_DIR}/stop_web_only_stack.sh" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

echo "[1/7] Static Stage 10 checklist..."
for p in \
  "from flask import Flask" \
  "@app.get(\"/api/dashboard\")" \
  "\"segment_analytics\"" \
  "\"queue_depth\"" \
  "\"latency_trend\"" \
  "\"alerts\"" \
  "\"active_flow_rules\"" \
  "\"controller_actions\"" \
  "id=\"mQueueDepth\"" \
  "id=\"mLatencyTrend\"" \
  "id=\"mActiveFlows\"" \
  "id=\"alertsPane\"" \
  "id=\"mControllerActions\""
do
  if ! has_pattern "${p}" "examples/campus_dashboard.py"; then
    echo "[FAIL] Missing Stage 10 dashboard element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 10 source checks passed."

echo "[2/7] Starting web-only stack in DQN mode..."
if ! SUDO_PASSWORD="${PW}" examples/run_web_only_stack.sh --ml-mode dqn >"${STAGE10_STACK_LOG}" 2>&1; then
  echo "[FAIL] Web-only stack failed to start."
  tail -n 180 "${STAGE10_STACK_LOG}" || true
  exit 1
fi
echo "[PASS] Web-only stack is up."

echo "[3/7] Running live dashboard option checks..."
if ! examples/verify_dashboard_options.sh >"${STAGE10_OPTIONS_LOG}" 2>&1; then
  echo "[FAIL] Dashboard options verification failed."
  tail -n 180 "${STAGE10_OPTIONS_LOG}" || true
  exit 1
fi
echo "[PASS] Dashboard option checks passed."

echo "[4/7] Validating dashboard HTML widgets..."
curl -fsS "http://127.0.0.1:8080/" >"${STAGE10_INDEX_HTML}"
for id in mQueueDepth mLatencyTrend mActiveFlows alertsPane mControllerActions; do
  if ! grep -q "id=\"${id}\"" "${STAGE10_INDEX_HTML}"; then
    echo "[FAIL] Dashboard HTML missing widget id=${id}"
    exit 1
  fi
done
echo "[PASS] Dashboard HTML includes Stage 10 widgets."

echo "[5/7] Validating /api/dashboard payload..."
curl -fsS "http://127.0.0.1:8080/api/dashboard" >"${STAGE10_DASH_JSON}"
python3 - <<'PY'
import json
from pathlib import Path

p = Path("/tmp/stage10_dashboard.json")
if not p.exists():
    raise SystemExit("[FAIL] /api/dashboard output file missing")
d = json.loads(p.read_text())

checks = []
checks.append(("has segment_analytics", isinstance(d.get("segment_analytics"), dict)))
checks.append(("has queue_depth", isinstance(d.get("queue_depth"), dict)))
checks.append(("has latency_trend", isinstance(d.get("latency_trend"), dict)))
checks.append(("has alerts list", isinstance(d.get("alerts"), list)))
checks.append(("has active_flow_rules", isinstance(d.get("active_flow_rules"), dict)))
checks.append(("has controller_actions", isinstance(d.get("controller_actions"), dict)))
checks.append(("has link_utilization", isinstance(d.get("link_utilization"), list)))
checks.append(
    (
        "segment analytics has segments",
        isinstance(d.get("segment_analytics", {}).get("segments"), list)
        and len(d.get("segment_analytics", {}).get("segments", [])) >= 4,
    )
)
checks.append(
    (
        "segment analytics has history series",
        isinstance(d.get("segment_analytics", {}).get("history", {}).get("series"), list),
    )
)
checks.append(
    (
        "segment analytics has analysis",
        isinstance(d.get("segment_analytics", {}).get("analysis"), dict),
    )
)
checks.append(("queue depth has total_packets", "total_packets" in d.get("queue_depth", {})))
checks.append(("latency trend has points", isinstance(d.get("latency_trend", {}).get("points"), list)))
checks.append(("active flows has total", "total" in d.get("active_flow_rules", {})))
checks.append(("controller actions has reroute state", "reroute_active" in d.get("controller_actions", {})))
checks.append(
    (
        "controller actions has policy events",
        isinstance(d.get("controller_actions", {}).get("recent_policy_events"), list),
    )
)
summary = d.get("summary", {})
checks.append(("summary has connected switches", int(summary.get("connected_switches", 0)) > 0))
checks.append(("summary has nodes", int(summary.get("nodes", 0)) > 0))
checks.append(("summary has links", int(summary.get("links", 0)) > 0))

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
if failed:
    raise SystemExit("[FAIL] Stage 10 payload validation failed: " + ", ".join(failed))
print("[PASS] Stage 10 /api/dashboard payload is valid.")
PY

echo "[6/7] Smoke check: dashboard endpoint freshness..."
sleep 2
FRESH_JSON="/tmp/stage10_dashboard_fresh.json"
READY_FRESH=0
for _ in $(seq 1 10); do
  if curl -fsS "http://127.0.0.1:8080/api/dashboard" >"${FRESH_JSON}" 2>/dev/null; then
    READY_FRESH=1
    break
  fi
  sleep 1
done
if [[ "${READY_FRESH}" -ne 1 ]]; then
  echo "[FAIL] Could not fetch fresh dashboard snapshot."
  exit 1
fi
python3 - <<'PY'
import json
from pathlib import Path
d = json.loads(Path("/tmp/stage10_dashboard_fresh.json").read_text())
summary = d.get("summary", {})
print("[INFO] connected_switches =", summary.get("connected_switches"))
print("[INFO] max_link_util_pct =", summary.get("max_link_util_pct"))
print("[INFO] alerts_count =", summary.get("alerts_count"))
PY

echo "[7/7] Stage 10 verification passed."
echo
echo "[PASS] Stage 10 complete: Flask monitoring dashboard is operational."
echo "Artifacts:"
echo "  Stack log   : ${STAGE10_STACK_LOG}"
echo "  Options log : ${STAGE10_OPTIONS_LOG}"
echo "  HTML dump   : ${STAGE10_INDEX_HTML}"
echo "  JSON dump   : ${STAGE10_DASH_JSON}"
