#!/usr/bin/env bash
#
# unstack_grid.sh — 3-2-1 피라미드를 2x3 격자로 해체하는 시퀀스
#                   (build_pyramid.sh 의 격자 버전 역동작)
#
# build_pyramid.sh 로 쌓은 피라미드를 위에서부터 하나씩 집어,
# 각 컵을 2x3 격자의 제자리(원래 픽 좌표)에 단일 컵(nested=1)으로 내려놓는다.
# unstack.sh 와 달리 한 nest 로 모으지 않고, 컵마다 별도 격자 칸에 놓는다.
#
# 해체 순서(위 → 아래, 필수):
#   3m → 2r → 2l → 1r → 1m → 1l
#
# 격자 목적지(슬롯 → x y) = build_pyramid.sh PICKS 매핑의 역방향:
#   3m → (0.350,  0.2) ; 2r → (0.350,  0.0) ; 2l → (0.350, -0.2)
#   1r → (0.250,  0.2) ; 1m → (0.250,  0.0) ; 1l → (0.250, -0.2)
# 모든 컵 nested=1 (격자 칸마다 단일 컵, nested 컬럼 아님).
#
# 사용법:
#   ./unstack_grid.sh                  # 기본 baseurl 사용
#   BASE_URL=https://other.host ./unstack_grid.sh
set -euo pipefail

BASE_URL="${BASE_URL:-https://yarr-api-31.simplyimg.com}"
MAX_RETRY="${MAX_RETRY:-5}"      # 200 이 아닐 때 재시도 횟수 (로봇 모션 타임아웃·터널 blip 대응)
RETRY_DELAY="${RETRY_DELAY:-3}"  # 재시도 간 대기(초)

# 해체 슬롯(위 → 아래)과 격자 목적지(x y)를 1:1 매핑. 6번 호출.
#   build_pyramid.sh 격자 매핑의 역방향.
PLACES=(
  "3m 0.350  0.2"
  "2r 0.350  0.0"
  "2l 0.350 -0.2"
  "1r 0.250  0.2"
  "1m 0.250  0.0"
  "1l 0.250 -0.2"
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
echo "피라미드 해체 시작 (6 컵 → 2x3 격자, 컵마다 nested=1)"
echo

i=0
for entry in "${PLACES[@]}"; do
  i=$((i + 1))
  # shellcheck disable=SC2086
  set -- $entry
  slot="$1"; x="$2"; y="$3"

  echo "[$i/6] slot=${slot} → grid(x=${x}, y=${y}, nested=1)"

  echo "  unstack (slot=${slot}) ..."
  if ! post_json "/api/robot/skill/unstack" \
       "{\"slot\": \"${slot}\", \"x\": ${x}, \"y\": ${y}, \"nested\": 1}"; then
    echo "  ✗ unstack 스킬 실패 — 시퀀스 중단" >&2
    exit 1
  fi

  echo "  ✓ slot=${slot} 완료 (x=${x}, y=${y})"
  echo
done

echo "피라미드 해체 완료 (6/6) → 2x3 격자"
