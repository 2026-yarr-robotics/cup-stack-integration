#!/usr/bin/env bash
#
# unstack.sh — 3-2-1 컵 피라미드 해체 시퀀스 (build_pyramid.sh 의 역동작)
#
# build_pyramid.sh 로 쌓은 피라미드의 컵을 위에서부터 하나씩 집어,
# 입력받은 목적지 (DEST_X, DEST_Y) 에 nested 컬럼으로 하나의 스택으로 쌓는다.
#
# 슬롯별 pick 좌표(피라미드 절대 위치)와 pick_z 는 서버가 /config/pyramid
# 캐시에서 자동으로 가져오므로 여기서는 슬롯 키만 넘긴다.
#
# 해체 순서(위 → 아래, 필수):
#   3m → 2r → 2l → 1r → 1m → 1l
# 매 컵마다 목적지 컬럼 높이 nested 를 1..6 으로 증가시켜 위로 nesting 한다.
#   place_z = pick_z + (nested-1) * nest_inc
#
# 사용법:
#   ./unstack.sh                       # 기본 목적지 (DEST_X, DEST_Y)
#   ./unstack.sh 0.40 0.10             # 목적지 x y 를 인자로 지정
#   DEST_X=0.40 DEST_Y=0.10 ./unstack.sh
#   BASE_URL=https://other.host ./unstack.sh
set -euo pipefail

BASE_URL="${BASE_URL:-https://yarr-api-31.simplyimg.com}"
DEST_X="${1:-${DEST_X:-0.400}}"   # 목적지 nest 중앙 X (base_link, m)
DEST_Y="${2:-${DEST_Y:-0.100}}"   # 목적지 nest 중앙 Y (base_link, m)
MAX_RETRY="${MAX_RETRY:-5}"       # 200 이 아닐 때 재시도 횟수 (로봇 모션 타임아웃·터널 blip 대응)
RETRY_DELAY="${RETRY_DELAY:-3}"   # 재시도 간 대기(초)

# 해체할 슬롯 순서(위에서부터). nested 는 목적지 컬럼 높이로 1..6 증가.
SLOTS=(3m 2r 2l 1r 1m 1l)

# curl 한 번 호출: 응답 본문 + HTTP 상태코드를 함께 받아 출력. 200 이면 0 반환.
post_json_once() {
  local path="$1" body="$2" resp http_code payload
  resp="$(curl -sS -w $'\n%{http_code}' \
    -X POST "${BASE_URL}${path}" \
    -H 'Content-Type: application/json' \
    -d "${body}")"
  http_code="${resp##*$'\n'}"
  payload="${resp%$'\n'*}"
  echo "  → HTTP ${http_code}: ${payload}"
  [ "${http_code}" = "200" ]
}

# 200 을 받을 때까지 최대 MAX_RETRY 회 재시도.
# 로봇 모션 서비스 타임아웃(409)·Cloudflare 터널 blip(530) 은 일시적이므로 재시도한다.
post_json() {
  local path="$1" body="$2" attempt
  for ((attempt = 1; attempt <= MAX_RETRY; attempt++)); do
    if post_json_once "${path}" "${body}"; then
      return 0
    fi
    if ((attempt < MAX_RETRY)); then
      echo "    재시도 ${attempt}/${MAX_RETRY} (${RETRY_DELAY}s 후) ..."
      sleep "${RETRY_DELAY}"
    fi
  done
  return 1
}

echo "Base URL: ${BASE_URL}"
echo "피라미드 해체 시작 (6 컵 → 목적지 nest (x=${DEST_X}, y=${DEST_Y}))"
echo

i=0
for slot in "${SLOTS[@]}"; do
  i=$((i + 1))
  nested="${i}"   # 목적지 컬럼 높이: 1번째 컵=1 ... 6번째 컵=6

  echo "[$i/6] slot=${slot} → nest(x=${DEST_X}, y=${DEST_Y}, nested=${nested})"

  echo "  unstack (slot=${slot}, nested=${nested}) ..."
  if ! post_json "/api/robot/skill/unstack" \
       "{\"slot\": \"${slot}\", \"x\": ${DEST_X}, \"y\": ${DEST_Y}, \"nested\": ${nested}}"; then
    echo "  ✗ unstack 스킬 실패 — 시퀀스 중단" >&2
    exit 1
  fi

  echo "  ✓ slot=${slot} 완료 (nested=${nested})"
  echo
done

echo "피라미드 해체 완료 (6/6) → nest (x=${DEST_X}, y=${DEST_Y})"
