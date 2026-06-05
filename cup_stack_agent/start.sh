#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-https://yarr-api-31.simplyimg.com/api/robot/skill/pyramid}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"
SKILL_STATUS_URL="${SKILL_STATUS_URL:-http://localhost:8765/status}"
SKILL_IDLE_TIMEOUT_S="${SKILL_IDLE_TIMEOUT_S:-10.0}"
SKILL_IDLE_POLL_S="${SKILL_IDLE_POLL_S:-0.2}"
SKILL_STATUS_TIMEOUT_S="${SKILL_STATUS_TIMEOUT_S:-1.0}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
# Operator command + x,y stabilizer tuning.
USER_COMMAND="${USER_COMMAND:-3단 피라미드 쌓아줘}"
STABILIZE_METHOD="${STABILIZE_METHOD:-median}"
STABILIZE_WINDOW_S="${STABILIZE_WINDOW_S:-1.0}"
STABILIZE_TRACK_TIMEOUT_S="${STABILIZE_TRACK_TIMEOUT_S:-1.0}"
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

# NOTE: the real vision pipeline must be running in its own (sourced) workspace,
# with these topic remaps so the aggregator can sit in front of goal_state_publisher:
#   point_cloud_node  -r /cups_on_table:=/vision/cups_on_table   (+ /digital_twin/boxes)
#   verifier_node     -r /stack:=/vision/stack                   (+ /stack_track_ids)
# The two glue nodes below live in this repo.

# aggregator: relay real vision world-state (/vision/cups_on_table, /vision/stack)
# to /cups_on_table, /stack for goal_state_publisher, and publish /user_command.
launch aggregator python3 scripts/fake_aggregator_node.py \
  --ros-args \
  -p user_command:="${USER_COMMAND}"
# x,y stabilizer: median/mean-filters the raw /digital_twin/boxes and republishes
# /digital_twin/boxes_filtered (what plan_executor consumes).
launch digital_twin_stabilizer python3 scripts/fake_digital_twin_node.py \
  --ros-args \
  -p method:="${STABILIZE_METHOD}" \
  -p window_s:="${STABILIZE_WINDOW_S}" \
  -p track_timeout_s:="${STABILIZE_TRACK_TIMEOUT_S}"
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
  -p api_url_pyramid:="${API_URL}" \
  -p api_timeout_s:="${API_TIMEOUT_S}" \
  -p skill_status_url:="${SKILL_STATUS_URL}" \
  -p skill_idle_timeout_s:="${SKILL_IDLE_TIMEOUT_S}" \
  -p skill_idle_poll_s:="${SKILL_IDLE_POLL_S}" \
  -p skill_status_timeout_s:="${SKILL_STATUS_TIMEOUT_S}" \
  -p dry_run:="${DRY_RUN}"

wait
