#!/usr/bin/env bash
set -euo pipefail

DRY_RUN=true
# Robot control API base. Default to localhost (nginx :80 → robot:8001) so the
# robot pick/place/move calls do NOT traverse the Cloudflare tunnel, which times
# out at ~60s (HTTP 504/530) on long arm motions. Override with the public host
# only if running off-box: ROBOT_API_BASE=https://yarr-api-31.simplyimg.com.
ROBOT_API_BASE="${ROBOT_API_BASE:-http://localhost}"
API_URL="${API_URL:-${ROBOT_API_BASE}/api/robot/move}"
API_TIMEOUT_S="${API_TIMEOUT_S:-180.0}"
# (Post-place arm retreat to HOME is handled server-side in /skill/pyramid_step
#  via try_move_home — no client-side home move needed here.)
MOVE_Z="${MOVE_Z:-0.45}"
# Fallen-cup recovery (LLM interrupt): plan_executor POSTs the server's async
# task API {ROBOT_API_BASE}/api/robot/fallen-cup/recovery and polls
# /api/robot/status until the task finishes. mode=place stands the cup at a
# clear spot so it becomes a pick candidate again.
RECOVERY_MODE="${RECOVERY_MODE:-place}"
RECOVERY_TIMEOUT_S="${RECOVERY_TIMEOUT_S:-240.0}"
# GSP world-freeze ceiling. Must exceed the longest single action — the
# recovery task (sense+pick+stand+home) can run past the old 60s default.
FREEZE_TIMEOUT_S="${FREEZE_TIMEOUT_S:-240.0}"
EXO_XY_ERROR_M="${EXO_XY_ERROR_M:-0.02}"
MODEL="${MODEL:-qwen3.6:35b}"
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434/api/chat}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-${OLLAMA_URL%/api/chat}}"
OLLAMA_BASE_URL="${OLLAMA_BASE_URL%/}"
OLLAMA_CONNECT_TIMEOUT_S="${OLLAMA_CONNECT_TIMEOUT_S:-45}"
START_OLLAMA_IF_NEEDED="${START_OLLAMA_IF_NEEDED:-true}"
OLLAMA_REQUIRE_MODEL="${OLLAMA_REQUIRE_MODEL:-true}"
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
STABILIZE_METHOD="${STABILIZE_METHOD:-ema}"
STABILIZE_WINDOW_S="${STABILIZE_WINDOW_S:-1.0}"
# Hold a track this long after its last fresh detection so a brief boxes
# dropout (e.g. exo depth stutter) doesn't flush all tracks → tracked=0.
STABILIZE_TRACK_TIMEOUT_S="${STABILIZE_TRACK_TIMEOUT_S:-30.0}"
STABILIZE_MERGE_DISTANCE_M="${STABILIZE_MERGE_DISTANCE_M:-0.055}"
# Hand-eye source for pick_node's fine pick (/hand_eye/boxes):
#   real = upright_cup_pose_node (real hand camera YOLO-seg -> base_link /tf)
#   fake = fake_hand_eye_node (GT). Revert with: HAND_EYE_MODE=fake ./start.sh
HAND_EYE_MODE="${HAND_EYE_MODE:-real}"
HAND_EYE_WEIGHTS="${HAND_EYE_WEIGHTS:-/home/ssu/cup-stack-integration/ros2-depth-point-cloude/vision/yolo/speedstack3class_yolo26s_seg_1280_epoch250_3class_lightaug_geom1_redp25_sm_a100_best.pt}"
HAND_EYE_CALIB="${HAND_EYE_CALIB:-/home/ssu/cup-stack-integration/fallen-cup-recovery/dsr_practice/config/T_gripper2camera.npy}"
HAND_EYE_DEVICE="${HAND_EYE_DEVICE:-cuda}"
# pick point 선택 방식. centroid = 검증된 기본 (마스크 무게중심 — 솔리드
# 영역이라 depth 가 항상 유효). top_ellipse(hand_pick 최신)는 pick 픽셀이
# 컵 윗면 구멍/림 엣지에 앉아 depth invalid → base 변환 탈락이 잦았음
# (20260612_184748 런: cups=4 base=2 — 조준 컵이 후보에서 빠져 뒷줄 오픽).
# depth 샘플링을 마스크 솔리드 영역으로 보완하기 전까지 centroid 유지.
HAND_EYE_PICK_METHOD="${HAND_EYE_PICK_METHOD:-centroid}"
# 색 tie-break: coarse 타깃 최근접 +4cm 윈도 안에서 step color 우선
# (pick_node filter_by_color). 후보가 2mm 차이로 갈릴 때 색이 결정.
PICK_FILTER_BY_COLOR="${PICK_FILTER_BY_COLOR:-true}"
# base_link 시간 평활/트래킹(CupTracker). outlier 무시(연속 4프레임 재획득)가
# 이동 직후 정확한 측정을 ~0.7s 거부할 수 있음 — pick_node 의 ignore 0.3s +
# settle 0.5s 와 아슬아슬하게 겹친다. stale 좌표 픽이 재발하면 false 로.
HAND_EYE_SMOOTHING="${HAND_EYE_SMOOTHING:-true}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d_%H%M%S)}"
LOG_DIR="${LOG_DIR:-logs/${RUN_ID}}"

if [[ "${1:-}" == "--real-api" ]]; then
  DRY_RUN=false
fi

CLEANUP_STALE_AGENT_PROCESSES="${CLEANUP_STALE_AGENT_PROCESSES:-true}"

cleanup_stale_agent_processes() {
  if [[ "${CLEANUP_STALE_AGENT_PROCESSES}" != "true" ]]; then
    echo "[start.sh] stale agent cleanup disabled"
    return
  fi

  local patterns=(
    "bash ./start.sh"
    "bash start.sh"
    "bash .*/cup_stack_agent/start.sh"
    "python3 scripts/fake_aggregator_node.py"
    "python3 scripts/aggregator_node.py"
    "python3 scripts/fake_digital_twin_node.py"
    "python3 scripts/digital_twin_stabilizer_node.py"
    "python3 scripts/fake_hand_eye_node.py"
    "python3 scripts/upright_cup_pose_node.py"
    "python3 scripts/goal_state_publisher_node.py"
    "python3 scripts/topic_logger_node.py"
    "python3 scripts/llm_node.py"
    "python3 scripts/plan_executor_node.py"
    "python3 scripts/pick_node.py"
    "tee -a logs/.*/aggregator.log"
    "tee -a logs/.*/digital_twin_stabilizer.log"
    "tee -a logs/.*/fake_hand_eye.log"
    "tee -a logs/.*/upright_cup_pose.log"
    "tee -a logs/.*/goal_state_publisher.log"
    "tee -a logs/.*/topic_logger.log"
    "tee -a logs/.*/llm_node.log"
    "tee -a logs/.*/plan_executor.log"
    "tee -a logs/.*/pick_node.log"
  )
  local pids=()
  local pid pattern
  for pattern in "${patterns[@]}"; do
    while IFS= read -r pid; do
      [[ -z "${pid}" || "${pid}" == "$$" ]] && continue
      pids+=("${pid}")
    done < <(pgrep -f "${pattern}" 2>/dev/null || true)
  done

  if [[ ${#pids[@]} -eq 0 ]]; then
    echo "[start.sh] no stale agent processes found"
    return
  fi

  mapfile -t pids < <(printf "%s\n" "${pids[@]}" | sort -u)
  echo "[start.sh] stopping stale agent processes: ${pids[*]}"
  kill "${pids[@]}" 2>/dev/null || true
  sleep 1

  local survivors=()
  for pid in "${pids[@]}"; do
    if kill -0 "${pid}" 2>/dev/null; then
      survivors+=("${pid}")
    fi
  done
  if [[ ${#survivors[@]} -gt 0 ]]; then
    echo "[start.sh] force-stopping stale agent processes: ${survivors[*]}"
    kill -9 "${survivors[@]}" 2>/dev/null || true
  fi
}

cleanup_stale_agent_processes


ollama_tags_url() {
  printf '%s/api/tags' "${OLLAMA_BASE_URL}"
}

ollama_ready() {
  curl -fsS --max-time 2 "$(ollama_tags_url)" >/dev/null 2>&1
}

start_ollama_if_needed() {
  if [[ "${START_OLLAMA_IF_NEEDED}" != "true" ]]; then
    return 1
  fi

  case "${OLLAMA_BASE_URL}" in
    http://localhost:*|http://127.0.0.1:*|http://0.0.0.0:*) ;;
    *)
      echo "[start.sh] Ollama auto-start skipped for non-local URL: ${OLLAMA_BASE_URL}"
      return 1
      ;;
  esac

  if command -v systemctl >/dev/null 2>&1; then
    if systemctl --user start ollama >/dev/null 2>&1; then
      echo "[start.sh] requested Ollama user service start"
      return 0
    fi
  fi

  if command -v ollama >/dev/null 2>&1; then
    echo "[start.sh] starting Ollama with: ollama serve"
    OLLAMA_HOST="${OLLAMA_HOST:-127.0.0.1:11434}" \
      ollama serve >>"${LOG_DIR}/ollama.log" 2>&1 &
    return 0
  fi

  return 1
}

ollama_model_available() {
  local tags
  tags="$(curl -fsS --max-time 5 "$(ollama_tags_url)")" || return 1
  MODEL="${MODEL}" python3 -c '
import json
import os
import sys
wanted = os.environ["MODEL"]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
names = [m.get("name", "") for m in data.get("models", [])]
accepted = set(names)
accepted.update(n.split(":", 1)[0] for n in names if n)
sys.exit(0 if wanted in accepted or wanted.split(":", 1)[0] in accepted else 2)
' <<<"${tags}"
}

ollama_available_models() {
  curl -fsS --max-time 5 "$(ollama_tags_url)" 2>/dev/null | python3 -c '
import json
import sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("unknown")
    sys.exit(0)
names = [m.get("name", "") for m in data.get("models", []) if m.get("name")]
print(", ".join(names) if names else "none")
'
}

wait_for_ollama() {
  echo "[start.sh] checking Ollama: ${OLLAMA_BASE_URL} model=${MODEL}"
  if ! command -v curl >/dev/null 2>&1; then
    echo "[start.sh] ERROR: curl is required to check Ollama" >&2
    exit 1
  fi

  if ! ollama_ready; then
    start_ollama_if_needed || true
  fi

  local deadline=$((SECONDS + OLLAMA_CONNECT_TIMEOUT_S))
  while ! ollama_ready; do
    if (( SECONDS >= deadline )); then
      echo "[start.sh] ERROR: Ollama did not respond at $(ollama_tags_url) within ${OLLAMA_CONNECT_TIMEOUT_S}s" >&2
      echo "[start.sh]        Start it manually or set OLLAMA_URL/OLLAMA_BASE_URL to a reachable server." >&2
      exit 1
    fi
    sleep 1
  done

  if [[ "${OLLAMA_REQUIRE_MODEL}" == "true" ]]; then
    if ! ollama_model_available; then
      echo "[start.sh] ERROR: Ollama is reachable, but model '${MODEL}' is not installed." >&2
      echo "[start.sh]        Available models: $(ollama_available_models)" >&2
      echo "[start.sh]        Install it with 'ollama pull ${MODEL}' or set MODEL=<available-model>." >&2
      exit 1
    fi
  fi

  echo "[start.sh] Ollama connected: ${OLLAMA_BASE_URL} (${MODEL})"
}

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
# Match the rest of the running stack (cameras/robot/vision run with
# ROS_LOCALHOST_ONLY=1 on domain 21); without these the agent lands on a
# different DDS config and cannot discover them.
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-21}"
export ROS_LOCALHOST_ONLY=1
echo "[start.sh] logs: ${LOG_DIR}"
wait_for_ollama

launch() {
  local name="$1"
  shift
  "$@" > >(tee -a "${LOG_DIR}/${name}.log") 2>&1 &
}

# file-only launch (no shared-console echo). For chatty nodes — e.g. the
# per-frame upright_cup_pose vision log — so they do not drown the
# LLM-interaction feed on the start.sh console. Full log still goes to file.
launch_quiet() {
  local name="$1"
  shift
  "$@" >> "${LOG_DIR}/${name}.log" 2>&1 &
}

# Real vision pipeline must be running in its own (sourced) workspace, with:
#   point_cloud_node  -> /digital_twin/boxes  (raw exo cup positions)
#                     -> /vision/cups_on_table (-r /cups_on_table:=/vision/cups_on_table)
#   verifier_node     -> /vision/stack         (-r /stack:=/vision/stack)
#                     -> /stack_track_ids
# aggregator relays the real world-state (/vision/*) to /cups_on_table, /stack.
launch aggregator python3 scripts/aggregator_node.py \
  --ros-args \
  -p user_command:="${USER_COMMAND}"
# digital_twin_stabilizer median-filters the real /digital_twin/boxes into
# /digital_twin/boxes_filtered (what plan_executor's coarse move reads).
launch digital_twin_stabilizer python3 scripts/digital_twin_stabilizer_node.py \
  --ros-args \
  -p method:="${STABILIZE_METHOD}" \
  -p window_s:="${STABILIZE_WINDOW_S}" \
  -p track_timeout_s:="${STABILIZE_TRACK_TIMEOUT_S}" \
  -p merge_distance_m:="${STABILIZE_MERGE_DISTANCE_M}"
# hand-eye source for pick_node fine pick (/hand_eye/boxes). HAND_EYE_MODE:
#   real = upright_cup_pose_node (hand cam YOLO-seg -> base_link via /tf FK)
#   fake = fake_hand_eye_node (GT)
if [[ "${HAND_EYE_MODE}" == "real" ]]; then
  launch_quiet upright_cup_pose python3 scripts/upright_cup_pose_node.py \
    --ros-args \
    -p weights_path:="${HAND_EYE_WEIGHTS}" \
    -p image_topic:=/hand/hand/color/image_raw \
    -p depth_topic:=/hand/hand/aligned_depth_to_color/image_raw \
    -p camera_info_topic:=/hand/hand/color/camera_info \
    -p calib_file:="${HAND_EYE_CALIB}" \
    -p calib_scale_mm_to_m:=true \
    -p device:="${HAND_EYE_DEVICE}" \
    -p imgsz:=1280 \
    -p conf:=0.35 \
    -p base_frame:=base_link \
    -p target_class_name:=upright-cup \
    -p pick_point_method:="${HAND_EYE_PICK_METHOD}" \
    -p enable_temporal_smoothing:="${HAND_EYE_SMOOTHING}"
else
  launch fake_hand_eye python3 scripts/fake_hand_eye_node.py \
    --ros-args \
    -p disturbance_enabled:="${DISTURBANCE_ENABLED}" \
    -p disturbance_trigger_slot:="${DISTURBANCE_TRIGGER_SLOT}" \
    -p disturbance_removed_slot:="${DISTURBANCE_REMOVED_SLOT}"
fi
launch goal_state_publisher python3 scripts/goal_state_publisher_node.py \
  --ros-args \
  -p freeze_timeout_s:="${FREEZE_TIMEOUT_S}"
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
  -p api_base_robot:="${ROBOT_API_BASE}" \
  -p recovery_mode:="${RECOVERY_MODE}" \
  -p recovery_timeout_s:="${RECOVERY_TIMEOUT_S}" \
  -p dry_run:="${DRY_RUN}"

# pick_node closes the loop (/move_result -> hand-eye fine pick ->
# /api/robot/skill/pyramid -> /action_result). It has NO dry-run and always
# POSTs the real pyramid API, so only launch it in --real-api mode; otherwise a
# dry run would drive the real robot.
if [[ "${DRY_RUN}" == "false" ]]; then
  launch pick_node python3 scripts/pick_node.py \
    --ros-args \
    -p api_base:="${ROBOT_API_BASE}" \
    -p api_timeout_sec:="${API_TIMEOUT_S}" \
    -p filter_by_color:="${PICK_FILTER_BY_COLOR}"
else
  echo "[start.sh] dry-run: pick_node NOT launched (no dry-run; would POST the" \
       "real pyramid API). Loop stays open. Use --real-api to close it."
fi

wait
