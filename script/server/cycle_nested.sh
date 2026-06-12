#!/usr/bin/env bash
#
# cycle_nested.sh — "nested stack" 빌드↔해체 반복 사이클
#
# build_pyramid_nested.sh (단일 6컵 nest → 피라미드) 와 unstack.sh (피라미드 → 단일 6컵 nest) 를
# 한 쌍(= 1 사이클)으로 묶어 CYCLES 회 반복한다. 각 스크립트는 로봇을 시작 상태로
# 되돌리므로, build → unstack 한 번이 곧 한 사이클이며 끝나면 원래 nest 상태로 복귀한다.
#
# 사이클 수(CYCLES): 첫 위치 인자 > 환경변수 CYCLES > 기본값 3.
# 자식 스크립트는 좌표를 위치 인자 또는 환경변수로 받는다. 여기서는 위치 인자 1번이
# CYCLES 이므로 좌표는 환경변수로만 전달한다:
#   SRC_X / SRC_Y  → build_pyramid_nested.sh 소스 nest 중앙 좌표
#   DEST_X / DEST_Y → unstack.sh 목적지 nest 중앙 좌표
# BASE_URL 도 설정 시 자식에 전달된다(미설정 시 자식 기본값 사용).
#
# 사용법:
#   ./cycle_nested.sh               # 3회 반복
#   ./cycle_nested.sh 5             # 5회 반복
#   CYCLES=4 SRC_X=0.25 SRC_Y=0.0 DEST_X=0.4 DEST_Y=0.1 ./cycle_nested.sh
#   BASE_URL=https://other.host ./cycle_nested.sh
set -euo pipefail

# 스크립트 위치를 기준으로 자식 스크립트를 찾는다(CWD 무관).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 사이클 수: 첫 위치 인자가 환경변수 CYCLES 를 덮어쓴다. 둘 다 없으면 3.
CYCLES="${1:-${CYCLES:-3}}"

# 양의 정수 검증.
if ! [[ "${CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "usage: $(basename "${BASH_SOURCE[0]}") [CYCLES]" >&2
  echo "  CYCLES 는 양의 정수여야 합니다 (받은 값: '${CYCLES}')" >&2
  echo "  예: ./cycle_nested.sh 5  |  CYCLES=4 SRC_X=0.25 DEST_X=0.4 ./cycle_nested.sh" >&2
  exit 1
fi

# BASE_URL · 좌표 환경변수를 설정된 것만 export 해 자식이 상속하도록 한다.
# (위치 인자로는 넘기지 않는다 — 인자 1번은 CYCLES 이므로.)
[[ -n "${BASE_URL:-}" ]] && export BASE_URL
[[ -n "${SRC_X:-}" ]] && export SRC_X
[[ -n "${SRC_Y:-}" ]] && export SRC_Y
[[ -n "${DEST_X:-}" ]] && export DEST_X
[[ -n "${DEST_Y:-}" ]] && export DEST_Y

echo "nested 사이클 시작: build_pyramid_nested.sh ↔ unstack.sh × ${CYCLES}"
echo

for ((c = 1; c <= CYCLES; c++)); do
  echo "=== cycle ${c}/${CYCLES}: build ==="
  if ! "${SCRIPT_DIR}/build_pyramid_nested.sh"; then
    echo "✗ cycle ${c}/${CYCLES}: build_pyramid_nested.sh 실패 — 중단" >&2
    exit 1
  fi

  echo "=== cycle ${c}/${CYCLES}: unstack ==="
  if ! "${SCRIPT_DIR}/unstack.sh"; then
    echo "✗ cycle ${c}/${CYCLES}: unstack.sh 실패 — 중단" >&2
    exit 1
  fi

  echo "✓ cycle ${c}/${CYCLES} 완료"
  echo
done

echo "nested 사이클 완료: ${CYCLES}/${CYCLES} 사이클 성공"
