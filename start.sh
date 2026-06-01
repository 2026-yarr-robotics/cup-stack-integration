#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-https://yarr-api-31.simplyimg.com/api/robot/skill/pyramid}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
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

launch fake_aggregator python3 scripts/fake_aggregator_node.py
launch fake_digital_twin python3 scripts/fake_digital_twin_node.py
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
  -p dry_run:="${DRY_RUN}"

wait
