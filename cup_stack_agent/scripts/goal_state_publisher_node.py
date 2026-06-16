"""goal_state_publisher_node — build the 7-field LLM input from system topics.

This node sits one layer above `system_state_aggregator`. It does NOT touch the
already-finished perception/aggregator nodes; it only *subscribes* to what they
publish, layers on the planner-side state (plan / goal / last action result /
previous world snapshot), and republishes a single LLM-ready payload.

  subscribe
    /cups_on_table  (std_msgs/String JSON)  {color: count}      ← point_cloud_node
    /stack          (std_msgs/String JSON)  {slot: color|null}  ← verifier_node
    /robot_state    (std_msgs/String JSON)  {gripper:{holding, force_n}} ← robot driver*
    /user_command   (std_msgs/String)        raw command text             ← UI / CLI
    /action_result  (std_msgs/String JSON)  {step, action, result, ...}  ← skill executor*
    /llm_output     (std_msgs/String JSON)  {plan|steps, ...}             ← LLM node (future)*

  publish
    /llm_input      (std_msgs/String JSON)   the 7-field payload (+ mode hint)

  * Topics not built yet by other teams. Until they appear, robot_state defaults
    to an empty gripper and the plan stays null (cold-start). Nothing blocks.

LLM triggers (spec §1.2) realized here: a new /user_command publishes a
cold-start payload; a new /action_result publishes an in-flight payload. The
node only builds the payload — it does NOT call the LLM (that is the next phase,
which will consume /llm_input and publish /llm_output back here).
"""
from __future__ import annotations

import json
import traceback

import rclpy
from rclpy.node import Node
from rclpy.executors import ExternalShutdownException
from std_msgs.msg import String

from payload_builder import (
    GoalStateBuilder, action_result_reflected, normalize_fallen_count,
    world_changed,
)


class GoalStatePublisher(Node):
    def __init__(self) -> None:
        super().__init__('goal_state_publisher')

        self.declare_parameter('cups_on_table_topic', '/cups_on_table')
        self.declare_parameter('stack_topic', '/stack')
        self.declare_parameter('robot_state_topic', '/robot_state')
        self.declare_parameter('user_command_topic', '/user_command')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter('llm_output_topic', '/llm_output')
        self.declare_parameter('llm_input_topic', '/llm_input')
        # DEFERRED (#7): re-emit on idle world change for disturbance reaction.
        # Turned OFF — the always-on polling published an llm_input per verifier
        # flicker, creating a decision backlog that broke step atomicity. The
        # disturbance reaction will be redone via the stop mechanism (#8) later;
        # the machinery (_should_republish_on_change/world_changed) stays inert
        # for that. Set true only to experiment.
        self.declare_parameter('publish_on_world_change', False)
        self.declare_parameter('strict_json', True)
        # Freeze the perception world-state while a pyramid action is in
        # flight (arm in view -> counts fluctuate). Resume on /action_result.
        # Stops mid-execution noise corrupting the world / spoofing the
        # action-reflection check.
        self.declare_parameter('freeze_world_during_action', True)
        self.declare_parameter('freeze_timeout_s', 60.0)
        # After /action_result (arm homed) keep the world frozen this much
        # longer so perception settles before resuming. 0.5s keeps the loop
        # tight now that the arm parks at the exact joint HOME (no lifted-
        # home drift to wait out); raise it if post-place counts flicker.
        self.declare_parameter('unfreeze_settle_s', 0.5)
        # Hand-eye fallback at the DECISION moment only (cold-start /
        # post-action unfreeze): if exo cups_on_table is empty THEN, use the
        # hand-eye counts. Not a continuous supplement.
        self.declare_parameter('handeye_fallback', True)
        self.declare_parameter('handeye_cups_topic',
                               '/vision/cups_on_table_handeye')
        self.declare_parameter('handeye_ttl_s', 1.5)
        # Hold a FUTURE step's slot null until its /stack occupancy is stable
        # this long — a raw-stack false-positive on a not-yet-built slot must
        # not flip the world to 'all filled' -> premature done. The just-built
        # slot still reflects immediately; next-step execution is NOT delayed.
        self.declare_parameter('future_slot_debounce_s', 3.0)
        # Give up waiting for a fresh /cups_on_table after an action and
        # advance on the last world if perception stalls this long.
        self.declare_parameter('pending_fresh_timeout_s', 5.0)
        # If perception never reflects an action's effect (verifier flicker /
        # stack loss), publish the decision anyway after this long so the loop
        # can NEVER hang on the reflection gate — the LLM then sees the current
        # world and replans/decides (a "success but slot still null" reads as a
        # contradicted success -> replan).
        self.declare_parameter('reflect_timeout_s', 10.0)
        # Hand-eye fallen count {"count": N} published every frame by
        # upright_cup_pose_node (same camera/inference as /hand_eye/boxes).
        # Gated into the payload top-level `fallen_count` at the DECISION
        # moment only, and ONLY when no graspable upright cup remains (exo
        # AND hand-eye fill both empty) — upright cups always win, because
        # recovery is physically impossible with upright cups nearby. The
        # hand-eye never feeds cups_on_table/stack (exo owns the world;
        # double-count hazard).
        self.declare_parameter('fallen_cups_topic', '/fallen_cups')
        self.declare_parameter('fallen_ttl_s', 1.5)
        # After a fallen_recovery result, hold the in-flight /llm_input until
        # a hand-eye fallen sample taken AFTER the recovery lands (frozen-
        # period samples are rejected, so the first accepted one is post-
        # settle, arm at HOME). Publishing earlier would gate fallen_count on
        # the stale pre-recovery sample. On timeout publish anyway so the LLM
        # can decide retry/replan.
        self.declare_parameter('recovery_clear_timeout_s', 8.0)
        # After a recovery finishes, hold the post-recovery decision at least
        # this long so exo cups_on_table reflects the stood-up cup (exo window
        # ~1-2s) before the LLM decides — else it may decide on a world that
        # hasn't shown the recovered cup yet.
        self.declare_parameter('recovery_settle_s', 1.5)
        # Safety: while a fallen_recovery is the in-flight action the world
        # stays frozen until its /action_result. recovery can take 88-214s
        # (>> the 60s default freeze_timeout), so use a recovery-specific
        # timeout ABOVE plan_executor's recovery_timeout_s(240) — else the
        # freeze expires mid-recovery and starts accepting garbage mid-motion
        # /fallen_cups samples that poison the gate.
        self.declare_parameter('recovery_freeze_timeout_s', 260.0)
        # Anti-runaway: cap consecutive fallen_recovery dispatches that make no
        # progress (e.g. the orchestrator no-ops because its grasp detector
        # cannot see the cup the gate sees). Reset on pyramid progress / new
        # command. Normal single-run recovery clears the cup in one pass and
        # never approaches this; it only breaks a pathological re-fire loop.
        # 0 disables the cap.
        self.declare_parameter('max_consecutive_recoveries', 6)
        # Done-race guard: after an action empties the last upright cup while
        # the target still has null slots, hold the decision up to this long
        # for a FRESH hand-eye fallen sample, so the decider sees the real
        # fallen_count instead of a stale 0 (which would call a premature
        # done(partial) and never recover the fallen cup). Bounded so a missing
        # hand-eye never stalls the loop.
        self.declare_parameter('fallen_settle_wait_s', 3.0)

        self._strict = bool(self.get_parameter('strict_json').value)
        self._on_world_change = bool(
            self.get_parameter('publish_on_world_change').value)
        self._freeze_enabled = bool(
            self.get_parameter('freeze_world_during_action').value)
        self._freeze_timeout_s = float(
            self.get_parameter('freeze_timeout_s').value)
        self._unfreeze_settle_s = float(
            self.get_parameter('unfreeze_settle_s').value)
        self._handeye_fallback = bool(
            self.get_parameter('handeye_fallback').value)
        self._handeye_ttl_s = float(
            self.get_parameter('handeye_ttl_s').value)
        self._handeye_counts: dict = {}
        self._handeye_seen_at = 0
        self._future_debounce_s = float(
            self.get_parameter('future_slot_debounce_s').value)
        self._pending_future: dict = {}   # slot -> {key, since(ns)}
        self._next_slot_blocked = False   # next-step slot occupancy unresolved
        self._pending_fresh_timeout_s = float(
            self.get_parameter('pending_fresh_timeout_s').value)
        self._reflect_timeout_s = float(
            self.get_parameter('reflect_timeout_s').value)
        self._cups_seen_ns = 0            # last APPLIED /cups_on_table (ns)
        self._pending_action_at_ns = 0    # when the pending action arrived
        out_topic = str(self.get_parameter('llm_input_topic').value)

        self._recovery_clear_timeout_s = float(
            self.get_parameter('recovery_clear_timeout_s').value)
        self._recovery_settle_s = float(
            self.get_parameter('recovery_settle_s').value)
        self._recovery_freeze_timeout_s = float(
            self.get_parameter('recovery_freeze_timeout_s').value)
        self._max_consecutive_recoveries = int(
            self.get_parameter('max_consecutive_recoveries').value)
        # True while a fallen_recovery is the in-flight (frozen) action.
        self._recovery_in_flight = False
        # consecutive fallen_recovery dispatches without intervening progress.
        self._consecutive_recoveries = 0
        self._fallen_ttl_s = float(self.get_parameter('fallen_ttl_s').value)
        self._fallen_count_sample = 0   # last accepted /fallen_cups count
        self._fallen_seen_ns = 0        # when that sample was accepted (ns)
        self._fallen_settle_wait_s = float(
            self.get_parameter('fallen_settle_wait_s').value)

        self._builder = GoalStateBuilder()
        self._pending_action_result = None
        self._pending_action_before_world = None
        self._action_in_flight = False
        self._freeze_started_at = None
        self._unfreeze_at = None   # ns deadline for the post-settle unfreeze
        # fallen_recovery result awaiting a post-recovery hand-eye sample
        # before the next /llm_input: {'at_ns'}.
        self._pending_recovery = None
        # Set when a pyramid pick just FAILED (e.g. select_failed: the exo cup
        # the LLM tried to place had no graspable upright cup under the hand-eye
        # — typically a fallen cup the exo mis-counted as upright). While set,
        # _apply_fallen_to_payload exposes the hand-eye fallen_count even though
        # exo still reports cups, so the next decision can route to
        # fallen_recovery (which stands the cup and returns to HOME) instead of
        # retrying the un-pickable cup forever. Cleared on a pyramid success or
        # a new user command.
        self._recover_after_pick_fail = False
        # Set on a pyramid pick FAIL to DEFER the next /llm_input until a fresh
        # post-unfreeze hand-eye fallen sample lands ({'at_ns'}). Without this
        # the immediate publish runs while still frozen (stale sample) -> fallen
        # gates to 0 -> the loop retries the un-pickable cup forever. Released by
        # _on_fallen_cups on a fresh sample, or by _freeze_tick on timeout.
        self._pending_pick_fail = None

        self._pub = self.create_publisher(String, out_topic, 10)
        self.create_subscription(
            String, str(self.get_parameter('cups_on_table_topic').value),
            self._on_cups_on_table, 10)
        self.create_subscription(
            String, str(self.get_parameter('stack_topic').value),
            self._on_stack, 10)
        self.create_subscription(
            String, str(self.get_parameter('robot_state_topic').value),
            self._on_robot_state, 10)
        self.create_subscription(
            String, str(self.get_parameter('user_command_topic').value),
            self._on_user_command, 10)
        self.create_subscription(
            String, str(self.get_parameter('action_result_topic').value),
            self._on_action_result, 10)
        self.create_subscription(
            String, str(self.get_parameter('llm_output_topic').value),
            self._on_llm_output, 10)
        self.create_subscription(
            String, str(self.get_parameter('handeye_cups_topic').value),
            self._on_handeye_cups, 10)
        self.create_subscription(
            String, str(self.get_parameter('fallen_cups_topic').value),
            self._on_fallen_cups, 10)
        # Wall-clock check of the unfreeze deadline / freeze timeout so it
        # fires even if perception stops (the gate alone is tick-driven).
        self.create_timer(0.2, self._freeze_tick)

        self.get_logger().debug(
            f'goal_state_publisher: out={out_topic} '
            f'publish_on_world_change={self._on_world_change}')

    # ── Subscriptions ─────────────────────────────────────────────────────

    def _on_cups_on_table(self, msg: String) -> None:
        obj = self._parse(msg.data, '/cups_on_table')
        if obj is None:
            return
        if self._world_frozen():
            return  # pyramid in flight — hold the world steady
        self._builder.set_world(obj, None)
        self._cups_seen_ns = self.get_clock().now().nanoseconds
        if self._maybe_publish_pending_action():
            return
        if self._should_republish_on_change():
            self._publish()  # idle disturbance: real world change while no action

    def _on_stack(self, msg: String) -> None:
        obj = self._parse(msg.data, '/stack')
        if obj is None:
            return
        if self._world_frozen():
            return  # pyramid in flight — hold the world steady
        obj = self._debounce_future_slots(obj)
        self._builder.set_world(None, obj)
        if self._maybe_publish_pending_action():
            return
        if self._should_republish_on_change():
            self._publish()  # idle disturbance: real world change while no action

    def _on_robot_state(self, msg: String) -> None:
        obj = self._parse(msg.data, '/robot_state')
        if obj is not None:
            self._builder.set_robot_state(obj)

    def _on_fallen_cups(self, msg: String) -> None:
        obj = self._parse(msg.data, '/fallen_cups')
        if obj is None or 'count' not in obj:
            return
        if self._world_frozen():
            return  # arm mid-action — only at-HOME hand-eye reads may land
        self._fallen_count_sample = normalize_fallen_count(obj.get('count'))
        self._fallen_seen_ns = self.get_clock().now().nanoseconds
        if self._maybe_publish_pending_recovery():
            return
        # A fresh post-fail fallen sample releases a deferred pick-fail decision
        # (now _apply_fallen_to_payload can expose the real fallen count so the
        # loop routes to recovery instead of retrying the un-pickable cup).
        if (self._pending_pick_fail is not None
                and self._fallen_seen_ns > self._pending_pick_fail['at_ns']):
            self._pending_pick_fail = None
            self._publish()
            return
        # A fresh fallen sample may release a pending action held by the
        # done-race guard (upright ran out with null target slots still left).
        self._maybe_publish_pending_action()

    def _on_user_command(self, msg: String) -> None:
        # Plain text, not JSON. Empty string clears the command.
        cmd = msg.data.strip() or None
        self.get_logger().info(f'/user_command received: {cmd!r}')
        self._pending_action_result = None
        self._pending_action_before_world = None
        self._pending_recovery = None
        self._recover_after_pick_fail = False
        self._pending_pick_fail = None
        self._consecutive_recoveries = 0
        self._unfreeze_world('new user command')
        self._builder.set_user_command(cmd)
        self._publish()  # cold-start trigger

    def _on_action_result(self, msg: String) -> None:
        obj = self._parse(msg.data, '/action_result')
        if obj is None:
            return
        if obj.get('action') == 'fallen_recovery':
            self._schedule_unfreeze('fallen recovery finished')
            self._builder.on_action_result(obj)  # plan NOT advanced (interrupt)
            # Success or fail, hold the in-flight trigger until a hand-eye
            # sample taken AFTER the recovery lands. Publishing on the stale
            # pre-recovery sample would mis-gate fallen_count both ways:
            # re-issue after a success, or report 0 (-> wrong done) after a
            # fail whose cup is still down.
            self._pending_recovery = {
                'at_ns': self.get_clock().now().nanoseconds,
            }
            self.get_logger().info(
                f"recovery {obj.get('result')} — waiting for a fresh "
                'hand-eye fallen sample')
            return
        if obj.get('action') == 'unstack':
            # Interrupt: plan NOT advanced. Hold the next decision until the
            # removal reflects (the slot reads null), then the LLM replans to
            # refill it with the correct color. Reuse the pyramid pending-action
            # reflection path (action_result_reflected handles the unstack case
            # = slot now empty). A failed unstack publishes immediately so the
            # LLM can re-decide from the current world.
            self._schedule_unfreeze('post-unstack home settle')
            before = (self._builder.previous_world_state()
                      or self._builder.current_world_state())
            self._builder.on_action_result(obj)  # plan NOT advanced (interrupt)
            if obj.get('result') == 'success':
                # Progress was made — any pending pyramid-pick-fail recovery
                # assumption is now stale; clear it so it can't leak fallen
                # exposure into a later decision.
                self._recover_after_pick_fail = False
                self._pending_pick_fail = None
                self._pending_action_result = obj
                self._pending_action_at_ns = self.get_clock().now().nanoseconds
                self._pending_action_before_world = before
                if not self._maybe_publish_pending_action():
                    self.get_logger().info(
                        f'/action_result pending world update: {obj}')
                return
            self._publish()  # failed unstack — let the LLM re-decide
            return
        self._schedule_unfreeze('post-place home settle')
        before = (self._builder.previous_world_state()
                  or self._builder.current_world_state())
        self._builder.on_action_result(obj)
        if obj.get('result') == 'success' and obj.get('action') == 'pyramid':
            self._recover_after_pick_fail = False  # progress made -> clear
            self._pending_pick_fail = None
            self._consecutive_recoveries = 0  # real progress -> reset cap
            self._pending_action_result = obj
            self._pending_action_at_ns = self.get_clock().now().nanoseconds
            self._pending_action_before_world = before
            if not self._maybe_publish_pending_action():
                self.get_logger().info(
                    f'/action_result pending world update: {obj}')
            return
        # A pyramid fail no longer opens a fallen-recovery exception (removed —
        # it conflated "pick failed" with "cup is fallen" and mis-fired on exo
        # phantoms). plan_executor returns the arm HOME and retries the step
        # (#11); fallen recovery only triggers via the clean exo==0 policy. Here
        # we just re-publish so the LLM re-decides from the current world.
        if obj.get('action') == 'pyramid' and obj.get('result') == 'fail':
            self.get_logger().warn(
                f"pyramid pick failed ({obj.get('error')}) — re-deciding from "
                'current world (plan_executor homes + retries)')
        self._publish()  # other failures do not require a world-state delta

    def _on_llm_output(self, msg: String) -> None:
        # The future LLM node feeds its plan back so we can track goal/steps.
        obj = self._parse(msg.data, '/llm_output')
        if obj is None:
            return
        decision = obj.get('decision')
        if decision == 'fallen_recovery':
            # Interrupt: current_plan/current_goal stay untouched. The
            # recovery arm motion crosses the exo view, so freeze the world
            # exactly like a pyramid action (released by its /action_result).
            self._freeze_world('fallen recovery dispatched')
            # Recovery freezes get a longer timeout (see _freeze_tick) and are
            # counted for the anti-runaway cap (reset on progress / command).
            self._recovery_in_flight = True
            self._consecutive_recoveries += 1
            return
        if decision == 'unstack':
            # Interrupt like fallen_recovery: current_plan/current_goal stay
            # untouched. The unstack arm motion crosses the exo view, so freeze
            # the world (released by its /action_result).
            self._freeze_world('unstack dispatched')
            return
        if obj.get('status') == 'ok':
            self._builder.set_plan(obj)
        elif decision == 'replan' and obj.get('plan') is not None:
            self._builder.set_plan(obj.get('plan'))
        elif decision == 'done':
            # done = task complete: clear the plan (idle). (#7 post-done
            # disturbance watch is deferred to the stop-based approach.)
            self._builder.set_plan(None)
        # decision == 'continue' (or other): keep the existing plan.

        # Freeze ONLY when a step will actually execute. current_goal() is
        # the next remaining step (None when empty/done), so this covers
        # cold-start, replan, an EMPTY replan (no freeze -> no stuck freeze),
        # AND the normal 'continue' path (plan_executor starts the next step
        # on 'continue', so the world must be frozen for it too).
        if decision != 'done' and self._builder.current_goal() is not None:
            self._freeze_world(f"action dispatched ({decision or 'cold_start'})")
        else:
            self._unfreeze_world('no executable step / done')

    # ── World freeze during action execution ──────────────────────────────
    def _freeze_world(self, reason: str) -> None:
        if not self._freeze_enabled:
            return
        if not self._action_in_flight:
            self.get_logger().info(f'world FROZEN during action ({reason})')
        self._action_in_flight = True
        self._freeze_started_at = self.get_clock().now()
        self._unfreeze_at = None

    def _unfreeze_world(self, reason: str) -> None:
        if self._action_in_flight:
            self.get_logger().info(f'world UNFROZEN ({reason})')
        self._action_in_flight = False
        self._recovery_in_flight = False
        self._freeze_started_at = None
        self._unfreeze_at = None

    def _schedule_unfreeze(self, reason: str) -> None:
        """Keep frozen for unfreeze_settle_s more (perception settles after
        the arm homes), then a perception tick past the deadline resumes."""
        if not (self._freeze_enabled and self._action_in_flight):
            return
        self._unfreeze_at = (self.get_clock().now().nanoseconds
                             + int(self._unfreeze_settle_s * 1e9))
        self.get_logger().info(
            f'world unfreeze in {self._unfreeze_settle_s:.1f}s ({reason})')

    def _freeze_tick(self) -> None:
        """Wall-clock: release the freeze on the post-settle deadline or the
        safety timeout, independent of perception ticks."""
        if self._pending_recovery is not None:
            age = (self.get_clock().now().nanoseconds
                   - self._pending_recovery['at_ns']) * 1e-9
            if age >= self._recovery_clear_timeout_s:
                self._maybe_publish_pending_recovery(timed_out=True)
        if self._pending_pick_fail is not None:
            age = (self.get_clock().now().nanoseconds
                   - self._pending_pick_fail['at_ns']) * 1e-9
            if age >= self._recovery_clear_timeout_s:
                self._pending_pick_fail = None
                self.get_logger().warn(
                    'no fresh fallen sample after pick fail — deciding anyway')
                self._publish()
        if not self._action_in_flight:
            return
        if (self._unfreeze_at is not None
                and self.get_clock().now().nanoseconds >= self._unfreeze_at):
            self._unfreeze_world('post-place settle done')
            return
        if self._freeze_started_at is not None:
            elapsed = (self.get_clock().now()
                       - self._freeze_started_at).nanoseconds * 1e-9
            # Recovery runs far longer than a pyramid step; use its own timeout
            # so the freeze does NOT expire mid-recovery and start accepting
            # mid-motion /fallen_cups samples (which would poison the gate).
            timeout = (self._recovery_freeze_timeout_s
                       if self._recovery_in_flight else self._freeze_timeout_s)
            if elapsed > timeout:
                self.get_logger().warn(
                    'world freeze timed out (no /action_result) — resuming '
                    'perception')
                self._unfreeze_world('timeout')

    def _world_frozen(self) -> bool:
        # Pure gate (no side effects); release timing is the timer's job.
        return self._freeze_enabled and self._action_in_flight

    def _should_republish_on_change(self) -> bool:
        """Idle disturbance gate: re-publish only when (a) the feature is on,
        (b) a task is active — a plan/target exists (in_flight), covering both
        between-steps idle AND the post-done grace window where the target is
        retained, and (c) the world actually changed since the last publish (so
        we fire once per real disturbance, not every perception frame)."""
        if not self._on_world_change:
            return False
        if self._builder.mode() != 'in_flight':
            return False  # no active task -> nothing to measure a disturbance against
        return world_changed(
            self._builder.previous_world_state(),
            self._builder.current_world_state())

    def _maybe_publish_pending_recovery(self, timed_out: bool = False) -> bool:
        """Publish the post-recovery /llm_input once a hand-eye fallen sample
        taken AFTER the recovery has landed.

        Frozen-period samples are rejected by _on_fallen_cups, so the first
        accepted one is post-settle with the arm back at the shared sense
        HOME — the only camera pose whose fallen reading means anything. On
        timeout publish anyway — the gate then sees a stale sample (-> 0) and
        the LLM decides from the world state alone.
        """
        if self._pending_recovery is None:
            return False
        age = (self.get_clock().now().nanoseconds
               - self._pending_recovery['at_ns']) * 1e-9
        fresh = self._fallen_seen_ns > self._pending_recovery['at_ns']
        # Hold the fresh-sample publish until recovery_settle_s has elapsed so
        # exo cups_on_table reflects the stood-up cup; timed_out always proceeds.
        if not (timed_out or (fresh and age >= self._recovery_settle_s)):
            return False
        if fresh:
            self.get_logger().info(
                'post-recovery hand-eye sample landed: '
                f'fallen count={self._fallen_count_sample}')
        else:
            self.get_logger().warn(
                'recovery sample-wait timed out — publishing without a '
                'post-recovery hand-eye sample')
        self._pending_recovery = None
        self._publish()
        return True

    def _maybe_publish_pending_action(self) -> bool:
        if self._pending_action_result is None:
            return False
        # P1: require a fresh /cups_on_table applied since the action (freeze
        # suppresses cups during execution), time-bounded so a stalled
        # perception still advances on the last world.
        if self._cups_seen_ns < self._pending_action_at_ns:
            age = (self.get_clock().now().nanoseconds
                   - self._pending_action_at_ns) * 1e-9
            if age < self._pending_fresh_timeout_s:
                return False
            self.get_logger().warn(
                'no fresh /cups_on_table since action — proceeding on last world')
        # next step's slot occupancy still debouncing -> hold the decision.
        if self._next_slot_blocked:
            return False
        current = self._builder.current_world_state()
        if not action_result_reflected(
            self._pending_action_result,
            self._pending_action_before_world,
            current,
        ):
            age = (self.get_clock().now().nanoseconds
                   - self._pending_action_at_ns) * 1e-9
            if age < self._reflect_timeout_s:
                return False
            self.get_logger().warn(
                f'action not reflected in {self._reflect_timeout_s:.0f}s — '
                'publishing anyway so the LLM can replan/decide')
        # Done-race guard: the action reflected, but if upright cups are now
        # exhausted and the target still has null slots, a fallen cup could
        # finish it. Hold until a FRESH hand-eye fallen sample lands so the
        # decider sees the real count (a stale gate-to-0 -> premature done).
        if self._fallen_decision_pending(current):
            age = (self.get_clock().now().nanoseconds
                   - self._pending_action_at_ns) * 1e-9
            if age < self._fallen_settle_wait_s:
                return False
            self.get_logger().warn(
                'no fresh fallen sample after upright ran out — proceeding '
                '(fallen_count gates to 0)')
        result = self._pending_action_result
        self._pending_action_result = None
        self._pending_action_before_world = None
        self.get_logger().info(
            f'/action_result reflected in world state: {result}')
        self._publish()
        return True

    def _fallen_decision_pending(self, current: dict | None) -> bool:
        """True when this decision hinges on a fallen reading that is not yet
        fresh: the action emptied the last upright cup(s) AND the target still
        has null slots, but no fresh hand-eye fallen sample has landed. Mirrors
        the gate in _apply_fallen_to_payload (exo empty AND no hand-eye fill)
        so the guard fires only when fallen_count would actually be exposed."""
        if self._fallen_fresh():
            return False
        if not self._builder.null_target_slots():
            return False
        cups = (current or {}).get('cups_on_table') or {}
        total = sum(int(v) for v in cups.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
        if total > 0:
            return False
        if self._handeye_fresh():
            return False  # hand-eye fill will supply cups -> no done-race
        return True

    def _parse(self, raw: str, src: str) -> dict | None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            level = self.get_logger().error if self._strict \
                else self.get_logger().warn
            level(f'{src} dropped — invalid JSON ({e}): {raw[:120]!r}')
            return None
        if not isinstance(obj, dict):
            self.get_logger().warn(
                f'{src} payload is not a JSON object: {type(obj).__name__}')
            return None
        return obj

    # ── Publish ───────────────────────────────────────────────────────────

    def _on_handeye_cups(self, msg: String) -> None:
        obj = self._parse(msg.data, '/vision/cups_on_table_handeye')
        if obj is None:
            return
        self._handeye_counts = {
            str(k): int(v) for k, v in obj.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            and int(v) > 0}
        self._handeye_seen_at = self.get_clock().now().nanoseconds

    def _handeye_fresh(self) -> bool:
        if not (self._handeye_fallback and self._handeye_counts):
            return False
        age = (self.get_clock().now().nanoseconds
               - self._handeye_seen_at) * 1e-9
        return age <= self._handeye_ttl_s

    def _apply_handeye_to_payload(self, payload: dict) -> None:
        """DECISION-moment only: if exo cups_on_table is empty when an
        llm_input is about to be built, override the PAYLOAD's cups with the
        hand-eye counts. Overriding the payload (not the builder) keeps the
        real exo world as the reflection baseline (previous_world_state)."""
        cw = payload.get('current_world_state')
        if not isinstance(cw, dict):
            return
        cups = cw.get('cups_on_table') or {}
        total = sum(int(v) for v in cups.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
        if total > 0:
            return
        if self._handeye_fresh():
            self.get_logger().info(
                f'exo empty at decision -> hand-eye fill {self._handeye_counts}')
            cw['cups_on_table'] = dict(self._handeye_counts)

    def _fallen_fresh(self) -> bool:
        if not self._fallen_seen_ns:
            return False
        age = (self.get_clock().now().nanoseconds
               - self._fallen_seen_ns) * 1e-9
        return age <= self._fallen_ttl_s

    def _apply_fallen_to_payload(self, payload: dict) -> None:
        """DECISION-moment gate (runs AFTER the hand-eye cups fill): the
        hand-eye fallen count enters the payload ONLY when no graspable
        upright cup remains anywhere — exo said 0 and the hand-eye fill had
        nothing to add. Upright cups always win: recovery is physically
        impossible with upright cups nearby, so the LLM must never see
        fallen_count > 0 while a pickable cup exists. A stale sample (arm
        not parked at the sense HOME recently) gates to 0 — never block on
        the fallen path."""
        payload['fallen_count'] = 0
        cw = payload.get('current_world_state')
        if not isinstance(cw, dict):
            return   # no world yet -> no recovery decision either
        cups = cw.get('cups_on_table') or {}
        total = sum(int(v) for v in cups.values()
                    if isinstance(v, (int, float)) and not isinstance(v, bool))
        # Upright cups always win: fallen_count is exposed ONLY when exo sees no
        # graspable cup (total == 0). (The old pick-fail exception that exposed
        # fallen despite exo cups > 0 was removed — it conflated "pick failed"
        # with "cup is fallen" and mis-fired on exo phantoms. A pick fail now
        # just retries from HOME (#11); a real fallen cup surfaces when exo == 0.)
        if total > 0:
            return
        if not self._fallen_fresh():
            return
        # Anti-runaway: if recovery has been dispatched this many times in a row
        # without progress (pyramid success / new command resets the counter),
        # stop exposing fallen_count so the LLM cannot re-dispatch forever. Only
        # trips on a pathological loop (orchestrator repeatedly no-ops on a cup
        # the gate sees); a normal single-run recovery clears the cup in one
        # pass — the just-recovered cup then shows up as a placeable cup
        # (total > 0 above) and a pyramid step resets the counter.
        if (self._max_consecutive_recoveries > 0
                and self._consecutive_recoveries
                >= self._max_consecutive_recoveries):
            self.get_logger().error(
                f'fallen_recovery dispatched {self._consecutive_recoveries}x '
                'without progress — suppressing further recovery (check the '
                'hand-eye grasp detector / cup reachability)')
            return
        if self._fallen_count_sample > 0:
            self.get_logger().info(
                'no graspable cups at decision -> hand-eye '
                f'fallen_count={self._fallen_count_sample}')
        payload['fallen_count'] = self._fallen_count_sample

    def _debounce_future_slots(self, raw_stack: dict) -> dict:
        """Reflect the just-built slot immediately, but hold a FUTURE step's
        slot at null until its occupancy is stable for future_slot_debounce_s
        (transient false-positive -> dropped). Does NOT delay execution."""
        self._next_slot_blocked = False
        if not isinstance(raw_stack, dict):
            return raw_stack
        future = self._builder.remaining_slots()
        goal = self._builder.current_goal()
        next_slot = str(goal.get('target_slot')) if goal else None
        for slot in list(self._pending_future):
            if slot not in future:
                self._pending_future.pop(slot, None)   # executed -> drop
        if not future:
            return raw_stack
        now = self.get_clock().now().nanoseconds
        out = dict(raw_stack)
        for slot in future:
            val = raw_stack.get(slot)
            if val is None:
                self._pending_future.pop(slot, None)
                continue
            key = val if isinstance(val, str) else json.dumps(val, sort_keys=True)
            p = self._pending_future.get(slot)
            if p is None or p['key'] != key:
                self._pending_future[slot] = {'key': key, 'since': now}
                p = self._pending_future[slot]
            stable = (now - p['since']) * 1e-9 >= self._future_debounce_s
            if slot == next_slot:
                # The next step's slot must stay null in the payload until that
                # step actually runs: a flickered/premature occupancy here would
                # become the step's `before` state and deadlock its reflection.
                # Never commit it; mask null and (while still debouncing) hold
                # the decision. plan_executor's own raw-/stack occupied-skip
                # guards against a genuine collision.
                out[slot] = None
                if not stable:
                    self._next_slot_blocked = True
            elif not stable:
                out[slot] = None                       # later future -> mask
        return out

    def _publish(self) -> None:
        try:
            payload = self._builder.build_payload()
            if self._handeye_fallback:
                self._apply_handeye_to_payload(payload)
            self._apply_fallen_to_payload(payload)
            msg = String()
            msg.data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            self._pub.publish(msg)
            self._builder.commit(payload)
            self.get_logger().info(
                f'llm_input ({payload["mode"]}) published, {len(msg.data)} bytes')
        except Exception:
            self.get_logger().error(f'_publish failed:\n{traceback.format_exc()}')


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = GoalStatePublisher()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
