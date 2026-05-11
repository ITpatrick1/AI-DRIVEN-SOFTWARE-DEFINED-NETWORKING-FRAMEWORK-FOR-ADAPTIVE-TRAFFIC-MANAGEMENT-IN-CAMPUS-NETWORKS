#!/usr/bin/env bash
# Partial Campus SDN launcher - starts components that don't need sudo.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_PATH="${HOME}/sdn-env"
ML_MODE="stub"

export CAMPUS_METRICS_FILE="/tmp/sdn_patrick/campus_metrics.json"
export CAMPUS_EVENTS_FILE="/tmp/sdn_patrick/campus_policy_events.jsonl"
export CAMPUS_ML_ACTION_FILE="/tmp/sdn_patrick/campus_ml_action.json"
export CAMPUS_DQN_MODEL_FILE="/tmp/sdn_patrick/campus_dqn_model.pt"
export CAMPUS_RYU_WSAPI_HOST="127.0.0.1"
export CAMPUS_RYU_WSAPI_PORT="8081"
export CAMPUS_DASHBOARD_HOST="0.0.0.0"
export CAMPUS_DASHBOARD_PORT="8080"
export CAMPUS_RUNTIME_API_HOST="127.0.0.1"
export CAMPUS_RUNTIME_API_PORT="9091"

mkdir -p /tmp/sdn_patrick

source "${VENV_PATH}/bin/activate"

echo "[1/4] Starting controller..."
nohup ryu-manager \
  --wsapi-host "${CAMPUS_RYU_WSAPI_HOST}" \
  --wsapi-port "${CAMPUS_RYU_WSAPI_PORT}" \
  examples/campus_controller.py ryu.app.ofctl_rest \
  >/tmp/sdn_patrick/ryu_campus.log 2>&1 < /dev/null &

echo "[2/4] Starting dashboard..."
nohup python3 examples/campus_dashboard.py --host "${CAMPUS_DASHBOARD_HOST}" --port "${CAMPUS_DASHBOARD_PORT}" \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --events-file "${CAMPUS_EVENTS_FILE}" \
  --runtime-api-base "http://${CAMPUS_RUNTIME_API_HOST}:${CAMPUS_RUNTIME_API_PORT}" \
  >/tmp/sdn_patrick/campus_dashboard.log 2>&1 < /dev/null &

echo "[3/4] Starting ML policy stub..."
nohup python3 examples/ml_policy_stub.py \
  --metrics-file "${CAMPUS_METRICS_FILE}" \
  --action-file "${CAMPUS_ML_ACTION_FILE}" >/tmp/sdn_patrick/campus_ml_stub.log 2>&1 < /dev/null &

echo "[4/4] Starting timetable engine..."
pkill -f "examples/timetable_engine.py" >/dev/null 2>&1 || true
nohup python3 examples/timetable_engine.py \
  --db "/tmp/sdn_patrick/campus_timetable.db" \
  --state "/tmp/sdn_patrick/campus_timetable_state.json" \
  --port 9092 \
  >/tmp/sdn_patrick/campus_timetable.log 2>&1 < /dev/null &

echo "Partial system started (Dashboard, Controller, ML, Timetable)."
echo "Topology (Mininet) requires sudo privileges."
