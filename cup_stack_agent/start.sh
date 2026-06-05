#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-https://yarr-api-31.simplyimg.com/api/robot/move}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"
MOVE_Z="${MOVE_Z:-0.45}"
EXO_XY_ERROR_M="${EXO_XY_ERROR_M:-0.02}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
DISTURBANCE_ENABLED="${DISTURBANCE_ENABLED:-true}"
DISTURBANCE_TRIGGER_SLOT="${DISTURBANCE_TRIGGER_SLOT:-L2_right}"
DISTURBANCE_REMOVED_SLOT="${DISTURBANCE_REMOVED_SLOT:-L2_left}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
LOG_DIR="${LOG_DIR:-logs/${RUN_ID}}"

if [[ "${1:-}" == "--real-api" ]]; then
  DRY_RUN=false
fi

cleanup() {
  local pids
  pids="$(jobs -p)"
  if [[ -n "${pids}" ]]; then
    kill ${pids} 2>/dev/null || true
  fi
}
trap cleanup EXIT

mkdir -p "${LOG_DIR}"
export PYTHONUNBUFFERED=1
echo "[start.sh] logs: ${LOG_DIR}"

launch() {
  local name="$1"
  shift
  "$@" > >(tee -a "${LOG_DIR}/${name}.log") 2>&1 &
}

launch fake_aggregator python3 scripts/fake_aggregator_node.py \
  --ros-args \
  -p disturbance_enabled:="${DISTURBANCE_ENABLED}" \
  -p disturbance_trigger_slot:="${DISTURBANCE_TRIGGER_SLOT}" \
  -p disturbance_removed_slot:="${DISTURBANCE_REMOVED_SLOT}"
launch fake_digital_twin python3 scripts/fake_digital_twin_node.py \
  --ros-args \
  -p exo_xy_error_m:="${EXO_XY_ERROR_M}" \
  -p disturbance_enabled:="${DISTURBANCE_ENABLED}" \
  -p disturbance_trigger_slot:="${DISTURBANCE_TRIGGER_SLOT}" \
  -p disturbance_removed_slot:="${DISTURBANCE_REMOVED_SLOT}"
launch fake_hand_eye python3 scripts/fake_hand_eye_node.py \
  --ros-args \
  -p disturbance_enabled:="${DISTURBANCE_ENABLED}" \
  -p disturbance_trigger_slot:="${DISTURBANCE_TRIGGER_SLOT}" \
  -p disturbance_removed_slot:="${DISTURBANCE_REMOVED_SLOT}"
launch goal_state_publisher python3 scripts/goal_state_publisher_node.py
launch topic_logger python3 scripts/topic_logger_node.py \
  --ros-args \
  -p log_dir:="${LOG_DIR}"
launch llm_node python3 scripts/llm_node.py \
  --ros-args \
  -p model:="${MODEL}" \
  -p ollama_url:="${OLLAMA_URL}"
launch plan_executor python3 scripts/plan_executor_node.py \
  --ros-args \
  -p api_url_move:="${API_URL}" \
  -p api_timeout_s:="${API_TIMEOUT_S}" \
  -p move_z:="${MOVE_Z}" \
  -p dry_run:="${DRY_RUN}"

wait
