# Isaac 통합 머지 기록 (MERGE.md)

> 브랜치: `feat/isaac-integration`  (base: `origin/main` = `07c3d1b`)
> 일자: 2026-06-12
> 방식: **전체 merge 아님 — isaac fork 고유분만 신구조로 외과적 이식(port)**

## 배경

`cup-stack-integration-isaac`(fork)와 `cup-stack-integration`(원본)은 공통
조상 `085a56b`에서 갈라졌다. 그 사이 **원본이 서브모듈 평탄화 + `cup-stack-server`
해체**(신구조)를 단행해, fork(구구조)와 디렉터리 레이아웃 자체가 달라졌다.
따라서 `git merge` 시 `.gitmodules`/gitlink-vs-tree 충돌이 전면 발생 → merge 대신
**최신 원본을 base로 두고 fork의 Isaac 고유 작업만 이식**했다.

검증 결과 무거운 부분은 이미 원본이 흡수한 상태였다:

| 서브모듈 | 결과 | 이식 |
|---|---|---|
| `ros2-depth-point-cloude` | 원본이 isaac 포함(앞섬) | 불필요 |
| `vision-node` | 원본이 isaac 포함 | 불필요 |
| `fallen-cup-recovery` | 원본이 isaac 포함 | 불필요 |
| `ros2-recode-sequence` | 원본 depth로 흡수 | 불필요 |
| `frontend` | 통합과 무관 | 손대지 않음(원본 핀 유지) |

## 이식 항목 (슈퍼프로젝트 커밋 5개)

| 커밋 | 항목 | 내용 |
|---|---|---|
| `6706f06` | A | `yarr-isaac-playground` 서브모듈 추가 (Isaac Sim 디지털 트윈, 핀 `b8f7c9a`→경로수정 후 `94c293c`) |
| `1d8dae3` | D | `script/{start,stop}_isaac.sh` 심링크 + 플레이그라운드 스크립트 신구조 경로 재매핑 |
| `df1d01c` | C | `ros2-cup-stack` SimRG 그리퍼 백엔드 (`CUP_STACK_GRIPPER_BACKEND=sim`) |
| `f5b2776` | E | `ros2-skill-manager` run_skill_manager: repo-relative + 6 엔드포인트 핀 + Doosan ws |
| `04a925a` | B | `server` vision-relay / LLM 전용 agent 창 분리 |

## 서브모듈 브랜치 (커밋 저자: EunwooSong, provenance는 cherry-pick `-x` 유지)

| 서브모듈 | 브랜치 | 핀 |
|---|---|---|
| `yarr-isaac-playground` | `fix/flatten-paths` | `94c293c` |
| `ros2-cup-stack` | `feat/sim-gripper-backend` | `c1a3e2f` |
| `ros2-skill-manager` | `fix/pin-all-endpoints-repo-relative` | `8605682` |
| `server` | `feat/vision-relay-llm-split` | `9dbf697` |

## 신구조 경로 재매핑 (D, 플레이그라운드 start/stop_isaac.sh 내부)

```
cup-stack-server/server              -> server
cup-stack-server/ros2-cup-stack      -> ros2-cup-stack
vision/ros2-depth-point-cloude       -> ros2-depth-point-cloude
vision/vision-node                   -> vision-node
cup-stack-server/fallen-cup-recovery -> fallen-cup-recovery
tools/run_skill_manager.sh           -> script/run_skill_manager.sh
```

## 충돌 해소 (B, server/start.sh — 1건)

cherry-pick `605581d` 시 `start.sh`에서 충돌. 원인은 **경로 깊이**: isaac은
`$SCRIPT_DIR/../../cup_stack_agent`(구구조, 2단계)를 썼으나 신구조는
`$SCRIPT_DIR/../cup_stack_agent`(1단계). 신구조 경로로 수정하고 중복 `AGENT_DIR`
정의를 제거해 해소(`bash -n` 통과).

## 검증 / 미검증

- ✅ 충돌 마커 없음, 심링크 체인 정상, 서브모듈 핀 정합, `bash -n` 통과
- ✅ [E] 안전성: 원본 `skill_manager_node.py`가 scan/move/position 파라미터 선언 확인
- ⚠️ **빌드/런타임(colcon, ROS, Isaac Sim) 미검증** — 통합 환경에서 한 번 실행 필요

## 푸시 순서

서브모듈 → 부모. 서브모듈 피처 브랜치 push로 핀 커밋이 원격에서 도달 가능해진다
(원하면 각 서브모듈 main으로 ff-merge 후 push — SHA 동일하게 유지). 마지막에
슈퍼프로젝트 `feat/isaac-integration` push.
