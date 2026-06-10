# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this repo is

`cup-stack-integration` is the top-level integration layer that drives a Doosan
M0609 cup-stacking robot from a natural-language command through an LLM planning
loop and a REST control server. It ties two pieces together:

```
cup-stack-integration/
├── cup_stack_agent/     # LLM closed-loop ROS 2 experiment (planner → executor)
├── cup-stack-server/    # git submodule: robot control stack (ROS + FastAPI + web)
└── docs/                # integration-level docs
```

- **`cup_stack_agent/`** — ROS 2 Python nodes that turn a user command
  ("3단 피라미드 쌓아줘") into pyramid pick-and-place API calls. Perception is
  faked but emitted over the *same* ROS topics the real pipeline uses, so the
  planner/executor/API path is exercised end-to-end. Full spec:
  `cup_stack_agent/docs/experiment_runbook.md` and the project `README.md`.
- **`cup-stack-server/`** — submodule (`github.com/2026-yarr-robotics/cup-stack-server`)
  holding the robot motion stack: `ros2-cup-stack/` (ROS 2 Humble, MoveIt 2,
  OnRobot gripper), `server/` (FastAPI REST + rosbridge), `frontend/` (React
  dashboard). It exposes `POST /api/robot/skill/pyramid`,
  `POST /api/robot/skill/unstack` (the reverse: slot → nested column),
  `/api/robot/move`, `/api/robot/config/pyramid`, etc. See its own `CLAUDE.md`.
- **`vision/`** — the real perception submodules: `vision/ros2-depth-point-cloude`
  (depth_digital_twin: detection + 3D boxes), `vision/ros2-recode-sequence`
  (RealSense `cameras_only.launch.py`), `vision/vision-node` (cup_stacking_verify:
  the `/stack` slot verifier).

> ⚠️ **Canonical vision copies live under `vision/` — edit those, not the
> duplicates.** `vision-node`, `ros2-depth-point-cloude`, and `ros2-recode-sequence`
> are *also* checked out as nested submodules under
> `cup-stack-server/yarr-robust-speed-stack/`, but **nothing at runtime uses those
> nested copies.** The run scripts (`cup-stack-server/server/start.sh`) source
> `vision/<pkg>/install/setup.bash`, so a change must land in `vision/<pkg>/` (and
> be `colcon build`-ed there) to take effect. Editing the
> `yarr-robust-speed-stack/<pkg>` copy is a silent no-op for the live system.
>
> Likewise the pyramid **placement** geometry is owned by the FastAPI server
> (`cup-stack-server/server/server/domains/robot.py`: `PYRAMID_CUP_SPACING`,
> `PYRAMID_LAYER_HEIGHT`, `DEFAULT_PYRAMID_DEGREE`). The verifier
> (`vision/vision-node` `verifier_node.py`: `cup_ref_w`, `layer_gap`, `degree`)
> must mirror those, or judged slots won't line up with where cups are placed.
> The `yarr-robust-speed-stack/system_state_aggregator/plan_executor_node.py` has
> its own copy of this geometry but is **not** the runtime executor —
> `cup_stack_agent/scripts/plan_executor_node.py` is (and it does no pyramid
> geometry; placement is the FastAPI server's job).

## Architecture (the closed loop)

```
fake_aggregator_node    → /cups_on_table /stack /user_command
fake_digital_twin_node  → /digital_twin/boxes /stack_track_ids   (measured cup poses)
goal_state_publisher    → /llm_input        (merges command + world + result)
llm_node                → /llm_output       (Ollama: cold-start plan / in-flight decide)
plan_executor_node      → POST /api/robot/move | /api/robot/skill/pyramid → /action_result
pick_node               → hand-eye fine pick (real-api mode only; needs moveit_py)
```

Core rule: the experiment is fake, but the I/O must match the real ROS pipeline —
do not invent alternate topics or pass fake coordinates through CLI/params.

## Running

```bash
cd cup_stack_agent
./start.sh                 # dry-run: logs request bodies, no real API calls
./start.sh --real-api      # closes the loop; POSTs the real robot API
```

`pick_node` (real-api) imports `moveit_py`; `start.sh` auto-sources
`/home/ssu/ros2_ws/install/setup.bash` (override `MOVEIT_SETUP`). Key env vars:
`API_URL`, `MODEL` (default `qwen3.6:35b`), `OLLAMA_URL`, `DISTURBANCE_ENABLED`.

Tests:

```bash
cd cup_stack_agent
python3 -m unittest discover -s tests -v
python3 -m py_compile scripts/*.py
bash -n start.sh
```

## Submodules & commits

- Hierarchy: `cup-stack-integration` (branch `v1.1`) ▷ `cup-stack-server` (`main`)
  ▷ `{server, ros2-cup-stack, yarr-robust-speed-stack}`.
- Changes **inside the `cup-stack-server` tree** (incl. its `server` and
  `ros2-cup-stack` submodules) are authored as **dwl21 <nggus5@gmail.com>**.
  The top-level `cup-stack-integration` repo keeps its own author (`sonicwarp`).
- To advance a submodule pointer: commit/push in the inner repo first, then
  `git add <submodule>` and commit the pointer bump in the parent. The
  `yarr-robust-speed-stack` submodule is usually dirty — leave it unstaged.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).

## Deployment note (the "31" host)

The live system runs from a **separate checkout** at `/home/ssu/cup-stack`
(not this integration checkout). `./start.sh` there brings up the `cup-stack`
tmux session and the Docker stack; the live API is `yarr-api-31.simplyimg.com`.
To ship a server change: commit/push here, then in `/home/ssu/cup-stack/server`
`git pull` and `docker compose build robot && docker compose up -d --force-recreate robot`
(`restart` does not pick up image or volume changes).
