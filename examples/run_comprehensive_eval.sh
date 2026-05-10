#!/usr/bin/env bash
set -euo pipefail

# Final Comprehensive Evaluation
# Validates the entire capstone project end-to-end and produces all required artifacts.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"
RESULTS_DIR="${RESULTS_DIR:-${REPO_ROOT}/results}"
TAG="${1:-final_$(date +%Y%m%d_%H%M%S)}"
PW="${SUDO_PASSWORD:-}"

_GREEN='\033[0;32m'
_CYAN='\033[0;36m'
_NC='\033[0m'

echo -e "${_CYAN}================================================================${_NC}"
echo -e "${_CYAN}   Tumba College - AI-Driven SDN Capstone Final Evaluation      ${_NC}"
echo -e "${_CYAN}================================================================${_NC}"

cd "${REPO_ROOT}"

# 1. Run Data Mining & Generate Phase I Report
echo -e "\n${_GREEN}[Step 1] Running Data Mining Analysis...${_NC}"
source "${VENV_PATH}/bin/activate"
python3 examples/stakeholder_requirements.py
python3 examples/data_mining.py
python3 examples/generate_phase1_report.py

# 2. Run Stage 11 (Throughput, Latency, Reroute Evaluation)
echo -e "\n${_GREEN}[Step 2] Running Stage 11 Network Performance Evaluation...${_NC}"
if [[ -n "${PW}" ]]; then
  SUDO_PASSWORD="${PW}" examples/run_stage11_evaluation.sh "${TAG}_perf"
else
  examples/run_stage11_evaluation.sh "${TAG}_perf"
fi

# 3. Run Phase III Stress Scenarios
echo -e "\n${_GREEN}[Step 3] Running Phase III Intelligent Stress Scenarios...${_NC}"
if [[ -n "${PW}" ]]; then
  SUDO_PASSWORD="${PW}" examples/run_phase3_scenarios.sh "${TAG}_phase3"
else
  examples/run_phase3_scenarios.sh "${TAG}_phase3"
fi

# 4. Verify Phase III Results
echo -e "\n${_GREEN}[Step 4] Verifying Phase III Scenario Results...${_NC}"
examples/verify_phase3_scenarios.sh

echo -e "\n${_GREEN}================================================================${_NC}"
echo -e "${_GREEN}   Comprehensive Evaluation Completed Successfully!             ${_NC}"
echo -e "${_GREEN}================================================================${_NC}"
echo "All validation artifacts and reports are in: ${RESULTS_DIR}"
echo "Check Phase_I_Analysis_Report.md for the complete analysis."
