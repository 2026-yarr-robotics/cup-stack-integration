# cup-stack-integration — YARR 컵 스태킹 로봇 통합

> **자연어 명령 한 줄로 Doosan M0609 로봇팔이 컵 3‑2‑1 피라미드를 쌓는다.**

```
"3단 피라미드 쌓아줘"
        │
        ▼
   LLM 플래너  ──▶  플랜 실행기  ──▶  POST /api/robot/skill/pyramid
 (cup_stack_agent)                  (server: FastAPI → ROS 2 → 로봇)
```

---

## 프로젝트 개요

`cup-stack-integration` 은 **YARR 컵 스태킹 로봇**의 최상위 통합 레이어다. 사람이 던진
자연어 명령("3단 피라미드 쌓아줘")을 LLM 플래너가 해석해 **집기·놓기 API 호출 시퀀스**로
바꾸고, REST 로봇 제어 서버가 실제 모션으로 실행한다.

핵심 설계 원칙은 **"통합 우선"** 이다. 실험은 perception 을 가짜로 주입하지만, 그 입출력은
**실제 ROS 파이프라인과 동일한 토픽**으로 흐른다. 덕분에 카메라·비전 스택 없이도
*플래너 → 실행기 → 로봇 API* 경로를 처음부터 끝까지 검증할 수 있다.

---

## 기획 배경 & 목표

- **무엇을** — 자연어 명령을 물리적 컵 피라미드(6컵, 3단)로 만드는 폐루프 시스템.
- **왜 LLM 폐루프인가** — 단순 1회 계획이 아니라, 쌓는 도중 컵이 쓰러지거나 사라지는
  **교란**을 감지해 **실시간 재계획(in‑flight replan)** 한다. cold‑start 계획 + in‑flight
  결정의 2단 구조.
- **역할 분리** — 로봇 서버는 **실행 레이어**일 뿐이다. 좌표 `(x, y)` 를 신뢰해 자기 피라미드
  기하로 매핑·모션만 수행하고, *계획·컵 선택·검증은 하지 않는다.* 그 판단은 모두 에이전트의 몫.
- **통합 검증** — 가짜 실험이라도 실제 파이프라인과 I/O 가 동일해야 한다. 대체 토픽을
  만들거나 CLI/파라미터로 좌표를 흘려보내지 않는다.

---

## 시스템 구성

서브모듈은 **레포 루트로 평탄화**되어 있다(구 `cup-stack-server/` 집합 계층과 `vision/`
디렉토리는 해체됨). 각 구성요소:

| 구성요소 | 역할 |
|---|---|
| `cup_stack_agent/` | **LLM 폐루프 실험**(자체 코드) — 가짜 perception + 플래너 + 실행기. 로봇 API 를 POST |
| `server/` | **서브모듈** — FastAPI REST + rosbridge 게이트웨이. `server/start.sh` 가 tmux 단일 진입점 |
| `ros2-cup-stack/` | **서브모듈** — ROS 2 Humble + MoveIt 2 + OnRobot 그리퍼 (안에 `doosan-robot2` 드라이버 중첩) |
| `frontend/` | **서브모듈** — React 모니터링 대시보드 |
| `fallen-cup-recovery/` | **서브모듈**(`released`) — 쓰러진 컵 복구 스킬 |
| `ros2-depth-point-cloude/` | **서브모듈** — `depth_digital_twin`(검출 + 3D 박스) + `recode_sequence`(카메라) |
| `vision-node/` | **서브모듈** — `cup_stacking_verify`(슬롯 채움 판정, `/stack`) |
| `ros2-skill-manager/` | **서브모듈** — 오퍼레이터 GUI + `run_skill_manager.sh` |
| `script/` | 실행 런처 — 서브모듈 스크립트 심볼릭 링크 + `send_command.sh`, `vision_rviz.sh` |
| `docs/`, `CLAUDE.md` | 통합 문서 + 에이전트 가이드 |

---

## 동작 방식 (폐루프)

`cup_stack_agent` 는 실제 ROS 토픽 파이프라인을 **가짜 perception** 으로 구동한다 — 진짜
노드들이 쓰는 *바로 그 토픽*으로 데이터를 주입한다.

```
fake_aggregator_node    → /cups_on_table /stack /user_command
fake_digital_twin_node  → /digital_twin/boxes /stack_track_ids   (측정된 컵 자세)
goal_state_publisher    → /llm_input        (명령 + 세계상태 + 직전결과 병합)
llm_node                → /llm_output       (Ollama: cold‑start 계획 / in‑flight 결정)
plan_executor_node      → 로봇 API POST → /action_result
pick_node               → hand‑eye 정밀 집기 (real‑api 모드)
```

고정 시나리오는 6컵 3단 피라미드를 6번의 API 호출로 쌓으며, LLM 슬롯 → API 슬롯을 매핑한다:

```
L1_left→1l  L1_mid→1m  L1_right→1r  L2_left→2l  L2_right→2r  L3_top→3m
```

기본값으로 `L2_right` 직후 컵 하나가 "사라지는" 교란이 시뮬레이션되며, in‑flight LLM
루프는 이를 감지해 재집기를 수행한다.

---

## 빠른 시작

```bash
cd cup_stack_agent

# 드라이런: 실행기 요청 바디만 로그, 실제 API 호출 없음
./start.sh

# 폐루프: 실제 로봇 API 를 POST (로봇 서버 접속 필요)
./start.sh --real-api
```

`pick_node`(real‑api)는 `moveit_py` 를 import 하며, `start.sh` 가
`/home/ssu/ros2_ws/install/setup.bash` 를 자동 source 한다(`MOVEIT_SETUP` 으로 재정의).

주요 환경변수: `API_URL`, `MODEL`(기본 `qwen3.6:35b`), `OLLAMA_URL`,
`DISTURBANCE_ENABLED`, `API_TIMEOUT_S`.

---

## 더 보기

- **노드 계약·시나리오·트러블슈팅** — `cup_stack_agent/docs/experiment_runbook.md`
- **에이전트/코드 작업 가이드 · 레포 구조** — [`CLAUDE.md`](CLAUDE.md)
- **배포 마이그레이션 방침** — [`docs/deploy_migration_policy.md`](docs/deploy_migration_policy.md)

---

## 규약

- 브랜치 `main`. Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- 서브모듈 내부 변경은 **dwl21** author 로, `chore/…` 브랜치 + PR 후 부모에서 포인터 bump.
- 가짜 실험의 I/O 는 실제 ROS 파이프라인과 동일하게 유지 — 대체 토픽이나 CLI/파라미터
  좌표 전달 금지.
