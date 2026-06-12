# cup-stack-integration

Integration layer for the **YARR** cup-stacking robot: it turns a natural-language
command into a physical 3‑2‑1 cup pyramid built by a Doosan M0609 arm, driven by
an LLM planning loop on top of a REST robot-control server.

```
"3단 피라미드 쌓아줘"
        │
        ▼
  LLM planner ──▶ plan executor ──▶  POST /api/robot/skill/pyramid
 (cup_stack_agent, ROS 2)            (server, FastAPI → ROS 2 → robot)
```

## Repository layout

As of 2026-06-12 the submodules are **flattened to the repo root** (the old
`cup-stack-server/` aggregation layer and `vision/` directory were dissolved):

| Path | What it is |
|------|------------|
| `cup_stack_agent/` | LLM closed-loop ROS 2 experiment — fake perception + planner + executor that POSTs the robot API (own code) |
| `server/` | **submodule** — FastAPI REST + rosbridge gateway (`server/start.sh` = tmux entrypoint) |
| `ros2-cup-stack/` | **submodule** — ROS 2 Humble + MoveIt 2 + OnRobot gripper (nests `ros2/src/doosan-robot2`) |
| `frontend/` | **submodule** — React dashboard |
| `fallen-cup-recovery/` | **submodule** (`released`) — fallen-cup recovery skill |
| `ros2-depth-point-cloude/` | **submodule** — `depth_digital_twin` (detection + 3D boxes) |
| `ros2-recode-sequence/` | **submodule** — `recode_sequence` (cameras); merge into depth pending |
| `vision-node/` | **submodule** — `cup_stacking_verify` (`/stack` slot verifier) |
| `ros2-skill-manager/` | **submodule** — operator GUI + `run_skill_manager.sh` |
| `script/` | launcher symlinks (→ `server/*`, `ros2-skill-manager/run_skill_manager.sh`) + `send_command.sh`, `vision_rviz.sh` |
| `docs/`, `CLAUDE.md` | integration docs + agent guidance |

Clone with submodules:

```bash
git clone --recurse-submodules https://github.com/2026-yarr-robotics/cup-stack-integration
# or, after a plain clone:
git submodule update --init --recursive
```

## How it works

`cup_stack_agent` runs the real ROS topic pipeline with **fake** perception so the
planner / executor / API request path can be validated without a perception stack.
Fake data is injected through the *same* topics the real nodes use.

```
fake_aggregator_node    → /cups_on_table /stack /user_command
fake_digital_twin_node  → /digital_twin/boxes /stack_track_ids   (measured cup poses)
goal_state_publisher    → /llm_input        (merges command + world state + last result)
llm_node                → /llm_output       (Ollama: cold-start plan / in-flight decide)
plan_executor_node      → POST robot API → /action_result
pick_node               → hand-eye fine pick (real-api mode only)
```

The fixed scenario builds a full 3‑level pyramid (6 cups) over six API calls,
mapping LLM slots → API slots:

```
L1_left→1l  L1_mid→1m  L1_right→1r  L2_left→2l  L2_right→2r  L3_top→3m
```

A disturbance is simulated by default (a cup is "removed" after `L2_right`), so the
in-flight LLM loop must detect it and re-pick. The robot server is an **execution
layer only** — it trusts the provided `(x, y)`, maps the API slot to its own
pyramid geometry, and runs the motion; it does not plan, choose cups, or verify.

Full node contract, scenario, and triage: **`cup_stack_agent/docs/experiment_runbook.md`**.

## Quickstart

```bash
cd cup_stack_agent

# Dry-run: logs each executor request body, publishes success, no real API calls
./start.sh

# Closed loop: actually POSTs the robot API (needs the robot server reachable)
./start.sh --real-api
```

`pick_node` (real-api) imports `moveit_py`; `start.sh` auto-sources
`/home/ssu/ros2_ws/install/setup.bash` (override via `MOVEIT_SETUP`).

Useful environment variables:

| Var | Default |
|-----|---------|
| `API_URL` | `https://yarr-api-31.simplyimg.com/api/robot/...` |
| `MODEL` | `qwen3.6:35b` |
| `OLLAMA_URL` | `http://localhost:11434/api/chat` |
| `DISTURBANCE_ENABLED` | `true` |
| `API_TIMEOUT_S` | `180.0` |

## Robot control server

The `server/` submodule is the real robot stack and runs independently
(Docker: nginx + per-domain FastAPI services + rosbridge). It exposes the REST API
the agent calls — e.g. `POST /api/robot/move`, `POST /api/robot/skill/pyramid`,
`POST /api/robot/skill/unstack` (reverse: pick a pyramid slot → nest at x,y),
`GET/POST /api/robot/config/pyramid`. `server/start.sh` is the single tmux
entrypoint and sources sibling submodules via `../<pkg>/install/setup.bash`. See
`server/CLAUDE.md` and [`docs/deploy_migration_policy.md`](docs/deploy_migration_policy.md).

## Tests

```bash
cd cup_stack_agent
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n start.sh
```

## Conventions

- Branch: `main`. Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).
- Changes inside any submodule are authored as **dwl21**; land them via a
  `chore/…` branch + PR, then advance the submodule pointer with a `chore:` commit
  in the parent. See `CLAUDE.md`.
- Keep the fake experiment's I/O identical to the real ROS pipeline — do not add
  alternate topics or pass coordinates through CLI/params.
