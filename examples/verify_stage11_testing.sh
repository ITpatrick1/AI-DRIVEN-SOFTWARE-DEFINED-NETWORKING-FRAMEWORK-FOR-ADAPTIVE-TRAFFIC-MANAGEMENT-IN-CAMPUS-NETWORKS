#!/usr/bin/env bash
set -euo pipefail

# Stage 11 verifier:
# - validates testing/evaluation tooling exists
# - runs full before-vs-after adaptive comparison
# - validates measurable evidence artifacts

VENV_PATH="${VENV_PATH:-$HOME/sdn-env}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PW="${SUDO_PASSWORD:-}"
TAG="${1:-stage11_$(date +%Y%m%d_%H%M%S)}"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/results}"
RUN_LOG="/tmp/stage11_run.log"

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

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

echo "[1/5] Static Stage 11 checklist..."
for p in \
  "throughput_mbps" \
  "loss_pct" \
  "pingall_loss_pct" \
  "congestion_response_s" \
  "backup_icmp_packets" \
  "reroute_observed"
do
  if ! has_pattern "${p}" "examples/adaptive_eval.py"; then
    echo "[FAIL] Missing Stage 11 metric in adaptive_eval.py: ${p}"
    exit 1
  fi
done
for p in \
  "before_adaptive" \
  "after_adaptive" \
  "stage11_comparison_" \
  "congestion_response_s" \
  "reroute_packets" \
  "measurable_project_results"
do
  if ! has_pattern "${p}" "examples/run_stage11_evaluation.sh"; then
    echo "[FAIL] Missing Stage 11 comparison element: ${p}"
    exit 1
  fi
done
echo "[PASS] Stage 11 source checks passed."

echo "[2/5] Running Stage 11 evaluation workflow..."
if ! SUDO_PASSWORD="${PW}" examples/run_stage11_evaluation.sh "${TAG}" >"${RUN_LOG}" 2>&1; then
  echo "[FAIL] Stage 11 runner failed."
  tail -n 220 "${RUN_LOG}" || true
  exit 1
fi
echo "[PASS] Stage 11 runner completed."

COMPARE_JSON="${RESULTS_DIR}/stage11_comparison_${TAG}.json"
COMPARE_CSV="${RESULTS_DIR}/stage11_comparison_${TAG}.csv"
COMPARE_MD="${RESULTS_DIR}/stage11_comparison_${TAG}.md"

echo "[3/5] Validating Stage 11 artifacts exist..."
for f in "${COMPARE_JSON}" "${COMPARE_CSV}" "${COMPARE_MD}"; do
  if [[ ! -f "${f}" ]]; then
    echo "[FAIL] Missing artifact: ${f}"
    exit 1
  fi
done
echo "[PASS] Stage 11 artifacts found."

echo "[4/5] Validating measurable comparison evidence..."
python3 - <<PY
import json
from pathlib import Path

p = Path("${COMPARE_JSON}")
d = json.loads(p.read_text())
before = d.get("before_adaptive", {})
after = d.get("after_adaptive", {})
impr = d.get("improvements", {})

checks = []
checks.append(("before pingall present", before.get("pingall_loss_pct") is not None))
checks.append(("after pingall present", after.get("pingall_loss_pct") is not None))
checks.append(("before throughput present", before.get("throughput_mbps") is not None))
checks.append(("after throughput present", after.get("throughput_mbps") is not None))
checks.append(("before loss present", before.get("packet_loss_pct") is not None))
checks.append(("after loss present", after.get("packet_loss_pct") is not None))
checks.append(("before delivery present", before.get("packet_delivery_pct") is not None))
checks.append(("after delivery present", after.get("packet_delivery_pct") is not None))
checks.append(("improvement deltas present", isinstance(impr, dict) and len(impr) >= 3))
checks.append(("before policy inactive", int(before.get("policy_activated_count", 0)) == 0))
checks.append(("after adaptive policy activated", int(after.get("policy_activated_count", 0)) > 0))
checks.append(("after congestion response measured", after.get("congestion_response_s") is not None))
checks.append((
    "after reroute evidence present",
    bool(after.get("reroute_observed"))
    or int(after.get("reroute_packets", 0)) > 0
))
checks.append((
    "before has less/equal reroute evidence",
    int(before.get("reroute_packets", 0)) <= int(after.get("reroute_packets", 0))
))
checks.append((
    "measurable project results written",
    isinstance(d.get("measurable_project_results"), list)
    and len(d.get("measurable_project_results")) >= 4
))

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
if failed:
    raise SystemExit("[FAIL] Stage 11 validation failed: " + ", ".join(failed))
print("[PASS] Stage 11 measurable evidence validated.")
PY

echo "[5/5] Stage 11 verification passed."
echo
echo "[PASS] Stage 11 complete: testing and evaluation evidence is ready."
echo "Artifacts:"
echo "  ${COMPARE_JSON}"
echo "  ${COMPARE_CSV}"
echo "  ${COMPARE_MD}"
echo "  ${RUN_LOG}"
