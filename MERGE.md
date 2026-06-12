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

---

## 후속 이식 2차 (2026-06-12, A–E 포트 이후 fork 추가분 + main 합류)

A–E 포트(`f7b01e4`) 이후에도 fork와 원본 main 양쪽이 전진해 2차 작업을 수행했다.

### 1) origin/main 합류 (`ea3855d`)

원본 main이 base `07c3d1b`에서 `59f31e8`로 전진 (staged speed-up, hand-eye pick
robustness). 슈퍼프로젝트 머지 시 gitlink 충돌 2건은 **각 서브모듈 피처 브랜치에
origin/main을 머지**해 해소 — 브랜치 SHA를 보존하면서 양쪽 작업을 모두 핀:

| 서브모듈 | 충돌 (브랜치 vs main) | 해소 핀 |
|---|---|---|
| `ros2-cup-stack` | `c1a3e2f` vs `4fc2983`(fast profiles) | `e45c3c6` (머지, 무충돌) |
| `server` | `9dbf697` vs `f10e964`(movel speed) | `9bbf7fd` (머지, 무충돌) |

### 2) 포트 이후 fork 추가분 이식 (핀 bump 커밋 1건)

| 서브모듈 | 핀 | 내용 |
|---|---|---|
| `yarr-isaac-playground` | `94c293c`→`090f2ff` | isaac 4커밋 cherry-pick `-x`: occlusion reset 렌더 fix(`22022ea`), occlusion_stand reset(`bc5283b`), layer-height seating grid(`a44f6bc`), C++ 브리지 카메라 writer 실시간화(`5fe9fda`, start_isaac.sh auto-merge) |
| `ros2-depth-point-cloude` | `b79c3e6`→`3ea3984` | origin/main 그대로 (coast-idle republish, YOLO weight path) |
| `vision-node` | `47f2e79`→`992f778` | origin/main 그대로 (verifier cp 0.450) |

### 3) 1차 포트 누락 수정 (`090f2ff`에 포함)

`config/sim_params.yaml`의 `robot.urdf`가 구구조
`../cup-stack-server/ros2-cup-stack/...`를 가리키고 있었다 — flatten-paths가
start/stop_isaac.sh만 재매핑하고 yaml은 놓침. `robot_loader.py`가
PLAYGROUND_ROOT 기준으로 resolve하므로 신구조에서 URDF 로드 실패였을 것.
`../ros2-cup-stack/...`로 수정, resolve 확인.

### 검증 / 미검증 (2차)

- ✅ cherry-pick 충돌 없음(`start_isaac.sh` auto-merge 내용 검수), `bash -n` 통과
- ✅ 머지 파일 `py_compile` 통과 (runtime.py, place_cup_at.py, robot.py)
- ✅ urdf 경로 신구조에서 resolve 확인
- ✅ **colcon 빌드**: ros2-cup-stack ws 26패키지 + depth(3)/vision-node(1)/skill-manager(1)
  전부 성공 (stderr는 doosan 벤더 deprecation 경고뿐)
- ✅ **Isaac Sim 통합 런타임 1회 실행** (`script/start_isaac.sh`, 2026-06-12):
  - Isaac 실시간 동기 유지 (`lag≈0s`, render 19–24ms, phys 3–4ms, `cam=C++writers`)
  - URDF 신구조 경로 로드 성공 (3)의 수정 없이는 로드 실패였음
  - 토픽 계약 정상: `aligned_depth_to_color/image_raw` 16UC1 4.3Hz (depth-noise 창),
    `/digital_twin/boxes` 10Hz, `/vision/stack` 14Hz, `/gripper/{target_width,width}` (SimRG)
  - DRCF 에뮬레이터 + 컨트롤러 3종 active, `GET /api/robot/position` 응답 정상
  - 비고: dsr_moveit_controller 이중 spawner의 "can not be configured from 'active'"
    에러 1회 — 최종 상태 active, 무해 (스크립트+런치 양쪽 spawn에 의한 기존 동작)
- ✅ cup_stack_agent 단위테스트 25/26 — 실패 1건(`test_stabilizer`의 `aggregate`
  import)은 origin/main 기존 결함 (`494e529` 리팩토링 때 심볼 제거, 본 통합과 무관)
- ⚠️ server pytest는 로컬 환경 문제로 미실행 (httpx2 부재, pytest 6.2.5 ↔ anyio 4.13 비호환)

### main 머지 전 참고

- 떠 있던 docker compose 스택이 rename 이전 경로에서 기동된 상태였음 → 검증 중
  `up -d`로 신구조 경로 기준으로 재생성됨 (nginx/robot 재생성, handineye/handtoeye 유지)
- origin/main의 `test_stabilizer` import 결함은 별도 수정 권장

---

## 후속 이식 3차 (2026-06-12 밤) — A–E 포트 누락 1건 + fusion.rviz 정리

### 1) cup_stack_agent start.sh WITH_VISION/WITH_LLM 분리 누락 (`6faab74`)

**증상**: start_isaac.sh의 vision-relay 창(WITH_LLM=false)이 Ollama 모델
체크(`qwen3.6:35b` 미설치)에서 ERROR 종료 → hand-eye 보정 노드
(`upright_cup_pose`)가 아예 뜨지 않음. "hand 보정이 YOLO 모델을 못 찾는다"로
보였던 문제의 실체 (2차 런타임 검증에서도 vision-relay 창은 미점검 — 누락).

**원인**: fork `cee9dea`(그룹 분리 + vision-only Ollama 스킵)가
**cup_stack_agent — 통합 리포 자체 코드 — 를 건드려 서브모듈 중심의 A–E 포트
에서 빠짐**. YOLO 경로 자체는 정상이었다: detection_node의 stat 불가 절대경로
폴스루 fix는 depth 핀 `3ea3984`에 포함, start_isaac.sh의 HAND_EYE_WEIGHTS
로컬 명시 경로도 유효(가중치 .pt는 git 추적 파일).

**이식**: upstream start.sh가 독립 전진(centroid pick, 색 tie-break,
recovery/freeze, DDS export)해 cherry-pick 충돌 → 수동 포트로 양쪽 보존.
aggregator `user_command` 작은따옴표 yaml 트릭도 함께 (USER_COMMAND=' ' →
None 파싱 사망 방지). 검증: `bash -n` + 양그룹 false 스모크런 (Ollama 스킵
확인, exit 0).

### 2) fusion.rviz 비활성화 (`25d8ea2`→핀 bump `c98ec1a`)

fusion 뷰 통합으로 start_isaac.sh vision-fusion 창을 `rviz:=false`로. launch
기본값(true)은 실기·수동용으로 유지 (`script/vision_rviz.sh VIEW=fusion`).

---

## 4차 (2026-06-13 새벽) — fallen-cup-recovery Isaac 통합 + E2E 검증

### 통합 구현 (Task #1)

| 리포 | 브랜치 | 내용 |
|---|---|---|
| fallen-cup-recovery | `feat/sim-gripper-topic-backend` (신규) | `gripper_backend` 파라미터(onrobot/topic/none, sim 플래그와 분리) + TopicRG(/gripper/target_width, 1/10mm→mm); one-shot 종료를 `os._exit`로 (MoveItPy teardown 크래시가 성공을 failed로 뒤집던 실측 버그) |
| ros2-cup-stack | `feat/sim-gripper-backend` | recovery wrapper launch에 env-기본값 패스스루: gripper_backend / place_cup_tilt_deg / pyramid_config_url(ROBOT_API_BASE 핀) / avoid_upright_cups / sim_cup_* |
| server | `feat/vision-relay-llm-split` | bringup_agent: colcon install 경로 폴백(ros2/install), task rc 레이스 수정(stdout EOF 직후 poll()=None→failed 오판); compose FALLEN_CUP_WEIGHTS 패스스루 |
| playground | `fix/flatten-paths` | bridge 기울기 조건부 attach(yaw 보존)+2-경로 release+wobble 연출(확률 샘플, 물리 미개입); skill-api 창→bringup-agent 창(:8099, lazy skill_api + recovery 전 stop = MoveItPy 경합 해소); 랜덤 yaw; GT 씬; fallen_clear; verify_recovery.py |

### 검증 0 — recovery E2E: **PASS** (`tools/verify_recovery.py`)

전도(ArUco 밖 0.29,0.20, z=0.038) → fallen-pose attach(tilt 97°, 스냅 없음)
→ FixedJoint 캐리(z 최대 1.0m) → stand 모션 → tilted release(166°) →
wobble STAND 샘플 → **직립 (0.315,0.172,z=0.003) kinematic** → HOME 복귀
→ task=idle. 그리퍼는 TopicRG(`[topic-gripper] target_width 70.0mm`) 경로.

**알려진 한계 2건 (후속 작업):**
1. **YOLO sim 도메인 갭**: 가용 .pt 전부가 시뮬 렌더의 전도 컵을
   fallen-cup으로 분류 못함 (직립 분류는 정상; 0609_exo가 가장 근접하나
   hand 뷰에서 빨간 직립 컵을 fallen으로 오검). 검증 툴은 GT-주입 모드
   (sim_cup_*)로 인식만 우회 — **sim 캡처 파인튜닝 필요**. 같은 이유로
   /hand_eye/boxes(3class)의 과검출이 유령 upright 장애물을 만들어
   planning을 깨므로 Isaac은 FALLEN_CUP_AVOID_UPRIGHT=false.
2. **stand release 기울기 충실도**: 실기 기대(~20°)와 달리 release가
   mouth-down 166°로 끝남 — sim_cup_yaw/그립 방향 계약 확인 필요
   (outcome 확률 모델이 최종 포즈를 보정하므로 결과는 정상).

### 검증 1 — "3단 쌓아줘" agent 폐루프: **기계 체인 검증 완료**

qwen3.6:35b 미설치 → qwen2.5-coder:14b + LLM_TIMEOUT_S=600 (GPU 를 Isaac
렌더와 공유, 생성 ~1 tok/s → decide 1회 5~6.5분).

관찰된 동작 (씬: 검증 0 의 recovery 팔 스윕이 만든 난장판 — 직립 3·전도 3):
- cold_start 플랜(3스텝) 채택 → coarse /move → hand-eye fine pick →
  **slot 1l 정시팅** `released cup_4 seated at (0.450,-0.078,0.003)` —
  서버 lazy skill_api(bringup-agent 경유) 포함 전 체인 ✓
- step fail → **in_flight replan** → 새 플랜으로 계속 ✓ (폐루프)
- 매달린 컵의 off-grid release(z=145mm, 격자이탈 41mm>tol 35) →
  **비상 divert 가드 작동** `emergency seating → (0.343,0.344)` ✓
- 두 번째 placement **slot 1r 정시팅** (0.450,+0.078,0.003) ✓

**정체 지점 (후속 작업)**: verifier 의 슬롯 라벨이 비전 y-바이어스
(~30-40mm)로 한 칸 어긋남 (실제 1l/1r → /stack L1_L/L1_M) → GSP 의
`action_result_reflected` 게이트(정확한 슬롯 요구, 무한 대기)가 안 풀려
루프가 멈춤. + LLM decide 가 GPU 경합으로 분 단위 — 풀 6컵 빌드는
비현실적. 재현/관찰엔 충분, 완주엔 vision 바이어스 튜닝 + 전용 LLM 필요.

### 수정 항목 검증
- **upright 랜덤 yaw**: spawn/reset 모두 적용 — hand 캠 캡처에서 로고
  방향 제각각 확인 ✓
- **GT 씬**: `gt` 모드/버튼 — 6컵이 MEASURED_CUPS 좌표
  (0.25/0.35 × ±0.2/0)에 정확 배치 확인 ✓

### 3) ROS_LOCALHOST_ONLY 비호환 — 2번째 근본 원인

1)의 이식 후에도 relay가 데이터를 못 받았다. upstream start.sh의
`export ROS_LOCALHOST_ONLY=1`(실기 전 참여자 공통)이 원인 — Isaac 스택의
다른 창들은 미설정이라 lo-only 참여자에게 데이터 경로가 안 열린다 (토픽
이름은 보이는데 /vision/* 10Hz가 relay 컨텍스트에서 0Hz; fork start.sh엔
이 줄 자체가 없어 fork에선 미발현). 기본값으로 완화하고 start_isaac.sh
vision-relay/agent 창이 `ROS_LOCALHOST_ONLY=0`을 넘긴다.

**라이브 검증** (사용자 세션에서 relay 재기동): Ollama 스킵, YOLO 로드
(`10.4M params`), `/cups_on_table` 10Hz 중계, `/hand_eye/boxes` 3.9Hz,
hand 캠 `cups=7 base=7 published=7` — hand-eye 보정 체인 정상.
