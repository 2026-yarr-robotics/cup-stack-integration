#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-http://localhost:8000/api/robot/skill/pyramid}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"

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

python3 scripts/fake_aggregator_node.py &
python3 scripts/fake_digital_twin_node.py &
python3 scripts/goal_state_publisher_node.py &
python3 scripts/llm_node.py \
  --ros-args \
  -p model:="${MODEL}" \
  -p ollama_url:="${OLLAMA_URL}" &
python3 scripts/plan_executor_node.py \
  --ros-args \
  -p api_url_pyramid:="${API_URL}" \
  -p dry_run:="${DRY_RUN}" &

wait
