# test_v1.0

YARR integration experiment for the LLM closed loop, driven by the **real vision
pipeline** (depth_digital_twin + cup_stacking_verify) over the same ROS topic
contract the agent already consumed.

This replaces the earlier GT-injection setup. One in-repo perception-glue node
remains (the file is still named `fake_*` for history, but no longer injects
ground truth):

- `fake_aggregator_node.py` → **user_command_node**: publishes only `/user_command`
  (the one world-state input perception cannot produce). `/cups_on_table` and
  `/stack` now come from the real nodes.

Cup-position stabilization no longer lives in this repo. It now runs in the
vision workspace as `box_stabilizer_node` (ROS package `depth_digital_twin`,
workspace `ros2-depth-point-cloude`), which subscribes the raw
`/digital_twin/boxes` from `point_cloud_node` and republishes
`/digital_twin/boxes_filtered`.

Both **blue and red** cups are supported (the real HSV classifier and the
executor's `parse_label` both handle every color). Track ids are dynamic
(ByteTrack), not a fixed slot→id table; the executor selects a cup by color +
upright + not-yet-stacked. The disturbance scenario is no longer scripted — a
physically moved cup is reflected by perception on the next frame.

This README is the source of truth for future AI/code agents. Follow this plan
before modifying implementation.

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

The I/O must match the real ROS pipeline.

Do not introduce alternate topic names, alternate message paths, or direct
coordinate parameters. The agent consumes exactly the topics the real perception
nodes publish.

## Real Vision Pipeline (separate workspace)

The perception nodes run in their own sourced workspace and must be publishing
before the agent is useful:

```text
point_cloud_node            (depth_digital_twin)
  -> /digital_twin/boxes       (raw per-frame cup markers)
  -> /cups_on_table            (real {blue, red, ...} counts, minus stacked)

box_stabilizer_node         (depth_digital_twin)
  <- /digital_twin/boxes       (raw, from point_cloud_node)
  -> /digital_twin/boxes_filtered  (per-track median over a sliding window)

verifier_node               (cup_stacking_verify)
  -> /stack                    (JSON {slot: color|null})
  -> /stack_track_ids          (track ids occupying the pyramid)
```

## Primary ROS Experiment Path

Use this path as the main experiment.

```text
point_cloud_node  -> /digital_twin/boxes  (raw)

box_stabilizer_node            (vision workspace, depth_digital_twin)
  <- /digital_twin/boxes
  -> /digital_twin/boxes_filtered  (per-track median x,y,z over a sliding window)

user_command_node              (was fake_aggregator_node)
  -> /user_command

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
  <- /digital_twin/boxes_filtered
  <- /stack_track_ids
  -> POST /api/robot/skill/pyramid {"x": ..., "y": ..., "slot": ...}
  -> /action_result
```

## Node Responsibilities

### fake_aggregator_node.py  (user_command_node)

Publishes the operator command — the only world-state input the real perception
pipeline cannot produce.

Publishes:

```text
/user_command
```

It publishes once, after `initial_command_delay_s` (default 2.0s), so
`goal_state_publisher_node` is up and the real perception topics have settled
first. `/cups_on_table` and `/stack` are NOT published here anymore — they come
from `point_cloud_node` and `verifier_node`.

Parameters:

```text
user_command              default '3단 피라미드 쌓아줘'
user_command_topic        default /user_command
initial_command_delay_s   default 2.0
publish_period_s          default 0.5
```

### Cup-position stabilization (moved to the vision repo)

Stabilization is no longer an in-repo node. It now runs in the vision workspace
as `box_stabilizer_node` (ROS package `depth_digital_twin`, workspace
`ros2-depth-point-cloude`). It subscribes the raw `/digital_twin/boxes` from
`point_cloud_node` and publishes `/digital_twin/boxes_filtered`: a per-track
median over a sliding time window (default 1.0s), with x, y, and z stabilized,
the color/class label passed through unchanged, in the `world` frame. The
executor here consumes `/digital_twin/boxes_filtered`. Its math is unit-tested
in the vision repo (`depth_digital_twin/test/test_box_stabilizer.py`).

There is no scripted disturbance: a physically moved or removed cup is reflected
by perception on the next frame (its track leaves/returns naturally, and
`verifier_node` updates `/stack_track_ids`).

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
read /digital_twin/boxes_filtered
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
for `skill_api_node` status to report `busy=false`. The first POST is allowed
without the status gate because the server starts `skill_api_node` lazily on
the first skill request.

## LLM Call Sequence

```text
1. user_command_node publishes /user_command.
2. goal_state_publisher_node publishes cold_start /llm_input.
3. llm_node publishes cold-start /llm_output with plan.
4. plan_executor_node executes the first pyramid step.
5. plan_executor_node publishes /action_result at cup release/place time.
6. point_cloud_node /cups_on_table drops the picked cup; verifier_node updates /stack.
7. verifier_node updates /stack_track_ids as the cup enters its slot.
8. goal_state_publisher_node publishes in_flight /llm_input.
9. llm_node publishes continue/replan/done.
10. plan_executor_node waits for skill_api_node busy=false.
11. plan_executor_node executes the next step on continue.
```

## Expected API Bodies

With cups at their nominal experiment positions, the executor should produce
roughly these bodies (real perception x,y will be close, not exact):

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

user_command_node:
  provide the operator command through /user_command.

box_stabilizer_node (real vision, separate workspace):
  smooth raw /digital_twin/boxes and publish /digital_twin/boxes_filtered.

point_cloud_node / verifier_node (real vision, separate workspace):
  provide world state through /cups_on_table, /stack, /stack_track_ids.

plan_executor_node:
  select a cup pose from /digital_twin/boxes_filtered,
  convert LLM target_slot to API slot,
  call the HTTP API.

HTTP API / server:
  execute the requested pick-and-place motion.
```

The server trusts whatever x,y the executor sends and picks there. The pick
accuracy therefore depends on perception + the stabilizer; the world frame must
be calibrated (world_origin_node) so cup positions land in the robot base frame.

## Run

First bring up the real vision pipeline in its own sourced workspace. Run
`point_cloud_node` normally (it publishes `/digital_twin/boxes`) and run
`box_stabilizer_node`, which produces `/digital_twin/boxes_filtered`:

```bash
ros2 run depth_digital_twin point_cloud_node
ros2 run depth_digital_twin box_stabilizer_node
# plus detection_node, world_origin_node, boxes_to_detections, verifier_node
```

Then start the agent. Dry-run mode logs executor request bodies without calling
the robot API:

```bash
./start.sh                                  # or: ros2 launch launch/agent.launch.py
```

To call the real pyramid API:

```bash
./start.sh --real-api                       # or: ros2 launch launch/agent.launch.py dry_run:=false
```

`launch/agent.launch.py` accepts `with_llm:=false` (skip ollama, e.g. to smoke
test the perception-glue path). The x,y stabilization is tuned in the vision
workspace on `box_stabilizer_node`, not here.

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
python3 -m unittest discover -s tests -v    # offline + live DDS node tests
python3 -m py_compile scripts/*.py launch/agent.launch.py
bash -n start.sh
```

`tests/test_user_command.py` covers the in-repo perception glue without a
camera/robot: a live rclpy test that spins `user_command_node` over DDS and
asserts it publishes `/user_command`. It skips automatically if ROS middleware
is unavailable. The stabilizer's own math is unit-tested in the vision repo
(`depth_digital_twin/test/test_box_stabilizer.py`).

Headless smoke test of the whole agent without ollama/camera:

```bash
ros2 launch launch/agent.launch.py with_llm:=false   # nodes come up; /user_command -> /llm_input
```
