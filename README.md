# test_v1.0

Temporary YARR integration experiment for validating the LLM closed loop with
fake perception inputs but the same ROS topic contract as the real pipeline.

This README is written as the source of truth for future AI/code agents. Follow
this plan before modifying implementation.

## Experiment Goal

Run one fixed scenario:

```text
3단 피라미드 쌓아줘
```

Expected cold-start normalization:

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

The robot should execute six pyramid API calls for the full 3-level pyramid:

```text
L1_left  -> API slot 1l
L1_mid   -> API slot 1m
L1_right -> API slot 1r
L2_left  -> API slot 2l
L2_right -> API slot 2r
L3_top   -> API slot 3m
```

## Core Rule

The experiment is fake, but the I/O must match the real ROS pipeline.

Do not introduce alternate topic names, alternate message paths, or direct
coordinate parameters for the ROS experiment. Fake data should be injected
through the same topics the real nodes use.

## Primary ROS Experiment Path

Use this path as the main experiment.

```text
fake_aggregator_node
  -> /cups_on_table
  -> /stack
  -> /user_command
  <- /action_result

fake_digital_twin_node
  -> /digital_twin/boxes
  -> /stack_track_ids
  <- /action_result

goal_state_publisher_node
  <- /cups_on_table
  <- /stack
  <- /robot_state       (optional; default empty gripper if absent)
  <- /user_command
  <- /action_result
  <- /llm_output
  -> /llm_input

llm_node
  <- /llm_input
  -> /llm_output

plan_executor_node
  <- /llm_output
  <- /digital_twin/boxes
  <- /stack_track_ids
  -> POST /api/robot/skill/pyramid {"x": ..., "y": ..., "slot": ...}
  -> /action_result
```

## Node Responsibilities

### fake_aggregator_node.py

Fake replacement for perception/world aggregation.

Publishes:

```text
/cups_on_table
/stack
/user_command
```

Subscribes:

```text
/action_result
```

Initial state:

```json
{
  "cups_on_table": {"blue": 6},
  "stack": {
    "L1_left": null,
    "L1_mid": null,
    "L1_right": null,
    "L2_left": null,
    "L2_right": null,
    "L3_top": null
  }
}
```

After a successful `/action_result`, it updates fake perception state:

```text
cups_on_table[color] -= 1
stack[target_slot] = color
```

### fake_digital_twin_node.py

Fake replacement for the digital twin cup pose output.

Publishes:

```text
/digital_twin/boxes
/stack_track_ids
```

Subscribes:

```text
/action_result
```

Measured blue cup poses:

```text
L1_left  -> track id 1, x=0.250, y=-0.20
L1_mid   -> track id 2, x=0.250, y=0.00
L1_right -> track id 3, x=0.250, y=0.20
L2_left  -> track id 4, x=0.350, y=-0.20
L2_right -> track id 5, x=0.350, y=0.00
L3_top   -> track id 6, x=0.350, y=0.20
```

These poses must be emitted as `visualization_msgs/MarkerArray`, matching what
`plan_executor_node.py` already consumes. Do not pass these coordinates through
CLI JSON or custom topics in the ROS experiment.

Disturbance mode is enabled by default for the current experiment:

```text
Trigger: after L2_right succeeds
Removed slot: L2_left
Returned cup: track id 4 at x=0.250, y=-0.20
```

This simulates a person removing the 4th stacked cup while the 5th step is
being completed. The fake digital twin removes track id 4 from
`/stack_track_ids` so the executor can pick it again.

### goal_state_publisher_node.py

Builds `/llm_input`.

It exists because the LLM input is not just user text. It combines:

```text
user_command
current_world_state
previous_world_state
robot_state
current_plan
current_goal
last_action_result
mode
```

LLM is called when GSP publishes `/llm_input`, which happens on:

```text
/user_command    -> cold_start
/action_result   -> in_flight
```

By default, world-state updates alone do not call the LLM.

### llm_node.py

Consumes `/llm_input`, routes by `payload["mode"]`, calls Ollama, and publishes
`/llm_output`.

Prompts are local:

```text
prompts/cold_start_planner.md
prompts/inflight_decider.md
```

### plan_executor_node.py

Use the real-compatible executor contract.

It must:

```text
read /llm_output
read /digital_twin/boxes
read /stack_track_ids
select a cup by color
map LLM target_slot to API slot
POST /api/robot/skill/pyramid {"x", "y", "slot"}
publish /action_result
```

It should not accept fake coordinates directly in the ROS experiment.

The robot server returns from `POST /api/robot/skill/pyramid` at cup release
time while the final lift can still be running. `plan_executor_node.py` starts
the LLM loop from that early success, but before sending the next POST it waits
for `skill_api_node` status to report `busy=false`.

## LLM Call Sequence

```text
1. fake_aggregator_node publishes /user_command.
2. goal_state_publisher_node publishes cold_start /llm_input.
3. llm_node publishes cold-start /llm_output with plan.
4. plan_executor_node executes the first pyramid step.
5. plan_executor_node publishes /action_result at cup release/place time.
6. fake_aggregator_node updates /cups_on_table and /stack.
7. fake_digital_twin_node updates /stack_track_ids.
8. goal_state_publisher_node publishes in_flight /llm_input.
9. llm_node publishes continue/replan/done.
10. plan_executor_node waits for skill_api_node busy=false.
11. plan_executor_node executes the next step on continue.
```

## Expected API Bodies

With the measured fake digital twin poses, the executor should produce:

```json
{"x": 0.250, "y": -0.20, "slot": "1l"}
{"x": 0.250, "y": 0.00, "slot": "1m"}
{"x": 0.250, "y": 0.20, "slot": "1r"}
{"x": 0.350, "y": -0.20, "slot": "2l"}
{"x": 0.350, "y": 0.00, "slot": "2r"}
{"x": 0.350, "y": 0.20, "slot": "3m"}
```

## HTTP API / Server Role

The FastAPI server endpoint is an execution layer, not a planner or verifier.

Endpoint:

```text
POST /api/robot/skill/pyramid
```

Request body:

```json
{"x": 0.250, "y": -0.20, "slot": "1l"}
```

Meaning:

```text
Pick the cup at measured table position (x, y).
Place it into the pyramid slot identified by the API slot key.
```

The server is expected to:

```text
1. trust the provided x,y as the pick target;
2. translate API slot keys such as 1l, 1m, 1r into internal place poses using
   its own pyramid geometry config;
3. call the lower-level robot/skill implementation;
4. return success after cup release/place while final lift continues;
5. keep skill_api_node busy=true until the final lift finishes.
```

The server is not expected to:

```text
validate whether the LLM plan is correct;
decide which cup color should be used;
interpret LLM slot names such as L1_left;
inspect /cups_on_table or /stack;
run perception;
decide whether the task is done.
```

Responsibility split:

```text
LLM / GSP:
  decide and track what should happen next.

fake_aggregator_node:
  provide fake world state through the same topics as perception/aggregation.

fake_digital_twin_node:
  provide fake measured cup poses through /digital_twin/boxes.

plan_executor_node:
  select a cup pose from /digital_twin/boxes,
  convert LLM target_slot to API slot,
  call the HTTP API.

HTTP API / server:
  execute the requested pick-and-place motion.
```

Therefore, if `fake_digital_twin_node` publishes a hardcoded measured coordinate,
the server will treat that coordinate as the real cup location and attempt to
pick there. This is intentional for this experiment: it isolates the
LLM/GSP/executor/API request path from perception accuracy.

## Run

Dry-run mode logs executor request bodies and publishes success without calling
the API.

```bash
./start.sh
```

To call the real pyramid API:

```bash
./start.sh --real-api
```

Useful environment variables:

```text
API_URL     default https://yarr-api-31.simplyimg.com/api/robot/skill/pyramid
API_TIMEOUT_S default 180.0
SKILL_STATUS_URL default http://localhost:8765/status
SKILL_IDLE_TIMEOUT_S default 10.0
SKILL_IDLE_POLL_S default 0.2
SKILL_STATUS_TIMEOUT_S default 1.0
MODEL       default qwen3.6:35b
OLLAMA_URL  default http://localhost:11434/api/chat
```

## Do Not Do

- Do not add `/raw_action_result` for this experiment.
- Do not pass fake XY to `plan_executor_node.py` through parameters.
- Do not bypass `goal_state_publisher_node.py` in the ROS experiment.
- Do not rename the real topics.
- Do not make `llm_node.py` choose the mode itself; mode is supplied in
  `/llm_input` by GSP.
- Do not add a separate non-ROS orchestration client that directly calls both
  Ollama and the robot skill API. This experiment should run through the ROS
  nodes one by one.

## Test

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n start.sh
```
