#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SURVEY_CSV="${1:-${REPO_ROOT}/Stakeholder Requirement Survey Intelligent SDN Project .csv}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

if [[ ! -f "${SURVEY_CSV}" ]]; then
  echo "[FAIL] Survey CSV not found: ${SURVEY_CSV}"
  exit 1
fi

python3 "${SCRIPT_DIR}/stakeholder_requirements.py" \
  --csv "${SURVEY_CSV}" \
  --report-json "${TMP_DIR}/report.json" \
  --report-md "${TMP_DIR}/report.md" \
  --manual-settings-file "${TMP_DIR}/manual.json" \
  --security-policy-file "${TMP_DIR}/security.json" \
  --dqn-policy-file "${TMP_DIR}/dqn.json" \
  --output-dir "${TMP_DIR}/results" >/dev/null

python3 - "${TMP_DIR}" <<'PY'
import json
import os
import sys

tmp_dir = sys.argv[1]
checks = []

def load(name):
    path = os.path.join(tmp_dir, name)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)

report = load("report.json")
manual = load("manual.json")
security = load("security.json")
dqn = load("dqn.json")

checks.append(("report has responses", int(report["survey_summary"]["response_count"]) > 0))
checks.append(("manual settings high>low", float(manual["congest_high_mbps"]) > float(manual["congest_low_mbps"])))
checks.append(("security policy has protected servers", len(security["protected_server_ips"]) >= 1))
checks.append(("dqn policy has reward weights", isinstance(dqn.get("reward_weights"), dict) and bool(dqn["reward_weights"])))
checks.append(("report copied to results", os.path.isfile(os.path.join(tmp_dir, "results", "stakeholder_analysis_latest.json"))))

failed = [name for name, ok in checks if not ok]
for name, ok in checks:
    print(("[PASS]" if ok else "[FAIL]"), name)
if failed:
    raise SystemExit(1)
PY

python3 -m py_compile \
  "${SCRIPT_DIR}/stakeholder_requirements.py" \
  "${SCRIPT_DIR}/campus_controller.py" \
  "${SCRIPT_DIR}/campus_dashboard.py" \
  "${SCRIPT_DIR}/dqn_routing_agent.py"

echo "[PASS] Stakeholder policy integration compiles cleanly"
