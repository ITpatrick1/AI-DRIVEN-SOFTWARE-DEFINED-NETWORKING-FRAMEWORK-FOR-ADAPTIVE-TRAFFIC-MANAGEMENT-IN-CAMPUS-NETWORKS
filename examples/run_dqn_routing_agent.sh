#!/usr/bin/env bash
set -euo pipefail

# Stage 8 helper:
# Run the DQN adaptive routing module as a standalone process.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${VENV_PATH:-${HOME}/sdn-env}"

if [[ ! -f "${VENV_PATH}/bin/activate" ]]; then
  echo "[FAIL] Virtualenv not found at ${VENV_PATH}"
  exit 1
fi

cd "${REPO_ROOT}"
source "${VENV_PATH}/bin/activate"

if ! python3 - <<'PY' >/dev/null 2>&1; then
import torch  # noqa: F401
PY
  echo "[FAIL] PyTorch is not installed in ${VENV_PATH}."
  echo "Install while online:"
  echo "  examples/prepare_offline_bundle.sh"
  exit 1
fi

METRICS_FILE="${CAMPUS_METRICS_FILE:-/tmp/campus_metrics.json}"
ACTION_FILE="${CAMPUS_ML_ACTION_FILE:-/tmp/campus_ml_action.json}"
MODEL_FILE="${CAMPUS_DQN_MODEL_FILE:-/tmp/campus_dqn_model.pt}"

echo "Starting DQN routing agent"
echo "  metrics: ${METRICS_FILE}"
echo "  action : ${ACTION_FILE}"
echo "  model  : ${MODEL_FILE}"

exec python3 examples/dqn_routing_agent.py \
  --metrics-file "${METRICS_FILE}" \
  --action-file "${ACTION_FILE}" \
  --model-file "${MODEL_FILE}" \
  "$@"

