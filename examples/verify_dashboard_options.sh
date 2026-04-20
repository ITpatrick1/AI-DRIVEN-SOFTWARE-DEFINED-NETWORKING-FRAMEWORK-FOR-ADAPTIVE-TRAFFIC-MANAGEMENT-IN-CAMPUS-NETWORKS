#!/usr/bin/env bash
set -euo pipefail

# Verify that the Network Manager UI backend options are truly functional.
# Run this while full stack is active:
#   examples/run_full_stack.sh
# Then in another terminal:
#   examples/verify_dashboard_options.sh

BASE_URL="${1:-http://127.0.0.1:8080}"
TMP_DIR="${TMPDIR:-/tmp}"

curl_json_retry() {
  local method="$1"
  local url="$2"
  local out="$3"
  local data="${4:-}"
  local tries="${5:-10}"
  local delay="${6:-2}"
  local i
  for i in $(seq 1 "${tries}"); do
    if [[ -n "${data}" ]]; then
      if curl -fsS -X "${method}" "${url}" -H "Content-Type: application/json" -d "${data}" >"${out}"; then
        return 0
      fi
      # Capture body for diagnostics when HTTP status is non-2xx.
      curl -sS -X "${method}" "${url}" -H "Content-Type: application/json" -d "${data}" >"${out}" 2>/dev/null || true
    else
      if curl -fsS -X "${method}" "${url}" >"${out}"; then
        return 0
      fi
      curl -sS -X "${method}" "${url}" >"${out}" 2>/dev/null || true
    fi
    echo "  attempt ${i}/${tries} failed for ${url}; retrying in ${delay}s..."
    sleep "${delay}"
  done
  echo "Request failed after retries: ${url}"
  if [[ -f "${out}" ]]; then
    echo "Response body:"
    cat "${out}" || true
    echo
  fi
  return 1
}

echo "[1/10] Checking metrics endpoint..."
curl_json_retry "GET" "${BASE_URL}/api/metrics" "${TMP_DIR}/campus_verify_metrics.json" "" 8 2

echo "[2/10] Checking topology endpoint..."
curl_json_retry "GET" "${BASE_URL}/api/topology" "${TMP_DIR}/campus_verify_topology_before.json" "" 10 2

echo "[3/10] Triggering real pingall..."
curl_json_retry "POST" "${BASE_URL}/api/actions/pingall" "${TMP_DIR}/campus_verify_pingall.json" "" 20 2

# Keep host names short to stay within Linux ifname constraints.
HOST_NAME="hui$((RANDOM % 9000 + 1000))"
HOST_IP="10.0.0.$((RANDOM % 80 + 150))"

echo "[4/10] Adding live host ${HOST_NAME} (${HOST_IP})..."
curl_json_retry "POST" "${BASE_URL}/api/devices" "${TMP_DIR}/campus_verify_add_device.json" \
  "{\"name\":\"${HOST_NAME}\",\"ip\":\"${HOST_IP}\",\"attach_switch\":\"s5\",\"bandwidth_mbps\":30}" 12 2

sleep 2

echo "[5/10] Reading devices and topology after add..."
curl_json_retry "GET" "${BASE_URL}/api/devices" "${TMP_DIR}/campus_verify_devices.json" "" 10 2
curl_json_retry "GET" "${BASE_URL}/api/topology" "${TMP_DIR}/campus_verify_topology_after.json" "" 10 2

echo "[6/10] Checking flow endpoint..."
curl_json_retry "GET" "${BASE_URL}/api/flows?switch=s1" "${TMP_DIR}/campus_verify_flows.json" "" 10 2

echo "[7/10] Checking operations endpoint..."
curl_json_retry "GET" "${BASE_URL}/api/operations" "${TMP_DIR}/campus_verify_ops_before.json" "" 10 2

echo "[8/10] Starting live stress demo..."
curl_json_retry "POST" "${BASE_URL}/api/actions/start-stress" "${TMP_DIR}/campus_verify_stress_start.json" \
  "{\"seconds\":20,\"reverse_download\":true}" 10 2
sleep 2
curl_json_retry "GET" "${BASE_URL}/api/operations" "${TMP_DIR}/campus_verify_ops_during.json" "" 10 2

echo "[9/10] Stopping live stress demo..."
curl_json_retry "POST" "${BASE_URL}/api/actions/stop-stress" "${TMP_DIR}/campus_verify_stress_stop.json" "" 10 2
sleep 1
curl_json_retry "GET" "${BASE_URL}/api/operations" "${TMP_DIR}/campus_verify_ops_after.json" "" 10 2

echo "[10/10] Validating JSON responses..."
python3 - <<'PY'
import json
from pathlib import Path

tmp = Path("/tmp")
metrics = json.loads((tmp / "campus_verify_metrics.json").read_text())
topo_before = json.loads((tmp / "campus_verify_topology_before.json").read_text())
pingall = json.loads((tmp / "campus_verify_pingall.json").read_text())
added = json.loads((tmp / "campus_verify_add_device.json").read_text())
devices = json.loads((tmp / "campus_verify_devices.json").read_text())
topo_after = json.loads((tmp / "campus_verify_topology_after.json").read_text())
flows = json.loads((tmp / "campus_verify_flows.json").read_text())
ops_before = json.loads((tmp / "campus_verify_ops_before.json").read_text())
ops_during = json.loads((tmp / "campus_verify_ops_during.json").read_text())
ops_after = json.loads((tmp / "campus_verify_ops_after.json").read_text())
stress_start = json.loads((tmp / "campus_verify_stress_start.json").read_text())
stress_stop = json.loads((tmp / "campus_verify_stress_stop.json").read_text())

host_name = added.get("device", {}).get("name")
after_nodes = {n.get("id") for n in topo_after.get("nodes", [])}
before_nodes = {n.get("id") for n in topo_before.get("nodes", [])}
device_names = {d.get("name") for d in devices} if isinstance(devices, list) else set()
running_during = ops_during.get("running_stress_clients", [])
running_after = ops_after.get("running_stress_clients", [])
pingall_result = pingall.get("result", {})

checks = [
    ("metrics has connected_switches", isinstance(metrics.get("connected_switches"), list)),
    (
        "topology has nodes",
        len(topo_before.get("nodes", [])) > 0 or len(topo_after.get("nodes", [])) > 0,
    ),
    ("pingall returned ok", bool(pingall.get("ok"))),
    ("pingall includes detailed result", bool(pingall_result) and "avg_rtt_ms" in pingall_result),
    ("add host returned ok", bool(added.get("ok"))),
    ("new host appears in topology", bool(host_name) and host_name in after_nodes and host_name not in before_nodes),
    ("new host appears in devices", bool(host_name) and host_name in device_names),
    ("flows endpoint returned payload", "output" in flows and "switch" in flows),
    ("operations endpoint returned payload", bool(ops_before.get("ok")) and isinstance(ops_before.get("events"), list)),
    ("stress start returned ok", bool(stress_start.get("ok"))),
    ("stress running clients visible", isinstance(running_during, list) and len(running_during) > 0),
    ("stress stop returned ok", bool(stress_stop.get("ok"))),
    ("stress clients stopped", isinstance(running_after, list) and len(running_after) == 0),
]

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")

if failed:
    raise SystemExit("Verification failed: " + ", ".join(failed))

print("All dashboard options verified successfully.")
PY
