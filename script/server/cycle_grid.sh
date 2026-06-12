#!/usr/bin/env bash
#
# cycle_grid.sh — "일반(grid) stack" 빌드↔해체 반복 사이클
#
# build_pyramid.sh (2x3 격자 → 피라미드) 와 unstack_grid.sh (피라미드 → 2x3 격자) 를
# 한 쌍(= 1 사이클)으로 묶어 CYCLES 회 반복한다. 각 스크립트는 로봇을 시작 상태로
# 되돌리므로, build → unstack 한 번이 곧 한 사이클이며 끝나면 원래 격자 상태로 복귀한다.
#
# 사이클 수(CYCLES): 첫 위치 인자 > 환경변수 CYCLES > 기본값 3.
# BASE_URL 은 자식 스크립트에 그대로 전달된다(미설정 시 자식 기본값 사용).
#
# 사용법:
#   ./cycle_grid.sh                 # 3회 반복
#   ./cycle_grid.sh 5               # 5회 반복
#   CYCLES=10 ./cycle_grid.sh       # 환경변수로 10회
#   BASE_URL=https://other.host ./cycle_grid.sh
set -euo pipefail

# 스크립트 위치를 기준으로 자식 스크립트를 찾는다(CWD 무관).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 사이클 수: 첫 위치 인자가 환경변수 CYCLES 를 덮어쓴다. 둘 다 없으면 3.
CYCLES="${1:-${CYCLES:-3}}"

# 양의 정수 검증.
if ! [[ "${CYCLES}" =~ ^[1-9][0-9]*$ ]]; then
  echo "usage: $(basename "${BASH_SOURCE[0]}") [CYCLES]" >&2
  echo "  CYCLES 는 양의 정수여야 합니다 (받은 값: '${CYCLES}')" >&2
  echo "  예: ./cycle_grid.sh 5  |  CYCLES=10 ./cycle_grid.sh" >&2
  exit 1
fi

# BASE_URL 이 설정돼 있으면 자식 스크립트가 상속하도록 export.
if [[ -n "${BASE_URL:-}" ]]; then
  export BASE_URL
fi

echo "grid 사이클 시작: build_pyramid.sh ↔ unstack_grid.sh × ${CYCLES}"
echo

for ((c = 1; c <= CYCLES; c++)); do
  echo "=== cycle ${c}/${CYCLES}: build ==="
  if ! "${SCRIPT_DIR}/build_pyramid.sh"; then
    echo "✗ cycle ${c}/${CYCLES}: build_pyramid.sh 실패 — 중단" >&2
    exit 1
  fi

  echo "=== cycle ${c}/${CYCLES}: unstack ==="
  if ! "${SCRIPT_DIR}/unstack_grid.sh"; then
    echo "✗ cycle ${c}/${CYCLES}: unstack_grid.sh 실패 — 중단" >&2
    exit 1
  fi

  echo "✓ cycle ${c}/${CYCLES} 완료"
  echo
done

echo "grid 사이클 완료: ${CYCLES}/${CYCLES} 사이클 성공"
