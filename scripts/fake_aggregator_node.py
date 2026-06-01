"""fake_aggregator_node — publish fixed experiment world-state topics.

This is fake data with the same topic shape the GSP already consumes:

  publish /cups_on_table
  publish /stack
  publish /user_command
  subscribe /action_result

It updates the fake world state after successful pyramid action results so the
next LLM input sees the same state transition perception would have reported.
"""
from __future__ import annotations

import json
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import String


STACK_SLOTS = [
    'L1_left',
    'L1_mid',
    'L1_right',
    'L2_left',
    'L2_right',
    'L3_top',
]


class FakeAggregatorNode(Node):
    def __init__(self) -> None:
        super().__init__('fake_aggregator_node')

        self.declare_parameter('cups_on_table_topic', '/cups_on_table')
        self.declare_parameter('stack_topic', '/stack')
        self.declare_parameter('user_command_topic', '/user_command')
        self.declare_parameter('action_result_topic', '/action_result')
        self.declare_parameter(
            'user_command',
            '3단 피라미드에서 1단만 쌓아줘',
        )
        self.declare_parameter('publish_period_s', 0.5)
        self.declare_parameter('initial_command_delay_s', 2.0)

        self._cups_on_table: dict[str, int] = {'red': 3}
        self._stack: dict[str, str | None] = {slot: None for slot in STACK_SLOTS}
        self._command_published = False
        self._started_at = time.monotonic()

        self._cups_pub = self.create_publisher(
            String,
            str(self.get_parameter('cups_on_table_topic').value),
            10,
        )
        self._stack_pub = self.create_publisher(
            String,
            str(self.get_parameter('stack_topic').value),
            10,
        )
        self._command_pub = self.create_publisher(
            String,
            str(self.get_parameter('user_command_topic').value),
            10,
        )
        self.create_subscription(
            String,
            str(self.get_parameter('action_result_topic').value),
            self._on_action_result,
            10,
        )
        self.create_timer(
            float(self.get_parameter('publish_period_s').value),
            self._publish,
        )
        self.get_logger().info(
            'fake_aggregator_node: publishing fixed cups/stack/user command')

    def _on_action_result(self, msg: String) -> None:
        try:
            result = json.loads(msg.data)
        except json.JSONDecodeError as e:
            self.get_logger().warn(f'/action_result invalid JSON: {e}')
            return
        if result.get('result') != 'success':
            return
        if result.get('action') != 'pyramid':
            return
        color = result.get('color')
        target_slot = result.get('target_slot')
        if not isinstance(color, str) or target_slot not in self._stack:
            return

        self._stack[target_slot] = color
        self._cups_on_table[color] = max(
            int(self._cups_on_table.get(color, 0)) - 1,
            0,
        )
        self._publish()

    def _publish(self) -> None:
        self._cups_pub.publish(
            String(data=json.dumps(self._cups_on_table, sort_keys=True)))
        self._stack_pub.publish(
            String(data=json.dumps(self._stack, sort_keys=True)))
        if not self._command_published:
            delay = float(self.get_parameter('initial_command_delay_s').value)
            if time.monotonic() - self._started_at < delay:
                return
            self._command_pub.publish(
                String(data=str(self.get_parameter('user_command').value)))
            self._command_published = True


def main(args: list[str] | None = None) -> None:
    rclpy.init(args=args)
    node = FakeAggregatorNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
