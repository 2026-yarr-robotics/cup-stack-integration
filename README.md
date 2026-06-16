# cup-stack-integration — YARR 컵 스태킹 로봇 통합

> **자연어 명령 한 줄로 Doosan M0609 로봇팔이 컵 3‑2‑1 피라미드를 쌓는다.**

```
"3단 피라미드 쌓아줘"
        │
        ▼
   LLM 플래너  ──▶  플랜 실행기  ──▶  POST /api/robot/move ─▶ pick_node ─▶ /api/robot/skill/pyramid
 (cup_stack_agent)   (coarse move)                          (hand-eye fine pick)
```

---

## 프로젝트 개요

`cup-stack-integration` 은 **YARR 컵 스태킹 로봇**의 최상위 통합 레이어다. 사람이 던진
자연어 명령("3단 피라미드 쌓아줘")을 LLM 플래너가 해석해 **집기·놓기 시퀀스**로
바꾸고, REST 로봇 제어 서버가 실제 모션으로 실행한다.

핵심 설계 원칙은 **"통합 우선"** 이다. 실험은 perception 을 가짜로 주입하지만, 그 입출력은
**실제 ROS 파이프라인과 동일한 토픽**으로 흐른다. 덕분에 카메라·비전 스택 없이도
*플래너 → 실행기 → 로봇 API* 경로를 처음부터 끝까지 검증할 수 있다.

---

## 기획 배경 & 목표

- **무엇을** — 자연어 명령을 물리적 컵 피라미드(6컵, 3단)로 만드는 폐루프 시스템.
- **왜 LLM 폐루프인가** — 단순 1회 계획이 아니라, 쌓는 도중 컵이 쓰러지거나 사라지는
  **교란**을 감지해 **실시간 재계획(in‑flight replan)** 한다. cold‑start 계획 + in‑flight
  결정의 2단 구조. plan 은 조언일 뿐이고, 매 결정의 brain 은 LLM 이다 — plan 이 소진되면
  실행기는 스스로 done 을 정하지 않고 멈춰 다음 LLM 결정을 기다린다.
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
| `ros2-cup-stack/` | **서브모듈** — ROS 2 Humble + MoveIt 2 + OnRobot 그리퍼 (안에 `doosan-robot2` 드라이버 중첩, `@yarr-integration` 포크) |
| `frontend/` | **서브모듈** — React 모니터링 대시보드 |
| `outlier-cup-recovery/` | **서브모듈**(`@main`) — 비정상 자세 컵 복구(쓰러진 컵 + 입구가 위인 컵) |
| `ros2-depth-point-cloude/` | **서브모듈** — `depth_digital_twin`(검출 + 3D 박스) + `recode_sequence`(카메라) |
| `vision-node/` | **서브모듈** — `cup_stacking_verify`(슬롯 채움 판정, `/stack`) |
| `ros2-skill-manager/` | **서브모듈** — 오퍼레이터 GUI + `run_skill_manager.sh` |
| `script/` | 실행 런처 — 서브모듈 스크립트 심볼릭 링크 + `send_command.sh`, `vision_rviz.sh` |
| `docs/`, `CLAUDE.md` | 통합 문서 + 에이전트 가이드 |

> 프레시 클론: `git submodule update --init --recursive`. 서브모듈 내부 변경은 author **dwl21**,
> 의미 prefix 브랜치(`feat/…`,`fix/…`,`docs/…`,`chore/…`) 후 부모에서 포인터 bump.

---

## 동작 방식 (폐루프)

`cup_stack_agent` 는 실제 ROS 토픽 파이프라인을 **가짜 perception** 으로 구동한다 — 진짜
노드들이 쓰는 *바로 그 토픽*으로 데이터를 주입한다. 폐루프의 두뇌·입·손은 세 노드로 나뉜다.

```
fake_aggregator_node    → /cups_on_table /stack /user_command
fake_digital_twin_node  → /digital_twin/boxes /stack_track_ids   (측정된 컵 자세)
goal_state_publisher    → /llm_input        (명령 + 세계상태 + 직전결과 병합)
llm_node                → /llm_output       (Ollama: cold‑start 계획 / in‑flight 결정)
plan_executor_node      → POST /api/robot/move → /move_result → /action_result
pick_node               → hand‑eye 정밀 집기 → POST /api/robot/skill/pyramid (real‑api 모드)
upright_cup_pose_node   → /hand_eye/boxes /fallen_cups   (real hand‑eye)
```

**goal_state_publisher (GSP)** — 시스템 토픽을 구독해 LLM 입력용 단일 payload `/llm_input`
을 조립·발행한다(LLM 을 직접 호출하지 않는다). `/user_command` 가 새로 오면 **cold_start**,
`/action_result` 가 오면 **in_flight** payload 를 낸다. 액션이 진행 중인 동안에는 세계상태를
freeze 해 팔이 시야에 들어와 카운트가 흔들리는 노이즈를 막고(`freeze_world_during_action`),
액션 효과가 세계에 반영(`action_result_reflected`)될 때까지 발행을 보류한다(무한 정지
방지 타임아웃 동반). 미래 슬롯의 false‑positive 는 null 로 마스킹해 조기 done 을 막는다.

**llm_node** — `/llm_input` 의 `mode` 로 프롬프트를 라우팅한다. cold_start →
`prompts/cold_start_planner.md`(6‑step 3‑level plan + 슬롯별 색 제약), 그 외 →
`prompts/inflight_decider.md`(continue / replan / unstack / done / fallen_recovery). Ollama 를
`temperature 0`, `format:json`, `num_predict` 캡으로 호출하고, 파싱/검증 실패 시 1회 재시도
후 HITL cold‑start fallback 으로 루프를 멈추지 않는다.

**plan_executor_node** — LLM plan 을 받아 **coarse move 절반만** 수행한다. 색 → exo 뷰 컵 XY
해석 후 `POST /api/robot/move {x,y,z}`(z 는 hand‑eye 가 컵을 볼 수 있는 고정 접근높이)로 팔을
대략 이동시키고 `/move_result` 만 낸다. 정밀 집기·피라미드 배치는 `pick_node` 가 hand‑eye
뷰로 `/api/robot/skill/pyramid` 를 직접 호출해 담당한다(이 노드는 피라미드 기하를 들고 있지
않다). 컵이 실제로 놓이는 곳은 pick_node 의 skill 호출이므로 완료 신호 `/action_result` 도
pick_node 소유다 — plan_executor 가 `/action_result` 를 직접 내는 건 ① recovery, ② coarse‑move
단계 실패(graspable 컵 없음) 두 경우뿐. **atomic step**: coarse move 성공만으론 step 을
전진시키지 않고, pick_node 의 pick 확정이 와야 다음 step 으로 넘어간다(실패 시 HOME 복귀 후
같은 step 재시도).

LLM 슬롯 → API 슬롯 매핑:

```
L1_left→1l  L1_mid→1m  L1_right→1r  L2_left→2l  L2_right→2r  L3_top→3m
```

**교란**은 스크립트가 아니라 **물리적**이다 — 이미 쌓인 컵을 손으로 빼면 verifier 가
`/stack` 에서 제거하고, 테이블 재등장 시 `/cups_on_table` 이 증가해 LLM 이 in‑flight 로
빈 슬롯을 재충전한다.

**쓰러진/비정상 컵 복구** — fallen 판단은 hand‑eye 전담이다. `upright_cup_pose_node` 가
`/fallen_cups {"count":N}` 를 내고, 집을 upright 가 하나도 없을 때만 게이트가 열린다.
`fallen_count>0` 이면 LLM 이 `decision="fallen_recovery"` 를 내고, plan_executor 는 좌표·색을
보내지 않고 `POST /api/robot/outlier-cup/recovery` 로 **outlier 복구 오케스트레이터**를 구동한다
(쓰러진 컵 + 입구가 위인 컵을 한 경로로 처리, API 호출당 1회 실행 후 hand‑eye 로 재판단).

---

## 빠른 시작

```bash
cd cup_stack_agent

# 폐루프(기본): 실제 로봇 API 를 POST (로봇 서버 접속 필요)
./start.sh                 # == --real-api
./start.sh --real-api      # 명시적으로 동일

# 드라이런: 실행기 요청 바디만 로그, 실제 API 호출 없음
./start.sh --dry-run
```

`pick_node`(real‑api)는 `moveit_py` 를 import 하며, `start.sh` 가
`/home/ssu/ros2_ws/install/setup.bash` 를 자동 source 한다(`MOVEIT_SETUP` 으로 재정의).

주요 환경변수: `API_URL`, `MODEL`(기본 `qwen3.6:35b`), `OLLAMA_URL`,
`DISTURBANCE_ENABLED`, `API_TIMEOUT_S`.

> 실로봇 안정 구동에는 ros2-cup-stack 의 RT 셋업(`setup_rt.sh` 1회 + `RT_REQUIRED=1`)이
> 필요하다 — `ros2_control_node` 가 SCHED_OTHER 로 떨어지면 mid‑motion 속도 스파이크로
> safety‑stop(red light)이 난다.

테스트:

```bash
cd cup_stack_agent
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n start.sh
```

> 참고: `test_stabilizer` 의 `aggregate` import 실패 1건은 리팩토링(494e529) 때 심볼이
> 제거되며 생긴 기존 결함으로, 본 통합과 무관하다(25/26 pass).

---

## 더 보기

- **노드 계약·시나리오·트러블슈팅** — `cup_stack_agent/docs/experiment_runbook.md`
- **동적 루프 설계(phase·결정)** — `cup_stack_agent/docs/dynamic_loop_plan.md`
- **시행착오 기록(되돌림·재수정·설계 피벗, commit‑cited)** — [`docs/trial_and_error.md`](docs/trial_and_error.md)
- **에이전트/코드 작업 가이드 · 레포 구조** — [`CLAUDE.md`](CLAUDE.md)
- **배포 마이그레이션 방침** — [`docs/deploy_migration_policy.md`](docs/deploy_migration_policy.md)

---

## 규약

- 브랜치 `main`. Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- 서브모듈 내부 변경은 **dwl21** author 로, 의미 prefix 브랜치 + 부모에서 포인터 bump.
- 가짜 실험의 I/O 는 실제 ROS 파이프라인과 동일하게 유지 — 대체 토픽이나 CLI/파라미터
  좌표 전달 금지.
