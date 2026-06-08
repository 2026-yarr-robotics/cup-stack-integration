#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-https://yarr-api-31.simplyimg.com/api/robot/move}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"
MOVE_Z="${MOVE_Z:-0.45}"
EXO_XY_ERROR_M="${EXO_XY_ERROR_M:-0.02}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
# Disturbance off for now — the experiment just verifies the happy-path loop.
# Robust disturbance recovery (continuous perception → GSP triggers LLM replan
# on world change) is deferred; the scenario code stays in the fake nodes.
# Re-enable for a run with: DISTURBANCE_ENABLED=true ./start.sh ...
DISTURBANCE_ENABLED="${DISTURBANCE_ENABLED:-false}"
DISTURBANCE_TRIGGER_SLOT="${DISTURBANCE_TRIGGER_SLOT:-L2_right}"
DISTURBANCE_REMOVED_SLOT="${DISTURBANCE_REMOVED_SLOT:-L2_left}"
# Real-vision integration: the exo view is now real perception, not GT.
# aggregator_node relays the real world state; digital_twin_stabilizer_node
# median-filters the real point_cloud_node boxes.
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

# Real vision pipeline must be running in its own (sourced) workspace, with:
#   point_cloud_node  -> /digital_twin/boxes  (raw exo cup positions)
#                     -> /vision/cups_on_table (-r /cups_on_table:=/vision/cups_on_table)
#   verifier_node     -> /vision/stack         (-r /stack:=/vision/stack)
#                     -> /stack_track_ids
# aggregator relays the real world-state (/vision/*) to /cups_on_table, /stack.
launch aggregator python3 scripts/fake_aggregator_node.py \
  --ros-args \
  -p user_command:="${USER_COMMAND}"
# digital_twin_stabilizer median-filters the real /digital_twin/boxes into
# /digital_twin/boxes_filtered (what plan_executor's coarse move reads).
launch digital_twin_stabilizer python3 scripts/fake_digital_twin_node.py \
  --ros-args \
  -p method:="${STABILIZE_METHOD}" \
  -p window_s:="${STABILIZE_WINDOW_S}" \
  -p track_timeout_s:="${STABILIZE_TRACK_TIMEOUT_S}"
# fake_hand_eye stays FAKE (GT /hand_eye/boxes) for pick_node's fine pick.
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

# pick_node closes the loop (/move_result -> hand-eye fine pick ->
# /api/robot/skill/pyramid -> /action_result). It has NO dry-run and always
# POSTs the real pyramid API, so only launch it in --real-api mode; otherwise a
# dry run would drive the real robot.
if [[ "${DRY_RUN}" == "false" ]]; then
  launch pick_node python3 scripts/pick_node.py \
    --ros-args \
    -p api_timeout_sec:="${API_TIMEOUT_S}"
else
  echo "[start.sh] dry-run: pick_node NOT launched (no dry-run; would POST the" \
       "real pyramid API). Loop stays open. Use --real-api to close it."
fi

wait
