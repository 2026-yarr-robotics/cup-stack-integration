# 이번 브랜치 전체 변경 사항 (cup-stack-integration)

이 문서는 이번 세션에서 작업한 **모든 변경**을 빠짐없이 나열한다. 각 항목은
**기존 동작 → 변경 후 → 이유** 순으로 적는다. 작성자 커밋은 모두 `ninejh`.

저장소 구조: 슈퍼프로젝트 `cup-stack-integration` 아래 서브모듈
`vision/ros2-depth-point-cloude`(depth 비전), `vision/vision-node`(verifier),
`cup-stack-server`(로봇 서버). 에이전트 루프는 슈퍼프로젝트의 `cup_stack_agent/`.

전체 에이전트 데이터 흐름:
```
exo 비전(cup_fusion) ─/vision/cups_on_table──┐
verifier ───────────/vision/stack──────────┤
                                            ▼
                                      aggregator_node ──/cups_on_table,/stack──┐
                                                                               ▼
                                                                  goal_state_publisher
                                                                    │ /llm_input
                                                                    ▼
                                                                  llm_node ──/llm_output──┐
                                                                                          ▼
                                                                              plan_executor_node
                                                                    │ POST /api/robot/move (coarse)
                                                                    │ /move_result
                                                                    ▼
                                                                  pick_node ──/hand_eye/boxes(정밀 pick)──> /api/robot/skill/pyramid
                                                                    │ /action_result
                                                                    ▼  (GSP가 world 반영 → 다음 step)
```

---

## A. DEPTH 비전 (서브모듈 `vision/ros2-depth-point-cloude`)

### A-1. (커밋됨, origin/main) 9e9d8e5 통합 + scan joint_states 토픽 수정
- **기존**: 슈퍼프로젝트가 depth를 옛 베이스(`5517444`)로 핀. `cup_fusion_node`의
  scan-lock이 글로벌 `/joint_states`(발행자 0)를 구독해 팔 도착 감지 불가.
- **변경**: 슈퍼 포인터를 `eb25a15`→`9e9d8e5`로 bump(머지 커밋 `02f77a0`).
  `cup_fusion_node`가 `/dsr01/joint_states`(M0609 실제 발행)를 구독하도록
  `scan_joint_states_topic` 파라미터 추가. (color contract + Eunwoo depth-filter/
  offset/live_use_hand 머지 포함.)
- **이유**: floor-fit 폴백/joint 미수신으로 scan-lock·융합이 동작 안 하던 것 해결.

### A-2. (temp 브랜치, 미커밋) fixed-box 모드 — point_cloud 오류 회피
파일: `point_cloud_node.py`, `cup_fusion_node.py`, `config/params.yaml`

- **기존**: 각 컵을 **deproject한 마스크 점군으로 fitting**.
  `_fit_cup_axis_xy`(truncated-cone 비선형 LS) + `_compute_box_world`(OBB/AABB
  폴백) + outlier 제거. per-frame depth 노이즈로 박스 위치/자세가 매우 불안정.
- **변경**: `fixed_cup_box`(기본 True) 모드 추가. 컵이면 fitting을 건너뛰고
  **고정 컵 크기 박스**(`cup_bottom_diameter_m`×`cup_height_m`)를 robust 중심에 배치.
  공용 헬퍼 `_fixed_cup_centroid(pts, cup_radius, base_pct, z_band)`:
  - **XY 중심**: Kåsa 대수 원-fit (가시표면 점들의 raw median 편향 보정). 반경이
    `[0.6,1.6]×cup_r`, median 근처, radial-MAD ≤ 0.3·cup_r, **angular coverage
    ≥120°**(partial arc 방어)일 때만 채택. 아니면 median 폴백.
  - **Z 바닥**: median 주변 **z-band(±z_band/2)** 내 low-percentile (테이블/배경
    마스크 누수 거부).
  - **residual proxy**: known-radius **radial MAD** `median(|dist−cup_r|)` ×
    `√(ref_points/N)` (점 적으면 덜 신뢰). KF measurement noise로 사용.
  - frustum=None → **네모 박스만**(콘 와이어프레임 제거).
- **적용 위치(중요)**: fusion_dual에서 point_cloud는 producer라 박스를 안 만들고
  raw 점군만 넘김 → **실제 `/digital_twin/boxes`는 `cup_fusion._fit`이 생성**.
  그래서 `point_cloud._fixed_cup_state` + `cup_fusion._fit` **둘 다** 적용.
  cup_fusion 트랙 KF는 residual을 `r_diag * infl`로 반영(`kf_residual_inflation`).
- **파라미터 추가**: `fixed_cup_box`, `fixed_cup_min_points`, `fixed_cup_base_pct`,
  `fixed_cup_kf_residual_m`, `fixed_cup_z_band_m`(point_cloud) / 동일 + `kf_residual_inflation`(cup_fusion).
- **이유**: 사용자가 point_cloud fitting 오류가 너무 심하다고 판단 → YOLO 검출 +
  depth 중심만 쓰고 박스는 고정 크기로. 속도는 약간 빨라지고 핵심 이득은 안정성.
- **2회 GPT 리뷰 반영**: ① median→원-fit 중심 보정 ② residual=0 과신 → spread proxy
  ③ 두 노드 fallback 일관성(터미널) ④ Kåsa partial-arc 가드(coverage+radial-MAD)
  ⑤ z-band 파라미터화 / fusion KF residual 실제 반영.

---

## B. 에이전트 — 커밋됨 (`cup_stack_agent`, ninejh)

### B-1. 2c0fbdd — real hand-eye pick + fake→real 리네임
- **upright_cup_pose_node.py (MoveItPy→TF)**:
  - **기존**: link_6 FK를 얻으려 `MoveItPy(node_name="upright_cup_pose_moveit_py")`로
    **두 번째 MoveItPy** 인스턴스화 → skill_api의 MoveItPy와 planning-scene-monitor
    충돌 위험(과거 scan 노드가 같은 이유로 죽음).
  - **변경**: MoveItPy 제거. `tf2_ros`로 `base_link←link_6`를 **TF lookup**.
    `quat_to_matrix` 헬퍼 + `_ee_matrix_from_tf()`. `TransformListener(spin_thread=True)`로
    YOLO 부하에도 TF 신선 유지. None이면 detection 스킵(기존 가드 유지).
  - **이유**: 충돌 근본 제거. dsr가 이미 /tf로 방송하므로 read-only FK로 충분.
- **start.sh**:
  - `VISION_MODE` 토글 `standalone|fusion|fusion_dual`(기본 fusion_dual) — exo
    producer + cup_fusion + (dual이면) hand producer + eye-in-hand 정적 TF.
  - `HAND_EYE_MODE` 토글 `real|fake`(기본 real) — real이면 upright_cup_pose,
    fake면 기존 fake_hand_eye. `fake_*`는 보존.
  - `launch_quiet()` 헬퍼 — upright는 **파일 전용 로그**(공용 콘솔 미오염, LLM 피드 보존).
  - `aggregator_node`/`digital_twin_stabilizer_node` 새 파일 사용.
- **신규 파일(리네임)**: `aggregator_node.py`(= 옛 fake_aggregator, 하드코딩 주입 없는
  real relay), `digital_twin_stabilizer_node.py`(= 옛 fake_digital_twin, median
  stabilizer). 옛 `fake_*` 파일은 **그대로 보존**.
- **run_upright_cup_pose.sh**: upright 단독 실행 래퍼.
- **pick_node.py**: 인위적 추론시간 대기 제거 — `box_wait_sec`(1.5s)를 항상 꽉
  채우던 것을 **첫 hand-eye 마커(xy) 도착 즉시 break**(실제 추론시간으로 바로 이동).

### B-2. c0b3f7e — plan_executor cold-start cup-wait
- **기존**: LLM 플랜 채택 직후 `_execute_next`가 즉시 첫 move. 그 순간 perception
  (`boxes_filtered`→`_cups`)이 아직이면 `tracked=0`으로 **하드 실패 → 루프 정지**.
- **변경**: `_do_move`에서 매칭 컵 0개면 `cup_wait_s`(5s) 동안 0.1s 폴링(락은 매
  시도 사이 해제). 컵 잡히면 진행, 끝까지 없으면 fail.
- **이유**: cold-start race로 첫 step이 멈추던 것 해소.

---

## C. 에이전트 — 미커밋 (이번 세션 핵심 작업)

### C-1. plan_executor — occupied-slot 가드 (기존 작업, 8-step spec)
- `/stack` 구독 + `normalize_stack`/`stack_slot_occupied`/`drop_occupied_steps`.
  이미 채워진 pyramid slot 대상 step을 skip(`_adopt_plan`/`_execute_next`),
  `_do_move`에 occupied 가드. (B-2의 cup-wait과 함께 들어감.)

### C-2. plan_executor — hand-eye 위치 폴백 + graspable 개수 발행
- **기존**: coarse move는 exo 위치(`boxes_filtered`→`_cups`)만 사용. exo 0이면 이동 불가.
- **변경**:
  - `/hand_eye/boxes` 구독 → `_handeye_cups`(base_link xy + color) 파싱.
  - `_graspable_handeye`: exo `select_cup`과 **동일한 slot 위치 + `stack_exclude_radius_m`
    제외** → **build/stack 영역(placed) 컵 절대 미포함**. TTL(`handeye_ttl_s`)로
    stale 컵 drop. id sort(결정적).
  - **위치 폴백**: `_do_move`에서 exo 빈 채로 `handeye_fallback_grace_s`(0.5s) 지나면
    즉시 graspable hand-eye 컵 xy로 coarse move(cup_wait 5s 안 기다림). hand-eye
    선택 id는 **negative namespace**(exo track id 충돌 방지).
  - **개수 발행**: graspable 색상별 개수를 `/vision/cups_on_table_handeye`(0.5s)로.
- **이유**: exo miss 시 hand-eye로 보충. stack 컵 제외는 사용자 강조.
- **GPT 리뷰 반영**: per-cup TTL, grace 분리(느림 해소), negative id, 결정적 정렬.

### C-3. aggregator — cold-start 게이트 + flicker debounce (hand-eye는 제거됨)
- **진화 과정**:
  1. cold-start 게이트: exo 컵이 처음 잡힌 뒤 `command_settle_s`(3s) 지나 `/user_command`
     발행(빈 테이블 cold_start 방지).
  2. (한때) exo=0이면 hand-eye 보충 + 침묵 타이머 → **사용자 의도 위반(상시 켜짐)으로 제거**.
  3. **flicker debounce**: exo가 순간 0이면 직전 비-0값을 `zero_debounce_s`(1.0s) HOLD,
     **지속 0**일 때만 `{}` 발행.
- **최종 동작**: exo `/vision/cups_on_table` relay + 0의 지속성 debounce. **hand-eye 없음.**
- `_cup_count`는 bool 제외(int subclass 방어, GPT P3).
- **이유**: 순간 노이즈 0이 LLM에 곧장 도달해 잘못 판단하는 것 방지.

### C-4. goal_state_publisher — world freeze (pyramid 수행 중 동결)
- **기존**: action_result 후 `action_result_reflected`로 perception 변화 확인 후
  반영. 단 수행 중 노이즈가 그 확인을 오작동시킬 수 있음.
- **변경**:
  - `/llm_output`이 **실행할 step이 있는 액션**(`current_goal()` 존재)이면 → `_freeze_world`.
    `done`/빈 plan → unfreeze. (cold-start/replan/**continue** 통일 — continue 미처리로
    수행 중 unfreeze 되던 버그 수정.)
  - freeze 중 `/cups_on_table`·`/stack` 무시(world 고정).
  - `/action_result`(팔 home 도착) → `_schedule_unfreeze`: `unfreeze_settle_s`(**1.5s**)
    뒤 unfreeze(노이즈 안정화 대기).
  - **wall-clock 타이머**(`_freeze_tick`, 0.2s)가 settle deadline/`freeze_timeout_s`(60s)
    검사 → perception 멈춰도 보장. `_world_frozen()`은 side-effect 없는 순수 게이트.
- **이유**: 수행 중 perception 흔들림이 world/반영체크를 오염시키는 것 차단.
- **GPT 리뷰 반영**: continue 경로 freeze, 빈 step freeze 방지, timer 기반 timeout, settle 1.5.

### C-5. goal_state_publisher — hand-eye 폴백을 "결정 시점"에만
- **기존(잘못된 1차 구현)**: aggregator가 상시 hand-eye 보충 → exo 정상 rate(1.1Hz)
  간격을 "침묵"으로 오판해 hand-eye 상시 발동(사용자 지적).
- **변경**: aggregator에서 hand-eye 완전 제거(C-3). hand-eye는 **goal_state의 결정
  시점**(=`_publish` 호출: cold-start / pyramid 후 UNFREEZE+settle)에만:
  - `/vision/cups_on_table_handeye` 구독 → `_handeye_counts` + TTL(`handeye_ttl_s` 1.5s).
  - `_apply_handeye_to_payload(payload)`(GPT P2): exo cups_on_table 총 0이면 **payload의
    cups만 hand-eye 개수로 override**. builder를 안 건드려 **previous_world_state
    baseline 오염 없음**.
- **이유**: 사용자 의도 — hand-eye는 "pyramid 후 home 이동 시점에 exo 비었을 때만",
  상시 아님.

### C-6. goal_state_publisher — P1 fresh-cups 게이트
- **기존**: `_on_stack` 틱도 `_maybe_publish_pending_action` 호출 → UNFREEZE 후
  `/stack`이 먼저 오면 **stale 이전 cups**로 publish(hand-eye도 미적용).
- **변경(GPT P1)**: `_maybe_publish_pending_action`이 **action 이후 fresh
  `/cups_on_table`이 적용된 뒤**에만 진행(`_cups_seen_ns ≥ _pending_action_at_ns`).
  perception 멈춤 대비 `pending_fresh_timeout_s`(5s) 후 last world로 진행 + warn.
- **이유**: 결정이 항상 settle 후 fresh exo 샘플 위에서 일어나게.

### C-7. goal_state_publisher — future-slot 조건부 debounce
- **기존**: raw `/stack`을 그대로 반영. step 5(L2_right) 성공 직후 step 6(L3_top)
  미실행인데 raw stack에 L3_top=blue가 잠깐 들어오면 `filled_slots=6` →
  LLM `done` → validator가 "remaining_steps 안 비었다"로 drop → step 6 영구 미실행.
- **변경**(`payload_builder.remaining_slots()` + GSP `_debounce_future_slots`):
  - **현재/방금 놓은 slot**(remaining에서 pop됨) → 즉시 반영.
  - **next step slot**(`current_goal().target_slot`) raw occupied:
    - pending<3s → `_next_slot_blocked=True` → 결정 차단(다음 step 안 나감).
    - 3초 내 null → false-positive로 진행.
    - 3초 stable → commit(occupied) → publish → LLM replan/done/skip 판단.
  - **later future slots** → null masking(pending), 차단은 안 함.
  - 실행된(=future 아닌) slot의 pending 자동 정리. `future_slot_debounce_s`(3.0s).
- **이유**: future slot false-positive로 done에 빠지는 것 차단. 단 next_slot이 진짜
  occupied면 충돌/중복 place 막으려 실행 지연(GPT 리뷰 반영).

### C-8. 프롬프트 — 부분 빌드 + count-check 복구
- **cold_start_planner.md**:
  - **기존**: "총 컵수 < cup_budget이면 insufficient_resources" 거부.
  - **1차 변경(부분빌드)**: "가능한 만큼 부분 빌드, total 0만 insufficient" — 그런데
    강한 count-check 문장을 빼서 LLM이 `{blue:6, 나머지 0}`를 보고 "zero cups"로 오판.
  - **최종**: **COUNT FIRST** 복구 — "total_cups = cups_on_table 값들의 SUM, 0짜리/
    distinct 색상 무관, `{blue:6, others 0}`는 6개지 0 아님. total>=budget→풀,
    0<total<budget→부분(ok), total==0(또는 요청 색 0)만 insufficient, **total>0이면
    절대 insufficient 안 함**." (색상은 사용자 뜻대로 **그대로 유지**, 0-count 필터 안 함.)
  - step count = `min(cup_budget, available)`(부분 허용).
- **inflight_decider.md**: 컵 소진(어느 색도 count>0 아님)이면 **done(out of cups)**,
  무한 replan 금지.
- **이유**: 부족해도 일단 수행, 진짜 0일 때만 멈춤(사용자 의도). 헛소리 차단.

### C-9. validator (llm_client.py / llm_node.py) — 부분 plan 허용 + 가드레일
- **기존**: `validate_cold_start`가 `len(steps) != cup_budget`이면 reject → **부분
  plan을 막음**. insufficient는 구조만 검사(컵 있어도 통과).
- **변경**:
  - `validate_cold_start(resp, payload=None)` — **부분 plan 허용**:
    `1 ≤ len(steps) ≤ min(cup_budget, available)`.
  - **semantic 가드레일**: 색 제약 없으면 → total>0인데 insufficient면 reject.
    색 제약 있으면 → **요청 색의 available count가 >0일 때만** reject(요청 색 전부 0이면
    insufficient 허용 = 프롬프트와 일치).
  - 색 단어는 **`_COLOR_ALIASES`(멀티문자 EN+KR)** — `'파'/'노'/'검'` 1글자 root 제거
    (파라미드/노력/검출 false-match 방지). count 합산 **bool 제외**.
  - `llm_node.py`: `validate_cold_start(parsed, payload)`로 payload 전달.
- **이유**: 부분빌드를 막던 핵심 검증 수정 + LLM 오판(컵 있는데 insufficient) 차단.
  (#1 0-count 색상 필터링은 사용자 뜻대로 **안 함**.)

---

## 적용/실행 주의
- 프롬프트·validator·노드 코드는 **llm_node init / import 시 1회 로드** → **agent
  재시작해야** 적용.
- 로봇 모션은 별도 — `cup-stack-server/server/bringup_real_31.sh <로봇IP>`(또는
  대시보드 bringup 버튼)로 Doosan 드라이버(`/dsr01/motion/*`)를 올려야 move 성공.
- depth `temp` 브랜치(fixed-box)는 빌드 필요: `colcon build --packages-select depth_digital_twin`.

## 미커밋 파일 요약
- **depth `temp`**: `point_cloud_node.py`, `cup_fusion_node.py`, `config/params.yaml`
- **agent main**: `prompts/cold_start_planner.md`, `prompts/inflight_decider.md`,
  `aggregator_node.py`, `goal_state_publisher_node.py`, `llm_client.py`,
  `llm_node.py`, `payload_builder.py`, `pick_node.py`, `plan_executor_node.py`
