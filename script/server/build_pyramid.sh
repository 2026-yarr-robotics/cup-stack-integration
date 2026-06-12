#!/usr/bin/env bash
#
# build_pyramid.sh — 3-2-1 컵 피라미드 빌드 시퀀스
#
# 2x3 격자(6개 픽 좌표)의 각 컵에 대해:
#   1) POST /api/robot/move  로 EE 를 픽 좌표(z=0.45 고정)로 이동
#   2) move 가 HTTP 200 이면 POST /api/robot/skill/pyramid 로 해당 슬롯에 적재
#
# 슬롯 순서: 1l → 1m → 1r → 2l → 2r → 3m (3-2-1 피라미드)
#
# 격자 좌표:
#   x: 0.250, 0.350   (2개)
#   y: -0.2, 0.0, 0.2 (3개)
#   z: 0.45 고정
#
# 사용법:
#   ./build_pyramid.sh                # 기본 baseurl 사용
#   BASE_URL=https://other.host ./build_pyramid.sh
set -euo pipefail

BASE_URL="${BASE_URL:-https://yarr-api-31.simplyimg.com}"
MOVE_Z="0.45"
MAX_RETRY="${MAX_RETRY:-5}"      # 200 이 아닐 때 재시도 횟수 (로봇 모션 타임아웃·터널 blip 대응)
RETRY_DELAY="${RETRY_DELAY:-3}"  # 재시도 간 대기(초)

# 픽 좌표(x y) 와 피라미드 슬롯을 1:1 매핑. 6번 호출.
#   격자: x ∈ {0.250, 0.350}, y ∈ {-0.2, 0.0, 0.2}
PICKS=(
  "0.250 -0.2 1l"
  "0.250  0.0 1m"
  "0.250  0.2 1r"
  "0.350 -0.2 2l"
  "0.350  0.0 2r"
  "0.350  0.2 3m"
)

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
echo "피라미드 빌드 시작 (6 컵, z=${MOVE_Z} 고정)"
echo

i=0
for entry in "${PICKS[@]}"; do
  i=$((i + 1))
  # shellcheck disable=SC2086
  set -- $entry
  x="$1"; y="$2"; slot="$3"

  echo "[$i/6] slot=${slot}  pick=(x=${x}, y=${y}, z=${MOVE_Z})"

  echo "  move ..."
  if ! post_json "/api/robot/move" \
       "{\"x\": ${x}, \"y\": ${y}, \"z\": ${MOVE_Z}, \"mode\": \"absolute\"}"; then
    echo "  ✗ move 가 200 이 아님 — 시퀀스 중단" >&2
    exit 1
  fi

  echo "  pyramid (slot=${slot}) ..."
  if ! post_json "/api/robot/skill/pyramid" \
       "{\"x\": ${x}, \"y\": ${y}, \"slot\": \"${slot}\"}"; then
    echo "  ✗ pyramid 스킬 실패 — 시퀀스 중단" >&2
    exit 1
  fi

  echo "  ✓ slot=${slot} 완료"
  echo
done

echo "피라미드 빌드 완료 (6/6)"
