#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SURVEY_CSV="${SURVEY_CSV:-${REPO_ROOT}/Stakeholder Requirement Survey Intelligent SDN Project .csv}"
LAUNCHER="${SCRIPT_DIR}/run_full_stack.sh"
PASS_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --survey-csv)
      shift
      if [[ $# -eq 0 ]]; then
        echo "Missing value for --survey-csv"
        exit 1
      fi
      SURVEY_CSV="$1"
      ;;
    --web-only)
      LAUNCHER="${SCRIPT_DIR}/run_web_only_stack.sh"
      ;;
    *)
      PASS_ARGS+=("$1")
      ;;
  esac
  shift
done

if [[ ! -f "${SURVEY_CSV}" ]]; then
  echo "Survey CSV not found: ${SURVEY_CSV}"
  exit 1
fi

export CAMPUS_STAKEHOLDER_REPORT_FILE="${CAMPUS_STAKEHOLDER_REPORT_FILE:-/tmp/campus_stakeholder_report.json}"
export CAMPUS_MANUAL_SETTINGS_FILE="${CAMPUS_MANUAL_SETTINGS_FILE:-/tmp/campus_manual_settings.json}"
export CAMPUS_SECURITY_POLICY_FILE="${CAMPUS_SECURITY_POLICY_FILE:-/tmp/campus_security_policy.json}"
export CAMPUS_DQN_POLICY_FILE="${CAMPUS_DQN_POLICY_FILE:-/tmp/campus_dqn_policy.json}"
export CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS="${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS:-1}"

python3 "${SCRIPT_DIR}/stakeholder_requirements.py" \
  --csv "${SURVEY_CSV}" \
  --report-json "${CAMPUS_STAKEHOLDER_REPORT_FILE}" \
  --manual-settings-file "${CAMPUS_MANUAL_SETTINGS_FILE}" \
  --security-policy-file "${CAMPUS_SECURITY_POLICY_FILE}" \
  --dqn-policy-file "${CAMPUS_DQN_POLICY_FILE}" \
  --output-dir "${REPO_ROOT}/results"

echo
echo "Stakeholder-driven SDN profile applied"
echo "  Survey CSV          : ${SURVEY_CSV}"
echo "  Stakeholder report  : ${CAMPUS_STAKEHOLDER_REPORT_FILE}"
echo "  Threshold policy    : ${CAMPUS_MANUAL_SETTINGS_FILE}"
echo "  Security policy     : ${CAMPUS_SECURITY_POLICY_FILE}"
echo "  DQN policy          : ${CAMPUS_DQN_POLICY_FILE}"
echo "  Smoke tests skipped : ${CAMPUS_SKIP_TOPOLOGY_SMOKE_TESTS}"
echo

exec "${LAUNCHER}" "${PASS_ARGS[@]}"
