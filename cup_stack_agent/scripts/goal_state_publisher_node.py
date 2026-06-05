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
from std_msgs.msg import String

from payload_builder import GoalStateBuilder, action_result_reflected


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

        self._strict = bool(self.get_parameter('strict_json').value)
        self._on_world_change = bool(
            self.get_parameter('publish_on_world_change').value)
        out_topic = str(self.get_parameter('llm_input_topic').value)

        self._builder = GoalStateBuilder()
        self._pending_action_result = None
        self._pending_action_before_world = None

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

        self.get_logger().info(
            f'goal_state_publisher: out={out_topic} '
            f'publish_on_world_change={self._on_world_change}')

    # ── Subscriptions ─────────────────────────────────────────────────────

    def _on_cups_on_table(self, msg: String) -> None:
        obj = self._parse(msg.data, '/cups_on_table')
        if obj is None:
            return
        self._builder.set_world(obj, None)
        if self._maybe_publish_pending_action():
            return
        if self._on_world_change:
            self._publish()  # in-flight: perception-detected change while idle

    def _on_stack(self, msg: String) -> None:
        obj = self._parse(msg.data, '/stack')
        if obj is None:
            return
        self._builder.set_world(None, obj)
        if self._maybe_publish_pending_action():
            return
        if self._on_world_change:
            self._publish()  # in-flight: perception-detected change while idle

    def _on_robot_state(self, msg: String) -> None:
        obj = self._parse(msg.data, '/robot_state')
        if obj is not None:
            self._builder.set_robot_state(obj)

    def _on_user_command(self, msg: String) -> None:
        # Plain text, not JSON. Empty string clears the command.
        cmd = msg.data.strip() or None
        self.get_logger().info(f'/user_command received: {cmd!r}')
        self._pending_action_result = None
        self._pending_action_before_world = None
        self._builder.set_user_command(cmd)
        self._publish()  # cold-start trigger

    def _on_action_result(self, msg: String) -> None:
        obj = self._parse(msg.data, '/action_result')
        if obj is None:
            return
        before = (self._builder.previous_world_state()
                  or self._builder.current_world_state())
        self._builder.on_action_result(obj)
        if obj.get('result') == 'success' and obj.get('action') == 'pyramid':
            self._pending_action_result = obj
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
        if obj.get('status') == 'ok':
            self._builder.set_plan(obj)
        elif obj.get('decision') == 'replan' and obj.get('plan') is not None:
            self._builder.set_plan(obj.get('plan'))
        elif obj.get('decision') == 'done':
            self._builder.set_plan(None)

    def _maybe_publish_pending_action(self) -> bool:
        if self._pending_action_result is None:
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

    def _publish(self) -> None:
        try:
            payload = self._builder.build_payload()
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
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
