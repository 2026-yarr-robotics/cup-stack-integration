# Experiment Runbook

This runbook describes how to execute the `test_v1.0` fake-perception
experiment.

The goal is to validate the real ROS topic pipeline with fake perception inputs:

```text
fake_aggregator_node
fake_digital_twin_node
goal_state_publisher_node
llm_node
plan_executor_node
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
cd /private/tmp/test_v1.0
git pull
```

## 1. Check LLM Backend

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
fake_aggregator_node
  publishes /user_command, /cups_on_table, /stack

goal_state_publisher_node
  publishes cold_start /llm_input

llm_node
  calls Ollama
  publishes /llm_output plan

plan_executor_node
  reads /digital_twin/boxes
  logs dry-run POST body
  publishes /action_result success

fake_aggregator_node
  updates /cups_on_table and /stack

fake_digital_twin_node
  updates /stack_track_ids

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
digital_twin__boxes.jsonl    /digital_twin/boxes only
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
ros2 topic echo /digital_twin/boxes
```

Dry-run success criteria:

```text
/llm_input cold_start is published once after /user_command.
/llm_output contains a 6-step 3-level plan.
plan_executor_node logs six dry-run POST bodies.
/action_result reports success for each executed pyramid step.
/stack fills L1_left, L1_mid, L1_right, L2_left, L2_right, L3_top over time.
/cups_on_table blue count decreases from 6 to 0.
/stack_track_ids accumulates used track ids.
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

Expected physical sequence:

```text
1. Pick measured cup at x=0.250, y=-0.20 and place slot 1l.
2. Pick measured cup at x=0.250, y=0.00 and place slot 1m.
3. Pick measured cup at x=0.250, y=0.20 and place slot 1r.
4. Pick measured cup at x=0.350, y=-0.20 and place slot 2l.
5. Pick measured cup at x=0.350, y=0.00 and place slot 2r.
6. Pick measured cup at x=0.350, y=0.20 and place slot 3m.
```

Current disturbance scenario is enabled by default:

```text
After step 5 succeeds:
  fake_aggregator_node publishes L2_left=null and blue table count +1.
  fake_digital_twin_node removes track id 4 from /stack_track_ids.
  track id 4 reappears at x=0.250, y=-0.20.

Expected LLM response:
  in_flight -> replan
  new plan fills L2_left first, then L3_top.
```

Disable the disturbance only when running a clean no-disturbance baseline:

```bash
DISTURBANCE_ENABLED=false ./start.sh --real-api
```

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
Malformed world state from fake_aggregator_node.
```

### POST body is wrong

Inspect:

```bash
ros2 topic echo /digital_twin/boxes
ros2 topic echo /stack_track_ids
```

Likely causes:

```text
fake_digital_twin_node marker labels not parsed as blue upright cups.
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
fake_digital_twin_node did not add the used track id after /action_result.
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
fake_aggregator_node did not update world state after /action_result.
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
python3 -m py_compile scripts/*.py
bash -n start.sh
```
