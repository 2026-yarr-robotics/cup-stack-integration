# test_v1.0

Temporary integration experiment for the YARR LLM closed loop.

This repo keeps the real topic/message contract but replaces perception inputs
with fake nodes for one fixed integration experiment.

Target scenario:

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

## Nodes

- `fake_aggregator_node.py`: publishes `/cups_on_table`, `/stack`, `/user_command`
  and updates fake world state from `/action_result`.
- `fake_digital_twin_node.py`: publishes `/digital_twin/boxes` and
  `/stack_track_ids` using measured red cup positions.
- `goal_state_publisher_node.py`: builds `/llm_input`.
- `llm_node.py`: calls Ollama and publishes `/llm_output`.
- `plan_executor_node.py`: unchanged executor contract; consumes
  `/llm_output`, `/digital_twin/boxes`, `/stack_track_ids`, calls the pyramid API,
  and publishes `/action_result`.

## Measured Cup Poses

The fake digital twin publishes these measured red cup positions:

```text
L1_left  -> track id 1, x=0.280, y=-0.15
L1_mid   -> track id 2, x=0.280, y=0.00
L1_right -> track id 3, x=0.280, y=0.15
```

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

## Run Individual Nodes

In separate terminals:

```bash
python3 scripts/goal_state_publisher_node.py
python3 scripts/llm_node.py --ros-args -p model:=qwen3.6:35b -p ollama_url:=http://localhost:11434/api/chat
python3 scripts/fake_aggregator_node.py
python3 scripts/fake_digital_twin_node.py
python3 scripts/plan_executor_node.py --ros-args -p dry_run:=false
```

## Parameters

- `api_url_pyramid`: default `http://localhost:8000/api/robot/skill/pyramid`
- `dry_run`: default `true`
- `initial_command_delay_s`: fake aggregator waits before publishing
  `/user_command` once, default `2.0`

## Test

```bash
python3 -m unittest discover -s tests
```
