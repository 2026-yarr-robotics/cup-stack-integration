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
3단 피라미드에서 1단만 쌓아줘
```

Expected cold-start target:

```json
{
  "base_levels": 3,
  "cup_budget": 3,
  "target_slots": ["L1_left", "L1_mid", "L1_right"]
}
```

Expected robot skill API bodies:

```json
{"x": 0.280, "y": -0.15, "slot": "1l"}
{"x": 0.280, "y": 0.0, "slot": "1m"}
{"x": 0.280, "y": 0.15, "slot": "1r"}
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

Use separate terminals if detailed inspection is needed:

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
/llm_output contains a 3-step bottom-row plan.
plan_executor_node logs three dry-run POST bodies.
/action_result reports success for each executed pyramid step.
/stack fills L1_left, L1_mid, L1_right over time.
/cups_on_table red count decreases from 3 to 0.
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

Expected physical sequence:

```text
1. Pick measured cup at x=0.280, y=-0.15 and place slot 1l.
2. Pick measured cup at x=0.280, y=0.00 and place slot 1m.
3. Pick measured cup at x=0.280, y=0.15 and place slot 1r.
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
fake_digital_twin_node marker labels not parsed as red upright cups.
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
