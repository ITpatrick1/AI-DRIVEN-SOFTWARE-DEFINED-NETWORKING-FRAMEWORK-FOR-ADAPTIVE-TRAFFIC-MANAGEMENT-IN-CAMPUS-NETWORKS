#!/usr/bin/env bash
set -euo pipefail

# Phase III Scenarios Verification Script
# Verifies that Phase III scenario testing generates the correct artifacts
# and that all scenarios passed the acceptance criteria.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/results}"

_GREEN='\033[0;32m'
_RED='\033[0;31m'
_CYAN='\033[0;36m'
_NC='\033[0m'

ok()   { echo -e "${_GREEN}[OK]${_NC}  $*"; }
fail() { echo -e "${_RED}[FAIL]${_NC} $*" >&2; }
info() { echo -e "${_CYAN}[INFO]${_NC} $*"; }

cd "${REPO_ROOT}"

echo "==========================================================="
echo "Verifying Phase III Scenarios (Intelligent Testing)"
echo "==========================================================="

LATEST_JSON=$(ls -t "${RESULTS_DIR}"/phase3_scenarios_*.json 2>/dev/null | head -1 || true)

if [[ -z "${LATEST_JSON}" ]]; then
  fail "No Phase III scenario results found in ${RESULTS_DIR}"
  info "Run examples/run_phase3_scenarios.sh first"
  exit 1
fi

info "Found Phase III results: $(basename "${LATEST_JSON}")"

python3 - <<PY
import json
import sys

with open("${LATEST_JSON}") as f:
    results = json.load(f)

print(f"\nChecking Scenario 1: Congestion Attack...")
s1 = results.get("scenario_1_congestion_attack", {})
if s1.get("result") == "PASS":
    print("  \033[0;32m[OK]\033[0m  Congestion Attack PASSED")
else:
    print("  \033[0;31m[FAIL]\033[0m Congestion Attack FAILED or PARTIAL")
    sys.exit(1)

print(f"\nChecking Scenario 2: Security Breach...")
s2 = results.get("scenario_2_security_breach", {})
if s2.get("result") == "PASS":
    print("  \033[0;32m[OK]\033[0m  Security Breach PASSED")
else:
    print("  \033[0;31m[FAIL]\033[0m Security Breach FAILED or PARTIAL")
    sys.exit(1)

print(f"\nChecking Scenario 3: Cloud Load Balancing...")
s3 = results.get("scenario_3_cloud_load_balancing", {})
if s3.get("result") == "PASS":
    print("  \033[0;32m[OK]\033[0m  Cloud Load Balancing PASSED")
else:
    print("  \033[0;31m[FAIL]\033[0m Cloud Load Balancing FAILED or PARTIAL")
    sys.exit(1)

print("\n\033[0;32m[OK]\033[0m All Phase III Scenarios Passed!")
PY

if [[ $? -eq 0 ]]; then
  echo "==========================================================="
  echo -e "${_GREEN}Phase III Verification Successful${_NC}"
  echo "==========================================================="
else
  echo "==========================================================="
  echo -e "${_RED}Phase III Verification Failed${_NC}"
  echo "==========================================================="
  exit 1
fi
