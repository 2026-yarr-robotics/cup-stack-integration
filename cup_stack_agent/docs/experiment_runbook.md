# Experiment Runbook

This runbook describes how to execute the `test_v1.0` experiment over the **real
vision pipeline**.

Agent nodes (this repo):

```text
user_command_node            (file: fake_aggregator_node.py)
goal_state_publisher_node
llm_node
plan_executor_node
```

Real vision nodes (separate sourced workspace) must also be running:

```text
point_cloud_node    -> /digital_twin/boxes, /cups_on_table
box_stabilizer_node -> /digital_twin/boxes_filtered  (consumes /digital_twin/boxes)
verifier_node       -> /stack, /stack_track_ids
(+ detection_node, world_origin_node, boxes_to_detections_node)
```

Do not use a separate non-ROS orchestration client for this experiment.

## Scenario

Fixed user command:

```text
3단 피라미드 쌓아줘
```

Expected cold-start target:

```json
{
  "base_levels": 3,
  "cup_budget": 6,
  "target_slots": [
    "L1_left",
    "L1_mid",
    "L1_right",
    "L2_left",
    "L2_right",
    "L3_top"
  ]
}
```

Expected robot skill API bodies:

```json
{"x": 0.250, "y": -0.20, "slot": "1l"}
{"x": 0.250, "y": 0.00, "slot": "1m"}
{"x": 0.250, "y": 0.20, "slot": "1r"}
{"x": 0.350, "y": -0.20, "slot": "2l"}
{"x": 0.350, "y": 0.00, "slot": "2r"}
{"x": 0.350, "y": 0.20, "slot": "3m"}
```

## 0. Sync Repo

```bash
cd ~/Projects/cup-stack-integration/cup_stack_agent
git pull
```

## 1. Bring Up Real Vision

In the vision workspace (sourced separately), start perception. Run
`point_cloud_node` normally (it publishes `/digital_twin/boxes`) and run
`box_stabilizer_node`, which produces `/digital_twin/boxes_filtered`:

```bash
ros2 run depth_digital_twin point_cloud_node
ros2 run depth_digital_twin box_stabilizer_node
# plus detection_node, world_origin_node, boxes_to_detections_node, verifier_node
```

Sanity check the topics carry real data before continuing:

```bash
ros2 topic echo --once /digital_twin/boxes_filtered  # stabilized markers present
ros2 topic echo --once /cups_on_table                # {"blue": N, "red": M, ...}
ros2 topic echo --once /stack                        # {slot: color|null}
```

## 1b. Check LLM Backend

Ollama must be running and the target model must be available.

```bash
ollama list
```

Optional environment overrides:

```bash
export MODEL=qwen3.6:35b
export OLLAMA_URL=http://localhost:11434/api/chat
```

## 2. Dry-Run ROS Loop

Run the complete ROS topic loop without calling the robot skill API:

```bash
./start.sh
```

Dry-run mode should log request bodies instead of moving the robot.

Expected high-level flow:

```text
user_command_node
  publishes /user_command

goal_state_publisher_node
  reads real /cups_on_table, /stack
  publishes cold_start /llm_input

llm_node
  calls Ollama
  publishes /llm_output plan

plan_executor_node
  reads /digital_twin/boxes_filtered (stabilized)
  logs dry-run POST body
  publishes /action_result success

real vision
  /cups_on_table drops the picked cup; verifier_node updates /stack and
  /stack_track_ids as the cup enters its slot

goal_state_publisher_node
  publishes in_flight /llm_input

llm_node
  publishes continue/done
```

## 3. Inspect Topics

`start.sh` writes node logs and topic snapshots under a timestamped directory:

```text
logs/YYYYmmdd_HHMMSS/
```

The run prints the exact directory at startup:

```text
[start.sh] logs: logs/YYYYmmdd_HHMMSS
```

Useful files:

```text
topics.jsonl                 all observed topic messages
llm_input.jsonl              /llm_input only
llm_output.jsonl             /llm_output only
action_result.jsonl          /action_result only
cups_on_table.jsonl          /cups_on_table only
stack.jsonl                  /stack only
stack_track_ids.jsonl        /stack_track_ids only
digital_twin__boxes_filtered.jsonl  /digital_twin/boxes_filtered only
plan_executor.log            plan_executor_node stdout/stderr
llm_node.log                 llm_node stdout/stderr
goal_state_publisher.log     goal_state_publisher_node stdout/stderr
```

Examples:

```bash
tail -f logs/YYYYmmdd_HHMMSS/plan_executor.log
tail -f logs/YYYYmmdd_HHMMSS/action_result.jsonl
tail -f logs/YYYYmmdd_HHMMSS/topics.jsonl
```

Use separate terminals only if live detailed inspection is still needed:

```bash
ros2 topic echo /llm_input
ros2 topic echo /llm_output
ros2 topic echo /action_result
ros2 topic echo /cups_on_table
ros2 topic echo /stack
ros2 topic echo /stack_track_ids
ros2 topic echo /digital_twin/boxes_filtered   # what the executor consumes
ros2 topic echo /digital_twin/boxes            # raw, from point_cloud_node
```

Dry-run success criteria:

```text
/llm_input cold_start is published once after /user_command.
/llm_output contains a 6-step 3-level plan.
plan_executor_node logs six dry-run POST bodies.
/action_result reports success for each executed pyramid step.
/stack fills L1_left, L1_mid, L1_right, L2_left, L2_right, L3_top over time.
/cups_on_table table count (blue/red) decreases as cups are stacked.
/stack_track_ids accumulates used track ids.
/digital_twin/boxes_filtered x,y are stable (stabilized), not jittering frame to frame.
```

## 4. Prepare Real Robot API

Only continue after dry-run is correct.

Required external services:

```text
robot skill API at https://yarr-api-31.simplyimg.com
skill_api_node on :8765
Doosan bringup / MoveIt ready
pyramid geometry config set on server
```

Check server status:

```bash
curl https://yarr-api-31.simplyimg.com/api/robot/status
```

## 5. Run Real API Experiment

This calls the real pyramid skill endpoint:

```bash
./start.sh --real-api
```

`start.sh` passes `api_timeout_s=180.0` to `plan_executor_node` by default because
real robot pyramid moves can take longer than the node's code default of 15
seconds. Override only when needed:

```bash
API_TIMEOUT_S=240.0 ./start.sh --real-api
```

The server now responds to each pyramid call at cup release/place time while
the final lift continues. This lets GSP and LLM infer during the lift. The
executor still gates the next physical POST by polling skill_api_node status:

```text
first POST starts skill_api_node lazily through the server
SKILL_STATUS_URL=http://localhost:8765/status
SKILL_IDLE_TIMEOUT_S=10.0
from the second POST onward, wait until busy=false, up to 10 seconds
then submit the next pyramid POST
```

Expected physical sequence:

```text
1. Pick measured cup at x=0.250, y=-0.20 and place slot 1l.
2. Pick measured cup at x=0.250, y=0.00 and place slot 1m.
3. Pick measured cup at x=0.250, y=0.20 and place slot 1r.
4. Pick measured cup at x=0.350, y=-0.20 and place slot 2l.
5. Pick measured cup at x=0.350, y=0.00 and place slot 2r.
6. Pick measured cup at x=0.350, y=0.20 and place slot 3m.
```

Disturbance is now physical, not scripted. To exercise replanning, remove an
already-stacked cup by hand during the run:

```text
After a stacked cup is physically removed:
  perception stops seeing it in its slot -> verifier_node drops it from
    /stack and /stack_track_ids.
  the cup reappears on the table -> point_cloud_node re-tracks it and
    /cups_on_table increments its color.

Expected LLM response:
  in_flight -> replan
  new plan refills the emptied slot before continuing.
```

For a clean baseline, simply do not disturb the stack.

## Failure Triage

### LLM output is wrong

Inspect:

```bash
ros2 topic echo /llm_input
ros2 topic echo /llm_output
```

Likely causes:

```text
Prompt mismatch.
Ollama model mismatch.
Malformed world state from point_cloud_node /cups_on_table or verifier /stack.
```

### POST body is wrong

Inspect:

```bash
ros2 topic echo /digital_twin/boxes
ros2 topic echo /stack_track_ids
```

Likely causes:

```text
point_cloud_node marker labels not parsed as upright cups of the wanted color.
world frame uncalibrated -> x,y off (check world_origin_node).
stabilizer window too long -> lags a moved cup (lower window_s).
stack_track_ids excluded the wrong cup.
target_slot to API slot mapping issue.
```

### Same cup is selected twice

Inspect:

```bash
ros2 topic echo /stack_track_ids
ros2 topic echo /action_result
```

Likely cause:

```text
verifier_node did not add the used track id to /stack_track_ids (cup not yet
seen in its slot), so point_cloud_node still counts it as on-table.
```

### LLM reports replan after success

Inspect:

```bash
ros2 topic echo /stack
ros2 topic echo /cups_on_table
ros2 topic echo /action_result
```

Likely cause:

```text
verifier_node /stack or point_cloud_node /cups_on_table did not reflect the
placed cup (perception did not see it land in its slot).
```

### Robot API fails

Inspect:

```text
server logs
skill_api_node logs
Doosan bringup / MoveIt state
pyramid geometry config
```

The HTTP API server is an execution layer. It trusts the provided `x`, `y`, and
`slot`; it does not validate the LLM plan or run perception.

## Verification Commands

Before committing changes:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py launch/agent.launch.py
bash -n start.sh
```

## Fallen-Cup Recovery (LLM interrupt)

LLM 입력에 top-level `fallen` `{color: count}` 맵이 추가됐다. plan_executor가
exo 트랙(`/digital_twin/boxes_filtered`)의 `fallen-cup` class 마커를 색상별로
세어 `/fallen_cups`(JSON)로 0.5s마다 발행하고, GSP가 payload에 싣는다.

`fallen`이 비어있지 않으면 LLM은 continue/replan/done 대신
`decision="fallen_recovery"`(top-level interrupt, `plan=null`)를 출력한다.
plan_executor는 **기존 plan/step을 건드리지 않고**:

1. `fallen_cup_detect`(hand-eye YOLO 서비스) 미기동 시
   `POST {ROBOT_API_BASE}/api/robot/fallen-cup/detection/start` 후 대기
   (`detection_warmup_s`, 기본 15s).
2. `POST {ROBOT_API_BASE}/api/robot/fallen-cup/recovery`
   `{"mode":"place","multi_cup":false}` — 좌표/색상은 보내지 않는다
   (recovery task가 자체 hand-eye 인식으로 grasp pose를 구한다. LLM color는
   exo 트랙 사전 검증용).
3. 비동기 task이므로 `GET /api/robot/status`의 `tasks[]`에서
   `fallen_cup_recovery`가 `running`을 벗어날 때까지 폴링
   (`recovery_timeout_s`, 기본 240s). `idle`=success, `failed`=fail.
4. `/action_result` `{"step":null,"action":"fallen_recovery",...}` 발행.

GSP는 recovery success 후 해당 색 fallen count가 실제로 줄 때까지
`/llm_input` 발행을 보류한다(`recovery_clear_timeout_s`, 기본 8s; 타임아웃 시
그냥 발행 → LLM이 재시도 판단). fallen이 비고 current_goal이 유효하면 LLM은
`continue`로 기존 plan에 복귀한다 — recovery 후 자동 replan은 하지 않는다
(stack slot이 무너진 경우에만 replan).

주의:

- 서버는 recovery 시작 전 자기가 관리하는 skill_api를 내리고 다음
  pick/pyramid 호출 때 lazy 재시작한다(MoveItPy 초기화 최대 ~90s).
  pick_node `api_timeout_sec`(180s)가 이를 흡수한다.
- recovery 모션은 freeze 기본 60s를 넘길 수 있어 start.sh가 GSP
  `freeze_timeout_s`를 240s로 올린다 (`FREEZE_TIMEOUT_S` env).
- dry-run에서는 recovery POST 없이 success를 합성한다.
