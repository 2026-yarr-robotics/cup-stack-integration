# cup-stack-integration 시행착오 기록 (trial-and-error)

> 목적: 커밋 이력에서 확인되는 시행착오(되돌림·재수정·추가 후 제거·켰다 끔·파라미터 왕복·설계 피벗·버그)를 한곳에 모은 기록. **README 아님.**
> 방법: 모든 항목은 commit 해시 + verbatim subject 또는 현재 파일 라인 근거. 추정 금지, 근거 없는 것은 `[불명확]`.
> 작성 기준: superproject `loop` HEAD = `25b870f`, `main` = `b25ad4e`. 서브모듈 7개 + nested doosan-robot2 + cup_stack_agent(자체 코드) + `feat/isaac-integration` 분기 전수 조사.

---

## 0. 현재 레포 상태 (검증된 사실)

- superproject HEAD = `loop`(`25b870f` "chore: bump server + ros2-cup-stack -> RT SCHED_FIFO promotion fixes"), main = `b25ad4e`.
- working tree 미커밋 변경: `cup_stack_agent/scripts/{pick_node,plan_executor_node,topic_logger_node,upright_cup_pose_node}.py`, `start.sh` (M); 서브모듈 포인터 5개(outlier-cup-recovery/ros2-cup-stack/ros2-depth-point-cloude/server/vision-node) 이동(m); untracked `docs/revert_plan_0615_server.md`, `docs/system_architecture*.drawio`.
- **06-15 revert 물결은 main 미머지** — 6개 revert PR이 각 레포 `fix/revert-*` 브랜치에만 존재, 절차서 `docs/merge_guide_0616_revert.md`(b14a768)로 순서만 정의된 대기 상태.
- 조사 제약: depth 레포는 grafted(shallow) — PR#1 이전 이력 조회 불가. frontend는 실질 README 부재(Vite 보일러플레이트). vision-node README/summary는 stale.

---

## 1. /stop · Abort · safe-stop · on-demand launch — 06-15 추가 → 06-16 일괄 revert (대기)

하루(06-15)에 4개 신기능이 server·ros2·frontend·agent에 들어갔고, 06-16 새벽 5개 revert 브랜치로 되돌려졌으나 **main 미반영**. 즉 main에는 이 기능들이 여전히 살아있음.

추가:
- server `1e868a2` feat(robot): add POST /api/robot/stop to interrupt the running skill + return HOME — 부수효과로 `_stop_motion`을 실제 서비스 `/dsr01/motion/move_stop`으로 수정(05-29 `c852dee` 이래 `/motion/stop_motion`은 항상 silent-fail이던 버그).
- ros2 `627395d` feat(skill): add POST /stop to interrupt the in-flight skill + return HOME
- frontend `ebe669e` feat(abort): wire Abort button to POST /api/robot/stop (works during skills)
- server `145ea68` feat(robot): auto-recover Doosan safety stop (yellow light) without bringup restart (+POST /api/robot/recover)
- server `bd5fa42` feat(robot): launch cup_stack_agent via host bringup agent (on-demand + clean shutdown) + `5366d73` chore(start): stop auto-launching cup_stack_agent
- agent `0e5dac8` feat(agent): cleanly terminate cup_stack_agent on done

revert:
- server `ff55838` revert(robot): drop POST /stop + host-delegated on-demand agent launch (브랜치 fix/revert-stop-and-bringup-agent) — always-on agent + topic-publish 모델로 복원. 단 `_stop_motion` 수정은 task/stop이 쓰므로 유지.
- server `17dd5c2` revert(robot): drop Doosan safety-stop (yellow-light) auto-recovery (fix/revert-safe-stop-recovery) — 명시 사유: 반응형 자동복구가 근본원인(속도 한계 초과)을 가렸고, 그 원인은 이제 플래닝 레이어 `c346b99`로 예방됨. surgical revert(`_run_skill_call` thin pass-through 유지).
- ros2 `704c378` Revert "feat(skill): add POST /stop ..." (fix/revert-skill-api-stop) — **revert 사유 commit/문서에 없음 `[불명확]`**.
- frontend `a1d0008` Revert "feat(abort): wire Abort button ..." (fix/revert-stopall-abort)
- agent `3b13b96` fix(agent): stop SIGINT-killing the stack on done (always-on restore) (fix/revert-agent-done-shutdown)
- agent `0efeb3e` fix(cup_stack_agent): #11 HOME-on-fail uses pluggable api_url_home, not /stop

부수 회귀: 에이전트 기동 방식이 `a2e47a9`(auto-launch) → `605581d/9dbf697`(vision-relay/LLM split) → `5366d73`(수동) → `bd5fa42`(on-demand) → `ff55838`(다시 auto-launch)로 한 바퀴 회귀.

---

## 2. red-light alarm 1908 / anti-jitter (RT + 드라이버 + 플래너 + 모션, 최소 7커밋)

- ros2 `8a2d7a8` fix(skill): slow LIN profile near the high-Z singularity (z≥0.50, 256°/s 스파이크)
- ros2 `4146c74` fix(skill): lower LIN velocity scaling to clear joint limits near singularities (slow만으론 부족, base도 하향)
- ros2 `e095bb9` fix(skill): up-over motion to clear the pyramid on HOME return and high picks
- ros2 `c346b99` fix(motion): re-time plans that exceed joint velocity limits (prevent red-light alarm 1908) — Pilz LIN이 joint vel 클램프 안 해 손목 323°/s(>225) → post-plan 가드로 0.9× 초과 시 시간 stretch, 6× 초과면 reject.
- doosan-robot2 fork `754f6f9` fix(dsr_controller2): dsr_moveit_controller position-only command interface — JTC position+velocity 동시 command stutter → velocity command_interface 제거 (cup-stack `bd5ccb0`가 이 포크로 repoint)
- doosan-robot2 fork `89b0efa` fix(hw): constant servoj_rt window + wrist velocity limit 3.927 — servo_time을 측정 dt 대신 nominal control period에서 도출, 손목 vel limit 3.2→3.927
- ros2 RT `20b3244` fix(rt): make SCHED_FIFO:80 promotion effective + ship reproducible RT setup (PR#11) — `ros2_control_node`가 조용히 SCHED_OTHER로 돌던 게 진짜 원인. 기존 `promote_rt`의 `sudo chrt`가 백그라운드에서 패스워드 프롬프트로 no-op이고 RLIMIT_RTPRIO 미부여라 실패가 swallow됨. → `chrt -p` 검증 + `setup_rt.sh`(limits.d) + `RT_REQUIRED=1` hard-fail. (이보다 이른 별도 RT 커밋은 없음 — 이전 시도는 `bringup_real.sh` diff 내부에만 존재 `[불명확]`)
- 앱 측 보강: ros2 `dd1a2e1` fix(skill): lift straight to travel height when colinear (중복 zero-velocity decel/accel 제거), `3b590d4` fix(skill_api): poll DRCF to STANDBY before pyramid_step reply (JOINT_SERVO↔movel 충돌로 `eMoveL rejected`, 라이브 3/6 거부)

---

## 3. perception flicker / box-track 안정화 (depth)

bursty 카메라로 마커가 "6→0→6" 깜빡이는 문제와의 반복 싸움:
- `8f57b95` fix(fusion): coast-idle marker republish under bursty camera + rim tuning (rim_keepalive_s 3.5→0.4, rim_obs_max_age_s 3.0→0.8)
- `1cbe389` fix(fusion): rim obs cache + track keepalive age in stream time, not wall time (같은 날 rim_keepalive_s 0.4→**0.9** 재상향)
- `05ed8d9` perf(detection): gate debug overlay on subscribers + cheap mask upsample + thread caps
- `4159635` fix(fusion): identity-first association — (cam,iid)→gid 바인딩으로 gid 플립 제거
- `494f2a2` feat(fusion): rim 가시성 게이트 — truncated mask 스킵 + min_visible 0.80 (rim_min_visible 0.10→0.80)
- `40958bd` fix(fusion): identity binding trusts EXO iids only — hand id-swap hijacked placed cups (place 중 hand ByteTrack id-swap으로 agent loop 6분+ deadlock)
- `90c28cc` feat(fusion): agent-flow scan & lock — cartesian home arrival (joint 매칭이 절대 재발화 안 됨 → cartesian EE로)
- `976f494` fix(point_cloud): exclude fallen-cup class from cups_on_table (fallen이 pickable로 집계돼 `no_upright_cup` 무한루프)
- `7e070b2` fix(perception): vote cup color from a tighter mask core + HSV diagnostic log (파란 컵이 빨강으로 읽혀 top 슬롯용 파랑 소진)
- detection 노드 크래시: `3ea3984` fix(detection): tolerate un-stat-able absolute YOLO weight path (mode 0750 절대경로 `p.exists()` PermissionError)

---

## 4. YOLO 가중치 (sim vs real) + device cuda/'0' 크래시

device='0' int 파싱 크래시가 **3개 레포에서 각각** 발생:
- ros2 `e8a30c1` fix(fallen_cup_detect): default device=cuda ('0' parsed as int -> node crash)
- server `3cdf852` fix(fallen): device cuda not '0' (int-parse crash); confirmed GPU offload (앞 `906be59`의 fix-the-fix)
- outlier `759252e` fix: default YOLO device to cuda → `6bf7a84` fix: pin node device default to cuda (cpu fallback retained)

sim/real 가중치 분기:
- depth `b4857df` feat(launch): fusion에 model_exo/model_hand 오버라이드 인자 → `90ca232`(3-class sim 가중치) → `d32f3d0` feat(yolo): sim 파인튜닝 가중치 v7 교체 — 라이브 전이 달성 (v1/v2는 라이브 0.00, RTX temporal denoiser 미수렴이 진짜 갭) → `f2ce595` docs(params): model_exo/hand 는 REAL 가중치 — sim_*_best 금지 가드 주석
- (isaac 분기) `2ef1c1f` chore: bump playground — sim YOLO 배선 롤백 (라이브 렌더 퇴행) → `7b192c4` sim YOLO v7 배포 (라이브 검증 통과)로 수렴

---

## 5. verifier 슬롯 기하 = 서버 피라미드 미러 (왕복)

- vision-node `891dc7e` fix(verifier): align slot geometry + direction to FastAPI pyramid — `cup_ref_w 0.070→0.078`(앞서 0.078→0.070 내린 걸 복원), `layer_gap 0.002→0.007`, `degree 0.0→90.0`. 서버 `PYRAMID_CUP_SPACING / PYRAMID_LAYER_HEIGHT / DEFAULT_PYRAMID_DEGREE`와 일치 필수.
- `9cbeeab`(greedy max-overlap 슬롯 매칭), `286aba7`(time-based latch), `4d92f59`(slot color-vote latch + release_off 5s), `5d71039`(cp_offset_x/y exo->base nudge; `992f778`의 cp x 0.5→0.450에 더해 런타임 offset 0.03/-0.02)

---

## 6. HOME 자세 통일 (3 레포 공유 원천)

- outlier `0f4180a` fix: root-namespace defaults, real-robot HOME (Pilz PTP), 4-class seg weight (실로봇 `/joint_states` 캡처 라디안 고정) — 앞서 `0ae90da`의 teach-pendant 캡처(`use_current_as_home` true)를 다시 False로 되돌림
- ros2 `948cd03` feat(home): skill_api HOME을 fallen-cup sense pose로 통일
- frontend `5eb5973` fix(home): set HOME move XYZ to FK of fallen-cup-recovery HOME_JOINTS ((0.244,-0.012,0.515); `33b4c4f`(0,0,0.4 도달불가→0.45,0,0.45)→`2be5c40`(0.611,-0.237,0.468)→FK 정합)
- skill-manager `b390278` feat(move): Scan Home 버튼 — measured joints (cartesian 포기, 측정 관절각)

---

## 7. cup_stack_agent 폐루프 — GSP (goal_state_publisher)

역할: 시스템 토픽 구독 → LLM 입력용 단일 payload `/llm_input` 조립·발행(LLM 직접 호출 안 함). 트리거 2개: `/user_command` 새로 들어오면 cold_start, `/action_result` 들어오면 in_flight.

시행착오 연대기(시간순, revert 미통합):
- `04514e2`(06-01) Add temporary LLM integration experiment — GSP 최초 생성(170줄).
- `7f13f48`(06-01) Publish in-flight input after world update — in-flight 즉시 발행 대신 world 갱신 후 발행(pending-action 경로).
- `93c87e3`(06-01) Use last LLM world snapshot for action reflection — reflection 기준선을 직전 world snapshot으로.
- `7d52f46`(06-08) fix(cold-start-planner): judge sufficiency by cup count, not color variety — `{blue:6}` 3단 요청을 color-variety 편향으로 INSUFFICIENT 오판(temperature 0이라 결정론적) → count 기반.
- `7971c41`(06-11) feat(agent): hand-eye pick-point options + agent loop state — GSP 대규모 확장: world freeze(`freeze_world_during_action`), handeye_fallback, future_slot_debounce, pending_fresh_timeout_s(5s). 당시 `unfreeze_settle_s=1.5`.
- `4c0cb7e`(06-12 15:49) feat(agent): fallen-cup recovery as a top-level LLM interrupt — fallen을 `{color:count}` 맵으로 도입, recovery 후 "color count drop"으로 clear 판정.
- `e153517`(06-12 17:51) feat(agent): fallen 판단을 hand-eye 전담으로 전환 (fallen_count 게이트) — **위를 ~2시간 만에 설계 피벗으로 폐기**: exo fallen-class 파생 제거, hand-eye 정수 `fallen_count`로, clear 판정도 "recovery 이후 fresh hand-eye 샘플 도착"으로 교체.
- `1829222`(06-12) tune(agent): unfreeze settle 1.5s -> 0.5s.
- `ddb1713`(06-15 17:02) feat(cup_stack_agent): robustify dynamic loop — reflection hold 상한 `reflect_timeout_s`(10s), done-race 가드 `fallen_settle_wait_s`(3s), pick-fail 라우팅 `_recover_after_pick_fail`/`_pending_pick_fail` 신설.
- `19b6dbc`(06-15 23:17) feat(cup_stack_agent): loop state machine — `publish_on_world_change` 기본 **False→True**(delta-gated), done에서 plan **clear 안 함**(post-done grace), unstack interrupt 분기 추가.
- `94b278c`(06-15 23:51) fix(cup_stack_agent): disable #7 world-change polling (restore step atomicity) — **19b6dbc 부분 revert**: `publish_on_world_change` **True→False**(verifier flicker마다 llm_input flood → decision backlog → step atomicity 붕괴), done에서 다시 `set_plan(None)` plan clear 복귀.
- `7c5cdd3`(06-16) feat(cup_stack_agent): atomic step (#11) — pick-fail 예외 제거(pyramid fail이 더 이상 fallen 노출 분기 안 엶; "pick failed"와 "cup is fallen" 혼동·exo phantom 오발화). **잔여물**: `_recover_after_pick_fail`/`_pending_pick_fail`가 현재 파일에 선언/참조되나 어디서도 True/세팅 안 되는 vestigial 상태.
- `4b9be8f`(06-16) fix(agent): single-run recovery hardening (GSP) — `recovery_freeze_timeout_s`(260s), `max_consecutive_recoveries`(6) anti-runaway, `_recovery_in_flight` 플래그.
- `e3e166d`(06-16) feat(agent): buried color-violation peel + color_check + HITL cold-start fallback — `_apply_color_check_to_payload`(매장된 색-위반 fixable 사실 주입). GSP 최신 커밋.

미병합 곁가지 / 주의:
- scan-freeze 접근 3커밋(`a558c49` hold in-flight /llm_input until the post-action scan freeze, `5ba62ac` GSP 2단계 scan 대기, `1e52bbd` bound the world-reflection hold)은 **`feat/isaac-integration` 브랜치 전용, loop/HEAD 미병합** — 현재 GSP에 `scan_event`/`wait_scan_after_action` 부재. loop 브랜치는 같은 "무경계 reflection hold가 6분+ 정지" 문제를 별도로 `ddb1713`의 `reflect_timeout_s`(10s)로 해결(철학 같고 구현/값 다름).
- `experiment_runbook.md`(436–437행)는 recovery freeze를 240s로 기술하나 `4b9be8f`는 별도 `recovery_freeze_timeout_s=260s` 도입 — 문서 stale 가능성, 코드 우선. `[불명확]`
- `publish_on_world_change`(#7 idle 교란 재발행)은 **현재 기본 False**(비활성), 헬퍼는 inert로 남김.

---

## 8. cup_stack_agent 폐루프 — LLM (llm_node / llm_client / payload_builder / prompts)

역할: `/llm_input` 구독 → Ollama 호출 → `/llm_output` 발행(스킬 실행 안 함, 결정만). payload `mode` 필드로 프롬프트 라우팅(cold_start→cold_start_planner.md, 그 외→inflight_decider.md). 파싱/검증 실패 시 1회 재시도 후 HITL cold-start fallback. 호출 옵션: temperature 0, format:'json', think:False, num_predict(cold 1536/inflight 768).

**모델 기본값 불일치 (코드 위생 이슈)**: `llm_client.py` `DEFAULT_MODEL='gemma4:26b'`(주석 "fastest model that passed the full suite")는 **stale** — 런타임 실제 모델은 `qwen3.6:35b`(`start.sh` `MODEL="${MODEL:-qwen3.6:35b}"` → `-p model:=`, `agent.launch.py` default `qwen3.6:35b`, 프롬프트 헤더도 "qwen3.6:35b 95%"). `gemma4:26b` 주석은 d06bd47 이후 미갱신.

시행착오 연대기(시간순, revert 미통합):
- `04514e2`(06-01) Add temporary LLM integration experiment — 최초 도입("temporary"/"experiment" 네이밍).
- `6277614`(06-01) feat(http_client): add LLM-driven pyramid skill HTTP client — ROS-free blocking 순차 실행 별도 접근. 이후 ROS 노드로 대체되며 폐기된 병렬 시도(`4a89ea4 Remove non-ROS HTTP orchestrator`).
- `3fddf4b`(06-01 16:04) Use no-replan pyramid prompt — inflight decider를 continue/done 2-결과 no-replan baseline으로(무한 replan 루프 디버깅용).
- `1fa7b0b`(06-01 16:05) Revert "Use no-replan pyramid prompt" — **88초 만에 즉시 revert**. replan 능력 복원.
- `2411182`(06-01) Increase robot API timeout → `e35bcec`(06-01) Pass robot timeout as double — timeout 타입(double) 정정.
- `7d52f46`(06-08) fix(cold-start-planner): judge sufficiency by cup count, not color variety — 위 §7과 동일 커밋(프롬프트 측면).
- `6016fdd`(06-09) fix(agent): ollama format:json + num_predict + reasoning cap + strict parse — "model rambled ~5.5KB / 37s and slipped a raw control char" → format:'json' + num_predict(cold 1536/inflight 768) + parse_model_json strict=False + reasoning cap "restore"(≤160자; "restore"는 그 사이 rewrite에서 cap이 유실됐음을 시사, 유실 커밋 `[불명확]`).
- `4c0cb7e`(06-12) feat(agent): fallen-cup recovery as a top-level LLM interrupt — payload `fallen {color:count}` 맵, `validate_fallen_recovery`.
- `e153517`(06-12) feat(agent): fallen 판단을 hand-eye 전담으로 전환 — **`{color:count}` 맵 접근을 ~2시간 만에 폐기**, 색 없는 정수 count로 재설계(§7과 같은 피벗의 LLM 측).
- `76fc88a`(06-15) feat(cup_stack_agent): color-aware planning prompts + validators — `target.slot_colors` 도입, unstack 결정 + top-down/exposed-only 교정, 검증기 추가(+174 테스트).
- `7596c2b`(06-16) fix(perception/loop): cups_on_table excludes fallen-cup ... — inflight 프롬프트에서 **모순 조항 제거**("recover first while cups_on_table shows a cup" → "upright cups always first").
- `02e0b5f`(06-16) fix(prompt): unstack top-exposed color violation every cycle, not just on done — continue 규칙이 직전 성공 step에 anchor → swap된 wrong-color 컵 무시 → continue 진입 전 top-exposed FIXABLE 위반 먼저 검사→unstack.
- `2a4939f`(06-16) fix(agent): hand-eye color default 'red' -> 'unknown' — `classify_color_bgr`가 모호 hue(160-170)에 confident "red" 반환 → red 슬롯 오판 픽 위험 → "unknown".
- `e3e166d`(06-16) feat(agent): buried color-violation peel + color_check + HITL cold-start fallback — buried 위반을 "done(partial)"→peel(top-down 후 refill), `compute_color_check`로 multi-hop 사실 사전계산 주입(no-CoT 모델용), llm_node HITL fallback.
- `7026677`(06-16) feat(agent): don't bury an unfixable color violation (keep_empty) — UNFIXABLE 위반 위 적재 금지, `keep_empty` emit. 한계 명시: "Post-done auto-fix when the color reappears is out of scope".

미병합 곁가지:
- `687cc00`(06-13) feat(agent): LLM_TIMEOUT_S env — GPU 공유 환경 타임아웃 조절 — **`feat/isaac-integration` 전용, HEAD 미조상**. 현재 HEAD llm_node 타임아웃은 하드코딩 120s 그대로, `start.sh`는 model·ollama_url만 주입(`LLM_TIMEOUT_S` 미전달). 그 커밋 body의 "qwen2.5-coder:14b ~1 tok/s, decide 5~6.5분"은 그 곁가지 검증 환경 기준이며 HEAD 기본 모델(qwen3.6:35b)과 별개.

---

## 9. cup_stack_agent 폐루프 — plan_executor

역할: LLM pyramid plan을 받아 **coarse move 절반만** 수행(`POST /api/robot/move {x,y,z}`, z=고정 접근높이 0.45). 정밀 픽/배치는 pick_node(hand-eye fine pick → `/api/robot/skill/pyramid` 직접 호출)가 담당. 이 노드는 pick/place geometry 미보유, skill 서버 미접촉. 유일한 정상 출력 `/move_result`, `/action_result`는 pick_node가 못 내는 2경우(recovery, coarse-move 실패)에만 직접 발행. step 소진 시 `'plan exhausted — awaiting LLM decision'`로 정지(plan은 조언, brain은 LLM).

slot 매핑(`_LLM_TO_API_SLOT`): L1_left→1l, L1_mid→1m, L1_right→1r, L2_left→2l, L2_right→2r, L3_top→3m.

시행착오 연대기(시간순, revert 미통합):
- `04514e2`(06-01) 노드 최초 등장(직접 pyramid skill 호출 시대).
- `51215cf`(06-01) Gate next skill until final lift completes → `689fef2`(06-01) Allow lazy skill API startup before idle gate — idle-gating 도입 후 완화. **이 idle-gating 접근은 035c15b에서 폐기.**
- `035c15b`(06-05) plan_executor: call /api/robot/move instead of pyramid skill — **핵심 설계 피벗**: 직접 pyramid → coarse→fine 분리, `/action_result` 발행 제거(pick_node로 이관), idle-gating param 삭제(−152줄).
- `d460a6f`(06-05) pick_node: drop MoveItPy, pick nearest cup by move_result xy — pick_node MoveItPy 의존(robot_description/namespace 크래시) 제거, **plan_executor가 `/move_result`에 x,y 재추가**(035c15b에서 뺐던 것 re-add).
- `b21bfd9`(06-05) pick_node: raise pyramid API timeout to 180s (was 10s) — 10s read timeout이 skill 도중 포기→다음 시도 HTTP 409 'skill already running'→fail/replan 무한루프 → 180s 직렬화.
- `494e529`(06-10) feat(agent): plan_executor occupied-slot guard + fake-node track confirm/hold — `/stack` 기반 점유 슬롯 즉시 skip. **이 "즉시 skip"이 후에 phantom-skip 유발 → a4fe5b0에서 debounce 보정.**
- `c0b3f7e`(06-10) fix(plan_executor): poll for cup before failing first move (cold-start race) — plan 채택 즉시 첫 move 발사 시 perception 미충전→hard-fail→정지 → `cup_wait_s`(5s) 폴링.
- `4c0cb7e`(06-12) fallen recovery 최초(당시 exo boxes_filtered fallen-cup 트랙 파생, `{color:count}`).
- `e153517`(06-12) **위 exo-파생 방식 폐기**: plan_executor `fallen_counts()`/0.5s 타이머 삭제, hand-eye `/fallen_cups {"count":N}` 구독으로 교체(설계 피벗).
- `e7a1798`(06-12) fix(agent): skip된 step을 건너뛴 성공 결과도 plan을 advance하게 — 점유 슬롯 step을 조용히 drop → head-match만 보면 remaining_steps 동결 → 성공 step이 remaining 안이면 소화하도록.
- `a4fe5b0`(06-15) fix(cup_stack_agent): executor/perception robustness — unstack dispatch interrupt, **stable occupied-skip debounce**(phantom `/stack`이 실제 step skip하던 L2_left desync 보정, `skip_debounce_s` 5s; 494e529 즉시-skip 교정), stale ghost 트랙 skip, done-grace.
- `94b278c`(06-15) disable #7 world-change polling (§7 참조) — per-step atomicity 복원.
- `7c5cdd3`(06-16) atomic step (#11) — coarse move가 step 전진 안 함, pick 확정까지 busy, pick fail 시 **`POST /api/robot/stop`(interrupt+HOME)** 후 같은 step 재시도, `_recover_after_pick_fail` GSP 예외 제거.
- `0efeb3e`(06-16) #11 HOME-on-fail uses pluggable api_url_home, not /stop — **바로 위 `/stop` 사용 되돌림**(skill-interrupt/held-cup 의미라 no-cup pick fail에 부적절 + 실로봇 미검증). HOME-on-fail을 DORMANT 처리(`api_url_home` empty→no-op), param 이름 `api_url_stop`→`api_url_home`.
- `7596c2b`(06-16) — **0efeb3e의 DORMANT를 다시 되돌려** `api_url_home` 기본을 `{api_base}/api/robot/home`으로 재배선(DORMANT→ACTIVE).
- `b25ad4e`(06-16, HEAD 직전) feat(recovery): handle mouth-up cups via outlier recovery — recovery URL `fallen-cup/recovery`→`outlier-cup/recovery`, 폴링 task명 `fallen_cup_recovery`→`outlier_cup_recovery`. "exo stays upright-only → select_cup mouth-up 특수처리 불요"라 단정.

현재 미커밋 변경(in-progress, b25ad4e 단정을 뒤집음):
- `_KNOWN_CLASSES`에 `'mouth-up-cup'` 추가, `select_cup` skip 조건을 `cup.cls == 'fallen-cup'` → `cup.cls in ('fallen-cup', 'mouth-up-cup')`로 변경(주석 "not a pickable upright cup — handled by outlier recovery"). 즉 exo가 mouth-up-cup을 보면 coarse 선택에서 제외하도록 보정 중.

주의/[불명확]:
- on-done SIGINT 종료가 `0e5dac8`(추가)→`3b13b96`(revert) 후에도 현재 파일에 `import os/signal`·`_terminate_process_group`·killpg 센티넬이 존재 → 3b13b96 이후 재도입 커밋 `[불명확]`. `done_grace_s` 기본 0이라 사실상 즉시 종료.
- `_shutdown_agent`/`_finalize_shutdown` 명명이 동작과 불일치 가능(3b13b96 주석: rename follow-up).

---

## 10. recovery exit-code (always-0 → exit1 → os._exit → single-run)

- `75fc368` feat: report fallen-cup recovery failure via process exit code (항상 0 종료 → bool 반환 + OnProcessExit raise)
- `fc51080` fix(exit): one-shot 종료를 os._exit 로 ... (MoveItPy teardown SIGABRT가 성공을 failed로 뒤집음 — **브랜치 feat/sim-gripper-topic-backend만, main 미머지**)
- `e9178ce` feat: tune fallen-cup recovery ... + fix success exit code (main에서 os._exit 독립 재구현 + grip 2.5cm/tilt 8°/sample 2.5s 튜닝)
- `2fa8a08` fix(recovery): single-run per API call — one cup then return for re-decide (mouth-up 무한 re-pick 루프 → 1회 cap; integration 현재 핀)

---

## 11. namespace dsr01 ↔ root 왕복

- ros2 `a769655`(action remap→/dsr01 namespace), `a803cbb`, `a65f903`, `b0c03b7`
- outlier `53a49e7`(bind MoveItPy to robot namespace dsr01) + `836df02`(override planning_scene_monitor joint_state_topic) → `0f4180a`에서 다시 root 기본(`robot_namespace=""`)으로 되돌림

---

## 12. 파라미터 왕복 모음

- gripper open width: ros2 `a139ab8`(75→90)→`0f62d83`(90→80)→`f0559a2`(80→90)
- pyramid: cup_spacing 0.079→0.078, place_z_base 0.323→0.321→0.318, layer_height 0.095→0.093 (server/ros2 양쪽 중복 반영), move_line 150→250mm/s 재상향(`98346bd`)
- motion scaling: ros2 `9555426`(v0.4/a0.2 통일)→`b1b3259`(LIN 0.2/0.1)→`27af924`(acc 0.2→0.08)
- camera payload: server 640→800→640w, q50→q35, 30→15fps (`20ead54`,`f759cb9`,`98bee21`)
- place tilt: outlier 0→25→8° (`0ae90da`,`84d2b85`,`e9178ce`)
- toolcharger_ip: ros2 `51dba7f`(→192.168.137.100)→`7eef59d` revert(→192.168.1.1)
- pyramid API timeout: 10s→180s(`b21bfd9`)

---

## 13. 추가 → 철회된 기능

- server `07fa759`(cup_detection 도메인)→`b64c7e7` 제거(dead path, publishing 노드 없음)
- server `b8a4128`(move_cartesian)→`9f4eb93` disable→`d2b6983` native move_line 대체 / ros2 `80d0e5f`→`e40ad34 remove(move_cartesian)`
- skill-manager `07d4dde`(settled-gate [L])→`9d777c0` remove the Locked-only view — the depth [L] tag is retired(라벨 v2로 [L] 폐기→후보 영구공백 버그)
- frontend `1d6d10e`(카메라 클릭 선택)→`deaeb8c` 전체 제거
- frontend `c83f94c`(fallen detect UI)→`46e5be4` detection start/stop 제거(서버 백그라운드화)
- outlier `1797911`(Y-flip)→`0ae90da`에서 "previous wrist config용 hack"이라며 비활성
- ros2 `28e546c` revert: remove safe-z defaults from fallen_cup_recovery launch
- server `d5f5388`(bringup이 skill_api kill)→`3ebea90` revert(bringup): don't kill skill_api on bringup restart

---

## 14. 구조 / 리네이밍

- `10efbf9` chore: flatten submodules to root, dissolve cup-stack-server (+`86a7f70` 완료 리포트). 이전 `cup-stack-server` 집합 계층 + `yarr-robust-speed-stack` 중복본 해체.
- `d06bd47` Move project sources under cup_stack_agent/
- depth `3a8893a` feat: merge recode_sequence camera package from ros2-recode-sequence + `2b40620` chore: drop ros2-recode-sequence submodule (merged into depth)
- fallen→outlier: `d5f255a`(통합 모듈)·`24abb87`(README 개명), integration 핀 `d1ab412`로 `fallen-cup-recovery@released` → `outlier-cup-recovery@main` 교체, `06949b8`로 전 서브모듈 `branch=main` 통일

---

## 15. feat/isaac-integration 분기 (미머지)

- main에 **미머지**(`origin/main..` ~56커밋). `.gitmodules`/gitlink 충돌로 `git merge` 포기, fork 고유분만 외과적 port(MERGE.md, 현재 tip엔 `1e8fe88`로 제거됨).
- 검증 기록: colcon 31패키지 빌드 PASS, recovery E2E PASS, "3단 쌓아줘" 폐루프는 **"기계 체인 검증 완료"(완주 아님)** — verifier y-바이어스 ~30-40mm 슬롯 어긋남 + qwen3.6:35b 미설치(qwen2.5-coder:14b, decide 1회 5~6.5분 GPU 경합)으로 풀 6컵 빌드 비현실적.
- sim-YOLO: `2ef1c1f`(롤백)→`7b192c4`(v7 재배포 라이브 통과)
- 이 분기 전용이라 운영 브랜치(loop)에 미반영인 것: GSP scan-freeze(`a558c49`/`5ba62ac`/`1e52bbd`), `LLM_TIMEOUT_S`(`687cc00`).

---

## 16. junk / WIP 커밋 (내용 불명확)

`8d15f7e asdf`, vision-node `06dfd4e asdf`/`8485459 asdf`, skill-manager `23628a6 first commit`/`fbfc343 asdf`/`b44044e new feat`, ros2 `372f6db Implement feature X ... bug Y in module Z`/`bcc4e63 control`, server `c1c0eb3`/`5439fda`/`03c885b`/`e333435 chore: update`.

---

## 부록 — vestigial 코드 / 미병합 곁가지 / [불명확] 목록

- vestigial: GSP `_recover_after_pick_fail`/`_pending_pick_fail`(7c5cdd3 이후 세팅 안 됨). plan_executor killpg 종료 경로(3b13b96 revert 후에도 잔존).
- 미병합 곁가지: 06-15 revert 6개 PR(브랜치), outlier `feat/sim-gripper-topic-backend`(c731570/fc51080), skill-manager `fix/pin-all-endpoints-repo-relative`/`feat/fallen-cup-recover-panel`(8605682/b390278), isaac 분기 전체.
- `[불명확]`: ros2 `/stop` revert(704c378) 사유 무기록; reasoning cap 유실 커밋; plan_executor 종료경로 재도입 커밋; recovery freeze 240 vs 260 문서/코드 차이; depth shallow 이전 이력; isaac scan-freeze 미병합이 의도인지 단순 미병합인지.
