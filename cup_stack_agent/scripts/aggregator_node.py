"""aggregator_node — relay real vision world-state to the goal-state publisher.

Real (non-fake) replacement for fake_aggregator_node.py. The fake file is kept
as-is for reference; THIS node is the one wired into start.sh. No hardcoded
world state — it relays the real vision pipeline:

  subscribe cups_in_topic   (default /vision/cups_on_table)  <- point_cloud_node
  subscribe stack_in_topic  (default /vision/stack)          <- verifier_node
  publish   cups_out_topic  (default /cups_on_table)         -> goal_state_publisher
  publish   stack_out_topic (default /stack)                 -> goal_state_publisher
  publish   user_command_topic (default /user_command)

It also publishes /user_command — the one world-state input perception cannot
produce. GATED on vision readiness: the command is held until exo vision has
reported at least one cup AND has had `command_settle_s` to converge on the full
count. This prevents the cold-start /llm_input firing on an empty table
(cups_on_table: {}) before perception warms up — which made the LLM return
insufficient_resources and stall the loop. Set require_cups_before_command=false
to revert to the old fixed-delay behaviour.
"""
from __future__ import annotations

import json
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


def _cup_count(payload: str) -> int:
    """Total cups in a /cups_on_table JSON ({color: count}); 0 if empty/bad."""
    try:
        obj = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(obj, dict):
        return 0
    return sum(int(v) for v in obj.values() if isinstance(v, (int, float)))


class AggregatorNode(Node):
    def __init__(self, **kwargs) -> None:
        super().__init__('aggregator_node', **kwargs)

        self.declare_parameter('cups_in_topic', '/vision/cups_on_table')
        self.declare_parameter('stack_in_topic', '/vision/stack')
        self.declare_parameter('cups_out_topic', '/cups_on_table')
        self.declare_parameter('stack_out_topic', '/stack')
        self.declare_parameter('user_command_topic', '/user_command')
        self.declare_parameter('user_command', '3단 피라미드 쌓아줘')
        # Minimum floor before the command may fire.
        self.declare_parameter('initial_command_delay_s', 2.0)
        # Gate the command on vision readiness (see module docstring).
        self.declare_parameter('require_cups_before_command', True)
        # After the first non-empty cups report, wait this long so perception
        # converges on the full count before triggering the one-shot cold start.
        self.declare_parameter('command_settle_s', 3.0)
        self.declare_parameter('publish_period_s', 0.5)

        self._cups_pub = self.create_publisher(
            String, str(self.get_parameter('cups_out_topic').value), 10)
        self._stack_pub = self.create_publisher(
            String, str(self.get_parameter('stack_out_topic').value), 10)
        self._command_pub = self.create_publisher(
            String, str(self.get_parameter('user_command_topic').value), 10)

        self.create_subscription(
            String, str(self.get_parameter('cups_in_topic').value),
            self._relay_cups, 10)
        self.create_subscription(
            String, str(self.get_parameter('stack_in_topic').value),
            self._relay_stack, 10)

        self._command_published = False
        self._started_at = time.monotonic()
        self._cups_seen_at: float | None = None   # first non-empty cups report
        self.create_timer(
            float(self.get_parameter('publish_period_s').value),
            self._tick_command)
        self.get_logger().info(
            'aggregator_node: relaying '
            f'{self.get_parameter("cups_in_topic").value} -> '
            f'{self.get_parameter("cups_out_topic").value}, '
            f'{self.get_parameter("stack_in_topic").value} -> '
            f'{self.get_parameter("stack_out_topic").value}')

    # ------------------------------------------------------------------
    def _relay_cups(self, msg: String) -> None:
        self._cups_pub.publish(msg)
        if self._cups_seen_at is None and _cup_count(msg.data) > 0:
            self._cups_seen_at = time.monotonic()
            self.get_logger().info(
                'aggregator_node: first non-empty cups_on_table seen '
                f'({_cup_count(msg.data)} cups) — command gate open in '
                f'{float(self.get_parameter("command_settle_s").value):.1f}s')

    def _relay_stack(self, msg: String) -> None:
        self._stack_pub.publish(msg)

    def _tick_command(self) -> None:
        if self._command_published:
            return
        now = time.monotonic()
        if now - self._started_at < float(
                self.get_parameter('initial_command_delay_s').value):
            return
        if bool(self.get_parameter('require_cups_before_command').value):
            # hold until vision has reported cups AND settled
            if self._cups_seen_at is None:
                return
            if now - self._cups_seen_at < float(
                    self.get_parameter('command_settle_s').value):
                return
        command = str(self.get_parameter('user_command').value)
        self._command_pub.publish(String(data=command))
        self._command_published = True
        self.get_logger().info(f'/user_command published: {command!r}')


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = AggregatorNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
