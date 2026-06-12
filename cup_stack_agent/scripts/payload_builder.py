"""Pure (ROS-free) builder for the LLM input payload.

The Goal State Publisher node feeds raw topic data into a `GoalStateBuilder`
and asks it for the 7-field LLM input payload defined in
`docs/pipeline_io_spec_comprehensive.md` §3.1:

    {
      "user_command":         str | null,
      "current_world_state":  WorldState | null,
      "fallen_count":         int,              # tipped-over cups (interrupt trigger)
      "previous_world_state": WorldState | null,
      "robot_state":          RobotState,
      "current_plan":         Plan | null,
      "current_goal":         Step | null,
      "last_action_result":   ActionResult | null,
      "mode":                 "cold_start" | "in_flight"   # additive routing hint
    }

Keeping this logic free of `rclpy` lets it be unit-tested offline with the
same labeled scenarios the prompt benchmarks use.

Two normalizations happen here because upstream perception emits a flatter
shape than the LLM contract expects:

  * `/stack` arrives as ``{"L1_left": "red", "L1_mid": null}`` (slot→color str)
    but the contract wants ``{"L1_left": {"color": "red"}, "L1_mid": null}``.
  * `robot_state.gripper.holding` may arrive as a bare color string; the
    contract wants ``{"color": "red"}`` or ``null``.
"""
from __future__ import annotations

from typing import Any

# Fixed 6-slot pyramid schema (spec §2.2). total_slots is always len(STACK_SLOTS).
STACK_SLOTS = [
    'L1_left', 'L1_mid', 'L1_right',
    'L2_left', 'L2_right', 'L3_top',
]

# vision-node's verifier publishes /stack with the short-form keys below.
# Map them to the canonical long form the LLM prompts expect so callers can
# feed raw verifier output straight in.
_VERIFIER_TO_CANONICAL: dict[str, str] = {
    'L1_L': 'L1_left', 'L1_M': 'L1_mid', 'L1_R': 'L1_right',
    'L2_L': 'L2_left', 'L2_R': 'L2_right',
    'L3_T': 'L3_top',
}

EMPTY_GRIPPER: dict[str, Any] = {'gripper': {'holding': None, 'force_n': 0.0}}


def normalize_stack(stack: dict | None) -> dict[str, Any]:
    """slot→(color str|{color}|null)  →  slot→({"color": str}|null), all 6 slots.

    Accepts:
      * verifier short-form keys (``L1_L``..``L3_T``) and translates them,
      * canonical long-form keys (``L1_left``..``L3_top``),
      * values as bare color string OR ``{"color": str}``.
    Missing slots default to null so downstream always sees the full 6-slot
    schema with the canonical key spelling.
    """
    stack = stack or {}
    # Canonical keys win over short-form aliases when both are supplied
    # (defensive against double-fill). Pass 1 takes canonical entries; pass 2
    # fills in any remaining slots from their short-form alias.
    normalized_in: dict[str, Any] = {}
    for slot in STACK_SLOTS:
        if slot in stack:
            normalized_in[slot] = stack[slot]
    for short, canonical in _VERIFIER_TO_CANONICAL.items():
        if canonical not in normalized_in and short in stack:
            normalized_in[canonical] = stack[short]
    out: dict[str, Any] = {}
    for slot in STACK_SLOTS:
        val = normalized_in.get(slot)
        if val is None:
            out[slot] = None
        elif isinstance(val, str):
            out[slot] = {'color': val}
        elif isinstance(val, dict) and 'color' in val:
            out[slot] = {'color': val['color']}
        else:
            out[slot] = None
    return out


def normalize_fallen_count(value: Any) -> int:
    """Coerce a fallen-cup count into a non-negative int.

    The hand-eye vision (/fallen_cups {"count": N}) reports a bare count —
    no color. Bools / non-int-coercible / negative values → 0, so
    ``0 == no fallen cups`` always holds for downstream consumers (LLM
    decision rule).
    """
    if isinstance(value, bool):
        return 0
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, count)


def normalize_robot_state(robot_state: dict | None) -> dict[str, Any]:
    """Coerce a robot_state into the contract shape, defaulting to empty grip.

    Tolerates ``holding`` given as a bare color string or as ``{"color": ...}``.
    """
    if not robot_state:
        return {'gripper': {'holding': None, 'force_n': 0.0}}
    gripper = robot_state.get('gripper') or {}
    holding = gripper.get('holding')
    if holding is True:
        # Physical grip detected but color unknown — filled from pick context.
        holding = {'color': None}
    elif isinstance(holding, str):
        holding = {'color': holding}
    elif isinstance(holding, dict) and 'color' in holding:
        holding = {'color': holding['color']}
    else:
        holding = None
    force_n = gripper.get('force_n', 0.0)
    try:
        force_n = float(force_n)
    except (TypeError, ValueError):
        force_n = 0.0
    return {'gripper': {'holding': holding, 'force_n': force_n}}


def stack_slot_color(world_state: dict | None, slot: str | None) -> str | None:
    if not world_state or not slot:
        return None
    stack = world_state.get('stack') or {}
    val = stack.get(slot)
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        color = val.get('color')
        return color if isinstance(color, str) else None
    return None


def action_result_reflected(
    result: dict | None,
    before: dict | None,
    current: dict | None,
) -> bool:
    """Whether world state reflects a successful atomic pyramid action.

    For this experiment a successful pyramid action must fill the target slot.
    In the normal flow the table count for that color decreases by one. In the
    disturbance scenario another stacked cup may be removed at the same time,
    making the table count stay unchanged. Publish once the target slot is
    reflected and the table count has not increased beyond the previous count.
    """
    if not result:
        return False
    if result.get('result') != 'success':
        return True
    if result.get('action') != 'pyramid':
        return True
    color = result.get('color')
    target_slot = result.get('target_slot')
    if not isinstance(color, str) or not isinstance(target_slot, str):
        return False
    if not before or not current:
        return False
    if stack_slot_color(before, target_slot) == color:
        return False
    if stack_slot_color(current, target_slot) != color:
        return False
    before_cups = before.get('cups_on_table') or {}
    current_cups = current.get('cups_on_table') or {}
    try:
        before_count = int(before_cups.get(color, 0))
        current_count = int(current_cups.get(color, 0))
    except (TypeError, ValueError):
        return False
    return current_count <= before_count


class GoalStateBuilder:
    """Holds the GSP state and assembles the LLM input payload on demand."""

    def __init__(self) -> None:
        self._cups_on_table: dict | None = None
        self._stack: dict | None = None
        self._robot_state: dict | None = None
        self._user_command: str | None = None
        self._current_plan: dict | None = None
        self._last_action_result: dict | None = None
        # Color of the cup currently held, inferred from the last successful
        # pick (the gripper itself can't sense color). Filled into robot_state.
        self._held_color: str | None = None
        # Snapshot of current_world_state sent in the *previous* /llm_input.
        self._prev_world_state: dict | None = None
        self._plan_counter: int = 0
        # Fallen-cup count from the hand-eye vision (/fallen_cups). Kept
        # OUTSIDE current_world_state: it is the LLM's interrupt trigger, not
        # part of the buildable-world contract — the hand-eye must NEVER feed
        # cups_on_table/stack (it would double-count cups the exo also sees).
        self._fallen_count: int = 0

    # ── Inputs ────────────────────────────────────────────────────────────

    def set_world(self, cups_on_table: dict | None, stack: dict | None) -> None:
        """Update from /system_state (aggregator output)."""
        if cups_on_table is not None:
            self._cups_on_table = cups_on_table
        if stack is not None:
            self._stack = stack

    def set_robot_state(self, robot_state: dict | None) -> None:
        self._robot_state = robot_state

    def set_fallen_count(self, count: Any) -> None:
        """Update the fallen-cup count from the hand-eye vision (/fallen_cups).

        Deliberately NOT cleared by set_user_command(): fallen is perception
        state (a cup is physically tipped over), independent of plan resets.
        """
        self._fallen_count = normalize_fallen_count(count)

    def fallen_count(self) -> int:
        """Current fallen-cup count (0 when none)."""
        return self._fallen_count

    def set_user_command(self, command: str | None) -> None:
        """A new user command starts a fresh task: cold-start mode.

        Plan/goal/result and the previous-world snapshot are cleared so the
        next payload is a clean cold-start (current_plan == null).
        """
        self._user_command = command
        self._current_plan = None
        self._last_action_result = None
        self._prev_world_state = None

    def set_plan(self, plan: dict | None) -> None:
        """Adopt a plan produced downstream (future LLM cold-start/replan output).

        Accepts either a bare plan (``{"steps": [...], "target": {...}}``) or
        the full LLM output. Cold-start puts ``target`` next to ``plan`` at the
        top level, while in-flight replan may put ``target`` inside ``plan``.
        Clearing the command here flips subsequent payloads into in-flight mode.
        """
        if plan is None:
            self._current_plan = None
            return
        plan_obj = plan.get('plan') if isinstance(plan.get('plan'), dict) else plan
        steps = list((plan_obj or {}).get('steps') or [])
        target = (plan_obj or {}).get('target')
        if target is None:
            target = plan.get('target')
        target_pattern = (plan_obj or {}).get('target_pattern')
        if target_pattern is None:
            target_pattern = plan.get('target_pattern')
        self._plan_counter += 1
        self._current_plan = {
            'plan_id': f'plan_{self._plan_counter:03d}',
            'target': target,
            'remaining_steps': steps,
        }
        if target_pattern is not None:
            self._current_plan['target_pattern'] = target_pattern
        self._user_command = None  # task is now planned → in-flight from here

    def on_action_result(self, result: dict | None) -> None:
        """Record a skill result and advance the plan past it on success."""
        self._last_action_result = result
        if not result:
            return
        if result.get('action') == 'fallen_recovery':
            # Recovery is an INTERRUPT, not a plan step: remaining_steps,
            # current_goal, and held color all stay untouched (its step is
            # null so the step-match below would skip anyway — this guard
            # makes the contract explicit).
            return
        # Track held color from successful picks/places (color the gripper
        # can't sense). A successful pick holds that color; a place empties it.
        if result.get('result') == 'success':
            if result.get('action') == 'pick':
                self._held_color = result.get('color')
            elif result.get('action') == 'place':
                self._held_color = None
        if not self._current_plan:
            return
        if result.get('result') != 'success':
            return
        steps = self._current_plan.get('remaining_steps') or []
        for i, step in enumerate(steps):
            if step.get('step') == result.get('step'):
                # plan_executor silently drops steps whose slot is already
                # occupied (no action_result is ever emitted for them), so a
                # success can land mid-list. Consume the skipped steps along
                # with the matched one — advancing only on an exact head
                # match would freeze remaining_steps/current_goal for the
                # rest of the run (stale plan -> LLM emits an invalid done).
                self._current_plan['remaining_steps'] = steps[i + 1:]
                break

    # ── Derived views ─────────────────────────────────────────────────────

    def current_world_state(self) -> dict | None:
        """Assemble WorldState (spec §2.3), computing filled/total slots."""
        if self._cups_on_table is None and self._stack is None:
            return None
        stack = normalize_stack(self._stack)
        filled = sum(1 for v in stack.values() if v is not None)
        return {
            'cups_on_table': self._cups_on_table or {},
            'stack': stack,
            'filled_slots': filled,
            'total_slots': len(STACK_SLOTS),
        }

    def previous_world_state(self) -> dict | None:
        """WorldState snapshot used in the last published LLM payload."""
        return self._prev_world_state

    def current_goal(self) -> dict | None:
        """The next step to execute = remaining_steps[0], or null when done."""
        if not self._current_plan:
            return None
        steps = self._current_plan.get('remaining_steps') or []
        return steps[0] if steps else None

    def remaining_slots(self) -> set:
        """Canonical target_slots of steps NOT yet executed (future slots)."""
        if not self._current_plan:
            return set()
        steps = self._current_plan.get('remaining_steps') or []
        return {str(s.get('target_slot')) for s in steps
                if s.get('target_slot')}

    def mode(self) -> str:
        """cold_start while no plan exists, in_flight once a plan is set."""
        return 'cold_start' if self._current_plan is None else 'in_flight'

    def _robot_state_payload(self) -> dict[str, Any]:
        """RobotState with the held color filled in from pick context.

        robot_state_node reports `holding` as a boolean (the gripper senses a
        grip but not its color). When physically holding and the color isn't
        already known, fill it from the last successful pick.
        """
        rs = normalize_robot_state(self._robot_state)
        holding = rs['gripper']['holding']
        if holding is not None and holding.get('color') is None:
            rs['gripper']['holding'] = {'color': self._held_color}
        return rs

    # ── Payload ───────────────────────────────────────────────────────────

    def build_payload(self) -> dict[str, Any]:
        """Assemble the 7-field LLM input payload (+ mode hint).

        Cold-start fills user_command and leaves plan/goal/result/previous
        null; in-flight carries plan/goal/result and the previous-world
        snapshot. Call `commit()` after publishing to roll the snapshot
        forward for disturbance detection on the next cycle.
        """
        cold = self.mode() == 'cold_start'
        cws = self.current_world_state()
        return {
            'user_command': self._user_command if cold else None,
            'current_world_state': cws,
            'fallen_count': self._fallen_count,
            'previous_world_state': None if cold else self._prev_world_state,
            'robot_state': self._robot_state_payload(),
            'current_plan': self._current_plan,
            'current_goal': self.current_goal(),
            'last_action_result': None if cold else self._last_action_result,
            'mode': self.mode(),
        }

    def commit(self, payload: dict[str, Any]) -> None:
        """Roll the world snapshot forward after a payload is published.

        previous_world_state on the next cycle is the current_world_state we
        just sent — the basis the decider uses to tell a normal delta from a
        disturbance (spec §3.1).
        """
        self._prev_world_state = payload.get('current_world_state')
