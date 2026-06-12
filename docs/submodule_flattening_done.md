# 서브모듈 평탄화 — 완료 보고서

> 브랜치: `chore/flatten-submodules` · 통합 PR: [cup-stack-integration#1](https://github.com/2026-yarr-robotics/cup-stack-integration/pull/1)
> 작성: 2026-06-12 · 계획서: [`submodule_flattening_plan.md`](submodule_flattening_plan.md)

이 문서는 **계획(plan)에서 실제로 실행 완료된 사항**을 기록한다. 미실행/후속은 §6.

---

## 1. 실행 요약

`cup-stack-server` 집합 레이어와 `vision/` 디렉토리를 **해체**하고, 모든 leaf 레포를
**통합 루트 서브모듈**로 평탄화했다. 서브모듈 포인터는 모두 각 레포 기본 브랜치
**최신 시점**으로 이동. 평탄화로 깨지는 경로는 전부 수정해 동작하도록 했다.

작업은 `main` 기준 worktree(`chore/flatten-submodules`)에서 수행했고, `tmp` 의 WIP 는
건드리지 않았다.

---

## 2. 머지/오픈된 PR

| 레포 | PR | 상태 | 내용 |
|---|---|---|---|
| `server` | [#1](https://github.com/2026-yarr-robotics/server/pull/1) | ✅ MERGED | `start.sh` `../../vision/*`→`../*`, `bringup_agent.py` 로그 경로 |
| `ros2-depth-point-cloude` | [#1](https://github.com/2026-yarr-robotics/ros2-depth-point-cloude/pull/1) | ✅ MERGED | `params.yaml` YOLO 절대경로 `vision/` 접두 제거 |
| `cup-stack-integration` | [#1](https://github.com/2026-yarr-robotics/cup-stack-integration/pull/1) | 🔵 OPEN | 본 평탄화(서브모듈 재구성 + 경로 수정 + 문서) |

서브모듈 내부 커밋은 `dwl21 <nggus5@gmail.com>` author.

---

## 3. 구조 변경 (완료)

**제거된 서브모듈**
- `cup-stack-server` (중첩 `LLM-prompting` 포함 — archived, 프롬프트는 이미 `cup_stack_agent/prompts` 로 이관)
- `vision/ros2-depth-point-cloude`, `vision/vision-node`, `vision/ros2-recode-sequence` (vision/ 계층 제거)

**추가된 루트 서브모듈 (@ latest)**

| 경로 | 포인터(@merge 시점) | 비고 |
|---|---|---|
| `server` | `4e3e3e5` (main) | server#1 merge 포함 |
| `ros2-cup-stack` | `dd1a2e1` (main) | 중첩 `ros2/src/doosan-robot2` @yarr-integration |
| `frontend` | `85ec030` (main) | |
| `fallen-cup-recovery` | `f210c67` (released) | |
| `ros2-depth-point-cloude` | `07e9d55` (main) | depth#1 merge 포함 |
| `vision-node` | `47f2e79` (main) | |
| `ros2-recode-sequence` | `fdd40c9` (main) | archived지만 카메라 bringup 으로 사용 중 → 예외 유지 |
| `tools/ros2-skill-manager` | `9d777c0` (main) | 구 `.gitignore` clone → 정식 서브모듈 승격 |

**이동**: `cup-stack-server/script/*` → `script/server/`
(`build_pyramid.sh`, `build_pyramid_nested.sh`, `cycle_grid.sh`, `cycle_nested.sh`, `unstack.sh`, `unstack_grid.sh`)

---

## 4. 경로 수정 (완료) — "합치면서 변경되는 상대경로 모두"

| 파일 | 수정 | 소유 |
|---|---|---|
| `server/start.sh` | `../../vision/<pkg>`→`../<pkg>`, `../../cup_stack_agent`→`../cup_stack_agent` (형제 `../ros2-cup-stack` 유지) | server (merged) |
| `server/bringup_agent.py` | `AGENT_LOGS_DIR` `_SCRIPT_DIR.parent.parent`→`.parent` (server/ 가 한 단계 얕아짐) | server (merged) |
| `ros2-depth-point-cloude/.../params.yaml` | YOLO 절대경로 `vision/ros2-depth-point-cloude`→`ros2-depth-point-cloude` | depth (merged) |
| `cup_stack_agent/start.sh` | `vision/`·`cup-stack-server/` 접두 제거 | integration |
| `cup_stack_agent/run_upright_cup_pose.sh` | 동일 | integration |
| `tools/run_skill_manager.sh` | `vision/<pkg>` 접두 제거 | integration |
| `vision_rviz.sh` | `$ROOT_DIR/vision/<pkg>`→`$ROOT_DIR/<pkg>` | integration |
| `.gitignore` | `/tools/ros2-skill-manager/` clone-ignore 제거 | integration |

> **의도적 미변경**: `server/server/config.py` 의 `Path(__file__).parents[...]` — server 는
> `docker compose up` 으로 **컨테이너 내부**에서 실행되어 경로가 `/app` 기준으로 해석됨.
> 호스트 트리 위치 이동과 무관하므로 변경하지 않음.

---

## 5. 검증 (완료)

- 수정 셸 스크립트 전부 `bash -n` ✓ (`server/start.sh`, `cup_stack_agent/*.sh`, `tools/run_skill_manager.sh`, `vision_rviz.sh`, `script/server/*.sh`)
- `server/bringup_agent.py` `python3 -m py_compile` ✓
- 잔존 깨진 경로(`cup-stack-server/<pkg>`, `/vision/<pkg>`) grep **0건** ✓
- `git submodule status --recursive` — 8 루트 + 1 중첩(doosan-robot2), 포인터 최신 ✓

---

## 6. 서브모듈 등록 현황 (org active 레포 기준)

**등록됨 (9)**: `server`, `ros2-cup-stack`, `frontend`, `fallen-cup-recovery`,
`ros2-depth-point-cloude`, `vision-node`, `ros2-recode-sequence`(archived·예외),
`tools/ros2-skill-manager`, (중첩) `doosan-robot2`.

**미등록 — active 레포 중 (의도적)**

| 레포 | 미등록 사유 |
|---|---|
| `cup-stack-integration` | 자기 자신 (대상 아님) |
| `cup-stack-server` | 해체됨 (leaf 를 루트로 직접 등록) |
| `hand_pick` | hand-eye pick 이 `cup_stack_agent` 로 inline 흡수, 참조 0건 |
| `vision-YOLO` | 오프라인 학습 레포(런타임 코드 아님), `.pt` 산출물만 소비 |
| `cup-stack-integration-isaac` | 병렬 통합 레포(Isaac 트랙), 중첩 부적합 |
| `yarr-isaac-playground` | Isaac sandbox, 별도 트랙 |

---

## 7. 후속 (미실행 — 별도 PR)

- [ ] `CLAUDE.md`/`README` 구조 섹션 갱신 (현재 `main` 은 구 구조 서술)
- [ ] `recode_sequence` 패키지를 `ros2-depth-point-cloude` 로 병합 → 아카이브 서브모듈 `ros2-recode-sequence` 소거 (계획서 §3-B)
- [ ] 배포 마이그레이션 방침 확정 (라이브 Docker = `/home/ssu/cup-stack`) — 계획서 §9-1
- [ ] `doosan-robot2` fork 브랜치 정책 (사용 `yarr-integration`, upstream 노이즈 정리)
- [ ] stale 브랜치 정리 (계획서 §8): `worktree-*`, `auto-sync-jazzy-*` 등 (`isaac` 보존)
