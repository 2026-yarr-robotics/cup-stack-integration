#!/usr/bin/env bash
#
# build_pyramid_nested.sh — 단일 nest 소스에서 3-2-1 컵 피라미드 빌드 시퀀스
#
# build_pyramid.sh 의 nested-source 버전. build_pyramid.sh 는 2x3 격자(6개 픽 좌표)에서
# 컵을 하나씩 집지만, 이 스크립트는 한 (x,y) 위치에 6개가 겹쳐진 nest 하나에서
# 서버의 `nested` 픽 파라미터로 위에서부터 한 개씩 집어 피라미드를 쌓는다.
#
# 슬롯 순서(아래 → 위, 3-2-1 피라미드): 1l → 1m → 1r → 2l → 2r → 3m
#
# nest 는 컵을 뺄수록 줄어든다. 첫 픽은 6겹 nest 의 맨 위(nested=6),
# 마지막 픽은 마지막 한 개(nested=1). 슬롯과 1:1 매핑:
#   1l→nested6, 1m→nested5, 1r→nested4, 2l→nested3, 2r→nested2, 3m→nested1
# 서버는 /config/pyramid 캐시에서 pick_z/center/degree 를 가져와
# 소스 top-cup pick_z = pick_z + (nested-1)*nest_inc 를 계산한다.
#
# 사용법:
#   ./build_pyramid_nested.sh                       # 기본 소스 (SRC_X, SRC_Y)
#   ./build_pyramid_nested.sh 0.250 0.0             # 소스 x y 를 인자로 지정
#   SRC_X=0.250 SRC_Y=0.0 ./build_pyramid_nested.sh
#   BASE_URL=https://other.host ./build_pyramid_nested.sh
set -euo pipefail

BASE_URL="${BASE_URL:-https://yarr-api-31.simplyimg.com}"
SRC_X="${1:-${SRC_X:-0.250}}"    # 소스 nest 중앙 X (base_link, m)
SRC_Y="${2:-${SRC_Y:-0.000}}"    # 소스 nest 중앙 Y (base_link, m)
MAX_RETRY="${MAX_RETRY:-5}"      # 200 이 아닐 때 재시도 횟수 (로봇 모션 타임아웃·터널 blip 대응)
RETRY_DELAY="${RETRY_DELAY:-3}"  # 재시도 간 대기(초)

# 피라미드 슬롯과 소스 nest 높이(nested)를 1:1 매핑. 6번 호출.
# 아래 → 위 순으로 쌓으면서 nest 는 6 → 1 로 줄어든다.
PICKS=(
  "1l 6"
  "1m 5"
  "1r 4"
  "2l 3"
  "2r 2"
  "3m 1"
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
echo "피라미드 빌드 시작 (6 컵, 소스 nest (x=${SRC_X}, y=${SRC_Y}))"
echo

i=0
for entry in "${PICKS[@]}"; do
  i=$((i + 1))
  # shellcheck disable=SC2086
  set -- $entry
  slot="$1"; nested="$2"

  echo "[$i/6] slot=${slot} ← nest(x=${SRC_X}, y=${SRC_Y}, nested=${nested})"

  echo "  pyramid (slot=${slot}, nested=${nested}) ..."
  if ! post_json "/api/robot/skill/pyramid" \
       "{\"x\": ${SRC_X}, \"y\": ${SRC_Y}, \"slot\": \"${slot}\", \"nested\": ${nested}}"; then
    echo "  ✗ pyramid 스킬 실패 — 시퀀스 중단" >&2
    exit 1
  fi

  echo "  ✓ slot=${slot} 완료 (nested=${nested})"
  echo
done

echo "피라미드 빌드 완료 (6/6) ← nest (x=${SRC_X}, y=${SRC_Y})"
