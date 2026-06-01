#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
API_URL="${API_URL:-http://localhost:8000/api/robot/skill/pyramid}"

if [[ "${1:-}" == "--real-api" ]]; then
  DRY_RUN=false
fi

if [[ -z "${FAKE_XY_BY_SLOT_JSON:-}" ]]; then
  echo "FAKE_XY_BY_SLOT_JSON is required, e.g. '{\"L1_left\":[x,y]}'" >&2
  exit 2
fi

python3 scripts/temp_plan_executor_node.py \
  --ros-args \
  -p api_url_pyramid:="${API_URL}" \
  -p dry_run:="${DRY_RUN}" \
  -p fake_xy_by_slot_json:="${FAKE_XY_BY_SLOT_JSON}"
