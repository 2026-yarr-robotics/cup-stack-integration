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
from rclpy.qos import (QoSDurabilityPolicy, QoSProfile)
from std_msgs.msg import String

from payload_builder import (
    GoalStateBuilder, action_result_reflected, normalize_fallen_count,
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
        # Re-emit the payload whenever /system_state changes, so a disturbance
        # detected by perception while idle reaches the decider. Off by default
        # until expected-state simulation lands with the executor integration.
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
        # Agent-scan sync (2026-06-13 flow): after every command the arm
        # returns to its high home pose and cup_fusion auto-runs a hand scan
        # there (clear→capture→freeze, ~1.3 s). The NEXT /llm_input must
        # reason on that post-action fit, so the in-flight publish is held
        # until a 'frozen' event on scan_event_topic NEWER than the
        # /action_result. Auto-detects availability: the gate only engages
        # once at least one scan event has been seen (vision without the
        # scan FSM ⇒ no event ⇒ no added latency); timeout publishes anyway.
        self.declare_parameter('scan_event_topic', '/digital_twin/scan_event')
        self.declare_parameter('wait_scan_after_action', True)
        self.declare_parameter('scan_sync_timeout_s', 8.0)

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
        self._cups_seen_ns = 0            # last APPLIED /cups_on_table (ns)
        self._pending_action_at_ns = 0    # when the pending action arrived
        out_topic = str(self.get_parameter('llm_input_topic').value)

        self._recovery_clear_timeout_s = float(
            self.get_parameter('recovery_clear_timeout_s').value)
        self._fallen_ttl_s = float(self.get_parameter('fallen_ttl_s').value)
        self._fallen_count_sample = 0   # last accepted /fallen_cups count
        self._fallen_seen_ns = 0        # when that sample was accepted (ns)

        self._builder = GoalStateBuilder()
        self._pending_action_result = None
        self._pending_action_before_world = None
        self._action_in_flight = False
        self._freeze_started_at = None
        self._unfreeze_at = None   # ns deadline for the post-settle unfreeze
        # fallen_recovery result awaiting a post-recovery hand-eye sample
        # before the next /llm_input: {'at_ns'}.
        self._pending_recovery = None
        self._wait_scan = bool(
            self.get_parameter('wait_scan_after_action').value)
        self._scan_sync_timeout_s = float(
            self.get_parameter('scan_sync_timeout_s').value)
        self._scan_event_seen = False   # any event ⇒ scan FSM is alive
        self._scan_frozen_ns = 0        # event-payload 't' of last 'frozen'

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
        # TRANSIENT_LOCAL: matches cup_fusion's latched publisher (a stale
        # latched event cannot satisfy a new wait — payload 't' is compared
        # against the action timestamp).
        self.create_subscription(
            String, str(self.get_parameter('scan_event_topic').value),
            self._on_scan_event,
            QoSProfile(depth=1,
                       durability=QoSDurabilityPolicy.TRANSIENT_LOCAL))
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
        if self._on_world_change:
            self._publish()  # in-flight: perception-detected change while idle

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
        if self._on_world_change:
            self._publish()  # in-flight: perception-detected change while idle

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
        self._maybe_publish_pending_recovery()

    def _on_scan_event(self, msg: String) -> None:
        """cup_fusion scan lifecycle ('capture_start' | 'frozen'). A 'frozen'
        newer than the pending /action_result releases the scan-sync hold."""
        obj = self._parse(msg.data, '/digital_twin/scan_event')
        if obj is None:
            return
        self._scan_event_seen = True
        if obj.get('event') == 'frozen':
            try:
                self._scan_frozen_ns = int(float(obj.get('t', 0.0)) * 1e9)
            except (TypeError, ValueError):
                return
            self._maybe_publish_pending_action()

    def _on_user_command(self, msg: String) -> None:
        # Plain text, not JSON. Empty string clears the command.
        cmd = msg.data.strip() or None
        self.get_logger().info(f'/user_command received: {cmd!r}')
        self._pending_action_result = None
        self._pending_action_before_world = None
        self._pending_recovery = None
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
        self._schedule_unfreeze('post-place home settle')
        before = (self._builder.previous_world_state()
                  or self._builder.current_world_state())
        self._builder.on_action_result(obj)
        if obj.get('result') == 'success' and obj.get('action') == 'pyramid':
            self._pending_action_result = obj
            self._pending_action_at_ns = self.get_clock().now().nanoseconds
            self._pending_action_before_world = before
            if not self._maybe_publish_pending_action():
                self.get_logger().info(
                    f'/action_result pending world update: {obj}')
            return
        self._publish()  # failures do not require a world-state delta

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
            return
        if obj.get('status') == 'ok':
            self._builder.set_plan(obj)
        elif decision == 'replan' and obj.get('plan') is not None:
            self._builder.set_plan(obj.get('plan'))
        elif decision == 'done':
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
        if not self._action_in_flight:
            return
        if (self._unfreeze_at is not None
                and self.get_clock().now().nanoseconds >= self._unfreeze_at):
            self._unfreeze_world('post-place settle done')
            return
        if self._freeze_started_at is not None:
            elapsed = (self.get_clock().now()
                       - self._freeze_started_at).nanoseconds * 1e-9
            if elapsed > self._freeze_timeout_s:
                self.get_logger().warn(
                    'world freeze timed out (no /action_result) — resuming '
                    'perception')
                self._unfreeze_world('timeout')

    def _world_frozen(self) -> bool:
        # Pure gate (no side effects); release timing is the timer's job.
        return self._freeze_enabled and self._action_in_flight

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
        fresh = self._fallen_seen_ns > self._pending_recovery['at_ns']
        if not (fresh or timed_out):
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
        # P1b: agent-scan sync — the decision must see the POST-action hand
        # scan ([S] refresh at the home pose). Engages only when the scan FSM
        # is alive (≥1 event seen); time-bounded so a missed arrival/capture
        # still advances the loop.
        if (self._wait_scan and self._scan_event_seen
                and self._scan_frozen_ns <= self._pending_action_at_ns):
            age = (self.get_clock().now().nanoseconds
                   - self._pending_action_at_ns) * 1e-9
            if age < self._scan_sync_timeout_s:
                return False
            self.get_logger().warn(
                f'no post-action scan freeze within '
                f'{self._scan_sync_timeout_s:.1f}s — proceeding without it')
        # next step's slot occupancy still debouncing -> hold the decision.
        if self._next_slot_blocked:
            return False
        current = self._builder.current_world_state()
        if not action_result_reflected(
            self._pending_action_result,
            self._pending_action_before_world,
            current,
        ):
            return False
        result = self._pending_action_result
        self._pending_action_result = None
        self._pending_action_before_world = None
        self.get_logger().info(
            f'/action_result reflected in world state: {result}')
        self._publish()
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
        if total > 0:
            return
        if not self._fallen_fresh():
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
                # NEXT step's slot: a real occupancy here would collide with
                # the next place, so HOLD the decision until it reverts (false
                # positive) or stays stable (commit -> LLM replans/done/skip).
                if stable:
                    self.get_logger().info(
                        f'next slot {slot} occupancy stable '
                        f'{self._future_debounce_s:.0f}s -> commit (LLM replans)')
                    # leave out[slot] = val (real occupancy)
                else:
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
