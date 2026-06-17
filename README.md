# Project Cup Stacks — 외란에 강건한 비전 기반 컵 스택 시스템

> **자연어 명령 한 줄로 Doosan M0609 로봇팔이 컵 3‑2‑1 피라미드를 쌓는다.
> 쌓는 도중 컵이 쓰러지거나 사라져도 LLM 이 실시간 재계획해 스스로 복구한다.**

```
"3단 피라미드 쌓아줘"
        │
        ▼
   LLM 플래너  ──▶  플랜 실행기  ──▶  POST /api/robot/move ─▶ pick_node ─▶ /api/robot/skill/pyramid
 (cup_stack_agent)   (coarse move)                          (hand‑eye 정밀 집기)
        ▲                                                              │
        └──────────── 비전(world state) ◀── 실측 perception ◀─────────┘  (closed loop)
```

숭실대학교 Co‑op 지능형 로보틱스 트랙 · **YARR** 팀 · 2026.06

---

## 프로젝트 개요

`cup-stack-integration` 은 **YARR 컵 스태킹 로봇**의 최상위 통합 레이어다. 사람이 던진
자연어 명령("3단 피라미드 쌓아줘")을 LLM 플래너가 해석해 **집기·놓기 시퀀스**로
바꾸고, REST 로봇 제어 서버가 검증된 파라미터화 스킬로 실제 모션을 실행한다. 비전
스택은 컵의 3D 위치·색·자세를 추정해 월드 상태를 만들고, 그 상태가 다시 LLM 으로
되먹임되어 **폐루프**를 닫는다.

핵심 설계 원칙은 **역할 분리**다.

- **LLM** 은 *"무엇을·어디에"* 만 결정한다 — 충돌·특이점·IK 분기는 모른다.
- **로봇 서버**는 *실행 레이어*다. 좌표 `(x, y)` 를 신뢰해 자기 피라미드 기하로 매핑·
  모션만 수행하고, *계획·컵 선택·검증은 하지 않는다.*
- **비전**은 *관측* 만 한다 — 컵을 검출·정렬·판정해 월드 상태로 환원한다.

그래서 매 동작의 두뇌는 LLM 이고, plan 은 조언일 뿐이다. plan 이 소진돼도 실행기는
스스로 done 을 정하지 않고 멈춰 다음 LLM 결정을 기다린다.

---

## 기획 배경 & 목표

| | |
|---|---|
| **문제 정의** | 기존 산업용 로봇은 **고정 경로**를 전제로 동작해 외란(예외 상황)에 매우 취약하다. 쉽게 다가갈 수 있는 **컵 스택** 문제로 이 한계를 푼다. |
| **목표** | 정해진 위치에 **최대 3단**까지 컵을 쌓는 **End‑to‑End Task** + **외란 발생 시 자가복구** 시스템. |
| **해결 방법** | **Exo‑view 카메라**로 컵 위치의 **3D Pose Estimation** 을 만들어 모두 파악(pick & place)하고, 외란 발생 시 **LLM 의 Re‑Plan** 으로 자가복구를 완성. |

---

## 팀 & 역할

| 이름 | 역할 | 담당 |
|---|---|---|
| **송은우** | 팀장 · PM/총괄 | Depth Point Cloud 환경 · Isaac Sim 환경 |
| **구정현** | LLM 모듈 개발 | LLM 파인튜닝·프롬프트 설계, GSP/Plan‑Executor 노드, 전체 통합·검증 |
| **김재림** | Vision 모델 개발 | YOLO 파인튜닝·검증, 데이터셋 수집·라벨링, vLLM 서버 호스팅 |
| **남궁현** | 로봇 제어 | ROS 2 제어, 웹 대시보드, REST Skill API·구현체 |
| **전석남** | 통합 모듈 개발 | 외란 처리 skill set, hand‑eye 기반 비전+제어, 전체 통합·검증 |
| **임정하** | 통합 모듈 개발 | YOLO 파인튜닝·검증, 데이터셋 수집·라벨링, 전체 통합·검증 |

---

## 시스템 아키텍처

```
                          ┌─────────────── Server (FastAPI + rosbridge) ───────────────┐
  User "3단 쌓아줘"  ───▶  │  User Interaction · Web 대시보드 · World State Streaming     │
                          └───────────────┬───────────────────────────┬───────────────┘
                                          │ /llm_input                │ POST /api/robot/skill/{name}
                                          ▼                            ▼
   ┌─ Exo‑view (RGB‑D) ─┐        ┌──────────────┐            ┌─ Parameterized Skill Pool ─┐
   │  YOLO seg + ArUco  │──┐     │  LLM Planner │ prompt     │  Stacking · Unstack ·       │
   │  → 3D Pose (Kasa)  │  │     │  (Ollama)    │◀──────────▶│  Recovery · Clean           │
   │  → Verify /stack   │  ├──▶  │   Executor   │ replan?    └────────────┬────────────────┘
   ├─ Hand‑eye (RGB‑D) ─┤  │     └──────────────┘                         │ MoveIt 2 (Solve IK)
   │  YOLO seg 정밀 pick│  │   World State Atoms                          ▼
   ├─ Robot State ──────┤  │   (cups_on_table·stack·robot·fallen)   M0609 + RG2 (Real / Isaac Sim)
   └────────────────────┘  └──────────────────────── closed loop ◀───────┘
```

실험은 perception 을 가짜로 주입할 수도, **실제 exo/hand‑eye 비전을 그대로 흘릴 수도**
있다. 어느 쪽이든 입출력은 **실제 ROS 파이프라인과 동일한 토픽**으로 흐른다 — 대체
토픽을 만들거나 CLI/파라미터로 좌표를 흘려보내지 않는다. 덕분에 *플래너 → 실행기 →
로봇 API* 경로를 처음부터 끝까지 검증할 수 있다.

---

## 레포 구성 (서브모듈)

서브모듈은 **레포 루트로 평탄화**되어 있다(구 `cup-stack-server/` 집합 계층과 `vision/`
디렉토리는 해체됨). 프레시 클론: `git submodule update --init --recursive`.

| 구성요소 | 종류 | 역할 / 주요 패키지·노드 |
|---|---|---|
| **`cup_stack_agent/`** | 자체 코드 | **LLM 폐루프 실험** — `goal_state_publisher`·`llm_node`·`plan_executor_node`·`pick_node`·`upright_cup_pose_node`·`aggregator_node`·`digital_twin_stabilizer_node`(+ `fake_*` 대응). 로봇 API 를 POST |
| **`server/`** | 서브모듈 | **FastAPI REST + rosbridge 게이트웨이**. `routers/{robot,dashboard,handineye,handtoeye}.py` + `domains/{robot,fallen_cup,mouth_up_cup,handineye,handtoeye}.py`. `start.sh` 가 tmux 단일 진입점 |
| **`ros2-cup-stack/`** | 서브모듈 | **ROS 2 Humble + MoveIt 2 + OnRobot 그리퍼** — `cup_stack`·`cup_stack_interfaces` 패키지. 안에 `doosan-robot2`(M0609 드라이버) + `outlier-cup-recovery` 중첩 |
| **`outlier-cup-recovery/`** | 서브모듈 | **비정상 자세 컵 복구** — `dsr_practice`(`outlier_cup_recovery`·`stand_fallen_cup`·`place_mouth_up_cup`) + `speed_stack_yolo_seg`(YOLO26‑seg) |
| **`ros2-depth-point-cloude/`** | 서브모듈 | **Depth 디지털 트윈** — `depth_digital_twin`(`detection_node`·`point_cloud_node`·`cup_fusion_node`…) + `recode_sequence`(카메라 녹화/재생·hand‑eye TF) |
| **`vision-node/`** | 서브모듈 | **컵 스택 검증** — `cup_stacking_verify/verifier_node.py`: 3D IoU 점유 판정으로 `/stack` 슬롯 채움 검증 |
| **`frontend/`** | 서브모듈 | **React + Vite 모니터링 대시보드** — 월드 상태·로봇 상태 라이브 스트리밍 |
| **`ros2-skill-manager/`** | 서브모듈 | **오퍼레이터 GUI** — `skill_manager` 패키지(Pick/Pyramid/UpdateInput) + `run_skill_manager.sh` |
| `script/` | 심볼릭 링크 | 서브모듈 스크립트 링크 + 통합 소유 `send_command.sh`, `vision_rviz.sh` |
| `docs/`, `CLAUDE.md` | 문서 | 통합 문서 + 에이전트/코드 작업 가이드 |

---

## 구성 시스템 상세

### 1. 비전 시스템 — 컵 위치/색/자세 → World State

| 단계 | 내용 |
|---|---|
| **YOLO seg** | YOLO26‑seg 인스턴스 세그멘테이션으로 컵 상태를 분류. **Exo‑view**: `fallen‑cup`/`upright‑cup` 2‑class(val mAP50 ≈ 0.99), **Hand‑eye**: `fallen‑cup`/`mouth‑up‑cup`/`upright‑cup` 3‑class. DDP 안정성·VRAM OOM 방지를 위해 Backbone 동결(Stage 1)→전체 미세조정(Stage 2) 2‑stage 학습, 회전/상하반전 차단 + HSV 증강 + Hard‑Negative 20% 로 false‑positive 제거 |
| **ArUco 좌표계** | Exo 카메라 RGB 에서 ArUco 마커를 찾아 `Base → Exo` 변환행렬을 계산 → 검출된 point cloud 를 **로봇 base 좌표계**로 변환. 세팅마다 빠르게 재보정 |
| **Digital Twin (Kasa)** | YOLO seg mask + depth 로 컵 중심을 추정. exo 가 멀어 depth 오차가 크므로, base 로 변환한 x·y 를 z 평면에 투영해 **Kasa 원 피팅**(최소자승)으로 중심·반지름을 빠르게 구하고(z 는 height 계산에만 사용), 쓰러진 컵은 OBB 로 자세를 추정. 정밀 조작은 hand‑eye 가 담당 |
| **Verifier** (`/stack`) | 서버가 정의한 컵 중심점·각도·Offset 으로 슬롯마다 **가상 영역**을 만들고, 3D IoU 가 임계 이상이면 점유로 판정. ID 필터링으로 조작 가능한 컵을 분리 |

> Raw depth → 3D cone fit → exo+hand cone fit → rim fit 을 거쳐, **노드 과다로 인한 CPU
> 포화** 때문에 가장 단순·빠른 **Kasa 원 피팅**으로 수렴했다(자세한 진화 과정은 발표자료).

### 2. LLM 플래너 — `cup_stack_agent` (자체 코드)

폐루프의 두뇌·입·손이 세 노드로 나뉜다.

- **`goal_state_publisher` (GSP)** — 시스템 토픽(`cups_on_table`·`stack`·`robot_state`·
  `user_command`·`action_result`)을 구독해 LLM 입력용 단일 payload `/llm_input` 을 JSON
  으로 조립·발행한다(LLM 을 직접 호출하지 않는다). `/user_command` 가 새로 오면
  **cold_start**, `/action_result` 가 오면 **in_flight** payload. 액션 진행 중에는 팔이 시야에
  들어와 카운트가 흔들리는 노이즈를 막으려 월드 상태를 **freeze** 하고(짧은 settle 뒤 해제),
  액션 효과가 세계에 반영될 때까지 발행을 보류한다(무한 정지 방지 타임아웃 동반). 미래
  슬롯의 false‑positive 는 null 로 마스킹해 조기 done 을 막는다.
- **`llm_node`** — `/llm_input` 의 `mode` 로 프롬프트를 라우팅한다. cold_start →
  `prompts/cold_start_planner.md`(6‑step 3‑level plan + 슬롯별 색 제약), 그 외 →
  `prompts/inflight_decider.md`(continue / replan / unstack / done / fallen_recovery). Ollama 를
  `temperature 0`, `format:json`, `num_predict` 캡으로 호출해 재현성을 확보하고, 파싱/검증
  실패 시 1회 재시도 후 cold‑start fallback 으로 루프를 멈추지 않는다.
- **`plan_executor_node`** — LLM plan 을 받아 **coarse move 절반만** 수행한다. 색 → exo 뷰
  컵 XY 해석 후 `POST /api/robot/move {x,y,z}`(z 는 hand‑eye 가 컵을 볼 수 있는 고정 접근
  높이)로 팔을 대략 이동시키고 `/move_result` 만 낸다. 정밀 집기·피라미드 배치는
  `pick_node` 가 hand‑eye 뷰로 `/api/robot/skill/pyramid` 를 직접 호출해 담당한다.
  **atomic step**: coarse move 성공만으론 step 을 전진시키지 않고, pick_node 의 pick 확정이
  와야 다음 step 으로 넘어간다(실패 시 HOME 복귀 후 같은 step 재시도).

LLM 슬롯 → API 슬롯 매핑: `L1_left→1l  L1_mid→1m  L1_right→1r  L2_left→2l  L2_right→2r  L3_top→3m`

### 3. 로봇 통합 — Parameterized Skill Pool

LLM 은 색·슬롯만 정하고, 실제 모션은 **검증된 파라미터화 스킬**의 REST 호출로 캡슐화된다.

```
LLM Executor → POST /api/robot/skill/{name}(params) → Skill Pool → MoveIt 2 Solve IK
            → joint trajectory → 로봇 실행 → results·status → World State → LLM 재판단
```

| 스킬 | 파라미터 | 동작 |
|---|---|---|
| **Stacking** (`/skill/pyramid`) | `{slot, x, y}` | 슬롯·좌표에 컵 적재 |
| **Unstack** (`/skill/unstack`, `/skill/unstack_all`) | `{slot}` | 피라미드 슬롯에서 컵을 빼 나열(Stacking 의 역) |
| **Recovery** (`/outlier-cup/recovery`) | — | 외란 컵을 hand‑eye 재인식 후 정상 자세로 |
| **Clean** | — | 쌓인 컵 전체를 정리해 다음 시도 가능 상태로 |

피라미드 배치 기하(`PYRAMID_CUP_SPACING`·`PYRAMID_LAYER_HEIGHT`·`DEFAULT_PYRAMID_DEGREE`)는
**서버**(`server/.../domains/robot.py`)가 소유한다 — verifier 의 `cup_ref_w`·`layer_gap`·
`degree` 가 이를 그대로 미러링해야 판정 슬롯이 실제 배치와 일치한다.

> **궤적 안전 가드(IK 분기)** — M0609 6축 IK 는 한 목표 pose 에 elbow‑up/down·손목 뒤집힘
> 등 여러 해(분기)가 있다. 수치 IK 가 현재와 다른 분기로 수렴하면 팔이 천장으로 크게
> 휘둘릴 수 있어, **현재 관절을 seed 로** 같은 분기로 유도하고 임계(2.0 rad) 초과 변화는
> "위험 궤적"으로 거부한다(같은 EE 목표에서 가드 OFF maxΔ 247° → 가드 ON maxΔ 69°).
> 주 플래너는 Pilz(PTP/LIN), 실패 시 OMPL RRTConnect 폴백.

### 4. 시뮬레이션 검증 — Isaac Sim

동일한 컵·로봇·ArUco 배치를 Isaac Sim 에 모델링하고 **같은 ROS 파이프라인**을 sim 에서
구동해 로직을 추가 검증한다(그리퍼‑물체 충돌 보정을 위해 sticky‑gripper 형태로 컵 조작).

---

## 외란 처리 & 자가복구

**교란은 스크립트가 아니라 물리적**이다 — 이미 쌓인 컵을 손으로 빼면 verifier 가 `/stack`
에서 제거하고, 테이블 재등장 시 `/cups_on_table` 이 증가해 LLM 이 in‑flight 로 빈 슬롯을
재충전한다. 다음 두 가지를 함께 다룬다.

- **쓰러진/비정상 컵 복구** — fallen 판단은 hand‑eye 전담이다. `upright_cup_pose_node` 가
  `/fallen_cups {"count":N}` 를 내고, **집을 upright 가 하나도 없을 때만** 게이트가 열린다.
  `fallen_count>0` 이면 LLM 이 `decision="fallen_recovery"` 를 내고, plan_executor 는 좌표·색
  대신 `POST /api/robot/outlier-cup/recovery` 로 **outlier 복구 오케스트레이터**를 구동한다
  (옆으로 쓰러진 컵은 세우고, 입구가 위인 `mouth‑up‑cup` 은 손목을 뒤집어 정상 자세로 —
  한 경로로 처리, API 호출당 1회 실행 후 hand‑eye 로 재판단).
- **컵 부족 → 부분 피라미드** — 목표 컵 수보다 테이블 컵이 적어도 **거부하지 않고**, 가진
  컵으로 채울 수 있는 만큼만 쌓는 valid PARTIAL 을 만든다(아래 "최근 변경" 참고).

---

## 최근 변경 (이 브랜치)

현재 작업은 **컵 부족·HITL·stale‑pick 강건화**에 집중돼 있다.

- **컵 부족 시 부분 피라미드** — temperature 0 모델이 컵이 부족해도 6컵 풀 피라미드를
  내며 *유령 컵*(없는 4번째 파란 컵 등)을 지어내던 문제. `llm_node` 가 plan 을 빌드 순서로
  훑어 테이블이 공급 못 하는 첫 step 에서 잘라(`_trim_cold_start_to_inventory`) **valid
  PARTIAL** 을 만든다(target/cup_budget/slot_colors 는 그대로). 프롬프트도 few‑shot 예시 8
  (색 제약 + 컵 부족 → 잘린 부분 계획; status 는 `ok` 유지, 컵을 지어내거나 거부하지 않음)
  을 추가해 코드·프롬프트 양쪽에서 강제.
- **HITL 재시도 루프** — 계획이 끝내 복구 불가하면 `llm_node` 가 비‑ok `/llm_output`
  (`_publish_hitl_drop`)을 내, plan_executor 가 plan 을 비우고 GSP 의 no‑progress 워치독을
  **이 drop 시점 기준**으로 무장한다(모델 지연과 레이스 없음). `decision_watchdog_s` 를
  15→**25s** 로(cold‑start 호출 단독 ~14s) — 사람이 장면을 고칠(부족한 컵 추가) 시간을 준
  뒤 cold‑start 재계획이 자동 발화한다.
- **stale‑pick 방지 타이밍** — *"느려도 되지만 절대 일찍(stale) 샘플하지 않는다."* hand‑eye
  카메라가 실측 ~1.8 Hz(프레임당 ~0.55s)로 느리고 CupTracker 는 이동 후 좌표가 튀면 4프레임
  연속 outlier 가 모여야 재획득(창 ≈ 2.2s)한다. 도착 직후 hand‑eye 입력을 버리는 FROZEN 을
  재획득 창보다 길게 둬(`PICK_FROZEN_SEC` 1.0→**3.0s**), frozen 종료 시점엔 트래커가 이미
  재획득을 끝낸 신선한 첫 마커를 받게 한다(+`PICK_SETTLE_SEC` 0.5s, `PICK_BOX_WAIT_SEC` 2.0s).
- 기본 명령을 색 제약 없는 `3단 피라미드 쌓아줘` 로 되돌림(start.sh).

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

주요 환경변수: `API_URL`, `MODEL`(기본 `qwen3.6:35b`), `OLLAMA_URL`, `DISTURBANCE_ENABLED`,
`API_TIMEOUT_S`, `USER_COMMAND`, `PICK_FROZEN_SEC`/`PICK_SETTLE_SEC`/`PICK_BOX_WAIT_SEC`(hand‑eye
샘플 타이밍).

> 실로봇 안정 구동에는 ros2-cup-stack 의 RT 셋업(`setup_rt.sh` 1회 + relogin)이 필요하다 —
> `ros2_control_node` 가 SCHED_OTHER 로 떨어지면 mid‑motion 속도 스파이크로 safety‑stop(red
> light)이 난다.

테스트:

```bash
cd cup_stack_agent
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n start.sh
```

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
- 새 작업은 의미 prefix 브랜치(`feat/…`,`fix/…`,`docs/…`,`chore/…`) → `main` 머지·push.
  서브모듈 내부 변경은 의미 prefix 브랜치 후 부모에서 포인터 bump.
- 가짜 실험의 I/O 는 실제 ROS 파이프라인과 동일하게 유지 — 대체 토픽이나 CLI/파라미터
  좌표 전달 금지.
