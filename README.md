# test_v1.0

Temporary integration experiment for the YARR LLM closed loop.

This repo intentionally avoids the real digital twin dependency. The temporary
executor subscribes to `/llm_output`, uses operator-provided fake pick XY
coordinates by `target_slot`, calls the pyramid API with `{x, y, slot}`, and
publishes `/action_result`.

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

## Run Temp Executor

`FAKE_XY_BY_SLOT_JSON` must be provided by the operator. The repo does not define
fake cup coordinates.

```bash
export FAKE_XY_BY_SLOT_JSON='{"L1_left":[0.280,-0.15],"L1_mid":[0.280,0.0],"L1_right":[0.280,0.15]}'
./start.sh
```

To call the real pyramid API:

```bash
export FAKE_XY_BY_SLOT_JSON='{"L1_left":[0.280,-0.15],"L1_mid":[0.280,0.0],"L1_right":[0.280,0.15]}'
./start.sh --real-api
```

## Run Individual Nodes

In separate terminals:

```bash
python3 scripts/goal_state_publisher_node.py
python3 scripts/llm_node.py --ros-args -p model:=qwen3.6:35b -p ollama_url:=http://localhost:11434/api/chat
FAKE_XY_BY_SLOT_JSON='{"L1_left":[0.280,-0.15],"L1_mid":[0.280,0.0],"L1_right":[0.280,0.15]}' ./start.sh
```

## Parameters

- `api_url_pyramid`: default `http://localhost:8000/api/robot/skill/pyramid`
- `dry_run`: default `true`
- `execute_on_cold_start`: default `true`
- `fake_xy_by_slot_json`: required, no default coordinates

## Test

```bash
python3 -m unittest discover -s tests
```
