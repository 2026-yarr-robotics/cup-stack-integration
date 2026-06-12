# CLAUDE.md

Guidance for Claude Code (claude.ai/code) when working in this repository.

## What this repo is

`cup-stack-integration` is the top-level integration layer that drives a Doosan
M0609 cup-stacking robot from a natural-language command through an LLM planning
loop and a REST control server.

> **Layout note (2026-06-12 flatten):** the `cup-stack-server` aggregation layer
> and the `vision/` directory were **dissolved**. All leaf repos are now
> submodules at the integration **root** (pinned to each repo's latest tip). The
> old `yarr-robust-speed-stack` duplicate copies are gone.

```
cup-stack-integration/
├── cup_stack_agent/           # LLM closed-loop ROS 2 experiment (planner → executor) — own code
├── server/                    # submodule: FastAPI REST + rosbridge gateway
├── ros2-cup-stack/            # submodule: ROS 2 Humble, MoveIt 2, OnRobot gripper
│   └── ros2/src/doosan-robot2 # nested submodule (@yarr-integration): M0609 driver
├── frontend/                  # submodule: React dashboard
├── fallen-cup-recovery/       # submodule (@released): fallen-cup recovery skill
├── ros2-depth-point-cloude/   # submodule: depth_digital_twin (detection + 3D boxes)
├── ros2-recode-sequence/      # submodule: recode_sequence (cameras) — merge into depth pending
├── vision-node/               # submodule: cup_stacking_verify (/stack slot verifier)
├── ros2-skill-manager/        # submodule: operator GUI (Pick/Pyramid/UpdateInput) + run_skill_manager.sh
├── script/                    # launcher symlinks → server/{start,stop,attach,bringup_real} &
│                              #   ros2-skill-manager/run_skill_manager.sh; + send_command.sh, vision_rviz.sh
├── docs/                      # integration-level docs
└── CLAUDE.md
```

> Operational scripts live in their owning submodule (`server/{start,stop,attach,
> bringup_real_31}.sh`, demo scripts in `server/script/`, `ros2-skill-manager/run_skill_manager.sh`);
> the root `script/` holds **symlinks** to them plus the integration-owned
> `send_command.sh` and `vision_rviz.sh`.

- **`cup_stack_agent/`** — ROS 2 Python nodes that turn a user command
  ("3단 피라미드 쌓아줘") into pyramid pick-and-place API calls. Perception is
  faked but emitted over the *same* ROS topics the real pipeline uses, so the
  planner/executor/API path is exercised end-to-end. Full spec:
  `cup_stack_agent/docs/experiment_runbook.md` and the project `README.md`.
- **`server/`** — FastAPI REST + rosbridge gateway. Exposes
  `POST /api/robot/skill/pyramid`, `POST /api/robot/skill/unstack` (slot → nested
  column), `/api/robot/move`, `/api/robot/config/pyramid`, etc. `server/start.sh`
  is the single tmux entrypoint; it sources sibling submodules via `../<pkg>/install/setup.bash`.
- **`ros2-cup-stack/`** — ROS 2 Humble cup_stack skill + MoveIt 2; nests
  `doosan-robot2` under `ros2/src/`.
- **perception** — `ros2-depth-point-cloude` (`depth_digital_twin`: detection + 3D
  boxes), `ros2-recode-sequence` (`recode_sequence`: cameras; archived upstream —
  planned to merge into depth), and `vision-node` (`cup_stacking_verify`: the
  `/stack` slot verifier).

> ⚠️ **Pyramid placement geometry is owned by the FastAPI server**
> (`server/server/domains/robot.py`: `PYRAMID_CUP_SPACING`, `PYRAMID_LAYER_HEIGHT`,
> `DEFAULT_PYRAMID_DEGREE`). The verifier (`vision-node/.../verifier_node.py`:
> `cup_ref_w`, `layer_gap`, `degree`) must mirror those, or judged slots won't line
> up with where cups are placed. The runtime executor is
> `cup_stack_agent/scripts/plan_executor_node.py` (coarse `/move`, no pyramid
> geometry; placement is the server's job).

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

- Flat hierarchy: `cup-stack-integration` (`main`) ▷ root submodules
  `{server, ros2-cup-stack (▷ doosan-robot2), frontend, fallen-cup-recovery,
  ros2-depth-point-cloude, vision-node, ros2-skill-manager}`. Fresh clone:
  `git submodule update --init --recursive`.
- Commits **inside any submodule** (server, ros2-cup-stack, frontend,
  fallen-cup-recovery, ros2-depth-point-cloude, vision-node, …) are authored as
  **dwl21 <nggus5@gmail.com>**. The top-level `cup-stack-integration` repo uses the
  checkout's own git user.
- Submodule changes go via a `chore/…` branch + PR in the inner repo, then bump
  the pointer in the parent (`git add <submodule>` → commit). Pointers track each
  repo's latest default-branch tip (`fallen-cup-recovery` → `released`).
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`).

## Deployment

See [`docs/deploy_migration_policy.md`](docs/deploy_migration_policy.md) for the
post-flatten deployment policy. In short: the live "31" host historically ran from
a **separate `cup-stack-server` checkout** at `/home/ssu/cup-stack`
(`docker compose` + the `cup-stack` tmux session; live API
`yarr-api-31.simplyimg.com`). The policy migrates that deploy onto a checkout of
this flattened integration repo (the `server/start.sh` relative paths were already
fixed for the root layout). To ship a server change: merge the submodule PR, bump
the pointer here, then on the deploy host `git pull --recurse-submodules` and
`docker compose build robot && docker compose up -d --force-recreate robot`
(`restart` does not pick up image or volume changes).
