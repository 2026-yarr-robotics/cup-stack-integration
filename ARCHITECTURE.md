# 컵 쌓기 시스템 — 전체 파이프라인 아키텍처

YARR 컵 쌓기 로봇의 end-to-end 설계: 실제 depth 카메라 비전 파이프라인이 LLM 계획
에이전트에 정보를 주고, 에이전트가 Doosan 로봇팔을 구동해 3단 컵 피라미드를 쌓는다.
이 문서는 두 ROS 2 워크스페이스에 걸친 전체 시스템의 단일 진실 소스(source of truth)다.

> 상태: 실제 비전 파이프라인 (GT 주입 아님). Intel RealSense D435I(exo 뷰)에서 라이브
> 검증 완료 — 빨강·파랑 컵 검출, 위치 보정, 월드상태 relay 확인.

---

## 1. 두 개의 워크스페이스

시스템은 독립적으로 source되는 두 개의 ROS 2(Humble) 워크스페이스이며, 서로
**오직 ROS 토픽으로만** 통신한다 — §3의 토픽 계약이 인터페이스의 전부다.

| 워크스페이스 | 경로 | 역할 |
|-----------|------|------|
| **Vision** | `ros2-depth-point-cloude`(pkg `depth_digital_twin`) + `vision-node`(pkg `cup_stacking_verify`) | 카메라 → 컵 검출, 3D 위치, 색상, 스택 점유 |
| **Agent** | `cup-stack-integration`(pkg 디렉터리 `cup_stack_agent`) | LLM 계획 + 실행 + 로봇 API 호출 |

카메라 기동은 세 번째 워크스페이스(`ros2-recode-sequence`, pkg `recode_sequence`,
launch `cameras_only.launch.py`)에 있다.

---

## 2. 전체 시스템 맵

```
┌─ 카메라 ───────────────────────────────────────────────────────────────────┐
│ RealSense D435I "exo"  (cameras_only.launch.py view:=exo)                   │
│   /exo/exo/color/image_raw , /exo/exo/aligned_depth_to_color/image_raw      │
└───────────────────────────────┬────────────────────────────────────────────┘
                                 ▼
┌─ VISION 워크스페이스 ──────────────────────────────────────────────────────┐
│ world_origin_node   ArUco id 0 → static TF  exo_color_optical_frame→world   │
│ detection_node      YOLO-seg (upright/fallen-cup) + ByteTrack id            │
│                       → /digital_twin/detections                            │
│ point_cloud_node    deproject + 절두원뿔 피팅 + HSV 색상 + EMA/lock         │
│                       → /digital_twin/points        (PointCloud2, world)    │
│                       → /digital_twin/boxes         (MarkerArray, raw)      │
│                       → /vision/cups_on_table       (JSON {색:개수})        │
│ boxes_to_detections → /detected_cups (Detection3DArray)                     │
│ verifier_node       피라미드 기하 vs 검출 → 슬롯 점유 판정                  │
│                       → /vision/stack               (JSON {슬롯:색|null})   │
│                       → /stack_track_ids            (Int32MultiArray)       │
└───────┬─────────────────────────────┬──────────────────────────┬───────────┘
        │ /digital_twin/boxes (raw)    │ /vision/cups_on_table     │ /vision/stack
        ▼                              ▼  /stack_track_ids         ▼
┌─ AGENT 워크스페이스 (cup_stack_agent) ─────────────────────────────────────┐
│ fake_digital_twin_node  (DigitalTwinStabilizerNode)                         │
│   track별 1초 윈도우 median → /digital_twin/boxes_filtered                  │
│                                                                             │
│ fake_aggregator_node    (AggregatorNode)                                    │
│   relay /vision/cups_on_table→/cups_on_table, /vision/stack→/stack          │
│   + /user_command 발행                                                      │
│        │                                                                    │
│        ▼                                                                    │
│ goal_state_publisher_node   월드상태 구성 → /llm_input                      │
│        ▼                                                                    │
│ llm_node    Ollama → /llm_output (계획: color + target_slot)               │
│        ▼                                                                    │
│ plan_executor_node                                                          │
│   /llm_output(color, slot) + /digital_twin/boxes_filtered(x,y)             │
│        + /stack_track_ids(이미 적재) 읽음 → select_cup(color)              │
│   POST /api/robot/skill/pyramid {x, y, slot} → /action_result              │
└─────────────────────────────────────────────┬───────────────────────────────┘
                                               ▼
                                    로봇 skill API → Doosan 로봇팔
```

`plan_executor`에서 두 갈래가 합류한다:
- **계획 lane** (무엇을 할지): 월드상태 개수/스택 → aggregator → GSP → LLM → 계획
  (`color` + `target_slot`). **LLM은 좌표를 보지 않는다.**
- **기하 lane** (어디를 집을지): raw 컵 위치 → stabilizer → `plan_executor`가 선택된
  색상의 x,y를 찾아 로봇에 전달.

---

## 3. 토픽 계약 (인터페이스)

| 토픽 | 타입 | 발행자 | 소비자 | 페이로드 |
|------|------|--------|--------|----------|
| `/exo/exo/color/image_raw` | sensor_msgs/Image | realsense | detection, point_cloud | RGB 1280×720 |
| `/exo/exo/aligned_depth_to_color/image_raw` | sensor_msgs/Image | realsense | point_cloud | 정렬된 depth |
| `/digital_twin/detections` | depth_digital_twin_msgs/SegmentedObjectArray | detection_node | point_cloud_node | class, instance_id, mask |
| `/digital_twin/points` | sensor_msgs/PointCloud2 | point_cloud_node | RViz | 컬러 클라우드, `world` |
| `/digital_twin/boxes` | visualization_msgs/MarkerArray | point_cloud_node | **stabilizer**, boxes_to_detections, RViz | raw 컵별 마커 |
| `/digital_twin/boxes_filtered` | visualization_msgs/MarkerArray | **stabilizer** | **plan_executor** | 보정된 컵별 마커 |
| `/vision/cups_on_table` | std_msgs/String (JSON) | point_cloud_node | **aggregator** | `{색: 개수}` (적재분 제외) |
| `/detected_cups` | vision_msgs/Detection3DArray | boxes_to_detections | verifier_node | 컵별 3D 박스 |
| `/vision/stack` | std_msgs/String (JSON) | verifier_node | **aggregator** | `{슬롯: 색|null}` |
| `/stack_track_ids` | std_msgs/Int32MultiArray | verifier_node | **plan_executor**, point_cloud_node | 피라미드 내 track id |
| `/cups_on_table` | std_msgs/String (JSON) | **aggregator** | goal_state_publisher | relay된 개수 |
| `/stack` | std_msgs/String (JSON) | **aggregator** | goal_state_publisher | relay된 점유 |
| `/user_command` | std_msgs/String | **aggregator** | goal_state_publisher | 예: `3단 피라미드 쌓아줘` |
| `/llm_input` | std_msgs/String (JSON) | goal_state_publisher | llm_node | 월드상태 + mode |
| `/llm_output` | std_msgs/String (JSON) | llm_node | plan_executor, goal_state_publisher | 계획 / 판단 |
| `/action_result` | std_msgs/String (JSON) | plan_executor | goal_state_publisher | 스텝 결과 |

`/digital_twin/boxes[_filtered]`의 마커 라벨 포맷(`plan_executor.parse_label`이
파싱): `#<id>_c=<color>_<class>_<score>`, 예) `#94_c=red_upright-cup_0.99`;
lock된 track은 `[L]` 접두사.

---

## 4. Vision 파이프라인 노드

### world_origin_node  (`depth_digital_twin`)
ArUco 마커 id 0(DICT_4X4_50)를 검출, ~30 샘플 평균 후 static TF
`exo_color_optical_frame → world`(world = 로봇 base, X-전방/Y-좌/Z-상) 발행.
마커는 base 기준 `(0.367, 0.003, 0.0) m`, yaw `-90°` 오프셋(`params.yaml` 설정).
15초간 마커 미검출 시 depth 평면 피팅으로 폴백.

### detection_node  (`depth_digital_twin`)
YOLO 세그멘테이션(클래스 `upright-cup`, `fallen-cup`) + Ultralytics ByteTrack
(`persist=True`) → 안정적 `instance_id`. `/digital_twin/detections`(mask + class +
id + score)를 카메라 프레임으로 발행. 여기선 색상 없음.

### point_cloud_node  (`depth_digital_twin`)
핵심 인식 노드.
- 각 mask를 `world` 프레임 포인트클라우드로 deproject (프레임별 TF 조회).
- upright 컵은 **절두원뿔**(컵 prior: top Ø54mm, bottom Ø78mm, height 95mm) 피팅;
  fallen 컵은 OBB/PCA 폴백.
- **색상**: mask의 median HSV → `_classify_color_bgr`가 빨강/주황/노랑/초록/파랑/보라/
  흰/검으로 분류; track별 투표.
- **안정화**: 중심·base z를 EMA 평활(`cup_smoothing_alpha`), scan→lock 상태기계로
  안정되면 track을 고정.
- **track id**: ByteTrack id, `track_world_merge_dist_m` 내 재-ID는 기존 track과 병합.
- `/digital_twin/points`, `/digital_twin/boxes`, `/vision/cups_on_table`(색별 개수,
  `/stack_track_ids` 제외) 발행.

### boxes_to_detections_node  (`cup_stacking_verify`)
`/digital_twin/boxes`(MarkerArray) → `/detected_cups`(vision_msgs/Detection3DArray)
브릿지. verifier에 타입 있는 입력 제공.

### verifier_node  (`cup_stacking_verify`)
각 검출을 고정 피라미드 슬롯 기하(L1=3, L2=2, L3=1)와 겹침 비교. `/vision/stack`
(`{슬롯: 색|null}`)과 `/stack_track_ids`(슬롯 점유 중인 id) 발행. 슬롯 키는 단축형:
`L1_L, L1_M, L1_R, L2_L, L2_R, L3_T`.

---

## 5. Agent 노드 (`cup_stack_agent/scripts`)

### fake_digital_twin_node.py → `DigitalTwinStabilizerNode` (위치 정제)
`/digital_twin/boxes`(raw) 구독. track별로 `box_top`(x,y,z) 샘플을 1초 슬라이딩
윈도우에 모아 **median**(YOLO/피팅 아웃라이어에 강함; `mean` 선택 가능)을
`/digital_twin/boxes_filtered`에 `world` 프레임으로 재발행, 라벨은 그대로 통과.
오래된 track은 `DELETE`. 파라미터: `method`, `window_s`, `track_timeout_s`,
`publish_period_s`, `boxes_in_topic`, `boxes_out_topic`.

### fake_aggregator_node.py → `AggregatorNode` (월드상태 seam)
namespace된 실제 비전 월드상태를 구독해 `goal_state_publisher`가 소비하는 토픽으로
relay: `/vision/cups_on_table → /cups_on_table`, `/vision/stack → /stack`. 또한
`initial_command_delay_s` 후 `/user_command`를 1회 발행. relay 콜백이 월드상태 정제
(예: 깜빡이는 개수의 시간적 debounce)를 넣을 **단일 지점**이다. 파라미터:
`cups_in_topic`, `stack_in_topic`, `cups_out_topic`, `stack_out_topic`,
`user_command*`.

### goal_state_publisher_node.py
`user_command`, 현재/이전 월드상태, 로봇상태, 현재 계획/목표, 마지막 action 결과,
`mode`를 합쳐 `/llm_input` 구성. `/user_command`(→ cold_start)와 `/action_result`
(→ in_flight)에서 발행. 월드상태 갱신만으로는 LLM을 호출하지 않음. 슬롯 키는
`payload_builder.normalize_stack`에서 정규화 — 단축형 `L1_L`을 표준형 `L1_left`로
별칭 매핑하므로 verifier의 키를 그대로 받는다.

### llm_node.py
`/llm_input`을 `mode`로 라우팅, Ollama 호출(`prompts/cold_start_planner.md`,
`prompts/inflight_decider.md`), `/llm_output` 발행.

### plan_executor_node.py
- `/digital_twin/boxes_filtered` → `{id: (pos, color, class, locked)}` 읽음.
- `/stack_track_ids` → 이미 적재된 id.
- `/llm_output`마다 각 스텝에서 `select_cup(color)` = 해당 색의 첫 번째 upright·미적재·
  위치 확정 컵 → 그 (x, y).
- 표준 슬롯 → API 키 매핑(`L1_left→1l`, … `L3_top→3m`).
- `POST /api/robot/skill/pyramid {x, y, slot}`; `/action_result` 발행.
- 다음 POST는 `skill_api_node` `busy=false`까지 대기(첫 POST는 게이트 없음).

`topic_logger_node.py`는 토픽을 `logs/<run>/`에 스냅샷. `payload_builder.py`,
`llm_client.py`는 헬퍼 모듈.

---

## 6. 횡단 관심사

**좌표계.** `world_origin_node` 하류는 모두 `world`(로봇 base): X-전방, Y-좌, Z-상,
미터 단위. 픽 정확도는 ArUco 캘리브에 의존 — 검출 컵이 실제 x,y 근처에 떨어지는지 확인.

**색상.** `point_cloud_node`의 HSV 분류가 빨강·파랑(및 그 이상) 지원. executor의
`parse_label`·`select_cup`은 모든 색 허용. `/cups_on_table` 개수는 색별.

**track id**는 동적(ByteTrack), 고정 슬롯→id 테이블 아님. executor는 *색 + upright +
미적재*로 선택하지, 하드코딩 id로 고르지 않음.

**픽에서 z는 무시.** stabilizer는 완전성을 위해 median z를 유지하지만, executor는 x,y만
사용; 놓는 높이는 로봇 서버가 관리.

**Disturbance(외란)**는 스크립트가 아니라 물리적: 적재된 컵을 손으로 치우면 인식이
다음 프레임에 반영(verifier가 `/stack`·`/stack_track_ids`에서 제거; point_cloud가
`/cups_on_table`에 다시 카운트)되어 LLM이 재계획.

---

## 7. End-to-end 시퀀스

```
1. aggregator가 /user_command 발행 (비전 토픽 안정화 후).
2. goal_state_publisher → cold_start /llm_input (개수 + 빈 스택).
3. llm_node → /llm_output: 6스텝 계획 (스텝별 color + target_slot).
4. plan_executor: select_cup(color) → POST {x,y,slot} → /action_result success.
5. 컵 놓임 → verifier가 슬롯에서 인식 → /vision/stack + /stack_track_ids 갱신;
   point_cloud가 /vision/cups_on_table에서 제외.
6. aggregator가 새 월드상태 relay → goal_state_publisher → in_flight /llm_input.
7. llm_node → continue / replan / done. 피라미드 완성까지 반복.
```

---

## 8. 실행법

모든 터미널: `source /opt/ros/humble/setup.bash` + `export ROS_LOCALHOST_ONLY=1`.

**Vision 워크스페이스** (`ros2-depth-point-cloude/install`,
`vision-node/install` 필요 시 source):
```bash
# 1) 카메라 (exo)
ros2 launch recode_sequence cameras_only.launch.py view:=exo
# 2) detection + point cloud + world-origin + RViz
#    (point_cloud가 이제 /vision/cups_on_table 발행 — params.yaml에 설정됨)
ros2 launch depth_digital_twin digital_twin.launch.py camera_ns:=exo
# 3) 스택 verifier  (launch remap으로 /vision/stack 발행)
ros2 launch cup_stacking_verify cup_verify.launch.py rviz:=false tuner:=false
```

**Agent 워크스페이스** (`cup-stack-integration/cup_stack_agent`):
```bash
./start.sh                                   # dry-run (POST 바디 로깅)
./start.sh --real-api                        # 실제 로봇 pyramid API 호출
# 또는 동등하게:
ros2 launch launch/agent.launch.py [dry_run:=false] [with_llm:=false]
```
`--real-api`는 추가로 Ollama(`ollama list`)와 로봇 skill 스택이 필요.

aggregator를 GSP 앞에 끼우는 영구 remap(이미 적용됨):
`params.yaml` → `point_cloud_node.cups_on_table_topic: /vision/cups_on_table`;
`cup_verify.launch.py` → verifier `remappings=[('/stack','/vision/stack')]`.

---

## 9. 테스트

Agent(`cup_stack_agent`), 하드웨어 불필요:
```bash
python3 -m unittest discover -s tests -v     # 오프라인 수학 + 라이브 DDS 노드 테스트
python3 -m py_compile scripts/*.py launch/agent.launch.py
bash -n start.sh
```
`tests/test_stabilizer.py`가 `aggregate()`(median이 아웃라이어 제거, 윈도우 트리밍),
라이브 stabilizer(`/digital_twin/boxes → boxes_filtered`), aggregator relay
(`/vision/* → /cups_on_table,/stack`), `/user_command`를 커버.

stabilizer 수학은 별도 위치 없이 agent의 `fake_digital_twin_node`에 있음. 라이브
인식은 §8 기동 후 §3 토픽을 echo로 확인.

---

## 10. 설계 근거 (FAQ)

- **왜 LLM은 좌표를 안 보나?** LLM은 정밀 숫자에 약하고 필요도 없다. LLM은 *색 + 슬롯*만
  결정하고, `plan_executor`가 `/digital_twin/boxes_filtered`에서 실제 x,y를 해결한다.
  기하와 언어를 의도적으로 분리.
- **왜 GSP가 비전을 직접 구독하지 않고 aggregator가 relay하나?** 인식과 계획 사이에
  단일 seam을 두기 위해. 개수/점유는 기하적 떨림이 없어 오늘은 pass-through지만,
  월드상태 debounce(LLM이 한 프레임 오검출에 반응하지 않도록)가 필요하면 여기 들어간다.
- **왜 별도 stabilizer 노드?** raw 프레임별 컵 피팅이 수 mm 떨린다. 짧은 윈도우 median이
  비전 노드 자체의 EMA/lock을 건드리지 않고 executor에 안정적 픽 타깃을 준다.
- **왜 두 agent 파일이 아직 `fake_*` 이름?** 역사적 이유 — GT 주입기로 시작했다. 지금은
  실제 노드(`AggregatorNode`, `DigitalTwinStabilizerNode`)이며, 파일명만 git 연속성을
  위해 남겼다.
```
