#!/usr/bin/env bash
# Manual operator GUI for YARR ros2-skill-manager.
# NOT for use during an automated LLM closed-loop (./start.sh --real-api):
# GUI skill commands collide with plan_executor/pick_node on the real robot.
#
# The node default api root is the PUBLIC host (https://yarr-api.simplyimg.com),
# which Cloudflare times out (~60s) on long robot motions. We override to
# localhost. api_url_{pick,pyramid,update_input} are NODE params (not launch
# args), so they are passed via `ros2 run ... --ros-args -p`.
set -euo pipefail

if [[ -z "${DISPLAY:-}" ]]; then
  echo "[skill-manager] DISPLAY is not set; tkinter GUI cannot start." >&2
  exit 1
fi

ROBOT_API_BASE="${ROBOT_API_BASE:-http://localhost}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"

source /opt/ros/humble/setup.bash
source "$HOME/cup-stack-integration/ros2-depth-point-cloude/install/setup.bash"
source "$HOME/cup-stack-integration/vision-node/install/setup.bash"
source "$HOME/cup-stack-integration/tools/ros2-skill-manager/install/setup.bash"

echo "[skill-manager] api base=${ROBOT_API_BASE}  timeout=${API_TIMEOUT_S}s"
echo "[skill-manager] WARNING: do NOT press Pick/Pyramid while ./start.sh --real-api is running."

exec ros2 run skill_manager skill_manager --ros-args \
  -p api_url_pick:="${ROBOT_API_BASE}/api/robot/skill/pick" \
  -p api_url_pyramid:="${ROBOT_API_BASE}/api/robot/skill/pyramid" \
  -p api_url_update_input:="${ROBOT_API_BASE}/api/robot/config/pyramid" \
  -p api_timeout_s:="${API_TIMEOUT_S}"
