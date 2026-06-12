#!/usr/bin/env bash
# send_command.sh — 대시보드 명령창과 동일하게 자연어 명령을 LLM 에이전트로 보낸다.
#
#   POST /api/robot/command {"text": "<명령>"}
#     -> rosbridge -> /user_command (std_msgs/String)
#     -> goal_state_publisher -> llm_node(Ollama) -> 로봇 스킬
#
# 통합 start.sh 는 'agent 준비 환경'만 띄우고 명령은 자동 발행하지 않으므로, 이
# 스크립트(또는 대시보드 버튼/명령창)로 명령을 보내야 동작한다.
#
# 사용법:
#   ./send_command.sh                 # 기본 "3단 쌓아줘" 전송
#   ./send_command.sh "2단 쌓아줘"     # 다른 명령 전송
#   ROBOT_API_BASE=https://yarr-api-31.simplyimg.com ./send_command.sh   # off-box
set -euo pipefail

# 로봇 API base. 기본 localhost(nginx :80 -> robot:8001) — Cloudflare 터널을 타지
# 않아 긴 동작에도 504/530 타임아웃이 없다.
ROBOT_API_BASE="${ROBOT_API_BASE:-http://localhost}"
CMD="${1:-3단 쌓아줘}"
URL="${ROBOT_API_BASE%/}/api/robot/command"

# 한글/따옴표 안전하게 JSON 인코딩 (python3 사용).
BODY="$(CMD="$CMD" python3 -c 'import json, os; print(json.dumps({"text": os.environ["CMD"]}))')"

echo "[send_command] POST $URL"
echo "[send_command] text: $CMD"
curl -fsS -X POST "$URL" \
  -H 'Content-Type: application/json' \
  --data "$BODY"
echo
