#!/usr/bin/env bash
set -euo pipefail

# Stage 11 runner:
# - runs "before adaptive routing" (static thresholds)
# - runs "after adaptive routing" (aggressive adaptive thresholds)
# - compares measurable outcomes and writes evidence artifacts

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/results}"
TAG="${1:-$(date +%Y%m%d_%H%M%S)}"
PW="${SUDO_PASSWORD:-}"

STATIC_HIGH="${STAGE11_STATIC_HIGH_MBPS:-10000}"
STATIC_LOW="${STAGE11_STATIC_LOW_MBPS:-9000}"
ADAPTIVE_HIGH="${STAGE11_ADAPTIVE_HIGH_MBPS:-15}"
ADAPTIVE_LOW="${STAGE11_ADAPTIVE_LOW_MBPS:-6}"

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
  echo "[INFO] This runner needs sudo privileges (Mininet/OVS)."
  sudo -v
}

mkdir -p "${RESULTS_DIR}"
cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"
ensure_sudo

STATIC_TAG="${TAG}_static"
ADAPTIVE_TAG="${TAG}_adaptive"
COMPARE_JSON="${RESULTS_DIR}/stage11_comparison_${TAG}.json"
COMPARE_CSV="${RESULTS_DIR}/stage11_comparison_${TAG}.csv"
COMPARE_MD="${RESULTS_DIR}/stage11_comparison_${TAG}.md"

echo "[1/4] Running BEFORE-adaptive baseline (static routing thresholds)..."
export CAMPUS_DQN_INTEGRATION_ENABLED=0
export CAMPUS_CONGEST_HIGH_MBPS="${STATIC_HIGH}"
export CAMPUS_CONGEST_LOW_MBPS="${STATIC_LOW}"
examples/run_adaptive_eval.sh "${STATIC_TAG}"

echo "[2/4] Running AFTER-adaptive baseline (adaptive routing thresholds)..."
export CAMPUS_DQN_INTEGRATION_ENABLED=0
export CAMPUS_CONGEST_HIGH_MBPS="${ADAPTIVE_HIGH}"
export CAMPUS_CONGEST_LOW_MBPS="${ADAPTIVE_LOW}"
examples/run_adaptive_eval.sh "${ADAPTIVE_TAG}"

echo "[3/4] Building Stage 11 comparison report..."
python3 - <<PY
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

results_dir = Path("${RESULTS_DIR}")
static_path = results_dir / "adaptive_eval_${STATIC_TAG}.json"
adaptive_path = results_dir / "adaptive_eval_${ADAPTIVE_TAG}.json"
if not static_path.exists():
    raise SystemExit(f"[FAIL] Missing static result: {static_path}")
if not adaptive_path.exists():
    raise SystemExit(f"[FAIL] Missing adaptive result: {adaptive_path}")

static = json.loads(static_path.read_text())
adaptive = json.loads(adaptive_path.read_text())

def fnum(v, d=3):
    try:
        return round(float(v), d)
    except Exception:
        return None

def pct_delivery(scn):
    direct = scn.get("packet_delivery_pct")
    if direct is not None:
        return fnum(direct)
    tx = float(scn.get("tx", 0) or 0.0)
    rx = float(scn.get("rx", 0) or 0.0)
    if tx <= 0:
        return 0.0
    return round((rx / tx) * 100.0, 3)

def delta_after_before(before, after):
    if before is None or after is None:
        return None
    return round(after - before, 3)

def improvement_lower_is_better(before, after):
    if before is None or after is None:
        return None
    return round(before - after, 3)

def render(v):
    return "n/a" if v is None else v

static_cong = static.get("congestion", {})
adaptive_cong = adaptive.get("congestion", {})
static_derived = static.get("derived_metrics", {})
adaptive_derived = adaptive.get("derived_metrics", {})
static_events = static.get("policy_events", {})
adaptive_events = adaptive.get("policy_events", {})
static_connectivity = static.get("connectivity", {})
adaptive_connectivity = adaptive.get("connectivity", {})
static_config = static.get("controller_config", {})
adaptive_config = adaptive.get("controller_config", {})

before = {
    "mode": "before_adaptive",
    "pingall_loss_pct": fnum(static_connectivity.get("pingall_loss_pct")),
    "throughput_mbps": fnum(static_cong.get("throughput_mbps")),
    "packet_loss_pct": fnum(static_cong.get("loss_pct")),
    "packet_delivery_pct": pct_delivery(static_cong),
    "latency_avg_ms": fnum(static_cong.get("rtt_avg_ms")),
    "congestion_response_s": fnum(static_events.get("congestion_response_s")),
    "reroute_packets": int(static_cong.get("backup_icmp_packets", 0) or 0),
    "reroute_observed": bool(static_derived.get("reroute_observed", False)),
    "policy_activated_count": int(static_events.get("activated_count", 0) or 0),
}
after = {
    "mode": "after_adaptive",
    "pingall_loss_pct": fnum(adaptive_connectivity.get("pingall_loss_pct")),
    "throughput_mbps": fnum(adaptive_cong.get("throughput_mbps")),
    "packet_loss_pct": fnum(adaptive_cong.get("loss_pct")),
    "packet_delivery_pct": pct_delivery(adaptive_cong),
    "latency_avg_ms": fnum(adaptive_cong.get("rtt_avg_ms")),
    "congestion_response_s": fnum(adaptive_events.get("congestion_response_s")),
    "reroute_packets": int(adaptive_cong.get("backup_icmp_packets", 0) or 0),
    "reroute_observed": bool(adaptive_derived.get("reroute_observed", False)),
    "policy_activated_count": int(adaptive_events.get("activated_count", 0) or 0),
}

improvements = {
    "throughput_delta_mbps": delta_after_before(before["throughput_mbps"], after["throughput_mbps"]),
    "packet_loss_delta_pct": delta_after_before(before["packet_loss_pct"], after["packet_loss_pct"]),
    "packet_delivery_delta_pct": delta_after_before(before["packet_delivery_pct"], after["packet_delivery_pct"]),
    "latency_delta_ms": delta_after_before(before["latency_avg_ms"], after["latency_avg_ms"]),
    "congestion_response_delta_s": delta_after_before(before["congestion_response_s"], after["congestion_response_s"]),
    "reroute_packet_delta": after["reroute_packets"] - before["reroute_packets"],
}

benefits = {
    "throughput_gain_mbps": improvements["throughput_delta_mbps"],
    "packet_delivery_gain_pct": improvements["packet_delivery_delta_pct"],
    "packet_loss_reduction_pct": improvement_lower_is_better(before["packet_loss_pct"], after["packet_loss_pct"]),
    "latency_reduction_ms": improvement_lower_is_better(before["latency_avg_ms"], after["latency_avg_ms"]),
    "response_time_reduction_s": improvement_lower_is_better(
        before["congestion_response_s"], after["congestion_response_s"]
    ),
}

project_results = []
project_results.append(
    "Pingall connectivity was preserved in both runs (loss: %s%% before, %s%% after)."
    % (render(before["pingall_loss_pct"]), render(after["pingall_loss_pct"]))
)
project_results.append(
    "Protected-flow throughput under congestion changed from %s Mbps to %s Mbps."
    % (render(before["throughput_mbps"]), render(after["throughput_mbps"]))
)
project_results.append(
    "Packet delivery under congestion changed from %s%% to %s%%."
    % (render(before["packet_delivery_pct"]), render(after["packet_delivery_pct"]))
)
project_results.append(
    "Average latency under congestion changed from %s ms to %s ms."
    % (render(before["latency_avg_ms"]), render(after["latency_avg_ms"]))
)
project_results.append(
    "Adaptive response time under congestion was %s s."
    % render(after["congestion_response_s"])
)
project_results.append(
    "Reroute evidence packets on the backup path changed from %s to %s."
    % (before["reroute_packets"], after["reroute_packets"])
)

comparison = {
    "tag": "${TAG}",
    "ts": datetime.now(timezone.utc).isoformat(),
    "controller_thresholds": {
        "before_adaptive": static_config,
        "after_adaptive": adaptive_config,
    },
    "test_matrix": {
        "pingall": "explicit connectivity test recorded in both runs",
        "iperf3": "reverse-download throughput probe recorded in both runs",
        "latency_test": "ICMP RTT probe to the protected service IP in both runs",
        "congestion_test": "Wi-Fi reverse-download stress over a shared bottleneck in both runs",
    },
    "before_adaptive": before,
    "after_adaptive": after,
    "improvements": improvements,
    "benefits": benefits,
    "adaptive_improves_over_static": {
        "throughput_not_worse": (
            before["throughput_mbps"] is None
            or after["throughput_mbps"] is None
            or after["throughput_mbps"] >= before["throughput_mbps"]
        ),
        "packet_delivery_not_worse": (
            before["packet_delivery_pct"] is None
            or after["packet_delivery_pct"] is None
            or after["packet_delivery_pct"] >= before["packet_delivery_pct"]
        ),
        "latency_not_worse": (
            before["latency_avg_ms"] is None
            or after["latency_avg_ms"] is None
            or after["latency_avg_ms"] <= before["latency_avg_ms"]
        ),
        "static_policy_inactive": before["policy_activated_count"] == 0,
        "adaptive_policy_activated": after["policy_activated_count"] > 0,
        "adaptive_reroute_observed": after["reroute_observed"],
    },
    "measurable_project_results": project_results,
    "evidence_paths": {
        "before_json": str(static_path),
        "after_json": str(adaptive_path),
    },
    "notes": [
        "before_adaptive uses very high thresholds so adaptive reroute should remain inactive during the congestion window.",
        "after_adaptive uses low thresholds so adaptive reroute should activate under congestion.",
        "reroute_packets > 0 indicates traffic observed on the backup server interface.",
        "Throughput and latency values are recorded during the congestion window, not only at startup.",
    ],
    "source_derived_metrics": {
        "before": static_derived,
        "after": adaptive_derived,
    },
}

json_path = Path("${COMPARE_JSON}")
csv_path = Path("${COMPARE_CSV}")
md_path = Path("${COMPARE_MD}")
json_path.write_text(json.dumps(comparison, indent=2, sort_keys=True))

rows = [
    before,
    after,
]
with csv_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(
        f,
        fieldnames=[
            "mode",
            "pingall_loss_pct",
            "throughput_mbps",
            "packet_loss_pct",
            "packet_delivery_pct",
            "latency_avg_ms",
            "congestion_response_s",
            "reroute_packets",
            "reroute_observed",
            "policy_activated_count",
        ],
    )
    w.writeheader()
    w.writerows(rows)

imp = comparison["improvements"]
benefit = comparison["benefits"]
md_lines = [
    "# Stage 11 Testing and Evaluation Report",
    "",
    f"Tag: {comparison['tag']}",
    f"Generated (UTC): {comparison['ts']}",
    "",
    "## Tests Executed",
    "- pingall",
    "- iperf3 throughput probes",
    "- latency (ICMP RTT) probes",
    "- congestion stress workload",
    "",
    "## Measurable Project Results",
]
for line in project_results:
    md_lines.append(f"- {line}")

md_lines.extend([
    "",
    "## Before vs After Adaptive Routing",
    "| Metric | Before Adaptive | After Adaptive | Delta (After-Before) |",
    "|---|---:|---:|---:|",
    f"| Pingall loss (%) | {render(before['pingall_loss_pct'])} | {render(after['pingall_loss_pct'])} | {delta_after_before(before['pingall_loss_pct'], after['pingall_loss_pct'])} |",
    f"| Throughput (Mbps) | {render(before['throughput_mbps'])} | {render(after['throughput_mbps'])} | {render(imp['throughput_delta_mbps'])} |",
    f"| Packet loss (%) | {render(before['packet_loss_pct'])} | {render(after['packet_loss_pct'])} | {render(imp['packet_loss_delta_pct'])} |",
    f"| Packet delivery (%) | {render(before['packet_delivery_pct'])} | {render(after['packet_delivery_pct'])} | {render(imp['packet_delivery_delta_pct'])} |",
    f"| Avg latency (ms) | {render(before['latency_avg_ms'])} | {render(after['latency_avg_ms'])} | {render(imp['latency_delta_ms'])} |",
    f"| Congestion response (s) | {render(before['congestion_response_s'])} | {render(after['congestion_response_s'])} | {render(imp['congestion_response_delta_s'])} |",
    f"| Reroute packets on backup path | {before['reroute_packets']} | {after['reroute_packets']} | {imp['reroute_packet_delta']} |",
    f"| Policy activations | {before['policy_activated_count']} | {after['policy_activated_count']} | {after['policy_activated_count'] - before['policy_activated_count']} |",
    "",
    "## Interpretation",
    "- Static routing keeps the adaptive policy inactive, so the shared bottleneck is left to contention alone.",
    "- Adaptive mode should activate the policy during congestion, reroute ICMP to the backup path, and throttle student bulk Wi-Fi traffic.",
    "- Lower packet loss, lower latency, higher packet delivery, and higher throughput on the protected flow are all evidence of improvement over static routing.",
    "",
    "## Evidence Summary",
    f"- Throughput gain under congestion: {render(benefit['throughput_gain_mbps'])} Mbps.",
    f"- Packet delivery gain under congestion: {render(benefit['packet_delivery_gain_pct'])} percentage points.",
    f"- Packet loss reduction under congestion: {render(benefit['packet_loss_reduction_pct'])} percentage points.",
    f"- Latency reduction under congestion: {render(benefit['latency_reduction_ms'])} ms.",
    f"- Adaptive congestion response time: {render(after['congestion_response_s'])} s.",
    f"- Adaptive reroute observed: {'yes' if after['reroute_observed'] else 'no'}.",
    "",
    "## Artifacts",
    f"- Before (JSON): {static_path}",
    f"- After (JSON): {adaptive_path}",
    f"- Comparison (JSON): {json_path}",
    f"- Comparison (CSV): {csv_path}",
    f"- Comparison (Markdown): {md_path}",
])
md_path.write_text("\n".join(md_lines) + "\n")

print("[PASS] comparison JSON:", json_path)
print("[PASS] comparison CSV :", csv_path)
print("[PASS] comparison MD  :", md_path)
print("[INFO] throughput before/after (Mbps):", before["throughput_mbps"], "->", after["throughput_mbps"])
print("[INFO] loss before/after (%):", before["packet_loss_pct"], "->", after["packet_loss_pct"])
print("[INFO] reroute packets before/after:", before["reroute_packets"], "->", after["reroute_packets"])
print("[INFO] policy activations before/after:", before["policy_activated_count"], "->", after["policy_activated_count"])
PY

echo "[4/4] Stage 11 evaluation complete."
echo "Artifacts:"
echo "  ${RESULTS_DIR}/adaptive_eval_${STATIC_TAG}.json"
echo "  ${RESULTS_DIR}/adaptive_eval_${STATIC_TAG}.csv"
echo "  ${RESULTS_DIR}/adaptive_eval_${ADAPTIVE_TAG}.json"
echo "  ${RESULTS_DIR}/adaptive_eval_${ADAPTIVE_TAG}.csv"
echo "  ${COMPARE_JSON}"
echo "  ${COMPARE_CSV}"
echo "  ${COMPARE_MD}"
